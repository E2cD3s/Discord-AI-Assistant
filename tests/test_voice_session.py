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

    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    guild_state = SimpleNamespace(_voice_clients={}, _remove_voice_client=lambda guild_id: guild_state._voice_clients.pop(guild_id, None))

    channel = SimpleNamespace(guild=SimpleNamespace(id=42, _state=guild_state, _voice_states={}, change_voice_state=lambda **_: None))
    connect_calls: list[bool] = []

    async def fake_connect(*, reconnect: bool):
        connect_calls.append(reconnect)
        if reconnect:
            raise discord.errors.ConnectionClosed(_DummySocket(), shard_id=None, code=4006)
        voice_client = _DummyVoiceClient(channel)
        guild_state._voice_clients[channel.guild.id] = voice_client
        return voice_client

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=channel.guild,
    )

    voice_client = _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert isinstance(voice_client, _DummyVoiceClient)
    assert connect_calls == [False]


def test_join_raises_helpful_error_when_voice_gateway_closes(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    channel = SimpleNamespace(guild=SimpleNamespace())

    async def fake_connect(*, reconnect: bool):  # noqa: ARG001
        raise discord.errors.ConnectionClosed(_DummySocket(4014), shard_id=None, code=4014)

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=channel.guild,
    )

    with pytest.raises(RuntimeError) as excinfo:
        _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert "close code 4014" in str(excinfo.value)


def test_join_raises_after_reconnect_attempts_when_close_code_4006_persists(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    channel = SimpleNamespace(guild=SimpleNamespace())

    async def fake_connect(*, reconnect: bool):  # noqa: ARG001
        raise discord.errors.ConnectionClosed(_DummySocket(4006), shard_id=None, code=4006)

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=channel.guild,
    )

    with pytest.raises(RuntimeError) as excinfo:
        _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert "invalidated the voice websocket" in str(excinfo.value)


def test_join_clears_cached_voice_client_and_voice_state_after_4006(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    disconnect_calls: list[bool] = []
    cleanup_calls: list[None] = []
    change_voice_state_calls: list[object] = []
    removed_voice_clients: list[int] = []

    async def fake_disconnect(*, force: bool):
        disconnect_calls.append(force)

    def fake_cleanup() -> None:
        cleanup_calls.append(None)

    async def fake_change_voice_state(*, channel=None, **_kwargs):
        change_voice_state_calls.append(channel)

    guild_state = SimpleNamespace(_voice_clients={})

    def remove_voice_client(guild_id: int) -> None:
        removed_voice_clients.append(guild_id)
        guild_state._voice_clients.pop(guild_id, None)

    guild_state._remove_voice_client = remove_voice_client  # type: ignore[attr-defined]
    guild_state._get_voice_client = lambda guild_id: guild_state._voice_clients.get(guild_id)  # type: ignore[attr-defined]

    voice_client = SimpleNamespace(
        disconnect=fake_disconnect,
        cleanup=fake_cleanup,
        channel=SimpleNamespace(id=999),
    )
    guild = SimpleNamespace(
        id=101,
        voice_client=None,
        _state=guild_state,
        _voice_states={555: object()},
        me=SimpleNamespace(id=555),
        change_voice_state=fake_change_voice_state,
    )

    guild_state._voice_clients[guild.id] = voice_client

    channel = SimpleNamespace(id=321, guild=guild)

    connect_attempts = 0

    async def fake_connect(*, reconnect: bool):
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts == 1:
            raise discord.errors.ConnectionClosed(_DummySocket(), shard_id=None, code=4006)
        new_voice_client = _DummyVoiceClient(channel)
        guild_state._voice_clients[guild.id] = new_voice_client
        guild.voice_client = new_voice_client
        return new_voice_client

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=guild,
    )

    result = _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert isinstance(result, _DummyVoiceClient)
    assert disconnect_calls == [True]
    assert cleanup_calls == [None]
    assert change_voice_state_calls == [None]
    assert removed_voice_clients == [guild.id]
    assert guild._voice_states == {}


def test_join_recovers_from_client_exception_with_stale_connection(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    cleanup_calls: list[None] = []
    disconnect_calls: list[bool] = []

    async def fake_disconnect(*, force: bool):
        disconnect_calls.append(force)

    def fake_cleanup() -> None:
        cleanup_calls.append(None)

    guild_state = SimpleNamespace(_voice_clients={})

    def remove_voice_client(guild_id: int) -> None:
        guild_state._voice_clients.pop(guild_id, None)

    guild_state._remove_voice_client = remove_voice_client  # type: ignore[attr-defined]
    guild_state._get_voice_client = lambda guild_id: guild_state._voice_clients.get(guild_id)  # type: ignore[attr-defined]

    guild_voice_client = SimpleNamespace(
        disconnect=fake_disconnect,
        cleanup=fake_cleanup,
        channel=SimpleNamespace(id=999),
    )
    guild = SimpleNamespace(
        id=202,
        voice_client=None,
        _state=guild_state,
        _voice_states={},
        change_voice_state=lambda **_: None,
    )

    guild_state._voice_clients[guild.id] = guild_voice_client

    channel = SimpleNamespace(id=654, guild=guild)

    attempts: list[bool] = []

    async def fake_connect(*, reconnect: bool):
        attempts.append(reconnect)
        if len(attempts) == 1:
            raise discord.ClientException("Already connected to a voice channel.")
        new_voice_client = _DummyVoiceClient(channel)
        guild_state._voice_clients[guild.id] = new_voice_client
        guild.voice_client = new_voice_client
        return new_voice_client

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=guild,
    )

    result = _EVENT_LOOP.run_until_complete(session.join(ctx))

    assert isinstance(result, _DummyVoiceClient)
    assert attempts == [False, False]
    assert disconnect_calls == [True]
    assert cleanup_calls == [None]


def test_join_serializes_parallel_connection_attempts(monkeypatch):
    monkeypatch.setattr(discord.voice_client, "has_nacl", True, raising=False)

    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    guild_state = SimpleNamespace(_voice_clients={})

    guild = SimpleNamespace(id=303, voice_client=None, _state=guild_state)
    channel = SimpleNamespace(id=404, guild=guild)

    connect_calls: list[bool] = []

    async def fake_connect(*, reconnect: bool):
        connect_calls.append(reconnect)
        await asyncio.sleep(0)
        voice_client = _DummyVoiceClient(channel)
        guild.voice_client = voice_client
        guild_state._voice_clients[guild.id] = voice_client
        return voice_client

    channel.connect = fake_connect  # type: ignore[assignment]

    ctx = SimpleNamespace(
        author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        voice_client=None,
        guild=guild,
    )

    async def _attempt_join() -> _DummyVoiceClient:
        result = await session.join(ctx)
        assert isinstance(result, _DummyVoiceClient)
        return result

    first, second = _EVENT_LOOP.run_until_complete(
        asyncio.gather(_attempt_join(), _attempt_join())
    )

    assert first is second
    assert connect_calls == [False]

