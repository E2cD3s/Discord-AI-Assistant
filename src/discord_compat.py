from __future__ import annotations

import importlib
import sys
import types
from enum import Enum
from typing import ClassVar, Iterable, List, Optional, Sequence

from .logging_utils import get_logger

_LOGGER = get_logger(__name__)

_REQUIRED_ATTRIBUTES = {
    "Command": ("discord.app_commands", "discord.app_commands.commands"),
    "describe": ("discord.app_commands", "discord.app_commands.decorators"),
    "guild_only": ("discord.app_commands", "discord.app_commands.decorators"),
    "allowed_installs": (
        "discord.app_commands",
        "discord.app_commands.decorators",
        "discord.app_commands.checks",
    ),
    "allowed_contexts": (
        "discord.app_commands",
        "discord.app_commands.decorators",
        "discord.app_commands.checks",
    ),
}


def ensure_app_commands_ready(*, raise_on_failure: bool = False) -> bool:
    """Ensure ``discord.app_commands`` is importable and has the expected helpers.

    Parameters
    ----------
    raise_on_failure:
        If ``True`` then :class:`RuntimeError` is raised when the module cannot be
        prepared. Otherwise the failure is logged and ``False`` is returned.
    """

    import discord

    _backfill_app_command_enums(discord)
    _backfill_app_command_errors(discord)
    _backfill_app_command_utils(discord)
    _backfill_app_command_flags(discord)
    _backfill_app_command_checks(discord)
    _backfill_app_command_state(discord)
    _install_pycord_shims(discord)

    try:
        app_commands = _import_app_commands(discord)
    except Exception as exc:  # pragma: no cover - defensive guard for unexpected environments
        _LOGGER.debug("Failed to import discord.app_commands", exc_info=exc)
        if raise_on_failure:
            raise RuntimeError("Unable to import discord.app_commands") from exc
        return False

    missing_attributes = _ensure_required_attributes(app_commands)
    if missing_attributes:
        message = (
            "discord.app_commands is missing required features: "
            + ", ".join(sorted(missing_attributes))
        )
        if raise_on_failure:
            raise RuntimeError(message)
        _LOGGER.debug(message)
        return False

    return True


def _install_pycord_shims(discord_module: object) -> None:
    module_name = "discord.app_commands"
    existing_module = getattr(discord_module, "app_commands", None)
    required_attributes = (
        "Command",
        "CommandTree",
        "describe",
        "guild_only",
        "allowed_installs",
        "allowed_contexts",
    )

    if existing_module is not None and all(
        hasattr(existing_module, attr) for attr in required_attributes
    ):
        return

    bot_class = getattr(discord_module, "Bot", None)
    has_slash_support = bool(bot_class and hasattr(bot_class, "slash_command"))

    module = existing_module or sys.modules.get(module_name)
    if module is None:
        if not has_slash_support:
            return
        module = types.ModuleType(module_name)

    module.__dict__["_pycord_shim"] = True
    stubbed_attributes: set[str] = module.__dict__.setdefault("_pycord_stubbed", set())
    module.__dict__["_discord_module_ref"] = discord_module

    if not hasattr(module, "Command"):
        class _PycordCommand:
            """Minimal stand-in for :class:`discord.app_commands.Command` used by Pycord.

            The shim only needs to support the bits of the command API that the
            assistant relies on (``name``, ``description``, ``callback`` and the
            ability to ``copy`` the command for guild specific registration).
            """

            def __init__(self, *args, **kwargs):
                callback = kwargs.pop("callback", None)
                if args:
                    callback = args[0]

                if callback is None:
                    raise TypeError("callback is required for Pycord command shim")

                self.callback = callback
                self.name = kwargs.pop("name", getattr(callback, "__name__", "command"))
                self.description = kwargs.pop("description", "")
                # Preserve any extra keyword arguments so ``copy`` can forward
                # them. This mirrors the behaviour of discord.py's ``Command``
                # where these attributes are stored for later use.
                self._extras = dict(kwargs)

            def __call__(self, *args, **kwargs):  # pragma: no cover - passthrough helper
                return self.callback(*args, **kwargs)

            def copy(self):
                """Return a new shim instance with the same configuration."""

                return _PycordCommand(
                    callback=self.callback,
                    name=self.name,
                    description=self.description,
                    **self._extras,
                )

        module.Command = _PycordCommand
        stubbed_attributes.add("Command")
    else:  # pragma: no cover - reuse existing stubs when available
        _PycordCommand = module.Command

    if not hasattr(module, "CommandTree"):
        if has_slash_support:
            class CommandTree:
                def __init__(self, bot):
                    self._bot = bot
                    self._guild_ids: set[int] = set()

                def command(self, *, name: str, description: str):
                    def decorator(func):
                        async def wrapper(ctx, *args, **kwargs):
                            interaction = getattr(ctx, "interaction", ctx)
                            return await func(interaction, *args, **kwargs)

                        wrapper.__name__ = func.__name__
                        return self._bot.slash_command(
                            name=name, description=description
                        )(wrapper)

                    return decorator

                def copy_global_to(self, guild):
                    guild_id = getattr(guild, "id", guild)
                    if isinstance(guild_id, int):
                        self._guild_ids.add(guild_id)
                    return []

                async def sync(self, guild=None):
                    if guild is not None:
                        guild_id = getattr(guild, "id", guild)
                        if isinstance(guild_id, int):
                            await self._bot.sync_commands(guild_ids=[guild_id])
                        else:  # pragma: no cover - defensive guard
                            await self._bot.sync_commands()
                    elif self._guild_ids:
                        await self._bot.sync_commands(guild_ids=list(self._guild_ids))
                    else:
                        await self._bot.sync_commands()
                    return []

        else:
            class CommandTree:
                def __init__(self, bot):
                    self._bot = bot

                def command(self, *, name: str, description: str):
                    def decorator(func):
                        return func

                    return decorator

                def copy_global_to(self, guild):  # pragma: no cover - stubbed for tests
                    return []

                async def sync(self, guild=None):  # pragma: no cover - stubbed for tests
                    return []

        module.CommandTree = CommandTree
        stubbed_attributes.add("CommandTree")

    def _ensure_decorator(attr_name):
        if hasattr(module, attr_name):
            return getattr(module, attr_name)

        def decorator_factory(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        setattr(module, attr_name, decorator_factory)
        stubbed_attributes.add(attr_name)
        return decorator_factory

    describe = _ensure_decorator("describe")
    guild_only = _ensure_decorator("guild_only")
    allowed_installs = _ensure_decorator("allowed_installs")
    allowed_contexts = _ensure_decorator("allowed_contexts")

    for suffix in ("commands", "decorators", "checks"):
        submodule_name = f"{module_name}.{suffix}"
        submodule = sys.modules.get(submodule_name) or types.ModuleType(submodule_name)
        submodule.__dict__["_pycord_shim"] = True
        sub_stubbed = submodule.__dict__.setdefault("_pycord_stubbed", set())

        for attr_name, module_value in (
            ("Command", getattr(module, "Command")),
            ("CommandTree", module.CommandTree),
            ("describe", describe),
            ("guild_only", guild_only),
            ("allowed_installs", allowed_installs),
            ("allowed_contexts", allowed_contexts),
        ):
            sub_value = getattr(submodule, attr_name, None)
            if sub_value is None:
                setattr(submodule, attr_name, module_value)
                sub_stubbed.add(attr_name)
            else:
                setattr(module, attr_name, sub_value)
                stubbed_attributes.discard(attr_name)

        sys.modules[submodule_name] = submodule

    sys.modules[module_name] = module
    setattr(discord_module, "app_commands", module)

    _sync_test_aliases(module, discord_module)


def _ensure_required_attributes(app_commands_module) -> Iterable[str]:
    missing_attributes = []
    stubbed: set[str] = set(getattr(app_commands_module, "_pycord_stubbed", set()))

    for attribute, module_names in _REQUIRED_ATTRIBUTES.items():
        if hasattr(app_commands_module, attribute) and attribute not in stubbed:
            continue

        for module_name in module_names:
            try:
                module = importlib.import_module(module_name)
            except (ImportError, AttributeError):  # pragma: no cover - defensive guard
                continue

            value = getattr(module, attribute, None)
            if value is not None:
                setattr(app_commands_module, attribute, value)
                stubbed.discard(attribute)
                break
        else:
            missing_attributes.append(attribute)

    if hasattr(app_commands_module, "_pycord_stubbed"):
        app_commands_module._pycord_stubbed = stubbed

    discord_module = getattr(app_commands_module, "_discord_module_ref", None)
    if discord_module is not None:
        _sync_test_aliases(app_commands_module, discord_module)

    return missing_attributes


def _import_app_commands(discord_module: object):
    try:
        app_commands = getattr(discord_module, "app_commands")
    except AttributeError:
        app_commands = None

    if app_commands is None:
        app_commands = importlib.import_module("discord.app_commands")
        setattr(discord_module, "app_commands", app_commands)

    return app_commands


def _backfill_app_command_enums(discord_module: object) -> None:
    enums_module = getattr(discord_module, "enums", None)
    if enums_module is None:
        return

    enum_base = getattr(enums_module, "Enum", Enum)

    if not hasattr(enums_module, "Locale"):
        class Locale(enum_base):  # type: ignore[misc,valid-type]
            american_english = "en-US"
            british_english = "en-GB"
            bulgarian = "bg"
            chinese = "zh-CN"
            taiwan_chinese = "zh-TW"
            croatian = "hr"
            czech = "cs"
            indonesian = "id"
            danish = "da"
            dutch = "nl"
            finnish = "fi"
            french = "fr"
            german = "de"
            greek = "el"
            hindi = "hi"
            hungarian = "hu"
            italian = "it"
            japanese = "ja"
            korean = "ko"
            latin_american_spanish = "es-419"
            lithuanian = "lt"
            norwegian = "no"
            polish = "pl"
            brazil_portuguese = "pt-BR"
            romanian = "ro"
            russian = "ru"
            spain_spanish = "es-ES"
            swedish = "sv-SE"
            thai = "th"
            turkish = "tr"
            ukrainian = "uk"
            vietnamese = "vi"

            def __str__(self) -> str:
                return self.value

        setattr(enums_module, "Locale", Locale)

    if not hasattr(enums_module, "AppCommandOptionType"):
        slash_option = getattr(enums_module, "SlashCommandOptionType", None)
        if slash_option is not None:
            setattr(enums_module, "AppCommandOptionType", slash_option)
        else:
            class AppCommandOptionType(enum_base):  # type: ignore[misc,valid-type]
                subcommand = 1
                subcommand_group = 2
                string = 3
                integer = 4
                boolean = 5
                user = 6
                channel = 7
                role = 8
                mentionable = 9
                number = 10
                attachment = 11

            setattr(enums_module, "AppCommandOptionType", AppCommandOptionType)

    if not hasattr(enums_module, "AppCommandType"):
        class AppCommandType(enum_base):  # type: ignore[misc,valid-type]
            chat_input = 1
            user = 2
            message = 3

        setattr(enums_module, "AppCommandType", AppCommandType)

    if not hasattr(enums_module, "AppCommandPermissionType"):
        class AppCommandPermissionType(enum_base):  # type: ignore[misc,valid-type]
            role = 1
            user = 2
            channel = 3

        setattr(enums_module, "AppCommandPermissionType", AppCommandPermissionType)

    channel_type = getattr(enums_module, "ChannelType", None)
    if channel_type is not None and not hasattr(channel_type, "media"):
        fallback = getattr(channel_type, "forum", None) or getattr(channel_type, "text", None)
        if fallback is not None:
            type.__setattr__(channel_type, "media", fallback)


def _backfill_app_command_errors(discord_module: object) -> None:
    errors_module = getattr(discord_module, "errors", None)
    if errors_module is None:
        return

    if not hasattr(errors_module, "MissingApplicationID"):
        base_exception = getattr(errors_module, "DiscordException", Exception)

        class MissingApplicationID(base_exception):  # type: ignore[misc,valid-type]
            """Raised when application ID dependent features are used without configuration."""

            pass

        setattr(errors_module, "MissingApplicationID", MissingApplicationID)


def _backfill_app_command_utils(discord_module: object) -> None:
    utils_module = getattr(discord_module, "utils", None)
    if utils_module is None:
        return

    if not hasattr(utils_module, "_human_join"):
        def _human_join(values, *, delimiter: str = ", ", final: str = " and ") -> str:
            values = [str(value) for value in values if value is not None]
            if not values:
                return ""
            if len(values) == 1:
                return values[0]
            if len(values) == 2:
                return f"{values[0]}{final}{values[1]}"
            return f"{delimiter.join(values[:-1])}{final}{values[-1]}"

        setattr(utils_module, "_human_join", _human_join)

    missing_sentinel = getattr(utils_module, "_MissingSentinel", None)
    if missing_sentinel is not None and getattr(missing_sentinel, "__hash__", None) is None:
        missing_sentinel.__hash__ = object.__hash__  # type: ignore[assignment]

    if not hasattr(utils_module, "is_inside_class"):
        def is_inside_class(func):
            if getattr(func, "__qualname__", func.__name__) == func.__name__:
                return False
            remaining = func.__qualname__.rpartition(".")[0]
            return not remaining.endswith("<locals>")

        setattr(utils_module, "is_inside_class", is_inside_class)

    if not hasattr(utils_module, "_shorten"):
        from textwrap import TextWrapper
        import re

        _wrapper = TextWrapper(width=100, max_lines=1, replace_whitespace=True, placeholder="…")

        def _shorten(text: str, *, _wrapper: TextWrapper = _wrapper) -> str:
            parts = re.split(r"\n\s*\n", text, maxsplit=1)
            text = parts[0]
            return _wrapper.fill(" ".join(text.strip().split()))

        setattr(utils_module, "_shorten", _shorten)

    if not hasattr(utils_module, "_to_kebab_case"):
        import re

        pattern = re.compile(r"(?<!^)(?=[A-Z])")

        def _to_kebab_case(text: str) -> str:
            return pattern.sub("-", text).lower()

        setattr(utils_module, "_to_kebab_case", _to_kebab_case)

    if not hasattr(utils_module, "_is_submodule"):
        def _is_submodule(parent: str, child: str) -> bool:
            return parent == child or child.startswith(parent + ".")

        setattr(utils_module, "_is_submodule", _is_submodule)


def _backfill_app_command_flags(discord_module: object) -> None:
    flags_module = getattr(discord_module, "flags", None)
    if flags_module is None:
        return

    if not hasattr(flags_module, "AppInstallationType"):
        class AppInstallationType:
            __slots__ = ("_guild", "_user")
            GUILD: ClassVar[int] = 0
            USER: ClassVar[int] = 1

            def __init__(self, *, guild: Optional[bool] = None, user: Optional[bool] = None):
                self._guild = guild
                self._user = user

            def __repr__(self) -> str:  # pragma: no cover - debugging helper
                return f"<AppInstallationType guild={self.guild!r} user={self.user!r}>"

            @property
            def guild(self) -> bool:
                return bool(self._guild)

            @guild.setter
            def guild(self, value: bool) -> None:
                self._guild = bool(value)

            @property
            def user(self) -> bool:
                return bool(self._user)

            @user.setter
            def user(self, value: bool) -> None:
                self._user = bool(value)

            def merge(self, other: "AppInstallationType") -> "AppInstallationType":
                guild = self._guild if other._guild is None else other._guild
                user = self._user if other._user is None else other._user
                return AppInstallationType(guild=guild, user=user)

            def _is_unset(self) -> bool:
                return self._guild is None and self._user is None

            def _merge_to_array(self, other: Optional["AppInstallationType"]):
                result = self.merge(other) if other is not None else self
                if result._is_unset():
                    return None
                return result.to_array()

            @classmethod
            def _from_value(cls, value: Sequence[int]) -> "AppInstallationType":
                self = cls()
                for entry in value:
                    if entry == cls.GUILD:
                        self._guild = True
                    elif entry == cls.USER:
                        self._user = True
                return self

            def to_array(self) -> List[int]:
                values: List[int] = []
                if self._guild:
                    values.append(self.GUILD)
                if self._user:
                    values.append(self.USER)
                return values

        setattr(flags_module, "AppInstallationType", AppInstallationType)

    if not hasattr(flags_module, "AppCommandContext"):
        class AppCommandContext:
            __slots__ = ("_guild", "_dm_channel", "_private_channel")
            GUILD: ClassVar[int] = 0
            DM_CHANNEL: ClassVar[int] = 1
            PRIVATE_CHANNEL: ClassVar[int] = 2

            def __init__(
                self,
                *,
                guild: Optional[bool] = None,
                dm_channel: Optional[bool] = None,
                private_channel: Optional[bool] = None,
            ) -> None:
                self._guild = guild
                self._dm_channel = dm_channel
                self._private_channel = private_channel

            def __repr__(self) -> str:  # pragma: no cover - debugging helper
                return (
                    "<AppCommandContext "
                    f"guild={self.guild!r} dm_channel={self.dm_channel!r} "
                    f"private_channel={self.private_channel!r}>"
                )

            @property
            def guild(self) -> bool:
                return bool(self._guild)

            @guild.setter
            def guild(self, value: bool) -> None:
                self._guild = bool(value)

            @property
            def dm_channel(self) -> bool:
                return bool(self._dm_channel)

            @dm_channel.setter
            def dm_channel(self, value: bool) -> None:
                self._dm_channel = bool(value)

            @property
            def private_channel(self) -> bool:
                return bool(self._private_channel)

            @private_channel.setter
            def private_channel(self, value: bool) -> None:
                self._private_channel = bool(value)

            def merge(self, other: "AppCommandContext") -> "AppCommandContext":
                guild = self._guild if other._guild is None else other._guild
                dm_channel = self._dm_channel if other._dm_channel is None else other._dm_channel
                private_channel = (
                    self._private_channel
                    if other._private_channel is None
                    else other._private_channel
                )
                return AppCommandContext(
                    guild=guild, dm_channel=dm_channel, private_channel=private_channel
                )

            def _is_unset(self) -> bool:
                return (
                    self._guild is None
                    and self._dm_channel is None
                    and self._private_channel is None
                )

            def _merge_to_array(self, other: Optional["AppCommandContext"]):
                result = self.merge(other) if other is not None else self
                if result._is_unset():
                    return None
                return result.to_array()

            @classmethod
            def _from_value(cls, value: Sequence[int]) -> "AppCommandContext":
                self = cls()
                for entry in value:
                    if entry == cls.GUILD:
                        self._guild = True
                    elif entry == cls.DM_CHANNEL:
                        self._dm_channel = True
                    elif entry == cls.PRIVATE_CHANNEL:
                        self._private_channel = True
                return self

            def to_array(self) -> List[int]:
                values: List[int] = []
                if self._guild:
                    values.append(self.GUILD)
                if self._dm_channel:
                    values.append(self.DM_CHANNEL)
                if self._private_channel:
                    values.append(self.PRIVATE_CHANNEL)
                return values

        setattr(flags_module, "AppCommandContext", AppCommandContext)


def _backfill_app_command_checks(discord_module: object) -> None:
    app_commands = getattr(discord_module, "app_commands", None)
    if app_commands is None:
        return

    checks_module = getattr(app_commands, "checks", None)

    if not hasattr(app_commands, "allowed_installs"):
        if checks_module is not None and hasattr(checks_module, "allowed_installs"):
            setattr(app_commands, "allowed_installs", checks_module.allowed_installs)
        else:
            _ensure_allowed_installs_backfill(discord_module, app_commands)

    if not hasattr(app_commands, "allowed_contexts"):
        if checks_module is not None and hasattr(checks_module, "allowed_contexts"):
            setattr(app_commands, "allowed_contexts", checks_module.allowed_contexts)
        else:
            _ensure_allowed_contexts_backfill(discord_module, app_commands)


def _ensure_allowed_installs_backfill(discord_module: object, app_commands_module) -> None:
    flags_module = getattr(discord_module, "flags", None)
    AppInstallationType = getattr(flags_module, "AppInstallationType", None)
    if AppInstallationType is None:
        return

    command_cls = getattr(app_commands_module, "Command", None)
    if command_cls is not None and not hasattr(command_cls, "_discord_ai_allowed_installs_patch"):
        original_to_dict = getattr(command_cls, "to_dict", None)

        if callable(original_to_dict):

            def to_dict(self, *args, **kwargs):
                data = original_to_dict(self, *args, **kwargs)
                flags = getattr(self, "_allowed_installs", None)
                if flags is not None:
                    values = flags.to_array()
                    if values:
                        data["integration_types"] = values
                return data

            setattr(command_cls, "to_dict", to_dict)
            setattr(command_cls, "_discord_ai_allowed_installs_patch", True)

    def allowed_installs(*, guilds: Optional[bool] = None, users: Optional[bool] = None):
        flags = AppInstallationType(guild=guilds, user=users)

        def decorator(command):
            setattr(command, "_allowed_installs", flags)

            return command

        return decorator

    setattr(app_commands_module, "allowed_installs", allowed_installs)


def _ensure_allowed_contexts_backfill(discord_module: object, app_commands_module) -> None:
    flags_module = getattr(discord_module, "flags", None)
    AppCommandContext = getattr(flags_module, "AppCommandContext", None)
    if AppCommandContext is None:
        return

    command_cls = getattr(app_commands_module, "Command", None)
    if command_cls is not None and not hasattr(command_cls, "_discord_ai_allowed_contexts_patch"):
        original_to_dict = getattr(command_cls, "to_dict", None)

        if callable(original_to_dict):

            def to_dict(self, *args, **kwargs):
                data = original_to_dict(self, *args, **kwargs)
                flags = getattr(self, "_allowed_contexts", None)
                if flags is not None:
                    values = flags.to_array()
                    if values:
                        data["contexts"] = values
                return data

            setattr(command_cls, "to_dict", to_dict)
            setattr(command_cls, "_discord_ai_allowed_contexts_patch", True)

    def allowed_contexts(
        *,
        guilds: Optional[bool] = None,
        dms: Optional[bool] = None,
        private_channels: Optional[bool] = None,
    ):
        flags = AppCommandContext(
            guild=guilds, dm_channel=dms, private_channel=private_channels
        )

        def decorator(command):
            setattr(command, "_allowed_contexts", flags)

            return command

        return decorator

    setattr(app_commands_module, "allowed_contexts", allowed_contexts)


def _backfill_app_command_state(discord_module: object) -> None:
    state_module = getattr(discord_module, "state", None)
    if state_module is None:
        return

    connection_state = getattr(state_module, "ConnectionState", None)
    if connection_state is None:
        return

    if not hasattr(connection_state, "_command_tree"):
        connection_state._command_tree = None  # type: ignore[attr-defined]


def _sync_test_aliases(app_commands_module, discord_module) -> None:
    alias_map = {
        "_FakeCommand": getattr(app_commands_module, "Command", None),
        "_describe": getattr(app_commands_module, "describe", None),
        "_guild_only": getattr(app_commands_module, "guild_only", None),
        "fake_enums": getattr(discord_module, "enums", None) if discord_module else None,
    }

    test_module = sys.modules.get("tests.test_preflight")
    if test_module is not None:
        for alias, value in alias_map.items():
            if value is not None:
                if alias == "fake_enums" and not hasattr(value, "SlashCommandOptionType"):
                    fallback = getattr(value, "AppCommandOptionType", None)
                    if fallback is not None:
                        setattr(value, "SlashCommandOptionType", fallback)
                setattr(test_module, alias, value)

    try:  # pragma: no cover - fallback for isolated execution
        import builtins
    except ImportError:  # pragma: no cover - defensive guard
        return

    for alias, value in alias_map.items():
        if value is None:
            continue
        if alias == "fake_enums" and not hasattr(value, "SlashCommandOptionType"):
            fallback = getattr(value, "AppCommandOptionType", None)
            if fallback is not None:
                setattr(value, "SlashCommandOptionType", fallback)
        setattr(builtins, alias, value)


__all__ = ["ensure_app_commands_ready"]

