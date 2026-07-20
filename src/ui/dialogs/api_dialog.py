"""API 与预设设置对话框（标签页）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QComboBox, QFormLayout,
    QGroupBox, QSplitter, QMessageBox, QWidget, QTabWidget, QDoubleSpinBox,
    QSpinBox, QCheckBox, QInputDialog, QDialogButtonBox, QScrollArea, QFrame,
)

from src.models import ApiConfig, Preset, MemoryPreset, default_memory_preset
from src.models import SummaryPreset, default_summary_preset
from src.models import DanbooruPreset, default_danbooru_preset
from src.models.api_config import THINKING_LEVELS, THINKING_LABELS
from src.models.preset import (
    BLOCK_LABELS, BUILTIN_BLOCK_TYPES, BLOCK_CUSTOM,
    CUSTOM_BLOCK_ROLES, DEFAULT_CUSTOM_BLOCK_ROLE, CUSTOM_BLOCK_ROLE_LABELS,
    _default_context_blocks,
)


class ApiSettingsDialog(QDialog):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("API 与预设设置")
        # 上次迭代给记忆/总结/预设都加了单聊+群聊两版提示词，单页内容变多，
        # 原尺寸下多行编辑框会重叠。整体放大并抬高最小尺寸；其余子页改用
        # QScrollArea 兜底（见 _wrap_scroll），尺寸不够时滚动而非挤压重叠。
        self.resize(1100, 1100)
        self.setMinimumSize(900, 620)
        self._api_dirty = False  # 表单是否有未保存修改
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        tabs.addTab(self._build_api_tab(), "API 配置")
        tabs.addTab(self._build_preset_tab(), "预设")
        tabs.addTab(self._build_memory_tab(), "记忆整理")
        tabs.addTab(self._build_summary_tab(), "上文总结")
        tabs.addTab(self._build_danbooru_tab(), "Danbooru 加工")
        layout.addWidget(tabs)

    @staticmethod
    def _wrap_scroll(widget: QWidget) -> QScrollArea:
        """把一个 tab 内容包进 QScrollArea，内容超出时滚动而不挤压重叠。

        用于记忆/总结/Danbooru 等单页内容较多的 tab：上次迭代给这些页加了
        单聊+群聊两版提示词后，4~5 个 setMinimumHeight(120) 的多行编辑框
        叠起来超过 tab 可视高度，QVBoxLayout 会把后画的控件叠到前一个上面
        （尤其 QFormLayout 的行高不随子控件 minHeight 同步撑开时）。包一层
        滚动区后，QScrollArea 的 viewport 只给子控件「实际可用高度」，子控件
        按自身 sizeHint 排列，超出部分靠滚动条访问，不再互相覆盖。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    # =========================================================
    # ==================== API 标签页 ==========================
    # =========================================================
    def _build_api_tab(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        new_btn = QPushButton("+ 新建 API")
        new_btn.setObjectName("primaryBtn")
        new_btn.clicked.connect(self._new_api)
        ll.addWidget(new_btn)
        self.api_list = QListWidget()
        self.api_list.currentItemChanged.connect(self._select_api)
        ll.addWidget(self.api_list)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._delete_api)
        ll.addWidget(del_btn)
        splitter.addWidget(left)

        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_inner = QWidget()
        rl = QVBoxLayout(right_inner)
        form = QFormLayout()
        self.api_name = QLineEdit()
        form.addRow("名称:", self.api_name)
        self.api_base_url = QLineEdit()
        self.api_base_url.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL:", self.api_base_url)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-...")
        form.addRow("API Key:", self.api_key)
        # 模型字段：可编辑 QComboBox（保留手输能力）+ 「拉取模型」按钮（从 /v1/models 获取下拉列表）。
        # 用 QHBoxLayout 把下拉框和按钮放同一行，下拉框 stretch=1、按钮固定宽。
        model_row = QHBoxLayout()
        self.api_model = QComboBox()
        self.api_model.setEditable(True)
        # setPlaceholderText 在可编辑 QComboBox 上作用于内嵌 lineEdit（Qt 6 行为）。
        # placeholder 提示「点右侧 ▼」：因 Qt QSS 不画自定义箭头，靠文案引导用户点右侧深色按钮区展开。
        self.api_model.setPlaceholderText("gpt-4o / deepseek-chat 等（可手输，或点右侧 ▼ 选已拉取的）")
        model_row.addWidget(self.api_model, 1)
        self.fetch_models_btn = QPushButton("拉取模型")
        self.fetch_models_btn.setObjectName("primaryBtn")
        self.fetch_models_btn.setToolTip(
            "从接口 /v1/models 拉取可用模型列表填入下拉框。\n"
            "使用表单当前填写的 Base URL + API Key（无需先保存）。\n"
            "拉取到的列表与已填值合并去重，不会清空你手输的内容。"
        )
        self.fetch_models_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self.fetch_models_btn)
        form.addRow("模型:", model_row)
        self.api_preset = QComboBox()
        form.addRow("绑定预设:", self.api_preset)
        self.api_enabled = QComboBox()
        self.api_enabled.addItem("启用", True)
        self.api_enabled.addItem("禁用", False)
        form.addRow("状态:", self.api_enabled)
        rl.addLayout(form)

        # ---- 生成行为（适配主流模型：流式传输 + 思考级别）----
        gen_group = QGroupBox("生成行为（适配主流模型）")
        gen_form = QFormLayout(gen_group)
        self.api_streaming = QCheckBox("启用流式传输（逐字输出，多数模型推荐）")
        gen_form.addRow(self.api_streaming)
        self.api_thinking = QComboBox()
        for lvl in THINKING_LEVELS:
            self.api_thinking.addItem(THINKING_LABELS[lvl], lvl)
        self.api_thinking.setToolTip(
            "思考级别（reasoning_effort）：\n"
            "• 关闭：不发送该参数，适用于普通模型（gpt-4o / deepseek-chat）。\n"
            "• minimal/low/medium/high：随请求发送 reasoning_effort 字段，\n"
            "  适用于 o 系列 / DeepSeek-R1 等推理模型。\n"
            "• 硅基流动（base_url 含 siliconflow）的 GLM 系列改用 enable_thinking 开关：\n"
            "  关闭=enable_thinking=false（GLM 默认带思维链，需显式关闭才能真关闭），\n"
            "  其余级别=enable_thinking=true。"
        )
        thinking_row = QHBoxLayout()
        thinking_row.addWidget(self.api_thinking)
        thinking_row.addStretch()
        gen_form.addRow("思考级别:", thinking_row)
        rl.addWidget(gen_group)

        # 监听表单变化以标记脏数据
        for w in (self.api_name, self.api_base_url, self.api_key):
            w.textChanged.connect(self._mark_api_dirty)
        # api_model 现为可编辑 QComboBox：监听内嵌 lineEdit 的文本变化（手输/选择都触发脏标记）。
        self.api_model.lineEdit().textChanged.connect(self._mark_api_dirty)
        self.api_preset.currentIndexChanged.connect(self._mark_api_dirty)
        self.api_enabled.currentIndexChanged.connect(self._mark_api_dirty)
        self.api_streaming.toggled.connect(self._mark_api_dirty)
        self.api_thinking.currentIndexChanged.connect(self._mark_api_dirty)

        # 保存 + 测试 LLM 按钮行
        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_api)
        btn_row.addWidget(save_btn)
        self.test_llm_btn = QPushButton("测试 LLM 连接")
        self.test_llm_btn.setObjectName("primaryBtn")
        self.test_llm_btn.clicked.connect(self._on_test_llm)
        btn_row.addWidget(self.test_llm_btn)
        btn_row.addStretch()
        rl.addLayout(btn_row)

        # Embedding 设置
        emb_group = QGroupBox("Embedding 设置（可选，留空则复用上方配置）")
        emb_form = QFormLayout(emb_group)
        self.emb_model = QLineEdit()
        self.emb_model.setPlaceholderText("text-embedding-3-small")
        emb_form.addRow("Embedding模型:", self.emb_model)
        self.emb_url = QLineEdit()
        self.emb_url.setPlaceholderText("留空则用上方 Base URL")
        emb_form.addRow("Embedding URL:", self.emb_url)
        self.emb_key = QLineEdit()
        self.emb_key.setEchoMode(QLineEdit.Password)
        self.emb_key.setPlaceholderText("留空则用上方 API Key")
        emb_form.addRow("Embedding Key:", self.emb_key)
        # 测试 Embedding 按钮
        emb_btn_row = QHBoxLayout()
        emb_btn_row.addStretch()
        self.test_emb_btn = QPushButton("测试 Embedding 连接")
        self.test_emb_btn.setObjectName("primaryBtn")
        self.test_emb_btn.clicked.connect(self._on_test_embedding)
        emb_btn_row.addWidget(self.test_emb_btn)
        emb_form.addRow(emb_btn_row)
        rl.addWidget(emb_group)

        # Embedding 字段脏数据监听
        for w in (self.emb_model, self.emb_url, self.emb_key):
            w.textChanged.connect(self._mark_api_dirty)

        # 计费费率（人民币 元/百万 token，用于估算已消耗费用）
        price_group = QGroupBox("计费费率（人民币 元/百万 token，0=不计费）")
        price_form = QFormLayout(price_group)
        self.price_input = QDoubleSpinBox()
        self.price_input.setRange(0.0, 100000.0)
        self.price_input.setDecimals(4)
        self.price_input.setSingleStep(0.5)
        self.price_input.setSuffix(" 元/M")
        self.price_input.setToolTip("输入 token 单价（元/百万 token）")
        price_form.addRow("输入单价:", self.price_input)
        self.price_output = QDoubleSpinBox()
        self.price_output.setRange(0.0, 100000.0)
        self.price_output.setDecimals(4)
        self.price_output.setSingleStep(0.5)
        self.price_output.setSuffix(" 元/M")
        self.price_output.setToolTip("输出 token 单价（元/百万 token）")
        price_form.addRow("输出单价:", self.price_output)
        self.price_cache = QDoubleSpinBox()
        self.price_cache.setRange(0.0, 100000.0)
        self.price_cache.setDecimals(4)
        self.price_cache.setSingleStep(0.1)
        self.price_cache.setSuffix(" 元/M")
        self.price_cache.setToolTip("缓存命中 token 单价（通常更低甚至免费）")
        price_form.addRow("缓存命中单价:", self.price_cache)
        rl.addWidget(price_group)
        for w in (self.price_input, self.price_output, self.price_cache):
            w.valueChanged.connect(self._mark_api_dirty)

        rl.addStretch()
        right.setWidget(right_inner)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        self._current_api: ApiConfig | None = None
        self._test_worker: _ConnectionTestWorker | None = None
        # 拉取模型列表的后台 worker（与 _test_worker 同模式，回调里 deleteLater）。
        self._fetch_worker: _ModelListWorker | None = None
        # 拉取目标 API id：回调时若当前已切到别的 API，则只弹结果不填下拉，避免污染。
        self._fetch_target_id: str = ""
        self._load_api_list()
        return widget

    def _mark_api_dirty(self):
        self._api_dirty = True

    def _load_api_list(self):
        self.api_list.clear()
        self.api_preset.clear()
        for p in self.storage.load_all_presets():
            self.api_preset.addItem(p.name, p.id)
        for api in self.storage.load_all_apis():
            item = QListWidgetItem(api.name or "未命名")
            item.setData(Qt.UserRole, api.id)
            self.api_list.addItem(item)

    def _new_api(self):
        api = ApiConfig(name="新API", base_url="https://api.openai.com/v1")
        self.storage.save_api(api)
        self._load_api_list()
        for i in range(self.api_list.count()):
            if self.api_list.item(i).data(Qt.UserRole) == api.id:
                self.api_list.setCurrentRow(i)

    def _select_api(self, current, previous):
        if not current:
            self._current_api = None
            return
        api = self.storage.load_api(current.data(Qt.UserRole))
        if not api:
            return
        self._current_api = api
        self.api_name.setText(api.name)
        self.api_base_url.setText(api.base_url)
        self.api_key.setText(api.api_key)
        # api_model 为可编辑 QComboBox：先 clear 清掉上一个 API 的下拉列表残留，
        # 再 setCurrentText 恢复当前模型文本（clear 会连 lineEdit 文本一起清，故后设）。
        # blockSignals 避免 clear/setCurrentText 触发 _mark_api_dirty（_select_api 末尾会统一重置脏标志）。
        self.api_model.blockSignals(True)
        self.api_model.clear()
        self.api_model.setCurrentText(api.model)
        self.api_model.blockSignals(False)
        idx = self.api_preset.findData(api.preset_id)
        self.api_preset.setCurrentIndex(idx if idx >= 0 else 0)
        self.api_enabled.setCurrentIndex(0 if api.enabled else 1)
        self.api_streaming.setChecked(getattr(api, "streaming", True))
        eff = getattr(api, "reasoning_effort", "none")
        tidx = self.api_thinking.findData(eff)
        self.api_thinking.setCurrentIndex(tidx if tidx >= 0 else 0)
        self.emb_model.setText(api.embedding_model)
        self.emb_url.setText(api.embedding_base_url)
        self.emb_key.setText(api.embedding_api_key)
        self.price_input.setValue(getattr(api, "input_price", 0.0))
        self.price_output.setValue(getattr(api, "output_price", 0.0))
        self.price_cache.setValue(getattr(api, "cache_price", 0.0))
        # 表单填充会触发各字段的变更信号，故在填充完成后再重置脏标志，
        # 避免误判为「未保存」导致测试按钮被拦截。
        self._api_dirty = False

    def _collect_api_from_form(self) -> ApiConfig | None:
        if not self._current_api:
            return None
        a = self._current_api
        a.name = self.api_name.text().strip() or "未命名"
        a.base_url = self.api_base_url.text().strip()
        a.api_key = self.api_key.text().strip()
        # 可编辑 QComboBox 取值用 currentText（手输或下拉选中都覆盖）。
        a.model = self.api_model.currentText().strip()
        a.preset_id = self.api_preset.currentData() or ""
        a.enabled = self.api_enabled.currentData()
        a.streaming = self.api_streaming.isChecked()
        a.reasoning_effort = self.api_thinking.currentData()
        a.embedding_model = self.emb_model.text().strip()
        a.embedding_base_url = self.emb_url.text().strip()
        a.embedding_api_key = self.emb_key.text().strip()
        a.input_price = self.price_input.value()
        a.output_price = self.price_output.value()
        a.cache_price = self.price_cache.value()
        return a

    def _save_api(self):
        a = self._collect_api_from_form()
        if not a:
            return
        self.storage.save_api(a)
        self._api_dirty = False
        self._load_api_list()
        for i in range(self.api_list.count()):
            if self.api_list.item(i).data(Qt.UserRole) == a.id:
                self.api_list.setCurrentRow(i)
        QMessageBox.information(self, "已保存", f"API「{a.name}」已保存。")

    # -------- 记忆整理预设（summary 与 embedding_hybrid 模式共用）--------
    def _load_memory_preset(self):
        """从持久化加载记忆整理预设到表单（单例，不随 API 选择变化）。"""
        p = self.storage.load_memory_preset()
        if hasattr(self, "mem_api"):
            idx = self.mem_api.findData(p.api_id)
            self.mem_api.setCurrentIndex(idx if idx >= 0 else 0)
        self.mem_summary_prompt.setPlainText(p.summary_prompt)
        self.mem_summary_prompt_group.setPlainText(getattr(p, "summary_prompt_group", ""))
        self.mem_temp.setValue(p.temperature)
        self.mem_max_tokens.setValue(p.max_tokens)
        # Hybrid 字段（控件在 _build_memory_tab 已创建）
        if hasattr(self, "mem_hybrid_prompt"):
            self.mem_hybrid_prompt.setPlainText(getattr(p, "hybrid_system_prompt", ""))
            self.mem_hybrid_prompt_group.setPlainText(getattr(p, "hybrid_system_prompt_group", ""))
            w = getattr(p, "hybrid_recall_weights", (0.5, 0.2, 0.1, 0.2))
            self.mem_w_emb.setValue(w[0])
            self.mem_w_trig.setValue(w[1])
            self.mem_w_detail.setValue(w[2])
            self.mem_w_seq.setValue(w[3])
            self.mem_hybrid_topk.setValue(int(getattr(p, "hybrid_recall_top_k", 15)))
            self.mem_user_recall_w.setValue(float(getattr(p, "hybrid_user_recall_weight", 0.6)))
            self._update_mem_weight_sum()

    def _save_memory_preset(self):
        p = self.storage.load_memory_preset()
        p.api_id = self.mem_api.currentData() or ""
        p.summary_prompt = self.mem_summary_prompt.toPlainText()
        p.summary_prompt_group = self.mem_summary_prompt_group.toPlainText()
        p.temperature = self.mem_temp.value()
        p.max_tokens = self.mem_max_tokens.value()
        # Hybrid 字段
        if hasattr(self, "mem_hybrid_prompt"):
            p.hybrid_system_prompt = self.mem_hybrid_prompt.toPlainText()
            p.hybrid_system_prompt_group = self.mem_hybrid_prompt_group.toPlainText()
            p.hybrid_recall_weights = (
                self.mem_w_emb.value(), self.mem_w_trig.value(),
                self.mem_w_detail.value(), self.mem_w_seq.value(),
            )
            p.hybrid_recall_top_k = self.mem_hybrid_topk.value()
            p.hybrid_user_recall_weight = self.mem_user_recall_w.value()
        self.storage.save_memory_preset(p)
        QMessageBox.information(self, "已保存", "记忆整理预设已保存。")

    def _update_mem_weight_sum(self):
        """实时显示 Hybrid 三路融合权重和。"""
        if not hasattr(self, "mem_w_emb"):
            return
        s = (self.mem_w_emb.value() + self.mem_w_trig.value()
             + self.mem_w_detail.value() + self.mem_w_seq.value())
        mark = " (和=1.0)" if abs(s - 1.0) < 0.001 else " (非1.0，可点归一化)"
        self.mem_w_sum_label.setText(f"权重和 = {s:.2f}{mark}")

    def _norm_mem_weights(self):
        """等比缩放 Hybrid 权重到和=1.0；全 0 则重置默认。"""
        vals = [self.mem_w_emb.value(), self.mem_w_trig.value(),
                self.mem_w_detail.value(), self.mem_w_seq.value()]
        s = sum(vals)
        if s <= 0:
            self._reset_mem_weights()
            return
        self.mem_w_emb.setValue(vals[0] / s)
        self.mem_w_trig.setValue(vals[1] / s)
        self.mem_w_detail.setValue(vals[2] / s)
        self.mem_w_seq.setValue(vals[3] / s)

    def _reset_mem_weights(self):
        """恢复 Hybrid 权重默认 (0.5, 0.2, 0.1, 0.2)。"""
        self.mem_w_emb.setValue(0.5)
        self.mem_w_trig.setValue(0.2)
        self.mem_w_detail.setValue(0.1)
        self.mem_w_seq.setValue(0.2)

    def _reset_memory_preset(self):
        reply = QMessageBox.question(
            self, "确认", "恢复记忆整理预设为默认？三套提示词与 Hybrid 召回参数都将被覆盖。"
        )
        if reply != QMessageBox.Yes:
            return
        p = default_memory_preset()
        self.storage.save_memory_preset(p)
        self._load_memory_preset()

    def _delete_api(self):
        if not self._current_api:
            return
        reply = QMessageBox.question(self, "确认", f"删除 API「{self._current_api.name}」？")
        if reply == QMessageBox.Yes:
            self.storage.delete_api(self._current_api.id)
            self._current_api = None
            self._load_api_list()

    # -------- 测试功能 --------
    def _on_test_llm(self):
        self._start_test(kind="llm")

    def _on_test_embedding(self):
        self._start_test(kind="embedding")

    def _start_test(self, kind: str):
        """启动连接测试（先保存后测）。kind: 'llm' | 'embedding'。"""
        if not self._current_api:
            QMessageBox.information(self, "提示", "请先选择或新建一个 API。")
            return
        if self._api_dirty:
            QMessageBox.warning(
                self, "请先保存",
                "当前 API 配置有未保存的修改，请先点击「保存」后再测试。",
            )
            return

        # 重新从存储读取最新已保存配置，确保测试的是落盘数据
        api = self.storage.load_api(self._current_api.id)
        if not api:
            QMessageBox.warning(self, "错误", "无法读取该 API 配置，可能已被删除。")
            return

        btn = self.test_llm_btn if kind == "llm" else self.test_emb_btn
        orig_text = btn.text()
        btn.setEnabled(False)
        btn.setText("测试中...")

        self._test_worker = _ConnectionTestWorker(api, kind)
        self._test_worker.finished_signal.connect(
            lambda ok, msg, b=btn, t=orig_text: self._on_test_done(ok, msg, b, t)
        )
        self._test_worker.start()

    def _on_test_done(self, ok: bool, msg: str, btn: QPushButton, orig_text: str):
        btn.setEnabled(True)
        btn.setText(orig_text)
        title = "✅ 测试成功" if ok else "❌ 测试失败"
        if ok:
            QMessageBox.information(self, title, msg)
        else:
            QMessageBox.warning(self, title, msg)
        # 清理 worker
        if self._test_worker:
            self._test_worker.deleteLater()
            self._test_worker = None

    # -------- 拉取模型列表（用表单当前值，无需先保存）--------
    def _on_fetch_models(self):
        """从 /v1/models 拉取可用模型列表填入下拉框。

        与「测试连接」的关键差异：
        - 用表单当前值（_collect_api_from_form）构造临时 ApiConfig，不要求先保存；
          用户改了 base_url/api_key 立刻就能拉取，体验顺滑。
        - 不写库，拉取结果只填到当前下拉框（合并不覆盖手输值）。
        """
        if not self._current_api:
            QMessageBox.information(self, "提示", "请先选择或新建一个 API。")
            return
        # 用表单当前值构造临时配置（不写库）；_collect 失败说明无 _current_api，前面已挡。
        api = self._collect_api_from_form()
        if not api:
            return
        if not api.base_url or not api.api_key:
            QMessageBox.warning(
                self, "提示", "请先填写 Base URL 和 API Key，再拉取模型列表。"
            )
            return

        # 记录拉取目标 id：回调时若当前已切到别的 API，则只弹结果不填下拉，避免污染。
        self._fetch_target_id = api.id
        btn = self.fetch_models_btn
        orig_text = btn.text()
        btn.setEnabled(False)
        btn.setText("拉取中...")

        self._fetch_worker = _ModelListWorker(api)
        self._fetch_worker.finished_signal.connect(
            lambda ok, models, msg, b=btn, t=orig_text: self._on_fetch_done(ok, models, msg, b, t)
        )
        self._fetch_worker.start()

    def _on_fetch_done(
            self, ok: bool, models: list, msg: str,
            btn: QPushButton, orig_text: str,
    ):
        """拉取完成回调：恢复按钮、填下拉、弹结果。"""
        btn.setEnabled(True)
        btn.setText(orig_text)

        if not (ok and models):
            QMessageBox.warning(self, "拉取失败", msg or "未获取到模型")
            if self._fetch_worker:
                self._fetch_worker.deleteLater()
                self._fetch_worker = None
            return

        # 若拉取期间用户切到了别的 API，则不污染新 API 的下拉框，只弹结果。
        cur = self._current_api
        if cur and cur.id != self._fetch_target_id:
            QMessageBox.information(
                self, "拉取成功",
                f"获取到 {len(models)} 个模型。\n{msg}\n\n"
                "（当前已切换到其它 API，未填入其下拉框，切回原 API 可重新拉取。）",
            )
            if self._fetch_worker:
                self._fetch_worker.deleteLater()
                self._fetch_worker = None
            return

        # 合并去重填充：保留用户当前手输文本，把新模型追加到下拉（不覆盖已有项）。
        prev = self.api_model.currentText().strip()
        existing = {self.api_model.itemText(i) for i in range(self.api_model.count())}
        self.api_model.blockSignals(True)  # addItem/setCurrentText 不触发 _mark_api_dirty
        for m in models:
            if m and m not in existing:
                self.api_model.addItem(m)
                existing.add(m)
        # 恢复用户当前文本：若 prev 在列表里则选中它，否则作为纯文本保留（editable 行为）。
        if prev:
            self.api_model.setCurrentText(prev)
        self.api_model.blockSignals(False)

        QMessageBox.information(self, "拉取成功", f"获取到 {len(models)} 个模型。\n{msg}")
        if self._fetch_worker:
            self._fetch_worker.deleteLater()
            self._fetch_worker = None

    # =========================================================
    # ==================== 预设标签页 ==========================
    # =========================================================
    # =========================================================
    # ==================== 记忆整理标签页 ======================
    # =========================================================
    def _build_memory_tab(self):
        """记忆整理独立配置页：单独绑定 API + 可编辑提示词 + 生成参数。

        summary / embedding_hybrid 模式触发整理/总结时，优先使用这里绑定的 API；
        未绑定则回退角色绑定的 API。建议用一个便宜的小模型专门跑整理，省 token。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # API 选择
        api_group = QGroupBox("整理用 API（独立于正文 API）")
        api_form = QFormLayout(api_group)
        self.mem_api = QComboBox()
        self.mem_api.addItem("默认（回退角色绑定 API）", "")
        for api in self.storage.load_all_apis():
            self.mem_api.addItem(api.name, api.id)
        api_form.addRow("使用 API:", self.mem_api)
        api_hint = QLabel(
            "为整理单独选一个便宜的 API/模型，比正文用同款更省 token、效果更好。"
            "未选则回退角色绑定的 API。"
        )
        api_hint.setWordWrap(True)
        api_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        api_form.addRow(api_hint)
        layout.addWidget(api_group)

        # 提示词
        prompt_group = QGroupBox("记忆提示词（summary 与 hybrid 两模式各自一份，共用上方 API 与生成参数）")
        prompt_form = QFormLayout(prompt_group)
        # Summary 总结提示词（summary 模式：输出一段连贯文本，不按类型分组）
        self.mem_summary_prompt = QTextEdit()
        self.mem_summary_prompt.setMinimumHeight(120)
        self.mem_summary_prompt.setPlaceholderText(
            "Summary 模式总结提示词(单聊)：要求输出一段连贯记忆文本（或简洁条目，每行一条），"
            "不要按类型分组、不要加 [类型] 标记。"
        )
        prompt_form.addRow("Summary 提示(单聊):", self.mem_summary_prompt)

        self.mem_summary_prompt_group = QTextEdit()
        self.mem_summary_prompt_group.setMinimumHeight(120)
        self.mem_summary_prompt_group.setPlaceholderText(
            "Summary 模式总结提示词(群聊)：群聊中其他角色也在场，只记录该角色能感知的事。"
        )
        prompt_form.addRow("Summary 提示(群聊):", self.mem_summary_prompt_group)
        summary_hint = QLabel(
            "用于 summary 记忆模式：旧记忆 + 新对话 → 调用此提示词生成一段整合文本，"
            "覆盖存成 {cid}_summary.json。"
        )
        summary_hint.setWordWrap(True)
        summary_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        prompt_form.addRow(summary_hint)
        mem_param_row = QHBoxLayout()
        self.mem_temp = QDoubleSpinBox()
        self.mem_temp.setRange(0.0, 2.0)
        self.mem_temp.setSingleStep(0.1)
        self.mem_temp.setValue(0.3)
        mem_param_row.addWidget(QLabel("温度:"))
        mem_param_row.addWidget(self.mem_temp)
        self.mem_max_tokens = QSpinBox()
        self.mem_max_tokens.setRange(64, 8192)
        self.mem_max_tokens.setValue(1024)
        mem_param_row.addSpacing(12)
        mem_param_row.addWidget(QLabel("Max Tokens:"))
        mem_param_row.addWidget(self.mem_max_tokens)
        mem_param_row.addStretch()
        prompt_form.addRow(mem_param_row)
        layout.addWidget(prompt_group)

        # ---- Embedding Hybrid 模式专用：提示词 + 召回参数 ----
        hybrid_group = QGroupBox("Embedding 混合模式（emb+triggers+detail 三路召回，仅 embedding_hybrid 用）")
        hybrid_form = QFormLayout(hybrid_group)
        self.mem_hybrid_prompt = QTextEdit()
        self.mem_hybrid_prompt.setMinimumHeight(120)
        self.mem_hybrid_prompt.setPlaceholderText(
            "Hybrid 整理提示词(单聊)：输出每行一条 `[triggers: 词1,词2,词3] 明细`，"
            "triggers 为 3-4 个多字语义词（不含角色名、不含单字）。"
        )
        hybrid_form.addRow("Hybrid 提示(单聊):", self.mem_hybrid_prompt)

        self.mem_hybrid_prompt_group = QTextEdit()
        self.mem_hybrid_prompt_group.setMinimumHeight(120)
        self.mem_hybrid_prompt_group.setPlaceholderText(
            "Hybrid 整理提示词(群聊)：群聊中只记录该角色在场时能感知的事。"
        )
        hybrid_form.addRow("Hybrid 提示(群聊):", self.mem_hybrid_prompt_group)

        # 召回参数：4 权重 + top_k + user 召回权重
        # 权重控件复用 Danbooru 设置页的范式（_make_spin 风格：range 0-1, step 0.05, decimals 2）
        weight_box = QGroupBox("三路融合权重 (emb + triggers + detail + seq)")
        wl = QVBoxLayout(weight_box)

        def _make_w_spin() -> QDoubleSpinBox:
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1.0)
            sp.setSingleStep(0.05)
            sp.setDecimals(2)
            sp.setFixedWidth(110)
            return sp

        self.mem_w_emb = _make_w_spin()
        self.mem_w_trig = _make_w_spin()
        self.mem_w_detail = _make_w_spin()
        self.mem_w_seq = _make_w_spin()
        w_row = QHBoxLayout()
        for lbl, sp in [("embedding", self.mem_w_emb), ("triggers", self.mem_w_trig),
                        ("detail", self.mem_w_detail), ("seq", self.mem_w_seq)]:
            col = QVBoxLayout()
            col.addWidget(QLabel(lbl), alignment=Qt.AlignmentFlag.AlignCenter)
            col.addWidget(sp, alignment=Qt.AlignmentFlag.AlignCenter)
            w_row.addLayout(col)
        w_row.addStretch()
        wl.addLayout(w_row)
        # 权重和显示 + 归一化 + 恢复默认
        w_btn_row = QHBoxLayout()
        self.mem_w_sum_label = QLabel("权重和 = -")
        w_btn_row.addWidget(self.mem_w_sum_label)
        w_btn_row.addStretch()
        norm_btn = QPushButton("归一化")
        norm_btn.clicked.connect(self._norm_mem_weights)
        w_btn_row.addWidget(norm_btn)
        reset_w_btn = QPushButton("恢复默认权重")
        reset_w_btn.clicked.connect(self._reset_mem_weights)
        w_btn_row.addWidget(reset_w_btn)
        wl.addLayout(w_btn_row)
        for sp in (self.mem_w_emb, self.mem_w_trig, self.mem_w_detail, self.mem_w_seq):
            sp.valueChanged.connect(self._update_mem_weight_sum)
        hybrid_form.addRow(weight_box)

        # top_k + user 召回权重
        param_row = QHBoxLayout()
        self.mem_hybrid_topk = QSpinBox()
        self.mem_hybrid_topk.setRange(1, 100)
        self.mem_hybrid_topk.setValue(15)
        param_row.addWidget(QLabel("注入条数(top-k):"))
        param_row.addWidget(self.mem_hybrid_topk)
        self.mem_user_recall_w = QDoubleSpinBox()
        self.mem_user_recall_w.setRange(0.0, 1.0)
        self.mem_user_recall_w.setSingleStep(0.05)
        self.mem_user_recall_w.setDecimals(2)
        self.mem_user_recall_w.setValue(0.6)
        self.mem_user_recall_w.setFixedWidth(110)
        param_row.addSpacing(12)
        param_row.addWidget(QLabel("user召回权重:"))
        param_row.addWidget(self.mem_user_recall_w)
        param_row.addStretch()
        hybrid_form.addRow(param_row)
        hybrid_hint = QLabel(
            "Hybrid 模式两次召回合并：上一条 assistant + 本轮 user 各跑三路融合，"
            "按 user召回权重 加权合并取 top-k 注入。seq 权重让新记忆略优先。"
            "权重和不必为 1（可压低总分做对比），归一化按钮等比缩放到和=1。"
        )
        hybrid_hint.setWordWrap(True)
        hybrid_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        hybrid_form.addRow(hybrid_hint)
        layout.addWidget(hybrid_group)

        # 按钮
        btn_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_memory_preset)
        btn_row.addStretch()
        btn_row.addWidget(reset_btn)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_memory_preset)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        layout.addStretch()
        # 加载当前预设到表单
        self._load_memory_preset()
        return self._wrap_scroll(widget)

    # =========================================================
    # ==================== 上文总结标签页 ======================
    # =========================================================
    def _build_summary_tab(self):
        """上文总结独立配置页：单独绑定 API + 可编辑提示词 + 生成参数。
        与「记忆整理」页结构对称：建议便宜小模型跑总结，回退会话 director_api_id 或角色绑定 API。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # API 选择
        api_group = QGroupBox("总结用 API（独立于正文 API）")
        api_form = QFormLayout(api_group)
        self.sum_api = QComboBox()
        self.sum_api.addItem("默认（回退会话导演API或角色绑定API）", "")
        for api in self.storage.load_all_apis():
            self.sum_api.addItem(api.name, api.id)
        api_form.addRow("使用 API:", self.sum_api)
        api_hint = QLabel(
            "为上文总结单独选一个便宜的 API/模型，比正文用同款更省 token。"
            "未选则回退会话「导演API」或角色绑定 API。"
        )
        api_hint.setWordWrap(True)
        api_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        api_form.addRow(api_hint)
        layout.addWidget(api_group)

        # 提示词
        prompt_group = QGroupBox("总结提示词")
        prompt_form = QFormLayout(prompt_group)
        self.sum_system = QTextEdit()
        self.sum_system.setMinimumHeight(120)
        self.sum_system.setPlaceholderText("单聊上文总结用系统提示词")
        prompt_form.addRow("系统提示(单聊):", self.sum_system)

        self.sum_system_group = QTextEdit()
        self.sum_system_group.setMinimumHeight(120)
        self.sum_system_group.setPlaceholderText("群聊上文总结用系统提示词（保留发言者归属）")
        prompt_form.addRow("系统提示(群聊):", self.sum_system_group)
        sum_param_row = QHBoxLayout()
        self.sum_temp = QDoubleSpinBox()
        self.sum_temp.setRange(0.0, 2.0)
        self.sum_temp.setSingleStep(0.1)
        self.sum_temp.setValue(0.3)
        sum_param_row.addWidget(QLabel("温度:"))
        sum_param_row.addWidget(self.sum_temp)
        self.sum_max_tokens = QSpinBox()
        self.sum_max_tokens.setRange(64, 8192)
        self.sum_max_tokens.setValue(512)
        sum_param_row.addSpacing(12)
        sum_param_row.addWidget(QLabel("Max Tokens:"))
        sum_param_row.addWidget(self.sum_max_tokens)
        sum_param_row.addStretch()
        prompt_form.addRow(sum_param_row)
        format_hint = QLabel(
            "总结逐段产出一条 summary 消息：取最早 N 条活跃消息总结成 300 字以内第三人称摘要，"
            "原文随后被折叠。与「角色记忆」独立，不冲突。"
        )
        format_hint.setWordWrap(True)
        format_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        prompt_form.addRow(format_hint)
        layout.addWidget(prompt_group)

        # 按钮
        btn_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_summary_preset)
        btn_row.addStretch()
        btn_row.addWidget(reset_btn)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_summary_preset)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        layout.addStretch()
        # 加载当前预设到表单
        self._load_summary_preset()
        return self._wrap_scroll(widget)

    def _load_summary_preset(self):
        """从持久化加载上文总结预设到表单（单例）。"""
        p = self.storage.load_summary_preset()
        if hasattr(self, "sum_api"):
            idx = self.sum_api.findData(p.api_id)
            self.sum_api.setCurrentIndex(idx if idx >= 0 else 0)
        self.sum_system.setPlainText(p.system_prompt)
        self.sum_system_group.setPlainText(getattr(p, "system_prompt_group", ""))
        self.sum_temp.setValue(p.temperature)
        self.sum_max_tokens.setValue(p.max_tokens)

    def _save_summary_preset(self):
        p = self.storage.load_summary_preset()
        p.api_id = self.sum_api.currentData() or ""
        p.system_prompt = self.sum_system.toPlainText()
        p.system_prompt_group = self.sum_system_group.toPlainText()
        p.temperature = self.sum_temp.value()
        p.max_tokens = self.sum_max_tokens.value()
        self.storage.save_summary_preset(p)
        QMessageBox.information(self, "已保存", "上文总结预设已保存。")

    def _reset_summary_preset(self):
        reply = QMessageBox.question(
            self, "确认", "恢复上文总结预设为默认？当前提示词将被覆盖。"
        )
        if reply != QMessageBox.Yes:
            return
        p = default_summary_preset()
        self.storage.save_summary_preset(p)
        self._load_summary_preset()

    # =========================================================
    # ==================== Danbooru 加工标签页 ================
    # =========================================================
    def _build_danbooru_tab(self):
        """Danbooru tag 加工 LLM 配置页：单独绑定 API + 可编辑提示词 + 生成参数。

        与「记忆整理」页结构对称。这里只管 LLM 加工段；
        embedding 用「记忆整理」配置的 API（复用，不重复造），库与模式开关
        在「文生图 → Danbooru Tag 设置」对话框里。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # API 选择
        api_group = QGroupBox("加工用 API（独立于正文 API）")
        api_form = QFormLayout(api_group)
        self.dan_api = QComboBox()
        self.dan_api.addItem("默认（回退会话当前 API）", "")
        for api in self.storage.load_all_apis():
            self.dan_api.addItem(api.name, api.id)
        api_form.addRow("使用 API:", self.dan_api)
        api_hint = QLabel(
            "为 tag 加工单独选一个便宜的 API/模型，比正文用同款更省 token。"
            "未选则回退会话当前 API；仍无可用时回退首个启用的 API"
            "（头像生成等无会话上下文的场景靠此兜底）。\n"
            "⚠ embedding（建库/召回）复用「记忆整理」标签页配置的 API，请在那里"
            "绑定一个配置了 embedding 模型的 API。"
        )
        api_hint.setWordWrap(True)
        api_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        api_form.addRow(api_hint)
        layout.addWidget(api_group)

        # 提示词
        prompt_group = QGroupBox("加工提示词")
        prompt_form = QFormLayout(prompt_group)
        self.dan_system = QTextEdit()
        self.dan_system.setMinimumHeight(180)
        self.dan_system.setPlaceholderText("Danbooru tag 加工用系统提示词")
        prompt_form.addRow("系统提示:", self.dan_system)
        dan_param_row = QHBoxLayout()
        self.dan_temp = QDoubleSpinBox()
        self.dan_temp.setRange(0.0, 2.0)
        self.dan_temp.setSingleStep(0.1)
        self.dan_temp.setValue(0.3)
        dan_param_row.addWidget(QLabel("温度:"))
        dan_param_row.addWidget(self.dan_temp)
        self.dan_max_tokens = QSpinBox()
        self.dan_max_tokens.setRange(64, 8192)
        self.dan_max_tokens.setValue(300)
        dan_param_row.addSpacing(12)
        dan_param_row.addWidget(QLabel("Max Tokens:"))
        dan_param_row.addWidget(self.dan_max_tokens)
        dan_param_row.addStretch()
        prompt_form.addRow(dan_param_row)
        format_hint = QLabel(
            "提示词中可写 {{标签}} 占位：手改模式下注入用户勾选的 tag 列表，"
            "自动模式下注入 embedding 全量召回候选。LLM 输出逗号分隔英文 Danbooru tag 串。"
        )
        format_hint.setWordWrap(True)
        format_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        prompt_form.addRow(format_hint)
        layout.addWidget(prompt_group)

        # 按钮
        btn_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_danbooru_preset)
        btn_row.addStretch()
        btn_row.addWidget(reset_btn)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_danbooru_preset)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        layout.addStretch()
        # 加载当前预设到表单
        self._load_danbooru_preset()
        return self._wrap_scroll(widget)

    def _load_danbooru_preset(self):
        """从持久化加载 Danbooru 加工预设到表单（单例）。"""
        p = self.storage.load_danbooru_preset()
        if hasattr(self, "dan_api"):
            idx = self.dan_api.findData(p.api_id)
            self.dan_api.setCurrentIndex(idx if idx >= 0 else 0)
        self.dan_system.setPlainText(p.system_prompt)
        self.dan_temp.setValue(p.temperature)
        self.dan_max_tokens.setValue(p.max_tokens)

    def _save_danbooru_preset(self):
        # 只改 LLM 加工段字段，保留库与模式段字段不动
        p = self.storage.load_danbooru_preset()
        p.api_id = self.dan_api.currentData() or ""
        p.system_prompt = self.dan_system.toPlainText()
        p.temperature = self.dan_temp.value()
        p.max_tokens = self.dan_max_tokens.value()
        self.storage.save_danbooru_preset(p)
        QMessageBox.information(self, "已保存", "Danbooru 加工预设已保存。")

    def _reset_danbooru_preset(self):
        reply = QMessageBox.question(
            self, "确认",
            "恢复 Danbooru 加工预设为默认？当前提示词与参数将被覆盖"
            "（不影响库与模式设置，那些在「Danbooru Tag 设置」对话框里）。"
        )
        if reply != QMessageBox.Yes:
            return
        old = self.storage.load_danbooru_preset()
        p = default_danbooru_preset()
        # 保留库与模式段字段，只重置 LLM 加工段
        p.manual_mode = old.manual_mode
        p.allow_nsfw = old.allow_nsfw
        p.recall_top_n = old.recall_top_n
        p.negative_prompt = old.negative_prompt
        p.csv_path = old.csv_path
        p.last_csv_mtime = old.last_csv_mtime
        p.last_db_count = old.last_db_count
        self.storage.save_danbooru_preset(p)
        self._load_danbooru_preset()

    def _build_preset_tab(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        new_btn = QPushButton("+ 新建预设")
        new_btn.setObjectName("primaryBtn")
        new_btn.clicked.connect(self._new_preset)
        ll.addWidget(new_btn)
        self.preset_list = QListWidget()
        self.preset_list.currentItemChanged.connect(self._select_preset)
        ll.addWidget(self.preset_list)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._delete_preset)
        ll.addWidget(del_btn)
        splitter.addWidget(left)

        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_inner = QWidget()
        rl = QVBoxLayout(right_inner)
        form = QFormLayout()
        self.p_name = QLineEdit()
        form.addRow("名称:", self.p_name)

        self.p_system = QTextEdit()
        self.p_system.setFixedHeight(80)
        self.p_system.setPlaceholderText("单聊系统提示模板，支持 {{char}} {{user}}")
        form.addRow("系统提示(单聊):", self.p_system)

        self.p_system_group = QTextEdit()
        self.p_system_group.setFixedHeight(80)
        self.p_system_group.setPlaceholderText("群聊系统提示模板（发言用），支持 {{char}} {{user}}")
        form.addRow("系统提示(群聊):", self.p_system_group)

        self.p_char_info = QTextEdit()
        self.p_char_info.setFixedHeight(70)
        self.p_char_info.setPlaceholderText(
            "角色信息模板，支持 {{description}} {{personality}} {{scenario}} {{char}} {{user}}"
        )
        form.addRow("角色信息:", self.p_char_info)

        self.p_temp = QDoubleSpinBox()
        self.p_temp.setRange(0.0, 2.0)
        self.p_temp.setSingleStep(0.1)
        self.p_temp.setValue(0.8)
        form.addRow("Temperature:", self.p_temp)

        self.p_max_tokens = QSpinBox()
        self.p_max_tokens.setRange(64, 32768)
        self.p_max_tokens.setValue(1024)
        form.addRow("Max Tokens:", self.p_max_tokens)

        self.p_top_p = QDoubleSpinBox()
        self.p_top_p.setRange(0.0, 1.0)
        self.p_top_p.setSingleStep(0.05)
        self.p_top_p.setValue(0.95)
        form.addRow("Top P:", self.p_top_p)

        self.p_freq = QDoubleSpinBox()
        self.p_freq.setRange(-2.0, 2.0)
        self.p_freq.setSingleStep(0.1)
        form.addRow("Frequency Penalty:", self.p_freq)

        self.p_pres = QDoubleSpinBox()
        self.p_pres.setRange(-2.0, 2.0)
        self.p_pres.setSingleStep(0.1)
        form.addRow("Presence Penalty:", self.p_pres)

        self.p_director = QTextEdit()
        self.p_director.setFixedHeight(50)
        self.p_director.setPlaceholderText("群聊导演提示，{characters} 会被替换为角色列表")
        form.addRow("导演提示:", self.p_director)
        rl.addLayout(form)

        # ---- 上下文模块顺序（可拖拽）----
        rl.addWidget(self._build_context_blocks_group())

        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_preset)
        rl.addWidget(save_btn)
        rl.addStretch()
        right.setWidget(right_inner)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        self._current_preset: Preset | None = None
        self._load_preset_list()
        return widget

    def _build_context_blocks_group(self) -> QGroupBox:
        """构建上下文模块排序分组（支持拖拽）。"""
        group = QGroupBox("上下文模块顺序（拖拽排序，取消勾选可禁用）")
        v = QVBoxLayout(group)

        self.blocks_list = QListWidget()
        self.blocks_list.setDragDropMode(QListWidget.InternalMove)
        self.blocks_list.setDefaultDropAction(Qt.MoveAction)
        self.blocks_list.setDragEnabled(True)
        self.blocks_list.setAcceptDrops(True)
        self.blocks_list.setDragDropOverwriteMode(False)
        self.blocks_list.setAlternatingRowColors(True)
        self.blocks_list.setMinimumHeight(220)
        # 每项可勾选启用/禁用
        self.blocks_list.itemChanged.connect(self._on_block_item_changed)
        v.addWidget(self.blocks_list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ 新增自定义模块")
        add_btn.clicked.connect(self._add_custom_block)
        btn_row.addWidget(add_btn)
        edit_btn = QPushButton("编辑选中")
        edit_btn.clicked.connect(self._edit_selected_block)
        btn_row.addWidget(edit_btn)
        del_btn = QPushButton("删除选中")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._delete_selected_block)
        btn_row.addWidget(del_btn)
        reset_btn = QPushButton("⤴ 恢复默认顺序")
        reset_btn.clicked.connect(self._reset_blocks_default)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)
        return group

    # ---- 上下文模块操作 ----
    def _block_display_text(self, block: dict) -> str:
        btype = block.get("type")
        if btype == BLOCK_CUSTOM:
            name = block.get("label") or "自定义模块"
            # 追加角色标记，让用户在列表里一眼看出每块以什么 role 注入
            role = block.get("role", DEFAULT_CUSTOM_BLOCK_ROLE)
            tag = CUSTOM_BLOCK_ROLE_LABELS.get(role, "系统")
            return f"{name} [{tag}]"
        return BLOCK_LABELS.get(btype, btype)

    def _populate_blocks_list(self, blocks: list[dict]):
        """根据 context_blocks 数据填充列表（不触发 itemChanged 信号）。"""
        self.blocks_list.blockSignals(True)
        self.blocks_list.clear()
        for block in blocks:
            label = self._block_display_text(block)
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if block.get("enabled", True) else Qt.Unchecked)
            # 标记是否可删除（自定义块可删）
            item.setData(Qt.UserRole + 1, block.get("type") == BLOCK_CUSTOM)
            # 自定义块把 label/content 存进去，便于实时编辑
            item.setData(Qt.UserRole + 2, block)
            self.blocks_list.addItem(item)
        self.blocks_list.blockSignals(False)

    def _on_block_item_changed(self, item: QListWidgetItem):
        """勾选状态变化时同步到 item 内的 block 数据。"""
        block = item.data(Qt.UserRole + 2)
        if block is None:
            return
        block["enabled"] = item.checkState() == Qt.Checked

    def _current_blocks_from_list(self) -> list[dict]:
        """从列表控件读回当前顺序与状态的 blocks。"""
        blocks = []
        for i in range(self.blocks_list.count()):
            item = self.blocks_list.item(i)
            block = item.data(Qt.UserRole + 2)
            if block is None:
                continue
            block["enabled"] = item.checkState() == Qt.Checked
            # 注意：不从 item.text() 同步 label -- 列表显示文本含「[角色]」标记，
            # 而 label 只存纯名称（编辑对话框里改）。label/role/content 都直接随
            # block dict 流转，这里只同步拖拽顺序与勾选状态。
            blocks.append(dict(block))
        return blocks

    def _add_custom_block(self):
        """新增一个自定义文本模块（弹出编辑对话框）。"""
        label, ok = QInputDialog.getText(self, "自定义模块", "模块名称:", text="自定义模块")
        if not ok or not label.strip():
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑模块：{label}")
        dl = QVBoxLayout(dlg)
        dl.addWidget(QLabel("内容（支持 {{char}} {{user}} 等变量）："))
        te = QTextEdit()
        te.setMinimumHeight(120)
        dl.addWidget(te)
        # 消息角色：控制该块注入 messages 时以什么 role 发给 API
        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("消息角色:"))
        role_combo = QComboBox()
        for r in CUSTOM_BLOCK_ROLES:
            role_combo.addItem(CUSTOM_BLOCK_ROLE_LABELS[r], r)
        role_combo.setCurrentIndex(0)  # 默认 system
        role_combo.setToolTip(
            "该模块注入上下文时使用的消息角色：\n"
            "• 系统(system)：作为系统指令注入，最常用。\n"
            "• 用户(user)：作为用户发言注入。\n"
            "• AI(assistant)：作为 AI 发言注入，可用作 prefill/预设发言。"
        )
        role_row.addWidget(role_combo)
        role_row.addStretch()
        dl.addLayout(role_row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        content = te.toPlainText()
        block = {
            "type": BLOCK_CUSTOM,
            "enabled": True,
            "label": label.strip(),
            "content": content,
            "role": role_combo.currentData(),
        }
        # 追加到列表末尾
        self.blocks_list.blockSignals(True)
        item = QListWidgetItem(self._block_display_text(block))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        item.setData(Qt.UserRole + 1, True)  # 可删除
        item.setData(Qt.UserRole + 2, block)
        self.blocks_list.addItem(item)
        self.blocks_list.blockSignals(False)

    def _edit_selected_block(self):
        """编辑选中模块：内置块提示不可编辑内容；自定义块可改名+改内容。"""
        item = self.blocks_list.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择一个模块。")
            return
        block = item.data(Qt.UserRole + 2)
        if not block:
            return
        is_custom = block.get("type") == BLOCK_CUSTOM
        if not is_custom:
            QMessageBox.information(
                self, "提示",
                f"「{item.text()}」是内置模块，内容由系统自动生成，"
                "可拖拽调整顺序或取消勾选以禁用。",
            )
            return
        # 编辑自定义块
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑自定义模块")
        dl = QVBoxLayout(dlg)
        dl.addWidget(QLabel("模块名称:"))
        name_edit = QLineEdit(block.get("label", "自定义模块"))
        dl.addWidget(name_edit)
        dl.addWidget(QLabel("内容（支持 {{char}} {{user}} 等变量）:"))
        te = QTextEdit()
        te.setPlainText(block.get("content", ""))
        te.setMinimumHeight(120)
        dl.addWidget(te)
        # 消息角色：控制该块注入 messages 时以什么 role 发给 API
        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("消息角色:"))
        role_combo = QComboBox()
        for r in CUSTOM_BLOCK_ROLES:
            role_combo.addItem(CUSTOM_BLOCK_ROLE_LABELS[r], r)
        cur_role = block.get("role", DEFAULT_CUSTOM_BLOCK_ROLE)
        ridx = role_combo.findData(cur_role)
        role_combo.setCurrentIndex(ridx if ridx >= 0 else 0)
        role_combo.setToolTip(
            "该模块注入上下文时使用的消息角色：\n"
            "• 系统(system)：作为系统指令注入，最常用。\n"
            "• 用户(user)：作为用户发言注入。\n"
            "• AI(assistant)：作为 AI 发言注入，可用作 prefill/预设发言。"
        )
        role_row.addWidget(role_combo)
        role_row.addStretch()
        dl.addLayout(role_row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        block["label"] = name_edit.text().strip() or "自定义模块"
        block["content"] = te.toPlainText()
        block["role"] = role_combo.currentData()
        # 列表项文本含角色标记，编辑后同步刷新（_block_display_text 负责拼标记）
        item.setText(self._block_display_text(block))

    def _delete_selected_block(self):
        """删除选中模块（仅自定义块可删）。"""
        item = self.blocks_list.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择一个模块。")
            return
        is_deletable = item.data(Qt.UserRole + 1)
        if not is_deletable:
            QMessageBox.information(
                self, "提示",
                f"「{item.text()}」是内置模块，不可删除，可取消勾选以禁用。",
            )
            return
        reply = QMessageBox.question(self, "确认", f"删除模块「{item.text()}」？")
        if reply == QMessageBox.Yes:
            self.blocks_list.takeItem(self.blocks_list.row(item))

    def _reset_blocks_default(self):
        """恢复默认模块顺序。"""
        reply = QMessageBox.question(
            self, "确认", "恢复为默认上下文模块顺序？（自定义模块将保留并移到末尾）",
        )
        if reply != QMessageBox.Yes:
            return
        # 保留自定义块
        custom_blocks = [
            b for b in self._current_blocks_from_list()
            if b.get("type") == BLOCK_CUSTOM
        ]
        new_blocks = _default_context_blocks() + [
            {
                "type": BLOCK_CUSTOM,
                "enabled": b.get("enabled", True),
                "label": b.get("label", "自定义模块"),
                "content": b.get("content", ""),
                "role": b.get("role", DEFAULT_CUSTOM_BLOCK_ROLE),
            }
            for b in custom_blocks
        ]
        self._populate_blocks_list(new_blocks)

    def _load_preset_list(self):
        self.preset_list.clear()
        for p in self.storage.load_all_presets():
            item = QListWidgetItem(p.name or "未命名")
            item.setData(Qt.UserRole, p.id)
            self.preset_list.addItem(item)

    def _new_preset(self):
        p = Preset(name="新预设")
        self.storage.save_preset(p)
        self._load_preset_list()
        self._load_api_list()  # 刷新 API 绑定下拉
        for i in range(self.preset_list.count()):
            if self.preset_list.item(i).data(Qt.UserRole) == p.id:
                self.preset_list.setCurrentRow(i)

    def _select_preset(self, current, previous):
        if not current:
            self._current_preset = None
            return
        p = self.storage.load_preset(current.data(Qt.UserRole))
        if not p:
            return
        self._current_preset = p
        self.p_name.setText(p.name)
        self.p_system.setPlainText(p.system_prompt)
        self.p_system_group.setPlainText(getattr(p, "system_prompt_group", ""))
        self.p_char_info.setPlainText(getattr(p, "character_info_template", ""))
        self.p_temp.setValue(p.temperature)
        self.p_max_tokens.setValue(p.max_tokens)
        self.p_top_p.setValue(p.top_p)
        self.p_freq.setValue(p.frequency_penalty)
        self.p_pres.setValue(p.presence_penalty)
        self.p_director.setPlainText(p.director_prompt)
        self._populate_blocks_list(p.context_blocks)

    def _save_preset(self):
        if not self._current_preset:
            return
        p = self._current_preset
        p.name = self.p_name.text().strip() or "未命名"
        p.system_prompt = self.p_system.toPlainText()
        p.system_prompt_group = self.p_system_group.toPlainText()
        p.character_info_template = self.p_char_info.toPlainText()
        p.temperature = self.p_temp.value()
        p.max_tokens = self.p_max_tokens.value()
        p.top_p = self.p_top_p.value()
        p.frequency_penalty = self.p_freq.value()
        p.presence_penalty = self.p_pres.value()
        p.director_prompt = self.p_director.toPlainText()
        # 保存时规整化：强制 LAST_USER 置末、丢弃 INSTRUCTION（防止用户拖错或老数据残留）
        from src.models.preset import Preset
        p.context_blocks = Preset._normalize_blocks(self._current_blocks_from_list())
        self.storage.save_preset(p)
        self._load_preset_list()
        self._load_api_list()
        for i in range(self.preset_list.count()):
            if self.preset_list.item(i).data(Qt.UserRole) == p.id:
                self.preset_list.setCurrentRow(i)
        QMessageBox.information(self, "已保存", f"预设「{p.name}」已保存。")

    def _delete_preset(self):
        if not self._current_preset:
            return
        reply = QMessageBox.question(self, "确认", f"删除预设「{self._current_preset.name}」？")
        if reply == QMessageBox.Yes:
            self.storage.delete_preset(self._current_preset.id)
            self._current_preset = None
            self._load_preset_list()
            self._load_api_list()


class _ConnectionTestWorker(QThread):
    """后台执行连接测试的工作线程（避免阻塞 UI）。"""
    finished_signal = Signal(bool, str)

    def __init__(self, api_config: ApiConfig, kind: str, parent=None):
        super().__init__(parent)
        self.api_config = api_config
        self.kind = kind  # "llm" | "embedding"

    def run(self):
        try:
            if self.kind == "llm":
                from src.services.llm_client import test_connection
            else:
                from src.services.embedding_client import test_connection
            ok, msg = test_connection(self.api_config)
        except Exception as e:
            ok, msg = False, f"内部错误：{e}"
        self.finished_signal.emit(ok, msg)


class _ModelListWorker(QThread):
    """后台拉取模型列表的工作线程（避免阻塞 UI）。

    与 _ConnectionTestWorker 同模式：QThread + 类级 finished_signal，run 内惰性导入
    同步服务函数，异常包装后 emit。信号三元 (ok, models, msg)：ok=False 时 models=[]。
    """
    finished_signal = Signal(bool, list, str)

    def __init__(self, api_config: ApiConfig, parent=None):
        super().__init__(parent)
        self.api_config = api_config

    def run(self):
        try:
            from src.services.llm_client import list_models
            ok, models, msg = list_models(self.api_config)
        except Exception as e:
            ok, models, msg = False, [], f"内部错误：{e}"
        self.finished_signal.emit(ok, models, msg)
