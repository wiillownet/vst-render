"""
Executor management + batch dispatch. Callers never talk to loky
directly — they go through ParallelBatchRenderer in api.py (or the CLI
via run_batch_to_disk).
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import as_completed
from typing import Callable, Iterator

from loky import get_reusable_executor

from .worker import init_worker, render_to_disk, render_to_memory

logger = logging.getLogger("fxp_render")


def resolve_worker_count(workers: int) -> int:
    """-1 -> cpu_count - 1 (floor 1); otherwise max(1, workers)."""
    if workers == -1:
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, workers)


def _get_executor(workers: int, plugin_path: str, sample_rate: int):
    """
    Build the reusable executor with our init_worker. 30-minute idle
    timeout matches CLAUDE.md — lower values cause cold-start surprises
    for long-running embedders.
    """
    return get_reusable_executor(
        max_workers=resolve_worker_count(workers),
        initializer=init_worker,
        initargs=(plugin_path, sample_rate),
        timeout=1800,
    )


def run_batch_to_disk(
    jobs: list[dict],
    workers: int,
    plugin_path: str,
    sample_rate: int,
    on_result: Callable[[dict], None] | None = None,
) -> list[dict]:
    """
    CLI/batch entry: submit every job to the pool, return results in
    input order. `on_result`, if provided, is called once per job as each
    future completes — use it to drive a progress bar or per-preset log.
    Per-job errors become `{"status": "error", ...}` dicts.

    If a worker process crashes (TerminatedWorkerError / BrokenProcessPool)
    the current executor reference is permanently flagged broken — every
    future submitted to it after that will also raise. We surface each
    broken future as an error result and let the caller decide whether to
    rerun; `--skip-existing` makes re-runs idempotent for the jobs that
    already landed on disk.
    """
    executor = _get_executor(workers, plugin_path, sample_rate)
    futures = {executor.submit(render_to_disk, job): idx for idx, job in enumerate(jobs)}
    results: list[dict | None] = [None] * len(jobs)
    for future in as_completed(futures):
        idx = futures[future]
        try:
            result = future.result()
        except Exception as exc:
            job = jobs[idx]
            logger.error("Worker error for %s: %s", job.get("preset_path"), exc)
            result = {
                "status": "error",
                "path": job.get("preset_path"),
                "error": str(exc),
            }
        results[idx] = result
        if on_result is not None:
            on_result(result)
    return results  # type: ignore[return-value]  # all slots filled by the loop above


def iter_batch_to_memory(
    jobs: list[dict], workers: int, plugin_path: str, sample_rate: int
) -> Iterator[dict]:
    """
    Library entry: yield `{"status", "path", "audio" | "error"}` dicts as
    each job completes (unordered — driven by whichever worker finishes
    first). Callers building an ordered result dict collect all yields.
    """
    executor = _get_executor(workers, plugin_path, sample_rate)
    futures = {executor.submit(render_to_memory, job): job for job in jobs}
    for future in as_completed(futures):
        job = futures[future]
        try:
            yield future.result()
        except Exception as exc:
            logger.error("Worker error for %s: %s", job.get("preset_path"), exc)
            yield {
                "status": "error",
                "path": job.get("preset_path"),
                "error": str(exc),
            }
