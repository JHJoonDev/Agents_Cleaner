"""B2 dialogs. All mutations go through the existing operations API."""
from datetime import datetime
from pathlib import Path
import os
import textwrap

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import operations
from .model import readable_size
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


class ReviewDialog(QDialog):
    requested = Signal(object)

    def __init__(self, selected, plan, destination, codex="", allow_delete=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("삭제 대상 최종 확인")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(880, 700 if plan else 620)
        self.allow_delete = allow_delete
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
        methods = "Codex: 공식 삭제 명령 + 대화 원문·DB 백업. 프로젝트 등록은 보존합니다.\nClaude·Gemini: 휴지통으로 이동. Claude 프로젝트 메모리는 보존합니다. 앱 실행 중에는 삭제를 차단합니다."
        if any(e.provider == "Codex" for e in selected):
            methods += f"\n사용할 Codex CLI: {codex}"
        outer.addWidget(label(methods, muted=True, wrap=True))
        backup_row = QHBoxLayout()
        backup_row.addWidget(label("삭제 전 백업 위치:"))
        self.destination = QLineEdit(str(destination))
        self.destination.setFont(QFont("Consolas", 9))
        backup_row.addWidget(self.destination, 1)
        button("변경…", self.choose_destination, backup_row)
        outer.addLayout(backup_row)
        self.backup_info = label(muted=True)
        outer.addWidget(self.backup_info)
        self.cache_backup = QCheckBox("캐시·로그도 백업")
        caches = [e for e in selected if e.kind == "cache"]
        if caches:
            self.cache_backup.setText(textwrap.fill(f"캐시·로그도 백업 ({readable_size(sum(e.extra.get('display_size', 0) for e in caches))}). 앱이 다시 만드는 파일이라 보통은 필요 없습니다.", width=64))
            outer.addWidget(self.cache_backup)
        else:
            self.cache_backup.hide()
        self.cache_backup.setChecked(False)
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
        self.confirm_button = button("백업 후 선택 기록 삭제", self.confirm, actions)
        self.cancel_button = button("취소", self.reject, actions)
        self.cancel_button.setDefault(True)
        outer.addLayout(actions)
        self.agreed.toggled.connect(self.update_enabled)
        self.moved.toggled.connect(self.update_enabled)
        self.destination.textChanged.connect(self.update_enabled)
        self.destination.editingFinished.connect(self.update_backup_info)
        self.update_backup_info()
        self.update_enabled()

    def choose_destination(self):
        value = QFileDialog.getExistingDirectory(self, "백업 폴더", self.destination.text())
        if value:
            self.destination.setText(value)
            self.update_backup_info()

    def update_backup_info(self):
        try:
            rows = operations.list_backups(Path(self.destination.text()))
            self.backup_info.setText(f"이 위치의 기존 백업 {len(rows)}개 · {readable_size(sum(r[1] for r in rows))} (백업 관리에서 정리)")
        except OSError as exc:
            self.backup_info.setText(f"백업 목록 조회 실패: {exc}")

    def update_enabled(self, *_):
        valid = self.allow_delete and bool(self.selected) and bool(self.destination.text().strip())
        self.confirm_button.setEnabled(valid and self.agreed.isChecked() and (not self.eligible or self.moved.isChecked()))

    def confirm(self):
        self.update_enabled()
        if not self.confirm_button.isEnabled():
            return
        self.requested.emit({"destination": Path(self.destination.text()).expanduser(),
                             "include_caches": self.cache_backup.isChecked(),
                             "workspaces": [i["path"] for i in self.eligible]})
        self.accept()


class BackupDialog(QDialog):
    def __init__(self, destination, allow_delete=True, changed=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("백업 관리")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(800, 470)
        self.allow_delete, self.changed = allow_delete, changed
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
        self.tree.clear()
        self.location.setText(str(self.folder))
        try:
            rows = operations.list_backups(self.folder)
            for path, size, modified in rows:
                row = QTreeWidgetItem([path.name, readable_size(size), datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")])
                row.setData(0, Qt.ItemDataRole.UserRole, str(path))
                row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.tree.addTopLevelItem(row)
            self.summary.setText(f"백업 {len(rows)}개 · 합계 {readable_size(sum(r[1] for r in rows))}. 선택한 백업은 휴지통을 거치지 않고 바로 삭제됩니다.")
        except OSError as exc:
            self.summary.setText(f"조회 실패: {exc}")

    def choose(self):
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
        if not self.allow_delete:
            return
        chosen = [Path(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.tree.selectedItems()]
        if not chosen:
            message(self, "선택 필요", "삭제할 백업을 선택하세요. Ctrl·Shift로 여러 개를 고를 수 있습니다.")
            return
        answer = QMessageBox.question(self, "백업 영구 삭제", f"백업 {len(chosen)}개를 영구 삭제합니다.\n이 백업에 든 기록은 더 이상 복구할 수 없습니다. 계속할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            operations.remove_backups(chosen, self.folder)
        except Exception as exc:
            message(self, "삭제 실패", str(exc), critical=True)
        self.load()


class ProcessesDialog(QDialog):
    def __init__(self, processes, running, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실행 중 앱")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(880, 430)
        outer = QVBoxLayout(self)
        if processes:
            text = "아래 프로그램을 모두 닫은 뒤 '새로고침'을 누르세요.\nOrca 터미널 안에서 실행했다면 Orca를 닫고 탐색기에서 실행.cmd를 다시 실행하세요."
        else:
            text = "실행 중인 Codex·Claude·Gemini·Orca 프로세스가 없습니다." if not running else "실행 상태를 확인하지 못해 모든 앱을 실행 중으로 간주합니다."
        outer.addWidget(label(text, wrap=True))
        self.tree = table(["앱", "프로세스", "PID", "실행 파일"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 85)
        self.tree.setColumnWidth(1, 180)
        self.tree.setColumnWidth(2, 75)
        for p in sorted(processes, key=lambda r: (r["app"], r["pid"])):
            row = QTreeWidgetItem([p["app"], p["name"], str(p["pid"]), p.get("path") or ""])
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.setToolTip(3, p.get("path") or "")
            self.tree.addTopLevelItem(row)
        outer.addWidget(self.tree, 1)
        outer.addWidget(label("Orca: 앱 전체(내부 Codex·Claude 포함) / Codex: 앱·CLI / Claude: 데스크톱·Claude Code / Gemini: CLI", muted=True, wrap=True))
        row = QHBoxLayout()
        row.addStretch()
        button("닫기", self.reject, row)
        outer.addLayout(row)


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
        output.setPlainText(f"백업: {archive}\n" + "".join(f"\n· {n}" for n in notes) + "".join(f"\n✕ {name}: {reason}" for name, reason in failed))
        outer.addWidget(output)
        row = QHBoxLayout()
        row.addStretch()
        button("닫기", self.accept, row)
        outer.addLayout(row)
