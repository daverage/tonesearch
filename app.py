"""TONE Search: describe a tone, get an AI tone brief and AI-ranked TONE3000 NAM packs.

Local run:  python app.py  ->  http://127.0.0.1:5090/
cPanel:     passenger_wsgi.py imports `application` from here (see README.md).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import time

from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from flask import Flask, Response, jsonify, redirect, render_template, request
from werkzeug.utils import secure_filename

from tonesearch import ai, cache, feedback, knowledge, mcp_server, overrides
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
app.jinja_env.globals["confidence_of"] = ai.confidence_of  # older entries say confirmed/artist/suggested
app.jinja_env.filters["datetime"] = lambda at: time.strftime("%d %b %Y %H:%M", time.gmtime(at)) + " UTC"


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
    "flag": 20,  # "wrong research" reports
    "feedback": 60,  # brief and pack ratings
}
# MCP web research defaults to the website's search limit, so neither runs out before the other.
LIMITS["mcp_research"] = int(os.environ.get("TONESEARCH_MCP_RESEARCH_PER_HOUR", LIMITS["search"]))
# Cloudflare's proxy ends any request after about 100 seconds, so a search or pack question must answer
# before then. The AI calls share this budget; ranking is skipped rather than overrunning it.
REQUEST_BUDGET_SECONDS = float(os.environ.get("TONESEARCH_REQUEST_BUDGET_SECONDS", "85"))
RANKING_RESERVE_SECONDS = 15  # kept back from the plan call so ranking has a chance to run
LIBRARY_ADMIN_PASSWORD = os.environ.get("TONESEARCH_ADMIN_PASSWORD", "")  # the research library's review page; off when unset
LOOKUP_CACHE: dict = {}  # (kind, query) -> (time, suggestions): autocomplete must not spend the 100/minute API limit
LOOKUP_CACHE_SECONDS = 3600


def _library() -> Path:
    return DATA_DIR / "knowledge.sqlite3"


mcp_server.library_path = _library  # the hosted MCP endpoint shares the website's research library


def _visitor() -> str:
    # Behind Cloudflare, CF-Connecting-IP is the real client. The first X-Forwarded-For entry is whatever the
    # client sent (Cloudflare appends to it), so trusting it would let anyone pick a new identity per request.
    # Without Cloudflare, the last entry is the one Apache/Passenger added.
    cloudflare = request.headers.get("CF-Connecting-IP", "").strip()
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
    return (cloudflare or forwarded or request.remote_addr or "unknown")[:64]


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
    # Take turns between the searches so one amp's packs can't fill every place: the brief lists
    # each piece of gear for a reason, and the AI can only rank what it's shown.
    groups: dict = {}
    for pack in sorted(packs, key=key, reverse=reverse):
        groups.setdefault(pack.get("query"), []).append(pack)
    shortlist: list = []
    while len(shortlist) < 12 and any(groups.values()):
        for group in groups.values():
            if group and len(shortlist) < 12:
                shortlist.append(group.pop(0))
    return sorted(shortlist, key=key, reverse=reverse)


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
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
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


# ---- Research library review (owner only) ----------------------------------------------------------------------

def _admin_denied():
    """None when the request carries the owner's password; otherwise the response to send instead."""
    if not LIBRARY_ADMIN_PASSWORD:
        return "Not found", 404  # the review page is off until TONESEARCH_ADMIN_PASSWORD is set
    auth = request.authorization
    given = (auth.password or "") if auth else ""
    if not hmac.compare_digest(given.encode(), LIBRARY_ADMIN_PASSWORD.encode()):
        return Response("Sign in to review the research library.", 401,
                        {"WWW-Authenticate": 'Basic realm="TONE Search admin", charset="UTF-8"'})
    if request.method == "POST":
        # Browsers resend Basic credentials to any site's form posts, so only accept this site's own forms.
        source = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if urlparse(source).netloc != request.host:
            return "Forbidden", 403
    return None


# The Activity view's rows: (event, label). Pairs show what was skipped against what had to be done.
ACTIVITY_EVENTS = (
    ("answer_saved", "Searches answered from a saved answer (no AI, web or TONE3000)"),
    ("answer_saved_alias", "…found under another name (aliases)"),
    ("answer_worked_out", "Searches worked out fresh"),
    ("research_library", "Research taken from the library"),
    ("research_web", "Research searched on the web"),
    ("plan_saved", "Tone plans reused"),
    ("plan_ai", "Tone plans asked of the AI"),
    ("rank_saved", "Rankings reused"),
    ("rank_ai", "Rankings asked of the AI"),
    ("catalogue_saved", "TONE3000 searches answered from the cache"),
    ("catalogue_fetched", "TONE3000 searches sent to TONE3000"),
    ("mcp_answer_saved", "MCP: saved answers returned"),
    ("mcp_research_library", "MCP: research taken from the library"),
    ("mcp_research_web", "MCP: research searched on the web"),
)


def _parse_gear(text: str) -> list:
    """Admin gear lines, "kind | name | role | confidence"; a line without a confidence level counts as "close"."""
    gear = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 2 and parts[1]:
            level = "close"
            if len(parts) >= 4 and parts[-1].lower() in ai._CONFIDENCE_SYNONYMS:
                level = ai.confidence_of(parts.pop())
            gear.append({"kind": parts[0][:20] or "other", "name": parts[1][:80], "role": " | ".join(parts[2:])[:240],
                         "confidence": level})
    return gear


_NOTE = re.compile(r"^- (.*?): (.*) \((https?://[^\s)]+)\)$")


def _sources(notes: str) -> list[dict]:
    """Research notes as sources for the admin page: title, text, link, and whether only a search snippet."""
    sources = []
    for line in (notes or "").splitlines():
        if not line.strip():
            continue
        match = _NOTE.match(line.strip())
        title, text, url = match.groups() if match else ("", line.strip().removeprefix("- "), "")
        snippet = text.startswith("Search snippet only: ")
        sources.append({"title": title, "text": text.removeprefix("Search snippet only: "), "url": url,
                        "snippet": snippet})
    return sources


app.jinja_env.filters["sources"] = _sources


def _entry_links(db, entries: list) -> dict:
    """What each library entry's research reaches: the topics of searches that used it (its own words, saved
    answers built on it under any name, votes on it), how many saved answers, and its votes."""
    answers = cache.answers_by_entry(db)
    try:
        votes = feedback.all_votes(db)
    except sqlite3.Error:
        votes = []
    links = {}
    for entry in entries:
        topics = {entry["words"]} | answers.get(entry["id"], set()) | {v["words"] for v in votes
                                                                      if v["entry_id"] == entry["id"]}
        mine = [v for v in votes if v["entry_id"] == entry["id"] or v["words"] in topics]
        links[entry["id"]] = {"topics": sorted(t for t in topics if t), "answers": len(answers.get(entry["id"], ())),
                              "good": sum(v["vote"] == 1 for v in mine), "bad": sum(v["vote"] == -1 for v in mine)}
    return links


def _delete_entry(db, entry_id: int) -> str:
    """Delete a research entry and everything built on it: saved answers, plans and rankings for every topic that
    used it, and the votes on those topics and on the entry itself."""
    entry = knowledge.get(db, entry_id)
    if not entry:
        return f"Research #{entry_id} was already deleted."
    link = _entry_links(db, [entry])[entry_id]
    saved = sum(cache.forget_topic(db, words) for words in link["topics"])
    try:
        votes = feedback.forget(db, words=link["topics"], entry_id=entry_id)
    except sqlite3.Error:
        votes = 0
    knowledge.delete(db, entry_id)
    return (f"Deleted research #{entry_id} ({entry['topic']}), {saved} saved answer{'s' if saved != 1 else ''} and "
            f"plan{'s' if saved != 1 else ''}, and {votes} vote{'s' if votes != 1 else ''}.")


@app.get("/admin")
def admin():
    if denied := _admin_denied():
        return denied
    q, status = request.args.get("q", "")[:100], request.args.get("status", "")
    if request.args.get("view") == "activity":
        return render_template("admin.html", view="activity", activity=cache.activity(_library()),
                               events=ACTIVITY_EVENTS, q="", message="")
    if request.args.get("view") == "feedback":
        vote = {"good": 1, "bad": -1}.get(request.args.get("vote", ""))
        return render_template("admin.html", view="feedback", q=q, vote=request.args.get("vote", ""),
                               votes=feedback.recent(_library(), vote=vote, text=q),
                               topics=feedback.topic_summary(_library()), message=request.args.get("message", "")[:200])
    every = knowledge.search(_library(), limit=100_000)
    counts = {"all": len(every), **{name: sum(e["status"] == name for e in every) for name in knowledge.STATUSES}}
    entries = knowledge.search(_library(), q, status)
    return render_template("admin.html", view="library", entries=entries, links=_entry_links(_library(), entries),
                           q=q, status=status, statuses=knowledge.STATUSES, counts=counts,
                           days=knowledge.UNREVIEWED_DAYS, message=request.args.get("message", "")[:300])


@app.post("/admin/entries/<int:entry_id>")
def admin_entry(entry_id: int):
    if denied := _admin_denied():
        return denied
    form = request.form
    action = form.get("action", "save")
    before = knowledge.get(_library(), entry_id)
    if action == "delete":
        message = _delete_entry(_library(), entry_id)
    elif not before:
        message = f"Research #{entry_id} no longer exists."
    elif action == "approve_only":  # one click from the list: keep everything, change only the status
        knowledge.update(_library(), entry_id, topic=before["topic"], notes=before["notes"], gear=before["gear"],
                         aliases=(before.get("aliases") or "").split(","), status="approved")
        message = f"Approved research #{entry_id}."
    else:
        cache.forget_topic(_library(), before["words"])  # saved plans and results were built on the old research
        try:
            knowledge.update(_library(), entry_id, topic=form.get("topic", ""), notes=form.get("notes", ""),
                             gear=_parse_gear(form.get("gear", "")), aliases=form.get("aliases", "")[:600].split(","),
                             status="approved" if action == "approve" else form.get("status", "new"))
            message = f"Saved research #{entry_id}{' and approved it' if action == 'approve' else ''}."
        except ValueError as exc:
            message = f"Research #{entry_id} not saved: {exc}"
    # Back to the same filtered list, so reviewing (say) every flagged entry doesn't mean filtering again each time.
    keep = {"q": form.get("filter_q", "")[:100], "status": form.get("filter_status", "")}
    keep = {name: value for name, value in keep.items() if value}
    return redirect(f"{request.script_root}/admin?{urlencode({**keep, 'message': message})}", 303)


@app.post("/admin/votes/<int:vote_id>/delete")
def admin_vote_delete(vote_id: int):
    if denied := _admin_denied():
        return denied
    deleted = feedback.delete_vote(_library(), vote_id)
    message = "Deleted the vote." if deleted else "That vote was already deleted."
    keep = {"view": "feedback", "q": request.form.get("filter_q", "")[:100], "vote": request.form.get("filter_vote", "")}
    keep = {name: value for name, value in keep.items() if value}
    return redirect(f"{request.script_root}/admin?{urlencode({**keep, 'message': message})}", 303)


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
    fresh = data.get("fresh", False)  # "Search again": work it out anew rather than reuse saved answers
    filters = _search_filters(data.get("filters"))
    reuse = data.get("reuse_plan")
    plan = _reused_plan(reuse) if reuse is not None else None
    history = data.get("history", [])
    if not isinstance(use_web, bool) or not isinstance(fresh, bool) or filters is None or (reuse is not None and plan is None):
        return jsonify({"error": "Invalid search options."}), 400
    if not _valid_history(history):
        return jsonify({"error": "Invalid conversation."}), 400

    # The topic names what was asked for across a conversation; feedback and saved answers are filed under it.
    first = next((item["content"] for item in history if item["role"] == "user"), "")
    topic = f"{first} {prompt}".strip()[:600] if first else prompt.strip()
    words = " ".join(knowledge.topic_words(topic))
    # Only a first search reads or writes saved answers: a refinement depends on the whole conversation.
    reusable = bool(words) and not history and reuse is None
    result_key = f"result:{words}:{use_web}:{json.dumps(filters, sort_keys=True)}"
    if reusable and not fresh:
        saved = cache.get(_library(), result_key, cache.CATALOGUE_SECONDS)  # holds TONE3000 results
        how = "answer_saved"
        if not saved:
            saved, how = _saved_answer_by_alias(prompt, use_web, filters), "answer_saved_alias"
        if saved:
            cache.count(_library(), how)
            feedback.apply_votes(saved["results"], feedback.pack_votes(_library(), words))
            return jsonify({**saved, "cached": True})  # no AI, web or TONE3000 calls, so no hourly limit spent

    if not _brings_own("provider", "api_key", "tone3000_api_key") and _over_limit("search"):
        return _limited()

    cache.count(_library(), "answer_worked_out")
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
    warnings: list = []
    research = ""
    library = None  # the research library entry this search used or created
    fresh_research = False
    if use_web and plan is None:
        library = knowledge.find(_library(), topic if history else prompt)
        if library:
            research = library["notes"]
            cache.count(_library(), "research_library")
        else:
            try:
                cache.count(_library(), "research_web")
                research = web_notes(prompt.strip())
                fresh_research = True
            except RuntimeError as exc:
                warnings.append(f"Web research unavailable: {exc}")
    plan_key = f"plan:{words}:{use_web}"
    if plan is None and reusable and not fresh and not fresh_research:
        plan = cache.get(_library(), plan_key, cache.AI_SECONDS)
        if plan:
            cache.count(_library(), "plan_saved")
    try:
        if plan is None:
            cache.count(_library(), "plan_ai")
            plan = ai.plan_tone(prompt, research_notes=research, history=history,
                                deadline=deadline - RANKING_RESERVE_SECONDS)
            if reusable:
                cache.put(_library(), plan_key, plan, words)
    except ai.AiError as exc:
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies

    if fresh_research:
        entry_id = knowledge.save(_library(), prompt, research, plan.get("gear"), plan.get("aliases"))
        library = knowledge.get(_library(), entry_id) if entry_id else None  # a reported entry stays flagged
    elif library:
        if not library["gear"]:
            knowledge.add_gear(_library(), library["id"], plan.get("gear") or [])
        if not library.get("aliases"):
            knowledge.add_aliases(_library(), library["id"], plan.get("aliases") or [])

    queries = plan["search_queries"] or [prompt.strip()[:80]]
    rank_query = " ".join(queries)
    packs: list = []
    seen: set = set()
    for query_index, query in enumerate(queries):
        try:
            for match in tone3000_search(query, filters=filters, rank_query=rank_query, cache_db=_library()):
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
        rank_key = f"rank:{words}:{use_web}:{','.join(str(p['id']) for p in sorted(packs, key=lambda p: p['id']))}"
        scores = cache.get(_library(), rank_key, cache.AI_SECONDS) if reusable and not fresh else None
        if scores is not None:
            cache.count(_library(), "rank_saved")
        if scores is None:
            scores = {}
            if deadline - time.monotonic() < 8:
                warnings.append("AI ranking skipped because the search ran out of time; showing catalogue order.")
            else:
                try:
                    cache.count(_library(), "rank_ai")
                    scores = ai.rank_packs(prompt, ai.gear_summary(plan), packs, deadline=deadline)
                    if reusable:
                        cache.put(_library(), rank_key, {str(k): v for k, v in scores.items()}, words)
                except ai.AiError as exc:
                    warnings.append(f"AI ranking unavailable, showing catalogue order: {exc}")
        scores = {int(k): v for k, v in scores.items()}  # JSON keys come back as strings
        for pack in packs:
            if pack["id"] in scores:
                pack["ai_fit"] = scores[pack["id"]]["fit"]
                pack["ai_why"] = scores[pack["id"]]["why"]
        packs.sort(key=lambda p: (p.get("ai_fit", -1), p.get("match_score", 0), p.get("downloads_count") or 0), reverse=True)
    body = {"ai": ai.source(), "plan": plan, "queries": queries, "results": packs, "warnings": warnings,
            "researched": bool(research), "filters": filters, "reused_plan": reuse is not None,
            "research_notes": research, "topic": topic, "saved_at": time.time(), "use_web": use_web,
            "library": {"id": library["id"], "status": library["status"], "reused": not fresh_research}
            if library else None}
    if reusable and packs and not warnings:  # never save a partial answer
        cache.put(_library(), result_key, {**body, "library": body["library"] and {**body["library"], "reused": True}},
                  words)
    feedback.apply_votes(packs, feedback.pack_votes(_library(), words))
    return jsonify({**body, "cached": False})


def _saved_answer_by_alias(prompt: str, use_web: bool, filters: dict) -> dict | None:
    """A saved answer for the same rig under another name ("Nolly Getgood bass" for "Periphery bass"), with the
    same filters and research setting, and no sound the saved one didn't describe."""
    best, best_score = None, 0.0
    for words, answer, _created in cache.by_prefix(_library(), "result:", cache.CATALOGUE_SECONDS):
        if answer.get("use_web") != use_web or answer.get("filters") != filters:
            continue
        aliases = ", ".join(knowledge.clean_aliases((answer.get("plan") or {}).get("aliases")))
        score = knowledge.match_score(prompt, words, aliases, same_sound=True)
        if score > best_score:  # rows come newest first, so ties keep the newest
            best, best_score = answer, score
    return best


@app.post("/api/feedback")
def api_feedback():
    """A visitor rates a tone brief or a pack. A bad brief also reports its research and drops saved answers."""
    data = request.get_json(silent=True) or {}
    topic, target, vote = data.get("topic"), data.get("target"), data.get("vote")
    comment, pack_id, pack_title = data.get("comment", ""), data.get("pack_id", 0), data.get("pack_title", "")
    library_id = data.get("library_id")
    if (not isinstance(topic, str) or not 0 < len(topic) <= 600 or target not in feedback.TARGETS
            or vote not in (1, -1) or not isinstance(comment, str) or len(comment) > 500
            or not isinstance(pack_id, int) or not isinstance(pack_title, str)
            or (target == "pack" and pack_id <= 0) or not (library_id is None or isinstance(library_id, int))):
        return jsonify({"error": "Invalid feedback."}), 400
    words = " ".join(knowledge.topic_words(topic))
    if not words:
        return jsonify({"error": "Invalid feedback."}), 400
    if _over_limit("feedback"):
        return _limited()
    try:
        feedback.record(_library(), voter_id=feedback.voter(_visitor()), words=words, prompt=topic, target=target,
                        vote=vote, pack_id=pack_id, pack_title=pack_title, comment=comment, entry_id=library_id)
    except sqlite3.Error:
        app.logger.exception("Feedback not saved")
        return jsonify({"error": "Feedback couldn't be saved just now."}), 503
    if target == "brief" and vote == -1:
        if library_id:
            knowledge.flag(_library(), library_id, comment or "Visitor rated the tone brief as wrong.")
        cache.forget_topic(_library(), words)
    return jsonify({"ok": True})


@app.post("/api/library/<int:entry_id>/flag")
def api_library_flag(entry_id: int):
    """A visitor reports wrong research: the entry stops being reused until the owner reviews it."""
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 300:
        return jsonify({"error": "Keep the reason under 300 characters."}), 400
    if _over_limit("flag"):
        return _limited()
    if not knowledge.flag(_library(), entry_id, reason):
        return jsonify({"error": "That research is no longer in the library."}), 404
    return jsonify({"ok": True})


@app.get("/api/packs/<int:tone_id>/models")
def api_models(tone_id: int):
    architecture = request.args.get("architecture", "2")
    if architecture not in research.ARCHITECTURES:
        return jsonify({"error": "Invalid NAM version."}), 400
    if not _brings_own("tone3000_api_key") and _over_limit("files"):
        return _limited()
    try:
        return jsonify({"models": tone3000_models(tone_id, architecture=architecture, cache_db=_library())})
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
        suggestions = tone3000_lookup(kind, query, cache_db=_library())
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
        models = tone3000_models(tone_id, architecture=architecture, cache_db=_library())
        answer = ai.ask_about_pack(question, safe_pack, [m["name"] for m in models], tone_goal=tone_goal, history=history,
                                   deadline=time.monotonic() + REQUEST_BUDGET_SECONDS)
    except RuntimeError as exc:  # AiError is a RuntimeError too
        return jsonify({"error": str(exc)}), 503  # not 502: Cloudflare replaces 502 bodies
    return jsonify({**answer, "models": models, "ai": ai.source()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5090")), debug=os.environ.get("FLASK_DEBUG") == "1")
