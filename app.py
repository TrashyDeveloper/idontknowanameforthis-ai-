"""Web app for Aria. Run it with:  python app.py  and open http://127.0.0.1:5000"""

from __future__ import annotations

import itertools
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from aria.brain import Brain
from aria.knowledge import KnowledgeBase

ROOT = Path(__file__).parent
REMEMBER = re.compile(r"^\s*(?:remember|learn|note)\s*(?:that)?\s*[:,]?\s+(.+)$", re.IGNORECASE | re.DOTALL)


def create_app(seed_path: Path | None = None, learned_path: Path | None = None,
               use_claude: bool | None = None) -> Flask:
    app = Flask(__name__)
    kb = KnowledgeBase(seed_path or ROOT / "data" / "seed_knowledge.json",
                       learned_path or ROOT / "data" / "learned.json")
    brain = Brain(kb, use_claude=use_claude)

    def entry_json(e):
        return {k: v for k, v in asdict(e).items() if k != "created"}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(mode=brain.mode, **kb.stats())

    @app.post("/api/chat")
    def chat():
        body = request.get_json(force=True)
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify(error="message is empty"), 400

        # "remember: ..." in the chat teaches a fact directly.
        match = REMEMBER.match(message)
        if match:
            fact = match.group(1).strip()
            entry = kb.add("fact", fact[:80], fact)
            return Response(_events([{"type": "meta", "mode": brain.mode, "sources": [], "learned": entry.id},
                                     {"type": "text", "text": f"Got it! I'll remember: _{fact}_"}]),
                            mimetype="application/x-ndjson")

        reply = brain.reply(message, body.get("history") or [])
        meta = {"type": "meta", "mode": reply.mode,
                "sources": [{"id": e.id, "title": e.title, "kind": e.kind, "source": e.source}
                            for e in reply.sources]}
        text_events = ({"type": "text", "text": chunk} for chunk in reply.chunks)
        return Response(_events(itertools.chain([meta], text_events)), mimetype="application/x-ndjson")

    @app.post("/api/feedback")
    def feedback():
        body = request.get_json(force=True)
        ids = [s for s in body.get("sources", []) if isinstance(s, str)]
        helpful = bool(body.get("helpful"))
        kb.feedback(ids, helpful)
        learned = None
        correction = (body.get("correction") or "").strip()
        question = (body.get("question") or "").strip()
        if not helpful and correction and question:
            learned = entry_json(kb.add("qa", question, correction))
        return jsonify(ok=True, learned=learned)

    @app.get("/api/knowledge")
    def list_knowledge():
        query = request.args.get("q", "").strip()
        if query:
            entries = [e for e, _ in kb.search(query, limit=50)]
        else:
            entries = sorted(kb.all_entries(), key=lambda e: (e.source != "user", e.kind, e.title.lower()))
        return jsonify(entries=[entry_json(e) for e in entries], **kb.stats())

    @app.post("/api/knowledge")
    def add_knowledge():
        body = request.get_json(force=True)
        try:
            entry = kb.add(kind=body.get("kind", "fact"), title=body.get("title", ""),
                           text=body.get("text", ""), code=body.get("code", ""),
                           framework=body.get("framework", ""), tags=body.get("tags", []))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(entry=entry_json(entry)), 201

    @app.delete("/api/knowledge/<entry_id>")
    def delete_knowledge(entry_id):
        if not kb.remove(entry_id):
            return jsonify(error="not found"), 404
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
    app = create_app()
    print("Aria is running. Open http://127.0.0.1:%s in your browser." % os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
