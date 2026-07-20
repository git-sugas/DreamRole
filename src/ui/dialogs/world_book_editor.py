"""世界书编辑器对话框。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QComboBox, QFormLayout,
    QGroupBox, QSplitter, QMessageBox, QWidget, QSpinBox, QCheckBox,
    QListWidget, QScrollArea,
)

from src.models import WorldBook, WorldBookEntry


class WorldBookEditorDialog(QDialog):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("世界书管理")
        self.resize(900, 650)
        self.setMinimumSize(840, 500)
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
        for wb in self.storage.load_all_world_books():
            item = QListWidgetItem(wb.name or "未命名")
            item.setData(Qt.UserRole, wb.id)
            self.wb_list.addItem(item)

    def _new_wb(self):
        wb = WorldBook(name="新世界书")
        self.storage.save_world_book(wb)
        self._load_wb_list()
        for i in range(self.wb_list.count()):
            if self.wb_list.item(i).data(Qt.UserRole) == wb.id:
                self.wb_list.setCurrentRow(i)

    def _select_wb(self, current, previous):
        self.entry_list.clear()
        if not current:
            self._current_wb = None
            return
        wb = self.storage.load_world_book(current.data(Qt.UserRole))
        if not wb:
            return
        self._current_wb = wb
        self.wb_name.setText(wb.name)
        for entry in wb.entries:
            label = ", ".join(entry.keys) or "(无关键词)"
            if not entry.enabled:
                label += " [禁用]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry.id)
            self.entry_list.addItem(item)

    def _delete_wb(self):
        if not self._current_wb:
            return
        if QMessageBox.question(self, "确认", "删除此世界书？") == QMessageBox.Yes:
            self.storage.delete_world_book(self._current_wb.id)
            self._current_wb = None
            self._load_wb_list()
            self.entry_list.clear()

    def _new_entry(self):
        if not self._current_wb:
            QMessageBox.warning(self, "提示", "请先选择世界书")
            return
        entry = WorldBookEntry(keys=["新关键词"], content="新内容")
        self._current_wb.entries.append(entry)
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
        if not self._current_entry or not self._current_wb:
            return
        e = self._current_entry
        e.keys = [k.strip() for k in self.entry_keys.text().split(",") if k.strip()]
        e.content = self.entry_content.toPlainText()
        e.position = self.entry_position.currentData()
        e.insertion_order = self.entry_order.value()
        e.enabled = self.entry_enabled.isChecked()
        e.constant = self.entry_constant.isChecked()
        e.selective = self.entry_selective.isChecked()
        e.secondary_keys = [k.strip() for k in self.entry_secondary.text().split(",") if k.strip()]
        self._current_wb.name = self.wb_name.text().strip() or "未命名"
        self.storage.save_world_book(self._current_wb)
        self._select_wb(self.wb_list.currentItem(), None)
        QMessageBox.information(self, "已保存", f"世界书「{self._current_wb.name}」已保存。")

    def _delete_entry(self):
        if not self._current_entry or not self._current_wb:
            return
        self._current_wb.entries = [
            e for e in self._current_wb.entries if e.id != self._current_entry.id
        ]
        self.storage.save_world_book(self._current_wb)
        self._current_entry = None
        self._select_wb(self.wb_list.currentItem(), None)