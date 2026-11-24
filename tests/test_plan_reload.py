"""Regression tests for main.py's mid-session plan hot-reload — the path a
manual `python -m agent replan` takes to reach an already-running main.py.

The safety-critical property: a trigger the operator has OPEN must keep its
state (and therefore its stop monitoring) across the swap. Losing it would
mean a live position silently stops being tracked.
"""

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

import config
import main as main_mod
from schema import Action, Condition, Invalidation, Plan, Target, Tick, Trigger
from triggers import TriggerStatus

ET = ZoneInfo("America/New_York")


def drive_to_open(engine, trigger_id: str, price: float) -> None:
    """Walk a trigger through the real state machine to OPEN, the way a
    live session would: a tick inside the zone fires it, the operator taps
    EXECUTE, then marks it filled. confirm()/mark_filled() are no-ops from
    the wrong state, so the fire step is not optional. Starts below every
    test zone (and above the invalidation), since v2 never fires on the
    first sight of a zone."""
    engine.evaluate(Tick(ts=_dt.datetime(2026, 7, 29, 9, 59, tzinfo=ET), price=95.0))
    engine.evaluate(Tick(ts=_dt.datetime(2026, 7, 29, 10, 0, tzinfo=ET), price=price))
    assert engine.states[trigger_id].status == TriggerStatus.FIRED
    engine.confirm(trigger_id)
    engine.mark_filled(trigger_id)
    assert engine.states[trigger_id].status == TriggerStatus.OPEN


def mk_trigger(id: str, zone: tuple[float, float] = (100.0, 110.0), stop: float = 90.0) -> Trigger:
    return Trigger(
        id=id,
        side="long",
        condition=Condition(price_between=zone),
        action=Action(instrument=f"SPX 0DTE {id}", size_pct=50),
        targets=[Target(level=120.0, scale_out_pct=100)],
        stop=stop,
    )


def mk_plan(triggers: list[Trigger], spx_reference: float = 100.0) -> Plan:
    return Plan(
        date="2026-07-29",
        generated_at="2026-07-29T08:00:00-04:00",
        bias="bullish",
        spx_reference=spx_reference,
        triggers=triggers,
        invalidation=Invalidation(below=50.0, action="flatten_and_halt"),
        flat_by="16:00",
    )


@pytest.fixture
async def orchestrator(tmp_path, monkeypatch):
    """An Orchestrator with a temp state dir and a connected journal, but no
    Discord notifier and no live feed."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DAILY_PLANS_DIR", str(tmp_path / "daily_plans"))
    monkeypatch.setattr(main_mod.config, "STATE_DIR", str(tmp_path))

    orch = main_mod.Orchestrator(sim=True)
    orch.journal = main_mod.JournalDB(tmp_path / "journal.db")
    await orch.journal.connect()
    # Freeze "today" so the reload looks for the file we actually write.
    monkeypatch.setattr(
        main_mod, "datetime", _FrozenDatetime, raising=True
    )
    yield orch, tmp_path
    await orch.journal.close()


class _FrozenDatetime:
    """Pins datetime.now(ET).strftime('%Y-%m-%d') to the plans' date."""

    @staticmethod
    def now(tz=None):
        import datetime as _dt

        return _dt.datetime(2026, 7, 29, 12, 0, tzinfo=tz)


def _write_plan(state_dir, plan: Plan) -> None:
    path = state_dir / f"plan_{plan.date}.json"
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")


async def test_reload_preserves_open_trigger_state(orchestrator):
    """The core safety property: an OPEN position survives a mid-day
    re-plan, so its stop keeps being monitored."""
    orch, state_dir = orchestrator

    original = mk_plan([mk_trigger("primary_long"), mk_trigger("fade", zone=(200.0, 210.0), stop=190.0)])
    _write_plan(state_dir, original)
    orch._load_todays_plan_if_present()

    # Operator confirmed and filled primary_long — it's a live position now.
    drive_to_open(orch.engine, "primary_long", price=105.0)

    # A manual replan rewrites the file: same id kept, new levels.
    replanned = mk_plan(
        [mk_trigger("primary_long", zone=(105.0, 115.0), stop=95.0), mk_trigger("fade", zone=(200.0, 210.0), stop=190.0)]
    )
    _write_plan(state_dir, replanned)

    await orch._reload_plan_if_file_changed()

    assert orch.engine.states["primary_long"].status == TriggerStatus.OPEN, "OPEN position lost across replan"
    assert orch.engine.states["primary_long"].trigger.stop == 95.0, "new levels not applied"


async def test_reload_is_noop_when_file_unchanged(orchestrator):
    orch, state_dir = orchestrator
    original = mk_plan([mk_trigger("primary_long")])
    _write_plan(state_dir, original)
    orch._load_todays_plan_if_present()

    engine_before = orch.engine
    await orch._reload_plan_if_file_changed()
    assert orch.engine is engine_before, "engine rebuilt despite no file change"


async def test_reload_ignores_unparseable_file(orchestrator):
    """A half-written file (caught mid-write) must not clobber the plan in
    force — the next poll retries."""
    orch, state_dir = orchestrator
    original = mk_plan([mk_trigger("primary_long")])
    _write_plan(state_dir, original)
    orch._load_todays_plan_if_present()

    path = state_dir / f"plan_{original.date}.json"
    path.write_text('{"date": "2026-07-29", "triggers": [', encoding="utf-8")  # truncated

    await orch._reload_plan_if_file_changed()
    assert orch.plan == original, "a corrupt plan file replaced the plan in force"


async def test_reload_keeps_dropped_open_trigger_for_review(orchestrator):
    """If a re-plan drops a trigger the operator has OPEN, the engine keeps
    it (and raises ReviewRequired) rather than silently discarding a live
    position — see triggers.TriggerEngine.load_plan."""
    orch, state_dir = orchestrator
    original = mk_plan([mk_trigger("primary_long"), mk_trigger("fade", zone=(200.0, 210.0), stop=190.0)])
    _write_plan(state_dir, original)
    orch._load_todays_plan_if_present()

    drive_to_open(orch.engine, "primary_long", price=105.0)

    replanned = mk_plan([mk_trigger("fade", zone=(200.0, 210.0), stop=190.0)])  # primary_long dropped
    _write_plan(state_dir, replanned)

    await orch._reload_plan_if_file_changed()

    assert "primary_long" in orch.engine.states, "OPEN trigger silently dropped by replan"
    assert orch.engine.states["primary_long"].status == TriggerStatus.OPEN
