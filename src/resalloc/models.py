"""Job, limits, and provider settings.

These objects are what user code builds. They serialize to JSON so the laptop
can hand a job to the hub over SSH.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from resalloc.names import safe_segment


def _seconds(value: timedelta) -> float:
    return value.total_seconds()


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


@dataclass
class Limits:
    """When to ask the job to save, and when to cut it off."""

    soft_buffer: timedelta = timedelta(minutes=15)
    hard_buffer: timedelta = timedelta(minutes=5)
    runpod_min_balance_usd: float = 1.0
    runpod_max_spend_usd: float | None = None
    runpod_max_hourly_usd: float | None = None
    modal_max_spend_usd: float = 25.0
    kaggle_min_gpu_hours: float = 0.3
    vast_min_balance_usd: float = 1.0
    vast_max_spend_usd: float | None = None
    vast_max_hourly_usd: float | None = None
    laptop_heartbeat_timeout: timedelta = timedelta(seconds=90)

    def __post_init__(self) -> None:
        if self.hard_buffer > self.soft_buffer:
            raise ValueError("hard_buffer must be less than or equal to soft_buffer")
        if self.soft_buffer < timedelta(0) or self.hard_buffer < timedelta(0):
            raise ValueError("buffers must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "soft_buffer_seconds": _seconds(self.soft_buffer),
            "hard_buffer_seconds": _seconds(self.hard_buffer),
            "runpod_min_balance_usd": self.runpod_min_balance_usd,
            "runpod_max_spend_usd": self.runpod_max_spend_usd,
            "runpod_max_hourly_usd": self.runpod_max_hourly_usd,
            "modal_max_spend_usd": self.modal_max_spend_usd,
            "kaggle_min_gpu_hours": self.kaggle_min_gpu_hours,
            "vast_min_balance_usd": self.vast_min_balance_usd,
            "vast_max_spend_usd": self.vast_max_spend_usd,
            "vast_max_hourly_usd": self.vast_max_hourly_usd,
            "laptop_heartbeat_timeout_seconds": _seconds(self.laptop_heartbeat_timeout),
        }


def limits_from_dict(data: dict[str, Any]) -> Limits:
    return Limits(
        soft_buffer=timedelta(seconds=float(data.get("soft_buffer_seconds", 900))),
        hard_buffer=timedelta(seconds=float(data.get("hard_buffer_seconds", 300))),
        runpod_min_balance_usd=float(data.get("runpod_min_balance_usd", 1.0)),
        runpod_max_spend_usd=data.get("runpod_max_spend_usd"),
        runpod_max_hourly_usd=data.get("runpod_max_hourly_usd"),
        modal_max_spend_usd=float(data.get("modal_max_spend_usd", 25.0)),
        kaggle_min_gpu_hours=float(data.get("kaggle_min_gpu_hours", 0.3)),
        vast_min_balance_usd=float(data.get("vast_min_balance_usd", 1.0)),
        vast_max_spend_usd=_optional_float(data.get("vast_max_spend_usd")),
        vast_max_hourly_usd=_optional_float(data.get("vast_max_hourly_usd")),
        laptop_heartbeat_timeout=timedelta(
            seconds=float(data.get("laptop_heartbeat_timeout_seconds", 90))
        ),
    )


@dataclass
class RunPod:
    gpu: str
    spot: bool = False
    image: str | None = None
    disk_gb: int = 20
    gpu_count: int = 1
    cloud: str | None = None
    data_centers: list[str] | None = None
    ports: list[str] | None = None
    volume_gb: int | None = None
    network_volume_id: str | None = None
    env: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "runpod",
            "gpu": self.gpu,
            "spot": self.spot,
            "image": self.image,
            "disk_gb": self.disk_gb,
            "gpu_count": self.gpu_count,
            "cloud": self.cloud,
            "data_centers": self.data_centers,
            "ports": self.ports,
            "volume_gb": self.volume_gb,
            "network_volume_id": self.network_volume_id,
            "env": self.env,
        }


@dataclass
class Modal:
    gpu: str
    image: str | None = None
    cpu: float | None = None
    memory_mb: int | None = None
    timeout_hours: float = 24.0
    volume: str | None = None
    region: str | None = None
    cloud: str | None = None
    env: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "modal",
            "gpu": self.gpu,
            "image": self.image,
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "timeout_hours": self.timeout_hours,
            "volume": self.volume,
            "region": self.region,
            "cloud": self.cloud,
            "env": self.env,
        }


@dataclass
class Kaggle:
    accelerator: str = "NvidiaTeslaT4"
    internet: bool = True
    title: str | None = None
    private: bool = True
    kernel_type: str = "script"
    dataset_slug: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "kaggle",
            "accelerator": self.accelerator,
            "internet": self.internet,
            "title": self.title,
            "private": self.private,
            "kernel_type": self.kernel_type,
            "dataset_slug": self.dataset_slug,
        }


@dataclass
class Vast:
    """A Vast.ai instance. The hub rents it, then copies the job over SSH.

    The account SSH key has to be registered on Vast before the first start.
    `gpu` accepts a Vast name (`RTX 4090`) or an NVIDIA catalog name.
    """

    gpu: str
    spot: bool = False
    image: str | None = None
    disk_gb: int = 32
    gpu_count: int = 1
    max_price: float | None = None
    min_reliability: float | None = None
    require_direct_ports: bool = True
    env: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "vast",
            "gpu": self.gpu,
            "spot": self.spot,
            "image": self.image,
            "disk_gb": self.disk_gb,
            "gpu_count": self.gpu_count,
            "max_price": self.max_price,
            "min_reliability": self.min_reliability,
            "require_direct_ports": self.require_direct_ports,
            "env": self.env,
        }


@dataclass
class Machine:
    """Any host the hub can SSH to. Stopping the job does not power the machine off."""

    host: str
    user: str = "root"
    port: int = 22
    identity: str | None = None
    gpu: str | None = None
    workdir: str = "/var/tmp/resalloc"
    python: str = "python3"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "machine",
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "identity": self.identity,
            "gpu": self.gpu,
            "workdir": self.workdir,
            "python": self.python,
        }


@dataclass
class Laptop:
    """This machine. Price in the policy is zero. It has to be awake."""

    cpus: int | None = None
    ram_gb: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": "laptop", "cpus": self.cpus, "ram_gb": self.ram_gb}


Target = RunPod | Modal | Kaggle | Vast | Machine | Laptop


def target_from_dict(data: dict[str, Any]) -> Target:
    kind = data.get("type")
    if kind == "runpod":
        return RunPod(
            gpu=data["gpu"],
            spot=bool(data.get("spot", False)),
            image=data.get("image"),
            disk_gb=int(data.get("disk_gb", 20)),
            gpu_count=int(data.get("gpu_count", 1)),
            cloud=data.get("cloud"),
            data_centers=data.get("data_centers"),
            ports=data.get("ports"),
            volume_gb=data.get("volume_gb"),
            network_volume_id=data.get("network_volume_id"),
            env=data.get("env"),
        )
    if kind == "modal":
        return Modal(
            gpu=data["gpu"],
            image=data.get("image"),
            cpu=data.get("cpu"),
            memory_mb=data.get("memory_mb"),
            timeout_hours=float(data.get("timeout_hours", 24.0)),
            volume=data.get("volume"),
            region=data.get("region"),
            cloud=data.get("cloud"),
            env=data.get("env"),
        )
    if kind == "kaggle":
        return Kaggle(
            accelerator=data.get("accelerator", "NvidiaTeslaT4"),
            internet=bool(data.get("internet", True)),
            title=data.get("title"),
            private=bool(data.get("private", True)),
            kernel_type=data.get("kernel_type", "script"),
            dataset_slug=data.get("dataset_slug"),
        )
    if kind == "vast":
        return Vast(
            gpu=data["gpu"],
            spot=bool(data.get("spot", False)),
            image=data.get("image"),
            disk_gb=int(data.get("disk_gb", 32)),
            gpu_count=int(data.get("gpu_count", 1)),
            max_price=_optional_float(data.get("max_price")),
            min_reliability=_optional_float(data.get("min_reliability")),
            require_direct_ports=bool(data.get("require_direct_ports", True)),
            env=data.get("env"),
        )
    if kind == "machine":
        return Machine(
            host=str(data["host"]),
            user=str(data.get("user") or "root"),
            port=int(data.get("port") or 22),
            identity=data.get("identity"),
            gpu=data.get("gpu"),
            workdir=str(data.get("workdir") or "/var/tmp/resalloc"),
            python=str(data.get("python") or "python3"),
        )
    if kind == "laptop":
        return Laptop(cpus=data.get("cpus"), ram_gb=data.get("ram_gb"))
    raise ValueError(f"unknown provider type: {kind}")


@dataclass
class Migrate:
    target: Target
    confirm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"confirm": self.confirm, "target": self.target.to_dict()}


ROUTES = ("hub", "direct", "stream")
# keep="hub" stores bytes on the hub. Any other keep is a provider name.
_KEEP = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass
class Dataset:
    """Where the bytes live. The hub still keeps checkpoints either way.

    route=hub copies the dataset onto Oracle.
    route=direct sends it from this machine to the provider store, one file at a time.
    route=stream does the same, but each finished file is marked so training can read it
    before the rest has arrived. Nothing is buffered whole in memory.
    """

    path: str
    route: str = "hub"
    keep: str = "hub"
    name: str | None = None
    store: str | None = None

    def __post_init__(self) -> None:
        if self.route not in ROUTES:
            raise ValueError(f"route must be one of {ROUTES}")
        if self.keep != "hub" and not _KEEP.fullmatch(self.keep):
            raise ValueError("keep must be 'hub' or a provider name")
        if self.route == "hub" and self.keep != "hub":
            raise ValueError("route hub stores the bytes on the hub")
        if self.name is None:
            self.name = Path(self.path).name
        if not safe_segment(str(self.name)):
            raise ValueError("dataset name must be a single path segment")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "route": self.route,
            "keep": self.keep,
            "name": self.name,
            "store": self.store,
        }


def coerce_dataset(item: str | Dataset | dict[str, Any]) -> Dataset:
    if isinstance(item, Dataset):
        return item
    if isinstance(item, str):
        return Dataset(path=item)
    if isinstance(item, dict):
        return Dataset(
            path=str(item.get("path") or item.get("name") or ""),
            route=str(item.get("route") or "hub"),
            keep=str(item.get("keep") or "hub"),
            name=item.get("name"),
            store=item.get("store"),
        )
    raise TypeError(f"dataset must be a path, Dataset, or dict, not {type(item).__name__}")


@dataclass
class Job:
    script: str
    start: Target
    requirements: str | None = None
    datasets: list[Any] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    failover: list[Migrate] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    bundle_paths: list[str] = field(default_factory=list)
    data_timeout_seconds: float = 3600

    def __post_init__(self) -> None:
        self.datasets = [coerce_dataset(item) for item in self.datasets]

    def targets(self) -> list[dict[str, Any]]:
        return [self.start.to_dict(), *[step.target.to_dict() for step in self.failover]]

    def confirms(self) -> list[bool]:
        return [False, *[step.confirm for step in self.failover]]
