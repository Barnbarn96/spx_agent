"""asyncio entrypoint: supervises the live loop, wires feed -> engine ->
agent -> notify -> journal together. Phase 1 is alerts-only — no order
routing. See SPEC.md.

--sim replays ticks from a CSV instead of the live Polygon websocket, so the
full loop (including real Discord confirm/skip/mark-filled buttons) can be
exercised end-to-end without market hours. Live mode (feed.py's
PolygonFeed, scheduler.py's cron jobs) has not been exercised against real
market data in this environment — see ROLLOUT_PLAN.md.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

import agent
import config
import releases
from feed import FeedManager, FeedStale, MoomooFeed, PolygonFeed, ReplayTickSource, TickSource
from journal import JournalDB
from replay import load_ticks
from risk import RiskManager
from schema import (
    Decision,
    EngineWarning,
    FlatByReached,
    Invalidated,
    Plan,
    ReviewRequired,
    ScheduledEvent,
    StopHit,
    TargetHit,
    Tick,
    TriggerFired,
)
from scheduler import AgentScheduler, SchedulerCallbacks
from triggers import TriggerEngine

ET = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")


class Orchestrator:
    def __init__(self, sim: bool) -> None:
        self.sim = sim
        self.engine: TriggerEngine | None = None
        self.plan: Plan | None = None
        self.last_price: float | None = None
        self.last_yield: float | None = None

        self.journal = JournalDB(config.JOURNAL_DB_PATH)
        self.risk = RiskManager(
            halt_path=config.HALT_FILE_PATH,
            daily_loss_limit_usd=config.DAILY_LOSS_LIMIT_USD,
            max_open_triggers=config.MAX_OPEN_TRIGGERS,
            entry_cutoff_et=config.ENTRY_CUTOFF_ET,
        )
        self.notifier = None  # built in run(), only if Discord is configured
        self.scheduler: AgentScheduler | None = None
        self._feed_source: TickSource | None = None  # set in run_live(); moomoo calibrates from this
        self._plan_file_mtime: float | None = None  # for detecting a manual replan mid-session

    # -- plan lifecycle ---------------------------------------------------

    def _load_plan(self, plan: Plan, source: str) -> bool:
        """Returns True if this load cleared an active invalidation (see
        TriggerEngine.load_plan) — callers should alert on that."""
        self.plan = plan
        cleared_invalidation = False
        if self.engine is None:
            self.engine = TriggerEngine(plan)
        else:
            cleared_invalidation = self.engine.load_plan(plan)
        if self.scheduler is not None:
            self.scheduler.schedule_release_jobs(plan)
        # The notifier's callback handler always needs the *current* engine
        # — it's replaced wholesale on every load_plan(), and in live mode
        # the notifier starts before any plan exists. Keep it synced here
        # rather than snapshotting it once at Notifier construction.
        if self.notifier is not None:
            self.notifier.engine = self.engine
        # MoomooFeed estimates SPX from SPY via a calibrated ratio — refresh
        # it from the operator-verified spx_reference on every plan load
        # (daily generation and any replan) so it doesn't drift for the life
        # of a long-running process.
        if isinstance(self._feed_source, MoomooFeed):
            self._feed_source.calibrate(plan.spx_reference)
        return cleared_invalidation

    async def on_generate_plan(self) -> None:
        """Scheduler callback: 08:00 ET pre-market plan generation. Live
        mode only — best-effort context gathering, unverified against a
        real pre-market session (see ROLLOUT_PLAN.md)."""
        today = datetime.now(ET).date()
        econ_calendar_text = await self._fetch_econ_calendar_text(today)
        context = {
            "DATE": today.isoformat(),
            "SPX_SPOT": str(self.last_price) if self.last_price is not None else "n/a",
            "ECON_CALENDAR": econ_calendar_text,
            "JOURNAL_SUMMARY": "See journal.db for prior sessions.",
            # The scheduled 08:00 job runs before the classic 08:30 ET prints,
            # so by definition nothing has printed yet at this point. To fold
            # an 08:30 release into the plan, re-run `python -m agent plan`
            # manually with --released after it lands.
            "RELEASED_DATA": "None — nothing has printed yet this morning.",
        }
        try:
            plan = await agent.generate_plan(context)
        except agent.PlanGenerationError as e:
            if self.notifier:
                await self.notifier.send_text(f"PLAN GENERATION FAILED — no trading today.\n{e}")
            return

        await self.journal.log_plan(plan, "daily")
        self._write_plan_files(plan)
        self._load_plan(plan, "daily")
        if self.notifier:
            await self.notifier.send_plan_summary(plan)

    async def _fetch_econ_calendar_text(self, today: date_type) -> str:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                entries = await releases.fetch_calendar(client, today, today)
            if not entries:
                return "No major releases scheduled."
            return "; ".join(
                f"{e.get('time', '')} ET: {e.get('event', '')} "
                f"(forecast {e.get('estimate', 'n/a')}, previous {e.get('prev', 'n/a')})"
                for e in entries
            )
        except Exception:
            return "Econ calendar unavailable — proceed on known catalysts only."

    def _write_plan_files(self, plan: Plan) -> None:
        plan_json = plan.model_dump_json(indent=2)
        for directory in (config.STATE_DIR, config.DAILY_PLANS_DIR):
            path = Path(directory) / f"plan_{plan.date}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plan_json, encoding="utf-8")

    async def on_release_poll(self, scheduled_event: ScheduledEvent) -> None:
        assert self.plan is not None
        result = await releases.poll(scheduled_event, self.plan.date)
        await self.journal.log_release(scheduled_event, result)

        if result is None:
            if self.notifier:
                await self.notifier.send_release_unavailable(scheduled_event.release)
            return
        if not scheduled_event.replan:
            return

        now_tick = Tick(ts=datetime.now(ET), price=self.last_price or self.plan.spx_reference, yield_10y=self.last_yield)
        try:
            new_plan = await agent.replan(self.plan, now_tick, agent.format_release_reason(result))
        except agent.PlanGenerationError as e:
            if self.notifier:
                await self.notifier.send_text(f"RE-PLAN FAILED — plan unchanged.\n{e}")
            return

        old_plan = self.plan
        await self.journal.log_plan(new_plan, "replan")
        self._write_plan_files(new_plan)
        cleared_invalidation = self._load_plan(new_plan, "replan")
        if self.notifier:
            await self.notifier.send_replan_diff(
                old_plan,
                new_plan,
                f"{result.get('release')} — actual={result.get('actual')} "
                f"forecast={result.get('forecast')} surprise={result.get('surprise')}",
            )
            if cleared_invalidation:
                await self.notifier.send_text(
                    "**Invalidation cleared by re-plan** — the prior flatten-and-halt no "
                    "longer applies; this plan's triggers can fire again."
                )

    async def on_entry_cutoff(self) -> None:
        if self.notifier:
            await self.notifier.send_text(
                f"Entry cutoff {config.ENTRY_CUTOFF_ET} ET reached — no new entries permitted."
            )

    async def on_eod_summary(self) -> None:
        since = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        events = await self.journal.events_since(since)
        decisions = await self.journal.decisions_since(since)
        lines = [f"{len(events)} events, {len(decisions)} decisions today."]
        for d in decisions:
            lines.append(f"- {d['trigger_id']}: agent={d['agent_action']} operator={d['operator_response']}")
        if self.notifier:
            await self.notifier.send_eod_summary("\n".join(lines))

    # -- tick processing (shared by live and --sim) --------------------------

    async def process_tick(self, tick: Tick) -> None:
        self.last_price = tick.price
        if tick.yield_10y is not None:
            self.last_yield = tick.yield_10y
        if self.engine is None:
            return

        events = self.engine.evaluate(tick)
        for event in events:
            await self.journal.log_event(event)
            await self._handle_event(event)

    async def _handle_event(self, event) -> None:
        if isinstance(event, TriggerFired):
            await self._handle_trigger_fired(event)
        elif isinstance(event, TargetHit) and self.notifier:
            await self.notifier.send_target_hit(event)
        elif isinstance(event, StopHit) and self.notifier:
            await self.notifier.send_stop_hit(event)
        elif isinstance(event, Invalidated) and self.notifier:
            await self.notifier.send_invalidated(event)
        elif isinstance(event, FlatByReached) and self.notifier:
            await self.notifier.send_flat_by(event)
        elif isinstance(event, ReviewRequired) and self.notifier:
            await self.notifier.send_review_required(event)
        elif isinstance(event, EngineWarning):
            pass  # journaled above; not Discord-worthy per SPEC.md's alert list

    async def _handle_trigger_fired(self, event: TriggerFired) -> None:
        """Always decide() and alert, even past the entry cutoff or other
        risk blocks — the operator should never go dark. The hard block
        moves to the EXECUTE tap itself: notify.py re-checks
        risk.gate_execute() in real time before calling engine.confirm(),
        so a stale fire-time gate can never let an entry through."""
        assert self.engine is not None and self.plan is not None
        trigger = self.engine.states[event.trigger_id].trigger

        market_context = {
            "spx_spot": event.tick.price,
            "tick_time_et": event.tick.ts.astimezone(ET).strftime("%H:%M:%S"),
            "tick_time_sgt": event.tick.ts.astimezone(SGT).strftime("%H:%M:%S"),
            "yield_10y": event.tick.yield_10y,
            "yield_direction": self.engine.yield_direction(),
            "other_trigger_statuses": {
                tid: s.status.value for tid, s in self.engine.states.items() if tid != event.trigger_id
            },
        }

        start = time.monotonic()
        decision: Decision = await agent.decide(trigger, self.plan, market_context)
        latency_ms = (time.monotonic() - start) * 1000
        decision_id = await self.journal.log_decision(event.trigger_id, decision, latency_ms)

        if decision.action == "WAIT":
            self.engine.wait(event.trigger_id, event.tick)
            if self.notifier:
                await self.notifier.send_wait_rearmed(trigger, event.tick, decision)
        elif self.notifier:
            await self.notifier.send_trigger_fired(trigger, event.tick, decision, decision_id)
        # else: no Discord configured — decision is journaled; operator
        # must review journal.db manually to confirm/skip.

    # -- live vs --sim drivers ------------------------------------------------

    async def _reload_plan_if_file_changed(self) -> None:
        """Pick up a manual `python -m agent replan` that rewrote today's
        plan file while this process is running. Goes through the normal
        _load_plan() path, so engine.load_plan()'s hot-swap rules apply:
        FIRED/OPEN state is preserved for triggers present in both plans,
        and an OPEN trigger dropped by the new plan raises ReviewRequired
        rather than being silently discarded."""
        today = datetime.now(ET).strftime("%Y-%m-%d")
        path = Path(config.STATE_DIR) / f"plan_{today}.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._plan_file_mtime is not None and mtime <= self._plan_file_mtime:
            return

        try:
            new_plan = Plan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as e:
            # A partially-written file is expected if we stat mid-write;
            # leave mtime unrecorded so the next poll retries.
            print(f"[main] plan file changed but did not parse ({e}); keeping current plan", file=sys.stderr)
            return

        self._plan_file_mtime = mtime
        if self._plan_file_mtime is not None and self.plan is not None and new_plan == self.plan:
            return  # touched but materially identical

        old_plan = self.plan
        await self.journal.log_plan(new_plan, "manual_replan")
        cleared_invalidation = self._load_plan(new_plan, "manual_replan")
        print(f"[main] hot-swapped to re-planned {path}", file=sys.stderr)
        if self.notifier and old_plan is not None:
            await self.notifier.send_replan_diff(old_plan, new_plan, "manual re-plan (operator-initiated)")
            if cleared_invalidation:
                await self.notifier.send_text(
                    "**Invalidation cleared by re-plan** — the prior flatten-and-halt no "
                    "longer applies; this plan's triggers can fire again."
                )

    def _load_todays_plan_if_present(self) -> None:
        """Live mode otherwise only ever gets a plan from the scheduled
        08:00 job — so starting (or restarting) the process after that
        time, with a plan already generated for today, would silently sit
        plan-less until tomorrow's job. Pick up state/plan_<today>.json if
        it's already there."""
        today = datetime.now(ET).strftime("%Y-%m-%d")
        path = Path(config.STATE_DIR) / f"plan_{today}.json"
        if path.exists():
            plan = Plan.model_validate_json(path.read_text(encoding="utf-8"))
            self._plan_file_mtime = path.stat().st_mtime
            self._load_plan(plan, "daily")
            print(f"[main] loaded existing plan for {today} from {path}", file=sys.stderr)
        else:
            print(
                f"[main] no plan for {today} on disk yet — waiting for the "
                f"{config.PLAN_GENERATION_TIME_ET} ET scheduled job",
                file=sys.stderr,
            )

    async def run_live(self) -> None:
        self._load_todays_plan_if_present()

        callbacks = SchedulerCallbacks(
            on_generate_plan=self.on_generate_plan,
            on_release_poll=self.on_release_poll,
            on_entry_cutoff=self.on_entry_cutoff,
            on_eod_summary=self.on_eod_summary,
        )
        self.scheduler = AgentScheduler(callbacks)
        self.scheduler.start()

        if config.FEED_SOURCE == "moomoo":
            source = MoomooFeed()
        elif config.FEED_SOURCE == "polygon":
            source = PolygonFeed()
        else:
            raise ValueError(f"unknown FEED_SOURCE: {config.FEED_SOURCE!r} (expected polygon|moomoo)")
        self._feed_source = source
        if self.plan is not None and isinstance(source, MoomooFeed):
            source.calibrate(self.plan.spx_reference)  # in case a plan already loaded above

        print(
            f"[main] feed source: {config.FEED_SOURCE}. Streaming ticks — "
            f"heartbeat every {config.HEARTBEAT_INTERVAL_S:g}s.",
            file=sys.stderr,
        )
        tick_count = 0
        last_heartbeat = time.monotonic()
        last_plan_check = time.monotonic()

        feed_manager = FeedManager(source)
        async for item in feed_manager.watch():
            if isinstance(item, FeedStale):
                if self.notifier:
                    await self.notifier.send_feed_stale(item.seconds_since_last_tick)
                continue

            tick_count += 1
            await self.process_tick(item)

            now = time.monotonic()
            if now - last_plan_check >= config.PLAN_RELOAD_POLL_S:
                last_plan_check = now
                await self._reload_plan_if_file_changed()

            if now - last_heartbeat >= config.HEARTBEAT_INTERVAL_S:
                last_heartbeat = now
                plan_state = "no plan loaded" if self.engine is None else f"plan {self.plan.date} loaded"
                print(
                    f"[main] heartbeat: {tick_count} ticks received, last_price={self.last_price}, "
                    f"{plan_state}",
                    file=sys.stderr,
                )

    async def run_sim(self, plan_path: Path, ticks_path: Path, speed: str) -> None:
        plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        await self.journal.log_plan(plan, "daily")
        self._load_plan(plan, "daily")
        if self.notifier:
            await self.notifier.send_plan_summary(plan)

        ticks = load_ticks(ticks_path)
        source = ReplayTickSource(ticks, speed=speed)
        async for tick in source.stream():
            await self.process_tick(tick)

        print(f"[--sim] replay complete ({len(ticks)} ticks).", file=sys.stderr)
        if self.notifier:
            await self.notifier.send_text("[--sim] Replay complete. Waiting 30s for pending taps, then shutting down.")
            await asyncio.sleep(30)

    # -- lifecycle -----------------------------------------------------------

    async def start_notifier(self) -> None:
        if config.DISCORD_BOT_TOKEN and config.DISCORD_CHANNEL_ID:
            from notify import Notifier

            self.notifier = Notifier(
                config.DISCORD_BOT_TOKEN,
                config.DISCORD_CHANNEL_ID,
                engine=self.engine,
                journal=self.journal,
                risk=self.risk,
            )
            await self.notifier.start()
        else:
            print("[main] DISCORD_BOT_TOKEN/CHANNEL_ID not set — alerts will only be journaled.", file=sys.stderr)

    async def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown()
        if self.notifier is not None:
            await self.notifier.stop()
        await self.journal.close()


async def _amain(args: argparse.Namespace) -> int:
    orchestrator = Orchestrator(sim=args.sim)
    await orchestrator.journal.connect()

    # Start the notifier before any plan loads — in live mode a plan may
    # not exist until the 08:00 job fires. Orchestrator._load_plan() syncs
    # notifier.engine whenever a plan is (re)loaded, in either mode, so the
    # ordering here doesn't matter.
    await orchestrator.start_notifier()

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler for SIGTERM

    try:
        if args.sim:
            await orchestrator.run_sim(args.plan, args.ticks, args.speed)
        else:
            run_task = asyncio.ensure_future(orchestrator.run_live())
            stop_task = asyncio.ensure_future(stop_event.wait())
            done, pending = await asyncio.wait(
                {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    finally:
        await orchestrator.shutdown()

    return 0


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python main.py")
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Replay ticks from a CSV instead of the live Polygon websocket",
    )
    parser.add_argument("--plan", type=Path, help="[--sim] Plan JSON to load (required with --sim)")
    parser.add_argument("--ticks", type=Path, help="[--sim] Ticks CSV, ts_et,price,yield_10y (required with --sim)")
    parser.add_argument("--speed", default="0", help="[--sim] 0 = as fast as possible, e.g. 60x = 60x real time")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)
    if args.sim and (args.plan is None or args.ticks is None):
        parser.error("--sim requires --plan and --ticks")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
