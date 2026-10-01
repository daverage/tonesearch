"""Visitors' and MCP users' ratings of tone briefs and packs, kept for the owner and used to re-order results.

One vote per voter, topic and target (the brief, or one pack): voting again changes it. Voters are a short hash
of an IP address or MCP key, never the value itself.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

TARGETS = ("brief", "pack")
SOURCES = ("web", "mcp")
_SALT = os.environ.get("TONESEARCH_FEEDBACK_SALT", "tonesearch-feedback")


def voter(identity: str) -> str:
    return hashlib.sha256(f"{_SALT}:{identity}".encode()).hexdigest()[:16]


def _connect(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, voter TEXT NOT NULL, words TEXT NOT NULL,
        prompt TEXT NOT NULL, target TEXT NOT NULL, pack_id INTEGER NOT NULL DEFAULT 0,
        pack_title TEXT NOT NULL DEFAULT '', vote INTEGER NOT NULL, comment TEXT NOT NULL DEFAULT '',
        entry_id INTEGER, source TEXT NOT NULL DEFAULT 'web',
        UNIQUE (voter, words, target, pack_id))""")
    return connection


def record(db: Path, *, voter_id: str, words: str, prompt: str, target: str, vote: int, pack_id: int = 0,
           pack_title: str = "", comment: str = "", entry_id: int | None = None, source: str = "web") -> None:
    if target not in TARGETS or vote not in (1, -1) or source not in SOURCES or not words:
        raise ValueError("invalid feedback")
    with _connect(db) as connection:
        connection.execute(
            "INSERT INTO feedback (created, voter, words, prompt, target, pack_id, pack_title, vote, comment,"
            " entry_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (voter, words, target, pack_id) DO UPDATE SET created = excluded.created,"
            " vote = excluded.vote, comment = excluded.comment, prompt = excluded.prompt",
            (time.time(), voter_id, words, prompt[:600], target, pack_id if target == "pack" else 0,
             pack_title[:200], vote, comment.strip()[:500], entry_id, source))


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
