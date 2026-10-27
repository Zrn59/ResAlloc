import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.config import Config
from resalloc.setup import (
    _install_script,
    checks_from_probe,
    extras_for_secrets,
    format_journal,
    hub_unit,
    init_config,
    install_local,
    local_checks,
    parse_edits,
    parse_extras,
    secret_names,
    wrapper_script,
)
from resalloc.transport import remote_shell
from resalloc.worker import Worker


class SetupTest(unittest.TestCase):
    def test_remote_shell_puts_the_wrapper_on_path(self) -> None:
        command = remote_shell("/home/ubuntu/resalloc", ["resalloc", "cache", "has", "abc"])
        self.assertIn("PATH=$HOME/.local/bin:$PATH", command)
        self.assertIn("RESALLOC_SSH_HOST=", command)
        self.assertIn("resalloc cache has abc", command.replace("'", ""))

    def test_edits_keep_gpu_names_as_text(self) -> None:
        changes = parse_edits("gpu=NVIDIA GeForce RTX 3090, spot=true, disk_gb=40")
        self.assertEqual(changes["gpu"], "NVIDIA GeForce RTX 3090")
        self.assertIs(changes["spot"], True)
        self.assertEqual(changes["disk_gb"], 40)

    def test_status_shows_the_parked_target(self) -> None:
        text = format_journal(
            {
                "id": "job-1",
                "state": "awaiting_confirm",
                "reason": "budget_headroom",
                "step": 2,
                "checkpoint_fresh": True,
                "handle": None,
                "pending_index": 1,
                "targets": [{"type": "runpod"}, {"type": "modal", "gpu": "A10G"}],
                "last_error": "",
            }
        )
        self.assertIn("awaiting_confirm", text)
        self.assertIn("next=modal gpu=A10G", text)

    def test_secret_names_skip_comments_and_empty_values(self) -> None:
        self.assertEqual(secret_names("# RUNPOD_API_KEY=\nRUNPOD_API_KEY=abc\nMODAL_TOKEN_ID=\n"), ["RUNPOD_API_KEY"])

    def test_unit_points_at_the_hub_root(self) -> None:
        text = hub_unit("/home/ubuntu/.local/bin/resalloc", "/home/ubuntu/resalloc")
        self.assertIn("ExecStart=/home/ubuntu/.local/bin/resalloc hub run", text)
        self.assertIn("EnvironmentFile=-/home/ubuntu/resalloc/secrets.env", text)
        self.assertIn("PYTHONPATH=/home/ubuntu/.local/share/resalloc", wrapper_script("/home/ubuntu/.local/share/resalloc"))
        self.assertIn("command -v python3", wrapper_script("/share"))

    def test_extras_follow_the_keys_that_are_already_present(self) -> None:
        self.assertEqual(extras_for_secrets(""), [])
        self.assertEqual(extras_for_secrets("MODAL_TOKEN_ID=abc\n"), ["modal"])
        self.assertEqual(parse_extras("all", ""), ["modal", "kaggle"])
        plain = _install_script("/share", "/bin/resalloc", "/hub", [])
        self.assertNotIn("modal>=", plain)
        self.assertIn("no user systemd", plain)
        cloud = _install_script("/share", "/bin/resalloc", "/hub", ["modal", "kaggle"])
        self.assertIn("modal>=0.73", cloud)
        self.assertIn("kaggle>=1.6", cloud)

    def test_init_and_local_install_do_not_need_ssh(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / "config.toml"
            hub = root / "hub"
            self.assertIn("wrote", init_config(config_file, hub, None))
            text = config_file.read_text(encoding="utf-8")
            self.assertIn(str(hub), text)
            self.assertNotIn('\nssh_host = "', "\n" + text)
            self.assertIn("kept", init_config(config_file, hub, "other", force=False))
            from resalloc.config import Config

            notes = install_local(Config(hub_root=hub), start=False)
            self.assertTrue((hub / "secrets.env").is_file())
            self.assertTrue(any("resalloc hub run" in note for note in notes))
            rows = {row.name: row for row in local_checks(Config(hub_root=hub), systemd_user=False)}
            self.assertTrue(rows["package"].ok)
            self.assertTrue(rows["hub"].ok)
            self.assertNotIn("ssh", rows)

    def test_a_hub_without_systemd_is_still_healthy(self) -> None:
        config = Config(hub_root=Path("/tmp/resalloc"), ssh_host="hub")
        rows = {row.name: row for row in checks_from_probe(config, {"free": 10**12, "reserve": 100, "import_ok": True, "resalloc_bin": "/bin/resalloc", "systemd_user": False, "secrets": []}, "")}
        self.assertTrue(rows["hub"].ok)
        self.assertTrue(rows["disk"].ok)
        self.assertNotIn("linger", rows)

    def test_hub_install_refuses_a_remote_config(self) -> None:
        from resalloc.cli import main

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            remote_root = root / "not-on-this-machine"
            config.write_text(f'ssh_host = "hub"\nhub_root = "{remote_root}"\n', encoding="utf-8")
            previous = os.environ.get("RESALLOC_CONFIG")
            os.environ["RESALLOC_CONFIG"] = str(config)
            try:
                code = main(["hub", "install", "--no-start"])
            finally:
                if previous is None:
                    os.environ.pop("RESALLOC_CONFIG", None)
                else:
                    os.environ["RESALLOC_CONFIG"] = previous
            self.assertEqual(code, 1)
            self.assertFalse(remote_root.exists())

    def test_probe_without_ssh_is_a_failed_check(self) -> None:
        config = Config(hub_root=Path("/home/ubuntu/resalloc"), ssh_host="zrnhost")
        rows = checks_from_probe(config, None, "connection refused")
        self.assertFalse(any(row.ok and row.name == "ssh" for row in rows))

    def test_save_runs_the_after_save_hook(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "pushed"
            previous = os.environ.get("RESALLOC_AFTER_SAVE")
            os.environ["RESALLOC_AFTER_SAVE"] = f"touch {marker}"
            try:
                Worker(root / "runtime").save({"epoch": 1})
            finally:
                if previous is None:
                    os.environ.pop("RESALLOC_AFTER_SAVE", None)
                else:
                    os.environ["RESALLOC_AFTER_SAVE"] = previous
            self.assertTrue(marker.exists())
            self.assertEqual((root / "runtime" / "state.json").exists(), True)


if __name__ == "__main__":
    unittest.main()
