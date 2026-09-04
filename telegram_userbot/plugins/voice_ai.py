"""Per-hosted-account AI voice mode for an active Telegram Voice Chat.

The mode is intentionally owned by the existing VoiceChatManager.  That keeps
the Gemini prompt, recording worker, and generated reply isolated per hosted
Telethon client while reusing the normal playback queue for TTS audio.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from html import escape
import logging
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import edge_tts
from pytgcalls.types import RecordStream
from telethon import events

import database.mongo as db
from plugins.bot import add_handler
from utils.decorators import rishabh
from utils.gemini_rotation import gemini_chat, get_keys


logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "You are a helpful, concise voice assistant inside a Telegram Voice Chat. "
    "Answer naturally for speech. Do not use markdown, bullets, emojis, or "
    "long explanations unless the user asks for detail."
)
_PROMPT_SETTING = "voice_ai_prompt"
_SILENCE_CHUNKS_TO_END = 2
_MAX_SPEECH_SECONDS = 30
_CAPTURE_IDLE_SECONDS = 0.9
_CAPTURE_NO_PACKET_TIMEOUT_SECONDS = 8.0
_CAPTURE_MIN_ACTIVE_SECONDS = 1.5
_SILENCE_THRESHOLD_DB = -42.0
_MIN_AUDIO_BYTES = 512
_MAX_INLINE_AUDIO_BYTES = 7_500_000
_MAX_PROMPT_LENGTH = 2_000
_MAX_SPOKEN_LENGTH = 3_500
_TTS_VOICE = "en-IN-NeerjaNeural"
_LISTENING_SETTLE_SECONDS = 0.2
_CAPTURE_RETRY_SECONDS = 1.0
_MAX_STAGE_ATTEMPTS = 3
_STAGE_RETRY_SECONDS = 0.75
_STT_TIMEOUT_SECONDS = 30.0

AI_LISTENING = "LISTENING"
AI_PROCESSING = "PROCESSING"
AI_SPEAKING = "SPEAKING"


class VoiceAIError(RuntimeError):
    """An expected failure while recording, transcribing, or speaking."""


def _manager(event):
    return getattr(event.client, "_voice_chat_manager", None)


def _hosted_user_id(event) -> int:
    context = getattr(event.client, "_userbot_context", None)
    user_id = getattr(context, "user_id", None)
    if user_id is None:
        raise VoiceAIError("The hosted account context is unavailable.")
    return int(user_id)


async def _load_prompt(manager, user_id: int) -> str:
    cached = getattr(manager, "_voice_ai_prompt", None)
    if cached is not None:
        return cached or _DEFAULT_PROMPT
    try:
        saved = await db.get_setting(user_id, _PROMPT_SETTING, "")
    except Exception:
        logger.exception("Could not load the AI voice prompt for user %s.", user_id)
        saved = ""
    prompt = str(saved or "").strip()[:_MAX_PROMPT_LENGTH]
    manager._voice_ai_prompt = prompt
    return prompt or _DEFAULT_PROMPT


async def _save_prompt(manager, user_id: int, prompt: str) -> None:
    normalized = prompt.strip()[:_MAX_PROMPT_LENGTH]
    await db.set_setting(user_id, _PROMPT_SETTING, normalized)
    manager._voice_ai_prompt = normalized


def _safe_speech(text: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    cleaned = re.sub(r"[*_~`#>\[\]{}]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_MAX_SPOKEN_LENGTH]


def _audio_mime(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0]
    if mime and mime.startswith("audio/"):
        return mime
    return "audio/mpeg" if path.suffix.lower() in {".mp3", ".mpga"} else "audio/ogg"


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True


async def _stop_capture(manager, chat_id: int, reason: str) -> None:
    """Stop only the incoming RecordStream, leaving outgoing capture intact."""
    started = time.perf_counter()
    logger.info(
        "[VOICE_AI_DEBUG] RecordStream stop requested chat=%s reason=%s.",
        chat_id,
        reason,
    )
    try:
        # ``record`` owns the PLAYBACK/incoming media source.  Calling
        # ``play(chat_id, None)`` here clears the CAPTURE/outgoing source and
        # can prevent the next incoming packet batch from reaching the
        # recorder.
        await manager.calls.record(chat_id, None)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "[VOICE_AI_DEBUG] RecordStream stop failed chat=%s reason=%s elapsed_ms=%.0f.",
            chat_id,
            reason,
            (time.perf_counter() - started) * 1000,
        )
    else:
        logger.info(
            "[VOICE_AI_DEBUG] RecordStream stop complete chat=%s reason=%s elapsed_ms=%.0f.",
            chat_id,
            reason,
            (time.perf_counter() - started) * 1000,
        )
async def _wait_for_recording_slot(manager, state, stop_event: asyncio.Event) -> bool:
    """Wait until manual recording has released the shared PyTgCalls slot."""
    while state.recording_path is not None and not stop_event.is_set():
        if manager.state is not state:
            return False
        await _wait_or_stop(stop_event, 0.5)
    return not stop_event.is_set() and manager.state is state


async def _wait_for_capture_end(manager, state, stop_event: asyncio.Event) -> str:
    """Keep the incoming recorder open until packet activity becomes quiet."""
    started = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        first_packet = getattr(
            manager,
            "_voice_ai_capture_first_packet_at",
            None,
        )
        last_packet = getattr(
            manager,
            "_voice_ai_capture_last_packet_at",
            None,
        )
        if first_packet is None:
            if now - started >= _CAPTURE_NO_PACKET_TIMEOUT_SECONDS:
                return "no-incoming-packets-timeout"
        elif (
            now - first_packet >= _CAPTURE_MIN_ACTIVE_SECONDS
            and last_packet is not None
            and now - last_packet >= _CAPTURE_IDLE_SECONDS
        ):
            return "packet-activity-quiet"
        elif now - started >= _MAX_SPEECH_SECONDS:
            return "max-speech-window"
        await _wait_or_stop(stop_event, 0.1)
    return "worker-stop"


def _chunk_is_silent(path: Path) -> bool:
    """Return True when an audio chunk is below the speech threshold."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", result.stderr)
    if match is None:
        # If probing fails, keep the audio rather than dropping a real voice
        # turn.  STT can still decide whether the chunk contains speech.
        logger.warning(
            "[VOICE_AI_DEBUG] VAD probe unavailable path=%s bytes=%d returncode=%d; keeping chunk.",
            path,
            path.stat().st_size if path.exists() else 0,
            result.returncode,
        )
        return False
    mean_volume = float(match.group(1))
    silent = mean_volume <= _SILENCE_THRESHOLD_DB
    logger.info(
        "[VOICE_AI_DEBUG] VAD detect path=%s bytes=%d mean_db=%.1f threshold_db=%.1f silent=%s.",
        path.name,
        path.stat().st_size if path.exists() else 0,
        mean_volume,
        _SILENCE_THRESHOLD_DB,
        silent,
    )
    return silent


def _concat_audio(chunks: list[Path], output: Path) -> None:
    manifest = output.parent / "concat.txt"
    def manifest_line(chunk: Path) -> str:
        escaped = str(chunk).replace("'", "'\\''")
        return f"file '{escaped}'\n"

    manifest.write_text(
        "".join(manifest_line(chunk) for chunk in chunks),
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-vn",
                "-c:a",
                "libopus",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise VoiceAIError(
                "Could not combine the captured voice audio."
                + (f" {detail[-500:]}" if detail else "")
            )
    finally:
        manifest.unlink(missing_ok=True)


async def _record_chunk(manager, state, stop_event: asyncio.Event, path: Path) -> bool:
    """Capture one short chunk and close only its RecordStream source."""
    logger.info(
        "[VOICE_AI_DEBUG] record stream preparing chat=%s path=%s.",
        state.chat_id,
        path,
    )
    if await _wait_or_stop(stop_event, _LISTENING_SETTLE_SECONDS):
        logger.info(
            "[VOICE_AI_DEBUG] RecordStream not opened chat=%s reason=stop-before-open.",
            state.chat_id,
        )
        return False
    started = time.perf_counter()
    manager._voice_ai_capture_chat_id = state.chat_id
    manager._voice_ai_capture_path = path
    manager._voice_ai_capture_activity = asyncio.Event()
    manager._voice_ai_capture_first_packet_at = None
    manager._voice_ai_capture_last_packet_at = None
    manager._voice_ai_capture_packet_count = 0
    manager._voice_ai_capture_packet_bytes = 0
    try:
        await manager.calls.record(
            state.chat_id,
            RecordStream(audio=path),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        manager._voice_ai_capture_chat_id = None
        manager._voice_ai_capture_path = None
        logger.exception(
            "[VOICE_AI_DEBUG] record stream open failed chat=%s path=%s elapsed_ms=%.0f.",
            state.chat_id,
            path,
            (time.perf_counter() - started) * 1000,
        )
        raise
    logger.info(
        "[VOICE_AI_DEBUG] RECORD_STARTED chat_id=%s path=%s source=incoming "
        "elapsed_ms=%.0f.",
        state.chat_id,
        path,
        (time.perf_counter() - started) * 1000,
    )
    stop_reason = await _wait_for_capture_end(manager, state, stop_event)
    await _stop_capture(manager, state.chat_id, stop_reason)
    await _wait_or_stop(stop_event, _LISTENING_SETTLE_SECONDS)
    final_bytes = path.stat().st_size if path.exists() else 0
    logger.info(
        "[VOICE_AI_DEBUG] RECORD_BYTES chat_id=%s path=%s bytes=%d "
        "packet_count=%d packet_bytes=%d.",
        state.chat_id,
        path,
        final_bytes,
        getattr(manager, "_voice_ai_capture_packet_count", 0),
        getattr(manager, "_voice_ai_capture_packet_bytes", 0),
    )
    logger.info(
        "[VOICE_AI_DEBUG] RECORD_STOPPED chat_id=%s path=%s reason=%s "
        "final_recorded_bytes=%d.",
        state.chat_id,
        path,
        stop_reason,
        final_bytes,
    )
    manager._voice_ai_capture_chat_id = None
    manager._voice_ai_capture_path = None
    manager._voice_ai_capture_activity = None
    return True


async def _record_segment(
    manager,
    state,
    stop_event: asyncio.Event,
    user_id: int | None = None,
) -> Path | None:
    if not await _wait_for_recording_slot(manager, state, stop_event):
        return None
    work_dir = Path(tempfile.mkdtemp(prefix="ai-voice-", dir=manager._temp_dir))
    recording_path = work_dir / "input.ogg"
    chunks: list[Path] = []
    speech_started = False
    silent_chunks = 0
    elapsed = 0.0
    keep_path = False
    state.recording_path = work_dir
    capture_started = time.perf_counter()
    log_user = user_id if user_id is not None else state.chat_id
    try:
        while not stop_event.is_set():
            elapsed = time.perf_counter() - capture_started
            if speech_started and elapsed >= _MAX_SPEECH_SECONDS:
                logger.info(
                    "AI voice capture user=%s reached max speech window %.2fs.",
                    log_user,
                    elapsed,
                )
                break

            chunk = work_dir / f"chunk-{len(chunks):04d}.ogg"
            if not await _record_chunk(manager, state, stop_event, chunk):
                return None
            if not chunk.is_file() or chunk.stat().st_size < _MIN_AUDIO_BYTES:
                continue

            silent = await asyncio.to_thread(_chunk_is_silent, chunk)
            if not speech_started:
                if silent:
                    chunk.unlink(missing_ok=True)
                    continue
                speech_started = True
                logger.info(
                    "[VOICE_AI_DEBUG] speech detected user=%s after %.2fs "
                    "chunk_bytes=%d.",
                    log_user,
                    time.perf_counter() - capture_started,
                    chunk.stat().st_size,
                )

            chunks.append(chunk)
            if silent:
                silent_chunks += 1
            else:
                silent_chunks = 0
            if silent_chunks >= _SILENCE_CHUNKS_TO_END:
                break

        if not speech_started or not chunks:
            return None
        await asyncio.to_thread(_concat_audio, chunks, recording_path)
        if recording_path.stat().st_size > _MAX_INLINE_AUDIO_BYTES:
            raise VoiceAIError("The captured voice segment is too large to transcribe.")
        keep_path = True
        logger.info(
            "AI voice capture complete user=%s elapsed_ms=%.0f chunks=%d.",
            log_user,
            (time.perf_counter() - capture_started) * 1000,
            len(chunks),
        )
        return recording_path
    except asyncio.CancelledError:
        await _stop_capture(manager, state.chat_id, "worker-cancelled")
        manager._voice_ai_capture_chat_id = None
        manager._voice_ai_capture_path = None
        raise
    finally:
        state.recording_path = None
        if not keep_path:
            shutil.rmtree(work_dir, ignore_errors=True)


async def _run_stage(stage: str, user_id: int, operation):
    started = time.perf_counter()
    try:
        result = await operation()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "AI voice stage=%s user=%s failed elapsed_ms=%.0f.",
            stage,
            user_id,
            (time.perf_counter() - started) * 1000,
        )
        raise
    logger.info(
        "AI voice stage=%s user=%s complete elapsed_ms=%.0f.",
        stage,
        user_id,
        (time.perf_counter() - started) * 1000,
    )
    return result


async def _run_stage_with_recovery(
    stage: str,
    user_id: int,
    stop_event: asyncio.Event,
    operation,
):
    for attempt in range(1, _MAX_STAGE_ATTEMPTS + 1):
        if stop_event.is_set():
            raise asyncio.CancelledError
        try:
            return await _run_stage(stage, user_id, operation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt == _MAX_STAGE_ATTEMPTS:
                raise VoiceAIError(
                    f"{stage} failed after {attempt} attempts: {exc}"
                ) from exc
            logger.warning(
                "AI voice stage=%s user=%s retry=%d/%d error=%s.",
                stage,
                user_id,
                attempt + 1,
                _MAX_STAGE_ATTEMPTS,
                exc,
            )
            await _wait_or_stop(stop_event, _STAGE_RETRY_SECONDS)


async def _transcribe(path: Path) -> str:
    started = time.perf_counter()
    try:
        audio = await asyncio.to_thread(path.read_bytes)
        mime = _audio_mime(path)
        logger.info(
            "STT_START path=%s exists=%s bytes=%d mime=%s.",
            path,
            path.is_file(),
            len(audio),
            mime,
        )
        request = gemini_chat(
            [
                {"mime_type": mime, "data": audio},
                (
                    "Transcribe the spoken audio exactly. Return only the "
                    "transcript text, with no labels, explanation, or markdown. "
                    "If there is no understandable speech, return an empty string."
                ),
            ],
            model="gemini-2.5-flash",
            system_instruction=(
                "You are a reliable speech-to-text engine. Preserve the speaker's "
                "language and meaning. Do not invent words when the audio is silent."
            ),
            generation_config={"max_output_tokens": 8192},
        )
        response = await asyncio.wait_for(request, timeout=_STT_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        logger.error(
            "STT_TIMEOUT path=%s timeout_seconds=%.1f elapsed_ms=%.0f.",
            path,
            _STT_TIMEOUT_SECONDS,
            (time.perf_counter() - started) * 1000,
        )
        raise VoiceAIError(
            f"Speech-to-text timed out after {_STT_TIMEOUT_SECONDS:.1f} seconds."
        ) from exc
    except Exception as exc:
        logger.exception(
            "STT_ERROR path=%s error=%s elapsed_ms=%.0f.",
            path,
            exc,
            (time.perf_counter() - started) * 1000,
        )
        raise
    transcript = " ".join(str(response or "").split()).strip()
    logger.info(
        "STT_SUCCESS path=%s transcript=%r transcript_chars=%d elapsed_ms=%.0f.",
        path,
        transcript,
        len(transcript),
        (time.perf_counter() - started) * 1000,
    )
    return transcript


async def _reply_text(prompt: str, history: deque[tuple[str, str]], transcript: str) -> str:
    conversation = "\n".join(
        f"User: {user}\nAssistant: {assistant}" for user, assistant in history
    )
    user_prompt = (
        f"{conversation}\n" if conversation else ""
    ) + f"User's latest spoken message: {transcript}\nReply for the user:"
    response = await gemini_chat(
        user_prompt,
        model="gemini-2.5-flash",
        system_instruction=prompt,
        generation_config={"max_output_tokens": 8192},
    )
    spoken = _safe_speech(response)
    if not spoken:
        raise VoiceAIError("Gemini returned an empty voice reply.")
    return spoken


async def _synthesize(text: str, output_path: Path) -> None:
    try:
        communicator = edge_tts.Communicate(text, voice=_TTS_VOICE)
        await asyncio.wait_for(communicator.save(str(output_path)), timeout=60)
    except Exception as exc:
        raise VoiceAIError(f"Text-to-speech failed: {exc}") from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise VoiceAIError("Text-to-speech returned an empty audio file.")


async def _wait_until_quiet(manager, state, stop_event: asyncio.Event) -> None:
    """Avoid transcribing the bot's own TTS or another queued recording."""
    while not stop_event.is_set():
        if manager.state is not state:
            return
        if state.current is None and not state.queue:
            return
        await _wait_or_stop(stop_event, 0.5)


def _set_ai_state(manager, state: str) -> None:
    """Keep the public status and internal processing flag in sync."""
    previous = getattr(manager, "_voice_ai_state", None)
    if previous != state:
        logger.debug("AI voice state %s -> %s", previous or "OFF", state)
    manager._voice_ai_state = state
    manager._voice_ai_processing = state == AI_PROCESSING


async def _voice_worker(manager, user_id: int, state, stop_event: asyncio.Event) -> None:
    history: deque[tuple[str, str]] = deque(maxlen=6)
    while not stop_event.is_set():
        if manager.state is not state or state.closing:
            return
        # Half-duplex behavior prevents the listener from transcribing normal
        # playback or the AI's own response.
        if state.current is not None or state.queue:
            _set_ai_state(manager, AI_SPEAKING)
            await _wait_until_quiet(manager, state, stop_event)
            continue

        recording_path: Path | None = None
        try:
            _set_ai_state(manager, AI_LISTENING)
            recording_path = await _record_segment(
                manager,
                state,
                stop_event,
                user_id,
            )
            if recording_path is None:
                continue
            _set_ai_state(manager, AI_PROCESSING)
            try:
                transcript = await _transcribe(recording_path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "AI voice STT failed for hosted user %s; returning to listening: %s",
                    user_id,
                    exc,
                )
                continue
            if not transcript:
                continue
            prompt = await _load_prompt(manager, user_id)
            reply = await _run_stage_with_recovery(
                "gemini",
                user_id,
                stop_event,
                lambda: _reply_text(prompt, history, transcript),
            )
            history.append((transcript, reply))

            tts_dir = Path(tempfile.mkdtemp(prefix="ai-reply-", dir=manager._temp_dir))
            tts_path = tts_dir / "reply.mp3"
            try:
                await _run_stage_with_recovery(
                    "tts",
                    user_id,
                    stop_event,
                    lambda: _synthesize(reply, tts_path),
                )
                if manager.state is not state or stop_event.is_set():
                    continue
                _set_ai_state(manager, AI_SPEAKING)
                await _run_stage(
                    "playback",
                    user_id,
                    lambda: manager.enqueue_file(
                        tts_path,
                        "AI voice reply",
                        f"voice-ai:{user_id}",
                    ),
                )
                # Do not reopen the recorder until the AI's own audio has
                # completely left the call. The next loop iteration then
                # starts a fresh RecordStream for the next human turn.
                await _wait_until_quiet(manager, state, stop_event)
                if not stop_event.is_set() and manager.state is state:
                    await _wait_or_stop(stop_event, _LISTENING_SETTLE_SECONDS)
            finally:
                shutil.rmtree(tts_dir, ignore_errors=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "AI voice turn failed for hosted user %s; retrying listening: %s",
                user_id,
                exc,
            )
            if not stop_event.is_set():
                await _wait_or_stop(stop_event, _CAPTURE_RETRY_SECONDS)
        finally:
            if recording_path is not None:
                shutil.rmtree(recording_path.parent, ignore_errors=True)
            if (
                manager.state is state
                and not stop_event.is_set()
                and getattr(manager, "_voice_ai_enabled", False)
            ):
                _set_ai_state(manager, AI_LISTENING)


async def _voice_worker_supervisor(
    manager,
    user_id: int,
    state,
    stop_event: asyncio.Event,
) -> None:
    """Keep one AI worker alive for the lifetime of the enabled mode."""
    while not stop_event.is_set():
        try:
            await _voice_worker(manager, user_id, state, stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "AI voice worker stopped unexpectedly for hosted user %s; restarting.",
                user_id,
            )
        if (
            stop_event.is_set()
            or manager.state is not state
            or state.closing
            or not getattr(manager, "_voice_ai_enabled", False)
        ):
            return
        logger.warning(
            "AI voice worker returned while enabled for hosted user %s; restarting.",
            user_id,
        )
        _set_ai_state(manager, AI_LISTENING)
        await _wait_or_stop(stop_event, _CAPTURE_RETRY_SECONDS)


async def _disable(manager) -> None:
    await manager.stop_ai_voice()


def _status_text(manager, prompt: str) -> str:
    state = manager.state
    if state is None:
        return "❌ Not connected to any Voice Chat. Use `.vcjoin` first."
    enabled = bool(getattr(manager, "_voice_ai_enabled", False))
    activity = getattr(manager, "_voice_ai_state", AI_LISTENING) if enabled else "IDLE"
    return (
        "🎙️ <b>AI voice mode</b>\n"
        f"Status: <b>{'ON' if enabled else 'OFF'}</b>\n"
        f"Group: <code>{state.chat_id}</code>\n"
        f"Activity: <b>{activity}</b>\n"
        f"Prompt: <b>{'custom' if prompt != _DEFAULT_PROMPT else 'default'}</b>\n"
        f"Gemini keys available: <b>{'yes' if get_keys() else 'no'}</b>"
    )


def init(client):
    add_handler(
        "voice_ai",
        [
            ".vcaion — Enable AI voice mode in the connected Voice Chat",
            ".vcaioff — Disable AI voice mode",
            ".vcaistatus — Show AI voice mode status",
            ".aiprompt <text> — Set the per-user AI voice prompt",
        ],
        "Per-user Gemini speech-to-text and spoken Voice Chat replies",
    )

    @client.on(events.NewMessage(pattern=r"(?i)^\.vcaion$"))
    @rishabh()
    async def vcaion_command(event):
        manager = _manager(event)
        if manager is None or manager.state is None:
            await event.reply("❌ Join an active Voice Chat first with `.vcjoin`.")
            return
        if not get_keys():
            await event.reply(
                "❌ No Gemini key is available. Add one with `.addkey <API_KEY>`."
            )
            return
        if getattr(manager, "_voice_ai_enabled", False):
            await event.reply("🎙️ AI voice mode is already ON.")
            return
        prompt = await _load_prompt(manager, _hosted_user_id(event))
        stop_event = asyncio.Event()
        manager._voice_ai_enabled = True
        _set_ai_state(manager, AI_LISTENING)
        manager._voice_ai_stop_event = stop_event
        task = asyncio.create_task(
            _voice_worker_supervisor(
                manager,
                _hosted_user_id(event),
                manager.state,
                stop_event,
            )
        )
        manager._voice_ai_task = task
        await event.reply(
            "🎙️ <b>AI voice mode ON.</b>\n"
            "I will transcribe speech in this Voice Chat, ask Gemini, and "
            "play the reply here.\n"
            f"Prompt: <code>{escape(prompt[:160])}</code>",
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"(?i)^\.vcaioff$"))
    @rishabh()
    async def vcaioff_command(event):
        manager = _manager(event)
        if manager is None:
            return
        if not getattr(manager, "_voice_ai_enabled", False):
            await event.reply("🎙️ AI voice mode is already OFF.")
            return
        await _disable(manager)
        await event.reply("🎙️ <b>AI voice mode OFF.</b>", parse_mode="html")

    @client.on(events.NewMessage(pattern=r"(?i)^\.vcaistatus$"))
    @rishabh()
    async def vcaistatus_command(event):
        manager = _manager(event)
        if manager is None:
            await event.reply("❌ Voice Chat manager is unavailable.")
            return
        prompt = await _load_prompt(manager, _hosted_user_id(event))
        await event.reply(_status_text(manager, prompt), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"(?i)^\.aiprompt(?:\s+(.+))?$"))
    @rishabh()
    async def aiprompt_command(event):
        manager = _manager(event)
        if manager is None:
            return
        user_id = _hosted_user_id(event)
        value = (event.pattern_match.group(1) or "").strip()
        if not value:
            prompt = await _load_prompt(manager, user_id)
            await event.reply(
                f"🎙️ Current AI voice prompt:\n<code>{escape(prompt[:1600])}</code>",
                parse_mode="html",
            )
            return
        if value.lower() in {"reset", "clear", "default"}:
            await _save_prompt(manager, user_id, "")
            await event.reply("✅ AI voice prompt reset to the default.")
            return
        await _save_prompt(manager, user_id, value)
        await event.reply("✅ Per-user AI voice prompt saved.")
