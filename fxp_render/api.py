"""
Public library API: BatchRenderer, ParallelBatchRenderer, render_preset.

Renderer entry validates paths (CLAUDE.md: __post_init__ does cheap
range checks only; file existence is verified on first use here).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from .batch import iter_batch_to_memory
from .config import RenderConfig
from .renderer import make_engine, render_one
from .utils import get_midi_duration


def _validate_paths(config: RenderConfig) -> float | None:
    """Plugin/MIDI existence check at renderer entry. Returns MIDI duration or None."""
    if not Path(config.plugin_path).exists():
        raise FileNotFoundError(f"Plugin not found: {config.plugin_path}")
    if config.midi_path is not None:
        midi_path = Path(config.midi_path)
        if not midi_path.exists():
            raise FileNotFoundError(f"MIDI file not found: {midi_path}")
        return get_midi_duration(midi_path)
    return None


class BatchRenderer:
    """
    Single-process, sequential renderer. Loads the plugin once in
    `__enter__` and reuses it for every `render()` call.
    """

    def __init__(self, config: RenderConfig):
        self.config = config
        self._engine = None
        self._synth = None
        self._midi_duration: float | None = None

    def __enter__(self) -> "BatchRenderer":
        self._midi_duration = _validate_paths(self.config)
        self._engine, self._synth = make_engine(
            self.config.plugin_path, self.config.sample_rate
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # DawDreamer has no explicit teardown — drop refs for GC.
        self._engine = None
        self._synth = None

    def render(self, fxp_path: str | Path) -> np.ndarray:
        if self._engine is None:
            raise RuntimeError("BatchRenderer must be used as a context manager")
        return render_one(
            self._engine,
            self._synth,
            fxp_path,
            note=self.config.note,
            velocity=self.config.velocity,
            duration=self.config.duration,
            tail=self.config.tail,
            midi_path=self.config.midi_path,
            midi_duration=self._midi_duration,
        )


class ParallelBatchRenderer:
    """
    Multi-process renderer for bulk use cases (e.g. pre-rendering a whole
    preset library for an on-demand browser). Audio is shipped back from
    workers to the main process — callers holding the entire result dict
    in memory will scale to tens or low hundreds of MBs before it hurts.
    For larger libraries, iterate and spill to disk.
    """

    def __init__(self, config: RenderConfig, workers: int = -1):
        self.config = config
        self.workers = workers
        self._midi_duration: float | None = None

    def __enter__(self) -> "ParallelBatchRenderer":
        self._midi_duration = _validate_paths(self.config)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Executor is owned by loky's reusable cache; leave it warm so the
        # next ParallelBatchRenderer in this process reuses the workers.
        pass

    def _build_jobs(self, preset_paths: list[str | Path]) -> list[dict]:
        return [
            {
                "preset_path": str(Path(p).resolve()),
                "note": self.config.note,
                "velocity": self.config.velocity,
                "duration": self.config.duration,
                "tail": self.config.tail,
                "midi_path": str(self.config.midi_path) if self.config.midi_path else None,
                "midi_duration": self._midi_duration,
                "sample_rate": self.config.sample_rate,
            }
            for p in preset_paths
        ]

    def iter_batch(self, preset_paths: list[str | Path]) -> Iterator[tuple[str, np.ndarray]]:
        """Yield `(fxp_path, audio)` as each job completes (unordered)."""
        jobs = self._build_jobs(preset_paths)
        for result in iter_batch_to_memory(
            jobs, self.workers, str(self.config.plugin_path), self.config.sample_rate
        ):
            if result["status"] == "ok":
                yield result["path"], result["audio"]
            # errors are logged by the worker/batch layer; skip silently here
            # so `iter_batch` can be consumed without try/except clutter

    def render_batch(self, preset_paths: list[str | Path]) -> dict[str, np.ndarray]:
        """Render all presets and return a dict mapping path -> audio array."""
        return dict(self.iter_batch(preset_paths))


def render_preset(fxp_path: str | Path, config: RenderConfig) -> np.ndarray:
    """
    One-off render. Spins up a fresh engine, renders, returns audio.
    Not suitable for batch use — each call pays the ~1-2s plugin cold-start.
    """
    _validate_paths_result = _validate_paths(config)
    engine, synth = make_engine(config.plugin_path, config.sample_rate)
    return render_one(
        engine,
        synth,
        fxp_path,
        note=config.note,
        velocity=config.velocity,
        duration=config.duration,
        tail=config.tail,
        midi_path=config.midi_path,
        midi_duration=_validate_paths_result,
    )
