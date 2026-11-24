# Rollout & Verification Plan

Milestone code passing `pytest` is not the same as trusting it with real money. This
tracks the verification steps that sit between "code compiles" and "safe to
paper-watch live" — most of them require the operator (not Claude Code) to actually
look at the output.

Update the checkboxes as steps complete. Keep this file, and `SPEC.md`, in sync with
reality — if a step reveals a design gap, fix `SPEC.md` first, then the code.

---

## Step 1 — Manually audit M1 yourself (~30 min, you, not Claude Code)

The point of this step is to build *your own* confidence in the trigger engine, not to
re-read a Claude-generated test report. Do it yourself, even though `pytest` is green.

```bash
pytest -v                          # read the test names — are the tricky cases actually there?
python replay.py --plan tests/fixtures/plan_ism_day.json \
                 --ticks tests/fixtures/ticks_sample.csv
```

- [ ] Read the `pytest -v` output yourself — do the test names cover the cases you'd
      worry about (debounce, gap-through, `requires_not_filled`, hot-swap, invalidation)?
- [ ] Run the replay and check it against what *you* would have done on July 1: does
      `primary_long` fire at 7,468 and not at 7,481? Does `fade_extension` stay gated?
- [ ] Break it on purpose: edit the fixture CSV so price gaps from 7,455 to 7,441,
      skipping the 7,450 stop, and confirm a `StopHit` still emits.
- [ ] If anything surprises you, fix it now — write a regression test first, then fix.

**Status: not done.** Claude Code verified the M1 replay output during implementation
(event sequence matched the ISM narrative, gap-through/hot-swap/requires_not_filled all
covered by tests), but that's the builder checking its own work — it doesn't substitute
for you independently running and breaking it.

---

## Step 2 — M2: `agent.py` + prompts + CLI

```
claude "Read SPEC.md. Implement Milestone 2: agent.py with structured output
validated against schema.py, plus prompts/. My existing daily analysis prompt is
in prompts/daily_plan.md — extend it to also emit the plan JSON per the schema.
Add a CLI: python -m agent plan --dry-run that prints the validated Plan or the
validation errors."
```

Test it for real: run `python -m agent plan` on an actual morning, compare the JSON
against the prose analysis you'd have written by hand. Two failure modes to watch for:

1. **Schema violations** — the retry logic should catch these automatically.
2. **Plausible-but-wrong plans** — levels that validate fine but don't match what the
   analysis text actually says. Only you can catch this one.

- [x] `agent.py` implemented: `generate_plan`, `replan`, `decide`, all validated
      against `schema.py`.
- [x] `prompts/daily_plan.md` (operator-authored), `prompts/replan.md`,
      `prompts/decide.md` in place.
- [x] `python -m agent plan --dry-run` CLI works.
- [x] Auth: swapped from a metered API key to the Claude Agent SDK on subscription
      auth (`claude login`) — see `SPEC.md`'s Agent module section for why.
- [x] One live run completed successfully (2026-07-19, `daily_plans/plan_2026-07-19.json`):
      schema-valid, three properly-gated triggers, plausible on read-through.
- [ ] **3–4 mornings of real runs, each compared against your own independently-written
      prose analysis for that morning.** Only 1 of 3–4 done so far.
- [ ] Run the non-dry-run form (`python -m agent plan`, writes `state/plan_<date>.json`)
      at least once — so far only `--dry-run` has been exercised live.

**Status: in progress.** Code and one-time live verification are solid; the repeated
real-morning comparison — the part that catches plausible-but-wrong output — is not
done yet.

---

## Step 3 — Wire M2 output into M1 replay

The integration checkpoint most people skip: take a real generated plan, feed it
through the replay harness with that day's actual price path (export minute bars from
Moomoo or Polygon into the CSV format). Do this for a week of plans — now you're
testing the full plan→trigger chain with zero live risk.

- [ ] Export real minute-bar data for at least one day with a generated plan.
- [ ] Run `python replay.py --plan daily_plans/plan_<date>.json --ticks <real_bars>.csv`.
- [ ] Repeat for a week of plans.

**Status: not started.**

---

## Step 4 — M3: live loop, alerts-only

```
claude "Read SPEC.md. Implement Milestone 3: feed.py, scheduler.py, releases.py,
notify.py, risk.py, journal.py, main.py. Alerts-only per spec — no broker code.
Include a --sim flag on main.py that uses replay ticks instead of the live
websocket so I can test the full loop including Telegram without market hours."
```

Test in this order — each layer has its own failure modes:

- [x] `--sim` mode, no Discord: `feed -> engine -> agent.decide() -> journal` verified
      live against the M1 ISM fixture. A real trigger fire produced a genuinely
      well-reasoned EXECUTE decision (82% confidence, cited the actual price/time/
      invalidation/stop numbers), correctly journaled, and correctly did **not**
      auto-advance past FIRED — confirming Phase 1's "operator must tap to confirm"
      design is actually enforced, not just described.
- [x] Notify swapped from Telegram to Discord (per operator request, after initial
      M3 implementation) — same shape (Gateway bot, no inbound ports), inline
      buttons via message components/interactions.
- [x] Discord bot connectivity verified live: `Notifier.start()` logs in, connects
      to the Gateway, and `send_text()` successfully posts a real message to the
      configured channel. This caught and fixed a genuine race-condition bug —
      `start()` scheduled the whole `client.start()` (login + connect) as a
      background task and immediately called `wait_until_ready()`, which raced
      `login()`'s internal setup and failed every time. Fixed by awaiting
      `login()` directly, then backgrounding only `connect()`. Also caught (not a
      code bug) a Discord-side 403 `Missing Access` from the bot not yet being
      invited to the server with View Channel/Send Messages permissions.
- [x] The button flow, both sides: verified live via `--sim` against the ISM fixture,
      two separate runs. Run 1: `TriggerFired` -> `agent.decide()` (EXECUTE) ->
      Discord alert -> operator tapped **EXECUTE** -> journal confirms
      `engine.confirm()` fired and `operator_response='confirm'` logged ~2.3s
      later. Run 2 (same fixture, fresh fire): agent again recommended EXECUTE,
      operator tapped **SKIP** instead -> journal confirms the human override
      worked (`operator_response='skip'`, ~3.8s later), and run 1's earlier
      `confirm` record was untouched — separate decision rows, no cross-talk.
      - **Bug found and fixed** during the EXECUTE run: the "Mark Filled"
        follow-up handler called `engine.mark_filled()` but never logged
        anything — and the tempting fix (reusing `log_operator_response`) would
        have silently overwritten the earlier `confirm` record, since both share
        one column. Fixed properly: added a separate `filled_ts` column
        (`journal.log_filled()`), plus a migration (`ALTER TABLE ... ADD COLUMN`
        gated on `PRAGMA table_info`) so the already-existing `journal.db` picks
        it up on next `connect()` without manual intervention. Verified live:
        migration applied cleanly to the real `state/journal.db` on the SKIP-path
        rerun, no errors. Two regression tests added
        (`test_log_filled_does_not_clobber_operator_response`,
        `test_migrate_adds_filled_ts_to_pre_existing_db`).
- [~] Live feed: switched from `PolygonFeed` (no API key available) to a new
      `MoomooFeed` using the operator's own Moomoo/Futu OpenD gateway — a real
      broker connection instead of a third-party data vendor. Discovery during
      build: Moomoo's OpenAPI flatly rejects subscribing to US indices
      ("US stock indices are not supported" — a platform restriction, not a
      code-string bug), so `MoomooFeed` subscribes to SPY (the ETF, confirmed
      live) and estimates SPX via a ratio calibrated against the plan's
      `spx_reference` (`MoomooFeed.calibrate()`, wired into `main.py`'s
      `_load_plan()` so it refreshes on every daily plan/replan).
      **Confirmed live**: OpenD connects and authenticates, SPY subscribe
      succeeds with real push data flowing, `calibrate()` correctly reconstructs
      the reference price on the first tick (7465.00 exactly, via
      `ratio = 7465/spy_price`), and quote timestamps (which include fractional
      seconds — a real parsing bug caught and fixed before it shipped) parse
      correctly to ET.
      **Not yet verified**: the full RTH scenario — continuous ticks over time
      during actual market hours (this test ran pre-market, seeing the prior
      close repeated), reconnect-with-backoff under a real dropped connection,
      and the dead-man rule actually firing. `PolygonFeed` itself remains
      completely untested (no API key) and now a secondary option behind
      `FEED_SOURCE=polygon`.
      Fixed since: `subscribe()` wasn't requesting extended-hours data
      (`extended_time=True, session=Session.ALL` added), so pre-market ticks
      weren't flowing before 08:00 — this also silently broke `main.py`'s
      automatic 08:00 plan generation, which sources `SPX_SPOT` from
      `self.last_price`. Also fixed: `main.py` didn't load an existing
      `state/plan_<today>.json` at startup, only ever getting a plan from the
      scheduled job — restarting the process after 08:00 would sit plan-less
      until the next day. Both fixed, plus a startup/heartbeat log so the
      process is never silently ambiguous about whether ticks are flowing.
      **Considered and rejected (for now)**: MES/ES futures as a tighter proxy
      than SPY (nearly 24/5, no pre-market gap). Confirmed live that Moomoo
      does carry `US.MESmain`/`US.ESmain` as valid codes, but subscribing
      returns "Insufficient quote permission — purchase a quote card" — gated
      behind a paid add-on the account doesn't have. (`US.ES` without the
      `main` suffix "succeeded" but resolved to an unrelated stock — Eversource
      Energy, ticker collision — confirmed by the returned price, ~$74, being
      nowhere near ES futures' actual level.) Sticking with the free,
      already-working SPY ratio unless the operator decides to buy that quote
      card.
- [ ] One release poll on a real econ print (schedule against the next CPI/ADP):
      verify `actual` populates within the 90s window and the re-plan diff lands
      in Discord. `releases.py`'s matching/surprise-calc logic is unit-tested with
      mocked data; a real Finnhub response has not been exercised.

**Status: code complete, mostly verified.** Core loop, Discord connectivity, both
button paths (EXECUTE + SKIP), and the Moomoo feed connection/calibration are
confirmed live — two real bugs found and fixed along the way (journal
clobbering; a timestamp-parsing bug that would have silently substituted
`datetime.now()` for every quote). Remaining before this step is done: the
full RTH scenario for the live feed (continuous ticks, reconnects, dead-man
rule under real conditions) and a real release poll.

---

## Step 5 — The 2-week paper-watch (the real gate)

Run it live every session, alerts-only, and *also* trade your normal manual process in
parallel. Each EOD, compare: did the agent's EXECUTE/SKIP decisions match yours?

```
claude "Read journal.db and summarize this week: every TriggerFired event, the
agent's decision + reasoning, and flag any decision where confidence was <70 or
the reasoning contradicts the plan's notes field."
```

Divergences are the interesting data — each one is either a prompt fix (`decide.md`
needs adjusting), a plan-schema gap (a condition you use mentally but never encoded,
like "skip if VIX spiking"), or occasionally the agent being right and you being
emotional. **Iterate on the prompts, not the engine.**

- [ ] Two weeks of parallel live paper-watch vs. manual trading.
- [ ] Weekly journal divergence review.

**Status: not started.**

---

## After Step 5

Only after the paper-watch stabilizes do you decide on M4 (broker integration), and
that's a separate go/no-go call. Stopping at alerts-forever is a legitimate outcome —
execution is the part where a bug costs real money.

## Meta-pattern

Keep `SPEC.md` as the source of truth. Update it when a design decision changes during
testing. Start each Claude Code session with "Read SPEC.md" so implementation never
drifts from the contract.
