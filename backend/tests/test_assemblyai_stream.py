"""Offline tests: no network, no API key. The SDK client is replaced by a fake."""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest

import app.services.assemblyai_stream as mod


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, *_a, **_k):
        self.handlers = {}
        self.sent = []
        self.params = None
        FakeClient.instances.append(self)

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def connect(self, params):
        self.params = params
        self.handlers[mod.StreamingEvents.Begin](self, NS(type="Begin"))

    def stream(self, chunk):
        self.sent.append(chunk)

    def disconnect(self, terminate=False):
        pass

    def emit_turn(self, order, text, formatted, start=100, end=900, conf=0.9):
        words = [NS(start=start, end=end, confidence=conf)]
        self.handlers[mod.StreamingEvents.Turn](
            self,
            NS(
                end_of_turn=True,
                transcript=text,
                turn_order=order,
                turn_is_formatted=formatted,
                words=words,
            ),
        )


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(mod, "StreamingClient", FakeClient)
    monkeypatch.setattr(mod, "FORMAT_WAIT_S", 0.2)


def _session(turns, statuses, **kw):
    async def on_turn(t):
        turns.append(t)

    async def on_status(s):
        statuses.append(s)

    return mod.AssemblyAISpeakerStream(
        api_key="k", speaker="candidate", on_turn=on_turn, on_status=on_status, **kw
    )


async def _wait(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        await asyncio.sleep(0.02)


def test_formatted_turn_is_emitted_once_with_speaker_and_times():
    async def go():
        turns, statuses = [], []
        s = _session(turns, statuses, keyterms=["FastAPI"] * 150)
        await s.start()
        await _wait(lambda: FakeClient.instances and "ready" in statuses)
        c = FakeClient.instances[0]
        assert len(c.params.keyterms_prompt) == 100
        s.feed(b"\x00\x00" * 4000)
        await _wait(lambda: c.sent)
        c.emit_turn(0, "hello there", formatted=False)
        c.emit_turn(0, "Hello there.", formatted=True)
        await _wait(lambda: turns)
        await s.stop()
        assert [t.text for t in turns] == ["Hello there."]
        assert turns[0].speaker == "candidate" and (turns[0].start_ms, turns[0].end_ms) == (100, 900)

    asyncio.run(go())


def test_unformatted_turn_is_used_when_formatted_copy_never_arrives():
    async def go():
        turns, statuses = [], []
        s = _session(turns, statuses)
        await s.start()
        await _wait(lambda: FakeClient.instances and "ready" in statuses)
        FakeClient.instances[0].emit_turn(3, "no format came", formatted=False)
        await _wait(lambda: turns, timeout=2)
        await s.stop()
        assert [t.text for t in turns] == ["no format came"]

    asyncio.run(go())


def test_empty_and_duplicate_turns_are_ignored():
    async def go():
        turns, statuses = [], []
        s = _session(turns, statuses)
        await s.start()
        await _wait(lambda: FakeClient.instances and "ready" in statuses)
        c = FakeClient.instances[0]
        c.emit_turn(0, "   ", formatted=True)
        c.emit_turn(1, "One.", formatted=True)
        c.emit_turn(1, "One again.", formatted=True)
        await _wait(lambda: turns)
        await asyncio.sleep(0.2)
        await s.stop()
        assert [t.text for t in turns] == ["One."]

    asyncio.run(go())


def test_full_queue_counts_dropped_frames_and_never_blocks():
    async def noop(_t):
        return None

    s = mod.AssemblyAISpeakerStream(
        api_key="k", speaker="x", on_turn=noop, max_queued_frames=2
    )
    for _ in range(5):
        s.feed(b"\x00\x00")
    assert s.dropped_frames == 3


def test_missing_key_is_rejected():
    async def noop(_t):
        return None

    with pytest.raises(ValueError):
        mod.AssemblyAISpeakerStream(api_key="", speaker="x", on_turn=noop)


def test_language_is_passed_only_when_pinned():
    async def go():
        turns, statuses = [], []
        s = _session(turns, statuses, language_code="en")
        await s.start()
        await _wait(lambda: FakeClient.instances and "ready" in statuses)
        assert FakeClient.instances[0].params.language_code == "en"
        await s.stop()
        FakeClient.instances.clear()
        statuses.clear()
        s2 = _session(turns, statuses)
        await s2.start()
        await _wait(lambda: FakeClient.instances and "ready" in statuses)
        assert FakeClient.instances[0].params.language_code is None
        await s2.stop()

    asyncio.run(go())
