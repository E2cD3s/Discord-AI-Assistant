from __future__ import annotations

import asyncio
from ctypes.util import find_library
from pathlib import Path
from shutil import which

import discord

from .ai.ollama_client import OllamaClient
from .config import AppConfig
from .discord_compat import ensure_app_commands_ready
from .logging_utils import get_logger

_LOGGER = get_logger(__name__)


def _ensure_ffmpeg_available() -> None:
    if which("ffmpeg") is None:
        raise RuntimeError(
            "FFmpeg executable not found on PATH. Install FFmpeg to enable Discord voice playback."
        )
    _LOGGER.debug("FFmpeg binary located successfully.")


def _ensure_opus_loaded() -> None:
    if discord.opus.is_loaded():
        return

    library_name = find_library("opus")
    if not library_name:
        raise RuntimeError(
            "Opus library not found. Install libopus (Linux), opus.dll (Windows), or libopus.dylib (macOS) for voice features."
        )

    try:
        discord.opus.load_opus(library_name)
    except OSError as exc:  # pragma: no cover - platform dependent
        raise RuntimeError(
            f"Failed to load Opus library '{library_name}'. Confirm the codec is installed and reachable."
        ) from exc

    _LOGGER.debug("Loaded Opus codec from %s", library_name)


def _ensure_discord_sinks_available() -> None:
    try:
        from discord import sinks as _sinks  # type: ignore
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Required discord voice modules are unavailable. Install 'py-cord[voice]==2.6.1' to enable voice capture."
        ) from exc

    if not hasattr(_sinks, "WaveSink"):
        raise RuntimeError(
            "discord.sinks.WaveSink is unavailable. Install or update 'py-cord[voice]==2.6.1' "
            "to enable voice capture."
        )

    if not ensure_app_commands_ready():
        raise RuntimeError(
            "discord.app_commands is missing required features (Command, describe, or guild_only)."
            " Install or update 'py-cord[voice]==2.6.1' to enable slash command support."
        )

    _LOGGER.debug("discord voice sinks and enums are available")


def _ensure_stt_assets(config: AppConfig) -> None:
    model_path = Path(config.stt.model_path)
    if not model_path.exists():
        raise FileNotFoundError(
            f"Configured Whisper model directory not found at {model_path}. Download the Faster-Whisper model before running."
        )


async def run_preflight_checks(config: AppConfig, ollama_client: OllamaClient) -> None:
    """Raise early errors for missing runtime dependencies before starting the bot."""

    _ensure_ffmpeg_available()
    _ensure_opus_loaded()
    _ensure_discord_sinks_available()
    _ensure_stt_assets(config)

    try:
        await asyncio.wait_for(ollama_client.ping(), timeout=config.ollama.request_timeout)
    except Exception as exc:
        raise RuntimeError(
            f"Unable to connect to Ollama at {config.ollama.host}. Ensure the service is running and reachable."
        ) from exc


__all__ = ["run_preflight_checks"]

