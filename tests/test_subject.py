from __future__ import annotations

import json

import pytest

import app as app_module
from tonesearch import ai, cache, knowledge, overrides

NOTES = "- Rundown: Jerry Cantrell played a Marshall JCM800 2203 on Man in the Box. (https://x.example)"
PLAN = {"summary": "s", "advice": [], "search_queries": ["Marshall JCM800"],
        "gear": [{"kind": "amp", "name": "Marshall JCM800 2203", "role": "main amp", "confidence": "best"}]}


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
    overrides.activate({})
    for name in ("NAM_MIXER_AI_PROVIDER", "NAM_MIXER_AI_LOCAL_MODEL", "NAM_MIXER_AI_LOCAL_SUBJECT_MODEL",
                 "NAM_MIXER_AI_SUBJECT_MODEL", "NAM_MIXER_AI_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_subject_words_keep_only_what_the_request_typed_give_or_take_a_typo():
    assert ai.subject_words("Pete Townshend - Live at Leeds", "Pete Townsend") == "Pete Townshend"  # no invented album
    assert ai.subject_words("Miles Davis - Kind of Blue", "warm jazz fusion tone with a vintage amp") == ""
    assert ai.subject_words("Darkthrone - Transylvanian Hunger", "dark throne transylvanian hunger") == \
        "Darkthrone Transylvanian Hunger"  # two typed words run together
    assert ai.subject_words("James Hetfield - Master of Puppets",
                            "vorrei ricreare il suono di james heatfield nella canzone master puppet") == \
        "James Hetfield Master Puppets"  # 'of' wasn't typed; the spelling fixes were close enough
    assert ai.subject_words("Alice in Chains - Man in the Box",
                            "Quisiera sonar como la cancion man in the box de alice in chains") == \
        "Alice in Chains Man in the Box"


def test_the_subject_model_is_its_own_setting_with_the_main_model_as_fallback(monkeypatch):
    monkeypatch.setenv("NAM_MIXER_AI_PROVIDER", "local")
    monkeypatch.setenv("NAM_MIXER_AI_LOCAL_MODEL", "big-model")
    assert ai.subject_config().model == "big-model"
    monkeypatch.setenv("NAM_MIXER_AI_LOCAL_SUBJECT_MODEL", "small-model")
    cfg = ai.subject_config()
    assert cfg.model == "small-model" and cfg.tuning.max_tokens == 120 and ai.config().model == "big-model"
    monkeypatch.setattr(ai, "_is_safe_public_host", lambda host: True)  # no DNS lookups in tests
    overrides.activate({"provider": "custom", "base_url": "https://api.example.com/v1", "model": "their-model",
                        "api_key": "k"})
    try:
        assert ai.subject_config().model == "their-model"  # a visitor's own provider uses their own model
    finally:
        overrides.activate({})


def test_subject_asks_the_model_and_says_whether_it_names_someone(monkeypatch):
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("custom", "https://x/v1", "m", "k"))

    def answer(text):
        return lambda req, timeout: _Response({"choices": [{"message": {"content": json.dumps({"subject": text})}}]})

    assert ai.subject("eric clapon layla", opener=answer("Eric Clapton - Layla")) == ("Eric Clapton Layla", True)
    assert ai.subject("Pete Townsend", opener=answer("Pete Townshend")) == ("Pete Townshend", True)
    assert ai.subject("fender twin clean with a bit of breakup", opener=answer("Fender Twin clean with breakup")) == \
        ("Fender Twin clean with breakup", False)  # a sound, not a name: searched as the visitor wrote it
    assert ai.subject("warm jazz fusion", opener=answer("Miles Davis - Kind of Blue")) == ("", False)


def _search(client, prompt, **extra):
    return client.post("/api/search", json={"prompt": prompt, "use_research": True, **extra}).get_json()


@pytest.fixture
def web(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "web_notes", lambda query: calls.append(query) or NOTES)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: json.loads(json.dumps(PLAN)))
    monkeypatch.setattr(app_module, "tone3000_search", lambda *a, **k: [])
    return calls


def test_another_language_finds_saved_research_through_its_subject(web, monkeypatch):
    knowledge.save(app_module._library(), "Alice in Chains Man in the Box", NOTES)
    monkeypatch.setattr(app_module.ai, "subject", lambda prompt, **k: ("Alice in Chains Man in the Box", True))
    body = _search(app_module.app.test_client(), "Quisiera sonar como la cancion man in the box de alice in chains")
    assert body["library"]["reused"] and web == []  # no web search
    assert cache.activity(app_module._library()).popitem()[1]["research_subject"] == 1


def test_new_research_is_searched_and_saved_under_the_subject(web, monkeypatch):
    monkeypatch.setattr(app_module.ai, "subject", lambda prompt, **k: ("James Hetfield Master Puppets", True))
    body = _search(app_module.app.test_client(), "vorrei il suono di james heatfield nella canzone master puppet")
    assert web == ["James Hetfield Master Puppets"]
    assert knowledge.get(app_module._library(), body["library"]["id"])["topic"] == "James Hetfield Master Puppets"
    # The next visitor who spells it right finds it without the subject call.
    monkeypatch.setattr(app_module.ai, "subject", lambda prompt, **k: pytest.fail("not needed"))
    assert _search(app_module.app.test_client(), "James Hetfield Master of Puppets")["library"]["reused"]


def test_a_sound_is_searched_as_written_and_a_failed_subject_changes_nothing(web, monkeypatch):
    monkeypatch.setattr(app_module.ai, "subject", lambda prompt, **k: ("Fender Twin clean breakup", False))
    _search(app_module.app.test_client(), "Fender Twin clean with a bit of breakup")
    assert web == ["Fender Twin clean with a bit of breakup"]

    def broken(prompt, **k):
        raise ai.AiError("model offline")

    monkeypatch.setattr(app_module.ai, "subject", broken)
    body = _search(app_module.app.test_client(), "Swallow the Sun Plague of Butterflies")
    assert web[-1] == "Swallow the Sun Plague of Butterflies" and "error" not in body


def test_follow_ups_never_ask_for_a_subject(web, monkeypatch):
    monkeypatch.setattr(app_module.ai, "subject", lambda prompt, **k: pytest.fail("follow-ups keep their topic"))
    history = [{"role": "user", "content": "Alice in Chains Man in the Box"}, {"role": "assistant", "content": "s"}]
    _search(app_module.app.test_client(), "more gain", history=history)
