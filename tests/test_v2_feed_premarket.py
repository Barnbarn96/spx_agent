"""Pre-market / overnight Moomoo quote rows still describe the previous
regular session (confirmed live 2026-11-24 07:31 ET): last_price is that
session's close, prev_close_price the one before, and the live price is in
pre_price / overnight_price. The feed must read them that way, so the SPX
estimate is right before the open and nothing jumps at 09:30."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from feed import MoomooFeed

ET = ZoneInfo("America/New_York")
SPX_PREV = 7237.17  # Mon Nov 23 SPX close
SPY_MON = 722.29  # Mon Nov 23 SPY close
SPY_FRI = 689.81  # Fri Nov 20 SPY close


def stale_row(pre=714.94, overnight=720.32):
    """The row exactly as OpenD returned it at 07:31 ET on Nov 24."""
    return {
        "data_date": "2026-11-23",
        "data_time": "16:00:00",
        "last_price": SPY_MON,
        "prev_close_price": SPY_FRI,
        "pre_price": pre,
        "after_price": 724.18,
        "overnight_price": overnight,
    }


def regular_row(price, hhmm, prev_close=SPY_MON):
    return {
        "data_date": "2026-11-24",
        "data_time": f"{hhmm}:00",
        "last_price": price,
        "prev_close_price": prev_close,
        "pre_price": 714.94,
        "after_price": 724.18,
        "overnight_price": 720.32,
    }


def feed_at(hh, mm, day=24):
    feed = MoomooFeed(initial_ratio=10.0)
    feed._clock = lambda: _dt.datetime(2026, 11, day, hh, mm, tzinfo=ET)
    feed.calibrate_from_prev_close(SPX_PREV)
    return feed


def test_premarket_row_uses_pre_price_and_last_close_for_ratio():
    feed = feed_at(7, 31)
    t = feed._to_tick(stale_row())
    assert feed.ratio == pytest.approx(SPX_PREV / SPY_MON)
    assert t.price == pytest.approx(714.94 * SPX_PREV / SPY_MON)  # ~7163.5, not ~7578
    assert t.ts == _dt.datetime(2026, 11, 24, 7, 31, tzinfo=ET)


def test_works_on_a_pandas_row_too():
    feed = feed_at(7, 31)
    t = feed._to_tick(pd.Series(stale_row()))
    assert t.price == pytest.approx(714.94 * SPX_PREV / SPY_MON)


def test_overnight_row_uses_overnight_price():
    feed = feed_at(2, 0)
    t = feed._to_tick(stale_row())
    assert t.price == pytest.approx(720.32 * SPX_PREV / SPY_MON)


def test_missing_premarket_price_gives_no_tick_but_sets_ratio():
    feed = feed_at(7, 31)
    assert feed._to_tick(stale_row(pre="N/A")) is None
    assert feed.ratio == pytest.approx(SPX_PREV / SPY_MON)


def test_no_ratio_jump_or_hold_at_the_open():
    feed = feed_at(9, 29)
    feed._to_tick(stale_row())
    before = feed.ratio
    feed._clock = lambda: _dt.datetime(2026, 11, 24, 9, 30, 5, tzinfo=ET)
    t = feed._to_tick(regular_row(715.10, "09:30"))
    assert feed.ratio == before
    assert feed.ratio_note == ""
    assert t.price == pytest.approx(715.10 * SPX_PREV / SPY_MON)


def test_regular_row_today_is_unchanged():
    feed = feed_at(10, 0)
    t = feed._to_tick(regular_row(716.0, "10:00"))
    assert feed.ratio == pytest.approx(SPX_PREV / SPY_MON)
    assert t.price == pytest.approx(716.0 * SPX_PREV / SPY_MON)
    assert t.ts == _dt.datetime(2026, 11, 24, 10, 0, tzinfo=ET)


def test_ex_dividend_day_premarket():
    feed = feed_at(7, 31)
    feed.set_spy_dividend(_dt.date(2026, 11, 24), 1.85)
    feed._to_tick(stale_row())
    assert feed.ratio == pytest.approx(SPX_PREV / (SPY_MON - 1.85))
