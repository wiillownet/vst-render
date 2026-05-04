from __future__ import annotations

import os
from pathlib import Path

import pytest

# Disable Typer's rich-panel error rendering for the whole test session.
# Typer 0.25 + Click 8.3 + Rich 15 wrap BadParameter messages into a
# bordered panel whose layout depends on the terminal mode. On CI
# runners the panel renders in a degenerate single-line form that drops
# the inner text entirely — substring assertions like "duration must be
# > 0" then fail even though the exit code is correct. Plain-text mode
# emits the same content as a regular line that the assertions can match
# reliably across local + CI environments.
from fxp_render.cli import app as _cli_app

_cli_app.rich_markup_mode = None
_cli_app.pretty_exceptions_enable = False


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
