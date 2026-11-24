"""tools/discord_ping.py: one test message, and a plain-English reason when
it fails. No network: a fake client stands in for discord.Client."""

from __future__ import annotations

import sys
from pathlib import Path

import discord
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import discord_ping  # noqa: E402


class FakeResponse:
    def __init__(self, status: int):
        self.status = status
        self.reason = "x"


class FakeChannel:
    def __init__(self, send_error: Exception | None = None):
        self.sent: list[str] = []
        self.send_error = send_error
        self.name = "spx-alerts"
        self.guild = type("G", (), {"name": "My Server"})()

    async def send(self, text):
        if self.send_error:
            raise self.send_error
        self.sent.append(text)


class FakeClient:
    def __init__(self, login_error=None, fetch_error=None, channel=None):
        self.login_error = login_error
        self.fetch_error = fetch_error
        self.channel = channel or FakeChannel()
        self.closed = False
        self.user = "spx-bot#0001"

    async def login(self, token):
        if self.login_error:
            raise self.login_error

    async def fetch_channel(self, channel_id):
        if self.fetch_error:
            raise self.fetch_error
        return self.channel

    async def close(self):
        self.closed = True


def http_error(cls, status):
    return cls(FakeResponse(status), "msg")


async def run(client, token="tok", channel_id="123"):
    return await discord_ping.ping(token, channel_id, "hello", client_factory=lambda: client)


async def test_ok_sends_the_message_and_names_the_channel():
    client = FakeClient()
    ok, line = await run(client)
    assert ok is True
    assert client.channel.sent == ["hello"]
    assert "#spx-alerts" in line and "My Server" in line
    assert client.closed


@pytest.mark.parametrize(
    "token,channel_id,needle",
    [
        ("", "123", "DISCORD_BOT_TOKEN is not set"),
        ("tok", "", "DISCORD_CHANNEL_ID is not set"),
        ("tok", "#alerts", "must be the numeric channel ID"),
    ],
)
async def test_missing_or_bad_settings_never_touch_discord(token, channel_id, needle):
    client = FakeClient()
    ok, line = await run(client, token=token, channel_id=channel_id)
    assert ok is False
    assert needle in line
    assert client.channel.sent == []


async def test_bad_token():
    ok, line = await run(FakeClient(login_error=discord.LoginFailure("bad")))
    assert ok is False
    assert "token was rejected" in line


async def test_unknown_channel():
    ok, line = await run(FakeClient(fetch_error=http_error(discord.NotFound, 404)))
    assert ok is False
    assert "channel 123 not found" in line


async def test_bot_cannot_see_channel():
    ok, line = await run(FakeClient(fetch_error=http_error(discord.Forbidden, 403)))
    assert ok is False
    assert "View Channel" in line


async def test_bot_cannot_send():
    client = FakeClient(channel=FakeChannel(send_error=http_error(discord.Forbidden, 403)))
    ok, line = await run(client)
    assert ok is False
    assert "Send Messages" in line
    assert client.closed


async def test_network_error_is_reported():
    ok, line = await run(FakeClient(login_error=OSError("ssl handshake failed")))
    assert ok is False
    assert "could not reach Discord" in line
    assert "ssl handshake failed" in line


async def test_output_is_ascii_and_never_shows_the_token():
    secret = "SECRET.TOKEN.VALUE"
    for client in (FakeClient(), FakeClient(login_error=discord.LoginFailure(secret))):
        _, line = await discord_ping.ping(secret, "123", "hi", client_factory=lambda c=client: c)
        assert secret not in line
        line.encode("ascii")
