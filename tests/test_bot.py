import json
from pathlib import Path

import pytest

from app import create_app
from aria.brain import Brain, format_knowledge, gather_context, wants_code
from aria.knowledge import KnowledgeBase, stem, tokenize

SEED = Path(__file__).parent.parent / "data" / "seed_knowledge.json"


@pytest.fixture
def kb(tmp_path):
    return KnowledgeBase(SEED, tmp_path / "learned.json")


@pytest.fixture
def client(tmp_path):
    app = create_app(SEED, tmp_path / "learned.json", use_claude=False)
    return app.test_client()


def chat(client, message, history=None):
    res = client.post("/api/chat", json={"message": message, "history": history or []})
    events = [json.loads(line) for line in res.get_data(as_text=True).splitlines() if line]
    return events[0], "".join(e["text"] for e in events if e["type"] == "text")


def test_tokenize_understands_scp_names_and_nicknames():
    assert "173" in tokenize("How do I beat SCP-173?")
    assert "173" in tokenize("the peanut keeps killing me")
    assert "0492" in tokenize("what is scp 049-2")
    assert "classd" in tokenize("Class-D tips")
    assert "096" in tokenize("shy guy")


@pytest.mark.parametrize("question, expected", [
    ("How do I survive SCP-173?", "seed-173"),
    ("what does the shy guy do", "seed-096"),
    ("what happens when a class-d escapes", "seed-escape"),
    ("how does the nuke work", "seed-warhead"),
    ("how do I beat 079", "seed-079"),
])
def test_search_finds_the_right_starter_entry(kb, question, expected):
    assert kb.search(question, packs=False)[0][0].id == expected


def test_learned_facts_persist_and_rank_first(tmp_path):
    learned = tmp_path / "learned.json"
    kb = KnowledgeBase(SEED, learned)
    entry = kb.add("fact", "SCP-173 on our server", "On our server SCP-173 has 5000 HP.", tags=["173"])
    reloaded = KnowledgeBase(SEED, learned)
    assert reloaded.get(entry.id).text == "On our server SCP-173 has 5000 HP."
    assert reloaded.search("SCP-173 server HP")[0][0].id == entry.id


def test_feedback_changes_trust_and_is_saved(tmp_path):
    learned = tmp_path / "learned.json"
    kb = KnowledgeBase(SEED, learned)
    kb.feedback(["seed-173"], helpful=False)
    assert kb.get("seed-173").weight == pytest.approx(0.7)
    assert KnowledgeBase(SEED, learned).get("seed-173").weight == pytest.approx(0.7)
    # The starter file itself is never modified.
    assert "weight" not in next(e for e in json.loads(SEED.read_text()) if e["id"] == "seed-173")


def test_hide_seed_and_delete_learned(kb):
    assert kb.remove("seed-173")
    assert all(e.id != "seed-173" for e, _ in kb.search("SCP-173"))
    entry = kb.add("fact", "temp", "temporary fact")
    assert kb.remove(entry.id)
    assert kb.get(entry.id) is None


def test_add_rejects_empty_entries(kb):
    with pytest.raises(ValueError):
        kb.add("fact", "", "")
    with pytest.raises(ValueError):
        kb.add("spell", "x", "y")


def test_code_requests_include_code_examples(kb):
    assert wants_code("write a LabAPI plugin that welcomes players")
    assert not wants_code("how do I survive scp-096")
    assert not wants_code("what happens when a class-d escapes")
    from aria.brain import gather_context
    context = gather_context(kb, "write a LabAPI plugin that welcomes players when they join")
    assert any(e.id == "seed-code-labapi-welcome" for e in context)
    text = format_knowledge(context)
    assert "LabApi.Loader.Features.Plugins" in text and 'framework="LabAPI"' in text


def test_offline_chat_answers_from_knowledge(client):
    meta, text = chat(client, "How do I survive SCP-173?")
    assert meta["mode"] == "offline"
    assert "SCP-173" in text
    assert any(s["id"] == "seed-173" for s in meta["sources"])


@pytest.mark.parametrize("question", ["who are you?", "what's your name", "what can you do"])
def test_aria_introduces_itself(client, question):
    _, text = chat(client, question)
    assert "I'm Aria" in text


def test_offline_chat_shows_code_example(client):
    _, text = chat(client, "make an EXILED plugin with a config for a welcome message")
    assert "```csharp" in text and "Plugin<Config>" in text


@pytest.mark.parametrize("question", ["what's the weather in Paris", "who is the owner of our community?"])
def test_offline_chat_admits_when_it_does_not_know(client, question):
    _, text = chat(client, question)
    assert "don't know" in text


def test_correction_teaches_the_bot(client):
    question = "Who is the best SCP:SL streamer?"
    meta, _ = chat(client, question)
    res = client.post("/api/feedback", json={"sources": [s["id"] for s in meta["sources"]], "helpful": False,
                                             "question": question, "correction": "It's TrashyDeveloper, obviously."})
    assert res.get_json()["learned"]["kind"] == "qa"
    _, text = chat(client, "best streamer for SCP:SL?")
    assert "TrashyDeveloper" in text


def test_remember_command_in_chat(client):
    _, text = chat(client, "remember: our server restarts every day at 6pm")
    assert "remember" in text.lower()
    _, text = chat(client, "when does our server restart?")
    assert "6pm" in text


def test_add_code_example_via_api(client):
    res = client.post("/api/knowledge", json={"kind": "code", "title": "LabAPI: tesla gate kills", "framework": "LabAPI",
                                              "text": "Example of the tesla event", "code": "public class Tesla {}"})
    assert res.status_code == 201
    listing = client.get("/api/knowledge?q=tesla").get_json()
    assert listing["entries"][0]["title"] == "LabAPI: tesla gate kills"


def test_claude_mode_builds_valid_conversation(kb, monkeypatch):
    """Claude mode should send an alternating conversation that ends with the knowledge + question."""
    sent = {}

    class FakeStream:
        text_stream = iter(["Hello", " there"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return type("M", (), {"stop_reason": "end_turn"})()

    class FakeClient:
        class beta:
            class messages:
                @staticmethod
                def stream(**kwargs):
                    sent.update(kwargs)
                    return FakeStream()

    brain = Brain(kb, use_claude=True)
    brain._client = FakeClient()
    history = [{"role": "assistant", "content": "intro"}, {"role": "user", "content": "hi"},
               {"role": "assistant", "content": "hello!"}]
    reply = brain.reply("How do I survive SCP-173?", history)
    assert "".join(reply.chunks) == "Hello there"
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert sent["messages"][-1]["content"].startswith("<knowledge>")
    assert "seed-173" in sent["messages"][-1]["content"]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["fallbacks"] == "default"


def test_statements_about_the_user_are_remembered(client):
    _, text = chat(client, "my favorite scp is SCP-106")
    assert "remember" in text.lower()
    meta, text = chat(client, "which is my favorite scp?")
    assert "You told me" in text and "SCP-106" in text
    assert meta["sources"][0]["source"] == "user"


@pytest.mark.parametrize("message", ["my favorite scp is what?", "what is my favorite scp", "SCP-106 is my favorite"])
def test_questions_are_not_mistaken_for_statements(client, message):
    _, text = chat(client, message)
    assert "I'll remember" not in text


def test_unknown_answer_lists_no_sources(client):
    meta, text = chat(client, "which is my favorite scp?")
    assert "don't know" in text
    assert meta["sources"] == []


def test_tokenize_splits_code_names_and_stems():
    tokens = tokenize("ChangingLeverStatus")
    assert {"changingleverstatu", "chang", "lever"} <= set(tokens)
    assert stem("hurting") == stem("hurt") and stem("escaped") == stem("escape")


def test_exiled_pack_is_loaded(kb):
    pack = kb.stats()["packs"][0]
    assert pack["name"].startswith("EXILED") and pack["entries"] > 500
    hurting = kb.get("exiled-event-Player.Hurting")
    assert hurting.pack.startswith("EXILED") and "IsAllowed = false" in hurting.text


@pytest.mark.parametrize("question, expected", [
    ("exiled event when a player gets hurt", "exiled-event-Player.Hurting"),
    ("EXILED plugin that does something when the warhead detonates", "exiled-event-Warhead.Detonated"),
    ("exiled event when a player escapes", "exiled-event-Player.Escaping"),
])
def test_plugin_questions_find_exiled_events(kb, question, expected):
    assert expected in [e.id for e in gather_context(kb, question)]


def test_gameplay_questions_ignore_api_packs(kb):
    assert all(not e.pack for e in gather_context(kb, "what happens when a class-d escapes"))
    assert wants_code("what DamageType does the micro hid use")
