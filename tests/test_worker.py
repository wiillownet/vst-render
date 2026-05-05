"""Unit coverage for vst_render.worker primitives that don't require a
plugin (defensive guards). The full render path is exercised by the
parallel smoke test."""
from __future__ import annotations

import pytest

from vst_render.worker import _do_render


def test_do_render_raises_when_midi_path_set_without_duration():
    """R5: midi_path + midi_duration are populated together by every
    current caller. The guard pins that contract so a future caller that
    forgets midi_duration gets a clear ValueError, not a confusing
    `TypeError: unsupported operand ... NoneType + float` from a few
    lines down."""
    job = {
        "preset_path": "/anywhere",
        "midi_path": "/some/seq.mid",
        "midi_duration": None,
        "note": 48,
        "velocity": 127,
        "duration": 1.0,
        "tail": 1.0,
    }
    with pytest.raises(ValueError, match="midi_duration"):
        _do_render(job)
