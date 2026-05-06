"""Serum 2 + mixed-format smoke tests.

`ParallelBatchRenderer` is still fxp-only at the public library API
(`_require_fxp_plugin` gate in api.py), so these tests drive the worker
pool the same way the CLI does — through `run_batch_to_disk` directly.
That's also the only path that can pass both a `--fxp` and a `--serum2`
plugin into one batch today.

Each test is gated on an independent fixture, so a user with only one
plugin still runs the smoke half they have plumbing for.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vst_render.batch import run_batch_to_disk


SILENCE_EPS = 3.16e-5  # -90 dBFS — same threshold worker.py logs against


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
