"""[P11b] 开局属性分配对话框：5 维属性玩家自选分配 + QQ 彩蛋码。

流程：世界生成 -> 预览 -> 属性分配（本对话框）-> 天赋选择 -> 保存。
- 5 维属性（力/敏/智/耐/运）各从 5 点凡人基线起步（引擎不再按职业预设属性）。
- 基础 10 点由玩家自由分配；输入作者彩蛋码 1965699077 -> 额外 +50（共 60 点）。
- 剩余点数保留为 stat_points（进游戏后可在「加点」面板继续分配，与升级加点同口径）。
- 确认时重算 hp_max（耐*10+等级*5）/ mp_max（智*5+等级*3）并回满。
- 职业仅决定初始技能与推荐倾向提示，不再强制属性（推荐仅作参考文案）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QLineEdit,
)

from src.services import combat_engine as ce
from src.models.world_sim_preset import GenreText

# [!] 作者彩蛋码（复用 §16 AUTHOR_QQ / P9 AUTHOR_TALENT_CODE 同码；属性点 10 -> 60）
AUTHOR_ATTR_CODE = "1965699077"

BASE_STAT = 5        # 每维凡人基线（与 NPC 模型默认一致）
BASE_POINTS = 10     # 基础可分配点数
BONUS_POINTS = 50    # 彩蛋码额外点数

# (key, 默认中文名, 生效点说明)；显示名优先走 GenreText 题材化（§24）
_STAT_KEYS = [
    ("str", "力", "攻击/暴击伤害/招架/采集(矿木)/奇遇破阵"),
    ("dex", "敏", "命中/暴击/闪避/速度/采集(草药)/奇遇闪避"),
    ("int", "智", "法术攻击/治疗/MP/合成成功率/奇遇参悟"),
    ("vit", "耐", "防御/HP 上限/负重上限/奇遇抗异变"),
    ("luk", "运", "速度/掉落率/掉落稀有度/暴击/逃跑/奇遇触发"),
]

# 职业推荐倾向（仅参考文案；不强制属性）
_CLASS_HINTS = [
    (("战士", "骑士", "武士", "蛮"), "近战路线：推荐主加 力(输出) 与 耐(生存)，敏适量"),
    (("法师", "术士", "巫", "魔"), "施法路线：推荐主加 智(法攻/MP)，耐保底生存"),
    (("盗贼", "刺客", "游侠", "弓"), "敏捷路线：推荐主加 敏(命中/暴击/闪避)，运提升掉落"),
]


def _class_hint(class_name: str) -> str:
    for keys, hint in _CLASS_HINTS:
        if any(k in (class_name or "") for k in keys):
            return hint
    return "自由发展：按你想玩的流派自由分配"


class AttributeAllocDialog(QDialog):
    """开局属性分配：5 维各 5 点基线 + 点数自由分配（10 点 / 彩蛋 60 点）。"""

    def __init__(self, world, parent=None):
        super().__init__(parent)
        self.world = world
        self.gt = GenreText(getattr(world, "config_overlay", None) or {})
        # [!] 固定从凡人基线起步：不读 player 当前值（PlayerState 默认 10 / 老存档各异），
        # 开局属性完全由玩家分配（引擎基线也统一 5，见 _init_combat_stats）。
        self.values = {k: BASE_STAT for k, _, _ in _STAT_KEYS}
        self.max_points = BASE_POINTS

        self.setWindowTitle("开局属性")
        self.resize(560, 560)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        title = QLabel("开局属性")
        title.setObjectName("titleLabel")
        outer.addWidget(title)
        cls = self.world.player.class_name or "无职业"
        desc = QLabel(f"职业「{cls}」只决定初始技能与推荐倾向，属性完全由你分配。"
                      f"每维从 {BASE_STAT} 点凡人基线起步，把点数加到你想要的方向。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(desc)

        # 职业推荐提示
        hint = QLabel(f"推荐：{_class_hint(self.world.player.class_name)}")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7dcfff; font-size:12px;")
        outer.addWidget(hint)

        # QQ 彩蛋输入框
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("彩蛋码（可选）:"))
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText(f"输入作者彩蛋码属性点 +{BONUS_POINTS}")
        self.code_input.textChanged.connect(self._on_code_changed)
        code_row.addWidget(self.code_input, 1)
        outer.addLayout(code_row)

        # 点数显示
        self.points_lbl = QLabel("")
        self.points_lbl.setStyleSheet("color:#e0af68; font-weight:bold; font-size:13px;")
        outer.addWidget(self.points_lbl)

        # 5 维属性行
        self._minus_btns: dict = {}
        self._plus_btns: dict = {}
        self._val_lbls: dict = {}
        for key, name, effect in _STAT_KEYS:
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 6, 10, 6)
            rl.setSpacing(8)
            disp = self.gt.stat(key) or name
            lbl = QLabel()
            lbl.setToolTip(f"{disp}（{name}）：{effect}")
            lbl.setStyleSheet("color:#c0caf5;")
            self._val_lbls[key] = lbl
            rl.addWidget(lbl, 1)
            minus = QPushButton("-1")
            minus.setFixedWidth(48)
            minus.clicked.connect(lambda _=False, k=key: self._adjust(k, -1))
            self._minus_btns[key] = minus
            rl.addWidget(minus)
            plus = QPushButton("+1")
            plus.setFixedWidth(48)
            plus.setObjectName("optionBtn")
            plus.clicked.connect(lambda _=False, k=key: self._adjust(k, 1))
            self._plus_btns[key] = plus
            rl.addWidget(plus)
            outer.addWidget(row)

        # 派生预览（hp/mp 随分配实时变化）
        self.derived_lbl = QLabel("")
        self.derived_lbl.setWordWrap(True)
        self.derived_lbl.setStyleSheet("color:#7dcfff; font-size:12px;")
        outer.addWidget(self.derived_lbl)

        outer.addStretch()
        # 确认/取消
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QPushButton("确认")
        self.ok_btn.setObjectName("optionBtn")
        self.ok_btn.setStyleSheet("QPushButton{background:#1f3a2a; color:#9ece6a;}")
        self.ok_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.ok_btn)
        outer.addLayout(btn_row)

    # ---- 状态 ----

    def _spent(self) -> int:
        return sum(self.values[k] - BASE_STAT for k, _, _ in _STAT_KEYS)

    def _remaining(self) -> int:
        return self.max_points - self._spent()

    def _code_active(self) -> bool:
        return self.code_input.text().strip() == AUTHOR_ATTR_CODE

    def _on_code_changed(self, text: str):
        self.max_points = BASE_POINTS + BONUS_POINTS if self._code_active() else BASE_POINTS
        self._refresh()

    def _adjust(self, key: str, delta: int):
        cur = self.values[key]
        if delta > 0 and self._remaining() <= 0:
            return
        if delta < 0 and cur <= BASE_STAT:
            return
        self.values[key] = cur + delta
        self._refresh()

    def _refresh(self):
        spent = self._spent()
        remaining = self._remaining()
        if self._code_active():
            self.points_lbl.setText(
                f"彩蛋已解锁！属性点：{self.max_points}（已用 {spent}，剩余 {max(0, remaining)}）")
            self.points_lbl.setStyleSheet("color:#e0af68; font-weight:bold; font-size:13px;")
        elif remaining < 0:
            # 彩蛋码输对又删掉的边界：已分配超过新上限，红字提示须 -1 退回
            self.points_lbl.setText(
                f"可分配属性点：0/{self.max_points}（超限 {-remaining} 点，请先 -1 退回）")
            self.points_lbl.setStyleSheet("color:#f7768d; font-weight:bold; font-size:13px;")
        else:
            self.points_lbl.setText(f"可分配属性点：{remaining}/{self.max_points}")
            self.points_lbl.setStyleSheet("color:#e0af68; font-weight:bold; font-size:13px;")
        for key, name, effect in _STAT_KEYS:
            disp = self.gt.stat(key) or name
            self._val_lbls[key].setText(f"{disp}（{name}·{effect}）：{self.values[key]}")
            self._minus_btns[key].setEnabled(self.values[key] > BASE_STAT)
            self._plus_btns[key].setEnabled(remaining > 0)
        # 派生预览：HP=vit*10+lv*5 / MP=int*5+lv*3（ce.max_hp_for 同口径）
        lv = max(1, int(getattr(self.world.player, "level", 1) or 1))
        hp = self.values["vit"] * 10 + lv * 5
        mp = self.values["int"] * 5 + lv * 3
        self.derived_lbl.setText(f"预览：HP 上限 {hp}（耐 {self.values['vit']} x10 + 等级 {lv} x5）  "
                                 f"MP 上限 {mp}（智 {self.values['int']} x5 + 等级 {lv} x3）")
        # 未分配完也可确认（剩余点保留进游戏再加），但不可超限
        self.ok_btn.setEnabled(remaining >= 0)

    def _on_confirm(self):
        """确认：落 player 5 维属性 + 剩余点转 stat_points + 重算 hp/mp 上限并回满。"""
        p = self.world.player
        for key, _, _ in _STAT_KEYS:
            setattr(p, f"stat_{key}", int(self.values[key]))
        p.stat_points = max(0, self._remaining())
        try:
            new_hp_max = ce.max_hp_for(p)
            p.hp_max = new_hp_max
            p.hp = new_hp_max
            # [C3] 天赋 mp_bonus 同口径接线（与 _init_combat_stats 一致）
            from src.services import talent_engine as te
            p.mp_max = te.effective_stat(p, "int") * 5 + int(p.level) * 3 \
                + int(te.get_talent_bonus(p, "mp_bonus") or 0)
            p.mp = p.mp_max
        except Exception:
            pass
        self.accept()
