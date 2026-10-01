"""The research library: web research notes and the gear taken from them, saved per tone topic and reused.

A new search first looks for a stored entry whose topic words strongly overlap its own; only a miss goes to the
web. Entries are reviewed on the admin page: approved ones never expire, unreviewed ones expire after
TONESEARCH_LIBRARY_DAYS, and a visitor's "wrong research" flag keeps an entry out of use until it's reviewed.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from tonesearch.research import _topic

STATUSES = ("new", "approved", "flagged")
UNREVIEWED_DAYS = float(os.environ.get("TONESEARCH_LIBRARY_DAYS", "30"))
MATCH_THRESHOLD = 0.75  # shared topic words over all topic words (Jaccard)
# Words that say nothing about which tone is meant, so they never decide a match.
_GENERIC = {"guitar", "guitars", "amp", "amps", "rig", "gear", "setup", "settings", "song", "album", "record",
            "track", "band", "sound", "tone", "tones", "style", "like", "vibe", "the", "and", "for"}


def topic_words(text: str) -> list[str]:
    """The words that identify a tone request, lowercased, sorted and without filler."""
    words = {w.strip(".'’-") for w in re.findall(r"[\w'’.-]+", _topic(text).lower())}
    return sorted(w for w in words if len(w) > 1 and w not in _GENERIC)


def _connect(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY, topic TEXT NOT NULL, words TEXT NOT NULL UNIQUE, notes TEXT NOT NULL,
        gear TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new', flags INTEGER NOT NULL DEFAULT 0,
        flag_reason TEXT NOT NULL DEFAULT '', uses INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
        updated REAL NOT NULL)""")
    return connection


def _entry(row) -> dict:
    return {**dict(row), "gear": json.loads(row["gear"] or "[]")}


def _best_effort(function):
    """The library only saves work: if its database fails, research carries on without it."""
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except sqlite3.Error as exc:
            print(f"Research library unavailable: {exc}", file=sys.stderr)
            return None
    run.__name__, run.__doc__ = function.__name__, function.__doc__
    return run


@_best_effort
def find(db: Path, text: str) -> dict | None:
    """The best usable entry for a request, or None when nothing overlaps strongly enough."""
    wanted = set(topic_words(text))
    if not wanted:
        return None
    oldest = time.time() - UNREVIEWED_DAYS * 86400
    best, best_key = None, (MATCH_THRESHOLD, False)
    with _connect(db) as connection:
        rows = connection.execute(
            "SELECT * FROM entries WHERE status = 'approved' OR (status = 'new' AND updated >= ?)", (oldest,))
        for row in rows:
            words = set(row["words"].split())
            key = (len(wanted & words) / len(wanted | words), row["status"] == "approved")  # ties go to reviewed
            if key >= best_key:
                best, best_key = row, key
        if best is None:
            return None
        connection.execute("UPDATE entries SET uses = uses + 1 WHERE id = ?", (best["id"],))
    return {**_entry(best), "score": round(best_key[0], 2)}


@_best_effort
def save(db: Path, text: str, notes: str, gear: list | None = None) -> int | None:
    """Store fresh research for a topic. An approved entry is never overwritten; others are refreshed."""
    words = " ".join(topic_words(text))
    if not words or not notes.strip():
        return None
    now = time.time()
    gear_json = json.dumps((gear or [])[:12])
    with _connect(db) as connection:
        row = connection.execute("SELECT id, status, gear FROM entries WHERE words = ?", (words,)).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO entries (topic, words, notes, gear, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                (_topic(text)[:200], words, notes[:6000], gear_json, now, now))
            return cursor.lastrowid
        if row["status"] != "approved":  # a flagged entry stays flagged until the owner reviews it
            connection.execute("UPDATE entries SET notes = ?, gear = ?, updated = ? WHERE id = ?",
                               (notes[:6000], gear_json if gear else row["gear"], now, row["id"]))
        return row["id"]


@_best_effort
def add_gear(db: Path, entry_id: int, gear: list) -> None:
    """Fill in the gear for an entry saved without it (MCP research has no AI to read the notes)."""
    if gear:
        with _connect(db) as connection:
            connection.execute("UPDATE entries SET gear = ? WHERE id = ? AND gear = '[]'",
                               (json.dumps(gear[:12]), entry_id))


def flag(db: Path, entry_id: int, reason: str = "") -> bool:
    """A visitor says the research is wrong: stop using it until the owner reviews it."""
    with _connect(db) as connection:
        cursor = connection.execute(
            "UPDATE entries SET status = 'flagged', flags = flags + 1, flag_reason = ? WHERE id = ?",
            (reason.strip()[:300], entry_id))
        return cursor.rowcount > 0


# ---- Owner review ---------------------------------------------------------------------------------------------

def search(db: Path, text: str = "", status: str = "", limit: int = 200) -> list[dict]:
    """Entries for the admin page: flagged first, then newest; `text` matches topic, notes or gear."""
    sql, params = "SELECT * FROM entries WHERE 1 = 1", []
    if status in STATUSES:
        sql += " AND status = ?"
        params.append(status)
    if text.strip():
        sql += " AND (topic LIKE ? OR notes LIKE ? OR gear LIKE ?)"
        params += [f"%{text.strip()}%"] * 3
    sql += " ORDER BY status = 'flagged' DESC, updated DESC LIMIT ?"
    params.append(limit)
    with _connect(db) as connection:
        return [_entry(row) for row in connection.execute(sql, params)]


def update(db: Path, entry_id: int, *, topic: str, notes: str, gear: list, status: str) -> None:
    if status not in STATUSES:
        raise ValueError("unknown status")
    words = " ".join(topic_words(topic))
    if not words:
        raise ValueError("The topic needs at least one identifying word.")
    with _connect(db) as connection:
        clash = connection.execute("SELECT id FROM entries WHERE words = ? AND id != ?", (words, entry_id)).fetchone()
        if clash:
            raise ValueError(f"Entry {clash['id']} already covers that topic.")
        connection.execute(
            "UPDATE entries SET topic = ?, words = ?, notes = ?, gear = ?, status = ?, updated = ?,"
            " flags = CASE WHEN ? = 'flagged' THEN flags ELSE 0 END,"
            " flag_reason = CASE WHEN ? = 'flagged' THEN flag_reason ELSE '' END WHERE id = ?",
            (topic.strip()[:200], words, notes[:6000], json.dumps(gear[:12]), status, time.time(), status, status,
             entry_id))


@_best_effort
def get(db: Path, entry_id: int) -> dict | None:
    with _connect(db) as connection:
        row = connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return _entry(row) if row else None


def delete(db: Path, entry_id: int) -> None:
    with _connect(db) as connection:
        connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
