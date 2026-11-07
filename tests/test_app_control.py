"""Stopping apps and following their locks. Only ever stops processes these tests started themselves."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import app_control, runtime
from manager.model import Entry, signature
from manager.operations import delete
from manager.runtime import LiveApps, entry_running, lock_key, lock_state, workspace_blocker

QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None
if QT_AVAILABLE:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from manager.qt_ui import App

DESKTOP = r"C:\Program Files\WindowsApps\Claude_4.1.0.0_x64__abc\app\Claude.exe"
CLI = r"C:\Users\fixture\.local\bin\claude.exe"
HOSTED = r"C:\Users\fixture\AppData\Local\Claude-Data\claude_abc\claude-code\2.1.0\abc\claude.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def claude_row(pid, path):
    return {"app": "Claude", "name": "claude.exe", "pid": pid, "path": path}


def conversation(home, sid=None, kind="conversation", provider="Claude"):
    return Entry(provider, Path(home), sid or str(uuid.uuid4()), "fixture", "project", 0, [], kind=kind)


class LockStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lock-state-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / ".claude"
        (self.home / "sessions").mkdir(parents=True)
        patcher = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def session_file(self, pid, sid, **extra):
        (self.home / "sessions" / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "status": "idle", **extra}), encoding="utf8")

    def test_desktop_shell_alone_locks_caches_but_not_conversations(self):
        live = lock_state([claude_row(1, DESKTOP)])
        self.assertEqual(live, {"Claude"})
        self.assertFalse(entry_running(conversation(self.home), live))
        self.assertTrue(entry_running(conversation(self.home, kind="cache"), live))

    def test_session_in_use_locks_only_that_conversation(self):
        sid = str(uuid.uuid4())
        self.session_file(os.getpid(), sid)
        live = lock_state([claude_row(1, DESKTOP), claude_row(os.getpid(), HOSTED)])
        self.assertFalse(live.claude_all)
        self.assertTrue(entry_running(conversation(self.home, sid), live))
        self.assertFalse(entry_running(conversation(self.home), live))

    def test_cli_without_session_file_locks_every_claude_conversation(self):
        # A Claude started from Herdr writes to ~/.claude/projects with no session file.
        live = lock_state([claude_row(os.getpid(), CLI)])
        self.assertTrue(live.claude_all)
        self.assertTrue(entry_running(conversation(self.home), live))

    def test_unusable_session_files_fail_closed(self):
        pid = os.getpid()
        path = self.home / "sessions" / f"{pid}.json"
        for label, write in [("broken json", lambda: path.write_text("{", encoding="utf8")),
                             ("other pid", lambda: self.session_file(pid + 1, "x") or path.write_text(json.dumps({"pid": pid + 1, "sessionId": "x"}), encoding="utf8")),
                             ("no id", lambda: path.write_text(json.dumps({"pid": pid}), encoding="utf8")),
                             ("not a dict", lambda: path.write_text("[]", encoding="utf8"))]:
            with self.subTest(label):
                write()
                self.assertTrue(lock_state([claude_row(pid, CLI)]).claude_all)

    def test_session_file_older_than_process_is_from_a_reused_pid(self):
        pid = os.getpid()
        self.session_file(pid, str(uuid.uuid4()))
        os.utime(self.home / "sessions" / f"{pid}.json", (1000, 1000))
        self.assertTrue(lock_state([claude_row(pid, CLI)]).claude_all)

    def test_cli_without_psutil_fails_closed(self):
        self.session_file(os.getpid(), str(uuid.uuid4()))
        with patch.object(runtime, "psutil", None):
            self.assertTrue(lock_state([claude_row(os.getpid(), CLI)]).claude_all)

    def test_hosted_binary_is_never_mistaken_for_the_desktop_shell(self):
        self.assertTrue(runtime.is_claude_desktop({"path": DESKTOP}))
        self.assertTrue(runtime.is_claude_desktop({"path": r"C:\Users\x\AppData\Local\AnthropicClaude\app-1\claude.exe"}))
        for path in (CLI, HOSTED, "", None, r"C:\Users\x\AppData\Local\AnthropicClaude\app-1\claude-code\claude.exe"):
            self.assertFalse(runtime.is_claude_desktop({"path": path}))

    def test_idle_herdr_no_longer_locks_records_but_still_protects_folders(self):
        live = lock_state([{"app": "Herdr", "name": "herdr-core.exe", "pid": 1, "path": ""}])
        for provider in ("Codex", "Claude", "Gemini"):
            self.assertFalse(entry_running(conversation(self.home, provider=provider), live))
        self.assertTrue(entry_running(conversation(self.home, provider="Herdr", kind="cache"), live))
        self.assertTrue(workspace_blocker(live))

    def test_agent_running_inside_herdr_is_still_locked(self):
        live = lock_state([{"app": "Herdr", "name": "herdr-core.exe", "pid": 1, "path": ""},
                           {"app": "Codex", "name": "codex.exe", "pid": 2, "path": ""}])
        self.assertTrue(entry_running(conversation(self.home, provider="Codex"), live))
        self.assertFalse(entry_running(conversation(self.home, provider="Gemini"), live))

    def test_codex_stays_locked_as_a_whole(self):
        live = lock_state([{"app": "Codex", "name": "codex.exe", "pid": 2, "path": ""}])
        self.assertTrue(entry_running(conversation(self.home, provider="Codex"), live))

    def test_orca_still_locks_homes_it_may_manage(self):
        live = LiveApps({"Orca"})
        accounts = Path.home() / ".codex" / "accounts" / "fixture"
        self.assertTrue(entry_running(conversation(accounts, provider="Codex"), live))
        self.assertTrue(entry_running(conversation(Path("C:/x/orca/claude"), provider="Claude"), live))
        self.assertFalse(entry_running(conversation(self.home), live))

    def test_plain_sets_keep_the_strict_whole_app_rule(self):
        for provider in ("Codex", "Claude", "Gemini"):
            self.assertTrue(entry_running(conversation(self.home, provider=provider), {"Herdr"}))
        self.assertTrue(entry_running(conversation(self.home), {"Claude"}))

    def test_workspace_blocker(self):
        self.assertTrue(workspace_blocker({"Orca"}))
        self.assertFalse(workspace_blocker(LiveApps({"Claude"})))
        self.assertTrue(workspace_blocker(LiveApps({"Claude"}, claude_sessions={"a"})))
        self.assertTrue(workspace_blocker(LiveApps({"Claude"}, claude_all=True)))

    def test_lock_key_sees_what_set_equality_cannot(self):
        self.assertEqual(LiveApps({"Claude"}), LiveApps({"Claude"}, claude_sessions={"a"}))
        self.assertNotEqual(lock_key(LiveApps({"Claude"})), lock_key(LiveApps({"Claude"}, claude_sessions={"a"})))
        self.assertNotEqual(lock_key(LiveApps({"Claude"})), lock_key(LiveApps({"Claude"}, claude_all=True)))

    def delete_fixture(self):
        folder = self.home / "projects" / "fixture"
        folder.mkdir(parents=True)
        sid = str(uuid.uuid4())
        path = folder / f"{sid}.jsonl"
        path.write_text(json.dumps({"cwd": str(self.home), "type": "user", "message": {"content": "x"}}) + "\n", encoding="utf8")
        from manager.providers import scan
        entries, _ = scan([("Claude", self.home)])
        entry = next(e for e in entries if e.session == sid)
        return entry, path

    def test_delete_rechecks_with_the_same_precision(self):
        entry, path = self.delete_fixture()
        recycled = []
        args = dict(detector=lambda: LiveApps({"Claude"}, claude_sessions={entry.session}), recycler=recycled.append)
        with self.assertRaises(RuntimeError):
            delete([entry], {entry.key: signature(entry)}, **args)
        self.assertEqual(recycled, [])
        self.assertTrue(path.exists())
        def recycler(target):
            recycled.append(target)
            Path(target).unlink()
        _, good, failed, _ = delete([entry], {entry.key: signature(entry)}, detector=lambda: LiveApps({"Claude"}), recycler=recycler)
        self.assertEqual((len(good), failed), (1, []))
        self.assertFalse(path.exists())

    def test_session_starting_mid_delete_stops_the_delete(self):
        entry, path = self.delete_fixture()
        states = iter([LiveApps({"Claude"}), LiveApps({"Claude"}, claude_sessions={entry.session})])
        recycled = []
        _, good, failed, _ = delete([entry], {entry.key: signature(entry)},
                                    detector=lambda: next(states), recycler=recycled.append)
        self.assertEqual((good, recycled), ([], []))
        self.assertEqual(len(failed), 1)
        self.assertTrue(path.exists())


@unittest.skipIf(runtime.psutil is None, "needs psutil")
class StopProcessTests(unittest.TestCase):
    def spawn(self, code="import time; time.sleep(120)"):
        process = subprocess.Popen([sys.executable, "-I", "-c", code], creationflags=NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.addCleanup(self.cleanup, process)
        return process

    @staticmethod
    def cleanup(process):
        try:
            for child in runtime.psutil.Process(process.pid).children(recursive=True):
                child.kill()
        except runtime.psutil.Error:
            pass
        process.kill()
        process.wait(timeout=5)

    def row(self, process, app="Codex"):
        return {"app": app, "name": "fixture.exe", "pid": process.pid, "path": sys.executable}

    def spawn_with_child(self):
        parent = self.spawn("import subprocess, sys, time\n"
                            f"subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'], creationflags={NO_WINDOW})\n"
                            "time.sleep(120)")
        info = runtime.psutil.Process(parent.pid)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            children = [c for c in info.children(recursive=True) if c.name().casefold().startswith("python")]
            if children:
                return parent, children[-1]
            time.sleep(.05)
        self.fail("child did not start")

    def test_force_stop_takes_the_children_too(self):
        parent, child = self.spawn_with_child()
        group = app_control.snapshot([self.row(parent)])[0]
        self.assertIn(child.pid, {t.pid for t in group.children})
        self.assertTrue(group.stoppable)
        left, notes = app_control.force_stop(group.targets)
        self.assertEqual((left, notes), ([], []))
        parent.wait(timeout=5)
        child.wait(timeout=5)
        self.assertFalse(runtime.psutil.pid_exists(child.pid))

    def test_reused_pid_is_not_stopped(self):
        process = self.spawn()
        real = runtime.psutil.Process(process.pid)
        stale = app_control.Target(process.pid, real.create_time() - 100, "fixture.exe")
        self.assertEqual(app_control.force_stop([stale]), ([], []))
        self.assertIsNone(process.poll())
        self.assertTrue(real.is_running())

    def test_never_stops_itself_or_what_started_it(self):
        me = runtime.psutil.Process()
        targets = [app_control._target(me)]
        with patch.object(runtime.psutil.Process, "kill", side_effect=AssertionError("must not kill")) as kill:
            self.assertEqual(app_control.force_stop(targets), ([], []))
            kill.assert_not_called()
        self.assertIn(me.pid, app_control.protected_pids())
        for parent in me.parents():
            self.assertIn(parent.pid, app_control.protected_pids())

    def test_group_holding_this_program_cannot_be_stopped(self):
        parent = runtime.psutil.Process().parent()
        if parent is None:
            self.skipTest("no parent process")
        row = {"app": "Herdr", "name": parent.name(), "pid": parent.pid, "path": ""}
        with patch.object(app_control, "visible_windows", return_value=[]):
            group = app_control.snapshot([row])[0]
        self.assertTrue(group.holds_self)
        self.assertFalse(group.stoppable)
        with self.assertRaises(RuntimeError):
            app_control.request_close([group])

    def test_program_without_a_window_is_reported_without_waiting(self):
        process = self.spawn()
        group = app_control.snapshot([self.row(process)])[0]
        started = time.monotonic()
        with patch.object(app_control, "visible_windows", return_value=[]):
            left, notes = app_control.request_close([group], grace=30)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn(process.pid, [t.pid for t in left])
        self.assertEqual(notes, [])
        self.assertIsNone(process.poll())

    def test_window_close_is_requested_and_waits_for_exit(self):
        process = self.spawn()
        group = app_control.snapshot([self.row(process)])[0]
        closed = []
        def close(hwnd):
            closed.append(hwnd)
            process.kill()
            return True
        with patch.object(app_control, "visible_windows", return_value=[(77, process.pid), (88, 1)]), \
                patch.object(app_control, "close_window", side_effect=close):
            left, _ = app_control.request_close([group], grace=10)
        self.assertEqual(closed, [77])
        self.assertEqual(left, [])

    def test_herdr_is_asked_with_its_own_command(self):
        process = self.spawn()
        group = app_control.snapshot([self.row(process, "Herdr")])[0]
        with patch.object(app_control, "visible_windows", return_value=[]), \
                patch.object(runtime, "command", side_effect=lambda *a, **k: process.kill()) as command:
            left, notes = app_control.request_close([group], grace=10)
        self.assertEqual(command.call_args.args[:2], ("hc", ["server", "stop"]))
        self.assertEqual((left, notes), ([], []))

    def test_failed_herdr_command_is_reported_and_leaves_the_decision_to_the_user(self):
        process = self.spawn()
        group = app_control.snapshot([self.row(process, "Herdr")])[0]
        with patch.object(app_control, "visible_windows", return_value=[]), \
                patch.object(runtime, "command", side_effect=RuntimeError("hc 실행 파일을 찾지 못함")):
            left, notes = app_control.request_close([group], grace=10)
        self.assertIn(process.pid, [t.pid for t in left])
        self.assertIn("hc", notes[0])

    def test_vanished_process_is_shown_but_not_targeted(self):
        process = self.spawn()
        pid = process.pid
        process.kill()
        process.wait(timeout=5)
        group = app_control.snapshot([{"app": "Codex", "name": "codex.exe", "pid": pid, "path": ""}])[0]
        self.assertEqual(group.targets, [])
        self.assertFalse(group.stoppable)
        self.assertEqual(len(group.rows), 1)

    def test_desktop_group_warns_about_sessions_it_hosts(self):
        parent, child = self.spawn_with_child()
        with patch.object(runtime, "_process_app", return_value="Claude"):
            group = app_control.snapshot([{"app": "Claude", "name": "Claude.exe", "pid": parent.pid, "path": DESKTOP}])[0]
        self.assertEqual(group.name, "Claude 데스크톱")
        # The shell itself is not an agent; what it started is. (python.exe may be a launcher plus a child.)
        self.assertEqual(group.agents, len(group.children))
        self.assertGreaterEqual(group.agents, 1)
        self.assertIn("에이전트", app_control.stop_summary([group]))

    def test_summaries_are_readable(self):
        target = app_control.Target(1, 0, "herdr-core.exe")
        text = app_control.leftovers_summary([target, app_control.Target(2, 0, "herdr-core.exe")], ["hc server stop 실패"])
        self.assertIn("herdr-core.exe 2개", text)
        self.assertIn("hc server stop 실패", text)


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class MonitorAndDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, condition, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("timed out")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="app-control-ui-")
        self.root = Path(self.temp.name)
        self.project = self.root / "source"
        self.project.mkdir()
        self.claude = self.root / "claude"
        folder = self.claude / "projects" / "fixture"
        folder.mkdir(parents=True)
        self.sessions = [str(uuid.uuid4()) for _ in range(2)]
        for sid in self.sessions:
            (folder / f"{sid}.jsonl").write_text(json.dumps({"cwd": str(self.project), "type": "user", "message": {"content": sid}}) + "\n", encoding="utf8")
        self.processes = patch("manager.qt_logic.running_processes", return_value=[])
        self.codex = patch("manager.qt_logic.codex_cli", return_value=("fixture-codex.exe", "0.0.0"))
        self.running = self.processes.start()
        self.codex.start()
        self.window = App(roots=[("Claude", self.claude)], auto_refresh=False)
        self.window.show()
        self.window.refresh()
        self.wait(lambda: not self.window.busy and bool(self.window.entries))
        self.entries = {e.session: e for e in self.window.entries if e.kind == "conversation"}

    def tearDown(self):
        self.wait(lambda: not self.window.busy and self.window.monitor_task is None)
        for dialog in list(self.window.dialogs):
            dialog.reject()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.codex.stop()
        self.processes.stop()
        self.temp.cleanup()

    def poll(self):
        self.window.poll_processes()
        self.wait(lambda: self.window.monitor_task is None)

    def checkable(self, sid):
        row = self.window.model.record_items[self.entries[sid].key]
        return bool(row[0].flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_lock_lifts_and_returns_without_rescanning(self):
        first, second = self.sessions
        self.window.set_records_checked([self.entries[second]], True)
        rows = dict(self.window.model.record_items)
        with patch("manager.qt_logic.scan", side_effect=AssertionError("must not rescan")), \
                patch("manager.qt_logic.lock_state", return_value=LiveApps({"Claude"}, claude_sessions={first})):
            self.running.return_value = [claude_row(1, HOSTED)]
            self.poll()
            self.assertFalse(self.checkable(first))
            self.assertTrue(self.checkable(second))
            row = self.window.model.record_items[self.entries[first].key]
            self.assertEqual(row[5].text(), "앱 종료 후 삭제")
            self.assertIn("잠긴 기록 1개", self.window.status_text.text())
        self.assertIn(self.entries[second].key, self.window.checked)
        self.assertIs(self.window.model.record_items[self.entries[first].key][0], rows[self.entries[first].key][0])
        with patch("manager.qt_logic.scan", side_effect=AssertionError("must not rescan")):
            self.running.return_value = []
            self.poll()
        self.assertTrue(self.checkable(first))
        row = self.window.model.record_items[self.entries[first].key]
        self.assertEqual(row[5].text(), "")
        self.assertTrue(row[5].icon().isNull())
        self.assertIn(self.entries[second].key, self.window.checked)

    def test_selection_is_dropped_when_its_record_becomes_locked(self):
        first, second = self.sessions
        self.window.set_records_checked(list(self.entries.values()), True)
        with patch("manager.qt_logic.lock_state", return_value=LiveApps({"Claude"}, claude_sessions={first})):
            self.running.return_value = [claude_row(1, HOSTED)]
            self.poll()
        self.assertEqual(self.window.checked, {self.entries[second].key})
        self.assertIn("1개", self.window.detail_hint.text())

    def test_failed_detection_locks_everything_until_it_recovers(self):
        self.window.set_records_checked(list(self.entries.values()), True)
        self.running.side_effect = RuntimeError("fixture failure")
        self.poll()
        self.running.side_effect = None
        self.assertFalse(self.window.checked)
        self.assertFalse(self.checkable(self.sessions[0]))
        self.assertIn("모두 잠금", self.window.status_text.text())
        self.assertFalse(self.window.errors)
        self.poll()
        self.assertTrue(self.checkable(self.sessions[0]))
        self.assertNotIn("모두 잠금", self.window.status_text.text())

    def test_poll_skips_while_busy_and_ignores_a_late_result(self):
        self.window.busy = True
        self.window.poll_processes()
        self.assertIsNone(self.window.monitor_task)
        self.window.monitor_done("monitor", (LiveApps({"Claude"}, claude_all=True), [claude_row(1, CLI)]), None)
        self.assertFalse(self.window.running)
        self.window.busy = False

    def test_closed_window_does_not_poll(self):
        self.window._closing = True
        self.window.poll_processes()
        self.assertIsNone(self.window.monitor_task)
        self.window._closing = False

    def test_auto_refresh_window_runs_the_monitor(self):
        window = App(roots=[("Claude", self.claude)], auto_refresh=True)
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.monitor_timer.isActive())
        self.wait(lambda: not window.startup_timer.isActive() and not window.busy and window.monitor_task is None)
        window.close()
        self.assertFalse(window.monitor_timer.isActive())

    def test_workspace_is_refused_while_herdr_runs_and_dropped_when_it_starts(self):
        group = next(iter(self.window.groups))
        self.window.toggle_workspace(group, True)
        self.assertTrue(self.window.workspaces)
        self.running.return_value = [{"app": "Herdr", "name": "herdr-core.exe", "pid": 1, "path": ""}]
        self.poll()
        self.assertFalse(self.window.workspaces)
        self.assertTrue(self.checkable(self.sessions[0]))
        self.window.toggle_workspace(group, True)
        self.assertFalse(self.window.workspaces)
        self.assertIn("작업 폴더를 보존", self.window.detail_hint.text())

    def dialog_with(self, process):
        self.running.return_value = [{"app": "Codex", "name": "fixture.exe", "pid": process.pid, "path": sys.executable}]
        self.poll()
        return self.window.show_processes()

    def spawn(self):
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"], creationflags=NO_WINDOW)
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.kill)
        return process

    def test_read_only_mode_offers_no_way_to_stop(self):
        process = self.spawn()
        self.window.allow_delete = False
        dialog = self.dialog_with(process)
        self.assertFalse(dialog.stop_buttons)
        self.assertIsNone(dialog.stop_all)
        self.assertIn("읽기 전용", dialog.hint.text())
        with patch("manager.qt_logic.ask", side_effect=AssertionError("must not ask")):
            self.window.stop_apps(dialog.groups)
        self.assertIsNone(process.poll())

    def test_cancelling_the_first_question_stops_nothing(self):
        process = self.spawn()
        dialog = self.dialog_with(process)
        with patch("manager.qt_logic.ask", return_value=False) as asked:
            dialog.stop_buttons["Codex"].click()
        asked.assert_called_once()
        self.assertFalse(self.window.busy)
        self.assertIsNone(process.poll())

    def test_declining_force_leaves_the_process_and_refreshes(self):
        process = self.spawn()
        dialog = self.dialog_with(process)
        with patch("manager.qt_logic.ask", side_effect=[True, False]) as asked, \
                patch.object(app_control, "force_stop", side_effect=AssertionError("must not force")) as force, \
                patch.object(app_control, "visible_windows", return_value=[]):
            dialog.stop_buttons["Codex"].click()
            self.wait(lambda: asked.call_count == 2 and not self.window.busy)
        force.assert_not_called()
        self.assertIsNone(process.poll())

    def test_full_flow_stops_the_app_and_unlocks_its_records(self):
        process = self.spawn()
        dialog = self.dialog_with(process)
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        self.assertFalse(self.window.tree.grab().isNull())
        self.assertFalse(dialog.grab().isNull())
        with patch("manager.qt_logic.ask", return_value=True) as asked, \
                patch.object(app_control, "visible_windows", return_value=[]):
            dialog.stop_all.click()
            self.assertFalse(dialog.stop_all.isEnabled())
            self.running.return_value = []
            self.wait(lambda: process.poll() is not None)
            self.wait(lambda: not self.window.busy)
        self.assertEqual(asked.call_count, 2)
        self.assertEqual(dialog.tree.topLevelItemCount(), 0)
        self.assertFalse(self.window.running)
        self.assertIn("없습니다", dialog.hint.text())

    def test_button_for_app_holding_this_program_is_disabled(self):
        parent = runtime.psutil.Process().parent()
        if parent is None:
            self.skipTest("no parent process")
        self.running.return_value = [{"app": "Herdr", "name": parent.name(), "pid": parent.pid, "path": ""}]
        self.poll()
        dialog = self.window.show_processes()
        self.assertFalse(dialog.stop_buttons["Herdr"].isEnabled())
        self.assertFalse(dialog.stop_all.isEnabled())
        self.assertIn("실행.cmd", dialog.hint.text())
        with patch("manager.qt_logic.ask", side_effect=AssertionError("must not ask")):
            self.window.stop_apps(dialog.groups)

    def test_dialog_follows_the_monitor_and_keeps_rows_open(self):
        process = self.spawn()
        dialog = self.dialog_with(process)
        dialog.tree.topLevelItem(0).setExpanded(True)
        second = self.spawn()
        self.running.return_value = [{"app": "Codex", "name": "fixture.exe", "pid": pid, "path": sys.executable}
                                     for pid in (process.pid, second.pid)]
        self.poll()
        self.assertEqual(dialog.tree.topLevelItem(0).childCount(), 2)
        self.assertTrue(dialog.tree.topLevelItem(0).isExpanded())
        dialog.reject()
        self.wait(lambda: self.window.process_dialog is None)
        self.running.return_value = []
        self.poll()


if __name__ == "__main__":
    unittest.main()
