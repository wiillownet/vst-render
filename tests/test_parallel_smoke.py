"""End-to-end smoke: render 2 presets in parallel via ParallelBatchRenderer."""
from __future__ import annotations

from pathlib import Path

import mido
import numpy as np
import pytest

from vst_render import ParallelBatchRenderer, RenderConfig


@pytest.mark.slow
def test_parallel_render_produces_audio(plugin_path, preset_files):
    config = RenderConfig(
        plugin_path=plugin_path,
        sample_rate=44100,
        note=48,
        velocity=127,
        duration=1.0,
        tail=1.0,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(preset_files)

    assert len(results) == len(preset_files), "expected one audio array per preset"
    for fxp_path, audio in results.items():
        assert audio.shape[0] == 2, f"{fxp_path}: expected stereo"
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5, f"{fxp_path}: audio is silent"


def _write_midi_sequence(path: Path, beats: int) -> None:
    """
    Minimal Type 1 MIDI: one held note of `beats` beats at default tempo
    (120 BPM, 480 ticks/beat -> 0.5 s/beat). Total duration = beats * 0.5.
    """
    mid = mido.MidiFile(type=1)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.Message("note_on", note=60, velocity=100, time=0))
    track.append(mido.Message("note_off", note=60, velocity=64, time=beats * 480))
    mid.save(str(path))


@pytest.mark.slow
def test_parallel_render_with_midi_file(plugin_path, preset_files, tmp_path):
    """`--midi` path: worker must load the MIDI file and render for
    (midi_duration + tail) seconds instead of (duration + tail)."""
    midi_path = tmp_path / "seq.mid"
    _write_midi_sequence(midi_path, beats=4)  # 4 beats @ 120 BPM = 2.0 s
    expected_midi_duration = 2.0
    tail = 0.5
    sample_rate = 44100

    config = RenderConfig(
        plugin_path=plugin_path,
        sample_rate=sample_rate,
        midi_path=midi_path,
        tail=tail,
        # `note` / `duration` must not affect this render when midi_path is set.
        note=48,
        duration=0.1,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(preset_files)

    assert len(results) == len(preset_files)
    expected_duration = expected_midi_duration + tail

    for fxp_path, audio in results.items():
        assert audio.shape[0] == 2, f"{fxp_path}: expected stereo"
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5, f"{fxp_path}: silent output"
        actual_seconds = audio.shape[1] / sample_rate
        # Allow a small slack for the engine's buffer-boundary rounding
        # (DawDreamer rounds render duration up to the next 512-sample buffer).
        assert abs(actual_seconds - expected_duration) < 0.05, (
            f"{fxp_path}: expected ~{expected_duration:.2f}s, got {actual_seconds:.2f}s"
        )
        # Sanity: actual duration must exceed a single-note default render
        # (0.1s duration + 0.5s tail = 0.6s). If MIDI were ignored the audio
        # would be ~0.6s long, not ~2.5s.
        assert actual_seconds > 1.0, (
            f"{fxp_path}: got {actual_seconds:.2f}s — MIDI file not loaded?"
        )
