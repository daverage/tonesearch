"""Short-lived saved answers, so repeat searches skip the AI, the web and TONE3000.

Values live in the research library's database for TONESEARCH_CACHE_DAYS (3 by default). Each row can carry a
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

CACHE_SECONDS = float(os.environ.get("TONESEARCH_CACHE_DAYS", "3")) * 86400


def _connect(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, timeout=5)
    connection.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, words TEXT NOT NULL DEFAULT '',"
                       " value TEXT NOT NULL, created REAL NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS cache_words ON cache (words)")
    return connection


def get(db: Path | None, key: str, max_age: float | None = None):
    """The saved value, or None when it's missing, expired or the database is unavailable."""
    if db is None:
        return None
    try:
        with _connect(db) as connection:
            row = connection.execute("SELECT value, created FROM cache WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)
        return None
    if row is None or time.time() - row[1] > (CACHE_SECONDS if max_age is None else max_age):
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
                connection.execute("DELETE FROM cache WHERE created < ?", (time.time() - CACHE_SECONDS,))
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)


def forget_topic(db: Path | None, words: str) -> None:
    """Drop every saved answer for a topic, so the next search works it out again."""
    if db is None or not words:
        return
    try:
        with _connect(db) as connection:
            connection.execute("DELETE FROM cache WHERE words = ?", (words,))
    except sqlite3.Error as exc:
        print(f"Cache unavailable: {exc}", file=sys.stderr)


def remember(db: Path | None, key: str, fetch, words: str = ""):
    """The saved value for `key`, or `fetch()`'s result, saved for next time."""
    value = get(db, key)
    if value is None:
        value = fetch()
        put(db, key, value, words)
    return value
