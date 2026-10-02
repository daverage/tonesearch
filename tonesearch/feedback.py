"""Visitors' and MCP users' ratings of tone briefs and packs, kept for the owner and used to re-order results.

One vote per voter, topic and target (the brief, or one pack): voting again changes it. Voters are a short hash
of an IP address or MCP key, never the value itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from tonesearch import ai, cache, knowledge
from tonesearch import db as database

TARGETS = ("brief", "pack")
SOURCES = ("web", "mcp")
_SALT = os.environ.get("TONESEARCH_FEEDBACK_SALT", "tonesearch-feedback")


def voter(identity: str) -> str:
    return hashlib.sha256(f"{_SALT}:{identity}".encode()).hexdigest()[:16]


def _setup(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, voter TEXT NOT NULL, words TEXT NOT NULL,
        prompt TEXT NOT NULL, target TEXT NOT NULL, pack_id INTEGER NOT NULL DEFAULT 0,
        pack_title TEXT NOT NULL DEFAULT '', vote INTEGER NOT NULL, comment TEXT NOT NULL DEFAULT '',
        entry_id INTEGER, source TEXT NOT NULL DEFAULT 'web',
        UNIQUE (voter, words, target, pack_id))""")
    # Brief votes name the brief they were cast on (brief_id), so they count together whichever wording showed it.
    if "brief" not in {row[1] for row in connection.execute("PRAGMA table_info(feedback)")}:
        connection.execute("ALTER TABLE feedback ADD COLUMN brief TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE INDEX IF NOT EXISTS feedback_brief ON feedback (brief)")


def _connect(db: Path):
    return database.connect(db, _setup, rows=True)


def record(db: Path, *, voter_id: str, words: str, prompt: str, target: str, vote: int, pack_id: int = 0,
           pack_title: str = "", comment: str = "", entry_id: int | None = None, source: str = "web",
           brief: str = "") -> None:
    if target not in TARGETS or vote not in (1, -1) or source not in SOURCES or not words:
        raise ValueError("invalid feedback")
    with _connect(db) as connection:
        connection.execute(
            "INSERT INTO feedback (created, voter, words, prompt, target, pack_id, pack_title, vote, comment,"
            " entry_id, source, brief) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (voter, words, target, pack_id) DO UPDATE SET created = excluded.created,"
            " vote = excluded.vote, comment = excluded.comment, prompt = excluded.prompt, brief = excluded.brief",
            (time.time(), voter_id, words, prompt[:600], target, pack_id if target == "pack" else 0,
             pack_title[:200], vote, comment.strip()[:500], entry_id, source, brief if target == "brief" else ""))


def brief_id(plan) -> str:
    """Names a brief by its content, so votes, liked status and pack scores belong to that brief whichever wording
    of the request showed it, and a new brief for the same tone starts unrated. '' for no plan."""
    if not isinstance(plan, dict):
        return ""
    content = {k: plan.get(k) for k in ("summary", "search_queries", "requirements")}
    content["gear"] = [(g.get("name"), g.get("confidence")) for g in plan.get("gear") or [] if isinstance(g, dict)]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]


def brief_votes(db: Path, brief: str) -> tuple[int, int]:
    """(good, bad) ratings of one brief, counting each voter once (their latest vote, under any wording).
    (0, 0) when the database is unavailable or there's no brief."""
    if not brief:
        return 0, 0
    try:
        with _connect(db) as connection:
            row = connection.execute(
                "SELECT SUM(vote = 1), SUM(vote = -1) FROM feedback f WHERE target = 'brief' AND brief = ? AND created = "
                "(SELECT MAX(created) FROM feedback WHERE target = 'brief' AND brief = f.brief AND voter = f.voter)",
                (brief,)).fetchone()
            return int(row[0] or 0), int(row[1] or 0)
    except sqlite3.Error as exc:
        print(f"Feedback unavailable: {exc}", file=sys.stderr)
        return 0, 0


def liked(db: Path, plan) -> bool:
    """True when more players rated this brief good than bad."""
    good, bad = brief_votes(db, brief_id(plan))
    return good > bad


def saved_plans(db: Path, words_list) -> list[dict]:
    """The saved plans for these topics (with and without web research)."""
    plans = []
    for words in dict.fromkeys(w for w in words_list if w):
        for use_web in (True, False):
            plan = cache.get(db, f"plan:{words}:{use_web}:p{ai.PLAN_VERSION}", float("inf"))
            if isinstance(plan, dict):
                plans.append(plan)
    return plans


def bad_brief(db: Path, words_list, entry: dict | None, reason: str) -> bool:
    """A player says a brief is wrong (website or MCP). Unless more players liked it, the saved answers, plans and
    pack scores of these topics (the request's, and the one the brief was saved under) are cleared so the next search
    works it out again, and the research is flagged for review. Research the owner approved stays approved: one
    visitor doesn't undo that. True when the brief was cleared."""
    if any(liked(db, plan) for plan in saved_plans(db, words_list)):
        return False
    if entry and entry.get("status") != "approved":
        knowledge.flag(db, entry["id"], reason)
    for words in dict.fromkeys(w for w in words_list if w):
        cache.forget_topic(db, words)
    return True


def pack_votes(db: Path, words: str) -> dict:
    """Net votes per pack id for a topic; {} when the database is unavailable."""
    try:
        with _connect(db) as connection:
            rows = connection.execute("SELECT pack_id, SUM(vote) FROM feedback WHERE words = ? AND target = 'pack'"
                                      " GROUP BY pack_id", (words,))
            return {pack_id: total for pack_id, total in rows if total}
    except sqlite3.Error as exc:
        print(f"Feedback unavailable: {exc}", file=sys.stderr)
        return {}


def apply_votes(packs: list, votes: dict) -> list:
    """Add `votes` to each pack and re-sort: each net vote moves a pack 8 fit points, at most 3 votes either way."""
    for pack in packs:
        pack["votes"] = votes.get(pack["id"], 0)
    if votes:
        packs.sort(key=lambda p: (p.get("ai_fit", -1) + 8 * max(-3, min(3, p["votes"])), p.get("match_score", 0),
                                  p.get("downloads_count") or 0), reverse=True)
    return packs


def recent(db: Path, *, vote: int | None = None, text: str = "", limit: int = 200) -> list[dict]:
    sql, params = "SELECT * FROM feedback WHERE 1 = 1", []
    if vote in (1, -1):
        sql += " AND vote = ?"
        params.append(vote)
    if text.strip():
        sql += " AND (prompt LIKE ? OR words LIKE ? OR pack_title LIKE ? OR comment LIKE ?)"
        params += [f"%{text.strip()}%"] * 4
    sql += " ORDER BY created DESC LIMIT ?"
    params.append(limit)
    with _connect(db) as connection:
        return [dict(row) for row in connection.execute(sql, params)]


def topic_summary(db: Path, limit: int = 50) -> list[dict]:
    """Topics with the most votes: good and bad counts for briefs and packs."""
    with _connect(db) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT words, COUNT(*) AS votes, SUM(vote = 1) AS good, SUM(vote = -1) AS bad,"
            " SUM(target = 'brief' AND vote = -1) AS bad_briefs, MAX(created) AS latest"
            " FROM feedback GROUP BY words ORDER BY bad_briefs DESC, votes DESC LIMIT ?", (limit,))]


def all_votes(db: Path) -> list[dict]:
    """Every vote's topic, research entry and direction, for linking votes to library entries."""
    with _connect(db) as connection:
        return [dict(row) for row in connection.execute("SELECT id, words, entry_id, vote FROM feedback")]


def forget(db: Path, *, words=(), entry_id: int | None = None) -> int:
    """Delete the votes for these topics and for this research entry; how many were deleted."""
    words = [w for w in words if w]
    clauses, params = [], []
    if entry_id is not None:
        clauses.append("entry_id = ?")
        params.append(entry_id)
    if words:
        clauses.append(f"words IN ({', '.join('?' * len(words))})")
        params += words
    if not clauses:
        return 0
    with _connect(db) as connection:
        return connection.execute(f"DELETE FROM feedback WHERE {' OR '.join(clauses)}", params).rowcount


def delete_vote(db: Path, vote_id: int) -> bool:
    with _connect(db) as connection:
        return connection.execute("DELETE FROM feedback WHERE id = ?", (vote_id,)).rowcount > 0
