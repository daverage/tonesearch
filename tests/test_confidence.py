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


def test_gear_gets_one_confidence_scale_and_weaker_items_are_not_searched():
    plan = _plan([{"kind": "guitar", "name": "Gretsch White Falcon", "confidence": "recording_confirmed"},
                  {"kind": "amp", "name": "Vox AC30", "confidence": "artist_general"},
                  {"kind": "amp", "name": "Matchless DC-30", "confidence": "speculative"},
                  {"kind": "effect", "name": "Strymon Timeline"}],
                 ["Vox AC30", "Matchless DC-30"])
    assert [g["confidence"] for g in plan["gear"]] == ["best", "close", "alternative", "close"]
    assert plan["search_queries"] == ["Vox AC30"]  # the guitar isn't searchable; the weaker amp isn't searched


def test_only_alternatives_are_still_searched_rather_than_nothing():
    plan = _plan([{"kind": "amp", "name": "Matchless DC-30", "confidence": "alternative"}], ["Matchless DC-30"])
    assert plan["search_queries"] == ["Matchless DC-30"]


def test_ranking_is_told_the_levels_and_requirements():
    summary = ai.gear_summary({"summary": "Clean.", "requirements": ["combo", "stereo"], "gear": [
        {"name": "Roland JC-40", "confidence": "best"}, {"name": "DSM Humboldt", "confidence": "alternative"},
        {"name": "Fender Deluxe Reverb", "confidence": "artist"}]})  # an older saved plan's level
    assert "Requirements (score packs of gear that breaks a hard one below 40): combo; stereo." in summary
    assert "Best match: Roland JC-40." in summary and "Close match: Fender Deluxe Reverb." in summary
    assert "Alternative: DSM Humboldt." in summary


def test_evidence_keeps_the_sentences_that_qualify_a_claim():
    page = ("<title>Hillsong United gear</title>"
            "<p>Our catalogue of Hillsong United has only two songs, Oceans and Another in the Fire.</p>"
            "<p>The Fender American Professional II Stratocaster listed here is a modern equivalent of his guitar.</p>")
    found = research._extract_evidence(page, "Hillsong United")
    assert "only two songs" in found and "modern equivalent" in found


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"o:pw").decode(), "Origin": "http://localhost"}


def test_admin_gear_lines_take_a_level_and_read_old_names(monkeypatch):
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    db = app_module._library()
    old = knowledge.save(db, "Periphery bass", "- a: Nolly used a Darkglass B7K. (https://x)",
                         [{"kind": "effect", "name": "Darkglass B7K", "confidence": "confirmed"}])
    client = app_module.app.test_client()
    assert "effect | Darkglass B7K |  | best" in client.get(f"/admin?q=%23{old}", headers=_auth()).get_data(as_text=True)
    form = {"topic": "Periphery bass", "notes": "n", "action": "save", "status": "new",
            "gear": "effect | Darkglass B7K | drive | best\nguitar | Dingwall NG-2 | the bass\namp | SVT | a | b | suggested"}
    client.post(f"/admin/entries/{old}", data=form, headers=_auth())
    gear = knowledge.get(db, old)["gear"]
    assert [(g["name"], g["role"], g["confidence"]) for g in gear] == [
        ("Darkglass B7K", "drive", "best"), ("Dingwall NG-2", "the bass", "close"), ("SVT", "a | b", "alternative")]


def test_mcp_save_gear_and_saved_answers_carry_levels():
    notes = "- Rundown: Nolly runs a Darkglass B7K preamp and an Origin Effects Cali76. (https://x.example)"
    mcp_server.web_research("Periphery bass", notes=lambda text: notes)
    mcp_server.save_gear("Periphery bass", [{"kind": "effect", "name": "Darkglass B7K", "confidence": "best"},
                                            {"kind": "effect", "name": "Origin Effects Cali76"}])
    gear = knowledge.find(app_module._library(), "Periphery bass")["gear"]
    assert [g["confidence"] for g in gear] == ["best", "close"]
    again = mcp_server.web_research("Periphery bass", notes=lambda text: pytest.fail("reuse the library"))
    assert "Darkglass B7K (best)" in again and "Cali76 (close)" in again and "best = documented" in again


def _recommend(prompt, body):
    return ai.plan_tone(prompt, opener=lambda req, timeout: _Response({"choices": [{"message": {"content": json.dumps(body)}}]}))


def test_requests_with_requirements_keep_only_the_gear_asked_for():
    plan = _recommend("stereo amp cab combo for gigging, a good pedal platform", {
        "summary": "A clean stereo combo for gigs.", "advice": [],
        "requirements": ["combo with speakers", "stereo", "clean headroom for pedals"],
        "search_queries": ["Roland JC-40", "Fender Deluxe Reverb"],
        "aliases": ["blackface deluxe reverb", "british invasion", "beatles"],
        "gear": [{"kind": "amp", "name": "Roland JC-40", "confidence": "best"},
                 {"kind": "amp", "name": "Fender Deluxe Reverb", "role": "mono", "confidence": "close"},
                 {"kind": "other", "name": "DSM Humboldt Simplifier MKII", "confidence": "alternative"},
                 {"kind": "effect", "name": "Ibanez Tube Screamer", "confidence": "close"},
                 {"kind": "cab", "name": "Celestion A-Type", "confidence": "close"}]})
    assert plan["requirements"][1] == "stereo" and plan["aliases"] == []  # no names in the request
    assert [g["name"] for g in plan["gear"]] == ["Roland JC-40", "Fender Deluxe Reverb", "DSM Humboldt Simplifier MKII"]
    assert plan["search_queries"] == ["Roland JC-40"]


def test_artist_requests_keep_their_pedals_and_aliases():
    plan = _recommend("Gilmour Comfortably Numb solo", {
        "summary": "s", "advice": [], "requirements": [], "search_queries": ["Hiwatt DR103"],
        "aliases": ["David Gilmour", "Pink Floyd", "The Wall"],
        "gear": [{"kind": "amp", "name": "Hiwatt DR103", "confidence": "best"},
                 {"kind": "effect", "name": "Electro-Harmonix Big Muff", "confidence": "best"}]})
    assert [g["name"] for g in plan["gear"]] == ["Hiwatt DR103", "Electro-Harmonix Big Muff"]
    assert plan["aliases"] == ["David Gilmour", "Pink Floyd", "The Wall"]


def test_a_described_sound_keeps_its_pedals_but_gets_no_aliases():
    plan = _recommend("chimey jangly clean with a fuzz edge", {
        "summary": "s", "advice": [], "search_queries": ["Vox AC30"], "aliases": ["Beatles"],
        "gear": [{"kind": "amp", "name": "Vox AC30"}, {"kind": "effect", "name": "Vox Tone Bender"}]})
    assert [g["name"] for g in plan["gear"]] == ["Vox AC30", "Vox Tone Bender"] and plan["aliases"] == []


def test_requirement_requests_research_specs_not_rigs(monkeypatch):
    monkeypatch.setattr(research.time, "sleep", lambda seconds: None)
    asked = []
    with pytest.raises(RuntimeError):
        research.web_notes("best stereo combo for gigging with pedals", search=lambda q, *a, **k: asked.append(q) or [])
    endings = {q.rsplit(" ", 1)[1] for q in asked}
    assert {"specifications", "review"} <= endings and not any("guitarist" in q for q in asked)
    asked.clear()
    with pytest.raises(RuntimeError):
        research.web_notes("Periphery bass tone", search=lambda q, *a, **k: asked.append(q) or [])
    assert asked and all("bassist" in q for q in asked if q != "Periphery bass")  # the last try is the bare topic


