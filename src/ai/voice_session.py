from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

import discord

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

    async def join(self, ctx: discord.ApplicationContext | discord.ext.commands.Context) -> discord.VoiceClient:
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise RuntimeError("User must be in a voice channel to summon the bot.")
        channel = ctx.author.voice.channel
        if ctx.voice_client:
            if ctx.voice_client.channel.id == channel.id:
                return ctx.voice_client
            await ctx.voice_client.move_to(channel)
            return ctx.voice_client
        return await channel.connect()

    async def leave(self, ctx: discord.ApplicationContext | discord.ext.commands.Context) -> None:
        if ctx.voice_client:
            await ctx.voice_client.disconnect()

    async def listen_once(
        self,
        voice_client: discord.VoiceClient,
        on_transcription: TranscriptionCallback,
        timeout: float = 20.0,
    ) -> None:
        if voice_client.is_playing():
            voice_client.stop()

        def after_recording(sink: discord.sinks.Sink, *_) -> None:
            task = asyncio.create_task(self._handle_sink(sink, on_transcription))
            self._active_recordings[voice_client.channel.id] = task

        sink = discord.sinks.WaveSink()
        voice_client.start_recording(sink, after_recording)
        await asyncio.sleep(timeout)
        voice_client.stop_recording()
        task = self._active_recordings.pop(voice_client.channel.id, None)
        if task:
            await task

    async def _handle_sink(self, sink: discord.sinks.Sink, on_transcription: TranscriptionCallback) -> None:
        try:
            await self._process_sink(sink, on_transcription)
        finally:
            sink.cleanup()

    async def _process_sink(self, sink: discord.sinks.Sink, on_transcription: TranscriptionCallback) -> None:
        for user, audio in sink.audio_data.items():
            if audio is None or audio.file is None:
                continue
            audio.file.seek(0)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_file.write(audio.file.getvalue())
                temp_path = Path(temp_file.name)
            try:
                transcript = await self._stt.transcribe(temp_path)
                if transcript:
                    await on_transcription(user, transcript)
                else:
                    _LOGGER.debug("No transcript produced for user %s", user)
            finally:
                temp_path.unlink(missing_ok=True)

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
