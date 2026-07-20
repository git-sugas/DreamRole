"""角色卡编辑器对话框。"""
from __future__ import annotations
import os
import shutil
import uuid

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QComboBox, QFormLayout,
    QGroupBox, QMessageBox, QFileDialog, QWidget, QSpinBox,
    QScrollArea,
)

from src.models import Character
from src.config import paths
from src.ui.widgets.avatar_button import render_avatar


class CharacterEditorDialog(QDialog):
    def __init__(self, storage, comfyui=None, danbooru=None, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.comfyui = comfyui
        self.danbooru = danbooru
        self.setWindowTitle("角色卡管理")
        self.resize(1200, 1280)
        self.setMinimumSize(900, 720)
        self._current: Character | None = None
        self._mem_rows: dict[str, tuple[QLabel, QWidget]] = {}  # 记忆字段行：key->(label, field)
        self._avatar_worker: _AvatarGenWorker | None = None  # 头像生成 worker（避免重复启动）
        self._build_ui()
        self._load_list()

    def closeEvent(self, event):
        # [!] 关闭 dialog 时若头像生成 worker 还在跑，必须先 disconnect 信号 + wait
        # 等子线程结束，否则子线程 emit finished_signal 会触发已销毁 dialog 的槽
        # (_on_avatar_gen_done)，访问已释放的 C++ 对象导致 0xC0000409 进程崩溃。
        # _AvatarGenWorker 无取消机制（comfyui.generate 是阻塞调用），只能 wait。
        worker = self._avatar_worker
        if worker is not None:
            try:
                if worker.isRunning():
                    # 先 disconnect 槽，避免 worker 结束时 emit 触发已销毁的接收者
                    try:
                        worker.finished_signal.disconnect(self._on_avatar_gen_done)
                    except (TypeError, RuntimeError):
                        pass
                    # 阻塞等待子线程结束（comfyui.generate 可能要几十秒，UI 会卡住，
                    # 但这是无取消机制下的唯一安全做法；用户可见对话框暂不消失）
                    worker.wait(120000)  # 最多等 120 秒
            except RuntimeError:
                # C++ 对象已删除（极端时序），忽略
                pass
            self._avatar_worker = None
        super().closeEvent(event)

    def _build_ui(self):
        layout = QHBoxLayout(self)

        # 左：列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        new_btn = QPushButton("+ 新建角色")
        new_btn.setObjectName("primaryBtn")
        new_btn.clicked.connect(self._on_new)
        ll.addWidget(new_btn)

        self.char_list = QListWidget()
        self.char_list.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.char_list)

        del_btn = QPushButton("删除角色")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._on_delete)
        ll.addWidget(del_btn)
        layout.addWidget(left)
        left.setFixedWidth(220)

        # 右：表单（QScrollArea 包裹，防表单过高时底部按钮被顶出可视区）。
        # [!] 不用 QSplitter：splitter 会按 QTextEdit 一行高的 sizeHint 把多行编辑框
        # 压成一行（setFixedHeight/setMinimumHeight 均失效）。改用 QHBoxLayout 直接放
        # 左右两栏（与 api_dialog.py 系统提示框同款结构，已验证 setFixedHeight 生效）。
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_inner = QWidget()
        rl = QVBoxLayout(right_inner)
        # 用 QFormLayout + setFixedHeight（api_dialog.py 系统提示框同款写法，已验证有效）。
        form = QFormLayout()
        form.setSpacing(8)

        self.name_edit = QLineEdit()
        form.addRow("名字:", self.name_edit)

        self.api_combo = QComboBox()
        form.addRow("绑定API:", self.api_combo)

        self.desc_edit = QTextEdit()
        self.desc_edit.setFixedHeight(96)
        form.addRow("描述:", self.desc_edit)

        self.personality_edit = QTextEdit()
        self.personality_edit.setFixedHeight(80)
        form.addRow("性格:", self.personality_edit)

        self.scenario_edit = QTextEdit()
        self.scenario_edit.setFixedHeight(80)
        form.addRow("场景:", self.scenario_edit)

        self.first_msg_edit = QTextEdit()
        self.first_msg_edit.setFixedHeight(96)
        form.addRow("第一条消息:", self.first_msg_edit)

        self.example_edit = QTextEdit()
        self.example_edit.setFixedHeight(96)
        form.addRow("对话示例:", self.example_edit)

        self.appearance_edit = QTextEdit()
        self.appearance_edit.setFixedHeight(80)
        self.appearance_edit.setPlaceholderText(
            "固定外貌 tag（原生英文 Danbooru tag 逗号分隔，如 long_hair, fox_ears, nine_tails）。"
            "生图时注入 LLM 让其按描述选用，不参与召回。头像自动生成与群聊文生图都会用到。"
        )
        form.addRow("固定外貌tag:", self.appearance_edit)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("逗号分隔")
        form.addRow("标签:", self.tags_edit)

        self.creator_edit = QLineEdit()
        form.addRow("作者:", self.creator_edit)
        rl.addLayout(form)

        # 备选开场白（alternate_greetings）
        greet_group = QGroupBox("备选开场白")
        gl = QVBoxLayout(greet_group)
        self.greetings_list = QListWidget()
        self.greetings_list.setMaximumHeight(90)
        gl.addWidget(self.greetings_list)
        greet_btns = QHBoxLayout()
        add_greet_btn = QPushButton("添加")
        add_greet_btn.clicked.connect(self._add_greeting)
        edit_greet_btn = QPushButton("编辑")
        edit_greet_btn.clicked.connect(self._edit_greeting)
        del_greet_btn = QPushButton("删除")
        del_greet_btn.clicked.connect(self._del_greeting)
        greet_btns.addWidget(add_greet_btn)
        greet_btns.addWidget(edit_greet_btn)
        greet_btns.addWidget(del_greet_btn)
        greet_btns.addStretch()
        gl.addLayout(greet_btns)
        rl.addWidget(greet_group)

        # 头像
        avatar_row = QHBoxLayout()
        self.avatar_preview = QLabel()
        self.avatar_preview.setFixedSize(48, 48)
        self.avatar_label = QLabel("未设置")
        self.avatar_btn = QPushButton("选择头像")
        self.avatar_btn.clicked.connect(self._pick_avatar)
        self.gen_avatar_btn = QPushButton("自动生成头像")
        self.gen_avatar_btn.clicked.connect(self._on_gen_avatar)
        # ComfyUI 未启用（或工作流为空）时禁用 + tooltip 提示
        if not (self.comfyui and self.comfyui.is_enabled()):
            self.gen_avatar_btn.setEnabled(False)
            self.gen_avatar_btn.setToolTip("请先在 ComfyUI 设置中启用并配置工作流")
        avatar_row.addWidget(self.avatar_preview)
        avatar_row.addWidget(self.avatar_label)
        avatar_row.addWidget(self.avatar_btn)
        avatar_row.addWidget(self.gen_avatar_btn)
        avatar_row.addStretch()
        rl.addLayout(avatar_row)

        # 记忆设置（按模式动态显隐对应字段：summary→总结间隔+总结窗口；
        # embedding→整理间隔+检索top-k；none→全隐）。
        mem_group = QGroupBox("记忆设置")
        mem_layout = QFormLayout(mem_group)
        # 记忆模式选择：始终可见
        self.memory_combo = QComboBox()
        self.memory_combo.addItem("无", "none")
        self.memory_combo.addItem("AI总结", "summary")
        self.memory_combo.addItem("Embedding混合(三路召回)", "embedding_hybrid")
        self.memory_combo.currentIndexChanged.connect(self._on_memory_mode_changed)
        mem_layout.addRow("记忆模式:", self.memory_combo)

        def _mem_row(key: str, text: str, field: QWidget, tooltip: str = ""):
            """记忆字段行：显式 QLabel 便于整行同步显隐（避免 labelForField 不可靠）。"""
            lbl = QLabel(text)
            if tooltip:
                field.setToolTip(tooltip)
            mem_layout.addRow(lbl, field)
            self._mem_rows[key] = (lbl, field)

        self.mem_interval = QSpinBox()
        self.mem_interval.setRange(1, 100)
        self.mem_interval.setValue(20)
        self.mem_interval.setToolTip("每隔多少条该角色消息触发一次总结")
        _mem_row("summary_interval", "总结间隔(条):", self.mem_interval)

        self.mem_window = QSpinBox()
        self.mem_window.setRange(1, 200)
        self.mem_window.setValue(20)
        self.mem_window.setToolTip("每次总结时取最近多少条对话喂给 AI（与触发间隔解耦，可大于间隔）")
        _mem_row("summary_window", "总结窗口(条):", self.mem_window)

        self.embed_interval = QSpinBox()
        self.embed_interval.setRange(1, 100)
        self.embed_interval.setValue(1)
        self.embed_interval.setToolTip("每隔多少条 AI 回复触发一次整理入库（1=每条都整理，越大越省 embedding 调用）")
        _mem_row("embedding_interval", "整理间隔(条):", self.embed_interval)

        rl.addWidget(mem_group)

        # 保存按钮
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._on_save)
        rl.addWidget(save_btn)
        rl.addStretch()
        right.setWidget(right_inner)
        layout.addWidget(right, 1)

    def _load_list(self):
        self.char_list.clear()
        self._refresh_api_combo()
        for char in self.storage.load_all_characters():
            item = QListWidgetItem(char.name or "未命名")
            item.setData(Qt.UserRole, char.id)
            self.char_list.addItem(item)

    def _refresh_api_combo(self):
        self.api_combo.clear()
        for api in self.storage.load_all_apis():
            self.api_combo.addItem(api.name, api.id)

    def _on_new(self):
        char = Character(name="新角色")
        self.storage.save_character(char)
        self._load_list()
        # 选中新建的
        for i in range(self.char_list.count()):
            if self.char_list.item(i).data(Qt.UserRole) == char.id:
                self.char_list.setCurrentRow(i)
                break

    def _on_select(self, current, previous):
        if not current:
            self._current = None
            return
        char_id = current.data(Qt.UserRole)
        char = self.storage.load_character(char_id)
        if not char:
            return
        self._current = char
        self.name_edit.setText(char.name)
        self.desc_edit.setPlainText(char.description)
        self.personality_edit.setPlainText(char.personality)
        self.scenario_edit.setPlainText(char.scenario)
        self.first_msg_edit.setPlainText(char.first_message)
        self.example_edit.setPlainText(char.mes_example)
        self.appearance_edit.setPlainText(char.appearance_tags)
        # 备选开场白
        self.greetings_list.clear()
        for i, g in enumerate(char.alternate_greetings):
            preview = g.replace("\n", " ").strip()
            self.greetings_list.addItem(f"{i + 1}. {preview[:40]}")
        self.tags_edit.setText(", ".join(char.tags))
        self.creator_edit.setText(char.creator)
        self.avatar_label.setText(char.avatar or "未设置")
        self._refresh_avatar_preview()
        # API
        idx = self.api_combo.findData(char.api_id)
        self.api_combo.setCurrentIndex(idx if idx >= 0 else 0)
        # 记忆
        idx = self.memory_combo.findData(char.memory_mode)
        self.memory_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.mem_interval.setValue(char.memory_config.get("summary_interval", 20))
        # 总结窗口：老角色卡缺该字段时回退到总结间隔（与服务层一致，行为零变化）
        cfg_window = char.memory_config.get("summary_window", 0)
        self.mem_window.setValue(cfg_window or char.memory_config.get("summary_interval", 20))
        self.embed_interval.setValue(char.memory_config.get("embedding_interval", 1))
        self._on_memory_mode_changed()

    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择头像", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.gif)"
        )
        if path:
            import uuid
            ext = os.path.splitext(path)[1]
            filename = f"{uuid.uuid4().hex[:8]}{ext}"
            dest = os.path.join(paths.avatars_dir(), filename)
            shutil.copy2(path, dest)
            if self._current:
                self._current.avatar = filename
                self.storage.save_character(self._current)
            self.avatar_label.setText(filename)
            self._refresh_avatar_preview()

    def _on_gen_avatar(self):
        """自动生成头像：用角色 description 作中文描述 + 本角色固定外貌 tag，
        后台跑 Danbooru 加工 -> ComfyUI 出图，完成后写回 avatar 并落库。
        """
        if not self._current:
            return
        if not (self.comfyui and self.comfyui.is_enabled()):
            QMessageBox.warning(self, "无法生成", "请先在 ComfyUI 设置中启用并配置工作流")
            return
        # 先保存当前表单（description/appearance_tags 可能刚改未存）
        description = self.desc_edit.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "无法生成", "请先填写角色描述（作为生图中文描述）")
            return
        appearance_tags = self.appearance_edit.toPlainText().strip()
        char_appearances = (
            [(self._current.name, appearance_tags)] if appearance_tags else None
        )
        # 防重复启动
        # [!] _avatar_worker 在 _on_avatar_gen_done 里置 None，但 deleteLater 是异步的，
        # 万一回调时序异常导致 Python 引用还在但 C++ 对象已删除，isRunning() 会抛
        # RuntimeError。用 try 兜底防御，异常时视为未运行，允许新建 worker。
        try:
            if self._avatar_worker and self._avatar_worker.isRunning():
                return
        except RuntimeError:
            self._avatar_worker = None
        self.gen_avatar_btn.setEnabled(False)
        self.gen_avatar_btn.setText("生成中…")
        self._avatar_worker = _AvatarGenWorker(
            description, char_appearances, self.comfyui, self.danbooru,
        )
        self._avatar_worker.finished_signal.connect(self._on_avatar_gen_done)
        self._avatar_worker.finished.connect(self._avatar_worker.deleteLater)
        self._avatar_worker.start()

    def _on_avatar_gen_done(self, ok: bool, msg: str):
        """worker 完成回调：ok=True 时 msg 是 avatars_dir 下图片绝对路径。"""
        # [!] 置 None 释放 Python 引用：finished 已连 deleteLater 清 C++ 对象，
        # 但 self._avatar_worker 引用不置 None 会导致下次 _on_gen_avatar 访问已删除对象
        # 报 RuntimeError: Internal C++ object already deleted。
        self._avatar_worker = None
        # 恢复按钮状态（ComfyUI 仍启用时恢复可点）
        if self.comfyui and self.comfyui.is_enabled():
            self.gen_avatar_btn.setEnabled(True)
        self.gen_avatar_btn.setText("自动生成头像")
        if not ok:
            QMessageBox.warning(self, "头像生成失败", msg)
            return
        if not self._current:
            return
        # msg 是 avatars_dir 下的临时文件名（img_xxx.png），改名为 uuid8 风格与现有头像一致
        src_path = msg
        if not os.path.exists(src_path):
            QMessageBox.warning(self, "头像生成失败", "生成的图片文件未找到")
            return
        new_name = f"{uuid.uuid4().hex[:8]}.png"
        new_path = os.path.join(paths.avatars_dir(), new_name)
        try:
            shutil.move(src_path, new_path)
        except OSError as e:
            QMessageBox.warning(self, "头像生成失败", f"保存头像文件失败: {e}")
            return
        # 删旧头像文件（若有）
        old_avatar = self._current.avatar
        if old_avatar:
            old_path = os.path.join(paths.avatars_dir(), old_avatar)
            try:
                if os.path.exists(old_path) and os.path.abspath(old_path) != os.path.abspath(new_path):
                    os.remove(old_path)
            except OSError:
                pass
        self._current.avatar = new_name
        self.storage.save_character(self._current)
        self.avatar_label.setText(new_name)
        self._refresh_avatar_preview()

    def _refresh_avatar_preview(self):
        """刷新头像预览框（支持 GIF 动图播放）。"""
        name = self._current.name if self._current else ""
        avatar = self._current.avatar if self._current else ""
        render_avatar(self.avatar_preview, name, avatar, 48)

    def _on_memory_mode_changed(self):
        """根据记忆模式显隐对应的配置控件（整行 label+field 同步显隐）。

        summary -> 总结间隔 + 总结窗口
        embedding_hybrid -> 整理间隔
        none -> 全隐
        """
        mode = self.memory_combo.currentData()
        visible_keys: set[str] = set()
        if mode == "summary":
            visible_keys = {"summary_interval", "summary_window"}
        elif mode == "embedding_hybrid":
            visible_keys = {"embedding_interval"}
        for key, (lbl, field) in self._mem_rows.items():
            on = key in visible_keys
            lbl.setVisible(on)
            field.setVisible(on)

    def _refresh_greetings_list(self):
        """刷新备选开场白列表显示。"""
        self.greetings_list.clear()
        for i, g in enumerate(self._current.alternate_greetings if self._current else []):
            preview = g.replace("\n", " ").strip()
            self.greetings_list.addItem(f"{i + 1}. {preview[:40]}")

    def _add_greeting(self):
        if not self._current:
            return
        text = self._edit_greeting_dialog("", "添加备选开场白")
        if text is not None:
            self._current.alternate_greetings.append(text)
            self._refresh_greetings_list()

    def _edit_greeting(self):
        if not self._current:
            return
        row = self.greetings_list.currentRow()
        if row < 0 or row >= len(self._current.alternate_greetings):
            QMessageBox.information(self, "提示", "请先选择一条开场白")
            return
        old = self._current.alternate_greetings[row]
        text = self._edit_greeting_dialog(old, "编辑开场白")
        if text is not None:
            self._current.alternate_greetings[row] = text
            self._refresh_greetings_list()

    def _del_greeting(self):
        if not self._current:
            return
        row = self.greetings_list.currentRow()
        if row < 0 or row >= len(self._current.alternate_greetings):
            QMessageBox.information(self, "提示", "请先选择一条开场白")
            return
        del self._current.alternate_greetings[row]
        self._refresh_greetings_list()

    def _edit_greeting_dialog(self, initial: str, title: str):
        """弹出多行文本编辑对话框，返回文本或 None（取消）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dl = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setPlainText(initial)
        te.setMinimumHeight(160)
        dl.addWidget(te)
        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            return te.toPlainText()
        return None

    def _on_save(self):
        if not self._current:
            return
        c = self._current
        c.name = self.name_edit.text().strip() or "未命名"
        c.description = self.desc_edit.toPlainText()
        c.personality = self.personality_edit.toPlainText()
        c.scenario = self.scenario_edit.toPlainText()
        c.first_message = self.first_msg_edit.toPlainText()
        c.mes_example = self.example_edit.toPlainText()
        c.appearance_tags = self.appearance_edit.toPlainText().strip()
        c.tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        c.creator = self.creator_edit.text().strip()
        c.api_id = self.api_combo.currentData() or ""
        c.memory_mode = self.memory_combo.currentData()
        c.memory_config = {
            "summary_interval": self.mem_interval.value(),
            "summary_window": self.mem_window.value(),
            "embedding_interval": self.embed_interval.value(),
        }
        c.touch()
        self.storage.save_character(c)
        self._load_list()
        # 重新选中
        for i in range(self.char_list.count()):
            if self.char_list.item(i).data(Qt.UserRole) == c.id:
                self.char_list.setCurrentRow(i)
                break
        QMessageBox.information(self, "已保存", f"角色「{c.name}」已保存。")

    def _on_delete(self):
        if not self._current:
            return
        reply = QMessageBox.question(self, "确认", f"删除角色「{self._current.name}」？")
        if reply == QMessageBox.Yes:
            # 清理头像文件
            if self._current.avatar:
                avatar_path = os.path.join(paths.avatars_dir(), self._current.avatar)
                if os.path.exists(avatar_path):
                    try:
                        os.remove(avatar_path)
                    except OSError:
                        pass
            self.storage.delete_character(self._current.id)
            self._current = None
            self._load_list()


class _AvatarGenWorker(QThread):
    """后台跑 Danbooru 加工 + ComfyUI 出图生成头像。

    仿 _ConnectionTestWorker 模式：__init__ 传服务实例+参数，run() 内惰性 import
    避免循环依赖，异常统一包成 (False, msg) emit。finished_signal: (ok, 头像路径或错误信息)。
    """
    finished_signal = Signal(bool, str)

    def __init__(self, description, character_appearances, comfyui, danbooru):
        super().__init__()
        self.description = description
        self.character_appearances = character_appearances
        self.comfyui = comfyui
        self.danbooru = danbooru

    def run(self):
        try:
            # 1. Danbooru 加工（含中文走 RAG，纯英文透传；character_appearances 注入 LLM）
            if self.danbooru is not None:
                positive, negative = self.danbooru.process_image_description(
                    self.description, session_api=None,
                    character_appearances=self.character_appearances,
                )
            else:
                # 无 Danbooru 服务：纯英文透传，中文则无法加工（直接用原文给 ComfyUI）
                positive, negative = self.description, ""
            if not positive:
                self.finished_signal.emit(False, "tag 加工失败，请检查 Danbooru 设置与日志")
                return
            # 2. ComfyUI 出图（直接存到 avatars_dir）
            avatar_path = self.comfyui.generate(
                positive, negative, dest_dir=paths.avatars_dir()
            )
            if not avatar_path:
                self.finished_signal.emit(False, "图片生成失败，请检查 ComfyUI 服务与工作流")
                return
            self.finished_signal.emit(True, avatar_path)
        except Exception as e:
            self.finished_signal.emit(False, str(e))