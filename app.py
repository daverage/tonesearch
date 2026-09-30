"""TONE Search: describe a tone, get an AI tone brief and AI-ranked TONE3000 NAM packs.

Local run:  python app.py  ->  http://127.0.0.1:5090/
cPanel:     passenger_wsgi.py imports `application` from here (see README.md).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from tonesearch import ai, mcp_server, overrides
from tonesearch import research
from tonesearch.research import (tone3000_lookup, tone3000_model_download, tone3000_models, tone3000_pack_zip,
                                 tone3000_search, web_notes)

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TONESEARCH_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
application = app
# Changes whenever a static file is redeployed, so Cloudflare's cache never serves stale JS/CSS.
ASSET_VERSION = str(int(max(f.stat().st_mtime for f in (ROOT / "static").iterdir())))
app.jinja_env.globals["asset_version"] = ASSET_VERSION


# Google AdSense (Auto ads), off unless the site owner sets their publisher ID (ca-pub-…).
# app.js loads the script only for visitors with no saved keys of their own.
ADSENSE_CLIENT = os.environ.get("TONESEARCH_ADSENSE_CLIENT", "").strip()
# With an ad unit's slot ID, the page shows that one banner above the footer instead of Auto ads.
ADSENSE_SLOT = os.environ.get("TONESEARCH_ADSENSE_SLOT", "").strip()


@app.context_processor
def _page_globals():
    return {"current_year": time.gmtime().tm_year, "adsense_client": ADSENSE_CLIENT, "adsense_slot": ADSENSE_SLOT}

# Per-visitor hourly limits: every search spends the site owner's TONE3000 and AI quota.
LIMITS = {
    "search": int(os.environ.get("TONESEARCH_SEARCHES_PER_HOUR", "12")),
    "chat": int(os.environ.get("TONESEARCH_CHATS_PER_HOUR", "40")),
    "files": int(os.environ.get("TONESEARCH_FILE_REQUESTS_PER_HOUR", "120")),
    "lookup": int(os.environ.get("TONESEARCH_LOOKUPS_PER_HOUR", "600")),
    # The hosted MCP endpoint uses the caller's own TONE3000 key; these limits protect this server and the web search.
    "mcp": int(os.environ.get("TONESEARCH_MCP_CALLS_PER_HOUR", "120")),
}
# MCP web research defaults to the website's search limit, so neither runs out before the other.
LIMITS["mcp_research"] = int(os.environ.get("TONESEARCH_MCP_RESEARCH_PER_HOUR", LIMITS["search"]))
# Cloudflare's proxy ends any request after about 100 seconds, so a search or pack question must answer
# before then. The AI calls share this budget; ranking is skipped rather than overrunning it.
REQUEST_BUDGET_SECONDS = float(os.environ.get("TONESEARCH_REQUEST_BUDGET_SECONDS", "85"))
RANKING_RESERVE_SECONDS = 15  # kept back from the plan call so ranking has a chance to run
LOOKUP_CACHE: dict = {}  # (kind, query) -> (time, suggestions): autocomplete must not spend the 100/minute API limit
LOOKUP_CACHE_SECONDS = 3600


def _visitor() -> str:
    # Passenger/Apache put the real client first in X-Forwarded-For.
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() or request.remote_addr or "unknown")[:64]


def _over_limit(bucket: str) -> bool:
    limit = LIMITS[bucket]
    if limit <= 0 or ai.server_is_local():  # local AI means the owner's own machine: nothing to protect
        return False
    now = time.time()
    with sqlite3.connect(DATA_DIR / "limits.sqlite3", timeout=5) as db:
        db.execute("CREATE TABLE IF NOT EXISTS hits (visitor TEXT, bucket TEXT, at REAL)")
        db.execute("DELETE FROM hits WHERE at < ?", (now - 3600,))
        (count,) = db.execute("SELECT COUNT(*) FROM hits WHERE visitor = ? AND bucket = ?", (_visitor(), bucket)).fetchone()
        if count >= limit:
            return True
        db.execute("INSERT INTO hits VALUES (?, ?, ?)", (_visitor(), bucket, now))
    return False


def _brings_own(*settings: str) -> bool:
    """True when the visitor's own keys cover every paid service a route uses."""
    return all(overrides.get(name) for name in settings)


_search_filters = research.validate_filters


def _reused_plan(raw) -> dict | None:
    """A previous tone brief sent back to re-run the catalogue search with new filters, skipping the AI plan."""
    if not isinstance(raw, dict):
        return None
    summary, queries = raw.get("summary"), raw.get("search_queries")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 4_000:
        return None
    if not isinstance(queries, list) or not 0 < len(queries) <= 3 or not all(isinstance(q, str) and 0 < len(q) <= 80 for q in queries):
        return None
    return {"summary": summary, "advice": [], "gear": [], "search_queries": [q.strip() for q in queries]}


def _shortlist(packs: list, sort: str) -> list:
    """The 12 packs the AI ranks. The chosen sort decides which ones make the cut, not just their order."""
    if sort == "downloads-all-time":
        key, reverse = (lambda p: p.get("downloads_count") or 0), True
    elif sort in ("newest", "oldest"):
        key, reverse = (lambda p: p.get("published") or ""), sort == "newest"
    elif sort == "trending":
        key, reverse = (lambda p: p.get("catalog_order", 999)), False
    else:  # best match: word overlap with the searches, then popularity
        key, reverse = (lambda p: (p.get("match_score", 0), p.get("downloads_count") or 0)), True
    return sorted(packs, key=key, reverse=reverse)[:12]


def _attachment(data: bytes, filename: str, mimetype: str) -> Response:
    """A download as a plain response. Not send_file: LiteSpeed's wsgi.file_wrapper fails on in-memory files
    with a server-level 500 that the app never sees."""
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return Response(data, mimetype=mimetype, headers={
        "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}',
        "Content-Length": str(len(data)),
        "Cache-Control": "no-store",
    })


def _limited():
    return jsonify({"error": "You've reached this hour's limit for this site. Try again later, or add your own AI key "
                             "and TONE3000 key in Settings to search without a limit."}), 429


def _valid_history(history) -> bool:
    return isinstance(history, list) and len(history) <= 12 and all(
        isinstance(item, dict) and item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str) and len(item["content"]) <= 4_000
        for item in history
    )


@app.errorhandler(Exception)
def _unexpected_error(exc):
    """API calls always get JSON (never the server's HTML error page); the traceback goes to stderr.log."""
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"error": f"Unexpected server error ({type(exc).__name__}). Details are in the server log."}), 500
    return "Internal server error", 500


@app.before_request
def _apply_visitor_settings():
    # Set on every request so a reused worker thread never keeps a previous visitor's keys.
    overrides.activate(overrides.from_headers(request.headers))


@app.post("/mcp")
def mcp_endpoint():
    """Hosted MCP (streamable HTTP, stateless, JSON replies). The local stdio server has no limits."""
    message = request.get_json(silent=True)
    if not isinstance(message, dict):
        return jsonify(mcp_server._error(None, -32700, "Send one JSON-RPC message as JSON.")), 400
    auth = request.headers.get("Authorization", "")
    # Claude.ai and ChatGPT connectors can only take a URL, so ?key= is accepted when no header is sent.
    key = (auth[7:] if auth.lower().startswith("bearer ")
           else request.headers.get("X-TONE3000-Key") or request.args.get("key", "")).strip()
    if message.get("method") == "tools/call":
        name = (message.get("params") or {}).get("name")
        if _over_limit("mcp") or (name == "web_research" and _over_limit("mcp_research")):
            reply = mcp_server._error(message.get("id"), -32000, (
                "This hour's limit for the hosted TONE Search MCP server has been reached. Try again later, "
                "or run it locally without limits: https://github.com/daverage/tonesearch#use-it-from-your-ai-assistant-mcp"))
            return jsonify(reply), 429
    reply = mcp_server.handle(message, key[:200])
    if reply is None:
        return "", 202
    return jsonify(reply)


@app.get("/mcp")
def mcp_stream():
    return ("This is TONE Search's MCP server for AI assistants, not a web page. Add this URL to your assistant "
            "as a Streamable HTTP MCP server with your own TONE3000 key: "
            "https://github.com/daverage/tonesearch#use-it-from-your-ai-assistant-mcp", 405,
            {"Content-Type": "text/plain; charset=utf-8", "Allow": "POST"})


@app.get("/")
def index():
    local = ai.local_model()
    return render_template("index.html", ai_ready=ai.is_configured(), ai_local=local,
                           searches_per_hour=0 if local else LIMITS["search"])


@app.post("/api/search")
def api_search():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 600:
        return jsonify({"error": "Describe the tone in 600 characters or fewer."}), 400
    use_web = data.get("use_research", False)
    filters = _search_filters(data.get("filters"))
    reuse = data.get("reuse_plan")
    plan = _reused_plan(reuse) if reuse is not None else None
    history = data.get("history", [])
    if not isinstance(use_web, bool) or filters is None or (reuse is not None and plan is None):
        return jsonify({"error": "Invalid search options."}), 400
    if not _valid_history(history):
        return jsonify({"error": "Invalid conversation."}), 400
    if not _brings_own("provider", "api_key", "tone3000_api_key") and _over_limit("search"):
        return _limited()

    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    warnings: list = []
    research = ""
    if use_web and plan is None:
        try:
            research = web_notes(prompt.strip())
        except RuntimeError as exc:
            warnings.append(f"Web research unavailable: {exc}")
    try:
        if plan is None:
            plan = ai.plan_tone(prompt, research_notes=research, history=history,
                                deadline=deadline - RANKING_RESERVE_SECONDS)
    except ai.AiError as exc:
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies

    queries = plan["search_queries"] or [prompt.strip()[:80]]
    rank_query = " ".join(queries)
    packs: list = []
    seen: set = set()
    for query_index, query in enumerate(queries):
        try:
            for match in tone3000_search(query, filters=filters, rank_query=rank_query):
                if match["id"] not in seen:
                    seen.add(match["id"])
                    # Interleave the searches in TONE3000's own order: first of each, then second of each...
                    packs.append({**match, "query": query, "catalog_order": match.get("catalog_rank", 99) * 10 + query_index})
        except RuntimeError as exc:
            if str(exc) not in warnings:
                warnings.append(str(exc))
    # Small models stop scoring partway through long lists: rank a catalogue-ordered shortlist.
    packs = _shortlist(packs, filters["sort"])
    if packs:
        scores = {}
        if deadline - time.monotonic() < 8:
            warnings.append("AI ranking skipped because the search ran out of time; showing catalogue order.")
        else:
            try:
                scores = ai.rank_packs(prompt, plan["summary"], packs, deadline=deadline)
            except ai.AiError as exc:
                warnings.append(f"AI ranking unavailable, showing catalogue order: {exc}")
        for pack in packs:
            if pack["id"] in scores:
                pack["ai_fit"] = scores[pack["id"]]["fit"]
                pack["ai_why"] = scores[pack["id"]]["why"]
        packs.sort(key=lambda p: (p.get("ai_fit", -1), p.get("match_score", 0), p.get("downloads_count") or 0), reverse=True)
    return jsonify({"ai": ai.source(), "plan": plan, "queries": queries, "results": packs, "warnings": warnings, "researched": bool(research), "filters": filters, "reused_plan": reuse is not None,
                    "research_notes": research})


@app.get("/api/packs/<int:tone_id>/models")
def api_models(tone_id: int):
    architecture = request.args.get("architecture", "2")
    if architecture not in research.ARCHITECTURES:
        return jsonify({"error": "Invalid NAM version."}), 400
    if not _brings_own("tone3000_api_key") and _over_limit("files"):
        return _limited()
    try:
        return jsonify({"models": tone3000_models(tone_id, architecture=architecture)})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies


@app.get("/api/packs/<int:tone_id>/models/<int:model_id>/download")
def api_download(tone_id: int, model_id: int):
    architecture = request.args.get("architecture", "2")
    if architecture not in research.ARCHITECTURES:
        return jsonify({"error": "Invalid NAM version."}), 400
    if not _brings_own("tone3000_api_key") and _over_limit("files"):
        return _limited()
    try:
        data, name = tone3000_model_download(tone_id, model_id, architecture=architecture)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies
    filename = secure_filename(name) or f"tone3000-{model_id}"
    if not filename.lower().endswith(".nam"):
        filename += ".nam"
    return _attachment(data, filename, "application/octet-stream")


@app.get("/api/packs/<int:tone_id>/download")
def api_pack_download(tone_id: int):
    if not _brings_own("tone3000_api_key") and _over_limit("files"):
        return _limited()
    architecture = request.args.get("architecture", "any")
    if architecture not in research.ARCHITECTURES:
        return jsonify({"error": "Invalid NAM version."}), 400
    try:
        data, _count = tone3000_pack_zip(tone_id, architecture=architecture)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies
    return _attachment(data, f"tone3000-pack-{tone_id}.zip", "application/zip")


@app.get("/api/lookup/<kind>")
def api_lookup(kind: str):
    query = request.args.get("query", "").strip()
    if kind not in research.LOOKUP_KINDS or not 2 <= len(query) <= 40:
        return jsonify({"error": "Type at least 2 characters."}), 400
    key = (kind, query.lower(), bool(overrides.get("tone3000_api_key")))
    cached = LOOKUP_CACHE.get(key)
    if cached and time.time() - cached[0] < LOOKUP_CACHE_SECONDS:
        return jsonify({"suggestions": cached[1]})
    if not _brings_own("tone3000_api_key") and _over_limit("lookup"):
        return _limited()
    try:
        suggestions = tone3000_lookup(kind, query)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies
    if len(LOOKUP_CACHE) > 2_000:
        LOOKUP_CACHE.clear()
    LOOKUP_CACHE[key] = (time.time(), suggestions)
    return jsonify({"suggestions": suggestions})


@app.post("/api/pack_chat")
def api_pack_chat():
    data = request.get_json(silent=True) or {}
    tone_id = data.get("tone_id")
    question = data.get("question")
    pack = data.get("pack") or {}
    tone_goal = data.get("tone_goal", "")
    history = data.get("history", [])
    architecture = data.get("architecture", "2")
    if architecture not in research.ARCHITECTURES:
        return jsonify({"error": "Invalid NAM version."}), 400
    if not isinstance(tone_id, int) or not isinstance(question, str) or not question.strip() or len(question) > 600:
        return jsonify({"error": "Ask a question of 600 characters or fewer."}), 400
    if not isinstance(tone_goal, str) or len(tone_goal) > 600 or not _valid_history(history) or not isinstance(pack, dict):
        return jsonify({"error": "Invalid conversation."}), 400
    if not _brings_own("provider", "api_key", "tone3000_api_key") and _over_limit("chat"):
        return _limited()
    safe_pack = {
        "title": str(pack.get("title") or "")[:200],
        "creator": str(pack.get("creator") or "")[:100],
        "description": str(pack.get("description") or "")[:600],
        "tags": [str(t)[:40] for t in (pack.get("tags") or [])[:12]] if isinstance(pack.get("tags"), list) else [],
    }
    try:
        models = tone3000_models(tone_id, architecture=architecture)
        answer = ai.ask_about_pack(question, safe_pack, [m["name"] for m in models], tone_goal=tone_goal, history=history,
                                   deadline=time.monotonic() + REQUEST_BUDGET_SECONDS)
    except RuntimeError as exc:  # AiError is a RuntimeError too
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies
    return jsonify({**answer, "models": models, "ai": ai.source()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5090")), debug=os.environ.get("FLASK_DEBUG") == "1")
