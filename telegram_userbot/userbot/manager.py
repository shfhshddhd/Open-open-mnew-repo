"""
Manages all active per-user Telethon userbot sessions.
"""
import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
import config
import database.mongo as db
from userbot.client import UserbotClient
from telethon.tl.types import User

logger = logging.getLogger(__name__)


class UserbotManager:
    def __init__(self):
        # Maps bot user_id → UserbotClient
        self._clients: dict[int, UserbotClient] = {}

    # ── Startup ────────────────────────────────────────────────────────────────

    async def start_all(self) -> None:
        """Load all active sessions from DB and start them."""
        try:
            users = await db.get_all_active_users()
        except Exception as exc:
            logger.warning(
                "Could not restore hosted userbots because the database is unavailable: %s",
                exc,
            )
            return
        tasks = [self._start_one(user["user_id"], user["session_string"]) for user in users]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for user, result in zip(users, results):
            if isinstance(result, Exception):
                logger.error("Failed to start userbot for %s: %s", user["user_id"], result)
        logger.info("Started %d userbot(s).", len(self._clients))

    async def _start_one(self, user_id: int, session_string: str) -> None:
        client = UserbotClient(user_id, session_string)
        client.manager = self
        await client.start()
        if client.is_running():
            self._clients[user_id] = client

    # ── Public API ─────────────────────────────────────────────────────────────

    async def add_session(self, user_id: int, session_string: str) -> None:
        """Persist and start a newly authenticated userbot."""
        if user_id in self._clients:
            await self._clients[user_id].stop()
        await db.save_session(user_id, session_string)
        await self._start_one(user_id, session_string)

    async def remove_session(self, user_id: int) -> None:
        """Stop and remove a userbot session."""
        if user_id in self._clients:
            await self._clients[user_id].stop()
            del self._clients[user_id]
        await db.delete_user(user_id)

    def is_hosted(self, user_id: int) -> bool:
        return user_id in self._clients and self._clients[user_id].is_running()

    def get_client(self, user_id: int) -> UserbotClient | None:
        return self._clients.get(user_id)

    # ── Telethon auth helpers (used by /host flow) ─────────────────────────────

    async def begin_auth(
        self,
        user_id: int,
        phone: str,
    ) -> tuple[TelegramClient, str, str]:
        """
        Start phone-number sign-in.
        Returns (client, phone_code_hash, delivery_type).
        The caller must later complete authentication.
        """
        client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        try:
            await client.connect()
            result = await client.send_code_request(phone)
        except Exception:
            # Do not leave an unauthenticated connection behind when Telegram
            # rejects the request (for example after a flood wait).
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        delivery_type = type(result.type).__name__
        logger.info(
            "Telegram login code requested successfully using delivery type %s.",
            delivery_type,
        )
        return client, result.phone_code_hash, delivery_type

    async def sign_in_with_code(
        self,
        user_id: int,
        client: TelegramClient,
        phone: str,
        phone_code_hash: str,
        otp: str,
    ) -> str:
        """
        Sign in with the OTP code.
        Returns the session string on success.
        Raises SessionPasswordNeededError if 2FA password is still required.
        The caller must store the client and call sign_in_with_password() next.
        """
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        # Reached here → auth complete, no 2FA needed
        session_string = client.session.save()
        await self.add_session(user_id, session_string)
        await client.disconnect()
        return session_string

    async def sign_in_with_password(
        self,
        user_id: int,
        client: TelegramClient,
        password: str,
    ) -> str:
        """
        Complete 2FA sign-in after SessionPasswordNeededError was raised.
        Returns the session string on success.
        """
        await client.sign_in(password=password)
        session_string = client.session.save()
        await self.add_session(user_id, session_string)
        await client.disconnect()
        return session_string

    async def resolve_target(self, user_id: int, identifier: str) -> dict | None:
        """
        Use the user's own Telethon client to resolve a username / user_id.
        Returns a target dict or None if not resolvable.
        """
        uc = self.get_client(user_id)
        if uc is None:
            return None
        try:
            normalized = identifier.strip()
            entity_ref: int | str
            if normalized.lstrip("-").isdigit():
                entity_ref = int(normalized)
            else:
                entity_ref = normalized.lstrip("@")
            try:
                entity = await uc.client.get_entity(entity_ref)
            except Exception:
                if not isinstance(entity_ref, int):
                    raise
                entity = await self._find_numeric_target(uc.client, entity_ref)
                if entity is None:
                    raise
            if not isinstance(entity, User):
                raise ValueError("The target identifier does not belong to a user.")
            name = " ".join(
                filter(None, [getattr(entity, "first_name", None), getattr(entity, "last_name", None)])
            ).strip() or str(entity.id)
            return {
                "target_id": entity.id,
                "username": getattr(entity, "username", None) or "",
                "name": name,
                "last_chat_id": None,
                "last_message_id": None,
            }
        except Exception as exc:
            logger.warning("Could not resolve target '%s': %s", identifier, exc)
            return None

    async def _find_numeric_target(self, client: TelegramClient, target_id: int):
        """
        Find a numeric user ID in the hosted account's dialogs when it is not
        already present in Telethon's entity cache. This supports users without
        usernames who share a group with the hosted account.
        """
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if getattr(entity, "id", None) == target_id:
                return entity
            if not getattr(entity, "megagroup", False) and not getattr(
                entity, "broadcast", False
            ):
                continue
            try:
                async for participant in client.iter_participants(entity):
                    if getattr(participant, "id", None) == target_id:
                        return participant
            except Exception as exc:
                logger.debug(
                    "Could not inspect participants in dialog %s: %s",
                    getattr(entity, "id", None),
                    exc,
                )
        return None
