# CLAUDE.md — vst-render Implementation Guide

This file is the implementation context for Claude Code. Read `DESIGN.md` first for the full specification. This document covers the *how*: build order, exact API calls, code skeletons, and constraints that the design doc omits because they're not relevant to a human reader.

---

## Project at a glance

- **Name:** `vst-render` (package: `vst_render`, CLI command: `vst-render`)
- **Purpose:** Batch render VST presets to audio files using DawDreamer as the headless engine. v1 supports two preset formats:
  - `.fxp` (Serum 1, VST2 preset format) — loaded via `synth.load_preset(path)`
  - `.SerumPreset` (Serum 2, JUCE state-blob format) — converted via `serum2_preset_loader.convert_preset_file(path)` to bytes, written to a per-worker tempfile, then loaded via `synth.load_state(path)`
- **Platform:** Windows (`.dll`/`.vst3`) and macOS (`.vst` and `.vst3` bundles). Linux untested. The plugin path on macOS is a bundle directory; `Path.exists()` and DawDreamer's `make_plugin_processor` both accept it.
- **License:** GPLv3 (inherited from DawDreamer)
- **Python:** 3.11–3.12 (`pyproject.toml` upper bound is `<3.13` to match DawDreamer 0.8.3's wheel coverage; 3.13 users must wait for upstream)

---

## Build order

Build files in this sequence. Each stage has no dependency on the next:

1. `pyproject.toml` — package metadata and entry point
2. `vst_render/__init__.py` — public API exports
3. `vst_render/config.py` — `RenderConfig` dataclass (pure Python, no DawDreamer)
4. `vst_render/utils.py` — filename sanitization, template composition, MIDI helpers (pure Python)
5. `vst_render/presets.py` — preset discovery (pure Python, pathlib only)
6. `vst_render/renderer.py` — single-preset render logic (uses DawDreamer)
7. `vst_render/worker.py` — loky worker init and task function (uses DawDreamer)
8. `vst_render/batch.py` — job construction, loky executor management, progress reporting
9. `vst_render/api.py` — `BatchRenderer`, `ParallelBatchRenderer`, `render_preset()` public classes
10. `vst_render/cli.py` — Typer app wiring everything together

Write tests alongside each module, not after. Tests for stages 3–5 require no plugin installed. Tests for stages 6–10 are gated behind the plugin availability fixture (see Testing section).

---

## pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "vst-render"
version = "0.2.0"
requires-python = ">=3.11,<3.13"
dependencies = [
    "dawdreamer",
    "soundfile",
    "typer",
    "loky",
    "mido",
    "numpy",
    "serum2-preset-loader @ git+https://github.com/wiillownet/serum-2-preset-loader@<full-40-char-sha>",
]

[project.scripts]
vst-render = "vst_render.cli:app"

[project.optional-dependencies]
dev = ["pytest", "pytest-mock"]

[tool.hatch.metadata]
# `serum2-preset-loader` is git-only until it ships on PyPI; hatch
# refuses direct URL deps without this opt-in.
allow-direct-references = true

[tool.pytest.ini_options]
markers = ["slow: integration tests that require a plugin to be installed"]
```

`serum2-preset-loader` must be pinned to a **full 40-character commit SHA**. Pip's partial clone fails on short SHAs with `error: pathspec '<short>' did not match any file(s) known to git`. Resolve via `git log -1 --format="%H" <short-sha>` from the source repo before pinning.

The entry point `vst-render = "vst_render.cli:app"` wires the `vst-render` command to the Typer app instance named `app` in `cli.py`.

---

## DawDreamer API reference

DawDreamer is niche — use these exact calls. Do not guess at method names.

### Engine and plugin setup

```python
import dawdreamer as daw

# Create engine: (sample_rate: int, buffer_size: int)
engine = daw.RenderEngine(44100, 512)

# Load a VST2 plugin: (name: str, path: str)
# path MUST be an absolute path string (not a Path object)
synth = engine.make_plugin_processor("serum", str(plugin_path.resolve()))

# Build the audio graph — instruments have no audio inputs
engine.load_graph([(synth, [])])
```

### Preset loading — `.fxp`

```python
# Load an .fxp preset — call this between renders, no need to rebuild graph
synth.load_preset(str(fxp_path.resolve()))  # must be absolute path string
```

### Preset loading — `.SerumPreset` (Serum 2)

`.SerumPreset` files are JUCE `IComponent` state blobs (cbor2 + zstandard
wrapper around the raw VST3 state). DawDreamer can load the inner state
via `synth.load_state(path)`, but the file on disk is the wrapped form
that the plugin's preset browser reads — not what `load_state` accepts.

The `serum2_preset_loader.convert_preset_file()` helper performs the
wrapper unwrap + cbor2 decode + zstd inflate and returns the raw state
as `bytes`. `load_state` takes a path, not bytes, so the worker writes
the bytes to a per-worker tempfile and passes that path:

```python
from serum2_preset_loader import convert_preset_file

state_bytes = convert_preset_file(serum_preset_path)  # returns bytes
state_path = Path(tempfile.mkdtemp(prefix="vst_render_serum2_")) / "state.bin"
state_path.write_bytes(state_bytes)
synth.load_state(str(state_path))  # must be absolute path string
```

Reuse the same `state_path` for every job in the worker — `write_bytes`
overwrites in place, and the previous job's blob is never re-read. Do
not share `state_path` across workers; each loky worker creates its own
`mkdtemp` directory at init time.

### MIDI — single note mode

```python
# Clear any previously added MIDI events
synth.clear_midi()

# Add a note: (note: int, velocity: int, start_time: float, duration: float)
# Timing is in seconds (beats=False is default)
synth.add_midi_note(48, 127, 0.0, 1.0)
```

### MIDI — file mode

```python
# Load a MIDI file (replaces any previously added notes)
synth.load_midi(
    str(midi_path.resolve()),  # must be absolute path string
    clear_previous=True,
    beats=False,               # use seconds, not beats
    all_events=True,           # include CC, pitch bend, etc.
)
```

### Rendering

```python
# Render: (duration: float) in seconds
engine.render(2.0)

# Get audio: returns float32 numpy array, shape (channels, samples) — typically (2, N)
audio = engine.get_audio()  # shape: (2, N), dtype: float32

# NOTE: soundfile expects (samples, channels) — transpose before writing:
import soundfile as sf
sf.write(str(output_path), audio.T, sample_rate, subtype="PCM_16")
```

### BPM (only needed if MIDI file uses beat-relative timing)

```python
engine.set_bpm(120.0)  # default is 120; only matters if beats=True
```

### Getting plugin parameter count and names (useful for debugging)

```python
num_params = synth.get_num_parameters()
names = [synth.get_parameter_name(i) for i in range(num_params)]
```

### Opening the plugin editor (debugging only — not used in headless rendering)

```python
synth.open_editor()  # blocks until window is closed
```

---

## Critical constraints

### 1. DawDreamer import order — MUST be first

DawDreamer uses LLVM internally (via Faust). If any LLVM-using library (JAX, PyTorch TorchScript, Numba) is imported before DawDreamer, the process will crash or produce corrupt audio with no clear error message.

**Stdlib imports (pathlib, os, logging, etc.) are safe at module level and do not conflict with DawDreamer.** Only ML/LLVM-adjacent third-party libraries need to be deferred. The rule is:

- Module-level: `from pathlib import Path`, `import logging`, `import os` — all fine
- Deferred (inside `init_worker`, as first statement): `import dawdreamer as daw`
- Deferred (inside `init_worker`, after dawdreamer): `import numpy`, `import soundfile`

```python
# worker.py — correct structure
from __future__ import annotations
from pathlib import Path       # stdlib — safe at module level
import logging                 # stdlib — safe at module level

logger = logging.getLogger("vst_render")

def init_worker(plugin_path: str, sample_rate: int) -> None:
    import dawdreamer as daw   # MUST be first non-stdlib import in this function
    import numpy as np         # after dawdreamer
    # ... rest of setup
```

This applies to `worker.py` (inside `init_worker`), `renderer.py` (if using DawDreamer), and any test that exercises the rendering path.

### 2. Never use threading with DawDreamer

Threading causes DawDreamer to hang after the first render completes. This is a confirmed bug in DawDreamer's JUCE internals — `insideVSTCallback` and related static variables are not thread-safe. **Use only `multiprocessing` or loky (which uses multiprocessing).** Do not use `threading.Thread`, `concurrent.futures.ThreadPoolExecutor`, or `asyncio` with DawDreamer calls.

### 3. One RenderEngine per worker, created once. Both synths share the engine.

Never create multiple `RenderEngine` instances inside a loop. Creating engines in a loop causes thread explosion (GitHub Issue #88) and memory leaks (Issue #1). When both `.fxp` and `.SerumPreset` rendering is enabled, both synths live in the **same engine's graph** — the idle synth in a shared graph is byte-identical silence relative to a single-synth render (probe 1, verified). The correct pattern:

```python
# CORRECT — one engine, both synths loaded at worker startup, dispatch on job format
_engine = None
_synth_fxp = None
_synth_serum2 = None

def init_worker(fxp_plugin_path: str | None, serum2_plugin_path: str | None,
                sample_rate: int) -> None:
    if fxp_plugin_path is None and serum2_plugin_path is None:
        raise ValueError("init_worker requires at least one plugin path")
    import dawdreamer as daw
    global _engine, _synth_fxp, _synth_serum2
    _engine = daw.RenderEngine(sample_rate, 512)
    processors = []
    if fxp_plugin_path is not None:
        _synth_fxp = _engine.make_plugin_processor("fxp_synth", fxp_plugin_path)
        processors.append((_synth_fxp, []))
    if serum2_plugin_path is not None:
        _synth_serum2 = _engine.make_plugin_processor("serum2_synth", serum2_plugin_path)
        processors.append((_synth_serum2, []))
    _engine.load_graph(processors)

def render_task(job: dict) -> dict:
    # Dispatch on preset_format; the unused synth stays idle (silent in graph).
    if job["preset_format"] == "fxp":
        _synth_fxp.load_preset(job["preset_path"])
        synth = _synth_fxp
    else:  # "serum2"
        state = convert_preset_file(job["preset_path"])
        _serum_state_path.write_bytes(state)
        _synth_serum2.load_state(str(_serum_state_path))
        synth = _synth_serum2
    ...
```

### 4. All paths passed to DawDreamer must be absolute strings

`make_plugin_processor`, `load_preset`, `load_midi` all require absolute path strings. Relative paths silently fail. Always call `str(Path(p).resolve())` before passing paths to DawDreamer.

### 5. Never call `logging.basicConfig()` from library code

Only `cli.py` configures logging. All library modules use:

```python
import logging
logger = logging.getLogger("vst_render")
```

Never call `logging.basicConfig()`, `logging.setLevel()`, or add handlers anywhere outside `cli.py`.

### 6. No `if __name__ == '__main__'` guard required

loky uses cloudpickle, which handles Windows spawn correctly without requiring the `__main__` guard. Do not add it. If it appears anywhere in library code, remove it — it would indicate a regression to raw `multiprocessing`.

---

## loky worker skeleton

This is the complete pattern for `worker.py` and `batch.py`. Do not deviate from this structure.

### Job dict schema

Every job passed to `render_task` must have these keys. `batch.py` is responsible for populating all of them before submitting:

| Key | Type | Populated by | Notes |
|---|---|---|---|
| `preset_path` | `str` | `batch.py` | Absolute path string |
| `preset_format` | `str` | `batch.py` via `presets.PresetFormat` | `"fxp"` or `"serum2"`. Drives worker dispatch: `fxp` → `synth_fxp.load_preset(...)`, `serum2` → `synth_serum2.load_state(...)` after `serum2_preset_loader.convert_preset_file(...)`. |
| `filename_stem` | `str` | `batch.py` via `compose_filename()` | Intermediate stem before collision resolution; consumed and replaced by `assign_output_paths()` which writes `output_path` |
| `output_path` | `str` | `batch.py` via `assign_output_paths()` | Absolute path string; replaces `filename_stem` after collision resolution |
| `note` | `int` | `batch.py` from CLI args | Ignored if `midi_path` is set |
| `velocity` | `int` | `batch.py` from CLI args | |
| `duration` | `float` | `batch.py` from CLI args | Note-on duration in seconds |
| `tail` | `float` | `batch.py` from CLI args | Post-release silence in seconds |
| `midi_path` | `str \| None` | `batch.py` from CLI args | Absolute path string or None |
| `midi_duration` | `float \| None` | `batch.py` via `get_midi_duration()` | Computed once in main process, passed to all jobs; None if no MIDI file |
| `sample_rate` | `int` | `batch.py` from CLI args | |
| `bit_depth` | `str` | `batch.py` from CLI args | `"16"`, `"24"`, or `"32f"` |
| `format` | `str` | `batch.py` from CLI args | `"wav"` or `"npy"` |
| `skip_existing` | `bool` | `batch.py` from CLI args | |

`midi_duration` is computed **once in the main process** by `get_midi_duration(midi_path)` before any jobs are submitted, then passed through every job dict. Workers never call `get_midi_duration()`.

### worker.py

```python
# worker.py
from __future__ import annotations
from pathlib import Path    # stdlib — safe at module level
import logging
import tempfile

logger = logging.getLogger("vst_render")

# Module-level globals — populated by init_worker, reused by render_task
_engine = None
_synth_fxp = None
_synth_serum2 = None
_serum_state_path: Path | None = None  # per-worker tempfile for serum2 state blobs


def init_worker(fxp_plugin_path: str | None,
                serum2_plugin_path: str | None,
                sample_rate: int) -> None:
    """Called once per worker by loky. Builds one engine + one or both synths."""
    # Validate BEFORE the dawdreamer import so a unit test can exercise this
    # guard without paying the import cost (and without violating the
    # import-order constraint in test processes that already loaded numpy).
    if fxp_plugin_path is None and serum2_plugin_path is None:
        raise ValueError("init_worker requires at least one plugin path")

    import dawdreamer as daw  # MUST be first non-stdlib import
    import numpy as np        # imported here (after dawdreamer) to ensure load order

    global _engine, _synth_fxp, _synth_serum2, _serum_state_path
    _engine = daw.RenderEngine(sample_rate, 512)

    processors = []
    if fxp_plugin_path is not None:
        _synth_fxp = _engine.make_plugin_processor(
            "fxp_synth", str(Path(fxp_plugin_path).resolve())
        )
        processors.append((_synth_fxp, []))
    if serum2_plugin_path is not None:
        _synth_serum2 = _engine.make_plugin_processor(
            "serum2_synth", str(Path(serum2_plugin_path).resolve())
        )
        processors.append((_synth_serum2, []))
    _engine.load_graph(processors)

    # Per-worker tempfile for the serum2 state.bin round-trip. Reused for
    # every serum2 job — write_bytes overwrites in place.
    _serum_state_path = Path(tempfile.mkdtemp(prefix="vst_render_serum2_")) / "state.bin"

    # Warmup render: Serum 2 lazy-loads sample data on first render and the
    # cold render comes out at ~10x steady-state level. A 0.1s render here
    # absorbs that anomaly inside init so the user's first job is correct.
    for synth in (_synth_fxp, _synth_serum2):
        if synth is None:
            continue
        synth.clear_midi()
        synth.add_midi_note(48, 127, 0.0, 0.05)
        _engine.render(0.1)


def render_task(job: dict) -> dict:
    """
    Render one preset. Dispatches on job["preset_format"]: "fxp" -> load_preset,
    "serum2" -> convert_preset_file + load_state. Required job keys: see the
    job dict schema in CLAUDE.md.
    """
    import numpy as np

    preset_path: str = job["preset_path"]
    output_path: str = job["output_path"]

    try:
        if job["skip_existing"] and Path(output_path).exists():
            return {"status": "skipped", "path": preset_path}

        fmt = job["preset_format"]
        if fmt == "fxp":
            if _synth_fxp is None:
                raise RuntimeError("worker has no fxp synth")
            synth = _synth_fxp
            synth.load_preset(preset_path)
        elif fmt == "serum2":
            if _synth_serum2 is None:
                raise RuntimeError("worker has no serum2 synth")
            from serum2_preset_loader import convert_preset_file  # deferred — worker contract
            synth = _synth_serum2
            _serum_state_path.write_bytes(convert_preset_file(preset_path))
            synth.load_state(str(_serum_state_path))
        else:
            raise ValueError(f"Unknown preset_format: {fmt!r}")

        if job["midi_path"] is not None:
            synth.load_midi(job["midi_path"], clear_previous=True,
                            beats=False, all_events=True)
            render_duration = job["midi_duration"] + job["tail"]
        else:
            synth.clear_midi()
            synth.add_midi_note(job["note"], job["velocity"], 0.0, job["duration"])
            render_duration = job["duration"] + job["tail"]

        _engine.render(render_duration)
        audio = _engine.get_audio()  # (2, N) float32

        # Silent output check: -90 dBFS ≈ 16-bit quantization floor
        if np.max(np.abs(audio)) < 3.16e-5:
            logger.warning("Silent output for preset: %s", preset_path)

        _write_audio(audio, output_path, job["sample_rate"],
                     job["bit_depth"], job["format"])

        return {"status": "ok", "path": preset_path}

    except Exception as exc:
        logger.warning("Failed to render %s: %s", preset_path, exc)
        return {"status": "error", "path": preset_path, "error": str(exc)}


def _write_audio(audio, output_path: str, sample_rate: int,
                 bit_depth: str, fmt: str) -> None:
    import numpy as np
    import soundfile as sf

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if fmt == "npy":
        np.save(output_path, audio)
    else:
        subtype_map = {"16": "PCM_16", "24": "PCM_24", "32f": "FLOAT"}
        sf.write(output_path, audio.T, sample_rate,
                 subtype=subtype_map[bit_depth])
```

### batch.py

`run_batch_to_disk` (CLI path) and `iter_batch_to_memory` (library path) both go through the private `_get_executor` helper, which is the single point that hands `init_worker` to loky. Callers from `api.py` and `cli.py` use the public functions; nothing calls the executor directly.

```python
# batch.py — loky executor management
from __future__ import annotations
import os
import logging
from concurrent.futures import as_completed
from loky import get_reusable_executor
from .worker import init_worker, render_to_disk, render_to_memory

logger = logging.getLogger("vst_render")


def resolve_worker_count(workers: int) -> int:
    if workers == -1:
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, workers)


def _get_executor(workers: int, fxp_plugin_path: str | None,
                  serum2_plugin_path: str | None, sample_rate: int):
    """Either plugin path may be None; init_worker requires at least one set.
    timeout=1800: workers idle for 30 minutes are shut down. Lower values
    cause unexpected cold starts for interactive/long-running callers."""
    return get_reusable_executor(
        max_workers=resolve_worker_count(workers),
        initializer=init_worker,
        initargs=(fxp_plugin_path, serum2_plugin_path, sample_rate),
        timeout=1800,
    )


def run_batch_to_disk(jobs, workers, fxp_plugin_path, serum2_plugin_path,
                      sample_rate, on_result=None) -> list[dict]:
    """CLI entry: submit every job, return results in input order.
    Per-job errors become `{"status": "error", ...}` dicts."""
    executor = _get_executor(workers, fxp_plugin_path, serum2_plugin_path, sample_rate)
    futures = {executor.submit(render_to_disk, job): idx for idx, job in enumerate(jobs)}
    results: list[dict | None] = [None] * len(jobs)
    for future in as_completed(futures):
        idx = futures[future]
        try:
            result = future.result()
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            result = {"status": "error", "path": jobs[idx].get("preset_path"),
                      "error": str(exc)}
        results[idx] = result
        if on_result is not None:
            on_result(result)
    return results
```

---

## CLI dispatch and validation

The CLI is **format-driven**, not plugin-driven: the user names the formats they're rendering via `--fxp` and `--serum2`, and the worker pool is wired with whichever subset matches. The four checks below are all in `cli.py` and run in this order:

1. **At least one of `--fxp` / `--serum2` must be set** (else exit 2). Allowing neither would just bottom out in `init_worker`'s ValueError later — front-load the error.
2. **Each provided plugin path must exist** (else exit 2). `Path.exists()` is the right check — VST3 bundles on macOS are directories, plain `.dll` / `.vst3` / `.vst` are files; both are valid.
3. **Discovered preset formats must be a subset of provided plugin formats.** A directory of `.fxp` files passed without `--fxp`, or `.SerumPreset` files without `--serum2`, must fail at start-up rather than mid-batch. The error names the missing flag, e.g. `found .SerumPreset files but --serum2 was not provided`.
4. **`--note` vs `--midi` mutual exclusion.** Typer cannot distinguish a user-supplied `--note 48` from the default `48`, so `note` defaults to `None` as a sentinel; the mutual-exclusion check uses `note is not None`.

```python
# cli.py — pattern only, see vst_render/cli.py for the real version
import typer
from typing import Optional
from pathlib import Path

app = typer.Typer()

@app.command()
def render(
    presets: Path = typer.Argument(...),
    output: Path = typer.Argument(...),
    fxp: Optional[Path] = typer.Option(None, "--fxp"),
    serum2: Optional[Path] = typer.Option(None, "--serum2"),
    note: Optional[int] = typer.Option(None, help="MIDI note (0-127). Default: 48 (C3)."),
    midi: Optional[Path] = typer.Option(None, help="Path to .mid file."),
    # ... other options
):
    # check 1
    if fxp is None and serum2 is None:
        typer.echo("At least one of --fxp or --serum2 is required.", err=True)
        raise typer.Exit(code=2)
    # check 4 — sentinel pattern; resolve default afterwards
    if midi is not None and note is not None:
        raise typer.BadParameter("--note and --midi are mutually exclusive.")
    if note is None:
        note = 48
```

Note: `typer.Context` is injected by parameter type annotation alone (no `typer.Option` wrapper). It is not needed here since the format-validation checks only use `fxp`, `serum2`, `note`, and `midi`.

---

## Filename template implementation

The template composition lives in `utils.py`. Key implementation notes:

```python
# utils.py
import re
from pathlib import Path


def sanitize(value: str) -> str:
    """
    Sanitize a single template variable value.
    Keeps only alphanumeric chars, hyphens, and underscores — everything else
    (including spaces) is replaced with '_'. Spaces are not preserved because
    filenames with literal spaces are awkward on the Windows CLI.
    """
    value = value.strip()
    value = re.sub(r'[^A-Za-z0-9_-]', '_', value)  # replace non-spec chars including spaces
    value = re.sub(r'_+', '_', value)                # collapse runs of underscores
    value = value.strip('_')
    return value


def compose_filename(
    template: str,
    preset_path: Path,
    presets_root: Path | None,   # None when PRESETS is a single file
    note: int,
    velocity: int,
) -> str:
    """
    Compose a filename stem from a template.
    Returns the stem only (no extension), truncated to 196 chars.
    Collision suffix (_1, _2, ...) is appended by assign_output_paths().
    Operations: sanitize each var → substitute → collapse separators → truncate to 196.
    """
    preset = sanitize(preset_path.stem)
    folder = sanitize(preset_path.parent.name)

    if presets_root is not None:
        try:
            rel = preset_path.parent.relative_to(presets_root)
            subpath = sanitize("_".join(rel.parts)) if rel.parts else ""
        except ValueError:
            subpath = ""
    else:
        subpath = ""  # single-file mode: no root to relativize against

    result = template
    result = result.replace("{preset}", preset)
    result = result.replace("{note}", str(note))
    result = result.replace("{velocity}", str(velocity))
    result = result.replace("{folder}", folder)
    result = result.replace("{subpath}", subpath)

    # Collapse separators introduced by empty {subpath} substitution
    result = re.sub(r'_+', '_', result).strip('_')

    # Truncate to 196 chars — leaves headroom for _1 through _999 collision suffixes
    # (total filename including extension stays under 200 chars)
    # Note: this caps the FILENAME only, not the full path. Windows MAX_PATH is 260
    # chars for the full path; users with deeply nested output dirs may still hit it.
    return result[:196]


def assign_output_paths(
    jobs: list[dict],
    output_dir: Path,
    extension: str,
) -> list[dict]:
    """
    Assign unique output paths to all jobs, disambiguating collisions.
    Modifies jobs in-place and returns them.
    Collision suffixes _1.._999 fit within the 196-char stem truncation headroom;
    no re-truncation is needed in practice.
    """
    seen: dict[str, int] = {}
    for job in jobs:
        stem = job["filename_stem"]
        if stem not in seen:
            seen[stem] = 0
            final_stem = stem
        else:
            seen[stem] += 1
            final_stem = f"{stem}_{seen[stem]}"
        job["output_path"] = str(output_dir / f"{final_stem}{extension}")
    return jobs
```

---

## MIDI duration helper

```python
# utils.py (continued)
import mido


def get_midi_duration(midi_path: Path) -> float:
    """
    Return total playback duration of a MIDI file in seconds.
    Raises TypeError for Type 2 (asynchronous) files.
    Raises ValueError for files that cannot be parsed.
    """
    try:
        mid = mido.MidiFile(str(midi_path))
    except Exception as exc:
        raise ValueError(f"Could not parse MIDI file '{midi_path}': {exc}") from exc

    if mid.type == 2:
        raise TypeError(
            f"MIDI file '{midi_path}' is Type 2 (asynchronous) and duration cannot "
            f"be determined. Convert it to Type 0 or Type 1 using your DAW or a "
            f"tool like mido before rendering."
        )

    return mid.length  # tempo-aware, returns float seconds
```

---

## Testing

### Structure

```
tests/
├── conftest.py              # fxp_plugin_path / serum2_plugin_path / preset_files / serum_preset_files fixtures
├── test_sanitize.py         # sanitize(), compose_filename()
├── test_filename.py         # assign_output_paths(), collision handling, {subpath} edge cases
├── test_presets.py          # discover_presets() — both formats, single file vs directory, --no-recurse
├── test_midi.py             # get_midi_duration(), Type 2 error, valid files
├── test_worker.py           # _do_render dispatch + format guards (no plugin required, mocked synth)
├── test_parallel_smoke.py   # integration: .fxp end-to-end (regression)
└── test_serum2_smoke.py     # integration: serum2-only + mixed-format acceptance gate
```

### Plugin/preset availability fixtures

Each fixture is gated independently on its own option/env-var pair, so a user with only one plugin still runs the smoke half they have plumbing for.

```python
# conftest.py
import pytest, os
from pathlib import Path

def pytest_addoption(parser):
    parser.addoption("--fxp-plugin-path", default=None,
        help="Path to a Serum 1 plugin (loads .fxp).")
    parser.addoption("--serum2-plugin-path", default=None,
        help="Path to the Serum 2 VST3 (loads .SerumPreset).")
    parser.addoption("--preset-dir", default=None,
        help="Directory containing .fxp presets.")
    parser.addoption("--serum-preset-dir", default=None,
        help="Directory containing .SerumPreset files.")

# Each fixture skips with a hint if its option / env var pair is unset.
# Env vars: VST_FXP_PLUGIN_PATH, VST_SERUM2_PLUGIN_PATH, VST_PRESET_DIR,
#           VST_SERUM_PRESET_DIR.
```

Run integration tests:
```bash
pytest tests/test_parallel_smoke.py tests/test_serum2_smoke.py \
    --fxp-plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2-plugin-path "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \
    --preset-dir "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets/Misc" \
    --serum-preset-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets/Factory/Piano"
```

Run unit tests only (no plugin required):
```bash
pytest tests/ --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py
```

### Smoke test outlines

`test_parallel_smoke.py` exercises the public library API (`.fxp`-only):
```python
@pytest.mark.slow
def test_parallel_render_produces_audio(fxp_plugin_path, preset_files):
    config = RenderConfig(
        fxp_plugin_path=fxp_plugin_path, sample_rate=44100,
        note=48, velocity=127, duration=1.0, tail=1.0,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(preset_files)
    # ... non-silent / stereo / float32 assertions
```

`test_serum2_smoke.py` drives `run_batch_to_disk` directly (the public library API is gated to fxp-only at 0.2.0; `run_batch_to_disk` is the path the CLI uses and is the only entry that accepts mixed-format batches):
```python
@pytest.mark.slow
def test_mixed_format_smoke(fxp_plugin_path, serum2_plugin_path,
                            preset_files, serum_preset_files, tmp_path):
    jobs = [
        _make_job(preset_path=preset_files[0], preset_format="fxp",  output_path=...),
        _make_job(preset_path=serum_preset_files[0], preset_format="serum2", output_path=...),
    ]
    results = run_batch_to_disk(
        jobs=jobs, workers=2,
        fxp_plugin_path=fxp_plugin_path,
        serum2_plugin_path=serum2_plugin_path,
        sample_rate=44100,
    )
    # ... per-job status==ok + stereo / non-silent assertions
```

---

## Public API exports (`__init__.py`)

```python
# vst_render/__init__.py
from .config import RenderConfig
from .api import BatchRenderer, ParallelBatchRenderer, render_preset

__all__ = [
    "RenderConfig",
    "BatchRenderer",
    "ParallelBatchRenderer",
    "render_preset",
]
```

---

## Explicit don'ts

- **Don't use `threading.Thread`, `ThreadPoolExecutor`, or `asyncio` with DawDreamer calls.** Threading crashes DawDreamer after the first render.
- **Don't create multiple `RenderEngine` instances per worker or inside a loop.** One engine per worker process, created once in `init_worker`, reused forever.
- **Don't pass relative paths to DawDreamer.** Always `str(Path(p).resolve())`.
- **Don't call `logging.basicConfig()` from any library module.** Only `cli.py` configures logging.
- **Don't import DawDreamer at module level in `worker.py`.** Import it inside `init_worker()` as the first statement.
- **Don't add `if __name__ == '__main__'` guards.** loky handles Windows spawn correctly without them; adding them signals a regression to raw multiprocessing.
- **Don't call `engine.load_graph()` on every render.** Build the graph once in `init_worker`; `load_preset()` updates the processor state in-place.
- **Don't use VST3 `.dll` or `.vst3` paths with `.fxp` presets.** DawDreamer silently ignores `.fxp` when loaded as VST3 — no error, just wrong output.
- **Don't assume a `.dll` in the VST2 folder is 64-bit.** On Windows, Serum's installer puts the 32-bit VST2 at `C:/Program Files/Common Files/VST2/Serum.dll` and the 64-bit VST2 at `C:/Program Files/Common Files/VST3/Serum_x64.dll` (yes, a VST2 `.dll` in the `VST3/` folder — the actual VST3 is the adjacent `Serum.vst3` bundle). Loading a 32-bit DLL from 64-bit Python raises `OSError [WinError 193] %1 is not a valid Win32 application`; DawDreamer surfaces this as `RuntimeError: Unable to load plugin.` with no hint as to why. Point users at the `_x64.dll`.
- **Don't pass the raw `.SerumPreset` bytes to `synth.load_state`.** The on-disk file is the wrapped form (cbor2 + zstandard); `load_state` wants the inner state. Always go through `serum2_preset_loader.convert_preset_file()` first.
- **Don't import `serum2_preset_loader` at module level.** It's pure-Python (no LLVM), so the import-order constraint isn't load-bearing — but `worker.py`'s contract is "stdlib only at module level, everything else deferred". Adding a non-stdlib top-level import here erodes a guarantee that `init_worker` validation tests rely on. Defer it inside `init_worker` and `_do_render`.
- **Don't share the serum2 `state.bin` path across workers.** Each loky worker creates its own `mkdtemp` directory in `init_worker`. Sharing one path means workers stomp on each other's writes mid-render. The path lives in a per-worker module global by design.
- **Don't drop the warmup render in `init_worker`.** Serum 2 lazy-loads sample data on first render; without the warmup, the first job in each worker comes out at ~10× steady-state level. Remove it only if you have direct evidence the upstream lazy-load is gone.

---

## Verified architectural findings

The three assumptions below were verified against real Serum before the rest of the package was built. The harness is checked in at `scripts/verify_dawdreamer.py` — run it if DawDreamer is upgraded or if a different VST2 plugin is added to the supported list.

1. **`load_preset()` on an already-loaded graph updates in place — verified.** Two sequential presets on one engine produce distinct non-silent audio. No need to rebuild the graph between renders.

2. **`load_preset()` on a missing file raises `RuntimeError` — verified.** The message is descriptive (`Error: (PluginProcessor::loadPreset) File not found: <path>`) and the engine stays usable for subsequent good loads. `render_task`'s `except Exception` block handles it cleanly; no pre-check needed.

3. **loky crash recovery — partially as expected; one nuance.** A worker killed via `os._exit` surfaces as `TerminatedWorkerError` on the future in ~60 ms (no hang). But the **executor reference itself is permanently flagged broken** — any further `submit()` on the same `executor` variable raises immediately. Recovery requires calling `get_reusable_executor(...)` again to obtain a freshly spawned pool. DESIGN.md's "redistribute unfinished jobs to surviving workers" wording is optimistic: loky does not redistribute; the reusable-executor API just respawns on the next call.

   **Implication for `batch.py`:** `_run_batch` as skeletoned above submits everything upfront and collects futures. On a single-worker crash every subsequent future on that executor also raises — each is logged as an error result and the batch proceeds (returning error dicts for the lost jobs). Users re-run with `--skip-existing` to pick up the remainder. A more ambitious implementation could call `get_reusable_executor()` again mid-batch and resubmit unfinished jobs; that's deferred until a user hits it.
