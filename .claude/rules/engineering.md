# Engineering rules

Run `.venv/bin/pytest tests/ --ignore=tests/test_parallel_smoke.py --ignore=tests/test_serum2_smoke.py` before every commit; do not commit on red.

Use plain-English imperative commit subjects, ~50 chars, matching the style of recent `git log`.

Commits land directly on `main`. Do not create or push feature branches.

After each commit, `git push origin main` automatically.

Stdlib-only at module level in `vst_render/worker.py`; defer `dawdreamer`, `numpy`, `soundfile`, `serum2_preset_loader` into functions.

Never use `threading.Thread`, `ThreadPoolExecutor`, or `asyncio` around DawDreamer calls.

Never call `logging.basicConfig()` outside `vst_render/cli.py`.

Always pass absolute path strings to DawDreamer (`str(Path(p).resolve())`).

Never add `if __name__ == '__main__'` guards in library code — loky handles Windows spawn.

Protected paths are listed in `CLAUDE.md` — never modify any path listed there without explicit confirmation.
