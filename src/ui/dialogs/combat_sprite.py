"""[战场化 2026-08-31] 战斗单位立绘控件（头像图 / 占位绘制 + 受击闪白 + 目标高亮）。

参考 demo_combat_anim.py 的纯表现层手法（QPropertyAnimation 驱动视觉属性，不参与数值结算）：
- 有 avatar 文件：QPixmap 等比铺满（敌我双方共用，边框色区分阵营）。
- 无图：绘制阵营色占位（圆形头像底 + 名字首字）。
- flash 0..1 = 受击闪白强度（Qt Property，QPropertyAnimation 直接驱动）。
- highlight 0..1 = 可点选目标高亮圈；clicked 信号用于选目标。
- down 0..1 = 倒下透明度（死亡淡出）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Property, Qt, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QBrush, QLinearGradient, QFont
from PySide6.QtWidgets import QWidget


class UnitSprite(QWidget):
    """战斗单位立绘：头像图或占位绘制 + 闪白/高亮/倒下三态视觉。"""

    clicked = Signal()

    def __init__(self, name: str, avatar: str, enemy: bool, parent=None):
        super().__init__(parent)
        self._name = name or "?"
        self._enemy = bool(enemy)
        self._flash = 0.0
        self._highlight = 0.0
        self._shield = 0.0
        self._down = 0.0
        self._pix = None
        if avatar:
            try:
                from src.config import paths
                p = os.path.join(paths.world_images_dir(), avatar)
                pix = QPixmap(p)
                if not pix.isNull():
                    self._pix = pix
            except Exception:
                self._pix = None

    # ---- Qt Properties（动画驱动口）----
    def get_flash(self) -> float:
        return self._flash

    def set_flash(self, v: float):
        self._flash = float(v)
        self.update()

    flash = Property(float, get_flash, set_flash)

    def get_highlight(self) -> float:
        return self._highlight

    def set_highlight(self, v: float):
        self._highlight = float(v)
        self.update()

    highlight = Property(float, get_highlight, set_highlight)

    def get_shield(self) -> float:
        return self._shield

    def set_shield(self, v: float):
        self._shield = float(v)
        self.update()

    shield = Property(float, get_shield, set_shield)

    def get_down(self) -> float:
        return self._down

    def set_down(self, v: float):
        self._down = float(v)
        # 倒下淡出同时禁鼠标
        self.setAttribute(Qt.WA_TransparentForMouseEvents, self._down > 0.5)
        self.update()

    down = Property(float, get_down, set_down)

    def mousePressEvent(self, _ev):
        self.clicked.emit()
        super().mousePressEvent(_ev)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        accent = QColor("#f7768e") if self._enemy else QColor("#7aa2f7")
        body = QRectF(4, 4, w - 8, h - 8)

        if self._pix is not None:
            p.setPen(QPen(accent, 2))
            p.drawPixmap(body.toRect(), self._pix)
        else:
            # 占位：阵营色渐变圆底 + 名字首字
            grad = QLinearGradient(0, 0, 0, h)
            c1 = QColor("#7a3b52") if self._enemy else QColor("#243b6b")
            c2 = QColor("#341c2a") if self._enemy else QColor("#161a2e")
            grad.setColorAt(0.0, c1)
            grad.setColorAt(1.0, c2)
            p.setPen(QPen(accent, 2))
            p.setBrush(QBrush(grad))
            p.drawEllipse(body.adjusted(2, 2, -2, -2))
            p.setPen(QColor("#c0caf5"))
            f = QFont()
            f.setPointSize(max(10, min(w, h) // 4))
            f.setBold(True)
            p.setFont(f)
            p.drawText(body, Qt.AlignCenter, self._name[:1])

        # 可点选高亮圈
        if self._highlight > 0.001:
            p.setPen(QPen(QColor(255, 215, 94, int(self._highlight * 255)), 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(body.adjusted(-4, -4, 4, 4))

        # 防御护盾光圈（双层蓝环，alpha 随 shield 渐变）
        if self._shield > 0.001:
            a = int(self._shield * 200)
            p.setPen(QPen(QColor(122, 162, 247, a), 4))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(body.adjusted(-7, -7, 7, 7))
            p.setPen(QPen(QColor(157, 124, 216, int(a * 0.7)), 2))
            p.drawEllipse(body.adjusted(-12, -12, 12, 12))

        # 受击闪白
        if self._flash > 0.001:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, int(self._flash * 200)))
            p.drawEllipse(body)

        # 倒下淡出（整控件压暗）
        if self._down > 0.001:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(16, 18, 28, int(self._down * 200)))
            p.drawRect(self.rect())
