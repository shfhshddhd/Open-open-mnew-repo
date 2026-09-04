"""Owner-only Gemini key management for the control bot."""

from telegram import Update
from telegram.ext import ContextTypes

import config
from utils.gemini_rotation import add_key
from utils.key_manager import (
    add_provider_key,
    provider_status,
    remove_provider_key,
    switch_provider_key,
)
from utils.message_ui import reply_text

ACCESS_DENIED = "⛔ Aapko is command ka access nahi hai."


def _is_owner(update: Update) -> bool:
    return bool(
        update.effective_user
        and config.Config.OWNER_ID
        and update.effective_user.id == config.Config.OWNER_ID
    )


async def _require_owner(update: Update) -> bool:
    if _is_owner(update):
        return True
    if update.message:
        await reply_text(update.message, ACCESS_DENIED)
    return False


async def addkey_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        return
    if not ctx.args:
        await reply_text(update.message, "Usage: /addkey <API_KEY>")
        return
    if add_key(" ".join(ctx.args)):
        await reply_text(
            update.message,
            "✅ Gemini key saved. Saved keys have priority over GEMINI_API_KEY.",
        )
    else:
        await reply_text(update.message, "⚠️ Key is empty or already saved.")


async def listkeys_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        return
    status = provider_status()
    lines = ["🔑 Configured AI provider keys:"]
    labels = {"gemini": "Gemini", "openrouter": "OpenRouter", "anthropic": "Anthropic Claude"}
    for provider, label in labels.items():
        rows = status.get(provider, [])
        lines.append(f"\n{label}:")
        if not rows:
            lines.append("  — none configured")
            continue
        for row in rows:
            source = "environment fallback" if row["source"] == "environment" else "saved"
            lines.append(
                f"  {row['index']}. {row['masked']} ({source}; {row['cooldown_text']})"
            )
    await reply_text(update.message, "\n".join(lines))


async def addopenrouterkey_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        return
    if not ctx.args:
        await reply_text(update.message, "Usage: /addopenrouterkey <API_KEY>")
        return
    if add_provider_key("openrouter", " ".join(ctx.args)):
        await reply_text(update.message, "✅ OpenRouter key saved securely.")
    else:
        await reply_text(update.message, "⚠️ OpenRouter key is empty or already saved.")


async def addclaudekey_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        return
    if not ctx.args:
        await reply_text(update.message, "Usage: /addclaudekey <API_KEY>")
        return
    if add_provider_key("anthropic", " ".join(ctx.args)):
        await reply_text(update.message, "✅ Anthropic Claude key saved securely.")
    else:
        await reply_text(update.message, "⚠️ Claude key is empty or already saved.")


async def delkey_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        return
    target = _key_target(ctx.args)
    if target is None:
        await reply_text(
            update.message,
            "Usage: /delkey [gemini|openrouter|claude] <number>"
        )
        return
    provider, index = target
    removed, reason = remove_provider_key(provider, index)
    if removed:
        await reply_text(update.message, f"✅ {provider.title()} key {index} deleted.")
    elif reason == "fallback":
        await reply_text(
            update.message,
            "⚠️ Environment fallback keys cannot be deleted here."
        )
    else:
        await reply_text(update.message, "⚠️ Invalid key number.")


async def switchkey_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update):
        return
    target = _key_target(ctx.args)
    if target is None:
        await reply_text(
            update.message,
            "Usage: /switchkey [gemini|openrouter|claude] <number>"
        )
        return
    provider, index = target
    if switch_provider_key(provider, index):
        await reply_text(
            update.message,
            f"✅ {provider.title()} key {index} will be tried first."
        )
    else:
        await reply_text(update.message, "⚠️ Invalid key number.")


def _key_target(args: list[str]) -> tuple[str, int] | None:
    """Parse a provider-local 1-based index for slash commands."""
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