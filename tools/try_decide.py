"""Run one decide() by hand, with the same context main.py builds, to see
how the model uses the write-up, the rules and the recent closes.

    python tools/try_decide.py                     # the Nov 23 example, buy_dip at 7205
    python tools/try_decide.py --print-prompt      # show the prompt, don't call the model
    python tools/try_decide.py --trigger breakout_long --price 7262 --closes 7240,7248,7255,7258,7261,7263
    python tools/try_decide.py --plan state/plan_2026-11-25.json --analysis analyses/2026-11-25.md --trigger ...

Calls the real model on your Claude subscription (claude login), up to
DECIDE_TIMEOUT_S. Writes nothing. Prints the decision in ASCII.
"""

from __future__ import annotations

import _common  # noqa: F401  (sets up imports / cwd)

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import agent
import config
import decide_context
from schema import Plan

ET = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")
EXAMPLE_PLAN = Path("tests/fixtures/plan_v2_example.json")
EXAMPLE_ANALYSIS = Path("tests/fixtures/analysis_2026-11-23.md")
# a pullback into the 7,195-7,215 dip zone that is slowing down
EXAMPLE_CLOSES = "7236,7228,7221,7214,7209,7207"


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def build_context(plan: Plan, analysis_text: str, price: float, closes: list[float], when: datetime) -> dict:
    start_min = when.hour * 60 + when.minute - when.minute % 5 - 5 * len(closes)
    labelled = [(f"{(start_min + 5 * i) // 60:02d}:{(start_min + 5 * i) % 60:02d}", c) for i, c in enumerate(closes)]
    return {
        "spx_spot": price,
        "tick_time_et": when.strftime("%H:%M:%S"),
        "tick_time_sgt": when.astimezone(SGT).strftime("%H:%M:%S"),
        "yield_10y": "not entered",
        "yield_direction": "n/a",
        "other_trigger_statuses": {t.id: "ARMED" for t in plan.triggers},
        "notes": "Hand test via tools/try_decide.py (synthetic closes)."
        + (f" | Plan notes: {plan.notes}" if plan.notes else ""),
        "analysis": decide_context.analysis_excerpt(analysis_text),
        "trading_rules": decide_context.load_trading_rules(config.TRADING_RULES_PATH),
        "recent_closes": decide_context.format_closes(labelled),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/try_decide.py")
    p.add_argument("--plan", type=Path, default=EXAMPLE_PLAN)
    p.add_argument("--analysis", type=Path, default=EXAMPLE_ANALYSIS)
    p.add_argument("--trigger", default="buy_dip", help="trigger id in the plan")
    p.add_argument("--price", type=float, default=7205.0, help="SPX at the fire")
    p.add_argument("--time", default="10:12", help="ET time of the fire, HH:MM")
    p.add_argument("--closes", default=EXAMPLE_CLOSES, help="last 5-minute closes, oldest first, comma-separated")
    p.add_argument("--print-prompt", action="store_true", help="print the prompt and exit without calling the model")
    args = p.parse_args(argv)

    plan = Plan.model_validate_json(args.plan.read_text(encoding="utf-8"))
    trigger = next((t for t in plan.triggers if t.id == args.trigger), None)
    if trigger is None:
        print(f"REJECTED: no trigger {args.trigger!r} in {args.plan} (have: "
              f"{', '.join(t.id for t in plan.triggers)})", file=sys.stderr)
        return 1
    hh, mm = (int(x) for x in args.time.split(":"))
    when = datetime(plan.date.year, plan.date.month, plan.date.day, hh, mm, tzinfo=ET)
    closes = [float(x) for x in args.closes.split(",") if x.strip()]
    ctx = build_context(plan, args.analysis.read_text(encoding="utf-8"), args.price, closes, when)

    if args.print_prompt:
        print(_ascii(agent.build_decide_prompt(trigger, plan, ctx)))
        return 0

    print(f"Calling decide() ({config.DECIDE_MODEL}, up to {config.DECIDE_TIMEOUT_S}s) for "
          f"{trigger.id} at {args.price:g}, {args.time} ET...")
    decision = asyncio.run(agent.decide(trigger, plan, ctx))
    print(_ascii(f"{decision.action} (confidence {decision.confidence}): {decision.reasoning}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
