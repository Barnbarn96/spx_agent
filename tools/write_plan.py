"""Validate a plan JSON written by Claude Code's /premarket (or /intraday
replan) and install it as today's plan for the running main.py.

    python tools/write_plan.py draft.json --analysis analyses/2026-11-24.md
    python tools/write_plan.py draft.json --analysis analyses/2026-11-24.md --replan
    python tools/write_plan.py --schema          # print the Plan JSON schema
    python tools/write_plan.py --example         # print an example plan

Checks, in order:
1. The JSON validates against schema.Plan (date and generated_at are stamped
   here, never trusted from the draft).
2. Grounding: every price/yield level in the plan must appear as a number in
   the analysis write-up. The model turns prose into JSON; it must not invent
   levels while doing so.
3. Hard rules: flat_by no later than 15:30 ET.
4. Overwriting today's plan requires --replan (main.py hot-swaps onto it).

Writes state/plan_<date>.json (what main.py loads) and daily_plans/ (archive).
"""

from __future__ import annotations

import _common  # noqa: F401  (sets up imports / cwd)

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

import config
import inputs
from schema import Plan

LATEST_FLAT_BY = "15:30"
EXAMPLE_PATH = Path("tests/fixtures/plan_v2_example.json")

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in_text(text: str) -> set[float]:
    """All numbers in the write-up, with thousands separators removed
    (7,195 -> 7195)."""
    flat = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    return {float(m) for m in _NUM_RE.findall(flat)}


def plan_levels(plan: Plan) -> list[tuple[str, float]]:
    """Every price/yield level the engine will act on. Sizes and scale-out
    percentages are excluded: "take half" legitimately becomes 50."""
    out: list[tuple[str, float]] = [("spx_reference", plan.spx_reference)]
    if plan.spx_prev_close is not None:
        out.append(("spx_prev_close", plan.spx_prev_close))
    if plan.invalidation.below is not None:
        out.append(("invalidation.below", plan.invalidation.below))
    if plan.invalidation.above is not None:
        out.append(("invalidation.above", plan.invalidation.above))
    for t in plan.triggers:
        c = t.condition
        if c.price_between is not None:
            out += [(f"{t.id}.price_between", c.price_between[0]), (f"{t.id}.price_between", c.price_between[1])]
        for name in ("price_above", "price_below", "yield_below", "yield_above"):
            v = getattr(c, name)
            if v is not None:
                out.append((f"{t.id}.{name}", v))
        out.append((f"{t.id}.stop", t.stop))
        out += [(f"{t.id}.target", tg.level) for tg in t.targets]
    return out


def ungrounded_levels(plan: Plan, analysis: str) -> list[str]:
    found = numbers_in_text(analysis)
    return [f"{where}={value:g}" for where, value in plan_levels(plan) if float(value) not in found]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/write_plan.py")
    p.add_argument("plan_json", nargs="?", help="Draft plan JSON file ('-' for stdin)")
    p.add_argument("--analysis", help="The analysis markdown the plan was drawn from")
    p.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today ET)")
    p.add_argument("--replan", action="store_true", help="Allow replacing today's plan")
    p.add_argument("--dry-run", action="store_true", help="Validate only, write nothing")
    p.add_argument("--schema", action="store_true", help="Print the Plan JSON schema and exit")
    p.add_argument("--example", action="store_true", help="Print an example plan and exit")
    args = p.parse_args(argv)

    if args.schema:
        print(json.dumps(Plan.model_json_schema(), indent=2))
        return 0
    if args.example:
        print(EXAMPLE_PATH.read_text(encoding="utf-8"))
        return 0
    if not args.plan_json or not args.analysis:
        p.error("plan_json and --analysis are required")

    raw = sys.stdin.read() if args.plan_json == "-" else Path(args.plan_json).read_text(encoding="utf-8")
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"REJECTED: not valid JSON ({e})", file=sys.stderr)
        return 1

    now = datetime.now(inputs.ET)
    draft["date"] = args.date or now.date().isoformat()
    draft["generated_at"] = now.isoformat()
    try:
        plan = Plan.model_validate(draft)
    except ValidationError as e:
        print(f"REJECTED: plan does not match the schema:\n{e}", file=sys.stderr)
        return 1

    problems: list[str] = []
    analysis_text = Path(args.analysis).read_text(encoding="utf-8")
    missing = ungrounded_levels(plan, analysis_text)
    if missing:
        problems.append(
            "levels not found anywhere in the analysis (fix the JSON or the write-up): " + ", ".join(missing)
        )
    if plan.flat_by > LATEST_FLAT_BY:
        problems.append(f"flat_by {plan.flat_by} is after the {LATEST_FLAT_BY} ET exit rule")
    if plan.no_entries_before is not None and not ("09:30" <= plan.no_entries_before <= config.ENTRY_CUTOFF_ET):
        problems.append(
            f"no_entries_before {plan.no_entries_before} must be between 09:30 and the "
            f"{config.ENTRY_CUTOFF_ET} ET entry cutoff"
        )

    state_path = Path(config.STATE_DIR) / f"plan_{plan.date}.json"
    old_plan = None
    if state_path.exists():
        old_plan = Plan.model_validate_json(state_path.read_text(encoding="utf-8"))
        if not args.replan:
            problems.append(f"{state_path} already exists - pass --replan to replace today's plan")

    if problems:
        print("REJECTED:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1

    print(f"Plan OK: {plan.date}, bias {plan.bias}, {len(plan.triggers)} triggers, flat by {plan.flat_by} ET")
    if plan.no_entries_before is not None:
        print(f"  Opening wait: alerts before {plan.no_entries_before} ET need an early-entry override")
    for t in plan.triggers:
        c = t.condition
        zone = (
            f"{c.price_between[0]:g}-{c.price_between[1]:g}" if c.price_between
            else f">{c.price_above:g}" if c.price_above is not None
            else f"<{c.price_below:g}" if c.price_below is not None
            else "(no price condition)"
        )
        extra = []
        if c.confirm is not None and c.confirm.type == "close":
            extra.append(f"on {c.confirm.timeframe} close")
        if c.confirm is not None and c.confirm.type == "dwell":
            extra.append(f"after holding {c.confirm.seconds}s")
        if c.approach:
            extra.append(c.approach.replace("_", " "))
        if c.first_touch:
            extra.append("first touch only")
        if c.not_before:
            extra.append(f"not before {c.not_before}")
        if c.yield_below is not None:
            extra.append(f"10Y <= {c.yield_below:g}")
        if c.yield_above is not None:
            extra.append(f"10Y >= {c.yield_above:g}")
        targets = "/".join(
            f"{tg.level:g}" + ("(then stop to BE)" if tg.then_stop == "breakeven" else "") for tg in t.targets
        )
        stop = f"{t.stop:g}" + (f" on {t.stop_confirm.timeframe} close" if t.stop_confirm and t.stop_confirm.type == "close" else "")
        print(f"  {t.id}: {t.side} {zone} stop {stop} targets {targets} size {t.action.size_pct:g}%"
              + (f" ({', '.join(extra)})" if extra else ""))
    if old_plan is not None:
        dropped = sorted({t.id for t in old_plan.triggers} - {t.id for t in plan.triggers})
        if dropped:
            print(f"NOTE: replan drops triggers {dropped}; an OPEN one raises a review alert in Discord.")

    if args.dry_run:
        print("(dry run - nothing written)")
        return 0

    plan_json = plan.model_dump_json(indent=2)
    inputs.atomic_write(state_path, plan_json)
    archive = Path(config.DAILY_PLANS_DIR) / f"plan_{plan.date}.json"
    inputs.atomic_write(archive, plan_json)
    print(f"Wrote {state_path} - main.py picks it up within {config.INPUT_POLL_S:g}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
