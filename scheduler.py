"""APScheduler jobs: pre-market plan generation, per-release polling,
entry-cutoff notice, EOD summary. Timezone America/New_York throughout. See
SPEC.md. Not used in --sim mode — main.py drives the loop directly from
replay ticks there."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from schema import Plan, ScheduledEvent

ET = ZoneInfo("America/New_York")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    hh, mm = hhmm.split(":")
    return int(hh), int(mm)


@dataclass
class SchedulerCallbacks:
    on_generate_plan: Callable[[], Awaitable[None]]
    on_release_poll: Callable[[ScheduledEvent], Awaitable[None]]
    on_entry_cutoff: Callable[[], Awaitable[None]]
    on_eod_summary: Callable[[], Awaitable[None]]


class AgentScheduler:
    def __init__(self, callbacks: SchedulerCallbacks) -> None:
        self.callbacks = callbacks
        self.scheduler = AsyncIOScheduler(timezone=ET)

    def start(self) -> None:
        gen_hh, gen_mm = _parse_hhmm(config.PLAN_GENERATION_TIME_ET)
        cutoff_hh, cutoff_mm = _parse_hhmm(config.ENTRY_CUTOFF_NOTICE_TIME_ET)
        eod_hh, eod_mm = _parse_hhmm(config.EOD_SUMMARY_TIME_ET)

        self.scheduler.add_job(
            self.callbacks.on_generate_plan,
            CronTrigger(day_of_week="mon-fri", hour=gen_hh, minute=gen_mm, timezone=ET),
            id="generate_plan",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.callbacks.on_entry_cutoff,
            CronTrigger(day_of_week="mon-fri", hour=cutoff_hh, minute=cutoff_mm, timezone=ET),
            id="entry_cutoff",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.callbacks.on_eod_summary,
            CronTrigger(day_of_week="mon-fri", hour=eod_hh, minute=eod_mm, timezone=ET),
            id="eod_summary",
            replace_existing=True,
        )
        self.scheduler.start()

    def schedule_release_jobs(self, plan: Plan) -> None:
        """Re-register per-release poll jobs for the current plan. Called
        whenever a plan is generated or replaced (fresh daily plan, or a
        replan that changes scheduled_events)."""
        for job in self.scheduler.get_jobs():
            if job.id.startswith("release_"):
                job.remove()

        for i, scheduled_event in enumerate(plan.scheduled_events):
            hh, mm = _parse_hhmm(scheduled_event.time)
            trigger = CronTrigger(day_of_week="mon-fri", hour=hh, minute=mm, second=5, timezone=ET)
            self.scheduler.add_job(
                self.callbacks.on_release_poll,
                trigger,
                args=[scheduled_event],
                id=f"release_{i}_{scheduled_event.source_id}",
                replace_existing=True,
            )

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
