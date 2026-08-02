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
    python scripts/run_evals.py --repeat 3            # pass rates, before a model decision
    python scripts/run_evals.py --models ministral-3:8b gemma4:26b --compare
    python scripts/run_evals.py --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uione.evals import (  # noqa: E402
    render,
    render_comparison,
    render_rates,
    run_repeated,
    run_suite,
)
from uione.evals.suites import SUITES, fixture_summary  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", nargs="+", default=["ministral-3:8b"])
    parser.add_argument("--suite", choices=sorted(SUITES), default="all")
    parser.add_argument("--compare", action="store_true", help="Side-by-side table.")
    parser.add_argument("--verbose", action="store_true", help="Show passing assertions too.")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Run the suite N times and report a pass rate per case. "
            "Use before any model decision: one run is a smoke test."
        ),
    )
    parser.add_argument("--show-fixtures", action="store_true")
    args = parser.parse_args()

    if args.show_fixtures:
        print(fixture_summary())
        return 0

    cases = SUITES[args.suite]
    print(f"Suite    : {args.suite} ({len(cases)} cases)")
    print(f"Models   : {', '.join(args.models)}")

    if args.repeat > 1:
        # A pass *rate*, because the interesting case is the one that does both.
        for model in args.models:
            print(f"\nRunning {model} × {args.repeat} …", flush=True)
            runs = await run_repeated(cases, model, times=args.repeat)
            print(render_rates(runs))
        # Deliberately no exit code from a repeated run. "Did it pass" is the
        # wrong question to ask of a rate, and answering it would invite the
        # habit of re-running until green.
        return 0

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
