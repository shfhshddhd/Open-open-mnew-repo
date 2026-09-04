# =============================================================================
#  FLEX FUCKER USERBOT Tohid Command
#  Author:         FLEX FUCKER USERBOT Dev ()
# =============================================================================

from telethon import events
from utils.utils import CipherElite
from plugins.bot import add_handler
from utils.decorators import rishabh

VERSION = "1.0.0"
CATEGORY = "utilities"


def init(client_instance):
    commands = [
        ".tohid - Replies with the owner's Telegram username"
    ]
    description = "👤 Shows the owner's Telegram username"
    add_handler("tohid", commands, description)


async def register_commands():
    @CipherElite.on(events.NewMessage(pattern=r"^\.tohid$", outgoing=True))
    @rishabh()
    async def tohid_cmd(event):
        await event.reply("@SAREEF_FUCKER")
