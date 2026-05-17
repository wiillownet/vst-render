## 2026-05-11 — audit-validate

### Applied (1) — tests passed
- pyproject.toml — removed unused `pytest-mock` from `[project.optional-dependencies] dev` (tests use built-in `monkeypatch`)

### Deferred (3)
See `TODO.md` for individual entries.

### Rejected (1)
- `vst_render/renderer.py:143-147` unreachable `else` arm — rejected: comment explicitly documents it as defensive scaffolding for the planned `.vstpreset` / `.vital` format additions (TODO.md items 5 & 6). Removing intentional extensibility guards is out of scope for audit-validate.

### Stale (0)
