"""
CliRunner coverage for fxp_render.cli. These tests never reach the
worker pool — they assert argument parsing, validation, error messaging,
exit codes, and the --dry-run planning path.
"""
from __future__ import annotations

from pathlib import Path

import mido
import pytest
from typer.testing import CliRunner

from fxp_render.cli import app


runner = CliRunner()


def _touch(p: Path, data: bytes = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def fake_env(tmp_path: Path):
    """A dummy plugin file + a nested preset dir — valid CLI inputs for dry-run."""
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
        app, [str(plugin), str(presets), str(output), "--note", "60", "--midi", str(midi)]
    )
    assert result.exit_code != 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "mutually exclusive" in combined


def test_bad_bit_depth(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(app, [str(plugin), str(presets), str(output), "--bit-depth", "8"])
    assert result.exit_code != 0
    assert "bit-depth" in (result.stderr or result.stdout)


def test_bad_format(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(app, [str(plugin), str(presets), str(output), "--format", "mp3"])
    assert result.exit_code != 0
    assert "format" in (result.stderr or result.stdout)


def test_duration_zero_rejected(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(app, [str(plugin), str(presets), str(output), "--duration", "0"])
    assert result.exit_code != 0
    assert "duration" in (result.stderr or result.stdout)


def test_tail_zero_accepted_in_dry_run(fake_env):
    # tail=0 is valid (percussive) and must not error out.
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(plugin), str(presets), str(output), "--tail", "0", "--dry-run"]
    )
    assert result.exit_code == 0, result.stderr or result.stdout


def test_sample_rate_zero_rejected(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(plugin), str(presets), str(output), "--sample-rate", "0"]
    )
    assert result.exit_code != 0


# ---- path validation ------------------------------------------------------


def test_missing_plugin(tmp_path):
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [str(tmp_path / "nope.dll"), str(presets), str(tmp_path / "out")]
    )
    assert result.exit_code == 2
    assert "Plugin not found" in (result.stderr or result.stdout)


def test_missing_presets(tmp_path):
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    result = runner.invoke(
        app, [str(plugin), str(tmp_path / "nope"), str(tmp_path / "out")]
    )
    assert result.exit_code == 2
    assert "Presets path not found" in (result.stderr or result.stdout)


def test_output_is_existing_file(fake_env, tmp_path):
    plugin, presets, _ = fake_env
    existing_file = tmp_path / "not_a_dir.txt"
    existing_file.write_bytes(b"hello")
    result = runner.invoke(app, [str(plugin), str(presets), str(existing_file)])
    assert result.exit_code == 2
    assert "not a directory" in (result.stderr or result.stdout)


def test_no_fxp_files_found_in_presets_dir(tmp_path):
    plugin = tmp_path / "plugin.dll"
    _touch(plugin)
    empty_presets = tmp_path / "empty_presets"
    empty_presets.mkdir()
    result = runner.invoke(app, [str(plugin), str(empty_presets), str(tmp_path / "out")])
    # Design says: warning + exit 0 when nothing matches.
    assert result.exit_code == 0
    assert "No .fxp files found" in (result.stderr or result.stdout)


# ---- MIDI error handling --------------------------------------------------


def test_missing_midi_file(fake_env, tmp_path):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(plugin), str(presets), str(output), "--midi", str(tmp_path / "nope.mid")]
    )
    assert result.exit_code == 2
    assert "MIDI file not found" in (result.stderr or result.stdout)


def test_type2_midi_file_clean_error(fake_env, tmp_path):
    plugin, presets, output = fake_env
    type2_midi = tmp_path / "type2.mid"
    mid = mido.MidiFile(type=2)
    mid.tracks.append(mido.MidiTrack())
    mid.save(str(type2_midi))

    result = runner.invoke(
        app, [str(plugin), str(presets), str(output), "--midi", str(type2_midi)]
    )
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "Type 2" in combined
    # No raw traceback should leak through — that's the whole point of the
    # try/except around get_midi_duration.
    assert "Traceback" not in combined


def test_corrupt_midi_file_clean_error(fake_env, tmp_path):
    plugin, presets, output = fake_env
    bad = tmp_path / "bad.mid"
    bad.write_bytes(b"not a midi file")

    result = runner.invoke(
        app, [str(plugin), str(presets), str(output), "--midi", str(bad)]
    )
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "Traceback" not in combined


# ---- --dry-run + relative-path {subpath} regression -----------------------


def test_dry_run_prints_plan_without_rendering(fake_env):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(plugin), str(presets), str(output), "--dry-run"]
    )
    assert result.exit_code == 0
    out = result.stdout or ""
    assert "Would render" in out
    assert "lead.fxp" in out and "bass.fxp" in out
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
            str(plugin),
            "presets",  # relative, the exact shape the bug needed
            "out",
            "--filename-template",
            "{subpath}_{preset}",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stderr or result.stdout
    out = result.stdout or ""
    # Post-fix the dry-run line contains the full "Leads_lead.wav" stem;
    # pre-fix it collapsed to just "lead.wav".
    assert "Leads_lead.wav" in out
