"""Tests for the TTS idle-unload behavior.

The full idle-unload loop runs as a long-lived asyncio task started by
the FastAPI lifespan. The unit tests below exercise the building blocks:

  * :meth:`OmniVoiceTTS.seconds_since_last_used` returns None before the
    first load and a positive number after.
  * :func:`audiomat.state.idle_unload_loop` calls ``unload`` once the
    idle threshold is exceeded, and skips the unload while a render
    thread is alive.

The model itself is never instantiated — we patch out the heavy bits.
"""
from __future__ import annotations

import asyncio
import threading
import time

from audiomat.tts import OmniVoiceTTS


class TestSecondsSinceLastUsed:
    def test_none_before_load(self):
        tts = OmniVoiceTTS()
        assert tts.seconds_since_last_used() is None

    def test_positive_after_use(self):
        tts = OmniVoiceTTS()
        tts._last_used = time.monotonic() - 5.0
        elapsed = tts.seconds_since_last_used()
        assert elapsed is not None
        assert 4.5 <= elapsed <= 6.0


class _StubLoadedTTS:
    """Minimal stub matching the bits of OmniVoiceTTS the loop touches."""

    def __init__(self, idle_seconds: float):
        self._idle = idle_seconds
        self.unload_calls = 0
        self.is_loaded = True

    def seconds_since_last_used(self) -> float:
        return self._idle

    def unload(self) -> None:
        self.unload_calls += 1
        self.is_loaded = False


def _run_loop_one_tick(*, idle_seconds: float, timeout_s: int,
                       render_alive: bool = False) -> _StubLoadedTTS:
    """Drive idle_unload_loop through exactly one wakeup with a stub
    instance injected into the real ``_TTS_INSTANCES`` registry, then
    cancel. Returns the stub so tests can assert on ``unload_calls``.

    Why inject into ``_TTS_INSTANCES`` rather than patching ``peek_tts``?
    The v0.4 multi-model refactor rewrote ``idle_unload_loop`` to iterate
    ``_TTS_INSTANCES`` directly — patching ``peek_tts`` no longer
    intercepts anything. Matching the loop's actual code path is the
    only way to keep this test honest.
    """
    import audiomat.state as state

    stub = _StubLoadedTTS(idle_seconds=idle_seconds)
    original_threads = dict(state.RENDER_THREADS)

    state.RENDER_THREADS.clear()
    if render_alive:
        # Spin up a real-but-trivial thread that stays alive long enough
        # for the loop's any(...is_alive()) check.
        evt = threading.Event()
        t = threading.Thread(target=evt.wait, daemon=True)
        t.start()
        state.RENDER_THREADS["dummy"] = t
        cleanup_evt = evt
    else:
        cleanup_evt = None

    # Inject the stub into the registry the loop actually walks. The
    # key is arbitrary — the loop iterates values, not keys.
    stub_key = "__idle_unload_test_stub__"
    with state._TTS_LOCK:
        original_instances = dict(state._TTS_INSTANCES)
        state._TTS_INSTANCES.clear()
        state._TTS_INSTANCES[stub_key] = stub  # type: ignore[assignment]

    async def _drive():
        task = asyncio.create_task(
            state.idle_unload_loop(timeout_s=timeout_s, interval_s=0)
        )
        # Yield control so the loop runs at least one iteration.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_drive())
    finally:
        with state._TTS_LOCK:
            state._TTS_INSTANCES.clear()
            state._TTS_INSTANCES.update(original_instances)
        state.RENDER_THREADS.clear()
        state.RENDER_THREADS.update(original_threads)
        if cleanup_evt is not None:
            cleanup_evt.set()
    return stub


class TestIdleUnloadLoop:
    def test_unloads_when_idle_exceeds_timeout(self):
        stub = _run_loop_one_tick(idle_seconds=120, timeout_s=60)
        assert stub.unload_calls >= 1, "expected unload after idle > timeout"

    def test_does_not_unload_when_under_timeout(self):
        stub = _run_loop_one_tick(idle_seconds=5, timeout_s=60)
        assert stub.unload_calls == 0

    def test_skips_unload_while_render_alive(self):
        stub = _run_loop_one_tick(idle_seconds=120, timeout_s=60,
                                   render_alive=True)
        assert stub.unload_calls == 0, (
            "must not unload while a render thread is still running — "
            "would force a wasted reload on the next chunk"
        )
