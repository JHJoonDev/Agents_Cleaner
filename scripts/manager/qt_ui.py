"""PySide6 implementation of design/option-b2.html. Tk stays as a fallback."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLineEdit, QMainWindow, QPushButton, QSizePolicy,
                               QSplitter, QToolBar, QTreeView, QVBoxLayout, QWidget)

from .model import readable_size
from .qt_logic import Logic
from .qt_models import BYTES, NODE, SEARCH, SORT, RecordModel, RecordProxy, SizeDelegate, exception, item, mark_row, size_item
from .qt_style import configure, icon, label, message


class App(QMainWindow, Logic):
    def __init__(self, roots=None, allow_delete=True, auto_refresh=True):
        super().__init__()
        configure(QApplication.instance())
        self.initialize_logic(roots, allow_delete)
        self.setWindowTitle("AI 기록 정리")
        self.setWindowIcon(icon("app"))
        self.setMinimumSize(1000, 650)
        self.fit_to_screen(1280, 800)
        self._updating = False
        self.actions = {}
        self.build_actions()
        self.build_widgets()
        self.update_actions()
        if auto_refresh:
            QTimer.singleShot(0, self.refresh)

    def fit_to_screen(self, width, height, frame=40):
        # Keep the title bar inside the work area on scaled screens or with a top taskbar.
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(width, height)
            return
        area = screen.availableGeometry()
        width = max(self.minimumWidth(), min(width, area.width() - frame))
        height = max(self.minimumHeight(), min(height, area.height() - frame))
        self.resize(width, height)
        self.move(area.x() + max(0, (area.width() - width) // 2),
                  area.y() + max(0, (area.height() - height - frame) // 2))

    def action(self, key, title, callback, name=None, shortcut=None):
        action = QAction(icon(name) if name else self.windowIcon(), title, self)
        action.triggered.connect(callback)
        if shortcut:
            action.setShortcut(shortcut)
        self.actions[key] = action
        return action

    def build_actions(self):
        self.action("refresh", "새로고침", self.refresh, "refresh", "F5")
        self.action("clear", "선택 해제", self.clear, "clear", "Ctrl+Shift+A")
        self.action("processes", "실행 중 앱", self.show_processes, "process")
        self.action("backups", "백업 관리", self.manage_backups, "backup")
        self.action("errors", "조회 오류", lambda: message(self, "조회 오류", "\n".join(self.errors) or "없음"), "info")
        self.action("review", "선택 항목 검토 → 삭제", self.review, "trash", "Delete")
        self.action("quit", "종료", self.close, shortcut="Ctrl+Q")
        self.action("find", "검색", lambda: self.search.setFocus(), "search", "Ctrl+K")
        self.action("expand", "모두 펼치기", lambda: self.tree.expandAll())
        self.action("collapse", "모두 접기", lambda: self.tree.collapseAll())
        self.action("size", "기록 용량순 정렬", lambda: self.tree.sortByColumn(3, Qt.SortOrder.DescendingOrder))
        self.action("date", "최근 변경순 정렬", lambda: self.tree.sortByColumn(4, Qt.SortOrder.DescendingOrder))
        self.action("about", "프로그램 정보", lambda: message(self, "AI 기록 정리", "AI 앱의 프로젝트·대화·캐시를 선택 정리합니다.\n작업 폴더는 따로 체크했을 때만 휴지통으로 이동합니다.\n계정·설정·프로젝트 메모리는 보존합니다."), "info")
        for name, keys in [("파일(&F)", ["refresh", "review", "quit"]), ("편집(&E)", ["clear", "find"]),
                           ("보기(&V)", ["expand", "collapse", "size", "date"]),
                           ("도구(&T)", ["processes", "backups", "errors"]), ("도움말(&H)", ["about"])]:
            menu = self.menuBar().addMenu(name)
            for key in keys:
                menu.addAction(self.actions[key])

    def build_widgets(self):
        toolbar = QToolBar("도구 모음", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for key in ["refresh", "clear", "processes", "backups", "errors"]:
            toolbar.addAction(self.actions[key])
            if key == "clear":
                toolbar.addSeparator()
        spacer = QWidget()
        spacer.setStyleSheet("background: transparent; border: none;")
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addWidget(label("검색: ", muted=True))
        self.search = QLineEdit()
        self.search.setPlaceholderText("프로젝트, 대화 또는 앱")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.textChanged.connect(self.filter_records)
        toolbar.addWidget(self.search)
        self.addToolBar(toolbar)
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        caption = QHBoxLayout()
        caption.setContentsMargins(11, 4, 11, 4)
        caption.addWidget(label("프로젝트 및 대화"))
        caption.addStretch()
        caption.addWidget(label("크기 막대: 전체 기록 대비", muted=True))
        top_layout.addLayout(caption)
        self.model = RecordModel(self)
        self.proxy = RecordProxy(self)
        self.proxy.setSourceModel(self.model)
        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(4, Qt.SortOrder.DescendingOrder)
        self.tree.setIndentation(17)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in enumerate([410, 90, 75, 170, 190, 175, 165]):
            self.tree.setColumnWidth(col, width)
        self.size_delegate = SizeDelegate(self.tree)
        self.tree.setItemDelegateForColumn(3, self.size_delegate)
        self.tree.clicked.connect(self.clicked)
        self.tree.selectionModel().currentChanged.connect(lambda current, _: self.show_details(current))
        self.model.itemChanged.connect(self.item_changed)
        top_layout.addWidget(self.tree, 1)
        self.splitter.addWidget(top)
        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(11, 5, 11, 7)
        details_layout.setSpacing(4)
        details_layout.addWidget(label("항목 정보", muted=True))
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        self.detail_title, self.detail_app, self.detail_folder, self.detail_store = [label() for _ in range(4)]
        for w in (self.detail_title, self.detail_app, self.detail_folder, self.detail_store):
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for row, (a, wa, b, wb) in enumerate([("대화", self.detail_title, "앱 / 구분", self.detail_app),
                                              ("작업 폴더", self.detail_folder, "AI 저장소", self.detail_store)]):
            grid.addWidget(label(a, muted=True), row, 0)
            grid.addWidget(wa, row, 1)
            grid.addWidget(label(b, muted=True), row, 2)
            grid.addWidget(wb, row, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        details_layout.addLayout(grid)
        self.detail_hint = label(muted=True, wrap=True)
        details_layout.addWidget(self.detail_hint)
        self.splitter.addWidget(details)
        self.splitter.setSizes([520, 120])
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        layout.addWidget(self.splitter, 1)
        summary = QFrame()
        summary.setObjectName("selection")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(12, 7, 12, 7)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.selection_summary = label("선택 없음")
        self.selection_sub = label("작업 폴더 보존", muted=True)
        text.addWidget(self.selection_summary)
        text.addWidget(self.selection_sub)
        summary_layout.addLayout(text, 1)
        self.review_button = QPushButton("선택 항목 검토 → 삭제")
        self.review_button.setIcon(icon("trash"))
        self.review_button.clicked.connect(self.review)
        summary_layout.addWidget(self.review_button)
        layout.addWidget(summary)
        self.setCentralWidget(center)
        self.status_text = label("목록을 읽는 중…")
        self.statusBar().addWidget(self.status_text, 1)
        self.statusBar().addPermanentWidget(label("읽기 전용" if not self.allow_delete else "계정·설정 보존", muted=True))

    def update_actions(self):
        for key, action in self.actions.items():
            if key not in {"quit", "about"}:
                action.setEnabled(not self.busy)
        enabled = self.allow_delete and not self.busy and bool(self.checked)
        self.actions["review"].setEnabled(enabled)
        if hasattr(self, "review_button"):
            self.review_button.setEnabled(enabled)
            self.tree.setEnabled(not self.busy)
            self.search.setEnabled(not self.busy)

    def render(self):
        self._updating = True
        try:
            self.model.removeRows(0, self.model.rowCount())
            self.model.record_items.clear()
            self.model.group_items.clear()
            for group, members in self.groups.items():
                size = sum(e.extra.get("display_size", 0) for e in members)
                latest = max(e.updated for e in members)
                date = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M") if latest else ""
                row = [item(Path(group[2]).name or group[2]), item(group[0]), item(f"{len(members)}개", len(members)),
                       size_item(size), item(date, latest, True), item(), item()]
                node = {"kind": "project", "group": list(group)}
                row[0].setData(node, NODE)
                row[0].setData(" ".join(f"{e.provider} {e.home} {e.project} {e.title}" for e in members).casefold(), SEARCH)
                row[0].setIcon(icon("folder"))
                row[0].setCheckable(True)
                if not any(self.can_select(e) for e in members):
                    row[0].setFlags(row[0].flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                k = self.workspace_of(group, members)
                row[6].setData(node, NODE)
                if k in self.protected:
                    exception(row[6], "보호됨")
                    row[6].setToolTip(self.protected[k])
                elif k:
                    row[6].setText("함께 삭제")
                    row[6].setCheckable(True)
                self.model.appendRow(row)
                self.model.group_items[group] = row
                for e in members:
                    date = datetime.fromtimestamp(e.updated).strftime("%Y-%m-%d %H:%M") if e.updated else ""
                    child = [item(" ".join(e.title.split())[:160]), item(e.provider), item("캐시" if e.kind == "cache" else "대화"),
                             size_item(e.extra.get("display_size", 0), bool(e.files)), item(date, e.updated, True), item(), item()]
                    child[0].setData({"kind": "entry", "key": e.key}, NODE)
                    child[0].setIcon(icon("file"))
                    child[0].setCheckable(True)
                    if not self.can_select(e):
                        child[0].setFlags(child[0].flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    reason = e.blocked or ("앱 종료 후 삭제" if self.is_running(e) else "")
                    exception(child[5], reason)
                    child[0].setToolTip(reason or e.title)
                    row[0].appendRow(child)
                    self.model.record_items[e.key] = child
            for p in self.orca:
                row = [item(p["name"]), item("Orca"), item("등록"), item("—", 0), item("", 0), item("조회 전용"), item("앱에서 등록 해제")]
                row[0].setData({"kind": "orca", "path": p["path"]}, NODE)
                row[0].setData(f"Orca {p['name']} {p['path']}".casefold(), SEARCH)
                row[0].setIcon(icon("folder"))
                self.model.appendRow(row)
        finally:
            self._updating = False
        self.size_delegate.total = sum(e.extra.get("display_size", 0) for e in self.entries)
        self.sync_checks()
        self.filter_records(self.search.text())

    def sync_checks(self):
        if not hasattr(self, "model"):
            return
        self._updating = True
        try:
            for key, row in self.model.record_items.items():
                row[0].setCheckState(Qt.CheckState.Checked if key in self.checked else Qt.CheckState.Unchecked)
                mark_row(row, key in self.checked)
            for group, row in self.model.group_items.items():
                available = [e for e in self.groups[group] if self.can_select(e)]
                count = sum(e.key in self.checked for e in available)
                state = Qt.CheckState.Checked if available and count == len(available) else (Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked)
                row[0].setCheckState(state)
                if row[6].isCheckable():
                    row[6].setCheckState(Qt.CheckState.Checked if self.workspace_of(group, self.groups[group]) in self.workspaces else Qt.CheckState.Unchecked)
        finally:
            self._updating = False
        total = sum(e.extra.get("display_size", 0) for e in self.entries if e.key in self.checked)
        self.selection_summary.setText(f"선택 {len(self.checked)}개 · 기록 파일 {readable_size(total)} (DB 공간 별도)")
        self.selection_sub.setText(f"작업 폴더 {len(self.workspaces)}개 함께 삭제 · 용량은 검토 단계에서 계산" if self.workspaces else "작업 폴더 보존")
        self.update_actions()

    def item_changed(self, value):
        if self._updating:
            return
        node = value.data(NODE) or {}
        state = value.checkState() != Qt.CheckState.Unchecked
        if node.get("kind") == "entry":
            self.set_records_checked([self.entry_by_key[node["key"]]], state)
        elif node.get("kind") == "project":
            group = tuple(node["group"])
            self.toggle_workspace(group, state) if value.column() == 6 else self.set_records_checked(self.groups[group], state, group)

    def filter_records(self, text):
        self.proxy.set_query(text)
        if text:
            self.tree.expandAll()

    def clicked(self, index):
        source = self.proxy.mapToSource(index)
        node = source.siblingAtColumn(0).data(NODE) or {}
        if index.column() == 6 and node.get("kind") == "project":
            group = tuple(node["group"])
            k = self.workspace_of(group, self.groups[group])
            if k in self.protected:
                self.show_details(index)
                self.toggle_workspace(group)
            return
        self.show_details(index)

    def show_details(self, index):
        if not index.isValid():
            return
        source = self.proxy.mapToSource(index).siblingAtColumn(0)
        node = source.data(NODE) or {}
        if node.get("kind") == "entry":
            e = self.entry_by_key[node["key"]]
            self.detail_title.setText(e.title)
            self.detail_app.setText(f"{e.provider} / {'캐시' if e.kind == 'cache' else '대화'}")
            self.detail_folder.setText(e.project)
            self.detail_store.setText(str(e.home))
            self.detail_hint.setText(e.blocked or ("앱 종료 후 삭제" if self.is_running(e) else f"ID: {e.session}"))
        elif node.get("kind") == "project":
            group = tuple(node["group"])
            self.detail_title.setText(Path(group[2]).name or group[2])
            self.detail_app.setText(f"{group[0]} / 프로젝트")
            self.detail_folder.setText(group[2])
            self.detail_store.setText(group[1])
            self.detail_hint.setText("프로젝트 행을 체크하면 소속 대화를 함께 선택합니다.")
        elif node.get("kind") == "orca":
            self.detail_folder.setText(node["path"])
            self.detail_hint.setText("Orca 프로젝트 등록은 조회 전용입니다. 앱에서 등록 해제하세요.")

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
            message(self, "처리 중", "백업·삭제 결과를 확인한 뒤 창을 닫으세요.")
        else:
            event.accept()
