"""
Cold vs warm pool init cost.

For each (worker count, format mode), render two back-to-back batches of
N identical jobs:

  cold_s          = wall-clock of batch 1 (pool spawn + worker imports +
                    make_plugin_processor + warmup render + N renders)
  warm_s          = wall-clock of batch 2 (same pool reused at steady state)
  init_overhead_s = cold_s - warm_s
  per_job_warm_ms = warm_s * 1000 / N
  break_even_N    = init_overhead_s * 1000 / per_job_warm_ms
                    (batch size at which init cost = render cost)

A fresh pool is forced between cells: each (workers, mode) cell either
changes `max_workers` or `initargs` for loky's get_reusable_executor,
which respawns the pool. The first cell is cold by construction (no pool
exists yet).

Modes:
  - fxp     — only Serum 1 plugin loaded; only .fxp jobs
  - serum2  — only Serum 2 plugin loaded; only .SerumPreset jobs
  - mixed   — both plugins loaded; alternating jobs

Defaults: N=50 per batch, workers={1, 4, 8}, modes={fxp, serum2, mixed}.
Whole sweep takes ~5 minutes.

Run from the repo root:
    .venv/bin/python scripts/stress_pool_init.py \\
        --fxp-dir   "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \\
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets"
"""
from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_FXP_PLUGIN = "/Library/Audio/Plug-Ins/VST/Serum.vst"
DEFAULT_SERUM2_PLUGIN = "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_NOTE = 48
DEFAULT_VELOCITY = 127
DEFAULT_DURATION = 1.0
DEFAULT_TAIL = 0.5

logger = logging.getLogger("stress_pool_init")


def _pick(items: list, n: int) -> list:
    if n >= len(items):
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _job(preset_path: Path, fmt: str, out_dir: Path, idx: int) -> dict:
    return {
        "preset_path": str(preset_path.resolve()),
        "preset_format": fmt,
        "output_path": str(out_dir / f"{fmt}_{idx:04d}.wav"),
        "note": DEFAULT_NOTE,
        "velocity": DEFAULT_VELOCITY,
        "duration": DEFAULT_DURATION,
        "tail": DEFAULT_TAIL,
        "midi_path": None,
        "midi_duration": None,
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "bit_depth": "16",
        "format": "wav",
        "skip_existing": False,
    }


def _build_mode_jobs(
    mode: str, n: int, fxp_subset: list[Path], serum2_subset: list[Path],
    out_dir: Path,
) -> list[dict]:
    if mode == "fxp":
        return [_job(p, "fxp", out_dir, i) for i, p in enumerate(fxp_subset[:n])]
    if mode == "serum2":
        return [_job(p, "serum2", out_dir, i) for i, p in enumerate(serum2_subset[:n])]
    if mode == "mixed":
        # Alternate fxp/serum2 so workers see both formats across the batch.
        half = n // 2
        fxp_part = fxp_subset[:half]
        serum2_part = serum2_subset[:n - half]
        jobs = []
        for i in range(max(len(fxp_part), len(serum2_part))):
            if i < len(fxp_part):
                jobs.append(_job(fxp_part[i], "fxp", out_dir, len(jobs)))
            if i < len(serum2_part):
                jobs.append(_job(serum2_part[i], "serum2", out_dir, len(jobs)))
        return jobs
    raise ValueError(f"unknown mode: {mode}")


def _plugins_for_mode(
    mode: str, fxp_plugin: str, serum2_plugin: str,
) -> tuple[str | None, str | None]:
    if mode == "fxp":
        return fxp_plugin, None
    if mode == "serum2":
        return None, serum2_plugin
    if mode == "mixed":
        return fxp_plugin, serum2_plugin
    raise ValueError(f"unknown mode: {mode}")


def _measure_cell(
    workers: int, mode: str, jobs_template: list[dict], out_dir: Path,
    fxp_plugin: str, serum2_plugin: str,
) -> dict:
    """Run cold + warm batch for one (workers, mode) cell."""
    from vst_render.batch import run_batch_to_disk

    fxp_p, serum2_p = _plugins_for_mode(mode, fxp_plugin, serum2_plugin)

    # Two fresh output dirs so cold and warm don't fight over filenames.
    cold_dir = out_dir / f"cell_w{workers}_{mode}_cold"
    warm_dir = out_dir / f"cell_w{workers}_{mode}_warm"
    for d in (cold_dir, warm_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    def _retarget(jobs: list[dict], target_dir: Path) -> list[dict]:
        out = []
        for j in jobs:
            nj = dict(j)
            nj["output_path"] = str(target_dir / Path(j["output_path"]).name)
            out.append(nj)
        return out

    cold_jobs = _retarget(jobs_template, cold_dir)
    warm_jobs = _retarget(jobs_template, warm_dir)

    t0 = time.monotonic()
    cold_results = run_batch_to_disk(
        cold_jobs, workers, fxp_p, serum2_p, DEFAULT_SAMPLE_RATE, on_result=None,
    )
    cold_elapsed = time.monotonic() - t0

    t1 = time.monotonic()
    warm_results = run_batch_to_disk(
        warm_jobs, workers, fxp_p, serum2_p, DEFAULT_SAMPLE_RATE, on_result=None,
    )
    warm_elapsed = time.monotonic() - t1

    n = len(cold_jobs)
    cold_ok = sum(1 for r in cold_results if r["status"] == "ok")
    warm_ok = sum(1 for r in warm_results if r["status"] == "ok")

    shutil.rmtree(cold_dir, ignore_errors=True)
    shutil.rmtree(warm_dir, ignore_errors=True)

    per_job_warm_ms = warm_elapsed * 1000.0 / max(n, 1)
    init_overhead_s = cold_elapsed - warm_elapsed
    break_even_n = (
        init_overhead_s * 1000.0 / per_job_warm_ms if per_job_warm_ms > 0 else 0.0
    )

    return {
        "workers": workers,
        "mode": mode,
        "n": n,
        "cold_ok": cold_ok,
        "warm_ok": warm_ok,
        "cold_s": cold_elapsed,
        "warm_s": warm_elapsed,
        "init_overhead_s": init_overhead_s,
        "per_job_warm_ms": per_job_warm_ms,
        "break_even_n": break_even_n,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--fxp-dir", type=Path, required=True)
    parser.add_argument("--serum2-dir", type=Path, required=True)
    parser.add_argument("--fxp-plugin", default=DEFAULT_FXP_PLUGIN)
    parser.add_argument("--serum2-plugin", default=DEFAULT_SERUM2_PLUGIN)
    parser.add_argument(
        "--workers", default="1,4,8",
        help="Comma-separated worker counts (default: 1,4,8)",
    )
    parser.add_argument(
        "--modes", default="fxp,serum2,mixed",
        help="Comma-separated modes (default: fxp,serum2,mixed)",
    )
    parser.add_argument("--n", type=int, default=50, help="jobs per batch")
    parser.add_argument(
        "--out-dir", type=Path, default=Path("./stress_pool_init"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    from vst_render.presets import discover_presets
    fxp_all = [p for p, _ in discover_presets(args.fxp_dir, recurse=True)]
    serum2_all = [p for p, _ in discover_presets(args.serum2_dir, recurse=True)]
    fxp_subset = _pick(fxp_all, args.n)
    serum2_subset = _pick(serum2_all, args.n)
    logger.info(
        "subsets: %d fxp + %d serum2 (from %d / %d total)",
        len(fxp_subset), len(serum2_subset), len(fxp_all), len(serum2_all),
    )

    worker_counts = [int(x) for x in args.workers.split(",") if x.strip()]
    modes = [x.strip() for x in args.modes.split(",") if x.strip()]
    logger.info("sweeping workers=%s, modes=%s, n=%d", worker_counts, modes, args.n)

    rows: list[dict] = []
    # Iterate (workers, mode) so each cell either changes max_workers or initargs
    # vs the previous one — loky respawns on either change, ensuring cold pool.
    for w in worker_counts:
        for m in modes:
            logger.info("=== workers=%d mode=%s ===", w, m)
            jobs = _build_mode_jobs(m, args.n, fxp_subset, serum2_subset, args.out_dir)
            r = _measure_cell(
                w, m, jobs, args.out_dir, args.fxp_plugin, args.serum2_plugin,
            )
            logger.info(
                "  cold=%.2fs  warm=%.2fs  init_overhead=%.2fs  "
                "per_job_warm=%.1f ms  break_even_N=%.0f",
                r["cold_s"], r["warm_s"], r["init_overhead_s"],
                r["per_job_warm_ms"], r["break_even_n"],
            )
            rows.append(r)

    csv_path = args.out_dir / "pool_init.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    logger.info("---- SUMMARY ----")
    logger.info(
        "%-8s %-8s %-9s %-9s %-13s %-15s %-10s",
        "workers", "mode", "cold_s", "warm_s", "init_overhead", "per_job_warm_ms", "break_even_N",
    )
    for r in rows:
        logger.info(
            "%-8d %-8s %-9.2f %-9.2f %-13.2f %-15.1f %-10.0f",
            r["workers"], r["mode"], r["cold_s"], r["warm_s"],
            r["init_overhead_s"], r["per_job_warm_ms"], r["break_even_n"],
        )
    logger.info("CSV: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
