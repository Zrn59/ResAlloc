"""Provider protocol and the handle stored in the journal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from resalloc.models import Limits
from resalloc.policy import AccountSnapshot, Signal


@dataclass
class RunningJob:
    provider: str
    job_id: str
    remote_id: str
    connection: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "job_id": self.job_id,
            "remote_id": self.remote_id,
            "connection": self.connection,
        }


def running_job_from_dict(data: dict[str, Any]) -> RunningJob:
    return RunningJob(
        provider=str(data["provider"]),
        job_id=str(data["job_id"]),
        remote_id=str(data["remote_id"]),
        connection=dict(data.get("connection") or {}),
    )


@dataclass
class JobLaunch:
    job_id: str
    job_dir: Path
    script: Path
    requirements: Path | None
    datasets: list[Path]
    checkpoint_dir: Path
    runtime_dir: Path
    limits: Limits
    settings: dict[str, Any]
    args: list[str]
    lease_soft: datetime | None = None
    lease_hard: datetime | None = None


class Provider(Protocol):
    name: str

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        """Budget and price before anything is started."""

    def start(self, launch: JobLaunch) -> RunningJob:
        """Upload the stage and start the script."""

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        """Budget and liveness of a running job."""

    def signal(
        self,
        handle: RunningJob,
        signal: Signal,
        reason: str,
        lease_soft: datetime,
        lease_hard: datetime,
    ) -> None:
        """Tell the worker to continue, checkpoint, or stop."""

    def ack_ready(self, handle: RunningJob) -> bool:
        """True once the worker has written ack.json."""

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        """Copy state and artifacts onto the hub. True if state.json arrived."""

    def stop(self, handle: RunningJob) -> None:
        """Release the paid resource."""
