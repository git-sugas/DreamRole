"""[P9] 天赋详情对话框（只读）：展示玩家已选天赋 + effects。[P16] 后天不可购买，奇遇觉醒提示。

入口：场景页玩家区「天赋」按钮。复用 talent_select_dialog._effects_summary 翻译 effects。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QWidget,
)

from src.models.world import Talent
from src.models.world_sim_preset import GenreText
from src.ui.dialogs.talent_select_dialog import _effects_summary


class TalentViewDialog(QDialog):
    """天赋详情（只读）。"""

    def __init__(self, world, parent=None):
        super().__init__(parent)
        self.world = world
        self.gt = GenreText(getattr(world, "config_overlay", None) or {})
        self.setWindowTitle("天赋详情")
        self.resize(540, 600)
        from src.ui.widgets.game_widgets import (banner_rule, card_sub,
                                                 game_badge, GameCard)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        # [深改 2026-10-01] 页名大字 + 金笔分隔 + 徽章化信息行（旧金色粗体一行平铺）
        title = QLabel("天赋详情")
        title.setObjectName("bannerName")
        outer.addWidget(title)
        outer.addWidget(banner_rule())
        outer.addSpacing(2)

        p = world.player
        info_row = QWidget()
        ih = QHBoxLayout(info_row)
        ih.setContentsMargins(0, 0, 0, 0)
        ih.setSpacing(8)
        ih.addWidget(game_badge(f"已选 {len(p.talents or [])} 个天赋", "gold"))
        ih.addStretch()
        outer.addWidget(info_row)
        hint = card_sub("后天无法购买，新天赋只能通过奇遇（参悟/传承/异变类）概率觉醒")
        outer.addWidget(hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = QWidget()
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(2, 2, 2, 2)
        self.content_lay.setSpacing(6)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

        talents = [Talent.from_dict(t) for t in (p.talents or []) if isinstance(t, dict)]
        if not talents:
            empty_card = GameCard()
            empty_card.set_title("尚未选择任何天赋")
            self.content_lay.addWidget(empty_card)
        for t in talents:
            self.content_lay.addWidget(self._card(t))
        self.content_lay.addStretch()

        close_btn = QPushButton("关闭")

        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

    def _rarity_color(self, rarity: str) -> str:
        return {"common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
                "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555"}.get(rarity, "#9aa5ce")

    def _card(self, t: Talent) -> QFrame:
        """[深改] GameCard[rarity] 品级左边线（品级色只留名字+左边线，不再满幅彩底）。"""
        from src.ui.widgets.game_widgets import GameCard, card_sub
        card = GameCard()
        # [深改] 品级左边线走 gameCard[rarity] QSS（与图鉴物品卡同语言）
        card.setProperty("rarity", str(t.rarity or "common"))
        color = self._rarity_color(t.rarity)
        lay = card.content
        rar_zh = self.gt.rarity(t.rarity)
        name = QLabel(f"【{t.name}】（{rar_zh} · {t.effective_cost()}点）")
        name.setStyleSheet(f"color:{color}; font-weight:bold;")
        name.setWordWrap(True)
        lay.addWidget(name)
        if t.desc:
            lay.addWidget(card_sub(t.desc))
        eff = _effects_summary(t.effects, t.element, self.gt)
        eff_lbl = QLabel(f"效果：{eff}")
        eff_lbl.setWordWrap(True)
        eff_lbl.setStyleSheet("color:#e0c48f; font-size:11px;")
        lay.addWidget(eff_lbl)
        return card
