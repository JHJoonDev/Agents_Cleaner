"""Offscreen regressions using temporary data and mocked readers only."""
import importlib.util
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if QT_AVAILABLE:
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    from manager.model import Entry
    from manager.qt_dialogs import BackupDialog, ResultDialog, ReviewDialog
    from manager.qt_ui import App
    from manager.workspaces import key as path_key


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class StartupPerfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleaner-startup-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.window = App(roots=[], allow_delete=False, auto_refresh=False)
        self.window.backup_dir = self.root / "backups"
        self.dialogs = []

    def wait(self, condition, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.001)
        self.fail("fixture background task did not finish")

    def tearDown(self):
        self.wait(lambda: not self.window.busy)
        for dialog in self.dialogs:
            dialog.reject()
            dialog.deleteLater()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def entries(self, count, project=None):
        return [Entry("Claude", self.root / "claude", str(i), f"fixture {i}",
                      str(project or self.project), 1700000000 + i,
                      extra={"display_size": 128}) for i in range(count)]

    def populate(self, entries):
        self.window.finished("scan", (entries, [], set(), [], [], "", {}, {}), None)

    def test_repeated_and_missing_workspace_paths_are_checked_once_per_refresh(self):
        entries = self.entries(400) + self.entries(100, self.root / "missing")
        with patch("manager.qt_logic.scan", return_value=(entries, [])), \
             patch("manager.qt_logic.running_processes", return_value=[]), \
             patch("manager.qt_logic.codex_cli", return_value=("fixture", "v0")), \
             patch("manager.qt_logic.ai_homes", return_value=set()), \
             patch("manager.qt_logic.fixed_problem", return_value="") as protection, \
             patch.object(Path, "is_dir", autospec=True,
                          side_effect=lambda p: p == self.project) as is_dir:
            self.window.refresh()
            self.wait(lambda: not self.window.busy)
            self.assertEqual(is_dir.call_count, 2)
            protection.assert_called_once_with(str(self.project), set())
            self.assertEqual(self.window.dirs, {path_key(self.project): str(self.project)})

    def test_single_selection_only_repaints_changed_record(self):
        entries = self.entries(2500)
        self.populate(entries)
        from manager.qt_models import mark_row
        with patch("manager.qt_ui.mark_row", wraps=mark_row) as paint:
            self.window.set_records_checked([entries[0]], True)
            self.assertEqual(paint.call_count, 1)
            self.window.set_records_checked([entries[0]], True)
            self.assertEqual(paint.call_count, 1)
            self.window.clear()
            self.assertEqual(paint.call_count, 2)
        self.assertFalse(self.window.checked)

    def test_programmatic_check_of_blocked_row_is_reverted_without_full_repaint(self):
        entries = self.entries(200)
        entries[0].blocked = "fixture protection"
        self.populate(entries)
        cell = self.window.model.record_items[entries[0].key][0]
        cell.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(cell.checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(self.window.checked)

    def test_partial_scan_blocks_project_and_workspace_but_allows_individual_record(self):
        entries = self.entries(2)
        dirs = {path_key(self.project): str(self.project)}
        self.window.finished("scan", (entries, ["fixture corrupt sibling"], set(), [], [], "", dirs, {}), None)
        group = entries[0].group
        row = self.window.model.group_items[group]
        self.assertFalse(row[0].flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertFalse(row[6].isCheckable())
        self.window.set_records_checked(entries, True, group=group)
        self.window.toggle_workspace(group)
        self.assertFalse(self.window.checked)
        self.assertFalse(self.window.workspaces)
        self.assertFalse(self.window.project_groups)
        self.window.set_records_checked([entries[0]], True)
        self.assertEqual(self.window.checked, {entries[0].key})

    def test_partial_scan_is_rechecked_at_review_and_submit_boundary(self):
        from manager.qt_logic import Logic
        # Exercise controller guards without creating any writable GUI.
        fake = SimpleNamespace(busy=False, allow_delete=True, errors=["fixture corrupt"],
                               workspaces={"fixture"}, project_groups={"fixture"})
        with patch("manager.qt_logic.message") as notice, \
             patch("manager.qt_logic.operations.delete") as delete:
            Logic.review(fake)
            Logic.submit_review(fake, [], {"fixture"}, [], {"workspaces": []})
            Logic.submit_review(fake, [], set(), [], {"workspaces": [self.root]})
            self.assertEqual(notice.call_count, 3)
            delete.assert_not_called()

    def test_failed_refresh_invalidates_previous_project_selection_until_success(self):
        entries = self.entries(2)
        self.populate(entries)
        self.window.set_records_checked(entries, True, group=entries[0].group)
        with patch("manager.qt_logic.scan", side_effect=RuntimeError("fixture scan failed")), \
             patch("manager.qt_logic.message"):
            self.window.refresh()
            self.wait(lambda: not self.window.busy)
        self.assertIn("fixture scan failed", self.window.errors)
        self.assertIn(entries[0].group, self.window.project_groups)
        self.window.toggle_workspace(entries[0].group)
        self.assertFalse(self.window.workspaces)
        self.populate(entries)
        self.assertFalse(self.window.errors)
        self.assertFalse(self.window.project_groups)

    def test_search_keeps_sibling_records_visible_and_selection_does_not_refilter(self):
        entries = self.entries(100)
        self.populate(entries)
        self.window.search.setText("fixture 99")
        parent = self.window.proxy.index(0, 0)
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.assertEqual(self.window.proxy.rowCount(parent), 100)
        self.window.set_records_checked([entries[0]], True)
        self.assertEqual(self.window.proxy.rowCount(parent), 100)
        self.assertEqual(self.window.checked, {entries[0].key})

    def test_invalid_timestamps_do_not_abort_entire_inventory(self):
        entries = self.entries(4)
        for entry, timestamp in zip(entries, [1e300, float("inf"), float("nan"), -1e300]):
            entry.updated = timestamp
        self.populate(entries)
        self.assertEqual(len(self.window.model.record_items), 4)
        self.assertTrue(all(row[4].text() == "날짜 오류"
                            for row in self.window.model.record_items.values()))

    def test_system_exit_worker_releases_busy_and_allows_retry(self):
        with patch("manager.qt_logic.message") as notice:
            self.assertTrue(self.window.launch(lambda: sys.exit("fixture exit"), "fixture"))
            self.wait(lambda: not self.window.busy)
            notice.assert_called_once()
        self.assertEqual(self.window.jobs, [])
        self.assertTrue(self.window.launch(lambda: None, "fixture"))
        self.wait(lambda: not self.window.busy)

    def test_pool_start_failure_releases_busy_and_allows_retry(self):
        pool = self.window.pool
        self.window.pool = SimpleNamespace(start=Mock(side_effect=RuntimeError("fixture pool")))
        with patch("manager.qt_logic.message"):
            self.assertFalse(self.window.launch(lambda: None, "fixture"))
        self.assertFalse(self.window.busy)
        self.assertEqual(self.window.jobs, [])
        self.assertTrue(self.window.actions["refresh"].isEnabled())
        self.window.pool = pool
        self.assertTrue(self.window.launch(lambda: None, "fixture"))
        self.wait(lambda: not self.window.busy)

    def test_result_handler_failure_releases_busy_and_can_retry(self):
        self.window.busy = True
        with patch.object(self.window, "render", side_effect=RuntimeError("fixture render")), \
             patch("manager.qt_logic.message") as notice:
            self.populate(self.entries(1))
            notice.assert_called_once()
        self.assertFalse(self.window.busy)
        self.populate(self.entries(2))
        self.assertEqual(len(self.window.model.record_items), 2)

    def test_closing_before_startup_timer_cancels_scan(self):
        other = App(roots=[], allow_delete=False)
        with patch("manager.qt_logic.scan") as scan:
            other.close()
            self.app.processEvents()
            scan.assert_not_called()
            self.assertFalse(other.launch(lambda: None, "fixture"))
        other.deleteLater()

    def test_backup_reader_is_off_gui_thread_and_keeps_timer_responsive(self):
        entered, release = threading.Event(), threading.Event()
        threads, heartbeat = [], []

        def held_reader(_):
            threads.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture reader not released")
            return []

        with patch("manager.qt_dialogs.operations.list_backups", side_effect=held_reader):
            try:
                dialog = BackupDialog(self.root, allow_delete=False, parent=self.window)
                self.dialogs.append(dialog)
                self.wait(entered.is_set)
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                self.wait(lambda: bool(heartbeat))
                self.assertTrue(dialog._reads)
                self.assertFalse(dialog.remove_button.isEnabled())
            finally:
                release.set()
                if self.dialogs:
                    self.wait(lambda: not dialog._reads)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_backup_folder_changes_coalesce_and_ignore_old_results(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def reader(folder):
            calls.append(folder)
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture reader not released")
            return [(folder / "fixture.zip", len(calls), 0)]

        with patch("manager.qt_dialogs.operations.list_backups", side_effect=reader):
            dialog = BackupDialog(self.root, allow_delete=False, parent=self.window)
            self.dialogs.append(dialog)
            try:
                self.wait(entered.is_set)
                for i in range(40):
                    dialog.folder = self.root / str(i)
                    dialog.load()
                self.assertEqual(len(dialog._reads), 1)
            finally:
                release.set()
                self.wait(lambda: not dialog._reads)
        self.assertEqual(calls, [self.root, self.root / "39"])
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        self.assertEqual(dialog.tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole),
                         str(self.root / "39" / "fixture.zip"))

    def test_review_does_not_read_backup_storage(self):
        with patch("manager.qt_dialogs.operations.list_backups", side_effect=PermissionError("fixture denied")) as reader:
            dialog = ReviewDialog(self.entries(1), [], self.root, allow_delete=False, parent=self.window)
            self.dialogs.append(dialog)
            self.assertFalse(dialog._reads)
            self.assertFalse(dialog.confirm_button.isEnabled())
            reader.assert_not_called()

    def test_destroyed_backup_dialog_does_not_receive_late_worker_result(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()

        def reader(_):
            entered.set()
            release.wait(5)
            exited.set()
            return []

        with patch("manager.qt_dialogs.operations.list_backups", side_effect=reader):
            try:
                dialog = BackupDialog(self.root, allow_delete=False, parent=self.window)
                self.wait(entered.is_set)
                dialog.reject()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            finally:
                release.set()
                self.wait(exited.is_set)
                self.app.processEvents()

    def test_parent_close_is_rejected_while_backup_mutation_is_active(self):
        self.window.show()
        # Simulate the state only; no delete action or writable dialog is used.
        self.window.dialogs.append(SimpleNamespace(_removing=True))
        with patch("manager.qt_ui.message"):
            self.assertFalse(self.window.close())
        self.window.dialogs.clear()
        self.assertTrue(self.window.close())

    def test_delete_confirmation_preflight_result_and_refresh_only_run_once(self):
        entries = self.entries(1)
        self.populate(entries)
        self.window.allow_delete = True
        self.window.set_records_checked(entries, True)
        preflight_entered, release_preflight = threading.Event(), threading.Event()
        refresh_entered, release_refresh = threading.Event(), threading.Event()
        threads, result_status = [], []
        window = self.window

        def signature(_):
            threads.append(threading.get_ident())
            preflight_entered.set()
            if not release_preflight.wait(5):
                raise RuntimeError("fixture preflight not released")
            return "fixture signature"

        def scan(_):
            refresh_entered.set()
            if not release_refresh.wait(5):
                raise RuntimeError("fixture refresh not released")
            return [], []

        class TimedResult(ResultDialog):
            def exec(self):
                result_status.append((window.status_text.text(), window.busy,
                                      window.elapsed_timer.isActive()))
                QTimer.singleShot(10, self.accept)
                return super().exec()

        with patch("manager.qt_dialogs.operations.list_backups", return_value=[]), \
             patch("manager.qt_logic.signature", side_effect=signature) as preflight, \
             patch("manager.qt_logic.operations.delete", return_value=(self.root / "fixture.zip", entries, [], [])) as delete, \
             patch("manager.qt_logic.scan", side_effect=scan) as refresh, \
             patch("manager.qt_logic.running_processes", return_value=[]), \
             patch("manager.qt_logic.codex_cli", return_value=("fixture", "v0")), \
             patch("manager.qt_logic.ai_homes", return_value=set()), \
             patch("manager.qt_logic.ResultDialog", TimedResult):
            try:
                dialog = self.window.review()
                dialog.agreed.setChecked(True)
                dialog.confirm()
                dialog.confirm()
                self.wait(preflight_entered.is_set)
                self.window.refresh()
                self.assertTrue(self.window.busy)
                delete.assert_not_called()
                release_preflight.set()
                self.wait(refresh_entered.is_set)
                self.window.refresh()
            finally:
                release_preflight.set()
                release_refresh.set()
                self.wait(lambda: not self.window.busy)
        preflight.assert_called_once()
        delete.assert_called_once()
        refresh.assert_called_once()
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertIn("정리 완료", result_status[0][0])
        self.assertEqual(result_status[0][1:], (True, False))

    def test_result_dialogs_are_released_after_repeated_deletion_results(self):
        class TimedResult(ResultDialog):
            def exec(self):
                QTimer.singleShot(0, self.accept)
                return super().exec()

        with patch.object(self.window, "refresh") as refresh, \
             patch("manager.qt_logic.ResultDialog", TimedResult):
            for _ in range(3):
                self.window.busy = True
                self.window.finished("delete", (self.root / "fixture.zip", [], [], []), None)
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(self.window.findChildren(ResultDialog))
        self.assertEqual(refresh.call_count, 3)

    def test_busy_status_shows_elapsed_time_and_stops_after_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def held():
            entered.set()
            release.wait(5)
        try:
            self.window.launch(held, "fixture")
            self.wait(entered.is_set)
            with patch("manager.qt_logic.monotonic", return_value=self.window.job_started + 61):
                self.window.update_busy_status()
            self.assertIn("61초 경과", self.window.status_text.text())
            self.assertTrue(self.window.elapsed_timer.isActive())
        finally:
            release.set()
            self.wait(lambda: not self.window.busy)
        self.assertFalse(self.window.elapsed_timer.isActive())


class TkWorkerTests(unittest.TestCase):
    def test_tk_review_backup_info_is_async_bounded_and_latest_request_wins(self):
        from manager.ui import App as TkApp
        entered, release = threading.Event(), threading.Event()
        calls, threads = [], []
        window, info = Mock(), Mock()
        window.winfo_exists.return_value = True
        fake = SimpleNamespace(events=queue.Queue(), _review_info_active=False, _review_info_pending=None)
        fake.load_review_backup_info = lambda *args: TkApp.load_review_backup_info(fake, *args)

        def reader(path):
            calls.append(path)
            threads.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            return []

        with patch("manager.ui.list_backups", side_effect=reader):
            try:
                TkApp.load_review_backup_info(fake, window, info, Path("fixture-first"))
                self.assertTrue(entered.wait(2))
                for i in range(20):
                    TkApp.load_review_backup_info(fake, window, info, Path(f"fixture-{i}"))
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            event, result, error = fake.events.get(timeout=2)
            self.assertEqual(event, "backup_info")
            TkApp.finish_review_backup_info(fake, result)
            _, result, _ = fake.events.get(timeout=2)
            TkApp.finish_review_backup_info(fake, result)
        self.assertEqual(calls, [Path("fixture-first"), Path("fixture-19")])
        self.assertTrue(all(t != threading.get_ident() for t in threads))
        info.set.assert_called_once()
        self.assertFalse(fake._review_info_active)

    def test_tk_informational_reader_completion_does_not_clear_delete_busy_state(self):
        from manager.ui import App as TkApp
        fake = SimpleNamespace(events=queue.Queue(), busy=True, _job_started=0, _job_status="fixture deletion",
                               status=Mock(), after=Mock(), poll=Mock(), delete_button=Mock(),
                               finish_review_backup_info=Mock())
        fake.events.put(("backup_info", "fixture result", None))
        TkApp.poll(fake)
        self.assertTrue(fake.busy)
        fake.finish_review_backup_info.assert_called_once_with("fixture result")
        fake.delete_button.state.assert_not_called()

    def test_tk_closed_review_window_ignores_late_backup_info(self):
        from manager.ui import App as TkApp
        window, info = Mock(), Mock()
        window.winfo_exists.return_value = False
        fake = SimpleNamespace(_review_info_active=True, _review_info_pending=None)
        TkApp.finish_review_backup_info(fake, ((window, info, Path("fixture")), "fixture result"))
        info.set.assert_not_called()
        self.assertFalse(fake._review_info_active)

    def test_tk_backup_info_thread_start_failure_releases_reader_slot(self):
        from manager.ui import App as TkApp
        window, info = Mock(), Mock()
        window.winfo_exists.return_value = True
        fake = SimpleNamespace(events=queue.Queue(), _review_info_active=False, _review_info_pending=None)
        with patch("manager.ui.threading.Thread.start", side_effect=RuntimeError("fixture reader start")):
            TkApp.load_review_backup_info(fake, window, info, Path("fixture"))
        _, result, _ = fake.events.get(timeout=2)
        TkApp.finish_review_backup_info(fake, result)
        self.assertIn("fixture reader start", info.set.call_args.args[0])
        self.assertFalse(fake._review_info_active)

    def test_tk_scan_failure_blocks_previous_project_selection(self):
        from manager.ui import App as TkApp
        fake = SimpleNamespace(events=queue.Queue(), allow_delete=True, checked=set(),
                               project_groups={"fixture"}, workspaces=set(), errors=[], status=Mock(),
                               delete_button=Mock(), after=Mock(), poll=Mock())
        fake.events.put(("scan", None, "fixture scan failed"))
        with patch("manager.ui.messagebox.showerror") as notice:
            TkApp.poll(fake)
            TkApp.review(fake)
            self.assertEqual(notice.call_count, 2)
        self.assertEqual(fake.errors, ["fixture scan failed"])

    def test_tk_poll_reschedules_after_render_failure(self):
        from manager.ui import App as TkApp
        fake = SimpleNamespace(events=queue.Queue(), allow_delete=False, checked=set(),
                               project_groups=set(), workspaces=set(), status=Mock(),
                               render=Mock(side_effect=ValueError("fixture render")),
                               after=Mock(), poll=Mock())
        fake.events.put(("scan", ([], [], set(), [], [], "", {}, {}), None))
        with patch("manager.ui.messagebox.showerror") as notice:
            TkApp.poll(fake)
            notice.assert_called_once()
        fake.after.assert_called_once_with(150, fake.poll)
        self.assertTrue(fake.errors)

    def test_tk_invalid_date_is_shown_without_exception(self):
        from manager.ui import display_date
        for timestamp in [1e300, float("nan"), float("inf")]:
            self.assertEqual(display_date(timestamp), "날짜 오류")

    def test_tk_running_record_cannot_be_checked(self):
        from manager.ui import App as TkApp
        fake_entry = SimpleNamespace(blocked="", provider="Claude", home=Path("fixture"))
        fake = SimpleNamespace(busy=False, rows={"entry": ("entry", fake_entry)},
                               tree=Mock(), running={"Claude"}, checked=set())
        fake.tree.get_children.return_value = []
        fake.is_running = lambda entry: TkApp.is_running(fake, entry)
        fake.can_select = lambda entry: TkApp.can_select(fake, entry)
        TkApp.toggle(fake, "entry")
        self.assertFalse(fake.checked)

    def test_tk_system_exit_is_reported_instead_of_leaving_busy_forever(self):
        from manager.ui import App as TkApp
        fake = SimpleNamespace(busy=False, delete_button=Mock(), events=queue.Queue())
        self.assertTrue(TkApp.launch(fake, lambda: sys.exit("fixture exit"), "scan"))
        self.assertEqual(fake.events.get(timeout=2), ("scan", None, "fixture exit"))

    def test_tk_thread_start_failure_is_reported(self):
        from manager.ui import App as TkApp
        fake = SimpleNamespace(busy=False, delete_button=Mock(), events=queue.Queue())
        with patch("manager.ui.threading.Thread.start", side_effect=RuntimeError("fixture start")):
            self.assertFalse(TkApp.launch(fake, lambda: None, "scan"))
        self.assertEqual(fake.events.get(timeout=2), ("scan", None, "fixture start"))


if __name__ == "__main__":
    unittest.main()
