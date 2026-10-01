"""TONE Search as an MCP server: the client's own AI plans and ranks; this server only fetches data.

No AI provider is called here. Every TONE3000 call uses the user's own secret key, which is mandatory:
TONE3000_API_KEY for the local stdio server (`python -m tonesearch.mcp_server`), or an
`Authorization: Bearer t3k_cs_…` header on the hosted endpoint (`POST /mcp` in app.py).
`handle` answers one JSON-RPC message for either transport, so no MCP SDK is needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from tonesearch import ai, cache, feedback, knowledge, overrides, research

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = """TONE Search finds TONE3000 NAM (Neural Amp Modeler) capture packs for a guitar tone.
You do the thinking; these tools fetch data. Workflow:
1. Always call web_research first with the user's description, even if you know the gear: when TONE Search
   already has an answer for this tone it returns it instantly (brief, gear and packs ranked by players' votes).
   Otherwise it researches the actual rig (amp, pedals, cab) on the web. Base your queries on what it finds.
   If it returned fresh research notes, call save_gear with the products those notes say the artist used.
   If the notes are poor or off-topic, call rate_result with rating 'bad' instead.
2. Plan 1-3 short catalogue queries naming gear, e.g. "Marshall Plexi" or "Klon Centaur", not song titles.
   Use lookup to find exact make, tag or creator slugs before using them as filters.
3. Call search_packs for each query and merge the results by id.
4. Rank the packs against the user's tone yourself and explain the fit briefly.
5. For a chosen pack, list_pack_models shows its files; download_link gives the pack's TONE3000 page to download it from.
6. When the user says whether a result was right or wrong, call rate_result: it improves results for everyone."""
NO_KEY = ("TONE Search needs your own TONE3000 secret key (t3k_cs_…) from https://www.tone3000.com: "
          "set TONE3000_API_KEY locally, or send 'Authorization: Bearer t3k_cs_…' to the hosted endpoint.")


class ToolError(RuntimeError):
    """A tool failed in a way the client's AI should read and act on."""


def library_path() -> Path:
    """The research library; app.py points this at the website's own, so hosted MCP shares it."""
    folder = Path(os.environ.get("TONESEARCH_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "knowledge.sqlite3"


def _gear_line(g: dict) -> str:
    return f"{g.get('kind', '')}: {g.get('name', '')} ({ai.confidence_of(g.get('confidence'))})"


def _saved_answer(description: str) -> str:
    """The website's saved answer for the same rig: its brief and ranked packs, with players' votes."""
    best, best_score = None, 0.0
    for words, answer, created in cache.by_prefix(library_path(), "result:", cache.CATALOGUE_SECONDS):
        aliases = ", ".join(knowledge.clean_aliases((answer.get("plan") or {}).get("aliases")))
        score = knowledge.match_score(description, words, aliases, same_sound=True)
        if score > best_score:  # rows come newest first, so ties keep the newest
            best, best_score = (words, answer, created), score
    if best is None:
        return ""
    words, answer, created = best
    plan, packs = answer.get("plan") or {}, answer.get("results") or []
    feedback.apply_votes(packs, feedback.pack_votes(library_path(), words))
    entry = knowledge.find(library_path(), description)
    checked = bool(entry and entry["status"] == "approved")
    lines = [f"TONE Search's saved answer for \"{answer.get('topic', words)}\" "
             f"({time.strftime('%d %b %Y', time.gmtime(created))}; packs ranked by the site's AI, then players' votes):",
             f"Summary: {plan.get('summary', '')}"]
    gear = "; ".join(_gear_line(g) for g in plan.get("gear") or [])
    if gear:
        lines.append(f"Gear (confidence: confirmed for this recording / artist's gear, era unconfirmed / suggested): {gear}")
    if answer.get("queries"):
        lines.append("Catalogue searches used: " + ", ".join(answer["queries"]))
    lines.append("Ranked packs:")
    for pack in packs[:12]:
        fit = f"{pack['ai_fit']}% fit" if "ai_fit" in pack else "unranked"
        votes = f", {pack['votes']:+d} player votes" if pack.get("votes") else ""
        lines.append(f"- [{pack['id']}] {pack.get('title', '')} by {pack.get('creator', '')}: {fit}{votes}. "
                     f"{pack.get('ai_why', '')} {pack.get('url') or download_link(pack['id'])}")
    lines.append("The site owner has checked this research." if checked else
                 "Not yet checked by the site owner. If its gear isn't what this artist actually used, tell the user, "
                 "call rate_result with rating 'bad' and a short comment (that clears it for everyone), and call "
                 "web_research again for fresh research.")
    return "\n".join(lines)


def web_research(description: str, *, notes=research.web_notes) -> str:
    description = description.strip()
    if not 0 < len(description) <= 500:
        raise ToolError("Describe the tone in 1-500 characters.")
    saved = _saved_answer(description)
    entry = knowledge.find(library_path(), description)
    if saved:  # instant: the gear, searches and ranking are already worked out
        return saved + (f"\n\nResearch notes:\n{entry['notes']}" if entry else "")
    if entry:
        label = "reviewed by the site owner" if entry["status"] == "approved" else "not yet reviewed"
        gear = "; ".join(_gear_line(g) for g in entry["gear"])
        return (f"From TONE Search's research library ({label}).\n{entry['notes']}"
                + (f"\nGear found earlier: {gear}" if gear else ""))
    found = notes(description)
    knowledge.save(library_path(), description, found)
    return found


def search_packs(query: str, gears=None, sizes=None, makes=None, tags=None, creators=None, format: str = "nam",
                 architecture: str = "2", sort: str = "best-match", calibrated: bool = False, verified: bool = False,
                 *, opener=research.urlopen) -> list[dict]:
    query = query.strip()
    if not 0 < len(query) <= 80:
        raise ToolError("The query must be 1-80 characters.")
    filters = research.validate_filters({
        "gears": gears or [], "sizes": sizes or [], "makes": makes or [], "tags": tags or [],
        "creators": creators or [], "format": format, "architecture": architecture, "sort": sort,
        "calibrated": calibrated, "verified": verified,
    })
    if filters is None:
        raise ToolError(f"Invalid filters. gears: {', '.join(research.GEARS)}; sizes: {', '.join(research.SIZES)}; "
                        f"format: {', '.join(research.FORMATS)}; architecture: {', '.join(research.ARCHITECTURES)}; "
                        f"sort: {', '.join(research.SORTS)}. Lists hold up to 10 values without '_' or ','.")
    return research.tone3000_search(query, filters=filters, opener=opener, cache_db=library_path())


def lookup(kind: str, text: str, *, opener=research.urlopen) -> list[dict]:
    if kind not in research.LOOKUP_KINDS:
        raise ToolError("kind must be makes, tags or creators.")
    if not 0 < len(text.strip()) <= 60:
        raise ToolError("The lookup text must be 1-60 characters.")
    return research.tone3000_lookup(kind, text.strip(), opener=opener, cache_db=library_path())


def list_pack_models(pack_id: int, architecture: str = "2", *, opener=research.urlopen) -> list[dict]:
    if architecture not in research.ARCHITECTURES:
        raise ToolError("architecture must be 1, 2 or any.")
    return research.tone3000_models(pack_id, architecture=architecture, opener=opener, cache_db=library_path())


def download_link(pack_id: int) -> str:
    return f"https://www.tone3000.com/tones/{pack_id}"


GEAR_KINDS = ("amp", "effect", "guitar", "pickup", "cab", "other")


def save_gear(description: str, gear: list, aliases: list | None = None) -> str:
    """Store the gear the assistant worked out from web_research, as the website's AI does for its searches.

    Only fills an empty gear list: never replaces gear the owner edited or the website's AI wrote."""
    if not isinstance(gear, list) or not 0 < len(gear) <= 12:
        raise ToolError("gear must list 1-12 items.")
    items = []
    for item in gear:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ToolError("Each gear item needs kind, name and role.")
        name = str(item["name"]).strip()[:80]
        if ai._GENERIC_GEAR.match(name):
            continue  # "Compressor" is a category, not a product
        kind = item.get("kind") if item.get("kind") in GEAR_KINDS else "other"
        items.append({"kind": kind, "name": name, "role": str(item.get("role") or "").strip()[:240],
                      "confidence": ai.confidence_of(item.get("confidence"))})
    if not items:
        raise ToolError("Name real products (e.g. 'Darkglass Microtubes B7K'), not categories like 'Compressor'.")
    entry = knowledge.find(library_path(), description, count_use=False)
    if not entry:
        return "There's no saved research for this tone yet: call web_research first."
    if aliases and not entry.get("aliases"):
        knowledge.add_aliases(library_path(), entry["id"], [str(a)[:60] for a in aliases[:8]])
    if entry["gear"]:
        return "This research already has a gear list, so it was left as it is."
    # Only gear the research itself names: the library records what sources say, not what an assistant recalls.
    notes = entry["notes"].lower()
    supported = [g for g in items if _named_in(g["name"], notes)]
    skipped = [g["name"] for g in items if g not in supported]
    if not supported:
        return ("Nothing saved: none of these appear in the research notes. Only save gear the notes name. "
                "If the research is poor, call rate_result with rating 'bad' instead.")
    knowledge.add_gear(library_path(), entry["id"], supported)
    return (f"Saved {len(supported)} gear item{'s' if len(supported) != 1 else ''} with the research."
            + (f" Not saved, because the notes don't name them: {', '.join(skipped)}." if skipped else ""))


def _named_in(name: str, notes: str) -> bool:
    """True when the notes mention the product: its model word (B7K, Cali76) or two of its other words."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2 or any(c.isdigit() for c in w)]
    found = [w for w in words if re.search(rf"\b{re.escape(w)}\b", notes)]
    return any(any(c.isdigit() for c in w) for w in found) or len(found) >= min(2, len(words))


def rate_result(description: str, rating: str, pack_id: int = 0, pack_title: str = "", comment: str = "", *,
                voter_id: str = "") -> str:
    """Save the user's verdict. A bad brief stops its saved research being reused until the owner checks it."""
    words = " ".join(knowledge.topic_words(description))
    if not words or len(description) > 500:
        raise ToolError("Give the tone the user asked for (1-500 characters).")
    if rating not in ("good", "bad") or len(comment) > 500 or pack_id < 0:
        raise ToolError("rating must be good or bad; comment up to 500 characters.")
    vote, target = (1 if rating == "good" else -1), ("pack" if pack_id else "brief")
    entry = knowledge.find(library_path(), description) if target == "brief" else None
    feedback.record(library_path(), voter_id=voter_id, words=words, prompt=description, target=target, vote=vote,
                    pack_id=pack_id, pack_title=pack_title, comment=comment, entry_id=entry and entry["id"],
                    source="mcp")
    if target == "brief" and vote == -1:
        if entry:
            knowledge.flag(library_path(), entry["id"], comment or "An MCP user rated the research as wrong.")
        cache.forget_topic(library_path(), words)
    return "Thanks, your feedback was saved."


def _strings(description):
    return {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": description}


TOOLS = {
    "web_research": (web_research, "Call this first for any tone request, even if you know the gear. Returns "
                                   "TONE Search's saved answer for the tone when one exists (instant: brief, gear and "
                                   "ranked packs with players' votes); otherwise researches the gear behind a guitar "
                                   "or bass tone (artist, song, era) on the web, returning short cited notes, which "
                                   "can take up to about 30 seconds.", {
        "description": {"type": "string", "description": "The tone, e.g. 'Gilmour's Comfortably Numb solo'"},
    }, ["description"]),
    "search_packs": (search_packs, "Search TONE3000 for NAM capture packs with one short gear query, e.g. 'Vox AC30'. "
                                   "Call web_research first: it may already have ranked packs for this tone. "
                                   "Returns up to 8 packs with a metadata match score.", {
        "query": {"type": "string", "description": "A short gear query, 1-80 characters"},
        "gears": {**_strings("Gear types"), "items": {"type": "string", "enum": list(research.GEARS)}},
        "sizes": {**_strings("Model sizes"), "items": {"type": "string", "enum": list(research.SIZES)}},
        "makes": _strings("Make slugs from lookup"),
        "tags": _strings("Tag slugs from lookup"),
        "creators": _strings("Creator usernames from lookup"),
        "format": {"type": "string", "enum": list(research.FORMATS), "default": "nam"},
        "architecture": {"type": "string", "enum": list(research.ARCHITECTURES), "default": "2",
                         "description": "NAM architecture version"},
        "sort": {"type": "string", "enum": list(research.SORTS), "default": "best-match"},
        "calibrated": {"type": "boolean", "default": False},
        "verified": {"type": "boolean", "default": False},
    }, ["query"]),
    "lookup": (lookup, "Find exact TONE3000 slugs for the makes, tags or creators filters.", {
        "kind": {"type": "string", "enum": list(research.LOOKUP_KINDS)},
        "text": {"type": "string", "description": "What to look up, e.g. 'mesa'"},
    }, ["kind", "text"]),
    "list_pack_models": (list_pack_models, "List the model files in a TONE3000 pack. File names often show the gain, "
                                           "channel and mic.", {
        "pack_id": {"type": "integer"},
        "architecture": {"type": "string", "enum": list(research.ARCHITECTURES), "default": "2"},
    }, ["pack_id"]),
    "download_link": (download_link, "Get the pack's TONE3000 page, where it can be downloaded.", {
        "pack_id": {"type": "integer"},
    }, ["pack_id"]),
}
TOOLS["save_gear"] = (save_gear, "After web_research returned fresh research notes (not a saved answer), save the "
                                "specific products the notes say the artist used, so later searches get them too. "
                                "Only gear the notes name is saved: not gear you know from elsewhere.", {
    "description": {"type": "string", "description": "The tone, exactly as passed to web_research"},
    "gear": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(GEAR_KINDS)},
        "name": {"type": "string", "description": "Make and model, e.g. 'Darkglass Microtubes B7K'"},
        "role": {"type": "string", "description": "What it does in this tone"},
        "confidence": {"type": "string", "enum": list(ai.CONFIDENCE),
                       "description": "confirmed: the notes document it for this recording or era; artist: documented "
                                      "for this player, other or unknown era; suggested: a modern equivalent or guess"},
    }, "required": ["kind", "name", "confidence"]}},
    "aliases": {"type": "array", "maxItems": 8, "items": {"type": "string"},
                "description": "Other names for the same rig: the player and nickname, band, song, album, era "
                               "(e.g. 'Nolly', 'Adam Getgood'). Names only, no sound descriptions."},
}, ["description", "gear"])
TOOLS["rate_result"] = (rate_result, "Save the user's verdict on a result, when they say whether it was right: "
                                    "the research and brief (leave pack_id out) or one pack. It improves future "
                                    "results for everyone.", {
    "description": {"type": "string", "description": "The tone the user asked for, as passed to web_research"},
    "rating": {"type": "string", "enum": ["good", "bad"]},
    "pack_id": {"type": "integer", "description": "The pack being rated; leave out to rate the research/brief"},
    "pack_title": {"type": "string"},
    "comment": {"type": "string", "description": "What was right or wrong, in the user's words"},
}, ["description", "rating"])
PROMPT = {"name": "find_tone", "description": "Find NAM captures for a guitar tone, step by step.",
          "arguments": [{"name": "description", "description": "The tone you want", "required": True}]}


KEY_CHECK_SECONDS = 3600
_CHECKED_KEYS: dict = {}  # sha256 of a key -> when TONE3000 last accepted it


def _check_key(key: str, *, opener=research.urlopen) -> None:
    """Keys are how callers are told apart from bots, so a made-up t3k_cs_ key must not get free web searches."""
    digest = hashlib.sha256(key.encode()).hexdigest()
    if time.time() - _CHECKED_KEYS.get(digest, 0) < KEY_CHECK_SECONDS:
        return
    try:
        research.tone3000_lookup("makes", "amp", opener=opener)
    except RuntimeError as exc:
        raise ToolError("TONE3000 did not accept your key. " + NO_KEY) from exc
    _CHECKED_KEYS[digest] = time.time()


def call_tool(name: str, arguments: dict, key: str, **injected) -> dict:
    """Run one tool with the user's own key and return an MCP tool result."""
    if name not in TOOLS:
        raise LookupError(name)
    function, _, properties, required = TOOLS[name]
    try:
        if not isinstance(arguments, dict) or set(arguments) - set(properties) or set(required) - set(arguments):
            raise ToolError(f"{name} takes {', '.join(properties)}; required: {', '.join(required)}.")
        if not key.startswith("t3k_cs_"):
            raise ToolError(NO_KEY)
        overrides.activate({"tone3000_api_key": key})  # never falls back to a server's own key
        try:
            # Saved answers and web research never reach TONE3000 with the caller's key, so check every key here:
            # otherwise any made-up t3k_cs_ key would get cached catalogue data and free web searches.
            _check_key(key)
            if name == "rate_result":
                injected = {**injected, "voter_id": feedback.voter("mcp:" + key)}
            result = function(**arguments, **injected)
        except TypeError as exc:
            raise ToolError(f"Invalid arguments for {name}.") from exc
        finally:
            overrides.activate({})
    except RuntimeError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception as exc:  # a bug must not end the local server's loop or hide the reason from the caller
        print(f"MCP tool {name} failed: {exc!r}", file=sys.stderr)
        return {"content": [{"type": "text", "text": f"{name} failed unexpectedly ({type(exc).__name__})."}],
                "isError": True}
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message, key: str) -> dict | None:
    """Answer one JSON-RPC message; notifications get None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(None, -32600, "Invalid request")
    if "id" not in message:
        return None
    ident, method, params = message["id"], message["method"], message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(ident, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}, "prompts": {}},
            "serverInfo": {"name": "TONE Search", "version": "1.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(ident, {})
    if method == "tools/list":
        return _result(ident, {"tools": [
            {"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}}
            for name, (_, description, properties, required) in TOOLS.items()
        ]})
    if method == "tools/call":
        try:
            return _result(ident, call_tool(params.get("name"), params.get("arguments") or {}, key))
        except LookupError:
            return _error(ident, -32602, f"Unknown tool: {params.get('name')}")
    if method == "prompts/list":
        return _result(ident, {"prompts": [PROMPT]})
    if method == "prompts/get":
        if params.get("name") != PROMPT["name"]:
            return _error(ident, -32602, f"Unknown prompt: {params.get('name')}")
        description = str((params.get("arguments") or {}).get("description", ""))[:500]
        return _result(ident, {"messages": [{"role": "user", "content": {
            "type": "text", "text": f"{INSTRUCTIONS}\n\nThe tone I want: {description}"}}]})
    return _error(ident, -32601, f"Method not found: {method}")


def _result(ident, result):
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def main() -> None:
    """The local stdio server: newline-delimited JSON-RPC, no rate limits."""
    key = os.environ.get("TONE3000_API_KEY", "").strip()
    if not key.startswith("t3k_cs_"):
        sys.exit(NO_KEY)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line), key)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "Parse error")
        except Exception as exc:  # keep serving: one bad message must not end the session
            print(f"MCP message failed: {exc!r}", file=sys.stderr)
            reply = _error(None, -32603, "Internal error")
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
