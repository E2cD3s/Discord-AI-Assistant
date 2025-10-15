from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence

import yaml


@dataclass
class DiscordConfig:
    token: str
    command_prefix: str
    owner_ids: List[int]
    guild_ids: List[int]
    status_rotation_seconds: int
    statuses: List[str]
    wake_word: str
    wake_word_cooldown_seconds: int
    reply_in_thread: bool = True
    voice_idle_timeout_seconds: int = 300
    voice_alone_timeout_seconds: int = 60


@dataclass
class ConversationConfig:
    system_prompt: str
    history_turns: int
    max_tokens: int
    temperature: float
    top_p: float
    presence_penalty: float
    frequency_penalty: float


@dataclass
class OllamaConfig:
    host: str
    model: str
    request_timeout: int
    stream: bool
    keep_alive: Optional[int] = None


@dataclass
class STTConfig:
    model_path: str
    device: str = "cpu"
    compute_type: str = "float32"
    beam_size: int = 5
    vad: bool = True
    energy_threshold: float = 0.5
    min_silence_duration_ms: int = 500


@dataclass
class KokoroConfig:
    voice: str
    speed: float = 1.0
    emotion: str = "neutral"
    output_dir: str = "tts_output"
    format: str = "wav"
    lang_code: str = "en"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: Optional[str] = None
    max_bytes: int = 1_048_576
    backup_count: int = 5


@dataclass
class AppConfig:
    discord: DiscordConfig
    conversation: ConversationConfig
    ollama: OllamaConfig
    stt: STTConfig
    kokoro: KokoroConfig
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    _config_dir: str = field(init=False, repr=False, default="")

    @property
    def config_dir(self) -> Path:
        config_dir = self._config_dir or "."
        return Path(config_dir)

    def resolve_paths(self) -> None:
        stt_path = Path(self.stt.model_path)
        if not stt_path.is_absolute():
            self.stt.model_path = str(self.config_dir / stt_path)
        if self.kokoro.output_dir:
            kokoro_dir = Path(self.kokoro.output_dir)
            if not kokoro_dir.is_absolute():
                self.kokoro.output_dir = str(self.config_dir / kokoro_dir)
        if self.logging.log_file:
            log_file = Path(self.logging.log_file)
            if not log_file.is_absolute():
                self.logging.log_file = str(self.config_dir / log_file)


def _validate_statuses(statuses: Any) -> List[str]:
    if isinstance(statuses, str) or not isinstance(statuses, Sequence):
        raise TypeError("discord.statuses must be a sequence of status strings")

    normalized_statuses: List[str] = []
    for index, status in enumerate(statuses):
        if not isinstance(status, str):
            raise TypeError(
                f"discord.statuses entry at index {index} must be a string, got {type(status).__name__}"
            )
        cleaned = status.strip()
        if not cleaned:
            raise ValueError("Status messages cannot be empty strings")
        normalized_statuses.append(cleaned)

    if not normalized_statuses:
        raise ValueError("At least one status message must be configured")

    return normalized_statuses


def load_config(path: Path | str) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    discord_cfg = raw_config.get("discord", {})
    statuses = _validate_statuses(discord_cfg.get("statuses", []))

    wake_word = str(discord_cfg.get("wake_word", "hey assistant")).strip()
    if not wake_word:
        raise ValueError("Wake word must be a non-empty string")

    status_rotation_seconds = int(discord_cfg.get("status_rotation_seconds", 300))
    if status_rotation_seconds <= 0:
        raise ValueError("status_rotation_seconds must be greater than zero")

    command_prefix = str(discord_cfg.get("command_prefix", "!")).strip()
    if not command_prefix:
        raise ValueError("command_prefix must not be empty")

    app_config = AppConfig(
        discord=DiscordConfig(
            token=str(discord_cfg.get("token", "")),
            command_prefix=command_prefix,
            owner_ids=[int(v) for v in discord_cfg.get("owner_ids", [])],
            guild_ids=[int(v) for v in discord_cfg.get("guild_ids", [])],
            status_rotation_seconds=status_rotation_seconds,
            statuses=statuses,
            wake_word=wake_word.lower(),
            wake_word_cooldown_seconds=int(discord_cfg.get("wake_word_cooldown_seconds", 10)),
            reply_in_thread=bool(discord_cfg.get("reply_in_thread", True)),
            voice_idle_timeout_seconds=max(
                0, int(discord_cfg.get("voice_idle_timeout_seconds", 300))
            ),
            voice_alone_timeout_seconds=max(
                0, int(discord_cfg.get("voice_alone_timeout_seconds", 60))
            ),
        ),
        conversation=ConversationConfig(
            system_prompt=raw_config.get("conversation", {}).get("system_prompt", "You are an offline assistant."),
            history_turns=int(raw_config.get("conversation", {}).get("history_turns", 12)),
            max_tokens=int(raw_config.get("conversation", {}).get("max_tokens", 512)),
            temperature=float(raw_config.get("conversation", {}).get("temperature", 0.7)),
            top_p=float(raw_config.get("conversation", {}).get("top_p", 0.9)),
            presence_penalty=float(raw_config.get("conversation", {}).get("presence_penalty", 0.0)),
            frequency_penalty=float(raw_config.get("conversation", {}).get("frequency_penalty", 0.0)),
        ),
        ollama=OllamaConfig(
            host=raw_config.get("ollama", {}).get("host", "http://localhost:11434"),
            model=raw_config.get("ollama", {}).get("model", "mistral"),
            request_timeout=int(raw_config.get("ollama", {}).get("request_timeout", 120)),
            stream=bool(raw_config.get("ollama", {}).get("stream", True)),
            keep_alive=raw_config.get("ollama", {}).get("keep_alive"),
        ),
        stt=STTConfig(
            model_path=raw_config.get("stt", {}).get("model_path", "models/faster-whisper-medium"),
            device=raw_config.get("stt", {}).get("device", "cpu"),
            compute_type=raw_config.get("stt", {}).get("compute_type", "float32"),
            beam_size=int(raw_config.get("stt", {}).get("beam_size", 5)),
            vad=bool(raw_config.get("stt", {}).get("vad", True)),
            energy_threshold=float(raw_config.get("stt", {}).get("energy_threshold", 0.5)),
            min_silence_duration_ms=int(raw_config.get("stt", {}).get("min_silence_duration_ms", 500)),
        ),
        kokoro=KokoroConfig(
            voice=raw_config.get("kokoro", {}).get("voice", "af_heart"),
            speed=float(raw_config.get("kokoro", {}).get("speed", 1.0)),
            emotion=raw_config.get("kokoro", {}).get("emotion", "neutral"),
            output_dir=raw_config.get("kokoro", {}).get("output_dir", "tts_output"),
            format=raw_config.get("kokoro", {}).get("format", "wav"),
            lang_code=raw_config.get("kokoro", {}).get("lang_code", "en"),
        ),
        logging=LoggingConfig(
            level=raw_config.get("logging", {}).get("level", "INFO"),
            log_file=raw_config.get("logging", {}).get("log_file"),
            max_bytes=int(raw_config.get("logging", {}).get("max_bytes", 1_048_576)),
            backup_count=int(raw_config.get("logging", {}).get("backup_count", 5)),
        ),
    )

    app_config._config_dir = str(config_path.parent)
    app_config.resolve_paths()

    if not app_config.discord.token:
        raise ValueError("Discord bot token must be provided in the configuration file")

    return app_config


__all__ = [
    "AppConfig",
    "DiscordConfig",
    "ConversationConfig",
    "OllamaConfig",
    "STTConfig",
    "KokoroConfig",
    "LoggingConfig",
    "load_config",
]
