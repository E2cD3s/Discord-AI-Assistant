"""Discord bot implementation for the AI assistant."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .config import DiscordConfig


WakeWordHandler = Callable[[discord.Message], Awaitable[None]]


class DiscordBot(commands.Bot):
    """Discord bot wired up to the assistant core."""

    def __init__(
        self,
        config: DiscordConfig,
        *,
        assistant: Any | None = None,
        wake_word_handler: WakeWordHandler | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self.config = config
        self.assistant = assistant
        self._wake_word_handler = wake_word_handler
        self._commands_registered = False

    async def setup_hook(self) -> None:
        self._register_commands()

        if self.config.guild_ids:
            for guild_id in self.config.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        if self.config.activity_text:
            activity = discord.Game(name=self.config.activity_text)
            await self.change_presence(activity=activity)

    def _register_commands(self) -> None:
        if self._commands_registered:
            return

        @self.tree.command(name="join", description="Join the voice channel you're currently in.")
        async def join(interaction: discord.Interaction) -> None:
            await self._handle_join_command(interaction)

        @self.tree.command(name="leave", description="Disconnect from the current voice channel.")
        async def leave(interaction: discord.Interaction) -> None:
            await self._handle_leave_command(interaction)

        @app_commands.describe(question="What would you like to ask the assistant?")
        @self.tree.command(name="ask", description="Ask the AI assistant a question.")
        async def ask(interaction: discord.Interaction, question: str) -> None:
            await self._handle_ask_command(interaction, question)

        self._commands_registered = True

    async def on_ready(self) -> None:
        if self.user:
            print(f"Logged in as {self.user.name} ({self.user.id})")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if not message.content:
            return

        wake_words = self.config.wake_words
        if not wake_words:
            return

        message_content = message.content.lower()
        if any(message_content.startswith(word.lower()) for word in wake_words):
            await self._dispatch_wake_word(message)

    async def _dispatch_wake_word(self, message: discord.Message) -> None:
        if self._wake_word_handler is not None:
            await self._wake_word_handler(message)
            return

        if self.assistant is None:
            return

        try:
            response = await self._call_assistant_with_message(message)
        except Exception as exc:  # pragma: no cover - logged by caller
            await message.channel.send(f"Unable to process wake word message: {exc}")
            return

        if response:
            await message.channel.send(response)

    async def _handle_join_command(self, interaction: discord.Interaction) -> None:
        user_state = getattr(interaction.user, "voice", None)
        if not user_state or not user_state.channel:
            await interaction.response.send_message(
                "You need to join a voice channel first.", ephemeral=True
            )
            return

        voice_client = interaction.guild.voice_client if interaction.guild else None
        if voice_client and voice_client.channel == user_state.channel:
            await interaction.response.send_message(
                f"I'm already connected to {voice_client.channel.mention}.", ephemeral=True
            )
            return

        if voice_client and voice_client.is_connected():
            await voice_client.disconnect()

        await user_state.channel.connect()
        await interaction.response.send_message(
            f"Joined {user_state.channel.mention}.",
            ephemeral=True,
        )

    async def _handle_leave_command(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client if interaction.guild else None
        if not voice_client or not voice_client.is_connected():
            await interaction.response.send_message(
                "I'm not connected to a voice channel.", ephemeral=True
            )
            return

        channel = voice_client.channel
        await voice_client.disconnect()
        channel_name = channel.mention if channel else "the voice channel"
        await interaction.response.send_message(
            f"Disconnected from {channel_name}.", ephemeral=True
        )

    async def _handle_ask_command(self, interaction: discord.Interaction, question: str) -> None:
        if not self.assistant:
            await interaction.response.send_message(
                "No assistant is configured to answer questions right now.",
                ephemeral=True,
            )
            return

        try:
            answer = await self._call_assistant("ask", question, interaction=interaction)
        except Exception as exc:
            await interaction.response.send_message(
                f"Failed to get a response: {exc}", ephemeral=True
            )
            return

        if answer is None:
            await interaction.response.send_message(
                "The assistant did not provide a response.", ephemeral=True
            )
            return

        await interaction.response.send_message(str(answer))

    async def _call_assistant_with_message(self, message: discord.Message) -> Optional[str]:
        handler_names = (
            "handle_message",
            "on_message",
            "process_message",
            "__call__",
        )
        for name in handler_names:
            handler = getattr(self.assistant, name, None)
            if handler is None:
                continue
            result = handler(message)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return str(result)
        return None

    async def _call_assistant(
        self,
        intent: str,
        *args: Any,
        interaction: Optional[discord.Interaction] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        handler_candidates = (
            intent,
            f"handle_{intent}",
            "handle_interaction",
            "__call__",
        )

        for name in handler_candidates:
            handler = getattr(self.assistant, name, None)
            if handler is None:
                continue

            call_args = list(args)
            call_kwargs = dict(kwargs)
            if interaction is not None:
                if "interaction" in inspect.signature(handler).parameters:
                    call_kwargs.setdefault("interaction", interaction)
                elif interaction not in call_args:
                    call_args.append(interaction)

            result = handler(*call_args, **call_kwargs)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return str(result)
        return None

    async def close(self) -> None:
        await super().close()
        await asyncio.sleep(0)
