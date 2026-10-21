"""Aria's "brain": picks relevant knowledge and turns it into an answer.

Two modes:
  * Claude mode (when an Anthropic API key is available): the relevant knowledge and
    code examples are given to Claude, which writes a full answer or C# plugin.
  * Offline mode (no key): answers straight from the knowledge base. It can only
    show what it has been taught, but it works with no account and no internet.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from .knowledge import Entry, KnowledgeBase, Query
from .language import CODE_REQUEST, wants_code, wants_examples, wants_list

MODEL = os.environ.get("ARIA_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("ARIA_EFFORT", "medium")
HISTORY_TURNS = 10  # how many earlier messages are sent along for context
MIN_OFFLINE_COVERAGE = 0.6  # below this share of matching words, Aria says she's not sure
RELEVANCE_CUTOFF = 0.35  # drop search results scoring under 35% of the best one

SYSTEM_PROMPT = """You are Aria, an assistant specialised in the game SCP: Secret Laboratory \
(by Northwood Studios) and in writing C# server plugins for it.

People talk to you casually, with typos, slang and incomplete sentences. Work out what they \
mean and answer that; only ask a question back when the request could mean very different things.

Each user message may start with a <knowledge> block holding entries from your knowledge base. \
Entries with a pack attribute were extracted from that framework's real source code at the \
stated version, so treat their event names, members and signatures as accurate. \
Entries marked source="user" were taught or corrected by your user; trust them over your own \
memory and over source="seed" entries when they disagree. The game is updated often, so if \
neither the knowledge nor your memory is certain for the current version, say so plainly \
instead of guessing.

When asked for plugin code:
- Follow the framework, namespaces and style of the code examples provided. If the user names a \
framework (LabAPI or EXILED), use it; otherwise use the one the examples use most.
- Only call API members that appear in the examples or that you are confident exist; point out \
any member you are unsure of so the user can check it.
- Give complete, compilable files, then a short note on how to build and install them.

Keep answers friendly and to the point. Use Markdown, with ```csharp blocks for code."""

DONT_KNOW = ("I don't know anything about that yet. You can teach me: press 👎 and type the right answer, "
             "or just tell me (for example *remember: ...*).")
OFFLINE_CODE_NOTE = ("_I'm in offline mode, so I can only show code I've learned. With an Anthropic API key "
                     "I can write new plugins for you (see the README)._")


def render_entry(e: Entry) -> str:
    if "remembered" in e.tags:
        return f"You told me: _{e.text}_"
    if e.kind == "code":
        framework = f" ({e.framework})" if e.framework else ""
        return f"**{e.title}**{framework}\n\n{e.text}\n\n```csharp\n{e.code}\n```"
    if e.text.startswith(e.title):
        return e.text
    return f"**{e.title}**\n\n{e.text}"


@dataclass
class Reply:
    mode: str  # "claude" or "offline"
    sources: list[Entry]
    chunks: Iterator[str]


def relevant(results: list[tuple[Entry, float]]) -> list[Entry]:
    if not results:
        return []
    top = max(score for _, score in results)
    return [e for e, score in results if score >= top * RELEVANCE_CUTOFF]


def gather_context(kb: KnowledgeBase, message: str) -> list[Entry]:
    # Knowledge packs (API references) are only searched for plugin/code questions,
    # so gameplay questions get gameplay answers.
    query = kb.analyze(message)
    coding = wants_code(message, query.tokens)
    facts = relevant(kb.search(query, limit=6, kinds=("fact", "qa"), packs=coding))
    code = relevant(kb.search(query, limit=6, kinds=("code",))) if coding else []
    if coding and not code:
        # A plugin request with no keyword match still benefits from seeing how plugins look.
        code = [e for e in kb.all_entries() if e.kind == "code" and not e.pack][:2]
    return facts + code


def format_knowledge(entries: list[Entry]) -> str:
    if not entries:
        return "<knowledge>\n(no matching entries)\n</knowledge>"
    parts = []
    for e in entries:
        body = e.text
        if e.code:
            body += f"\n```csharp\n{e.code}\n```"
        framework = f' framework="{e.framework}"' if e.framework else ""
        pack = f' pack="{e.pack}"' if e.pack else ""
        parts.append(f'<entry id="{e.id}" kind="{e.kind}" source="{e.source}"{framework}{pack}>\n'
                     f"{e.title}\n{body}\n</entry>")
    return "<knowledge>\n" + "\n".join(parts) + "\n</knowledge>"


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class Brain:
    def __init__(self, kb: KnowledgeBase, use_claude: bool | None = None):
        self.kb = kb
        self.key_available = claude_available() if use_claude is None else use_claude
        self._client = None

    @property
    def offline_chosen(self) -> bool:
        """You can choose offline mode even with an API key (the switch on the page, or ARIA_MODE=offline)."""
        return self.kb.settings.get("mode", os.environ.get("ARIA_MODE", "")).lower() == "offline"

    @property
    def use_claude(self) -> bool:
        return self.key_available and not self.offline_chosen

    @property
    def mode(self) -> str:
        return "claude" if self.use_claude else "offline"

    def set_mode(self, mode: str) -> None:
        if mode not in ("claude", "offline"):
            raise ValueError("mode must be 'claude' or 'offline'")
        if mode == "claude" and not self.key_available:
            raise ValueError("Claude mode needs an Anthropic API key. See the README for how to add one.")
        self.kb.set_setting("mode", mode)

    def reply(self, message: str, history: list[dict] | None = None) -> Reply:
        if self.use_claude:
            sources = gather_context(self.kb, message)
            return Reply("claude", sources, self._claude_answer(message, history or [], sources))
        text, used = self._offline_answer(message)
        return Reply("offline", used, iter([text]))

    # ---------- offline mode ----------

    def _offline_answer(self, message: str) -> tuple[str, list[Entry]]:
        """Answers with whatever matches best. Returns the answer and the entries it used."""
        query: Query = self.kb.analyze(message)
        coding = wants_code(message, query.tokens)
        facts = self.kb.search(query, limit=8, kinds=("fact", "qa"), packs=coding)
        codes = self.kb.search(query, limit=8, kinds=("code",)) if coding else []
        asked_for_code = bool(CODE_REQUEST.search(message)) or wants_examples(message) or \
            bool({"exampl", "sampl", "snippet", "templat"} & set(query.tokens))

        if wants_list(message):
            # "show me examples", "what do you know about X": show the best match and list the rest.
            pool = codes if wants_examples(message) and codes else sorted(facts + codes, key=lambda r: r[1], reverse=True)
            if not pool:
                return DONT_KNOW, []
            entries = relevant(pool)
            answer = "Here's what I know about that:\n\n" + render_entry(entries[0])
            if len(entries) > 1:
                answer += "\n\n**Other things I know about this:**\n" + "\n".join(f"- {e.title}" for e in entries[1:])
                answer += "\n\n_Click one below to see it._"
            if entries[0].kind == "code":
                answer += "\n\n" + OFFLINE_CODE_NOTE
            return answer, entries

        # For "make me an example/plugin" show code. Otherwise show whichever matches more of the
        # question (a code example wins ties, unless a reference page scores clearly better).
        if not codes and not facts:
            return DONT_KNOW, []
        use_code = bool(codes)
        if codes and facts and not asked_for_code:
            code_cov, fact_cov = self.kb.coverage(codes[0][0], query), self.kb.coverage(facts[0][0], query)
            use_code = code_cov > fact_cov or code_cov == fact_cov and facts[0][1] <= codes[0][1] * 1.2
        results = codes if use_code else facts
        best, others = results[0][0], relevant(results)[1:4]
        answer = render_entry(best)
        if self.kb.coverage(best, query) < MIN_OFFLINE_COVERAGE:
            answer = "I'm not sure I know exactly that, but here's the closest thing I know:\n\n" + answer
        elif best.kind == "code" and asked_for_code:
            answer = "Here's an example I know:\n\n" + answer
        if others:
            answer += "\n\n_Related: " + " · ".join(e.title for e in others) + "_"
        if best.kind == "code":
            answer += "\n\n" + OFFLINE_CODE_NOTE
        return answer, [best, *others]

    # ---------- Claude mode ----------

    def _claude_answer(self, message: str, history: list[dict], sources: list[Entry]) -> Iterator[str]:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in history[-HISTORY_TURNS:]
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        # The API expects the conversation to start with the user and alternate roles.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        cleaned: list[dict] = []
        for m in messages:
            if cleaned and cleaned[-1]["role"] == m["role"]:
                cleaned[-1]["content"] += "\n\n" + m["content"]
            else:
                cleaned.append(m)
        if cleaned and cleaned[-1]["role"] == "user":
            cleaned.pop()
        cleaned.append({"role": "user", "content": f"{format_knowledge(sources)}\n\n{message}"})

        try:
            with self._client.beta.messages.stream(
                model=MODEL,
                max_tokens=32000,
                system=SYSTEM_PROMPT,
                messages=cleaned,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                # If a safety check declines the request, let the API retry it on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for text in stream.text_stream:
                    yield text
                final = stream.get_final_message()
            if final.stop_reason == "refusal":
                yield "\n\n_(The model declined to answer this one.)_"
            elif final.stop_reason == "max_tokens":
                yield "\n\n_(The answer was cut off because it got too long. Ask me to continue.)_"
        except anthropic.AuthenticationError:
            yield "⚠️ The Anthropic API key was rejected. Check `ANTHROPIC_API_KEY` and restart the app."
        except anthropic.RateLimitError:
            yield "⚠️ Too many requests right now (rate limited). Wait a minute and try again."
        except anthropic.APIStatusError as e:
            yield f"⚠️ The Claude API returned an error ({e.status_code}): {e.message}"
        except anthropic.APIConnectionError:
            yield "⚠️ Couldn't reach the Claude API. Check your internet connection."
