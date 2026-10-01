from __future__ import annotations

import base64
import time

import pytest

import app as app_module
from tonesearch import cache, feedback, knowledge, mcp_server, overrides, research

GEAR = [{"kind": "amp", "name": "Bad Cat Hot Cat 100", "role": "main amp"}]
NOTES = "- Rig page: Wilson had the Bad Cat Hot Cat amp in the studio. (https://example.com/a)"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    overrides.activate({})
    for name in ("NAM_MIXER_AI_PROVIDER", "TONE3000_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def calls(monkeypatch):
    """Counts every expensive step a search takes."""
    counts = {"web": 0, "plan": 0, "search": 0, "rank": 0}

    def web(prompt):
        counts["web"] += 1
        return NOTES

    def plan(*args, **kwargs):
        counts["plan"] += 1
        return {"summary": "Bad Cat crunch", "advice": [], "gear": GEAR, "search_queries": ["Bad Cat"]}

    def search(query, **kwargs):
        counts["search"] += 1
        return [{"id": 1, "title": "Bad Cat Hot Cat", "match_score": 9}, {"id": 2, "title": "JCM800", "match_score": 5}]

    def rank(*args, **kwargs):
        counts["rank"] += 1
        return {1: {"fit": 80, "why": "same amp"}, 2: {"fit": 60, "why": "close"}}

    monkeypatch.setattr(app_module, "web_notes", web)
    monkeypatch.setattr(app_module.ai, "plan_tone", plan)
    monkeypatch.setattr(app_module, "tone3000_search", search)
    monkeypatch.setattr(app_module.ai, "rank_packs", rank)
    return counts


def _search(client, prompt="Porcupine Tree In Absentia", **extra):
    return client.post("/api/search", json={"prompt": prompt, "use_research": True, **extra}).get_json()


def test_a_repeat_search_needs_no_ai_web_or_tone3000(calls):
    client = app_module.app.test_client()
    first = _search(client)
    assert not first["cached"] and calls == {"web": 1, "plan": 1, "search": 1, "rank": 1}
    again = _search(client, "in absentia porcupine tree tone")
    assert again["cached"] and again["plan"] == first["plan"] and [p["id"] for p in again["results"]] == [1, 2]
    assert again["library"]["reused"] and calls == {"web": 1, "plan": 1, "search": 1, "rank": 1}


def test_new_filters_reuse_the_plan_and_ranking_inputs(calls):
    client = app_module.app.test_client()
    _search(client)
    _search(client, filters={"gears": ["amp"]})
    assert calls["plan"] == 1 and calls["web"] == 1 and calls["search"] == 2  # only the catalogue search is new
    assert calls["rank"] == 1  # same pack set, same ranking


def test_search_again_and_refinements_skip_saved_answers(calls):
    client = app_module.app.test_client()
    _search(client)
    assert not _search(client, fresh=True)["cached"] and calls["plan"] == 2 and calls["rank"] == 2
    history = [{"role": "user", "content": "Porcupine Tree In Absentia"}, {"role": "assistant", "content": "s"}]
    refined = _search(client, "more gain", history=history)
    assert not refined["cached"] and refined["topic"] == "Porcupine Tree In Absentia more gain" and calls["plan"] == 3


def test_degraded_answers_are_not_saved(calls, monkeypatch):
    monkeypatch.setattr(app_module.ai, "rank_packs", lambda *a, **k: (_ for _ in ()).throw(app_module.ai.AiError("busy")))
    client = app_module.app.test_client()
    assert _search(client)["warnings"]
    assert not _search(client)["cached"]


def test_pack_votes_reorder_results_and_a_bad_brief_drops_saved_answers(calls):
    client = app_module.app.test_client()
    first = _search(client)
    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4"):
        assert client.post("/api/feedback", json={"topic": first["topic"], "target": "pack", "vote": 1, "pack_id": 2,
                                                  "pack_title": "JCM800"}, headers={"CF-Connecting-IP": ip}).status_code == 200
    reordered = _search(client)
    assert reordered["cached"] and [p["id"] for p in reordered["results"]] == [2, 1]  # 60 + 3*8 = 84 beats 80; the cap is 3 votes
    assert reordered["results"][0]["votes"] == 4

    bad = {"topic": first["topic"], "target": "brief", "vote": -1, "comment": "wrong amp", "library_id": first["library"]["id"]}
    assert client.post("/api/feedback", json=bad).status_code == 200
    assert knowledge.search(app_module._library())[0]["status"] == "flagged"
    assert not _search(client)["cached"] and calls["web"] == 2  # reported research is searched again


def test_voting_twice_changes_the_vote():
    db = app_module._library()
    for vote in (1, -1):
        feedback.record(db, voter_id="v", words="srv", prompt="SRV", target="pack", vote=vote, pack_id=7)
    assert feedback.pack_votes(db, "srv") == {7: -1}


def test_feedback_is_validated():
    client = app_module.app.test_client()
    for body in ({"topic": "SRV", "target": "pack", "vote": 1}, {"topic": "SRV", "target": "brief", "vote": 5},
                 {"topic": "", "target": "brief", "vote": 1}, {"topic": "SRV", "target": "song", "vote": 1}):
        assert client.post("/api/feedback", json=body).status_code == 400


def test_catalogue_results_are_shared_but_download_links_are_never_cached(tmp_path, monkeypatch):
    db = tmp_path / "c.sqlite3"
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    hits = []

    class Reply:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return self.body

    def opener(request, timeout=None):
        hits.append(request.full_url)
        if "/models" in request.full_url:
            return Reply(b'{"data": [{"id": 3, "name": "Crunch", "model_url": "https://s.example/3.nam?sig=x"}]}')
        return Reply(b'{"data": [{"id": 9, "title": "Vox AC30"}]}')

    for _ in range(2):
        assert research.tone3000_search("Vox AC30", opener=opener, cache_db=db)[0]["id"] == 9
        models = research.tone3000_models(9, opener=opener, cache_db=db)
    assert len(hits) == 2 and "model_url" not in models[0] and "sig=" not in str(cache.get(db, "models:9:2", cache.REFERENCE_SECONDS))


def test_mcp_rate_result_saves_feedback_and_reports_research():
    db = app_module._library()
    entry_id = knowledge.save(db, "Porcupine Tree In Absentia", NOTES, GEAR)
    cache.put(db, "plan:absentia porcupine tree:True", {"x": 1}, "absentia porcupine tree")
    key = "t3k_cs_user"
    mcp_server._CHECKED_KEYS[__import__("hashlib").sha256(key.encode()).hexdigest()] = time.time()
    result = mcp_server.call_tool("rate_result", {"description": "Porcupine Tree In Absentia", "rating": "bad",
                                                  "comment": "Bad Cat, not JCM800"}, key)
    assert not result["isError"]
    assert knowledge.get(db, entry_id)["status"] == "flagged"
    assert cache.get(db, "plan:absentia porcupine tree:True", cache.AI_SECONDS) is None
    vote = feedback.recent(db)[0]
    assert vote["source"] == "mcp" and vote["vote"] == -1 and vote["voter"] != key and vote["entry_id"] == entry_id


def test_admin_feedback_view(monkeypatch):
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    feedback.record(app_module._library(), voter_id="v", words="srv texas flood", prompt="SRV Texas Flood",
                    target="brief", vote=-1, comment="needs the Dumble")
    auth = {"Authorization": "Basic " + base64.b64encode(b"o:pw").decode()}
    page = app_module.app.test_client().get("/admin?view=feedback", headers=auth).get_data(as_text=True)
    assert "needs the Dumble" in page and "srv texas flood" in page


def test_new_packs_appear_after_a_day_without_asking_the_ai_again(calls, monkeypatch):
    client = app_module.app.test_client()
    _search(client)
    monkeypatch.setattr(cache, "CATALOGUE_SECONDS", 0)  # a day later: catalogue results and whole answers expire
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [{"id": 3, "title": "New Bad Cat", "match_score": 9}])
    later = _search(client)
    assert not later["cached"] and [p["id"] for p in later["results"]] == [3]
    assert calls["plan"] == 1 and calls["web"] == 1 and calls["rank"] == 2  # new pack set: ranked once more
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [{"id": 3, "title": "New Bad Cat", "match_score": 9}])
    _search(client)
    assert calls["plan"] == 1 and calls["rank"] == 2  # same packs again: the saved ranking is reused


def test_mcp_research_returns_the_websites_saved_answer(calls):
    client = app_module.app.test_client()
    first = _search(client, "Periphery bass compressed distorted highs but clear lows")
    client.post("/api/feedback", json={"topic": first["topic"], "target": "pack", "vote": 1, "pack_id": 2})
    saved = mcp_server.web_research("periphery bass", notes=lambda text: pytest.fail("should not search the web"))
    assert saved.startswith("TONE Search's saved answer") and "Bad Cat crunch" in saved
    assert "[1] Bad Cat Hot Cat" in saved and "+1 player votes" in saved and "Research notes:" in saved
    fresh = mcp_server.web_research("Periphery guitar", notes=lambda text: NOTES)  # a different rig: no reuse
    assert fresh == NOTES


def test_mcp_says_whether_a_saved_answer_was_checked(calls):
    client = app_module.app.test_client()
    first = _search(client, "Periphery bass")
    unchecked = mcp_server.web_research("periphery bass", notes=lambda text: NOTES)
    assert "Not yet checked" in unchecked and "rate_result" in unchecked
    entry = knowledge.get(app_module._library(), first["library"]["id"])
    knowledge.update(app_module._library(), entry["id"], topic=entry["topic"], notes=entry["notes"], gear=[], status="approved")
    _search(client, "Periphery bass")  # the edit cleared saved answers; this saves one again
    assert "The site owner has checked" in mcp_server.web_research("periphery bass", notes=lambda text: NOTES)


def test_mcp_save_gear_fills_an_empty_gear_list_once(monkeypatch):
    db = app_module._library()
    mcp_server.web_research("Periphery bass Nolly", notes=lambda text: NOTES)
    gear = [{"kind": "effect", "name": "Darkglass Microtubes B7K", "role": "drive for the highs"},
            {"kind": "effect", "name": "Compressor"}, {"kind": "weird", "name": "Origin Effects Cali76"}]
    assert mcp_server.save_gear("periphery bass nolly", gear).startswith("Saved 2 gear items")
    entry = knowledge.find(db, "Periphery bass Nolly")
    assert [g["name"] for g in entry["gear"]] == ["Darkglass Microtubes B7K", "Origin Effects Cali76"]
    assert entry["gear"][1]["kind"] == "other"
    assert "left as it is" in mcp_server.save_gear("periphery bass nolly", [{"kind": "amp", "name": "Ampeg SVT"}])
    assert "call web_research first" in mcp_server.save_gear("Comfortably Numb", [{"kind": "amp", "name": "Hiwatt DR103"}])
    mcp_server._CHECKED_KEYS[__import__("hashlib").sha256(b"t3k_cs_user").hexdigest()] = time.time()
    result = mcp_server.call_tool("save_gear", {"description": "x", "gear": [{"kind": "effect", "name": "Delay"}]}, "t3k_cs_user")
    assert result["isError"] and "not categories" in result["content"][0]["text"]
