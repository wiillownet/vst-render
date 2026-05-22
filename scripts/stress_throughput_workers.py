"""
Throughput vs worker count sweep.

Renders the full preset library (or a subset via --limit) at each
worker count in the sweep, measures wall-clock + throughput, and
prints a table. Validates the per-worker amortisation model from the
2026-05-21 phase profiling.

Outputs land under --out-dir/run_w{N}/ and are removed at the end of
each worker-count run (set --keep-outputs to retain them).

Run from the repo root:
    .venv/bin/python scripts/stress_throughput_workers.py \\
        --fxp-dir   "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \\
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" \\
        --workers 1,2,4,6,8,12

The sweep is deliberately destructive of the per-run outputs because
this script only cares about wall-clock — keeping ~7 GB of duplicate
WAVs around per pass is wasteful. The result CSV is preserved.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
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
DEFAULT_TAIL = 1.0
DEFAULT_BIT_DEPTH = "16"  # smaller files; we don't read these back

logger = logging.getLogger("stress_throughput_workers")


def _build_jobs(
    fxp_dir: Path | None,
    serum2_dir: Path | None,
    out_dir: Path,
    limit: int | None,
) -> list[dict]:
    from vst_render.presets import discover_presets
    from vst_render.utils import compose_filename

    jobs: list[dict] = []
    if fxp_dir is not None:
        for preset_path, fmt in discover_presets(fxp_dir, recurse=True):
            stem = compose_filename(
                "fxp_{subpath}_{preset}", preset_path, fxp_dir,
                DEFAULT_NOTE, DEFAULT_VELOCITY,
            )
            jobs.append({
                "preset_path": str(preset_path.resolve()),
                "preset_format": fmt.value,
                "output_path": str(out_dir / f"{stem}.wav"),
                "note": DEFAULT_NOTE,
                "velocity": DEFAULT_VELOCITY,
                "duration": DEFAULT_DURATION,
                "tail": DEFAULT_TAIL,
                "midi_path": None,
                "midi_duration": None,
                "sample_rate": DEFAULT_SAMPLE_RATE,
                "bit_depth": DEFAULT_BIT_DEPTH,
                "format": "wav",
                "skip_existing": False,
            })
    if serum2_dir is not None:
        for preset_path, fmt in discover_presets(serum2_dir, recurse=True):
            stem = compose_filename(
                "serum2_{subpath}_{preset}", preset_path, serum2_dir,
                DEFAULT_NOTE, DEFAULT_VELOCITY,
            )
            jobs.append({
                "preset_path": str(preset_path.resolve()),
                "preset_format": fmt.value,
                "output_path": str(out_dir / f"{stem}.wav"),
                "note": DEFAULT_NOTE,
                "velocity": DEFAULT_VELOCITY,
                "duration": DEFAULT_DURATION,
                "tail": DEFAULT_TAIL,
                "midi_path": None,
                "midi_duration": None,
                "sample_rate": DEFAULT_SAMPLE_RATE,
                "bit_depth": DEFAULT_BIT_DEPTH,
                "format": "wav",
                "skip_existing": False,
            })

    # Disambiguate same-stem collisions in-order.
    seen: dict[str, int] = {}
    final: list[dict] = []
    for job in jobs:
        stem = Path(job["output_path"]).stem or "preset"
        if stem in seen:
            seen[stem] += 1
            job["output_path"] = str(
                Path(job["output_path"]).with_stem(f"{stem}_{seen[stem]}")
            )
        else:
            seen[stem] = 0
        final.append(job)

    if limit is not None:
        final = final[:limit]
    return final


def _run_one(
    workers: int, jobs: list[dict], out_dir: Path,
    fxp_plugin: str, serum2_plugin: str, keep_outputs: bool,
) -> dict:
    from vst_render.batch import run_batch_to_disk

    run_dir = out_dir / f"run_w{workers}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # Rewrite each job's output_path into this run's dir so the sweeps
    # don't stomp on each other.
    run_jobs = []
    for j in jobs:
        nj = dict(j)
        nj["output_path"] = str(run_dir / Path(j["output_path"]).name)
        run_jobs.append(nj)

    total = len(run_jobs)
    t0 = time.monotonic()
    done = [0]

    def _on_result(r: dict) -> None:
        done[0] += 1
        # Throttle progress logging: print at 10%, 25%, 50%, 75%, 100%.
        if done[0] in (
            max(1, total // 10), max(1, total // 4),
            max(1, total // 2), max(1, total * 3 // 4), total,
        ):
            elapsed = time.monotonic() - t0
            rate = done[0] / max(elapsed, 1e-9)
            logger.info(
                "    w=%d  %d/%d (%.1fs, %.2f/s)",
                workers, done[0], total, elapsed, rate,
            )

    results = run_batch_to_disk(
        run_jobs, workers, fxp_plugin, serum2_plugin,
        DEFAULT_SAMPLE_RATE, on_result=_on_result,
    )
    elapsed = time.monotonic() - t0
    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    skipped = sum(1 for r in results if r["status"] == "skipped")

    if not keep_outputs:
        shutil.rmtree(run_dir, ignore_errors=True)

    return {
        "workers": workers,
        "total": total,
        "ok": ok,
        "error": err,
        "skipped": skipped,
        "elapsed_s": elapsed,
        "throughput_per_s": total / max(elapsed, 1e-9),
        "ms_per_render": elapsed * 1000.0 / max(total, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--fxp-dir", type=Path)
    parser.add_argument("--serum2-dir", type=Path)
    parser.add_argument("--fxp-plugin", default=DEFAULT_FXP_PLUGIN)
    parser.add_argument("--serum2-plugin", default=DEFAULT_SERUM2_PLUGIN)
    parser.add_argument(
        "--workers", default="1,2,4,6,8,12",
        help="Comma-separated worker counts to sweep (default: 1,2,4,6,8,12)",
    )
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path("./stress_throughput_workers"),
        help="Directory for per-run output WAVs (removed after each run unless --keep-outputs)",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--keep-outputs", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.fxp_dir is None and args.serum2_dir is None:
        parser.error("provide at least one of --fxp-dir / --serum2-dir")

    worker_counts = [int(x) for x in args.workers.split(",") if x.strip()]
    logger.info("sweeping worker counts: %s", worker_counts)
    logger.info("CPU: physical=%s logical=%s",
                os.cpu_count(), os.cpu_count())

    args.out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("discovering presets...")
    jobs = _build_jobs(args.fxp_dir, args.serum2_dir, args.out_dir, args.limit)
    n_fxp = sum(1 for j in jobs if j["preset_format"] == "fxp")
    n_serum2 = sum(1 for j in jobs if j["preset_format"] == "serum2")
    logger.info("found %d presets (%d fxp, %d serum2)", len(jobs), n_fxp, n_serum2)
    if not jobs:
        logger.error("no presets to render")
        return 1

    results: list[dict] = []
    for w in worker_counts:
        logger.info("=== sweeping workers=%d ===", w)
        r = _run_one(
            w, jobs, args.out_dir,
            args.fxp_plugin, args.serum2_plugin, args.keep_outputs,
        )
        logger.info(
            "  w=%d done: %.1fs total, %.2f/s, %.1f ms/render (ok=%d err=%d)",
            r["workers"], r["elapsed_s"], r["throughput_per_s"],
            r["ms_per_render"], r["ok"], r["error"],
        )
        results.append(r)

    # Speedup vs w=1 baseline
    baseline = next((r for r in results if r["workers"] == 1), results[0])
    base_t = baseline["elapsed_s"]
    for r in results:
        r["speedup_vs_w1"] = base_t / max(r["elapsed_s"], 1e-9)
        # Parallel efficiency: speedup / N. 1.0 = perfect linear scaling.
        r["efficiency"] = r["speedup_vs_w1"] / r["workers"]

    csv_path = args.out_dir / "throughput.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    logger.info("---- SUMMARY ----")
    logger.info(
        "%-8s %-7s %-9s %-10s %-12s %-12s %-10s",
        "workers", "ok", "errors", "elapsed", "throughput", "ms/render", "efficiency",
    )
    for r in results:
        logger.info(
            "%-8d %-7d %-9d %-10.1f %-12.2f %-12.1f %-10.2f",
            r["workers"], r["ok"], r["error"], r["elapsed_s"],
            r["throughput_per_s"], r["ms_per_render"],
            r["efficiency"],
        )
    logger.info("CSV: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
