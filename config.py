"""Env/config loading. Reads .env (via python-dotenv) into module constants."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# agent.py authenticates via the operator's Claude subscription (through the
# locally-installed `claude` CLI), not an API key — see agent.py's module
# docstring. No ANTHROPIC_API_KEY constant is defined here on purpose: were
# it loaded into os.environ, it would silently shadow subscription auth.
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

DAILY_LOSS_LIMIT_USD = float(os.environ.get("DAILY_LOSS_LIMIT_USD", "1000"))
MAX_OPEN_TRIGGERS = int(os.environ.get("MAX_OPEN_TRIGGERS", "1"))
YIELD_WINDOW_MIN = int(os.environ.get("YIELD_WINDOW_MIN", "5"))
YIELD_EPSILON = float(os.environ.get("YIELD_EPSILON", "0.005"))

DECIDE_MODEL = os.environ.get("DECIDE_MODEL", "claude-sonnet-5-5")
PLAN_MODEL = os.environ.get("PLAN_MODEL", "claude-sonnet-5-5")

# --------------------------------------------------------------------------
# v2: operator inputs (inputs.py)
# --------------------------------------------------------------------------

# A manually entered 10Y yield counts as missing after this many minutes —
# yield-level triggers then fail closed and Discord asks for a fresh one.
YIELD_MAX_AGE_MIN = float(os.environ.get("YIELD_MAX_AGE_MIN", "20"))
# GEX read off a screenshot is shown as STALE to decide() after this long.
GEX_MAX_AGE_MIN = float(os.environ.get("GEX_MAX_AGE_MIN", "90"))
# Warn when the SPX printed in a fresh GEX screenshot differs from the
# SPY-based estimate by more than this many points.
SPX_ESTIMATE_DRIFT_WARN_PTS = float(os.environ.get("SPX_ESTIMATE_DRIFT_WARN_PTS", "2"))
# How often main.py polls state/ for plan / yield / GEX changes and checks
# time-based reminders. Runs on its own timer, independent of ticks.
INPUT_POLL_S = float(os.environ.get("INPUT_POLL_S", "5"))
# v1 generated a plan automatically at 08:00 ET. In v2 the plan comes from
# Claude Code's /premarket command, so this is off unless explicitly enabled.
PLAN_AUTOGEN = os.environ.get("PLAN_AUTOGEN", "false").strip().lower() in ("1", "true", "yes")

# Thinking/tool-use depth per call site: low|medium|high|xhigh|max, or blank
# to leave it to the model's own default. These are deliberately separate —
# the two call sites want opposite things:
#   - decide() is a constrained gate between three pre-defined actions with
#     all context already supplied, on a 30s fail-closed timeout. Low effort
#     suits it, and directly helps the timeouts already seen in journal.db.
#   - generate_plan()/replan() do live WebSearch research (GEX/news/
#     sentiment) and produce every price level the engine trades against all
#     day. Lower effort here means fewer searches and shallower analysis —
#     this is the last place to economize.
PLAN_EFFORT = os.environ.get("PLAN_EFFORT", "") or None
DECIDE_EFFORT = os.environ.get("DECIDE_EFFORT", "") or None

# decide() carries no tools (pure completion, no search) — 30s from SPEC.md
# holds up fine. generate_plan()/replan() run live WebSearch for GEX/news/
# sentiment; measured end-to-end at ~382s for a real daily_plan.md prompt via
# the Agent SDK, so the original spec'd 120s was too tight for that research
# depth. Sized with headroom above the observed run, not the original guess.
DECIDE_TIMEOUT_S = 30
PLAN_TIMEOUT_S = 600
REPLAN_TIMEOUT_S = 300

# --------------------------------------------------------------------------
# M3: live loop
# --------------------------------------------------------------------------

STATE_DIR = os.environ.get("STATE_DIR", "state")
DAILY_PLANS_DIR = os.environ.get("DAILY_PLANS_DIR", "daily_plans")
JOURNAL_DB_PATH = os.path.join(STATE_DIR, "journal.db")
HALT_FILE_PATH = os.path.join(STATE_DIR, "HALT")

MARKET_TZ = "America/New_York"
SGT_TZ = "Asia/Singapore"
ENTRY_CUTOFF_ET = "15:00"

# feed.py: which live source main.py's run_live() uses — "polygon" or "moomoo"
FEED_SOURCE = os.environ.get("FEED_SOURCE", "moomoo")  # v2 default: Moomoo (SPY-based)

# feed.py: Polygon.io indices websocket
POLYGON_WS_URL = os.environ.get("POLYGON_WS_URL", "wss://socket.polygon.io/indices")
POLYGON_SPX_SYMBOL = os.environ.get("POLYGON_SPX_SYMBOL", "I:SPX")
POLYGON_YIELD_SYMBOL = os.environ.get("POLYGON_YIELD_SYMBOL", "I:TNX")

# feed.py: Moomoo (Futu) OpenD gateway — desktop app must be running and
# logged in, with OpenD running locally. Confirmed live: Moomoo's OpenAPI
# rejects subscribing to US indices outright ("US stock indices are not
# supported"), so SPX is estimated from SPY (the ETF) scaled by a ratio
# calibrated against the plan's spx_reference — see feed.MoomooFeed.
# SPY_SPX_RATIO is only the initial guess before the first calibration.
MOOMOO_HOST = os.environ.get("MOOMOO_HOST", "127.0.0.1")
MOOMOO_PORT = int(os.environ.get("MOOMOO_PORT", "11111"))
MOOMOO_SPY_CODE = os.environ.get("MOOMOO_SPY_CODE", "US.SPY")
MOOMOO_SPY_SPX_RATIO = float(os.environ.get("MOOMOO_SPY_SPX_RATIO", "10.06"))

FEED_RECONNECT_MIN_S = 1
FEED_RECONNECT_MAX_S = 60
FEED_STALE_S = 10  # dead-man rule: no tick for this long during RTH -> FeedStale
HEARTBEAT_INTERVAL_S = float(os.environ.get("HEARTBEAT_INTERVAL_S", "60"))

# releases.py: Finnhub economic calendar
FINNHUB_CALENDAR_URL = os.environ.get(
    "FINNHUB_CALENDAR_URL", "https://finnhub.io/api/v1/calendar/economic"
)
RELEASE_POLL_INTERVAL_S = 2
RELEASE_POLL_MAX_S = 90

# scheduler.py cron times (ET)
PLAN_GENERATION_TIME_ET = "08:00"
PLAN_SUMMARY_TIME_ET = "08:05"
ENTRY_CUTOFF_NOTICE_TIME_ET = "15:00"
EOD_SUMMARY_TIME_ET = "16:05"
