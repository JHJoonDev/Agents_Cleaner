"""Qt controller: the same backend calls and snapshots as the Tk fallback."""
from pathlib import Path
from collections import defaultdict
from time import monotonic

from PySide6.QtCore import QThreadPool, QTimer, Slot, Qt

from . import operations
from .model import signature
from .providers import scan
from .runtime import PROVIDERS, codex_cli, entry_running, orca_projects, running_processes
from .workspaces import ai_homes, conversations_in, fixed_problem, key as path_key, plan_workspaces
from .qt_dialogs import BackupDialog, ProcessesDialog, ResultDialog, ReviewDialog
from .qt_models import Task
from .qt_style import message


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
                running = {p["app"] for p in processes}
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
            live = ", ".join(sorted(self.running)) or "없음"
            self.status_text.setText(f"기록 {len(self.entries):,}개 · 조회 오류 {len(self.errors)}개 · 실행 중: {live}")
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
        if not self.busy:
            return self.show_dialog(ProcessesDialog(self.processes, self.running, self))

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
