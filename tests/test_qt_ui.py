"""Offscreen UI checks. Never press a delete button or delete real app records."""
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if QT_AVAILABLE:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QPlainTextEdit
    from PySide6.QtGui import QFontDatabase
    from manager.qt_ui import App
    from manager.qt_dialogs import BackupDialog, ResultDialog, ReviewDialog
    from manager.qt_models import SORT
    from manager.workspaces import key as path_key


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class QtSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, condition, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Qt background task timed out")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-qt-smoke-")
        self.root = Path(self.temp.name)
        self.project = self.root / "source"
        (self.project / "child").mkdir(parents=True)
        (self.project / "source.py").write_text("preserve source", encoding="utf8")
        self.claude = self.root / "claude"
        folder = self.claude / "projects" / "fixture"
        folder.mkdir(parents=True)
        for title, cwd in [("Fixture root chat", self.project), ("Fixture child chat", self.project / "child")]:
            sid = str(uuid.uuid4())
            (folder / (sid + ".jsonl")).write_text(json.dumps({"cwd": str(cwd), "type": "user", "message": {"content": title}}) + "\n", encoding="utf8")
        (self.claude / "cache").mkdir()
        (self.claude / "cache" / "temp.bin").write_bytes(b"cache")
        self.gemini = self.root / "gemini"
        gem = self.gemini / "tmp" / "hash"
        (gem / "chats").mkdir(parents=True)
        (gem / ".project_root").write_text(str(self.project / "child"), encoding="utf8")
        (gem / "chats" / "session-fixture.json").write_text(json.dumps({"sessionId": str(uuid.uuid4()), "messages": [{"type": "user", "content": "Fixture Gemini child"}]}), encoding="utf8")
        self.patches = [patch("manager.qt_logic.running_processes", return_value=[]),
                        patch("manager.qt_logic.codex_cli", return_value=("fixture-codex.exe", "0.0.0"))]
        for p in self.patches:
            p.start()
        self.window = App(roots=[("Claude", self.claude), ("Gemini", self.gemini)], auto_refresh=False)
        self.window.backup_dir = self.root / "backups"
        self.window.backup_dir.mkdir()
        self.window.show()
        self.window.refresh()
        self.wait(lambda: not self.window.busy and bool(self.window.entries))
        self.chats = [e for e in self.window.entries if e.kind == "conversation"]
        self.group = next(e.group for e in self.chats if Path(e.project) == self.project)

    def tearDown(self):
        self.wait(lambda: not self.window.busy)
        for dialog in list(self.window.dialogs):
            dialog.reject()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_fake_profile_window_and_project_checkbox(self):
        self.assertEqual(self.window.model.columnCount(), 7)
        self.assertEqual(len(self.chats), 3)
        self.assertTrue(QFontDatabase.families())
        first = self.window.model.group_items[self.group][0]
        first.setCheckState(Qt.CheckState.Checked)
        self.assertTrue(all(e.key in self.window.checked for e in self.window.groups[self.group]))
        self.assertFalse(self.window.tree.grab().isNull())
        self.assertTrue((self.project / "source.py").exists())

    def test_work_folder_selects_all_apps_and_descendants_then_unchecks(self):
        folder = self.window.model.group_items[self.group][6]
        folder.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.window.checked, {e.key for e in self.chats})
        self.assertIn(path_key(self.project), self.window.workspaces)
        gemini = next(e for e in self.chats if e.provider == "Gemini")
        self.window.model.record_items[gemini.key][0].setCheckState(Qt.CheckState.Unchecked)
        self.assertNotIn(path_key(self.project), self.window.workspaces)
        self.assertEqual(folder.checkState(), Qt.CheckState.Unchecked)

    def test_blocked_and_running_entries_cannot_be_checked(self):
        blocked = next(e for e in self.chats if e.provider == "Claude")
        blocked.blocked = "fixture blocked"
        self.window.running = {"Gemini"}
        self.window.render()
        for e in [blocked, next(e for e in self.chats if e.provider == "Gemini")]:
            cell = self.window.model.record_items[e.key][0]
            self.assertFalse(cell.flags() & Qt.ItemFlag.ItemIsUserCheckable)
            cell.setCheckState(Qt.CheckState.Checked)
            self.assertNotIn(e.key, self.window.checked)
            self.assertEqual(cell.checkState(), Qt.CheckState.Unchecked)

    def test_protected_folder_shows_reason_without_marking(self):
        self.window.protected[path_key(self.project)] = "fixture protected folder"
        self.window.render()
        self.window.toggle_workspace(self.group)
        self.assertFalse(self.window.workspaces)
        self.assertIn("fixture protected folder", self.window.detail_hint.text())
        self.assertEqual(self.window.model.group_items[self.group][6].text(), "보호됨")

    def test_readonly_blocks_record_and_backup_deletion(self):
        self.window.allow_delete = False
        self.window.set_records_checked(self.chats, True)
        self.window.update_actions()
        self.assertFalse(self.window.review_button.isEnabled())
        self.assertFalse(self.window.actions["review"].isEnabled())
        with patch("manager.operations.delete") as delete, patch("manager.operations.remove_backups") as remove:
            self.assertIsNone(self.window.review())
            self.window.submit_review(self.chats, set(), self.window.entries, {})
            dialog = self.window.manage_backups()
            self.assertFalse(dialog.remove_button.isEnabled())
            dialog.remove()  # Guard only: no button click, no backend call.
            delete.assert_not_called()
            remove.assert_not_called()

    def test_confirmation_two_checks_and_cache_default(self):
        plan = [{"path": str(self.project), "size": 13, "files": 1, "warnings": ["fixture git warning"], "problem": ""}]
        dialog = ReviewDialog(self.window.entries, plan, self.window.backup_dir, parent=self.window)
        self.assertFalse(dialog.cache_backup.isChecked())
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.agreed.setChecked(True)
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.moved.setChecked(True)
        self.assertTrue(dialog.confirm_button.isEnabled())
        dialog.destination.clear()
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.reject()

    def test_scan_and_workspace_plan_run_off_gui_thread(self):
        self.window.toggle_workspace(self.group)
        threads = []
        def plan(*_):
            threads.append(threading.get_ident())
            return [{"path": str(self.project), "size": 0, "files": 0, "warnings": [], "problem": ""}]
        with patch("manager.qt_logic.plan_workspaces", side_effect=plan):
            self.window.review()
            self.wait(lambda: not self.window.busy and any(isinstance(d, ReviewDialog) for d in self.window.dialogs))
        self.assertNotEqual(threads[0], threading.get_ident())
        with patch("manager.qt_logic.scan", wraps=__import__("manager.providers", fromlist=["scan"]).scan) as scan:
            self.window.refresh()
            self.wait(lambda: not self.window.busy)
            scan.assert_called_once()

    def test_sort_search_and_real_profile_path_text(self):
        self.window.tree.sortByColumn(3, Qt.SortOrder.DescendingOrder)
        values = [self.window.proxy.index(i, 3).data(SORT) for i in range(self.window.proxy.rowCount())]
        self.assertEqual(values, sorted(values, reverse=True))
        self.window.search.setText("Fixture Gemini child")
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.search.clear()
        e = self.chats[0]
        source = self.window.model.record_items[e.key][0].index()
        self.window.show_details(self.window.proxy.mapFromSource(source))
        self.assertEqual(self.window.detail_store.text(), str(e.home))

    def test_result_dialog_contains_failures_and_notes(self):
        dialog = ResultDialog((self.root / "fixture.zip", [self.chats[0]], [("fixture failure", "reason")], ["fixture note"]), self.window)
        text = dialog.findChild(QPlainTextEdit).toPlainText()
        self.assertIn("fixture failure", text)
        self.assertIn("fixture note", text)
        dialog.reject()

    def test_controller_passes_existing_delete_api_and_refreshes(self):
        # The backend is mocked. No delete button is pressed and no file is removed.
        selected = list(self.chats)
        groups = {e.group for e in selected}
        everything = list(self.window.entries)
        self.window.set_records_checked(selected, True)
        payload = {"destination": self.window.backup_dir, "include_caches": False, "workspaces": []}
        result = (self.root / "fixture.zip", selected, [], ["fixture note"])
        with patch("manager.operations.delete", return_value=result) as backend, patch("manager.qt_logic.ResultDialog") as output:
            self.window.submit_review(selected, groups, everything, payload)
            self.wait(lambda: not self.window.busy and not self.window.checked)
            backend.assert_called_once()
            args, kwargs = backend.call_args
            self.assertEqual(args[0], selected)
            self.assertEqual(set(args[1]), {e.key for e in selected})
            self.assertEqual(args[2], self.window.backup_dir)
            self.assertEqual(args[3], groups)
            self.assertFalse(kwargs["include_caches"])
            self.assertEqual(kwargs["workspaces"], [])
            self.assertEqual(kwargs["all_entries"], everything)
            output.assert_called_once_with(result, self.window)
        self.assertTrue((self.project / "source.py").exists())

    def test_menu_actions_are_connected_to_real_ui_actions(self):
        self.window.set_records_checked(self.chats, True)
        self.window.actions["clear"].trigger()
        self.assertFalse(self.window.checked)
        with patch("manager.qt_ui.message") as information:
            self.window.actions["errors"].trigger()
            information.assert_called_once()
        self.window.actions["processes"].trigger()
        self.assertEqual(self.window.dialogs[-1].windowTitle(), "실행 중 앱")
        self.window.dialogs[-1].reject()
        self.window.actions["backups"].trigger()
        self.assertIsInstance(self.window.dialogs[-1], BackupDialog)
        self.window.dialogs[-1].reject()


class LauncherTests(unittest.TestCase):
    def test_tk_fallback_when_pyside_is_missing(self):
        import builtins
        import main
        original_import = builtins.__import__
        def without_qt(name, *args, **kwargs):
            if name.startswith("PySide6"):
                raise ImportError("fixture: no Qt")
            return original_import(name, *args, **kwargs)
        with patch("manager.ui.App") as tk_app, patch("builtins.__import__", side_effect=without_qt), contextlib.redirect_stdout(io.StringIO()) as out:
            main.run_gui(allow_delete=False)
        tk_app.assert_called_once_with(allow_delete=False)
        tk_app.return_value.mainloop.assert_called_once()
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
