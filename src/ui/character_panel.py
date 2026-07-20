"""角色面板：右侧栏显示群聊成员/单聊角色，可点击触发发言。"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QSizePolicy,
)

from src.models import Character
from src.ui.widgets.avatar_button import AvatarButton


class CharacterCard(QWidget):
    """单个角色卡片。"""
    clicked = Signal(str)  # character_id

    def __init__(self, character: Character, api_name: str = "", parent=None):
        super().__init__(parent)
        self.character = character
        self._build_ui(character, api_name)

    def _build_ui(self, char: Character, api_name: str):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self.avatar = AvatarButton(char.id, char.name, char.avatar, size=56)
        self.avatar.clicked_character.connect(self.clicked.emit)
        layout.addWidget(self.avatar)

        info = QVBoxLayout()
        info.setSpacing(4)
        name_label = QLabel(char.name)
        name_label.setObjectName("charNameLabel")
        name_label.setStyleSheet("font-size: 13px;")
        info.addWidget(name_label)

        badges = []
        if api_name:
            badges.append(f"API: {api_name}")
        if char.memory_mode != "none":
            mem_map = {"summary": "总结记忆", "embedding_hybrid": "混合记忆"}
            badges.append(mem_map.get(char.memory_mode, char.memory_mode))
        if badges:
            badge_label = QLabel(" | ".join(badges))
            badge_label.setStyleSheet("color: #565f89; font-size: 11px;")
            info.addWidget(badge_label)

        info.addStretch()
        layout.addLayout(info, 1)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_highlight(self, highlighted: bool):
        if highlighted:
            self.setStyleSheet("background-color: #2a2b3d; border-radius: 6px;")
        else:
            self.setStyleSheet("")


class CharacterPanel(QScrollArea):
    """角色面板（右侧栏）。"""
    character_clicked = Signal(str)  # character_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFixedWidth(260)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(8, 12, 8, 12)
        self._layout.setSpacing(8)
        self.setWidget(self._container)

        self._cards: dict[str, CharacterCard] = {}

        title = QLabel("角色")
        title.setObjectName("titleLabel")
        self._layout.addWidget(title)

        self._cards_widget = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_widget)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(8)
        self._layout.addWidget(self._cards_widget)
        self._layout.addStretch()

        self._hint = QLabel("选择会话后显示角色")
        self._hint.setStyleSheet("color: #565f89; font-size: 12px;")
        self._layout.addWidget(self._hint)

    def set_characters(self, characters: list[Character], api_names: dict[str, str] | None = None):
        """设置角色列表。"""
        api_names = api_names or {}
        # 清空旧卡片
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._hint.setVisible(not characters)

        for char in characters:
            card = CharacterCard(char, api_names.get(char.id, ""))
            card.clicked.connect(self.character_clicked.emit)
            self._cards[char.id] = card
            self._cards_layout.addWidget(card)

    def highlight_character(self, character_id: str):
        for cid, card in self._cards.items():
            card.set_highlight(cid == character_id)