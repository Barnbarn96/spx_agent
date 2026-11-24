---
description: Pre-market SPX 0DTE analysis and today's trade plan for main.py
argument-hint: <SPX from TradingView SPREADEX:SPX> [attach Moomoo GEX screenshot]
---

# /premarket

Input: `$ARGUMENTS` - the current SPX price from TradingView `SPREADEX:SPX`, optionally with extra notes. A Moomoo Gamma Exposure screenshot may be attached. If no SPX price is given, ask for it once.

## Steps

1. **Context.** Read the newest `context/context_*.md`. Note yesterday's official SPX close from its "Recent SPX closes" table - it becomes `spx_prev_close`. If the newest close in the table isn't the previous trading day, ask the user for it.

2. **GEX screenshot (if attached).** Read: expiration date, gamma flip, call wall, put wall, max pain, the `.SPX` price and the clock time in the status bar (SGT). If the expiration is not today's ET date, tell the user to switch the dropdown and resend, and continue without GEX. Otherwise run:
   ```
   .venv/Scripts/python.exe tools/set_gex.py --expiry <YYYY-MM-DD> --flip <flip> --call-wall <cw> --put-wall <pw> --max-pain <mp> --spx <spx> --as-of <HH:MM>
   ```

3. **Analysis.** Use the `spx-premarket-analysis` skill with the SPX price, the context doc and the GEX levels (if any). In the levels table, add GEX levels with reason "GEX", and state the gamma regime: SPX below the flip = negative gamma (moves extend: favour breakout/breakdown triggers, wider stops or smaller size); above = positive gamma (pinning: favour fades/range). Give max pain low weight. With GEX available, drop the "no live GEX" caveat. **Show the full analysis in chat**, in the skill's 8-section format, the same as Claude web, before doing anything else.

4. **Save the write-up** to `analyses/<today ET YYYY-MM-DD>.md`, exactly as shown to the user.

5. **Draft the plan JSON** from the write-up's levels and trade bias. Run `.venv/Scripts/python.exe tools/write_plan.py --example` to see the format (and `--schema` for every field). Rules:
   - `spx_reference` = the SPREADEX price; `spx_reference_source` = "spreadex <price> @ <HH:MM> ET"; `spx_prev_close` from step 1.
   - One trigger per trade idea in the trade bias, with that idea's entry, stop and targets. Use only numbers that appear in the write-up.
   - "Take half at TP1" = two targets at 50/50; "and move the stop to breakeven" = `"then_stop": "breakeven"` on the first target.
   - Map the write-up's entry wording to the engine:
     - "5-minute close above/below X": `price_above`/`price_below` X with `"confirm": {"type": "close", "timeframe": "5m"}`.
     - "hold below/above X": `price_below`/`price_above` X with `"confirm": {"type": "dwell", "seconds": 300}` (or a 5m close).
     - "buy dips at A-B": `price_between` [A, B] with `"approach": "from_above"` (a rally up into the zone isn't a dip). "Sell rips": `"from_below"`.
     - "fade the first touch of X": `"first_touch": true`. A stall or rejection itself can't be expressed; say so in `notes`.
     - Plain "at X" / no wording: leave `confirm` out (any tick).
   - Stops default to any tick (fastest exit). Use `"stop_confirm": {"type": "close", "timeframe": "1m"}` only if the write-up says to stop on a close.
   - `not_before`: "09:30" normally; "09:45"-"10:00" when the write-up says to wait (data day, big gap, VIX > 30).
   - `not_after` "15:00", `flat_by` "15:30" (or earlier on a half day).
   - Size from the write-up within the VIX tiers in CLAUDE.md.
   - Yield gates where the write-up uses them: `yield_below` / `yield_above` in percent.
   - Link mutually exclusive ideas with `requires_not_filled`.
   - `invalidation`: the level that kills the day's thesis.
   - `notes`: one or two sentences of thesis, plus anything the engine can't express (see CLAUDE.md) that the user must confirm by eye.
   Write it to a scratch file (the session scratchpad if you have one), not into `state/`.

6. **Install it:**
   ```
   .venv/Scripts/python.exe tools/write_plan.py <draft.json> --analysis analyses/<date>.md
   ```
   If it reports ungrounded levels, fix the JSON (or the write-up, then re-show the changed lines to the user) and rerun. If today's plan already exists, ask the user before rerunning with `--replan`.

7. **Report** in a few lines under the analysis: the triggers as the tool printed them (entry, stop, targets, size, time gates), what the user must confirm by eye, and that main.py will pick it up automatically (remind them to start `.venv/Scripts/python.exe main.py` if it isn't running).
