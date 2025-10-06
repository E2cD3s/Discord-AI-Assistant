from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
from enum import Enum as PyEnum

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_discord_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_commands: types.ModuleType | None = None,
    enums: types.ModuleType | None = None,
) -> None:
    for module_name in list(sys.modules):
        if module_name == "discord" or module_name.startswith("discord."):
            sys.modules.pop(module_name, None)

    fake_discord = types.ModuleType("discord")
    fake_opus = types.SimpleNamespace(is_loaded=lambda: True, load_opus=lambda _: None)
    fake_sinks = types.ModuleType("discord.sinks")
    fake_sinks.WaveSink = object()
    fake_enums = enums or types.ModuleType("discord.enums")
    if not hasattr(fake_enums, "Enum"):
        fake_enums.Enum = PyEnum

    fake_discord.opus = fake_opus
    fake_discord.sinks = fake_sinks
    fake_discord.enums = fake_enums
    if app_commands is not None:
        fake_discord.app_commands = app_commands

    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setitem(sys.modules, "discord.sinks", fake_sinks)
    monkeypatch.setitem(sys.modules, "discord.enums", fake_enums)
    if app_commands is not None:
        monkeypatch.setitem(sys.modules, "discord.app_commands", app_commands)


def test_missing_app_command_support_surfaces_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discord_stub(monkeypatch)
    sys.modules.pop("src.discord_compat", None)
    sys.modules.pop("src.preflight", None)
    preflight = importlib.import_module("src.preflight")

    try:
        with pytest.raises(RuntimeError) as excinfo:
            preflight._ensure_discord_sinks_available()
    finally:
        sys.modules.pop("src.preflight", None)

    assert "discord.py[voice]" in str(excinfo.value)


def test_app_command_support_with_required_attributes_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app_commands = types.ModuleType("discord.app_commands")
    fake_app_commands.Command = object()
    fake_app_commands.describe = lambda **_: None
    fake_app_commands.guild_only = lambda: None

    _install_discord_stub(monkeypatch, app_commands=fake_app_commands)
    sys.modules.pop("src.discord_compat", None)
    sys.modules.pop("src.preflight", None)
    preflight = importlib.import_module("src.preflight")

    try:
        preflight._ensure_discord_sinks_available()
    finally:
        sys.modules.pop("src.preflight", None)


def test_app_command_support_can_patch_missing_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app_commands = types.ModuleType("discord.app_commands")
    fake_commands_module = types.ModuleType("discord.app_commands.commands")
    fake_commands_module.Command = object()
    fake_decorators_module = types.ModuleType("discord.app_commands.decorators")
    fake_decorators_module.describe = lambda **_: None
    fake_decorators_module.guild_only = lambda: None

    _install_discord_stub(monkeypatch, app_commands=fake_app_commands)
    monkeypatch.setitem(sys.modules, "discord.app_commands.commands", fake_commands_module)
    monkeypatch.setitem(sys.modules, "discord.app_commands.decorators", fake_decorators_module)

    sys.modules.pop("src.discord_compat", None)
    sys.modules.pop("src.preflight", None)
    preflight = importlib.import_module("src.preflight")

    try:
        preflight._ensure_discord_sinks_available()
    finally:
        sys.modules.pop("src.preflight", None)


def test_app_command_support_backfills_pycord_enums(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_enums = types.ModuleType("discord.enums")
    fake_enums.Enum = PyEnum
    fake_enums.SlashCommandOptionType = PyEnum(
        "SlashCommandOptionType",
        {
            "subcommand": 1,
            "subcommand_group": 2,
            "string": 3,
            "integer": 4,
            "boolean": 5,
            "user": 6,
            "channel": 7,
            "role": 8,
            "mentionable": 9,
            "number": 10,
            "attachment": 11,
        },
    )

    _install_discord_stub(monkeypatch, enums=fake_enums)

    fake_app_commands_package = types.ModuleType("discord.app_commands")
    fake_app_commands_package.__path__ = []
    fake_commands_module = types.ModuleType("discord.app_commands.commands")

    class _FakeCommand:  # pragma: no cover - simple placeholder
        pass

    def _describe(**_: object):
        from discord.enums import Locale  # type: ignore

        _ = Locale.american_english

        def decorator(func):
            return func

        return decorator

    def _guild_only():
        return _describe()

    fake_commands_module.Command = _FakeCommand
    fake_commands_module.describe = _describe
    fake_commands_module.guild_only = _guild_only

    fake_decorators_module = types.ModuleType("discord.app_commands.decorators")
    fake_decorators_module.describe = _describe
    fake_decorators_module.guild_only = _guild_only

    monkeypatch.setitem(sys.modules, "discord.app_commands", fake_app_commands_package)
    monkeypatch.setitem(sys.modules, "discord.app_commands.commands", fake_commands_module)
    monkeypatch.setitem(sys.modules, "discord.app_commands.decorators", fake_decorators_module)

    sys.modules.pop("src.discord_compat", None)
def test_app_command_support_can_patch_missing_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app_commands = types.ModuleType("discord.app_commands")
    fake_commands_module = types.ModuleType("discord.app_commands.commands")
    fake_commands_module.Command = object()
    fake_decorators_module = types.ModuleType("discord.app_commands.decorators")
    fake_decorators_module.describe = lambda **_: None
    fake_decorators_module.guild_only = lambda: None

    _install_discord_stub(monkeypatch, app_commands=fake_app_commands)
    monkeypatch.setitem(sys.modules, "discord.app_commands.commands", fake_commands_module)
    monkeypatch.setitem(sys.modules, "discord.app_commands.decorators", fake_decorators_module)

    sys.modules.pop("src.preflight", None)
    preflight = importlib.import_module("src.preflight")

    try:
        preflight._ensure_discord_sinks_available()
    finally:
        sys.modules.pop("src.preflight", None)

    import discord

    assert hasattr(discord, "app_commands")
    assert discord.app_commands.Command is _FakeCommand
    assert discord.app_commands.describe is _describe
    assert discord.app_commands.guild_only is _guild_only

    assert hasattr(discord.enums, "Locale")
    assert discord.enums.Locale.american_english.value == "en-US"
    assert discord.enums.AppCommandOptionType is fake_enums.SlashCommandOptionType
    assert discord.enums.AppCommandType.chat_input.value == 1
    assert discord.enums.AppCommandPermissionType.role.value == 1
