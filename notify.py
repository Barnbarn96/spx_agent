"""Discord bot: alerts + inline confirm/skip/mark-filled buttons. Runs over
Discord's Gateway (a persistent outbound websocket, like Telegram's
long-polling) — no inbound ports, no public webhook endpoint. Every alert
carries price, time (ET + SGT), trigger id, and the agent's one-line
reasoning where applicable. See SPEC.md.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from schema import (
    Decision,
    FlatByReached,
    Invalidated,
    Plan,
    ReviewRequired,
    StopHit,
    TargetHit,
    Tick,
    Trigger,
)
from triggers import TriggerEngine

ET = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")


def _fmt_times(ts: datetime) -> str:
    et = ts.astimezone(ET).strftime("%H:%M:%S")
    sgt = ts.astimezone(SGT).strftime("%H:%M:%S")
    return f"{et} ET / {sgt} SGT"


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
        text = (
            f"**TRIGGER FIRED: {trigger.id}**\n"
            f"{trigger.side} @ {_fmt_price(tick.price)} — {_fmt_times(tick.ts)}\n"
            f"Instrument: {trigger.action.instrument} ({trigger.action.size_pct}%)\n"
            f"Agent recommends **{decision.action}** (confidence {decision.confidence}): {decision.reasoning}"
        )
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="EXECUTE", style=discord.ButtonStyle.success, custom_id=f"confirm:{trigger.id}"
            )
        )
        view.add_item(
            discord.ui.Button(
                label="SKIP", style=discord.ButtonStyle.danger, custom_id=f"skip:{trigger.id}"
            )
        )
        await self.send_text(text, view=view)

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

    async def send_stop_hit(self, event: StopHit) -> None:
        await self.send_text(
            f"**STOP HIT: {event.trigger_id}** @ {_fmt_price(event.tick.price)} — {_fmt_times(event.tick.ts)}"
        )

    async def send_invalidated(self, event: Invalidated) -> None:
        await self.send_text(
            f"**INVALIDATION CROSSED** @ {_fmt_price(event.level)} — flatten and halt. "
            f"{_fmt_times(event.tick.ts)}"
        )

    async def send_flat_by(self, event: FlatByReached) -> None:
        await self.send_text(f"**FLAT-BY TIME REACHED** — close any open positions now. {_fmt_times(event.tick.ts)}")

    async def send_review_required(self, event: ReviewRequired) -> None:
        await self.send_text(f"**REVIEW REQUIRED: {event.trigger_id}** — {event.reason}")

    async def send_feed_stale(self, seconds_since_last_tick: float) -> None:
        await self.send_text(
            f"**FEED STALE** — no tick for {seconds_since_last_tick:.0f}s during RTH. "
            "Treating as HALT for new entries until the feed recovers."
        )

    async def send_halt(self, reason: str) -> None:
        await self.send_text(f"**HALT** — {reason}")

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
            if self.risk is not None:
                gate = self.risk.gate_execute(self.engine, datetime.now(ET))
                if not gate.allowed:
                    await interaction.response.edit_message(
                        content=original + f"\n\n>> EXECUTE blocked by risk ({gate.reason}) — not confirmed"
                    )
                    return
            self.engine.confirm(trigger_id)
            decision_id = self._pending_decision_id.get(trigger_id)
            if decision_id is not None and self.journal is not None:
                await self.journal.log_operator_response(decision_id, "confirm")
            await interaction.response.edit_message(content=original + "\n\n>> EXECUTE confirmed by operator")
            await self.send_mark_filled_prompt(trigger_id)
        elif action == "skip":
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
