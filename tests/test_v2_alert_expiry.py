"""v2 alerts expire: EXECUTE is refused when the alert is older than
ALERT_TTL_MIN, or when SPX has moved more than ALERT_MAX_DISTANCE_PTS from
the fire price. An expired trigger goes FIRED -> SKIPPED and is journaled as
"expired". Untapped alerts also expire on their own (the input poller)."""

from __future__ import annotations

import datetime as _dt

import pytest

import config
import main as main_mod
from journal import JournalDB
from schema import Decision, Tick
from triggers import TriggerStatus

from test_v2_early_entry import (
    FakeInteraction,
    FakeRisk,
    at,
    decision_row,
    fired_engine,
    mk_notifier,
    mk_plan,
)

FIRE_PRICE = 7205.0


@pytest.fixture
async def journal(tmp_path):
    j = JournalDB(tmp_path / "journal.db")
    await j.connect()
    yield j
    await j.close()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


async def alerted(journal, sent_at=None, last_price=FIRE_PRICE, wait="09:30"):
    """A FIRED trigger whose Discord alert went out at sent_at (10:05 ET)."""
    sent_at = sent_at or at(10, 5)
    engine = fired_engine(mk_plan(wait), sent_at)
    risk = FakeRisk()
    n, decision_id = await mk_notifier(engine, journal, risk, sent_at)
    clock = Clock(sent_at)
    n.now_fn = clock
    n.alert_ttl_min = 10
    n.alert_max_distance_pts = 10
    price = {"last": last_price}
    n.last_price_fn = lambda: price["last"]
    trigger = engine.states["primary_long"].trigger
    await n.send_trigger_fired(
        trigger, Tick(ts=sent_at, price=FIRE_PRICE),
        Decision(action="EXECUTE", reasoning="ok", confidence=70), decision_id,
    )
    return engine, n, decision_id, clock, price, risk


# -- on the EXECUTE tap -------------------------------------------------------------


async def test_fresh_tap_confirms(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    clock.now = at(10, 14)
    price["last"] = FIRE_PRICE + 9.5
    interaction = FakeInteraction()
    await n._on_interaction(interaction)
    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED
    assert "EXECUTE confirmed" in interaction.response.edited_content


async def test_stale_by_time_is_refused_and_skipped(journal):
    engine, n, decision_id, clock, price, risk = await alerted(journal)
    clock.now = at(10, 16)
    interaction = FakeInteraction()
    await n._on_interaction(interaction)

    text = interaction.response.edited_content
    assert "EXPIRED" in text and "11 min old" in text and "limit 10" in text
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED
    assert (await decision_row(journal, decision_id))[0] == "expired"
    assert risk.calls == 0


@pytest.mark.parametrize("move", [+10.5, -12.0])
async def test_stale_by_distance_is_refused_and_skipped(journal, move):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    clock.now = at(10, 7)
    price["last"] = FIRE_PRICE + move
    interaction = FakeInteraction()
    await n._on_interaction(interaction)

    text = interaction.response.edited_content
    assert "EXPIRED" in text
    assert f"{abs(move):.1f} pts from the 7,205.00 alert price" in text
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED
    assert (await decision_row(journal, decision_id))[0] == "expired"


async def test_no_price_yet_only_checks_time(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal, last_price=None)
    clock.now = at(10, 8)
    await n._on_interaction(FakeInteraction())
    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED


async def test_early_tap_on_a_stale_alert_expires_without_the_reason_box(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal, sent_at=at(9, 40), wait="10:00")
    clock.now = at(9, 52)
    interaction = FakeInteraction()
    await n._on_interaction(interaction)
    assert interaction.response.modal is None
    assert "EXPIRED" in interaction.response.edited_content
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED


async def test_alert_that_goes_stale_while_the_reason_box_is_open(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal, sent_at=at(9, 40), wait="10:00")
    clock.now = at(9, 42)
    await n._on_interaction(FakeInteraction())
    price["last"] = FIRE_PRICE - 15
    submit = FakeInteraction()
    await n.submit_early_override(submit, "primary_long", "strong reclaim")
    assert "EXPIRED" in submit.response.edited_content
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED
    assert (await decision_row(journal, decision_id))[0] == "expired"


async def test_skip_on_a_stale_alert_is_still_a_skip(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    clock.now = at(10, 30)
    await n._on_interaction(FakeInteraction("skip:primary_long"))
    assert (await decision_row(journal, decision_id))[0] == "skip"


# -- untapped alerts expire on their own ----------------------------------------------


async def test_untapped_alert_expires_once(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    n.sent.clear()

    clock.now = at(10, 14)
    assert await n.expire_stale_alerts() == []
    assert engine.states["primary_long"].status == TriggerStatus.FIRED

    clock.now = at(10, 16)
    assert await n.expire_stale_alerts() == ["primary_long"]
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED
    assert (await decision_row(journal, decision_id))[0] == "expired"
    notices = [text for text, _ in n.sent]
    assert len(notices) == 1 and "ALERT EXPIRED" in notices[0] and "primary_long" in notices[0]

    clock.now = at(10, 30)
    assert await n.expire_stale_alerts() == []
    assert len(n.sent) == 1


async def test_tapped_alert_is_not_expired_later(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    clock.now = at(10, 6)
    await n._on_interaction(FakeInteraction())
    clock.now = at(10, 30)
    assert await n.expire_stale_alerts() == []
    assert engine.states["primary_long"].status == TriggerStatus.CONFIRMED


async def test_poller_expires_alerts_and_saves_state(tmp_path, monkeypatch, journal):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    o = main_mod.Orchestrator(sim=False)
    o.journal = journal
    o.engine, o.plan, o.notifier = engine, engine.plan, n
    clock.now = at(10, 20)
    await o.poll_inputs()
    assert engine.states["primary_long"].status == TriggerStatus.SKIPPED
    saved = (tmp_path / f"engine_{engine.plan.date}.json").read_text(encoding="utf-8")
    assert "SKIPPED" in saved


def test_settings_exist():
    assert config.ALERT_TTL_MIN == 10
    assert config.ALERT_MAX_DISTANCE_PTS == 10
