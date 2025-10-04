from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks

from .ai.conversation_manager import ConversationManager
from .ai.voice_session import VoiceSession
from .config import AppConfig
from .logging_utils import get_logger
from .discord_compat import ensure_app_commands_ready

ensure_app_commands_ready(raise_on_failure=True)
from discord import app_commands

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
        if not hasattr(self, "tree"):
            self.tree = app_commands.CommandTree(self)
        self._status_index = 0
        self._voice_states: Dict[int, WakeConversationState] = {}
        self._wake_cooldowns: Dict[int, float] = {}
        wake_tokens = [token for token in re.split(r"\s+", config.discord.wake_word.strip()) if token]
        pattern = r"\W+".join(re.escape(token) for token in wake_tokens) if wake_tokens else re.escape(config.discord.wake_word)
        self._wake_word_regex = re.compile(rf"(?<!\w){pattern}(?:\W+|$)", re.IGNORECASE)
        self.status_rotator = tasks.loop(seconds=config.discord.status_rotation_seconds)(self.rotate_status)
        self._register_commands()

    async def setup_hook(self) -> None:
        if self.config_data.discord.guild_ids:
            for guild_id in self.config_data.discord.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def _register_commands(self) -> None:
        @self.tree.command(name="reset", description="Clear the assistant conversation history for this channel")
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        async def reset_conversation(interaction: discord.Interaction) -> None:
            try:
                await self._reset_channel(interaction.channel_id)
            except RuntimeError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message("Conversation history cleared for this channel.")

        @self.tree.command(name="ask", description="Ask the assistant a question")
        @app_commands.describe(question="The question you want to ask the assistant")
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        async def ask(interaction: discord.Interaction, question: str) -> None:
            await interaction.response.defer(thinking=True)
            try:
                reply = await self._ask_channel(interaction.channel_id, question)
            except RuntimeError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await interaction.followup.send(reply)

        @self.tree.command(name="join", description="Summon the assistant to your current voice channel")
        @app_commands.guild_only()
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        async def join_voice(interaction: discord.Interaction) -> None:
            try:
                voice_client = await self.voice_session.join(interaction)
            except RuntimeError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            await self._initialize_voice_state(voice_client, interaction.channel_id)

            async def on_transcription(user: discord.abc.User, transcript: str) -> None:
                await self._handle_transcription(voice_client, user, transcript)

            await self.voice_session.start_listening(
                voice_client,
                on_transcription,
                timeout=5.0,
            )
            await interaction.response.send_message(
                f"Joined voice channel {voice_client.channel.name}."
            )

        @self.tree.command(name="leave", description="Disconnect the assistant from the voice channel")
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        async def leave_voice(interaction: discord.Interaction) -> None:
            voice_client = getattr(interaction.guild, "voice_client", None)
            if voice_client and voice_client.channel:
                await self._cleanup_voice_state(voice_client.channel.id)
            await self.voice_session.leave(interaction)
            await interaction.response.send_message("Disconnected from voice channel.")

        @self.tree.command(name="say", description="Have the assistant speak in the connected voice channel")
        @app_commands.describe(text="What you want the assistant to say")
        @app_commands.allowed_installs(guilds=True, users=False)
        @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
        async def say_voice(interaction: discord.Interaction, text: str) -> None:
            voice_client = getattr(interaction.guild, "voice_client", None)
            if not voice_client:
                await interaction.response.send_message(
                    "I need to be in a voice channel to speak. Use the /join command first.",
                    ephemeral=True,
                )
                return
            await self.voice_session.speak(voice_client, text)
            await interaction.response.send_message("Playing synthesized speech.")

        @self.tree.command(name="status", description="Show configuration details for the assistant")
        async def status_command(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=self._build_status_embed())

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
                    reply = await self._ask_channel(ctx.channel.id if ctx.channel else None, question)
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

        @self.command(name="status", help="Show configuration details for the assistant")
        async def status_prefix(ctx: commands.Context) -> None:
            await ctx.send(embed=self._build_status_embed())

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

    async def close(self) -> None:
        self.status_rotator.cancel()
        await super().close()

    async def on_ready(self) -> None:
        _LOGGER.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        await self.rotate_status()
        if not self.status_rotator.is_running():
            self.status_rotator.start()

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
