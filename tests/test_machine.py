import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.errors import ProviderError
from resalloc.models import Limits
from resalloc.providers.base import JobLaunch
from resalloc.providers.machine import MachineAdapter


class MachineTest(unittest.TestCase):
    def test_start_copies_the_job_and_stop_only_kills_the_process(self) -> None:
        commands: list[str] = []
        synced: list[str] = []

        def ssh(endpoint, command):
            self.assertEqual(endpoint["host"], "lab.example")
            self.assertEqual(endpoint["identity"], "/keys/lab")
            commands.append(command)
            if command.startswith("cat "):
                raise ProviderError("No such file")
            return "ok\n"

        def sync(endpoint, files, root):
            synced.append(root)
            self.assertTrue(files)

        adapter = MachineAdapter(ssh_runner=ssh, file_sync=sync, file_pull=lambda *args: None)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "bundle" / "train.py"
            script.parent.mkdir()
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
                limits=Limits(),
                settings={
                    "type": "machine",
                    "host": "lab.example",
                    "user": "trainer",
                    "identity": "/keys/lab",
                    "python": "python3",
                },
                args=[],
            )
            snapshot = adapter.preflight(launch)
            self.assertTrue(snapshot.alive)
            self.assertEqual(snapshot.hourly_price_usd, 0.0)
            handle = adapter.start(launch)
            adapter.stop(handle)
        self.assertEqual(synced, ["/var/tmp/resalloc/job-1"])
        self.assertTrue(any("python3" in command and "/var/tmp/resalloc/job-1" in command for command in commands))
        self.assertTrue(any(command.startswith("if [ -f ") and "poweroff" not in command for command in commands))
        self.assertFalse(any("shutdown" in command or "poweroff" in command for command in commands))

    def test_a_refused_connection_is_an_offline_machine(self) -> None:
        def ssh(endpoint, command):
            del endpoint, command
            raise ProviderError("ssh: connect to host lab port 22: Connection refused")

        adapter = MachineAdapter(ssh_runner=ssh)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "train.py"
            script.write_text("print(1)\n", encoding="utf-8")
            launch = JobLaunch(
                job_id="job-1",
                job_dir=root,
                script=script,
                requirements=None,
                datasets=[],
                checkpoint_dir=root / "checkpoints",
                runtime_dir=root / "runtime",
                limits=Limits(),
                settings={"type": "machine", "host": "lab.example"},
                args=[],
            )
            snapshot = adapter.preflight(launch)
        self.assertFalse(snapshot.alive)
        self.assertEqual(snapshot.detail, "workload_stopped")


if __name__ == "__main__":
    unittest.main()
