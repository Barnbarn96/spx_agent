"""Deterministic trigger engine. Pure, synchronous, no I/O.

Takes ticks in, emits EngineEvents out. All time comparisons for
not_before/not_after/flat_by are done in America/New_York against the
tick's own timestamp (which must already be tz-aware).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from schema import (
    AlreadyInZone,
    EngineEvent,
    EngineWarning,
    FlatByReached,
    Invalidated,
    Plan,
    ReviewRequired,
    StopHit,
    StopMoved,
    TargetHit,
    Tick,
    Trigger,
    TriggerFired,
    ZoneSkipped,
)

ET = ZoneInfo("America/New_York")

DEFAULT_YIELD_WINDOW_MIN = 5
DEFAULT_YIELD_EPSILON = 0.005
DEFAULT_WAIT_COOLDOWN_SEC = 300

# v2: bar timeframes the engine builds from ticks, in minutes
_TIMEFRAMES = {"1m": 1, "5m": 5}


class TriggerStatus(str, Enum):
    ARMED = "ARMED"
    FIRED = "FIRED"
    CONFIRMED = "CONFIRMED"
    OPEN = "OPEN"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


# Statuses that count as "in flight" for requires_not_filled gating: the
# referenced trigger has fired but not yet resolved (skipped/expired/closed).
_ACTIVE_STATUSES = {
    TriggerStatus.FIRED,
    TriggerStatus.CONFIRMED,
    TriggerStatus.OPEN,
}


def _hhmm_on_date(hhmm: str, ref_ts: datetime) -> datetime:
    hh, mm = (int(p) for p in hhmm.split(":"))
    local_ref = ref_ts.astimezone(ET)
    return local_ref.replace(
        hour=hh, minute=mm, second=0, microsecond=0
    )


@dataclass
class TriggerState:
    trigger: Trigger
    status: TriggerStatus = TriggerStatus.ARMED
    # Zone membership on the last tick that reached the price check. None =
    # unknown (no such tick yet, a time/yield gate has been blocking, or a
    # WAIT re-arm) — the next in-zone tick may fire.
    was_in_zone: bool | None = None
    targets_hit: set[float] = field(default_factory=set)
    cooldown_until: datetime | None = None
    warned_no_yield: bool = False
    # v2: False until the engine has evaluated this trigger once. The very
    # first evaluation never fires on price already inside the zone (startup,
    # new plan, replan) — it emits AlreadyInZone instead.
    observed: bool = False
    # v2 first_touch: entries into the zone today (tick prices, gates ignored)
    touches: int = 0
    in_zone_last_tick: bool | None = None
    # v2 confirm "close": the last bar close evaluated for this trigger
    last_close: float | None = None
    # v2 confirm "dwell": when the current stay in the zone began
    zone_since: datetime | None = None
    # v2: price at the tick that fired, and the entry price once filled
    # (the operator's fill if given, else the fire price)
    fire_price: float | None = None
    entry_price: float | None = None
    # v2: stop moved by a then_stop target (breakeven); None = trigger.stop
    stop_override: float | None = None

    @property
    def stop(self) -> float:
        return self.stop_override if self.stop_override is not None else self.trigger.stop


def _same_zone(a: Trigger, b: Trigger) -> bool:
    """Same price condition, so touches of one are touches of the other."""
    ca, cb = a.condition, b.condition
    return (ca.price_between, ca.price_above, ca.price_below) == (
        cb.price_between,
        cb.price_above,
        cb.price_below,
    )


class TriggerEngine:
    def __init__(
        self,
        plan: Plan,
        yield_window_min: int = DEFAULT_YIELD_WINDOW_MIN,
        yield_epsilon: float = DEFAULT_YIELD_EPSILON,
    ) -> None:
        self.plan = plan
        self.yield_window_min = yield_window_min
        self.yield_epsilon = yield_epsilon

        self.states: dict[str, TriggerState] = {
            t.id: TriggerState(trigger=t) for t in plan.triggers
        }
        self.invalidated = False
        self.prev_tick: Tick | None = None
        self._yield_ticks: deque[tuple[datetime, float]] = deque()
        self._flat_by_reached = False
        self._pending_events: list[EngineEvent] = []
        # v2 bars: current bucket and last price per timeframe, and the
        # close produced on the current tick (None if no bar closed)
        self._bar_bucket: dict[str, tuple] = {}
        self._bar_last: dict[str, float] = {}
        self._closed: dict[str, float | None] = {}
        # v2: recent completed bars per timeframe, (bar start "HH:MM" ET, close),
        # for decide()'s context. Not persisted: a restart starts empty.
        self._recent_closes: dict[str, deque[tuple[str, float]]] = {
            tf: deque(maxlen=24) for tf in _TIMEFRAMES
        }

    # -- operator actions ---------------------------------------------

    def confirm(self, trigger_id: str) -> None:
        state = self.states[trigger_id]
        if state.status == TriggerStatus.FIRED:
            state.status = TriggerStatus.CONFIRMED

    def skip(self, trigger_id: str) -> None:
        state = self.states[trigger_id]
        if state.status == TriggerStatus.FIRED:
            state.status = TriggerStatus.SKIPPED

    def mark_filled(self, trigger_id: str, fill_price: float | None = None) -> None:
        """Position opened. fill_price is the SPX level the operator got in
        at; without it the fire price stands in as the entry (used by
        then_stop="breakeven")."""
        state = self.states[trigger_id]
        if state.status == TriggerStatus.CONFIRMED:
            state.status = TriggerStatus.OPEN
            state.entry_price = fill_price if fill_price is not None else state.fire_price

    def wait(
        self, trigger_id: str, tick: Tick, cooldown_sec: int = DEFAULT_WAIT_COOLDOWN_SEC
    ) -> None:
        """Operator/agent requested WAIT: re-arm with a cooldown."""
        state = self.states[trigger_id]
        if state.status != TriggerStatus.FIRED:
            return
        state.status = TriggerStatus.ARMED
        state.cooldown_until = tick.ts + timedelta(seconds=cooldown_sec)
        state.was_in_zone = None  # forces fresh debounce once cooldown ends

    def load_plan(self, new_plan: Plan) -> bool:
        """Hot-swap onto a new plan. Returns True if this plan supersedes an
        invalidation that had halted new entries for the day — a re-plan is
        a deliberate operator decision that the old plan (and its
        invalidation) no longer holds, so `invalidated`/`_flat_by_reached`
        are cleared to let the new plan's triggers fire. Callers should
        alert on a True return: clearing an invalidation is not something
        that should happen silently."""
        was_invalidated = self.invalidated
        self.invalidated = False
        self._flat_by_reached = False

        old_states = self.states
        new_states: dict[str, TriggerState] = {}
        new_ids = {t.id for t in new_plan.triggers}

        for t in new_plan.triggers:
            old = old_states.get(t.id)
            if old is not None and old.status in (
                TriggerStatus.FIRED,
                TriggerStatus.CONFIRMED,
                TriggerStatus.OPEN,
            ):
                old.trigger = t  # new levels apply, status preserved
                new_states[t.id] = old
            else:
                fresh = TriggerState(trigger=t)
                if old is not None and _same_zone(old.trigger, t):
                    # v2: "first touch" means today's first, replan or not
                    fresh.touches = old.touches
                    fresh.in_zone_last_tick = old.in_zone_last_tick
                new_states[t.id] = fresh

        for tid, old in old_states.items():
            if tid in new_ids:
                continue
            if old.status == TriggerStatus.OPEN:
                # never silently drop an OPEN trigger
                new_states[tid] = old
                self._pending_events.append(
                    ReviewRequired(
                        trigger_id=tid,
                        reason="trigger removed by re-plan while OPEN",
                    )
                )
            # else: dropped

        self.plan = new_plan
        self.states = new_states
        return was_invalidated

    # -- persistence (v2) -------------------------------------------------

    def to_snapshot(self) -> dict:
        """Everything needed to resume after a restart: per-trigger status,
        debounce/first-sight flags, targets hit, cooldowns, plus the
        engine-wide invalidation and flat-by flags. JSON-serializable."""
        return {
            "plan_date": self.plan.date.isoformat(),
            "invalidated": self.invalidated,
            "flat_by_reached": self._flat_by_reached,
            "states": {
                tid: {
                    "trigger": s.trigger.model_dump(mode="json"),
                    "status": s.status.value,
                    "was_in_zone": s.was_in_zone,
                    "targets_hit": sorted(s.targets_hit),
                    "cooldown_until": s.cooldown_until.isoformat() if s.cooldown_until else None,
                    "warned_no_yield": s.warned_no_yield,
                    "observed": s.observed,
                    "touches": s.touches,
                    "in_zone_last_tick": s.in_zone_last_tick,
                    "last_close": s.last_close,
                    "zone_since": s.zone_since.isoformat() if s.zone_since else None,
                    "fire_price": s.fire_price,
                    "entry_price": s.entry_price,
                    "stop_override": s.stop_override,
                }
                for tid, s in self.states.items()
            },
        }

    def restore_snapshot(self, snapshot: dict) -> list[str]:
        """Re-apply a to_snapshot() taken earlier the same day. Triggers in
        the current plan take the saved status/flags; a saved trigger that
        is in flight (FIRED/CONFIRMED/OPEN) but missing from the plan is
        kept, using its saved definition, so an open position never loses
        its stop. Returns the ids restored. A snapshot for a different plan
        date is ignored."""
        if snapshot.get("plan_date") != self.plan.date.isoformat():
            return []
        self.invalidated = bool(snapshot.get("invalidated", False))
        self._flat_by_reached = bool(snapshot.get("flat_by_reached", False))
        restored = []
        for tid, saved in snapshot.get("states", {}).items():
            status = TriggerStatus(saved["status"])
            state = self.states.get(tid)
            if state is None:
                if status not in _ACTIVE_STATUSES:
                    continue
                state = TriggerState(trigger=Trigger.model_validate(saved["trigger"]))
                self.states[tid] = state
            state.status = status
            state.was_in_zone = saved.get("was_in_zone")
            state.targets_hit = set(saved.get("targets_hit", []))
            cooldown = saved.get("cooldown_until")
            state.cooldown_until = datetime.fromisoformat(cooldown) if cooldown else None
            state.warned_no_yield = bool(saved.get("warned_no_yield", False))
            state.observed = bool(saved.get("observed", False))
            state.touches = int(saved.get("touches", 0))
            state.in_zone_last_tick = saved.get("in_zone_last_tick")
            state.last_close = saved.get("last_close")
            since = saved.get("zone_since")
            state.zone_since = datetime.fromisoformat(since) if since else None
            state.fire_price = saved.get("fire_price")
            state.entry_price = saved.get("entry_price")
            state.stop_override = saved.get("stop_override")
            restored.append(tid)
        return restored

    # -- main evaluation loop -------------------------------------------

    def evaluate(self, tick: Tick) -> list[EngineEvent]:
        events: list[EngineEvent] = list(self._pending_events)
        self._pending_events.clear()
        self._update_bars(tick)

        if tick.yield_10y is not None:
            self._yield_ticks.append((tick.ts, tick.yield_10y))
            cutoff = tick.ts - timedelta(minutes=self.yield_window_min)
            while self._yield_ticks and self._yield_ticks[0][0] < cutoff:
                self._yield_ticks.popleft()

        if not self.invalidated:
            inv_event = self._check_invalidation(tick)
            if inv_event is not None:
                events.append(inv_event)
                self.invalidated = True

        if self.invalidated:
            for state in self.states.values():
                if state.status == TriggerStatus.OPEN:
                    stop_evt = self._check_stop(state, tick)
                    if stop_evt is not None:
                        events.append(stop_evt)
                        state.status = TriggerStatus.CLOSED
        else:
            for state in self.states.values():
                if state.status == TriggerStatus.ARMED:
                    fired_evt = self._evaluate_armed(state, tick, events)
                    if fired_evt is not None:
                        events.append(fired_evt)
                elif state.status == TriggerStatus.OPEN:
                    self._evaluate_open(state, tick, events)

        if any(s.status == TriggerStatus.OPEN for s in self.states.values()):
            if not self._flat_by_reached:
                flat_by_dt = _hhmm_on_date(self.plan.flat_by, tick.ts)
                if tick.ts.astimezone(ET) >= flat_by_dt:
                    self._flat_by_reached = True
                    events.append(FlatByReached(tick=tick))

        self.prev_tick = tick
        return events

    # -- internal helpers -------------------------------------------------

    def _check_invalidation(self, tick: Tick) -> Invalidated | None:
        inv = self.plan.invalidation
        prev = self.prev_tick
        if inv.below is not None:
            crossed = tick.price <= inv.below and (
                prev is None or prev.price > inv.below
            )
            if crossed:
                return Invalidated(level=inv.below, tick=tick)
        if inv.above is not None:
            crossed = tick.price >= inv.above and (
                prev is None or prev.price < inv.above
            )
            if crossed:
                return Invalidated(level=inv.above, tick=tick)
        return None

    def _in_zone(self, trigger: Trigger, price: float) -> bool:
        cond = trigger.condition
        if cond.price_between is not None:
            low, high = cond.price_between
            if not (low <= price <= high):
                return False
        if cond.price_above is not None and not (price > cond.price_above):
            return False
        if cond.price_below is not None and not (price < cond.price_below):
            return False
        return True

    def _has_price_condition(self, trigger: Trigger) -> bool:
        cond = trigger.condition
        return (
            cond.price_between is not None
            or cond.price_above is not None
            or cond.price_below is not None
        )

    def _yield_direction(self) -> str | None:
        if not self._yield_ticks:
            return None
        latest = self._yield_ticks[-1][1]
        mean = sum(v for _, v in self._yield_ticks) / len(self._yield_ticks)
        if latest < mean - self.yield_epsilon:
            return "falling"
        if latest > mean + self.yield_epsilon:
            return "rising"
        return "flat"

    def yield_direction(self) -> str | None:
        """Public accessor for the current rolling-window yield direction —
        used by callers (e.g. main.py) building decide() market context."""
        return self._yield_direction()

    def _condition_met(
        self, state: TriggerState, tick: Tick, events: list[EngineEvent]
    ) -> bool:
        trigger = state.trigger
        cond = trigger.condition
        now_et = tick.ts.astimezone(ET)
        has_price = self._has_price_condition(trigger)
        mode = cond.confirm.type if cond.confirm is not None else "tick"

        # Recorded before any gate, so a trigger first seen while a
        # not_before/yield gate blocks it can still fire when the gate opens
        # with price inside the zone — that's a real "conditions now met".
        first_observation = not state.observed
        state.observed = True

        # v2: bookkeeping that must see every tick, gated or not.
        candidate = True
        if has_price:
            self._track_touches(state, tick)
            if mode == "close":
                candidate = self._close_candidate(state, tick, events, first_observation)
            elif mode == "dwell":
                candidate = self._dwell_candidate(state, tick, events, first_observation)

        if not self._gates_open(state, tick, now_et, events):
            return False

        if has_price and mode == "tick":
            candidate = self._tick_candidate(state, tick, events, first_observation)

        if candidate and cond.first_touch and state.touches > 1:
            return False  # an earlier touch today already used up "first touch"
        return candidate

    def _gates_open(
        self, state: TriggerState, tick: Tick, now_et: datetime, events: list[EngineEvent]
    ) -> bool:
        """Time, requires_not_filled and yield conditions."""
        trigger = state.trigger
        cond = trigger.condition

        if cond.not_before is not None:
            if now_et < _hhmm_on_date(cond.not_before, tick.ts):
                return False
        # not_after is enforced by _evaluate_armed (which expires the
        # trigger outright), so it is not re-checked here.

        for ref_id in cond.requires_not_filled:
            ref_state = self.states.get(ref_id)
            if ref_state is not None and ref_state.status in _ACTIVE_STATUSES:
                return False

        if cond.yields_10y is not None:
            direction = self._yield_direction()
            if direction is None:
                if not state.warned_no_yield:
                    state.warned_no_yield = True
                    events.append(
                        EngineWarning(
                            message=(
                                f"trigger {trigger.id!r} requires yields_10y "
                                f"but no yield data has arrived; failing closed"
                            ),
                            trigger_id=trigger.id,
                            tick=tick,
                        )
                    )
                return False
            if direction != cond.yields_10y:
                return False

        if cond.yield_below is not None or cond.yield_above is not None:
            # Level checks use the yield carried on this tick — main.py only
            # attaches a manual yield while it is fresh, so None here means
            # missing or expired. Fail closed, as with yields_10y.
            y = tick.yield_10y
            if y is None:
                if not state.warned_no_yield:
                    state.warned_no_yield = True
                    events.append(
                        EngineWarning(
                            message=(
                                f"trigger {trigger.id!r} has a yield level condition "
                                f"but no fresh 10Y yield is available; failing closed"
                            ),
                            trigger_id=trigger.id,
                            tick=tick,
                        )
                    )
                return False
            state.warned_no_yield = False
            if cond.yield_below is not None and not y <= cond.yield_below:
                return False
            if cond.yield_above is not None and not y >= cond.yield_above:
                return False
        return True

    # -- price-condition modes (v2) ------------------------------------

    def _approach_ok(self, trigger: Trigger, prev_price: float | None) -> bool:
        """Direction of approach for a price_between zone: where price was
        just before it entered."""
        approach = trigger.condition.approach
        if approach is None:
            return True
        if prev_price is None:
            return False
        low, high = trigger.condition.price_between
        return prev_price > high if approach == "from_above" else prev_price < low

    def _track_touches(self, state: TriggerState, tick: Tick) -> None:
        """Count entries into the zone (tick prices, ignoring gates) for
        first_touch. Already inside at first sight counts as a touch, and so
        does a jump clean through the zone."""
        # in_zone_last_tick is None until this trigger's first tick; a replan
        # with an unchanged zone carries it over (load_plan), so a touch that
        # spans the replan isn't counted twice.
        in_now = self._in_zone(state.trigger, tick.price)
        seen_before = state.in_zone_last_tick is not None
        if in_now and not state.in_zone_last_tick:
            state.touches += 1
        elif not in_now and seen_before and self._jumped_through_zone(state.trigger, tick):
            state.touches += 1
        state.in_zone_last_tick = in_now

    def _tick_candidate(
        self, state: TriggerState, tick: Tick, events: list[EngineEvent], first_observation: bool
    ) -> bool:
        """v1 behaviour: any tick entering the zone, debounced, plus the v2
        first-sight, zone-skipped and approach rules."""
        trigger = state.trigger
        in_zone = self._in_zone(trigger, tick.price)
        was_in_zone = state.was_in_zone
        state.was_in_zone = in_zone
        if not in_zone:
            # v2: a fast move can carry price clean across a narrow
            # price_between zone between two ticks. Don't fire — price
            # is now outside the planned entry zone (firing would chase,
            # e.g. a data spike up through a buy-the-dip zone) — but tell
            # the operator. A later move back into the zone still fires,
            # since was_in_zone is now False.
            if not first_observation and self._jumped_through_zone(trigger, tick):
                events.append(
                    ZoneSkipped(trigger_id=trigger.id, from_price=self.prev_tick.price, tick=tick)
                )
            return False
        if was_in_zone:
            # already inside on the previous tick -> debounced, no refire
            return False
        if first_observation:
            # v2: price was already inside the zone the first time the
            # engine saw this trigger. Don't fire on a setup that may
            # be half over; wait for a fresh entry, but say so.
            events.append(AlreadyInZone(trigger_id=trigger.id, tick=tick))
            return False
        prev_price = self.prev_tick.price if self.prev_tick is not None else None
        return self._approach_ok(trigger, prev_price)

    def _close_candidate(
        self, state: TriggerState, tick: Tick, events: list[EngineEvent], first_observation: bool
    ) -> bool:
        """confirm.type == "close": the zone is judged on bar closes. Fires
        on the first close inside the zone after a close (or the first-sight
        price) outside it. Tracked on every bar, gated or not, so a breakout
        that happened while a time gate was closed isn't fired late."""
        trigger = state.trigger
        if first_observation:
            state.was_in_zone = self._in_zone(trigger, tick.price)
            state.last_close = None
            if state.was_in_zone:
                events.append(AlreadyInZone(trigger_id=trigger.id, tick=tick))
            return False
        close = self._closed.get(trigger.condition.confirm.timeframe)
        if close is None:
            return False  # no bar closed on this tick
        in_zone = self._in_zone(trigger, close)
        was_in_zone = state.was_in_zone
        prev_close = state.last_close
        state.was_in_zone = in_zone
        state.last_close = close
        if not in_zone or was_in_zone:
            return False
        return self._approach_ok(trigger, prev_close)

    def _dwell_candidate(
        self, state: TriggerState, tick: Tick, events: list[EngineEvent], first_observation: bool
    ) -> bool:
        """confirm.type == "dwell": price must stay in the zone for
        confirm.seconds after a fresh entry (e.g. "hold below 7,170"). The
        clock runs whether or not a gate is open; the trigger fires on the
        first tick, with gates open, once the dwell is satisfied."""
        trigger = state.trigger
        in_zone = self._in_zone(trigger, tick.price)
        was_in_zone = state.was_in_zone
        state.was_in_zone = in_zone
        if not in_zone:
            state.zone_since = None
            return False
        if first_observation:
            events.append(AlreadyInZone(trigger_id=trigger.id, tick=tick))
            return False
        if not was_in_zone:
            prev_price = self.prev_tick.price if self.prev_tick is not None else None
            # a fresh entry starts the clock, if it came from the right side
            state.zone_since = tick.ts if self._approach_ok(trigger, prev_price) else None
        if state.zone_since is None:
            return False
        return (tick.ts - state.zone_since).total_seconds() >= trigger.condition.confirm.seconds

    def _jumped_through_zone(self, trigger: Trigger, tick: Tick) -> bool:
        between = trigger.condition.price_between
        prev = self.prev_tick
        if between is None or prev is None:
            return False
        low, high = between
        return (prev.price > high and tick.price < low) or (
            prev.price < low and tick.price > high
        )

    # -- bars (v2) -------------------------------------------------------

    def recent_closes(self, timeframe: str = "5m", n: int = 6) -> list[tuple[str, float]]:
        """The last n completed bars as (bar start HH:MM ET, close), oldest first."""
        return list(self._recent_closes[timeframe])[-n:]

    def _update_bars(self, tick: Tick) -> None:
        """Clock-aligned 1m/5m bars in ET, built from ticks. Sets
        self._closed[tf] to the previous bar's close on the first tick of a
        new bar (None otherwise). A bar closes when the next bar's first
        tick arrives, so sparse ticks delay a close, never invent one."""
        local = tick.ts.astimezone(ET)
        for tf, minutes in _TIMEFRAMES.items():
            bucket = (local.date(), local.hour, local.minute - local.minute % minutes)
            current = self._bar_bucket.get(tf)
            self._closed[tf] = self._bar_last[tf] if current is not None and bucket != current else None
            if self._closed[tf] is not None:
                self._recent_closes[tf].append((f"{current[1]:02d}:{current[2]:02d}", self._closed[tf]))
            self._bar_bucket[tf] = bucket
            self._bar_last[tf] = tick.price

    def _evaluate_armed(
        self, state: TriggerState, tick: Tick, events: list[EngineEvent]
    ) -> TriggerFired | None:
        trigger = state.trigger
        now_et = tick.ts.astimezone(ET)

        if state.cooldown_until is not None:
            if now_et < state.cooldown_until.astimezone(ET):
                return None
            state.cooldown_until = None

        if trigger.condition.not_after is not None:
            if now_et > _hhmm_on_date(trigger.condition.not_after, tick.ts):
                state.status = TriggerStatus.EXPIRED
                return None

        if self._condition_met(state, tick, events):
            state.status = TriggerStatus.FIRED
            state.fire_price = tick.price
            return TriggerFired(trigger_id=trigger.id, tick=tick)
        return None

    def _check_stop(self, state: TriggerState, tick: Tick) -> StopHit | None:
        trigger = state.trigger
        prev = self.prev_tick
        stop = state.stop  # v2: a breakeven move overrides trigger.stop
        confirm = trigger.stop_confirm
        if confirm is not None and confirm.type == "close":
            # v2: judged on bar closes only — a wick through the stop inside
            # the bar doesn't count, a close beyond it does.
            close = self._closed.get(confirm.timeframe)
            if close is None:
                return None
            beyond = close <= stop if trigger.side == "long" else close >= stop
            return StopHit(trigger_id=trigger.id, tick=tick) if beyond else None
        if trigger.side == "long":
            crossed = tick.price <= stop and (prev is None or prev.price > stop)
        else:
            crossed = tick.price >= stop and (prev is None or prev.price < stop)
        if crossed:
            return StopHit(trigger_id=trigger.id, tick=tick)
        return None

    def _evaluate_open(
        self, state: TriggerState, tick: Tick, events: list[EngineEvent]
    ) -> None:
        stop_evt = self._check_stop(state, tick)
        if stop_evt is not None:
            events.append(stop_evt)
            state.status = TriggerStatus.CLOSED
            return

        trigger = state.trigger
        prev = self.prev_tick
        for target in trigger.targets:
            if target.level in state.targets_hit:
                continue
            if trigger.side == "long":
                crossed = tick.price >= target.level and (
                    prev is None or prev.price < target.level
                )
            else:
                crossed = tick.price <= target.level and (
                    prev is None or prev.price > target.level
                )
            if crossed:
                state.targets_hit.add(target.level)
                events.append(
                    TargetHit(
                        trigger_id=trigger.id,
                        level=target.level,
                        scale_out_pct=target.scale_out_pct,
                        tick=tick,
                    )
                )
                if target.then_stop == "breakeven":
                    self._move_stop_to_breakeven(state, target, tick, events)

        if len(state.targets_hit) == len(trigger.targets):
            state.status = TriggerStatus.CLOSED

    def _move_stop_to_breakeven(
        self, state: TriggerState, target, tick: Tick, events: list[EngineEvent]
    ) -> None:
        """v2: then_stop="breakeven" — move the stop to the entry price, but
        only ever in the protective direction (never widen it)."""
        entry = state.entry_price
        if entry is None:
            events.append(
                EngineWarning(
                    message=(
                        f"trigger {state.trigger.id!r}: target {target.level:g} asks for a "
                        f"breakeven stop but no entry price is known; stop left at {state.stop:g}"
                    ),
                    trigger_id=state.trigger.id,
                    tick=tick,
                )
            )
            return
        old = state.stop
        tighter = entry > old if state.trigger.side == "long" else entry < old
        if not tighter:
            return
        state.stop_override = entry
        events.append(
            StopMoved(
                trigger_id=state.trigger.id,
                old_stop=old,
                new_stop=entry,
                reason=f"target {target.level:g} hit: stop to breakeven",
                tick=tick,
            )
        )
