"""Regression test for the confirm-time risk gate in notify.py.

Alerts (TriggerFired -> decide() -> Discord message) now flow regardless of
risk state, per main.py's _handle_trigger_fired — the operator should never
go dark, e.g. past the entry cutoff. The hard guardrail moves here instead:
tapping EXECUTE must be refused in real time by risk.gate_execute() rather
than relying on a stale fire-time check.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import discord

from notify import Notifier
from risk import RiskCheckResult
from schema import Action, Condition, Invalidation, Plan, Target, Trigger
from triggers import TriggerEngine

ET = ZoneInfo("America/New_York")


class FakeRisk:
    def __init__(self, allowed: bool, reason: str | None = None) -> None:
        self._result = RiskCheckResult(allowed, reason)
        self.calls = 0

    def gate_execute(self, engine, now) -> RiskCheckResult:
        self.calls += 1
        return self._result


class FakeResponse:
    def __init__(self) -> None:
        self.edited_content: str | None = None

    async def edit_message(self, content: str) -> None:
        self.edited_content = content


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeInteraction:
    def __init__(self, custom_id: str) -> None:
        self.type = discord.InteractionType.component
        self.data = {"custom_id": custom_id}
        self.message = FakeMessage("**TRIGGER FIRED: primary_long**")
        self.response = FakeResponse()


def mk_engine() -> TriggerEngine:
    trigger = Trigger(
        id="primary_long",
        side="long",
        condition=Condition(price_between=(100.0, 110.0)),
        action=Action(instrument="SPX 0DTE primary_long", size_pct=50),
        targets=[Target(level=120.0, scale_out_pct=100)],
        stop=90.0,
    )
    plan = Plan(
        date="2026-07-29",
        generated_at="2026-07-29T08:00:00-04:00",
        bias="bullish",
        spx_reference=100.0,
        triggers=[trigger],
        invalidation=Invalidation(below=50.0, action="flatten_and_halt"),
        flat_by="16:00",
    )
    engine = TriggerEngine(plan)
    from schema import Tick

    # start outside the zone (v2: no fire on first sight), then enter it
    engine.evaluate(Tick(ts=_dt.datetime(2026, 7, 29, 9, 59, tzinfo=ET), price=95.0))
    engine.evaluate(Tick(ts=_dt.datetime(2026, 7, 29, 10, 0, tzinfo=ET), price=105.0))
    return engine


async def test_confirm_blocked_by_risk_does_not_confirm() -> None:
    engine = mk_engine()
    risk = FakeRisk(allowed=False, reason="past hard entry cutoff 15:00 ET")
    notifier = Notifier("token", 123, engine=engine, journal=None, risk=risk)

    interaction = FakeInteraction("confirm:primary_long")
    await notifier._on_interaction(interaction)

    assert risk.calls == 1
    assert "blocked by risk" in interaction.response.edited_content
    assert "past hard entry cutoff" in interaction.response.edited_content
    from triggers import TriggerStatus

    assert engine.states["primary_long"].status == TriggerStatus.FIRED, "confirm() must not run when blocked"


async def test_confirm_allowed_by_risk_confirms() -> None:
    engine = mk_engine()
    risk = FakeRisk(allowed=True)
    notifier = Notifier("token", 123, engine=engine, journal=None, risk=risk)

    async def _noop(trigger_id: str) -> None:
        pass

    notifier.send_mark_filled_prompt = _noop  # avoid a real Discord send

    interaction = FakeInteraction("confirm:primary_long")
    await notifier._on_interaction(interaction)

    from triggers import TriggerStatus

    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED
    assert "EXECUTE confirmed by operator" in interaction.response.edited_content


async def test_stale_button_is_ignored_not_falsely_confirmed() -> None:
    """v2: a button can outlive its trigger's FIRED state (replan, restart
    re-arm). Tapping it must say so rather than claim success."""
    engine = mk_engine()
    engine.skip("primary_long")  # no longer awaiting a decision
    risk = FakeRisk(allowed=True)
    notifier = Notifier("token", 123, engine=engine, journal=None, risk=risk)

    interaction = FakeInteraction("confirm:primary_long")
    await notifier._on_interaction(interaction)

    from triggers import TriggerStatus

    assert "Ignored" in interaction.response.edited_content
    assert "SKIPPED" in interaction.response.edited_content
    assert risk.calls == 0
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED
