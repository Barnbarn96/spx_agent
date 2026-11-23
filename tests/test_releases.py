from datetime import date

import pytest

import releases
from schema import ScheduledEvent


def test_event_matches_exact():
    assert releases.event_matches("ISM Manufacturing PMI", "ISM Manufacturing PMI") is True


def test_event_matches_partial_overlap():
    assert releases.event_matches("Manufacturing PMI", "ISM Manufacturing PMI") is True


def test_event_matches_case_insensitive():
    assert releases.event_matches("ism manufacturing pmi", "ISM Manufacturing PMI") is True


def test_event_matches_unrelated_release():
    assert releases.event_matches("Nonfarm Payrolls", "ISM Manufacturing PMI") is False


def test_event_matches_empty_strings():
    assert releases.event_matches("", "ISM Manufacturing PMI") is False
    assert releases.event_matches("ISM Manufacturing PMI", "") is False


@pytest.mark.parametrize(
    "actual,forecast,expected",
    [
        (49.5, 49.2, pytest.approx(0.3)),
        ("49.5", "49.2", pytest.approx(0.3)),
        ("3.1%", "3.0%", pytest.approx(0.1)),
        (None, 49.2, None),
        (49.5, None, None),
        ("n/a", "49.2", None),
    ],
)
def test_compute_surprise(actual, forecast, expected):
    result = releases.compute_surprise(actual, forecast)
    if expected is None:
        assert result is None
    else:
        assert result == expected


def test_find_matching_entry_returns_first_match():
    entries = [
        {"event": "Nonfarm Payrolls", "actual": 200},
        {"event": "ISM Manufacturing PMI", "actual": 49.5},
    ]
    match = releases.find_matching_entry(entries, "ISM Manufacturing PMI")
    assert match is not None
    assert match["actual"] == 49.5


def test_find_matching_entry_no_match():
    entries = [{"event": "Nonfarm Payrolls", "actual": 200}]
    assert releases.find_matching_entry(entries, "ISM Manufacturing PMI") is None


def _mk_scheduled_event() -> ScheduledEvent:
    return ScheduledEvent(
        time="10:00", release="ISM Manufacturing PMI", source_id="finnhub:ism-mfg", replan=True
    )


async def test_poll_returns_result_once_actual_populates(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_calendar(client, date_from, date_to):
        calls["n"] += 1
        if calls["n"] < 2:
            return [{"event": "ISM Manufacturing PMI", "actual": None, "estimate": 49.2, "prev": 48.7}]
        return [{"event": "ISM Manufacturing PMI", "actual": 49.5, "estimate": 49.2, "prev": 48.7}]

    monkeypatch.setattr(releases, "fetch_calendar", fake_fetch_calendar)

    result = await releases.poll(
        _mk_scheduled_event(), date(2026, 7, 21), interval_s=0.01, max_wait_s=1.0
    )
    assert result is not None
    assert result["actual"] == 49.5
    assert result["forecast"] == 49.2
    assert result["surprise"] == pytest.approx(0.3)
    assert calls["n"] >= 2


async def test_poll_times_out_when_actual_never_populates(monkeypatch):
    async def fake_fetch_calendar(client, date_from, date_to):
        return [{"event": "ISM Manufacturing PMI", "actual": None, "estimate": 49.2, "prev": 48.7}]

    monkeypatch.setattr(releases, "fetch_calendar", fake_fetch_calendar)

    result = await releases.poll(
        _mk_scheduled_event(), date(2026, 7, 21), interval_s=0.01, max_wait_s=0.05
    )
    assert result is None
