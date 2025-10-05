import asyncio
from types import SimpleNamespace

import discord
import pytest

from src.ai.voice_session import VoiceSession


_EVENT_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_EVENT_LOOP)


class _DummySocket:
    def __init__(self, close_code: int | None = None) -> None:
        self.close_code = close_code


class _DummyVoiceClient:
    def __init__(self, channel: object) -> None:
        self.channel = channel


def test_join_retries_with_fresh_voice_session_when_invalidated(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    channel = SimpleNamespace()
    connect_calls: list[bool] = []

    async def fake_connect(*, reconnect: bool):
        connect_calls.append(reconnect)
        if reconnect:
            raise discord.errors.ConnectionClosed(_DummySocket(), shard_id=None, code=4006)
        return _DummyVoiceClient(channel)

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=SimpleNamespace(voice_client=None),
    )

    voice_client = _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert isinstance(voice_client, _DummyVoiceClient)
    assert connect_calls == [True, False]


def test_join_raises_helpful_error_when_voice_gateway_closes(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    channel = SimpleNamespace()

    async def fake_connect(*, reconnect: bool):  # noqa: ARG001
        raise discord.errors.ConnectionClosed(_DummySocket(4014), shard_id=None, code=4014)

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=SimpleNamespace(voice_client=None),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert "close code 4014" in str(excinfo.value)


def test_join_raises_after_reconnect_attempts_when_close_code_4006_persists(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    channel = SimpleNamespace()

    async def fake_connect(*, reconnect: bool):  # noqa: ARG001
        raise discord.errors.ConnectionClosed(_DummySocket(4006), shard_id=None, code=4006)

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=SimpleNamespace(voice_client=None),
    )

    with pytest.raises(RuntimeError) as excinfo:
        _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert "invalidated the voice websocket" in str(excinfo.value)

