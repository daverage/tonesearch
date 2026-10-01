"""Run web_notes on real web searches and record what it found, to compare research before and after a change.

    python scripts/eval_research.py                  # every case
    python scripts/eval_research.py "Tom Quayle"     # any requests you like
    python scripts/eval_research.py --compare A.json B.json

For each request it records the searches made, the results returned, which pages gave evidence and which notes
fell back to the search engine's snippet, and the notes themselves. Search results change from day to day, so
this is for reading side by side, not a pass/fail test. Runs are saved to $TONESEARCH_DATA_DIR/eval/research-<time>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tonesearch import research  # noqa: E402

OUT_DIR = Path(os.environ.get("TONESEARCH_DATA_DIR", ROOT / "data")) / "eval"
CASES = [
    "Hillsong United We Stand",
    "Tom Quayle",
    "Knights of Cydonia",
    "stereo pedal platform combo gigging",
    "Periphery bass",
    "AC30",
    "Mick Barr Orthrelm guitar tone",  # an obscure guitarist
    "Benson Chimera amp",  # a less common amp
]


def run(request: str) -> dict:
    searches, pages, extractors = [], {}, {}
    main_text = research._main_text

    def counted(html, url="", forum=False):  # which extractor read each page: trafilatura or the fallback parser
        text, extractor = main_text(html, url, forum)
        extractors[url] = extractor
        return text, extractor

    def search(query, max_results, **kwargs):
        found = research._ddgs_search(query, max_results, **kwargs)
        searches.append({"query": query, "backend": kwargs.get("backend"), "results": [r.get("href") for r in found]})
        return found

    def evidence(href, topic):
        pages[href] = research._page_evidence(href, topic)
        return pages[href]

    started = time.monotonic()
    research._main_text = counted
    try:
        notes, error = research.web_notes(request, search=search, evidence=evidence), None
    except RuntimeError as exc:
        notes, error = "", str(exc)
    finally:
        research._main_text = main_text
    lines = notes.splitlines()
    from_snippet = [line for line in lines if not any(pages.get(href) and href in line for href in pages)]
    return {"request": request, "topic": research._topic(request), "seconds": round(time.monotonic() - started, 1),
            "searches": searches, "pages_with_evidence": [h for h, text in pages.items() if text],
            "pages_without": [h for h, text in pages.items() if not text], "notes": lines,
            "notes_from_snippets": len(from_snippet), "characters": len(notes), "error": error,
            "extractors": extractors}


def show(result: dict) -> None:
    print(f"\n== {result['request']!r} -> topic {result['topic']!r} ({result['seconds']}s, {result['characters']} chars)")
    for s in result["searches"]:
        print(f"  search [{s['backend']}] {s['query']!r}: {len(s['results'])} results")
    used = list((result.get("extractors") or {}).values())
    print(f"  pages fetched: {len(result['pages_with_evidence'])} with evidence, {len(result['pages_without'])} without; "
          f"{result['notes_from_snippets']} note(s) from snippets; read by trafilatura {used.count('trafilatura')}, "
          f"parser {used.count('parser')}")
    if result["error"]:
        print(f"  ERROR {result['error']}")
    for line in result["notes"]:
        print(f"  {line}")


def compare(before: Path, after: Path) -> None:
    old = {r["request"]: r for r in json.loads(before.read_text())["results"]}
    for new in json.loads(after.read_text())["results"]:
        prior = old.get(new["request"])
        print(f"\n######## {new['request']}")
        for label, result in (("BEFORE", prior), ("AFTER", new)):
            if result:
                print(f"--- {label}")
                show(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requests", nargs="*", help="requests to research (default: the built-in cases)")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"), help="show two saved runs side by side")
    parser.add_argument("--label", default="", help="a word added to the saved file's name, e.g. before or after")
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return 0
    results = []
    for request in args.requests or CASES:
        results.append(run(request))
        show(results[-1])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"research-{time.strftime('%Y%m%d-%H%M%S')}{'-' + args.label if args.label else ''}.json"
    out.write_text(json.dumps({"results": results}, indent=2))
    print(f"\nsaved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
