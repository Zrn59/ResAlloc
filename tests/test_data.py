import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.data import DirectorySink, describe, transfer_tree
from resalloc.models import Dataset, Modal, RunPod
from resalloc.providers.kaggle import render_launcher, session_hour_cap
from resalloc.providers.modal_backend import burn_from_meter, usage_usd
from resalloc.providers.runpod import build_create_body, ssh_endpoint, training_command
from resalloc.worker import Worker


class RecordingSink:
    def __init__(self) -> None:
        self.max_write = 0
        self.files: list[str] = []
        self.done = False

    def begin(self, relative: str, size: int) -> None:
        del relative, size

    def write(self, data: bytes) -> None:
        self.max_write = max(self.max_write, len(data))

    def end(self, relative: str, digest: str) -> None:
        del digest
        self.files.append(relative)

    def complete(self) -> None:
        self.done = True


class DataRouteTest(unittest.TestCase):
    def test_stream_keeps_only_one_chunk(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "frames.bin"
            payload.write_bytes(b"abcdefghij")
            sink = RecordingSink()
            report = transfer_tree(payload, sink, chunk=4)
            self.assertEqual(sink.max_write, 4)
            self.assertEqual(sink.files, ["frames.bin"])
            self.assertTrue(sink.done)
            self.assertEqual(report["files"], 1)

    def test_directory_sink_marks_files_before_complete(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            source.mkdir()
            (source / "a.txt").write_text("hi", encoding="utf-8")
            dest = root / "out"
            transfer_tree(source, DirectorySink(dest), chunk=1)
            self.assertTrue((dest / "a.txt").exists())
            self.assertTrue((dest / "a.txt.done").exists())
            self.assertTrue((dest / "COMPLETE").exists())
            worker = Worker(root / "job")
            (worker.job_dir / "datasets").mkdir()
            (worker.job_dir / "datasets" / "src").symlink_to(dest, target_is_directory=True)
            ready = list(worker.iter_ready("src", poll=0.01))
            self.assertEqual([path.name for path in ready], ["a.txt"])

    def test_direct_modal_dataset_is_not_a_hub_copy(self) -> None:
        described = describe(Dataset("./huge", route="direct", keep="modal", store="datasets"), Modal(gpu="A10G", volume="datasets"))
        self.assertEqual(described["keep"], "modal")
        self.assertEqual(described["store"], "datasets")
        self.assertNotIn("hash", described)

    def test_kaggle_upload_leaves_the_source_untouched(self) -> None:
        import os

        from resalloc.data_stores import upload_kaggle

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "frames"
            source.mkdir()
            (source / "a.bin").write_bytes(b"frame")
            calls: list[list[str]] = []

            def runner(argv: list[str]) -> str:
                calls.append(argv)
                return ""

            old = os.environ.get("KAGGLE_USERNAME")
            os.environ["KAGGLE_USERNAME"] = "me"
            try:
                upload_kaggle("frames", source, runner=runner)
            finally:
                if old is None:
                    os.environ.pop("KAGGLE_USERNAME", None)
                else:
                    os.environ["KAGGLE_USERNAME"] = old
            self.assertFalse((source / "dataset-metadata.json").exists())
            self.assertTrue(calls)
            self.assertNotIn(str(source), calls[0])

    def test_plain_path_stays_on_the_hub(self) -> None:
        described = describe(Dataset("./small"), RunPod(gpu="NVIDIA GeForce RTX 4090"))
        self.assertEqual(described["route"], "hub")
        self.assertEqual(described["keep"], "hub")

    def test_runpod_settings_reach_the_create_body(self) -> None:
        body = build_create_body(
            {
                "gpu": "NVIDIA GeForce RTX 4090",
                "spot": True,
                "cloud": "SECURE",
                "data_centers": ["EU-NL-1"],
                "volume_gb": 100,
                "ports": ["8888/http"],
                "env": {"RUN": "1"},
            },
            "job-1",
        )
        self.assertEqual(body["cloud"], "SECURE")
        self.assertEqual(body["dataCenterIds"], ["EU-NL-1"])
        self.assertEqual(body["mounts"]["persistent"]["size"], 100)
        self.assertIn("22/tcp", body["ports"])
        self.assertEqual(body["env"]["RUN"], "1")

    def test_runpod_direct_ssh_is_preferred_and_the_script_records_exit(self) -> None:
        endpoint, kind = ssh_endpoint(
            {
                "id": "pod",
                "ssh": {
                    "proxy": {"host": "ssh.runpod.io", "port": 22, "username": "pod"},
                    "direct": {"host": "203.0.113.5", "port": 4000, "username": "root"},
                },
            }
        )
        self.assertEqual(kind, "direct")
        self.assertEqual(endpoint["host"], "203.0.113.5")
        command = training_command("train.py", [])
        self.assertIn("finished.json", command)
        self.assertIn("requirements.txt", command)
        self.assertIn("RESALLOC_DATA_DIR=/resalloc/datasets", command)
        waiting = training_command("train.py", [], datasets=["big set"], data_timeout=10)
        self.assertIn("big set/COMPLETE", waiting)
        self.assertIn("_left=10", waiting)

    def test_modal_meter_ignores_a_zero_invoice_while_credits_remain(self) -> None:
        summary = type("Summary", (), {"metered_cost": "4.5", "billed_cost": "0"})()
        self.assertEqual(usage_usd(summary), 4.5)
        start = __import__("datetime").datetime(2026, 10, 16, 12, 0, tzinfo=__import__("datetime").timezone.utc)
        later = start + __import__("datetime").timedelta(minutes=30)
        self.assertAlmostEqual(burn_from_meter((1.0, start), 1.5, later), 1.0)

    def test_kaggle_session_is_shorter_than_the_platform_kill(self) -> None:
        self.assertEqual(session_hour_cap("NvidiaTeslaT4"), 11.0)
        self.assertEqual(session_hour_cap("TpuV5E8"), 8.0)
        text = render_launcher("train.py", [])
        self.assertIn("/kaggle/working", text)

    def test_worker_finds_a_dataset_outside_the_runtime_dir(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "datasets" / "huge"
            data.mkdir(parents=True)
            (data / "COMPLETE").write_text("ok\n", encoding="utf-8")
            worker = Worker(root / "runtime")
            self.assertEqual(worker.dataset_root("huge"), data)

    def test_proxy_checkpoint_comes_back_through_tar(self) -> None:
        from resalloc.providers.base import RunningJob
        from resalloc.providers.runpod import RunpodAdapter

        fetched: list[str] = []

        def download(endpoint: dict, remote: str, local: Path) -> None:
            del endpoint
            fetched.append(remote)
            (local / "state.json").write_text('{"epoch": 4}', encoding="utf-8")
            (local / "ack.json").write_text('{"signal": "stop"}', encoding="utf-8")

        adapter = RunpodAdapter(
            ssh_runner=lambda endpoint, command: "",
            file_pull=lambda *args: (_ for _ in ()).throw(AssertionError("rsync")),
            shell_download_fn=download,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            handle = RunningJob(
                "runpod",
                "job",
                "pod",
                {
                    "ssh": {"host": "ssh.runpod.io", "port": 22, "username": "pod"},
                    "ssh_kind": "proxy",
                    "runtime_dir": str(runtime),
                },
            )
            self.assertTrue(adapter.pull_checkpoint(handle, root / "ckpt"))
            self.assertEqual(fetched, ["/resalloc/runtime"])

    def test_modal_burn_uses_the_first_meter_sample(self) -> None:
        from datetime import datetime, timedelta, timezone

        from resalloc.providers.modal_backend import ModalAdapter

        start = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        clock = {"now": start}
        samples = iter((1.0, 1.0, 1.4))

        def billing() -> dict:
            return {"billed_cost": next(samples), "burn_per_hour": None}

        adapter = ModalAdapter(billing=billing, clock=lambda: clock["now"])
        adapter._money(25, alive=True, detail="", meter_key="sandbox")
        clock["now"] = start + timedelta(minutes=3)
        flat = adapter._money(25, alive=True, detail="", meter_key="sandbox")
        self.assertEqual(flat.burn_usd_per_hour, 0)
        clock["now"] = start + timedelta(minutes=4)
        jumped = adapter._money(25, alive=True, detail="", meter_key="sandbox")
        self.assertAlmostEqual(jumped.burn_usd_per_hour or 0, 6.0)


if __name__ == "__main__":
    unittest.main()
