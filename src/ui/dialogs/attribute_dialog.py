"""[P7d2] 属性加点对话框：分配升级获得的属性点到 str/dex/int/vit/luk。

入口：场景页玩家区「加点」按钮（stat_points > 0 时显示）。加点后重算 hp_max/mp_max 上限
（vit 影响 HP 上限，int 影响 MP 上限）。完全代码驱动，不用 LLM。

[UI 改造 2026-10-01 第三批] 暗金古卷设计系统：属性=卡片行（题材名徽章 + 大号数值 +
装备/天赋加成注记），可分配点金顶线强调卡；加点逻辑与 stat_labels/stat_btns 契约不变。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from src.models.world_sim_preset import GenreText
from src.services import combat_engine as ce
from src.ui.widgets.game_widgets import GameCard, game_badge, banner_rule, card_sub

# (key, 默认中文名, 生效点说明)；显示名优先走 GenreText 题材化（§23，与
# attribute_alloc_dialog / 场景页左栏同口径——硬编码「力/敏」会与题材名
# 「蛮力/身法/灵根」脱节，三次真人测试反馈）。
_STAT_INFO = [
    ("str", "力", "攻击/暴击伤害/招架"),
    ("dex", "敏", "命中/暴击/速度/闪避"),
    ("int", "智", "法术攻击/治疗/法力"),
    ("vit", "耐", "防御/生命上限"),
    ("luk", "运", "速度/掉落"),
]

# 属性卡数值大字色（金），加成注记（灰蓝）
_TONE_KEY = {"str": "danger", "dex": "success", "int": "info",
             "vit": "gold", "luk": "warn"}


class AttributeDialog(QDialog):
    """属性加点：每点 +1 某属性，消耗 1 stat_point。"""

    def __init__(self, world, on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.on_changed = on_changed
        # [§23] 属性显示名跟随题材（world.config_overlay 驱动，与场景页左栏同口径）
        self.gt = GenreText(getattr(world, "config_overlay", None) or {})
        self.setWindowTitle("属性加点")
        self.resize(430, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        title = QLabel("天赋根骨")
        title.setObjectName("bannerName")
        outer.addWidget(title)
        outer.addWidget(banner_rule())

        # 可分配点（金顶线强调卡）
        pts = GameCard(gold=True)
        head = QWidget()
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(8)
        hh.addWidget(QLabel("尚可淬炼"))
        hh.addStretch()
        self.points_lbl = QLabel()
        self.points_lbl.setStyleSheet("color:#e0c48f; font-weight:bold; font-size:18px;")
        hh.addWidget(self.points_lbl)
        pts.add(head)
        _s = self.gt.stat
        pts.add(card_sub(f"升级自动获得属性点（每级 +3）。{_s('str')}影响攻击，"
                         f"{_s('dex')}影响命中/闪避，{_s('int')}影响法术，"
                         f"{_s('vit')}影响生命，{_s('luk')}影响掉落。"))
        outer.addWidget(pts)

        # 五维属性卡（题材名徽章 + 大号数值 + 装备/天赋加成 + 加点按钮）
        self.stat_labels: dict = {}
        self.stat_btns: dict = {}
        self._stat_bonus: dict = {}
        for key, name, desc in _STAT_INFO:
            card = GameCard()
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)
            disp = self.gt.stat(key) or name
            h.addWidget(game_badge(disp, _TONE_KEY.get(key, "info")))
            txt_col = QWidget()
            vc = QVBoxLayout(txt_col)
            vc.setContentsMargins(0, 0, 0, 0)
            vc.setSpacing(1)
            val = QLabel()
            val.setStyleSheet("color:#e8d5ae; font-size:17px; font-weight:bold;")
            self.stat_labels[key] = val
            vc.addWidget(val)
            bonus = QLabel(desc)
            bonus.setObjectName("gameCardSub")
            self._stat_bonus[key] = bonus
            vc.addWidget(bonus)
            h.addWidget(txt_col, 1)
            btn = QPushButton("+1 淬炼")
            btn.setObjectName("optionBtn")
            btn.setFixedWidth(80)
            btn.clicked.connect(lambda _=False, k=key: self._add(k))
            self.stat_btns[key] = btn
            h.addWidget(btn, 0)
            card.add(row)
            outer.addWidget(card)
        outer.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        outer.addLayout(row)
        self._refresh()

    def _refresh(self):
        p = self.world.player
        self.points_lbl.setText(f"{int(p.stat_points or 0)} 点")
        for key, name, desc in _STAT_INFO:
            val = int(getattr(p, f"stat_{key}", 0) or 0)
            # 生效值（含装备/天赋加成，与背包/场景页同口径的单一来源）
            try:
                eff = ce.primary_stat_display(self.world, p, key)
            except Exception:
                eff = str(val)
            self.stat_labels[key].setText(f"{val}　生效 {eff}")
            bonus_txt = desc
            try:
                if str(eff) != str(val):
                    bonus_txt = f"{desc}（含装备/天赋加成）"
            except Exception:
                pass
            self._stat_bonus[key].setText(bonus_txt)
        for btn in self.stat_btns.values():
            btn.setEnabled(int(p.stat_points or 0) > 0)

    def _add(self, key: str):
        p = self.world.player
        if int(p.stat_points or 0) <= 0:
            return
        p.stat_points = int(p.stat_points) - 1
        cur = int(getattr(p, f"stat_{key}", 0) or 0)
        setattr(p, f"stat_{key}", cur + 1)
        # 重算 hp_max（vit 影响）/ mp_max（int 影响），hp/mp 钳制到新上限
        try:
            new_hp_max = ce.max_hp_for(p)
            p.hp_max = new_hp_max
            p.hp = min(int(p.hp or 0), new_hp_max)
            # [C3] 天赋 mp_bonus 同口径接线（与 _init_combat_stats 一致）
            from src.services import talent_engine as te
            p.mp_max = te.effective_stat(p, "int") * 5 + int(p.level) * 3 \
                + int(te.get_talent_bonus(p, "mp_bonus") or 0)
            p.mp = min(int(p.mp or 0), p.mp_max)
        except Exception:
            pass
        if self.on_changed is not None:
            try:
                self.on_changed()
            except Exception:
                pass
        self._refresh()
