"""A short GPU smoke test. It checks that a provider can run and then exits."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.client import ResAlloc
from resalloc.errors import LaptopOfflineError
from resalloc.models import Job, Kaggle, Limits, Modal, RunPod, Vast

SMOKE_PROVIDERS = ("modal", "kaggle", "runpod", "vast", "laptop")

SMOKE_SCRIPT = """
from resalloc.worker import Worker

worker = Worker()
info = {"ok": False}
try:
    import torch
    info["torch"] = torch.__version__
    info["cuda"] = bool(torch.cuda.is_available())
    if torch.cuda.is_available():
        info["device"] = torch.cuda.get_device_name(0)
        matrix = torch.randn(256, 256, device="cuda")
        info["norm"] = float((matrix @ matrix).norm())
        info["ok"] = True
    else:
        info["error"] = "cuda is not available"
except Exception as exc:
    info["error"] = str(exc)
worker.save(info)
"""


def parse_providers(names: list[str]) -> list[str]:
    chosen: list[str] = []
    for raw in names:
        for piece in raw.split(","):
            name = piece.strip().lower()
            if not name:
                continue
            if name not in SMOKE_PROVIDERS:
                raise ValueError(f"unknown provider {name!r}; choose from {', '.join(SMOKE_PROVIDERS)}")
            if name not in chosen:
                chosen.append(name)
    if not chosen:
        raise ValueError("name at least one provider")
    return chosen


def smoke_limits() -> Limits:
    return Limits(
        soft_buffer=timedelta(minutes=2),
        hard_buffer=timedelta(seconds=30),
        modal_max_spend_usd=0.10,
        runpod_max_spend_usd=0.20,
        runpod_max_hourly_usd=0.70,
        kaggle_min_gpu_hours=0.05,
        vast_max_spend_usd=0.20,
        vast_max_hourly_usd=0.30,
    )


def smoke_target(provider: str):
    if provider == "modal":
        return Modal(gpu="T4", timeout_hours=0.1)
    if provider == "kaggle":
        return Kaggle(accelerator="NvidiaTeslaT4", internet=False)
    if provider == "runpod":
        return RunPod(gpu="NVIDIA GeForce RTX 3090", disk_gb=20)
    if provider == "vast":
        return Vast(gpu="RTX 3090", disk_gb=16)
    if provider == "laptop":
        from resalloc.models import Laptop

        return Laptop()
    raise ValueError(provider)


def kaggle_token_ready(secret_names: list[str]) -> bool:
    return "KAGGLE_API_TOKEN" in secret_names


def run_smoke(
    client: ResAlloc,
    providers: list[str],
    *,
    timeout_seconds: float = 900,
    poll_seconds: float = 10,
    on_line=None,
) -> list[dict]:
    results = []
    for provider in providers:
        if on_line is not None:
            on_line(f".. {provider}")
        if provider == "kaggle" and not _hub_has_kaggle_token(client):
            result = {
                "provider": provider,
                "ok": False,
                "error": "KAGGLE_API_TOKEN is not in the hub secrets.env, so the test was not started",
            }
        elif provider == "vast" and not _hub_has_secret(client, "VAST_API_KEY"):
            result = {
                "provider": provider,
                "ok": False,
                "error": "VAST_API_KEY is not in the hub secrets.env, so the test was not started",
            }
        else:
            result = _run_one(client, provider, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds)
        results.append(result)
        if on_line is not None:
            on_line(format_result(result))
    return results


def _hub_has_secret(client: ResAlloc, name: str) -> bool:
    import os

    if client.config.local:
        return bool(os.environ.get(name))
    from resalloc.setup import probe_hub

    probe, _error = probe_hub(client.config)
    if not probe:
        return False
    return name in set(probe.get("secrets") or [])


def _hub_has_kaggle_token(client: ResAlloc) -> bool:
    from resalloc.setup import probe_hub

    if client.config.local:
        return kaggle_token_ready([])
    probe, _error = probe_hub(client.config)
    if not probe:
        return False
    return kaggle_token_ready(list(probe.get("secrets") or []))


def _run_one(client: ResAlloc, provider: str, *, timeout_seconds: float, poll_seconds: float) -> dict:
    started = time.monotonic()
    release_agent = None
    if provider == "laptop" and not client.transport.agent_status(90).get("online"):
        release_agent = _hold_laptop_agent(client)
    try:
        return _run_submitted(
            client,
            provider,
            started=started,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
    finally:
        if release_agent is not None:
            release_agent()


def _hold_laptop_agent(client: ResAlloc):
    """Heartbeat and run the laptop job for this test, then stop."""

    import threading

    from resalloc.laptop_agent import LaptopAgent

    stop = threading.Event()
    agent = LaptopAgent(client.config)

    def loop() -> None:
        while not stop.is_set():
            agent.once()
            stop.wait(5)

    thread = threading.Thread(target=loop, name="resalloc-smoke-laptop", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if client.transport.agent_status(90).get("online"):
            break
        time.sleep(0.5)
    else:
        stop.set()
        raise LaptopOfflineError(
            "start is the laptop, and it is not heartbeating. Run `resalloc laptop agent` on it."
        )

    def release() -> None:
        stop.set()
        for proc in list(agent.procs.values()):
            if proc.poll() is None:
                proc.terminate()
        thread.join(timeout=5)

    return release


def _run_submitted(
    client: ResAlloc,
    provider: str,
    *,
    started: float,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict:
    with TemporaryDirectory(prefix="resalloc-smoke-") as temporary:
        script = Path(temporary) / "smoke.py"
        script.write_text(SMOKE_SCRIPT, encoding="utf-8")
        requirements = None
        if provider == "modal":
            # Modal's plain image has no torch. The GPU drivers are on the host,
            # and `pip install torch` is the image Modal documents for CUDA.
            req = Path(temporary) / "requirements.txt"
            req.write_text("torch\n", encoding="utf-8")
            requirements = str(req)
        job_id = client.submit(
            Job(
                script=str(script),
                requirements=requirements,
                start=smoke_target(provider),
                limits=smoke_limits(),
            )
        )
    deadline = time.monotonic() + timeout_seconds
    last = {}
    while time.monotonic() < deadline:
        journal = client.status(job_id)
        if isinstance(journal, dict):
            last = journal
            if journal.get("state") in {"completed", "stopped", "failed"}:
                break
        time.sleep(poll_seconds)
    else:
        return {"provider": provider, "job_id": job_id, "ok": False, "error": "timed out", "state": last.get("state")}
    state_text = client.transport.read_job_file(job_id, "checkpoints/state.json")
    info = {}
    if state_text:
        try:
            info = json.loads(state_text)
        except json.JSONDecodeError:
            info = {"error": "checkpoint was not valid json"}
    elapsed = round(time.monotonic() - started, 1)
    return {
        "provider": provider,
        "job_id": job_id,
        "ok": bool(info.get("ok")),
        "seconds": elapsed,
        "state": last.get("state"),
        "reason": last.get("reason") or "",
        "device": info.get("device"),
        "norm": info.get("norm"),
        "error": info.get("error") or last.get("last_error") or "",
    }


def format_result(result: dict) -> str:
    mark = "ok" if result.get("ok") else "!!"
    bits = [mark, str(result.get("provider"))]
    if result.get("seconds") is not None:
        bits.append(f"{result['seconds']}s")
    if result.get("device"):
        bits.append(str(result["device"]))
    if result.get("norm") is not None:
        bits.append(f"norm={result['norm']}")
    if result.get("error"):
        bits.append(str(result["error"]))
    elif not result.get("ok") and result.get("reason"):
        bits.append(str(result["reason"]))
    if result.get("job_id"):
        bits.append(str(result["job_id"]))
    return "  ".join(bits)
