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
from vst_render.cli import app as _cli_app

_cli_app.rich_markup_mode = None
_cli_app.pretty_exceptions_enable = False


def pytest_addoption(parser):
    parser.addoption(
        "--fxp-plugin-path",
        action="store",
        default=None,
        help="Path to a Serum 1 VST2/VST3 (loads .fxp presets) for integration tests.",
    )
    parser.addoption(
        "--serum2-plugin-path",
        action="store",
        default=None,
        help="Path to the Serum 2 VST3 (loads .SerumPreset state blobs) for integration tests.",
    )
    parser.addoption(
        "--preset-dir",
        action="store",
        default=None,
        help="Directory containing .fxp presets for integration tests.",
    )
    parser.addoption(
        "--serum-preset-dir",
        action="store",
        default=None,
        help="Directory containing .SerumPreset files for the serum2 smoke test.",
    )


@pytest.fixture
def fxp_plugin_path(request) -> str:
    """Resolved path to a Serum 1 plugin that can load .fxp presets.

    Skips if neither --fxp-plugin-path nor VST_FXP_PLUGIN_PATH is set."""
    path = request.config.getoption("--fxp-plugin-path") or os.environ.get(
        "VST_FXP_PLUGIN_PATH"
    )
    if not path:
        pytest.skip(
            "No fxp plugin path provided. Set --fxp-plugin-path or "
            "VST_FXP_PLUGIN_PATH."
        )
    return str(Path(path).resolve())


@pytest.fixture
def serum2_plugin_path(request) -> str:
    """Resolved path to the Serum 2 VST3 plugin (loads .SerumPreset via load_state).

    Skips if neither --serum2-plugin-path nor VST_SERUM2_PLUGIN_PATH is set."""
    path = request.config.getoption("--serum2-plugin-path") or os.environ.get(
        "VST_SERUM2_PLUGIN_PATH"
    )
    if not path:
        pytest.skip(
            "No serum2 plugin path provided. Set --serum2-plugin-path or "
            "VST_SERUM2_PLUGIN_PATH."
        )
    return str(Path(path).resolve())


@pytest.fixture
def preset_files(request) -> list[str]:
    """Two real `.fxp` files for smoke tests; skips if unavailable."""
    preset_dir = request.config.getoption("--preset-dir") or os.environ.get("VST_PRESET_DIR")
    if not preset_dir:
        pytest.skip("No preset dir provided. Set --preset-dir or VST_PRESET_DIR.")
    files = sorted(Path(preset_dir).rglob("*.fxp"))[:2]
    if len(files) < 2:
        pytest.skip(f"Need >=2 .fxp files in preset dir, found {len(files)}.")
    return [str(f.resolve()) for f in files]


@pytest.fixture
def serum_preset_files(request) -> list[str]:
    """Two real `.SerumPreset` files for the serum2 smoke test; skips if unavailable.

    Mirrors `preset_files` but for the Serum 2 format. The fixture is gated
    independently so a user with only one of the two plugins can still run
    the matching smoke half."""
    preset_dir = request.config.getoption("--serum-preset-dir") or os.environ.get(
        "VST_SERUM_PRESET_DIR"
    )
    if not preset_dir:
        pytest.skip(
            "No serum preset dir provided. Set --serum-preset-dir or "
            "VST_SERUM_PRESET_DIR."
        )
    files = sorted(Path(preset_dir).rglob("*.SerumPreset"))[:2]
    if len(files) < 2:
        pytest.skip(
            f"Need >=2 .SerumPreset files in {preset_dir}, found {len(files)}."
        )
    return [str(f.resolve()) for f in files]
