"""Record GEX levels read off Moomoo's Options -> Analysis -> Gamma Exposure
screen (0DTE expiration) for the running main.py.

    python tools/set_gex.py --expiry 2026-11-24 --flip 7300.59 \
        --call-wall 7200 --put-wall 7200 --max-pain 7405 --spx 7237.17 --as-of 15:15

--as-of is the clock time on the screenshot, in SGT by default (phone
status bar); pass --tz ET for an ET time, or omit it to use "now".
Rejects a screenshot whose expiration isn't today's (ET) date unless
--allow-other-expiry is given. Prints what changed since the last reading.
"""

from __future__ import annotations

import _common  # noqa: F401  (sets up imports / cwd)

import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import inputs

TZS = {"SGT": ZoneInfo("Asia/Singapore"), "ET": ZoneInfo("America/New_York")}


def _parse_as_of(value: str | None, tz_name: str) -> datetime:
    now_et = datetime.now(inputs.ET)
    if value is None or value == "now":
        return now_et
    tz = TZS[tz_name]
    hh, mm = (int(x) for x in value.split(":"))
    now_local = datetime.now(tz)
    local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local - now_local > timedelta(minutes=5):
        local -= timedelta(days=1)  # e.g. a 23:55 screenshot recorded at 00:05
    return local.astimezone(inputs.ET)


def _diff(old: inputs.GexInput | None, new: inputs.GexInput) -> list[str]:
    if old is None or old.expiry_date != new.expiry_date:
        return ["first reading for this expiry"]
    out = []
    for name in ("gamma_flip", "call_wall", "put_wall", "max_pain"):
        a, b = getattr(old, name), getattr(new, name)
        if a != b:
            out.append(f"{name.replace('_', ' ')} {a:g} -> {b:g}" if a is not None and b is not None
                       else f"{name.replace('_', ' ')} {a} -> {b}")
    return out or ["levels unchanged"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/set_gex.py")
    p.add_argument("--expiry", required=True, help="Expiration shown in the screenshot, YYYY-MM-DD")
    p.add_argument("--flip", type=float, required=True, help="Gamma flip level")
    p.add_argument("--call-wall", type=float)
    p.add_argument("--put-wall", type=float)
    p.add_argument("--max-pain", type=float)
    p.add_argument("--spx", type=float, help="The .SPX price shown in the screenshot")
    p.add_argument("--as-of", default=None, help="HH:MM clock time of the screenshot, or 'now'")
    p.add_argument("--tz", default="SGT", choices=sorted(TZS))
    p.add_argument("--notes", default="")
    p.add_argument("--allow-other-expiry", action="store_true")
    args = p.parse_args(argv)

    today_et = datetime.now(inputs.ET).date()
    expiry = date.fromisoformat(args.expiry)
    if expiry != today_et and not args.allow_other_expiry:
        print(
            f"REJECTED: screenshot expiration {expiry} is not today's 0DTE expiry ({today_et} ET). "
            "Switch the Expiration dropdown to today and resend, or pass --allow-other-expiry.",
            file=sys.stderr,
        )
        return 1

    previous = inputs.read_gex()
    try:
        gex = inputs.GexInput(
            expiry_date=expiry,
            gamma_flip=args.flip,
            call_wall=args.call_wall,
            put_wall=args.put_wall,
            max_pain=args.max_pain,
            spx_in_shot=args.spx,
            as_of=_parse_as_of(args.as_of, args.tz),
            notes=args.notes,
        )
    except ValueError as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1
    inputs.write_gex(gex)

    now = datetime.now(inputs.ET)
    print("GEX recorded: " + gex.summary(args.spx, now))
    print("Changes: " + "; ".join(_diff(previous, gex)))
    if previous is not None and args.spx is not None and previous.spx_in_shot is not None:
        was = "below" if previous.spx_in_shot < previous.gamma_flip else "above"
        now_side = "below" if args.spx < gex.gamma_flip else "above"
        if was != now_side:
            print(f"REGIME CHANGE: SPX moved from {was} to {now_side} the gamma flip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
