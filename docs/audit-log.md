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
