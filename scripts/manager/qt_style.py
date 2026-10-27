"""B2 palette and local vector icons; no web or image dependencies."""
from functools import lru_cache
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QRect
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QProxyStyle, QStyle, QStyleFactory

PATHS = {
    "folder": '<path d="M3 6h7l2 2h9v12H3z"/>',
    "file": '<path d="M6 3h8l4 4v14H6zM14 3v5h4"/>',
    "refresh": '<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
    "clear": '<path d="M5 5h14v14H5zM9 12h6"/>',
    "process": '<path d="M2 12h5l3-7 4 14 3-7h5"/>',
    "backup": '<path d="M4 4h16v5H4zM5 9v11h14V9M10 13h4"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    "lock": '<rect x="5" y="10" width="14" height="11" rx="1"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "warning": '<path d="m12 3 10 18H2zM12 9v5M12 17h.01"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    "app": '<rect x="3" y="3" width="18" height="18" rx="1"/><path d="M3 8h18M8 8v13"/>',
}

STYLE = """
QMainWindow,QDialog,QWidget { background:#252629; color:#d6d8dc; }
QMenuBar,QToolBar,QStatusBar { background:#2c2e32; border:0; }
QMenuBar { border-bottom:1px solid #42454b; }
QMenuBar::item { padding:4px 10px; background:transparent; }
QMenuBar::item:selected,QMenu::item:selected { background:#34556e; }
QMenu { background:#303338; border:1px solid #626870; padding:3px; }
QMenu::item { padding:5px 25px 5px 10px; }
QToolBar { spacing:3px; padding:4px 7px; border-bottom:1px solid #44474d; }
QToolBar::separator { background:#4b4e54; width:1px; margin:4px 5px; }
QToolButton { background:transparent; border:1px solid transparent; border-radius:2px; padding:5px 8px; }
QToolButton:hover { border:1px solid #626870; background:#3a3e44; }
QPushButton { min-height:27px; padding:0 12px; border:1px solid #565b62; border-radius:3px; background:#34373c; }
QPushButton:hover { background:#3a3e44; border-color:#7c828b; }
QPushButton:default { border:1px solid #668cad; }
QPushButton:disabled { color:#7c8087; border-color:#44474c; background:#303236; }
QLineEdit { background:#202124; border:1px solid #5b5e65; border-radius:2px; min-height:26px; padding:0 7px; }
QLineEdit:focus { border-color:#78a5cf; }
QTreeView,QTreeWidget,QPlainTextEdit { background:#202124; border:1px solid #42454b; border-radius:0; selection-background-color:#315d7d; selection-color:#e4eaf0; }
QTreeView::item { min-height:26px; border:0; }
QTreeView::item:selected { background:#315d7d; color:#e4eaf0; }
QTreeView::item:hover:!selected { background:#2d333b; }
QHeaderView::section { background:#303237; color:#bdc1c8; border:0; border-right:1px solid #45484e; border-bottom:1px solid #555960; padding:5px 9px; min-height:18px; }
QAbstractScrollArea::corner { background:#303237; }
QSplitter::handle { background:#383b40; height:5px; }
QGroupBox { border:1px solid #50545c; border-radius:2px; margin-top:9px; padding:11px; }
QGroupBox::title { subcontrol-origin:margin; left:9px; padding:0 5px; }
QLabel#muted { color:#9da2aa; }
QLabel#warning { color:#d6b779; }
QLabel#problem { color:#e29494; }
QFrame#selection { background:#2a2c30; border-top:1px solid #44474d; }
QStatusBar::item { border:0; border-right:1px solid #4a4e54; }
QCheckBox { spacing:7px; background:transparent; }
QCheckBox::indicator { width:13px; height:13px; }
QScrollBar:vertical { width:9px; background:#25272a; }
QScrollBar:horizontal { height:9px; background:#25272a; }
QScrollBar::handle { background:#555b64; min-height:22px; min-width:22px; border-radius:3px; }
QScrollBar::add-line,QScrollBar::sub-line { width:0; height:0; }
QToolTip { background:#33383e; border:1px solid #717985; color:#d6d8dc; padding:5px; }
"""


@lru_cache(maxsize=1)
def load_offscreen_fonts():
    # The Windows offscreen plugin has an empty font database. Use installed OS fonts,
    # without downloading, bundling, or copying licensed font files.
    root = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
    for name in ("segoeui.ttf", "malgun.ttf", "consola.ttf"):
        path = root / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))


class CheckStyle(QProxyStyle):
    """Keep unchecked boxes visible on the dark palette, without image assets."""
    def drawPrimitive(self, element, option, painter, widget=None):
        if element not in (QStyle.PrimitiveElement.PE_IndicatorCheckBox,
                            QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck):
            return super().drawPrimitive(element, option, painter, widget)
        painter.save()
        center = option.rect.center()
        box = QRect(center.x() - 6, center.y() - 6, 13, 13)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        on = bool(option.state & QStyle.StateFlag.State_On)
        partial = bool(option.state & QStyle.StateFlag.State_NoChange)
        painter.setPen(QColor("#7c8794" if enabled else "#4e555e"))
        painter.setBrush(QColor("#35566f" if on or partial else "#202124"))
        painter.drawRect(box.adjusted(0, 0, -1, -1))
        painter.setPen(QPen(QColor("#dfe8f0" if enabled else "#7c8794"), 1.5))
        if on:
            painter.drawLine(box.left() + 3, box.top() + 6, box.left() + 5, box.top() + 9)
            painter.drawLine(box.left() + 5, box.top() + 9, box.left() + 10, box.top() + 3)
        elif partial:
            painter.drawLine(box.left() + 3, box.top() + 6, box.left() + 9, box.top() + 6)
        painter.restore()


def configure(app: QApplication):
    if app.platformName() == "offscreen" and os.name == "nt":
        load_offscreen_fonts()
    app.setStyle(CheckStyle(QStyleFactory.create("Fusion")))
    font = QFont("Segoe UI", 9)
    font.setFamilies(["Segoe UI", "Malgun Gothic", "맑은 고딕"])
    app.setFont(font)
    palette = QPalette()
    colors = {"Window": "#252629", "WindowText": "#d6d8dc", "Base": "#202124",
              "AlternateBase": "#26282c", "Text": "#d6d8dc", "Button": "#34373c",
              "ButtonText": "#d6d8dc", "Highlight": "#315d7d", "HighlightedText": "#e4eaf0",
              "ToolTipBase": "#33383e", "ToolTipText": "#d6d8dc", "Link": "#8eb4d3"}
    for role, color in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#858b95"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)


@lru_cache(maxsize=32)
def icon(name, color="#adb6c2"):
    source = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="square" stroke-linejoin="miter">{PATHS[name]}</svg>'
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(source.encode())).render(painter)
    painter.end()
    return QIcon(pixmap)


def label(text="", muted=False, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if muted:
        widget.setObjectName("muted")
    return widget


def message(parent, title, text, critical=False):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(str(text))
    box.setIcon(QMessageBox.Icon.Critical if critical else QMessageBox.Icon.Information)
    box.exec()
