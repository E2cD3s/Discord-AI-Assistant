from __future__ import annotations

import asyncio
import audioop
import inspect
import wave
from contextlib import closing, suppress
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

import discord
from discord.ext import commands

from .discord_voice_compat import ensure_voice_recording_support

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

ensure_voice_recording_support()

_LOGGER = get_logger(__name__)

TranscriptionCallback = Callable[[discord.abc.User, str], Awaitable[None]]


class VoiceSession:
    _NORMALISED_SAMPLE_RATE = 16000
    _NORMALISED_CHANNELS = 1

    def __init__(self, stt: SpeechToText, tts: TextToSpeech) -> None:
        self._stt = stt
        self._tts = tts
        self._active_recordings: Dict[int, asyncio.Task[None]] = {}
        self._listener_tasks: Dict[int, asyncio.Task[None]] = {}
        self._connection_locks: Dict[int, asyncio.Lock] = {}

    def _voice_key(self, voice_client: discord.VoiceClient) -> int:
        guild = getattr(voice_client, "guild", None)
        if guild is not None:
            return guild.id
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            raise RuntimeError("Voice client is not connected to any channel")
        return channel.id

    async def _ensure_voice_reception(self, voice_client: discord.VoiceClient) -> None:
        """Make sure the bot is not self-deafened or muted in the channel."""

        guild = getattr(voice_client, "guild", None)
        channel = getattr(voice_client, "channel", None)
        if guild is None or channel is None:
            return

        self._log_voice_channel_details(voice_client)

        change_state = getattr(guild, "change_voice_state", None)
        if callable(change_state):
            result: Any | None = None
            try:
                result = change_state(channel=channel, self_mute=False, self_deaf=False)
            except TypeError:
                try:
                    result = change_state(channel=channel)
                except Exception:  # pragma: no cover - network side effects
                    _LOGGER.exception(
                        "Failed to update voice state for channel %s", getattr(channel, "id", "unknown")
                    )
            except Exception:  # pragma: no cover - network side effects
                _LOGGER.exception(
                    "Failed to update voice state for channel %s", getattr(channel, "id", "unknown")
                )

            if inspect.isawaitable(result):
                try:
                    await result
                except Exception:  # pragma: no cover - network side effects
                    _LOGGER.exception(
                        "Voice state update coroutine failed for channel %s", getattr(channel, "id", "unknown")
                    )

        if getattr(voice_client, "self_deaf", False):
            try:
                voice_client.self_deaf = False  # type: ignore[attr-defined-outside-init]
            except Exception:
                pass

        if getattr(voice_client, "self_mute", False):
            try:
                voice_client.self_mute = False  # type: ignore[attr-defined-outside-init]
            except Exception:
                pass

        if isinstance(channel, discord.StageChannel):  # pragma: no branch - network effect
            bot_member = getattr(guild, "me", None)
            voice_state = getattr(bot_member, "voice", None) if bot_member else None
            if voice_state is not None and getattr(voice_state, "suppressed", False):
                request_to_speak = getattr(channel, "request_to_speak", None)
                if callable(request_to_speak):
                    try:
                        await request_to_speak()
                        _LOGGER.info("Requested to speak in stage channel %s", channel)
                    except Exception:  # pragma: no cover - depends on Discord state
                        _LOGGER.exception("Failed to request to speak in stage channel %s", channel)
                else:
                    _LOGGER.warning(
                        "Bot is suppressed in stage channel %s and cannot automatically request to speak",
                        channel,
                    )

        await self._wait_until_voice_ready(voice_client)
        self._configure_encoder_bitrate(voice_client)

    async def join(
        self,
        ctx: commands.Context | discord.Interaction,
    ) -> discord.VoiceClient:
        author = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        voice_state = getattr(author, "voice", None) if author else None
        channel = getattr(voice_state, "channel", None) if voice_state else None
        if channel is None:
            raise RuntimeError("User must be in a voice channel to summon the bot.")
        self._validate_voice_permissions(channel)
        guild = getattr(channel, "guild", None)
        guild_id = getattr(guild, "id", None) if guild else None
        lock_key = guild_id if guild_id is not None else id(channel)
        lock = self._connection_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[lock_key] = lock

        async with lock:
            voice_client = getattr(ctx, "voice_client", None)
            if voice_client is None:
                guild = getattr(ctx, "guild", None)
                voice_client = getattr(guild, "voice_client", None)
            if voice_client:
                if voice_client.channel.id == channel.id:
                    await self._ensure_voice_reception(voice_client)
                    return voice_client
                await voice_client.move_to(channel)
                await self._ensure_voice_reception(voice_client)
                return voice_client

            if not bool(getattr(discord.voice_client, "has_nacl", False)):
                raise RuntimeError(
                    "Voice connections require the PyNaCl dependency. "
                    "Install 'pynacl' and ensure the voice extra is enabled for discord.py."
                )

            async def _cleanup_failed_connection() -> None:
                guild = getattr(channel, "guild", None)
                if guild is None:
                    return

                state = getattr(guild, "_state", None)

                voice_client = getattr(guild, "voice_client", None)
                if voice_client is None and state is not None:
                    getter = getattr(state, "_get_voice_client", None)
                    if callable(getter):
                        with suppress(Exception):
                            voice_client = getter(getattr(guild, "id", None))

                if voice_client is not None:
                    with suppress(Exception):
                        await voice_client.disconnect(force=True)
                    with suppress(Exception):
                        voice_client.cleanup()

                if state is not None:
                    remover = getattr(state, "_remove_voice_client", None)
                    if callable(remover):
                        with suppress(Exception):
                            remover(getattr(guild, "id", None))

                bot_member = getattr(guild, "me", None)
                voice_states = getattr(guild, "_voice_states", None)
                if bot_member is not None and isinstance(voice_states, dict):
                    with suppress(Exception):
                        voice_states.pop(bot_member.id, None)

                change_voice_state = getattr(guild, "change_voice_state", None)
                if callable(change_voice_state):
                    with suppress(Exception):
                        await change_voice_state(channel=None)

                with suppress(Exception):
                    setattr(guild, "_voice_client", None)

            async def _connect() -> discord.VoiceClient:
                last_error: RuntimeError | None = None
                max_attempts = 4
                for attempt in range(1, max_attempts + 1):
                    reconnect = False
                    try:
                        try:
                            return await channel.connect(
                                reconnect=reconnect,
                                self_deaf=False,
                                self_mute=False,
                            )
                        except TypeError:
                            return await channel.connect(reconnect=reconnect)
                    except discord.errors.ConnectionClosed as exc:
                        close_code = getattr(exc, "code", None)
                        if close_code == 4006:
                            _LOGGER.warning(
                                "Voice websocket session invalidated with close code 4006. "
                                "Attempting to establish a fresh voice connection."
                            )
                            last_error = RuntimeError(
                                "Discord invalidated the voice websocket (close code 4006). "
                                "Try re-running the join command if the issue persists."
                            )
                            await _cleanup_failed_connection()
                            if attempt < max_attempts:
                                backoff = min(5.0, 2 ** (attempt - 1))
                                await asyncio.sleep(backoff)
                            continue
                        last_error = RuntimeError(
                            "Discord closed the voice connection unexpectedly "
                            f"(close code {close_code or 'unknown'}). "
                            "Try running the join command again or restart the bot."
                        )
                        raise last_error from exc
                    except discord.ClientException as exc:
                        message = str(exc)
                        if "Already connected" in message or "connect to voice" in message:
                            _LOGGER.warning(
                                "Voice client reported an invalid connection state (%s). "
                                "Attempting to reset the cached session before retrying.",
                                message,
                            )
                            last_error = RuntimeError(
                                "Discord reported a stale voice connection. "
                                "Retrying with a fresh session."
                            )
                            await _cleanup_failed_connection()
                            await asyncio.sleep(1)
                            continue
                        last_error = RuntimeError("Failed to connect to the voice channel")
                        raise last_error from exc
                    except Exception as exc:  # pragma: no cover - defensive guard
                        last_error = RuntimeError("Failed to connect to the voice channel")
                        raise last_error from exc

                if last_error is not None:
                    raise last_error

                raise RuntimeError("Failed to connect to the voice channel")

            voice_client = await _connect()
            await self._ensure_voice_reception(voice_client)
            return voice_client

    async def leave(
        self,
        ctx: commands.Context | discord.Interaction,
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
        await self._wait_for_playback_to_finish(
            voice_client, timeout=max(0.0, min(timeout, 15.0))
        )

        await self._ensure_voice_reception(voice_client)

        opus_module = getattr(discord, "opus", None)
        try:
            opus_loaded = bool(opus_module and opus_module.is_loaded())
        except Exception:  # pragma: no cover - best effort safety
            opus_loaded = True

        if not opus_loaded:
            raise RuntimeError(
                "Cannot start listening because the native Opus library is not loaded. "
                "Install 'pynacl' (or ensure the discord.py voice extra is installed) and restart the bot."
            )

        _LOGGER.info(
            "Starting voice capture in channel %s for up to %.1f seconds",
            voice_client.channel,
            timeout,
        )

        if not getattr(voice_client, "is_connected", lambda: False)():
            raise RuntimeError(
                "Cannot start listening because the voice client is not connected to a channel. "
                "Ensure the bot has successfully joined a voice channel before issuing listen commands."
            )

        await self._wait_until_voice_ready(voice_client)

        wave_sink = self._create_wave_sink()

        def after_recording(completed_sink: DiscordSink, *_) -> None:
            task = asyncio.create_task(self._handle_sink(completed_sink, on_transcription))
            self._active_recordings[self._voice_key(voice_client)] = task

        start_recording = getattr(voice_client, "start_recording", None)
        if not callable(start_recording):
            raise RuntimeError(
                "The active voice client does not expose recording support. "
                "Install or upgrade to 'discord.py[voice]>=2.3.2' (or an equivalent fork with sinks support)."
            )

        try:
            start_recording(wave_sink, after_recording)
        except Exception:
            _LOGGER.exception("Failed to start voice recording in channel %s", voice_client.channel)
            raise

        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            _LOGGER.info("Voice capture in %s cancelled", voice_client.channel)
            raise
        finally:
            stop_recording = getattr(voice_client, "stop_recording", None)
            if callable(stop_recording):
                stop_recording()
            else:
                _LOGGER.warning(
                    "Voice client for channel %s does not implement stop_recording(); "
                    "audio capture may continue until the client disconnects.",
                    voice_client.channel,
                )
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
                "Install 'discord.py[voice]>=2.3.2' to enable voice recording support."
            )
        wave_sink = getattr(discord_sinks, "WaveSink", None)
        if wave_sink is None:
            raise RuntimeError(
                "discord.sinks.WaveSink is unavailable. Update to a newer version of discord.py to continue."
            )
        return wave_sink()

    def _validate_voice_permissions(self, channel: Any) -> None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return

        bot_member = getattr(guild, "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if bot_member is None or not callable(permissions_for):
            return

        permissions = permissions_for(bot_member)
        missing: list[str] = []

        if not bool(getattr(permissions, "view_channel", True)):
            missing.append("View Channel")
        if not bool(getattr(permissions, "connect", True)):
            missing.append("Connect")

        if missing:
            raise RuntimeError(
                "The bot lacks the following permissions required to capture audio in %s: %s"
                % (channel, ", ".join(missing))
            )

        if not bool(getattr(permissions, "speak", True)):
            _LOGGER.warning(
                "Bot does not have permission to speak in %s. Discord may prevent other members from hearing it.",
                channel,
            )

        if not bool(getattr(permissions, "use_voice_activation", True)):
            _LOGGER.warning(
                (
                    "Bot is missing the 'Use Voice Activity' permission in %s. Incoming audio should still work, "
                    "but outgoing speech may be push-to-talk only."
                ),
                channel,
            )

    async def _wait_until_voice_ready(
        self, voice_client: discord.VoiceClient, *, timeout: float = 5.0
    ) -> None:
        if timeout <= 0:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - fallback for synchronous contexts
            loop = asyncio.get_event_loop()

        start = loop.time()

        while True:
            is_connected = bool(getattr(voice_client, "is_connected", lambda: False)())
            ws = getattr(voice_client, "ws", None)
            ws_connected = True
            udp_connected = True

            if ws is not None:
                ws_connected = bool(getattr(ws, "connected", True))
                udp = getattr(ws, "udp", None)
                if udp is not None:
                    udp_connected = bool(
                        getattr(udp, "connected", getattr(udp, "_connected", True))
                    )

            if is_connected and ws_connected and udp_connected:
                return

            if loop.time() - start >= timeout:
                channel = getattr(voice_client, "channel", "the voice channel")
                raise RuntimeError(
                    "Voice connection to %s did not become ready in time. Try moving the bot to a different "
                    "channel or reconnecting it."
                    % channel
                )

            await asyncio.sleep(0.1)

    async def _wait_for_playback_to_finish(
        self, voice_client: Any, *, timeout: float = 10.0
    ) -> None:
        is_playing = getattr(voice_client, "is_playing", None)
        stop = getattr(voice_client, "stop", None)

        if not callable(is_playing) or not is_playing():
            return

        channel = getattr(voice_client, "channel", "the voice channel")

        if timeout <= 0:
            if callable(stop):
                _LOGGER.debug(
                    "Not waiting for playback in %s because timeout is non-positive; stopping playback immediately.",
                    channel,
                )
                try:
                    stop()
                except Exception:  # pragma: no cover - defensive stop
                    _LOGGER.exception("Failed to stop playback in %s", channel)
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - fallback for synchronous contexts
            loop = asyncio.get_event_loop()

        _LOGGER.debug(
            "Waiting up to %.1fs for existing playback in %s to finish before recording.",
            timeout,
            channel,
        )

        start = loop.time()

        while True:
            try:
                still_playing = is_playing()
            except Exception:  # pragma: no cover - defensive guard
                still_playing = False

            if not still_playing:
                _LOGGER.debug("Playback completed in %s; proceeding with recording.", channel)
                return

            if loop.time() - start >= timeout:
                if callable(stop):
                    _LOGGER.warning(
                        "Timed out waiting for playback to finish in %s; stopping audio so listening can begin.",
                        channel,
                    )
                    try:
                        stop()
                    except Exception:  # pragma: no cover - defensive stop
                        _LOGGER.exception("Failed to stop playback in %s after timeout", channel)
                return

            await asyncio.sleep(0.1)

    async def _handle_sink(self, sink: DiscordSink, on_transcription: TranscriptionCallback) -> None:
        try:
            await self._process_sink(sink, on_transcription)
        finally:
            sink.cleanup()

    def _diagnose_channel_silence(self, voice_client: Any) -> None:
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            return

        members = list(getattr(channel, "members", []) or [])
        if not members:
            _LOGGER.info("No other participants are present in %s; there may be nobody to listen to.", channel)
            return

        guild = getattr(channel, "guild", None)
        bot_member = getattr(guild, "me", None) if guild else None

        participant_descriptions: list[str] = []
        non_bot_members: list[Any] = []
        muted_members: list[Any] = []

        for member in members:
            if bot_member is not None and getattr(member, "id", None) == getattr(bot_member, "id", None):
                continue

            voice_state = getattr(member, "voice", None)
            flags: list[str] = []

            if getattr(member, "bot", False):
                flags.append("bot")

            if voice_state is not None:
                if getattr(voice_state, "self_mute", False):
                    flags.append("self-muted")
                if getattr(voice_state, "mute", False):
                    flags.append("server-muted")
                if getattr(voice_state, "self_deaf", False):
                    flags.append("self-deafened")
                if getattr(voice_state, "deaf", False):
                    flags.append("server-deafened")
                if getattr(voice_state, "suppressed", False):
                    flags.append("suppressed")

            if not flags:
                flags.append("active")

            display_name = getattr(member, "display_name", None) or getattr(member, "name", member)
            participant_descriptions.append(f"{display_name} ({', '.join(flags)})")

            if not getattr(member, "bot", False):
                non_bot_members.append(member)
                if any(flag in ("self-muted", "server-muted", "self-deafened", "server-deafened", "suppressed") for flag in flags):
                    muted_members.append(member)

        if participant_descriptions:
            _LOGGER.info(
                "Voice participants in %s: %s",
                channel,
                "; ".join(participant_descriptions),
            )

        if non_bot_members and len(muted_members) == len(non_bot_members):
            _LOGGER.warning(
                "All non-bot members in %s are currently muted, deafened, or suppressed. The bot will not hear them until they are able to speak.",
                channel,
            )

    async def _process_sink(self, sink: DiscordSink, on_transcription: TranscriptionCallback) -> None:
        buffered_audio = []
        for user, audio in sink.audio_data.items():
            if audio is None or audio.file is None:
                continue

            start_time = getattr(audio, "start_time", 0.0)
            audio_file = audio.file
            audio_bytes: bytes | None = None

            if hasattr(audio_file, "getvalue"):
                try:
                    audio_bytes = audio_file.getvalue()
                except Exception:  # pragma: no cover - defensive guard
                    _LOGGER.exception(
                        "Failed to read buffered audio via getvalue() for user %s", user
                    )
                    audio_bytes = None

            if audio_bytes is None:
                try:
                    if hasattr(audio_file, "seek"):
                        audio_file.seek(0)
                    audio_bytes = audio_file.read()
                except Exception:  # pragma: no cover - defensive guard
                    _LOGGER.exception(
                        "Failed to read buffered audio via read() for user %s", user
                    )
                    audio_bytes = None

            if not audio_bytes:
                _LOGGER.debug("Ignoring empty audio buffer for user %s", user)
                continue

            buffered_audio.append((start_time, user, audio_bytes))

        buffered_audio.sort(key=lambda item: item[0])

        if not buffered_audio:
            voice_client = getattr(sink, "vc", None)
            state_details: list[str] = []
            if voice_client is not None:
                if getattr(voice_client, "self_deaf", False):
                    state_details.append("voice client is currently self-deafened")
                if getattr(voice_client, "self_mute", False):
                    state_details.append("voice client is currently self-muted")

                guild = getattr(voice_client, "guild", None)
                bot_member = getattr(guild, "me", None) if guild else None
                voice_state = getattr(bot_member, "voice", None) if bot_member else None
                if voice_state is not None:
                    if getattr(voice_state, "self_deaf", False):
                        state_details.append("bot member is server-deafened")
                    if getattr(voice_state, "self_mute", False):
                        state_details.append("bot member is server-muted")

            if state_details:
                _LOGGER.info(
                    "No audio detected during the last listening window (%s)",
                    "; ".join(state_details),
                )
            else:
                _LOGGER.info("No audio detected during the last listening window")
            self._diagnose_channel_silence(voice_client)
            return

        for _, user, audio_bytes in buffered_audio:
            stream = self._normalise_audio_stream(audio_bytes, source_user=user)
            _LOGGER.debug("Transcribing audio captured from user %s", user)
            transcript = await self._stt.transcribe(stream)
            if transcript:
                _LOGGER.info("Live transcription from %s: %s", user, transcript)
                await on_transcription(user, transcript)
            else:
                _LOGGER.debug("No transcript produced for user %s", user)

    async def speak(self, voice_client: discord.VoiceClient, text: str) -> Optional[Path]:
        audio_path = await self._tts.synthesize(text)
        if voice_client.is_playing():
            voice_client.stop()

        audio_source = discord.FFmpegPCMAudio(str(audio_path))

        def after_playback(error: Optional[Exception]) -> None:
            if error:
                _LOGGER.error("FFmpeg playback error: %s", error)

            try:
                audio_source.cleanup()
            except Exception:  # pragma: no cover - cleanup best effort
                _LOGGER.exception("Failed to cleanup audio source for %s", audio_path)

            try:
                audio_path.unlink(missing_ok=True)
            except PermissionError:
                _LOGGER.warning(
                    "Unable to remove synthesized audio file %s because it is still in use", audio_path
                )
            except Exception:  # pragma: no cover - cleanup best effort
                _LOGGER.exception("Failed to remove synthesized audio file %s", audio_path)

        voice_client.play(audio_source, after=after_playback)
        return audio_path

    def stop_speaking(self, voice_client: discord.VoiceClient) -> bool:
        """Stop any active voice playback.

        Returns ``True`` if playback was stopped, or ``False`` if the bot was
        not currently speaking.
        """

        if voice_client.is_playing():
            voice_client.stop()
            return True

        return False

    def _log_voice_channel_details(self, voice_client: discord.VoiceClient) -> None:
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            return

        bitrate = getattr(channel, "bitrate", None)
        user_limit = getattr(channel, "user_limit", None)
        rtc_region = getattr(channel, "rtc_region", None)

        _LOGGER.debug(
            "Voice channel diagnostics for %s: bitrate=%s, user_limit=%s, region=%s",
            getattr(channel, "id", channel),
            bitrate if bitrate is not None else "unknown",
            user_limit if user_limit not in (None, 0) else "unlimited",
            rtc_region or "automatic",
        )

        if isinstance(bitrate, int) and bitrate > 0 and bitrate < 32000:
            _LOGGER.warning(
                "Voice channel %s is configured with a very low bitrate (%dkbps). "
                "Speech recognition accuracy may suffer; consider increasing it via the Discord client.",
                channel,
                bitrate // 1000,
            )

    def _configure_encoder_bitrate(self, voice_client: discord.VoiceClient) -> None:
        channel = getattr(voice_client, "channel", None)
        encoder = getattr(voice_client, "encoder", None)
        bitrate = getattr(channel, "bitrate", None) if channel is not None else None

        if encoder is None or not isinstance(bitrate, int) or bitrate <= 0:
            return

        minimum_bitrate = 16000
        maximum_bitrate = 320000
        target_bitrate = max(minimum_bitrate, min(bitrate, maximum_bitrate))

        try:
            encoder.set_bitrate(target_bitrate)
        except Exception:  # pragma: no cover - depends on voice backend
            _LOGGER.exception("Failed to configure encoder bitrate for channel %s", channel)
        else:
            _LOGGER.debug(
                "Configured Opus encoder bitrate to %d bps for channel %s",
                target_bitrate,
                channel,
            )

    def _normalise_audio_stream(self, audio_bytes: bytes, *, source_user: Any | None = None) -> BytesIO:
        stream = BytesIO(audio_bytes)

        try:
            with closing(wave.open(stream, "rb")) as wav_in:
                sample_rate = wav_in.getframerate()
                sample_width = wav_in.getsampwidth()
                channels = wav_in.getnchannels()
                frames = wav_in.readframes(wav_in.getnframes())
        except (wave.Error, EOFError) as exc:
            if source_user is not None:
                _LOGGER.warning(
                    "Received audio payload for %s is not a valid WAV stream; using raw bytes. Error: %s",
                    source_user,
                    exc,
                )
            else:
                _LOGGER.warning("Received audio payload is not a valid WAV stream; using raw bytes. Error: %s", exc)
            stream.seek(0)
            return stream

        # Reset stream so it can be reused if normalisation is unnecessary.
        stream.seek(0)

        original_bitrate = sample_rate * sample_width * 8 * channels
        target_rate = self._NORMALISED_SAMPLE_RATE
        target_channels = self._NORMALISED_CHANNELS

        needs_channel_downmix = channels != target_channels
        needs_resample = sample_rate != target_rate
        needs_width_adjustment = sample_width not in (1, 2)

        if not (needs_channel_downmix or needs_resample or needs_width_adjustment):
            _LOGGER.debug(
                "Audio stream already matches expected format (%d Hz, %d channel(s)).",
                sample_rate,
                channels,
            )
            stream.seek(0)
            return stream

        try:
            processed_frames = frames
            processed_width = sample_width

            if needs_channel_downmix:
                processed_frames = audioop.tomono(processed_frames, sample_width, 1, 1)
                processed_width = sample_width

            if needs_width_adjustment and processed_width != 2:
                processed_frames = audioop.lin2lin(processed_frames, processed_width, 2)
                processed_width = 2

            if needs_resample:
                processed_frames, _ = audioop.ratecv(
                    processed_frames,
                    processed_width,
                    target_channels,
                    sample_rate,
                    target_rate,
                    None,
                )

        except (audioop.error, ValueError) as exc:
            if source_user is not None:
                _LOGGER.warning(
                    "Failed to normalise audio for %s (rate %d Hz, channels %d): %s",
                    source_user,
                    sample_rate,
                    channels,
                    exc,
                )
            else:
                _LOGGER.warning(
                    "Failed to normalise audio stream (rate %d Hz, channels %d): %s",
                    sample_rate,
                    channels,
                    exc,
                )
            stream.seek(0)
            return stream

        output = BytesIO()
        with closing(wave.open(output, "wb")) as wav_out:
            wav_out.setnchannels(target_channels)
            wav_out.setsampwidth(processed_width)
            wav_out.setframerate(target_rate)
            wav_out.writeframes(processed_frames)

        output.seek(0)

        _LOGGER.debug(
            "Normalised audio stream from %d Hz/%d ch (%d bps) to %d Hz/%d ch (%d bps)%s",
            sample_rate,
            channels,
            original_bitrate,
            target_rate,
            target_channels,
            target_rate * processed_width * 8 * target_channels,
            f" for user {getattr(source_user, 'id', source_user)}" if source_user is not None else "",
        )

        return output


__all__ = ["VoiceSession", "TranscriptionCallback"]
