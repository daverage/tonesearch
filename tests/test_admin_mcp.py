from __future__ import annotations

import json

import pytest

import app as app_module
from tonesearch import admin_mcp, cache, knowledge, overrides

NOTES = ("- Rig rundown: Nolly runs a Darkglass B7K preamp into a Cali76 compressor. (https://rigs.example/nolly)\n"
         "- Gear list: Search snippet only: Nolly played a Dingwall NG-2 through the album sessions. (https://list.example/x)")
PLAN = {"summary": "Clean compressed lows under distorted highs.", "advice": [], "search_queries": ["Darkglass B7K"],
        "gear": [{"kind": "effect", "name": "Darkglass B7K", "role": "drive", "confidence": "best"}],
        "aliases": ["Nolly", "Adam Getgood"]}
TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "ADMIN_MCP_TOKEN", TOKEN)
    overrides.activate({})
    monkeypatch.setattr(app_module, "web_notes", lambda prompt: NOTES)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: json.loads(json.dumps(PLAN)))
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [{"id": 1, "title": "B7K", "match_score": 9}])
    monkeypatch.setattr(app_module.ai, "rank_packs", lambda *a, **k: {1: {"fit": 90, "why": "same preamp"}})


@pytest.fixture
def client():
    return app_module.app.test_client()


@pytest.fixture
def entry_id(client):
    body = client.post("/api/search", json={"prompt": "Periphery bass", "use_research": True}).get_json()
    return body["library"]["id"]


def _call(client, name, headers=None, query="", **arguments):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    reply = client.post(f"/admin/mcp{query}", json=message,
                        headers=headers if headers is not None else {"Authorization": f"Bearer {TOKEN}"})
    assert reply.status_code == 200
    result = reply.get_json()["result"]
    return result["content"][0]["text"], result["isError"]


def _saved_answers():
    return cache.by_prefix(app_module._library(), "result:", 1e9)


def test_the_endpoint_hides_without_the_right_token(client, monkeypatch):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post("/admin/mcp", json=message).status_code == 404
    assert client.post("/admin/mcp", json=message, headers={"Authorization": "Bearer wrong"}).status_code == 404
    assert client.post("/admin/mcp", json=message, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.post(f"/admin/mcp?key={TOKEN}", json=message).status_code == 200
    monkeypatch.setattr(app_module, "ADMIN_MCP_TOKEN", "")
    assert client.post("/admin/mcp", json=message, headers={"Authorization": "Bearer "}).status_code == 404


def test_review_queue_and_entry_show_what_the_owner_needs(client, entry_id):
    text, error = _call(client, "review_queue")
    assert not error
    assert f"#{entry_id} Periphery bass [new]: 1 gear, 2 sources (1 snippet only)" in text
    text, error = _call(client, "get_entry", entry_id=entry_id)
    assert not error
    assert "effect | Darkglass B7K | drive | best" in text
    assert "[2] Gear list: Search snippet only:" in text
    assert "Clean compressed lows under distorted highs." in text  # the saved brief built on it


def test_changes_are_only_saved_with_confirm(client, entry_id):
    gear = [{"kind": "amp", "name": "Darkglass B7K Ultra", "role": "preamp", "confidence": "close"}]
    text, error = _call(client, "update_entry", entry_id=entry_id, gear=gear, aliases=["Nolly"], remove_sources=[2],
                        status="approved")
    assert not error and "NOT saved" in text
    assert "- effect | Darkglass B7K | drive | best" in text and "+ amp | Darkglass B7K Ultra | preamp | close" in text
    assert "- adam getgood" in text and "Remove source [2]" in text and "Status: new -> approved" in text
    assert knowledge.get(app_module._library(), entry_id)["status"] == "new"
    assert _saved_answers()

    text, error = _call(client, "update_entry", entry_id=entry_id, gear=gear, aliases=["Nolly"], remove_sources=[2],
                        status="approved", confirm=True)
    assert not error and text.startswith(f"Saved research #{entry_id}")
    entry = knowledge.get(app_module._library(), entry_id)
    assert entry["status"] == "approved" and entry["gear"] == gear and entry["aliases"] == "nolly"
    assert "Dingwall" not in entry["notes"] and "B7K preamp" in entry["notes"]
    assert not _saved_answers()  # built on the old research


def test_update_refuses_categories_and_emptying_the_sources(client, entry_id):
    text, error = _call(client, "update_entry", entry_id=entry_id,
                        gear=[{"kind": "effect", "name": "Compressor", "role": "x", "confidence": "best"}])
    assert error and "category" in text
    text, error = _call(client, "update_entry", entry_id=entry_id, remove_sources=[1, 2], confirm=True)
    assert error and "delete_entry" in text
    text, error = _call(client, "update_entry", entry_id=entry_id, remove_sources=[3])
    assert error


def test_approve_keeps_everything(client, entry_id):
    before = knowledge.get(app_module._library(), entry_id)
    text, error = _call(client, "approve_entry", entry_id=entry_id)
    assert not error and "Approved" in text
    after = knowledge.get(app_module._library(), entry_id)
    assert after["status"] == "approved"
    assert (after["notes"], after["gear"], after["aliases"]) == (before["notes"], before["gear"], before["aliases"])


def test_delete_needs_confirm(client, entry_id):
    text, error = _call(client, "delete_entry", entry_id=entry_id)
    assert not error and "NOT deleted" in text
    assert knowledge.get(app_module._library(), entry_id)
    text, error = _call(client, "delete_entry", entry_id=entry_id, confirm=True)
    assert not error and text.startswith(f"Deleted research #{entry_id}")
    assert knowledge.get(app_module._library(), entry_id) is None
    text, error = _call(client, "get_entry", entry_id=entry_id)
    assert error


def test_the_review_prompt_and_unknown_arguments(client):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    reply = client.post("/admin/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                                                             "params": {"name": "review_library"}}).get_json()
    assert "approve the ones that are right" in reply["result"]["messages"][0]["content"]["text"]
    text, error = _call(client, "approve_entry", entry_id=1, extra=True)
    assert error and "takes entry_id" in text
    assert set(admin_mcp.TOOLS) == {"review_queue", "get_entry", "approve_entry", "update_entry", "delete_entry"}
