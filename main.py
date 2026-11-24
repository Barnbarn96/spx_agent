"""asyncio entrypoint: supervises the live loop, wires feed -> engine ->
agent -> notify -> journal together. Phase 1 is alerts-only — no order
routing. See SPEC.md.

--sim replays ticks from a CSV instead of the live Polygon websocket, so the
full loop (including real Discord confirm/skip/mark-filled buttons) can be
exercised end-to-end without market hours. Live mode (feed.py's
PolygonFeed, scheduler.py's cron jobs) has not been exercised against real
market data in this environment — see docs/v1/ROLLOUT_PLAN.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
import inputs
import releases
from feed import FeedManager, FeedStale, MoomooFeed, PolygonFeed, ReplayTickSource, TickSource
from journal import JournalDB
from replay import load_ticks
from risk import RiskManager, is_early
from schema import (
    AlreadyInZone,
    Decision,
    EngineWarning,
    FlatByReached,
    Invalidated,
    Plan,
    ReviewRequired,
    ScheduledEvent,
    StopHit,
    StopMoved,
    TargetHit,
    Tick,
    TriggerFired,
    ZoneSkipped,
)
from scheduler import AgentScheduler, SchedulerCallbacks
from triggers import TriggerEngine, TriggerStatus

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
            max_stops_per_day=config.MAX_STOPS_PER_DAY,
        )
        self.notifier = None  # built in run(), only if Discord is configured
        self.scheduler: AgentScheduler | None = None
        self._feed_source: TickSource | None = None  # set in run_live(); moomoo calibrates from this
        self._plan_file_text: str | None = None  # last plan file contents loaded, to detect a replan
        # v2 operator inputs (inputs.py), refreshed by poll_inputs()
        self.manual_yield: inputs.YieldInput | None = None
        self.gex: inputs.GexInput | None = None
        self._yield_mtime: float | None = None
        self._gex_mtime: float | None = None
        self._yield_nudge_sent = False
        # v2: decide() runs as background tasks so ticks keep flowing
        self._decide_tasks: set[asyncio.Task] = set()
        # v2: last engine snapshot written to state/engine_<date>.json
        self._saved_engine_json: str | None = None
        # Discord messages produced before the notifier was ready (e.g. at restore)
        self._pending_notices: list[str] = []

    # -- engine state persistence (v2) --------------------------------------

    def _engine_state_path(self, plan_date) -> Path:
        return Path(config.STATE_DIR) / f"engine_{plan_date}.json"

    def _restore_engine_state(self) -> None:
        """On the first plan load of a process, pick up where a previous run
        of main.py left off today — OPEN trades keep their stops. Not used
        in --sim, which must not mix replayed state with live state."""
        assert self.engine is not None and self.plan is not None
        if self.sim:
            return
        path = self._engine_state_path(self.plan.date)
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            print(f"[main] could not read {path} ({e}); starting with fresh trigger state", file=sys.stderr)
            return
        restored = self.engine.restore_snapshot(snapshot)
        if not restored:
            return
        # A FIRED trigger was waiting on decide() or on a button tap when the
        # last run stopped; its alert may never have been sent. Re-arm it so
        # a fresh entry alerts again, rather than leaving it stuck.
        rearmed = []
        for tid in restored:
            state = self.engine.states[tid]
            if state.status == TriggerStatus.FIRED:
                state.status = TriggerStatus.ARMED
                state.was_in_zone = True  # needs price to leave and re-enter
                rearmed.append(tid)
        summary = ", ".join(f"{tid}={self.engine.states[tid].status.value}" for tid in restored)
        print(f"[main] restored engine state from {path}: {summary}", file=sys.stderr)
        note = f"Restarted: restored trigger state ({summary})."
        if rearmed:
            note += (
                f" Re-armed {', '.join(rearmed)}: they had fired but weren't confirmed before the "
                f"restart, so buttons on their earlier alerts no longer apply."
            )
        self._pending_notices.append(note)

    def save_engine_state(self) -> None:
        """Write the engine snapshot if it changed since the last write.
        Called after every tick, from the input poller (to catch Discord
        button taps) and at shutdown. Skipped in --sim."""
        if self.engine is None or self.sim:
            return
        text = json.dumps(self.engine.to_snapshot(), indent=1, sort_keys=True)
        if text == self._saved_engine_json:
            return
        try:
            inputs.atomic_write(self._engine_state_path(self.engine.plan.date), text)
            self._saved_engine_json = text
        except OSError as e:
            print(f"[main] could not save engine state ({e})", file=sys.stderr)

    # -- plan lifecycle ---------------------------------------------------

    def _load_plan(self, plan: Plan, source: str) -> bool:
        """Returns True if this load cleared an active invalidation (see
        TriggerEngine.load_plan) — callers should alert on that."""
        self.plan = plan
        cleared_invalidation = False
        if self.engine is None:
            self.engine = TriggerEngine(plan)
            self._restore_engine_state()
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
        self._calibrate_feed()
        return cleared_invalidation

    def _calibrate_feed(self) -> None:
        """MoomooFeed estimates SPX as SPY x ratio. v2 prefers a close-to-
        close ratio (plan.spx_prev_close / SPY's previous close); plans
        without spx_prev_close fall back to v1's spx_reference calibration."""
        if self.plan is None or not isinstance(self._feed_source, MoomooFeed):
            return
        if self.plan.spx_prev_close is not None:
            self._feed_source.calibrate_from_prev_close(self.plan.spx_prev_close)
        else:
            print(
                "[main] plan has no spx_prev_close — falling back to calibrating the "
                "SPY->SPX ratio against spx_reference (less accurate)",
                file=sys.stderr,
            )
            self._feed_source.calibrate(self.plan.spx_reference)

    async def on_generate_plan(self) -> None:
        """Scheduler callback: 08:00 ET pre-market plan generation. Live
        mode only — best-effort context gathering, unverified against a
        real pre-market session (see docs/v1/ROLLOUT_PLAN.md)."""
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

    def _attach_yield(self, tick: Tick) -> Tick:
        """A fresh manual 10Y (state/yield.json) overrides the feed's; an
        expired one is dropped so yield conditions fail closed. Feed yields
        quoted TNX-style (x10) are normalized to percent."""
        now = tick.ts.astimezone(ET)
        if self.manual_yield is not None and self.manual_yield.is_fresh(now):
            return tick.model_copy(update={"yield_10y": self.manual_yield.value})
        if tick.yield_10y is not None:
            try:
                return tick.model_copy(update={"yield_10y": inputs.normalize_yield(tick.yield_10y)})
            except ValueError:
                return tick.model_copy(update={"yield_10y": None})
        return tick

    async def process_tick(self, tick: Tick) -> None:
        tick = self._attach_yield(tick)
        self.last_price = tick.price
        self.last_yield = tick.yield_10y
        if self.engine is None:
            return

        events = self.engine.evaluate(tick)
        for event in events:
            await self.journal.log_event(event)
            await self._handle_event(event)
        self.save_engine_state()

    async def _handle_event(self, event) -> None:
        if isinstance(event, TriggerFired):
            # v2: decide() can take up to DECIDE_TIMEOUT_S. Run it in the
            # background so stops/targets on other trades keep being checked.
            self._spawn_decide(event)
        elif isinstance(event, AlreadyInZone) and self.notifier:
            await self.notifier.send_text(
                f"`{event.trigger_id}`: SPX {event.tick.price:.2f} is already inside this "
                f"trigger's zone. Not firing on a setup that may be half over; it will "
                f"alert if price leaves and re-enters."
            )
        elif isinstance(event, ZoneSkipped) and self.notifier:
            low, high = self.engine.states[event.trigger_id].trigger.condition.price_between
            await self.notifier.send_text(
                f"`{event.trigger_id}`: SPX jumped {event.from_price:.2f} -> {event.tick.price:.2f}, "
                f"straight through the {low:g}-{high:g} entry zone without trading in it. "
                f"Not firing outside the zone; it will alert if price comes back into it."
            )
        elif isinstance(event, StopMoved) and self.notifier:
            await self.notifier.send_text(
                f"**STOP MOVED** `{event.trigger_id}`: {event.old_stop:g} -> {event.new_stop:g} "
                f"({event.reason}). Move your stop order to match."
            )
        elif isinstance(event, TargetHit) and self.notifier:
            await self.notifier.send_target_hit(event)
        elif isinstance(event, StopHit):
            if self.notifier:
                await self.notifier.send_stop_hit(event)
            await self._record_stop(event)
        elif isinstance(event, Invalidated) and self.notifier:
            await self.notifier.send_invalidated(event)
        elif isinstance(event, FlatByReached) and self.notifier:
            await self.notifier.send_flat_by(event)
        elif isinstance(event, ReviewRequired) and self.notifier:
            await self.notifier.send_review_required(event)
        elif isinstance(event, EngineWarning):
            pass  # journaled above; not Discord-worthy per SPEC.md's alert list

    async def _record_stop(self, event: StopHit) -> None:
        """v2: the Nth stop of the ET day (MAX_STOPS_PER_DAY) halts new entries."""
        if self.risk.record_stop(event.tick.ts):
            print(f"[main] {self.risk.max_stops_per_day} stops today: HALT created", file=sys.stderr)
            if self.notifier:
                await self.notifier.send_halt(
                    f"{self.risk.max_stops_per_day} stops: no new entries today (state/HALT)"
                )

    async def restore_stop_count(self) -> None:
        """Rebuild today's stop count from the journal, so a restart after
        one stop still halts on the next. Live mode only."""
        if self.sim:
            return
        now = datetime.now(ET)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = await self.journal.events_since(midnight.isoformat())
        count = sum(1 for e in events if e["type"] == "stop_hit")
        self.risk.seed_stops(count, now)
        if count:
            print(f"[main] restored {count} stop(s) already hit today", file=sys.stderr)

    def _spawn_decide(self, event: TriggerFired) -> None:
        task = asyncio.ensure_future(self._handle_trigger_fired(event))
        self._decide_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._decide_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                print(
                    f"[main] decide/alert for {event.trigger_id} failed: {t.exception()!r}",
                    file=sys.stderr,
                )

        task.add_done_callback(_done)

    async def wait_for_decisions(self) -> None:
        """Let in-flight decide() calls finish (end of --sim, shutdown, tests)."""
        while self._decide_tasks:
            await asyncio.gather(*list(self._decide_tasks), return_exceptions=True)

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
            "notes": self._decide_notes(event.tick),
        }

        start = time.monotonic()
        decision: Decision = await agent.decide(trigger, self.plan, market_context)
        latency_ms = (time.monotonic() - start) * 1000
        entry_timing = "early" if is_early(self.plan, event.tick.ts) else "regular"
        decision_id = await self.journal.log_decision(
            event.trigger_id, decision, latency_ms, entry_timing=entry_timing
        )

        # decide() ran in the background, so the world may have moved on: a
        # replan can have dropped or reset the trigger. Only alert on a
        # trigger that is still waiting for the operator.
        state = self.engine.states.get(event.trigger_id)
        if state is None or state.status != TriggerStatus.FIRED:
            now_status = "removed" if state is None else state.status.value
            print(
                f"[main] {event.trigger_id} is {now_status} after decide(); not alerting",
                file=sys.stderr,
            )
            return

        if decision.action == "WAIT":
            self.engine.wait(event.trigger_id, event.tick)
            if self.notifier:
                await self.notifier.send_wait_rearmed(trigger, event.tick, decision)
        elif self.notifier:
            await self.notifier.send_trigger_fired(trigger, event.tick, decision, decision_id)
        # else: no Discord configured — decision is journaled; operator
        # must review journal.db manually to confirm/skip.

    def _decide_notes(self, tick: Tick) -> str:
        """Extra context for decide()'s "Additional context" slot, which v1
        never filled."""
        now = tick.ts.astimezone(ET)
        notes = []
        if self.manual_yield is not None:
            age_min = int(self.manual_yield.age(now).total_seconds() // 60)
            state = "fresh" if self.manual_yield.is_fresh(now) else "EXPIRED, not used"
            notes.append(f"10Y {self.manual_yield.value:g}% entered by hand {age_min} min ago ({state})")
        else:
            notes.append("10Y: no manual reading entered today")
        if self.gex is not None:
            notes.append(self.gex.summary(tick.price, now))
        if isinstance(self._feed_source, MoomooFeed):
            notes.append(f"SPX is estimated from SPY (ratio {self._feed_source.ratio:.4f}, {self._feed_source.ratio_source})")
        if self.plan is not None and self.plan.notes:
            notes.append(f"Plan notes: {self.plan.notes}")
        return " | ".join(notes)

    # -- operator inputs (v2), polled on a timer -------------------------------

    async def poll_inputs(self) -> None:
        """One polling pass: plan file, manual yield, GEX, and time-based
        reminders. Runs on its own timer (run_input_poller), so a quiet feed
        or a missing plan never stops inputs from being picked up."""
        await self._reload_plan_if_file_changed()
        await self._reload_yield_if_changed()
        await self._reload_gex_if_changed()
        await self._check_yield_nudge(datetime.now(ET))
        if self.notifier and self._pending_notices:
            notices, self._pending_notices = self._pending_notices, []
            for text in notices:
                await self.notifier.send_text(text)
        # Discord taps (EXECUTE/SKIP/Mark Filled) change engine state outside
        # the tick path; persist them here.
        self.save_engine_state()

    async def run_input_poller(self) -> None:
        while True:
            try:
                await self.poll_inputs()
            except Exception as e:  # never let a bad input file kill the loop
                print(f"[main] input poll failed: {e!r}", file=sys.stderr)
            await asyncio.sleep(config.INPUT_POLL_S)

    async def _reload_yield_if_changed(self) -> None:
        mtime = inputs.file_mtime(inputs.YIELD_FILE)
        if mtime is None or mtime == self._yield_mtime:
            return
        y = inputs.read_yield()
        if y is None:
            return  # unreadable — retry on the next poll
        self._yield_mtime = mtime
        self.manual_yield = y
        self._yield_nudge_sent = False
        print(f"[main] 10Y yield updated: {y.value:g}% ({y.source}, {y.ts.isoformat()})", file=sys.stderr)

    async def _reload_gex_if_changed(self) -> None:
        mtime = inputs.file_mtime(inputs.GEX_FILE)
        if mtime is None or mtime == self._gex_mtime:
            return
        gex = inputs.read_gex()
        if gex is None:
            return
        self._gex_mtime = mtime
        self.gex = gex
        now = datetime.now(ET)
        print(f"[main] {gex.summary(self.last_price, now)}", file=sys.stderr)
        await self._check_spx_estimate_drift(gex, now)

    async def _check_spx_estimate_drift(self, gex: inputs.GexInput, now: datetime) -> None:
        """A GEX screenshot shows the real .SPX price. If it's recent, compare
        it with the SPY-based estimate and warn on drift."""
        if gex.spx_in_shot is None or self.last_price is None:
            return
        if not isinstance(self._feed_source, MoomooFeed):
            return
        if abs((now - gex.as_of).total_seconds()) > 180:
            return  # too old to compare against the current estimate
        drift = self.last_price - gex.spx_in_shot
        if abs(drift) > config.SPX_ESTIMATE_DRIFT_WARN_PTS and self.notifier:
            await self.notifier.send_text(
                f"SPX estimate check: SPY-based estimate {self.last_price:.2f} vs screenshot "
                f"{gex.spx_in_shot:.2f} ({drift:+.2f} pts). Ratio {self._feed_source.ratio:.4f} "
                f"from {self._feed_source.ratio_source}."
            )

    def _plan_uses_yield_levels(self) -> bool:
        if self.engine is None:
            return False
        return any(
            s.status.value == "ARMED"
            and (s.trigger.condition.yield_below is not None or s.trigger.condition.yield_above is not None)
            for s in self.engine.states.values()
        )

    async def _check_yield_nudge(self, now: datetime) -> None:
        """Remind the operator once per lapse when an armed trigger depends on
        the 10Y and the manual reading is missing or expired during RTH."""
        rth = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)
        fresh = self.manual_yield is not None and self.manual_yield.is_fresh(now)
        if not rth or fresh or not self._plan_uses_yield_levels():
            return
        if self._yield_nudge_sent:
            return
        self._yield_nudge_sent = True
        if self.notifier:
            last = (
                f"last {self.manual_yield.value:g}% at {self.manual_yield.ts.astimezone(ET):%H:%M} ET"
                if self.manual_yield is not None
                else "none entered today"
            )
            await self.notifier.send_text(
                f"10Y yield needed ({last}). Triggers with yield conditions are paused until "
                f"you send a fresh one: `/intraday <spx> <10y>` in Claude Code, or "
                f"`python tools/set_yield.py 4.93`."
            )

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
        # v2: detect changes by content, not mtime. Two writes inside the
        # filesystem's timestamp resolution share an mtime, and a replan
        # written that quickly was silently missed. The file is small and
        # polled every few seconds, so reading it each time is cheap.
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return
        if raw == self._plan_file_text:
            return

        try:
            new_plan = Plan.model_validate_json(raw)
        except ValidationError as e:
            # A partially-written file is possible if a writer bypassed the
            # atomic tools; leave it unrecorded so the next poll retries.
            print(f"[main] plan file changed but did not parse ({e}); keeping current plan", file=sys.stderr)
            return

        self._plan_file_text = raw
        if self.plan is not None and new_plan == self.plan:
            return  # rewritten but materially identical

        old_plan = self.plan
        await self.journal.log_plan(new_plan, "manual_replan")
        cleared_invalidation = self._load_plan(new_plan, "manual_replan")
        if old_plan is None:
            # v2: the first plan of the day usually arrives this way, written
            # by Claude Code's /premarket while main.py is already running.
            print(f"[main] loaded today's plan from {path}", file=sys.stderr)
            if self.notifier:
                await self.notifier.send_plan_summary(new_plan)
            return
        print(f"[main] hot-swapped to re-planned {path}", file=sys.stderr)
        if self.notifier:
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
            raw = path.read_text(encoding="utf-8")
            plan = Plan.model_validate_json(raw)
            self._plan_file_text = raw
            self._load_plan(plan, "daily")
            print(f"[main] loaded existing plan for {today} from {path}", file=sys.stderr)
        else:
            waiting_for = (
                f"the {config.PLAN_GENERATION_TIME_ET} ET scheduled job"
                if config.PLAN_AUTOGEN
                else "/premarket in Claude Code (picked up automatically when written)"
            )
            print(f"[main] no plan for {today} on disk yet — waiting for {waiting_for}", file=sys.stderr)

    async def run_live(self) -> None:
        await self.restore_stop_count()
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
        self._calibrate_feed()  # in case a plan already loaded above

        print(
            f"[main] feed source: {config.FEED_SOURCE}. Streaming ticks — "
            f"heartbeat every {config.HEARTBEAT_INTERVAL_S:g}s.",
            file=sys.stderr,
        )
        tick_count = 0
        last_heartbeat = time.monotonic()

        # v2: plan / yield / GEX files are polled on their own timer, so they
        # are picked up even before the feed produces its first tick.
        poller = asyncio.ensure_future(self.run_input_poller())
        try:
            await self._consume_feed(source, tick_count, last_heartbeat)
        finally:
            poller.cancel()

    async def _consume_feed(self, source: TickSource, tick_count: int, last_heartbeat: float) -> None:
        feed_manager = FeedManager(source)
        async for item in feed_manager.watch():
            if isinstance(item, FeedStale):
                if self.notifier:
                    await self.notifier.send_feed_stale(item.seconds_since_last_tick)
                continue

            tick_count += 1
            await self.process_tick(item)

            now = time.monotonic()
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
        await self.wait_for_decisions()

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
        for task in list(self._decide_tasks):
            task.cancel()  # its trigger stays FIRED; a restart re-arms it (see _restore_engine_state)
        self.save_engine_state()
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
