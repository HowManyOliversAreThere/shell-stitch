"""Zoomable, pannable image viewer with a scale bar in real units."""

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QWidget


class ScaleBar(QWidget):
    """Scale bar overlay, fixed to the view's bottom-left corner.

    A separate widget rather than painted into the scene: QGraphicsView scrolls by moving
    already-painted pixels, which would drag anything drawn over the image along with it.
    """

    MARGIN = 14

    def __init__(self, view):
        super().__init__(view)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.length_px, self.label = 0.0, ""
        self.hide()

    def set_scale(self, units_per_screen_px, unit):
        target = 140 * units_per_screen_px
        mag = 10 ** math.floor(math.log10(target))
        length = max(m * mag for m in (1, 2, 5) if m * mag <= target)  # a "nice" 1/2/5 length
        self.length_px = length / units_per_screen_px
        self.label = f"{length:g} {unit}" if length >= 1 else f"{length * 1000:g} µm"
        text_w = self.fontMetrics().horizontalAdvance(self.label)
        self.resize(int(max(self.length_px, text_w)) + 20, 36)
        vp = self.parent().viewport().geometry()
        self.move(vp.left() + self.MARGIN, vp.bottom() - self.height() - self.MARGIN)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        p.setBrush(QColor("white"))
        p.drawRect(QRectF(10, self.height() - 12, self.length_px, 4))
        p.setPen(QColor("white"))
        p.drawText(QPointF(10, self.height() - 18), self.label)


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
        self.scale_bar = ScaleBar(self)
        self.zoomChanged.connect(self._update_scale_bar)

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
        self._update_scale_bar()
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
        self._update_scale_bar()

    def _update_scale_bar(self, *_):
        if self.units_per_pixel and self.has_image():
            self.scale_bar.set_scale(self.units_per_pixel / self.zoom(), self.unit)
        else:
            self.scale_bar.hide()

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
        if self.message:  # only shown when there's no image, so nothing scrolls under it
            painter.setPen(QColor("#9aa3ab"))
            painter.drawText(vp, Qt.AlignCenter, self.message)
        painter.restore()
