from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402


def _write_config(tmp_path: Path, statuses: Any) -> Path:
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    config_content = {
        "discord": {
            "token": "token-value",
            "statuses": statuses,
        },
        "conversation": {},
        "ollama": {},
        "stt": {"model_path": "model"},
        "kokoro": {"voice": "af_heart"},
    }
    config_path.write_text(yaml.safe_dump(config_content), encoding="utf-8")
    return config_path


def test_statuses_must_be_sequence(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "single status")

    with pytest.raises(TypeError):
        load_config(config_path)


def test_statuses_entries_cannot_be_blank(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, ["Valid", "   ", "Also valid"])

    with pytest.raises(ValueError):
        load_config(config_path)


def test_statuses_are_normalized_and_trimmed(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, ["  Ready  ", " Away "])

    config = load_config(config_path)

    assert config.discord.statuses == ["Ready", "Away"]
