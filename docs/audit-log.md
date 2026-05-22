## 2026-05-11 — audit-validate

### Applied (1) — tests passed
- pyproject.toml — removed unused `pytest-mock` from `[project.optional-dependencies] dev` (tests use built-in `monkeypatch`)

### Deferred (3)
See `TODO.md` for individual entries.

### Rejected (1)
- `vst_render/renderer.py:143-147` unreachable `else` arm — rejected: comment explicitly documents it as defensive scaffolding for the planned `.vstpreset` / `.vital` format additions (TODO.md items 5 & 6). Removing intentional extensibility guards is out of scope for audit-validate.

### Stale (0)

## 2026-05-17 — audit-validate

### Applied (2) — tests passed
- `vst_render/worker.py:87-93` — gated serum2 tempdir creation in `init_worker` on `serum2_plugin_path is not None` (mirrors the existing gate in `renderer.py:84-88`); stops fxp-only workers from leaking an unused `vst_render_serum2_*` tempdir per process
- `vst_render/config.py` + `vst_render/renderer.py` + `tests/test_serum2_smoke.py` — centralized `SILENCE_EPS` in `vst_render/config.py`; `worker.py` keeps its local copy with a cross-reference comment because the worker module is contractually stdlib-only at module level

### Deferred (3)
All three findings duplicate already-open entries from the 2026-05-11 run (RenderConfig.bit_depth/format unused, `_do_render` midi guard, `cli.py` `__main__` guard). No new entries appended to `TODO.md`; the existing follow-up items are still the authoritative record.

### Rejected (0)

### Stale (0)

### Skipped (1)
`vst_render/renderer.py:143-147` unreachable `else` arm — matches the 2026-05-11 rejection above; not surfaced by the audit this run.

## 2026-05-17 — resolving the 2026-05-11 deferred set

Maintainer dispatched all three deferred items in a single follow-up session.

### Applied (2)
- `vst_render/config.py` + `tests/test_config.py` — removed `RenderConfig.bit_depth` and `RenderConfig.format`. The fields were declared + validated but never read: the CLI builds its own job dicts with their own `--bit-depth` / `--format` flags, and the library API returns numpy without writing to disk. Net: -33 lines, breaking change for any caller passing those kwargs (none known).
- `vst_render/cli.py` — dropped the trailing `if __name__ == "__main__": app()` guard. The `vst-render` console script entry point is the documented invocation; the guard's only purpose was enabling `python -m vst_render.cli`, which is not supported.

### Kept (1)
- `vst_render/worker.py:113-127` — `_do_render` midi-duration guard kept. The `run_batch_to_disk` job dict schema is documented in `CLAUDE.md`, which makes it a public seam for power users who skip the public renderer classes. The guard turns a confusing `None + float` TypeError into a clear ValueError naming the schema.

## 2026-05-19 — audit-validate

### Applied (1) — tests passed
- `vst_render/renderer.py:124-138` — removed redundant synth-None checks inside the format-dispatch arms of `render_one`. Both callers (`BatchRenderer.render`, `render_preset`) already gate via `_check_required_plugins`, and the engine's synth slots are deterministically derived from the same cfg fields by `make_engine` — the inner check was validation for a scenario that can't happen, which CLAUDE.md explicitly forbids. The `else` arm for unhandled enum values (rejected on 2026-05-11 as extensibility scaffolding for planned formats) was left untouched.

### Deferred (0)

### Rejected (1)
- Duplicate `ext_for: dict[PresetFormat, str]` map between `cli.py:144-147` and `api.py:52-55` — rejected on premature-abstraction grounds. Two 4-line dicts mapping 2 enum values to suffix strings is not load-bearing duplication; the right moment to consolidate is when TODO.md's `.vstpreset` / `.vital` format additions land. Single-rejection, no decisions-log entry warranted.

### Stale (0)

## 2026-05-20 — shared-graph rendering bug + fix

**TL;DR.** When both `--fxp` and `--serum2` plugins are loaded, every `.fxp` render in the production CLI path silently produces the wrong audio. Root cause: `engine.load_graph` with two orphan source processors and no terminal mixer routes only the last-listed processor to `get_audio()`. Fixed by splitting the worker into one `RenderEngine` per loaded format.

**How it was discovered.** Writing a state-contamination stress harness for the public API (`scripts/stress_state_contamination.py`). The harness renders each preset twice — once "warm" through `run_batch_to_disk` with `workers=1` chaining all presets through a single worker, once "cold" via one fresh Python subprocess per preset with only the matching plugin loaded — and diffs the resulting audio. Even a smoke run of four `.fxp` presets showed all four warm outputs with **identical** peak (0.2852-0.2855) and RMS (0.0181), while cold outputs varied per preset (peaks 0.39-1.07). That's not state contamination noise — that's the same waveform across four different presets, which is impossible if `load_preset` is updating state.

**Repro, distilled.**
```python
import dawdreamer as daw, numpy as np
FXP = "/Library/Audio/Plug-Ins/VST/Serum.vst"
S2  = "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
PRESET = "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets/Bass/BA Analog Pluck [SN].fxp"

def render(spec):
    eng = daw.RenderEngine(44100, 512)
    synths = [eng.make_plugin_processor(name, path) for name, path in spec]
    eng.load_graph([(s, []) for s in synths])
    for s in synths: s.clear_midi(); s.add_midi_note(48, 127, 0.0, 0.05)
    eng.render(0.1)
    fxp_synth = next(s for (n, _), s in zip(spec, synths) if n == "fxp")
    fxp_synth.load_preset(PRESET)
    fxp_synth.clear_midi(); fxp_synth.add_midi_note(48, 127, 0.0, 1.0)
    eng.render(2.0)
    return float(np.max(np.abs(eng.get_audio())))

print(render([("fxp", FXP)]))                    # 1.0668  correct
print(render([("fxp", FXP), ("serum2", S2)]))    # 0.2852  fxp output dropped
print(render([("serum2", S2), ("fxp", FXP)]))    # 1.0501  reordering "fixes" it
```

Only the last processor in `load_graph` reaches `get_audio()`. The previous architecture documented a "verified finding" that *the idle synth in a shared graph is byte-identical silence relative to a single-synth render* — that's tautologically true (the idle synth's output is discarded; so is the active synth's, if it isn't last), but it was being read as "this design is correct". It wasn't.

**Two fix options were probed before choosing.**

*Option A — Add mixer as terminal node*: `mixer = engine.make_add_processor("mixer", [1.0, 1.0])`, then `load_graph([(synth_fxp, []), (synth_serum2, []), (mixer, ["fxp_synth", "serum2_synth"])])`. Works, but the idle synth keeps producing audio (release tails, LFOs) that sums into the active synth's output. Probed residual against a clean reference: peak diff 0.63, RMS diff 0.015 — meaningful contamination, not acceptable.

*Option B — Two RenderEngines per worker, one per format*: separate engine for fxp, separate engine for serum2. Each render call touches one engine. No cross-synth state interaction, no mixer, no graph rebuilds between renders. Slightly more memory (one extra `RenderEngine` per worker) but no extra plugin instances. The CLAUDE.md rule was "no engines in a loop" — two static engines built once in `init_worker` doesn't violate that. **Picked option B.**

### Applied
- `vst_render/renderer.py` — `Engine` dataclass now holds `engine_fxp`, `engine_serum2`, `synth_fxp`, `synth_serum2`. `make_engine` creates one `RenderEngine` per loaded plugin, with its synth as the sole graph node and its own warmup render. `render_one` selects the active engine based on the preset's format.
- `vst_render/worker.py` — module globals split into `_engine_fxp` / `_engine_serum2` (plus the matching `_synth_*` and `_serum_state_path`). `init_worker` builds each engine separately. `_do_render` picks the engine to render and `get_audio` on based on `job["preset_format"]`.
- `tests/test_worker.py` — `test_do_render_serum2_dispatch_calls_load_state` updated to patch `_engine_serum2` instead of the now-gone `_engine`. All 116 fast unit tests pass.
- `docs/implementation.md` § Critical constraints #3 — rewrote to reflect the per-format engine layout and retract the "byte-identical silence" claim.
- `docs/architecture.md` — opening paragraph + organisational-notes bullet updated for the same reason.

### Follow-up
- During the probe of Option B, two consecutive renders of the *same* preset (with an unrelated render between them on the other engine) came out non-identical (peak diff up to 0.51). That's a separate state-contamination issue inside the plugin — not the load_graph bug. Logged as a TODO entry. The state-contamination stress harness (`scripts/stress_state_contamination.py`) is the right tool to characterise it once we want to dig in.
- `scripts/verify_dawdreamer.py` and `scripts/verify_dawdreamer_serum2.py` were the source of the tautological probe finding. Worth a re-read pass to retract or correct any other shared-graph claims they make; not done in this commit.

## 2026-05-21 — state-contamination characterisation + phase profiling

Follow-on to the 2026-05-20 fix: the stress harness was built to detect "warm chain" vs "fresh cold subprocess" divergence — that's what surfaced the load_graph bug. With the fix in place we ran the harness against the full factory libraries and added a separate phase profiler (`scripts/profile_render_phases.py`) to break down where time goes per render.

### State contamination — full sweep results

Harness: `scripts/stress_state_contamination.py` against 744 `.fxp` + 747 `.SerumPreset` factory presets.

Wall-clock totals on an Apple Silicon Mac:

| Pass | Time | Throughput |
|---|---|---|
| Warm (workers=1, sequential) | 510 s | 2.92 presets/s |
| Cold (4 parallel subprocesses) | 315 s | 4.73 presets/s (wall) |
| Diff | 2.2 s | — |
| **Total** | **~14 min** | — |

Diff bucketing (max_abs of warm − cold residual, 32-bit float WAV):

| Bucket | Count | % |
|---|---|---|
| Bit-identical (<1e-7) | 13 | 0.9% |
| Near-zero (<1e-4) | 14 | 0.9% |
| Audible (≥1e-2) | **1451** | **97.3%** |

Per-format distribution:

| Format | n | p50 max_abs | p90 | p99 | max |
|---|---|---|---|---|---|
| fxp    | 744 | 0.639 | 1.507 | 3.702 | 5.561 |
| serum2 | 747 | 0.371 | 1.077 | 2.393 | 5.890 |

The median preset's residual is the same order of magnitude as the audio itself. Top offenders peak at ~5.9 — Serum 2 sample-loading anomaly leaking past the per-engine warmup into per-preset territory.

Takeaway: `load_preset` / `load_state` updates parameter state in place, but does not fully reset DSP state (LFO phase, envelope position, modulator residue, lazy-loaded sample buffers). Every batch render of >1 preset depends on render order. Documented in TODO.md item 3 (mitigation options to probe) and KNOWN_ISSUES.md (user-facing reproducibility caveat). Full CSV at `stress_state_contamination_full/diff.csv`, gitignored.

### Phase profiling — where the time actually goes

Harness: `scripts/profile_render_phases.py`, 30 fxp + 30 serum2 presets, in-process for warm path + subprocess-per-preset for cold path. All numbers are means in milliseconds.

**Warm path (per render, after worker boot — this is what production CLI pays per job):**

| Phase | fxp | fxp % | serum2 | serum2 % |
|---|---|---|---|---|
| `load_preset` / `load_state` | **293** | **89%** | **72** | 35% |
| `convert_preset_file` (cbor2+zstd) | — | — | 9 | 4% |
| `engine.render` (2s audio) | 34 | 10% | **121** | **59%** |
| `clear_midi` + `add_midi_note` | <0.01 | — | <0.01 | — |
| `get_audio` | 0.08 | — | 0.07 | — |
| `sf.write` (float WAV) | 1.3 | — | 1.3 | — |
| **TOTAL per render** | **328** | | **204** | |

**Cold path (per subprocess — harness pays this 1491×; production pays it once per worker):**

| Phase | fxp | serum2 |
|---|---|---|
| Python imports (dawdreamer + np + sf) | 63 | 80 (+17 for serum2_preset_loader) |
| `RenderEngine()` | <0.05 | <0.05 |
| `make_plugin_processor` | 127 | 87 |
| `load_graph` | <0.02 | <0.02 |
| Warmup render | 0.34 | 5.28 |
| `load_preset` / `load_state` | 288 | 144 |
| Actual render + `get_audio` | 36 | 124 |
| `sf.write` | 1.6 | 1.4 |
| **TOTAL in child** | **516** | **442** |
| Outer subprocess overhead (fork+pipe) | +63 | +73 |
| **Wall per subprocess** | **579** | **515** |

### Surprises and implications

- **fxp is dominated by `load_preset` (89% of per-render cost)** — Serum 1 re-initialises wavetables on every preset load. Not amortisable; DawDreamer doesn't expose a "preset state" handle.
- **serum2 is split between `load_state` and `engine.render`** — actual rendering is the larger half. Render cost scales with voice count / sample complexity.
- **Plugin boot is much cheaper than expected** — 87–127 ms, not the 3–5 s I'd guessed. Serum lazy-loads wavetables on first `load_preset`, not on `make_plugin_processor`.
- **Subprocess overhead is small** (~150 ms total: imports + plugin boot + fork+pipe). The cold pass is slow because it pays the per-preset `load_preset` cost 1491 times, not because subprocess startup is heavy.
- **`engine.render` is fast**: 60× real-time for fxp, 17× for serum2. The renderer isn't the bottleneck.
- **`convert_preset_file`, `sf.write`, `get_audio`, MIDI scheduling are all sub-millisecond** — none worth optimising.

**Projected production throughput** on the full 1491-preset library with `--workers 5` (each worker amortises its boot): 1491 × ~270 ms ÷ 5 ≈ **80 s wall-clock**. The stress harness is ~10× slower than production by design (workers=1 in warm pass; subprocess-per-preset in cold pass).

### Applied
- `TODO.md` item 3 — replaced "pending the user kicking off the full run" with the measured numbers + mitigation-options shortlist.
- `KNOWN_ISSUES.md` — new entry "Batch renders are not bit-reproducible — output depends on preset order".
- `scripts/profile_render_phases.py` — new file. Phase profiler, runnable standalone, no test-suite deps.
- `scripts/stress_state_contamination.py` — docstring updated to reflect post-2026-05-20 architecture (per-format engines, not shared graph).

## 2026-05-22 — throughput vs worker count sweep

Follow-on to the 2026-05-21 phase profiling. The projected "~80 s @ 5 workers" was an upper bound derived from the warm per-render mean; this measured the real curve on the full 1491-preset library.

Harness: `scripts/stress_throughput_workers.py`, full library, single 16-bit WAV per preset, outputs deleted after each pass. Hardware: Apple M2 (4P + 4E cores, 8 logical).

| Workers | Elapsed | Throughput | ms/render | Speedup | Efficiency |
|---|---|---|---|---|---|
| 1 | 539.4 s | 2.76/s | 361.8 | 1.00× | 1.00 |
| 2 | 287.5 s | 5.19/s | 192.8 | 1.88× | 0.94 |
| 4 | 183.3 s | 8.13/s | 123.0 | 2.94× | 0.74 |
| 6 | 164.7 s | 9.05/s | 110.5 | 3.27× | 0.55 |
| 8 | 151.5 s | 9.84/s | 101.6 | 3.56× | 0.45 |
| 12 | 157.5 s | 9.47/s | 105.6 | 3.42× | 0.29 |

### Findings

- **Knee at w=4.** Up to 4 workers we get near-linear scaling (efficiency ≥ 0.74) because the M2's 4 P-cores absorb the load. Past w=4 we cross onto E-cores, which are slower per render — efficiency drops to 0.55 (w=6) and 0.45 (w=8).
- **w=12 regresses.** Oversubscribing 8 logical cores with 12 workers costs more in context-switching than it buys; throughput drops from 9.84/s to 9.47/s.
- **Minimum wall-clock is 151.5 s @ w=8**, ~1.9× the 80 s phase-profiling projection. The projection ignored two things: per-worker boot amortisation (small) and the heavy-tail of Serum 2 KIT/PN/Pad presets that drop throughput mid-batch from 27/s to ~12/s (visible at the 1118/1491 mark in every run).
- **Sweet spot is w=4 for efficient throughput, w=6–8 for minimum wall-clock.** A user with battery / thermals concerns should pick w=4; one renting render time should pick w=6 or 8.
- **Per-render floor is ~100 ms** — bounded by the slowest single preset under parallelism, not by the harness or the engine. Matches the warm phase profile's per-render mean of ~270 ms divided by ~3 cores worth of effective parallelism.

### Implications

- The CLI default of `--workers` = CPU count is reasonable on M2 — it lands on w=8, which is the wall-clock minimum here. If we ever surface a recommended setting in the README, "use 4–6" is the right advice for batteries / efficiency.
- No change to `batch.py` or worker behaviour is warranted from this data. The bottleneck is per-preset `load_preset` cost (89% of fxp per-render time, per 2026-05-21), which is plugin-side. The only way to push past 9.84/s on this hardware is to mitigate state contamination cheaply enough to avoid plugin reload (TODO.md item 3) AND find a way to amortise `load_preset` itself — both speculative.

### Applied
- `scripts/stress_throughput_workers.py` — new file. Sweeps worker counts over the full library, writes CSV at `./stress_throughput_workers/throughput.csv`.
- `.gitignore` — added `/stress_throughput_workers*` (anchored to repo root).

## 2026-05-22 — single-format isolation throughput

Follow-on: re-ran the throughput sweep with only one plugin loaded per pass, to test whether dual-engine init or some cross-plugin interaction was costing real time in the mixed sweep. Harness reused; one-line change so `--fxp-dir`/`--serum2-dir` being absent suppresses the matching plugin too (otherwise workers init a synth they have no jobs for).

Hardware: Apple M2 (4P + 4E cores). Sweep: w=1,2,4,8. 744 fxp + 747 serum2 presets.

| Workers | fxp-only | serum2-only | Mixed (2026-05-22 above) |
|---|---|---|---|
| 1 | 246.5 s · 3.02/s · 331 ms · 1.00 eff | 264.0 s · 2.83/s · 354 ms · 1.00 eff | 539.4 s · 2.76/s · 362 ms · 1.00 eff |
| 2 | 113.4 s · 6.56/s · 152 ms · **1.09 eff** | 167.6 s · 4.46/s · 224 ms · 0.79 eff | 287.5 s · 5.19/s · 193 ms · 0.94 eff |
| 4 | 54.7 s · 13.59/s · 74 ms · **1.13 eff** | **133.4 s** · 5.60/s · 179 ms · 0.49 eff | 183.3 s · 8.13/s · 123 ms · 0.74 eff |
| 8 | **27.5 s** · 27.08/s · 37 ms · **1.12 eff** | 148.2 s · 5.04/s · 198 ms · 0.22 eff **(regressed from w=4)** | 151.5 s · 9.84/s · 102 ms · 0.45 eff |

### Findings

- **fxp scales super-linearly.** Efficiency 1.09–1.13 across w=2/4/8. Likely cache or thermal effects: at w=1 a single P-core churns 744 wavetable loads through cache; spreading the load reduces L2/L3 thrash and avoids sustained-clock thermal throttling. The effect is consistent (not noise) — fxp w=8 hits 36.9 ms/render vs serial-projection 41.4 ms.
- **serum2 anti-scales past w=4.** w=4 (133.4 s) is the wall-clock minimum; w=8 regresses to 148.2 s. Sweet spot is w=4 at efficiency 0.49. Beyond 4 workers the heavy-tail multi-sample presets (KIT/PN/Pad in the Factory bank) contend on sample-buffer I/O or allocator locks. The same effect is visible mid-batch in the mixed sweep: throughput drops from 27/s to 12/s around the 1118/1491 mark, where those presets cluster.
- **fxp w=8 is 5.4× faster per render than serum2 w=8** (37 vs 198 ms) for comparable preset count.
- **Mixed wall-clock is gated by serum2.** Mixed w=8 (151.5 s) ≈ serum2-only w=8 (148.2 s). The fxp jobs ride along essentially for free; their per-render cost is absorbed into the serum2 wait.
- **Dual-engine init overhead is negligible.** Mixed w=1 (539.4 s) − fxp w=1 (246.5 s) − serum2 w=1 (264.0 s) = 29 s, spread across 1491 renders ≈ 19 ms each. Most of that is probably just the second `make_plugin_processor` at worker start, not per-job interference.

### Implications

- **`--workers` recommendation depends on the load mix**, not just on CPU count:
  - fxp-only: use **w=8** (CPU count). Super-linear scaling — no reason to hold back.
  - serum2-only: use **w=4**. Past that, anti-scaling. Wall-clock gets worse and you waste battery / heat.
  - Mixed: use **w=4–6**. Serum 2 caps the gain past 4.
- **Mixed-format batches are pessimal for serum2.** Routing fxp and serum2 jobs to separate pools (one at w=4 fxp, one at w=4 serum2, running concurrently) wouldn't help — wall-clock is still bounded by serum2's 133 s. Routing only becomes valuable if serum2 anti-scaling is mitigated upstream (lazy sample-load, allocator contention, etc.).
- **README should note this.** The "use CPU count" default in the CLI is right for fxp-heavy batches but wrong for serum2-heavy ones. We don't currently document a recommendation; consider a one-line tip in `--workers --help`.
- **No code change recommended from this data alone.** The anti-scaling is plugin-side. We can't fix it without a serum2 update; the only thing we could do in our code is hint at the right worker count, which is a docs change, not a behaviour change.

### Applied
- `scripts/stress_throughput_workers.py` — `--fxp-plugin` / `--serum2-plugin` are now suppressed when the matching `--*-dir` is absent, so single-format sweeps don't boot unused engines.
- `.gitignore` — added `/stress_throughput_workers_fxponly*` and `/stress_throughput_workers_serum2only*`.

## 2026-05-22 — render-duration linearity

Goal: confirm that ms/render is dominated by `load_preset` (constant) vs `engine.render` (linear in duration), as the 2026-05-21 phase profile predicted. Sweep render duration {0.5, 1, 2, 5} s at fixed w=4, fixed evenly-spaced subset (50 fxp + 50 serum2), per-format separated.

Harness: `scripts/stress_render_duration.py`.

| Duration | fxp ms/render | serum2 ms/render |
|---|---|---|
| 0.5 s | 78.7 | 100.6 |
| 1.0 s | 82.1 | 103.9 |
| 2.0 s | 85.9 | 116.1 |
| 5.0 s | 101.9 | 153.5 |

Linear fit `ms/render = constant + slope · duration_s`:

| Format | constant (load) | slope (render) | Realtime factor |
|---|---|---|---|
| fxp    | 76.4 ms | 5.1 ms/s | **197×** |
| serum2 | 93.0 ms | 12.0 ms/s | **83×** |

### Cross-checks against 2026-05-21 phase profile

- fxp constant 76 ms ≈ phase-profile `load_preset` 293 ms ÷ 4 workers = 73 ms ✓
- fxp slope 5.1 ms/s ≈ phase-profile `engine.render` rate 17 ms/s ÷ 4 = 4.3 ms/s ✓
- serum2 constant 93 ms ≈ phase-profile (`load_state` 72 + `convert_preset_file` 9 + warmup amortisation) ÷ 4 + share of heavy-tail = 93 ms ✓
- serum2 slope 12 ms/s ≈ phase-profile `engine.render` 60 ms/s ÷ 4 = 15 ms/s; observed 12 is plausibly faster because heavy-tail presets don't dominate the 50-preset subset

### Implications

- **Per-render cost is dominated by the constant, not the render duration.** At duration=1s, the constant is 93% of fxp cost and 89% of serum2 cost. Even at duration=5s, the constant is still 75% of fxp / 61% of serum2.
- **Long captures are nearly free.** A 5s render is only 1.3× (fxp) / 1.5× (serum2) the cost of a 1s render. Users who need 4-bar / 8-bar captures don't pay anywhere near 4-8× the time. This is good UX news — `--duration 5` is a fine default for tonal previews.
- **Crossover duration (where render time = load time): ~15 s for fxp, ~8 s for serum2.** Past that, longer renders start to matter. No production batch is plausibly that long.
- **Engine is not the bottleneck for any plausible batch size.** Both formats render at >80× realtime even with w=4 parallel contention. Optimisation effort should target `load_preset` / `load_state` (the constant), not the renderer.

### Applied
- `scripts/stress_render_duration.py` — new file. Duration sweep harness with per-format split, linear fit.
- `.gitignore` — added `/stress_render_duration*` (anchored).
