from __future__ import annotations

import base64
import json

import pytest

import app as app_module
from tonesearch import ai, knowledge, mcp_server, overrides, research


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, _n=None):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("custom", "https://x/v1", "m", "k"))
    overrides.activate({})


def _plan(gear, queries):
    content = json.dumps({"summary": "s", "advice": [], "gear": gear, "search_queries": queries})
    return ai.plan_tone("Hillsong United United We Stand", opener=lambda req, timeout: _Response(
        {"choices": [{"message": {"content": content}}]}))


def test_gear_gets_a_confidence_level_and_guesses_are_not_searched():
    plan = _plan([{"kind": "guitar", "name": "Gretsch White Falcon", "confidence": "recording_confirmed"},
                  {"kind": "amp", "name": "Vox AC30", "confidence": "artist_general"},
                  {"kind": "amp", "name": "Matchless DC-30", "confidence": "speculative"},
                  {"kind": "effect", "name": "Strymon Timeline"}],
                 ["Vox AC30", "Matchless DC-30"])
    assert [g["confidence"] for g in plan["gear"]] == ["confirmed", "artist", "suggested", "artist"]
    assert plan["search_queries"] == ["Vox AC30"]  # the confirmed guitar isn't searchable; the guess isn't searched


def test_only_guesses_are_still_searched_rather_than_nothing():
    plan = _plan([{"kind": "amp", "name": "Matchless DC-30", "confidence": "suggested"}], ["Matchless DC-30"])
    assert plan["search_queries"] == ["Matchless DC-30"]


def test_ranking_is_told_which_gear_is_confirmed():
    summary = ai.gear_summary({"summary": "Shimmery clean.", "gear": [
        {"name": "Vox AC30", "confidence": "confirmed"}, {"name": "Matchless DC-30", "confidence": "suggested"},
        {"name": "Fender Telecaster"}]})
    assert "Confirmed for this recording: Vox AC30." in summary and "Suggested substitutes only: Matchless DC-30." in summary
    assert "era unconfirmed): Fender Telecaster." in summary


def test_evidence_keeps_the_sentences_that_qualify_a_claim():
    page = ("<title>Hillsong United gear</title>"
            "<p>Our catalogue of Hillsong United has only two songs, Oceans and Another in the Fire.</p>"
            "<p>The Fender American Professional II Stratocaster listed here is a modern equivalent of his guitar.</p>")
    found = research._extract_evidence(page, "Hillsong United")
    assert "only two songs" in found and "modern equivalent" in found


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"o:pw").decode(), "Origin": "http://localhost"}


def test_admin_marks_unlabelled_gear_as_artist_and_keeps_typed_levels(monkeypatch):
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    db = app_module._library()
    old = knowledge.save(db, "Periphery bass", "- a: Nolly used a Darkglass B7K. (https://x)",
                         [{"kind": "effect", "name": "Darkglass B7K", "role": "drive"}])
    client = app_module.app.test_client()
    page = client.get("/admin", headers=_auth()).get_data(as_text=True)
    assert "1 gear item in the library has no confidence level" in page
    assert client.post("/admin/gear-confidence", headers=_auth()).status_code == 303
    assert knowledge.get(db, old)["gear"][0]["confidence"] == "artist"
    assert "no confidence level" not in client.get("/admin", headers=_auth()).get_data(as_text=True)

    form = {"topic": "Periphery bass", "notes": "n", "action": "save", "status": "new",
            "gear": "effect | Darkglass B7K | drive | confirmed\nguitar | Dingwall NG-2 | the bass\namp | SVT | a | b | suggested"}
    client.post(f"/admin/entries/{old}", data=form, headers=_auth())
    gear = knowledge.get(db, old)["gear"]
    assert [(g["name"], g["role"], g["confidence"]) for g in gear] == [
        ("Darkglass B7K", "drive", "confirmed"), ("Dingwall NG-2", "the bass", "artist"), ("SVT", "a | b", "suggested")]


def test_mcp_save_gear_and_saved_answers_carry_confidence():
    notes = "- Rundown: Nolly runs a Darkglass B7K preamp and an Origin Effects Cali76. (https://x.example)"
    mcp_server.web_research("Periphery bass", notes=lambda text: notes)
    mcp_server.save_gear("Periphery bass", [{"kind": "effect", "name": "Darkglass B7K", "confidence": "confirmed"},
                                            {"kind": "effect", "name": "Origin Effects Cali76"}])
    gear = knowledge.find(app_module._library(), "Periphery bass")["gear"]
    assert [g["confidence"] for g in gear] == ["confirmed", "artist"]
    again = mcp_server.web_research("Periphery bass", notes=lambda text: pytest.fail("reuse the library"))
    assert "Darkglass B7K (confirmed)" in again and "Cali76 (artist)" in again
