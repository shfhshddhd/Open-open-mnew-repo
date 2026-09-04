"""
/host and /unhost command handlers.

/host flow:
  1. Ask for phone number  → PHONE
  2. Send OTP              → OTP
  3. [Optional] 2FA pass   → PASSWORD
  4. Done
"""
import logging
import re
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from telegram import Update
from telegram.ext import (
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from utils.message_ui import reply_html, reply_text

logger = logging.getLogger(__name__)

PHONE, OTP, PASSWORD = range(3)

# Temporary auth state keyed by bot user_id
# {user_id: {"client": TelegramClient, "phone": str, "phone_code_hash": str}}
_pending: dict[int, dict] = {}

HOST_SUCCESS_TEXT = (
    "🎉 <b>Your account has been hosted successfully!</b>\n\n"
    "✅ Your userbot is now active.\n\n"
    "📚 <b>Start with these commands from your hosted account:</b>\n"
    "<code>.help</code> — show all loaded plugins and commands\n"
    "<code>.help &lt;plugin&gt;</code> — show one plugin's commands\n"
    "<code>.plugins</code> — list all plugin modules\n"
    "<code>.findplugin &lt;word&gt;</code> — search plugins\n"
    "<code>.helpstats</code> — show plugin statistics\n"
    "<code>.alive</code> / <code>.ping</code> — check userbot status\n\n"
    "🎯 To configure the bridge, return here and use "
    "<code>/targetadd &lt;group_chat_id&gt; &lt;@username_or_user_id&gt;</code>."
)


# ── /host ──────────────────────────────────────────────────────────────────────

async def host_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]

    if manager.is_hosted(user_id):
        await reply_text(
            update.message,
            "✅ You already have an active hosted account.\n"
            "Use /unhost first if you want to replace it."
        )
        return ConversationHandler.END

    await reply_text(
        update.message,
        "📱 Please send your phone number in international format.\n"
        "Example: +14155552671\n\n"
        "Send /cancel at any time to abort."
    )
    return PHONE


async def host_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    raw_phone = update.message.text.strip()
    digits = re.sub(r"\D", "", raw_phone)
    if raw_phone.startswith("+"):
        phone = f"+{digits}"
    elif raw_phone.startswith("00"):
        phone = f"+{digits[2:]}"
    else:
        phone = digits
    if not phone or phone == "+":
        await reply_text(
            update.message,
            "❌ Please send a valid phone number in international format, "
            "for example +14155552671.",
        )
        return PHONE

    await reply_text(update.message, "⏳ Sending verification code…")
    try:
        client, phone_code_hash, delivery_type = await manager.begin_auth(user_id, phone)
    except Exception as exc:
        logger.error("begin_auth failed for %s: %s", user_id, exc)
        await reply_text(update.message, f"❌ Could not send OTP: {exc}\nTry /host again.")
        return ConversationHandler.END

    _pending[user_id] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "delivery_type": delivery_type,
    }
    delivery_hint = {
        "SentCodeTypeApp": (
            "Telegram sent it inside an already logged-in Telegram app, "
            "usually in the verified Telegram service chat—not by SMS."
        ),
        "SentCodeTypeSms": "Telegram sent it by SMS to this phone number.",
        "SentCodeTypeCall": "Telegram sent it by an automated phone call.",
        "SentCodeTypeFlashCall": "Telegram sent it through a verification call.",
        "SentCodeTypeMissedCall": "Telegram sent it through a missed-call verification.",
    }.get(
        delivery_type,
        "Check your Telegram app, verified Telegram service chat, SMS, and calls.",
    )
    await reply_text(
        update.message,
        f"✉️ Telegram requested your verification code.\n{delivery_hint}\n"
        "Please enter your OTP with spaces between each digit.\n"
        "Example: 1 2 3 4 5."
    )
    return OTP


async def host_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    otp = re.sub(r"\D", "", update.message.text)

    pending = _pending.get(user_id)
    if not pending:
        await reply_text(update.message, "❌ Session expired. Please start over with /host.")
        return ConversationHandler.END

    try:
        await manager.sign_in_with_code(
            user_id=user_id,
            client=pending["client"],
            phone=pending["phone"],
            phone_code_hash=pending["phone_code_hash"],
            otp=otp,
        )
        _pending.pop(user_id, None)
        await reply_html(update.message, HOST_SUCCESS_TEXT)
        return ConversationHandler.END

    except SessionPasswordNeededError:
        # Keep the client alive in _pending so we can use it for 2FA
        await reply_text(
            update.message,
            "🔒 Your account has Two-Step Verification enabled.\n"
            "Please enter your 2FA password:"
        )
        return PASSWORD

    except PhoneCodeInvalidError:
        await reply_text(update.message, "❌ Invalid code. Please try again:")
        return OTP

    except Exception as exc:
        logger.exception("sign_in error for %s: %s", user_id, exc)
        _pending.pop(user_id, None)
        await reply_text(update.message, f"❌ Authentication failed: {exc}\nTry /host again.")
        return ConversationHandler.END


async def host_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]
    password = update.message.text.strip()

    pending = _pending.get(user_id)
    if not pending:
        await reply_text(update.message, "❌ Session expired. Please start over with /host.")
        return ConversationHandler.END

    try:
        await manager.sign_in_with_password(
            user_id=user_id,
            client=pending["client"],
            password=password,
        )
        _pending.pop(user_id, None)
        await reply_html(update.message, HOST_SUCCESS_TEXT)
    except Exception as exc:
        logger.exception("2FA error for %s: %s", user_id, exc)
        _pending.pop(user_id, None)
        await reply_text(update.message, f"❌ 2FA failed: {exc}\nTry /host again.")

    return ConversationHandler.END


async def host_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    pending = _pending.pop(user_id, None)
    if pending:
        try:
            await pending["client"].disconnect()
        except Exception:
            pass
    await reply_text(update.message, "❎ /host cancelled.")
    return ConversationHandler.END


def build_host_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("host", host_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, host_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, host_otp)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, host_password)],
        },
        fallbacks=[CommandHandler("cancel", host_cancel)],
        allow_reentry=True,
    )


# ── /unhost ────────────────────────────────────────────────────────────────────

async def unhost_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    manager = ctx.bot_data["manager"]

    if not manager.is_hosted(user_id):
        await reply_text(update.message, "ℹ️ You don't have an active hosted account.")
        return

    await manager.remove_session(user_id)
    await reply_text(
        update.message,
        "🗑️ Your hosted account has been removed.\n"
        "Session deleted, userbot stopped, all data cleared."
    )
