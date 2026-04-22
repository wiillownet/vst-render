"""
fxp-render — batch VST2 .fxp preset rendering via DawDreamer.

Public API:
    from fxp_render import (
        RenderConfig,
        BatchRenderer,
        ParallelBatchRenderer,
        render_preset,
    )

RenderConfig is eager (pure-Python). The renderer classes are exposed
lazily via PEP 562 __getattr__ so that `import fxp_render.worker`
inside a loky worker process does NOT transitively import dawdreamer /
numpy at module level — that import order is enforced inside
init_worker and must not be preempted.
"""
from __future__ import annotations

from .config import RenderConfig

__all__ = [
    "RenderConfig",
    "BatchRenderer",
    "ParallelBatchRenderer",
    "render_preset",
]


def __getattr__(name: str):
    if name in ("BatchRenderer", "ParallelBatchRenderer", "render_preset"):
        from . import api
        return getattr(api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
