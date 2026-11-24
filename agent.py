"""Claude Agent SDK integration: plan generation, re-planning, and
trigger-fired decisions. Authenticates via the operator's Claude
subscription (through the locally-installed `claude` CLI) rather than a
metered API key — see SPEC.md and the M2 conversation log for context.

Design constraints (see SPEC.md):
- The LLM never originates numbers. All prices/levels must appear in the
  operator-supplied context or the existing plan; `generated_at`/`date` are
  always stamped by this module, never trusted from the model.
- Plan generation and re-planning use a prose-then-JSON prompt format (the
  operator's existing analysis style) and are validated against schema.py,
  with exactly one retry on a validation error.
- decide() never originates levels/size either — it only gates a pre-planned
  action, and fails closed (SKIP) on any timeout or SDK error.
- The model is given only the `WebSearch` tool (plan/replan) or no tools at
  all (decide) — never Bash/Read/Write/Edit — since this runs unattended.

A stray `ANTHROPIC_API_KEY` in the environment would silently shadow
subscription auth for the spawned `claude` subprocess (it wins the same way
it does for the raw SDK client), so every entry point scrubs it before
calling the SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, ResultMessage, query
from pydantic import ValidationError

import config
from schema import Decision, Plan, Tick, Trigger

ET = ZoneInfo("America/New_York")
PROMPTS_DIR = Path(__file__).parent / "prompts"

# Turns a query() call belongs to, not wall-clock — generous enough to allow
# a handful of WebSearch round-trips within one logical prompt.
PLAN_MAX_TURNS = 12

DECISION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["EXECUTE", "SKIP", "WAIT"]},
        "reasoning": {"type": "string"},
        "confidence": {"type": "integer"},
    },
    "required": ["action", "reasoning", "confidence"],
    "additionalProperties": False,
}

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)

_SHADOWING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class PlanGenerationError(Exception):
    """Raised when a plan/replan fails validation twice, or the SDK call
    itself fails. The caller must alert the operator and must not trade."""


def _scrub_api_key_env() -> None:
    """Remove credentials that would shadow subscription (OAuth) auth for
    the `claude` subprocess the SDK spawns."""
    for var in _SHADOWING_ENV_VARS:
        os.environ.pop(var, None)


def _now_et_iso() -> str:
    return datetime.now(ET).isoformat()


def _fill_template(template: str, context: dict[str, str]) -> str:
    filled = template
    for key, value in context.items():
        filled = filled.replace("{{" + key + "}}", str(value))
    return filled


def _extract_json_block(text: str) -> dict:
    matches = _JSON_BLOCK_RE.findall(text)
    if not matches:
        raise ValueError("no fenced ```json block found in model response")
    return json.loads(matches[-1])


def _validate_plan_text(text: str, context: dict[str, str]) -> tuple[Plan | None, str | None]:
    try:
        data = _extract_json_block(text)
    except (ValueError, json.JSONDecodeError) as e:
        return None, f"could not extract a valid JSON block: {e}"

    # generated_at and date are stamped here, never trusted from the model.
    data["generated_at"] = _now_et_iso()
    if "DATE" in context:
        data["date"] = context["DATE"]

    try:
        return Plan.model_validate(data), None
    except ValidationError as e:
        return None, str(e)


async def _run_query(
    prompt: str, options: ClaudeAgentOptions, timeout_s: float
) -> ResultMessage:
    """Run one query() to completion and return its ResultMessage."""
    _scrub_api_key_env()

    async def _consume() -> ResultMessage:
        result: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None:
            raise PlanGenerationError("claude-agent-sdk produced no ResultMessage")
        return result

    try:
        return await asyncio.wait_for(_consume(), timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise PlanGenerationError(f"query timed out after {timeout_s}s") from e
    except ClaudeSDKError as e:
        raise PlanGenerationError(f"claude-agent-sdk call failed: {e}") from e


def _plan_options(model: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=model,
        tools=["WebSearch"],
        allowed_tools=["WebSearch"],
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=PLAN_MAX_TURNS,
        effort=config.PLAN_EFFORT,
    )


async def _run_plan_prompt(
    prompt_path: Path,
    context: dict[str, str],
    model: str,
    timeout_s: float,
) -> Plan:
    template = prompt_path.read_text(encoding="utf-8")
    filled = _fill_template(template, context)
    options = _plan_options(model)

    result = await _run_query(filled, options, timeout_s)
    if result.is_error or not result.result:
        raise PlanGenerationError(
            f"plan generation call failed (subtype={result.subtype!r}): "
            f"{result.errors or result.result}"
        )

    plan, error = _validate_plan_text(result.result, context)
    if plan is not None:
        return plan

    # One retry with the validation error appended, per SPEC.md. Each
    # query() call is stateless, so the retry prompt is self-contained
    # rather than resuming the prior session.
    retry_prompt = (
        f"{filled}\n\n---\n\nYour first attempt produced this response:\n\n"
        f"{result.result}\n\n---\n\n"
        f"That JSON did not validate against the schema:\n{error}\n\n"
        "Re-emit your full response (prose analysis followed by the "
        "corrected JSON block) fixing only the schema issue. Do not change "
        "the trade ideas themselves unless they caused the error."
    )
    result2 = await _run_query(retry_prompt, options, timeout_s)
    if result2.is_error or not result2.result:
        raise PlanGenerationError(
            f"plan generation retry call failed (subtype={result2.subtype!r}): "
            f"{result2.errors or result2.result}"
        )

    plan2, error2 = _validate_plan_text(result2.result, context)
    if plan2 is not None:
        return plan2

    raise PlanGenerationError(f"plan failed validation twice: {error2}")


async def generate_plan(context: dict[str, str]) -> Plan:
    """Pre-market plan generation. `context` supplies DATE, SPX_SPOT,
    ECON_CALENDAR, JOURNAL_SUMMARY — the placeholders in daily_plan.md."""
    return await _run_plan_prompt(
        PROMPTS_DIR / "daily_plan.md", context, config.PLAN_MODEL, config.PLAN_TIMEOUT_S
    )


def describe_move(current_plan: Plan, spx_now: float, now_et: datetime) -> str:
    """Fallback replan reason when the operator doesn't supply one: state
    the move factually, from the plan's own reference vs. the price now.
    Computed here rather than left to the model — design principle 2."""
    ref = current_plan.spx_reference
    delta = spx_now - ref
    pct = (delta / ref * 100) if ref else 0.0
    return (
        f"Operator-initiated re-plan; no written reason was supplied, so assess "
        f"from the plan and the market data below.\n\n"
        f"SPX has moved from {ref:g} (the reference this plan was built on) to "
        f"{spx_now:g} as of {now_et.strftime('%H:%M')} ET — {delta:+.1f} points "
        f"({pct:+.2f}%). Treat the *cause* as unknown: it may be a headline, a "
        f"scheduled speaker, or purely technical. Do not invent a narrative for "
        f"it — plan for where price is now and say plainly that the driver is "
        f"unconfirmed."
    )


def format_release_reason(release_data: dict) -> str:
    """Render a data release as a replan reason (the automated path)."""
    return (
        f"A scheduled economic data release just printed.\n\n"
        f"- **Release:** {release_data.get('release', '')}\n"
        f"- **Actual:** {release_data.get('actual', '')}\n"
        f"- **Forecast:** {release_data.get('forecast', '')}\n"
        f"- **Previous:** {release_data.get('previous', '')}\n"
        f"- **Surprise:** {release_data.get('surprise', '')}"
    )


async def replan(current_plan: Plan, current_tick: Tick, reason: str) -> Plan:
    """Re-plan mid-session. `reason` is free text describing what changed —
    a data release (see format_release_reason) or an operator-described
    price move that invalidated the morning's structure."""
    context = {
        "DATE": str(current_plan.date),
        "REPLAN_REASON": reason,
        "SPX_SPOT": str(current_tick.price),
        "TICK_TIME_ET": current_tick.ts.astimezone(ET).strftime("%H:%M:%S"),
        "YIELD_10Y": str(current_tick.yield_10y) if current_tick.yield_10y is not None else "n/a",
        "CURRENT_PLAN_JSON": current_plan.model_dump_json(indent=2),
    }
    return await _run_plan_prompt(
        PROMPTS_DIR / "replan.md", context, config.PLAN_MODEL, config.REPLAN_TIMEOUT_S
    )


def build_decide_prompt(trigger: Trigger, plan: Plan, market_context: dict) -> str:
    """prompts/decide.md filled in for one fired trigger."""
    template = (PROMPTS_DIR / "decide.md").read_text(encoding="utf-8")
    context = {
        "TRIGGER_JSON": trigger.model_dump_json(indent=2),
        "BIAS": plan.bias,
        "INVALIDATION_JSON": plan.invalidation.model_dump_json(),
        "OTHER_TRIGGER_STATUSES": str(market_context.get("other_trigger_statuses", "n/a")),
        "SPX_SPOT": str(market_context.get("spx_spot", "n/a")),
        "TICK_TIME_ET": str(market_context.get("tick_time_et", "n/a")),
        "TICK_TIME_SGT": str(market_context.get("tick_time_sgt", "n/a")),
        "YIELD_10Y": str(market_context.get("yield_10y", "n/a")),
        "YIELD_DIRECTION": str(market_context.get("yield_direction", "n/a")),
        "MARKET_CONTEXT": str(market_context.get("notes", "n/a")),
        "ANALYSIS": str(market_context.get("analysis", "(not provided)")),
        "TRADING_RULES": str(market_context.get("trading_rules", "(not provided)")),
        "RECENT_CLOSES": str(market_context.get("recent_closes", "(not provided)")),
    }
    return _fill_template(template, context)


async def decide(trigger: Trigger, plan: Plan, market_context: dict) -> Decision:
    """Gate a fired trigger's pre-planned action. Fails closed to SKIP on
    any timeout or SDK error. No tools — this is a pure decision call."""
    filled = build_decide_prompt(trigger, plan, market_context)

    options = ClaudeAgentOptions(
        model=config.DECIDE_MODEL,
        tools=[],
        setting_sources=[],
        output_format={"type": "json_schema", "schema": DECISION_JSON_SCHEMA},
        effort=config.DECIDE_EFFORT,
    )

    try:
        result = await _run_query(filled, options, config.DECIDE_TIMEOUT_S)
    except PlanGenerationError as e:
        print(f"[agent.decide] ALERT: decide() call failed ({e}); defaulting to SKIP", file=sys.stderr)
        return Decision(action="SKIP", reasoning=f"agent call failed: {e}", confidence=0)

    if result.is_error:
        print(
            f"[agent.decide] ALERT: decide() returned an error (subtype={result.subtype!r}, "
            f"errors={result.errors}); defaulting to SKIP",
            file=sys.stderr,
        )
        return Decision(action="SKIP", reasoning=f"agent error: {result.errors}", confidence=0)

    try:
        if result.structured_output is not None:
            return Decision.model_validate(result.structured_output)
        return Decision.model_validate(json.loads(result.result or ""))
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        print(f"[agent.decide] ALERT: invalid decision output ({e}); defaulting to SKIP", file=sys.stderr)
        return Decision(action="SKIP", reasoning=f"invalid decision output: {e}", confidence=0)


# --------------------------------------------------------------------------
# CLI: python -m agent plan [--dry-run]
# --------------------------------------------------------------------------


def _read_text_or_literal(value: str) -> str:
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def format_released_data(released: list[list[str]] | None) -> str:
    """Render already-printed releases for the daily_plan.md prompt.

    Surprise is computed here (reusing releases.compute_surprise, the same
    path the automated replan uses) rather than left to the model — per
    SPEC.md design principle 2, the LLM never originates numbers."""
    if not released:
        return "None — nothing has printed yet this morning."

    import releases

    lines = []
    for name, actual, forecast, previous in released:
        surprise = releases.compute_surprise(actual, forecast)
        surprise_text = "" if surprise is None else f", surprise {surprise:+g} vs forecast"
        lines.append(
            f"- {name}: actual {actual}, forecast {forecast}, previous {previous}{surprise_text}"
        )
    return "\n" + "\n".join(lines)


def _add_reference_price_args(sub_parser: argparse.ArgumentParser) -> None:
    """Shared by `plan` and `replan`: how to supply the current SPX price,
    either directly or derived from MES (SPX has no live pre-market print,
    and MES stays quotable when the operator is away from the feed)."""
    sub_parser.add_argument(
        "--spx-reference",
        type=float,
        default=None,
        help="Current SPX spot price, from the data feed — not guessed. "
        "Required unless --mes-current/--mes-prev-close/--spx-prev-close are all given instead "
        "(pre-market SPX has no live print of its own — see SPEC.md's Feed section).",
    )
    sub_parser.add_argument(
        "--mes-current", type=float, default=None, help="MESmain current live price (SPX proxy)"
    )
    sub_parser.add_argument("--mes-prev-close", type=float, default=None, help="MESmain previous session close")
    sub_parser.add_argument("--spx-prev-close", type=float, default=None, help=".SPX previous session close")


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="Generate the pre-market daily plan")
    plan_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the validated Plan (or validation errors) without writing state/plan_<date>.json",
    )
    plan_p.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today in America/New_York)")
    _add_reference_price_args(plan_p)
    plan_p.add_argument(
        "--released",
        nargs=4,
        action="append",
        metavar=("NAME", "ACTUAL", "FORECAST", "PREVIOUS"),
        default=None,
        help="A release that has ALREADY printed this morning (e.g. an 08:30 ET CPI, "
        'before the 09:30 open): --released "CPI YoY" 3.1 3.0 2.9. Repeatable for '
        "clustered prints. Surprise is computed here, not by the model.",
    )
    plan_p.add_argument(
        "--econ-calendar",
        default="No major releases scheduled.",
        help="Today's econ calendar text, or a path to a file containing it",
    )
    plan_p.add_argument(
        "--journal-summary",
        default="No prior session data available.",
        help="Prior-day journal summary text, or a path to a file containing it",
    )

    replan_p = sub.add_parser(
        "replan",
        help="Re-plan mid-session against the plan currently in state/ "
        "(e.g. price dumped/spiked through the morning's structure)",
    )
    replan_p.add_argument(
        "--reason",
        default=None,
        help='Optional: why you are re-planning, in your own words — e.g. "dumped '
        'through the 7450 must-hold on no headline". Omit it and the move is '
        "described automatically from the price delta; supply it when you know "
        "something the numbers do not show (a headline, a Fed speaker, order flow).",
    )
    replan_p.add_argument(
        "--released",
        nargs=4,
        action="append",
        metavar=("NAME", "ACTUAL", "FORECAST", "PREVIOUS"),
        default=None,
        help="Optional: an intraday release that just printed, if that's the trigger. "
        'Repeatable. e.g. --released "ISM Manufacturing PMI" 49.5 49.2 48.7',
    )
    replan_p.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today in America/New_York)")
    _add_reference_price_args(replan_p)
    replan_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the re-planned Plan without overwriting state/plan_<date>.json "
        "(so the running main.py will NOT pick it up)",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    if args.command == "plan":
        date_str = args.date or datetime.now(ET).strftime("%Y-%m-%d")
        context = {
            "DATE": date_str,
            "SPX_SPOT": str(args.spx_reference),
            "ECON_CALENDAR": _read_text_or_literal(args.econ_calendar),
            "JOURNAL_SUMMARY": _read_text_or_literal(args.journal_summary),
            "RELEASED_DATA": format_released_data(args.released),
        }
        try:
            plan = await generate_plan(context)
        except PlanGenerationError as e:
            print(
                f"PLAN GENERATION FAILED — alert the operator; do not trade today.\n{e}",
                file=sys.stderr,
            )
            return 1

        _write_plan_outputs(plan, dry_run=args.dry_run)
        return 0

    if args.command == "replan":
        date_str = args.date or datetime.now(ET).strftime("%Y-%m-%d")
        current_path = Path("state") / f"plan_{date_str}.json"
        if not current_path.exists():
            print(
                f"No plan in force at {current_path} — nothing to re-plan against. "
                "Run `python -m agent plan` first.",
                file=sys.stderr,
            )
            return 1
        current_plan = Plan.model_validate_json(current_path.read_text(encoding="utf-8"))

        now_et = datetime.now(ET)
        reason = args.reason or describe_move(current_plan, args.spx_reference, now_et)
        if args.released:
            reason = f"{reason}\n\nReleases that just printed:{format_released_data(args.released)}"

        current_tick = Tick(ts=now_et, price=args.spx_reference, yield_10y=None)
        try:
            new_plan = await replan(current_plan, current_tick, reason)
        except PlanGenerationError as e:
            print(
                f"RE-PLAN FAILED — the existing plan remains in force, unchanged.\n{e}",
                file=sys.stderr,
            )
            return 1

        open_ids = {t.id for t in current_plan.triggers} - {t.id for t in new_plan.triggers}
        if open_ids:
            print(
                f"NOTE: triggers dropped by this re-plan: {sorted(open_ids)}. If any of "
                "these is currently OPEN, the engine will emit ReviewRequired rather "
                "than silently drop it — check Discord.",
                file=sys.stderr,
            )

        _write_plan_outputs(new_plan, dry_run=args.dry_run)
        if not args.dry_run:
            print(
                "The running main.py will hot-swap to this plan within "
                f"{config.INPUT_POLL_S:g}s, preserving FIRED/OPEN trigger state.",
                file=sys.stderr,
            )
        return 0

    return 1


def _write_plan_outputs(plan: Plan, *, dry_run: bool) -> None:
    plan_json = plan.model_dump_json(indent=2)
    print(plan_json)

    # daily_plans/ is a human-readable journal archive — every generated
    # plan lands here, dry-run or not, purely for later review. It is
    # never read by the engine; that's state/'s job (see below).
    journal_path = Path("daily_plans") / f"plan_{plan.date}.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(plan_json, encoding="utf-8")
    print(f"\nWrote {journal_path} (journal copy)", file=sys.stderr)

    if not dry_run:
        out_path = Path("state") / f"plan_{plan.date}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(plan_json, encoding="utf-8")
        print(f"Wrote {out_path} (engine-canonical)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)

    if args.command in ("plan", "replan") and args.spx_reference is None:
        # SPX has no live pre-market print of its own — MESmain does, so
        # this derives an estimate the same way an operator would by hand:
        # basis = yesterday's SPX close vs. MES close at that same moment,
        # applied to today's live MES price. Pure deterministic arithmetic,
        # not something the LLM originates — see SPEC.md design principle 2.
        mes_args = (args.mes_current, args.mes_prev_close, args.spx_prev_close)
        if any(v is None for v in mes_args):
            parser.error(
                "--spx-reference is required, unless --mes-current, --mes-prev-close, "
                "and --spx-prev-close are all given instead"
            )
        basis = args.spx_prev_close - args.mes_prev_close
        args.spx_reference = args.mes_current + basis
        print(
            f"[agent] derived --spx-reference={args.spx_reference:.2f} from MES "
            f"(basis={basis:+.2f} = spx_prev_close {args.spx_prev_close} - mes_prev_close {args.mes_prev_close})",
            file=sys.stderr,
        )

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
