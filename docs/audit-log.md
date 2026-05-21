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
