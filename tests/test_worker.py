"""Unit coverage for vst_render.worker primitives that don't require a
real plugin. The full render path is exercised by the parallel + serum2
smoke tests."""
from __future__ import annotations

import pytest

import vst_render.worker as worker
from vst_render.worker import _do_render, init_worker


# ---- _do_render guards ----------------------------------------------------


def test_do_render_raises_when_midi_path_set_without_duration():
    """midi_path + midi_duration are populated together by every current
    caller. The guard pins that contract so a future caller that forgets
    midi_duration gets a clear ValueError, not a confusing
    `TypeError: unsupported operand ... NoneType + float` from a few lines
    down."""
    job = {
        "preset_path": "/anywhere",
        "preset_format": "fxp",
        "midi_path": "/some/seq.mid",
        "midi_duration": None,
        "note": 48,
        "velocity": 127,
        "duration": 1.0,
        "tail": 1.0,
    }
    with pytest.raises(ValueError, match="midi_duration"):
        _do_render(job)


def test_do_render_rejects_unknown_preset_format():
    job = {
        "preset_path": "/anywhere",
        "preset_format": "vital",
        "midi_path": None,
        "midi_duration": None,
        "note": 48,
        "velocity": 127,
        "duration": 1.0,
        "tail": 1.0,
    }
    with pytest.raises(ValueError, match="Unknown preset_format"):
        _do_render(job)


def test_do_render_fxp_job_without_fxp_synth_raises(monkeypatch):
    """A worker booted with serum2-only (no fxp synth) that gets handed an
    fxp job must fail clean, not None-deref."""
    monkeypatch.setattr(worker, "_synth_fxp", None)
    monkeypatch.setattr(worker, "_synth_serum2", object())  # truthy, won't be touched
    job = {
        "preset_path": "/anywhere.fxp",
        "preset_format": "fxp",
        "midi_path": None,
        "midi_duration": None,
        "note": 48,
        "velocity": 127,
        "duration": 1.0,
        "tail": 1.0,
    }
    with pytest.raises(RuntimeError, match="no fxp synth"):
        _do_render(job)


def test_do_render_serum2_job_without_serum2_synth_raises(monkeypatch):
    """Mirror of the previous test for the serum2 dispatch arm."""
    monkeypatch.setattr(worker, "_synth_fxp", object())
    monkeypatch.setattr(worker, "_synth_serum2", None)
    job = {
        "preset_path": "/anywhere.SerumPreset",
        "preset_format": "serum2",
        "midi_path": None,
        "midi_duration": None,
        "note": 48,
        "velocity": 127,
        "duration": 1.0,
        "tail": 1.0,
    }
    with pytest.raises(RuntimeError, match="no serum2 synth"):
        _do_render(job)


def test_do_render_serum2_dispatch_calls_load_state(monkeypatch, tmp_path):
    """Serum 2 jobs must round-trip through convert_preset_file → write_bytes
    → load_state, not load_preset. Mock both the converter and the synth
    so this test runs without a plugin."""
    fake_blob = b"\x00\x01STATEBLOB\x02\x03"
    state_path = tmp_path / "state.bin"

    calls: dict[str, object] = {}

    def fake_convert(path):
        calls["convert_called_with"] = path
        return fake_blob

    class FakeSynth:
        def load_preset(self, path):
            calls["load_preset_called"] = True

        def load_state(self, path):
            calls["load_state_called_with"] = path

        def clear_midi(self):
            calls["clear_midi_called"] = True

        def add_midi_note(self, *a, **kw):
            pass

    class FakeEngine:
        def render(self, dur):
            calls["render_called_with"] = dur

        def get_audio(self):
            import numpy as np
            return np.zeros((2, 4410), dtype="float32")

    monkeypatch.setattr(worker, "_engine", FakeEngine())
    monkeypatch.setattr(worker, "_synth_fxp", None)
    monkeypatch.setattr(worker, "_synth_serum2", FakeSynth())
    monkeypatch.setattr(worker, "_serum_state_path", state_path)
    # The deferred import inside `_do_render` looks the symbol up in
    # the serum2_preset_loader module — patch it there.
    import serum2_preset_loader as spl
    monkeypatch.setattr(spl, "convert_preset_file", fake_convert)

    job = {
        "preset_path": "/some/preset.SerumPreset",
        "preset_format": "serum2",
        "midi_path": None,
        "midi_duration": None,
        "note": 48,
        "velocity": 127,
        "duration": 0.1,
        "tail": 0.1,
    }
    _do_render(job)

    assert calls.get("convert_called_with") == "/some/preset.SerumPreset"
    assert calls.get("load_state_called_with") == str(state_path)
    assert "load_preset_called" not in calls, (
        "serum2 dispatch must not call load_preset"
    )
    # The blob must have actually been written, with exact contents.
    assert state_path.read_bytes() == fake_blob


# ---- init_worker validation ----------------------------------------------


def test_init_worker_rejects_all_none_paths():
    """Loky is responsible for invoking init_worker once per worker; a
    silently-succeeding init with no synths would only blow up on the
    first job dispatch. Fail at init time instead."""
    with pytest.raises(ValueError, match="at least one"):
        init_worker(None, None, 44100)
