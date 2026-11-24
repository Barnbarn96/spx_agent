"""Hard risk guardrails, enforced as plain code — never delegated to the
LLM. Checked on every tick and before relaying any EXECUTE. See SPEC.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from triggers import TriggerEngine, TriggerStatus

ET = ZoneInfo("America/New_York")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    hh, mm = hhmm.split(":")
    return int(hh), int(mm)


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str | None = None


class RiskManager:
    def __init__(
        self,
        halt_path: str | Path,
        daily_loss_limit_usd: float,
        max_open_triggers: int,
        entry_cutoff_et: str = "15:00",
        max_stops_per_day: int = 2,
    ) -> None:
        self.halt_path = Path(halt_path)
        self.daily_loss_limit_usd = daily_loss_limit_usd
        self.max_open_triggers = max_open_triggers
        self.entry_cutoff_et = entry_cutoff_et
        self.max_stops_per_day = max_stops_per_day

        self._pnl_date: date_type | None = None
        self._daily_pnl_usd: float = 0.0
        self._stops_date: date_type | None = None
        self._stops_count: int = 0

    # -- HALT file --------------------------------------------------------

    def is_halted(self) -> bool:
        return self.halt_path.exists()

    def create_halt(self, reason: str) -> None:
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.halt_path.exists():
            self.halt_path.write_text(f"{datetime.now(ET).isoformat()} {reason}\n", encoding="utf-8")

    def clear_halt(self) -> None:
        """Operator-initiated only — never called automatically."""
        self.halt_path.unlink(missing_ok=True)

    # -- daily realized-loss limit -----------------------------------------

    def _roll_day_if_needed(self, now: datetime) -> None:
        today = now.astimezone(ET).date()
        if self._pnl_date != today:
            self._pnl_date = today
            self._daily_pnl_usd = 0.0

    def record_realized_pnl(self, amount_usd: float, now: datetime | None = None) -> None:
        now = now or datetime.now(ET)
        self._roll_day_if_needed(now)
        self._daily_pnl_usd += amount_usd
        if self._daily_pnl_usd <= -abs(self.daily_loss_limit_usd):
            self.create_halt(
                f"daily realized loss {self._daily_pnl_usd:.2f} breached "
                f"limit -{self.daily_loss_limit_usd:.2f}"
            )

    def daily_pnl(self, now: datetime | None = None) -> float:
        now = now or datetime.now(ET)
        self._roll_day_if_needed(now)
        return self._daily_pnl_usd

    # -- stops per day ("no re-entry after two stops") ----------------------

    def _roll_stops_if_needed(self, now: datetime) -> None:
        today = now.astimezone(ET).date()
        if self._stops_date != today:
            self._stops_date = today
            self._stops_count = 0

    def seed_stops(self, count: int, now: datetime) -> None:
        """Restore today's stop count after a restart (from the journal).
        Never creates the HALT itself: only the next stop does."""
        self._roll_stops_if_needed(now)
        self._stops_count = count

    def record_stop(self, now: datetime) -> bool:
        """Count a StopHit. Returns True if this stop is the one that
        reaches the limit and created the HALT (callers alert on that)."""
        self._roll_stops_if_needed(now)
        self._stops_count += 1
        if self._stops_count == self.max_stops_per_day:
            self.create_halt(f"two stops today ({self._stops_count}); no new entries")
            return True
        return False

    def stops_today(self, now: datetime) -> int:
        self._roll_stops_if_needed(now)
        return self._stops_count

    # -- entry cutoff / max open triggers ----------------------------------

    def entry_cutoff_reached(self, now: datetime) -> bool:
        cutoff_hh, cutoff_mm = _parse_hhmm(self.entry_cutoff_et)
        now_et = now.astimezone(ET)
        cutoff = now_et.replace(hour=cutoff_hh, minute=cutoff_mm, second=0, microsecond=0)
        return now_et >= cutoff

    def max_open_triggers_reached(self, engine: TriggerEngine) -> bool:
        open_count = sum(1 for s in engine.states.values() if s.status == TriggerStatus.OPEN)
        return open_count >= self.max_open_triggers

    # -- combined gate, checked before relaying any EXECUTE ------------------

    def gate_execute(self, engine: TriggerEngine, now: datetime) -> RiskCheckResult:
        if self.is_halted():
            return RiskCheckResult(False, "HALT file present")
        if self.entry_cutoff_reached(now):
            return RiskCheckResult(False, f"past hard entry cutoff {self.entry_cutoff_et} ET")
        if self.max_open_triggers_reached(engine):
            return RiskCheckResult(
                False, f"max open triggers ({self.max_open_triggers}) reached"
            )
        return RiskCheckResult(True)
