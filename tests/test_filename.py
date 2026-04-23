from pathlib import Path

from fxp_render.utils import assign_output_paths, compose_filename


def test_compose_simple_preset(tmp_path: Path):
    preset = tmp_path / "Leads" / "MyPreset.fxp"
    assert compose_filename("{preset}", preset, tmp_path, 48, 127) == "MyPreset"


def test_compose_folder_preset(tmp_path: Path):
    preset = tmp_path / "Leads" / "MyPreset.fxp"
    assert compose_filename("{folder}_{preset}", preset, tmp_path, 48, 127) == "Leads_MyPreset"


def test_compose_subpath_nested(tmp_path: Path):
    preset = tmp_path / "Leads" / "Bright" / "p.fxp"
    assert compose_filename("{subpath}_{preset}", preset, tmp_path, 48, 127) == "Leads_Bright_p"


def test_compose_subpath_none_single_file_mode(tmp_path: Path):
    # Single-file mode: presets_root is None -> {subpath} collapses out
    preset = tmp_path / "p.fxp"
    assert compose_filename("{subpath}_{preset}", preset, None, 48, 127) == "p"


def test_compose_subpath_at_root(tmp_path: Path):
    # Preset directly under the root -> rel.parts == () -> subpath == ""
    preset = tmp_path / "p.fxp"
    assert compose_filename("{subpath}_{preset}", preset, tmp_path, 48, 127) == "p"


def test_compose_note_and_velocity(tmp_path: Path):
    preset = tmp_path / "p.fxp"
    result = compose_filename("{preset}_n{note}_v{velocity}", preset, tmp_path, 60, 100)
    assert result == "p_n60_v100"


def test_compose_sanitizes_preset_stem(tmp_path: Path):
    preset = tmp_path / "Leads" / "Lead [FP].fxp"
    assert compose_filename("{preset}", preset, tmp_path, 48, 127) == "Lead_FP"


def test_compose_sanitizes_folder(tmp_path: Path):
    preset = tmp_path / "Bass (Hard)" / "BA.fxp"
    assert compose_filename("{folder}_{preset}", preset, tmp_path, 48, 127) == "Bass_Hard_BA"


def test_compose_truncates_to_196(tmp_path: Path):
    # Long preset name must truncate to 196 chars to leave collision headroom.
    long_stem = "a" * 300
    preset = tmp_path / f"{long_stem}.fxp"
    result = compose_filename("{preset}", preset, tmp_path, 48, 127)
    assert len(result) == 196


def test_compose_relative_preset_root_with_relative_preset(tmp_path, monkeypatch):
    # Regression: a relative presets_root passed alongside an absolute
    # preset_path (as discover_presets returns) used to raise ValueError
    # inside relative_to() and silently collapse {subpath} to "".
    # The CLI now resolves presets_root before calling compose_filename;
    # this test pins the resolved-abs + abs-preset contract.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "presets" / "Leads").mkdir(parents=True)
    preset_abs = (tmp_path / "presets" / "Leads" / "lead.fxp").resolve()
    preset_abs.write_bytes(b"")

    # presets_root resolved to absolute (what cli.py does post-fix).
    resolved_root = Path("presets").resolve()
    result = compose_filename("{subpath}_{preset}", preset_abs, resolved_root, 48, 127)
    assert result == "Leads_lead", (
        f"expected subpath to resolve to 'Leads'; got {result!r}"
    )


def test_compose_preset_outside_root(tmp_path: Path):
    # If the preset isn't under presets_root, subpath silently becomes ""
    # (relative_to raises ValueError, which we swallow).
    preset = tmp_path / "outside" / "p.fxp"
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    assert compose_filename("{subpath}_{preset}", preset, other_root, 48, 127) == "p"


def test_assign_no_collisions(tmp_path: Path):
    jobs = [{"filename_stem": "a"}, {"filename_stem": "b"}]
    result = assign_output_paths(jobs, tmp_path, ".wav")
    assert result[0]["output_path"] == str(tmp_path / "a.wav")
    assert result[1]["output_path"] == str(tmp_path / "b.wav")


def test_assign_disambiguates_collisions(tmp_path: Path):
    jobs = [{"filename_stem": "dup"} for _ in range(3)]
    result = assign_output_paths(jobs, tmp_path, ".wav")
    assert result[0]["output_path"] == str(tmp_path / "dup.wav")
    assert result[1]["output_path"] == str(tmp_path / "dup_1.wav")
    assert result[2]["output_path"] == str(tmp_path / "dup_2.wav")


def test_assign_preserves_input_order(tmp_path: Path):
    jobs = [
        {"filename_stem": "z"},
        {"filename_stem": "a"},
        {"filename_stem": "m"},
    ]
    result = assign_output_paths(jobs, tmp_path, ".wav")
    assert [Path(j["output_path"]).stem for j in result] == ["z", "a", "m"]


def test_assign_mixes_collisions_with_unique(tmp_path: Path):
    jobs = [
        {"filename_stem": "dup"},
        {"filename_stem": "unique"},
        {"filename_stem": "dup"},
    ]
    result = assign_output_paths(jobs, tmp_path, ".wav")
    assert result[0]["output_path"] == str(tmp_path / "dup.wav")
    assert result[1]["output_path"] == str(tmp_path / "unique.wav")
    assert result[2]["output_path"] == str(tmp_path / "dup_1.wav")


def test_assign_honors_extension(tmp_path: Path):
    jobs = [{"filename_stem": "a"}]
    result = assign_output_paths(jobs, tmp_path, ".npy")
    assert result[0]["output_path"].endswith(".npy")


def test_assign_empty_stem_falls_back_to_index(tmp_path: Path):
    jobs = [{"filename_stem": ""}, {"filename_stem": "ok"}, {"filename_stem": ""}]
    result = assign_output_paths(jobs, tmp_path, ".wav")
    assert result[0]["output_path"] == str(tmp_path / "preset_0000.wav")
    assert result[1]["output_path"] == str(tmp_path / "ok.wav")
    assert result[2]["output_path"] == str(tmp_path / "preset_0002.wav")
