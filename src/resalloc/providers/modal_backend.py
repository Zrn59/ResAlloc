"""Modal adapter.

Long jobs are sandboxes with a Volume, so the hub can write a stop file and
call terminate. Billing comes from Workspace.billing.summary and
billing.report. The API reports billed cost, not remaining credits, so the
ceiling is limits.modal_max_spend_usd.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from resalloc.bundle import iter_stage_files
from resalloc.errors import ProviderError
from resalloc.io import dump_dt, utcnow
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runtime_files import ack_is_ready, publish_runtime, store_checkpoint


def burn_from_report(items: Any, now: datetime | None = None) -> float | None:
    """Sum costs whose interval overlaps the last hour."""

    if items is None:
        return None
    moment = now or utcnow()
    window_start = moment - timedelta(hours=1)
    total = 0.0
    found = False
    for item in items:
        cost = _cost(item)
        end = _moment(item, "end", "interval_end", "timestamp")
        if cost is None:
            continue
        if end is not None and end < window_start:
            continue
        total += cost
        found = True
    if not found:
        return None
    return total


def _cost(item: Any) -> float | None:
    if isinstance(item, dict):
        for key in ("cost", "billed_cost", "amount"):
            if item.get(key) is not None:
                return float(item[key])
        return None
    for key in ("cost", "billed_cost", "amount"):
        if hasattr(item, key) and getattr(item, key) is not None:
            return float(getattr(item, key))
    return None


def _moment(item: Any, *names: str) -> datetime | None:
    for name in names:
        value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=utcnow().tzinfo)
        if isinstance(value, str):
            from resalloc.io import parse_dt

            return parse_dt(value)
    return None


def _modal_env(launch: JobLaunch) -> dict[str, str]:
    env = {
        "RESALLOC_JOB_DIR": "/resalloc/runtime",
        "RESALLOC_DATA_DIR": "/data" if launch.settings.get("volume") else "/resalloc/datasets",
        "PYTHONPATH": "/resalloc/pkg",
    }
    for key, value in (launch.settings.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


def _already_finished(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "already finished" in text or "already terminated" in text


def import_modal() -> Any:
    try:
        import modal as modal_sdk
    except ImportError as exc:
        raise ProviderError("Modal SDK is not installed. Install resalloc with the modal extra.") from exc
    return modal_sdk


def usage_usd(summary: Any) -> float:
    """Spend before credits. billed_cost stays 0 while the $30 credit covers the meter."""

    metered = getattr(summary, "metered_cost", None)
    if metered is None and isinstance(summary, dict):
        metered = summary.get("metered_cost")
    if metered is not None:
        return float(metered)
    billed = getattr(summary, "billed_cost", None)
    if billed is None and isinstance(summary, dict):
        billed = summary.get("billed_cost", 0)
    return float(billed or 0)


def burn_from_meter(previous: tuple[float, datetime] | None, metered: float, now: datetime) -> float | None:
    """Hourly rate from two meter readings when the hourly report has not closed yet."""

    if previous is None:
        return None
    old, then = previous
    hours = (now - then).total_seconds() / 3600
    if hours <= 0:
        return None
    delta = metered - old
    if delta < 0:
        return None
    return delta / hours


def current_workspace(modal_sdk: Any) -> Any:
    workspace_cls = getattr(modal_sdk, "Workspace", None)
    if workspace_cls is None:
        raise ProviderError("this Modal SDK has no Workspace billing API")
    from_context = getattr(workspace_cls, "from_context", None)
    if from_context is not None:
        try:
            return from_context()
        except TypeError:
            pass
    name = os.environ.get("MODAL_WORKSPACE")
    if name and hasattr(workspace_cls, "from_name"):
        return workspace_cls.from_name(name)
    for attr in ("current", "lookup"):
        method = getattr(workspace_cls, attr, None)
        if method is None:
            continue
        try:
            return method()
        except TypeError:
            continue
    raise ProviderError("could not resolve the Modal workspace; set MODAL_WORKSPACE")


def sdk_billing(now: datetime | None = None, previous: tuple[float, datetime] | None = None) -> dict[str, float | None]:
    modal_sdk = import_modal()
    workspace = current_workspace(modal_sdk)
    summary = workspace.billing.summary()
    metered = usage_usd(summary)
    moment = now or utcnow()
    try:
        report = workspace.billing.report(start=moment - timedelta(hours=1), end=moment, resolution="h")
    except TypeError:
        report = workspace.billing.report(start=moment - timedelta(hours=1), end=moment)
    burn = burn_from_report(report, moment)
    if burn is None:
        burn = burn_from_meter(previous, metered, moment)
    return {"billed_cost": metered, "burn_per_hour": burn}


class ModalAdapter:
    name = "modal"

    def __init__(
        self,
        billing: Callable[[], dict[str, Any]] | None = None,
        sandbox_factory: Callable[..., Any] | None = None,
        volume_factory: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.billing = billing
        self.sandbox_factory = sandbox_factory
        self.volume_factory = volume_factory
        self.clock = clock or utcnow
        self._sandboxes: dict[str, Any] = {}
        self._volumes: dict[str, Any] = {}
        self._meter: dict[str, tuple[float, datetime]] = {}
        self._lock = threading.Lock()

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        return self._money(launch.limits.modal_max_spend_usd, alive=True, detail="")

    def start(self, launch: JobLaunch) -> RunningJob:
        volume_name = f"resalloc-{launch.job_id}"
        data_volume = launch.settings.get("volume")
        if self.sandbox_factory is not None and self.volume_factory is not None:
            volume = self.volume_factory(name=volume_name)
            self._upload(volume, iter_stage_files(launch.job_dir))
            sandbox = self.sandbox_factory(
                script=launch.script.name,
                args=list(launch.args),
                gpu=launch.settings.get("gpu"),
                image=launch.settings.get("image"),
                cpu=launch.settings.get("cpu"),
                memory_mb=launch.settings.get("memory_mb"),
                timeout_hours=launch.settings.get("timeout_hours", 24),
                volume=volume,
                env=_modal_env(launch),
                region=launch.settings.get("region"),
                cloud=launch.settings.get("cloud"),
            )
        else:
            sandbox, volume = self._sdk_start(launch, volume_name)
        remote_id = str(getattr(sandbox, "object_id", None) or getattr(sandbox, "id", volume_name))
        self._sandboxes[remote_id] = sandbox
        self._volumes[remote_id] = volume
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=remote_id,
            connection={
                "volume": volume_name,
                "spend_cap": launch.limits.modal_max_spend_usd,
                "runtime_dir": str(launch.runtime_dir),
                "started_at": dump_dt(self.clock()),
                "data_endpoint": {
                    "provider": "modal",
                    "volume": data_volume or volume_name,
                    "remote_dir": "/data" if data_volume else "/resalloc",
                },
            },
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        sandbox = self._sandbox(handle)
        code = sandbox.poll()
        alive = code is None
        detail = ""
        if code == 0:
            detail = "completed"
        elif code is not None:
            detail = "worker_failed"
        cap = float(handle.connection.get("spend_cap", 0))
        return self._money(cap, alive=alive, detail=detail, meter_key=handle.remote_id)

    def signal(self, handle: RunningJob, signal: Signal, reason: str, lease_soft: datetime, lease_hard: datetime) -> None:
        runtime = Path(str(handle.connection["runtime_dir"]))
        publish_runtime(runtime, signal, reason, lease_soft, lease_hard)
        volume = self._volume(handle)
        self._upload(
            volume,
            [
                (runtime / "control.json", "runtime/control.json"),
                (runtime / "lease.json", "runtime/lease.json"),
            ],
        )
        sandbox = self._sandbox(handle)
        reload = getattr(sandbox, "reload_volumes", None)
        if reload is None:
            return
        try:
            reload()
        except Exception as exc:
            if _already_finished(exc):
                return
            raise ProviderError(str(exc)) from exc

    def ack_ready(self, handle: RunningJob) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        self._download_runtime(handle, runtime)
        return ack_is_ready(runtime)

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        self._download_runtime(handle, runtime)
        return store_checkpoint(runtime, checkpoint_dir)

    def stop(self, handle: RunningJob) -> None:
        sandbox = self._sandbox(handle)
        try:
            sandbox.terminate(wait=False)
        except Exception as exc:
            if _already_finished(exc):
                return
            raise ProviderError(str(exc)) from exc

    def _money(self, cap: float, *, alive: bool, detail: str, meter_key: str = "") -> AccountSnapshot:
        with self._lock:
            previous = self._meter.get(meter_key) if meter_key else None
        if self.billing is not None:
            report = self.billing()
        else:
            report = sdk_billing(self.clock(), previous)
        billed = float(report.get("billed_cost") or 0)
        burn = report.get("burn_per_hour")
        if burn is None:
            burn = burn_from_meter(previous, billed, self.clock())
        # Keep the first sample. Modal updates the meter in batches, so a
        # one-minute window is zero for several ticks and then a spike.
        if meter_key:
            with self._lock:
                if meter_key not in self._meter:
                    self._meter[meter_key] = (billed, self.clock())
        room = cap - billed
        if room <= 0 and not detail:
            detail = "modal_spend_cap"
        return AccountSnapshot(
            provider=self.name,
            alive=alive,
            headroom_usd=room,
            burn_usd_per_hour=None if burn is None else float(burn),
            detail=detail,
        )

    def _sdk_start(self, launch: JobLaunch, volume_name: str) -> tuple[Any, Any]:
        modal_sdk = import_modal()
        app = modal_sdk.App.lookup("resalloc", create_if_missing=True)
        volume = modal_sdk.Volume.from_name(volume_name, create_if_missing=True)
        self._upload(volume, iter_stage_files(launch.job_dir))
        image = modal_sdk.Image.debian_slim()
        if launch.settings.get("image"):
            image = modal_sdk.Image.from_registry(str(launch.settings["image"]))
        elif launch.requirements is not None:
            image = image.pip_install_from_requirements(str(launch.requirements))
        volumes = {"/resalloc": volume}
        if launch.settings.get("volume"):
            volumes["/data"] = modal_sdk.Volume.from_name(str(launch.settings["volume"]), create_if_missing=True)
        kwargs: dict[str, Any] = {
            "app": app,
            "image": image,
            "volumes": volumes,
            "timeout": int(float(launch.settings.get("timeout_hours", 24)) * 3600),
            "workdir": "/resalloc",
            "env": _modal_env(launch),
        }
        gpu = launch.settings.get("gpu")
        if gpu and str(gpu).lower() not in {"cpu", "none"}:
            kwargs["gpu"] = gpu
        if launch.settings.get("region"):
            kwargs["region"] = launch.settings["region"]
        if launch.settings.get("cloud"):
            kwargs["cloud"] = launch.settings["cloud"]
        if launch.settings.get("cpu"):
            kwargs["cpu"] = float(launch.settings["cpu"])
        if launch.settings.get("memory_mb"):
            kwargs["memory"] = int(launch.settings["memory_mb"])
        command = ["python", "-u", launch.script.name, *launch.args]
        sandbox = modal_sdk.Sandbox.create(*command, **kwargs)
        return sandbox, volume

    def _upload(self, volume: Any, files: list[tuple[Path, str]]) -> None:
        if hasattr(volume, "upload"):
            volume.upload(files)
            return
        batch_upload = getattr(volume, "batch_upload", None)
        if batch_upload is None:
            raise ProviderError("Modal volume has no upload method")
        with batch_upload(force=True) as batch:
            for source, relative in files:
                batch.put_file(str(source), relative)

    def _download_runtime(self, handle: RunningJob, runtime: Path) -> None:
        volume = self._volume(handle)
        if hasattr(volume, "download_runtime"):
            volume.download_runtime(runtime)
            return
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("state.json", "ack.json", "pulse.json", "control.json", "lease.json"):
            self._read_remote(volume, f"runtime/{name}", runtime / name)
        listdir = getattr(volume, "listdir", None)
        if listdir is None:
            return
        try:
            entries = listdir("runtime/artifacts", recursive=True)
        except TypeError:
            entries = listdir("runtime/artifacts")
        except Exception:
            entries = []
        for entry in entries:
            remote = str(getattr(entry, "path", None) or getattr(entry, "name", None) or entry).lstrip("/")
            kind = str(getattr(entry, "type", "") or "").lower()
            if "dir" in kind and "file" not in kind:
                continue
            if not remote.startswith("runtime/artifacts/"):
                remote = f"runtime/artifacts/{Path(remote).name}"
            if remote.endswith("/"):
                continue
            local = runtime / Path(remote).relative_to("runtime")
            self._read_remote(volume, remote, local)
        self._read_remote(volume, "runtime/worker.log", runtime / "worker.log")

    def _read_remote(self, volume: Any, remote: str, local: Path) -> None:
        read_file = getattr(volume, "read_file", None)
        if read_file is None:
            return
        try:
            chunks = read_file(remote)
            if isinstance(chunks, bytes):
                data = chunks
            elif isinstance(chunks, str):
                data = chunks.encode("utf-8")
            else:
                data = b"".join(chunks)
        except Exception:
            return
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)

    def _sandbox(self, handle: RunningJob) -> Any:
        with self._lock:
            cached = self._sandboxes.get(handle.remote_id)
        if cached is not None:
            return cached
        modal_sdk = import_modal()
        sandbox = modal_sdk.Sandbox.from_id(handle.remote_id)
        with self._lock:
            return self._sandboxes.setdefault(handle.remote_id, sandbox)

    def _volume(self, handle: RunningJob) -> Any:
        with self._lock:
            cached = self._volumes.get(handle.remote_id)
        if cached is not None:
            return cached
        modal_sdk = import_modal()
        volume = modal_sdk.Volume.from_name(str(handle.connection["volume"]))
        with self._lock:
            return self._volumes.setdefault(handle.remote_id, volume)


def _factory(root: Path) -> ModalAdapter:
    del root
    return ModalAdapter()
