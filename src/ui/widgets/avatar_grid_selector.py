"""圆形头像宫格选择器。

用于新建会话对话框中"选择角色"（多选）与"选择用户"（单选），
把原来的纯文字 QListWidget / QComboBox 升级为带头像的宫格，点击切换选中态。
复用 AvatarButton（圆形裁剪 + [selected] 描边态，QSS 已有 #avatarBtn[selected="true"] 规则）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QWidget, QGridLayout, QVBoxLayout, QLabel, QScrollArea, QSizePolicy,
)

from src.ui.widgets.avatar_button import make_avatar_pixmap


class _AvatarCell(QWidget):
    """宫格中的一个单元：上方圆形头像 + 下方名字，整体可点击。

    [!] 不用 AvatarButton 作为子控件：disabled 的 QPushButton 在 Windows 真实环境
    会吞掉鼠标事件不向上传播，导致 _AvatarCell.mousePressEvent 收不到点击（离屏
    测试无法复现，因 offscreen 平台事件分发行为不同）。改用 QLabel 显示头像，
    整块单元自己处理点击，彻底避免子控件吃事件。
    """

    clicked_id = Signal(str)  # 该单元绑定的实体 id

    def __init__(self, entity_id: str, name: str, avatar: str = "",
                 size: int = 56, parent=None):
        super().__init__(parent)
        self.entity_id = entity_id
        self._name = name
        self._avatar = avatar
        self._size = size
        self._selected = False
        self.setObjectName("avatarGridCell")
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignHCenter)

        # 头像用 QLabel + make_avatar_pixmap（圆形裁剪 + 空头像回退首字母）
        # [!] 设 WA_TransparentForMouseEvents：让鼠标点击穿透 label 到父单元，
        # 避免 label 拦截事件（虽然 QLabel 默认不处理 mousePress 会 ignore 冒泡，
        # 但显式穿透更可靠，跨平台行为一致）。
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(size, size)
        self.avatar_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._refresh_avatar()
        layout.addWidget(self.avatar_label, alignment=Qt.AlignHCenter)

        # 名字（过长截断）
        display = name if len(name) <= 6 else name[:6] + "…"
        self.name_label = QLabel(display)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setStyleSheet("color: #c0caf5; font-size: 12px;")
        self.name_label.setWordWrap(False)
        self.name_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.name_label)

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _refresh_avatar(self):
        """刷新头像 pixmap（圆形裁剪，空头像回退首字母占位）。

        选中态时在头像外圈画一圈光晕描边（类似聊天页头像 hover 效果），
        未选中时为纯头像。
        """
        # 头像本体尺寸，选中光圈需在外侧留 padding，故 pixmap 略大于头像
        pad = 4 if self._selected else 0
        total = self._size + pad * 2
        pix = QPixmap(total, total)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)

        # 画头像本体（居中）
        base = make_avatar_pixmap(self._name, self._avatar, self._size)
        p.drawPixmap(pad, pad, base)

        # 选中态：画圆形光圈描边
        if self._selected:
            p.setBrush(Qt.NoBrush)
            # [修 2026-10-02 用户指示·描边换色] 旧紫 #bb9af7 暗底对比不足（真机
            # 验收选中态难辨）——换暗金体系亮金 #e0c48f（theme.qss 主金注释同源）。
            p.setPen(QPen(QColor("#e0c48f"), 3))
            # 光圈圆略大于头像，落在头像边缘外侧
            r = self._size // 2 + 1
            cx = cy = total // 2
            p.drawEllipse(QPoint(cx, cy), r, r)
        p.end()
        self.avatar_label.setPixmap(pix)
        # 选中时光圈让 label 需要更大显示区
        self.avatar_label.setFixedSize(total, total)

    def set_selected(self, selected: bool):
        self._selected = selected
        # 选中态刷新头像（加/去光圈）
        self._refresh_avatar()
        # 选中时名字高亮（[修 2026-10-02] 选中色随描边换亮金 #e0c48f）
        self.name_label.setStyleSheet(
            f"color: {'#e0c48f' if selected else '#c0caf5'}; "
            f"font-size: 12px; {'font-weight: bold;' if selected else ''}"
        )
        # 选中态背景 + 描边靠 QSS #avatarGridCell[selected="true"]
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def is_selected(self) -> bool:
        return self._selected

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.entity_id:
            self.clicked_id.emit(self.entity_id)
        super().mousePressEvent(event)


class AvatarGridSelector(QWidget):
    """圆形头像宫格选择器。

    - multi=True：多选模式（选角色），点一下切一个。
    - multi=False：单选模式（选用户），点一下只选一个，互斥；首项可为「默认/不绑定」占位。
    选中态通过 AvatarButton 的紫色描边 + 单元背景体现（QSS #avatarGridCell[selected]）。
    """

    selection_changed = Signal()           # 选中集合变化时发射，供外部联动（如刷新开场白/玩家名）
    current_id_changed = Signal(str)       # 单选模式下当前选中 id 变化时发射（id 可能为空串=取消选中）

    def __init__(self, multi: bool = True, columns: int = 4,
                 avatar_size: int = 56, parent=None):
        super().__init__(parent)
        self.multi = multi
        self._columns = columns
        self._avatar_size = avatar_size
        self._cells: dict[str, _AvatarCell] = {}  # id -> cell
        self._entity_order: list[str] = []        # 保持插入顺序（单选回退/默认项用）

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self._scroll.setWidget(self._container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        self._empty_hint = QLabel("（暂无可选项）")
        self._empty_hint.setStyleSheet("color: #565f89; font-size: 12px;")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        outer.addWidget(self._empty_hint)
        self._empty_hint.setVisible(True)
        self._scroll.setVisible(False)

    def set_items(self, items: list[tuple[str, str, str]]):
        """设置候选项。

        items: [(id, name, avatar), ...]，avatar 为文件名（可空，空则首字母占位）。
        单选模式下，调用方可把「默认不绑定」作为第一项 (id="", name="默认")。
        """
        # 清空旧单元
        while self._grid.count():
            it = self._grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._cells.clear()
        self._entity_order = []

        has_items = bool(items)
        self._empty_hint.setVisible(not has_items)
        self._scroll.setVisible(has_items)
        if not has_items:
            return

        for i, (eid, name, avatar) in enumerate(items):
            cell = _AvatarCell(eid, name, avatar, size=self._avatar_size)
            cell.clicked_id.connect(self._on_cell_clicked)
            row, col = divmod(i, self._columns)
            self._grid.addWidget(cell, row, col, alignment=Qt.AlignCenter)
            self._cells[eid] = cell
            self._entity_order.append(eid)

    def _on_cell_clicked(self, eid: str):
        cell = self._cells.get(eid)
        if cell is None:
            return
        if self.multi:
            cell.set_selected(not cell.is_selected())
        else:
            # 单选互斥：先清掉所有，再选当前
            for c in self._cells.values():
                if c.is_selected():
                    c.set_selected(False)
            cell.set_selected(True)
        self.selection_changed.emit()
        if not self.multi:
            self.current_id_changed.emit(eid)

    def get_selected_ids(self) -> list[str]:
        """返回选中 id 列表（按设置顺序）。空 id（默认占位项）会被过滤。"""
        return [eid for eid in self._entity_order
                if eid and self._cells[eid].is_selected()]

    def get_selected_id(self) -> str:
        """单选模式取唯一选中 id，无选中返回空串。"""
        ids = self.get_selected_ids()
        return ids[0] if ids else ""

    def set_selected_ids(self, ids: list[str]):
        """外部设置选中（多选）。"""
        idset = set(ids)
        for eid, cell in self._cells.items():
            cell.set_selected(eid in idset)
        self.selection_changed.emit()

    def select_exactly(self, eid: str, emit: bool = False):
        """单选模式下精确选中某个 id。

        emit=False（默认）：不发射信号，用于初始化（避免触发联动）。
        emit=True：发射 selection_changed + current_id_changed，用于程序化选中后
        需要外部联动加载详情的场景（如「新建角色」后自动选中并加载表单）。
        """
        for cid, cell in self._cells.items():
            cell.set_selected(cid == eid and bool(eid))
        if emit:
            self.selection_changed.emit()
            if not self.multi:
                self.current_id_changed.emit(eid)
