from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import (
    AppConfig,
    ConversationConfig,
    DiscordConfig,
    KokoroConfig,
    OllamaConfig,
    STTConfig,
)
from src.discord_bot import DiscordAssistantBot


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


def test_wake_word_matches_with_punctuation(bot: DiscordAssistantBot) -> None:
    assert bot._wake_word_regex.search("hey, assistant tell me a joke")


def test_wake_word_matches_with_trailing_punctuation(bot: DiscordAssistantBot) -> None:
    assert bot._wake_word_regex.search("hey assistant?")


def test_wake_word_removal_preserves_remaining_text(bot: DiscordAssistantBot) -> None:
    message = "hey assistant? can you remind hey assistant later"
    cleaned = bot._wake_word_regex.sub("", message, count=1).strip()
    assert cleaned == "can you remind hey assistant later"
