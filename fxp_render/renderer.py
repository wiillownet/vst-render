"""
In-process rendering primitives used by BatchRenderer and render_preset.

Not used by the loky worker — worker.py is self-contained to keep its
module-level import graph free of anything that could load LLVM-adjacent
libs ahead of dawdreamer. This module is safe to use module-level
imports because it only runs in the main process.
"""
from __future__ import annotations

import logging
from pathlib import Path

import dawdreamer as daw
import numpy as np

logger = logging.getLogger("fxp_render")

# -90 dBFS peak; below the 16-bit quantization floor. Matches CLAUDE.md.
SILENCE_EPS = 3.16e-5


def make_engine(plugin_path: str | Path, sample_rate: int):
    """Build a RenderEngine + PluginProcessor + graph. Returns (engine, synth)."""
    resolved = str(Path(plugin_path).resolve())
    engine = daw.RenderEngine(sample_rate, 512)
    synth = engine.make_plugin_processor("plugin", resolved)
    engine.load_graph([(synth, [])])
    return engine, synth


def render_one(
    engine,
    synth,
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
    Render one preset on the provided engine+synth. Returns float32 audio
    of shape (channels, samples). `midi_duration` is required when
    `midi_path` is given — compute it once in the caller with
    `utils.get_midi_duration` and pass through; workers never compute it.
    """
    synth.load_preset(str(Path(preset_path).resolve()))

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

    engine.render(render_duration)
    audio = engine.get_audio()

    if np.max(np.abs(audio)) < SILENCE_EPS:
        logger.warning("Silent output for preset: %s", preset_path)

    return audio
