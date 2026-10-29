import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.cli import main
from resalloc.client import ResAlloc
from resalloc.errors import DiskWatermarkError
from resalloc.hashing import hash_tree
from resalloc.hub import Hub
from resalloc.models import Job, RunPod
from tests.fakes import FakeProvider


class ClientTest(unittest.TestCase):
    def test_dataset_is_uploaded_once_and_status_prints(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            data = root / "frames"
            data.mkdir()
            (data / "a.bin").write_bytes(b"frame")
            job = Job(script=str(script), datasets=[str(data)], start=RunPod(gpu="NVIDIA GeForce RTX 4090"))
            client = ResAlloc(hub_root=root, reserve_bytes=1024 * 1024)
            first = client.submit(job)
            second = client.submit(job)
            digest = hash_tree(data)
            self.assertTrue((root / "cache" / "datasets" / digest / "payload" / "a.bin").exists())
            self.assertNotEqual(first, second)
            hub = Hub(root, providers={"runpod": FakeProvider("runpod")}, reserve_bytes=1024 * 1024)
            hub.tick()
            self.assertEqual(client.status(first)["state"], "running")
            self.assertEqual(client.status(second)["state"], "running")

    def test_watermark_blocks_a_new_upload(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            client = ResAlloc(hub_root=root)
            client.transport.disk.reserve_bytes = 10**18
            with self.assertRaises(DiskWatermarkError):
                client.submit(Job(script=str(script), start=RunPod(gpu="NVIDIA GeForce RTX 4090")))

    def test_class_submit_uses_the_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            previous_root = os.environ.get("RESALLOC_HUB_ROOT")
            previous_host = os.environ.get("RESALLOC_SSH_HOST")
            os.environ["RESALLOC_HUB_ROOT"] = str(root)
            os.environ["RESALLOC_SSH_HOST"] = ""
            os.environ["RESALLOC_RESERVE_BYTES"] = str(1024 * 1024)
            try:
                job_id = ResAlloc.submit(Job(script=str(script), start=RunPod(gpu="NVIDIA GeForce RTX 4090")))
            finally:
                if previous_root is None:
                    os.environ.pop("RESALLOC_HUB_ROOT", None)
                else:
                    os.environ["RESALLOC_HUB_ROOT"] = previous_root
                if previous_host is None:
                    os.environ.pop("RESALLOC_SSH_HOST", None)
                else:
                    os.environ["RESALLOC_SSH_HOST"] = previous_host
            os.environ.pop("RESALLOC_RESERVE_BYTES", None)
            self.assertTrue((root / "inbox" / job_id / "job.json").exists())

    def test_cli_status_reads_the_configured_root(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            job_id = ResAlloc(hub_root=root, reserve_bytes=1024 * 1024).submit(
                Job(script=str(script), start=RunPod(gpu="NVIDIA GeForce RTX 4090"))
            )
            Hub(root, providers={"runpod": FakeProvider("runpod")}, reserve_bytes=1024 * 1024).tick()
            previous_root = os.environ.get("RESALLOC_HUB_ROOT")
            previous_host = os.environ.get("RESALLOC_SSH_HOST")
            os.environ["RESALLOC_HUB_ROOT"] = str(root)
            os.environ["RESALLOC_SSH_HOST"] = ""
            try:
                from io import StringIO
                from contextlib import redirect_stdout

                buffer = StringIO()
                with redirect_stdout(buffer):
                    code = main(["status", job_id, "--json"])
            finally:
                if previous_root is None:
                    os.environ.pop("RESALLOC_HUB_ROOT", None)
                else:
                    os.environ["RESALLOC_HUB_ROOT"] = previous_root
                if previous_host is None:
                    os.environ.pop("RESALLOC_SSH_HOST", None)
                else:
                    os.environ["RESALLOC_SSH_HOST"] = previous_host
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["id"], job_id)
            self.assertEqual(payload["state"], "running")

    def test_submit_fills_default_limits_from_the_config_file(self) -> None:
        from resalloc.models import Limits

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text(
                f'hub_root = "{root}"\n\n[limits]\nmodal_max_spend_usd = 10\nrunpod_max_spend_usd = 4\n',
                encoding="utf-8",
            )
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            previous = os.environ.get("RESALLOC_CONFIG")
            previous_host = os.environ.get("RESALLOC_SSH_HOST")
            os.environ["RESALLOC_CONFIG"] = str(config)
            os.environ["RESALLOC_SSH_HOST"] = ""
            try:
                explicit = ResAlloc(hub_root=root, reserve_bytes=1024 * 1024).submit(
                    Job(
                        script=str(script),
                        start=RunPod(gpu="NVIDIA GeForce RTX 4090"),
                        limits=Limits(modal_max_spend_usd=8),
                    )
                )
                defaulted = ResAlloc(hub_root=root, reserve_bytes=1024 * 1024).submit(
                    Job(script=str(script), start=RunPod(gpu="NVIDIA GeForce RTX 4090"))
                )
            finally:
                if previous is None:
                    os.environ.pop("RESALLOC_CONFIG", None)
                else:
                    os.environ["RESALLOC_CONFIG"] = previous
                if previous_host is None:
                    os.environ.pop("RESALLOC_SSH_HOST", None)
                else:
                    os.environ["RESALLOC_SSH_HOST"] = previous_host
            explicit_limits = json.loads((root / "inbox" / explicit / "job.json").read_text(encoding="utf-8"))["limits"]
            default_limits = json.loads((root / "inbox" / defaulted / "job.json").read_text(encoding="utf-8"))["limits"]
            self.assertEqual(explicit_limits["modal_max_spend_usd"], 8)
            self.assertEqual(explicit_limits["runpod_max_spend_usd"], 4)
            self.assertEqual(default_limits["modal_max_spend_usd"], 10)
            self.assertEqual(default_limits["runpod_max_spend_usd"], 4)

    def test_edit_flags_are_written_for_the_hub(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous_root = os.environ.get("RESALLOC_HUB_ROOT")
            previous_host = os.environ.get("RESALLOC_SSH_HOST")
            os.environ["RESALLOC_HUB_ROOT"] = str(root)
            os.environ["RESALLOC_SSH_HOST"] = ""
            try:
                code = main(
                    [
                        "edit",
                        "job-1",
                        "--volume",
                        "weights",
                        "--region",
                        "us-east",
                        "--cloud",
                        "aws",
                        "--max-price",
                        "0.4",
                        "--host",
                        "lab.example",
                        "--json",
                        '{"gpu": "RTX 3090"}',
                    ]
                )
            finally:
                if previous_root is None:
                    os.environ.pop("RESALLOC_HUB_ROOT", None)
                else:
                    os.environ["RESALLOC_HUB_ROOT"] = previous_root
                if previous_host is None:
                    os.environ.pop("RESALLOC_SSH_HOST", None)
                else:
                    os.environ["RESALLOC_SSH_HOST"] = previous_host
            self.assertEqual(code, 0)
            payload = json.loads((root / "jobs" / "job-1" / "commands" / "edit.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["gpu"], "RTX 3090")
            self.assertEqual(payload["volume"], "weights")
            self.assertEqual(payload["region"], "us-east")
            self.assertEqual(payload["cloud"], "aws")
            self.assertEqual(payload["max_price"], 0.4)
            self.assertEqual(payload["host"], "lab.example")


if __name__ == "__main__":
    unittest.main()
