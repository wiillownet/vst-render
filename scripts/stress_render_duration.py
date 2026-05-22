"""
Render duration linearity sweep.

Renders the same fixed preset subset at multiple note durations and
reports ms/render per format. Validates the per-render cost model:

    ms/render ≈ constant + duration × render_rate

where `constant` is dominated by load_preset/load_state (per phase
profile) and `render_rate` is `1 / realtime_factor` (60× for fxp,
17× for serum2 per 2026-05-21 phase profile).

Fixed at w=4 — the per-format sweet spot from 2026-05-22 isolation
sweep. A small fixed subset (default 50 fxp + 50 serum2) keeps each
duration's run inside ~30 s so the whole sweep fits in a few minutes.

Run from the repo root:
    .venv/bin/python scripts/stress_render_duration.py \\
        --fxp-dir   "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \\
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" \\
        --durations 0.5,1,2,5
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
DEFAULT_TAIL = 0.5

logger = logging.getLogger("stress_render_duration")


def _pick(preset_paths: list[tuple[Path, str]], n: int) -> list[tuple[Path, str]]:
    """Evenly-spaced subset so we don't bias toward the first N alphabetically."""
    if n >= len(preset_paths):
        return preset_paths
    step = len(preset_paths) / n
    return [preset_paths[int(i * step)] for i in range(n)]


def _build_jobs(
    fxp_subset: list[Path], serum2_subset: list[Path],
    out_dir: Path, duration: float,
) -> list[dict]:
    jobs: list[dict] = []
    for i, p in enumerate(fxp_subset):
        jobs.append({
            "preset_path": str(p.resolve()),
            "preset_format": "fxp",
            "output_path": str(out_dir / f"fxp_{i:04d}.wav"),
            "note": DEFAULT_NOTE,
            "velocity": DEFAULT_VELOCITY,
            "duration": duration,
            "tail": DEFAULT_TAIL,
            "midi_path": None,
            "midi_duration": None,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "bit_depth": "16",
            "format": "wav",
            "skip_existing": False,
        })
    for i, p in enumerate(serum2_subset):
        jobs.append({
            "preset_path": str(p.resolve()),
            "preset_format": "serum2",
            "output_path": str(out_dir / f"serum2_{i:04d}.wav"),
            "note": DEFAULT_NOTE,
            "velocity": DEFAULT_VELOCITY,
            "duration": duration,
            "tail": DEFAULT_TAIL,
            "midi_path": None,
            "midi_duration": None,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "bit_depth": "16",
            "format": "wav",
            "skip_existing": False,
        })
    return jobs


def _run_one(
    duration: float, fxp_subset: list[Path], serum2_subset: list[Path],
    out_dir: Path, workers: int, fxp_plugin: str | None, serum2_plugin: str | None,
) -> dict:
    from vst_render.batch import run_batch_to_disk

    run_dir = out_dir / f"run_d{duration}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    jobs = _build_jobs(fxp_subset, serum2_subset, run_dir, duration)
    total = len(jobs)

    # Per-job timing comes from run_batch_to_disk's elapsed-per-result if we
    # collected it, but we don't — easiest is wall-clock per format. For a
    # clean per-render number we time the whole pass and divide; mixed pool
    # parallelism means this is mean-not-per-format, so we also separate
    # fxp-only and serum2-only passes for clean per-format numbers.
    t0 = time.monotonic()
    results = run_batch_to_disk(
        jobs, workers, fxp_plugin, serum2_plugin,
        DEFAULT_SAMPLE_RATE, on_result=None,
    )
    elapsed = time.monotonic() - t0
    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")

    shutil.rmtree(run_dir, ignore_errors=True)

    return {
        "duration_s": duration,
        "total": total,
        "ok": ok,
        "error": err,
        "elapsed_s": elapsed,
        "ms_per_render": elapsed * 1000.0 / max(total, 1),
    }


def _run_per_format(
    duration: float, fxp_subset: list[Path], serum2_subset: list[Path],
    out_dir: Path, workers: int, fxp_plugin: str, serum2_plugin: str,
) -> dict:
    """Two passes, one per format, each with only its plugin loaded."""
    from vst_render.batch import run_batch_to_disk

    run_dir = out_dir / f"run_d{duration}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    fxp_jobs = _build_jobs(fxp_subset, [], run_dir, duration)
    serum2_jobs = _build_jobs([], serum2_subset, run_dir, duration)

    t0 = time.monotonic()
    fxp_results = run_batch_to_disk(
        fxp_jobs, workers, fxp_plugin, None,
        DEFAULT_SAMPLE_RATE, on_result=None,
    )
    fxp_elapsed = time.monotonic() - t0

    t1 = time.monotonic()
    serum2_results = run_batch_to_disk(
        serum2_jobs, workers, None, serum2_plugin,
        DEFAULT_SAMPLE_RATE, on_result=None,
    )
    serum2_elapsed = time.monotonic() - t1

    shutil.rmtree(run_dir, ignore_errors=True)

    return {
        "duration_s": duration,
        "fxp_n": len(fxp_jobs),
        "fxp_elapsed_s": fxp_elapsed,
        "fxp_ms_per_render": fxp_elapsed * 1000.0 / max(len(fxp_jobs), 1),
        "fxp_ok": sum(1 for r in fxp_results if r["status"] == "ok"),
        "serum2_n": len(serum2_jobs),
        "serum2_elapsed_s": serum2_elapsed,
        "serum2_ms_per_render": serum2_elapsed * 1000.0 / max(len(serum2_jobs), 1),
        "serum2_ok": sum(1 for r in serum2_results if r["status"] == "ok"),
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
        "--durations", default="0.5,1,2,5",
        help="Comma-separated render durations in seconds (default: 0.5,1,2,5)",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--n-per-format", type=int, default=50)
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path("./stress_render_duration"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.fxp_dir is None or args.serum2_dir is None:
        parser.error("provide BOTH --fxp-dir and --serum2-dir for the per-format split")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    durations = [float(x) for x in args.durations.split(",") if x.strip()]

    from vst_render.presets import discover_presets
    fxp_all = list(discover_presets(args.fxp_dir, recurse=True))
    serum2_all = list(discover_presets(args.serum2_dir, recurse=True))
    fxp_subset = [p for p, _ in _pick(fxp_all, args.n_per_format)]
    serum2_subset = [p for p, _ in _pick(serum2_all, args.n_per_format)]
    logger.info(
        "subset: %d fxp + %d serum2 (from %d / %d total)",
        len(fxp_subset), len(serum2_subset), len(fxp_all), len(serum2_all),
    )
    logger.info("sweeping durations: %s s; workers=%d", durations, args.workers)

    results = []
    for d in durations:
        logger.info("=== duration=%.2fs ===", d)
        r = _run_per_format(
            d, fxp_subset, serum2_subset, args.out_dir,
            args.workers, args.fxp_plugin, args.serum2_plugin,
        )
        logger.info(
            "  fxp:    %.1fs / %d = %.1f ms/render",
            r["fxp_elapsed_s"], r["fxp_n"], r["fxp_ms_per_render"],
        )
        logger.info(
            "  serum2: %.1fs / %d = %.1f ms/render",
            r["serum2_elapsed_s"], r["serum2_n"], r["serum2_ms_per_render"],
        )
        results.append(r)

    # Fit linear slope per format: ms/render = a + b * duration
    def _fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
        n = len(xs)
        sx = sum(xs); sy = sum(ys)
        sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if denom == 0:
            return (sy / n, 0.0)
        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n
        return (a, b)

    xs = [r["duration_s"] for r in results]
    fxp_a, fxp_b = _fit(xs, [r["fxp_ms_per_render"] for r in results])
    s2_a, s2_b = _fit(xs, [r["serum2_ms_per_render"] for r in results])

    csv_path = args.out_dir / "duration.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    logger.info("---- SUMMARY ----")
    logger.info("%-10s %-15s %-18s", "duration", "fxp ms/render", "serum2 ms/render")
    for r in results:
        logger.info(
            "%-10.2f %-15.1f %-18.1f",
            r["duration_s"], r["fxp_ms_per_render"], r["serum2_ms_per_render"],
        )
    logger.info("---- LINEAR FIT (ms/render = constant + slope · duration_s) ----")
    logger.info(
        "fxp:    constant=%.1f ms, slope=%.1f ms/s   (=> realtime factor %.0fx)",
        fxp_a, fxp_b, 1000.0 / max(fxp_b, 1e-9),
    )
    logger.info(
        "serum2: constant=%.1f ms, slope=%.1f ms/s   (=> realtime factor %.0fx)",
        s2_a, s2_b, 1000.0 / max(s2_b, 1e-9),
    )
    logger.info("CSV: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
