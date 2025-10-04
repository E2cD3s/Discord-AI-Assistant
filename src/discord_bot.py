from __future__ import annotations

import asyncio
import re
import time
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .ai.conversation_manager import ConversationManager
from .ai.voice_session import VoiceSession
from .config import AppConfig
from .logging_utils import get_logger

_LOGGER = get_logger(__name__)


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
        async def reset_conversation(interaction: discord.Interaction) -> None:
            await self.conversation_manager.reset(interaction.channel_id)
            await interaction.response.send_message("Conversation history cleared for this channel.")

        @self.tree.command(name="ask", description="Ask the assistant a question")
        @app_commands.describe(question="The question you want to ask the assistant")
        async def ask(interaction: discord.Interaction, question: str) -> None:
            await interaction.response.defer(thinking=True)
            reply = await self.conversation_manager.generate_reply(interaction.channel_id, question)
            await interaction.followup.send(reply)

        @self.tree.command(name="join", description="Summon the assistant to your current voice channel")
        @app_commands.guild_only()
        async def join_voice(interaction: discord.Interaction) -> None:
            try:
                voice_client = await self.voice_session.join(interaction)
            except RuntimeError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Joined voice channel {voice_client.channel.name}."
            )

        @self.tree.command(name="leave", description="Disconnect the assistant from the voice channel")
        async def leave_voice(interaction: discord.Interaction) -> None:
            await self.voice_session.leave(interaction)
            await interaction.response.send_message("Disconnected from voice channel.")

        @self.tree.command(name="listen", description="Listen to the voice channel and transcribe speech")
        @app_commands.describe(timeout="Seconds to listen before stopping (defaults to 15 seconds)")
        async def listen_voice(interaction: discord.Interaction, timeout: Optional[int] = None) -> None:
            voice_client = getattr(interaction.guild, "voice_client", None)
            if not voice_client:
                await interaction.response.send_message(
                    "I need to be in a voice channel. Use the /join command first.",
                    ephemeral=True,
                )
                return

            timeout_value = float(timeout or 15)
            await interaction.response.send_message(
                f"Listening for up to {timeout_value:.0f} seconds..."
            )

            async def on_transcription(user: discord.abc.User, transcript: str) -> None:
                _LOGGER.info("Transcribed from %s: %s", user, transcript)
                reply = await self.conversation_manager.generate_reply(interaction.channel_id, transcript)
                channel = interaction.channel
                if channel:
                    await channel.send(
                        f"**{user.display_name}:** {transcript}\n**Assistant:** {reply}"
                    )
                if voice_client:
                    await self.voice_session.speak(voice_client, reply)

            await self.voice_session.listen_once(voice_client, on_transcription, timeout=timeout_value)

        @self.tree.command(name="say", description="Have the assistant speak in the connected voice channel")
        @app_commands.describe(text="What you want the assistant to say")
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
            await interaction.response.send_message(embed=embed)

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
