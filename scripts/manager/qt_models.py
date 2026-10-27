"""Qt-only presentation models; no deletion or filesystem mutation."""
from PySide6.QtCore import QSortFilterProxyModel, Qt, QRect
from PySide6.QtGui import QBrush, QColor, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .model import readable_size
from .qt_style import icon

NODE = int(Qt.ItemDataRole.UserRole) + 1
SORT = NODE + 1
BYTES = NODE + 2
SEARCH = NODE + 3
HEADERS = ["프로젝트 / 대화", "앱", "구분", "기록 용량", "최근 변경", "상태", "작업 폴더"]


def item(text="", sort=None, numeric=False):
    value = QStandardItem(str(text))
    value.setEditable(False)
    value.setData(str(text).casefold() if sort is None else sort, SORT)
    if numeric:
        value.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value.setFont(QFont("Consolas", 9))
    return value


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

    def set_query(self, text):
        self.beginFilterChange()
        self.query = text.strip().casefold()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        idx = self.sourceModel().index(row, 0, parent)
        if parent.isValid():
            idx = parent.siblingAtColumn(0)
        return not self.query or self.query in (idx.data(SEARCH) or "")

    def lessThan(self, left, right):
        a, b = left.data(SORT), right.data(SORT)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        return str(a or "").casefold() < str(b or "").casefold()


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
    if text:
        cell.setIcon(icon("lock"))


def size_item(amount, has_files=True):
    value = item(readable_size(amount) if has_files else "DB 항목만", amount, True)
    value.setData(amount if has_files else None, BYTES)
    return value
