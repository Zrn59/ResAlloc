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


if __name__ == "__main__":
    unittest.main()
