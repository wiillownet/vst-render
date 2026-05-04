# fxp-render

Batch-render VST2 `.fxp` presets to audio files using [DawDreamer](https://github.com/DBraun/DawDreamer) as the headless engine. Windows and macOS; v1 officially supports Serum.

See [DESIGN.md](DESIGN.md) for the full specification, [CLAUDE.md](CLAUDE.md) for the implementation notes, and [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for tracked limitations.

## Requirements

- Windows or macOS
- Python 3.11 – 3.12 (`pyproject.toml` upper bound is `<3.13` to match DawDreamer 0.8.3's wheel coverage; 3.13 will be allowed once upstream ships)
- A VST2 plugin:
  - **Windows:** 64-bit `.dll`. For Serum, point at `C:/Program Files/Common Files/VST3/Serum_x64.dll`, not the 32-bit build in `VST2/` (a 32-bit DLL raises `WinError 193` on 64-bit Python).
  - **macOS:** `.vst` bundle. For Serum, `/Library/Audio/Plug-Ins/VST/Serum.vst`. Note: `.fxp` is a VST2 preset format; the `.vst3` and `.component` (Audio Unit) versions of the plugin will not load `.fxp` files even if DawDreamer accepts the path.
- A valid plugin license present on the machine (DawDreamer does not bypass authorization).

## Install

```bash
pip install git+https://github.com/wiillownet/fxp-render.git
```

Or from a local checkout:

```bash
pip install -e .
```

## CLI

```bash
fxp-render PLUGIN PRESETS OUTPUT [OPTIONS]
```

Render every `.fxp` under a directory to WAV at 44.1 kHz/16-bit:

```bash
# Windows
fxp-render \
    "C:/Program Files/Common Files/VST3/Serum_x64.dll" \
    "C:/Serum Presets/Leads/" \
    ./output/

# macOS
fxp-render \
    "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    "~/Documents/Serum Presets/Leads/" \
    ./output/
```

Common options:

| Flag | Default | Purpose |
| --- | --- | --- |
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

Run `fxp-render --help` for the full list.

## Library API

```python
from fxp_render import RenderConfig, BatchRenderer, ParallelBatchRenderer, render_preset

config = RenderConfig(
    plugin_path="C:/Program Files/Common Files/VST3/Serum_x64.dll",
    sample_rate=44100,
    note=48,
    duration=1.0,
    tail=1.0,
)

# Sequential, reuses one plugin instance across renders
with BatchRenderer(config) as r:
    audio = r.render("C:/Presets/lead.fxp")   # (2, N) float32

# Parallel — returns a dict of path -> audio
with ParallelBatchRenderer(config, workers=-1) as r:
    results = r.render_batch(["a.fxp", "b.fxp", "c.fxp"])

# One-off
audio = render_preset("C:/Presets/lead.fxp", config)
```

## Development

Windows:
```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_parallel_smoke.py  # unit tests
.venv/Scripts/python.exe -m pytest tests/test_parallel_smoke.py \
    --plugin-path "C:/Program Files/Common Files/VST3/Serum_x64.dll" \
    --preset-dir "C:/Serum Presets/Leads/"                                         # integration
```

macOS:
```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ --ignore=tests/test_parallel_smoke.py
.venv/bin/python -m pytest tests/test_parallel_smoke.py \
    --plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --preset-dir "$HOME/Documents/Serum Presets/Leads/"
```

`scripts/verify_dawdreamer.py` sanity-checks three architectural assumptions (preset hot-swap, bad-path recovery, loky crash recovery) against a real plugin. Re-run it after upgrading DawDreamer or adding plugin support.

## License

[GPL-3.0](LICENSE) (inherited from DawDreamer).
