"""[P9] 开局天赋选择对话框：随机锁死 1 个 + 天赋点自选（cost 驱动）+ QQ 彩蛋码。

流程：世界生成 -> 预览 -> 天赋选择（本对话框）-> 保存。
- 默认天赋点 10（每天赋按 rarity 收费 1-5 点），输入作者彩蛋码 1965699077 -> +50（共 60 点）。
- 锁死天赋：SeededRng 从世界天赋池确定性抽 1 个（不可改，每世界固定）。
- 自选：勾选天赋池其余项，剩余点数 >= cost 才允许；超限禁选。
- 选完落 player.talents（含锁死）+ player.talent_points（剩余）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea,
    QWidget, QLineEdit,
)

from src.utils.rng import SeededRng
from src.models.world_sim_preset import GenreText
from src.models.world import Talent

# [!] 作者彩蛋码（复用 §16 AUTHOR_QQ；[P12] 输入此码天赋点 10 -> 60，与属性彩蛋同口径 +50）
AUTHOR_TALENT_CODE = "1965699077"
TALENT_BONUS_POINTS = 50   # [P12] 彩蛋码额外点数（原 89 -> 50，与属性分配同口径）


def _effects_summary(effects: dict, element: str, gt: GenreText) -> str:
    """把天赋 effects dict 翻译成人话摘要（供 UI 一行展示）。"""
    if not isinstance(effects, dict) or not effects:
        return "无加成"
    parts = []
    elem_zh = {"fire": "火系", "thunder": "雷系", "ice": "冰系", "wind": "风系",
               "wood": "木系", "metal": "金系", "earth": "土系", "light": "光系",
               "dark": "暗系", "physical": "物理"}.get(element, "")
    for k, v in effects.items():
        if k == "skill_dmg_mult":
            scope = f"{elem_zh}" if element else "技能"
            parts.append(f"{scope}伤害+{int((v - 1) * 100)}%")
        elif k == "atk_mult":
            parts.append(f"攻击+{int((v - 1) * 100)}%")
        elif k == "gather_mult":
            parts.append(f"采集+{int((v - 1) * 100)}%")
        elif k == "craft_mult":
            parts.append(f"合成+{int((v - 1) * 100)}%")
        elif k == "check_mult":
            parts.append(f"检定+{int((v - 1) * 100)}%")
        elif k == "loot_mult":
            parts.append(f"掉落+{int((v - 1) * 100)}%")
        elif k == "luck_bonus":
            parts.append(f"幸运+{v}")
        elif k == "mp_bonus":
            parts.append(f"法力+{v}")
        elif k == "stat_bonus" and isinstance(v, dict):
            stat_names = {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"}
            sb_parts = [f"{stat_names.get(sk, sk)}+{sv}" for sk, sv in v.items()]
            if sb_parts:
                parts.append("、".join(sb_parts))
    return "，".join(parts) if parts else "无加成"


class TalentSelectDialog(QDialog):
    """开局天赋选择：锁死 1 随机 + 天赋点自选（cost 驱动）。"""

    def __init__(self, world, parent=None):
        super().__init__(parent)
        self.world = world
        self.max_points = 10                      # 默认 10 点；QQ 码 -> 60
        self.gt = GenreText(getattr(world, "config_overlay", None) or {})
        # 锁死天赋：从天赋池确定性抽 1 个（每世界固定，用 world.id 作种子）
        pool = list(getattr(world, "talent_pool", []) or [])
        rng = SeededRng.seed_from(world.id, 0, "locked_talent")
        self.locked_talent = rng.pick(pool) if pool else None
        # 自选状态：{talent_id: talent_dict}
        self.selected: dict[str, dict] = {}
        # 可选池 = 全池 - 锁死
        self.pool = [t for t in pool if not (self.locked_talent and t.get("id") == self.locked_talent.get("id"))]

        self.setWindowTitle("开局天赋")
        self.resize(620, 640)
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        from src.ui.widgets.game_widgets import banner_rule, card_title, card_sub
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(8)
        # [深改 2026-10-01] 页名大字 + 金笔分隔 + 金字区头（旧三色区头/紫底行杂乱）
        title = QLabel("开局天赋")
        title.setObjectName("bannerName")
        outer.addWidget(title)
        outer.addWidget(banner_rule())
        outer.addSpacing(2)
        desc = card_sub("命运随机锁死 1 个天赋（不可改），余下用天赋点自选。强天赋消耗更多点数。")
        outer.addWidget(desc)

        # QQ 彩蛋输入框
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("彩蛋码（可选）:"))
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText(f"输入作者彩蛋码天赋点 +{TALENT_BONUS_POINTS}")
        self.code_input.textChanged.connect(self._on_code_changed)
        code_row.addWidget(self.code_input, 1)
        outer.addLayout(code_row)

        # 点数徽章（超限变红，_refresh 动态切 level）
        self.points_lbl = QLabel("")
        self.points_lbl.setObjectName("gameBadge")
        self.points_lbl.setProperty("level", "gold")
        outer.addWidget(self.points_lbl)

        # 锁死天赋区（卡片化，_refresh 重建）
        outer.addWidget(self._sec_header("命运天赋（锁死）"))
        self.locked_host = QWidget()
        self.locked_lay = QVBoxLayout(self.locked_host)
        self.locked_lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.locked_host)

        # 自选池
        outer.addWidget(self._sec_header("天赋池（自选）"))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = QWidget()
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(2, 2, 2, 2)
        self.content_lay.setSpacing(6)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

        # 确认/取消
        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QPushButton("确认")
        self.ok_btn.setObjectName("primaryBtn")
        self.ok_btn.clicked.connect(self._on_confirm)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.ok_btn)
        outer.addLayout(btn_row)

    @staticmethod
    def _sec_header(text: str) -> QWidget:
        from src.ui.widgets.game_widgets import card_title
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(1)
        col.addWidget(card_title(text))
        return host

    def _talent_cost(self, t: dict) -> int:
        return Talent.from_dict(t).effective_cost()

    def _rarity_color(self, rarity: str) -> str:
        return {"common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
                "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555"}.get(rarity, "#9aa5ce")

    def _on_code_changed(self, text: str):
        code = (text or "").strip()
        if code == AUTHOR_TALENT_CODE:
            self.max_points = 10 + TALENT_BONUS_POINTS
        else:
            self.max_points = 10
        # 点数徽章文案/配色统一由 _refresh 管理（level 属性切换）
        self._refresh()

    def _code_active(self) -> bool:
        return self.code_input.text().strip() == AUTHOR_TALENT_CODE

    def _used_points(self) -> int:
        return sum(self._talent_cost(t) for t in self.selected.values())

    def _refresh(self):
        # 点数徽章（level 动态切换后须 unpolish/polish 强制重算 QSS）
        used = self._used_points()
        _lvl = "gold"
        if self._code_active():
            self.points_lbl.setText(f"彩蛋已解锁！天赋点：{10 + TALENT_BONUS_POINTS}（已用 {used}）")
        else:
            over = used > self.max_points
            _lvl = "danger" if over else "gold"
            over_txt = "（超限！）" if over else ""
            self.points_lbl.setText(f"天赋点：{used}/{self.max_points}{over_txt}")
        self.points_lbl.setProperty("level", _lvl)
        self.points_lbl.style().unpolish(self.points_lbl)
        self.points_lbl.style().polish(self.points_lbl)
        # 锁死天赋（卡片化重建）
        while self.locked_lay.count():
            it = self.locked_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if self.locked_talent:
            self.locked_lay.addWidget(self._locked_card(self.locked_talent))
        else:
            from src.ui.widgets.game_widgets import GameCard
            empty = GameCard()
            empty.set_title("（本世界无天赋池，跳过锁死）")
            self.locked_lay.addWidget(empty)
        # 自选池卡片
        while self.content_lay.count():
            it = self.content_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for t in self.pool:
            self.content_lay.addWidget(self._talent_card(t))
        self.content_lay.addStretch()
        # 确认按钮：超限禁用
        self.ok_btn.setEnabled(used <= self.max_points)

    def _locked_card(self, t: dict):
        """[深改] 命运锁死天赋卡：gameCard[gold] + 品级彩名（旧紫底整行富文本）。"""
        from src.ui.widgets.game_widgets import GameCard, game_badge, card_sub
        card = GameCard(gold=True)
        rar = self.gt.rarity(t.get("rarity", "common"))
        cost = self._talent_cost(t)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        name = QLabel(f"【{t.get('name', '')}】")
        name.setStyleSheet(f"color:{self._rarity_color(t.get('rarity', 'common'))};"
                           "font-weight:bold; font-size:14px;")
        hl.addWidget(name)
        hl.addWidget(game_badge(f"{rar} · 占 {cost} 点·免费锁死", "gold"))
        hl.addStretch()
        card.add(head)
        if t.get("desc"):
            card.add(card_sub(t["desc"]))
        eff = _effects_summary(t.get("effects", {}), t.get("element", ""), self.gt)
        eff_lbl = QLabel(f"效果：{eff}")
        eff_lbl.setWordWrap(True)
        eff_lbl.setStyleSheet("color:#e0c48f; font-size:11px;")
        card.add(eff_lbl)
        return card

    def _talent_card(self, t: dict) -> QFrame:
        from src.ui.widgets.game_widgets import GameCard, game_badge, card_sub
        tid = t.get("id", "")
        cost = self._talent_cost(t)
        rarity = t.get("rarity", "common")
        rar_zh = self.gt.rarity(rarity)
        color = self._rarity_color(rarity)
        is_sel = tid in self.selected
        remaining = self.max_points - self._used_points()
        can_select = (not is_sel) and (remaining >= cost)

        # [深改] gameCard[rarity] 品级左边线；选中态叠 gold 顶线（旧绿底整卡变色）
        card = GameCard(gold=is_sel)
        card.setProperty("rarity", str(rarity or "common"))
        cl = card.content
        # 名 + 稀有度 + cost
        row1 = QHBoxLayout()
        name_lbl = QLabel(f"【{t.get('name', '')}】")
        name_lbl.setStyleSheet(f"color:{color}; font-weight:bold;")
        name_lbl.setWordWrap(True)
        row1.addWidget(name_lbl, 1)
        row1.addWidget(game_badge(f"{rar_zh} · {cost}点", "info"))
        cl.addLayout(row1)
        # 描述
        if t.get("desc"):
            cl.addWidget(card_sub(t["desc"]))
        # 效果
        eff = _effects_summary(t.get("effects", {}), t.get("element", ""), self.gt)
        eff_lbl = QLabel(f"效果：{eff}")
        eff_lbl.setWordWrap(True)
        eff_lbl.setStyleSheet("color:#e0c48f; font-size:11px;")
        cl.addWidget(eff_lbl)
        # 选择按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        if is_sel:
            btn = QPushButton("已选（取消）")
            btn.setObjectName("dangerBtn")
            btn.clicked.connect(lambda _=False, _tid=tid: self._toggle(_tid))
        else:
            btn = QPushButton(f"选择（{cost}点）" if can_select else "点数不足")
            btn.setObjectName("primaryBtn")
            btn.setEnabled(can_select)
            if can_select:
                btn.clicked.connect(lambda _=False, _tid=tid: self._toggle(_tid))
        btn_row.addWidget(btn)
        cl.addLayout(btn_row)
        return card

    def _toggle(self, tid: str):
        if tid in self.selected:
            self.selected.pop(tid, None)
        else:
            t = next((x for x in self.pool if x.get("id") == tid), None)
            if t is None:
                return
            # 校验点数
            if self._used_points() + self._talent_cost(t) > self.max_points:
                return
            self.selected[tid] = t
        self._refresh()

    def _on_confirm(self):
        """确认：落 player.talents（锁死 + 自选）+ talent_points（剩余）。"""
        talents = []
        if self.locked_talent:
            talents.append(dict(self.locked_talent))
        talents.extend(self.selected.values())
        # 落到 world.player
        self.world.player.talents = talents
        self.world.player.talent_points = max(0, self.max_points - self._used_points())
        # [遗留修复 2026-08-29] 天赋落定后重算 mp_max：开局 mp_max 在选天赋前算好，
        # mp_bonus 天赋（雷天灵根/混沌道体）不重算就全程不生效；开局满蓝口径不变。
        from src.services import talent_engine as _te
        p = self.world.player
        from src.services import combat_engine as _ce
        p.hp_max = _ce.max_hp_for(p)
        p.hp = p.hp_max
        p.mp_max = max(0, _te.effective_stat(p, "int") * 5 + int(p.level) * 3
                       + int(_te.get_talent_bonus(p, "mp_bonus") or 0))
        p.mp = p.mp_max
        self.accept()
