import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.client import ResAlloc
from resalloc.config import resolve
from resalloc.hub import Hub
from resalloc.io import read_json
from resalloc.laptop_agent import LaptopAgent
from resalloc.models import Job, Laptop, Migrate, RunPod
from resalloc.providers.laptop import LaptopAdapter, write_heartbeat
from tests.fakes import FakeProvider


class AgentTest(unittest.TestCase):
    def test_laptop_runs_the_script_and_a_clean_exit_completes_the_job(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text(
                "from resalloc.worker import Worker\n"
                "worker = Worker()\n"
                "worker.save({'epoch': 3})\n",
                encoding="utf-8",
            )
            write_heartbeat(root)
            job_id = ResAlloc(hub_root=root, reserve_bytes=1024 * 1024).submit(
                Job(script=str(script), start=Laptop(), failover=[Migrate(RunPod(gpu="NVIDIA GeForce RTX 4090"))])
            )
            fake = FakeProvider("runpod")
            hub = Hub(
                root,
                providers={"laptop": LaptopAdapter(root), "runpod": fake},
                reserve_bytes=1024 * 1024,
            )
            hub.tick()
            agent = LaptopAgent(resolve(root, prefer_local=True))
            for _ in range(40):
                agent.once()
                finished = root / "jobs" / job_id / "runtime" / "finished.json"
                if finished.exists():
                    break
                time.sleep(0.05)
            self.assertTrue((root / "jobs" / job_id / "runtime" / "finished.json").exists())
            hub.tick()
            journal = read_json(root / "jobs" / job_id / "journal.json")
            self.assertEqual(journal["state"], "completed")
            self.assertEqual(journal["step"], 3)
            self.assertEqual(fake.started, [])


if __name__ == "__main__":
    unittest.main()
