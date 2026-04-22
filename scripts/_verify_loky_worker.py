"""
Loky worker helpers for verify_dawdreamer.py. Lives in its own importable
module so that cloudpickle resolves worker functions by module reference
— `global` assignments inside init_worker must land in the same module
namespace that the render task reads from. That does not hold when
everything lives in `__main__`.
"""
from __future__ import annotations

import os
from pathlib import Path

_engine = None
_synth = None


def init_worker(plugin_path: str, sample_rate: int) -> None:
    """Mirror the production init_worker pattern."""
    import dawdreamer as daw  # must be first non-stdlib import
    import numpy as np  # noqa: F401  (ensure post-dawdreamer load order)
    global _engine, _synth
    _engine = daw.RenderEngine(sample_rate, 512)
    _synth = _engine.make_plugin_processor("serum", str(Path(plugin_path).resolve()))
    _engine.load_graph([(_synth, [])])


def render_task(preset_path: str) -> dict:
    import numpy as np
    if _synth is None:
        raise RuntimeError("worker _synth is None — init_worker did not run or "
                           "did not populate module globals")
    _synth.load_preset(preset_path)
    _synth.clear_midi()
    _synth.add_midi_note(48, 127, 0.0, 1.0)
    _engine.render(2.0)
    audio = _engine.get_audio()
    return {"status": "ok", "peak": float(np.max(np.abs(audio))), "pid": os.getpid()}


def kill_task() -> None:
    """Simulate a wedged worker — abrupt exit, no cleanup."""
    os._exit(42)
