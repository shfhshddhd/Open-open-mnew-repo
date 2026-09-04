"""Lightweight aiohttp server for the Telegram Mini App."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from aiohttp import web

import config
from .auth import (
    MiniAppAuthError,
    issue_ticket,
    validate_init_data,
    verify_ticket,
)

logger = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).with_name("static")


class MiniAppServer:
    """Owns the Mini App HTTP server and keeps all account state server-side."""

    _MAX_REQUEST_BYTES = 64 * 1024

    def __init__(self, manager):
        self.manager = manager
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def is_running(self) -> bool:
        return self._runner is not None and self._site is not None

    async def start(self) -> bool:
        if self.is_running:
            return True
        if not config.SESSION_SECRET:
            logger.error(
                "Mini App disabled: SESSION_SECRET is not available in the bot workflow."
            )
            return False

        app = web.Application(client_max_size=self._MAX_REQUEST_BYTES)
        app.router.add_get(config.MINI_APP_PATH, self._index)
        base_path = config.MINI_APP_PATH.rstrip("/")
        app.router.add_get(base_path, self._redirect_to_index)
        app.router.add_post(f"{base_path}/api/auth", self._authenticate)
        app.router.add_get(f"{base_path}/api/status", self._status)
        app.router.add_get(f"{base_path}/api/live", self._live_audio)
        app.router.add_get(f"{base_path}/favicon.ico", self._favicon)
        app.router.add_static(f"{base_path}/static/", _STATIC_DIR, show_index=False)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "0.0.0.0", config.WEBAPP_PORT)
            await site.start()
        except Exception:
            await runner.cleanup()
            raise

        self._runner = runner
        self._site = site
        logger.info(
            "Telegram Mini App listening on 0.0.0.0:%s at %s",
            config.WEBAPP_PORT,
            config.MINI_APP_PATH,
        )
        if not config.mini_app_url():
            logger.warning(
                "MINI_APP_URL is not configured and no Replit domain was detected; "
                "the /start Web App button will be omitted."
            )
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    async def _favicon(self, _request: web.Request) -> web.StreamResponse:
        return web.Response(status=204)

    async def _redirect_to_index(self, _request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(config.MINI_APP_PATH)

    async def _authenticate(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise MiniAppAuthError("Authentication payload is malformed.")
            init_data = str(body.get("initData") or "")
            user = validate_init_data(init_data, config.BOT_TOKEN)
            if not self.manager.is_hosted(user.user_id):
                return web.json_response(
                    {
                        "ok": False,
                        "code": "not_hosted",
                        "message": "No active hosted Telegram account is connected.",
                    },
                    status=403,
                )
            ticket = issue_ticket(user, config.SESSION_SECRET)
            return web.json_response({"ok": True, "ticket": ticket})
        except (MiniAppAuthError, json.JSONDecodeError, ValueError, TypeError):
            return web.json_response(
                {"ok": False, "code": "unauthorized", "message": "Telegram authentication failed."},
                status=401,
            )

    async def _status(self, request: web.Request) -> web.Response:
        try:
            user_id = verify_ticket(
                self._bearer_token(request),
                config.SESSION_SECRET,
            )
        except MiniAppAuthError:
            return web.json_response(
                {"ok": False, "code": "unauthorized", "message": "Authorization expired."},
                status=401,
            )

        # A ticket is deliberately stateless, so re-check the live hosted-user
        # mapping on every request. This immediately revokes access after
        # /unhost or a session disconnect instead of trusting an old ticket.
        if not self.manager.is_hosted(user_id):
            return web.json_response(
                {
                    "ok": False,
                    "code": "not_hosted",
                    "message": "No active hosted Telegram account is connected.",
                },
                status=403,
            )

        hosted = self.manager.get_client(user_id)
        is_hosted = hosted is not None and hosted.is_running()
        payload = {
            "ok": True,
            "telegram_user_id": user_id,
            "hosted": is_hosted,
            "session_active": is_hosted,
            "voice_chat": {
                "connected": False,
                "chat_id": None,
                "title": None,
                "playing": False,
                "queued": 0,
                "volume": 100,
                "muted": False,
                "live": {
                    "active": False,
                    "mic_enabled": False,
                    "push_to_talk": False,
                    "push_active": False,
                    "frames": 0,
                    "bytes": 0,
                    "started_at": None,
                    "last_frame_at": None,
                },
            },
        }
        if not is_hosted:
            return web.json_response(payload)

        voice = getattr(hosted.client, "_voice_chat_manager", None)
        state = getattr(voice, "state", None)
        if state is not None:
            payload["voice_chat"] = {
                "connected": True,
                "chat_id": state.chat_id,
                "title": state.chat_title or "Voice Chat",
                "playing": state.current is not None,
                "queued": len(state.queue),
                "volume": state.volume,
                "muted": state.muted,
                "live": voice.live_snapshot(state.chat_id),
            }
        return web.json_response(payload)

    async def _live_audio(self, request: web.Request) -> web.StreamResponse:
        """Authenticate a Mini App WebSocket and forward real PCM16L frames."""
        websocket = web.WebSocketResponse(
            heartbeat=30,
            max_msg_size=96 * 1024,
        )
        await websocket.prepare(request)

        voice = None
        started_here = False
        user_id: int | None = None
        receive_queue = None
        receive_task = None

        async def send_error(message: str) -> None:
            if not websocket.closed:
                await websocket.send_json({"type": "error", "message": message})

        async def send_state() -> None:
            if voice is None or websocket.closed:
                return
            state = getattr(voice, "state", None)
            snapshot = voice.live_snapshot(state.chat_id if state else None)
            await websocket.send_json(
                {
                    "type": "state",
                    "voice_connected": state is not None,
                    "chat_id": state.chat_id if state else None,
                    "volume": state.volume if state else 100,
                    "live": snapshot,
                }
            )

        try:
            try:
                auth_message = await websocket.receive(timeout=10)
            except asyncio.TimeoutError:
                await send_error("Live audio authentication timed out.")
                return websocket
            if auth_message.type != web.WSMsgType.TEXT:
                await send_error("The live audio session must start with authentication.")
                return websocket
            try:
                auth_payload = json.loads(auth_message.data)
            except (json.JSONDecodeError, TypeError):
                await send_error("Live audio authentication payload is malformed.")
                return websocket
            if auth_payload.get("type") != "auth":
                await send_error("Live audio authentication is required.")
                return websocket

            try:
                user_id = verify_ticket(
                    str(auth_payload.get("ticket") or ""),
                    config.SESSION_SECRET,
                )
            except MiniAppAuthError:
                await send_error("Authorization expired. Reconnect the Mini App.")
                return websocket

            if not self.manager.is_hosted(user_id):
                await send_error("The hosted Telegram account is no longer active.")
                return websocket
            hosted = self.manager.get_client(user_id)
            voice = getattr(hosted.client, "_voice_chat_manager", None) if hosted else None
            if voice is None:
                await send_error("Voice Chat is not available for this hosted account.")
                return websocket

            await websocket.send_json({"type": "ready"})
            await send_state()
            state = getattr(voice, "state", None)
            if state is not None:
                receive_queue = voice.subscribe_receive(state.chat_id)

                async def send_received_audio() -> None:
                    while not websocket.closed:
                        payload = await receive_queue.get()
                        if payload and not websocket.closed:
                            await websocket.send_bytes(payload)

                receive_task = asyncio.create_task(send_received_audio())

            async for message in websocket:
                if message.type == web.WSMsgType.BINARY:
                    if len(message.data) > 64 * 1024:
                        await send_error("Audio frame is too large.")
                        continue
                    state = getattr(voice, "state", None)
                    if state is None or not state.live_active:
                        continue
                    try:
                        await voice.send_live_frame(state.chat_id, bytes(message.data))
                    except Exception as exc:
                        logger.warning(
                            "Live microphone frame forwarding failed for user %s: %s",
                            user_id,
                            exc,
                        )
                        await send_error("Telegram rejected the live audio frame.")
                        break
                elif message.type == web.WSMsgType.TEXT:
                    try:
                        command = json.loads(message.data)
                    except (json.JSONDecodeError, TypeError):
                        await send_error("Live audio command is malformed.")
                        continue
                    command_type = str(command.get("type") or "").lower()
                    state = getattr(voice, "state", None)
                    try:
                        if command_type == "start":
                            if state is None:
                                raise RuntimeError(
                                    "Join an active Voice Chat first with .vcjoin."
                                )
                            await voice.start_live(state.chat_id)
                            started_here = True
                        elif command_type == "stop":
                            if state is not None:
                                await voice.stop_live(state.chat_id)
                            started_here = False
                        elif command_type == "gain":
                            if state is None:
                                raise RuntimeError(
                                    "Join an active Voice Chat first with .vcjoin."
                                )
                            value = int(command.get("value"))
                            await voice.change_volume(state.chat_id, value)
                        elif command_type == "controls":
                            if state is None:
                                raise RuntimeError(
                                    "Join an active Voice Chat first with .vcjoin."
                                )
                            voice.set_live_controls(
                                state.chat_id,
                                mic_enabled=command.get("mic_enabled"),
                                push_to_talk=command.get("push_to_talk"),
                                push_active=command.get("push_active"),
                            )
                        elif command_type == "ping":
                            await websocket.send_json({"type": "pong"})
                        else:
                            await send_error("Unknown live audio command.")
                            continue
                    except (TypeError, ValueError):
                        await send_error("Gain must be a valid number.")
                        continue
                    except Exception as exc:
                        await send_error(str(exc))
                        continue
                    await send_state()
                elif message.type in {
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSED,
                    web.WSMsgType.ERROR,
                }:
                    break
        finally:
            if receive_task is not None:
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            if receive_queue is not None and voice is not None:
                state = getattr(voice, "state", None)
                if state is not None:
                    voice.unsubscribe_receive(state.chat_id, receive_queue)
            if started_here and voice is not None:
                state = getattr(voice, "state", None)
                if state is not None and state.live_active:
                    with contextlib.suppress(Exception):
                        await voice.stop_live(state.chat_id)
            logger.info("Mini App live audio session closed for user %s.", user_id)
        return websocket

    @staticmethod
    def _bearer_token(request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""