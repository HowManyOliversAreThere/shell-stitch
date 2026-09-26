"""Shell Stitch desktop app."""

import json
import os
import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QPushButton,
                               QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from .. import __version__
from . import theme
from .common import page_header
from .results_page import ResultsPage
from .stitch_page import StitchPage

APP_NAME = "Shell Stitch"


def resource(name):
    base = getattr(sys, "_MEIPASS", None)  # inside a PyInstaller build
    if base:
        return os.path.join(base, "shellstitch", "gui", "resources", name)
    return os.path.join(os.path.dirname(__file__), "resources", name)


ABOUT = f"""
<p><b>{APP_NAME} {__version__}</b>: automatic stitching of overlapping microscope photos of shell
sections into one full-resolution, calibrated mosaic for sclerochronology.</p>
<h3>How it works</h3>
<ol>
<li><b>Feature matching</b>: every photo is matched against every other, so capture order and scan
pattern don't matter.</li>
<li><b>Global alignment</b>: all matches are solved together. The sample moves rigidly (rotation and
translation, no scaling, so distances stay measurable). The camera's slight tilt and lens distortion
are shared by all photos and estimated at the same time. False matches are discarded.</li>
<li><b>Full-resolution refinement</b>: each overlap is re-measured at full resolution.</li>
<li><b>Illumination correction</b>: uneven lighting and exposure differences are evened out.</li>
<li><b>Blending</b>: photos are feathered together and written as a TIFF with the microscope's
µm-per-pixel calibration.</li>
</ol>
<h3>Outputs (per section)</h3>
<ul>
<li><code>NAME.tif</code>: full-resolution mosaic</li>
<li><code>NAME_preview.jpg</code>, <code>NAME_layout.jpg</code>: previews, with and without photo outlines</li>
<li><code>NAME_report.json</code>: alignment details (shown on the Results page)</li>
</ul>
<p>The same engine is available from the command line: <code>shellstitch --help</code>.</p>
<h3>License</h3>
<p>{APP_NAME} is open-source software released under the MIT License.<br>
Copyright © 2026 Oliver Robson.</p>
<p>It is built on these open-source components, used under their own licenses:</p>
<ul>
<li>Qt 6 and PySide6: LGPL-3.0 (<a href="https://www.qt.io/licensing/open-source-lgpl-obligations">details</a>)</li>
<li>OpenCV: Apache-2.0</li>
<li>NumPy and SciPy: BSD-3-Clause</li>
<li>tifffile: BSD-3-Clause</li>
<li>tqdm: MPL-2.0 and MIT</li>
</ul>
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1000, 680)
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(200)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 12)
        sl.setSpacing(0)
        t = QLabel(APP_NAME)
        t.setObjectName("AppTitle")
        s = QLabel("Microscope mosaic stitching")
        s.setObjectName("AppSubtitle")
        sl.addWidget(t)
        sl.addWidget(s)

        self.stack = QStackedWidget()
        self.stitch = StitchPage()
        self.results = ResultsPage()
        about = QWidget()
        about.setObjectName("Page")
        al = QVBoxLayout(about)
        al.setContentsMargins(24, 20, 24, 20)
        al.addWidget(page_header(f"About {APP_NAME}", f"Version {__version__}"))
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(ABOUT)
        al.addWidget(tb, 1)

        self.nav = QButtonGroup(self)
        for i, (name, page) in enumerate((("Stitch", self.stitch), ("Results", self.results),
                                          ("About", about))):
            b = QPushButton(name)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.nav.addButton(b, i)
            sl.addWidget(b)
            self.stack.addWidget(page)
        self.nav.idClicked.connect(self.go)
        sl.addStretch()
        self.version = QLabel(f"v{__version__}")
        self.version.setObjectName("AppSubtitle")
        sl.addWidget(self.version)

        lay.addWidget(side)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # keep the output folder in step between the two pages
        self.stitch.outputChanged.connect(self.results.set_output_dir)
        self.results.outputChanged.connect(lambda p: self.stitch.output.set_path(p) if p != self.stitch.output.path() else None)
        self.stitch.sectionStitched.connect(lambda name: self.results.refresh(keep=True))
        self.stitch.viewResults.connect(self.show_result)
        self.stitch.runningChanged.connect(self._running_changed)

        self.settings = QSettings()
        self._restore()
        self.go(0)

    def go(self, idx):
        self.nav.button(idx).setChecked(True)
        self.stack.setCurrentIndex(idx)

    def show_result(self, name):
        self.results.set_output_dir(self.stitch.output.path())
        self.results.show_section(name)
        self.go(1)

    def _running_changed(self, running):
        self.setWindowTitle(f"{APP_NAME}: stitching…" if running else APP_NAME)

    def _restore(self):
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(min(1400, int(screen.width() * 0.85)), min(900, int(screen.height() * 0.85)))
        try:
            st = json.loads(self.settings.value("stitch", "{}"))
        except (TypeError, ValueError):
            st = {}
        self.stitch.restore(st)
        self.results.set_output_dir(self.stitch.output.path())

    def closeEvent(self, event):
        if self.stitch.is_running():
            from PySide6.QtWidgets import QMessageBox
            answer = QMessageBox.question(self, "Stitching in progress",
                                          "Stitching is still running. Cancel it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.stitch.cancel()
            self.stitch.thread.quit()
            self.stitch.thread.wait()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("stitch", json.dumps(self.stitch.state()))
        event.accept()


def main():
    if "--self-test" in sys.argv:
        from ..selftest import run
        sys.exit(run(gui=True))
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("ShellStitch")
    QApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource("icon.png")))
    theme.apply(app)
    app.styleHints().colorSchemeChanged.connect(lambda _: theme.apply(app))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
