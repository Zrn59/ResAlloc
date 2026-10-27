"""Assemble the directory a provider receives: script, package, runtime, data."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def copy_package(destination_parent: Path) -> None:
    source = Path(__file__).resolve().parent
    target = destination_parent / "resalloc"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def ensure_job_dirs(job_dir: Path) -> None:
    for name in ("bundle", "pkg", "runtime", "runtime/artifacts", "checkpoints", "checkpoints/artifacts", "datasets", "logs", "commands"):
        (job_dir / name).mkdir(parents=True, exist_ok=True)


def seed_runtime_from_checkpoint(job_dir: Path) -> None:
    """The next provider's worker loads state from the runtime directory."""

    runtime = job_dir / "runtime"
    checkpoint = job_dir / "checkpoints"
    runtime.mkdir(parents=True, exist_ok=True)
    state = checkpoint / "state.json"
    if state.exists():
        shutil.copy2(state, runtime / "state.json")
    source_artifacts = checkpoint / "artifacts"
    if source_artifacts.exists():
        target = runtime / "artifacts"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_artifacts, target)


def iter_stage_files(job_dir: Path, *, include_datasets: bool = True) -> list[tuple[Path, str]]:
    """Local file and the path it should have inside the provider workspace."""

    pairs: list[tuple[Path, str]] = []
    bundle = job_dir / "bundle"
    if bundle.exists():
        for path in bundle.rglob("*"):
            if path.is_file():
                pairs.append((path, path.relative_to(bundle).as_posix()))
    pkg = job_dir / "pkg"
    if pkg.exists():
        for path in pkg.rglob("*"):
            if path.is_file():
                pairs.append((path, f"pkg/{path.relative_to(pkg).as_posix()}"))
    runtime = job_dir / "runtime"
    if runtime.exists():
        for path in runtime.rglob("*"):
            if path.is_file():
                pairs.append((path, f"runtime/{path.relative_to(runtime).as_posix()}"))
    datasets = job_dir / "datasets"
    if include_datasets and datasets.exists():
        for entry in datasets.iterdir():
            real = entry.resolve()
            prefix = f"datasets/{entry.name}"
            if real.is_file():
                pairs.append((real, prefix))
                continue
            if real.is_dir():
                for path in real.rglob("*"):
                    if path.is_file():
                        pairs.append((path, f"{prefix}/{path.relative_to(real).as_posix()}"))
    return pairs


def materialize(job_dir: Path, destination: Path, *, include_datasets: bool = True) -> None:
    """Copy the stage into a real directory. Hardlinks when the disk allows it."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for source, relative in iter_stage_files(job_dir, include_datasets=include_datasets):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
