"""Local disk and SSH. The laptop uses SSH; tests and the hub use the disk."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from resalloc.config import Config
from resalloc.disk import DiskBudget, configured_reserve, dir_size
from resalloc.layout import Layout
from resalloc.records import list_journals, protected_hashes, read_journal, write_command


class LocalTransport:
    same_filesystem = True

    def __init__(self, root: Path, reserve_bytes: int | None = None) -> None:
        self.root = root
        self.layout = Layout(root)
        self.disk = DiskBudget(root, configured_reserve(reserve_bytes, root=root))
        self.layout.ensure()

    def cache_exists(self, digest: str) -> bool:
        return (self.layout.cache(digest) / "payload").exists()

    def disk_info(self) -> dict[str, int]:
        return self.disk.info()

    def evict(self) -> None:
        self.disk.evict(set(protected_hashes(self.root)))

    def protected(self) -> list[str]:
        return protected_hashes(self.root)

    def upload_cache(self, source: Path, digest: str, name: str) -> None:
        payload = self.layout.cache(digest) / "payload"
        if payload.exists():
            return
        payload.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            import shutil

            shutil.copytree(source, payload)
        else:
            payload.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(source, payload / source.name)
        self.disk.touch(digest, name, dir_size(payload))

    def upload_inbox(self, source: Path, job_id: str) -> None:
        import shutil

        dest = self.layout.inbox(job_id)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)

    def read_journal(self, job_id: str) -> dict[str, Any]:
        return read_journal(self.root, job_id)

    def list_journals(self) -> list[dict[str, Any]]:
        return list_journals(self.root)

    def write_command(self, job_id: str, name: str, payload: dict[str, Any]) -> None:
        write_command(self.root, job_id, name, payload)

    def heartbeat(self) -> None:
        from resalloc.providers.laptop import write_heartbeat

        write_heartbeat(self.root)

    def agent_status(self, timeout_seconds: float) -> dict[str, Any]:
        from resalloc.providers.laptop import heartbeat_age_seconds

        age = heartbeat_age_seconds(self.root)
        online = age is not None and age <= timeout_seconds
        return {"online": online, "age_seconds": age}

    def laptop_jobs(self) -> list[dict[str, Any]]:
        jobs = []
        for journal in self.list_journals():
            handle = journal.get("handle") or {}
            if journal.get("state") in {"running", "checkpointing"} and handle.get("provider") == "laptop":
                jobs.append(journal)
        return jobs

    def job_dir(self, job_id: str) -> Path:
        return self.layout.job(job_id)

    def read_job_file(self, job_id: str, relative: str) -> str | None:
        path = _job_file(self.layout.job(job_id), relative)
        if not path.exists() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")


class SshTransport:
    same_filesystem = False

    def __init__(self, config: Config) -> None:
        if not config.ssh_host:
            raise ValueError("SSH transport needs ssh_host")
        self.config = config

    def _remote(self, argv: list[str]) -> str:
        return remote_shell(str(self.config.hub_root), argv)

    def _ssh(self, argv: list[str]) -> str:
        command = ["ssh", "-o", "BatchMode=yes"]
        if self.config.ssh_identity:
            command += ["-i", self.config.ssh_identity]
        if self.config.ssh_port:
            command += ["-p", str(self.config.ssh_port)]
        target = self.config.ssh_host or ""
        if self.config.ssh_user:
            target = f"{self.config.ssh_user}@{target}"
        command.append(target)
        command.append(self._remote(argv))
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or f"ssh exited {completed.returncode}")
        return completed.stdout

    def _rsync(self, source: Path, remote_rel: str) -> None:
        remote_path = str(Path(self.config.hub_root) / remote_rel)
        self._ssh(["mkdir", "-p", remote_path])
        ssh_cmd = "ssh -o BatchMode=yes"
        if self.config.ssh_port:
            ssh_cmd += f" -p {self.config.ssh_port}"
        if self.config.ssh_identity:
            ssh_cmd += f" -i {shlex.quote(self.config.ssh_identity)}"
        target = self.config.ssh_host or ""
        if self.config.ssh_user:
            target = f"{self.config.ssh_user}@{target}"
        local = str(source)
        if source.is_dir():
            local = local.rstrip("/") + "/"
        remote = f"{target}:{remote_path}/"
        completed = subprocess.run(
            ["rsync", "-a", "-e", ssh_cmd, local, remote],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or f"rsync exited {completed.returncode}")

    def cache_exists(self, digest: str) -> bool:
        text = self._ssh(["resalloc", "cache", "has", digest]).strip()
        return text == "yes"

    def disk_info(self) -> dict[str, int]:
        return json.loads(self._ssh(["resalloc", "disk", "info"]))

    def evict(self) -> None:
        self._ssh(["resalloc", "cache", "evict"])

    def protected(self) -> list[str]:
        return json.loads(self._ssh(["resalloc", "jobs", "protected"]))

    def upload_cache(self, source: Path, digest: str, name: str) -> None:
        self._rsync(source, f"cache/datasets/{digest}/payload")
        self._ssh(["resalloc", "cache", "touch", digest, name])

    def upload_inbox(self, source: Path, job_id: str) -> None:
        self._rsync(source, f"inbox/{job_id}")

    def read_journal(self, job_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._ssh(["resalloc", "status", job_id, "--json"]))
        except RuntimeError as exc:
            text = str(exc)
            if "journal.json" in text or "No such file" in text:
                return {"id": job_id, "state": "queued", "reason": "waiting for the hub"}
            raise

    def list_journals(self) -> list[dict[str, Any]]:
        return json.loads(self._ssh(["resalloc", "status", "--json"]))

    def write_command(self, job_id: str, name: str, payload: dict[str, Any]) -> None:
        if name == "confirm":
            self._ssh(["resalloc", "confirm", job_id])
            return
        if name == "resume":
            self._ssh(["resalloc", "resume", job_id])
            return
        if name == "edit":
            argv = ["resalloc", "edit", job_id, "--json", json.dumps(payload)]
            self._ssh(argv)
            return
        if name == "data_ready":
            self._ssh(["resalloc", "jobs", "data-ready", job_id, "--json", json.dumps(payload)])
            return
        raise ValueError(f"unknown command {name}")

    def heartbeat(self) -> None:
        self._ssh(["resalloc", "laptop", "heartbeat"])

    def agent_status(self, timeout_seconds: float) -> dict[str, Any]:
        text = self._ssh(["resalloc", "laptop", "status", "--timeout", str(timeout_seconds)])
        return json.loads(text)

    def laptop_jobs(self) -> list[dict[str, Any]]:
        return json.loads(self._ssh(["resalloc", "laptop", "poll"]))

    def pull_job(self, job_id: str, local_dir: Path) -> None:
        remote = str(Path(self.config.hub_root) / "jobs" / job_id) + "/"
        local_dir.mkdir(parents=True, exist_ok=True)
        ssh_cmd = "ssh -o BatchMode=yes"
        if self.config.ssh_port:
            ssh_cmd += f" -p {self.config.ssh_port}"
        if self.config.ssh_identity:
            ssh_cmd += f" -i {shlex.quote(self.config.ssh_identity)}"
        target = self.config.ssh_host or ""
        if self.config.ssh_user:
            target = f"{self.config.ssh_user}@{target}"
        completed = subprocess.run(
            ["rsync", "-a", "-u", "-e", ssh_cmd, f"{target}:{remote}", str(local_dir) + "/"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "rsync pull failed")

    def push_runtime_outputs(self, job_id: str, runtime: Path) -> None:
        remote = f"jobs/{job_id}/runtime"
        for name in ("state.json", "ack.json", "pulse.json", "finished.json"):
            path = runtime / name
            if path.exists():
                self._rsync(path, f"{remote}/{name}")
        artifacts = runtime / "artifacts"
        if artifacts.exists():
            self._rsync(artifacts, f"{remote}/artifacts")

    def read_job_file(self, job_id: str, relative: str) -> str | None:
        job_dir = Path(self.config.hub_root) / "jobs" / job_id
        path = _job_file(job_dir, relative)
        try:
            return self._ssh(["cat", str(path)])
        except RuntimeError as exc:
            if "no such file" in str(exc).lower() or "not a directory" in str(exc).lower():
                return None
            raise


def _job_file(job_dir: Path, relative: str) -> Path:
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError(f"job file must stay inside the job directory: {relative}")
    path = (job_dir / relative).resolve()
    root = job_dir.resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"job file must stay inside the job directory: {relative}")
    return path


def remote_shell(hub_root: str, argv: list[str]) -> str:
    """Command run on the hub. ~/.local/bin is not on the default SSH PATH."""

    quoted = " ".join(shlex.quote(part) for part in argv)
    root = shlex.quote(hub_root)
    return f"PATH=$HOME/.local/bin:$PATH RESALLOC_HUB_ROOT={root} RESALLOC_SSH_HOST= {quoted}"


def make_transport(config: Config, reserve_bytes: int | None = None) -> LocalTransport | SshTransport:
    if config.local:
        return LocalTransport(config.hub_root, reserve_bytes)
    return SshTransport(config)


def write_cache_touch(root: Path, digest: str, name: str) -> None:
    DiskBudget(root).touch(digest, name)
