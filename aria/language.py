"""How Aria reads what you type: tokens, synonyms, filler words, typos and what you're asking for.

Offline, Aria matches words, not meaning. This module makes that much more forgiving:
  * filler words ("I need you Aria to ...", "please", "could you") are ignored
  * words with the same meaning ("damage"/"hurt", "demo"/"example") find each other
  * code names are split ("ChangingLeverStatus" -> changing, lever, status)
  * word endings are trimmed ("hurting" -> "hurt", "escaped" -> "escap")
  * typos are fixed against the words Aria knows (see KnowledgeBase.correct)
"""

from __future__ import annotations

import re

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "is", "it", "its", "me", "my", "of", "on", "or", "so", "that",
    "the", "this", "to", "was", "what", "when", "where", "which", "who", "why", "with",
    "you", "your", "should", "would", "could", "about", "tell", "please", "there", "get",
    "our", "we", "us", "they", "them", "best", "good", "way", "some", "any", "all", "much",
    "many", "really", "know", "work", "use", "will", "just", "also", "very", "too", "into",
    "then", "than", "have", "has", "had", "been", "being", "am", "im",
    # "how do I beat X" style words: the X is what matters
    "survive", "beat", "counter", "deal", "fight", "handle", "against", "stop", "avoid", "play", "playing",
    # talking to Aria / asking for things
    "hey", "hi", "hello", "yo", "pls", "plz", "thanks", "thank", "thx", "ok", "okay",
    "need", "want", "wanna", "gonna", "like", "let", "lets", "let's", "kind", "sort", "maybe",
    "something", "anything", "thing", "things", "stuff", "one", "some", "new", "now", "again",
    "make", "makes", "making", "made", "write", "writing", "create", "creating", "build", "building",
    "generate", "show", "explain", "help", "simple", "basic", "quick", "small",
    "gimme", "u", "ur", "take", "takes", "taking", "got", "where", "while", "whenever", "someone",
    "somebody", "everyone", "able", "possible", "ways",
    # chat slang and filler
    "whats", "wats", "wat", "whatis", "sup", "up", "guy", "guys", "dude", "bro", "bruh", "lol", "lmao", "xd",
    "tho", "though", "info", "information", "details", "stuff", "basically", "actually", "literally", "idk",
    "know", "knows", "wanna", "needs", "going", "gotta", "kinda", "sorta", "yeah", "yes", "no", "nah",
}

# Nicknames players use, mapped to the words that appear in the knowledge base.
ALIASES = {
    "peanut": "173", "shyguy": "096", "shy": "096", "larry": "106", "oldman": "106",
    "doctor": "049", "plague": "049", "zombie": "0492", "zombies": "0492", "dog": "939",
    "dogs": "939", "computer": "079", "pc": "079", "skeleton": "3114", "skelly": "3114",
    "nuke": "warhead", "nuked": "warhead", "ntf": "mtf", "ci": "chaos", "dclass": "classd",
    "d": "classd", "scientists": "scientist", "guards": "guard", "keycards": "keycard",
    "plugins": "plugin", "csharp": "c#", "hp": "health", "tp": "teleport", "cmd": "command",
    "ra": "remoteadmin", "nw": "northwood", "sl": "scpsl", "scpsl": "scpsl",
}

# Words that mean (roughly) the same thing. A word in a question also searches for the
# others in its group, at a lower weight, so the exact wording matters less.
SYNONYM_GROUPS = [
    ["example", "examples", "sample", "demo", "template", "snippet", "tutorial", "showcase"],
    ["plugin", "mod", "addon", "script", "extension"],
    ["kill", "killed", "kills", "die", "dies", "died", "death", "dying", "dead", "murder"],
    ["hurt", "hurting", "damage", "damaged", "injure", "harm", "hit", "shoot", "shot"],
    ["heal", "healing", "health", "medkit", "regen", "regenerate"],
    ["spawn", "spawned", "spawning", "respawn"],
    ["give", "add", "grant", "receive"],
    ["item", "items", "object", "loot", "pickup"],
    ["weapon", "gun", "firearm", "rifle", "pistol"],
    ["teleport", "warp", "position", "move"],
    ["door", "doors", "gate", "gates"],
    ["player", "players", "user", "person", "people", "human", "humans"],
    ["role", "roles", "class", "classes", "team"],
    ["custom", "customrole", "customitem", "own", "personal"],
    ["message", "broadcast", "hint", "text", "announce", "notify", "chat"],
    ["cassie", "announcement", "announcer"],
    ["ban", "kick", "punish", "mute"],
    ["command", "commands", "remoteadmin", "console"],
    ["config", "configuration", "setting", "settings", "option", "options"],
    ["round", "game", "match"],
    ["start", "starts", "started", "begin", "begins"],
    ["end", "ends", "ended", "finish", "over"],
    ["leave", "left", "disconnect", "quit"],
    ["join", "joined", "connect", "connected", "verified"],
    ["elevator", "lift"],
    ["effect", "effects", "status", "buff", "debuff"],
    ["speed", "fast", "faster", "movement", "boost"],
    ["invisible", "invisibility", "hidden"],
    ["grenade", "explosive", "frag", "explosion", "flashbang"],
    ["escape", "escaping", "escaped"],
    ["upgrade", "914", "refine"],
    ["keycard", "card", "access", "permission"],
    ["warhead", "detonate", "detonation", "explode"],
    ["decontamination", "decon"],
    ["ability", "abilities", "power", "skill"],
    ["error", "bug", "crash", "exception", "broken", "issue"],
]

# Asking Aria to produce code ("make an example", "write a plugin", "code me a ...").
CODE_REQUEST = re.compile(
    r"\b(make|write|create|build|generate|code|do|need|want|give me|show me)\b.*"
    r"\b(examples?|plugins?|scripts?|code|snippets?|templates?|demos?|commands?|mods?|event handlers?|custom ?\w+|abilit(?:y|ies)|patch(?:es)?)\b",
    re.IGNORECASE,
)
CODE_WORDS = {
    "plugin", "plugins", "code", "c#", "csharp", "script", "event", "events", "handler",
    "command", "exiled", "labapi", "nwapi", "dll", "compile", "method", "config", "api",
    "property", "properties", "enum", "args", "eventargs", "ev", "hook", "harmony",
    "customrole", "customroles", "customitem", "customitems", "snippet",
}
# "show me", "what do you know about X", "list", "examples": answer with everything related.
LIST_REQUEST = re.compile(r"\b(what (?:do |else )?you know|show me|list|examples|everything|overview|tell me about)\b",
                          re.IGNORECASE)
EXAMPLES = re.compile(r"\b(examples?|samples?|demos?|templates?|snippets?)\b", re.IGNORECASE)


def _split_camel(m: re.Match) -> str:
    word = m.group(0)
    return word + " " + " ".join(re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", word))


def stem(tok: str) -> str:
    """A tiny stemmer so "hurting"/"hurt" and "spawned"/"spawn" match."""
    if len(tok) > 4 and tok.endswith("ies"):
        tok = tok[:-3] + "y"  # "abilities" -> "ability"
    elif len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        tok = tok[:-1]  # "items" -> "item"
    if len(tok) > 5 and tok.endswith("ing"):
        tok = tok[:-3]
    elif len(tok) > 4 and tok.endswith("ed"):
        tok = tok[:-2]
    if len(tok) > 4 and tok.endswith("e"):
        tok = tok[:-1]  # "escape" and "escap(ing)" both become "escap"
    return tok


def normalize(text: str) -> str:
    """Rewrites a text so names written in different ways become the same words."""
    # Version numbers like "9.15.2" would match unrelated numbers, so they're dropped.
    text = re.sub(r"\bv?\d+\.\d+(?:\.\d+)*\b", " ", text)
    # Code names like "ChangingLeverStatus" are kept whole and also split into words.
    text = re.sub(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*\b", _split_camel, text)
    text = text.lower().replace("’", "'")
    # Join "SCP-049-2" / "class-d" / "shy guy" style names into single tokens.
    text = re.sub(r"scp[\s-]*(\d+)(?:[\s-]+(\d))?", lambda m: " " + m.group(1) + (m.group(2) or ""), text)
    text = re.sub(r"\bclass[\s-]*d\b", " classd ", text)
    text = re.sub(r"\bd[\s-]*(?:boys?|class)\b", " classd ", text)
    text = re.sub(r"\bshy[\s-]*guy\b", " shyguy ", text)
    text = re.sub(r"\bold[\s-]*man\b", " oldman ", text)
    text = re.sub(r"\bcustom[\s-]+(role|item)s?\b", r" custom \1 custom\1 ", text)
    text = re.sub(r"\bremote[\s-]*admin\b", " remoteadmin ", text)
    text = text.replace("c#", " csharp ")
    # Questions about Aria herself become one word; "Aria" used to address her is dropped.
    text = re.sub(r"\b(?:who|what) (?:are you|is aria)\b|\byour name\b|\bwhat can you do\b|\byourself\b|\babout aria\b",
                  " aria_self ", text)
    text = re.sub(r"\baria\b", " ", text)
    return text


def token_pairs(text: str) -> list[tuple[str, str]]:
    """(the word as typed, the word Aria searches for) for every meaningful word."""
    pairs = []
    for tok in re.findall(r"[a-z0-9_]+", normalize(text)):
        if tok == "aria_self":
            pairs.append(("aria", "aria"))
            continue
        if tok in STOPWORDS or (len(tok) == 1 and tok not in ALIASES and not tok.isdigit()):
            continue
        pairs.append((stem(tok), stem(ALIASES.get(tok, tok))))
    return pairs


def tokenize(text: str) -> list[str]:
    return [final for _, final in token_pairs(text)]


SYNONYMS: dict[str, set[str]] = {}
for _group in SYNONYM_GROUPS:
    _stems = {stem(ALIASES.get(w, w)) for w in _group}
    for _s in _stems:
        SYNONYMS.setdefault(_s, set()).update(_stems - {_s})


CODE_TOKENS = {stem(ALIASES.get(w, w)) for w in CODE_WORDS} | {
    "exampl", "sampl", "patch", "customrol", "customitem", "eventarg", "handler", "exil"}


def wants_code(message: str, tokens: list[str] | None = None) -> bool:
    """Whether a message is about plugins/code. Pass the (typo-corrected) tokens if you have them."""
    words = set(re.findall(r"[a-z#]+", message.lower()))
    if words & CODE_WORDS or CODE_REQUEST.search(message):
        return True
    if tokens and set(tokens) & CODE_TOKENS:
        return True
    # A code name like "DamageType" or "Player.Hurting" also means it's a code question.
    return bool(re.search(r"\b[A-Z][a-z]+[A-Z]\w*|\b[A-Z]\w+\.[A-Z]\w+", message))


def wants_list(message: str) -> bool:
    return bool(LIST_REQUEST.search(message))


def wants_examples(message: str) -> bool:
    return bool(EXAMPLES.search(message))
