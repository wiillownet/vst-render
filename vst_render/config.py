from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class RenderConfig:
    # At least one of these must be set. `fxp_plugin_path` accepts either
    # the VST2 binary or the VST3 build of Serum 1 — DawDreamer's
    # `load_preset` works on both. `serum2_plugin_path` is Serum 2's VST3
    # binary and is paired with `synth.load_state` in the worker.
    fxp_plugin_path: str | Path | None = None
    serum2_plugin_path: str | Path | None = None
    sample_rate: int = 44100
    note: int = 48
    velocity: int = 127
    duration: float = 1.0
    tail: float = 1.0
    bit_depth: Literal["16", "24", "32f"] = "16"
    format: Literal["wav", "npy"] = "wav"
    midi_path: str | Path | None = None

    def __post_init__(self) -> None:
        if self.fxp_plugin_path is None and self.serum2_plugin_path is None:
            raise ValueError(
                "RenderConfig requires at least one of fxp_plugin_path or "
                "serum2_plugin_path to be set."
            )
        if self.fxp_plugin_path is not None:
            self.fxp_plugin_path = Path(self.fxp_plugin_path)
        if self.serum2_plugin_path is not None:
            self.serum2_plugin_path = Path(self.serum2_plugin_path)
        if self.midi_path is not None:
            self.midi_path = Path(self.midi_path)

        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if not (0 <= self.note <= 127):
            raise ValueError(f"note must be 0-127, got {self.note}")
        if not (1 <= self.velocity <= 127):
            raise ValueError(f"velocity must be 1-127, got {self.velocity}")
        if self.duration <= 0:
            raise ValueError(f"duration must be > 0, got {self.duration}")
        if self.tail < 0:
            raise ValueError(f"tail must be >= 0, got {self.tail}")
        if self.bit_depth not in ("16", "24", "32f"):
            raise ValueError(
                f"bit_depth must be '16', '24', or '32f', got {self.bit_depth!r}"
            )
        if self.format not in ("wav", "npy"):
            raise ValueError(f"format must be 'wav' or 'npy', got {self.format!r}")
