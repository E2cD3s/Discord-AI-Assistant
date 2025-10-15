import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.config import (
    AppConfig,
    ConversationConfig,
    DiscordConfig,
    KokoroConfig,
    OllamaConfig,
    STTConfig,
)
from src.discord_bot import DiscordAssistantBot, WakeConversationState


def create_bot() -> DiscordAssistantBot:
    config = AppConfig(
        discord=DiscordConfig(
            token="dummy-token",
            command_prefix="!",
            owner_ids=[],
            guild_ids=[],
            status_rotation_seconds=60,
            statuses=["Ready"],
            wake_word="hey assistant",
            wake_word_cooldown_seconds=0,
            reply_in_thread=False,
            voice_idle_timeout_seconds=1,
            voice_alone_timeout_seconds=1,
        ),
        conversation=ConversationConfig(
            system_prompt="You are a bot.",
            history_turns=10,
            max_tokens=256,
            temperature=0.7,
            top_p=0.9,
            presence_penalty=0.0,
            frequency_penalty=0.0,
        ),
        ollama=OllamaConfig(
            host="http://localhost",
            model="test-model",
            request_timeout=30,
            stream=True,
            keep_alive=None,
        ),
        stt=STTConfig(
            model_path="model",
            device="cpu",
            compute_type="float32",
            beam_size=5,
            vad=True,
            energy_threshold=0.5,
            min_silence_duration_ms=500,
        ),
        kokoro=KokoroConfig(
            voice="voice",
            speed=1.0,
            emotion="neutral",
            output_dir="tts_output",
            format="wav",
            lang_code="en",
        ),
    )
    conversation_manager = MagicMock()
    voice_session = MagicMock()
    voice_session.stop_listening = AsyncMock()
    return DiscordAssistantBot(config, conversation_manager, voice_session)


def test_disconnects_when_left_alone() -> None:
    async def runner() -> None:
        bot = create_bot()

        channel_members = [SimpleNamespace(id=42)]
        channel = SimpleNamespace(id=999, name="Test Channel", members=channel_members)

        voice_client = MagicMock()
        voice_client.channel = channel
        voice_client.disconnect = AsyncMock()

        state = WakeConversationState(voice_client=voice_client, text_channel_id=None)
        bot._voice_states[channel.id] = state

        bot._update_voice_channel_population(channel)

        await asyncio.sleep(1.1)

        voice_client.disconnect.assert_awaited()

    asyncio.run(runner())


def test_disconnects_after_idle_timeout() -> None:
    async def runner() -> None:
        bot = create_bot()
        bot.config_data.discord.voice_alone_timeout_seconds = 0

        channel_members = [SimpleNamespace(id=42), SimpleNamespace(id=100)]
        channel = SimpleNamespace(id=500, name="Busy Channel", members=channel_members)

        voice_client = MagicMock()
        voice_client.channel = channel
        voice_client.disconnect = AsyncMock()

        state = WakeConversationState(voice_client=voice_client, text_channel_id=None)
        bot._voice_states[channel.id] = state

        bot._mark_voice_activity(channel.id)

        await asyncio.sleep(1.1)

        voice_client.disconnect.assert_awaited()

    asyncio.run(runner())
