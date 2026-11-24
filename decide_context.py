"""v2: the extra context decide() gets when a trigger fires: the day's
write-up (trimmed to the sections that matter for a go/no-go call), the
trading rules from CLAUDE.md, and recent 5-minute closes. Pure text
helpers; main.py does the wiring. See SPEC.md section 11."""

from __future__ import annotations

import re
from pathlib import Path

# Headings from the spx-premarket-analysis format worth keeping, matched
# case-insensitively anywhere in a "## " heading.
KEEP_HEADINGS = ("levels", "must-hold", "must hold", "resistance", "scenario", "trade bias")
INTRADAY_HEADING = "intraday update"
MAX_ANALYSIS_CHARS = 6000
TRIMMED = "\n[... trimmed]"

_HEADING_RE = re.compile(r"^## .*$", re.MULTILINE)


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading line, full section text) for each "## " section."""
    starts = [m.start() for m in _HEADING_RE.finditer(text)]
    out = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        body = text[start:end].strip()
        heading = body.splitlines()[0]
        out.append((heading, body))
    return out


def _cap(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + TRIMMED


def analysis_excerpt(text: str, max_chars: int = MAX_ANALYSIS_CHARS) -> str:
    """Levels table, must-hold, resistance, scenarios and trade bias, plus
    the latest "Intraday update" section (a replan's new view), in that
    order. Falls back to the start of the text if no heading matches."""
    keep, intraday = [], None
    for heading, body in _sections(text):
        lower = heading.lower()
        if INTRADAY_HEADING in lower:
            intraday = body  # the last one wins
        elif any(k in lower for k in KEEP_HEADINGS):
            keep.append(body)
    if intraday is not None:
        keep.append(intraday)
    out = "\n\n".join(keep) if keep else text.strip()
    # the write-up's closing disclaimer is for the reader, not the decision
    out = "\n".join(line for line in out.splitlines() if "not financial advice" not in line.lower()).strip()
    return _cap(out, max_chars)


def load_analysis(analyses_dir: str | Path, plan_date: str) -> str:
    path = Path(analyses_dir) / f"{plan_date}.md"
    try:
        return analysis_excerpt(path.read_text(encoding="utf-8"))
    except OSError:
        return f"(no analysis write-up found for {plan_date}; judge from the trigger and plan only)"


def load_trading_rules(path: str | Path) -> str:
    """The "## Trading rules" section of CLAUDE.md, without its heading."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return "(trading rules unavailable)"
    for heading, body in _sections(text):
        if "trading rules" in heading.lower():
            return body[len(heading):].strip()
    return "(trading rules unavailable)"


def format_closes(closes: list[tuple[str, float]]) -> str:
    if not closes:
        return "no completed 5-minute bars yet today"
    return " | ".join(f"{hhmm} {price:,.2f}" for hhmm, price in closes)
