"""Compatibility helpers for Discord voice recording support.

This module backports the voice receive helpers that are available in
``py-cord`` to environments that only provide the upstream
``discord.py`` package.  The upstream library (as of 2.4) exposes voice
playback APIs but does not ship the voice recording helpers that power
``discord.sinks``.  Our bot relies on those helpers (specifically
``VoiceClient.start_recording`` and ``VoiceClient.stop_recording``).

When the helpers are missing we monkey-patch a small subset of the
py-cord implementation (which is licensed under the MIT license) so that
recording works transparently on vanilla ``discord.py`` installations.

The implementation is intentionally conservative and only patches the
attributes we rely on.  All heavy lifting (decoding, sink management,
etc.) still happens inside ``discord.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import select
import struct
import threading
import time
from contextlib import suppress
from typing import Any, Callable

import discord

try:  # pragma: no cover - optional dependency availability
    from discord import opus
    from discord.sinks.core import RawData, Sink
    from discord.sinks.errors import RecordingException
except (ImportError, AttributeError):  # pragma: no cover - handled at runtime
    opus = None  # type: ignore[assignment]
    RawData = None  # type: ignore[assignment]
    Sink = None  # type: ignore[assignment]
    RecordingException = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

_RecordingCallback = Callable[[Sink, Any], Any]


def ensure_voice_recording_support() -> None:
    """Ensure :class:`discord.VoiceClient` exposes recording helpers.

    If the installed Discord library already provides ``start_recording``
    we leave it untouched.  Otherwise we patch in a minimal implementation
    that mirrors the behaviour from py-cord.
    """

    if opus is None or RawData is None or Sink is None or RecordingException is None:
        # The voice sink subsystem is unavailable; there's nothing to patch.
        return

    voice_client_cls = discord.VoiceClient
    if hasattr(voice_client_cls, "start_recording") and hasattr(voice_client_cls, "stop_recording"):
        return

    def _ensure_state(self: discord.VoiceClient) -> None:
        # These attributes are defined by py-cord but not discord.py.
        if not hasattr(self, "recording"):
            self.recording = False  # type: ignore[attribute-defined-outside-init]
        if not hasattr(self, "paused"):
            self.paused = False  # type: ignore[attribute-defined-outside-init]
        if not hasattr(self, "sink"):
            self.sink = None  # type: ignore[attribute-defined-outside-init]
        if not hasattr(self, "decoder"):
            self.decoder = None  # type: ignore[attribute-defined-outside-init]
        if not hasattr(self, "sync_start"):
            self.sync_start = False  # type: ignore[attribute-defined-outside-init]

    def _empty_socket(self: discord.VoiceClient) -> None:
        while True:
            ready, _, _ = select.select([self.socket], [], [], 0.0)
            if not ready:
                break
            for sock in ready:
                with suppress(Exception):  # pragma: no branch - defensive guard
                    sock.recv(4096)

    def _start_recording(
        self: discord.VoiceClient,
        sink: Sink,
        callback: _RecordingCallback,
        *args: Any,
        sync_start: bool = False,
    ) -> None:
        _ensure_state(self)

        if not self.is_connected():
            raise RecordingException("Not connected to voice channel.")
        if self.recording:
            raise RecordingException("Already recording.")
        if not isinstance(sink, Sink):
            raise RecordingException("Must provide a Sink object.")

        _empty_socket(self)

        decoder = opus.DecodeManager(self)
        decoder.start()
        self.decoder = decoder
        self.recording = True
        self.paused = False
        self.sync_start = sync_start
        self.sink = sink
        sink.init(self)

        thread = threading.Thread(
            target=_recv_audio,
            args=(self, sink, callback, args),
            daemon=True,
        )
        thread.start()
        self._recording_thread = thread  # type: ignore[attribute-defined-outside-init]

    def _stop_recording(self: discord.VoiceClient) -> None:
        _ensure_state(self)
        if not self.recording:
            raise RecordingException("Not currently recording.")

        decoder = getattr(self, "decoder", None)
        if decoder is not None:
            with suppress(Exception):
                decoder.stop()
        self.recording = False
        self.paused = False

    def _recv_audio(
        self: discord.VoiceClient,
        sink: Sink,
        callback: _RecordingCallback,
        args: tuple[Any, ...],
    ) -> None:
        self.user_timestamps: dict[int, tuple[int, float]] = {}
        self.starting_time = time.perf_counter()
        log_context = f"voice channel {getattr(self.channel, 'id', 'unknown')}"

        try:
            while self.recording:
                try:
                    ready, _, err = select.select([self.socket], [], [self.socket], 0.01)
                except Exception:  # pragma: no cover - defensive guard
                    _LOGGER.exception("Voice receive select() failed in %s", log_context)
                    break

                if not ready:
                    if err:
                        _LOGGER.debug("Voice socket reported errors: %s", err)
                    continue

                try:
                    data = self.socket.recv(4096)
                except OSError:
                    _LOGGER.exception("Voice socket closed unexpectedly in %s", log_context)
                    _stop_recording(self)
                    break

                try:
                    _unpack_audio(self, data)
                except Exception:  # pragma: no cover - defensive guard
                    _LOGGER.exception("Failed to decode received audio in %s", log_context)

        finally:
            self.stopping_time = time.perf_counter()
            try:
                sink.cleanup()
            except Exception:  # pragma: no cover - defensive guard
                _LOGGER.exception("Voice sink cleanup failed in %s", log_context)

            try:
                result = callback(sink, *args)
                if inspect.iscoroutine(result):
                    future = asyncio.run_coroutine_threadsafe(result, self.loop)
                    future.result()
            except Exception:
                _LOGGER.exception("Recording completion callback raised in %s", log_context)

    def _unpack_audio(self: discord.VoiceClient, data: bytes) -> None:
        if len(data) < 2:
            return
        if 200 <= data[1] <= 204:
            return
        if getattr(self, "paused", False):
            return

        packet = RawData(data, self)
        if packet.decrypted_data == b"\xf8\xff\xfe":
            return
        decoder = getattr(self, "decoder", None)
        if decoder is None:
            return
        decoder.decode(packet)

    def _recv_decoded_audio(self: discord.VoiceClient, packet: RawData) -> None:
        if packet.ssrc not in getattr(self, "user_timestamps", {}):
            if not self.user_timestamps or not getattr(self, "sync_start", False):
                self.first_packet_timestamp = packet.receive_time  # type: ignore[attr-defined]
                silence = 0.0
            else:
                silence = (packet.receive_time - getattr(self, "first_packet_timestamp", packet.receive_time)) * 48000 - 960
        else:
            previous_timestamp, previous_time = self.user_timestamps[packet.ssrc]
            delta_receive = (packet.receive_time - previous_time) * 48000
            delta_timestamp = packet.timestamp - previous_timestamp
            diff = abs(100 - delta_timestamp * 100 / max(delta_receive, 1))
            silence = (delta_receive - 960) if (diff > 60 and delta_timestamp != 960) else (delta_timestamp - 960)

        self.user_timestamps[packet.ssrc] = (packet.timestamp, packet.receive_time)

        silence_frames = max(0, int(silence))
        if silence_frames:
            packet.decoded_data = struct.pack("<h", 0) * silence_frames * opus._OpusStruct.CHANNELS + packet.decoded_data

        while packet.ssrc not in self.ws.ssrc_map:
            time.sleep(0.05)

        user_id = self.ws.ssrc_map[packet.ssrc]["user_id"]
        self.sink.write(packet.decoded_data, user_id)

    # Patch in the helpers if they're missing.
    if not hasattr(voice_client_cls, "empty_socket"):
        voice_client_cls.empty_socket = _empty_socket  # type: ignore[assignment]
    if not hasattr(voice_client_cls, "start_recording"):
        voice_client_cls.start_recording = _start_recording  # type: ignore[assignment]
    if not hasattr(voice_client_cls, "stop_recording"):
        voice_client_cls.stop_recording = _stop_recording  # type: ignore[assignment]
    if not hasattr(voice_client_cls, "recv_audio"):
        voice_client_cls.recv_audio = _recv_audio  # type: ignore[assignment]
    if not hasattr(voice_client_cls, "unpack_audio"):
        voice_client_cls.unpack_audio = _unpack_audio  # type: ignore[assignment]
    if not hasattr(voice_client_cls, "recv_decoded_audio"):
        voice_client_cls.recv_decoded_audio = _recv_decoded_audio  # type: ignore[assignment]


__all__ = ["ensure_voice_recording_support"]
