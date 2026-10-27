import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.io import write_json
from resalloc.layout import Layout
from resalloc.records import read_journal
from resalloc.smoke import format_result, kaggle_token_ready, parse_providers, smoke_limits, smoke_target
from resalloc.transport import _job_file


class SmokeCommandTest(unittest.TestCase):
    def test_providers_can_be_comma_or_space_separated(self) -> None:
        self.assertEqual(parse_providers(["modal,kaggle", "modal"]), ["modal", "kaggle"])

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_providers(["paperspace"])

    def test_modal_smoke_budget_is_ten_cents(self) -> None:
        self.assertEqual(smoke_limits().modal_max_spend_usd, 0.10)
        self.assertEqual(smoke_target("modal").gpu, "T4")
        self.assertLessEqual(smoke_target("modal").timeout_hours, 0.1)

    def test_legacy_kaggle_key_is_not_the_api_token(self) -> None:
        self.assertFalse(kaggle_token_ready(["KAGGLE_USERNAME", "KAGGLE_KEY"]))
        self.assertTrue(kaggle_token_ready(["KAGGLE_API_TOKEN"]))

    def test_inbox_job_reports_queued_until_the_hub_opens_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = Layout(root)
            layout.ensure()
            write_json(layout.inbox("job-1") / "job.json", {"id": "job-1"})
            self.assertEqual(read_journal(root, "job-1")["state"], "queued")

    def test_job_file_cannot_escape_the_job_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            job = Path(temporary) / "job"
            job.mkdir()
            with self.assertRaises(ValueError):
                _job_file(job, "../secrets.env")

    def test_result_line_includes_the_device(self) -> None:
        text = format_result({"ok": True, "provider": "modal", "seconds": 40, "device": "Tesla T4", "norm": 1.2, "job_id": "job-1"})
        self.assertIn("ok", text)
        self.assertIn("Tesla T4", text)


if __name__ == "__main__":
    unittest.main()
