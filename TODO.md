# TODO

Open work-items, ordered by recommended next-up. Strikethrough or delete entries as they ship.

---

## Recently shipped (May 2026)

- **0.2.0 — Serum 2 (`.SerumPreset`) support** — Steps A–F across `presets.py`, `worker.py`, `batch.py`, `api.py`, `cli.py`. CLI accepts `--fxp` and/or `--serum2`; format-driven dispatch through a dual-synth worker; warmup render absorbs Serum 2 cold-start anomaly. 6 commits, A → F. Verified end-to-end on macOS against real Serum 1 (`.vst`) + Serum 2 (`.vst3`).
- **Gate lift — public library API** — `BatchRenderer`, `ParallelBatchRenderer`, and `render_preset` now accept `.SerumPreset` paths via auto-detection from the file suffix. `_require_fxp_plugin` removed; replaced with `_check_required_plugins`. 135/135 tests pass.
- **DESIGN.md demoted to historical doc** — README, CLAUDE-implementation.md now point at CLAUDE.md as the live spec; DESIGN.md kept for original v1 rationale.
- **`BatchRenderer` mixed-format smoke test** — closes the in-process coverage gap left by the gate lift, symmetric with the existing `ParallelBatchRenderer` mixed-format test.
- **CI job for git-URL install** — new `git-install` job in `.github/workflows/tests.yml` installs from `git+https://github.com/${repo}.git@${sha}` on every push to `main` and runs `vst-render --help`. Catches the `allow-direct-references = true` opt-in regression and other packaging breakage that the editable install can hide.

---

## Next up

### 1. Generic `.vstpreset` support (any VST3 plugin, not just Serum 2)
Serum 2's `.SerumPreset` shipped in 0.2.0, but the generic VST3 `.vstpreset` standard remains unsupported — that's the format Vital, Pigments, and most modern VST3 plugins use. DawDreamer's `synth.load_state(path)` accepts the inner state, but `.vstpreset` files have a small VST3 header before the state payload that needs to be stripped first. Touches `presets.py` (extension list + format enum entry), `worker.py` and `renderer.py` (third dispatch arm + per-plugin format compatibility check), `cli.py` (a third format flag, or unify under `--vst3-plugin`).

### 2. Add Vital as a second supported plugin (unlocks real CI)
Vital is free and cross-platform, so it can ship on CI runners that Serum can't. Adding Vital both proves the architecture isn't Serum-specific and lets us run smoke tests on every PR. Vital uses `.vital` preset format (its own, not `.fxp` or `.vstpreset`), so this layers cleanly on the format-dispatch already in place — a third `PresetFormat` enum entry plus a `.vital` load path in `worker.py` and `renderer.py`. Likely depends on item 1 if we want a single plugin path to support both `.vstpreset` and `.vital`.

### 3. macOS KNOWN_ISSUES audit pass
Document macOS-specific quirks that the May 2026 macOS-support pass didn't fully investigate:
- Gatekeeper / quarantine behavior on un-notarized VST bundles
- universal2 vs arm64-only plugin builds (Rosetta implications)

Output: new entries in `KNOWN_ISSUES.md`.

---

## Eventual / blocked

### Switch `serum2-preset-loader` from git pin to PyPI version
Currently pinned to a 40-char SHA via `git+https://...@<sha>`. Once `serum2-preset-loader` ships a PyPI release, replace the git URL with a `>=x.y` version constraint. This lets us drop the `[tool.hatch.metadata] allow-direct-references = true` opt-in (assuming no other direct refs land in the meantime) and cleans up the install path. Blocked on upstream releasing.

---

## Audit-validate follow-ups

- [ ] [audit-validate] Decide what to do about `RenderConfig.bit_depth` and `RenderConfig.format` — declared + validated but never read by any library code path (CLI bypasses RenderConfig and uses its own --bit-depth/--format options; library API returns numpy without writing to disk).
  - File: vst_render/config.py:22-23, 48-53
  - Why deferred: removing changes the public API surface of a published library; the alternative is to wire these into a future `render_to_file` library path. Semantic call only the maintainer can make.
  - Source: audit run on 2026-05-11
- [ ] [audit-validate] Decide whether the `_do_render` midi-duration guard is internal-only or part of a public job-dict contract.
  - File: vst_render/worker.py:121-125
  - Why deferred: the comment admits it's defensive against a hypothetical third-party caller; pinned by a dedicated test. Keep if `run_batch_to_disk` job dicts are a public seam, remove if strictly internal.
  - Source: audit run on 2026-05-11
- [ ] [audit-validate] Decide whether `python -m vst_render.cli` is a supported invocation; if not, drop the `if __name__ == "__main__": app()` guard at the bottom of cli.py.
  - File: vst_render/cli.py:247-248
  - Why deferred: the `vst-render` console script entry point doesn't need the guard; the guard only enables `python -m vst_render.cli`. Whether to support that invocation is a docs/UX call.
  - Source: audit run on 2026-05-11
