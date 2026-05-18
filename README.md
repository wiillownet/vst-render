# vst-render

Batch-render VST presets to audio files using [DawDreamer](https://github.com/DBraun/DawDreamer) as the headless engine. Windows and macOS; v1 officially supports Serum 1 (`.fxp`) and Serum 2 (`.SerumPreset`).

See [CLAUDE.md](CLAUDE.md) and [CLAUDE-implementation.md](CLAUDE-implementation.md) for the live specification, [docs/architecture.md](docs/architecture.md) for a high-level orientation, [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for tracked limitations, and [DESIGN.md](DESIGN.md) for the original v1 design rationale.

## Requirements

- Windows or macOS
- Python 3.11 – 3.12 (`pyproject.toml` upper bound is `<3.13` to match DawDreamer 0.8.3's wheel coverage; 3.13 will be allowed once upstream ships)
- One or both of:
  - **A Serum 1 plugin** for `.fxp` presets:
    - Windows: 64-bit `.dll`. Point at `C:/Program Files/Common Files/VST3/Serum_x64.dll` — not the 32-bit build under `VST2/`, which raises `WinError 193` on 64-bit Python.
    - macOS: `.vst` bundle, e.g. `/Library/Audio/Plug-Ins/VST/Serum.vst`.
    - Note: `.fxp` is a VST2 preset format. The `.vst3` and `.component` (Audio Unit) versions of Serum 1 will not load `.fxp` files even if DawDreamer accepts the path.
  - **The Serum 2 VST3** for `.SerumPreset` presets:
    - Windows: `Serum2.vst3`.
    - macOS: `/Library/Audio/Plug-Ins/VST3/Serum2.vst3` (a `.vst3` bundle).
    - Serum 2 stores presets in JUCE `IComponent` state-blob form (cbor2 + zstandard), which vst-render decodes via the [`serum2-preset-loader`](https://github.com/wiillownet/serum-2-preset-loader) library before handing the bytes to `synth.load_state`.
- A valid plugin license present on the machine (DawDreamer does not bypass authorization).

## Install

```bash
pip install git+https://github.com/wiillownet/vst-render.git
```

Or from a local checkout:

```bash
pip install -e .
```

## CLI

```bash
vst-render PRESETS OUTPUT --fxp <plugin> [--serum2 <plugin>] [OPTIONS]
```

Pass `--fxp` for `.fxp` presets, `--serum2` for `.SerumPreset` presets, or both for a mixed batch. At least one is required, and every preset format encountered in the input must have its corresponding flag — running over a directory containing both formats with only `--fxp` set is rejected at start-up rather than mid-batch.

Render every `.fxp` under a directory:

```bash
# Windows
vst-render \
    "C:/Serum Presets/Leads/" \
    ./output/ \
    --fxp "C:/Program Files/Common Files/VST3/Serum_x64.dll"

# macOS
vst-render \
    "~/Documents/Serum Presets/Leads/" \
    ./output/ \
    --fxp "/Library/Audio/Plug-Ins/VST/Serum.vst"
```

Render `.SerumPreset` files (Serum 2 only):

```bash
# macOS
vst-render \
    "~/Documents/Serum 2 Presets/Pads/" \
    ./output/ \
    --serum2 "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
```

Mixed-format directory (one batch hits both engines):

```bash
vst-render ~/all-presets/ ./output/ \
    --fxp "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2 "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
```

Common options:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--fxp` | — | Path to a Serum 1 plugin that loads `.fxp`. Required if any input is `.fxp`. |
| `--serum2` | — | Path to the Serum 2 VST3. Required if any input is `.SerumPreset`. |
| `--note` | `48` | MIDI note (0–127). Mutually exclusive with `--midi`. |
| `--velocity` | `127` | MIDI velocity (1–127). |
| `--duration` | `1.0` | Note-on duration (s). |
| `--tail` | `1.0` | Release silence after note-off (s). |
| `--sample-rate` | `44100` | Output sample rate. |
| `--bit-depth` | `16` | `16`, `24`, or `32f`. |
| `--format` | `wav` | `wav` or `npy` (raw float32 stereo array). |
| `--filename-template` | `{preset}` | Vars: `{preset}` `{note}` `{velocity}` `{folder}` `{subpath}`. |
| `--midi` | — | Use a `.mid` file instead of a single note. |
| `--workers` | `-1` | Parallel workers; `-1` = `cpu_count - 1`. |
| `--skip-existing` | off | Skip presets whose output file already exists. |
| `--no-recurse` | off | Don't descend into subdirectories. |
| `--dry-run` | off | Print the render plan and exit. |

Run `vst-render --help` for the full list.

## Migrating from 0.1.x

vst-render 0.2.0 reworks the plugin-path interface for Serum 2 support:

- **CLI:** the leading `PLUGIN` positional argument was replaced with the named flags `--fxp` and `--serum2`. Old: `vst-render <plugin> <presets> <output>`. New: `vst-render <presets> <output> --fxp <plugin>`.
- **Library:** `RenderConfig.plugin_path` was renamed to `RenderConfig.fxp_plugin_path`, and a `serum2_plugin_path` field was added. Existing 0.1.x code passing `plugin_path=` will raise `TypeError: unexpected keyword argument`.
- **Tests / fixtures:** `--plugin-path` and `VST_PLUGIN_PATH` were renamed to `--fxp-plugin-path` and `VST_FXP_PLUGIN_PATH`.

There is no compatibility shim — call sites need a one-time edit. Sorry.

## Library API

`RenderConfig` accepts both plugin paths; the renderer auto-detects each preset's format from its file suffix and dispatches to the matching synth.

```python
from vst_render import RenderConfig, BatchRenderer, ParallelBatchRenderer, render_preset

config = RenderConfig(
    fxp_plugin_path="C:/Program Files/Common Files/VST3/Serum_x64.dll",
    serum2_plugin_path="C:/Program Files/Common Files/VST3/Serum2.vst3",
    sample_rate=44100,
    note=48,
    duration=1.0,
    tail=1.0,
)

# Sequential, reuses one engine across renders
with BatchRenderer(config) as r:
    audio = r.render("C:/Presets/lead.fxp")          # auto-detected as fxp
    audio = r.render("C:/Presets/pad.SerumPreset")   # auto-detected as serum2

# Parallel mixed batch — returns a dict of path -> audio
with ParallelBatchRenderer(config, workers=-1) as r:
    results = r.render_batch([
        "a.fxp",
        "b.fxp",
        "c.SerumPreset",
    ])

# One-off
audio = render_preset("C:/Presets/lead.fxp", config)
```

You only need to set the plugin paths for the formats you actually render — a `.fxp`-only batch needs only `fxp_plugin_path`; a `.SerumPreset`-only batch needs only `serum2_plugin_path`. If a preset's format is encountered without its matching plugin path, the renderer raises `ValueError` with a message naming the missing field, before any worker boots.

## Development

Windows:
```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest tests/ \
    --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py     # unit tests
.venv/Scripts/python.exe -m pytest tests/test_parallel_smoke.py tests/test_serum2_smoke.py \
    --fxp-plugin-path "C:/Program Files/Common Files/VST3/Serum_x64.dll" \
    --serum2-plugin-path "C:/Program Files/Common Files/VST3/Serum2.vst3" \
    --preset-dir "C:/Serum Presets/Leads/" \
    --serum-preset-dir "C:/Serum 2 Presets/Pads/"                                  # integration
```

macOS:
```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ \
    --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py
.venv/bin/python -m pytest tests/test_parallel_smoke.py tests/test_serum2_smoke.py \
    --fxp-plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2-plugin-path "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \
    --preset-dir "$HOME/Documents/Serum Presets/Leads/" \
    --serum-preset-dir "$HOME/Documents/Serum 2 Presets/Pads/"
```

Each fixture is gated independently: a user with only one plugin still runs the smoke half they have plumbing for. Env vars `VST_FXP_PLUGIN_PATH`, `VST_SERUM2_PLUGIN_PATH`, `VST_PRESET_DIR`, `VST_SERUM_PRESET_DIR` are accepted as alternatives to the flags.

`scripts/verify_dawdreamer.py` sanity-checks three architectural assumptions (preset hot-swap, bad-path recovery, loky crash recovery) against a real plugin. Re-run it after upgrading DawDreamer or adding plugin support.

## License

[GPL-3.0](LICENSE) (inherited from DawDreamer).
