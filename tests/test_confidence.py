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


def test_admin_gear_lines_take_a_confidence_level(monkeypatch):
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    db = app_module._library()
    old = knowledge.save(db, "Periphery bass", "- a: Nolly used a Darkglass B7K. (https://x)")
    form = {"topic": "Periphery bass", "notes": "n", "action": "save", "status": "new",
            "gear": "effect | Darkglass B7K | drive | confirmed\nguitar | Dingwall NG-2 | the bass\namp | SVT | a | b | suggested"}
    app_module.app.test_client().post(f"/admin/entries/{old}", data=form, headers=_auth())
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


def test_requirement_requests_keep_candidates_that_fit_and_label_them():
    content = json.dumps({"summary": "A clean stereo combo for gigs.", "advice": [], "intent": "product_recommendation",
                          "requirements": ["combo with speakers", "stereo", "clean headroom for pedals"],
                          "search_queries": ["Roland JC-40", "Fender Deluxe Reverb"],
                          "gear": [{"kind": "amp", "name": "Roland JC-40", "confidence": "confirmed"},
                                   {"kind": "amp", "name": "Fender Deluxe Reverb", "role": "mono", "confidence": "artist"},
                                   {"kind": "other", "name": "DSM Humboldt Simplifier MKII", "confidence": "suggested"},
                                   {"kind": "effect", "name": "Ibanez Tube Screamer", "confidence": "artist"},
                                   {"kind": "cab", "name": "Celestion A-Type", "confidence": "artist"}]})
    plan = ai.plan_tone("stereo amp cab combo for gigging, a good pedal platform", opener=lambda req, timeout: _Response(
        {"choices": [{"message": {"content": content}}]}))
    assert plan["intent"] == "requirements" and plan["requirements"][1] == "stereo"
    assert [g["name"] for g in plan["gear"]] == ["Roland JC-40", "Fender Deluxe Reverb", "DSM Humboldt Simplifier MKII"]
    assert plan["search_queries"] == ["Roland JC-40"]  # meets every requirement, so it's what gets searched
    summary = ai.gear_summary(plan)
    assert "Requirements (score packs of gear that breaks a hard one below 40): combo with speakers" in summary
    assert "Meets every requirement: Roland JC-40." in summary and "Partial match" in summary


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


@pytest.mark.parametrize("intent, aliases, expected", [
    (None, [], "requirements"),          # not given: "needs" words decide
    ("artist", [], "requirements"),      # a small model's default, with no artist named
    ("artist", ["SRV"], "artist"),       # a named artist with needs words stays an artist request
    ("requirements", [], "requirements"),
])
def test_intent_is_checked_against_the_request(intent, aliases, expected):
    body = {"summary": "s", "advice": [], "search_queries": ["Fender Blues Deluxe"], "aliases": aliases,
            "gear": [{"kind": "amp", "name": "Fender Blues Deluxe Reissue"}]}
    if intent:
        body["intent"] = intent
    plan = ai.plan_tone("amp cab combo good pedal platform gigging", opener=lambda req, timeout: _Response(
        {"choices": [{"message": {"content": json.dumps(body)}}]}))
    assert plan["intent"] == expected
    assert plan["gear"][0]["confidence"] == "artist"  # unlabelled: a partial match for requirements


def test_a_described_sound_without_needs_or_names_is_a_sound_request():
    body = {"summary": "s", "advice": [], "search_queries": ["Vox AC30"], "gear": []}
    plan = ai.plan_tone("chimey jangly clean", opener=lambda req, timeout: _Response(
        {"choices": [{"message": {"content": json.dumps(body)}}]}))
    assert plan["intent"] == "sound"


def test_requirement_research_gets_no_aliases_and_explains_its_levels(monkeypatch):
    body = {"summary": "Clean pedal platform.", "advice": [], "search_queries": ["Fender Deluxe Reverb"],
            "aliases": ["blackface deluxe reverb", "british invasion", "beatles"],
            "gear": [{"kind": "amp", "name": "Fender Deluxe Reverb"}, {"kind": "cab", "name": "Fender 4x10"}]}
    plan = ai.plan_tone("clean pedal platform amp cab gigging", opener=lambda req, timeout: _Response(
        {"choices": [{"message": {"content": json.dumps(body)}}]}))
    assert plan["intent"] == "requirements" and plan["aliases"] == []
    assert [g["name"] for g in plan["gear"]] == ["Fender Deluxe Reverb"]  # "amp cab" didn't ask for a cab
    db = app_module._library()
    entry_id = knowledge.save(db, "clean pedal platform amp cab gigging", "- a: notes. (https://x)", plan["gear"],
                              plan["aliases"], plan["intent"])
    assert knowledge.find(db, "Beatles British Invasion") is None
    monkeypatch.setattr(app_module, "LIBRARY_ADMIN_PASSWORD", "pw")
    page = app_module.app.test_client().get(f"/admin?q=%23{entry_id}", headers=_auth()).get_data(as_text=True)
    assert "Gear recommendation research" in page and "<strong>artist</strong> = partial match" in page
