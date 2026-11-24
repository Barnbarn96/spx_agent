"""v2 decide() gets the real context: the day's write-up (levels table,
must-hold, resistance, scenarios, trade bias, the latest intraday update),
the trading rules from CLAUDE.md, and the last six 5-minute closes."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import agent
import config
import decide_context as dc
import main as main_mod
from schema import Decision, Tick

from test_v2_trigger_types import at, engine_for, mk, run

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parent.parent
ANALYSIS = (REPO / "tests" / "fixtures" / "analysis_2026-11-23.md").read_text(encoding="utf-8")


# -- the write-up ----------------------------------------------------------------------


def test_excerpt_keeps_the_trading_sections_and_drops_the_recap():
    text = dc.analysis_excerpt(ANALYSIS)
    for needle in ("## 4. Levels", "| **7,288** |", "## 5. Must-hold", "## 6. Major resistance",
                   "## 7. Scenarios", "## 8. Trade bias", "Buy dips at 7,195-7,215"):
        assert needle in text
    for gone in ("## 1. Recap", "## 2. Catalysts", "## 3. Data read", "not financial advice"):
        assert gone not in text


def test_excerpt_adds_only_the_latest_intraday_update():
    text = ANALYSIS + (
        "\n\n## Intraday update 10:40 ET\nOld view: 7,250 held.\n"
        "\n## Intraday update 11:30 ET\nNew view: 7,250 lost, bias now neutral.\n"
    )
    out = dc.analysis_excerpt(text)
    assert "## Intraday update 11:30 ET" in out and "bias now neutral" in out
    assert "10:40" not in out
    assert out.index("## 8. Trade bias") < out.index("## Intraday update 11:30 ET")


def test_excerpt_is_capped():
    big = ANALYSIS.replace("## 4. Levels", "## 4. Levels\n" + "| 7,000 | filler |\n" * 2000)
    out = dc.analysis_excerpt(big, max_chars=3000)
    assert len(out) <= 3000 + len(dc.TRIMMED)
    assert out.endswith(dc.TRIMMED)


def test_excerpt_without_known_headings_falls_back_to_the_start():
    out = dc.analysis_excerpt("Some free-form notes about 7,250.\n", max_chars=100)
    assert out.startswith("Some free-form notes")


def test_load_analysis_missing_file(tmp_path):
    assert "no analysis write-up" in dc.load_analysis(tmp_path, "2026-11-24")


def test_load_analysis_reads_the_days_file(tmp_path):
    (tmp_path / "2026-11-23.md").write_text(ANALYSIS, encoding="utf-8")
    assert "## 4. Levels" in dc.load_analysis(tmp_path, "2026-11-23")


# -- trading rules ------------------------------------------------------------------------


def test_trading_rules_come_from_claude_md():
    rules = dc.load_trading_rules(REPO / "CLAUDE.md")
    assert "No re-entry after two stops" in rules
    assert "fade first touch of resistance" in rules
    assert "Hard rules for Claude" not in rules  # stops at the next section


def test_trading_rules_missing_file(tmp_path):
    assert "unavailable" in dc.load_trading_rules(tmp_path / "nope.md")


# -- recent 5-minute closes -------------------------------------------------------------------


def test_engine_keeps_recent_five_minute_closes():
    e = engine_for(mk("x", price_above=9999.0, stop=9990.0))
    ticks = [(at(10, 0) + _dt.timedelta(minutes=m), 7200.0 + m) for m in range(0, 41)]
    run(e, *ticks)
    closes = e.recent_closes("5m", 6)
    # labelled by bar start; the 10:35 bar closed when the 10:40 tick arrived
    assert closes == [("10:10", 7214.0), ("10:15", 7219.0), ("10:20", 7224.0),
                      ("10:25", 7229.0), ("10:30", 7234.0), ("10:35", 7239.0)]


def test_format_closes():
    assert dc.format_closes([]) == "no completed 5-minute bars yet today"
    assert dc.format_closes([("10:05", 7209.0), ("10:10", 7214.5)]) == "10:05 7,209.00 | 10:10 7,214.50"


# -- wiring --------------------------------------------------------------------------------------


async def test_main_passes_the_context_to_decide(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    analyses = tmp_path / "analyses"
    analyses.mkdir()
    (analyses / "2026-11-24.md").write_text(ANALYSIS, encoding="utf-8")
    monkeypatch.setattr(config, "ANALYSES_DIR", str(analyses))

    seen = {}

    async def fake_decide(trigger, plan_, ctx):
        seen.update(ctx)
        return Decision(action="SKIP", reasoning="no", confidence=10)

    monkeypatch.setattr(agent, "decide", fake_decide)
    from test_v2_engine import plan, trig

    o = main_mod.Orchestrator(sim=True)
    o.journal = main_mod.JournalDB(tmp_path / "j.db")
    await o.journal.connect()
    try:
        o._load_plan(plan(trig("buy_dip")), "daily")
        for m in range(0, 12):
            await o.process_tick(Tick(ts=at(10, 0) + _dt.timedelta(minutes=m), price=7240.0 - m))
        await o.process_tick(Tick(ts=at(10, 12), price=7205.0))
        await o.wait_for_decisions()
    finally:
        await o.journal.close()

    assert "## 4. Levels" in seen["analysis"]
    assert "No re-entry after two stops" in seen["trading_rules"]
    assert seen["recent_closes"].startswith("10:00 7,236.00 | 10:05 7,231.00")


async def test_decide_prompt_contains_everything(monkeypatch):
    captured = {}

    class Result:
        is_error = False
        structured_output = {"action": "SKIP", "reasoning": "x", "confidence": 5}
        result = None

    async def fake_run_query(prompt, options, timeout_s):
        captured["prompt"] = prompt
        captured["timeout"] = timeout_s
        return Result()

    monkeypatch.setattr(agent, "_run_query", fake_run_query)
    from test_v2_engine import plan, trig

    p = plan(trig("buy_dip"))
    ctx = {
        "spx_spot": 7205.0,
        "analysis": "## 4. Levels\n| 7,210 | first support |",
        "trading_rules": "- Don't chase into support/resistance.",
        "recent_closes": "10:05 7,209.00 | 10:10 7,214.50",
    }
    d = await agent.decide(p.triggers[0], p, ctx)
    prompt = captured["prompt"]
    assert d.action == "SKIP"
    assert captured["timeout"] == config.DECIDE_TIMEOUT_S == 30
    assert "{{" not in prompt
    for needle in ("| 7,210 | first support |", "Don't chase into support/resistance.",
                   "10:10 7,214.50", "levels table"):
        assert needle in prompt


async def test_decide_still_skips_on_failure(monkeypatch):
    async def boom(prompt, options, timeout_s):
        raise agent.PlanGenerationError("timeout")

    monkeypatch.setattr(agent, "_run_query", boom)
    from test_v2_engine import plan, trig

    p = plan(trig("buy_dip"))
    d = await agent.decide(p.triggers[0], p, {})
    assert d.action == "SKIP" and d.confidence == 0


# -- tools/try_decide.py ----------------------------------------------------------------------

import sys as _sys  # noqa: E402

_sys.path.insert(0, str(REPO / "tools"))
import try_decide  # noqa: E402


def test_try_decide_prints_the_full_prompt(capsys):
    assert try_decide.main(["--print-prompt"]) == 0
    out = capsys.readouterr().out
    assert "{{" not in out
    for needle in ("\"id\": \"buy_dip\"", "## 4. Levels", "No re-entry after two stops",
                   "09:40 7,236.00", "10:05 7,207.00", "7205.0 at 10:12:00 ET (23:12:00 SGT)"):
        assert needle in out
    out.encode("ascii")


def test_try_decide_calls_decide(monkeypatch, capsys):
    async def fake_decide(trigger, plan_, ctx):
        assert trigger.id == "breakout_long" and ctx["spx_spot"] == 7262.0
        return Decision(action="EXECUTE", reasoning="5m close above 7,260 held", confidence=66)

    monkeypatch.setattr(agent, "decide", fake_decide)
    assert try_decide.main(["--trigger", "breakout_long", "--price", "7262"]) == 0
    assert "EXECUTE (confidence 66): 5m close above 7,260 held" in capsys.readouterr().out


def test_try_decide_unknown_trigger(capsys):
    assert try_decide.main(["--trigger", "nope"]) == 1
    assert "buy_dip" in capsys.readouterr().err
