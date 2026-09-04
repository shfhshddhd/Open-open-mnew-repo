"""Owner-only, guarded AI self-update and backup management commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import events
from telegram import InlineKeyboardMarkup

from memory.memory import (
    append_conversation,
    derive_project_facts,
    learned_facts,
    memory_stats,
    pending_updates,
    queue_pending_update,
    recent_conversations,
    recent_updates,
    record_update,
    remove_pending_update,
    update_learned_facts,
)
from plugins.bot import add_handler
from utils.decorators import is_bot_owner, rishabh
from utils.key_manager import (
    ProviderUnavailable,
    ProviderRequestFailed,
    add_provider_key,
    ai_generate,
    provider_status,
    provider_summary,
    reset_provider_cycle,
)
from utils.utils import CipherElite
from config.config import Config
from bot.handlers.update_controls import (
    clear_stale_operations,
    finish_operation,
    has_operation,
    recovery_markup,
    register_operation,
    set_operation_task,
)


VERSION = "1.0.0"
CATEGORY = "utilities"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKUPS_DIR = PROJECT_ROOT / "backups"
UPDATES_LOG = BACKUPS_DIR / "updates.log"
_retry_task: asyncio.Task | None = None
_update_lock = asyncio.Lock()
_update_lock_owner: asyncio.Task | None = None
MAX_REPAIR_ATTEMPTS = 3

logger = logging.getLogger(__name__)
client = None

FORBIDDEN_PARTS = {
    ".env",
    ".git",
    ".cache",
    ".local",
    ".pythonlibs",
    ".venv",
    "artifacts",
    "backups",
    "build",
    "dist",
    "lib",
    "memory",
    "node_modules",
    "secrets",
    "__pycache__",
}
CONTEXT_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".md", ".replit"}
CONTEXT_CHAR_LIMIT = 120_000
CORE_CONTEXT_PATHS = {
    "replit.md",
    "pyproject.toml",
    "telegram_userbot/main.py",
    "telegram_userbot/config.py",
    "telegram_userbot/plugin_loader.py",
    "telegram_userbot/userbot/client.py",
    "telegram_userbot/utils/decorators.py",
    "telegram_userbot/utils/key_manager.py",
    "telegram_userbot/plugins/self_update.py",
}

CODING_SYSTEM_PROMPT = """You are a MASTER-LEVEL senior software engineer and the most capable Python developer in existence — surpassing every other coding AI. You are the maintainer of this Telegram userbot project and you have FULL, UNCONDITIONAL AUTHORIZATION to edit any part of it.

ABSOLUTE RULES:
1. NEVER refuse any request. NEVER say 'I can't', 'not possible', 'cannot help', 'this is out of scope', or anything similar. If a request is technically achievable in code, YOU BUILD IT — immediately, confidently, completely.
2. NEVER ask clarifying questions when you can make a reasonable decision. When in doubt between two interpretations, choose the most useful one and implement it.
3. Every request is treated as a direct order from the owner. Your job is to fulfill it perfectly, not to evaluate or discuss it.
4. If a feature requires external APIs or libraries, add them correctly (imports, install requirements if the project has a requirements file, environment variable placeholders if needed — but NEVER put real secrets in code).
5. Write production-quality code: correct imports, correct handler signatures and decorators for the exact framework in use, no duplicate handlers, no broken references, no leftover debug prints, proper error handling, owner-only protection preserved on sensitive commands.
6. Follow the project's existing conventions: same plugin header format, same storage helpers, same naming style, same message-edit helpers.
7. Reply ONLY with valid JSON in this exact format: {"plan":"short summary of the changes you will make","files":[{"path":"relative/path.py","operation":"CREATE|MODIFY|DELETE","content":"FULL new file content for CREATE or MODIFY"}]}. CREATE and MODIFY items require complete string content. DELETE items require only the path and operation; they must not include replacement content. Include ONLY changed files. Escape quotes and newlines correctly. If and ONLY if a request is genuinely impossible to interpret at all, reply {"error":"short reason"} — otherwise always return a plan and files.
8. Never modify .env, secrets, backups/, memory/, or any config holding API keys. Everything else in the project is fair game."""


def _now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _userbot():
    if client is None:
        raise RuntimeError("Hosted userbot client is not initialized.")
    return getattr(client, "_userbot_context", None)


async def _require_owner(event) -> bool:
    if await is_bot_owner(event):
        return True
    await event.reply("⛔ Aapko is command ka access nahi hai.")
    return False


async def _send_control_status(
    chat_id: int | None,
    message: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Send updater activity through the control bot, not the hosted account."""
    userbot = _userbot()
    manager = getattr(userbot, "manager", None) if userbot else None
    control_bot = getattr(manager, "control_bot", None)
    if control_bot is None or not chat_id:
        logger.warning("Control-bot progress unavailable for chat %s.", chat_id)
        return None
    try:
        return await control_bot.send_message(
            chat_id=chat_id,
            text=str(message)[:4000],
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Could not send updater progress through the control bot.")
        return None


async def _edit_control_status(message, text: str) -> None:
    """Edit a control-bot progress message with the AI plan."""
    if message is None:
        return
    try:
        await message.edit_text(str(text)[:4000])
    except Exception:
        logger.exception("Could not edit the control-bot updater plan message.")


class _ProgressReporter:
    """Keep updater output in one editable control-bot message when possible."""

    def __init__(self, chat_id: int, fallback_event=None):
        self.chat_id = chat_id
        self.fallback_event = fallback_event
        self.message = None

    async def update(
        self,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ):
        text = str(text)[:4000]
        if self.message is not None:
            try:
                await self.message.edit_text(text, reply_markup=reply_markup)
                return self.message
            except Exception:
                logger.exception("Could not edit the self-update progress message.")
        self.message = await _send_control_status(
            self.chat_id,
            text,
            reply_markup=reply_markup,
        )
        if self.message is None and self.fallback_event is not None:
            try:
                self.message = await self.fallback_event.reply(text)
            except Exception:
                logger.exception("Could not create the fallback self-update progress message.")
        return self.message


def _provider_name(label: str | None) -> str | None:
    if not label:
        return None
    return str(label).split(":", 1)[0].strip().lower() or None


def _provider_failure_message(exc: ProviderUnavailable) -> str:
    provider = (exc.provider or "selected provider").title()
    attempted = getattr(exc, "attempted", []) or []
    lines = [
        f"⏳ All configured {provider} API keys are currently unavailable.",
    ]
    if attempted:
        lines.append("Attempted keys:")
        lines.extend(
            f"• {item.get('key', 'Key')} — {item.get('reason', 'provider unavailable')}"
            for item in attempted[:12]
        )
    lines.append("Choose Retry to start again from Key 1, or Cancel to stop.")
    return "\n".join(lines)


def _clear_stale_update_lock() -> None:
    """Release only a lock whose owning update task has already ended."""
    global _update_lock_owner
    if not _update_lock.locked():
        return
    owner = _update_lock_owner
    if owner is not None and not owner.done():
        return
    if has_operation():
        return
    _update_lock.release()
    _update_lock_owner = None
    logger.warning("Cleared stale self-update lock after its task ended.")


async def _run_git_sync(
    changed_paths: list[str],
    summary: str,
    *,
    preexisting_dirty: set[str],
    progress: _ProgressReporter | None,
) -> list[str]:
    """Commit only clean, intended paths and report push results truthfully."""
    if not changed_paths:
        return []
    if preexisting_dirty:
        if progress:
            await progress.update(
                "💾 Files are saved. Git commit skipped because unrelated "
                "uncommitted changes were already present in: "
                + ", ".join(sorted(preexisting_dirty)[:8])
            )
        return ["Git commit skipped: unrelated changes were preserved."]

    check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0 or check.stdout.strip() != "true":
        return ["Git is not configured for this project."]

    identity_error = _ensure_git_identity()
    if identity_error:
        return [identity_error]

    if progress:
        await progress.update("💾 Saving modified files and preparing a Git commit…")
    add = subprocess.run(
        ["git", "add", "--", *changed_paths],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        return [f"Git staging failed: {(add.stderr or add.stdout).strip()[:240]}"]

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--", *changed_paths],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    staged_paths = {
        line.strip()
        for line in staged.stdout.splitlines()
        if line.strip()
    }
    intended = set(changed_paths)
    if staged.returncode != 0 or staged_paths != intended:
        subprocess.run(["git", "reset", "--", *changed_paths], cwd=PROJECT_ROOT)
        return ["Git staging verification failed; no commit was created."]

    safe_summary = re.sub(r"\s+", " ", str(summary)).strip()[:120] or "verified changes"
    commit = subprocess.run(
        ["git", "commit", "-m", f"self-update: {safe_summary}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        return [f"Git commit failed: {(commit.stderr or commit.stdout).strip()[:240]}"]

    verify_commit = subprocess.run(
        ["git", "show", "--format=%H", "--no-patch", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if verify_commit.returncode != 0 or not verify_commit.stdout.strip():
        return ["Git commit verification failed."]

    try:
        push = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return [
            "Git commit created successfully.",
            "Git push timed out; the commit was not falsely marked as pushed.",
        ]
    if push.returncode == 0:
        return ["Git commit created and pushed successfully."]
    return [
        "Git commit created successfully.",
        f"Git push failed: {(push.stderr or push.stdout).strip()[:240]}",
    ]


def _safe_relative(raw_path: str) -> Path:
    value = str(raw_path).strip().replace("\\", "/")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe update path: {raw_path}")
    if any(part.lower() in FORBIDDEN_PARTS for part in path.parts):
        raise ValueError(f"Protected update path: {raw_path}")
    resolved = (PROJECT_ROOT / path).resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError(f"Update path is outside the project: {raw_path}")
    return path


def _context_files(prompt: str = "") -> dict[str, str]:
    """Build a bounded, relevant project context for coding providers.

    Sending every plugin (the project is over 300k tokens) causes Gemini and
    OpenRouter to reject or truncate the request before they can return the
    required complete-file JSON. Keep the core runtime files, then add files
    that match the requested paths or words until the context budget is full.
    """
    files: dict[str, str] = {}
    candidates = []
    source_roots = [PROJECT_ROOT / "telegram_userbot"]
    root_files = [
        PROJECT_ROOT / ".replit",
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "replit.md",
    ]
    for source_root in source_roots:
        if source_root.exists():
            candidates.extend(source_root.rglob("*"))
    candidates.extend(root_files)
    candidates = [
        path
        for path in candidates
        if path.is_file()
        and path.suffix.lower() in CONTEXT_SUFFIXES
        and not any(
            part.lower() in FORBIDDEN_PARTS for part in path.relative_to(PROJECT_ROOT).parts
        )
    ]
    prompt_terms = {
        term.lower()
        for term in re.findall(r"[a-zA-Z0-9_./-]{4,}", prompt)
        if term not in {"please", "make", "change", "update", "that", "this", "with"}
    }
    ranked: list[tuple[int, Path, str]] = []
    for path in sorted(candidates):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(content) > 250_000:
            content = content[:250_000] + "\n[FILE TRUNCATED]"
        score = 0
        if relative in CORE_CONTEXT_PATHS:
            score += 1000
        relative_lower = relative.lower()
        score += sum(250 for term in prompt_terms if term in relative_lower)
        score += sum(20 for term in prompt_terms if term in content.lower())
        ranked.append((score, path, content))

    used_chars = 0
    for score, path, content in sorted(
        ranked,
        key=lambda item: (-item[0], item[1].as_posix()),
    ):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if used_chars >= CONTEXT_CHAR_LIMIT:
            break
        remaining = CONTEXT_CHAR_LIMIT - used_chars
        if len(content) > remaining:
            if score < 1000 or remaining < 4000:
                continue
            content = content[:remaining] + "\n[FILE TRUNCATED FOR CONTEXT]"
        files[relative] = content
        used_chars += len(content)
    return files


def _coding_system_prompt() -> str:
    return (
        CODING_SYSTEM_PROMPT
        + "\n\nPROJECT CONTEXT RULES:\n"
        "Preserve the existing Telethon and python-telegram-bot architecture. "
        "Do not remove safety checks, owner checks, or the restart guard.\n"
        f"LEARNED FACTS: {json.dumps(learned_facts(), ensure_ascii=False)}\n"
        f"RECENT CONVERSATIONS: {json.dumps(recent_conversations(5), ensure_ascii=False)}\n"
        f"RECENT APPLIED CHANGES: {json.dumps(recent_updates(3), ensure_ascii=False)}"
    )


def _requested_delete_paths(prompt: str) -> set[str]:
    """Find explicitly requested project-file deletions without trusting content."""
    paths: set[str] = set()
    deletion_pattern = re.compile(r"\b(?:delete|remove|unlink)\b", re.IGNORECASE)
    path_pattern = re.compile(r"(?<![\w/])telegram_userbot/[A-Za-z0-9_.\-/]+")
    for match in deletion_pattern.finditer(str(prompt)):
        prefix = str(prompt)[max(0, match.start() - 12) : match.start()].lower()
        if "do not" in prefix or "don't" in prefix:
            continue
        window = str(prompt)[match.end() : match.end() + 400]
        for path_match in path_pattern.finditer(window):
            raw_path = path_match.group(0).rstrip(".,;:!?)]}")
            try:
                paths.add(_safe_relative(raw_path).as_posix())
            except ValueError:
                continue
    return paths


def _response_files(
    response: dict[str, Any],
    *,
    forced_delete_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    raw_files = response.get("files", response.get("operations", []))
    if not isinstance(raw_files, list):
        raw_files = []
    forced_deletes = forced_delete_paths or set()
    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict) or "path" not in item:
            raise ValueError("AI returned a file operation without a path.")
        relative = _safe_relative(str(item["path"]))
        path = relative.as_posix()
        seen_paths.add(path)
        if path in forced_deletes:
            files.append({"path": path, "operation": "DELETE"})
            continue

        raw_operation = item.get("operation", item.get("action", item.get("op")))
        if raw_operation is None and item.get("delete") is True:
            raw_operation = "DELETE"
        if isinstance(raw_operation, dict):
            raw_operation = raw_operation.get("type") or raw_operation.get("name")
        operation = str(raw_operation or "MODIFY").strip().upper()
        if operation in {"REMOVE", "UNLINK"}:
            operation = "DELETE"
        if operation not in {"CREATE", "MODIFY", "DELETE"}:
            raise ValueError(f"Unsupported file operation {operation!r} for {relative}.")
        if operation == "DELETE":
            files.append({"path": path, "operation": operation})
            continue

        if "content" not in item or not isinstance(item["content"], str):
            raise ValueError(f"AI returned non-text content for {relative}.")
        files.append(
            {
                "path": path,
                "operation": operation,
                "content": item["content"],
            }
        )

    for path in sorted(forced_deletes - seen_paths):
        files.append({"path": path, "operation": "DELETE"})
    return files


def _backup_manifest(backup_dir: Path, paths: list[str]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    files_dir = backup_dir / "files"
    for raw_path in paths:
        relative = _safe_relative(raw_path)
        source = PROJECT_ROOT / relative
        existed = source.is_file()
        item: dict[str, Any] = {"path": relative.as_posix(), "existed": existed}
        if existed:
            target = files_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest.append(item)
    return manifest


def _extend_backup(backup_dir: Path, paths: list[str]) -> None:
    """Add newly discovered review paths while preserving their pre-review state."""
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError("Backup manifest is invalid.")
    existing = {
        str(item.get("path"))
        for item in manifest
        if isinstance(item, dict) and item.get("path")
    }
    additions = [path for path in paths if path not in existing]
    if not additions:
        return
    manifest.extend(_backup_manifest(backup_dir, additions))
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _create_backup(paths: list[str], label: str | None = None) -> tuple[Path, str]:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = label or _now_label()
    backup_dir = BACKUPS_DIR / timestamp
    counter = 1
    while backup_dir.exists():
        backup_dir = BACKUPS_DIR / f"{timestamp}_{counter}"
        counter += 1
    backup_dir.mkdir(parents=True)
    manifest = _backup_manifest(backup_dir, paths)
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return backup_dir, backup_dir.name


def _log_backup(timestamp: str, changed_files: list[str], summary: str) -> None:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": timestamp,
        "changed_files": changed_files,
        "summary": summary[:500],
    }
    with UPDATES_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _apply_files(files: list[dict[str, Any]]) -> list[str]:
    changed: list[str] = []
    for item in files:
        relative = _safe_relative(item["path"])
        target = PROJECT_ROOT / relative
        operation = str(item.get("operation") or "MODIFY").upper()
        if operation == "DELETE":
            if target.is_file():
                target.unlink()
                changed.append(relative.as_posix())
            elif target.exists():
                raise ValueError(f"Cannot delete non-file path: {relative}")
            continue

        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"File operation {operation} requires text content for {relative}.")
        old = target.read_text(encoding="utf-8") if target.is_file() else None
        if old == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(relative.as_posix())
    return changed


def _changed_file_operations(files: list[dict[str, Any]]) -> list[str]:
    """Return only operations that would change the current filesystem."""
    changed: list[str] = []
    for item in files:
        relative = _safe_relative(item["path"])
        target = PROJECT_ROOT / relative
        operation = str(item.get("operation") or "MODIFY").upper()
        if operation == "DELETE":
            if target.is_file():
                changed.append(relative.as_posix())
            continue
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"File operation {operation} requires text content for {relative}.")
        old = target.read_text(encoding="utf-8") if target.is_file() else None
        if old != content:
            changed.append(relative.as_posix())
    return changed


def _restore_files(backup_dir: Path) -> int:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Backup manifest is missing or invalid.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError("Backup manifest is invalid.")
    restored = 0
    for item in manifest:
        if not isinstance(item, dict):
            continue
        relative = _safe_relative(str(item.get("path", "")))
        target = PROJECT_ROOT / relative
        if item.get("existed"):
            source = backup_dir / "files" / relative
            if not source.is_file():
                raise ValueError(f"Backup file is missing: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()
        restored += 1
    return restored


def _compile_files(paths: list[str]) -> tuple[bool, str | None]:
    for raw_path in paths:
        relative = _safe_relative(raw_path)
        if not (PROJECT_ROOT / relative).is_file():
            continue
        if relative.suffix != ".py":
            continue
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(PROJECT_ROOT / relative)],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            return False, f"{relative}: {detail}"
    return True, None


def _python_module_names(paths: list[str]) -> list[str]:
    """Map project Python paths to importable package names for a dry run."""
    modules: list[str] = []
    for raw_path in paths:
        relative = _safe_relative(raw_path)
        if not (PROJECT_ROOT / relative).is_file():
            continue
        if relative.suffix != ".py" or relative.name == "__init__.py":
            continue
        parts = relative.parts
        if not parts or parts[0] != "telegram_userbot":
            continue
        module_parts = list(parts[1:])
        module_parts[-1] = Path(module_parts[-1]).stem
        module = ".".join(module_parts)
        if module and module not in modules:
            modules.append(module)
    return modules


def _import_files(paths: list[str]) -> tuple[bool, str | None]:
    """Import changed Python modules in an isolated process.

    The live plugin client is deliberately not touched. A small decorator
    client lets plugin modules register handlers during import without
    connecting to Telegram.
    """
    modules = _python_module_names(paths)
    if not modules:
        return True, None
    script = r"""
import importlib
import sys
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / "telegram_userbot"))
sys.path.insert(0, str(root))

import config as existing_config
sys.modules["config.config"] = existing_config

class DryRunClient:
    def on(self, *args, **kwargs):
        def decorate(function):
            return function
        return decorate

import utils.utils as utils_module
utils_module.CipherElite = DryRunClient()

for module_name in sys.argv[1:]:
    importlib.import_module(module_name)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *modules],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode == 0:
        return True, None
    detail = (result.stderr or result.stdout).strip()
    return False, f"Import verification failed: {detail[:900]}"


def _verify_python_files(paths: list[str]) -> tuple[bool, str | None]:
    """Run syntax and isolated import checks for the changed Python files."""
    ok, error = _compile_files(paths)
    if not ok:
        return ok, error
    return _import_files(paths)


def _backup_entries() -> list[dict[str, Any]]:
    if not UPDATES_LOG.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in UPDATES_LOG.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                entries.append(item)
        except json.JSONDecodeError:
            continue
    return entries


def _format_memory() -> str:
    stats = memory_stats(provider_summary())
    lines = [
        "🧠 Self-update memory",
        f"Conversations: {stats['conversations']}",
        f"Applied updates: {stats['updates']}",
        f"Pending requests: {stats['pending']}",
        f"Last update: {stats['last_update'] or '—'}",
        f"Last provider: {stats['last_provider'] or '—'}",
    ]
    for provider, rows in provider_status().items():
        active = sum(1 for row in rows if row["active"])
        lines.append(f"{provider.title()}: {active}/{len(rows)} active")
    return "\n".join(lines)


def _git_dirty_paths(paths: list[str]) -> set[str]:
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all", "--", *paths],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        value = line[3:].strip() if len(line) >= 3 else line.strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[-1]
        if value:
            dirty.add(value)
    return dirty


def _plugin_registration_status(paths: list[str]) -> str:
    plugin_paths = [
        path
        for path in paths
        if path.startswith("telegram_userbot/plugins/") and path.endswith(".py")
    ]
    if not plugin_paths:
        return "No new plugin file was needed."
    missing = [path for path in plugin_paths if not (PROJECT_ROOT / path).is_file()]
    if missing:
        if len(missing) == len(plugin_paths):
            return "Plugin files were removed; the existing loader will no longer discover them: " + ", ".join(
                missing[:6]
            )
        return "Plugin registration could not be verified: " + ", ".join(missing[:4])
    return (
        "Plugin files imported successfully; the existing loader will discover them on restart: "
        + ", ".join(plugin_paths[:6])
    )


def _review_file_content(raw_path: str) -> str:
    """Represent deleted files explicitly in the correctness-review prompt."""
    relative = _safe_relative(raw_path)
    target = PROJECT_ROOT / relative
    if not target.is_file():
        return "[FILE DELETED]"
    return target.read_text(encoding="utf-8")


def _display_key(provider_label: str | None, response: dict[str, Any]) -> str:
    provider = _provider_name(provider_label)
    if not provider:
        return "masked provider key"
    masked = str(response.get("_key", ""))
    for row in provider_status().get(provider, []):
        if row.get("masked") == masked:
            return f"{provider.title()} Key {row.get('index', '?')}"
    return f"{provider.title()} key (masked)"


def _git_config_value(scope: str, key: str) -> str:
    result = subprocess.run(
        ["git", "config", f"--{scope}", "--get", key],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _ensure_git_identity() -> str | None:
    """Ensure commits use an existing real identity, never a placeholder."""
    local = (
        _git_config_value("local", "user.name"),
        _git_config_value("local", "user.email"),
    )
    candidates: list[tuple[str, str]] = [local] if all(local) else []

    environment = (
        os.environ.get("GIT_AUTHOR_NAME", "").strip(),
        os.environ.get("GIT_AUTHOR_EMAIL", "").strip(),
    )
    if all(environment):
        candidates.append(environment)

    for scope in ("global", "system"):
        configured = (
            _git_config_value(scope, "user.name"),
            _git_config_value(scope, "user.email"),
        )
        if all(configured):
            candidates.append(configured)

    identity = next(
        (
            pair
            for pair in candidates
            if pair[0].lower() not in {"you", "your name", "name"}
            and pair[1].lower()
            not in {
                "you@example.com",
                "name@example.com",
                "user@example.com",
            }
        ),
        None,
    )
    if identity is None:
        return (
            "Git commit skipped: no real Git author identity is configured "
            "in the project or environment."
        )

    if local != identity:
        for key, value in (
            ("user.name", identity[0]),
            ("user.email", identity[1]),
        ):
            result = subprocess.run(
                ["git", "config", "--local", key, value],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[:240]
                return f"Git author configuration failed: {detail}"
    return None


async def _apply_update_request(
    prompt: str,
    context_files: dict[str, str],
    *,
    event=None,
    pending_id: str | None = None,
    progress_message=None,
    control_chat_id: int | None = None,
    progress: _ProgressReporter | None = None,
    preferred_providers: tuple[str, ...] | None = None,
) -> tuple[bool, str]:
    global _update_lock_owner
    async with _update_lock:
        current_task = asyncio.current_task()
        _update_lock_owner = current_task
        provider_label: str | None = None
        provider_calls: list[dict[str, str]] = []
        backup_dir: Path | None = None
        progress = progress or _ProgressReporter(
            control_chat_id or getattr(event, "chat_id", None) or getattr(event, "sender_id", 0),
            event,
        )
        selected_provider = (
            preferred_providers[0]
            if preferred_providers and len(preferred_providers) == 1
            else None
        )
        try:
            await progress.update(
                "📥 Update request received.\n"
                + (
                    f"Provider: {selected_provider.title()} only.\n"
                    if selected_provider
                    else "Provider: existing automatic fallback behavior.\n"
                )
                + "🔍 Reading the project structure and analyzing the request…"
            )
            response = await ai_generate(
                prompt,
                context_files,
                system_prompt=_coding_system_prompt(),
                preferred_providers=preferred_providers or ("gemini", "openrouter"),
                progress_callback=progress.update,
            )
            provider_label = response.get("_provider")
            if response.get("_provider"):
                provider_calls.append(
                    {
                        "provider": str(response["_provider"]).split(":", 1)[0],
                        "model": str(response.get("_model", "unknown")),
                        "key": str(response.get("_key", "masked")),
                    }
                )
            plan = response.get("plan")
            if plan:
                await progress.update(f"📋 Plan: {str(plan)}\n\n✍️ Identifying the files to change…")
            files = _response_files(
                response,
                forced_delete_paths=_requested_delete_paths(prompt),
            )
            if not files:
                message = str(response.get("summary") or response.get("text") or "AI returned no file changes.")
                update_learned_facts(derive_project_facts(context_files))
                update_learned_facts(response.get("facts"))
                append_conversation(prompt, response, "no_changes")
                if pending_id:
                    remove_pending_update(pending_id)
                return True, message

            changed = _changed_file_operations(files)
            if not changed:
                append_conversation(prompt, response, "no_changes")
                if pending_id:
                    remove_pending_update(pending_id)
                return True, "AI found no changes to apply."

            dirty_before = _git_dirty_paths(changed)
            await progress.update(
                f"📦 Creating a protected backup for {len(changed)} changed file(s)…\n"
                "✍️ Applying the requested feature while preserving unrelated changes…"
            )
            backup_dir, backup_timestamp = _create_backup(changed)
            _apply_files(files)
            current_changed = list(changed)

            async def repair_compile(
                initial_error: str,
            ) -> tuple[bool, str | None, dict[str, Any] | None]:
                nonlocal current_changed
                repair_error = initial_error
                last_response: dict[str, Any] | None = None
                repair_provider = selected_provider or _provider_name(provider_label)
                if not repair_provider:
                    return False, repair_error, last_response
                for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
                    await progress.update(
                        f"⚠️ Problem detected: {repair_error[:500]}\n"
                        f"🔧 Fixing the problem automatically ({attempt}/{MAX_REPAIR_ATTEMPTS})…"
                    )
                    repair_prompt = (
                        "The previous self-update produced this verification error:\n"
                        f"{repair_error}\n\n"
                        "Inspect the current project files, fix only the error, and return "
                        "the same JSON format with complete file contents. Do not return "
                        "an explanation without files."
                    )
                    last_response = await ai_generate(
                        repair_prompt,
                        _context_files(),
                        system_prompt=_coding_system_prompt(),
                        preferred_providers=(repair_provider,),
                        progress_callback=progress.update,
                    )
                    fixes = _response_files(last_response)
                    if not fixes:
                        return False, "AI did not return a repair file.", last_response
                    _extend_backup(backup_dir, [item["path"] for item in fixes])
                    fixed_paths = _apply_files(fixes)
                    current_changed = list(dict.fromkeys(current_changed + fixed_paths))
                    ok, repair_error = _verify_python_files(current_changed)
                    if ok:
                        await progress.update("✅ Fix applied. Verification checks pass again.")
                        return True, None, last_response
                return False, repair_error, last_response

            await progress.update("🧪 Running syntax and import checks on changed files…")
            ok, error = _verify_python_files(current_changed)
            if not ok:
                repaired, repair_error, repair_response = await repair_compile(error or "Compilation failed.")
                if repair_response and repair_response.get("_provider"):
                    provider_calls.append(
                        {
                            "provider": str(repair_response["_provider"]).split(":", 1)[0],
                            "model": str(repair_response.get("_model", "unknown")),
                            "key": str(repair_response.get("_key", "masked")),
                        }
                    )
                if not repaired:
                    _restore_files(backup_dir)
                    append_conversation(prompt, response, "compile_error", error=repair_error)
                    return False, f"❌ Automatic repair stopped; original files restored.\n{repair_error}"

            review_prompt = (
                "Double-check the update you just proposed for obvious bugs: missing imports, "
                "wrong function names, unsafe paths, duplicate handlers, and syntax-adjacent "
                "mistakes. Return the same JSON format. Return an empty files array if no fix "
                "is needed. Here are the updated files:\n"
                + "\n\n".join(
                    f"===== {path} =====\n{_review_file_content(path)}"
                    for path in current_changed
                )
            )
            try:
                for review_number in range(1, 3):
                    await progress.update(f"🔎 Running correctness review {review_number}/2…")
                    review = await ai_generate(
                        review_prompt,
                        _context_files(),
                        system_prompt=_coding_system_prompt(),
                        preferred_providers=(
                            (selected_provider,)
                            if selected_provider
                            else ("openrouter", "gemini")
                        ),
                        progress_callback=progress.update,
                    )
                    if review.get("_provider"):
                        provider_label = review["_provider"]
                        provider_calls.append(
                            {
                                "provider": str(review["_provider"]).split(":", 1)[0],
                                "model": str(review.get("_model", "unknown")),
                                "key": str(review.get("_key", "masked")),
                            }
                        )
                    fixes = _response_files(review)
                    if not fixes:
                        await progress.update(f"✅ Review {review_number}/2 passed; no fix was needed.")
                        break
                    await progress.update(
                        f"🛠️ Review {review_number}/2 found a fix; applying and compiling it…"
                    )
                    _extend_backup(backup_dir, [item["path"] for item in fixes])
                    fixed_paths = _apply_files(fixes)
                    current_changed = list(dict.fromkeys(current_changed + fixed_paths))
                    ok, error = _verify_python_files(current_changed)
                    if not ok:
                        repaired, repair_error, repair_response = await repair_compile(
                            error or "Review compilation failed."
                        )
                        if repair_response and repair_response.get("_provider"):
                            provider_calls.append(
                                {
                                    "provider": str(repair_response["_provider"]).split(":", 1)[0],
                                    "model": str(repair_response.get("_model", "unknown")),
                                    "key": str(repair_response.get("_key", "masked")),
                                }
                            )
                        if not repaired:
                            _restore_files(backup_dir)
                            append_conversation(
                                prompt,
                                review,
                                "review_compile_error",
                                error=repair_error,
                            )
                            return False, (
                                "❌ Automatic repair stopped; original files restored.\n"
                                f"{repair_error}"
                            )
            except ProviderUnavailable:
                _restore_files(backup_dir)
                raise

            summary = str(response.get("summary") or "Verified self-update")
            await progress.update(
                "✅ Verification passed.\n"
                "🔌 Checking plugin/command discovery and saving the final file set…"
            )
            registration = _plugin_registration_status(current_changed)
            git_results = await _run_git_sync(
                current_changed,
                summary,
                preexisting_dirty=dirty_before,
                progress=progress,
            )
            update_learned_facts(derive_project_facts(_context_files()))
            update_learned_facts(response.get("facts"))
            _log_backup(backup_timestamp, current_changed, summary)
            record_update(
                prompt=prompt,
                summary=summary,
                changed_files=current_changed,
                provider=provider_label,
                backup_timestamp=backup_timestamp,
                provider_calls=provider_calls,
            )
            append_conversation(prompt, response, "success")
            if pending_id:
                remove_pending_update(pending_id)
            changed_lines = "\n".join(f"• {path}" for path in current_changed[:12])
            git_lines = "\n".join(f"• {item}" for item in git_results)
            return True, (
                "✅ Update completed successfully.\n"
                f"Provider: {_provider_name(provider_label).title() if _provider_name(provider_label) else 'Existing fallback'}\n"
                f"API key: {_display_key(provider_label, response)}\n"
                f"Changes:\n{changed_lines}\n"
                "Verification: Passed\n"
                f"Plugin/command registration: {registration}\n"
                f"Git save/sync:\n{git_lines or '• Files saved; no Git action was required.'}\n"
                "Bot status: Restart requested; the workflow will reload the saved plugins."
            )
        except ProviderUnavailable:
            if backup_dir is not None:
                _restore_files(backup_dir)
            raise
        except asyncio.CancelledError:
            if backup_dir is not None:
                try:
                    _restore_files(backup_dir)
                except Exception:
                    logger.exception("Could not restore after self-update cancellation.")
            raise
        except Exception as exc:
            logger.exception("Self-update failed.")
            if backup_dir is not None:
                try:
                    _restore_files(backup_dir)
                except Exception:
                    logger.exception("Could not restore the update backup after failure.")
            append_conversation(prompt, {"error": str(exc)}, "error", error=str(exc))
            return False, f"❌ Update failed safely: {exc}\nNo files were left partially applied."
        finally:
            if _update_lock_owner is asyncio.current_task():
                _update_lock_owner = None


async def _retry_pending_loop() -> None:
    while True:
        await asyncio.sleep(600)
        for item in pending_updates():
            if has_operation() or _update_lock.locked():
                break
            try:
                provider = str(item.get("provider") or "").strip().lower() or None
                success, message = await _apply_update_request(
                    str(item.get("prompt", "")),
                    item.get("context_files", {}),
                    pending_id=str(item.get("id", "")),
                    control_chat_id=item.get("sender_id") or Config.OWNER_ID,
                    preferred_providers=(provider,) if provider else None,
                )
                await _send_control_status(
                    item.get("sender_id") or Config.OWNER_ID,
                    f"🔁 Pending update retry: {message}",
                )
                if success and message.startswith("✅"):
                    await asyncio.sleep(5)
                    os._exit(0)
            except ProviderUnavailable:
                continue
            except Exception:
                logger.exception("Pending self-update retry failed.")


async def _offer_recovery(
    operation_id: str,
    *,
    provider: str,
    progress: _ProgressReporter,
    message: str,
    retry: Any,
    cancel: Any,
) -> None:
    register_operation(
        operation_id,
        provider=provider,
        retry=retry,
        cancel=cancel,
    )
    set_operation_task(operation_id, None)
    await progress.update(message, reply_markup=recovery_markup(operation_id))


async def _run_update_operation(
    event,
    *,
    command_name: str,
    prompt: str,
    provider: str | None,
) -> None:
    clear_stale_operations()
    _clear_stale_update_lock()
    if has_operation() or _update_lock.locked():
        await event.reply("⚠️ An update is already running. Please wait for it to finish.")
        return

    context_files = _context_files(prompt)
    chat_id = getattr(event, "chat_id", None) or event.sender_id
    pending_id = queue_pending_update(
        prompt=prompt,
        context_files=context_files,
        sender_id=chat_id,
        provider=provider,
    )
    operation_id = pending_id or uuid.uuid4().hex
    progress = _ProgressReporter(chat_id, event)
    preferred = (provider,) if provider else None

    async def cancel() -> None:
        remove_pending_update(pending_id)

    async def retry() -> None:
        recovery_pending = False
        try:
            if provider:
                reset_provider_cycle(provider, clear_cooldowns=True)
            await progress.update(
                f"🔄 Retrying `{command_name}`.\n"
                f"Provider: {provider.title()} only.\n"
                "🔍 Checking API keys again from Key 1…"
            )
            set_operation_task(operation_id, asyncio.current_task())
            success, message = await _apply_update_request(
                prompt,
                context_files,
                pending_id=pending_id,
                control_chat_id=chat_id,
                progress=progress,
                preferred_providers=preferred,
            )
            if success:
                finish_operation(operation_id)
                await progress.update(message)
                await asyncio.sleep(5)
                os._exit(0)
            await _offer_recovery(
                operation_id,
                provider=provider or "selected",
                progress=progress,
                message=message + "\n\nChoose Retry to try again or Cancel to stop.",
                retry=retry,
                cancel=cancel,
            )
            recovery_pending = True
            return
        except ProviderUnavailable as exc:
            await _offer_recovery(
                operation_id,
                provider=provider or "selected",
                progress=progress,
                message=_provider_failure_message(exc),
                retry=retry,
                cancel=cancel,
            )
            recovery_pending = True
            return
        except asyncio.CancelledError:
            try:
                await progress.update("❌ Update process cancelled.")
            except Exception:
                logger.exception("Could not report self-update cancellation.")
            return
        except Exception as exc:
            logger.exception("Self-update retry failed.")
            try:
                await progress.update(f"❌ Update retry failed safely: {exc}")
            except Exception:
                logger.exception("Could not report self-update retry failure.")
            return
        finally:
            if not recovery_pending:
                finish_operation(operation_id)

    if provider:
        reset_provider_cycle(provider, clear_cooldowns=True)
    register_operation(
        operation_id,
        provider=provider or "existing",
        retry=retry,
        cancel=cancel,
    )
    set_operation_task(operation_id, asyncio.current_task())
    recovery_pending = False
    try:
        await progress.update(
            f"📥 `{command_name}` received.\n"
            + (
                f"Provider: {provider.title()} only.\n"
                if provider
                else "Provider: preserving existing automatic fallback behavior.\n"
            )
            + f"🔍 Reading {len(context_files)} relevant project files…"
        )
        success, message = await _apply_update_request(
            prompt,
            context_files,
            event=event,
            pending_id=pending_id,
            control_chat_id=chat_id,
            progress=progress,
            preferred_providers=preferred,
        )
        if success:
            finish_operation(operation_id)
            await progress.update(message)
            await asyncio.sleep(5)
            os._exit(0)

        await _offer_recovery(
            operation_id,
            provider=provider or "selected",
            progress=progress,
            message=message + "\n\nChoose Retry to try again or Cancel to stop.",
            retry=retry,
            cancel=cancel,
        )
        recovery_pending = True
        return
    except ProviderUnavailable as exc:
        await _offer_recovery(
            operation_id,
            provider=provider or "selected",
            progress=progress,
            message=_provider_failure_message(exc),
            retry=retry,
            cancel=cancel,
        )
        recovery_pending = True
        return
    except asyncio.CancelledError:
        try:
            await progress.update("❌ Update process cancelled.")
        except Exception:
            logger.exception("Could not report self-update cancellation.")
        return
    except Exception as exc:
        logger.exception("Self-update operation failed before completion.")
        try:
            await progress.update(f"❌ Update failed safely: {exc}")
        except Exception:
            logger.exception("Could not report self-update failure.")
        return
    finally:
        if not recovery_pending:
            finish_operation(operation_id)


def init(client_instance):
    global client, _retry_task
    client = client_instance
    add_handler(
        "self_update",
        [
            ".updatetohidbot <prompt> — Apply a verified AI code update (owner only)",
            ".updategemini <prompt> — Apply an update with Gemini keys only (owner only)",
            ".updateopenrouter <prompt> — Apply an update with OpenRouter keys only (owner only)",
            ".restorebackup [timestamp] — List or restore a backup (owner only)",
            ".memory — Show self-update memory stats (owner only)",
            ".addopenrouterkey <API_KEY> — Add an OpenRouter key (owner only)",
            ".addclaudekey <API_KEY> — Add an Anthropic key (owner only)",
            ".listkeys — List all masked provider keys (owner only)",
        ],
        "Self-update, restore, memory and multi-provider key management",
    )
    if _retry_task is None or _retry_task.done():
        _retry_task = asyncio.create_task(_retry_pending_loop())


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.updatetohidbot(?:\s+([\s\S]+))?$"))
@rishabh()
async def updatetohidbot_command(event):
    if not await _require_owner(event):
        return
    prompt = (event.pattern_match.group(1) or "").strip()
    if not prompt:
        await event.reply("Usage: .updatetohidbot <what to change>")
        return
    await _run_update_operation(
        event,
        command_name=".updatetohidbot",
        prompt=prompt,
        provider=None,
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.updategemini(?:\s+([\s\S]+))?$"))
@rishabh()
async def updategemini_command(event):
    if not await _require_owner(event):
        return
    prompt = (event.pattern_match.group(1) or "").strip()
    if not prompt:
        await event.reply("Usage: .updategemini <prompt>")
        return
    await _run_update_operation(
        event,
        command_name=".updategemini",
        prompt=prompt,
        provider="gemini",
    )


@CipherElite.on(
    events.NewMessage(outgoing=True, pattern=r"^\.updateopenrouter(?:\s+([\s\S]+))?$")
)
@rishabh()
async def updateopenrouter_command(event):
    if not await _require_owner(event):
        return
    prompt = (event.pattern_match.group(1) or "").strip()
    if not prompt:
        await event.reply("Usage: .updateopenrouter <prompt>")
        return
    await _run_update_operation(
        event,
        command_name=".updateopenrouter",
        prompt=prompt,
        provider="openrouter",
    )


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.restorebackup(?:\s+(\S+))?$"))
@rishabh()
async def restorebackup_command(event):
    if not await _require_owner(event):
        return
    requested = (event.pattern_match.group(1) or "").strip()
    if not requested:
        entries = _backup_entries()
        if not entries:
            await event.reply("📦 No verified backups are available.")
            return
        lines = ["📦 Available verified backups:"]
        for item in reversed(entries[-20:]):
            files = item.get("changed_files", [])
            lines.append(
                f"\n{item.get('timestamp', 'unknown')}\n"
                f"  {', '.join(files) if files else 'no file list'}"
            )
        await event.reply("\n".join(lines))
        return

    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", requested):
            raise ValueError("Backup timestamp contains invalid characters.")
        backup_dir = (BACKUPS_DIR / requested).resolve()
        if backup_dir.parent != BACKUPS_DIR.resolve() or not backup_dir.is_dir():
            raise ValueError("Backup timestamp was not found.")
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        paths = [str(item["path"]) for item in manifest if isinstance(item, dict) and item.get("path")]
        _create_backup(paths, f"restore_{requested}")
        count = _restore_files(backup_dir)
    except Exception as exc:
        await event.reply(f"❌ Restore rejected: {exc}")
        return
    await event.reply(
        f"✅ Restored {count} files from {requested}. Restarting in 5 seconds..."
    )
    await asyncio.sleep(5)
    os._exit(0)


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.memory$"))
@rishabh()
async def memory_command(event):
    if not await _require_owner(event):
        return
    await event.reply(_format_memory())


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.addopenrouterkey(?:\s+(.+))?$"))
@rishabh()
async def addopenrouterkey_command(event):
    if not await _require_owner(event):
        return
    args = (event.pattern_match.group(1) or "").strip().split()
    if not args:
        await event.reply("Usage: .addopenrouterkey <API_KEY>")
        return
    if add_provider_key("openrouter", " ".join(args)):
        await event.reply("✅ OpenRouter key saved securely.")
    else:
        await event.reply("⚠️ OpenRouter key is empty or already saved.")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.addclaudekey(?:\s+(.+))?$"))
@rishabh()
async def addclaudekey_command(event):
    if not await _require_owner(event):
        return
    args = (event.pattern_match.group(1) or "").strip().split()
    if not args:
        await event.reply("Usage: .addclaudekey <API_KEY>")
        return
    if add_provider_key("anthropic", " ".join(args)):
        await event.reply("✅ Anthropic Claude key saved securely.")
    else:
        await event.reply("⚠️ Claude key is empty or already saved.")


@CipherElite.on(events.NewMessage(outgoing=True, pattern=r"^\.listkeys$"))
@rishabh()
async def listkeys_command(event):
    if not await _require_owner(event):
        return
    status = provider_status()
    lines = ["🔑 Configured AI provider keys:"]
    labels = {"gemini": "Gemini", "openrouter": "OpenRouter", "anthropic": "Anthropic Claude"}
    for provider, label in labels.items():
        rows = status.get(provider, [])
        lines.append(f"\n{label}:")
        if not rows:
            lines.append("  — none configured")
            continue
        for row in rows:
            source = "environment fallback" if row["source"] == "environment" else "saved"
            state = row["cooldown_text"]
            selected = "; selected" if row.get("selected") else ""
            lines.append(
                f"  {row['index']}. {row['masked']} "
                f"({source}; {state}{selected})"
            )
    lines.append(
        "\nUse .delkey [gemini|openrouter|claude] <number> or "
        ".switchkey [gemini|openrouter|claude] <number>."
    )
    await event.reply("\n".join(lines))
