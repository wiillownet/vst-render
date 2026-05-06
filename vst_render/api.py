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
from .presets import PresetFormat, format_for_path
from .renderer import make_engine, render_one
from .utils import get_midi_duration


def _validate_paths(config: RenderConfig) -> float | None:
    """Plugin/MIDI existence check at renderer entry. Returns MIDI duration or None.

    Validates whichever plugin paths are set on the config. The dual-synth
    engine tolerates either path being None; the gate that matters at this
    layer is "if you set it, it must exist."
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


def _check_required_plugins(
    config: RenderConfig, formats: set[PresetFormat]
) -> None:
    """Reject configs that omit a plugin path for a format the caller is
    actually trying to render. Mirrors the CLI's start-up check so library
    users get the same clear error before any worker boots."""
    flag_for: dict[PresetFormat, str] = {
        PresetFormat.FXP: "fxp_plugin_path",
        PresetFormat.SERUM2: "serum2_plugin_path",
    }
    ext_for: dict[PresetFormat, str] = {
        PresetFormat.FXP: ".fxp",
        PresetFormat.SERUM2: ".SerumPreset",
    }
    have = set()
    if config.fxp_plugin_path is not None:
        have.add(PresetFormat.FXP)
    if config.serum2_plugin_path is not None:
        have.add(PresetFormat.SERUM2)
    missing = formats - have
    if missing:
        msgs = sorted(
            f"{ext_for[m]} preset(s) supplied but RenderConfig.{flag_for[m]} is unset"
            for m in missing
        )
        raise ValueError("; ".join(msgs))


class BatchRenderer:
    """
    Single-process, sequential renderer. Loads one or both plugins once in
    `__enter__` and reuses them for every `render()` call. The format of
    each preset is auto-detected from its file suffix.
    """

    def __init__(self, config: RenderConfig):
        self.config = config
        self._frozen_config: RenderConfig | None = None
        self._engine = None
        self._midi_duration: float | None = None

    def __enter__(self) -> "BatchRenderer":
        # Freeze a copy of the config at entry so subsequent mutations to
        # `self.config` can't desync `_midi_duration` from the (possibly new)
        # `midi_path`. Cheap insurance against a silent failure mode.
        self._frozen_config = dataclasses.replace(self.config)
        self._midi_duration = _validate_paths(self._frozen_config)
        self._engine = make_engine(
            self._frozen_config.fxp_plugin_path,
            self._frozen_config.serum2_plugin_path,
            self._frozen_config.sample_rate,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # DawDreamer has no explicit teardown — drop refs for GC.
        self._engine = None
        self._frozen_config = None

    def render(self, preset_path: str | Path) -> np.ndarray:
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
        # render_one auto-detects format and refuses if the matching synth
        # isn't loaded — but we can give a better error before that fires by
        # checking the config now (the engine has already been built in
        # __enter__, so the per-format synth presence mirrors the config).
        fmt = format_for_path(Path(preset_path))
        _check_required_plugins(cfg, {fmt})
        return render_one(
            self._engine,
            preset_path,
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

    Mixed-format batches are supported: pass `.fxp` and `.SerumPreset`
    paths in the same call as long as `RenderConfig` provides the
    matching plugin paths. Format is auto-detected per preset.
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
                "preset_format": format_for_path(Path(p)).value,
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
        """Yield `(preset_path, audio)` as each job completes (unordered)."""
        jobs = self._build_jobs(preset_paths)
        cfg = self._frozen_config
        # Validate now, before booting workers: every format actually
        # appearing in this batch must have its plugin path set.
        formats_in_batch = {
            PresetFormat(j["preset_format"]) for j in jobs
        }
        _check_required_plugins(cfg, formats_in_batch)

        # Pass both plugin paths to the worker pool; the worker conditionally
        # boots whichever synth is non-None. Idle synth in graph is silent
        # (probe 1, byte-identical), so the cost of booting an unused synth
        # is the user's choice via RenderConfig.
        fxp_str = (
            str(cfg.fxp_plugin_path) if cfg.fxp_plugin_path is not None else None
        )
        serum2_str = (
            str(cfg.serum2_plugin_path) if cfg.serum2_plugin_path is not None else None
        )
        for result in iter_batch_to_memory(
            jobs,
            self.workers,
            fxp_str,
            serum2_str,
            cfg.sample_rate,
        ):
            if result["status"] == "ok":
                yield result["path"], result["audio"]
            # errors are logged by the worker/batch layer; skip silently here
            # so `iter_batch` can be consumed without try/except clutter

    def render_batch(self, preset_paths: list[str | Path]) -> dict[str, np.ndarray]:
        """Render all presets and return a dict mapping path -> audio array."""
        return dict(self.iter_batch(preset_paths))


def render_preset(preset_path: str | Path, config: RenderConfig) -> np.ndarray:
    """
    One-off render. Spins up a fresh engine, renders, returns audio.
    Not suitable for batch use — each call pays the ~1-2s plugin cold-start
    plus a 0.1s warmup render per loaded synth.

    Format is auto-detected from the preset's file suffix; the matching
    plugin path must be set on `config`.
    """
    midi_duration = _validate_paths(config)
    fmt = format_for_path(Path(preset_path))
    _check_required_plugins(config, {fmt})
    engine = make_engine(
        config.fxp_plugin_path, config.serum2_plugin_path, config.sample_rate
    )
    return render_one(
        engine,
        preset_path,
        note=config.note,
        velocity=config.velocity,
        duration=config.duration,
        tail=config.tail,
        midi_path=config.midi_path,
        midi_duration=midi_duration,
    )
