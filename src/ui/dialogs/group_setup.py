"""新建会话 / 群聊创建对话框。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QListWidget, QListWidgetItem, QCheckBox, QPushButton, QGroupBox,
    QFormLayout, QMessageBox, QWidget, QScrollArea,
)

from PySide6.QtWidgets import QSpinBox
from src.models import Character, ApiConfig, WorldBook, User


class GroupSetupDialog(QDialog):
    """新建聊天会话对话框（单聊 / 群聊）。"""

    def __init__(self, characters: list[Character], apis: list[ApiConfig],
                 world_books: list[WorldBook], users: list[User] | None = None,
                 parent=None):
        super().__init__(parent)
        self.characters = characters
        self.apis = apis
        self.world_books = world_books
        self.users = users or []
        self._result = {}
        self._greeting_options: list[tuple[str, str]] = [("", "")]  # 索引 -> (character_id, greeting_text)
        self.setWindowTitle("新建会话")
        self.resize(580, 820)
        self.setMinimumSize(560, 720)
        self._build_ui()

    def _build_ui(self):
        # 内容组包 QScrollArea，创建/取消按钮行固定在外层底部始终可见。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)

        # 标题
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("自动生成或自定义标题")
        form.addRow("会话标题:", self.title_edit)

        # 选择用户（下拉）+ 玩家名覆盖（可改写用于此会话）
        # 无 user_id 仍能用「玩家名」自由取名，向后兼容老「只用 player_name 文本」用法。
        self.user_combo = QComboBox()
        self.user_combo.addItem("（默认，不绑定用户）", "")
        for u in self.users:
            self.user_combo.addItem(u.name or "未命名用户", u.id)
        form.addRow("选择用户:", self.user_combo)
        self.player_edit = QLineEdit("用户")
        self.player_edit.setPlaceholderText("覆盖用户名（默认取所选用户名）")
        # 选用户时联动把「玩家名」填成该用户名，仍可改
        self.user_combo.currentIndexChanged.connect(self._on_user_changed)
        form.addRow("玩家名(覆盖):", self.player_edit)
        if self.users:
            # 初始选中第一个真实用户以方便
            self.user_combo.setCurrentIndex(1)
        # 否则保持默认占位「用户」

        # 上文自动总结设置（会话级，与角色记忆独立，不冲突）
        sum_group = QGroupBox("上文自动总结（会话级 token 压缩，与角色记忆独立）")
        sl = QFormLayout(sum_group)
        self.sum_enabled_chk = QCheckBox("启用（活跃消息达阈值时自动总结并折叠原文）")
        self.sum_enabled_chk.setChecked(True)
        sl.addRow(self.sum_enabled_chk)
        self.sum_threshold_spin = QSpinBox()
        self.sum_threshold_spin.setRange(5, 500)
        self.sum_threshold_spin.setValue(30)
        self.sum_threshold_spin.setToolTip("未折叠消息累计超过此数即触发一次自动总结")
        sl.addRow("触发阈值(条):", self.sum_threshold_spin)
        self.sum_count_spin = QSpinBox()
        self.sum_count_spin.setRange(2, 100)
        self.sum_count_spin.setValue(15)
        self.sum_count_spin.setToolTip("每次总结取最早的 N 条活跃消息（其余保留）")
        sl.addRow("每次总结N条:", self.sum_count_spin)
        sum_hint = QLabel(
            "「上文总结」= 把对话总结成可见的 summary 消息并折叠原文（会话内压缩）；"
            "「角色记忆」= 跨会话、按角色的隐形长程记忆。两者独立，可同时开启或都关闭。"
        )
        sum_hint.setWordWrap(True)
        sum_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        sl.addRow(sum_hint)
        form.addRow(sum_group)

        # 会话类型
        self.type_combo = QComboBox()
        self.type_combo.addItem("单聊（1 个角色）", "single")
        self.type_combo.addItem("群聊（多角色）", "group")
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("会话类型:", self.type_combo)
        layout.addLayout(form)

        # 角色选择
        self.char_group = QGroupBox("选择角色（可多选，多选为群聊）")
        char_layout = QVBoxLayout(self.char_group)
        self.char_list = QListWidget()
        for char in self.characters:
            api_name = next((a.name for a in self.apis if a.id == char.api_id), "未绑定")
            item = QListWidgetItem(f"{char.name}  [{api_name}]")
            item.setData(Qt.UserRole, char.id)
            item.setCheckState(Qt.Unchecked)
            self.char_list.addItem(item)
        self.char_list.itemChanged.connect(self._on_char_changed)
        char_layout.addWidget(self.char_list)
        layout.addWidget(self.char_group)

        # 群聊设置
        self.group_settings = QGroupBox("群聊设置")
        gs_layout = QFormLayout(self.group_settings)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("手动（点击头像触发）", "manual")
        self.mode_combo.addItem("自动（API选择发言者）", "auto")
        gs_layout.addRow("发言模式:", self.mode_combo)

        self.director_combo = QComboBox()
        for api in self.apis:
            self.director_combo.addItem(api.name, api.id)
        gs_layout.addRow("导演API:", self.director_combo)
        self.group_settings.setVisible(False)
        layout.addWidget(self.group_settings)

        # 世界书
        wb_group = QGroupBox("世界书（可选）")
        wb_layout = QVBoxLayout(wb_group)
        self.wb_combo = QComboBox()
        self.wb_combo.addItem("无", "")
        for wb in self.world_books:
            self.wb_combo.addItem(wb.name, wb.id)
        wb_layout.addWidget(self.wb_combo)
        layout.addWidget(wb_group)

        # 开场白选择（仅单聊、角色有开场白时显示）
        self.greeting_group = QGroupBox("开场白")
        gl = QVBoxLayout(self.greeting_group)
        self.greeting_combo = QComboBox()
        gl.addWidget(self.greeting_combo)
        self.greeting_group.setVisible(False)
        layout.addWidget(self.greeting_group)

        # 滚动区收尾：内容挂到 scroll，scroll 放进外层
        layout.addStretch()
        self._scroll.setWidget(content)
        outer.addWidget(self._scroll)

        # 按钮（固定在滚动区外底部，始终可见）
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 8, 8, 8)
        btn_row.addStretch()
        ok_btn = QPushButton("创建")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.clicked.connect(self._on_ok)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

    def _on_user_changed(self):
        """选用户时把「玩家名」覆盖行填成该用户名（可手动改）。"""
        uid = self.user_combo.currentData()
        if not uid:
            return  # 「默认」项不动
        u = next((x for x in self.users if x.id == uid), None)
        if u and u.name:
            self.player_edit.setText(u.name)

    def _on_type_changed(self):
        # 切换会话类型时刷新开场白/群聊设置显隐
        self._on_char_changed()

    def _on_char_changed(self):
        checked = sum(
            1 for i in range(self.char_list.count())
            if self.char_list.item(i).checkState() == Qt.Checked
        )
        is_group = checked > 1
        self.group_settings.setVisible(is_group)
        self._refresh_greeting_combo(checked)

    def _refresh_greeting_combo(self, checked_count: int = None):
        """单聊或群聊模式，填充开场白下拉框。

        - 单聊（1 个角色）：可选该角色的第一条消息 / 备选开场白。
        - 群聊（>1 个角色）：可选任一角色的开场白作为首条发言（显示带角色名前缀）。
        """
        if checked_count is None:
            checked_count = sum(
                1 for i in range(self.char_list.count())
                if self.char_list.item(i).checkState() == Qt.Checked
            )
        self.greeting_combo.clear()
        # _greeting_options: 下拉项索引 -> (character_id, greeting_text)
        self._greeting_options = []
        if checked_count < 1:
            self.greeting_group.setVisible(False)
            return
        # 收集所有选中角色
        selected_chars = []
        for i in range(self.char_list.count()):
            item = self.char_list.item(i)
            if item.checkState() == Qt.Checked:
                char = next(
                    (c for c in self.characters if c.id == item.data(Qt.UserRole)), None
                )
                if char:
                    selected_chars.append(char)
        if not selected_chars:
            self.greeting_group.setVisible(False)
            return
        greetings = []  # (显示文本, character_id, 实际内容)
        for char in selected_chars:
            prefix = "" if len(selected_chars) == 1 else f"{char.name} · "
            if char.first_message.strip():
                previews = char.first_message.replace("\n", " ").strip()[:24]
                greetings.append((f"{prefix}第一条消息: {previews}", char.id, char.first_message))
            for g in char.alternate_greetings:
                if g.strip():
                    previewg = g.replace("\n", " ").strip()[:24]
                    greetings.append((f"{prefix}备选: {previewg}", char.id, g))
        # 无开场白选项（默认）
        self.greeting_combo.addItem("无开场白", 0)
        self._greeting_options.append(("", ""))
        for i, (label, cid, content) in enumerate(greetings, start=1):
            self.greeting_combo.addItem(label, i)
            self._greeting_options.append((cid, content))
        self.greeting_combo.setCurrentIndex(0)
        self.greeting_group.setVisible(bool(greetings))

    def _on_ok(self):
        selected = []
        for i in range(self.char_list.count()):
            item = self.char_list.item(i)
            if item.checkState() == Qt.Checked:
                selected.append(item.data(Qt.UserRole))
        if not selected:
            QMessageBox.warning(self, "提示", "请至少选择一个角色")
            return

        is_group = len(selected) > 1
        chosen_type = self.type_combo.currentData()
        title = self.title_edit.text().strip()
        if not title:
            names = []
            for cid in selected:
                char = next((c for c in self.characters if c.id == cid), None)
                if char:
                    names.append(char.name)
            title = "、".join(names)

        if chosen_type == "single":
            session_type = "single" if len(selected) == 1 else "group"
            is_group = session_type == "group"
        else:  # group
            session_type = "group" if len(selected) > 1 else "single"
            is_group = session_type == "group"

        self._result = {
            "title": title,
            "session_type": session_type,
            "character_ids": selected,
            "world_book_id": self.wb_combo.currentData(),
            "user_id": self.user_combo.currentData() or "",
            "player_name": self.player_edit.text().strip() or "用户",
            "group_mode": self.mode_combo.currentData() if is_group else "manual",
            "director_api_id": self.director_combo.currentData() if is_group else "",
            "greeting": self._greeting_options[self.greeting_combo.currentIndex()][1],
            "greeting_character_id": self._greeting_options[self.greeting_combo.currentIndex()][0],
            "auto_summary_enabled": self.sum_enabled_chk.isChecked(),
            "auto_summary_threshold": self.sum_threshold_spin.value(),
            "auto_summary_count": self.sum_count_spin.value(),
        }
        self.accept()

    def get_result(self) -> dict:
        return self._result