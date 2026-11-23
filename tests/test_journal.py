from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from journal import JournalDB
from schema import (
    Action,
    Condition,
    Decision,
    Invalidation,
    Plan,
    ScheduledEvent,
    Target,
    Tick,
    Trigger,
    TriggerFired,
)

ET = ZoneInfo("America/New_York")


def mk_plan() -> Plan:
    trigger = Trigger(
        id="t1",
        side="long",
        condition=Condition(price_between=(100.0, 110.0)),
        action=Action(instrument="test", size_pct=50),
        targets=[Target(level=120.0, scale_out_pct=100)],
        stop=90.0,
    )
    return Plan(
        date="2026-07-21",
        generated_at="2026-07-21T08:00:00-04:00",
        bias="bullish",
        spx_reference=100.0,
        triggers=[trigger],
        invalidation=Invalidation(below=50.0, action="flatten_and_halt"),
        flat_by="16:00",
    )


@pytest.fixture
async def db(tmp_path):
    journal = JournalDB(tmp_path / "journal.db")
    await journal.connect()
    yield journal
    await journal.close()


async def test_log_and_reconstruct_plan(db):
    plan = mk_plan()
    row_id = await db.log_plan(plan, "daily")
    assert row_id == 1

    cursor = await db.conn.execute("SELECT date, source, plan_json FROM plans WHERE id = ?", (row_id,))
    row = await cursor.fetchone()
    assert row[0] == "2026-07-21"
    assert row[1] == "daily"
    assert Plan.model_validate_json(row[2]) == plan


async def test_log_event(db):
    tick = Tick(ts=datetime(2026, 7, 21, 10, 0, tzinfo=ET), price=105.0)
    event = TriggerFired(trigger_id="t1", tick=tick)
    row_id = await db.log_event(event)

    cursor = await db.conn.execute("SELECT type, trigger_id FROM events WHERE id = ?", (row_id,))
    row = await cursor.fetchone()
    assert row[0] == "trigger_fired"
    assert row[1] == "t1"


async def test_log_decision_and_operator_response(db):
    decision = Decision(action="EXECUTE", reasoning="looks good", confidence=80)
    decision_id = await db.log_decision("t1", decision, latency_ms=123.4)

    await db.log_operator_response(decision_id, "confirm")

    cursor = await db.conn.execute(
        "SELECT agent_action, confidence, operator_response FROM decisions WHERE id = ?",
        (decision_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "EXECUTE"
    assert row[1] == 80
    assert row[2] == "confirm"


async def test_log_filled_does_not_clobber_operator_response(db):
    """Regression test: a confirmed trigger later marked filled must keep
    its confirm record — log_filled uses a separate column precisely so
    this doesn't happen. See notify.py's mark-filled handler."""
    decision = Decision(action="EXECUTE", reasoning="looks good", confidence=80)
    decision_id = await db.log_decision("t1", decision)

    await db.log_operator_response(decision_id, "confirm")
    await db.log_filled(decision_id)

    cursor = await db.conn.execute(
        "SELECT operator_response, filled_ts FROM decisions WHERE id = ?", (decision_id,)
    )
    row = await cursor.fetchone()
    assert row[0] == "confirm"  # not overwritten by the later filled mark
    assert row[1] is not None


async def test_migrate_adds_filled_ts_to_pre_existing_db(tmp_path):
    """Regression test: an already-existing journal.db (created before
    filled_ts existed) must gain the column on connect(), not error out."""
    import aiosqlite

    path = tmp_path / "old_journal.db"
    old_conn = await aiosqlite.connect(path)
    await old_conn.execute(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            trigger_id TEXT NOT NULL,
            agent_action TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            latency_ms REAL,
            operator_response TEXT,
            operator_response_ts TEXT
        )
        """
    )
    await old_conn.commit()
    await old_conn.close()

    journal = JournalDB(path)
    await journal.connect()
    try:
        decision_id = await journal.log_decision("t1", Decision(action="SKIP", reasoning="n/a", confidence=50))
        await journal.log_filled(decision_id)  # must not raise "no such column"
    finally:
        await journal.close()


async def test_log_release_success_and_timeout(db):
    se = ScheduledEvent(time="10:00", release="ISM Manufacturing PMI", source_id="finnhub:ism-mfg", replan=True)

    await db.log_release(se, {"actual": 49.5, "forecast": 49.2, "previous": 48.7, "surprise": 0.3})
    await db.log_release(se, None)

    cursor = await db.conn.execute("SELECT actual, timed_out FROM releases ORDER BY id")
    rows = await cursor.fetchall()
    assert rows[0][0] == "49.5"
    assert rows[0][1] == 0
    assert rows[1][0] is None
    assert rows[1][1] == 1


async def test_events_since_filters_by_timestamp(db):
    tick = Tick(ts=datetime(2026, 7, 21, 10, 0, tzinfo=ET), price=105.0)
    await db.log_event(TriggerFired(trigger_id="t1", tick=tick))

    future_cutoff = "2099-01-01T00:00:00"
    assert await db.events_since(future_cutoff) == []

    past_cutoff = "2000-01-01T00:00:00"
    events = await db.events_since(past_cutoff)
    assert len(events) == 1
    assert events[0]["trigger_id"] == "t1"
