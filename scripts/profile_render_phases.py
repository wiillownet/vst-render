"""
Phase-by-phase timing of the render pipeline.

Two profiles:
  1. Per-render breakdown in the warm path - the cost per preset once
     the worker is hot. Production-relevant: this is what the CLI pays
     for each job inside a long-running batch.
  2. Per-subprocess breakdown in the cold path - the one-shot cost of
     starting fresh: Python boot, dawdreamer import, plugin load,
     warmup render. Production pays this ONCE per worker; the stress
     harness pays it 1491 times.

The script can run either profile alone or both. Sample size is small
(default 30 fxp + 30 serum2) - we don't need a million renders to know
where the milliseconds go.

Usage:
    .venv/bin/python scripts/profile_render_phases.py \\
        --fxp-dir "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \\
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" \\
        --sample 30
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_FXP_PLUGIN = "/Library/Audio/Plug-Ins/VST/Serum.vst"
DEFAULT_SERUM2_PLUGIN = "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
SAMPLE_RATE = 44100
BUFFER_SIZE = 512
NOTE = 48
VELOCITY = 127
DURATION = 1.0
TAIL = 1.0


def _pick_presets(root: Path, suffix: str, n: int) -> list[Path]:
    """Deterministic sample: first N presets by sorted path."""
    paths = sorted(p for p in root.rglob(f"*{suffix}") if p.is_file())
    return paths[:n]


def _fmt_stats(label: str, values_ms: list[float]) -> str:
    if not values_ms:
        return f"  {label:<30} empty"
    vs = sorted(values_ms)
    n = len(vs)
    p50 = vs[n // 2]
    p90 = vs[min(int(n * 0.9), n - 1)]
    p99 = vs[min(int(n * 0.99), n - 1)]
    mean = statistics.fmean(vs)
    return (
        f"  {label:<30} n={n:<3} mean={mean:7.2f}ms  "
        f"p50={p50:7.2f}ms  p90={p90:7.2f}ms  p99={p99:7.2f}ms  "
        f"min={vs[0]:7.2f}ms  max={vs[-1]:7.2f}ms"
    )


def profile_warm(fxp_dir: Path | None, serum2_dir: Path | None,
                 fxp_plugin: str, serum2_plugin: str, sample: int,
                 out_dir: Path) -> None:
    """Build a single worker (in-process), then time each render phase
    across N fxp + N serum2 presets, reporting the distribution."""
    import dawdreamer as daw
    import numpy as np
    import soundfile as sf
    from serum2_preset_loader import convert_preset_file

    # Boot timings (one-shot, recorded but not aggregated)
    print("=== warm boot ===")
    boot: dict[str, float] = {}

    if fxp_dir is not None:
        t = time.perf_counter()
        eng_fxp = daw.RenderEngine(SAMPLE_RATE, BUFFER_SIZE)
        boot["fxp_RenderEngine"] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        synth_fxp = eng_fxp.make_plugin_processor("fxp_synth", str(Path(fxp_plugin).resolve()))
        boot["fxp_make_plugin_processor"] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        eng_fxp.load_graph([(synth_fxp, [])])
        boot["fxp_load_graph"] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        synth_fxp.clear_midi()
        synth_fxp.add_midi_note(NOTE, VELOCITY, 0.0, 0.05)
        eng_fxp.render(0.1)
        boot["fxp_warmup_render"] = (time.perf_counter() - t) * 1000

    if serum2_dir is not None:
        t = time.perf_counter()
        eng_s2 = daw.RenderEngine(SAMPLE_RATE, BUFFER_SIZE)
        boot["serum2_RenderEngine"] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        synth_s2 = eng_s2.make_plugin_processor("serum2_synth", str(Path(serum2_plugin).resolve()))
        boot["serum2_make_plugin_processor"] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        eng_s2.load_graph([(synth_s2, [])])
        boot["serum2_load_graph"] = (time.perf_counter() - t) * 1000

        tmpdir = Path(tempfile.mkdtemp(prefix="profile_serum2_"))
        state_path = tmpdir / "state.bin"

        t = time.perf_counter()
        synth_s2.clear_midi()
        synth_s2.add_midi_note(NOTE, VELOCITY, 0.0, 0.05)
        eng_s2.render(0.1)
        boot["serum2_warmup_render"] = (time.perf_counter() - t) * 1000

    for k, v in boot.items():
        print(f"  {k:<30} {v:7.2f}ms")

    # Per-render timings
    phases_fxp: dict[str, list[float]] = {
        "load_preset": [], "clear_add_midi": [],
        "engine_render": [], "get_audio": [], "sf_write": [],
        "TOTAL_per_render": [],
    }
    phases_s2: dict[str, list[float]] = {
        "convert_preset_file": [], "write_bytes": [], "load_state": [],
        "clear_add_midi": [],
        "engine_render": [], "get_audio": [], "sf_write": [],
        "TOTAL_per_render": [],
    }

    out_warm = out_dir / "warm"
    out_warm.mkdir(parents=True, exist_ok=True)

    if fxp_dir is not None:
        presets = _pick_presets(fxp_dir, ".fxp", sample)
        print(f"\n=== warm fxp renders (n={len(presets)}) ===")
        for p in presets:
            t_total = time.perf_counter()
            t = time.perf_counter()
            synth_fxp.load_preset(str(p.resolve()))
            phases_fxp["load_preset"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            synth_fxp.clear_midi()
            synth_fxp.add_midi_note(NOTE, VELOCITY, 0.0, DURATION)
            phases_fxp["clear_add_midi"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            eng_fxp.render(DURATION + TAIL)
            phases_fxp["engine_render"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            audio = eng_fxp.get_audio()
            phases_fxp["get_audio"].append((time.perf_counter() - t) * 1000)

            out_path = out_warm / f"fxp_{p.stem}.wav"
            t = time.perf_counter()
            sf.write(str(out_path), audio.T, SAMPLE_RATE, subtype="FLOAT")
            phases_fxp["sf_write"].append((time.perf_counter() - t) * 1000)

            phases_fxp["TOTAL_per_render"].append((time.perf_counter() - t_total) * 1000)

        for label, vs in phases_fxp.items():
            print(_fmt_stats(label, vs))

    if serum2_dir is not None:
        presets = _pick_presets(serum2_dir, ".SerumPreset", sample)
        print(f"\n=== warm serum2 renders (n={len(presets)}) ===")
        for p in presets:
            t_total = time.perf_counter()
            t = time.perf_counter()
            state_blob = convert_preset_file(p)
            phases_s2["convert_preset_file"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            state_path.write_bytes(state_blob)
            phases_s2["write_bytes"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            synth_s2.load_state(str(state_path))
            phases_s2["load_state"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            synth_s2.clear_midi()
            synth_s2.add_midi_note(NOTE, VELOCITY, 0.0, DURATION)
            phases_s2["clear_add_midi"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            eng_s2.render(DURATION + TAIL)
            phases_s2["engine_render"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            audio = eng_s2.get_audio()
            phases_s2["get_audio"].append((time.perf_counter() - t) * 1000)

            out_path = out_warm / f"serum2_{p.stem}.wav"
            t = time.perf_counter()
            sf.write(str(out_path), audio.T, SAMPLE_RATE, subtype="FLOAT")
            phases_s2["sf_write"].append((time.perf_counter() - t) * 1000)

            phases_s2["TOTAL_per_render"].append((time.perf_counter() - t_total) * 1000)

        for label, vs in phases_s2.items():
            print(_fmt_stats(label, vs))


def profile_cold(fxp_dir: Path | None, serum2_dir: Path | None,
                 fxp_plugin: str, serum2_plugin: str, sample: int,
                 out_dir: Path) -> None:
    """Run subprocess-per-preset across a small sample; each child
    self-times its own boot + render phases and writes a JSON line."""
    out_cold = out_dir / "cold"
    out_cold.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())

    targets: list[tuple[str, Path, str]] = []
    if fxp_dir is not None:
        for p in _pick_presets(fxp_dir, ".fxp", sample):
            targets.append(("fxp", p, fxp_plugin))
    if serum2_dir is not None:
        for p in _pick_presets(serum2_dir, ".SerumPreset", sample):
            targets.append(("serum2", p, serum2_plugin))

    cold_phases: dict[str, dict[str, list[float]]] = {
        "fxp": {}, "serum2": {},
    }

    print(f"\n=== cold subprocesses (n={len(targets)}) ===")
    for fmt, preset, plugin in targets:
        out_path = out_cold / f"{fmt}_{preset.stem}.wav"
        t_outer = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, script,
             "--cold-one",
             "--preset", str(preset.resolve()),
             "--out", str(out_path),
             "--format", fmt,
             "--plugin", plugin],
            capture_output=True, text=True, timeout=60,
        )
        outer_elapsed = (time.perf_counter() - t_outer) * 1000
        if proc.returncode != 0:
            print(f"  ERR  {fmt} {preset.name}: {proc.stderr.strip()[:200]}")
            continue
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            print(f"  ERR  {fmt} {preset.name}: bad JSON output ({exc})")
            continue
        data["wall_outer"] = outer_elapsed
        for k, v in data.items():
            cold_phases[fmt].setdefault(k, []).append(v)

    for fmt in ("fxp", "serum2"):
        if not cold_phases[fmt]:
            continue
        print(f"\n--- cold {fmt} phases ---")
        for label, vs in cold_phases[fmt].items():
            print(_fmt_stats(label, vs))


def _cold_one(preset: str, out: str, fmt: str, plugin: str) -> int:
    """Child entry point. Times each phase and prints a single JSON line
    on stdout for the parent to parse."""
    t_start = time.perf_counter()

    t = time.perf_counter()
    import dawdreamer as daw
    t_import_daw = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    import numpy as np  # noqa: F401
    import soundfile as sf
    t_import_np_sf = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    if fmt == "serum2":
        from serum2_preset_loader import convert_preset_file
    t_import_s2pl = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    eng = daw.RenderEngine(SAMPLE_RATE, BUFFER_SIZE)
    t_engine = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    synth = eng.make_plugin_processor("synth", str(Path(plugin).resolve()))
    t_plugin = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    eng.load_graph([(synth, [])])
    t_graph = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    synth.clear_midi()
    synth.add_midi_note(NOTE, VELOCITY, 0.0, 0.05)
    eng.render(0.1)
    t_warmup = (time.perf_counter() - t) * 1000

    if fmt == "fxp":
        t = time.perf_counter()
        synth.load_preset(str(Path(preset).resolve()))
        t_load = (time.perf_counter() - t) * 1000
    else:
        t = time.perf_counter()
        blob = convert_preset_file(Path(preset))
        sp = Path(tempfile.mkdtemp(prefix="profile_cold_")) / "state.bin"
        sp.write_bytes(blob)
        synth.load_state(str(sp))
        t_load = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    synth.clear_midi()
    synth.add_midi_note(NOTE, VELOCITY, 0.0, DURATION)
    eng.render(DURATION + TAIL)
    audio = eng.get_audio()
    t_render = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, audio.T, SAMPLE_RATE, subtype="FLOAT")
    t_write = (time.perf_counter() - t) * 1000

    total = (time.perf_counter() - t_start) * 1000
    print(json.dumps({
        "import_dawdreamer": t_import_daw,
        "import_np_sf": t_import_np_sf,
        "import_serum2_loader": t_import_s2pl,
        "RenderEngine": t_engine,
        "make_plugin_processor": t_plugin,
        "load_graph": t_graph,
        "warmup_render": t_warmup,
        "load_preset_or_state": t_load,
        "render_get_audio": t_render,
        "sf_write": t_write,
        "TOTAL_in_child": total,
    }))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fxp-dir", type=Path)
    parser.add_argument("--serum2-dir", type=Path)
    parser.add_argument("--fxp-plugin", default=DEFAULT_FXP_PLUGIN)
    parser.add_argument("--serum2-plugin", default=DEFAULT_SERUM2_PLUGIN)
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--out-dir", type=Path, default=Path("./profile_render_phases"))
    parser.add_argument("--skip-warm", action="store_true")
    parser.add_argument("--skip-cold", action="store_true")
    parser.add_argument("--cold-one", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preset", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    parser.add_argument("--format", dest="fmt", choices=("fxp", "serum2"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--plugin", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.cold_one:
        return _cold_one(args.preset, args.out, args.fmt, args.plugin)

    if args.fxp_dir is None and args.serum2_dir is None:
        parser.error("provide at least one of --fxp-dir / --serum2-dir")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_warm:
        profile_warm(args.fxp_dir, args.serum2_dir,
                     args.fxp_plugin, args.serum2_plugin,
                     args.sample, args.out_dir)
    if not args.skip_cold:
        profile_cold(args.fxp_dir, args.serum2_dir,
                     args.fxp_plugin, args.serum2_plugin,
                     args.sample, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
