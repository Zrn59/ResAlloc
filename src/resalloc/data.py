"""Move dataset bytes without parking them on the hub.

hub: copy onto Oracle, then the hub hands the cache to the provider.
direct: this machine sends the original files straight to the provider store.
stream: same path, in fixed-size chunks, and each finished file is marked
so training can read it before the rest arrives.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from resalloc.models import Dataset, Target

CHUNK = 8 * 1024 * 1024


class ChunkSink(Protocol):
    def begin(self, relative: str, size: int) -> None:
        """Open one output file."""

    def write(self, data: bytes) -> None:
        """Accept the next chunk. Implementations must not accumulate the whole file."""

    def end(self, relative: str, digest: str) -> None:
        """Close the file and publish its done marker."""

    def complete(self) -> None:
        """Mark the dataset finished."""


def iter_chunks(path: Path, chunk: int = CHUNK):
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                return
            yield block


def list_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(path for path in root.rglob("*") if path.is_file())


def relative_name(root: Path, path: Path) -> str:
    if root.is_file():
        return root.name
    return path.relative_to(root).as_posix()


def transfer_tree(root: Path, sink: ChunkSink, chunk: int = CHUNK) -> dict[str, object]:
    """Copy a tree through the sink. Peak extra memory is one chunk."""

    overall = hashlib.sha256()
    files = list_files(root)
    try:
        for path in files:
            relative = relative_name(root, path)
            digest = hashlib.sha256()
            sink.begin(relative, path.stat().st_size)
            for block in iter_chunks(path, chunk):
                digest.update(block)
                overall.update(block)
                sink.write(block)
            sink.end(relative, digest.hexdigest())
        sink.complete()
    except Exception:
        abort = getattr(sink, "abort", None)
        if abort is not None:
            abort()
        raise
    return {"files": len(files), "hash": overall.hexdigest()}


class DirectorySink:
    """Write a dataset directory with per-file `.done` markers and a final COMPLETE."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self._handle = None
        self._tmp: Path | None = None
        self._final: Path | None = None

    def begin(self, relative: str, size: int) -> None:
        del size
        final = self.destination / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.with_name(final.name + ".partial")
        self._final = final
        self._tmp = temporary
        self._handle = temporary.open("wb")

    def write(self, data: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("write before begin")
        self._handle.write(data)

    def end(self, relative: str, digest: str) -> None:
        del relative
        if self._handle is None or self._tmp is None or self._final is None:
            raise RuntimeError("end before begin")
        self._handle.close()
        self._tmp.replace(self._final)
        self._final.with_name(self._final.name + ".done").write_text(digest + "\n", encoding="utf-8")
        self._handle = None

    def complete(self) -> None:
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "COMPLETE").write_text("ok\n", encoding="utf-8")

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._tmp is not None:
            self._tmp.unlink(missing_ok=True)
            self._tmp = None


def default_keep(dataset: Dataset, start: Target) -> str:
    if dataset.route == "hub":
        return "hub"
    if dataset.keep != "hub":
        return dataset.keep
    return str(start.to_dict()["type"])


def default_store(dataset: Dataset, start: Target) -> str | None:
    if dataset.store:
        return dataset.store
    settings = start.to_dict()
    if dataset.keep == "modal" or (dataset.route != "hub" and settings.get("type") == "modal"):
        return settings.get("volume") or "resalloc-data"
    if settings.get("type") == "kaggle" or dataset.keep == "kaggle":
        return settings.get("dataset_slug")
    return None


def describe(dataset: Dataset, start: Target) -> dict[str, object]:
    keep = default_keep(dataset, start)
    return {
        "name": dataset.name,
        "route": dataset.route,
        "keep": keep,
        "store": dataset.store or default_store(dataset, start),
        "path": dataset.path,
    }
