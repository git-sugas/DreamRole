"""[P25d] 世界 Boss 讨伐对话框（Boss 信息/形态进度/援护状态/窗口倒计时/挑战）。

纯直改模式（仿 DungeonDialog 轻量）：「挑战」发 challenge_requested 信号由场景页
起 CombatDialog(skip_judge_with=...)（prepare_world_boss_combat 喂入 Boss 临时单位 +
确定性小弟 + 声望援护）；对话框随即关闭防与战斗并发。无 LLM 调用故无 worker。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QScrollArea, QFrame, QWidget, QPushButton,
)
from PySide6.QtCore import Signal

from src.models.world import WorldBoss


_FORM_TACTICS = {
    "aggressive": "猛攻：常用攻击技能，观察敌情预告准备防御或打断",
    "defensive": "守势：低血量可能举盾，普攻会进入攻防对撞",
    "caster": "施法：耗灵力释放术式，并可能附加负面状态",
    "balanced": "多变：普攻与技能交替，需要留意每回合敌情",
}
_MECHANIC_TACTICS = {
    "shield": "护盾：普攻伤害降低，技能和同伴攻击可绕过；普攻能消耗盾值",
    "summon_tide": "召唤潮：每 3 回合可能增援，优先清理前排爪牙",
    "enrage_timer": "狂暴计时：拖久后攻击增强，尽量在狂暴前结束战斗",
}


class WorldBossDialog(QDialog):
    """[P25d] 世界 Boss 页：展示 Boss 信息 + 多形态进度 + 援护 + 窗口倒计时 + 挑战入口。"""

    challenge_requested = Signal(object)   # 发 WorldBoss，场景页起 CombatDialog skip_judge

    def __init__(self, world, boss: WorldBoss, world_sim_service, parent=None):
        super().__init__(parent)
        self.world = world
        self.boss = boss
        self.svc = world_sim_service
        self.setWindowTitle(f"世界 Boss：{boss.name}")
        self.resize(560, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        # 提示
        hint = QLabel("世界级威胁盘踞此地，窗口期内可挑战。Boss 有多个形态，逐形态讨伐——"
                      "每击败一个形态它会变身更强（你可回到场景休整回血再战下一形态）。"
                      "击败最终形态：声望变动 + 战后拾取奖励。窗口到期未讨伐则离去留传闻。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        # Boss 概览
        day = int(getattr(world, "day_count", 1) or 1)
        left = max(0, int(boss.window_until_day or 0) - day)
        gt = None
        try:
            gt = self.svc._genre_text(world)
        except Exception:
            from src.models.world_sim_preset import GenreText
            gt = GenreText(getattr(world, "config_overlay", None) or {})
        cur = getattr(gt, "currency", "金币")
        overview = QLabel(
            f"<b style='color:#f7768e;'>{boss.name}</b>\n"
            f"{boss.desc}\n"
            f"等级 {boss.level}　危险度 {boss.danger}　余约 {left} 天（到期离去）"
        )
        overview.setWordWrap(True)
        overview.setStyleSheet("background:#2a1f2e; border-radius:8px; padding:10px; color:#c0caf5;")
        body.addWidget(overview)

        # 形态进度
        fidx = max(0, min(len(boss.forms) - 1, int(boss.current_form_index)))
        forms_lbl = QLabel("【形态进度】")
        forms_lbl.setStyleSheet("color:#7dcfff; font-weight:bold;")
        body.addWidget(forms_lbl)
        for i, form in enumerate(boss.forms):
            mark = "▶" if i == fidx else ("✓" if i < fidx else "·")
            state = "（当前）" if i == fidx else ("（已破）" if i < fidx else "")
            row = QLabel(
                f"{mark} 第{i + 1}形态「{form.name}」{state}　"
                f"血量×{form.hp_mult:.1f}　技能：{form.skill_name}　随从×{form.minion_count}\n"
                f"战法：{_FORM_TACTICS.get(form.ai, _FORM_TACTICS['balanced'])}"
            )
            row.setWordWrap(True)
            color = "#e0af68" if i == fidx else ("#9ece6a" if i < fidx else "#565f89")
            row.setStyleSheet(f"color:{color}; font-size:12px; padding-left:8px;")
            body.addWidget(row)

        mechanics = [str(x) for x in (boss.mechanics or []) if str(x) in _MECHANIC_TACTICS]
        if mechanics:
            mech_lbl = QLabel("【特殊机制】\n" + "\n".join(_MECHANIC_TACTICS[x] for x in mechanics))
            mech_lbl.setWordWrap(True)
            mech_lbl.setStyleSheet("color:#bb9af7; font-size:12px; padding:8px;")
            body.addWidget(mech_lbl)

        # 援护状态
        rein_line = "【援护】无（无势力声望达 自己人 以上）"
        if boss.reinforcement_npc_id:
            rn = next((n for n in (getattr(world, "npcs", None) or [])
                       if getattr(n, "id", "") == boss.reinforcement_npc_id), None)
            fac = next((f.name for f in (getattr(world, "factions", None) or [])
                        if f.id == boss.reinforcement_faction_id), "")
            rn_name = rn.name if rn is not None else boss.reinforcement_npc_id
            rein_line = f"【援护】{fac} 已遣 {rn_name} 前来助阵（一次性）"
        rein_lbl = QLabel(rein_line)
        rein_lbl.setWordWrap(True)
        rein_lbl.setStyleSheet("color:#9aa5ce; font-size:12px;")
        body.addWidget(rein_lbl)

        # 奖励预览（显示名走 GenreText）
        reward_lbl = QLabel(
            f"【击杀奖励】{cur}（{(50 + boss.danger * 20)}基数×节奏）+ 目标传说级掉落（物品池缺档则降档，战后拾取）"
            "+ 全势力声望变动（关联势力降、敌对势力升）"
        )
        reward_lbl.setWordWrap(True)
        reward_lbl.setStyleSheet("color:#9aa5ce; font-size:12px;")
        body.addWidget(reward_lbl)

        body.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # 按钮
        btn_lay = QVBoxLayout()
        btn_lay.setSpacing(6)
        self.challenge_btn = QPushButton(f"挑战「{boss.name}」（第{fidx + 1}形态）")
        self.challenge_btn.setToolTip("开启回合制战斗（Boss + 确定性小弟 + 声望援护）；"
                                      "战胜当前形态后 Boss 变身更强，你可休整后再战下一形态")
        self.challenge_btn.clicked.connect(self._on_challenge)
        btn_lay.addWidget(self.challenge_btn)
        leave_btn = QPushButton("离开")
        leave_btn.clicked.connect(self.reject)
        btn_lay.addWidget(leave_btn)
        outer.addLayout(btn_lay)

    def _on_challenge(self):
        """发信号让场景页起战斗（对话框关闭防与战斗并发）。"""
        self.challenge_requested.emit(self.boss)
        self.accept()
