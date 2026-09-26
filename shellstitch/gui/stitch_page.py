"""Stitch page: choose folders, pick sections, adjust settings, run with progress."""

import html
import os
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import engine
from . import theme, worker
from .common import PathPicker, card, muted, page_header
from .settings_form import SettingsForm

# Where each engine stage sits within one section's work, for the overall progress bar.
STAGES = [("Finding features", 0.00, 0.05), ("Matching photo pairs", 0.05, 0.40),
          ("Checking matches", 0.45, 0.05),
          ("Solving alignment", 0.50, 0.00), ("Loading photos", 0.50, 0.00),
          ("Refining overlaps (1", 0.50, 0.10), ("Refining overlaps (2", 0.60, 0.10),
          ("Correcting exposure", 0.70, 0.02), ("Rendering preview", 0.72, 0.05),
          ("Writing mosaic", 0.77, 0.23)]


def _span(desc):
    for prefix, start, span in STAGES:
        if desc.startswith(prefix):
            return start, span
    return None


class StitchPage(QWidget):
    outputChanged = Signal(str)
    sectionStitched = Signal(str)   # section name, after its outputs were written
    viewResults = Signal(str)       # section name to show on the results page
    runningChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.sections = []
        self.thread = self.worker = None
        self._last_done = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        root.addWidget(page_header(
            "Stitch sections",
            "Choose the folder that holds one subfolder of overlapping photos per shell section, "
            "tick the sections to stitch, and start."))

        # --- folders
        box, lay = card()
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.source = PathPicker("Choose the folder containing your section folders",
                                 "Folder containing one subfolder per section")
        self.output = PathPicker("Choose where to save stitched mosaics", "Where mosaics are saved")
        form.addRow("Photos folder", self.source)
        form.addRow("Output folder", self.output)
        lay.addLayout(form)
        root.addWidget(box)

        # --- sections + settings
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        left, llay = card("Sections")
        bar = QHBoxLayout()
        self.count_label = muted("")
        bar.addWidget(self.count_label, 1)
        for text, mode in (("All", "all"), ("None", "none"), ("Not yet stitched", "new")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, m=mode: self.select(m))
            bar.addWidget(b)
        llay.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Section", "Photos", "Photo size", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.setFocusPolicy(Qt.NoFocus)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._update_count)
        self.tree.itemDoubleClicked.connect(self._open_result)
        llay.addWidget(self.tree, 1)
        self.empty_hint = muted("Choose a photos folder above. Each subfolder with two or more "
                                "photos is listed here as a section.")
        llay.addWidget(self.empty_hint)
        split.addWidget(left)

        right, rlay = card("Settings")
        self.settings = SettingsForm()
        self.settings.setObjectName("ScrollContent")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.settings)
        scroll.setFrameShape(QScrollArea.NoFrame)
        rlay.addWidget(scroll)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.splitter = split
        root.addWidget(split, 1)

        # --- run
        run, runlay = card()
        row = QHBoxLayout()
        self.start_btn = QPushButton("Start stitching")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("Danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.skip_btn = QPushButton("Skip section")
        self.skip_btn.setToolTip("Stop working on the current section and move on to the next one")
        self.skip_btn.setEnabled(False)
        self.skip_btn.clicked.connect(self.skip)
        self.overall_label = QLabel("Ready")
        f = QFont(self.overall_label.font())
        f.setBold(True)
        self.overall_label.setFont(f)
        self.view_btn = QPushButton("View results")
        self.view_btn.setVisible(False)
        self.view_btn.clicked.connect(lambda: self.viewResults.emit(self._last_done or ""))
        self.log_btn = QPushButton("Show log")
        self.log_btn.setCheckable(True)
        row.addWidget(self.start_btn)
        row.addWidget(self.skip_btn)
        row.addWidget(self.cancel_btn)
        row.addSpacing(10)
        row.addWidget(self.overall_label, 1)
        row.addWidget(self.view_btn)
        row.addWidget(self.log_btn)
        runlay.addLayout(row)
        self.overall_bar = QProgressBar()
        self.overall_bar.setTextVisible(False)
        self.overall_bar.setRange(0, 1000)
        runlay.addWidget(self.overall_bar)
        self.stage_label = muted("")
        runlay.addWidget(self.stage_label)
        self.stage_bar = QProgressBar()
        self.stage_bar.setTextVisible(False)
        runlay.addWidget(self.stage_bar)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(140)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log_view.setVisible(False)
        self.log_btn.toggled.connect(self.log_view.setVisible)
        self.log_btn.toggled.connect(lambda on: self.log_btn.setText("Hide log" if on else "Show log"))
        runlay.addWidget(self.log_view)
        root.addWidget(run)

        self._stage_span = None
        self.current = 0
        self._rescan_timer = QTimer(self, singleShot=True, interval=400)
        self._rescan_timer.timeout.connect(self.rescan)
        self.source.changed.connect(self._source_changed)
        self.output.changed.connect(self._output_changed)
        self.settings.changed.connect(self._rescan_timer.start)
        self._set_stage_idle()

    # ------------------------------------------------------------------ state

    def options(self):
        opts = self.settings.options()
        opts.output = self.output.path()
        return opts

    def state(self):
        return {"source": self.source.path(), "output": self.output.path(),
                "options": self.options().to_dict(), "checked": self.checked_names(),
                "splitter": self.splitter.sizes(), "advanced": self.settings.show_advanced.isChecked()}

    def restore(self, st):
        from ..options import Options
        self.settings.set_options(Options.from_dict(st.get("options", {})))
        self.settings.show_advanced.setChecked(bool(st.get("advanced", False)))
        self._restore_checked = set(st.get("checked", []))
        self.output.set_path(st.get("output", ""), notify=False)
        self.source.set_path(st.get("source", ""), notify=False)
        if st.get("splitter"):
            self.splitter.setSizes([int(x) for x in st["splitter"]])
        self.outputChanged.emit(self.output.path())
        self.rescan()

    def restyle(self):
        """Re-colour the section statuses after a theme change (they're coloured in code)."""
        if not self.is_running():
            self.rescan()

    def _source_changed(self, path):
        if path and not self.output.path():
            self.output.set_path(os.path.join(os.path.dirname(os.path.normpath(path)), "stitched"))
        self.rescan()

    def _output_changed(self, path):
        self.outputChanged.emit(path)
        self.rescan()

    # ------------------------------------------------------------------ sections

    def checked_names(self):
        return [self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(i).checkState(0) == Qt.Checked]

    def rescan(self):
        if self.is_running():
            return
        root = self.source.path()
        keep = set(self.checked_names()) or getattr(self, "_restore_checked", set())
        first_scan = not self.sections
        self.sections = []
        if root and os.path.isdir(root):
            try:
                self.sections = engine.find_sections(root, self.settings.options().exclude, self.output.path())
            except OSError as e:
                self.empty_hint.setText(f"Can't read {root}: {e}")
        self.tree.blockSignals(True)
        self.tree.clear()
        for s in self.sections:
            it = QTreeWidgetItem([s["name"], str(s["n_images"]),
                                  "×".join(map(str, s["image_size"])) if s["image_size"] else "?", ""])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setToolTip(0, s["path"])
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            new = self._status_of(it, s)
            checked = s["name"] in keep if keep else (new if first_scan else False)
            it.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        self.tree.blockSignals(False)
        self._restore_checked = set()
        self.empty_hint.setVisible(not self.sections)
        if root and os.path.isdir(root) and not self.sections:
            self.empty_hint.setText("No section folders found here: each section needs its own "
                                    "subfolder with at least two photos.")
        self._update_count()

    def _status_of(self, it, s):
        """Fill in the status column from existing outputs; returns True if not yet stitched."""
        out = engine.output_paths(self.output.path(), s["name"])
        c = theme.colors()
        if os.path.exists(out["tif"]):
            when = datetime.fromtimestamp(os.path.getmtime(out["tif"])).strftime("%d %b %Y")
            self._set_status(it, f"Stitched {when}", c["good"])
            return False
        if os.path.exists(out["preview"]):
            self._set_status(it, "Preview only", c["ok"])
            return True
        self._set_status(it, "Not yet stitched", c["muted"])
        return True

    def _set_status(self, it, text, color, bold=False):
        it.setText(3, text)
        it.setForeground(3, QColor(color))
        f = it.font(3)
        f.setBold(bold)
        it.setFont(3, f)

    def select(self, mode):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            on = mode == "all" or (mode == "new" and not it.text(3).startswith("Stitched"))
            it.setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def _update_count(self, *_):
        n = len(self.checked_names())
        total = self.tree.topLevelItemCount()
        self.count_label.setText(f"{n} of {total} selected" if total else "")
        self.start_btn.setEnabled(n > 0 and not self.is_running())

    def _open_result(self, item, _col):
        if item.text(3).startswith(("Stitched", "Preview", "Done")):
            self.viewResults.emit(item.text(0))

    # ------------------------------------------------------------------ running

    def is_running(self):
        return self.thread is not None

    def start(self):
        names = self.checked_names()
        opts = self.options()
        if not names:
            return
        if not opts.output:
            QMessageBox.warning(self, "No output folder", "Choose an output folder first.")
            return
        try:
            os.makedirs(opts.output, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Output folder", f"Can't create the output folder:\n{e}")
            return
        by_name = {s["name"]: s["path"] for s in self.sections}
        self.jobs = [(n, by_name[n]) for n in names]
        self.rows = {}
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.text(0) in names:
                self.rows[names.index(it.text(0))] = it
                self._set_status(it, "Waiting", theme.colors()["muted"])
        self.results = []
        self.log_view.clear()
        self.overall_bar.setValue(0)
        self.view_btn.setVisible(False)
        self.t_start = time.monotonic()
        self.worker = worker.StitchWorker(self.jobs, opts)
        w = self.worker
        w.log.connect(self._on_log)
        w.stage.connect(self._on_stage)
        w.advance.connect(self._on_advance)
        w.detail.connect(self._on_detail)
        w.sectionStarted.connect(self._on_section_started)
        w.sectionFinished.connect(self._on_section_finished)
        w.finished.connect(self._on_finished)
        self.thread = worker.start(w)
        self._set_running(True)

    def skip(self):
        if self.worker:
            self.worker.skip()
            self.skip_btn.setEnabled(False)
            self.stage_label.setText("Skipping… (finishing the current step)")

    def cancel(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.overall_label.setText("Cancelling… (finishing the current step)")

    def _set_running(self, running):
        for w in (self.source, self.output, self.settings, self.tree):
            w.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.skip_btn.setEnabled(running and len(self.jobs) > 0)
        self.runningChanged.emit(running)
        self._update_count()

    def _set_stage_idle(self):
        self.stage_label.setText("")
        self.stage_bar.setRange(0, 1)
        self.stage_bar.setValue(0)

    def _on_log(self, msg, level):
        if level == "debug":
            self.log_view.appendPlainText(msg)
            return
        c = theme.colors()
        color = {"warning": c["warn"], "error": c["bad"]}.get(level)
        text = html.escape(msg).replace(" ", "&nbsp;")
        self.log_view.appendHtml(f'<span style="color:{color}">{text}</span>' if color else text)

    def _on_section_started(self, idx):
        self.current = idx
        name = self.jobs[idx][0]
        self.overall_label.setText(f"Section {idx + 1} of {len(self.jobs)}: {name}")
        it = self.rows.get(idx)
        if it:
            self._set_status(it, "Stitching…", theme.colors()["accent"], bold=True)
            self.tree.scrollToItem(it)
        self._set_overall(0.0)
        self.skip_btn.setEnabled(True)

    def _on_stage(self, desc, total):
        self.stage_desc, self.stage_total, self.stage_t0 = desc, total, time.monotonic()
        if total:
            self.stage_bar.setRange(0, total)
            self.stage_bar.setValue(0)
            self.stage_label.setText(f"{desc}  ·  0 / {total}")
        else:
            self.stage_bar.setRange(0, 0)  # busy indicator
            self.stage_label.setText(f"{desc}…")
        self._stage_span = _span(desc)
        if self._stage_span:
            self._set_overall(self._stage_span[0])

    def _on_detail(self, detail):
        text = f"{self.stage_desc}…  {detail}"
        if time.monotonic() - self.stage_t0 > 45:
            text += "  ·  taking a while: this section can be skipped"
        self.stage_label.setText(text)

    def _on_advance(self, done):
        total = self.stage_total
        self.stage_bar.setValue(done)
        text = f"{self.stage_desc}  ·  {done} / {total}"
        elapsed = time.monotonic() - self.stage_t0
        if 0 < done < total and elapsed > 2:
            left = elapsed / done * (total - done)
            text += f"  ·  about {int(left // 60)} min {int(left % 60)} s left" if left >= 60 \
                else f"  ·  about {int(left) + 1} s left"
        self.stage_label.setText(text)
        if self._stage_span and total:
            start, span = self._stage_span
            self._set_overall(start + span * done / total)

    def _set_overall(self, frac):
        n = max(1, len(self.jobs))
        self.overall_bar.setValue(int(1000 * (self.current + frac) / n))

    def _on_section_finished(self, idx, res):
        c = theme.colors()
        it = self.rows.get(idx)
        status = res.get("status")
        self.results.append(res)
        if it:
            if status in ("done", "preview"):
                text = "Done" if status == "done" else "Done (preview only)"
                left_out = len(res.get("unplaced", [])) + len(res.get("excluded", []))
                if left_out:
                    text += f" · {left_out} photo{'s' if left_out > 1 else ''} left out"
                self._set_status(it, text, c["warn"] if left_out else c["good"], bold=True)
            elif status == "skipped":
                self._set_status(it, f"Skipped ({res.get('message', '')})", c["muted"])
            elif status == "cancelled":
                self._set_status(it, "Cancelled", c["warn"])
            else:
                self._set_status(it, "Failed: " + res.get("message", ""), c["bad"], bold=True)
                it.setToolTip(3, res.get("message", ""))
        if status in ("done", "preview"):
            self._last_done = res["name"]
            self.sectionStitched.emit(res["name"])
        self.current = idx
        self._set_overall(1.0)

    def _on_finished(self, cancelled):
        # the worker has returned from run(); stop its thread's event loop and join it
        self.thread.quit()
        self.thread.wait()
        self.thread = self.worker = None
        done = sum(r.get("status") in ("done", "preview") for r in self.results)
        failed = sum(r.get("status") == "failed" for r in self.results)
        skipped = sum(r.get("status") == "skipped" for r in self.results)
        elapsed = time.monotonic() - self.t_start
        parts = [f"{done} stitched"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if failed:
            parts.append(f"{failed} failed")
        took = f"{int(elapsed // 60)} min {int(elapsed % 60)} s" if elapsed >= 60 else f"{int(elapsed)} s"
        self.overall_label.setText(("Cancelled: " if cancelled else "Finished: ") + ", ".join(parts)
                                   + f" in {took}")
        if not cancelled:
            self.overall_bar.setValue(1000)
        self._set_stage_idle()
        self.view_btn.setVisible(done > 0)
        if failed and not self.log_view.isVisible():
            self.log_btn.setChecked(True)
        self._set_running(False)
