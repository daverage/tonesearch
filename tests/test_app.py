from __future__ import annotations

import json

import pytest

import app as app_module
from tonesearch import ai, overrides, research
from tonesearch.research import _tone3000_card_fields


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    overrides.activate({})
    for name in ("NAM_MIXER_AI_PROVIDER", "NAM_MIXER_AI_CLOUDFLARE_MODEL", "NAM_MIXER_AI_CLOUDFLARE_ACCOUNT_ID",
                 "NAM_MIXER_AI_CLOUDFLARE_API_KEY", "NAM_MIXER_AI_MODEL", "NAM_MIXER_AI_API_KEY", "TONE3000_API_KEY",
                 *(f"NAM_MIXER_AI_{name}" for name in ai.TUNING_LIMITS)):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_cloudflare_config_from_environment(monkeypatch):
    monkeypatch.setenv("NAM_MIXER_AI_PROVIDER", "cloudflare")
    monkeypatch.setenv("NAM_MIXER_AI_CLOUDFLARE_MODEL", "@cf/m")
    monkeypatch.setenv("NAM_MIXER_AI_CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("NAM_MIXER_AI_CLOUDFLARE_API_KEY", "tok")
    cfg = ai.config()
    assert cfg.base_url == "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/ai/v1"
    assert cfg.api_key == "tok" and cfg.model == "@cf/m"


def test_custom_provider_requires_https(monkeypatch):
    monkeypatch.setenv("NAM_MIXER_AI_PROVIDER", "custom")
    monkeypatch.setenv("NAM_MIXER_AI_MODEL", "gpt")
    monkeypatch.setenv("NAM_MIXER_AI_CUSTOM_BASE_URL", "http://example.com/v1")
    with pytest.raises(ai.AiError, match="https"):
        ai.config()


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, _n=None):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_plan_parses_provider_json(monkeypatch):
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("custom", "https://x/v1", "m", "k"))
    content = json.dumps({"summary": "s", "advice": ["a"], "gear": [{"kind": "weird", "name": "Fender Vibroverb"}],
                          "search_queries": ["Fender Vibroverb", "Fender Vibroverb"]})
    plan = ai.plan_tone("srv", opener=lambda req, timeout: _Response({"choices": [{"message": {"content": content}}]}))
    assert plan["search_queries"] == ["Fender Vibroverb"] and plan["gear"][0]["kind"] == "other"



def test_plan_moves_amp_brand_pedals_to_effects(monkeypatch):
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("custom", "https://x/v1", "m", "k"))
    names = ["Marshall Shredmaster", "Marshall Bluesbreaker", "Mesa Boogie Trem-O-Verb", "ProCo RAT", "Marshall JCM800"]
    content = json.dumps({"summary": "s", "advice": [], "gear": [{"kind": "amp", "name": n} for n in names], "search_queries": []})
    plan = ai.plan_tone("creep", opener=lambda req, timeout: _Response({"choices": [{"message": {"content": content}}]}))
    assert [g["kind"] for g in plan["gear"]] == ["effect", "amp", "amp", "effect", "amp"]

def test_short_file_names_only_match_when_unambiguous():
    files = ["EDGY - x", "CLEAN - x", "CLEANEST - x"]
    assert ai.match_file("EDGY", files) == "EDGY - x"
    assert ai.match_file("CLEAN", files) is None
    assert ai.match_file("clean - x", files) == "CLEAN - x"


def test_card_fields_keep_only_tone3000_links():
    fields = _tone3000_card_fields({"images": ["https://evil.example/a.jpg"], "url": "https://www.tone3000.com/tones/x"})
    assert fields["image"] is None and fields["url"] == "https://www.tone3000.com/tones/x"


def test_search_ranks_and_rate_limits(client, monkeypatch):
    monkeypatch.setattr(ai, "plan_tone", lambda *a, **k: {"summary": "s", "advice": [], "gear": [], "search_queries": ["A"]})
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [{"id": 1, "title": "one", "match_score": 5},
                                                                       {"id": 2, "title": "two", "match_score": 9}])
    monkeypatch.setattr(ai, "rank_packs", lambda *a, **k: {1: {"fit": 90, "why": "w"}})
    monkeypatch.setitem(app_module.LIMITS, "search", 2)
    first = client.post("/api/search", json={"prompt": "blues"}).get_json()
    assert [p["id"] for p in first["results"]] == [1, 2] and first["results"][0]["ai_fit"] == 90
    assert client.post("/api/search", json={"prompt": "blues"}).status_code == 200
    assert client.post("/api/search", json={"prompt": "blues"}).status_code == 429


def test_page_renders_with_script_root(client):
    html = client.get("/", base_url="http://localhost/tonesearch/").get_data(as_text=True)
    assert '<base href="/tonesearch/">' in html and "not configured" in html
    assert "google-adsense-account" not in html and 'data-ad-client=""' in html  # ads are off unless configured


def test_adsense_only_with_the_owners_publisher_id(client, monkeypatch):
    monkeypatch.setattr(app_module, "ADSENSE_CLIENT", "ca-pub-1234567890")
    html = client.get("/").get_data(as_text=True)
    # The ad script itself is added by app.js, and only for visitors without their own keys.
    assert 'name="google-adsense-account" content="ca-pub-1234567890"' in html and "adsbygoogle.js" not in html
    monkeypatch.setattr(app_module, "ADSENSE_SLOT", "4368199345")
    html = client.get("/").get_data(as_text=True)
    assert 'data-ad-slot="4368199345"' in html and 'id="ad-banner"' in html


def test_visitor_ai_settings_never_borrow_server_secrets(monkeypatch):
    monkeypatch.setenv("NAM_MIXER_AI_PROVIDER", "custom")
    monkeypatch.setenv("NAM_MIXER_AI_API_KEY", "server-secret")
    monkeypatch.setattr(ai, "_is_safe_public_host", lambda host: True)
    overrides.activate({"provider": "custom", "base_url": "https://visitor.example/v1", "model": "m"})
    cfg = ai.config()
    assert cfg.base_url == "https://visitor.example/v1" and cfg.api_key is None and cfg.visitor


def test_visitor_ai_settings_reject_local_and_private_targets(monkeypatch):
    overrides.activate({"provider": "local", "model": "m"})
    with pytest.raises(ai.AiError, match="Cloudflare or custom"):
        ai.config()
    monkeypatch.setattr(ai, "_is_safe_public_host", lambda host: False)
    overrides.activate({"provider": "custom", "base_url": "https://10.0.0.5/v1", "model": "m"})
    with pytest.raises(ai.AiError, match="public"):
        ai.config()


def test_visitor_tone3000_key_wins_and_is_validated(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    overrides.activate({"tone3000_api_key": "t3k_cs_visitor"})
    assert research._require_tone3000_api_key(for_action="search") == "t3k_cs_visitor"
    overrides.activate({"tone3000_api_key": "nope"})
    with pytest.raises(RuntimeError, match="t3k_cs_"):
        research._require_tone3000_api_key(for_action="search")


def test_headers_become_settings_and_own_keys_skip_the_limit(client, monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "plan_tone", lambda *a, **k: seen.append(dict(overrides._current.get())) or
                        {"summary": "s", "advice": [], "gear": [], "search_queries": ["A"]})
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: [])
    monkeypatch.setitem(app_module.LIMITS, "search", 1)
    own = {"X-AI-Provider": "custom", "X-AI-Model": "m", "X-AI-Key": "k", "X-TONE3000-Key": "t3k_cs_v"}
    for _ in range(3):
        assert client.post("/api/search", json={"prompt": "blues"}, headers=own).status_code == 200
    assert seen[0] == {"provider": "custom", "model": "m", "api_key": "k", "tone3000_api_key": "t3k_cs_v"}
    no_ai_key = {k: v for k, v in own.items() if k != "X-AI-Key"}
    assert client.post("/api/search", json={"prompt": "blues"}, headers=no_ai_key).status_code == 200
    limited = client.post("/api/search", json={"prompt": "blues"})
    assert limited.status_code == 429 and "own AI key" in limited.get_json()["error"]


def _cloudflare_env(monkeypatch):
    monkeypatch.setenv("NAM_MIXER_AI_CLOUDFLARE_MODEL", "@cf/m")
    monkeypatch.setenv("NAM_MIXER_AI_CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("NAM_MIXER_AI_CLOUDFLARE_API_KEY", "tok")


def test_server_tuning_from_environment_is_clamped(monkeypatch):
    _cloudflare_env(monkeypatch)
    monkeypatch.setenv("NAM_MIXER_AI_MAX_TOKENS", "4000")
    monkeypatch.setenv("NAM_MIXER_AI_TIMEOUT_SECONDS", "9999")
    tuning = ai.config().tuning
    assert tuning.max_tokens == 4000 and tuning.timeout_seconds == 180 and tuning.temperature == 0.3
    monkeypatch.setenv("NAM_MIXER_AI_TEMPERATURE", "warm")
    with pytest.raises(ai.AiError, match="TEMPERATURE must be a number"):
        ai.config()


def test_visitor_tuning_only_applies_to_their_own_ai(monkeypatch):
    _cloudflare_env(monkeypatch)
    overrides.activate({"max_tokens": "6000"})
    assert ai.config().tuning.max_tokens == 1800
    overrides.activate({"provider": "cloudflare", "account_id": "b" * 32, "api_key": "mine", "model": "m",
                        "max_tokens": "6000", "history_messages": "0"})
    tuning = ai.config().tuning
    assert tuning.max_tokens == 6000 and tuning.history_messages == 0
    assert ai._history([{"role": "user", "content": "x"}], tuning) == []


_RIG_PAGE = """<html><body><nav><a>Tools</a> Tab Generator Chord Generator Guitar Practice Planner Amp Settings Marshall Fender.</nav>
<header>Amp Settings For Layla By Eric Clapton Gear And Tone Tips.</header>
<article><p>Clapton recorded Layla with his Stratocaster through a small Fender Champ amp, cranked hard.</p>
<p>Clapton recorded Layla with his Stratocaster through a small Fender Champ amp, cranked hard.</p>
<p>Subscribe to our newsletter for weekly tips and deals.</p></article>
<footer>Copyright Fender Marshall Vox amp pedal reviews all rights reserved.</footer></body></html>"""


def test_research_topic_drops_conversational_filler():
    assert research._topic("Looking for the tone eric clapton had for layla") == "eric clapton layla"


def test_research_evidence_skips_menus_and_repeats():
    evidence = research._extract_evidence(_RIG_PAGE, "eric clapton layla")
    assert evidence == "Clapton recorded Layla with his Stratocaster through a small Fender Champ amp, cranked hard."


def test_web_notes_filters_sources_and_keeps_urls_last():
    results = [
        {"href": "https://www.tiktok.com/@x/video/1", "title": "TikTok", "body": "Clapton used a Fender Champ amp on Layla."},
        {"href": "https://example.com/layla", "title": "Layla rig", "body": ""},
        {"href": "https://example.org/menu", "title": "Menu page", "body": "Home About Contact"},
    ]
    notes = research.web_notes("layla clapton", search=lambda q, n, **k: results,
                               evidence=lambda href, topic: _RIG_PAGE and research._extract_evidence(_RIG_PAGE, topic)
                               if "example.com" in href else "")
    assert notes.splitlines() == ["- Layla rig: Clapton recorded Layla with his Stratocaster through a small Fender Champ "
                                  "amp, cranked hard. (https://example.com/layla)"]


def test_pack_models_follow_every_page(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    requested = []

    def opener(request, timeout):
        page = int(request.full_url.split("page=")[1].split("&")[0])
        requested.append(page)
        size = 300 if page < 2 else 7
        return _Response({"data": [{"id": page * 100 + i, "name": f"p{page}-{i}", "model_url": "https://x/m.nam"}
                                   for i in range(size)]})

    models = research.tone3000_models(1, opener=opener)
    assert requested == [1, 2] and len(models) == 307


def test_plan_drops_practice_amps_unless_asked(monkeypatch):
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("custom", "https://x/v1", "m", "k"))
    content = json.dumps({"summary": "s", "advice": [], "search_queries": ["Mesa Boogie Trem-O-Verb", "Fender Mustang LT25"],
                          "gear": [{"kind": "amp", "name": "Mesa Boogie Trem-O-Verb"}, {"kind": "amp", "name": "Fender Mustang LT25"}]})
    reply = lambda req, timeout: _Response({"choices": [{"message": {"content": content}}]})
    plan = ai.plan_tone("radiohead creep", opener=reply)
    assert [g["name"] for g in plan["gear"]] == ["Mesa Boogie Trem-O-Verb"] and plan["search_queries"] == ["Mesa Boogie Trem-O-Verb"]
    assert len(ai.plan_tone("creep on a budget practice amp", opener=reply)["gear"]) == 2


def test_research_rejects_related_link_strips():
    strip = "How to get Radiohead's guitar tone → · more Rock guitar tones → · Mesa amp settings for the riff."
    assert research._sentence_score(strip, {"radiohead"}) == 0


def test_search_filters_are_validated_and_sent(client, monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "plan_tone", lambda *a, **k: {"summary": "s", "advice": [], "gear": [], "search_queries": ["A"]})
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: seen.append(k["filters"]) or [])
    good = {"gears": ["pedal", "amp"], "sizes": ["nano"], "tags": ["clean"], "creators": ["ripper"], "sort": "trending",
            "architecture": "any", "verified": True}
    assert client.post("/api/search", json={"prompt": "x", "filters": good}).status_code == 200
    assert seen[0]["gears"] == ["pedal", "amp"] and seen[0]["architecture"] == "any" and seen[0]["format"] == "nam"
    for bad in ({"gears": ["spaceship"]}, {"sizes": "nano"}, {"tags": ["a_b"]}, {"sort": "random"}, {"verified": "yes"}):
        assert client.post("/api/search", json={"prompt": "x", "filters": bad}).status_code == 400


def test_search_params_follow_the_api_separators():
    params = research._search_params("JCM800", {**research.DEFAULT_FILTERS, "gears": ["amp", "pedal"], "sizes": ["nano", "lite"],
                                                "creators": ["a", "b"], "format": "ir", "calibrated": True})
    assert params["gears"] == "amp_pedal" and params["sizes"] == "nano-lite" and params["creators"] == "a,b"
    assert params["format"] == "ir" and "architecture" not in params and params["calibrated"] == "true"


def test_reused_plan_skips_research_and_planning(client, monkeypatch):
    monkeypatch.setattr(ai, "plan_tone", lambda *a, **k: pytest.fail("plan_tone should not run"))
    monkeypatch.setattr(app_module, "web_notes", lambda q: pytest.fail("research should not run"))
    queries = []
    monkeypatch.setattr(app_module, "tone3000_search", lambda q, **k: queries.append(q) or [])
    body = {"prompt": "x", "use_research": True, "reuse_plan": {"summary": "Les Paul into a Marshall", "search_queries": ["Marshall 1959"]}}
    data = client.post("/api/search", json=body).get_json()
    assert queries == ["Marshall 1959"] and data["reused_plan"] is True
    assert client.post("/api/search", json={"prompt": "x", "reuse_plan": {"summary": "s", "search_queries": []}}).status_code == 400


def test_lookup_is_cached_and_bounded(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "tone3000_lookup", lambda kind, q: calls.append((kind, q)) or [{"value": "marshall", "label": "Marshall", "count": 9}])
    app_module.LOOKUP_CACHE.clear()
    for _ in range(3):
        assert client.get("/api/lookup/makes?query=Mars").get_json()["suggestions"][0]["label"] == "Marshall"
    assert calls == [("makes", "Mars")]
    assert client.get("/api/lookup/makes?query=M").status_code == 400
    assert client.get("/api/lookup/passwords?query=Mars").status_code == 400


class _Http:
    """Fake opener: serves by URL, records which requests carried the API key."""

    def __init__(self, routes):
        self.routes, self.keyed = routes, []

    def __call__(self, request, timeout):
        self.keyed.append((request.full_url, "Authorization" in request.headers))
        route = self.routes[request.full_url.split("?")[0]]
        if isinstance(route, Exception):
            raise route
        return _Response(route) if not isinstance(route, bytes) else _Bytes(route)


class _Bytes(_Response):
    def __init__(self, data):
        self.payload = data


def test_file_downloads_never_send_the_key_off_tone3000(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    monkeypatch.setattr(research, "_is_safe_public_host", lambda host: True)
    models = {"data": [{"id": 7, "name": "Edge", "model_url": "https://storage.example/signed/7.nam?sig=x"}]}
    http = _Http({f"{research.TONE3000_BASE}/models": models, "https://storage.example/signed/7.nam": b"NAMDATA"})
    overrides.activate({"tone3000_api_key": "t3k_cs_visitor"})
    data, name = research.tone3000_model_download(1, 7, opener=http)
    assert data == b"NAMDATA" and name == "Edge"
    assert ("https://storage.example/signed/7.nam?sig=x", False) in http.keyed


def test_pack_download_is_a_zip(client, monkeypatch):
    import io
    import zipfile
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    monkeypatch.setattr(research, "_is_safe_public_host", lambda host: True)
    models = {"data": [{"id": 1, "name": "Edge", "model_url": "https://s.example/1.nam"},
                       {"id": 2, "name": "Edge", "model_url": "https://s.example/2.nam"}]}
    http = _Http({f"{research.TONE3000_BASE}/models": models, "https://s.example/1.nam": b"one", "https://s.example/2.nam": b"two"})
    real_zip = research.tone3000_pack_zip
    monkeypatch.setattr(app_module, "tone3000_pack_zip", lambda tone_id, architecture: real_zip(tone_id, architecture=architecture, opener=http))
    response = client.get("/api/packs/5/download", headers={"X-TONE3000-Key": "t3k_cs_visitor"})
    assert response.status_code == 200 and response.mimetype == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert sorted(archive.namelist()) == ["Edge (2).nam", "Edge.nam"] and archive.read("Edge.nam") == b"one"


def test_a_visitors_own_key_is_always_used_for_downloads(client, monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    seen = []
    monkeypatch.setattr(app_module, "tone3000_pack_zip",
                        lambda tone_id, architecture: seen.append(research._require_tone3000_api_key(for_action="downloads")) or (b"z", 0))
    client.get("/api/packs/5/download", headers={"X-TONE3000-Key": "t3k_cs_visitor"})
    client.get("/api/packs/5/download")
    assert seen == ["t3k_cs_visitor", "t3k_cs_server"]  # the site's key only for visitors without their own


def _cfg():
    return ai.AiConfig("custom", "https://x/v1", "m", "k")


def _replies(*contents, rejects=0):
    """Fake provider: the first `rejects` calls fail as 'format unsupported', then each call returns the next content."""
    from urllib.error import HTTPError
    sent = []

    def opener(request, timeout):
        body = json.loads(request.data)
        sent.append(body.get("response_format", {}).get("type", "none"))
        if len(sent) <= rejects:
            raise HTTPError("u", 400, "unsupported response_format", {}, None)
        content = contents[min(len(sent) - rejects, len(contents)) - 1]
        message = content if isinstance(content, dict) and "content" in content else {"content": content}
        return _Response({"choices": [{"message": message, "finish_reason": message.pop("finish", "stop")}]})
    return opener, sent


@pytest.mark.parametrize("content", [
    'Sure! Here is the plan:\n```json\n{"summary": "Fat Marshall crunch", "gear": [], "search_queries": ["Marshall JCM800"], "advice": []}\n```',
    '{"summary": "Fat Marshall crunch", "gear": [], "search_queries": ["Marshall JCM800",], "advice": [],}',
    "{'summary': 'Fat Marshall crunch', 'gear': [], 'search_queries': ['Marshall JCM800'], 'advice': [], 'extra': None}",
    '{"tone_plan": {"summary": "Fat Marshall crunch", "search_queries": "Marshall JCM800", "advice": "- Bridge pickup\n- Gain at 6"}}',
    '<think>hmm</think>{"summary": "Fat Marshall crunch", "search_queries": ["Marshall JCM800"], "advice": ["Bridge pickup", "Gain at 6", "Stay tight',
])
def test_plan_survives_messy_json(content):
    opener, _ = _replies(content)
    cfg_plan = ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert cfg_plan.summary == "Fat Marshall crunch" and cfg_plan.search_queries == ["Marshall JCM800"]


def test_ranking_accepts_loose_shapes_and_keeps_good_entries():
    content = '[{"pack_id": "12", "score": "85%", "reason": "Plexi"}, {"id": "oops", "fit": 50}, {"id": 13, "fit": 0.4}]'
    opener, _ = _replies(content)
    ranking = ai._ask(_cfg(), "x", "pack_ranking", ai._RANK_SCHEMA, ai._Ranking, opener=opener)
    assert [(r.id, r.fit, r.why) for r in ranking.ranking] == [(12, 85, "Plexi"), (13, 40, "")]


def test_prose_pack_answer_is_kept():
    opener, _ = _replies("The EDGY file is the closest: it has more gain and a brighter top end.")
    answer = ai._ask(_cfg(), "x", "pack_answer", ai._PACK_SCHEMA, ai._PackAnswer, opener=opener, text_fallback="reply")
    assert answer.reply.startswith("The EDGY file") and answer.recommended_files == []


def test_response_format_steps_down_and_is_remembered():
    ai._FORMAT_TIER.clear()
    good = '{"summary": "s", "search_queries": ["Vox AC30"]}'
    opener, sent = _replies(good, good, rejects=2)
    ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert sent == ["json_schema", "json_object", "none", "none"]


def test_thinking_model_out_of_tokens_gets_a_clear_error():
    opener, sent = _replies({"content": None, "reasoning_content": "Let me think about Clapton...", "finish": "length"})
    with pytest.raises(ai.AiError, match="Max tokens"):
        ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert len(sent) == 1  # no pointless retry


def test_shortlist_follows_the_chosen_sort():
    packs = [{"id": i, "match_score": 10 - i, "downloads_count": i * 100, "published": f"2020-0{i + 1}", "catalog_order": (5 - i) * 10}
             for i in range(5)]
    ids = lambda sort: [p["id"] for p in app_module._shortlist(packs, sort)]
    assert ids("best-match") == [0, 1, 2, 3, 4]
    assert ids("downloads-all-time") == [4, 3, 2, 1, 0]
    assert ids("newest") == [4, 3, 2, 1, 0] and ids("oldest") == [0, 1, 2, 3, 4]
    assert ids("trending") == [4, 3, 2, 1, 0]
    assert len(app_module._shortlist(packs * 5, "best-match")) == 12


def test_provider_refusal_explains_itself_without_retrying():
    import io
    from urllib.error import HTTPError
    ai._FORMAT_TIER.clear()
    calls = []

    def opener(request, timeout):
        calls.append(1)
        body = io.BytesIO(b'{"success": false, "errors": [{"code": 10000, "message": "Authentication error"}]}')
        raise HTTPError("u", 403, "Forbidden", {}, body)
    with pytest.raises(ai.AiError, match="HTTP 403.*Authentication error.*Workers AI permission"):
        ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert len(calls) == 1


def test_web_research_retries_other_engines(monkeypatch):
    monkeypatch.setattr(research.time, "sleep", lambda s: None)
    tried = []

    def search(query, n, backend, timeout):
        tried.append((query.split()[0], backend))
        if query != "layla clapton" or backend != "auto":  # only the topic-only last try succeeds
            raise Exception("No results found.")
        return [{"href": "https://example.com/layla", "title": "Layla", "body": ""}]
    notes = research.web_notes("layla clapton", search=search, evidence=lambda href, topic: "Clapton used a Fender Champ amp.")
    assert "example.com/layla" in notes
    assert [b for q, b in tried if q == "layla"] == ["auto", "brave,mojeek,yahoo"] * 2 + ["brave,mojeek,yahoo", "auto"]


def test_web_research_failure_is_explained(monkeypatch):
    monkeypatch.setattr(research.time, "sleep", lambda s: None)

    def search(query, n, backend, timeout):
        raise Exception("No results found.")
    with pytest.raises(RuntimeError, match="may be limiting this server.*continued without it"):
        research.web_notes("layla clapton", search=search, evidence=lambda href, topic: "")


def test_download_finds_the_file_under_the_listed_version(monkeypatch):
    monkeypatch.setenv("TONE3000_API_KEY", "t3k_cs_server")
    monkeypatch.setattr(research, "_is_safe_public_host", lambda host: True)
    pages = {"2": {"data": [{"id": 7, "name": "PiezoV2", "model_url": "https://s.example/7.nam"}]}, "any": {"data": []}}

    def opener(request, timeout):
        url = request.full_url
        if "/models?" in url:
            return _Response(pages["2"] if "architecture=2" in url else pages["any"])
        return _Bytes(b"NAM")
    overrides.activate({"tone3000_api_key": "t3k_cs_visitor"})
    assert research.tone3000_model_download(1, 7, architecture="any", opener=opener) == (b"NAM", "PiezoV2")


def test_unexpected_api_errors_are_json(client, monkeypatch):
    monkeypatch.setattr(app_module, "tone3000_models", lambda *a, **k: 1 / 0)
    response = client.get("/api/packs/1/models")
    assert response.status_code == 500 and "ZeroDivisionError" in response.get_json()["error"]


class _Stream:
    """Fake server-sent-events reply: one `data:` line per chunk, like a streaming OpenAI-compatible API."""
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, chunks, delay=0.0):
        self.lines = [f"data: {json.dumps({'choices': [c]})}\n".encode() for c in chunks] + [b"data: [DONE]\n"]
        self.delay = delay

    def readline(self):
        import time as _time
        _time.sleep(self.delay)
        return self.lines.pop(0) if self.lines else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset_ai_memory():
    for memory in (ai._FORMAT_TIER, ai._HINT_TIER, ai._THINKS, ai._NO_STREAM):
        memory.clear()


def test_streamed_reply_is_assembled_and_asks_for_a_stream():
    _reset_ai_memory()
    sent = []
    parts = ['{"summary": "Fat ', 'Marshall crunch", "search_queries": ["Marshall JCM800"]}']

    def opener(request, timeout):
        sent.append(json.loads(request.data))
        return _Stream([{"delta": {"reasoning_content": "hmm"}}] + [{"delta": {"content": p}} for p in parts]
                       + [{"delta": {}, "finish_reason": "stop"}])
    plan = ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert plan.summary == "Fat Marshall crunch" and sent[0]["stream"] is True


def _hint_opener(accepts):
    """Fake provider that rejects any request carrying a thinking hint it does not know."""
    from urllib.error import HTTPError
    import io
    sent = []
    good = '{"summary": "s", "search_queries": ["Vox AC30"]}'

    def opener(request, timeout):
        body = json.loads(request.data)
        hints = {k for k in ("reasoning_effort", "chat_template_kwargs") if k in body}
        sent.append(hints)
        if hints - accepts:
            raise HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error": {"message": "Unrecognized request argument"}}'))
        return _Response({"choices": [{"message": {"content": good}, "finish_reason": "stop"}]})
    return opener, sent


def test_thinking_hints_step_down_until_accepted_and_are_remembered():
    _reset_ai_memory()
    cfg = ai.AiConfig("cloudflare", "https://x/v1", "@cf/zai-org/glm-4.7-flash", "k")
    opener, sent = _hint_opener(accepts={"chat_template_kwargs"})
    ai._ask(cfg, "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    ai._ask(cfg, "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert sent == [{"reasoning_effort", "chat_template_kwargs"}, {"reasoning_effort"}, {"chat_template_kwargs"},
                    {"chat_template_kwargs"}]
    assert ai._FORMAT_TIER[("cloudflare", "https://x/v1", "@cf/zai-org/glm-4.7-flash")] == 0  # format untouched


def test_endpoint_that_takes_no_hints_still_works():
    _reset_ai_memory()
    cfg = ai.AiConfig("custom", "https://x/v1", "qwen3-32b", "k")
    opener, sent = _hint_opener(accepts=set())
    ai._ask(cfg, "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert sent[-1] == set() and len(sent) == 4


def test_ordinary_models_never_get_thinking_hints():
    _reset_ai_memory()
    opener, sent = _hint_opener(accepts=set())
    ai._ask(ai.AiConfig("cloudflare", "https://x/v1", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "k"),
            "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)
    assert sent == [set()]


def test_model_still_thinking_at_the_deadline_gets_a_clear_error():
    import time as _time
    _reset_ai_memory()
    opener = lambda request, timeout: _Stream([{"delta": {"reasoning_content": "Let me think "}}] * 50, delay=0.02)
    with pytest.raises(ai.AiError, match="still thinking after"):
        ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener, deadline=_time.monotonic() + 0.2)


def test_silent_provider_timeout_is_explained():
    import socket
    _reset_ai_memory()

    def opener(request, timeout):
        raise socket.timeout("The read operation timed out")
    with pytest.raises(ai.AiError, match="did not respond"):
        ai._ask(_cfg(), "x", "tone_plan", ai._PLAN_SCHEMA, ai._Plan, opener=opener)


def test_search_skips_ranking_when_out_of_time(client, monkeypatch):
    plan = {"summary": "s", "advice": [], "gear": [], "search_queries": ["Vox AC30"]}
    monkeypatch.setattr(app_module, "REQUEST_BUDGET_SECONDS", 5)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: plan)
    monkeypatch.setattr(app_module, "tone3000_search", lambda *a, **k: [{"id": 1, "title": "AC30", "match_score": 1}])
    ranked = []
    monkeypatch.setattr(app_module.ai, "rank_packs", lambda *a, **k: ranked.append(1) or {})
    data = client.post("/api/search", json={"prompt": "vox"}).get_json()
    assert ranked == [] and any("ran out of time" in w for w in data["warnings"]) and data["results"][0]["id"] == 1


def _local_ai(monkeypatch):
    monkeypatch.setenv("NAM_MIXER_AI_PROVIDER", "local")
    monkeypatch.setenv("NAM_MIXER_AI_LOCAL_MODEL", "gemma4:e4b")


def test_local_ai_has_no_hourly_limit_and_is_labelled(client, monkeypatch):
    _local_ai(monkeypatch)
    monkeypatch.setitem(app_module.LIMITS, "search", 1)
    monkeypatch.setattr(app_module.ai, "plan_tone", lambda *a, **k: {"summary": "s", "advice": [], "gear": [], "search_queries": ["x"]})
    monkeypatch.setattr(app_module, "tone3000_search", lambda *a, **k: [])
    for _ in range(3):
        response = client.post("/api/search", json={"prompt": "vox"})
        assert response.status_code == 200
    assert response.get_json()["ai"] == {"source": "local", "model": "gemma4:e4b"}
    html = client.get("/").get_data(as_text=True)
    assert 'data-ai-local="gemma4:e4b"' in html and "Local AI on this computer: gemma4:e4b" in html


def test_local_ai_uses_the_settings_tuning():
    from tonesearch import overrides
    import os
    os.environ.update(NAM_MIXER_AI_PROVIDER="local", NAM_MIXER_AI_LOCAL_MODEL="gemma4:e4b")
    try:
        overrides.activate({"max_tokens": "4000"})
        assert ai.config().tuning.max_tokens == 4000
    finally:
        overrides.activate({})
        del os.environ["NAM_MIXER_AI_PROVIDER"], os.environ["NAM_MIXER_AI_LOCAL_MODEL"]


def test_site_model_name_stays_private(monkeypatch):
    monkeypatch.setattr(ai, "config", lambda: ai.AiConfig("cloudflare", "https://x/v1", "@cf/secret-model", "k"))
    assert ai.source() == {"source": "site", "model": ""}


def test_web_search_runs_in_a_killable_process():
    import subprocess
    from types import SimpleNamespace

    def hung(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k["timeout"])
    with pytest.raises(RuntimeError, match="did not finish"):
        research._ddgs_search("marshall", 6, timeout=2, run=hung)
    ok = lambda *a, **k: SimpleNamespace(stdout='{"results": [{"href": "https://a.example", "title": "A"}]}\n', stderr="")
    assert research._ddgs_search("marshall", 6, run=ok) == [{"href": "https://a.example", "title": "A"}]
    missing = lambda *a, **k: SimpleNamespace(stdout='{"missing": true}\n', stderr="")
    with pytest.raises(RuntimeError, match="'ddgs' package"):
        research._ddgs_search("marshall", 6, run=missing)


def test_shortlist_keeps_every_search_in_the_running():
    mesa = [{"id": i, "query": "Mesa Dual Rectifier", "match_score": 90} for i in range(10)]
    bad_cat = [{"id": 100 + i, "query": "Bad Cat Hot Cat 100", "match_score": 40} for i in range(3)]
    screamer = [{"id": 200 + i, "query": "Ibanez Tube Screamer", "match_score": 30} for i in range(8)]
    shortlist = app_module._shortlist(mesa + bad_cat + screamer, "best-match")
    queries = [pack["query"] for pack in shortlist]
    assert len(shortlist) == 12 and queries.count("Bad Cat Hot Cat 100") == 3
    assert queries.count("Mesa Dual Rectifier") == 5 and queries.count("Ibanez Tube Screamer") == 4
    assert shortlist[0]["match_score"] == 90  # still ordered by the chosen sort


def test_evidence_skips_questions_and_sales_copy():
    topic = {"porcupine", "tree", "absentia"}
    assert research._sentence_score("Does anyone know the gear used on Porcupine Tree's In Absentia album?", topic) == 0
    assert research._sentence_score("This recipe adapts Porcupine Tree's amp settings to your guitar and pedals.", topic) == 0
    assert research._sentence_score("Wilson recorded In Absentia with a Mesa Boogie amp and a Strat.", {"absentia"}) > 0


def test_web_notes_take_one_page_per_site():
    pages = [{"href": f"https://tonesite.example/song-{i}", "title": f"Song {i}", "body": ""} for i in range(3)]
    pages.append({"href": "https://forum.example/thread", "title": "Thread", "body": ""})
    fetched = []

    def evidence(href, topic):
        fetched.append(href)
        return "He recorded it with a Marshall amp and a fuzz pedal through the studio cab."

    notes = research.web_notes("SRV tone", search=lambda *a, **k: pages, evidence=evidence)
    assert fetched == ["https://tonesite.example/song-0", "https://forum.example/thread"]
    assert notes.count("\n") == 1
