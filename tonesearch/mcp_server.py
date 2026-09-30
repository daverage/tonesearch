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
import sys
import time

from tonesearch import overrides, research

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = """TONE Search finds TONE3000 NAM (Neural Amp Modeler) capture packs for a guitar tone.
You do the thinking; these tools fetch data. Workflow:
1. Always call web_research first with the user's description to learn the actual rig (amp, pedals, cab).
   Skip it only when the user already names the exact gear. Base your queries on what it finds.
2. Plan 1-3 short catalogue queries naming gear, e.g. "Marshall Plexi" or "Klon Centaur", not song titles.
   Use lookup to find exact make, tag or creator slugs before using them as filters.
3. Call search_packs for each query and merge the results by id.
4. Rank the packs against the user's tone yourself and explain the fit briefly.
5. For a chosen pack, list_pack_models shows its files; download_link gives the pack's TONE3000 page to download it from."""
NO_KEY = ("TONE Search needs your own TONE3000 secret key (t3k_cs_…) from https://www.tone3000.com: "
          "set TONE3000_API_KEY locally, or send 'Authorization: Bearer t3k_cs_…' to the hosted endpoint.")


class ToolError(RuntimeError):
    """A tool failed in a way the client's AI should read and act on."""


def web_research(description: str, *, notes=research.web_notes) -> str:
    description = description.strip()
    if not 0 < len(description) <= 500:
        raise ToolError("Describe the tone in 1-500 characters.")
    return notes(description)


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
    return research.tone3000_search(query, filters=filters, opener=opener)


def lookup(kind: str, text: str, *, opener=research.urlopen) -> list[dict]:
    if kind not in research.LOOKUP_KINDS:
        raise ToolError("kind must be makes, tags or creators.")
    if not 0 < len(text.strip()) <= 60:
        raise ToolError("The lookup text must be 1-60 characters.")
    return research.tone3000_lookup(kind, text.strip(), opener=opener)


def list_pack_models(pack_id: int, architecture: str = "2", *, opener=research.urlopen) -> list[dict]:
    if architecture not in research.ARCHITECTURES:
        raise ToolError("architecture must be 1, 2 or any.")
    return research.tone3000_models(pack_id, architecture=architecture, opener=opener)


def download_link(pack_id: int) -> str:
    return f"https://www.tone3000.com/tones/{pack_id}"


def _strings(description):
    return {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": description}


TOOLS = {
    "web_research": (web_research, "Search the web for the gear behind a described guitar tone (artist, song, era). "
                                   "Call this first, before search_packs, unless the user names the exact gear. "
                                   "Returns short cited notes. Slow: up to about 30 seconds.", {
        "description": {"type": "string", "description": "The tone, e.g. 'Gilmour's Comfortably Numb solo'"},
    }, ["description"]),
    "search_packs": (search_packs, "Search TONE3000 for NAM capture packs with one short gear query, e.g. 'Vox AC30'. "
                                   "Call web_research first to learn the gear, unless the user names it exactly. "
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
            if name == "web_research":  # the one tool that never reaches TONE3000, so check the key there first
                _check_key(key)
            result = function(**arguments, **injected)
        except TypeError as exc:
            raise ToolError(f"Invalid arguments for {name}.") from exc
        finally:
            overrides.activate({})
    except RuntimeError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(message, key: str) -> dict | None:
    """Answer one JSON-RPC message; notifications get None."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(None, -32600, "Invalid request")
    if "id" not in message:
        return None
    ident, method, params = message["id"], message["method"], message.get("params") or {}
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
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
