# 0DTE SPX Trading Agent — Implementation Spec

Local, single-process trading assistant that (1) generates a structured daily trade plan via the Claude API, (2) watches live SPX prices against that plan with a deterministic trigger engine, (3) re-plans automatically when scheduled economic data releases, and (4) sends decisions to Discord for tap-to-confirm execution. **Phase 1 is alerts-only — no order routing.**

---

## Context

- Operator runs a manual pre-market 0DTE SPX analysis (GEX, macro catalysts, sentiment) producing a prose plan with entry zones, targets, stops, and scenario probabilities.
- Goal: automate the mechanical parts — level monitoring, data-release re-planning, decision logging — while keeping the operator in the loop for execution.
- Operator is in Singapore (SGT); market hours are US ET. **All internal time handling must be in `America/New_York` via `zoneinfo`.** Never use naive datetimes or local system time.

## Design principles (non-negotiable)

1. **Deterministic layer decides WHEN, LLM decides WHETHER.** Price triggers, stops, targets, cutoff times, and risk limits are plain code. The LLM is only invoked at decision points and only chooses between pre-defined actions.
2. **The LLM never originates numbers.** All prices, strikes, sizes, and levels come from the market data feed or the structured plan JSON. Reject/clamp any LLM output containing levels not present in the plan.
3. **Hard risk guardrails live outside the LLM** as code: daily loss limit, max concurrent triggers, no new entries after 15:00 ET, kill switch via `state/HALT` file.
4. **Everything is testable offline** via a tick-replay harness before any live connection.

## Non-goals (Phase 1)

- No automated order execution (alerts + confirm buttons only).
- No sub-second latency requirements (level trading, not HFT).
- No GEX computation from raw options chains (plan generation prompt handles GEX narrative; a data integration can come later).

---

## Repository layout

```
trading-agent/
├── main.py              # asyncio entrypoint: supervises tasks, wires modules
├── config.py            # env/config loading (.env), constants
├── schema.py            # pydantic v2 models: Plan, Trigger, Condition, Action, events
├── triggers.py          # deterministic trigger engine (pure, no I/O)
├── agent.py             # Claude API: plan generation, decision calls, re-plan calls
├── scheduler.py         # APScheduler jobs: pre-market, release polls, 15:00 cutoff
├── feed.py              # market data websocket client — async tick stream
├── releases.py          # econ calendar polling at release times
├── risk.py              # guardrails: HALT file, loss limits, entry cutoff
├── notify.py            # Discord bot: alerts, inline confirm/skip buttons
├── journal.py            # SQLite logging: every tick-trigger-decision-outcome
├── replay.py            # offline harness: replay ticks from CSV against a plan
├── prompts/
│   ├── daily_plan.md    # pre-market plan generation prompt (operator's existing prompt, adapted)
│   ├── replan.md        # data-release re-plan prompt
│   └── decide.md        # trigger-fired decision prompt
├── state/
│   ├── plan_YYYY-MM-DD.json   # engine-canonical plan — this is what M3's scheduler/engine reads
│   ├── journal.db
│   └── HALT             # presence of this file = kill switch
├── daily_plans/
│   └── plan_YYYY-MM-DD.json   # human-readable journal archive of every generated plan (dry-run or not); never read by the engine
├── tests/
│   ├── test_triggers.py
│   ├── test_schema.py
│   └── fixtures/ticks_sample.csv
├── .env.example
├── requirements.txt
└── README.md
```

Dependencies: `claude-agent-sdk`, `pydantic>=2`, `apscheduler>=3.10`, `websockets`, `httpx`, `aiosqlite`, `discord.py`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`.

---

## Plan JSON schema (`schema.py`)

Pydantic v2 models. The daily plan the LLM must emit (via forced JSON / tool-use output):

```json
{
  "date": "2026-07-01",
  "generated_at": "2026-07-01T08:02:11-04:00",
  "bias": "bullish",
  "spx_reference": 7465.0,
  "triggers": [
    {
      "id": "primary_long",
      "side": "long",
      "condition": {
        "price_between": [7465.0, 7472.0],
        "yields_10y": "falling",
        "not_after": "15:00",
        "requires_not_filled": []
      },
      "action": {
        "instrument": "SPX 0DTE 7495C",
        "size_pct": 50
      },
      "targets": [
        {"level": 7498.0, "scale_out_pct": 40},
        {"level": 7515.0, "scale_out_pct": 35},
        {"level": 7527.0, "scale_out_pct": 25}
      ],
      "stop": 7450.0,
      "one_shot": true
    },
    {
      "id": "fade_extension",
      "side": "short",
      "condition": {
        "price_between": [7497.0, 7512.0],
        "requires_not_filled": ["primary_long"],
        "not_after": "15:00"
      },
      "action": {"instrument": "SPX 0DTE 7480P", "size_pct": 50},
      "targets": [{"level": 7470.0, "scale_out_pct": 60}, {"level": 7455.0, "scale_out_pct": 40}],
      "stop": 7525.0,
      "one_shot": true
    }
  ],
  "invalidation": {"below": 7408.0, "action": "flatten_and_halt"},
  "flat_by": "15:00",
  "scheduled_events": [
    {"time": "10:00", "release": "ISM Manufacturing PMI", "source_id": "finnhub:ism-mfg", "replan": true}
  ],
  "notes": "free-text rationale, not machine-read"
}
```

Model requirements:

- `Condition` fields, all optional, ANDed together: `price_between: tuple[float, float]`, `price_above: float`, `price_below: float`, `yields_10y: Literal["falling","rising"] | None`, `not_before: str (HH:MM ET)`, `not_after: str`, `requires_not_filled: list[str]` (trigger ids that must NOT have fired/filled).
- Validators: `price_between` low < high; `targets` scale_out_pct sums to 100 ± 1; `stop` on the correct side of the entry zone for the trigger's `side`; trigger ids unique; `requires_not_filled` references existing ids.
- Times are ET wall-clock strings in the plan; parse against `America/New_York` for the plan's `date`.
- Also define runtime models: `Tick {ts, price, yield_10y: float | None}`, and engine output events (see below).

## Trigger engine (`triggers.py`)

Pure, synchronous, no I/O — takes ticks, returns events. Fully unit-testable.

State machine per trigger: `ARMED → FIRED → (CONFIRMED → OPEN → managing targets/stop → CLOSED) | SKIPPED | EXPIRED`.

```python
class TriggerEngine:
    def __init__(self, plan: Plan): ...
    def evaluate(self, tick: Tick) -> list[EngineEvent]: ...
    def confirm(self, trigger_id: str) -> None       # operator tapped EXECUTE
    def skip(self, trigger_id: str) -> None          # operator tapped SKIP
    def mark_filled(self, trigger_id: str) -> None   # position opened (manual in Phase 1)
    def load_plan(self, plan: Plan) -> bool          # hot-swap on re-plan (see rules below); True if it cleared an invalidation
```

Event types (`EngineEvent` union):

- `TriggerFired(trigger_id, tick)` — condition met, request decision. One-shot: never re-fires unless engine explicitly re-arms.
- `TargetHit(trigger_id, level, scale_out_pct, tick)` — for OPEN positions.
- `StopHit(trigger_id, tick)`
- `Invalidated(level, tick)` — plan invalidation crossed — emit + engine enters HALT (no further fires).
- `FlatByReached(tick)` — first tick at/after `flat_by` with any OPEN position.

Behavioral rules:

- **Yield direction**: maintain rolling window of yield ticks (default 5 min); `falling` = current < window mean − epsilon (epsilon = 0.005, configurable). If a condition needs yields but no yield data has arrived, the condition is NOT met (fail-closed) — log a warning event.
- **Zone entry debounce**: fire on the first tick inside `price_between`, but require the previous tick to be outside the zone OR this to be the first tick seen (prevents refire on load).
- **Gap-through handling**: if price crosses a stop/target/invalidation level between ticks (previous tick one side, current tick the other) without printing inside it, still emit the event at the current tick.
- **Hot-swap on re-plan**: keep state for triggers whose id exists in both plans (FIRED/OPEN status preserved; new levels apply); drop removed triggers unless OPEN — an OPEN trigger removed by re-plan emits a `ReviewRequired` event, never silently drops. New triggers start ARMED.
- **After `Invalidated` or HALT-file detection**: evaluate() returns only stop/flatten-relevant events, never new `TriggerFired`.
- **`load_plan()` clears an active invalidation.** `invalidated` is otherwise permanent for the engine's lifetime — the deliberate design for the automatic case (a plan invalidating mid-day should not quietly re-arm itself). But a re-plan is an explicit operator decision that the old plan (and its invalidation) no longer holds, so `load_plan()` resets both `invalidated` and `_flat_by_reached` and returns `True` when it did. Both replan paths in `main.py` (`on_release_poll` and the manual-replan hot-swap in `_reload_plan_if_file_changed`) alert on Discord when this happens ("**Invalidation cleared by re-plan**") — clearing a flatten-and-halt is never silent. Without this, a re-plan issued after an `INVALIDATION CROSSED` alert would load fine but its triggers would sit `ARMED` forever and never fire, with no error. Tests: `test_load_plan_clears_invalidation_and_reports_it`, `test_load_plan_reports_false_when_nothing_was_invalidated` in `test_triggers.py`.

## Agent module (`agent.py`)

**Transport: Claude Agent SDK (`claude-agent-sdk`), authenticated via the operator's Claude subscription (`claude login`) rather than a metered API key.** This is a deliberate M2 decision, not the originally-spec'd raw Messages API — see the M2 conversation log for the reasoning (billing pool tradeoffs, ToS scope for unattended automation). Consequences of the swap:

- All three calls are `async def` (the SDK's `query()` is an async generator); `main.py`'s asyncio event loop is a natural fit in M3.
- `generate_plan`/`replan` still use the prose-then-fenced-JSON prompt format (not tool-use/`output_config.format`, since the daily-plan prompt is prose + JSON, not pure JSON) and validate against `schema.py` exactly as before.
- `decide` uses the SDK's `output_format={"type": "json_schema", ...}` (mirrors the Messages API shape) for forced structured output.
- The model is granted only the `WebSearch` tool for `generate_plan`/`replan`, and no tools at all for `decide` — never Bash/Read/Write/Edit — since this runs unattended.
- Every entry point scrubs `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` from the environment before invoking the SDK, since either would silently shadow subscription auth for the spawned `claude` subprocess.
- Requires the Claude Code CLI on `PATH` (`npm install -g @anthropic-ai/claude-code`) in addition to the `claude-agent-sdk` PyPI package.

Three calls, all with structured output enforced where the prompt format allows it:

1. `generate_plan(context) -> Plan` — pre-market. Prompt = `prompts/daily_plan.md` + web-fetched context the orchestrator supplies (headlines, calendar, prior-day journal summary). Validate with pydantic; on validation error, one retry with the error message appended; on second failure, alert operator and do not trade.
   - **Already-printed releases** (`{{RELEASED_DATA}}`): the scheduled 08:00 ET job necessarily runs *before* the classic 08:30 ET prints (CPI/PPI/NFP), so a major catalyst can land after the plan is built but an hour before the open. To fold one in, re-run `python -m agent plan` manually after it prints, passing `--released NAME ACTUAL FORECAST PREVIOUS` (repeatable, for clustered prints like CPI + Core CPI). Surprise is computed in `agent.format_released_data()` via `releases.compute_surprise()` — the same path the automated replan uses — never by the model, per design principle 2. The prompt instructs the model to treat these as authoritative fact already in the price (not an upcoming risk), and to *not* mark such an event `"replan": true` in `scheduled_events`, since there's nothing left to wait for. This is also the manual fallback for intraday releases now that both Finnhub and FMP turned out to be paid-only.
2. `replan(current_plan, current_tick, reason) -> Plan` — mid-session. Same validation path. Log a diff of old vs new levels to journal + Discord. `reason` is free text describing what changed, so the same call serves both paths: a data release (formatted by `agent.format_release_reason()`) or an operator-described price move.
   - **Manual mid-session re-plan**: `python -m agent replan [--spx-reference N | --mes-*] [--reason "..."] [--released ...] [--dry-run]`. Loads the plan in force from `state/`, re-plans against it, and rewrites both `state/` and `daily_plans/`. This exists because a morning plan's levels can be invalidated by a large intraday move, with no release involved — and because the automated release path needs a paid econ-calendar API we don't have. `--reason` is optional: omitted, `agent.describe_move()` states the move factually from the plan's own `spx_reference` vs. the price now (computed in code, per principle 2) and explicitly tells the model the cause is unconfirmed, so it doesn't invent a narrative. Supply it when you know something the price delta can't show — a headline, a Fed speaker, order flow.
   - **How a running `main.py` picks it up**: `run_live()` re-stats `state/plan_<today>.json` every `PLAN_RELOAD_POLL_S` (default 15s) and hot-swaps on mtime change, going through the normal `_load_plan()` → `engine.load_plan()` path. **This is why the manual command rewrites the plan file rather than restarting anything**: `engine.load_plan()`'s hot-swap rules preserve FIRED/OPEN state for triggers present in both plans, so a live position keeps its stop monitoring across the swap, and an OPEN trigger dropped by the new plan raises `ReviewRequired` instead of being silently discarded. A half-written file that fails to parse is ignored (the next poll retries) rather than clobbering the plan in force. Covered by `tests/test_plan_reload.py`.
3. `decide(trigger, plan, market_context) -> Decision` — when a trigger fires. `Decision = {action: Literal["EXECUTE","SKIP","WAIT"], reasoning: str, confidence: int}`. **The decision cannot alter levels or size** — it only gates the pre-planned action. `WAIT` re-arms the trigger with a 5-min cooldown.

Model: `claude-sonnet-4-6` for `decide` (latency), operator's choice for plan generation (both configured via `.env`, no code change needed to swap). Timeouts, enforced by wrapping the SDK's async call in `asyncio.wait_for(...)` (since `ClaudeAgentOptions` has no direct request-timeout field): 30s `decide` (no tools, pure completion — the original spec'd figure holds up), 600s `generate_plan` / 300s `replan` (both run live `WebSearch` for GEX/news/sentiment; a real `daily_plan.md` call measured ~382s end-to-end via the Agent SDK, so the originally spec'd 120s was too tight for that research depth — sized with headroom above the observed run). On timeout/error in `decide`: default to `SKIP` + alert (fail-closed).

Effort (`PLAN_EFFORT` / `DECIDE_EFFORT` in `.env`, mapped to `ClaudeAgentOptions.effort`) is deliberately **separate per call site**, because the two want opposite things: `decide` is a constrained 3-way gate with all context pre-supplied on a fail-closed 30s timeout, so low effort suits it; `generate_plan`/`replan` do the live research that sets every level the engine trades against all day, so lower effort there means fewer searches and shallower analysis — the last place to economize. Blank = the model's own default. Measured on `claude-opus-5`: `decide` at `effort=low` ran **10.3s** vs. the 17–35s range previously seen on `claude-sonnet-4-6` at default effort (one of which breached the 30s timeout and fail-closed to SKIP — see `journal.db` decisions rows 6–10), with reasoning quality preserved (still cites the actual price/stop/invalidation numbers).

## Scheduler (`scheduler.py`)

APScheduler `AsyncIOScheduler`, timezone `America/New_York`:

- 08:00 ET Mon–Fri: `generate_plan` job.
- Per `scheduled_events` in the plan: job at release time + 5s — `releases.poll_and_replan`.
- 15:00 ET: flat-by check (the entry cutoff itself is enforced in real time by `risk.gate_execute()` at the EXECUTE tap — see Risk/Notify below — not by a scheduled job).
- 16:05 ET: end-of-day journal summary — Discord.

## Releases (`releases.py`)

- `poll(source_id) -> ReleaseResult`: hit econ calendar API (Finnhub `/calendar/economic` or FMP equivalent — pick one, config-driven) in a loop: 2s interval, 90s max, until `actual` is non-null for the matching event. Return `{release, actual, forecast, previous, surprise}`.
- On success — `agent.replan(...)` — validate — `engine.load_plan(new_plan)` — Discord summary with old→new level diff.
- On timeout — alert operator "release data unavailable, plan unchanged."

## Feed (`feed.py`)

- Async generator `stream() -> AsyncIterator[Tick]` behind a small `TickSource` interface, so the live source is swappable. `FEED_SOURCE` in `.env` selects it:
  - `PolygonFeed` (default): Polygon.io indices `I:SPX` websocket, plus `I:TNX` for 10Y yield merged into ticks (carry last-known yield forward). Reconnect with exponential backoff (1s — 60s cap).
  - `MoomooFeed`: SPX *estimated* over a local Moomoo (Futu) OpenD gateway — requires the Moomoo desktop app running, logged in, with OpenD running locally (`moomoo-api` PyPI package). Confirmed live that Moomoo's OpenAPI rejects subscribing to US indices outright ("US stock indices are not supported" — a platform restriction, not a code-string issue), so it subscribes to SPY (the ETF, confirmed live) and scales by a ratio calibrated against a known-good SPX reference (`MoomooFeed.calibrate()`, called by `main.py` from each loaded plan's `spx_reference` — refreshes on every daily plan/replan rather than drifting for the life of a long-running process; `MOOMOO_SPY_SPX_RATIO` is only the pre-calibration initial guess). Bridges the SDK's thread-based push callback (`StockQuoteHandlerBase.on_recv_rsp`, confirmed fired on its own child thread) into the asyncio stream via a thread-safe queue. No 10Y yield support — `yield_10y` is always `None`; conditions requiring `yields_10y` simply never fire, per triggers.py's existing fail-closed behavior on missing yield data.
- **Dead-man rule** (`FeedManager`, source-agnostic): during RTH, no tick for >10s — emit `FeedStale` event — notify + engine treats as HALT for new entries.
- **Pre-market SPX reference for `agent.py`'s CLI**: SPX itself has no live pre-market print (it's computed from constituent trades, most of which aren't liquid pre-market — confirmed live, `.SPX` just repeats the prior close before 09:30). MESmain futures do trade pre-market, so `python -m agent plan` accepts `--mes-current`/`--mes-prev-close`/`--spx-prev-close` as an alternative to `--spx-reference`: derives `spx_reference = mes_current + (spx_prev_close - mes_prev_close)` — the same basis-adjustment a trader does by hand, computed once as plain deterministic arithmetic, never by the LLM. MES/ES futures were considered for the *live tick feed* too but rejected for now — Moomoo's OpenAPI gates futures quotes behind a paid "quote card" the operator's account doesn't have (confirmed live); `US.ES` without the `main` suffix looks like it subscribes but silently resolves to an unrelated stock (Eversource Energy) via ticker collision.

## Risk (`risk.py`)

`RiskManager.gate_execute(engine, now) -> RiskCheckResult` is the single hard gate, checked in real time at the moment the operator taps **EXECUTE** (`notify.py`'s `_on_interaction`, not at `TriggerFired` time — see Notify below):

- `state/HALT` file exists — block all new entries, alert once.
- Daily realized-loss limit (config, e.g. from journal marks) — auto-create HALT.
- Max simultaneous OPEN triggers: 1 (config).
- Hard entry cutoff 15:00 ET regardless of plan contents.

## Notify (`notify.py`)

- Discord bot over the Gateway (persistent outbound websocket, like Telegram's long-polling — no inbound ports, no public webhook endpoint). Messages: plan summary at 08:05, re-plan diffs, `TriggerFired` with **inline buttons [EXECUTE] [SKIP]** wired to `engine.confirm/skip`, target/stop/invalidation alerts, EOD summary.
- All alerts include price, time (ET + SGT), trigger id, and the agent's one-line reasoning.
- **`TriggerFired` always calls `decide()` and always alerts**, even when `risk.gate_execute()` would currently refuse — e.g. past the 15:00 entry cutoff, or with a HALT file present. The operator should never go dark just because a guardrail is active; they still see the trigger, the agent's recommendation, and can act with full context. The hard block is enforced **at the EXECUTE tap itself**: `Notifier` is constructed with the live `RiskManager` (`main.py`'s `start_notifier()`), and `_on_interaction`'s `confirm` branch re-checks `risk.gate_execute(engine, datetime.now(ET))` in real time before calling `engine.confirm()`. If blocked, the tap is refused with the reason edited into the message and `engine.confirm()` never runs — the trigger stays `FIRED` (operator can still tap SKIP). This moves the guardrail from a stale fire-time check to the actual moment of commitment, without weakening it: nothing can execute past cutoff, but nothing goes silent either. Test: `tests/test_notify.py`.

## Journal (`journal.py`)

SQLite (aiosqlite). Tables: `plans` (full JSON, generated_at, trigger source), `events` (every EngineEvent), `decisions` (trigger id, agent action, reasoning, confidence, operator response, latency), `releases`. This is the accountability layer — every decision must be reconstructable.

## Replay harness (`replay.py`)

- `python replay.py --plan state/plan_2026-07-01.json --ticks tests/fixtures/ticks_sample.csv [--speed 0|60x]`
- CSV columns: `ts_et,price,yield_10y`. Feeds ticks through the real `TriggerEngine`, prints events, writes to a throwaway journal. Auto-decisions stubbed (`decide` mocked to EXECUTE) unless `--interactive`.
- Include a fixture reproducing the 2026-07-01 ISM scenario: pre-market 7465 → 7480 post-release → dip to 7468 (fires primary_long) → run to 7499 (target 1) etc.

---

## Milestones (implement in this order)

1. **M1 — Core, offline**: `schema.py`, `triggers.py`, `replay.py`, tests. Exit criteria: replay of the ISM fixture produces the expected event sequence; `pytest` green; gap-through and hot-swap cases covered.
2. **M2 — Agent + prompts**: `agent.py` with validated structured output; `prompts/` populated (adapt operator's existing daily prompt to demand the JSON block); plan generation runnable standalone: `python -m agent plan`.
3. **M3 — Live loop, alerts-only**: `feed.py`, `scheduler.py`, `releases.py`, `notify.py`, `risk.py`, `journal.py`, `main.py`. Paper-watch for ≥2 weeks.
   - Implemented. `main.py --sim` (feed -> engine -> `agent.decide()` -> journal, no Discord) verified live against the M1 ISM fixture: a real trigger fire produced a well-reasoned `decide()` call, correctly journaled, and correctly did **not** auto-advance to OPEN/target/stop monitoring — that requires an operator's Discord tap (`confirm`/`mark_filled`), which is by design.
   - `notify.py` was swapped from Telegram to Discord after initial M3 implementation — same shape (Gateway bot, no inbound ports, inline buttons via components/interactions instead of Telegram's `InlineKeyboardButton`/`CallbackQueryHandler`), a contained rewrite since `main.py` only calls `notify.py`'s methods.
   - **Not yet verified live**: the Discord-integrated path (`notify.py`'s inline buttons actually calling `engine.confirm/skip/mark_filled`) — needs a real bot token/channel id; `feed.py`'s `PolygonFeed` websocket — needs a Polygon key and RTH; `scheduler.py`'s cron jobs and a real `releases.py` poll against a live econ print. See `ROLLOUT_PLAN.md` Step 4.
4. **M4 (later, separate approval)**: broker integration (Tradier sandbox first), position marks, auto scale-outs.

## Config (`.env.example`)

```
# agent.py authenticates via `claude login` (Claude subscription) — no
# ANTHROPIC_API_KEY here on purpose, see agent.py.
POLYGON_API_KEY=
FINNHUB_API_KEY=
DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_ID=
FEED_SOURCE=polygon
MOOMOO_HOST=127.0.0.1
MOOMOO_PORT=11111
MOOMOO_SPY_CODE=US.SPY
MOOMOO_SPY_SPX_RATIO=10.06
DAILY_LOSS_LIMIT_USD=1000
MAX_OPEN_TRIGGERS=1
YIELD_WINDOW_MIN=5
YIELD_EPSILON=0.005
DECIDE_MODEL=claude-sonnet-4-6
PLAN_MODEL=claude-sonnet-4-6
```

## Testing requirements

- `test_schema.py`: validator rejections (stop on wrong side, scale-out ≠ 100, dangling `requires_not_filled`).
- `test_triggers.py`: zone entry debounce; one-shot; `requires_not_filled` gating; yield fail-closed; gap-through stop; invalidation halts new fires; hot-swap preserves OPEN, emits ReviewRequired on removed-OPEN; `not_after` expiry; WAIT cooldown re-arm; `load_plan()` clears invalidation and reports it.
- No test may require network access.
