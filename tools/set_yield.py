"""Record a 10-year Treasury yield reading for the running main.py.

    python tools/set_yield.py 4.93
    python tools/set_yield.py 49.3        # TNX-style x10 quote, stored as 4.93

Writes state/yield.json with the current time. main.py picks it up within
INPUT_POLL_S seconds; it counts as missing after YIELD_MAX_AGE_MIN minutes.
"""

from __future__ import annotations

import _common  # noqa: F401  (sets up imports / cwd)

import argparse
import sys
from datetime import datetime

import config
import inputs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/set_yield.py")
    p.add_argument("value", type=float, help="10Y yield in percent, e.g. 4.93")
    p.add_argument("--source", default="manual", choices=["manual", "ocr", "feed"])
    args = p.parse_args(argv)

    previous = inputs.read_yield()
    try:
        y = inputs.write_yield(args.value, source=args.source)
    except ValueError as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1

    now = datetime.now(inputs.ET)
    change = ""
    if previous is not None:
        mins = int(previous.age(now).total_seconds() // 60)
        change = f" (was {previous.value:g}% {mins} min ago, {y.value - previous.value:+.3f})"
    pivot = "below" if y.value < 5.0 else "at/above"
    print(
        f"10Y = {y.value:g}%{change} at {y.ts:%H:%M} ET, {pivot} the 5.00% pivot. "
        f"Valid for {config.YIELD_MAX_AGE_MIN:g} min."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
