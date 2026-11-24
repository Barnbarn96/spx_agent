"""v2 operator inputs that don't come from the price feed: the 10-year
yield (entered by hand) and GEX levels (read off Moomoo's Gamma Exposure
screen). Both live as small JSON files under state/ so that anything can
write them — the Claude Code /intraday command via tools/set_yield.py and
tools/set_gex.py, or the Discord bot — and the running main.py picks them
up by polling, the same way it hot-reloads the plan.

Every input carries its own timestamp and expires: an old value is treated
as missing, never as current.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

import config

ET = ZoneInfo("America/New_York")

YIELD_FILE = "yield.json"
GEX_FILE = "gex.json"


def normalize_yield(value: float) -> float:
    """Return the 10-year yield in percent. Cboe's TNX index is quoted at
    10x the yield (49.5 = 4.95%), so anything above 20 is scaled down."""
    if value > 20:
        value = value / 10
    if not 0.5 <= value <= 15:
        raise ValueError(f"10Y yield {value} is outside the plausible 0.5-15% range")
    return round(value, 4)


class YieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float  # percent, e.g. 4.93
    source: Literal["manual", "ocr", "feed"] = "manual"
    ts: datetime  # tz-aware

    @model_validator(mode="after")
    def _check(self) -> "YieldInput":
        if self.ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        self.value = normalize_yield(self.value)
        return self

    def age(self, now: datetime) -> timedelta:
        return now - self.ts

    def is_fresh(self, now: datetime, max_age_min: float | None = None) -> bool:
        limit = config.YIELD_MAX_AGE_MIN if max_age_min is None else max_age_min
        # -1 min tolerates small clock skew between writer and reader
        return -timedelta(minutes=1) <= self.age(now) <= timedelta(minutes=limit)


class GexInput(BaseModel):
    """Levels read off Moomoo's Options -> Analysis -> Gamma Exposure view
    for the 0DTE expiration."""

    model_config = ConfigDict(extra="forbid")

    expiry_date: date_type
    gamma_flip: float
    call_wall: float | None = None
    put_wall: float | None = None
    max_pain: float | None = None
    spx_in_shot: float | None = None  # the .SPX price shown in the screenshot
    as_of: datetime  # when the screenshot was taken (tz-aware)
    source: str = "moomoo-screenshot"
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> "GexInput":
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        for name in ("gamma_flip", "call_wall", "put_wall", "max_pain", "spx_in_shot"):
            v = getattr(self, name)
            if v is not None and not 1000 <= v <= 20000:
                raise ValueError(f"{name}={v} does not look like an SPX level")
        return self

    def regime(self, spx: float) -> str:
        return "negative gamma (moves tend to extend)" if spx < self.gamma_flip else (
            "positive gamma (moves tend to pin / mean-revert)"
        )

    def is_fresh(self, now: datetime, max_age_min: float | None = None) -> bool:
        limit = config.GEX_MAX_AGE_MIN if max_age_min is None else max_age_min
        return now - self.as_of <= timedelta(minutes=limit)

    def summary(self, spx: float | None, now: datetime) -> str:
        parts = [f"gamma flip {self.gamma_flip:g}"]
        if self.call_wall is not None:
            parts.append(f"call wall {self.call_wall:g}")
        if self.put_wall is not None:
            parts.append(f"put wall {self.put_wall:g}")
        if self.max_pain is not None:
            parts.append(f"max pain {self.max_pain:g} (low weight)")
        age_min = int((now - self.as_of).total_seconds() // 60)
        text = f"GEX ({self.expiry_date} expiry, {age_min} min old"
        text += ", STALE)" if not self.is_fresh(now) else ")"
        text += ": " + ", ".join(parts)
        if spx is not None:
            text += f". SPX {spx - self.gamma_flip:+.1f} vs flip -> {self.regime(spx)}"
        return text


def _state_path(name: str, state_dir: str | os.PathLike | None = None) -> Path:
    return Path(state_dir if state_dir is not None else config.STATE_DIR) / name


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so the poller never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_yield(value: float, *, source: str = "manual", ts: datetime | None = None,
                state_dir: str | os.PathLike | None = None) -> YieldInput:
    y = YieldInput(value=value, source=source, ts=ts or datetime.now(ET))
    atomic_write(_state_path(YIELD_FILE, state_dir), y.model_dump_json(indent=2))
    return y


def read_yield(state_dir: str | os.PathLike | None = None) -> YieldInput | None:
    path = _state_path(YIELD_FILE, state_dir)
    try:
        return YieldInput.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_gex(gex: GexInput, state_dir: str | os.PathLike | None = None) -> GexInput:
    atomic_write(_state_path(GEX_FILE, state_dir), gex.model_dump_json(indent=2))
    return gex


def read_gex(state_dir: str | os.PathLike | None = None) -> GexInput | None:
    path = _state_path(GEX_FILE, state_dir)
    try:
        return GexInput.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def file_mtime(name: str, state_dir: str | os.PathLike | None = None) -> float | None:
    try:
        return _state_path(name, state_dir).stat().st_mtime
    except OSError:
        return None
