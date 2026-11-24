"""v2 feed polish: the SPY->SPX ratio isn't changed between 09:30 and 09:35
ET once it's calibrated (changes wait until 09:35), and on a SPY
ex-dividend day the dividend is taken off SPY's previous close."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

import config
import main as main_mod
from feed import MoomooFeed

ET = ZoneInfo("America/New_York")
SPX_PREV = 7237.17
SPY_PREV = 719.40


def row(price, hhmm="10:00", prev_close=SPY_PREV, day="2026-11-24"):
    return {"last_price": price, "data_date": day, "data_time": f"{hhmm}:00", "prev_close_price": prev_close}


def calibrated_feed():
    feed = MoomooFeed(initial_ratio=10.0)
    feed.calibrate_from_prev_close(SPX_PREV)
    feed._to_tick(row(720.0, "09:20"))
    assert feed.ratio == pytest.approx(SPX_PREV / SPY_PREV)
    return feed


# -- opening freeze ----------------------------------------------------------------------


def test_new_prev_close_inside_the_window_waits_until_0935():
    feed = calibrated_feed()
    before = feed.ratio
    t = feed._to_tick(row(721.0, "09:31", prev_close=718.0))
    assert feed.ratio == before
    assert t.price == pytest.approx(721.0 * before)
    assert "held until 09:35" in feed.ratio_note

    feed._to_tick(row(721.5, "09:34", prev_close=718.0))
    assert feed.ratio == before

    t = feed._to_tick(row(722.0, "09:35", prev_close=718.0))
    assert feed.ratio == pytest.approx(SPX_PREV / 718.0)
    assert t.price == pytest.approx(722.0 * SPX_PREV / 718.0)
    assert feed.ratio_note == ""


def test_replan_calibration_inside_the_window_waits():
    feed = calibrated_feed()
    feed._to_tick(row(721.0, "09:32"))
    before = feed.ratio
    feed.calibrate_from_prev_close(7240.0)  # a replan with a different previous close
    assert feed.ratio == before
    feed._to_tick(row(721.0, "09:36"))
    assert feed.ratio == pytest.approx(7240.0 / SPY_PREV)


def test_first_calibration_is_never_held_back():
    """An uncalibrated ratio (the initial guess) is worse than a noisy one."""
    feed = MoomooFeed(initial_ratio=10.0)
    feed.calibrate_from_prev_close(SPX_PREV)
    feed._to_tick(row(720.0, "09:31"))
    assert feed.ratio == pytest.approx(SPX_PREV / SPY_PREV)


def test_outside_the_window_changes_apply_at_once():
    feed = calibrated_feed()
    feed._to_tick(row(721.0, "09:29", prev_close=718.0))
    assert feed.ratio == pytest.approx(SPX_PREV / 718.0)
    feed._to_tick(row(721.0, "09:36", prev_close=717.0))
    assert feed.ratio == pytest.approx(SPX_PREV / 717.0)


# -- SPY ex-dividend day --------------------------------------------------------------------


def test_dividend_is_taken_off_spy_prev_close_on_its_day():
    feed = MoomooFeed(initial_ratio=10.0)
    feed.set_spy_dividend(_dt.date(2026, 12, 18), 1.85)
    feed.calibrate_from_prev_close(SPX_PREV)
    feed._to_tick(row(718.0, "09:20", day="2026-12-18"))
    assert feed.ratio == pytest.approx(SPX_PREV / (SPY_PREV - 1.85))
    assert "ex-div -1.85" in feed.ratio_source


def test_dividend_is_ignored_on_other_days():
    feed = MoomooFeed(initial_ratio=10.0)
    feed.set_spy_dividend(_dt.date(2026, 12, 18), 1.85)
    feed.calibrate_from_prev_close(SPX_PREV)
    feed._to_tick(row(720.0, "09:20", day="2026-12-19"))
    assert feed.ratio == pytest.approx(SPX_PREV / SPY_PREV)
    assert "ex-div" not in feed.ratio_source


@pytest.mark.parametrize("bad", [-1.0, 50.0])
def test_dividend_must_be_plausible(bad):
    feed = MoomooFeed(initial_ratio=10.0)
    with pytest.raises(ValueError):
        feed.set_spy_dividend(_dt.date(2026, 12, 18), bad)


def test_dividend_settings_from_config(monkeypatch):
    monkeypatch.setattr(config, "SPY_DIVIDEND_DATE", "2026-12-18")
    monkeypatch.setattr(config, "SPY_DIVIDEND_USD", 1.85)
    feed = MoomooFeed(initial_ratio=10.0)
    assert feed.spy_dividend == (_dt.date(2026, 12, 18), 1.85)


def test_no_dividend_by_default():
    assert MoomooFeed(initial_ratio=10.0).spy_dividend is None


# -- main.py --spy-dividend ------------------------------------------------------------------


def test_cli_spy_dividend_is_for_today(monkeypatch):
    args = main_mod._build_cli().parse_args(["--spy-dividend", "1.85"])
    assert args.spy_dividend == 1.85
    feed = MoomooFeed(initial_ratio=10.0)
    main_mod.apply_spy_dividend_arg(feed, args.spy_dividend, today=_dt.date(2026, 12, 18))
    assert feed.spy_dividend == (_dt.date(2026, 12, 18), 1.85)


def test_cli_without_spy_dividend_changes_nothing():
    args = main_mod._build_cli().parse_args([])
    feed = MoomooFeed(initial_ratio=10.0)
    main_mod.apply_spy_dividend_arg(feed, args.spy_dividend, today=_dt.date(2026, 12, 18))
    assert feed.spy_dividend is None
