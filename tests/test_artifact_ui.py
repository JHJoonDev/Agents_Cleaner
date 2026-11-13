"""Artifact presentation using fake entries only, never real app data."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager.model import Entry
from manager.runtime import cleanup_summary, entry_group

QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if QT_AVAILABLE:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from manager.qt_ui import App
    from manager.qt_dialogs import ReviewDialog


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class ArtifactUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = App(roots=[], auto_refresh=False)
        self.window.monitor_timer.stop()
        self.entries = []
        for tier, size in ((1, 100), (2, 200), (3, 300)):
            self.entries.append(Entry("Artifacts", self.root, f"artifact:test:{tier}",
                                      f"부산물 {tier}", str(self.root / "project"), 0,
                                      [self.root / f"target{tier}"], kind="artifact",
                                      extra={"tier": tier, "method": "delete" if tier == 1 else "recycle",
                                             "display_size": size, "idle_days": 42,
                                             "reinstall": "npm install" if tier == 2 else ""},
                                      blocked="실행 중이어서 보존" if tier == 3 else ""))
        self.window.apply_result("scan", (self.entries, [], {"Orca", "Herdr"}, [], [], "", {}, {}), None)

    def tearDown(self):
        for dialog in list(self.window.dialogs):
            dialog.reject()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_group_labels_kinds_and_blocked_checkboxes(self):
        self.assertEqual(len(self.window.groups), 3)
        for e in self.entries:
            group = entry_group(e)
            self.assertIn("작업 부산물", self.window.model.group_items[group][0].text())
            self.assertIn({1: "1 바로 정리", 2: "2 확인 후 정리", 3: "3 개수 제한"}[e.extra["tier"]], group[2])
            row = self.window.model.record_items[e.key]
            self.assertEqual(row[2].text(), "부산물")
            self.assertEqual(bool(row[0].flags() & Qt.ItemFlag.ItemIsUserCheckable), not bool(e.blocked))
            self.assertFalse(self.window.model.group_items[group][6].isCheckable())
        self.window.select_all()
        self.assertEqual(self.window.checked, {e.key for e in self.entries if not e.blocked})
        blocked = self.entries[-1]
        self.window.model.record_items[blocked.key][0].setCheckState(Qt.CheckState.Checked)
        self.assertNotIn(blocked.key, self.window.checked)

    def test_group_selection_does_not_request_project_deletion(self):
        e = self.entries[0]
        self.window.model.group_items[entry_group(e)][0].setCheckState(Qt.CheckState.Checked)
        self.assertIn(e.key, self.window.checked)
        self.assertFalse(self.window.project_groups)
        self.assertFalse(self.window.workspaces)

    def test_details_include_path_size_tier_method_idle_reinstall_and_block(self):
        for e in self.entries:
            index = self.window.proxy.mapFromSource(self.window.model.record_items[e.key][0].index())
            self.window.show_details(index)
            text = self.window.detail_hint.text()
            for value in (str(e.files[0]), "용량:", "단계:", "42일", "차단 사유:",
                          "영구 삭제" if e.extra["method"] == "delete" else "휴지통",
                          e.extra["reinstall"] or "해당 없음", e.blocked or "없음"):
                self.assertIn(value, text)

    def test_confirmation_separates_permanent_and_recycle_counts_and_bytes(self):
        dialog = ReviewDialog(self.entries[:2], [], self.root)
        try:
            self.assertEqual(dialog.method_summary.text(), "영구 삭제: 1개 · 100.0 B\n휴지통 이동: 1개 · 200.0 B")
            self.assertFalse(dialog.confirm_button.isEnabled())
            dialog.agreed.setChecked(True)
            self.assertTrue(dialog.confirm_button.isEnabled())
        finally:
            dialog.reject()
            dialog.deleteLater()

    def test_read_only_stays_read_only(self):
        self.window.allow_delete = False
        self.window.select_all()
        self.window.update_actions()
        self.assertFalse(self.window.review_button.isEnabled())
        self.assertIsNone(self.window.review())
        dialog = ReviewDialog(self.entries[:2], [], self.root, allow_delete=False)
        try:
            dialog.agreed.setChecked(True)
            self.assertFalse(dialog.confirm_button.isEnabled())
        finally:
            dialog.reject()
            dialog.deleteLater()

    def test_summary_preserves_existing_record_methods_and_counts_workspaces(self):
        codex = Entry("Codex", self.root, "id", "record", "project", 0, extra={"display_size": 50})
        cache = Entry("Codex", self.root, "cache", "cache", "project", 0, kind="cache", extra={"display_size": 25})
        plan = [{"problem": "", "size": 75}, {"problem": "blocked", "size": 999}]
        self.assertEqual(cleanup_summary([codex, cache], plan),
                         "영구 삭제: 1개 · 50.0 B\n휴지통 이동: 2개 · 100.0 B")
