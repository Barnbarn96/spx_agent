# v2 Status & Handoff

**Read this first if you're picking up `release-v2`.**
- [SPEC.md](../SPEC.md) is the contract: how things work.
- This file is the plan: where the build stopped, what to do next, what's decided and what's still open.

| | |
|---|---|
| Last updated | 2026-11-24 |
| Branch | `release-v2` (from `release-v1` @ `f867349`) |
| Commits so far | `fd68343` milestone 1 → `6d2d208` engine fixes → `16f1219` trigger types |
| Tests | **130 passing**: `.venv/Scripts/python.exe -m pytest -q` |
| Live verification of v2 | **None yet.** Everything is unit-tested and replay-tested only. |

**When you finish a task:**
1. Move it from §1 to §4 (Done).
2. Add any decision to §3, with the date and the reason.
3. Update the [planned] markers in SPEC.md.
4. Run the full suite.
5. Re-run the 07-01 replay (§6) if you touched the engine.

---

## 1. START HERE: next tasks, in order

Work top to bottom. Nothing in **1a** needs the operator. Items in **1b** are blocked on a decision in §2.

Ground rules for every task:
- **Write the tests first.** For engine work, test through the real `TriggerEngine`.
- **Each rule needs a test that shows it blocking**, not only one that shows it passing.
- **Never let a Discord button claim success it didn't have.** `notify.py` already checks the trigger's state before acting; keep that.

### 1a. Ready to build now

**1. Two stops ends the day** (`risk.py`, `main.py`)
- In `main.py`'s `_handle_event`, on `StopHit` call a new `RiskManager.record_stop(now)`.
- Count stops per ET day. On the 2nd, call `create_halt("two stops today")` and post to Discord: "2 stops: no new entries today (state/HALT)". `gate_execute()` already blocks on HALT.
- The count must survive a restart. Either rebuild it at startup from `journal.events_since(<today 00:00 ET>)` (filter `type == "stop_hit"`), or persist it next to the engine snapshot.
- *Done when:* two `StopHit`s create `state/HALT` and EXECUTE is refused, and a restart after one stop still halts on the next one.

**2. EXECUTE asks for the fill price** (`notify.py`, `triggers.py`)
- EXECUTE runs the risk gate, then opens a `discord.ui.Modal` with one optional field: "SPX fill level". `interaction.response.send_modal()` must be the first response to the interaction.
- On submit, call `engine.confirm(id)` then `engine.mark_filled(id, fill_price=<value or None>)`, and journal it with `journal.log_filled()` and the price (add a `fill_price` column with the same `PRAGMA table_info` migration pattern as `filled_ts`).
- Keep the "Mark Filled" button only as a fallback for a dismissed modal.
- This gives breakeven a real entry. Today it uses the fire price.
- *Done when:* a fake interaction or modal test shows CONFIRMED→OPEN with `entry_price` equal to the typed value, a blank value falls back to the fire price, and a risk-blocked EXECUTE never opens the modal.

**3. Soft early-entry block** (`schema.py`, `risk.py`, `notify.py`, `journal.py`, `.claude/commands/premarket.md`)
- Add `Plan.no_entries_before: str | None` (HH:MM ET). `/premarket` sets it: "09:30" normally, "09:45"-"10:00" on data days, big gaps or VIX > 30.
- Before that time, the alert's EXECUTE button becomes **"Early entry — override?"**. Tapping it asks for a one-line reason, which goes in a new journal column. The hard rules (HALT, cutoff, max open) still apply.
- Tag each decision `early` / `regular` in the journal, so the operator can later compare results (§3, 11-24 opening-wait decision).
- *Done when:* tests cover the early path (override needed, reason journaled), the regular path (unchanged) and HALT still winning over an override.

**4. Alerts expire** (`notify.py`, `main.py`, `config.py`)
- New settings: `ALERT_TTL_MIN` (e.g. 10) and `ALERT_MAX_DISTANCE_PTS` (e.g. 10).
- On an EXECUTE tap, refuse if the alert is older than the TTL, or if the last SPX price is further than the maximum from the fire price. The notifier needs the orchestrator's `last_price`; pass a callable in.
- Also move the trigger FIRED → SKIPPED in the engine when it expires, and journal it as an operator response of "expired".
- *Done when:* tests show a stale-by-time tap and a stale-by-distance tap both refused with a reason, and a fresh tap working.

**5. @mention on the alerts that matter** (`notify.py`, `config.py`)
- New setting: `DISCORD_MENTION_USER_ID`.
- Prefix `<@id>` on: stop hit, invalidation, STOP MOVED, the two-stops HALT and feed stale.
- Add a reminder if a FIRED alert has gone untapped for N minutes (the input poller in `main.py` is the place for timed checks).
- *Done when:* tests check the prefix and that the reminder is sent once per alert.

**6. `!yield 4.93` in Discord** (`notify.py`)
- Add an `on_message` handler for the configured channel. It needs `intents.message_content = True`, and the bot needs the Message Content intent turned on in the Discord developer portal; tell the operator.
- Parse `!yield <value>` and call `inputs.write_yield(value)`. Reply with `tools/set_yield.py`'s one-line format.
- GEX images in Discord are a separate, bigger task (1b-4).
- *Done when:* a fake message writes `state/yield.json`, and junk input gets a polite rejection.

**7. `decide()` gets the real context** (`main.py` `_decide_notes`, `prompts/decide.md`)
- Add:
  - the day's `analyses/<date>.md`: at least the levels table, scenarios and trade bias, trimmed to a sensible size
  - the trading rules section of CLAUDE.md
  - the last six 5-minute bar closes (the engine already builds bars; expose them)
  - VIX, once 1b-1 lands
- Update `prompts/decide.md` to tell the model how to use them. Keep the 30 s timeout and the default to SKIP.
- *Done when:* a test asserts the notes contain the levels table and recent closes. Then run one real `decide()` by hand and check the reasoning cites them.

**8. Feed polish** (`feed.py`)
- Don't recalibrate between 9:30 and 9:35 ET.
- SPY ex-dividend days: allow a manual `--spy-dividend` / env override that's added back to SPY's previous close for that day.
- Optional, with the operator's OK: recalibrate from a fresh GEX screenshot's `spx_in_shot` when drift is above 2 points.

### 1b. Blocked on an operator decision (see §2)
1. **VIX size tiers and the "VIX > 30" wait.** Needs decision §2.3. The size rule is up to 30% below 20, 15-20% at 20-30, about 10% above 30. Enforce it at plan install (`write_plan.py`) and at EXECUTE.
2. **Per-trade dollar cap.** Needs §2.2. A size above the cap blocks EXECUTE.
3. **The daily loss limit is inert (gap found 11-24).** `RiskManager.record_realized_pnl()` exists, but nothing in production calls it, so the `DAILY_LOSS_LIMIT_USD` HALT can never trigger. It needs realized P&L per trade. One approach is that the operator types the P&L when a trade closes (a Discord modal on stop or final target). Ask the operator how they want to report it.
4. **GEX screenshots posted in Discord run the intraday flow.** It needs the Agent SDK with image input, `setting_sources=["project"]` and the Skill tool; check the current SDK docs. Ask the operator whether `/intraday` in Claude Code is enough first.
5. **Cleanup of unneeded v1 code** (§2.1). Do it only after the operator approves, and preferably after 1a, so you don't fight merge noise.

---

## 2. Open decisions, waiting on the operator

1. **Cleanup list. Proposed, NOT approved. Don't delete anything until the operator says so.**
   - AI plan generation in `agent.py`: `generate_plan`, `replan`, the `python -m agent` CLI, the MES helper, `prompts/daily_plan.md`, `prompts/replan.md`. Keep `decide()` and `prompts/decide.md`.
   - `PLAN_AUTOGEN` and `on_generate_plan`.
   - Release auto-replan: `releases.py`, Finnhub, `on_release_poll`, release jobs in `scheduler.py`, `Plan.scheduled_events`, `test_releases.py`.
   - `PolygonFeed` and `POLYGON_*`.
   - The direction-based yield check: `yields_10y`, `YIELD_WINDOW_MIN`, `YIELD_EPSILON`, `yield_direction`, plus the related tests.
   - Untrack `.claude/scheduled_tasks.lock` and add it to `.gitignore`.
2. **Per-trade dollar cap amount.** The context doc still says "[your number, e.g. 1% of account]".
3. **VIX source.** Manual (`/intraday ... vix 18`), read off a screenshot, or Moomoo (unverified).
4. **How to report realized P&L** for the daily loss limit (1b-3).

---

## 3. Decisions already made (don't relitigate)

| Date | Decision | Why |
|---|---|---|
| 11-24 | **Claude Code is the analyst; `main.py` is the watcher.** They share files in `state/`. Set up like the operator's other repo: CLAUDE.md, skills, commands, `tools/*.py`, a settings allow list. | The operator already plans with their 3 skills in Claude; v2 connects that to the engine. |
| 11-24 | **No subagents for now.** | Speed matters mid-session, and Python validators beat reviewer agents for checking rules. |
| 11-24 | **SPX comes from Moomoo (SPY × ratio).** The operator sends SPX once, pre-market, from TradingView `SPREADEX:SPX` as `spx_reference`. MES is a fallback. | Their data source. Polygon has no key. |
| 11-24 | **Ratio = previous SPX close ÷ previous SPY close.** | v1 compared a pre-market estimate with thin pre-market SPY, taken at different moments: possibly 3-5+ points off all day. |
| 11-24 | **The 10Y is entered by hand only** and expires after 20 minutes. Yield triggers are level-based (4.95 / 5.00 / 5.15). | Moomoo has no yields, and direction can't come from sparse readings. OCR is optional, for later. |
| 11-24 | **GEX comes from Moomoo Gamma Exposure screenshots** (0DTE expiry) via `/intraday`, repeatedly. | It fills the "no live GEX" gap. Rhythm: pre-market, around 10:00, midday, around 14:00, and near the flip or a wall. |
| 11-24 | **The opening wait is per-day and soft** (`no_entries_before`, plus an override and journal tags). | The operator entered early on 11-23 and questioned the 30-minute rule. Decide from journal data. |
| 11-24 | **The repo copy of the skills is the master.** | Versioned with the code that reads their output. |
| 11-24 | **The operator uses `/intraday` (not the bare skill)** for data or GEX screenshots. | Only the command records inputs for `main.py` and offers a replan. |
| 11-24 | **v1 SPEC and ROLLOUT_PLAN live in `docs/v1/`.** | — |
| 11-24 | **A jump straight through an entry zone → `ZoneSkipped`, no fire.** | The first version fired and chased the 07-01 spike, 7464→7480 through the 7465-7472 dip zone, instead of the 7468 pullback. |
| 11-24 | **Price already in a zone at first sight → `AlreadyInZone`, no fire.** A gate opening with price in the zone still fires (tick mode). | Don't enter a half-over setup. A gate opening is a real "conditions now met". |
| 11-24 | **Restored FIRED triggers are re-armed on restart**, with a Discord notice. | Their alert may never have been sent. |
| 11-24 | **Close-confirmed triggers track closes through closed gates.** A close-through before `not_before` isn't fired late. The dwell clock runs through gates. | A 9:45 breakout is old news at 10:00. A hold is still a hold. |
| 11-24 | **`first_touch` counts touches during gates and price in the zone at startup.** | "Fade the first touch" means the first one. |
| 11-24 | **Stops default to any tick; `stop_confirm: close` is opt-in.** | The fastest exit is the safe default for 0DTE. |
| 11-24 | **Breakeven uses the fire price as the entry until 1a-2 (fill price) lands.** It only ever tightens. | No fill price is captured yet. |

---

## 4. Done

**Milestone 1 (`fd68343`): workspace and inputs**
- **Claude Code workspace:**
  - `CLAUDE.md`
  - `.claude/skills/` (3 copies)
  - `.claude/commands/{premarket,intraday,eod}.md`
  - `.claude/settings.json`
  - `context/context_2026-11-23.md`
- **Inputs:**
  - `inputs.py` (yield and GEX files: atomic writes, expiry, GEX regime)
  - `tools/set_yield.py`, `tools/set_gex.py`
  - `tools/write_plan.py` (schema check, grounding check, flat_by ≤ 15:30, `--replan` guard, `--schema`, `--example`)
- **Schema and engine:** `Condition.yield_below` / `yield_above`; `Plan.spx_reference_source` / `spx_prev_close`.
- **Feed:** `MoomooFeed.calibrate_from_prev_close()`.
- **`main.py`:**
  - timer-based input poller
  - the first plan of the day is announced
  - yield attached to ticks, with a reminder per lapse
  - the GEX check of the SPX estimate
  - `decide()` notes
- **Config:** `PLAN_AUTOGEN` off; models `claude-sonnet-5-5`; `FEED_SOURCE=moomoo`.

**Engine fixes (`6d2d208`)**
- **`ZoneSkipped`:** no fire on a jump through a zone, with a 07-01 regression test.
- **`AlreadyInZone`:** no fire at first sight.
- **`decide()` in the background:** `_spawn_decide` / `wait_for_decisions`; no alert if the trigger is no longer FIRED.
- **Persistence:** `to_snapshot` / `restore_snapshot` to `state/engine_<date>.json`.
- **Fixes:** plan reload compares contents, not mtime (a flaky miss on Windows); stale Discord buttons reply "Ignored".

**Trigger types (`16f1219`)**
- **Confirmation:** `Confirm {tick | close 1m/5m | dwell seconds}` on entries, `stop_confirm` on stops, and the engine bar builder.
- **New fields:** `approach`, `first_touch`, and `then_stop: "breakeven"` with the `StopMoved` event and Discord notice.
- **Fills:** `mark_filled(id, fill_price=None)`.
- **Restructure:** `_condition_met` = pre-gate bookkeeping → `_gates_open` → tick, close or dwell candidate.
- **Plan tooling:** `/premarket` maps write-up wording to fields; the example plan uses every feature.

**Tests:** `test_v2_inputs.py` (22), `test_v2_engine.py` (16), `test_v2_trigger_types.py` (24), plus updates to v1 tests. v1 tests that assumed "first tick in zone fires" now start outside the zone.

---

## 5. Known gaps and risks (not yet tasks, but don't forget them)
- **The daily loss limit is inert:** nothing feeds realized P&L (1b-3).
- **A replan resets touch counts** for still-ARMED triggers (`load_plan` creates a fresh `TriggerState`), so a `first_touch` trigger could fire on a later touch after a replan. Fix: carry `touches` over when the id and zone are unchanged.
- **Close-confirmed stops have no hard backstop.** A violent move only exits at the next bar close. Consider a "tick stop at stop ± N pts" safety net.
- **A "stall"/rejection pattern can't be expressed.** Plans say "confirm by eye" in `notes`.
- **The `main.py` handler for `StopMoved` has no test** (the engine side does).
- **Whether `prev_close_price` is present in Moomoo extended-hours push rows is unverified** (§7). If it isn't, calibration falls back silently except for a log line.
- **`main.py --sim` calls the real `decide()`,** which uses the SDK and the subscription. For offline checks use `replay.py`, which stubs `decide`.

---

## 6. How to check your work
- **Unit tests:** `.venv/Scripts/python.exe -m pytest -q`. All must pass. Run them more than once if you touched timing or files (a flaky mtime bug was found this way).
- **Engine replay:** `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe replay.py --plan tests/fixtures/plan_2026-07-01.json --ticks tests/fixtures/ticks_sample.csv`. Expected: `zone_skipped` at 7480, `trigger_fired` primary_long at **7468** (10:02), then targets 7498 / 7515 / 7527.
- **Plan tooling:** `.venv/Scripts/python.exe tools/write_plan.py tests/fixtures/plan_v2_example.json --analysis tests/fixtures/analysis_2026-11-23.md --date 2026-11-23 --dry-run`. Expect "Plan OK" with 4 triggers.

## 7. Live verification checklist (the operator runs these; record results here)
- [ ] `main.py` with Moomoo OpenD pre-market: SPY ticks flow, and after a plan with `spx_prev_close` loads, the log or the `decide()` notes show `ratio_source` = **prev closes**.
- [ ] `/premarket <price>` end to end: analysis in chat, `analyses/<date>.md` written, plan installed, `main.py` logs "loaded today's plan", Discord summary posted.
- [ ] `/intraday 7268 4.93` writes `state/yield.json`, and `main.py` logs "10Y yield updated".
- [ ] `/intraday` with a GEX screenshot writes `state/gex.json`, shows the changes, and posts the SPX estimate warning when off by more than 2 points.
- [ ] Discord posts the yield reminder when a yield-gated trigger is armed and the reading has expired.
- [ ] `AlreadyInZone`, `ZoneSkipped` and STOP MOVED notices appear in Discord when they happen.
- [ ] Restart `main.py` mid-session: the "Restarted: restored trigger state" notice appears, and an OPEN trade keeps its stop.
- [ ] `/eod` writes `context/context_<date>.md`.
- [ ] From v1, still unverified: continuous RTH ticks, reconnect with backoff, the dead-man rule (`docs/v1/ROLLOUT_PLAN.md` Step 4).

## 8. Environment notes / gotchas
- **Python:** always `.venv/Scripts/python.exe` (Python 3.12 venv in the repo). The Store `python` on PATH has no packages.
- **pip on this network** fails SSL verification because of corporate inspection. Don't disable verification. Export the Windows trusted roots to a PEM (`ssl.enum_certificates("ROOT"/"CA")`), set the environment variable `PIP_CERT=<pem>`, then install.
- **The Windows console mangles non-ASCII.** Keep `tools/` output ASCII, and set `PYTHONIOENCODING=utf-8` when printing JSON with non-ASCII.
- **Bash heredocs** containing `'''` broke once. For multi-line Python, write a scratch file and run it.
- **`.claude/settings.json`** denies reading `.env` / `.env.local` and editing `state/` by hand. Don't widen it to `.env.*`, which blocks `.env.example`.
- **Moomoo:**
  - no SPX, MES/ES (paid quote card) or yields
  - `US.ES` resolves to Eversource Energy
  - `.SPX` repeats the previous close before 09:30
- **The operator works in SGT.** Screenshot clocks are SGT: 21:30/22:00 SGT = 8:30/10:00 ET until 2027-03-14.
- **Skills:** use the repo copies in `.claude/skills/`. The claude.ai-synced `spx-*:spx-*` copies also exist.
