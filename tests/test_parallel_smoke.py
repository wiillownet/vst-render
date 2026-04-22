"""End-to-end smoke: render 2 presets in parallel via ParallelBatchRenderer."""
from __future__ import annotations

import numpy as np
import pytest

from fxp_render import ParallelBatchRenderer, RenderConfig


@pytest.mark.slow
def test_parallel_render_produces_audio(plugin_path, preset_files):
    config = RenderConfig(
        plugin_path=plugin_path,
        sample_rate=44100,
        note=48,
        velocity=127,
        duration=1.0,
        tail=1.0,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(preset_files)

    assert len(results) == len(preset_files), "expected one audio array per preset"
    for fxp_path, audio in results.items():
        assert audio.shape[0] == 2, f"{fxp_path}: expected stereo"
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5, f"{fxp_path}: audio is silent"
