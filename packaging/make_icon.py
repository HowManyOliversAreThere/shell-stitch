"""Draws the app icon (shellstitch/gui/resources/icon.png). Run: uv run python packaging/make_icon.py

PyInstaller converts the PNG to .icns / .ico when building (needs Pillow, in the build group).
"""

import os
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

SIZE = 1024


def draw():
    img = QImage(SIZE, SIZE, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # rounded-square background (macOS-style margins)
    m = SIZE * 0.09
    bg = QRectF(m, m, SIZE - 2 * m, SIZE - 2 * m)
    grad = QLinearGradient(bg.topLeft(), bg.bottomRight())
    grad.setColorAt(0, QColor("#2a93a8"))
    grad.setColorAt(1, QColor("#145766"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(bg, SIZE * 0.2, SIZE * 0.2)

    # shell section: a crescent filled with growth lines
    cx, cy = SIZE * 0.5, SIZE * 1.02
    outer, inner = SIZE * 0.66, SIZE * 0.36
    shell = QPainterPath()
    shell.addEllipse(QPointF(cx, cy), outer, outer * 0.92)
    hole = QPainterPath()
    hole.addEllipse(QPointF(cx, cy + SIZE * 0.05), inner, inner * 0.85)
    shell = shell.subtracted(hole).intersected(_rounded(bg))
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawPath(shell)
    p.save()
    p.setClipPath(shell)
    for i in range(18):
        r = inner + (outer - inner) * (i + 0.5) / 18
        pen = QPen(QColor(20, 87, 102, 150 if i % 3 == 0 else 70), SIZE * (0.008 if i % 3 == 0 else 0.004))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy + SIZE * 0.05 * (1 - i / 18)), r, r * (0.85 + 0.07 * i / 18))
    p.restore()

    # two overlapping photo tiles
    for (x, y, a) in ((0.20, 0.20, -6), (0.40, 0.30, 4)):
        p.save()
        p.translate(SIZE * (x + 0.2), SIZE * (y + 0.15))
        p.rotate(a)
        p.setPen(QPen(QColor("#ffd166"), SIZE * 0.022, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(255, 209, 102, 40))
        p.drawRoundedRect(QRectF(-SIZE * 0.2, -SIZE * 0.15, SIZE * 0.4, SIZE * 0.3), SIZE * 0.025, SIZE * 0.025)
        p.restore()
    p.end()
    return img


def _rounded(rect):
    path = QPainterPath()
    path.addRoundedRect(rect, SIZE * 0.2, SIZE * 0.2)
    return path


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)
    out = os.path.join(os.path.dirname(__file__), "..", "shellstitch", "gui", "resources", "icon.png")
    draw().save(os.path.normpath(out))
    print("wrote", os.path.normpath(out))
