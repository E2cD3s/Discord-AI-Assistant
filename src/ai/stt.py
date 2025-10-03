from __future__ import annotations

import asyncio
from pathlib import Path

from faster_whisper import WhisperModel

from ..config import STTConfig
from ..logging_utils import get_logger

_LOGGER = get_logger(__name__)


class SpeechToText:
    def __init__(self, config: STTConfig) -> None:
        model_path = Path(config.model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"Whisper model directory not found at {model_path}. Make sure to download it before running the bot."
            )
        self._config = config
        self._loop = asyncio.get_running_loop()
        _LOGGER.info("Loading Whisper model from %s", model_path)
        self._model = WhisperModel(
            str(model_path),
            device=config.device,
            compute_type=config.compute_type,
        )

    async def transcribe(self, audio_path: Path | str) -> str:
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file for transcription not found: {path}")

        result = await self._loop.run_in_executor(
            None,
            self._transcribe_sync,
            path,
        )
        return result

    def _transcribe_sync(self, audio_path: Path) -> str:
        segments, _ = self._model.transcribe(
            str(audio_path),
            beam_size=self._config.beam_size,
            vad_filter=self._config.vad,
            vad_parameters={
                "threshold": self._config.energy_threshold,
                "min_silence_duration_ms": self._config.min_silence_duration_ms,
            },
            temperature=0.0,
        )
        transcript_parts = [segment.text.strip() for segment in segments]
        transcript = " ".join(part for part in transcript_parts if part)
        _LOGGER.debug("Transcribed %s into: %s", audio_path, transcript)
        return transcript


__all__ = ["SpeechToText"]
