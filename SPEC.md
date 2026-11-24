# 0DTE SPX Trading Agent — v2 Spec

This is the contract for `release-v2`. Code that disagrees with it is a bug, unless the spec is out of date. In that case, fix the spec first, then the code.

- **Where the build stands and what to do next:** [docs/V2_STATUS.md](docs/V2_STATUS.md). Read it before starting work.
- **v1 design and verification history:** [docs/v1/SPEC.md](docs/v1/SPEC.md) and [docs/v1/ROLLOUT_PLAN.md](docs/v1/ROLLOUT_PLAN.md).

Sections marked **[planned]** describe agreed v2 behaviour that isn't built yet. Everything else is implemented and covered by tests.

---

## 1. What it is

An **alerts-only** assistant for trading SPX 0DTE options. It never places orders: every entry needs the operator's tap in Discord.

v2 splits the work in two:

| Half | Runs as | Job |
|---|---|---|
| **Analyst** | Claude Code desktop, in this repo (`CLAUDE.md`, `.claude/`) | Pre-market analysis and the day's plan, intraday reads, end-of-day context update. Uses the operator's 3 skills. |
| **Watcher** | `python main.py`, in its own terminal, all session | Streams SPX, evaluates the plan's triggers, asks `decide()` when one fires, posts Discord alerts with EXECUTE/SKIP buttons, journals everything. |

**The two halves only communicate through files in `state/`**, written by `tools/*.py`. Neither calls the other.

### Operator workflow (times in ET; the operator is in Singapore, SGT = ET+13 until 2027-03-14, then ET+12)

1. **Before the open:** start `main.py`, then run `/premarket <SPREADEX:SPX price>` in Claude Code, optionally with a Moomoo GEX screenshot. The plan is installed and `main.py` picks it up and posts a summary.
2. **During the session:** run `/intraday <SPX> <10Y>`, or `/intraday` with a data or GEX screenshot. This records the 10Y and GEX and gives a short read. If the bias changed, it offers a replan, which happens only after the operator says yes.
3. **Discord alerts:** EXECUTE/SKIP on each trigger, then Mark Filled, then target, stop and flat-by alerts.
4. **After the close:** `/eod` writes the next context doc.

## 2. Design principles (non-negotiable)

1. **Code decides WHEN, the model decides WHETHER.** Triggers, stops, targets, times and risk limits are plain code. `decide()` only picks EXECUTE / SKIP / WAIT for an action that's already fully specified.
2. **Models never originate numbers.**
   - Every level in a plan must appear in that day's analysis write-up; `tools/write_plan.py` enforces this.
   - `date` and `generated_at` are stamped by code.
   - `decide()` can't change levels, size or instrument.
3. **Hard guardrails live in code, not prompts:** the HALT file, the daily loss limit, max open trades, the 15:00 entry cutoff, and `flat_by` ≤ 15:30.
4. **Fail closed.**
   - A missing or expired input (yield, plan) blocks the triggers that depend on it, never the other way round.
   - `decide()` errors become SKIP.
   - Unparseable input files are ignored and retried.
5. **Never go dark.** A risk block refuses the EXECUTE tap with a reason; it doesn't suppress the alert.
6. **Times are `America/New_York`** via `zoneinfo`, never naive datetimes. Plan times are ET `HH:MM`.
7. **Every input carries a timestamp and expires.** An old value is treated as missing, never as current.
8. **Testable offline.** No test may need the network, Moomoo, Discord or a model.

## 3. Repository layout

```
CLAUDE.md                 Claude Code's standing instructions (analyst role, rules, files)
SPEC.md                   this file
docs/V2_STATUS.md         build status + handoff for the next agent
docs/v1/                  archived v1 spec and rollout plan
.claude/
  skills/spx-*/           repo copies of the operator's 3 skills (repo copy is the master)
  commands/               /premarket, /intraday, /eod
  settings.json           allow list for tools/*.py; denies .env and hand edits to state/
context/context_<date>.md rolling trading context doc (newest = current)
analyses/<date>.md        that day's pre-market write-up (+ intraday updates); gitignored
tools/
  write_plan.py           validate + ground + install a plan
  set_yield.py            record a manual 10Y reading
  set_gex.py              record GEX levels from a screenshot
inputs.py                 yield.json / gex.json models, atomic read/write, expiry
main.py                   watcher: feed -> engine -> decide -> notify -> journal; input poller
triggers.py               deterministic trigger engine (pure, no I/O)
schema.py                 pydantic models: Plan, Trigger, Condition, events
agent.py                  decide() (+ v1 plan generation, slated for removal)
feed.py                   MoomooFeed (SPY x ratio), PolygonFeed (v1, unused), FeedManager
risk.py                   RiskManager.gate_execute()
notify.py                 Discord bot: alerts + buttons
journal.py                SQLite journal
scheduler.py              cron jobs: entry-cutoff notice, EOD summary (+ v1 release polls)
replay.py                 offline replay harness (main.py --sim)
releases.py               v1 econ-calendar polling, slated for removal
prompts/decide.md         decide() prompt (daily_plan.md / replan.md are v1, slated for removal)
state/                    runtime files (gitignored): plan_<date>.json, yield.json, gex.json, journal.db, HALT
daily_plans/              archive copy of every installed plan (gitignored)
tests/                    pytest; fixtures/ has the Nov 23 analysis + plan_v2_example.json
```

## 4. File contracts (`state/`)

Writers always go through `tools/*.py` / `inputs.py`, which validate and write atomically (temp file + `os.replace`). `main.py` polls `state/` every `INPUT_POLL_S` (5 s) on its own timer, not on ticks.

| File | Writer | Reader | Contents | Freshness |
|---|---|---|---|---|
| `plan_<date>.json` | `tools/write_plan.py` | `main.py` (hot-swap) | `schema.Plan` | Lives for the day. Replacing it requires `--replan`. |
| `yield.json` | `tools/set_yield.py` | `main.py` | `inputs.YieldInput {value %, source, ts}` | Missing after `YIELD_MAX_AGE_MIN` (20) min |
| `gex.json` | `tools/set_gex.py` | `main.py` → `decide()` | `inputs.GexInput {expiry_date, gamma_flip, call_wall, put_wall, max_pain, spx_in_shot, as_of, source, notes}` | Shown as STALE after `GEX_MAX_AGE_MIN` (90) min |
| `journal.db` | `main.py`, `notify.py` | operator / reviews | plans, events, decisions, releases | — |
| `HALT` | operator or `risk.py` | `risk.py` | presence = no new entries | until removed |

## 5. Plan schema (`schema.py`)

See `python tools/write_plan.py --schema`, and `--example` for a worked plan built from the 2026-11-23 analysis.

- **Plan:**
  - `date`, `generated_at` (stamped by `write_plan.py`)
  - `bias` (bullish | bearish | neutral)
  - `spx_reference` (the operator's TradingView `SPREADEX:SPX` price)
  - `spx_reference_source` (e.g. `"spreadex 7250 @ 09:27 ET"`)
  - **`spx_prev_close`** (official previous SPX close; drives feed calibration)
  - `triggers[]`
  - `invalidation {below?, above?, action}`
  - `flat_by` (HH:MM ET, ≤ 15:30)
  - `scheduled_events[]` (v1, slated for removal)
  - `notes`
- **Trigger:** `id`, `side` (long | short), `condition`, `action {instrument, size_pct}`, `targets[] {level, scale_out_pct}` (summing to 100±1), `stop` (on the correct side of the entry zone), `one_shot`.
- **Condition** (all optional, ANDed together):
  - price: `price_between [lo, hi]`, `price_above`, `price_below`
  - yield levels, in percent: **`yield_below`**, **`yield_above`**, each validated to 0.5-15 with above < below
  - `yields_10y` (falling | rising; v1, needs a live yield stream, slated for removal)
  - time: `not_before`, `not_after`
  - `requires_not_filled[]`
- **v2 condition fields:**
  - `confirm {type: tick | close | dwell, timeframe: 1m | 5m (close only), seconds (dwell only)}`. Default: tick.
  - `approach: from_above | from_below`, which needs `price_between`.
  - `first_touch: bool`. `confirm` and `first_touch` need a price condition.
- **v2 trigger and target fields:**
  - `Trigger.stop_confirm` (tick or close only; default tick)
  - `Target.then_stop: "breakeven"`
- **[planned]:** plan-level `no_entries_before`.

## 6. Trigger engine (`triggers.py`)

Pure and synchronous. Ticks in, events out. Per-trigger state machine: `ARMED → FIRED → (CONFIRMED → OPEN → CLOSED) | SKIPPED | EXPIRED`.

Events: `TriggerFired`, `TargetHit`, `StopHit`, `Invalidated`, `FlatByReached`, `ReviewRequired`, **`AlreadyInZone`**, **`ZoneSkipped`**, `EngineWarning`.

**Current rules:**
- **Entry zone:** fires on a tick inside the zone when the previous tick that reached the price check was outside it, or when a time or yield gate has just opened.
- **First sight (v2):** the first time the engine evaluates a trigger (at startup, on a new plan, or when a replan adds it), price already inside the zone does **not** fire. It emits `AlreadyInZone` once, and the trigger fires only after price leaves and re-enters. "First sight" is recorded before any gate, so a trigger first seen at 9:50 with `not_before` 10:00 still fires at 10:00 if price is in the zone then.
- **Zone skipped (v2):** if price moves from one side of a `price_between` zone to the other between two ticks without trading inside it, the trigger **does not fire**. Price is then outside the planned entry zone, and firing would mean chasing, as with the 2026-07-01 ISM spike 7464→7480 through the 7465-7472 dip zone. It emits `ZoneSkipped`, and a later move back into the zone fires normally. Jumps while a gate is closed are silent.
- **Stops, targets and invalidation:** checked as crossings between consecutive ticks, so a gap through a level still emits the event.
- **Yield conditions:** level conditions use the tick's `yield_10y`. `main.py` only attaches a fresh manual yield, so `None` means missing and the condition fails closed with one `EngineWarning` per lapse.
- **Requirements:** `requires_not_filled` blocks firing while a referenced trigger is FIRED, CONFIRMED or OPEN. `not_after` expires the trigger, and WAIT re-arms it with a 5-minute cooldown.
- **After `Invalidated`:** only stops on OPEN trades are evaluated.
- **Hot-swap (`load_plan`):**
  - FIRED, CONFIRMED and OPEN state is kept for trigger ids present in both plans.
  - An OPEN trigger the new plan drops emits `ReviewRequired`.
  - It clears an invalidation and returns True, and `main.py` alerts on that.

- **Persistence (v2):** `to_snapshot()` / `restore_snapshot()` save and restore per-trigger status, debounce and first-sight flags, targets hit, cooldowns, and the invalidation and flat-by flags. A snapshot from another plan date is ignored. An in-flight trigger missing from the current plan is restored from its saved definition.

**Trigger types (v2):**
- **Bars:** the engine builds clock-aligned 1m and 5m bars in ET from ticks. A bar's close is the last tick in it, known when the next bar's first tick arrives. Sparse ticks delay a close; they never invent one.
- **`confirm: close`:** the zone is judged on bar closes.
  - Fires on the first close inside after a close outside, or after the first-sight price outside. Intrabar wicks don't count.
  - Closes are tracked **through closed gates**, so a close-through that happened before `not_before` is not fired late when the gate opens. It needs a fresh close-through.
  - At first sight, the tick price seeds the state (`AlreadyInZone` if inside).
- **`confirm: dwell`:** price must stay in the zone for `seconds` after a fresh entry.
  - The clock resets when price leaves, and it runs through closed gates.
  - The trigger fires on the first tick with gates open once the dwell is satisfied.
- **`approach`:** for tick mode, the previous tick must be beyond the zone on that side. For close mode, the previous close. For dwell, the tick before the entry; an entry from the wrong side doesn't start the clock.
- **`first_touch`:** entries into the zone are counted on tick prices, ignoring gates. Already inside at first sight counts as a touch, and so does a jump through the zone. The trigger fires only while it's on touch #1.
- **`then_stop: breakeven`:**
  - When that target is hit, the stop moves to the entry price (`StopMoved` event). It only ever tightens.
  - Entry = `mark_filled(fill_price)`, or the fire price if no fill was given (C1 will supply fills).
  - With no entry price known, it emits `EngineWarning` and leaves the stop unchanged.
- **`stop_confirm: close`:** a stop counts only when a 1m or 5m bar closes beyond it. The default tick stop exits on the first tick through, which is the fastest. There's no hard backstop for close-confirmed stops yet.

**[planned] (see V2_STATUS):** a "stall"/rejection pattern; plan-level `no_entries_before` with the early-entry override.

## 7. Watcher (`main.py`)

- **Startup:** load `state/plan_<today>.json` if it exists, start the scheduler, the feed and the **input poller** task.
- **Engine state persistence:**
  - After every tick, every poll and at shutdown, `main.py` writes `state/engine_<date>.json` if the snapshot changed. Polling catches Discord taps.
  - On the first plan load of a process it restores that file.
  - Restored **FIRED** triggers are re-armed, with a fresh entry required, because their alert may never have gone out. A Discord notice says so.
  - Not used in `--sim`.
- **Input poller (every 5 s):**
  - reload the plan if the file **contents** changed. It compares contents, not modification time, because two writes inside the filesystem's timestamp resolution share an mtime. A first plan of the day gets a Discord summary; a replan gets a diff.
  - reload `yield.json` and `gex.json`.
  - check the SPX estimate against the screenshot price (when a GEX screenshot is ≤ 3 min old and differs by more than `SPX_ESTIMATE_DRIFT_WARN_PTS`, default 2, post a Discord warning).
  - during RTH, if an ARMED trigger has a yield condition and the yield is missing or expired, post **one yield reminder per lapse**.
- **Tick path:**
  1. Attach the yield. A fresh manual yield overrides the feed's. A feed yield quoted TNX-style (×10) is normalized. An invalid one is dropped.
  2. `engine.evaluate()`.
  3. Journal and handle each event.
- **TriggerFired:** `decide()` gets `market_context.notes`:
  - the manual yield, its age and whether it's fresh
  - the GEX summary and regime relative to the flip
  - the SPY ratio and where it came from
  - the plan notes

  WAIT re-arms the trigger; otherwise Discord gets an alert with EXECUTE/SKIP.
- **`decide()` runs as a background task (v2),** so stops and targets on other trades keep being checked during its up-to-30 s call. When it returns, the alert is sent only if the trigger is still FIRED; a replan may have dropped or reset it. `wait_for_decisions()` drains pending calls (end of `--sim`, tests). Shutdown cancels them.
- **`AlreadyInZone` and `ZoneSkipped`** each post a short Discord notice. **`StopMoved`** posts "STOP MOVED … move your stop order to match".
- `PLAN_AUTOGEN=false` (the default) disables v1's 08:00 auto-plan.

## 8. Feed (`feed.py`)

- **`MoomooFeed`** (the default, `FEED_SOURCE=moomoo`). Moomoo's OpenAPI can't subscribe to US indices, so it subscribes to `US.SPY` with extended hours and computes **SPX ≈ SPY × ratio**.
- **Calibration priority:**
  1. **`calibrate_from_prev_close(plan.spx_prev_close)`:** ratio = SPX previous close ÷ SPY `prev_close_price` from the quote rows. Two official 4 PM prints. Replans with the same previous close don't change it.
  2. Fallback when the plan has no `spx_prev_close`: v1's `calibrate(spx_reference)`, which uses the latest SPY print. It's less accurate, and it's ignored once (1) is set.

  `ratio_source` records which method set the ratio.
- **Known gaps:**
  - SPY ex-dividend days aren't adjusted (error about 0.2%).
  - That `prev_close_price` is present in push rows during extended hours is **unverified live**.
- **Not available on the operator's Moomoo account:**
  - SPX index quotes (an API restriction)
  - MES/ES futures (they need a paid quote card)
  - Treasury yields (the API doesn't cover them)

  `US.ES` without `main` resolves to an unrelated stock.
- `FeedManager`: dead-man rule. No tick for more than 10 s during RTH emits `FeedStale`, which triggers a Discord alert.
- `PolygonFeed` (`I:SPX`, `I:TNX`) is v1 code, never verified (no key), and slated for removal.

## 9. Operator inputs (`inputs.py`, `tools/`)

- **10Y yield:**
  - Entered by hand only.
  - `normalize_yield()` scales TNX-style values (above 20, divided by 10) and rejects anything outside 0.5-15%.
  - Expires after 20 minutes.
  - "Falling/rising" doesn't work on sparse manual readings, so plans use the level checks: ≤4.95 supportive, ≥5.15 bearish, 5.00 pivot.
- **GEX:** from Moomoo app → `.SPX` → Options → Analysis → Gamma Exposure, with **Expiration = today**.
  - `set_gex.py` rejects any other expiry unless `--allow-other-expiry` is passed.
  - `--as-of HH:MM` is the screenshot's clock time, in SGT by default. A future time rolls back one day.
  - It prints what changed and `REGIME CHANGE` when SPX crosses the flip.
  - Regime: SPX below the flip is negative gamma (moves extend); above it is positive gamma (pinning). Max pain is low weight.
- **Not planned (11-24):** Discord `!yield` and GEX image intake. Discord is for alerts only; inputs go through `/intraday`. An optional OCR screen reader writing `yield.json` with `source: "ocr"` remains a later idea.

## 10. Plan installation (`tools/write_plan.py`)

Pipeline: the analysis is shown in chat and saved to `analyses/<date>.md`, Claude drafts the JSON, and `write_plan.py` validates and installs it.

`write_plan.py`:
- stamps `date` and `generated_at`
- validates the plan against the schema
- **grounding:** every price or yield level must appear as a number in the analysis (thousands separators are ignored; sizes and scale-out percentages are exempt)
- rejects `flat_by` later than 15:30
- refuses to replace today's plan without `--replan`
- writes `state/` and `daily_plans/` atomically and prints one line per trigger

## 11. `decide()` (`agent.py`)

- **Transport:** Claude Agent SDK on the operator's subscription (`claude login`). `ANTHROPIC_API_KEY` is scrubbed from the environment. No tools.
- **Output:** forced JSON schema `{action: EXECUTE | SKIP | WAIT, reasoning, confidence 0-100}`.
- **Model and limits:** `DECIDE_MODEL` (default `claude-sonnet-5-5`), `DECIDE_EFFORT`, 30 s timeout. Any error or invalid output becomes SKIP.
- **Prompt:** `prompts/decide.md`. Context: the trigger JSON, bias, invalidation, other trigger statuses, spot, ET and SGT time, the 10Y and its direction, and the notes (§7).
- **[planned]** add to the context:
  - the day's analysis write-up and levels table
  - the trading rules
  - recent 5-minute bars

## 12. Risk (`risk.py`)

`gate_execute(engine, now)` runs at the **EXECUTE tap** and blocks on:
- the HALT file
- the daily realized-loss limit (which auto-creates HALT). **Inert by choice:** nothing feeds `record_realized_pnl()` in production, and the operator decided on 11-24 not to report P&L.
- max open trades (`MAX_OPEN_TRIGGERS`, 1)
- the 15:00 ET entry cutoff

`flat_by` ≤ 15:30 is enforced at plan install.

**Two stops ends the day (v2):** `main.py` calls `RiskManager.record_stop()` on every `StopHit`. The count is per ET day (by the tick's time). The stop that reaches `MAX_STOPS_PER_DAY` (2) creates `state/HALT` and posts "**HALT** — 2 stops: no new entries today (state/HALT)". At startup, `run_live()` rebuilds the count from the journal's `stop_hit` events since 00:00 ET, so a restart after one stop still halts on the next. The rebuild never creates the HALT itself. If the operator deletes the HALT, a later stop doesn't re-create it (only the one that reaches the limit does).

**Not in code, by decision (11-24):** VIX size tiers and the VIX > 30 wait are applied in the `/premarket` write-up and the plan's `size_pct` / `not_before`. There's no per-trade dollar cap.

**[planned]:**
- **A soft early-entry block:** the plan sets `no_entries_before`; a trigger before it shows "Early entry — override?"; the override and reason are journaled; trades are tagged early or regular.

## 13. Discord (`notify.py`)

Gateway bot, no inbound ports. Alerts include price, ET and SGT time, the trigger id and the agent's reasoning.

Buttons: EXECUTE (re-checks risk), SKIP, Mark Filled. Stop and target tracking starts only after Mark Filled.

**Stale buttons (v2):** a tap whose trigger is no longer in the expected state (EXECUTE/SKIP need FIRED, Mark Filled needs CONFIRMED) is answered with "Ignored: … no longer awaiting this (STATUS)". It no longer falsely reports success. This happens after a replan or a restart that re-armed the trigger.

**[planned]:**
- EXECUTE asks for the fill price and opens the trade, replacing Mark Filled.
- Alerts expire on a time limit or when price is too far from the entry.
- Stops and invalidations @mention the operator, with a reminder if an alert goes untapped.
- Inbound `!yield` and screenshots.

## 14. Claude Code workspace

- `CLAUDE.md` holds the role, file contracts, trading rules and hard rules (never invent levels, never hand-edit `state/`, never read `.env`, replans need the operator's OK).
- **Commands** (`.claude/commands/`):
  - `premarket.md`: context → GEX → skill analysis shown in chat → `analyses/` → draft JSON → `write_plan.py` → trigger list
  - `intraday.md`: record inputs first → skill read → replan only on a changed bias and the operator's yes
  - `eod.md`: skill → new `context/context_<date>.md`; lessons only with the operator's confirmation
- The skills are repo copies of the operator's claude.ai skills (`spx-premarket-analysis`, `spx-intraday-read`, `spx-context-update`). **The repo copy is the master**, and claude.ai edits must be copied over.

## 15. Config (`.env`, see `.env.example`)

| Group | Settings |
|---|---|
| Discord | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` |
| Feed | `FEED_SOURCE=moomoo`, `MOOMOO_HOST`, `MOOMOO_PORT`, `MOOMOO_SPY_CODE`, `MOOMOO_SPY_SPX_RATIO` (initial guess) |
| Risk | `DAILY_LOSS_LIMIT_USD`, `MAX_OPEN_TRIGGERS` |
| Model | `DECIDE_MODEL`, `PLAN_MODEL`, `DECIDE_EFFORT`, `PLAN_EFFORT` |
| v2 inputs | `YIELD_MAX_AGE_MIN` (20), `GEX_MAX_AGE_MIN` (90), `SPX_ESTIMATE_DRIFT_WARN_PTS` (2), `INPUT_POLL_S` (5), `PLAN_AUTOGEN` (false) |
| v1, slated for removal | `POLYGON_*`, `FINNHUB_*`, `YIELD_WINDOW_MIN`, `YIELD_EPSILON` |

## 16. Testing

- Run with `.venv/Scripts/python.exe -m pytest -q`. 130 tests as of `16f1219`.
- **Engine replay check:** the 07-01 fixture must give `zone_skipped` at 7480, then a fire at 7468 and three targets. See V2_STATUS §6 for the exact command.
- `tests/test_v2_inputs.py` covers the v2 inputs, yield-level conditions, calibration, `write_plan`/`set_gex`, and the poller, reminder and `decide()` notes.
- Each engine change needs replay-style tests through the real `TriggerEngine`. Each new rule needs a test that shows it **blocking**, not only one that shows it passing.
