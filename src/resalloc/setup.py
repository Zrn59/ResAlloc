"""Config checks, installing the hub over SSH, and the confirm prompt."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from resalloc.config import Config, config_path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def parse_edits(text: str) -> dict[str, Any]:
    """`gpu=NVIDIA GeForce RTX 3090, spot=true, disk_gb=40`."""

    changes: dict[str, Any] = {}
    numeric = {"disk_gb", "cpus", "gpu_count", "memory_mb", "ram_gb", "timeout_hours", "cpu", "max_price"}
    flags = {"spot", "internet"}
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"edit needs key=value, got {piece!r}")
        key, raw = piece.split("=", 1)
        key = key.strip()
        value = raw.strip()
        if key in flags:
            changes[key] = value.lower() in {"1", "true", "yes", "on"}
        elif key in numeric:
            changes[key] = float(value) if "." in value else int(value)
        else:
            changes[key] = value
    return changes


def format_journal(journal: dict[str, Any]) -> str:
    handle = journal.get("handle") or {}
    provider = handle.get("provider") or "-"
    line = (
        f"{journal.get('id')}  {journal.get('state')}  provider={provider}  "
        f"reason={journal.get('reason') or '-'}  step={journal.get('step')}  "
        f"fresh={journal.get('checkpoint_fresh')}"
    )
    index = journal.get("pending_index")
    if journal.get("state") == "awaiting_confirm" and index is not None:
        target = journal["targets"][int(index)]
        bits = [str(target.get("type"))]
        for key in ("gpu", "accelerator", "image", "spot"):
            if target.get(key) not in (None, ""):
                bits.append(f"{key}={target[key]}")
        line += "  next=" + " ".join(bits)
    if journal.get("last_error"):
        line += f"  error={journal['last_error']}"
    return line


def secret_names(text: str) -> list[str]:
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            found.append(key.strip())
    return found


def local_secret_report() -> dict[str, bool]:
    home = Path.home()
    return {
        "RUNPOD_API_KEY": bool(os.environ.get("RUNPOD_API_KEY")),
        "modal": home.joinpath(".modal.toml").exists() or bool(os.environ.get("MODAL_TOKEN_ID")),
        "kaggle": home.joinpath(".kaggle/kaggle.json").exists() or bool(os.environ.get("KAGGLE_USERNAME")),
        "vast": bool(os.environ.get("VAST_API_KEY")),
    }


def _import_probe() -> str:
    return textwrap.dedent(
        """\
        import json, os, shutil
        from pathlib import Path
        home = Path.home()
        hub = Path(os.environ["RESALLOC_HUB_ROOT"])
        usage = shutil.disk_usage(str(home))
        secrets_path = hub / "secrets.env"
        names = []
        if secrets_path.exists():
            for raw in secrets_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if value.strip():
                    names.append(key.strip())
        import_ok = False
        import_error = ""
        try:
            import resalloc
            import_ok = True
        except Exception as exc:
            import_error = str(exc)
        import subprocess
        reserve = os.environ.get("RESALLOC_RESERVE_BYTES") or str(20 * 1024**3)
        systemd_user = False
        try:
            systemd_user = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0
        except FileNotFoundError:
            systemd_user = False
        print(json.dumps({
            "python": os.popen("python3 --version").read().strip(),
            "arch": os.uname().machine,
            "free": usage.free,
            "total": usage.total,
            "reserve": int(reserve),
            "systemd_user": systemd_user,
            "hub": str(hub),
            "hub_exists": hub.exists(),
            "resalloc_bin": shutil.which("resalloc") or "",
            "import_ok": import_ok,
            "import_error": import_error,
            "secrets": names,
            "modal": (home / ".modal.toml").exists(),
            "kaggle": (home / ".kaggle" / "kaggle.json").exists(),
            "runpod_env": bool(os.environ.get("RUNPOD_API_KEY")),
            "service": os.popen("systemctl --user is-active resalloc-hub.service").read().strip(),
            "linger": os.popen("loginctl show-user $(id -un) -p Linger").read().strip(),
        }))
        """
    )


def checks_from_probe(config: Config, probe: dict[str, Any] | None, ssh_error: str = "") -> list[Check]:
    rows = [
        Check("config", config_path().exists() and bool(config.ssh_host), f"ssh_host={config.ssh_host or '-'} hub={config.hub_root}"),
    ]
    if ssh_error:
        rows.append(Check("ssh", False, ssh_error))
        return rows
    if probe is None:
        rows.append(Check("ssh", False, "no probe"))
        return rows
    rows.append(Check("ssh", True, f"{probe.get('arch')} {probe.get('python')}"))
    free = int(probe.get("free") or 0)
    reserve = int(probe["reserve"]) if probe.get("reserve") is not None else 20 * 1024**3
    rows.append(Check("disk", free > reserve, f"free={free} reserve={reserve}"))
    rows.append(Check("package", bool(probe.get("import_ok")), probe.get("import_error") or "import resalloc"))
    rows.append(Check("command", bool(probe.get("resalloc_bin")), probe.get("resalloc_bin") or "resalloc is not on PATH"))
    if probe.get("systemd_user") is False:
        rows.append(Check("hub", True, "no user systemd; on the hub run `resalloc hub run`"))
    else:
        service = str(probe.get("service") or "")
        rows.append(Check("hub", service == "active", service or "not installed"))
        linger = str(probe.get("linger") or "")
        rows.append(Check("linger", "Linger=yes" in linger, linger or "unknown"))
    secret_bits = list(probe.get("secrets") or [])
    if probe.get("runpod_env"):
        secret_bits.append("RUNPOD_API_KEY")
    if probe.get("modal"):
        secret_bits.append("modal")
    if probe.get("kaggle"):
        secret_bits.append("kaggle")
    rows.append(
        Check(
            "secrets",
            True,
            ", ".join(secret_bits) or "no cloud keys; laptop and an SSH machine need none",
        )
    )
    names = set(secret_bits)
    uses_kaggle = bool(probe.get("kaggle")) or bool(names & {"KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN"})
    if uses_kaggle:
        rows.append(
            Check(
                "kaggle_token",
                "KAGGLE_API_TOKEN" in names,
                "KAGGLE_API_TOKEN"
                if "KAGGLE_API_TOKEN" in names
                else "the Kaggle CLI reads KAGGLE_API_TOKEN or ~/.kaggle/access_token. "
                "KAGGLE_KEY and kaggle.json are the old format, so a token created in the "
                "Kaggle UI stays unused until that line is in secrets.env",
            )
        )
    return rows


def wrapper_script(share_dir: str) -> str:
    return textwrap.dedent(
        f"""\
        #!/bin/sh
        export PYTHONPATH={shlex.quote(share_dir)}${{PYTHONPATH:+:$PYTHONPATH}}
        export RESALLOC_SSH_HOST=
        if [ -x "$HOME/resalloc-venv/bin/python" ]; then
          exec "$HOME/resalloc-venv/bin/python" -m resalloc "$@"
        fi
        if command -v python3 >/dev/null 2>&1; then
          exec python3 -m resalloc "$@"
        fi
        echo "python3 is not installed" >&2
        exit 1
        """
    )


def hub_unit(binary: str, hub_root: str, reserve_bytes: int | None = None) -> str:
    reserve = ""
    if reserve_bytes is not None:
        reserve = f"        Environment=RESALLOC_RESERVE_BYTES={reserve_bytes}\n"
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=resalloc hub
        After=network-online.target

        [Service]
        Type=simple
        ExecStart={binary} hub run
        Environment=RESALLOC_HUB_ROOT={hub_root}
        Environment=RESALLOC_SSH_HOST=
        Environment=RESALLOC_TICK_WORKERS=4
{reserve}        EnvironmentFile=-{hub_root}/secrets.env
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
        """
    )


def extras_for_secrets(text: str) -> list[str]:
    """Install only the cloud CLIs whose keys are already on the hub."""

    names = set(secret_names(text))
    found = []
    if names & {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"}:
        found.append("modal")
    if names & {"KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN"}:
        found.append("kaggle")
    return found


def parse_extras(text: str, secret_text: str = "") -> list[str]:
    raw = text.strip()
    if not raw:
        return extras_for_secrets(secret_text)
    if raw == "all":
        return ["modal", "kaggle"]
    chosen = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in chosen:
            if name not in {"modal", "kaggle"}:
                raise ValueError(f"unknown extra {name!r}; choose modal, kaggle, or all")
            chosen.append(name)
    return chosen


def render_config(hub_root: Path, ssh_host: str | None) -> str:
    root = json.dumps(str(hub_root))
    lines = [f"hub_root = {root}"]
    if ssh_host:
        lines.append(f"ssh_host = {json.dumps(ssh_host)}")
    else:
        lines.append('# ssh_host = "hub"  # a Host from ~/.ssh/config; omit it to run the hub here')
    lines.append("# ssh_port = 22")
    lines.append('# ssh_identity = ""')
    lines.append("")
    lines.append("# Spend caps for fields a job leaves at the Limits default. An explicit job value wins.")
    lines.append("# [limits]")
    lines.append("# modal_max_spend_usd = 10")
    lines.append("# runpod_max_spend_usd = 5")
    lines.append("# vast_max_spend_usd = 5")
    lines.append("# runpod_max_hourly_usd = 1.5")
    lines.append("# vast_max_hourly_usd = 0.4")
    return "\n".join(lines) + "\n"


class SwitchHubError(ValueError):
    """The laptop config already points somewhere else."""


class NeedRemoteHome(Exception):
    """The hub directory is the remote home, and it has not been looked up yet."""


def remote_target(
    existing_host: str | None,
    existing_root: Path | None,
    ssh_host: str,
    hub_root: Path | None,
    *,
    force: bool,
    remote_home: str | None,
) -> tuple[Path, bool]:
    """Where the hub directory is, and whether the laptop config should be rewritten."""

    if hub_root is not None:
        if existing_root is not None and existing_host != ssh_host and not force:
            if not existing_host:
                raise SwitchHubError("config is local. Pass --force to point this laptop at the SSH hub.")
            raise SwitchHubError(f"config points at {existing_host}. Pass --force to switch to {ssh_host}.")
        changed = existing_root is None or existing_host != ssh_host or existing_root != hub_root
        return hub_root, changed
    if existing_root is not None and existing_host == ssh_host:
        return existing_root, False
    if existing_root is not None and not force:
        if not existing_host:
            raise SwitchHubError("config is local. Pass --force to point this laptop at the SSH hub.")
        raise SwitchHubError(f"config points at {existing_host}. Pass --force to switch to {ssh_host}.")
    if not remote_home:
        raise NeedRemoteHome()
    return Path(remote_home) / "resalloc", True


def quickstart() -> str:
    return textwrap.dedent(
        """\
        На этой машине хаб:
          pip install resalloc && resalloc up

        Ноутбук сдаёт задачи, хостинг — узел. Имя Host уже лежит в ~/.ssh/config:
          resalloc up --ssh-host ИМЯ

        Потом ключи провайдеров в secrets.env на хабе и проверка:
          resalloc doctor

        This machine is the hub:
          pip install resalloc && resalloc up

        The laptop submits jobs, another host is the hub:
          resalloc up --ssh-host NAME

        Then put provider keys in the hub's secrets.env and check:
          resalloc doctor

        resalloc help      эта страница / this page
        resalloc config    текущие настройки, без значений ключей
        resalloc --help    все команды / every command
        docs/SHORT.md      кратко по-русски
        docs/en/SHORT.md   short English
        README.ru.md       полный русский текст
        README.md          full English
        """
    )


def init_config(path: Path, hub_root: Path, ssh_host: str | None, *, force: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return f"kept {path}"
    path.write_text(render_config(hub_root, ssh_host), encoding="utf-8")
    return f"wrote {path}"


def user_systemd_works() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        completed = subprocess.run([systemctl, "--user", "show-environment"], capture_output=True)
    except OSError:
        return False
    return completed.returncode == 0


def local_checks(config: Config, *, systemd_user: bool | None = None) -> list[Check]:
    """Doctor for a hub on this machine. SSH and linger are not required."""

    root = config.hub_root
    rows = [Check("config", True, f"local hub={root}")]
    try:
        import resalloc  # noqa: F401

        rows.append(Check("package", True, "import resalloc"))
    except Exception as exc:
        rows.append(Check("package", False, str(exc)))
    usage_root = root if root.exists() else root.parent
    if not usage_root.exists():
        rows.append(Check("disk", False, f"{root} does not exist yet"))
    else:
        free = shutil.disk_usage(usage_root).free
        from resalloc.disk import configured_reserve

        reserve = configured_reserve(root=root)
        rows.append(Check("disk", free > reserve, f"free={free} reserve={reserve}"))
    secrets = root / "secrets.env"
    if secrets.is_file():
        names = secret_names(secrets.read_text(encoding="utf-8"))
        detail = ", ".join(names) or "no cloud keys; laptop and an SSH machine need none"
    else:
        detail = "no secrets file yet; laptop and an SSH machine need none"
    rows.append(Check("secrets", True, detail))
    systemd = user_systemd_works() if systemd_user is None else systemd_user
    unit = Path.home() / ".config" / "systemd" / "user" / "resalloc-hub.service"
    if not systemd:
        rows.append(Check("hub", True, "no user systemd; start it with resalloc hub run"))
    elif not unit.is_file():
        rows.append(Check("hub", True, "no unit installed; resalloc hub install  or  resalloc hub run"))
    else:
        systemctl = shutil.which("systemctl") or "systemctl"
        completed = subprocess.run([systemctl, "--user", "is-active", "resalloc-hub.service"], capture_output=True, text=True)
        state = (completed.stdout or completed.stderr or "").strip()
        rows.append(Check("hub", state == "active", state or "not installed"))
    return rows


def ensure_hub_reserve(root: Path) -> int:
    """Write reserve_bytes into hub.toml once. A later edit of that file is kept."""

    from resalloc.disk import choose_reserve, reserve_from_hub

    found = reserve_from_hub(root)
    if found is not None:
        return found
    usage = root if root.exists() else root.parent
    reserve = choose_reserve(shutil.disk_usage(usage).free)
    path = root / "hub.toml"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if "reserve_bytes" not in text:
            path.write_text(text.rstrip() + f"\nreserve_bytes = {reserve}\n", encoding="utf-8")
        return reserve
    path.write_text(hub_toml_text(reserve), encoding="utf-8")
    return reserve


def hub_toml_text(reserve: int) -> str:
    return (
        "# Caps stay off until a line is uncommented.\n"
        "# max_running = 4\n"
        "# tick_workers = 4\n"
        "# probe_failures = 5\n"
        "#\n"
        "# [providers.runpod]\n"
        "# max_running = 2\n"
        "#\n"
        "# [providers.vast]\n"
        "# max_running = 2\n"
        "#\n"
        "# POST JSON when a job starts waiting for confirm. The URL is not printed by resalloc config.\n"
        '# notify_url = "https://ntfy.sh/your-topic"\n'
        "# notify_command = \"notify-send resalloc\"\n"
        f"reserve_bytes = {reserve}\n"
    )


def describe_hub(root: Path) -> dict[str, Any]:
    """Hub settings safe to print. Secret values and notify targets stay out."""

    from resalloc.disk import configured_reserve
    from resalloc.hub import load_hub_limits, notify_settings

    per, total, workers, probes = load_hub_limits(root)
    secrets_path = root / "secrets.env"
    names = secret_names(secrets_path.read_text(encoding="utf-8")) if secrets_path.is_file() else []
    url, command = notify_settings(root)
    return {
        "hub_root": str(root),
        "reserve_bytes": configured_reserve(root=root),
        "max_running": total,
        "tick_workers": workers,
        "probe_failures": probes,
        "providers": per,
        "secrets": names,
        "notify": bool(url or command),
    }


def install_local(config: Config, *, start: bool = True) -> list[str]:
    """Create the hub directory on this machine and start it when systemd is there."""

    root = config.hub_root
    root.mkdir(parents=True, exist_ok=True)
    notes = [f"hub root {root}"]
    reserve = ensure_hub_reserve(root)
    notes.append(f"disk reserve {reserve} bytes")
    secrets = root / "secrets.env"
    if not secrets.exists():
        secrets.write_text(secrets_template(), encoding="utf-8")
        secrets.chmod(0o600)
        notes.append(f"secrets -> {secrets}")
    else:
        notes.append(f"secrets kept at {secrets}")
    if not start or not user_systemd_works():
        notes.append("start the hub with: resalloc hub run")
        return notes
    binary = f"{sys.executable} -m resalloc"
    unit = Path.home() / ".config" / "systemd" / "user" / "resalloc-hub.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(hub_unit(binary, str(root), reserve), encoding="utf-8")
    systemctl = shutil.which("systemctl") or "systemctl"
    reload = subprocess.run([systemctl, "--user", "daemon-reload"], capture_output=True, text=True)
    if reload.returncode != 0:
        notes.append((reload.stderr or "systemd reload failed").strip())
        notes.append("start the hub with: resalloc hub run")
        return notes
    subprocess.run([systemctl, "--user", "enable", "--now", "resalloc-hub.service"], capture_output=True, text=True)
    subprocess.run(["sudo", "-n", "loginctl", "enable-linger", subprocess.check_output(["id", "-un"], text=True).strip()], capture_output=True)
    notes.append(f"unit -> {unit}")
    return notes


def secrets_template() -> str:
    return textwrap.dedent(
        """\
        # Keys for the hub daemon. Values stay on this machine.
        # RUNPOD_API_KEY=
        # MODAL_TOKEN_ID=
        # MODAL_TOKEN_SECRET=
        # MODAL_WORKSPACE=
        # KAGGLE_USERNAME=
        # KAGGLE_KEY=
        # KAGGLE_API_TOKEN=
        # VAST_API_KEY=
        """
    )


def push_local_runtime(job_id: str) -> None:
    from resalloc.config import resolve as resolve_config
    from resalloc.transport import LocalTransport, make_transport

    transport = make_transport(resolve_config())
    if isinstance(transport, LocalTransport):
        return
    if os.environ.get("RESALLOC_JOB_DIR"):
        runtime = Path(os.environ["RESALLOC_JOB_DIR"])
    else:
        runtime = Path.home() / ".resalloc-work" / job_id / "runtime"
    if not runtime.exists():
        raise RuntimeError(f"no local runtime at {runtime}")
    transport.push_runtime_outputs(job_id, runtime)


def run_command(argv: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, input=input_text, text=True, capture_output=True, check=False)


def ssh_target(config: Config) -> str:
    host = config.ssh_host or ""
    if config.ssh_user:
        return f"{config.ssh_user}@{host}"
    return host


def ssh_base(config: Config) -> list[str]:
    command = ["ssh", "-o", "BatchMode=yes"]
    if config.ssh_identity:
        command += ["-i", config.ssh_identity]
    if config.ssh_port:
        command += ["-p", str(config.ssh_port)]
    command.append(ssh_target(config))
    return command


def probe_hub(config: Config) -> tuple[dict[str, Any] | None, str]:
    env = (
        f"RESALLOC_HUB_ROOT={shlex.quote(str(config.hub_root))} RESALLOC_SSH_HOST= "
        "PATH=$HOME/.local/bin:$PATH "
        "PYTHONPATH=$HOME/.local/share/resalloc${PYTHONPATH:+:$PYTHONPATH}"
    )
    completed = run_command(ssh_base(config) + [f"{env} python3 -"], input_text=_import_probe())
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return None, detail or f"ssh exited {completed.returncode}"
    try:
        return json.loads(completed.stdout), ""
    except json.JSONDecodeError:
        return None, completed.stdout.strip() or "probe did not return JSON"


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def bootstrap(config: Config, extras: str = "") -> list[str]:
    """Copy the package to the hub and start it. Modal and Kaggle are installed only with --extras."""

    if not config.ssh_host:
        raise RuntimeError("hub bootstrap needs ssh_host in the config")
    home = _remote_home(config)
    share = f"{home}/.local/share/resalloc"
    binary = f"{home}/.local/bin/resalloc"
    hub = str(config.hub_root)
    notes: list[str] = []
    mkdir = run_command(
        ssh_base(config) + [f"mkdir -p {shlex.quote(share)} {shlex.quote(home + '/.local/bin')} {shlex.quote(hub)}"]
    )
    if mkdir.returncode != 0:
        raise RuntimeError(mkdir.stderr.strip() or "mkdir on the hub failed")
    rsync = run_command(_rsync_package(config, f"{share}/resalloc"))
    if rsync.returncode != 0:
        raise RuntimeError(rsync.stderr.strip() or "rsync to the hub failed")
    notes.append(f"package -> {share}/resalloc")
    chosen: list[str] = []
    if extras.strip():
        chosen = parse_extras(extras, _remote_secret_text(config))
    installed = run_command(
        ssh_base(config) + ["sh", "-s"],
        input_text=_install_script(share, binary, hub, chosen),
    )
    if installed.returncode != 0:
        raise RuntimeError((installed.stderr or installed.stdout).strip() or "hub install failed")
    notes.append(installed.stdout.strip())
    return notes


def _remote_secret_text(config: Config) -> str:
    path = shlex.quote(str(config.hub_root) + "/secrets.env")
    completed = run_command(ssh_base(config) + [f"cat {path} 2>/dev/null || true"])
    if completed.returncode != 0:
        return ""
    return completed.stdout


def _remote_home(config: Config) -> str:
    completed = run_command(ssh_base(config) + ['printf %s "$HOME"'])
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(completed.stderr.strip() or "could not read the hub home directory")
    return completed.stdout.strip()


def _rsync_package(config: Config, remote_dir: str) -> list[str]:
    ssh_cmd = "ssh -o BatchMode=yes"
    if config.ssh_port:
        ssh_cmd += f" -p {config.ssh_port}"
    if config.ssh_identity:
        ssh_cmd += f" -i {shlex.quote(config.ssh_identity)}"
    source = str(package_dir()).rstrip("/") + "/"
    return [
        "rsync",
        "-a",
        "--delete",
        "--exclude",
        "__pycache__",
        "--exclude",
        "*.pyc",
        "-e",
        ssh_cmd,
        source,
        f"{ssh_target(config)}:{remote_dir}/",
    ]


def _install_script(share: str, binary: str, hub: str, extras: list[str] | None = None) -> str:
    unit_path = "$HOME/.config/systemd/user/resalloc-hub.service"
    quoted_hub = shlex.quote(hub)
    quoted_bin = shlex.quote(binary)
    packages = []
    for name in extras or []:
        if name == "modal":
            packages.append("'modal>=0.73'")
        elif name == "kaggle":
            packages.append("'kaggle>=1.6'")
    pip_line = ""
    if packages:
        pip_line = (
            f"python3 -m pip install -q --user {' '.join(packages)}"
            " || echo 'optional provider packages were not installed'\n"
        )
    return (
        "set -eu\n"
        "if ! command -v python3 >/dev/null 2>&1; then\n"
        "  echo 'python3 is required' >&2\n"
        "  exit 1\n"
        "fi\n"
        f"{pip_line}"
        "mkdir -p \"$HOME/.config/systemd/user\"\n"
        f"cat > {quoted_bin} << 'EOF'\n{wrapper_script(share)}EOF\n"
        f"chmod 755 {quoted_bin}\n"
        f"mkdir -p {quoted_hub}\n"
        f"if [ ! -f {shlex.quote(hub + '/secrets.env')} ]; then\n"
        f"cat > {shlex.quote(hub + '/secrets.env')} << 'EOF'\n{secrets_template()}EOF\n"
        f"chmod 600 {shlex.quote(hub + '/secrets.env')}\n"
        "fi\n"
        "if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then\n"
        f"cat > {unit_path} << 'EOF'\n{hub_unit(binary, hub)}EOF\n"
        "systemctl --user daemon-reload\n"
        "systemctl --user enable resalloc-hub.service\n"
        "systemctl --user restart resalloc-hub.service\n"
        "sudo -n loginctl enable-linger \"$(id -un)\" || true\n"
        "systemctl --user is-active resalloc-hub.service\n"
        "else\n"
        "  echo 'no user systemd; on the hub run: resalloc hub run'\n"
        "fi\n"
    )
