"""Per-hosted-account Telegram voice-chat playback and recording.

All runtime state belongs to one manager per hosted Telethon client.  A hosted
account can therefore have only one active voice-chat connection, while
different hosted accounts remain isolated from one another.
"""

from __future__ import annotations

import asyncio
import contextlib
from html import escape
import logging
import re
import shutil
import subprocess
import tempfile
import time
from array import array
from collections.abc import Awaitable, Callable
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls import PyTgCalls
from pytgcalls.pytgcalls_session import PyTgCallsSession
from pytgcalls.types import (
    Device,
    ExternalMedia,
    MediaStream,
    RecordStream,
    StreamEnded,
    StreamFrames,
)
from pytgcalls.types.raw import AudioParameters
from telethon import functions
from telethon.tl import types as tl_types
from telethon.utils import get_peer_id

from plugins.bot import add_handler

try:
    import yt_dlp
except ImportError:  # pragma: no cover - URL playback reports this at runtime
    yt_dlp = None


logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MAX_TRACKS = 50
# The command uses 100 as unity gain: 200 is 2x, 500 is 5x, and 1000 is 10x.
# This is intentionally much higher than PyTgCalls' call-output range because
# gain is applied to a temporary playback copy before it is streamed.
_MAX_VOLUME = 100_000_000
_SAFE_DEFAULT_VOLUME = 100
_PLAYBACK_END_GRACE_SECONDS = 1.5
_LIVE_FRAME_QUEUE_SIZE = 3
_LIVE_RECEIVE_QUEUE_SIZE = 8
# NTgCalls AudioSink is fixed to 10 ms PCM frames. At 48 kHz mono, 16-bit
# little-endian PCM that is 480 samples / 960 bytes per external frame.
_LIVE_FRAME_BYTES = 480 * 2


@dataclass
class Track:
    title: str
    path: Path
    source: str
    on_complete: Callable[[], Awaitable[None]] | None = None


@dataclass
class VoiceState:
    chat_id: int
    chat_title: str = ""
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    volume: int = 100
    muted: bool = False
    joined_at: float = field(default_factory=time.monotonic)
    recording_path: Path | None = None
    recording_task: asyncio.Task | None = None
    recording_started_at: float | None = None
    closing: bool = False
    playback_epoch: int = 0
    playback_watchdog: asyncio.Task | None = None
    playback_deadline: float | None = None
    playback_remaining: float | None = None
    playback_paused: bool = False
    live_active: bool = False
    live_mic_enabled: bool = True
    live_push_to_talk: bool = False
    live_push_active: bool = True
    live_started_at: float | None = None
    live_frame_count: int = 0
    live_byte_count: int = 0
    live_last_frame_at: float | None = None
    live_frame_queue: asyncio.Queue[bytes] | None = None
    live_sender_task: asyncio.Task | None = None
    receive_subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _safe_title(value: str) -> str:
    value = " ".join((value or "").split()).strip()
    return value[:160] or "Voice chat audio"


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def _download_url(url: str, output_dir: Path) -> tuple[Path, str]:
    if yt_dlp is None:
        raise RuntimeError("URL playback needs the yt-dlp package.")
    template = str(output_dir / "download.%(ext)s")
    options = {
        "format": "bestaudio/best",
        "outtmpl": template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        prepared = Path(downloader.prepare_filename(info))
        title = _safe_title(info.get("title") or prepared.stem)
    if not prepared.exists():
        matches = sorted(output_dir.glob("download.*"))
        if not matches:
            raise FileNotFoundError("The audio download did not produce a file.")
        prepared = matches[0]
    return prepared, title


def _create_gain_copy(source: Path, output_dir: Path, volume: int) -> Path:
    """Create a gain-only, temporary stream copy without touching ``source``."""
    playback_path = output_dir / "playback-gain.wav"
    gain = volume / 100
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            f"volume={gain:.12g}:precision=float",
            "-c:a",
            "pcm_f32le",
            str(playback_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0 or not playback_path.is_file():
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "Could not prepare the audio for Voice Chat playback."
            + (f" {detail[-500:]}" if detail else "")
        )
    if playback_path.stat().st_size == 0:
        raise RuntimeError("The prepared Voice Chat audio file is empty.")
    return playback_path


def _probe_duration(source: Path) -> float | None:
    """Read a local media duration for the end-of-stream safety watchdog."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Playback must remain usable when only ffmpeg is available or when
        # probing a damaged/remote-derived file takes too long. StreamEnded
        # remains the primary completion signal in that case.
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float((result.stdout or "").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


class VoiceChatManager:
    """One PyTgCalls connection and one voice-chat state per hosted account."""

    def __init__(self, client):
        self.client = client
        self.calls = PyTgCalls(client)
        self.state: VoiceState | None = None
        self._started = False
        self._tasks: set[asyncio.Task] = set()
        self._temp_dir = Path(tempfile.mkdtemp(prefix="telegram-userbot-vc-"))

    async def start(self) -> None:
        if self._started:
            return
        if not self.client.is_connected():
            raise RuntimeError("The hosted Telethon client is not connected.")
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "FFmpeg is not available. Install FFmpeg before using Voice Chat."
            )
        # PyTgCalls performs a remote version check during its first start.
        # The check is informational and must not delay the userbot startup.
        PyTgCallsSession.notice_displayed = True

        async def on_update(_, update):
            if isinstance(update, StreamFrames):
                update_chat_id = getattr(update, "chat_id", None)
                state = self.state
                chat_match = (
                    state is not None
                    and state.chat_id == update_chat_id
                )
                direction = str(
                    getattr(update.direction, "name", update.direction)
                ).upper()
                device = str(
                    getattr(update.device, "name", update.device)
                ).upper()
                if chat_match:
                    logger.info(
                        "[VOICE_AI_DEBUG] PYTG_CALLS_UPDATE_RECEIVED "
                        "type=StreamFrames chat_id=%s direction=%s device=%s "
                        "frame_count=%d CHAT_MATCH=%s.",
                        update_chat_id,
                        direction,
                        device,
                        len(update.frames),
                        chat_match,
                    )
                if not (chat_match and direction == "INCOMING" and device == "SPEAKER"):
                    return
                for frame in update.frames:
                    payload = (
                        getattr(
                            frame,
                            "frame",
                            getattr(frame, "data", b""),
                        )
                        or b""
                    )
                    frame_info = getattr(frame, "info", None)
                    frame_timestamp = getattr(
                        frame_info,
                        "capture_time",
                        None,
                    )
                    voice_ai_active = (
                        getattr(self, "_voice_ai_enabled", False)
                        and getattr(self, "_voice_ai_capture_chat_id", None)
                        == update_chat_id
                    )
                    voice_ai_reached = voice_ai_active and len(payload) > 0
                    if payload:
                        for subscriber in tuple(state.receive_subscribers):
                            if subscriber.full():
                                with contextlib.suppress(asyncio.QueueEmpty):
                                    subscriber.get_nowait()
                            with contextlib.suppress(asyncio.QueueFull):
                                subscriber.put_nowait(payload)
                    if voice_ai_reached:
                        now = time.monotonic()
                        self._voice_ai_capture_first_packet_at = (
                            getattr(
                                self,
                                "_voice_ai_capture_first_packet_at",
                                None,
                            )
                            or now
                        )
                        self._voice_ai_capture_last_packet_at = now
                        self._voice_ai_capture_packet_count = (
                            getattr(
                                self,
                                "_voice_ai_capture_packet_count",
                                0,
                            )
                            + 1
                        )
                        self._voice_ai_capture_packet_bytes = (
                            getattr(
                                self,
                                "_voice_ai_capture_packet_bytes",
                                0,
                            )
                            + len(payload)
                        )
                        activity_event = getattr(
                            self,
                            "_voice_ai_capture_activity",
                            None,
                        )
                        if activity_event is not None:
                            activity_event.set()
                    logger.info(
                        "[VOICE_AI_DEBUG] PACKET_RECEIVED chat_id=%s "
                        "frame_type=%s frame_bytes=%d timestamp=%s "
                        "PACKET_BYTES=%d CHAT_MATCH=%s VOICE_AI_REACHED=%s.",
                        update_chat_id,
                        type(frame).__name__,
                        len(payload),
                        frame_timestamp,
                        len(payload),
                        chat_match,
                        voice_ai_reached,
                    )
                return
            if not isinstance(update, StreamEnded):
                return
            if getattr(self, "_voice_ai_enabled", False):
                logger.info(
                    "[VOICE_AI_DEBUG] stream ended chat=%s type=%s device=%s.",
                    update.chat_id,
                    update.stream_type,
                    update.device,
                )
            if update.stream_type != StreamEnded.Type.AUDIO:
                return
            state = self.state
            expected_track = (
                state.current
                if state is not None
                and state.chat_id == update.chat_id
                and not state.closing
                else None
            )
            if expected_track is None:
                return
            task = asyncio.create_task(
                self._advance_after_end(update.chat_id, expected_track)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        self.calls.on_update()(on_update)
        logger.info(
            "[VOICE_AI_DEBUG] PYTG_CALLS_UPDATE_HANDLER_REGISTERED "
            "api=on_update callback=VoiceChatManager.on_update callbacks=%d.",
            len(getattr(self.calls, "_callbacks", [])),
        )
        await asyncio.wait_for(self.calls.start(), timeout=20)
        self._started = True

    def _ensure_single_connection(self, chat_id: int) -> VoiceState:
        if self.state is not None and self.state.chat_id != chat_id:
            raise RuntimeError(
                f"I am already connected to `{self.state.chat_id}`. "
                "Use .vcleave there before joining another voice chat."
            )
        if self.state is None:
            self.state = VoiceState(chat_id=chat_id, volume=_SAFE_DEFAULT_VOLUME)
        return self.state

    def _require_state(self, chat_id: int) -> VoiceState:
        if self.state is None or self.state.chat_id != chat_id:
            raise RuntimeError("I am not connected to a voice chat here.")
        return self.state

    async def _active_group_call(self, entity):
        """Return Telegram's active group-call descriptor, if one exists."""
        if isinstance(entity, tl_types.Channel):
            full_chat = await self.client(
                functions.channels.GetFullChannelRequest(channel=entity)
            )
        elif isinstance(entity, tl_types.Chat):
            full_chat = await self.client(
                functions.messages.GetFullChatRequest(chat_id=entity.id)
            )
        else:
            raise ValueError("The target must be a group or supergroup.")
        return getattr(full_chat.full_chat, "call", None)

    async def join_target(self, identifier: str) -> str:
        """Resolve a group, require an active VC, then connect once."""
        if not identifier:
            raise ValueError("Usage: .vcjoin <group username or chat ID>")
        await self.start()
        try:
            entity = await self.client.get_entity(
                int(identifier) if identifier.lstrip("-").isdigit() else identifier.lstrip("@")
            )
        except Exception as exc:
            raise ValueError(
                "Could not find that group. Use a group username or numeric chat ID."
            ) from exc

        if (
            not isinstance(entity, tl_types.Chat)
            and not (
                isinstance(entity, tl_types.Channel)
                and bool(getattr(entity, "megagroup", False))
            )
        ):
            raise ValueError("The target must be a group or supergroup.")

        if await self._active_group_call(entity) is None:
            raise NoActiveGroupCall()

        chat_id = int(get_peer_id(entity))
        if self.state is not None and self.state.chat_id != chat_id:
            raise RuntimeError(
                f"I am already connected to <code>{self.state.chat_id}</code>. "
                "Use .vcleave before joining another voice chat."
            )
        if self.state is not None:
            title = escape(_safe_title(getattr(entity, "title", None)))
            return (
                f"✅ Already connected to <b>{title}</b> "
                f"(<code>{chat_id}</code>)."
            )

        try:
            await self.calls.play(chat_id, None)
        except NoActiveGroupCall:
            raise
        except Exception as exc:
            raise RuntimeError(f"Could not connect to the active Voice Chat: {exc}") from exc

        self.state = VoiceState(
            chat_id=chat_id,
            chat_title=_safe_title(getattr(entity, "title", None)),
            volume=_SAFE_DEFAULT_VOLUME,
        )
        return (
            f"✅ Connected to <b>{escape(self.state.chat_title)}</b> "
            f"(<code>{chat_id}</code>)."
        )

    async def _prepare_track(self, event, args: str) -> Track:
        work_dir = Path(tempfile.mkdtemp(prefix="track-", dir=self._temp_dir))
        try:
            url_match = _URL_RE.search(args)
            source = ""
            title = ""
            if url_match:
                source = url_match.group(0).rstrip(".,)>")
                raw_path, title = await asyncio.to_thread(
                    _download_url, source, work_dir
                )
            else:
                reply = await event.get_reply_message()
                if reply is None or not reply.media:
                    raise ValueError(
                        "Reply to an audio, voice, or video message, or provide a URL."
                    )
                downloaded = await reply.download_media(file=str(work_dir / "input"))
                if not downloaded:
                    raise RuntimeError("Telegram did not provide a downloadable media file.")
                raw_path = Path(downloaded)
                source = f"message:{reply.id}"
                title = _safe_title(
                    getattr(reply.file, "name", None)
                    or getattr(reply, "text", None)
                    or "Telegram audio"
                )

            return Track(
                title=_safe_title(title or raw_path.stem),
                path=raw_path,
                source=source,
            )
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    async def enqueue(self, event, chat_id: int, args: str) -> str:
        state = self._ensure_single_connection(chat_id)
        if len(state.queue) >= _MAX_TRACKS:
            raise RuntimeError(f"The queue is full ({_MAX_TRACKS} tracks maximum).")
        track = await self._prepare_track(event, args.strip())
        state.queue.append(track)
        if state.current is None:
            await self._play_next(state)
            return self._now_playing_text(state)
        return (
            f"➕ Queued: <b>{escape(track.title)}</b> · "
            f"position {len(state.queue)}"
        )

    async def enqueue_file(
        self,
        source: Path,
        title: str,
        source_label: str,
        on_complete: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """Queue a control-bot download without changing its audio quality."""
        if not source.exists() or not source.is_file():
            raise FileNotFoundError("The replied audio file is unavailable.")
        state = self.state
        if state is None:
            raise RuntimeError("Join an active Voice Chat first with .vcjoin.")
        if len(state.queue) >= _MAX_TRACKS:
            raise RuntimeError(f"The queue is full ({_MAX_TRACKS} tracks maximum).")

        work_dir = Path(tempfile.mkdtemp(prefix="track-", dir=self._temp_dir))
        destination = work_dir / source.name
        try:
            await asyncio.to_thread(shutil.copy2, source, destination)
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

        track = Track(
            title=_safe_title(title or destination.stem),
            path=destination,
            source=source_label,
            on_complete=on_complete,
        )
        state.queue.append(track)
        try:
            if state.current is None:
                await self._play_next(state)
                return self._now_playing_text(state)
            return (
                f"➕ Queued: <b>{escape(track.title)}</b> · "
                f"position {len(state.queue)}"
            )
        except Exception:
            with contextlib.suppress(ValueError):
                state.queue.remove(track)
            self._remove_track(track)
            raise

    async def _play_next(self, state: VoiceState) -> None:
        async with state.transition_lock:
            await self._play_next_locked(state)

    async def _play_next_locked(self, state: VoiceState) -> None:
        """Start the next track while ``state.transition_lock`` is held."""
        if state.closing or state.current is not None or not state.queue:
            return
        track = state.queue.popleft()
        playback_epoch = state.playback_epoch
        try:
            # Keep the downloaded track untouched. The only transform is
            # digital gain on a temporary copy used by the stream.
            playback_path = await asyncio.to_thread(
                _create_gain_copy,
                track.path,
                track.path.parent,
                state.volume,
            )
            playback_duration = await asyncio.to_thread(
                _probe_duration,
                playback_path,
            )
            # .vcstop can request cancellation while the gain copy is being
            # prepared. Do not start a stream that was already cancelled.
            if (
                state.closing
                or state.playback_epoch != playback_epoch
                or self.state is not state
            ):
                self._remove_track(track)
                return
            await self.calls.play(
                state.chat_id,
                MediaStream(
                    playback_path,
                    audio_flags=MediaStream.Flags.REQUIRED,
                    video_flags=MediaStream.Flags.IGNORE,
                ),
            )
            # A stop request may arrive while PyTgCalls is replacing the
            # source. Clear that source here as well; the stop command will
            # perform the same idempotent operation once it owns the lock.
            if (
                state.closing
                or state.playback_epoch != playback_epoch
                or self.state is not state
            ):
                with contextlib.suppress(Exception):
                    await self.calls.play(state.chat_id, None)
                self._remove_track(track)
                return
        except Exception:
            self._remove_track(track)
            raise
        state.current = track
        state.playback_paused = False
        if playback_duration is not None:
            self._schedule_playback_watchdog(
                state,
                track,
                playback_duration + _PLAYBACK_END_GRACE_SECONDS,
                playback_epoch,
            )

    @staticmethod
    def _now_playing_text(state: VoiceState) -> str:
        if state.current is None:
            return "▶️ Playback started."
        muted = " (muted)" if state.muted else ""
        return (
            f"▶️ Now playing: <b>{escape(state.current.title)}</b>\n"
            f"🔊 Volume: <code>{state.volume}%</code>{muted}"
        )

    @staticmethod
    def _apply_live_gain(data: bytes, volume: int) -> bytes:
        """Apply the configured linear gain to little-endian PCM16 samples."""
        if not data or volume == 100:
            return data
        usable_length = len(data) - (len(data) % 2)
        if usable_length <= 0:
            return b""
        samples = array("h")
        samples.frombytes(data[:usable_length])
        if volume == 0:
            samples = array("h", [0]) * len(samples)
        else:
            multiplier = volume / 100
            for index, sample in enumerate(samples):
                amplified = int(round(sample * multiplier))
                samples[index] = max(-32768, min(32767, amplified))
        if samples.itemsize != 2:
            raise RuntimeError("The runtime does not expose 16-bit PCM samples.")
        return samples.tobytes()

    async def start_live(self, chat_id: int) -> str:
        """Replace an idle playback source with an external PCM microphone source."""
        state = self._require_state(chat_id)
        if state.live_active:
            return "🎙️ Live microphone is already streaming."
        if state.current is not None or state.queue:
            raise RuntimeError(
                "Stop playback with .vcstop before starting the live microphone."
            )
        stream = MediaStream(
            ExternalMedia.AUDIO,
            AudioParameters(bitrate=48000, channels=1),
            audio_flags=MediaStream.Flags.REQUIRED,
            video_flags=MediaStream.Flags.IGNORE,
        )
        await self.calls.play(chat_id, stream)
        state.live_active = True
        state.live_mic_enabled = True
        state.live_push_to_talk = False
        state.live_push_active = True
        state.live_started_at = time.monotonic()
        state.live_frame_count = 0
        state.live_byte_count = 0
        state.live_last_frame_at = None
        state.live_frame_queue = asyncio.Queue(maxsize=_LIVE_FRAME_QUEUE_SIZE)
        state.live_sender_task = asyncio.create_task(
            self._send_live_frames(state),
            name=f"live-audio-{chat_id}",
        )
        self._tasks.add(state.live_sender_task)
        state.live_sender_task.add_done_callback(self._tasks.discard)
        logger.info(
            "Live microphone source started for Voice Chat %s using "
            "PyTgCalls external PCM16L frames.",
            chat_id,
        )
        return "🎙️ Live microphone started."

    async def stop_live(self, chat_id: int) -> str:
        """Stop only the external microphone source and keep the VC connection."""
        state = self._require_state(chat_id)
        if not state.live_active:
            return "Live microphone is already stopped."
        state.live_active = False
        await self._stop_live_sender(state)
        await self.calls.play(chat_id, None)
        state.live_started_at = None
        state.live_last_frame_at = None
        state.live_push_active = False
        logger.info(
            "Live microphone source stopped for Voice Chat %s after %d frame(s).",
            chat_id,
            state.live_frame_count,
        )
        return "⏹️ Live microphone stopped; I am still in the voice chat."

    async def _stop_live_sender(self, state: VoiceState) -> None:
        queue = state.live_frame_queue
        state.live_frame_queue = None
        sender_task = state.live_sender_task
        state.live_sender_task = None
        if sender_task is not None and sender_task is not asyncio.current_task():
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
        if queue is not None:
            while not queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()

    async def _send_live_frames(self, state: VoiceState) -> None:
        """Forward live PCM without allowing a slow native call to build latency."""
        queue = state.live_frame_queue
        if queue is None:
            return
        try:
            while state.live_active and state.live_frame_queue is queue:
                data = await queue.get()
                if not data or not state.live_active:
                    continue
                transformed = self._apply_live_gain(data, state.volume)
                if not transformed:
                    continue
                await self.calls.send_frame(
                    state.chat_id,
                    Device.MICROPHONE,
                    transformed,
                )
                state.live_frame_count += 1
                state.live_byte_count += len(transformed)
                state.live_last_frame_at = time.monotonic()
                if state.live_frame_count == 1 or state.live_frame_count % 100 == 0:
                    logger.info(
                        "Live microphone frame sent to Telegram Voice Chat %s: "
                        "frames=%d bytes=%d gain=%d%% queue=%d.",
                        state.chat_id,
                        state.live_frame_count,
                        state.live_byte_count,
                        state.volume,
                        queue.qsize(),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Live microphone sender stopped for Voice Chat %s.",
                state.chat_id,
            )

    def set_live_controls(
        self,
        chat_id: int,
        *,
        mic_enabled: bool | None = None,
        push_to_talk: bool | None = None,
        push_active: bool | None = None,
    ) -> dict:
        state = self._require_state(chat_id)
        if mic_enabled is not None:
            state.live_mic_enabled = bool(mic_enabled)
        if push_to_talk is not None:
            state.live_push_to_talk = bool(push_to_talk)
        if push_active is not None:
            state.live_push_active = bool(push_active)
        return self.live_snapshot(chat_id)

    async def send_live_frame(self, chat_id: int, data: bytes) -> bool:
        """Queue one browser PCM16L frame without creating an audio backlog."""
        state = self._require_state(chat_id)
        if not state.live_active or not state.live_mic_enabled:
            return False
        if state.live_push_to_talk and not state.live_push_active:
            return False
        if not data:
            return False
        if len(data) != _LIVE_FRAME_BYTES:
            logger.warning(
                "Ignoring malformed live microphone frame for Voice Chat %s: "
                "got %d bytes, expected %d.",
                chat_id,
                len(data),
                _LIVE_FRAME_BYTES,
            )
            return False
        queue = state.live_frame_queue
        if queue is None:
            return False
        # Never wait for the native sender here. Keeping the newest few frames
        # bounds end-to-end latency when Telegram briefly slows down.
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(data)
        return True

    def subscribe_receive(self, chat_id: int) -> asyncio.Queue[bytes]:
        """Subscribe to untouched Telegram playback PCM for one Mini App."""
        state = self._require_state(chat_id)
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_LIVE_RECEIVE_QUEUE_SIZE)
        state.receive_subscribers.add(queue)
        return queue

    def unsubscribe_receive(self, chat_id: int, queue: asyncio.Queue[bytes]) -> None:
        state = self.state
        if state is not None and state.chat_id == chat_id:
            state.receive_subscribers.discard(queue)

    def live_snapshot(self, chat_id: int | None = None) -> dict:
        state = self.state
        if state is None or (chat_id is not None and state.chat_id != chat_id):
            return {
                "active": False,
                "mic_enabled": False,
                "push_to_talk": False,
                "push_active": False,
                "frames": 0,
                "bytes": 0,
                "started_at": None,
                "last_frame_at": None,
            }
        return {
            "active": state.live_active,
            "mic_enabled": state.live_mic_enabled,
            "push_to_talk": state.live_push_to_talk,
            "push_active": state.live_push_active,
            "frames": state.live_frame_count,
            "bytes": state.live_byte_count,
            "started_at": state.live_started_at,
            "last_frame_at": state.live_last_frame_at,
        }

    async def _advance_after_end(self, chat_id: int, expected_track: Track) -> None:
        state = self.state
        if state is None or state.chat_id != chat_id:
            return
        completion_callback = None
        async with state.transition_lock:
            if state.closing or state.current is not expected_track:
                return
            finished = expected_track
            await self._cancel_playback_watchdog(state)
            state.playback_deadline = None
            state.playback_remaining = None
            state.playback_paused = False
            state.current = None
            self._remove_track(finished)
            completion_callback = finished.on_complete
            try:
                await self._play_next_locked(state)
            except Exception:
                logger.exception(
                    "Could not advance the voice-chat queue in %s.", chat_id
                )
        if completion_callback is not None:
            await self._notify_playback_complete(finished)

    async def _playback_end_watchdog(
        self,
        state: VoiceState,
        expected_track: Track,
        playback_epoch: int,
    ) -> None:
        try:
            while True:
                deadline = state.playback_deadline
                if deadline is None:
                    return
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                if not state.playback_paused:
                    break
                await asyncio.sleep(0.25)
            if state.playback_epoch != playback_epoch:
                return
            await self._advance_after_end(state.chat_id, expected_track)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Playback completion watchdog failed in %s.",
                state.chat_id,
            )

    def _schedule_playback_watchdog(
        self,
        state: VoiceState,
        track: Track,
        remaining: float,
        playback_epoch: int,
    ) -> None:
        if remaining <= 0:
            return
        state.playback_remaining = None
        state.playback_deadline = time.monotonic() + remaining
        watchdog = asyncio.create_task(
            self._playback_end_watchdog(
                state,
                track,
                playback_epoch,
            )
        )
        state.playback_watchdog = watchdog
        self._tasks.add(watchdog)
        watchdog.add_done_callback(self._tasks.discard)

    async def _cancel_playback_watchdog(self, state: VoiceState) -> None:
        watchdog = state.playback_watchdog
        state.playback_watchdog = None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    async def _notify_playback_complete(self, track: Track) -> None:
        if track.on_complete is None:
            return
        try:
            await track.on_complete()
        except Exception:
            logger.exception(
                "Could not send playback completion notice for %s.",
                track.title,
            )

    @staticmethod
    def _remove_track(track: Track) -> None:
        shutil.rmtree(track.path.parent, ignore_errors=True)

    async def pause(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        await self.calls.pause(chat_id)
        if state.current is not None and state.playback_deadline is not None:
            state.playback_remaining = max(
                0,
                state.playback_deadline - time.monotonic(),
            )
            state.playback_deadline = None
            state.playback_paused = True
            await self._cancel_playback_watchdog(state)
        return "⏸️ Playback paused."

    async def resume(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        await self.calls.resume(chat_id)
        if state.current is not None:
            state.playback_paused = False
            if state.playback_remaining is not None:
                remaining = state.playback_remaining
                state.playback_remaining = None
                self._schedule_playback_watchdog(
                    state,
                    state.current,
                    remaining,
                    state.playback_epoch,
                )
        return "▶️ Playback resumed."

    async def skip(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        if state.current is not None:
            await self._cancel_playback_watchdog(state)
            state.playback_deadline = None
            state.playback_remaining = None
            state.playback_paused = False
            self._remove_track(state.current)
            state.current = None
        await self._play_next(state)
        if state.current is None:
            await self.calls.play(chat_id, None)
            return "⏭️ Skipped. The queue is empty."
        return f"⏭️ {self._now_playing_text(state)}"

    async def stop_ai_voice(self) -> None:
        """Cancel the optional AI voice worker without touching the VC."""
        stop_event = getattr(self, "_voice_ai_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        task = getattr(self, "_voice_ai_task", None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._voice_ai_task = None
        self._voice_ai_stop_event = None
        self._voice_ai_enabled = False
        self._voice_ai_processing = False
        self._voice_ai_state = "IDLE"

    async def stop(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        # Invalidate any end-of-stream callback or in-flight start before
        # waiting for the transition lock. This prevents a queued track from
        # being started while .vcstop is taking control.
        state.playback_epoch += 1
        state.closing = True
        async with state.transition_lock:
            queued = list(state.queue)
            state.queue.clear()
            current = state.current
            # Clear the logical playback state before touching PyTgCalls so a
            # delayed StreamEnded update cannot advance the queue afterward.
            state.current = None
            try:
                await self._cancel_playback_watchdog(state)
                state.playback_deadline = None
                state.playback_remaining = None
                state.playback_paused = False
                if state.live_active:
                    state.live_active = False
                    await self._stop_live_sender(state)
                # PyTgCalls treats play(chat_id, None) as an empty source:
                # it stops its managed FFmpeg process while keeping the
                # existing group-call connection alive.
                await self.calls.play(chat_id, None)
            finally:
                for track in queued:
                    self._remove_track(track)
                if current is not None:
                    self._remove_track(current)
                state.closing = False
            state.live_active = False
            state.live_started_at = None
            state.live_last_frame_at = None
            state.live_push_active = False
        return "⏹️ Playback stopped; I am still in the voice chat."

    async def leave(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        await self.stop_ai_voice()
        state.closing = True
        try:
            if state.recording_path is not None:
                await self._stop_recording(state, chat_id, send_file=False)
            with contextlib.suppress(Exception):
                if state.live_active:
                    await self.stop_live(chat_id)
            with contextlib.suppress(Exception):
                await self.calls.leave_call(chat_id)
        finally:
            self._clear_state(state)
            self.state = None
        return "👋 Left the voice chat and cleared the queue."

    async def change_volume(self, chat_id: int, value: int) -> str:
        state = self._require_state(chat_id)
        if not 0 <= value <= _MAX_VOLUME:
            raise ValueError(f"Volume must be between 0 and {_MAX_VOLUME}.")
        state.volume = value
        state.muted = value == 0
        return f"🔊 Playback gain set to {value}%."

    async def mute(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        await self.calls.mute(chat_id)
        state.muted = True
        return "🔇 Playback muted."

    async def unmute(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        await self.calls.unmute(chat_id)
        state.muted = False
        return "🔊 Playback unmuted."

    async def status(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        lines = [
            "🎙️ <b>Voice chat status</b>",
            f"Group: <b>{escape(state.chat_title or 'Unknown group')}</b>",
            f"Chat: <code>{state.chat_id}</code>",
            f"Connected for: <code>{_format_duration(time.monotonic() - state.joined_at)}</code>",
            f"Volume gain: <code>{state.volume}%</code>{' (muted)' if state.muted else ''}",
            (
                f"Now playing: <b>{escape(state.current.title)}</b>"
                if state.current
                else "Now playing: <i>nothing</i>"
            ),
            f"Queued: <code>{len(state.queue)}</code>",
        ]
        if state.recording_path is not None and state.recording_started_at is not None:
            lines.append(
                "Recording: <code>"
                f"{_format_duration(time.monotonic() - state.recording_started_at)}"
                "</code>"
            )
        return "\n".join(lines)

    async def control_status(self) -> str:
        """Status response for the private control bot."""
        if self.state is None:
            return "❌ Not connected to any Voice Chat."
        return await self.status(self.state.chat_id)

    async def queue_text(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        lines = ["📚 <b>Voice queue</b>"]
        if state.current:
            lines.append(f"▶️ <b>Now:</b> {escape(state.current.title)}")
        if state.queue:
            lines.extend(
                f"{i}. {escape(track.title)}"
                for i, track in enumerate(state.queue, 1)
            )
        else:
            lines.append("<i>The queue is empty.</i>")
        return "\n".join(lines)

    async def clear_queue(self, chat_id: int) -> str:
        state = self._require_state(chat_id)
        while state.queue:
            self._remove_track(state.queue.popleft())
        return "🧹 Queue cleared."

    async def start_recording(self, chat_id: int, seconds: int | None = None) -> str:
        state = self._ensure_single_connection(chat_id)
        if state.recording_path is not None:
            raise RuntimeError("A recording is already in progress. Use /record stop.")
        recording_dir = Path(tempfile.mkdtemp(prefix="recording-", dir=self._temp_dir))
        output = recording_dir / "voice-chat.mp3"
        try:
            await self.calls.record(chat_id, RecordStream(audio=output))
        except Exception:
            shutil.rmtree(recording_dir, ignore_errors=True)
            raise
        state.recording_path = output
        state.recording_started_at = time.monotonic()
        if seconds:
            state.recording_task = asyncio.create_task(
                self._timed_recording_stop(state, chat_id, seconds)
            )
        suffix = f" for {seconds}s" if seconds else ""
        return f"⏺️ Recording started{suffix}. Use /record stop when finished."

    async def _timed_recording_stop(self, state: VoiceState, chat_id: int, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state is state and state.recording_path is not None:
                await self._stop_recording(state, chat_id, send_file=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Timed voice-chat recording failed in %s.", chat_id)

    async def stop_recording(self, event, chat_id: int) -> str:
        state = self._require_state(chat_id)
        if state.recording_path is None:
            raise RuntimeError("No recording is in progress.")
        return await self._stop_recording(state, chat_id, send_file=True, event=event)

    async def _stop_recording(
        self,
        state: VoiceState,
        chat_id: int,
        send_file: bool,
        event=None,
    ) -> str:
        if (
            state.recording_task is not None
            and state.recording_task is not asyncio.current_task()
        ):
            state.recording_task.cancel()
            state.recording_task = None
        path = state.recording_path
        state.recording_path = None
        state.recording_started_at = None
        if path is None:
            return "No recording is in progress."
        with contextlib.suppress(Exception):
            await self.calls.play(chat_id, None)
        if not path.exists():
            shutil.rmtree(path.parent, ignore_errors=True)
            raise RuntimeError("The recording did not produce an audio file.")
        try:
            if send_file:
                target = event if event is not None else chat_id
                await self.client.send_file(
                    target,
                    path,
                    force_document=True,
                    caption="🎧 Voice chat recording",
                )
            return "⏹️ Recording stopped and processed."
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def _clear_state(self, state: VoiceState) -> None:
        while state.queue:
            self._remove_track(state.queue.popleft())
        if state.current:
            self._remove_track(state.current)
        state.current = None
        state.playback_deadline = None
        state.playback_remaining = None
        state.playback_paused = False
        if state.playback_watchdog is not None:
            state.playback_watchdog.cancel()
            state.playback_watchdog = None
        state.live_active = False
        if state.live_sender_task is not None:
            state.live_sender_task.cancel()
            state.live_sender_task = None
        state.live_frame_queue = None
        state.live_started_at = None
        state.live_last_frame_at = None
        state.live_push_active = False

    async def shutdown(self) -> None:
        await self.stop_ai_voice()
        if self.state is not None:
            state = self.state
            state.closing = True
            if state.recording_path is not None:
                with contextlib.suppress(Exception):
                    await self._stop_recording(state, state.chat_id, send_file=False)
            with contextlib.suppress(Exception):
                if state.live_active:
                    await self.stop_live(state.chat_id)
            with contextlib.suppress(Exception):
                await self.calls.leave_call(state.chat_id)
            self._clear_state(state)
            self.state = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        shutil.rmtree(self._temp_dir, ignore_errors=True)


_manager: VoiceChatManager | None = None


async def init(client_instance):
    global _manager
    previous = getattr(client_instance, "_voice_chat_manager", None)
    if previous is not None:
        with contextlib.suppress(Exception):
            await previous.shutdown()
    manager = VoiceChatManager(client_instance)
    _manager = manager
    setattr(client_instance, "_voice_chat_manager", manager)


async def register_commands():
    if _manager is None:
        raise RuntimeError("Voice-chat manager could not load.")
    add_handler(
        "voice_chat",
        [
            ".vcjoin <group> — Join an active group Voice Chat from the private control bot",
            ".vcstatus — Show the connected group and playback status",
            ".vcstop — Stop playback and clear the queue without leaving",
            ".vcleave — Leave and clear the Voice Chat",
            ".play — Play replied audio in the connected Voice Chat",
            ".pause / .resume / .queue / .clearqueue — Playback controls",
            ".volume <0-100000000> / .mute / .unmute — Gain-only playback controls",
        ],
        "Private control-bot Voice Chat playback and gain-only controls",
    )