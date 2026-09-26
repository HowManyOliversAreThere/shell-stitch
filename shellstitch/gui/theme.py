"""Look and feel: Qt's cross-platform Fusion style with a light/dark palette that follows the OS."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

ACCENT = "#1f7a8c"
ACCENT_DARK = "#4fb3c8"

LIGHT = {
    "window": "#f3f5f6", "base": "#ffffff", "alt": "#f7f9fa", "text": "#1d2327", "muted": "#6b7280",
    "button": "#ffffff", "line": "#dde2e5", "sidebar": "#e9edef", "card": "#ffffff",
    "good": "#1a7f4b", "ok": "#1f6f8b", "warn": "#a86100", "bad": "#b42318", "accent": ACCENT,
    "accent_text": "#ffffff", "viewer": "#2b2f33", "frame": "#9aa3ab",
}
DARK = {
    "window": "#1e2124", "base": "#26292d", "alt": "#2b2f33", "text": "#e6e8ea", "muted": "#9aa3ab",
    "button": "#2f3337", "line": "#3a3f44", "sidebar": "#181a1d", "card": "#26292d",
    "good": "#4cc38a", "ok": "#5cb8d6", "warn": "#f0a54a", "bad": "#f07167", "accent": ACCENT_DARK,
    "accent_text": "#0b1d22", "viewer": "#141618", "frame": "#7d868f",
}


def is_dark():
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def colors():
    return DARK if is_dark() else LIGHT


def apply(app):
    app.setStyle("Fusion")
    c = colors()
    p = QPalette()
    roles = {
        QPalette.Window: c["window"], QPalette.WindowText: c["text"], QPalette.Base: c["base"],
        QPalette.AlternateBase: c["alt"], QPalette.Text: c["text"], QPalette.Button: c["button"],
        QPalette.ButtonText: c["text"], QPalette.Highlight: c["accent"],
        QPalette.HighlightedText: c["accent_text"], QPalette.ToolTipBase: c["base"],
        QPalette.ToolTipText: c["text"], QPalette.PlaceholderText: c["muted"], QPalette.Link: c["accent"],
        # Fusion draws checkbox/radio outlines and spin arrows with these
        QPalette.Mid: c["frame"], QPalette.Midlight: c["line"], QPalette.Light: c["base"],
        QPalette.Dark: c["frame"], QPalette.Shadow: c["line"],
    }
    for role, col in roles.items():
        p.setColor(role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(c["muted"]))
    app.setPalette(p)
    app.setStyleSheet(STYLE.format(**c))


STYLE = """
QWidget {{ font-size: 13px; }}
QMainWindow, #Page {{ background: {window}; }}
#Sidebar {{ background: {sidebar}; border-right: 1px solid {line}; }}
#AppTitle {{ font-size: 16px; font-weight: 700; padding: 18px 16px 2px 16px; }}
#AppSubtitle {{ color: {muted}; padding: 0 16px 14px 16px; font-size: 12px; }}
#NavButton {{ text-align: left; padding: 9px 16px; border: none; border-radius: 8px;
              margin: 2px 8px; background: transparent; font-size: 14px; }}
#NavButton:hover {{ background: {line}; }}
#NavButton:checked {{ background: {accent}; color: {accent_text}; font-weight: 600; }}
#PageTitle {{ font-size: 22px; font-weight: 700; }}
#PageSubtitle {{ color: {muted}; }}
#Muted {{ color: {muted}; }}
#Card {{ background: {card}; border: 1px solid {line}; border-radius: 10px; }}
#CardTitle {{ font-size: 14px; font-weight: 600; }}
QPushButton {{ padding: 6px 14px; border: 1px solid {line}; border-radius: 7px; background: {button}; }}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:disabled {{ color: {muted}; }}
QPushButton#Primary {{ background: {accent}; color: {accent_text}; border: none; font-weight: 600;
                       padding: 8px 20px; }}
QPushButton#Primary:disabled {{ background: {line}; color: {muted}; }}
QPushButton#Danger {{ color: {bad}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ padding: 4px 6px; border: 1px solid {line};
    border-radius: 6px; background: {base}; min-height: 20px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ border: 1px solid {line}; background: {base}; selection-background-color: {accent}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {accent}; }}
QGroupBox {{ border: none; border-top: 1px solid {line}; margin-top: 22px; padding: 14px 2px 2px 2px;
             background: transparent; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 0px; top: 2px; padding: 0 6px 0 0; color: {accent}; }}
QGroupBox QLabel, QGroupBox QCheckBox {{ font-weight: normal; }}
QTreeWidget, QTableWidget, QListWidget, QPlainTextEdit, QTextBrowser {{ border: 1px solid {line};
    border-radius: 8px; background: {base}; }}
QHeaderView::section {{ background: {alt}; border: none; border-bottom: 1px solid {line};
                        padding: 5px 6px; color: {muted}; font-weight: 600; }}
QProgressBar {{ border: none; border-radius: 4px; background: {line}; max-height: 8px; min-height: 8px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: 7px 14px; border: none; border-bottom: 2px solid transparent; background: transparent;
                color: {muted}; }}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; font-weight: 600; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget#qt_scrollarea_viewport, #ScrollContent {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {frame}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:horizontal {{ background: {frame}; border-radius: 3px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QToolTip {{ border: 1px solid {line}; padding: 5px; }}
"""
