"""v2 task 1a-1: the second stop of the ET day halts new entries, and the
count survives a restart of main.py (rebuilt from the journal)."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

import config
import main as main_mod
from risk import RiskManager
from schema import StopHit, Tick
from triggers import TriggerEngine

from test_v2_engine import FakeNotifier, plan, trig

ET = ZoneInfo("America/New_York")


def at(hh: int, mm: int, day: int = 24) -> _dt.datetime:
    return _dt.datetime(2026, 11, day, hh, mm, tzinfo=ET)


def stop(trigger_id: str, when: _dt.datetime, price: float = 7170.0) -> StopHit:
    return StopHit(trigger_id=trigger_id, tick=Tick(ts=when, price=price))


# -- RiskManager ----------------------------------------------------------------


@pytest.fixture
def risk_mgr(tmp_path):
    return RiskManager(
        halt_path=tmp_path / "HALT",
        daily_loss_limit_usd=1000.0,
        max_open_triggers=1,
        entry_cutoff_et="15:00",
        max_stops_per_day=2,
    )


def test_one_stop_does_not_halt(risk_mgr):
    assert risk_mgr.record_stop(at(10, 15)) is False
    assert risk_mgr.stops_today(at(10, 16)) == 1
    assert risk_mgr.is_halted() is False


def test_second_stop_halts_and_blocks_execute(risk_mgr):
    engine = TriggerEngine(plan(trig("a")))
    risk_mgr.record_stop(at(10, 15))
    assert risk_mgr.gate_execute(engine, at(10, 20)).allowed is True

    assert risk_mgr.record_stop(at(11, 5)) is True
    assert risk_mgr.is_halted() is True
    result = risk_mgr.gate_execute(engine, at(11, 10))
    assert result.allowed is False
    assert "HALT" in result.reason
    assert "two stops" in risk_mgr.halt_path.read_text()


def test_stop_count_resets_on_a_new_et_day(risk_mgr):
    risk_mgr.record_stop(at(14, 0, day=23))
    assert risk_mgr.record_stop(at(10, 0, day=24)) is False
    assert risk_mgr.stops_today(at(10, 1, day=24)) == 1
    assert risk_mgr.is_halted() is False


def test_seeded_count_halts_on_next_stop(risk_mgr):
    risk_mgr.seed_stops(1, at(9, 0))
    assert risk_mgr.is_halted() is False
    assert risk_mgr.record_stop(at(10, 30)) is True
    assert risk_mgr.is_halted() is True


def test_third_stop_reports_no_new_halt(risk_mgr):
    """Only the stop that crosses the limit announces the HALT."""
    risk_mgr.record_stop(at(10, 0))
    assert risk_mgr.record_stop(at(10, 30)) is True
    risk_mgr.clear_halt()  # operator overrides
    assert risk_mgr.record_stop(at(11, 0)) is False
    assert risk_mgr.is_halted() is False


# -- main.py ----------------------------------------------------------------------


@pytest.fixture
async def orch_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    made = []

    async def make(journal_name: str = "journal.db"):
        o = main_mod.Orchestrator(sim=False)
        o.risk = RiskManager(
            halt_path=tmp_path / "HALT",
            daily_loss_limit_usd=1000.0,
            max_open_triggers=1,
            entry_cutoff_et="15:00",
            max_stops_per_day=2,
        )
        o.journal = main_mod.JournalDB(tmp_path / journal_name)
        await o.journal.connect()
        o.notifier = FakeNotifier()
        made.append(o)
        return o

    yield make
    for o in made:
        await o.journal.close()


async def _stop_through_main(o, event: StopHit) -> None:
    await o.journal.log_event(event)
    await o._handle_event(event)


def _halt_notices(o) -> list[str]:
    return [text for kind, text in o.notifier.sent if kind == "text" and "2 stops" in text]


async def test_main_two_stops_create_halt_and_notify(orch_factory, tmp_path):
    o = await orch_factory()
    now = _dt.datetime.now(ET)

    await _stop_through_main(o, stop("a", now))
    assert not (tmp_path / "HALT").exists()
    assert _halt_notices(o) == []

    await _stop_through_main(o, stop("b", now))
    assert (tmp_path / "HALT").exists()
    assert _halt_notices(o) == ["**HALT** — 2 stops: no new entries today (state/HALT)"]
    # the stop alerts themselves still go out
    assert [s for s in o.notifier.sent if s[0] == "stop"] == [("stop", "a"), ("stop", "b")]


async def test_restart_after_one_stop_still_halts_on_the_next(orch_factory, tmp_path):
    now = _dt.datetime.now(ET)
    first = await orch_factory()
    await _stop_through_main(first, stop("a", now))
    await first.journal.close()

    # a fresh process over the same journal
    second = await orch_factory()
    await second.restore_stop_count()
    assert second.risk.stops_today(now) == 1
    assert not (tmp_path / "HALT").exists()

    await _stop_through_main(second, stop("b", now))
    assert (tmp_path / "HALT").exists()
    assert len(_halt_notices(second)) == 1


async def test_restart_ignores_yesterdays_stops(orch_factory, tmp_path):
    o = await orch_factory()
    yesterday = _dt.datetime.now(ET) - _dt.timedelta(days=1)
    await o.journal.conn.execute(
        "INSERT INTO events (ts, type, trigger_id, payload) VALUES (?, ?, ?, ?)",
        (yesterday.isoformat(), "stop_hit", "a", stop("a", yesterday).model_dump_json()),
    )
    await o.journal.conn.commit()

    await o.restore_stop_count()
    assert o.risk.stops_today(_dt.datetime.now(ET)) == 0
