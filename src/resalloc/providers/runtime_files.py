"""control.json and lease.json, the files a worker pulse() reads."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from resalloc.io import dump_dt, read_json, write_json
from resalloc.policy import Signal


def publish_runtime(
    runtime_dir: Path,
    signal: Signal,
    reason: str,
    lease_soft: datetime,
    lease_hard: datetime,
) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        runtime_dir / "control.json",
        {"command": signal.value, "reason": reason},
    )
    write_json(
        runtime_dir / "lease.json",
        {"soft_at": dump_dt(lease_soft), "hard_at": dump_dt(lease_hard)},
    )


def ack_is_ready(runtime_dir: Path) -> bool:
    path = runtime_dir / "ack.json"
    if not path.exists():
        return False
    data = read_json(path)
    return bool(data.get("signal"))


def store_checkpoint(runtime_dir: Path, checkpoint_dir: Path) -> bool:
    """Copy worker output from a runtime directory into the hub checkpoint."""

    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    state = runtime_dir / "state.json"
    found = False
    if state.exists():
        shutil.copy2(state, checkpoint_dir / "state.json")
        found = True
    artifacts = runtime_dir / "artifacts"
    if artifacts.exists():
        target = checkpoint_dir / "artifacts"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(artifacts, target)
        found = True
    ack = runtime_dir / "ack.json"
    if ack.exists():
        shutil.copy2(ack, checkpoint_dir / "ack.json")
    return found
