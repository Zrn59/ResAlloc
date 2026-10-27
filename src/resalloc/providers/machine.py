"""An SSH host the hub can dial.

This is the extra machine that is not Runpod, Vast, Modal, or Kaggle:
a lab box, a cloud VM, a second desktop. The hub copies the job over and
kills the training process when it is done. It does not shut the machine down.
The laptop provider is the other way around: the laptop dials the hub, because
the hub cannot get through NAT.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from resalloc.bundle import iter_stage_files
from resalloc.errors import ProviderError
from resalloc.io import utcnow
from resalloc.names import safe_job_id
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runpod import _ssh_not_ready, default_pull, default_ssh, default_sync, training_command
from resalloc.providers.runtime_files import ack_is_ready, publish_runtime, store_checkpoint

_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,253}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
_PYTHON = re.compile(r"[A-Za-z0-9_./+-]{1,120}")


def machine_root(settings: dict[str, Any], job_id: str) -> str:
    base = str(settings.get("workdir") or "/var/tmp/resalloc")
    if not base.startswith("/") or ".." in Path(base).parts or any(ord(char) < 32 for char in base):
        raise ProviderError("machine workdir must be an absolute path")
    if not safe_job_id(job_id):
        raise ProviderError("unsafe job id")
    return f"{base.rstrip('/')}/{job_id}"


def machine_endpoint(settings: dict[str, Any]) -> dict[str, Any]:
    host = str(settings.get("host") or "")
    user = str(settings.get("user") or "root")
    if not _HOST.fullmatch(host):
        raise ProviderError("machine host must be a hostname or address, with no spaces")
    if not _USER.fullmatch(user):
        raise ProviderError("machine user must be a plain account name")
    endpoint: dict[str, Any] = {"host": host, "port": int(settings.get("port") or 22), "username": user}
    if settings.get("identity"):
        endpoint["identity"] = str(settings["identity"])
    return endpoint


def python_bin(settings: dict[str, Any]) -> str:
    text = str(settings.get("python") or "python3")
    if not _PYTHON.fullmatch(text) or ".." in text:
        raise ProviderError("unsafe python path")
    return text


class MachineAdapter:
    name = "machine"

    def __init__(
        self,
        ssh_runner: Callable[[dict[str, Any], str], str] | None = None,
        file_sync: Callable[..., None] | None = None,
        file_pull: Callable[[dict[str, Any], str, Path], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.ssh_runner = ssh_runner or default_ssh
        self.file_sync = file_sync or _sync_to_root
        self.file_pull = file_pull or default_pull
        self.clock = clock or utcnow

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        endpoint = machine_endpoint(launch.settings)
        return self._liveness(endpoint, assigned=False)

    def start(self, launch: JobLaunch) -> RunningJob:
        endpoint = machine_endpoint(launch.settings)
        root = machine_root(launch.settings, launch.job_id)
        endpoint["remote_root"] = root
        snapshot = self._liveness(endpoint, assigned=False)
        if not snapshot.alive:
            raise ProviderError(snapshot.detail or "machine is not accepting SSH")
        self._sync(endpoint, iter_stage_files(launch.job_dir), root)
        self.ssh_runner(
            endpoint,
            training_command(
                launch.script.name,
                launch.args,
                datasets=[str(name) for name in (launch.settings.get("_wait_datasets") or [])],
                data_timeout=float(launch.settings.get("_data_timeout") or 3600),
                root=root,
                python=python_bin(launch.settings),
            ),
        )
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=f"{endpoint['username']}@{endpoint['host']}",
            connection={
                "ssh": endpoint,
                "root": root,
                "runtime_dir": str(launch.runtime_dir),
                "data_endpoint": {
                    "provider": "machine",
                    "ssh": endpoint,
                    "remote_dir": f"{root}/datasets",
                },
            },
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        endpoint = dict(handle.connection.get("ssh") or {})
        finished = self._read_finished(handle, endpoint)
        if finished is not None:
            code = int(finished.get("returncode", 1))
            return AccountSnapshot(
                provider=self.name,
                alive=False,
                hourly_price_usd=0.0,
                burn_usd_per_hour=0.0,
                detail="completed" if code == 0 else "worker_failed",
            )
        return self._liveness(endpoint, assigned=True)

    def signal(self, handle: RunningJob, signal: Signal, reason: str, lease_soft: datetime, lease_hard: datetime) -> None:
        runtime = Path(str(handle.connection["runtime_dir"]))
        publish_runtime(runtime, signal, reason, lease_soft, lease_hard)
        endpoint = dict(handle.connection.get("ssh") or {})
        root = str(handle.connection.get("root") or endpoint.get("remote_root") or "/var/tmp/resalloc")
        endpoint["remote_root"] = root
        self._sync(
            endpoint,
            [
                (runtime / "control.json", "runtime/control.json"),
                (runtime / "lease.json", "runtime/lease.json"),
            ],
            root,
        )

    def ack_ready(self, handle: RunningJob) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        self._fetch(handle, runtime)
        return ack_is_ready(runtime)

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        self._fetch(handle, runtime)
        return store_checkpoint(runtime, checkpoint_dir)

    def stop(self, handle: RunningJob) -> None:
        """Kill the training process. The machine itself stays up."""

        endpoint = dict(handle.connection.get("ssh") or {})
        root = str(handle.connection.get("root") or "")
        if not endpoint.get("host") or not root:
            return
        pid = shlex.quote(root + "/runtime/worker.pid")
        self.ssh_runner(endpoint, f"if [ -f {pid} ]; then kill $(cat {pid}) 2>/dev/null || true; fi")

    def _liveness(self, endpoint: dict[str, Any], *, assigned: bool) -> AccountSnapshot:
        try:
            self.ssh_runner(endpoint, "echo ok")
        except (ProviderError, OSError) as exc:
            text = str(exc)
            if not assigned and not _ssh_not_ready(text) and "Permission denied" not in text:
                raise ProviderError(text)
            return AccountSnapshot(
                provider=self.name,
                alive=False,
                hourly_price_usd=0.0,
                burn_usd_per_hour=0.0,
                detail="workload_stopped",
            )
        return AccountSnapshot(provider=self.name, alive=True, hourly_price_usd=0.0, burn_usd_per_hour=0.0)

    def _sync(self, endpoint: dict[str, Any], files: list[tuple[Path, str]], root: str) -> None:
        try:
            self.file_sync(endpoint, files, root)
        except TypeError:
            self.file_sync(endpoint, files)

    def _fetch(self, handle: RunningJob, runtime: Path) -> None:
        endpoint = handle.connection.get("ssh")
        root = str(handle.connection.get("root") or "")
        if not isinstance(endpoint, dict) or not root:
            return
        self.file_pull(endpoint, f"{root}/runtime", runtime)

    def _read_finished(self, handle: RunningJob, endpoint: dict[str, Any]) -> dict[str, Any] | None:
        runtime = Path(str(handle.connection.get("runtime_dir") or ""))
        local = runtime / "finished.json"
        if local.is_file():
            try:
                data = json.loads(local.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None
        root = str(handle.connection.get("root") or "")
        if not endpoint.get("host") or not root:
            return None
        try:
            text = self.ssh_runner(endpoint, f"cat {shlex.quote(root + '/runtime/finished.json')}")
        except (ProviderError, OSError) as exc:
            if _ssh_not_ready(str(exc)):
                raise ProviderError(str(exc))
            return None
        text = (text or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


def _sync_to_root(endpoint: dict[str, Any], files: list[tuple[Path, str]], root: str = "/var/tmp/resalloc") -> None:
    default_sync(endpoint, files, remote_root=root)


def _factory(root: Path) -> MachineAdapter:
    del root
    return MachineAdapter()
