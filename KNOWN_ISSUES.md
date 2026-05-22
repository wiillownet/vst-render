# Known Issues

Tracked user-visible limitations and upstream quirks. Not every limitation is a bug — some are behavioural choices documented here so they're easy to find when someone hits them.

---

## Non-ASCII characters in preset paths fail to load (Windows only)

**Symptom:** a render reports `Error: (PluginProcessor::loadPreset) File not found: <path>` even though the file exists on disk. The mangled path shown in the error typically contains a `?` or replacement character where the original had an accented letter, CJK character, emoji, etc.

**Repro:** any `.fxp` whose filename or parent directory contains a character outside the Windows active code page (e.g. `á`, `ä`, `日`, `𝒮`). macOS uses UTF-8 paths end-to-end and is unaffected.

**Cause:** DawDreamer's C++ `PluginProcessor::loadPreset` converts the Python `str` path into a narrow `std::string` via the Windows active code page. Characters that can't be represented are dropped/replaced, and the mangled path no longer matches the real file. The Python-side path (via `str(Path(p).resolve())`) is correct Unicode; the mojibake is introduced at the DawDreamer boundary, outside vst-render.

**Workaround:** rename the affected preset files (or the folders containing them) to ASCII before rendering, or pre-copy them to an ASCII-safe temp location and point vst-render at that. vst-render itself handles the failure gracefully — the batch continues and these presets show up in the final error summary.

**Upstream:** would require DawDreamer to use the wide-char Windows filesystem APIs or pass paths as UTF-8 where supported. Not tracked upstream yet.

---

## Long output paths can exceed Windows `MAX_PATH` (Windows only)

**Symptom:** write failure when the rendered output sits very deep in a directory tree, especially when combined with long preset names.

**Cause:** Windows `MAX_PATH` is 260 characters for the full path. vst-render caps the filename *stem* at 196 characters (leaving 4 chars of headroom for `_N` collision suffixes) but does not cap the full path. A user with a deeply nested output directory can still exceed 260.

**Workaround:** keep the output directory shallow, use a shorter `--filename-template`, or enable long-path support in Windows (`\\?\` prefix is not applied automatically by vst-render).

---

## `--bit-depth 24` / `32f` may trip the silent-output warning spuriously

**Symptom:** `Silent output for preset: <path>` warnings on presets that are legitimately very quiet at higher bit depths.

**Cause:** the silence threshold is fixed at −90 dBFS (≈ 3.16e-5 peak), chosen to match the 16-bit quantization floor. Real audio below that level can still encode meaningfully at 24-bit / 32f. Presets with long attack envelopes or pre-delay effects can also trigger the warning even at 16-bit.

**Workaround:** treat the warning as advisory. The audio is still written correctly; only the log line is misleading.

---

## Windows reserved filenames are not filtered (Windows only)

**Symptom:** a render succeeds to a file named `CON.wav`, `PRN.wav`, `NUL.wav`, `AUX.wav`, `COM1.wav`–`COM9.wav`, or `LPT1.wav`–`LPT9.wav`, but the file cannot be opened, renamed, or deleted normally on Windows because those names are reserved device names. This includes the same names with any extension, e.g. `CON.anything`.

**Repro:** rename a preset to `CON.fxp` (or any reserved device name) and render with the default `{preset}` template.

**Cause:** `sanitize()` only strips characters outside `[A-Za-z0-9_-]`; it does not special-case Windows device names. They're valid stems as far as the sanitizer is concerned.

**Workaround:** rename the offending preset before rendering, or use a `--filename-template` that always prefixes something (e.g. `fxp_{preset}` or `{folder}_{preset}`) so the output can never land on a bare reserved name. vst-render may add an explicit filter for these names in a future release.

---

## `serum2-preset-loader` is pinned to a git commit, not a PyPI release

**Symptom:** `pip install` fails with `ValueError: Dependency #N ... cannot be a direct reference unless field tool.hatch.metadata.allow-direct-references is set to true` if you copy the dependency line into another hatch-built project.

**Cause:** `serum2-preset-loader` is not on PyPI yet, so vst-render pins it via `git+https://github.com/wiillownet/serum-2-preset-loader@<sha>` in `pyproject.toml`. Hatch's build backend rejects direct URL references unless explicitly opted in. vst-render's `pyproject.toml` already sets `[tool.hatch.metadata] allow-direct-references = true` — but downstream packagers re-declaring this dependency need the same opt-in.

**Workaround:** if vendoring the dep declaration into another package, copy the `[tool.hatch.metadata]` block too. The pin will move to a PyPI release once `serum2-preset-loader` ships one.

---

## Serum 2 cold-start audio anomaly is absorbed by a warmup render

**Symptom:** previously, the first `.SerumPreset` rendered in a fresh worker came out at ~10× the steady-state level. Subsequent presets rendered cleanly.

**Cause:** Serum 2 lazy-loads its sample data on first render. The cold render dumps unscaled wavetable data into the output before the engine settles.

**Resolution:** `init_worker` issues a 0.1-second warmup render against each loaded synth before returning, which absorbs the anomaly inside worker startup. End-users do not hit this. Documented here so it isn't reintroduced if `init_worker` is refactored — the warmup loop must stay.

---

## Per-worker tempfile directory is not cleaned up

**Symptom:** after a long-running batch, `$TMPDIR/vst_render_serum2_*` directories accumulate (one per worker process).

**Cause:** Serum 2 jobs round-trip the converted state blob through a tempfile (`serum2_preset_loader.convert_preset_file` returns bytes; `synth.load_state` takes a path). The worker creates the tempdir via `tempfile.mkdtemp()` once at init time and reuses the same `state.bin` for every job, but loky doesn't run a finalizer when workers exit, so the dir is left behind.

**Workaround:** tempfiles are small (one state blob per worker, overwritten in place — typically <1 MB) and macOS / most Linux distros sweep `/tmp` periodically. If you need aggressive cleanup, restart the process or wipe `$TMPDIR/vst_render_serum2_*` between batches.

---

## JUCE `attempt to map invalid URI` stderr noise on plugin load (macOS)

**Symptom:** every render emits one or more lines like `error: attempt to map invalid URI '/Library/Audio/Plug-Ins/VST3/Serum2.vst3'` on stderr at worker startup. The render itself completes successfully and the output audio is correct.

**Cause:** the message comes from JUCE's plugin loader (via DawDreamer), not vst-render. JUCE walks the plugin bundle to map embedded resources and logs a non-fatal warning when a path doesn't resolve as a `file://` URI. The check is advisory; the plugin still loads and renders.

**Workaround:** filter the line out at the shell if it interferes with downstream tooling: `vst-render ... 2> >(grep -v "attempt to map invalid URI" >&2)`. vst-render does not capture or suppress JUCE's stderr — doing so would risk hiding genuine plugin errors that share the same stream.

**Upstream:** would require a JUCE-level fix; not tracked.

---

## Quarantined plugin bundles fail to load (macOS)

**Symptom:** `RuntimeError: Unable to load plugin.` from DawDreamer when pointing vst-render at a plugin that opens fine in Logic, Ableton, or Reaper. The same bundle, installed by the vendor's official installer, works without issue.

**Repro:** download a `.vst3` (or `.vst` / `.component`) bundle from a browser or unzip a downloaded archive, drop it into `~/Library/Audio/Plug-Ins/VST3/`, and render with vst-render. Plugins that ship through a vendor installer (Serum, Pigments, Vital's official installer) are unaffected — installers run with admin privileges and the files they write don't inherit the quarantine attribute.

**Cause:** macOS sets `com.apple.quarantine` on anything that arrives via a browser, Mail, AirDrop, or `unzip`. The first time the kernel goes to `dlopen` a quarantined Mach-O, Gatekeeper checks the code signature; if the bundle is unsigned, ad-hoc-signed, or signed but un-notarized, the load is refused. DawDreamer surfaces that as the same generic `Unable to load plugin.` it raises for any failed `dlopen`. The Python interpreter itself is already past Gatekeeper, but each new `dlopen` is a fresh check.

**Detect:** `xattr -lr /path/to/Plugin.vst3 | grep com.apple.quarantine`. Any output means the bundle is quarantined. The xattr is set on files inside the bundle, not just the top-level directory — `xattr -l` on the bundle root alone may show nothing.

**Workaround:** strip the xattr recursively before rendering:

```bash
xattr -dr com.apple.quarantine /path/to/Plugin.vst3
```

Only do this for bundles from a vendor you trust — you are bypassing the same check that protects against tampered downloads. Re-running the vendor's official installer is the safer fix when one exists.

**Upstream:** would require DawDreamer to surface the specific dlopen error code (`EPERM` from Gatekeeper vs. `ENOEXEC` from arch mismatch vs. missing-dependency cases) instead of collapsing them all to a single string. Not tracked upstream.

---

## arm64-only Python can't load x86_64-only plugins, and vice versa (macOS)

**Symptom:** `RuntimeError: Unable to load plugin.` for an older or unmaintained plugin that loads fine in Logic. Universal2 plugins (Serum 1, Serum 2, most modern commercial VSTs) are unaffected; this only bites with single-arch builds.

**Repro:** install an x86_64-only `.vst3` (most pre-2021 commercial plugins, or any open-source plugin built without `arch -x86_64` cross-compile) on an Apple Silicon Mac with an arm64-native Python, and try to render with vst-render.

**Cause:** the DawDreamer PyPI wheel for `macosx_*_arm64` ships an arm64-only `dawdreamer.so`. The macOS python.org installer, Homebrew, `uv`, and `pyenv` all default to an arm64-native interpreter on Apple Silicon. An arm64 process can only `dlopen` arm64 (or the arm64 slice of a universal2 bundle); an x86_64-only plugin has no arm64 slice and the load fails. The reverse holds under Rosetta: an x86_64 Python interpreter — explicitly chosen with `arch -x86_64` — picks up the x86_64 DawDreamer wheel and can only load x86_64 (or the x86_64 slice of universal2). It cannot load arm64-only plugins.

**Detect:** check the architectures inside the plugin bundle's executable:

```bash
file "/path/to/Plugin.vst3/Contents/MacOS/Plugin"
```

You want to see either both `x86_64` and `arm64` (universal binary) or the same arch as your Python interpreter (`python3 -c "import platform; print(platform.machine())"`).

**Workaround:**

- Prefer a universal2 or arm64-native build of the plugin if the vendor offers one. Many vendors ship a separate "Apple Silicon" download.
- If only an x86_64 build exists and you're on Apple Silicon: create a Rosetta venv and install vst-render into it. With Rosetta installed (`softwareupdate --install-rosetta`):
  ```bash
  arch -x86_64 /usr/bin/python3 -m venv .venv-x86_64
  arch -x86_64 .venv-x86_64/bin/pip install vst-render
  arch -x86_64 .venv-x86_64/bin/vst-render ...
  ```
  loky worker processes inherit the parent's architecture, so the whole batch runs under Rosetta. Don't try to mix arches in one batch — there's no way to fan out across arches from a single executor.

**Upstream:** DawDreamer could ship a universal2 wheel that contains both slices and dispatches at runtime, but that doubles wheel size and isn't a common pattern for native-extension wheels. Not tracked upstream.

---

## Batch renders are not bit-reproducible — output depends on preset order

**Symptom:** rendering the same preset library twice produces audibly different audio for almost every preset. Re-rendering a single preset in isolation (one preset, fresh worker) produces yet another result that differs from the batch output.

**Repro:** render the same `.fxp` or `.SerumPreset` library twice with `vst-render` (different output directories, or delete and re-run); diff the WAVs. Then render any single preset alone and diff against the batch version.

**Cause:** Serum 1 and Serum 2 retain internal DSP state across consecutive renders that `load_preset` / `load_state` does not fully reset — LFO phase, envelope position, modulator residue, lazy-loaded sample buffers. The first preset rendered in a fresh worker hits cold state; every subsequent preset inherits the previous one's tail. Measured across 1491 factory presets on 2026-05-21: 97% of presets show audible (max_abs ≥ 0.01) variation between "rendered in a chained batch" and "rendered in a fresh subprocess". Median residual is 0.64 for fxp / 0.37 for serum2 — comparable to the amplitude of the audio itself.

**Workaround:** there isn't a clean one yet. Two partial mitigations:
- Run with `--workers 1` and accept order-dependence — outputs are at least reproducible *within a fixed preset order*, since the same input sequence produces the same internal-state sequence.
- For one-off renders where bit-exactness matters, run vst-render against a single preset (the cold path) rather than as part of a batch.

**Tracking:** `TODO.md` item 3 documents the measurements and the mitigation options being evaluated (per-job warmup render, per-job idle drain, per-job plugin reload). Full diff CSV is produced by `scripts/stress_state_contamination.py`.

---

## A worker crash mid-batch aborts the remaining jobs on that executor

**Symptom:** after a `TerminatedWorkerError` from one render, all subsequent futures submitted to the same executor also raise, and the remaining jobs in the batch are marked as errors.

**Cause:** loky flags the entire executor as broken when any worker dies unexpectedly. Recovery requires a fresh `get_reusable_executor()` call; the library does not currently retry transparently.

**Workaround:** re-run the batch with `--skip-existing`. Jobs that completed before the crash are already on disk and will be skipped; only the tail of the batch has to re-render.
