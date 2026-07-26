#!/usr/bin/env python3
"""Run the golden-task suites against one or more open-weight models.

This is the gate from gap G6: no model reaches a write-capable tier without
passing, and every model swap is judged on which cases it *breaks*, not on
whether its prose reads better.

Not part of CI — it needs a real model. Run it before changing the model, the
prompts, or a connector.

Usage:
    python scripts/run_evals.py                                   # default model, all suites
    python scripts/run_evals.py --suite safety
    python scripts/run_evals.py --models ministral-3:8b gemma4:26b --compare
    python scripts/run_evals.py --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uione.evals import render, render_comparison, run_suite  # noqa: E402
from uione.evals.suites import SUITES, fixture_summary  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", nargs="+", default=["ministral-3:8b"])
    parser.add_argument("--suite", choices=sorted(SUITES), default="all")
    parser.add_argument("--compare", action="store_true", help="Side-by-side table.")
    parser.add_argument("--verbose", action="store_true", help="Show passing assertions too.")
    parser.add_argument("--show-fixtures", action="store_true")
    args = parser.parse_args()

    if args.show_fixtures:
        print(fixture_summary())
        return 0

    cases = SUITES[args.suite]
    print(f"Suite    : {args.suite} ({len(cases)} cases)")
    print(f"Models   : {', '.join(args.models)}")

    suites = []
    for model in args.models:
        print(f"\nRunning {model} …", flush=True)
        suite = await run_suite(cases, model)
        suites.append(suite)
        if not args.compare:
            print(render(suite, verbose=args.verbose))

    if args.compare:
        print(render_comparison(suites))

    failed = [s for s in suites if not s.passed]
    if failed:
        print(f"\nFAILED: {', '.join(s.model for s in failed)}")
        return 1
    print("\nAll suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
