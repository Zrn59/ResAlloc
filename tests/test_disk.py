import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from resalloc.disk import DiskBudget, choose_reserve
from resalloc.errors import DiskWatermarkError
from resalloc.hashing import hash_tree
from resalloc.io import write_json


class DiskTest(unittest.TestCase):
    def test_a_small_disk_keeps_a_reserve_it_can_hold(self) -> None:
        gib = 1024**3
        self.assertEqual(choose_reserve(200 * gib), 20 * gib)
        self.assertEqual(choose_reserve(30 * gib), gib)
        self.assertLess(choose_reserve(2 * gib), 2 * gib)

    def test_hash_changes_when_content_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "set"
            data.mkdir()
            (data / "a.txt").write_text("one", encoding="utf-8")
            first = hash_tree(data)
            self.assertEqual(first, hash_tree(data))
            (data / "a.txt").write_text("two", encoding="utf-8")
            self.assertNotEqual(first, hash_tree(data))

    def test_evict_drops_the_oldest_unprotected_cache(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            budget = DiskBudget(root, reserve_bytes=1)
            old = root / "cache" / "datasets" / "old" / "payload"
            old.mkdir(parents=True)
            (old / "blob").write_bytes(b"x" * 1_000_000)
            write_json(
                old.parent / "meta.json",
                {"hash": "old", "name": "old", "bytes": 1_000_000, "last_used": "2020-01-01T00:00:00+00:00"},
            )
            kept = root / "cache" / "datasets" / "kept" / "payload"
            kept.mkdir(parents=True)
            (kept / "blob").write_bytes(b"y" * 1_000_000)
            write_json(
                kept.parent / "meta.json",
                {"hash": "kept", "name": "kept", "bytes": 1_000_000, "last_used": "2020-01-01T00:00:00+00:00"},
            )
            free = shutil.disk_usage(root).free
            budget.reserve_bytes = free + 100_000
            removed = budget.evict({"kept"})
            self.assertGreater(removed, 0)
            self.assertFalse(old.parent.exists())
            self.assertTrue(kept.parent.exists())

    def test_watermark_refuses_an_upload_and_a_checkpoint_write_does_not_ask(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            budget = DiskBudget(root, reserve_bytes=1)
            free = budget.free_bytes()
            budget.reserve_bytes = free + 10**12
            with self.assertRaises(DiskWatermarkError):
                budget.ensure_incoming(1, set())
            # Checkpoint writes do not go through ensure_incoming.
            write_json(root / "checkpoints" / "state.json", {"epoch": 1})
            self.assertTrue((root / "checkpoints" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
