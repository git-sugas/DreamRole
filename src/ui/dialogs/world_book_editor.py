"""世界书编辑器对话框。"""
from __future__ import annotations

import os
import shutil
import uuid

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QComboBox, QFormLayout,
    QSplitter, QMessageBox, QWidget, QSpinBox, QCheckBox,
    QScrollArea, QFileDialog,
)

from src.config import paths
from src.models import WorldBook, WorldBookEntry


class WorldBookEditorDialog(QDialog):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("世界书管理")
        # 加宽：右栏新增封面行（72px 预览 + 文件名 + 选择/清除两按钮），900 偏挤
        self.resize(1040, 680)
        self.setMinimumSize(940, 540)
        self._current_wb: WorldBook | None = None
        self._current_entry: WorldBookEntry | None = None
        self._build_ui()
        self._load_wb_list()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        # 左：世界书列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("世界书"))
        new_btn = QPushButton("+ 新建")
        new_btn.setObjectName("primaryBtn")
        new_btn.clicked.connect(self._new_wb)
        ll.addWidget(new_btn)
        self.wb_list = QListWidget()
        self.wb_list.setIconSize(QSize(28, 28))
        self.wb_list.currentItemChanged.connect(self._select_wb)
        ll.addWidget(self.wb_list)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._delete_wb)
        ll.addWidget(del_btn)
        splitter.addWidget(left)

        # 中：条目列表
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.addWidget(QLabel("条目"))
        new_entry_btn = QPushButton("+ 新建条目")
        new_entry_btn.clicked.connect(self._new_entry)
        ml.addWidget(new_entry_btn)
        self.entry_list = QListWidget()
        self.entry_list.currentItemChanged.connect(self._select_entry)
        ml.addWidget(self.entry_list)
        del_entry_btn = QPushButton("删除条目")
        del_entry_btn.setObjectName("dangerBtn")
        del_entry_btn.clicked.connect(self._delete_entry)
        ml.addWidget(del_entry_btn)
        splitter.addWidget(mid)

        # 右：条目编辑（包 QScrollArea，防止表单高出对话框时底部保存按钮被裁）
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_inner = QWidget()
        rl = QVBoxLayout(right_inner)
        form = QFormLayout()

        self.wb_name = QLineEdit()
        form.addRow("世界书名:", self.wb_name)

        # 封面（矩形缩略图，存 covers_dir；纯展示用，不进 LLM 上下文）
        cover_container = QWidget()
        cover_row = QHBoxLayout(cover_container)
        cover_row.setContentsMargins(0, 0, 0, 0)
        cover_row.setSpacing(6)
        self.cover_preview = QLabel()
        self.cover_preview.setFixedSize(72, 72)
        self.cover_preview.setAlignment(Qt.AlignCenter)
        self.cover_preview.setStyleSheet("background:#1a1b26; border-radius:6px;")
        self.cover_label = QLabel("未设置")
        self.cover_label.setStyleSheet("color:#565f89;")
        self.cover_btn = QPushButton("选择封面")
        self.cover_btn.clicked.connect(self._pick_cover)
        self.clear_cover_btn = QPushButton("清除")
        self.clear_cover_btn.clicked.connect(self._clear_cover)
        cover_row.addWidget(self.cover_preview)
        cover_row.addWidget(self.cover_label, 1)
        cover_row.addWidget(self.cover_btn)
        cover_row.addWidget(self.clear_cover_btn)
        form.addRow("封面:", cover_container)

        self.entry_keys = QLineEdit()
        self.entry_keys.setPlaceholderText("逗号分隔的关键词")
        form.addRow("关键词:", self.entry_keys)

        self.entry_content = QTextEdit()
        self.entry_content.setMinimumHeight(100)
        form.addRow("内容:", self.entry_content)

        self.entry_position = QComboBox()
        self.entry_position.addItem("角色描述前", "before_char")
        self.entry_position.addItem("角色描述后", "after_char")
        self.entry_position.addItem("消息前(AN前)", "before_an")
        self.entry_position.addItem("消息后(AN后)", "after_an")
        self.entry_position.addItem("顶部", "at_top")
        self.entry_position.addItem("底部", "at_bottom")
        self.entry_position.setToolTip(
            "注：当前实现下，世界书统一合并到「世界书」上下文块内，"
            "按注入顺序(insertion_order)排序后整体注入，position 字段仅作记录、不影响实际位置。"
        )
        form.addRow("位置:", self.entry_position)

        self.entry_order = QSpinBox()
        self.entry_order.setRange(0, 999)
        self.entry_order.setValue(100)
        form.addRow("注入顺序:", self.entry_order)

        self.entry_enabled = QCheckBox("启用")
        self.entry_enabled.setChecked(True)
        form.addRow("", self.entry_enabled)

        self.entry_constant = QCheckBox("始终注入(不依赖关键词)")
        form.addRow("", self.entry_constant)

        self.entry_selective = QCheckBox("选择性(需次关键词也匹配)")
        form.addRow("", self.entry_selective)

        self.entry_secondary = QLineEdit()
        self.entry_secondary.setPlaceholderText("逗号分隔的次关键词")
        form.addRow("次关键词:", self.entry_secondary)
        rl.addLayout(form)

        save_btn = QPushButton("保存条目")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_entry)
        rl.addWidget(save_btn)
        rl.addStretch()
        right.setWidget(right_inner)
        splitter.addWidget(right)

        splitter.setStretchFactor(2, 1)
        layout.addWidget(splitter)

    def _load_wb_list(self):
        self.wb_list.clear()
        # 按 updated_at 倒序，最新的在最上面（老世界书无时间字段回退 _now）
        wbs = sorted(
            self.storage.load_all_world_books(),
            key=lambda w: w.updated_at or w.created_at,
            reverse=True,
        )
        for wb in wbs:
            item = QListWidgetItem(wb.name or "未命名")
            item.setData(Qt.UserRole, wb.id)
            icon = self._wb_icon(wb.cover)
            if icon is not None:
                item.setIcon(icon)
            self.wb_list.addItem(item)

    def _new_wb(self):
        wb = WorldBook(name="新世界书")
        wb.touch()
        self.storage.save_world_book(wb)
        self._load_wb_list()
        for i in range(self.wb_list.count()):
            if self.wb_list.item(i).data(Qt.UserRole) == wb.id:
                self.wb_list.setCurrentRow(i)

    def _select_wb(self, current, previous):
        self.entry_list.clear()
        if not current:
            self._current_wb = None
            self._clear_entry_form()
            self._load_cover_preview()
            return
        wb = self.storage.load_world_book(current.data(Qt.UserRole))
        if not wb:
            return
        self._current_wb = wb
        self.wb_name.setText(wb.name)
        self._load_cover_preview()
        # 按 insertion_order 排序显示（与 context_builder 实际注入顺序一致，order 小的在上）
        sorted_entries = sorted(wb.entries, key=lambda e: e.insertion_order)
        for entry in sorted_entries:
            label = ", ".join(entry.keys) or "(无关键词)"
            if not entry.enabled:
                label += " [禁用]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry.id)
            self.entry_list.addItem(item)
        # [!] 选世界书时自动加载第一个条目到右侧表单（否则右侧空白，用户以为没渲染）。
        # [!] 不用 entry_list.setCurrentRow(0) 触发信号：addItem 后立即 setCurrentRow
        # 在某些 Qt 时序下 currentItem 未更新，信号回调里 current 可能为 None 致表单不填。
        # 改为直接手动加载第一个条目（绕过信号时序问题），并同步选中列表项。
        if sorted_entries:
            self.entry_list.setCurrentRow(0)
            self._load_entry_form(sorted_entries[0])
        else:
            self._current_entry = None
            self._clear_entry_form()

    def _clear_entry_form(self):
        """清空右侧条目编辑表单（无世界书/无条目时调用）。"""
        self._current_entry = None
        self.entry_keys.clear()
        self.entry_content.clear()
        self.entry_position.setCurrentIndex(0)
        self.entry_order.setValue(100)
        self.entry_enabled.setChecked(True)
        self.entry_constant.setChecked(False)
        self.entry_selective.setChecked(False)
        self.entry_secondary.clear()

    def _delete_wb(self):
        if not self._current_wb:
            return
        if QMessageBox.question(self, "确认", "删除此世界书？") == QMessageBox.Yes:
            # 删除封面文件（镜像角色卡删头像：storage 只删 JSON，图片文件由编辑器清）
            self._remove_cover_file(self._current_wb.cover)
            self.storage.delete_world_book(self._current_wb.id)
            self._current_wb = None
            self._load_cover_preview()
            self._load_wb_list()
            self.entry_list.clear()

    def _new_entry(self):
        if not self._current_wb:
            QMessageBox.warning(self, "提示", "请先选择世界书")
            return
        entry = WorldBookEntry(keys=["新关键词"], content="新内容")
        self._current_wb.entries.append(entry)
        self._current_wb.touch()
        self.storage.save_world_book(self._current_wb)
        self._select_wb(self.wb_list.currentItem(), None)

    def _select_entry(self, current, previous):
        if not current or not self._current_wb:
            self._current_entry = None
            return
        entry_id = current.data(Qt.UserRole)
        entry = next((e for e in self._current_wb.entries if e.id == entry_id), None)
        if not entry:
            return
        self._load_entry_form(entry)

    def _load_entry_form(self, entry: WorldBookEntry):
        """填充右侧条目编辑表单（_select_entry 与 _select_wb 自动加载首个条目共用）。"""
        self._current_entry = entry
        self.wb_name.setText(self._current_wb.name)
        self.entry_keys.setText(", ".join(entry.keys))
        self.entry_content.setPlainText(entry.content)
        idx = self.entry_position.findData(entry.position)
        self.entry_position.setCurrentIndex(idx if idx >= 0 else 0)
        self.entry_order.setValue(entry.insertion_order)
        self.entry_enabled.setChecked(entry.enabled)
        self.entry_constant.setChecked(entry.constant)
        self.entry_selective.setChecked(entry.selective)
        self.entry_secondary.setText(", ".join(entry.secondary_keys))

    def _save_entry(self):
        if not self._current_wb:
            return
        # 世界书名始终随保存生效（即使没选条目也能改名）
        self._current_wb.name = self.wb_name.text().strip() or "未命名"
        if self._current_entry:
            e = self._current_entry
            e.keys = [k.strip() for k in self.entry_keys.text().split(",") if k.strip()]
            e.content = self.entry_content.toPlainText()
            e.position = self.entry_position.currentData()
            e.insertion_order = self.entry_order.value()
            e.enabled = self.entry_enabled.isChecked()
            e.constant = self.entry_constant.isChecked()
            e.selective = self.entry_selective.isChecked()
            e.secondary_keys = [k.strip() for k in self.entry_secondary.text().split(",") if k.strip()]
        self._current_wb.touch()
        self.storage.save_world_book(self._current_wb)
        self._select_wb(self.wb_list.currentItem(), None)
        QMessageBox.information(self, "已保存", f"世界书「{self._current_wb.name}」已保存。")

    def _delete_entry(self):
        if not self._current_entry or not self._current_wb:
            return
        self._current_wb.entries = [
            e for e in self._current_wb.entries if e.id != self._current_entry.id
        ]
        self._current_wb.touch()
        self.storage.save_world_book(self._current_wb)
        self._current_entry = None
        self._select_wb(self.wb_list.currentItem(), None)

    # ============ 封面 ============
    def _load_cover_preview(self):
        """刷新右栏封面预览（矩形，居中裁剪为 72x72，不拉伸变形）。"""
        if not self._current_wb or not self._current_wb.cover:
            self.cover_preview.clear()
            self.cover_label.setText("未设置")
            self.cover_label.setStyleSheet("color:#565f89;")
            return
        cover = self._current_wb.cover
        self.cover_label.setText(cover)
        self.cover_label.setStyleSheet("color:#9aa5ce;")
        path = os.path.join(paths.covers_dir(), cover)
        if not os.path.exists(path):
            self.cover_preview.clear()
            return
        pix = QPixmap(path)
        if pix.isNull():
            self.cover_preview.clear()
            return
        src = pix.scaled(72, 72, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = (src.width() - 72) // 2
        y = (src.height() - 72) // 2
        self.cover_preview.setPixmap(src.copy(x, y, 72, 72))

    def _wb_icon(self, cover: str):
        """世界书封面 -> 列表项 QIcon（无封面返回 None）。"""
        if not cover:
            return None
        p = os.path.join(paths.covers_dir(), cover)
        if not os.path.exists(p):
            return None
        pix = QPixmap(p)
        if pix.isNull():
            return None
        return QIcon(pix.scaled(28, 28, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def _pick_cover(self):
        if not self._current_wb:
            QMessageBox.warning(self, "提示", "请先选择世界书")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.gif)"
        )
        if not path:
            return
        ext = os.path.splitext(path)[1]
        filename = f"{uuid.uuid4().hex[:8]}{ext}"
        dest = os.path.join(paths.covers_dir(), filename)
        shutil.copy2(path, dest)
        # 删旧封面文件（与换头像一致，避免孤儿）
        self._remove_cover_file(self._current_wb.cover, exclude=filename)
        self._current_wb.cover = filename
        self._current_wb.touch()
        self.storage.save_world_book(self._current_wb)
        self._load_cover_preview()
        # 原地刷新当前列表项封面缩略图（不重载列表，避免丢正在编辑的条目表单）
        cur = self.wb_list.currentItem()
        if cur:
            icon = self._wb_icon(self._current_wb.cover)
            if icon is not None:
                cur.setIcon(icon)

    def _clear_cover(self):
        if not self._current_wb or not self._current_wb.cover:
            return
        if QMessageBox.question(self, "确认", "清除封面？") != QMessageBox.Yes:
            return
        self._remove_cover_file(self._current_wb.cover)
        self._current_wb.cover = ""
        self._current_wb.touch()
        self.storage.save_world_book(self._current_wb)
        self._load_cover_preview()
        cur = self.wb_list.currentItem()
        if cur:
            cur.setIcon(QIcon())

    def _remove_cover_file(self, cover: str, exclude: str = ""):
        """删除 covers_dir 下指定封面文件（exclude 时跳过该文件名）。"""
        if not cover or cover == exclude:
            return
        p = os.path.join(paths.covers_dir(), cover)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass