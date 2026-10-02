"""[P24a] 住宅页（HomeDialog）：购宅 / 仓库 / 陈列架 / 家具 / 休憩入口。

生活向核心：金币回收端（购宅/家具是持续 sink）+ 负重痛点（仓库不占负重）+ 休憩跳时。
引擎结算全在 home_engine（纯 Python），本对话框只做展示与调用；变更落 save_world +
changed 信号通知场景页刷新。购宅自命名（弹输入框预填题材池名，同步纯引擎，无 LLM 无 worker）。
休憩不在本对话框内结算：发 rest_requested(home_name) 信号 + accept 关窗，由场景页起
叙事回合（preset_intent=rest -> apply_intent 跳时 -> narrate -> tick_world 一次）。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QMessageBox, QComboBox, QGridLayout,
    QSizePolicy, QInputDialog,
)

from src.services import crafting_engine as cra
from src.services import farm_engine as fe
from src.services import home_engine as he
from src.services import refine_engine as rfe
from src.utils.rng import SeededRng
from src.ui.dialogs.dice_check_overlay import DiceCheckOverlay
from src.ui.widgets.item_brief import ItemBriefRow

# 家具四类的展示说明（kind -> 一行功效文案）
_FURNITURE_KIND_LABELS = {
    "storage": "储物（仓库容量 +10）",
    "bed": "床铺（休憩附少许经验）",
    "decor": "装饰（宅邸氛围素材）",
    "display": "陈列（展位 +2）",
}
# [P34c] 住宅内建筑种类标签（门控锻造/炼制/洗练高 level 物品）
_BUILDING_KIND_LABELS = {
    "forge": "锻造屋（门控武器/护甲配方）",
    "alchemy": "炼丹房（门控药剂/丹方配方）",
    "refine": "洗练室（1级即可鉴定/洗练；升级暂未增加效果）",
    "study": "书房（当前仅作居所陈设；研读功能待开放）",
    "garden": "灵田（播种灵植收获作物，1级=2 块每级+1）",
    "warehouse": "仓库（建造后才能仓库存取，等级=容量档）",
}


class HomeDialog(QDialog):
    """[P24a] 住宅管理页（双视图：概览 = 宅邸列表/当前聚落购宅；宅内 = 仓库/陈列/家具/建造/田地）。

    概览页每张宅邸卡片带入口：在宅所在聚落 -> 「进入」切宅内视图（顶部「返回」回概览）；
    不在 -> 「前往」发 travel_requested(home_id) + accept，场景页起 go_home 归宅回合
    （不受相邻限制，消耗 1 回合）。在自有宅聚落开 dialog 默认即宅内视图。
    """

    changed = Signal()            # 仓库/陈列/家具变更后发（场景页刷新 + 落盘在外层已做）
    rest_requested = Signal(str)  # 点「休憩至清晨」发（宅名），场景页据此起 rest 叙事回合
    travel_requested = Signal(str)  # 点「前往」发（宅 id），场景页据此起 go_home 归宅回合

    def __init__(self, world, world_sim_service, storage, preset, parent=None):
        super().__init__(parent)
        self.world = world
        self.svc = world_sim_service
        self.storage = storage
        self.preset = preset
        # [住宅网格 2026-08-24] 左 5x5 网格当前选中格子（-1=未选）；右栏随选中渲染
        self._sel_slot = -1
        # 双视图导航：None = 概览；宅 id = 宅内视图。在自有宅聚落开 dialog 默认即宅内
        _, _cur = self._cur_home()
        self._view_home_id = _cur.id if _cur is not None else None
        self.setWindowTitle("住宅")
        # [网格v3 2026-08-24] 大气整页左右分布：左 5x5 网格 + 右栏功能面板，无下方堆叠
        self.resize(1280, 860)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        raw_max = getattr(self.preset, "buildings_max_level", 5)
        can_build = bool(getattr(self.preset, "buildings_enabled", True)) and int(raw_max if raw_max is not None else 5) > 0
        hint = QLabel(
            "在聚落置一处宅邸：建仓库存物不占负重、休憩回复并跳到次日清晨、家具扩容、陈列战利品。"
            if can_build else
            "在聚落置一处宅邸：休憩回复并跳到次日清晨，添置家具、陈列战利品。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(hint)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
        outer.addWidget(self.status_lbl)

        # [P50 地点网格 2026-09-25 用户拍板] 村庄地图入口：把整个地点画成网格——
        # 看得到铁匠铺药铺在哪、自己的宅安置在哪、田在哪一格种的什么。
        self.map_btn = QPushButton("打开村庄地图")
        self.map_btn.setToolTip("网格化地点地图：蓝格=可前往的场所，绿格=你的宅，灰格=可安置的宅位，橙格=田块。")
        self.map_btn.clicked.connect(self._open_place_grid)
        map_row = QHBoxLayout()
        map_row.addWidget(self.map_btn)
        map_row.addStretch(1)
        outer.addLayout(map_row)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        self.body.addStretch()
        self._scroll.setWidget(inner)
        outer.addWidget(self._scroll, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.rest_btn = QPushButton("休憩至清晨")
        self.rest_btn.setObjectName("primaryBtn")
        self.rest_btn.setToolTip(f"在自宅安睡：{self._gt().hp}/{self._gt().mp} 全回复，时间跳到次日清晨（消耗一轮世界演化）")
        self.rest_btn.clicked.connect(self._on_rest)
        bottom.addWidget(self.rest_btn)
        # [P60 宴请 2026-09-26] 宅中设宴：花钱换交情/印象/声望（5 天冷却，宾客容量吃舒适度）
        self.banquet_btn = QPushButton("设宴")
        self.banquet_btn.setToolTip("宅中设宴：花一笔花销宴请同聚落的熟识（交情>=25），"
                                    "每人交情 +4、记「慷慨」印象，宅邸势力声望 +1。\n"
                                    "5 天冷却；舒适度越高可邀宾客越多（陈列+家具撑场面）")
        self.banquet_btn.clicked.connect(self._on_banquet)
        bottom.addWidget(self.banquet_btn)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        outer.addLayout(bottom)

        self._rebuild()

    # ---------- 便捷读取 ----------
    def _gt(self):
        try:
            return self.svc._genre_text(self.world)
        except Exception:
            from src.models.world_sim_preset import GenreText
            return GenreText(self.world.config_overlay if isinstance(self.world.config_overlay, dict) else {})

    def _cur_home(self):
        loc = self.svc._current_location(self.world)
        return loc, he.player_home_at(self.world, loc)

    def _item(self, item_id: str):
        return next((it for it in self.world.items if it.id == item_id), None)

    def _mat_hint(self) -> str:
        """建造/升级耗材说明：锻造类+制造类各 1，背包内对应类别皆可，附示例名。"""
        from src.models.world import effective_category
        ex = {"forge": "", "craft": ""}
        inv = set(self.world.player.inventory or [])
        pool = [self._item(i) for i in inv] + [i for i in self.world.items if i.id not in inv]
        for it in pool:
            if it is None:
                continue
            c = effective_category(it)
            if c in ex and not ex[c]:
                ex[c] = str(getattr(it, "name", "") or "")
            if ex["forge"] and ex["craft"]:
                break
        return (f"锻造材料×1（如「{ex['forge'] or '矿石/锭材'}」）+ "
                f"制造材料×1（如「{ex['craft'] or '木料/布料'}」，背包内同类别皆可）")

    # ---------- 视觉组件（图标/徽章/卡片，[住宅视觉] 批次）----------
    _KIND_BADGE_LEVEL = {
        "forge": "danger", "alchemy": "success", "refine": "warn",
        "study": "info", "garden": "success", "warehouse": "info",
        "storage": "info", "bed": "success", "decor": "warn", "display": "info",
    }
    _KIND_BADGE_ZH = {"forge": "锻造", "alchemy": "炼丹", "refine": "洗练",
                      "study": "书房", "garden": "灵田", "warehouse": "仓库",
                      "storage": "储物", "bed": "床铺", "decor": "装饰", "display": "陈列"}
    # [网格v3] 陈列柜格子边框色：按陈列架最高品级物品取色（无陈列=默认绿边）
    _RARITY_BORDER = {"common": "#565f89", "uncommon": "#9ece6a", "rare": "#7aa2f7",
                      "epic": "#bb9af7", "legendary": "#e0af68", "mythic": "#e05555"}
    _RARITY_ORDER = ("common", "uncommon", "rare", "epic", "legendary", "mythic")

    def _display_border_color(self, home) -> str:
        """[网格v3] 陈列柜格子边框色：陈列架最高品级物品取色；无陈列回退绿。"""
        top = -1
        for iid in (home.display_shelf or []):
            it = self._item(iid)
            r = getattr(it, "rarity", "common") if it is not None else "common"
            idx = self._RARITY_ORDER.index(r) if r in self._RARITY_ORDER else 0
            top = max(top, idx)
        if top < 0:
            return "#9ece6a"
        return self._RARITY_BORDER[self._RARITY_ORDER[top]]

    def _kind_icon_path(self, prefix: str, kind: str):
        """查建筑/家具预生成图标缓存（全路径）；无图 None -> 回退徽章。"""
        import os
        from src.config import paths
        try:
            fn = (self.svc.cached_building_icon(self.world, kind) if prefix == "building"
                  else self.svc.cached_furniture_icon(self.world, kind))
        except Exception:
            fn = None
        if not fn:
            return None
        p = os.path.join(paths.world_images_dir(), fn)
        return p if os.path.exists(p) else None

    def _icon_or_badge(self, prefix: str, kind: str) -> QLabel:
        """40x40 图标（有图）或品类彩色徽章（无图回退，功能不受影响）。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap
        p = self._kind_icon_path(prefix, kind)
        if p:
            pix = QPixmap(p)
            if not pix.isNull():
                av = QLabel()
                av.setPixmap(pix.scaled(40, 40, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
                return av
        b = QLabel(self._KIND_BADGE_ZH.get(kind, kind))
        b.setObjectName("atmosphereBadge")
        b.setProperty("badgeLevel", self._KIND_BADGE_LEVEL.get(kind, "info"))
        return b

    def _chip(self, text: str, level: str = "info") -> QLabel:
        c = QLabel(text)
        c.setObjectName("atmosphereBadge")
        c.setProperty("badgeLevel", level)
        return c

    def _plot_chip(self, home, r: dict) -> QFrame:
        """田地块芯片：左=作物生长图(有则，放大)，右=名+阶段状态徽章+浇水/收获按钮。"""
        chip = QFrame()
        chip.setObjectName("gameCard")
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(8)
        # [住宅网格 2026-08-27] 生长阶段图（放大；无图回退：不显示，仅徽章+文字）
        cimg = None
        try:
            cimg = self.svc.cached_crop_image(self.world, r["crop"], r["stage"])
        except Exception:
            cimg = None
        if cimg:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QPixmap
            import os
            from src.config import paths
            p = os.path.join(paths.world_images_dir(), cimg)
            pix = QPixmap(p)
            if not pix.isNull():
                av = QLabel()
                av.setFixedSize(72, 72)
                av.setPixmap(pix.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                av.setAlignment(Qt.AlignCenter)
                lay.addWidget(av, 0, Qt.AlignVCenter)
        info = QVBoxLayout()
        info.setSpacing(2)
        state_txt = r["stage_name"] + (f" {r['grown']}/{r['need']}天" if r["stage"] < 3 else "")
        nm = QLabel(f"{r['crop']}·{state_txt}")
        nm.setStyleSheet("color:#c0caf5; font-weight:bold; font-size:11px;")
        info.addWidget(nm)
        notes = []
        if r["pest"]:
            notes.append("虫害")
        if r["watered"]:
            notes.append("今日已浇")
        elif r["stage"] < 3:
            notes.append("未浇")
        level = "danger" if r["pest"] else (
            "success" if (r["watered"] or r["stage"] >= 3) else "warn")
        info.addWidget(self._chip("·".join(notes) if notes else "长势正常", level))
        if r["stage"] >= 3:
            btn = QPushButton("收 获")
            btn.setObjectName("optionBtn")
            btn.clicked.connect(lambda _=False, i=r["idx"]: self._on_harvest(home, i))
            info.addWidget(btn)
        elif not r["watered"]:
            btn = QPushButton("浇 水")
            btn.setObjectName("optionBtn")
            btn.clicked.connect(lambda _=False, i=r["idx"]: self._on_water(home, i))
            info.addWidget(btn)
        lay.addLayout(info, 1)
        return chip

    # ---------- 构建 ----------
    def _rebuild(self):
        # [!] 保持滚动位置：入仓/出仓等 _after_change 全量重建后滚动条会跳底，
        # 玩家连点几次就要反复滚回仓库区——记录旧值，布局完成后还原
        sb = self._scroll.verticalScrollBar()
        old_pos = sb.value()
        while self.body.count() > 1:
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        gt = self._gt()
        loc, home = self._cur_home()
        view_home = self._view_home()
        # [!] 宅内视图须玩家在宅所在聚落（dialog 模态期间地点不会他变，此处纯防御脏态）
        if view_home is not None and (home is None or view_home.id != home.id):
            self._view_home_id = None
            view_home = None
        if view_home is not None:
            self._insert_interior(view_home, gt)
        else:
            self._insert_overview(loc, home, gt)
        # 底部行动直接呈现可行性，避免无宅时点设宴毫无反馈。
        self.rest_btn.setEnabled(home is not None)
        if home is None:
            self.banquet_btn.setEnabled(False)
            self.banquet_btn.setText("设宴（需住宅）")
            self.banquet_btn.setToolTip("需先在当前聚落拥有住宅。")
        else:
            banquet_ok, banquet_reason = he.can_host_banquet(self.world, home)
            self.banquet_btn.setEnabled(banquet_ok)
            if banquet_ok:
                self.banquet_btn.setText("设宴")
            elif "没有愿意赴宴" in banquet_reason:
                self.banquet_btn.setText("设宴（无宾客）")
            elif "还差" in banquet_reason:
                self.banquet_btn.setText("设宴（冷却中）")
            else:
                self.banquet_btn.setText("设宴（钱不足）")
            banquet_hint = (f"需 {he.banquet_cost(home)} {gt.currency}；同聚落交情达到 25 的熟识可赴宴。"
                            "赴宴者交情 +4；若此地有势力且已建立声望，声望 +1；5 天冷却。")
            self.banquet_btn.setToolTip(
                banquet_hint if banquet_ok else f"{banquet_reason}\n{banquet_hint}")
        # 布局完成后还原滚动位置（singleShot 等 sizeHint 生效；内容变短时自动钳回合法范围）
        QTimer.singleShot(0, lambda: sb.setValue(old_pos))

    def _view_home(self):
        if not self._view_home_id:
            return None
        return next((h for h in self.world.homes if h.id == self._view_home_id), None)

    # ---- 概览视图（宅邸列表 + 当前聚落购宅/置宅提示）----
    def _insert_overview(self, loc, home, gt):
        # ---- 我的宅邸 ----
        self._insert_section(f"我的宅邸（{len(self.world.homes)}）")
        if not self.world.homes:
            self._insert_empty("尚无宅邸。在聚落可购置。")
        else:
            loc_by_id = {l.id: l for l in self.world.locations}
            for h in self.world.homes:
                l = loc_by_id.get(h.location_id)
                here = "（你在此）" if (home is not None and h.id == home.id) else ""
                self._insert_widget(self._home_row(h, l, here))
        # ---- 当前地点上下文 ----
        if loc is None:
            self._insert_empty("世界暂无地点。")
        elif getattr(loc, "kind", "") != "settlement":
            self._insert_section("置宅")
            self._insert_empty(f"「{loc.name}」不是聚落，无法置宅（聚落 = 城/镇/村/坊市等安全区）。")
        elif home is not None:
            self._insert_section("管理住宅")
            self._insert_empty(
                f"你正在自宅「{home.name}」所在聚落，点上方宅邸卡的「进入」可入内管理仓库/陈列/家具/田地。")
        else:
            self._insert_buy_section(loc, gt)

    # ---- 宅内视图（顶部返回导航 + 管理区）----
    def _insert_interior(self, home, gt):
        nav = QHBoxLayout()
        nav.setSpacing(8)
        back_btn = QPushButton("← 返回住宅概览")
        back_btn.setToolTip("回到概览页（宅邸列表 / 当前聚落购宅）")
        back_btn.clicked.connect(lambda _=False: self._back_to_overview())
        nav.addWidget(back_btn)
        loc = next((l for l in self.world.locations if l.id == home.location_id), None)
        title = QLabel(f"「{home.name}」· {he.tier_name(self.world, home.tier)} · {loc.name if loc else '未知聚落'}")
        title.setStyleSheet("color:#c0caf5; font-weight:bold;")
        nav.addWidget(title, 1)
        nav_w = QWidget()
        nav_w.setLayout(nav)
        self._insert_widget(nav_w)
        self._insert_manage_sections(home, gt)

    def _insert_section(self, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#7aa2f7; font-weight:bold; margin-top:4px;")
        self.body.insertWidget(self.body.count() - 1, lbl)

    def _insert_empty(self, text: str):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#565f89; font-size:12px;")
        self.body.insertWidget(self.body.count() - 1, lbl)

    def _insert_widget(self, w):
        self.body.insertWidget(self.body.count() - 1, w)

    def _home_row(self, home, loc, here: str) -> QFrame:
        row = QFrame()
        row.setObjectName("gameCard")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)
        info = QVBoxLayout()
        info.setSpacing(2)
        nm = QLabel(f"「{home.name}」{here}")
        nm.setStyleSheet("color:#c0caf5; font-weight:bold;")
        info.addWidget(nm)
        sub = QLabel(
            f"{he.tier_name(self.world, home.tier)} · {loc.name if loc else '未知聚落'} · "
            f"第 {home.purchased_day} 天购入"
        )
        sub.setStyleSheet("color:#9aa5ce; font-size:11px;")
        info.addWidget(sub)
        # 状态芯片（仓库/陈列/家具计数，徽章样式）
        chips = QHBoxLayout()
        chips.setSpacing(4)
        chips.addWidget(self._chip(f"仓库 {len(home.stash)}/{he.stash_capacity(home)}"))
        chips.addWidget(self._chip(f"陈列 {len(home.display_shelf)}/{he.display_capacity(home)}"))
        chips.addWidget(self._chip(f"家具 {len(home.furniture)}"))
        chips.addStretch()
        info.addLayout(chips)
        lay.addLayout(info, 1)
        if here:
            btn = QPushButton("进 入")
            btn.setObjectName("primaryBtn")
            btn.setToolTip("进入宅内：仓库/陈列/家具/建造/田地")
            btn.clicked.connect(lambda _=False, hid=home.id: self._enter_home(hid))
        else:
            btn = QPushButton("前 往")
            btn.setToolTip("动身返回此宅所在聚落（消耗 1 回合世界演化）")
            btn.clicked.connect(lambda _=False, hid=home.id: self._on_travel(hid))
        lay.addWidget(btn)
        return row

    # ---- 购宅区 ----
    def _insert_buy_section(self, loc, gt):
        self._insert_section(f"在「{loc.name}」购宅（每处聚落限一宅）")
        desc_hint = {
            # [修 2026-09-10 用户指示] 文案口径修正：仓库是「建造后」的建筑（未建=0 容量，
            # 建好 30 格起），不是住宅自带——原「仓库 30 格」会被读成买宅即送 30 格。
            1: "简朴安身处：可建仓库（建成后 30 格）+ 陈列 4 位，足以安放行囊。",
            2: "殷实人家：门庭像样，作生活基地正合适（仓库/陈列容量同小宅，气派与身份象征）。",
            3: "气派宅邸：当地显赫之宅，长线攒钱的大目标（仓库/陈列容量同小宅，顶格身份象征）。",
        }
        raw_max = getattr(self.preset, "buildings_max_level", 5)
        if not getattr(self.preset, "buildings_enabled", True) or int(raw_max if raw_max is not None else 5) <= 0:
            desc_hint = {
                1: "简朴安身处：可以休憩和添置家具，适合安放战利品。",
                2: "殷实人家：门庭像样，作生活基地正合适。",
                3: "气派宅邸：当地显赫之宅，长线攒钱的大目标。",
            }
        for t in (1, 2, 3):
            price = he.home_price(self.world, loc, t)
            row = QFrame()
            row.setObjectName("gameCard")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(10, 8, 10, 8)
            lay.setSpacing(10)
            info = QVBoxLayout()
            info.setSpacing(2)
            nm = QLabel(f"{he.tier_name(self.world, t)}")
            nm.setStyleSheet("color:#c0caf5; font-weight:bold;")
            info.addWidget(nm)
            sub = QLabel(f"{desc_hint.get(t, '')} 售价 {price} {gt.currency}（持有 {self.world.player.gold} {gt.currency}）")
            sub.setStyleSheet("color:#9aa5ce; font-size:11px;")
            sub.setWordWrap(True)
            info.addWidget(sub)
            lay.addLayout(info, 1)
            buy_btn = QPushButton("购 买")
            buy_btn.setObjectName("primaryBtn")
            # [P34e] 村庄限 tier1：超过 max_home_tier 的档位禁用 + tooltip 提示
            mt = he.max_home_tier(loc)
            if t > mt:
                buy_btn.setEnabled(False)
                buy_btn.setToolTip("村庄仅限小宅（一档），城市/城镇可选全档。")
            else:
                buy_btn.setEnabled(self.world.player.gold >= price)
            buy_btn.clicked.connect(lambda _=False, t=t: self._on_buy(loc, t))
            lay.addWidget(buy_btn)
            self._insert_widget(row)

    # ---- 管理区（在自有宅）----
    def _insert_manage_sections(self, home, gt):
        # [住宅网格v3 2026-08-24] 整页左右分布：左 5x5 网格（建筑+家具）+ 右栏功能面板，
        # 下方不再堆叠任何区（仓库/陈列/建造/购置/田地全迁入网格+右栏）。ensure_home_grid
        # 迁移老档（补 slot + 自动补仓库）。建筑关闭时仍须保留家具与宅内管理入口。
        he.ensure_home_grid(home)
        # [网格v4] stretch=1 让左右分栏垂直铺满（吃掉 body 末尾 addStretch 的空隙）
        self.body.insertWidget(self.body.count() - 1, self._grid_and_panel(home, gt), 1)

    # ---------- [住宅网格v3 2026-08-24] 左 5x5 网格（建筑+家具）+ 右栏功能面板 ----------
    def _spec_hint(self, kind: str, level: int) -> str:
        """升级材料需求人读文案（spec 具体名/品级数/旧口径回退）。"""
        spec = he._spec_for(self.world, kind, level)
        if not spec:
            return self._mat_hint()
        parts = []
        for e in spec:
            if e.get("item_id"):
                parts.append(f"「{e.get('name', '?')}」")
            else:
                parts.append(f"{e.get('count', 1)}件{self._gt().rarity(e.get('rarity', 'common'))}")
        return "、".join(parts)

    def _level_benefit(self, kind: str, level: int) -> str:
        """只展示真实已接线的收益，不把未实现的建筑等级写成可用能力。"""
        if kind == "warehouse":
            return "开通 30 格住宅仓库" if level == 1 else "仓库容量增加 10 格"
        if kind == "garden":
            return f"灵田可同时种植 {level + 1} 块地"
        if kind in ("forge", "alchemy"):
            item_by_id = {it.id: it for it in (self.world.items or [])}
            names = [str(getattr(item_by_id.get(r.output_item_id), "name", r.name) or r.name)
                     for r in (self.world.recipes or [])
                     if getattr(r, "required_building", "") == kind
                     and max(1, int(getattr(r, "min_building_level", 1) or 1)) == level]
            return ("新解锁配方：" + "、".join(names[:3]) + ("等" if len(names) > 3 else "")) \
                if names else "当前世界此级暂无新增配方"
        if kind == "refine":
            return "1级即可使用鉴定和洗练；升级暂未增加效果"
        if kind == "study":
            return "书房研读功能尚未开放，目前仅作陈设"
        return ""

    # [网格v4] 格子固定正方形边长（图片为正方形，格子同正方形防样式高度抖动）
    _CELL = 118

    def _grid_and_panel(self, home, gt) -> QWidget:
        """左正方形格子网格 + 右栏上下文面板；整块左右铺满、垂直撑满。

        格子固定 _CELL x _CELL 正方形（样式稳定）；网格整体在左框内居中，左/右两栏
        随对话框高度铺满（sizePolicy Expanding + 外层 insertWidget stretch=1）。"""
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        hbox = QHBoxLayout(w)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(10)
        # 左：正方形格子网格（居中铺满）
        left = QFrame()
        left.setStyleSheet("QFrame{background:#161a2b; border-radius:6px;}")
        left.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer = QVBoxLayout(left)
        outer.setContentsMargins(10, 10, 10, 10)
        grid_w = QWidget()
        gl = QGridLayout(grid_w)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(8)
        for slot in range(he.GRID_SLOTS):
            gl.addWidget(self._grid_cell(home, slot, gt),
                         slot // he.GRID_SIZE, slot % he.GRID_SIZE)
        outer.addWidget(grid_w, 0, Qt.AlignCenter)
        hbox.addWidget(left, 3)
        # 右：上下文面板（可滚，铺满高度）
        rs = QScrollArea()
        rs.setWidgetResizable(True)
        rs.setFrameShape(QFrame.NoFrame)
        rs.setMinimumWidth(420)
        rs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        rs.setWidget(self._right_content(home, gt))
        hbox.addWidget(rs, 2)
        return w

    def _grid_cell(self, home, slot: int, gt) -> QPushButton:
        typ, ent = he.entity_at_slot(home, slot)
        btn = QPushButton()
        btn.setFixedSize(self._CELL, self._CELL)  # 固定正方形，样式稳定
        if typ == "building":
            kind = ent.get("kind", "")
            p = self._kind_icon_path("building", kind)
            if p:
                from PySide6.QtGui import QIcon
                from PySide6.QtCore import QSize
                btn.setIcon(QIcon(p))
                btn.setIconSize(QSize(self._CELL - 8, self._CELL - 8))
            else:
                btn.setText(f"{ent.get('name', kind)}\n{ent.get('level', 1)}级")
            btn.setToolTip(f"{ent.get('name', kind)}（{ent.get('level', 1)}级，点击查看/使用）")
            btn.setStyleSheet("QPushButton{background:#1f2a4a; border:1px solid #7aa2f7; border-radius:6px; color:#c0caf5;}"
                              "QPushButton:hover{background:#24305a;}")
        elif typ == "furniture":
            kind = ent.get("kind", "")
            p = self._kind_icon_path("furniture", kind)
            if p:
                from PySide6.QtGui import QIcon
                from PySide6.QtCore import QSize
                btn.setIcon(QIcon(p))
                btn.setIconSize(QSize(self._CELL - 8, self._CELL - 8))
            else:
                btn.setText(ent.get("name", kind))
            # [网格v3] 陈列柜边框按陈列架最高品级取色加粗；其余家具默认绿边
            if kind == "display":
                bc = self._display_border_color(home)
                btn.setToolTip(f"陈列柜（{len(home.display_shelf)}/{he.display_capacity(home)}）：边框=陈列最高品级")
                btn.setStyleSheet(f"QPushButton{{background:#1a2b21; border:2px solid {bc}; border-radius:6px; color:#c0caf5;}}"
                                  "QPushButton:hover{background:#20362a;}")
            else:
                btn.setToolTip(f"{_FURNITURE_KIND_LABELS.get(kind, kind)}（点击查看/拆除）")
                btn.setStyleSheet("QPushButton{background:#1a2b21; border:1px solid #9ece6a; border-radius:6px; color:#c0caf5;}"
                                  "QPushButton:hover{background:#20362a;}")
        else:
            btn.setText("空地")
            btn.setToolTip("空地块：点击在右栏选择建造/购置")
            btn.setStyleSheet("QPushButton{background:#121219; border:1px dashed #2a2e44; border-radius:6px; color:#565f89;}"
                              "QPushButton:hover{background:#161a2b;}")
        if slot == self._sel_slot:
            btn.setStyleSheet(btn.styleSheet().replace("#7aa2f7", "#e0af68").replace("#2a2e44", "#e0af68").replace("#9ece6a", "#e0af68"))
        btn.clicked.connect(lambda _=False, s=slot: self._on_select_slot(s))
        return btn

    def _on_select_slot(self, slot: int):
        self._sel_slot = slot if slot != self._sel_slot else -1
        self._rebuild()

    def _right_content(self, home, gt) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)
        if self._sel_slot < 0:
            built = len([b for b in (home.buildings or []) if isinstance(b, dict)])
            free = sum(1 for s in range(he.GRID_SLOTS) if he.slot_free(home, s))
            t = QLabel(f"已建建筑 {built} 座｜空地 {free} 格\n"
                       "点击左侧格子：\n· 空地 → 建造建筑 / 购置家具\n"
                       "· 建筑 → 查看当前作用与下级收益\n· 家具 → 查看 / 拆除")
            t.setWordWrap(True)
            t.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(t)
            lay.addStretch()
            return w
        typ, ent = he.entity_at_slot(home, self._sel_slot)
        if typ == "building":
            self._building_panel(home, ent, gt, lay)
        elif typ == "furniture":
            self._furniture_panel(home, ent, gt, lay)
        else:
            self._build_chooser(home, gt, lay)
        lay.addStretch()
        return w

    def _build_chooser(self, home, gt, lay):
        t = QLabel(f"在格子 {self._sel_slot} 放置（两类：建筑 / 家具）")
        t.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        lay.addWidget(t)
        # ---- 建筑（每类限一座）----
        bt = QLabel("建筑")
        bt.setStyleSheet("color:#7aa2f7; font-size:11px;")
        lay.addWidget(bt)
        raw_max = getattr(self.preset, "buildings_max_level", 5)
        max_lvl = max(0, int(raw_max if raw_max is not None else 5))
        building_enabled = bool(getattr(self.preset, "buildings_enabled", True)) and max_lvl > 0
        if not building_enabled:
            lay.addWidget(self._mk_lbl("当前世界关闭了住宅建筑建造。", "#e0af68"))
        for entry in (he.building_catalog(self.world) if building_enabled else []):
            kind = entry.get("kind", "")
            if he.building_of(home, kind) is not None:
                continue  # 每类限一，已建不重复
            price = he.building_price(self.world, entry, 0)
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QVBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(2)
            hl = QHBoxLayout()
            hl.addWidget(self._icon_or_badge("building", kind), 0)
            nm = QLabel(entry.get("name", ""))
            nm.setStyleSheet("color:#c0caf5; font-weight:bold;")
            hl.addWidget(nm, 1)
            rl.addLayout(hl)
            d = QLabel(_BUILDING_KIND_LABELS.get(kind, ""))
            d.setWordWrap(True)
            d.setStyleSheet("color:#9aa5ce; font-size:10px;")
            rl.addWidget(d)
            benefit = QLabel(self._level_benefit(kind, 1))
            benefit.setWordWrap(True)
            benefit.setStyleSheet("color:#9ece6a; font-size:10px;")
            rl.addWidget(benefit)
            btn = QPushButton(f"建 造（{price} {gt.currency}+材料）")
            has_mats, mat_reason = he.building_material_status(self.world, kind, 1)
            money_ok = self.world.player.gold >= price
            btn.setEnabled(money_ok and has_mats)
            btn.setToolTip(f"建造需 {price} {gt.currency} + {self._spec_hint(kind, 1)}")
            btn.clicked.connect(lambda _=False, e=dict(entry): self._on_build_building_at(e))
            rl.addWidget(btn)
            status = ("资源已备齐" if money_ok and has_mats else
                      (f"还差 {price - self.world.player.gold} {gt.currency}；" if not money_ok else "")
                      + (mat_reason if not has_mats else ""))
            rl.addWidget(self._mk_lbl(status, "#9ece6a" if btn.isEnabled() else "#e0af68"))
            lay.addWidget(row)
        # ---- 家具（可重复摆，每件占一格）----
        ft = QLabel("家具")
        ft.setStyleSheet("color:#9ece6a; font-size:11px;")
        lay.addWidget(ft)
        for entry in he.furniture_catalog(self.world):
            kind = entry.get("kind", "")
            if kind == "storage" and not building_enabled and he.building_of(home, "warehouse") is None:
                continue  # 当前世界无法建仓库，避免购入永远不能生效的储物家具
            price = he.furniture_price(self.world, entry)
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QVBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(2)
            hl = QHBoxLayout()
            hl.addWidget(self._icon_or_badge("furniture", kind), 0)
            nm = QLabel(f"{entry.get('name', '')}（{_FURNITURE_KIND_LABELS.get(kind, '')}）")
            nm.setStyleSheet("color:#c0caf5; font-weight:bold;")
            hl.addWidget(nm, 1)
            rl.addLayout(hl)
            btn = QPushButton(f"购 置（{price} {gt.currency}）")
            btn.setEnabled(self.world.player.gold >= price)
            btn.clicked.connect(lambda _=False, e=dict(entry): self._on_buy_furniture_at(e))
            rl.addWidget(btn)
            lay.addWidget(row)

    def _on_buy_furniture_at(self, entry: dict):
        _, home = self._cur_home()
        if home is None:
            return
        ok, err = he.buy_furniture(self.world, home, entry, slot=self._sel_slot)
        if not ok:
            QMessageBox.warning(self, "购置失败", err or f"{self._gt().currency}不足。")
            return
        self._after_change(f"已购置「{entry.get('name', '家具')}」于格子 {self._sel_slot}。")

    def _on_build_building_at(self, entry: dict):
        _, home = self._cur_home()
        if home is None:
            return
        ok, err = he.build_building(self.world, home, entry, self.preset, slot=self._sel_slot)
        if not ok:
            QMessageBox.warning(self, "建造失败", err or "资源不足。")
            return
        QMessageBox.information(self, "建造成功",
                                f"「{entry.get('name', '建筑')}」已在格子 {self._sel_slot} 落成（1级）。")
        self._after_change("")

    def _building_panel(self, home, b: dict, gt, lay):
        kind = b.get("kind", "")
        cur_lvl = int(b.get("level", 1) or 1)
        raw_max = getattr(self.preset, "buildings_max_level", 5)
        max_lvl = max(0, int(raw_max if raw_max is not None else 5))
        building_enabled = bool(getattr(self.preset, "buildings_enabled", True)) and max_lvl > 0
        # 头部：图标 + 名 + Lv + 升级
        hl = QHBoxLayout()
        hl.addWidget(self._icon_or_badge("building", kind), 0)
        nm = QLabel(f"{b.get('name', kind)}  {cur_lvl}级")
        nm.setStyleSheet("color:#c0caf5; font-weight:bold; font-size:13px;")
        hl.addWidget(nm, 1)
        lay.addLayout(hl)
        d = QLabel(_BUILDING_KIND_LABELS.get(kind, ""))
        d.setWordWrap(True)
        d.setStyleSheet("color:#9aa5ce; font-size:10px;")
        lay.addWidget(d)
        if kind in ("study", "refine"):
            lay.addWidget(self._mk_lbl(self._level_benefit(kind, cur_lvl), "#e0af68"))
        elif building_enabled and cur_lvl < max_lvl:
            price = he.building_price(self.world, {"base_price": he._catalog_base_price(self.world, kind)}, cur_lvl)
            ub = QPushButton(f"升 级（{price} {gt.currency}+材料）")
            has_mats, mat_reason = he.building_material_status(self.world, kind, cur_lvl + 1)
            money_ok = self.world.player.gold >= price
            ub.setEnabled(money_ok and has_mats)
            ub.setToolTip(f"升级至 {cur_lvl + 1} 级 需 {price} {gt.currency} + {self._spec_hint(kind, cur_lvl + 1)}")
            ub.clicked.connect(lambda _=False, k=kind: self._on_upgrade_building(home, k))
            lay.addWidget(ub)
            lay.addWidget(self._mk_lbl(self._level_benefit(kind, cur_lvl + 1), "#9ece6a"))
            status = ("资源已备齐" if money_ok and has_mats else
                      (f"还差 {price - self.world.player.gold} {gt.currency}；" if not money_ok else "")
                      + (mat_reason if not has_mats else ""))
            lay.addWidget(self._mk_lbl(status, "#9ece6a" if ub.isEnabled() else "#e0af68"))
        else:
            lay.addWidget(self._chip("已满级" if building_enabled and cur_lvl >= max_lvl else "此世界不可升级", "success"))
        lay.addWidget(QLabel(""))  # 间隔
        # 功能面板
        if kind in ("forge", "alchemy"):
            self._panel_craft(home, kind, lay)
        elif kind == "refine":
            self._panel_refine(home, lay)
        elif kind == "garden":
            self._panel_garden(home, gt, lay)
        elif kind == "warehouse":
            self._panel_stash(home, gt, lay)
        else:
            t = QLabel("书房功能预留（技能书研读）。")
            t.setWordWrap(True)
            t.setStyleSheet("color:#565f89; font-size:11px;")
            lay.addWidget(t)

    def _panel_stash(self, home, gt, lay):
        """仓库建筑右栏面板：背包<->仓库存取（出仓走负重校验）。"""
        cap = he.stash_capacity(home)
        t = QLabel(f"仓库（{len(home.stash)}/{cap}，不占负重；储物家具扩容）")
        t.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        lay.addWidget(t)
        from src.services import domain_engine as de
        dm = de.domain_at(self.world, home.location_id)
        if dm is not None:
            delivering = sum(1 for o in (dm.orders or [])
                             if o.get("ship") == "home" and o.get("state") in ("open", "stalled"))
            lay.addWidget(self._mk_lbl(
                f"同地据点「{dm.name}」可把产线成品直送这里"
                + (f"；当前有 {delivering} 张待交付订单" if delivering else "。"),
                "#9aa5ce"))
        inv = list(self.world.player.inventory or [])
        if not inv and not home.stash:
            lay.addWidget(self._mk_lbl("背包与仓库都是空的。", "#565f89"))
        for iid in home.stash:
            it = self._item(iid)
            if it is None:
                continue
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(6)
            rl.addWidget(ItemBriefRow(self.world, it, "仓中", icon_size=32), 1)
            btn = QPushButton("← 出仓")
            btn.clicked.connect(lambda _=False, i=iid: self._on_withdraw(home, i))
            rl.addWidget(btn)
            lay.addWidget(row)
        for iid in inv:
            it = self._item(iid)
            if it is None:
                continue
            row = QFrame()
            row.setStyleSheet("QFrame{background:#161a2b; border-radius:6px;}")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(6)
            rl.addWidget(ItemBriefRow(self.world, it, "背包", icon_size=32), 1)
            btn = QPushButton("入仓 →")
            btn.clicked.connect(lambda _=False, i=iid: self._on_deposit(home, i))
            rl.addWidget(btn)
            lay.addWidget(row)

    def _furniture_panel(self, home, f: dict, gt, lay):
        """家具右栏面板：功效说明 +（陈列柜=陈列架存取）+ 拆除。"""
        kind = f.get("kind", "")
        hl = QHBoxLayout()
        hl.addWidget(self._icon_or_badge("furniture", kind), 0)
        nm = QLabel(f.get("name", kind))
        nm.setStyleSheet("color:#c0caf5; font-weight:bold; font-size:13px;")
        hl.addWidget(nm, 1)
        lay.addLayout(hl)
        d = QLabel(_FURNITURE_KIND_LABELS.get(kind, ""))
        d.setWordWrap(True)
        d.setStyleSheet("color:#9aa5ce; font-size:11px;")
        lay.addWidget(d)
        if kind == "display":
            self._panel_display(home, gt, lay)
        rb = QPushButton("拆 除（不退款，容量随之缩）")
        rb.clicked.connect(lambda _=False, s=self._sel_slot: self._on_remove_furniture(s))
        lay.addWidget(rb)

    def _panel_display(self, home, gt, lay):
        """[网格v3] 陈列柜右栏面板：背包<->陈列架存取（必须建陈列柜才有格子）。"""
        cap = he.display_capacity(home)
        t = QLabel(f"陈列架（{len(home.display_shelf)}/{cap}；战利品展示，旁白可见）")
        t.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        lay.addWidget(t)
        if cap <= 0:
            lay.addWidget(self._mk_lbl("陈列柜未生效（容量 0）。", "#565f89"))
        inv = list(self.world.player.inventory or [])
        if not inv and not home.display_shelf:
            lay.addWidget(self._mk_lbl("背包与陈列架都是空的。", "#565f89"))
        for iid in home.display_shelf:
            it = self._item(iid)
            if it is None:
                continue
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(6)
            rl.addWidget(ItemBriefRow(self.world, it, "陈列中", icon_size=32), 1)
            btn = QPushButton("← 取回")
            btn.clicked.connect(lambda _=False, i=iid: self._on_shelf_take(home, i))
            rl.addWidget(btn)
            lay.addWidget(row)
        for iid in inv:
            it = self._item(iid)
            if it is None:
                continue
            row = QFrame()
            row.setStyleSheet("QFrame{background:#161a2b; border-radius:6px;}")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(6)
            rl.addWidget(ItemBriefRow(self.world, it, "背包", icon_size=32), 1)
            btn = QPushButton("上架 →")
            btn.setEnabled(len(home.display_shelf) < cap)
            btn.clicked.connect(lambda _=False, i=iid: self._on_shelf_put(home, i))
            rl.addWidget(btn)
            lay.addWidget(row)

    def _on_remove_furniture(self, slot: int):
        _, home = self._cur_home()
        if home is None:
            return
        ok, err = he.remove_furniture(home, slot)
        if not ok:
            QMessageBox.warning(self, "拆除失败", err or "该格子没有家具。")
            return
        self._after_change("已拆除家具。")

    def _open_place_grid(self):
        """[P50] 打开地点网格地图（可视化：别人的店/自己的宅/田块）。"""
        from src.ui.dialogs.place_grid_dialog import PlaceGridDialog
        _, home = self._cur_home()
        loc = None
        if home is not None:
            loc = next((l for l in self.world.locations
                        if l.id == home.location_id), None)
        if loc is None:
            loc = next((l for l in self.world.locations
                        if l.id == getattr(self.world.player, "location_id", "")), None)
        if loc is None:
            QMessageBox.information(self, "村庄地图", "当前没有可显示的地点。")
            return
        PlaceGridDialog(self.world, loc, home=home, parent=self).exec()

    def _panel_craft(self, home, kind, lay):
        t = QLabel("可合成配方（专属配方按建筑等级解锁；通用配方家里也能做）")
        t.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        lay.addWidget(t)
        # [P60 锻造修行熟练线 UI 2026-09-26] P53 引擎既有进度首次露出（合成成功/失败积经验）
        _lvl, _exp, _need = cra.craft_prof_progress(self.world.player)
        _title = cra.craft_prof_title(self.world.player, self.world)
        _bonus = int(cra.craft_prof_bonus(self.world.player) * 100)
        _prog = ("已臻化境" if _need == 0 else f"下一阶还差 {_need} 点经验")
        t2 = QLabel(f"锻造修行：{_title}（熟练 {_exp}｜成功率 +{_bonus}%｜{_prog}）")
        t2.setStyleSheet("color:#e0af68; font-size:11px;")
        t2.setToolTip("合成成功/大成功/失败都会积累熟练经验；每升一阶成功率 +2%（四阶封顶）。")
        lay.addWidget(t2)
        # [P60 残料回炉] 大失败残料的再利用出口（3 件残料 -> 1 件 common 材料）
        _scrap_n = cra.scrap_count(self.world.player)
        rc = QPushButton(f"回炉残料（{_scrap_n}/3 -> 1 件基础材料）")
        rc.setToolTip("把合成大失败留下的残料重铸回基础材料（3 件合一）")
        rc.setEnabled(_scrap_n >= cra.SCRAP_RECYCLE_COUNT)
        rc.clicked.connect(self._on_recycle_scrap)
        lay.addWidget(rc)
        # [P49 合成可达性修复 2026-09-25] 原筛选只列「本建筑专属」配方——地图拓展与
        # 通用（required_building 为空）配方玩家完全看不到、造不了（Qwen 方案实锤缺口①）。
        recipes = [r for r in self.world.recipes
                   if getattr(r, "required_building", "") == kind]
        common = [r for r in self.world.recipes
                  if not getattr(r, "required_building", "")]
        if not recipes and not common:
            lay.addWidget(self._mk_lbl("暂无该建筑配方。", "#565f89"))
            return
        for r in recipes:
            out = self._item(getattr(r, "output_item_id", ""))
            if out is None:
                continue
            ok, reason = cra.can_craft(self.world.player, r, self.world, self.preset)
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QVBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(2)
            rl.addWidget(ItemBriefRow(self.world, out, "", icon_size=32), 0)
            mats = "、".join((self._item(i).name if self._item(i) else i) for i in (r.inputs or []))
            ml = QLabel(f"材料：{mats or '无'}")
            ml.setWordWrap(True)
            ml.setStyleSheet("color:#9aa5ce; font-size:10px;")
            rl.addWidget(ml)
            ready, _, picks = cra.home_material_plan(self.world.player, r, home, self.world)
            if ready:
                take_btn = QPushButton(f"从住宅仓库取齐材料（{len(picks)} 件）")
                take_btn.clicked.connect(lambda _=False, rc=r, h=home: self._on_prepare_craft(rc, h))
                rl.addWidget(take_btn)
            ch = cra.craft_chance(self.world.player, r, self.world, self.preset)
            btn = QPushButton(f"合 成（{int(ch * 100)}%）")
            btn.setEnabled(bool(ok))
            if not ok:
                btn.setToolTip(reason or "条件不足")
                rl.addWidget(self._mk_lbl(reason or "条件不足", "#e0af68"))
            btn.clicked.connect(lambda _=False, rc=r: self._on_craft_recipe(rc))
            rl.addWidget(btn)
            lay.addWidget(row)
        # [P49] 通用配方组：无建筑门控的配方在任何建筑面板都能做（原版无处可合成）
        if common:
            lay.addWidget(self._mk_lbl("通用配方（任何工坊均可制作）", "#7dcfff"))
            for r in common:
                out = self._item(getattr(r, "output_item_id", ""))
                if out is None:
                    continue
                ok, reason = cra.can_craft(self.world.player, r, self.world, self.preset)
                row = QFrame()
                row.setObjectName("gameCard")
                rl = QVBoxLayout(row)
                rl.setContentsMargins(8, 6, 8, 6)
                rl.setSpacing(2)
                rl.addWidget(ItemBriefRow(self.world, out, "", icon_size=32), 0)
                # [P49] 缺料全列（原 can_craft 只报第一个缺的——玩家要来回试）
                lacks = cra.missing_inputs(self.world.player, r, self.world)
                mats = "、".join((self._item(i).name if self._item(i) else i) for i in (r.inputs or []))
                ml = QLabel(f"材料：{mats or '无'}"
                            + (f"　｜　缺：{'、'.join(f'{nm} x{n}' for nm, n in lacks)}" if lacks else ""))
                ml.setWordWrap(True)
                ml.setStyleSheet("color:#9aa5ce; font-size:10px;")
                rl.addWidget(ml)
                ready, _, picks = cra.home_material_plan(self.world.player, r, home, self.world)
                if ready:
                    take_btn = QPushButton(f"从住宅仓库取齐材料（{len(picks)} 件）")
                    take_btn.clicked.connect(lambda _=False, rc=r, h=home: self._on_prepare_craft(rc, h))
                    rl.addWidget(take_btn)
                ch = cra.craft_chance(self.world.player, r, self.world, self.preset)
                btn = QPushButton(f"合 成（{int(ch * 100)}%）")
                btn.setEnabled(bool(ok))
                if not ok:
                    btn.setToolTip(reason or "条件不足")
                    rl.addWidget(self._mk_lbl(reason or "条件不足", "#e0af68"))
                btn.clicked.connect(lambda _=False, rc=r: self._on_craft_recipe(rc))
                rl.addWidget(btn)
                lay.addWidget(row)

    def _on_prepare_craft(self, recipe, home):
        ok, msg, _ = cra.prepare_home_materials(self.world.player, recipe, home, self.world)
        if ok:
            self._after_change(msg)
        else:
            QMessageBox.information(self, "取料未成", msg)
            self._rebuild()

    def _on_recycle_scrap(self):
        """[P60 残料回炉] 3 件残料 -> 1 件 common 材料（成功后重建面板刷新熟练行/按钮态）。"""
        ok, msg = cra.recycle_scrap(self.world, self.world.player)
        if ok:
            self.storage.save_world(self.world)
            self._after_change(msg)
        else:
            QMessageBox.information(self, "回炉", msg)
            self._rebuild()

    def _on_craft_recipe(self, recipe):
        # 先预检（避免弹骰后才发现材料不足）
        ok, reason = cra.can_craft(self.world.player, recipe, self.world, self.preset)
        if not ok:
            QMessageBox.warning(self, "合成失败", reason or "材料不足/建筑等级不足。")
            self._rebuild()
            return
        chance = cra.craft_chance(self.world.player, recipe, self.world, self.preset)
        overlay = DiceCheckOverlay(chance, parent=self)
        overlay.exec()
        dice = overlay.result
        if dice is None:
            return
        res = cra.craft(self.world.player, recipe, self.world, rng=None, preset=self.preset, dice=dice)
        if not res.get("success"):
            QMessageBox.warning(self, "合成失败", res.get("reason") or "材料不足/建筑等级不足。")
            self._rebuild()
            return
        self._after_change(f"合成成功：{res.get('output_name', '')}")

    def _panel_refine(self, home, lay):
        t = QLabel("器物台（鉴定·洗练）")
        t.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        lay.addWidget(t)
        inv = list(self.world.player.inventory or [])
        from src.models.world import effective_category
        equips = [self._item(i) for i in inv
                  if self._item(i) is not None and self._item(i).type in ("weapon", "armor", "accessory")]
        scrolls = [self._item(i) for i in inv if self._item(i) is not None
                   and effective_category(self._item(i)) == "cultivate"]
        id_scrolls = [it for it in scrolls if getattr(it, "reagent_kind", "") == "identify"]
        rf_stones = [it for it in scrolls if getattr(it, "reagent_kind", "") == "refine"]
        # 目标
        lay.addWidget(self._mk_lbl("目标装备：", "#9aa5ce"))
        self._rf_target = QComboBox()
        for it in equips:
            self._rf_target.addItem(it.name, it.id)
        lay.addWidget(self._rf_target)
        # 鉴定
        lay.addWidget(self._mk_lbl("鉴定卷轴：", "#9aa5ce"))
        self._rf_scroll = QComboBox()
        for it in id_scrolls:
            self._rf_scroll.addItem(it.name, it.id)
        lay.addWidget(self._rf_scroll)
        ab = QPushButton("鉴 定")
        ab.setEnabled(bool(equips) and bool(id_scrolls))
        ab.clicked.connect(self._on_appraise_sel)
        lay.addWidget(ab)
        # 洗练
        lay.addWidget(self._mk_lbl("洗练石：", "#9aa5ce"))
        self._rf_stone = QComboBox()
        for it in rf_stones:
            self._rf_stone.addItem(it.name, it.id)
        lay.addWidget(self._rf_stone)
        rb = QPushButton("洗 练")
        rb.setEnabled(bool(equips) and bool(rf_stones))
        rb.clicked.connect(self._on_refine_sel)
        lay.addWidget(rb)

    def _on_appraise_sel(self):
        tid = self._rf_target.currentData()
        sid = self._rf_scroll.currentData()
        if not tid or not sid:
            return
        ok, reason = rfe.appraise_check(self.world, self.world.player, tid, sid)
        if not ok:
            QMessageBox.information(self, "鉴定", reason)
            return
        item = next((i for i in (self.world.items or []) if getattr(i, "id", "") == tid), None)
        reagent = next((i for i in (self.world.items or []) if getattr(i, "id", "") == sid), None)
        overlay = DiceCheckOverlay(rfe.appraise_chance(item, reagent), parent=self)
        overlay.exec()
        if overlay.result is None:
            return
        r = rfe.appraise(self.world, self.world.player, tid, sid, dice=overlay.result)
        QMessageBox.information(self, "鉴定",
                                ("鉴定成功！" if r.get("ok") else r.get("reason", "")))
        self._after_change("")

    def _on_refine_sel(self):
        tid = self._rf_target.currentData()
        sid = self._rf_stone.currentData()
        if not tid or not sid:
            return
        ok, reason = rfe.refine_check(self.world, self.world.player, tid, sid)
        if not ok:
            QMessageBox.information(self, "洗练", reason)
            return
        item = next((i for i in (self.world.items or []) if getattr(i, "id", "") == tid), None)
        reagent = next((i for i in (self.world.items or []) if getattr(i, "id", "") == sid), None)
        overlay = DiceCheckOverlay(rfe.appraise_chance(item, reagent), parent=self)
        overlay.exec()
        if overlay.result is None:
            return
        r = rfe.refine(self.world, self.world.player, tid, sid, dice=overlay.result)
        QMessageBox.information(self, "洗练",
                                ("洗练成功！" if r.get("ok") else r.get("reason", "")))
        self._after_change("")

    def _panel_garden(self, home, gt, lay):
        cap = fe.plot_capacity(home)
        if cap <= 0:
            lay.addWidget(self._mk_lbl("灵田未生效。", "#565f89"))
            return
        t = QLabel(f"灵田（{len(home.garden)}/{cap} 块）")
        t.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        lay.addWidget(t)
        seeds, seen = [], set()
        for iid in list(self.world.player.inventory or []):
            if iid in seen:
                continue
            it = self._item(iid)
            if it is not None and fe.is_seed_item(it):
                seeds.append(it)
                seen.add(iid)
        if len(home.garden) < cap and seeds:
            row = QHBoxLayout()
            combo = QComboBox()
            for s in seeds:
                combo.addItem(s.name, s.id)
            row.addWidget(QLabel("播种："), 0)
            row.addWidget(combo, 1)
            pb = QPushButton("播 种")
            pb.clicked.connect(lambda _=False, c=combo: self._on_plant(home, c.currentData()))
            row.addWidget(pb)
            rw = QWidget()
            rw.setLayout(row)
            lay.addWidget(rw)
        for r in fe.farm_summary(self.world, home):
            lay.addWidget(self._plot_chip(home, r))
        for _i in range(max(0, cap - len(home.garden))):
            lay.addWidget(self._chip("空块", "info"))

    def _mk_lbl(self, text, color) -> QLabel:
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet(f"color:{color}; font-size:11px;")
        return l

    # ---------- 动作 ----------
    def _after_change(self, msg: str):
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        self.status_lbl.setText(msg)
        self._rebuild()
        self.changed.emit()

    def _on_deposit(self, home, item_id: str):
        it = self._item(item_id)
        if not he.stash_deposit(home, self.world.player, item_id):
            QMessageBox.warning(self, "无法入仓", "仓库已满（购置储物家具可扩容）。")
            return
        self._after_change(f"已入仓：{it.name if it else item_id}")

    def _on_withdraw(self, home, item_id: str):
        it = self._item(item_id)
        if not he.stash_withdraw(home, self.world.player, item_id):
            QMessageBox.warning(self, "无法出仓", "背包已满（负重上限），先整理背包或入仓其他物品。")
            return
        self._after_change(f"已出仓：{it.name if it else item_id}")

    def _on_shelf_put(self, home, item_id: str):
        it = self._item(item_id)
        if not he.shelf_put(home, self.world.player, item_id):
            QMessageBox.warning(self, "无法上架", "陈列架已满（购置陈列家具可扩位）。")
            return
        self._after_change(f"已陈列：{it.name if it else item_id}")

    def _on_shelf_take(self, home, item_id: str):
        it = self._item(item_id)
        if not he.shelf_take(home, self.world.player, item_id):
            QMessageBox.warning(self, "无法取回", "背包已满（负重上限），先整理背包。")
            return
        self._after_change(f"已取回：{it.name if it else item_id}")

    def _on_upgrade_building(self, home, kind: str):
        # [P34c] 升级住宅内建筑（level+1，扣金币 + 消耗材料）
        b = he.building_of(home, kind)
        name = b.get("name", kind) if b else kind
        ok, err = he.upgrade_building(self.world, home, kind, self.preset)
        if not ok:
            QMessageBox.warning(self, "升级失败", err or "资源不足或已达上限。")
            return
        QMessageBox.information(
            self, "升级成功",
            f"「{name}」已升级至 {he.building_level(home, kind)} 级。")
        self._after_change("")

    # ---------- [P35] 种植 ----------
    def _on_plant(self, home, seed_item_id: str):
        ok, msg = fe.plant(self.world, home, seed_item_id)
        if not ok:
            QMessageBox.warning(self, "播种失败", msg)
            return
        self._after_change(msg)

    def _on_water(self, home, idx: int):
        ok, msg = fe.water(self.world, home, idx)
        if not ok:
            QMessageBox.warning(self, "浇水失败", msg)
            return
        self._after_change(msg)

    def _on_harvest(self, home, idx: int):
        r = fe.harvest(self.world, home, idx)
        if not r.get("ok"):
            QMessageBox.warning(self, "收获失败", r.get("reason", ""))
            return
        note = f"收获{r.get('crop', '')} x{r.get('count', 0)}"
        if r.get("variant"):
            note += f"——种出了变异株，返还一颗种子！" if r.get("seeds_back") \
                else "——种出了变异株！（背包已满，种子掉落）"
        self._after_change(note)

    def _on_buy(self, loc, tier: int):
        price = he.home_price(self.world, loc, tier)
        gt = self._gt()
        # [2026-08-31 改自命名] 玩家自命名：预填题材池名作建议，可改；取消则不购
        name, ok = QInputDialog.getText(
            self, "购置住宅",
            f"在「{loc.name}」购下{he.tier_name(self.world, tier)}（{price} {gt.currency}，"
            f"持有 {self.world.player.gold} {gt.currency}）。请为你的宅邸命名：",
            text=he.fallback_home_name(self.world, loc),
        )
        if not ok:
            return
        ok2, err = he.buy_home(self.world, loc, tier, (name or "").strip(), "")
        if not ok2:
            QMessageBox.warning(self, "购宅失败", err or "未知错误")
            return
        home = he.home_at(self.world, loc.id)
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        QMessageBox.information(
            self, "购宅成功",
            f"你成为「{home.name}」的主人——{home.desc or '安身之所'}。"
            f"\n（仓库 {he.stash_capacity(home)} 格 / 陈列 {he.display_capacity(home)} 位）")
        # [P50 地点网格 2026-09-25] 购宅即安置：打开地点网格选一块宅位落格（可跳过，
        # 稍后也能在村庄地图点灰格补安置）
        try:
            from src.ui.dialogs.place_grid_dialog import PlaceGridDialog
            if QMessageBox.question(
                    self, "安置宅邸",
                    f"要在地图上为「{home.name}」选一块安置的位置吗？"
                    f"（也可稍后在村庄地图点灰格安置）",
                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
                PlaceGridDialog(self.world, loc, home=home, parent=self).exec()
        except Exception:
            pass
        self.status_lbl.setText("")
        # 购宅必在宅聚落：与「在宅聚落开 dialog 默认宅内」同口径，直接入宅内视图
        self._view_home_id = home.id
        self._rebuild()
        self.changed.emit()

    def _on_rest(self):
        loc, home = self._cur_home()
        if home is None:
            return
        self.rest_requested.emit(home.name)
        self.accept()

    def _on_banquet(self):
        """[P60 宴请] 设宴：引擎结算（金币+冷却+宾客），完成后 _after_change 落盘重建。"""
        loc, home = self._cur_home()
        if home is None:
            return
        ok, reason = he.can_host_banquet(self.world, home)
        if not ok:
            QMessageBox.information(self, "设宴", reason)
            return
        cost = he.banquet_cost(home)
        if QMessageBox.question(
                self, "设宴",
                f"花 {cost}（{self._gt().currency}）在{home.name}设下宴席，"
                "宴请同聚落的熟识？\n宾客每人交情 +4、记「慷慨」印象，宅邸势力声望 +1。",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        res = he.host_banquet(self.world, home)
        if not res.get("ok"):
            QMessageBox.information(self, "设宴", res.get("msg", "宴未开成。"))
            self._rebuild()
            return
        self._after_change(res.get("msg", "宴席圆满。"))

    # ---------- 双视图导航 ----------
    def _enter_home(self, home_id: str):
        self._view_home_id = home_id
        self._rebuild()

    def _back_to_overview(self):
        self._view_home_id = None
        self._rebuild()

    def _on_travel(self, home_id: str):
        """「前往」：确认后发 travel_requested + 关窗，场景页起 go_home 归宅回合。"""
        home = next((h for h in self.world.homes if h.id == home_id), None)
        if home is None:
            return
        loc = next((l for l in self.world.locations if l.id == home.location_id), None)
        loc_name = loc.name if loc else "住宅所在聚落"
        ret = QMessageBox.question(
            self, "确认归宅",
            f"你不在该宅所在聚落。现在动身返回「{loc_name}」？"
            f"（消耗 1 回合世界演化）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self.travel_requested.emit(home_id)
        self.accept()
