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


def test_gear_gets_one_confidence_scale_and_weaker_items_are_still_searched():
    plan = _plan([{"kind": "guitar", "name": "Gretsch White Falcon", "confidence": "recording_confirmed"},
                  {"kind": "amp", "name": "Vox AC30", "confidence": "artist_general"},
                  {"kind": "amp", "name": "Matchless DC-30", "confidence": "speculative"},
                  {"kind": "effect", "name": "Strymon Timeline"}],
                 ["Vox AC30", "Matchless DC-30"])
    assert [g["confidence"] for g in plan["gear"]] == ["best", "close", "alternative", "close"]
    assert plan["search_queries"] == ["Vox AC30", "Matchless DC-30"]  # the guitar isn't searchable


def test_every_documented_amp_is_searched_whatever_its_level():
    plan = _plan([{"kind": "amp", "name": "Diezel VH4", "confidence": "best"},
                  {"kind": "amp", "name": "Vox AC30", "confidence": "close"}], ["Diezel VH4", "Vox AC30"])
    assert plan["search_queries"] == ["Diezel VH4", "Vox AC30"]


def test_searches_for_alternatives_go_last():
    plan = _plan([{"kind": "amp", "name": "Matchless DC-30", "confidence": "alternative"},
                  {"kind": "amp", "name": "Vox AC30", "confidence": "close"},
                  {"kind": "effect", "name": "Strymon Timeline", "confidence": "alternative"},
                  {"kind": "effect", "name": "Boss DD-5", "confidence": "best"}],
                 ["Matchless DC-30", "Strymon Timeline", "Boss DD-5"])
    assert plan["search_queries"] == ["Boss DD-5", "Vox AC30", "Matchless DC-30"]  # the limit drops the Strymon


def test_guitar_searches_are_dropped_by_kind_not_by_name():
    plan = _plan([{"kind": "guitar", "name": "Gibson SG", "confidence": "best"},
                  {"kind": "effect", "name": "Boss SG-1 Slow Gear", "confidence": "best"},
                  {"kind": "amp", "name": "Vox AC30", "confidence": "best"}],
                 ["Gibson SG", "Boss SG-1 Slow Gear", "Vox AC30"])
    assert [g["name"] for g in plan["gear"]][0] == "Gibson SG"  # still in the gear list
    assert plan["search_queries"] == ["Boss SG-1 Slow Gear", "Vox AC30"]


def test_spare_slots_take_amps_that_no_search_covers():
    plan = _plan([{"kind": "amp", "name": "Fender Twin Reverb", "confidence": "best"},
                  {"kind": "amp", "name": "Fender Deluxe Reverb", "confidence": "close"},
                  {"kind": "amp", "name": "Marshall JCM800 2203", "confidence": "close"}],
                 ["Fender Twin Reverb", "Marshall JCM800"])
    assert plan["search_queries"] == ["Fender Twin Reverb", "Marshall JCM800", "Fender Deluxe Reverb"]


def test_only_alternatives_are_still_searched_rather_than_nothing():
    plan = _plan([{"kind": "amp", "name": "Matchless DC-30", "confidence": "alternative"}], ["Matchless DC-30"])
    assert plan["search_queries"] == ["Matchless DC-30"]


def test_ranking_is_told_the_levels_and_requirements():
    summary = ai.gear_summary({"summary": "Clean.", "requirements": ["combo", "stereo"], "gear": [
        {"name": "Roland JC-40", "confidence": "best"}, {"name": "DSM Humboldt", "confidence": "alternative"},
        {"name": "Fender Deluxe Reverb", "confidence": "artist"}]})  # an older saved plan's level
    assert summary.startswith("Clean. Requirements for the real gear: combo; stereo.")
    assert "Best match: Roland JC-40." in summary and "Close match: Fender Deluxe Reverb." in summary
    assert "Alternative: DSM Humboldt." in summary


def test_ranking_does_not_mark_captures_down_for_the_physical_box():
    # The fit itself is the model's call (see scripts/eval_plans.py); this checks what the ranker is told.
    summary = ai.gear_summary({"summary": "Clean, high headroom.", "requirements": ["combo with built-in speakers",
                               "stereo", "clean headroom for pedals"], "gear": [{"name": "Fender Twin Reverb"}]})
    sent = []
    reply = {"choices": [{"message": {"content": json.dumps({"ranking": [{"id": 1, "fit": 88, "why": "clean"}]})}}]}
    ranked = ai.rank_packs("stereo combo pedal platform for gigging", summary, [{"id": 1, "title": "Twin Reverb head"}],
                           opener=lambda req, timeout: sent.append(json.loads(req.data)) or _Response(reply))
    prompt = sent[0]["messages"][-1]["content"]
    assert "ignore requirements about the physical box (combo or head, built-in or stereo speakers" in prompt
    assert ranked == {1: {"fit": 88, "why": "clean"}}


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


def test_requirement_requests_keep_the_models_gear_but_no_stray_aliases():
    plan = _recommend("stereo amp cab combo for gigging, a good pedal platform", {
        "summary": "A clean stereo combo for gigs.", "advice": [],
        "requirements": ["combo with speakers", "stereo", "clean headroom for pedals"],
        "search_queries": ["Roland JC-40", "Fender Deluxe Reverb"],
        "aliases": ["blackface deluxe reverb", "british invasion", "beatles"],
        "keywords": ["stereo gigging combo", "pedal platform combo amp", "Roland JC-40"],
        "gear": [{"kind": "amp", "name": "Roland JC-40", "confidence": "best"},
                 {"kind": "amp", "name": "Fender Deluxe Reverb", "role": "mono", "confidence": "close"},
                 {"kind": "other", "name": "DSM Humboldt Simplifier MKII", "confidence": "alternative"}]})
    assert plan["requirements"][1] == "stereo"
    assert plan["aliases"] == ["stereo gigging combo", "pedal platform combo amp"]  # wordings, never names or gear
    assert [g["name"] for g in plan["gear"]] == ["Roland JC-40", "Fender Deluxe Reverb", "DSM Humboldt Simplifier MKII"]
    assert plan["search_queries"] == ["Roland JC-40", "Fender Deluxe Reverb"]


def test_artist_requests_keep_their_pedals_and_aliases():
    plan = _recommend("Gilmour Comfortably Numb solo", {
        "summary": "David Gilmour's solo from Pink Floyd's The Wall.", "advice": [], "requirements": [], "search_queries": ["Hiwatt DR103"],
        "aliases": ["David Gilmour", "Pink Floyd", "The Wall"],
        "gear": [{"kind": "amp", "name": "Hiwatt DR103", "confidence": "best"},
                 {"kind": "effect", "name": "Electro-Harmonix Big Muff", "confidence": "best"}]})
    assert [g["name"] for g in plan["gear"]] == ["Hiwatt DR103", "Electro-Harmonix Big Muff"]
    assert plan["aliases"] == ["David Gilmour", "Pink Floyd", "The Wall"]


def test_a_described_sound_keeps_its_pedals_and_gets_keywords_but_no_names():
    plan = _recommend("chimey jangly clean with a fuzz edge", {
        "summary": "s", "advice": [], "search_queries": ["Vox AC30"], "aliases": ["Beatles"],
        "keywords": ["jangly chimey clean with fuzz edge", "Beatles jangle", "Vox AC30 chime"],
        "gear": [{"kind": "amp", "name": "Vox AC30"}, {"kind": "effect", "name": "Vox Tone Bender"}]})
    assert [g["name"] for g in plan["gear"]] == ["Vox AC30", "Vox Tone Bender"]
    assert plan["aliases"] == ["jangly chimey clean with fuzz edge"]  # a rewording; not an artist or gear


def test_keywords_must_be_mostly_the_requests_own_words():
    notes = "Jimi Hendrix was a master of the edge of breakup."
    kept = ai.filter_aliases("edge breakup blues", [], evidence=notes, gear=[{"name": "Marshall Bluesbreaker", "role": ""}],
                             keywords=["edge of break-up blues", "breakup blues", "Jimi Hendrix blues", "Bluesbreaker"])
    assert kept == ["edge of break-up blues", "breakup blues"]


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




def test_aliases_drop_the_plans_own_gear_but_keep_signature_names():
    plan = _recommend("Tom Quayle", {
        "summary": "Tom Quayle (TQ) plays legato fusion.", "advice": [], "search_queries": ["Laney Lionheart"],
        "aliases": ["Tom Quayle", "Laney Lionheart 60", "Ibanez TQM2", "TQ"],
        "gear": [{"kind": "amp", "name": "Laney Lionheart 60-watt", "confidence": "best"},
                 {"kind": "guitar", "name": "Ibanez TQM2", "confidence": "best"},
                 {"kind": "effect", "name": "Xotic Tom Quayle Signature Drive"}]})
    assert plan["aliases"] == ["Tom Quayle", "TQ"]
    nolly = _recommend("Periphery bass", {
        "summary": "Adam Getgood's bass on Juggernaut, Periphery II and Select Difficulty.", "advice": [],
        "search_queries": [], "aliases": ["Nolly", "Periphery", "Adam Getgood", "Juggernaut", "Periphery II",
                                          "Select Difficulty"],
        "gear": [{"kind": "guitar", "name": "Dingwall NG-3 Nolly Signature"}]})
    assert nolly["aliases"] == ["Nolly", "Periphery", "Adam Getgood", "Juggernaut", "Periphery II"]  # at most 5


def test_aliases_drop_gear_categories_and_keep_the_players_name_in_their_own_gear():
    lake = _recommend("Brandon Lake", {
        "summary": "s", "advice": [], "search_queries": [],
        "aliases": ["Brandon Lake", "Telecaster", "AC30", "channel", "overdrive"],
        "gear": [{"kind": "guitar", "name": "Fender Player Telecaster"}, {"kind": "amp", "name": "Vox AC30 C2"}]})
    assert lake["aliases"] == ["Brandon Lake"]
    clapton = _recommend("Clapton Layla", {
        "summary": "Eric Clapton's tone with Derek and the Dominos.", "advice": [], "search_queries": [], "aliases": ["Eric Clapton", "Derek and the Dominos"],
        "gear": [{"kind": "guitar", "name": "Fender Eric Clapton Stratocaster"}]})
    assert clapton["aliases"] == ["Eric Clapton", "Derek and the Dominos"]


def test_microphones_are_listed_but_never_searched_and_a_classical_guitar_can_be():
    plan = _recommend("make a direct input classical guitar sound like a condenser microphone", {
        "summary": "s", "advice": [], "search_queries": ["Austrian Audio OC818", "Takamine DH90", "acoustic simulator"],
        "gear": [{"kind": "mic", "name": "Austrian Audio OC818", "role": "studio microphone", "confidence": "best"},
                 {"kind": "guitar", "name": "Takamine DH90", "role": "a nylon guitar to sound like", "confidence": "close"}]})
    assert [g["kind"] for g in plan["gear"]] == ["mic", "guitar"]
    assert plan["search_queries"] == ["Takamine DH90", "acoustic simulator"]


def test_an_item_its_own_role_calls_an_alternative_is_labelled_one():
    plan = _recommend("classical guitar condenser microphone", {
        "summary": "s", "advice": [], "search_queries": [],
        "gear": [{"kind": "mic", "name": "Gefell m296", "role": "Alternative high-end studio microphone", "confidence": "best"},
                 {"kind": "mic", "name": "Royer SF2", "role": "a modern equivalent of the original", "confidence": "close"},
                 {"kind": "mic", "name": "Neumann KM184", "role": "the mic on the original recording", "confidence": "best"}]})
    assert [g["confidence"] for g in plan["gear"]] == ["alternative", "alternative", "best"]


def test_ranking_judges_what_a_pack_captures_not_what_recorded_it():
    sent = []
    reply = {"choices": [{"message": {"content": json.dumps({"ranking": [{"id": 1, "fit": 5, "why": "a speaker cab"}]})}}]}
    ai.rank_packs("DI classical guitar to sound miked", "Austrian Audio OC818 microphone",
                  [{"id": 1, "title": "Celestion Vintage 30", "gear": "amp-cab", "description": "recorded with an OC818"}],
                  opener=lambda req, timeout: sent.append(json.loads(req.data)) or _Response(reply))
    assert "a speaker cabinet recorded with the requested microphone is still a speaker cabinet" in \
        sent[0]["messages"][-1]["content"]



def test_other_names_the_evidence_supports_are_kept_without_repeating_the_request():
    notes = ("- Rig: Matt Bellamy of Muse recorded Knights of Cydonia for Black Holes and Revelations in 2006 through "
             "a Diezel VH4. (https://rig.example)")
    body = {"summary": "Bellamy's fuzzy high gain.", "advice": [], "search_queries": ["Diezel VH4"],
            "aliases": ["Muse", "Matt Bellamy", "Black Holes and Revelations", "2006", "Origin of Symmetry"],
            "gear": [{"kind": "amp", "name": "Diezel VH4"}]}
    plan = ai.plan_tone("Knights of Cydonia", research_notes=notes, opener=lambda req, timeout: _Response(
        {"choices": [{"message": {"content": json.dumps(body)}}]}))
    assert plan["aliases"] == ["Muse", "Matt Bellamy", "Black Holes and Revelations", "2006"]  # not the other album
    nolly = _recommend("Periphery bass", {"summary": "Nolly's Darkglass tone.", "advice": [], "search_queries": [],
                                          "aliases": ["Nolly", "Adam Getgood"], "gear": []})
    assert nolly["aliases"] == ["Nolly"]  # the plan names Nolly; nothing names Adam Getgood
