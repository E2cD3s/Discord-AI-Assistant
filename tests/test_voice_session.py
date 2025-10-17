import asyncio
from types import SimpleNamespace

import logging

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


def test_validate_voice_permissions_raises_when_connect_missing():
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    permissions = SimpleNamespace(view_channel=True, connect=False)
    channel = SimpleNamespace(
        guild=SimpleNamespace(me=SimpleNamespace()),
        permissions_for=lambda _member: permissions,
    )

    with pytest.raises(RuntimeError) as excinfo:
        session._validate_voice_permissions(channel)

    assert "Connect" in str(excinfo.value)


def test_validate_voice_permissions_warns_for_voice_activity(caplog):
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    permissions = SimpleNamespace(
        view_channel=True,
        connect=True,
        speak=True,
        use_voice_activation=False,
    )
    channel = SimpleNamespace(
        guild=SimpleNamespace(me=SimpleNamespace()),
        permissions_for=lambda _member: permissions,
    )

    with caplog.at_level(logging.WARNING):
        session._validate_voice_permissions(channel)

    assert "Use Voice Activity" in caplog.text


def test_wait_until_voice_ready_completes_when_gateway_recovers():
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    udp_client = SimpleNamespace(connected=False)
    ws = SimpleNamespace(connected=False, udp=udp_client)

    voice_client = SimpleNamespace(ws=ws, channel="test-channel")
    voice_client.is_connected = lambda: True  # type: ignore[attr-defined]

    async def _mark_ready():
        await asyncio.sleep(0.05)
        ws.connected = True
        udp_client.connected = True

    _EVENT_LOOP.create_task(_mark_ready())

    _EVENT_LOOP.run_until_complete(session._wait_until_voice_ready(voice_client, timeout=0.5))


def test_wait_until_voice_ready_times_out():
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    voice_client = SimpleNamespace(
        ws=SimpleNamespace(connected=False, udp=SimpleNamespace(connected=False)),
        channel="timeout-channel",
    )
    voice_client.is_connected = lambda: True  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError) as excinfo:
        _EVENT_LOOP.run_until_complete(session._wait_until_voice_ready(voice_client, timeout=0.2))

    assert "did not become ready" in str(excinfo.value)


def test_wait_for_playback_to_finish_allows_existing_audio():
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    state = {"playing": True}
    stop_called = False

    def stop():
        nonlocal stop_called
        stop_called = True

    voice_client = SimpleNamespace(
        channel="test-channel",
        is_playing=lambda: state["playing"],
        stop=stop,
    )

    async def _finish_playback():
        await asyncio.sleep(0.05)
        state["playing"] = False

    _EVENT_LOOP.create_task(_finish_playback())

    _EVENT_LOOP.run_until_complete(
        session._wait_for_playback_to_finish(voice_client, timeout=0.5)
    )

    assert not stop_called


def test_wait_for_playback_to_finish_times_out_and_stops(caplog):
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    state = {"playing": True}
    stop_called = False

    def stop():
        nonlocal stop_called
        stop_called = True
        state["playing"] = False

    voice_client = SimpleNamespace(
        channel="timeout-channel",
        is_playing=lambda: state["playing"],
        stop=stop,
    )

    with caplog.at_level(logging.WARNING):
        _EVENT_LOOP.run_until_complete(
            session._wait_for_playback_to_finish(voice_client, timeout=0.1)
        )

    assert stop_called
    assert "Timed out waiting for playback" in caplog.text


def test_diagnose_channel_silence_warns_when_all_members_muted(caplog):
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    voice_state = SimpleNamespace(
        self_mute=True,
        mute=False,
        self_deaf=False,
        deaf=False,
        suppressed=False,
    )
    member = SimpleNamespace(
        id=123,
        name="Listener",
        bot=False,
        voice=voice_state,
    )
    channel = SimpleNamespace(
        members=[member],
        guild=SimpleNamespace(me=SimpleNamespace(id=999)),
    )
    voice_client = SimpleNamespace(channel=channel)

    with caplog.at_level(logging.INFO):
        session._diagnose_channel_silence(voice_client)

    assert "Listener" in caplog.text
    assert "All non-bot members" in caplog.text


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


def test_listen_once_waits_for_playback_before_recording(monkeypatch):
    session = VoiceSession(SimpleNamespace(), SimpleNamespace())

    channel = SimpleNamespace(id=123)
    guild = SimpleNamespace(id=456)

    state = {"playing": True}
    stop_called = False
    start_states: list[bool] = []
    stop_recording_called = False

    def stop():
        nonlocal stop_called
        stop_called = True

    def is_playing():
        return state["playing"]

    def start_recording(_sink, after):
        start_states.append(state["playing"])
        after(SimpleNamespace(audio_data={}, cleanup=lambda: None, vc=voice_client))

    def stop_recording():
        nonlocal stop_recording_called
        stop_recording_called = True

    async def fake_handle_sink(_sink, _cb):
        return None

    async def on_transcription(*_args):
        return None

    voice_client = SimpleNamespace(
        channel=channel,
        guild=guild,
        ws=SimpleNamespace(connected=True, udp=SimpleNamespace(connected=True)),
        is_playing=is_playing,
        stop=stop,
        start_recording=start_recording,
        stop_recording=stop_recording,
    )
    voice_client.is_connected = lambda: True  # type: ignore[attr-defined]

    monkeypatch.setattr(session, "_create_wave_sink", lambda: SimpleNamespace())
    monkeypatch.setattr(session, "_handle_sink", fake_handle_sink)
    monkeypatch.setattr(
        discord,
        "opus",
        SimpleNamespace(is_loaded=lambda: True),
        raising=False,
    )

    async def _finish_playback():
        await asyncio.sleep(0.05)
        state["playing"] = False

    _EVENT_LOOP.create_task(_finish_playback())

    _EVENT_LOOP.run_until_complete(
        session.listen_once(voice_client, on_transcription, timeout=0.2)
    )

    assert start_states == [False]
    assert not stop_called
    assert stop_recording_called


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

