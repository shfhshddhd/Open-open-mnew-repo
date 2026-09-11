"""Private VC control group setup and VC-to-VC audio forwarding bridge.

This module adapts the VC-to-VC functionality from the friend reference bot into
the master bot's native PyTgCalls pipeline. It does NOT add a second Telegram
client, a second PyTgCalls instance, or a competing audio pipeline.  It
reuses the per-hosted-account ``VoiceChatManager`` from ``plugins.voice_chat``:

the same ``PyTgCalls`` binding hosts the main Voice Chat connection (the
"source"/personal VC) and one or more target Voice Chat connections.  The
native ``StreamFrames`` INCOMING/SPEAKER frames received from the source VC are
forwarded into the target VC via the native ``send_frame(..., Device.MICROPHONE, pcm)``
external-microphone path (the same path the Live Mic Mini App already uses).

Private control group authorization: only the registered private group can
issue the VC-to-VC commands; allowed senders are the owned account owner (or a
group administrator of that private group);the original owner's hosted session
is always the account that executes the commands.

The ``.privategroupvcsetup`` command lives here too, as a Telethon dot|
command usable by the hosted account after /host.  The private-group slash
commands are handled by ``bot.handlers.private_vc_control`` which dispatches
into this bridge manager.
"""


from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from html import escape
import logging
import math
import shutil
import struct
import tempfile
import time
from array import array
from pathlib import Path

from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import (
    Device,
    ExternalMedia,
    MediaStream,
)
from pytgcalls.types.raw import AudioParameters
from telethon import events
from telethon.tl import types as tl_types
from telethon.utils import get_peer_id

import database.mongo as db
from plugins.bot import add_handler

logger = logging.getLogger(__name__)

MIN_LEVEL, MAX_LEVEL = 1, 25
MIN_BASS, MAX_BASS = 0, 15
DEFAULT_LEVEL = 5
DEFAULT_BASS = 0

_BRIDGE_SAMPLE_RATE = 48000
_BRIDGE_CHANNELS = 1
_BASS_FREQ = 150.0

SETUP_STEP_TEXT_TEMPLATE = (
    "*Private VC Control Group setup*\n\n"
    "1. Create a **new private Telegram group** (make it private).\n"
    "2. Add my bot to that group: `@{bot_username}`\n"
    "3. Give the bot **admin rights** in that group.\n"
    "4. (Recommended) Add your hosted Telegram account to that group.\n"
    "5. Send me the private group's chat ID or @username as your next message.\n\n"
    "The numeric chat ID looks like `-1001234567890`; you can get it from\n"
    "@userinfobot or by forwarding a message from the group to him.\n\n"
    "Send /cancel to abort."
)


@dataclass
class BridgeSession:
    target_chat_id: int
    target_title: str = field(default="")
    source_chat_id: int = field(default=0)
    level: int = field(default=DEFAULT_LEVEL)
    bass: int = field(default=DEFAULT_BASS)
    muted: bool = field(default=False)
    recording: bool = field(default=False)
    recording_handle: object | None = field(default=None)
    recording_path: Path | None = field(default=None)
    recording_bytes: int = field(default=0)
    send_task: asyncio.Task | None = field(default=None)
    input_queue: asyncio.Queue[bytes] | None = field(default=None)
    frames_forwarded: int = field(default=0)
    bytes_forwarded: int = field(default=0)
    last_frame_at: float | None = field(default=None)
    reconnect_delay: float = field(default=2.0)
    error_count: int = field(default=0)
    stopping: bool = field(default=False)
    bass_coeffs: tuple | None = field(default=None)
    bx1: float = field(default=0.0)
    bx2: float = field(default=0.0)
    by1: float = field(default=0.0)
    by2: float = field(default=0.0)


class VCBridgeManager:

    def __init__(self, voice):
        self.voice = voice
        self.calls = voice.calls
        self.client = voice.client
        self.sessions: dict[int, BridgeSession] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._temp_dir = Path(tempfile.mkdtemp(prefix="vc-bridge-"))

    async def start(self):
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(
            self._watchdog(),
            name="vc-bridge-watchdog",
        )
        self._watchdog_task.add_done_callback(self._watchdog_done)

    def _watchdog_done(self, task):
        if task.cancelled():
            self._watchdog_task = None
            return
        with contextlib.suppress(Exception):
            exc = task.exception()
            if exc is not None:
                logger.error("VC bridge watchdog stopped: %s", exc)
        self._watchdog_task = None

    async def _watchdog(self):
        while True:
            await asyncio.sleep(10)
            for target_chat_id in list(self.sessions.keys()):
                session = self.sessions.get(target_chat_id)
                if session is None or session.stopping:
                    continue
                try:
                    if session.send_task is None or session.send_task.done():
                        session.send_task = asyncio.create_task(
                            self._run_session(session),
                            name=f"vc-bridge-send-{target_chat_id}",
                        )
                    if not await self._is_in_call(target_chat_id):
                        await self._join_target(session)
                        logger.warning(
                            "VC bridge watchdog reconnected target %s (source %s).",
                            target_chat_id,
                            session.source_chat_id,
                        )
                    else:
                        session.error_count = 0
                        session.reconnect_delay = 2.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    session.error_count += 1
                    logger.warning(
                        "VC bridge watchdog error for target %s: %s",
                        target_chat_id,
                        exc,
                    )

    async def _is_in_call(self, chat_id):
        try:
            calls = await self.calls.calls
            return int(chat_id) in calls
        except Exception:
            return False

    def attach_hooks(self):
        if not hasattr(self.voice, "_vc_bridge") or self.voice._vc_bridge is not self:
            self.voice._vc_bridge = self

    def on_source_frames(self, chat_id: int, payload: bytes):
        if not payload or not self.sessions:
            return
        for session in tuple(self.sessions.values()):
            if (
                session.stopping
                or session.source_chat_id != int(chat_id)
                or session.input_queue is None
            ):
                continue
            queue = session.input_queue
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)
    # ---------- command surface ----------

    async def join(self, target_identifier: str, source_chat_id: int) -> str:
        if not target_identifier:
            raise ValueError(
                "Usage: /join <target group username or chat ID>\n"
                "Joins the target group's Voice Chat and forwards live audio from "
                "the source Voice Chat you joined with .vcjoin."
            )
        await self.voice.start()
        try:
            entity = await self.client.get_entity(
                int(target_identifier) if target_identifier.lstrip("-").isdigit() else target_identifier.lstrip("@")
            )
        except Exception as exc:
            raise ValueError(
                "Could not find that group. Use a group username or numeric chat ID "
                "visible to the hosted account."
            ) from exc

        if (
            not isinstance(entity, tl_types.Chat)
            and not (
                isinstance(entity, tl_types.Channel)
                and bool(getattr(entity, "megagroup", False))
            )
        ):
            raise ValueError("The target must be a group or supergroup.")

        target_chat_id = int(get_peer_id(entity))
        if target_chat_id == int(source_chat_id):
            raise ValueError("The target Voice Chat must differ from the source Voice Chat.")

        if target_chat_id in self.sessions:
            session = self.sessions[target_chat_id]
            return (
                f"Already forwarding into **{escape(session.target_title)}** "
                f"(`{target_chat_id}`)."
            )

        if await self._is_in_call(target_chat_id):
            with contextlib.suppress(Exception):
                await self.calls.leave_call(target_chat_id)



        session = BridgeSession(
            target_chat_id=target_chat_id,
            target_title=_safe_title(getattr(entity, "title", None)),
            source_chat_id=int(source_chat_id),
        )
        self.sessions[target_chat_id] = session
        try:
            await self._join_target(session)
        except Exception:
            self.sessions.pop(target_chat_id, None)
            raise
        session.input_queue = asyncio.Queue(maxsize=8)
        session.send_task = asyncio.create_task(
            self._run_session(session),
            name=f"vc-bridge-send-{target_chat_id}",
        )
        await self.start()
        return (
            f"Joined Voice Chat **{escape(session.target_title)}** `{target_chat_id}`\n"
            f"Now forwarding live audio from source Voice Chat "
            f"(`{session.source_chat_id}`) into it."
        )

    async def _join_target(self, session):
        entity = await self.client.get_entity(session.target_chat_id)
        active_call = await self.voice._active_group_call(entity)
        if active_call is None:
            raise NoActiveGroupCall(
                f"No active Voice Chat found in target chat {session.target_chat_id}. "
                "Start the Voice Chat there first."
            )
        stream = MediaStream(
            ExternalMedia.AUDIO,
            AudioParameters(bitrate=_BRIDGE_SAMPLE_RATE, channels=_BRIDGE_CHANNELS),
            audio_flags=MediaStream.Flags.REQUIRED,
            video_flags=MediaStream.Flags.IGNORE,
        )
        await self.calls.play(session.target_chat_id, stream)

    async def leave(self, target_chat_id: int) -> str:
        session = self.sessions.pop(int(target_chat_id), None)
        if session is None:
            raise ValueError("No active forwarding session found for that target.")
        await self._teardown_session(session)
        return f"Left Voice Chat `{target_chat_id}` and stopped forwarding."

    async def leave_playback_only(self, target_chat_id: int) -> str:
        return await self.leave(target_chat_id)



    async def leave_all(self) -> int:
        targets = list(self.sessions.keys())
        for target_chat_id in targets:
            with contextlib.suppress(Exception):
                await self.leave(target_chat_id)




        return len(targets)

    # ---------- audio settings ----------

    def _single_target(self):
        if len(self.sessions) == 1:
            return next(iter(self.sessions.keys()))
        return None

    def _resolve_session(self, requested):
        if requested and requested.strip():
            try:
                target_chat_id = int(requested.strip())
            except ValueError:
                raise ValueError("Target must be a numeric chat ID.")
            session = self.sessions.get(target_chat_id)
            if session is None:
                raise ValueError("No active forwarding session found for that target.")
            return session
        single = self._single_target()
        if single is None:
            raise ValueError("Multiple forwarding sessions are active. Specify a target chat ID.")
        return self.sessions[single]

    async def set_level(self, requested, value: int) -> str:
        if not MIN_LEVEL <= value <= MAX_LEVEL:
            raise ValueError(f"Level must be between {MIN_LEVEL}and {MAX_LEVEL}.")
        session = self._resolve_session(requested)
        session.level = value
        session.bx1 = 0.0
        session.bx2 = 0.0
        session.by1 = 0.0
        session.by2 = 0.0
        return f"Volume/level set to `{value}/{MAX_BASS}`."

    async def set_bass(self, requested, value: int) -> str:
        if not MIN_BASS <= value <= MAX_BASS:
            raise ValueError(f"Bass must be between {MIN_BASS}and {MAX_BASS}.")
        session = self._resolve_session(requested)
        session.bass = value
        session.bass_coeffs = None
        return f"Bass boost set to {value}/{MAX_BASS}."

    async def set_mute(self, requested, muted: bool) -> str:
        session = self._resolve_session(requested)
        session.muted = bool(muted)
        return "*Forwarding muted.*" if muted else "*Forwarding unmuted.*"

    # ---------- recording ----------

    async def start_recording(self, requested) -> str:
        session = self._resolve_session(requested)
        if session.recording:
            raise ValueError("A recording is already in progress for this session.")
        recording_dir = Path(tempfile.mkdtemp(prefix="bridge-rec-", dir=self._temp_dir))
        output = recording_dir / f"vc-bridge-{session.target_chat_id}.wav"
        session.recording = True
        session.recording_handle = _WavWriter(output)
        session.recording_path = output
        session.recording_bytes = 0
        return f"Recording started for target `{session.target_chat_id}`."

    async def stop_recording(self, requested, control_chat_id: int) -> str:
        session = self._resolve_session(requested)
        if not session.recording:
            raise ValueError("No recording is in progress for that session.")
        handle = session.recording_handle
        session.recording_handle = None
        session.recording = False
        path = session.recording_path
        session.recording_path = None
        if handle is not None:
            await asyncio.to_thread(handle.finish)
        if path is None or not path.exists() or path.stat().st_size <= 44:
            with contextlib.suppress(Exception):
                shutil.rmtree(path.parent, ignore_errors=True)
            raise RuntimeError("The recording did not produce audio.")
        try:
            await self._send_recording(path, control_chat_id, session.target_chat_id)



            return "Recording stopped and processed."
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    async def _send_recording(self, path, control_chat_id: int, target_chat_id: int):
        userbot_context = getattr(self.client, "_userbot_context", None)
        manager = getattr(userbot_context, "manager", None) if userbot_context is not None else None
        control_bot = getattr(manager, "control_bot", None) if manager is not None else None
        if control_bot is None:
            logger.warning("Cannot upload recording: control bot is unavailable.")
            return
        with contextlib.suppress(Exception):
            await control_bot.send_audio(
                chat_id=control_chat_id,
                audio=path,
                caption=f"Recorded forwarded audio for target `{target_chat_id}`.",
                parse_mode="Markdown",
            )
    # ---------- forwarding loop ----------

    async def _run_session(self, session):
        queue = session.input_queue
        if queue is None:
            return
        try:
            while not session.stopping:
                data = await queue.get()
                if not data or session.stopping:
                    continue
                if not await self._is_in_call(session.target_chat_id):
                    await self._join_target(session)
                pcm = self._process_audio(session, data)
                if not pcm:
                    continue
                try:
                    await self.calls.send_frame(
                        session.target_chat_id,
                        Device.MICROPHONE,
                        pcm,
                    )
                    session.frames_forwarded += 1
                    session.bytes_forwarded += len(pcm)
                    session.last_frame_at = time.monotonic()
                    if session.recording and session.recording_handle is not None:
                        with contextlib.suppress(Exception):
                            session.recording_handle.write(pcm)
                            session.recording_handle.flush()
                            session.recording_bytes += len(pcm)
                except NotInCallError:
                    with contextlib.suppress(Exception):
                        await self._join_target(session)
                except Exception as exc:
                    session.error_count += 1
                    logger.warning(
                        "VC bridge frame send failed for target %s: %s",
                        session.target_chat_id,
                        exc,
                    )
                    await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "VC bridge sender stopped for target %s.",
                session.target_chat_id,
            )

    def _process_audio(self, session, data: bytes) -> bytes:
        if not data:
            return b""
        samples = _to_mono(data)
        if not samples:
            return b""
        volume = session.level / 5.0
        if session.muted:
            volume = 0.0
        if volume != 1.0:
            samples = _apply_volume(samples, volume)
        if session.bass > 0:
            samples = _apply_bass(session, samples)
        return samples.tobytes()
    def speed_stats(self) -> dict:
        frames = sum(s.frames_forwarded for s in self.sessions.values())
        bytes_forwarded = sum(s.bytes_forwarded for s in self.sessions.values())
        last = None
        for s in self.sessions.values():
            if s.last_frame_at is not None and (last is None or s.last_frame_at > last):
                last = s.last_frame_at
        return {
            "sessions": len(self.sessions),
            "frames": frames,
            "bytes": bytes_forwarded,
            "last_frame_at": last,
        }


# ---------- teardown ----------

    async def _teardown_session(self, session):
        session.stopping = True
        if session.send_task is not None and session.send_task is not asyncio.current_task():
            session.send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.send_task
        with contextlib.suppress(Exception):
            await self.calls.leave_call(session.target_chat_id)
        if session.recording and session.recording_handle is not None:
            with contextlib.suppress(Exception):
                session.recording_handle.finish()
        session.recording = False
        if session.recording_path is not None:
            with contextlib.suppress(Exception):
                shutil.rmtree(session.recording_path.parent, ignore_errors=True)
        session.input_queue = None

    async def shutdown(self):
        for target_chat_id in list(self.sessions.keys()):
            session = self.sessions.pop(target_chat_id, None)
            if session is not None:
                with contextlib.suppress(Exception):
                    await self._teardown_session(session)
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        shutil.rmtree(self._temp_dir, ignore_errors=True)


class _WavWriter:
    def __init__(self, path: Path):
        self.path = path
        self.bytes_written = 0
        with open(path, "wb")as handle:
            handle.write(b"RIFF")
            handle.write(struct.pack("<I", 0))
            handle.write(b"WAVEfmt ")
            handle.write(
                struct.pack(
                    "<IHHIIHH",
                    16, 1, 1, _BRIDGE_SAMPLE_RATE,
                    _BRIDGE_SAMPLE_RATE * 1 * 2 // 8,
                    1 * 2,
                    16,
                )
            )
            handle.write(b"data")
            handle.write(struct.pack("<I", 0))
            self.data_offset = handle.tell()

    def write(self, data: bytes):
        if not data:
            return
        with open(self.path, "ab")as handle:
            handle.write(data)
            self.bytes_written += len(data)

    def flush(self):
        return None

    def finish(self):
        with open(self.path, "r+b")as handle:
            handle.seek(4)
            handle.write(struct.pack("<I", 36 + self.bytes_written))
            handle.seek(40)
            handle.write(struct.pack("<I", self.bytes_written))


def _safe_title(value) -> str:
    return " ".join((value or "").split()).strip()[:160] or "Voice Chat"

def _to_mono(data: bytes) -> array:
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return array("h")
    samples = array("h")
    samples.frombytes(data[:usable])
    if len(samples) >= 960 and len(samples) % 2 == 0:
        mono = array("h", [0]) * (len(samples) // 2)
        for index in range(0, len(samples), 2):
            left = samples[index]
            right = samples[index + 1]
            mono[index // 2] = (int(left) + int(right)) // 2
        return mono
    return samples


def _apply_volume(samples: array, volume: float) -> array:
    if volume <= 0:
        return array("h", [0]) * len(samples)
    if volume == 1.0:
        return samples
    out = array("h", [0]) * len(samples)
    for index, sample in enumerate(samples):
        amplified = int(round(sample * volume))
        out[index] = max(-32768, min(32767, amplified))
    return out



def _biquad_low_shelf_coeffs(gain_db: float):
    A =                            10.0 ** (gain_db / 40.0)
    w0 =                            2.0 * math.pi * (_BASS_FREQ / _BRIDGE_SAMPLE_RATE)
    alpha =                          math.sin(w0) / (2.0 * 0.7071)
    cos_w0 =                         math.cos(w0)
    sqrt_a =                          math.sqrt(A)
    a0 = (A +                         1) - (A -                         1) * cos_w0 +                         2 * sqrt_a * alpha
    inv_a0 =                         1.0 / a0
    b0 = A * ((A +                         1) - (A -                         1) * cos_w0 +                         2 * sqrt_a * alpha) * inv_a0
    b1 =                         2 * A * ((A -                         1) - (A +                         1) * cos_w0) * inv_a0
    b2 = A * ((A +                         1) - (A -                         1) * cos_w0 -                         2 * sqrt_a * alpha) * inv_a0
    a1 =                         -2 * ((A -                         1) - (A +                         1) * cos_w0) * inv_a0
    a2 = ((A +                         1) - (A -                         1) * cos_w0 -                         2 * sqrt_a * alpha) * inv_a0
    return (b0, b1, b2, a1, a2)

def _apply_bass(session, samples: array) -> array:
    gain = float(session.bass) * 1.5
    if gain ==                                                                                                                                         0:
        return samples
    if session.bass_coeffs is None:
        session.bass_coeffs = _biquad_low_shelf_coeffs(gain)
    b0, b1, b2, a1, a2 = session.bass_coeffs
    x0 = samples
    y0 = array("h", [0]) * len(x0)
    for index in range(len(x0)):
        previous1 = x0[index - 1] if index > 0 else 0
        previous2 = x0[index - 2] if index > 1 else                                                                                                                                                                                                                                                                                                                                                         0
        previous_y1 = y0[index - 1] if index > 0 else 0
        previous_y2 = y0[index - 2] if index > 1 else 0
        y0[index] = max(-32768, min(32767, int(
            b0 * x0[index] + b1 * previous1 + b2 * previous2
            - a1 * previous_y1 - a2 * previous_y2
        )))
    return y0

# ----------------------------------------------------------------------------
# Setup flow: .privategroupvcsetup -> capture control_chat_id -> persist mapping
# ----------------------------------------------------------------------------

_STEP_KEY = "_privategroupvcsetup_step"
_PENDING_KEY = "_privategroupvcsetup_pending"


def _format_chat_identifier(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.lstrip("-").isdigit():
        return str(int(value))
    return value.lstrip("@")


async def _ensure_direct_chat(event) -> bool:
    chat = getattr(event, "chat", None)
    if chat is None or getattr(chat, "id", None) is None:
        return False
    peer_type = type(chat).__name__.lower()
    return peer_type in ("user", "private")


async def _confirm_privategroup(client, chat_id: int) -> bool:
    try:
        entity = await client.get_entity(chat_id)
    except Exception:
        return False
    if isinstance(entity, tl_types.Chat):
        return True
    if (
        isinstance(entity, tl_types.Channel)
        and bool(getattr(entity, "megagroup", False))
        and not bool(getattr(entity, "broadcast", False))
    ):
            return True
    return False


def _register_setup_handlers(client):
    from utils.decorators import authorized_users_only

    @client.on(events.NewMessage(pattern=r"^\.privategroupvcsetup\s*$"))
    @authorized_users_only()
    async def privategroupvcsetup_handler(event):
        if not await _ensure_direct_chat(event):
            await event.reply(
                "Please run **.privategroupvcsetup** in a private chat with the bot."
            )
            return
        client._private_vc_setup_step = 1
        client._private_vc_setup_pending = True
        bot_username = getattr(client, "_bot_username", None) or "the bot"
        await event.reply(SETUP_STEP_TEXT_TEMPLATE.replace("{bot_username}", bot_username))

    @client.on(events.NewMessage(incoming=True))
    @authorized_users_only()
    async def privategroupvc_capture(event):
        if not getattr(client, "_private_vc_setup_step", False):
            return
        if (getattr(event, "text", None) or "").strip().lower() == "/cancel":
            client._private_vc_setup_step = False
            return await event.reply("Private VC setup cancelled.")
        if not await _ensure_direct_chat(event):
            return
        if not getattr(event, "out", False):
            return
        text = (getattr(event, "text", None) or "").strip()
        if not text:
            return
        identifier = _format_chat_identifier(text)
        if not identifier:
            return await event.reply("That does not look like a chat ID or username.")
        entry = await db.get_private_vc_control(client._own_id)
        new_mapping = identifier.lstrip("-").isdigit()
        if not new_mapping:
            return await event.reply(
                "Could not resolve the private group chat ID. Send the numeric chat ID.\n"
                "Tip: forward a message from the group to @userinfobot to see it."
            )
        chat_id = int(identifier)
        if not await _confirm_privategroup(client, chat_id):
            return await event.reply(
                "That does not look like a valid private group I can see.\n"
                "Make sure the bot is a **group admin** there and try again."
            )
        await db.set_private_vc_control(client._own_id, chat_id)
        client._private_vc_setup_step = False
        first = f"Your **Private VC Control Group** is now `{chat_id}`."
        if entry is None:
            first += "\n\nCommands like /join, /leave, /level, /bass, /mute, /startrecord, /speedtest now work there."
        else:
            first += "\n\nIt replaced the previous control group mapping."
        await event.reply(first)

    @client.on(events.NewMessage(pattern=r"^\.privategroupstatus\s*$"))
    @authorized_users_only()
    async def privategroupstatus_handler(event):
        entry = await db.get_private_vc_control(client._own_id)
        if not entry:
            return await event.reply("No private VC control group is set up yet.")
        control_chat_id = int(entry.get("control_chat_id", 0))
        bridge = await _get_bridge_for_client(client)
        active = 0 if bridge is None else len(bridge.sessions)
        await event.reply(
            f"**Private VC Control Group:** `{control_chat_id}`\n"
            f"Active forwarding sessions: {active}"
        )

# ----------------------------------------------------------------------------
# Module entrypoints: bridge lifecycle per hosted account
# ----------------------------------------------------------------------------

async def _get_bridge_for_client(client) -> VCBridgeManager | None:
    voice = getattr(client, '_voice_chat_manager', None)
    if voice is None:
        return None
    bridge = getattr(voice, '_vc_bridge', None)
    if bridge is None:
        bridge = VCBridgeManager(voice)
        voice._vc_bridge = bridge
        await bridge.start()
    return bridge


async def init(client_instance):
    _previous_bridge = getattr(client_instance, '_vc_bridge', None)
    if _previous_bridge is not None:
        with contextlib.suppress(Exception):
            await _previous_bridge.shutdown()
    voice = getattr(client_instance, '_voice_chat_manager', None)
    if voice is not None:
        voice._vc_bridge = None
    setattr(client_instance, '_voice_chat_manager_bridge_ready', True)
    if not hasattr(client_instance, '_own_id'):
        with contextlib.suppress(Exception):
            me = await client_instance.get_me()
            client_instance._own_id = getattr(me, 'id', None)
    _register_setup_handlers(client_instance)
    return None


async def register_commands():
    add_handler(
        'private_vc_bridge',
        [
            '.privategroupvcsetup — Set up your private VC-to-VC control group',
            '.privategroupstatus — Show your registered control group',
        ],
        'Private VC-to-VC audio bridge: setup and control-group registration',
    )
