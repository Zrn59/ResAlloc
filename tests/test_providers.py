import ast
import json
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.models import Limits
from resalloc.providers.base import JobLaunch
from resalloc.errors import ProviderError
from resalloc.providers.kaggle import (
    _hours,
    cli_command,
    dataset_status_pending,
    default_cli,
    failure_from_log,
    interpret_status,
    kernel_metadata,
    parse_quota_csv,
    quota_hours_left,
    render_launcher,
)
from resalloc.providers.kaggle import KaggleAdapter
from resalloc.providers.modal_backend import burn_from_report
from resalloc.providers.runpod import (
    RunpodAdapter,
    RunpodApi,
    RunpodHttpError,
    billing_total,
    build_create_body,
    headroom_usd,
    price_of,
    spent_estimate,
)


QUOTA = """resource,used,remaining,total,refreshAt
GPU,1.50,8.50,10,2026-10-20T00:00:00Z
TPU,0,9,9,
"""


class RunpodMathTest(unittest.TestCase):
    def test_create_body_uses_community_cloud_for_spot(self) -> None:
        body = build_create_body({"gpu": "NVIDIA GeForce RTX 4090", "spot": True, "disk_gb": 30}, "job-1")
        self.assertEqual(body["cloud"], "COMMUNITY")
        self.assertEqual(body["gpu"]["id"], "NVIDIA GeForce RTX 4090")
        self.assertEqual(body["disk"], 30)
        self.assertTrue(body["startSsh"])
        self.assertNotIn("entrypoint", body)
        self.assertNotIn("cmd", body)

    def test_billing_and_headroom(self) -> None:
        payload = {"metadata": {"totals": {"totalAmount": 4.5}}, "records": []}
        self.assertEqual(billing_total(payload), 4.5)
        self.assertEqual(spent_estimate(billing_delta=0.1, hourly=0.5, elapsed_hours=2), 1.0)
        self.assertEqual(headroom_usd(balance=3, min_balance=1, max_spend=5, spent=1), 2)
        self.assertEqual(headroom_usd(balance=None, min_balance=1, max_spend=5, spent=1), 4)
        self.assertIsNone(headroom_usd(balance=None, min_balance=1, max_spend=None, spent=1))

    def test_price_row(self) -> None:
        row = {"id": "NVIDIA GeForce RTX 4090", "price": {"secure": 0.69, "community": 0.34}}
        self.assertEqual(price_of(row, spot=True), 0.34)
        self.assertEqual(price_of(row, spot=False), 0.69)

    def test_balance_404_is_skipped_and_spend_cap_is_used(self) -> None:
        calls: list[str] = []

        def transport(method, url, body, headers):
            del method, body, headers
            calls.append(url)
            if url.endswith("/v2/account") or url.endswith("/v2/account/billing") or url.endswith("/v2/billing/balance"):
                return 404, {"detail": "missing"}
            if url.endswith("/v2/billing?bucketSize=hour&lastN=48"):
                return 200, {"metadata": {"totals": {"totalAmount": 1.25}}}
            if url.endswith("/v2/catalog/gpus"):
                return 200, [{"id": "NVIDIA GeForce RTX 4090", "price": {"secure": 0.5, "community": 0.2}}]
            if url.endswith("/v2/pods") and "billing" not in url:
                return 201, {"id": "pod1", "status": "RUNNING"}
            if "/v2/pods/" in url:
                return 200, {
                    "id": "pod1",
                    "status": "RUNNING",
                    "cost": 0.5,
                    "ssh": {"direct": {"host": "203.0.113.5", "port": 4000, "username": "root"}},
                }
            raise AssertionError(url)

        api = RunpodApi("test-key", transport=transport)
        self.assertIsNone(api.try_balance())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            (root / "runtime").mkdir()
            launch = JobLaunch(
                job_id="job-1",
                job_dir=root,
                script=script,
                requirements=None,
                datasets=[],
                checkpoint_dir=root / "checkpoints",
                runtime_dir=root / "runtime",
                limits=Limits(runpod_max_spend_usd=3, runpod_min_balance_usd=1),
                settings={"type": "runpod", "gpu": "NVIDIA GeForce RTX 4090", "spot": False},
                args=[],
            )
            synced: list[tuple] = []
            commands: list[str] = []
            adapter = RunpodAdapter(
                api=api,
                ssh_runner=lambda endpoint, command: commands.append(command) or "",
                file_sync=lambda endpoint, files: synced.append((endpoint["host"], len(files))),
                shell_upload_fn=lambda endpoint, files: synced.append((endpoint["host"], len(files))),
                file_pull=lambda endpoint, remote, local: None,
                sleep=lambda seconds: None,
            )
            snapshot = adapter.preflight(launch)
            self.assertEqual(snapshot.hourly_price_usd, 0.5)
            self.assertAlmostEqual(snapshot.headroom_usd, 3)
            handle = adapter.start(launch)
            self.assertEqual(handle.remote_id, "pod1")
            self.assertTrue(any("python" in command for command in commands))
            self.assertEqual(commands[0], "echo ok")
            self.assertTrue(synced)

    def test_ssh_ready_retries_refusal_and_stops_on_auth_failure(self) -> None:
        attempts = {"n": 0}

        def refused(endpoint: dict, command: str) -> str:
            del endpoint, command
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ProviderError("ssh: connect to host 203.0.113.5 port 4000: Connection refused")
            return "ok\n"

        adapter = RunpodAdapter(ssh_runner=refused, sleep=lambda seconds: None)
        adapter._wait_ssh_accepts({"host": "203.0.113.5", "port": 4000})
        self.assertEqual(attempts["n"], 3)

        def denied(endpoint: dict, command: str) -> str:
            del endpoint, command
            raise ProviderError("Permission denied (publickey)")

        adapter = RunpodAdapter(ssh_runner=denied, sleep=lambda seconds: None)
        with self.assertRaises(ProviderError):
            adapter._wait_ssh_accepts({"host": "203.0.113.5", "port": 4000})


class ModalDownloadTest(unittest.TestCase):
    def test_a_missing_file_does_not_abort_the_rest_of_the_download(self) -> None:
        from resalloc.providers.modal_backend import ModalAdapter

        class Volume:
            def read_file(self, remote: str):
                def chunks():
                    if remote.endswith("ack.json"):
                        raise FileNotFoundError('path "/runtime/ack.json" does not exist')
                    yield b'{"ok": true}'

                return chunks()

        with TemporaryDirectory() as temporary:
            local = Path(temporary) / "state.json"
            adapter = ModalAdapter()
            adapter._read_remote(Volume(), "runtime/state.json", local)
            self.assertEqual(local.read_bytes(), b'{"ok": true}')
            missing = Path(temporary) / "ack.json"
            adapter._read_remote(Volume(), "runtime/ack.json", missing)
            self.assertFalse(missing.exists())

    def test_burn_uses_the_last_hour(self) -> None:
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        items = [
            {"cost": "0.25", "end": (now - timedelta(minutes=10)).isoformat()},
            {"cost": "9", "end": (now - timedelta(hours=3)).isoformat()},
        ]
        self.assertEqual(burn_from_report(items, now), 0.25)


class KaggleParseTest(unittest.TestCase):
    def test_wait_ready_retries_a_forbidden_status(self) -> None:
        calls = {"n": 0}

        def runner(argv: list[str]) -> str:
            del argv
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderError(
                    "403 Client Error: Forbidden for url: https://api.kaggle.com/v1/datasets.DatasetApiService/GetDatasetStatus"
                )
            return "ready"

        with patch("time.sleep"):
            KaggleAdapter(runner=runner, username="me")._wait_ready("me/data")
        self.assertEqual(calls["n"], 2)

    def test_a_forbidden_status_is_still_pending(self) -> None:
        self.assertTrue(
            dataset_status_pending(
                "403 Client Error: Forbidden for url: https://api.kaggle.com/v1/datasets.DatasetApiService/GetDatasetStatus"
            )
        )
        self.assertFalse(dataset_status_pending("ready"))

    def test_missing_cli_is_a_provider_error(self) -> None:
        self.assertEqual(cli_command(["python", "-V"]), ["python", "-V"])
        with self.assertRaises(ProviderError):
            default_cli(["resalloc-no-such-kaggle"])

    def test_venv_python_symlink_still_selects_the_venv_kaggle(self) -> None:
        with TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            (bin_dir / "python").symlink_to(sys.executable)
            kaggle = bin_dir / "kaggle"
            kaggle.write_text("#!/bin/sh\n", encoding="utf-8")
            kaggle.chmod(0o755)
            command = cli_command(["kaggle", "quota"], executable=str(bin_dir / "python"))
            self.assertEqual(command[0], str(kaggle))


    def test_quota_and_status(self) -> None:
        parsed = parse_quota_csv(QUOTA)
        self.assertEqual(quota_hours_left(parsed, "NvidiaTeslaT4", 0.3), 8.2)
        self.assertEqual(interpret_status('user/slug has status "COMPLETE"'), "completed")
        self.assertEqual(interpret_status("INCOMPLETE"), "unknown")
        self.assertEqual(interpret_status("RUNNING"), "running")
        self.assertEqual(_hours("0s"), 0.0)
        self.assertEqual(_hours("1h30m"), 1.5)
        self.assertEqual(_hours("8.50"), 8.5)
        meta = kernel_metadata(
            username="me",
            slug="resalloc-job",
            title="resalloc",
            accelerator="NvidiaTeslaT4",
            internet=True,
            dataset_sources=["me/data"],
        )
        self.assertEqual(meta["id"], "me/resalloc-job")
        self.assertTrue(meta["enable_gpu"])
        launcher = render_launcher("train.py", ["--epochs", "1"])
        ast.parse(launcher)
        self.assertIn("runpy.run_path", launcher)
        self.assertIn("/kaggle/input", launcher)
        self.assertIn("tarfile", launcher)
        self.assertIn("train.py", launcher)
        log = '[{"stream_name":"stderr","data":"Traceback (most recent call last):\\n"},{"stream_name":"stderr","data":"FileNotFoundError: missing /kaggle/src/smoke.py\\n"}]'
        self.assertIn("FileNotFoundError", failure_from_log(log))

    def test_push_uses_the_cli(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> str:
            calls.append(argv)
            if argv[1:3] == ["datasets", "status"]:
                return "ready"
            return ""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "bundle" / "train.py"
            script.parent.mkdir()
            script.write_text("print(1)\n", encoding="utf-8")
            (root / "runtime").mkdir()
            (root / "checkpoints").mkdir()
            launch = JobLaunch(
                job_id="job-1",
                job_dir=root,
                script=script,
                requirements=None,
                datasets=[],
                checkpoint_dir=root / "checkpoints",
                runtime_dir=root / "runtime",
                limits=Limits(),
                settings={"type": "kaggle", "accelerator": "NvidiaTeslaT4", "internet": True},
                args=[],
            )
            handle = KaggleAdapter(runner=runner, username="me").start(launch)
            self.assertEqual(handle.remote_id, "me/resalloc-job-1")
            self.assertTrue(any(item[:3] == ["kaggle", "kernels", "push"] for item in calls))
            version = next(item for item in calls if item[:3] == ["kaggle", "datasets", "version"])
            self.assertEqual(version[-2:], ["-r", "tar"])
            self.assertTrue(any(item[:3] == ["kaggle", "datasets", "status"] for item in calls))
            meta = json.loads((root / "kaggle-stage" / "kernel-metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["code_file"], "resalloc_launch.py")
            self.assertIn("me/resalloc-code-job-1", meta["dataset_sources"])


class RunpodHttpTest(unittest.TestCase):
    def test_http_error_carries_the_status(self) -> None:
        def transport(method, url, body, headers):
            del method, url, body, headers
            return 402, {"detail": "insufficient balance"}

        api = RunpodApi("k", transport=transport)
        with self.assertRaises(RunpodHttpError) as caught:
            api.create_pod({"name": "x"})
        self.assertEqual(caught.exception.status, 402)


if __name__ == "__main__":
    unittest.main()
