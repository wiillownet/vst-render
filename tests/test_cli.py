"""
CliRunner coverage for vst_render.cli. These tests never reach the
worker pool — they assert argument parsing, validation, error messaging,
exit codes, and the --dry-run planning path.

Output is read via `result.output` rather than `result.stdout` /
`result.stderr` so the suite is portable across Click versions: in
Click 8.0–8.2 the default `mix_stderr=True` makes `result.stderr`
raise; in Click 8.3+ `mix_stderr` is gone and stderr is always
captured separately. `result.output` is the merged stream in both
versions and never raises.
"""
from __future__ import annotations

from pathlib import Path

import mido
import pytest
from typer.testing import CliRunner

from vst_render.cli import app


runner = CliRunner()


def _touch(p: Path, data: bytes = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def fake_env(tmp_path: Path):
    """A dummy fxp plugin file + a nested preset dir — valid CLI inputs for dry-run."""
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "Leads" / "lead.fxp")
    _touch(presets / "Bass" / "bass.fxp")
    output = tmp_path / "out"
    return plugin, presets, output


# ---- argument validation --------------------------------------------------


def test_note_and_midi_mutually_exclusive(fake_env, tmp_path):
    plugin, presets, output = fake_env
    midi = tmp_path / "seq.mid"
    mid = mido.MidiFile(type=1)
    mid.tracks.append(mido.MidiTrack())
    mid.save(str(midi))

    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--fxp", str(plugin),
            "--note", "60",
            "--midi", str(midi),
        ]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_bad_bit_depth(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--fxp", str(plugin), "--bit-depth", "8"]
    )
    assert result.exit_code != 0
    # Match the explicit BadParameter message, not the help text.
    assert "must be 16, 24, or 32f" in result.output


def test_bad_format(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--fxp", str(plugin), "--format", "mp3"]
    )
    assert result.exit_code != 0
    assert "must be wav or npy" in result.output


def test_duration_zero_rejected(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--fxp", str(plugin), "--duration", "0"]
    )
    assert result.exit_code != 0
    assert "duration must be > 0" in result.output


def test_tail_zero_accepted_in_dry_run(fake_env):
    # tail=0 is valid (percussive) and must not error out.
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--fxp", str(plugin),
            "--tail", "0",
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output


def test_sample_rate_zero_rejected(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--fxp", str(plugin),
            "--sample-rate", "0",
        ]
    )
    assert result.exit_code != 0
    # Typer surfaces `min=1` violations with an "Invalid value" range message.
    assert "Invalid value" in result.output and "sample-rate" in result.output


# ---- plugin-flag validation ----------------------------------------------


def test_no_plugin_flag_rejected(tmp_path):
    """At least one of --fxp or --serum2 is required."""
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(app, [str(presets), str(tmp_path / "out")])
    assert result.exit_code == 2
    assert "At least one of --fxp or --serum2" in result.output


def test_serum2_files_without_serum2_flag(tmp_path):
    """Discovering .SerumPreset files without --serum2 must error with a
    message naming the missing flag, not silently dispatch them through the
    fxp synth."""
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.SerumPreset")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--fxp", str(plugin)]
    )
    assert result.exit_code == 2
    assert ".SerumPreset" in result.output
    assert "--serum2" in result.output


def test_fxp_files_without_fxp_flag(tmp_path):
    """Mirror of the previous test: .fxp files require --fxp."""
    plugin = tmp_path / "plugin.vst3"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--serum2", str(plugin)]
    )
    assert result.exit_code == 2
    assert ".fxp" in result.output
    assert "--fxp" in result.output


def test_mixed_dir_requires_both_flags(tmp_path):
    """A directory with both formats requires both flags — providing only
    one must error out."""
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    _touch(presets / "b.SerumPreset")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--fxp", str(plugin)]
    )
    assert result.exit_code == 2
    assert "--serum2" in result.output


def test_mixed_dir_with_both_flags_passes_validation(tmp_path):
    """Both formats discovered + both flags provided -> dry-run succeeds."""
    fxp_plugin = tmp_path / "fxp_plugin.dll"
    serum2_plugin = tmp_path / "serum2_plugin.vst3"
    _touch(fxp_plugin)
    _touch(serum2_plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    _touch(presets / "b.SerumPreset")
    result = runner.invoke(
        app, [
            str(presets), str(tmp_path / "out"),
            "--fxp", str(fxp_plugin),
            "--serum2", str(serum2_plugin),
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "a.fxp" in result.output
    assert "b.SerumPreset" in result.output


# ---- path validation ------------------------------------------------------


def test_missing_fxp_plugin(tmp_path):
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [
            str(presets),
            str(tmp_path / "out"),
            "--fxp", str(tmp_path / "nope.dll"),
        ]
    )
    assert result.exit_code == 2
    assert "Plugin not found" in result.output


def test_missing_serum2_plugin(tmp_path):
    presets = tmp_path / "presets"
    _touch(presets / "a.SerumPreset")
    result = runner.invoke(
        app, [
            str(presets),
            str(tmp_path / "out"),
            "--serum2", str(tmp_path / "nope.vst3"),
        ]
    )
    assert result.exit_code == 2
    assert "Plugin not found" in result.output


def test_missing_presets(tmp_path):
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    result = runner.invoke(
        app, [
            str(tmp_path / "nope"),
            str(tmp_path / "out"),
            "--fxp", str(plugin),
        ]
    )
    assert result.exit_code == 2
    assert "Presets path not found" in result.output


def test_output_is_existing_file(fake_env, tmp_path):
    plugin, presets, _ = fake_env
    existing_file = tmp_path / "not_a_dir.txt"
    existing_file.write_bytes(b"hello")
    result = runner.invoke(
        app, [str(presets), str(existing_file), "--fxp", str(plugin)]
    )
    assert result.exit_code == 2
    assert "not a directory" in result.output


def test_no_preset_files_found_in_presets_dir(tmp_path):
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    empty_presets = tmp_path / "empty_presets"
    empty_presets.mkdir()
    result = runner.invoke(
        app, [
            str(empty_presets),
            str(tmp_path / "out"),
            "--fxp", str(plugin),
        ]
    )
    # Design says: warning + exit 0 when nothing matches.
    assert result.exit_code == 0
    assert "No supported preset files" in result.output


# ---- MIDI error handling --------------------------------------------------


def test_missing_midi_file(fake_env, tmp_path):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--fxp", str(plugin),
            "--midi", str(tmp_path / "nope.mid"),
        ]
    )
    assert result.exit_code == 2
    assert "MIDI file not found" in result.output


def test_type2_midi_file_clean_error(fake_env, tmp_path):
    plugin, presets, output = fake_env
    type2_midi = tmp_path / "type2.mid"
    mid = mido.MidiFile(type=2)
    mid.tracks.append(mido.MidiTrack())
    mid.save(str(type2_midi))

    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--fxp", str(plugin),
            "--midi", str(type2_midi),
        ]
    )
    assert result.exit_code == 2
    assert "Type 2" in result.output
    # No raw traceback should leak through — that's the whole point of the
    # try/except around get_midi_duration.
    assert "Traceback" not in result.output


def test_corrupt_midi_file_clean_error(fake_env, tmp_path):
    plugin, presets, output = fake_env
    bad = tmp_path / "bad.mid"
    bad.write_bytes(b"not a midi file")

    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--fxp", str(plugin),
            "--midi", str(bad),
        ]
    )
    assert result.exit_code == 2
    assert "Error reading MIDI file" in result.output
    assert "Traceback" not in result.output


# ---- --dry-run + relative-path {subpath} regression -----------------------


def test_dry_run_prints_plan_without_rendering(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--fxp", str(plugin), "--dry-run"]
    )
    assert result.exit_code == 0
    out = result.output
    assert "Would render" in out
    # Pin both sides of the input -> output mapping (input alone leaves room
    # for an output-side regression to pass silently).
    assert " -> " in out
    assert "lead.fxp" in out and "lead.wav" in out
    assert "bass.fxp" in out and "bass.wav" in out
    # Output directory must not have been created when --dry-run.
    assert not output.exists()


def test_dry_run_with_relative_presets_dir_resolves_subpath(tmp_path, monkeypatch):
    """B1 regression: a relative PRESETS arg must still produce a non-empty
    {subpath} — previously the CLI forwarded the relative path to
    compose_filename, relative_to() raised, and the exception was swallowed."""
    plugin = tmp_path / "plugin.dll"
    plugin.write_bytes(b"")
    (tmp_path / "presets" / "Leads").mkdir(parents=True)
    (tmp_path / "presets" / "Leads" / "lead.fxp").write_bytes(b"")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "presets",  # relative — the exact shape the bug needed
            "out",
            "--fxp", str(plugin),
            "--filename-template",
            "{subpath}_{preset}",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    # Post-fix the dry-run line contains the full "Leads_lead.wav" stem;
    # pre-fix it collapsed to just "lead.wav".
    assert "Leads_lead.wav" in result.output
