"""Zoomable, pannable image viewer with a scale bar in real units."""

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView


class ImageView(QGraphicsView):
    """Mouse wheel zooms at the cursor, dragging pans, double-click fits the image."""

    zoomChanged = Signal(float)
    cursorMoved = Signal(str)  # position under the cursor, formatted

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self._item)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setFrameShape(QFrame.NoFrame)
        self.setMouseTracking(True)
        self._fitted = True
        self.units_per_pixel = None  # real-world size of one image pixel
        self.unit = "mm"
        self.message = ""

    def set_background(self, color):
        self.setBackgroundBrush(QColor(color))

    def set_image(self, path, units_per_pixel=None, keep_view=False):
        """Show an image file; keep_view keeps zoom/position (e.g. switching overlays)."""
        pm = QPixmap(path) if path else QPixmap()
        same_size = keep_view and not self._item.pixmap().isNull() and pm.size() == self._item.pixmap().size()
        self._item.setPixmap(pm)
        self.units_per_pixel = units_per_pixel
        self.message = "" if not pm.isNull() else ("Image not found" if path else "")
        self.scene().setSceneRect(QRectF(pm.rect()))
        if not same_size:
            self.fit()
        self.viewport().update()

    def has_image(self):
        return not self._item.pixmap().isNull()

    def zoom(self):
        return self.transform().m11()

    def fit(self):
        if self.has_image():
            self.fitInView(self._item, Qt.KeepAspectRatio)
            self._fitted = True
            self.zoomChanged.emit(self.zoom())

    def actual_size(self):
        self.resetTransform()
        self._fitted = False
        self.zoomChanged.emit(1.0)

    def zoom_by(self, factor):
        z = self.zoom() * factor
        if not 0.02 < z < 40:
            return
        self.scale(factor, factor)
        self._fitted = False
        self.zoomChanged.emit(self.zoom())

    def wheelEvent(self, event):
        if not self.has_image():
            return
        steps = event.angleDelta().y() / 120
        if steps:
            self.zoom_by(1.25 ** steps)

    def mouseDoubleClickEvent(self, event):
        self.fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fitted:
            self.fit()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.has_image():
            return
        p = self.mapToScene(event.position().toPoint())
        r = self._item.pixmap().rect()
        if not (0 <= p.x() < r.width() and 0 <= p.y() < r.height()):
            self.cursorMoved.emit("")
        elif self.units_per_pixel:
            u = self.units_per_pixel
            self.cursorMoved.emit(f"x {p.x() * u:.2f} {self.unit}   y {p.y() * u:.2f} {self.unit}")
        else:
            self.cursorMoved.emit(f"x {p.x():.0f}   y {p.y():.0f}")

    def drawForeground(self, painter, rect):
        painter.save()
        painter.resetTransform()
        vp = self.viewport().rect()
        if self.message:
            painter.setPen(QColor("#9aa3ab"))
            painter.drawText(vp, Qt.AlignCenter, self.message)
        elif self.units_per_pixel and self.has_image():
            self._draw_scale_bar(painter, vp)
        painter.restore()

    def _draw_scale_bar(self, painter, vp):
        units_per_screen_px = self.units_per_pixel / self.zoom()
        target = 140 * units_per_screen_px
        mag = 10 ** math.floor(math.log10(target))
        length = max(m * mag for m in (1, 2, 5) if m * mag <= target)  # a "nice" 1/2/5 length
        px = length / units_per_screen_px
        label = f"{length:g} {self.unit}" if length >= 1 else f"{length * 1000:g} µm"
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont(painter.font())
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        painter.setFont(font)
        tw = painter.fontMetrics().horizontalAdvance(label)
        box = QRectF(14, vp.height() - 50, max(px, tw) + 20, 36)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(box, 6, 6)
        painter.setBrush(QColor("white"))
        painter.drawRect(QRectF(box.left() + 10, box.bottom() - 12, px, 4))
        painter.setPen(QColor("white"))
        painter.drawText(QPointF(box.left() + 10, box.bottom() - 18), label)
