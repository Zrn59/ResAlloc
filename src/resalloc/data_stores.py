"""Direct and streamed uploads. Bytes go to the provider, not onto the hub disk."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from resalloc.data import CHUNK, list_files, relative_name, transfer_tree
from resalloc.errors import ProviderError


def ssh_argv(ssh: dict, remote: str) -> list[str]:
    port = str(ssh.get("port") or 22)
    user = str(ssh.get("username") or "root")
    argv = [
        "ssh",
        "-p",
        port,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    identity = ssh.get("identity") or os.environ.get("RESALLOC_SSH_IDENTITY")
    if identity:
        argv.extend(["-i", str(identity)])
    argv.extend([f"{user}@{ssh['host']}", remote])
    return argv


class SshChunkSink:
    """Stream one file at a time over SSH. The pipe holds a chunk, not the dataset."""

    def __init__(self, ssh: dict, destination: str) -> None:
        self.ssh = ssh
        self.destination = destination.rstrip("/")
        self._proc: subprocess.Popen[bytes] | None = None
        self._relative = ""

    def begin(self, relative: str, size: int) -> None:
        del size
        self._relative = relative
        remote = f"{self.destination}/{relative}.partial"
        remote_dir = str(Path(remote).parent)
        subprocess.run(ssh_argv(self.ssh, f"mkdir -p {shlex.quote(remote_dir)}"), check=True)
        self._proc = subprocess.Popen(ssh_argv(self.ssh, f"cat > {shlex.quote(remote)}"), stdin=subprocess.PIPE)

    def write(self, data: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("write before begin")
        self._proc.stdin.write(data)

    def end(self, relative: str, digest: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("end before begin")
        self._proc.stdin.close()
        code = self._proc.wait()
        self._proc = None
        if code != 0:
            raise ProviderError(f"ssh stream failed for {relative} ({code})")

    def abort(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        final = f"{self.destination}/{relative}"
        partial = final + ".partial"
        marker = final + ".done"
        command = (
            f"mv {shlex.quote(partial)} {shlex.quote(final)} && "
            f"printf %s {shlex.quote(digest)} > {shlex.quote(marker)}"
        )
        subprocess.run(ssh_argv(self.ssh, command), check=True)

    def complete(self) -> None:
        marker = f"{self.destination}/COMPLETE"
        subprocess.run(ssh_argv(self.ssh, f"mkdir -p {shlex.quote(self.destination)} && touch {shlex.quote(marker)}"), check=True)


def upload_runpod(endpoint: dict, path: Path, name: str, stream: bool) -> None:
    ssh = endpoint.get("ssh") or {}
    if not ssh.get("host"):
        raise ProviderError("Runpod data endpoint has no SSH host")
    remote = f"{endpoint.get('remote_dir') or '/resalloc/datasets'}/{name}"
    # The proxy cannot rsync. A shell pipe still works, and stream mode uses it anyway.
    if stream or endpoint.get("ssh_kind") == "proxy":
        transfer_tree(path, SshChunkSink(ssh, remote))
        return
    subprocess.run(ssh_argv(ssh, f"mkdir -p {shlex.quote(remote)}"), check=True)
    user = ssh.get("username") or "root"
    destination = f"{user}@{ssh['host']}:{shlex.quote(remote + '/')}"
    ssh_cmd = f"ssh -p {ssh.get('port') or 22} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    identity = ssh.get("identity") or os.environ.get("RESALLOC_SSH_IDENTITY")
    if identity:
        ssh_cmd += f" -i {shlex.quote(str(identity))}"
    source = str(path).rstrip("/") + ("/" if path.is_dir() else "")
    subprocess.run(["rsync", "-a", "-e", ssh_cmd, source, destination], check=True)


def upload_modal(store: str, path: Path, stream: bool, prefix: str = "") -> None:
    if not store:
        raise ProviderError("Modal dataset needs a volume name (Modal.volume or Dataset.store)")
    try:
        import modal
    except ImportError as exc:
        raise ProviderError("direct Modal upload needs the modal package and a token on this machine") from exc
    volume = modal.Volume.from_name(store, create_if_missing=True)
    files = list_files(path)
    folder = prefix.strip("/")
    print(f"modal volume {store}: {len(files)} files -> {folder or '/'}", flush=True)
    if stream:
        for item in files:
            _modal_put(volume, path, [item], folder)
            _modal_marker(volume, _remote_name(folder, relative_name(path, item) + ".done"))
        _modal_marker(volume, _remote_name(folder, "COMPLETE"))
        return
    _modal_put(volume, path, files, folder)
    _modal_marker(volume, _remote_name(folder, "COMPLETE"))


def _remote_name(folder: str, name: str) -> str:
    if not folder:
        return name
    return f"{folder}/{name}"


def _modal_put(volume: object, root: Path, files: list[Path], folder: str = "") -> None:
    """Commit in ~1 GiB chunks. One VolumePutFiles of the whole tree hits Modal's stream timeout."""

    batch_upload = getattr(volume, "batch_upload", None)
    if batch_upload is None:
        raise ProviderError("this Modal volume cannot accept uploads")
    total = len(files)
    done = 0
    chunk: list[Path] = []
    chunk_bytes = 0
    limit = 1024**3

    def flush() -> None:
        nonlocal done, chunk_bytes
        if not chunk:
            return
        with batch_upload(force=True) as batch:
            for item in chunk:
                batch.put_file(str(item), _remote_name(folder, relative_name(root, item)))
        done += len(chunk)
        print(f"modal committed {done}/{total} {relative_name(root, chunk[-1])}", flush=True)
        chunk.clear()
        chunk_bytes = 0

    for item in files:
        chunk.append(item)
        chunk_bytes += item.stat().st_size
        if chunk_bytes >= limit or len(chunk) >= 2000:
            flush()
    flush()


def _modal_marker(volume: object, name: str) -> None:
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    try:
        handle.write("ok\n")
        handle.close()
        batch_upload = getattr(volume, "batch_upload")
        with batch_upload(force=True) as batch:
            batch.put_file(handle.name, name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def upload_kaggle(slug: str, path: Path, runner=None) -> None:
    """Upload a hardlinked stage. The source directory is not modified.

    Kaggle has no live stream into a running kernel.
    """

    import json
    import os
    import shutil
    import tempfile

    run = runner or _kaggle
    username = os.environ.get("KAGGLE_USERNAME") or ""
    if not username:
        config = Path.home() / ".kaggle" / "kaggle.json"
        if config.exists():
            username = str(json.loads(config.read_text(encoding="utf-8")).get("username") or "")
    if not username:
        raise ProviderError("direct Kaggle upload needs KAGGLE_USERNAME or ~/.kaggle/kaggle.json on this machine")
    stage_root = Path(tempfile.mkdtemp(prefix="resalloc-kaggle-"))
    stage = stage_root / "dataset"
    stage.mkdir()
    try:
        _stage_tree(path, stage)
        meta = stage / "dataset-metadata.json"
        if not meta.exists():
            meta.write_text(
                json.dumps({"title": slug, "id": f"{username}/{slug}", "licenses": [{"name": "CC0-1.0"}]}) + "\n",
                encoding="utf-8",
            )
        try:
            run(["kaggle", "datasets", "version", "-p", str(stage), "-m", "resalloc", "-r", "tar"])
        except Exception:
            run(["kaggle", "datasets", "create", "-p", str(stage), "-r", "tar"])
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _stage_tree(source: Path, stage: Path) -> None:
    """Hardlink the tree into a temp directory so metadata can sit beside it."""

    import os
    import shutil

    files = list_files(source)
    for item in files:
        relative = relative_name(source, item)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(item, target)
        except OSError:
            shutil.copy2(item, target)


def _kaggle(argv: list[str]) -> str:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ProviderError((completed.stderr or completed.stdout or "kaggle failed").strip())
    return completed.stdout


# Re-exported so callers can share the chunk size with tests.
__all__ = ["CHUNK", "upload_kaggle", "upload_modal", "upload_runpod"]
