"""气泡配色规则编辑对话框。

功能：
  - 规则列表（启用/禁用、上下移动调优先级、增删）
  - 编辑表单（名称/正则/颜色/斜体/作用域/启用/优先级/是否保留匹配标记）
  - 颜色用 QColorDialog 选色，hex 输入框双向同步
  - 实时预览区：用当前编辑中的规则渲染示例文本，所见即所得
  - 正则测试：对预览文本跑当前正则，高亮命中片段
  - 重置默认 / 保存

规则全局生效，保存后调用 markup.set_rules_config + storage 持久化，
并由 MainWindow 触发 chat_view.refresh_all_bubbles 刷新所有气泡。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QComboBox,
    QGroupBox, QSpinBox, QCheckBox, QMessageBox, QColorDialog, QSplitter,
    QWidget,
)

from src.models import (
    RenderRule, RenderRulesConfig,
    SCOPE_AI, SCOPE_USER, SCOPE_ALL, SCOPE_LABELS,
    default_config,
)
from src.utils.markup import render_with_config, test_pattern
import src.utils.markup as markup


# 预览示例文本：覆盖对话/旁白/心声/符号等多种情形
SAMPLE_TEXT = (
    "*轻轻推开门，走了进来*\n"
    "「你醒了吗？今天是周末哦。」\n"
    "（这家伙睡相真差……不过还挺可爱的）\n"
    "\"早上好！\" 她笑着说。\n"
    "*把窗帘拉开，阳光洒进来*\n"
    "『今天的天气真不错』\n"
    "♡ ❤ ★\n"
    "随便说点什么未匹配的旁白文本。"
)


class RenderRulesDialog(QDialog):
    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("气泡配色规则")
        self.resize(1180, 860)
        self.setMinimumSize(1040, 720)
        # 工作副本：编辑期间不直接改全局，保存时才落盘 + set_rules_config
        self._cfg: RenderRulesConfig = storage.load_render_rules()
        self._current: RenderRule | None = None
        self._suppress_change = False  # 防止表单回填时触发预览刷新
        self._build_ui()
        self._load_list()

    # ============ UI ============
    def _build_ui(self):
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Horizontal)

        # 左：规则列表
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.addWidget(QLabel("规则列表（上→下为优先级顺序）"))
        self.rule_list = QListWidget()
        self.rule_list.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.rule_list)
        btns = QHBoxLayout()
        add_btn = QPushButton("+ 新建")
        add_btn.setObjectName("primaryBtn")
        add_btn.clicked.connect(self._on_add)
        up_btn = QPushButton("上移")
        up_btn.clicked.connect(lambda: self._move(-1))
        down_btn = QPushButton("下移")
        down_btn.clicked.connect(lambda: self._move(1))
        del_btn = QPushButton("删除")
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._on_delete)
        btns.addWidget(add_btn)
        btns.addWidget(up_btn)
        btns.addWidget(down_btn)
        btns.addWidget(del_btn)
        ll.addLayout(btns)
        splitter.addWidget(left)

        # 右：编辑表单 + 预览
        right = QWidget()
        rl = QVBoxLayout(right)

        form_group = QGroupBox("规则编辑")
        # [!] 用 QVBoxLayout + 每行一个固定高度 QWidget 容器，彻底解决行间重叠。
        # QGridLayout/QFormLayout/QVBoxLayout 直接放控件时，按 sizeHint 算行位置，
        # 但 QTextEdit 的 sizeHint(192) 与 setFixedHeight(58) 不一致、单行框被 QSS
        # min-height 撑大 sizeHint，导致行位置算错、行间重叠（颜色行压进正则行等）。
        # 解法：每行套一个 setFixedHeight 的 QWidget 容器，QVBoxLayout 只看到一堆
        # 高度确定的容器，行间距完全由 spacing 控制，杜绝跨行重叠。
        # [!] 控件高度需 >= QSS min-height + padding 撑出的值，否则 setFixedHeight 被
        # QSS 覆盖：QLineEdit/QComboBox 32、QSpinBox 36（有上下按钮）。
        ROW_H = 32
        SPIN_H = 36
        PATTERN_H = 58
        form = QVBoxLayout(form_group)
        form.setSpacing(10)
        form.setContentsMargins(12, 10, 12, 10)
        LABEL_W = 56  # 标签列固定宽，对齐

        def _mk_row_container(height: int, label_text: str = "") -> tuple:
            """创建固定高度的行容器，返回 (container, inner_layout)。

            inner_layout 内已放好标签（或占位间距），调用方往里加字段控件。
            """
            container = QWidget()
            container.setFixedHeight(height)
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            if label_text:
                lbl = QLabel(label_text)
                lbl.setFixedWidth(LABEL_W)
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                row.addWidget(lbl)
            else:
                row.addSpacing(LABEL_W)
            return container, row

        # 名称
        c, row = _mk_row_container(ROW_H, "名称:")
        self.name_edit = QLineEdit()
        self.name_edit.setFixedHeight(ROW_H)
        row.addWidget(self.name_edit)
        form.addWidget(c)

        # 正则
        c, row = _mk_row_container(PATTERN_H, "正则:")
        self.pattern_edit = QTextEdit()
        # setFixedHeight 让实际高度=容器高度（QSS 不给 QTextEdit 设 min-height，生效）
        self.pattern_edit.setFixedHeight(PATTERN_H)
        self.pattern_edit.setPlaceholderText('正则表达式，如 「[^」]*」')
        row.addWidget(self.pattern_edit)
        form.addWidget(c)

        # 颜色：色块按钮 + hex 输入
        c, row = _mk_row_container(ROW_H, "颜色:")
        self.color_btn = QPushButton()
        # [!] 按钮高度必须 = 容器高，否则 QSS 的 QPushButton min-height+padding 会把它
        # 撑到 34（实测 setFixedSize(40,24) 被 QSS 覆盖为 34），溢出 32 高的容器，
        # 导致 QVBoxLayout 行位置计算错乱、压进上一行。
        self.color_btn.setFixedSize(40, ROW_H)
        self.color_btn.clicked.connect(self._pick_color)
        self.color_edit = QLineEdit()
        self.color_edit.setFixedHeight(ROW_H)
        self.color_edit.setMaximumWidth(120)
        self.color_edit.setPlaceholderText("#hex")
        self.color_edit.editingFinished.connect(self._on_hex_changed)
        row.addWidget(self.color_btn)
        row.addWidget(self.color_edit)
        row.addStretch()
        form.addWidget(c)

        # 斜体
        c, row = _mk_row_container(ROW_H, "")
        self.italic_chk = QCheckBox("斜体")
        row.addWidget(self.italic_chk)
        row.addStretch()
        form.addWidget(c)

        # 保留匹配标记
        c, row = _mk_row_container(ROW_H, "")
        self.keep_marks_chk = QCheckBox("保留匹配标记（如引号/括号）；取消则去掉首尾各一字符")
        row.addWidget(self.keep_marks_chk)
        row.addStretch()
        form.addWidget(c)

        # 作用域
        c, row = _mk_row_container(ROW_H, "作用域:")
        self.scope_combo = QComboBox()
        self.scope_combo.setFixedHeight(ROW_H)
        self.scope_combo.addItem(SCOPE_LABELS[SCOPE_ALL], SCOPE_ALL)
        self.scope_combo.addItem(SCOPE_LABELS[SCOPE_AI], SCOPE_AI)
        self.scope_combo.addItem(SCOPE_LABELS[SCOPE_USER], SCOPE_USER)
        row.addWidget(self.scope_combo)
        row.addStretch()
        form.addWidget(c)

        # 优先级
        c, row = _mk_row_container(SPIN_H, "优先级:")
        self.priority_spin = QSpinBox()
        self.priority_spin.setFixedHeight(SPIN_H)
        self.priority_spin.setRange(0, 9999)
        self.priority_spin.setValue(100)
        self.priority_spin.setToolTip("数字小的先匹配；同等优先级按列表顺序")
        row.addWidget(self.priority_spin)
        row.addStretch()
        form.addWidget(c)

        # 启用
        c, row = _mk_row_container(ROW_H, "")
        self.enabled_chk = QCheckBox("启用")
        self.enabled_chk.setChecked(True)
        row.addWidget(self.enabled_chk)
        row.addStretch()
        form.addWidget(c)
        form.addStretch()

        # [!] form_group 设 minimumHeight：右侧 rl(QVBoxLayout) 把 preview_group
        # 设了 stretch=1 拿大头，form_group 默认 stretch=0 会被压到 sizeHint 以下
        # （sizeHint 542 但实际只给 378），内部 QVBoxLayout 被迫挤压行间距/spacing
        # 导致行间重叠。设 minimumHeight 让 form_group 至少拿到 sizeHint 的空间。
        form_group.setMinimumHeight(form_group.sizeHint().height())

        rl.addWidget(form_group)

        # 默认色（未命中文本）
        default_group = QGroupBox("默认色（未命中任何规则的文本）")
        dl = QHBoxLayout(default_group)
        dl.addWidget(QLabel("AI气泡:"))
        self.ai_default_btn = QPushButton()
        self.ai_default_btn.setFixedSize(40, ROW_H)
        self.ai_default_btn.clicked.connect(lambda: self._pick_default("ai"))
        self.ai_default_edit = QLineEdit()
        self.ai_default_edit.setFixedHeight(ROW_H)
        self.ai_default_edit.setMaximumWidth(90)
        self.ai_default_edit.editingFinished.connect(lambda: self._on_default_hex("ai"))
        dl.addWidget(self.ai_default_btn)
        dl.addWidget(self.ai_default_edit)
        dl.addSpacing(12)
        dl.addWidget(QLabel("用户气泡:"))
        self.user_default_btn = QPushButton()
        self.user_default_btn.setFixedSize(40, ROW_H)
        self.user_default_btn.clicked.connect(lambda: self._pick_default("user"))
        self.user_default_edit = QLineEdit()
        self.user_default_edit.setFixedHeight(ROW_H)
        self.user_default_edit.setMaximumWidth(90)
        self.user_default_edit.editingFinished.connect(lambda: self._on_default_hex("user"))
        dl.addWidget(self.user_default_btn)
        dl.addWidget(self.user_default_edit)
        dl.addStretch()
        rl.addWidget(default_group)

        # 预览区
        preview_group = QGroupBox("实时预览")
        pl = QVBoxLayout(preview_group)
        pl.setSpacing(8)
        pl.addWidget(QLabel("示例文本（可编辑）："))
        self.sample_edit = QTextEdit()
        self.sample_edit.setPlainText(SAMPLE_TEXT)
        # [!] 用 setFixedHeight 而非 setMinimumHeight：QTextEdit sizeHint(192) 远大于
        # minHeight(120)，QVBoxLayout 按 sizeHint 算位置会与下方「预览视角」行重叠。
        # setFixedHeight 让 sizeHint 与实际高度一致（QSS 不给 QTextEdit 设 min-height，生效）。
        self.sample_edit.setFixedHeight(120)
        pl.addWidget(self.sample_edit)
        # 切换预览气泡视角
        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 4, 0, 4)  # 上下边距，隔开示例文本与预览视角行
        view_row.addWidget(QLabel("预览视角:"))
        self.view_combo = QComboBox()
        self.view_combo.setFixedHeight(ROW_H)
        self.view_combo.addItem("AI 气泡", SCOPE_AI)
        self.view_combo.addItem("用户气泡", SCOPE_USER)
        self.view_combo.currentIndexChanged.connect(self._refresh_preview)
        view_row.addWidget(self.view_combo)
        view_row.addStretch()
        test_btn = QPushButton("测试当前正则")
        test_btn.setFixedHeight(ROW_H)
        test_btn.clicked.connect(self._on_test_pattern)
        view_row.addWidget(test_btn)
        pl.addLayout(view_row)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextFormat(Qt.RichText)
        self.preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pl.addWidget(self.preview_label)
        self.test_result_label = QLabel()
        self.test_result_label.setWordWrap(True)
        self.test_result_label.setTextFormat(Qt.RichText)
        pl.addWidget(self.test_result_label)
        rl.addWidget(preview_group, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        # 底部按钮
        bottom = QHBoxLayout()
        reset_btn = QPushButton("重置默认")
        reset_btn.clicked.connect(self._on_reset)
        bottom.addWidget(reset_btn)
        bottom.addStretch()
        ok_btn = QPushButton("保存")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(ok_btn)
        bottom.addWidget(cancel_btn)
        layout.addLayout(bottom)

        # 表单变更信号 → 刷新预览
        self.name_edit.textChanged.connect(self._on_form_changed)
        self.pattern_edit.textChanged.connect(self._on_form_changed)
        self.color_edit.editingFinished.connect(self._on_form_changed)
        self.italic_chk.stateChanged.connect(self._on_form_changed)
        self.keep_marks_chk.stateChanged.connect(self._on_form_changed)
        self.scope_combo.currentIndexChanged.connect(self._on_form_changed)
        self.priority_spin.valueChanged.connect(self._on_form_changed)
        self.enabled_chk.stateChanged.connect(self._on_form_changed)
        self.sample_edit.textChanged.connect(self._refresh_preview)
        self._apply_default_color_btns()

    # ============ 列表加载 ============
    def _load_list(self):
        self.rule_list.clear()
        # 按 priority 升序展示
        rules = sorted(self._cfg.rules, key=lambda r: r.priority)
        for r in rules:
            tag = "" if r.enabled else "  [禁用]"
            item = QListWidgetItem(f"{r.priority:>4}  {r.name}{tag}")
            item.setData(Qt.UserRole, r.id)
            self.rule_list.addItem(item)
        if rules:
            self.rule_list.setCurrentRow(0)
        else:
            self._current = None
            self._clear_form()

    def _on_select(self, current, _previous):
        if not current:
            self._current = None
            return
        rid = current.data(Qt.UserRole)
        rule = next((r for r in self._cfg.rules if r.id == rid), None)
        if not rule:
            return
        self._current = rule
        self._fill_form(rule)

    def _fill_form(self, rule: RenderRule):
        self._suppress_change = True
        self.name_edit.setText(rule.name)
        self.pattern_edit.setPlainText(rule.pattern)
        self.color_edit.setText(rule.color)
        self._apply_color_btn(rule.color)
        self.italic_chk.setChecked(rule.italic)
        self.keep_marks_chk.setChecked(rule.keep_marks)
        idx = self.scope_combo.findData(rule.scope)
        self.scope_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.priority_spin.setValue(rule.priority)
        self.enabled_chk.setChecked(rule.enabled)
        self._suppress_change = False
        self._refresh_preview()

    def _clear_form(self):
        self._suppress_change = True
        self.name_edit.clear()
        self.pattern_edit.clear()
        self.color_edit.setText("#c0caf5")
        self._apply_color_btn("#c0caf5")
        self.italic_chk.setChecked(False)
        self.keep_marks_chk.setChecked(True)
        self.scope_combo.setCurrentIndex(0)
        self.priority_spin.setValue(100)
        self.enabled_chk.setChecked(True)
        self._suppress_change = False

    # ============ 表单变更 → 回写当前规则 + 刷新预览 ============
    def _on_form_changed(self):
        if self._suppress_change or not self._current:
            return
        self._apply_form_to_rule(self._current)
        self._refresh_preview()

    def _apply_form_to_rule(self, rule: RenderRule):
        rule.name = self.name_edit.text().strip() or "未命名"
        rule.pattern = self.pattern_edit.toPlainText()
        rule.color = self.color_edit.text().strip() or rule.color
        rule.italic = self.italic_chk.isChecked()
        rule.keep_marks = self.keep_marks_chk.isChecked()
        rule.scope = self.scope_combo.currentData()
        rule.priority = self.priority_spin.value()
        rule.enabled = self.enabled_chk.isChecked()

    # ============ 增删移动 ============
    def _on_add(self):
        # 新规则优先级取当前最大+10
        max_pri = max((r.priority for r in self._cfg.rules), default=0)
        rule = RenderRule(
            name="新规则", pattern="", color="#e0af68",
            enabled=True, priority=max_pri + 10, scope=SCOPE_ALL, keep_marks=True,
        )
        self._cfg.rules.append(rule)
        self._load_list()
        # 选中新建
        for i in range(self.rule_list.count()):
            if self.rule_list.item(i).data(Qt.UserRole) == rule.id:
                self.rule_list.setCurrentRow(i)
                break

    def _on_delete(self):
        if not self._current:
            return
        reply = QMessageBox.question(
            self, "确认", f"删除规则「{self._current.name}」？"
        )
        if reply != QMessageBox.Yes:
            return
        self._cfg.rules = [r for r in self._cfg.rules if r.id != self._current.id]
        self._current = None
        self._load_list()

    def _move(self, delta: int):
        """上移/下移：调整选中规则的 priority 与相邻规则交换。"""
        if not self._current:
            return
        rules = sorted(self._cfg.rules, key=lambda r: r.priority)
        idx = next((i for i, r in enumerate(rules) if r.id == self._current.id), None)
        if idx is None:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(rules):
            return
        # 交换两者的 priority
        rules[idx].priority, rules[new_idx].priority = (
            rules[new_idx].priority, rules[idx].priority
        )
        self._load_list()
        # 重新选中
        for i in range(self.rule_list.count()):
            if self.rule_list.item(i).data(Qt.UserRole) == self._current.id:
                self.rule_list.setCurrentRow(i)
                break

    # ============ 颜色 ============
    def _apply_color_btn(self, hex_color: str):
        c = QColor(hex_color) if QColor(hex_color).isValid() else QColor("#888888")
        self.color_btn.setStyleSheet(
            f"background-color:{hex_color}; border:1px solid #333;"
        )

    def _pick_color(self):
        initial = QColor(self.color_edit.text() or "#c0caf5")
        c = QColorDialog.getColor(initial, self, "选择颜色")
        if c.isValid():
            self.color_edit.setText(c.name())
            self._apply_color_btn(c.name())
            self._on_form_changed()

    def _on_hex_changed(self):
        hex_color = self.color_edit.text().strip()
        if QColor(hex_color).isValid():
            self._apply_color_btn(hex_color)
            self._on_form_changed()

    # 默认色按钮
    def _apply_default_color_btns(self):
        self._apply_default_btn(self.ai_default_btn, self._cfg.ai_default_color)
        self.ai_default_edit.setText(self._cfg.ai_default_color)
        self._apply_default_btn(self.user_default_btn, self._cfg.user_default_color)
        self.user_default_edit.setText(self._cfg.user_default_color)

    def _apply_default_btn(self, btn, hex_color):
        btn.setStyleSheet(f"background-color:{hex_color}; border:1px solid #333;")

    def _pick_default(self, which: str):
        cur = self._cfg.ai_default_color if which == "ai" else self._cfg.user_default_color
        c = QColorDialog.getColor(QColor(cur), self, "选择默认色")
        if c.isValid():
            if which == "ai":
                self._cfg.ai_default_color = c.name()
            else:
                self._cfg.user_default_color = c.name()
            self._apply_default_color_btns()
            self._refresh_preview()

    def _on_default_hex(self, which: str):
        edit = self.ai_default_edit if which == "ai" else self.user_default_edit
        hex_color = edit.text().strip()
        if QColor(hex_color).isValid():
            if which == "ai":
                self._cfg.ai_default_color = hex_color
            else:
                self._cfg.user_default_color = hex_color
            self._apply_default_color_btns()
            self._refresh_preview()

    # ============ 预览 ============
    def _build_preview_config(self) -> RenderRulesConfig:
        """构造预览用的配置（含当前编辑中的规则）。"""
        # 若有当前规则，把表单值同步进去
        if self._current:
            self._apply_form_to_rule(self._current)
        return self._cfg

    def _refresh_preview(self):
        cfg = self._build_preview_config()
        is_user = self.view_combo.currentData() == SCOPE_USER
        text = self.sample_edit.toPlainText()
        self.preview_label.setText(render_with_config(text, is_user, cfg))

    def _on_test_pattern(self):
        """测试当前正则：对示例文本跑一次，高亮命中片段。"""
        pattern = self.pattern_edit.toPlainText()
        text = self.sample_edit.toPlainText()
        if not pattern:
            self.test_result_label.setText("（请先填写正则）")
            return
        hits = test_pattern(pattern, text)
        if not hits:
            # 区分「无命中」和「正则非法」
            try:
                import re
                re.compile(pattern)
                self.test_result_label.setText(
                    '<span style="color:#e0af68;">正则合法，但示例文本中无命中。</span>'
                )
            except re.error as e:
                self.test_result_label.setText(
                    f'<span style="color:#f7768e;">正则非法：{e}</span>'
                )
            return
        # 高亮命中片段
        import html as _html
        out: list[str] = []
        pos = 0
        for start, end, matched in hits:
            if start > pos:
                out.append(_html.escape(text[pos:start]))
            out.append(
                f'<span style="background-color:#f7768e;color:#1a1b26;">'
                f'{_html.escape(matched)}</span>'
            )
            pos = end
        if pos < len(text):
            out.append(_html.escape(text[pos:]))
        count = len(hits)
        self.test_result_label.setText(
            f'<span style="color:#9ece6a;">命中 {count} 处：</span><br>'
            + "".join(out).replace("\n", "<br>")
        )

    # ============ 重置 / 保存 ============
    def _on_reset(self):
        reply = QMessageBox.question(
            self, "重置默认",
            "恢复为默认规则集？当前所有自定义规则将被覆盖。"
        )
        if reply != QMessageBox.Yes:
            return
        self._cfg = default_config()
        self._current = None
        self._load_list()
        self._apply_default_color_btns()
        self._refresh_preview()

    def _on_save(self):
        # 同步当前编辑中的规则
        if self._current:
            self._apply_form_to_rule(self._current)
        # 校验：空 pattern 的启用规则提示
        bad = [r for r in self._cfg.rules if r.enabled and not r.pattern.strip()]
        if bad:
            QMessageBox.warning(
                self, "提示",
                f"有 {len(bad)} 条已启用规则的正则为空，请填写正则或禁用该规则。"
            )
            return
        self.storage.save_render_rules(self._cfg)
        markup.set_rules_config(self._cfg)
        QMessageBox.information(self, "已保存", "配色规则已保存。")
        self.accept()
