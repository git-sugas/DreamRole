"""新建会话 / 群聊创建对话框。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QCheckBox, QPushButton, QGroupBox,
    QFormLayout, QMessageBox, QWidget, QScrollArea,
)

from PySide6.QtWidgets import QSpinBox
from src.models import Character, ApiConfig, WorldBook, User
from src.ui.widgets.avatar_grid_selector import AvatarGridSelector


class GroupSetupDialog(QDialog):
    """新建聊天会话对话框（单聊 / 群聊）。"""

    def __init__(self, characters: list[Character], apis: list[ApiConfig],
                 world_books: list[WorldBook], users: list[User] | None = None,
                 parent=None,
                 lock_type: str = "",
                 preset_character_id: str = "",
                 preset_world_book_id: str = ""):
        """
        lock_type: "" 默认（全自由，类型按角色数自动判定，对应会话页/菜单「+新建会话」）；
                   "single" 单聊锁定（角色预选+隐藏角色 grid、类型锁定 single，其余可改）；
                   "group" 群聊锁定（世界书锁定、类型锁定 group、显示角色多选 grid）。
        preset_character_id: lock_type="single" 时预选的角色 id。
        preset_world_book_id: lock_type="group" 时锁定的世界书 id。
        """
        super().__init__(parent)
        self.characters = characters
        self.apis = apis
        self.world_books = world_books
        self.users = users or []
        self._lock_type = lock_type or ""
        self._preset_character_id = preset_character_id or ""
        self._preset_world_book_id = preset_world_book_id or ""
        self._result = {}
        self._greeting_options: list[tuple[str, str]] = [("", "")]  # 索引 -> (character_id, greeting_text)
        self.setWindowTitle("新建会话")
        self.resize(640, 820)
        self.setMinimumSize(620, 720)
        self._build_ui()
        self._apply_lock()

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
        layout.setContentsMargins(18, 14, 18, 6)

        # [深改 2026-10-01] 页名大字 + 金笔分隔（旧无页头，表单裸起）
        from src.ui.widgets.game_widgets import banner_rule
        _title = QLabel("新建会话")
        _title.setObjectName("bannerName")
        layout.addWidget(_title)
        layout.addWidget(banner_rule())

        # 标题
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("自动生成或自定义标题")
        form.addRow("会话标题:", self.title_edit)

        # 选择用户（宫格单选，带头像）+ 玩家名覆盖（可改写用于此会话）
        # 无 user_id 仍能用「玩家名」自由取名，向后兼容老「只用 player_name 文本」用法。
        # [!] 不设「默认」占位项：不选任何用户头像即=不绑定用户（get_selected_id 返回空串），
        # 无需专门一个占位框（占位框作为头像出现多余碍眼）。
        user_items = [(u.id, u.name or "未命名用户", u.avatar) for u in self.users]
        self.user_grid = AvatarGridSelector(multi=False, columns=5, avatar_size=48)
        self.user_grid.set_items(user_items)
        self.user_grid.selection_changed.connect(self._on_user_changed)
        form.addRow("选择用户:", self.user_grid)
        self.player_edit = QLineEdit("用户")
        self.player_edit.setPlaceholderText("覆盖用户名（默认取所选用户名）")
        # 选用户时联动把「玩家名」填成该用户名，仍可改
        form.addRow("玩家名(覆盖):", self.player_edit)
        # [!] 不默认选中任何用户：让用户主动点选（单选模式下默认选中后，
        # 只有一个用户卡时点它无反应，体验差）。初始为不绑定状态，
        # 玩家名保持占位「用户」，用户可手动选某个用户卡或直接改玩家名。
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
        self.sum_threshold_spin.setToolTip(
            "未折叠的活跃消息（含用户和 AI 双方的发言）累计超过此数即触发一次自动总结。"
            "按全部消息计数，不是只数 AI 回复（与角色记忆的「每 N 条 AI 回复触发」口径不同）。"
        )
        sl.addRow("触发阈值(条):", self.sum_threshold_spin)
        self.sum_count_spin = QSpinBox()
        self.sum_count_spin.setRange(2, 100)
        self.sum_count_spin.setValue(15)
        self.sum_count_spin.setToolTip("每次总结取最早的 N 条活跃消息（含用户与 AI 发言）喂给总结模型，其余消息保留。")
        sl.addRow("每次总结N条:", self.sum_count_spin)
        sum_hint = QLabel(
            "「上文总结」= 把对话总结成可见的 summary 消息并折叠原文（会话内压缩）；"
            "「角色记忆」= 跨会话、按角色的隐形长程记忆。两者独立，可同时开启或都关闭。"
            "另外注意：上文总结的阈值按全部消息（含用户发言）计数，而角色记忆按 AI 回复条数计数，"
            "两者口径不同，不要混淆。"
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

        # 角色选择（宫格多选，带头像）
        self.char_group = QGroupBox("选择角色（可多选，多选为群聊）")
        char_layout = QVBoxLayout(self.char_group)
        # 宫格选择器；角色多时内部 QScrollArea 可滚动
        self.char_grid = AvatarGridSelector(multi=True, columns=4, avatar_size=56)
        char_items = [(c.id, c.name, c.avatar) for c in self.characters]
        self.char_grid.set_items(char_items)
        self.char_grid.selection_changed.connect(self._on_char_changed)
        char_layout.addWidget(self.char_grid)
        layout.addWidget(self.char_group)

        # 单聊锁定模式：只读角色展示（替代角色多选 grid，仅 lock_type="single" 显示）
        self.preset_char_label = QLabel()
        self.preset_char_label.setVisible(False)
        self.preset_char_label.setStyleSheet("padding:8px; color:#9aa5ce;")
        layout.addWidget(self.preset_char_label)

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

        # 群聊记忆整理（会话级）：0=禁用走角色个人配置，>0=每 N 条全量消息触发
        self.group_mem_interval = QSpinBox()
        self.group_mem_interval.setRange(0, 100)
        self.group_mem_interval.setValue(0)
        self.group_mem_interval.setSpecialValueText("禁用(用角色个人配置)")
        self.group_mem_interval.setToolTip(
            "群聊时按全量消息条数(用户+所有角色)触发记忆整理。\n"
            "0=禁用，走各角色卡个人配置(默认)；\n"
            ">0=每N条全量消息触发一次，窗口内发言过的角色都整理这同一段对话(全群共用一个边界)。\n"
            "记忆模式仍用各角色卡自己的设置(summary/embedding_hybrid)。"
        )
        gs_layout.addRow("群聊记忆整理(条):", self.group_mem_interval)

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

    def _apply_lock(self):
        """按 lock_type 调整 UI 锁定（默认模式 "" 不做任何事，保持全自由流程零回归）。"""
        if self._lock_type == "single":
            # 角色预选+只读：隐藏角色多选 grid，显示已选定角色
            preset = next((c for c in self.characters if c.id == self._preset_character_id), None)
            name = preset.name if preset else "（未知角色）"
            self.preset_char_label.setText(f"已选定角色：{name}（单聊，不可更改）")
            self.preset_char_label.setVisible(True)
            self.char_group.setVisible(False)
            with QSignalBlocker(self.type_combo):
                self.type_combo.setCurrentIndex(0)  # single
            self.type_combo.setEnabled(False)
            self.group_settings.setVisible(False)
            # 按预选角色预填开场白下拉
            self._on_char_changed()
        elif self._lock_type == "group":
            # 世界书锁定为 preset
            idx = self.wb_combo.findData(self._preset_world_book_id)
            if idx >= 0:
                with QSignalBlocker(self.wb_combo):
                    self.wb_combo.setCurrentIndex(idx)
            self.wb_combo.setEnabled(False)
            with QSignalBlocker(self.type_combo):
                self.type_combo.setCurrentIndex(1)  # group
            self.type_combo.setEnabled(False)
            self.group_settings.setVisible(True)

    def _effective_selected_ids(self) -> list[str]:
        """单聊锁定时返回 [预选角色]；否则读角色多选 grid。"""
        if self._lock_type == "single":
            return [self._preset_character_id] if self._preset_character_id else []
        return self.char_grid.get_selected_ids()

    def _on_user_changed(self):
        """选用户时把「玩家名」覆盖行填成该用户名（可手动改）。"""
        uid = self.user_grid.get_selected_id()
        if not uid:
            return  # 「默认」项不动
        u = next((x for x in self.users if x.id == uid), None)
        if u and u.name:
            self.player_edit.setText(u.name)

    def _on_type_changed(self):
        # 切换会话类型时刷新开场白/群聊设置显隐
        self._on_char_changed()

    def _on_char_changed(self):
        selected_ids = self._effective_selected_ids()
        checked = len(selected_ids)
        is_group = checked > 1
        # [!] lock 模式类型已锁定，不自动同步 type_combo（默认模式才按角色数判定）
        if not self._lock_type:
            # 用 QSignalBlocker 防止 setCurrentIndex 触发 _on_type_changed 回环
            target_idx = 1 if is_group else 0
            with QSignalBlocker(self.type_combo):
                self.type_combo.setCurrentIndex(target_idx)
        # group_settings 显隐：lock=single 强隐；lock=group 强显；default 按 is_group
        if self._lock_type == "single":
            self.group_settings.setVisible(False)
        elif self._lock_type == "group":
            self.group_settings.setVisible(True)
        else:
            self.group_settings.setVisible(is_group)
        self._refresh_greeting_combo(checked)

    def _refresh_greeting_combo(self, checked_count: int = None):
        """单聊或群聊模式，填充开场白下拉框。

        - 单聊（1 个角色）：可选该角色的第一条消息 / 备选开场白。
        - 群聊（>1 个角色）：可选任一角色的开场白作为首条发言（显示带角色名前缀）。
        """
        selected_ids = self._effective_selected_ids()
        if checked_count is None:
            checked_count = len(selected_ids)
        self.greeting_combo.clear()
        # _greeting_options: 下拉项索引 -> (character_id, greeting_text)
        self._greeting_options = []
        if checked_count < 1:
            self.greeting_group.setVisible(False)
            return
        # 收集所有选中角色
        selected_chars = []
        for cid in selected_ids:
            char = next((c for c in self.characters if c.id == cid), None)
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
        selected = self._effective_selected_ids()
        if not selected:
            QMessageBox.warning(self, "提示", "请至少选择一个角色")
            return

        title = self.title_edit.text().strip()
        if not title:
            names = []
            for cid in selected:
                char = next((c for c in self.characters if c.id == cid), None)
                if char:
                    names.append(char.name)
            title = "、".join(names)

        # 会话类型：lock 模式强制；默认模式按角色数判定（原逻辑）
        if self._lock_type == "single":
            session_type = "single"
            is_group = False
        elif self._lock_type == "group":
            session_type = "group"
            is_group = True
        else:
            chosen_type = self.type_combo.currentData()
            if chosen_type == "single":
                session_type = "single" if len(selected) == 1 else "group"
            else:  # group
                session_type = "group" if len(selected) > 1 else "single"
            is_group = session_type == "group"

        # 世界书：group-lock 强制用 preset（wb_combo 已 disable 且选中 preset，currentData 一致）
        wb_id = self.wb_combo.currentData() if self.wb_combo.isEnabled() else self._preset_world_book_id

        self._result = {
            "title": title,
            "session_type": session_type,
            "character_ids": selected,
            "world_book_id": wb_id,
            "user_id": self.user_grid.get_selected_id(),
            "player_name": self.player_edit.text().strip() or "用户",
            "group_mode": self.mode_combo.currentData() if is_group else "manual",
            "director_api_id": self.director_combo.currentData() if is_group else "",
            "group_memory_interval": self.group_mem_interval.value() if is_group else 0,
            "greeting": self._greeting_options[self.greeting_combo.currentIndex()][1],
            "greeting_character_id": self._greeting_options[self.greeting_combo.currentIndex()][0],
            "auto_summary_enabled": self.sum_enabled_chk.isChecked(),
            "auto_summary_threshold": self.sum_threshold_spin.value(),
            "auto_summary_count": self.sum_count_spin.value(),
        }
        self.accept()

    def get_result(self) -> dict:
        return self._result