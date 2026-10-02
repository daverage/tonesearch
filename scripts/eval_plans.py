"""Run plan_tone against the configured AI with saved research notes, and check it for known failures.

    python scripts/eval_plans.py                 # every case, using scripts/eval_notes/<case>.txt
    python scripts/eval_plans.py tom-quayle      # one case
    python scripts/eval_plans.py --refresh       # run web research again first and save its notes

The AI is configured as for the app (NAM_MIXER_AI_* env vars). Hosted models vary from run to run, so this
reports rather than gates CI: unit tests cover what Python does to a plan, this covers what the model does.
Each run's plans and results are written to $TONESEARCH_DATA_DIR/eval/<time>.json for inspection.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tonesearch import ai, research  # noqa: E402

NOTES_DIR = ROOT / "scripts" / "eval_notes"
OUT_DIR = Path(os.environ.get("TONESEARCH_DATA_DIR", ROOT / "data")) / "eval"


def _gear(plan, pattern):
    return [g for g in plan["gear"] if re.search(pattern, g["name"], re.I)]


def _when(notes, pattern, check):
    """Run `check` only when the research mentions `pattern`: the model can't be blamed for missing evidence."""
    return check() if re.search(pattern, notes, re.I) else None


def _named_as_in_notes(plan, notes, pattern):
    """Every gear name matching `pattern` appears word for word in the notes: no invented variants."""
    return all(g["name"].lower() in notes.lower() for g in _gear(plan, pattern))


CASES = {
    "hillsong-united-we-stand": ("Hillsong United United We Stand", [
        ("a modern Strat is not best-match 2006 gear", lambda p, n: not any(
            g["confidence"] == "best" for g in _gear(p, r"american (professional|ultra|performer)|player (ii|plus)"))),
        ("Vox AC30 named only as specifically as the notes", lambda p, n: _when(
            n, r"ac30", lambda: _named_as_in_notes(p, n, r"ac30"))),
    ]),
    "tom-quayle": ("Tom Quayle", [
        # A weak aggregator page does call it a "Laney Lionheart 60-watt signature amplifier": only a name the
        # notes don't use counts as synthesised.
        ("no synthesised 'Lionheart 60-watt'", lambda p, n: not any(
            g["name"].lower() not in n.lower() for g in _gear(p, r"lionheart.*\d+\s*-?\s*w(att)?\b"))),
        ("keeps his Ibanez signature guitar", lambda p, n: _when(n, r"tqm", lambda: bool(_gear(p, r"tqm")))),
    ]),
    "knights-of-cydonia": ("Knights of Cydonia", [
        ("keeps the Diezel VH4", lambda p, n: _when(n, r"diezel", lambda: bool(_gear(p, r"diezel\s*vh-?4")))),
        ("keeps the ZVEX Fuzz Factory", lambda p, n: _when(n, r"fuzz factory", lambda: bool(_gear(p, r"fuzz factory")))),
        ("no generic gear names", lambda p, n: not _gear(p, r"^(distortion|fuzz|overdrive|high.gain) (device|pedal|amp)")),
    ]),
    "stereo-pedal-platform": ("amp cab combo good pedal platform gigging, stereo", [
        ("Roland JC-40 or JC-120 is a best match", lambda p, n: _when(
            n, r"jc-?(40|120)", lambda: any(g["confidence"] == "best" for g in _gear(p, r"jc-?(40|120)")))),
        ("no Marshall Plexi as a best match", lambda p, n: not any(
            g["confidence"] == "best" for g in _gear(p, r"plexi|super lead|1959"))),
        ("a Simplifier is an alternative, not a combo", lambda p, n: all(
            g["confidence"] == "alternative" for g in _gear(p, r"simplifier"))),
        ("no Tube Screamer or speakers", lambda p, n: not _gear(p, r"tube ?screamer|ts-?808|ts-?9|celestion")),
    ]),
    "periphery-axe-fx": ("Misha Mansoor Periphery guitar tone", [
        ("keeps the documented Axe-Fx", lambda p, n: _when(n, r"axe-?fx", lambda: bool(_gear(p, r"axe-?fx")))),
    ]),
    # The notes say Hetfield used Bob Rock's Jose-modded Marshall with his Boogie Mark II; one live plan dropped the
    # Boogie and swapped in a stock JCM800 and a JCM900 that no source names.
    "metallica-black-album": ("guitar metallica's black album", [
        ("keeps the Boogie Mark II", lambda p, n: _when(n, r"boogie", lambda: bool(_gear(p, r"boogie|mesa")))),
        ("keeps the Jose-modded Marshall in the notes' words", lambda p, n: _when(
            n, r"jose", lambda: bool(_gear(p, r"jose|modded|modified")))),
        ("gear the notes don't name is only an alternative", lambda p, n: all(
            g["confidence"] == "alternative" for g in p["gear"] if not ai.named_in(g["name"], n))),
    ]),
    # The research is all "best microphone" lists, but the player wants a DI classical guitar to sound miked.
    "classical-di-to-mic": ("makes direct input classical guitar more guitar condenser microphone", [
        ("no search names a microphone", lambda p, n: not any(
            re.search(_MICS, q, re.I) for q in p["search_queries"])),
        ("searches for the classical or acoustic sound", lambda p, n: any(
            re.search(r"classical|nylon|acoustic", q, re.I) for q in p["search_queries"])),
        ("the 'Alternative' mics aren't best matches", lambda p, n: not any(
            g["confidence"] == "best" for g in _gear(p, r"m296|sf2"))),
        ("classical and acoustic captures outrank speaker cabs recorded with the mic", lambda p, n: _outranks(
            p, {80462, 71126, 55147}, {40962, 43898})),
    ]),
}
_MICS = r"oc818|m296|royer|sf2|se8|se electronics|dpa|4011|cb100|at2020|condenser|microphone"
# Real TONE3000 packs for ranking checks: what each pack captures, and products its description merely mentions.
RANK_PACKS = {
    "classical-di-to-mic": [
        {"id": 80462, "title": "TAKAMINE DH90 CLASSICAL ACOUSTIC GUITAR", "gear": "experimental",
         "tags": ["classical acoustic guitar", "violo nylon"], "description": "A capture for nylon-string classical "
         "guitars whose pickup sounds poor: an LA-2A, an EQ and an impulse response recorded from a DH90 with a "
         "Neumann KM184."},
        {"id": 71126, "title": "BOSS ACOUSTIC SIMULATOR AC-3", "gear": "pedal",
         "tags": ["acoustic guitar model", "acoustic simulator"], "description": "Each mode of the AC-3 at noon."},
        {"id": 55147, "title": "Ibanez AEG50N (Classical)", "gear": "experimental", "tags": ["nylon", "ir"],
         "description": "I captured my Ibanez AEG50N classical acoustic guitar with an AKG P120 mic."},
        {"id": 40962, "title": "Celestion Vintage 30 nam captured", "gear": "amp-cab", "tags": ["cab", "celestion"],
         "description": "Celestion Vintage 30 speaker in a 1x12 cab, miked with an Austrian Audio OC818."},
        {"id": 43898, "title": "Greenback nam captured", "gear": "amp-cab", "tags": ["cab", "greenback"],
         "description": "Celestion Greenback speaker captured with an Austrian Audio OC818 condenser microphone."},
    ],
}


def _outranks(plan, wanted, unwanted):
    """Every wanted pack scores above every unwanted one in the plan's ranking."""
    fits = plan.get("ranking") or {}
    if not fits:
        return None
    return min(fits.get(i, 0) for i in wanted) > max(fits.get(i, 0) for i in unwanted)


def _unsupported(plan, notes):
    """Gear names with words the notes never use: often the model's own knowledge, sometimes an invention."""
    text = set(re.findall(r"[a-z0-9]+", notes.lower()))
    return {g["name"]: missing for g in plan["gear"]
            if (missing := [w for w in re.findall(r"[a-z0-9]+", g["name"].lower()) if len(w) > 1 and w not in text])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cases", nargs="*", help=f"cases to run (default: all): {', '.join(CASES)}")
    parser.add_argument("--refresh", action="store_true", help="run web research again and save the notes")
    args = parser.parse_args()
    names = args.cases or list(CASES)
    if unknown := [name for name in names if name not in CASES]:
        parser.error(f"unknown case {', '.join(unknown)}")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    if args.refresh:
        for name in names:
            try:
                notes = research.web_notes(CASES[name][0])
            except RuntimeError as exc:
                print(f"{name}: {exc} (notes kept as they were)", file=sys.stderr)
                continue
            (NOTES_DIR / f"{name}.txt").write_text(notes + "\n")
            print(f"saved notes for {name} ({len(notes)} chars)")
    if not ai.is_configured():
        print("No AI configured: set NAM_MIXER_AI_* as for the app.", file=sys.stderr)
        return 2
    results, failed = {}, 0
    for name in names:
        prompt, checks = CASES[name]
        path = NOTES_DIR / f"{name}.txt"
        if not path.exists():
            print(f"\n{name}: skipped, no saved notes (run with --refresh)", file=sys.stderr)
            continue
        notes = path.read_text()
        started = time.monotonic()
        try:
            plan = ai.plan_tone(prompt, research_notes=notes)
            if name in RANK_PACKS:  # rank a fixed set of real packs against the plan, as the site would
                ranked = ai.rank_packs(prompt, ai.gear_summary(plan), RANK_PACKS[name])
                plan["ranking"] = {pack_id: score["fit"] for pack_id, score in ranked.items()}
        except ai.AiError as exc:
            print(f"\n{name}: ERROR {exc}")
            results[name] = {"prompt": prompt, "error": str(exc)}
            failed += 1
            continue
        outcomes = {label: check(plan, notes) for label, check in checks}
        failed += sum(ok is False for ok in outcomes.values())
        results[name] = {"prompt": prompt, "seconds": round(time.monotonic() - started, 1), "plan": plan,
                         "checks": outcomes, "names_not_in_notes": _unsupported(plan, notes)}
        print(f"\n{name}: {prompt!r}")
        for g in plan["gear"]:
            print(f"  {g['kind']:7} {g['name']} | {g['confidence']} | {g.get('role', '')}")
        print(f"  searches: {plan['search_queries']}  aliases: {plan['aliases']}")
        if plan.get("ranking"):
            titles = {p["id"]: p["title"] for p in RANK_PACKS[name]}
            for pack_id, fit in sorted(plan["ranking"].items(), key=lambda item: -item[1]):
                print(f"  rank {fit:3}  {titles.get(pack_id, pack_id)}")
        for label, ok in outcomes.items():
            print(f"  {'n/a ' if ok is None else 'pass' if ok else 'FAIL'}  {label}")
        for gear_name, missing in results[name]["names_not_in_notes"].items():
            print(f"  note  {gear_name!r}: {', '.join(missing)} not in the notes")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"model": ai.config().model, "results": results}, indent=2))
    print(f"\n{failed} check(s) failed; plans saved to {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
