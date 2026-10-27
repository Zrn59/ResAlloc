"""Where the hub lives, and how the laptop reaches it."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def config_path() -> Path:
    override = os.environ.get("RESALLOC_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "resalloc" / "config.toml"


def load_file(path: Path | None = None) -> dict:
    target = path or config_path()
    if not target.exists():
        return {}
    with target.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{target} must be a table")
    return data


@dataclass(frozen=True)
class Config:
    hub_root: Path
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int = 22
    ssh_identity: str | None = None

    @property
    def local(self) -> bool:
        return not self.ssh_host


def resolve(hub_root: str | Path | None = None, ssh_host: str | None = None, *, prefer_local: bool = False) -> Config:
    file_data = load_file()
    root_value = hub_root or os.environ.get("RESALLOC_HUB_ROOT") or file_data.get("hub_root")
    if not root_value:
        root_value = Path.home() / "resalloc"
    host: str | None
    if prefer_local:
        host = ssh_host or None
    elif ssh_host is not None:
        host = ssh_host or None
    else:
        env_host = os.environ.get("RESALLOC_SSH_HOST")
        raw = env_host if env_host is not None else file_data.get("ssh_host")
        host = str(raw) if raw else None
    user = file_data.get("ssh_user") or None
    port = int(file_data.get("ssh_port") or 22)
    identity = file_data.get("ssh_identity") or os.environ.get("RESALLOC_SSH_IDENTITY") or None
    return Config(
        hub_root=Path(root_value),
        ssh_host=host,
        ssh_user=str(user) if user else None,
        ssh_port=port,
        ssh_identity=str(identity) if identity else None,
    )
