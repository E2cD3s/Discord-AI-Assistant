from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - heavy import
    from kokoro import KPipeline  # type: ignore
except ImportError as exc:  # pragma: no cover - dependency error
    raise ImportError(
        "The 'kokoro' package is required for text-to-speech synthesis. "
        "Install it from https://github.com/hexgrad/kokoro before running the bot."
    ) from exc

from kokoro.pipeline import LANG_CODES  # type: ignore

from ..config import KokoroConfig
from ..logging_utils import get_logger

_LOGGER = get_logger(__name__)


class TextToSpeech:
    def __init__(self, config: KokoroConfig) -> None:
        if config.format.lower() != "wav":
            raise ValueError("Kokoro currently supports only WAV output. Set format to 'wav' in the config.")
        self._config = config
        output_dir = Path(config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.get_running_loop()
        _LOGGER.info("Initializing Kokoro pipeline with voice %s", config.voice)
        lang_code = self._resolve_lang_code(config.lang_code)
        self._pipeline = KPipeline(lang_code=lang_code)

    async def synthesize(self, text: str, filename: Optional[str] = None) -> Path:
        if not text:
            raise ValueError("Cannot synthesize empty text")
        path = await self._loop.run_in_executor(
            None,
            self._synthesize_sync,
            text,
            filename,
        )
        return path

    def _synthesize_sync(self, text: str, filename: Optional[str]) -> Path:
        output_dir = Path(self._config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        file_stem = filename or f"tts_{int(time.time())}"
        output_path = output_dir / f"{file_stem}.{self._config.format}"
        self._pipeline.save_wav(
            text,
            output_path,
            speaker=self._config.voice,
            speed=self._config.speed,
            emotion=self._config.emotion,
        )
        _LOGGER.debug("Generated speech saved to %s", output_path)
        return output_path

    @staticmethod
    def _resolve_lang_code(configured_code: str) -> str:
        normalized = configured_code.strip().lower().replace("_", "-")
        if not normalized:
            raise ValueError("Kokoro language code must be a non-empty string")

        if normalized in LANG_CODES:
            return normalized

        alias_map = {
            "en": "a",
            "en-us": "a",
            "english": "a",
            "american-english": "a",
            "en-gb": "b",
            "british-english": "b",
            "uk-english": "b",
            "es": "e",
            "es-es": "e",
            "spanish": "e",
            "fr": "f",
            "fr-fr": "f",
            "french": "f",
            "hi": "h",
            "hindi": "h",
            "it": "i",
            "italian": "i",
            "pt": "p",
            "pt-br": "p",
            "portuguese": "p",
            "pt-brasil": "p",
            "ja": "j",
            "jp": "j",
            "japanese": "j",
            "zh": "z",
            "zh-cn": "z",
            "mandarin": "z",
            "chinese": "z",
        }

        if normalized in alias_map:
            return alias_map[normalized]

        for code, description in LANG_CODES.items():
            if normalized == description.lower().replace("_", "-"):
                return code

        raise ValueError(
            "Unsupported Kokoro language code '%s'. Supported codes are: %s"
            % (configured_code, ", ".join(sorted(set(LANG_CODES) | set(alias_map.keys()))))
        )


__all__ = ["TextToSpeech"]
