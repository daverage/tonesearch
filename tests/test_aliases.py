from __future__ import annotations

import base64
import json

import pytest

import app as app_module
from tonesearch import ai, knowledge, mcp_server, overrides

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
    assert knowledge.match_score("Nolly Getgood bass", words, aliases) > 0
    assert knowledge.match_score("Periphery", words, aliases) == 0  # one word is not enough
    assert knowledge.match_score("Nolly Getgood bass Juggernaut", words, aliases) == 0  # more specific than saved
    assert knowledge.match_score("Nolly bass warm", words, aliases, same_sound=True) == 0  # a sound it never described
    assert knowledge.match_score("Nolly bass warm", words, aliases) > 0  # but the research still applies


def test_aliases_are_cleaned_of_descriptions():
    assert knowledge.clean_aliases(["Nolly", "Adam Getgood", "warm and punchy", "Nolly"]) == ["nolly", "adam getgood"]


def test_a_search_under_another_name_reuses_the_saved_answer_and_research(calls):
    first = _search("Periphery bass")
    assert knowledge.get(app_module._library(), first["library"]["id"])["aliases"] == "nolly, adam getgood, periphery"
    again = _search("Nolly Getgood bass")
    assert again["cached"] and calls == {"web": 1, "plan": 1}
    other_sound = _search("Nolly Getgood bass warm")
    assert not other_sound["cached"] and calls == {"web": 1, "plan": 2}  # new brief, same research
    assert other_sound["library"]["reused"]
    assert not _search("Nolly Getgood bass", filters={"gears": ["pedal"]})["cached"]  # other filters


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
