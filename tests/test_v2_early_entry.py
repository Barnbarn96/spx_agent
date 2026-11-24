"""v2 soft early-entry block: Plan.no_entries_before. Triggers still alert
before it, but EXECUTE needs an override with a one-line reason. HALT and
the other hard rules still win. Each decision is tagged early / regular in
the journal so the operator can compare results later."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import aiosqlite
import discord
import pydantic
import pytest

import agent
import config
import main as main_mod
from journal import JournalDB
from notify import EarlyEntryModal, Notifier
from risk import RiskCheckResult, is_early
from schema import Action, Condition, Decision, Invalidation, Plan, Target, Tick, Trigger
from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")


def at(hh: int, mm: int) -> _dt.datetime:
    return _dt.datetime(2026, 11, 24, hh, mm, tzinfo=ET)


def mk_plan(no_entries_before: str | None = "10:00") -> Plan:
    trigger = Trigger(
        id="primary_long",
        side="long",
        condition=Condition(price_between=(7195.0, 7215.0), not_before="09:30"),
        action=Action(instrument="SPX 0DTE", size_pct=15),
        targets=[Target(level=7250.0, scale_out_pct=100)],
        stop=7172.0,
    )
    return Plan(
        date="2026-11-24",
        generated_at="2026-11-24T09:00:00-05:00",
        bias="bullish",
        spx_reference=7230.0,
        triggers=[trigger],
        invalidation=Invalidation(below=7000.0, action="stand aside"),
        flat_by="15:30",
        no_entries_before=no_entries_before,
    )


def fired_engine(plan: Plan, when: _dt.datetime) -> TriggerEngine:
    engine = TriggerEngine(plan)
    engine.evaluate(Tick(ts=when - _dt.timedelta(minutes=1), price=7230.0))
    engine.evaluate(Tick(ts=when, price=7205.0))
    assert engine.states["primary_long"].status == TriggerStatus.FIRED
    return engine


# -- schema / risk ----------------------------------------------------------------


def test_plan_no_entries_before_defaults_to_none_and_validates():
    assert mk_plan(None).no_entries_before is None
    assert mk_plan("09:45").no_entries_before == "09:45"
    with pytest.raises(pydantic.ValidationError):
        mk_plan("9.45")


def test_is_early():
    p = mk_plan("10:00")
    assert is_early(p, at(9, 59)) is True
    assert is_early(p, at(10, 0)) is False
    assert is_early(mk_plan(None), at(9, 31)) is False


# -- fakes ------------------------------------------------------------------------


class FakeRisk:
    def __init__(self, allowed: bool = True, reason: str | None = None):
        self.result = RiskCheckResult(allowed, reason)
        self.calls = 0

    def gate_execute(self, engine, now):
        self.calls += 1
        return self.result


class FakeResponse:
    def __init__(self):
        self.edited_content: str | None = None
        self.modal = None

    async def edit_message(self, content: str):
        self.edited_content = content

    async def send_modal(self, modal):
        self.modal = modal


class FakeInteraction:
    def __init__(self, custom_id: str = "confirm:primary_long", content: str = "**TRIGGER FIRED: primary_long**"):
        self.type = discord.InteractionType.component
        self.data = {"custom_id": custom_id}
        self.message = type("M", (), {"content": content})()
        self.response = FakeResponse()


@pytest.fixture
async def journal(tmp_path):
    j = JournalDB(tmp_path / "journal.db")
    await j.connect()
    yield j
    await j.close()


async def mk_notifier(engine, journal, risk, now):
    n = Notifier("token", 123, engine=engine, journal=journal, risk=risk)
    n.now_fn = lambda: now
    n.sent = []

    async def fake_send_text(text, view=None):
        n.sent.append((text, view))

    n.send_text = fake_send_text
    decision_id = await journal.log_decision(
        "primary_long", Decision(action="EXECUTE", reasoning="ok", confidence=70), entry_timing="early"
    )
    n.register_decision("primary_long", decision_id)
    return n, decision_id


async def decision_row(journal, decision_id):
    cursor = await journal.conn.execute(
        "SELECT operator_response, entry_timing, early_reason FROM decisions WHERE id = ?", (decision_id,)
    )
    return await cursor.fetchone()


# -- the alert's button ---------------------------------------------------------------


def _labels(view) -> list[str]:
    return [item.label for item in view.children]


async def test_alert_before_the_wait_shows_the_override_button(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    n, _ = await mk_notifier(engine, journal, FakeRisk(), at(9, 40))
    trigger = engine.states["primary_long"].trigger
    await n.send_trigger_fired(trigger, Tick(ts=at(9, 40), price=7205.0),
                               Decision(action="EXECUTE", reasoning="ok", confidence=70), 1)
    text, view = n.sent[-1]
    assert _labels(view) == ["Early entry — override?", "SKIP"]
    assert "10:00 ET" in text and "23:00 SGT" in text


async def test_alert_after_the_wait_is_unchanged(journal):
    engine = fired_engine(mk_plan("10:00"), at(10, 5))
    n, _ = await mk_notifier(engine, journal, FakeRisk(), at(10, 5))
    trigger = engine.states["primary_long"].trigger
    await n.send_trigger_fired(trigger, Tick(ts=at(10, 5), price=7205.0),
                               Decision(action="EXECUTE", reasoning="ok", confidence=70), 1)
    text, view = n.sent[-1]
    assert _labels(view) == ["EXECUTE", "SKIP"]
    assert "Early" not in text


# -- tapping EXECUTE ------------------------------------------------------------------


async def test_early_tap_opens_the_reason_box_and_does_not_confirm(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    risk = FakeRisk()
    n, decision_id = await mk_notifier(engine, journal, risk, at(9, 41))

    interaction = FakeInteraction()
    await n._on_interaction(interaction)

    assert isinstance(interaction.response.modal, EarlyEntryModal)
    assert interaction.response.edited_content is None  # the modal is the only response
    assert risk.calls == 1
    assert engine.states["primary_long"].status == TriggerStatus.FIRED
    assert (await decision_row(journal, decision_id))[0] is None


async def test_override_with_reason_confirms_and_journals_it(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    risk = FakeRisk()
    n, decision_id = await mk_notifier(engine, journal, risk, at(9, 41))

    await n._on_interaction(FakeInteraction())
    submit = FakeInteraction()
    await n.submit_early_override(submit, "primary_long", "  reclaimed 7200 on volume  ")

    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED
    assert "EARLY ENTRY confirmed" in submit.response.edited_content
    assert "reclaimed 7200 on volume" in submit.response.edited_content
    assert risk.calls == 2  # re-checked on submit
    assert await decision_row(journal, decision_id) == ("confirm", "early", "reclaimed 7200 on volume")
    assert any(v is not None and _labels(v) == ["Mark Filled"] for _, v in n.sent)


async def test_blank_reason_is_refused(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(), at(9, 41))
    submit = FakeInteraction()
    await n.submit_early_override(submit, "primary_long", "   ")
    assert engine.states["primary_long"].status == TriggerStatus.FIRED
    assert "needs a reason" in submit.response.edited_content
    assert (await decision_row(journal, decision_id))[0] is None


async def test_regular_tap_confirms_directly_and_tags_regular(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 55))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(), at(10, 1))

    interaction = FakeInteraction()
    await n._on_interaction(interaction)

    assert interaction.response.modal is None
    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED
    assert "EXECUTE confirmed by operator" in interaction.response.edited_content
    # fired at 09:55 (tagged early), but entered at 10:01: the entry is regular
    assert await decision_row(journal, decision_id) == ("confirm", "regular", None)


async def test_plan_without_a_wait_never_asks(journal):
    engine = fired_engine(mk_plan(None), at(9, 35))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(), at(9, 35))
    interaction = FakeInteraction()
    await n._on_interaction(interaction)
    assert interaction.response.modal is None
    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED


async def test_halt_wins_over_an_early_tap(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(False, "HALT file present"), at(9, 41))

    interaction = FakeInteraction()
    await n._on_interaction(interaction)

    assert interaction.response.modal is None
    assert "blocked by risk (HALT file present)" in interaction.response.edited_content
    assert engine.states["primary_long"].status == TriggerStatus.FIRED


async def test_halt_created_while_the_box_is_open_still_blocks(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    risk = FakeRisk()
    n, decision_id = await mk_notifier(engine, journal, risk, at(9, 41))
    await n._on_interaction(FakeInteraction())

    risk.result = RiskCheckResult(False, "HALT file present")
    submit = FakeInteraction()
    await n.submit_early_override(submit, "primary_long", "strong reclaim")

    assert "blocked by risk (HALT file present)" in submit.response.edited_content
    assert engine.states["primary_long"].status == TriggerStatus.FIRED
    assert (await decision_row(journal, decision_id))[0] is None


async def test_trigger_skipped_while_the_box_is_open_is_ignored(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(), at(9, 41))
    await n._on_interaction(FakeInteraction())

    engine.skip("primary_long")
    submit = FakeInteraction()
    await n.submit_early_override(submit, "primary_long", "strong reclaim")

    assert "Ignored" in submit.response.edited_content
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED


async def test_modal_submit_calls_back_into_the_notifier(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(), at(9, 41))
    interaction = FakeInteraction()
    await n._on_interaction(interaction)

    modal = interaction.response.modal
    modal.reason._value = "strong reclaim"
    submit = FakeInteraction()
    await modal.on_submit(submit)
    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED


# -- journal ------------------------------------------------------------------------


async def test_skip_keeps_the_fire_time_tag(journal):
    engine = fired_engine(mk_plan("10:00"), at(9, 40))
    n, decision_id = await mk_notifier(engine, journal, FakeRisk(), at(9, 41))
    await n._on_interaction(FakeInteraction("skip:primary_long"))
    assert await decision_row(journal, decision_id) == ("skip", "early", None)


async def test_old_journal_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "trigger_id TEXT NOT NULL, agent_action TEXT NOT NULL, reasoning TEXT NOT NULL, "
            "confidence INTEGER NOT NULL, latency_ms REAL, operator_response TEXT, operator_response_ts TEXT)"
        )
        await conn.commit()
    j = JournalDB(path)
    await j.connect()
    cursor = await j.conn.execute("PRAGMA table_info(decisions)")
    columns = {row[1] for row in await cursor.fetchall()}
    await j.close()
    assert {"filled_ts", "entry_timing", "early_reason"} <= columns


# -- main.py tags each decision by fire time --------------------------------------------


@pytest.mark.parametrize("fire,expected", [((9, 40), "early"), ((10, 5), "regular")])
async def test_main_tags_decisions_by_fire_time(tmp_path, monkeypatch, fire, expected):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))

    async def fake_decide(trigger, plan_, ctx):
        return Decision(action="SKIP", reasoning="no", confidence=10)

    monkeypatch.setattr(agent, "decide", fake_decide)
    o = main_mod.Orchestrator(sim=True)
    o.journal = JournalDB(tmp_path / "j.db")
    await o.journal.connect()
    try:
        o._load_plan(mk_plan("10:00"), "daily")
        when = at(*fire)
        await o.process_tick(Tick(ts=when - _dt.timedelta(minutes=1), price=7230.0))
        await o.process_tick(Tick(ts=when, price=7205.0))
        await o.wait_for_decisions()
        cursor = await o.journal.conn.execute("SELECT entry_timing FROM decisions")
        assert [r[0] for r in await cursor.fetchall()] == [expected]
    finally:
        await o.journal.close()
