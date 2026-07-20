"""Danbooru tag 勾选对话框（手改模式用）。

手改模式：emb 召回的候选 tag 列表展示给用户，用户勾选/新增 → 返回选中的
英文 name 列表 → 编排器把列表通过 {{标签}} 注入 LLM 预设做最终排序加工。

调用方（编排器在 worker 线程）通过 QDialog.exec() 在主线程阻塞执行。
返回值由调用方 main_window._on_manual_select_request 决定：
- 确定 -> dlg.selected() 返回勾选的英文 name 列表（可能含新增项，可能为空列表）
- 取消 -> 调用方直接置 None（编排器据此跳过此图；对话框自身 selected() 不在取消路径被调用）
⚠️ 空列表 []（用户点确定但未勾任何项）不是取消：编排器仍以此走 LLM 加工（候选空盲猜）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QGroupBox,
    QCheckBox, QInputDialog, QMessageBox,
)

from src.models import DANBOORU_CATEGORY_LIST, category_label


class DanbooruSelectDialog(QDialog):
    """候选 tag 勾选对话框。

    入参 candidates: list[TagCandidate]（来自 danbooru_service.recall_candidates）。
    返回：selected() -> list[str] 用户勾选+新增的英文 tag name 列表。
    """

    def __init__(self, candidates, description: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("挑选 Danbooru 标签")
        self.resize(640, 620)
        self.setMinimumSize(560, 480)
        self._candidates = list(candidates)
        self._result: list[str] = []
        self._build_ui(description)

    def _build_ui(self, description: str):
        layout = QVBoxLayout(self)

        # 原始描述
        if description:
            desc_group = QGroupBox("原始中文描述")
            dl = QVBoxLayout(desc_group)
            dl.addWidget(QLabel(description))
            layout.addWidget(desc_group)

        # 搜索框
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("过滤:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入关键字过滤候选（name 或 cn_name）")
        self.search_edit.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)

        # category 过滤复选框行（与搜索框叠加生效，默认全选，隐藏不删除保留选中）
        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("类别:"))
        self.cat_checks: dict[int, QCheckBox] = {}
        for cat_int, cat_label in DANBOORU_CATEGORY_LIST:
            cb = QCheckBox(cat_label)
            cb.setChecked(True)
            cb.stateChanged.connect(self._apply_filter)
            self.cat_checks[cat_int] = cb
            cat_row.addWidget(cb)
        cat_row.addStretch()
        layout.addLayout(cat_row)

        # 候选列表
        cand_group = QGroupBox("召回的候选 tag（勾选要用的）")
        cl = QVBoxLayout(cand_group)
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.MultiSelection)
        for c in self._candidates:
            display = (
                f"{c.name}  |  {c.cn_name}  |  pc={c.post_count}  |  "
                f"{category_label(c.category)}  |  score={c.score:.3f}  |  [{c.src}]"
            )
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, c.name)  # 存英文 name
            item.setData(Qt.UserRole + 1, display.lower())  # 文本过滤用小写串
            item.setData(Qt.UserRole + 2, c.category)  # category 过滤用 int
            self.list_widget.addItem(item)
        cl.addWidget(self.list_widget)

        # 新增 + 已选统计行
        action_row = QHBoxLayout()
        add_btn = QPushButton("+ 新增自定义标签")
        add_btn.clicked.connect(self._add_custom)
        action_row.addWidget(add_btn)
        action_row.addStretch()
        self.selected_label = QLabel("已选: 0")
        action_row.addWidget(self.selected_label)
        cl.addLayout(action_row)
        self.list_widget.itemChanged.connect(self._update_selected_count)

        layout.addWidget(cand_group)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消（跳过此图）")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _apply_filter(self, *_):
        """文本过滤 + category 过滤叠加：任一不满足则隐藏项（不删除，保留选中状态）。"""
        text = self.search_edit.text().strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            key = item.data(Qt.UserRole + 1) or ""
            cat = item.data(Qt.UserRole + 2)
            text_ok = (not text) or (text in key)
            # category 过滤：未知 category（cat is None，如自定义新增项）始终显示；
            # 已知 category 按对应复选框状态过滤。
            cat_ok = True
            if cat is not None:
                cb = self.cat_checks.get(cat)
                cat_ok = cb is not None and cb.isChecked()
            item.setHidden(not (text_ok and cat_ok))

    def _add_custom(self):
        name, ok = QInputDialog.getText(
            self, "新增标签", "输入英文 Danbooru tag（用下划线连接，如 blue_hair）:"
        )
        if ok and name.strip():
            name = name.strip()
            display = f"{name}  |  （自定义新增）"
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, name)
            item.setData(Qt.UserRole + 1, display.lower())
            # 自定义项不设 category（UserRole+2 留空），过滤时始终显示不受复选框影响
            item.setSelected(True)  # 新增项默认选中
            self.list_widget.addItem(item)
            self._update_selected_count()

    def _update_selected_count(self, *_):
        self.selected_label.setText(f"已选: {len(self.list_widget.selectedItems())}")

    def _on_ok(self):
        names = []
        seen = set()
        for item in self.list_widget.selectedItems():
            name = item.data(Qt.UserRole) or ""
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        self._result = names
        self.accept()

    def selected(self) -> list[str]:
        """返回用户勾选+新增的 tag name 列表。取消时返回空列表。"""
        return self._result
