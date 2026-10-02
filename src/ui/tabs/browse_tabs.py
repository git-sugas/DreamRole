"""主界面顶部三标签里的「单聊 / 群聊」浏览页。

- CharacterBrowseTab：列出所有角色卡（矩形卡墙），点卡 -> 开单聊建会话。
- WorldBookBrowseTab：列出所有世界书（封面卡墙），点卡 -> 开群聊建会话。
两页都带搜索框 + 「管理」按钮（开对应编辑器）。空态有提示。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)

from src.config import paths
from src.ui.widgets.card_grid import CardGrid


class _BrowseTab(QWidget):
    """浏览页基类：标题 + 搜索框 + 管理按钮 + 卡片网格。子类提供 populate 与 activated 信号。"""

    manage_requested = Signal()

    def __init__(self, title: str, manage_btn_text: str, parent=None):
        super().__init__(parent)
        self._all_items: list[tuple[str, str, str, str]] = []  # [(id,name,img_fn,img_dir)]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(16)

        # 头部：标题 + 搜索 + 管理按钮
        header = QHBoxLayout()
        header.setSpacing(12)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("titleLabel")
        header.addWidget(self.title_label)
        header.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索…")
        self.search.setFixedWidth(280)
        self.search.textChanged.connect(self._apply_filter)
        header.addWidget(self.search)
        self.manage_btn = QPushButton(manage_btn_text)
        self.manage_btn.clicked.connect(self.manage_requested)
        header.addWidget(self.manage_btn)
        layout.addLayout(header)

        self.grid = CardGrid(image_size=210)
        self.grid.activated.connect(self._on_card)
        # [!] 右键菜单：基类默认 no-op。单聊/群聊卡墙原本就无内联删除（走「管理」进编辑器），
        # 但 CardGrid 公共信号已加，基类顺手接上避免 dead wire；后续子类按需覆写弹菜单。
        self.grid.context_menu_requested.connect(self._on_card_context_menu)
        layout.addWidget(self.grid, 1)

    def _set_items(self, items: list[tuple[str, str, str, str]]):
        self._all_items = items
        self._apply_filter()

    def _apply_filter(self):
        kw = self.search.text().strip().lower()
        items = [it for it in self._all_items if not kw or kw in (it[1] or "").lower()]
        self.grid.set_items(items)

    def _on_card(self, eid: str):
        """子类重写：点卡时发出对应信号。"""
        pass

    def _on_card_context_menu(self, eid: str, pos):
        """子类按需重写：弹右键菜单。基类 no-op（单聊/群聊卡墙无内联删除）。"""
        pass


class CharacterBrowseTab(_BrowseTab):
    """单聊页：角色卡墙。点角色卡 -> character_selected(id)。"""

    character_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__("单聊 · 选择角色开始对话", "角色卡管理", parent)

    def populate(self, characters):
        self._set_items([
            (c.id, c.name, c.avatar, paths.avatars_dir()) for c in characters
        ])

    def _on_card(self, eid: str):
        self.character_selected.emit(eid)


class WorldBookBrowseTab(_BrowseTab):
    """群聊页：世界书卡墙。点世界书卡 -> world_book_selected(id)。"""

    world_book_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__("群聊 · 选择世界书开始对话", "世界书管理", parent)

    def populate(self, world_books):
        self._set_items([
            (wb.id, wb.name, wb.cover, paths.covers_dir()) for wb in world_books
        ])

    def _on_card(self, eid: str):
        self.world_book_selected.emit(eid)
