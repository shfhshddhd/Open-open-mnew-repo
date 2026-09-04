"""Permanent group-to-target mapping commands."""
from html import escape
import logging
from telegram import Update
from telegram.ext import ContextTypes
import database.mongo as db
from utils.message_ui import reply_html, reply_text

logger = logging.getLogger(__name__)
_pending_removeall: set[int] = set()

async def _resolve_group(client, chat_id: int):
    entity = await client.get_entity(chat_id)
    if getattr(entity, "broadcast", False) or not getattr(entity, "title", None):
        raise ValueError("The chat ID does not belong to a group.")
    return entity


def _parse_args(args: list[str]) -> tuple[int, str] | None:
    if len(args) != 2:
        return None
    try:
        group_chat_id = int(args[0])
    except ValueError:
        return None
    if not args[1].strip():
        return None
    return group_chat_id, args[1].strip()


async def targetadd_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    parsed = _parse_args(ctx.args or [])
    if parsed is None:
        await reply_text(
            update.message,
            "Usage: /targetadd <group_chat_id> <@username_or_user_id>\n"
            "Example: /targetadd -1001234567890 @username"
        )
        return
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    group_chat_id, identifier = parsed
    userbot = manager.get_client(user_id)
    try:
        group = await _resolve_group(userbot.client, group_chat_id)
    except Exception as exc:
        logger.warning(
            "Target mapping group validation failed: user_id=%s group_chat_id=%s reason=%s",
            user_id,
            group_chat_id,
            exc,
        )
        await reply_html(
            update.message,
            f"❌ Could not validate group <code>{group_chat_id}</code>.\n"
            "Make sure the hosted account is a member of that group.",
        )
        return

    await reply_text(update.message, f"🔍 Validating target {identifier}…")
    target = await manager.resolve_target(user_id, identifier)
    if target is None:
        await reply_html(
            update.message,
            f"❌ Could not validate user <code>{identifier}</code>.\n"
            "Use a valid @username or Telegram user ID visible to the hosted account.",
        )
        return

    created = await db.upsert_target_mapping(
        user_id=user_id,
        group_chat_id=group_chat_id,
        target=target,
        group_title=getattr(group, "title", None) or str(group_chat_id),
    )
    bot_enabled = await db.get_setting(user_id, "bot_enabled", True)
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
    await reply_html(
        update.message,
        f"✅ Mapping {action}.\n\n"
        f"Group: <b>{getattr(group, 'title', None) or group_chat_id}</b>\n"
        f"Group ID: <code>{group_chat_id}</code>\n"
        f"Target: <b>{target['name']}</b>\n"
        f"Target ID: <code>{target['target_id']}</code>\n\n"
        f"{status_text}\n\n"
        "Messages from this target in this group will be copied to Saved Messages.",
    )


async def targetremove_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    parsed = _parse_args(ctx.args or [])
    if parsed is None:
        await reply_text(
            update.message,
            "Usage: /targetremove <group_chat_id> <@username_or_user_id>\n"
            "Example: /targetremove -1001234567890 @username"
        )
        return
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    group_chat_id, identifier = parsed
    mapping = await db.get_target_mapping_by_identifier(
        user_id, group_chat_id, identifier
    )
    if mapping is None:
        resolved = await manager.resolve_target(user_id, identifier)
        if resolved is not None:
            mapping = await db.get_target_mapping(
                user_id, group_chat_id, int(resolved["target_id"])
            )
    if mapping is None:
        await reply_text(
            update.message,
            "❌ No matching target mapping was found for that group."
        )
        return

    target_id = int(mapping["target_user_id"])
    removed = await db.remove_target_mapping(user_id, group_chat_id, target_id)
    userbot = manager.get_client(user_id)
    if userbot is not None:
        await userbot.clear_target_monitoring(target_id, group_chat_id)
        if not await db.get_target_mappings(user_id):
            await userbot.disable_monitoring()
    if removed:
        await reply_html(
            update.message,
            f"🗑️ Removed target <code>{target_id}</code> from group "
            f"<code>{group_chat_id}</code>.",
        )
    else:
        await reply_text(update.message, "❌ The target mapping was already removed.")


async def targetlist_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List all permanent group-to-target mappings for the caller."""
    user_id = update.effective_user.id
    mappings = await db.get_target_mappings(user_id)
    if not mappings:
        await reply_text(update.message, "📋 No target mappings are configured.")
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
    await reply_html(update.message, "\n".join(lines))


async def targetremoveall_command(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Remove all permanent mappings after an explicit second confirmation."""
    user_id = update.effective_user.id
    mappings = await db.get_target_mappings(user_id)
    if not mappings:
        _pending_removeall.discard(user_id)
        await reply_text(update.message, "📋 Your target mapping list is already empty.")
        return

    if user_id not in _pending_removeall:
        _pending_removeall.add(user_id)
        await reply_html(
            update.message,
            f"⚠️ This will remove all <b>{len(mappings)}</b> target mapping(s).\n"
            "Send /targetremoveall again to confirm, or use another command to cancel.",
        )
        return

    _pending_removeall.discard(user_id)
    removed_count = await db.remove_all_target_mappings(user_id)
    userbot = ctx.bot_data["manager"].get_client(user_id)
    if userbot is not None:
        await userbot.disable_monitoring()
    else:
        await db.clear_monitoring_data(user_id)
    await reply_html(
        update.message,
        f"🗑️ Removed <b>{removed_count}</b> target mapping(s) and cleared monitoring data.",
    )
