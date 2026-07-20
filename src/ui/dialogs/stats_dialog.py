"""统计信息对话框：按 API 展示 token 消耗与缓存命中。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QGroupBox,
)

from src.utils.helpers import format_tokens


class StatsDialog(QDialog):
    def __init__(self, storage, stats_service, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.stats_service = stats_service
        self.setWindowTitle("统计信息")
        self.resize(940, 460)
        self.setMinimumSize(880, 400)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel("以下为各 API 的累计统计（可单独重置）：")
        info.setStyleSheet("color: #565f89;")
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "API名称", "请求次数", "Prompt", "Completion", "缓存命中", "命中率",
            "节省Token", "已消耗费用(¥)", "操作",
        ])
        # 列宽：默认 Stretch 等分，但两列固定——
        #   第 0 列（API 名称）按内容自适应，避免表头/API 名被切；
        #   第 8 列（操作）按内容自适应贴合「重置」按钮宽，不经意拉太宽。
        # 其余 7 列在 Stretch 模式下于加宽后的窗体里均分，整体不再挤压中文表头。
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 160)
        header.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)

        self.total_label = QLabel("合计已消耗费用：—")
        self.total_label.setStyleSheet("color: #e0af68; font-size: 13px;")
        layout.addWidget(self.total_label)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        reset_all_btn = QPushButton("全部重置")
        reset_all_btn.setObjectName("dangerBtn")
        reset_all_btn.clicked.connect(self._reset_all)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        btn_row.addWidget(reset_all_btn)
        layout.addLayout(btn_row)

    def _refresh(self):
        apis = self.storage.load_all_apis()
        self.table.setRowCount(len(apis))
        for i, api in enumerate(apis):
            stats = self.stats_service.get_stats(api.id)
            rate = f"{stats.cache_hit_rate * 100:.1f}%" if stats.total_prompt_tokens > 0 else "—"
            cost = self.stats_service.compute_cost(stats, api)
            cost_text = f"¥{cost:.4f}" if cost > 0 else "—"
            self.table.setItem(i, 0, QTableWidgetItem(api.name or api.id[:8]))
            self.table.setItem(i, 1, QTableWidgetItem(str(stats.request_count)))
            self.table.setItem(i, 2, QTableWidgetItem(format_tokens(stats.total_prompt_tokens)))
            self.table.setItem(i, 3, QTableWidgetItem(format_tokens(stats.total_completion_tokens)))
            self.table.setItem(i, 4, QTableWidgetItem(format_tokens(stats.total_cached_tokens)))
            self.table.setItem(i, 5, QTableWidgetItem(rate))
            self.table.setItem(i, 6, QTableWidgetItem(format_tokens(stats.saved_tokens)))
            self.table.setItem(i, 7, QTableWidgetItem(cost_text))

            reset_btn = QPushButton("重置")
            reset_btn.clicked.connect(lambda checked, aid=api.id: self._reset_one(aid))
            self.table.setCellWidget(i, 8, reset_btn)

        # 底部合计费用
        total_cost = self.stats_service.get_total_cost()
        self.total_label.setText(
            f"合计已消耗费用：¥{total_cost:.4f}" if total_cost > 0
            else "合计已消耗费用：—（未设置费率或无消耗）"
        )

    def _reset_one(self, api_id):
        api = self.storage.load_api(api_id)
        name = api.name if api else api_id[:8]
        if QMessageBox.question(self, "确认", f"重置「{name}」的统计？") == QMessageBox.Yes:
            self.stats_service.reset(api_id)
            self._refresh()

    def _reset_all(self):
        if QMessageBox.question(self, "确认", "重置所有 API 的统计？") == QMessageBox.Yes:
            self.stats_service.reset_all()
            self._refresh()