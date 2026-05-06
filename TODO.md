# TODO

Open work-items, lowest-effort first. Strikethrough or delete entries as they ship.

## Lift the fxp-only gate on the public library API
`BatchRenderer`, `ParallelBatchRenderer`, and `render_preset` raise `NotImplementedError` if `RenderConfig.fxp_plugin_path` is unset (the `_require_fxp_plugin` guard in `api.py`). The CLI is already format-driven; the library equivalent needs a way for the caller to mark each preset path with its format. Two reasonable shapes:
- `render_batch` accepts `list[tuple[str | Path, PresetFormat]]` instead of bare paths, and `BatchRenderer.render` takes the format alongside the path.
- Auto-detect from the file extension via `presets.detect_format`, which is what the CLI does.

The auto-detect form keeps the existing API shape and is probably the right call. Touches `api.py` (`_build_jobs`, `BatchRenderer.render`, `render_preset`), README's "Library API" section, and a new test file alongside `test_api.py`.

## macOS KNOWN_ISSUES audit pass
Document macOS-specific quirks that the May 2026 macOS-support pass didn't fully investigate:
- Gatekeeper / quarantine behavior on un-notarized VST bundles
- universal2 vs arm64-only plugin builds (Rosetta implications)
- ~~The recurring `attempt to map invalid URI` JUCE stderr noise on every plugin load~~ (documented in KNOWN_ISSUES, May 2026)

Output: new entries in `KNOWN_ISSUES.md`.

## Add CI job that installs from git URL
README documents `pip install git+https://github.com/wiillownet/vst-render.git` but CI only exercises the editable install. Add a job that runs the git URL install and confirms `vst-render --help` works — catches packaging regressions, including the `[tool.hatch.metadata] allow-direct-references = true` opt-in that the `serum2-preset-loader` git pin requires.

## Generic `.vstpreset` support (any VST3 plugin, not just Serum 2)
Serum 2's `.SerumPreset` format shipped in 0.2.0, but the generic VST3 `.vstpreset` standard remains unsupported — that's the format Vital, Pigments, and most modern VST3 plugins use. DawDreamer's `synth.load_state(path)` accepts the inner state, but `.vstpreset` files have a small VST3 header before the state payload that needs to be stripped first. Touches `presets.py` (extension list + format enum entry), `worker.py` (third dispatch arm + per-plugin format compatibility check), `cli.py` (a third format flag, or unify under `--vst3-plugin`).

## Add Vital as second supported plugin
Vital is free + cross-platform, so adding it as a second supported plugin both proves the architecture isn't Serum-specific and unlocks a real integration test in CI (Serum can't ship on CI runners; Vital can). Vital uses `.vital` preset format (its own, not `.fxp` or `.vstpreset`), so this layers on the format-dispatch work already in place — adding a third `PresetFormat` enum value plus a Vital-specific load path in `worker.py`.
