import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.errors import ProviderError
from resalloc.models import Limits
from resalloc.providers.base import JobLaunch
from resalloc.providers.vast import (
    VastAdapter,
    VastApi,
    choose_offer,
    create_body,
    search_body,
    vast_gpu_name,
)


def _launch(root: Path, **settings) -> JobLaunch:
    script = root / "train.py"
    script.write_text("print(1)\n", encoding="utf-8")
    (root / "runtime").mkdir(exist_ok=True)
    base = {"type": "vast", "gpu": "NVIDIA GeForce RTX 4090", "disk_gb": 32}
    base.update(settings)
    return JobLaunch(
        job_id="job-1",
        job_dir=root,
        script=script,
        requirements=None,
        datasets=[],
        checkpoint_dir=root / "checkpoints",
        runtime_dir=root / "runtime",
        limits=Limits(vast_min_balance_usd=1, vast_max_spend_usd=5, vast_max_hourly_usd=0.5),
        settings=base,
        args=[],
    )


class VastShapeTest(unittest.TestCase):
    def test_gpu_name_and_search_prefer_a_cheap_direct_offer(self) -> None:
        self.assertEqual(vast_gpu_name("NVIDIA GeForce RTX 4090"), "RTX 4090")
        body = search_body({"gpu": "NVIDIA GeForce RTX 4090", "spot": False, "max_price": 0.5})
        self.assertEqual(body["gpu_name"], {"eq": "RTX 4090"})
        self.assertEqual(body["type"], "on-demand")
        self.assertEqual(body["direct_port_count"], {"gte": 1})
        self.assertEqual(body["dph_total"], {"lte": 0.5})
        offer = choose_offer(
            [
                {"id": 2, "dph_total": 0.4},
                {"id": 1, "dph_total": 0.2},
                {"id": 3, "dph_total": 0.9},
            ],
            max_hourly=0.5,
        )
        self.assertEqual(offer["id"], 1)
        created = create_body({"disk_gb": 32, "spot": True}, "job-1", offer)
        self.assertEqual(created["runtype"], "ssh")
        self.assertTrue(created["cancel_unavail"])
        self.assertEqual(created["price"], 0.2)


class VastStartTest(unittest.TestCase):
    def test_start_rents_the_cheap_offer_and_a_dead_instance_is_destroyed(self) -> None:
        calls: list[tuple[str, str]] = []

        def transport(method, url, body, headers):
            del headers
            calls.append((method, url))
            payload = json.loads(body) if body else None
            if url.endswith("/api/v0/bundles/"):
                self.assertEqual(payload["gpu_name"]["eq"], "RTX 4090")
                return 200, {"offers": [{"id": 7, "dph_total": 0.2, "gpu_name": "RTX 4090"}]}
            if url.endswith("/api/v0/asks/7/") and method == "PUT":
                self.assertEqual(payload["runtype"], "ssh")
                return 200, {"success": True, "new_contract": 99}
            if url.endswith("/api/v0/users/current/"):
                return 200, {"balance": 10}
            if url.endswith("/api/v0/instances/99/") and method == "GET":
                return 200, {
                    "instances": {
                        "id": 99,
                        "actual_status": "running",
                        "ssh_host": "ssh7.vast.ai",
                        "ssh_port": 2222,
                        "dph_total": 0.2,
                    }
                }
            if method == "DELETE":
                return 200, {"success": True}
            raise AssertionError((method, url))

        commands: list[str] = []
        synced: list[int] = []
        api = VastApi("test-key", transport=transport, sleep=lambda seconds: None)
        adapter = VastAdapter(
            api=api,
            ssh_runner=lambda endpoint, command: commands.append(command) or "ok",
            file_sync=lambda endpoint, files, root="/resalloc": synced.append(len(files)),
            sleep=lambda seconds: None,
        )
        with TemporaryDirectory() as temporary:
            launch = _launch(Path(temporary))
            snapshot = adapter.preflight(launch)
            self.assertAlmostEqual(snapshot.headroom_usd or 0, 5)
            self.assertEqual(snapshot.hourly_price_usd, 0.2)
            handle = adapter.start(launch)
        self.assertEqual(handle.remote_id, "99")
        self.assertEqual(handle.connection["ssh"]["host"], "ssh7.vast.ai")
        self.assertEqual(commands[0], "echo ok")
        self.assertTrue(any("finished.json" in command for command in commands))
        self.assertTrue(synced)
        self.assertFalse(any(method == "DELETE" for method, _url in calls))

        def dead(method, url, payload, headers):
            if method == "GET" and url.endswith("/instances/99/"):
                return 200, {"instances": {"id": 99, "actual_status": "exited"}}
            if method == "DELETE":
                calls.append((method, url))
                return 200, {"success": True}
            if url.endswith("/users/current/"):
                return 200, {"balance": 9}
            return transport(method, url, payload, headers)

        api.transport = dead
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ProviderError):
                adapter.start(_launch(Path(temporary)))
        self.assertTrue(any(method == "DELETE" for method, _url in calls))


if __name__ == "__main__":
    unittest.main()
