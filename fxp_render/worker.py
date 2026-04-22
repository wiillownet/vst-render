"""
Loky worker harness. MUST be safe to import in a fresh worker process
without triggering any LLVM-adjacent library load ahead of dawdreamer —
so module-level imports are restricted to stdlib. Everything else is
deferred into init_worker and the render tasks.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("fxp_render")

# Module-level globals populated by init_worker, reused for every task.
_engine = None
_synth = None

SILENCE_EPS = 3.16e-5  # -90 dBFS, 16-bit quantization floor


def init_worker(plugin_path: str, sample_rate: int) -> None:
    """
    Called once per worker process by loky. Builds the DawDreamer engine
    and stores it in module globals so render tasks reuse it.

    Import order matters: dawdreamer MUST be the first non-stdlib import
    in the worker process. numpy/soundfile load after.
    """
    import dawdreamer as daw  # noqa: F401  MUST be first non-stdlib import
    import numpy  # noqa: F401  ensure load order before soundfile
    global _engine, _synth
    resolved = str(Path(plugin_path).resolve())
    _engine = daw.RenderEngine(sample_rate, 512)
    _synth = _engine.make_plugin_processor("plugin", resolved)
    _engine.load_graph([(_synth, [])])
    logger.debug("Worker initialized with plugin: %s", resolved)


def _do_render(job: dict):
    """Shared render path: load preset, set MIDI, render, return audio."""
    import numpy as np

    _synth.load_preset(job["preset_path"])

    if job["midi_path"] is not None:
        _synth.load_midi(
            job["midi_path"], clear_previous=True, beats=False, all_events=True
        )
        render_duration = job["midi_duration"] + job["tail"]
    else:
        _synth.clear_midi()
        _synth.add_midi_note(job["note"], job["velocity"], 0.0, job["duration"])
        render_duration = job["duration"] + job["tail"]

    _engine.render(render_duration)
    audio = _engine.get_audio()  # (2, N) float32

    if np.max(np.abs(audio)) < SILENCE_EPS:
        logger.warning("Silent output for preset: %s", job["preset_path"])

    return audio


def render_to_disk(job: dict) -> dict:
    """
    CLI/batch worker task: render and write to `job["output_path"]`.
    Returns a small status dict — not the audio — so IPC stays cheap.

    Required job keys: preset_path, output_path, note, velocity, duration,
    tail, midi_path, midi_duration, sample_rate, bit_depth, format,
    skip_existing. See the job-dict schema in CLAUDE.md.
    """
    preset_path = job["preset_path"]
    output_path = job["output_path"]

    try:
        if job["skip_existing"] and Path(output_path).exists():
            return {"status": "skipped", "path": preset_path}

        audio = _do_render(job)
        _write_audio(
            audio, output_path, job["sample_rate"], job["bit_depth"], job["format"]
        )
        return {"status": "ok", "path": preset_path}

    except Exception as exc:
        logger.warning("Failed to render %s: %s", preset_path, exc)
        return {"status": "error", "path": preset_path, "error": str(exc)}


def render_to_memory(job: dict) -> dict:
    """
    Library worker task: render and return audio to the main process. The
    full numpy array is IPC'd back — ~700 KB per 2-second stereo 44.1kHz
    render, so memory cost grows with batch size.
    """
    preset_path = job["preset_path"]
    try:
        audio = _do_render(job)
        return {"status": "ok", "path": preset_path, "audio": audio}
    except Exception as exc:
        logger.warning("Failed to render %s: %s", preset_path, exc)
        return {"status": "error", "path": preset_path, "error": str(exc)}


def _write_audio(audio, output_path: str, sample_rate: int, bit_depth: str, fmt: str) -> None:
    import numpy as np
    import soundfile as sf

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if fmt == "npy":
        np.save(output_path, audio)
        return

    subtype_map = {"16": "PCM_16", "24": "PCM_24", "32f": "FLOAT"}
    sf.write(output_path, audio.T, sample_rate, subtype=subtype_map[bit_depth])
