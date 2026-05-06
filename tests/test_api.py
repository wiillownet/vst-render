"""Unit coverage for the BatchRenderer / ParallelBatchRenderer guards
that don't require a plugin (R6 thread-safety, R7 config freezing,
format auto-detection + required-plugin checks).
End-to-end behavior is covered by the smoke tests."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from vst_render import BatchRenderer, ParallelBatchRenderer, RenderConfig
from vst_render.api import _check_required_plugins
from vst_render.presets import PresetFormat


# ---- R6: thread-safety guard ---------------------------------------------


def test_batch_renderer_render_rejects_non_main_thread(tmp_path):
    """DawDreamer hangs when called from a thread after the first render.
    The guard surfaces the foot-gun loudly instead of silently hanging."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")

    cfg = RenderConfig(fxp_plugin_path=plugin)
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

    cfg = RenderConfig(fxp_plugin_path=plugin, note=48, velocity=100)
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

    cfg = RenderConfig(fxp_plugin_path=plugin, note=48)
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

    cfg = RenderConfig(fxp_plugin_path=plugin)
    r = ParallelBatchRenderer(cfg, workers=1)
    with pytest.raises(RuntimeError, match="context manager"):
        r._build_jobs([Path("p.fxp")])


# ---- Format auto-detection in _build_jobs --------------------------------


def test_build_jobs_tags_fxp_path_with_fxp_format(tmp_path):
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")
    cfg = RenderConfig(fxp_plugin_path=plugin)
    with ParallelBatchRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "lead.fxp"])
    assert jobs[0]["preset_format"] == PresetFormat.FXP.value


def test_build_jobs_tags_serum_preset_path_with_serum2_format(tmp_path):
    plugin = tmp_path / "Serum2.vst3"
    plugin.write_bytes(b"")
    cfg = RenderConfig(serum2_plugin_path=plugin)
    with ParallelBatchRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "pad.SerumPreset"])
    assert jobs[0]["preset_format"] == PresetFormat.SERUM2.value


def test_build_jobs_tags_mixed_paths_per_path(tmp_path):
    """Format must be detected per path, not per batch — a mixed batch
    must produce one fxp-tagged and one serum2-tagged job."""
    fxp_plugin = tmp_path / "Serum.vst"
    fxp_plugin.write_bytes(b"")
    serum2_plugin = tmp_path / "Serum2.vst3"
    serum2_plugin.write_bytes(b"")
    cfg = RenderConfig(fxp_plugin_path=fxp_plugin, serum2_plugin_path=serum2_plugin)
    with ParallelBatchRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "a.fxp", tmp_path / "b.SerumPreset"])
    formats = [j["preset_format"] for j in jobs]
    assert formats == [PresetFormat.FXP.value, PresetFormat.SERUM2.value]


def test_build_jobs_rejects_unknown_suffix(tmp_path):
    """ParallelBatchRenderer should reject unknown extensions at job-build
    time so the user sees a clean error before any worker boots."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")
    cfg = RenderConfig(fxp_plugin_path=plugin)
    with ParallelBatchRenderer(cfg, workers=1) as r:
        with pytest.raises(ValueError, match="Unsupported preset suffix"):
            r._build_jobs([tmp_path / "weird.vital"])


# ---- _check_required_plugins guard ---------------------------------------


def test_check_required_plugins_passes_when_all_formats_have_paths(tmp_path):
    fxp_plugin = tmp_path / "Serum.vst"
    fxp_plugin.write_bytes(b"")
    cfg = RenderConfig(fxp_plugin_path=fxp_plugin)
    # No raise — fxp_plugin_path is set and only fxp is required.
    _check_required_plugins(cfg, {PresetFormat.FXP})


def test_check_required_plugins_rejects_serum2_without_serum2_path(tmp_path):
    fxp_plugin = tmp_path / "Serum.vst"
    fxp_plugin.write_bytes(b"")
    cfg = RenderConfig(fxp_plugin_path=fxp_plugin)
    with pytest.raises(ValueError, match=r"\.SerumPreset.*serum2_plugin_path"):
        _check_required_plugins(cfg, {PresetFormat.SERUM2})


def test_check_required_plugins_rejects_fxp_without_fxp_path(tmp_path):
    serum2_plugin = tmp_path / "Serum2.vst3"
    serum2_plugin.write_bytes(b"")
    cfg = RenderConfig(serum2_plugin_path=serum2_plugin)
    with pytest.raises(ValueError, match=r"\.fxp.*fxp_plugin_path"):
        _check_required_plugins(cfg, {PresetFormat.FXP})


def test_check_required_plugins_lists_both_missing_in_error(tmp_path):
    """If both formats are required but neither is set, the error message
    must mention both flags so the user fixes them in one pass."""
    # Construct an "impossible" config by patching plugin paths to None
    # post-init (RenderConfig.__post_init__ would otherwise reject).
    fxp = tmp_path / "Serum.vst"
    fxp.write_bytes(b"")
    cfg = RenderConfig(fxp_plugin_path=fxp)
    cfg.fxp_plugin_path = None  # bypass __post_init__ for the test
    with pytest.raises(ValueError) as excinfo:
        _check_required_plugins(cfg, {PresetFormat.FXP, PresetFormat.SERUM2})
    msg = str(excinfo.value)
    assert "fxp_plugin_path" in msg and "serum2_plugin_path" in msg


# ---- BatchRenderer construction is no longer fxp-only -------------------


def test_batch_renderer_accepts_serum2_only_config(tmp_path):
    """Constructing a BatchRenderer with only serum2_plugin_path used to
    blow up via _require_fxp_plugin; the gate has been lifted."""
    serum2 = tmp_path / "Serum2.vst3"
    serum2.write_bytes(b"")
    cfg = RenderConfig(serum2_plugin_path=serum2)
    # Construction is cheap — exercising __enter__ would actually load the
    # plugin, which we can't do without a real binary. The construction
    # path used to gate on fxp_plugin_path even before __enter__; verify
    # that gate is gone.
    BatchRenderer(cfg)


def test_parallel_batch_renderer_serum2_only_build_jobs(tmp_path):
    """ParallelBatchRenderer must accept a serum2-only config and produce
    a serum2-tagged job dict without requiring fxp_plugin_path."""
    serum2 = tmp_path / "Serum2.vst3"
    serum2.write_bytes(b"")
    cfg = RenderConfig(serum2_plugin_path=serum2)
    with ParallelBatchRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "pad.SerumPreset"])
    assert jobs[0]["preset_format"] == PresetFormat.SERUM2.value
