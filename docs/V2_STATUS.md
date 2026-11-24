# v2 Status & Handoff

**Read this first if you are picking up `release-v2`.** It records where the build stopped, what's decided, what's still open, and what to do next, in order. [SPEC.md](../SPEC.md) is the contract. This file is the to-do list and the decision log.

- **Last updated:** 2026-11-24 (milestone 1 committed as `fd68343`; engine fixes A1-A8 done after it, check `git status` / `git log`)
- **Branch:** `release-v2`, cut from `release-v1` at `f867349`.
- **Tests:** 130 passing (`.venv/Scripts/python.exe -m pytest -q`).
- **Live verification of v2: none yet.** Everything below "done" is unit-tested only.

Keep this file current: tick items off, add decisions with dates, and move anything finished into "Done".

---

## 1. Decisions already made (don't relitigate)

| Date | Decision | Why |
|---|---|---|
| 11-24 | **Claude Code is the analyst; `main.py` is the watcher**, and they share files in `state/`. The setup follows the operator's other repo (CLAUDE.md, skills, commands, `tools/*.py`, settings allow list). | The operator already gets the daily plan from Claude using their 3 skills. v2 keeps that workflow and connects it to the engine. |
| 11-24 | **No subagents for now.** A read-only pre-market researcher might come later, if `/premarket` gets slow. | Speed matters mid-session, and Python validators beat reviewer agents for checking rules. |
| 11-24 | **SPX comes from the Moomoo live feed** (SPY × ratio). The operator sends SPX once, pre-market, from TradingView `SPREADEX:SPX`, as `spx_reference`. The MES calculation is only a fallback. | The operator's own data source. Polygon has no key. |
| 11-24 | **The SPY ratio = previous SPX close ÷ previous SPY close**, not Spreadex or pre-market prints. | v1 calibrated a pre-market estimate against thin pre-market SPY at plan-load time: two different moments, a possible error of 3-5+ points all day. |
| 11-24 | **The 10Y yield is entered by hand only** and expires after 20 minutes. Yield triggers are **level-based** (4.95 / 5.00 / 5.15). | Moomoo has no yields, and direction can't be computed from sparse manual readings. OCR screen reading is optional, for later. |
| 11-24 | **GEX comes from Moomoo Gamma Exposure screenshots** (0DTE expiry), sent repeatedly through `/intraday`. | It fills the "no live GEX" gap in the skills. Suggested rhythm: pre-market, around 10:00, midday, around 14:00, and near the flip or a wall. |
| 11-24 | **The opening wait becomes per-day and soft** (the plan's `no_entries_before`, plus an override button and journal tags), not a hard 30 minutes. | The operator entered at 7,195 before the 30-minute window on 11-23 and questioned the rule. Decide from journal data, not one trade. |
| 11-24 | **The repo copy of the skills is the master.** | They're versioned with the code that reads their output. |
| 11-24 | **The operator always uses `/intraday`, not the bare skill,** for data or GEX screenshots. | Only the command records inputs for `main.py` and offers a replan. |
| 11-24 | **v1 SPEC and ROLLOUT_PLAN archived** to `docs/v1/`. A new v2 `SPEC.md` was written. | — |
| 11-24 | **A jump straight through an entry zone alerts (`ZoneSkipped`) but does NOT fire.** | The first version fired. The 07-01 ISM replay showed it chasing the 7464→7480 spike through a 7465-7472 dip zone instead of waiting for the 7468 pullback. Firing outside the planned zone also breaks "models never originate numbers". |
| 11-24 | **Price already in a zone at first sight (startup, new plan, replan) does not fire (`AlreadyInZone`).** A gate opening with price in the zone still fires. | Don't enter a setup that may be half over. A not_before or yield gate opening is a real "conditions now met". |
| 11-24 | **On restart, restored FIRED triggers are re-armed** (a fresh entry is needed), and Discord is told. | Their alert may never have been sent, and the old buttons no longer apply. |
| 11-24 | **Close-confirmed triggers track closes through closed time gates.** A close-through before `not_before` isn't fired at the gate; a fresh one is needed. The dwell clock also runs through gates, and the trigger fires once the gate opens. | "5-min close above 7,260 after the 30-min wait": if it broke out at 9:45, entering at 10:00 is chasing. A sustained hold is still a hold. |
| 11-24 | **`first_touch` counts touches that happen while a gate is closed, and price already in the zone at startup.** | The rule is "fade the *first* touch". A touch during the opening wait is still the first touch. |
| 11-24 | **Stops default to any tick; `stop_confirm: close` is opt-in per trigger.** | The fastest exit is the safe default for 0DTE. Close-confirmed stops avoid wicks but exit later and have no hard backstop yet. |
| 11-24 | **Breakeven uses the fire price as the entry until the Discord fill-price prompt (C1) exists.** It only ever tightens the stop. | No fill price is captured yet. |

## 2. Done (milestone 1)

**Claude Code workspace:**
- `CLAUDE.md`
- `.claude/skills/` (3 skill copies)
- `.claude/commands/{premarket,intraday,eod}.md`
- `.claude/settings.json`
- `context/context_2026-11-23.md`

**Code:**
- **`inputs.py`:**
  - `YieldInput`, `GexInput`, `normalize_yield`
  - atomic writes, expiry, GEX regime and summary
- **`tools/`:**
  - `write_plan.py`: schema check, grounding check, flat_by ≤ 15:30, `--replan` guard, `--schema`, `--example`
  - `set_yield.py`
  - `set_gex.py`: expiry check, SGT `--as-of`, change diff, regime change
- **`schema.py`:**
  - `Condition.yield_below` / `yield_above`, validated
  - `Plan.spx_reference_source`, `Plan.spx_prev_close`
- **`triggers.py`:** yield level checks, fail closed with a warning.
- **`feed.py`:** `MoomooFeed.calibrate_from_prev_close()` reads SPY's `prev_close_price` from quote rows and records `ratio_source`. The old `calibrate()` is now only a fallback.
- **`main.py`:**
  - the input poller task, independent of ticks
  - the first plan of the day loads automatically and posts its Discord summary
  - yield attached to ticks (manual overrides feed, ×10 values normalized)
  - one yield reminder per lapse
  - the SPX estimate check against the GEX screenshot
  - `decide()` notes: yield, GEX, ratio source, plan notes
  - `_calibrate_feed()`
- **`scheduler.py`:** the 08:00 auto-plan only runs when `PLAN_AUTOGEN=true`.
- **`config.py`:** v2 settings. Default models `claude-sonnet-5-5`. `FEED_SOURCE` defaults to moomoo. `PLAN_RELOAD_POLL_S` replaced by `INPUT_POLL_S`.

**Tests, fixtures and docs:**
- `tests/test_v2_inputs.py` (22 tests)
- fixtures `analysis_2026-11-23.md` and `plan_v2_example.json`
- README v2 quick start
- `.env.example`, `.gitignore`

### Engine fixes (after milestone 1)
- **A1 zone skipped:** `ZoneSkipped` event, no fire, Discord notice. Regression test for the 07-01 replay.
- **A2 first sight:** `TriggerState.observed`, `AlreadyInZone` event, Discord notice. v1 tests that assumed "first tick in zone fires" now start outside the zone.
- **A7 `decide()` in the background:** `Orchestrator._spawn_decide` / `wait_for_decisions`. No alert if the trigger is no longer FIRED when `decide()` returns.
- **A8 persistence:** `TriggerEngine.to_snapshot` / `restore_snapshot`, `state/engine_<date>.json`, saved after ticks and polls and at shutdown. Restored on first plan load, with FIRED re-armed.
- **Fixed along the way:**
  - Plan reload now compares file **contents**, not mtime. The mtime check intermittently missed a replan written inside the timestamp resolution (seen as a flaky test on Windows).
  - Discord buttons for a trigger no longer in the expected state reply "Ignored …" instead of falsely saying "confirmed".
- Tests: `tests/test_v2_engine.py` (16), plus one in `test_notify.py`.

### Trigger types (after the engine fixes)
- **A3 confirmation:**
  - `Confirm {tick | close (1m/5m) | dwell (seconds)}` on `Condition.confirm`, and `Trigger.stop_confirm` (tick or close)
  - engine bar builder `_update_bars`
  - `_close_candidate` / `_dwell_candidate`
- **A4 approach:** `Condition.approach` (from_above / from_below, needs `price_between`).
- **A5 first touch:** `Condition.first_touch`, with `TriggerState.touches` counted ignoring gates.
- **A6 breakeven:**
  - `Target.then_stop="breakeven"` and the `StopMoved` event (Discord: "STOP MOVED")
  - `mark_filled(trigger_id, fill_price=None)`; the entry is the fill price or the fire price
  - `TriggerState.stop` property (override, else `trigger.stop`)
- **Restructure:** `_condition_met` now does its bookkeeping first (touches, close tracking, dwell clock), then `_gates_open`, then picks the tick, close or dwell candidate. Tick-mode behaviour is unchanged (07-01 replay is identical).
- **Plan tooling:**
  - `tools/write_plan.py` prints the new features
  - `plan_v2_example.json` uses all of them
  - `/premarket` maps write-up wording to fields
  - CLAUDE.md's feature list is updated
- Tests: `tests/test_v2_trigger_types.py` (24).

## 3. Open decisions, waiting on the operator

1. **Cleanup of unneeded v1 code.** Proposed, not yet approved. Don't delete until the operator says so.
   - AI plan generation in `agent.py`: `generate_plan`, `replan`, the CLI, the MES helper, `prompts/daily_plan.md`, `prompts/replan.md`. Keep `decide()`.
   - `PLAN_AUTOGEN` and `on_generate_plan`.
   - Release auto-replan: `releases.py`, Finnhub, `on_release_poll`, release jobs, `Plan.scheduled_events`, `test_releases.py`.
   - `PolygonFeed` and `POLYGON_*`.
   - The direction-based yield check: `yields_10y`, `YIELD_WINDOW_MIN`, `YIELD_EPSILON`, `yield_direction`, plus the related tests and fixture fields.
   - Untrack `.claude/scheduled_tasks.lock` (a stray lock file committed in v1) and add it to `.gitignore`.
   - *(Already done: SPEC/ROLLOUT moved to `docs/v1/`.)*
2. **Per-trade dollar cap amount.** The context doc still has the placeholder "[your number, e.g. 1% of account]".
3. **Where VIX comes from**, for VIX size tiers and the "VIX > 30" wait. Options: manual (`/intraday ... vix 18`), read off a screenshot, or Moomoo if the API quotes it (unverified).

## 4. Next tasks, in order

Each item lists what "done" means. Write tests first for engine changes (replay-style, through the real `TriggerEngine`).

### A. Engine correctness (highest priority, affects real money)
1. ~~**Fast move through a whole zone.** (Built as alert-only `ZoneSkipped`, not a fire; see the decision log.) Treat the previous tick on one side and the current tick on the other side of `price_between` as entering it. *Done when:* a 7,220→7,190 jump fires `buy_dip [7195, 7215]`.~~ **Done 11-24** (see §2).
2. ~~**No fire on startup inside a zone.** The first tick after start or (re)arm only records `was_in_zone`, unless the plan opts in. *Done when:* starting with price inside a zone doesn't fire until price leaves and re-enters.~~ **Done 11-24** (see §2).
3. ~~**Confirmation modes.** `Condition.confirm {type: tick | close | dwell, timeframe, seconds}`. Needs a bar builder (1m/5m) in the engine or `main.py`. Stops get their own optional confirmation (default stricter than one tick, configurable). *Done when:* "5-min close above 7,260" is expressible, and a single-tick wick doesn't fire it.~~ **Done 11-24** (see §2).
4. ~~**Direction of approach.** `approach: from_above | from_below`.~~ **Done 11-24** (see §2).
5. ~~**First-touch / stall fade.** `first_touch: true` fires only on the first touch of the session.~~ **Done 11-24** (see §2).
6. ~~**Take half at TP1 → breakeven stop.** `Target.then_stop: "breakeven"` moves the trigger's stop to the entry price after that target. Needs a recorded entry price (see C1).~~ **Done 11-24** (see §2).
7. ~~**`decide()` off the tick loop.** Run it as an `asyncio` task in `main.py`. *Done when:* a 30 s `decide()` doesn't delay stop evaluation on another OPEN trigger (test with a slow fake `decide`).~~ **Done 11-24** (see §2).
8. ~~**Save engine state** (trigger statuses, targets hit, entry and fill prices) to `state/engine_<date>.json` on every change, and restore it on startup. *Done when:* restarting `main.py` mid-trade keeps the trade OPEN with its stop.~~ **Done 11-24** (see §2).

### B. Risk rules (`risk.py`, at the EXECUTE tap)
1. **Two stops ends the day:** count `StopHit` per day and create HALT on the 2nd.
2. **VIX size tiers.** Needs open decision 3. Size above the tier blocks or warns.
3. **Per-trade dollar cap.** Needs open decision 2.
4. **Soft early-entry block:**
   - add `Plan.no_entries_before`
   - before that time, the alert shows "Early entry — override?"
   - the override is journaled with a reason
   - the journal tags each trade early or regular
   - the `/premarket` command sets it (9:30 normally, 9:45-10:00 on data days, gap days or VIX > 30)

### C. Discord (`notify.py`)
1. **EXECUTE asks for the fill price** (a modal), then sets the trade OPEN with an entry price. That replaces Mark Filled and enables A6.
2. **Alerts expire** on a time limit or a maximum distance from the entry; the button is disabled after that.
3. **Stops and invalidations @mention the operator**, with a reminder if an alert stays untapped for N minutes.
4. **Inbound commands:** `!yield 4.93` → `inputs.write_yield`. Posting a GEX screenshot → run the intraday flow (needs Agent SDK image input with `setting_sources=["project"]` and the Skill tool; check the current SDK docs).
5. **Alerts show "SPX ≈ x (SPY est.)"**, with an optional 1-2 point buffer on tight levels.

### D. `decide()` context
- Add the day's `analyses/<date>.md` (levels table, scenarios, trade bias), the trading rules from CLAUDE.md, the last 5-minute bars and VIX.
- Update `prompts/decide.md` to use them.

### E. Feed
1. **SPY ex-dividend handling.** On ex-dividend dates, add the dividend back to SPY's previous close, or allow a manual override.
2. Don't recalibrate between 9:30 and 9:35 ET.
3. Optional: recalibrate intraday from the `spx_in_shot` of a fresh GEX screenshot when drift exceeds 2 points (with the operator's OK).

### F. After A-C: cleanup and docs
- Carry out the approved cleanup list (§3.1).
- Update SPEC.md sections marked **[planned]** as each item lands.
- Update CLAUDE.md "What the engine can't express yet".

## 5. Live verification checklist (the operator runs these; record results here)

- [ ] `.venv/Scripts/python.exe main.py` with Moomoo OpenD running pre-market: SPY ticks flow, and the log shows `ratio_source` = **prev closes** after a plan with `spx_prev_close` loads. **Unverified: whether `prev_close_price` is present in extended-hours push rows.** If it's missing, `ratio_source` stays on the `spx_reference` fallback, and `calibrate_from_prev_close` needs to fetch it with `get_market_snapshot`.
- [ ] `/premarket <price>` end to end: the analysis is shown, `analyses/<date>.md` is written, the plan is installed, `main.py` logs "loaded today's plan" and Discord gets the summary.
- [ ] `/intraday 7268 4.93`: `state/yield.json` is written, and `main.py` logs "10Y yield updated".
- [ ] `/intraday` with a GEX screenshot: `state/gex.json` is written, the change diff is shown, and the SPX estimate check fires when SPX is off by more than 2 points.
- [ ] The yield reminder arrives in Discord when a yield-gated trigger is armed and the reading has expired.
- [ ] `/eod` writes `context/context_<date>.md`.
- [ ] From v1, still unverified: continuous RTH ticks, reconnect with backoff, the dead-man rule firing (see `docs/v1/ROLLOUT_PLAN.md` Step 4).

## 6. Environment notes / gotchas

- **Python venv:** `.venv/` in the repo (Python 3.12, `C:\Users\...\Programs\Python\Python312`). The Microsoft Store `python` on PATH has no packages, so always use `.venv/Scripts/python.exe`.
- **pip on this network** fails SSL verification because of corporate SSL inspection. Don't disable verification. Export the Windows trusted roots to a PEM (`ssl.enum_certificates("ROOT"/"CA")`), then set `PIP_CERT=<pem>` (the environment variable, so isolated builds see it too) before `pip install -r requirements.txt`.
- **The Windows console mangles non-ASCII** (em dashes). Keep `tools/` output ASCII.
- **`.claude/settings.json`** denies reading `.env` / `.env.local` and editing `state/` by hand. Don't widen the deny rules to `.env.*`: that blocks `.env.example`.
- **Moomoo:**
  - can't subscribe to SPX, MES/ES (paid quote card) or yields
  - `US.ES` resolves to Eversource Energy
  - the `.SPX` quote repeats the previous close before 09:30
- **The operator works in SGT.** Screenshot clocks are SGT: 21:30/22:00 SGT = 8:30/10:00 ET until 2027-03-14.
- **Skills:** the repo copies under `.claude/skills/`. The operator's claude.ai-synced copies also appear as `spx-*:spx-*`. Use the repo ones.
