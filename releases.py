"""Econ calendar polling. On a scheduled_event's release time, poll Finnhub's
economic calendar every RELEASE_POLL_INTERVAL_S until `actual` populates for
the matching event, or RELEASE_POLL_MAX_S elapses. See SPEC.md.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import date as date_type

import httpx

import config
from schema import ScheduledEvent

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(s: str) -> set[str]:
    return set(_TOKEN_RE.findall(s.lower()))


def event_matches(entry_event: str, target_release: str) -> bool:
    """True if entry_event plausibly refers to the same release as
    target_release — at least half of target_release's significant words
    must appear in entry_event."""
    entry_tokens = _tokenize(entry_event)
    target_tokens = _tokenize(target_release)
    if not entry_tokens or not target_tokens:
        return False
    overlap = entry_tokens & target_tokens
    return len(overlap) >= math.ceil(len(target_tokens) / 2)


def _try_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().rstrip("%")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compute_surprise(actual: object, forecast: object) -> float | str | None:
    actual_f = _try_float(actual)
    forecast_f = _try_float(forecast)
    if actual_f is not None and forecast_f is not None:
        return round(actual_f - forecast_f, 4)
    return None


def find_matching_entry(entries: list[dict], release_name: str) -> dict | None:
    for entry in entries:
        if event_matches(str(entry.get("event", "")), release_name):
            return entry
    return None


async def fetch_calendar(
    client: httpx.AsyncClient, date_from: date_type, date_to: date_type
) -> list[dict]:
    response = await client.get(
        config.FINNHUB_CALENDAR_URL,
        params={
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "token": config.FINNHUB_API_KEY,
        },
    )
    response.raise_for_status()
    data = response.json()
    return data.get("economicCalendar", [])


def _to_result(scheduled_event: ScheduledEvent, entry: dict) -> dict:
    actual = entry.get("actual")
    forecast = entry.get("estimate")
    return {
        "release": scheduled_event.release,
        "actual": actual,
        "forecast": forecast,
        "previous": entry.get("prev"),
        "surprise": compute_surprise(actual, forecast),
    }


async def poll(
    scheduled_event: ScheduledEvent,
    plan_date: date_type,
    *,
    client: httpx.AsyncClient | None = None,
    interval_s: float | None = None,
    max_wait_s: float | None = None,
) -> dict | None:
    """Poll until `actual` is populated for the matching event, or time out.

    Returns the release result dict on success, or None on timeout (caller
    alerts "release data unavailable, plan unchanged" and does not replan).
    """
    interval_s = config.RELEASE_POLL_INTERVAL_S if interval_s is None else interval_s
    max_wait_s = config.RELEASE_POLL_MAX_S if max_wait_s is None else max_wait_s

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        elapsed = 0.0
        while True:
            try:
                entries = await fetch_calendar(client, plan_date, plan_date)
                entry = find_matching_entry(entries, scheduled_event.release)
                if entry is not None and entry.get("actual") not in (None, ""):
                    return _to_result(scheduled_event, entry)
            except httpx.HTTPError:
                pass  # transient network error — keep polling until max_wait_s

            if elapsed >= max_wait_s:
                return None
            await asyncio.sleep(interval_s)
            elapsed += interval_s
    finally:
        if owns_client:
            await client.aclose()
