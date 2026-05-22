"""
soundfile.write vs engine.render share of wall-clock under parallel load.

The 2026-05-21 phase profile measured these phases single-threaded.
Under w≥4, parallel I/O can either overlap freely with concurrent renders
(small wall-clock cost) or serialise on the disk (large wall-clock cost).
We answer this by running an A/B per (workers, mode) cell:

  A (disk):    each job calls _do_render then _write_audio
  B (no-disk): each job calls _do_render only (skips the WAV write)

with a throwaway warmup batch first so the pool is hot. Two metrics:

  cumulative_write_ms = sum of per-job write_ms across the whole batch
                        (the budget consumed by I/O, summed across workers)
  wall_clock_io_share = (T_disk - T_nodisk) / T_disk
                        (how much of wall-clock disappears if writes are
                        free — small if parallel I/O is overlapped, large
                        if it's serialised)

cumulative_write_ms / (cumulative_render_ms + cumulative_write_ms) tells
us how much CPU+IO budget is writes. wall_clock_io_share tells us whether
that budget actually shows up on the user's clock.

Run from the repo root:
    .venv/bin/python scripts/stress_io_split.py \\
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

logger = logging.getLogger("stress_io_split")


def timed_task(job: dict) -> dict:
    """Worker task: time _do_render and (optionally) _write_audio separately.

    Job dict carries an extra `_do_write` boolean controlling whether the
    WAV write happens. Both phases are timed with time.perf_counter().
    """
    import time as _time
    from vst_render import worker as W

    t0 = _time.perf_counter()
    audio = W._do_render(job)
    render_ms = (_time.perf_counter() - t0) * 1000.0

    write_ms = 0.0
    if job.get("_do_write", True):
        t1 = _time.perf_counter()
        W._write_audio(
            audio, job["output_path"], job["sample_rate"],
            job["bit_depth"], job["format"],
        )
        write_ms = (_time.perf_counter() - t1) * 1000.0

    return {
        "status": "ok",
        "path": job["preset_path"],
        "render_ms": render_ms,
        "write_ms": write_ms,
    }


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


def _submit_batch(
    executor, jobs: list[dict], do_write: bool,
) -> tuple[float, list[dict]]:
    from concurrent.futures import as_completed
    tagged = [{**j, "_do_write": do_write} for j in jobs]
    t0 = time.monotonic()
    futures = [executor.submit(timed_task, j) for j in tagged]
    results = [f.result() for f in as_completed(futures)]
    return time.monotonic() - t0, results


def _measure_cell(
    workers: int, mode: str, subset: list[Path], out_dir: Path,
    fxp_plugin: str, serum2_plugin: str,
) -> dict:
    """Per (workers, mode): warmup → write=True (measured) → write=False (measured)."""
    from loky import get_reusable_executor
    from vst_render import worker as W

    fxp_p = fxp_plugin if mode == "fxp" else None
    serum2_p = serum2_plugin if mode == "serum2" else None

    executor = get_reusable_executor(
        max_workers=workers,
        initializer=W.init_worker,
        initargs=(fxp_p, serum2_p, DEFAULT_SAMPLE_RATE),
        timeout=1800,
    )

    cell_dir = out_dir / f"cell_w{workers}_{mode}"
    if cell_dir.exists():
        shutil.rmtree(cell_dir)
    cell_dir.mkdir(parents=True)

    def _build(subdir: str) -> list[dict]:
        d = cell_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        return [_job(p, mode, d, i) for i, p in enumerate(subset)]

    # 1. Warmup pass — throwaway timing. Writes to disk so the file cache
    # is in a realistic state by the time we measure.
    _ = _submit_batch(executor, _build("warmup"), do_write=True)

    # 2. Disk pass — measured.
    disk_elapsed, disk_results = _submit_batch(
        executor, _build("disk"), do_write=True,
    )

    # 3. No-disk pass — measured.
    nodisk_elapsed, nodisk_results = _submit_batch(
        executor, _build("nodisk"), do_write=False,
    )

    shutil.rmtree(cell_dir, ignore_errors=True)

    n = len(subset)
    sum_render_disk = sum(r["render_ms"] for r in disk_results)
    sum_write_disk = sum(r["write_ms"] for r in disk_results)
    sum_render_nodisk = sum(r["render_ms"] for r in nodisk_results)

    cpu_io_total = sum_render_disk + sum_write_disk
    write_share_cum = (sum_write_disk / cpu_io_total) if cpu_io_total > 0 else 0.0
    wallclock_io_share = (
        (disk_elapsed - nodisk_elapsed) / disk_elapsed if disk_elapsed > 0 else 0.0
    )

    return {
        "workers": workers,
        "mode": mode,
        "n": n,
        "disk_wall_s": disk_elapsed,
        "nodisk_wall_s": nodisk_elapsed,
        "wallclock_io_share": wallclock_io_share,
        "sum_render_ms_disk": sum_render_disk,
        "sum_write_ms_disk": sum_write_disk,
        "sum_render_ms_nodisk": sum_render_nodisk,
        "cum_write_share": write_share_cum,
        "mean_render_ms_disk": sum_render_disk / n,
        "mean_write_ms_disk": sum_write_disk / n,
        "mean_render_ms_nodisk": sum_render_nodisk / n,
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
        "--workers", default="4,8",
        help="Comma-separated worker counts (default: 4,8)",
    )
    parser.add_argument(
        "--modes", default="fxp,serum2",
        help="Comma-separated modes (default: fxp,serum2)",
    )
    parser.add_argument("--n", type=int, default=300, help="jobs per batch (warmup + disk + nodisk)")
    parser.add_argument(
        "--out-dir", type=Path, default=Path("./stress_io_split"),
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
    for w in worker_counts:
        for m in modes:
            subset = fxp_subset if m == "fxp" else serum2_subset
            logger.info("=== workers=%d mode=%s ===", w, m)
            r = _measure_cell(
                w, m, subset, args.out_dir,
                args.fxp_plugin, args.serum2_plugin,
            )
            logger.info(
                "  disk wall=%.2fs  nodisk wall=%.2fs  wallclock_io_share=%.1f%%",
                r["disk_wall_s"], r["nodisk_wall_s"],
                r["wallclock_io_share"] * 100,
            )
            logger.info(
                "  per-job: render=%.1fms  write=%.1fms  (cumulative write share %.1f%%)",
                r["mean_render_ms_disk"], r["mean_write_ms_disk"],
                r["cum_write_share"] * 100,
            )
            rows.append(r)

    csv_path = args.out_dir / "io_split.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    logger.info("---- SUMMARY ----")
    logger.info(
        "%-8s %-8s %-9s %-11s %-15s %-13s %-13s %-15s",
        "workers", "mode", "disk_s", "nodisk_s", "wallclock_io_%",
        "render_ms/job", "write_ms/job", "cum_write_%",
    )
    for r in rows:
        logger.info(
            "%-8d %-8s %-9.2f %-11.2f %-15.1f %-13.1f %-13.1f %-15.1f",
            r["workers"], r["mode"], r["disk_wall_s"], r["nodisk_wall_s"],
            r["wallclock_io_share"] * 100,
            r["mean_render_ms_disk"], r["mean_write_ms_disk"],
            r["cum_write_share"] * 100,
        )
    logger.info("CSV: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
