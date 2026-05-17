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
