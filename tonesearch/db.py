"""SQLite connections for the library, saved answers, votes and rate limits.

Each module passes a `setup` function that creates its tables. It runs once per database file per process, not on
every query: a search touches the database a dozen times. If the tables disappear (the file was replaced), the
next connection sets them up again. Connections are always closed, and committed only when the block succeeds.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

_READY: set = set()  # (database path, setup function) pairs whose tables exist


@contextmanager
def connect(db: Path, setup: Callable[[sqlite3.Connection], None], *, rows: bool = False) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db, timeout=5)
    try:
        if rows:
            connection.row_factory = sqlite3.Row
        key = (str(db), setup)
        if key not in _READY:
            setup(connection)
            _READY.add(key)
        with connection:  # commit, or roll back on an error
            yield connection
    except sqlite3.OperationalError:
        _READY.discard((str(db), setup))  # "no such table": set it up again next time
        raise
    finally:
        connection.close()
