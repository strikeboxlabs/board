"""Filesystem helpers for the evidence store.

Used by the dispatcher (walk + hash an intent's evidence directory after
it concludes) and by the server (fill in missing hashes when evidence is
registered directly through the API).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024

_KIND_BY_SUFFIX = {
    ".png": "screenshot",
    ".jpg": "screenshot",
    ".jpeg": "screenshot",
    ".gif": "screenshot",
    ".webp": "screenshot",
    ".pcap": "capture",
    ".pcapng": "capture",
    ".har": "capture",
    ".http": "capture",
    ".req": "capture",
    ".resp": "capture",
    ".log": "capture",
    ".json": "data",
    ".csv": "data",
    ".xml": "data",
    ".yaml": "data",
    ".yml": "data",
    ".md": "notes",
    ".txt": "notes",
    ".py": "script",
    ".sh": "script",
    ".rb": "script",
    ".js": "script",
    ".ts": "script",
    ".go": "script",
    ".ps1": "script",
}


def infer_evidence_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return _KIND_BY_SUFFIX.get(suffix, "data")


def hash_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256 hex, size in bytes)`` for a regular file."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def walk_evidence_files(directory: Path) -> list[Path]:
    """All regular files under ``directory`` (recursively), sorted for stable ids.

    Hidden files and directories are skipped; they are worker tooling
    droppings, not artefacts.
    """
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for child in sorted(directory.rglob("*")):
        if any(part.startswith(".") for part in child.relative_to(directory).parts):
            continue
        if child.is_file():
            files.append(child)
    return files
