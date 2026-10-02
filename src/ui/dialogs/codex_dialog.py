"""[P7k6] 图鉴对话框：探索驱动的数据收集展示（地点/NPC/怪物/物品/编年史/传闻）。

完全代码驱动（读 PlayerState codex_* + Location.explored + 已有静态数据），不用 LLM。
解锁由已有事件钩子标记（移动/遇 NPC/击败/采集/获物）。展示读世界生成时 LLM 出的静态字段。

[因果可见性 2026-09-10] 编年史 / 传闻 两个 tab 是只读查看器：世界模拟这两个子系统
此前只把产出注入叙事上下文（【世界编年史】/【坊间热议】），玩家没有任何主动翻阅入口
——引擎在跑、LLM 在写，但玩家只能靠 NPC 口述间接感知。此处补读数，只读不改引擎数据。

[UI 改造 2026-10-01 第二批] 暗金古卷设计系统重排：
- 顶部「收集进度」卡：四条进度条（地点/NPC/物品/怪物）+ 胜场/采集徽章
- 各类目改双列卡片网格（宽度利用翻倍）；NPC/物品带未解锁占位卡（收集感）
- 物品卡稀有度左边线（#gameCard[rarity]）；怪物卡状态徽章（陨落/重生/重返）
- 页签标题格式不变（test_codex_readers 锁「编年史（N）」等文案）
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget,
    QScrollArea, QFrame, QWidget, QGridLayout,
)
from PySide6.QtCore import Qt

from src.models import World
from src.ui.widgets.game_widgets import GameCard, StatBar, game_badge, card_sub

# 未解锁占位卡展示上限（防超长名单刷屏；超出折成一行「……还有 N 位」）
_LOCKED_CAP = 18


def _genre_text(world) -> "object":
    """GenreText（题材化显示名单一来源；overlay 脏数据回退默认）。"""
    try:
        from src.models.world_sim_preset import GenreText
        return GenreText(getattr(world, "config_overlay", None)
                         if isinstance(getattr(world, "config_overlay", None), dict) else {})
    except Exception:  # noqa: BLE001
        return None


class CodexDialog(QDialog):
    """图鉴：地点 / NPC / 怪物 / 物品 / 编年史 / 传闻 六类，按已解锁展示。"""

    def __init__(self, world: World, parent=None, preset=None):
        super().__init__(parent)
        self.world = world
        self.preset = preset
        self.setWindowTitle("图鉴")
        self.resize(780, 660)
        self._gt = _genre_text(world)   # [UI 禁英文] 品级/类型/属性名题材化单一来源
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)
        outer.addWidget(self._build_progress_card())

        p = world.player
        loc_total = len(world.locations)
        loc_seen = sum(1 for l in world.locations if l.explored)
        tabs = QTabWidget()
        tabs.addTab(self._build_locations_tab(), f"地点（{loc_seen}/{loc_total}）")
        tabs.addTab(self._build_npcs_tab(), f"人物（{len(p.codex_npcs)}）")
        tabs.addTab(self._build_monsters_tab(), f"怪物（{len(p.codex_defeated)}）")
        tabs.addTab(self._build_items_tab(), f"物品（{len(p.codex_items)}/{len(world.items)}）")
        # [因果可见性 2026-09-10] 编年史/传闻此前只注入叙事上下文、玩家无任何阅读入口
        # （引擎在跑、LLM 在写，但产出只能靠 NPC 口述间接感知）。此处补只读查看器，
        # 数据直接读 world.chronicle / world.shared_rumors，不动任何引擎逻辑。
        tabs.addTab(self._build_chronicle_tab(), f"编年史（{len(getattr(world, 'chronicle', None) or [])}）")
        tabs.addTab(self._build_rumors_tab(), f"传闻（{len(getattr(world, 'shared_rumors', None) or [])}）")
        outer.addWidget(tabs, 1)

        close_btn = QPushButton("合上图鉴")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        row = QVBoxLayout()
        row.addStretch()
        row.addWidget(close_btn, alignment=Qt.AlignRight)
        outer.addLayout(row)

    # ============ 顶部收集进度卡 ============
    def _build_progress_card(self) -> GameCard:
        p = self.world.player
        loc_total = len(self.world.locations)
        loc_seen = sum(1 for l in self.world.locations if l.explored)
        card = GameCard("收集进度")
        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setSpacing(6)
        grid.addWidget(StatBar("地点", loc_seen, loc_total, tone="green"), 0, 0)
        grid.addWidget(StatBar("人物", len(p.codex_npcs), len(self.world.npcs), tone="blue"), 0, 1)
        grid.addWidget(StatBar("物品", len(p.codex_items), len(self.world.items), tone="gold"), 1, 0)
        defeated = len(p.codex_defeated)
        grid.addWidget(StatBar("怪物击败", defeated, max(defeated, len(self.world.npcs)),
                               tone="purple"), 1, 1)
        card.content.addLayout(grid)
        badge_row = QWidget()
        bh = QHBoxLayout(badge_row)
        bh.setContentsMargins(0, 4, 0, 0)
        bh.setSpacing(6)
        bh.addWidget(game_badge(f"战斗胜场 {int(getattr(p, 'combat_wins', 0))}", "gold"))
        bh.addWidget(game_badge(f"采集次数 {int(getattr(p, 'gathers_done', 0))}", "info"))
        bh.addStretch()
        card.add(badge_row)
        return card

    # ============ 通用：卡片网格（双列，宽卡自动占整行）============
    def _wrap_scroll(self, content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _grid_page(self) -> tuple[QWidget, QGridLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        lay.addWidget(holder)
        lay.addStretch()
        return w, grid

    def _add_card(self, grid: QGridLayout, card: QFrame, full: bool = False) -> None:
        """双列排卡；full=True 占整行（长文卡：编年史/传闻/描述长的地点）。"""
        n = grid.count()
        if full:
            grid.addWidget(card, n, 0, 1, 2)
        else:
            grid.addWidget(card, n // 2, n % 2)

    def _locked_card(self, sub: str = "尚未发现") -> GameCard:
        c = GameCard("？？？")
        c.setProperty("locked", True)
        c.add(card_sub(sub))
        return c

    # ============ 地点 ============
    def _build_locations_tab(self) -> QWidget:
        w, grid = self._grid_page()
        seen = [l for l in self.world.locations if l.explored]
        unseen = [l for l in self.world.locations if not l.explored]
        for l in seen:
            card = GameCard(l.name)
            badge_row = QWidget()
            bh = QHBoxLayout(badge_row)
            bh.setContentsMargins(0, 0, 0, 0)
            bh.setSpacing(6)
            bh.addWidget(game_badge(l.region or "未知", "info"))
            danger = int(getattr(l, "danger", 0) or 0)
            bh.addWidget(game_badge(
                f"危险度 {danger}",
                "danger" if danger >= 4 else ("warn" if danger >= 2 else "success")))
            bh.addStretch()
            card.add(badge_row)
            desc = QLabel(l.desc or "（无描述）")
            desc.setWordWrap(True)
            desc.setStyleSheet("color:#9aa5ce; font-size:12px;")
            card.add(desc)
            self._add_card(grid, card)
        for l in unseen[:_LOCKED_CAP]:
            self._add_card(grid, self._locked_card("尚未探索的地点"))
        if len(unseen) > _LOCKED_CAP:
            tail = QLabel(f"……还有 {len(unseen) - _LOCKED_CAP} 处未知之地")
            tail.setStyleSheet("color:#565f89; font-size:12px;")
            grid.addWidget(tail, grid.count(), 0, 1, 2)
        if not seen and not unseen:
            grid.addWidget(QLabel("（这个世界还没有地点。）"), 0, 0)
        return self._wrap_scroll(w)

    # ============ NPC ============
    def _build_npcs_tab(self) -> QWidget:
        w, grid = self._grid_page()
        met = [n for n in self.world.npcs if n.id in self.world.player.codex_npcs]
        unmet = [n for n in self.world.npcs if n.id not in self.world.player.codex_npcs]
        for n in met:
            card = GameCard(n.name)
            lines = [card_sub(n.role or "身份未知", wrap=False)]
            if n.personality:
                lines.append(card_sub(f"性情：{n.personality}"))
            if n.goal:
                lines.append(card_sub(f"目标：{n.goal}"))
            for ln in lines:
                card.add(ln)
            self._add_card(grid, card)
        for _n in unmet[:_LOCKED_CAP]:
            self._add_card(grid, self._locked_card("尚未遇见"))
        if len(unmet) > _LOCKED_CAP:
            tail = QLabel(f"……还有 {len(unmet) - _LOCKED_CAP} 位尚未谋面")
            tail.setStyleSheet("color:#565f89; font-size:12px;")
            grid.addWidget(tail, grid.count(), 0, 1, 2)
        if not met and not unmet:
            grid.addWidget(QLabel("（这个世界还没有 NPC。）"), 0, 0)
        return self._wrap_scroll(w)

    # ============ 怪物 ============
    def _build_monsters_tab(self) -> QWidget:
        w, grid = self._grid_page()
        defeated = [n for n in self.world.npcs if n.id in self.world.player.codex_defeated]
        if not defeated:
            hint = QLabel("尚未击败任何敌人。战斗胜利后怪物图鉴解锁。")
            hint.setStyleSheet("color:#9aa5ce;")
            grid.addWidget(hint, 0, 0)
        for n in defeated:
            status = self._monster_status(n)
            card = GameCard(n.name)
            badge_row = QWidget()
            bh = QHBoxLayout(badge_row)
            bh.setContentsMargins(0, 0, 0, 0)
            bh.setSpacing(6)
            _gt = self._gt
            _stat = (_gt.stat if _gt is not None else (lambda k: {"str": "力", "dex": "敏",
                                                                  "int": "智", "vit": "耐",
                                                                  "luk": "运"}[k]))
            _hp = _gt.hp if _gt is not None else "生命"
            bh.addWidget(game_badge(f"等级 {n.level}", "warn"))
            bh.addWidget(game_badge(status[0], status[1]))
            bh.addStretch()
            card.add(badge_row)
            card.add(card_sub(
                f"{_hp} {n.hp_max}｜{_stat('str')}{n.stat_str} {_stat('dex')}{n.stat_dex} "
                f"{_stat('int')}{n.stat_int} {_stat('vit')}{n.stat_vit} "
                f"{_stat('luk')}{n.stat_luk}", wrap=False))
            # [修 2026-09-05 用户指示] 死亡信息入图鉴：陨落状态 + 最后一次击败的天/地点/死因
            death = (self.world.player.codex_deaths or {}).get(n.id) or {}
            if death:
                card.add(card_sub(
                    f"击败记录：第{death.get('day', '?')}天 · {death.get('loc', '未知')} · "
                    f"{death.get('cause', '被你击败')}"))
            self._add_card(grid, card)
        # [P25a] 秘境讨伐：通关记录走 dungeon.clears（临时 Boss 不入 codex_defeated，
        # 防 pseudo-id 计数漂移——见 world_sim_service finish_combat 图鉴口径注释）
        try:
            cleared = [d for d in (getattr(self.world, "dungeons", None) or [])
                       if getattr(d, "clears", 0) > 0]
            for d in cleared:
                ent = next((l for l in self.world.locations if l.id == d.location_id), None)
                card = GameCard(d.name, gold=True)
                card.add(card_sub(
                    f"通关 {d.clears} 次｜危险度 {d.danger}｜{len(d.floors)} 层｜"
                    f"位于 {ent.name if ent else '未知'}"))
                self._add_card(grid, card)
        except Exception:
            pass
        return self._wrap_scroll(w)

    def _monster_status(self, n) -> tuple[str, str]:
        """(状态文案, 徽章色)：陨落/等待重生/已重返。"""
        alive = bool(getattr(n, "alive", True))
        # [P45 v3.1 审核修复 qwen] 新死亡路径一律写 respawn>0（8-14 tick），
        # 永久死的判定须叠加 npc_permadeath 开关：开=水位悬置不消费 -> 永不重生。
        # 开关口径与 service._per_world 一致（overlay 优先 -> preset 字段）。
        _pd = (getattr(self.world, "config_overlay", None) or {}).get("npc_permadeath",
                                                                     None)
        if _pd is None:
            _pd = getattr(self.preset, "npc_permadeath", False)
        perma = (not alive) and (bool(_pd)
                                 or int(getattr(n, "respawn_at_tick", 0) or 0) <= 0)
        if perma:
            return "已陨落", "danger"
        return ("已击败 · 已重返", "success") if alive else ("已击败 · 等待重生", "warn")

    # ============ 编年史 / 传闻（时间线整行卡）============
    def _build_chronicle_tab(self) -> QWidget:
        """[因果可见性 2026-09-10] 世界编年史只读页（史官沉淀条目）。

        数据 = world.chronicle（chronicle_engine.commit_chronicle 写入，滚 8 条，
        {start_day,end_day,text}）。倒序（最新在上，与战报/动态栏同口径）。
        """
        w, grid = self._grid_page()
        entries = [c for c in (getattr(self.world, "chronicle", None) or [])
                   if isinstance(c, dict)]
        if not entries:
            hint = QLabel("史册尚是空白。世界的大事累积到一定数量后，"
                          "史官会为你记下第一条编年史。")
            hint.setStyleSheet("color:#9aa5ce;")
            grid.addWidget(hint, 0, 0)
        for c in reversed(entries):
            sd = int(c.get("start_day", 1) or 1)
            ed = int(c.get("end_day", sd) or sd)
            span = f"第{sd}天" if sd == ed else f"第{sd}-{ed}天"
            text = str(c.get("text", "") or "").strip() or "（无正文）"
            card = GameCard()
            head = QWidget()
            hh = QHBoxLayout(head)
            hh.setContentsMargins(0, 0, 0, 0)
            hh.setSpacing(8)
            hh.addWidget(game_badge(span, "gold"))
            hh.addStretch()
            card.add(head)
            body = QLabel(text)
            body.setWordWrap(True)
            body.setStyleSheet("color:#c0caf5;")
            card.add(body)
            self._add_card(grid, card, full=True)
        return self._wrap_scroll(w)

    def _build_rumors_tab(self) -> QWidget:
        """[因果可见性 2026-09-10] 市井传闻只读页（LLM 口述化后的 shared_rumors）。

        数据 = world.shared_rumors（rumor_engine.commit 写入，滚 24 条，
        {tick,title,text,...}）。tick->天 走 rumor_engine.tick_day（与注入侧同口径）。
        [!] 只读展示：传闻的「谁知道哪条」由 rumor_engine.knows 在注入侧过滤，
        本页展示的是世界里流通的全部传闻（玩家作为旁观者的全知视角）。
        """
        w, grid = self._grid_page()
        rumors = [r for r in (getattr(self.world, "shared_rumors", None) or [])
                  if isinstance(r, dict)]
        if not rumors:
            hint = QLabel("街头巷尾还没有什么风声。等世界发生几件大事，"
                          "消息自然会传开。")
            hint.setStyleSheet("color:#9aa5ce;")
            grid.addWidget(hint, 0, 0)
        try:
            from src.services import rumor_engine as rum
            tick_day = rum.tick_day
        except Exception:
            tick_day = lambda t: int(t or 0) // 12 + 1
        for r in reversed(rumors):
            day = tick_day(int(r.get("tick", 0) or 0))
            title = str(r.get("title", "") or "").strip() or "市井风声"
            text = str(r.get("text", "") or "").strip() or "（无正文）"
            card = GameCard()
            head = QWidget()
            hh = QHBoxLayout(head)
            hh.setContentsMargins(0, 0, 0, 0)
            hh.setSpacing(8)
            hh.addWidget(game_badge(f"第{day}天", "info"))
            t = QLabel(title)
            t.setStyleSheet("color:#e8e8f5; font-weight:bold;")
            hh.addWidget(t)
            hh.addStretch()
            card.add(head)
            body = QLabel(text)
            body.setWordWrap(True)
            body.setStyleSheet("color:#9aa5ce; font-size:12px;")
            card.add(body)
            self._add_card(grid, card, full=True)
        return self._wrap_scroll(w)

    # ============ 物品（稀有度左边线 + 图标头部）============
    def _build_items_tab(self) -> QWidget:
        w, grid = self._grid_page()
        owned = [i for i in self.world.items if i.id in self.world.player.codex_items]
        locked = [i for i in self.world.items if i.id not in self.world.player.codex_items]
        if not owned:
            hint = QLabel("尚未获得任何物品。采集/战斗掉落/商店购买以解锁。")
            hint.setStyleSheet("color:#9aa5ce;")
            grid.addWidget(hint, 0, 0)
        from src.ui.widgets.item_brief import ItemBriefRow
        for it in owned:
            lines = []
            if it.attack:
                lines.append(f"攻击 +{it.attack}")
            if it.defense:
                lines.append(f"防御 +{it.defense}")
            if int(getattr(it, "heal_pct", 0) or 0) > 0:
                lines.append(f"回血 +{it.heal_pct}%")
            elif it.heal_amount:
                lines.append(f"回血 +{it.heal_amount}")
            if it.desc:
                lines.append(it.desc)
            # [P12e] 图鉴卡图片化：头部为 [物品图 | 名字 + 品级·类型]
            # [UI 禁英文] 品级/类型走 GenreText 题材名（凡铁/精钢…、武器/材料…）
            _gt = self._gt
            _rn = _gt.rarity_names() if _gt is not None else {}
            _rarity_zh = _rn.get(str(it.rarity), str(it.rarity))
            _type_zh = _gt.item_type(str(it.type)) if _gt is not None else str(it.type)
            card = GameCard()
            card.setProperty("rarity", str(it.rarity or "common"))
            card.add(ItemBriefRow(
                self.world, it, sub_text=f"{_rarity_zh} · {_type_zh}"))
            for ln in lines:
                lbl = QLabel(ln)
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color:#9aa5ce; font-size:12px;")
                card.add(lbl)
            self._add_card(grid, card)
        for _i in locked[:_LOCKED_CAP]:
            self._add_card(grid, self._locked_card("尚未入手"))
        if len(locked) > _LOCKED_CAP:
            tail = QLabel(f"……还有 {len(locked) - _LOCKED_CAP} 件未见之物")
            tail.setStyleSheet("color:#565f89; font-size:12px;")
            grid.addWidget(tail, grid.count(), 0, 1, 2)
        return self._wrap_scroll(w)
