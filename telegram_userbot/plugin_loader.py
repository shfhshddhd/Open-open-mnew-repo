"""Load the shared plugin tree once per hosted Telethon client.

The upstream plugin project assumes one global client and one import lifetime.
This loader keeps that compatibility surface while giving each hosted account
its own freshly imported plugin modules and help registry.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import re
import sys
from pathlib import Path

import config as existing_config
from telethon import events


logger = logging.getLogger(__name__)
_PLUGIN_DIR = Path(__file__).resolve().parent / "plugins"
_load_lock = asyncio.Lock()
_SKIPPED = {"__init__", "bot", "help"}
_DOT_COMMAND_RE = re.compile(r"^\s*\.[A-Za-z0-9_]+(?:\s|$)")


def _clear_plugin_namespace() -> None:
    """Drop source modules so the next hosted client gets fresh globals."""
    for name in list(sys.modules):
        if name == "plugins" or name.startswith("plugins."):
            sys.modules.pop(name, None)
        elif name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)


def _prepare_compatibility_aliases() -> None:
    # The source uses ``from config.config import Config`` while the target
    # intentionally has a single config.py module.
    sys.modules["config.config"] = existing_config
    # The persistent self-update memory lives at the project root so it is
    # shared by every hosted account and is not mistaken for plugin state.
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


async def _call_maybe_async(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _owner_only_command_guard(event) -> None:
    """Stop unauthorized dot commands before any plugin handler sees them."""
    raw_text = getattr(event, "raw_text", None) or getattr(event, "text", None) or ""
    if not _DOT_COMMAND_RE.match(raw_text):
        return

    from utils.decorators import is_hosted_owner

    if not await is_hosted_owner(event):
        raise events.StopPropagation


def _handler_snapshot(client) -> set[tuple[object, object]]:
    """Return the handlers currently registered on one hosted client."""
    return set(client.list_event_handlers())


def _remove_handlers(client, handlers: set[tuple[object, object]]) -> None:
    for callback, event in handlers:
        client.remove_event_handler(callback, event)


async def load_for(userbot) -> dict[str, object]:
    """Load every compatible plugin and bind its handlers to ``userbot``."""
    async with _load_lock:
        _clear_plugin_namespace()
        _prepare_compatibility_aliases()

        client = userbot.client
        # Plugins that mirror control-bot operations need the same manager
        # instance so they can reuse session, target, and monitoring state.
        setattr(client, "_userbot_context", userbot)
        if getattr(userbot, "_plugin_event_handlers", None):
            userbot._remove_plugin_event_handlers()
        baseline_handlers = _handler_snapshot(client)
        guard_event = events.NewMessage()
        client.add_event_handler(_owner_only_command_guard, guard_event)
        bot_module = importlib.import_module("plugins.bot")
        bot_module.init(client)
        await bot_module.register_commands()
        utils_module = importlib.import_module("utils.utils")
        utils_module.init_client(client)

        candidates = sorted(
            (
                path
                for path in _PLUGIN_DIR.glob("*.py")
                if path.stem not in _SKIPPED
            ),
            key=lambda path: (path.stem != "ai_setup", path.name.lower()),
        )
        loaded: list[str] = []
        skipped: list[dict[str, str]] = []

        for path in candidates:
            module_name = f"plugins.{path.stem}"
            plugin_baseline = _handler_snapshot(client)
            try:
                module = importlib.import_module(module_name)
                if hasattr(module, "init"):
                    await _call_maybe_async(module.init(client))
                if hasattr(module, "register_commands"):
                    await _call_maybe_async(module.register_commands())
                loaded.append(path.stem)
            except Exception as exc:
                # A plugin can register a handler before failing later in its
                # setup. Remove those partial handlers with the plugin too.
                _remove_handlers(client, _handler_snapshot(client) - plugin_baseline)
                skipped.append(
                    {
                        "plugin": path.stem,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                logger.exception(
                    "Skipped plugin %s for user %s.",
                    path.stem,
                    getattr(userbot, "user_id", "unknown"),
                )

        plugin_handlers = _handler_snapshot(client) - baseline_handlers
        userbot._plugin_event_handlers = list(plugin_handlers)
        command_count = sum(
            len(data.get("commands", []))
            for data in bot_module.CMD_LIST.values()
            if isinstance(data, dict)
        )
        result = {
            "loaded": loaded,
            "skipped": skipped,
            "plugin_count": len(loaded),
            "command_count": command_count,
            "command_registry": bot_module.CMD_LIST,
        }
        userbot.plugin_report = result
        logger.info(
            "%d plugins loaded for user %s (%d commands); %d skipped.",
            len(loaded),
            getattr(userbot, "user_id", "unknown"),
            command_count,
            len(skipped),
        )

        # Do not leave the current source namespace as the shared import
        # authority. Existing callbacks retain their module globals, while a
        # later hosted user gets a fresh import tree.
        _clear_plugin_namespace()
        _prepare_compatibility_aliases()
        return result
