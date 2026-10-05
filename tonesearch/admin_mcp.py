"""The owner's MCP server: their own assistant reviews the research library, the way they would on /admin.

Like mcp_server, it calls no AI provider: the assistant reads each entry and judges it. Approving keeps the entry as
it is. Changes and deletions are two calls: without `confirm` they only describe what would change, so the assistant
can show the owner, and they are saved only when called again with confirm=true after the owner agrees.

Hosted at `POST /admin/mcp` (app.py) with `Authorization: Bearer <TONESEARCH_ADMIN_MCP_TOKEN>` or `?key=`, and
off (404) until that token is set. Locally, on the local data folder: `python -m tonesearch.admin_mcp`.
"""
from __future__ import annotations

import sqlite3
import time

from tonesearch import ai, cache, feedback, knowledge, mcp_server

INSTRUCTIONS = """You review TONE Search's research library for its owner. Each entry is web research for one tone
request (an artist, song, album, era or sound): cited source notes, the gear taken from them, and other names
(aliases) that later searches match on. Approved entries are reused by every matching search, never expire, and
their gear goes to the site's AI as checked by the owner, so approve only what is right.
1. review_queue lists the entries waiting for review (new, or flagged by visitors).
2. get_entry shows one in full. Judge it:
   - Sources: are they about this topic? Note off-topic, shop or SEO-filler sources.
   - Gear: is each item what the artist used for this request's era or recording? Right kind? Right level (best =
     documented for this recording or era; close = their gear from another era; alternative = a substitute or guess)?
     Is anything important missing, or is one era's rig presented as their whole career?
   - Aliases: other names for the same rig only. An alias for a different album or era whose rig differs sends
     those searches to the wrong research; single common words match too much.
   - Votes and comments from players, and the saved briefs built on it, show what visitors were given.
3. Tell the owner your verdict for each entry, briefly: approve, change (what and why) or delete.
4. Approve right entries with approve_entry. For changes, call update_entry (or delete_entry) without confirm: it
   shows the exact change. Show it to the owner and call again with confirm=true only after they agree.
Gear you add from your own knowledge is allowed: say so in its role, and use 'alternative' when you're unsure."""

STATUS_ORDER = ("new", "flagged", "approved")


class ToolError(RuntimeError):
    """A tool failed in a way the assistant should read and act on."""


def _db():
    return mcp_server.library_path()


def _when(seconds: float) -> str:
    return time.strftime("%d %b %Y", time.gmtime(seconds))


def _sources(notes: str) -> list[str]:
    return [line.strip() for line in (notes or "").splitlines() if line.strip()]


def _gear_line(g: dict) -> str:
    return f"{g.get('kind', '')} | {g.get('name', '')} | {g.get('role', '')} | {ai.confidence_of(g.get('confidence'))}"


def _aliases(entry: dict) -> list[str]:
    return knowledge.clean_aliases((entry.get("aliases") or "").split(","))


def _entry(entry_id) -> dict:
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise ToolError("entry_id must be a number from review_queue.")
    entry = knowledge.get(_db(), entry_id)
    if not entry:
        raise ToolError(f"There is no research #{entry_id}. It may have been deleted.")
    return entry


# ---- What an entry reaches (shared with the /admin page) -------------------------------------------------------

def entry_links(db, entries: list) -> dict:
    """What each library entry's research reaches: the topics of searches that used it (its own words, saved
    answers built on it under any name, votes on it), how many saved answers, and its votes."""
    answers = cache.answers_by_entry(db)
    try:
        votes = feedback.all_votes(db)
    except sqlite3.Error:
        votes = []
    by_entry: dict = {}
    by_words: dict = {}
    for v in votes:
        by_entry.setdefault(v["entry_id"], []).append(v)
        by_words.setdefault(v["words"], []).append(v)
    links = {}
    for entry in entries:
        on_entry = by_entry.get(entry["id"], [])
        topics = {entry["words"]} | answers.get(entry["id"], set()) | {v["words"] for v in on_entry}
        mine = {v["id"]: v for v in [*on_entry, *(v for t in topics for v in by_words.get(t, []))]}.values()
        links[entry["id"]] = {"topics": sorted(t for t in topics if t), "answers": len(answers.get(entry["id"], ())),
                              "good": sum(v["vote"] == 1 for v in mine), "bad": sum(v["vote"] == -1 for v in mine)}
    return links


def delete_entry_and_links(db, entry_id: int) -> str:
    """Delete a research entry and everything built on it: saved answers, plans and rankings for every topic that
    used it, and the votes on those topics and on the entry itself."""
    entry = knowledge.get(db, entry_id)
    if not entry:
        return f"Research #{entry_id} was already deleted."
    link = entry_links(db, [entry])[entry_id]
    saved = sum(cache.forget_topic(db, words) for words in link["topics"])
    try:
        votes = feedback.forget(db, words=link["topics"], entry_id=entry_id)
    except sqlite3.Error:
        votes = 0
    knowledge.delete(db, entry_id)
    return (f"Deleted research #{entry_id} ({entry['topic']}), {saved} saved answer{'s' if saved != 1 else ''} and "
            f"plan{'s' if saved != 1 else ''}, and {votes} vote{'s' if votes != 1 else ''}.")


def save_entry(db, entry: dict, **changes) -> int:
    """Save changes to an entry (topic, notes, gear, aliases, status; the rest kept), then clear the saved answers,
    plans and rankings of every topic built on the old version, found under any name; how many were cleared.
    Raises ValueError, with nothing cleared, when knowledge.update refuses the change."""
    topics = entry_links(db, [entry])[entry["id"]]["topics"]
    knowledge.update(db, entry["id"], **{"topic": entry["topic"], "notes": entry["notes"], "gear": entry["gear"],
                                         "aliases": _aliases(entry), "status": entry["status"], **changes})
    return sum(cache.forget_topic(db, words) for words in topics)


def approve_as_is(db, entry: dict) -> None:
    """Approve an entry without changing it: answers built on it still fit, so nothing is cleared."""
    knowledge.update(db, entry["id"], topic=entry["topic"], notes=entry["notes"], gear=entry["gear"],
                     aliases=_aliases(entry), status="approved")


# ---- Tools ----------------------------------------------------------------------------------------------------

def review_queue(status: str = "waiting", limit: int = 20, query: str = "") -> str:
    """Entries to review, one line each. "waiting" is new and flagged entries, flagged first."""
    if status not in ("waiting", "all", *STATUS_ORDER):
        raise ToolError("status must be waiting, new, flagged, approved or all.")
    if not isinstance(limit, int) or not 0 < limit <= 50:
        raise ToolError("limit must be 1-50.")
    db = _db()
    wanted = ("flagged", "new") if status == "waiting" else (STATUS_ORDER if status == "all" else (status,))
    entries = [e for s in wanted for e in knowledge.search(db, query[:100], s, limit)][:limit]
    counts = knowledge.counts(db)
    lines = [f"Library: {counts['all']} entries ({', '.join(f'{counts[s]} {s}' for s in STATUS_ORDER)})."]
    if not entries:
        return "\n".join(lines + ["Nothing matches."])
    links = entry_links(db, entries)
    for e in entries:
        sources = _sources(e["notes"])
        snippets = sum("Search snippet only:" in s for s in sources)
        link = links[e["id"]]
        line = (f"#{e['id']} {e['topic']} [{e['status']}]: {len(e['gear'])} gear, {len(sources)} sources"
                f"{f' ({snippets} snippet only)' if snippets else ''}, used {e['uses']}x, "
                f"votes {link['good']} good / {link['bad']} bad, updated {_when(e['updated'])}")
        if _aliases(e):
            line += f"; aliases: {', '.join(_aliases(e))}"
        if e["status"] == "flagged" and e["flag_reason"]:
            line += f"; flagged {e['flags']}x: \"{e['flag_reason']}\""
        lines.append(line)
    return "\n".join(lines)


def _votes(db, entry_id: int, topics: list) -> list[dict]:
    try:
        return feedback.on_entry(db, entry_id, topics)
    except sqlite3.Error:
        return []


def get_entry(entry_id: int) -> str:
    db = _db()
    e = _entry(entry_id)
    link = entry_links(db, [e])[e["id"]]
    lines = [f"Research #{e['id']}: {e['topic']}",
             f"Status: {e['status']}" + (f" (flagged {e['flags']}x: \"{e['flag_reason']}\")"
                                         if e["status"] == "flagged" else ""),
             f"Matched on the words: {e['words']}",
             f"Aliases: {', '.join(_aliases(e)) or 'none'}",
             f"Used {e['uses']}x; created {_when(e['created'])}, updated {_when(e['updated'])}",
             "", "Gear (kind | name | role | level):"]
    lines += [f"  {n}. {_gear_line(g)}" for n, g in enumerate(e["gear"], 1)] or ["  none"]
    lines += ["", "Sources (numbers are for update_entry's remove_sources):"]
    lines += [f"  [{n}] {s.removeprefix('- ')}" for n, s in enumerate(_sources(e["notes"]), 1)]
    votes = _votes(db, e["id"], link["topics"])
    lines += ["", f"Votes: {link['good']} good, {link['bad']} bad"]
    for v in votes:
        target = f"pack \"{v['pack_title']}\"" if v["target"] == "pack" else "brief"
        comment = f": \"{v['comment']}\"" if v["comment"] else ""
        lines.append(f"  {'good' if v['vote'] == 1 else 'bad'} {target} for \"{v['prompt']}\" ({v['source']}){comment}")
    briefs = [(words, answer) for words, answer, _ in cache.by_prefix(db, "result:", cache.ANSWER_SECONDS)
              if isinstance(answer, dict) and ((answer.get("library") or {}).get("id") == e["id"])]
    if briefs:
        lines += ["", "Saved briefs built on it:"]
        for words, answer in briefs[:3]:
            plan = answer.get("plan") or {}
            gear = ", ".join(f"{g.get('name')} ({ai.confidence_of(g.get('confidence'))})" for g in plan.get("gear") or [])
            lines.append(f"  \"{answer.get('topic', words)}\": {plan.get('summary', '')} Gear: {gear or 'none'}. "
                         f"Searches: {', '.join(answer.get('queries') or [])}")
    return "\n".join(lines)


def approve_entry(entry_id: int) -> str:
    e = _entry(entry_id)
    if e["status"] == "approved":
        return f"Research #{e['id']} was already approved."
    approve_as_is(_db(), e)
    return f"Approved research #{e['id']} ({e['topic']})."


def _clean_gear(gear) -> list[dict]:
    if not isinstance(gear, list) or len(gear) > 12:
        raise ToolError("gear must list up to 12 items.")
    items = []
    for item in gear:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ToolError("Each gear item needs kind, name, role and confidence.")
        name = str(item["name"]).strip()[:80]
        if ai._generic(name):
            raise ToolError(f"'{name}' is a category, not a product: name the make and model.")
        kind = item.get("kind") if item.get("kind") in mcp_server.GEAR_KINDS else "other"
        items.append({"kind": kind, "name": name, "role": str(item.get("role") or "").strip()[:240],
                      "confidence": ai.confidence_of(item.get("confidence"))})
    return items


def _list_change(label: str, before: list[str], after: list[str]) -> list[str]:
    if before == after:
        return []
    removed = [x for x in before if x not in after]
    added = [x for x in after if x not in before]
    lines = [f"{label}:"]
    lines += [f"  - {x}" for x in removed] + [f"  + {x}" for x in added]
    if not removed and not added:
        lines.append("  (same items, new order)")
    return lines


def update_entry(entry_id: int, topic: str | None = None, aliases: list | None = None, gear: list | None = None,
                 remove_sources: list | None = None, status: str | None = None, confirm: bool = False) -> str:
    """Change an entry. Without confirm, only describes the change; with confirm=true, saves it."""
    e = _entry(entry_id)
    new_topic = e["topic"] if topic is None else str(topic).strip()[:200]
    if aliases is not None and not isinstance(aliases, list):
        raise ToolError("aliases must be a list of names.")
    new_aliases = _aliases(e) if aliases is None else knowledge.clean_aliases([str(a)[:60] for a in aliases[:12]])
    new_gear = e["gear"] if gear is None else _clean_gear(gear)
    new_status = e["status"] if status is None else status
    if new_status not in knowledge.STATUSES:
        raise ToolError("status must be new, approved or flagged.")
    sources = _sources(e["notes"])
    if remove_sources is not None and not isinstance(remove_sources, list):
        raise ToolError("remove_sources must be a list of source numbers.")
    drop = set(remove_sources or [])
    if any(not isinstance(n, int) or not 0 < n <= len(sources) for n in drop):
        raise ToolError(f"remove_sources takes source numbers from get_entry (1-{len(sources)}).")
    kept = [s for n, s in enumerate(sources, 1) if n not in drop]
    if not kept:
        raise ToolError("That removes every source. Use delete_entry instead.")
    if not knowledge.identity_words(knowledge.topic_words(new_topic)):
        raise ToolError("The topic needs at least one identifying word (an artist, song, album or gear name).")

    changes = []
    if new_topic != e["topic"]:
        changes += [f"Topic: \"{e['topic']}\" -> \"{new_topic}\""]
    changes += _list_change("Aliases", _aliases(e), new_aliases)
    changes += _list_change("Gear", [_gear_line(g) for g in e["gear"]], [_gear_line(g) for g in new_gear])
    changes += [f"Remove source [{n}]: {sources[n - 1].removeprefix('- ')[:160]}" for n in sorted(drop)]
    if new_status != e["status"]:
        changes += [f"Status: {e['status']} -> {new_status}"]
    if not changes:
        return f"Nothing would change in research #{e['id']}."
    summary = "\n".join(changes)
    if not confirm:
        return (f"Proposed changes to research #{e['id']} ({e['topic']}), NOT saved yet:\n{summary}\n\n"
                "Show these to the owner. Once they agree, call update_entry again with the same arguments and "
                "confirm=true.")
    try:
        cleared = save_entry(_db(), e, topic=new_topic, notes="\n".join(kept), gear=new_gear, aliases=new_aliases,
                             status=new_status)
    except ValueError as exc:
        raise ToolError(f"Not saved: {exc}") from exc
    return (f"Saved research #{e['id']}:\n{summary}\n"
            f"Cleared {cleared} saved answer{'s' if cleared != 1 else ''}, plan{'s' if cleared != 1 else ''} and "
            "ranking" + ("s" if cleared != 1 else "") + " built on the old version.")


def delete_entry(entry_id: int, confirm: bool = False) -> str:
    db = _db()
    e = _entry(entry_id)
    if not confirm:
        link = entry_links(db, [e])[e["id"]]
        return (f"Deleting research #{e['id']} ({e['topic']}) would also delete the saved answers, plans and "
                f"rankings of {len(link['topics'])} topic{'s' if len(link['topics']) != 1 else ''} "
                f"({'; '.join(link['topics'])}), and their votes ({link['good']} good, {link['bad']} bad). "
                "NOT deleted yet: show this to the owner, and call delete_entry again with confirm=true once they agree.")
    return delete_entry_and_links(db, e["id"])


_CONFIRM = {"type": "boolean", "default": False,
            "description": "Leave false to see the change. True saves it: only after the owner agreed."}
TOOLS = {
    "review_queue": (review_queue, "List research library entries for review, one line each: topic, status, gear "
                                   "and source counts, uses, votes, aliases and any visitor's flag reason.", {
        "status": {"type": "string", "enum": ["waiting", "new", "flagged", "approved", "all"], "default": "waiting",
                   "description": "waiting = new and flagged, flagged first"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "query": {"type": "string", "description": "Words in the topic, notes or gear, or '#12' for one entry"},
    }, []),
    "get_entry": (get_entry, "One entry in full: topic, aliases, gear, numbered sources, players' votes and comments, "
                             "and the saved briefs built on it.", {
        "entry_id": {"type": "integer"},
    }, ["entry_id"]),
    "approve_entry": (approve_entry, "Approve an entry as it is: it is then reused for matching searches, never "
                                     "expires, and its gear is given to the site's AI as checked.", {
        "entry_id": {"type": "integer"},
    }, ["entry_id"]),
    "update_entry": (update_entry, "Change an entry: give only what changes. Without confirm it shows the change and "
                                   "saves nothing; with confirm=true (after the owner agrees) it saves it and clears "
                                   "the saved answers built on the old version.", {
        "entry_id": {"type": "integer"},
        "topic": {"type": "string", "description": "The request it answers, e.g. 'Pete Townshend Live at Leeds'. "
                                                   "Searches match on its words."},
        "aliases": {"type": "array", "maxItems": 12, "items": {"type": "string"},
                    "description": "The full new list (replaces the old one; [] clears it). Other names for the same "
                                   "rig only."},
        "gear": {"type": "array", "maxItems": 12, "description": "The full new gear list (replaces the old one).",
                 "items": {"type": "object", "properties": {
                     "kind": {"type": "string", "enum": list(mcp_server.GEAR_KINDS)},
                     "name": {"type": "string", "description": "Make and model, e.g. 'Hiwatt DR103'"},
                     "role": {"type": "string", "description": "What it does in this tone, and its era"},
                     "confidence": {"type": "string", "enum": list(ai.CONFIDENCE),
                                    "description": mcp_server.LEVELS},
                 }, "required": ["kind", "name", "role", "confidence"]}},
        "remove_sources": {"type": "array", "items": {"type": "integer"},
                           "description": "Numbers of off-topic or useless sources, from get_entry"},
        "status": {"type": "string", "enum": list(knowledge.STATUSES),
                   "description": "Usually 'approved' once fixed; leave out to keep the current status"},
        "confirm": _CONFIRM,
    }, ["entry_id"]),
    "delete_entry": (delete_entry, "Delete an entry and everything built on it (saved answers, plans, rankings and "
                                   "votes). Without confirm it shows what would go; confirm=true deletes, only after "
                                   "the owner agrees.", {
        "entry_id": {"type": "integer"},
        "confirm": _CONFIRM,
    }, ["entry_id"]),
}
PROMPT = {"name": "review_library", "description": "Review the research library entries waiting for approval.",
          "arguments": [{"name": "limit", "description": "How many entries to review (default 10)", "required": False}]}


def call_tool(name: str, arguments: dict) -> dict:
    return mcp_server.run_tool(TOOLS, name, arguments, lambda function, arguments: function(**arguments),
                               server="Admin MCP")


def _prompt_text(arguments: dict) -> str:
    limit = str(arguments.get("limit") or "10")[:3]
    return (f"{INSTRUCTIONS}\n\nReview up to {limit} entries waiting for approval. Give me a short verdict for each, "
            "approve the ones that are right, and show me the proposed changes for the rest.")


def handle(message) -> dict | None:
    """Answer one JSON-RPC message; notifications get None. The caller has already checked the owner's token."""
    return mcp_server.answer(message, server="TONE Search admin", instructions=INSTRUCTIONS, tools=TOOLS,
                             prompt=PROMPT, call=call_tool, prompt_text=_prompt_text)


def main() -> None:
    """The local stdio server, on the local data folder (TONESEARCH_DATA_DIR)."""
    mcp_server.serve(handle, "Admin MCP")


if __name__ == "__main__":
    main()
