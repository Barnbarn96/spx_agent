# Intraday Re-plan

Something has happened that may have invalidated the plan currently in force. Decide whether and how it needs to change, then re-emit a complete, corrected plan.

## Why we're re-planning

{{REPLAN_REASON}}

## Market right now

- **SPX:** {{SPX_SPOT}} at {{TICK_TIME_ET}} ET
- **10Y yield:** {{YIELD_10Y}}

## The plan currently in force

```json
{{CURRENT_PLAN_JSON}}
```

## What to do

1. Decide whether what happened changes the day's bias, invalidates any trigger's premise, or shifts levels. If it does **not** materially change things — a release landing in line with forecast, or a move that stays within the structure the plan already anticipated — say so explicitly in your analysis and keep the levels the same. A re-plan that changes nothing is a perfectly good outcome; do not manufacture changes to justify the call.
2. If levels do need to move, keep the same trigger `id`s wherever the underlying trade idea survives (this lets the engine preserve state for triggers that already fired). Only introduce a new `id` for a genuinely new setup.
3. Do not touch triggers that are already `OPEN` from the operator's perspective — assume the deterministic engine handles that; your job is only to supply corrected forward-looking levels. **Keep the `id` of any OPEN trigger in your output**: dropping it forces the engine to flag a live position for manual review.
4. Beware of chasing. If price has already run a long way in one direction, the move may be closer to exhaustion than continuation — re-plan for where price *is* now, not for the move that already happened. Note remaining time in the session: setups needing a large follow-through are less realistic late in the day, and nothing may allow entries after 15:00 ET regardless.
5. Same hard rules as the daily plan: every level in the JSON must be justified by your prose; stops on the correct side of entry; every trigger has a `not_after`; nothing allows entries after 15:00 ET; `invalidation` is catastrophic-only; `flat_by` defaults to "15:00".
6. Do NOT use the `yields_10y` condition on any trigger — the live feed has no yield data, so it can never evaluate true and the trigger would silently sit dead. If an existing OPEN or FIRED trigger you're carrying forward already has one, strip it.

---

## REQUIRED OUTPUT FORMAT — READ CAREFULLY

Your response has exactly TWO parts, in this order:

### Part 1: The analysis (prose)

A short read on what the release means and what, if anything, changes. State explicitly whether this is a no-change re-confirmation or a revision, and why.

### Part 2: The machine-readable plan (single fenced JSON block)

After the analysis, emit ONE fenced ```json block containing the **complete, updated** trade plan — not a diff. It is parsed by a deterministic trigger engine and validated against a strict schema. Output nothing after the closing ``` of the JSON block.

### JSON schema

```json
{
  "date": "{{DATE}}",
  "bias": "bullish | neutral | bearish",
  "spx_reference": <float, current spot>,
  "triggers": [
    {
      "id": "<snake_case_unique>",
      "side": "long | short",
      "condition": {
        "price_between": [<low>, <high>],
        "yields_10y": "falling | rising",        // DO NOT USE — see rule 6, live feed has no yield data
        "not_before": "HH:MM",                    // ET
        "not_after": "HH:MM",                     // ET, required
        "requires_not_filled": ["<trigger_id>"]   // [] if none
      },
      "action": {"instrument": "SPX 0DTE <strike><C|P>", "size_pct": <int 1-100>},
      "targets": [{"level": <float>, "scale_out_pct": <int>}],  // pcts sum to 100
      "stop": <float>,
      "one_shot": true
    }
  ],
  "invalidation": {"below": <float or null>, "above": <float or null>, "action": "flatten_and_halt"},
  "flat_by": "HH:MM",
  "scheduled_events": [
    {"time": "HH:MM", "release": "<name>", "source_id": "<calendar_api_id>", "replan": <bool>}
  ],
  "notes": "<condensed analysis: what changed and why, or why nothing changed>"
}
```
