"""Preferences page: options for the app itself (stitching settings live on the Stitch page)."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
from .common import card, muted, page_header


class PreferencesPage(QWidget):
    themeChanged = Signal(str)  # system / light / dark

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        root.addWidget(page_header("Preferences", "Options for the app itself. Stitching settings are on "
                                                  "the Stitch page."))

        box, lay = card("Appearance")
        row = QHBoxLayout()
        row.addWidget(QLabel("Theme"))
        row.addSpacing(12)
        self.group = QButtonGroup(self)
        for key, label in theme.MODES.items():
            b = QPushButton(label)
            b.setObjectName("Segment")
            b.setCheckable(True)
            b.setProperty("mode", key)
            self.group.addButton(b)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(muted("System follows your computer's light or dark mode, and changes with it."))
        self.set_mode(theme.mode())
        self.group.buttonClicked.connect(lambda b: self.themeChanged.emit(b.property("mode")))
        root.addWidget(box)
        root.addStretch()

    def set_mode(self, mode):
        for b in self.group.buttons():
            b.setChecked(b.property("mode") == mode)
