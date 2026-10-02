"""[三骰取二 2026-08-26] 骰子演出控件（从 tools/demo_dice.py 抽取并泛化）。

- DieFace：单骰，独立翻滚 -> 落定弹跳；支持 d10（0-9）与百分骰（1-100）。
  滚动数字用系统随机（纯演出噪声），真实骰面由引擎/roll_dice_check 算好，落定时锁定。
- ValueTrack：1-100 读数轴（掷高制），红[1,成功线) / 绿[成功线,大成功线) / 金[大成功线,100]。
"""
from __future__ import annotations

import math
import random

from PySide6.QtCore import Qt, QTimer, Signal, QRectF, QPointF, QVariantAnimation, QEasingCurve
from PySide6.QtGui import (QPainter, QColor, QFont, QPen, QBrush, QLinearGradient,
                           QRadialGradient, QPainterPath)
from PySide6.QtWidgets import QWidget

TIER_CRIT, TIER_OK, TIER_FAIL, TIER_FUMBLE = "crit", "ok", "fail", "fumble"
TIER_INFO = {
    TIER_CRIT: ("大成功", QColor(255, 215, 106)),
    TIER_OK: ("成 功", QColor(111, 224, 138)),
    TIER_FAIL: ("失 败", QColor(224, 112, 112)),
    TIER_FUMBLE: ("大失败", QColor(224, 112, 112)),
}


class DieFace(QWidget):
    """单骰：独立翻滚 -> 落定弹跳；多颗错峰停靠 `index` 控制间隔/时长。"""

    landed = Signal(int)

    def __init__(self, role: str, index: int, parent=None,
                 min_value: int = 0, max_value: int = 9, label: str | None = None):
        super().__init__(parent)
        self._role = role
        self._min = int(min_value)
        self._max = int(max_value)
        self._label = label
        self.setFixedSize(150, 150)
        self._display = self._min
        self._final = self._min
        self._angle = 0.0
        self._scale = 1.0
        self._glow = 0.0
        self._frame = None  # None 滚动中 / "gold" / "red" / "gray"
        self._churn = QTimer(self)
        self._churn.setInterval(48 + index * 16)
        self._churn.timeout.connect(self._churn_value)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1300 + index * 650)  # 靠前的先停，靠后的后停
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda v: self._on_tick(float(v)))
        self._anim.finished.connect(self._on_settle)
        self._pop = None
        self._phase = index * 1.1

    def roll(self, final_value: int):
        self._final = max(self._min, min(self._max, int(final_value)))
        self._frame = None
        self._glow = 0.0
        self._churn.start()
        self._anim.stop()
        self._anim.start()

    def _churn_value(self):
        # 翻滚数字用系统随机（纯演出噪声）；真实骰面已算好，落定时锁定
        self._display = random.randint(self._min, self._max)
        self.update()

    def _on_tick(self, t: float):
        self._angle = math.sin(t * 13.0 + self._phase) * 24.0 * (1.0 - t)
        self._scale = 1.0 + 0.06 * math.sin(t * math.pi + self._phase)
        if t > 0.82:
            self._display = self._final
            if self._churn.isActive():
                self._churn.stop()
        self.update()

    def _on_settle(self):
        self._churn.stop()
        self._display = self._final
        self._angle = 0.0
        self._frame = "gray"
        pop = QVariantAnimation(self)
        pop.setStartValue(0.0)
        pop.setEndValue(1.0)
        pop.setDuration(380)
        pop.setEasingCurve(QEasingCurve.Type.OutBack)
        pop.valueChanged.connect(lambda v: self._on_pop(float(v)))
        pop.finished.connect(lambda: self.landed.emit(self._final))
        pop.start()
        self._pop = pop

    def set_frame(self, frame):
        self._frame = frame
        self.update()

    def _on_pop(self, v: float):
        self._scale = 1.0 + 0.16 * (1.0 - v)
        self._glow = v
        self.update()

    def _font_point(self) -> int:
        digits = max(len(str(self._min)), len(str(self._max)))
        return {1: 40, 2: 40, 3: 26}.get(digits, 26)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        frame = {"gold": QColor(255, 215, 106), "red": QColor(224, 112, 112),
                 "gray": QColor(120, 128, 142)}.get(self._frame, QColor(90, 96, 110))
        if self._frame == "gold" and self._glow > 0:  # 大成功射线
            p.translate(cx, cy)
            p.rotate(self._glow * 20)
            ray = QColor(255, 215, 106)
            for i in range(10):
                a = math.radians(i * 36)
                r0, r1 = 64, 64 + 16 * self._glow
                ray.setAlpha(int(190 * self._glow))
                p.setPen(QPen(ray, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(r0 * math.cos(a), r0 * math.sin(a)),
                           QPointF(r1 * math.cos(a), r1 * math.sin(a)))
            p.resetTransform()
        p.translate(cx, cy)
        p.rotate(self._angle)
        p.scale(self._scale, self._scale)
        rect = QRectF(-56, -56, 112, 112)
        if self._glow > 0 and self._frame in ("gold", "red"):
            halo = QRadialGradient(QPointF(0, 0), 74)
            c = QColor(frame)
            c.setAlpha(int(110 * self._glow))
            halo.setColorAt(0.0, c)
            c0 = QColor(frame)
            c0.setAlpha(0)
            halo.setColorAt(1.0, c0)
            p.setBrush(QBrush(halo))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(0, 0), 74, 74)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0.0, QColor(35, 38, 46))
        grad.setColorAt(1.0, QColor(20, 22, 28))
        edge = QColor(frame)
        edge.setAlpha(220 if self._frame else 120)
        p.setPen(QPen(edge, 4 if self._frame else 2))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(rect, 22, 22)
        p.setPen(QPen(QColor(58, 63, 74), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(7, 7, -7, -7), 16, 16)
        f = QFont(self.font())
        f.setPointSize(self._font_point())
        f.setBold(True)
        p.setFont(f)
        num_color = QColor(240, 242, 245)
        if self._frame == "gold":
            num_color = QColor(255, 235, 170)
        elif self._frame == "red":
            num_color = QColor(255, 180, 180)
        p.setPen(num_color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self._display))
        if self._label:
            f2 = QFont(self.font())
            f2.setPointSize(8)
            f2.setBold(True)
            p.setFont(f2)
            p.setPen(QColor(120, 128, 142))
            p.drawText(rect.adjusted(0, 0, -6, -40), Qt.AlignmentFlag.AlignRight, self._label)
        p.end()


class ValueTrack(QWidget):
    """1-100 读数轴（掷高制）：红[1,成功线) / 绿[成功线,大成功线) / 金[大成功线,100] + 指针。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(78)
        self._succ_line = 36
        self._crit_thresh = 95
        self._pos = 0.0
        self._anim = None

    def set_lines(self, p_pct: int, crit_thresh: int):
        self._succ_line = max(1, min(100, 101 - p_pct))
        self._crit_thresh = max(self._succ_line, min(100, crit_thresh))
        self._pos = 0.0
        self.update()

    def animate_to(self, read: int):
        frac = max(0.0, min(1.0, (read - 1) / 99.0))
        a = QVariantAnimation(self)
        a.setStartValue(self._pos)
        a.setEndValue(frac)
        a.setDuration(600)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.valueChanged.connect(lambda v: self._on_move(float(v)))
        a.start()
        self._anim = a

    def _on_move(self, v: float):
        self._pos = v
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        m, bar_h = 14, 18
        w = self.width() - m * 2
        y = 30

        def vx(v):
            return m + w * (v - 1) / 99.0

        x_succ, x_crit = vx(self._succ_line), vx(self._crit_thresh)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(92, 28, 28))
        p.drawRoundedRect(QRectF(m, y, max(2.0, x_succ - m), bar_h), 8, 8)
        p.setBrush(QColor(28, 92, 52))
        p.drawRoundedRect(QRectF(x_succ, y, max(2.0, x_crit - x_succ), bar_h), 8, 8)
        p.setBrush(QColor(126, 100, 26))
        p.drawRoundedRect(QRectF(x_crit, y, max(2.0, m + w - x_crit), bar_h), 8, 8)
        f = QFont(self.font())
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(TIER_INFO[TIER_FAIL][1])
        p.drawText(QRectF(m, y - 16, max(x_succ - m, 60), 14), Qt.AlignmentFlag.AlignLeft,
                   f"失败 (< {self._succ_line})")
        p.setPen(TIER_INFO[TIER_OK][1])
        p.drawText(QRectF(x_succ, y - 16, max(x_crit - x_succ, 70), 14),
                   Qt.AlignmentFlag.AlignHCenter, f"成功 (>= {self._succ_line})")
        p.setPen(TIER_INFO[TIER_CRIT][1])
        p.drawText(QRectF(x_crit, y - 16, m + w - x_crit, 14), Qt.AlignmentFlag.AlignRight,
                   f"大成功 (>= {self._crit_thresh})")
        p.setPen(QPen(QColor(240, 242, 245), 2))
        p.drawLine(QPointF(x_succ, y - 4), QPointF(x_succ, y + bar_h + 4))
        p.setPen(QColor(200, 208, 218))
        p.drawText(QRectF(x_succ - 40, y + bar_h + 4, 80, 14), Qt.AlignmentFlag.AlignHCenter,
                   f"判定线 {self._succ_line}")
        px = m + w * self._pos
        diamond = QPainterPath()
        diamond.moveTo(px, y + bar_h / 2 - 10)
        diamond.lineTo(px + 7, y + bar_h / 2)
        diamond.lineTo(px, y + bar_h / 2 + 10)
        diamond.lineTo(px - 7, y + bar_h / 2)
        p.setPen(QPen(QColor(240, 242, 245), 1))
        p.setBrush(QBrush(QColor(250, 250, 252)))
        p.drawPath(diamond)
        p.end()
