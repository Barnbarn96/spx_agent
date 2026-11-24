"""v2 engine fixes: a fast move through a whole zone, no fire on first sight
of a zone, decide() off the tick loop, and engine state that survives a
restart of main.py."""

from __future__ import annotations

import asyncio
import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

import agent
import config
import main as main_mod
from schema import (
    Action,
    AlreadyInZone,
    Condition,
    Decision,
    Invalidation,
    Plan,
    StopHit,
    Target,
    Tick,
    Trigger,
    TriggerFired,
    ZoneSkipped,
)
from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")


def at(hh: int, mm: int, ss: int = 0) -> _dt.datetime:
    return _dt.datetime(2026, 11, 24, hh, mm, ss, tzinfo=ET)


def t(price: float, when: _dt.datetime) -> Tick:
    return Tick(ts=when, price=price)


def trig(id: str, side: str = "long", stop: float = 7172.0, target: float = 7250.0, **cond) -> Trigger:
    cond.setdefault("price_between", (7195.0, 7215.0))
    return Trigger(
        id=id,
        side=side,
        condition=Condition(**cond),
        action=Action(instrument="SPX 0DTE", size_pct=15),
        targets=[Target(level=target, scale_out_pct=100)],
        stop=stop,
    )


def plan(*triggers: Trigger) -> Plan:
    return Plan(
        date="2026-11-24",
        generated_at="2026-11-24T09:00:00-05:00",
        bias="bullish",
        spx_reference=7250.0,
        triggers=list(triggers),
        invalidation=Invalidation(below=7000.0, action="stand aside"),
        flat_by="15:30",
    )


def fired(events) -> list[str]:
    return [e.trigger_id for e in events if isinstance(e, TriggerFired)]


# -- fast move through a whole zone -------------------------------------------


def skipped(events) -> list[str]:
    return [e.trigger_id for e in events if isinstance(e, ZoneSkipped)]


def test_drop_through_whole_zone_alerts_without_firing():
    engine = TriggerEngine(plan(trig("buy_dip")))
    engine.evaluate(t(7220.0, at(10, 0)))
    events = engine.evaluate(t(7190.0, at(10, 1)))  # never printed 7195-7215
    assert fired(events) == []
    assert skipped(events) == ["buy_dip"]
    assert events[0].from_price == 7220.0


def test_spike_through_dip_zone_then_pullback_fires_inside_zone():
    """Regression, 2026-07-01 ISM fixture: a spike 7464 -> 7480 through the
    7465-7472 buy-the-dip zone must not fire at 7480 (chasing); the pullback
    to 7468 is the planned entry."""
    engine = TriggerEngine(plan(trig("buy_dip", price_between=(7465.0, 7472.0), stop=7450.0, target=7498.0)))
    engine.evaluate(t(7464.0, at(9, 59)))
    spike = engine.evaluate(t(7480.0, at(10, 0)))
    assert fired(spike) == [] and skipped(spike) == ["buy_dip"]
    assert fired(engine.evaluate(t(7474.0, at(10, 1)))) == []
    pullback = engine.evaluate(t(7468.0, at(10, 2)))
    assert fired(pullback) == ["buy_dip"]
    assert pullback[0].tick.price == 7468.0


def test_partial_move_toward_zone_is_not_a_skip():
    engine = TriggerEngine(plan(trig("buy_dip")))
    engine.evaluate(t(7230.0, at(10, 0)))
    assert engine.evaluate(t(7220.0, at(10, 1))) == []


def test_jump_through_while_time_gated_is_silent():
    engine = TriggerEngine(plan(trig("buy_dip", not_before="10:00")))
    engine.evaluate(t(7220.0, at(9, 40)))
    assert engine.evaluate(t(7190.0, at(9, 41))) == []


# -- no fire on first sight -----------------------------------------------------


def test_first_sight_inside_zone_reports_and_waits_for_fresh_entry():
    engine = TriggerEngine(plan(trig("buy_dip")))
    events = engine.evaluate(t(7205.0, at(10, 30)))
    assert fired(events) == []
    assert [e.trigger_id for e in events if isinstance(e, AlreadyInZone)] == ["buy_dip"]
    # reported once, not on every tick
    assert not any(isinstance(e, AlreadyInZone) for e in engine.evaluate(t(7204.0, at(10, 31))))
    engine.evaluate(t(7220.0, at(10, 32)))
    assert fired(engine.evaluate(t(7210.0, at(10, 33)))) == ["buy_dip"]


def test_first_sight_above_breakout_level_does_not_fire():
    engine = TriggerEngine(plan(trig("breakout", price_between=None, price_above=7260.0, stop=7238.0, target=7288.0)))
    assert fired(engine.evaluate(t(7270.0, at(10, 30)))) == []
    engine.evaluate(t(7255.0, at(10, 31)))
    assert fired(engine.evaluate(t(7262.0, at(10, 32)))) == ["breakout"]


def test_gate_opening_with_price_in_zone_still_fires():
    """Started before not_before with price already in the zone: the gate
    opening is a real 'conditions now met', so it fires then."""
    engine = TriggerEngine(plan(trig("buy_dip", not_before="10:00")))
    assert fired(engine.evaluate(t(7205.0, at(9, 50)))) == []
    assert fired(engine.evaluate(t(7206.0, at(10, 0)))) == ["buy_dip"]


def test_replan_adding_trigger_with_price_in_its_zone_does_not_fire():
    engine = TriggerEngine(plan(trig("buy_dip")))
    engine.evaluate(t(7240.0, at(10, 0)))
    engine.load_plan(plan(trig("buy_dip"), trig("buy_higher", price_between=(7235.0, 7245.0), stop=7220.0)))
    events = engine.evaluate(t(7241.0, at(10, 1)))
    assert fired(events) == []
    assert [e.trigger_id for e in events if isinstance(e, AlreadyInZone)] == ["buy_higher"]


# -- snapshot / restore ---------------------------------------------------------


def _open(engine: TriggerEngine, tid: str, outside: float, inside: float) -> None:
    engine.evaluate(t(outside, at(10, 0)))
    engine.evaluate(t(inside, at(10, 1)))
    engine.confirm(tid)
    engine.mark_filled(tid)
    assert engine.states[tid].status == TriggerStatus.OPEN


def test_snapshot_restores_open_trade_and_its_stop():
    p = plan(trig("buy_dip"), trig("fade", side="short", price_between=(7285.0, 7290.0), stop=7302.0, target=7250.0))
    engine = TriggerEngine(p)
    _open(engine, "buy_dip", 7220.0, 7205.0)

    restarted = TriggerEngine(p)
    assert restarted.restore_snapshot(engine.to_snapshot())
    assert restarted.states["buy_dip"].status == TriggerStatus.OPEN
    events = restarted.evaluate(t(7170.0, at(10, 30)))
    assert [e.trigger_id for e in events if isinstance(e, StopHit)] == ["buy_dip"]


def test_snapshot_keeps_debounce_so_restart_inside_zone_does_not_refire():
    p = plan(trig("buy_dip"), trig("other", price_between=(7100.0, 7110.0), stop=7090.0, target=7150.0))
    engine = TriggerEngine(p)
    engine.evaluate(t(7220.0, at(10, 0)))
    engine.evaluate(t(7205.0, at(10, 1)))
    engine.skip("buy_dip")

    restarted = TriggerEngine(p)
    restarted.restore_snapshot(engine.to_snapshot())
    assert restarted.states["buy_dip"].status == TriggerStatus.SKIPPED
    assert fired(restarted.evaluate(t(7206.0, at(10, 5)))) == []


def test_snapshot_keeps_open_trade_missing_from_plan():
    engine = TriggerEngine(plan(trig("buy_dip")))
    _open(engine, "buy_dip", 7220.0, 7205.0)
    other = plan(trig("fade", side="short", price_between=(7285.0, 7290.0), stop=7302.0, target=7250.0))
    restarted = TriggerEngine(other)
    restarted.restore_snapshot(engine.to_snapshot())
    assert restarted.states["buy_dip"].status == TriggerStatus.OPEN


def test_snapshot_from_another_day_is_ignored():
    engine = TriggerEngine(plan(trig("buy_dip")))
    snap = engine.to_snapshot()
    snap["plan_date"] = "2026-11-23"
    assert TriggerEngine(plan(trig("buy_dip"))).restore_snapshot(snap) == []


# -- main.py: decide() in the background, restart ------------------------------


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.engine = None

    async def send_text(self, text, view=None):
        self.sent.append(("text", text))

    async def send_trigger_fired(self, trigger, tick, decision, decision_id):
        self.sent.append(("fired", trigger.id))

    async def send_stop_hit(self, event):
        self.sent.append(("stop", event.trigger_id))

    async def send_halt(self, reason):
        self.sent.append(("text", f"**HALT** — {reason}"))

    async def send_wait_rearmed(self, trigger, tick, decision):
        self.sent.append(("wait", trigger.id))


@pytest.fixture
async def orch_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    made = []

    async def make():
        o = main_mod.Orchestrator(sim=False)
        o.journal = main_mod.JournalDB(tmp_path / f"journal_{len(made)}.db")
        await o.journal.connect()
        o.notifier = FakeNotifier()
        made.append(o)
        return o

    yield make
    for o in made:
        for task in list(o._decide_tasks):
            task.cancel()
        await o.journal.close()


async def test_tick_processed_while_decide_pending(orch_factory, monkeypatch):
    release = asyncio.Event()

    async def slow_decide(trigger, plan_, ctx):
        await release.wait()
        return Decision(action="EXECUTE", reasoning="ok", confidence=70)

    monkeypatch.setattr(agent, "decide", slow_decide)
    o = await orch_factory()
    p = plan(
        trig("buy_dip"),
        trig("other_long", price_between=(7100.0, 7110.0), stop=7090.0, target=7300.0),
    )
    o._load_plan(p, "test")
    o.engine.evaluate(t(7120.0, at(9, 59)))
    o.engine.evaluate(t(7105.0, at(10, 0)))
    o.engine.confirm("other_long")
    o.engine.mark_filled("other_long")

    await o.process_tick(t(7220.0, at(10, 1)))
    await o.process_tick(t(7205.0, at(10, 2)))  # buy_dip fires; decide blocks
    assert o._decide_tasks
    await asyncio.wait_for(o.process_tick(t(7085.0, at(10, 3))), timeout=1)  # other_long stop
    assert ("stop", "other_long") in o.notifier.sent
    assert ("fired", "buy_dip") not in o.notifier.sent  # decide still pending

    release.set()
    await o.wait_for_decisions()
    assert ("fired", "buy_dip") in o.notifier.sent


async def test_no_alert_if_trigger_dropped_while_deciding(orch_factory, monkeypatch):
    release = asyncio.Event()

    async def slow_decide(trigger, plan_, ctx):
        await release.wait()
        return Decision(action="EXECUTE", reasoning="ok", confidence=70)

    monkeypatch.setattr(agent, "decide", slow_decide)
    o = await orch_factory()
    o._load_plan(plan(trig("buy_dip"), trig("keep", price_between=(7300.0, 7310.0), stop=7290.0, target=7350.0)), "test")
    await o.process_tick(t(7220.0, at(10, 0)))
    await o.process_tick(t(7205.0, at(10, 1)))
    o._load_plan(plan(trig("keep", price_between=(7300.0, 7310.0), stop=7290.0, target=7350.0)), "replan")
    release.set()
    await o.wait_for_decisions()
    assert ("fired", "buy_dip") not in o.notifier.sent


async def test_restart_restores_open_trade_and_rearms_unconfirmed_fire(orch_factory, monkeypatch):
    async def never_called(*a):
        raise AssertionError("decide() should not run here")

    p = plan(
        trig("buy_dip"),
        trig("other_long", price_between=(7100.0, 7110.0), stop=7090.0, target=7300.0),
    )
    first = await orch_factory()
    first._load_plan(p, "test")
    first.engine.evaluate(t(7120.0, at(9, 59)))
    first.engine.evaluate(t(7105.0, at(10, 0)))
    first.engine.confirm("other_long")
    first.engine.mark_filled("other_long")
    first.engine.evaluate(t(7220.0, at(10, 1)))
    first.engine.evaluate(t(7205.0, at(10, 2)))  # buy_dip FIRED, never confirmed
    first.save_engine_state()

    monkeypatch.setattr(agent, "decide", never_called)
    second = await orch_factory()
    second._load_plan(p, "test")
    assert second.engine.states["other_long"].status == TriggerStatus.OPEN
    assert second.engine.states["buy_dip"].status == TriggerStatus.ARMED
    assert any("Re-armed buy_dip" in n for n in second._pending_notices)

    await second.process_tick(t(7206.0, at(10, 5)))  # still in zone: no refire
    assert second.engine.states["buy_dip"].status == TriggerStatus.ARMED
    await second.process_tick(t(7085.0, at(10, 6)))
    assert ("stop", "other_long") in second.notifier.sent
