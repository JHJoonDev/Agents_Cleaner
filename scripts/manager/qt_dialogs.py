"""B2 dialogs. All mutations go through the existing operations API."""
from pathlib import Path
import os
import textwrap

from PySide6.QtCore import QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import operations
from .model import readable_size
from .qt_models import Task, display_date
from .qt_style import icon, label, message


def button(text, callback, layout):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


def table(headers, parent=None):
    widget = QTreeWidget(parent)
    widget.setHeaderLabels(headers)
    widget.setRootIsDecorated(False)
    widget.setUniformRowHeights(True)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.header().setStretchLastSection(False)
    widget.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    widget.setAlternatingRowColors(False)
    return widget


class AsyncDialog(QDialog):
    """One reader at a time, keeping only the latest requested folder."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._reads, self._next_read, self._read_id = {}, None, 0
        self._closed = False
        self.finished.connect(self._read_closed)

    def _read_closed(self, _):
        self._closed = True
        self._next_read = None

    def _start_read(self, function, callback):
        if self._closed:
            return
        if self._reads:
            self._next_read = function, callback
            return
        self._read_id += 1
        event = str(self._read_id)
        task = Task(function, event)
        task.signals.done.connect(self._read_done, Qt.ConnectionType.QueuedConnection)
        self._reads[event] = task, callback
        try:
            QThreadPool.globalInstance().start(task)
        except Exception as exc:
            self._read_done(event, None, str(exc) or type(exc).__name__)

    @Slot(str, object, object)
    def _read_done(self, event, result, error):
        job = self._reads.pop(event, None)
        if job is None or self._closed:
            return
        if self._next_read is not None:
            function, callback = self._next_read
            self._next_read = None
            self._start_read(function, callback)
        else:
            job[1](result, error)


class ReviewDialog(AsyncDialog):
    requested = Signal(object)

    def __init__(self, selected, plan, destination, codex="", allow_delete=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("삭제 대상 최종 확인")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(880, 700 if plan else 620)
        self.allow_delete = allow_delete
        self._submitted = False
        self.selected, self.plan = list(selected), list(plan)
        self.eligible = [i for i in plan if not i["problem"]]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 17, 20, 15)
        outer.setSpacing(12)
        text = "선택한 AI 기록을 정리합니다." + (" 표시한 작업 폴더도 휴지통으로 옮깁니다." if plan else " 작업 폴더(결과물)는 보존됩니다.")
        outer.addWidget(label(text, wrap=True))
        if plan:
            group = QGroupBox("함께 삭제할 작업 폴더 (프로그램이 따로 백업하지 않음)")
            folder_layout = QVBoxLayout(group)
            for item in plan:
                line = QHBoxLayout()
                path = label(str(item["path"]))
                path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                path.setMinimumWidth(120)
                path.setToolTip(str(item["path"]))
                line.addWidget(path, 1)
                line.addWidget(label(f"{readable_size(item['size'])} · 파일 {item['files']:,}개", muted=True))
                folder_layout.addLayout(line)
                if item["problem"]:
                    note = label("삭제하지 않음: " + item["problem"], wrap=True)
                    note.setObjectName("problem")
                    folder_layout.addWidget(note)
                else:
                    for warning in item["warnings"]:
                        row = QHBoxLayout()
                        symbol = label()
                        symbol.setPixmap(icon("warning", "#d6b779").pixmap(14, 14))
                        row.addWidget(symbol)
                        note = label(str(warning), wrap=True)
                        note.setObjectName("warning")
                        row.addWidget(note, 1)
                        folder_layout.addLayout(row)
            outer.addWidget(group)
        outer.addWidget(label(f"정리할 AI 기록 · {len(selected)}개"))
        self.targets = table(["대화 / 캐시", "앱", "기록 용량"])
        self.targets.setStyleSheet("QTreeWidget::item { height: 26px; }")
        self.targets.setMinimumHeight(95)
        self.targets.setColumnWidth(1, 90)
        self.targets.setColumnWidth(2, 120)
        for e in selected:
            value = readable_size(e.extra.get("display_size", 0)) if e.files else "DB 항목만"
            row = QTreeWidgetItem([" ".join(e.title.split())[:120], e.provider, value])
            row.setToolTip(0, f"ID: {e.session}\n저장소: {e.home}")
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.targets.addTopLevelItem(row)
        outer.addWidget(self.targets, 1)
        methods = "백업을 만들지 않습니다. Codex 대화는 공식 명령으로 영구 삭제하며 복구할 수 없습니다.\nClaude·Gemini 대화와 캐시·로그는 휴지통으로 이동합니다. 프로젝트 등록·메모리는 보존하며, 앱 실행 중에는 삭제를 차단합니다."
        if any(e.provider == "Codex" for e in selected):
            methods += f"\n사용할 Codex CLI: {codex}"
        outer.addWidget(label(methods, muted=True, wrap=True))
        self.agreed = QCheckBox(textwrap.fill("선택 작업이 종료되었고, 필요한 결과물은 보관했거나 필요 없음을 확인했습니다.", width=64))
        outer.addWidget(self.agreed)
        self.moved = QCheckBox()
        if self.eligible:
            total = readable_size(sum(i["size"] for i in self.eligible))
            self.moved.setText(textwrap.fill(f"작업 폴더 {len(self.eligible)}개({total})의 결과물을 실제 사용할 폴더로 옮겼거나 필요 없음을 확인했습니다.", width=64))
            outer.addWidget(self.moved)
        else:
            self.moved.hide()
        actions = QHBoxLayout()
        actions.addStretch()
        self.confirm_button = button("선택 기록 삭제", self.confirm, actions)
        self.cancel_button = button("취소", self.reject, actions)
        self.cancel_button.setDefault(True)
        outer.addLayout(actions)
        self.agreed.toggled.connect(self.update_enabled)
        self.moved.toggled.connect(self.update_enabled)
        self.update_enabled()

    def update_enabled(self, *_):
        valid = not self._submitted and self.allow_delete and bool(self.selected)
        self.confirm_button.setEnabled(valid and self.agreed.isChecked() and (not self.eligible or self.moved.isChecked()))

    def confirm(self):
        if self._closed or self._submitted:
            return
        self.update_enabled()
        if not self.confirm_button.isEnabled():
            return
        self._submitted = True
        self.confirm_button.setEnabled(False)
        self.requested.emit({"workspaces": [i["path"] for i in self.eligible]})
        self.accept()


class BackupDialog(AsyncDialog):
    def __init__(self, destination, allow_delete=True, changed=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("백업 관리")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(800, 470)
        self.allow_delete, self.changed = allow_delete, changed
        self._loading, self._removing = False, False
        self.folder = Path(destination)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 15, 18, 15)
        self.location = label()
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary = label(muted=True, wrap=True)
        outer.addWidget(self.location)
        outer.addWidget(self.summary)
        self.tree = table(["백업 파일", "크기", "만든 시각"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 160)
        outer.addWidget(self.tree, 1)
        row = QHBoxLayout()
        button("다른 폴더", self.choose, row)
        button("폴더 열기", self.open_folder, row)
        row.addStretch()
        self.remove_button = button("선택 백업 영구 삭제", self.remove, row)
        self.remove_button.setEnabled(allow_delete)
        button("닫기", self.reject, row)
        outer.addLayout(row)
        self.load()

    def load(self):
        self._loading = True
        self.remove_button.setEnabled(False)
        self.tree.clear()
        self.location.setText(str(self.folder))
        self.summary.setText("백업 목록을 읽는 중…")
        folder = self.folder
        self._start_read(lambda: operations.list_backups(folder), self._loaded)

    def _loaded(self, rows, error):
        self._loading = False
        self.remove_button.setEnabled(self.allow_delete and not self._removing)
        if error:
            self.summary.setText(f"조회 실패: {error}")
        else:
            for path, size, modified in rows:
                row = QTreeWidgetItem([path.name, readable_size(size), display_date(modified)])
                row.setData(0, Qt.ItemDataRole.UserRole, str(path))
                row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.tree.addTopLevelItem(row)
            self.summary.setText(f"백업 {len(rows)}개 · 합계 {readable_size(sum(r[1] for r in rows))}. 선택한 백업은 휴지통을 거치지 않고 바로 삭제됩니다.")

    def choose(self):
        if self._removing:
            return
        picked = QFileDialog.getExistingDirectory(self, "백업 폴더", str(self.folder))
        if picked:
            self.folder = Path(picked)
            if self.changed:
                self.changed(self.folder)
            self.load()

    def open_folder(self):
        if self.folder.is_dir():
            os.startfile(str(self.folder))

    def remove(self):
        if not self.allow_delete or self._loading or self._removing or self._closed:
            return
        chosen = [Path(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.tree.selectedItems()]
        if not chosen:
            message(self, "선택 필요", "삭제할 백업을 선택하세요. Ctrl·Shift로 여러 개를 고를 수 있습니다.")
            return
        self._removing = True
        self.remove_button.setEnabled(False)
        answer = QMessageBox.question(self, "백업 영구 삭제", f"백업 {len(chosen)}개를 영구 삭제합니다.\n이 백업에 든 기록은 더 이상 복구할 수 없습니다. 계속할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self._removing = False
            self.remove_button.setEnabled(self.allow_delete)
            return
        folder = self.folder
        self.summary.setText("선택한 백업을 정리하는 중…")
        self._start_read(lambda: operations.remove_backups(chosen, folder), self._removed)

    def _removed(self, _, error):
        if error:
            message(self, "삭제 실패", error, critical=True)
        self._removing = False
        self.load()

    def reject(self):
        if not self._removing:
            super().reject()

    def closeEvent(self, event):
        if self._removing:
            event.ignore()
        else:
            super().closeEvent(event)


class ProcessesDialog(QDialog):
    """Apps, not PIDs: what is running, and a way to close it unless `stop` is None (read-only)."""
    def __init__(self, groups, running, stop=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실행 중 앱")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(1120, 460)
        self.stop, self.groups, self.running, self.busy = stop, [], running, False
        self.stop_buttons = {}
        outer = QVBoxLayout(self)
        self.hint = label(wrap=True)
        outer.addWidget(self.hint)
        self.tree = table(["앱 / 프로세스", "상태", "PID", "메모리", "실행 파일", ""])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate([150, 500, 60, 80]):
            self.tree.setColumnWidth(column, width)
        self.tree.setColumnWidth(5, 72)
        outer.addWidget(self.tree, 1)
        outer.addWidget(label("Orca: 앱 전체(내부 Codex·Claude 포함) / Codex: 앱·CLI / Claude: 데스크톱·Claude Code / Gemini: CLI / Herdr: 앱·core 서버", muted=True, wrap=True))
        row = QHBoxLayout()
        self.stop_all = button("모두 종료", self.stop_everything, row) if stop else None
        row.addStretch()
        button("닫기", self.reject, row)
        outer.addLayout(row)
        self.set_groups(groups, running)

    def set_groups(self, groups, running):
        opened = {self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())
                  if self.tree.topLevelItem(i).isExpanded()}
        self.groups, self.running = groups, running
        self.tree.clear()
        self.stop_buttons = {}
        for group in groups:
            top = QTreeWidgetItem([group.name, group.describe(), "", readable_size(group.memory), ""])
            top.setToolTip(1, group.describe())
            self.tree.addTopLevelItem(top)
            for p in sorted(group.rows, key=lambda r: r["pid"]):
                child = QTreeWidgetItem([p["name"], "", str(p["pid"]), "", p.get("path") or ""])
                child.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                child.setToolTip(4, p.get("path") or "")
                top.addChild(child)
            top.setExpanded(group.name in opened)
            if self.stop:
                widget = QPushButton("종료")
                widget.setToolTip("" if group.stoppable else self.blocked_reason(group))
                widget.clicked.connect(lambda _=False, g=group: self.stop([g]))
                self.tree.setItemWidget(top, 5, widget)
                self.stop_buttons[group.name] = widget
        self.update_hint()
        self.set_busy(self.busy)

    @staticmethod
    def blocked_reason(group):
        if group.holds_self:
            return "이 프로그램이 이 앱 안에서 실행 중이라 함께 꺼집니다. 탐색기에서 실행.cmd로 다시 실행하세요."
        return "프로세스를 확인할 수 없어 종료할 수 없습니다."

    def update_hint(self):
        if not self.groups:
            text = "실행 중인 Codex·Claude·Gemini·Orca·Herdr 프로세스가 없습니다." if not self.running else "실행 상태를 확인하지 못해 모든 앱을 실행 중으로 간주합니다."
        elif not self.stop:
            text = "읽기 전용 모드에서는 앱을 종료할 수 없습니다. 아래 프로그램을 직접 닫으면 잠금이 자동으로 풀립니다."
        else:
            text = "'종료'를 누르면 확인 후 앱을 닫습니다. 앱이 꺼지면 잠금이 자동으로 풀립니다."
            blocked = [g.name for g in self.groups if g.holds_self]
            if blocked:
                text += f"\n{', '.join(blocked)} 안에서 이 프로그램을 실행해 그 앱은 닫을 수 없습니다. 탐색기에서 실행.cmd로 다시 실행하세요."
        self.hint.setText(text)

    def stop_everything(self):
        self.stop([g for g in self.groups if g.stoppable])

    def set_busy(self, busy):
        self.busy = busy
        for group in self.groups:
            widget = self.stop_buttons.get(group.name)
            if widget:
                widget.setEnabled(group.stoppable and not busy)
        if self.stop_all:
            self.stop_all.setEnabled(not busy and any(g.stoppable for g in self.groups))


class ResultDialog(QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        archive, good, failed, notes = result
        self.setWindowTitle("정리 결과")
        self.resize(800, 450)
        outer = QVBoxLayout(self)
        outer.addWidget(label(f"처리 성공 {len(good)}개 / 오류 {len(failed)}개"))
        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.setPlainText((f"백업: {archive}\n" if archive else "백업 없이 처리했습니다.\n") + "".join(f"\n· {n}" for n in notes) + "".join(f"\n✕ {name}: {reason}" for name, reason in failed))
        outer.addWidget(output)
        row = QHBoxLayout()
        row.addStretch()
        button("닫기", self.accept, row)
        outer.addLayout(row)
