from pathlib import Path

import pytest

from fxp_render.presets import discover_presets


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def test_single_file_returned_as_is(tmp_path: Path):
    f = tmp_path / "one.fxp"
    _touch(f)
    assert discover_presets(f) == [f.resolve()]


def test_directory_recursive_by_default(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "sub" / "b.fxp")
    names = [p.name for p in discover_presets(tmp_path)]
    assert "a.fxp" in names and "b.fxp" in names


def test_directory_no_recurse(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "sub" / "b.fxp")
    names = [p.name for p in discover_presets(tmp_path, recurse=False)]
    assert names == ["a.fxp"]


def test_results_sorted_alphabetically(tmp_path: Path):
    for n in ["c.fxp", "a.fxp", "b.fxp"]:
        _touch(tmp_path / n)
    names = [p.name for p in discover_presets(tmp_path)]
    assert names == ["a.fxp", "b.fxp", "c.fxp"]


def test_nonexistent_path_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        discover_presets(tmp_path / "does_not_exist")


def test_empty_directory_returns_empty(tmp_path: Path):
    assert discover_presets(tmp_path) == []


def test_non_fxp_files_ignored(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "b.wav")
    _touch(tmp_path / "c.txt")
    result = discover_presets(tmp_path)
    assert len(result) == 1 and result[0].name == "a.fxp"


def test_results_are_absolute(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    for p in discover_presets(tmp_path):
        assert p.is_absolute()
