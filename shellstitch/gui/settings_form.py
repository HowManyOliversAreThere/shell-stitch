"""Settings form generated from `Options`, so every setting is available with its help text."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..options import GROUPS, Options, default_of, option_fields


class SettingsForm(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets = {}
        self._advanced_rows = []
        self._groups = []  # (group box, number of settings that are always shown)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        self.show_advanced = QCheckBox("Show advanced settings")
        self.show_advanced.toggled.connect(self._update_advanced)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(lambda: self.set_options(Options(output=self.options().output)))
        top.addWidget(self.show_advanced)
        top.addStretch()
        top.addWidget(reset)
        lay.addLayout(top)

        by_group = {g: [] for g in GROUPS}
        for f in option_fields():
            if f.metadata.get("gui", True):
                by_group[f.metadata["group"]].append(f)
        for group in GROUPS:
            if not by_group[group]:
                continue
            box = QGroupBox(group)
            form = QFormLayout(box)
            form.setHorizontalSpacing(14)
            form.setVerticalSpacing(8)
            for f in by_group[group]:
                w, label = self._make_widget(f)
                self._widgets[f.name] = w
                w.setToolTip(f.metadata["help"])
                if label is None:  # checkboxes carry their own label
                    form.addRow(w)
                    row = (form, w, None)
                else:
                    lbl = QLabel(label)
                    lbl.setToolTip(f.metadata["help"])
                    form.addRow(lbl, w)
                    row = (form, w, lbl)
                if f.metadata.get("advanced"):
                    self._advanced_rows.append((box, row))
            n_basic = sum(not f.metadata.get("advanced") for f in by_group[group])
            self._groups.append((box, n_basic))
            lay.addWidget(box)
        lay.addStretch()
        self._update_advanced(False)

    def _make_widget(self, f):
        m, default = f.metadata, default_of(f)
        if isinstance(default, bool):
            w = QCheckBox(m["label"])
            w.toggled.connect(self.changed)
            return w, None
        if "choices" in m:
            w = QComboBox()
            w.addItems(m["choices"])
            w.currentTextChanged.connect(self.changed)
        elif isinstance(default, int):
            w = QSpinBox()
            w.setRange(m.get("min", 0), m.get("max", 10 ** 6))
            w.setSingleStep(m.get("step", 1))
            w.setGroupSeparatorShown(True)
            w.valueChanged.connect(self.changed)
        elif isinstance(default, float):
            w = QDoubleSpinBox()
            w.setDecimals(m.get("decimals", 2))
            w.setRange(m.get("min", 0.0), m.get("max", 1e6))
            w.setSingleStep(m.get("step", 0.1))
            w.valueChanged.connect(self.changed)
        elif isinstance(default, list):
            w = QLineEdit()
            w.setPlaceholderText("patterns separated by ;")
            w.textChanged.connect(self.changed)
        else:
            w = QLineEdit()
            w.textChanged.connect(self.changed)
        w.setMaximumWidth(260)
        return w, m["label"]

    def _update_advanced(self, show):
        for _box, (_form, w, lbl) in self._advanced_rows:
            w.setVisible(show)
            if lbl is not None:
                lbl.setVisible(show)
        # hide groups left with nothing to show (e.g. Performance when advanced settings are hidden)
        for box, n_basic in self._groups:
            box.setVisible(show or n_basic > 0)

    def set_options(self, opts):
        for name, w in self._widgets.items():
            v = getattr(opts, name)
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(v)
            elif isinstance(w, QComboBox):
                w.setCurrentText(v)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.setValue(v)
            elif isinstance(v, list):
                w.setText("; ".join(v))
            else:
                w.setText(str(v))
            w.blockSignals(False)
        self._output = opts.output
        self.changed.emit()

    def options(self):
        opts = Options(output=getattr(self, "_output", Options().output))
        for name, w in self._widgets.items():
            if isinstance(w, QCheckBox):
                v = w.isChecked()
            elif isinstance(w, QComboBox):
                v = w.currentText()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                v = w.value()
            elif isinstance(getattr(opts, name), list):
                v = [p.strip() for p in w.text().split(";") if p.strip()]
            else:
                v = w.text()
            setattr(opts, name, v)
        return opts
