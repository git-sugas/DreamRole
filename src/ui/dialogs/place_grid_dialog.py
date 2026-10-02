"""[P50 地点网格 2026-09-25 用户拍板] 地点网格对话框：把地点画成一张能看的图。

网格内容（place_grid_engine.grid_for_location 推导）：
- 场所格（B）：显示场所名，点击 = 提示前往/进店信息（场景页自由输入可到）。
- 宅位格（H）：可安置；玩家的宅已安置则显示宅名，点击 = 打开住宅。
- 田块格（G）：一格一作物（作物名 + 生长阶），点击 = 打开住宅种植页。
- 空地（.）：纯走位装饰，不可点。

[深改 2026-10-01 用户拍板·方案二浮起图块] 自绘瓷砖（QPainter）：建筑图标 +
手绘投影 + 城市底板（路面/街道带/街灯/门坊）。

[进阶适配 2026-10-01 用户指示] **题材 × 聚落规模双维皮肤**：
- 题材（6 内置）：换建筑图标/地面材质/街灯/门坊造型（翘角屋顶·石板路·红灯笼·
  牌坊 → 尖塔·冷石·火把·塔门 → 方楼·柏油·路灯·路牌 → 圆顶舱·金属格·光柱·
  气闸门 → 破屋·废土·篝火·铁丝网）。瓷砖底色恒走暗金体系统一语言（§23 精神：
  几何/视觉骨架跨题材，题材感来自图内容与图标——与引擎「几何与题材解耦」同构）。
- 规模（city/town/village/wilderness）：city=双横街+纵街+四灯+双门坊；
  town=十字街+两灯+双门坊；village=单条土路+一灯+简易栅门；wilderness=无街无灯
  无门（野地草石）。
[!] offscreen 禁用 QGraphicsEffect（挂批量子控件再 grab 段错误），投影手绘；
QPainterPath 二次曲线是 quadTo（无 quadraticTo——paintEvent 内抛异常会段错误）。
守 §21 完全独立铁律：只读展示 + 既有引擎调用（settle_cell），不复制场景逻辑。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (QFont, QFontMetrics, QColor, QPen, QBrush,
                           QLinearGradient, QPainter, QPainterPath,
                           QRadialGradient)
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout, QMessageBox,
    QFrame, QWidget,
)

from src.services import place_grid_engine as pge

# 瓷砖调色（暗金古卷，跨题材统一）：kind -> (底上, 底下, 边, 图标/字)
_KIND_PAL = {
    "place":       ("#2b2418", "#1f1a11", "#57482a", "#e0c48f"),
    "home":        ("#1d2b22", "#141f18", "#3d5a45", "#9ece6a"),
    "lot":         ("#1d1e2a", "#171822", "#3a3a4a", "#8b87a8"),
    "plot":        ("#2a2214", "#1f1a0e", "#6b5326", "#e0af68"),
    "locked_plot": ("#191a24", "#14151d", "#2a2b3d", "#565f89"),
    "vacant":      ("#191a24", "#14151d", "#2a2b3d", "#565f89"),
    "empty":       ("#16161e", "#121218", "#20202c", "#3b4261"),
}


# ============ 题材皮肤：建筑图标 / 地面 / 街灯 / 门坊 ============
def _house_eaved(p: QPainter, cx: float, cy: float, col: QColor, home: bool):
    """古风（武侠/仙侠）：曲线翘角屋顶小屋；宅款挂红灯笼。"""
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawRect(QRectF(cx - 11, cy - 1, 22, 12))
    p.setBrush(col.darker(210))
    p.drawRect(QRectF(cx - 3, cy + 3, 6, 8))
    roof = QPainterPath(QPointF(cx - 17, cy))
    roof.quadTo(QPointF(cx - 9, cy - 12), QPointF(cx, cy - 13))
    roof.quadTo(QPointF(cx + 9, cy - 12), QPointF(cx + 17, cy))
    pen = QPen(col, 2.0)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(roof)
    p.drawLine(QPointF(cx - 19, cy - 2), QPointF(cx - 17, cy))
    p.drawLine(QPointF(cx + 17, cy), QPointF(cx + 19, cy - 2))
    if home:
        p.setPen(QPen(col.darker(160), 1.2))
        p.drawLine(QPointF(cx + 13, cy - 8), QPointF(cx + 13, cy - 3))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e0688a"))
        p.drawEllipse(QPointF(cx + 13, cy - 1), 2.6, 2.6)


def _house_tower(p: QPainter, cx: float, cy: float, col: QColor, home: bool):
    """西幻：尖顶塔楼（旗标）；宅款=平顶小屋+壁灯。"""
    if not home:
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRect(QRectF(cx - 7, cy - 4, 14, 16))
        roof = QPainterPath(QPointF(cx - 11, cy - 4))
        roof.lineTo(QPointF(cx, cy - 16))
        roof.lineTo(QPointF(cx + 11, cy - 4))
        p.setPen(QPen(col, 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawPath(roof)
        p.drawLine(QPointF(cx, cy - 16), QPointF(cx, cy - 22))     # 旗杆
        flag = QPainterPath(QPointF(cx, cy - 22))
        flag.lineTo(QPointF(cx + 8, cy - 20))
        flag.lineTo(QPointF(cx, cy - 18))
        p.setBrush(col)
        p.setPen(Qt.NoPen)
        p.drawPath(flag)
        p.setBrush(col.darker(210))
        p.drawRect(QRectF(cx - 2, cy + 2, 4, 10))                  # 门
    else:
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRect(QRectF(cx - 11, cy - 2, 22, 14))
        p.setPen(QPen(col, 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(cx - 14, cy - 2), QPointF(cx, cy - 11))
        p.drawLine(QPointF(cx, cy - 11), QPointF(cx + 14, cy - 2))
        p.setPen(Qt.NoPen)
        p.setBrush(col.darker(210))
        p.drawRect(QRectF(cx - 3, cy + 4, 6, 8))
        p.setBrush(QColor("#e0af68"))                              # 壁灯
        p.drawEllipse(QPointF(cx + 8, cy + 3), 2.0, 2.0)


def _house_block(p: QPainter, cx: float, cy: float, col: QColor, home: bool):
    """现代：方盒子楼（窗格）；宅款=两层小楼+门廊灯。"""
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    if not home:
        p.drawRect(QRectF(cx - 8, cy - 12, 16, 24))
        win = QColor("#e8e3d0")
        p.setBrush(win)
        for wy in (-8, -2, 4):
            p.drawRect(QRectF(cx - 5, cy + wy, 4, 4))
            p.drawRect(QRectF(cx + 1, cy + wy, 4, 4))
    else:
        p.drawRect(QRectF(cx - 11, cy - 6, 22, 18))
        p.drawRect(QRectF(cx - 8, cy - 14, 12, 8))                 # 二层
        p.setBrush(QColor("#e8e3d0"))
        p.drawRect(QRectF(cx - 5, cy - 12, 5, 4))
        p.drawRect(QRectF(cx - 5, cy + 1, 5, 4))
        p.setBrush(QColor("#e0af68"))                              # 门廊灯
        p.drawEllipse(QPointF(cx + 7, cy - 1), 1.8, 1.8)


def _house_dome(p: QPainter, cx: float, cy: float, col: QColor, home: bool):
    """科幻：圆顶舱（天线）；宅款=舱段+发光门。"""
    p.setPen(QPen(col, 1.8))
    p.setBrush(Qt.NoBrush)
    dome = QPainterPath()
    dome.arcMoveTo(QRectF(cx - 11, cy - 10, 22, 20), 180)
    dome.arcTo(QRectF(cx - 11, cy - 10, 22, 20), 180, 180)
    p.drawPath(dome)
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawRect(QRectF(cx - 12, cy, 24, 3))                         # 底座
    if not home:
        p.setBrush(col)
        p.drawRect(QRectF(cx - 1, cy - 16, 2, 6))                  # 天线
        p.drawEllipse(QPointF(cx, cy - 17), 1.4, 1.4)
    else:
        glow = QColor("#7fd6c2")
        p.setBrush(glow)
        p.drawRect(QRectF(cx - 2, cy - 5, 4, 5))                   # 发光门


def _house_ruin(p: QPainter, cx: float, cy: float, col: QColor, home: bool):
    """废土：破屋（缺口屋顶+木板补丁）；宅款=铁皮棚+烛火。"""
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawRect(QRectF(cx - 11, cy - 2, 22, 13))
    p.setPen(QPen(col, 1.8))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(cx - 14, cy - 2), QPointF(cx - 4, cy - 10))   # 缺口双坡
    p.drawLine(QPointF(cx + 2, cy - 9), QPointF(cx + 14, cy - 2))
    p.drawLine(QPointF(cx - 6, cy + 2), QPointF(cx + 6, cy + 6))     # 木板补丁
    p.setPen(Qt.NoPen)
    p.setBrush(col.darker(210))
    p.drawRect(QRectF(cx - 3, cy + 4, 6, 7))
    if home:
        p.setBrush(QColor("#e0af68"))                               # 烛火
        p.drawEllipse(QPointF(cx + 9, cy + 1), 1.8, 1.8)


def _lantern_red(p: QPainter, x: float, y: float):
    """古风：挑杆红灯笼（灯晕）。"""
    p.setPen(QPen(QColor(255, 235, 190, 70), 1))
    p.drawLine(QPointF(x, y - 8), QPointF(x, y - 2))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(224, 104, 138, 40))
    p.drawEllipse(QPointF(x, y), 6.5, 6.5)
    p.setBrush(QColor(224, 104, 138, 200))
    p.drawEllipse(QPointF(x, y), 2.8, 2.8)


def _lantern_torch(p: QPainter, x: float, y: float):
    """西幻：火把（火苗）。"""
    p.setPen(QPen(QColor("#8a6f3c"), 2))
    p.drawLine(QPointF(x, y - 9), QPointF(x, y - 2))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(224, 160, 60, 40))
    p.drawEllipse(QPointF(x, y - 1), 6.5, 6.5)
    flame = QPainterPath(QPointF(x - 2.6, y))
    flame.quadTo(QPointF(x, y - 9), QPointF(x + 2.6, y))
    p.setBrush(QColor(240, 170, 60, 220))
    p.drawPath(flame)


def _lantern_street(p: QPainter, x: float, y: float):
    """现代：路灯（弯杆+冷光灯晕）。"""
    p.setPen(QPen(QColor("#8b90a0"), 1.6))
    p.drawLine(QPointF(x, y - 10), QPointF(x, y - 2))
    p.drawLine(QPointF(x, y - 10), QPointF(x + 4, y - 10))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(220, 230, 255, 40))
    p.drawEllipse(QPointF(x + 4, y - 8), 6.0, 6.0)
    p.setBrush(QColor(220, 230, 255, 210))
    p.drawEllipse(QPointF(x + 4, y - 8), 2.0, 2.0)


def _lantern_beam(p: QPainter, x: float, y: float):
    """科幻：光柱。"""
    grad = QRadialGradient(QPointF(x, y), 8.0)
    grad.setColorAt(0.0, QColor(0, 240, 220, 90))
    grad.setColorAt(1.0, QColor(0, 240, 220, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawEllipse(QPointF(x, y), 8.0, 8.0)
    p.setBrush(QColor(140, 255, 240, 200))
    p.drawRect(QRectF(x - 1, y - 6, 2, 12))


def _lantern_fire(p: QPainter, x: float, y: float):
    """废土：篝火（柴+火苗）。"""
    p.setPen(QPen(QColor("#8a6f3c"), 1.6))
    p.drawLine(QPointF(x - 5, y + 3), QPointF(x + 5, y - 1))
    p.drawLine(QPointF(x + 5, y + 3), QPointF(x - 5, y - 1))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(240, 150, 60, 40))
    p.drawEllipse(QPointF(x, y - 3), 6.5, 6.5)
    flame = QPainterPath(QPointF(x - 2.4, y - 1))
    flame.quadTo(QPointF(x, y - 10), QPointF(x + 2.4, y - 1))
    p.setBrush(QColor(240, 160, 60, 220))
    p.drawPath(flame)


def _gate_paifang(p: QPainter, r: QRectF, hy: float, small: bool = False):
    """古风：牌坊（双柱+横枋）。"""
    scale = 0.7 if small else 1.0
    pen = QPen(QColor("#8a6f3c"), 2)
    p.setPen(pen)
    gy0 = hy - 20 * scale
    for gx, d in ((r.left() + 6, 1), (r.right() - 6, -1)):
        p.drawLine(QPointF(gx + 3 * d * scale, gy0),
                   QPointF(gx + 3 * d * scale, hy + 8 * scale))
        p.drawLine(QPointF(gx + 14 * d * scale, gy0),
                   QPointF(gx + 14 * d * scale, hy + 8 * scale))
        p.drawLine(QPointF(gx, gy0), QPointF(gx + 17 * d * scale, gy0))


def _gate_twin_spire(p: QPainter, r: QRectF, hy: float, small: bool = False):
    """西幻：双尖塔门。"""
    scale = 0.7 if small else 1.0
    pen = QPen(QColor("#5a6f9c"), 1.8)
    p.setPen(pen)
    h = 22 * scale
    for gx, d in ((r.left() + 7, 1), (r.right() - 7, -1)):
        x0 = gx
        p.drawLine(QPointF(x0, hy + 8 * scale), QPointF(x0, hy - h * 0.6))
        p.drawLine(QPointF(x0, hy - h * 0.6), QPointF(x0 + 4 * d * scale, hy - h))
        p.drawLine(QPointF(x0 + 4 * d * scale, hy - h), QPointF(x0 + 8 * d * scale, hy - h * 0.6))
        p.drawLine(QPointF(x0 + 8 * d * scale, hy - h * 0.6), QPointF(x0 + 8 * d * scale, hy + 8 * scale))


def _gate_sign(p: QPainter, r: QRectF, hy: float, small: bool = False):
    """现代：路牌（双柱横牌）。"""
    scale = 0.7 if small else 1.0
    pen = QPen(QColor("#8b90a0"), 1.6)
    p.setPen(pen)
    y0 = hy - 14 * scale
    for gx, d in ((r.left() + 7, 1), (r.right() - 7, -1)):
        p.drawLine(QPointF(gx, y0), QPointF(gx, hy + 8 * scale))
        p.drawLine(QPointF(gx + 3 * d * scale, y0), QPointF(gx + 10 * d * scale, y0))


def _gate_airlock(p: QPainter, r: QRectF, hy: float, small: bool = False):
    """科幻：气闸门框。"""
    scale = 0.7 if small else 1.0
    pen = QPen(QColor(0, 240, 220, 140), 1.6)
    p.setPen(pen)
    w, h = 16 * scale, 22 * scale
    for gx in (r.left() + 6, r.right() - 6):
        p.drawRect(QRectF(gx, hy - h, w, h))


def _gate_wire(p: QPainter, r: QRectF, hy: float, small: bool = False):
    """废土：铁丝网缺口。"""
    scale = 0.7 if small else 1.0
    pen = QPen(QColor("#6b5a45"), 1.4)
    p.setPen(pen)
    for gx, d in ((r.left() + 6, 1), (r.right() - 6, -1)):
        for i in range(3):
            yy = hy - 16 * scale + i * 7 * scale
            p.drawLine(QPointF(gx, yy), QPointF(gx + 10 * d * scale, yy - 4 * scale))
        p.drawLine(QPointF(gx, hy - 16 * scale), QPointF(gx, hy + 8 * scale))
        p.drawLine(QPointF(gx + 10 * d * scale, hy - 20 * scale),
                   QPointF(gx + 10 * d * scale, hy + 4 * scale))


# 皮肤注册表：题材 -> {建筑/宅图标, 地面, 街道色, 石缝色, 街灯, 门}
_SKINS: dict[str, dict] = {
    "wuxia": {
        "house": _house_eaved, "lantern": _lantern_red, "gate": _gate_paifang,
        "ground": ("#241e13", "#181410"), "street": (255, 235, 190, 16),
        "joint": (255, 240, 200, 14), "label": "坊市图",
    },
    "xianxia": {
        "house": _house_eaved, "lantern": _lantern_red, "gate": _gate_paifang,
        "ground": ("#241e13", "#181410"), "street": (255, 235, 190, 16),
        "joint": (255, 240, 200, 14), "label": "坊市图",
    },
    "western_fantasy": {
        "house": _house_tower, "lantern": _lantern_torch, "gate": _gate_twin_spire,
        "ground": ("#1e2028", "#15161d"), "street": (200, 220, 255, 14),
        "joint": (180, 200, 255, 12), "label": "坊镇图",
    },
    "modern": {
        "house": _house_block, "lantern": _lantern_street, "gate": _gate_sign,
        "ground": ("#1b1d21", "#141519"), "street": (255, 255, 255, 18),
        "joint": (255, 255, 255, 8), "label": "街区图",
    },
    "scifi": {
        "house": _house_dome, "lantern": _lantern_beam, "gate": _gate_airlock,
        "ground": ("#141a1e", "#0f1418"), "street": (0, 240, 220, 18),
        "joint": (0, 255, 220, 10), "label": "舱区图",
    },
    "apocalypse": {
        "house": _house_ruin, "lantern": _lantern_fire, "gate": _gate_wire,
        "ground": ("#1e1a15", "#161310"), "street": (200, 150, 90, 14),
        "joint": (0, 0, 0, 0), "label": "据点图",
    },
}

# 规模档：横街数 / 纵街 / 灯数 / 门
_SIZE_FEATS = {
    "city":       {"h": (0.32, 0.62), "v": 0.5, "lamps": 4, "gate": True},
    "town":       {"h": (0.42,), "v": 0.5, "lamps": 2, "gate": True},
    "village":    {"h": (0.5,), "v": None, "lamps": 1, "gate": "small"},
    "wilderness": {"h": (), "v": None, "lamps": 0, "gate": False},
}


def _grid_skin(world, loc) -> tuple[dict, dict]:
    """(题材皮肤, 规模档)。题材缺档回退西幻（GenreText 同口径）；野外优先于规模。"""
    genre = str((getattr(world, "config_overlay", None) or {})
                .get("attribute_template_id", "") or "western_fantasy")
    skin = _SKINS.get(genre, _SKINS["western_fantasy"])
    if getattr(loc, "kind", "") == "wilderness":
        size = _SIZE_FEATS["wilderness"]
    else:
        size = _SIZE_FEATS.get(str(getattr(loc, "settlement_size", "") or ""),
                               _SIZE_FEATS["town"])
    return skin, size


class GridTile(QWidget):
    """自绘瓷砖：上=题材建筑图标，下=名牌；手绘投影浮起。"""

    TILE_W, TILE_H = 92, 66

    def __init__(self, kind: str, label: str, skin: dict, on_click=None):
        super().__init__()
        self.kind, self.label, self.skin, self._on_click = kind, label, skin, on_click
        self.setFixedSize(self.TILE_W, self.TILE_H)
        self._hover = False
        if on_click is not None:
            self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, e):
        if self._on_click is not None:
            self._hover = True
            self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if self._on_click is not None and e.button() == Qt.LeftButton:
            self._on_click()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg1, bg2, bd, fg = _KIND_PAL.get(self.kind, _KIND_PAL["empty"])
        r = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 130))
        p.drawRoundedRect(r.translated(2.5, 3.5), 8.0, 8.0)
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0.0, QColor(bg1))
        grad.setColorAt(1.0, QColor(bg2))
        p.setBrush(QBrush(grad))
        dashed = self.kind in ("lot", "locked_plot", "vacant")
        pen = QPen(QColor("#c9a86a" if self._hover else bd), 2.0 if self._hover else 1.4)
        if dashed:
            pen.setStyle(Qt.DashLine)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawRoundedRect(r, 8.0, 8.0)
        cx, cy = r.center().x(), r.top() + 26
        col = QColor(fg)
        if self.kind in ("place", "home"):
            self.skin["house"](p, cx, cy, col, home=(self.kind == "home"))
        elif self.kind == "lot":
            pen2 = QPen(col, 1.6)
            pen2.setStyle(Qt.DashLine)
            p.setPen(pen2)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(cx - 14, cy - 10, 28, 22), 5, 5)
            p.setPen(QPen(col, 1.8))
            p.drawLine(QPointF(cx - 5, cy), QPointF(cx + 5, cy))
            p.drawLine(QPointF(cx, cy - 5), QPointF(cx, cy + 5))
        elif self.kind == "plot":
            self._sprout(p, cx, cy, col)
        elif self.kind == "locked_plot":
            self._sprout(p, cx, cy, col.darker(140))
        elif self.kind == "vacant":
            pen2 = QPen(col, 1.4)
            pen2.setStyle(Qt.DashLine)
            p.setPen(pen2)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(cx - 12, cy - 8, 24, 18), 4, 4)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(fg))
            p.drawEllipse(QPointF(cx, cy), 1.6, 1.6)
        f = QFont(self.font())
        f.setPointSizeF(8.0)
        p.setFont(f)
        fm = QFontMetrics(f)
        text = fm.elidedText(self.label or "", Qt.ElideRight, int(r.width()) - 10)
        p.setPen(QColor(fg))
        p.drawText(QRectF(r.left(), r.bottom() - 22, r.width(), 18),
                   Qt.AlignHCenter | Qt.AlignVCenter, text)

    @staticmethod
    def _sprout(p: QPainter, cx: float, cy: float, col: QColor):
        """秧苗（跨题材通用：农田意象）。"""
        pen = QPen(col, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(cx, cy + 9), QPointF(cx, cy - 3))
        leaf_l = QPainterPath(QPointF(cx, cy - 1))
        leaf_l.quadTo(QPointF(cx - 7, cy - 3), QPointF(cx - 9, cy - 9))
        p.drawPath(leaf_l)
        leaf_r = QPainterPath(QPointF(cx, cy - 4))
        leaf_r.quadTo(QPointF(cx + 7, cy - 6), QPointF(cx + 9, cy - 12))
        p.drawPath(leaf_r)
        p.setPen(QPen(col.darker(160), 1.6))
        p.drawArc(QRectF(cx - 8, cy + 6, 16, 6), 180 * 16, 180 * 16)


class GroundBoard(QWidget):
    """城市底板：题材地面 + 规模街道骨架（横/纵街 + 街灯 + 门坊）。"""

    def __init__(self, skin: dict, size: dict):
        super().__init__()
        self.skin, self.size = skin, size
        self.setObjectName("groundBoard")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        g1, g2 = self.skin["ground"]
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0.0, QColor(g1))
        grad.setColorAt(1.0, QColor(g2))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#57482a"), 1))
        p.drawRoundedRect(r, 10, 10)
        jr, jg, jb, ja = self.skin["joint"]
        if ja > 0:
            pen = QPen(QColor(jr, jg, jb, ja), 1)
            p.setPen(pen)
            step = 23
            x = r.left() + step
            while x < r.right():
                p.drawLine(QPointF(x, r.top() + 8), QPointF(x, r.bottom() - 8))
                x += step
            y = r.top() + step
            while y < r.bottom():
                p.drawLine(QPointF(r.left() + 8, y), QPointF(r.right() - 8, y))
                y += step
        if self.size["lamps"] == 0:
            # 野地：碎石草点（无街道无灯无门）
            p.setPen(Qt.NoPen)
            rnd = 7
            for i in range(24):
                gx = r.left() + 10 + (i * 37 + rnd * 13) % int(r.width() - 20)
                gy = r.top() + 10 + (i * 53 + rnd * 29) % int(r.height() - 20)
                if i % 3 == 0:
                    p.setBrush(QColor(140, 170, 90, 70))
                    p.drawEllipse(QPointF(gx, gy), 1.8, 1.8)     # 草
                else:
                    p.setBrush(QColor(200, 190, 160, 40))
                    p.drawEllipse(QPointF(gx, gy), 1.2, 1.2)     # 碎石
            return
        sr, sg, sb, sa = self.skin["street"]
        band = QBrush(QColor(sr, sg, sb, sa))
        h_frac = self.size["h"]
        band_h = 32 if len(h_frac) > 1 else (26 if self.size["gate"] is True else 20)
        ys = [r.top() + r.height() * f for f in h_frac]
        p.setPen(Qt.NoPen)
        p.setBrush(band)
        for hy in ys:
            p.drawRoundedRect(QRectF(r.left() + 6, hy - band_h / 2,
                                     r.width() - 12, band_h), 8, 8)
        vx = None
        if self.size["v"] is not None:
            vx = r.left() + r.width() * self.size["v"]
            p.drawRoundedRect(QRectF(vx - 16, r.top() + 6, 32, r.height() - 12), 8, 8)
        # 街灯：沿主街（第一条横街）分布
        n = int(self.size["lamps"])
        main = ys[0]
        for i in range(n):
            lx = r.left() + r.width() * (0.22 + 0.56 * (i / max(1, n - 1)))
            self.skin["lantern"](p, lx, main - 4)
        # 门坊：主街两端
        gate = self.size["gate"]
        if gate:
            self.skin["gate"](p, r, main, small=(gate == "small"))


class PlaceGridDialog(QDialog):
    """地点网格地图（只读可视化 + 宅位安置/进宅入口）。"""

    def __init__(self, world, loc, home=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.loc = loc
        self.home = home                       # 玩家在此地的宅（可能 None=未购宅）
        self.skin, self.size = _grid_skin(world, loc)
        self.setWindowTitle(f"{loc.name} · 地图")
        from src.ui.widgets.game_widgets import banner_rule, card_sub, game_badge
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)
        head = QLabel(f"{loc.name} · {self.skin['label']}")
        head.setObjectName("bannerName")
        lay.addWidget(head)
        lay.addWidget(banner_rule())
        lay.addSpacing(2)
        if loc.desc:
            lay.addWidget(card_sub(loc.desc))
        self.board = GroundBoard(self.skin, self.size)
        self.grid_lay = QGridLayout(self.board)
        self.grid_lay.setContentsMargins(16, 14, 16, 14)
        self.grid_lay.setSpacing(10)
        lay.addWidget(self.board, 1)
        legend = QWidget()
        lh = QHBoxLayout(legend)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.setSpacing(6)
        for text, level in (("场所", "gold"), ("你的宅", "success"),
                            ("可安置", "info"), ("田块", "warn"),
                            ("未开垦/空置", "info"), ("空地", "info")):
            lh.addWidget(game_badge(text, level))
        lh.addStretch()
        lay.addWidget(legend)
        self._tip = QLabel("点击场所/宅/田块查看说明；宅位格点击安置你的宅。")
        self._tip.setStyleSheet("color:#565f89; font-size:11px;")
        self._tip.setWordWrap(True)
        lay.addWidget(self._tip)
        self._render()

    def _render(self):
        while self.grid_lay.count():
            it = self.grid_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        grid = pge.grid_for_location(self.world, self.loc)
        cols = max(1, int(grid.get("cols") or 8))
        rows = max(1, int(grid.get("rows") or 6))
        for c in grid["cells"]:
            interactive = c["kind"] in ("place", "home", "lot", "plot")
            tile = GridTile(
                c["kind"], c.get("label") or "", self.skin,
                on_click=(lambda cc=dict(c): self._on_cell(cc)) if interactive else None)
            tile.setToolTip(c.get("label") or "")
            self.grid_lay.addWidget(tile, c["y"], c["x"])
        need_w = cols * GridTile.TILE_W + (cols - 1) * 10 + 32 + 52
        need_h = rows * GridTile.TILE_H + (rows - 1) * 10 + 28 + 300
        self.setMinimumSize(min(980, max(560, need_w)), min(760, max(460, need_h)))
        self.resize(min(980, max(560, need_w)), min(760, max(460, need_h)))

    def _on_cell(self, cell: dict):
        kind = cell["kind"]
        if kind == "place":
            QMessageBox.information(
                self, cell["label"],
                f"「{cell['label']}」就在这里。\n在场景页输入“前往{cell['label']}”即可过去。")
        elif kind == "home":
            QMessageBox.information(self, "你的宅",
                                    "这就是你的宅子。\n在场景页输入“回宅”即可回去休憩、打理。")
        elif kind == "lot":
            if self.home is None:
                QMessageBox.information(self, "可安置的宅位",
                                        "这里可以安置宅子——先在本聚落购房。")
                return
            ok, err = pge.settle_cell(self.world, self.home, self.loc,
                                      cell["x"], cell["y"])
            if ok:
                QMessageBox.information(self, "安置完成",
                                        f"宅子安置在了村口第 {cell['x']},{cell['y']} 格。")
                self._render()
            else:
                QMessageBox.warning(self, "无法安置", err)
        elif kind == "plot":
            QMessageBox.information(self, "田块",
                                    "这是你家的田（一格一种作物）。\n在住宅页播种/浇水/收获。")


def open_place_grid(world, loc, parent=None):
    """场景页/住宅页入口：打开地点网格（自动找玩家在此地的宅）。"""
    home = next((h for h in (world.homes or [])
                 if h.location_id == loc.id
                 and loc.id in (getattr(world.player, "home_ids", None) or [])), None)
    dlg = PlaceGridDialog(world, loc, home=home, parent=parent)
    return dlg
