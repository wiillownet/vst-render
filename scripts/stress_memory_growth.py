"""
Long-run memory growth sweep.

Renders the preset library N times (default 3) through a single loky pool,
sampling per-worker RSS between passes. Detects memory leaks via monotonic
RSS growth across passes.

loky's get_reusable_executor reuses the same worker processes when its
config matches, so consecutive run_batch_to_disk calls with identical
plugin paths + workers + sample_rate hit the same PIDs every pass. We
sample those PIDs via `ps` after each pass (pool idle) and compare.

What "growth" means here:
- macOS RSS includes shared memory, so the absolute number is dominated by
  the JUCE/Serum DLL footprint shared across workers. The signal of
  interest is the *delta* between pass 1 (warm) and pass N. A monotonic
  growth of N × constant across passes implies a leak proportional to
  render count.

Defaults: workers=8, passes=3. Render the full library each pass (no
--limit) since leaks proportional to render count only show with enough
samples. Override via --limit for a smoke test.

Run from the repo root:
    .venv/bin/python scripts/stress_memory_growth.py \\
        --fxp-dir   "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \\
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" \\
        --workers 8 --passes 3
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import shutil
import subprocess
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
DEFAULT_BIT_DEPTH = "16"

logger = logging.getLogger("stress_memory_growth")


def _list_worker_pids() -> list[int]:
    """Direct children of this process that look like loky Python workers."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(os.getpid())],
            stderr=subprocess.DEVNULL,
        ).decode()
    except subprocess.CalledProcessError:
        return []
    pids: list[int] = []
    for tok in out.split():
        tok = tok.strip()
        if not tok:
            continue
        pid = int(tok)
        try:
            comm = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "comm="],
                stderr=subprocess.DEVNULL,
            ).decode().strip().lower()
        except subprocess.CalledProcessError:
            continue
        # loky workers + the semaphore/resource_tracker are both python.
        # The resource_tracker has a tiny RSS (~10 MB) compared to a
        # render worker (hundreds of MB), so we keep all "python" children
        # and label by RSS in the report.
        if "python" in comm:
            pids.append(pid)
    return sorted(pids)


def _rss_mb(pid: int) -> float | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "rss="],
            stderr=subprocess.DEVNULL,
        )
        return int(out.decode().strip()) / 1024.0  # KB -> MB
    except (subprocess.CalledProcessError, ValueError):
        return None


def _build_jobs(
    fxp_dir: Path | None, serum2_dir: Path | None,
    out_dir: Path, limit: int | None,
) -> list[dict]:
    from vst_render.presets import discover_presets
    from vst_render.utils import compose_filename

    jobs: list[dict] = []
    for src, fmt_label, template in (
        (fxp_dir, "fxp", "fxp_{subpath}_{preset}"),
        (serum2_dir, "serum2", "serum2_{subpath}_{preset}"),
    ):
        if src is None:
            continue
        for preset_path, fmt in discover_presets(src, recurse=True):
            stem = compose_filename(
                template, preset_path, src, DEFAULT_NOTE, DEFAULT_VELOCITY,
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


def _run_pass(
    pass_idx: int, jobs: list[dict], out_dir: Path, workers: int,
    fxp_plugin: str | None, serum2_plugin: str | None,
) -> dict:
    from vst_render.batch import run_batch_to_disk

    run_dir = out_dir / f"pass_{pass_idx}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    pass_jobs = []
    for j in jobs:
        nj = dict(j)
        nj["output_path"] = str(run_dir / Path(j["output_path"]).name)
        pass_jobs.append(nj)

    t0 = time.monotonic()
    results = run_batch_to_disk(
        pass_jobs, workers, fxp_plugin, serum2_plugin,
        DEFAULT_SAMPLE_RATE, on_result=None,
    )
    elapsed = time.monotonic() - t0
    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")

    shutil.rmtree(run_dir, ignore_errors=True)

    return {
        "pass": pass_idx,
        "n": len(pass_jobs),
        "ok": ok,
        "error": err,
        "elapsed_s": elapsed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--fxp-dir", type=Path)
    parser.add_argument("--serum2-dir", type=Path)
    parser.add_argument("--fxp-plugin", default=DEFAULT_FXP_PLUGIN)
    parser.add_argument("--serum2-plugin", default=DEFAULT_SERUM2_PLUGIN)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None,
                        help="Truncate the job list per pass (smoke test).")
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path("./stress_memory_growth"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.fxp_dir is None and args.serum2_dir is None:
        parser.error("provide at least one of --fxp-dir / --serum2-dir")

    fxp_plugin = args.fxp_plugin if args.fxp_dir is not None else None
    serum2_plugin = args.serum2_plugin if args.serum2_dir is not None else None

    args.out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("discovering presets...")
    jobs = _build_jobs(args.fxp_dir, args.serum2_dir, args.out_dir, args.limit)
    n_fxp = sum(1 for j in jobs if j["preset_format"] == "fxp")
    n_serum2 = sum(1 for j in jobs if j["preset_format"] == "serum2")
    logger.info(
        "library: %d presets (%d fxp, %d serum2); workers=%d; passes=%d",
        len(jobs), n_fxp, n_serum2, args.workers, args.passes,
    )
    if not jobs:
        logger.error("no presets to render")
        return 1

    # Track RSS per worker PID across passes. PIDs that disappear (crashed
    # workers) drop out; new PIDs after a respawn show up as new rows.
    pid_rss_history: dict[int, list[float | None]] = {}
    pass_summaries: list[dict] = []

    # Sample before pass 1 to capture "cold pool" baseline. There won't
    # be any worker PIDs yet, but the call is cheap.
    logger.info("sampling RSS before any pass (workers not yet spawned)...")
    pre_pids = _list_worker_pids()
    logger.info("  pre-pass worker children: %d", len(pre_pids))

    for pass_idx in range(1, args.passes + 1):
        logger.info("=== pass %d/%d ===", pass_idx, args.passes)
        summary = _run_pass(
            pass_idx, jobs, args.out_dir, args.workers,
            fxp_plugin, serum2_plugin,
        )
        logger.info(
            "  pass %d: %.1fs / %d jobs (ok=%d err=%d)",
            pass_idx, summary["elapsed_s"], summary["n"],
            summary["ok"], summary["error"],
        )
        # Sample immediately after the pass completes (pool idle).
        pids = _list_worker_pids()
        rss_now = {pid: _rss_mb(pid) for pid in pids}
        for pid in pids:
            if pid not in pid_rss_history:
                pid_rss_history[pid] = [None] * (pass_idx - 1)
            pid_rss_history[pid].append(rss_now[pid])
        # Pad any pid that disappeared.
        for pid, history in pid_rss_history.items():
            if len(history) < pass_idx:
                history.append(None)
        summary["pids"] = pids
        summary["rss_mb_per_worker"] = rss_now
        summary["total_rss_mb"] = sum(
            v for v in rss_now.values() if v is not None
        )
        pass_summaries.append(summary)
        logger.info(
            "  RSS: %d workers, %.0f MB total (%s)",
            len(pids), summary["total_rss_mb"],
            ", ".join(
                f"{int(v)}MB" for v in rss_now.values() if v is not None
            ),
        )

    # Write CSV: one row per worker PID, one column per pass.
    csv_path = args.out_dir / "memory.csv"
    cols = ["pid"] + [f"pass{i}_mb" for i in range(1, args.passes + 1)]
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for pid, history in sorted(pid_rss_history.items()):
            row = [pid] + [
                f"{v:.1f}" if v is not None else "" for v in history
            ]
            writer.writerow(row)

    logger.info("---- SUMMARY ----")
    logger.info(
        "%-8s %-10s %-10s",
        "pass", "elapsed_s", "total_rss_mb",
    )
    for s in pass_summaries:
        logger.info(
            "%-8d %-10.1f %-10.0f",
            s["pass"], s["elapsed_s"], s["total_rss_mb"],
        )

    logger.info("---- PER-WORKER RSS (MB) ----")
    header = "pid       " + " ".join(f"p{i:<8d}" for i in range(1, args.passes + 1))
    logger.info(header)
    for pid, history in sorted(pid_rss_history.items()):
        row = " ".join(
            f"{v:<9.0f}" if v is not None else "—        " for v in history
        )
        logger.info("%-9d %s", pid, row)

    # Growth analysis: per-worker delta(pass_N - pass_1) and avg/total.
    if args.passes >= 2:
        deltas = []
        for pid, history in pid_rss_history.items():
            if history[0] is None or history[-1] is None:
                continue
            deltas.append(history[-1] - history[0])
        if deltas:
            avg_growth = sum(deltas) / len(deltas)
            max_growth = max(deltas)
            logger.info("---- GROWTH (pass %d vs pass 1) ----", args.passes)
            logger.info(
                "per-worker avg=%.1f MB, max=%.1f MB (n=%d workers)",
                avg_growth, max_growth, len(deltas),
            )
            total_first = sum(
                history[0] for history in pid_rss_history.values()
                if history[0] is not None
            )
            total_last = sum(
                history[-1] for history in pid_rss_history.values()
                if history[-1] is not None
            )
            logger.info(
                "pool total: pass 1 = %.0f MB, pass %d = %.0f MB (delta %+.0f MB)",
                total_first, args.passes, total_last,
                total_last - total_first,
            )
    logger.info("CSV: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
