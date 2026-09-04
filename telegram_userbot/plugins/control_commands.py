# =============================================================================
#  FLEX FUCKER USERBOT — Control Command Bridge Plugin
#
#  Purpose: expose the control bot's slash-command operations as dot commands
#  on the hosted Telethon account while reusing the existing shared storage.
# =============================================================================

from __future__ import annotations

from html import escape
import logging

from telethon import events

import database.mongo as db
from bot.handlers.start import ALL_COMMANDS_TEXT, MENU_TEXT
from bot.handlers.target import _pending_removeall
from plugins.bot import add_handler
from utils.decorators import is_bot_owner, rishabh
from utils.utils import CipherElite
from utils.ai_provider import ai_status_for_user, normalize_ai_provider, set_ai_provider
from utils.gemini_rotation import add_key
from utils.key_manager import add_provider_key, remove_provider_key, switch_provider_key


VERSION = "1.0.0"
CATEGORY = "utilities"

logger = logging.getLogger(__name__)
client = None


def _userbot():
    if client is None:
        raise RuntimeError("Hosted userbot client is not initialized.")
    userbot = getattr(client, "_userbot_context", None)
    if userbot is None:
        raise RuntimeError("Hosted userbot context is not available.")
    return userbot


async def _is_bot_owner(event) -> bool:
    """Only the control-bot owner may manage shared provider keys."""
    return await is_bot_owner(event)


async def _require_bot_owner(event) -> bool:
    if await _is_bot_owner(event):
        return True
    await event.reply("⛔ Aapko is command ka access nahi hai.")
    return False


def _args(event) -> list[str]:
    raw = event.pattern_match.group(1) or ""
    return raw.strip().split() if raw.strip() else []


def _key_target(args: list[str]) -> tuple[str, int] | None:
    """Parse ``<number>`` or ``<provider> <number>`` command arguments."""
    if len(args) == 1 and args[0].lstrip("-").isdigit():
        return "gemini", int(args[0])
    if len(args) == 2 and args[1].lstrip("-").isdigit():
        provider = {
            "gemini": "gemini",
            "openrouter": "openrouter",
            "claude": "anthropic",
            "anthropic": "anthropic",
        }.get(args[0].lower())
        if provider:
            return provider, int(args[1])
    return None


def _register(name: str, commands: list[str], description: str) -> None:
    add_handler(name, commands, description)


def init(client_instance):
    global client
    client = client_instance

    _register(
        "control_commands",
        [
            ".start — Show the control-bot main menu",
            ".allcommands — Show the complete control-bot command list",
            ".host — Show hosted-account status",
            ".unhost — Remove the hosted account and stop the userbot",
            ".cancel — Cancel an in-progress hosting operation",
            ".targetadd <group_chat_id> <@username_or_user_id> — Add a target mapping",
            ".targetremove <group_chat_id> <@username_or_user_id> — Remove a target mapping",
            ".targetlist — List target mappings",
            ".targetremoveall — Remove all target mappings after confirmation",
            ".boton — Enable bridge monitoring",
            ".botoff — Disable bridge monitoring",
            ".aimode <gemini|openrouter> [API_KEY] — Select provider or add its key",
            ".aimodeon — Enable AI mention replies",
            ".aimodeoff — Disable AI mention replies",
            ".addkey <API_KEY> — Add a Gemini API key (owner only)",
            ".delkey [provider] <number> — Delete a saved key (owner only)",
            ".switchkey [provider] <number> — Choose the first key (owner only)",
        ],
        "Hosted-account equivalents of the control-bot slash commands",
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.start$"))
@rishabh()
async def start_command(event):
    await event.reply(MENU_TEXT, parse_mode="html")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.allcommands$"))
@rishabh()
async def allcommands_command(event):
    await event.reply(ALL_COMMANDS_TEXT, parse_mode="html")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.host$"))
@rishabh()
async def host_command(event):
    await event.reply(
        "✅ You already have an active hosted account.\n"
        "Use /unhost first if you want to replace it."
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.unhost$"))
@rishabh()
async def unhost_command(event):
    userbot = _userbot()
    await event.reply(
        "🗑️ Your hosted account has been removed.\n"
        "Session deleted, userbot stopped, all data cleared."
    )
    manager = getattr(userbot, "manager", None)
    if manager is None:
        raise RuntimeError("Hosted userbot manager is not initialized.")
    await manager.remove_session(userbot.user_id)


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.cancel$"))
@rishabh()
async def cancel_command(event):
    await event.reply("❎ /host cancelled.")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.targetadd(?:\s+(.+))?$"))
@rishabh()
async def targetadd_command(event):
    args = _args(event)
    if len(args) != 2:
        await event.reply(
            "Usage: .targetadd <group_chat_id> <@username_or_user_id>\n"
            "Example: .targetadd -1001234567890 @username"
        )
        return

    userbot = _userbot()
    if not userbot.is_running():
        await event.reply("⚠️ You need a hosted account first. Use /host to set one up.")
        return

    try:
        group_chat_id = int(args[0])
    except ValueError:
        await event.reply(
            "Usage: .targetadd <group_chat_id> <@username_or_user_id>\n"
            "Example: .targetadd -1001234567890 @username"
        )
        return
    identifier = args[1].strip()
    if not identifier:
        await event.reply(
            "Usage: .targetadd <group_chat_id> <@username_or_user_id>\n"
            "Example: .targetadd -1001234567890 @username"
        )
        return

    try:
        group = await userbot.client.get_entity(group_chat_id)
        if getattr(group, "broadcast", False) or not getattr(group, "title", None):
            raise ValueError("The chat ID does not belong to a group.")
    except Exception:
        logger.warning(
            "Target mapping group validation failed: user_id=%s group_chat_id=%s",
            userbot.user_id,
            group_chat_id,
        )
        await event.reply(
            f"❌ Could not validate group <code>{group_chat_id}</code>.\n"
            "Make sure the hosted account is a member of that group.",
            parse_mode="HTML",
        )
        return

    await event.reply(f"🔍 Validating target {identifier}…")
    target = await userbot.manager.resolve_target(userbot.user_id, identifier)
    if target is None:
        await event.reply(
            f"❌ Could not validate user <code>{identifier}</code>.\n"
            "Use a valid @username or Telegram user ID visible to the hosted account.",
            parse_mode="HTML",
        )
        return

    created = await db.upsert_target_mapping(
        user_id=userbot.user_id,
        group_chat_id=group_chat_id,
        target=target,
        group_title=getattr(group, "title", None) or str(group_chat_id),
    )
    bot_enabled = await db.get_setting(userbot.user_id, "bot_enabled", True)
    if bot_enabled:
        await userbot.enable_monitoring()
    action = "created" if created else "updated"
    status_text = (
        "🟢 <b>Aapka bot abhi ON hai.</b>\n"
        "Target monitoring active hai."
        if bot_enabled
        else "🔴 <b>Aapka bot abhi OFF hai.</b>\n"
        "Mapping save ho gayi hai, lekin monitoring band hai.\n"
        "Monitoring shuru karne ke liye /boton bhejein."
    )
    await event.reply(
        f"✅ Mapping {action}.\n\n"
        f"Group: <b>{getattr(group, 'title', None) or group_chat_id}</b>\n"
        f"Group ID: <code>{group_chat_id}</code>\n"
        f"Target: <b>{target['name']}</b>\n"
        f"Target ID: <code>{target['target_id']}</code>\n\n"
        f"{status_text}\n\n"
        "Messages from this target in this group will be copied to Saved Messages.",
        parse_mode="HTML",
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.targetremove(?:\s+(.+))?$"))
@rishabh()
async def targetremove_command(event):
    args = _args(event)
    if len(args) != 2:
        await event.reply(
            "Usage: .targetremove <group_chat_id> <@username_or_user_id>\n"
            "Example: .targetremove -1001234567890 @username"
        )
        return

    userbot = _userbot()
    if not userbot.is_running():
        await event.reply("⚠️ You need a hosted account first. Use /host to set one up.")
        return
    try:
        group_chat_id = int(args[0])
    except ValueError:
        await event.reply(
            "Usage: .targetremove <group_chat_id> <@username_or_user_id>\n"
            "Example: .targetremove -1001234567890 @username"
        )
        return
    identifier = args[1].strip()

    mapping = await db.get_target_mapping_by_identifier(
        userbot.user_id, group_chat_id, identifier
    )
    if mapping is None:
        resolved = await userbot.manager.resolve_target(userbot.user_id, identifier)
        if resolved is not None:
            mapping = await db.get_target_mapping(
                userbot.user_id, group_chat_id, int(resolved["target_id"])
            )
    if mapping is None:
        await event.reply("❌ No matching target mapping was found for that group.")
        return

    target_id = int(mapping["target_user_id"])
    removed = await db.remove_target_mapping(userbot.user_id, group_chat_id, target_id)
    await userbot.clear_target_monitoring(target_id, group_chat_id)
    if not await db.get_target_mappings(userbot.user_id):
        await userbot.disable_monitoring()
    if removed:
        await event.reply(
            f"🗑️ Removed target <code>{target_id}</code> from group "
            f"<code>{group_chat_id}</code>.",
            parse_mode="HTML",
        )
    else:
        await event.reply("❌ The target mapping was already removed.")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.targetlist$"))
@rishabh()
async def targetlist_command(event):
    userbot = _userbot()
    mappings = await db.get_target_mappings(userbot.user_id)
    if not mappings:
        await event.reply("📋 No target mappings are configured.")
        return

    lines = ["📋 <b>Your target mappings:</b>\n"]
    for index, mapping in enumerate(
        sorted(
            mappings,
            key=lambda item: (
                str(item.get("group_title") or ""),
                int(item.get("group_chat_id", 0)),
                str(item.get("target_name") or ""),
            ),
        ),
        1,
    ):
        group_title = escape(str(mapping.get("group_title") or "Unknown group"))
        group_id = escape(str(mapping.get("group_chat_id", "—")))
        target_name = escape(str(mapping.get("target_name") or "Unknown user"))
        target_username = mapping.get("target_username") or ""
        username_text = f"@{escape(target_username)}" if target_username else "—"
        target_id = escape(str(mapping.get("target_user_id", "—")))
        lines.append(
            f"{index}. <b>{group_title}</b> "
            f"(<code>{group_id}</code>)\n"
            f"   ↳ <b>{target_name}</b> {username_text} "
            f"(<code>{target_id}</code>)"
        )
    await event.reply("\n".join(lines), parse_mode="HTML")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.targetremoveall$"))
@rishabh()
async def targetremoveall_command(event):
    userbot = _userbot()
    mappings = await db.get_target_mappings(userbot.user_id)
    if not mappings:
        _pending_removeall.discard(userbot.user_id)
        await event.reply("📋 Your target mapping list is already empty.")
        return

    if userbot.user_id not in _pending_removeall:
        _pending_removeall.add(userbot.user_id)
        await event.reply(
            f"⚠️ This will remove all <b>{len(mappings)}</b> target mapping(s).\n"
            "Send .targetremoveall again to confirm, or use another command to cancel.",
            parse_mode="HTML",
        )
        return

    _pending_removeall.discard(userbot.user_id)
    removed_count = await db.remove_all_target_mappings(userbot.user_id)
    await userbot.disable_monitoring()
    await event.reply(
        f"🗑️ Removed <b>{removed_count}</b> target mapping(s) and cleared monitoring data.",
        parse_mode="HTML",
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.boton$"))
@rishabh()
async def boton_command(event):
    userbot = _userbot()
    await db.set_setting(userbot.user_id, "bot_enabled", True)
    mappings = await db.get_target_mappings(userbot.user_id)
    if mappings:
        await userbot.enable_monitoring()
    await event.reply(
            f"🟢 Bot enabled. Monitoring {len(mappings)} target mapping(s)."
        if mappings
            else "🟢 Bot enabled. Add a mapping with .targetadd to start monitoring."
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.botoff$"))
@rishabh()
async def botoff_command(event):
    userbot = _userbot()
    await db.set_setting(userbot.user_id, "bot_enabled", False)
    await userbot.disable_monitoring()
    await event.reply(
        "🔴 Bot disabled. Mappings safe hain aur .boton ke baad resume honge."
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.aimode(?:\s+(.+))?$"))
@rishabh()
async def aimode_command(event):
    """Select the provider and optionally add a provider key."""
    args = _args(event)
    if not args or len(args) > 2:
        await event.reply(
            "Usage: .aimode <gemini|openrouter> [API_KEY]\n"
            "Example: .aimode gemini YOUR_API_KEY"
        )
        return

    provider_name = args[0].lower()
    if provider_name not in {"gemini", "openrouter"}:
        await event.reply("⚠️ Choose a provider: gemini or openrouter.")
        return
    provider = normalize_ai_provider(provider_name)
    userbot = _userbot()

    key_message = ""
    if len(args) == 2:
        if not await _require_bot_owner(event):
            return
        added = (
            add_key(args[1])
            if provider == "gemini"
            else add_provider_key(provider, args[1])
        )
        key_message = (
            f"{provider.upper()} API KEY SAVED"
            if added
            else f"{provider.upper()} API KEY ALREADY SAVED OR INVALID"
        )

    await set_ai_provider(userbot.user_id, provider)
    enabled = bool(await db.get_setting(userbot.user_id, "ai_mode", False))
    status = await ai_status_for_user(userbot.user_id, enabled=enabled)
    selected = status.get("selected")
    active_key = str(selected["index"]) if selected else "NONE"
    response = (
        "🤖 AI provider selected\n"
        f"Provider: {status['provider'].title()}\n"
        f"Active API key: {active_key}\n"
        f"Available API keys: {status['active_count']}/{status['total_count']}"
    )
    if key_message:
        response = f"{key_message}\n{response}"
    await event.reply(response)


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.aimodeon$"))
@rishabh()
async def aimodeon_command(event):
    userbot = _userbot()
    already_enabled = bool(await db.get_setting(userbot.user_id, "ai_mode", False))
    if already_enabled:
        status = await ai_status_for_user(userbot.user_id, enabled=True)
        selected = status.get("selected")
        active_key = str(selected["index"]) if selected else "NONE"
        await event.reply(
            "✅ AI Mode is already on\n"
            f"Provider: {status['provider'].title()}\n"
            f"Active API key: {active_key}\n"
            f"Available API keys: {status['active_count']}/{status['total_count']}"
        )
        return

    await db.set_setting(userbot.user_id, "ai_mode", True)
    userbot.enable_ai_mode()
    status = await ai_status_for_user(userbot.user_id, enabled=True)
    selected = status.get("selected")
    active_key = str(selected["index"]) if selected else "NONE"
    await event.reply(
        "✅ AI Mode is on\n"
        f"Provider: {status['provider'].title()}\n"
        f"Active API key: {active_key}\n"
        f"Available API keys: {status['active_count']}/{status['total_count']}\n"
        "I will reply to group mentions after a short delay."
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.aimodeoff$"))
@rishabh()
async def aimodeoff_command(event):
    userbot = _userbot()
    await db.set_setting(userbot.user_id, "ai_mode", False)
    await userbot.disable_ai_mode()
    status = await ai_status_for_user(userbot.user_id, enabled=False)
    await event.reply(
        "🔴 AI Mode is off. Saved conversation memory is unchanged.\n"
        f"Provider remains: {status['provider'].title()}\n"
        f"Rotation: {status['rotation']}"
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.addkey(?:\s+(.+))?$"))
@rishabh()
async def addkey_command(event):
    if not await _require_bot_owner(event):
        return
    args = _args(event)
    if not args:
        await event.reply("Usage: .addkey <API_KEY>")
        return
    if add_key(" ".join(args)):
        await event.reply(
            "✅ Gemini key saved. Saved keys have priority over GEMINI_API_KEY."
        )
    else:
        await event.reply("⚠️ Key is empty or already saved.")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.delkey(?:\s+(.+))?$"))
@rishabh()
async def delkey_command(event):
    if not await _require_bot_owner(event):
        return
    args = _args(event)
    target = _key_target(args)
    if target is None:
        await event.reply("Usage: .delkey [gemini|openrouter|claude] <number>")
        return
    provider, index = target
    removed, reason = remove_provider_key(provider, index)
    if removed:
        await event.reply(f"✅ {provider.title()} key {index} deleted.")
    elif reason == "fallback":
        await event.reply(
            "⚠️ Environment fallback keys cannot be deleted here."
        )
    else:
        await event.reply("⚠️ Invalid key number.")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.switchkey(?:\s+(.+))?$"))
@rishabh()
async def switchkey_command(event):
    if not await _require_bot_owner(event):
        return
    args = _args(event)
    target = _key_target(args)
    if target is None:
        await event.reply("Usage: .switchkey [gemini|openrouter|claude] <number>")
        return
    provider, index = target
    if switch_provider_key(provider, index):
        await event.reply(f"✅ {provider.title()} key {index} will be tried first.")
    else:
        await event.reply("⚠️ Invalid key number.")
