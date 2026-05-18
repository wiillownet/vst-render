from pathlib import Path

import pytest

from vst_render.config import RenderConfig


def test_defaults_resolve():
    cfg = RenderConfig(fxp_plugin_path="C:/fake/Serum.dll")
    assert cfg.sample_rate == 44100
    assert cfg.note == 48
    assert cfg.velocity == 127
    assert cfg.duration == 1.0
    assert cfg.tail == 1.0
    assert cfg.midi_path is None
    assert cfg.serum2_plugin_path is None


def test_fxp_plugin_path_coerced_to_path():
    cfg = RenderConfig(fxp_plugin_path="C:/fake/Serum.dll")
    assert isinstance(cfg.fxp_plugin_path, Path)


def test_serum2_plugin_path_coerced_to_path():
    cfg = RenderConfig(serum2_plugin_path="C:/fake/Serum2.vst3")
    assert isinstance(cfg.serum2_plugin_path, Path)
    assert cfg.fxp_plugin_path is None


def test_both_plugin_paths_set_is_allowed():
    cfg = RenderConfig(
        fxp_plugin_path="C:/fake/Serum.dll",
        serum2_plugin_path="C:/fake/Serum2.vst3",
    )
    assert isinstance(cfg.fxp_plugin_path, Path)
    assert isinstance(cfg.serum2_plugin_path, Path)


def test_no_plugin_path_rejected():
    with pytest.raises(ValueError, match="at least one"):
        RenderConfig()


def test_midi_path_coerced_to_path_when_set():
    cfg = RenderConfig(fxp_plugin_path="p", midi_path="seq.mid")
    assert isinstance(cfg.midi_path, Path)


def test_midi_path_stays_none_when_not_set():
    cfg = RenderConfig(fxp_plugin_path="p")
    assert cfg.midi_path is None


@pytest.mark.parametrize("sr", [0, -1, -44100])
def test_sample_rate_must_be_positive(sr):
    with pytest.raises(ValueError, match="sample_rate"):
        RenderConfig(fxp_plugin_path="p", sample_rate=sr)


@pytest.mark.parametrize("note", [-1, 128, 999])
def test_note_range_enforced(note):
    with pytest.raises(ValueError, match="note"):
        RenderConfig(fxp_plugin_path="p", note=note)


def test_note_at_boundaries_allowed():
    RenderConfig(fxp_plugin_path="p", note=0)
    RenderConfig(fxp_plugin_path="p", note=127)


@pytest.mark.parametrize("vel", [0, -1, 128])
def test_velocity_range_enforced(vel):
    with pytest.raises(ValueError, match="velocity"):
        RenderConfig(fxp_plugin_path="p", velocity=vel)


def test_velocity_boundaries():
    RenderConfig(fxp_plugin_path="p", velocity=1)
    RenderConfig(fxp_plugin_path="p", velocity=127)


@pytest.mark.parametrize("dur", [0, -0.1, -1.0])
def test_duration_must_be_positive(dur):
    with pytest.raises(ValueError, match="duration"):
        RenderConfig(fxp_plugin_path="p", duration=dur)


@pytest.mark.parametrize("tail", [-0.1, -1.0])
def test_tail_must_be_non_negative(tail):
    with pytest.raises(ValueError, match="tail"):
        RenderConfig(fxp_plugin_path="p", tail=tail)


def test_tail_zero_accepted():
    # tail=0 is legitimate for percussive one-shots that don't need a
    # release-envelope tail.
    RenderConfig(fxp_plugin_path="p", tail=0)
