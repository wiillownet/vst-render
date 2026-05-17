# vst-render

Batch-render VST presets (`.fxp` Serum 1, `.SerumPreset` Serum 2) to audio files via DawDreamer. Python 3.11–3.12, GPL-3.0. CLI command `vst-render`; library exports `RenderConfig`, `BatchRenderer`, `ParallelBatchRenderer`, `render_preset`.

## Tech stack

- Python 3.11–3.12 (`pyproject.toml` upper bound `<3.13` matches DawDreamer wheel coverage)
- Build: hatchling
- Runtime: dawdreamer, soundfile, typer + rich, loky, mido, numpy, serum2-preset-loader (git pin)
- Tests: pytest with a `slow` marker for integration smokes
- CI: GitHub Actions (`.github/workflows/tests.yml`) on macOS + Windows

## Test commands

```bash
.venv/bin/pytest tests/ --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py
```

Run fast unit tests before every commit. The smoke tests (`test_parallel_smoke.py`, `test_serum2_smoke.py`) require Serum 1 + Serum 2 installed locally and run on demand — see `docs/git-workflow.md`.

## Workflow rules

- Commits land directly on `main`. No feature branches.
- Commit messages: plain English imperative (e.g. "Add Vital plugin support"). Match the style of recent history.
- After each commit, push to `origin/main` automatically.
- Run the fast unit-test command above before committing; do not commit if it fails.

## Protected paths

- `LICENSE`

## Imports

@CLAUDE-implementation.md
@docs/git-workflow.md
@docs/architecture.md
