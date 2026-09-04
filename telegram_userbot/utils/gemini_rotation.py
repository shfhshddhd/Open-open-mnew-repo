"""Backward-compatible Gemini rotation facade.

Older plugins import this module directly.  The implementation now lives in
``key_manager`` so every Gemini request shares cooldown and last-used state.
"""

from __future__ import annotations

from typing import Any

from utils.key_manager import (
    add_key,
    gemini_chat,
    get_keys,
    get_last_used_index,
    mask_key,
    remove_key,
    switch_key,
)

ALL_KEYS_EXHAUSTED_MESSAGE = (
    "❌ All Gemini keys are exhausted. Add another key with /addkey <API_KEY>."
)

__all__ = [
    "ALL_KEYS_EXHAUSTED_MESSAGE",
    "add_key",
    "gemini_chat",
    "get_keys",
    "get_last_used_index",
    "mask_key",
    "remove_key",
    "switch_key",
]
