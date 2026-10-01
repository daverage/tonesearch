"""Clear TONE Search's saved data: research library, saved answers, votes, activity counts and rate limits.

    python scripts/reset_data.py                    # show what is stored; changes nothing
    python scripts/reset_data.py --all --yes        # start completely fresh
    python scripts/reset_data.py --votes --yes      # just the votes (also --library, --answers, --activity, --limits)

Data lives in $TONESEARCH_DATA_DIR (default: the data/ folder next to app.py), so run it with the same environment
as the app. Rows are deleted rather than files, so it is safe while the app is running. Clearing the library also
clears saved answers, which were built on it.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TONESEARCH_DATA_DIR", ROOT / "data"))
# option: (database file, tables, description)
PARTS = {
    "library": ("knowledge.sqlite3", ("entries",), "research library entries"),
    "answers": ("knowledge.sqlite3", ("cache",), "saved answers, plans, rankings and TONE3000 results"),
    "votes": ("knowledge.sqlite3", ("feedback",), "votes on briefs and packs"),
    "activity": ("knowledge.sqlite3", ("activity",), "daily activity counts"),
    "limits": ("limits.sqlite3", None, "hourly rate-limit counters"),  # None: every table in the file
}


def _tables(db: Path, names) -> dict:
    """Row counts for the named tables (or every table) that exist in `db`."""
    if not db.exists():
        return {}
    with sqlite3.connect(db) as connection:
        existing = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for name in existing if names is None or name in names}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, (_, _, description) in PARTS.items():
        parser.add_argument(f"--{name}", action="store_true", help=f"clear the {description}")
    parser.add_argument("--all", action="store_true", help="clear everything")
    parser.add_argument("--yes", action="store_true", help="actually delete (without it, only show what would go)")
    args = parser.parse_args()
    chosen = [name for name in PARTS if args.all or getattr(args, name)]
    if "library" in chosen and "answers" not in chosen:
        chosen.append("answers")  # saved answers point at library entries that would no longer exist

    print(f"Data folder: {DATA_DIR}")
    for name, (file, tables, description) in PARTS.items():
        counts = _tables(DATA_DIR / file, tables)
        mark = "  will clear" if name in chosen else ""
        print(f"  {description:<55} {sum(counts.values()):>7} rows{mark}")
    if not chosen:
        print("\nNothing chosen. Add --all, or --library/--answers/--votes/--activity/--limits, and --yes.")
        return 0
    if not args.yes:
        print("\nDry run: add --yes to delete.")
        return 0
    for name in chosen:
        file, tables, description = PARTS[name]
        db = DATA_DIR / file
        counts = _tables(db, tables)
        if not counts:
            continue
        with sqlite3.connect(db) as connection:
            for table in counts:
                connection.execute(f'DELETE FROM "{table}"')
        print(f"Cleared the {description} ({sum(counts.values())} rows).")
    for file in {PARTS[name][0] for name in chosen}:
        if (DATA_DIR / file).exists():
            with sqlite3.connect(DATA_DIR / file) as connection:
                connection.execute("VACUUM")  # give the space back
    return 0


if __name__ == "__main__":
    sys.exit(main())
