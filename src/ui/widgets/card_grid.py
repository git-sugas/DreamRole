"""矩形卡片网格组件（浏览式）。

与 AvatarGridSelector 的区别：
- AvatarGridSelector 是「选择式」（点选/多选/单选，带选中圈），用于对话框里挑实体。
- CardGrid 是「浏览式」（点卡即触发动作，不做选中态），用于主界面单聊/群聊卡墙：
  点角色卡 -> 开单聊建会话；点世界书卡 -> 开群聊建会话。
卡片为矩形缩略图（头像/封面）+ 名字，类似 LoRA 管理器的卡片样式。
列数自适应：按可用宽度动态计算列数（resize 时重排不重建 cell，不重读图片）。
"""
from __future__ import annotations
import os

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QBrush, QPainterPath
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QGridLayout, QScrollArea, QSizePolicy,
)

# 文字占位卡背景色调色板（与 avatar_button._AVATAR_COLORS 一致，保持视觉统一）
_CARD_COLORS = ["#7aa2f7", "#bb9af7", "#9ece6a", "#e0af68", "#f7768e", "#7dcfff"]

# 卡片布局常量（列宽步进 = _CARD_PAD*2 + image_size + 网格 spacing）
_CARD_PAD = 12          # 卡片内边距（四边一致）
_GRID_SPACING = 22      # 网格间距
_CORNER_RADIUS_RATIO = 16  # 圆角 = size // 16（130->8, 210->13）


# 卡片缩略图缓存：key=(路径, mtime_ns, size字节, 渲染尺寸, 名字)，文件被覆写时 mtime 变
# 自动失效（防换图后显示旧卡）。QPixmap 隐式共享，返回同一实例安全（调用方只 setPixmap）。
_PIX_CACHE: dict[tuple, QPixmap] = {}
_PIX_CACHE_MAX = 512


def make_card_pixmap(name: str, image_filename: str, image_dir: str, size: int) -> QPixmap:
    """生成矩形卡片缩略图 QPixmap。

    - 有图：从 image_dir/image_filename 加载，按 KeepAspectRatioByExpanding 缩放后居中裁剪为
      size×size 正方形，并按圆角路径裁剪（与无图占位卡圆角一致）。
    - 无图：画带圆角的彩色方块 + 首字母（矩形版，区别于 make_avatar_pixmap 的圆形）。
    """
    key = None
    if image_filename:
        path = os.path.join(image_dir, image_filename)
        try:
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size, size, name)
        except OSError:
            key = None
        if key is not None and key in _PIX_CACHE:
            return _PIX_CACHE[key]
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)

    radius = max(8, size // _CORNER_RADIUS_RATIO)
    loaded = False
    if image_filename:
        path = os.path.join(image_dir, image_filename)
        if os.path.exists(path):
            img = QPixmap(path)
            if not img.isNull():
                img = img.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                x = (img.width() - size) // 2
                y = (img.height() - size) // 2
                img = img.copy(x, y, size, size)
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
                p.setClipPath(clip)
                p.drawPixmap(0, 0, img)
                p.setClipping(False)
                loaded = True

    if not loaded:
        color = _CARD_COLORS[hash(name) % len(_CARD_COLORS)] if name else "#565f89"
        p.setBrush(QBrush(QColor(color)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, size, size, radius, radius)
        p.setPen(QColor("#1a1b26"))
        p.setFont(QFont("Microsoft YaHei", size // 2, QFont.Bold))
        p.drawText(pix.rect(), Qt.AlignCenter, (name[0] if name else "?"))

    p.end()
    if key is not None:
        if len(_PIX_CACHE) >= _PIX_CACHE_MAX:
            _PIX_CACHE.clear()
        _PIX_CACHE[key] = pix
    return pix


class CardCell(QWidget):
    """单张矩形卡片：缩略图 + 名字，点击触发 activated(id)；右键触发 context_menu_requested(id, pos)。"""

    activated = Signal(str)
    # [!] 右键菜单信号：参数 (entity_id, global_pos)。CardGrid 聚合后向外转发。
    context_menu_requested = Signal(str, object)

    def __init__(self, entity_id: str, name: str, image_filename: str,
                 image_dir: str, image_size: int = 210, parent=None):
        super().__init__(parent)
        self._id = entity_id
        self._name = name
        self._image_size = image_size
        self.setObjectName("entityCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_CARD_PAD, _CARD_PAD, _CARD_PAD, _CARD_PAD)
        layout.setSpacing(10)

        self.image_label = QLabel()
        self.image_label.setFixedSize(image_size, image_size)
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.image_label.setPixmap(make_card_pixmap(name, image_filename, image_dir, image_size))
        layout.addWidget(self.image_label)

        self.name_label = QLabel(name or "未命名")
        self.name_label.setObjectName("cardNameLabel")
        self.name_label.setFixedWidth(image_size)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        # 名字过长省略
        self.name_label.setWordWrap(False)
        layout.addWidget(self.name_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self._id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        # [!] 子 QLabel (image_label/name_label) 已设 WA_TransparentForMouseEvents，
        # 右键事件正常冒泡到 CardCell。globalPos 用于在屏幕坐标系弹 QMenu。
        self.context_menu_requested.emit(self._id, event.globalPos())
        event.accept()


class CardGrid(QWidget):
    """矩形卡片网格（浏览式，点卡触发 activated 信号，不做选中态）。

    列数自适应：按 scroll viewport 可用宽度 / 卡片步进（image_size + 2*_CARD_PAD + spacing）
    动态计算；resize 时只重排已有 cell 位置（removeWidget + 重新 addWidget），不重建
    cell、不重读图片文件，避免拖动窗口边缘时闪烁与磁盘 IO。
    """

    activated = Signal(str)
    # [!] 聚合 cell 的右键菜单信号，转发给外层使用方（按需接 QMenu）。
    context_menu_requested = Signal(str, object)

    def __init__(self, image_size: int = 210, parent=None, auto_height: bool = False):
        super().__init__(parent)
        self._image_size = image_size
        self._columns = 1
        self._cells: list[CardCell] = []
        # [!] auto_height：外层已是滚动页（如世界详情页 _wrap_scroll）时，内嵌
        # QScrollArea 的默认 sizeHint 高度远大于卡片实际内容，把下方文字推得过远。
        # 此模式按内容行数覆盖 sizeHint/minimumSizeHint，隐藏内层滚动条。
        self._auto_height = auto_height

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        if self._auto_height:
            self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QScrollArea.NoFrame)

        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(_GRID_SPACING)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self._empty_hint = QLabel("（暂无内容）")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setStyleSheet("color:#565f89; padding:60px; font-size:15px;")
        self._grid.addWidget(self._empty_hint, 0, 0)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

    def set_items(self, items: list[tuple[str, str, str, str]]):
        """items: [(id, name, image_filename, image_dir), ...]"""
        # 清旧 cell：立即从布局 detach + setParent(None)（deleteLater 是异步的，
        # 否则重渲染时旧 cell 还在布局里与新 cell 短暂同框重叠）
        for cell in self._cells:
            self._grid.removeWidget(cell)
            cell.setParent(None)
            cell.deleteLater()
        self._cells.clear()
        # empty_hint 从布局移除并隐藏（仅空态显示，否则会停在左上角残留可见）
        self._grid.removeWidget(self._empty_hint)
        self._empty_hint.hide()

        if not items:
            self._empty_hint.show()
            self._grid.addWidget(self._empty_hint, 0, 0)
            return

        for eid, name, img_fn, img_dir in items:
            cell = CardCell(eid, name, img_fn, img_dir, self._image_size)
            cell.activated.connect(self._on_cell_activated)
            cell.context_menu_requested.connect(self._on_cell_context_menu)
            self._cells.append(cell)
        self._reflow()

    def _reflow(self):
        """按当前列数重排已有 cell（不重建、不重读图片）。"""
        for i, cell in enumerate(self._cells):
            self._grid.removeWidget(cell)
            r, c = divmod(i, self._columns)
            self._grid.addWidget(cell, r, c, alignment=Qt.AlignTop | Qt.AlignHCenter)
        if self._auto_height:
            self.updateGeometry()

    # ---- auto_height 高度推导（width 保持默认，只覆盖 height）----
    def _content_height(self) -> int:
        import math
        if not self._cells:
            return 180  # 空态提示（padding 60 的一行文字）
        rows = max(1, math.ceil(len(self._cells) / max(1, self._columns)))
        cell_h = self._image_size + 2 * _CARD_PAD + 10 + 24  # 图 + 边距 + 间距 + 名字行
        return rows * cell_h + (rows - 1) * _GRID_SPACING

    def sizeHint(self):
        base = super().sizeHint()
        if self._auto_height:
            base.setHeight(self._content_height())
        return base

    def minimumSizeHint(self):
        base = super().minimumSizeHint()
        if self._auto_height:
            base.setHeight(self._content_height())
        return base

    def _calc_columns(self) -> int:
        pitch = self._image_size + 2 * _CARD_PAD + _GRID_SPACING
        avail = self._scroll.viewport().width() + _GRID_SPACING
        return max(1, avail // pitch)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cols = self._calc_columns()
        if cols != self._columns:
            self._columns = cols
            if self._cells:
                self._reflow()
            elif self._auto_height:
                self.updateGeometry()

    def showEvent(self, event):
        # 首次显示时 viewport 宽才有真实值，此时校准一次列数
        super().showEvent(event)
        cols = self._calc_columns()
        if cols != self._columns:
            self._columns = cols
            if self._cells:
                self._reflow()
            elif self._auto_height:
                self.updateGeometry()

    def _on_cell_activated(self, eid: str):
        self.activated.emit(eid)

    def _on_cell_context_menu(self, eid: str, pos):
        self.context_menu_requested.emit(eid, pos)
