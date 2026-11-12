import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
QT = importlib.util.find_spec("PySide6") is not None
if QT:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from manager.qt_sizes import SizeTab
    from manager.project_sizes import scan_roots


@unittest.skipUnless(QT, "PySide6 unavailable")
class SizeTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_report_default_and_readonly_cannot_select_or_delete(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            cache = root / "project/node_modules"
            cache.mkdir(parents=True)
            (cache / "fixture.js").write_text("fixture", encoding="utf8")
            projects, errors = scan_roots([root])
            for writable in (True, False):
                tab = SizeTab(writable)
                tab.loaded(projects, errors)
                child = tab.tree.topLevelItem(0).child(0)
                self.assertEqual(child.checkState(0), Qt.CheckState.Unchecked)
                self.assertEqual(bool(child.flags() & Qt.ItemFlag.ItemIsUserCheckable), writable)
                self.assertEqual(tab.delete_button.isEnabled(), writable)
                with patch("manager.qt_sizes.recycle_candidate") as recycle:
                    tab.review()  # no checkbox selected: no confirmation or deletion
                    recycle.assert_not_called()
                tab.close()


if __name__ == "__main__":
    unittest.main()
