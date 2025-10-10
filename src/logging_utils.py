from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


RESET = "\033[0m"
LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
}


class StyledFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        original_name = record.name
        original_message = record.getMessage()
        original_exc_text = record.exc_text

        record.levelname = self._colorize_level(original_levelname)
        record.name = self._colorize_name(original_name)
        record.message = self._colorize_message(original_name, original_message)

        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        formatted = self.formatMessage(record)

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            formatted = f"{formatted}\n{record.exc_text}"
        if record.stack_info:
            formatted = f"{formatted}\n{self.formatStack(record.stack_info)}"

        record.levelname = original_levelname
        record.name = original_name
        record.message = original_message
        record.exc_text = original_exc_text
        return formatted

    def _colorize_level(self, levelname: str) -> str:
        color = LEVEL_COLORS.get(levelname.upper())
        if not color:
            return levelname
        return f"{color}{levelname}{RESET}"

    def _colorize_name(self, name: str) -> str:
        if name.startswith("src.ai.voice_session"):
            return f"\033[35m{name}{RESET}"
        if name.startswith("src.discord_bot"):
            return f"\033[34m{name}{RESET}"
        return name

    def _colorize_message(self, name: str, message: str) -> str:
        if name.startswith("src.ai.voice_session") and "Live transcription" in message:
            return f"\033[96m{message}{RESET}"
        return message

from .config import LoggingConfig


def configure_logging(config: LoggingConfig) -> None:
    level = getattr(logging, config.level.upper(), logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(
        StyledFormatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )

    logging.basicConfig(level=level, handlers=[console_handler], force=True)

    if config.log_file:
        log_path = Path(config.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(handler)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name)


__all__ = ["configure_logging", "get_logger"]
