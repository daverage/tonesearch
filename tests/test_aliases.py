from __future__ import annotations

import base64
import json

import pytest

import app as app_module
from tonesearch import knowledge, mcp_server, overrides

NOTES = "- Rundown: Nolly runs a Darkglass B7K preamp into a Cali76 compressor. (https://x.example)"
PLAN = {"summary": "Clean compressed lows under distorted highs.", "advice": [], "search_queries": ["Darkglass B7K"],
        "gear": [{"kind": "effect", "name": "Darkglass B7K", "role": "drive", "confidence": "confirmed"}],
        "aliases": ["Nolly", "Adam Getgood", "Periphery", "warm and punchy"]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    overrides.activate({})


@pytest.fixture
def calls(monkeypatch):
    counts = {"web": 0, "plan": 0}
    monkeypatch.setattr(app_module, "web_notes", lambda p: counts.__setitem__("web", counts["web"] + 1) or NOTES)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: counts.__setitem__("plan", counts["plan"] + 1) or json.loads(json.dumps(PLAN)))
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [{"id": 1, "title": "B7K", "match_score": 9}])
    monkeypatch.setattr(app_module.ai, "rank_packs", lambda *a, **k: {1: {"fit": 90, "why": "same preamp"}})
    return counts


def _search(prompt, **extra):
    return app_module.app.test_client().post("/api/search", json={"prompt": prompt, "use_research": True, **extra}).get_json()


def test_match_score_rules():
    words = " ".join(knowledge.topic_words("Periphery bass"))
    aliases = "nolly, adam getgood, periphery"
    assert knowledge.match_score("Adam Getgood bass", words, aliases) > 0
    assert knowledge.match_score("Nolly Getgood bass", words, aliases) == 0  # never pooled from two aliases
    assert knowledge.match_score("Periphery", words, aliases) == 0  # one word is not enough
    assert knowledge.match_score("Adam Getgood bass Juggernaut", words, aliases) == 0  # more specific than saved
    assert knowledge.match_score("Nolly bass warm", words, aliases, same_sound=True) == 0  # a sound it never described
    assert knowledge.match_score("Nolly bass warm", words, aliases) > 0  # but the research still applies


def test_aliases_are_cleaned_of_descriptions():
    assert knowledge.clean_aliases(["Nolly", "Adam Getgood", "warm and punchy", "Nolly"]) == ["nolly", "adam getgood"]


def test_a_search_under_another_name_reuses_the_saved_answer_and_research(calls):
    first = _search("Periphery bass")
    assert knowledge.get(app_module._library(), first["library"]["id"])["aliases"] == "nolly, adam getgood, periphery"
    again = _search("Adam Getgood bass")
    assert again["cached"] and calls == {"web": 1, "plan": 1}
    other_sound = _search("Adam Getgood bass warm")
    assert not other_sound["cached"] and calls == {"web": 1, "plan": 2}  # new brief, same research
    assert other_sound["library"]["reused"]
    assert not _search("Adam Getgood bass", filters={"gears": ["pedal"]})["cached"]  # other filters


def test_mcp_finds_the_saved_answer_by_alias(calls):
    _search("Periphery bass")
    saved = mcp_server.web_research("Adam Getgood bass", notes=lambda text: pytest.fail("should reuse"))
    assert saved.startswith("TONE Search's saved answer")


def test_mcp_save_gear_fills_empty_aliases():
    mcp_server.web_research("Periphery bass", notes=lambda text: NOTES)
    mcp_server.save_gear("Periphery bass", [{"kind": "effect", "name": "Darkglass B7K", "confidence": "confirmed"}],
                         aliases=["Nolly", "Adam Getgood"])
    assert knowledge.find(app_module._library(), "Adam Getgood bass")["notes"] == NOTES


def test_admin_edits_aliases(monkeypatch):
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    entry_id = knowledge.save(app_module._library(), "Porcupine Tree In Absentia", NOTES)
    auth = {"Authorization": "Basic " + base64.b64encode(b"o:pw").decode(), "Origin": "http://localhost"}
    form = {"topic": "Porcupine Tree In Absentia", "notes": NOTES, "gear": "", "action": "save", "status": "new",
            "aliases": "Steven Wilson, In Absentia, 2002"}
    app_module.app.test_client().post(f"/admin/entries/{entry_id}", data=form, headers=auth)
    assert knowledge.get(app_module._library(), entry_id)["aliases"] == "steven wilson, absentia, 2002"  # "in" is filler
    assert knowledge.find(app_module._library(), "Steven Wilson In Absentia")["id"] == entry_id


def test_a_plan_saved_by_older_rules_is_worked_out_again_and_fills_the_aliases(calls):
    from tonesearch import cache
    db = app_module._library()
    entry_id = knowledge.save(db, "Periphery bass", NOTES)  # research saved without aliases, as the old rules did
    words = " ".join(knowledge.topic_words("Periphery bass"))
    cache.put(db, f"plan:{words}:True", {**PLAN, "aliases": []}, words)  # the old rules' plan, under the old key
    _search("Periphery bass")
    assert calls["plan"] == 1  # not reused
    assert knowledge.get(db, entry_id)["aliases"] == "nolly, adam getgood, periphery"


def _age_saved(prefix: str, days: float):
    import sqlite3, time
    with sqlite3.connect(app_module._library()) as db:
        db.execute("UPDATE cache SET created = ? WHERE key LIKE ?", (time.time() - days * 86400, f"{prefix}%"))


def test_saved_answers_last_a_month_not_a_day(calls):
    _search("Periphery bass")
    _age_saved("result:", 3)
    again = _search("Periphery bass")
    assert again["cached"] and calls["plan"] == 1  # older than the 24-hour catalogue cache, still instant
    _age_saved("result:", 31)
    assert not _search("Periphery bass")["cached"]


def test_the_same_rig_asked_another_way_reuses_the_plan_with_the_research(calls):
    _search("Periphery bass")
    other = _search("Adam Getgood bass", filters={"gears": ["pedal"]})  # other filters: no saved answer
    assert not other["cached"] and other["library"]["reused"]
    assert calls == {"web": 1, "plan": 1}  # research and plan both reused; only TONE3000 and ranking ran
    _search("Adam Getgood bass warm")  # a sound the saved request didn't describe: a new plan
    assert calls["plan"] == 2


def test_approved_research_sends_its_checked_gear_to_the_ai(monkeypatch, calls):
    seen = []
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda prompt, research_notes="", **k: seen.append(research_notes) or json.loads(json.dumps(PLAN)))
    entry_id = knowledge.save(app_module._library(), "Periphery bass", NOTES,
                              [{"kind": "amp", "name": "Darkglass B7K", "role": "his preamp", "confidence": "best"}])
    _search("Periphery bass", fresh=True)
    assert seen[-1] == NOTES  # unreviewed research: the notes only
    knowledge.update(app_module._library(), entry_id, topic="Periphery bass", notes=NOTES, status="approved",
                     gear=[{"kind": "effect", "name": "Darkglass B7K", "role": "his preamp", "confidence": "best"}],
                     aliases=[])
    _search("Periphery bass", fresh=True)
    first, rest = seen[-1].split("\n", 1)
    assert first.startswith("- Gear checked by the site owner") and "Darkglass B7K (effect, best: his preamp)" in first
    assert rest == NOTES


def _rate(answer, vote, visitor):
    return app_module.app.test_client().post("/api/feedback", headers={"CF-Connecting-IP": visitor}, json={
        "topic": answer["topic"], "target": "brief", "vote": vote,
        "brief_id": answer["brief_id"], "brief_words": answer["plan"].get("words", "")})


def test_votes_from_every_wording_count_for_the_same_brief(calls):
    first = _search("Periphery bass")
    _rate(first, 1, "203.0.113.1")
    other = _search("Adam Getgood bass", filters={"gears": ["pedal"]})  # the same brief, reused through the research
    assert other["brief_id"] == first["brief_id"] and other["rated_good"] == 1
    _rate(other, 1, "203.0.113.2")
    _rate(other, 1, "203.0.113.1")  # the first voter again, under another wording: still one vote
    assert app_module.feedback.brief_votes(app_module._library(), first["brief_id"]) == (2, 0)


def test_a_vote_for_a_brief_that_isnt_saved_counts_for_nothing(calls):
    first = _search("Periphery bass")
    _rate({**first, "brief_id": "0" * 20}, 1, "203.0.113.9")
    assert app_module.feedback.brief_votes(app_module._library(), "0" * 20) == (0, 0)
    assert app_module.feedback.brief_votes(app_module._library(), first["brief_id"]) == (0, 0)


def test_a_bad_rating_clears_the_brief_where_it_was_saved(calls):
    _search("Periphery bass")
    other = _search("Adam Getgood bass", filters={"gears": ["pedal"]})
    assert calls["plan"] == 1
    _rate(other, -1, "203.0.113.3")
    _search("Adam Getgood bass", filters={"gears": ["pedal"]})
    assert calls["plan"] == 2  # the brief saved under "Periphery bass" was cleared, so it's worked out again


def test_an_alias_stands_in_for_part_of_the_topic_never_all_of_it():
    words = " ".join(knowledge.topic_words("guitar metallica's black album"))
    aliases = "james hetfield, 1991, metal thrash"
    assert words == "black metallica"  # the possessive is trimmed
    assert knowledge.match_score("metallica 1991", words, aliases) > 0
    assert knowledge.match_score("James Hetfield Black Album", words, aliases) > 0
    assert knowledge.match_score("James Hetfield", words, aliases) == 0  # his whole career isn't this album
    assert knowledge.match_score("thrash metal", words, aliases) == 0  # a genre the notes mention isn't this rig
    assert knowledge.match_score("metallica black album", "black metallica's", "") == 1.0  # words saved before
    assert knowledge.clean_aliases(["James Hetfield"]) == ["james hetfield"]  # kept in the order written


def test_single_words_already_in_the_request_are_not_kept_as_aliases():
    from tonesearch import ai
    kept = ai.filter_aliases("guitar metallica's black album", ["Metallica", "Black", "1991", "James Hetfield"],
                             evidence="James Hetfield recorded it in 1991")
    assert kept == ["1991", "James Hetfield"]


def test_older_feedback_tables_gain_the_brief_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as old:
        old.execute("""CREATE TABLE feedback (id INTEGER PRIMARY KEY, created REAL NOT NULL, voter TEXT NOT NULL,
            words TEXT NOT NULL, prompt TEXT NOT NULL, target TEXT NOT NULL, pack_id INTEGER NOT NULL DEFAULT 0,
            pack_title TEXT NOT NULL DEFAULT '', vote INTEGER NOT NULL, comment TEXT NOT NULL DEFAULT '',
            entry_id INTEGER, source TEXT NOT NULL DEFAULT 'web', UNIQUE (voter, words, target, pack_id))""")
        old.execute("INSERT INTO feedback (created, voter, words, prompt, target, vote) VALUES (1, 'v', 'w', 'w', 'brief', 1)")
    app_module.feedback.record(db, voter_id="v2", words="w", prompt="w", target="brief", vote=1, brief="b" * 20)
    assert app_module.feedback.brief_votes(db, "b" * 20) == (1, 0)
