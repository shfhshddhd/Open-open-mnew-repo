# This plugin is part of the FLEX FUCKER USERBOT Telegram UserBot
# Author: Rishabh (https://github.com/rishabhops)
# License: MIT License — See LICENSE file for full text

import asyncio

from telethon import events, errors
from telethon.tl import functions, types
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from plugins.bot import add_handler
from utils.utils import CipherElite
from utils.decorators import rishabh

VERSION = "1.0.0"
CATEGORY = "admin"

def init(client_instance):
    commands = [
        ".ban - Ban a user from the group",
        ".banall - Ban all normal members while preserving the owner and admins",
        ".unban - Unban a user from the group",
        ".mute - Mute a user in the group",
        ".unmute - Unmute a user in the group",
        ".promote - Promote a user to admin",
        ".demote - Remove admin rights from a user",
        ".pin - Pin a message in the group",
        ".unpin - Unpin a message in the group"
    ]
    description = "Admin commands for group management 👮‍♂️"
    add_handler("admin", commands, description)

async def _protected_member_ids(event) -> set[int]:
    """Collect the hosted owner and current group admins before bulk bans."""
    protected: set[int] = set()
    me = await event.client.get_me()
    if me is not None:
        protected.add(int(me.id))

    entity = await event.client.get_entity(event.chat_id)
    if isinstance(entity, types.Channel):
        offset = 0
        while True:
            result = await event.client(
                functions.channels.GetParticipantsRequest(
                    channel=entity,
                    filter=types.ChannelParticipantsAdmins(),
                    offset=offset,
                    limit=100,
                    hash=0,
                )
            )
            protected.update(int(user.id) for user in result.users)
            count = len(result.participants)
            if count < 100:
                break
            offset += count
    elif isinstance(entity, types.Chat):
        full = await event.client(functions.messages.GetFullChatRequest(chat_id=entity.id))
        participants = getattr(full.full_chat.participants, "participants", ())
        protected.update(
            int(participant.user_id)
            for participant in participants
            if isinstance(
                participant,
                (types.ChatParticipantAdmin, types.ChatParticipantCreator),
            )
        )
    return protected


async def _banall(event):
    if not event.is_group:
        await event.reply("❌ .banall can only be used inside a group or supergroup.")
        return

    try:
        protected = await _protected_member_ids(event)
        scanned = banned = skipped = failed = 0
        async for member in event.client.iter_participants(event.chat_id):
            scanned += 1
            member_id = int(member.id)
            if member_id in protected:
                skipped += 1
                continue
            try:
                await event.client(
                    EditBannedRequest(
                        event.chat_id,
                        member_id,
                        ChatBannedRights(until_date=None, view_messages=True),
                    )
                )
                banned += 1
            except errors.FloodWaitError as flood:
                wait_seconds = max(0, int(flood.seconds))
                await event.reply(
                    f"⏸️ Telegram flood protection: waiting {wait_seconds}s before continuing..."
                )
                await asyncio.sleep(wait_seconds)
                try:
                    await event.client(
                        EditBannedRequest(
                            event.chat_id,
                            member_id,
                            ChatBannedRights(until_date=None, view_messages=True),
                        )
                    )
                    banned += 1
                except Exception:
                    failed += 1
            except (errors.ChatAdminRequiredError, errors.UserAdminInvalidError):
                failed += 1
            except Exception:
                failed += 1

        await event.reply(
            f"✅ Ban All complete. Scanned: {scanned} | Banned: {banned} | "
            f"Protected: {skipped} | Failed: {failed}"
        )
    except Exception:
        await event.reply(
            "❌ Ban All failed. Make sure this account is an admin with ban-users permission."
        )

async def register_commands():
    @CipherElite.on(events.NewMessage(pattern=r"(?i)^\.banall$"))
    @rishabh()
    async def ban_all(event):
        await _banall(event)

    @CipherElite.on(events.NewMessage(pattern=r"\.ban"))
    @rishabh()
    async def ban(event):
        if event.is_reply:
            reply = await event.get_reply_message()
            try:
                await event.client(EditBannedRequest(
                    event.chat_id,
                    reply.sender_id,
                    ChatBannedRights(until_date=None, view_messages=True)
                ))
                await event.reply("🚫 User has been banned!")
            except:
                await event.reply("❌ Failed to ban user. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.unban"))
    @rishabh()
    async def unban(event):
        if event.is_reply:
            reply = await event.get_reply_message()
            try:
                await event.client(EditBannedRequest(
                    event.chat_id,
                    reply.sender_id,
                    ChatBannedRights(until_date=None, view_messages=False)
                ))
                await event.reply("✅ User has been unbanned!")
            except:
                await event.reply("❌ Failed to unban user. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.mute"))
    @rishabh()
    async def mute(event):
        if event.is_reply:
            reply = await event.get_reply_message()
            try:
                await event.client(EditBannedRequest(
                    event.chat_id,
                    reply.sender_id,
                    ChatBannedRights(until_date=None, send_messages=True)
                ))
                await event.reply("🤐 User has been muted!")
            except:
                await event.reply("❌ Failed to mute user. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.unmute"))
    @rishabh()
    async def unmute(event):
        if event.is_reply:
            reply = await event.get_reply_message()
            try:
                await event.client(EditBannedRequest(
                    event.chat_id,
                    reply.sender_id,
                    ChatBannedRights(until_date=None, send_messages=False)
                ))
                await event.reply("🔊 User has been unmuted!")
            except:
                await event.reply("❌ Failed to unmute user. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.promote"))
    @rishabh()
    async def promote(event):
        if event.is_reply:
            reply = await event.get_reply_message()
            try:
                await event.client.edit_admin(
                    event.chat_id,
                    reply.sender_id,
                    is_admin=True,
                    title="Admin"
                )
                await event.reply("👑 User has been promoted to admin!")
            except:
                await event.reply("❌ Failed to promote user. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.demote"))
    @rishabh()
    async def demote(event):
        if event.is_reply:
            reply = await event.get_reply_message()
            try:
                await event.client.edit_admin(
                    event.chat_id,
                    reply.sender_id,
                    is_admin=False,
                    title=None
                )
                await event.reply("⬇️ User has been demoted!")
            except:
                await event.reply("❌ Failed to demote user. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.pin"))
    @rishabh()
    async def pin_message(event):
        if event.is_reply:
            try:
                await event.client.pin_message(
                    event.chat_id,
                    (await event.get_reply_message()).id,
                    notify=True
                )
                await event.reply("📌 Message pinned successfully!")
            except:
                await event.reply("❌ Failed to pin message. Make sure you have the right permissions!")

    @CipherElite.on(events.NewMessage(pattern=r"\.unpin"))
    @rishabh()
    async def unpin_message(event):
        if event.is_reply:
            try:
                await event.client.unpin_message(
                    event.chat_id,
                    (await event.get_reply_message()).id
                )
                await event.reply("📍 Message unpinned successfully!")
            except:
                await event.reply("❌ Failed to unpin message. Make sure you have the right permissions!")
                
