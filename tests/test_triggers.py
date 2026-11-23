from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from schema import (
    Action,
    Condition,
    EngineWarning,
    FlatByReached,
    Invalidated,
    Invalidation,
    Plan,
    ReviewRequired,
    StopHit,
    TargetHit,
    Target,
    Tick,
    Trigger,
    TriggerFired,
)
from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")
DATE = "2026-07-01"


def ts(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 7, 1, hh, mm, ss, tzinfo=ET)


def tick(price: float, at: datetime, yield_10y: float | None = None) -> Tick:
    return Tick(ts=at, price=price, yield_10y=yield_10y)


def mk_trigger(
    id: str,
    side: str = "long",
    price_between: tuple[float, float] | None = (100.0, 110.0),
    price_above: float | None = None,
    price_below: float | None = None,
    stop: float = 90.0,
    targets: list[Target] | None = None,
    not_after: str | None = None,
    not_before: str | None = None,
    requires_not_filled: list[str] | None = None,
    yields_10y: str | None = None,
    one_shot: bool = True,
) -> Trigger:
    return Trigger(
        id=id,
        side=side,
        condition=Condition(
            price_between=price_between,
            price_above=price_above,
            price_below=price_below,
            yields_10y=yields_10y,
            not_before=not_before,
            not_after=not_after,
            requires_not_filled=requires_not_filled or [],
        ),
        action=Action(instrument="SPX 0DTE test", size_pct=50),
        targets=targets or [Target(level=120.0, scale_out_pct=100)],
        stop=stop,
        one_shot=one_shot,
    )


def mk_plan(triggers: list[Trigger], below: float = 50.0, flat_by: str = "16:00") -> Plan:
    return Plan(
        date=DATE,
        generated_at=f"{DATE}T08:00:00-04:00",
        bias="bullish",
        spx_reference=100.0,
        triggers=triggers,
        invalidation=Invalidation(below=below, action="flatten_and_halt"),
        flat_by=flat_by,
    )


def events_of(events, cls):
    return [e for e in events if isinstance(e, cls)]


# -- zone entry debounce -------------------------------------------------


def test_fires_on_transition_into_zone_not_while_staying_inside():
    plan = mk_plan([mk_trigger("t1")])
    engine = TriggerEngine(plan)

    e1 = engine.evaluate(tick(95.0, ts(9, 30)))  # outside
    assert events_of(e1, TriggerFired) == []

    e2 = engine.evaluate(tick(105.0, ts(9, 31)))  # transition into zone
    fired = events_of(e2, TriggerFired)
    assert len(fired) == 1
    assert fired[0].trigger_id == "t1"

    e3 = engine.evaluate(tick(106.0, ts(9, 32)))  # still inside -> no refire
    assert events_of(e3, TriggerFired) == []


def test_fires_on_first_tick_ever_if_already_inside_zone():
    plan = mk_plan([mk_trigger("t1")])
    engine = TriggerEngine(plan)
    e1 = engine.evaluate(tick(105.0, ts(9, 30)))
    assert len(events_of(e1, TriggerFired)) == 1


# -- one-shot --------------------------------------------------------------


def test_one_shot_never_refires_after_resolution():
    plan = mk_plan([mk_trigger("t1")])
    engine = TriggerEngine(plan)

    engine.evaluate(tick(105.0, ts(9, 30)))
    assert engine.states["t1"].status == TriggerStatus.FIRED
    engine.skip("t1")
    assert engine.states["t1"].status == TriggerStatus.SKIPPED

    e = engine.evaluate(tick(95.0, ts(9, 31)))  # leave zone
    e2 = engine.evaluate(tick(105.0, ts(9, 32)))  # re-enter zone
    assert events_of(e, TriggerFired) == []
    assert events_of(e2, TriggerFired) == []
    assert engine.states["t1"].status == TriggerStatus.SKIPPED


# -- requires_not_filled gating --------------------------------------------


def test_requires_not_filled_blocks_while_referenced_trigger_active():
    a = mk_trigger("a", price_between=(100.0, 110.0))
    b = mk_trigger("b", price_between=(100.0, 110.0), requires_not_filled=["a"])
    engine = TriggerEngine(mk_plan([a, b]))

    events = engine.evaluate(tick(105.0, ts(9, 30)))
    fired_ids = {e.trigger_id for e in events_of(events, TriggerFired)}
    assert fired_ids == {"a"}
    assert engine.states["b"].status == TriggerStatus.ARMED


def test_requires_not_filled_unblocks_once_referenced_trigger_resolved():
    a = mk_trigger("a", price_between=(100.0, 110.0))
    b = mk_trigger("b", price_between=(100.0, 110.0), requires_not_filled=["a"])
    engine = TriggerEngine(mk_plan([a, b]))

    engine.evaluate(tick(105.0, ts(9, 30)))
    engine.skip("a")

    events = engine.evaluate(tick(106.0, ts(9, 31)))
    fired_ids = {e.trigger_id for e in events_of(events, TriggerFired)}
    assert fired_ids == {"b"}


# -- yield fail-closed -------------------------------------------------------


def test_yield_condition_fails_closed_without_yield_data():
    trig = mk_trigger("t1", price_between=None, price_above=100.0, yields_10y="falling")
    engine = TriggerEngine(mk_plan([trig]))

    events = engine.evaluate(tick(105.0, ts(9, 30), yield_10y=None))
    assert events_of(events, TriggerFired) == []
    assert len(events_of(events, EngineWarning)) == 1
    assert engine.states["t1"].status == TriggerStatus.ARMED


def test_yield_condition_fires_once_direction_confirmed():
    trig = mk_trigger("t1", price_between=None, price_above=100.0, yields_10y="falling")
    engine = TriggerEngine(mk_plan([trig]))

    # build a window where yield is falling: mean pulled up by earlier higher prints
    engine.evaluate(tick(95.0, ts(9, 25), yield_10y=4.50))
    engine.evaluate(tick(96.0, ts(9, 26), yield_10y=4.50))
    events = engine.evaluate(tick(105.0, ts(9, 27), yield_10y=4.30))
    assert len(events_of(events, TriggerFired)) == 1


# -- gap-through stop --------------------------------------------------------


def test_gap_through_stop_hit_emitted_even_without_printing_at_stop():
    trig = mk_trigger("t1", stop=90.0)
    engine = TriggerEngine(mk_plan([trig]))

    engine.evaluate(tick(105.0, ts(9, 30)))  # fires
    engine.confirm("t1")
    engine.mark_filled("t1")
    assert engine.states["t1"].status == TriggerStatus.OPEN

    engine.evaluate(tick(95.0, ts(9, 31)))  # still above stop
    events = engine.evaluate(tick(85.0, ts(9, 32)))  # gapped through 90
    hits = events_of(events, StopHit)
    assert len(hits) == 1
    assert hits[0].trigger_id == "t1"
    assert engine.states["t1"].status == TriggerStatus.CLOSED


# -- invalidation halts new fires -------------------------------------------


def test_invalidation_blocks_subsequent_trigger_fires():
    a = mk_trigger("a", price_between=(100.0, 110.0))
    b = mk_trigger("b", price_between=(50.0, 60.0), stop=40.0)
    engine = TriggerEngine(mk_plan([a, b], below=80.0))

    e1 = engine.evaluate(tick(105.0, ts(9, 30)))
    assert len(events_of(e1, TriggerFired)) == 1  # a fires

    e2 = engine.evaluate(tick(75.0, ts(9, 31)))  # crosses invalidation
    assert len(events_of(e2, Invalidated)) == 1
    assert engine.invalidated is True

    e3 = engine.evaluate(tick(55.0, ts(9, 32)))  # would enter b's zone
    assert events_of(e3, TriggerFired) == []


def test_load_plan_clears_invalidation_and_reports_it():
    a = mk_trigger("a", price_between=(100.0, 110.0))
    engine = TriggerEngine(mk_plan([a], below=80.0))
    engine.evaluate(tick(75.0, ts(9, 31)))  # crosses invalidation
    assert engine.invalidated is True

    b = mk_trigger("b", price_between=(50.0, 60.0), stop=40.0)
    cleared = engine.load_plan(mk_plan([b], below=30.0))
    assert cleared is True, "load_plan must report that it superseded an invalidation"
    assert engine.invalidated is False

    e = engine.evaluate(tick(55.0, ts(9, 32)))  # enters b's zone
    assert len(events_of(e, TriggerFired)) == 1, "new plan's triggers must be able to fire post-clear"


def test_load_plan_reports_false_when_nothing_was_invalidated():
    a = mk_trigger("a", price_between=(100.0, 110.0))
    engine = TriggerEngine(mk_plan([a], below=80.0))

    cleared = engine.load_plan(mk_plan([a], below=80.0))
    assert cleared is False


# -- hot-swap ----------------------------------------------------------------


def test_hot_swap_preserves_open_and_emits_review_required_on_removal():
    a = mk_trigger("a", price_between=(100.0, 110.0), stop=90.0)
    c = mk_trigger("c", price_between=(200.0, 210.0))
    engine = TriggerEngine(mk_plan([a, c]))

    engine.evaluate(tick(105.0, ts(9, 30)))
    engine.confirm("a")
    engine.mark_filled("a")
    assert engine.states["a"].status == TriggerStatus.OPEN

    b_new = mk_trigger("b", price_between=(300.0, 310.0))
    new_plan = mk_plan([b_new, c])  # "a" removed while OPEN
    engine.load_plan(new_plan)

    assert "a" in engine.states
    assert engine.states["a"].status == TriggerStatus.OPEN

    events = engine.evaluate(tick(305.0, ts(9, 31)))
    review_events = events_of(events, ReviewRequired)
    assert len(review_events) == 1
    assert review_events[0].trigger_id == "a"


def test_hot_swap_updates_levels_for_still_armed_trigger():
    c = mk_trigger("c", price_between=(200.0, 210.0))
    engine = TriggerEngine(mk_plan([c]))

    c_new = mk_trigger("c", price_between=(300.0, 310.0))
    engine.load_plan(mk_plan([c_new]))

    assert engine.states["c"].trigger.condition.price_between == (300.0, 310.0)
    assert engine.states["c"].status == TriggerStatus.ARMED


# -- not_after expiry ---------------------------------------------------------


def test_trigger_expires_after_not_after_and_never_fires():
    trig = mk_trigger("t1", not_after="10:00")
    engine = TriggerEngine(mk_plan([trig]))

    events = engine.evaluate(tick(105.0, ts(10, 5)))
    assert events_of(events, TriggerFired) == []
    assert engine.states["t1"].status == TriggerStatus.EXPIRED


# -- WAIT cooldown re-arm -------------------------------------------------------


def test_wait_rearms_with_cooldown_then_refires():
    trig = mk_trigger("t1")
    engine = TriggerEngine(mk_plan([trig]))

    t0 = ts(9, 30)
    e1 = engine.evaluate(tick(105.0, t0))
    assert len(events_of(e1, TriggerFired)) == 1

    engine.wait("t1", tick(105.0, t0), cooldown_sec=300)
    assert engine.states["t1"].status == TriggerStatus.ARMED

    e2 = engine.evaluate(tick(106.0, t0 + timedelta(seconds=60)))
    assert events_of(e2, TriggerFired) == []  # still in cooldown

    e3 = engine.evaluate(tick(107.0, t0 + timedelta(seconds=301)))
    assert len(events_of(e3, TriggerFired)) == 1  # cooldown elapsed, refires


# -- target hits + flat_by ------------------------------------------------------


def test_gap_through_multiple_targets_close_trigger():
    targets = [Target(level=120.0, scale_out_pct=50), Target(level=130.0, scale_out_pct=50)]
    trig = mk_trigger("t1", targets=targets)
    engine = TriggerEngine(mk_plan([trig]))

    engine.evaluate(tick(105.0, ts(9, 30)))
    engine.confirm("t1")
    engine.mark_filled("t1")

    events = engine.evaluate(tick(135.0, ts(9, 31)))  # gaps through both targets
    hits = events_of(events, TargetHit)
    assert {h.level for h in hits} == {120.0, 130.0}
    assert engine.states["t1"].status == TriggerStatus.CLOSED


def test_flat_by_reached_emitted_once_for_open_position():
    trig = mk_trigger("t1")
    engine = TriggerEngine(mk_plan([trig], flat_by="10:00"))

    engine.evaluate(tick(105.0, ts(9, 30)))
    engine.confirm("t1")
    engine.mark_filled("t1")

    e1 = engine.evaluate(tick(106.0, ts(10, 0)))
    assert len(events_of(e1, FlatByReached)) == 1

    e2 = engine.evaluate(tick(106.0, ts(10, 1)))
    assert events_of(e2, FlatByReached) == []
