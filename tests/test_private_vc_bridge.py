"""Unit integration tests for the private VC-to-VC bridge (no Telegram calls)."""

import asyncio
import contextlib
import shutil
import tempfile
import struct
import sys
from pathlib import Path

import pytest

# The bridge module imports "database.mongo" and "plugins.bot".  Stub both
# so the test can drive the pure-Python logic (DSP, sessions, teardown).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram_userbot"))
import types

fake_db: object = type("FakeDb", (), {})()
fake_add_handler: object = lambda *a, **k: None
fake_bot_module: object = type("FakeBot", (), {"add_handler": staticmethod(fake_add_handler)})()

sys.modules["plugins.bot"] = fake_bot_module
sys.modules["database.mongo"] = fake_db

import plugins.private_vc_bridge as bridge
from telethon.tl import types as tl_types


def _pcm16(n_samples: int, value: int = 3000) -> bytes:
    return struct.pack("<%dh" % n_samples, *([value] * n_samples))


class FakeCalls:
    def __init__(self):
        self.calls_prop: dict[int, bool] = {}
        self.played: list[int] = []
        self.frame_sinks: dict[int, list[bytes]] = {}
        self.left: list[int] = []

    @property
    def calls(self):
        return self.calls_prop

    async def play(self, chat_id, stream=None, config=None):
        self.played.append(chat_id)
        if stream is not None:
            self.calls_prop[int(chat_id)] = True
            self.frame_sinks[int(chat_id)] = []
        else:
            self.calls_prop.pop(int(chat_id), None)

    async def leave_call(self, chat_id, close=False):
        self.left.append(chat_id)
        self.calls_prop.pop(int(chat_id), None)

    async def send_frame(self, chat_id, device, data, frame_data=None):
        self.frame_sinks.setdefault(int(chat_id), []).append(data)


class FakeVoiceManager:
    def __init__(self, client):
        self.client = client
        self.calls = FakeCalls()
        self.state = type("State", (), {"chat_id": 999001})()
        self.started = False
        self._fake_entity = type("Ent", (), {"megagroup": False, "chat_type": "Group"}) ()

    async def start(self):
        self.started = True

    async def _active_group_call(self, entity):
        return object()  # an active group call exists


def _bare_to_peer(bare: int) -> int:
    # get_peer_id(Channel(id=bare)) => -(1_000_000_000_000 + bare).
    return -(1000000000000 + bare)


def _peer_to_bare(peer: int) -> int:
    return -peer - 1000000000000


class FakeClient:
    def __init__(self):
        self.peers: dict[str, int] = {}
        self.chat_titles: dict[int, str] = {}

    async def get_entity(self, identifier):
        key = str(identifier).lstrip("@")
        peer_by_key = {k: _bare_to_peer(v) for k, v in self.peers.items()}
        if key not in peer_by_key:
            raise ValueError("no such entity")
        peer_id = peer_by_key[key]
        bare = _peer_to_bare(peer_id)
        return tl_types.Channel(
            id=bare,
            title=self.chat_titles.get(bare, "Fake Group"),
            megagroup=True,
            broadcast=False,
            photo=None,
            date=0,
        )


PEER1 = _bare_to_peer(1002)
PEER2 = _bare_to_peer(1003)


@pytest.fixture
def wiring():
    client = FakeClient()
    client.peers["targ"] = 1002
    client.peers["targ1003"] = 1003
    client.peers[str(_bare_to_peer(1002))] = 1002
    client.peers[str(_bare_to_peer(1003))] = 1003
    client.chat_titles[1002] = "Target Group"
    client.chat_titles[1003] = "Other Group"
    voice = FakeVoiceManager(client)
    manager = bridge.VCBridgeManager(voice)
    manager.calls.frame_sinks[PEER1] = []
    return client, voice, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("stereo", [True, False])
async def test_dsp_volume_bass_mute(wiring, stereo):
    _, _, manager = wiring
    raw = _pcm16(960 if stereo else 480, 3000)
    processed = manager._process_audio(bridge.BridgeSession(target_chat_id=1, source_chat_id=2), raw)
    # default level 5 -> volume 1.0 unchanged
    sample = struct.unpack("<h", processed[:2])[0]
    assert sample == 3000
    # bass boost
    session = bridge.BridgeSession(target_chat_id=1, source_chat_id=2)
    session.bass = 10
    boosted = manager._process_audio(session, raw)
    assert len(boosted) == len(processed)
    # mute zeroes
    session.muted = True
    muted = manager._process_audio(session, raw)
    assert set(muted) == {0, 0}


@pytest.mark.asyncio
async def test_dsp_bass_coeffs_stable(wiring):
    _, _, manager = wiring
    session = bridge.BridgeSession(target_chat_id=1, source_chat_id=2)
    session.bass = 8
    a = manager._process_audio(session, _pcm16(960))
    b = manager._process_audio(session, _pcm16(960))
    assert session.bass_coeffs is not None
    assert len(session.bass_coeffs) == 5


@pytest.mark.asyncio
async def test_join_resolves_target_and_starts_forwarding(wiring):
    client, voice, manager = wiring
    text = await manager.join("targ", 999001)
    assert voice.started
    assert manager.calls.played == [PEER1]
    assert PEER1 in manager.calls.calls_prop
    session = manager.sessions[PEER1]
    assert session.source_chat_id == 999001
    assert session.target_chat_id == PEER1
    assert "Target Group" in text


@pytest.mark.asyncio
async def test_join_rejects_same_chat(wiring):
    _, _, manager = wiring
    with pytest.raises(ValueError):
        await manager.join("targ", PEER1)


@pytest.mark.asyncio
async def test_on_source_frames_feeds_queues(wiring):
    _, _, manager = wiring
    await manager.join("targ", 999001)
    payload = _pcm16(480)
    manager.on_source_frames(999001, payload)
    session = manager.sessions[PEER1]
    assert session.input_queue.qsize() == 1
    assert session.input_queue.get_nowait() == payload


@pytest.mark.asyncio
async def test_send_loop_forwards_frames(wiring):
    _, _, manager = wiring
    await manager.join("targ", 999001)
    session = manager.sessions[PEER1]
    payload = _pcm16(480)
    manager.on_source_frames(999001, payload)

    async def drain():
        while session.frames_forwarded == 0:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(drain(), 5)
    assert len(manager.calls.frame_sinks[PEER1]) >= 1
    assert session.frames_forwarded == len(manager.calls.frame_sinks[PEER1])
    assert session.bytes_forwarded == sum(len(f) for f in manager.calls.frame_sinks[PEER1])


@pytest.mark.asyncio
async def test_recording_writes_wav(wiring):
    _, _, manager = wiring
    await manager.join("targ", 999001)
    session = manager.sessions[PEER1]
    payload = _pcm16(960)
    manager.on_source_frames(999001, payload)
    session.recording = True
    tmpdir = Path(tempfile.mkdtemp(prefix="bridge-rec-test-"))
    outputd = tmpdir / "out.wav"
    writer = bridge._WavWriter(outputd)
    writer.write(payload)
    writer.finish()
    data = outputd.read_bytes()
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert data[36:40] == b"data"
    assert struct.unpack("<I", data[40:44])[0] == len(payload)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_leave_and_leave_all_and_mute(wiring):
    _, _, manager = wiring
    await manager.join("targ", 999001)
    await manager.join("targ1003", 999001)
    assert await manager.leave_all() == 2
    assert manager.calls.calls_prop == {}
    assert len(manager.calls.left) == 2
    assert manager.sessions == {}


@pytest.mark.asyncio
async def test_unhost_cleanup_shuts_bridge(wiring):
    client, voice, manager = wiring
    await manager.join("targ", 999001)
    session = manager.sessions[PEER1]
    session.send_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await session.send_task
    session.send_task = None
    await manager.shutdown()
    assert manager.sessions == {}
    assert manager.calls.calls_prop == {}
    assert not manager._watchdog_task or manager._watchdog_task.done()
