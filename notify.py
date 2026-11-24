"""Discord bot: alerts + inline confirm/skip/mark-filled buttons. Runs over
Discord's Gateway (a persistent outbound websocket, like Telegram's
long-polling) — no inbound ports, no public webhook endpoint. Every alert
carries price, time (ET + SGT), trigger id, and the agent's one-line
reasoning where applicable. See SPEC.md.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import discord

import config
from schema import (
    Decision,
    FlatByReached,
    Invalidated,
    Plan,
    ReviewRequired,
    StopHit,
    StopMoved,
    TargetHit,
    Tick,
    Trigger,
)
from risk import is_early
from triggers import TriggerEngine

ET = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")


def _fmt_times(ts: datetime) -> str:
    et = ts.astimezone(ET).strftime("%H:%M:%S")
    sgt = ts.astimezone(SGT).strftime("%H:%M:%S")
    return f"{et} ET / {sgt} SGT"


def _fmt_wait(plan: Plan) -> str:
    """The plan's soft opening wait in ET and SGT, e.g. "10:00 ET / 23:00 SGT"."""
    hh, mm = (int(x) for x in plan.no_entries_before.split(":"))
    et = datetime(plan.date.year, plan.date.month, plan.date.day, hh, mm, tzinfo=ET)
    return f"{et:%H:%M} ET / {et.astimezone(SGT):%H:%M} SGT"


EARLY_LABEL = "Early entry — override?"


class EarlyEntryModal(discord.ui.Modal):
    """Asks for a one-line reason before an entry inside the opening wait.
    Its submit goes back through Notifier.submit_early_override, which
    re-checks the trigger state and the risk gate."""

    def __init__(self, notifier: "Notifier", trigger_id: str, wait: str) -> None:
        super().__init__(title="Early entry override", timeout=300)
        self.notifier = notifier
        self.trigger_id = trigger_id
        self.reason = discord.ui.TextInput(
            label="Why enter before the wait?",
            placeholder=f"Wait is until {wait}. One line, e.g. reclaimed 7200 on volume",
            max_length=200,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.notifier.submit_early_override(interaction, self.trigger_id, self.reason.value)


def _fmt_price(p: float) -> str:
    return f"{p:,.2f}"


def _diff_plans(old_plan: Plan, new_plan: Plan) -> str:
    old_by_id = {t.id: t for t in old_plan.triggers}
    new_by_id = {t.id: t for t in new_plan.triggers}
    lines: list[str] = []

    if old_plan.bias != new_plan.bias:
        lines.append(f"bias: {old_plan.bias} -> {new_plan.bias}")

    for tid in sorted(set(old_by_id) | set(new_by_id)):
        old_t, new_t = old_by_id.get(tid), new_by_id.get(tid)
        if old_t is None:
            lines.append(f"+ {tid}: new trigger ({new_t.side}, zone={new_t.condition.price_between})")
        elif new_t is None:
            lines.append(f"- {tid}: removed")
        elif old_t.model_dump() != new_t.model_dump():
            changes = []
            if old_t.condition.price_between != new_t.condition.price_between:
                changes.append(f"zone {old_t.condition.price_between} -> {new_t.condition.price_between}")
            if old_t.stop != new_t.stop:
                changes.append(f"stop {old_t.stop} -> {new_t.stop}")
            if [t.level for t in old_t.targets] != [t.level for t in new_t.targets]:
                changes.append(
                    f"targets {[t.level for t in old_t.targets]} -> {[t.level for t in new_t.targets]}"
                )
            if changes:
                lines.append(f"~ {tid}: " + "; ".join(changes))

    if old_plan.invalidation.model_dump() != new_plan.invalidation.model_dump():
        lines.append(f"invalidation: {old_plan.invalidation} -> {new_plan.invalidation}")

    return "\n".join(lines) if lines else "(no material change)"


class Notifier:
    def __init__(
        self, bot_token: str, channel_id: int | str, engine: TriggerEngine | None, journal, risk=None
    ) -> None:
        self.bot_token = bot_token
        self.channel_id = int(channel_id)
        # Mutable: set at construction if a plan is already loaded, or
        # synced later by Orchestrator._load_plan() when one is. Never
        # snapshot this — the engine is replaced wholesale on every
        # load_plan(), and _on_interaction always needs the current one.
        self.engine = engine
        self.journal = journal
        # Real-time gate for the EXECUTE tap — checked at confirm-time, not
        # fire-time, so alerts keep flowing (e.g. past the entry cutoff)
        # without ever letting a blocked entry actually go through.
        self.risk = risk
        # trigger_id -> journal decisions.id, so a button tap can be logged
        # against the decision that produced it.
        self._pending_decision_id: dict[str, int] = {}
        # Clock for the EXECUTE-tap checks; tests replace it.
        self.now_fn = lambda: datetime.now(ET)
        # v2 alert expiry. main.py points last_price_fn at its last SPX price.
        self.last_price_fn: Callable[[], float | None] = lambda: None
        self.alert_ttl_min = config.ALERT_TTL_MIN
        self.alert_max_distance_pts = config.ALERT_MAX_DISTANCE_PTS
        # trigger_id -> when its EXECUTE/SKIP alert was sent (wall clock, so
        # a --sim replay of a past date isn't expired on arrival)
        self._alert_sent_at: dict[str, datetime] = {}
        # v2 @mentions on urgent alerts, and one reminder per untapped alert
        self.mention_user_id = config.DISCORD_MENTION_USER_ID
        self.alert_remind_min = config.ALERT_REMIND_MIN
        self._reminded: set[str] = set()

        intents = discord.Intents.default()
        self.client = discord.Client(intents=intents)
        self._client_task: asyncio.Task | None = None

        @self.client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            await self._on_interaction(interaction)

    def register_decision(self, trigger_id: str, decision_id: int) -> None:
        self._pending_decision_id[trigger_id] = decision_id

    async def start(self) -> None:
        # client.start() is just login() + connect(); login() is what
        # initializes the internal readiness event, so it must be awaited
        # directly before wait_until_ready() can be called — scheduling the
        # whole of start() as a background task and immediately awaiting
        # wait_until_ready() races login() and fails every time.
        await self.client.login(self.bot_token)
        self._client_task = asyncio.ensure_future(self.client.connect())
        await self.client.wait_until_ready()

    async def stop(self) -> None:
        await self.client.close()
        if self._client_task is not None:
            await self._client_task

    # -- outbound sends -----------------------------------------------------

    async def _channel(self):
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(self.channel_id)
        return channel

    async def send_text(self, text: str, view: discord.ui.View | None = None) -> discord.Message:
        channel = await self._channel()
        return await channel.send(text, view=view)

    async def send_urgent(self, text: str, view: discord.ui.View | None = None) -> None:
        """send_text with an @mention of the operator in front, if configured."""
        if self.mention_user_id:
            text = f"<@{self.mention_user_id}> {text}"
        await self.send_text(text, view=view)

    async def send_plan_summary(self, plan: Plan) -> None:
        lines = [
            f"**Plan {plan.date}** — bias: {plan.bias}, SPX ref: {_fmt_price(plan.spx_reference)}",
            "",
        ]
        for t in plan.triggers:
            zone = t.condition.price_between
            lines.append(
                f"• `{t.id}` {t.side} {zone} -> {t.action.instrument} "
                f"({t.action.size_pct}%), stop {t.stop}"
            )
        lines.append("")
        lines.append(f"Invalidation: {plan.invalidation.below or ''} / {plan.invalidation.above or ''}")
        if plan.no_entries_before is not None:
            lines.append(f"Opening wait: until {_fmt_wait(plan)} (earlier entries need an override)")
        lines.append(f"Flat by: {plan.flat_by} ET")
        if plan.scheduled_events:
            lines.append("Scheduled: " + ", ".join(f"{e.time} {e.release}" for e in plan.scheduled_events))
        await self.send_text("\n".join(lines))

    async def send_replan_diff(self, old_plan: Plan, new_plan: Plan, summary: str) -> None:
        """`summary` is a one-line description of what prompted the re-plan
        — a release's actual/forecast/surprise, or an operator-supplied
        reason for a manual mid-session re-plan."""
        await self.send_text(
            f"**Re-plan: {summary}**\n\n{_diff_plans(old_plan, new_plan)}"
        )

    async def send_release_unavailable(self, release_name: str) -> None:
        await self.send_text(f"Release data unavailable for {release_name} — plan unchanged.")

    async def send_trigger_fired(
        self, trigger: Trigger, tick: Tick, decision: Decision, decision_id: int
    ) -> None:
        self.register_decision(trigger.id, decision_id)
        self._forget_alert(trigger.id)  # a re-fire starts a fresh clock and reminder
        self._alert_sent_at[trigger.id] = self.now_fn()
        text = (
            f"**TRIGGER FIRED: {trigger.id}**\n"
            f"{trigger.side} @ {_fmt_price(tick.price)} — {_fmt_times(tick.ts)}\n"
            f"Instrument: {trigger.action.instrument} ({trigger.action.size_pct}%)\n"
            f"Agent recommends **{decision.action}** (confidence {decision.confidence}): {decision.reasoning}"
        )
        plan = self.engine.plan if self.engine is not None else None
        early = is_early(plan, tick.ts)
        if early:
            text += (
                f"\n**Early:** inside today's opening wait (until {_fmt_wait(plan)}). "
                "Entering now needs an override with a reason."
            )
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=EARLY_LABEL if early else "EXECUTE",
                style=discord.ButtonStyle.secondary if early else discord.ButtonStyle.success,
                custom_id=f"confirm:{trigger.id}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="SKIP", style=discord.ButtonStyle.danger, custom_id=f"skip:{trigger.id}"
            )
        )
        await self.send_urgent(text, view=view)

    async def send_wait_rearmed(self, trigger: Trigger, tick: Tick, decision: Decision) -> None:
        await self.send_text(
            f"**{trigger.id}** fired but agent says WAIT (confidence {decision.confidence}): "
            f"{decision.reasoning}\nRe-armed with a 5-min cooldown. — {_fmt_times(tick.ts)}"
        )

    async def send_target_hit(self, event: TargetHit) -> None:
        await self.send_text(
            f"**TARGET HIT: {event.trigger_id}** @ {_fmt_price(event.level)} "
            f"(scale out {event.scale_out_pct}%) — {_fmt_times(event.tick.ts)}"
        )

    async def send_stop_moved(self, event: StopMoved) -> None:
        await self.send_urgent(
            f"**STOP MOVED** `{event.trigger_id}`: {event.old_stop:g} -> {event.new_stop:g} "
            f"({event.reason}). Move your stop order to match."
        )

    async def send_stop_hit(self, event: StopHit) -> None:
        await self.send_urgent(
            f"**STOP HIT: {event.trigger_id}** @ {_fmt_price(event.tick.price)} — {_fmt_times(event.tick.ts)}"
        )

    async def send_invalidated(self, event: Invalidated) -> None:
        await self.send_urgent(
            f"**INVALIDATION CROSSED** @ {_fmt_price(event.level)} — flatten and halt. "
            f"{_fmt_times(event.tick.ts)}"
        )

    async def send_flat_by(self, event: FlatByReached) -> None:
        await self.send_text(f"**FLAT-BY TIME REACHED** — close any open positions now. {_fmt_times(event.tick.ts)}")

    async def send_review_required(self, event: ReviewRequired) -> None:
        await self.send_text(f"**REVIEW REQUIRED: {event.trigger_id}** — {event.reason}")

    async def send_feed_stale(self, seconds_since_last_tick: float) -> None:
        await self.send_urgent(
            f"**FEED STALE** — no tick for {seconds_since_last_tick:.0f}s during RTH. "
            "Treating as HALT for new entries until the feed recovers."
        )

    async def send_halt(self, reason: str) -> None:
        await self.send_urgent(f"**HALT** — {reason}")

    async def send_eod_summary(self, text: str) -> None:
        await self.send_text(f"**End-of-day summary**\n{text}")

    async def send_mark_filled_prompt(self, trigger_id: str) -> None:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Mark Filled", style=discord.ButtonStyle.primary, custom_id=f"filled:{trigger_id}"))
        await self.send_text(f"Confirmed **{trigger_id}**. Tap once you're filled.", view=view)

    # -- inbound interaction --------------------------------------------------

    async def _on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id") if interaction.data else None
        if custom_id is None:
            return

        if self.engine is None:
            await interaction.response.edit_message(
                content=(interaction.message.content if interaction.message else "") + "\n\n>> No plan loaded yet, ignored"
            )
            return

        action, _, trigger_id = custom_id.partition(":")
        original = interaction.message.content if interaction.message else ""

        # v2: a button can outlive its trigger's FIRED state (a replan, a
        # restart that re-armed it). confirm/skip/mark_filled would silently
        # no-op, so say so instead of claiming success.
        expected = {"confirm": "FIRED", "skip": "FIRED", "filled": "CONFIRMED"}.get(action)
        state = self.engine.states.get(trigger_id)
        if expected is not None and (state is None or state.status.value != expected):
            now = "removed" if state is None else state.status.value
            await interaction.response.edit_message(
                content=original + f"\n\n>> Ignored: `{trigger_id}` is no longer awaiting this ({now})"
            )
            return

        if action == "confirm":
            now = self.now_fn()
            if await self._expire_if_stale(interaction, trigger_id, original, now):
                return
            blocked = self._risk_block(now)
            if blocked:
                await interaction.response.edit_message(content=original + blocked)
                return
            # v2: inside the plan's soft opening wait, ask for a reason first.
            # The decision is by tap time, whatever the button's label says.
            if is_early(self.engine.plan, now):
                await interaction.response.send_modal(
                    EarlyEntryModal(self, trigger_id, _fmt_wait(self.engine.plan))
                )
                return
            await self._confirm(trigger_id, "regular", None)
            await interaction.response.edit_message(content=original + "\n\n>> EXECUTE confirmed by operator")
            await self.send_mark_filled_prompt(trigger_id)
        elif action == "skip":  # keeps the fire-time early/regular tag
            self._forget_alert(trigger_id)
            self.engine.skip(trigger_id)
            decision_id = self._pending_decision_id.get(trigger_id)
            if decision_id is not None and self.journal is not None:
                await self.journal.log_operator_response(decision_id, "skip")
            await interaction.response.edit_message(content=original + "\n\n>> SKIPPED by operator")
        elif action == "filled":
            self.engine.mark_filled(trigger_id)
            decision_id = self._pending_decision_id.get(trigger_id)
            if decision_id is not None and self.journal is not None:
                await self.journal.log_filled(decision_id)
            await interaction.response.edit_message(content=original + "\n\n>> Marked FILLED")

    # -- EXECUTE helpers (v2) ---------------------------------------------------

    def _risk_block(self, now: datetime) -> str | None:
        """The text to append if the risk gate refuses an entry, else None."""
        if self.risk is None:
            return None
        gate = self.risk.gate_execute(self.engine, now)
        if gate.allowed:
            return None
        return f"\n\n>> EXECUTE blocked by risk ({gate.reason}) — not confirmed"

    def _stale_reason(self, trigger_id: str, now: datetime) -> str | None:
        """Why this alert is too old to act on, or None if it's still fresh."""
        sent_at = self._alert_sent_at.get(trigger_id)
        if sent_at is not None:
            age_min = (now - sent_at).total_seconds() / 60
            if age_min > self.alert_ttl_min:
                return f"alert is {int(age_min)} min old (limit {self.alert_ttl_min:g})"
        state = self.engine.states.get(trigger_id) if self.engine is not None else None
        last = self.last_price_fn()
        if state is not None and state.fire_price is not None and last is not None:
            distance = abs(last - state.fire_price)
            if distance > self.alert_max_distance_pts:
                return (
                    f"SPX {_fmt_price(last)} is {distance:.1f} pts from the "
                    f"{_fmt_price(state.fire_price)} alert price (limit {self.alert_max_distance_pts:g})"
                )
        return None

    def _forget_alert(self, trigger_id: str) -> None:
        self._alert_sent_at.pop(trigger_id, None)
        self._reminded.discard(trigger_id)

    async def _expire(self, trigger_id: str) -> None:
        """FIRED -> SKIPPED, journaled as "expired"."""
        self.engine.skip(trigger_id)
        self._forget_alert(trigger_id)
        decision_id = self._pending_decision_id.get(trigger_id)
        if decision_id is not None and self.journal is not None:
            await self.journal.log_operator_response(decision_id, "expired")

    async def _expire_if_stale(
        self, interaction: discord.Interaction, trigger_id: str, original: str, now: datetime
    ) -> bool:
        reason = self._stale_reason(trigger_id, now)
        if reason is None:
            return False
        await self._expire(trigger_id)
        await interaction.response.edit_message(
            content=original + f"\n\n>> EXPIRED: {reason} — not confirmed, trigger skipped"
        )
        return True

    async def expire_stale_alerts(self) -> list[str]:
        """Called from main.py's input poller. An alert still untapped after
        ALERT_REMIND_MIN gets one @mention reminder; past the TTL it is
        skipped, with one notice. Distance alone doesn't expire an untapped
        alert (price may come back); it's checked on the tap."""
        if self.engine is None:
            return []
        now = self.now_fn()
        expired = []
        for trigger_id, sent_at in list(self._alert_sent_at.items()):
            state = self.engine.states.get(trigger_id)
            if state is None or state.status.value != "FIRED":
                self._forget_alert(trigger_id)  # tapped, replanned away
                continue
            age_min = (now - sent_at).total_seconds() / 60
            if age_min <= self.alert_ttl_min:
                if age_min >= self.alert_remind_min and trigger_id not in self._reminded:
                    self._reminded.add(trigger_id)
                    await self.send_urgent(
                        f"**REMINDER: {trigger_id}** alert untapped for {int(age_min)} min — "
                        f"expires in {int(self.alert_ttl_min - age_min)} min. Tap EXECUTE or SKIP."
                    )
                continue
            await self._expire(trigger_id)
            await self.send_urgent(
                f"**ALERT EXPIRED: {trigger_id}** — untapped for {int(age_min)} min "
                f"(limit {self.alert_ttl_min:g}). Skipped; no entry. {_fmt_times(now)}"
            )
            expired.append(trigger_id)
        return expired

    async def _confirm(self, trigger_id: str, entry_timing: str, early_reason: str | None) -> None:
        self._forget_alert(trigger_id)
        self.engine.confirm(trigger_id)
        decision_id = self._pending_decision_id.get(trigger_id)
        if decision_id is not None and self.journal is not None:
            await self.journal.log_operator_response(
                decision_id, "confirm", entry_timing=entry_timing, early_reason=early_reason
            )

    async def submit_early_override(
        self, interaction: discord.Interaction, trigger_id: str, reason: str
    ) -> None:
        """The early-entry reason box was submitted. Time has passed since the
        tap, so re-check the trigger and the risk gate (HALT still wins)."""
        original = interaction.message.content if interaction.message else ""
        reason = " ".join((reason or "").split())
        state = self.engine.states.get(trigger_id) if self.engine is not None else None
        if state is None or state.status.value != "FIRED":
            now_status = "removed" if state is None else state.status.value
            await interaction.response.edit_message(
                content=original + f"\n\n>> Ignored: `{trigger_id}` is no longer awaiting this ({now_status})"
            )
            return
        if not reason:
            await interaction.response.edit_message(
                content=original + "\n\n>> Early entry needs a reason — not confirmed. Tap again to retry."
            )
            return
        now = self.now_fn()
        if await self._expire_if_stale(interaction, trigger_id, original, now):
            return
        blocked = self._risk_block(now)
        if blocked:
            await interaction.response.edit_message(content=original + blocked)
            return
        await self._confirm(trigger_id, "early", reason)
        await interaction.response.edit_message(
            content=original + f"\n\n>> EARLY ENTRY confirmed by operator: {reason}"
        )
        await self.send_mark_filled_prompt(trigger_id)
