"""
sraid.py — Target sequence reply plugin

Commands:
  .sraid [reply | @username | userid]      -> user target list me add (silent)
  .sraidadd @username|id <word>            -> word add (FIRST word = trigger signal)
  .dsraid [reply | @username | userid]     -> target remove + auto-delete confirm
  .sraidinfo                               -> saare targets + trigger + words

Kaam kaise karta hai:
  Group me target user ko TAG karke uska trigger word bolo (jaise "hlo")
  -> bot turant baaki saare words ek-ek karke usi user ko tag karke bhejega.
"""

import asyncio
import json
import os
import re

from telethon import events
from telethon.tl.types import MessageEntityMention, MessageEntityMentionName

# ================= CONFIG =================
SEND_DELAY = 0.3            # har word ke beech gap (seconds)
CONFIRM_DELETE_AFTER = 5    # .dsraid/.sraidadd confirm itne sec me khud delete
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sraid_data.json")
# ==========================================

DATA = {}

CMD_RE = re.compile(r"^\.(sraidadd|sraidinfo|dsraid|sraid)(?:\s+(.*))?$", re.S)


def _load():
    global DATA
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            DATA = json.load(f)
    except Exception:
        DATA = {}


def _save():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_load()


def make_tag(info, uid):
    uname = info.get("username")
    if uname:
        return f"@{uname}"
    name = info.get("name") or "user"
    return f"[{name}](tg://user?id={uid})"


def add_user(user):
    uid = str(user.id)
    if uid not in DATA:
        DATA[uid] = {
            "name": getattr(user, "first_name", None) or getattr(user, "title", None) or "user",
            "username": getattr(user, "username", None),
            "trigger": None,
            "words": [],
        }
        _save()


async def _resolve_user(client, event, arg):
    """arg, reply ya tag — kahin se bhi user nikalo."""
    if arg:
        try:
            clean = arg.lstrip("@")
            return await client.get_entity(int(clean) if clean.isdigit() else clean)
        except Exception:
            return None
    if event.is_reply:
        try:
            r = await event.get_reply_message()
            return await r.get_sender()
        except Exception:
            return None
    try:
        users = await event.message.get_mentioned_users()
        if users:
            return users[0]
    except Exception:
        pass
    return None


# ---------------- MAIN HANDLER ----------------

async def dispatch(event):
    m = CMD_RE.match(event.raw_text or "")
    if not m:
        await handle_trigger(event)
        return
    cmd, arg = m.group(1), (m.group(2) or "").strip()
    client = event.client
    if cmd == "sraid":
        await cmd_sraid(client, event, arg)
    elif cmd == "sraidadd":
        await cmd_sraidadd(client, event, arg)
    elif cmd == "dsraid":
        await cmd_dsraid(client, event, arg)
    elif cmd == "sraidinfo":
        await cmd_sraidinfo(client, event)


# ---------------- COMMANDS ----------------

async def cmd_sraid(client, event, arg):
    user = await _resolve_user(client, event, arg)
    if user is None:
        return await event.edit(
            "**.sraid:** user nahi mila. Reply karo, usko tag karo, "
            "ya `.sraid @username/id` bhejo."
        )
    add_user(user)
    info = DATA[str(user.id)]
    try:
        await event.delete()   # silent add — command gayab
    except Exception:
        pass
    if event.is_group:
        # group me bhi kuch na dikhe — chhota confirm jo khud delete ho jaye
        msg = await event.respond(
            f"✅ **Target added:** {info['name']}"
            + (f" (ab `.sraidadd {arg or user.id} <word>` se words add karo)" if not info["trigger"] else "")
        )
        await asyncio.sleep(CONFIRM_DELETE_AFTER)
        try:
            await msg.delete()
        except Exception:
            pass


async def cmd_sraidadd(client, event, arg):
    parts = arg.split(None, 1)
    if len(parts) < 2:
        return await event.edit("**Usage:** `.sraidadd @username/id <word>`")
    uarg, word = parts[0], parts[1].strip()
    user = await _resolve_user(client, event, uarg)
    if user is None:
        return await event.edit("**.sraidadd:** user resolve nahi hua. @username ya numeric id use karo.")
    add_user(user)
    info = DATA[str(user.id)]
    if info["trigger"] is None:
        info["trigger"] = word          # FIRST word = sirf trigger signal, ye send nahi hota
        text = f"🎯 **Sraid target ready:** {info['name']} | Trigger: `{word}`\nAb aage ke words add karte jao."
    else:
        info["words"].append(word)
        text = f"➕ **Word added ({len(info['words'])}):** `{word}`"
    _save()
    try:
        await event.delete()            # command silent
    except Exception:
        pass
    msg = await event.respond(text)
    await asyncio.sleep(CONFIRM_DELETE_AFTER)
    try:
        await msg.delete()
    except Exception:
        pass


async def cmd_dsraid(client, event, arg):
    user = await _resolve_user(client, event, arg)
    if user is None:
        return
    uid = str(user.id)
    name = DATA.get(uid, {}).get("name", getattr(user, "first_name", "user"))
    DATA.pop(uid, None)                 # trigger, words, sab kuch — full refresh
    _save()
    try:
        await event.delete()
    except Exception:
        pass
    if event.is_group:
        msg = await event.respond(f"🗑️ **Target removed:** {name} — saari config reset ho gayi.")
        await asyncio.sleep(CONFIRM_DELETE_AFTER)
        try:
            await msg.delete()
        except Exception:
            pass


async def cmd_sraidinfo(client, event):
    if not DATA:
        return await event.edit("**Sraid list khali hai.**")
    lines = ["**🎯 Sraid Targets:**\n"]
    for i, (uid, info) in enumerate(DATA.items(), 1):
        lines.append(f"**{i}.** {info.get('name')} (`{uid}`)")
        lines.append(f"   • Trigger: `{info.get('trigger') or 'set nahi hua'}`")
        if info.get("words"):
            lines.append("   • Words: " + "  ".join(f"`{w}`" for w in info["words"]))
        else:
            lines.append("   • Words: _koi nahi (.sraidadd se add karo)_")
        lines.append("")
    await event.edit("\n".join(lines))


# ---------------- TRIGGER LOGIC ----------------

async def fire_sequence(client, chat_id, uid, reply_to=None):
    info = DATA.get(uid)
    if not info or not info.get("words"):
        return

    if reply_to:
        # Reply-trigger: har configured word usi original target message par reply hoga.
        for word in info["words"]:
            await client.send_message(chat_id, word, reply_to=reply_to)
            await asyncio.sleep(SEND_DELAY)
    else:
        # @username trigger: existing mention behavior.
        tag = make_tag(info, uid)
        for word in info["words"]:
            await client.send_message(chat_id, f"{tag} {word}")
            await asyncio.sleep(SEND_DELAY)


async def handle_trigger(event):
    if not event.is_group:
        return

    msg = event.message
    text = msg.raw_text or ""

    # Target can be identified either by:
    # 1) replying to the target's message, or
    # 2) mentioning/tagging the target with @username / Telegram mention.
    target_ids = set()

    # Reply-based target detection
    if event.is_reply:
        try:
            reply_msg = await event.get_reply_message()
            sender = await reply_msg.get_sender()
            if sender and getattr(sender, "id", None):
                target_ids.add(str(sender.id))
        except Exception:
            pass

    # Mention-based target detection
    try:
        users = await msg.get_mentioned_users()
        for user in users or []:
            if getattr(user, "id", None):
                target_ids.add(str(user.id))
    except Exception:
        pass

    if not target_ids:
        return

    # Remove the @mention text from the message before checking the trigger.
    leftover = text

    for entity in sorted(msg.entities or [], key=lambda e: -e.offset):
        if isinstance(entity, (MessageEntityMention, MessageEntityMentionName)):
            leftover = leftover[:entity.offset] + leftover[entity.offset + entity.length:]

    leftover = " ".join(leftover.split()).strip().lower()

    # A trigger starts only when the remaining message is exactly the
    # configured trigger for the identified target.
    for uid in target_ids:
        info = DATA.get(uid)
        if info and info.get("trigger") and leftover == str(info["trigger"]).strip().lower():
            reply_to = None
            if event.is_reply:
                try:
                    reply_msg = await event.get_reply_message()
                    sender = await reply_msg.get_sender()
                    if sender and str(getattr(sender, "id", "")) == uid:
                        reply_to = reply_msg.id
                except Exception:
                    pass

            await fire_sequence(
                event.client,
                event.chat_id,
                uid,
                reply_to=reply_to,
            )
            break


# ---------------- REGISTRATION ----------------

def init(client_instance):
    from plugins.bot import add_handler

    commands = [
        ".sraid [reply | @username | userid] - user ko target list me add karo",
        ".sraidadd @username|id <word> - trigger/word add karo",
        ".dsraid [reply | @username | userid] - target remove karo",
        ".sraidinfo - targets, trigger aur words dekho",
    ]
    description = "SRAID target sequence reply plugin"
    add_handler("sraid", commands, description)

    build_handlers(client_instance)


def build_handlers(client):
    client.add_event_handler(dispatch, events.NewMessage(outgoing=True))


def _find_client():
    """Common userbot frameworks se client auto-detect karo."""
    import sys
    for modname in ("userbot", "catuserbot", "legendbot", "uniborg",
                    "fridaybot", "paperplane", "telethon"):
        mod = sys.modules.get(modname)
        if mod:
            for attr in ("bot", "tgbot", "client", "borg", "friday", "kitsune"):
                c = getattr(mod, attr, None)
                if c and hasattr(c, "add_event_handler"):
                    return c
    return None




# Standalone test ke liye:  python3 sraid.py  (API_ID / API_HASH env me set karke)
if __name__ == "__main__":
    from telethon import TelegramClient
    client = TelegramClient("sraid_session", int(os.environ["API_ID"]), os.environ["API_HASH"])
    build_handlers(client)
    print("sraid plugin active...")
    client.run_until_disconnected()
