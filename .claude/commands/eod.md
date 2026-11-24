---
description: Roll the SPX trading context doc forward after the close
argument-hint: [anything notable from today, lessons to add]
---

# /eod

Input: `$ARGUMENTS` - optional notes from the user about today's session.

1. Read the newest `context/context_*.md` and today's `analyses/<date>.md` if it exists.
2. Use the `spx-context-update` skill to produce the next version. Add a lesson only if the user stated it or confirms one you suggest - ask before adding.
3. Write it as a NEW file `context/context_<today ET YYYY-MM-DD>.md` (never overwrite an older one), with the doc date updated.
4. Show the user a short diff summary: closes added, levels added/removed, regime changes, calendar changes, lessons proposed.
5. Tomorrow's `/premarket` reads this file, including today's close as `spx_prev_close` - make sure the close in "Recent SPX closes" is the official close, confirmed by a second source if snippets conflict.
