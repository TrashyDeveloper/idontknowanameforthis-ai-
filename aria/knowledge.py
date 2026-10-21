"""Knowledge base: stores what Aria knows, finds what's relevant to a question, and learns.

Where knowledge comes from:
  * data/seed_knowledge.json - the starter knowledge shipped with the project (never modified)
  * data/packs/*.json        - knowledge packs, e.g. the EXILED API made by tools/learn_exiled.py
  * data/learned.json        - everything Aria learns from you: facts, corrections, code examples,
                               feedback weights, words you taught her, and the words you use

Search uses BM25 (a classic keyword-ranking formula) multiplied by a "trust" weight that goes
up when you mark answers as helpful and down when you mark them wrong. Questions are expanded
with synonyms and words you taught her, and typos are corrected against the words she knows.

Self-learning: when you 👍 an answer, the words of your question are linked to the knowledge
that answered it, so next time the same way of asking finds it straight away.
"""

from __future__ import annotations

import difflib
import json
import math
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .language import SYNONYMS, token_pairs, tokenize

KINDS = ("fact", "qa", "code")

MIN_WEIGHT, MAX_WEIGHT = 0.1, 3.0
USER_ENTRY_WEIGHT = 1.5  # things you taught her outrank the starter knowledge
SYNONYM_WEIGHT = 0.5  # a synonym counts half as much as the word you actually typed
ASSOCIATION_BOOST = 2  # each learned word counts like this many mentions in the entry
MAX_ASSOCIATIONS = 40  # learned words kept per entry
TITLE_BONUS = 0.5  # up to +50% when every word of the question is in the title
MAX_INDEXED_CODE = 2500  # characters of an entry's code used for searching


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
            # Only the start of long files is indexed, so huge files don't match every question.
            parts.append(self.code[:MAX_INDEXED_CODE])
        return " ".join(parts)


@dataclass
class Query:
    """A question broken into words. Each word may also match synonyms or a typo fix."""
    text: str
    groups: list[dict[str, float]]  # one {token: weight} per word typed

    @property
    def tokens(self) -> list[str]:
        return [t for g in self.groups for t, w in g.items() if w == 1.0]


class KnowledgeBase:
    def __init__(self, seed_path: Path, learned_path: Path, packs_dir: Path | None = None):
        self.seed_path = Path(seed_path)
        self.learned_path = Path(learned_path)
        self.packs_dir = Path(packs_dir) if packs_dir else self.seed_path.parent / "packs"
        self._lock = threading.RLock()
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
        self.synonyms: dict[str, list[str]] = learned["synonyms"]  # word -> what it means
        self.associations: dict[str, dict[str, int]] = learned["associations"]  # entry -> {word: count}
        self.settings: dict = learned["settings"]  # e.g. {"mode": "offline"}
        for entry_id, weight in self.seed_weights.items():
            if entry_id in self.entries:
                self.entries[entry_id].weight = weight
        self._docs: dict[str, Counter] = {}
        self._titles: dict[str, set[str]] = {}
        self._reindex()

    def _read_learned(self) -> dict:
        data = {"entries": [], "seed_weights": {}, "hidden": [], "synonyms": {}, "associations": {}, "settings": {}}
        if self.learned_path.exists():
            try:
                data.update(json.loads(self.learned_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Never lose what she learned: keep the damaged file and start fresh.
                backup = self.learned_path.with_name(f"learned.broken-{int(time.time())}.json")
                self.learned_path.replace(backup)
                print(f"Warning: {self.learned_path.name} was damaged. It was moved to {backup.name}.")
        return data

    def _save(self) -> None:
        data = {
            "entries": [asdict(e) for e in self.entries.values() if e.source == "user"],
            "seed_weights": self.seed_weights,
            "hidden": sorted(self.hidden),
            "synonyms": self.synonyms,
            "associations": self.associations,
            "settings": self.settings,
        }
        self.learned_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.learned_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.learned_path)

    def _doc(self, entry: Entry) -> Counter:
        counts = Counter(tokenize(entry.search_text()))
        for word, n in self.associations.get(entry.id, {}).items():
            counts[word] += n * ASSOCIATION_BOOST
        return counts

    def _reindex(self, changed: set[str] | None = None) -> None:
        # Entries are tokenized once and cached; only new or changed ones are processed.
        for eid in self._docs.keys() - self.entries.keys():
            del self._docs[eid]
            self._titles.pop(eid, None)
        for eid, e in self.entries.items():
            if eid not in self._docs or (changed and eid in changed):
                self._docs[eid] = self._doc(e)
                self._titles[eid] = set(tokenize(e.title))
        self._doc_len = {eid: sum(c.values()) for eid, c in self._docs.items()}
        self._avg_len = (sum(self._doc_len.values()) / len(self._doc_len)) if self._doc_len else 1.0
        df: Counter = Counter()
        for counts in self._docs.values():
            df.update(counts.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self._vocab_by_letter: dict[str, list[str]] = defaultdict(list)
        for t in self._idf:
            if len(t) >= 3 and not t.isdigit():
                self._vocab_by_letter[t[0]].append(t)

    # ---------- understanding questions ----------

    def correct(self, token: str) -> str | None:
        """The known word closest to a typo ("exild" -> "exil"), or None."""
        if token in self._idf or token in self.synonyms or len(token) < 4 or token.isdigit():
            return None
        match = difflib.get_close_matches(token, self._vocab_by_letter.get(token[0], []), n=1, cutoff=0.8)
        return match[0] if match else None

    def analyze(self, text: str) -> Query:
        groups = []
        for typed, tok in token_pairs(text):
            # Words you taught win over typo fixing ("glorp" stays "glorp").
            taught = self.synonyms.get(typed, []) + (self.synonyms.get(tok, []) if tok != typed else [])
            fixed = None if taught else self.correct(tok)
            group = {fixed or tok: 1.0}
            for meaning in taught:
                for t in tokenize(meaning):
                    group.setdefault(t, 1.0)
            for base in list(group):
                for syn in SYNONYMS.get(base, ()):
                    group.setdefault(syn, SYNONYM_WEIGHT)
            groups.append(group)
        return Query(text, groups)

    # ---------- searching ----------

    def search(self, query: str | Query, limit: int = 5, kinds: tuple[str, ...] | None = None,
               packs: bool = True) -> list[tuple[Entry, float]]:
        q = query if isinstance(query, Query) else self.analyze(query)
        if not q.groups:
            return []
        k1, b = 1.5, 0.75
        with self._lock:
            docs = list(self._docs.items())
            doc_len, avg_len, idf, titles = self._doc_len, self._avg_len, self._idf, self._titles
        results = []
        for eid, counts in docs:
            entry = self.entries.get(eid)
            if entry is None or eid in self.hidden:
                continue
            if kinds and entry.kind not in kinds or not packs and entry.pack:
                continue
            # Each word typed counts once: its exact form, a typo fix or its best synonym.
            score, matched = 0.0, 0
            for group in q.groups:
                best = 0.0
                for term, w in group.items():
                    tf = counts.get(term, 0)
                    if tf:
                        norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_len.get(eid, 1) / avg_len))
                        best = max(best, w * idf.get(term, 0.0) * norm)
                if best:
                    score += best
                    matched += 1
            if score > 0:
                # Matching more of the question's words matters more than matching one word a lot.
                cover = matched / len(q.groups)
                # Words found in the title count extra: titles say what an entry is about.
                title = titles.get(eid, set())
                in_title = sum(1 for g in q.groups if any(t in title for t, w in g.items() if w == 1.0))
                bonus = 1 + TITLE_BONUS * in_title / len(q.groups)
                results.append((entry, score * entry.weight * bonus * (0.3 + 0.7 * cover) ** 2))
        results.sort(key=lambda r: r[1], reverse=True)
        return results[:limit]

    def coverage(self, entry: Entry, query: str | Query) -> float:
        """Share of the question's words (or their synonyms) found in an entry (0.0 to 1.0)."""
        q = query if isinstance(query, Query) else self.analyze(query)
        if not q.groups:
            return 0.0
        doc = self._docs.get(entry.id, {})
        return sum(1 for g in q.groups if any(doc.get(t) for t in g)) / len(q.groups)

    def get(self, entry_id: str) -> Entry | None:
        return self.entries.get(entry_id)

    def all_entries(self, include_hidden: bool = False) -> list[Entry]:
        return [e for e in self.entries.values() if include_hidden or e.id not in self.hidden]

    def hidden_entries(self) -> list[Entry]:
        return [self.entries[eid] for eid in sorted(self.hidden) if eid in self.entries]

    def stats(self) -> dict:
        visible = self.all_entries()
        return {
            "total": len(visible),
            "learned": sum(1 for e in visible if e.source == "user"),
            "code_examples": sum(1 for e in visible if e.kind == "code" and not e.pack),
            "hidden": len(self.hidden),
            "words": len(self.synonyms),
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
        with self._lock:
            # Teaching the same thing twice doesn't create a duplicate.
            for e in self.entries.values():
                if e.source == "user" and e.kind == kind and e.title.lower() == title.lower() \
                        and e.text.lower() == text.lower() and e.code == code:
                    return e
            entry = Entry(id=f"user-{uuid.uuid4().hex[:10]}", kind=kind, title=title, text=text,
                          code=code, framework=framework.strip(),
                          tags=[t.strip().lower() for t in tags or [] if t.strip()])
            self.entries[entry.id] = entry
            self._save()
            self._reindex()
        return entry

    def feedback(self, entry_ids: list[str], helpful: bool, question: str = "") -> None:
        """Trust entries that gave a good answer more and bad ones less.

        On a 👍 the question's words are also linked to the main entry that answered it.
        """
        with self._lock:
            for eid in entry_ids:
                entry = self.entries.get(eid)
                if not entry:
                    continue
                change = 0.2 if helpful else -0.3
                entry.weight = round(min(MAX_WEIGHT, max(MIN_WEIGHT, entry.weight + change)), 3)
                if entry.source == "seed":
                    self.seed_weights[eid] = entry.weight
            main = next((eid for eid in entry_ids if eid in self.entries), None)
            if helpful and question and main:
                self._associate(main, question)
            self._save()

    def _associate(self, entry_id: str, question: str) -> None:
        words = self.associations.setdefault(entry_id, {})
        for tok in set(self.analyze(question).tokens):
            words[tok] = words.get(tok, 0) + 1
        if len(words) > MAX_ASSOCIATIONS:
            self.associations[entry_id] = dict(Counter(words).most_common(MAX_ASSOCIATIONS))
        self._reindex(changed={entry_id})

    def learn_word(self, word: str, meaning: str) -> list[str]:
        """Teach a word: "tp means teleport" makes "tp" also search for "teleport"."""
        tokens, meaning = [typed for typed, _ in token_pairs(word)], meaning.strip().lower()
        if not tokens or not tokenize(meaning):
            raise ValueError("I couldn't understand that word")
        with self._lock:
            for tok in tokens:
                known = self.synonyms.setdefault(tok, [])
                if meaning not in known:
                    known.append(meaning)
            self._save()
        return tokens

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self.settings[key] = value
            self._save()

    def forget_word(self, word: str) -> None:
        with self._lock:
            self.synonyms.pop(word, None)
            self._save()

    def remove(self, entry_id: str) -> bool:
        """Delete something you taught, or hide a starter entry you think is wrong."""
        with self._lock:
            entry = self.entries.get(entry_id)
            if not entry:
                return False
            if entry.source == "user":
                del self.entries[entry_id]
                self.associations.pop(entry_id, None)
                self._reindex()
            else:
                self.hidden.add(entry_id)
            self._save()
        return True

    def restore(self, entry_id: str) -> bool:
        """Un-hide a starter entry."""
        with self._lock:
            if entry_id not in self.hidden:
                return False
            self.hidden.discard(entry_id)
            self._save()
        return True
