"""Small shared widgets and helpers."""

import os
import subprocess
import sys

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)


def open_path(path):
    """Open a file or folder with the system's default application."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))


def reveal(path):
    """Show a file selected in Finder / Explorer (or open its folder on Linux)."""
    path = os.path.abspath(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        open_path(os.path.dirname(path) if os.path.isfile(path) else path)


class PathPicker(QWidget):
    """A folder path field with a Browse… button."""

    changed = Signal(str)

    def __init__(self, dialog_title, placeholder="", parent=None):
        super().__init__(parent)
        self.dialog_title = dialog_title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.path()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)

    def path(self):
        return os.path.expanduser(self.edit.text().strip())

    def set_path(self, path, notify=True):
        self.edit.setText(path or "")
        if notify:
            self.changed.emit(self.path())

    def _browse(self):
        start = self.path() if os.path.isdir(self.path()) else os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, self.dialog_title, start)
        if d:
            self.set_path(d)


def card(title=None):
    """A rounded panel; returns (frame, inner layout)."""
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(8)
    if title:
        t = QLabel(title)
        t.setObjectName("CardTitle")
        lay.addWidget(t)
    return frame, lay


def page_header(title, subtitle):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 4)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def muted(text):
    lbl = QLabel(text)
    lbl.setObjectName("Muted")
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl
