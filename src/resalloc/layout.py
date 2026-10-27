"""Directories on the hub disk."""

from __future__ import annotations

from pathlib import Path

from resalloc.names import safe_digest, safe_job_id


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure(self) -> None:
        for relative in ("inbox", "jobs", "cache/datasets", "agents", "commands"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)

    def inbox(self, job_id: str) -> Path:
        _require_job_id(job_id)
        return self.root / "inbox" / job_id

    def job(self, job_id: str) -> Path:
        _require_job_id(job_id)
        return self.root / "jobs" / job_id

    def cache(self, digest: str) -> Path:
        if not safe_digest(digest):
            raise ValueError("dataset hash must be a sha256 digest")
        return self.root / "cache" / "datasets" / digest

    def agent_heartbeat(self) -> Path:
        return self.root / "agents" / "laptop.json"

    def jobs_root(self) -> Path:
        return self.root / "jobs"

    def inbox_root(self) -> Path:
        return self.root / "inbox"


def _require_job_id(job_id: str) -> None:
    if not safe_job_id(job_id):
        raise ValueError(f"unsafe job id: {job_id}")
