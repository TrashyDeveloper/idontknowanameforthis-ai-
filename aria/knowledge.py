"""Knowledge base: stores what the bot knows and finds the entries relevant to a question.

Where knowledge comes from:
  * data/seed_knowledge.json - the starter knowledge shipped with the project (never modified)
  * data/packs/*.json        - knowledge packs, e.g. the EXILED API made by tools/learn_exiled.py
  * data/learned.json        - everything the bot learns from you (facts, corrections,
                               code examples, and feedback weights)

Search uses BM25 (a classic keyword-ranking formula) multiplied by a "trust" weight
that goes up when you mark answers as helpful and down when you mark them wrong.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = ("fact", "qa", "code")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "is", "it", "its", "me", "my", "of", "on", "or", "so", "that",
    "the", "this", "to", "was", "what", "when", "where", "which", "who", "why", "with",
    "you", "your", "should", "would", "could", "about", "tell", "please", "there", "get",
    "our", "we", "us", "they", "them", "best", "good", "way",
    # "how do I beat X" style words: the X is what matters
    "survive", "beat", "counter", "deal", "fight", "handle", "against", "stop", "avoid", "play", "playing", "some", "any", "all", "much", "many", "really", "know", "work", "use",
}

# Nicknames players use, mapped to the words that appear in the knowledge base.
ALIASES = {
    "peanut": "173", "shyguy": "096", "shy": "096", "larry": "106", "oldman": "106",
    "doctor": "049", "plague": "049", "zombie": "0492", "zombies": "0492", "dog": "939",
    "dogs": "939", "computer": "079", "pc": "079", "skeleton": "3114", "skelly": "3114",
    "nuke": "warhead", "nuked": "warhead", "ntf": "mtf", "ci": "chaos", "dclass": "classd",
    "d": "classd", "scientists": "scientist", "guards": "guard", "keycards": "keycard",
    "plugins": "plugin", "exiled": "exiled", "labapi": "labapi", "csharp": "c#",
}

MIN_WEIGHT, MAX_WEIGHT = 0.1, 3.0
USER_ENTRY_WEIGHT = 1.5  # things you taught it outrank the starter knowledge


def _split_camel(m: re.Match) -> str:
    word = m.group(0)
    return word + " " + " ".join(re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", word))


def stem(tok: str) -> str:
    """A tiny stemmer so "hurting", "hurt" and "spawned", "spawn" match."""
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        tok = tok[:-1]  # "items" -> "item"
    if len(tok) > 5 and tok.endswith("ing"):
        tok = tok[:-3]
    elif len(tok) > 4 and tok.endswith("ed"):
        tok = tok[:-2]
    if len(tok) > 4 and tok.endswith("e"):
        tok = tok[:-1]  # "escape" and "escap(ing)" both become "escap"
    return tok


def tokenize(text: str) -> list[str]:
    # Version numbers like "9.15.2" would match unrelated numbers, so they're ignored.
    text = re.sub(r"\bv?\d+\.\d+(?:\.\d+)*\b", " ", text)
    # Code names like "ChangingLeverStatus" are indexed whole and as separate words.
    text = re.sub(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*\b", _split_camel, text)
    text = text.lower()
    # Join "SCP-049-2" / "class-d" / "shy guy" style names into single tokens first.
    text = re.sub(r"scp[\s-]*(\d+)(?:[\s-]+(\d))?", lambda m: " " + m.group(1) + (m.group(2) or ""), text)
    text = re.sub(r"\bclass[\s-]*d\b", " classd ", text)
    text = re.sub(r"\bshy[\s-]*guy\b", " shyguy ", text)
    text = re.sub(r"\bold[\s-]*man\b", " oldman ", text)
    text = re.sub(r"\bd[\s-]*class\b", " dclass ", text)
    text = text.replace("c#", " csharp ")
    # Questions about the bot itself.
    text = re.sub(r"\b(who|what) are you\b|\byour name\b|\bwhat can you do\b|\byourself\b", " aria ", text)
    tokens = []
    for tok in re.findall(r"[a-z0-9]+", text):
        if tok in STOPWORDS or (len(tok) == 1 and tok not in ALIASES and not tok.isdigit()):
            continue
        tokens.append(stem(ALIASES.get(tok, tok)))
    return tokens


@dataclass
class Entry:
    id: str
    kind: str  # "fact", "qa" or "code"
    title: str  # a fact's topic, a question, or a code example's name
    text: str  # the fact / answer / explanation of the code
    code: str = ""
    framework: str = ""  # for code: "LabAPI", "EXILED", ...
    tags: list[str] = field(default_factory=list)
    source: str = "user"  # "seed" (starter knowledge and packs) or "user"
    pack: str = ""  # name of the knowledge pack it came from, e.g. "EXILED 9.15.2"
    weight: float = USER_ENTRY_WEIGHT
    created: float = field(default_factory=time.time)

    def search_text(self) -> str:
        parts = [self.title, self.title, self.text, " ".join(self.tags), self.framework]
        if self.code:
            # Identifiers in code are useful search words (e.g. "PlayerEvents", "Broadcast").
            parts.append(" ".join(re.findall(r"[A-Za-z]{4,}", self.code)))
        return " ".join(parts)


class KnowledgeBase:
    def __init__(self, seed_path: Path, learned_path: Path, packs_dir: Path | None = None):
        self.seed_path = Path(seed_path)
        self.learned_path = Path(learned_path)
        self.packs_dir = Path(packs_dir) if packs_dir else self.seed_path.parent / "packs"
        self._lock = threading.Lock()
        self._load()

    # ---------- loading and saving ----------

    def _load(self) -> None:
        seed = json.loads(self.seed_path.read_text(encoding="utf-8")) if self.seed_path.exists() else []
        learned = self._read_learned()
        self.entries: dict[str, Entry] = {}
        for raw in seed:
            entry = Entry(**{**raw, "source": "seed", "weight": raw.get("weight", 1.0)})
            self.entries[entry.id] = entry
        self.packs: list[dict] = []
        for path in sorted(self.packs_dir.glob("*.json")) if self.packs_dir.is_dir() else []:
            pack = json.loads(path.read_text(encoding="utf-8"))
            label = f"{pack.get('name', path.stem)} {pack.get('version', '')}".strip()
            for raw in pack["entries"]:
                entry = Entry(**{**raw, "source": "seed", "pack": label, "weight": 1.0})
                self.entries[entry.id] = entry
            self.packs.append({"name": label, "entries": len(pack["entries"])})
        for raw in learned["entries"]:
            entry = Entry(**{**raw, "source": "user"})
            self.entries[entry.id] = entry
        # Feedback on starter entries is saved separately so the seed file stays untouched.
        self.seed_weights: dict[str, float] = learned["seed_weights"]
        self.hidden: set[str] = set(learned["hidden"])
        for entry_id, weight in self.seed_weights.items():
            if entry_id in self.entries:
                self.entries[entry_id].weight = weight
        self._docs: dict[str, Counter] = {}
        self._reindex()

    def _read_learned(self) -> dict:
        data = {"entries": [], "seed_weights": {}, "hidden": []}
        if self.learned_path.exists():
            data.update(json.loads(self.learned_path.read_text(encoding="utf-8")))
        return data

    def _save(self) -> None:
        data = {
            "entries": [asdict(e) for e in self.entries.values() if e.source == "user"],
            "seed_weights": self.seed_weights,
            "hidden": sorted(self.hidden),
        }
        self.learned_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.learned_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.learned_path)

    def _reindex(self) -> None:
        # Entries are tokenized once and cached; only new ones are processed here.
        for eid in self._docs.keys() - self.entries.keys():
            del self._docs[eid]
        for eid, e in self.entries.items():
            if eid not in self._docs:
                self._docs[eid] = Counter(tokenize(e.search_text()))
        self._doc_len = {eid: sum(c.values()) for eid, c in self._docs.items()}
        self._avg_len = (sum(self._doc_len.values()) / len(self._doc_len)) if self._doc_len else 1.0
        df: Counter = Counter()
        for counts in self._docs.values():
            df.update(counts.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    # ---------- searching ----------

    def search(self, query: str, limit: int = 5, kinds: tuple[str, ...] | None = None,
               packs: bool = True) -> list[tuple[Entry, float]]:
        terms = tokenize(query)
        if not terms:
            return []
        k1, b = 1.5, 0.75
        results = []
        for eid, counts in self._docs.items():
            if eid in self.hidden:
                continue
            entry = self.entries[eid]
            if kinds and entry.kind not in kinds or not packs and entry.pack:
                continue
            score = 0.0
            for term in set(terms):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * self._doc_len[eid] / self._avg_len))
                score += self._idf.get(term, 0.0) * norm
            if score > 0:
                results.append((entry, score * entry.weight))
        results.sort(key=lambda r: r[1], reverse=True)
        return results[:limit]

    def coverage(self, entry: Entry, query: str) -> float:
        """Share of the question's words that appear in an entry (0.0 to 1.0)."""
        terms = set(tokenize(query))
        if not terms:
            return 0.0
        return sum(1 for t in terms if self._docs.get(entry.id, {}).get(t)) / len(terms)

    def get(self, entry_id: str) -> Entry | None:
        return self.entries.get(entry_id)

    def all_entries(self, include_hidden: bool = False) -> list[Entry]:
        return [e for e in self.entries.values() if include_hidden or e.id not in self.hidden]

    def stats(self) -> dict:
        visible = self.all_entries()
        return {
            "total": len(visible),
            "learned": sum(1 for e in visible if e.source == "user"),
            "code_examples": sum(1 for e in visible if e.kind == "code" and not e.pack),
            "packs": self.packs,
        }

    # ---------- learning ----------

    def add(self, kind: str, title: str, text: str, code: str = "", framework: str = "",
            tags: list[str] | None = None) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        title, text, code = title.strip(), text.strip(), code.strip()
        if not title or not (text or code):
            raise ValueError("an entry needs a title and some text or code")
        entry = Entry(id=f"user-{uuid.uuid4().hex[:10]}", kind=kind, title=title, text=text,
                      code=code, framework=framework.strip(), tags=[t.strip().lower() for t in tags or [] if t.strip()])
        with self._lock:
            self.entries[entry.id] = entry
            self._save()
            self._reindex()
        return entry

    def feedback(self, entry_ids: list[str], helpful: bool) -> None:
        """Make entries that gave a good answer more trusted, and bad ones less trusted."""
        with self._lock:
            for eid in entry_ids:
                entry = self.entries.get(eid)
                if not entry:
                    continue
                change = 0.2 if helpful else -0.3
                entry.weight = round(min(MAX_WEIGHT, max(MIN_WEIGHT, entry.weight + change)), 3)
                if entry.source == "seed":
                    self.seed_weights[eid] = entry.weight
            self._save()

    def remove(self, entry_id: str) -> bool:
        """Delete something you taught, or hide a starter entry you think is wrong."""
        with self._lock:
            entry = self.entries.get(entry_id)
            if not entry:
                return False
            if entry.source == "user":
                del self.entries[entry_id]
                self._reindex()
            else:
                self.hidden.add(entry_id)
            self._save()
        return True
