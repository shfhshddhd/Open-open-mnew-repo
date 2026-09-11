"""Private control-bot handlers for the hosted account's Voice Chat."""

from __future__ import annotations

import html
import re
import shutil
import tempfile
from pathlib import Path

from pytgcalls.exceptions import NoActiveGroupCall
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
import database.mongo as db
from plugins.private_vc_bridge import MAX_BASS, MAX_LEVEL, MIN_BASS, MIN_LEVEL
from utils.message_ui import reply_html


_VOICE_COMMAND_RE = re.compile(
    r"^\s*\.(?P<command>"
    r"vcjoin|vcstatus|vcstop|vcleave|play|pause|resume|queue|clearqueue|"
    r"volume|mute|unmute"
    r")"
    r"(?:\s+(?P<args>.*?))?\s*$",
    re.IGNORECASE,
)


def _voice_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve only the hosted client bound to this private control chat."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return None
    if message.chat is None or message.chat.type != "private":
        return None

    manager = context.bot_data.get("manager")
    hosted = manager.get_client(user.id) if manager is not None else None
    if hosted is None or not hosted.is_running():
        return None
    return getattr(hosted.client, "_voice_chat_manager", None)


def _command(message) -> tuple[str, str] | None:
    match = _VOICE_COMMAND_RE.match(message.text or "")
    if match is None:
        return None
    return match.group("command").lower(), (match.group("args") or "").strip()


def _reply_media(message):
    reply = message.reply_to_message
    if reply is None:
        return None
    for attribute in ("audio", "voice", "video", "document"):
        media = getattr(reply, attribute, None)
        if media is None:
            continue
        if attribute == "document" and not str(
            getattr(media, "mime_type", "") or ""
        ).startswith("audio/"):
            continue
        return media, reply
    return None


async def _download_reply_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Path, str, str]:
    message = update.effective_message
    media_info = _reply_media(message)
    if media_info is None:
        raise ValueError(
            "Reply to an audio, voice, or video message with .play."
        )

    media, reply = media_info
    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise ValueError("The replied media has no downloadable audio file.")

    filename = (
        getattr(media, "file_name", None)
        or getattr(media, "title", None)
        or f"voice-chat-{reply.message_id}.audio"
    )
    filename = Path(str(filename)).name or f"voice-chat-{reply.message_id}.audio"
    temp_dir = Path(tempfile.mkdtemp(prefix="control-vc-"))
    destination = temp_dir / filename
    try:
        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=destination)
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("Telegram returned an empty audio file.")
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    title = (
        getattr(media, "title", None)
        or getattr(media, "file_name", None)
        or "Telegram audio"
    )
    return destination, str(title), f"control-bot-message:{reply.message_id}"


async def _reply_error(message, exc: Exception) -> None:
    if isinstance(exc, NoActiveGroupCall):
        text = "❌ No active Voice Chat found."
    else:
        text = f"❌ {html.escape(str(exc))}"
    await reply_html(message, text)


async def voice_chat_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    parsed = _command(message)
    if parsed is None:
        return

    voice = _voice_manager(update, context)
    # Ignore group commands and senders without an authorized hosted account.
    if voice is None:
        return

    command, args = parsed
    try:
        if command == "vcjoin":
            text = await voice.join_target(args)
        elif command == "vcstatus":
            text = await voice.control_status()
        elif command == "vcstop":
            if voice.state is None:
                text = "❌ Not connected to any Voice Chat."
            else:
                text = await voice.stop(voice.state.chat_id)
        elif command == "vcleave":
            if voice.state is None:
                text = "❌ Not connected to any Voice Chat."
            else:
                text = await voice.leave(voice.state.chat_id)
        elif command == "play":
            if args:
                raise ValueError(
                    "Reply to an audio, voice, or video message with .play."
                )
            path, title, source = await _download_reply_audio(update, context)
            try:
                async def notify_playback_complete() -> None:
                    await context.bot.send_message(
                        chat_id=message.chat_id,
                        text=f"✅ Playback finished: {title}",
                    )

                text = await voice.enqueue_file(
                    path,
                    title,
                    source,
                    on_complete=notify_playback_complete,
                )
            finally:
                shutil.rmtree(path.parent, ignore_errors=True)
        elif command == "pause":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            text = await voice.pause(voice.state.chat_id)
        elif command == "resume":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            text = await voice.resume(voice.state.chat_id)
        elif command == "queue":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            text = await voice.queue_text(voice.state.chat_id)
        elif command == "clearqueue":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            text = await voice.clear_queue(voice.state.chat_id)
        elif command == "volume":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            try:
                value = int(args)
            except ValueError as exc:
                raise ValueError("Usage: .volume <0-100000000>") from exc
            text = await voice.change_volume(voice.state.chat_id, value)
        elif command == "mute":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            text = await voice.mute(voice.state.chat_id)
        elif command == "unmute":
            if voice.state is None:
                raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
            text = await voice.unmute(voice.state.chat_id)
        else:  # pragma: no cover - guarded by the command regex
            return
        await reply_html(message, text)
    except Exception as exc:
        await _reply_error(message, exc)


def build_voice_chat_handler() -> MessageHandler:
    """Match only dot commands sent to the control bot in private chats."""
    return MessageHandler(
        filters.ChatType.PRIVATE
        & filters.TEXT
        & filters.Regex(_VOICE_COMMAND_RE),
        voice_chat_command,
    )

_PRIVATE_VC_CONTROL_RE = re.compile(
    r"^\s*/(?P<command>"
    r"join|leave|leaveall|leaveplay|level|bass|mute|unmute|startrecord|stoprecord|speedtest"
    r")"
    r"(?:\s+(?P<args>.*?))?\s*$",
    re.IGNORECASE,
)


async def _allowed_control_sender(message, registry_entry) -> bool:
    """Only the private group's owner account or its group admins may control."""
    if message is None or registry_entry is None:
        return False
    chat_id = message.chat_id
    control_chat_id = int(registry_entry.get("control_chat_id", 0))
    if chat_id != control_chat_id:
        return False
    user_id = int(message.from_user.id if message.from_user is not None else 0)
    owner_user_id = int(registry_entry.get("user_id", 0))
    if user_id == owner_user_id:
        return True
    sender_chat_member = await message.chat.get_member(user_id)
    return bool(
        getattr(sender_chat_member, "status", "")
        in {"administrator", "creator"}
    )


async def _resolve_private_vc_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return None
    if message.chat is None or message.chat.type != "group" and message.chat.type != "supergroup":
        return None
    registry_entry = await db.get_private_vc_control_by_chat(message.chat_id)
    if registry_entry is None:
        return None
    if not await _allowed_control_sender(message, registry_entry):
        return None
    manager = context.bot_data.get("manager")
    hosted = manager.get_client(int(registry_entry["user_id"])) if manager is not None else None
    if hosted is None or not hosted.is_running():
        return None
    bridge = getattr(hosted.client, "_vc_bridge", None)
    if bridge is None:
        return None
    voice = hosted.client._voice_chat_manager
    if voice is None:
        return None
    return bridge, voice, int(registry_entry["user_id"])


async def _parse_int_arg(args, command: str, minimum: int, maximum: int) -> int:
    try:
        value = int(args)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Usage: /{command} <{minimum}-{maximum}>") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{command.capitalize()} must be between {minimum} and {maximum}.")
    return value


async def private_vc_control_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    parsed = _PRIVATE_VC_CONTROL_RE.match(message.text or "")
    if parsed is None:
        return

    resolved = await _resolve_private_vc_manager(update, context)
    if resolved is None:
        return
    bridge, voice, hosted_user_id = resolved
    command = parsed.group("command").lower()
    args = (parsed.group("args") or "").strip()
    source_chat_id = voice.state.chat_id if voice.state is not None else None
    try:
        if command == "join":
            if source_chat_id is None:
                raise ValueError("Join a source Voice Chat first with .vcjoin (or .play).")
            text = await bridge.join(args, source_chat_id)
        elif command == "leave":
            text = await bridge.leave(int(args))
        elif command == "leaveall":
            await bridge.leave_all()
            text = "Left all forwarding sessions."
        elif command == "leaveplay":
            if source_chat_id is not None:
                text = await voice.leave(source_chat_id)

                text = "Not connected to a source Voice Chat."
        elif command == "level":
            value = await _parse_int_arg(args, command, MIN_LEVEL, MAX_LEVEL)
            text = await bridge.set_level(args2 if len(args.split()) > 1 else None, value)



        elif command == "bass":
            value = await _parse_int_arg(args, command, MIN_BASS, MAX_BASS)
            text = await bridge.set_bass(
                args2 if len(args.split()) > 1 else None,
                value,
            )
        elif command == "mute":
            text = await bridge.set_mute(args2 if len(args.split()) > 1 else None, True)



        elif command == "unmute":
            text = await bridge.set_mute(args2 if len(args.split()) > 1 else None, False)

        elif command == "startrecord":
            text = await bridge.start_recording(
                args2 if len(args.split()) > 1 else None,
            )
        elif command == "stoprecord":
            text = await bridge.stop_recording(
                args2 if len(args.split()) > 1 else None,
                message.chat_id,
            )
        elif command == "speedtest":
            stats = bridge.speed_stats()
            text = (
                f"*VC-to-VC audio stats*\n"
                f"Sessions: `{stats['sessions']}`\n"
                f"Frames forwarded: `{stats['frames']}`\n"
                f"Bytes forwarded: `{stats['bytes']}`\n"
                f"Last frame received at: `{stats['last_frame_at']}`"
            )
        else:  # pragma: no cover - guarded by the command regex
            return

        await reply_html(message, text)
    except Exception as exc:
        await _reply_error(message, exc)


def build_private_vc_control_handler() -> MessageHandler:
    """Match the registered private control group's VC-to-VC slash commands."""
    return MessageHandler(
        filters.TEXT
        & filters.Regex(_PRIVATE_VC_CONTROL_RE),
        private_vc_control_command,
    )