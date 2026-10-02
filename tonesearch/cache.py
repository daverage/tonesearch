"""Saved answers, so repeat searches skip the AI, the web and TONE3000.

Values live in the research library's database. Each read says how old a value may be, by what it holds:
TONE3000 search results go out of date when a pack is uploaded, so they (and whole answers, which contain them)
last CATALOGUE_SECONDS; the AI's plans and rankings only change with better research, so they last AI_SECONDS;
pack file lists and filter suggestions change slowly and last REFERENCE_SECONDS. Each row can carry a
topic (`words`, see knowledge.topic_words) so everything saved for a topic can be dropped at once: when the
owner edits its research or a visitor reports a wrong brief. Like the library, the cache is best-effort: a
database failure means a cache miss, never a failed search.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from tonesearch import db as database

CATALOGUE_SECONDS = float(os.environ.get("TONESEARCH_CATALOGUE_HOURS", "24")) * 3600
AI_SECONDS = float(os.environ.get("TONESEARCH_AI_CACHE_DAYS", "30")) * 86400
REFERENCE_SECONDS = 7 * 86400
_LONGEST = max(CATALOGUE_SECONDS, AI_SECONDS, REFERENCE_SECONDS)  # rows older than this are never read again


def _setup(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, words TEXT NOT NULL DEFAULT '',"
                       " value TEXT NOT NULL, created REAL NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS cache_words ON cache (words)")
    connection.execute("CREATE TABLE IF NOT EXISTS activity (day TEXT NOT NULL, event TEXT NOT NULL,"
                       " n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, event))")


def _connect(db: Path):
    return database.connect(db, _setup)


def get(db: Path | None, key: str, max_age: float):
    """The saved value, or None when it's missing, expired or the database is unavailable."""
    if db is None:
        return None
    try:
        with _connect(db) as connection:
            row = connection.execute("SELECT value, created FROM cache WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)
        return None
    if row is None or time.time() - row[1] > max_age:
        return None
    return json.loads(row[0])


def put(db: Path | None, key: str, value, words: str = "") -> None:
    if db is None:
        return
    try:
        with _connect(db) as connection:
            connection.execute("INSERT OR REPLACE INTO cache (key, words, value, created) VALUES (?, ?, ?, ?)",
                               (key, words, json.dumps(value), time.time()))
            if time.time() % 50 < 1:  # now and then, clear out expired rows
                connection.execute("DELETE FROM cache WHERE created < ?", (time.time() - _LONGEST,))
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)


def forget_topic(db: Path | None, words: str) -> int:
    """Drop every saved answer, plan and ranking for a topic, so the next search works it out again; how many."""
    if db is None or not words:
        return 0
    try:
        with _connect(db) as connection:
            return connection.execute("DELETE FROM cache WHERE words = ?", (words,)).rowcount
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)
        return 0


def answers_by_entry(db: Path | None) -> dict[int, set[str]]:
    """For each research library entry, the topics of the saved answers built on it (found under any name)."""
    found: dict[int, set[str]] = {}
    for words, answer, _created in by_prefix(db, "result:", _LONGEST):
        entry_id = ((answer or {}).get("library") or {}).get("id") if isinstance(answer, dict) else None
        if isinstance(entry_id, int) and words:
            found.setdefault(entry_id, set()).add(words)
    return found


def remember(db: Path | None, key: str, fetch, max_age: float, words: str = "", event: str = ""):
    """The saved value for `key` if younger than `max_age`, or `fetch()`'s result, saved for next time.
    With `event`, counts "<event>_saved" or "<event>_fetched" for the admin page's Activity view."""
    value = get(db, key, max_age)
    if value is None:
        value = fetch()
        put(db, key, value, words)
        count(db, f"{event}_fetched") if event else None
    elif event:
        count(db, f"{event}_saved")
    return value


# ---- Activity counts: how often each step was skipped -----------------------------------------------------------

def count(db: Path | None, event: str) -> None:
    """Add one to today's count for `event` (e.g. "answer_saved", "plan_ai"); never fails a search."""
    if db is None:
        return
    try:
        with _connect(db) as connection:
            connection.execute("INSERT INTO activity (day, event, n) VALUES (?, ?, 1)"
                               " ON CONFLICT (day, event) DO UPDATE SET n = n + 1",
                               (time.strftime("%Y-%m-%d", time.gmtime()), event))
    except sqlite3.Error as exc:
        print(f"Activity not counted: {exc}", file=sys.stderr)


def activity(db: Path, days: int = 14) -> dict:
    """{day: {event: n}} for the last `days` days, newest first."""
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
    try:
        with _connect(db) as connection:
            rows = connection.execute("SELECT day, event, n FROM activity WHERE day >= ? ORDER BY day DESC",
                                      (since,)).fetchall()
    except sqlite3.Error:
        return {}
    result: dict = {}
    for day, event, n in rows:
        result.setdefault(day, {})[event] = n
    return result


def by_prefix(db: Path | None, prefix: str, max_age: float, suffix: str = "") -> list[tuple[str, object, float]]:
    """(words, value, created) for every unexpired value whose key starts with `prefix` (and ends with `suffix`),
    newest first. The suffix is matched in SQL, so values that can't match are never decoded."""
    if db is None:
        return []
    try:
        with _connect(db) as connection:
            sql, params = "SELECT words, value, created FROM cache WHERE key >= ? AND key < ? AND created >= ?", [
                prefix, prefix + "\uffff", time.time() - max_age]
            if suffix:
                sql += " AND substr(key, -?) = ?"
                params += [len(suffix), suffix]
            rows = connection.execute(sql + " ORDER BY created DESC", params).fetchall()
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)
        return []
    return [(words, json.loads(value), created) for words, value, created in rows]
