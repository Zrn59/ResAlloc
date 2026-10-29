import json
import os
import shlex
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.client import ResAlloc
from resalloc.errors import ProviderError
from resalloc.io import read_json, write_json
from resalloc.models import Job, Laptop, Limits, Migrate, Modal, RunPod
from resalloc.policy import AccountSnapshot, Signal
from resalloc.providers.laptop import LaptopAdapter, write_heartbeat
from resalloc.records import write_command
from tests.fakes import FakeProvider

from resalloc.hub import Hub, send_confirm_notice


def _limits() -> Limits:
    return Limits(soft_buffer=timedelta(minutes=15), hard_buffer=timedelta(minutes=5))


def _job(script: Path, start, failover) -> Job:
    return Job(script=str(script), start=start, limits=_limits(), failover=failover)


class HubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "train.py"
        self.script.write_text("print('train')\n", encoding="utf-8")
        self.runpod = FakeProvider("runpod")
        self.modal = FakeProvider(
            "modal",
            snapshot=AccountSnapshot(provider="modal", alive=True, headroom_usd=20, burn_usd_per_hour=0.2),
        )
        self.hub = Hub(
            self.root,
            providers={"runpod": self.runpod, "modal": self.modal, "laptop": LaptopAdapter(self.root)},
            reserve_bytes=1024 * 1024,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _submit(self, job: Job) -> str:
        return ResAlloc(hub_root=self.root, reserve_bytes=1024 * 1024).submit(job)

    def test_soft_limit_moves_to_the_next_provider_with_the_checkpoint(self) -> None:
        job_id = self._submit(
            _job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090", spot=True), [Migrate(Modal(gpu="A10G"))])
        )
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["state"], "running")
        self.assertEqual(self.runpod.started[0]["gpu"], "NVIDIA GeForce RTX 4090")
        self.assertTrue(self.runpod.started[0]["spot"])

        self.runpod.snapshot = AccountSnapshot(
            provider="runpod", alive=True, headroom_usd=0.2, burn_usd_per_hour=1.2, hourly_price_usd=1.2
        )
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["state"], "checkpointing")
        self.assertEqual(self.runpod.signals, [Signal.CHECKPOINT_AND_MIGRATE])

        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "running")
        self.assertEqual(journal["handle"]["provider"], "modal")
        self.assertTrue(journal["checkpoint_fresh"])
        self.assertEqual(journal["step"], 2)
        self.assertEqual(self.runpod.stopped, ["runpod-1"])
        self.assertEqual(self.modal.started[0]["gpu"], "A10G")
        self.assertEqual(read_json(self.root / "jobs" / job_id / "checkpoints" / "state.json")["epoch"], 2)

    def test_confirm_parks_and_edit_changes_the_next_gpu(self) -> None:
        job_id = self._submit(
            _job(
                self.script,
                RunPod(gpu="NVIDIA GeForce RTX 4090"),
                [Migrate(Modal(gpu="A10G"), confirm=True)],
            )
        )
        self.hub.tick()
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=True, headroom_usd=0, burn_usd_per_hour=1)
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "awaiting_confirm")
        self.assertEqual(self.modal.started, [])
        self.assertTrue(journal["checkpoint_fresh"])

        write_command(self.root, job_id, "edit", {"gpu": "RTX 3090"})
        write_command(self.root, job_id, "confirm", {})
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "running")
        self.assertEqual(journal["handle"]["provider"], "modal")
        self.assertEqual(self.modal.started[0]["gpu"], "RTX 3090")

    def test_a_queued_job_starts_on_the_next_tick(self) -> None:
        job_id = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), []))
        self.hub.tick()
        self.assertEqual(len(self.runpod.started), 1)
        journal = self._journal(job_id)
        journal["state"] = "queued"
        journal["handle"] = None
        journal["cursor"] = -1
        write_json(self.root / "jobs" / job_id / "journal.json", journal)
        self.hub.tick()
        self.assertEqual(len(self.runpod.started), 2)
        self.assertEqual(self._journal(job_id)["state"], "running")

    def test_finished_worker_is_collected_without_signaling(self) -> None:
        job_id = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), []))
        self.hub.tick()
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=False, detail="completed")
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "completed")
        self.assertEqual(self.runpod.signals, [])
        self.assertEqual(self.runpod.stopped, ["runpod-1"])

    def test_a_provider_exception_does_not_kill_the_tick(self) -> None:
        job_id = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), [Migrate(Modal(gpu="A10G"))]))
        self.hub.tick()
        self.runpod.signal_error = RuntimeError("Task has already finished with status success")
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=True, headroom_usd=0, burn_usd_per_hour=1)
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["handle"]["provider"], "modal")

    def test_hard_stop_without_an_ack_still_releases_the_provider(self) -> None:
        self.runpod.ignore_signal = True
        job_id = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), [Migrate(Modal(gpu="A10G"))]))
        self.hub.tick()
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=True, headroom_usd=0, burn_usd_per_hour=1)
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["handle"]["provider"], "modal")
        self.assertFalse(journal["checkpoint_fresh"])
        self.assertEqual(self.runpod.stopped, ["runpod-1"])

    def test_resume_restarts_the_same_provider(self) -> None:
        job_id = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), []))
        self.hub.tick()
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=True, headroom_usd=0, burn_usd_per_hour=1)
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["state"], "stopped")
        write_command(self.root, job_id, "resume", {})
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=True, headroom_usd=10, burn_usd_per_hour=0.1)
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["state"], "running")
        self.assertEqual(len(self.runpod.started), 2)

    def test_laptop_heartbeat_loss_moves_the_job(self) -> None:
        write_heartbeat(self.root)
        job_id = self._submit(_job(self.script, Laptop(), [Migrate(RunPod(gpu="NVIDIA GeForce RTX 4090"))]))
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["handle"]["provider"], "laptop")
        from datetime import datetime, timezone

        write_heartbeat(self.root, now=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["handle"]["provider"], "runpod")
        self.assertIn(journal["reason"], ("", "laptop_heartbeat_lost"))
        self.assertTrue(any(item["reason"] == "laptop_heartbeat_lost" for item in journal["history"]))

    def test_a_failed_script_stops_instead_of_spending_the_next_provider(self) -> None:
        job_id = self._submit(
            _job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), [Migrate(Modal(gpu="A10G"))])
        )
        self.hub.tick()
        self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=False, detail="worker_failed")
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual(journal["reason"], "worker_failed")
        self.assertEqual(self.modal.started, [])
        self.assertEqual(self.runpod.stopped, ["runpod-1"])

    def test_repeated_probe_failures_release_the_provider(self) -> None:
        job_id = self._submit(
            _job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), [Migrate(Modal(gpu="A10G"))])
        )
        self.hub.probe_failures = 2
        self.hub.tick()
        self.runpod.probe_error = ProviderError("api down")
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["state"], "running")
        self.assertEqual(self._journal(job_id)["handle"]["provider"], "runpod")
        self.hub.tick()
        self.assertEqual(self._journal(job_id)["handle"]["provider"], "modal")
        self.assertEqual(self.runpod.stopped, ["runpod-1"])

    def test_capacity_leaves_the_extra_job_queued(self) -> None:
        self.hub.max_total = 1
        first = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), []))
        second = self._submit(_job(self.script, RunPod(gpu="NVIDIA GeForce RTX 4090"), []))
        self.hub.tick()
        states = {job_id: self._journal(job_id)["state"] for job_id in (first, second)}
        self.assertEqual(sorted(states.values()), ["queued", "running"])
        queued = next(job_id for job_id, state in states.items() if state == "queued")
        self.assertEqual(self._journal(queued)["reason"], "at_capacity")
        self.assertEqual(len(self.runpod.started), 1)

    def test_low_disk_keeps_the_bundle_and_rejects_the_job(self) -> None:
        job_id = "job-disk"
        inbox = self.root / "inbox" / job_id
        (inbox / "bundle").mkdir(parents=True)
        (inbox / "bundle" / "train.py").write_text("print(1)\n", encoding="utf-8")
        write_json(
            inbox / "job.json",
            {
                "id": job_id,
                "script": "train.py",
                "targets": [{"type": "runpod", "gpu": "NVIDIA GeForce RTX 4090"}],
                "confirms": [False],
                "limits": _limits().to_dict(),
                "datasets": [],
            },
        )
        self.hub.disk.reserve_bytes = 10**18
        self.hub.tick()
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "failed")
        self.assertEqual(journal["reason"], "disk_watermark")
        self.assertTrue((self.root / "jobs" / job_id / "bundle" / "train.py").exists())

    def test_an_unsafe_job_id_never_leaves_the_inbox(self) -> None:
        inbox = self.root / "inbox" / "escape"
        inbox.mkdir(parents=True)
        write_json(inbox / "job.json", {"id": "../outside", "script": "train.py", "targets": [{}], "confirms": [False]})
        self.hub.tick()
        self.assertTrue((inbox / "rejected").exists())
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse((self.root / "jobs" / "outside").exists())

    def test_hub_toml_sets_the_caps(self) -> None:
        (self.root / "hub.toml").write_text(
            "max_running = 3\nprobe_failures = 4\n\n[providers.runpod]\nmax_running = 1\n",
            encoding="utf-8",
        )
        hub = Hub(self.root, providers={"runpod": self.runpod}, reserve_bytes=1024 * 1024)
        self.assertEqual(hub.max_total, 3)
        self.assertEqual(hub.max_running["runpod"], 1)
        self.assertEqual(hub.probe_failures, 4)

    def test_parking_runs_the_notify_command_once(self) -> None:
        marker = self.root / "noted"
        script = self.root / "note.py"
        script.write_text(
            "import os\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text("
            "os.environ['RESALLOC_JOB_ID'] + '\\n' + os.environ.get('RESALLOC_NOTIFY_PROVIDER', '') + '\\n',"
            " encoding='utf-8')\n",
            encoding="utf-8",
        )
        command = shlex.join([sys.executable, str(script)])
        (self.root / "hub.toml").write_text(
            "notify_command = " + json.dumps(command) + "\n",
            encoding="utf-8",
        )
        previous_url = os.environ.pop("RESALLOC_NOTIFY_URL", None)
        previous_command = os.environ.pop("RESALLOC_NOTIFY_COMMAND", None)
        try:
            job_id = self._submit(
                _job(
                    self.script,
                    RunPod(gpu="NVIDIA GeForce RTX 4090"),
                    [Migrate(Modal(gpu="A10G"), confirm=True)],
                )
            )
            self.hub.tick()
            self.runpod.snapshot = AccountSnapshot(provider="runpod", alive=True, headroom_usd=0, burn_usd_per_hour=1)
            self.hub.tick()
            self.hub.tick()
        finally:
            if previous_url is not None:
                os.environ["RESALLOC_NOTIFY_URL"] = previous_url
            if previous_command is not None:
                os.environ["RESALLOC_NOTIFY_COMMAND"] = previous_command
        journal = self._journal(job_id)
        self.assertEqual(journal["state"], "awaiting_confirm")
        self.assertTrue(journal["confirm_notified"])
        self.assertEqual(marker.read_text(encoding="utf-8"), f"{job_id}\nmodal\n")

    def test_notice_is_skipped_when_nothing_is_configured(self) -> None:
        previous_url = os.environ.pop("RESALLOC_NOTIFY_URL", None)
        previous_command = os.environ.pop("RESALLOC_NOTIFY_COMMAND", None)
        try:
            self.assertFalse(send_confirm_notice(self.root, {"id": "job"}))
            (self.root / "hub.toml").write_text('notify_url = "file:///tmp/nope"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                send_confirm_notice(self.root, {"id": "job", "reason": "next"}, "modal")
        finally:
            if previous_url is not None:
                os.environ["RESALLOC_NOTIFY_URL"] = previous_url
            if previous_command is not None:
                os.environ["RESALLOC_NOTIFY_COMMAND"] = previous_command

    def test_offline_laptop_is_refused_at_submit(self) -> None:
        from resalloc.errors import LaptopOfflineError

        with self.assertRaises(LaptopOfflineError):
            self._submit(_job(self.script, Laptop(), []))

    def _journal(self, job_id: str) -> dict:
        return read_json(self.root / "jobs" / job_id / "journal.json")


if __name__ == "__main__":
    unittest.main()
