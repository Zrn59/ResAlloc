"""Vast.ai adapter.

Search offers, accept one, copy the job over SSH, and destroy the instance
when it stops. A stopped instance keeps the disk bill, so stop() deletes it.
The account SSH key must already be registered on Vast; the API does not
install a key at create time.

Docs: https://docs.vast.ai/api-reference/creating-instances-with-api
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from resalloc.bundle import iter_stage_files
from resalloc.errors import ProviderError
from resalloc.io import dump_dt, parse_dt, utcnow
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runpod import (
    _ssh_not_ready,
    default_pull,
    default_ssh,
    default_sync,
    headroom_usd,
    training_command,
)
from resalloc.providers.runtime_files import ack_is_ready, publish_runtime, store_checkpoint

API_ROOT = "https://console.vast.ai"
DEFAULT_IMAGE = "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime"
DEAD = {"exited", "unknown", "offline"}


class VastHttpError(ProviderError):
    def __init__(self, status: int, path: str, detail: str) -> None:
        super().__init__(f"Vast {status} {path}: {detail}")
        self.status = status


def vast_gpu_name(name: str) -> str:
    """Vast lists `RTX 4090`, not the NVIDIA catalog string."""

    text = " ".join(str(name).split())
    for prefix in ("NVIDIA GeForce ", "Nvidia GeForce ", "NVIDIA ", "Nvidia "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text


def search_body(settings: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "gpu_name": {"eq": vast_gpu_name(str(settings["gpu"]))},
        "num_gpus": {"eq": int(settings.get("gpu_count") or 1)},
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "order": [["dph_total", "asc"]],
        "type": "bid" if settings.get("spot") else "on-demand",
        "limit": 20,
    }
    if settings.get("require_direct_ports", True):
        body["direct_port_count"] = {"gte": 1}
    if settings.get("min_reliability") is not None:
        body["reliability"] = {"gte": float(settings["min_reliability"])}
    if settings.get("max_price") is not None:
        body["dph_total"] = {"lte": float(settings["max_price"])}
    return body


def offer_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("offers")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def choose_offer(offers: list[dict[str, Any]], *, max_hourly: float | None) -> dict[str, Any]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for offer in offers:
        if offer.get("dph_total") is None or offer.get("id") is None:
            continue
        price = float(offer["dph_total"])
        if max_hourly is not None and price > max_hourly:
            continue
        ranked.append((price, offer))
    if not ranked:
        raise ProviderError("Vast has no rentable offer inside the price cap")
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def create_body(settings: dict[str, Any], job_id: str, offer: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "image": settings.get("image") or DEFAULT_IMAGE,
        "disk": int(settings.get("disk_gb") or 32),
        "label": f"resalloc-{job_id}"[:64],
        "runtype": "ssh",
        "cancel_unavail": True,
    }
    if settings.get("env"):
        body["env"] = {str(key): str(value) for key, value in settings["env"].items()}
    if settings.get("spot"):
        body["price"] = float(offer["dph_total"])
    return body


def instance_record(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        inst = payload.get("instances", payload)
        if isinstance(inst, list):
            inst = inst[0] if inst else None
        if isinstance(inst, dict) and (inst.get("id") is not None or inst.get("actual_status") is not None or inst.get("ssh_host")):
            return inst
    raise ProviderError("Vast returned an unexpected instance body")


def vast_ssh(instance: dict[str, Any]) -> dict[str, Any] | None:
    host = instance.get("ssh_host")
    port = instance.get("ssh_port")
    if not host or not port:
        return None
    return {"host": str(host), "port": int(port), "username": "root"}


def urllib_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if not raw:
        return status, None
    text = raw.decode("utf-8")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


class VastApi:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = API_ROOT,
        transport: Callable[..., tuple[int, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport or urllib_transport
        self.sleep = sleep or time.sleep
        self._balance: tuple[float, float] | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise ProviderError("VAST_API_KEY is not set")
        url = self.base_url + path
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        status, parsed = self._call(method, url, payload, headers)
        if isinstance(parsed, dict) and parsed.get("success") is False and status < 400:
            raise VastHttpError(status, path, str(parsed.get("msg") or parsed.get("error") or parsed))
        if status >= 400:
            if isinstance(parsed, dict):
                detail = parsed.get("msg") or parsed.get("error") or parsed.get("detail") or parsed
            else:
                detail = parsed
            raise VastHttpError(status, path, str(detail))
        return parsed

    def _call(self, method: str, url: str, payload: bytes | None, headers: dict[str, str]) -> tuple[int, Any]:
        # Creating an instance is not idempotent. Search and reads are.
        retry = method != "PUT"
        status, parsed = self.transport(method, url, payload, headers)
        if retry and status == 429:
            for attempt in range(3):
                self.sleep(min(2 ** (attempt + 1), 8))
                status, parsed = self.transport(method, url, payload, headers)
                if status != 429:
                    break
        return status, parsed

    def search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return offer_rows(self.request("POST", "/api/v0/bundles/", body))

    def create(self, offer_id: str, body: dict[str, Any]) -> str:
        created = self.request("PUT", f"/api/v0/asks/{offer_id}/", body)
        if not isinstance(created, dict):
            raise ProviderError("Vast create returned an unexpected body")
        contract = created.get("new_contract") or created.get("id")
        if not contract:
            raise ProviderError("Vast create returned no instance id")
        return str(contract)

    def show(self, instance_id: str) -> dict[str, Any]:
        return instance_record(self.request("GET", f"/api/v0/instances/{instance_id}/"))

    def destroy(self, instance_id: str) -> None:
        try:
            self.request("DELETE", f"/api/v0/instances/{instance_id}/")
        except VastHttpError as exc:
            if exc.status == 404:
                return
            raise

    def balance(self) -> float | None:
        if not self.api_key:
            return None
        cached = self._balance
        if cached is not None and time.monotonic() - cached[1] < 30:
            return cached[0]
        payload = self.request("GET", "/api/v0/users/current/")
        if not isinstance(payload, dict) or payload.get("balance") is None:
            return None
        value = float(payload["balance"])
        self._balance = (value, time.monotonic())
        return value


class VastAdapter:
    name = "vast"

    def __init__(
        self,
        api: VastApi | None = None,
        ssh_runner: Callable[[dict[str, Any], str], str] | None = None,
        file_sync: Callable[..., None] | None = None,
        file_pull: Callable[[dict[str, Any], str, Path], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        ready_timeout: float = 600,
    ) -> None:
        self.api = api or VastApi(os.environ.get("VAST_API_KEY"))
        self.ssh_runner = ssh_runner or default_ssh
        self.file_sync = file_sync or default_sync
        self.file_pull = file_pull or default_pull
        self.sleep = sleep or time.sleep
        self.clock = clock or utcnow
        self.ready_timeout = ready_timeout

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        settings = dict(launch.settings)
        settings["max_price"] = settings.get("max_price")
        if settings["max_price"] is None:
            settings["max_price"] = launch.limits.vast_max_hourly_usd
        try:
            offers = self.api.search(search_body(settings))
            offer = choose_offer(offers, max_hourly=_optional(settings.get("max_price")))
            hourly = float(offer["dph_total"])
            detail = ""
            alive = True
        except ProviderError as exc:
            hourly = None
            detail = str(exc)
            alive = False
        return self._money(launch.limits, handle=None, hourly=hourly, alive=alive, detail=detail, spent=0.0)

    def start(self, launch: JobLaunch) -> RunningJob:
        settings = dict(launch.settings)
        if settings.get("max_price") is None:
            settings["max_price"] = launch.limits.vast_max_hourly_usd
        offers = self.api.search(search_body(settings))
        offer = choose_offer(offers, max_hourly=_optional(settings.get("max_price")))
        instance_id = self.api.create(str(offer["id"]), create_body(settings, launch.job_id, offer))
        try:
            instance, endpoint = self._wait_ready(instance_id)
            identity = os.environ.get("VAST_SSH_IDENTITY") or os.environ.get("RESALLOC_SSH_IDENTITY")
            if identity:
                endpoint["identity"] = identity
            self._wait_ssh(endpoint)
            files = iter_stage_files(launch.job_dir)
            self._sync(endpoint, files)
            self.ssh_runner(
                endpoint,
                training_command(
                    launch.script.name,
                    launch.args,
                    datasets=[str(name) for name in (launch.settings.get("_wait_datasets") or [])],
                    data_timeout=float(launch.settings.get("_data_timeout") or 3600),
                ),
            )
        except Exception:
            try:
                self.api.destroy(instance_id)
            except Exception:
                pass
            raise
        try:
            baseline = self.api.balance()
        except ProviderError:
            baseline = None
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=instance_id,
            connection={
                "ssh": endpoint,
                "hourly": float(instance.get("dph_total") or offer["dph_total"]),
                "balance_baseline": baseline,
                "started_at": dump_dt(self.clock()),
                "min_balance": launch.limits.vast_min_balance_usd,
                "max_spend": launch.limits.vast_max_spend_usd,
                "runtime_dir": str(launch.runtime_dir),
                "gpu": settings.get("gpu"),
                "data_endpoint": {
                    "provider": "vast",
                    "ssh": endpoint,
                    "remote_dir": "/resalloc/datasets",
                },
            },
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        try:
            instance = self.api.show(handle.remote_id)
        except VastHttpError as exc:
            if exc.status == 404:
                return AccountSnapshot(provider=self.name, alive=False, burn_usd_per_hour=0.0, detail="workload_stopped")
            raise
        status = str(instance.get("actual_status") or "").lower()
        alive = status not in DEAD
        detail = "" if alive else "workload_stopped"
        hourly = instance.get("dph_total")
        hourly_value = float(hourly) if hourly is not None else _optional(handle.connection.get("hourly"))
        snapshot = self._money(
            None,
            handle=handle,
            hourly=hourly_value,
            alive=alive,
            detail=detail,
            spent=self._spent(handle, hourly_value),
        )
        finished = self._read_finished(handle)
        if finished is None:
            return snapshot
        code = int(finished.get("returncode", 1))
        from dataclasses import replace

        return replace(snapshot, alive=False, detail="completed" if code == 0 else "worker_failed")

    def signal(self, handle: RunningJob, signal: Signal, reason: str, lease_soft: datetime, lease_hard: datetime) -> None:
        runtime = Path(str(handle.connection["runtime_dir"]))
        publish_runtime(runtime, signal, reason, lease_soft, lease_hard)
        endpoint = handle.connection.get("ssh")
        if not isinstance(endpoint, dict):
            return
        self._sync(
            endpoint,
            [
                (runtime / "control.json", "runtime/control.json"),
                (runtime / "lease.json", "runtime/lease.json"),
            ],
        )

    def ack_ready(self, handle: RunningJob) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        endpoint = handle.connection.get("ssh")
        if isinstance(endpoint, dict):
            try:
                self._pull(endpoint, "/resalloc/runtime", runtime)
            except ProviderError:
                return False
        return ack_is_ready(runtime)

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        endpoint = handle.connection.get("ssh")
        if isinstance(endpoint, dict):
            self._pull(endpoint, "/resalloc/runtime", runtime)
        return store_checkpoint(runtime, checkpoint_dir)

    def stop(self, handle: RunningJob) -> None:
        self.api.destroy(handle.remote_id)

    def _money(
        self,
        limits: Any,
        *,
        handle: RunningJob | None,
        hourly: float | None,
        alive: bool,
        detail: str,
        spent: float,
    ) -> AccountSnapshot:
        balance = self.api.balance()
        if handle is None and limits is not None:
            min_balance = float(limits.vast_min_balance_usd)
            max_spend = limits.vast_max_spend_usd
        else:
            min_balance = float((handle.connection.get("min_balance") if handle else 0) or 0)
            max_spend = handle.connection.get("max_spend") if handle else None
        room = headroom_usd(
            balance=balance,
            min_balance=min_balance,
            max_spend=None if max_spend is None else float(max_spend),
            spent=spent,
        )
        if room is None and not detail:
            detail = "vast balance was not returned; set vast_max_spend_usd to cap the session"
        return AccountSnapshot(
            provider=self.name,
            alive=alive,
            headroom_usd=room,
            burn_usd_per_hour=hourly if handle is not None and alive else 0.0,
            hourly_price_usd=hourly,
            detail=detail,
        )

    def _spent(self, handle: RunningJob, hourly: float | None) -> float:
        baseline = handle.connection.get("balance_baseline")
        current = self.api.balance()
        drop = 0.0
        if isinstance(baseline, (int, float)) and isinstance(current, (int, float)):
            drop = max(0.0, float(baseline) - float(current))
        started = self.clock()
        if handle.connection.get("started_at"):
            started = parse_dt(str(handle.connection["started_at"]))
        elapsed = max(0.0, (self.clock() - started).total_seconds() / 3600)
        live = max(0.0, hourly or 0.0) * elapsed
        return max(drop, live)

    def _wait_ready(self, instance_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        deadline = time.monotonic() + self.ready_timeout
        last = "instance never published SSH"
        while True:
            instance = self.api.show(instance_id)
            status = str(instance.get("actual_status") or "").lower()
            if status in DEAD:
                raise ProviderError(f"instance {instance_id} entered {status} before SSH was usable")
            endpoint = vast_ssh(instance)
            if status == "running" and endpoint is not None:
                return instance, endpoint
            last = status or last
            if time.monotonic() >= deadline:
                raise ProviderError(f"instance {instance_id} stayed in {last or 'unknown'} until the ready timeout")
            self.sleep(8)

    def _wait_ssh(self, endpoint: dict[str, Any]) -> None:
        deadline = time.monotonic() + 90
        last = "ssh endpoint never accepted a command"
        while True:
            try:
                self.ssh_runner(endpoint, "echo ok")
                return
            except (ProviderError, OSError) as exc:
                last = str(exc)
                if not _ssh_not_ready(last):
                    raise ProviderError(last)
                if time.monotonic() >= deadline:
                    raise ProviderError(last)
                self.sleep(5)

    def _sync(self, endpoint: dict[str, Any], files: list[tuple[Path, str]]) -> None:
        try:
            self.file_sync(endpoint, files, "/resalloc")
        except TypeError:
            self.file_sync(endpoint, files)

    def _pull(self, endpoint: dict[str, Any], remote: str, local: Path) -> None:
        self.file_pull(endpoint, remote, local)

    def _read_finished(self, handle: RunningJob) -> dict[str, Any] | None:
        runtime = Path(str(handle.connection.get("runtime_dir") or ""))
        local = runtime / "finished.json"
        if local.is_file():
            try:
                data = json.loads(local.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None
        endpoint = handle.connection.get("ssh")
        if not isinstance(endpoint, dict) or not endpoint.get("host"):
            return None
        try:
            text = self.ssh_runner(endpoint, "cat /resalloc/runtime/finished.json")
        except (ProviderError, OSError):
            return None
        text = (text or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


def _optional(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _factory(root: Path) -> VastAdapter:
    del root
    return VastAdapter()
