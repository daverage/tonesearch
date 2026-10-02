from __future__ import annotations

import base64
import json
import sqlite3

import pytest

import app as app_module
from tonesearch import knowledge, overrides

NOTES = ("- Rig rundown: Nolly runs a Darkglass B7K preamp into a Cali76 compressor. (https://rigs.example/nolly)\n"
         "- Gear list: Search snippet only: Nolly played a Dingwall NG-2 through the album sessions. (https://list.example/x)")
PLAN = {"summary": "Clean compressed lows under distorted highs.", "advice": [], "search_queries": ["Darkglass B7K"],
        "gear": [{"kind": "effect", "name": "Darkglass B7K", "role": "drive", "confidence": "best"}],
        "aliases": ["Nolly", "Adam Getgood", "Periphery"]}
AUTH = {"Authorization": "Basic " + base64.b64encode(b"o:pw").decode(), "Origin": "http://localhost"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    overrides.activate({})
    monkeypatch.setattr(app_module, "web_notes", lambda prompt: NOTES)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: json.loads(json.dumps(PLAN)))
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [{"id": 1, "title": "B7K", "match_score": 9}])
    monkeypatch.setattr(app_module.ai, "rank_packs", lambda *a, **k: {1: {"fit": 90, "why": "same preamp"}})


def _search(client, prompt):
    return client.post("/api/search", json={"prompt": prompt, "use_research": True}).get_json()


def _vote(client, topic, **extra):
    body = {"topic": topic, "target": "pack", "vote": 1, "pack_id": 1, "pack_title": "B7K", **extra}
    assert client.post("/api/feedback", json=body).status_code == 200


def _rows(table, where=""):
    with sqlite3.connect(app_module._library()) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def test_deleting_research_deletes_its_answers_and_votes_under_every_name():
    client = app_module.app.test_client()
    first = _search(client, "Periphery bass")
    entry_id = first["library"]["id"]
    other_name = _search(client, "Adam Getgood bass warm")  # reuses the research under an alias, saved under its own words
    assert other_name["library"]["id"] == entry_id and not other_name["cached"]
    _vote(client, "Periphery bass")
    _vote(client, "Adam Getgood bass warm", target="brief", pack_id=0, vote=1, library_id=entry_id)
    _search(client, "Porcupine Tree In Absentia")  # unrelated research, answers and votes stay
    _vote(client, "Porcupine Tree In Absentia")
    assert _rows("feedback") == 3

    page = client.get(f"/admin?q=%23{entry_id}", headers=AUTH).get_data(as_text=True)
    assert "2 saved answers" in page and "2 votes (2 good, 0 bad)" in page and "adam bass getgood warm" in page

    done = client.post(f"/admin/entries/{entry_id}", data={"action": "delete"}, headers=AUTH)
    message = done.headers["Location"]
    assert "Deleted+research" in message and "2+votes" in message
    assert knowledge.get(app_module._library(), entry_id) is None
    assert _rows("feedback") == 1 and _rows("feedback", "WHERE words LIKE '%absentia%'") == 1
    assert _rows("cache", "WHERE words IN ('bass periphery', 'adam bass getgood warm')") == 0
    assert _rows("cache", "WHERE words LIKE '%absentia%'") > 0
    assert not _search(client, "Periphery bass")["cached"]  # worked out again from scratch


def test_approve_from_the_list_keeps_everything_else():
    client = app_module.app.test_client()
    entry_id = _search(client, "Periphery bass")["library"]["id"]
    before = knowledge.get(app_module._library(), entry_id)
    client.post(f"/admin/entries/{entry_id}", data={"action": "approve_only"}, headers=AUTH)
    after = knowledge.get(app_module._library(), entry_id)
    assert after["status"] == "approved"
    assert (after["notes"], after["gear"], after["aliases"]) == (before["notes"], before["gear"], before["aliases"])


def test_a_single_vote_can_be_deleted():
    client = app_module.app.test_client()
    _search(client, "Periphery bass")
    _vote(client, "Periphery bass")
    with sqlite3.connect(app_module._library()) as connection:
        vote_id = connection.execute("SELECT id FROM feedback").fetchone()[0]
    page = client.get("/admin?view=feedback", headers=AUTH).get_data(as_text=True)
    assert f'action="admin/votes/{vote_id}/delete"' in page
    client.post(f"/admin/votes/{vote_id}/delete", headers=AUTH)
    assert _rows("feedback") == 0
    assert client.post(f"/admin/votes/{vote_id}/delete", headers={"Authorization": AUTH["Authorization"],
                                                                   "Origin": "https://evil.example"}).status_code == 403


def test_the_library_page_shows_sources_as_links_and_marks_snippets():
    client = app_module.app.test_client()
    _search(client, "Periphery bass")
    page = client.get("/admin", headers=AUTH).get_data(as_text=True)
    assert '<a href="https://rigs.example/nolly"' in page and "Snippet only" in page
    assert "Delete research, answers and votes" in page


def test_suggested_aliases_follow_the_same_rules_as_new_searches(monkeypatch):
    from tonesearch import ai
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("custom", "https://x/v1", "m", "k"))
    reply = {"aliases": ["Muse", "Matt Bellamy", "Origin of Symmetry", "Diezel VH4", "fuzz"]}

    class _Reply:
        def __init__(self):
            self.payload = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()

        def read(self, _n=None):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    notes = "- Rig: Matt Bellamy of Muse tracked Knights of Cydonia through a Diezel VH4. (https://rig.example)"
    gear = [{"kind": "amp", "name": "Diezel VH4", "role": "main amp", "confidence": "best"}]
    assert ai.suggest_aliases("Knights of Cydonia", notes, gear, opener=lambda req, timeout: _Reply()) == [
        "Muse", "Matt Bellamy"]  # not an album the research never mentions, gear or a category
    reply.update(aliases=["Beatles"], keywords=["gigging stereo combo", "Beatles AC30 combo"])
    assert ai.suggest_aliases("stereo combo for gigging, pedal platform", "- The Beatles used AC30 combos. (https://x)",
                              [], opener=lambda req, timeout: _Reply()) == ["gigging stereo combo"]
