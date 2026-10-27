"""Content hash for a file or directory, so a dataset is uploaded once."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(path: Path) -> str:
    target = path.resolve()
    digest = hashlib.sha256()
    if target.is_file():
        digest.update(b"f\0")
        digest.update(target.name.encode())
        digest.update(b"\0")
        digest.update(_file_hash(target).encode())
        return digest.hexdigest()
    files = sorted(item for item in target.rglob("*") if item.is_file())
    for item in files:
        relative = item.relative_to(target).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(_file_hash(item).encode())
        digest.update(b"\0")
    return digest.hexdigest()
