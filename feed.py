"""Market data feed: async tick stream, abstracted behind a small interface
so Polygon, Moomoo, and replay ticks (--sim) are interchangeable to main.py.

NOTE: PolygonFeed is implemented against Polygon.io's documented indices
websocket protocol but has not been exercised against a live connection in
this environment (no configured POLYGON_API_KEY, and market-data messages
only flow during RTH). Treat it as unverified until run live — see
docs/v1/ROLLOUT_PLAN.md.

NOTE: MoomooFeed estimates SPX from SPY (the ETF), not the index directly —
confirmed live that Moomoo's OpenAPI rejects subscribing to US indices
outright ("US stock indices are not supported", not a code-string issue).
SPY subscription and push data were confirmed live. The SPY->SPX ratio is
calibrated against a known-good SPX reference (see MoomooFeed.calibrate),
not hardcoded, since the ratio drifts slowly over time. Quote timestamps
(data_date/data_time, confirmed to include fractional seconds live) are
assumed to already be ET wall-clock time. Requires the Moomoo desktop app
running, logged in, with OpenD running locally — see docs/v1/ROLLOUT_PLAN.md.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import websockets

import config
from schema import Tick

ET = ZoneInfo("America/New_York")


@dataclass
class FeedStale:
    """Dead-man rule: no tick received for FEED_STALE_S during RTH."""

    ts: datetime
    seconds_since_last_tick: float


class TickSource(ABC):
    @abstractmethod
    def stream(self) -> AsyncIterator[Tick]:
        """Yield ticks indefinitely. Implementations own their own
        reconnect/retry policy; callers just iterate."""
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator signature


class PolygonFeed(TickSource):
    """SPX + 10Y yield over Polygon's indices websocket. Carries the
    last-known yield forward onto SPX ticks per SPEC.md."""

    def __init__(
        self,
        api_key: str = config.POLYGON_API_KEY,
        ws_url: str = config.POLYGON_WS_URL,
        spx_symbol: str = config.POLYGON_SPX_SYMBOL,
        yield_symbol: str = config.POLYGON_YIELD_SYMBOL,
    ) -> None:
        self.api_key = api_key
        self.ws_url = ws_url
        self.spx_symbol = spx_symbol
        self.yield_symbol = yield_symbol

    @staticmethod
    def _parse_ts(raw_ms: float | int | None) -> datetime:
        if raw_ms is None:
            return datetime.now(ET)
        return datetime.fromtimestamp(raw_ms / 1000, tz=timezone.utc).astimezone(ET)

    async def stream(self) -> AsyncIterator[Tick]:
        backoff = config.FEED_RECONNECT_MIN_S
        last_yield: float | None = None

        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    await ws.recv()  # initial "connected" status frame
                    await ws.send(json.dumps({"action": "auth", "params": self.api_key}))
                    await ws.recv()  # auth_success / auth_failed status frame

                    await ws.send(
                        json.dumps(
                            {
                                "action": "subscribe",
                                "params": f"A.{self.spx_symbol},A.{self.yield_symbol}",
                            }
                        )
                    )
                    backoff = config.FEED_RECONNECT_MIN_S  # reset after a clean connect

                    async for raw in ws:
                        for msg in json.loads(raw):
                            sym = msg.get("sym")
                            price = msg.get("c")
                            if price is None or sym is None:
                                continue
                            ts = self._parse_ts(msg.get("s") or msg.get("e"))

                            if sym == self.yield_symbol:
                                last_yield = float(price)
                                continue
                            if sym == self.spx_symbol:
                                yield Tick(ts=ts, price=float(price), yield_10y=last_yield)
            except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, config.FEED_RECONNECT_MAX_S)
                continue


def _in_opening_window(now_et: datetime) -> bool:
    """09:30:00-09:34:59 ET: the opening prints, when SPY is at its noisiest."""
    return (now_et.hour, now_et.minute) >= (9, 30) and (now_et.hour, now_et.minute) < (9, 35)


_EXTENDED_FIELDS = ("pre_price", "after_price", "overnight_price")


def _parse_moomoo_ts(data_date: str, data_time: str) -> datetime:
    combined = f"{data_date} {data_time}"
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(combined, fmt).replace(tzinfo=ET)
        except ValueError:
            continue
    return datetime.now(ET)


class MoomooFeed(TickSource):
    """SPX estimated from a local Moomoo (Futu) OpenD gateway, via SPY (the
    ETF) scaled by a calibrated ratio — Moomoo's OpenAPI does not support
    subscribing to US indices directly ("US stock indices are not
    supported", confirmed live; not a code-string issue). No 10Y yield
    support — see the module docstring. Requires OpenD running and logged in.

    The SPY->SPX ratio isn't fixed (SPY drifts slowly from SPX/10 over time
    from dividend/compounding effects), so it's calibrated against a
    known-good SPX reference price rather than hardcoded. Call calibrate()
    whenever one becomes available — main.py does this automatically from
    each loaded plan's spx_reference, so the ratio refreshes on every new
    plan rather than drifting for the life of a long-running process.
    """

    def __init__(
        self,
        host: str = config.MOOMOO_HOST,
        port: int = config.MOOMOO_PORT,
        spy_code: str = config.MOOMOO_SPY_CODE,
        initial_ratio: float = config.MOOMOO_SPY_SPX_RATIO,
    ) -> None:
        if importlib.util.find_spec("moomoo") is None:
            raise ImportError("MoomooFeed requires the moomoo-api package: pip install moomoo-api")
        self.host = host
        self.port = port
        self.spy_code = spy_code
        self.ratio = initial_ratio
        self.ratio_source = "initial guess (MOOMOO_SPY_SPX_RATIO)"
        self._last_spy_price: float | None = None
        self._pending_spx_reference: float | None = None
        # v2 close-to-close calibration: SPX prev close (from the plan) and
        # SPY prev close (from Moomoo's own quote rows).
        self._spx_prev_close: float | None = None
        self._spy_prev_close: float | None = None
        # v2: no ratio changes 09:30-09:35 ET once calibrated; a change asked
        # for in that window is held and applied at 09:35.
        self._calibrated = False
        self._deferred: tuple[float, str] | None = None
        self._last_ts: datetime | None = None
        self.ratio_note = ""
        # wall clock, replaceable in tests; only used to spot quote rows
        # left over from the previous session (pre-market / overnight)
        self._clock = lambda: datetime.now(ET)
        # v2: (ex-dividend date, dollars) for SPY ex-dividend days
        self.spy_dividend: tuple[date_type, float] | None = None
        if config.SPY_DIVIDEND_DATE and config.SPY_DIVIDEND_USD:
            self.set_spy_dividend(date_type.fromisoformat(config.SPY_DIVIDEND_DATE), config.SPY_DIVIDEND_USD)

    def set_spy_dividend(self, ex_date: date_type, usd: float) -> None:
        """On ex_date only, SPY's previous close is reduced by usd before the
        ratio is computed: SPY opens about one dividend lower while SPX
        doesn't move. Assumes Moomoo's prev_close_price is the unadjusted
        close (unverified, see V2_STATUS)."""
        if not 0 < usd < 10:
            raise ValueError(f"SPY dividend must be between 0 and 10 dollars, got {usd!r}")
        self.spy_dividend = (ex_date, usd)
        self._apply_prev_close_ratio()

    def _now(self) -> datetime:
        return self._last_ts or self._clock()

    def _stale_session_row(self, row) -> tuple[datetime, float | None, float] | None:
        """If the quote row is dated before today (ET) and carries an
        extended-hours price field, return (now, live SPY price, previous
        close) read the extended-hours way; otherwise None (regular row).
        The live price is pre_price from 04:00 ET (also used if the row is
        still stale after 09:30, before the first regular print), else
        overnight_price. A missing/N/A live price gives None for it."""
        if not any(k in row for k in _EXTENDED_FIELDS):
            return None
        now = self._clock().astimezone(ET)
        try:
            row_date = date_type.fromisoformat(str(row.get("data_date", ""))[:10])
        except ValueError:
            return None
        if row_date >= now.date():
            return None
        try:
            prev_close = float(row.get("last_price") or 0)
        except (TypeError, ValueError):
            prev_close = 0.0
        field = "pre_price" if now.hour >= 4 and now.hour < 20 else "overnight_price"
        try:
            live = float(row.get(field))
        except (TypeError, ValueError):
            live = None
        return now, (live if live and live > 0 else None), prev_close

    def _set_ratio(self, ratio: float, source: str) -> None:
        now = self._now().astimezone(ET)
        if self._calibrated and _in_opening_window(now):
            self._deferred = (ratio, source)
            self.ratio_note = f"new ratio {ratio:.4f} ({source}) held until 09:35 ET"
            return
        self.ratio = ratio
        self.ratio_source = source
        self._calibrated = True
        self._deferred = None
        self.ratio_note = ""

    def calibrate(self, spx_reference: float) -> None:
        """v1 fallback: recompute the SPY->SPX ratio against a live SPX
        estimate (plan.spx_reference) and whatever SPY last printed. Less
        accurate than calibrate_from_prev_close() — the two prices come from
        different moments and pre-market SPY is thin — so it is ignored once
        a close-to-close calibration is available."""
        if self._spx_prev_close is not None:
            return
        if self._last_spy_price:
            self._set_ratio(
                spx_reference / self._last_spy_price,
                f"spx_reference {spx_reference:g} / live SPY {self._last_spy_price:g}",
            )
        else:
            self._pending_spx_reference = spx_reference

    def calibrate_from_prev_close(self, spx_prev_close: float) -> None:
        """Preferred: ratio = previous SPX close / previous SPY close, both
        official 4 PM prints. SPY's previous close is read from Moomoo's
        quote rows (prev_close_price), so the ratio is applied as soon as
        one arrives. Replans that keep the same spx_prev_close leave it
        unchanged.

        SPY ex-dividend days: set the dividend (set_spy_dividend, or
        SPY_DIVIDEND_DATE/SPY_DIVIDEND_USD, or main.py --spy-dividend) and it
        is taken off SPY's previous close on that day."""
        self._spx_prev_close = spx_prev_close
        self._pending_spx_reference = None
        self._apply_prev_close_ratio()

    def _apply_prev_close_ratio(self) -> None:
        if self._spx_prev_close and self._spy_prev_close:
            spy_prev, note = self._spy_prev_close, ""
            if self.spy_dividend is not None and self.spy_dividend[0] == self._now().astimezone(ET).date():
                spy_prev -= self.spy_dividend[1]
                note = f" (ex-div -{self.spy_dividend[1]:g})"
            self._set_ratio(
                self._spx_prev_close / spy_prev,
                f"prev closes: SPX {self._spx_prev_close:g} / SPY {self._spy_prev_close:g}{note}",
            )

    def _to_tick(self, row) -> Tick | None:
        stale = self._stale_session_row(row)
        if stale is not None:
            # Pre-market / overnight: the row still describes the last
            # regular session. Its last_price is that session's close (the
            # previous close for today's ratio) and the live price is in
            # the extended-hours field. Confirmed live 2026-11-24 07:31 ET:
            # data_date 11-23 16:00, last 722.29 (Mon close), prev_close
            # 689.81 (Fri close), pre_price 714.94.
            ts, spy_price, prev_close = stale
        else:
            try:
                spy_price = float(row["last_price"])
            except (KeyError, TypeError, ValueError):
                return None
            ts = _parse_moomoo_ts(str(row.get("data_date", "")), str(row.get("data_time", "")))
            try:
                prev_close = float(row.get("prev_close_price") or 0)
            except (TypeError, ValueError):
                prev_close = 0.0
        if spy_price is None or spy_price <= 0:
            # still record the previous close so the ratio is ready
            if prev_close > 0 and prev_close != self._spy_prev_close:
                self._spy_prev_close = prev_close
                self._apply_prev_close_ratio()
            return None
        self._last_ts = ts
        if self._deferred is not None and not _in_opening_window(ts.astimezone(ET)):
            self._set_ratio(*self._deferred)

        self._last_spy_price = spy_price
        if prev_close > 0 and prev_close != self._spy_prev_close:
            self._spy_prev_close = prev_close
            self._apply_prev_close_ratio()
        if self._pending_spx_reference is not None and self._spx_prev_close is None:
            self._set_ratio(
                self._pending_spx_reference / spy_price,
                f"spx_reference {self._pending_spx_reference:g} / live SPY {spy_price:g}",
            )
            self._pending_spx_reference = None

        # yield_10y is always None — Futu/moomoo's OpenAPI covers equities/
        # index/options/futures quotes, not treasury yields. Conditions
        # requiring yields_10y simply never fire (triggers.py fails closed
        # on missing yield data), which is the existing, already-designed-
        # for behavior.
        return Tick(ts=ts, price=spy_price * self.ratio, yield_10y=None)

    async def stream(self) -> AsyncIterator[Tick]:
        from moomoo import RET_OK, OpenQuoteContext, Session, StockQuoteHandlerBase, SubType

        loop = asyncio.get_event_loop()
        backoff = config.FEED_RECONNECT_MIN_S
        outer = self

        while True:
            tick_queue: asyncio.Queue[Tick] = asyncio.Queue()

            class _Handler(StockQuoteHandlerBase):
                def on_recv_rsp(self, rsp_pb):
                    ret_code, content = super().on_recv_rsp(rsp_pb)
                    if ret_code != RET_OK:
                        return ret_code, content
                    for _, row in content.iterrows():
                        tick = outer._to_tick(row)
                        if tick is not None:
                            loop.call_soon_threadsafe(tick_queue.put_nowait, tick)
                    return RET_OK, content

            ctx = None
            try:
                ctx = OpenQuoteContext(host=self.host, port=self.port)
                ctx.set_handler(_Handler())
                # extended_time/session=ALL: without these, subscribe only
                # delivers regular-hours (9:30-16:00 ET) prints — confirmed
                # live this silently produces nothing new pre-market (just
                # the prior day's close, repeated), which is no good for a
                # feed meant to be flowing before the 08:00 plan generation.
                ret, err = ctx.subscribe(
                    [self.spy_code], [SubType.QUOTE], extended_time=True, session=Session.ALL
                )
                if ret != RET_OK:
                    raise RuntimeError(f"moomoo subscribe failed: {err}")

                backoff = config.FEED_RECONNECT_MIN_S
                while True:
                    # moomoo-api doesn't surface an explicit disconnect
                    # callback we've confirmed, so a stalled queue (rather
                    # than only a raised exception) is also treated as a
                    # reason to reconnect.
                    try:
                        tick = await asyncio.wait_for(
                            tick_queue.get(), timeout=config.FEED_STALE_S * 2
                        )
                    except asyncio.TimeoutError as e:
                        raise RuntimeError("no ticks from moomoo — reconnecting") from e
                    yield tick
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, config.FEED_RECONNECT_MAX_S)
                continue
            finally:
                if ctx is not None:
                    ctx.close()


class ReplayTickSource(TickSource):
    """Wraps a pre-loaded list of ticks (from replay.load_ticks) as a
    TickSource, for --sim mode. No reconnect/dead-man logic — it's a fixed
    historical sequence, not a live connection."""

    def __init__(self, ticks: list[Tick], speed: str = "0") -> None:
        self.ticks = ticks
        self.speed = speed

    async def stream(self) -> AsyncIterator[Tick]:
        speed_multiplier = None if self.speed == "0" else float(self.speed.rstrip("xX"))
        prev_ts: datetime | None = None
        for tick in self.ticks:
            if speed_multiplier is not None and prev_ts is not None:
                real_gap = (tick.ts - prev_ts).total_seconds()
                sleep_for = max(0.0, real_gap / speed_multiplier)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            prev_ts = tick.ts
            yield tick


def _is_rth(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et <= close_t


class FeedManager:
    """Wraps a live TickSource with dead-man detection: if no tick arrives
    for FEED_STALE_S during RTH, emits a FeedStale alongside the tick
    stream. Intended for PolygonFeed, not ReplayTickSource."""

    def __init__(self, source: TickSource, stale_after_s: float = config.FEED_STALE_S) -> None:
        self.source = source
        self.stale_after_s = stale_after_s

    async def watch(self) -> AsyncIterator[Tick | FeedStale]:
        queue: asyncio.Queue[Tick | FeedStale] = asyncio.Queue()
        last_tick_at = asyncio.get_event_loop().time()
        stale_alerted = False

        async def _pump_ticks() -> None:
            nonlocal last_tick_at
            async for tick in self.source.stream():
                last_tick_at = asyncio.get_event_loop().time()
                await queue.put(tick)

        async def _watchdog() -> None:
            nonlocal stale_alerted
            while True:
                await asyncio.sleep(1.0)
                now_et = datetime.now(ET)
                elapsed = asyncio.get_event_loop().time() - last_tick_at
                if _is_rth(now_et) and elapsed > self.stale_after_s:
                    if not stale_alerted:
                        stale_alerted = True
                        await queue.put(FeedStale(ts=now_et, seconds_since_last_tick=elapsed))
                else:
                    stale_alerted = False

        pump_task = asyncio.ensure_future(_pump_ticks())
        watchdog_task = asyncio.ensure_future(_watchdog())
        try:
            while True:
                item = await queue.get()
                yield item
        finally:
            pump_task.cancel()
            watchdog_task.cancel()
