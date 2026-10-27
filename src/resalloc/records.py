"""Journal and command files. The laptop writes these; the hub applies them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from resalloc.io import read_json, write_json
from resalloc.layout import Layout


def write_command(root: Path, job_id: str, name: str, payload: dict[str, Any] | None = None) -> None:
    write_json(Layout(root).job(job_id) / "commands" / f"{name}.json", payload or {})


def read_journal(root: Path, job_id: str) -> dict[str, Any]:
    layout = Layout(root)
    path = layout.job(job_id) / "journal.json"
    if path.is_file():
        return read_json(path)
    inbox = layout.inbox(job_id) / "job.json"
    if inbox.is_file():
        spec = read_json(inbox)
        return {
            "id": str(spec.get("id") or job_id),
            "state": "queued",
            "reason": "waiting for the hub",
        }
    raise FileNotFoundError(path)


def list_journals(root: Path) -> list[dict[str, Any]]:
    jobs = Layout(root).jobs_root()
    if not jobs.exists():
        return []
    found = []
    for path in sorted(jobs.iterdir()):
        journal = path / "journal.json"
        if journal.exists():
            found.append(read_json(journal))
    return found


def protected_hashes(root: Path) -> list[str]:
    terminal = {"stopped", "failed", "completed"}
    found: set[str] = set()
    for journal in list_journals(root):
        if journal.get("state") in terminal:
            continue
        for item in journal.get("datasets") or []:
            if item.get("hash"):
                found.add(str(item["hash"]))
    return sorted(found)
