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

from .presets import PresetFormat, format_for_path

logger = logging.getLogger("vst_render")

# -90 dBFS peak; below the 16-bit quantization floor. Matches CLAUDE.md.
SILENCE_EPS = 3.16e-5


@dataclass
class Engine:
    """One DawDreamer engine with one or both synths loaded into a single graph.

    Mirrors the worker.py layout but lives in the main process — used by
    BatchRenderer and render_preset. Either synth may be None if the
    corresponding plugin path wasn't supplied; render_one's dispatch refuses
    formats whose synth isn't loaded.
    """
    engine: object
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
    """Build a RenderEngine + one or both PluginProcessors + audio graph.

    At least one of `fxp_plugin_path` / `serum2_plugin_path` must be set.

    Issues a 0.1s warmup render against each loaded synth before returning —
    Serum 2 lazy-loads sample data on first render, and the cold render
    comes out at ~10x steady-state level. Workers absorb this in init and
    so do we, so the caller's first render is correct.
    """
    if fxp_plugin_path is None and serum2_plugin_path is None:
        raise ValueError(
            "make_engine requires at least one of fxp_plugin_path or "
            "serum2_plugin_path"
        )

    engine = daw.RenderEngine(sample_rate, 512)
    synth_fxp = None
    synth_serum2 = None
    serum_state_path: Path | None = None

    processors: list = []
    if fxp_plugin_path is not None:
        synth_fxp = engine.make_plugin_processor(
            "fxp_synth", str(Path(fxp_plugin_path).resolve())
        )
        processors.append((synth_fxp, []))
    if serum2_plugin_path is not None:
        synth_serum2 = engine.make_plugin_processor(
            "serum2_synth", str(Path(serum2_plugin_path).resolve())
        )
        processors.append((synth_serum2, []))

    engine.load_graph(processors)

    if synth_serum2 is not None:
        # Per-engine tempfile dir. mkdtemp() is unique per call; we don't
        # rely on cleanup (process exit is best-effort).
        tmpdir = tempfile.mkdtemp(prefix="vst_render_serum2_")
        serum_state_path = Path(tmpdir) / "state.bin"

    for synth in (synth_fxp, synth_serum2):
        if synth is None:
            continue
        synth.clear_midi()
        synth.add_midi_note(48, 127, 0.0, 0.05)
        engine.render(0.1)

    return Engine(
        engine=engine,
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
        if engine.synth_fxp is None:
            raise RuntimeError(
                f"Got an .fxp preset ({preset_path.name}) but the engine has "
                "no fxp synth — RenderConfig.fxp_plugin_path was not set"
            )
        synth = engine.synth_fxp
        synth.load_preset(str(preset_path.resolve()))
    elif fmt == PresetFormat.SERUM2:
        if engine.synth_serum2 is None:
            raise RuntimeError(
                f"Got a .SerumPreset ({preset_path.name}) but the engine has "
                "no serum2 synth — RenderConfig.serum2_plugin_path was not set"
            )
        synth = engine.synth_serum2
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

    engine.engine.render(render_duration)
    audio = engine.engine.get_audio()

    if np.max(np.abs(audio)) < SILENCE_EPS:
        logger.warning("Silent output for preset: %s", preset_path)

    return audio
