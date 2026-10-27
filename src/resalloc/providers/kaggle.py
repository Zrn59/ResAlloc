"""Kaggle adapter.

Quota, kernel push, status, and output are the official CLI. There is no
channel that can drop a stop file into a kernel that is already running, so
the lease is baked in at push time from the remaining accelerator hours.
The worker's own pulse() exits when that lease is inside the buffer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from csv import DictReader
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from resalloc.bundle import materialize
from resalloc.errors import ProviderError
from resalloc.io import dump_dt, utcnow
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.base import JobLaunch, RunningJob
from resalloc.providers.runtime_files import publish_runtime, store_checkpoint


def parse_quota_csv(text: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    reader = DictReader(StringIO(text.strip()))
    for row in reader:
        if not row:
            continue
        resource = str(row.get("resource") or row.get("Resource") or "").strip().upper()
        if not resource:
            continue
        remaining = _hours(row.get("remaining") or row.get("Remaining") or "0")
        rows[resource] = {
            "remaining": remaining,
            "used": _hours(row.get("used") or row.get("Used") or "0"),
            "total": _hours(row.get("total") or row.get("Total") or "0"),
            "refresh_at": row.get("refreshAt") or row.get("refresh_at") or "",
        }
    return rows


def _hours(value: str) -> float:
    """Kaggle prints both `8.5` and durations such as `0s` or `1h30m`."""

    text = str(value).strip().lower().replace(" ", "")
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if re.fullmatch(r"\d+(?:\.\d+)?h", text):
        return float(text[:-1])
    matches = re.findall(r"(\d+(?:\.\d+)?)([hms])", text)
    if matches and "".join(f"{number}{unit}" for number, unit in matches) == text:
        total = 0.0
        for number, unit in matches:
            amount = float(number)
            if unit == "h":
                total += amount
            elif unit == "m":
                total += amount / 60.0
            else:
                total += amount / 3600.0
        return total
    return float(text)


def accelerator_kind(accelerator: str) -> str:
    return "TPU" if accelerator.lower().startswith("tpu") else "GPU"


def session_hour_cap(accelerator: str) -> float:
    """One Kaggle session, shorter than the platform kill so the lease fires first.

    Docs: 12 hours for CPU and GPU, 9 hours for TPU.
    https://www.kaggle.com/docs/notebooks
    """

    if accelerator.lower().startswith("tpu"):
        return 8.0
    return 11.0


def quota_hours_left(parsed: dict[str, dict[str, Any]], accelerator: str, minimum: float) -> float:
    kind = accelerator_kind(accelerator)
    row = parsed.get(kind)
    if row is None:
        raise ProviderError(f"Kaggle quota response has no {kind} row")
    remaining = float(row["remaining"]) - minimum
    return min(remaining, session_hour_cap(accelerator))


def interpret_status(text: str) -> str:
    upper = text.upper()
    # "INCOMPLETE" contains "COMPLETE". Strip it before looking for a real completion.
    folded = upper.replace("INCOMPLETE", "")
    if "COMPLETE" in folded or "SUCCESS" in folded:
        return "completed"
    if "ERROR" in upper or "FAILED" in upper or "CANCEL" in upper:
        return "failed"
    if any(word in upper for word in ("RUNNING", "QUEUED", "PENDING", "STARTING")):
        return "running"
    return "unknown"


def slugify(value: str, limit: int = 50) -> str:
    cleaned = []
    for char in value.lower():
        cleaned.append(char if char.isalnum() else "-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "job")[:limit].strip("-")


def kernel_metadata(
    *,
    username: str,
    slug: str,
    title: str,
    accelerator: str,
    internet: bool,
    dataset_sources: list[str],
    private: bool = True,
    kernel_type: str = "script",
) -> dict[str, Any]:
    return {
        "id": f"{username}/{slug}",
        "title": title,
        "code_file": "resalloc_launch.py",
        "language": "python",
        "kernel_type": kernel_type,
        "is_private": private,
        "enable_gpu": accelerator_kind(accelerator) == "GPU",
        "enable_tpu": accelerator_kind(accelerator) == "TPU",
        "enable_internet": internet,
        "machine_shape": accelerator,
        "dataset_sources": dataset_sources,
    }


def render_launcher(script_name: str, args: list[str]) -> str:
    # Not an f-string: the script contains its own braces.
    text = textwrap.dedent(
        """\
        import os
        import runpy
        import subprocess
        import sys
        import tarfile
        from pathlib import Path

        def extract_tars(inputs: Path) -> None:
            if not inputs.is_dir():
                return
            for archive in list(inputs.rglob("*.tar")):
                dest = archive.parent / archive.stem
                dest.mkdir(parents=True, exist_ok=True)
                with tarfile.open(archive, "r:") as handle:
                    try:
                        handle.extractall(dest, filter="data")
                    except TypeError:
                        handle.extractall(dest)

        def find_root(name: str) -> Path:
            # A script kernel runs as /kaggle/src/script.py. The code dataset is
            # mounted under /kaggle/input, and directories arrive as tar files.
            here = Path(__file__).resolve().parent
            inputs = Path("/kaggle/input")
            extract_tars(inputs)
            if (here / name).is_file():
                return here
            if inputs.is_dir():
                matches = [path for path in inputs.rglob(name) if path.is_file()]
                if matches:
                    return matches[0].parent
            return here

        root = find_root(SCRIPT_NAME)
        sys.path.insert(0, str(root / "pkg"))
        work = Path("/kaggle/working") if Path("/kaggle/working").is_dir() else root
        runtime = work / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        os.environ["RESALLOC_JOB_DIR"] = str(runtime)
        os.environ["RESALLOC_DATA_DIR"] = "/kaggle/input"
        os.chdir(work)
        for name in ("control.json", "lease.json"):
            source = root / "runtime" / name
            target = runtime / name
            if source.is_file() and not target.exists():
                target.write_bytes(source.read_bytes())
        requirements = root / "requirements.txt"
        if requirements.exists():
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)])
        inputs = Path("/kaggle/input")
        if inputs.exists():
            for state in inputs.rglob("state.json"):
                if state.parent == root or (root in state.parents and state.parent.name == "runtime"):
                    continue
                target = runtime / "state.json"
                if not target.exists():
                    target.write_bytes(state.read_bytes())
                artifacts = state.parent / "artifacts"
                if artifacts.is_dir() and not (runtime / "artifacts").exists():
                    import shutil
                    shutil.copytree(artifacts, runtime / "artifacts")
        script = root / SCRIPT_NAME
        sys.argv = [SCRIPT_NAME, *SCRIPT_ARGS]
        runpy.run_path(str(script), run_name="__main__")
        """
    )
    return text.replace("SCRIPT_NAME", repr(script_name)).replace("SCRIPT_ARGS", repr(list(args)))


def dataset_status_pending(text: str) -> bool:
    """A dataset that was just uploaded is not visible to the status call yet."""

    lowered = text.lower()
    return any(
        bit in lowered
        for bit in ("403", "forbidden", "404", "not found", "pending", "processing", "creating")
    )


def failure_from_log(text: str) -> str:
    """The last exception line from a Kaggle kernel log."""

    stripped = text.strip()
    items: list[Any] = []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        items = parsed
    lines = []
    for item in items:
        if isinstance(item, dict):
            lines.append(str(item.get("data") or "").strip())
    for line in reversed(lines):
        if "Error" in line and not line.startswith("Traceback") and not line.startswith("File "):
            return line
    return ""


def dataset_metadata(username: str, slug: str, title: str) -> dict[str, Any]:
    return {
        "title": title,
        "id": f"{username}/{slug}",
        "licenses": [{"name": "CC0-1.0"}],
    }


def cli_command(argv: list[str], executable: str | None = None) -> list[str]:
    """Use the kaggle binary next to this interpreter. systemd does not put the venv on PATH.

    The venv python is a symlink to /usr/bin. Resolving it would look for kaggle there.
    """

    if not argv or Path(argv[0]).name != "kaggle":
        return list(argv)
    sibling = Path(executable or sys.executable).parent / "kaggle"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling), *argv[1:]]
    return list(argv)


def default_cli(argv: list[str]) -> str:
    command = cli_command(argv)
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ProviderError(f"{command[0]} was not found") from exc
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = (stderr or completed.stdout or "").strip()
        raise ProviderError(detail or f"{command[0]} exited {completed.returncode}")
    # kernels push exits 0 even when it drops a dataset that is not ready yet.
    if "not valid dataset sources" in stderr.lower():
        raise ProviderError(stderr)
    return completed.stdout


def kaggle_username() -> str:
    from_env = os.environ.get("KAGGLE_USERNAME")
    if from_env:
        return from_env
    config = Path.home() / ".kaggle" / "kaggle.json"
    if config.exists():
        data = json.loads(config.read_text(encoding="utf-8"))
        if data.get("username"):
            return str(data["username"])
    raise ProviderError("set KAGGLE_USERNAME or username in ~/.kaggle/kaggle.json")


class KaggleAdapter:
    name = "kaggle"

    def __init__(
        self,
        runner: Callable[[list[str]], str] | None = None,
        username: str | None = None,
    ) -> None:
        self.runner = runner or default_cli
        self.username = username

    def preflight(self, launch: JobLaunch) -> AccountSnapshot:
        return self._quota_snapshot(str(launch.settings.get("accelerator") or "NvidiaTeslaT4"), launch.limits.kaggle_min_gpu_hours, alive=True, detail="")

    def start(self, launch: JobLaunch) -> RunningJob:
        username = self.username or kaggle_username()
        slug = slugify(f"resalloc-{launch.job_id}")
        title = str(launch.settings.get("title") or f"resalloc {launch.job_id}")
        accelerator = str(launch.settings.get("accelerator") or "NvidiaTeslaT4")
        sources = self._publish_inputs(launch, username)
        if launch.settings.get("dataset_slug"):
            sources.append(str(launch.settings["dataset_slug"]))
        stage_parent = launch.job_dir / "kaggle-stage"
        materialize(launch.job_dir, stage_parent, include_datasets=False)
        code_slug = slugify(f"resalloc-code-{launch.job_id}")
        self._ensure_dataset(
            stage_parent,
            launch.job_dir / "kaggle-datasets" / code_slug,
            username,
            code_slug,
            f"resalloc code {launch.job_id}",
        )
        sources.append(f"{username}/{code_slug}")
        (stage_parent / "resalloc_launch.py").write_text(
            render_launcher(launch.script.name, launch.args),
            encoding="utf-8",
        )
        meta = kernel_metadata(
            username=username,
            slug=slug,
            title=title,
            accelerator=accelerator,
            internet=bool(launch.settings.get("internet", True)),
            dataset_sources=sources,
            private=bool(launch.settings.get("private", True)),
            kernel_type=str(launch.settings.get("kernel_type") or "script"),
        )
        (stage_parent / "kernel-metadata.json").write_text(
            json.dumps(meta, indent=2) + "\n",
            encoding="utf-8",
        )
        self.runner(["kaggle", "kernels", "push", "-p", str(stage_parent)])
        return RunningJob(
            provider=self.name,
            job_id=launch.job_id,
            remote_id=f"{username}/{slug}",
            connection={
                "runtime_dir": str(launch.runtime_dir),
                "stage": str(stage_parent),
                "accelerator": accelerator,
                "minimum_hours": launch.limits.kaggle_min_gpu_hours,
                "output_dir": str(launch.job_dir / "kaggle-output"),
                "data_endpoint": {
                    "provider": "kaggle",
                    "slug": launch.settings.get("dataset_slug") or f"{username}/{slugify(launch.job_id)}",
                    "remote_dir": "/kaggle/input",
                },
            },
        )

    def probe(self, handle: RunningJob) -> AccountSnapshot:
        text = self.runner(["kaggle", "kernels", "status", handle.remote_id])
        kind = interpret_status(text)
        alive = kind in {"running", "unknown"}
        detail = ""
        if kind == "completed":
            detail = "completed"
        elif kind == "failed":
            detail = "worker_failed"
        elif kind == "unknown":
            detail = "kaggle status unrecognized"
        return self._quota_snapshot(
            str(handle.connection.get("accelerator") or "NvidiaTeslaT4"),
            float(handle.connection.get("minimum_hours") or 0),
            alive=alive,
            detail=detail,
        )

    def signal(self, handle: RunningJob, signal: Signal, reason: str, lease_soft: datetime, lease_hard: datetime) -> None:
        # The running kernel cannot see files written after the push.
        # The lease published at start is what the worker honors.
        runtime = Path(str(handle.connection["runtime_dir"]))
        publish_runtime(runtime, signal, reason, lease_soft, lease_hard)

    def ack_ready(self, handle: RunningJob) -> bool:
        # Output appears when the kernel finishes. A live ack is not available.
        text = self.runner(["kaggle", "kernels", "status", handle.remote_id])
        return interpret_status(text) in {"completed", "failed"}

    def pull_checkpoint(self, handle: RunningJob, checkpoint_dir: Path) -> bool:
        output = Path(str(handle.connection["output_dir"]))
        if output.exists():
            import shutil

            shutil.rmtree(output)
        output.mkdir(parents=True, exist_ok=True)
        self.runner(["kaggle", "kernels", "output", handle.remote_id, "-p", str(output)])
        runtime = output / "runtime"
        if not (runtime / "state.json").exists() and not (output / "state.json").exists():
            message = _failure_from_output(output)
            if message:
                from resalloc.io import write_json

                runtime.mkdir(parents=True, exist_ok=True)
                write_json(runtime / "state.json", {"ok": False, "error": message})
        if not runtime.exists():
            runtime = output
        return store_checkpoint(runtime, checkpoint_dir)

    def stop(self, handle: RunningJob) -> None:
        # Kaggle has no official "stop this kernel but let it flush" call.
        # The baked-in lease is what makes the script exit before the quota does.
        del handle

    def _quota_snapshot(self, accelerator: str, minimum: float, *, alive: bool, detail: str) -> AccountSnapshot:
        text = self.runner(["kaggle", "quota", "--csv"])
        parsed = parse_quota_csv(text)
        left = quota_hours_left(parsed, accelerator, minimum)
        return AccountSnapshot(
            provider=self.name,
            alive=alive,
            quota_hours_left=left,
            detail=detail,
        )

    def _publish_inputs(self, launch: JobLaunch, username: str) -> list[str]:
        sources: list[str] = []
        staging = launch.job_dir / "kaggle-datasets"
        for dataset in launch.datasets:
            digest_name = dataset.name
            slug = slugify(f"resalloc-ds-{digest_name}", limit=50)
            self._ensure_dataset(dataset, staging / slug, username, slug, f"resalloc {digest_name}")
            sources.append(f"{username}/{slug}")
        checkpoint = launch.checkpoint_dir
        artifacts = checkpoint / "artifacts"
        has_checkpoint = (checkpoint / "state.json").is_file() or (
            artifacts.is_dir() and any(path.is_file() for path in artifacts.rglob("*"))
        )
        if has_checkpoint:
            slug = slugify(f"resalloc-ckpt-{launch.job_id}")
            self._ensure_dataset(checkpoint, staging / slug, username, slug, f"resalloc checkpoint {launch.job_id}")
            sources.append(f"{username}/{slug}")
        return sources

    def _ensure_dataset(self, source: Path, stage: Path, username: str, slug: str, title: str) -> None:
        import shutil

        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        real = source.resolve()
        if real.is_file():
            try:
                os.link(real, stage / real.name)
            except OSError:
                shutil.copy2(real, stage / real.name)
        elif real.is_dir():
            for path in real.rglob("*"):
                if not path.is_file():
                    continue
                target = stage / path.relative_to(real)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copy2(path, target)
        (stage / "dataset-metadata.json").write_text(
            json.dumps(dataset_metadata(username, slug, title[:50]), indent=2) + "\n",
            encoding="utf-8",
        )
        # CLI 2.2 skips directories unless --dir-mode tar. The kernel extracts them.
        version = ["kaggle", "datasets", "version", "-p", str(stage), "-m", "resalloc", "-r", "tar"]
        try:
            self.runner(version)
        except ProviderError:
            self.runner(["kaggle", "datasets", "create", "-p", str(stage), "-r", "tar"])
        self._wait_ready(f"{username}/{slug}")

    def _wait_ready(self, ref: str) -> None:
        import time

        deadline = time.monotonic() + 300
        last = ""
        while True:
            try:
                text = self.runner(["kaggle", "datasets", "status", ref]).strip()
            except ProviderError as exc:
                text = str(exc)
                if not dataset_status_pending(text):
                    raise
            if text.strip().lower() == "ready":
                return
            last = text.strip()
            if last and not dataset_status_pending(last):
                raise ProviderError(f"{ref} dataset status: {last}")
            if time.monotonic() >= deadline:
                raise ProviderError(f"{ref} dataset was not ready: {last}")
            time.sleep(3)


def _failure_from_output(output: Path) -> str:
    for path in sorted(output.glob("*.log")):
        message = failure_from_log(path.read_text(encoding="utf-8", errors="replace"))
        if message:
            return message
    return ""


def _factory(root: Path) -> KaggleAdapter:
    del root
    return KaggleAdapter()
