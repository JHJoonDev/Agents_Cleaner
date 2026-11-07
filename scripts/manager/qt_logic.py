"""Qt controller: the same backend calls and snapshots as the Tk fallback."""
from pathlib import Path
from collections import defaultdict
from time import monotonic

from PySide6.QtCore import QThreadPool, QTimer, Slot, Qt

from . import app_control, operations
from .model import signature
from .providers import scan
from .runtime import (PROVIDERS, codex_cli, entry_running, lock_key, lock_state, orca_projects, psutil,
                      running_processes, workspace_blocker)
from .workspaces import ai_homes, conversations_in, fixed_problem, key as path_key, plan_workspaces
from .qt_dialogs import BackupDialog, ProcessesDialog, ResultDialog, ReviewDialog
from .qt_models import Task
from .qt_style import ask, message

# Detection takes milliseconds with psutil. Its PowerShell fallback starts a process each time.
MONITOR_MS = 2000 if psutil is not None else 15000


class Logic:
    def initialize_logic(self, roots, allow_delete):
        self.roots, self.allow_delete = roots, allow_delete
        self.entries, self.errors, self.processes, self.orca = [], [], [], []
        self.running, self.checked, self.project_groups, self.workspaces = set(), set(), set(), set()
        self.dirs, self.protected, self.groups, self.entry_by_key = {}, {}, {}, {}
        self.backup_dir = operations.DEFAULT_BACKUP_DIR
        self.codex, self.busy, self.pending = "", False, []
        self.jobs, self.dialogs = [], []
        self.pool = QThreadPool.globalInstance()
        self.job_started, self.job_status = None, ""
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self.update_busy_status)
        self.process_dialog, self.monitor_task, self.monitor_failed = None, None, False
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(MONITOR_MS)
        self.monitor_timer.timeout.connect(self.poll_processes)

    def update_busy_status(self):
        if self.busy and self.job_started is not None:
            seconds = max(0, int(monotonic() - self.job_started))
            self.status_text.setText(f"{self.job_status} ({seconds}초 경과)")

    def launch(self, function, event):
        if self.busy or self._closing:
            return False
        self.busy = True
        self.job_started, self.job_status = monotonic(), self.status_text.text()
        self.elapsed_timer.start()
        self.update_actions()
        task = Task(function, event)
        task.signals.done.connect(self.finished, Qt.ConnectionType.QueuedConnection)
        self.jobs.append(task)
        try:
            self.pool.start(task)
        except Exception as exc:
            self.finished(event, None, str(exc) or type(exc).__name__)
            return False
        return True

    def refresh(self):
        if self.busy or self._closing:
            return
        self.status_text.setText("앱별 프로젝트·대화기록을 읽는 중…")
        def work():
            entries, errors = scan(self.roots)
            for e in entries:
                e.extra["display_size"] = e.size
            try:
                processes = running_processes()
                running = lock_state(processes)
            except Exception as exc:
                errors.append(str(exc))
                processes, running = [], set(PROVIDERS)
            projects = []
            if self.roots is None:
                try:
                    projects = orca_projects()
                except Exception as exc:
                    errors.append(f"Orca 프로젝트: {exc}")
            try:
                path, version = codex_cli()
                codex = f"{version} ({path})"
            except Exception as exc:
                codex = f"찾지 못함: {exc}"
            dirs, protected, homes = {}, {}, ai_homes(entries)
            seen = set()
            for e in entries:
                if e.kind != "conversation" or not Path(e.project).is_absolute():
                    continue
                k = path_key(e.project)
                if k in seen:
                    continue
                seen.add(k)
                if Path(e.project).is_dir():
                    dirs[k] = e.project
                    problem = fixed_problem(e.project, homes)
                    if problem:
                        protected[k] = problem
            return entries, errors, running, processes, projects, codex, dirs, protected
        self.launch(work, "scan")

    @Slot(str, object, object)
    def finished(self, event, result, error):
        self.elapsed_timer.stop()
        self.job_started = None
        self.jobs.clear()
        try:
            self.apply_result(event, result, error)
        except Exception as exc:
            self.errors.append(f"화면 갱신 실패: {exc}")
            self.status_text.setText("실패: " + (str(exc) or type(exc).__name__))
            message(self, "작업 중단", str(exc) or type(exc).__name__, critical=True)
        finally:
            self.busy = False
            self.update_actions()
        if event == "delete" and not error:
            self.refresh()
        elif event in {"stop", "force"}:
            self.after_stop(event, result, error)

    def apply_result(self, event, result, error):
        if error:
            if event == "scan":
                self.errors.append(error)
            self.status_text.setText("실패: " + error)
            message(self, "작업 중단", error, critical=True)
            return
        if event == "scan":
            self.entries, self.errors, self.running, self.processes, self.orca, self.codex, self.dirs, self.protected = result
            self.checked.clear()
            self.project_groups.clear()
            self.workspaces.clear()
            self.entry_by_key = {e.key: e for e in self.entries}
            grouped = defaultdict(list)
            for e in self.entries:
                grouped[e.group].append(e)
            self.groups = dict(grouped)
            self.render()
            self.monitor_failed = False
            self.update_status()
            self.update_process_dialog()
        elif event == "plan":
            self.status_text.setText("작업 폴더 확인 완료")
            self.open_review(self.pending, result)
        elif event == "delete":
            _, good, failed, _ = result
            self.status_text.setText(f"정리 완료 · 성공 {len(good)}개 / 오류 {len(failed)}개 · 결과창을 닫으면 목록을 새로고칩니다.")
            dialog = ResultDialog(result, self)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()

    def update_status(self):
        live = ", ".join(sorted(self.running)) or "없음"
        text = f"기록 {len(self.entries):,}개 · 조회 오류 {len(self.errors)}개 · 실행 중: {live}"
        locked = sum(self.is_running(e) for e in self.entries)
        if locked:
            text += f" · 잠긴 기록 {locked:,}개"
        if self.monitor_failed:
            text += " · 실행 상태 확인 실패로 모두 잠금"
        self.status_text.setText(text)

    def poll_processes(self):
        if self.busy or self._closing or self.monitor_task is not None:
            return
        def work():
            processes = running_processes()
            return lock_state(processes), processes
        task = Task(work, "monitor")
        task.signals.done.connect(self.monitor_done, Qt.ConnectionType.QueuedConnection)
        self.monitor_task = task
        try:
            self.pool.start(task)
        except Exception as exc:
            self.monitor_done("monitor", None, str(exc) or type(exc).__name__)

    @Slot(str, object, object)
    def monitor_done(self, _event, result, error):
        self.monitor_task = None
        # A scan or delete in progress has fresher facts, or will check for itself.
        if self.busy or self._closing:
            return
        running, processes = (set(PROVIDERS), []) if error else result
        self.apply_processes(running, processes, bool(error))

    def apply_processes(self, running, processes, failed=False):
        """Follow the apps without rescanning, so the list and the current selection survive."""
        locks_changed = lock_key(running) != lock_key(self.running) or failed != self.monitor_failed
        apps_changed = {p["pid"] for p in processes} != {p["pid"] for p in self.processes}
        self.running, self.processes, self.monitor_failed = running, processes, failed
        if locks_changed:
            dropped = self.prune_selection()
            self.update_locks()
            self.update_status()
            if dropped:
                self.detail_hint.setText(f"앱 실행 상태가 바뀌어 선택 {dropped}개를 해제했습니다.")
        if apps_changed or locks_changed:
            self.update_process_dialog()

    def prune_selection(self):
        before = len(self.checked)
        self.checked = {k for k in self.checked if k in self.entry_by_key and self.can_select(self.entry_by_key[k])}
        self.project_groups = {g for g in self.project_groups if any(e.key in self.checked for e in self.groups.get(g, []))}
        if workspace_blocker(self.running):
            self.workspaces.clear()
        else:
            self.workspaces = {k for k in self.workspaces
                               if all(e.key in self.checked for e in conversations_in(self.dirs[k], self.entries))}
        return before - len(self.checked)

    def update_process_dialog(self):
        if self.process_dialog is not None:
            self.process_dialog.set_groups(app_control.snapshot(self.processes), self.running)

    def is_running(self, e):
        return entry_running(e, self.running)

    def can_select(self, e):
        return not e.blocked and not self.is_running(e)

    def workspace_of(self, group, members):
        if all(e.kind == "cache" for e in members):
            return None
        k = path_key(group[2]) if Path(group[2]).is_absolute() else None
        return k if k in self.dirs else None

    def set_records_checked(self, members, state, group=None):
        if self.busy:
            self.sync_checks()
            return
        if group is not None and self.errors:
            self.detail_hint.setText("조회 오류가 있어 프로젝트 일괄 선택을 차단했습니다. 개별 대화만 선택할 수 있습니다.")
            self.sync_checks()
            return
        eligible = [e for e in members if self.can_select(e)]
        if not eligible:
            self.sync_checks()
            return
        for e in eligible:
            self.checked.add(e.key) if state else self.checked.discard(e.key)
        if group is not None:
            self.project_groups.add(group) if state else self.project_groups.discard(group)
        else:
            for e in members:
                self.project_groups.discard(e.group)
        for k in list(self.workspaces):
            if any(e.key not in self.checked for e in conversations_in(self.dirs[k], self.entries)):
                self.workspaces.discard(k)
        self.sync_checks()

    def toggle_workspace(self, group, desired=None):
        if self.busy:
            self.sync_checks()
            return
        if self.errors:
            self.detail_hint.setText("조회 오류가 있어 작업 폴더 삭제를 차단했습니다. 새로고침 후 오류를 확인하세요.")
            self.sync_checks()
            return
        k = self.workspace_of(group, self.groups[group])
        if not k:
            return
        path = self.dirs[k]
        if k in self.protected:
            self.detail_hint.setText(f"작업 폴더 보호됨: {path}\n이유: {self.protected[k]}. 이 폴더는 프로그램에서 삭제할 수 없습니다.")
            self.sync_checks()
            return
        state = k not in self.workspaces if desired is None else desired
        if state and workspace_blocker(self.running):
            self.detail_hint.setText(f"{workspace_blocker(self.running)}. '실행 중 앱'에서 종료한 뒤 다시 선택하세요.")
            self.sync_checks()
            return
        if not state:
            self.workspaces.discard(k)
            self.detail_hint.setText(f"작업 폴더 보존: {path}")
        else:
            members = conversations_in(path, self.entries)
            for e in members:
                if self.can_select(e):
                    self.checked.add(e.key)
            for g, ms in self.groups.items():
                if all(m.key in self.checked for m in ms):
                    self.project_groups.add(g)
            self.workspaces.add(k)
            locked = sum(not self.can_select(e) for e in members)
            self.detail_hint.setText(f"작업 폴더를 함께 삭제합니다: {path}\n하위 폴더 포함 대화 {len(members)}개를 앱 구분 없이 선택했습니다." + (f" 선택 불가 {locked}개가 있어 검토 단계에서 폴더가 제외됩니다." if locked else ""))
        self.sync_checks()

    def select_all(self):
        # Select records only; deleting source folders remains an explicit choice.
        self.set_records_checked(self.entries, True)

    def clear(self):
        if not self.busy:
            self.checked.clear()
            self.project_groups.clear()
            self.workspaces.clear()
            self.sync_checks()

    def show_dialog(self, dialog):
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.dialogs.append(dialog)
        dialog.finished.connect(lambda _: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.open()
        return dialog

    def show_processes(self):
        if self.busy:
            return None
        stop = self.stop_apps if self.allow_delete else None
        dialog = ProcessesDialog(app_control.snapshot(self.processes), self.running, stop, self)
        self.process_dialog = dialog
        dialog.finished.connect(lambda _: setattr(self, "process_dialog", None))
        return self.show_dialog(dialog)

    def stop_apps(self, groups):
        groups = [g for g in groups if g.stoppable]
        if self.busy or not self.allow_delete or not groups:
            return
        if not ask(self.process_dialog or self, "앱 종료", app_control.stop_summary(groups), "종료"):
            return
        self.status_text.setText("앱에 종료를 요청하는 중…")
        self.launch(lambda: app_control.request_close(groups), "stop")

    def after_stop(self, event, result, error):
        """Runs once the task is finished, so the next step can start its own."""
        left, notes = result if not error else ([], [])
        if event == "stop" and left:
            if ask(self.process_dialog or self, "강제 종료", app_control.leftovers_summary(left, notes), "강제 종료"):
                self.status_text.setText("남은 프로세스를 강제 종료하는 중…")
                self.launch(lambda: app_control.force_stop(left), "force")
                return
        elif event == "force" and (left or notes):
            lines = [*notes, *(f"{t.name} (PID {t.pid}) 종료 실패" for t in left)]
            message(self.process_dialog or self, "일부 프로세스 종료 실패", "\n".join(lines))
        elif notes:
            message(self.process_dialog or self, "앱 종료", "\n".join(notes))
        # Closing apps can write out last-minute changes, so read the records again.
        self.refresh()

    def manage_backups(self):
        if not self.busy:
            return self.show_dialog(BackupDialog(self.backup_dir, self.allow_delete, self.set_backup_dir, self))

    def set_backup_dir(self, path):
        self.backup_dir = Path(path)

    def review(self):
        if self.busy or not self.allow_delete:
            return
        if self.errors and (self.workspaces or self.project_groups):
            message(self, "삭제 차단", "조회 오류가 있어 프로젝트·작업 폴더 삭제를 차단했습니다. 새로고침 후 확인하세요.")
            return
        selected = [e for e in self.entries if e.key in self.checked and self.can_select(e)]
        if not selected:
            message(self, "선택 필요", "정리할 프로젝트 또는 대화를 체크하세요.")
            return
        if not self.workspaces:
            return self.open_review(selected, [])
        self.pending = selected
        paths = [self.dirs[k] for k in self.workspaces]
        keys, entries, orca = set(self.checked), list(self.entries), [p.get("path", "") for p in self.orca]
        self.status_text.setText("작업 폴더 용량과 git 상태를 확인하는 중…")
        self.launch(lambda: plan_workspaces(paths, entries, keys, orca), "plan")

    def open_review(self, selected, plan):
        dialog = ReviewDialog(selected, plan, self.backup_dir, self.codex, self.allow_delete, self)
        groups, everything = set(self.project_groups), list(self.entries)
        dialog.requested.connect(lambda payload: self.submit_review(list(selected), groups, everything, payload))
        return self.show_dialog(dialog)

    def submit_review(self, selected, groups, everything, payload):
        if self.busy or not self.allow_delete:
            return
        if self.errors and (groups or payload.get("workspaces")):
            message(self, "삭제 차단", "조회 오류가 있어 프로젝트·작업 폴더 삭제를 차단했습니다. 새로고침 후 확인하세요.")
            return
        if any(not self.can_select(e) for e in selected):
            message(self, "삭제 차단", "실행 중이거나 차단된 항목이 있습니다. 새로고침 후 확인하세요.")
            return
        self.status_text.setText("선택한 기록을 정리하는 중…")
        def work():
            expected = {e.key: signature(e) for e in selected}
            return operations.delete(selected, expected, project_groups=groups,
                                     workspaces=payload["workspaces"], all_entries=everything)
        self.launch(work, "delete")
