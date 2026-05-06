from __future__ import annotations

from enum import Enum
from pathlib import Path


class PresetFormat(str, Enum):
    """Preset file format. Drives dispatch in the worker.

    Stored as a string-valued Enum so that `.value` is a stable wire
    format for the job dict (which crosses a process boundary via
    cloudpickle and is more legible if the format field is just "fxp"
    or "serum2" rather than an Enum member repr).
    """
    FXP = "fxp"
    SERUM2 = "serum2"


# Suffix → format. One place to add new formats.
_SUFFIX_TO_FORMAT: dict[str, PresetFormat] = {
    ".fxp": PresetFormat.FXP,
    ".SerumPreset": PresetFormat.SERUM2,
}


def format_for_path(path: Path) -> PresetFormat:
    """Return the PresetFormat for a path's suffix.

    Raises ValueError on an unknown suffix so callers (single-file CLI
    mode) get a clean error rather than a silent dispatch surprise.
    """
    fmt = _SUFFIX_TO_FORMAT.get(path.suffix)
    if fmt is None:
        supported = ", ".join(sorted(_SUFFIX_TO_FORMAT))
        raise ValueError(
            f"Unsupported preset suffix {path.suffix!r} on {path.name}; "
            f"supported: {supported}"
        )
    return fmt


def discover_presets(
    path: Path, recurse: bool = True
) -> list[tuple[Path, PresetFormat]]:
    """
    Discover preset files at the given path.

    Supported formats: `.fxp` (VST2 preset), `.SerumPreset` (Serum 2).

    - If `path` is a single file, infers the format from its suffix.
      Unknown suffix raises ValueError — single-file mode no longer
      "trusts the user" because a misnamed file would be dispatched to
      the wrong worker branch.
    - If `path` is a directory, globs every supported extension
      recursively (default) or only at the top level (`recurse=False`).
      Results are sorted alphabetically by absolute path.
    - Raises `FileNotFoundError` if `path` does not exist.

    Returns a list of `(absolute_path, format)` tuples.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Preset path not found: {path}")

    if path.is_file():
        return [(path.resolve(), format_for_path(path))]

    files: list[tuple[Path, PresetFormat]] = []
    for suffix, fmt in _SUFFIX_TO_FORMAT.items():
        pattern = f"*{suffix}"
        matches = path.rglob(pattern) if recurse else path.glob(pattern)
        for f in matches:
            files.append((f.resolve(), fmt))

    return sorted(files, key=lambda t: t[0])
