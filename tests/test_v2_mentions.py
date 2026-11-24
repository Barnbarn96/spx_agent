"""v2 @mentions: the alerts that need the operator now are prefixed with
<@DISCORD_MENTION_USER_ID>, and an untapped trigger alert gets one reminder
after ALERT_REMIND_MIN, before it expires."""

from __future__ import annotations

import datetime as _dt

import pytest

import config
import main as main_mod
from journal import JournalDB
from notify import Notifier
from schema import (
    Decision,
    Invalidated,
    StopHit,
    StopMoved,
    Tick,
)
from triggers import TriggerStatus

from test_v2_alert_expiry import FIRE_PRICE, Clock, alerted
from test_v2_early_entry import FakeInteraction, at

USER = "123456789012345678"
PING = f"<@{USER}>"


@pytest.fixture
async def journal(tmp_path):
    j = JournalDB(tmp_path / "journal.db")
    await j.connect()
    yield j
    await j.close()


def capture(n: Notifier) -> list[str]:
    sent: list[str] = []

    async def fake_send_text(text, view=None):
        sent.append(text)

    n.send_text = fake_send_text
    return sent


def tick(price=7170.0):
    return Tick(ts=at(10, 30), price=price)


def notifier(user=USER) -> tuple[Notifier, list[str]]:
    n = Notifier("token", 1, engine=None, journal=None, risk=None)
    n.mention_user_id = user
    return n, capture(n)


SENDS = {
    "stop": lambda n: n.send_stop_hit(StopHit(trigger_id="a", tick=tick())),
    "invalidated": lambda n: n.send_invalidated(Invalidated(level=7150.0, tick=tick(7149.0))),
    "stop_moved": lambda n: n.send_stop_moved(
        StopMoved(trigger_id="a", old_stop=7172.0, new_stop=7205.0, reason="breakeven after 7250", tick=tick(7250.0))
    ),
    "halt": lambda n: n.send_halt("2 stops: no new entries today (state/HALT)"),
    "feed_stale": lambda n: n.send_feed_stale(95.0),
}


@pytest.mark.parametrize("kind", sorted(SENDS))
async def test_urgent_alerts_mention_the_operator(kind):
    n, sent = notifier()
    await SENDS[kind](n)
    assert len(sent) == 1
    assert sent[0].startswith(PING + " ")


@pytest.mark.parametrize("kind", sorted(SENDS))
async def test_no_mention_when_no_user_is_set(kind):
    n, sent = notifier(user="")
    await SENDS[kind](n)
    assert "<@" not in sent[0]


async def test_routine_messages_do_not_mention():
    n, sent = notifier()
    await n.send_text("plain notice")
    from schema import TargetHit

    await n.send_target_hit(TargetHit(trigger_id="a", level=7250.0, scale_out_pct=50, tick=tick(7250.0)))
    await n.send_eod_summary("done")
    assert not any(PING in s for s in sent)


async def test_trigger_fired_alert_mentions(journal):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    n.mention_user_id = USER
    sent = capture(n)
    trigger = engine.states["primary_long"].trigger
    await n.send_trigger_fired(trigger, Tick(ts=at(10, 5), price=FIRE_PRICE),
                               Decision(action="EXECUTE", reasoning="ok", confidence=70), decision_id)
    assert sent[0].startswith(PING + " **TRIGGER FIRED")


def test_mention_setting_exists():
    assert hasattr(config, "DISCORD_MENTION_USER_ID")
    assert config.ALERT_REMIND_MIN == 5


# -- the untapped-alert reminder --------------------------------------------------------


async def _reminder_setup(journal, user=USER):
    engine, n, decision_id, clock, price, _ = await alerted(journal)
    n.mention_user_id = user
    n.alert_remind_min = 5
    n.sent.clear()
    return engine, n, clock


async def test_reminder_sent_once_before_expiry(journal):
    engine, n, clock = await _reminder_setup(journal)

    clock.now = at(10, 9)
    await n.expire_stale_alerts()
    assert n.sent == []

    clock.now = at(10, 10, ) + _dt.timedelta(seconds=30)
    await n.expire_stale_alerts()
    assert len(n.sent) == 1
    text = n.sent[0][0]
    assert text.startswith(PING + " ")
    assert "REMINDER" in text and "primary_long" in text and "expires in 4 min" in text

    clock.now = at(10, 12)
    await n.expire_stale_alerts()
    assert len(n.sent) == 1, "only one reminder per alert"
    assert engine.states["primary_long"].status == TriggerStatus.FIRED


async def test_no_reminder_after_a_tap(journal):
    engine, n, clock = await _reminder_setup(journal)
    clock.now = at(10, 7)
    await n._on_interaction(FakeInteraction("skip:primary_long"))
    n.sent.clear()
    clock.now = at(10, 11)
    await n.expire_stale_alerts()
    assert n.sent == []


async def test_reminder_then_expiry(journal):
    engine, n, clock = await _reminder_setup(journal)
    clock.now = at(10, 11)
    await n.expire_stale_alerts()
    clock.now = at(10, 16)
    await n.expire_stale_alerts()
    kinds = ["REMINDER" in t for t, _ in n.sent]
    assert kinds == [True, False]
    assert "ALERT EXPIRED" in n.sent[1][0]
    assert n.sent[1][0].startswith(PING + " ")


async def test_reminder_without_a_user_still_sends_plain(journal):
    engine, n, clock = await _reminder_setup(journal, user="")
    clock.now = at(10, 11)
    await n.expire_stale_alerts()
    assert len(n.sent) == 1 and n.sent[0][0].startswith("**REMINDER")


# -- main.py routes StopMoved through the notifier ----------------------------------------


class RecordingNotifier:
    def __init__(self):
        self.calls = []
        self.engine = None

    async def send_stop_moved(self, event):
        self.calls.append(("stop_moved", event.trigger_id, event.new_stop))

    async def send_text(self, text, view=None):
        self.calls.append(("text", text))


async def test_main_sends_stop_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    o = main_mod.Orchestrator(sim=True)
    o.notifier = RecordingNotifier()
    event = StopMoved(trigger_id="a", old_stop=7172.0, new_stop=7205.0, reason="breakeven", tick=tick(7250.0))
    await o._handle_event(event)
    assert o.notifier.calls == [("stop_moved", "a", 7205.0)]
