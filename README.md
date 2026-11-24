# 0DTE SPX Trading Agent

Local, single-process, alerts-only trading assistant for SPX 0DTE options.
Deterministic code decides *when* something happens (triggers, stops,
targets, risk gates); the LLM only decides *whether* to act on it. Nothing
executes automatically — every entry needs an explicit Discord button tap.
See [SPEC.md](SPEC.md) for the full design.

## v2 quick start (release-v2)

v2 splits the work in two. **Claude Code is the analyst** (open this folder
in Claude Code desktop; see [CLAUDE.md](CLAUDE.md)) and **`main.py` is the
watcher**. They share files under `state/`.

1. One-time: `py -3.12 -m venv .venv`, then
   `.venv\Scripts\python.exe -m pip install -r requirements.txt`, then
   copy `.env.example` to `.env` (`FEED_SOURCE=moomoo`, Discord token/channel).
2. Start the watcher in a terminal and leave it running:
   `.venv\Scripts\python.exe main.py` (Moomoo OpenD must be running).
3. In Claude Code:
   - `/premarket 7250` with the TradingView `SPREADEX:SPX` price, plus a
     Moomoo GEX screenshot if you have one. It writes `analyses/<date>.md`
     and installs `state/plan_<date>.json`. `main.py` loads it within seconds
     and posts the summary to Discord.
   - `/intraday 7268 4.93` during the session (SPX, 10Y) or with a GEX or
     data screenshot. The 10Y is only entered by hand and expires after
     20 min. Triggers with yield conditions pause until a fresh one arrives.
   - `/eod` after the close to roll `context/` forward.

What changed from v1:
- the 08:00 auto-plan is off (`PLAN_AUTOGEN=false`)
- the SPY->SPX ratio comes from the previous closes (`spx_prev_close` in the plan)
- yield-level trigger conditions (`yield_below` / `yield_above`)
- GEX input and a check of the SPX estimate against the screenshot price
- `decide()` gets yield, GEX and plan notes, and runs in the background
- inputs are polled on a timer instead of on ticks
- trigger types:
  - 5-minute close / hold confirmation, direction of approach, first touch
  - breakeven stop after TP1
  - optional stops on a bar close
- no firing on a jump through a zone or on price already in a zone
- trigger state survives a restart of `main.py`

**Working on the code?** Read [SPEC.md](SPEC.md), then
[docs/V2_STATUS.md](docs/V2_STATUS.md). Its §1 is the ordered to-do list.

The rest of this README describes v1.

## 1. Setup

```
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code   # Agent SDK auth needs the CLI on PATH
claude login                                # authenticate via your Claude subscription
```

Copy `.env.example` to `.env` and fill in:

- `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` — required for alerts and the
  EXECUTE/SKIP buttons.
- `FEED_SOURCE` — `polygon` (default, needs `POLYGON_API_KEY`) or `moomoo`
  (needs the Moomoo/Futu desktop app running, logged in, with OpenD active
  locally — see SPEC.md's Feed section for what it can and can't quote).
- `DECIDE_MODEL` / `PLAN_MODEL` / `DECIDE_EFFORT` / `PLAN_EFFORT` — model and
  effort per call site. `decide()` is a fast fail-closed gate (low effort is
  fine); plan generation does real research, so leave its effort alone
  unless you're deliberately trading quality for tokens.
- `DAILY_LOSS_LIMIT_USD`, `MAX_OPEN_TRIGGERS` — hard risk guardrails, enforced
  in `risk.py`, never by the LLM.

## 2. Generate the day's plan

Needs a same-day SPX reference price. Either pass it directly, or derive it
pre-market from MES futures (SPX itself has no real pre-market print):

```
python -m agent plan --spx-reference 6410

python -m agent plan --mes-current 7461 --mes-prev-close 7465 --spx-prev-close 7428
```

If something (e.g. CPI) already printed before the plan is generated, feed
the real numbers in so the model treats them as settled fact rather than
guessing at them — repeat `--released` for multiple prints:

```
python -m agent plan --mes-current 7509 --mes-prev-close 7447 --spx-prev-close 7412 \
  --released "CPI YoY" 3.1 3.0 2.9
```

This writes `daily_plans/plan_<date>.json` (archival) and
`state/plan_<date>.json` (what `main.py` actually loads).

## 3. Run the live loop

```
python main.py
```

Starts the Discord bot, loads today's plan if one already exists, and
streams ticks from the configured feed. At 08:00 ET Mon–Fri it generates a
fresh plan automatically if `python -m agent plan` hasn't already been run
for the day. `Ctrl+C` shuts down cleanly.

Discord alerts you get: plan summary, `TRIGGER FIRED` (with EXECUTE/SKIP
buttons and the agent's recommendation), target/stop/invalidation hits,
flat-by reminder, HALT notices, EOD summary. Alerts keep flowing even past
the 15:00 ET entry cutoff or during a HALT — the hard block is enforced in
real time when you actually tap EXECUTE, not by going silent beforehand.

## 4. Re-plan mid-session

If price moves enough to invalidate the plan in force — a big unscheduled
move, or a scheduled release printing — rewrite the plan without restarting
`main.py`; it hot-swaps onto the new file within `PLAN_RELOAD_POLL_S`
seconds (default 15), preserving any position that's already `OPEN`.

```
python -m agent replan --reason "SPX dumped 60pts through the 7450 must-hold in 20min on no headline" \
  --mes-current 7412 --mes-prev-close 7447 --spx-prev-close 7412
```

`--reason` is optional — omit it and the CLI derives a factual one from the
old plan's `spx_reference` vs. the new spot price:

```
python -m agent replan --mes-current 7412 --mes-prev-close 7447 --spx-prev-close 7412
```

Add `--released "<name>" <actual> <forecast> <previous>` the same way as
`plan` if the re-plan is triggered by a data print. Add `--dry-run` to
preview the new plan without touching `state/` (still writes to
`daily_plans/`).

## 5. Backtest / dry run against recorded ticks

```
python main.py --sim --plan daily_plans/plan_2026-07-22.json --ticks ticks.csv --speed 60x
```

`ticks.csv` columns: `ts_et,price,yield_10y`. `--speed 0` (default) replays
as fast as possible; `--speed 60x` paces it at 60x real time.

## 6. Manual controls

- `state/HALT` — create this file to hard-block all new entries immediately
  (checked before every EXECUTE); delete it to resume. Never created or
  cleared automatically except by the daily-loss-limit breach.
- `journal.db` (SQLite, under `state/`) — every plan, event, and decision is
  logged here for after-the-fact review.

## Tests

```
pytest
pyflakes .
```



## Run daily trade plan
python -m agent plan --spx-reference <today's SPX close or a rough number>
python -m agent plan --mes-current 7461 --mes-prev-close 7465 --spx-prev-close 7428
python -m agent plan --mes-current 7509 --mes-prev-close 7447 --spx-prev-close 7412 \
  --released "CPI YoY" 3.1 3.0 2.9

python -m agent replan --reason "SPX dumped 60pts through the 7450 must-hold in 20min on no headline" \
  --mes-current 7412 --mes-prev-close 7447 --spx-prev-close 7412

python -m agent replan --mes-current 7412 --mes-prev-close 7447 --spx-prev-close 7412

python main.py


python -m agent plan --mes-current 7606 --mes-prev-close 7643 --spx-prev-close 7636 --released "PPI MoM" 0.4% 0.4% 0.1%

python -m agent plan --mes-current 7672 --mes-prev-close 7598 --spx-prev-close 7591 --released "core inflation rate mom" 0.3% 0.2% 0.2% --released "core inflation rate yoy" 2.4% 2.4% 2.5% --released "inflation rate mom" 0.4% 0.4% 0.1% --released "inflation rate yoy" 3.4% 3.4% 3.4%


actual forecast previous

python -m agent plan --spx-reference 7777 --released "JOLTS job openings" M 7.23M 7.271M --released "JOLTS job quits" M n/a 3.056M

python -m agent plan --mes-current 7765 --mes-prev-close 7732 --spx-prev-close 7670 --released "core pce price index mom" 0.2% 0.3% 0.1% --released "core pce price index yoy" 3% 3.3% 3% --released "GDP Growth Rate QoQ (Final)" 2.2%	1.5% 2.1% --released "Real Consumer Spending QoQ (Final)" 3.8% 3.4% 0.7%


python -m agent plan --mes-current 7802 --mes-prev-close 7724 --spx-prev-close 7666 --released "non farm payroll"  29K 90K 133K --release "Unemployment Rate" 4.2% 4.1% 4.1% --released "Average Hourly Earnings MoM" 0.1% 0.3% 0.3% --released "Average Hourly Earnings YoY" 3% 3.2% 3.1%

python -m agent plan --spx-reference 7785  --released "core inflation rate mom" 0.3% 0.2% 0.3% --released "core inflation rate mom" 2.6% 2.5% 2.4% --released "inflation rate mom" 0.6%	0.6% 0.4% --released "inflation rate yoy" 3.7% 3.6% 3.4%