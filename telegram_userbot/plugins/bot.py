"""Direct-reply help registry for the hosted userbot clients.

The original plugin project used a second Telegram bot for inline help.  That
cannot be shared by this multi-user application because BOT_TOKEN belongs to
the PTB control bot, so this module deliberately has no Telegram bot client.
"""

from __future__ import annotations

from html import escape

from telethon import events

from utils import help_ui
from utils import utils as userbot_utils
from utils.decorators import rishabh


CMD_LIST: dict[str, dict[str, object]] = {}
bot = None
_client = None

HELP_COMMANDS = [
    ".help — Show all loaded plugins and commands",
    ".help <plugin> — Show one plugin's commands",
    ".plugins — List all loaded plugin modules",
    ".findplugin <term> — Search plugins by name or keyword",
    ".helpstats — Show loaded plugin statistics",
]


def init(client_instance):
    global _client
    _client = client_instance
    return None


def init_bot(client=None):
    """Compatibility no-op: the PTB bot owns BOT_TOKEN in this application."""
    return None


def add_handler(plugin_name, commands, description=""):
    normalized = commands.copy() if isinstance(commands, list) else [commands]
    CMD_LIST[str(plugin_name)] = {
        "commands": normalized,
        "description": str(description or ""),
    }


def remove_handler(plugin_name):
    """Compatibility helper used by the upstream plugin manager."""
    return CMD_LIST.pop(str(plugin_name), None) is not None


def _sorted_names() -> list[str]:
    return sorted(CMD_LIST, key=lambda name: (name != "quickhelp", name.lower()))


def _plugin_text(plugin_name: str) -> str:
    data = CMD_LIST[plugin_name]
    commands = data.get("commands", [])
    description = data.get("description", "No description")
    return help_ui.build_plugin_text(
        plugin_name,
        {
            "commands": commands if isinstance(commands, list) else [commands],
            "description": description,
        },
    )


def _menu_text() -> str:
    return "\n\n".join(_plugin_overview_sections())


def _plugin_overview_sections(max_chars: int = 3500) -> list[str]:
    """Render the complete plugin overview as Telegram-safe message sections."""
    names = _sorted_names()
    total_commands = sum(
        len(data.get("commands", []))
        for data in CMD_LIST.values()
        if isinstance(data, dict)
    )
    header = [
        "<b>FLEX FUCKER USERBOT — Help Menu</b>",
        "",
        f"<i>{len(names)} plugins · {total_commands} commands</i>",
        "",
    ]
    sections: list[str] = []
    lines = header.copy()
    for position, name in enumerate(names, start=1):
        data = CMD_LIST[name]
        commands = data.get("commands", [])
        description = str(data.get("description") or "No description")
        count = len(commands) if isinstance(commands, list) else 1
        plugin_line = (
            f"{position}.{help_ui.icon_for(name)} <b>{help_ui.esc(name.title())}</b> "
            f"<code>[{count}]</code> — {help_ui.esc(description[:120])}"
        )
        if len(lines) > len(header) and len("\n".join(lines + [plugin_line])) > max_chars:
            sections.append("\n".join(lines))
            lines = [
                "<b>All plugins (continued)</b>",
                "",
            ]
        lines.append(plugin_line)
    lines.extend(
        [
            "",
            "<i>Use <code>.help &lt;plugin&gt;</code> for module details.</i>",
        ]
    )
    if lines:
        sections.append("\n".join(lines))
    return sections


def _full_help_text() -> str:
    """Render only the complete plugin overview."""
    return "\n\n".join(_plugin_overview_sections())


def _full_help_sections() -> list[str]:
    """Return only plugin-list sections under Telegram's message limit."""
    return _plugin_overview_sections()


async def _reply_sections(event, sections: list[str]) -> None:
    """Send full help in complete HTML sections under Telegram's size limit."""
    current: list[str] = []
    current_length = 0
    for section in sections:
        section_length = len(section) + (2 if current else 0)
        if current and current_length + section_length > 3900:
            await event.reply("\n\n".join(current), parse_mode="html")
            current = []
            current_length = 0
        current.append(section)
        current_length += len(section) + (2 if len(current) > 1 else 0)
    if current:
        await event.reply("\n\n".join(current), parse_mode="html")


async def _reply_chunks(event, text: str) -> None:
    for offset in range(0, len(text), 4000):
        await event.reply(text[offset : offset + 4000], parse_mode="html")


async def register_commands():
    """Register direct help commands on the current hosted Telethon client."""
    client = _client or userbot_utils.CipherElite
    if client is None:
        raise RuntimeError("Help commands cannot load without a hosted client.")

    add_handler(
        "help",
        HELP_COMMANDS,
        "Help, plugin discovery and loaded-command statistics",
    )

    @client.on(events.NewMessage(pattern=r"\.help(?:\s+(.+))?"))
    @rishabh()
    async def help_handler(event):
        requested = event.pattern_match.group(1)
        if requested:
            plugin_name = requested.strip().lower()
            if plugin_name in CMD_LIST:
                await _reply_chunks(event, _plugin_text(plugin_name))
                return
            available = "\n".join(
                f"{help_ui.icon_for(name)} <code>{help_ui.esc(name)}</code>"
                for name in _sorted_names()
            )
            await _reply_chunks(
                event,
                f"<b>Plugin not found:</b> <code>{help_ui.esc(plugin_name)}</code>\n\n"
                f"<b>Available modules</b>\n{available}",
            )
            return
        await _reply_sections(event, _full_help_sections())

    @client.on(events.NewMessage(pattern=r"\.plugins$"))
    @rishabh()
    async def plugins_handler(event):
        await _reply_sections(event, _plugin_overview_sections())

    @client.on(events.NewMessage(pattern=r"\.findplugin\s+(.+)"))
    @rishabh()
    async def find_plugin_handler(event):
        term = event.pattern_match.group(1).strip().lower()
        matches = [name for name in _sorted_names() if term in name.lower()]
        if not matches:
            await event.reply(
                f"<b>No plugin matched:</b> <code>{help_ui.esc(term)}</code>",
                parse_mode="html",
            )
            return
        await _reply_chunks(
            event,
            "<b>Matching modules</b>\n\n"
            + "\n".join(
                f"{help_ui.icon_for(name)} <code>.help {help_ui.esc(name)}</code>"
                for name in matches
            ),
        )

    @client.on(events.NewMessage(pattern=r"\.helpstats$"))
    @rishabh()
    async def help_stats_handler(event):
        total_plugins = len(CMD_LIST)
        total_commands = sum(
            len(data.get("commands", []))
            for data in CMD_LIST.values()
            if isinstance(data, dict)
        )
        await event.reply(
            "<b>Help Statistics</b>\n\n"
            f"<code>{total_plugins}</code> plugins\n"
            f"<code>{total_commands}</code> commands",
            parse_mode="html",
        )
