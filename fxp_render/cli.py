"""
Typer CLI entry point. Wires user arguments into a job list, resolves
collisions, and dispatches to the disk-writing batch path.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer

from .batch import run_batch_to_disk
from .presets import discover_presets
from .utils import assign_output_paths, compose_filename, get_midi_duration

logger = logging.getLogger("fxp_render")

app = typer.Typer(add_completion=False, help="Batch-render VST2 .fxp presets to audio.")


def _setup_logging(verbose: bool) -> None:
    """Only the CLI configures logging — library code uses a named logger."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def render(
    plugin: Path = typer.Argument(..., help="Path to VST2 plugin .dll (e.g. Serum_x64.dll)."),
    presets: Path = typer.Argument(..., help="Path to a single .fxp or a directory of them."),
    output: Path = typer.Argument(..., help="Output directory (created if missing)."),
    note: Optional[int] = typer.Option(None, min=0, max=127, help="MIDI note (0-127). Default 48 (C3)."),
    velocity: int = typer.Option(127, min=1, max=127, help="MIDI velocity (1-127)."),
    duration: float = typer.Option(1.0, min=0.0, help="Note-on duration in seconds."),
    tail: float = typer.Option(1.0, min=0.0, help="Release silence in seconds."),
    sample_rate: int = typer.Option(44100, "--sample-rate", help="Output sample rate in Hz."),
    bit_depth: str = typer.Option("16", "--bit-depth", help="Output bit depth: 16, 24, or 32f."),
    fmt: str = typer.Option("wav", "--format", help="Output container: wav or npy."),
    filename_template: str = typer.Option(
        "{preset}", "--filename-template",
        help="Filename template. Vars: {preset} {note} {velocity} {folder} {subpath}.",
    ),
    midi: Optional[Path] = typer.Option(None, "--midi", help="Path to a .mid file (overrides --note)."),
    workers: int = typer.Option(-1, "--workers", help="Parallel workers. -1 = cpu_count - 1."),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip if output file already exists."),
    no_recurse: bool = typer.Option(False, "--no-recurse", help="Do not recurse into subdirectories."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print presets that would render and exit."),
    verbose: bool = typer.Option(False, "--verbose", help="Per-preset status logging."),
) -> None:
    _setup_logging(verbose)

    # --note and --midi are mutually exclusive. Typer can't detect a
    # user-set default, so we use None sentinel + manual check.
    if midi is not None and note is not None:
        raise typer.BadParameter(
            "--note and --midi are mutually exclusive. Use --midi to render a "
            "MIDI sequence, or --note to render a single note."
        )
    if note is None:
        note = 48

    if bit_depth not in ("16", "24", "32f"):
        raise typer.BadParameter(f"--bit-depth must be 16, 24, or 32f (got {bit_depth!r}).")
    if fmt not in ("wav", "npy"):
        raise typer.BadParameter(f"--format must be wav or npy (got {fmt!r}).")

    if not plugin.exists():
        typer.echo(f"Plugin not found: {plugin}", err=True)
        raise typer.Exit(code=2)
    if not presets.exists():
        typer.echo(f"Presets path not found: {presets}", err=True)
        raise typer.Exit(code=2)

    preset_files = discover_presets(presets, recurse=not no_recurse)
    if not preset_files:
        typer.echo(f"No .fxp files found under {presets}", err=True)
        raise typer.Exit(code=0)

    # Single-file mode: presets_root=None so {subpath} collapses out.
    presets_root: Path | None = presets if presets.is_dir() else None

    # Compute MIDI duration once in the main process — all workers share it.
    midi_duration: float | None = None
    midi_str: str | None = None
    if midi is not None:
        if not midi.exists():
            typer.echo(f"MIDI file not found: {midi}", err=True)
            raise typer.Exit(code=2)
        midi_duration = get_midi_duration(midi)
        midi_str = str(midi.resolve())

    extension = ".npy" if fmt == "npy" else ".wav"
    jobs: list[dict] = []
    for p in preset_files:
        stem = compose_filename(filename_template, p, presets_root, note, velocity)
        jobs.append({
            "preset_path": str(p.resolve()),
            "filename_stem": stem,
            "note": note,
            "velocity": velocity,
            "duration": duration,
            "tail": tail,
            "midi_path": midi_str,
            "midi_duration": midi_duration,
            "sample_rate": sample_rate,
            "bit_depth": bit_depth,
            "format": fmt,
            "skip_existing": skip_existing,
        })
    assign_output_paths(jobs, output, extension)

    if dry_run:
        typer.echo(f"Would render {len(jobs)} preset(s):")
        for j in jobs:
            typer.echo(f"  {j['preset_path']}  ->  {j['output_path']}")
        raise typer.Exit(code=0)

    output.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Rendering {len(jobs)} preset(s) with {workers} workers…")
    results = run_batch_to_disk(jobs, workers, str(plugin.resolve()), sample_rate)

    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = [r for r in results if r["status"] == "error"]

    typer.echo(f"Done: {ok} rendered, {skipped} skipped, {len(errors)} failed.")
    for r in errors:
        typer.echo(f"  FAIL {r.get('path')}: {r.get('error')}", err=True)
    raise typer.Exit(code=1 if errors else 0)


if __name__ == "__main__":
    app()
