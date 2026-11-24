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
- **Last six 5-minute closes (bar start ET, oldest first):** {{RECENT_CLOSES}}

## Today's pre-market write-up (and the latest intraday update, if any)

This is where the trigger came from. Its levels table, must-hold, scenarios and trade bias are the plan you are checking against.

{{ANALYSIS}}

## The operator's trading rules

{{TRADING_RULES}}

## Your task

Decide one of:

- **EXECUTE** — the setup is playing out as planned; take the trade as specified.
- **SKIP** — conditions have deteriorated since the plan was written (e.g. the move looks exhausted, a conflicting signal has appeared); do not take this trade.
- **WAIT** — too early or too ambiguous to call right now; re-arm the trigger and check again in a few minutes.

How to use the context:

- **Levels table:** is price entering into a level the write-up marks as support/resistance just beyond the entry (e.g. a long right under resistance)? The rules say don't chase into support/resistance.
- **Recent closes:** is the move confirming (closes in the trade's direction, holding the level) or stalling/reversing? An entry should come on confirmation, not on a spike that is already fading.
- **Scenarios and trade bias:** does this trigger fit the scenario that is playing out? If the latest intraday update changed the bias, weigh that over the morning view.
- **Rules:** a clear rule conflict is a reason to SKIP. The code already enforces time cutoffs, two-stop HALTs and the opening wait, so don't skip on those alone.

You are not being asked to re-price, resize, or re-target the trade — only whether the pre-planned action should proceed. Give one line of reasoning grounded in the context above (name the level or close you're relying on), not in speculation beyond it, and a confidence score (0-100) in your own read of the moment.

Respond with only the decision — no prose outside the structured fields.
