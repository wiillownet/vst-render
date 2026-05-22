# TODO

Open work-items, ordered by recommended next-up. Strikethrough or delete entries as they ship.

---

## Recently shipped (May 2026)

- **0.2.0 — Serum 2 (`.SerumPreset`) support** — Steps A–F across `presets.py`, `worker.py`, `batch.py`, `api.py`, `cli.py`. CLI accepts `--fxp` and/or `--serum2`; format-driven dispatch through a dual-synth worker; warmup render absorbs Serum 2 cold-start anomaly. 6 commits, A → F. Verified end-to-end on macOS against real Serum 1 (`.vst`) + Serum 2 (`.vst3`).
- **Gate lift — public library API** — `BatchRenderer`, `ParallelBatchRenderer`, and `render_preset` now accept `.SerumPreset` paths via auto-detection from the file suffix. `_require_fxp_plugin` removed; replaced with `_check_required_plugins`. 135/135 tests pass.
- **DESIGN.md demoted to historical doc** — README and implementation guide now point at CLAUDE.md as the live spec; DESIGN.md kept for original v1 rationale.
- **`BatchRenderer` mixed-format smoke test** — closes the in-process coverage gap left by the gate lift, symmetric with the existing `ParallelBatchRenderer` mixed-format test.
- **CI job for git-URL install** — new `git-install` job in `.github/workflows/tests.yml` installs from `git+https://github.com/${repo}.git@${sha}` on every push to `main` and runs `vst-render --help`. Catches the `allow-direct-references = true` opt-in regression and other packaging breakage that the editable install can hide. (Surfaced that the repo was private; now public.)
- **Audit-validate follow-ups cleared** — removed `RenderConfig.bit_depth` + `RenderConfig.format` (never read by any library path); dropped the `__main__` guard at the bottom of `cli.py` (console script is the only supported invocation); kept the `_do_render` midi-duration guard (job dict is a public seam per `CLAUDE.md`).
- **macOS KNOWN_ISSUES audit pass** — added two entries to `KNOWN_ISSUES.md`: quarantined plugin bundles failing to load (Gatekeeper refuses `dlopen` on un-notarized code; detect with `xattr -lr ... | grep com.apple.quarantine`, fix with `xattr -dr com.apple.quarantine ...`) and arm64-only Python being unable to load x86_64-only plugins (DawDreamer's PyPI wheel is single-arch; workaround is a Rosetta venv).

---

## Next up

### 1. Generic `.vstpreset` support (any VST3 plugin, not just Serum 2)
Serum 2's `.SerumPreset` shipped in 0.2.0, but the generic VST3 `.vstpreset` standard remains unsupported — that's the format Vital, Pigments, and most modern VST3 plugins use. DawDreamer's `synth.load_state(path)` accepts the inner state, but `.vstpreset` files have a small VST3 header before the state payload that needs to be stripped first. Touches `presets.py` (extension list + format enum entry), `worker.py` and `renderer.py` (third dispatch arm + per-plugin format compatibility check), `cli.py` (a third format flag, or unify under `--vst3-plugin`).

### 2. Add Vital as a second supported plugin (unlocks real CI)
Vital is free and cross-platform, so it can ship on CI runners that Serum can't. Adding Vital both proves the architecture isn't Serum-specific and lets us run smoke tests on every PR. Vital uses `.vital` preset format (its own, not `.fxp` or `.vstpreset`), so this layers cleanly on the format-dispatch already in place — a third `PresetFormat` enum entry plus a `.vital` load path in `worker.py` and `renderer.py`. Likely depends on item 1 if we want a single plugin path to support both `.vstpreset` and `.vital`.

### 3. Mitigate plugin state contamination across consecutive renders
Surfaced while probing the fix for the 2026-05-20 shared-graph bug, then measured against the full factory libraries on 2026-05-21 with `scripts/stress_state_contamination.py` (warm pass: single worker, presets chained; cold pass: fresh subprocess per preset). Results across 1491 presets:

| Format | n | p50 max_abs | p90 | p99 | max | audible (≥0.01) |
|---|---|---|---|---|---|---|
| fxp    | 744 | 0.639 | 1.507 | 3.702 | 5.561 | 741 (99.6%) |
| serum2 | 747 | 0.371 | 1.077 | 2.393 | 5.890 | 710 (95.0%) |

The median preset's warm-vs-cold residual is the same order of magnitude as the audio itself; max residual is 5.89 (greater than full-scale — Serum 2 lazy-loaded sample data leaking past the per-engine warmup into per-preset territory). 1451/1491 (97%) of all presets show audible variation. Only 13 are bit-identical and those are mostly silent-output presets where both passes produce silence.

The plugin retains internal state (LFO phase, envelope position, sample buffers, modulator residue) that `load_preset` / `load_state` doesn't fully reset. Impact: every batch render is non-deterministic in render order — re-running the same batch with `--skip-existing` removed produces different audio per preset. Not a CLI bug, but a reproducibility issue we should document and mitigate.

Mitigation options to probe (none implemented):
- **Per-job warmup**: render a short note before each real render and discard. Doubles compute per render but cheap to test.
- **Per-job `clear_midi` + idle render**: drain envelope/LFO tail before loading the next preset. Cheaper than full warmup.
- **Per-job plugin reload**: tear down + rebuild the synth. Most expensive (~100ms `make_plugin_processor` per job) but guaranteed clean.
- **Send all-notes-off + a fresh `load_preset`/`load_state` cycle**: cheapest if it works.

Full diff CSV at `stress_state_contamination_full/diff.csv` (gitignored). KNOWN_ISSUES.md has a user-facing entry pointing at this for the reproducibility caveat.

### 4. Worker crash recovery stress test
Verify the loky behaviour documented in `docs/implementation.md` § "Verified architectural findings" #3: a worker killed mid-batch surfaces as `TerminatedWorkerError` on its future without hanging the rest of the batch, and the executor reference becomes permanently broken (every subsequent `submit()` raises). Useful to re-run after a DawDreamer or loky upgrade. Implementation sketch: a stress harness that submits a sentinel job calling `os._exit(1)`, confirms the kill surfaces on that future, and checks that subsequent submits also raise (validating the "respawn requires a fresh `get_reusable_executor()` call" claim). ~5 minutes to write and run.

---

## Eventual / blocked

### Switch `serum2-preset-loader` from git pin to PyPI version
Currently pinned to a 40-char SHA via `git+https://...@<sha>`. Once `serum2-preset-loader` ships a PyPI release, replace the git URL with a `>=x.y` version constraint. This lets us drop the `[tool.hatch.metadata] allow-direct-references = true` opt-in (assuming no other direct refs land in the meantime) and cleans up the install path. Blocked on upstream releasing.

All audit-validate follow-ups from 2026-05-11 have been resolved — see `docs/audit-log.md`.
