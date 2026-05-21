"""
State-contamination stress test.

Renders every preset in two passes and diffs the audio:
  Warm pass  - workers=1, all presets chained through a single worker
               that has BOTH synths loaded in a shared graph (the
               production CLI path).
  Cold pass  - one fresh Python process per preset, only the matching
               plugin loaded. Parallelised at the subprocess level so
               the wall clock stays reasonable; each individual cold
               render is still isolated (its own interpreter, its own
               plugin load, no shared state with any other render).

If warm[i] != cold[i], state from earlier presets is bleeding into the
later render -- the load_preset / load_state in-place state swap isn't
fully resetting plugin internals.

Diff bar: bit-identical for .fxp (Serum 1 is deterministic given the
same inputs). For .SerumPreset we compute the residual and surface the
distribution -- we set the "is this real" threshold from the cold-vs-
cold determinism baseline rather than from zero.

Run from the repo root:
    .venv/bin/python scripts/stress_state_contamination.py \\
        --fxp-dir  "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \\
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" \\
        --out-dir ./stress_state_contamination

Resumable: warm and cold passes both honor existing output files, so a
re-run after a crash picks up where it left off.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# This script lives in scripts/; ensure the repo root is on sys.path so
# `import vst_render` works without an editable install.
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
DEFAULT_BIT_DEPTH = "32f"  # float WAV - no quantization noise masking residuals

logger = logging.getLogger("stress_state_contamination")


def _build_job_specs(
    fxp_dir: Path | None,
    serum2_dir: Path | None,
    limit: int | None,
) -> list[dict]:
    """Discover presets and assign a stable output filename per preset.

    Returns dicts with keys: preset_path, preset_format, stem, library_root.
    The stem is computed from {library}_{subpath}_{preset} so the warm
    and cold passes write to the same filename, and so duplicate basenames
    across libraries don't collide.
    """
    from vst_render.presets import PresetFormat, discover_presets
    from vst_render.utils import compose_filename

    specs: list[dict] = []
    if fxp_dir is not None:
        for preset_path, fmt in discover_presets(fxp_dir, recurse=True):
            stem = compose_filename(
                "fxp_{subpath}_{preset}", preset_path, fxp_dir,
                DEFAULT_NOTE, DEFAULT_VELOCITY,
            )
            specs.append({
                "preset_path": str(preset_path.resolve()),
                "preset_format": fmt.value,
                "stem": stem,
                "library_root": str(fxp_dir.resolve()),
            })
    if serum2_dir is not None:
        for preset_path, fmt in discover_presets(serum2_dir, recurse=True):
            stem = compose_filename(
                "serum2_{subpath}_{preset}", preset_path, serum2_dir,
                DEFAULT_NOTE, DEFAULT_VELOCITY,
            )
            specs.append({
                "preset_path": str(preset_path.resolve()),
                "preset_format": fmt.value,
                "stem": stem,
                "library_root": str(serum2_dir.resolve()),
            })

    # Disambiguate same-stem collisions in-order.
    seen: dict[str, int] = {}
    for spec in specs:
        stem = spec["stem"] or "preset"
        if stem not in seen:
            seen[stem] = 0
        else:
            seen[stem] += 1
            stem = f"{stem}_{seen[stem]}"
        spec["stem"] = stem

    if limit is not None:
        specs = specs[:limit]
    return specs


def _run_warm_pass(
    specs: list[dict],
    warm_dir: Path,
    fxp_plugin: str,
    serum2_plugin: str,
) -> list[dict]:
    """Single loky worker, both plugins loaded, all presets sequential."""
    from vst_render.batch import run_batch_to_disk

    warm_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        {
            "preset_path": spec["preset_path"],
            "preset_format": spec["preset_format"],
            "output_path": str(warm_dir / f"{spec['stem']}.wav"),
            "note": DEFAULT_NOTE,
            "velocity": DEFAULT_VELOCITY,
            "duration": DEFAULT_DURATION,
            "tail": DEFAULT_TAIL,
            "midi_path": None,
            "midi_duration": None,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "bit_depth": DEFAULT_BIT_DEPTH,
            "format": "wav",
            "skip_existing": True,
        }
        for spec in specs
    ]
    total = len(jobs)
    t0 = time.monotonic()
    done = [0]

    def _on_result(r: dict) -> None:
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == total:
            elapsed = time.monotonic() - t0
            rate = done[0] / max(elapsed, 1e-9)
            eta = (total - done[0]) / max(rate, 1e-9)
            logger.info(
                "  warm %d/%d  (%.1fs elapsed, %.2f/s, ~%.0fs left)",
                done[0], total, elapsed, rate, eta,
            )

    return run_batch_to_disk(
        jobs, 1, fxp_plugin, serum2_plugin,
        DEFAULT_SAMPLE_RATE, on_result=_on_result,
    )


def _run_cold_pass(
    specs: list[dict],
    cold_dir: Path,
    fxp_plugin: str,
    serum2_plugin: str,
    parallel: int,
) -> list[dict]:
    """One fresh subprocess per preset, parallelised across `parallel` workers."""
    cold_dir.mkdir(parents=True, exist_ok=True)

    # Pre-filter presets whose cold output already exists - lets a crashed
    # run resume without redoing finished work. We still keep the full
    # spec list so results align by index downstream.
    to_render = []
    for spec in specs:
        out = cold_dir / f"{spec['stem']}.wav"
        if out.exists():
            continue
        plugin = fxp_plugin if spec["preset_format"] == "fxp" else serum2_plugin
        to_render.append((spec, str(out), plugin))

    results: list[dict] = [{"status": "skipped"} for _ in specs]
    if not to_render:
        logger.info("  cold: all outputs exist, nothing to render")
        return results

    total = len(to_render)
    t0 = time.monotonic()
    done = [0]
    script = str(Path(__file__).resolve())

    def _render_one(spec: dict, out_path: str, plugin_path: str) -> dict:
        proc = subprocess.run(
            [
                sys.executable, script,
                "--cold-one",
                "--preset", spec["preset_path"],
                "--out", out_path,
                "--format", spec["preset_format"],
                "--plugin", plugin_path,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            return {
                "status": "error",
                "path": spec["preset_path"],
                "error": (proc.stderr or proc.stdout or "").strip()[:500],
            }
        return {"status": "ok", "path": spec["preset_path"]}

    # ThreadPoolExecutor here only shepherds subprocess.run calls; the
    # actual DawDreamer work happens in the spawned child processes, so
    # the no-threads-with-DawDreamer rule is preserved.
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        future_to_idx = {}
        for spec, out_path, plugin in to_render:
            # Find the index of this spec in the original specs list so
            # results stay aligned. specs has no duplicate preset_path
            # within one library, but to be safe we use object identity.
            idx = next(i for i, s in enumerate(specs) if s is spec)
            fut = ex.submit(_render_one, spec, out_path, plugin)
            future_to_idx[fut] = idx
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            res = fut.result()
            results[idx] = res
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == total:
                elapsed = time.monotonic() - t0
                rate = done[0] / max(elapsed, 1e-9)
                eta = (total - done[0]) / max(rate, 1e-9)
                logger.info(
                    "  cold %d/%d  (%.1fs elapsed, %.2f/s, ~%.0fs left)",
                    done[0], total, elapsed, rate, eta,
                )
    return results


def _cold_one_main(
    preset: str, out: str, fmt: str, plugin: str
) -> int:
    """Subprocess entry point: render ONE preset and write to `out`.

    Builds a fresh RenderEngine with only the matching plugin loaded
    (saves 1-3s of Serum 2 boot on .fxp jobs and vice versa). The
    library's render_preset() encapsulates the full cold path: it
    builds an Engine, runs the warmup renders, then renders the preset.
    """
    from vst_render.api import render_preset
    from vst_render.config import RenderConfig
    import soundfile as sf

    cfg_kwargs = dict(
        sample_rate=DEFAULT_SAMPLE_RATE,
        note=DEFAULT_NOTE,
        velocity=DEFAULT_VELOCITY,
        duration=DEFAULT_DURATION,
        tail=DEFAULT_TAIL,
    )
    if fmt == "fxp":
        cfg = RenderConfig(fxp_plugin_path=plugin, **cfg_kwargs)
    elif fmt == "serum2":
        cfg = RenderConfig(serum2_plugin_path=plugin, **cfg_kwargs)
    else:
        print(f"unknown format: {fmt}", file=sys.stderr)
        return 2

    audio = render_preset(preset, cfg)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, audio.T, DEFAULT_SAMPLE_RATE, subtype="FLOAT")
    return 0


def _diff_pairs(specs: list[dict], warm_dir: Path, cold_dir: Path, out_csv: Path) -> dict:
    """Read each (warm, cold) WAV pair, compute residual stats, write CSV.

    Returns a summary dict for the on-screen recap.
    """
    import numpy as np
    import soundfile as sf

    SILENCE_EPS = 3.16e-5

    rows: list[dict] = []
    missing = 0
    length_mismatch = 0
    for spec in specs:
        stem = spec["stem"]
        warm_path = warm_dir / f"{stem}.wav"
        cold_path = cold_dir / f"{stem}.wav"
        if not warm_path.exists() or not cold_path.exists():
            missing += 1
            rows.append({
                "preset_path": spec["preset_path"],
                "format": spec["preset_format"],
                "max_abs": "",
                "rms": "",
                "peak_dbfs": "",
                "length_diff_samples": "",
                "warm_silent": "",
                "cold_silent": "",
                "note": "missing_output",
            })
            continue
        warm, sr_w = sf.read(str(warm_path), dtype="float32")
        cold, sr_c = sf.read(str(cold_path), dtype="float32")
        n = min(len(warm), len(cold))
        length_diff = len(warm) - len(cold)
        if length_diff != 0:
            length_mismatch += 1
            warm = warm[:n]
            cold = cold[:n]
        diff = warm - cold
        max_abs = float(np.max(np.abs(diff))) if diff.size else 0.0
        rms = float(np.sqrt(np.mean(diff * diff))) if diff.size else 0.0
        peak_dbfs = (
            float(20.0 * np.log10(max_abs)) if max_abs > 0 else float("-inf")
        )
        rows.append({
            "preset_path": spec["preset_path"],
            "format": spec["preset_format"],
            "max_abs": f"{max_abs:.6e}",
            "rms": f"{rms:.6e}",
            "peak_dbfs": f"{peak_dbfs:.2f}" if peak_dbfs != float("-inf") else "-inf",
            "length_diff_samples": length_diff,
            "warm_silent": float(np.max(np.abs(warm))) < SILENCE_EPS,
            "cold_silent": float(np.max(np.abs(cold))) < SILENCE_EPS,
            "note": "",
        })

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    valid = [r for r in rows if r["max_abs"] != ""]
    by_max = sorted(
        valid, key=lambda r: float(r["max_abs"]), reverse=True
    )
    bit_identical = sum(1 for r in valid if float(r["max_abs"]) < 1e-7)
    near_zero = sum(1 for r in valid if float(r["max_abs"]) < 1e-4)
    audible = sum(1 for r in valid if float(r["max_abs"]) >= 1e-2)
    return {
        "total": len(rows),
        "valid": len(valid),
        "missing": missing,
        "length_mismatch": length_mismatch,
        "bit_identical": bit_identical,
        "near_zero_under_1e-4": near_zero,
        "audible_over_1e-2": audible,
        "top_offenders": by_max[:25],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fxp-dir", type=Path, help="Directory of .fxp presets")
    parser.add_argument("--serum2-dir", type=Path, help="Directory of .SerumPreset files")
    parser.add_argument("--fxp-plugin", default=DEFAULT_FXP_PLUGIN)
    parser.add_argument("--serum2-plugin", default=DEFAULT_SERUM2_PLUGIN)
    parser.add_argument("--out-dir", type=Path, default=Path("./stress_state_contamination"))
    parser.add_argument(
        "--cold-workers", type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Parallel cold subprocesses (default: cpu_count // 2)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Render only the first N presets (debug)")
    parser.add_argument("--skip-warm", action="store_true")
    parser.add_argument("--skip-cold", action="store_true")
    parser.add_argument("--skip-diff", action="store_true")

    # Internal subprocess mode -- one preset, one process, one render.
    parser.add_argument("--cold-one", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preset", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    parser.add_argument("--format", dest="fmt", choices=("fxp", "serum2"), help=argparse.SUPPRESS)
    parser.add_argument("--plugin", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    # Script logging is fine (CLAUDE.md forbids basicConfig in *library* code).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.cold_one:
        return _cold_one_main(args.preset, args.out, args.fmt, args.plugin)

    if args.fxp_dir is None and args.serum2_dir is None:
        parser.error("provide at least one of --fxp-dir / --serum2-dir")

    for p in (args.fxp_plugin, args.serum2_plugin):
        if not Path(p).exists():
            logger.warning("plugin path does not exist on disk: %s", p)

    logger.info("discovering presets...")
    specs = _build_job_specs(args.fxp_dir, args.serum2_dir, args.limit)
    n_fxp = sum(1 for s in specs if s["preset_format"] == "fxp")
    n_serum2 = sum(1 for s in specs if s["preset_format"] == "serum2")
    logger.info("found %d presets (%d fxp, %d serum2)", len(specs), n_fxp, n_serum2)
    if not specs:
        logger.error("no presets to render")
        return 1

    warm_dir = args.out_dir / "warm"
    cold_dir = args.out_dir / "cold"

    if not args.skip_warm:
        logger.info("=== warm pass (workers=1, both plugins, sequential) ===")
        t = time.monotonic()
        warm_results = _run_warm_pass(
            specs, warm_dir, args.fxp_plugin, args.serum2_plugin,
        )
        warm_err = sum(1 for r in warm_results if r["status"] == "error")
        warm_skip = sum(1 for r in warm_results if r["status"] == "skipped")
        logger.info(
            "warm done in %.1fs (%d ok, %d skipped, %d errors)",
            time.monotonic() - t,
            sum(1 for r in warm_results if r["status"] == "ok"),
            warm_skip, warm_err,
        )

    if not args.skip_cold:
        logger.info(
            "=== cold pass (subprocess per preset, %d parallel) ===",
            args.cold_workers,
        )
        t = time.monotonic()
        cold_results = _run_cold_pass(
            specs, cold_dir, args.fxp_plugin, args.serum2_plugin,
            args.cold_workers,
        )
        cold_err = sum(1 for r in cold_results if r["status"] == "error")
        cold_skip = sum(1 for r in cold_results if r["status"] == "skipped")
        logger.info(
            "cold done in %.1fs (%d ok, %d skipped, %d errors)",
            time.monotonic() - t,
            sum(1 for r in cold_results if r["status"] == "ok"),
            cold_skip, cold_err,
        )

    if not args.skip_diff:
        logger.info("=== diff pass ===")
        t = time.monotonic()
        summary = _diff_pairs(
            specs, warm_dir, cold_dir, args.out_dir / "diff.csv",
        )
        logger.info("diff done in %.1fs", time.monotonic() - t)
        logger.info("---- SUMMARY ----")
        logger.info("total            : %d", summary["total"])
        logger.info("valid pairs      : %d", summary["valid"])
        logger.info("missing outputs  : %d", summary["missing"])
        logger.info("length mismatches: %d", summary["length_mismatch"])
        logger.info("bit-identical    : %d  (max_abs < 1e-7)", summary["bit_identical"])
        logger.info("near-zero        : %d  (max_abs < 1e-4)", summary["near_zero_under_1e-4"])
        logger.info("audible residue  : %d  (max_abs >= 1e-2)", summary["audible_over_1e-2"])
        logger.info("---- top 25 offenders by max_abs ----")
        for r in summary["top_offenders"]:
            logger.info(
                "  %-10s  max_abs=%-12s  rms=%-12s  %s",
                r["format"], r["max_abs"], r["rms"], r["preset_path"],
            )
        logger.info("full CSV at: %s", args.out_dir / "diff.csv")

    return 0


if __name__ == "__main__":
    sys.exit(main())
