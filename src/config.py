"""Application configuration helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml


class ConfigError(ValueError):
    """Raised when the configuration file is missing required values."""


@dataclass(slots=True)
class DiscordConfig:
    """Settings specific to the Discord integration."""

    token: str
    guild_ids: List[int] = field(default_factory=list)
    wake_words: List[str] = field(default_factory=list)
    activity_text: Optional[str] = None
    command_prefix: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """Serialize the configuration to a basic dictionary."""
        data: Dict[str, Any] = {
            "token": self.token,
            "guild_ids": self.guild_ids,
            "wake_words": self.wake_words,
        }
        if self.activity_text is not None:
            data["activity_text"] = self.activity_text
        if self.command_prefix is not None:
            data["command_prefix"] = self.command_prefix
        return data


@dataclass(slots=True)
class AppConfig:
    """Container for all configuration sections."""

    discord: DiscordConfig


def _coerce_guild_ids(raw_ids: Optional[Iterable[Any]]) -> List[int]:
    if raw_ids is None:
        return []

    guild_ids: List[int] = []
    for value in raw_ids:
        try:
            guild_ids.append(int(value))
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive branch
            msg = f"Invalid guild id value {value!r}: {exc}"
            raise ConfigError(msg) from exc
    return guild_ids


def _ensure_mapping(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):  # pragma: no cover - defensive branch
        raise ConfigError("Configuration file must contain a mapping at the top level")
    return data


def _load_discord_config(section: Mapping[str, Any]) -> DiscordConfig:
    token = section.get("token")
    if not token:
        raise ConfigError("discord.token is required")

    wake_words_raw = section.get("wake_words") or []
    if not isinstance(wake_words_raw, Iterable) or isinstance(wake_words_raw, (str, bytes)):
        raise ConfigError("discord.wake_words must be a list of strings")

    wake_words = [str(word).strip() for word in wake_words_raw if str(word).strip()]

    command_prefix_value = section.get("command_prefix")
    command_prefix = str(command_prefix_value).strip() or None if command_prefix_value is not None else None

    activity_text_value = section.get("activity_text")
    activity_text = (
        str(activity_text_value).strip() or None
        if activity_text_value is not None
        else None
    )

    guild_ids = _coerce_guild_ids(section.get("guild_ids"))

    return DiscordConfig(
        token=str(token),
        wake_words=wake_words,
        guild_ids=guild_ids,
        activity_text=activity_text,
        command_prefix=command_prefix,
    )


def load_config(path: str | Path) -> AppConfig:
    """Load configuration from a YAML file."""

    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"Configuration file not found: {file_path}")

    with file_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    root = _ensure_mapping(data)
    discord_section_raw = root.get("discord")
    if discord_section_raw is None:
        raise ConfigError("Missing 'discord' section in configuration file")

    discord_section = _ensure_mapping(discord_section_raw)
    discord = _load_discord_config(discord_section)
    return AppConfig(discord=discord)
