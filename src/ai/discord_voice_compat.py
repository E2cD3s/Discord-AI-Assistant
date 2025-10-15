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
import socket
import struct
import threading
import time
from collections import deque
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


if opus is not None and not hasattr(opus, "DecodeManager"):
    class _CompatDecodeManager(threading.Thread):
        """Lightweight stand-in for :class:`py-cord`'s ``DecodeManager``.

        The upstream ``discord.py`` library does not bundle the voice receive
        helpers that py-cord exposes.  Some optional packages provide
        ``discord.sinks`` but omit the decode manager class, leading to an
        ``AttributeError`` when our compatibility layer attempts to start
        recording.  This implementation mirrors the behaviour of py-cord's
        version closely enough for our needs while avoiding the dependency on
        py-cord itself.
        """

        def __init__(self, client: discord.VoiceClient) -> None:
            super().__init__(daemon=True, name="DecodeManager")
            self.client = client
            self._decoder_cache: dict[int, opus.Decoder] = {}
            self._queue: deque[RawData] = deque()
            self._queue_lock = threading.Lock()
            self._queue_event = threading.Event()
            self._stop_event = threading.Event()

        def decode(self, opus_frame: RawData) -> None:
            if RawData is not None and not isinstance(opus_frame, RawData):
                raise TypeError("opus_frame should be a RawData object.")

            with self._queue_lock:
                self._queue.append(opus_frame)
            self._queue_event.set()

        def run(self) -> None:  # pragma: no cover - requires voice hardware
            while True:
                self._queue_event.wait(0.05)

                while True:
                    with self._queue_lock:
                        if self._queue:
                            packet = self._queue.popleft()
                        else:
                            packet = None

                    if packet is None:
                        break

                    decrypted = getattr(packet, "decrypted_data", None)
                    if decrypted is None:
                        continue

                    try:
                        decoder = self._get_decoder(packet.ssrc)
                        packet.decoded_data = decoder.decode(decrypted)
                    except opus.OpusError:
                        _LOGGER.exception("Failed to decode Opus frame in voice receive thread.")
                        continue

                    try:
                        self.client.recv_decoded_audio(packet)
                    except Exception:  # pragma: no cover - defensive guard
                        _LOGGER.exception("Voice client failed to handle decoded audio packet.")

                if self._stop_event.is_set() and not self._has_pending_packets():
                    break

                self._queue_event.clear()

        def stop(self) -> None:
            self._stop_event.set()
            self._queue_event.set()
            if self.is_alive():
                self.join(timeout=1.0)
            self._decoder_cache.clear()

        def _get_decoder(self, ssrc: int) -> opus.Decoder:
            try:
                return self._decoder_cache[ssrc]
            except KeyError:
                decoder = opus.Decoder()
                self._decoder_cache[ssrc] = decoder
                return decoder

        def _has_pending_packets(self) -> bool:
            with self._queue_lock:
                return bool(self._queue)

    opus.DecodeManager = _CompatDecodeManager  # type: ignore[attr-defined]


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

    def _locate_voice_socket(self: discord.VoiceClient) -> Any:
        """Return a socket-like object suitable for ``select`` operations."""

        primary = getattr(self, "socket", None) or getattr(self, "udp", None)
        if primary is None:
            return None

        seen: set[int] = set()
        candidate: Any | None = primary
        while candidate is not None and id(candidate) not in seen:
            seen.add(id(candidate))

            fileno = getattr(candidate, "fileno", None)
            if callable(fileno):
                try:
                    fd = fileno()
                except (AttributeError, OSError, ValueError, TypeError):
                    fd = None
                else:
                    if isinstance(fd, int):
                        return candidate

            for attr in ("socket", "_socket", "sock", "transport", "udp"):
                next_candidate = getattr(candidate, attr, None)
                if next_candidate is not None and id(next_candidate) not in seen:
                    candidate = next_candidate
                    break
            else:
                break

        return None

    def _empty_socket(self: discord.VoiceClient) -> None:
        sock = _locate_voice_socket(self)
        if sock is None:
            _LOGGER.debug("No selectable voice socket found while draining pending data.")
            return

        while True:
            try:
                ready, _, _ = select.select([sock], [], [], 0.0)
            except (OSError, ValueError, TypeError):
                _LOGGER.debug("Voice socket unavailable while draining pending data.", exc_info=True)
                break

            if not ready:
                break
            for ready_sock in ready:
                with suppress(Exception):  # pragma: no branch - defensive guard
                    ready_sock.recv(4096)

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

        if getattr(self, "socket", None) is None:
            raise RecordingException("Voice UDP socket is not initialised.")

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
                udp_socket = _locate_voice_socket(self)
                if udp_socket is None:
                    time.sleep(0.01)
                    continue

                try:
                    ready, _, err = select.select([udp_socket], [], [udp_socket], 0.01)
                except Exception:  # pragma: no cover - defensive guard
                    _LOGGER.exception("Voice receive select() failed in %s", log_context)
                    break

                if not ready:
                    if err:
                        _LOGGER.debug("Voice socket reported errors: %s", err)
                    continue

                try:
                    data = udp_socket.recv(4096)
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

            loop = getattr(self, "loop", None)

            def _invoke_callback() -> None:
                try:
                    result = callback(sink, *args)
                    if inspect.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception:
                    _LOGGER.exception("Recording completion callback raised in %s", log_context)

            if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed():
                loop.call_soon_threadsafe(_invoke_callback)
            else:
                try:
                    result = callback(sink, *args)
                    if inspect.iscoroutine(result):
                        asyncio.run(result)
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
