from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from risk import RiskManager
from schema import Action, Condition, Invalidation, Plan, Target, Trigger
from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")


def ts(hh: int, mm: int) -> datetime:
    return datetime(2026, 7, 21, hh, mm, tzinfo=ET)


def mk_trigger(id: str) -> Trigger:
    return Trigger(
        id=id,
        side="long",
        condition=Condition(price_between=(100.0, 110.0)),
        action=Action(instrument="test", size_pct=50),
        targets=[Target(level=120.0, scale_out_pct=100)],
        stop=90.0,
    )


def mk_plan(triggers: list[Trigger]) -> Plan:
    return Plan(
        date="2026-07-21",
        generated_at="2026-07-21T08:00:00-04:00",
        bias="bullish",
        spx_reference=100.0,
        triggers=triggers,
        invalidation=Invalidation(below=50.0, action="flatten_and_halt"),
        flat_by="16:00",
    )


@pytest.fixture
def risk_mgr(tmp_path):
    return RiskManager(
        halt_path=tmp_path / "HALT",
        daily_loss_limit_usd=1000.0,
        max_open_triggers=1,
        entry_cutoff_et="15:00",
    )


def test_not_halted_by_default(risk_mgr):
    assert risk_mgr.is_halted() is False


def test_create_halt_writes_file_with_reason(risk_mgr, tmp_path):
    risk_mgr.create_halt("test reason")
    assert risk_mgr.is_halted() is True
    assert "test reason" in (tmp_path / "HALT").read_text()


def test_create_halt_is_idempotent(risk_mgr, tmp_path):
    risk_mgr.create_halt("first")
    risk_mgr.create_halt("second")
    # first reason preserved, not overwritten
    assert "first" in (tmp_path / "HALT").read_text()
    assert "second" not in (tmp_path / "HALT").read_text()


def test_clear_halt_removes_file(risk_mgr):
    risk_mgr.create_halt("reason")
    risk_mgr.clear_halt()
    assert risk_mgr.is_halted() is False


def test_daily_loss_limit_creates_halt(risk_mgr):
    risk_mgr.record_realized_pnl(-500.0, now=ts(10, 0))
    assert risk_mgr.is_halted() is False
    risk_mgr.record_realized_pnl(-600.0, now=ts(10, 5))
    assert risk_mgr.is_halted() is True


def test_daily_pnl_resets_on_new_day(risk_mgr):
    risk_mgr.record_realized_pnl(-200.0, now=datetime(2026, 7, 21, 10, 0, tzinfo=ET))
    assert risk_mgr.daily_pnl(datetime(2026, 7, 21, 11, 0, tzinfo=ET)) == -200.0
    # next day: rolls over to 0 before the new amount is added
    assert risk_mgr.daily_pnl(datetime(2026, 7, 22, 9, 0, tzinfo=ET)) == 0.0


def test_entry_cutoff_reached(risk_mgr):
    assert risk_mgr.entry_cutoff_reached(ts(14, 59)) is False
    assert risk_mgr.entry_cutoff_reached(ts(15, 0)) is True
    assert risk_mgr.entry_cutoff_reached(ts(15, 1)) is True


def test_max_open_triggers_reached(risk_mgr):
    plan = mk_plan([mk_trigger("a"), mk_trigger("b")])
    engine = TriggerEngine(plan)
    assert risk_mgr.max_open_triggers_reached(engine) is False

    engine.states["a"].status = TriggerStatus.OPEN
    assert risk_mgr.max_open_triggers_reached(engine) is True  # limit is 1


def test_gate_execute_blocks_on_halt(risk_mgr):
    plan = mk_plan([mk_trigger("a")])
    engine = TriggerEngine(plan)
    risk_mgr.create_halt("manual halt")
    result = risk_mgr.gate_execute(engine, ts(10, 0))
    assert result.allowed is False
    assert "HALT" in result.reason


def test_gate_execute_blocks_past_cutoff(risk_mgr):
    plan = mk_plan([mk_trigger("a")])
    engine = TriggerEngine(plan)
    result = risk_mgr.gate_execute(engine, ts(15, 1))
    assert result.allowed is False
    assert "cutoff" in result.reason


def test_gate_execute_blocks_max_open(risk_mgr):
    plan = mk_plan([mk_trigger("a"), mk_trigger("b")])
    engine = TriggerEngine(plan)
    engine.states["a"].status = TriggerStatus.OPEN
    result = risk_mgr.gate_execute(engine, ts(10, 0))
    assert result.allowed is False
    assert "max open" in result.reason


def test_gate_execute_allows_when_clear(risk_mgr):
    plan = mk_plan([mk_trigger("a")])
    engine = TriggerEngine(plan)
    result = risk_mgr.gate_execute(engine, ts(10, 0))
    assert result.allowed is True
    assert result.reason is None
