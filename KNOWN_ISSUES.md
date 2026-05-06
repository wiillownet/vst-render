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

## A worker crash mid-batch aborts the remaining jobs on that executor

**Symptom:** after a `TerminatedWorkerError` from one render, all subsequent futures submitted to the same executor also raise, and the remaining jobs in the batch are marked as errors.

**Cause:** loky flags the entire executor as broken when any worker dies unexpectedly. Recovery requires a fresh `get_reusable_executor()` call; the library does not currently retry transparently.

**Workaround:** re-run the batch with `--skip-existing`. Jobs that completed before the crash are already on disk and will be skipped; only the tail of the batch has to re-render.
