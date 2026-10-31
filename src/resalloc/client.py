"""Submit a job from the laptop. The hub takes it from the inbox."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from resalloc.config import load_file, resolve
from resalloc.data import describe
from resalloc.disk import dir_size
from resalloc.errors import DiskWatermarkError, LaptopOfflineError
from resalloc.hashing import hash_tree
from resalloc.io import write_json
from resalloc.models import Dataset, Job, merge_limits
from resalloc.names import job_folder_name
from resalloc.transport import make_transport


def new_job_id(label: str, taken: Any = None) -> str:
    """A directory name a person can read. Retries when that folder already exists."""

    for _ in range(8):
        job_id = job_folder_name(label)
        if taken is None or not taken(job_id):
            return job_id
    return job_folder_name(label)


class _Call:
    def __init__(self, method: str) -> None:
        self.method = method

    def __get__(self, obj: Any, owner: type) -> Any:
        if obj is None:
            def forwarded(*args: Any, **kwargs: Any) -> Any:
                hub_root = kwargs.pop("hub_root", None)
                ssh_host = kwargs.pop("ssh_host", None)
                reserve_bytes = kwargs.pop("reserve_bytes", None)
                return getattr(owner(hub_root=hub_root, ssh_host=ssh_host, reserve_bytes=reserve_bytes), self.method)(*args, **kwargs)

            return forwarded
        return getattr(obj, self.method)


class ResAlloc:
    """Laptop-side handle. ResAlloc.submit(job) uses the config file and environment."""

    def __init__(
        self,
        hub_root: str | Path | None = None,
        ssh_host: str | None = None,
        reserve_bytes: int | None = None,
    ) -> None:
        self.config = resolve(hub_root, ssh_host, prefer_local=hub_root is not None and ssh_host is None)
        self.transport = make_transport(self.config, reserve_bytes)

    def _job_taken(self, job_id: str) -> bool:
        if not self.config.local:
            return False
        root = self.config.hub_root
        return (root / "jobs" / job_id).exists() or (root / "inbox" / job_id).exists()

    def _submit(self, job: Job) -> str:
        saved = load_file().get("limits")
        limits = merge_limits(job.limits, saved if isinstance(saved, dict) else None)
        script = Path(job.script)
        if not script.is_file():
            raise FileNotFoundError(script)
        requirements = Path(job.requirements) if job.requirements else None
        if requirements is not None and not requirements.is_file():
            raise FileNotFoundError(requirements)
        if job.start.to_dict()["type"] == "laptop":
            status = self.transport.agent_status(limits.laptop_heartbeat_timeout.total_seconds())
            if not status["online"]:
                raise LaptopOfflineError(
                    "start is the laptop, and it is not heartbeating. Run `resalloc laptop agent` on it."
                )
        needed = dir_size(script) + (dir_size(requirements) if requirements else 0)
        meta = []
        external: list[tuple[Dataset, Path]] = []
        for dataset in job.datasets:
            path = Path(dataset.path)
            if not path.exists():
                raise FileNotFoundError(path)
            described = describe(dataset, job.start)
            if dataset.route == "hub":
                digest = hash_tree(path)
                size = 0 if self.transport.cache_exists(digest) else dir_size(path)
                needed += size
                if size:
                    # Space is checked once, then every hub dataset is uploaded.
                    pass
                described["hash"] = digest
                described["bytes"] = dir_size(path)
                meta.append((path, digest, dataset.name, size, described))
            else:
                described["bytes"] = dir_size(path)
                external.append((dataset, path))
                meta.append((None, None, dataset.name, 0, described))
        self._ensure_space(needed)
        shipped = []
        for path, digest, name, size, described in meta:
            if path is not None and size:
                self.transport.upload_cache(path, digest, str(name))
            shipped.append({key: value for key, value in described.items() if key != "path"})
        label = (job.name or "").strip() or script.stem
        job_id = new_job_id(label, self._job_taken)
        pre = []
        post = []
        for dataset, path in external:
            keep = str(describe(dataset, job.start)["keep"])
            if keep in {"modal", "kaggle"}:
                pre.append((dataset, path))
            else:
                post.append((dataset, path))
        # Volume bytes land before the hub can start a GPU on the inbox job.
        self._ship_stores(pre, job)
        with tempfile.TemporaryDirectory(prefix="resalloc-submit-") as temporary:
            bundle_root = Path(temporary)
            bundle = bundle_root / "bundle"
            bundle.mkdir()
            shutil.copy2(script, bundle / script.name)
            if requirements is not None:
                shutil.copy2(requirements, bundle / requirements.name)
            for raw in job.bundle_paths:
                src = Path(raw)
                if not src.exists():
                    raise FileNotFoundError(src)
                dest = bundle / src.name
                if src.is_dir():
                    shutil.copytree(
                        src,
                        dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                    )
                else:
                    shutil.copy2(src, dest)
            write_json(
                bundle_root / "job.json",
                {
                    "id": job_id,
                    "name": label,
                    "script": script.name,
                    "requirements": None if requirements is None else requirements.name,
                    "args": list(job.args),
                    "datasets": shipped,
                    "limits": limits.to_dict(),
                    "targets": job.targets(),
                    "confirms": job.confirms(),
                    "data_timeout_seconds": job.data_timeout_seconds,
                },
            )
            self.transport.upload_inbox(bundle_root, job_id)
        self.deliver_external(job_id, post, job)
        return job_id

    def _ship_stores(self, external: list[tuple[Dataset, Path]], job: Job) -> None:
        for dataset, path in external:
            described = describe(dataset, job.start)
            self._upload_store(
                str(described["keep"]),
                str(described.get("store") or ""),
                path,
                dataset.route,
                str(dataset.name),
            )

    def deliver_external(self, job_id: str, external: list[tuple[Dataset, Path]], job: Job) -> None:
        """Send direct and streamed datasets from this machine, not via the hub disk.

        Modal and Kaggle stores are filled before the GPU is needed. A Runpod
        pod has to exist first, so that upload waits for the SSH endpoint and
        then writes straight to the pod.
        """

        for dataset, path in external:
            described = describe(dataset, job.start)
            keep = str(described["keep"])
            store = str(described.get("store") or "")
            if keep in {"modal", "kaggle"}:
                self._upload_store(keep, store, path, dataset.route, str(dataset.name))
            elif keep in {"runpod", "vast", "machine"}:
                endpoint = self._wait_data_endpoint(job_id, job.data_timeout_seconds)
                self._stream_ssh(endpoint, path, str(dataset.name), dataset.route)
            elif keep == "laptop":
                continue
            else:
                raise RuntimeError(f"don't know how to deliver to {keep}")
            self.transport.write_command(
                job_id,
                "data_ready",
                {"name": dataset.name, "keep": keep, "store": store, "route": dataset.route},
            )

    def _upload_store(self, keep: str, store: str, path: Path, route: str, name: str) -> None:
        if keep == "modal":
            from resalloc.data_stores import upload_modal

            upload_modal(store, path, route == "stream", prefix=name)
            return
        if keep == "kaggle":
            from resalloc.data_stores import upload_kaggle

            upload_kaggle(store or path.name, path)
            return
        raise RuntimeError(f"don't know how to deliver to {keep}")

    def _wait_data_endpoint(self, job_id: str, timeout: float) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + timeout
        while True:
            journal = self.transport.read_journal(job_id)
            endpoint = journal.get("data_endpoint")
            if isinstance(endpoint, dict) and endpoint.get("provider"):
                return endpoint
            if journal.get("state") in {"failed", "stopped"}:
                raise RuntimeError(journal.get("reason") or "job ended before the data endpoint was ready")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{job_id} did not publish a data endpoint")
            time.sleep(2)

    def _stream_ssh(self, endpoint: dict[str, Any], path: Path, name: str, route: str) -> Path:
        from resalloc.data_stores import upload_runpod

        upload_runpod(endpoint, path, name, route == "stream")
        return path

    def _ensure_space(self, needed: int) -> None:
        info = self.transport.disk_info()
        if info["free"] - needed >= info["reserve"]:
            return
        self.transport.evict()
        info = self.transport.disk_info()
        if info["free"] - needed < info["reserve"]:
            raise DiskWatermarkError(
                "not enough free space on the hub for this upload. "
                f"free={info['free']} reserve={info['reserve']} need={needed}"
            )

    def _status(self, job_id: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        if job_id:
            return self.transport.read_journal(job_id)
        return self.transport.list_journals()

    def _confirm(self, job_id: str) -> None:
        self.transport.write_command(job_id, "confirm", {})

    def _edit(self, job_id: str, **changes: Any) -> None:
        self.transport.write_command(job_id, "edit", changes)

    def _resume(self, job_id: str) -> None:
        self.transport.write_command(job_id, "resume", {})

    submit = _Call("_submit")
    status = _Call("_status")
    confirm = _Call("_confirm")
    edit = _Call("_edit")
    resume = _Call("_resume")
