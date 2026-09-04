"""Provider selection and status helpers for hosted-account AI features."""

from __future__ import annotations

import database.mongo as db
from utils.key_manager import provider_status


SUPPORTED_AI_PROVIDERS = ("gemini", "openrouter")
DEFAULT_AI_PROVIDER = "gemini"


def normalize_ai_provider(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_AI_PROVIDERS:
        return DEFAULT_AI_PROVIDER
    return normalized


async def get_ai_provider(user_id: int) -> str:
    """Load the per-hosted-account provider without changing the user schema."""
    return normalize_ai_provider(await db.get_setting(user_id, "ai_provider", DEFAULT_AI_PROVIDER))


async def set_ai_provider(user_id: int, provider: str) -> str:
    normalized = normalize_ai_provider(provider)
    if str(provider).strip().lower() not in SUPPORTED_AI_PROVIDERS:
        raise ValueError("Unsupported AI provider. Choose gemini or openrouter.")
    await db.set_setting(user_id, "ai_provider", normalized)
    return normalized


def provider_status_for(provider: str) -> dict:
    normalized = normalize_ai_provider(provider)
    rows = provider_status().get(normalized, [])
    selected = next((row for row in rows if row.get("selected")), None)
    active_count = sum(1 for row in rows if row.get("active"))
    return {
        "provider": normalized,
        "rows": rows,
        "total_count": len(rows),
        "active_count": active_count,
        "selected": selected,
        "rotation": (
            f"key {selected['index']} selected; "
            f"{active_count}/{len(rows)} keys available"
            if selected is not None
            else f"no key selected; {active_count}/{len(rows)} keys available"
        ),
    }


async def ai_status_for_user(user_id: int, *, enabled: bool | None = None) -> dict:
    provider = await get_ai_provider(user_id)
    status = provider_status_for(provider)
    status["enabled"] = enabled
    return status