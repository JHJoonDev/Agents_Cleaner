"""Lazy project-size tab, separate from AI record selections."""
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
                               QLabel, QTreeWidget, QTreeWidgetItem, QMessageBox, QCheckBox, QDialog, QDialogButtonBox)

from .model import readable_size
from .project_sizes import configured_roots, save_roots, scan_roots, recycle_candidate


class ScanThread(QThread):
    complete = Signal(object, object)

    def __init__(self, roots, parent):
        super().__init__(parent)
        self.roots = roots

    def run(self):
        try:
            self.complete.emit(*scan_roots(self.roots))
        except Exception:
            self.complete.emit([], ["용량 스캔 실패"])


class SizeTab(QWidget):
    def __init__(self, allow_delete, parent=None):
        super().__init__(parent)
        self.allow_delete, self.projects, self.worker = allow_delete, [], None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.roots = QLineEdit(";".join(map(str, configured_roots())))
        self.roots.setPlaceholderText("검색 루트 (여러 개는 ;로 구분)")
        row.addWidget(self.roots, 1)
        self.scan_button = QPushButton("용량 조회")
        self.scan_button.clicked.connect(self.scan)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        self.hint = QLabel("조회 전용 기본값 · 체크한 항목만 확인 후 휴지통 이동 · ToxAuto-* 제외")
        layout.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["프로젝트 / 정리 항목", "전체 용량", "회수 가능", "구분"])
        self.tree.setColumnWidth(0, 580)
        layout.addWidget(self.tree)
        self.delete_button = QPushButton("체크 항목 검토 → 휴지통")
        self.delete_button.setEnabled(allow_delete)
        self.delete_button.clicked.connect(self.review)
        layout.addWidget(self.delete_button)

    def scan(self):
        roots = [Path(x.strip()) for x in self.roots.text().split(";") if x.strip()]
        if not roots or any(not p.is_absolute() for p in roots):
            QMessageBox.warning(self, "검색 루트", "절대 경로를 입력하세요.")
            return
        save_roots(roots)
        self.scan_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.hint.setText("용량 확인 중…")
        self.worker = ScanThread(roots, self)
        self.worker.complete.connect(self.loaded)
        self.worker.start()

    def loaded(self, projects, errors):
        self.projects = projects
        self.tree.clear()
        for p in projects:
            parent = QTreeWidgetItem([p.path.name, readable_size(p.size), readable_size(p.reclaimable), str(p.path)])
            self.tree.addTopLevelItem(parent)
            for c in p.candidates:
                child = QTreeWidgetItem([str(c.path.relative_to(p.path)), readable_size(c.size),
                                         readable_size(c.size) if not c.blocked else "—", c.blocked or c.kind])
                child.setData(0, Qt.ItemDataRole.UserRole, (p, c))
                if self.allow_delete and not c.blocked:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                else:
                    child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                parent.addChild(child)
        self.scan_button.setEnabled(True)
        self.delete_button.setEnabled(self.allow_delete)
        self.hint.setText(f"프로젝트 {len(projects)}개 · 회수 가능 {readable_size(sum(p.reclaimable for p in projects))} · 측정 오류 {len(errors) + sum(len(p.errors) for p in projects)}건")

    def review(self):
        if not self.allow_delete:
            return
        selected = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    selected.append(child.data(0, Qt.ItemDataRole.UserRole))
        if not selected:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("선택 항목 삭제 확인")
        layout = QVBoxLayout(dialog)
        text = QLabel("선택 항목만 백업 없이 휴지통으로 이동합니다.\n" + "\n".join(str(c.path) for _, c in selected))
        text.setWordWrap(True)
        layout.addWidget(text)
        confirm = QCheckBox("선택 경로를 확인했으며 휴지통으로 이동합니다")
        layout.addWidget(confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        confirm.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted or not confirm.isChecked():
            return
        errors = []
        for project, candidate in selected:
            try:
                recycle_candidate(candidate, project)
            except Exception as exc:
                errors.append(f"{candidate.path.name}: {type(exc).__name__}")
        if errors:
            QMessageBox.warning(self, "처리 실패", "\n".join(errors))
        self.scan()

    def close_worker(self):
        if self.worker and self.worker.isRunning():
            return False
        return True
