# =============================================================================#  FLEX FUCKER USERBOT Couple Command
#  Author:         FLEX FUCKER USERBOT Dev ()
# =============================================================================

import random
from telethon import events
from utils.utils import CipherElite
from plugins.bot import add_handler
from utils.decorators import rishabh

VERSION = "1.0.0"
CATEGORY = "fun"

# Romantic messages database
COUPLE_MESSAGES = [
    "💕 **Romantic Moment**\n\n"
    "Your love is like a beautiful flower that blooms every day.\n\n"
    "❤️ **FLEX FUCKER USERBOT**",
    "\n\n",
    "🌹 **Love Letter**\n\n"
    "Distance means so little when someone means so much.\n\n"
    "💖 **FLEX FUCKER USERBOT**",
    "\n\n",
    "🌟 **Sweet Thought**\n\n"
    "Just wanted to remind you that you're amazing.\n\n"
    "💕 **FLEX FUCKER USERBOT**",
    "\n\n",
    "💝 **Valentine**\n\n"
    "You're the best thing that's ever happened to me.\n\n"
    "💘 **FLEX FUCKER USERBOT**",
    "\n\n",
    "🌸 **Flower Power**\n\n"
    "Roses are red, violets are blue,\n"
    "You're the one I'm thinking of.\n\n"
    "💝 **FLEX FUCKER USERBOT**",
]

def init(client_instance):
    commands = [
        ".couple <@username_or_userid> - Send a romantic message"
    ]
    description = "💘 Send a romantic message to someone"
    add_handler("couple", commands, description)

async def register_commands():
    @CipherElite.on(events.NewMessage(pattern=r"^\.couple(?: |$)([\s\S]*)"))
    @rishabh()
    async def couple_cmd(event):
        target = event.pattern_match.group(1).strip()
        
        if not target:
            await event.reply(
                "❌ **Usage:** `.couple <@username_or_userid>`\n\n"
                "Example: `.couple @username` or `.couple 123456789`"
            )
            return
        
        # Send a random romantic message
        message = random.choice(COUPLE_MESSAGES)
        
        # Try to resolve the target and send the message
        try:
            # Parse the target
            if target.startswith("@"):
                target_user = await event.client.get_entity(target)
            else:
                target_user = await event.client.get_entity(int(target))
            
            # Send the message to the target
            await event.client.send_message(target_user, message)
            
            # Confirm to the user
            await event.reply(
                f"✅ **Romantic message sent!**\n\n"
                f"📤 Sent to: {target_user.first_name or target_user.username}\n"
                f"💌 Message sent successfully!"
            )
        except Exception as e:
            await event.reply(
                f"❌ **Error:** Could not send message to `{target}`.\n"
                f"💡 **Make sure the username or user ID is correct.**\n"
                f"🔧 **Error:** {str(e)[:100]}"
            )
