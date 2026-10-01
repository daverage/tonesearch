from __future__ import annotations

import base64
import time

import pytest

import app as app_module
from tonesearch import knowledge, mcp_server, overrides

NOTES = "- Gear page: Wilson played a Bad Cat Hot Cat on In Absentia. (https://example.com/a)"
GEAR = [{"kind": "amp", "name": "Bad Cat Hot Cat 100", "role": "main amp"}]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    overrides.activate({})
    for name in ("NAM_MIXER_AI_PROVIDER", "TONE3000_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "knowledge.sqlite3"


def test_topic_words_ignore_filler_and_order():
    assert knowledge.topic_words("I want the Porcupine Tree In Absentia tone") == ["absentia", "porcupine", "tree"]
    assert knowledge.topic_words("in absentia, porcupine tree guitar") == ["absentia", "porcupine", "tree"]


def test_find_matches_strong_overlap_only(db):
    knowledge.save(db, "Porcupine Tree In Absentia tone", NOTES, GEAR)
    found = knowledge.find(db, "porcupine tree - in absentia guitar sound")
    assert found["notes"] == NOTES and found["gear"] == GEAR and found["score"] == 1.0
    assert knowledge.find(db, "Porcupine Tree Deadwing") is None  # 2 of 4 words: too weak
    assert knowledge.find(db, "Comfortably Numb") is None


def test_flagged_and_expired_entries_are_not_reused(db, monkeypatch):
    entry_id = knowledge.save(db, "Texas Flood SRV", NOTES)
    assert knowledge.flag(db, entry_id, "wrong amp")
    assert knowledge.find(db, "Texas Flood SRV") is None
    other = knowledge.save(db, "Comfortably Numb solo", NOTES)
    monkeypatch.setattr(knowledge, "UNREVIEWED_DAYS", 0)
    time.sleep(0.01)
    assert knowledge.find(db, "Comfortably Numb solo") is None
    knowledge.update(db, other, topic="Comfortably Numb solo", notes=NOTES, gear=[], status="approved")
    assert knowledge.find(db, "Comfortably Numb solo")["status"] == "approved"  # approved never expires


def test_save_never_overwrites_approved_research(db):
    entry_id = knowledge.save(db, "Texas Flood SRV", NOTES, GEAR)
    knowledge.update(db, entry_id, topic="Texas Flood SRV", notes="checked", gear=GEAR, status="approved")
    assert knowledge.save(db, "SRV Texas Flood", "newer, unchecked") == entry_id
    assert knowledge.find(db, "Texas Flood SRV")["notes"] == "checked"


def test_search_reuses_library_and_saves_fresh_research(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "web_notes", lambda prompt: calls.append(prompt) or NOTES)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: {
        "summary": "s", "advice": [], "gear": GEAR, "search_queries": ["Bad Cat"]})
    monkeypatch.setattr(app_module, "tone3000_search", lambda *a, **k: [])
    client = app_module.app.test_client()
    first = client.post("/api/search", json={"prompt": "Porcupine Tree In Absentia", "use_research": True}).get_json()
    assert first["library"]["reused"] is False and len(calls) == 1
    second = client.post("/api/search", json={"prompt": "in absentia porcupine tree tone", "use_research": True}).get_json()
    assert second["library"] == {"id": first["library"]["id"], "status": "new", "reused": True}
    assert second["research_notes"] == NOTES and len(calls) == 1  # no second web search

    assert client.post(f"/api/library/{first['library']['id']}/flag", json={"reason": "wrong"}).status_code == 200
    client.post("/api/search", json={"prompt": "Porcupine Tree In Absentia", "use_research": True})
    assert len(calls) == 2  # flagged research is searched again
    assert client.post("/api/library/999/flag", json={}).status_code == 404


def test_mcp_research_reads_and_writes_the_library(monkeypatch):
    found = mcp_server.web_research("Porcupine Tree In Absentia", notes=lambda text: NOTES)
    assert found == NOTES
    again = mcp_server.web_research("In Absentia Porcupine Tree", notes=lambda text: pytest.fail("should reuse"))
    assert again.startswith("From TONE Search's research library (not yet reviewed).") and NOTES in again


def _auth(password):
    return {"Authorization": "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()}


def test_admin_page_needs_the_password_and_same_site_posts(monkeypatch, db):
    client = app_module.app.test_client()
    assert client.get("/admin").status_code == 404  # off without a password
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "secret")
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", headers=_auth("wrong")).status_code == 401
    entry_id = knowledge.save(db, "Texas Flood SRV", NOTES)
    page = client.get("/admin", headers=_auth("secret"))
    assert page.status_code == 200 and b"Texas Flood SRV" in page.data

    form = {"topic": "Texas Flood SRV", "notes": NOTES, "gear": "amp | Fender Vibroverb | main amp", "action": "approve"}
    assert client.post(f"/admin/entries/{entry_id}", data=form, headers=_auth("secret")).status_code == 403
    same_site = {**_auth("secret"), "Origin": "http://localhost"}
    assert client.post(f"/admin/entries/{entry_id}", data=form, headers=same_site).status_code == 303
    entry = knowledge.find(db, "Texas Flood SRV")
    assert entry["status"] == "approved" and entry["gear"][0]["name"] == "Fender Vibroverb"
    client.post(f"/admin/entries/{entry_id}", data={"action": "delete"}, headers=same_site)
    assert knowledge.search(db) == []


def test_fresh_research_keeps_a_report_for_review(db):
    entry_id = knowledge.save(db, "Texas Flood SRV", NOTES)
    knowledge.flag(db, entry_id, "wrong amp")
    knowledge.save(db, "SRV Texas Flood", "- b: fresher notes. (https://example.com/b)")
    entry = knowledge.search(db)[0]
    assert entry["status"] == "flagged" and entry["flag_reason"] == "wrong amp" and "fresher" in entry["notes"]


def test_a_broken_library_never_stops_research(monkeypatch, tmp_path):
    missing = tmp_path / "no" / "such" / "dir" / "k.sqlite3"
    assert knowledge.find(missing, "SRV") is None and knowledge.save(missing, "SRV", NOTES) is None
    monkeypatch.setattr(mcp_server, "library_path", lambda: missing)
    assert mcp_server.web_research("SRV Texas Flood", notes=lambda text: NOTES) == NOTES


def test_admin_actions_return_to_the_same_filter(monkeypatch, db):
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "secret")
    entry_id = knowledge.save(db, "Texas Flood SRV", NOTES)
    headers = {**_auth("secret"), "Origin": "http://localhost"}
    client = app_module.app.test_client()
    assert b'name="filter_status" value="new"' in client.get("/admin?status=new&q=flood", headers=headers).data
    form = {"topic": "Texas Flood SRV", "notes": NOTES, "gear": "", "action": "approve",
            "filter_q": "flood", "filter_status": "new"}
    location = client.post(f"/admin/entries/{entry_id}", data=form, headers=headers).headers["Location"]
    assert "q=flood" in location and "status=new" in location and "message=" in location
