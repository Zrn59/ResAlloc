"""API imported by the training script, on every provider.

The script stays the same. pulse() tells it to keep going, save and move, or stop.
Heavy saving belongs in an on_stop callback: that runs before the ack is written.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from resalloc.io import parse_dt, read_json, utcnow, write_json
from resalloc.names import safe_segment
from resalloc.policy import Signal


class Worker:
    def __init__(self, job_dir: str | Path | None = None) -> None:
        raw = job_dir if job_dir is not None else os.environ.get("RESALLOC_JOB_DIR")
        if not raw:
            raise RuntimeError("RESALLOC_JOB_DIR is not set and no job_dir was given")
        self.job_dir = Path(raw)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self._callbacks: list[Callable[[], None]] = []

    def load(self) -> dict[str, Any]:
        path = self.job_dir / "state.json"
        if not path.exists():
            return {}
        data = read_json(path)
        if not isinstance(data, dict):
            raise RuntimeError("state.json must be an object")
        return data

    def save(self, state: dict[str, Any]) -> None:
        write_json(self.job_dir / "state.json", state)
        hook = os.environ.get("RESALLOC_AFTER_SAVE")
        if hook:
            subprocess.run(shlex.split(hook), check=False)

    def path(self, name: str) -> Path:
        """A file under this job that the hub will pick up with the checkpoint."""

        target = self.job_dir / "artifacts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def dataset_root(self, dataset: str) -> Path:
        """Find a dataset at the mount the provider actually used.

        RESALLOC_JOB_DIR is /resalloc/runtime, but hub and Runpod data live in
        /resalloc/datasets, a Modal volume is /data, and Kaggle mounts /kaggle/input.
        A finished upload (COMPLETE or a .done marker) wins. An empty directory
        does not hide a mount that shows up later.
        """

        if not safe_segment(dataset):
            raise ValueError("dataset name must be a single path segment")
        named = self._dataset_candidates(dataset)
        for path in named:
            if (path / "COMPLETE").exists() or (path.is_dir() and any(path.glob("*.done"))):
                return path
        for path in named:
            if path.is_dir() and any(path.iterdir()):
                return path
        for path in named:
            if path.is_dir():
                return path
        return named[0]

    def _dataset_candidates(self, dataset: str) -> list[Path]:
        named: list[Path] = []
        data_dir = os.environ.get("RESALLOC_DATA_DIR")
        if data_dir:
            named.append(Path(data_dir) / dataset)
        named.append(self.job_dir.parent / "datasets" / dataset)
        named.append(self.job_dir / "datasets" / dataset)
        named.append(Path("/data") / dataset)
        kaggle = Path("/kaggle/input")
        named.append(kaggle / dataset)
        if kaggle.is_dir():
            named.extend(_named_dirs(kaggle, dataset, depth=4))
        unique: list[Path] = []
        seen: set[str] = set()
        for path in named:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def wait_data(self, dataset: str, timeout: float | None = None) -> Path:
        """Block until a direct upload has written COMPLETE for this dataset."""

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            root = self.dataset_root(dataset)
            if (root / "COMPLETE").exists():
                return root
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"{dataset} did not finish streaming")
            time.sleep(0.2)

    def iter_ready(self, dataset: str, poll: float = 0.2):
        """Yield files as a stream marks them done. Stops after COMPLETE."""

        seen: set[str] = set()
        while True:
            root = self.dataset_root(dataset)
            if root.is_dir():
                for marker in sorted(root.rglob("*.done")):
                    data = marker.with_name(marker.name[: -len(".done")])
                    if not data.is_file():
                        continue
                    key = data.relative_to(root).as_posix()
                    if key in seen:
                        continue
                    seen.add(key)
                    yield data
            if (root / "COMPLETE").exists():
                return
            time.sleep(poll)

    def on_stop(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def pulse(self) -> Signal:
        write_json(self.job_dir / "pulse.json", {"at": utcnow().isoformat()})
        signal = self._signal()
        if signal is Signal.CONTINUE:
            return signal
        for callback in self._callbacks:
            callback()
        write_json(
            self.job_dir / "ack.json",
            {
                "signal": signal.value,
                "at": utcnow().isoformat(),
                "state": self.load() if (self.job_dir / "state.json").exists() else {},
            },
        )
        return signal

    def _signal(self) -> Signal:
        now = utcnow()
        command = "continue"
        control_path = self.job_dir / "control.json"
        if control_path.exists():
            command = str(read_json(control_path).get("command") or "continue")
        lease_path = self.job_dir / "lease.json"
        soft_hit = False
        hard_hit = False
        if lease_path.exists():
            lease = read_json(lease_path)
            if lease.get("hard_at") and now >= parse_dt(str(lease["hard_at"])):
                hard_hit = True
            if lease.get("soft_at") and now >= parse_dt(str(lease["soft_at"])):
                soft_hit = True
        if command == "stop" or hard_hit:
            return Signal.STOP
        if command == Signal.CHECKPOINT_AND_MIGRATE.value or soft_hit:
            return Signal.CHECKPOINT_AND_MIGRATE
        return Signal.CONTINUE


def _named_dirs(root: Path, name: str, depth: int) -> list[Path]:
    """Directories named exactly `name`, a few levels down. The name is not a glob."""

    found: list[Path] = []
    if depth < 0 or not root.is_dir():
        return found
    try:
        entries = list(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name == name:
            found.append(entry)
        elif depth:
            found.extend(_named_dirs(entry, name, depth - 1))
    return found
