import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import (
    AppConfig,
    ConversationConfig,
    DiscordConfig,
    KokoroConfig,
    OllamaConfig,
    STTConfig,
)
from src.discord_bot import DiscordAssistantBot, WakeConversationState


@pytest.fixture()
def bot() -> DiscordAssistantBot:
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
    return DiscordAssistantBot(config, conversation_manager, voice_session)


def test_voice_stop_command_halts_playback(bot: DiscordAssistantBot) -> None:
    voice_client = MagicMock()
    voice_client.is_playing.return_value = True
    voice_client.channel = SimpleNamespace(id=123)

    state = WakeConversationState(voice_client=voice_client, text_channel_id=456)
    bot._voice_states[voice_client.channel.id] = state

    bot.voice_session.stop_speaking.return_value = True

    sent_messages: list[str] = []

    class DummyChannel:
        async def send(self, message: str) -> None:
            sent_messages.append(message)

    bot.get_channel = MagicMock(return_value=DummyChannel())

    asyncio.run(bot._handle_transcription(voice_client, MagicMock(), "please stop talking now"))

    bot.voice_session.stop_speaking.assert_called_once_with(voice_client)
    assert sent_messages == ["Stopped the current voice playback."]
    assert state.transcripts == []


def test_voice_stop_command_ignored_when_not_playing(bot: DiscordAssistantBot) -> None:
    voice_client = MagicMock()
    voice_client.is_playing.return_value = False
    voice_client.channel = SimpleNamespace(id=321)

    state = WakeConversationState(voice_client=voice_client, text_channel_id=None)
    state.active = True
    state.start_time = time.monotonic()
    bot._voice_states[voice_client.channel.id] = state

    asyncio.run(bot._handle_transcription(voice_client, MagicMock(), "stop"))

    bot.voice_session.stop_speaking.assert_not_called()
    assert state.transcripts == ["stop"]


def test_voice_stop_command_without_state(bot: DiscordAssistantBot) -> None:
    voice_client = MagicMock()
    voice_client.is_playing.return_value = True
    voice_client.channel = SimpleNamespace(id=777)

    bot.voice_session.stop_speaking.return_value = True

    asyncio.run(bot._handle_transcription(voice_client, MagicMock(), "stop"))

    bot.voice_session.stop_speaking.assert_called_once_with(voice_client)
