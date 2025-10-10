import sys
import types

import pytest

from src.discord_compat import _install_pycord_shims


@pytest.fixture()
def fake_discord(monkeypatch):
    module = types.ModuleType("discord")

    class Bot:
        @staticmethod
        def slash_command(*, name, description):
            def decorator(func):
                func.__slash_command__ = (name, description)
                return func

            return decorator

        async def sync_commands(self, *, guild_ids=None):  # pragma: no cover - helper stub
            return guild_ids or []

    module.Bot = Bot
    monkeypatch.setitem(sys.modules, "discord", module)
    yield module
    monkeypatch.delitem(sys.modules, "discord", raising=False)
    monkeypatch.delitem(sys.modules, "discord.app_commands", raising=False)


def test_pycord_command_accepts_named_arguments(fake_discord):
    _install_pycord_shims(fake_discord)

    assert hasattr(fake_discord, "app_commands")
    command_cls = fake_discord.app_commands.Command

    def callback():
        return "ok"

    command = command_cls(
        name="reset",
        description="Reset the session",
        callback=callback,
        extras=42,
    )

    assert command.name == "reset"
    assert command.description == "Reset the session"
    assert command.callback is callback
    assert command._extras == {"extras": 42}

    assert command() == "ok"

    copied = command.copy()
    assert copied is not command
    assert copied.name == command.name
    assert copied.description == command.description
    assert copied.callback is command.callback
    assert copied._extras == command._extras
