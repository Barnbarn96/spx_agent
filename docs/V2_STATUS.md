# v2 Status & Handoff

**Read this first if you're picking up `release-v2`.**
- [SPEC.md](../SPEC.md) is the contract: how things work.
- This file is the plan: where the build stopped, what to do next, what's decided and what's still open.

| | |
|---|---|
| Last updated | 2026-11-24 |
| Branch | `release-v2` (from `release-v1` @ `f867349`) |
| Commits so far | `fd68343` milestone 1 → `6d2d208` engine fixes → `16f1219` trigger types → two stops ends the day |
| Tests | **180 passing**: `.venv/Scripts/python.exe -m pytest -q` |
| Live verification of v2 | **None yet.** Everything is unit-tested and replay-tested only. |

**When you finish a task:**
1. Move it from §1 to §4 (Done).
2. Add any decision to §3, with the date and the reason.
3. Update the [planned] markers in SPEC.md.
4. Run the full suite.
5. Re-run the 07-01 replay (§6) if you touched the engine.

---

## 1. START HERE: next tasks, in order

Work top to bottom. Nothing in **1a** needs the operator. **1b** is parked (v1 cleanup, later).

Ground rules for every task:
- **Write the tests first.** For engine work, test through the real `TriggerEngine`.
- **Each rule needs a test that shows it blocking**, not only one that shows it passing.
- **Never let a Discord button claim success it didn't have.** `notify.py` already checks the trigger's state before acting; keep that.

### 1a. Ready to build now

**1. @mention on the alerts that matter** (`notify.py`, `config.py`)
- New setting: `DISCORD_MENTION_USER_ID`.
- Prefix `<@id>` on: stop hit, invalidation, STOP MOVED, the two-stops HALT and feed stale.
- Add a reminder if a FIRED alert has gone untapped for N minutes (the input poller in `main.py` is the place for timed checks).
- *Done when:* tests check the prefix and that the reminder is sent once per alert.

**2. A replan keeps `first_touch` counts** (`triggers.py`)
- Today `load_plan` creates a fresh `TriggerState`, so a replan resets `touches`. A `first_touch` trigger that survives a replan unchanged could then fire on a second touch, against the "fade the first touch" rule.
- Replans happen through `/intraday` when a data or GEX screenshot changes the bias, so this is a real path.
- In `TriggerEngine.load_plan`, carry `touches` (and the first-sight flag) over when the trigger id **and** its `price_between` zone are unchanged. A changed zone starts fresh.
- *Done when:* engine tests show a touch, a replan with the same id and zone, then a second touch **not** firing; and a replan with a moved zone firing on its first touch.

**3. `decide()` gets the real context** (`main.py` `_decide_notes`, `prompts/decide.md`)
- Add:
  - the day's `analyses/<date>.md`: at least the levels table, scenarios and trade bias, trimmed to a sensible size
  - the trading rules section of CLAUDE.md
  - the last six 5-minute bar closes (the engine already builds bars; expose them)
- Update `prompts/decide.md` to tell the model how to use them. Keep the 30 s timeout and the default to SKIP.
- *Done when:* a test asserts the notes contain the levels table and recent closes. Then run one real `decide()` by hand and check the reasoning cites them.

**4. Feed polish** (`feed.py`)
- Don't recalibrate between 9:30 and 9:35 ET.
- SPY ex-dividend days: allow a manual `--spy-dividend` / env override that's added back to SPY's previous close for that day.
- Optional, with the operator's OK: recalibrate from a fresh GEX screenshot's `spx_in_shot` when drift is above 2 points.

### 1b. Parked
1. **Cleanup of unneeded v1 code** (list in §2.1). The operator said on 11-24 to leave the v1 code alone for now and come back to it later. Don't delete anything until they say so.

---

## 2. Open decisions, waiting on the operator

1. **Cleanup list. Parked (11-24): leave the v1 code alone for now; the operator will come back to it. Don't delete anything until they say so.**
   - AI plan generation in `agent.py`: `generate_plan`, `replan`, the `python -m agent` CLI, the MES helper, `prompts/daily_plan.md`, `prompts/replan.md`. Keep `decide()` and `prompts/decide.md`.
   - `PLAN_AUTOGEN` and `on_generate_plan`.
   - Release auto-replan: `releases.py`, Finnhub, `on_release_poll`, release jobs in `scheduler.py`, `Plan.scheduled_events`, `test_releases.py`.
   - `PolygonFeed` and `POLYGON_*`.
   - The direction-based yield check: `yields_10y`, `YIELD_WINDOW_MIN`, `YIELD_EPSILON`, `yield_direction`, plus the related tests.
   - Untrack `.claude/scheduled_tasks.lock` and add it to `.gitignore`.

Nothing else is open. The dollar cap, VIX source and P&L reporting were decided on 11-24 (§3).

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
| 11-24 | **Alert age is measured from the Discord send time (wall clock), not the tick time; an expired alert goes to SKIPPED, not EXPIRED.** Untapped alerts expire by time only. | A `--sim` replay of a past date would otherwise expire on arrival. `EXPIRED` already means a trigger's window closed before it fired. Price can come back to an untapped alert, so distance is checked on the tap. |
| 11-24 | **Breakeven uses the fire price as the entry.** It only ever tightens. | — |
| 11-24 | **No fill price on EXECUTE (task dropped).** Mark Filled stays as the one-tap "I'm in" that starts stop/target tracking. | The operator trades off SPX key levels, not the option premium. |
| 11-24 | **The soft wait is decided at tap time, and triggers alert inside it.** `/premarket` sets triggers' `not_before` to 09:30 and puts the wait in `no_entries_before`. A tap after the wait is a regular entry even if the alert came earlier; the fire-time tag is kept only for skips. | The journal should record when the operator actually entered. Seeing alerts inside the wait is what lets the operator judge early setups. |
| 11-24 | **No per-trade dollar cap in code.** | The operator sizes each trade by hand; nothing executes automatically. |
| 11-24 | **No VIX source in code.** VIX tiers and the VIX > 30 wait live in the `/premarket` write-up, its `size_pct` and its `not_before`. A mid-session VIX spike is handled by an `/intraday` replan. | A code check would duplicate the analysis; the operator sees VIX on screen. |
| 11-24 | **No realized P&L reporting.** The daily loss limit HALT stays inert on purpose; two stops ends the day is the daily brake. | The operator doesn't want to type P&L. |
| 11-24 | **Discord is for alerts only.** No `!yield` command and no GEX screenshots in Discord; inputs go through `/intraday` in Claude Code. | One input path, and it's the one that records state and offers replans. |
| 11-24 | **Replans stay, via `/intraday`** (on a changed bias and the operator's yes). So the replan touch-count gap gets fixed (1a-2). | Adjusting the plan after a data surprise is the point of mid-session screenshots. |
| 11-24 | **Two stops: the count is rebuilt from the journal at startup, and the rebuild never creates the HALT.** Only the stop that reaches the limit does, so a HALT the operator deleted isn't re-created by a restart or a third stop. | The journal already has every `stop_hit`; no new state file. Deleting the HALT is the operator's override. |

---

## 4. Done

**Alerts expire (was 1a-1 after the fill-price drop, 11-24)**
- `ALERT_TTL_MIN` (10) and `ALERT_MAX_DISTANCE_PTS` (10) in `config.py` and `.env.example` (plus `MAX_STOPS_PER_DAY`).
- `notify.py`: `_stale_reason()` / `_expire_if_stale()` on the EXECUTE tap and on the early-entry submit, before the risk gate; `expire_stale_alerts()` for untapped alerts. `main.py` wires `last_price_fn` and calls it from `poll_inputs()`, which then saves engine state.
- Tests: `tests/test_v2_alert_expiry.py` (12). A mutation check (staleness off) fails 7.

**Soft early-entry block (was 1a-2, 11-24)**
- `Plan.no_entries_before`; `risk.is_early()`; `write_plan.py` checks it's within 09:30-15:00 and prints the wait.
- `notify.py`: the alert's button becomes "Early entry — override?" with an Early line; an early tap runs the risk gate, then `EarlyEntryModal` asks for a reason; `submit_early_override()` re-checks FIRED and the risk gate, then confirms. The plan summary shows the wait.
- `journal.py`: `decisions.entry_timing` and `early_reason` columns (migrated on old DBs). `main.py` tags by fire time; confirm re-tags by tap time.
- `/premarket` now sets `not_before` 09:30 and the wait in `no_entries_before`; `plan_v2_example.json` uses the pattern.
- Tests: `tests/test_v2_early_entry.py` (17) and 3 in `test_v2_inputs.py`. A mutation check (early detection off) fails 8 of them.

**Discord ping tool (11-24)**
- `tools/discord_ping.py`: one test message via REST with the `.env` settings; prints OK or a plain-English failure (bad token, wrong or non-numeric channel ID, missing View Channel / Send Messages, network/SSL). Exit 0/1. Never prints the token.
- Tests: `tests/test_discord_ping.py` (10), with a fake client.
- Why: the operator saw no Discord messages and the unit tests never touch Discord.

**Two stops ends the day (was 1a-1, 11-24)**
- `RiskManager.record_stop()` / `seed_stops()` / `stops_today()`, counted per ET day; `MAX_STOPS_PER_DAY` (2) in `config.py`.
- `main.py`: every `StopHit` is counted after its alert; the 2nd posts "**HALT** — 2 stops: no new entries today (state/HALT)". `run_live()` calls `restore_stop_count()` from the journal first.
- Tests: `tests/test_v2_two_stops.py` (8): one stop doesn't halt, the 2nd halts and EXECUTE is refused, a new ET day resets, a restart after one stop halts on the next, yesterday's stops are ignored.

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
- **The daily loss limit is inert by choice** (§3). Two stops ends the day is the daily brake.
- **A replan resets touch counts:** now task 1a-2.
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
- [x] `tools/discord_ping.py` prints "Discord OK" and the message appears in the channel. **Passed 11-24** (operator's PC). Do this first: every Discord item below depends on it.
- [ ] `main.py` with Moomoo OpenD pre-market: SPY ticks flow, and after a plan with `spx_prev_close` loads, the log or the `decide()` notes show `ratio_source` = **prev closes**.
- [ ] `/premarket <price>` end to end: analysis in chat, `analyses/<date>.md` written, plan installed, `main.py` logs "loaded today's plan", Discord summary posted.
- [ ] `/intraday 7268 4.93` writes `state/yield.json`, and `main.py` logs "10Y yield updated".
- [ ] `/intraday` with a GEX screenshot writes `state/gex.json`, shows the changes, and posts the SPX estimate warning when off by more than 2 points.
- [ ] Discord posts the yield reminder when a yield-gated trigger is armed and the reading has expired.
- [ ] `AlreadyInZone`, `ZoneSkipped` and STOP MOVED notices appear in Discord when they happen.
- [ ] Restart `main.py` mid-session: the "Restarted: restored trigger state" notice appears, and an OPEN trade keeps its stop.
- [ ] `/eod` writes `context/context_<date>.md`.
- [ ] Early entry: with a plan whose `no_entries_before` is in the future, an alert shows "Early entry — override?"; tapping it opens the reason box; submitting confirms and the reason appears in the message.
- [ ] Alert expiry: leave an alert untapped for 10+ minutes and the "ALERT EXPIRED" notice appears; tapping EXECUTE on it then says Ignored (SKIPPED).
- [ ] Two stops in one day: the 2nd posts the HALT notice and `state/HALT` exists; EXECUTE on a later alert is refused. Delete `state/HALT` by hand to resume.
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
