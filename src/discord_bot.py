from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

import discord
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
        @self.command(name="reset")
        async def reset_conversation(ctx: commands.Context) -> None:
            await self.conversation_manager.reset(ctx.channel.id)
            await ctx.reply("Conversation history cleared for this channel.")

        @self.command(name="ask")
        async def ask(ctx: commands.Context, *, question: str) -> None:
            reply = await self.conversation_manager.generate_reply(ctx.channel.id, question)
            await ctx.reply(reply, mention_author=False)

        @self.command(name="join")
        async def join_voice(ctx: commands.Context) -> None:
            try:
                voice_client = await self.voice_session.join(ctx)
            except RuntimeError as exc:
                await ctx.reply(str(exc))
                return
            await ctx.reply(f"Joined voice channel {voice_client.channel.name}.")

        @self.command(name="leave")
        async def leave_voice(ctx: commands.Context) -> None:
            await self.voice_session.leave(ctx)
            await ctx.reply("Disconnected from voice channel.")

        @self.command(name="listen")
        async def listen_voice(ctx: commands.Context, timeout: Optional[int] = None) -> None:
            if not ctx.voice_client:
                await ctx.reply("I need to be in a voice channel. Use the join command first.")
                return

            timeout_value = float(timeout or 15)
            await ctx.reply(f"Listening for up to {timeout_value:.0f} seconds...")

            async def on_transcription(user: discord.abc.User, transcript: str) -> None:
                _LOGGER.info("Transcribed from %s: %s", user, transcript)
                reply = await self.conversation_manager.generate_reply(ctx.channel.id, transcript)
                await ctx.channel.send(f"**{user.display_name}:** {transcript}\n**Assistant:** {reply}")
                if ctx.voice_client:
                    await self.voice_session.speak(ctx.voice_client, reply)

            await self.voice_session.listen_once(ctx.voice_client, on_transcription, timeout=timeout_value)

        @self.command(name="say")
        async def say_voice(ctx: commands.Context, *, text: str) -> None:
            if not ctx.voice_client:
                await ctx.reply("I need to be in a voice channel to speak. Use the join command first.")
                return
            await self.voice_session.speak(ctx.voice_client, text)
            await ctx.reply("Playing synthesized speech.")

        @self.command(name="status")
        async def status_command(ctx: commands.Context) -> None:
            embed = discord.Embed(title="Assistant Status", color=discord.Color.blurple())
            embed.add_field(name="Model", value=self.config_data.ollama.model, inline=False)
            embed.add_field(name="Wake Word", value=self.config_data.discord.wake_word, inline=False)
            embed.add_field(name="History Turns", value=str(self.config_data.conversation.history_turns), inline=False)
            embed.add_field(name="Status Rotation", value=f"{self.config_data.discord.status_rotation_seconds}s", inline=False)
            await ctx.reply(embed=embed)

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
        content_lower = message.content.lower()
        if self.config_data.discord.wake_word not in content_lower:
            return
        now = time.monotonic()
        last = self._wake_cooldowns.get(message.channel.id, 0.0)
        if now - last < self.config_data.discord.wake_word_cooldown_seconds:
            return
        self._wake_cooldowns[message.channel.id] = now
        cleaned = message.content.lower().replace(self.config_data.discord.wake_word, "").strip()
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
