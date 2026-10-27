"""In-memory provider used by the hub tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from resalloc.io import write_json
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runtime_files import ack_is_ready, publish_runtime, store_checkpoint


@dataclass
class FakeProvider:
    name: str = "runpod"
    snapshot: AccountSnapshot | None = None
    ignore_signal: bool = False
    signal_error: BaseException | None = None
    probe_error: BaseException | None = None
    state: dict = field(default_factory=lambda: {"epoch": 2})
    started: list = field(default_factory=list)
    stopped: list = field(default_factory=list)
    signals: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.snapshot is None:
            self.snapshot = AccountSnapshot(
                provider=self.name,
                alive=True,
                headroom_usd=10,
                burn_usd_per_hour=0.1,
                hourly_price_usd=0.4,
            )

    def _copy(self, *, alive: bool = True) -> AccountSnapshot:
        assert self.snapshot is not None
        return AccountSnapshot(
            provider=self.name,
            alive=alive and self.snapshot.alive,
            headroom_usd=self.snapshot.headroom_usd,
            burn_usd_per_hour=self.snapshot.burn_usd_per_hour,
            hourly_price_usd=self.snapshot.hourly_price_usd,
            quota_hours_left=self.snapshot.quota_hours_left,
            heartbeat_age_seconds=self.snapshot.heartbeat_age_seconds,
            detail=self.snapshot.detail,
        )

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        del launch
        return self._copy(alive=True)

    def start(self, launch: JobLaunch) -> RunningJob:
        self.started.append(dict(launch.settings))
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=f"{self.name}-1",
            connection={"runtime_dir": str(launch.runtime_dir)},
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        del handle
        if self.probe_error is not None:
            raise self.probe_error
        return self._copy()

    def signal(
        self,
        handle: RunningJob,
        signal: Signal,
        reason: str,
        lease_soft: datetime,
        lease_hard: datetime,
    ) -> None:
        if self.signal_error is not None:
            raise self.signal_error
        self.signals.append(signal)
        runtime = Path(str(handle.connection["runtime_dir"]))
        publish_runtime(runtime, signal, reason, lease_soft, lease_hard)
        if self.ignore_signal or signal is Signal.CONTINUE:
            return
        write_json(runtime / "state.json", self.state)
        write_json(
            runtime / "ack.json",
            {"signal": signal.value, "at": lease_soft.isoformat(), "state": self.state},
        )

    def ack_ready(self, handle: RunningJob) -> bool:
        return ack_is_ready(Path(str(handle.connection["runtime_dir"])))

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        return store_checkpoint(Path(str(handle.connection["runtime_dir"])), checkpoint_dir)

    def stop(self, handle: RunningJob) -> None:
        self.stopped.append(handle.remote_id)
