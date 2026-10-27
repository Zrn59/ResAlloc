"""Outbound laptop worker. The hub never connects inbound."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from resalloc.config import Config
from resalloc.io import write_json
from resalloc.transport import LocalTransport, SshTransport, make_transport


class LaptopAgent:
    def __init__(self, config: Config, work_root: Path | None = None) -> None:
        self.config = config
        self.transport = make_transport(config)
        self.work_root = work_root or (Path.home() / ".resalloc-work")
        self.procs: dict[str, subprocess.Popen[bytes]] = {}

    def once(self) -> None:
        self.transport.heartbeat()
        self._reap()
        for journal in self.transport.laptop_jobs():
            self._manage(journal)
        self._reap()

    def _manage(self, journal: dict[str, Any]) -> None:
        job_id = str(journal["id"])
        if isinstance(self.transport, LocalTransport):
            self._ensure_local_proc(journal, self.transport.job_dir(job_id))
            return
        assert isinstance(self.transport, SshTransport)
        local = self.work_root / job_id
        self.transport.pull_job(job_id, local)
        self._ensure_local_proc(journal, local)
        runtime = local / "runtime"
        if runtime.exists():
            self.transport.push_runtime_outputs(job_id, runtime)

    def _ensure_local_proc(self, journal: dict[str, Any], job_dir: Path) -> None:
        job_id = str(journal["id"])
        existing = self.procs.get(job_id)
        if existing is not None:
            return
        runtime = job_dir / "runtime"
        finished = runtime / "finished.json"
        if finished.exists():
            return
        script = job_dir / "bundle" / str(journal["script"])
        if not script.exists():
            return
        env = os.environ.copy()
        env["RESALLOC_JOB_DIR"] = str(runtime)
        env["RESALLOC_DATA_DIR"] = str(job_dir / "datasets")
        env["RESALLOC_JOB_ID"] = job_id
        requirements = job_dir / "bundle" / "requirements.txt"
        if requirements.is_file() and not (runtime / ".requirements.ok").exists():
            installed = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
                check=False,
            )
            if installed.returncode != 0:
                runtime.mkdir(parents=True, exist_ok=True)
                write_json(runtime / "finished.json", {"returncode": installed.returncode})
                return
            (runtime / ".requirements.ok").write_text("ok\n", encoding="utf-8")
        if isinstance(self.transport, SshTransport):
            env["RESALLOC_AFTER_SAVE"] = f"{sys.executable} -m resalloc laptop push {job_id}"
        pkg = str(job_dir / "pkg")
        env["PYTHONPATH"] = pkg + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.procs[job_id] = subprocess.Popen(
            [sys.executable, str(script), *[str(arg) for arg in journal.get("args") or []]],
            cwd=str(job_dir / "bundle"),
            env=env,
        )

    def _reap(self) -> None:
        finished_ids = []
        for job_id, proc in self.procs.items():
            code = proc.poll()
            if code is None:
                continue
            runtime = self._runtime_for(job_id)
            if runtime is not None:
                write_json(runtime / "finished.json", {"returncode": code})
            finished_ids.append(job_id)
        for job_id in finished_ids:
            if isinstance(self.transport, SshTransport):
                runtime = self.work_root / job_id / "runtime"
                if runtime.exists():
                    self.transport.push_runtime_outputs(job_id, runtime)
            del self.procs[job_id]

    def _runtime_for(self, job_id: str) -> Path | None:
        if isinstance(self.transport, LocalTransport):
            runtime = self.transport.job_dir(job_id) / "runtime"
        else:
            runtime = self.work_root / job_id / "runtime"
        if runtime.exists():
            return runtime
        return None
