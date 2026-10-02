"""世界模拟设置对话框：配置 WorldSimPreset（仿 TtsSettingsDialog）。

含：结算 API / 叙事 API 绑定、骨架生成提示词、生成参数、模拟旋钮、生图勾选清单、世界生成默认。
[!] 保存时先 load 再改字段（守 §15 AppConfig 保留字段模式），用 QMessageBox.information 反馈。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox, QTabWidget,
    QScrollArea, QFrame, QMessageBox, QWidget, QGroupBox,
)

from src.models import WorldSimPreset, CUTOUT_BG_COLORS, CUTOUT_BG_LABELS


def _api_combo(storage, current_id: str) -> QComboBox:
    """构建 API 选择下拉（仿 tts_dialog.py:586-590）。"""
    combo = QComboBox()
    combo.addItem("默认（回退首个启用 API）", "")
    for api in storage.load_all_apis():
        combo.addItem(api.name, api.id)
    idx = combo.findData(current_id)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    return combo


def _spin_float(val: float, lo=0.0, hi=2.0, step=0.1) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(2)
    s.setValue(val)
    return s


def _spin_int(val: int, lo=1, hi=32768, step=1) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(val)
    return s


# 四档强度枚举的中文显示（data 仍是英文 key，_on_save 读 currentData 保存口径不变）
_LEVEL_ZH = (("off", "关闭"), ("light", "轻度"), ("medium", "中度"), ("heavy", "重度"))


def _level_combo(cur: str, tip: str) -> QComboBox:
    """构建四档强度下拉（中文显示 + 英枚举 data + tooltip）。"""
    combo = QComboBox()
    for key, zh in _LEVEL_ZH:
        combo.addItem(f"{zh}（{key}）", key)
    idx = combo.findData(cur)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.setToolTip(tip)
    return combo


class WorldSimSettingsDialog(QDialog):
    """世界模拟全局预设配置。"""

    def __init__(self, storage, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("世界模拟设置")
        self.resize(780, 700)
        self.setMinimumSize(700, 580)

        outer = QVBoxLayout(self)
        from src.ui.widgets.game_widgets import page_header
        outer.addWidget(page_header("世界模拟设置"))
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        p = storage.load_world_sim_preset()
        self._p = p

        # ---- Tab 1: LLM 角色 ----
        tabs.addTab(self._wrap(self._build_llm_tab(p)), "LLM 角色")
        # ---- Tab 2: 模拟旋钮 ----
        tabs.addTab(self._wrap(self._build_sim_tab(p)), "模拟与数值")
        # ---- Tab 3: 生图 ----
        tabs.addTab(self._wrap(self._build_image_tab(p)), "生图")
        # ---- Tab 4: 世界生成默认 ----
        tabs.addTab(self._wrap(self._build_gen_tab(p)), "世界生成默认")

        # 底部
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(16, 8, 16, 10)
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        outer.addLayout(btn_row)

    @staticmethod
    def _wrap(widget: QWidget) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        s.setFrameShape(QFrame.NoFrame)
        s.setWidget(widget)
        return s

    def _build_llm_tab(self, p: WorldSimPreset) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)

        # [P5] 结算 LLM API 分组（QGroupBox 蓝标题视觉分组）
        calc_box = QGroupBox("结算 LLM (骨架生成 / 数值结算)")
        calc_box.setFlat(True)
        calc_box.setToolTip("负责「动脑子算数」的模型：生成世界骨架、把玩家的行动结算成数值结果。\n建议配强模型（需稳定输出 JSON），与负责写作的叙事 LLM 分开可省钱。")
        calc_inner = QHBoxLayout(calc_box)
        calc_inner.setContentsMargins(8, 6, 8, 0)
        calc_inner.addStretch(1)
        form.addRow(calc_box)

        self.calc_api = _api_combo(self.storage, p.calculator_api_id)
        self.calc_api.setToolTip("用于世界骨架生成与数值结算的 LLM（推荐贵/强模型，需支持 JSON 输出）。空=自动选首个 enabled API。")
        form.addRow("结算 LLM API:", self.calc_api)
        hint1 = QLabel("用于世界骨架生成 / 数值结算（贵/强模型，JSON 输出）。空=回退首个启用 API。")
        hint1.setWordWrap(True)
        hint1.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", hint1)

        self.calc_temp = _spin_float(p.calculator_temperature, 0, 2, 0.1)
        self.calc_temp.setToolTip("LLM 采样温度（0-2）。0=完全确定性（总选最高概率 token）；1=均衡；2=发散。骨架生成建议 0.3-0.7。")
        form.addRow("结算 temperature:", self.calc_temp)
        self.calc_max = _spin_int(p.calculator_max_tokens, 10000, 100000, 256)
        self.calc_max.setToolTip("LLM 单次回复最大 token 数（下限 10000，输出侧支持 1-2 万）。骨架生成 JSON 输出大，太小会被截断致解析失败。")
        form.addRow("结算 max_tokens:", self.calc_max)
        self.calc_top_p = _spin_float(p.calculator_top_p, 0, 1, 0.05)
        self.calc_top_p.setToolTip("核采样阈值（0-1）。仅从累计概率达此值的候选 token 中采样。0.1=保守；1.0=全候选。建议 0.9。")
        form.addRow("结算 top_p:", self.calc_top_p)

        # [P5] 叙事 LLM API 分组
        narr_box = QGroupBox("叙事 LLM (NPC 对话 / 旁白 / 场景叙事)")
        narr_box.setFlat(True)
        narr_box.setToolTip("负责「写作」的模型：场景旁白、NPC 台词、私聊回复（流式输出）。\n便宜/快的模型即可，写出风格靠下面的提示词调。")
        narr_inner = QHBoxLayout(narr_box)
        narr_inner.setContentsMargins(8, 6, 8, 0)
        narr_inner.addStretch(1)
        form.addRow(narr_box)

        self.narr_api = _api_combo(self.storage, p.narrative_api_id)
        self.narr_api.setToolTip("用于 NPC 对话 / 旁白 / 场景叙事的 API（流式输出，便宜/快模型即可）。空=回退结算 API。")
        form.addRow("叙事 LLM API:", self.narr_api)
        hint2 = QLabel("用于 NPC 对话 / 旁白 / 场景叙事（便宜/快模型，流式）。P2 场景交互循环用。")
        hint2.setWordWrap(True)
        hint2.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", hint2)

        self.narr_temp = _spin_float(p.narrative_temperature, 0, 2, 0.1)
        self.narr_temp.setToolTip("叙事采样温度（0-2）。建议 0.8-1.2，温度越高 NPC 措辞越有性格、越不可控。")
        form.addRow("叙事 temperature:", self.narr_temp)
        self.narr_max = _spin_int(p.narrative_max_tokens, 10000, 100000, 256)
        self.narr_max.setToolTip("单次叙事回复 token 上限（下限 10000）。旁白 300-500 字/段，重大时刻多段长文不被截断。")
        form.addRow("叙事 max_tokens:", self.narr_max)
        self.narr_top_p = _spin_float(p.narrative_top_p, 0, 1, 0.05)
        self.narr_top_p.setToolTip("叙事核采样阈值（0-1）。建议 0.9-0.95。")
        form.addRow("叙事 top_p:", self.narr_top_p)

        # [P5] 提示词分组
        prompt_box = QGroupBox("系统提示词")
        prompt_box.setFlat(True)
        prompt_box.setToolTip("两段提示词分别约束「玩家行动怎么结算」「旁白怎么写」。\n清空某段=恢复默认（改过想还原就全选删除）。")
        prompt_inner = QHBoxLayout(prompt_box)
        prompt_inner.setContentsMargins(8, 6, 8, 0)
        prompt_inner.addStretch(1)
        form.addRow(prompt_box)

        form.addRow(QLabel("场景结算提示词:"))
        self.settle_prompt = QTextEdit()
        self.settle_prompt.setPlainText(p.settle_system_prompt)
        self.settle_prompt.setMinimumHeight(140)
        self.settle_prompt.setToolTip("每回合把玩家的行动交给结算 LLM 时的系统提示词：指导它把「我攻击强盗」解析成意图 JSON（交给引擎算数值）。\n包含输出格式约定，改动可能破坏结算，谨慎。")
        form.addRow(self.settle_prompt)

        form.addRow(QLabel("场景叙事提示词:"))
        self.narr_prompt = QTextEdit()
        self.narr_prompt.setPlainText(p.narrative_system_prompt)
        self.narr_prompt.setMinimumHeight(120)
        self.narr_prompt.setToolTip("叙事 LLM 写旁白时的系统提示词：控制文风、视角、篇幅。\n想换文风（更文艺/更爽文/第一人称）在这里改。")
        form.addRow(self.narr_prompt)

        # [P19] 文风层（与规则层解耦，可清空=不注入）
        form.addRow(QLabel("叙事文风偏好:"))
        self.style_prompt = QTextEdit()
        self.style_prompt.setPlainText(p.narrative_style_prompt)
        self.style_prompt.setMinimumHeight(110)
        self.style_prompt.setToolTip(
            "追加在叙事规则之后的文风偏好（各题材语体/用词口味/节奏）。\n"
            "清空 = 不注入任何文风约束，旁白只按规则层写。\n"
            "与上面的叙事提示词分工：规则层管「什么能写什么不能写」，这里管「怎么写好看」。")
        form.addRow(self.style_prompt)
        return w

    def _build_sim_tab(self, p: WorldSimPreset) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)

        self.sim_enabled = QCheckBox("启用世界滴答（玩家行动后世界推进）")
        self.sim_enabled.setChecked(p.sim_enabled)
        self.sim_enabled.setToolTip("关闭后玩家行动不再触发世界推进（NPC 不动、商店不补货、势力不战）。关=静态世界，开=活世界。")
        form.addRow("", self.sim_enabled)

        self.economy = _level_combo(
            p.economy_sim, "世界每推进一回合时，物价和 NPC 财富的变化强度。\n"
            "关闭=价格财富永远不变；开得越大，物品价格涨跌越频繁（可刷差价也更不稳定）。")
        form.addRow("经济模拟强度:", self.economy)

        # [饱食度 2026-09-06 / 2026-09-08 食堂已删]
        self.hunger_enabled = QCheckBox("启用饱食度（食物店 + 每日衰减）")
        self.hunger_enabled.setChecked(p.hunger_enabled)
        self.hunger_enabled.setToolTip(
            "开启后：题材食物入世（食物店/游商有售，吃食物恢复饱食度）；\n"
            "玩家与 NPC 每日饱食度下降（约 3 天见底）。玩家饱食度低于 30 全属性减半\n"
            "（吃食物即恢复）；NPC 饿了会自己吃随身食物或去商店买食物。关闭则一切保持现状。")
        form.addRow("", self.hunger_enabled)

        # [自然恢复 2026-09-06 用户指示] 每日跨日自然回血
        self.daily_hp_regen = _spin_int(p.daily_hp_regen_pct, 0, 100, 5)
        self.daily_hp_regen.setToolTip(
            "每过一天（跨日结算时）玩家与存活 NPC 自然恢复最大生命的百分比。"
            "默认 30；0=关闭自然恢复。向上取整、不超上限；离线跳天只按 1 日回。")
        form.addRow("每日自然恢复生命%(0=关):", self.daily_hp_regen)

        # [P35] 种植系统
        self.farm_enabled = QCheckBox("启用种植系统（灵田建筑 + 种子/作物入世 + 每日生长）")
        self.farm_enabled.setChecked(p.farm_enabled)
        self.farm_enabled.setToolTip("住宅可建灵田（garden 建筑，等级=地块数）：播种后浇水促长、雨天免浇，收获作物可炼丹或出售，低概率种出变异高产株。关闭则种子不入世、灵田不可用。")
        form.addRow("", self.farm_enabled)

        self.farm_wither = _spin_int(p.farm_wither_days, 0, 10, 1)
        self.farm_wither.setToolTip("作物连续多少天既没浇水也不是雨天会枯萎（清空地块）。0=永不枯萎（挂机友好）。")
        form.addRow("作物枯萎天数(0=不枯萎):", self.farm_wither)

        self.faction_war = _level_combo(
            p.faction_war, "不同势力（门派/国家/帮派）互相攻打夺地的频率。\n"
            "关闭=世界永远和平；开得越大，地盘易主和战事事件越频繁（重要剧情 NPC 永不死亡）。")
        form.addRow("势力战强度:", self.faction_war)

        self.offscreen = QCheckBox("离屏 NPC 演化（屏幕外 NPC 自行行动）")
        self.offscreen.setChecked(p.offscreen_npc_tick)
        self.offscreen.setToolTip("开启后玩家不在场时 NPC 也按日程行动（巡逻/开店/移动）。关=NPC 等玩家到场才动。")
        form.addRow("", self.offscreen)

        self.reconcile = _spin_int(p.reconcile_interval, 0, 200, 1)
        self.reconcile.setToolTip("每 N 回合调一次世界校准 LLM 修复叙事不一致与阵营漂移。0=关闭（长局可能累积矛盾）。")
        form.addRow("世界校准间隔(回合,0=关):", self.reconcile)

        self.budget = _spin_int(p.sim_budget_per_tick, 1, 100, 1)
        self.budget.setToolTip("单次世界滴答最多调几次 LLM（要角决策 + 世界校准共用此预算）。值大=世界更鲜活但更贵。")
        form.addRow("每滴答 LLM 调用预算:", self.budget)

        self.combat = QComboBox()
        self.combat.addItem("叙事战斗（narrative，一回合文字定胜负，无数值）", "narrative")
        self.combat.addItem("数值战斗（crpg，回合制 HP/命中/暴击/技能）", "crpg")
        self.combat.setCurrentIndex(self.combat.findData(p.combat_system))
        self.combat.setToolTip("战斗怎么打：\n叙事=像小说一样一回合文字决出胜负，没有血条数字；\n数值=回合制 RPG 战斗，有 HP/属性/命中/暴击/技能/掉落，可操作。")
        form.addRow("战斗系统:", self.combat)

        self.granularity = QComboBox()
        self.granularity.addItem("轻量（light，必中无暴击）", "light")
        self.granularity.addItem("标准（medium，带抗性减免+暴击）", "medium")
        self.granularity.addItem("硬核（heavy，加命中/闪避/招架判定）", "heavy")
        self.granularity.setCurrentIndex(self.granularity.findData(p.crpg_granularity))
        self.granularity.setToolTip("数值战斗的判定精细度（战斗系统选 crpg 时生效）：\n轻量=攻击必中、无暴击，节奏快；\n标准=有抗性减免和暴击；\n硬核=再加命中/闪避/招架，最像硬核 RPG。")
        form.addRow("CRPG 数值粒度:", self.granularity)

        self.difficulty = QComboBox()
        self.difficulty.addItem("简单（easy，无修正，等同一般游戏普通难度）", "easy")
        self.difficulty.addItem("普通（normal，玩家伤害 x0.85 / 受击 x1.2）", "normal")
        self.difficulty.addItem("困难（hard，玩家伤害 x0.7 / 受击 x1.4）", "hard")
        self.difficulty.setCurrentIndex(self.difficulty.findData(p.difficulty))
        self.difficulty.setToolTip("全局难度：整体已上调一档（简单=一般游戏普通难度，普通=困难，困难=更困难）。括号内是实际数值倍率。")
        form.addRow("难度:", self.difficulty)

        # ---- P4 世界滴答细分 ----
        # [P5] 用 QGroupBox 替代文字分隔：仿主程序 QGroupBox 样式（蓝标题 + 圆角边框），
        # setFlat(True) 弱化边框避免与输入框视觉冲突。
        # 注：QFormLayout 不支持嵌套 addRow(跨多字段的 group widget)，故用 QLabel
        # 嵌入 QGroupBox 间接做标题行；下方字段仍 addRow 到外层 form。
        tick_box = QGroupBox("世界滴答（离屏世界演化）")
        tick_box.setFlat(True)
        tick_box.setToolTip("「世界滴答」=你每行动一回合，你看不到的地方也在变化：NPC 各干各的、势力打仗、物价波动。\n本分区控制这个幕后世界演化的一堆开关和强度。")
        tick_inner = QHBoxLayout(tick_box)
        tick_inner.setContentsMargins(8, 6, 8, 0)
        tick_inner.addStretch(1)
        form.addRow(tick_box)
        # 隐藏 QGroupBox 默认 layout（仅保留边框 + 标题），下方字段继续 addRow 到外层。
        # 简化：QGroupBox 仅作为「分割视觉占位」，用其标题文字传达分组语义；
        # 实际字段仍在外层 form（避免 QFormLayout 嵌套带来的字段对齐不一致）。

        self.econ_vol = _level_combo(
            p.economy_volatility, "每次经济模拟时价格波动的幅度。\n"
            "关闭=价格不动；开得越大单次涨跌越猛（重度可能出现物价暴涨暴跌）。")
        form.addRow("经济波动幅度:", self.econ_vol)

        self.fw_lethal = _level_combo(
            p.faction_war_lethality, "势力开战时，双方杂兵 NPC 的死亡率。\n"
            "关闭=只抢地盘不死人；开得越大战场伤亡越重（重要剧情 NPC 永不死亡）。")
        form.addRow("势力战致命度:", self.fw_lethal)

        self.event_log_max = _spin_int(p.event_log_max, 10, 2000, 10)
        self.event_log_max.setToolTip("「世界大事记」最多保留多少条事件记录，超出丢最旧的。\n"
                                      "日志在详情页/图鉴里查看，调大更全但更占内存。")
        form.addRow("事件日志上限:", self.event_log_max)

        self.max_events = _spin_int(p.max_events_per_tick, 0, 20, 1)
        self.max_events.setToolTip("世界每推进一回合最多记录几条事件（0=不限制）。超出按重要程度截断。")
        form.addRow("单回合事件上限:", self.max_events)

        self.key_npc_enabled = QCheckBox("要角 NPC 走 LLM 决策（关闭则全走规则）")
        self.key_npc_enabled.setChecked(p.key_npc_decision_enabled)
        self.key_npc_enabled.setToolTip("「要角」=剧情重要 NPC（首领/长老等）。\n"
                                        "开启=他们由 LLM 思考后自主行动（更智能但每回合花 token）；\n"
                                        "关闭=所有 NPC 都按固定规则行动（省钱）。")
        form.addRow("", self.key_npc_enabled)

        self.key_npc_budget = _spin_int(p.key_npc_budget, 0, 20, 1)
        self.key_npc_budget.setToolTip("每回合最多让几个要角 NPC 走 LLM 决策（0=全不走，等于只按规则行动）。\n另受上方「每滴答 LLM 调用预算」总预算双重约束。")
        form.addRow("要角决策预算(名/回合):", self.key_npc_budget)

        self.sim_api = _api_combo(self.storage, p.sim_api_id)
        self.sim_api.setToolTip("专门给「要角 NPC 决策 / 世界校准」用的 LLM API。\n空=回退结算 LLM API，再回退首个启用 API。")
        form.addRow("滴答 LLM API:", self.sim_api)
        hint_sim = QLabel("用于要角 NPC 决策 / 世界校准。空=回退结算 LLM API。")
        hint_sim.setWordWrap(True)
        hint_sim.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", hint_sim)

        self.sim_temp = _spin_float(p.sim_temperature, 0, 2, 0.1)
        self.sim_temp.setToolTip("要角决策 LLM 的采样温度（0-2）。低=行为稳定保守；高=行为更放飞。建议 0.6-0.9。")
        form.addRow("滴答 temperature:", self.sim_temp)
        self.sim_max = _spin_int(p.sim_max_tokens, 10000, 100000, 256)
        self.sim_max.setToolTip("要角决策 LLM 单次回复的 token 上限（下限 10000）。决策 JSON 本身短，下限只作护栏。")
        form.addRow("滴答 max_tokens:", self.sim_max)

        form.addRow(QLabel("滴答提示词（要角决策 + 世界校准）:"))
        self.sim_prompt = QTextEdit()
        self.sim_prompt.setPlainText(p.sim_system_prompt)
        self.sim_prompt.setMinimumHeight(120)
        self.sim_prompt.setToolTip("要角 NPC 自主决策与世界校准 LLM 的系统提示词：指导它替重要 NPC 想下一步行动、修复前后矛盾的剧情。\n包含输出格式约定，改动可能破坏滴答，谨慎。")
        form.addRow(self.sim_prompt)

        note = QLabel("战斗引擎与世界滴答逻辑已接入，以上旋钮即时生效。"
                      "每世界可在 config_overlay 单独覆盖（暂仅生成时继承 preset 值）。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#e0af68; font-size:12px;")
        form.addRow("", note)

        # ---- [P6] 商店与经济（活世界 + 代码交易）----
        shop_box = QGroupBox("商店与经济（活世界 + 代码交易）")
        shop_box.setFlat(True)
        shop_box.setToolTip("商店买卖完全由代码结算（不花 LLM）：价格按品级算、声望打折、货架定期补货。\n本分区控制补货节奏和 LLM 换货。")
        shop_inner = QHBoxLayout(shop_box)
        shop_inner.setContentsMargins(8, 6, 8, 0)
        shop_inner.addStretch(1)
        form.addRow(shop_box)

        self.shops_enabled = QCheckBox("启用商店系统（商售 NPC 摆货；关闭则世界无商店）")
        self.shops_enabled.setChecked(p.shops_enabled)
        self.shops_enabled.setToolTip("开启后商人类 NPC 会开店卖东西（武器店/药店/黑市等），可在场景页买卖。\n关闭则世界没有任何商店，物资全靠捡。")
        form.addRow("", self.shops_enabled)

        self.shops_restock_interval = _spin_int(p.shops_restock_interval, 1, 50, 1)
        self.shops_restock_interval.setToolTip("商店每隔几个回合自动补一次货（货架数量向库存上限靠拢）。")
        form.addRow("补货间隔(回合):", self.shops_restock_interval)

        # [删死旋钮 2026-09-10] 「补货幅度」旋钮（shops_price_volatility）全仓无消费方、
        # 文案还与字段名矛盾，已连字段一起移除；真实生效的是下面的「价格漂移」。
        self.shops_price_drift = _level_combo(
            p.shops_price_drift, "货架价格随每个补货周期浮动的幅度（模拟市场供需波动）。\n"
            "关闭=价格写死后永不变；轻/中/重=基准价 ±5%/±10%/±15% 确定性浮动。\n"
            "同一物品在不同商店、不同时点价格会有差异，破「价格僵死」。")
        form.addRow("价格漂移:", self.shops_price_drift)

        self.shops_llm_restock = QCheckBox("过期商店走 LLM 换货（关闭则只确定性补货，不调 LLM）")
        self.shops_llm_restock.setChecked(p.shops_llm_restock_enabled)
        self.shops_llm_restock.setToolTip("货架长期没补货的商店由 LLM 重新设计在卖什么（更贴合剧情，但花 token）。\n关闭则只按固定规则补数量，不调 LLM。")
        form.addRow("", self.shops_llm_restock)

        self.shops_wallclock = _spin_int(p.shops_wallclock_restock_minutes, 0, 120, 1)
        self.shops_wallclock.setToolTip("墙钟定时器：每 N 分钟给所有商店 LLM 换货（0=关，世界静止时不补货）。让世界活起来。")
        form.addRow("墙钟换货(分钟,0=关):", self.shops_wallclock)

        # [P14] 经济节奏 + 交易回应
        self.econ_pace = QComboBox()
        self.econ_pace.addItem("宽裕（买 8 折 / 卖与金币产出 x1.3，练级向）", "relaxed")
        self.econ_pace.addItem("标准（不修正，既有手感）", "standard")
        self.econ_pace.addItem("紧缩（买价 x1.4 / 卖与金币 x0.7，高阶货要攒钱）", "hard")
        self.econ_pace.addItem("硬核（买价 x2 / 卖与金币 x0.5，攒钱是核心玩法）", "hardcore")
        self.econ_pace.setCurrentIndex(self.econ_pace.findData(p.economy_pace))
        self.econ_pace.setToolTip("经济节奏难度开关：拉长游戏寿命。\n"
                                  "高品级道具本就按稀有度 11 倍曲线定价，紧缩/硬核再同时抬买价、压卖价、"
                                  "砍任务与奇遇的金币产出——传奇装备需要真攒钱，除非运气好摸到实物。\n"
                                  "每世界独立（创建时从这里的值继承）。")
        form.addRow("经济节奏:", self.econ_pace)

        self.trade_reply_chk = QCheckBox("关店后店主对本次交易说一句（LLM 回应，仅真商人店）")
        self.trade_reply_chk.setChecked(p.trade_reply_enabled)
        self.trade_reply_chk.setToolTip("离开真商人开的店时，若本次有买卖，店主会以自己的口吻回应一两句——\n"
                                        "比如你卖了高品质药水，店主可能说「这批货成色不错，"
                                        "后面有货再来找我」；买走贵货则会叮嘱几句。\n"
                                        "用叙事 LLM 生成（每次关店一小段），关闭则纯系统结算。\n"
                                        "游商店无店主恒不回应（不受此开关影响）。")
        form.addRow("", self.trade_reply_chk)

        form.addRow(QLabel("商店备货提示词（LLM 差异化货架生成）:"))
        self.shops_prompt = QTextEdit()
        self.shops_prompt.setPlainText(p.shops_system_prompt)
        self.shops_prompt.setMinimumHeight(100)
        self.shops_prompt.setToolTip("LLM 给商店重新设计货架时的系统提示词：让它按店的类型/所在地区/当前剧情上不同的货。\n包含输出格式约定，改动可能破坏备货，谨慎。")
        form.addRow(self.shops_prompt)

        # ---- [P6c] NPC 个人记忆 ----
        mem_box = QGroupBox("NPC 个人记忆（跨场景沉淀 + 召回注入）")
        mem_box.setFlat(True)
        mem_box.setToolTip("每个 NPC 独立记一份「跟你有关的事」，交互时自动整理、对话时自动召回注入。\n与聊天页的角色记忆是两套独立系统。")
        mem_inner = QHBoxLayout(mem_box)
        mem_inner.setContentsMargins(8, 6, 8, 0)
        mem_inner.addStretch(1)
        form.addRow(mem_box)

        self.npc_mem_enabled = QCheckBox("启用 NPC 个人记忆（关闭则 NPC 不沉淀记忆、不召回）")
        self.npc_mem_enabled.setChecked(p.npc_memory_enabled)
        self.npc_mem_enabled.setToolTip("开启后每个 NPC 会记住你跟他说过/做过的事，之后再见面会提起。\n关闭则 NPC 每次见面都是「初次见面」。")
        form.addRow("", self.npc_mem_enabled)

        self.npc_mem_mode = QComboBox()
        self.npc_mem_mode.addItem("summary（总结段落，覆盖整合）", "summary")
        self.npc_mem_mode.addItem("embedding_hybrid（明细条目 + 向量召回 top-K）", "embedding_hybrid")
        self.npc_mem_mode.setCurrentIndex(self.npc_mem_mode.findData(p.npc_memory_mode))
        self.npc_mem_mode.setToolTip("记忆怎么存：\n总结段落=不断把旧记忆合并成一段概括（省空间，丢细节）；\n明细条目=每件事单独存一条，聊天时按相关度召回最相关的几条（细节全，需配 Embedding API）。")
        form.addRow("记忆模式:", self.npc_mem_mode)

        self.npc_mem_interval = _spin_int(p.npc_memory_interval, 1, 30, 1)
        self.npc_mem_interval.setToolTip("每与一个 NPC 交互 N 次整理一次记忆（取窗 = interval）。")
        form.addRow("整理间隔(次交互):", self.npc_mem_interval)

        self.npc_mem_top_k = _spin_int(p.npc_memory_top_k, 1, 30, 1)
        self.npc_mem_top_k.setToolTip("注入场景上下文的记忆上限（emb top-K 或最近 N 条）。")
        form.addRow("召回上限(top-K):", self.npc_mem_top_k)

        self.npc_mem_api = _api_combo(self.storage, p.npc_memory_api_id)
        self.npc_mem_api.setToolTip("记忆整理 LLM API（空=回退结算 LLM API -> 首个 enabled）。")
        form.addRow("记忆整理 API:", self.npc_mem_api)

        note = QLabel("记忆独立于角色记忆（memory_service），存于 data/worlds/{id}_npc_memory/ + "
                      "ChromaDB npc_*_hybrid。档案页「查看记忆」展示沉淀结果（默认隐藏）。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", note)

        # ---- [P7g] 采集系统 ----
        gather_box = QGroupBox("采集系统（场景资源点 + 题材化采集）")
        gather_box.setFlat(True)
        gather_box.setToolTip("地点上的资源点（灵药田/矿脉/水池等）可反复采集拿材料，采空后等冷却再生。\n采集成功率/产出全由代码结算（属性+工具加成），不经 LLM。")
        g_inner = QHBoxLayout(gather_box)
        g_inner.setContentsMargins(8, 6, 8, 0)
        g_inner.addStretch(1)
        form.addRow(gather_box)

        self.gather_enabled = QCheckBox("启用采集系统（关闭则地点无资源点）")
        self.gather_enabled.setChecked(p.gathering_enabled)
        self.gather_enabled.setToolTip("开启后，世界生成时每个地点自动挂载 1 个题材化资源点（仙侠灵药田/现代水池/科幻能量节点等），玩家可在场景页采集。")
        form.addRow("", self.gather_enabled)

        self.gather_base_rate = _spin_float(p.gathering_base_rate, 0.05, 0.95, 0.05)
        self.gather_base_rate.setToolTip("采集基础成功率（实际 = base + 属性*1% + 工具*5% - 难度*2%，钳 5%-95%）。越高越容易采到。")
        form.addRow("基础成功率:", self.gather_base_rate)

        self.res_cooldown = _spin_int(p.resource_cooldown_ticks, 0, 30, 1)
        self.res_cooldown.setToolTip("资源点采完后冷却多少回合再生（0=不冷却，每次都可采）。冷却到期丰度恢复满。")
        form.addRow("再生冷却(回合,0=不冷却):", self.res_cooldown)

        self.res_decay = _spin_int(p.resource_richness_decay, 1, 50, 1)
        self.res_decay.setToolTip("每次采集丰度递减量（丰度到 0 枯竭，需等冷却再生）。值越大资源点越快枯竭。")
        form.addRow("丰度递减/次:", self.res_decay)

        gnote = QLabel("采集成功率/产出/丰度/冷却全由纯 Python 引擎结算（gather_engine），不经 LLM。"
                       "资源点题材化：仙侠(灵药田/灵矿脉)、武侠(草药丛/铁矿山)、现代(水池/冰箱/药箱)、"
                       "科幻(能量节点/废料堆)、末日(废墟搜刮/变异植物)。")
        gnote.setWordWrap(True)
        gnote.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", gnote)

        # ---- [P7f] 地图拓展 ----
        map_box = QGroupBox("地图拓展（边界迷雾块 + 无限增殖沙盒）")
        map_box.setFlat(True)
        map_box.setToolTip("世界地图不是固定的：已探索地点的边界显示迷雾块，点击向外生成新地点，地图可以无限长大。\n新地点由 LLM 按当前区域风格生成，失败自动回退模板。")
        m_inner = QHBoxLayout(map_box)
        m_inner.setContentsMargins(8, 6, 8, 0)
        m_inner.addStretch(1)
        form.addRow(map_box)

        self.map_expand_enabled = QCheckBox("启用地图拓展（已探索地点边界显示迷雾块，点击向外生成新地点）")
        self.map_expand_enabled.setChecked(p.map_expansion_enabled)
        self.map_expand_enabled.setToolTip("开启后，世界地图上已探索地点的边界（上下左右空格）显示迷雾块，点击调用 LLM 向该方向生成新地点，实现地图无限拓展。")
        form.addRow("", self.map_expand_enabled)

        self.map_batch = _spin_int(p.map_expansion_batch_size, 1, 5, 1)
        self.map_batch.setToolTip("每次点击迷雾块拓展生成几个新地点（1-5）。值越大单次探索发现越多。")
        form.addRow("单次拓展地点数(1-5):", self.map_batch)

        # [P11c] 拓展伴随新物品：物品入世主渠道（商店只从已有物品进货，新物品靠探索获得）
        self.map_items = _spin_int(p.map_expansion_item_count, 0, 10, 1)
        self.map_items.setToolTip("每开辟一个新地点伴随生成几件新物品（0=关，1-10）。\n"
                                  "新物品由 LLM 按新地点特色生成（失败回退题材装备模板），入库后商店换货时会上架。\n"
                                  "商店不再凭空进货——想让世界有新东西，去探索开新地点。")
        form.addRow("新地点伴随物品数(0=关):", self.map_items)

        self.map_recipes = _spin_int(p.map_expansion_recipe_count, 0, 10, 1)
        self.map_recipes.setToolTip("每次拓展生成新配方目标数（0=关，0-10）。\n"
                                    "配方消耗新地点的资源产出（资源-配方联动），让新物资有地方消耗。")
        form.addRow("单次拓展配方数(0=关):", self.map_recipes)

        self.map_dungeon_chance = _spin_float(p.map_expansion_dungeon_chance, 0.0, 1.0, 0.05)
        self.map_dungeon_chance.setToolTip("新野外地点挂秘境入口的概率（0=永不，0-1）。\n"
                                           "命中时在新地点生成一座可探索的秘境地牢。")
        form.addRow("新地点秘境入口概率:", self.map_dungeon_chance)

        self.map_monsters = _spin_int(p.map_expansion_monster_count, 0, 5, 1)
        self.map_monsters.setToolTip("每次拓展新增怪物物种数（0=关，0-5）。\n"
                                     "由 LLM 按新地点特色生成（失败回退题材怪物池），野外遇怪从此池抽取。")
        form.addRow("单次拓展怪物物种数(0=关):", self.map_monsters)

        mnote = QLabel("拓展由 LLM 据当前区域风格生成新地点（仙侠仍是灵山/洞府；现代仍是街道/商铺），"
                       "引擎自动分配坐标 + 接入连通图 + 标记已发现 + 挂资源点。LLM 失败回退确定性模板，保证总有结果。")
        mnote.setWordWrap(True)
        mnote.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", mnote)

        # ---- [P12] 资源分层 / 配方 / 野外怪物 / 天赋池 ----
        p12_box = QGroupBox("资源与配方（分层资源 + 配方联动）")
        p12_box.setFlat(True)
        p12_box.setToolTip("资源-配方-怪物联动经济环：野外点按危险度分 5 层产出对应稀有度材料；"
                           "配方覆盖各层资源；野外点有怪物把守（打败才安全采集）。")
        p12_inner = QHBoxLayout(p12_box)
        p12_inner.setContentsMargins(8, 6, 8, 0)
        p12_inner.addStretch(1)
        form.addRow(p12_box)

        self.gen_recipe_count = _spin_int(p.gen_recipe_count, 3, 100, 1)
        self.gen_recipe_count.setToolTip("世界生成时 LLM 出多少个合成配方（3-100，此数为下限）。\n"
                                         "实际目标会随「生成物品数量」自动抬高：不低于物品数的一半（最少 8 条），\n"
                                         "物品池扩容时锻造/炼制覆盖同步跟上。引擎保证每个资源层级（1-5）的材料\n"
                                         "至少被 1 个配方消耗，且优先为尚无配方的装备/消耗品补配方；\n"
                                         "LLM 数量不足或失败时兜底补齐。开新地点也会按新资源追加配方。")
        form.addRow("生成配方数量(3-100,下限):", self.gen_recipe_count)

        self.item_gen_count = _spin_int(p.item_gen_count, 4, 200, 1)
        self.item_gen_count.setToolTip("世界生成时 LLM 出多少件物品（4-200，含武器/防具/饰品/消耗品/材料）。\n"
                                       "拆分生成后物品数量独立成旋钮，不再随规模自动分档。")
        form.addRow("生成物品数量(4-200):", self.item_gen_count)

        self.talent_pool_size = _spin_int(p.talent_pool_size, 6, 100, 1)
        self.talent_pool_size.setToolTip("世界生成时天赋池有多少个天赋（6-100）。\n"
                                         "开局锁死 1 个 + 天赋点自选都从这个池里挑；后天无法购买，\n"
                                         "新的天赋只能通过奇遇（参悟/传承/异变类）从本池概率觉醒。")
        form.addRow("天赋池数量(6-100):", self.talent_pool_size)

        # [P13] 技能池：每个池技能生成一本技能书（商店/精英怪掉落/奇遇/任务奖励获取研读）
        self.skill_pool_size = _spin_int(p.skill_pool_size, 6, 100, 1)
        self.skill_pool_size.setToolTip("世界生成时可学习技能池的大小（6-100）。\n"
                                        "每个池技能都会生成一本题材化技能书（秘籍/谱/教程/芯片/手记/卷轴），"
                                        "经商店购买、精英怪/Boss 掉落、奇遇、开场任务奖励获得，"
                                        "背包里「使用」研读习得（已会的不重复学）。\n"
                                        "技能学会后继续靠施展攒熟练度升级（每级威力 +15%）。")
        form.addRow("技能池数量(6-100):", self.skill_pool_size)

        self.monster_pool_size = _spin_int(p.monster_pool_size, 6, 100, 1)
        self.monster_pool_size.setToolTip("世界生成时怪物池的目标数量（6-100）。\n"
                                          "LLM 生成的题材怪物池大小（danger 区间 + 掉落材料）；"
                                          "生成不足或失败时用题材兜底池补齐。")
        form.addRow("怪物池数量(6-100):", self.monster_pool_size)

        # ---- [P12] 野外怪物遭遇 ----
        wm_box = QGroupBox("野外怪物（野外点把守 + 跟随玩家等级成长）")
        wm_box.setFlat(True)
        wm_box.setToolTip("野外地点（山林/矿脉/废墟等）玩家每回合行动后判定遇怪，命中强制进入战斗。\n"
                          "怪物等级 = max(地点危险度, 玩家等级-1) 动态成长；聚落（城/镇/村）不会遇怪。")
        wm_inner = QHBoxLayout(wm_box)
        wm_inner.setContentsMargins(8, 6, 8, 0)
        wm_inner.addStretch(1)
        form.addRow(wm_box)

        self.wm_enabled = QCheckBox("启用野外怪物遭遇（野外点行动后可能被袭击）")
        self.wm_enabled.setChecked(p.wilderness_monsters_enabled)
        self.wm_enabled.setToolTip("开启后野外地点每回合行动（含采集）后 roll 一次遇怪判定，命中强制战斗。\n"
                                   "聚落永远安全；采集比普通行动更容易惊动怪物。")
        form.addRow("", self.wm_enabled)

        self.wm_chance = _spin_float(p.wilderness_monster_chance, 0.0, 1.0, 0.05)
        self.wm_chance.setToolTip("野外遇怪基础概率（0-1）。实际 = 基础 + 危险度x0.02 +（采集 +0.10），钳 5%-60%。\n"
                                  "危险度 5 的野外点采集时约 45% 遇怪。")
        form.addRow("遇怪基础概率:", self.wm_chance)

        self.elite_chance = _spin_float(p.elite_monster_chance, 0.0, 1.0, 0.05)
        self.elite_chance.setToolTip("遇怪时刷出精英怪的概率（0-1）。精英怪 +2 级、名字带题材前缀"
                                     "（凶悍的/妖化的…）、掉落率和掉落品质更高。")
        form.addRow("精英怪概率:", self.elite_chance)

        # ---- [P34e] 探查/地图拓展概率 ----
        self.expand_city = _spin_float(p.expand_new_city_chance, 0.0, 1.0, 0.05)
        self.expand_city.setToolTip("地图拓展新地点时 roll 此概率，命中则强制生成城市型聚落"
                                    "（含交易所/锻造屋/药店三类场所与商店）。0=拓展地点永不出城市。")
        form.addRow("拓展出城市概率:", self.expand_city)

        self.expand_npc = _spin_float(p.expand_new_npc_chance, 0.0, 1.0, 0.05)
        self.expand_npc.setToolTip("地图拓展新地点时 roll 此概率，命中则按下方数量生成 NPC 挂此地点"
                                   "（题材化命名，纯引擎无 LLM）。让世界越探索越有人。")
        form.addRow("拓展出 NPC 概率:", self.expand_npc)

        self.expand_npc_count = _spin_int(p.expand_new_npc_count, 0, 10, 1)
        self.expand_npc_count.setToolTip("拓展出 NPC 概率命中时，本次生成几个 NPC（0-10）。"
                                         "0=即使概率命中也不生成。")
        form.addRow("每次拓展 NPC 数量(0-10):", self.expand_npc_count)

        self.expand_fac = _spin_float(p.expand_new_faction_chance, 0.0, 1.0, 0.05)
        self.expand_fac.setToolTip("地图拓展新地点时 roll 此概率，命中则生成 1 个势力挂此地点"
                                   "（题材化命名，纯引擎无 LLM）。让世界越探索越多势力。")
        form.addRow("拓展出势力概率:", self.expand_fac)

        # ---- [P34f] 交易所股市 ----
        self.stock_on = QCheckBox("开启交易所股市（城市交易所可买卖大宗商品，每日 LLM 定价）")
        self.stock_on.setChecked(p.stock_market_enabled)
        self.stock_on.setToolTip("开启后城市交易所(auction shop)可访问股市，买卖题材化大宗商品"
                                 "（灵石/精铁/粮食等）；价格每日据世界事件/节日/经济 LLM 定价，"
                                 "可信息套利。LLM 失败走确定性兜底防随机游走。")
        form.addRow("", self.stock_on)

        self.stock_drift = _spin_float(p.stock_price_drift_pct, 0.0, 1.0, 0.05)
        self.stock_drift.setToolTip("单日行情价格漂移幅度上限（0-1，如 0.30=±30%）。"
                                    "LLM 给的新价会被钳制在此幅度内防崩盘；兜底波动也锚定基准价。")
        form.addRow("行情单日波动上限:", self.stock_drift)

        # ---- [P34g] 拍卖会 ----
        self.auction_on = QCheckBox("开启拍卖会（城市定期举办，限时竞价高 level 未鉴定拍品）")
        self.auction_on.setChecked(p.auction_enabled)
        self.auction_on.setToolTip("开启后城市型聚落周期性举办拍卖会，拍品含高 level 未鉴定物品"
                                   "（唯一商店外获取高 level 物品稳定渠道之一）。押金竞价，被超出退还。")
        form.addRow("", self.auction_on)

        self.auction_interval = _spin_int(p.auction_interval_days, 1, 60, 1)
        self.auction_interval.setToolTip("拍卖会生成间隔（天）：每 N 天在一座随机城市生成一场新拍卖会。")
        form.addRow("拍卖会间隔天数:", self.auction_interval)

        self.auction_duration = _spin_int(p.auction_duration_days, 1, 30, 1)
        self.auction_duration.setToolTip("拍卖会持续天数：start_day + N = 结束日，结束后未拍出拍品消失。")
        form.addRow("拍卖会持续天数:", self.auction_duration)

        self.auction_lots = _spin_int(p.auction_lot_count, 1, 20, 1)
        self.auction_lots.setToolTip("每场拍卖会拍品数量（1-20 件）。")
        form.addRow("拍卖会拍品数:", self.auction_lots)

        # ---- [P12] NPC 自主生活 ----
        self.npc_auto = QCheckBox("NPC 离屏自主生活（打怪/采集/采购更新背包 （升级走打猎结算））")
        self.npc_auto.setChecked(p.npc_autonomous_enabled)
        self.npc_auto.setToolTip("玩家看不见的地方，NPC 也在过日子：在野外的会狩猎采集（背包获得资源，"
                                 "送礼/搜刮有货可依），在聚落的会采购物资；战斗 NPC 缓慢升级并按定位自动加点。\n"
                                 "关闭则 NPC 只在原地做日常动作。")
        form.addRow("", self.npc_auto)

        # ---- [P7h] 回合制战斗 ----
        combat_box = QGroupBox("回合制战斗（玩家可操作 + 敌人 AI + 技能冷却）")
        combat_box.setFlat(True)
        combat_box.setToolTip("战斗打进可操作的回合制界面：你选攻击/技能/物品/防御/逃跑，敌人由 AI 行动。\n伤害/暴击/命中/掉落全由代码结算（不用 LLM 打架）。")
        c_inner = QHBoxLayout(combat_box)
        c_inner.setContentsMargins(8, 6, 8, 0)
        c_inner.addStretch(1)
        form.addRow(combat_box)

        self.combat_controlled = QCheckBox("启用玩家可操作回合制战斗（关则用老一击制自动结算）")
        self.combat_controlled.setChecked(p.combat_player_controlled)
        self.combat_controlled.setToolTip("开启后与敌对 NPC 战斗进入回合制界面：玩家可选攻击/技能/物品/防御/逃跑，敌人有 4 种 AI（好战/防御/施法/均衡）。关闭则兼容老版自动一击结算。")
        form.addRow("", self.combat_controlled)

        self.combat_max_rounds = _spin_int(p.combat_max_rounds, 5, 200, 5)
        self.combat_max_rounds.setToolTip("单场战斗回合上限（防死循环，超限自动脱战）。多敌遭遇时上限会按敌人数量自动放宽。")
        form.addRow("回合上限:", self.combat_max_rounds)

        self.combat_enemy_delay = _spin_int(p.combat_enemy_delay_ms, 0, 5000, 100)
        self.combat_enemy_delay.setToolTip("敌方每次行动前的停顿毫秒数（0=立即结算）。战斗中玩家行动后敌人稍作蓄势再出手，回合交互感更好；不改变任何战斗结果，只调节奏。")
        form.addRow("敌方行动延迟(毫秒):", self.combat_enemy_delay)

        # ---- [P10] 遭遇判定 + 同伴助战 ----
        self.combat_judge_llm = QCheckBox("遭遇规模与助战由 LLM 判定（失败回退确定性 roll）")
        self.combat_judge_llm.setChecked(p.combat_encounter_judge_llm)
        self.combat_judge_llm.setToolTip("开战时先调一次结算 LLM 判断：这次遭遇有几个敌人（狼群/匪帮会成群）、哪些在场同伴愿意助战（交情/等级/性格）。失败或关闭时用确定性 roll（据地点危险度与交情）。")
        form.addRow("", self.combat_judge_llm)

        self.combat_max_enemies = _spin_int(p.combat_max_enemies, 1, 5, 1)
        self.combat_max_enemies.setToolTip("单场战斗敌人数量上限（含主目标）。LLM/兜底判定出的增援随从不会超过此数。")
        form.addRow("敌人数量上限:", self.combat_max_enemies)

        self.combat_allies = QCheckBox("允许同场景高交情 NPC 助战（交情 40+ 的战斗 NPC 可能并肩作战）")
        self.combat_allies.setChecked(p.combat_allies_enabled)
        self.combat_allies.setToolTip("玩家开战时，同场景交情 40 以上且有战斗能力（等级>0）的 NPC 可能主动助战：同伴在玩家之后、敌人之前行动，可攻击敌人或治疗玩家；战后交情 +4，同伴倒下不死亡（保 1 血）。")
        form.addRow("", self.combat_allies)

        self.combat_max_allies = _spin_int(p.combat_max_allies, 0, 4, 1)
        self.combat_max_allies.setToolTip("单场战斗最多几名同伴同时助战（0=禁止助战）。")
        form.addRow("助战同伴上限:", self.combat_max_allies)

        # ---- [P25d] 世界 Boss ----
        self.world_boss_enabled = QCheckBox("世界 Boss 系统（tick 危机事件高危险地点可生成世界级威胁）")
        self.world_boss_enabled.setChecked(p.world_boss_enabled)
        self.world_boss_enabled.setToolTip(
            "开启后，tick 产生的重大/危机级世界事件若关联高危险地点（危险度达下限），"
            "会在此地生成一个世界级威胁 Boss（盘踞窗口 N 天，多形态递进讨伐）。"
            "击败最终形态：传说级掉落 + 全势力声望变动。窗口到期未讨伐则离去留传闻。")
        form.addRow("", self.world_boss_enabled)

        self.world_boss_min_danger = _spin_int(p.world_boss_min_danger, 1, 10, 1)
        self.world_boss_min_danger.setToolTip("生成世界 Boss 所需的地点最低危险度（1-10）。越高越易出 Boss。")
        form.addRow("世界 Boss 最低危险度:", self.world_boss_min_danger)

        self.world_boss_window_days = _spin_int(p.world_boss_window_days, 3, 30, 1)
        self.world_boss_window_days.setToolTip("世界 Boss 盘踞窗口天数（3-30）。到期未讨伐则离去留传闻，不强行终结。")
        form.addRow("世界 Boss 窗口天数:", self.world_boss_window_days)

        # ---- [P26a] 结义/婚恋 ----
        self.relations_enabled = QCheckBox("结义/婚恋系统（NPC 关系阶段：好友 -> 结义 -> 恋人 -> 配偶）")
        self.relations_enabled.setChecked(p.relations_enabled)
        self.relations_enabled.setToolTip(
            "开启后，玩家可与交情达标的 NPC 结义（并肩作战普攻加成）、送定情信物结为恋人、"
            "在节日当天于宅中求婚成婚。配偶常伴左右（同行位豁免），离屏时也更易带话寄礼。"
            "关系只进不退（无限世界持续互动）；NPC 死亡后关系条目保留但无玩法效果。")
        form.addRow("", self.relations_enabled)

        self.sworn_damage_bonus = _spin_float(p.sworn_damage_bonus, 1.0, 2.0, 0.05)
        self.sworn_damage_bonus.setToolTip(
            "结义同伴在场时，彼此普通攻击伤害倍率（1.0=不加成，1.10=默认+10%，最高 2.0）。"
            "仅对普攻生效（技能路径豁免，口径统一）。")
        form.addRow("结义联手伤害倍率:", self.sworn_damage_bonus)

        # ---- [P27] 二层地图 ----
        self.places_enabled = QCheckBox("地点内场所系统（二层地图：地点 -> 场所，场所间连通移动）")
        self.places_enabled.setChecked(p.places_enabled)
        self.places_enabled.setToolTip(
            "开启后，每个地点（聚落/野外）生成多个场所（酒馆/铁匠铺/集市/宅邸/林间空地等），"
            "玩家与 NPC 可在场所间移动（连通图），在场 NPC 按场所级过滤。"
            "关闭则退化单层地图（无场所，位置精度=地点级）。仅影响新世界生成；已有世界的场所保留。")
        form.addRow("", self.places_enabled)

        self.places_per_location = _spin_int(p.places_per_location, 0, 10, 1)
        self.places_per_location.setToolTip(
            "世界生成时每个地点生成的场所数（0=不生成，3-5 推荐；规模档 small/medium/large 默认 3/4/5）。"
            "聚落场所如酒馆/铁匠铺/集市/宅邸；野外场所如林间空地/溪边/山崖。题材化命名。")
        form.addRow("每地点场所数:", self.places_per_location)

        # ---- [P32] 同伴插话 ----
        self.companion_interject_enabled = QCheckBox("同伴插话（同行同伴在场景中偶尔发表一句简短插话）")
        self.companion_interject_enabled.setChecked(p.companion_interject_enabled)
        self.companion_interject_enabled.setToolTip(
            "开启后，同行同伴在玩家行动后可能主动插话（认同/中立/反对，呼应其好感与腔调）。"
            "引擎纯 Python+SeededRng 判定\"何时说\"（同伴在场/行动类型门控/频率水位线/好感极值加成），"
            "叙事 LLM 写\"说什么\"。插话须显式署名避免误归因。不带同伴时不触发。")
        form.addRow("", self.companion_interject_enabled)

        self.companion_interject_interval = _spin_int(p.companion_interject_interval, 1, 30, 1)
        self.companion_interject_interval.setToolTip(
            "同一同伴两次插话的最小间隔（tick，1-30，默认 5）。间隔内不再触发，防连续刷屏。"
            "高频行动（战斗/冒险）触发率高于移动/观察。")
        form.addRow("同伴插话最小间隔:", self.companion_interject_interval)

        # ---- [P33] 隐藏/被动检定 ----
        self.hidden_check_enabled = QCheckBox("隐藏/被动察觉检定（属性特长让探索更有意义）")
        self.hidden_check_enabled.setChecked(p.hidden_check_enabled)
        self.hidden_check_enabled.setToolTip(
            "开启后，进入秘境陷阱房时可能预发现免伤绕行、宝藏房可能发现暗格额外奖励、"
            "首次进入地点时可能察觉隐藏人物或线索。检定属性 = 智力(int)与运气(luk)取高 + "
            "天赋 check_mult 加成。纯 Python+SeededRng 确定性，结果走叙事提示不持久化。")
        form.addRow("", self.hidden_check_enabled)

        cnote = QLabel("战斗完全由纯 Python 引擎结算（不用 LLM 打架）：伤害/暴击/命中/闪避/招架/掉落/经验全代码算。"
                       "敌人 AI 据 role 推断（法师->施法、首领->好战、守卫->防御、杂兵->均衡）。"
                       "技能有冷却回合 + mp 消耗；战斗中可用背包消耗品回血。"
                       "多敌遭遇可点击敌人名字切换攻击目标；同伴自动行动。")
        cnote.setWordWrap(True)
        cnote.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", cnote)

        # ---- [P10] 同场景 NPC 互动 + 好友 ----
        npc_box = QGroupBox("同场景 NPC 互动 + 好友（打招呼/送礼/氛围行动/私聊）")
        npc_box.setFlat(True)
        npc_box.setToolTip("跟 NPC 的关系经营系统：混熟了 NPC 会主动打招呼/送礼，交情够了可加好友、私聊、邀请同行。\n全部确定性判定（同回合同结果），文本由叙事 LLM 展开。")
        n_inner = QHBoxLayout(npc_box)
        n_inner.setContentsMargins(8, 6, 8, 0)
        n_inner.addStretch(1)
        form.addRow(npc_box)

        self.npc_reactions = QCheckBox("启用同场景 NPC 主动行为（关系够好时会主动互动）")
        self.npc_reactions.setChecked(p.npc_reactions_enabled)
        self.npc_reactions.setToolTip("玩家行动结算后，同场景 NPC 可能主动反应：移动到达时交情 25+ 的 NPC 上前打招呼；聊天后交情 55+ 的 NPC 可能送礼（从随身物品或世界池挑）；交情 45+ 的 NPC 会有氛围小行动。全部确定性 roll（同回合同结果），产出叙事要点由旁白 LLM 展开。")
        form.addRow("", self.npc_reactions)

        self.npc_gift_chance = _spin_float(p.npc_gift_chance, 0.0, 1.0, 0.05)
        self.npc_gift_chance.setToolTip("高交情 NPC（55+）谈话后送礼的基础概率（0-1）。交情越高实际概率越高（此值约 0.5-1 倍生效）。")
        form.addRow("送礼基础概率:", self.npc_gift_chance)

        self.friend_threshold = _spin_int(p.friend_affinity_threshold, 0, 100, 5)
        self.friend_threshold.setToolTip("交情达到此值（0-100）的 NPC 可添加为好友。好友可从好友页私聊（交情 +1/次），互动更积极。")
        form.addRow("好友交情门槛:", self.friend_threshold)

        self.friend_chat = QCheckBox("允许好友私聊（独立于场景回合的对话）")
        self.friend_chat.setChecked(p.friend_chat_enabled)
        self.friend_chat.setToolTip("好友页/NPC 面板的「私聊」入口：用叙事 LLM 以该 NPC 的性格/交情/个人记忆直接对话，历史持久化（data/worlds/{id}_npc_chat/）。")
        form.addRow("", self.friend_chat)

        # [A3 2026-08-28] 好友主动私聊开关
        self.friend_chat_proactive = QCheckBox("允许好友主动发私聊（交情≥70 的好友会主动联系你）")
        self.friend_chat_proactive.setChecked(p.friend_chat_proactive_enabled)
        self.friend_chat_proactive.setToolTip("交情≥70（恋人/配偶加权）的好友每数回合有小概率主动给你发一条私聊（LLM 以其性格/记忆生成，落进私聊记录，可在好友页回复）；同好友至少隔 2 天一条，每回合最多生成 1 条（占滴答 LLM 预算）。")
        form.addRow("", self.friend_chat_proactive)

        # ---- [P10b] 同行 ----
        self.companions_chk = QCheckBox("允许邀请 NPC 同行（交情达门槛可结伴移动）")
        self.companions_chk.setChecked(p.companions_enabled)
        self.companions_chk.setToolTip("交情达门槛（默认 50）的同场景 NPC 可从交互面板「邀请同行」：随你一同移动地点（含地图跳转），再点对话「道别（退队）」。同行 NPC 在战斗遭遇判定中几乎必定助战（若有战斗能力）。")
        form.addRow("", self.companions_chk)

        self.companion_threshold = _spin_int(p.companion_affinity_threshold, 0, 100, 5)
        self.companion_threshold.setToolTip("邀请同行所需交情（0-100）。默认 50（相熟），低于好友门槛 60、高于助战门槛 40。")
        form.addRow("同行交情门槛:", self.companion_threshold)

        self.companion_max = _spin_int(p.companion_max_followers, 0, 3, 1)
        self.companion_max.setToolTip("最多同时几名 NPC 同行（0-3）。队伍满时需先道别再邀请。")
        form.addRow("同行人数上限:", self.companion_max)

        # ---- [P7i] 任务系统 ----
        quest_box = QGroupBox("任务系统（接取/追踪/领奖）")
        quest_box.setFlat(True)
        quest_box.setToolTip("结构化任务：有目标（击败/采集/交谈/到达/收集）和奖励（金币/经验/物品）。\n进度自动推进，做完去任务日志领奖；全由代码判定。")
        q_inner = QHBoxLayout(quest_box)
        q_inner.setContentsMargins(8, 6, 8, 0)
        q_inner.addStretch(1)
        form.addRow(quest_box)

        self.quest_enabled = QCheckBox("启用结构化任务（接取/进度追踪/完成领奖）")
        self.quest_enabled.setChecked(p.quest_system_enabled)
        self.quest_enabled.setToolTip("开启后，任务有结构化目标（击败/采集/交谈/到达/收集）和奖励（金币/经验/物品）。玩家击败敌人/采集/到达地点/交谈时自动推进进度，完成后可在任务日志领奖。关闭则任务仅作叙事提示无数值奖励。")
        form.addRow("", self.quest_enabled)

        qnote = QLabel("任务进度完全由纯 Python 引擎判定（quest_engine，不用 LLM 实时判定）：击败敌人->kill、"
                       "采集->gather、到达地点->visit、与 NPC 交谈->talk、获得物品->collect。LLM 只在世界生成时出任务定义（目标+奖励的题材包装）。")
        qnote.setWordWrap(True)
        qnote.setStyleSheet("color:#7dcfff; font-size:12px;")
        form.addRow("", qnote)

        # ---- [P15a] 场景日志滚动总结（token 优化）----
        self.scene_summary_threshold = _spin_int(p.scene_summary_threshold, 0, 200, 1)
        self.scene_summary_threshold.setToolTip(
            "场景日志条数超过此值时，回合收尾自动把最老一批记录压成一段「前情摘要」"
            "（保留最近 12 条原文），此后送 LLM 的上下文用「摘要 + 最近 2-3 条全文」"
            "代替长历史回灌，长局每回合可省大量 token。0=关闭滚动总结。")
        form.addRow("场景日志总结阈值(条,0=关):", self.scene_summary_threshold)
        self.scene_summary_max_chars = _spin_int(p.scene_summary_max_chars, 0, 2000, 50)
        self.scene_summary_max_chars.setToolTip(
            "「前情摘要」的字数上限（0=不限制）。上限注入总结提示词作硬约束："
            "越小越长局上下文越省，但保留的细节越少；默认 400 字约 3-6 句。")
        form.addRow("前情摘要字数上限(字,0=不限):", self.scene_summary_max_chars)

        # ---- [P7k8] 永久死亡（roguelike 硬核）----
        pd_box = QGroupBox("永久死亡（roguelike 硬核模式）")
        pd_box.setFlat(True)
        pd_box.setToolTip("roguelike 硬核模式：战斗被打败时整个世界存档直接删除，无法恢复。\n慎开！关闭则只是剧情上描写你倒下，世界还在。")
        pd_inner = QHBoxLayout(pd_box)
        pd_inner.setContentsMargins(8, 6, 8, 0)
        pd_inner.addStretch(1)
        form.addRow(pd_box)

        self.permadeath_chk = QCheckBox("启用永久死亡（玩家战斗失败 HP 归 0 时删除整个世界，回首页）")
        self.permadeath_chk.setChecked(p.permadeath_enabled)

        # [P36] NPC 永久死亡（用户指示 2026-08-21）
        self.npc_permadeath_chk = QCheckBox("NPC 永久死亡（阵亡一律不重生，商人与要角死了就是死了）")
        self.npc_permadeath_chk.setChecked(p.npc_permadeath)
        self.npc_permadeath_chk.setToolTip("开启后所有 NPC（含杂兵与商人）死亡不再重生：击杀有永久后果，商人阵亡则其商店永久停摆。关闭为默认：所有 NPC（含要角）死亡后 8-14 天重生（[P45 v3 2026-09-12] 要角不再永久死，统一由本开关决定；主线发布人不死）。硬核活世界选项。")
        form.addRow("", self.npc_permadeath_chk)

        # [P46] 死亡补员（用户指示 2026-08-23）：仅 npc_permadeath 开启时生效
        self.npc_backfill_chk = QCheckBox("死亡补员（商人阵亡有接班人接手店铺，平民有空缺新人顶替）")
        self.npc_backfill_chk.setChecked(p.npc_backfill_enabled)
        self.npc_backfill_chk.setToolTip("仅「NPC 永久死亡」开启时生效：死者到岗期满后自动补员——店主由接班人接手（继承店铺、势力与货底，另有开业本金），平民由同角色新人顶替。敌对单位与要角不补（战果与剧情死亡留有分量）。")
        form.addRow("", self.npc_backfill_chk)

        # [P38a/P39a] NPC 社交演化 + 每日社会交互（同地结交/结仇 + 互市/冲突/互赠，活世界氛围）
        self.social_chk = QCheckBox("NPC 社交与互市（同地 NPC 自行结交好友、结下仇怨、互相交易）")
        self.social_chk.setChecked(p.social_enabled)
        self.social_chk.setToolTip("开启后 NPC 之间会随时间自行演化关系：兴趣相投者结为好友甚至莫逆，阵营敌对或结怨者渐成仇敌；每日还有社会交互——缺料的互相买卖（好友有折扣）、仇敌偶遇起冲突、挚友互赠随身之物，NPC 每日也有生活开销。关系与交易变化出现在右侧「NPC 动态」日志，并影响旁白与 NPC 详情里的关系描述。纯本地模拟，不消耗 token。")
        form.addRow("", self.social_chk)

        # [P42c] 日常跑腿任务
        self.errand_chk = QCheckBox("日常跑腿（聚落每日刷新传话/捎物小差事，小奖励+交情）")
        self.errand_chk.setChecked(p.errands_enabled)
        self.errand_chk.setToolTip("开启后每个聚落每日刷新 1-2 条跑腿差事（替 NPC 传话或捎物给另一位 NPC），完成后回到发布人处领小额金币与交情。当日未完成自动作废。开局不打怪的谋生路子。纯本地模拟，不消耗 token。")
        form.addRow("", self.errand_chk)

        # [P39d+e] 剧情导演层 + 世界编年史
        self.chronicle_chk = QCheckBox("坊间热议与世界编年史（世界大事按相关性进入旁白谈资，史官定期沉淀编年史）")
        self.chronicle_chk.setChecked(p.chronicle_enabled)
        self.chronicle_chk.setToolTip("开启后：近期世界大事会按与你的相关性（所在地点 > 关系人物 > 其他）选 3 条进入叙事上下文，在场 NPC 可自然提起；世界大事累积到一定数量会由史官（低频一次小 LLM 调用）压缩成编年史条目长期保留，让世界记得自己发生过什么。关闭则回退纯时间序的世界动态。")
        form.addRow("", self.chronicle_chk)

        # [剧情线 P46 2026-09-12] 世界自演剧情线（前瞻导演层）
        self.story_arc_chk = QCheckBox("剧情线（世界自演：策划者串起多名 NPC，阶段推进产事件、可派生介入任务）")
        self.story_arc_chk.setChecked(p.story_arcs_enabled)
        self.story_arc_chk.setToolTip(
            "开启后世界每隔几天会自发酝酿 1-2 条剧情线（有策划者、有参与者、分阶段推进）："
            "阶段到期进入事件流/坊间热议/传闻/编年史，要角与参与 NPC 的决策和日常会被剧情线牵引，"
            "玩家还能接取剧情线派生的介入任务。结局只做软结算（势力消长、人际起伏），不夺城不杀人。"
            "无可用 API 时由引擎从真实敌意（势力负关系/世仇）取材出保底剧情线。")
        form.addRow("", self.story_arc_chk)

        self.arc_interval = _spin_int(p.arc_interval_days, 1, 30, 1)
        self.arc_interval.setToolTip("剧情线规划间隔（天）：每隔 N 天批量规划新一批剧情线（一次 LLM 调用或引擎兜底）。")
        form.addRow("剧情线规划间隔:", self.arc_interval)

        self.arc_max_active = _spin_int(p.arc_max_active, 0, 12, 1)
        self.arc_max_active.setToolTip("同时在演的剧情线上限（0=只推进旧线、不再开新线）。建议 2-6。")
        form.addRow("剧情线并行上限:", self.arc_max_active)

        # [P39b] 委托订单板（NPC 收购单：玩家生产交付赚钱/交情/声望）
        self.commission_chk = QCheckBox("委托订单（NPC 发布收购单，玩家生产交付赚高价与声望）")
        self.commission_chk.setChecked(p.commissions_enabled)
        self.commission_chk.setToolTip("开启后聚落的 NPC 会每日发布收购委托（补货/节庆/急缺三类），出价高于商店收价 1.2-1.5 倍。在场景页「订单」查看，生产或收集对应物品后到发布聚落当面交付，获得金币、发布人交情与势力声望。订单数日过期。纯本地模拟，不消耗 token。")
        form.addRow("", self.commission_chk)
        self.permadeath_chk.setToolTip("roguelike 硬核模式：玩家在战斗中被击败（HP 归 0）时，整个世界被永久删除并回到世界模拟首页，无法恢复。慎用！关闭则玩家倒下后会被送回出生地并以 1 点 HP 复活，世界保留可继续。")
        form.addRow("", self.permadeath_chk)
        return w

    def _build_image_tab(self, p: WorldSimPreset) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)

        # [P5] 文生图总开关 + 勾选清单 分组
        global_box = QGroupBox("文生图总开关")
        global_box.setFlat(True)
        global_inner = QHBoxLayout(global_box)
        global_inner.setContentsMargins(8, 6, 8, 0)
        global_inner.addStretch(1)
        form.addRow(global_box)

        self.img_enabled = QCheckBox("启用文生图（总开关）")
        self.img_enabled.setChecked(p.image_enabled)
        self.img_enabled.setToolTip("世界模拟所有生图的总开关（世界横幅/地点背景/NPC 头像/物品图/场景插图）。\n关闭后完全不生图（省时省 GPU），也不管下面的勾选。需要配置好 ComfyUI 才能出图。")
        form.addRow("", self.img_enabled)

        # [P5] 生成阶段勾选清单分组
        gen_box = QGroupBox("世界生成时勾选清单")
        gen_box.setFlat(True)
        gen_box.setToolTip("世界生成那一次分别要出哪些图（出完随存档保存，之后不再重复生成）。\n勾得越多生成越慢，按需取舍。")
        gen_inner = QHBoxLayout(gen_box)
        gen_inner.setContentsMargins(8, 6, 8, 0)
        gen_inner.addStretch(1)
        form.addRow(gen_box)

        self.img_banner = QCheckBox("世界 banner")
        self.img_banner.setChecked(p.image_world_banner)
        self.img_banner.setToolTip("生成世界的宽幅横幅图（详情页顶部大图，按世界观出一张风景/氛围图）。")
        self.img_loc = QCheckBox("地点背景")
        self.img_loc.setChecked(p.image_location_bg)
        self.img_loc.setToolTip("给每个地点生成一张场景背景图（进场景页时铺在右侧叙事区背景）。")
        self.img_npc = QCheckBox("NPC 头像")
        self.img_npc.setChecked(p.image_npc_avatar)
        self.img_npc.setToolTip("给每个 NPC 生成头像（场景页在场列表/交互面板/档案页显示）。")
        self.img_legend = QCheckBox("传说/史诗物品图")
        self.img_legend.setChecked(p.image_legendary_item)
        self.img_legend.setToolTip("给传说/史诗品级的物品生成图标（背包/商店/合成界面显示）。")
        self.img_normal = QCheckBox("普通物品图")
        self.img_normal.setChecked(p.image_normal_item)
        self.img_normal.setToolTip("也给普通/精良等低品级物品生成图标。\n不勾则低品级物品显示品级色占位块（生成更快）。")
        self.img_monster = QCheckBox("预生成图（怪物/宠物/技能图标）")
        self.img_monster.setChecked(p.image_monster)
        self.img_monster.setToolTip(
            "世界生成时批量出图，游玩过程中不现场渲染，UI 只展示缓存：\n"
            "题材怪物池（地图拓展新增物种时同步补图，战斗对话框敌人区展示）、"
            "宠物种族池（宠物页头像）、世界技能池+已学技能（战斗技能按钮图标）。")
        self.img_home = QCheckBox("住宅图标（建筑/家具）")
        self.img_home.setChecked(p.image_home)
        self.img_home.setToolTip(
            "世界生成时批量出建筑/家具图标（住宅页卡片展示）。\n"
            "不勾或无 ComfyUI 时住宅页回退彩色品类徽章，功能不受影响。")
        # [2026-09-25 用户指示] 人物/怪物图「纯色背景 -> 代码抠透明」
        self.combat_intents = QCheckBox("战斗敌情预告")
        self.combat_intents.setChecked(p.combat_intents_enabled)
        self.combat_intents.setToolTip(
            "战斗中每回合播报敌方主力锁定的目标与预计威胁（轻/中/重击）。\n"
            "让你先看敌情再做决定：举盾、喝药、集火还是换目标。")
        form.addRow("", self.combat_intents)
        self.img_cutout = QCheckBox("人物/怪物/宠物图透明背景")
        self.img_cutout.setChecked(p.image_cutout_enabled)
        self.img_cutout.setToolTip(
            "对 NPC 头像/玩家立绘/怪物/宠物图：生图时要求纯色背景，出图后用代码把背景抠成透明。\n"
            "战斗立绘与卡片头像就没有方形底色块了，叠在场景图上更干净。\n"
            "抠图失败（背景不是纯色）会自动保留原图，不影响出图结果。\n"
            "已经生好的旧图：详情页「补缺失图」会顺带把能抠的存量图补抠一次"
            "（NPC 头像在档案页「重新生成头像」时也会按本设置重出）。")
        self.img_cutout_bg = QComboBox()
        for _c in CUTOUT_BG_COLORS:
            self.img_cutout_bg.addItem(CUTOUT_BG_LABELS.get(_c, _c), _c)
        _bg_idx = self.img_cutout_bg.findData(p.image_cutout_bg)
        if _bg_idx < 0:
            _bg_idx = self.img_cutout_bg.findData("white")
        self.img_cutout_bg.setCurrentIndex(max(0, _bg_idx))
        self.img_cutout_bg.setToolTip(
            "生图时要求模型用的背景色。抠图代码不依赖它——实际背景色从图像边缘自动判定。\n"
            "白最通用；主体含大量白色（白发/白袍）时换黑/灰/绿更保险。")
        for cb in (self.img_banner, self.img_loc, self.img_npc, self.img_legend, self.img_normal,
                   self.img_monster, self.img_home, self.img_cutout):
            form.addRow("", cb)
        form.addRow("透明背景颜色:", self.img_cutout_bg)

        # ---- 场景事件生图（P2 场景交互循环，叙事 LLM 旁白内 [img:...] 触发）----
        # [!] 与上方"关键事件插图"独立：本开关控场景交互回合内生图，独立于世界生成时批量出图。
        # [P5] QGroupBox 视觉分组（替代文字分隔）
        scene_img_box = QGroupBox("场景事件生图 (场景交互回合内)")
        scene_img_box.setFlat(True)
        scene_img_inner = QHBoxLayout(scene_img_box)
        scene_img_inner.setContentsMargins(8, 6, 8, 0)
        scene_img_inner.addStretch(1)
        form.addRow(scene_img_box)
        self.narrative_img_event = QCheckBox("剧情回合插图（由叙事判断画面）")
        self.narrative_img_event.setChecked(p.narrative_image_event)
        self.narrative_img_event.setToolTip("叙事判断关键画面并在回合完成后插入图片，重进场景仍能查看。\n同时需要开启上方的文生图总开关；关闭时不生成回合插图。\n勾选后旁白系统提示会注入「图片标签要求」（教叙事 LLM 在旁白末尾输出 [img:...] 标签），取消勾选则不注入。")
        form.addRow("", self.narrative_img_event)

        # [P11d] 关键事件插图（游玩期生图，与场景生图同组；区别于上方「世界生成时」一次性清单）
        self.img_event = QCheckBox("启用关键事件插图（世界大事记的重大事件配插画）")
        self.img_event.setChecked(p.image_event)
        self.img_event.setToolTip("与上面的场景生图不同：这里给「世界大事记」的重大事件配图——世界滴答产出 "
                                  "major/crisis 级事件（势力开战/要角异动等）时，自动给最严重的一条配一张插画，"
                                  "显示在世界详情页的「事件」页。\n每回合最多 1 张（小事件不配图）。关闭则大事记纯文字。")
        form.addRow("", self.img_event)
        # 频次限频：每 N 回合允许一次，0=仅 LLM 主动控制
        self.narrative_img_freq = QSpinBox()
        self.narrative_img_freq.setRange(0, 100)
        self.narrative_img_freq.setValue(p.narrative_image_freq)
        self.narrative_img_freq.setToolTip("每 N 回合才允许生一张图（控制生图频率，防每回合都出图太慢）。\n0=不限频，由叙事 LLM 自己决定何时插图。")
        form.addRow("触发频次(每 N 回合):", self.narrative_img_freq)
        # 同一世界累计上限：0=不限
        self.narrative_img_max = QSpinBox()
        self.narrative_img_max.setRange(0, 999)
        self.narrative_img_max.setValue(p.narrative_image_max_per_session)
        self.narrative_img_max.setToolTip("同一个世界存档累计最多生多少张场景图（防无限生图占满磁盘）。\n0=不限。")
        form.addRow("本世界累计上限:", self.narrative_img_max)

        self.img_prefix = QLineEdit(p.image_style_prefix)
        self.img_prefix.setPlaceholderText("如画质/画风 LoRA 前缀，留空则无")
        self.img_prefix.setToolTip("拼在所有生图提示词最前面的固定前缀。\n放画风/画质词（如 masterpiece, best quality 或某个画风 LoRA 触发词），让全部图风格统一。")
        form.addRow("画风前缀:", self.img_prefix)

        self.img_skip = QCheckBox("创建时允许跳过生图")
        self.img_skip.setChecked(p.image_skip_allowed)
        self.img_skip.setToolTip("世界生成界面出现「跳过生图」按钮：骨架先生成保存，图以后再手动补。\n适合先快速开局试玩、之后再补图的玩法。")
        form.addRow("", self.img_skip)
        return w

    def _build_gen_tab(self, p: WorldSimPreset) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)

        # [P5] 世界生成默认分组
        gen_box = QGroupBox("世界生成默认")
        gen_box.setFlat(True)
        gen_inner = QHBoxLayout(gen_box)
        gen_inner.setContentsMargins(8, 6, 8, 0)
        gen_inner.addStretch(1)
        form.addRow(gen_box)

        self.scale = QComboBox()
        # [P5b] 默认规模下拉：内置三档 + 用户自定义档（按 preset.custom_world_scales 合并）
        from src.models.world_sim_preset import BUILTIN_SCALES
        for c in BUILTIN_SCALES:
            self.scale.addItem(str(c.get("label") or c.get("id")), c["id"])
        for it in (p.custom_world_scales or []):
            if isinstance(it, dict) and it.get("id"):
                self.scale.addItem(str(it.get("label") or it.get("id")), it["id"])
        idx = self.scale.findData(p.default_world_scale)
        self.scale.setCurrentIndex(idx if idx >= 0 else 0)
        self.scale.setToolTip("新建世界时规模下拉默认选哪一档（小=几个地点紧凑剧情；中/大=更多地点与 NPC）。\n只是默认值，每次生成时仍可改。")
        form.addRow("默认规模:", self.scale)

        # [2026-09-13 用户指示] 聚落数量旋钮（0=自动）：注入生成提示词 + 引擎一致性兜底
        self.gen_settlement_count = QSpinBox()
        self.gen_settlement_count.setRange(0, 999)
        self.gen_settlement_count.setValue(max(0, int(getattr(p, "gen_settlement_count", 0) or 0)))
        self.gen_settlement_count.setSpecialValueText("自动（LLM 按世界观分配）")
        self.gen_settlement_count.setToolTip(
            "新建世界时聚落（城市/镇/村）的数量。\n"
            "0=自动，由 LLM 按世界观自由分配；\n"
            ">0=固定聚落数量，其余地点全为野外——刷怪/采集都在野外，至少保留 1 块。\n"
            "聚落数超过规模总地点数时按「留 1 块野外」钳制。安全区（聚落）危险度恒为 0。")
        form.addRow("聚落数量:", self.gen_settlement_count)

        self.tags = QLineEdit(", ".join(p.default_genre_tags))
        self.tags.setPlaceholderText("逗号分隔，如 修仙,末日,赛博朋克")
        self.tags.setToolTip("新建世界时预填的题材标签（逗号分隔），决定世界观风格。\n如：修仙/武侠/末日/科幻/都市/西幻，可自由组合。")
        form.addRow("默认题材标签:", self.tags)

        # [P5b] 自定义规模档位列表编辑器
        # 设计：每行 = id输入框 + label输入框 + hint输入框 + 删除按钮；
        # 顶部一行「+ 添加」按钮。空状态显示提示文案。
        custom_box = QGroupBox("自定义规模档位（可增删，生成世界时并入下拉）")
        custom_box.setFlat(True)
        custom_layout = QVBoxLayout(custom_box)
        custom_layout.setContentsMargins(8, 6, 8, 6)
        custom_layout.setSpacing(4)

        # 标题行（说明列含义）
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        h_id = QLabel("ID (英文, 唯一)")
        h_id.setStyleSheet("color:#7aa2f7; font-weight:bold; font-size:11px;")
        h_label = QLabel("显示名 (中文)")
        h_label.setStyleSheet("color:#7aa2f7; font-weight:bold; font-size:11px;")
        h_nums = QLabel("地点 / NPC / 场所")
        h_nums.setStyleSheet("color:#7aa2f7; font-weight:bold; font-size:11px;")
        header_row.addWidget(h_id, 1)
        header_row.addWidget(h_label, 2)
        header_row.addWidget(h_nums, 4)
        header_row.addWidget(QLabel(""), 0)  # 占位对齐删除按钮列
        custom_layout.addLayout(header_row)

        # 容器：装动态行
        self._scale_rows_container = QWidget()
        self._scale_rows_layout = QVBoxLayout(self._scale_rows_container)
        self._scale_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._scale_rows_layout.setSpacing(4)
        # 起点加 stretch 让新增行插到上面
        self._scale_rows_layout.addStretch(1)
        custom_layout.addWidget(self._scale_rows_container)

        # + 添加 按钮（在最下方）
        add_btn = QPushButton("+ 添加自定义规模")
        add_btn.clicked.connect(self._on_add_custom_scale)
        custom_layout.addWidget(add_btn)

        # 提示文案
        custom_hint = QLabel(
            "示例: id=tiny, 显示名=微型沙盒, 地点2 / NPC3 / 场所2\n"
            "只填数字（地点数/NPC数/每地点场所数），提示词文本由系统按固定模板生成。\n"
            "注意: id 不能与内置三档 (small/medium/large) 重复；保存时校验去重。"
        )
        custom_hint.setWordWrap(True)
        custom_hint.setStyleSheet("color:#7dcfff; font-size:11px;")
        custom_layout.addWidget(custom_hint)

        # 初始化：填充现有自定义档
        for it in (p.custom_world_scales or []):
            if isinstance(it, dict):
                self._add_scale_row(it.get("id", ""), it.get("label", ""),
                                    it.get("locations", 6), it.get("npcs", 5), it.get("places", 3))

        form.addRow(custom_box)

        # [P5c] 属性/装备模板分组：默认模板下拉 + 立绘开关 + 内置模板说明
        attr_box = QGroupBox("属性/装备模板（题材化字段名）")
        attr_box.setFlat(True)
        attr_layout = QVBoxLayout(attr_box)
        attr_layout.setContentsMargins(8, 6, 8, 6)
        attr_layout.setSpacing(6)

        # 默认模板下拉（内置 6 + 自定义）
        from src.models.world_sim_preset import (
            BUILTIN_ATTRIBUTES_TEMPLATES, DEFAULT_ATTRIBUTE_TEMPLATE_ID,
        )
        tmpl_combo_row = QHBoxLayout()
        tmpl_combo_row.addWidget(QLabel("默认模板:"))
        self.default_template = QComboBox()
        for t in BUILTIN_ATTRIBUTES_TEMPLATES:
            self.default_template.addItem(f"{t['label']}（{t['id']}）", t["id"])
        for t in (p.attribute_templates or []):
            if isinstance(t, dict) and t.get("id"):
                self.default_template.addItem(f"{t.get('label', t['id'])}（{t['id']}）", t["id"])
        idx = self.default_template.findData(DEFAULT_ATTRIBUTE_TEMPLATE_ID)
        self.default_template.setCurrentIndex(idx if idx >= 0 else 0)
        self.default_template.setToolTip("属性/装备的显示名按哪个题材模板翻译。\n如修仙模板把 力/敏 显示为 膂力/身法、金币显示为 灵石。\n只改显示名，不改任何数值逻辑；世界生成时 LLM 也会按题材标签自动选模板。")
        tmpl_combo_row.addWidget(self.default_template, 1)
        attr_layout.addLayout(tmpl_combo_row)

        # 立绘开关
        self.img_player_avatar = QCheckBox("世界生成时生成玩家立绘（增加一次生图）")
        self.img_player_avatar.setChecked(p.image_player_avatar)
        self.img_player_avatar.setToolTip("按你的开局描述生成一张玩家立绘，显示在场景页玩家状态卡上。")
        attr_layout.addWidget(self.img_player_avatar)

        # 内置模板说明
        from src.models.world import EQUIPMENT_SLOTS
        hint = QLabel(
            "内置模板：" + "、".join(t["label"] for t in BUILTIN_ATTRIBUTES_TEMPLATES) + "\n"
            "说明：底层字段（stat_str/dex/int/vit/luk + 8 槽 id）不变；模板只决定显示名题材化。\n"
            "世界生成时 LLM 据题材标签选模板，UI 显示与 LLM 上下文都走题材化字段名。\n"
            "8 槽：" + " / ".join(EQUIPMENT_SLOTS)
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7dcfff; font-size:11px;")
        attr_layout.addWidget(hint)

        form.addRow(attr_box)
        return w

    def _add_scale_row(self, sid: str = "", label: str = "", locations: int = 6,
                       npcs: int = 5, places: int = 3):
        """[P5b] 在自定义规模列表编辑器里添加一行（id/label/地点/NPC/场所 + 删除）。"""
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        id_edit = QLineEdit(sid)
        id_edit.setPlaceholderText("tiny")
        id_edit.setMaximumWidth(110)
        label_edit = QLineEdit(label)
        label_edit.setPlaceholderText("微型沙盒")
        loc_spin = _spin_int(int(locations), 1, 60)
        npc_spin = _spin_int(int(npcs), 1, 60)
        plc_spin = _spin_int(int(places), 1, 10)
        loc_spin.setMaximumWidth(70)
        npc_spin.setMaximumWidth(70)
        plc_spin.setMaximumWidth(70)
        loc_spin.setToolTip("地点数")
        npc_spin.setToolTip("NPC 数")
        plc_spin.setToolTip("每地点场所数")
        del_btn = QPushButton("删除")
        del_btn.setObjectName("dangerBtn")
        del_btn.setMaximumWidth(60)
        rl.addWidget(id_edit, 1)
        rl.addWidget(label_edit, 2)
        rl.addWidget(loc_spin, 1)
        rl.addWidget(npc_spin, 1)
        rl.addWidget(plc_spin, 1)
        rl.addWidget(del_btn, 0)
        def _on_del():
            self._scale_rows_layout.removeWidget(row)
            row.deleteLater()
        del_btn.clicked.connect(_on_del)
        # 插到 stretch 之前
        self._scale_rows_layout.insertWidget(self._scale_rows_layout.count() - 1, row)

    def _on_add_custom_scale(self):
        """[P5b] 「+ 添加」按钮：插入空白行让用户填。"""
        self._add_scale_row()

    def _collect_custom_scales(self) -> list[dict]:
        """[P5b] 从 UI 收集所有自定义规模行 -> list[dict]，去重 + 过滤空 id。"""
        out: list[dict] = []
        seen: set[str] = set()
        # 遍历 _scale_rows_layout 找 QWidget（含 2 个 QLineEdit + 3 个 QSpinBox）
        for i in range(self._scale_rows_layout.count()):
            item = self._scale_rows_layout.itemAt(i)
            w = item.widget() if item else None
            if not w:
                continue
            edits = w.findChildren(QLineEdit)
            spins = w.findChildren(QSpinBox)
            if len(edits) < 2 or len(spins) < 3:
                continue
            sid = edits[0].text().strip()
            label = edits[1].text().strip()
            if not sid or not label:
                continue
            if sid in seen:
                continue
            from src.models.world_sim_preset import _SCALE_VALUES
            if sid in _SCALE_VALUES:
                # 与内置三档冲突的 ID 跳过
                continue
            seen.add(sid)
            out.append({"id": sid, "label": label,
                        "locations": max(1, spins[0].value()),
                        "npcs": max(1, spins[1].value()),
                        "places": max(1, spins[2].value())})
        return out

    def _on_save(self):
        # [!] 先 load 再改字段（守 §15 AppConfig 保留字段模式，仿 TtsSettingsDialog）：
        # self._p 是 __init__ 时载入的快照，期间用户可能在别处改过；重新 load 保证不丢字段。
        p = self.storage.load_world_sim_preset()
        p.calculator_api_id = self.calc_api.currentData()
        p.narrative_api_id = self.narr_api.currentData()
        p.settle_system_prompt = self.settle_prompt.toPlainText() or ""
        p.narrative_system_prompt = self.narr_prompt.toPlainText() or ""
        # [P19] 文风层：空串=不注入（toPlainText 直接存，勿 or 兜底）
        p.narrative_style_prompt = self.style_prompt.toPlainText()
        p.calculator_temperature = self.calc_temp.value()
        p.calculator_max_tokens = self.calc_max.value()
        p.calculator_top_p = self.calc_top_p.value()
        p.narrative_temperature = self.narr_temp.value()
        p.narrative_max_tokens = self.narr_max.value()
        p.narrative_top_p = self.narr_top_p.value()
        p.sim_enabled = self.sim_enabled.isChecked()
        p.economy_sim = self.economy.currentData()
        p.hunger_enabled = self.hunger_enabled.isChecked()
        p.daily_hp_regen_pct = self.daily_hp_regen.value()
        p.farm_enabled = self.farm_enabled.isChecked()
        p.farm_wither_days = self.farm_wither.value()
        p.faction_war = self.faction_war.currentData()
        p.offscreen_npc_tick = self.offscreen.isChecked()
        p.reconcile_interval = self.reconcile.value()
        p.sim_budget_per_tick = self.budget.value()
        p.combat_system = self.combat.currentData()
        p.crpg_granularity = self.granularity.currentData()
        p.difficulty = self.difficulty.currentData()
        # ---- P4 世界滴答细分 ----
        p.economy_volatility = self.econ_vol.currentData()
        p.faction_war_lethality = self.fw_lethal.currentData()
        p.event_log_max = self.event_log_max.value()
        p.max_events_per_tick = self.max_events.value()
        p.key_npc_decision_enabled = self.key_npc_enabled.isChecked()
        p.key_npc_budget = self.key_npc_budget.value()
        p.sim_api_id = self.sim_api.currentData()
        p.sim_temperature = self.sim_temp.value()
        p.sim_max_tokens = self.sim_max.value()
        p.sim_system_prompt = self.sim_prompt.toPlainText() or ""
        p.image_enabled = self.img_enabled.isChecked()
        p.image_world_banner = self.img_banner.isChecked()
        p.image_location_bg = self.img_loc.isChecked()
        p.image_npc_avatar = self.img_npc.isChecked()
        p.image_legendary_item = self.img_legend.isChecked()
        p.image_normal_item = self.img_normal.isChecked()
        # [P11d] 关键事件插图（已接活，ensure_event_image 在 tick_world 事件入库后调用）
        p.image_event = self.img_event.isChecked()
        p.image_style_prefix = self.img_prefix.text().strip()
        # 场景事件生图（P2 场景交互循环，叙事 LLM 旁白 [img:...] 触发）
        p.narrative_image_event = self.narrative_img_event.isChecked()
        p.narrative_image_freq = self.narrative_img_freq.value()
        p.narrative_image_max_per_session = self.narrative_img_max.value()
        p.image_skip_allowed = self.img_skip.isChecked()
        p.default_world_scale = self.scale.currentData()
        # [P5b] 自定义规模档位：先从 UI 收集（保留空集合的"清空"语义）
        p.custom_world_scales = self._collect_custom_scales()
        # [P5b] 兜底校验：若 default_world_scale 既不在内置三档也不在 custom 里则回退 medium
        from src.models.world_sim_preset import _SCALE_VALUES
        if p.default_world_scale not in _SCALE_VALUES:
            valid_ids = {it.get("id") for it in p.custom_world_scales if isinstance(it, dict)}
            if p.default_world_scale not in valid_ids:
                p.default_world_scale = "medium"
        # [2026-09-13] 聚落数量（0=自动，LLM 自由分配）
        p.gen_settlement_count = max(0, int(self.gen_settlement_count.value()))
        tags_text = self.tags.text().strip()
        p.default_genre_tags = [t.strip() for t in tags_text.split(",") if t.strip()] if tags_text else []
        # [P5c] 玩家立绘开关（attribute_templates 不在 UI 编辑，保留 from_dict 时的值）
        p.image_player_avatar = self.img_player_avatar.isChecked()
        p.image_monster = self.img_monster.isChecked()
        p.image_home = self.img_home.isChecked()
        # [2026-09-25] 人像/怪物图纯色背景抠透明（颜色经 currentData 取白名单值）
        p.combat_intents_enabled = self.combat_intents.isChecked()
        p.image_cutout_enabled = self.img_cutout.isChecked()
        p.image_cutout_bg = self.img_cutout_bg.currentData() or "white"
        # [P6] 商店与经济旋钮
        p.shops_enabled = self.shops_enabled.isChecked()
        p.shops_restock_interval = self.shops_restock_interval.value()
        p.shops_price_drift = self.shops_price_drift.currentData()
        p.shops_llm_restock_enabled = self.shops_llm_restock.isChecked()
        # [P14] 经济节奏 + 交易回应
        p.economy_pace = self.econ_pace.currentData()
        p.trade_reply_enabled = self.trade_reply_chk.isChecked()
        p.shops_wallclock_restock_minutes = self.shops_wallclock.value()
        p.shops_system_prompt = self.shops_prompt.toPlainText()
        # [P6c] NPC 个人记忆旋钮
        p.npc_memory_enabled = self.npc_mem_enabled.isChecked()
        p.npc_memory_mode = self.npc_mem_mode.currentData()
        p.npc_memory_interval = self.npc_mem_interval.value()
        p.npc_memory_top_k = self.npc_mem_top_k.value()
        p.npc_memory_api_id = self.npc_mem_api.currentData() or ""

        # [P7g] 采集系统旋钮
        p.gathering_enabled = self.gather_enabled.isChecked()
        p.gathering_base_rate = self.gather_base_rate.value()
        p.resource_cooldown_ticks = self.res_cooldown.value()
        p.resource_richness_decay = self.res_decay.value()

        # [P7f] 地图拓展旋钮
        p.map_expansion_enabled = self.map_expand_enabled.isChecked()
        p.map_expansion_batch_size = self.map_batch.value()
        # [P11c] 新地点伴随物品数
        p.map_expansion_item_count = self.map_items.value()
        # [数据量] 拓展伴随配方/秘境/怪物物种
        p.map_expansion_recipe_count = self.map_recipes.value()
        p.map_expansion_dungeon_chance = self.map_dungeon_chance.value()
        p.map_expansion_monster_count = self.map_monsters.value()
        # [P12] 资源/配方/天赋/野外怪/NPC 自主
        p.gen_recipe_count = self.gen_recipe_count.value()
        p.item_gen_count = self.item_gen_count.value()
        p.talent_pool_size = self.talent_pool_size.value()
        p.skill_pool_size = self.skill_pool_size.value()
        p.monster_pool_size = self.monster_pool_size.value()
        p.wilderness_monsters_enabled = self.wm_enabled.isChecked()
        p.wilderness_monster_chance = self.wm_chance.value()
        p.elite_monster_chance = self.elite_chance.value()
        # [P34e] 探查/地图拓展三概率
        p.expand_new_city_chance = self.expand_city.value()
        p.expand_new_npc_chance = self.expand_npc.value()
        p.expand_new_npc_count = self.expand_npc_count.value()
        p.expand_new_faction_chance = self.expand_fac.value()
        # [P34f] 股市旋钮
        p.stock_market_enabled = self.stock_on.isChecked()
        p.stock_price_drift_pct = self.stock_drift.value()
        # [P34g] 拍卖会旋钮
        p.auction_enabled = self.auction_on.isChecked()
        p.auction_interval_days = self.auction_interval.value()
        p.auction_duration_days = self.auction_duration.value()
        p.auction_lot_count = self.auction_lots.value()
        p.npc_autonomous_enabled = self.npc_auto.isChecked()

        # [P7h] 回合制战斗旋钮
        p.combat_player_controlled = self.combat_controlled.isChecked()
        p.combat_max_rounds = self.combat_max_rounds.value()
        p.combat_enemy_delay_ms = self.combat_enemy_delay.value()
        # [P10] 遭遇判定 + 助战 + NPC 互动 + 好友
        p.combat_encounter_judge_llm = self.combat_judge_llm.isChecked()
        p.combat_max_enemies = self.combat_max_enemies.value()
        p.combat_allies_enabled = self.combat_allies.isChecked()
        p.world_boss_enabled = self.world_boss_enabled.isChecked()
        p.world_boss_min_danger = self.world_boss_min_danger.value()
        p.world_boss_window_days = self.world_boss_window_days.value()
        p.relations_enabled = self.relations_enabled.isChecked()
        p.sworn_damage_bonus = self.sworn_damage_bonus.value()
        p.places_enabled = self.places_enabled.isChecked()
        p.places_per_location = self.places_per_location.value()
        p.companion_interject_enabled = self.companion_interject_enabled.isChecked()
        p.companion_interject_interval = self.companion_interject_interval.value()
        p.hidden_check_enabled = self.hidden_check_enabled.isChecked()
        p.combat_max_allies = self.combat_max_allies.value()
        p.npc_reactions_enabled = self.npc_reactions.isChecked()
        p.npc_gift_chance = self.npc_gift_chance.value()
        p.friend_affinity_threshold = self.friend_threshold.value()
        p.friend_chat_enabled = self.friend_chat.isChecked()
        p.friend_chat_proactive_enabled = self.friend_chat_proactive.isChecked()
        # [P10b] 同行
        p.companions_enabled = self.companions_chk.isChecked()
        p.companion_affinity_threshold = self.companion_threshold.value()
        p.companion_max_followers = self.companion_max.value()

        # [P7i] 任务系统旋钮
        p.quest_system_enabled = self.quest_enabled.isChecked()

        # [P15a] 场景日志滚动总结旋钮
        p.scene_summary_threshold = self.scene_summary_threshold.value()
        p.scene_summary_max_chars = self.scene_summary_max_chars.value()

        # [P7k8] 永久死亡旋钮
        p.permadeath_enabled = self.permadeath_chk.isChecked()
        p.npc_permadeath = self.npc_permadeath_chk.isChecked()
        p.npc_backfill_enabled = self.npc_backfill_chk.isChecked()
        # [P38a] NPC 社交演化开关
        p.social_enabled = self.social_chk.isChecked()
        # [P39b] 委托订单开关
        p.commissions_enabled = self.commission_chk.isChecked()
        # [P39d+e] 编年史开关
        p.chronicle_enabled = self.chronicle_chk.isChecked()
        # [剧情线 P46] 旋钮（_on_save 先 load 再改，守新旋钮补 setToolTip 契约）
        p.story_arcs_enabled = self.story_arc_chk.isChecked()
        p.arc_interval_days = self.arc_interval.value()
        p.arc_max_active = self.arc_max_active.value()
        # [P42c] 跑腿开关
        p.errands_enabled = self.errand_chk.isChecked()

        self.storage.save_world_sim_preset(p)
        QMessageBox.information(self, "已保存", "世界模拟设置已保存。")
        self.accept()
