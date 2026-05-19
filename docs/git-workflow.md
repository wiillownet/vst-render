# Git workflow

Solo project. All work lands directly on `main`. There is no PR review step.

## Branching

Don't create branches. Commit straight to `main`. If something needs isolation (a destructive refactor, an experiment), make a local branch but don't push it — merge or discard locally.

## Commit messages

Plain English imperative, ~50 char subject. Match what's already in the log:

```
Lift fxp-only gate on the public library API
Bump CI actions to v6 (Node 24) and tighten Python upper bound to <3.13
Add Serum 2 pre-implementation probes
Defensive guards from re-audit: thread safety, config freezing, midi pairing
```

Multi-step changes that were planned as a sequence use a `Step X:` prefix (see commits `4d07e49`–`bc8ae8d` for the Step A → F Serum 2 landing). Use that only when a single PR-equivalent feature is being landed across multiple commits intentionally — not for normal one-off changes.

Body is optional. Use it when the *why* isn't obvious from the diff (compatibility fix, a non-obvious tradeoff, a workaround for an upstream bug). For routine adds/fixes/cleanups, subject-only is fine.

## Workflow per change

1. Run the fast unit tests: `.venv/bin/pytest tests/ --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py`
2. If they fail, fix the failure before committing. Do not commit on red.
3. `git add` the specific files (avoid `git add -A` to keep `output/`, `.DS_Store`, `.venv/` out of the index — they're gitignored but `-A` invites accidents on untracked paths).
4. `git commit` with a plain-English imperative subject.
5. `git push origin main` immediately after the commit.

## Integration smoke tests

The two `*_smoke.py` files require Serum 1 and/or Serum 2 installed and licensed. Run them on demand:

```bash
.venv/bin/pytest tests/test_parallel_smoke.py tests/test_serum2_smoke.py \
    --fxp-plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2-plugin-path "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \
    --preset-dir "$HOME/Documents/Serum Presets/Leads/" \
    --serum-preset-dir "$HOME/Documents/Serum 2 Presets/Pads/"
```

Env vars `VST_FXP_PLUGIN_PATH`, `VST_SERUM2_PLUGIN_PATH`, `VST_PRESET_DIR`, `VST_SERUM_PRESET_DIR` are accepted as alternatives. Each fixture is independently gated, so a machine with only one plugin runs the corresponding half.

## CI

`.github/workflows/tests.yml` runs the fast unit tests on macOS and Windows. CI does not exercise the smoke tests (no Serum on runners). A red CI on `main` should be fixed in the next commit — don't leave `main` broken.

## When tests fail before committing

1. Read the failure. Fix the underlying issue.
2. Re-run the same pytest command — confirm green.
3. Then `git add`, commit, push.

Don't commit "WIP" or "fix later" on `main`.
