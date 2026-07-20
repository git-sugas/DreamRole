"""ComfyUI 设置对话框。"""
from __future__ import annotations
import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QCheckBox, QFormLayout, QGroupBox, QSpinBox, QDoubleSpinBox,
    QComboBox, QMessageBox, QFileDialog, QWidget, QScrollArea,
)

from src.services.comfyui_service import (
    ComfyUiConfig, ComfyUiService, DEFAULT_WORKFLOW, MAX_LORAS,
)


# ComfyUI 常见采样器 / 调度器（可编辑下拉，用户也可手动输入自定义节点支持的值）
SAMPLER_NAMES = [
    "euler", "euler_ancestral", "euler_cfg_pp", "euler_ancestral_cfg_pp",
    "dpmpp_2m", "dpmpp_2m_cfg", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
    "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "dpmpp_sde", "dpmpp_sde_gpu",
    "dpm_fast", "dpm_adaptive", "ddim", "ddpm", "pndm", "heun", "heunpp2",
    "lms", "uni_pc", "uni_pc_bh2", "lcm", "ipndm", "ipndm_v",
]
SCHEDULERS = [
    "normal", "karras", "exponential", "sgm_uniform", "simple",
    "ddim_uniform", "beta", "klr", "kl_optimal",
]
LORA_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf")


class ComfyUiDialog(QDialog):
    def __init__(self, comfyui_service: ComfyUiService, parent=None):
        super().__init__(parent)
        self.service = comfyui_service
        self.config = comfyui_service.config
        self.setWindowTitle("ComfyUI 设置")
        self.resize(850, 1220)
        self.setMinimumSize(680, 600)
        self._build_ui()

    def _build_ui(self):
        # 整体内容包 QScrollArea：ComfyUI 设置项多（基础参数+LoRA+模型+工作流+提示+保存），
        # 静态高度远超对话框最小高度，无垂直滚动会裁掉底部提示与保存按钮。
        # [!] 水平滚动关闭（ScrollBarAlwaysOff）：setWidgetResizable(True) 配合水平 AsNeeded 时，
        # 内容 widget 会被拉到 sizeHint 宽度（lora name_combo 路径变长后 sizeHint 变宽），
        # 导致所有输入框被拉宽溢出对话框。关水平滚动后内容压缩到视口宽度，输入框按布局正常分配。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        # 内容 widget 最大宽度约束到视口，防止 name_combo 等控件的 sizeHint 撑宽整页
        content.setMaximumWidth(820)
        layout = QVBoxLayout(content)

        form = QFormLayout()
        self.enabled_cb = QCheckBox("启用文生图")
        self.enabled_cb.setChecked(self.config.enabled)
        form.addRow("", self.enabled_cb)

        self.url_edit = QLineEdit(self.config.server_url)
        form.addRow("服务器地址:", self.url_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setValue(self.config.timeout)
        form.addRow("超时(秒):", self.timeout_spin)
        layout.addLayout(form)

        # 测试连接
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_label = QLabel("")
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_label)
        test_row.addStretch()
        layout.addLayout(test_row)

        layout.addWidget(self._build_basic_group())
        layout.addWidget(self._build_model_group())
        layout.addWidget(self._build_lora_group())

        # 工作流 JSON
        wf_group = QGroupBox("工作流 JSON（用 {{positive}} 和 {{negative}} 作为提示词占位符）")
        wf_layout = QVBoxLayout(wf_group)
        self.wf_edit = QTextEdit()
        self.wf_edit.setPlainText(self.config.workflow_json or DEFAULT_WORKFLOW)
        self.wf_edit.setFontFamily("Consolas")
        wf_layout.addWidget(self.wf_edit)

        reset_wf_btn = QPushButton("恢复默认工作流")
        reset_wf_btn.clicked.connect(lambda: self.wf_edit.setPlainText(DEFAULT_WORKFLOW))
        wf_layout.addWidget(reset_wf_btn)
        layout.addWidget(wf_group)

        hint = QLabel(
            "使用说明：AI 回复中包含 [img:正面提示词] 或 [img:正面|负面] 标签时，"
            "自动调用 ComfyUI 生成图片并插入聊天（不加入上下文）。\n"
            "工作流通过通配符占位符接收参数，不依赖节点 class_type，兼容第三方改名插件：\n"
            "  数值占位符（JSON 中不带引号）：{{seed}} {{steps}} {{cfg}} {{width}} {{height}}\n"
            "  字符串占位符（JSON 中带引号）：{{positive}} {{negative}} {{sampler_name}} {{scheduler}} {{model_name}}\n"
            "  {{model_name}} 是通用模型名占位符，可在工作流任一加载器字段（ckpt_name/unet_name/clip_name/vae_name）"
            "用它（一处即可，一个值填不了多个不同模型名）。\n"
            "  多 LoRA 占位符（每个 LoraLoader 节点用对应序号）：\n"
            "    {{lora_name_1}} {{lora_strength_model_1}} {{lora_strength_clip_1}}（第 1 个）\n"
            "    {{lora_name_2}} {{lora_strength_model_2}} {{lora_strength_clip_2}}（第 2 个）\n"
            "    ... 到 {{lora_name_5}} 等，最多 5 个。\n"
            "  老占位符 {{lora_name}}/{{lora_strength_model}}/{{lora_strength_clip}} 仍可用（= 第 1 个）。\n"
            "LoRA 名称 = 固定前缀（全局共用）+ 选择的文件名。模型名/LoRA 名存库时统一转 / 分隔符。"
            "工作流中无对应占位符则该参数不生效。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 12px;")
        layout.addWidget(hint)

        # 保存
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

        # 把内容挂到滚动区
        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ---------- 基础参数 ----------
    def _build_basic_group(self) -> QGroupBox:
        group = QGroupBox("基础参数")
        form = QFormLayout(group)

        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 200)
        self.steps_spin.setValue(self.config.steps)
        form.addRow("步数 (steps):", self.steps_spin)

        self.cfg_spin = QDoubleSpinBox()
        self.cfg_spin.setRange(0.0, 30.0)
        self.cfg_spin.setSingleStep(0.5)
        self.cfg_spin.setValue(self.config.cfg)
        form.addRow("CFG:", self.cfg_spin)

        w_h_row = QHBoxLayout()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 8192)
        self.width_spin.setSingleStep(64)
        self.width_spin.setValue(self.config.width)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 8192)
        self.height_spin.setSingleStep(64)
        self.height_spin.setValue(self.config.height)
        w_h_row.addWidget(self.width_spin)
        w_h_row.addWidget(QLabel("×"))
        w_h_row.addWidget(self.height_spin)
        w_h_row.addStretch()
        form.addRow("图片宽 × 高:", w_h_row)

        self.sampler_combo = QComboBox()
        self.sampler_combo.setEditable(True)
        self.sampler_combo.addItems(SAMPLER_NAMES)
        self.sampler_combo.setCurrentText(self.config.sampler_name)
        form.addRow("采样器:", self.sampler_combo)

        self.scheduler_combo = QComboBox()
        self.scheduler_combo.setEditable(True)
        self.scheduler_combo.addItems(SCHEDULERS)
        self.scheduler_combo.setCurrentText(self.config.scheduler)
        form.addRow("调度器:", self.scheduler_combo)
        return group

    # ---------- 模型文件选择 ----------
    def _build_model_group(self) -> QGroupBox:
        # 通用模型文件选择：扫描本地 models 文件夹填下拉，占位符 {{model_name}} 注入工作流。
        # [!] 通用占位符，用户在工作流里任一加载器（unet/clip/vae/ckpt）字段用它（一处即可）。
        group = QGroupBox("模型文件（通用 {{model_name}} 占位符）")
        form = QFormLayout(group)
        # model 名单缓存：扫描结果存此，过滤时从缓存取子集重填下拉（与 lora 缓存同理）
        self._model_name_cache: list[str] = []

        # 文件夹选择
        folder_row = QHBoxLayout()
        self.model_folder_edit = QLineEdit(self.config.model_folder)
        self.model_folder_edit.setPlaceholderText("选择本地模型文件夹以读取文件名")
        pick_btn = QPushButton("选择文件夹")
        pick_btn.clicked.connect(self._pick_model_folder)
        folder_row.addWidget(self.model_folder_edit)
        folder_row.addWidget(pick_btn)
        form.addRow("模型文件夹:", folder_row)

        # 扫描按钮 + 状态
        rescan_row = QHBoxLayout()
        rescan_btn = QPushButton("重新扫描文件名")
        rescan_btn.clicked.connect(self._scan_models)
        self.model_scan_status_label = QLabel("")
        rescan_row.addWidget(rescan_btn)
        rescan_row.addWidget(self.model_scan_status_label)
        rescan_row.addStretch()
        form.addRow("", rescan_row)

        # 模型名下拉（可编辑：扫描结果填下拉，也允许手输）
        self.model_name_combo = QComboBox()
        self.model_name_combo.setEditable(True)
        self.model_name_combo.setPlaceholderText("选择或输入模型文件名")
        self.model_name_combo.setCurrentText(self.config.model_name)
        # 接输入过滤：边打字边从缓存过滤下拉项（大小写不敏感子串匹配）
        self._attach_combo_filter(self.model_name_combo, lambda: self._model_name_cache)
        form.addRow("模型名:", self.model_name_combo)

        # 初始化：已选文件夹时立即扫描填充下拉
        if self.config.model_folder and os.path.isdir(self.config.model_folder):
            self._scan_models()
        return group

    def _pick_model_folder(self):
        start = self.model_folder_edit.text().strip() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "选择模型文件夹", start)
        if folder:
            self.model_folder_edit.setText(folder)
            self._scan_models()

    def _scan_models(self):
        folder = self.model_folder_edit.text().strip()
        # 记住当前已输入/已选的名称，扫描后回填，避免重扫或换文件夹时丢失已输入值
        prev = self.model_name_combo.currentText()
        if not folder:
            self._model_name_cache = []
            self.model_scan_status_label.setText("未选择文件夹")
            self.model_scan_status_label.setStyleSheet("color: #e0af68; font-size: 12px;")
        elif not os.path.isdir(folder):
            self._model_name_cache = []
            self.model_scan_status_label.setText("文件夹不存在")
            self.model_scan_status_label.setStyleSheet("color: #f7768e; font-size: 12px;")
        else:
            names: list[str] = []
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(LORA_EXTS):
                        # [!] 保留 os.sep 原始分隔符（Windows 是 \）：
                        # ComfyUI Windows 本地版的 lora/model 列表用 \ 存路径
                        # （/object_info 返回的就是 \），之前误转 / 会导致
                        # 'value not in list' 400 错误。Linux ComfyUI 才用 /。
                        rel = os.path.relpath(os.path.join(root, f), folder)
                        names.append(rel)
            names.sort()
            self._model_name_cache = names  # 更新缓存，过滤时从缓存取子集
            self.model_scan_status_label.setText(f"已扫描 {len(names)} 个文件")
            self.model_scan_status_label.setStyleSheet("color: #9ece6a; font-size: 12px;")
        # 用缓存全量重填下拉（扫描后应全量显示，不被 currentText 过滤）
        self._populate_combo_full(self.model_name_combo, self._model_name_cache)
        # 回填原值（editable combo 保留任意文本；若新文件夹无此文件仍显示，便于对照）
        if prev:
            self.model_name_combo.setCurrentText(prev)

    # ---------- LoRA 配置 ----------
    def _build_lora_group(self) -> QGroupBox:
        group = QGroupBox("LoRA 配置（可叠加多个，最多 5 个）")
        form = QFormLayout(group)

        # 文件夹选择（全局共享，所有 lora 行的下拉都从这里扫描文件名）
        folder_row = QHBoxLayout()
        self.lora_folder_edit = QLineEdit(self.config.lora_folder)
        self.lora_folder_edit.setPlaceholderText("选择本地 LoRA 文件夹以读取文件名")
        pick_btn = QPushButton("选择文件夹")
        pick_btn.clicked.connect(self._pick_lora_folder)
        folder_row.addWidget(self.lora_folder_edit)
        folder_row.addWidget(pick_btn)
        form.addRow("LoRA 文件夹:", folder_row)

        # 扫描按钮（手动重新扫描）
        rescan_row = QHBoxLayout()
        rescan_btn = QPushButton("重新扫描文件名")
        rescan_btn.clicked.connect(self._scan_loras)
        self.scan_status_label = QLabel("")
        rescan_row.addWidget(rescan_btn)
        rescan_row.addWidget(self.scan_status_label)
        rescan_row.addStretch()
        form.addRow("", rescan_row)

        # 固定前缀（全局共用，所有 lora_name_N 都拼 prefix + name）
        self.lora_prefix_edit = QLineEdit(self.config.lora_prefix)
        self.lora_prefix_edit.setPlaceholderText("可空，适配「前缀+文件名」选 lora 的插件")
        form.addRow("固定前缀:", self.lora_prefix_edit)

        # 多 lora 行容器
        self._lora_rows: list[dict] = []  # 每行: {name_combo, sm_spin, sc_spin, del_btn, container, row_layout}
        # lora 名单缓存：扫描结果 + 首项 "None"（rgthree Lora Loader Stack 用 "None" 表示无 lora）。
        # 新增行用此缓存填充下拉，避免「+ 号新增的行下拉空、需重新扫描」的问题。
        self._lora_name_cache: list[str] = ["None"]
        self._lora_rows_container = QVBoxLayout()
        self._lora_rows_container.setSpacing(4)
        form.addRow("LoRA 列表:", self._lora_rows_container)

        # 新增按钮行
        add_row = QHBoxLayout()
        self.add_lora_btn = QPushButton("+ 新增 LoRA")
        self.add_lora_btn.clicked.connect(self._on_add_lora_row)
        add_row.addWidget(self.add_lora_btn)
        add_row.addStretch()
        form.addRow("", add_row)

        # 初始化：按已保存的 lora_names 长度建行（至少 1 行）
        names = self.config.lora_names or [""]
        sm = self.config.lora_strength_models or [0.8]
        sc = self.config.lora_strength_clips or [0.8]
        count = max(1, len(names))
        for i in range(count):
            self._add_lora_row(
                name=names[i] if i < len(names) else "",
                sm=sm[i] if i < len(sm) else 0.8,
                sc=sc[i] if i < len(sc) else 0.8,
            )
        # 打开对话框时：文件夹仍有效则扫描填充下拉选项
        if self.config.lora_folder and os.path.isdir(self.config.lora_folder):
            self._scan_loras()
        return group

    def _populate_lora_combo(self, combo: QComboBox):
        """用 lora 名单缓存填充下拉（首项 "None" 表示无 lora，rgthree 约定）。
        按当前输入文本过滤；无输入时全量填充。"""
        self._populate_combo_filtered(combo, self._lora_name_cache)

    def _populate_combo_full(self, combo: QComboBox, all_items: list[str]):
        """全量填充下拉（不过滤），用于扫描刷新场景。
        [!] 与 _populate_combo_filtered 区别：不按 currentText 过滤，直接全量 addItems。
        扫描后应全量显示所有文件，再由回填逻辑恢复 currentText。
        """
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(all_items)
        combo.blockSignals(False)

    def _populate_combo_filtered(self, combo: QComboBox, all_items: list[str]):
        """按 combo 当前输入文本过滤 all_items 后重填下拉（大小写不敏感子串匹配）。
        [!] 保留 combo 当前 currentText（用户正在输入的过滤词），重填后恢复。
        [!] "None" 始终保留（lora 无选项时的占位，rgthree 约定）；model 列表无 None 不受影响。
        [!] 用 blockSignals 避免 clear/addItems 触发 textEdited 死循环。
        """
        text = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        if not text:
            # 无输入：全量填充
            combo.addItems(all_items)
        else:
            # 有输入：大小写不敏感子串匹配，"None" 始终保留
            text_lower = text.lower()
            filtered = [
                item for item in all_items
                if item == "None" or text_lower in item.lower()
            ]
            combo.addItems(filtered)
        combo.blockSignals(False)
        # 恢复用户正在输入的文本（clear 会清空 lineEdit）
        combo.setEditText(text)

    def _attach_combo_filter(self, combo: QComboBox, all_items_getter):
        """给可编辑 QComboBox 接输入过滤：textEdited 时按输入从 all_items_getter() 取子集重填。
        [!] 用 textEdited 而非 textChanged：textEdited 只在用户键入时触发，
        代码 setCurrentText/addItems 不会触发（避免死循环）。
        [!] 重填后弹出下拉让用户看到过滤结果；保留光标位置避免跳回行首。
        """
        def _on_text_edited(_text: str):
            # 记住光标位置，重填后恢复（否则 setEditText 会把光标移到末尾）
            cursor_pos = combo.lineEdit().cursorPosition()
            self._populate_combo_filtered(combo, all_items_getter())
            combo.lineEdit().setCursorPosition(cursor_pos)
            # 弹出下拉显示过滤结果（若已聚焦）
            if combo.hasFocus():
                combo.showPopup()
        combo.lineEdit().textEdited.connect(_on_text_edited)

    def _add_lora_row(self, name: str = "", sm: float = 0.8, sc: float = 0.8):
        """新增一行 lora 配置：序号 + 名称下拉 + 模型强度 + CLIP 强度 + 删除按钮。"""
        if len(self._lora_rows) >= MAX_LORAS:
            return
        row_layout = QHBoxLayout()
        row_layout.setSpacing(4)
        idx = len(self._lora_rows) + 1
        idx_label = QLabel(f"{idx}.")
        idx_label.setFixedWidth(20)
        name_combo = QComboBox()
        name_combo.setEditable(True)
        name_combo.setPlaceholderText("选择或输入 LoRA 文件名")
        # 用缓存填充下拉（含 "None" 首项），避免新增行下拉为空需重新扫描
        self._populate_lora_combo(name_combo)
        # 接输入过滤：边打字边从缓存过滤下拉项（大小写不敏感子串匹配，None 始终保留）
        self._attach_combo_filter(name_combo, lambda: self._lora_name_cache)
        # 未指定 name 时默认选 "None"（rgthree Lora Loader Stack 用 "None" 表示无 lora，
        # 空串可能被节点误判；用户可手动改成具体 lora 文件名）
        name_combo.setCurrentText(name if name else "None")
        sm_spin = QDoubleSpinBox()
        sm_spin.setRange(-10.0, 10.0)
        sm_spin.setSingleStep(0.05)
        sm_spin.setValue(sm)
        sm_spin.setFixedWidth(80)
        sc_spin = QDoubleSpinBox()
        sc_spin.setRange(-10.0, 10.0)
        sc_spin.setSingleStep(0.05)
        sc_spin.setValue(sc)
        sc_spin.setFixedWidth(80)
        del_btn = QPushButton("×")
        del_btn.setFixedWidth(28)
        del_btn.setToolTip("删除此 LoRA")
        row_layout.addWidget(idx_label)
        row_layout.addWidget(name_combo, 1)
        row_layout.addWidget(QLabel("模型"))
        row_layout.addWidget(sm_spin)
        row_layout.addWidget(QLabel("CLIP"))
        row_layout.addWidget(sc_spin)
        row_layout.addWidget(del_btn)
        row_info = {
            "name_combo": name_combo, "sm_spin": sm_spin, "sc_spin": sc_spin,
            "del_btn": del_btn, "row_layout": row_layout, "idx_label": idx_label,
        }
        # 删除回调：移除行并重排版序号
        del_btn.clicked.connect(lambda _, r=row_info: self._on_del_lora_row(r))
        self._lora_rows.append(row_info)
        self._lora_rows_container.addLayout(row_layout)
        self._refresh_lora_row_indices()
        self._refresh_add_btn_state()

    def _on_add_lora_row(self):
        self._add_lora_row()

    def _on_del_lora_row(self, row_info: dict):
        """删除指定 lora 行（至少保留 1 行），重排版序号与状态。"""
        if len(self._lora_rows) <= 1:
            return  # 至少保留 1 行
        # 逐个移除子控件再删 layout（layout 不能直接 deleteLater）
        rl = row_info["row_layout"]
        while rl.count():
            item = rl.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._lora_rows_container.removeItem(rl)
        self._lora_rows.remove(row_info)
        self._refresh_lora_row_indices()
        self._refresh_add_btn_state()

    def _refresh_lora_row_indices(self):
        """删除/新增后重排版每行序号标签。"""
        for i, row_info in enumerate(self._lora_rows):
            row_info["idx_label"].setText(f"{i + 1}.")

    def _refresh_add_btn_state(self):
        """达到 MAX_LORAS 时禁用新增按钮。"""
        self.add_lora_btn.setEnabled(len(self._lora_rows) < MAX_LORAS)

    # ---------- LoRA 文件夹操作 ----------
    def _pick_lora_folder(self):
        start = self.lora_folder_edit.text().strip() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "选择 LoRA 文件夹", start)
        if folder:
            self.lora_folder_edit.setText(folder)
            self._scan_loras()

    def _scan_loras(self):
        folder = self.lora_folder_edit.text().strip()
        # 记住每行当前已输入/已选的名称，扫描后各自回填，避免重扫或换文件夹时丢失已输入值
        prev_names = [r["name_combo"].currentText() for r in self._lora_rows]
        if not folder:
            self._lora_name_cache = ["None"]
            self.scan_status_label.setText("未选择文件夹")
            self.scan_status_label.setStyleSheet("color: #e0af68; font-size: 12px;")
        elif not os.path.isdir(folder):
            self._lora_name_cache = ["None"]
            self.scan_status_label.setText("文件夹不存在")
            self.scan_status_label.setStyleSheet("color: #f7768e; font-size: 12px;")
        else:
            names: list[str] = ["None"]  # 首项 "None" 表示无 lora（rgthree 约定）
            scanned: list[str] = []
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(LORA_EXTS):
                        # [!] 保留 os.sep 原始分隔符（Windows 是 \）：
                        # ComfyUI Windows 本地版的 lora/model 列表用 \ 存路径
                        # （/object_info 返回的就是 \），之前误转 / 会导致
                        # 'value not in list' 400 错误。Linux ComfyUI 才用 /。
                        rel = os.path.relpath(os.path.join(root, f), folder)
                        scanned.append(rel)
            scanned.sort()
            names.extend(scanned)
            self._lora_name_cache = names  # 更新缓存，后续 + 号新增行也用此缓存
            self.scan_status_label.setText(f"已扫描 {len(scanned)} 个文件")
            self.scan_status_label.setStyleSheet("color: #9ece6a; font-size: 12px;")
        # 用缓存全量刷新所有行下拉（扫描后应全量显示，不被 currentText 过滤）
        for r in self._lora_rows:
            self._populate_combo_full(r["name_combo"], self._lora_name_cache)
        # 各行回填原值（editable combo 保留任意文本；若新文件夹无此文件仍显示，便于对照）
        for r, prev in zip(self._lora_rows, prev_names):
            if prev:
                r["name_combo"].setCurrentText(prev)

    # ---------- 测试连接 ----------
    def _test_connection(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        temp_config = ComfyUiConfig(server_url=url)
        temp_service = ComfyUiService(temp_config)
        ok, msg = temp_service.test_connection()
        self.test_label.setText(msg)
        self.test_label.setStyleSheet(
            f"color: {'#9ece6a' if ok else '#f7768e'}; font-size: 12px;"
        )

    # ---------- 保存 ----------
    def _save(self):
        # 验证 JSON（占位符替换为临时值后校验，兼容含 {{...}} 的模板）
        ok, err = ComfyUiService.validate_workflow_json(self.wf_edit.toPlainText())
        if not ok:
            QMessageBox.warning(self, "JSON 错误", f"工作流 JSON 格式错误:\n{err}")
            return
        self.config.enabled = self.enabled_cb.isChecked()
        self.config.server_url = self.url_edit.text().strip()
        self.config.timeout = self.timeout_spin.value()
        self.config.workflow_json = self.wf_edit.toPlainText()
        # 基础参数
        self.config.steps = self.steps_spin.value()
        self.config.cfg = self.cfg_spin.value()
        self.config.width = self.width_spin.value()
        self.config.height = self.height_spin.value()
        self.config.sampler_name = self.sampler_combo.currentText().strip()
        self.config.scheduler = self.scheduler_combo.currentText().strip()
        # LoRA（多 lora 列表，从各行控件收集）
        self.config.lora_folder = self.lora_folder_edit.text().strip()
        self.config.lora_prefix = self.lora_prefix_edit.text().strip()
        # [!] lora_names 保留原始分隔符不转换：
        # ComfyUI Windows 本地版用 \（/object_info 返回的就是 \），Linux 版才用 /。
        # 扫描结果已是 os.sep 原始格式，手输值也保留用户输入原样，不在存库时强制转换。
        self.config.lora_names = [
            r["name_combo"].currentText().strip() for r in self._lora_rows
        ]
        self.config.lora_strength_models = [r["sm_spin"].value() for r in self._lora_rows]
        self.config.lora_strength_clips = [r["sc_spin"].value() for r in self._lora_rows]
        # 模型文件（通用 {{model_name}} 占位符）
        self.config.model_folder = self.model_folder_edit.text().strip()
        self.config.model_name = self.model_name_combo.currentText().strip()
        self.service.config = self.config
        from src.services.comfyui_service import save_comfyui_config
        save_comfyui_config(self.config)
        QMessageBox.information(self, "已保存", "ComfyUI 设置已保存。")
        self.accept()
