"""
In-process rendering primitives used by BatchRenderer and render_preset.

Not used by the loky worker — worker.py is self-contained to keep its
module-level import graph free of anything that could load LLVM-adjacent
libs ahead of dawdreamer. This module is safe to use module-level
imports because it only runs in the main process.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import dawdreamer as daw
import numpy as np
from serum2_preset_loader import convert_preset_file

from .config import SILENCE_EPS
from .presets import PresetFormat, format_for_path

logger = logging.getLogger("vst_render")


@dataclass
class Engine:
    """One DawDreamer RenderEngine PER LOADED FORMAT.

    Earlier versions of this code put both synths into a single engine's
    graph as orphan source nodes. That doesn't work: `engine.load_graph`
    with multiple source processors and no terminal mixer routes only the
    last processor in the list to `get_audio()` — every other synth's
    output is silently discarded. The previous "idle synth = byte-
    identical silence" probe was a tautology: yes, the idle synth was
    silent at the output, but so would the active synth have been if it
    weren't last. See `docs/audit-log.md` 2026-05-20 for the full repro.

    Holding two engines (each with a single synth) is the simplest fix:
    each render call operates on exactly one engine, so output routing is
    unambiguous and the synths can't contaminate each other through
    persistent internal state (LFOs, release tails, modulators) summing
    into the wrong job's audio.
    """
    engine_fxp: object | None
    engine_serum2: object | None
    synth_fxp: object | None
    synth_serum2: object | None
    # Per-engine tempfile for the serum2 state.bin round-trip. Reused for
    # every serum2 render — write_bytes overwrites in place.
    serum_state_path: Path | None


def make_engine(
    fxp_plugin_path: str | Path | None,
    serum2_plugin_path: str | Path | None,
    sample_rate: int,
) -> Engine:
    """Build one RenderEngine per loaded format.

    At least one of `fxp_plugin_path` / `serum2_plugin_path` must be set.

    Each loaded synth gets a 0.1s warmup render on its own engine before
    returning — Serum 2 lazy-loads sample data on first render and the
    cold render comes out at ~10x steady-state level. Workers absorb this
    in init and so do we, so the caller's first render is correct.
    """
    if fxp_plugin_path is None and serum2_plugin_path is None:
        raise ValueError(
            "make_engine requires at least one of fxp_plugin_path or "
            "serum2_plugin_path"
        )

    engine_fxp = None
    engine_serum2 = None
    synth_fxp = None
    synth_serum2 = None
    serum_state_path: Path | None = None

    if fxp_plugin_path is not None:
        engine_fxp = daw.RenderEngine(sample_rate, 512)
        synth_fxp = engine_fxp.make_plugin_processor(
            "fxp_synth", str(Path(fxp_plugin_path).resolve())
        )
        engine_fxp.load_graph([(synth_fxp, [])])
        synth_fxp.clear_midi()
        synth_fxp.add_midi_note(48, 127, 0.0, 0.05)
        engine_fxp.render(0.1)

    if serum2_plugin_path is not None:
        engine_serum2 = daw.RenderEngine(sample_rate, 512)
        synth_serum2 = engine_serum2.make_plugin_processor(
            "serum2_synth", str(Path(serum2_plugin_path).resolve())
        )
        engine_serum2.load_graph([(synth_serum2, [])])
        # Per-engine tempfile dir. mkdtemp() is unique per call; we don't
        # rely on cleanup (process exit is best-effort).
        tmpdir = tempfile.mkdtemp(prefix="vst_render_serum2_")
        serum_state_path = Path(tmpdir) / "state.bin"
        synth_serum2.clear_midi()
        synth_serum2.add_midi_note(48, 127, 0.0, 0.05)
        engine_serum2.render(0.1)

    return Engine(
        engine_fxp=engine_fxp,
        engine_serum2=engine_serum2,
        synth_fxp=synth_fxp,
        synth_serum2=synth_serum2,
        serum_state_path=serum_state_path,
    )


def render_one(
    engine: Engine,
    preset_path: str | Path,
    *,
    note: int = 48,
    velocity: int = 127,
    duration: float = 1.0,
    tail: float = 1.0,
    midi_path: str | Path | None = None,
    midi_duration: float | None = None,
) -> np.ndarray:
    """
    Render one preset. Auto-dispatches on the preset's file suffix:
    `.fxp` -> `synth.load_preset`, `.SerumPreset` -> convert + `load_state`.
    Returns float32 audio of shape (channels, samples). `midi_duration` is
    required when `midi_path` is given — compute it once in the caller via
    `utils.get_midi_duration` and pass through; workers never compute it.
    """
    preset_path = Path(preset_path)
    fmt = format_for_path(preset_path)

    if fmt == PresetFormat.FXP:
        synth = engine.synth_fxp
        active_engine = engine.engine_fxp
        synth.load_preset(str(preset_path.resolve()))
    elif fmt == PresetFormat.SERUM2:
        synth = engine.synth_serum2
        active_engine = engine.engine_serum2
        engine.serum_state_path.write_bytes(convert_preset_file(str(preset_path)))
        synth.load_state(str(engine.serum_state_path))
    else:
        # format_for_path already raised on unknown suffixes; this is a
        # defensive arm for the case where a new PresetFormat enum value
        # is added but render_one wasn't taught about it.
        raise ValueError(f"Unhandled preset format: {fmt!r}")

    if midi_path is not None:
        if midi_duration is None:
            raise ValueError("midi_duration is required when midi_path is set")
        synth.load_midi(
            str(Path(midi_path).resolve()),
            clear_previous=True,
            beats=False,
            all_events=True,
        )
        render_duration = midi_duration + tail
    else:
        synth.clear_midi()
        synth.add_midi_note(note, velocity, 0.0, duration)
        render_duration = duration + tail

    active_engine.render(render_duration)
    audio = active_engine.get_audio()

    if np.max(np.abs(audio)) < SILENCE_EPS:
        logger.warning("Silent output for preset: %s", preset_path)

    return audio
