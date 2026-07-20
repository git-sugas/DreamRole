"""角色记忆查看对话框。

两 tab 结构（与 mode 无关，磁盘上有数据就展示）：
  - Summary 记忆 tab：summary 模式记忆文本（{cid}_summary.json）
  - Hybrid 记忆 tab：embedding_hybrid 模式 SQLite 条目表格 + 两次召回合并测试区
角色当前 mode 决定的是「清空」按钮清哪一类，展示与 mode 无关 -- 切换模式前后
已落盘的各类记忆都看得到，避免因切模式而「看不见」旧数据。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QTextEdit, QGroupBox, QMessageBox, QFormLayout, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QLineEdit, QHeaderView,
    QAbstractItemView, QWidget,
)

from src.models import ApiConfig
from src.services.memory_service import MemoryService


class _HybridTestWorker(QThread):
    """hybrid 测试召回 worker：后台跑两次召回合并，信号回传结果。

    仿 Danbooru _TestWorker 范式：result_signal 发回明细列表，finished_signal
    发回完成态。大 try/except 兜底保证按钮一定能解锁。
    """
    result_signal = Signal(list)     # list[dict] 明细结果
    finished_signal = Signal(bool, str)  # (ok, msg)

    def __init__(self, memory_service: MemoryService, character, api_config,
                 assistant_query: str, user_query: str, parent=None):
        super().__init__(parent)
        self.memory = memory_service
        self.character = character
        self.api_config = api_config
        self.assistant_query = assistant_query
        self.user_query = user_query

    def run(self):
        try:
            result = self.memory.recall_hybrid_with_detail(
                self.character, self.api_config,
                self.assistant_query, self.user_query,
            )
            self.result_signal.emit(result)
            self.finished_signal.emit(True, f"召回完成，共 {len(result)} 条")
        except Exception as e:
            self.result_signal.emit([])
            self.finished_signal.emit(False, f"内部错误：{e}")


class MemoryDialog(QDialog):
    def __init__(self, storage, memory_service: MemoryService, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.memory = memory_service
        self.setWindowTitle("角色记忆")
        self.resize(860, 700)
        self.setMinimumSize(760, 560)
        self._test_worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 角色选择
        row = QHBoxLayout()
        row.addWidget(QLabel("选择角色:"))
        self.char_combo = QComboBox()
        for char in self.storage.load_all_characters():
            self.char_combo.addItem(char.name, char.id)
        self.char_combo.currentIndexChanged.connect(self._refresh)
        row.addWidget(self.char_combo)
        row.addStretch()
        layout.addLayout(row)

        # 记忆信息（当前模式 + 三类各自计数）
        self.info_group = QGroupBox("记忆信息")
        info_layout = QFormLayout(self.info_group)
        self.mode_label = QLabel("-")
        self.sum_count_label = QLabel("-")
        self.hyb_count_label = QLabel("-")
        info_layout.addRow("当前记忆模式:", self.mode_label)
        info_layout.addRow("Summary 记忆:", self.sum_count_label)
        info_layout.addRow("Hybrid 记忆:", self.hyb_count_label)
        layout.addWidget(self.info_group)

        # 三 tab 结构
        self.tabs = QTabWidget()
        # Tab 1: Summary 记忆
        sum_widget = QWidget()
        sl = QVBoxLayout(sum_widget)
        self.summary_edit = QTextEdit()
        self.summary_edit.setReadOnly(True)
        self.summary_edit.setPlaceholderText("选择角色后显示 summary 记忆文本；无则显示「（无）」")
        sl.addWidget(self.summary_edit)
        self.tabs.addTab(sum_widget, "Summary 记忆")

        # Tab 2: Hybrid 记忆（表格 + 测试区）
        self.tabs.addTab(self._build_hybrid_tab(), "Hybrid 记忆")
        layout.addWidget(self.tabs, 1)

        # 操作按钮
        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        clear_btn = QPushButton("清空当前模式记忆")
        clear_btn.setObjectName("dangerBtn")
        clear_btn.setToolTip("清空角色当前 memory_mode 对应的那一类记忆（不影响其他类已落盘数据）")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

    def _build_hybrid_tab(self) -> QWidget:
        """Hybrid tab：上区记忆表格 + 下区两次召回测试区。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        splitter = QSplitter(Qt.Vertical)

        # 上区：记忆表格
        table_box = QGroupBox("记忆条目（按 seq 排序，可点列头重排）")
        tl = QVBoxLayout(table_box)
        # 表格上方刷新按钮
        tbl_btn_row = QHBoxLayout()
        tbl_btn_row.addStretch()
        self.hyb_refresh_btn = QPushButton("刷新表格")
        self.hyb_refresh_btn.clicked.connect(self._refresh_hybrid_table)
        tbl_btn_row.addWidget(self.hyb_refresh_btn)
        tl.addLayout(tbl_btn_row)
        self.hybrid_table = QTableWidget(0, 4)
        self.hybrid_table.setHorizontalHeaderLabels(["seq", "triggers", "detail", "created_msg_index"])
        self.hybrid_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hybrid_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hybrid_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.hybrid_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.hybrid_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.hybrid_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.hybrid_table.verticalHeader().setVisible(False)
        self.hybrid_table.setSortingEnabled(True)
        tl.addWidget(self.hybrid_table)
        splitter.addWidget(table_box)

        # 下区：测试区
        test_box = QGroupBox("召回测试（两次召回合并，emb 路用记忆 API）")
        self._build_test_area(test_box)
        splitter.addWidget(test_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        return widget

    def _build_test_area(self, container):
        """构建两次召回合并测试区。"""
        tl = QVBoxLayout(container)
        # 输入行1：上一条 assistant
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("上一条 assistant:"))
        self.test_assistant_input = QLineEdit()
        self.test_assistant_input.setPlaceholderText("模拟上一条 AI 回复（首次对话可填角色开场白）")
        r1.addWidget(self.test_assistant_input, 1)
        tl.addLayout(r1)
        # 输入行2：本轮 user
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("本轮 user:"))
        self.test_user_input = QLineEdit()
        self.test_user_input.setPlaceholderText("模拟本轮用户输入")
        r2.addWidget(self.test_user_input, 1)
        tl.addLayout(r2)
        # 按钮行
        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("召回测试")
        self.test_btn.setObjectName("primaryBtn")
        self.test_btn.clicked.connect(self._run_hybrid_test)
        self.test_status = QLabel("-")
        self.test_status.setStyleSheet("color: #565f89; font-size: 11px;")
        btn_row.addWidget(self.test_btn)
        btn_row.addWidget(self.test_status)
        btn_row.addStretch()
        tl.addLayout(btn_row)
        # 结果表格
        self.test_result_table = QTableWidget(0, 9)
        self.test_result_table.setHorizontalHeaderLabels(
            ["seq", "triggers", "detail", "emb", "trig", "detail", "seq分", "合并分", "来源(a/u)"]
        )
        self.test_result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.test_result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hdr = self.test_result_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        for i in range(3, 9):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.test_result_table.verticalHeader().setVisible(False)
        tl.addWidget(self.test_result_table)

    # ============ 数据刷新 ============
    def _get_current_character(self):
        char_id = self.char_combo.currentData()
        if not char_id:
            return None
        return self.storage.load_character(char_id)

    def _refresh(self):
        char = self._get_current_character()
        if not char:
            return
        info = self.memory.get_memory_info(char)
        mode_map = {
            "none": "无", "summary": "AI总结",
            "embedding_hybrid": "Embedding混合(三路召回)",
        }
        self.mode_label.setText(mode_map.get(info.get("mode", "none"), info.get("mode", "")))

        # Tab 1: summary 记忆文本
        summary_text = self.memory.get_summary_text(char.id)
        sum_msg_count = self.memory.get_summary_msg_count(char.id)
        if summary_text:
            self.summary_edit.setPlainText(summary_text)
            self.sum_count_label.setText(f"{sum_msg_count} 条消息已总结，文本 {len(summary_text)} 字")
        else:
            self.summary_edit.setPlainText("（无 summary 记忆）")
            self.sum_count_label.setText("无")

        # Tab 2: hybrid 表格
        self._refresh_hybrid_table()
        hyb_count = self.memory.get_hybrid_entry_count(char.id)
        self.hyb_count_label.setText(f"{hyb_count} 条" if hyb_count > 0 else "无")

    def _refresh_hybrid_table(self):
        """刷新 Hybrid tab 上区记忆表格。"""
        char = self._get_current_character()
        if not char:
            return
        self.hybrid_table.setSortingEnabled(False)
        self.hybrid_table.setRowCount(0)
        entries = self.storage.fetch_all_char_memory_entries(char.id)
        self.hybrid_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            seq_item = QTableWidgetItem()
            seq_item.setData(Qt.DisplayRole, int(e["seq"]))
            idx_item = QTableWidgetItem()
            idx_item.setData(Qt.DisplayRole, int(e.get("created_msg_index", 0)))
            self.hybrid_table.setItem(i, 0, seq_item)
            self.hybrid_table.setItem(i, 1, QTableWidgetItem(str(e.get("triggers", ""))))
            det = str(e.get("detail", ""))
            det_item = QTableWidgetItem(det)
            det_item.setToolTip(det)   # 长文本 tooltip 看全文
            self.hybrid_table.setItem(i, 2, det_item)
            self.hybrid_table.setItem(i, 3, idx_item)
        self.hybrid_table.setSortingEnabled(True)
        self.hybrid_table.sortByColumn(0, Qt.AscendingOrder)

    # ============ 测试区 ============
    def _resolve_test_api(self, character):
        """解析测试用 API：MemoryPreset 绑定 API -> 角色绑定 API。无则 None。"""
        try:
            preset = self.storage.load_memory_preset()
            if preset.api_id:
                api = self.storage.load_api(preset.api_id)
                if api and api.enabled:
                    return api
        except Exception:
            pass
        # 回退角色绑定 API
        if character.api_id:
            try:
                api = self.storage.load_api(character.api_id)
                if api and api.enabled:
                    return api
            except Exception:
                pass
        return None

    def _run_hybrid_test(self):
        """启动 hybrid 测试召回 worker。"""
        char = self._get_current_character()
        if not char:
            return
        assistant_q = self.test_assistant_input.text().strip()
        user_q = self.test_user_input.text().strip()
        if not assistant_q and not user_q:
            QMessageBox.warning(self, "提示", "请至少输入一条查询文本（上一条 assistant 或本轮 user）。")
            return
        if self.memory.get_hybrid_entry_count(char.id) == 0:
            QMessageBox.information(self, "提示", "该角色无 Hybrid 记忆数据，先对话生成记忆后再测试。")
            return
        api = self._resolve_test_api(char)
        if api is None:
            self.test_status.setText("未找到可用 API，emb 路已短路，仅展示 trig+detail 两路")
        elif not getattr(api, "embedding_model", ""):
            self.test_status.setText("API 未配置 embedding_model，emb 路短路，仅展示 trig+detail 两路")
        else:
            self.test_status.setText("正在召回...")
        # 无 API 也要能测（emb 路短路），构造一个空 api_config 让 service 内部短路
        if api is None:
            api = ApiConfig(name="无", model="", embedding_model="")
        # 锁定按钮
        self.test_btn.setEnabled(False)
        self.test_result_table.setRowCount(0)
        self._test_worker = _HybridTestWorker(
            self.memory, char, api, assistant_q, user_q, self,
        )
        self._test_worker.result_signal.connect(self._on_test_result)
        self._test_worker.finished_signal.connect(self._on_test_finished)
        self._test_worker.start()

    def _on_test_result(self, result):
        """填充测试结果表格。"""
        self.test_result_table.setSortingEnabled(False)
        self.test_result_table.setRowCount(len(result))
        for i, item in enumerate(result):
            seq_item = QTableWidgetItem()
            seq_item.setData(Qt.DisplayRole, int(item["seq"]))
            self.test_result_table.setItem(i, 0, seq_item)
            self.test_result_table.setItem(i, 1, QTableWidgetItem(str(item.get("triggers", ""))))
            det = str(item.get("detail", ""))
            det_item = QTableWidgetItem(det[:50] + ("..." if len(det) > 50 else ""))
            det_item.setToolTip(det)
            self.test_result_table.setItem(i, 2, det_item)
            for col, key in [(3, "s_emb"), (4, "s_trig"), (5, "s_detail"),
                             (6, "s_seq"), (7, "merged_score")]:
                v_item = QTableWidgetItem()
                v_item.setData(Qt.DisplayRole, round(float(item.get(key, 0.0)), 4))
                self.test_result_table.setItem(i, col, v_item)
            src = f"a:{item.get('src_assistant', '') or '-'}\nu:{item.get('src_user', '') or '-'}"
            self.test_result_table.setItem(i, 8, QTableWidgetItem(src))
        self.test_result_table.setSortingEnabled(True)
        self.test_result_table.sortByColumn(7, Qt.DescendingOrder)   # 按合并分降序

    def _on_test_finished(self, ok, msg):
        """worker 完成：解锁按钮，更新状态。"""
        self.test_btn.setEnabled(True)
        # 若之前没设过短路提示，用 worker 的 msg 覆盖
        if not self.test_status.text().startswith("未找到") and not self.test_status.text().startswith("API 未配置"):
            self.test_status.setText(msg)

    def _clear(self):
        char = self._get_current_character()
        if not char:
            return
        mode_map = {
            "none": "无", "summary": "Summary 文本记忆",
            "embedding_hybrid": "Hybrid 三路召回记忆",
        }
        what = mode_map.get(char.memory_mode, char.memory_mode)
        if char.memory_mode == "none":
            QMessageBox.information(self, "提示", "该角色未启用记忆功能，无可清空的内容。")
            return
        if QMessageBox.question(self, "确认", f"清空「{char.name}」的 {what}？\n（其他类已落盘记忆不受影响）") == QMessageBox.Yes:
            self.memory.clear_memory(char)
            self._refresh()
