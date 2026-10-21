import json
from pathlib import Path

import pytest

from app import create_app
from aria.brain import Brain, format_knowledge, gather_context
from aria.language import stem, tokenize, wants_code
from aria.knowledge import KnowledgeBase

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
    meta, text = chat(client, question)
    assert "don't know" in text or "not sure" in text


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


def test_unsure_answer_lists_only_what_it_showed(client):
    meta, text = chat(client, "which is my favorite scp?")
    assert "not sure" in text
    assert meta["sources"] and meta["sources"][0]["title"] in text


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


def test_list_request_shows_what_aria_knows(client):
    meta, text = chat(client, "show me examples that you know about the new exiled 9.15.2")
    assert text.startswith("Here's what I know about that")
    assert "Other things I know about this" in text and "```csharp" in text
    assert all(s["kind"] == "code" for s in meta["sources"])


def test_unsure_answers_show_the_closest_thing(client):
    _, text = chat(client, "is the micro hid good against 173")
    assert "don't know" not in text


def test_nothing_known_says_so(client):
    meta, text = chat(client, "xyzzy plugh")
    assert "don't know" in text and meta["sources"] == []


# ---------- understanding casual language ----------

@pytest.mark.parametrize("question, expected", [
    ("i need you aria to do a example about exiled custom roles", "seed-code-exiled-custom-role"),
    ("yo can u make me a custum item exmple in exild", "seed-code-exiled-custom-item"),
    ("how do i make a custom ability", "seed-code-exiled-custom-ability"),
    ("hey aria wats up with the larry guy", "seed-106"),
    ("whats the best gun", "seed-weapons"),
    ("how does exiled patch the hurting event", "exiled-patch-Events/Player/Hurting.cs"),
])
def test_understands_casual_sentences(kb, question, expected):
    brain = Brain(kb, use_claude=False)
    assert brain.reply(question).sources[0].id == expected


def test_filler_words_are_ignored():
    assert tokenize("i need you aria to do a example about exiled custom roles") == \
        tokenize("example exiled custom roles")


def test_typos_are_corrected(kb):
    groups = kb.analyze("exild custum exmple").groups
    assert [next(iter(g)) for g in groups] == ["exil", "custom", "exampl"]


def test_synonyms_are_searched(kb):
    group = kb.analyze("damage").groups[0]
    assert group["damag"] == 1.0 and group["hurt"] == 0.5


# ---------- switching modes ----------

def test_switch_to_offline_and_back(tmp_path):
    learned = tmp_path / "learned.json"
    client = create_app(SEED, learned, use_claude=True).test_client()
    assert client.get("/api/status").get_json()["mode"] == "claude"
    assert client.post("/api/mode", json={"mode": "offline"}).get_json()["mode"] == "offline"
    meta, _ = chat(client, "How do I survive SCP-173?")
    assert meta["mode"] == "offline"
    # The choice is remembered after a restart.
    restarted = create_app(SEED, learned, use_claude=True).test_client()
    assert restarted.get("/api/status").get_json()["mode"] == "offline"
    assert restarted.post("/api/mode", json={"mode": "claude"}).get_json()["mode"] == "claude"


def test_claude_mode_needs_a_key(client):
    res = client.post("/api/mode", json={"mode": "claude"})
    assert res.status_code == 400 and "API key" in res.get_json()["error"]
    assert client.post("/api/mode", json={"mode": "banana"}).status_code == 400


# ---------- self-learning ----------

def test_thumbs_up_links_the_users_words_to_the_answer(tmp_path):
    learned = tmp_path / "learned.json"
    kb = KnowledgeBase(SEED, learned)
    question = "zorblax spawn thing"
    kb.feedback(["seed-code-exiled-spawn-item"], helpful=True, question=question)
    assert kb.search(question, packs=False)[0][0].id == "seed-code-exiled-spawn-item"
    # ...and it's remembered after a restart.
    assert KnowledgeBase(SEED, learned).search(question, packs=False)[0][0].id == "seed-code-exiled-spawn-item"


def test_teaching_a_word_in_chat(client):
    _, text = chat(client, "glorp means warhead")
    assert "glorp" in text and "warhead" in text
    meta, _ = chat(client, "how does the glorp work")
    assert meta["sources"][0]["id"] == "seed-warhead"
    words = client.get("/api/knowledge").get_json()["word_list"]
    assert words == {"glorp": ["warhead"]}
    assert client.delete("/api/words/glorp").status_code == 200


def test_teaching_a_word_with_the_form(client):
    assert client.post("/api/words", json={"word": "tp", "meaning": "teleport"}).status_code == 201
    assert client.post("/api/words", json={"word": "", "meaning": "x"}).status_code == 400


def test_forget_removes_something_taught(client):
    chat(client, "my favorite scp is SCP-106")
    _, text = chat(client, "forget that my favorite scp is 106")
    assert "forgot" in text
    _, text = chat(client, "forget that i like pizza")
    assert "couldn't find" in text


@pytest.mark.parametrize("message", ["my plugin is not working", "my server has an error", "learn about exiled"])
def test_help_requests_are_not_saved_as_facts(client, message):
    _, text = chat(client, message)
    assert "I'll remember" not in text


def test_teaching_the_same_fact_twice_keeps_one(kb):
    a = kb.add("fact", "x", "the same fact")
    b = kb.add("fact", "x", "the same fact")
    assert a.id == b.id


def test_hidden_entries_can_be_restored(client):
    client.delete("/api/knowledge/seed-173")
    hidden = client.get("/api/knowledge?hidden=1").get_json()["entries"]
    assert [e["id"] for e in hidden] == ["seed-173"]
    assert client.post("/api/knowledge/seed-173/restore").status_code == 200
    assert client.get("/api/knowledge?hidden=1").get_json()["entries"] == []


def test_damaged_learned_file_is_backed_up(tmp_path):
    learned = tmp_path / "learned.json"
    learned.write_text("{ this is not json", encoding="utf-8")
    kb = KnowledgeBase(SEED, learned)
    assert kb.stats()["learned"] == 0
    assert list(tmp_path.glob("learned.broken-*.json"))


def test_bad_requests_dont_crash(client):
    assert client.post("/api/chat", data="not json").status_code == 400
    assert client.post("/api/chat", json=["a list"]).status_code == 400
    assert client.post("/api/feedback", json={"sources": "oops"}).status_code == 200
    res = client.post("/api/knowledge", json={"kind": "fact", "title": "t", "text": "x", "tags": "a, b"})
    assert res.get_json()["entry"]["tags"] == ["a", "b"]


def test_exiled_pack_includes_custom_roles_items_sources_and_patches(kb):
    ids = {e.id for e in kb.all_entries()}
    assert any(i.startswith("exiled-api-Exiled.CustomRoles.API.Features.CustomRole") for i in ids)
    assert any(i.startswith("exiled-api-Exiled.CustomItems.API.Features.CustomItem") for i in ids)
    assert "exiled-source-Exiled.CustomRoles/Commands/Give.cs" in ids
    assert "exiled-patch-Events/Player/Hurting.cs" in ids
    assert "PlayerStats.DealDamage" in kb.get("exiled-event-Player.Hurting").text
