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

    # -- operator actions ---------------------------------------------

    def confirm(self, trigger_id: str) -> None:
        state = self.states[trigger_id]
        if state.status == TriggerStatus.FIRED:
            state.status = TriggerStatus.CONFIRMED

    def skip(self, trigger_id: str) -> None:
        state = self.states[trigger_id]
        if state.status == TriggerStatus.FIRED:
            state.status = TriggerStatus.SKIPPED

    def mark_filled(self, trigger_id: str) -> None:
        state = self.states[trigger_id]
        if state.status == TriggerStatus.CONFIRMED:
            state.status = TriggerStatus.OPEN

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
                new_states[t.id] = TriggerState(trigger=t)

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
            restored.append(tid)
        return restored

    # -- main evaluation loop -------------------------------------------

    def evaluate(self, tick: Tick) -> list[EngineEvent]:
        events: list[EngineEvent] = list(self._pending_events)
        self._pending_events.clear()

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

        # Recorded before any gate, so a trigger first seen while a
        # not_before/yield gate blocks it can still fire when the gate opens
        # with price inside the zone — that's a real "conditions now met".
        first_observation = not state.observed
        state.observed = True

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

        if self._has_price_condition(trigger):
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

        return True

    def _jumped_through_zone(self, trigger: Trigger, tick: Tick) -> bool:
        between = trigger.condition.price_between
        prev = self.prev_tick
        if between is None or prev is None:
            return False
        low, high = between
        return (prev.price > high and tick.price < low) or (
            prev.price < low and tick.price > high
        )

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
            return TriggerFired(trigger_id=trigger.id, tick=tick)
        return None

    def _check_stop(self, state: TriggerState, tick: Tick) -> StopHit | None:
        trigger = state.trigger
        prev = self.prev_tick
        stop = trigger.stop
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

        if len(state.targets_hit) == len(trigger.targets):
            state.status = TriggerStatus.CLOSED
