"""The always-on loop. It accepts jobs, watches budgets, and moves work."""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from resalloc.bundle import copy_package, ensure_job_dirs, seed_runtime_from_checkpoint
from resalloc.disk import DEFAULT_RESERVE_BYTES, DiskBudget, configured_reserve
from resalloc.errors import DiskWatermarkError, LaptopOfflineError, ProviderBusy, ProviderError
from resalloc.io import dump_dt, parse_dt, read_json, utcnow, write_json
from resalloc.layout import Layout
from resalloc.models import limits_from_dict
from resalloc.names import safe_digest, safe_job_id, safe_relative, safe_segment
from resalloc.policy import AccountSnapshot, Action, decide, lease_bounds, signal_for, time_to_limit
from resalloc.providers.base import JobLaunch, Provider, running_job_from_dict
from resalloc.providers.laptop import heartbeat_age_seconds

logger = logging.getLogger("resalloc.hub")

TERMINAL = {"stopped", "failed", "completed"}
EDITABLE = {
    "gpu",
    "image",
    "spot",
    "disk_gb",
    "gpu_count",
    "accelerator",
    "internet",
    "title",
    "cpus",
    "ram_gb",
    "cpu",
    "memory_mb",
    "timeout_hours",
    "volume",
    "region",
    "cloud",
    "volume_gb",
    "network_volume_id",
    "dataset_slug",
    "private",
    "kernel_type",
    "host",
    "user",
    "port",
    "identity",
    "workdir",
    "python",
    "max_price",
    "min_reliability",
    "require_direct_ports",
}


_SSH_KEEPS = {"runpod", "vast", "machine"}


class Hub:
    def __init__(
        self,
        root: str | Path,
        providers: dict[str, Provider] | None = None,
        now: Callable[[], datetime] | None = None,
        reserve_bytes: int = DEFAULT_RESERVE_BYTES,
        max_running: dict[str, int] | None = None,
        max_total: int | None = None,
        tick_workers: int | None = None,
        probe_failures: int | None = None,
    ) -> None:
        self.root = Path(root)
        self.layout = Layout(self.root)
        self.disk = DiskBudget(self.root, configured_reserve(None if reserve_bytes == DEFAULT_RESERVE_BYTES else reserve_bytes))
        self.now = now or utcnow
        self.providers = providers if providers is not None else default_providers(self.root)
        file_caps, file_total, file_workers, file_probe = load_hub_limits(self.root)
        self.max_running = file_caps if max_running is None else dict(max_running)
        self.max_total = file_total if max_total is None else max_total
        self.tick_workers = file_workers if tick_workers is None else tick_workers
        self.probe_failures = file_probe if probe_failures is None else probe_failures
        self._launch_lock = threading.Lock()

    def run(self, interval: float = 60, *, once: bool = False) -> None:
        import time

        while True:
            try:
                self.tick()
            except BlockingIOError:
                logger.info("another hub tick holds the lock")
            if once:
                return
            time.sleep(interval)

    def tick(self) -> None:
        self.layout.ensure()
        lock_path = self.root / "hub.lock"
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self._tick()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _tick(self) -> None:
        self._accept_inbox()
        job_ids = self._job_ids()
        if self.tick_workers <= 1 or len(job_ids) <= 1:
            for job_id in job_ids:
                self._safe_tick_job(job_id)
            return
        with ThreadPoolExecutor(max_workers=self.tick_workers, thread_name_prefix="resalloc") as pool:
            list(pool.map(self._safe_tick_job, job_ids))

    def _safe_tick_job(self, job_id: str) -> None:
        try:
            self._tick_job(job_id)
        except Exception:
            logger.exception("hub tick failed for %s", job_id)

    def _tick_job(self, job_id: str) -> None:
        journal = self._load(job_id)
        self._apply_commands(journal)
        journal = self._load(job_id)
        if journal["state"] == "queued":
            # A crash after the journal is saved leaves the job here. The next tick starts it.
            self._follow(journal, int(journal.get("cursor") if journal.get("cursor") is not None else -1))
            return
        if journal["state"] == "running":
            self._supervise(journal)
        elif journal["state"] == "checkpointing":
            self._await_ack(journal)

    def protected_hashes(self) -> set[str]:
        found: set[str] = set()
        for job_id in self._job_ids():
            journal = self._load(job_id)
            if journal["state"] in TERMINAL:
                continue
            for item in journal.get("datasets") or []:
                if item.get("hash"):
                    found.add(str(item["hash"]))
        return found

    def _accept_inbox(self) -> None:
        inbox = self.layout.inbox_root()
        if not inbox.exists():
            return
        for folder in sorted(path for path in inbox.iterdir() if path.is_dir()):
            if (folder / "rejected").exists():
                continue
            spec_path = folder / "job.json"
            if not spec_path.exists():
                continue
            try:
                spec = read_json(spec_path)
            except Exception:
                logger.exception("inbox %s is not valid json", folder.name)
                (folder / "rejected").write_text("invalid job.json\n", encoding="utf-8")
                continue
            job_id = str(spec.get("id") or "")
            if not safe_job_id(job_id) or folder.name != job_id:
                logger.error("refusing inbox %s with job id %r", folder.name, job_id)
                (folder / "rejected").write_text("unsafe job id\n", encoding="utf-8")
                continue
            dest = self.layout.job(job_id)
            if dest.exists() and (dest / "journal.json").is_file():
                shutil.rmtree(folder)
                continue
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(folder), str(dest))
            try:
                self._open(dest, spec)
            except Exception:
                logger.exception("accepting %s failed", job_id)
                if not (dest / "journal.json").is_file():
                    self._write_failed(dest, spec, "accept_failed")

    def _open(self, job_dir: Path, spec: dict[str, Any]) -> None:
        self._check_spec(spec)
        datasets = list(spec.get("datasets") or [])
        protected = self.protected_hashes() | {
            str(item["hash"]) for item in datasets if item.get("hash") and safe_digest(str(item["hash"]))
        }
        self.disk.evict(protected)
        for item in datasets:
            if item.get("route", "hub") != "hub" or not item.get("hash"):
                continue
            self.disk.touch(str(item["hash"]), str(item["name"]))
            payload = self.layout.cache(str(item["hash"])) / "payload"
            link = job_dir / "datasets" / str(item["name"])
            link.parent.mkdir(parents=True, exist_ok=True)
            if payload.exists():
                link.symlink_to(payload, target_is_directory=payload.is_dir())
        journal = {
            "id": spec["id"],
            "state": "queued",
            "reason": "",
            "cursor": -1,
            "pending_index": None,
            "targets": spec["targets"],
            "confirms": spec["confirms"],
            "limits": spec["limits"],
            "script": spec["script"],
            "requirements": spec.get("requirements"),
            "args": list(spec.get("args") or []),
            "data_timeout_seconds": float(spec.get("data_timeout_seconds") or 3600),
            "datasets": datasets,
            "handle": None,
            "checkpoint_fresh": None,
            "step": None,
            "ack_deadline": None,
            "wait_for_ack": False,
            "pending_action": None,
            "last_error": "",
            "history": [],
            "created_at": dump_dt(self.now()),
            "updated_at": dump_dt(self.now()),
        }
        ensure_job_dirs(job_dir)
        if self.disk.free_bytes() < self.disk.reserve_bytes:
            journal["state"] = "failed"
            journal["reason"] = "disk_watermark"
            self._history(journal, "rejected", "disk_watermark")
            self._save(journal)
            return
        self._save(journal)

    def _supervise(self, journal: dict[str, Any]) -> None:
        handle = running_job_from_dict(journal["handle"])
        provider = self._provider(handle.provider)
        try:
            snapshot = provider.probe(handle)
        except ProviderError as exc:
            # A provider that keeps failing still bills. After a few ticks, treat
            # the workload as gone and move or stop it.
            failures = int(journal.get("probe_failures") or 0) + 1
            journal["probe_failures"] = failures
            journal["last_error"] = str(exc)
            logger.warning("probe failed for %s (%s/%s): %s", journal["id"], failures, self.probe_failures, exc)
            if failures < self.probe_failures:
                self._save(journal)
                return
            snapshot = AccountSnapshot(provider=handle.provider, alive=False, detail="workload_stopped")
        else:
            if journal.get("probe_failures"):
                journal["probe_failures"] = 0
        limits = limits_from_dict(journal["limits"])
        has_next, confirm_next, _index = self._next(journal)
        decision = decide(snapshot, limits, has_next=has_next, confirm_next=confirm_next)
        if decision.action is Action.CONTINUE:
            self._pull_reports(journal, handle, provider)
            self._save(journal)
            return
        remaining, _reason = time_to_limit(snapshot, limits)
        soft, hard = lease_bounds(snapshot, limits, self.now())
        # A finished sandbox rejects reload_volumes. The checkpoint is already
        # on its volume, so pull it instead of signaling.
        if snapshot.alive:
            try:
                provider.signal(handle, signal_for(decision.action), decision.reason, soft, hard)
            except Exception as exc:
                logger.warning("signal failed for %s: %s", journal["id"], exc)
                journal["last_error"] = str(exc)
        journal["state"] = "checkpointing"
        journal["pending_action"] = decision.action.value
        journal["wait_for_ack"] = decision.wait_for_ack
        journal["reason"] = decision.reason
        journal["pending_index"] = self._next(journal)[2]
        if decision.wait_for_ack and remaining is not None:
            deadline = self.now() + max(remaining - limits.hard_buffer, timedelta(seconds=1))
        else:
            deadline = self.now()
        journal["ack_deadline"] = dump_dt(deadline)
        self._history(journal, "signal", decision.reason)
        self._save(journal)
        if not decision.wait_for_ack or self.now() >= deadline:
            self._finalize(journal)

    def _await_ack(self, journal: dict[str, Any]) -> None:
        handle = running_job_from_dict(journal["handle"])
        provider = self._provider(handle.provider)
        ready = False
        try:
            ready = provider.ack_ready(handle)
        except ProviderError as exc:
            journal["last_error"] = str(exc)
        deadline = parse_dt(str(journal["ack_deadline"]))
        if ready or self.now() >= deadline:
            self._finalize(journal)
        else:
            self._save(journal)

    def _pull_reports(self, journal: dict[str, Any], handle: Any, provider: Any) -> None:
        mirror = getattr(provider, "pull_reports", None)
        if mirror is None:
            return
        try:
            mirror(handle)
        except Exception as exc:
            logger.warning("report pull failed for %s: %s", journal["id"], exc)
            journal["last_error"] = str(exc)
            return
        _archive_logs(self.layout.job(str(journal["id"])))

    def _finalize(self, journal: dict[str, Any]) -> None:
        handle = running_job_from_dict(journal["handle"])
        provider = self._provider(handle.provider)
        checkpoint_dir = self.layout.job(journal["id"]) / "checkpoints"
        fresh = False
        pulled = False
        try:
            fresh = provider.ack_ready(handle)
        except Exception as exc:
            journal["last_error"] = str(exc)
        try:
            pulled = provider.pull_checkpoint(handle, checkpoint_dir)
        except Exception as exc:
            logger.warning("checkpoint pull failed for %s: %s", journal["id"], exc)
            journal["last_error"] = str(exc)
            pulled = False
        fresh = bool(fresh and pulled)
        try:
            provider.stop(handle)
        except Exception as exc:
            logger.warning("stop failed for %s: %s", journal["id"], exc)
            journal["last_error"] = str(exc)
        journal["checkpoint_fresh"] = fresh
        journal["step"] = _read_step(checkpoint_dir)
        _archive_logs(self.layout.job(journal["id"]))
        self._history(journal, "checkpoint", journal.get("reason") or "")
        action = journal.get("pending_action")
        reason = journal.get("reason") or ""
        if action == Action.STOP.value or reason == "completed":
            journal["state"] = "completed" if reason == "completed" else "stopped"
            journal["handle"] = None
            self._save(journal)
            return
        if action == Action.PARK.value:
            journal["state"] = "awaiting_confirm"
            journal["handle"] = None
            self._save(journal)
            return
        journal["handle"] = None
        self._save(journal)
        self._follow(journal, int(journal["cursor"]))

    def _follow(self, journal: dict[str, Any], from_index: int) -> None:
        index = from_index
        while True:
            index += 1
            if index >= len(journal["targets"]):
                if journal.get("reason") == "completed":
                    journal["state"] = "completed"
                else:
                    journal["state"] = "stopped"
                    if not journal.get("reason"):
                        journal["reason"] = "no_failover"
                self._save(journal)
                return
            if journal["confirms"][index]:
                journal["state"] = "awaiting_confirm"
                journal["pending_index"] = index
                self._save(journal)
                return
            outcome = self._try_start(journal, index, failover=True)
            if outcome == "started":
                return
            if outcome == "busy":
                journal["state"] = "queued"
                journal["reason"] = "at_capacity"
                journal["last_error"] = f"at capacity for {journal['targets'][index]['type']}"
                self._save(journal)
                return
            if outcome == "disk":
                return

    def _try_start(self, journal: dict[str, Any], index: int, *, failover: bool) -> str:
        """Start one target. Returns started, busy, disk, skip, or failed."""

        try:
            self._start_target(journal, index)
            return "started"
        except ProviderBusy:
            return "busy"
        except DiskWatermarkError as exc:
            journal["state"] = "failed"
            journal["reason"] = "disk_watermark"
            journal["last_error"] = str(exc)
            self._history(journal, "rejected", "disk_watermark")
            self._save(journal)
            return "disk"
        except (ProviderError, LaptopOfflineError) as exc:
            journal["cursor"] = index
            journal["reason"] = str(exc)
            journal["last_error"] = str(exc)
            if failover:
                self._history(journal, "skip", str(exc))
                self._save(journal)
                return "skip"
            journal["state"] = "failed"
            self._history(journal, "resume_failed", str(exc))
            self._save(journal)
            return "failed"

    def _start_target(self, journal: dict[str, Any], index: int) -> None:
        target = journal["targets"][index]
        provider_name = str(target["type"])
        with self._launch_lock:
            if self.disk.free_bytes() < self.disk.reserve_bytes:
                raise DiskWatermarkError("hub disk is inside the reserve")
            if self._at_capacity(provider_name, str(journal["id"])):
                raise ProviderBusy(provider_name)
            self._start_target_locked(journal, index)

    def _start_target_locked(self, journal: dict[str, Any], index: int) -> None:
        target = journal["targets"][index]
        provider = self._provider(str(target["type"]))
        job_dir = self.layout.job(journal["id"])
        ensure_job_dirs(job_dir)
        copy_package(job_dir / "pkg")
        seed_runtime_from_checkpoint(job_dir)
        runtime = job_dir / "runtime"
        for name in ("ack.json", "finished.json", "stop_requested.json"):
            stale = runtime / name
            if stale.exists():
                stale.unlink()
        launch = self._launch(journal, index)
        snapshot = provider.preflight(launch)
        limits = launch.limits
        decision = decide(snapshot, limits, has_next=False, confirm_next=False)
        if decision.action is not Action.CONTINUE:
            raise ProviderError(decision.reason or "limit")
        soft, hard = lease_bounds(snapshot, limits, self.now())
        launch.lease_soft = soft
        launch.lease_hard = hard
        from resalloc.policy import Signal
        from resalloc.providers.runtime_files import publish_runtime

        publish_runtime(launch.runtime_dir, Signal.CONTINUE, "", soft, hard)
        for item in journal.get("datasets") or []:
            digest = str(item.get("hash") or "")
            if safe_digest(digest):
                self.disk.touch(digest, str(item["name"]))
        handle = provider.start(launch)
        journal["handle"] = handle.to_dict()
        endpoint = (handle.connection or {}).get("data_endpoint")
        if endpoint:
            journal["data_endpoint"] = endpoint
        journal["cursor"] = index
        journal["state"] = "running"
        journal["probe_failures"] = 0
        journal["pending_action"] = None
        journal["pending_index"] = None
        journal["reason"] = ""
        journal["ack_deadline"] = None
        self._history(journal, "start", target["type"])
        self._save(journal)

    def _apply_commands(self, journal: dict[str, Any]) -> None:
        commands = self.layout.job(journal["id"]) / "commands"
        if not commands.exists():
            return
        edit_path = commands / "edit.json"
        changed = False
        if edit_path.exists():
            self._edit(journal, read_json(edit_path))
            edit_path.unlink()
            changed = True
        data_path = commands / "data_ready.json"
        if data_path.exists():
            ready = list(journal.get("data_ready") or [])
            ready.append(read_json(data_path))
            journal["data_ready"] = ready
            data_path.unlink()
            changed = True
        confirm_path = commands / "confirm.json"
        if confirm_path.exists():
            self._confirm(journal)
            confirm_path.unlink()
            changed = True
        resume_path = commands / "resume.json"
        if resume_path.exists():
            self._resume(journal)
            resume_path.unlink()
            changed = True
        if changed:
            self._save(journal)

    def _edit(self, journal: dict[str, Any], changes: dict[str, Any]) -> None:
        if journal["state"] != "awaiting_confirm" or journal.get("pending_index") is None:
            journal["last_error"] = "edit is available while the job is waiting for confirmation"
            return
        target = journal["targets"][int(journal["pending_index"])]
        for key, value in changes.items():
            if key in EDITABLE:
                target[key] = value
        self._history(journal, "edit", "settings updated")

    def _confirm(self, journal: dict[str, Any]) -> None:
        if journal["state"] != "awaiting_confirm" or journal.get("pending_index") is None:
            journal["last_error"] = "confirm is available while the job is waiting for confirmation"
            return
        index = int(journal["pending_index"])
        outcome = self._try_start(journal, index, failover=True)
        if outcome == "busy":
            journal["last_error"] = f"at capacity for {journal['targets'][index]['type']}"
            return
        if outcome == "skip":
            self._follow(journal, index)

    def _resume(self, journal: dict[str, Any]) -> None:
        if journal["state"] not in {"stopped", "completed", "failed"}:
            journal["last_error"] = "resume continues a stopped or failed job"
            return
        journal["reason"] = ""
        index = int(journal["cursor"])
        if index < 0:
            index = 0
        outcome = self._try_start(journal, index, failover=False)
        if outcome == "busy":
            journal["state"] = "stopped"
            journal["last_error"] = f"at capacity for {journal['targets'][index]['type']}"

    def _launch(self, journal: dict[str, Any], index: int) -> JobLaunch:
        job_dir = self.layout.job(journal["id"])
        requirements_name = journal.get("requirements")
        requirements = None if not requirements_name else job_dir / "bundle" / str(requirements_name)
        return JobLaunch(
            job_id=str(journal["id"]),
            job_dir=job_dir,
            script=job_dir / "bundle" / str(journal["script"]),
            requirements=requirements,
            datasets=[job_dir / "datasets" / str(item["name"]) for item in journal.get("datasets") or []],
            checkpoint_dir=job_dir / "checkpoints",
            runtime_dir=job_dir / "runtime",
            limits=limits_from_dict(journal["limits"]),
            settings=_launch_settings(journal, index),
            args=list(journal.get("args") or []),
        )

    def _next(self, journal: dict[str, Any]) -> tuple[bool, bool, int | None]:
        index = int(journal["cursor"]) + 1
        if index >= len(journal["targets"]):
            return False, False, None
        return True, bool(journal["confirms"][index]), index

    def _at_capacity(self, provider: str, job_id: str) -> bool:
        per = self.max_running.get(provider)
        total = self.max_total
        if per is None and total is None:
            return False
        running: list[str] = []
        for other in self._job_ids():
            if other == job_id:
                continue
            other_journal = self._load(other)
            if other_journal.get("state") not in {"running", "checkpointing"}:
                continue
            handle = other_journal.get("handle") or {}
            if handle.get("provider"):
                running.append(str(handle["provider"]))
        if per is not None and running.count(provider) >= per:
            return True
        return total is not None and len(running) >= total

    def _check_spec(self, spec: dict[str, Any]) -> None:
        if not safe_relative(str(spec.get("script") or "")):
            raise ValueError("unsafe script path")
        requirements = spec.get("requirements")
        if requirements and not safe_relative(str(requirements)):
            raise ValueError("unsafe requirements path")
        targets = spec.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError("job needs at least one target")
        confirms = spec.get("confirms")
        if not isinstance(confirms, list) or len(confirms) != len(targets):
            raise ValueError("confirms must match targets")
        for item in spec.get("datasets") or []:
            if not isinstance(item, dict):
                raise ValueError("dataset entry must be an object")
            if item.get("name") and not safe_segment(str(item["name"])):
                raise ValueError("unsafe dataset name")
            if item.get("hash") and not safe_digest(str(item["hash"])):
                raise ValueError("unsafe dataset hash")

    def _write_failed(self, job_dir: Path, spec: dict[str, Any], reason: str) -> None:
        ensure_job_dirs(job_dir)
        journal = {
            "id": spec.get("id"),
            "state": "failed",
            "reason": reason,
            "cursor": -1,
            "pending_index": None,
            "targets": list(spec.get("targets") or []),
            "confirms": list(spec.get("confirms") or []),
            "limits": spec.get("limits") or {},
            "script": spec.get("script") or "",
            "requirements": spec.get("requirements"),
            "args": list(spec.get("args") or []),
            "datasets": list(spec.get("datasets") or []),
            "handle": None,
            "history": [],
            "last_error": reason,
            "created_at": dump_dt(self.now()),
            "updated_at": dump_dt(self.now()),
        }
        self._history(journal, "rejected", reason)
        self._save(journal)

    def _provider(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise ProviderError(f"no adapter registered for {name}") from exc

    def _job_ids(self) -> list[str]:
        root = self.layout.jobs_root()
        if not root.exists():
            return []
        return sorted(path.name for path in root.iterdir() if (path / "journal.json").exists())

    def _load(self, job_id: str) -> dict[str, Any]:
        return read_json(self.layout.job(job_id) / "journal.json")

    def _save(self, journal: dict[str, Any]) -> None:
        journal["updated_at"] = dump_dt(self.now())
        write_json(self.layout.job(str(journal["id"])) / "journal.json", journal)

    def _history(self, journal: dict[str, Any], event: str, reason: str) -> None:
        provider = None
        if journal.get("handle"):
            provider = journal["handle"].get("provider")
        journal.setdefault("history", []).append(
            {
                "at": dump_dt(self.now()),
                "provider": provider,
                "event": event,
                "reason": reason,
                "checkpoint_fresh": journal.get("checkpoint_fresh"),
            }
        )


def _launch_settings(journal: dict[str, Any], index: int) -> dict[str, Any]:
    settings = dict(journal["targets"][index])
    names = [
        str(item["name"])
        for item in journal.get("datasets") or []
        if item.get("route") == "direct" and item.get("keep") in _SSH_KEEPS and item.get("name")
    ]
    settings["_wait_datasets"] = names
    settings["_wait_data"] = bool(names)
    settings["_data_timeout"] = float(journal.get("data_timeout_seconds") or 3600)
    return settings


def _archive_logs(job_dir: Path) -> None:
    runtime = job_dir / "runtime"
    logs = job_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for path in runtime.glob("*.log"):
        if path.is_file():
            shutil.copy2(path, logs / path.name)
    artifacts = runtime / "artifacts"
    if artifacts.is_dir():
        for path in artifacts.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".csv", ".log", ".txt"}:
                shutil.copy2(path, logs / path.name)
        previews = artifacts / "previews"
        if previews.is_dir():
            dest = logs / "previews"
            dest.mkdir(parents=True, exist_ok=True)
            for path in previews.glob("*.png"):
                shutil.copy2(path, dest / path.name)


def _read_step(checkpoint_dir: Path) -> int | None:
    path = checkpoint_dir / "state.json"
    if not path.exists():
        return None
    state = read_json(path)
    if not isinstance(state, dict):
        return None
    for key in ("step", "epoch"):
        value = state.get(key)
        if isinstance(value, int):
            return value
    return None


def load_hub_limits(root: Path) -> tuple[dict[str, int], int | None, int, int]:
    """Per-provider caps, a total cap, tick workers, and the probe-failure budget.

    `hub.toml` in the hub root is the file. Environment variables override it:
    `RESALLOC_MAX_RUNNING`, `RESALLOC_MAX_RUNNING_<PROVIDER>`,
    `RESALLOC_TICK_WORKERS`, `RESALLOC_PROBE_FAILURES`.
    """

    per: dict[str, int] = {}
    total: int | None = None
    workers = 1
    probe_failures = 5
    path = root / "hub.toml"
    if path.is_file():
        import tomllib

        with path.open("rb") as handle:
            data = tomllib.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"{path} must be a table")
        if data.get("max_running") is not None:
            total = int(data["max_running"])
        if data.get("tick_workers") is not None:
            workers = int(data["tick_workers"])
        if data.get("probe_failures") is not None:
            probe_failures = int(data["probe_failures"])
        providers = data.get("providers") or {}
        if not isinstance(providers, dict):
            raise ValueError(f"{path} providers must be a table")
        for name, value in providers.items():
            if isinstance(value, dict):
                if value.get("max_running") is not None:
                    per[str(name)] = int(value["max_running"])
            else:
                per[str(name)] = int(value)
    if os.environ.get("RESALLOC_MAX_RUNNING"):
        total = int(os.environ["RESALLOC_MAX_RUNNING"])
    if os.environ.get("RESALLOC_TICK_WORKERS"):
        workers = int(os.environ["RESALLOC_TICK_WORKERS"])
    if os.environ.get("RESALLOC_PROBE_FAILURES"):
        probe_failures = int(os.environ["RESALLOC_PROBE_FAILURES"])
    prefix = "RESALLOC_MAX_RUNNING_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            per[key[len(prefix) :].lower()] = int(value)
    return per, total, max(1, workers), max(1, probe_failures)


def laptop_online(root: Path, timeout: timedelta, now: datetime | None = None) -> bool:
    age = heartbeat_age_seconds(root, now)
    return age is not None and age <= timeout.total_seconds()


def default_providers(root: Path) -> dict[str, Provider]:
    from resalloc.providers import build_default

    return build_default(root)
