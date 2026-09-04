"""Shared, deliberately small presentation layer for command replies.

This module only changes how text is displayed. It does not inspect or mutate
command state, arguments, provider settings, or database values.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any


_LEADING_MARKER = re.compile(
    r"^\s*(?:"
    r"[\u2000-\u3300\U0001F000-\U0001FAFF]|"
    r"<(?:b|strong|i|em|code|pre|blockquote|tg-spoiler)\b[^>]*>|"
    r"\*\*|__|`"
    r")"
)
_PLAIN_CAPS = re.compile(r"^[^<`*_]*[A-Z][A-Z0-9 &:/!?.,()'’+\-]*$")
_ACRONYMS = {
    "AI",
    "API",
    "AFK",
    "OTP",
    "ID",
    "URL",
    "GIF",
    "HTML",
    "JSON",
    "OpenAI",
}
_SMALL_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _title_case_display_line(line: str) -> str:
    """Convert a simple all-caps display line without touching markup/data."""
    match = re.match(r"^(\s*(?:[^\w\s<`*_]+\s*)?)(.*)$", line)
    if not match:
        return line
    prefix, body = match.groups()
    if not _PLAIN_CAPS.fullmatch(body.strip()) or len(re.findall(r"[A-Z]", body)) < 2:
        return line

    words = body.strip().split()
    rendered: list[str] = []
    for index, word in enumerate(words):
        clean = word.strip(".,!?():;")
        punctuation = word[len(clean) :]
        if clean in _ACRONYMS:
            value = clean
        elif index and clean.lower() in _SMALL_WORDS:
            value = clean.lower()
        else:
            value = clean[:1].upper() + clean[1:].lower()
        rendered.append(value + punctuation)
    return prefix + " ".join(rendered)


def format_reply_text(text: Any) -> Any:
    """Apply conservative readability cleanup to a textual reply."""
    if not isinstance(text, str) or not text.strip():
        return text

    lines = [_title_case_display_line(line.rstrip()) for line in text.strip().splitlines()]
    cleaned: list[str] = []
    blank_seen = False
    for line in lines:
        if not line:
            if not blank_seen:
                cleaned.append(line)
            blank_seen = True
        else:
            cleaned.append(line)
            blank_seen = False

    result = "\n".join(cleaned).strip()
    if result and not _LEADING_MARKER.match(result):
        result = f"ℹ️ {result}"
    return result


async def reply_text(message: Any, text: Any, *args: Any, **kwargs: Any) -> Any:
    """Styled wrapper for python-telegram-bot text replies."""
    return await message.reply_text(format_reply_text(text), *args, **kwargs)


async def reply_html(message: Any, text: Any, *args: Any, **kwargs: Any) -> Any:
    """Styled wrapper for python-telegram-bot HTML replies."""
    kwargs.setdefault("parse_mode", "HTML")
    return await reply_text(message, text, *args, **kwargs)


def install_telethon_reply_style() -> None:
    """Style replies from every loaded Telethon command plugin.

    Userbot plugins consistently use ``event.reply``. Installing this small
    adapter once keeps the presentation consistent without changing hundreds
    of command handlers or their behavior.
    """
    try:
        from telethon.tl.custom.message import Message
    except ImportError:
        return

    if getattr(Message.reply, "_flex_message_ui", False):
        return

    original_reply: Callable[..., Awaitable[Any]] = Message.reply
    original_edit: Callable[..., Awaitable[Any]] = Message.edit

    async def styled_reply(self: Any, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], str):
            args = (format_reply_text(args[0]), *args[1:])
        elif isinstance(kwargs.get("message"), str):
            kwargs = {**kwargs, "message": format_reply_text(kwargs["message"])}
        return await original_reply(self, *args, **kwargs)

    styled_reply._flex_message_ui = True  # type: ignore[attr-defined]
    Message.reply = styled_reply  # type: ignore[assignment]

    async def styled_edit(self: Any, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], str):
            args = (format_reply_text(args[0]), *args[1:])
        elif isinstance(kwargs.get("text"), str):
            kwargs = {**kwargs, "text": format_reply_text(kwargs["text"])}
        return await original_edit(self, *args, **kwargs)

    styled_edit._flex_message_ui = True  # type: ignore[attr-defined]
    Message.edit = styled_edit  # type: ignore[assignment]