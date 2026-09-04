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