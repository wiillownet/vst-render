# CLAUDE.md — fxp-render Implementation Guide

This file is the implementation context for Claude Code. Read `DESIGN.md` first for the full specification. This document covers the *how*: build order, exact API calls, code skeletons, and constraints that the design doc omits because they're not relevant to a human reader.

---

## Project at a glance

- **Name:** `fxp-render` (package: `fxp_render`, CLI command: `fxp-render`)
- **Purpose:** Batch render VST2 `.fxp` presets to audio files using DawDreamer as the headless engine
- **Platform:** Windows only (VST2 `.dll` format)
- **License:** GPLv3 (inherited from DawDreamer)
- **Python:** 3.11–3.13

---

## Build order

Build files in this sequence. Each stage has no dependency on the next:

1. `pyproject.toml` — package metadata and entry point
2. `fxp_render/__init__.py` — public API exports
3. `fxp_render/config.py` — `RenderConfig` dataclass (pure Python, no DawDreamer)
4. `fxp_render/utils.py` — filename sanitization, template composition, MIDI helpers (pure Python)
5. `fxp_render/presets.py` — preset discovery (pure Python, pathlib only)
6. `fxp_render/renderer.py` — single-preset render logic (uses DawDreamer)
7. `fxp_render/worker.py` — loky worker init and task function (uses DawDreamer)
8. `fxp_render/batch.py` — job construction, loky executor management, progress reporting
9. `fxp_render/api.py` — `BatchRenderer`, `ParallelBatchRenderer`, `render_preset()` public classes
10. `fxp_render/cli.py` — Typer app wiring everything together

Write tests alongside each module, not after. Tests for stages 3–5 require no plugin installed. Tests for stages 6–10 are gated behind the plugin availability fixture (see Testing section).

---

## pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fxp-render"
version = "0.1.0"
requires-python = ">=3.11,<3.14"
dependencies = [
    "dawdreamer",
    "soundfile",
    "typer",
    "loky",
    "mido",
    "numpy",
]

[project.scripts]
fxp-render = "fxp_render.cli:app"

[project.optional-dependencies]
dev = ["pytest", "pytest-mock"]

[tool.pytest.ini_options]
markers = ["slow: integration tests that require a plugin to be installed"]
```

The entry point `fxp-render = "fxp_render.cli:app"` wires the `fxp-render` command to the Typer app instance named `app` in `cli.py`.

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

### Preset loading

```python
# Load an .fxp preset — call this between renders, no need to rebuild graph
synth.load_preset(str(fxp_path.resolve()))  # must be absolute path string
```

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

logger = logging.getLogger("fxp_render")

def init_worker(plugin_path: str, sample_rate: int) -> None:
    import dawdreamer as daw   # MUST be first non-stdlib import in this function
    import numpy as np         # after dawdreamer
    # ... rest of setup
```

This applies to `worker.py` (inside `init_worker`), `renderer.py` (if using DawDreamer), and any test that exercises the rendering path.

### 2. Never use threading with DawDreamer

Threading causes DawDreamer to hang after the first render completes. This is a confirmed bug in DawDreamer's JUCE internals — `insideVSTCallback` and related static variables are not thread-safe. **Use only `multiprocessing` or loky (which uses multiprocessing).** Do not use `threading.Thread`, `concurrent.futures.ThreadPoolExecutor`, or `asyncio` with DawDreamer calls.

### 3. One RenderEngine per worker, created once

Never create multiple `RenderEngine` instances inside a loop. Creating engines in a loop causes thread explosion (GitHub Issue #88) and memory leaks (Issue #1). The correct pattern:

```python
# CORRECT — one engine, created once at worker startup
_engine = None
_synth = None

def init_worker(plugin_path: str, sample_rate: int) -> None:
    import dawdreamer as daw
    global _engine, _synth
    _engine = daw.RenderEngine(sample_rate, 512)
    _synth = _engine.make_plugin_processor("plugin", plugin_path)
    _engine.load_graph([(_synth, [])])

def render_task(job: dict) -> dict:
    # reuses _engine and _synth — no recreation
    _synth.load_preset(job["preset_path"])
    ...
```

### 4. All paths passed to DawDreamer must be absolute strings

`make_plugin_processor`, `load_preset`, `load_midi` all require absolute path strings. Relative paths silently fail. Always call `str(Path(p).resolve())` before passing paths to DawDreamer.

### 5. Never call `logging.basicConfig()` from library code

Only `cli.py` configures logging. All library modules use:

```python
import logging
logger = logging.getLogger("fxp_render")
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

logger = logging.getLogger("fxp_render")

# Module-level globals — populated by init_worker, reused by render_task
_engine = None
_synth = None


def init_worker(plugin_path: str, sample_rate: int) -> None:
    """Called once per worker process by loky. Sets up DawDreamer engine."""
    import dawdreamer as daw  # MUST be first non-stdlib import
    import numpy as np        # imported here (after dawdreamer) to ensure load order
                              # even though this function doesn't use numpy directly
    global _engine, _synth
    # plugin_path is resolved to absolute by init_worker — DawDreamer silently fails with relative paths
    resolved = str(Path(plugin_path).resolve())
    _engine = daw.RenderEngine(sample_rate, 512)
    _synth = _engine.make_plugin_processor("plugin", resolved)
    _engine.load_graph([(_synth, [])])
    logger.debug("Worker initialized with plugin: %s", resolved)


def render_task(job: dict) -> dict:
    """
    Render one preset. Called by loky worker. Returns result dict.

    Required job keys: preset_path, output_path, note, velocity, duration,
    tail, midi_path, midi_duration, sample_rate, bit_depth, format, skip_existing.
    See job dict schema in CLAUDE.md.
    """
    import numpy as np

    preset_path: str = job["preset_path"]
    output_path: str = job["output_path"]

    try:
        if job["skip_existing"] and Path(output_path).exists():
            return {"status": "skipped", "path": preset_path}

        _synth.load_preset(preset_path)

        if job["midi_path"] is not None:
            _synth.load_midi(job["midi_path"], clear_previous=True,
                             beats=False, all_events=True)
            render_duration = job["midi_duration"] + job["tail"]
        else:
            _synth.clear_midi()
            _synth.add_midi_note(job["note"], job["velocity"], 0.0, job["duration"])
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

`_run_batch` is a private helper function (underscore-prefixed per Python convention). `ParallelBatchRenderer` in `api.py` wraps `_run_batch` and manages the executor lifecycle via the context manager protocol (`__enter__`/`__exit__`). Callers never call `_run_batch` directly.

```python
# batch.py — loky executor management
from __future__ import annotations
import os
import logging
from pathlib import Path
from loky import get_reusable_executor
from .worker import init_worker, render_task

logger = logging.getLogger("fxp_render")


def resolve_worker_count(workers: int) -> int:
    if workers == -1:
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, workers)


def _run_batch(jobs: list[dict], workers: int, plugin_path: str,
               sample_rate: int) -> list[dict]:
    """Private helper. Submit jobs to loky worker pool, return results.
    Called by ParallelBatchRenderer — do not call directly.
    Path resolution for plugin_path is handled inside init_worker."""
    n_workers = resolve_worker_count(workers)
    # timeout=1800: workers idle for 30 minutes are shut down.
    # Lower values cause unexpected cold starts for interactive/long-running callers.
    executor = get_reusable_executor(
        max_workers=n_workers,
        initializer=init_worker,
        initargs=(plugin_path, sample_rate),
        timeout=1800,
    )
    futures = [executor.submit(render_task, job) for job in jobs]
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            results.append({"status": "error", "error": str(exc)})
    return results
```

---

## CLI mutual exclusion pattern (`--note` vs `--midi`)

Typer cannot distinguish a user-supplied `--note 48` from the default `48`. Use `None` as the sentinel default. The mutual exclusion check only needs `note` and `midi` — no Click context required:

```python
# cli.py
import typer
from typing import Optional
from pathlib import Path

app = typer.Typer()

@app.command()
def render(
    plugin: Path = typer.Argument(...),
    presets: Path = typer.Argument(...),
    output: Path = typer.Argument(...),
    note: Optional[int] = typer.Option(None, help="MIDI note (0-127). Default: 48 (C3)."),
    midi: Optional[Path] = typer.Option(None, help="Path to .mid file."),
    # ... other options
):
    # Manual mutual exclusion check — Typer cannot do this automatically
    if midi is not None and note is not None:
        raise typer.BadParameter(
            "--note and --midi are mutually exclusive. "
            "Use --midi to render a MIDI sequence, or --note to render a single note."
        )
    # Resolve default after exclusion check
    if note is None:
        note = 48
```

Note: `typer.Context` is injected by parameter type annotation alone (no `typer.Option` wrapper). It is not needed here since the check only uses `note` and `midi`.

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
├── conftest.py              # shared fixtures including plugin_available skip
├── test_sanitize.py         # sanitize(), compose_filename()
├── test_filename.py         # assign_output_paths(), collision handling, {subpath} edge cases
├── test_presets.py          # discover_presets(), single file vs directory, --no-recurse
├── test_midi.py             # get_midi_duration(), Type 2 error, valid files
└── test_parallel_smoke.py   # integration: end-to-end render, requires plugin
```

### Plugin availability fixture

```python
# conftest.py
import pytest
import os
from pathlib import Path

def pytest_addoption(parser):
    parser.addoption(
        "--plugin-path",
        action="store",
        default=None,
        help="Path to Serum VST2 .dll for integration tests",
    )
    parser.addoption(
        "--preset-dir",
        action="store",
        default=None,
        help="Directory containing .fxp presets for integration tests",
    )

@pytest.fixture
def plugin_path(request):
    path = request.config.getoption("--plugin-path") or os.environ.get("FXP_PLUGIN_PATH")
    if not path:
        pytest.skip("No plugin path provided. Set --plugin-path or FXP_PLUGIN_PATH env var.")
    return str(Path(path).resolve())

@pytest.fixture
def preset_files(request):
    """Returns a list of 2 .fxp preset paths for smoke tests."""
    preset_dir = request.config.getoption("--preset-dir") or os.environ.get("FXP_PRESET_DIR")
    if not preset_dir:
        pytest.skip("No preset dir provided. Set --preset-dir or FXP_PRESET_DIR env var.")
    files = sorted(Path(preset_dir).glob("*.fxp"))[:2]
    if len(files) < 2:
        pytest.skip(f"Need at least 2 .fxp files in preset dir, found {len(files)}.")
    return [str(f) for f in files]
```

Run integration tests:
```bash
pytest tests/test_parallel_smoke.py \
    --plugin-path "C:/VSTPlugins/Serum.dll" \
    --preset-dir "C:/Serum Presets/Leads/"
# or via env vars
FXP_PLUGIN_PATH="C:/VSTPlugins/Serum.dll" FXP_PRESET_DIR="C:/Presets/" pytest tests/test_parallel_smoke.py
```

Run unit tests only (no plugin required):
```bash
pytest tests/ --ignore=tests/test_parallel_smoke.py
```

### Smoke test outline

```python
# test_parallel_smoke.py
import pytest
import numpy as np
from fxp_render import ParallelBatchRenderer, RenderConfig

@pytest.mark.slow
def test_parallel_render_produces_audio(plugin_path, preset_files):
    """Smoke test: render 2 presets in parallel, verify non-silent stereo output."""
    config = RenderConfig(
        plugin_path=plugin_path,
        sample_rate=44100,
        note=48,
        velocity=127,
        duration=1.0,
        tail=1.0,
    )
    with ParallelBatchRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(preset_files)

    assert len(results) == len(preset_files)
    for fxp_path, audio in results.items():
        assert audio.shape[0] == 2              # stereo
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5  # not silent
```

---

## Public API exports (`__init__.py`)

```python
# fxp_render/__init__.py
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

---

## Likely implementation gotchas to verify early

These assumptions are correct to the best of our knowledge but should be verified with a real Serum render before building anything that depends on them:

1. **`load_preset()` on an already-loaded graph works without rebuilding.** The design assumes you can call `synth.load_preset()` between renders without calling `engine.load_graph()` again. Verify this with a two-preset sequential render in a scratch script as the very first thing you do.

2. **loky `BrokenProcessPool` redistribution catches wedged plugin instances.** If Serum enters a bad state mid-render, loky should surface this as a `BrokenProcessPool` exception on the future, allowing the job to be resubmitted to a new worker. Verify this behaves as expected rather than hanging indefinitely.

3. **`load_preset()` on a nonexistent file doesn't wedge the worker pool.** DawDreamer's error handling for a missing `.fxp` path is plugin-specific. Verify that calling `_synth.load_preset("nonexistent.fxp")` raises a catchable Python exception (so `render_task`'s `except` block handles it cleanly) rather than crashing the worker process silently. Test by passing a deliberately bad path early.

If any assumption is wrong, the fix is small but the architecture depends on it — better to know immediately.
