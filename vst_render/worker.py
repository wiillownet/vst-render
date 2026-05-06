"""
Loky worker harness. MUST be safe to import in a fresh worker process
without triggering any LLVM-adjacent library load ahead of dawdreamer —
so module-level imports are restricted to stdlib. Everything else is
deferred into init_worker and the render tasks.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger("vst_render")

# Module-level globals populated by init_worker, reused for every task.
# Either synth may be None depending on which plugin paths were supplied —
# _do_render dispatches on job["preset_format"] and refuses if the matching
# synth isn't loaded.
_engine = None
_synth_fxp = None
_synth_serum2 = None
# Per-worker tempfile path used to round-trip Serum 2 state from
# `serum2_preset_loader.convert_preset_file` (returns bytes) into
# `synth.load_state` (takes a path). Reused for every serum2 job.
_serum_state_path: Path | None = None

SILENCE_EPS = 3.16e-5  # -90 dBFS, 16-bit quantization floor


def init_worker(
    fxp_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
) -> None:
    """
    Called once per worker process by loky. Builds one DawDreamer engine
    with one or both synths loaded into a single graph (probe 1: an idle
    synth in a shared graph is byte-identical silence relative to the
    single-synth render).

    At least one of `fxp_plugin_path` or `serum2_plugin_path` must be set.

    Import order matters: dawdreamer MUST be the first non-stdlib import
    in the worker process. numpy/soundfile load after. The pure-Python
    serum2_preset_loader is also deferred to keep this module's
    stdlib-imports-only contract.

    Each loaded synth gets a warmup render before init returns — Serum 2
    lazy-loads sample data on first render, and the cold render produces
    audio at ~10x the steady-state level. Workers must absorb that
    anomaly here so the user's first job in the batch isn't off-level
    (probe 1 finding).
    """
    # Validate args before loading dawdreamer — both to keep the bad-args
    # path cheap and so a unit test can exercise this guard without paying
    # the import cost (which would also violate the import-order constraint
    # in test processes that have already loaded numpy at module level).
    if fxp_plugin_path is None and serum2_plugin_path is None:
        raise ValueError(
            "init_worker requires at least one of fxp_plugin_path or "
            "serum2_plugin_path"
        )

    import dawdreamer as daw  # MUST be first non-stdlib import
    import numpy  # noqa: F401  ensure load order before soundfile
    # Pure-Python (cbor2 + zstandard); LLVM order isn't load-bearing,
    # but the worker module's contract is "nothing third-party at
    # module level". Defer.
    from serum2_preset_loader import convert_preset_file  # noqa: F401

    global _engine, _synth_fxp, _synth_serum2, _serum_state_path

    _engine = daw.RenderEngine(sample_rate, 512)

    processors: list = []
    if fxp_plugin_path is not None:
        resolved = str(Path(fxp_plugin_path).resolve())
        _synth_fxp = _engine.make_plugin_processor("fxp_synth", resolved)
        processors.append((_synth_fxp, []))
    if serum2_plugin_path is not None:
        resolved = str(Path(serum2_plugin_path).resolve())
        _synth_serum2 = _engine.make_plugin_processor("serum2_synth", resolved)
        processors.append((_synth_serum2, []))

    _engine.load_graph(processors)

    # Per-worker tempfile dir. mkdtemp() is unique per call; loky workers
    # exiting cleanly is best-effort, so we don't rely on cleanup. Each
    # serum2 job overwrites state.bin via write_bytes.
    tmpdir = tempfile.mkdtemp(prefix="vst_render_serum2_")
    _serum_state_path = Path(tmpdir) / "state.bin"

    # Warmup renders. Short 0.1s renders are enough to pull Serum 2's
    # samples off disk; the engine renders both synths simultaneously,
    # so by the time both warmup passes complete each synth has been
    # asked to render at least once.
    for synth in (_synth_fxp, _synth_serum2):
        if synth is None:
            continue
        synth.clear_midi()
        synth.add_midi_note(48, 127, 0.0, 0.05)
        _engine.render(0.1)

    logger.debug(
        "Worker initialized (fxp=%s, serum2=%s)",
        fxp_plugin_path is not None,
        serum2_plugin_path is not None,
    )


def _do_render(job: dict):
    """Shared render path: dispatch to the right synth, load preset state,
    set MIDI, render, return audio."""
    import numpy as np

    # Defensive: midi_path + midi_duration are populated together by every
    # current caller (cli.py, api.ParallelBatchRenderer). Pinning the
    # contract here means a future caller that forgets midi_duration gets
    # a clear ValueError rather than `None + float -> TypeError` from a
    # confusing arithmetic line below.
    if job["midi_path"] is not None and job["midi_duration"] is None:
        raise ValueError(
            "midi_duration must be populated when midi_path is set; "
            "see the job-dict schema in CLAUDE.md"
        )

    fmt = job["preset_format"]
    if fmt == "fxp":
        if _synth_fxp is None:
            raise RuntimeError(
                "Got an .fxp job but the worker has no fxp synth — "
                "init_worker was called without fxp_plugin_path"
            )
        synth = _synth_fxp
        synth.load_preset(job["preset_path"])
    elif fmt == "serum2":
        if _synth_serum2 is None:
            raise RuntimeError(
                "Got a .SerumPreset job but the worker has no serum2 synth — "
                "init_worker was called without serum2_plugin_path"
            )
        from serum2_preset_loader import convert_preset_file
        synth = _synth_serum2
        state_blob = convert_preset_file(job["preset_path"])
        # write_bytes (not append) so blob-size variance across presets
        # doesn't leave stale tail bytes from a larger prior blob.
        _serum_state_path.write_bytes(state_blob)
        synth.load_state(str(_serum_state_path))
    else:
        raise ValueError(f"Unknown preset_format: {fmt!r}")

    if job["midi_path"] is not None:
        synth.load_midi(
            job["midi_path"], clear_previous=True, beats=False, all_events=True
        )
        render_duration = job["midi_duration"] + job["tail"]
    else:
        synth.clear_midi()
        synth.add_midi_note(job["note"], job["velocity"], 0.0, job["duration"])
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

    Required job keys: preset_path, preset_format, output_path, note,
    velocity, duration, tail, midi_path, midi_duration, sample_rate,
    bit_depth, format, skip_existing. See the job-dict schema in CLAUDE.md.
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
