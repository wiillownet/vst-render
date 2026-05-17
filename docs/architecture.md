# Architecture

`vst-render` batch-renders VST presets to audio files using DawDreamer (a Python wrapper around JUCE) as a headless rendering engine. It supports two preset formats in v0.2.x: Serum 1's `.fxp` (loaded via `synth.load_preset`) and Serum 2's `.SerumPreset` (a cbor2+zstandard wrapper around the inner JUCE state blob, decoded by `serum2-preset-loader` then handed to `synth.load_state`). A worker pool (`loky`) runs one DawDreamer engine per process, with both synths loaded into a shared graph; jobs dispatch to the correct synth based on the preset's file suffix.

## Top-level directory tree

```
vst-render/
├── vst_render/              # The Python package (importable as `vst_render`)
│   ├── __init__.py          # Public API exports: RenderConfig, BatchRenderer, ParallelBatchRenderer, render_preset
│   ├── config.py            # RenderConfig dataclass; validates plugin paths and parameter shape
│   ├── presets.py           # Preset discovery; PresetFormat enum; suffix -> format mapping
│   ├── utils.py             # Filename sanitization, template composition, MIDI duration helper
│   ├── renderer.py          # In-process single-render path (used by BatchRenderer); make_engine() builds dual-synth Engine
│   ├── worker.py            # loky worker init + render_to_disk task; module globals hold engine + synths
│   ├── batch.py             # loky executor management; run_batch_to_disk, iter_batch_to_memory
│   ├── api.py               # Public-facing BatchRenderer, ParallelBatchRenderer, render_preset
│   └── cli.py               # Typer app — `vst-render` console script entry point
├── tests/                   # pytest suite; fast unit tests + slow-marked integration smokes
├── scripts/                 # Verification harnesses (verify_dawdreamer.py, verify_dawdreamer_serum2.py)
├── docs/                    # This documentation
├── .github/workflows/       # GitHub Actions CI (tests.yml)
├── pyproject.toml           # hatchling build, deps, entry point, pytest config
├── CLAUDE.md                # Short instruction file (this skill's output)
├── CLAUDE-implementation.md # Detailed implementation guide (DawDreamer API contracts, worker pattern, don'ts)
├── DESIGN.md                # Original specification (predates 0.2.x; reconciliation noted in TODO.md)
├── KNOWN_ISSUES.md          # User-visible limitations and upstream quirks
└── TODO.md                  # Open work-items, ordered by recommended next-up
```

## Entry points

- **CLI**: `vst-render` console script → `vst_render.cli:app` (Typer). Defined in `pyproject.toml` `[project.scripts]`.
- **Library**: `from vst_render import RenderConfig, BatchRenderer, ParallelBatchRenderer, render_preset` (re-exported from `vst_render/__init__.py`).
- **Worker entry inside loky pool**: `vst_render.worker.init_worker` (runs once per process) + `vst_render.worker.render_to_disk` / `render_to_memory` (per-job task).

## Top dependencies and what they do

| Dependency | Purpose |
| --- | --- |
| `dawdreamer` | Headless VST host built on JUCE. Provides `RenderEngine`, plugin processors, MIDI scheduling, offline rendering. |
| `serum2-preset-loader` | Decodes Serum 2's `.SerumPreset` files (cbor2 + zstandard wrapper) into the raw JUCE state bytes that `synth.load_state` accepts. Git-pinned to a commit SHA until upstream ships on PyPI. |
| `loky` | Robust process pool (cloudpickle-based). Used for parallel rendering; survives Windows spawn without an `if __name__ == '__main__'` guard. |
| `soundfile` | WAV writing (PCM_16/24/FLOAT subtypes). |
| `numpy` | Audio array handling; the `.npy` output format. |
| `mido` | MIDI file parsing — used by `get_midi_duration()` to compute total playback length for `--midi` mode. |
| `typer` + `rich` | CLI framework + formatted error output. Rich panels are disabled in tests for stable error assertions. |
| `hatchling` | Build backend. Requires `[tool.hatch.metadata] allow-direct-references = true` because `serum2-preset-loader` is a git URL dep. |

## Non-obvious organisational notes

- **DawDreamer import order is load-bearing.** DawDreamer uses LLVM internally; importing any other LLVM-using library before it crashes the process. `worker.py` enforces this by deferring `import dawdreamer` (and `numpy`, `soundfile`) into `init_worker()`, with only stdlib at module level. See `CLAUDE-implementation.md` § "Critical constraints" for the full rules.
- **One engine per worker, both synths in a shared graph.** Multiple `RenderEngine` instances per worker cause thread explosion and memory leaks (DawDreamer issues #88 + #1). The shared-graph approach was probe-verified: the idle synth produces byte-identical silence vs. a single-synth render.
- **A warmup render runs in `init_worker`.** Serum 2 lazy-loads sample data on the first render and the cold output comes out at ~10× steady-state level. Don't remove the warmup loop without direct evidence the upstream issue is fixed (`KNOWN_ISSUES.md` documents this).
- **Per-worker tempfiles for Serum 2.** `synth.load_state` takes a path, not bytes, so each worker's `init_worker` creates its own `mkdtemp()` dir and the worker reuses one `state.bin` (`write_bytes` overwrites). Never share the path across workers.
- **`run_batch_to_disk` is the only mixed-format entry.** The public library API (`BatchRenderer`, `ParallelBatchRenderer`) auto-detects format per path. The CLI bypasses the public API and calls `run_batch_to_disk` directly with pre-built job dicts.
- **No `__main__` guard anywhere.** loky's cloudpickle handles Windows spawn correctly. If `if __name__ == '__main__'` appears in library code, that's a regression to raw multiprocessing.
- **Threading is forbidden.** DawDreamer's JUCE internals are not thread-safe; threading causes hangs after the first render. Only `multiprocessing`/`loky` is supported.
