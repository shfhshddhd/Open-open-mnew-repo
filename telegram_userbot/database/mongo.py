import logging
from datetime import datetime, timezone
import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import motor.motor_asyncio
from config import MONGO_URI

logger = logging.getLogger(__name__)

_client: motor.motor_asyncio.AsyncIOMotorClient | None = None
_db: Any = None
_local_lock = asyncio.Lock()
_local_path = Path(__file__).resolve().parents[1] / "DB" / "mongo_fallback.json"


def _matches(document: dict, query: dict) -> bool:
    """Match the small Mongo query subset used by this application."""
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, branch) for branch in expected):
                return False
            continue

        exists = key in document
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected and exists != bool(expected["$exists"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            operators = {"$exists", "$in"}
            if not set(expected).issubset(operators):
                if actual != expected:
                    return False
            continue
        if actual != expected:
            return False
    return True


def _project(document: dict, projection: dict | None) -> dict:
    result = copy.deepcopy(document)
    if not projection:
        return result
    excluded = [key for key, value in projection.items() if value == 0]
    included = [key for key, value in projection.items() if value == 1]
    if included:
        result = {key: result[key] for key in included if key in result}
        if projection.get("_id", 1) and "_id" in document:
            result["_id"] = document["_id"]
    for key in excluded:
        result.pop(key, None)
    return result


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class _LocalCursor:
    def __init__(self, documents: list[dict]):
        self._documents = documents

    async def to_list(self, length: int | None = None) -> list[dict]:
        documents = copy.deepcopy(self._documents)
        return documents if length is None else documents[:length]


class _LocalResult:
    def __init__(self, *, matched_count: int = 0, deleted_count: int = 0):
        self.matched_count = matched_count
        self.deleted_count = deleted_count


class _LocalCollection:
    def __init__(self, database: "_LocalDatabase", name: str):
        self._database = database
        self._name = name

    @property
    def _documents(self) -> list[dict]:
        return self._database._collections.setdefault(self._name, [])

    async def create_index(self, *_args, **_kwargs) -> None:
        return None

    async def find_one(self, query: dict, projection: dict | None = None) -> dict | None:
        async with _local_lock:
            document = next((item for item in self._documents if _matches(item, query)), None)
            return _project(document, projection) if document else None

    def find(
        self,
        query: dict,
        projection: dict | None = None,
        sort: list[tuple[str, int]] | None = None,
    ) -> _LocalCursor:
        documents = [
            _project(item, projection)
            for item in self._documents
            if _matches(item, query)
        ]
        if sort:
            for key, direction in reversed(sort):
                documents.sort(
                    key=lambda item: (item.get(key) is None, str(item.get(key))),
                    reverse=direction < 0,
                )
        return _LocalCursor(documents)

    async def update_one(
        self,
        query: dict,
        update: dict,
        upsert: bool = False,
    ) -> _LocalResult:
        async with _local_lock:
            document = next((item for item in self._documents if _matches(item, query)), None)
            inserted = document is None
            if document is None:
                if not upsert:
                    return _LocalResult()
                document = {
                    key: value
                    for key, value in query.items()
                    if not key.startswith("$") and not isinstance(value, dict)
                }
                self._documents.append(document)

            _apply_update(document, update, inserting=inserted)
            self._database._save()
            return _LocalResult(matched_count=1)

    async def delete_one(self, query: dict) -> _LocalResult:
        async with _local_lock:
            for index, document in enumerate(self._documents):
                if _matches(document, query):
                    del self._documents[index]
                    self._database._save()
                    return _LocalResult(deleted_count=1)
            return _LocalResult()

    async def delete_many(self, query: dict) -> _LocalResult:
        async with _local_lock:
            original = len(self._documents)
            self._database._collections[self._name] = [
                item for item in self._documents if not _matches(item, query)
            ]
            deleted = original - len(self._database._collections[self._name])
            if deleted:
                self._database._save()
            return _LocalResult(deleted_count=deleted)


def _apply_update(document: dict, update: dict, *, inserting: bool) -> None:
    for operator, values in update.items():
        if operator == "$set":
            document.update(copy.deepcopy(values))
        elif operator == "$setOnInsert" and inserting:
            document.update(copy.deepcopy(values))
        elif operator == "$unset":
            for key in values:
                document.pop(key, None)
        elif operator == "$inc":
            for key, amount in values.items():
                document[key] = document.get(key, 0) + amount
        elif operator in {"$addToSet", "$push"}:
            for key, value in values.items():
                current = document.setdefault(key, [])
                if operator == "$addToSet":
                    if value not in current:
                        current.append(copy.deepcopy(value))
                    continue
                if isinstance(value, dict) and "$each" in value:
                    current.extend(copy.deepcopy(value["$each"]))
                    if "$slice" in value:
                        slice_size = int(value["$slice"])
                        document[key] = current[:slice_size] if slice_size >= 0 else current[slice_size:]
                else:
                    current.append(copy.deepcopy(value))
        elif operator == "$pull":
            for key, value in values.items():
                current = document.get(key, [])
                if isinstance(current, list):
                    document[key] = [item for item in current if item != value]


class _LocalDatabase:
    def __init__(self, path: Path):
        self._path = path
        self._collections: dict[str, list[dict]] = {}
        self._load()

    def __getattr__(self, name: str) -> _LocalCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return _LocalCollection(self, name)

    def _load(self) -> None:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self._collections = payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._collections = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self._collections, handle, ensure_ascii=False, indent=2, default=_json_default)
        temporary.replace(self._path)


async def connect() -> Any:
    """Connect to MongoDB without preventing the bot from starting.

    MongoDB stores hosted-account state, but it is not needed for the PTB
    control bot to answer basic commands such as /start. Atlas/network
    outages therefore put the application into a degraded mode instead of
    crashing the whole polling process.
    """
    global _client, _db
    if not MONGO_URI:
        logger.warning("MONGO_URI is empty; starting without persistent database.")
        _db = _LocalDatabase(_local_path)
        return _db

    client = motor.motor_asyncio.AsyncIOMotorClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5_000,
        connectTimeoutMS=5_000,
        socketTimeoutMS=5_000,
    )
    database = client["telegram_userbot"]

    try:
        await database.command("ping")
        # Create indexes
        await database.users.create_index("user_id", unique=True)
        # Per-group latest-message index: (user_id, target_id, chat_id) is the unique key
        await database.group_messages.create_index(
            [("user_id", 1), ("target_id", 1), ("chat_id", 1)],
            unique=True,
        )
        # Saved Messages reply bridge: maps a forwarded message back to its source.
        await database.setgroup_map.create_index(
            [("user_id", 1), ("saved_msg_id", 1)],
            unique=True,
        )
        await database.setgroup_map.create_index(
            [("user_id", 1), ("forwarded_message_id", 1)]
        )
        await database.setgroup_map.create_index(
            [("user_id", 1), ("forwarded_saved_message_id", 1)]
        )
        # Permanent group-to-target configuration. One target can be mapped once
        # per group for each bot user; re-adding it updates this record.
        await database.target_mappings.create_index(
            [("user_id", 1), ("group_chat_id", 1), ("target_user_id", 1)],
            unique=True,
        )
        await database.target_mappings.create_index(
            [("user_id", 1), ("group_chat_id", 1)]
        )
        # Per-owner, per-group, per-participant conversation memory for AI mode.
        await database.ai_memory.create_index(
            [("user_id", 1), ("chat_id", 1), ("participant_id", 1)],
            unique=True,
        )
    except Exception as exc:
        client.close()
        _client = None
        logger.error(
            "MongoDB unavailable; using local fallback storage: %s",
            exc,
        )
        _db = _LocalDatabase(_local_path)
        return _db

    _client = client
    _db = database
    logger.info("Connected to MongoDB.")
    return database


def get_db() -> Any:
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db


# ── User record ────────────────────────────────────────────────────────────────

async def get_user(user_id: int) -> dict | None:
    return await get_db().users.find_one({"user_id": user_id})


async def upsert_user(user_id: int, data: dict) -> None:
    await get_db().users.update_one(
        {"user_id": user_id},
        {"$set": data},
        upsert=True,
    )


async def delete_user(user_id: int) -> None:
    await get_db().users.delete_one({"user_id": user_id})


# ── Session ────────────────────────────────────────────────────────────────────

async def save_session(user_id: int, session_string: str) -> None:
    await upsert_user(user_id, {"session_string": session_string, "active": True})


async def get_session(user_id: int) -> str | None:
    user = await get_user(user_id)
    return user.get("session_string") if user else None


# ── Settings ───────────────────────────────────────────────────────────────────

async def get_setting(user_id: int, key: str, default=None):
    user = await get_user(user_id)
    return user.get(key, default) if user else default


async def set_setting(user_id: int, key: str, value) -> None:
    await upsert_user(user_id, {key: value})


async def get_ai_memory(
    user_id: int,
    chat_id: int,
    participant_id: int,
    limit: int = 12,
) -> list[dict]:
    """Return recent AI conversation turns for one group participant."""
    document = await get_db().ai_memory.find_one(
        {
            "user_id": user_id,
            "chat_id": chat_id,
            "participant_id": participant_id,
        },
        {"_id": 0, "messages": 1},
    )
    return (document or {}).get("messages", [])[-limit:]


async def append_ai_memory(
    user_id: int,
    chat_id: int,
    participant_id: int,
    user_message: str,
    assistant_message: str,
    max_messages: int = 20,
) -> None:
    """Persist a bounded pair of user/assistant turns for future replies."""
    now = datetime.now(timezone.utc)
    await get_db().ai_memory.update_one(
        {
            "user_id": user_id,
            "chat_id": chat_id,
            "participant_id": participant_id,
        },
        {
            "$set": {"updated_at": now},
            "$setOnInsert": {
                "user_id": user_id,
                "chat_id": chat_id,
                "participant_id": participant_id,
                "created_at": now,
            },
            "$push": {
                "messages": {
                    "$each": [
                        {"role": "user", "content": user_message, "created_at": now},
                        {
                            "role": "assistant",
                            "content": assistant_message,
                            "created_at": now,
                        },
                    ],
                    "$slice": -max_messages,
                }
            },
        },
        upsert=True,
    )


# ── Permanent group-to-target mappings ─────────────────────────────────────────

async def get_target_mappings(
    user_id: int,
    group_chat_id: int | None = None,
    target_user_id: int | None = None,
) -> list[dict]:
    query: dict = {"user_id": user_id}
    if group_chat_id is not None:
        query["group_chat_id"] = group_chat_id
    if target_user_id is not None:
        query["target_user_id"] = target_user_id
    cursor = get_db().target_mappings.find(query, {"_id": 0})
    return await cursor.to_list(length=None)


async def get_target_mapping(
    user_id: int,
    group_chat_id: int,
    target_user_id: int,
) -> dict | None:
    return await get_db().target_mappings.find_one(
        {
            "user_id": user_id,
            "group_chat_id": group_chat_id,
            "target_user_id": target_user_id,
        },
        {"_id": 0},
    )


async def get_target_mapping_by_identifier(
    user_id: int,
    group_chat_id: int,
    identifier: str,
) -> dict | None:
    normalized = identifier.strip().lstrip("@").lower()
    query: dict = {
        "user_id": user_id,
        "group_chat_id": group_chat_id,
    }
    if normalized.lstrip("-").isdigit():
        query["target_user_id"] = int(normalized)
    else:
        query["target_username"] = normalized
    return await get_db().target_mappings.find_one(query, {"_id": 0})


async def upsert_target_mapping(
    user_id: int,
    group_chat_id: int,
    target: dict,
    group_title: str,
) -> bool:
    """Create or update one permanent group-to-target mapping.

    Returns True when a new mapping was created and False when an existing
    mapping was updated.
    """
    target_user_id = int(target["target_id"])
    existing = await get_target_mapping(user_id, group_chat_id, target_user_id)
    await get_db().target_mappings.update_one(
        {
            "user_id": user_id,
            "group_chat_id": group_chat_id,
            "target_user_id": target_user_id,
        },
        {
            "$set": {
                "target_username": (target.get("username") or "").lower(),
                "target_name": target.get("name") or str(target_user_id),
                "group_title": group_title,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "user_id": user_id,
                "group_chat_id": group_chat_id,
                "target_user_id": target_user_id,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )
    return existing is None


async def remove_target_mapping(
    user_id: int,
    group_chat_id: int,
    target_user_id: int,
) -> bool:
    result = await get_db().target_mappings.delete_one(
        {
            "user_id": user_id,
            "group_chat_id": group_chat_id,
            "target_user_id": target_user_id,
        }
    )
    return result.deleted_count > 0


async def remove_all_target_mappings(user_id: int) -> int:
    """Remove every permanent group-to-target mapping for one bot user."""
    result = await get_db().target_mappings.delete_many({"user_id": user_id})
    return int(result.deleted_count)


async def get_latest_mapped_target_message(
    user_id: int,
) -> tuple[dict, int] | None:
    """Return the newest tracked message across all permanent mappings."""
    mappings = await get_target_mappings(user_id)
    if not mappings:
        return None
    pair_filters = [
        {
            "target_id": int(mapping["target_user_id"]),
            "chat_id": int(mapping["group_chat_id"]),
        }
        for mapping in mappings
    ]
    doc = await get_db().group_messages.find_one(
        {"user_id": user_id, "$or": pair_filters},
        {"_id": 0},
        sort=[("updated_at", -1), ("message_id", -1)],
    )
    if not doc or doc.get("message_id") is None:
        return None
    mapping = next(
        (
            item
            for item in mappings
            if int(item["target_user_id"]) == int(doc["target_id"])
            and int(item["group_chat_id"]) == int(doc["chat_id"])
        ),
        None,
    )
    return (mapping, int(doc["message_id"])) if mapping else None


# ── Legacy target records retained for safe database compatibility ─────────────

async def get_targets(user_id: int) -> list[dict]:
    user = await get_user(user_id)
    return user.get("targets", []) if user else []


async def add_target(user_id: int, target: dict) -> bool:
    """Add a target if not already present. Returns True if added, False if duplicate."""
    user = await get_user(user_id)
    existing = user.get("targets", []) if user else []
    for t in existing:
        if t["target_id"] == target["target_id"]:
            return False
    await get_db().users.update_one(
        {"user_id": user_id},
        {"$push": {"targets": target}},
        upsert=True,
    )
    return True


async def remove_target(
    user_id: int,
    identifier: str,
    resolved_target_id: int | None = None,
) -> bool:
    """Remove target by username (with or without @) or resolved numeric ID."""
    user = await get_user(user_id)
    if not user:
        return False
    targets = user.get("targets", [])
    ident = identifier.lstrip("@").lower()
    new_targets = [
        t for t in targets
        if not (
            (
                resolved_target_id is not None
                and t.get("target_id") == resolved_target_id
            )
            or (
                resolved_target_id is None
                and (
                    str(t.get("target_id")) == ident
                    or (t.get("username") or "").lower() == ident
                )
            )
        )
    ]
    if len(new_targets) == len(targets):
        return False
    await get_db().users.update_one(
        {"user_id": user_id},
        {"$set": {"targets": new_targets}},
    )
    return True


async def clear_targets(user_id: int) -> None:
    await upsert_user(user_id, {"targets": []})


async def get_target(user_id: int, target_id: int) -> dict | None:
    """Return one stored target by its stable Telegram user ID."""
    targets = await get_targets(user_id)
    return next((t for t in targets if t.get("target_id") == target_id), None)


async def update_target_last_message(
    user_id: int,
    target_id: int,
    chat_id: int,
    message_id: int,
    message_date: datetime | None = None,
) -> None:
    """
    Store the latest message from a target user in a specific group.
    Keyed by (user_id, target_id, chat_id) so each group is tracked independently.
    """
    await get_db().group_messages.update_one(
        {"user_id": user_id, "target_id": target_id, "chat_id": chat_id},
        {
            "$set": {
                "message_id": message_id,
                "updated_at": message_date or datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


async def get_target_message_in_chat(
    user_id: int, target_id: int, chat_id: int
) -> int | None:
    """
    Return the latest message_id from target_id in chat_id, or None if not seen yet.
    """
    doc = await get_db().group_messages.find_one(
        {"user_id": user_id, "target_id": target_id, "chat_id": chat_id}
    )
    return doc["message_id"] if doc else None


async def get_latest_target_in_chat(
    user_id: int, chat_id: int
) -> tuple[dict, int] | None:
    """
    Return the active target with the newest tracked message in one group.

    Target membership is read from the user's current target list, so removed
    targets cannot be selected even if an old group_messages record remains.
    Telegram message IDs increase within a chat and therefore provide the
    existing latest-message ordering without changing the stored schema.
    """
    targets = await get_targets(user_id)
    target_by_id = {
        int(target["target_id"]): target
        for target in targets
        if target.get("target_id") is not None
    }
    if not target_by_id:
        return None

    doc = await get_db().group_messages.find_one(
        {
            "user_id": user_id,
            "chat_id": chat_id,
            "target_id": {"$in": list(target_by_id)},
        },
        sort=[("message_id", -1)],
    )
    if not doc or doc.get("message_id") is None:
        return None

    target = target_by_id.get(int(doc["target_id"]))
    if target is None:
        return None
    return target, int(doc["message_id"])


# ── SetGroup ───────────────────────────────────────────────────────────────────

async def set_active_group(user_id: int, chat_id: int) -> None:
    """Persist the one active group for the setgroup feature."""
    await upsert_user(user_id, {"active_group": chat_id})


async def get_active_group(user_id: int) -> int | None:
    """Return the active group chat_id, or None if not set."""
    user = await get_user(user_id)
    return user.get("active_group") if user else None


async def clear_active_group(user_id: int) -> None:
    """Remove the active group so a later target add cannot reuse it."""
    await get_db().users.update_one(
        {"user_id": user_id},
        {"$unset": {"active_group": ""}},
    )


async def save_setgroup_mapping(
    user_id: int,
    original_chat_id: int,
    original_message_id: int,
    forwarded_message_id: int,
    target_id: int,
    active_group_id: int,
) -> None:
    """
    Store the complete Saved Messages reply bridge mapping.
    """
    await get_db().setgroup_map.update_one(
        {"user_id": user_id, "saved_msg_id": forwarded_message_id},
        {"$set": {
            "original_chat_id": original_chat_id,
            "original_message_id": original_message_id,
            "forwarded_saved_message_id": forwarded_message_id,
            "target_user_id": target_id,
            "active_group_id": active_group_id,
            "reply_sent": False,
            # Legacy aliases retained for existing records and indexes.
            "forwarded_message_id": forwarded_message_id,
            "saved_msg_id": forwarded_message_id,
            "target_id": target_id,
        }},
        upsert=True,
    )


async def get_setgroup_mapping(user_id: int, saved_msg_id: int) -> dict | None:
    """
    Return a normalized mapping for a Saved Messages message, or None.

    The legacy field names are accepted so an existing database does not break
    while old mappings are being cleaned up.
    """
    collection = get_db().setgroup_map
    doc = await collection.find_one(
        {
            "user_id": user_id,
            "$or": [
                {"forwarded_saved_message_id": saved_msg_id},
                {"forwarded_message_id": saved_msg_id},
                {"saved_msg_id": saved_msg_id},
            ],
        },
        {"_id": 0},
    )
    if not doc:
        return None
    return {
        "original_chat_id": doc.get("original_chat_id", doc.get("group_chat_id")),
        "original_message_id": doc.get(
            "original_message_id", doc.get("group_msg_id")
        ),
        "forwarded_saved_message_id": doc.get(
            "forwarded_saved_message_id",
            doc.get("forwarded_message_id", doc.get("saved_msg_id", saved_msg_id)),
        ),
        "target_user_id": doc.get("target_user_id", doc.get("target_id")),
        "active_group_id": doc.get(
            "active_group_id", doc.get("group_chat_id")
        ),
        "reply_sent": bool(doc.get("reply_sent", False)),
    }


async def get_setgroup_mappings(user_id: int) -> list[dict]:
    """Load all Saved Messages bridge mappings for a hosted user."""
    cursor = get_db().setgroup_map.find(
        {"user_id": user_id},
        {"_id": 0},
    )
    docs = await cursor.to_list(length=None)
    mappings: list[dict] = []
    for doc in docs:
        forwarded_id = doc.get(
            "forwarded_saved_message_id",
            doc.get("forwarded_message_id", doc.get("saved_msg_id")),
        )
        original_chat_id = doc.get("original_chat_id", doc.get("group_chat_id"))
        original_message_id = doc.get(
            "original_message_id", doc.get("group_msg_id")
        )
        if (
            forwarded_id is None
            or original_chat_id is None
            or original_message_id is None
            or doc.get("target_user_id", doc.get("target_id")) is None
        ):
            continue
        mappings.append(
            {
                "original_chat_id": original_chat_id,
                "original_message_id": original_message_id,
                "forwarded_saved_message_id": forwarded_id,
                "target_user_id": doc.get("target_user_id", doc.get("target_id")),
                "active_group_id": doc.get(
                    "active_group_id", doc.get("group_chat_id")
                ),
                "reply_sent": bool(doc.get("reply_sent", False)),
            }
        )
    return mappings


async def mark_setgroup_reply_sent(
    user_id: int,
    forwarded_saved_message_id: int,
    reply_message_id: int,
) -> None:
    """Persist that a Saved Messages reply was already bridged successfully."""
    await get_db().setgroup_map.update_one(
        {
            "user_id": user_id,
            "$or": [
                {"forwarded_saved_message_id": forwarded_saved_message_id},
                {"forwarded_message_id": forwarded_saved_message_id},
                {"saved_msg_id": forwarded_saved_message_id},
            ],
        },
        {
            "$set": {
                "reply_sent": True,
                "reply_message_id": reply_message_id,
            }
        },
    )


async def clear_monitoring_data(
    user_id: int,
    target_id: int | None = None,
    group_chat_id: int | None = None,
) -> list[int]:
    """
    Remove cached target messages and Saved Messages bridge mappings.

    Returns forwarded Saved Messages IDs so the caller can remove the
    corresponding messages from Saved Messages as well.
    """
    target_filter: dict = {"user_id": user_id}
    if target_id is not None:
        target_filter["target_id"] = target_id

    await get_db().group_messages.delete_many(target_filter | (
        {"chat_id": group_chat_id} if group_chat_id is not None else {}
    ))

    mapping_filter: dict = {"user_id": user_id}
    if target_id is not None:
        mapping_filter["target_id"] = target_id
    if group_chat_id is not None:
        mapping_filter["$or"] = [
            {"active_group_id": group_chat_id},
            {"original_chat_id": group_chat_id},
            {"group_chat_id": group_chat_id},
        ]

    cursor = get_db().setgroup_map.find(
        mapping_filter,
        {
            "_id": 0,
            "forwarded_saved_message_id": 1,
            "forwarded_message_id": 1,
            "saved_msg_id": 1,
        },
    )
    docs = await cursor.to_list(length=None)
    forwarded_ids = [
        int(
            doc["forwarded_saved_message_id"]
            if doc.get("forwarded_saved_message_id") is not None
            else (
                doc["forwarded_message_id"]
                if doc.get("forwarded_message_id") is not None
                else doc["saved_msg_id"]
            )
        )
        for doc in docs
        if doc.get("forwarded_saved_message_id") is not None
        or doc.get("forwarded_message_id") is not None
        or doc.get("saved_msg_id") is not None
    ]
    await get_db().setgroup_map.delete_many(mapping_filter)
    return forwarded_ids


# ── Bulk load ──────────────────────────────────────────────────────────────────

async def get_all_active_users() -> list[dict]:
    """Return all users with an active session."""
    cursor = get_db().users.find({"active": True, "session_string": {"$exists": True}})
    return await cursor.to_list(length=None)
