# Trigger-Fired Decision

A pre-planned trigger just fired against live price. You are gating a single, already-fully-specified action — you cannot change its levels, size, or instrument. Choose whether to execute it now, skip it, or wait.

## The trigger that fired

```json
{{TRIGGER_JSON}}
```

## Plan context

- **Bias:** {{BIAS}}
- **Invalidation:** {{INVALIDATION_JSON}}
- **Other trigger statuses:** {{OTHER_TRIGGER_STATUSES}}

## Market right now

- **SPX:** {{SPX_SPOT}} at {{TICK_TIME_ET}} ET ({{TICK_TIME_SGT}} SGT)
- **10Y yield:** {{YIELD_10Y}} ({{YIELD_DIRECTION}})
- **Additional context:** {{MARKET_CONTEXT}}

## Your task

Decide one of:

- **EXECUTE** — the setup is playing out as planned; take the trade as specified.
- **SKIP** — conditions have deteriorated since the plan was written (e.g. the move looks exhausted, a conflicting signal has appeared); do not take this trade.
- **WAIT** — too early or too ambiguous to call right now; re-arm the trigger and check again in a few minutes.

You are not being asked to re-price, resize, or re-target the trade — only whether the pre-planned action should proceed. Give one line of reasoning grounded in the market context above, not in speculation beyond it, and a confidence score (0-100) in your own read of the moment.

Respond with only the decision — no prose outside the structured fields.
