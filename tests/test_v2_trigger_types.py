"""v2 trigger types: bar-close and dwell confirmation, direction of
approach, first touch, breakeven stop after a target, and stops judged on
bar closes."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from schema import (
    Action,
    AlreadyInZone,
    Condition,
    Confirm,
    EngineWarning,
    Invalidation,
    Plan,
    StopHit,
    StopMoved,
    Target,
    Tick,
    Trigger,
    TriggerFired,
)
from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")


def at(hh: int, mm: int, ss: int = 0) -> _dt.datetime:
    return _dt.datetime(2026, 11, 24, hh, mm, ss, tzinfo=ET)


def mk(id: str, side: str = "long", stop: float = 7172.0, targets=None, stop_confirm=None, **cond) -> Trigger:
    return Trigger(
        id=id,
        side=side,
        condition=Condition(**cond),
        action=Action(instrument="SPX 0DTE", size_pct=10),
        targets=targets or [Target(level=7250.0 if side == "long" else 7100.0, scale_out_pct=100)],
        stop=stop,
        stop_confirm=stop_confirm,
    )


def engine_for(*triggers: Trigger) -> TriggerEngine:
    return TriggerEngine(
        Plan(
            date="2026-11-24",
            generated_at="2026-11-24T09:00:00-05:00",
            bias="neutral",
            spx_reference=7250.0,
            triggers=list(triggers),
            invalidation=Invalidation(below=7000.0, action="stand aside"),
            flat_by="15:30",
        )
    )


def run(engine: TriggerEngine, *ticks: tuple[_dt.datetime, float]) -> list:
    events = []
    for when, price in ticks:
        events += engine.evaluate(Tick(ts=when, price=price))
    return events


def fired(events) -> list[tuple[str, float]]:
    return [(e.trigger_id, e.tick.price) for e in events if isinstance(e, TriggerFired)]


CLOSE_5M = {"type": "close", "timeframe": "5m"}


# -- schema ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price_above": 7260.0, "confirm": {"type": "close"}},  # close needs timeframe
        {"price_above": 7260.0, "confirm": {"type": "dwell"}},  # dwell needs seconds
        {"price_above": 7260.0, "confirm": {"type": "tick", "seconds": 5}},
        {"price_above": 7260.0, "approach": "from_above"},  # approach needs a zone
        {"first_touch": True},  # needs a price condition
    ],
)
def test_condition_rejects_bad_combinations(kwargs):
    with pytest.raises(ValueError):
        Condition(**kwargs)


def test_stop_confirm_cannot_be_dwell():
    with pytest.raises(ValueError):
        mk("x", price_above=7260.0, stop=7238.0, stop_confirm=Confirm(type="dwell", seconds=60))


# -- confirm on a bar close ------------------------------------------------------


def test_five_minute_close_above_ignores_intrabar_wick():
    e = engine_for(mk("breakout", price_above=7260.0, stop=7238.0, confirm=CLOSE_5M))
    events = run(
        e,
        (at(10, 0), 7250.0),
        (at(10, 2), 7265.0),  # tick above, but the bar hasn't closed
        (at(10, 4), 7258.0),  # back below by the close
        (at(10, 5, 10), 7262.0),  # 10:00 bar closed at 7258: no
        (at(10, 9), 7266.0),
    )
    assert fired(events) == []
    events = run(e, (at(10, 10, 5), 7270.0))  # 10:05 bar closed at 7266: yes
    assert fired(events) == [("breakout", 7270.0)]


def test_close_mode_first_sight_above_level_waits_for_fresh_break():
    e = engine_for(mk("breakout", price_above=7260.0, stop=7238.0, confirm=CLOSE_5M))
    events = run(e, (at(10, 1), 7270.0), (at(10, 6), 7272.0), (at(10, 11), 7275.0))
    assert fired(events) == []
    assert [x.trigger_id for x in events if isinstance(x, AlreadyInZone)] == ["breakout"]
    # closes below, then above again -> fires
    events = run(e, (at(10, 14), 7255.0), (at(10, 16), 7258.0), (at(10, 19), 7265.0), (at(10, 21), 7266.0))
    assert fired(events) == [("breakout", 7266.0)]


def test_close_mode_break_while_time_gated_is_not_fired_late():
    """The 5m close above happened before not_before; when the gate opens the
    breakout is old news, so it waits for a fresh close-through."""
    e = engine_for(mk("breakout", price_above=7260.0, stop=7238.0, confirm=CLOSE_5M, not_before="10:00"))
    events = run(e, (at(9, 45), 7250.0), (at(9, 52), 7263.0), (at(9, 56), 7264.0), (at(10, 1), 7265.0))
    assert fired(events) == []


# -- dwell ------------------------------------------------------------------------


def test_hold_below_for_five_minutes():
    e = engine_for(mk("breakdown", side="short", price_below=7170.0, stop=7190.0,
                      confirm={"type": "dwell", "seconds": 300}))
    events = run(e, (at(10, 0), 7175.0), (at(10, 1), 7168.0), (at(10, 4), 7165.0))
    assert fired(events) == []
    assert fired(run(e, (at(10, 6, 5), 7166.0))) == [("breakdown", 7166.0)]


def test_dwell_clock_resets_when_price_leaves():
    e = engine_for(mk("breakdown", side="short", price_below=7170.0, stop=7190.0,
                      confirm={"type": "dwell", "seconds": 300}))
    events = run(
        e,
        (at(10, 0), 7175.0),
        (at(10, 1), 7168.0),  # clock starts
        (at(10, 3), 7172.0),  # back above: reset
        (at(10, 4), 7168.0),  # clock restarts
        (at(10, 8), 7166.0),  # only 240 s
    )
    assert fired(events) == []
    assert fired(run(e, (at(10, 9, 1), 7165.0))) == [("breakdown", 7165.0)]


# -- direction of approach -----------------------------------------------------------


def test_buy_dip_needs_approach_from_above():
    e = engine_for(mk("buy_dip", price_between=(7195.0, 7215.0), approach="from_above"))
    assert fired(run(e, (at(10, 0), 7180.0), (at(10, 1), 7205.0))) == []  # rally up into it
    assert fired(run(e, (at(10, 2), 7225.0), (at(10, 3), 7210.0))) == [("buy_dip", 7210.0)]


def test_sell_rip_needs_approach_from_below():
    e = engine_for(mk("sell_rip", side="short", price_between=(7280.0, 7290.0), stop=7302.0,
                      approach="from_below"))
    assert fired(run(e, (at(10, 0), 7300.0), (at(10, 1), 7285.0))) == []  # came down into it
    assert fired(run(e, (at(10, 2), 7270.0), (at(10, 3), 7284.0))) == [("sell_rip", 7284.0)]


# -- first touch ---------------------------------------------------------------------


def test_first_touch_while_gated_uses_it_up():
    e = engine_for(mk("fade", side="short", price_above=7288.0, stop=7302.0, first_touch=True,
                      not_before="10:00"))
    events = run(
        e,
        (at(9, 40), 7280.0),
        (at(9, 45), 7290.0),  # first touch, during the wait
        (at(9, 50), 7280.0),
        (at(10, 15), 7289.0),  # second touch
    )
    assert fired(events) == []


def test_without_first_touch_the_second_touch_fires():
    e = engine_for(mk("fade", side="short", price_above=7288.0, stop=7302.0, not_before="10:00"))
    events = run(e, (at(9, 40), 7280.0), (at(9, 45), 7290.0), (at(9, 50), 7280.0), (at(10, 15), 7289.0))
    assert fired(events) == [("fade", 7289.0)]


def test_first_touch_fires_on_the_first_touch():
    e = engine_for(mk("fade", side="short", price_above=7288.0, stop=7302.0, first_touch=True))
    assert fired(run(e, (at(10, 0), 7280.0), (at(10, 5), 7290.0))) == [("fade", 7290.0)]


def test_already_above_at_startup_counts_as_the_first_touch():
    e = engine_for(mk("fade", side="short", price_above=7288.0, stop=7302.0, first_touch=True))
    events = run(e, (at(10, 0), 7290.0), (at(10, 5), 7280.0), (at(10, 10), 7291.0))
    assert fired(events) == []


# -- breakeven after a target ------------------------------------------------------------


def _open_long(e: TriggerEngine, fill: float | None = None) -> None:
    run(e, (at(10, 0), 7225.0), (at(10, 1), 7205.0))
    e.confirm("buy_dip")
    e.mark_filled("buy_dip", fill_price=fill)
    assert e.states["buy_dip"].status == TriggerStatus.OPEN


BE_TARGETS = [Target(level=7250.0, scale_out_pct=50, then_stop="breakeven"), Target(level=7288.0, scale_out_pct=50)]


def test_tp1_moves_stop_to_fire_price_then_stops_out_at_breakeven():
    e = engine_for(mk("buy_dip", price_between=(7195.0, 7215.0), targets=BE_TARGETS))
    _open_long(e)
    events = run(e, (at(10, 20), 7251.0))
    moved = [x for x in events if isinstance(x, StopMoved)]
    assert [(m.old_stop, m.new_stop) for m in moved] == [(7172.0, 7205.0)]
    events = run(e, (at(10, 30), 7230.0), (at(10, 40), 7204.0))
    assert [x.trigger_id for x in events if isinstance(x, StopHit)] == ["buy_dip"]


def test_breakeven_uses_operator_fill_price():
    e = engine_for(mk("buy_dip", price_between=(7195.0, 7215.0), targets=BE_TARGETS))
    _open_long(e, fill=7209.5)
    events = run(e, (at(10, 20), 7251.0))
    assert [m.new_stop for m in events if isinstance(m, StopMoved)] == [7209.5]


def test_short_breakeven():
    targets = [Target(level=7150.0, scale_out_pct=50, then_stop="breakeven"), Target(level=7100.0, scale_out_pct=50)]
    e = engine_for(mk("bd", side="short", price_below=7170.0, stop=7190.0, targets=targets))
    run(e, (at(10, 0), 7175.0), (at(10, 1), 7168.0))
    e.confirm("bd")
    e.mark_filled("bd")
    events = run(e, (at(10, 10), 7149.0))
    assert [m.new_stop for m in events if isinstance(m, StopMoved)] == [7168.0]
    assert [x.trigger_id for x in run(e, (at(10, 20), 7169.0)) if isinstance(x, StopHit)] == ["bd"]


def test_breakeven_without_entry_price_warns_and_keeps_stop():
    e = engine_for(mk("buy_dip", price_between=(7195.0, 7215.0), targets=BE_TARGETS))
    _open_long(e)
    e.states["buy_dip"].entry_price = None
    events = run(e, (at(10, 20), 7251.0))
    assert not any(isinstance(x, StopMoved) for x in events)
    assert any(isinstance(x, EngineWarning) and "breakeven" in x.message for x in events)
    assert e.states["buy_dip"].stop == 7172.0


def test_breakeven_stop_survives_restart():
    p_trig = mk("buy_dip", price_between=(7195.0, 7215.0), targets=BE_TARGETS)
    e = engine_for(p_trig)
    _open_long(e)
    run(e, (at(10, 20), 7251.0))
    restarted = engine_for(p_trig)
    restarted.restore_snapshot(e.to_snapshot())
    assert restarted.states["buy_dip"].stop == 7205.0
    assert [x.trigger_id for x in run(restarted, (at(10, 30), 7240.0), (at(10, 31), 7204.0))
            if isinstance(x, StopHit)] == ["buy_dip"]


# -- stop judged on bar closes -----------------------------------------------------------


def test_stop_on_one_minute_close_ignores_wick():
    e = engine_for(mk("buy_dip", price_between=(7195.0, 7215.0),
                      stop_confirm=Confirm(type="close", timeframe="1m")))
    _open_long(e)
    events = run(
        e,
        (at(10, 20, 10), 7180.0),
        (at(10, 20, 30), 7170.0),  # wick through 7172
        (at(10, 20, 50), 7175.0),
        (at(10, 21, 5), 7176.0),  # 10:20 bar closed at 7175: no stop
    )
    assert not any(isinstance(x, StopHit) for x in events)
    events = run(e, (at(10, 21, 40), 7171.0), (at(10, 22, 1), 7170.0))  # 10:21 closed at 7171
    assert [x.trigger_id for x in events if isinstance(x, StopHit)] == ["buy_dip"]


def test_default_tick_stop_still_hits_on_the_wick():
    e = engine_for(mk("buy_dip", price_between=(7195.0, 7215.0)))
    _open_long(e)
    events = run(e, (at(10, 20, 10), 7180.0), (at(10, 20, 30), 7170.0))
    assert [x.trigger_id for x in events if isinstance(x, StopHit)] == ["buy_dip"]
