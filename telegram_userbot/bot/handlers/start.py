"""Control-bot onboarding and command menu handlers."""

import config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes
from utils.message_ui import reply_html

MENU_TEXT = """👋 <b>Welcome to FLEX FUCKER USERBOT</b>

📋 <b>Main Commands</b>

<b>🔐 Account</b>
/host — Connect and host your Telegram account (OTP/2FA required)
/unhost — Remove your hosted account and stop the userbot
/cancel — Cancel an in-progress hosting operation

<b>ℹ️ General</b>
/start — Show this main menu
/help — Show this main menu
/allcommands — Show this complete command list

🧩 <b>Hosted Userbot Commands</b>
After <code>/host</code> succeeds, use these commands from your hosted account:

<code>.help</code> — all loaded plugins and command counts
<code>.help &lt;plugin&gt;</code> — detailed commands for one plugin
<code>.plugins</code> — list all plugin modules
<code>.findplugin &lt;word&gt;</code> — search for a plugin
<code>.helpstats</code> — show loaded plugin statistics
<code>.alive</code> / <code>.ping</code> — check userbot status

<b>🎙️ Voice Chat (private control bot only)</b>
<code>.vcjoin &lt;group&gt;</code> — join an active Voice Chat
<code>.vcstatus</code> — show connected group and playback status
Reply to audio/voice/video with <code>.play</code> — play it in the connected Voice Chat

The complete plugin command list is available through <code>.help</code>.
"""

ALL_COMMANDS_TEXT = """👋 <b>Welcome to FLEX FUCKER USERBOT</b>

This control bot lets you host your Telegram account as a userbot and manage its bridge, targets, and AI mode.

🚀 <b>Quick Start</b>
1️⃣ Send <code>/host</code> and complete OTP/2FA.
2️⃣ After hosting succeeds, open your hosted Telegram account.
3️⃣ Send <code>.help</code> there to see every loaded plugin.

<b>Important:</b> slash commands are for this control bot. Dot commands work from your hosted account.

📋 <b>Main Commands</b>

<b>🔐 Account</b>
/host — Connect and host your Telegram account (OTP/2FA required)
/unhost — Remove your hosted account and stop the userbot
/cancel — Cancel an in-progress hosting operation

<b>🎯 Targets and bridge</b>
/targetadd &lt;group_chat_id&gt; &lt;@username_or_user_id&gt; — Map a target to a group
/targetremove &lt;group_chat_id&gt; &lt;@username_or_user_id&gt; — Remove a mapping
/targetlist — Show all saved group-to-target mappings
/targetremoveall — Remove all mappings after confirmation
/boton — Enable monitoring and Saved Messages bridging
/botoff — Disable monitoring without deleting mappings

<b>🤖 AI mode</b>
/aimode &lt;gemini|openrouter&gt; [API_KEY] — Select provider or add its key
/aimodeon — Enable delayed AI replies to group mentions
/aimodeoff — Disable AI replies without deleting memory

<b>🔑 AI provider keys (owner only)</b>
/addkey &lt;API_KEY&gt; — Add a rotating Gemini key
/addopenrouterkey &lt;API_KEY&gt; — Add an OpenRouter key
/addclaudekey &lt;API_KEY&gt; — Add an Anthropic Claude key
/listkeys — List masked keys and cooldown status
/delkey [provider] &lt;number&gt; — Delete a saved provider key
/switchkey [provider] &lt;number&gt; — Choose the first provider key to try

Keys added with <code>/addkey</code> are tried before the <code>GEMINI_API_KEY</code>
environment fallback key.

<b>ℹ️ General</b>
/start — Show this main menu
/help — Show this main menu
/allcommands — Show this complete command list

🧩 <b>Hosted Userbot Commands</b>
After <code>/host</code> succeeds, use these commands from your hosted account:

<code>.help</code> — all loaded plugins and command counts
<code>.help &lt;plugin&gt;</code> — detailed commands for one plugin
<code>.plugins</code> — list all plugin modules
<code>.findplugin &lt;word&gt;</code> — search for a plugin
<code>.helpstats</code> — show loaded plugin statistics
<code>.alive</code> / <code>.ping</code> — check userbot status

<b>🎙️ Voice Chat (private control bot only)</b>
<code>.vcjoin &lt;group&gt;</code> — join an active Voice Chat
<code>.vcstatus</code> — show connected group and playback status
Reply to audio/voice/video with <code>.play</code> — play it in the connected Voice Chat

The complete plugin command list is available through <code>.help</code>.
"""


async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    mini_app = ctx.bot_data.get("mini_app_server")
    markup = None
    if (
        update.effective_chat is not None
        and update.effective_chat.type == "private"
        and config.mini_app_url()
        and mini_app is not None
        and mini_app.is_running
    ):
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open Live VC", web_app=WebAppInfo(config.mini_app_url()))]]
        )
    await reply_html(update.message, MENU_TEXT, reply_markup=markup)


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update.message, MENU_TEXT)


async def allcommands_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(update.message, ALL_COMMANDS_TEXT)
