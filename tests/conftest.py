from __future__ import annotations

import os
from pathlib import Path

import pytest

# Typer 0.25 / Click 8.3 render BadParameter messages inside a rich panel,
# and rich line-wraps the inner text to fit the panel width. On CI runners
# (narrow / non-TTY terminals) a substring like "duration must be > 0"
# gets broken across two visible lines, which makes the substring
# assertions in test_cli.py spuriously fail. Force a wide terminal for
# the whole test session so the panel fits the message on one line.
# Direct assignment, not setdefault — CI shells often pre-set COLUMNS
# to a narrow value that we need to override.
os.environ["COLUMNS"] = "200"


def pytest_addoption(parser):
    parser.addoption(
        "--plugin-path",
        action="store",
        default=None,
        help="Path to Serum VST2 .dll for integration tests.",
    )
    parser.addoption(
        "--preset-dir",
        action="store",
        default=None,
        help="Directory containing .fxp presets for integration tests.",
    )


@pytest.fixture
def plugin_path(request) -> str:
    path = request.config.getoption("--plugin-path") or os.environ.get("FXP_PLUGIN_PATH")
    if not path:
        pytest.skip("No plugin path provided. Set --plugin-path or FXP_PLUGIN_PATH.")
    return str(Path(path).resolve())


@pytest.fixture
def preset_files(request) -> list[str]:
    """Two real `.fxp` files for smoke tests; skips if unavailable."""
    preset_dir = request.config.getoption("--preset-dir") or os.environ.get("FXP_PRESET_DIR")
    if not preset_dir:
        pytest.skip("No preset dir provided. Set --preset-dir or FXP_PRESET_DIR.")
    files = sorted(Path(preset_dir).rglob("*.fxp"))[:2]
    if len(files) < 2:
        pytest.skip(f"Need >=2 .fxp files in preset dir, found {len(files)}.")
    return [str(f.resolve()) for f in files]
