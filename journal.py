"""SQLite (aiosqlite) accountability log: every plan, engine event, agent
decision, and release poll. Every decision must be reconstructable from this
file alone — see SPEC.md.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from schema import Decision, EngineEvent, Plan, ScheduledEvent

ET = ZoneInfo("America/New_York")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    plan_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    trigger_id TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    agent_action TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    latency_ms REAL,
    operator_response TEXT,
    operator_response_ts TEXT,
    filled_ts TEXT
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    release TEXT NOT NULL,
    source_id TEXT NOT NULL,
    actual TEXT,
    forecast TEXT,
    previous TEXT,
    surprise TEXT,
    timed_out INTEGER NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(ET).isoformat()


class JournalDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS doesn't add columns to an
        already-existing table — patch older journal.db files forward."""
        cursor = await self._conn.execute("PRAGMA table_info(decisions)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "filled_ts" not in columns:
            await self._conn.execute("ALTER TABLE decisions ADD COLUMN filled_ts TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "JournalDB":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("JournalDB.connect() must be called first")
        return self._conn

    async def log_plan(self, plan: Plan, source: str) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO plans (ts, date, generated_at, source, plan_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), str(plan.date), plan.generated_at.isoformat(), source, plan.model_dump_json()),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def log_event(self, event: EngineEvent) -> int:
        payload = event.model_dump(mode="json")
        trigger_id = payload.get("trigger_id")
        cursor = await self.conn.execute(
            "INSERT INTO events (ts, type, trigger_id, payload) VALUES (?, ?, ?, ?)",
            (_now_iso(), event.type, trigger_id, event.model_dump_json()),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def log_decision(
        self, trigger_id: str, decision: Decision, latency_ms: float | None = None
    ) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO decisions "
            "(ts, trigger_id, agent_action, reasoning, confidence, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now_iso(), trigger_id, decision.action, decision.reasoning, decision.confidence, latency_ms),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def log_operator_response(self, decision_id: int, response: str) -> None:
        """Records the operator's gate decision (confirm/skip) on a fired
        trigger. Not used for the later mark-filled step — see log_filled —
        since a single decision can go through both, and this would
        overwrite the confirm/skip record if reused."""
        await self.conn.execute(
            "UPDATE decisions SET operator_response = ?, operator_response_ts = ? WHERE id = ?",
            (response, _now_iso(), decision_id),
        )
        await self.conn.commit()

    async def log_filled(self, decision_id: int) -> None:
        """Records when the operator marked a confirmed trigger as filled —
        a separate column from operator_response so it doesn't clobber the
        earlier confirm record for the same decision."""
        await self.conn.execute(
            "UPDATE decisions SET filled_ts = ? WHERE id = ?",
            (_now_iso(), decision_id),
        )
        await self.conn.commit()

    async def log_release(
        self, scheduled_event: ScheduledEvent, result: dict | None
    ) -> int:
        timed_out = result is None
        cursor = await self.conn.execute(
            "INSERT INTO releases "
            "(ts, release, source_id, actual, forecast, previous, surprise, timed_out) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now_iso(),
                scheduled_event.release,
                scheduled_event.source_id,
                None if result is None else str(result.get("actual")),
                None if result is None else str(result.get("forecast")),
                None if result is None else str(result.get("previous")),
                None if result is None else str(result.get("surprise")),
                int(timed_out),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def events_since(self, since_iso: str) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT ts, type, trigger_id, payload FROM events WHERE ts >= ? ORDER BY ts", (since_iso,)
        )
        rows = await cursor.fetchall()
        return [{"ts": r[0], "type": r[1], "trigger_id": r[2], "payload": r[3]} for r in rows]

    async def decisions_since(self, since_iso: str) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT ts, trigger_id, agent_action, reasoning, confidence, operator_response, filled_ts "
            "FROM decisions WHERE ts >= ? ORDER BY ts",
            (since_iso,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "ts": r[0],
                "trigger_id": r[1],
                "agent_action": r[2],
                "reasoning": r[3],
                "confidence": r[4],
                "operator_response": r[5],
                "filled_ts": r[6],
            }
            for r in rows
        ]
