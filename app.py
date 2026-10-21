"""Web app for Aria. Run it with:  python app.py  and open http://127.0.0.1:5000"""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from aria.brain import Brain
from aria.knowledge import KnowledgeBase
from aria.language import tokenize

ROOT = Path(__file__).parent

# "remember: ...", "remember that ...", "note: ..." teach a fact.
REMEMBER = re.compile(r"^\s*(?:remember|note|learn)(?:\s+that\b|\s*:)\s*(.+)$|^\s*remember\s+(.+)$",
                      re.IGNORECASE | re.DOTALL)
# Statements about the user or their server, like "my favorite SCP is 106" or "our server has 30 slots".
STATEMENT = re.compile(r"^\s*(?:my|our)\s+\w[^?]*\b(?:is|are|was|has|have)\b[^?]+$", re.IGNORECASE)
# ...but not when they're asking for help: "my plugin is not working", "my server has an error".
NOT_A_FACT = re.compile(r"\b(help|how|why|what|can you|could you|pls|please|not working|doesn'?t|don'?t|won'?t|"
                        r"isn'?t|error|exception|crash\w*|broken|bug\w*|wrong|issue|problem|fix)\b", re.IGNORECASE)
# Teaching words: "tp means teleport", "when I say tp I mean teleport", "ff = friendly fire".
WORD_MEANING = re.compile(
    r"^\s*(?:remember\s*:?\s*)?(?:when i say\s+)?[\"']?(?P<word>[\w#+.-]+(?:\s[\w#+.-]+)?)[\"']?\s*"
    r"(?:,?\s*i mean|means|is short for|stands for|is the same as|=)\s*[\"']?(?P<meaning>[^\"'?]{1,60}?)[\"']?\s*\.?\s*$",
    re.IGNORECASE,
)
# "forget that my favorite SCP is 106", "forget about tp".
FORGET = re.compile(r"^\s*(?:please\s+)?forget(?:\s+that|\s+about)?\s+(.+)$", re.IGNORECASE)
MAX_STATEMENT_WORDS = 25


def create_app(seed_path: Path | None = None, learned_path: Path | None = None,
               use_claude: bool | None = None) -> Flask:
    app = Flask(__name__)
    kb = KnowledgeBase(seed_path or ROOT / "data" / "seed_knowledge.json",
                       learned_path or ROOT / "data" / "learned.json")
    brain = Brain(kb, use_claude=use_claude)

    def entry_json(e):
        return {k: v for k, v in asdict(e).items() if k != "created"}

    def json_body() -> dict:
        body = request.get_json(force=True, silent=True)
        return body if isinstance(body, dict) else {}

    def say(text: str, learned: str | None = None) -> Response:
        meta = {"type": "meta", "mode": brain.mode, "sources": [], "learned": learned}
        return Response(_events([meta, {"type": "text", "text": text}]), mimetype="application/x-ndjson")

    def learn_from_chat(message: str) -> Response | None:
        """Handles messages that teach or un-teach Aria something. None if it's a normal question."""
        word = WORD_MEANING.match(message)
        if word and len(word.group("word").split()) <= 2 and not NOT_A_FACT.search(word.group("word")):
            try:
                kb.learn_word(word.group("word"), word.group("meaning"))
            except ValueError:
                pass
            else:
                return say(f"Got it! When you say **{word.group('word')}**, I'll know you mean "
                           f"**{word.group('meaning').strip()}**.", learned="word")

        forget = FORGET.match(message)
        if forget:
            hits = [e for e, _ in kb.search(forget.group(1), limit=10) if e.source == "user"]
            if hits and kb.coverage(hits[0], forget.group(1)) >= 0.6:
                kb.remove(hits[0].id)
                return say(f"Okay, I forgot: _{hits[0].text}_", learned="forgot")
            return say("I couldn't find that among the things you taught me. "
                       "You can see and delete them in the **Knowledge** tab.")

        match = REMEMBER.match(message)
        fact = (match.group(1) or match.group(2)).strip() if match else None
        if not fact and STATEMENT.match(message) and not NOT_A_FACT.search(message) \
                and len(message.split()) <= MAX_STATEMENT_WORDS:
            fact = message.strip()
        if fact and tokenize(fact):
            entry = kb.add("fact", fact[:80], fact, tags=["remembered"])
            return say(f"Got it! I'll remember: _{fact}_", learned=entry.id)
        return None

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(mode=brain.mode, key_available=brain.key_available, **kb.stats())

    @app.post("/api/mode")
    def set_mode():
        try:
            brain.set_mode(str(json_body().get("mode") or ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(mode=brain.mode)

    @app.post("/api/chat")
    def chat():
        body = json_body()
        message = str(body.get("message") or "").strip()
        if not message:
            return jsonify(error="message is empty"), 400

        taught = learn_from_chat(message)
        if taught:
            return taught

        history = [m for m in body.get("history") or [] if isinstance(m, dict)]
        reply = brain.reply(message, history)
        meta = {"type": "meta", "mode": reply.mode,
                "sources": [{"id": e.id, "title": e.title, "kind": e.kind, "source": e.source, "pack": e.pack}
                            for e in reply.sources]}
        text_events = ({"type": "text", "text": chunk} for chunk in reply.chunks)
        return Response(_events(itertools.chain([meta], text_events)), mimetype="application/x-ndjson")

    @app.post("/api/feedback")
    def feedback():
        body = json_body()
        ids = [s for s in body.get("sources") or [] if isinstance(s, str)]
        helpful = bool(body.get("helpful"))
        question = str(body.get("question") or "").strip()
        kb.feedback(ids, helpful, question=question)
        learned = None
        correction = str(body.get("correction") or "").strip()
        if not helpful and correction and question:
            learned = entry_json(kb.add("qa", question, correction))
        return jsonify(ok=True, learned=learned)

    @app.get("/api/knowledge")
    def list_knowledge():
        query = request.args.get("q", "").strip()
        if request.args.get("hidden"):
            entries = kb.hidden_entries()
        elif query:
            entries = [e for e, _ in kb.search(query, limit=50)]
        else:
            # Packs hold hundreds of API entries, so they're only listed when searching.
            entries = sorted((e for e in kb.all_entries() if not e.pack),
                             key=lambda e: (e.source != "user", e.kind, e.title.lower()))
        return jsonify(entries=[entry_json(e) for e in entries], word_list=kb.synonyms, **kb.stats())

    @app.post("/api/knowledge")
    def add_knowledge():
        body = json_body()
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        try:
            entry = kb.add(kind=str(body.get("kind") or "fact"), title=str(body.get("title") or ""),
                           text=str(body.get("text") or ""), code=str(body.get("code") or ""),
                           framework=str(body.get("framework") or ""),
                           tags=[str(t) for t in tags if isinstance(t, (str, int))])
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(entry=entry_json(entry)), 201

    @app.delete("/api/knowledge/<entry_id>")
    def delete_knowledge(entry_id):
        if not kb.remove(entry_id):
            return jsonify(error="not found"), 404
        return jsonify(ok=True)

    @app.post("/api/knowledge/<entry_id>/restore")
    def restore_knowledge(entry_id):
        if not kb.restore(entry_id):
            return jsonify(error="not hidden"), 404
        return jsonify(ok=True)

    @app.post("/api/words")
    def add_word():
        body = json_body()
        word, meaning = str(body.get("word") or "").strip(), str(body.get("meaning") or "").strip()
        if not word or not meaning:
            return jsonify(error="a word and its meaning are both needed"), 400
        try:
            kb.learn_word(word, meaning)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True), 201

    @app.delete("/api/words/<word>")
    def forget_word(word):
        if word not in kb.synonyms:
            return jsonify(error="not found"), 404
        kb.forget_word(word)
        return jsonify(ok=True)

    @app.get("/api/export")
    def export():
        data = kb.learned_path.read_text(encoding="utf-8") if kb.learned_path.exists() else "{}"
        return Response(data, mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=aria-learned.json"})

    return app


def _events(events):
    """Stream one JSON object per line so the page can show the answer as it's written."""
    for event in events:
        yield json.dumps(event) + "\n"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    url = f"http://127.0.0.1:{port}"
    app = create_app()
    print(f"Aria is running. Open {url} in your browser. Press Ctrl+C to stop.")
    if not os.environ.get("ARIA_NO_BROWSER"):
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    except OSError:
        print(f"Port {port} is already in use. Is Aria already running in another window?")
