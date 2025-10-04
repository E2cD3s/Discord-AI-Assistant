from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .ai.conversation_manager import ConversationManager
from .ai.ollama_client import OllamaClient
from .ai.stt import SpeechToText
from .ai.tts import TextToSpeech
from .ai.voice_session import VoiceSession
from .config import AppConfig, load_config
from .logging_utils import configure_logging
from .preflight import run_preflight_checks


async def run_bot(config: AppConfig) -> None:
    configure_logging(config.logging)
    ollama_client = OllamaClient(config.ollama)
    try:
        await run_preflight_checks(config, ollama_client)
        from .discord_bot import create_bot
        stt = SpeechToText(config.stt)
        tts = TextToSpeech(config.kokoro)
    except Exception:
        await ollama_client.close()
        raise

    conversation_manager = ConversationManager(config.conversation, ollama_client)
    voice_session = VoiceSession(stt, tts)
    bot = create_bot(config, conversation_manager, voice_session)
    try:
        await bot.start(config.discord.token)
    finally:
        await ollama_client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord AI Assistant")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the configuration file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    asyncio.run(run_bot(config))


if __name__ == "__main__":
    main()
