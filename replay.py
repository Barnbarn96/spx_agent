"""Offline harness: replay ticks from a CSV against a plan through the real
TriggerEngine. No network access, no live decision calls — `decide` is
stubbed to always EXECUTE unless --interactive is passed.

Usage:
    python replay.py --plan state/plan_2026-07-01.json \\
        --ticks tests/fixtures/ticks_sample.csv [--speed 0|60x] [--interactive] \\
        [--journal state/replay_journal.db]

CSV columns: ts_et,price,yield_10y
  - ts_et: ISO 8601 datetime. If it has no UTC offset, it is assumed to
    already be America/New_York wall-clock time.
  - yield_10y: optional, blank means "no yield tick this row".
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time as time_module
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from schema import EngineEvent, Plan, Tick, TriggerFired
from triggers import TriggerEngine

ET = ZoneInfo("America/New_York")


def load_plan(path: Path) -> Plan:
    return Plan.model_validate_json(path.read_text(encoding="utf-8"))


def load_ticks(path: Path) -> list[Tick]:
    ticks: list[Tick] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.fromisoformat(row["ts_et"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            yield_raw = (row.get("yield_10y") or "").strip()
            yield_10y = float(yield_raw) if yield_raw else None
            ticks.append(Tick(ts=ts, price=float(row["price"]), yield_10y=yield_10y))
    return ticks


def init_journal(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            trigger_id TEXT,
            payload TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def log_event(conn: sqlite3.Connection, event: EngineEvent) -> None:
    payload = event.model_dump(mode="json")
    ts = payload.get("tick", {}).get("ts", "")
    trigger_id = payload.get("trigger_id")
    conn.execute(
        "INSERT INTO events (ts, type, trigger_id, payload) VALUES (?, ?, ?, ?)",
        (ts, event.type, trigger_id, json.dumps(payload)),
    )
    conn.commit()


def format_event(event: EngineEvent) -> str:
    return f"{event.type:16s} {event.model_dump_json(exclude_none=True)}"


def run(
    engine: TriggerEngine,
    ticks: list[Tick],
    conn: sqlite3.Connection,
    speed: str,
    interactive: bool,
) -> list[EngineEvent]:
    all_events: list[EngineEvent] = []
    prev_ts: datetime | None = None
    speed_multiplier = None if speed == "0" else float(speed.rstrip("xX"))

    for tick in ticks:
        if speed_multiplier is not None and prev_ts is not None:
            real_gap = (tick.ts - prev_ts).total_seconds()
            sleep_for = max(0.0, real_gap / speed_multiplier)
            if sleep_for > 0:
                time_module.sleep(sleep_for)
        prev_ts = tick.ts

        events = engine.evaluate(tick)
        for event in events:
            print(format_event(event))
            log_event(conn, event)
            all_events.append(event)

            if isinstance(event, TriggerFired):
                if interactive:
                    resp = input(
                        f"  -> {event.trigger_id}: [E]xecute / [S]kip / [W]ait? "
                    ).strip().lower()
                else:
                    resp = "e"

                if resp.startswith("e"):
                    engine.confirm(event.trigger_id)
                    engine.mark_filled(event.trigger_id)
                elif resp.startswith("w"):
                    engine.wait(event.trigger_id, tick)
                else:
                    engine.skip(event.trigger_id)

    return all_events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--ticks", required=True, type=Path)
    parser.add_argument("--speed", default="0", help="0 = as fast as possible, e.g. 60x = 60x real time")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="sqlite journal path (default: throwaway temp file)",
    )
    args = parser.parse_args(argv)

    plan = load_plan(args.plan)
    ticks = load_ticks(args.ticks)
    engine = TriggerEngine(plan)

    if args.journal is not None:
        journal_path = args.journal
    else:
        journal_path = Path("state") / f"replay_journal_{plan.date}.db"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        if journal_path.exists():
            journal_path.unlink()

    conn = init_journal(journal_path)
    try:
        events = run(engine, ticks, conn, args.speed, args.interactive)
    finally:
        conn.close()

    print(f"\n{len(events)} events. Journal: {journal_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
