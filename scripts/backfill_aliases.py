"""Fill in the other names (aliases) of research library entries that were saved without them.

    python scripts/backfill_aliases.py               # dry run: what each entry would get, nothing saved
    python scripts/backfill_aliases.py --yes         # save them
    python scripts/backfill_aliases.py --id 12 --yes # one entry
    python scripts/backfill_aliases.py --replace     # also redo entries that already have aliases (not approved ones)

It asks the AI configured for the app (NAM_MIXER_AI_* environment variables) for other names for each entry's
topic, given its research notes and gear, and keeps only the names the same rules as a new search allow: named in
the request or the research, not gear, genres or other players. Data lives in $TONESEARCH_DATA_DIR (default: the
data/ folder next to app.py), so run it with the app's environment. Each entry is one AI request.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tonesearch import ai, knowledge  # noqa: E402

DB = Path(os.environ.get("TONESEARCH_DATA_DIR", ROOT / "data")) / "knowledge.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="save the aliases (without it, only show them)")
    parser.add_argument("--id", type=int, help="only this entry")
    parser.add_argument("--replace", action="store_true",
                        help="also redo entries that already have aliases; approved entries are never changed")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many entries")
    args = parser.parse_args()
    if not DB.exists():
        print(f"No research library at {DB}.", file=sys.stderr)
        return 1
    if not ai.is_configured():
        print("No AI configured: set NAM_MIXER_AI_* as for the app.", file=sys.stderr)
        return 2
    entries = [knowledge.get(DB, args.id)] if args.id else knowledge.search(DB, limit=100_000)
    todo = [e for e in entries if e and (not e.get("aliases") or (args.replace and e["status"] != "approved"))]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} of {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} to do"
          f"{'' if args.yes else ' (dry run: add --yes to save)'}; AI: {ai.config().model}\n")
    saved = failed = 0
    for entry in todo:
        try:
            aliases = ai.suggest_aliases(entry["topic"], entry["notes"], entry["gear"])
        except ai.AiError as exc:
            print(f"#{entry['id']} {entry['topic']}: AI failed ({exc})")
            failed += 1
            continue
        before = f" (was: {entry['aliases']})" if entry.get("aliases") else ""
        print(f"#{entry['id']} {entry['topic']}: {', '.join(aliases) or 'no other names'}{before}")
        if not args.yes or not aliases:
            continue
        if entry.get("aliases"):  # --replace: keep everything else as it is
            knowledge.update(DB, entry["id"], topic=entry["topic"], notes=entry["notes"], gear=entry["gear"],
                             status=entry["status"], aliases=aliases)
        else:
            knowledge.add_aliases(DB, entry["id"], aliases)
        saved += 1
    if args.yes:
        print(f"\nSaved aliases for {saved} entr{'y' if saved == 1 else 'ies'}; {failed} failed.")
    return 1 if failed and not saved else 0


if __name__ == "__main__":
    sys.exit(main())
