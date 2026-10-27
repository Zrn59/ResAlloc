import threading
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.io import dump_dt, utcnow, write_json
from resalloc.policy import Signal
from resalloc.worker import Worker


class WorkerTest(unittest.TestCase):
    def test_pulse_continues_and_then_acks_a_stop(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved: list[str] = []
            worker = Worker(root)
            worker.on_stop(lambda: saved.append("flushed"))
            self.assertEqual(worker.load(), {})
            self.assertIs(worker.pulse(), Signal.CONTINUE)
            self.assertEqual(saved, [])

            write_json(root / "control.json", {"command": "checkpoint_and_migrate", "reason": "budget"})
            worker.save({"epoch": 4})
            signal = worker.pulse()
            self.assertIs(signal, Signal.CHECKPOINT_AND_MIGRATE)
            self.assertEqual(saved, ["flushed"])
            self.assertEqual(worker.load()["epoch"], 4)
            self.assertTrue((root / "ack.json").exists())

    def test_hard_lease_stops_even_without_a_control_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = utcnow()
            write_json(
                root / "lease.json",
                {
                    "soft_at": dump_dt(now - timedelta(minutes=2)),
                    "hard_at": dump_dt(now - timedelta(seconds=1)),
                },
            )
            self.assertIs(Worker(root).pulse(), Signal.STOP)

    def test_iter_ready_notices_a_dataset_that_appears_later(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = Worker(root / "runtime")
            found: list[str] = []

            def collect() -> None:
                found.extend(path.name for path in worker.iter_ready("frames", poll=0.01))

            thread = threading.Thread(target=collect)
            thread.start()
            data = root / "datasets" / "frames"
            data.mkdir(parents=True)
            (data / "a.bin").write_bytes(b"a")
            (data / "a.bin.done").write_text("ok\n", encoding="utf-8")
            (data / "COMPLETE").write_text("ok\n", encoding="utf-8")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(found, ["a.bin"])

    def test_artifact_path_stays_inside_the_job(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = Worker(root).path("model.pt")
            target.write_bytes(b"weights")
            self.assertEqual(target.parent, root / "artifacts")


if __name__ == "__main__":
    unittest.main()
