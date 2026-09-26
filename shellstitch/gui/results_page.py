"""Results page: browse stitched sections, their mosaics and human-readable reports."""

import os

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .. import report as rpt
from . import theme
from .common import PathPicker, card, muted, open_path, page_header, reveal
from .image_view import ImageView


class NumItem(QTableWidgetItem):
    """Table cell that shows formatted text but sorts by its numeric value."""

    def __init__(self, value, text=None):
        super().__init__(text if text is not None else str(value))
        self.value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, NumItem):
            return self.value < other.value
        return super().__lt__(other)


def thumbnail(path, size=112):
    reader = QImageReader(path)
    s = reader.size()
    if s.isValid():
        s.scale(size * 2, size * 2, Qt.KeepAspectRatio)
        reader.setScaledSize(s)
    img = reader.read()
    return QPixmap.fromImage(img) if not img.isNull() else QPixmap()


def make_table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class ResultsPage(QWidget):
    outputChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.reports = []
        self.current = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        root.addWidget(page_header("Results", "Stitched sections in the output folder. Select one to "
                                              "inspect the mosaic and its alignment report."))
        box, lay = card()
        row = QHBoxLayout()
        row.addWidget(QLabel("Output folder"))
        self.folder = PathPicker("Choose an output folder to browse", "Folder with stitched results")
        self.folder.changed.connect(self._folder_changed)
        row.addWidget(self.folder, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh(keep=True))
        row.addWidget(refresh)
        lay.addLayout(row)
        root.addWidget(box)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.list = QListWidget()
        self.list.setIconSize(QSize(112, 64))
        self.list.setSpacing(2)
        self.list.setMinimumWidth(280)
        self.list.setMaximumWidth(380)
        self.list.currentRowChanged.connect(self._select)
        split.addWidget(self.list)

        self.detail_stack = QStackedWidget()
        self.empty = muted("No stitched sections here yet. Stitch some on the Stitch page, or choose "
                           "another output folder.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.detail_stack.addWidget(self.empty)
        self.detail_stack.addWidget(self._build_detail())
        split.addWidget(self.detail_stack)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        self.splitter = split

    # ------------------------------------------------------------------ detail panel

    def _build_detail(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("PageTitle")
        self.subtitle = muted("")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.open_tif = QPushButton("Open full-resolution TIFF")
        self.open_tif.setObjectName("Primary")
        self.open_tif.clicked.connect(lambda: self._open("tif"))
        show = QPushButton("Show in folder")
        show.clicked.connect(self._reveal)
        export = QPushButton("Export report…")
        export.clicked.connect(self._export)
        for b in (self.open_tif, show, export):
            head.addWidget(b, 0, Qt.AlignTop)
        lay.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # mosaic tab
        mosaic = QWidget()
        ml = QVBoxLayout(mosaic)
        ml.setContentsMargins(0, 8, 0, 0)
        tools = QHBoxLayout()
        self.outlines = QCheckBox("Show photo outlines")
        self.outlines.toggled.connect(lambda: self._show_image(keep_view=True))
        fit = QPushButton("Fit")
        fit.clicked.connect(lambda: self.view.fit())
        one = QPushButton("100%")
        one.clicked.connect(lambda: self.view.actual_size())
        zin, zout = QPushButton("+"), QPushButton("−")
        zin.clicked.connect(lambda: self.view.zoom_by(1.25))
        zout.clicked.connect(lambda: self.view.zoom_by(0.8))
        self.zoom_label = muted("")
        self.pos_label = muted("")
        for b in (fit, one, zout, zin):
            b.setFixedWidth(b.sizeHint().width())
        tools.addWidget(self.outlines)
        tools.addStretch()
        tools.addWidget(self.pos_label)
        tools.addSpacing(12)
        tools.addWidget(self.zoom_label)
        for b in (zout, zin, fit, one):
            tools.addWidget(b)
        ml.addLayout(tools)
        self.view = ImageView()
        self.view.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f}% of preview"))
        self.view.cursorMoved.connect(self.pos_label.setText)
        ml.addWidget(self.view, 1)
        ml.addWidget(muted("This is the preview. Scroll to zoom, drag to pan, double-click to fit. "
                           "The full-resolution TIFF opens in your default image app."))
        self.tabs.addTab(mosaic, "Mosaic")

        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(True)
        self.tabs.addTab(self.summary, "Summary")

        self.photos = make_table(["Photo", "Centre x", "Centre y", "Rotation (°)", "Brightness",
                                  "Overlaps", "Worst error (px)"])
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 8, 0, 0)
        self.photos_note = muted("")
        pl.addWidget(self.photos_note)
        pl.addWidget(self.photos, 1)
        self.tabs.addTab(pw, "Photos")

        self.overlaps = make_table(["Photo A", "Photo B", "Matches", "Error (px)", "Error (µm)"])
        ow = QWidget()
        ol = QVBoxLayout(ow)
        ol.setContentsMargins(0, 8, 0, 0)
        self.overlaps_note = muted("")
        ol.addWidget(self.overlaps_note)
        ol.addWidget(self.overlaps, 1)
        self.tabs.addTab(ow, "Overlaps")
        lay.addWidget(self.tabs, 1)
        return w

    # ------------------------------------------------------------------ data

    def output_dir(self):
        return self.folder.path()

    def set_output_dir(self, path):
        if path != self.folder.path():
            self.folder.set_path(path, notify=False)
            self.refresh()

    def _folder_changed(self, path):
        self.outputChanged.emit(path)
        self.refresh()

    def refresh(self, keep=False, select=None):
        name = select or (self.current["section"] if keep and self.current else None)
        self.reports = rpt.find_reports(self.output_dir())
        self.list.blockSignals(True)
        self.list.clear()
        c = theme.colors()
        for r in self.reports:
            label, _, level = rpt.verdict(r)
            total = r.get("n_photos", len(r["images"]))
            lines = [r["section"], f"{len(r['images'])}/{total} photos · {label.split()[0]}"]
            if r.get("um_per_px"):
                lines.append(f"{r['width_px'] * r['um_per_px'] / 1000:.1f} × "
                             f"{r['height_px'] * r['um_per_px'] / 1000:.1f} mm")
            it = QListWidgetItem("\n".join(lines))
            it.setIcon(QIcon(thumbnail(r["_outputs"]["preview"])))
            it.setToolTip(r["_path"])
            if level in ("warn", "bad") or r.get("unplaced"):
                it.setForeground(QColor(c["warn"]))
            it.setSizeHint(QSize(0, 76))
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.detail_stack.setCurrentIndex(1 if self.reports else 0)
        idx = next((i for i, r in enumerate(self.reports) if r["section"] == name), 0)
        if self.reports:
            self.list.setCurrentRow(idx)
            self._select(idx)
        else:
            self.current = None

    def show_section(self, name):
        self.refresh(select=name)

    def _select(self, idx):
        if not 0 <= idx < len(self.reports):
            return
        prev = self.current["section"] if self.current else None
        r = self.current = self.reports[idx]
        self.title.setText(r["section"])
        self.subtitle.setText(rpt.size_text(r) + ("  ·  " + r["folder"] if r.get("folder") else ""))
        has_tif = os.path.exists(r["_outputs"]["tif"])
        self.open_tif.setEnabled(has_tif)
        self.open_tif.setToolTip("" if has_tif else "Only the preview was written for this section")
        self._show_image(keep_view=r["section"] == prev)
        c = theme.colors()
        self.summary.setHtml(rpt.to_html(r, c))
        self._fill_photos(r)
        self._fill_overlaps(r)

    def _show_image(self, keep_view=False):
        r = self.current
        if not r:
            return
        path = r["_outputs"]["layout" if self.outlines.isChecked() else "preview"]
        units = None
        if r.get("um_per_px"):
            prev_w = r.get("preview_width_px") or QImageReader(path).size().width() or r["width_px"]
            units = r["um_per_px"] / 1000 * r["width_px"] / prev_w  # mm per preview pixel
        self.view.set_background(theme.colors()["viewer"])
        self.view.set_image(path if os.path.exists(path) else None, units, keep_view=keep_view)

    def _fill_photos(self, r):
        rows = rpt.photo_rows(r)
        unit = "mm" if r.get("um_per_px") else "px"
        self.photos.setHorizontalHeaderLabels(["Photo", f"Centre x ({unit})", f"Centre y ({unit})",
                                               "Rotation (°)", "Brightness", "Overlaps", "Worst error (px)"])
        t = self.photos
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        dec = 2 if unit == "mm" else 0
        for i, p in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(p["file"]))
            t.setItem(i, 1, NumItem(p["x"], f"{p['x']:.{dec}f}"))
            t.setItem(i, 2, NumItem(p["y"], f"{p['y']:.{dec}f}"))
            t.setItem(i, 3, NumItem(p["rotation"], f"{p['rotation']:.2f}"))
            t.setItem(i, 4, NumItem(p["brightness"], f"{p['brightness']:+.1f}%"))
            t.setItem(i, 5, NumItem(p["overlaps"]))
            t.setItem(i, 6, NumItem(p["worst"], f"{p['worst']:.2f}"))
        t.setSortingEnabled(True)
        t.sortItems(0, Qt.AscendingOrder)
        note = (f"{len(rows)} photos placed. Positions are photo centres in the mosaic; rotation is "
                "relative to the reference photo; brightness is the exposure correction applied.")
        if r.get("unplaced"):
            note += f" Left out: {', '.join(r['unplaced'])}."
        self.photos_note.setText(note)

    def _fill_overlaps(self, r):
        c = theme.colors()
        flagged = {(o["a"], o["b"]) for o in rpt.flagged_overlaps(r)}
        ov = sorted(r.get("overlaps", []), key=lambda o: -o["error_px"])
        um = r.get("um_per_px")
        t = self.overlaps
        t.setSortingEnabled(False)
        t.setRowCount(len(ov))
        for i, o in enumerate(ov):
            cells = [QTableWidgetItem(o["a"]), QTableWidgetItem(o["b"]), NumItem(o["matches"]),
                     NumItem(o["error_px"], f"{o['error_px']:.2f}"),
                     NumItem(o["error_px"] * um if um else 0, f"{o['error_px'] * um:.1f}" if um else "")]
            for j, cell in enumerate(cells):
                if (o["a"], o["b"]) in flagged:
                    cell.setForeground(QColor(c["warn"]))
                t.setItem(i, j, cell)
        t.setSortingEnabled(True)
        t.sortItems(3, Qt.DescendingOrder)
        note = (f"{len(ov)} overlapping pairs were used. Error is how far the same features in the two "
                f"photos disagree after alignment (typical: {rpt.error_text(r, r['median_error_px'])}).")
        if flagged:
            note += f" {len(flagged)} highlighted pair(s) align noticeably worse than the rest."
        self.overlaps_note.setText(note)

    # ------------------------------------------------------------------ actions

    def _open(self, key):
        if self.current:
            path = self.current["_outputs"][key]
            if os.path.exists(path):
                open_path(path)

    def _reveal(self):
        if self.current:
            o = self.current["_outputs"]
            reveal(o["tif"] if os.path.exists(o["tif"]) else o["preview"])

    def _export(self):
        if not self.current:
            return
        r = self.current
        default = os.path.join(self.output_dir(), f"{r['section']}_report.html")
        path, _ = QFileDialog.getSaveFileName(self, "Export report", default, "HTML page (*.html)")
        if not path:
            return
        try:
            rpt.export_html(r, path)
        except OSError as e:
            QMessageBox.warning(self, "Export report", f"Couldn't save the report:\n{e}")
            return
        QTimer.singleShot(0, lambda: open_path(path))
