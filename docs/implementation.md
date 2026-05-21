# Implementation guide

Implementation context for Claude Code and contributors. Together with `CLAUDE.md` this file is the live specification — read `CLAUDE.md` first for the project overview, then this file for the constraints and API contracts that the high-level doc omits. `DESIGN.md` is preserved as the original v1 design rationale, not current truth.

The actual code in `vst_render/` is the source of truth for module structure and function signatures — this doc no longer mirrors module skeletons. When in doubt, read the file.

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

`.SerumPreset` files are JUCE `IComponent` state blobs (cbor2 + zstandard wrapper around the raw VST3 state). DawDreamer can load the inner state via `synth.load_state(path)`, but the file on disk is the wrapped form that the plugin's preset browser reads — not what `load_state` accepts.

The `serum2_preset_loader.convert_preset_file()` helper performs the wrapper unwrap + cbor2 decode + zstd inflate and returns the raw state as `bytes`. `load_state` takes a path, not bytes, so the worker writes the bytes to a per-worker tempfile and passes that path:

```python
from serum2_preset_loader import convert_preset_file

state_bytes = convert_preset_file(serum_preset_path)  # returns bytes
state_path = Path(tempfile.mkdtemp(prefix="vst_render_serum2_")) / "state.bin"
state_path.write_bytes(state_bytes)
synth.load_state(str(state_path))  # must be absolute path string
```

Reuse the same `state_path` for every job in the worker — `write_bytes` overwrites in place, and the previous job's blob is never re-read. Do not share `state_path` across workers; each loky worker creates its own `mkdtemp` directory at init time.

### MIDI — single note mode

```python
synth.clear_midi()
# (note: int, velocity: int, start_time: float, duration: float); seconds, not beats
synth.add_midi_note(48, 127, 0.0, 1.0)
```

### MIDI — file mode

```python
synth.load_midi(
    str(midi_path.resolve()),  # must be absolute path string
    clear_previous=True,
    beats=False,               # use seconds, not beats
    all_events=True,           # include CC, pitch bend, etc.
)
```

### Rendering

```python
engine.render(2.0)              # duration in seconds
audio = engine.get_audio()      # (2, N) float32 numpy array

# soundfile expects (samples, channels) — transpose before writing:
import soundfile as sf
sf.write(str(output_path), audio.T, sample_rate, subtype="PCM_16")
```

### BPM (only needed if MIDI file uses beat-relative timing)

```python
engine.set_bpm(120.0)  # default is 120; only matters if beats=True
```

### Debug helpers

```python
num_params = synth.get_num_parameters()
names = [synth.get_parameter_name(i) for i in range(num_params)]
synth.open_editor()  # blocks until window is closed
```

`pyproject.toml` pins `serum2-preset-loader` to a **full 40-character commit SHA** — partial SHAs fail pip's clone with `error: pathspec '<short>' did not match any file(s) known to git`. Resolve a short SHA via `git log -1 --format="%H" <short-sha>` from the source repo.

---

## Critical constraints

### 1. DawDreamer import order — MUST be first

DawDreamer uses LLVM internally (via Faust). If any LLVM-using library (JAX, PyTorch TorchScript, Numba) is imported before DawDreamer, the process will crash or produce corrupt audio with no clear error message.

**Stdlib imports (pathlib, os, logging, etc.) are safe at module level and do not conflict with DawDreamer.** Only ML/LLVM-adjacent third-party libraries need to be deferred. The rule is:

- Module-level: `from pathlib import Path`, `import logging`, `import os` — all fine
- Deferred (inside `init_worker`, as first non-stdlib statement): `import dawdreamer as daw`
- Deferred (inside `init_worker`, after dawdreamer): `import numpy`, `import soundfile`

This applies to `worker.py` (inside `init_worker`), `renderer.py` (if using DawDreamer), and any test that exercises the rendering path.

### 2. Never use threading with DawDreamer

Threading causes DawDreamer to hang after the first render completes. This is a confirmed bug in DawDreamer's JUCE internals — `insideVSTCallback` and related static variables are not thread-safe. **Use only `multiprocessing` or loky (which uses multiprocessing).** Do not use `threading.Thread`, `concurrent.futures.ThreadPoolExecutor`, or `asyncio` with DawDreamer calls.

### 3. One RenderEngine per loaded format per worker, created once. Format engines do NOT share.

Never create `RenderEngine` instances inside a loop or per-job. Creating engines in a loop causes thread explosion (GitHub Issue #88) and memory leaks (Issue #1). When both `.fxp` and `.SerumPreset` rendering is enabled, each plugin gets its **own dedicated engine** — built once in `init_worker`, reused for every job in that format. Subsequent `load_preset()` / `load_state()` calls update processor state in place.

Earlier code put both synths into one engine's graph as orphan source nodes. That doesn't work: `engine.load_graph` with multiple orphan source processors and no terminal mixer routes only the last processor in the list to `get_audio()`; every other synth's output is silently discarded. The original "idle synth is byte-identical silence" probe was a tautology — yes, the idle synth was silent at the output, but so would the active synth have been if it weren't last. See `docs/audit-log.md` 2026-05-20 for the repro and the fix.

### 4. All paths passed to DawDreamer must be absolute strings

`make_plugin_processor`, `load_preset`, `load_midi` all require absolute path strings. Relative paths silently fail. Always call `str(Path(p).resolve())` before passing paths to DawDreamer.

### 5. Never call `logging.basicConfig()` from library code

Only `cli.py` configures logging. All library modules use `logger = logging.getLogger("vst_render")` and emit through that. No `basicConfig`, no `setLevel`, no handler attachment outside `cli.py`.

### 6. No `if __name__ == '__main__'` guard required

loky uses cloudpickle, which handles Windows spawn correctly without requiring the `__main__` guard. Do not add one. If it appears anywhere in library code, remove it — it would indicate a regression to raw `multiprocessing`.

---

## Worker job dict schema

`run_batch_to_disk` and `iter_batch_to_memory` consume a list of job dicts. The schema is a public seam: power users may build their own jobs and skip the public renderer classes. CLI builds these dicts; workers consume them.

| Key | Type | Notes |
|---|---|---|
| `preset_path` | `str` | Absolute path string |
| `preset_format` | `str` | `"fxp"` or `"serum2"`. Drives worker dispatch: `fxp` → `synth_fxp.load_preset(...)`, `serum2` → `synth_serum2.load_state(...)` after `serum2_preset_loader.convert_preset_file(...)`. |
| `filename_stem` | `str` | Intermediate stem before collision resolution; replaced by `assign_output_paths()` which writes `output_path` |
| `output_path` | `str` | Absolute path string |
| `note` | `int` | Ignored if `midi_path` is set |
| `velocity` | `int` | |
| `duration` | `float` | Note-on duration in seconds |
| `tail` | `float` | Post-release silence in seconds |
| `midi_path` | `str \| None` | Absolute path string or None |
| `midi_duration` | `float \| None` | Computed once in main process by `get_midi_duration(midi_path)`; None if no MIDI file. Workers never call `get_midi_duration()`. |
| `sample_rate` | `int` | |
| `bit_depth` | `str` | `"16"`, `"24"`, or `"32f"` |
| `format` | `str` | `"wav"` or `"npy"` |
| `skip_existing` | `bool` | |

The worker's inner dispatch helper (`_do_render` in `vst_render/worker.py`) validates that the matching synth exists for the job's `preset_format` and raises a clear `RuntimeError` / `ValueError` otherwise.

---

## CLI dispatch and validation

The CLI is **format-driven**, not plugin-driven: the user names the formats they're rendering via `--fxp` and `--serum2`, and the worker pool is wired with whichever subset matches. Four checks run in `cli.py`, in this order:

1. **At least one of `--fxp` / `--serum2` must be set** (else exit 2). Allowing neither would just bottom out in `init_worker`'s `ValueError` later — front-load the error.
2. **Each provided plugin path must exist** (else exit 2). `Path.exists()` is the right check — VST3 bundles on macOS are directories, plain `.dll` / `.vst3` / `.vst` are files; both are valid.
3. **Discovered preset formats must be a subset of provided plugin formats.** A directory of `.fxp` files passed without `--fxp`, or `.SerumPreset` files without `--serum2`, must fail at start-up rather than mid-batch. The error names the missing flag, e.g. `found .SerumPreset files but --serum2 was not provided`.
4. **`--note` vs `--midi` mutual exclusion.** Typer cannot distinguish a user-supplied `--note 48` from the default `48`, so `note` defaults to `None` as a sentinel; the mutual-exclusion check uses `note is not None`, then resolves the default afterwards.

---

## Public library API: format auto-detection

`BatchRenderer`, `ParallelBatchRenderer`, and `render_preset` accept both `.fxp` and `.SerumPreset` paths. Format dispatch is auto-detected from the path suffix via `presets.format_for_path`; the caller does **not** mark each path with its format.

The validation surface mirrors the CLI:

1. `RenderConfig.__post_init__` rejects a config with neither plugin path set.
2. `_validate_paths` (called from `__enter__` / `render_preset`) checks that any plugin path the caller did set actually exists on disk.
3. `_check_required_plugins` (called per-render or per-batch) checks that every format in the actual preset list has its matching plugin path on the config — e.g. a batch containing `.SerumPreset` paths with `serum2_plugin_path=None` fails fast with a clear error before any worker boots.

Error messages name the missing field (`serum2_plugin_path`, `fxp_plugin_path`) rather than the CLI flag, since at this layer the user is calling Python:

```python
ValueError: .SerumPreset preset(s) supplied but RenderConfig.serum2_plugin_path is unset
```

`renderer.py` mirrors `worker.py`'s dual-synth init — `make_engine(fxp_plugin_path, serum2_plugin_path, sample_rate)` returns an `Engine` dataclass with both synth slots and the per-engine `serum_state_path` tempfile. The same warmup-render loop runs at engine build to absorb Serum 2's first-render anomaly. Don't drop it.

---

## Testing

### Fixtures

`tests/conftest.py` declares four pytest options, each gated independently so a user with only one plugin still runs the half they have plumbing for:

- `--fxp-plugin-path` (env: `VST_FXP_PLUGIN_PATH`) — Serum 1 plugin loading `.fxp`
- `--serum2-plugin-path` (env: `VST_SERUM2_PLUGIN_PATH`) — Serum 2 VST3 loading `.SerumPreset`
- `--preset-dir` (env: `VST_PRESET_DIR`) — directory of `.fxp` presets
- `--serum-preset-dir` (env: `VST_SERUM_PRESET_DIR`) — directory of `.SerumPreset` files

Each fixture skips with a hint if its option / env-var pair is unset.

### Test commands

Fast unit tests (no plugin required):
```bash
pytest tests/ --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py
```

Integration smokes (one or both plugins installed):
```bash
pytest tests/test_parallel_smoke.py tests/test_serum2_smoke.py \
    --fxp-plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2-plugin-path "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \
    --preset-dir "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets/Misc" \
    --serum-preset-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets/Factory/Piano"
```

`test_parallel_smoke.py` exercises the public library API end-to-end (`.fxp`-only — `ParallelBatchRenderer`). `test_serum2_smoke.py` drives `run_batch_to_disk` directly because it's the only entry that accepts a single batch containing both `.fxp` and `.SerumPreset` jobs — the public `BatchRenderer` / `ParallelBatchRenderer` auto-dispatch per call, but a mixed-format batch in one call exercises the CLI's actual code path.

---

## Explicit don'ts

- **Don't use `threading.Thread`, `ThreadPoolExecutor`, or `asyncio` with DawDreamer calls.** Threading crashes DawDreamer after the first render.
- **Don't create multiple `RenderEngine` instances per worker or inside a loop.** One engine per worker process, created once in `init_worker`, reused forever.
- **Don't pass relative paths to DawDreamer.** Always `str(Path(p).resolve())`.
- **Don't call `logging.basicConfig()` from any library module.** Only `cli.py` configures logging.
- **Don't import DawDreamer at module level in `worker.py`.** Import it inside `init_worker()` as the first non-stdlib statement.
- **Don't add `if __name__ == '__main__'` guards.** loky handles Windows spawn correctly without them; adding them signals a regression to raw multiprocessing.
- **Don't call `engine.load_graph()` on every render.** Build the graph once in `init_worker`; `load_preset()` / `load_state()` update processor state in-place.
- **Don't use VST3 `.dll` or `.vst3` paths with `.fxp` presets.** DawDreamer silently ignores `.fxp` when loaded as VST3 — no error, just wrong output.
- **Don't assume a `.dll` in the VST2 folder is 64-bit.** On Windows, Serum's installer puts the 32-bit VST2 at `C:/Program Files/Common Files/VST2/Serum.dll` and the 64-bit VST2 at `C:/Program Files/Common Files/VST3/Serum_x64.dll` (yes, a VST2 `.dll` in the `VST3/` folder — the actual VST3 is the adjacent `Serum.vst3` bundle). Loading a 32-bit DLL from 64-bit Python raises `OSError [WinError 193] %1 is not a valid Win32 application`; DawDreamer surfaces this as `RuntimeError: Unable to load plugin.` with no hint as to why. Point users at the `_x64.dll`.
- **Don't pass the raw `.SerumPreset` bytes to `synth.load_state`.** The on-disk file is the wrapped form (cbor2 + zstandard); `load_state` wants the inner state. Always go through `serum2_preset_loader.convert_preset_file()` first.
- **Don't import `serum2_preset_loader` at module level in `worker.py`.** It's pure-Python (no LLVM), so the import-order constraint isn't load-bearing — but `worker.py`'s contract is "stdlib only at module level, everything else deferred". Adding a non-stdlib top-level import here erodes a guarantee that `init_worker` validation tests rely on. Defer it inside `init_worker` and `_do_render`.
- **Don't share the serum2 `state.bin` path across workers.** Each loky worker creates its own `mkdtemp` directory in `init_worker`. Sharing one path means workers stomp on each other's writes mid-render. The path lives in a per-worker module global by design.
- **Don't drop the warmup render in `init_worker`.** Serum 2 lazy-loads sample data on first render; without the warmup, the first job in each worker comes out at ~10× steady-state level. Remove it only if you have direct evidence the upstream lazy-load is gone.

---

## Verified architectural findings

The three assumptions below were verified against real Serum before the rest of the package was built. The harness is checked in at `scripts/verify_dawdreamer.py` — run it if DawDreamer is upgraded or if a different VST2 plugin is added to the supported list.

1. **`load_preset()` on an already-loaded graph updates in place — verified.** Two sequential presets on one engine produce distinct non-silent audio. No need to rebuild the graph between renders.

2. **`load_preset()` on a missing file raises `RuntimeError` — verified.** The message is descriptive (`Error: (PluginProcessor::loadPreset) File not found: <path>`) and the engine stays usable for subsequent good loads. `_do_render`'s `except Exception` block handles it cleanly; no pre-check needed.

3. **loky crash recovery — partially as expected; one nuance.** A worker killed via `os._exit` surfaces as `TerminatedWorkerError` on the future in ~60 ms (no hang). But the **executor reference itself is permanently flagged broken** — any further `submit()` on the same `executor` variable raises immediately. Recovery requires calling `get_reusable_executor(...)` again to obtain a freshly spawned pool. loky does not redistribute jobs to surviving workers; the reusable-executor API just respawns on the next call.

   **Implication for `batch.py`:** the current implementation submits everything upfront and collects futures. On a single-worker crash every subsequent future on that executor also raises — each is logged as an error result and the batch proceeds (returning error dicts for the lost jobs). Users re-run with `--skip-existing` to pick up the remainder. A more ambitious implementation could call `get_reusable_executor()` again mid-batch and resubmit unfinished jobs; that's deferred until a user hits it.
