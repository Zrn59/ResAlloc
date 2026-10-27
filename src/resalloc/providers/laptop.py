"""The laptop as a free provider.

The hub never dials into the laptop. The laptop agent heartbeats outward and
pulls assignments. A missing heartbeat is the same kind of interrupt as a pod
that disappeared.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from resalloc.errors import LaptopOfflineError
from resalloc.io import parse_dt, read_json, utcnow, write_json
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runtime_files import ack_is_ready, publish_runtime, store_checkpoint


def heartbeat_age_seconds(root: Path, now: datetime | None = None) -> float | None:
    path = root / "agents" / "laptop.json"
    if not path.exists():
        return None
    moment = now or utcnow()
    seen = parse_dt(str(read_json(path)["at"]))
    return max(0.0, (moment - seen).total_seconds())


def write_heartbeat(root: Path, now: datetime | None = None) -> None:
    write_json(root / "agents" / "laptop.json", {"at": (now or utcnow()).isoformat()})


class LaptopAdapter:
    name = "laptop"

    def __init__(self, root: Path, now: Callable[[], datetime] | None = None) -> None:
        self.root = root
        self.now = now or utcnow

    def online(self, timeout_seconds: float) -> bool:
        age = heartbeat_age_seconds(self.root, self.now())
        return age is not None and age <= timeout_seconds

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        return self._snapshot(launch.runtime_dir, timeout=launch.limits.laptop_heartbeat_timeout.total_seconds(), assigned=False)

    def start(self, launch: JobLaunch) -> RunningJob:
        timeout = launch.limits.laptop_heartbeat_timeout.total_seconds()
        if not self.online(timeout):
            raise LaptopOfflineError(
                "laptop provider was selected, but no heartbeat is arriving at the hub"
            )
        write_json(
            launch.runtime_dir / "assigned.json",
            {"job_id": launch.job_id, "at": self.now().isoformat()},
        )
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=f"laptop-{launch.job_id}",
            connection={
                "runtime_dir": str(launch.runtime_dir),
                "timeout": timeout,
            },
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        runtime = Path(str(handle.connection["runtime_dir"]))
        timeout = float(handle.connection.get("timeout") or 90)
        return self._snapshot(runtime, timeout=timeout, assigned=True)

    def signal(self, handle: RunningJob, signal: Signal, reason: str, lease_soft: datetime, lease_hard: datetime) -> None:
        publish_runtime(Path(str(handle.connection["runtime_dir"])), signal, reason, lease_soft, lease_hard)

    def ack_ready(self, handle: RunningJob) -> bool:
        return ack_is_ready(Path(str(handle.connection["runtime_dir"])))

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        return store_checkpoint(Path(str(handle.connection["runtime_dir"])), checkpoint_dir)

    def stop(self, handle: RunningJob) -> None:
        runtime = Path(str(handle.connection["runtime_dir"]))
        write_json(runtime / "stop_requested.json", {"at": self.now().isoformat()})

    def _snapshot(self, runtime: Path, *, timeout: float, assigned: bool) -> AccountSnapshot:
        age = heartbeat_age_seconds(self.root, self.now())
        finished_path = runtime / "finished.json"
        detail = ""
        alive = True
        if finished_path.exists():
            finished = read_json(finished_path)
            alive = False
            detail = "completed" if int(finished.get("returncode") or 0) == 0 else "worker_failed"
        elif age is None or age > timeout:
            alive = not assigned
            if assigned:
                detail = "laptop_heartbeat_lost"
        return AccountSnapshot(
            provider=self.name,
            alive=alive,
            hourly_price_usd=0.0,
            heartbeat_age_seconds=age,
            detail=detail,
        )


def _factory(root: Path) -> LaptopAdapter:
    return LaptopAdapter(root)
