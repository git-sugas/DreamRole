"""[P6] 物品品级边框控件（世界模拟专用）。

需求来源：read.md §22 Phase A「品级动态边框」。不同品级物品应有不同边框，传说级要金色流光动态边框。

实现要点：
- Qt QSS **不支持** CSS @keyframes/animation（read.md §22 Phase C 调研确认），动画只能用
  QPropertyAnimation 在 Python 端驱动。故传说流光走 paintEvent 自绘 + QPropertyAnimation。
- `RarityFrame(QFrame)`：按 Item.rarity 显示品级边框。
  * common/uncommon/rare/epic：走 QSS 静态边框（objectName="rarityItem" + 动态属性 rarity，
    theme.qss 提供五档色/宽）。
  * legendary：QSS border 置 none，paintEvent 自绘「流光金边」= 外层柔光 + 底色金边 +
    QConicalGradient 旋转高亮弧（phase 0..1 循环驱动），QSS 底色保留。
- 仅传说级动画，其余静态；hideEvent 暂停动画省 CPU，showEvent 恢复。
- 内容：自带 QVBoxLayout（margin 8），外部 frame.addWidget(...) 或 frame.layout().addWidget(...)。

守 §21 完全独立铁律：世界模拟专用控件，objectName="rarityItem" 限定，不污染全局样式。
零 emoji（守 §283）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Property, QPropertyAnimation, QEasingCurve, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QConicalGradient, QPainterPath
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget


# 六档品级 -> 边框色（与 theme.qss #rarityItem 静态规则一致；paintEvent 仅史诗及以上用）。
# [P 验收] 白>绿>蓝>紫>橙>红；紫（epic）及以上有流动特效。
RARITY_BORDER_COLOR = {
    "common": "#d7dae0",
    "uncommon": "#9ece6a",
    "rare": "#7aa2f7",
    "epic": "#bb9af7",
    "legendary": "#e0a860",
    "mythic": "#e05555",
}
# 紫及以上流动特效的流光主色（epic 紫 / legendary 橙 / mythic 红）。
_FLOW_COLOR = {
    "epic": "#bb9af7",
    "legendary": "#e0a860",
    "mythic": "#e05555",
}
_FLOW_RARITIES = frozenset(_FLOW_COLOR)
# 传说/神话流光循环周期（毫秒）：值越小流光越快。
_LEGENDARY_DURATION_MS = 2600


class RarityFrame(QFrame):
    """品级边框容器。set_rarity 决定静态边框（common..epic）或传说流光动画（legendary）。

    用法：
        f = RarityFrame("legendary")
        f.addWidget(QLabel("神器·诛仙剑"))
    """

    def __init__(self, rarity: str = "common", parent=None):
        super().__init__(parent)
        self.setObjectName("rarityItem")
        self._rarity: str = ""
        self._phase: float = 0.0
        self._anim: QPropertyAnimation | None = None
        # 内容布局（外部可 addWidget）
        self._content = QVBoxLayout(self)
        self._content.setContentsMargins(8, 8, 8, 8)
        self._content.setSpacing(4)
        self.set_rarity(rarity)

    # ============ Qt 动画属性 phase（0..1）============
    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, v: float) -> None:
        self._phase = float(v)
        self.update()  # 触发 paintEvent 重绘流光

    phase = Property(float, _get_phase, _set_phase)

    # ============ 品级 ============
    def set_rarity(self, rarity: str) -> None:
        """切换品级：刷新动态属性 + 决定是否启用流光动画。"""
        r = rarity if rarity in RARITY_BORDER_COLOR else "common"
        self._rarity = r
        self.setProperty("rarity", r)
        # 动态属性变更需 unpolish/polish 让 QSS [rarity="..."] 选择器重新生效
        self.style().unpolish(self)
        self.style().polish(self)
        if r in _FLOW_RARITIES:
            self._ensure_anim()
        else:
            self._stop_anim()

    @property
    def rarity(self) -> str:
        return self._rarity

    def addWidget(self, w: QWidget) -> None:
        self._content.addWidget(w)

    def addLayout(self, l) -> None:
        """转发到内容布局（横排 [图标 | 文字] 等复合行用）。"""
        self._content.addLayout(l)

    def content_layout(self) -> QVBoxLayout:
        return self._content

    # ============ 动画生命周期 ============
    def _ensure_anim(self) -> None:
        if self._anim is None:
            self._anim = QPropertyAnimation(self, b"phase")
            self._anim.setDuration(_LEGENDARY_DURATION_MS)
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.setLoopCount(-1)
            self._anim.setEasingCurve(QEasingCurve.Linear)
        if self._anim.state() != QPropertyAnimation.Running and self.isVisible():
            self._anim.start()

    def _stop_anim(self) -> None:
        if self._anim is not None:
            self._anim.stop()

    def showEvent(self, e):
        super().showEvent(e)
        if self._rarity in _FLOW_RARITIES:
            self._ensure_anim()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._stop_anim()

    # ============ 史诗及以上流光自绘 ============
    def paintEvent(self, e):
        # 先让 QFrame 画背景/子内容（QSS 背景）；border 由我们接管
        # （紫及以上时 theme.qss #rarityItem[rarity="..."] border:none，避免与自绘重边）。
        super().paintEvent(e)
        if self._rarity not in _FLOW_RARITIES:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        if w < 4 or h < 4:
            return
        rect = QRectF(1.5, 1.5, w - 3.0, h - 3.0)
        radius = 6.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        gold = QColor(_FLOW_COLOR[self._rarity])
        # 1) 外层柔光（宽半透明金描边，模拟 bloom 光晕）
        glow_pen = QPen(QColor(gold.red(), gold.green(), gold.blue(), 55))
        glow_pen.setWidth(6)
        painter.setPen(glow_pen)
        painter.drawPath(path)
        # 2) 底色金边
        base_pen = QPen(gold)
        base_pen.setWidth(2)
        painter.setPen(base_pen)
        painter.drawPath(path)
        # 3) 流光高亮弧：conical 渐变以矩形中心为原点，起始角随 phase 旋转；
        #    弧的大部分透明，一段亮白金 -> 旋转产生流光（绕边流动）。
        conic = QConicalGradient(QPointF(w / 2.0, h / 2.0), self._phase * 360.0)
        conic.setColorAt(0.00, QColor(255, 240, 200, 0))
        conic.setColorAt(0.74, QColor(255, 240, 200, 0))
        conic.setColorAt(0.88, QColor(255, 248, 220, 220))
        conic.setColorAt(0.93, QColor(255, 255, 245, 255))
        conic.setColorAt(1.00, QColor(255, 240, 200, 0))
        flow_pen = QPen(QBrush(conic), 2.6)
        flow_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(flow_pen)
        painter.drawPath(path)
        painter.end()
