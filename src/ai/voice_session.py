from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

import discord

try:  # pragma: no cover - optional dependency resolution
    from discord import sinks as discord_sinks
except (ImportError, AttributeError):  # pragma: no cover - handled at runtime
    discord_sinks = None

if TYPE_CHECKING:  # pragma: no cover - typing only
    from discord.sinks import Sink as DiscordSink
else:
    DiscordSink = Any

from ..logging_utils import get_logger
from .stt import SpeechToText
from .tts import TextToSpeech

_LOGGER = get_logger(__name__)

TranscriptionCallback = Callable[[discord.abc.User, str], Awaitable[None]]


class VoiceSession:
    def __init__(self, stt: SpeechToText, tts: TextToSpeech) -> None:
        self._stt = stt
        self._tts = tts
        self._active_recordings: Dict[int, asyncio.Task[None]] = {}
        self._listener_tasks: Dict[int, asyncio.Task[None]] = {}

    def _voice_key(self, voice_client: discord.VoiceClient) -> int:
        guild = getattr(voice_client, "guild", None)
        if guild is not None:
            return guild.id
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            raise RuntimeError("Voice client is not connected to any channel")
        return channel.id

    async def join(
        self,
        ctx: discord.ApplicationContext | discord.ext.commands.Context | discord.Interaction,
    ) -> discord.VoiceClient:
        author = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        voice_state = getattr(author, "voice", None) if author else None
        channel = getattr(voice_state, "channel", None) if voice_state else None
        if channel is None:
            raise RuntimeError("User must be in a voice channel to summon the bot.")
        voice_client = getattr(ctx, "voice_client", None)
        if voice_client is None:
            guild = getattr(ctx, "guild", None)
            voice_client = getattr(guild, "voice_client", None)
        if voice_client:
            if voice_client.channel.id == channel.id:
                return voice_client
            await voice_client.move_to(channel)
            return voice_client
        if not bool(getattr(discord.voice_client, "has_nacl", False)):
            raise RuntimeError(
                "Voice connections require the PyNaCl dependency. "
                "Install 'pynacl' and ensure the voice extra is enabled for py-cord."
            )
        async def _connect() -> discord.VoiceClient:
            last_error: RuntimeError | None = None
            for reconnect in (True, False):
                try:
                    return await channel.connect(reconnect=reconnect)
                except discord.errors.ConnectionClosed as exc:
                    close_code = getattr(exc, "code", None)
                    if close_code == 4006:
                        if reconnect:
                            _LOGGER.warning(
                                "Voice websocket session invalidated with close code 4006. "
                                "Retrying with a fresh voice connection."
                            )
                            continue
                        last_error = RuntimeError(
                            "Discord invalidated the voice websocket (close code 4006) after "
                            "retrying with a fresh connection. Try re-running the join command."
                        )
                    else:
                        last_error = RuntimeError(
                            "Discord closed the voice connection unexpectedly "
                            f"(close code {close_code or 'unknown'}). "
                            "Try running the join command again or restart the bot."
                        )
                    raise last_error from exc
                except Exception as exc:  # pragma: no cover - defensive guard
                    last_error = RuntimeError("Failed to connect to the voice channel")
                    raise last_error from exc

            if last_error is not None:
                raise last_error

            raise RuntimeError("Failed to connect to the voice channel")

        return await _connect()

    async def leave(
        self,
        ctx: discord.ApplicationContext | discord.ext.commands.Context | discord.Interaction,
    ) -> None:
        voice_client = getattr(ctx, "voice_client", None)
        if voice_client is None:
            guild = getattr(ctx, "guild", None)
            voice_client = getattr(guild, "voice_client", None)
        if voice_client:
            await self.stop_listening(voice_client)
            await voice_client.disconnect()

    async def listen_once(
        self,
        voice_client: discord.VoiceClient,
        on_transcription: TranscriptionCallback,
        timeout: float = 20.0,
    ) -> None:
        if voice_client.is_playing():
            voice_client.stop()

        _LOGGER.info(
            "Starting voice capture in channel %s for up to %.1f seconds",
            voice_client.channel,
            timeout,
        )

        wave_sink = self._create_wave_sink()

        def after_recording(completed_sink: DiscordSink, *_) -> None:
            task = asyncio.create_task(self._handle_sink(completed_sink, on_transcription))
            self._active_recordings[self._voice_key(voice_client)] = task

        voice_client.start_recording(wave_sink, after_recording)

        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            _LOGGER.info("Voice capture in %s cancelled", voice_client.channel)
            raise
        finally:
            voice_client.stop_recording()
            task = self._active_recordings.pop(self._voice_key(voice_client), None)
            if task:
                await task
            _LOGGER.info("Completed voice capture in channel %s", voice_client.channel)

    def is_listening(self, voice_client: discord.VoiceClient) -> bool:
        task = self._listener_tasks.get(self._voice_key(voice_client))
        return bool(task and not task.done())

    async def start_listening(
        self,
        voice_client: discord.VoiceClient,
        on_transcription: TranscriptionCallback,
        timeout: float = 20.0,
    ) -> None:
        key = self._voice_key(voice_client)
        if self.is_listening(voice_client):
            _LOGGER.info("Already listening to channel %s", voice_client.channel)
            return

        async def _listen_loop() -> None:
            try:
                while True:
                    await self.listen_once(voice_client, on_transcription, timeout)
            except asyncio.CancelledError:
                _LOGGER.info("Stopped continuous listening in channel %s", voice_client.channel)
                raise
            except Exception:  # pragma: no cover - best effort logging
                _LOGGER.exception("Unexpected error while listening in channel %s", voice_client.channel)

        task = asyncio.create_task(_listen_loop())
        self._listener_tasks[key] = task
        _LOGGER.info("Started continuous listening in channel %s", voice_client.channel)

    async def stop_listening(self, voice_client: discord.VoiceClient) -> None:
        key = self._voice_key(voice_client)
        task = self._listener_tasks.pop(key, None)
        if not task:
            _LOGGER.info("No active listener to stop in channel %s", voice_client.channel)
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _create_wave_sink(self) -> DiscordSink:
        if discord_sinks is None:
            raise RuntimeError(
                "The installed Discord library does not expose voice sinks. "
                "Install 'py-cord[voice]>=2.5.0' to enable voice recording support."
            )
        wave_sink = getattr(discord_sinks, "WaveSink", None)
        if wave_sink is None:
            raise RuntimeError(
                "discord.sinks.WaveSink is unavailable. Update to a newer version of py-cord to continue."
            )
        return wave_sink()

    async def _handle_sink(self, sink: DiscordSink, on_transcription: TranscriptionCallback) -> None:
        try:
            await self._process_sink(sink, on_transcription)
        finally:
            sink.cleanup()

    async def _process_sink(self, sink: DiscordSink, on_transcription: TranscriptionCallback) -> None:
        buffered_audio = []
        for user, audio in sink.audio_data.items():
            if audio is None or audio.file is None:
                continue

            start_time = getattr(audio, "start_time", 0.0)
            audio_bytes = audio.file.getvalue()
            buffered_audio.append((start_time, user, audio_bytes))

        buffered_audio.sort(key=lambda item: item[0])

        if not buffered_audio:
            _LOGGER.info("No audio detected during the last listening window")
            return

        for _, user, audio_bytes in buffered_audio:
            stream = BytesIO(audio_bytes)
            _LOGGER.debug("Transcribing audio captured from user %s", user)
            transcript = await self._stt.transcribe(stream)
            if transcript:
                await on_transcription(user, transcript)
            else:
                _LOGGER.debug("No transcript produced for user %s", user)

    async def speak(self, voice_client: discord.VoiceClient, text: str) -> Optional[Path]:
        audio_path = await self._tts.synthesize(text)
        if voice_client.is_playing():
            voice_client.stop()

        def after_playback(error: Optional[Exception]) -> None:
            if error:
                _LOGGER.error("FFmpeg playback error: %s", error)
            audio_path.unlink(missing_ok=True)

        audio_source = discord.FFmpegPCMAudio(str(audio_path))
        voice_client.play(audio_source, after=after_playback)
        return audio_path


__all__ = ["VoiceSession", "TranscriptionCallback"]
