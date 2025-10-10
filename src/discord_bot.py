from __future__ import annotations

import asyncio
import inspect
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .ai.conversation_manager import ConversationManager
from .ai.voice_session import VoiceSession
from .config import AppConfig
from .logging_utils import get_logger

_InteractionResponded = getattr(discord, "InteractionResponded", RuntimeError)
_NotFound = getattr(getattr(discord, "errors", discord), "NotFound", RuntimeError)

_LOGGER = get_logger(__name__)


@dataclass
class WakeConversationState:
    voice_client: discord.VoiceClient
    text_channel_id: Optional[int]
    active: bool = False
    initiator_id: Optional[int] = None
    initiator_name: Optional[str] = None
    transcripts: List[str] = field(default_factory=list)
    start_time: float = 0.0
    inactivity_task: Optional[asyncio.Task[None]] = None
    max_duration_task: Optional[asyncio.Task[None]] = None


class DiscordAssistantBot(commands.Bot):
    def __init__(
        self,
        config: AppConfig,
        conversation_manager: ConversationManager,
        voice_session: VoiceSession,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix=config.discord.command_prefix, intents=intents)
        self.config_data = config
        self.conversation_manager = conversation_manager
        self.voice_session = voice_session
        self._status_index = 0
        self._commands_synced = False
        self._voice_states: Dict[int, WakeConversationState] = {}
        self._wake_cooldowns: Dict[int, float] = {}
        wake_tokens = [token for token in re.split(r"\s+", config.discord.wake_word.strip()) if token]
        pattern = r"\W+".join(re.escape(token) for token in wake_tokens) if wake_tokens else re.escape(config.discord.wake_word)
        self._wake_word_regex = re.compile(rf"(?<!\w){pattern}(?:\W+|$)", re.IGNORECASE)
        self._stop_voice_regex = re.compile(
            r"\b(?:stop(?:\s+(?:talking|speaking|playing|playback|audio))?|shut\s+up|be\s+quiet|quiet|silence)\b",
            re.IGNORECASE,
        )
        self.status_rotator = tasks.loop(seconds=config.discord.status_rotation_seconds)(self.rotate_status)
        self._register_commands()

    async def setup_hook(self) -> None:
        await self._sync_application_commands()

    async def _sync_application_commands(self) -> None:
        if self._commands_synced:
            return

        try:
            if self.config_data.discord.guild_ids:
                for guild_id in self.config_data.discord.guild_ids:
                    await self.tree.sync(guild=discord.Object(id=guild_id))
            else:
                await self.tree.sync()
        except AttributeError:  # pragma: no cover - defensive guard for unsupported clients
            _LOGGER.debug("Command tree synchronization is unavailable on this Discord client")
            return

        self._commands_synced = True

    def _register_commands(self) -> None:
        slash_registration = self._register_slash_commands
        prefix_registration = self._register_prefix_commands
        slash_registration()
        prefix_registration()

    def _register_slash_commands(self) -> None:
        guild_ids = self.config_data.discord.guild_ids or []
        guild_objects = [discord.Object(id=guild_id) for guild_id in guild_ids]

        def _register_command(command: app_commands.Command) -> None:
            if guild_objects:
                for guild in guild_objects:
                    self.tree.add_command(command.copy(), guild=guild)
            else:
                self.tree.add_command(command)

        async def reset_handler(interaction: discord.Interaction) -> None:
            try:
                await self._reset_channel(interaction.channel_id)
            except RuntimeError as exc:
                await self._send_interaction_message(
                    interaction, str(exc), ephemeral=True
                )
                return
            await self._send_interaction_message(
                interaction, "Conversation history cleared for this channel."
            )

        async def ask_handler(interaction: discord.Interaction, question: str) -> str | None:
            await self._defer_interaction(interaction)
            try:
                return await self._ask_channel(interaction.channel_id, question)
            except RuntimeError as exc:
                await self._send_interaction_message(
                    interaction, str(exc), ephemeral=True, prefer_followup=True
                )
                return None

        async def status_handler(interaction: discord.Interaction) -> None:
            await self._send_interaction_message(
                interaction, embed=self._build_status_embed()
            )

        reset_command = app_commands.Command(
            name="reset",
            description="Clear the assistant conversation history for this channel",
            callback=reset_handler,
        )
        _register_command(reset_command)

        ask_group = app_commands.Group(
            name="ask",
            description="Ask the assistant a question",
        )

        @ask_group.command(
            name="text",
            description="Ask the assistant a question and receive a text reply",
        )
        @app_commands.describe(
            question="The question you want to ask the assistant",
        )
        async def ask_text_command(
            interaction: discord.Interaction, question: str
        ) -> None:
            reply = await ask_handler(interaction, question)
            if reply is None:
                return
            await self._send_interaction_message(
                interaction, reply, prefer_followup=True
            )

        @ask_group.command(
            name="voice",
            description="Ask the assistant a question and hear the reply in voice",
        )
        @app_commands.describe(
            question="The question you want to ask the assistant",
        )
        async def ask_voice_command(
            interaction: discord.Interaction, question: str
        ) -> None:
            reply = await ask_handler(interaction, question)
            if reply is None:
                return
            try:
                voice_client, _ = await self._ensure_voice_connection(
                    interaction,
                    text_channel_id=interaction.channel_id,
                    start_listening=True,
                )
            except RuntimeError as exc:
                await self._send_interaction_message(
                    interaction, str(exc), ephemeral=True, prefer_followup=True
                )
                return

            try:
                await self.voice_session.speak(voice_client, reply)
            except Exception:
                _LOGGER.exception("Failed to play synthesized speech for voice ask")
                await self._send_interaction_message(
                    interaction,
                    "Unable to play synthesized speech in the voice channel.",
                    ephemeral=True,
                    prefer_followup=True,
                )
                return

            await self._send_interaction_message(
                interaction,
                reply,
                prefer_followup=True,
            )

        _register_command(ask_group)

        @app_commands.command(
            name="join",
            description="Summon the assistant to your current voice channel",
        )
        async def join_command(interaction: discord.Interaction) -> None:
            await self._defer_interaction(interaction)
            try:
                voice_client, _ = await self._ensure_voice_connection(
                    interaction,
                    text_channel_id=interaction.channel_id,
                    start_listening=True,
                )
            except RuntimeError as exc:
                await self._send_interaction_message(
                    interaction, str(exc), ephemeral=True
                )
                return
            await self._send_interaction_message(
                interaction, f"Joined voice channel {voice_client.channel.name}."
            )

        _register_command(join_command)

        @app_commands.command(
            name="leave",
            description="Disconnect the assistant from the voice channel",
        )
        async def leave_command(interaction: discord.Interaction) -> None:
            voice_client = getattr(interaction.guild, "voice_client", None)
            if voice_client and voice_client.channel:
                await self._cleanup_voice_state(voice_client.channel.id)
            await self.voice_session.leave(interaction)
            await self._send_interaction_message(
                interaction, "Disconnected from voice channel."
            )

        _register_command(leave_command)

        @app_commands.command(
            name="say",
            description="Have the assistant speak in the connected voice channel",
        )
        @app_commands.describe(
            text="What you want the assistant to say",
        )
        async def say_command(interaction: discord.Interaction, text: str) -> None:
            voice_client = (
                getattr(interaction.guild, "voice_client", None)
                if interaction.guild
                else None
            )
            if not voice_client or not getattr(voice_client, "channel", None):
                try:
                    voice_client, _ = await self._ensure_voice_connection(
                        interaction,
                        text_channel_id=interaction.channel_id,
                        start_listening=True,
                    )
                except RuntimeError as exc:
                    await self._send_interaction_message(
                        interaction,
                        str(exc),
                        ephemeral=True,
                    )
                    return
            await self.voice_session.speak(voice_client, text)
            await self._send_interaction_message(
                interaction, "Playing synthesized speech."
            )

        _register_command(say_command)

        @app_commands.command(
            name="stop",
            description="Stop the assistant's current voice playback",
        )
        async def stop_command(interaction: discord.Interaction) -> None:
            voice_client = (
                getattr(interaction.guild, "voice_client", None)
                if interaction.guild
                else None
            )
            if not voice_client or not getattr(voice_client, "channel", None):
                await self._send_interaction_message(
                    interaction,
                    "I'm not connected to a voice channel.",
                    ephemeral=True,
                )
                return

            if self.voice_session.stop_speaking(voice_client):
                await self._send_interaction_message(
                    interaction, "Stopped the current voice playback."
                )
                return

            await self._send_interaction_message(
                interaction,
                "There is no active voice playback to stop.",
                ephemeral=True,
            )

        _register_command(stop_command)

        @app_commands.command(
            name="status",
            description="Show configuration details for the assistant",
        )
        async def status_command(interaction: discord.Interaction) -> None:
            await status_handler(interaction)

        _register_command(status_command)

    def _register_prefix_commands(self) -> None:

        @self.command(name="reset", help="Clear the assistant conversation history for this channel")
        async def reset_command(ctx: commands.Context) -> None:
            try:
                await self._reset_channel(ctx.channel.id if ctx.channel else None)
            except RuntimeError as exc:
                await ctx.send(str(exc))
                return
            await ctx.send("Conversation history cleared for this channel.")

        @self.command(name="ask", help="Ask the assistant a question")
        async def ask_command(ctx: commands.Context, *, question: str) -> None:
            async with ctx.typing():
                try:
                    reply = await self._ask_channel(
                        ctx.channel.id if ctx.channel else None, question
                    )
                except RuntimeError as exc:
                    await ctx.send(str(exc))
                    return
            await ctx.send(reply)

        @self.command(name="join", help="Summon the assistant to your current voice channel")
        async def join_command(ctx: commands.Context) -> None:
            try:
                voice_client = await self.voice_session.join(ctx)
            except RuntimeError as exc:
                await ctx.send(str(exc))
                return

            await self._initialize_voice_state(voice_client, ctx.channel.id if ctx.channel else None)

            async def on_transcription(user: discord.abc.User, transcript: str) -> None:
                await self._handle_transcription(voice_client, user, transcript)

            await self.voice_session.start_listening(
                voice_client,
                on_transcription,
                timeout=5.0,
            )
            await ctx.send(f"Joined voice channel {voice_client.channel.name}.")

        @self.command(name="leave", help="Disconnect the assistant from the voice channel")
        async def leave_command(ctx: commands.Context) -> None:
            voice_client = getattr(ctx.guild, "voice_client", None) if ctx.guild else None
            if voice_client and voice_client.channel:
                await self._cleanup_voice_state(voice_client.channel.id)
            await self.voice_session.leave(ctx)
            await ctx.send("Disconnected from voice channel.")

        @self.command(name="say", help="Have the assistant speak in the connected voice channel")
        async def say_command(ctx: commands.Context, *, text: str) -> None:
            voice_client = getattr(ctx.guild, "voice_client", None) if ctx.guild else None
            if not voice_client:
                await ctx.send("I need to be in a voice channel to speak. Use the !join command first.")
                return
            await self.voice_session.speak(voice_client, text)
            await ctx.send("Playing synthesized speech.")

        @self.command(name="stop", help="Stop the assistant's current voice playback")
        async def stop_command(ctx: commands.Context) -> None:
            voice_client = getattr(ctx.guild, "voice_client", None) if ctx.guild else None
            if not voice_client:
                await ctx.send("I'm not connected to a voice channel.")
                return

            if self.voice_session.stop_speaking(voice_client):
                await ctx.send("Stopped the current voice playback.")
            else:
                await ctx.send("There is no active voice playback to stop.")

        @self.command(name="status", help="Show configuration details for the assistant")
        async def status_prefix(ctx: commands.Context) -> None:
            await ctx.send(embed=self._build_status_embed())

    async def _ensure_voice_connection(
        self,
        ctx: commands.Context | discord.Interaction,
        *,
        text_channel_id: Optional[int],
        start_listening: bool = False,
    ) -> tuple[discord.VoiceClient, bool]:
        guild = getattr(ctx, "guild", None)
        voice_client = (
            getattr(guild, "voice_client", None)
            if guild is not None
            else getattr(ctx, "voice_client", None)
        )
        connected_channel = getattr(voice_client, "channel", None) if voice_client else None
        joined = False

        if voice_client is None or connected_channel is None:
            voice_client = await self.voice_session.join(ctx)
            connected_channel = getattr(voice_client, "channel", None)
            joined = True

        if connected_channel is None:
            raise RuntimeError("Unable to determine the connected voice channel.")

        if joined:
            await self._initialize_voice_state(voice_client, text_channel_id)

        if start_listening:

            async def on_transcription(user: discord.abc.User, transcript: str) -> None:
                await self._handle_transcription(voice_client, user, transcript)

            await self.voice_session.start_listening(
                voice_client,
                on_transcription,
                timeout=5.0,
            )

        return voice_client, joined

    async def _defer_interaction(self, interaction: discord.Interaction) -> None:
        response = getattr(interaction, "response", None)
        if response is None:
            return

        defer = getattr(response, "defer", None)
        if not callable(defer):  # pragma: no cover - defensive guard
            return

        try:
            await defer(thinking=True)
        except TypeError:
            await defer()

    async def _send_interaction_message(
        self,
        interaction: discord.Interaction,
        content: Optional[str] = None,
        *,
        ephemeral: bool = False,
        prefer_followup: bool = False,
        embed: Optional[discord.Embed] = None,
    ) -> None:
        kwargs: Dict[str, Any] = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if not kwargs:
            return

        response = getattr(interaction, "response", None)
        followup = getattr(interaction, "followup", None)

        async def _send_via_followup() -> bool:
            if followup is None or not hasattr(followup, "send"):
                return False
            try:
                await followup.send(**kwargs, ephemeral=ephemeral)
                return True
            except _NotFound:
                return False

        if prefer_followup:
            if await _send_via_followup():
                return

        if response is not None:
            is_done = getattr(response, "is_done", None)
            if callable(is_done) and is_done():
                if await _send_via_followup():
                    return
            send_message = getattr(response, "send_message", None)
            if callable(send_message):
                try:
                    await send_message(ephemeral=ephemeral, **kwargs)
                    return
                except (_InteractionResponded, _NotFound):
                    if await _send_via_followup():
                        return

        if await _send_via_followup():
            return

        channel = getattr(interaction, "channel", None)
        if channel is not None and hasattr(channel, "send"):
            await channel.send(**kwargs)

    async def _reset_channel(self, channel_id: Optional[int]) -> None:
        if channel_id is None:
            raise RuntimeError("Unable to determine which channel to reset.")
        await self.conversation_manager.reset(channel_id)

    async def _ask_channel(self, channel_id: Optional[int], question: str) -> str:
        if channel_id is None:
            raise RuntimeError("Unable to determine which channel to answer in.")
        return await self.conversation_manager.generate_reply(channel_id, question)

    def _build_status_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Assistant Status", color=discord.Color.blurple())
        embed.add_field(name="Model", value=self.config_data.ollama.model, inline=False)
        embed.add_field(name="Wake Word", value=self.config_data.discord.wake_word, inline=False)
        embed.add_field(
            name="History Turns",
            value=str(self.config_data.conversation.history_turns),
            inline=False,
        )
        embed.add_field(
            name="Status Rotation",
            value=f"{self.config_data.discord.status_rotation_seconds}s",
            inline=False,
        )
        return embed

    async def _initialize_voice_state(
        self, voice_client: discord.VoiceClient, text_channel_id: Optional[int]
    ) -> None:
        channel_id = voice_client.channel.id
        state = self._voice_states.get(channel_id)
        if state:
            state.voice_client = voice_client
            state.text_channel_id = text_channel_id
            for task_attr in ("inactivity_task", "max_duration_task"):
                task = getattr(state, task_attr)
                if task and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    setattr(state, task_attr, None)
            state.transcripts.clear()
            state.active = False
            state.initiator_id = None
            state.initiator_name = None
        else:
            self._voice_states[channel_id] = WakeConversationState(
                voice_client=voice_client,
                text_channel_id=text_channel_id,
            )

    async def _cleanup_voice_state(self, channel_id: int) -> None:
        state = self._voice_states.pop(channel_id, None)
        if not state:
            return
        for task in (state.inactivity_task, state.max_duration_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _set_inactivity_timer(self, channel_id: int, delay: float = 2.0) -> None:
        state = self._voice_states.get(channel_id)
        if not state:
            return
        if state.inactivity_task and not state.inactivity_task.done():
            state.inactivity_task.cancel()
        state.inactivity_task = asyncio.create_task(
            self._end_conversation_after(channel_id, delay, "silence")
        )

    def _set_max_duration_timer(self, channel_id: int, duration: float = 30.0) -> None:
        state = self._voice_states.get(channel_id)
        if not state:
            return
        if state.max_duration_task and not state.max_duration_task.done():
            return
        state.max_duration_task = asyncio.create_task(
            self._end_conversation_after(channel_id, duration, "maximum duration")
        )

    async def _end_conversation_after(
        self, channel_id: int, delay: float, reason: str
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self._finalize_conversation(channel_id, reason)
        except asyncio.CancelledError:
            raise

    async def _handle_transcription(
        self, voice_client: discord.VoiceClient, user: discord.abc.User, transcript: str
    ) -> None:
        channel = getattr(voice_client, "channel", None)
        if channel is None:
            return
        state = self._voice_states.get(channel.id)

        if voice_client.is_playing() and self._is_voice_stop_request(transcript):
            if state:
                state.voice_client = voice_client
            stopped = False
            try:
                stopped = self.voice_session.stop_speaking(voice_client)
            except Exception:
                _LOGGER.exception(
                    "Failed to stop voice playback in channel %s via voice command",
                    getattr(channel, "id", "unknown"),
                )
            message = (
                "Stopped the current voice playback."
                if stopped
                else "There is no active voice playback to stop."
            )
            await self._send_voice_feedback(state, message)
            return

        if state is None:
            return

        state.voice_client = voice_client
        _LOGGER.info("Transcribed from %s: %s", user, transcript)

        match = self._wake_word_regex.search(transcript)
        now = time.monotonic()

        if not state.active:
            if not match:
                return
            state.active = True
            state.start_time = now
            state.initiator_id = getattr(user, "id", None)
            state.initiator_name = getattr(user, "display_name", getattr(user, "name", None))
            state.transcripts.clear()
            post_wake = transcript[match.end():].strip()
            if post_wake:
                state.transcripts.append(post_wake)
            self._set_inactivity_timer(channel.id)
            self._set_max_duration_timer(channel.id)
            return

        if match:
            content = transcript[match.end():].strip() or transcript
        else:
            content = transcript

        if content:
            state.transcripts.append(content)
        self._set_inactivity_timer(channel.id)

        if now - state.start_time >= 30.0:
            await self._finalize_conversation(channel.id, "maximum duration")

    def _is_voice_stop_request(self, transcript: str) -> bool:
        if not transcript:
            return False
        return bool(self._stop_voice_regex.search(transcript))

    async def _send_voice_feedback(
        self, state: WakeConversationState | None, message: str
    ) -> None:
        if not message:
            return
        if not state or not state.text_channel_id:
            return

        channel_id = state.text_channel_id
        channel = self.get_channel(channel_id)
        if channel is None:
            return

        send = getattr(channel, "send", None)
        if not callable(send):
            return

        try:
            result = send(message)
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - defensive logging
            _LOGGER.exception(
                "Failed to send voice control feedback to channel %s", channel_id
            )

    async def _finalize_conversation(self, channel_id: int, reason: str) -> None:
        state = self._voice_states.get(channel_id)
        if not state or not state.active:
            return

        state.active = False
        current_task = asyncio.current_task()

        inactivity_task = state.inactivity_task
        state.inactivity_task = None
        if (
            inactivity_task
            and inactivity_task is not current_task
            and not inactivity_task.done()
        ):
            inactivity_task.cancel()
            with suppress(asyncio.CancelledError):
                await inactivity_task

        max_duration_task = state.max_duration_task
        state.max_duration_task = None
        if max_duration_task and max_duration_task is not current_task and not max_duration_task.done():
            max_duration_task.cancel()
            with suppress(asyncio.CancelledError):
                await max_duration_task

        transcript_text = " ".join(state.transcripts).strip()
        state.transcripts.clear()
        if not transcript_text:
            _LOGGER.info(
                "Wake conversation in channel %s ended (%s) without speech to forward",
                channel_id,
                reason,
            )
            return

        text_channel_id = state.text_channel_id or channel_id
        reply = await self.conversation_manager.generate_reply(text_channel_id, transcript_text)

        text_channel = self.get_channel(text_channel_id)
        if isinstance(text_channel, (discord.TextChannel, discord.Thread)):
            speaker = state.initiator_name or "User"
            await text_channel.send(
                f"**{speaker}:** {transcript_text}\n**Assistant:** {reply}"
            )
        else:
            _LOGGER.warning(
                "No text channel available to post transcription response for channel %s",
                channel_id,
            )

        try:
            await self.voice_session.speak(state.voice_client, reply)
        except Exception:  # pragma: no cover - defensive logging
            _LOGGER.exception("Failed to play synthesized speech in channel %s", channel_id)

        state.initiator_id = None
        state.initiator_name = None

    async def on_ready(self) -> None:
        _LOGGER.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        await self.rotate_status()
        if not self.status_rotator.is_running():
            self.status_rotator.start()
        await self._sync_application_commands()

    async def close(self) -> None:
        self.status_rotator.cancel()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.content:
            return
        await self.process_commands(message)
        if message.content.startswith(self.command_prefix):
            return
        if not self._wake_word_regex.search(message.content):
            return
        now = time.monotonic()
        last = self._wake_cooldowns.get(message.channel.id, 0.0)
        if now - last < self.config_data.discord.wake_word_cooldown_seconds:
            return
        self._wake_cooldowns[message.channel.id] = now
        cleaned = self._wake_word_regex.sub("", message.content, count=1).strip()
        prompt = cleaned or message.content
        reply = await self.conversation_manager.generate_reply(message.channel.id, prompt)
        await self._send_reply(message, reply)
        if message.guild and message.guild.voice_client:
            try:
                await self.voice_session.speak(message.guild.voice_client, reply)
            except Exception:  # pragma: no cover - best effort
                _LOGGER.exception("Failed to play synthesized speech")

    async def rotate_status(self) -> None:
        status_text = self.config_data.discord.statuses[self._status_index % len(self.config_data.discord.statuses)]
        self._status_index += 1
        await self.change_presence(activity=discord.Game(name=status_text))

    async def _send_reply(self, message: discord.Message, reply: str) -> None:
        if not reply:
            _LOGGER.warning("Empty reply generated for message %s", message.id)
            return
        try:
            if self.config_data.discord.reply_in_thread and isinstance(message.channel, discord.TextChannel):
                thread = message.thread
                if thread is None:
                    thread_name = f"Chat with {message.author.display_name}"[:100]
                    thread = await message.create_thread(name=thread_name)
                await thread.send(reply)
            else:
                await message.reply(reply, mention_author=False)
        except discord.Forbidden:
            _LOGGER.warning("Missing permissions to send message in channel %s", message.channel.id)
        except discord.HTTPException:
            _LOGGER.exception("Failed to send reply to message %s", message.id)


def create_bot(config: AppConfig, conversation_manager: ConversationManager, voice_session: VoiceSession) -> DiscordAssistantBot:
    return DiscordAssistantBot(config, conversation_manager, voice_session)


__all__ = ["DiscordAssistantBot", "create_bot"]
