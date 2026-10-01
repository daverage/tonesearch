from __future__ import annotations

import io
import json

import pytest

import app as app_module
from tonesearch import mcp_server, overrides, research


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def recording_opener(payload, seen):
    def opener(request, timeout=None):
        seen.append(request)
        return FakeResponse(json.dumps(payload).encode())
    return opener


def no_network(*args, **kwargs):
    pytest.fail("no network expected")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server_owner")  # must never be used for MCP callers
    overrides.activate({})


def call(name, arguments, key="t3k_cs_user"):
    return mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}}, key)["result"]


def test_tools_need_the_callers_own_key_and_never_the_servers():
    result = call("lookup", {"kind": "makes", "text": "vox"}, key="")
    assert result["isError"] and "your own TONE3000 secret key" in result["content"][0]["text"]
    assert call("lookup", {"kind": "makes", "text": "vox"}, key="t3k_pk_public")["isError"]


def test_search_packs_sends_filters_with_the_callers_key(monkeypatch):
    seen = []
    tone = {"id": 7, "title": "Vox AC30 Top Boost", "user": {"username": "someone"}}
    monkeypatch.setattr(research, "urlopen", recording_opener({"data": [tone]}, seen))
    monkeypatch.setitem(mcp_server.TOOLS, "search_packs", (
        lambda **kw: mcp_server.search_packs(**kw, opener=research.urlopen), *mcp_server.TOOLS["search_packs"][1:]))
    result = call("search_packs", {"query": "Vox AC30", "gears": ["amp"], "makes": ["vox"]})
    assert not result["isError"] and json.loads(result["content"][0]["text"])[0]["id"] == 7
    assert seen[0].get_header("Authorization") == "Bearer t3k_cs_user"
    assert "gears=amp" in seen[0].full_url and "makes=vox" in seen[0].full_url
    assert overrides.get("tone3000_api_key") == ""  # the key does not outlive the call


def test_bad_arguments_become_tool_errors():
    assert call("search_packs", {"query": "Vox", "gears": ["tuba"]})["isError"]
    assert "required" in call("search_packs", {})["content"][0]["text"]
    assert call("search_packs", {"query": "Vox", "colour": "red"})["isError"]


def test_protocol_basics():
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26"}}, "")
    assert init["result"]["protocolVersion"] == "2025-03-26"
    listed = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "")
    assert [t["name"] for t in listed["result"]["tools"]] == list(mcp_server.TOOLS)
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "") is None
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}, "")["error"]["code"] == -32601
    prompt = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
                                "params": {"name": "find_tone", "arguments": {"description": "SRV Texas Flood"}}}, "")
    assert "SRV Texas Flood" in prompt["result"]["messages"][0]["content"]["text"]


def test_web_research_needs_a_key_tone3000_accepts(monkeypatch):
    from urllib.error import HTTPError
    monkeypatch.setattr(mcp_server, "_CHECKED_KEYS", {})
    monkeypatch.setitem(mcp_server.TOOLS, "web_research", (
        lambda description: f"notes on {description}", *mcp_server.TOOLS["web_research"][1:]))
    assert call("web_research", {"description": "Gilmour"}, key="")["isError"]

    def refused(request, timeout=None):
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(research, "urlopen", refused)
    monkeypatch.setattr(mcp_server._check_key, "__kwdefaults__", {"opener": refused})
    assert "did not accept your key" in call("web_research", {"description": "Gilmour"}, key="t3k_cs_fake")["content"][0]["text"]

    seen = []
    monkeypatch.setattr(mcp_server._check_key, "__kwdefaults__", {"opener": recording_opener({"data": []}, seen)})
    for _ in range(2):
        assert call("web_research", {"description": "Gilmour"})["content"][0]["text"] == "notes on Gilmour"
    assert len(seen) == 1 and seen[0].get_header("Authorization") == "Bearer t3k_cs_user"  # checked once, then cached


def test_hosted_endpoint_reads_bearer_key_and_rate_limits(monkeypatch):
    monkeypatch.setitem(app_module.LIMITS, "mcp", 2)
    monkeypatch.setitem(mcp_server.TOOLS, "lookup", (
        lambda kind, text: [overrides.get("tone3000_api_key")], *mcp_server.TOOLS["lookup"][1:]))
    client = app_module.app.test_client()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"kind": "makes", "text": "vox"}}}
    headers = {"Authorization": "Bearer t3k_cs_user"}
    first = client.post("/mcp", json=body, headers=headers).get_json()
    assert json.loads(first["result"]["content"][0]["text"]) == ["t3k_cs_user"]
    client.post("/mcp", json=body, headers=headers)
    limited = client.post("/mcp", json=body, headers=headers)
    assert limited.status_code == 429 and "run it locally" in limited.get_json()["error"]["message"]
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).status_code == 200
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert client.get("/mcp").status_code == 405


def test_hosted_endpoint_accepts_key_in_url(monkeypatch):
    monkeypatch.setitem(mcp_server.TOOLS, "lookup", (
        lambda kind, text: [overrides.get("tone3000_api_key")], *mcp_server.TOOLS["lookup"][1:]))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"kind": "makes", "text": "vox"}}}
    reply = app_module.app.test_client().post("/mcp?key=t3k_cs_url", json=body).get_json()
    assert json.loads(reply["result"]["content"][0]["text"]) == ["t3k_cs_url"]



def test_download_link_is_the_tone3000_pack_page():
    result = call("download_link", {"pack_id": 5})
    assert not result["isError"] and result["content"][0]["text"] == "https://www.tone3000.com/tones/5"


def test_malformed_messages_get_errors_not_crashes(monkeypatch):
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]}, "t3k_cs_user")
    assert reply["error"]["code"] == -32602
    monkeypatch.setitem(mcp_server.TOOLS, "lookup", (
        lambda kind, text: 1 / 0, *mcp_server.TOOLS["lookup"][1:]))
    result = call("lookup", {"kind": "makes", "text": "vox"})
    assert result["isError"] and "ZeroDivisionError" in result["content"][0]["text"]
    client = app_module.app.test_client()
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}).status_code == 200
