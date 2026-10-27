"""Keep a free reserve on the hub disk and evict old dataset cache."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from resalloc.errors import DiskWatermarkError
from resalloc.io import parse_dt, read_json, utcnow, write_json
from resalloc.names import safe_digest

DEFAULT_RESERVE_BYTES = 20 * 1024**3


def configured_reserve(explicit: int | None = None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get("RESALLOC_RESERVE_BYTES")
    if raw:
        return int(raw)
    return DEFAULT_RESERVE_BYTES


@dataclass
class CacheEntry:
    digest: str
    path: Path
    size: int
    last_used: str


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


class DiskBudget:
    def __init__(self, root: Path, reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> None:
        self.root = root
        self.reserve_bytes = reserve_bytes
        self.cache_root = root / "cache" / "datasets"

    def free_bytes(self) -> int:
        usage_root = self.root if self.root.exists() else self.root.parent
        usage_root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(usage_root).free

    def info(self) -> dict[str, int]:
        return {"free": self.free_bytes(), "reserve": self.reserve_bytes}

    def list_entries(self) -> list[CacheEntry]:
        if not self.cache_root.exists():
            return []
        entries: list[CacheEntry] = []
        for child in self.cache_root.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            last_used = "1970-01-01T00:00:00+00:00"
            if meta_path.exists():
                last_used = str(read_json(meta_path).get("last_used") or last_used)
            entries.append(
                CacheEntry(
                    digest=child.name,
                    path=child,
                    size=dir_size(child),
                    last_used=last_used,
                )
            )
        entries.sort(key=lambda item: parse_dt(item.last_used))
        return entries

    def touch(self, digest: str, name: str, size: int | None = None) -> None:
        if not safe_digest(digest):
            raise ValueError("dataset hash must be a sha256 digest")
        meta = self.cache_root / digest / "meta.json"
        payload = self.cache_root / digest / "payload"
        write_json(
            meta,
            {
                "hash": digest,
                "name": name,
                "bytes": size if size is not None else dir_size(payload),
                "last_used": utcnow().isoformat(),
            },
        )

    def evict(self, protected: set[str], needed_bytes: int = 0) -> int:
        """Delete oldest unreferenced cache entries until the reserve holds.

        Returns the number of bytes removed.
        """

        removed = 0
        for entry in self.list_entries():
            if self.free_bytes() - needed_bytes >= self.reserve_bytes:
                break
            if entry.digest in protected:
                continue
            shutil.rmtree(entry.path)
            removed += entry.size
        return removed

    def ensure_incoming(self, needed_bytes: int, protected: set[str]) -> None:
        self.evict(protected, needed_bytes)
        if self.free_bytes() - needed_bytes < self.reserve_bytes:
            raise DiskWatermarkError(
                "hub disk is inside the reserve; refusing a new upload. "
                f"free={self.free_bytes()} reserve={self.reserve_bytes} need={needed_bytes}"
            )
