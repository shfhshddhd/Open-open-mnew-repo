"""Persistent bridge monitoring controls."""
from telegram import Update
from telegram.ext import ContextTypes
import database.mongo as db
from utils.message_ui import reply_text


async def boton_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable bridge monitoring without changing saved mappings."""
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    await db.set_setting(user_id, "bot_enabled", True)
    userbot = manager.get_client(user_id)
    mappings = await db.get_target_mappings(user_id)
    if userbot is not None and mappings:
        await userbot.enable_monitoring()
    await reply_text(
        update.message,
        f"🟢 Bot is on. Monitoring {len(mappings)} target mapping(s)."
        if mappings
        else "🟢 Bot is on. Add a mapping with /targetadd to start monitoring."
    )


async def botoff_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable bridge monitoring while keeping saved mappings intact."""
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    await db.set_setting(user_id, "bot_enabled", False)
    userbot = manager.get_client(user_id)
    if userbot is not None:
        await userbot.disable_monitoring()
    else:
        await db.clear_monitoring_data(user_id)
    await reply_text(
        update.message,
        "🔴 Bot is off. Your mappings are safe and will resume after /boton."
    )