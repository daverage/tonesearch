"""Share links: a snapshot of a visitor's searches that anyone with the link can open.

Every answer `/api/search` gives is kept for ANSWER_SECONDS under a random id (`remember`). A share names those ids
in order (`create`), so a snapshot only ever holds what this server produced: a visitor can't put their own text or
links on the site under its name. Snapshots never expire. Ids are random, so links can't be guessed or listed.
Like the cache, answers are best-effort: a database error means the answer can't be shared, never a failed search.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from tonesearch import db as database

ANSWER_SECONDS = 3 * 86400  # how long after a search it can still be shared
MAX_TURNS = 20  # searches and filter changes in one link


def _setup(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS answers (id TEXT PRIMARY KEY, value TEXT NOT NULL,"
                       " created REAL NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, value TEXT NOT NULL,"
                       " created REAL NOT NULL)")


def _new_id() -> str:
    return secrets.token_urlsafe(9)  # 12 characters


def remember(db: Path, prompt: str, kind: str, answer: dict) -> str:
    """Keep one answer so it can be shared; its id, or "" when the database is unavailable."""
    answer_id = _new_id()
    try:
        with database.connect(db, _setup) as connection:
            connection.execute("INSERT INTO answers (id, value, created) VALUES (?, ?, ?)",
                               (answer_id, json.dumps({"prompt": prompt, "kind": kind, "data": answer}), time.time()))
            if time.time() % 50 < 1:  # now and then, clear out answers too old to share
                connection.execute("DELETE FROM answers WHERE created < ?", (time.time() - ANSWER_SECONDS,))
    except sqlite3.Error as exc:
        print(f"Share store unavailable: {exc}", file=sys.stderr)
        return ""
    return answer_id


def create(db: Path, refs: list) -> str:
    """A share of these answers, in order; its id. Each ref is an answer id, or "<share id>/<turn>" for a turn of
    an earlier share, so a visitor who opened a link and searched on can share the lot. RuntimeError when one has
    expired or never existed."""
    oldest = time.time() - ANSWER_SECONDS
    with database.connect(db, _setup) as connection:
        turns = []
        for ref in refs:
            share_id, _, index = ref.partition("/")
            if index:
                row = connection.execute("SELECT value FROM shares WHERE id = ?", (share_id,)).fetchone()
                earlier = json.loads(row[0])["turns"] if row else []
                turn = earlier[int(index)] if index.isdigit() and int(index) < len(earlier) else None
            else:
                row = connection.execute("SELECT value FROM answers WHERE id = ? AND created >= ?",
                                         (ref, oldest)).fetchone()
                turn = json.loads(row[0]) if row else None
            if turn is None:
                raise RuntimeError("Some of these searches are too old to share. Search again, then share.")
            turns.append(turn)
        share_id = _new_id()
        connection.execute("INSERT INTO shares (id, value, created) VALUES (?, ?, ?)",
                           (share_id, json.dumps({"turns": turns}), time.time()))
    return share_id


def get(db: Path, share_id: str) -> dict | None:
    """The snapshot ({"turns": [...], "created": time}), or None."""
    try:
        with database.connect(db, _setup) as connection:
            row = connection.execute("SELECT value, created FROM shares WHERE id = ?", (share_id,)).fetchone()
    except sqlite3.Error as exc:
        print(f"Share store unavailable: {exc}", file=sys.stderr)
        return None
    return {**json.loads(row[0]), "created": row[1]} if row else None
