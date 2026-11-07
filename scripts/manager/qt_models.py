"""Qt presentation models and the shared background task wrapper."""
from datetime import datetime
from PySide6.QtCore import QObject, QRunnable, QSortFilterProxyModel, Qt, QRect, Signal, QEvent
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .model import readable_size
from .qt_style import icon

NODE = int(Qt.ItemDataRole.UserRole) + 1
SORT = NODE + 1
BYTES = NODE + 2
SEARCH = NODE + 3
HEADERS = ["프로젝트 / 대화", "앱", "구분", "기록 용량", "최근 변경", "상태", "작업 폴더"]


class Signals(QObject):
    done = Signal(str, object, object)


class Task(QRunnable):
    """Always report termination, including SystemExit in third-party readers."""
    def __init__(self, function, event):
        super().__init__()
        self.function, self.event, self.signals = function, event, Signals()

    def run(self):
        try:
            result, error = self.function(), None
        except BaseException as exc:
            result, error = None, str(exc) or type(exc).__name__
        self.signals.done.emit(self.event, result, error)


def item(text="", sort=None, numeric=False):
    value = QStandardItem(str(text))
    value.setEditable(False)
    value.setData(str(text).casefold() if sort is None else sort, SORT)
    if numeric:
        value.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value.setFont(QFont("Consolas", 9))
    return value


def display_date(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else ""
    except (OverflowError, OSError, ValueError):
        return "날짜 오류"


class RecordModel(QStandardItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalHeaderLabels(HEADERS)
        self.record_items = {}
        self.group_items = {}


class RecordProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.setSortRole(SORT)
        self.setFilterRole(SEARCH)

    def set_query(self, text):
        self.beginFilterChange()
        self.query = text.strip().casefold()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        if parent.isValid():
            # The parent already passed the group search. Do not search its
            # full conversation text again for each of its children.
            return True
        idx = self.sourceModel().index(row, 0, parent)
        return not self.query or self.query in (idx.data(SEARCH) or "")

    def lessThan(self, left, right):
        a, b = left.data(SORT), right.data(SORT)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        return str(a or "").casefold() < str(b or "").casefold()


class FolderCheckDelegate(QStyledItemDelegate):
    """Make the folder checkbox and its label one click target."""
    def editorEvent(self, event, model, option, index):
        if index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                return event.button() == Qt.MouseButton.LeftButton
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if not index.flags() & Qt.ItemFlag.ItemIsEnabled:
                    return False
                state = index.data(Qt.ItemDataRole.CheckStateRole)
                checked = state in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                return model.setData(index, Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
                                     Qt.ItemDataRole.CheckStateRole)
        return super().editorEvent(event, model, option, index)


class SizeDelegate(QStyledItemDelegate):
    """56 x 7px ratio track left of the right-aligned number."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.total = 0

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        amount = index.data(BYTES)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if amount is not None and self.total:
            track = QRect(opt.rect.left() + 9, opt.rect.center().y() - 3, 56, 7)
            painter.fillRect(track, QColor("#30373e"))
            width = max(1, round(56 * min(1, amount / self.total))) if amount else 0
            painter.fillRect(QRect(track.x(), track.y(), width, 7), QColor("#9ab7ce" if selected else "#6b8da6"))
        rect = opt.rect.adjusted(76, 0, -9, 0) if amount is not None else opt.rect.adjusted(5, 0, -9, 0)
        painter.setFont(QFont("Consolas", 9))
        painter.setPen(QColor("#e4eaf0" if selected else "#d6d8dc"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


def mark_row(row, checked):
    for cell in row:
        cell.setBackground(QBrush(QColor("#274963")) if checked else QBrush())


def exception(cell, text):
    cell.setText(text)
    cell.setData(text.casefold(), SORT)
    cell.setForeground(QBrush(QColor("#9da2aa")))
    # Locks now lift while the row stays on screen, so the icon must be able to go too.
    cell.setIcon(icon("lock") if text else QIcon())


def size_item(amount, has_files=True):
    value = item(readable_size(amount) if has_files else "DB 항목만", amount, True)
    value.setData(amount if has_files else None, BYTES)
    return value
