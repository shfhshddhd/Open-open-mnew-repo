"""AI reply mode controls."""
from telegram import Update
from telegram.ext import ContextTypes
import database.mongo as db
import config
from utils.ai_provider import (
    ai_status_for_user,
    normalize_ai_provider,
    set_ai_provider,
)
from utils.key_manager import add_key, add_provider_key
from utils.message_ui import reply_text


def _status_text(status: dict) -> str:
    selected = status.get("selected")
    active_key = str(selected["index"]) if selected else "NONE"
    return (
        f"Provider: {status['provider'].title()}\n"
        f"Active API key: {active_key}\n"
        f"Available API keys: {status['active_count']}/{status['total_count']}"
    )


def _is_owner(update: Update) -> bool:
    return bool(
        update.effective_user
        and config.Config.OWNER_ID
        and update.effective_user.id == config.Config.OWNER_ID
    )


async def aimodeon_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable delayed AI replies to mentions in group chats."""
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    already_enabled = bool(await db.get_setting(user_id, "ai_mode", False))
    if already_enabled:
        status = await ai_status_for_user(user_id, enabled=True)
        await reply_text(
            update.message,
            "✅ AI Mode is already on\n"
            f"{_status_text(status)}"
        )
        return

    await db.set_setting(user_id, "ai_mode", True)
    userbot = manager.get_client(user_id)
    if userbot is not None:
        userbot.enable_ai_mode()
    status = await ai_status_for_user(user_id, enabled=True)
    await reply_text(
        update.message,
        "✅ AI Mode is on\n"
        f"{_status_text(status)}\n"
        "I will reply to group mentions after a short delay."
    )


async def aimodeoff_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable AI replies while keeping conversation memory."""
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    await db.set_setting(user_id, "ai_mode", False)
    userbot = manager.get_client(user_id)
    if userbot is not None:
        await userbot.disable_ai_mode()
    status = await ai_status_for_user(user_id, enabled=False)
    await reply_text(
        update.message,
        "🔴 AI Mode is off. Saved conversation memory is unchanged.\n"
        f"Provider remains: {status['provider'].title()}\n"
        f"Rotation: {status['rotation']}"
    )


async def aimode_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Select an AI provider and optionally add a key for that provider.

    Examples:
      /aimode gemini <API_KEY>
      /aimode openrouter <API_KEY>
      /aimode gemini
    """
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    if not manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "⚠️ You need a hosted account first. Use /host to set one up."
        )
        return

    args = [str(arg).strip() for arg in (ctx.args or []) if str(arg).strip()]
    if not args or len(args) > 2:
        await reply_text(
            update.message,
            "Usage: /aimode <gemini|openrouter> [API_KEY]\n"
            "Example: /aimode gemini YOUR_API_KEY"
        )
        return

    provider = normalize_ai_provider(args[0])
    if args[0].strip().lower() not in {"gemini", "openrouter"}:
        await reply_text(
            update.message,
            "⚠️ Choose a provider: gemini or openrouter."
        )
        return

    key_message = ""
    if len(args) == 2:
        if not _is_owner(update):
            await reply_text(update.message, "⛔ Only the bot owner can add API keys.")
            return
        added = (
            add_key(args[1])
            if provider == "gemini"
            else add_provider_key(provider, args[1])
        )
        key_message = (
            f"✅ {provider.title()} API key saved"
            if added
            else f"⚠️ {provider.title()} API key is already saved or invalid"
        )

    await set_ai_provider(user_id, provider)
    status = await ai_status_for_user(
        user_id,
        enabled=bool(await db.get_setting(user_id, "ai_mode", False)),
    )
    response = f"🤖 AI provider selected\n{_status_text(status)}"
    if key_message:
        response = f"{key_message}\n{response}"
    await reply_text(update.message, response)