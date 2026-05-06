# TODO

Open work-items, ordered by recommended next-up. Strikethrough or delete entries as they ship.

---

## Recently shipped (May 2026)

- **0.2.0 — Serum 2 (`.SerumPreset`) support** — Steps A–F across `presets.py`, `worker.py`, `batch.py`, `api.py`, `cli.py`. CLI accepts `--fxp` and/or `--serum2`; format-driven dispatch through a dual-synth worker; warmup render absorbs Serum 2 cold-start anomaly. 6 commits, A → F. Verified end-to-end on macOS against real Serum 1 (`.vst`) + Serum 2 (`.vst3`).
- **Gate lift — public library API** — `BatchRenderer`, `ParallelBatchRenderer`, and `render_preset` now accept `.SerumPreset` paths via auto-detection from the file suffix. `_require_fxp_plugin` removed; replaced with `_check_required_plugins`. 135/135 tests pass.

---

## Next up

### 1. Push the branch + open a PR (or merge to `main`)
Local `main` is 8 commits ahead of `origin/main` (PR 2 Steps A–F + post-cleanup + gate lift). Decide whether to push directly or stage them as a PR for review. If publishing as a release, bump `pyproject.toml` to `0.3.0` first — the gate lift is a meaningful API change (callers who relied on the `NotImplementedError` from `_require_fxp_plugin` to detect "library doesn't support this format yet" will need to switch to the new `ValueError` path).

### 2. Reconcile DESIGN.md with shipped reality
`DESIGN.md` still describes vst-render as a `.fxp`-only, VST2-only tool. It hasn't been touched since the package was scaffolded; CLAUDE.md and README have moved on. Either:
- Refresh DESIGN.md to reflect 0.2.x (dual-format dispatch, the `serum2-preset-loader` dependency, the warmup-render constraint), or
- Demote DESIGN.md to a historical/rationale doc and link forward to CLAUDE.md as the live spec.

The second option is cheaper and makes the drift problem go away permanently — DESIGN.md becomes "why we built it this way" while CLAUDE.md remains "what the code currently does."

### 3. Add a `BatchRenderer` mixed-format smoke test
The gate lift added two `ParallelBatchRenderer` end-to-end tests. The single-process `BatchRenderer` only has the existing thread-safety guard test — there is no smoke test that proves `BatchRenderer.render()` actually round-trips a mixed-format batch through the in-process `make_engine` path. Adding one to `tests/test_serum2_smoke.py` is ~20 lines and closes the obvious coverage gap symmetrically with `ParallelBatchRenderer`.

### 4. Add CI job that installs from the git URL
README documents `pip install git+https://github.com/wiillownet/vst-render.git`, but CI only exercises the editable install. Add a job that runs the git URL install and confirms `vst-render --help` works — catches packaging regressions, including the `[tool.hatch.metadata] allow-direct-references = true` opt-in that the `serum2-preset-loader` git pin requires.

### 5. Generic `.vstpreset` support (any VST3 plugin, not just Serum 2)
Serum 2's `.SerumPreset` shipped in 0.2.0, but the generic VST3 `.vstpreset` standard remains unsupported — that's the format Vital, Pigments, and most modern VST3 plugins use. DawDreamer's `synth.load_state(path)` accepts the inner state, but `.vstpreset` files have a small VST3 header before the state payload that needs to be stripped first. Touches `presets.py` (extension list + format enum entry), `worker.py` and `renderer.py` (third dispatch arm + per-plugin format compatibility check), `cli.py` (a third format flag, or unify under `--vst3-plugin`).

### 6. Add Vital as a second supported plugin (unlocks real CI)
Vital is free and cross-platform, so it can ship on CI runners that Serum can't. Adding Vital both proves the architecture isn't Serum-specific and lets us run smoke tests on every PR. Vital uses `.vital` preset format (its own, not `.fxp` or `.vstpreset`), so this layers cleanly on the format-dispatch already in place — a third `PresetFormat` enum entry plus a `.vital` load path in `worker.py` and `renderer.py`. Likely depends on item 5 if we want a single plugin path to support both `.vstpreset` and `.vital`.

### 7. macOS KNOWN_ISSUES audit pass
Document macOS-specific quirks that the May 2026 macOS-support pass didn't fully investigate:
- Gatekeeper / quarantine behavior on un-notarized VST bundles
- universal2 vs arm64-only plugin builds (Rosetta implications)

Output: new entries in `KNOWN_ISSUES.md`.

---

## Eventual / blocked

### Switch `serum2-preset-loader` from git pin to PyPI version
Currently pinned to a 40-char SHA via `git+https://...@<sha>`. Once `serum2-preset-loader` ships a PyPI release, replace the git URL with a `>=x.y` version constraint. This lets us drop the `[tool.hatch.metadata] allow-direct-references = true` opt-in (assuming no other direct refs land in the meantime) and cleans up the install path. Blocked on upstream releasing.
