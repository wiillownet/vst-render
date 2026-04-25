# fxp-render — Design Document

## Overview

`fxp-render` is an open-source Python command-line tool for batch rendering VST2 plugin presets (`.fxp` files) to audio samples using [DawDreamer](https://github.com/DBraun/DawDreamer) as the headless rendering engine. The primary motivation is ML dataset creation, but it is designed to be general-purpose and approachable for any user on Windows. **v1 officially supports Serum (VST2) only.** Support for additional VST2 plugins that use the `.fxp` format is planned — see the Future Considerations section for the testing roadmap.

---

## Goals

- Accept a VST2 plugin `.dll` path and a preset source (single `.fxp` or directory) and produce WAV one-shots with minimal configuration required.
- Support MIDI file input as an alternative to fixed-note rendering.
- Parallelize rendering across CPU cores with sensible defaults.
- Be installable as a proper Python package from GitHub.
- Stay simple: one command, clear flags, no required config files.

---

## Non-Goals (v1)

- Cross-platform support (Windows only, due to VST2 `.dll` format).
- VST3 support (DawDreamer + `.fxp` is confirmed broken for VST3).
- Support for plugins other than Serum (v1 only — see plugin roadmap in Future Considerations).
- Keymap / multi-note rendering per preset (single note per preset in v1; possible future feature).
- Audio format conversion (users handle that downstream).
- GUI.

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Rendering engine | [DawDreamer](https://github.com/DBraun/DawDreamer) | Headless VST2 hosting in Python; has an official multiprocessing + Serum example |
| Audio output | [soundfile](https://pypi.org/project/soundfile/) | Clean 16-bit PCM WAV writing from float32 numpy arrays; ships libsndfile binaries on Windows |
| CLI | [Typer](https://typer.tiangolo.com/) | Type hint-driven CLI with minimal boilerplate, auto `--help`, progress bars |
| Parallelism | [loky](https://github.com/joblib/loky) | Persistent worker processes with initializer support; eliminates Windows `__main__` guard requirement that would break library embedding in other projects |
| Package management | `pyproject.toml` + pip | Standard modern Python packaging; installable via `pip install git+...` |

**License note:** DawDreamer is GPLv3. This tool will therefore also be GPLv3.

---

## Repository Layout

```
fxp-render/
├── fxp_render/
│   ├── __init__.py
│   ├── cli.py           # Typer app, argument definitions, entry point
│   ├── renderer.py      # Single-preset render logic (runs inside each worker)
│   ├── worker.py        # Multiprocessing worker: engine init, job loop
│   ├── batch.py         # Job queue construction, worker pool management
│   ├── presets.py       # Preset discovery (file/dir traversal, filtering)
│   └── utils.py         # Filename sanitization, MIDI helpers, logging setup
├── tests/
│   ├── test_sanitize.py         # Filename sanitization rules
│   ├── test_filename.py         # Template composition, variable resolution, collision handling
│   ├── test_presets.py          # Preset discovery, single-file vs directory input
│   ├── test_midi.py             # MIDI duration calculation, Type 2 error handling
│   └── test_parallel_smoke.py   # Integration: renders 2–3 presets via ParallelBatchRenderer(workers=2); skipped if plugin not available
├── pyproject.toml
├── README.md
├── LICENSE              # GPLv3
└── DESIGN.md            # This document
```

---

## CLI Interface

### Entry Point

```
fxp-render [OPTIONS] PLUGIN PRESETS OUTPUT
```

### Positional Arguments

| Argument | Type | Description |
|---|---|---|
| `PLUGIN` | Path | Absolute or relative path to the VST2 plugin `.dll` (e.g. `Serum.dll`) |
| `PRESETS` | Path | Path to a single `.fxp` file, or a directory of `.fxp` files |
| `OUTPUT` | Path | Directory where rendered WAV files will be written (created if missing) |

### Options

| Flag | Type | Default | Description |
|---|---|---|---|
| `--note` | int (0–127) | `48` (C3) | MIDI note to render. Defaults to `None` internally — resolved to `48` in code, not by Typer, so that `--midi` mutual exclusion can be detected cleanly. |
| `--velocity` | int (1–127) | `127` | MIDI velocity |
| `--duration` | float | `1.0` | Note-on duration in seconds |
| `--tail` | float | `1.0` | Silence after note-off to capture release envelope (seconds, `>= 0`; pass `0` for percussive presets that don't need a release tail). Total render = `duration + tail`. |
| `--sample-rate` | int | `44100` | Output sample rate in Hz |
| `--bit-depth` | choice: `16`, `24`, `32f` | `16` | Output bit depth. `32f` = 32-bit float WAV (useful for ML pipelines). |
| `--format` | choice: `wav`, `npy` | `wav` | Output container. `npy` saves raw float32 numpy arrays, skipping bit-depth conversion entirely. |
| `--filename-template` | str | `{preset}` | Output filename template. Available variables: `{preset}` (sanitized preset stem), `{note}` (MIDI note number), `{velocity}`, `{folder}` (immediate parent directory of the preset file), `{subpath}` (full relative path from the presets root, separators replaced with `_`). Example: `{folder}_{preset}` → `Leads_BrightLead.wav`. |
| `--midi` | Path | None | Path to a `.mid` file. Overrides `--note`, `--velocity`, and `--duration`. Mutually exclusive with `--note`. |
| `--workers` | int | `-1` (auto) | Number of parallel worker processes. `-1` = `cpu_count - 1`, minimum 1. |
| `--skip-existing` | flag | off | Skip rendering if the output file already exists. Default is to overwrite. |
| `--no-recurse` | flag | off | Disable recursive subdirectory traversal when `PRESETS` is a directory. |
| `--dry-run` | flag | off | Print the list of presets that would be rendered and exit. No audio is produced. |
| `--verbose` | flag | off | Print per-preset status (loaded, skipped, failed). |

### Example Invocations

```bash
# Minimal — render all presets in a folder with all defaults
fxp-render "C:/VSTPlugins/Serum.dll" "C:/Serum Presets/Leads/" ./output/

# Custom note and tail
fxp-render "C:/VSTPlugins/Serum.dll" presets/ output/ --note 60 --tail 3.0

# MIDI file mode — use a custom MIDI sequence instead of a single note
fxp-render "C:/VSTPlugins/Serum.dll" presets/ output/ --midi my_sequence.mid

# 24-bit output, 6 workers, no subdirectory recursion
fxp-render "C:/VSTPlugins/Serum.dll" presets/ output/ --bit-depth 24 --workers 6 --no-recurse

# Dry run — see what would be rendered
fxp-render "C:/VSTPlugins/Serum.dll" presets/ output/ --dry-run
```

---

## Rendering Pipeline (Per Preset)

Each worker process runs this sequence:

1. **Load engine** (once, at worker startup): Create `daw.RenderEngine(sample_rate, 512)` and `PluginProcessor` pointing at the plugin `.dll`, then call `engine.load_graph(...)`. This is the expensive step — it happens once per worker, not per preset. Subsequent calls to `load_preset()` on a processor already in a loaded graph are supported by DawDreamer without rebuilding the graph — the processor state is updated in-place. (Verified against DawDreamer v0.8.x; re-confirm if upgrading major versions.)
2. **Load preset**: Call `synth.load_preset(fxp_path)`.
3. **Set MIDI**:
   - If `--midi` was provided: `synth.load_midi(midi_path, clear_previous=True, beats=False, all_events=True)`. Render duration = MIDI file length + `--tail`.
   - Otherwise: `synth.clear_midi()` then `synth.add_midi_note(note, velocity, 0.0, duration)`. Render duration = `duration + tail` (default: 1.0 + 1.0 = 2.0 seconds).
4. **Render**: `engine.render(render_duration)`.
5. **Get audio**: `audio = engine.get_audio()` → float32 numpy array, shape `(2, N)`.
6. **Write WAV**: `soundfile.write(output_path, audio.T, sample_rate, subtype=subtype)`.
7. **Report result** back to the main process (success / skip / failure message).

If `load_preset()` fails or the render raises an exception, the worker logs a warning and moves to the next job. It does **not** terminate.

---

## Preset Discovery

When `PRESETS` is a directory, the tool collects all `.fxp` files using `pathlib.Path.rglob("*.fxp")` (recursive by default) or `Path.glob("*.fxp")` (with `--no-recurse`). Files are sorted alphabetically before being enqueued.

When `PRESETS` is a single `.fxp` file, exactly one job is enqueued.

---

## Output File Naming

Each output file is named using the `--filename-template` value (default: `{preset}`), with the result sanitized for filesystem safety and a `.wav` (or `.npy`) extension appended, written into the flat `OUTPUT` directory.

### Template Variables

| Variable | Description | Example |
|---|---|---|
| `{preset}` | Sanitized stem of the preset filename | `Lead_Synth_01` |
| `{note}` | MIDI note number as integer | `48` |
| `{velocity}` | MIDI velocity as integer | `127` |
| `{folder}` | Immediate parent directory name of the preset file | `Leads` |
| `{subpath}` | Full relative path from the presets root, with path separators replaced by `_` | `Leads_Bright` |

For a preset at `Presets/Leads/Bright/Lead_001.fxp` rendered against the root `Presets/`:
- `{preset}` → `Lead_001`
- `{folder}` → `Bright`
- `{subpath}` → `Leads_Bright`
- `{folder}_{preset}` → `Bright_Lead_001.wav`
- `{subpath}_{preset}` → `Leads_Bright_Lead_001.wav`

**When `PRESETS` is a single file:** `{folder}` resolves to the immediate parent directory of that file on disk (same as the directory case). `{subpath}` resolves to an empty string, since there is no presets root to compute a relative path from — if `{subpath}` is used in the template, it is omitted and any adjacent separator is collapsed (e.g. `{subpath}_{preset}` → `Lead_001`).

### Sanitization Rules

Each variable's value is independently sanitized before substitution:

1. Strip leading/trailing whitespace.
2. Replace any character that is not alphanumeric, a hyphen, or an underscore with `_`. Spaces are replaced — filenames with literal spaces are awkward on the Windows CLI.
3. Collapse runs of multiple `_` into a single `_`.
4. Strip leading/trailing `_`.

A variable's sanitized value is allowed to be empty — adjacent separators in the composed name collapse out in the next step, so an empty `{subpath}` or `{folder}` simply vanishes from the filename without breaking it.

The operations are applied in this order to avoid truncation interfering with collision detection:

1. **Sanitize** each variable independently (rules above).
2. **Compose** the full filename by substituting variables into the template and collapsing any runs of `_` introduced by empty substitutions.
3. **Truncate** the composed name to **196 characters** (leaving 4 characters of headroom for short collision suffixes).
4. **Check for collisions** against already-assigned output paths.
5. If a collision exists, **append `_1`, `_2`, etc.** The 196-char truncation from step 3 leaves 4 chars of headroom, fitting suffixes up to `_999` without re-truncation; larger collision counts push the filename past the 200-char comfort zone but stay well under Windows `MAX_PATH` in typical output paths.

**Final-stem fallback:** if the composed-and-collapsed stem ends up empty (e.g. the template resolves to only empty variables, or a preset name sanitizes to nothing), the job gets a deterministic `preset_NNNN` stem where `NNNN` is the zero-padded job index. Applied by `assign_output_paths`, not inside variable sanitization.

### Collision Handling and Overwrite Behaviour

Collision detection (two presets producing the same output filename) is resolved at job-construction time in the main process, so workers always receive a unique pre-computed output path.

**By default, existing files at the output path are overwritten without prompting.** This is intentional for batch jobs where re-running a render should cleanly replace previous output. Use `--skip-existing` to preserve files from a previous run instead.

---

## Parallelism Architecture

```
Main Process
│
├── Builds job list (preset path → output path mapping)
├── Creates loky reusable executor (persistent worker pool)
│
├── Worker 0: init_worker() → RenderEngine loaded → consume jobs
├── Worker 1: init_worker() → RenderEngine loaded → consume jobs
│   ...
├── Worker N: init_worker() → RenderEngine loaded → consume jobs
│
└── Collects futures, updates progress bar, logs failures
```

Workers are initialized once via `loky.get_reusable_executor(initializer=init_worker, initargs=(...))`. Each worker loads Serum in `init_worker()` and stores the engine in a module-level global, which persists across all tasks assigned to that worker. Serum is never reloaded between presets within a worker.

**Why loky over raw `multiprocessing`:** On Windows, stdlib multiprocessing requires an `if __name__ == '__main__'` guard at the entry point of any script that spawns workers. This guard requirement propagates to any application that imports this library — meaning the preset browser app would also need it. loky uses cloudpickle to serialize tasks, bypassing this requirement entirely and making the library safe to embed without constraints on the calling application.

**Executor lifecycle:** The library manages the loky executor internally. Callers simply invoke `render_batch()` or use a context manager — no executor setup or teardown required. The executor is created on first use and shut down when the context manager exits or the process ends. If the executor needs to be reconfigured (e.g. different worker count), the library handles this transparently via loky's `kill_workers=True` parameter.

**Worker count default:** `max(1, os.cpu_count() - 1)`, selected when `--workers -1` is passed. Leaving one core free keeps the system responsive. A machine with 8 cores uses 7 workers by default.

**Memory estimate:** Each worker loads Python + DawDreamer + Serum into its own process, roughly **300–600 MB per worker**. Users with limited RAM should use `--workers 1` or `--workers 2`.

**Import order in workers:** The `init_worker()` function must import `dawdreamer` as its very first statement, before any other imports. loky uses cloudpickle to serialize tasks, which may transitively import libraries (e.g. numba) that conflict with DawDreamer's LLVM backend if they load first. The worker module should be structured as:

```python
def init_worker(plugin_path, sample_rate):
    import dawdreamer  # MUST be first
    # all other imports follow
    ...
```

---

## MIDI File Mode

When `--midi` is supplied:

- `--note`, `--velocity`, and `--duration` are ignored. Because Typer cannot distinguish a user-supplied `--note` from its default value, `--note` defaults to `None` internally — if both `--note` (non-None) and `--midi` are present, the CLI raises an error with a manual check on the `note` and `midi` parameters before execution begins.
- The same MIDI file is used for every preset in the batch.
- Render duration = MIDI file length + `--tail`. MIDI file length is computed using `mido.MidiFile(path).length`, which handles tempo changes automatically and returns total playback time in seconds.
- If the MIDI file is a **Type 2 (asynchronous)** file, mido cannot compute a linear duration and the tool **raises a hard error** with an actionable message: `Error: MIDI file "X.mid" is Type 2 (asynchronous) and duration cannot be determined. Convert it to Type 0 or Type 1 using your DAW or a tool like mido before rendering.` Type 2 files are extremely rare in practice — no modern DAW exports them.
- `mido` is an additional dependency (pure Python, MIT license, ~150 KB).

---

## Bit Depth Mapping

16-bit is the default because this tool is designed for batch rendering — potentially thousands of files at once. Keeping the default bit depth low minimizes disk usage and write time across large jobs. Users who need higher fidelity (e.g. for professional sample pack distribution) can opt in with `--bit-depth 24`. 32-bit float is included because it costs nothing to implement (one extra entry in a lookup table) and is a natural pairing with `.npy` output for ML pipelines requiring end-to-end float precision.

| `--bit-depth` | soundfile `subtype` | Notes |
|---|---|---|
| `16` | `PCM_16` | **Default.** Best for batch jobs where disk space matters. Standard CD quality. |
| `24` | `PCM_24` | Professional sample pack standard. ~50% larger files than 16-bit. |
| `32f` | `FLOAT` | 32-bit float WAV. Matches DAW internal precision. Useful for ML pipelines. Largest files. |

---

## Raw / Programmatic Output

For users integrating `fxp-render` into another Python project, writing WAV files to disk may be unnecessary overhead. Two mechanisms are provided:

### `--format npy` (CLI flag)

An optional `--format` flag selects the output container:

| `--format` | Extension | Description |
|---|---|---|
| `wav` | `.wav` | **Default.** Standard audio file, bit depth controlled by `--bit-depth`. |
| `npy` | `.npy` | NumPy binary format. Saves the raw float32 stereo array `(2, N)` directly. No bit-depth conversion. Immediately loadable with `numpy.load()`. |

`.npy` output is most useful when the rendered audio will be consumed by another Python script (e.g., a training data pipeline) and WAV encoding/decoding would be wasteful. File sizes are comparable to 32-bit float WAV.

```python
# Loading .npy output in another project
import numpy as np
audio = np.load("PresetName.npy")  # shape: (2, num_samples), dtype: float32
```

### Python Library API

The core rendering logic is exposed as a public Python API so the tool can be used programmatically without spawning a subprocess or touching disk.

#### `RenderConfig` — configuration dataclass

All renderer classes and `render_preset()` accept a `RenderConfig` instance. Fields and defaults match the CLI:

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

@dataclass
class RenderConfig:
    plugin_path: str | Path           # Required. Absolute path to VST2 .dll
    sample_rate: int = 44100
    note: int = 48                    # C3; ignored if midi_path is set
    velocity: int = 127
    duration: float = 1.0             # Note-on duration in seconds
    tail: float = 1.0                 # Post-release silence in seconds
    bit_depth: Literal["16", "24", "32f"] = "16"
    format: Literal["wav", "npy"] = "wav"
    midi_path: str | Path | None = None  # If set, overrides note/velocity/duration

    def __post_init__(self):
        self.plugin_path = Path(self.plugin_path)
        if self.midi_path is not None:
            self.midi_path = Path(self.midi_path)
        # Cheap range checks only — no file I/O here
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if not (0 <= self.note <= 127):
            raise ValueError(f"note must be 0–127, got {self.note}")
        if not (1 <= self.velocity <= 127):
            raise ValueError(f"velocity must be 1–127, got {self.velocity}")
        if self.duration <= 0:
            raise ValueError(f"duration must be > 0, got {self.duration}")
        if self.tail < 0:
            raise ValueError(f"tail must be >= 0, got {self.tail}")
```

`bit_depth` must be passed as one of the string literals `"16"`, `"24"`, or `"32f"` — integer values are not coerced.

**Validation scope:** `__post_init__` performs only cheap checks (range validation, path normalization) with no file I/O. Plugin path existence and MIDI file validity (including Type 0/1 check) are verified at renderer entry — `BatchRenderer.__enter__()` or the first `render()` call — where failures produce clear, actionable errors without surprising side effects on construction.

**Note on CLI vs library path checking:** The CLI performs its own upfront `PLUGIN` path existence check before any workers start — this is separate from `RenderConfig` validation. The library API defers path checking to renderer entry. Both ultimately produce the same error; the timing differs.

#### Instance reuse

A critical design point: **Serum is loaded once and reused for the entire lifetime of a renderer object.** Calling `renderer.render(new_preset)` does not reload the DLL — it calls `load_preset()` on the already-running instance, then clears MIDI state and renders. This makes sequential rendering fast after the initial cold start (~1–2s for the DLL load).

#### `BatchRenderer` — single-process, sequential

Boots one Serum instance and reuses it across all renders. No multiprocessing overhead. Best for pipelines that handle their own parallelism or don't need it.

```python
from fxp_render import BatchRenderer, RenderConfig

config = RenderConfig(
    plugin_path="C:/VSTPlugins/Serum.dll",
    sample_rate=44100,
    note=48,
    velocity=127,
    duration=1.0,
    tail=1.0,
)

with BatchRenderer(config) as renderer:
    for fxp_path in preset_list:
        audio = renderer.render(fxp_path)  # float32 numpy (2, N), Serum never reloads
```

#### `ParallelBatchRenderer` — multi-process, parallel

Manages a pool of worker processes internally, each with their own warm plugin instance. Distributes a list of presets across workers and collects results. `workers` is passed as a constructor argument (not part of `RenderConfig`) since `BatchRenderer` and `render_preset()` have no use for it.

A key use case is **pre-rendering all presets upfront** for applications like a preset browser, where instant audio playback on selection is required. Rather than rendering on demand (slow), the application renders the entire library once at startup or as a background task, then caches results for instant retrieval.

```python
from fxp_render import ParallelBatchRenderer, RenderConfig

config = RenderConfig(plugin_path="C:/VSTPlugins/Serum.dll", ...)

with ParallelBatchRenderer(config, workers=-1) as renderer:
    # Returns a dict mapping fxp_path -> float32 numpy array
    results = renderer.render_batch(preset_list)

    # Or iterate as results come in (unordered, yields as each completes)
    for fxp_path, audio in renderer.iter_batch(preset_list):
        print(f"Rendered: {fxp_path}")
```

`workers=-1` uses the same auto-detect logic as the CLI (`cpu_count - 1`).

**Memory consideration:** Holding all rendered audio in memory is only practical for small-to-medium libraries. At 44.1kHz stereo float32, a 2-second render (the default: 1s note + 1s tail) is ~700 KB per preset — 1000 presets ≈ 700 MB RAM. For larger libraries, the recommended pattern is to write results to `.npy` or `.wav` files via `iter_batch()` and load individual files on demand, rather than keeping everything in memory.


#### `render_preset()` — convenience function (single render, no reuse)

For one-off renders where lifecycle management is not needed. Creates a fresh engine per call — convenient but not suitable for batch use.

```python
from fxp_render import render_preset, RenderConfig

audio = render_preset("C:/Presets/lead.fxp", RenderConfig(...))
```

**Audio playback is out of scope for this library.** Callers are responsible for playing back or otherwise consuming the returned numpy arrays. `sounddevice` is a common choice for Python audio playback if needed.

---

## Error Handling Summary

| Situation | Behavior |
|---|---|
| `PLUGIN` path not found | CLI error before any workers start |
| `PRESETS` path not found | CLI error before any workers start |
| `OUTPUT` dir does not exist | Created automatically |
| No `.fxp` files found | Warning + exit 0 |
| `load_preset()` failure | Skip preset, log warning, continue |
| Render exception | Skip preset, log warning, continue |
| Silent output (peak amplitude below −90 dBFS) | Log warning with preset path, continue. Checked as `np.max(np.abs(audio)) < 3.16e-5`. The −90 dBFS threshold was chosen to match the 16-bit quantization floor — anything quieter is below the noise floor of the default output format. Users rendering at `--bit-depth 24` or `32f` may see this fire for legitimately quiet presets; treat warnings as advisory. Note: presets with long attack envelopes or pre-delay effects may trigger this spuriously. |
| Worker crash | Log the error on the crashed job's future. loky flags the entire executor as broken, so every remaining future submitted to it also raises — each is surfaced as an error result and the batch returns without hanging. Recovery on a re-run is handled by `--skip-existing`; see `KNOWN_ISSUES.md` for details. |
| `--midi` + `--note` both passed | Manual check on `note` and `midi` parameters raises error before execution |
| Invalid MIDI file | CLI error before workers start |
| Type 2 MIDI file | Hard error with message directing user to convert to Type 0 or Type 1 |
| Output file already exists | Overwritten by default; skipped if `--skip-existing` is set |

---

## Dependencies

```toml
[project]
name = "fxp-render"
requires-python = ">=3.11,<3.14"
dependencies = [
    "dawdreamer",
    "soundfile",
    "typer",
    "loky",       # Persistent multiprocessing workers without __main__ guard requirement
    "mido",       # MIDI file duration parsing
    "numpy",      # Transitive via dawdreamer, but declared explicitly
]
```

The upper bound `<3.14` reflects that DawDreamer's PyPI wheels are currently published for Python 3.11–3.13. Confirm wheel availability and remove or advance this cap when upgrading Python. Install from GitHub:
```bash
pip install git+https://github.com/<username>/fxp-render.git
```

---

## Open Decisions

All decisions have been resolved. See the table below for a full record.

| # | Decision | Resolution |
|---|---|---|
| 1 | Default note duration | ✅ **1.0 second** |
| 2 | Default tail duration | ✅ **1.0 second** (total render = 2.0s by default) |
| 3 | Worker crash behavior | ✅ **Log each broken future as an error; return the batch without hanging.** Verified: loky flags the whole executor broken on a worker crash, it does not redistribute. Re-runs use `--skip-existing`. |
| 4 | Filename convention | ✅ **Preserve preset stem by default**; `--filename-template` flag for overrides |
| 5 | `--bit-depth 32f` | ✅ **Included** — trivial to implement, useful for ML float pipelines |
| 6 | `mido` dependency | ✅ **Keep** — `MidiFile.length` handles tempo-aware duration in one line |
| 7 | Library API scope | ✅ **`BatchRenderer` + `ParallelBatchRenderer` in v1; `PreviewRenderer` deferred indefinitely** |
| 8 | `.npy` format | ✅ **Included in v1** via `--format npy` |
| 9 | `PreviewRenderer` cancellation | ✅ **Moot** — `PreviewRenderer` deferred |
| 10 | Audio playback scope | ✅ **Out of scope** — callers handle their own playback |

---

## Known Limitations & Gotchas

- **VST2 only.** Serum's `.fxp` format is VST2-native. DawDreamer's VST3 path silently ignores `.fxp` files.
- **`set_parameter()` may not affect audio for some Serum effects** (DawDreamer Issue #205). Use fully-configured presets rather than trying to patch parameters at render time.
- **DawDreamer must be imported before JAX, PyTorch TorchScript, or Numba** in any process — including loky worker processes. `init_worker()` must import `dawdreamer` as its very first statement.
- **Renders are not guaranteed to be bit-identical across runs.** Serum presets that use randomized oscillator phase, random LFO start position, or stochastic modulation will produce different audio each time they are rendered. For ML datasets requiring strict reproducibility, verify that target presets do not use these features — there is no `--seed` mechanism in DawDreamer or Serum's headless rendering path.
- **Serum requires a valid license** present on the machine. DawDreamer does not bypass plugin authorization.
- **Large batches consume significant disk space.** 1000 presets × 2-second stereo WAV at 44.1kHz/16-bit ≈ ~350 MB.
- **`--skip-existing` and collision disambiguation interact.** When two presets map to the same filename, they are disambiguated by appending `_1`, `_2`, etc. in job-construction order. On a re-run with `--skip-existing`, disambiguation is recomputed fresh — if the preset list has changed (files added or removed), suffix assignments may shift, causing the wrong files to be skipped. This is a known limitation; for stable re-runs, ensure the preset list is identical between runs.
- **The library uses a named logger** (`logging.getLogger("fxp_render")`) and never calls `logging.basicConfig()`. Only the CLI entry point configures logging. Embedders can control output by adding handlers to the `fxp_render` logger without affecting the rest of their application.

---

## Future Considerations (Post-v1)

### Plugin Support Expansion

v1 officially supports **Serum (VST2) only**. Expanding to other plugins is a planned roadmap item, but each plugin must be manually tested and verified before being listed as officially supported. The following are candidates based on known .fxp compatibility:

**High priority (known .fxp support, likely well-behaved headlessly):**
- Sylenth1 — VST2-only, .fxp is the sole preset format
- OB-Xd — parameter mode .fxp, open source, no licensing complications
- Synth1 — parameter mode .fxp, VST2-only, widely used for datasets
- TAL-NoiseMaker — confirmed working in DawDreamer
- u-he Diva / Zebra2 — confirmed working in DawDreamer

**Medium priority (works but with known quirks):**
- Vital — loads fine but uses `.vital` JSON format; requires `load_state()` workaround
- TAL-U-NO-LX — state leakage between renders requires mitigation
- FabFilter effects — effects chain rendering (different pipeline from instruments)

**Requires investigation:**
- NI Massive — proprietary format; phone-home licensing may block headless use
- Arturia V Collection — authorization issues reported in headless environments

Testing each plugin involves: confirming `load_preset()` works, verifying no silent output, checking for state leakage across sequential renders, and documenting any required workarounds (warm-up delay, editor initialization, etc.).

### Other Planned Features
- **Keymap mode:** Render each preset across a note range (e.g., C2–C6) for sampler instrument creation.
- **Velocity layers:** Render each preset at multiple velocities.
- **Metadata manifest:** Write a companion CSV/JSON mapping output filenames to preset name, MIDI note, velocity, and render settings.
- **`PreviewRenderer`:** On-demand single-instance async renderer for large libraries where pre-rendering upfront isn't feasible. Deferred indefinitely — add when there is a concrete use case.
- **pip release:** Publish to PyPI once the tool is stable and tested across multiple plugins.
