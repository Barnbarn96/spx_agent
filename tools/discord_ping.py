"""Send one test message to the Discord alert channel, using the same
DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID from .env that main.py uses.

    python tools/discord_ping.py
    python tools/discord_ping.py --message "custom text"

Prints one line: "Discord OK: ..." or "Discord FAILED: <reason and fix>".
Exit code 0 on success, 1 on failure. Uses Discord's REST API only (no
gateway connection), so it doesn't need main.py stopped. It never prints
the token.
"""

from __future__ import annotations

import _common  # noqa: F401  (sets up imports / cwd)

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import discord

import config

ET = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def default_message(now: datetime | None = None) -> str:
    now = now or datetime.now(ET)
    return (
        "spx_agent ping: Discord alerts are working. "
        f"{now.astimezone(ET):%H:%M:%S} ET / {now.astimezone(SGT):%H:%M:%S} SGT"
    )


async def ping(
    token: str,
    channel_id: str,
    message: str,
    client_factory: Callable[[], discord.Client] | None = None,
) -> tuple[bool, str]:
    """Returns (ok, one-line ASCII result). Never includes the token."""
    token = token.strip()
    channel_id = channel_id.strip()
    if not token:
        return False, "DISCORD_BOT_TOKEN is not set (check .env is in the repo folder)."
    if not channel_id:
        return False, "DISCORD_CHANNEL_ID is not set (check .env is in the repo folder)."
    if not channel_id.isdigit():
        return False, (
            f"DISCORD_CHANNEL_ID must be the numeric channel ID, not {channel_id!r}. "
            "In Discord: User Settings > Advanced > Developer Mode on, then right-click "
            "the channel > Copy Channel ID."
        )

    client = client_factory() if client_factory else discord.Client(intents=discord.Intents.none())
    step = "logging in"
    try:
        try:
            await client.login(token)
        except discord.LoginFailure:
            return False, (
                "the bot token was rejected. Reset it in the Discord developer portal "
                "(your app > Bot > Reset Token) and update DISCORD_BOT_TOKEN in .env."
            )
        step = "finding the channel"
        try:
            channel = await client.fetch_channel(int(channel_id))
        except discord.NotFound:
            return False, (
                f"channel {channel_id} not found. Check the ID, and that the bot has been "
                "invited to that server."
            )
        except discord.Forbidden:
            return False, (
                f"the bot can't see channel {channel_id}. Give its role the View Channel "
                "permission on that channel (or invite it to the server)."
            )
        where = f"#{getattr(channel, 'name', channel_id)}"
        guild = getattr(channel, "guild", None)
        if guild is not None:
            where += f" in {guild.name}"

        step = "sending"
        try:
            await channel.send(message)
        except discord.Forbidden:
            return False, (
                f"the bot can see {_ascii(where)} but can't post there. Give its role the "
                "Send Messages permission on that channel."
            )
        return True, _ascii(f"sent to {where} as {client.user}. Check the channel.")
    except discord.HTTPException as e:
        return False, _ascii(f"Discord returned HTTP {e.status} while {step}: {e.text or e}")
    except (OSError, asyncio.TimeoutError) as e:
        detail = str(e).replace(token, "<token>") if token else str(e)
        return False, _ascii(
            f"could not reach Discord while {step} ({type(e).__name__}: {detail}). "
            "Check the internet connection, VPN or a corporate proxy/SSL inspection."
        )
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/discord_ping.py")
    p.add_argument("--message", help="text to send (default: a timestamped ping)")
    args = p.parse_args(argv)

    ok, line = asyncio.run(
        ping(config.DISCORD_BOT_TOKEN, config.DISCORD_CHANNEL_ID, args.message or default_message())
    )
    if ok:
        print(f"Discord OK: {line}")
        return 0
    print(f"Discord FAILED: {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
