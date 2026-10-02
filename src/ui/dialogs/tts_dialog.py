"""TTS 语音设置对话框。

三 tab 结构（仿 api_dialog QTabWidget）：
1. 音色管理：左 QListWidget 音色列表 + 右表单（reference_id/format/latency/speed +
   动态键值对参数 extra_params），仿 API tab 分栏 + comfyui LoRA 动态行模式。
2. 正则规则：左 QListWidget 规则列表 + 右表单（pattern/voice_id/priority/enabled），
   仿 render_rules_dialog。
3. 情绪 LLM：单例表单（tts_api_key/base_url/auto_tts/emotion_enabled/api_id/
   system_prompt/temperature/max_tokens/top_p），仿 summary tab。

菜单「设置 -> TTS 语音设置」打开。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox,
    QPushButton, QListWidget, QListWidgetItem, QSplitter, QGroupBox,
    QTextEdit, QScrollArea, QMessageBox,
)

from src.models import (
    TtsVoice, TtsRule,
    DEFAULT_TTS_EMOTION_SYSTEM_PROMPT,
)


class _TtsTestWorker(QThread):
    """后台测试 TTS 连接（避免阻塞 UI）。"""
    finished_signal = Signal(bool, str)

    def __init__(self, tts_service, voice, preset, parent=None):
        super().__init__(parent)
        self.tts_service = tts_service
        self.voice = voice
        self.preset = preset

    def run(self):
        try:
            ok, msg = self.tts_service.test_connection(self.voice, self.preset)
        except Exception as e:
            ok, msg = False, f"内部错误：{e}"
        self.finished_signal.emit(ok, msg)


class TtsSettingsDialog(QDialog):
    """TTS 语音设置对话框。"""

    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("TTS 语音设置")
        self.resize(1000, 820)
        self.setMinimumSize(860, 640)
        self._tts_test_worker = None
        self._build_ui()

    # ============ 整体骨架 ============
    def _build_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_voices_tab(), "音色管理")
        tabs.addTab(self._build_rules_tab(), "正则规则")
        tabs.addTab(self._build_preset_tab(), "情绪 LLM")
        layout.addWidget(tabs)

    @staticmethod
    def _wrap_scroll(widget: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    # ============ Tab 1: 音色管理 ============
    def _build_voices_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        splitter = QSplitter(Qt.Horizontal)

        # 左：音色列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.addWidget(QLabel("音色列表"))
        self.voice_list = QListWidget()
        self.voice_list.currentItemChanged.connect(self._on_voice_select)
        ll.addWidget(self.voice_list, 1)
        vbtns = QHBoxLayout()
        add_vbtn = QPushButton("+ 新建音色")
        add_vbtn.setObjectName("primaryBtn")
        add_vbtn.clicked.connect(self._on_add_voice)
        vbtns.addWidget(add_vbtn)
        del_vbtn = QPushButton("删除")
        del_vbtn.setObjectName("dangerBtn")
        del_vbtn.clicked.connect(self._on_del_voice)
        vbtns.addWidget(del_vbtn)
        ll.addLayout(vbtns)
        splitter.addWidget(left)

        # 右：音色表单
        right = QWidget()
        rl = QVBoxLayout(right)
        form = QFormLayout()
        self.v_name = QLineEdit()
        form.addRow("备注名:", self.v_name)
        self.v_reference_id = QLineEdit()
        self.v_reference_id.setPlaceholderText("Fish Audio voice model id（如 9a9cf477...）")
        form.addRow("Voice ID:", self.v_reference_id)
        self.v_format = QComboBox()
        for f in ("mp3", "wav", "pcm", "opus"):
            self.v_format.addItem(f, f)
        form.addRow("格式:", self.v_format)
        self.v_latency = QComboBox()
        self.v_latency.addItem("balanced", "balanced")
        self.v_latency.addItem("normal", "normal")
        form.addRow("延迟:", self.v_latency)
        speed_row = QHBoxLayout()
        self.v_speed = QDoubleSpinBox()
        self.v_speed.setRange(0.5, 2.0)
        self.v_speed.setSingleStep(0.1)
        self.v_speed.setValue(1.0)
        speed_row.addWidget(self.v_speed)
        speed_row.addStretch()
        form.addRow("语速:", speed_row)
        self.v_enabled = QCheckBox("启用")
        self.v_enabled.setChecked(True)
        form.addRow("", self.v_enabled)
        rl.addLayout(form)

        # 自定义参数区（动态键值对行，仿 comfyui LoRA）
        param_group = QGroupBox("自定义参数（兼容不同 TTS 的非通用字段）")
        pgl = QVBoxLayout(param_group)
        self._param_rows = []
        self._param_rows_container = QVBoxLayout()
        pgl.addLayout(self._param_rows_container)
        param_hint = QLabel(
            "键值对形式，嵌套键用点号分隔（如 prosody.volume=0、chunk_length=150）。\n"
            "数值会自动转 int/float，非数值按字符串透传到 TTS 请求 body。"
        )
        param_hint.setWordWrap(True)
        param_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        pgl.addWidget(param_hint)
        add_param_btn = QPushButton("+ 新增参数")
        add_param_btn.clicked.connect(self._on_add_param_row)
        pgl.addWidget(add_param_btn)
        self._add_param_btn = add_param_btn
        rl.addWidget(param_group)

        # 音色保存 + 测试按钮
        vbtn_row = QHBoxLayout()
        test_vbtn = QPushButton("测试连接")
        test_vbtn.clicked.connect(self._on_test_voice)
        vbtn_row.addWidget(test_vbtn)
        self._test_voice_label = QLabel("")
        self._test_voice_label.setStyleSheet("color: #9aa5ce; font-size: 11px;")
        vbtn_row.addWidget(self._test_voice_label, 1)
        save_vbtn = QPushButton("保存音色")
        save_vbtn.setObjectName("primaryBtn")
        save_vbtn.clicked.connect(self._on_save_voice)
        vbtn_row.addWidget(save_vbtn)
        rl.addLayout(vbtn_row)
        rl.addStretch()

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self._load_voice_list()
        return widget

    def _load_voice_list(self):
        self.voice_list.clear()
        voices = self.storage.load_all_tts_voices()
        for v in voices:
            tag = "" if v.enabled else "（禁用）"
            item = QListWidgetItem(f"{v.name}{tag}" if v.name else f"未命名{tag}")
            item.setData(Qt.UserRole, v.id)
            self.voice_list.addItem(item)
        if self.voice_list.count() > 0:
            self.voice_list.setCurrentRow(0)
        else:
            self._clear_voice_form()

    def _clear_voice_form(self):
        self.v_name.clear()
        self.v_reference_id.clear()
        self.v_format.setCurrentIndex(0)
        self.v_latency.setCurrentIndex(0)
        self.v_speed.setValue(1.0)
        self.v_enabled.setChecked(True)
        self._clear_param_rows()

    def _on_voice_select(self, cur, prev):
        if cur is None:
            self._clear_voice_form()
            return
        vid = cur.data(Qt.UserRole)
        v = self.storage.load_tts_voice(vid)
        if not v:
            self._clear_voice_form()
            return
        self.v_name.setText(v.name)
        self.v_reference_id.setText(v.reference_id)
        idx = self.v_format.findData(v.format)
        self.v_format.setCurrentIndex(idx if idx >= 0 else 0)
        idx = self.v_latency.findData(v.latency)
        self.v_latency.setCurrentIndex(idx if idx >= 0 else 0)
        self.v_speed.setValue(v.speed)
        self.v_enabled.setChecked(v.enabled)
        self._clear_param_rows()
        for p in v.extra_params:
            self._add_param_row(p.get("key", ""), p.get("value", ""))

    def _on_add_voice(self):
        from src.models import default_tts_voice
        v = default_tts_voice()
        v.name = "新音色"
        v.touch()
        self.storage.save_tts_voice(v)
        self._load_voice_list()
        # 选中新加的
        for i in range(self.voice_list.count()):
            if self.voice_list.item(i).data(Qt.UserRole) == v.id:
                self.voice_list.setCurrentRow(i)
                break

    def _on_del_voice(self):
        cur = self.voice_list.currentItem()
        if not cur:
            return
        vid = cur.data(Qt.UserRole)
        reply = QMessageBox.question(self, "确认删除", "确定删除此音色？")
        if reply != QMessageBox.Yes:
            return
        self.storage.delete_tts_voice(vid)
        self._load_voice_list()

    def _on_save_voice(self):
        cur = self.voice_list.currentItem()
        if not cur:
            QMessageBox.information(self, "提示", "请先选择或新建一个音色。")
            return
        vid = cur.data(Qt.UserRole)
        v = self.storage.load_tts_voice(vid)
        if not v:
            v = TtsVoice(id=vid)
        v.name = self.v_name.text().strip()
        v.reference_id = self.v_reference_id.text().strip()
        v.format = self.v_format.currentData()
        v.latency = self.v_latency.currentData()
        v.speed = self.v_speed.value()
        v.enabled = self.v_enabled.isChecked()
        v.extra_params = self._collect_param_rows()
        v.touch()
        self.storage.save_tts_voice(v)
        self._load_voice_list()
        QMessageBox.information(self, "已保存", f"音色「{v.name}」已保存。")

    # ---- 自定义参数动态行（仿 comfyui LoRA）----
    def _add_param_row(self, key="", value=""):
        row_layout = QHBoxLayout()
        idx = len(self._param_rows) + 1
        idx_label = QLabel(f"{idx}.")
        idx_label.setFixedWidth(20)
        key_edit = QLineEdit()
        key_edit.setPlaceholderText("参数名（如 prosody.volume）")
        key_edit.setText(key)
        val_edit = QLineEdit()
        val_edit.setPlaceholderText("值")
        val_edit.setText(value)
        del_btn = QPushButton("×")
        del_btn.setFixedWidth(28)
        row_layout.addWidget(idx_label)
        row_layout.addWidget(key_edit, 1)
        row_layout.addWidget(QLabel("="))
        row_layout.addWidget(val_edit, 1)
        row_layout.addWidget(del_btn)
        row_info = {"key_edit": key_edit, "val_edit": val_edit, "del_btn": del_btn,
                    "row_layout": row_layout, "idx_label": idx_label}
        del_btn.clicked.connect(lambda _, r=row_info: self._on_del_param_row(r))
        self._param_rows.append(row_info)
        self._param_rows_container.addLayout(row_layout)
        self._refresh_param_indices()

    def _on_add_param_row(self):
        self._add_param_row()

    def _on_del_param_row(self, row_info):
        rl = row_info["row_layout"]
        while rl.count():
            item = rl.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._param_rows_container.removeItem(rl)
        self._param_rows.remove(row_info)
        self._refresh_param_indices()

    def _refresh_param_indices(self):
        for i, ri in enumerate(self._param_rows):
            ri["idx_label"].setText(f"{i + 1}.")

    def _clear_param_rows(self):
        for ri in list(self._param_rows):
            self._on_del_param_row(ri)

    def _collect_param_rows(self):
        result = []
        for ri in self._param_rows:
            k = ri["key_edit"].text().strip()
            v = ri["val_edit"].text()
            if k:
                result.append({"key": k, "value": v})
        return result

    def _on_test_voice(self):
        cur = self.voice_list.currentItem()
        if not cur:
            QMessageBox.information(self, "提示", "请先选择一个音色。")
            return
        vid = cur.data(Qt.UserRole)
        v = self.storage.load_tts_voice(vid)
        if not v:
            return
        preset = self.storage.load_tts_preset()
        if not preset.tts_api_key:
            QMessageBox.warning(self, "提示", "请先在「情绪 LLM」tab 配置 TTS API Key。")
            return
        self._test_voice_label.setText("测试中...")
        from src.services.tts_service import TtsService
        svc = TtsService(self.storage)
        self._tts_test_worker = _TtsTestWorker(svc, v, preset, self)
        self._tts_test_worker.finished_signal.connect(self._on_test_voice_done)
        self._tts_test_worker.finished.connect(self._tts_test_worker.deleteLater)
        self._tts_test_worker.start()

    def _on_test_voice_done(self, ok, msg):
        self._test_voice_label.setText(msg)
        self._test_voice_label.setStyleSheet(
            "color: #9ece6a; font-size: 11px;" if ok else "color: #f7768e; font-size: 11px;"
        )
        self._tts_test_worker = None

    # ============ Tab 2: 正则规则 ============
    def _build_rules_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.addWidget(QLabel("规则列表（数字小先匹配）"))
        self.rule_list = QListWidget()
        self.rule_list.currentItemChanged.connect(self._on_rule_select)
        ll.addWidget(self.rule_list, 1)
        rbtns = QHBoxLayout()
        add_rbtn = QPushButton("+ 新建规则")
        add_rbtn.setObjectName("primaryBtn")
        add_rbtn.clicked.connect(self._on_add_rule)
        rbtns.addWidget(add_rbtn)
        del_rbtn = QPushButton("删除")
        del_rbtn.setObjectName("dangerBtn")
        del_rbtn.clicked.connect(self._on_del_rule)
        rbtns.addWidget(del_rbtn)
        ll.addLayout(rbtns)
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        form = QFormLayout()
        self.r_name = QLineEdit()
        form.addRow("规则名:", self.r_name)
        self.r_pattern = QLineEdit()
        self.r_pattern.setPlaceholderText(r"正则，如 「[^」]*」 匹配中文引号对话")
        form.addRow("正则:", self.r_pattern)
        self.r_voice = QComboBox()
        self.r_voice.addItem("（请选择音色）", "")
        form.addRow("配音音色:", self.r_voice)
        prio_row = QHBoxLayout()
        self.r_priority = QSpinBox()
        self.r_priority.setRange(0, 9999)
        self.r_priority.setValue(100)
        prio_row.addWidget(self.r_priority)
        prio_row.addStretch()
        form.addRow("优先级:", prio_row)
        self.r_enabled = QCheckBox("启用")
        self.r_enabled.setChecked(True)
        form.addRow("", self.r_enabled)
        rl.addLayout(form)

        hint = QLabel(
            "正则匹配命中的文本段会用对应音色配音，未匹配的段不配音。\n"
            "示例：心声 （[^）]*） | 对话台词 「[^」]*」 | 旁白 \\*[^*]*\\*"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        rl.addWidget(hint)
        rl.addStretch()

        # 保存规则按钮
        sbtn_row = QHBoxLayout()
        sbtn_row.addStretch()
        save_rbtn = QPushButton("保存规则")
        save_rbtn.setObjectName("primaryBtn")
        save_rbtn.clicked.connect(self._on_save_rule)
        sbtn_row.addWidget(save_rbtn)
        rl.addLayout(sbtn_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        # 底部保存全部规则
        bottom = QHBoxLayout()
        bottom.addStretch()
        save_all_rbtn = QPushButton("保存全部规则")
        save_all_rbtn.setObjectName("primaryBtn")
        save_all_rbtn.clicked.connect(self._on_save_all_rules)
        bottom.addWidget(save_all_rbtn)
        layout.addLayout(bottom)

        self._load_rule_list()
        return widget

    def _refresh_voice_combo(self):
        """刷新音色下拉（规则 tab 用）。"""
        cur_id = self.r_voice.currentData() if hasattr(self, "r_voice") else ""
        self.r_voice.clear()
        self.r_voice.addItem("（请选择音色）", "")
        for v in self.storage.load_all_tts_voices():
            if v.enabled:
                name = v.name if v.name else "未命名"
                self.r_voice.addItem(name, v.id)
        if cur_id:
            idx = self.r_voice.findData(cur_id)
            if idx >= 0:
                self.r_voice.setCurrentIndex(idx)

    def _load_rule_list(self):
        self.rule_list.clear()
        cfg = self.storage.load_tts_rules()
        self._refresh_voice_combo()
        for r in cfg.rules:
            tag = "" if r.enabled else "（禁用）"
            item = QListWidgetItem(f"{r.priority:>4}  {r.name}{tag}")
            item.setData(Qt.UserRole, r.id)
            self.rule_list.addItem(item)
        if self.rule_list.count() > 0:
            self.rule_list.setCurrentRow(0)
        else:
            self._clear_rule_form()

    def _clear_rule_form(self):
        self.r_name.clear()
        self.r_pattern.clear()
        self.r_voice.setCurrentIndex(0)
        self.r_priority.setValue(100)
        self.r_enabled.setChecked(True)

    def _on_rule_select(self, cur, prev):
        if cur is None:
            self._clear_rule_form()
            return
        rid = cur.data(Qt.UserRole)
        cfg = self.storage.load_tts_rules()
        r = next((x for x in cfg.rules if x.id == rid), None)
        if not r:
            self._clear_rule_form()
            return
        self.r_name.setText(r.name)
        self.r_pattern.setText(r.pattern)
        self._refresh_voice_combo()
        idx = self.r_voice.findData(r.voice_id)
        self.r_voice.setCurrentIndex(idx if idx >= 0 else 0)
        self.r_priority.setValue(r.priority)
        self.r_enabled.setChecked(r.enabled)

    def _on_add_rule(self):
        r = TtsRule(name="新规则", priority=100)
        cfg = self.storage.load_tts_rules()
        cfg.rules.append(r)
        self.storage.save_tts_rules(cfg)
        self._load_rule_list()
        for i in range(self.rule_list.count()):
            if self.rule_list.item(i).data(Qt.UserRole) == r.id:
                self.rule_list.setCurrentRow(i)
                break

    def _on_del_rule(self):
        cur = self.rule_list.currentItem()
        if not cur:
            return
        rid = cur.data(Qt.UserRole)
        reply = QMessageBox.question(self, "确认删除", "确定删除此规则？")
        if reply != QMessageBox.Yes:
            return
        cfg = self.storage.load_tts_rules()
        cfg.rules = [r for r in cfg.rules if r.id != rid]
        self.storage.save_tts_rules(cfg)
        self._load_rule_list()

    def _on_save_rule(self):
        cur = self.rule_list.currentItem()
        if not cur:
            QMessageBox.information(self, "提示", "请先选择或新建一个规则。")
            return
        rid = cur.data(Qt.UserRole)
        cfg = self.storage.load_tts_rules()
        r = next((x for x in cfg.rules if x.id == rid), None)
        if not r:
            r = TtsRule(id=rid)
            cfg.rules.append(r)
        r.name = self.r_name.text().strip()
        r.pattern = self.r_pattern.text().strip()
        r.voice_id = self.r_voice.currentData() or ""
        r.priority = self.r_priority.value()
        r.enabled = self.r_enabled.isChecked()
        self.storage.save_tts_rules(cfg)
        self._load_rule_list()
        QMessageBox.information(self, "已保存", f"规则「{r.name}」已保存。")

    def _on_save_all_rules(self):
        # 当前编辑的规则先保存（与 _on_save_rule 逻辑一致）
        cur = self.rule_list.currentItem()
        if cur:
            rid = cur.data(Qt.UserRole)
            cfg = self.storage.load_tts_rules()
            r = next((x for x in cfg.rules if x.id == rid), None)
            if r:
                r.name = self.r_name.text().strip()
                r.pattern = self.r_pattern.text().strip()
                r.voice_id = self.r_voice.currentData() or ""
                r.priority = self.r_priority.value()
                r.enabled = self.r_enabled.isChecked()
                self.storage.save_tts_rules(cfg)
        QMessageBox.information(self, "已保存", "TTS 正则规则已保存。")

    # ============ Tab 3: 情绪 LLM ============
    def _build_preset_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # TTS 基础设置
        tts_group = QGroupBox("TTS 连接设置（所有音色共用）")
        tts_form = QFormLayout(tts_group)
        self.tts_api_key = QLineEdit()
        self.tts_api_key.setEchoMode(QLineEdit.Password)
        self.tts_api_key.setPlaceholderText("Fish Audio API Key")
        tts_form.addRow("API Key:", self.tts_api_key)
        self.tts_base_url = QLineEdit()
        self.tts_base_url.setText("https://api.fish.audio")
        self.tts_base_url.setPlaceholderText("https://api.fish.audio")
        tts_form.addRow("Base URL:", self.tts_base_url)
        self.tts_model = QComboBox()
        self.tts_model.setEditable(True)
        for m in ("s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"):
            self.tts_model.addItem(m, m)
        self.tts_model.setCurrentText("s2.1-pro-free")
        tts_form.addRow("模型:", self.tts_model)
        self.tts_proxy = QLineEdit()
        self.tts_proxy.setPlaceholderText("http://127.0.0.1:7890（留空=不走代理直连）")
        tts_form.addRow("代理:", self.tts_proxy)
        self.auto_tts_chk = QCheckBox("自动配音（每条 LLM 回复渲染完成后自动生成 TTS 音频）")
        tts_form.addRow("", self.auto_tts_chk)
        self.auto_play_chk = QCheckBox("自动播放（生成 TTS 后自动播放；关闭则仅生成音频，点击气泡句子播放）")
        self.auto_play_chk.setChecked(True)
        tts_form.addRow("", self.auto_play_chk)
        tts_hint = QLabel(
            "API Key 全局共用（不存音色里）。自动配音开启后，每条 assistant 消息渲染完成"
            "会按正则规则切分文本、选音色、合成音频。自动播放开启则合成后立即播放全部，"
            "关闭则仅生成音频落到气泡，每段配音前显示 ▶ 可点击播放该段。"
        )
        tts_hint.setWordWrap(True)
        tts_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        tts_form.addRow(tts_hint)
        layout.addWidget(tts_group)

        # 情绪 LLM 设置
        emo_group = QGroupBox("情绪控制 LLM")
        emo_form = QFormLayout(emo_group)
        self.emotion_chk = QCheckBox("启用情绪 LLM 加工（开启后对配音文本调 LLM 插入情绪标记再合成）")
        emo_form.addRow("", self.emotion_chk)
        self.emo_api = QComboBox()
        self.emo_api.addItem("默认（回退会话 API / 首个启用 API）", "")
        for api in self.storage.load_all_apis():
            self.emo_api.addItem(api.name, api.id)
        emo_form.addRow("使用 API:", self.emo_api)
        emo_hint = QLabel(
            "情绪 LLM 是独立调用（不影响正文 LLM）。开启后对正则命中的每段文本调一次 LLM，"
            "会把当前气泡完整内容作为上下文传给 LLM，让它判断该片段的情绪，在文本中插入 "
            "Fish Audio S2 方括号情绪标记（如 [happy] [sad][whispering]），再用带标记的文本调 TTS。"
            "关闭则直接用原文调 TTS。系统提示词里含完整的 Fish Audio 情绪标签清单供 LLM 参考。"
        )
        emo_hint.setWordWrap(True)
        emo_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        emo_form.addRow(emo_hint)

        self.emo_system = QTextEdit()
        self.emo_system.setMinimumHeight(160)
        self.emo_system.setPlaceholderText("情绪标注系统提示词")
        emo_form.addRow("系统提示:", self.emo_system)

        param_row = QHBoxLayout()
        self.emo_temp = QDoubleSpinBox()
        self.emo_temp.setRange(0.0, 2.0)
        self.emo_temp.setSingleStep(0.1)
        self.emo_temp.setValue(0.7)
        param_row.addWidget(QLabel("温度:"))
        param_row.addWidget(self.emo_temp)
        self.emo_max_tokens = QSpinBox()
        self.emo_max_tokens.setRange(64, 100000)
        self.emo_max_tokens.setValue(1024)
        param_row.addSpacing(12)
        param_row.addWidget(QLabel("Max Tokens:"))
        param_row.addWidget(self.emo_max_tokens)
        self.emo_top_p = QDoubleSpinBox()
        self.emo_top_p.setRange(0.0, 1.0)
        self.emo_top_p.setSingleStep(0.05)
        self.emo_top_p.setValue(0.7)
        param_row.addSpacing(12)
        param_row.addWidget(QLabel("Top P:"))
        param_row.addWidget(self.emo_top_p)
        param_row.addStretch()
        emo_form.addRow(param_row)
        layout.addWidget(emo_group)

        # 按钮
        btn_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._on_reset_preset)
        btn_row.addStretch()
        btn_row.addWidget(reset_btn)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._on_save_preset)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        layout.addStretch()
        self._load_preset_form()
        return self._wrap_scroll(widget)

    def _load_preset_form(self):
        p = self.storage.load_tts_preset()
        self.tts_api_key.setText(p.tts_api_key)
        self.tts_base_url.setText(p.tts_base_url)
        self.tts_model.setCurrentText(p.tts_model or "s2.1-pro-free")
        self.tts_proxy.setText(p.tts_proxy)
        self.auto_tts_chk.setChecked(p.auto_tts_enabled)
        self.auto_play_chk.setChecked(p.auto_play_enabled)
        self.emotion_chk.setChecked(p.emotion_enabled)
        if hasattr(self, "emo_api"):
            idx = self.emo_api.findData(p.api_id)
            self.emo_api.setCurrentIndex(idx if idx >= 0 else 0)
        self.emo_system.setPlainText(p.system_prompt)
        self.emo_temp.setValue(p.temperature)
        self.emo_max_tokens.setValue(p.max_tokens)
        self.emo_top_p.setValue(p.top_p)

    def _on_save_preset(self):
        p = self.storage.load_tts_preset()
        p.tts_api_key = self.tts_api_key.text().strip()
        p.tts_base_url = self.tts_base_url.text().strip() or "https://api.fish.audio"
        p.tts_model = self.tts_model.currentText().strip() or "s2.1-pro-free"
        p.tts_proxy = self.tts_proxy.text().strip()
        p.auto_tts_enabled = self.auto_tts_chk.isChecked()
        p.auto_play_enabled = self.auto_play_chk.isChecked()
        p.emotion_enabled = self.emotion_chk.isChecked()
        p.api_id = self.emo_api.currentData() or ""
        p.system_prompt = self.emo_system.toPlainText() or DEFAULT_TTS_EMOTION_SYSTEM_PROMPT
        p.temperature = self.emo_temp.value()
        p.max_tokens = self.emo_max_tokens.value()
        p.top_p = self.emo_top_p.value()
        self.storage.save_tts_preset(p)
        QMessageBox.information(self, "已保存", "TTS 预设已保存。")

    def _on_reset_preset(self):
        reply = QMessageBox.question(self, "确认", "恢复情绪 LLM 提示词为默认值？")
        if reply != QMessageBox.Yes:
            return
        self.emo_system.setPlainText(DEFAULT_TTS_EMOTION_SYSTEM_PROMPT)

    def closeEvent(self, event):
        # 等待测试 worker 结束防崩溃（仿现有 worker 生命周期模式）
        if self._tts_test_worker is not None and self._tts_test_worker.isRunning():
            try:
                self._tts_test_worker.wait(5000)
            except Exception:
                pass
        super().closeEvent(event)
