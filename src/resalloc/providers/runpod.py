"""Runpod REST API v2 adapter.

Pods, the GPU catalog, and billing history are the documented v2 surface.
A live credit balance is not part of that published surface. probe() uses it
when an account endpoint returns one, and otherwise measures the session
against runpod_max_spend_usd. Spend is the larger of the billing-history
delta and catalog_price times elapsed time, because hourly buckets lag.

spot=True selects the COMMUNITY cloud. The v2 create body has no separate
interruptible flag; community cloud is the cheaper pool.

GraphQL is not used. It stops serving in January 2027.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import replace
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from resalloc.bundle import iter_stage_files
from resalloc.errors import ProviderError
from resalloc.io import dump_dt, parse_dt, utcnow
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runtime_files import ack_is_ready, publish_runtime, store_checkpoint

DEFAULT_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
API_ROOT = "https://api.runpod.io"
BALANCE_PATHS = ("/v2/account", "/v2/account/billing", "/v2/billing/balance")
BALANCE_KEYS = ("clientBalance", "balance", "creditBalance")
ALIVE_STATUSES = {"PROVISIONING", "STARTING", "RUNNING"}


class RunpodHttpError(ProviderError):
    def __init__(self, status: int, path: str, detail: str) -> None:
        super().__init__(f"Runpod {status} {path}: {detail}")
        self.status = status


def gpu_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("gpus", "data", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def price_of(row: dict[str, Any], *, spot: bool) -> float | None:
    price = row.get("price") or {}
    key = "community" if spot else "secure"
    if key not in price or price[key] is None:
        return None
    return float(price[key])


def billing_total(payload: dict[str, Any]) -> float:
    totals = (payload.get("metadata") or {}).get("totals") or {}
    if "totalAmount" in totals and totals["totalAmount"] is not None:
        return float(totals["totalAmount"])
    records = payload.get("records") or []
    return sum(float(item.get("totalAmount") or 0) for item in records if isinstance(item, dict))


def balance_from_payload(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in BALANCE_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def spent_estimate(billing_delta: float, hourly: float, elapsed_hours: float) -> float:
    live = max(0.0, hourly) * max(0.0, elapsed_hours)
    return max(max(0.0, billing_delta), live)


def _ssh_not_ready(detail: str) -> bool:
    markers = (
        "Connection refused",
        "Connection timed out",
        "Connection reset",
        "No route to host",
        "Operation timed out",
        "kex_exchange_identification",
    )
    return any(marker in detail for marker in markers)


def headroom_usd(
    *,
    balance: float | None,
    min_balance: float,
    max_spend: float | None,
    spent: float,
) -> float | None:
    options: list[float] = []
    if balance is not None:
        options.append(balance - min_balance)
    if max_spend is not None:
        options.append(max_spend - spent)
    if not options:
        return None
    return min(options)


def read_public_key() -> str | None:
    """Hub key that /start.sh writes into authorized_keys when PUBLIC_KEY is set."""

    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return None


def build_create_body(settings: dict[str, Any], job_id: str) -> dict[str, Any]:
    name = f"resalloc-{job_id}"
    # v2 has no interruptible flag; spot selects COMMUNITY unless cloud is set.
    # Do not send entrypoint or cmd. runpod/pytorch starts sshd inside /start.sh
    # (only when PUBLIC_KEY is set) and that script already ends in sleep infinity.
    # Replacing it leaves port 22 with nothing listening, so rsync is refused
    # for the whole wait and the pod gets deleted.
    cloud = settings.get("cloud") or ("COMMUNITY" if settings.get("spot") else "SECURE")
    ports = list(settings.get("ports") or ["22/tcp"])
    if "22/tcp" not in ports:
        ports.append("22/tcp")
    env = {
        "RESALLOC_JOB_DIR": "/resalloc/runtime",
        "PYTHONPATH": "/resalloc/pkg",
    }
    env.update({str(key): str(value) for key, value in (settings.get("env") or {}).items()})
    body: dict[str, Any] = {
        "name": name[:60],
        "image": settings.get("image") or DEFAULT_IMAGE,
        "cloud": cloud,
        "gpu": {"id": settings["gpu"], "count": int(settings.get("gpu_count") or 1)},
        "disk": int(settings.get("disk_gb") or 20),
        "startSsh": True,
        "ports": ports,
        "env": env,
    }
    if settings.get("data_centers"):
        body["dataCenterIds"] = list(settings["data_centers"])
    mounts: dict[str, Any] = {}
    if settings.get("volume_gb"):
        mounts["persistent"] = {"size": int(settings["volume_gb"]), "path": "/data"}
    if settings.get("network_volume_id"):
        mounts["network"] = [{"volumeId": settings["network_volume_id"], "path": "/data"}]
    if mounts:
        body["mounts"] = mounts
    return body


def _usable_ssh(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and value.get("host"):
        return value
    return None


def ssh_endpoint(pod: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Prefer direct TCP. Runpod's proxy at ssh.runpod.io is shell-only: no rsync or SCP.

    Docs: https://docs.runpod.io/pods/configuration/use-ssh
    """

    block = pod.get("ssh") or {}
    direct = _usable_ssh(block.get("direct"))
    if direct:
        return direct, "direct"
    proxy = _usable_ssh(block.get("proxy"))
    if proxy:
        return proxy, "proxy"
    raise ProviderError(f"pod {pod.get('id')} has no SSH endpoint yet")


def training_command(
    script_name: str,
    args: list[str],
    *,
    wait_data: bool = False,
    datasets: list[str] | None = None,
    data_timeout: float = 3600,
    root: str = "/resalloc",
    python: str = "python",
) -> str:
    """Start the script in the background and record its exit code.

    Runpod's /start.sh ends in sleep infinity, so a finished script would
    otherwise leave the pod RUNNING and billing. A direct upload waits for
    each dataset's COMPLETE, then gives up so a missing upload cannot hold
    the GPU forever.
    """

    names = [name for name in (datasets or []) if name]
    runtime = f"{root}/runtime"
    data_root = f"{root}/datasets"
    py = shlex.quote(python)
    command = " ".join(shlex.quote(part) for part in [python, "-u", script_name, *args])
    timeout = max(1, int(data_timeout))
    if names:
        markers = " ".join(shlex.quote(f"{data_root}/{name}/COMPLETE") for name in names)
        wait = (
            f"_left={timeout}; "
            "while true; do _ok=1; "
            f"for _m in {markers}; do [ -f \"$_m\" ] || _ok=0; done; "
            "[ \"$_ok\" -eq 1 ] && break; "
            "[ \"$_left\" -le 0 ] && printf '{\"returncode\": 1}\\n' > "
            f"{shlex.quote(runtime + '/finished.json')} && exit 1; "
            "sleep 2; _left=$((_left - 2)); done; "
        )
    elif wait_data:
        wait = (
            f"_left={timeout}; "
            f"while ! find {shlex.quote(data_root)} -name COMPLETE | grep -q .; do "
            "[ \"$_left\" -le 0 ] && printf '{\"returncode\": 1}\\n' > "
            f"{shlex.quote(runtime + '/finished.json')} && exit 1; "
            "sleep 2; _left=$((_left - 2)); done; "
        )
    else:
        wait = ""
    inner = (
        "req=; if [ -f requirements.txt ]; then req=requirements.txt; "
        "elif [ -f requirements-run.txt ]; then req=requirements-run.txt; fi; "
        f"if [ -n \"$req\" ]; then {py} -m pip install --break-system-packages -q -r \"$req\" "
        f"|| {{ printf '{{\"returncode\": 1}}\\n' > {shlex.quote(runtime + '/finished.json')}; exit 1; }}; fi; "
        f"{wait}{command}; rc=$?; "
        f"printf '{{\"returncode\": %s}}\\n' \"$rc\" > {shlex.quote(runtime + '/finished.json')}; "
        "exit $rc"
    )
    return (
        f"mkdir -p {shlex.quote(runtime)} {shlex.quote(data_root)} && cd {shlex.quote(root)} && "
        f"export PYTHONPATH={shlex.quote(root + '/pkg')} "
        f"RESALLOC_JOB_DIR={shlex.quote(runtime)} RESALLOC_DATA_DIR={shlex.quote(data_root)}; "
        f"nohup bash -c {shlex.quote(inner)} >> {shlex.quote(runtime + '/worker.log')} 2>&1 & "
        f"echo $! > {shlex.quote(runtime + '/worker.pid')}"
    )


def ssh_base(endpoint: dict[str, Any]) -> list[str]:
    """ssh argv up to the host. Identity comes from the endpoint or the environment."""

    argv = [
        "ssh",
        "-p",
        str(endpoint.get("port") or 22),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    identity = endpoint.get("identity") or os.environ.get("RESALLOC_SSH_IDENTITY")
    if identity:
        argv.extend(["-i", str(identity)])
    user = str(endpoint.get("username") or "root")
    argv.append(f"{user}@{endpoint['host']}")
    return argv


def shell_download(endpoint: dict[str, Any], remote_dir: str, local_dir: Path) -> None:
    """Fetch a directory through a shell tar pipe. The Runpod proxy refuses rsync."""

    import io
    import tarfile

    local_dir.mkdir(parents=True, exist_ok=True)
    remote = f"tar -C {shlex.quote(remote_dir)} -cf - ."
    process = subprocess.Popen(
        [*ssh_base(endpoint), remote],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ProviderError(detail or f"ssh tar download exited {process.returncode}")
    with tarfile.open(fileobj=io.BytesIO(stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                continue
            try:
                archive.extract(member, local_dir, filter="data")
            except TypeError:
                archive.extract(member, local_dir)


def shell_upload(endpoint: dict[str, Any], files: list[tuple[Path, str]]) -> None:
    """Stream a tar over SSH. The proxy refuses rsync, and buffering the whole archive would fill RAM."""

    import tarfile
    import threading

    process = subprocess.Popen(
        [*ssh_base(endpoint), "mkdir -p /resalloc && tar -C /resalloc -xf -"],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stderr is None:
        raise ProviderError("ssh tar upload did not open pipes")

    def write_archive() -> None:
        assert process.stdin is not None
        with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
            for source, relative in files:
                archive.add(source, arcname=relative)
        process.stdin.close()

    writer = threading.Thread(target=write_archive)
    writer.start()
    stderr = process.stderr.read()
    writer.join()
    code = process.wait()
    if code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ProviderError(detail or f"ssh tar upload exited {code}")


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


class RunpodApi:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = API_ROOT,
        transport: Callable[..., tuple[int, Any]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport or urllib_transport
        self.sleep = time.sleep
        self._balance_absent = False
        self._catalog: Any = None
        self._catalog_at = 0.0
        self._lock = __import__("threading").Lock()

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> Any:
        if not self.api_key:
            raise ProviderError("RUNPOD_API_KEY is not set")
        url = self.base_url + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        status, parsed = self.transport(method, url, payload, headers)
        if method == "GET" and status in {429, 500, 502, 503, 504}:
            for attempt in range(2):
                self.sleep(min(2 ** (attempt + 1), 8))
                status, parsed = self.transport(method, url, payload, headers)
                if status not in {429, 500, 502, 503, 504}:
                    break
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else str(parsed)
            raise RunpodHttpError(status, path, str(detail))
        return parsed

    def create_pod(self, body: dict[str, Any]) -> dict[str, Any]:
        created = self.request("POST", "/v2/pods", body)
        if not isinstance(created, dict) or not created.get("id"):
            raise ProviderError("Runpod create pod returned no id")
        return created

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        pod = self.request("GET", f"/v2/pods/{pod_id}")
        if not isinstance(pod, dict):
            raise ProviderError("Runpod get pod returned an unexpected body")
        return pod

    def terminate(self, pod_id: str) -> None:
        # Terminate, not just stop: a stopped pod can keep billing for disk,
        # and the checkpoint already lives on the hub.
        self.request("DELETE", f"/v2/pods/{pod_id}")

    def billing_total(self) -> float:
        payload = self.request("GET", "/v2/billing", query={"bucketSize": "hour", "lastN": "48"})
        if not isinstance(payload, dict):
            return 0.0
        return billing_total(payload)

    def try_balance(self) -> float | None:
        with self._lock:
            if self._balance_absent:
                return None
        for path in BALANCE_PATHS:
            try:
                payload = self.request("GET", path)
            except RunpodHttpError as exc:
                if exc.status in {404, 405, 400}:
                    continue
                raise
            found = balance_from_payload(payload)
            if found is not None:
                return found
        with self._lock:
            self._balance_absent = True
        return None

    def gpu_price(self, gpu_id: str, *, spot: bool) -> float | None:
        with self._lock:
            fresh = self._catalog is not None and time.monotonic() - self._catalog_at < 600
            payload = self._catalog if fresh else None
        if payload is None:
            payload = self.request("GET", "/v2/catalog/gpus")
            with self._lock:
                self._catalog = payload
                self._catalog_at = time.monotonic()
        for row in gpu_rows(payload):
            if row.get("id") == gpu_id:
                return price_of(row, spot=spot)
        return None


def default_ssh(endpoint: dict[str, Any], command: str) -> str:
    argv = [*ssh_base(endpoint), command]
    import subprocess

    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    text = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    if completed.returncode != 0 or "doesn't support PTY" in text:
        raise ProviderError(text.strip() or f"ssh exited {completed.returncode}")
    return completed.stdout


def default_sync(endpoint: dict[str, Any], files: list[tuple[Path, str]], remote_root: str = "/resalloc") -> None:
    import subprocess
    import tempfile

    host = str(endpoint["host"])
    user = str(endpoint.get("username") or "root")
    ssh_cmd = _ssh_cmd(endpoint)
    remote_spec = f"{user}@{host}:{shlex.quote(remote_root.rstrip('/') + '/')}"
    stage_on = files[0][0].parent if files else None
    with tempfile.TemporaryDirectory(prefix="resalloc-stage-", dir=stage_on) as temporary:
        stage = Path(temporary)
        for source, relative in files:
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os_link(source, target)
            except OSError:
                import shutil

                shutil.copy2(source, target)
        completed = subprocess.run(
            ["rsync", "-a", "-e", ssh_cmd, f"{stage}/", remote_spec],
            check=False,
            capture_output=True,
            text=True,
        )
    if completed.returncode != 0:
        raise ProviderError(completed.stderr.strip() or f"rsync exited {completed.returncode}")


def _ssh_cmd(endpoint: dict[str, Any]) -> str:
    port = str(endpoint.get("port") or 22)
    identity = endpoint.get("identity") or os.environ.get("RESALLOC_SSH_IDENTITY")
    command = f"ssh -p {port} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    if identity:
        command += f" -i {shlex.quote(str(identity))}"
    return command


def _rsync_file(endpoint: dict[str, Any], remote_path: str, local_path: Path) -> None:
    import subprocess

    local_path.parent.mkdir(parents=True, exist_ok=True)
    user = str(endpoint.get("username") or "root")
    host = str(endpoint["host"])
    completed = subprocess.run(
        ["rsync", "-a", "-e", _ssh_cmd(endpoint), f"{user}@{host}:{remote_path}", str(local_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ProviderError(completed.stderr.strip() or f"rsync exited {completed.returncode}")


def default_pull(endpoint: dict[str, Any], remote_dir: str, local_dir: Path) -> None:
    import subprocess

    host = str(endpoint["host"])
    user = str(endpoint.get("username") or "root")
    ssh_cmd = _ssh_cmd(endpoint)
    local_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["rsync", "-a", "-e", ssh_cmd, f"{user}@{host}:{remote_dir}/", f"{local_dir}/"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ProviderError(completed.stderr.strip() or f"rsync exited {completed.returncode}")


def os_link(source: Path, target: Path) -> None:
    import os

    os.link(source, target)


class RunpodAdapter:
    name = "runpod"

    def __init__(
        self,
        api: RunpodApi | None = None,
        ssh_runner: Callable[[dict[str, Any], str], str] | None = None,
        file_sync: Callable[[dict[str, Any], list[tuple[Path, str]]], None] | None = None,
        shell_upload_fn: Callable[[dict[str, Any], list[tuple[Path, str]]], None] | None = None,
        shell_download_fn: Callable[[dict[str, Any], str, Path], None] | None = None,
        file_pull: Callable[[dict[str, Any], str, Path], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        ready_timeout: float = 600,
    ) -> None:
        self.api = api or RunpodApi(os.environ.get("RUNPOD_API_KEY"))
        self.ssh_runner = ssh_runner or default_ssh
        self.file_sync = file_sync or default_sync
        self.shell_upload = shell_upload_fn or shell_upload
        self.shell_download = shell_download_fn or shell_download
        self.file_pull = file_pull or default_pull
        self.sleep = sleep or time.sleep
        self.clock = clock or utcnow
        self.ready_timeout = ready_timeout

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        settings = dict(launch.settings)
        settings["min_balance"] = launch.limits.runpod_min_balance_usd
        settings["max_spend"] = launch.limits.runpod_max_spend_usd
        return self._snapshot(settings, handle=None, pod=None)

    def start(self, launch: JobLaunch) -> RunningJob:
        settings = dict(launch.settings)
        settings["script_name"] = launch.script.name
        settings["args"] = list(launch.args)
        env = dict(settings.get("env") or {})
        if not env.get("PUBLIC_KEY"):
            key = read_public_key()
            if key:
                env["PUBLIC_KEY"] = key
                settings["env"] = env
        created = self.api.create_pod(build_create_body(settings, launch.job_id))
        pod_id = str(created["id"])
        try:
            pod, endpoint, ssh_kind = self._wait_direct_ssh(pod_id)
            # The API publishes ssh.direct as soon as the pod is RUNNING.
            # sshd itself comes up only after /start.sh finishes, a few seconds later.
            self._wait_ssh_accepts(endpoint)
            files = iter_stage_files(launch.job_dir)
            self._sync_when_ready(endpoint, files)
            self.ssh_runner(
                endpoint,
                training_command(
                    launch.script.name,
                    launch.args,
                    wait_data=bool(launch.settings.get("_wait_data")),
                    datasets=[str(name) for name in (launch.settings.get("_wait_datasets") or [])],
                    data_timeout=float(launch.settings.get("_data_timeout") or 3600),
                ),
            )
        except Exception:
            try:
                self.api.terminate(pod_id)
            except Exception:
                pass
            raise
        baseline = self.api.billing_total()
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=str(pod["id"]),
            connection={
                "ssh": endpoint,
                "ssh_kind": ssh_kind,
                "billing_baseline": baseline,
                "started_at": dump_dt(self.clock()),
                "spot": bool(settings.get("spot")),
                "gpu": settings.get("gpu"),
                "min_balance": launch.limits.runpod_min_balance_usd,
                "max_spend": launch.limits.runpod_max_spend_usd,
                "runtime_dir": str(launch.runtime_dir),
                "script_name": launch.script.name,
                "args": list(launch.args),
                "data_endpoint": {
                    "provider": "runpod",
                    "ssh": endpoint,
                    "ssh_kind": ssh_kind,
                    "remote_dir": "/resalloc/datasets",
                },
            },
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        try:
            pod = self.api.get_pod(handle.remote_id)
        except RunpodHttpError as exc:
            if exc.status == 404:
                return AccountSnapshot(
                    provider=self.name,
                    alive=False,
                    burn_usd_per_hour=0.0,
                    detail="workload_stopped",
                )
            raise
        snapshot = self._snapshot(handle.connection, handle=handle, pod=pod)
        finished = self._read_finished(handle)
        if finished is None:
            return snapshot
        code = int(finished.get("returncode", 1))
        return replace(snapshot, alive=False, detail="completed" if code == 0 else "worker_failed")

    def signal(self, handle: RunningJob, signal: Signal, reason: str, lease_soft: datetime, lease_hard: datetime) -> None:
        runtime = Path(str(handle.connection["runtime_dir"]))
        publish_runtime(runtime, signal, reason, lease_soft, lease_hard)
        endpoint = handle.connection.get("ssh")
        if not endpoint:
            return
        remote_runtime = "/resalloc/runtime"
        files = [
            (runtime / "control.json", "runtime/control.json"),
            (runtime / "lease.json", "runtime/lease.json"),
        ]
        self._send_files(handle, endpoint, files)

    def ack_ready(self, handle: RunningJob) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        endpoint = handle.connection.get("ssh")
        if endpoint:
            try:
                self._fetch_runtime(handle, endpoint, runtime)
            except ProviderError:
                return False
        return ack_is_ready(runtime)

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        runtime = Path(str(handle.connection["runtime_dir"]))
        endpoint = handle.connection.get("ssh")
        if endpoint:
            self._fetch_runtime(handle, endpoint, runtime)
        return store_checkpoint(runtime, checkpoint_dir)

    def pull_reports(self, handle: RunningJob) -> None:
        """Copy the live log and previews. Checkpoints stay on the pod until the job stops."""

        endpoint = handle.connection.get("ssh")
        if not isinstance(endpoint, dict) or not endpoint.get("host"):
            return
        if handle.connection.get("ssh_kind") == "proxy":
            return
        runtime = Path(str(handle.connection["runtime_dir"]))
        artifacts = runtime / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        # A missing preview directory used to abort the log copy on every tick.
        missing = ("no such file", "link_stat", "vanished", "not a directory", "failed to stat")
        for action in (
            lambda: self.file_pull(endpoint, "/resalloc/runtime/artifacts/previews", artifacts / "previews"),
            lambda: _rsync_file(endpoint, "/resalloc/runtime/artifacts/log.csv", artifacts / "log.csv"),
        ):
            try:
                action()
            except ProviderError as exc:
                if any(bit in str(exc).lower() for bit in missing):
                    continue
                raise

    def _send_files(self, handle: RunningJob, endpoint: dict[str, Any], files: list[tuple[Path, str]]) -> None:
        if handle.connection.get("ssh_kind") == "proxy":
            self.shell_upload(endpoint, files)
            return
        self.file_sync(endpoint, files)

    def _fetch_runtime(self, handle: RunningJob, endpoint: dict[str, Any], runtime: Path) -> None:
        if handle.connection.get("ssh_kind") == "proxy":
            self.shell_download(endpoint, "/resalloc/runtime", runtime)
            return
        self.file_pull(endpoint, "/resalloc/runtime", runtime)

    def stop(self, handle: RunningJob) -> None:
        self.api.terminate(handle.remote_id)

    def _read_finished(self, handle: RunningJob) -> dict[str, Any] | None:
        runtime = Path(str(handle.connection.get("runtime_dir") or ""))
        local = runtime / "finished.json"
        if local.exists():
            return json.loads(local.read_text(encoding="utf-8"))
        endpoint = handle.connection.get("ssh")
        if not isinstance(endpoint, dict) or not endpoint.get("host"):
            return None
        try:
            text = self.ssh_runner(endpoint, "cat /resalloc/runtime/finished.json")
        except (ProviderError, OSError, subprocess.SubprocessError):
            return None
        text = (text or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _wait_ssh_accepts(self, endpoint: dict[str, Any]) -> None:
        """Retry until sshd answers. Connection refused here means /start.sh has not opened port 22 yet."""

        deadline = time.monotonic() + 90
        last = "ssh endpoint never accepted a command"
        while True:
            try:
                self.ssh_runner(endpoint, "echo ok")
                return
            except (ProviderError, OSError, subprocess.SubprocessError) as exc:
                last = str(exc)
                if not _ssh_not_ready(last):
                    raise ProviderError(last)
                if time.monotonic() >= deadline:
                    raise ProviderError(last)
                self.sleep(5)

    def _sync_when_ready(self, endpoint: dict[str, Any], files: list[tuple[Path, str]]) -> None:
        deadline = time.monotonic() + 120
        last = "ssh endpoint never accepted a connection"
        while True:
            try:
                self.file_sync(endpoint, files)
                return
            except ProviderError as exc:
                last = str(exc)
                if "Connection refused" not in last and "Connection timed out" not in last:
                    raise
                if time.monotonic() >= deadline:
                    raise ProviderError(last)
                self.sleep(5)

    def _wait_direct_ssh(self, pod_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        """Wait until the pod exposes direct TCP SSH. The proxy accepts a command and then does not run it."""

        deadline = time.monotonic() + 120
        last = "pod has no direct SSH endpoint"
        while True:
            pod = self.api.get_pod(pod_id)
            status = str(pod.get("status") or "")
            if status in {"EXITED", "ERROR", "TERMINATED"}:
                raise ProviderError(f"pod {pod_id} entered {status} before SSH was usable")
            try:
                endpoint, kind = ssh_endpoint(pod)
            except ProviderError as exc:
                last = str(exc)
                endpoint, kind = None, ""
            if kind == "direct" and endpoint is not None:
                return pod, endpoint, kind
            if time.monotonic() >= deadline:
                raise ProviderError(last)
            self.sleep(3)

    def _wait_until_running(self, pod_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.ready_timeout
        last = ""
        while True:
            pod = self.api.get_pod(pod_id)
            status = str(pod.get("status") or "")
            last = status
            if status == "RUNNING":
                return pod
            if status in {"EXITED", "ERROR", "TERMINATED"}:
                raise ProviderError(f"pod {pod_id} entered {status} before it was usable")
            if time.monotonic() >= deadline:
                raise ProviderError(f"pod {pod_id} stayed in {last or 'unknown'} until the ready timeout")
            self.sleep(5)

    def _snapshot(
        self,
        settings: dict[str, Any],
        *,
        handle: RunningJob | None,
        pod: dict[str, Any] | None,
    ) -> AccountSnapshot:
        spot = bool(settings.get("spot"))
        gpu = str(settings.get("gpu") or "")
        hourly = None
        alive = True
        detail = ""
        if pod is not None:
            status = str(pod.get("status") or "")
            alive = status in ALIVE_STATUSES
            if not alive:
                # The script's own exit is read from finished.json and overrides this.
                # An ERROR pod with no script result is an infrastructure failure.
                detail = "workload_stopped"
            if pod.get("cost") is not None:
                hourly = float(pod["cost"])
        if hourly is None and gpu:
            hourly = self.api.gpu_price(gpu, spot=spot)
        balance = self.api.try_balance()
        total = self.api.billing_total()
        baseline = float(handle.connection.get("billing_baseline", total)) if handle else total
        started = self.clock()
        if handle and handle.connection.get("started_at"):
            started = parse_dt(str(handle.connection["started_at"]))
        elapsed_hours = max(0.0, (self.clock() - started).total_seconds() / 3600)
        spent = 0.0 if handle is None else spent_estimate(total - baseline, hourly or 0.0, elapsed_hours)
        min_balance = float(settings.get("min_balance", 0.0))
        max_spend = settings.get("max_spend")
        if handle is not None:
            min_balance = float(handle.connection.get("min_balance", min_balance))
            max_spend = handle.connection.get("max_spend", max_spend)
        room = headroom_usd(
            balance=balance,
            min_balance=min_balance,
            max_spend=None if max_spend is None else float(max_spend),
            spent=spent,
        )
        burn = hourly if handle is not None and alive else 0.0
        if room is None:
            detail = detail or "runpod balance is not in the REST v2 API; set runpod_max_spend_usd to cap the session"
        return AccountSnapshot(
            provider=self.name,
            alive=alive,
            headroom_usd=room,
            burn_usd_per_hour=burn,
            hourly_price_usd=hourly,
            detail=detail,
        )


def _factory(root: Path) -> RunpodAdapter:
    del root
    return RunpodAdapter()
