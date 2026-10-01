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


# How a tone sounds, not whose rig it is: "Periphery bass, compressed distorted highs" uses the same bassist's
# gear as "Periphery bass", so research is matched without these. Era, live, song and album words stay, and so
# does "bass" (a bassist's rig is not the guitarist's). Saved briefs and feedback still use every word.
_DESCRIPTIONS = {
    "compressed", "compression", "distorted", "distortion", "overdriven", "driven", "fuzzy", "saturated", "clean",
    "clear", "clarity", "defined", "definition", "articulate", "tight", "loose", "warm", "bright", "dark", "heavy",
    "crunchy", "crunch", "fat", "thick", "thin", "scooped", "mids", "mid", "midrange", "high", "highs", "low", "lows",
    "frequencies", "frequency", "end", "gain", "gainy", "aggressive", "smooth", "sparkly", "chimey", "chimy",
    "punchy", "punch", "growl", "growly", "gritty", "grit", "glassy", "spanky", "woolly", "creamy", "biting",
    "but", "more", "less", "very", "little", "bit", "much", "lot", "slightly", "quite", "lots", "some", "kind",
}


def identity_words(words) -> set:
    """The words that say whose rig a request is about: topic words without sound descriptions."""
    return {w for w in words if w not in _DESCRIPTIONS}


def clean_aliases(aliases) -> list[str]:
    """Up to 12 short names (people, bands, songs, albums, eras) with their description words removed."""
    cleaned = []
    for alias in aliases if isinstance(aliases, list) else []:
        words = [w for w in topic_words(str(alias)) if w not in _DESCRIPTIONS]
        if words and len(" ".join(words)) <= 60:
            cleaned.append(" ".join(words))
    return list(dict.fromkeys(cleaned))[:12]


def match_score(text: str, words: str, aliases: str = "", *, same_sound: bool = False) -> float:
    """How well a request matches saved work for `words` (plus its aliases); 0 when it doesn't.

    Either the two topics' identity words overlap by MATCH_THRESHOLD, or (using the aliases) every one of the
    request's identity words, at least two, is a name the saved work goes by: so "Nolly Getgood bass" finds
    "Periphery bass", but "Periphery" alone and "Nolly bass Juggernaut" (an era it may not cover) don't. With `same_sound`, the request
    may not add description words the saved work lacks ("warm and clean" wants its own brief)."""
    asked = topic_words(text)
    wanted, theirs = identity_words(asked), identity_words(words.split())
    if not wanted or not theirs:
        return 0.0
    if same_sound and set(asked) - wanted - set(words.split()):
        return 0.0
    overlap = len(wanted & theirs) / len(wanted | theirs)
    if overlap >= MATCH_THRESHOLD:
        return overlap
    known = theirs | set(aliases.replace(",", " ").split())
    if len(wanted) >= 2 and wanted <= known:
        return MATCH_THRESHOLD - 0.01  # below any direct overlap
    return 0.0


def _connect(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY, topic TEXT NOT NULL, words TEXT NOT NULL UNIQUE, notes TEXT NOT NULL,
        gear TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new', flags INTEGER NOT NULL DEFAULT 0,
        flag_reason TEXT NOT NULL DEFAULT '', uses INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
        updated REAL NOT NULL)""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(entries)")}
    if "aliases" not in columns:  # added later: the AI's names for the same rig ("nolly", "steven wilson")
        connection.execute("ALTER TABLE entries ADD COLUMN aliases TEXT NOT NULL DEFAULT ''")
    if "intent" not in columns:  # what kind of request found it (ai.INTENTS): decides what gear levels mean
        connection.execute("ALTER TABLE entries ADD COLUMN intent TEXT NOT NULL DEFAULT ''")
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
def find(db: Path, text: str, *, count_use: bool = True) -> dict | None:
    """The best usable entry for a request, or None when its identity words don't overlap strongly enough.

    A request with no identity words (only descriptions, or a band called Low) matches nothing: the failure
    is fresh research, never someone else's rig."""
    if not identity_words(topic_words(text)):
        return None
    oldest = time.time() - UNREVIEWED_DAYS * 86400
    best, best_key = None, (0.0, False)
    with _connect(db) as connection:
        rows = connection.execute(
            "SELECT * FROM entries WHERE status = 'approved' OR (status = 'new' AND updated >= ?)", (oldest,))
        for row in rows:
            score = match_score(text, row["words"], row["aliases"])
            key = (score, row["status"] == "approved")  # ties go to reviewed entries
            if score and key >= best_key:
                best, best_key = row, key
        if best is None:
            return None
        if count_use:
            connection.execute("UPDATE entries SET uses = uses + 1 WHERE id = ?", (best["id"],))
    return {**_entry(best), "score": round(best_key[0], 2)}


@_best_effort
def save(db: Path, text: str, notes: str, gear: list | None = None, aliases: list | None = None,
         intent: str = "") -> int | None:
    """Store fresh research for a topic. An approved entry is never overwritten; others are refreshed."""
    words = " ".join(topic_words(text))
    if not words or not notes.strip():
        return None
    now = time.time()
    gear_json = json.dumps((gear or [])[:12])
    alias_words = ", ".join(clean_aliases(aliases))
    with _connect(db) as connection:
        row = connection.execute("SELECT id, status, gear, aliases, intent FROM entries WHERE words = ?",
                                 (words,)).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO entries (topic, words, notes, gear, aliases, intent, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_topic(text)[:200], words, notes[:6000], gear_json, alias_words, intent, now, now))
            return cursor.lastrowid
        if row["status"] != "approved":  # a flagged entry stays flagged until the owner reviews it
            connection.execute("UPDATE entries SET notes = ?, gear = ?, aliases = ?, intent = ?, updated = ? WHERE id = ?",
                               (notes[:6000], gear_json if gear else row["gear"], alias_words or row["aliases"],
                                intent or row["intent"], now, row["id"]))
        return row["id"]


@_best_effort
def add_aliases(db: Path, entry_id: int, aliases: list) -> None:
    """Fill in an entry's aliases when it has none (MCP research, or research saved before aliases existed)."""
    alias_words = ", ".join(clean_aliases(aliases))
    if alias_words:
        with _connect(db) as connection:
            connection.execute("UPDATE entries SET aliases = ? WHERE id = ? AND aliases = ''", (alias_words, entry_id))


@_best_effort
def add_gear(db: Path, entry_id: int, gear: list, intent: str = "") -> None:
    """Fill in the gear (and what kind of request it answers) for an entry saved without it: MCP research has no
    AI on the server to read the notes."""
    if gear:
        with _connect(db) as connection:
            connection.execute("UPDATE entries SET gear = ?, intent = CASE WHEN intent = '' THEN ? ELSE intent END"
                               " WHERE id = ? AND gear = '[]'", (json.dumps(gear[:12]), intent, entry_id))


def flag(db: Path, entry_id: int, reason: str = "") -> bool:
    """A visitor says the research is wrong: stop using it until the owner reviews it."""
    with _connect(db) as connection:
        cursor = connection.execute(
            "UPDATE entries SET status = 'flagged', flags = flags + 1, flag_reason = ? WHERE id = ?",
            (reason.strip()[:300], entry_id))
        return cursor.rowcount > 0


# ---- Owner review ---------------------------------------------------------------------------------------------

def search(db: Path, text: str = "", status: str = "", limit: int = 200) -> list[dict]:
    """Entries for the admin page: flagged first, then newest. `text` is "#8" for one entry, or words found in
    its topic, notes, gear or topic words (the sorted form the feedback page links with)."""
    sql, params = "SELECT * FROM entries WHERE 1 = 1", []
    if status in STATUSES:
        sql += " AND status = ?"
        params.append(status)
    entry_id = re.fullmatch(r"#(\d+)", text.strip())
    if entry_id:
        sql += " AND id = ?"
        params.append(int(entry_id.group(1)))
    elif text.strip():
        sql += " AND (topic LIKE ? OR notes LIKE ? OR gear LIKE ? OR words LIKE ?)"
        params += [f"%{text.strip()}%"] * 4
    sql += " ORDER BY status = 'flagged' DESC, updated DESC LIMIT ?"
    params.append(limit)
    with _connect(db) as connection:
        return [_entry(row) for row in connection.execute(sql, params)]


def update(db: Path, entry_id: int, *, topic: str, notes: str, gear: list, status: str,
           aliases: list | None = None) -> None:
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
            "UPDATE entries SET topic = ?, words = ?, notes = ?, gear = ?, aliases = ?, status = ?, updated = ?,"
            " flags = CASE WHEN ? = 'flagged' THEN flags ELSE 0 END,"
            " flag_reason = CASE WHEN ? = 'flagged' THEN flag_reason ELSE '' END WHERE id = ?",
            (topic.strip()[:200], words, notes[:6000], json.dumps(gear[:12]), ", ".join(clean_aliases(aliases or [])),
             status, time.time(), status, status, entry_id))


@_best_effort
def get(db: Path, entry_id: int) -> dict | None:
    with _connect(db) as connection:
        row = connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return _entry(row) if row else None


def delete(db: Path, entry_id: int) -> None:
    with _connect(db) as connection:
        connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
