from __future__ import annotations

import asyncio
from io import BufferedIOBase, BytesIO
from pathlib import Path
from typing import BinaryIO, Union

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

    async def transcribe(self, audio_source: Union[Path, str, BinaryIO, BufferedIOBase, BytesIO]) -> str:
        path: Path | None = None
        stream: BinaryIO | BufferedIOBase | BytesIO | None = None

        if isinstance(audio_source, (str, Path)):
            path = Path(audio_source)
            if not path.exists():
                raise FileNotFoundError(f"Audio file for transcription not found: {path}")
        elif hasattr(audio_source, "read"):
            stream = audio_source  # type: ignore[assignment]
        else:
            raise TypeError("audio_source must be a path-like object or a binary stream")

        result = await self._loop.run_in_executor(
            None,
            self._transcribe_sync,
            path,
            stream,
        )
        return result

    def _transcribe_sync(
        self,
        audio_path: Path | None,
        audio_stream: BinaryIO | BufferedIOBase | BytesIO | None,
    ) -> str:
        if audio_path is not None:
            audio_input: Union[str, BinaryIO, BufferedIOBase, BytesIO] = str(audio_path)
        elif audio_stream is not None:
            try:
                audio_stream.seek(0)
            except (AttributeError, OSError):
                _LOGGER.warning("Audio stream is not seekable; transcription accuracy may be affected.")
            audio_input = audio_stream
        else:
            raise ValueError("Either audio_path or audio_stream must be provided for transcription")

        segments, _ = self._model.transcribe(
            audio_input,
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
