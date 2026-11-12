import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager.project_sizes import scan_roots, recycle_candidate, write_report


class ProjectSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, size):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_sorted_without_double_counting_and_tox_exclusion(self):
        self.put("small/node_modules/pkg/dist/a.js", 12)
        self.put("small/main.py", 3)
        self.put("large/.venv311/lib/x.py", 25)
        self.put("ToxAuto-private/node_modules/a", 99)
        projects, errors = scan_roots([self.root])
        self.assertFalse(errors)
        self.assertEqual([p.path.name for p in projects], ["large", "small"])
        self.assertEqual([p.size for p in projects], [25, 15])
        self.assertEqual(projects[1].reclaimable, 12)
        self.assertEqual(len(projects[1].candidates), 1)

    def test_protected_descendants_and_large_files_never_selected(self):
        self.put("p/output/data.db", 20)
        self.put("p/output/.env", 2)
        self.put("p/large.py", 21)
        self.put("p/cache.bin", 22)
        self.put("p/datasets/training.bin", 30)
        projects, _ = scan_roots([self.root], large=10)
        p = projects[0]
        self.assertEqual(p.size, 95)
        self.assertEqual(p.reclaimable, 22)
        self.assertTrue(next(c for c in p.candidates if c.path.name == "output").blocked)
        self.assertTrue(next(c for c in p.candidates if c.path.name == "large.py").blocked)
        self.assertFalse(any(c.path.name == "training.bin" for c in p.candidates))

    def test_changed_candidate_refused_and_no_deletion_during_scan(self):
        file = self.put("p/node_modules/x", 2)
        projects, _ = scan_roots([self.root])
        p, c = projects[0], projects[0].candidates[0]
        self.assertTrue(file.exists())
        file.write_bytes(b"changed")
        with patch("manager.recycle.recycle") as delete:
            with self.assertRaises(ValueError):
                recycle_candidate(c, p)
            delete.assert_not_called()

    def test_confirmed_candidate_calls_existing_recycle_only(self):
        file = self.put("p/node_modules/x", 2)
        projects, _ = scan_roots([self.root])
        p, c = projects[0], projects[0].candidates[0]
        with patch("manager.recycle.recycle") as delete:
            recycle_candidate(c, p)
            delete.assert_called_once_with(c.path)
        self.assertTrue(file.exists())

    def test_link_not_followed_and_report_contains_read_failures(self):
        self.put("p/cache.bin", 8)
        link = self.root / "p/node_modules"
        try:
            os.symlink(self.root / "outside", link, target_is_directory=True)
        except OSError:
            pass  # Windows may not permit symlink creation without developer mode.
        projects, errors = scan_roots([self.root, self.root / "missing"])
        self.assertEqual(projects[0].size, 8)
        report = write_report(projects, errors, self.root / "report.md")
        self.assertIn("자동 삭제 없음", report.read_text(encoding="utf8"))
        self.assertIn("측정 누락", report.read_text(encoding="utf8"))


if __name__ == "__main__":
    unittest.main()
