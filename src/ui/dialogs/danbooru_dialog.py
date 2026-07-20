"""Danbooru Tag 设置对话框：库管理 + 模式开关 + 负面模板 + 测试区。

管「库/模式/nsfw/召回数/负面模板」这些 Danbooru 专属设置；
LLM 加工的 API+prompt 在「API 与预设 → Danbooru 加工」标签页。

测试区：输入中文 → 后台跑完整链路（emb召回→LLM加工，固定自动模式）→
分两栏显示召回候选 + 加工结果，方便定位问题（库/emb/LLM 哪一环）。
"""
from __future__ import annotations
import os
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QCheckBox, QFormLayout, QGroupBox, QSpinBox,
    QDoubleSpinBox,
    QRadioButton, QButtonGroup, QMessageBox, QFileDialog, QWidget,
    QScrollArea, QProgressBar, QListWidget, QListWidgetItem, QSplitter,
)

from src.models import (
    DanbooruPreset, default_danbooru_preset, DEFAULT_NEGATIVE_PROMPT,
    DEFAULT_POSITIVE_PREFIX,
    DANBOORU_CATEGORY_LIST, category_label,
)
from src.services.storage import Storage


class DanbooruSettingsDialog(QDialog):
    def __init__(self, storage: Storage, danbooru_service, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.service = danbooru_service
        self.setWindowTitle("Danbooru Tag 设置")
        self.resize(920, 1200)
        self.setMinimumSize(820, 720)
        self.preset = storage.load_danbooru_preset()
        self._index_worker = None
        self._test_worker = None
        # 测试区单步调试缓存：最近一次召回的候选与输入文本，供「测试-加工」复用。
        self._last_cands = []   # list[TagCandidate]
        self._last_text = ""    # 召回时的输入文本（加工前校验一致）
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)

        layout.addWidget(self._build_csv_group())
        layout.addWidget(self._build_mode_group())
        layout.addWidget(self._build_positive_group())
        layout.addWidget(self._build_negative_group())
        layout.addWidget(self._build_char_whitelist_group())
        layout.addWidget(self._build_test_group())

        # 保存
        save_btn = QPushButton("保存设置")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ---------- CSV 导入与建库 ----------
    def _build_csv_group(self) -> QGroupBox:
        group = QGroupBox("标签库（CSV 导入与重建）")
        layout = QVBoxLayout(group)

        # 当前路径 + 状态
        info_row = QHBoxLayout()
        info_row.addWidget(QLabel("CSV 文件:"))
        self.csv_label = QLabel(self.preset.csv_path or "（未选择）")
        self.csv_label.setWordWrap(True)
        self.csv_label.setStyleSheet("color: #565f89;")
        info_row.addWidget(self.csv_label, 1)
        layout.addLayout(info_row)

        self.db_status_label = QLabel("")
        self.db_status_label.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(self.db_status_label)

        # 按钮行
        btn_row = QHBoxLayout()
        pick_btn = QPushButton("选择 CSV…")
        pick_btn.clicked.connect(self._pick_csv)
        btn_row.addWidget(pick_btn)
        self.rebuild_btn = QPushButton("重建标签库")
        self.rebuild_btn.setObjectName("primaryBtn")
        self.rebuild_btn.clicked.connect(self._rebuild)
        btn_row.addWidget(self.rebuild_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        hint = QLabel(
            "CSV 格式：name,cn_name,wiki,post_count,category,nsfw（6 列标准逗号分隔，"
            "cn_name 用引号包裹多个中文别名）。"
            "embedding 用「API 与预设 → 记忆整理」配置的 API，请先在那里绑定。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(hint)

        self._refresh_db_status()
        return group

    def _refresh_db_status(self):
        count = self.service.db_count() if self.service else 0
        mtime = self.preset.last_csv_mtime or "—"
        self.db_status_label.setText(
            f"库内当前 {count} 条 | 上次建库：{mtime}"
        )

    def _pick_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Danbooru 标签 CSV", "", "CSV 文件 (*.csv);;所有文件 (*)"
        )
        if path:
            self.csv_label.setText(path)
            # 暂存路径，保存时写入 preset；此处先记录到 preset 内存对象供重建用
            self.preset.csv_path = path

    def _rebuild(self):
        csv_path = self.csv_label.text().strip()
        if not csv_path or csv_path == "（未选择）":
            QMessageBox.warning(self, "提示", "请先选择 CSV 文件。")
            return
        if not os.path.isfile(csv_path):
            QMessageBox.warning(self, "提示", f"文件不存在：{csv_path}")
            return
        reply = QMessageBox.question(
            self, "确认重建",
            f"将从 CSV 重建标签库（约需几分钟，取决于 embedding 服务商）。\n"
            f"CSV：{csv_path}\n继续？"
        )
        if reply != QMessageBox.Yes:
            return
        # 启动后台 worker
        self.rebuild_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._index_worker = _IndexWorker(self.service, csv_path)
        self._index_worker.progress_signal.connect(self._on_index_progress)
        self._index_worker.finished_signal.connect(self._on_index_finished)
        self._index_worker.start()

    def _on_index_progress(self, current, total):
        if total > 0:
            self.progress.setValue(int(current / total * 100))

    def _on_index_finished(self, success, msg, count):
        self.progress.setVisible(False)
        self.rebuild_btn.setEnabled(True)
        if success:
            self.preset.last_db_count = count
            try:
                mtime = os.path.getmtime(self.preset.csv_path)
                self.preset.last_csv_mtime = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(mtime)
                )
            except Exception:
                self.preset.last_csv_mtime = time.strftime("%Y-%m-%d %H:%M:%S")
            self.storage.save_danbooru_preset(self.preset)
            self._refresh_db_status()
            QMessageBox.information(self, "完成", msg)
        else:
            QMessageBox.warning(self, "失败", msg)
        self._index_worker = None

    # ---------- 模式与开关 ----------
    def _build_mode_group(self) -> QGroupBox:
        group = QGroupBox("加工模式")
        layout = QVBoxLayout(group)

        mode_row = QHBoxLayout()
        self.rb_auto = QRadioButton("自动模式（emb召回 → 直接送 LLM 加工）")
        self.rb_manual = QRadioButton("手改模式（emb召回 → 弹窗勾选 → LLM 加工）")
        self.rb_auto.setChecked(not self.preset.manual_mode)
        self.rb_manual.setChecked(self.preset.manual_mode)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_auto)
        self.mode_group.addButton(self.rb_manual)
        mode_row.addWidget(self.rb_auto)
        mode_row.addWidget(self.rb_manual)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        form = QFormLayout()
        self.nsfw_cb = QCheckBox("允许召回 NSFW 标签（关闭则全局过滤）")
        self.nsfw_cb.setChecked(self.preset.allow_nsfw)
        form.addRow("", self.nsfw_cb)

        self.wiki_fts_cb = QCheckBox("FTS5 召回 wiki（开启则 wiki 含查询词也召回并低权重打分；关闭仅 cn_search）")
        self.wiki_fts_cb.setChecked(self.preset.enable_wiki_fts)
        form.addRow("", self.wiki_fts_cb)

        recall_row = QHBoxLayout()
        self.recall_spin = QSpinBox()
        self.recall_spin.setRange(10, 100)
        self.recall_spin.setValue(self.preset.recall_top_n)
        recall_row.addWidget(self.recall_spin)
        recall_row.addWidget(QLabel("（embedding 召回的候选 tag 数量）"))
        recall_row.addStretch()
        form.addRow("召回数量:", recall_row)
        layout.addLayout(form)

        # [!] 召回分类过滤（全局生效）：勾选的 category 才会进入召回候选，
        # 聊天出图 / 头像自动生成 / 测试区同源生效（recall_candidates 在融合排序后硬过滤）。
        # 默认全开（向后兼容老 preset）；用户去掉某类（如「画师」「版权」）即可让该类 tag
        # 不再出现在候选池，LLM 加工时自然不会被选中。
        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("召回分类过滤:"))
        self.cat_checks: dict[int, QCheckBox] = {}
        allow_cats = set(self.preset.allow_categories or (0, 1, 3, 4, 5))
        for cat_int, cat_label in DANBOORU_CATEGORY_LIST:
            cb = QCheckBox(cat_label)
            cb.setChecked(cat_int in allow_cats)
            cb.setToolTip(f"category={cat_int}，不勾选则召回时丢弃该类 tag")
            self.cat_checks[cat_int] = cb
            cat_row.addWidget(cb)
        cat_row.addStretch()
        layout.addLayout(cat_row)
        cat_hint = QLabel(
            "勾选的类别才会进入召回候选（融合排序后硬过滤）。"
            "聊天出图 [img:...]、角色/用户头像自动生成、本对话框测试区同源生效。"
        )
        cat_hint.setWordWrap(True)
        cat_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(cat_hint)

        # 融合权重：score = w_emb·emb + w_fts·fts + w_wiki·wiki + w_pc·pc_norm
        # 默认值 0.5/0.35/0.1/0.15 = 经验值；不强制归一化（便于压低总分做对比）。
        # w_fts 给 cn_name 精确命中（高置信度）；w_wiki 给 wiki 语义兜底命中（低置信度，远低于 w_fts）。
        weight_group = QGroupBox("融合权重（score = w_emb·emb_sim + w_fts·fts_sim + w_wiki·wiki_sim + w_pc·pc_norm）")
        wl = QVBoxLayout(weight_group)
        weight_row = QHBoxLayout()
        def _make_spin(val: float) -> QDoubleSpinBox:
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1.0)
            sp.setSingleStep(0.05)
            sp.setDecimals(2)
            sp.setValue(val)
            # 80px 过窄：QSS 横向 padding(10px) + 内置上下按钮 sub-control 会把数值
            # 挤到按钮上重叠。放宽到 110px 给数值留出独立显示区。
            sp.setFixedWidth(110)
            return sp
        self.weight_emb_spin = _make_spin(self.preset.weight_emb)
        self.weight_fts_spin = _make_spin(self.preset.weight_fts)
        self.weight_wiki_spin = _make_spin(self.preset.weight_wiki)
        self.weight_pc_spin = _make_spin(self.preset.weight_pc)
        for sp in (self.weight_emb_spin, self.weight_fts_spin, self.weight_wiki_spin, self.weight_pc_spin):
            sp.valueChanged.connect(lambda *_: self._update_weight_sum())
        # 四个权重输入各占一列（标签居上 + 数值居下）；addStretch 左推挤防窄窗重叠。
        for label_text, spin in (("embedding", self.weight_emb_spin),
                                 ("FTS5", self.weight_fts_spin),
                                 ("wiki", self.weight_wiki_spin),
                                 ("post_count", self.weight_pc_spin)):
            col = QVBoxLayout()
            col.addWidget(QLabel(label_text), alignment=Qt.AlignCenter)
            col.addWidget(spin, alignment=Qt.AlignCenter)
            weight_row.addLayout(col)
        weight_row.addStretch()
        wl.addLayout(weight_row)

        # 归一化 + 恢复默认按钮单放一行：原与输入框同行，窄窗下挤到数值上重叠。
        btn_row = QHBoxLayout()
        norm_btn = QPushButton("权重归一化")
        norm_btn.setToolTip("四个权重等比缩放到和=1.0（和为0则重置默认）")
        norm_btn.clicked.connect(self._normalize_weights)
        reset_w_btn = QPushButton("恢复默认")
        reset_w_btn.clicked.connect(self._reset_weights)
        btn_row.addWidget(norm_btn)
        btn_row.addWidget(reset_w_btn)
        btn_row.addStretch()
        wl.addLayout(btn_row)
        self.weight_sum_label = QLabel("")
        self.weight_sum_label.setStyleSheet("color: #565f89; font-size: 11px;")
        wl.addWidget(self.weight_sum_label)
        layout.addWidget(weight_group)
        self._update_weight_sum()

        return group

    def _update_weight_sum(self):
        s = (self.weight_emb_spin.value() + self.weight_fts_spin.value()
             + self.weight_wiki_spin.value() + self.weight_pc_spin.value())
        self.weight_sum_label.setText(f"当前权重和 = {s:.2f}" + ("（和=1.0 ✓）" if abs(s - 1.0) < 1e-6 else "（非1.0，可点归一化）"))

    def _normalize_weights(self):
        s = (self.weight_emb_spin.value() + self.weight_fts_spin.value()
             + self.weight_wiki_spin.value() + self.weight_pc_spin.value())
        if s <= 1e-6:
            # 全0无法归一化，重置默认
            self._reset_weights()
            return
        self.weight_emb_spin.setValue(self.weight_emb_spin.value() / s)
        self.weight_fts_spin.setValue(self.weight_fts_spin.value() / s)
        self.weight_wiki_spin.setValue(self.weight_wiki_spin.value() / s)
        self.weight_pc_spin.setValue(self.weight_pc_spin.value() / s)
        self._update_weight_sum()

    def _reset_weights(self):
        self.weight_emb_spin.setValue(0.5)
        self.weight_fts_spin.setValue(0.20)
        self.weight_wiki_spin.setValue(0.10)
        self.weight_pc_spin.setValue(0.20)
        self._update_weight_sum()

    # ---------- 正面提示词模板 ----------
    def _build_positive_group(self) -> QGroupBox:
        group = QGroupBox("正面提示词前缀（拼在加工产出的正向 tag 之前，质量与画风统一控制）")
        layout = QVBoxLayout(group)
        hint = QLabel(
            "LLM 加工时已被约束只出画面内容 tag、不输出质量词，"
            "质量/画风词由这里统一追加在前。留空则不追加。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(hint)
        self.pos_edit = QTextEdit()
        self.pos_edit.setPlainText(self.preset.positive_prefix)
        self.pos_edit.setMinimumHeight(80)
        layout.addWidget(self.pos_edit)
        reset_btn = QPushButton("恢复默认正面提示词")
        reset_btn.clicked.connect(
            lambda: self.pos_edit.setPlainText(DEFAULT_POSITIVE_PREFIX)
        )
        layout.addWidget(reset_btn)
        return group

    # ---------- 负面模板 ----------
    def _build_negative_group(self) -> QGroupBox:
        group = QGroupBox("负面提示词模板（拼在加工产出的正向 tag 之后）")
        layout = QVBoxLayout(group)
        self.neg_edit = QTextEdit()
        self.neg_edit.setPlainText(self.preset.negative_prompt)
        self.neg_edit.setMinimumHeight(80)
        layout.addWidget(self.neg_edit)
        reset_btn = QPushButton("恢复默认负面模板")
        reset_btn.clicked.connect(
            lambda: self.neg_edit.setPlainText(DEFAULT_NEGATIVE_PROMPT)
        )
        layout.addWidget(reset_btn)
        return group

    # ---------- 单字白名单（查询单字过滤，反向：不在表里丢弃）----------
    def _build_char_whitelist_group(self) -> QGroupBox:
        group = QGroupBox("单字白名单（查询单字过滤）")
        layout = QVBoxLayout(group)
        hint = QLabel(
            "查询时 jieba 切出的「CJK 单字 token」不在此表则丢弃（多字 token 不受影响）。"
            "建库时自动从 cn_name 里本来就存在的单字 alias 重新生成；此处可手动增删，每行一个单字。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(hint)
        self.char_whitelist_edit = QTextEdit()
        # 读取现有白名单（每行一个单字）回填，文件不存在则为空
        chars = self.storage.load_char_whitelist()
        self.char_whitelist_edit.setPlainText("\n".join(sorted(chars)))
        self.char_whitelist_edit.setMinimumHeight(80)
        layout.addWidget(self.char_whitelist_edit)
        wl_btn_row = QHBoxLayout()
        save_wl_btn = QPushButton("保存白名单")
        save_wl_btn.clicked.connect(self._save_char_whitelist)
        wl_btn_row.addWidget(save_wl_btn)
        wl_btn_row.addStretch()
        layout.addLayout(wl_btn_row)
        return group

    def _save_char_whitelist(self):
        """解析编辑框文本为单字集合（每行/每个非空 token 取首个 CJK 字）保存。"""
        text = self.char_whitelist_edit.toPlainText()
        chars: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # 每行可能含多个字（用户随意输入），逐字符收 CJK 单字
            for ch in line:
                if "\u4e00" <= ch <= "\u9fff":
                    chars.add(ch)
        self.storage.save_char_whitelist(chars)
        # 回填规范化后的内容（排序、每行一个）
        self.char_whitelist_edit.setPlainText("\n".join(sorted(chars)))
        QMessageBox.information(self, "已保存", f"单字白名单已保存（{len(chars)} 字）。")

    # ---------- 测试区 ----------
    def _build_test_group(self) -> QGroupBox:
        group = QGroupBox("测试（输入中文 -> 看召回候选与加工结果，验证链路）")
        layout = QVBoxLayout(group)

        input_row = QHBoxLayout()
        self.test_input = QLineEdit()
        self.test_input.setPlaceholderText("输入中文描述，如：一个蓝发女孩在教室里微笑")
        input_row.addWidget(self.test_input, 1)
        # 三个并列按钮：①只召回 ②只加工 ③完整链路（手改模式弹勾选窗）
        self.test_recall_btn = QPushButton("测试-召回")
        self.test_recall_btn.clicked.connect(lambda: self._run_test("recall"))
        input_row.addWidget(self.test_recall_btn)
        self.test_process_btn = QPushButton("测试-加工")
        self.test_process_btn.clicked.connect(lambda: self._run_test("process"))
        input_row.addWidget(self.test_process_btn)
        self.test_full_btn = QPushButton("完整链路")
        self.test_full_btn.setObjectName("primaryBtn")
        self.test_full_btn.clicked.connect(lambda: self._run_test("full"))
        input_row.addWidget(self.test_full_btn)
        layout.addLayout(input_row)

        self.test_status = QLabel("")
        self.test_status.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(self.test_status)

        splitter = QSplitter(Qt.Vertical)
        # 召回候选
        cand_box = QGroupBox("① 召回的候选 tag（验证 emb 建库与检索）")
        cl = QVBoxLayout(cand_box)
        # [!] 分类过滤已上移到「加工模式」组全局生效（聊天出图/头像生成/测试区同源），
        # 这里不再放局部复选框行；测试区候选列表直接显示已按全局配置过滤后的结果。
        cat_hint = QLabel("候选已按上方「召回分类过滤」设置过滤（聊天出图与头像生成同源生效）。")
        cat_hint.setStyleSheet("color: #565f89; font-size: 11px;")
        cat_hint.setWordWrap(True)
        cl.addWidget(cat_hint)
        self.cand_list = QListWidget()
        self.cand_list.setMinimumHeight(150)
        cl.addWidget(self.cand_list)
        splitter.addWidget(cand_box)
        # 加工结果
        out_box = QGroupBox("② LLM 加工后的英文 tag 串（验证加工预设）")
        ol = QVBoxLayout(out_box)
        self.result_edit = QTextEdit()
        self.result_edit.setReadOnly(True)
        self.result_edit.setMinimumHeight(90)
        ol.addWidget(self.result_edit)
        splitter.addWidget(out_box)
        splitter.setSizes([300, 150])
        layout.addWidget(splitter)

        return group

    def _run_test(self, mode: str):
        """启动测试 worker。mode: 'recall' | 'process' | 'full'。"""
        text = self.test_input.text().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请输入测试文本。")
            return
        if self.service is None:
            QMessageBox.warning(self, "提示", "服务未初始化。")
            return
        if self.service.db_count() == 0:
            QMessageBox.warning(
                self, "提示", "标签库为空，请先在上方导入 CSV 并重建标签库。"
            )
            return
        if mode == "process":
            # 「测试-加工」复用上次召回结果；未召回或输入文本变了则提示先召回
            if not self._last_cands:
                QMessageBox.information(self, "提示", "请先点「测试-召回」拿到候选，再点「测试-加工」。")
                return
            if self._last_text != text:
                reply = QMessageBox.question(
                    self, "输入已变更",
                    "输入文本与上次召回时不一致，是否用上次召回的候选继续加工？\n"
                    "（点「否」则取消，请先重新点「测试-召回」）"
                )
                if reply != QMessageBox.Yes:
                    return
        # 锁定三个按钮
        for btn in (self.test_recall_btn, self.test_process_btn, self.test_full_btn):
            btn.setEnabled(False)
        status_map = {
            "recall": "正在召回…",
            "process": "正在加工（用上次召回的候选）…",
            "full": "正在召回 + 加工…",
        }
        self.test_status.setText(status_map.get(mode, "处理中…"))
        if mode != "process":
            self.cand_list.clear()
        self.result_edit.clear()
        # 后台跑链路
        self._test_worker = _TestWorker(
            self.service, self.storage, text, mode=mode,
            cands=self._last_cands if mode == "process" else None,
        )
        self._test_worker.candidates_signal.connect(self._on_test_candidates)
        self._test_worker.result_signal.connect(self._on_test_result)
        self._test_worker.finished_signal.connect(self._on_test_finished)
        # 手改模式完整链路：连接阻塞信号弹勾选窗
        self._test_worker.manual_select_request.connect(
            self._on_test_manual_select, Qt.BlockingQueuedConnection
        )
        self._test_worker.start()

    def _on_test_candidates(self, cand_lines):
        """接收召回候选（每行一条展示串），填入列表。"""
        self.cand_list.clear()
        for line, cat_int in cand_lines:
            item = QListWidgetItem(line)
            item.setData(Qt.UserRole + 2, cat_int)  # 存 category int（保留以备扩展，过滤已在召回层做）
            self.cand_list.addItem(item)

    def _on_test_result(self, result_text):
        self.result_edit.setPlainText(result_text)

    def _on_test_finished(self, ok, msg, cands, text):
        """测试完成：解锁按钮，缓存召回结果（供「测试-加工」复用）。"""
        for btn in (self.test_recall_btn, self.test_process_btn, self.test_full_btn):
            btn.setEnabled(True)
        self.test_status.setText(msg)
        # recall/full 模式会回传召回的候选与文本，缓存供「测试-加工」用
        if cands is not None:
            self._last_cands = cands
            self._last_text = text or ""

    def _on_test_manual_select(self, candidates, description):
        """测试区手改模式完整链路：主线程弹勾选窗，结果回填 worker（阻塞 worker）。"""
        from src.ui.dialogs.danbooru_select_dialog import DanbooruSelectDialog
        dlg = DanbooruSelectDialog(candidates, description, self)
        if dlg.exec() == QDialog.Accepted:
            result = dlg.selected()
        else:
            result = None
        worker = self.sender()
        if worker is not None:
            worker._manual_select_result = result

    # ---------- 保存 ----------
    def _save(self):
        self.preset.manual_mode = self.rb_manual.isChecked()
        self.preset.allow_nsfw = self.nsfw_cb.isChecked()
        # 召回分类过滤：按勾选状态收集 category int（全未勾选时回退全开，避免召回空集）
        allow_cats = [ci for ci, cb in self.cat_checks.items() if cb.isChecked()]
        self.preset.allow_categories = tuple(allow_cats) if allow_cats else (0, 1, 3, 4, 5)
        self.preset.enable_wiki_fts = self.wiki_fts_cb.isChecked()
        self.preset.recall_top_n = self.recall_spin.value()
        self.preset.negative_prompt = self.neg_edit.toPlainText()
        self.preset.positive_prefix = self.pos_edit.toPlainText()
        self.preset.weight_emb = self.weight_emb_spin.value()
        self.preset.weight_fts = self.weight_fts_spin.value()
        self.preset.weight_wiki = self.weight_wiki_spin.value()
        self.preset.weight_pc = self.weight_pc_spin.value()
        # csv_path 已在 _pick_csv 时更新到 preset 内存对象
        self.storage.save_danbooru_preset(self.preset)
        QMessageBox.information(self, "已保存", "Danbooru 设置已保存。")
        self.accept()


# ============ 后台 Worker ============
class _IndexWorker(QThread):
    """建库工作线程。"""
    progress_signal = Signal(int, int)        # current, total
    finished_signal = Signal(bool, str, int)  # success, message, count

    def __init__(self, service, csv_path, parent=None):
        super().__init__(parent)
        self.service = service
        self.csv_path = csv_path

    def run(self):
        try:
            ok, msg, count = self.service.build_index(
                self.csv_path,
                session_api=None,  # 建库用 MemoryPreset 绑的 API，回退 None
                on_progress=lambda c, t: self.progress_signal.emit(c, t),
            )
        except Exception as e:
            ok, msg, count = False, f"内部错误：{e}", 0
        self.finished_signal.emit(ok, msg, count)


class _TestWorker(QThread):
    """测试链路工作线程，支持单步调试。

    mode:
      - 'recall': 只召回，emit 候选，不调 LLM
      - 'process': 用传入的 cands（上次召回缓存）调 LLM 加工，不召回
      - 'full': 召回 -> 加工；手改模式（preset.manual_mode）时召回后经
        manual_select_request 信号弹勾选窗拿 user_selected 再加工
    finished_signal 回传 (ok, msg, cands, text) 供 UI 缓存召回结果供「测试-加工」复用。
    """
    candidates_signal = Signal(list)    # list[(展示串, category_int)]
    result_signal = Signal(str)         # 加工结果
    finished_signal = Signal(bool, str, object, str)  # ok, message, cands, text
    manual_select_request = Signal(object, str)       # 候选列表, 描述（阻塞式跨线程弹窗）

    def __init__(self, service, storage, text, mode="full",
                 cands=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.storage = storage
        self.text = text
        self.mode = mode
        self._cands_in = cands   # process 模式传入的上次召回候选
        self._manual_select_result = None   # 主线程弹窗回填

    def run(self):
        try:
            preset = self.storage.load_danbooru_preset()
            cands = []
            text = self.text
            if self.mode in ("recall", "full"):
                # 召回（透传 preset 融合权重）
                cands = self.service.recall_candidates(
                    self.text, preset.recall_top_n, preset.allow_nsfw,
                    session_api=None,
                    weights=(preset.weight_emb, preset.weight_fts, preset.weight_wiki, preset.weight_pc),
                    enable_wiki=preset.enable_wiki_fts,
                    allow_categories=preset.allow_categories,
                )
                lines = [
                    (f"{c.name} | {c.cn_name} | pc={c.post_count} "
                     f"| {category_label(c.category)} | score={c.score:.3f} | [{c.src}]",
                     c.category)
                    for c in cands
                ]
                self.candidates_signal.emit(lines)
            elif self.mode == "process":
                # 加工模式复用传入的候选
                cands = list(self._cands_in or [])

            if self.mode == "recall":
                # 只召回，不加工
                self.finished_signal.emit(
                    True, f"召回完成，共 {len(cands)} 个候选", cands, text
                )
                return

            # 加工阶段
            user_selected = None
            if self.mode == "full" and preset.manual_mode:
                # 手改模式完整链路：弹勾选窗拿用户勾选
                user_selected = self._on_manual_select(cands, text)
                if user_selected is None:
                    # 用户取消勾选窗
                    self.result_signal.emit("（已取消，跳过加工）")
                    self.finished_signal.emit(False, "用户取消勾选，已跳过加工", cands, text)
                    return
            positive = self.service.process_to_tags(
                text, cands, user_selected, preset, session_api=None,
            )
            self.result_signal.emit(positive or "（加工失败或返回空，检查 API 配置）")
            if positive:
                self.finished_signal.emit(True, "测试完成", cands, text)
            else:
                self.finished_signal.emit(
                    False, "召回完成但加工失败，请检查 Danbooru 加工 API 配置",
                    cands, text,
                )
        except Exception as e:
            self.finished_signal.emit(False, f"内部错误：{e}", None, "")

    def _on_manual_select(self, candidates, description):
        """worker 线程发起：重置结果 -> emit 阻塞信号等主线程弹窗回填 -> 返回结果。"""
        self._manual_select_result = None
        self.manual_select_request.emit(candidates, description)
        return self._manual_select_result
