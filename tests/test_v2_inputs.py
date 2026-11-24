"""v2: manual 10Y yield, GEX input, yield-level triggers, close-to-close
SPY->SPX calibration, plan installation via tools/write_plan.py, and the
input poller in main.py."""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import config
import inputs
import main as main_mod
from schema import Action, Condition, Invalidation, Plan, Target, Tick, Trigger
from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(REPO / "tools"))

import set_gex  # noqa: E402
import write_plan  # noqa: E402


def at(hh: int, mm: int, day: int = 24) -> _dt.datetime:
    return _dt.datetime(2026, 11, day, hh, mm, tzinfo=ET)


def yield_trigger(**cond) -> Trigger:
    return Trigger(
        id="y",
        side="long",
        condition=Condition(price_between=(7195.0, 7215.0), **cond),
        action=Action(instrument="SPX 0DTE call", size_pct=15),
        targets=[Target(level=7250.0, scale_out_pct=100)],
        stop=7172.0,
    )


def mk_plan(triggers, **kw) -> Plan:
    return Plan(
        date="2026-11-24",
        generated_at="2026-11-24T09:00:00-05:00",
        bias="bullish",
        spx_reference=7250.0,
        triggers=triggers,
        invalidation=Invalidation(below=7100.0, action="stand aside"),
        flat_by="15:30",
        **kw,
    )


# -- inputs.py ---------------------------------------------------------------


def test_normalize_yield_scales_tnx_quotes():
    assert inputs.normalize_yield(4.93) == 4.93
    assert inputs.normalize_yield(49.3) == 4.93
    with pytest.raises(ValueError):
        inputs.normalize_yield(0.1)


def test_yield_expires(tmp_path):
    y = inputs.write_yield(4.93, ts=at(10, 0), state_dir=tmp_path)
    assert inputs.read_yield(tmp_path) == y
    assert y.is_fresh(at(10, 19), max_age_min=20)
    assert not y.is_fresh(at(10, 21), max_age_min=20)


def test_gex_rejects_non_spx_levels():
    with pytest.raises(ValueError):
        inputs.GexInput(expiry_date="2026-11-24", gamma_flip=730.0, as_of=at(9, 0))


def test_gex_regime():
    g = inputs.GexInput(expiry_date="2026-11-24", gamma_flip=7300.59, call_wall=7200, put_wall=7200, as_of=at(9, 0))
    assert "negative" in g.regime(7237.17)
    assert "positive" in g.regime(7310)


# -- yield level conditions ---------------------------------------------------


def test_schema_rejects_yield_not_in_percent():
    with pytest.raises(ValueError):
        Condition(yield_below=49.5)
    with pytest.raises(ValueError):
        Condition(yield_below=4.9, yield_above=5.1)


def test_yield_below_fails_closed_without_yield_then_fires():
    engine = TriggerEngine(mk_plan([yield_trigger(yield_below=4.95)]))
    events = engine.evaluate(Tick(ts=at(10, 0), price=7220.0))
    events += engine.evaluate(Tick(ts=at(10, 1), price=7205.0))  # in zone, no yield
    assert engine.states["y"].status == TriggerStatus.ARMED
    assert any(e.type == "warning" for e in events)

    engine.evaluate(Tick(ts=at(10, 2), price=7220.0, yield_10y=5.02))
    engine.evaluate(Tick(ts=at(10, 3), price=7205.0, yield_10y=5.02))  # yield too high
    assert engine.states["y"].status == TriggerStatus.ARMED

    engine.evaluate(Tick(ts=at(10, 4), price=7220.0, yield_10y=4.93))
    fired = engine.evaluate(Tick(ts=at(10, 5), price=7205.0, yield_10y=4.93))
    assert [e.type for e in fired] == ["trigger_fired"]


def test_yield_above_gate():
    engine = TriggerEngine(mk_plan([yield_trigger(yield_above=5.15)]))
    engine.evaluate(Tick(ts=at(10, 0), price=7220.0, yield_10y=5.10))
    engine.evaluate(Tick(ts=at(10, 1), price=7205.0, yield_10y=5.10))
    assert engine.states["y"].status == TriggerStatus.ARMED
    engine.evaluate(Tick(ts=at(10, 2), price=7220.0, yield_10y=5.16))
    engine.evaluate(Tick(ts=at(10, 3), price=7205.0, yield_10y=5.16))
    assert engine.states["y"].status == TriggerStatus.FIRED


# -- Moomoo calibration -------------------------------------------------------


def _row(last: float, prev_close: float | None = None) -> dict:
    row = {"last_price": last, "data_date": "2026-11-24", "data_time": "09:31:00"}
    if prev_close is not None:
        row["prev_close_price"] = prev_close
    return row


def test_moomoo_calibrates_from_previous_closes():
    from feed import MoomooFeed

    feed = MoomooFeed(initial_ratio=10.0)
    feed.calibrate_from_prev_close(7237.17)
    tick = feed._to_tick(_row(722.80, prev_close=719.40))
    assert feed.ratio == pytest.approx(7237.17 / 719.40)
    assert tick.price == pytest.approx(722.80 * 7237.17 / 719.40)
    assert "prev closes" in feed.ratio_source

    # a replan's spx_reference must not override the close-to-close ratio
    feed.calibrate(7300.0)
    assert feed.ratio == pytest.approx(7237.17 / 719.40)


def test_moomoo_falls_back_to_spx_reference_without_prev_close():
    from feed import MoomooFeed

    feed = MoomooFeed(initial_ratio=10.0)
    feed.calibrate(7250.0)  # queued until a SPY price arrives
    feed._to_tick(_row(725.0))
    assert feed.ratio == pytest.approx(10.0)
    assert "spx_reference" in feed.ratio_source


# -- tools/write_plan.py ------------------------------------------------------


def test_example_plan_is_grounded_in_its_analysis():
    plan = Plan.model_validate_json((FIXTURES / "plan_v2_example.json").read_text(encoding="utf-8"))
    analysis = (FIXTURES / "analysis_2026-11-23.md").read_text(encoding="utf-8")
    assert write_plan.ungrounded_levels(plan, analysis) == []


def test_invented_level_is_caught():
    data = json.loads((FIXTURES / "plan_v2_example.json").read_text(encoding="utf-8"))
    data["triggers"][0]["stop"] = 7171.0  # not in the write-up (it says 7,172)
    plan = Plan.model_validate(data)
    analysis = (FIXTURES / "analysis_2026-11-23.md").read_text(encoding="utf-8")
    assert write_plan.ungrounded_levels(plan, analysis) == ["buy_dip.stop=7171"]


def test_numbers_in_text_handles_thousands_separators():
    assert {7195.0, 7215.0, 6910.39} <= write_plan.numbers_in_text("dips 7,195-7,215; close 6,910.39")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "DAILY_PLANS_DIR", str(tmp_path / "daily_plans"))
    return tmp_path / "state"


def _cli(*args: str) -> int:
    return write_plan.main([str(a) for a in args])


def test_write_plan_installs_then_requires_replan(state_dir, capsys):
    plan_json = FIXTURES / "plan_v2_example.json"
    analysis = FIXTURES / "analysis_2026-11-23.md"
    assert _cli(plan_json, "--analysis", analysis, "--date", "2026-11-23") == 0
    installed = Plan.model_validate_json((state_dir / "plan_2026-11-23.json").read_text(encoding="utf-8"))
    assert installed.spx_prev_close == 6910.39

    assert _cli(plan_json, "--analysis", analysis, "--date", "2026-11-23") == 1
    assert "--replan" in capsys.readouterr().err
    assert _cli(plan_json, "--analysis", analysis, "--date", "2026-11-23", "--replan") == 0


def test_write_plan_rejects_late_flat_by(state_dir, tmp_path, capsys):
    data = json.loads((FIXTURES / "plan_v2_example.json").read_text(encoding="utf-8"))
    data["flat_by"] = "15:45"
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(data), encoding="utf-8")
    assert _cli(draft, "--analysis", FIXTURES / "analysis_2026-11-23.md", "--dry-run") == 1
    assert "15:30" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["09:15", "15:10"])
def test_write_plan_rejects_opening_wait_outside_the_session(state_dir, tmp_path, capsys, bad):
    data = json.loads((FIXTURES / "plan_v2_example.json").read_text(encoding="utf-8"))
    data["no_entries_before"] = bad
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(data), encoding="utf-8")
    assert _cli(draft, "--analysis", FIXTURES / "analysis_2026-11-23.md", "--dry-run") == 1
    assert "no_entries_before" in capsys.readouterr().err


def test_write_plan_shows_the_opening_wait(state_dir, capsys):
    plan_json = FIXTURES / "plan_v2_example.json"
    assert _cli(plan_json, "--analysis", FIXTURES / "analysis_2026-11-23.md", "--date", "2026-11-23", "--dry-run") == 0
    assert "Opening wait: alerts before 10:00 ET need an early-entry override" in capsys.readouterr().out


# -- tools/set_gex.py ---------------------------------------------------------


def test_set_gex_rejects_wrong_expiry(state_dir, capsys):
    assert set_gex.main(["--expiry", "2020-01-02", "--flip", "7300"]) == 1
    assert "not today's 0DTE expiry" in capsys.readouterr().err


def test_set_gex_records_and_diffs(state_dir, capsys):
    today = _dt.datetime.now(ET).date().isoformat()
    assert set_gex.main(["--expiry", today, "--flip", "7300.59", "--call-wall", "7200", "--spx", "7237.17"]) == 0
    assert set_gex.main(["--expiry", today, "--flip", "7280", "--call-wall", "7200", "--spx", "7290"]) == 0
    out = capsys.readouterr().out
    assert "gamma flip 7300.59 -> 7280" in out
    assert "REGIME CHANGE" in out
    assert inputs.read_gex(state_dir).gamma_flip == 7280


# -- main.py input poller -----------------------------------------------------


class FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []
        self.summaries: list[Plan] = []
        self.engine = None

    async def send_text(self, text, view=None):
        self.texts.append(text)

    async def send_plan_summary(self, plan):
        self.summaries.append(plan)


@pytest.fixture
async def orch(state_dir, monkeypatch):
    state_dir.mkdir(parents=True, exist_ok=True)
    o = main_mod.Orchestrator(sim=True)
    o.journal = main_mod.JournalDB(state_dir / "journal.db")
    await o.journal.connect()
    o.notifier = FakeNotifier()
    yield o
    await o.journal.close()


async def test_manual_yield_attached_while_fresh_then_dropped(orch, state_dir):
    inputs.write_yield(4.93, ts=at(10, 0))
    await orch._reload_yield_if_changed()
    assert orch._attach_yield(Tick(ts=at(10, 5), price=7200.0)).yield_10y == 4.93
    assert orch._attach_yield(Tick(ts=at(11, 0), price=7200.0)).yield_10y is None
    # feed yields quoted TNX-style are normalized
    assert orch._attach_yield(Tick(ts=at(11, 0), price=7200.0, yield_10y=49.3)).yield_10y == 4.93


async def test_first_plan_written_after_start_is_loaded_and_announced(orch, state_dir, monkeypatch):
    plan = mk_plan([yield_trigger()])
    monkeypatch.setattr(main_mod, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: at(9, 0))}))
    await orch._reload_plan_if_file_changed()
    assert orch.plan is None

    (state_dir / "plan_2026-11-24.json").write_text(plan.model_dump_json(), encoding="utf-8")
    await orch._reload_plan_if_file_changed()
    assert orch.plan == plan
    assert orch.notifier.summaries == [plan]


async def test_yield_nudge_once_per_lapse(orch):
    orch._load_plan(mk_plan([yield_trigger(yield_below=4.95)]), "test")
    await orch._check_yield_nudge(at(8, 0))  # before the open: quiet
    assert orch.notifier.texts == []
    await orch._check_yield_nudge(at(10, 0))
    await orch._check_yield_nudge(at(10, 5))
    assert len(orch.notifier.texts) == 1 and "10Y yield needed" in orch.notifier.texts[0]

    inputs.write_yield(4.93, ts=at(10, 6))
    await orch._reload_yield_if_changed()
    await orch._check_yield_nudge(at(10, 10))  # fresh: quiet
    await orch._check_yield_nudge(at(10, 30))  # expired again: one more nudge
    assert len(orch.notifier.texts) == 2


async def test_no_yield_nudge_when_plan_has_no_yield_conditions(orch):
    orch._load_plan(mk_plan([yield_trigger()]), "test")
    await orch._check_yield_nudge(at(10, 0))
    assert orch.notifier.texts == []


async def test_decide_notes_include_yield_and_gex(orch, state_dir):
    orch._load_plan(mk_plan([yield_trigger()], notes="gap day"), "test")
    inputs.write_yield(4.93, ts=at(10, 0))
    inputs.write_gex(inputs.GexInput(expiry_date="2026-11-24", gamma_flip=7300.59, as_of=at(9, 50)))
    await orch._reload_yield_if_changed()
    await orch._reload_gex_if_changed()
    notes = orch._decide_notes(Tick(ts=at(10, 2), price=7237.0))
    assert "10Y 4.93%" in notes and "fresh" in notes
    assert "gamma flip 7300.59" in notes and "negative gamma" in notes
    assert "Plan notes: gap day" in notes


async def test_scheduler_skips_autogen_by_default(monkeypatch):
    import scheduler

    monkeypatch.setattr(config, "PLAN_AUTOGEN", False)

    async def noop(*a):
        return None

    s = scheduler.AgentScheduler(scheduler.SchedulerCallbacks(noop, noop, noop, noop))
    s.start()
    try:
        ids = {j.id for j in s.scheduler.get_jobs()}
        assert "generate_plan" not in ids and "eod_summary" in ids
    finally:
        s.shutdown()
