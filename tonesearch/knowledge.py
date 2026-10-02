"""The research library: web research notes and the gear taken from them, saved per tone topic and reused.

A new search first looks for a stored entry whose topic words strongly overlap its own; only a miss goes to the
web. Entries are reviewed on the admin page: approved ones never expire, unreviewed ones expire after
TONESEARCH_LIBRARY_DAYS, and a visitor's "wrong research" flag keeps an entry out of use until it's reviewed.

Matching is deliberately conservative: a missed reuse only costs fresh research, while a false match can attach
one artist/song/era's research to another and contaminate later answers.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from tonesearch import cache
from tonesearch import db as database
from tonesearch.research import _topic

STATUSES = ("new", "approved", "flagged")
UNREVIEWED_DAYS = float(os.environ.get("TONESEARCH_LIBRARY_DAYS", "30"))
MATCH_THRESHOLD = 0.75  # shared identity words over all identity words (Jaccard)

# Words that say nothing about which tone is meant, so they never decide a match.
_GENERIC = {"guitar", "guitars", "amp", "amps", "rig", "gear", "setup", "settings", "song", "album", "record",
            "track", "band", "sound", "tone", "tones", "style", "like", "vibe", "the", "and", "for"}


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


def topic_words(text: str) -> list[str]:
    """The words that identify a tone request, lowercased, sorted and without filler."""
    words = {w.strip(".'’-") for w in re.findall(r"[\w'’.-]+", _topic(text).lower())}
    return sorted(w for w in words if len(w) > 1 and w not in _GENERIC)


def identity_words(words) -> set[str]:
    """The words that say whose rig a request is about: topic words without sound descriptions."""
    return {w for w in words if w not in _DESCRIPTIONS}


def clean_aliases(aliases) -> list[str]:
    """Up to 12 short alternate names, kept as phrases rather than one shared bag of words."""
    cleaned = []
    for alias in aliases if isinstance(aliases, list) else []:
        words = [w for w in topic_words(str(alias)) if w not in _DESCRIPTIONS]
        phrase = " ".join(words)
        if phrase and len(phrase) <= 60:
            cleaned.append(phrase)
    return list(dict.fromkeys(cleaned))[:12]


def _alias_phrases(aliases: str) -> list[set[str]]:
    """Existing DB format is comma-separated phrases; keep each phrase isolated while matching.

    This deliberately supports the current schema, so no migration is required. Older rows such as
    "adam getgood, nolly, periphery" continue to work, but words from separate aliases are never combined into
    one synthetic identity.
    """
    phrases = []
    for raw in str(aliases or "").split(","):
        words = identity_words(topic_words(raw.strip()))
        if words:
            phrases.append(words)
    return phrases


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _match_details(text: str, words: str, aliases: str = "", *, same_sound: bool = False) -> tuple[float, int, bool]:
    """Return (score, matched identity-word count, direct-topic match).

    Direct topic overlap is preferred. Alias matching is deliberately conservative and checks each alias phrase
    independently, optionally alongside the stored topic words. It never pools words from unrelated aliases.
    """
    asked = topic_words(text)
    wanted = identity_words(asked)
    theirs = identity_words(words.split())
    if not wanted or not theirs:
        return 0.0, 0, False

    # Saved briefs can require the same requested sound as well as the same rig. Research lookup normally leaves
    # same_sound=False so added adjectives such as "compressed" still reuse the same underlying rig research.
    if same_sound and set(asked) - wanted - set(words.split()):
        return 0.0, 0, False

    overlap = _jaccard(wanted, theirs)
    if overlap >= MATCH_THRESHOLD:
        return overlap, len(wanted & theirs), True

    # Aliases are alternate names for this subject, not a vocabulary pool. Compare one phrase at a time. The stored
    # topic words may accompany an alias so "Adam Getgood bass" can match topic "Periphery bass" + alias
    # "Adam Getgood", but "Nolly Getgood bass" cannot be assembled from aliases "Nolly" and "Adam Getgood".
    if len(wanted) >= 2:
        for alias in _alias_phrases(aliases):
            known = theirs | alias
            if wanted <= known:
                return MATCH_THRESHOLD - 0.01, len(wanted), False
            alias_overlap = _jaccard(wanted, known)
            if alias_overlap >= MATCH_THRESHOLD:
                return MATCH_THRESHOLD - 0.01, len(wanted & known), False

    return 0.0, 0, False


def match_score(text: str, words: str, aliases: str = "", *, same_sound: bool = False) -> float:
    """How well a request matches saved work for `words` (plus individual aliases); 0 when it doesn't.

    A strong direct identity match wins. Alias matching requires at least two requested identity words and compares
    against one alias phrase at a time, so unrelated aliases can no longer combine into a false match. With
    `same_sound`, the request may not add description words the saved work lacks ("warm and clean" wants its own
    saved brief even when the underlying rig research can still be reused).
    """
    return _match_details(text, words, aliases, same_sound=same_sound)[0]


def _setup(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY, topic TEXT NOT NULL, words TEXT NOT NULL UNIQUE, notes TEXT NOT NULL,
        gear TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new', flags INTEGER NOT NULL DEFAULT 0,
        flag_reason TEXT NOT NULL DEFAULT '', uses INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
        updated REAL NOT NULL)""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(entries)")}
    if "aliases" not in columns:  # added later: the AI's names for the same rig ("nolly", "steven wilson")
        connection.execute("ALTER TABLE entries ADD COLUMN aliases TEXT NOT NULL DEFAULT ''")


def _connect(db: Path):
    return database.connect(db, _setup, rows=True)


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
    """The best usable entry for a request, or None when identity overlap is not strong enough.

    False positives are deliberately more expensive than misses: if the subject is uncertain, fresh research is
    safer than reusing another artist/song/era's rig. Exact/direct matches beat alias matches; where scores tie,
    more matched identity words win, then reviewed entries win.
    """
    if not identity_words(topic_words(text)):
        return None
    oldest = time.time() - UNREVIEWED_DAYS * 86400
    best, best_key = None, (0.0, False, 0, False)
    with _connect(db) as connection:
        # Only what matching needs: notes and gear are read for the winner alone.
        rows = connection.execute("SELECT id, words, aliases, status FROM entries"
                                  " WHERE status = 'approved' OR (status = 'new' AND updated >= ?)", (oldest,))
        for row in rows:
            score, matched_words, direct = _match_details(text, row["words"], row["aliases"])
            # Direct topic evidence is safer than alias expansion. Specificity then breaks equal-score matches;
            # approved status is deliberately last so a broader approved row cannot beat a more specific direct row.
            key = (score, direct, matched_words, row["status"] == "approved")
            if score and key > best_key:
                best, best_key = row, key
        if best is None:
            return None
        if count_use:
            connection.execute("UPDATE entries SET uses = uses + 1 WHERE id = ?", (best["id"],))
        entry = connection.execute("SELECT * FROM entries WHERE id = ?", (best["id"],)).fetchone()
    return {**_entry(entry), "score": round(best_key[0], 2)}


def saved_answer(db: Path, text: str, *, suffix: str = "") -> tuple[str, dict, float] | None:
    """The website's saved answer for the same rig, found under its topic or any of its aliases: (words, answer,
    created), or None. `suffix` narrows the saved answers by the end of their key (research setting and filters).
    A request that describes a sound the saved one didn't gets no saved answer (see match_score's same_sound)."""
    best, best_score = None, 0.0
    for words, answer, created in cache.by_prefix(db, "result:", cache.CATALOGUE_SECONDS, suffix):
        if not isinstance(answer, dict):
            continue
        aliases = ", ".join(clean_aliases((answer.get("plan") or {}).get("aliases")))
        score = match_score(text, words, aliases, same_sound=True)
        if score > best_score:  # rows come newest first, so ties keep the newest
            best, best_score = (words, answer, created), score
    return best


@_best_effort
def save(db: Path, text: str, notes: str, gear: list | None = None, aliases: list | None = None) -> int | None:
    """Store fresh research for a topic. An approved entry is never overwritten; others are refreshed."""
    words = " ".join(topic_words(text))
    if not words or not notes.strip():
        return None
    now = time.time()
    gear_json = json.dumps((gear or [])[:12])
    alias_words = ", ".join(clean_aliases(aliases))
    with _connect(db) as connection:
        row = connection.execute("SELECT id, status, gear, aliases FROM entries WHERE words = ?", (words,)).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO entries (topic, words, notes, gear, aliases, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_topic(text)[:200], words, notes[:6000], gear_json, alias_words, now, now))
            return cursor.lastrowid
        if row["status"] != "approved":  # a flagged entry stays flagged until the owner reviews it
            connection.execute("UPDATE entries SET notes = ?, gear = ?, aliases = ?, updated = ? WHERE id = ?",
                               (notes[:6000], gear_json if gear else row["gear"], alias_words or row["aliases"],
                                now, row["id"]))
        return row["id"]


@_best_effort
def add_aliases(db: Path, entry_id: int, aliases: list) -> None:
    """Fill in an entry's aliases when it has none (MCP research, or research saved before aliases existed)."""
    alias_words = ", ".join(clean_aliases(aliases))
    if alias_words:
        with _connect(db) as connection:
            connection.execute("UPDATE entries SET aliases = ? WHERE id = ? AND aliases = ''", (alias_words, entry_id))


@_best_effort
def add_gear(db: Path, entry_id: int, gear: list) -> None:
    """Fill in the gear for an entry saved without it (MCP research has no AI on the server to read the notes)."""
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

def counts(db: Path) -> dict:
    """How many entries there are in all and with each status, for the admin page's filter."""
    with _connect(db) as connection:
        found = dict(connection.execute("SELECT status, COUNT(*) FROM entries GROUP BY status").fetchall())
    return {"all": sum(found.values()), **{status: found.get(status, 0) for status in STATUSES}}


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
