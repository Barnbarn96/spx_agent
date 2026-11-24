---
name: "spx-premarket-analysis"
description: "Use when Barnabas asks for a 0DTE SPX pre-market analysis or gives the morning SPX level before the US open."
---

# SPX 0DTE Pre-Market Analysis

## Inputs
- Current SPX level (from the user). If missing, ask once.
- The latest "SPX 0DTE Trading Context" doc if attached or in context — use its regime, levels table and recent closes as the baseline. If it isn't available, ask for it or proceed with a fresh search and say so.

## Research (search before writing; never rely on memory for live facts)
1. Overnight futures, Asia/Europe moves, VIX level.
2. Today's catalysts: data releases (with ET and SGT times), Fed speakers, earnings (pre- and post-market), Treasury auctions.
3. 10-year and 2-year yields; Brent/WTI and any Iran/oil headlines.
4. Megacap/AI news (NVDA, MSFT, AAPL, GOOGL, AMZN, META, AVGO, SMH) — SPX is ~40% tech.
5. Fed pricing for the next meeting (CME FedWatch / Kalshi / Polymarket if findable).
When search snippets conflict, state both and which source looks more reliable. Note limits: no live GEX, X/Twitter or Fear & Greed data.

## Output format (always this order)
1. **Recap** — prior session close, range, what drove it.
2. **Catalysts** — today's events with ET and SGT times.
3. **Data read** — consensus vs prior and what hot/cool means for SPX via the 10-year.
4. **Levels table** — support/resistance with the reason for each level.
5. **Must-hold** — the one level that flips the bias if lost.
6. **Major resistance** — the level to fade on first touch.
7. **Scenario odds** — bull/base/bear with % and SPX targets (sum to 100).
8. **Trade bias** — direction, entry zone, first target, stop, and size within the rules below.

## Trading rules to apply
- Data/volatile days: wait 15-30 min after the open. Exit by 3:30 PM ET.
- Size 15-30% depending on VIX and event risk.
- Take half at first target, move stop to breakeven. No re-entry after two stops.
- Don't chase into support/resistance; fade first touch of resistance; range trades target the other side.
- Data-day trigger: 10-year yield, 5.00% pivot (update if the context doc says otherwise).
- Remember: gap-ups this month often faded; first disappointment is company-specific, second confirms a trend.

Keep it concise and scannable. End with a one-line reminder that this is analysis, not financial advice.