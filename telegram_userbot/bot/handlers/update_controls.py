"""Callback controls for owner-facing AI self-update operations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
from utils.message_ui import format_reply_text


logger = logging.getLogger(__name__)
RetryCallback = Callable[[], Awaitable[None]]
CancelCallback = Callable[[], Awaitable[None]]


@dataclass
class UpdateOperation:
    operation_id: str
    provider: str
    retry: RetryCallback
    cancel: CancelCallback
    task: asyncio.Task | None = None
    terminal: bool = True


_operations: dict[str, UpdateOperation] = {}


def recovery_markup(operation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Retry",
                    callback_data=f"self_update:retry:{operation_id}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"self_update:cancel:{operation_id}",
                ),
            ]
        ]
    )


def register_operation(
    operation_id: str,
    *,
    provider: str,
    retry: RetryCallback,
    cancel: CancelCallback,
) -> None:
    _operations[operation_id] = UpdateOperation(
        operation_id=operation_id,
        provider=provider,
        retry=retry,
        cancel=cancel,
    )


def set_operation_task(operation_id: str, task: asyncio.Task | None) -> None:
    operation = _operations.get(operation_id)
    if operation is not None:
        operation.task = task
        operation.terminal = task is None


def finish_operation(operation_id: str) -> None:
    _operations.pop(operation_id, None)


def clear_stale_operations() -> int:
    """Remove operation records whose update task has already ended."""
    stale_ids = [
        operation_id
        for operation_id, operation in _operations.items()
        if operation.task is not None and operation.task.done()
    ]
    for operation_id in stale_ids:
        _operations.pop(operation_id, None)
    return len(stale_ids)


def has_operation() -> bool:
    clear_stale_operations()
    return any(
        operation.task is not None and not operation.task.done()
        for operation in _operations.values()
    )


async def update_control_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return
    if not (
        query.from_user
        and config.Config.OWNER_ID
        and query.from_user.id == config.Config.OWNER_ID
    ):
        await query.answer(format_reply_text("This control is owner-only."), show_alert=True)
        return

    parts = str(query.data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "self_update":
        await query.answer(
            format_reply_text("This control is no longer available."),
            show_alert=True,
        )
        return

    operation = _operations.get(parts[2])
    if operation is None:
        await query.answer(
            format_reply_text("This update is no longer active."),
            show_alert=True,
        )
        await query.edit_message_reply_markup(reply_markup=None)
        return

    action = parts[1]
    if action == "cancel":
        await query.answer(format_reply_text("Cancelling update…"))
        operation.task.cancel() if operation.task and not operation.task.done() else None
        try:
            await operation.cancel()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Could not cancel self-update operation %s.", operation.operation_id)
        finish_operation(operation.operation_id)
        await query.edit_message_text(format_reply_text("❌ Update process cancelled."))
        return

    if action != "retry":
        await query.answer(format_reply_text("Unknown update control."), show_alert=True)
        return

    if operation.task and not operation.task.done():
        await query.answer(
            format_reply_text("This update is already running."),
            show_alert=True,
        )
        return

    await query.answer(
        format_reply_text(f"Retrying with {operation.provider.title()} key 1…")
    )
    await query.edit_message_reply_markup(reply_markup=None)
    task = asyncio.create_task(operation.retry())
    operation.task = task
    operation.terminal = False
