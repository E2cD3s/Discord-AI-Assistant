from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_discord_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_discord = types.ModuleType("discord")
    fake_opus = types.SimpleNamespace(is_loaded=lambda: True, load_opus=lambda _: None)
    fake_sinks = types.ModuleType("discord.sinks")
    fake_sinks.WaveSink = object()
    fake_enums = types.ModuleType("discord.enums")

    fake_discord.opus = fake_opus
    fake_discord.sinks = fake_sinks
    fake_discord.enums = fake_enums

    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setitem(sys.modules, "discord.sinks", fake_sinks)
    monkeypatch.setitem(sys.modules, "discord.enums", fake_enums)


def test_missing_app_command_option_type_surfaces_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discord_stub(monkeypatch)
    sys.modules.pop("src.preflight", None)
    preflight = importlib.import_module("src.preflight")

    try:
        with pytest.raises(RuntimeError) as excinfo:
            preflight._ensure_discord_sinks_available()
    finally:
        sys.modules.pop("src.preflight", None)

    assert "py-cord[voice]" in str(excinfo.value)
