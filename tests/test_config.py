from pathlib import Path

import pytest

from fxp_render.config import RenderConfig


def test_defaults_resolve():
    cfg = RenderConfig(plugin_path="C:/fake/Serum.dll")
    assert cfg.sample_rate == 44100
    assert cfg.note == 48
    assert cfg.velocity == 127
    assert cfg.duration == 1.0
    assert cfg.tail == 1.0
    assert cfg.bit_depth == "16"
    assert cfg.format == "wav"
    assert cfg.midi_path is None


def test_plugin_path_coerced_to_path():
    cfg = RenderConfig(plugin_path="C:/fake/Serum.dll")
    assert isinstance(cfg.plugin_path, Path)


def test_midi_path_coerced_to_path_when_set():
    cfg = RenderConfig(plugin_path="p", midi_path="seq.mid")
    assert isinstance(cfg.midi_path, Path)


def test_midi_path_stays_none_when_not_set():
    cfg = RenderConfig(plugin_path="p")
    assert cfg.midi_path is None


@pytest.mark.parametrize("sr", [0, -1, -44100])
def test_sample_rate_must_be_positive(sr):
    with pytest.raises(ValueError, match="sample_rate"):
        RenderConfig(plugin_path="p", sample_rate=sr)


@pytest.mark.parametrize("note", [-1, 128, 999])
def test_note_range_enforced(note):
    with pytest.raises(ValueError, match="note"):
        RenderConfig(plugin_path="p", note=note)


def test_note_at_boundaries_allowed():
    RenderConfig(plugin_path="p", note=0)
    RenderConfig(plugin_path="p", note=127)


@pytest.mark.parametrize("vel", [0, -1, 128])
def test_velocity_range_enforced(vel):
    with pytest.raises(ValueError, match="velocity"):
        RenderConfig(plugin_path="p", velocity=vel)


def test_velocity_boundaries():
    RenderConfig(plugin_path="p", velocity=1)
    RenderConfig(plugin_path="p", velocity=127)


@pytest.mark.parametrize("dur", [0, -0.1, -1.0])
def test_duration_must_be_positive(dur):
    with pytest.raises(ValueError, match="duration"):
        RenderConfig(plugin_path="p", duration=dur)


@pytest.mark.parametrize("tail", [-0.1, -1.0])
def test_tail_must_be_non_negative(tail):
    with pytest.raises(ValueError, match="tail"):
        RenderConfig(plugin_path="p", tail=tail)


def test_tail_zero_accepted():
    # tail=0 is legitimate for percussive one-shots that don't need a
    # release-envelope tail.
    RenderConfig(plugin_path="p", tail=0)


@pytest.mark.parametrize("bd", ["8", "32", 16, "16bit"])
def test_bit_depth_rejects_invalid(bd):
    with pytest.raises(ValueError, match="bit_depth"):
        RenderConfig(plugin_path="p", bit_depth=bd)


@pytest.mark.parametrize("bd", ["16", "24", "32f"])
def test_bit_depth_accepts_valid(bd):
    RenderConfig(plugin_path="p", bit_depth=bd)


@pytest.mark.parametrize("fmt", ["mp3", "flac", "WAV", ""])
def test_format_rejects_invalid(fmt):
    with pytest.raises(ValueError, match="format"):
        RenderConfig(plugin_path="p", format=fmt)


@pytest.mark.parametrize("fmt", ["wav", "npy"])
def test_format_accepts_valid(fmt):
    RenderConfig(plugin_path="p", format=fmt)
