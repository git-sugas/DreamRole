"""用户卡管理对话框（轻量：姓名 + 头像 + 描述）。

与角色卡管理结构对称，但字段精简--用户不需要 api_id / 记忆 / 开场白等。
头像复用 paths.avatars_dir()（与角色共享头像目录）。
[!] 自动生成头像复用 character_editor._AvatarGenWorker（Danbooru 加工 + ComfyUI 出图），
逻辑与角色头像生成一致；用户无 appearance_tags，character_appearances 传 None。
"""
from __future__ import annotations
import os
import shutil
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QFormLayout,
    QGroupBox, QSplitter, QMessageBox, QFileDialog, QWidget,
)

from src.models import User
from src.config import paths
from src.ui.widgets.avatar_button import render_avatar
# 复用角色头像生成 worker（Danbooru 加工 + ComfyUI 出图，逻辑完全一致）
from src.ui.dialogs.character_editor import _AvatarGenWorker


class UserEditorDialog(QDialog):
    def __init__(self, storage, comfyui=None, danbooru=None, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.comfyui = comfyui
        self.danbooru = danbooru
        self.setWindowTitle("用户管理")
        self.resize(640, 560)
        self.setMinimumSize(600, 480)
        self._current: User | None = None
        # [!] 头像生成 worker 跟踪（与 character_editor 一致：防重入 + closeEvent 安全退出）
        self._avatar_worker: _AvatarGenWorker | None = None
        self._build_ui()
        self._load_list()

    def closeEvent(self, event):
        # [!] 关闭 dialog 时若头像生成 worker 还在跑，必须先 disconnect 信号 + wait
        # 等子线程结束，否则子线程 emit finished_signal 会触发已销毁 dialog 的槽
        # (_on_avatar_gen_done)，访问已释放的 C++ 对象导致 0xC0000409 进程崩溃。
        # _AvatarGenWorker 无取消机制（comfyui.generate 是阻塞调用），只能 wait。
        # 与 character_editor.closeEvent 完全对称。
        worker = self._avatar_worker
        if worker is not None:
            try:
                if worker.isRunning():
                    try:
                        worker.finished_signal.disconnect(self._on_avatar_gen_done)
                    except (TypeError, RuntimeError):
                        pass
                    worker.wait(120000)  # 最多等 120 秒
            except RuntimeError:
                pass
            self._avatar_worker = None
        super().closeEvent(event)

    def _build_ui(self):
        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        # 左：列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        new_btn = QPushButton("+ 新建用户")
        new_btn.setObjectName("primaryBtn")
        new_btn.clicked.connect(self._on_new)
        ll.addWidget(new_btn)
        self.user_list = QListWidget()
        self.user_list.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.user_list)
        del_btn = QPushButton("删除用户")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._on_delete)
        ll.addWidget(del_btn)
        splitter.addWidget(left)

        # 右：表单（用 QFormLayout + setMinimumHeight 给输入框稳定多行高）
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        form = QFormLayout()
        form.setSpacing(8)

        self.name_edit = QLineEdit()
        form.addRow("名字:", self.name_edit)

        self.desc_edit = QTextEdit()
        self.desc_edit.setMinimumHeight(100)
        self.desc_edit.setPlaceholderText("用户描述 / 人设（注入 BLOCK_USER 与 {{user_description}}）")
        form.addRow("描述:", self.desc_edit)

        rl.addLayout(form)

        # 头像
        avatar_group = QGroupBox("头像")
        al = QHBoxLayout(avatar_group)
        self.avatar_preview = QLabel()
        self.avatar_preview.setFixedSize(48, 48)
        self.avatar_label = QLabel("未设置")
        self.avatar_btn = QPushButton("选择头像")
        self.avatar_btn.clicked.connect(self._pick_avatar)
        self.gen_avatar_btn = QPushButton("自动生成头像")
        self.gen_avatar_btn.clicked.connect(self._on_gen_avatar)
        # ComfyUI 未启用（或工作流为空）时禁用 + tooltip 提示（与角色编辑器一致）
        if not (self.comfyui and self.comfyui.is_enabled()):
            self.gen_avatar_btn.setEnabled(False)
            self.gen_avatar_btn.setToolTip("请先在 ComfyUI 设置中启用并配置工作流")
        al.addWidget(self.avatar_preview)
        al.addWidget(self.avatar_label)
        al.addWidget(self.avatar_btn)
        al.addWidget(self.gen_avatar_btn)
        al.addStretch()
        rl.addWidget(avatar_group)

        # 保存
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._on_save)
        rl.addWidget(save_btn)
        rl.addStretch()
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    def _load_list(self):
        self.user_list.clear()
        for u in self.storage.load_all_users():
            item = QListWidgetItem(u.name or "未命名")
            item.setData(Qt.UserRole, u.id)
            self.user_list.addItem(item)

    def _on_new(self):
        u = User(name="新用户")
        self.storage.save_user(u)
        self._load_list()
        for i in range(self.user_list.count()):
            if self.user_list.item(i).data(Qt.UserRole) == u.id:
                self.user_list.setCurrentRow(i)
                break

    def _on_select(self, current, previous):
        if not current:
            self._current = None
            return
        uid = current.data(Qt.UserRole)
        u = self.storage.load_user(uid)
        if not u:
            return
        self._current = u
        self.name_edit.setText(u.name)
        self.desc_edit.setPlainText(u.description)
        self.avatar_label.setText(u.avatar or "未设置")
        self._refresh_avatar_preview()

    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择头像", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.gif)"
        )
        if path:
            ext = os.path.splitext(path)[1]
            filename = f"{uuid.uuid4().hex[:8]}{ext}"
            dest = os.path.join(paths.avatars_dir(), filename)
            shutil.copy2(path, dest)
            if self._current:
                self._current.avatar = filename
                self.storage.save_user(self._current)
            self.avatar_label.setText(filename)
            self._refresh_avatar_preview()

    def _on_gen_avatar(self):
        """自动生成头像：用用户 description 作中文描述，后台跑 Danbooru 加工 ->
        ComfyUI 出图，完成后写回 avatar 并落库。

        与 character_editor._on_gen_avatar 对称，区别：用户无 appearance_tags，
        character_appearances 传 None（Danbooru 加工时不注入角色固定外貌参考）。
        """
        if not self._current:
            return
        if not (self.comfyui and self.comfyui.is_enabled()):
            QMessageBox.warning(self, "无法生成", "请先在 ComfyUI 设置中启用并配置工作流")
            return
        # 先取当前表单描述（可能刚改未存）
        description = self.desc_edit.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "无法生成", "请先填写用户描述（作为生图中文描述）")
            return
        # 防重复启动（与角色编辑器一致的 try 兜底防御）
        try:
            if self._avatar_worker and self._avatar_worker.isRunning():
                return
        except RuntimeError:
            self._avatar_worker = None
        self.gen_avatar_btn.setEnabled(False)
        self.gen_avatar_btn.setText("生成中…")
        self._avatar_worker = _AvatarGenWorker(
            description, None, self.comfyui, self.danbooru,
        )
        self._avatar_worker.finished_signal.connect(self._on_avatar_gen_done)
        self._avatar_worker.finished.connect(self._avatar_worker.deleteLater)
        self._avatar_worker.start()

    def _on_avatar_gen_done(self, ok: bool, msg: str):
        """worker 完成回调：ok=True 时 msg 是 avatars_dir 下图片绝对路径。"""
        # [!] 置 None 释放 Python 引用（与角色编辑器一致，防 RuntimeError）
        self._avatar_worker = None
        if self.comfyui and self.comfyui.is_enabled():
            self.gen_avatar_btn.setEnabled(True)
        self.gen_avatar_btn.setText("自动生成头像")
        if not ok:
            QMessageBox.warning(self, "头像生成失败", msg)
            return
        if not self._current:
            return
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
        self.storage.save_user(self._current)
        self.avatar_label.setText(new_name)
        self._refresh_avatar_preview()

    def _refresh_avatar_preview(self):
        """刷新头像预览框（支持 GIF 动图播放）。"""
        name = self._current.name if self._current else ""
        avatar = self._current.avatar if self._current else ""
        render_avatar(self.avatar_preview, name, avatar, 48)

    def _on_save(self):
        if not self._current:
            return
        u = self._current
        u.name = self.name_edit.text().strip() or "未命名"
        u.description = self.desc_edit.toPlainText()
        u.touch()
        self.storage.save_user(u)
        self._load_list()
        for i in range(self.user_list.count()):
            if self.user_list.item(i).data(Qt.UserRole) == u.id:
                self.user_list.setCurrentRow(i)
                break
        QMessageBox.information(self, "已保存", f"用户「{u.name}」已保存。")

    def _on_delete(self):
        if not self._current:
            return
        reply = QMessageBox.question(self, "确认", f"删除用户「{self._current.name}」？")
        if reply == QMessageBox.Yes:
            # 清理头像文件
            if self._current.avatar:
                avatar_path = os.path.join(paths.avatars_dir(), self._current.avatar)
                if os.path.exists(avatar_path):
                    try:
                        os.remove(avatar_path)
                    except OSError:
                        pass
            self.storage.delete_user(self._current.id)
            self._current = None
            self._load_list()