"""Pydantic v2 models for the daily plan and runtime engine I/O.

All wall-clock time strings in the plan ("HH:MM") are ET and are resolved
against `America/New_York` for the plan's `date` — never against local
system time.
"""

from __future__ import annotations

import re
from datetime import date as date_type
from datetime import datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(value: str) -> str:
    if not HHMM_RE.match(value):
        raise ValueError(f"time must be HH:MM (24h ET), got {value!r}")
    return value


# --------------------------------------------------------------------------
# Plan schema
# --------------------------------------------------------------------------


class Condition(BaseModel):
    """All fields optional and ANDed together."""

    model_config = ConfigDict(extra="forbid")

    price_between: tuple[float, float] | None = None
    price_above: float | None = None
    price_below: float | None = None
    yields_10y: Literal["falling", "rising"] | None = None
    # v2: level checks on the 10-year yield, in percent (e.g. 4.95). These
    # work with sparse manual input (state/yield.json), unlike yields_10y's
    # rolling-window direction which needs a live yield stream.
    yield_below: float | None = None
    yield_above: float | None = None
    not_before: str | None = None
    not_after: str | None = None
    requires_not_filled: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_fields(self) -> "Condition":
        if self.price_between is not None:
            low, high = self.price_between
            if not low < high:
                raise ValueError(
                    f"price_between low must be < high, got ({low}, {high})"
                )
        if self.not_before is not None:
            _validate_hhmm(self.not_before)
        if self.not_after is not None:
            _validate_hhmm(self.not_after)
        for name in ("yield_below", "yield_above"):
            value = getattr(self, name)
            if value is not None and not 0.5 <= value <= 15:
                raise ValueError(f"{name} must be a yield in percent (e.g. 4.95), got {value}")
        if (
            self.yield_below is not None
            and self.yield_above is not None
            and not self.yield_above < self.yield_below
        ):
            raise ValueError(
                f"yield_above ({self.yield_above}) must be < yield_below ({self.yield_below}) "
                "when both are set"
            )
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument: str
    size_pct: float = Field(gt=0, le=100)


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: float
    scale_out_pct: float = Field(gt=0, le=100)


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    side: Literal["long", "short"]
    condition: Condition
    action: Action
    targets: list[Target] = Field(min_length=1)
    stop: float
    one_shot: bool = True

    @model_validator(mode="after")
    def _check_targets_sum(self) -> "Trigger":
        total = sum(t.scale_out_pct for t in self.targets)
        if abs(total - 100.0) > 1.0:
            raise ValueError(
                f"trigger {self.id!r}: targets scale_out_pct must sum to "
                f"100 +/- 1, got {total}"
            )
        return self

    @model_validator(mode="after")
    def _check_stop_side(self) -> "Trigger":
        cond = self.condition
        if self.side == "long":
            low = None
            if cond.price_between is not None:
                low = cond.price_between[0]
            elif cond.price_above is not None:
                low = cond.price_above
            if low is not None and not self.stop < low:
                raise ValueError(
                    f"trigger {self.id!r}: long stop ({self.stop}) must be "
                    f"below entry zone low ({low})"
                )
        else:  # short
            high = None
            if cond.price_between is not None:
                high = cond.price_between[1]
            elif cond.price_below is not None:
                high = cond.price_below
            if high is not None and not self.stop > high:
                raise ValueError(
                    f"trigger {self.id!r}: short stop ({self.stop}) must be "
                    f"above entry zone high ({high})"
                )
        return self


class Invalidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    below: float | None = None
    above: float | None = None
    action: str

    @model_validator(mode="after")
    def _check_at_least_one(self) -> "Invalidation":
        if self.below is None and self.above is None:
            raise ValueError("invalidation requires at least one of below/above")
        return self


class ScheduledEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: str
    release: str
    source_id: str
    replan: bool = True

    @model_validator(mode="after")
    def _check_time(self) -> "ScheduledEvent":
        _validate_hhmm(self.time)
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date_type
    generated_at: datetime
    bias: Literal["bullish", "bearish", "neutral"]
    spx_reference: float
    # v2: where spx_reference came from, e.g. "spreadex 7250 @ 09:27 ET" or
    # "mes-derived". Informational only.
    spx_reference_source: str = ""
    # v2: the previous session's official SPX close. When set, the Moomoo
    # feed calibrates its SPY->SPX ratio as spx_prev_close / SPY's previous
    # close (two official 4 PM prints) instead of against spx_reference.
    spx_prev_close: float | None = None
    triggers: list[Trigger] = Field(min_length=1)
    invalidation: Invalidation
    flat_by: str
    scheduled_events: list[ScheduledEvent] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _check_flat_by(self) -> "Plan":
        _validate_hhmm(self.flat_by)
        return self

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "Plan":
        ids = [t.id for t in self.triggers]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate trigger ids: {sorted(dupes)}")
        return self

    @model_validator(mode="after")
    def _check_requires_not_filled_refs(self) -> "Plan":
        ids = {t.id for t in self.triggers}
        for t in self.triggers:
            for ref in t.condition.requires_not_filled:
                if ref not in ids:
                    raise ValueError(
                        f"trigger {t.id!r}: requires_not_filled references "
                        f"unknown trigger id {ref!r}"
                    )
                if ref == t.id:
                    raise ValueError(
                        f"trigger {t.id!r}: requires_not_filled cannot "
                        f"reference itself"
                    )
        return self


# --------------------------------------------------------------------------
# Runtime models
# --------------------------------------------------------------------------


class Tick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    price: float
    yield_10y: float | None = None


class Decision(BaseModel):
    """Output of agent.decide(). Gates a pre-planned action; never carries
    new levels, sizes, or instruments — those come only from the Plan."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["EXECUTE", "SKIP", "WAIT"]
    reasoning: str
    confidence: int = Field(ge=0, le=100)


class TriggerFired(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["trigger_fired"] = "trigger_fired"
    trigger_id: str
    tick: Tick


class TargetHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["target_hit"] = "target_hit"
    trigger_id: str
    level: float
    scale_out_pct: float
    tick: Tick


class StopHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["stop_hit"] = "stop_hit"
    trigger_id: str
    tick: Tick


class Invalidated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["invalidated"] = "invalidated"
    level: float
    tick: Tick


class FlatByReached(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["flat_by_reached"] = "flat_by_reached"
    tick: Tick


class ReviewRequired(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["review_required"] = "review_required"
    trigger_id: str
    reason: str
    tick: Tick | None = None


class AlreadyInZone(BaseModel):
    """v2: the first time the engine sees a trigger (startup, a new plan, or
    a replan adding it), price was already inside its zone. The trigger
    does NOT fire — it waits for price to leave and re-enter — but the
    operator is told, since the setup may already be in play."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["already_in_zone"] = "already_in_zone"
    trigger_id: str
    tick: Tick


class ZoneSkipped(BaseModel):
    """v2: price moved clean across a trigger's price_between zone between
    two ticks without printing inside it. The trigger does NOT fire — the
    current price is outside the planned entry zone, and firing there would
    mean chasing (e.g. a data spike through a buy-the-dip zone) — but the
    operator is told, and a later move back into the zone still fires."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["zone_skipped"] = "zone_skipped"
    trigger_id: str
    from_price: float
    tick: Tick


class EngineWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["warning"] = "warning"
    message: str
    trigger_id: str | None = None
    tick: Tick | None = None


EngineEvent = Union[
    TriggerFired,
    TargetHit,
    StopHit,
    Invalidated,
    FlatByReached,
    ReviewRequired,
    AlreadyInZone,
    ZoneSkipped,
    EngineWarning,
]
