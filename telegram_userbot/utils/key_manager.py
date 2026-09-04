"""Quota-aware multi-provider key management for Telegram AI and self-updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Callable

import google.generativeai as genai
import requests

try:
    from replit import db as replit_db
except Exception:
    replit_db = None


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = PROJECT_ROOT / "memory"
STATE_FILE = MEMORY_DIR / "keys_state.json"
LEGACY_GEMINI_FILE = PROJECT_ROOT / "telegram_userbot" / "DB" / "gemini_keys.json"

COOLDOWN_SECONDS = 24 * 60 * 60
_DB_KEYS_NAME = "gemini_api_keys"
_state_lock = RLock()
_request_lock = asyncio.Lock()

ProgressCallback = Callable[[str], Any] | None


class ProviderUnavailable(RuntimeError):
    """Raised when every configured key for a provider is unavailable."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        attempted: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.attempted = attempted or []


class ProviderRequestFailed(RuntimeError):
    """Raised for a normal provider/request failure that is not key exhaustion."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


def _utc_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso_timestamp(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _mask_key(key: str) -> str:
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:8]}...{key[-4:]}"


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def mask_key(key: str) -> str:
    """Public masking helper; never return a raw key from status output."""
    return _mask_key(key)


def _json_read() -> dict[str, Any]:
    with _state_lock:
        try:
            if STATE_FILE.exists():
                value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    return {}
                # Migrate older state files that persisted environment secrets.
                changed = False
                for provider_state in value.get("providers", {}).values():
                    if not isinstance(provider_state, dict):
                        continue
                    for entry in provider_state.get("keys", []):
                        if (
                            isinstance(entry, dict)
                            and entry.get("source") == "environment"
                            and entry.get("value")
                        ):
                            entry["fingerprint"] = _key_fingerprint(str(entry["value"]))
                            entry.pop("value", None)
                            changed = True
                if changed:
                    _json_write(value)
                return value
        except Exception as exc:
            logger.warning("Could not read provider state: %s", exc)
    return {}


def _json_write(value: dict[str, Any]) -> None:
    with _state_lock:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        persisted = json.loads(json.dumps(value))
        for provider_state in persisted.get("providers", {}).values():
            if not isinstance(provider_state, dict):
                continue
            for entry in provider_state.get("keys", []):
                if isinstance(entry, dict) and entry.get("source") == "environment":
                    if entry.get("value"):
                        entry["fingerprint"] = _key_fingerprint(str(entry["value"]))
                    entry.pop("value", None)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(persisted, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(STATE_FILE)


def _load_saved_gemini_keys() -> list[str]:
    """Load Gemini keys from the shared state, plus any older storage.

    Older versions stored Gemini keys in Replit DB or
    ``telegram_userbot/DB/gemini_keys.json`` while the other providers used
    ``memory/keys_state.json``.  Reading all three locations here lets the
    next successful write migrate old keys without losing them.
    """
    with _state_lock:
        values: list[str] = []
        state = _normalise_state(_json_read())
        for entry in state["providers"]["gemini"].get("keys", []):
            if isinstance(entry, dict):
                value = str(entry.get("value", "")).strip()
            else:
                value = str(entry).strip()
            if value and value not in values:
                values.append(value)

        legacy_values: list[str] = []
        if replit_db is not None:
            try:
                value = replit_db.get(_DB_KEYS_NAME, [])
                if isinstance(value, list):
                    legacy_values.extend(
                        str(item).strip() for item in value if str(item).strip()
                    )
            except Exception as exc:
                logger.warning("Gemini key database read failed; using file fallback: %s", exc)
        try:
            if LEGACY_GEMINI_FILE.exists():
                value = json.loads(LEGACY_GEMINI_FILE.read_text(encoding="utf-8"))
                keys = value.get("keys", []) if isinstance(value, dict) else []
                if isinstance(keys, list):
                    legacy_values.extend(
                        str(item).strip() for item in keys if str(item).strip()
                    )
        except Exception as exc:
            logger.warning("Gemini key JSON fallback read failed: %s", exc)

        for value in legacy_values:
            if value not in values:
                values.append(value)
        return values


def _legacy_gemini_keys() -> list[str]:
    """Compatibility helper retained for callers that only need old storage."""
    with _state_lock:
        if replit_db is not None:
            try:
                value = replit_db.get(_DB_KEYS_NAME, [])
                if isinstance(value, list):
                    return [str(item).strip() for item in value if str(item).strip()]
            except Exception as exc:
                logger.warning("Gemini key database read failed; using JSON fallback: %s", exc)
        try:
            if LEGACY_GEMINI_FILE.exists():
                value = json.loads(LEGACY_GEMINI_FILE.read_text(encoding="utf-8"))
                keys = value.get("keys", []) if isinstance(value, dict) else []
                if isinstance(keys, list):
                    return [str(item).strip() for item in keys if str(item).strip()]
        except Exception as exc:
            logger.warning("Gemini key JSON fallback read failed: %s", exc)
    return []


def _save_gemini_keys(keys: list[str]) -> None:
    """Persist Gemini keys in the same state file as every other provider."""
    with _state_lock:
        state = _normalise_state(_json_read())
        provider_state = state["providers"]["gemini"]
        old_records = provider_state.get("keys", [])
        old_by_value = {
            str(entry.get("value", "")): entry
            for entry in old_records
            if isinstance(entry, dict) and entry.get("value")
        }
        records: list[dict[str, Any]] = []
        for key in keys:
            normalized = str(key).strip()
            if not normalized:
                continue
            old = old_by_value.get(normalized, {})
            records.append(
                {
                    "value": normalized,
                    "fingerprint": _key_fingerprint(normalized),
                    "source": "saved",
                    "cooldown_until": old.get("cooldown_until"),
                    "last_used_at": old.get("last_used_at"),
                }
            )
        provider_state["keys"] = records
        if records:
            try:
                provider_state["last_used_index"] = int(
                    provider_state.get("last_used_index", 0)
                ) % len(records)
            except (TypeError, ValueError):
                provider_state["last_used_index"] = 0
        else:
            provider_state["last_used_index"] = 0
        _json_write(state)


def _normalise_state(state: dict[str, Any]) -> dict[str, Any]:
    providers = state.setdefault("providers", {})
    if not isinstance(providers, dict):
        state["providers"] = providers = {}
    for name in ("gemini", "openrouter", "anthropic"):
        item = providers.setdefault(name, {})
        if not isinstance(item, dict):
            providers[name] = item = {}
        if not isinstance(item.get("keys"), list):
            item["keys"] = []
        item.setdefault("last_used_index", 0)
    return state


def _configured_keys(provider: str) -> list[tuple[str, str]]:
    def environment_keys(name: str) -> list[str]:
        candidates: list[tuple[int, str]] = []
        for env_name, value in os.environ.items():
            if env_name == name:
                order = 0
            else:
                match = re.fullmatch(rf"{re.escape(name)}_(\d+)", env_name)
                if not match:
                    continue
                order = int(match.group(1))
            normalized = str(value).strip()
            if normalized:
                candidates.append((order, normalized))
        return [
            value
            for _, value in sorted(candidates, key=lambda item: item[0])
        ]

    if provider == "gemini":
        saved = _load_saved_gemini_keys()
        environment_keys_list = environment_keys("GEMINI_API_KEY")
        keys = [(key, "saved") for key in saved]
        keys.extend(
            (environment, "environment")
            for environment in environment_keys_list
            if environment not in saved
        )
        return keys
    if provider == "openrouter":
        saved = _saved_provider_values("openrouter")
        environment_keys_list = environment_keys("OPENROUTER_API_KEY")
    else:
        saved = _saved_provider_values("anthropic")
        environment_keys_list = environment_keys("ANTHROPIC_API_KEY")
    keys = [(key, "saved") for key in saved]
    keys.extend(
        (environment, "environment")
        for environment in environment_keys_list
        if environment not in saved
    )
    return keys


def _configured_keys_for_source(
    provider: str,
    source: str | None,
) -> list[tuple[str, str]]:
    """Return configured keys filtered without changing shared provider state."""
    keys = _configured_keys(provider)
    if source is None:
        return keys
    return [(value, origin) for value, origin in keys if origin == source]


def _saved_provider_values(provider: str) -> list[str]:
    state = _normalise_state(_json_read())
    values: list[str] = []
    for entry in state["providers"][provider].get("keys", []):
        if isinstance(entry, dict) and str(entry.get("value", "")).strip():
            values.append(str(entry["value"]).strip())
        elif isinstance(entry, str) and entry.strip():
            values.append(entry.strip())
    return values


def _refresh_provider_records(
    state: dict[str, Any],
    provider: str,
) -> list[dict[str, Any]]:
    provider_state = state["providers"][provider]
    old_records = provider_state.get("keys", [])
    old_by_value = {
        str(entry.get("value", "")): entry
        for entry in old_records
        if isinstance(entry, dict) and entry.get("value")
    }
    old_by_fingerprint = {
        str(entry.get("fingerprint")): entry
        for entry in old_records
        if isinstance(entry, dict) and entry.get("fingerprint")
    }
    records: list[dict[str, Any]] = []
    for value, source in _configured_keys(provider):
        old = old_by_value.get(value) or old_by_fingerprint.get(_key_fingerprint(value), {})
        cooldown = old.get("cooldown_until")
        try:
            cooldown_number = float(cooldown) if cooldown else 0
        except (TypeError, ValueError):
            cooldown_number = 0
        if cooldown_number and cooldown_number <= _utc_now():
            cooldown_number = 0
        records.append(
            {
                "value": value,
                "fingerprint": _key_fingerprint(value),
                "source": source,
                "cooldown_until": cooldown_number or None,
                "last_used_at": old.get("last_used_at"),
            }
        )
    provider_state["keys"] = records
    if records:
        try:
            provider_state["last_used_index"] = int(provider_state.get("last_used_index", 0)) % len(records)
        except (TypeError, ValueError):
            provider_state["last_used_index"] = 0
    else:
        provider_state["last_used_index"] = 0
    return records


def _all_records(provider: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = _normalise_state(_json_read())
    records = _refresh_provider_records(state, provider)
    _json_write(state)
    return state, records


def _available_record(
    state: dict[str, Any],
    provider: str,
    *,
    allowed_indices: set[int] | None = None,
) -> tuple[int, dict[str, Any]] | None:
    records = state["providers"][provider]["keys"]
    if not records:
        return None
    try:
        start = int(state["providers"][provider].get("last_used_index", 0)) % len(records)
    except (TypeError, ValueError):
        start = 0
    now = _utc_now()
    for offset in range(len(records)):
        index = (start + offset) % len(records)
        if allowed_indices is not None and index not in allowed_indices:
            continue
        record = records[index]
        cooldown = record.get("cooldown_until")
        if not cooldown or float(cooldown) <= now:
            return index, record
    return None


def _mark_used(
    state: dict[str, Any],
    provider: str,
    index: int,
    record: dict[str, Any],
) -> None:
    # A successful request proves that a previously cooling key is usable
    # again. Clear stale cooldown metadata before persisting its selection.
    record["cooldown_until"] = None
    record["last_used_at"] = _utc_now()
    # Keep the current key selected after a successful request. Rotation is
    # driven by a retryable provider failure, not by normal traffic.
    state["providers"][provider]["last_used_index"] = index
    _json_write(state)


def _mark_exhausted(
    state: dict[str, Any],
    provider: str,
    index: int,
) -> None:
    record = state["providers"][provider]["keys"][index]
    record["cooldown_until"] = _utc_now() + COOLDOWN_SECONDS
    state["providers"][provider]["last_used_index"] = (index + 1) % max(
        1, len(state["providers"][provider]["keys"])
    )
    _json_write(state)


def _reset_provider_cycle(state: dict[str, Any], provider: str) -> None:
    """Make the next available pass begin with the provider's first key."""
    state["providers"][provider]["last_used_index"] = 0
    _json_write(state)


def reset_provider_cycle(provider: str, *, clear_cooldowns: bool = False) -> None:
    """Start the next coding request at the provider's first configured key."""
    if provider not in {"gemini", "openrouter", "anthropic"}:
        raise ValueError("Unsupported provider.")
    state, records = _all_records(provider)
    if clear_cooldowns:
        for record in records:
            record["cooldown_until"] = None
    _reset_provider_cycle(state, provider)


def _is_retryable_error(exc: Exception) -> bool:
    message = str(exc).upper()
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if re.search(r"\b5\d{2}\b", message):
        return True
    return any(
        marker in message
        for marker in (
            "401",
            "402",
            "429",
            "403",
            "500",
            "502",
            "503",
            "RESOURCE_EXHAUSTED",
            "QUOTA EXCEEDED",
            "DAILY LIMIT",
            "DAILY QUOTA",
            "INSUFFICIENT CREDIT",
            "INSUFFICIENT FUND",
            "CREDIT BALANCE",
            "BILLING",
            "RATE LIMIT",
            "RATE_LIMIT",
            "TOO MANY REQUESTS",
            "TIMEOUT",
            "TIMED OUT",
            "CONNECTION",
            "CONNECTION RESET",
            "TEMPORARY",
            "UNAVAILABLE",
            "BAD GATEWAY",
            "GATEWAY TIMEOUT",
            "SERVICE UNAVAILABLE",
        )
    )


def _safe_error_reason(exc: Exception) -> str:
    """Return a short provider reason without echoing credentials or huge bodies."""
    message = str(exc)
    # Provider responses are not trusted to omit credentials. Redact every
    # configured value before putting a reason into progress or recovery text.
    for provider in ("gemini", "openrouter", "anthropic"):
        for key, _source in _configured_keys(provider):
            if key:
                message = message.replace(key, "[redacted]")
    message = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"\s+", " ", message).strip()
    return message[:180] or type(exc).__name__


async def _notify(callback: ProgressCallback, message: str) -> None:
    if callback is None:
        return
    result = callback(message)
    if isinstance(result, Awaitable):
        await result


def _gemini_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    return str(response)


async def _gemini_request(
    key: str,
    prompt: Any,
    *,
    system_instruction: str | None,
    generation_config: dict[str, Any] | None,
) -> tuple[str, str]:
    genai.configure(api_key=key)
    last_error: Exception | None = None
    for model_name in (
        "gemini-2.5-flash",
        "gemini-flash-latest",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ):
        try:
            model = genai.GenerativeModel(
                model_name,
                system_instruction=system_instruction,
            )
            response = await model.generate_content_async(
                prompt,
                generation_config=generation_config,
            )
            return _gemini_text(response), model_name
        except Exception as exc:
            last_error = exc
            if _is_retryable_error(exc):
                raise
    if last_error is not None:
        raise last_error
    raise ProviderUnavailable("Gemini did not return a response.")


async def _openrouter_request(
    key: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str]:
    last_error: Exception | None = None
    for model in (
        "openrouter/free",
        "qwen/qwen-2.5-coder-32b-instruct:free",
        "deepseek/deepseek-chat:free",
        "anthropic/claude-3.5-sonnet",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-coder-32b-instruct",
    ):
        try:
            response = await asyncio.to_thread(
                requests.post,
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://replit.com/",
                    "X-Title": "Telegram Userbot Self Update",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 16000,
                },
                timeout=120,
            )
            if response.status_code >= 400:
                error = RuntimeError(
                    f"OpenRouter HTTP {response.status_code}: {response.text[:500]}"
                )
                # Authentication failures apply to the key, not to a model.
                # Other model/credit errors should fall through to the next
                # OpenRouter route, especially the free routes above.
                if response.status_code in {401, 403}:
                    raise error
                raise error
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("OpenRouter returned no choices.")
            content = choices[0].get("message", {}).get("content")
            if not content:
                raise RuntimeError("OpenRouter returned an empty response.")
            return str(content), model
        except Exception as exc:
            last_error = exc
            if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                raise
            logger.info("OpenRouter model %s failed; trying the next route: %s", model, exc)
            continue
    if last_error is not None:
        raise last_error
    raise ProviderUnavailable("OpenRouter did not return a response.")


async def _anthropic_request(
    key: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str]:
    last_error: Exception | None = None
    for model in ("claude-sonnet-4-20250514", "claude-3-5-sonnet-latest"):
        try:
            response = await asyncio.to_thread(
                requests.post,
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 16000,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=120,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Anthropic HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            blocks = data.get("content", [])
            return (
                "\n".join(
                    str(block.get("text", ""))
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ),
                model,
            )
        except Exception as exc:
            last_error = exc
            if _is_retryable_error(exc):
                raise
    if last_error is not None:
        raise last_error
    raise ProviderUnavailable("Anthropic did not return a response.")


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {"text": text}
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(candidate[start : end + 1])
                return parsed if isinstance(parsed, dict) else {"text": text}
            except json.JSONDecodeError:
                pass
    return {"text": text}


def _provider_label(provider: str, record: dict[str, Any]) -> str:
    return f"{provider}:{_mask_key(str(record.get('value', '')))}"


def _is_coding_retryable_error(exc: Exception) -> bool:
    """Apply the full cooldown policy to self-update calls only."""
    message = str(exc).upper()
    return _is_retryable_error(exc) or bool(re.search(r"\b5\d{2}\b", message))


def _prompt_to_text(prompt: Any) -> str:
    """Convert Gemini-style history into a provider-neutral text prompt."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, (list, tuple)):
        lines: list[str] = []
        for item in prompt:
            if not isinstance(item, dict):
                lines.append(str(item))
                continue
            role = str(item.get("role", "user")).title()
            content = item.get("content")
            if content is None:
                parts = item.get("parts", [])
                if isinstance(parts, list):
                    content = "\n".join(
                        str(part.get("text", ""))
                        if isinstance(part, dict)
                        else str(part)
                        for part in parts
                    )
                else:
                    content = parts
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
    return str(prompt)


async def _generate_with_provider(
    provider: str,
    system_prompt: str,
    user_prompt: str,
    progress_callback: ProgressCallback,
    *,
    source_filter: str | None = None,
) -> tuple[str, str, str]:
    state, records = _all_records(provider)
    allowed_indices = {
        index
        for index, record in enumerate(records)
        if source_filter is None or record.get("source") == source_filter
    }
    if not records or not allowed_indices:
        raise ProviderUnavailable(
            f"No {provider} keys configured.",
            provider=provider,
        )
    attempted = 0
    attempted_details: list[dict[str, str]] = []
    while attempted < len(allowed_indices):
        selected = _available_record(state, provider, allowed_indices=allowed_indices)
        if selected is None:
            _reset_provider_cycle(state, provider)
            raise ProviderUnavailable(
                f"All {provider} keys are cooling down.",
                provider=provider,
                attempted=attempted_details,
            )
        index, record = selected
        attempted += 1
        _mark_used(state, provider, index, record)
        key = str(record["value"])
        await _notify(
            progress_callback,
            f"🔑 {provider.title()} Key {index + 1} is being used.",
        )
        try:
            if provider == "gemini":
                result, model = await _gemini_request(
                    key,
                    user_prompt,
                    system_instruction=system_prompt,
                    generation_config={"max_output_tokens": 16000},
                )
            elif provider == "openrouter":
                result, model = await _openrouter_request(key, system_prompt, user_prompt)
            else:
                result, model = await _anthropic_request(key, system_prompt, user_prompt)
            return result, _provider_label(provider, record), model
        except Exception as exc:
            if _is_coding_retryable_error(exc):
                _mark_exhausted(state, provider, index)
                attempted_details.append(
                    {
                        "key": f"Key {index + 1}",
                        "reason": _safe_error_reason(exc),
                    }
                )
                await _notify(
                    progress_callback,
                    f"⚠️ {provider.title()} Key {index + 1} is unavailable "
                    "for this provider request; switching to the next key.",
                )
                continue
            logger.warning("%s request failed without cooldown: %s", provider, exc)
            raise ProviderRequestFailed(
                f"{provider.title()} request failed: {_safe_error_reason(exc)}",
                provider=provider,
            ) from exc
    raise ProviderUnavailable(
        f"{provider.title()} keys exhausted or unavailable.",
        provider=provider,
        attempted=attempted_details,
    )


async def _chat_with_provider(
    provider: str,
    prompt: Any,
    *,
    model: str,
    system_instruction: str | None,
    generation_config: dict[str, Any] | None,
) -> str:
    """Run one chat request with per-key cooldown and rotation."""
    state, records = _all_records(provider)
    if not records:
        raise ProviderUnavailable(f"No {provider} keys configured.")

    attempted = 0
    while attempted < len(records):
        selected = _available_record(state, provider)
        if selected is None:
            # A complete provider cycle has already been attempted and every
            # key is marked cooling down. Start the next cycle at key 1 on a
            # later request, but never retry the same key forever in one
            # request. If it succeeds, _mark_used clears its stale cooldown.
            if attempted == 0:
                index, record = 0, records[0]
                selected = (index, record)
            else:
                _reset_provider_cycle(state, provider)
                raise ProviderUnavailable(f"All {provider} keys are cooling down.")
        index, record = selected
        attempted += 1
        _mark_used(state, provider, index, record)
        key = str(record["value"])
        try:
            if provider == "gemini":
                result, _ = await _gemini_request(
                    key,
                    prompt,
                    system_instruction=system_instruction,
                    generation_config=generation_config,
                )
            elif provider == "openrouter":
                result, _ = await _openrouter_request(
                    key,
                    system_instruction or "",
                    _prompt_to_text(prompt),
                )
            else:
                result, _ = await _anthropic_request(
                    key,
                    system_instruction or "",
                    _prompt_to_text(prompt),
                )
            return result
        except Exception as exc:
            if _is_retryable_error(exc):
                _mark_exhausted(state, provider, index)
                logger.warning(
                    "%s key %d reached a limit or provider error; rotating.",
                    provider,
                    index + 1,
                )
                continue
            raise
    raise ProviderUnavailable(f"{provider} keys exhausted or unavailable.")


async def provider_chat(
    provider: str,
    prompt: Any,
    *,
    model: str = "",
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
) -> str:
    """Generate through one provider and rotate only within that provider."""
    if provider not in {"gemini", "openrouter"}:
        raise ProviderUnavailable(f"Unsupported AI provider: {provider}.", provider=provider)
    async with _request_lock:
        return await _chat_with_provider(
            provider,
            prompt,
            model=model,
            system_instruction=system_instruction,
            generation_config=generation_config,
        )


async def ai_generate(
    prompt: str,
    context_files: dict[str, str] | None = None,
    *,
    system_prompt: str = "",
    progress_callback: ProgressCallback = None,
    preferred_providers: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Generate coding JSON with configurable provider priority and fallback."""
    user_prompt = prompt
    if context_files:
        user_prompt += "\n\nPROJECT FILES:\n" + "\n\n".join(
            f"===== {path} =====\n{content}" for path, content in context_files.items()
        )
    async with _request_lock:
        errors: list[str] = []
        last_unavailable: ProviderUnavailable | None = None
        last_request_failure: ProviderRequestFailed | None = None
        requested_order = preferred_providers or ("gemini", "openrouter", "anthropic")
        provider_order: list[str] = []
        for provider in requested_order:
            if provider in {"gemini", "openrouter", "anthropic"} and provider not in provider_order:
                provider_order.append(provider)
        if not provider_order:
            provider_order = ["gemini", "openrouter", "anthropic"]

        coding_routes: list[tuple[str, str | None]] = []
        for provider in provider_order:
            if provider == "gemini":
                coding_routes.append(("gemini", "saved"))
            else:
                coding_routes.append((provider, None))
        if "gemini" in provider_order:
            coding_routes.append(("gemini", "environment"))

        for provider, source_filter in coding_routes:
            try:
                await _notify(
                    progress_callback,
                    f"🧠 Trying {provider.title()} coding provider...",
                )
                text, provider_label, model = await _generate_with_provider(
                    provider,
                    system_prompt,
                    user_prompt,
                    progress_callback,
                    source_filter=source_filter,
                )
                parsed = _extract_json(text)
                parsed["_provider"] = provider_label
                parsed["_model"] = model
                parsed["_key"] = provider_label.split(":", 1)[-1]
                return parsed
            except ProviderUnavailable as exc:
                last_unavailable = exc
                errors.append(str(exc))
                logger.info("Provider %s unavailable: %s", provider, exc)
            except ProviderRequestFailed as exc:
                last_request_failure = exc
                errors.append(str(exc))
                logger.info("Provider %s unavailable: %s", provider, exc)
                if len(provider_order) == 1:
                    raise
        if len(provider_order) == 1:
            if last_request_failure is not None:
                raise last_request_failure
            if last_unavailable is not None:
                raise last_unavailable
        raise ProviderUnavailable("; ".join(errors) or "No AI provider returned a response.")


async def gemini_chat(
    prompt: Any,
    *,
    model: str = "gemini-flash-latest",
    system_instruction: str | None = None,
    generation_config: dict[str, Any] | None = None,
) -> str:
    """Gemini chat API with circular Gemini-key rotation."""
    async with _request_lock:
        return await _chat_with_provider(
            "gemini",
            prompt,
            model=model,
            system_instruction=system_instruction,
            generation_config=generation_config,
        )


def get_keys() -> list[str]:
    """Return Gemini saved keys followed by the environment fallback."""
    return [value for value, _ in _configured_keys("gemini")]


def has_any_provider_key() -> bool:
    """Return whether any Gemini, OpenRouter, or Claude key is configured."""
    return any(_configured_keys(provider) for provider in ("gemini", "openrouter", "anthropic"))


def has_provider_key(provider: str) -> bool:
    """Return whether a provider has at least one configured key."""
    return bool(_configured_keys(provider))


def add_key(key: str) -> bool:
    normalized = key.strip()
    if not normalized or normalized in get_keys():
        return False
    saved = _load_saved_gemini_keys()
    saved.append(normalized)
    _save_gemini_keys(saved)
    return True


def add_provider_key(provider: str, key: str) -> bool:
    if provider not in {"openrouter", "anthropic"}:
        raise ValueError("Unsupported provider.")
    normalized = key.strip()
    if not normalized or normalized in [value for value, _ in _configured_keys(provider)]:
        return False
    state = _normalise_state(_json_read())
    state["providers"][provider].setdefault("keys", []).append(
        {"value": normalized, "source": "saved", "cooldown_until": None}
    )
    _json_write(state)
    return True


def remove_provider_key(provider: str, index: int) -> tuple[bool, str]:
    """Remove a displayed key using a 1-based provider-local index."""
    if provider not in {"gemini", "openrouter", "anthropic"}:
        return False, "invalid"
    state, records = _all_records(provider)
    if index < 1 or index > len(records):
        return False, "invalid"
    selected = records[index - 1]
    if selected.get("source") != "saved":
        return False, "fallback"

    selected_value = str(selected.get("value", ""))
    if provider == "gemini":
        saved = [
            value
            for value, source in _configured_keys("gemini")
            if source == "saved" and value != selected_value
        ]
        _save_gemini_keys(saved)
        return True, "removed"

    provider_state = state["providers"][provider]
    provider_state["keys"] = [
        record
        for record in records
        if str(record.get("fingerprint", "")) != str(selected.get("fingerprint", ""))
    ]
    if provider_state["keys"]:
        provider_state["last_used_index"] = min(
            int(provider_state.get("last_used_index", 0)),
            len(provider_state["keys"]) - 1,
        )
    else:
        provider_state["last_used_index"] = 0
    _json_write(state)
    return True, "removed"


def switch_provider_key(provider: str, index: int) -> bool:
    """Select a displayed key using a 1-based provider-local index."""
    if provider not in {"gemini", "openrouter", "anthropic"}:
        return False
    state, records = _all_records(provider)
    if index < 1 or index > len(records):
        return False
    state["providers"][provider]["last_used_index"] = index - 1
    _json_write(state)
    return True


def remove_key(key: str | int) -> tuple[bool, str]:
    keys = get_keys()
    if isinstance(key, int):
        if key < 0 or key >= len(keys):
            return False, "invalid"
        selected = keys[key]
    else:
        selected = key.strip()
        if selected not in keys:
            return False, "invalid"
    saved = _load_saved_gemini_keys()
    if selected not in saved:
        return False, "fallback"
    saved.remove(selected)
    _save_gemini_keys(saved)
    return True, "removed"


def get_last_used_index() -> int:
    state, records = _all_records("gemini")
    if not records:
        return 0
    return int(state["providers"]["gemini"].get("last_used_index", 0)) % len(records)


def switch_key(index: int) -> bool:
    state, records = _all_records("gemini")
    if index < 0 or index >= len(records):
        return False
    state["providers"]["gemini"]["last_used_index"] = index
    _json_write(state)
    return True


def provider_status() -> dict[str, list[dict[str, Any]]]:
    statuses: dict[str, list[dict[str, Any]]] = {}
    for provider in ("gemini", "openrouter", "anthropic"):
        state, records = _all_records(provider)
        try:
            selected_index = int(
                state["providers"][provider].get("last_used_index", 0)
            ) % len(records)
        except (TypeError, ValueError, ZeroDivisionError):
            selected_index = 0
        rows: list[dict[str, Any]] = []
        for index, record in enumerate(records, 1):
            cooldown = record.get("cooldown_until")
            active = not cooldown or float(cooldown) <= _utc_now()
            rows.append(
                {
                    "index": index,
                    "masked": _mask_key(str(record["value"])),
                    "source": record.get("source", "saved"),
                    "active": active,
                    "cooldown_until": cooldown,
                    "cooldown_text": _iso_timestamp(cooldown) if not active else "active",
                    "selected": index - 1 == selected_index,
                }
            )
        statuses[provider] = rows
    return statuses


def provider_summary() -> dict[str, Any]:
    status = provider_status()
    last_used = None
    for provider, rows in status.items():
        used_rows = [row for row in rows if row["active"] or row["cooldown_until"]]
        if used_rows:
            last_used = provider
    return {
        "providers": {
            provider: {
                "count": len(rows),
                "active": sum(1 for row in rows if row["active"]),
                "exhausted": sum(1 for row in rows if not row["active"]),
            }
            for provider, rows in status.items()
        },
        "last_provider": last_used,
    }
