"""
Public library API: BatchRenderer, ParallelBatchRenderer, render_preset.

Renderer entry validates paths (CLAUDE.md: __post_init__ does cheap
range checks only; file existence is verified on first use here).
"""
from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Iterator

import numpy as np

from .batch import iter_batch_to_memory
from .config import RenderConfig
from .presets import PresetFormat
from .renderer import make_engine, render_one
from .utils import get_midi_duration


def _validate_paths(config: RenderConfig) -> float | None:
    """Plugin/MIDI existence check at renderer entry. Returns MIDI duration or None.

    Validates whichever plugin paths are set on the config. The in-process
    and parallel renderers below still only drive the .fxp synth; Step D
    of the Serum 2 expansion threads `serum2_plugin_path` through the
    worker pool. Until then, configs without `fxp_plugin_path` are
    rejected at first use rather than producing a confusing
    AttributeError downstream.
    """
    if config.fxp_plugin_path is not None and not Path(config.fxp_plugin_path).exists():
        raise FileNotFoundError(f"Plugin not found: {config.fxp_plugin_path}")
    if config.serum2_plugin_path is not None and not Path(config.serum2_plugin_path).exists():
        raise FileNotFoundError(f"Plugin not found: {config.serum2_plugin_path}")
    if config.midi_path is not None:
        midi_path = Path(config.midi_path)
        if not midi_path.exists():
            raise FileNotFoundError(f"MIDI file not found: {midi_path}")
        return get_midi_duration(midi_path)
    return None


def _require_fxp_plugin(config: RenderConfig) -> Path:
    """Until Step D wires up dual-synth workers, the in-process and parallel
    renderers can only drive the .fxp synth. Reject configs that only set
    the serum2 path with a clear error rather than a None deref deeper in."""
    if config.fxp_plugin_path is None:
        raise NotImplementedError(
            "BatchRenderer / ParallelBatchRenderer currently require "
            "fxp_plugin_path. Serum 2 (.SerumPreset) rendering through "
            "these classes lands in a follow-up; use the CLI in the meantime."
        )
    return Path(config.fxp_plugin_path)


class BatchRenderer:
    """
    Single-process, sequential renderer. Loads the plugin once in
    `__enter__` and reuses it for every `render()` call.
    """

    def __init__(self, config: RenderConfig):
        self.config = config
        self._frozen_config: RenderConfig | None = None
        self._engine = None
        self._synth = None
        self._midi_duration: float | None = None

    def __enter__(self) -> "BatchRenderer":
        # Freeze a copy of the config at entry so subsequent mutations to
        # `self.config` can't desync `_midi_duration` from the (possibly new)
        # `midi_path`. Cheap insurance against a silent failure mode.
        self._frozen_config = dataclasses.replace(self.config)
        self._midi_duration = _validate_paths(self._frozen_config)
        fxp_plugin_path = _require_fxp_plugin(self._frozen_config)
        self._engine, self._synth = make_engine(
            fxp_plugin_path, self._frozen_config.sample_rate
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # DawDreamer has no explicit teardown — drop refs for GC.
        self._engine = None
        self._synth = None
        self._frozen_config = None

    def render(self, fxp_path: str | Path) -> np.ndarray:
        # CLAUDE.md forbids threading with DawDreamer — it hangs after the
        # first cross-thread call. Surface the foot-gun loudly here rather
        # than letting an embedder lose hours to a silent hang.
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "BatchRenderer.render() must be called from the main thread. "
                "DawDreamer hangs when used from threads (see CLAUDE.md). "
                "Use ParallelBatchRenderer if you need concurrent rendering."
            )
        if self._engine is None:
            raise RuntimeError("BatchRenderer must be used as a context manager")
        cfg = self._frozen_config
        return render_one(
            self._engine,
            self._synth,
            fxp_path,
            note=cfg.note,
            velocity=cfg.velocity,
            duration=cfg.duration,
            tail=cfg.tail,
            midi_path=cfg.midi_path,
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
        self._frozen_config: RenderConfig | None = None
        self._midi_duration: float | None = None

    def __enter__(self) -> "ParallelBatchRenderer":
        # Freeze a copy of the config at entry. _midi_duration is computed
        # against this snapshot; jobs read midi_path / sample_rate / etc.
        # from the same snapshot so post-enter mutations to self.config
        # can't desync the two.
        self._frozen_config = dataclasses.replace(self.config)
        self._midi_duration = _validate_paths(self._frozen_config)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Executor is owned by loky's reusable cache; leave it warm so the
        # next ParallelBatchRenderer in this process reuses the workers.
        self._frozen_config = None

    def _build_jobs(self, preset_paths: list[str | Path]) -> list[dict]:
        cfg = self._frozen_config
        if cfg is None:
            raise RuntimeError(
                "ParallelBatchRenderer must be used as a context manager"
            )
        return [
            {
                "preset_path": str(Path(p).resolve()),
                # ParallelBatchRenderer is gated to fxp_plugin_path until
                # Step D adds dual-synth init; every job built here is
                # therefore an .fxp by construction. When Serum 2 lands in
                # the library API the caller will supply the tag.
                "preset_format": PresetFormat.FXP.value,
                "note": cfg.note,
                "velocity": cfg.velocity,
                "duration": cfg.duration,
                "tail": cfg.tail,
                "midi_path": str(cfg.midi_path) if cfg.midi_path else None,
                "midi_duration": self._midi_duration,
                "sample_rate": cfg.sample_rate,
            }
            for p in preset_paths
        ]

    def iter_batch(self, preset_paths: list[str | Path]) -> Iterator[tuple[str, np.ndarray]]:
        """Yield `(fxp_path, audio)` as each job completes (unordered)."""
        jobs = self._build_jobs(preset_paths)
        cfg = self._frozen_config
        fxp_plugin_path = _require_fxp_plugin(cfg)
        for result in iter_batch_to_memory(
            jobs, self.workers, str(fxp_plugin_path), cfg.sample_rate
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
    fxp_plugin_path = _require_fxp_plugin(config)
    engine, synth = make_engine(fxp_plugin_path, config.sample_rate)
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
