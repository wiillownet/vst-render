from __future__ import annotations

from pathlib import Path


def discover_presets(path: Path, recurse: bool = True) -> list[Path]:
    """
    Discover `.fxp` preset files at the given path.

    - If `path` is a single file, returns `[path.resolve()]` regardless of
      extension (the caller is responsible for handing this function a
      preset path; single-file mode trusts the user).
    - If `path` is a directory, globs `*.fxp` recursively (default) or
      only at the top level (`recurse=False`). Results are sorted
      alphabetically and resolved to absolute paths.
    - Raises `FileNotFoundError` if `path` does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Preset path not found: {path}")

    if path.is_file():
        return [path.resolve()]

    pattern = "*.fxp"
    files = path.rglob(pattern) if recurse else path.glob(pattern)
    return sorted(f.resolve() for f in files)
