---
description: Mid-session SPX read; records the 10Y yield and GEX for main.py
argument-hint: <SPX> <10Y>  |  attach a data or GEX screenshot  |  news?
---

# /intraday

Input: `$ARGUMENTS`, plus any attached screenshot. Keep the reply short - this is used mid-session.

## Steps

1. **Record inputs first** (fast, before any analysis):
   - A 10Y yield given (e.g. `4.93`, or `49.3` TNX-style): `.venv/Scripts/python.exe tools/set_yield.py <value>`
   - A Moomoo Gamma Exposure screenshot: read expiration, gamma flip, call/put walls, max pain, the `.SPX` price and the status-bar time (SGT), then `.venv/Scripts/python.exe tools/set_gex.py --expiry ... --flip ... --call-wall ... --put-wall ... --max-pain ... --spx ... --as-of HH:MM`. If the expiration isn't today, tell the user to switch it to today and resend.
   Quote each tool's one-line output.

2. **Read the market** with the `spx-intraday-read` skill, against today's `analyses/<date>.md` and `state/plan_<date>.json`:
   - SPX given: where it sits vs the levels table and the plan's trigger zones; approaching support/resistance or first touch.
   - GEX: only what changed (the tool prints it) and whether the regime flipped.
   - Data screenshot or "news?": follow the skill.
   - Flag when within 30 min of the 3:30 PM ET exit.

3. **Replan only if the bias changed.** If the read says the plan no longer holds (must-hold lost, regime flip with a thesis change, a data surprise), say so in one line and ask: "Replan?" Only after a yes:
   - append an "Intraday update <HH:MM ET>" section to `analyses/<date>.md` with the new levels and trade ideas,
   - draft a new plan JSON (keep the ids of triggers that are still valid - an OPEN trade keeps its state only if its id survives),
   - run `.venv/Scripts/python.exe tools/write_plan.py <draft.json> --analysis analyses/<date>.md --replan`.
   main.py hot-swaps within seconds and posts the diff to Discord.

End with the one-line "analysis, not financial advice" reminder only when giving a trade view.
