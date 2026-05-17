"""Serum 2 + mixed-format smoke tests.

Two entry-points exercised:
 - `run_batch_to_disk` (the path the CLI uses) — required when verifying
   disk-output behavior or when sending a job dict directly.
 - `ParallelBatchRenderer.render_batch` — the public library API, which
   gained Serum 2 support after the `_require_fxp_plugin` gate was lifted.

Each test is gated on an independent fixture, so a user with only one
plugin still runs the smoke half they have plumbing for.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vst_render import BatchRenderer, ParallelBatchRenderer, RenderConfig
from vst_render.batch import run_batch_to_disk
from vst_render.config import SILENCE_EPS


def _check_audio(wav_path: Path) -> None:
    """Verify the rendered .wav is stereo, non-silent, and the right sample rate."""
    audio, sample_rate = sf.read(str(wav_path))
    assert sample_rate == 44100, f"{wav_path}: sample rate {sample_rate} != 44100"
    # soundfile returns (samples, channels) — stereo means second axis == 2.
    assert audio.ndim == 2 and audio.shape[1] == 2, (
        f"{wav_path}: expected stereo, got shape {audio.shape}"
    )
    assert np.max(np.abs(audio)) > SILENCE_EPS, f"{wav_path}: silent output"


def _make_job(preset_path: str, preset_format: str, output_path: Path) -> dict:
    """Build a job dict matching the schema in CLAUDE.md.

    Mirrors what cli.py constructs after `compose_filename` /
    `assign_output_paths` finish, but skips template processing —
    the test fixes preset_path → output_path directly."""
    return {
        "preset_path": preset_path,
        "preset_format": preset_format,
        "output_path": str(output_path),
        "note": 48,
        "velocity": 127,
        "duration": 1.0,
        "tail": 1.0,
        "midi_path": None,
        "midi_duration": None,
        "sample_rate": 44100,
        "bit_depth": "16",
        "format": "wav",
        "skip_existing": False,
    }


@pytest.mark.slow
def test_serum2_render_produces_audio(
    serum2_plugin_path, serum_preset_files, tmp_path
):
    """Two .SerumPreset files rendered through the dual-synth worker with
    only the serum2 plugin loaded. Validates the convert_preset_file →
    write_bytes → load_state round-trip end-to-end."""
    jobs = [
        _make_job(
            preset_path=src,
            preset_format="serum2",
            output_path=tmp_path / f"out_{i}.wav",
        )
        for i, src in enumerate(serum_preset_files)
    ]

    results = run_batch_to_disk(
        jobs=jobs,
        workers=2,
        fxp_plugin_path=None,
        serum2_plugin_path=serum2_plugin_path,
        sample_rate=44100,
    )

    assert len(results) == len(jobs)
    for r in results:
        assert r["status"] == "ok", f"{r.get('path')}: {r.get('error')}"
    for j in jobs:
        _check_audio(Path(j["output_path"]))


@pytest.mark.slow
def test_mixed_format_smoke(
    fxp_plugin_path,
    serum2_plugin_path,
    preset_files,
    serum_preset_files,
    tmp_path,
):
    """Final acceptance gate: one .fxp and one .SerumPreset rendered in the
    same batch with both plugin paths supplied. Each job dispatches to its
    matching synth in the same worker; an idle synth in the shared graph
    is byte-identical silence (probe 1)."""
    fxp_src = preset_files[0]
    serum_src = serum_preset_files[0]

    jobs = [
        _make_job(
            preset_path=fxp_src,
            preset_format="fxp",
            output_path=tmp_path / "fxp_out.wav",
        ),
        _make_job(
            preset_path=serum_src,
            preset_format="serum2",
            output_path=tmp_path / "serum2_out.wav",
        ),
    ]

    results = run_batch_to_disk(
        jobs=jobs,
        workers=2,
        fxp_plugin_path=fxp_plugin_path,
        serum2_plugin_path=serum2_plugin_path,
        sample_rate=44100,
    )

    assert len(results) == 2
    # Order results by preset_path so the assertions don't race the
    # unordered dispatch of run_batch_to_disk.
    by_path = {r["path"]: r for r in results}
    assert by_path[fxp_src]["status"] == "ok", by_path[fxp_src].get("error")
    assert by_path[serum_src]["status"] == "ok", by_path[serum_src].get("error")

    _check_audio(tmp_path / "fxp_out.wav")
    _check_audio(tmp_path / "serum2_out.wav")


@pytest.mark.slow
def test_parallel_batch_renderer_serum2_smoke(
    serum2_plugin_path, serum_preset_files
):
    """The public `ParallelBatchRenderer` API must accept a serum2-only
    config and a list of `.SerumPreset` paths, returning non-silent stereo
    audio. This is the gate-lift acceptance test — pre-0.2.x this raised
    `NotImplementedError` because the library API was hardcoded to fxp."""
    config = RenderConfig(
        serum2_plugin_path=serum2_plugin_path,
        sample_rate=44100,
        note=48,
        velocity=127,
        duration=1.0,
        tail=1.0,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(serum_preset_files)

    assert len(results) == len(serum_preset_files)
    for preset_path, audio in results.items():
        assert audio.shape[0] == 2, f"{preset_path}: expected stereo"
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5, f"{preset_path}: silent output"


@pytest.mark.slow
def test_parallel_batch_renderer_mixed_format(
    fxp_plugin_path,
    serum2_plugin_path,
    preset_files,
    serum_preset_files,
):
    """Mixed batch through the public library API: one .fxp + one
    .SerumPreset returned as a path -> audio dict, with both formats
    auto-detected from the suffix."""
    fxp_src = preset_files[0]
    serum_src = serum_preset_files[0]
    config = RenderConfig(
        fxp_plugin_path=fxp_plugin_path,
        serum2_plugin_path=serum2_plugin_path,
        sample_rate=44100,
        note=48,
        duration=1.0,
        tail=1.0,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch([fxp_src, serum_src])

    assert set(results.keys()) == {fxp_src, serum_src}
    for path, audio in results.items():
        assert audio.shape[0] == 2 and audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5, f"{path}: silent"


@pytest.mark.slow
def test_batch_renderer_mixed_format(
    fxp_plugin_path,
    serum2_plugin_path,
    preset_files,
    serum_preset_files,
):
    """In-process `BatchRenderer` mixed-format round-trip. Symmetric to
    `test_parallel_batch_renderer_mixed_format` but goes through
    `make_engine` / `render_one` rather than the loky worker — the two
    paths share dispatch logic but boot the engine differently, so each
    needs its own end-to-end coverage."""
    fxp_src = preset_files[0]
    serum_src = serum_preset_files[0]
    config = RenderConfig(
        fxp_plugin_path=fxp_plugin_path,
        serum2_plugin_path=serum2_plugin_path,
        sample_rate=44100,
        note=48,
        duration=1.0,
        tail=1.0,
    )
    with BatchRenderer(config) as renderer:
        results = {path: renderer.render(path) for path in (fxp_src, serum_src)}

    for path, audio in results.items():
        assert audio.shape[0] == 2, f"{path}: expected stereo"
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > SILENCE_EPS, f"{path}: silent output"
