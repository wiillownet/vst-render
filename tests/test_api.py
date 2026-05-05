"""Unit coverage for the BatchRenderer / ParallelBatchRenderer guards
that don't require a plugin (R6 thread-safety, R7 config freezing).
End-to-end behavior is covered by the smoke tests."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from vst_render import BatchRenderer, ParallelBatchRenderer, RenderConfig


# ---- R6: thread-safety guard ---------------------------------------------


def test_batch_renderer_render_rejects_non_main_thread(tmp_path):
    """DawDreamer hangs when called from a thread after the first render.
    The guard surfaces the foot-gun loudly instead of silently hanging."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")

    cfg = RenderConfig(plugin_path=plugin)
    r = BatchRenderer(cfg)
    # No __enter__ here — the thread guard fires before the context
    # manager check, so we can verify it without loading DawDreamer.

    captured: list[Exception] = []

    def call_from_thread():
        try:
            r.render(tmp_path / "fake.fxp")
        except Exception as exc:
            captured.append(exc)

    t = threading.Thread(target=call_from_thread)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "render() didn't return — the guard hung"
    assert len(captured) == 1
    exc = captured[0]
    assert isinstance(exc, RuntimeError)
    assert "main thread" in str(exc).lower()


# ---- R7: config freezing on __enter__ ------------------------------------


def test_parallel_batch_renderer_freezes_config_on_enter(tmp_path):
    """Mutating self.config after __enter__ used to leak through to
    _build_jobs (it reads self.config lazily) while _midi_duration was
    already pinned at __enter__. This test verifies the frozen copy
    doesn't reflect post-enter mutations."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")

    cfg = RenderConfig(plugin_path=plugin, note=48, velocity=100)
    r = ParallelBatchRenderer(cfg, workers=1)
    with r:
        # Pre-mutation: frozen copy matches the originals.
        assert r._frozen_config.note == 48
        assert r._frozen_config.velocity == 100

        # Mutate the original config object.
        cfg.note = 99
        cfg.velocity = 1

        # Frozen copy must NOT reflect the mutation.
        assert r._frozen_config.note == 48
        assert r._frozen_config.velocity == 100


def test_parallel_batch_renderer_build_jobs_uses_frozen_config(tmp_path):
    """The job dicts handed to workers must reference the frozen config,
    not self.config — otherwise post-enter mutation desyncs midi_path
    from the already-computed midi_duration."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")

    cfg = RenderConfig(plugin_path=plugin, note=48)
    r = ParallelBatchRenderer(cfg, workers=1)
    with r:
        cfg.note = 99  # post-enter mutation
        jobs = r._build_jobs([tmp_path / "p.fxp"])
        assert jobs[0]["note"] == 48, (
            "job inherited post-enter mutation; freeze didn't take effect"
        )


def test_parallel_batch_renderer_build_jobs_outside_context(tmp_path):
    """Calling _build_jobs without entering the context must error
    cleanly rather than producing jobs with stale/None values."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")

    cfg = RenderConfig(plugin_path=plugin)
    r = ParallelBatchRenderer(cfg, workers=1)
    with pytest.raises(RuntimeError, match="context manager"):
        r._build_jobs([Path("p.fxp")])
