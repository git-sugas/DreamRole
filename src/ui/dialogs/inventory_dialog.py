"""世界模拟背包/装备对话框（P3 CRPG 数值层；P5c 纸娃娃 UI 重做；P34d 分类 tab 化）。

[P5c] 布局：左背包列表 + 右纸娃娃（中央立绘 + 8 槽环绕 + 顶部战斗数值面板）。
- 8 槽位：head/chest/legs/feet/main_hand/off_hand/accessory1/accessory2
- 槽位题材化显示名走 world.config_overlay.slot_display_names（缺省回退中文兜底）
- 立绘：PlayerState.avatar（无图时走渐变占位 + 玩家名/职业）
- 战斗数值面板：调 combat_engine.compute_stats 算聚合 atk/def/crit_rate/crit_dmg/speed
- 装备：双击背包物品自动装入对应槽（按 Item.slot 或 Item.type 反查）；
  点击槽位弹"卸下"按钮（每个槽独立，不再循环找第一个）
- 修复 P3 bug：丢弃前检查是否已装备，warn 后再丢

[P34d] 背包格子化与分类：左侧从单列 QListWidget 升级为「QTabWidget 分类 tab + 每 tab
QListWidget」，按 Item 的 effective_category（P34a）分桶——装备/消耗品/技能书/材料
(锻造·制造·采集)/培养(鉴定洗练 reagent)/任务/其他。空 tab 隐藏。右键菜单扩
「鉴定/洗练/使用/装备/丢弃/入仓」（P34b/P34c 入口）。纸娃娃右面板 + 顶部战斗面板
不动（读 equipped/inventory by id，与渲染解耦）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QFrame, QSplitter, QMessageBox, QGridLayout,
    QTabWidget, QMenu, QWidget,
)

from src.config import paths
from src.models import World, Item
from src.models.world import EQUIPMENT_SLOTS, SLOT_TO_TYPE, effective_category
from src.models.world_sim_preset import GenreText
from src.ui.widgets.rarity_frame import RarityFrame


_RARITY_COLOR = {
    "common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
    "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555",
}
# 兜底槽位显示名（无 overlay 时用，与西幻 CRPG 一致）
_DEFAULT_SLOT_LABEL = {
    "head": "头部", "chest": "胸甲", "legs": "护腿", "feet": "靴子",
    "main_hand": "主手", "off_hand": "副手",
    "accessory1": "饰品1", "accessory2": "饰品2",
}
# 兜底属性显示名（无 overlay 时用）
_DEFAULT_STAT_LABEL = {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"}

# [P34d] 背包分类 tab 固定顺序（按 effective_category 值分桶；底层字段名守 §23 不变，
# tab 标题走 GenreText.item_category 题材化）。顺序=真游戏背包常见排布：装备在前、
# 消耗/技能书次之、材料(锻造·制造·采集)再次、培养 reagent、任务、其他兜底末尾。
_CATEGORY_ORDER = (
    "weapon", "armor", "accessory",
    "consume", "skillbook", "pet_food",
    "material", "forge", "craft", "seed",
    "cultivate", "key", "misc",
)


class InventoryDialog(QDialog):
    """背包/装备对话框（P5c 纸娃娃布局 + P34d 分类 tab）。"""

    def __init__(self, world: World, storage=None, on_changed=None,
                 open_refine=None, open_home=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.storage = storage
        self.on_changed = on_changed
        # [P34d] 右键菜单「鉴定/洗练」「入仓」入口回调：场景页提供（打开 RefineDialog /
        # HomeDialog）。无回调时（如测试）菜单项仍弹，handler 内回退提示走场景页操作。
        self.open_refine = open_refine
        self.open_home = open_home
        self.setWindowTitle("背包与装备")
        self.resize(900, 640)

        # 读 overlay 的题材化显示名（缺省兜底）
        ov = world.config_overlay if isinstance(world.config_overlay, dict) else {}
        # [P6] 题材化 bundle：货币单位/品级名/物品大类名（底层字段不变，仅显示中文名题材适配）。
        self._gt = GenreText(ov)
        self._slot_labels_map = dict(_DEFAULT_SLOT_LABEL)
        sdn_ov = ov.get("slot_display_names")
        if isinstance(sdn_ov, dict):
            for k in EQUIPMENT_SLOTS:
                if sdn_ov.get(k):
                    self._slot_labels_map[k] = sdn_ov[k]
        self._stat_labels_map = dict(_DEFAULT_STAT_LABEL)
        statn_ov = ov.get("stat_display_names")
        if isinstance(statn_ov, dict):
            for k in ("str", "dex", "int", "vit", "luk"):
                if statn_ov.get(k):
                    self._stat_labels_map[k] = statn_ov[k]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # 顶部：金币 + 战斗数值面板
        outer.addLayout(self._build_top_bar())

        # 主体：左背包 / 右纸娃娃
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_inventory_panel())
        splitter.addWidget(self._build_paper_doll_panel())
        splitter.setSizes([300, 580])
        outer.addWidget(splitter, 1)

        # 底部关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._refresh()

    # ============ 顶部：金币 + 战斗数值面板 ============
    def _build_top_bar(self) -> QHBoxLayout:
        """[UI 改造 2026-10-01 第三批] 战力读数徽章行（替代单条长文本）：货币金徽章 +
        攻击/防御/暴击/速度/气血上限/负重六枚固定徽章 + 特效词条动态徽章区。"""
        # [复评收口] 两行布局防溢出：行1 货币 + 特效词条；行2 战力五徽章（气血上限已在下方概要行，不重复占位）
        vbox = QVBoxLayout()
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.gold_label = QLabel()
        self.gold_label.setObjectName("gameBadge")
        self.gold_label.setProperty("level", "gold")
        row1.addWidget(self.gold_label)
        row1.addStretch()
        self._fx_holder = QWidget()
        self._fx_row = QHBoxLayout(self._fx_holder)
        self._fx_row.setContentsMargins(0, 0, 0, 0)
        self._fx_row.setSpacing(6)
        row1.addWidget(self._fx_holder)
        vbox.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._combat_badges: dict = {}
        for key, level in (("atk", "danger"), ("def", "info"), ("crit", "warn"),
                           ("speed", "info"), ("cap", "info")):
            b = QLabel()
            b.setObjectName("gameBadge")
            b.setProperty("level", level)
            self._combat_badges[key] = b
            row2.addWidget(b)
        row2.addStretch()
        vbox.addLayout(row2)
        vbox.setSpacing(4)
        return vbox

    # ============ 左：背包面板 ============
    def _build_inventory_panel(self) -> QFrame:
        left = QFrame()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        ll.addWidget(QLabel("背包"))
        # [P34d] QTabWidget 分类：每个 tab = 一个 category，按 _CATEGORY_ORDER 固定顺序，
        # _refresh 时按 effective_category 分桶填各 tab，空 tab 隐藏。每 tab 一个 QListWidget
        # 复用现有 _make_item_widget（RarityFrame 品级边框 + tooltip + item_brief 显示口径）。
        self.category_tabs = QTabWidget()
        self.category_tabs.setDocumentMode(True)
        # category -> QListWidget（仅含「曾有过物品」的 tab；_refresh 动态显隐）
        self.inv_lists: dict[str, QListWidget] = {}
        for cat in _CATEGORY_ORDER:
            lw = QListWidget()
            lw.itemClicked.connect(self._on_inv_click)
            lw.itemDoubleClicked.connect(self._on_inv_double_click)
            lw.setContextMenuPolicy(Qt.CustomContextMenu)
            lw.customContextMenuRequested.connect(
                lambda pos, _lw=lw: self._on_inv_context_menu(_lw, pos))
            self.inv_lists[cat] = lw
            # tab 标题走题材化分类名（带占位计数，_refresh 时更新）
            self.category_tabs.addTab(lw, self._gt.item_category(cat))
        # tab 切换重算当前 tab 选中态（按钮启用）
        self.category_tabs.currentChanged.connect(lambda _i: self._on_inv_click())
        ll.addWidget(self.category_tabs, 1)
        # 操作按钮（使用 / 丢弃，装备走双击）
        btn_row = QHBoxLayout()
        self.use_btn = QPushButton("使用")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._on_use)
        self.drop_btn = QPushButton("丢弃")
        self.drop_btn.setObjectName("dangerBtn")
        self.drop_btn.setEnabled(False)
        self.drop_btn.clicked.connect(self._on_drop)
        btn_row.addWidget(self.use_btn)
        btn_row.addWidget(self.drop_btn)
        ll.addLayout(btn_row)
        # 提示
        hint = QLabel("双击物品自动装备；右键更多操作；点击槽位弹卸下")
        hint.setStyleSheet("color:#565f89; font-size:11px;")
        hint.setWordWrap(True)
        ll.addWidget(hint)
        return left

    # ============ 右：纸娃娃面板 ============
    def _build_paper_doll_panel(self) -> QFrame:
        right = QFrame()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        # 8 槽 + 中央立绘的 QGridLayout（4 行 3 列）
        grid_frame = QFrame()
        gl = QGridLayout(grid_frame)
        gl.setContentsMargins(8, 8, 8, 8)
        gl.setSpacing(6)

        # 中央立绘（跨 row 0-2 col 1）
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(200, 240)
        self.avatar_label.setAlignment(Qt.AlignCenter)
        self.avatar_label.setObjectName("bannerPlaceholder")
        self.avatar_label.setStyleSheet(
            "QLabel{background: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1,"
            " stop:0 #1a1b26, stop:0.5 #2a2e3d, stop:1 #2a2540);"
            " border:1px solid #2c2e42; border-radius:8px; color:#565f89; font-size:13px;}"
        )
        gl.addWidget(self.avatar_label, 0, 1, 3, 1, Qt.AlignCenter)

        # 8 槽位 widget（按位置摆放）
        # 布局示意（4 行 3 列，立绘占中央 row 0-2 col 1）：
        #   row 0: [空]      [head]    [空]
        #   row 1: [main_hand] [立绘]  [off_hand]
        #   row 2: [acc1]    [立绘]    [acc2]
        #   row 3: [feet]    [chest]   [legs]
        self.slot_widgets: dict[str, QFrame] = {}
        self.slot_item_labels: dict[str, QLabel] = {}
        self.slot_unequip_btns: dict[str, QPushButton] = {}
        slot_positions = {
            "head": (0, 0),
            "main_hand": (1, 0),
            "accessory1": (2, 0),
            "feet": (3, 0),
            "off_hand": (0, 2),
            "accessory2": (1, 2),
            "chest": (2, 2),
            "legs": (3, 2),
        }
        for slot, (r, c) in slot_positions.items():
            w = self._build_slot_widget(slot)
            gl.addWidget(w, r, c)
            self.slot_widgets[slot] = w

        # 底部属性面板（5 项属性 + HP + level）
        self.stats_label = QLabel("")
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet(
            "QLabel{background:#1f2335; border-radius:6px; padding:8px; color:#c0caf5;"
            " font-size:12px;}"
        )
        rl.addWidget(grid_frame, 1)
        rl.addWidget(self.stats_label)
        return right

    def _build_slot_widget(self, slot: str) -> QFrame:
        """构造单个装备槽 widget（标题 + 装备图 + 名字 + 卸下按钮，属性悬浮）。"""
        box = QFrame()
        box.setFixedSize(148, 104)
        box.setStyleSheet(
            "QFrame{background:#1f2335; border:1px solid #2c2e42; border-radius:6px;}"
            "QFrame:hover{border-color:#7aa2f7;}"
        )
        bl = QVBoxLayout(box)
        bl.setContentsMargins(6, 4, 6, 4)
        bl.setSpacing(2)
        # 槽位标题（题材化）
        title = QLabel(self._slot_labels_map.get(slot, slot))
        title.setStyleSheet("color:#7aa2f7; font-size:11px; font-weight:bold;")
        title.setAlignment(Qt.AlignCenter)
        bl.addWidget(title)
        # 装备图（有 icon 展示图片，无图占位块）
        icon_row = QHBoxLayout()
        icon_row.setContentsMargins(0, 0, 0, 0)
        icon_row.addStretch()
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon_row.addWidget(icon, 0, Qt.AlignVCenter)
        icon_row.addStretch()
        bl.addLayout(icon_row)
        if not hasattr(self, "slot_icon_labels"):
            self.slot_icon_labels = {}
        self.slot_icon_labels[slot] = icon
        # 物品名 label（属性进 tooltip）
        item_lbl = QLabel("（空）")
        item_lbl.setWordWrap(True)
        item_lbl.setAlignment(Qt.AlignCenter)
        item_lbl.setStyleSheet("color:#565f89; font-size:11px;")
        bl.addWidget(item_lbl, 1)
        self.slot_item_labels[slot] = item_lbl
        # 卸下按钮（默认隐藏）
        unequip_btn = QPushButton("卸下")
        unequip_btn.setObjectName("dangerBtn")
        unequip_btn.setFixedHeight(18)
        unequip_btn.setStyleSheet(
            "QPushButton{font-size:10px; padding:1px 4px; border-radius:3px;}"
        )
        unequip_btn.hide()
        unequip_btn.clicked.connect(lambda _=False, s=slot: self._on_unequip_slot(s))
        bl.addWidget(unequip_btn)
        self.slot_unequip_btns[slot] = unequip_btn
        # 点击槽位切换卸下按钮可见性
        box.mousePressEvent = lambda e, s=slot: self._on_slot_click(s)
        return box

    # ============ 刷新 ============
    def _item_tooltip(self, it: Item) -> str:
        """物品完整属性悬浮提示（复用 item_brief.item_tooltip 统一显示口径）。

        [P34b] 共享版已处理 display_name（未鉴定打码 + affix 前缀名）+ 未鉴定属性隐藏 +
        affix 词条显示，避免两处显示逻辑漂移。
        """
        from src.ui.widgets.item_brief import item_tooltip
        return item_tooltip(self.world, it, self._gt)

    def _make_item_widget(self, it: Item, color: str) -> RarityFrame:
        """[P6] 背包物品行：RarityFrame 品级边框 + [物品图 | 名字/类型小字] 横排。

        [!] 属性数值不进行内（挤且看不清），全部走悬浮 tooltip；有 icon 展示
        物品图（世界生成时已出的 45+ 张），无 icon 用品级色占位块。
        """
        from src.ui.widgets.item_brief import full_item_pixmap, display_name, kind_label
        rf = RarityFrame(it.rarity)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        icon = QLabel()
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(full_item_pixmap(it.name, getattr(it, "icon", ""), 44))
        row.addWidget(icon, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        name = QLabel(display_name(it))
        name.setStyleSheet(f"color:{color}; font-weight:bold;")
        name.setWordWrap(True)
        col.addWidget(name)
        # [2026-08-23] 类别小字按 slot 题材名（与装备栏一致），不再用笼统 type 名
        sub = QLabel(f"{self._gt.rarity(it.rarity)} · {kind_label(self.world, it, self._gt)}")
        sub.setStyleSheet("color:#9aa5ce; font-size:11px;")
        col.addWidget(sub)
        row.addLayout(col, 1)
        rf.addLayout(row)
        rf.setToolTip(self._item_tooltip(it))
        rf.setToolTipDuration(10000)
        return rf

    def _refresh(self):
        """刷新背包（按 category 分桶填各 tab + 空 tab 隐藏）+ 槽位 + 立绘 + 战斗数值 + 属性面板。"""
        p = self.world.player
        self.gold_label.setText(f"{self._gt.currency}：{p.gold}")

        # [P34d] 背包按 effective_category（P34a）分桶填各 tab。空 tab 隐藏（真游戏背包只显示
        # 有物的分类）。QListWidgetItem 持有 data(UserRole)=iid 供单击/双击/右键逻辑复用。
        buckets: dict[str, list[tuple[str, Item]]] = {c: [] for c in _CATEGORY_ORDER}
        for iid in p.inventory:
            it = next((i for i in self.world.items if i.id == iid), None)
            if it is None:
                continue
            cat = effective_category(it)
            if cat not in buckets:
                cat = "misc"  # 防御：effective_category 已钳制白名单，兜底归「其他」
            buckets[cat].append((iid, it))

        # 记录刷新前当前 tab（category），刷新后尽量保持选中同一分类
        prev_cat = self._current_category()
        for idx, cat in enumerate(_CATEGORY_ORDER):
            lw = self.inv_lists[cat]
            lw.clear()
            items = buckets[cat]
            if items:
                # tab 可见 + 标题带计数
                if not self.category_tabs.isTabVisible(idx):
                    self.category_tabs.setTabVisible(idx, True)
                self.category_tabs.setTabText(idx, f"{self._gt.item_category(cat)}({len(items)})")
                for iid, it in items:
                    color = _RARITY_COLOR.get(it.rarity, "#c0caf5")
                    wid = self._make_item_widget(it, color)
                    item = QListWidgetItem()
                    item.setData(Qt.UserRole, iid)
                    wid_w = max(140, lw.viewport().width() - 12)
                    # 实测 + 12px 下边距：heightForWidth 只量内容布局，不含 QSS 边框
                    # 与 epic+ 流光外层柔光，边距不足卡片底边被下一行遮（6px 仍裁，
                    # 三次真人测试反馈加到 12；与 shop_dialog._row_height 同口径）
                    if wid.hasHeightForWidth():
                        row_h = max(74, wid.heightForWidth(wid_w) + 12)
                    else:
                        row_h = max(74, wid.sizeHint().height() + 12)
                    item.setSizeHint(QSize(0, row_h))
                    lw.addItem(item)
                    lw.setItemWidget(item, wid)
            else:
                # 空 tab 隐藏；若当前 tab 被隐藏，切到首个可见 tab（_on_inv_click 重算按钮）
                self.category_tabs.setTabVisible(idx, False)

        # 切到合适 tab：保持原分类 / 首个可见 / 兜底 0
        visible_idxs = [i for i in range(self.category_tabs.count())
                        if self.category_tabs.isTabVisible(i)]
        if visible_idxs:
            if not self.category_tabs.isTabVisible(self.category_tabs.currentIndex()):
                self.category_tabs.setCurrentIndex(visible_idxs[0])
            elif prev_cat and prev_cat in self.inv_lists:
                ti = _CATEGORY_ORDER.index(prev_cat)
                if self.category_tabs.isTabVisible(ti):
                    self.category_tabs.setCurrentIndex(ti)
        # 没有任何物品时 tab 全隐藏 -> 强制显示「其他」tab 作空态（否则 QTabWidget 看着像坏的）
        if not visible_idxs:
            misc_idx = _CATEGORY_ORDER.index("misc")
            self.category_tabs.setTabVisible(misc_idx, True)
            self.category_tabs.setCurrentIndex(misc_idx)

        # 8 槽位（[P12f] 装备图完整显示：full_item_pixmap 等比不裁剪）
        from src.ui.widgets.item_brief import full_item_pixmap
        equipped = p.equipped if isinstance(p.equipped, dict) else {}
        for slot in EQUIPMENT_SLOTS:
            iid = equipped.get(slot)
            lbl = self.slot_item_labels[slot]
            btn = self.slot_unequip_btns[slot]
            icon = self.slot_icon_labels.get(slot)
            box = self.slot_widgets.get(slot)
            if iid:
                it = next((i for i in self.world.items if i.id == iid), None)
                if it:
                    color = _RARITY_COLOR.get(it.rarity, "#c0caf5")
                    lbl.setText(it.name)
                    lbl.setStyleSheet(f"color:{color}; font-size:11px;")
                    if icon is not None:
                        icon.setPixmap(full_item_pixmap(it.name, getattr(it, "icon", ""), 40))
                    if box is not None:
                        box.setToolTip(self._item_tooltip(it))
                        box.setToolTipDuration(10000)
                    btn.show()
                else:
                    lbl.setText("（数据缺失）")
                    lbl.setStyleSheet("color:#f7768e; font-size:11px;")
                    btn.hide()
            else:
                lbl.setText("（空）")
                lbl.setStyleSheet("color:#565f89; font-size:11px;")
                if icon is not None:
                    icon.setPixmap(full_item_pixmap("", "", 40))
                if box is not None:
                    box.setToolTip(self._slot_labels_map.get(slot, slot))
                btn.hide()

        # 立绘
        self._refresh_avatar()

        # 战斗数值面板（调 combat_engine 算聚合）
        self._refresh_combat_panel()

        # 属性面板
        sdn = self._stat_labels_map
        # 五维常态值含装备、天赋、固有加成和词缀，括号标来源。
        from src.services import combat_engine as ce
        def _st(key: str, base: int) -> str:
            return ce.primary_stat_display(self.world, p, key)
        self.stats_label.setText(
            f"等级:{p.level} | {self._gt.hp}:{p.hp}/{p.hp_max} | 经验:{p.xp}/{p.xp_next}\n"
            f"{sdn['str']}{_st('str', p.stat_str)} {sdn['dex']}{_st('dex', p.stat_dex)} "
            f"{sdn['int']}{_st('int', p.stat_int)} {sdn['vit']}{_st('vit', p.stat_vit)} "
            f"{sdn['luk']}{_st('luk', p.stat_luk)} | 装备:{len(equipped)}/8"
        )
        self.stats_label.setToolTip(
            "五维括号：装=装备、赋=天赋、固=固有、词=词缀；显示常态值，饥饿和伤势在战斗时另计。")

        # 刷新后重算按钮启用态（当前 tab 选中项决定）
        self._on_inv_click()

    def _current_category(self) -> str | None:
        """当前可见 tab 对应的 category（无可见 tab 返回 None）。"""
        idx = self.category_tabs.currentIndex()
        if idx < 0 or not self.category_tabs.isTabVisible(idx):
            return None
        return _CATEGORY_ORDER[idx]

    def _current_list(self) -> QListWidget:
        """当前 tab 的 QListWidget（刷新期 tab 可能全隐藏，兜底返回 misc 列表）。"""
        cat = self._current_category() or "misc"
        return self.inv_lists[cat]

    def _refresh_avatar(self):
        """刷新中央立绘（有 avatar 文件 -> 加载；否则渐变占位 + 玩家名/职业）。"""
        p = self.world.player
        if p.avatar:
            pm = QPixmap(self._avatar_path(p.avatar))
            if not pm.isNull():
                self.avatar_label.setPixmap(pm.scaled(
                    180, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation,
                ))
                return
        # 占位：玩家名 + 职业
        self.avatar_label.clear()
        name = p.class_name or "无名"
        bg = p.background or ""
        text = f"{name}\n\n{bg[:14] + '…' if len(bg) > 14 else bg}" if bg else name
        self.avatar_label.setText(text)

    def _avatar_path(self, filename: str) -> str:
        import os
        return os.path.join(paths.world_images_dir(), filename)

    def _equipped_stat_bonus(self) -> dict:
        """[C1 修复 2026-08-25] 已装备物品顶层 stat_bonus 合计。

        [修 2026-09-10] 改为**委托** combat_engine.equipped_stat_bonus（单一来源）：原先此处
        是独立复制的同口径实现，而 read.md 声称「svc 与 inventory_dialog 均委托」——口径漂移
        隐患（战斗侧改一处、面板忘改就会「结算生效了、面板还打裸值」）。
        """
        from src.services.combat_engine import equipped_stat_bonus
        return equipped_stat_bonus(self.world, self.world.player)

    def _refresh_combat_panel(self):
        """算聚合 atk/def/crit_rate/crit_dmg/speed 显示在顶部。"""
        try:
            from src.services import combat_engine as ce
            p = self.world.player
            # 复用引擎聚合（与战斗结算一致）
            w_atk, a_def = ce.equipped_attack_defense(self.world, p)
            affixes = []
            equipped = p.equipped if isinstance(p.equipped, dict) else {}
            for slot, iid in equipped.items():
                it = next((i for i in self.world.items if i.id == iid), None)
                if not it:
                    continue
                if it.affixes:
                    affixes.extend(it.affixes)
            snap = ce.compute_stats(p, weapon_attack=w_atk, armor_defense=a_def,
                                    affixes=affixes, equip_stat_bonus=self._equipped_stat_bonus(),
                                    hunger_mult=ce.hunger_stat_mult(p))
            # [P8] 负重：物品种类 / carry_capacity（vit 驱动，=20+vit*2）
            cap = ce.carry_capacity(p)
            cur = len(getattr(p, "inventory", []) or [])
            cap_color = "#e0af68" if cur >= cap else "#9aa5ce"
            # [装备新属性 2026-09-01] 概率型三件套（有值才显示，零值不占位）
            fx = []
            if snap.counter_rate > 0:
                fx.append(f"反击 {round(snap.counter_rate*100,1)}%")
            if snap.combo_rate > 0:
                fx.append(f"连击 {round(snap.combo_rate*100,1)}%")
            if snap.lifesteal_rate > 0:
                fx.append(f"吸血 {round(snap.lifesteal_rate*100,1)}%")
            # [方案 B 2026-09-10 用户拍板] 武器元素双向生效：面板显示附伤与受克元素
            # （形态与 item_brief 词条/read.md 契约统一：附X伤（受Y克））
            from src.services.combat_engine import ELEMENT_ZH, element_countered_by
            for _e in (getattr(snap, "elements", ()) or ()):
                _zh = ELEMENT_ZH.get(str(_e), str(_e))
                _cv = element_countered_by(_e)
                fx.append(f"附{_zh}伤" + (f"（受{ELEMENT_ZH.get(_cv, _cv)}克）" if _cv else ""))
            fx_txt_parts = fx  # [UI 改造] 特效词条改徽章区展示（富文本拼接废弃）
            cap_warn = cur >= cap
            b = self._combat_badges
            b["atk"].setText(f"攻击 {snap.atk}")
            b["def"].setText(f"防御 {snap.def_}")
            b["crit"].setText(f"暴击 {int(snap.crit_rate * 100)}% ×{snap.crit_dmg:.1f}")
            b["speed"].setText(f"速度 {snap.speed}")
            b["cap"].setText(f"负重 {cur}/{cap}")
            b["cap"].setProperty("level", "warn" if cap_warn else "info")
            b["cap"].setStyleSheet("")   # 清内联样式，让 QSS 属性选择器接管
            b["cap"].style().unpolish(b["cap"])
            b["cap"].style().polish(b["cap"])
            # 特效徽章区动态重建（反击/连击/吸血/附伤）
            while self._fx_row.count():
                _w = self._fx_row.takeAt(0).widget()
                if _w is not None:
                    _w.setParent(None)
                    _w.deleteLater()
            from src.ui.widgets.game_widgets import game_badge
            for txt in fx_txt_parts:
                self._fx_row.addWidget(game_badge(txt, "gold"))
        except Exception as e:
            # 兜底：算不出来不阻塞 UI
            if "atk" in self._combat_badges:
                self._combat_badges["atk"].setText(f"数值面板加载失败：{e}")

    # ============ 交互 ============
    def _selected_item(self) -> Item | None:
        lw = self._current_list()
        row = lw.currentRow()
        if row < 0:
            return None
        iid = lw.item(row).data(Qt.UserRole)
        return next((i for i in self.world.items if i.id == iid), None)

    def _on_inv_click(self):
        it = self._selected_item()
        if it is None:
            self.use_btn.setEnabled(False)
            self.drop_btn.setEnabled(False)
            return
        # [P7k2] 使用按钮：消耗品 或 技能书（teach_skill 非空）可用
        self.use_btn.setEnabled(it.type == "consumable" or bool(getattr(it, "teach_skill", None)))
        # 丢弃始终可用（但 _on_drop 内会 warn 已装备的）
        self.drop_btn.setEnabled(True)

    def _on_inv_double_click(self):
        """双击背包物品：自动装入对应槽（按 Item.slot 或 Item.type 反查）。"""
        it = self._selected_item()
        if it is None:
            return
        # 消耗品走使用
        if it.type == "consumable":
            self._on_use()
            return
        # 装备类：找目标槽
        target_slot = self._resolve_target_slot(it)
        if target_slot is None:
            QMessageBox.information(self, "无法装备", f"{it.name} 不是可装备物品。")
            return
        self._equip_to_slot(it, target_slot)

    def _on_inv_context_menu(self, lw: QListWidget, pos):
        """[P34d] 背包格子右键菜单：使用/装备/鉴定·洗练/入仓/丢弃。

        鉴定·洗练 + 入仓为 P34b/P34c 入口：有回调时调场景页打开对应对话框，无回调时提示
        走场景页按钮（测试/无场景页宿主时优雅降级）。菜单构建抽 _build_inv_context_menu
        便于测试断言 actions（不 exec）。
        """
        built = self._build_inv_context_menu(lw, pos)
        if built is None:
            return
        menu, it, acts = built
        action = menu.exec(lw.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action is acts.get("use"):
            self._on_use()
        elif action is acts.get("equip"):
            target_slot = self._resolve_target_slot(it)
            if target_slot is None:
                QMessageBox.information(self, "无法装备", f"{it.name} 不是可装备物品。")
                return
            self._equip_to_slot(it, target_slot)
        elif action is acts.get("refine"):
            self._open_refine_dialog()
        elif action is acts.get("stash"):
            self._stash_item(it)
        elif action is acts.get("drop"):
            self._on_drop()

    def _build_inv_context_menu(self, lw: QListWidget, pos):
        """[P34d] 构造右键菜单（不 exec）。返回 (QMenu, Item, {act_key: QAction}) 或 None。

        抽出便于测试断言 actions 显隐（menu.exec 在 offscreen Qt 是模态会阻塞，测试直接读
        menu.actions() 文本）。act_key: use/equip/refine/stash/drop（不存在则缺键）。
        """
        item = lw.itemAt(pos)
        if item is None:
            return None
        iid = item.data(Qt.UserRole)
        it = next((i for i in self.world.items if i.id == iid), None)
        if it is None:
            return None
        menu = QMenu(self)
        acts: dict[str, object] = {}
        # 使用（消耗品 / 技能书）
        if it.type == "consumable" or getattr(it, "teach_skill", None):
            acts["use"] = menu.addAction("使用")
        # 装备（武器/护甲/饰品）
        if it.type in ("weapon", "armor", "accessory"):
            acts["equip"] = menu.addAction("装备")
        menu.addSeparator()
        # 鉴定·洗练（装备类 -> 器物台；cultivate reagent 也提示去器物台消耗）
        acts["refine"] = menu.addAction("鉴定·洗练…")
        # 入仓（仓库不占负重）
        acts["stash"] = menu.addAction("入仓")
        menu.addSeparator()
        acts["drop"] = menu.addAction("丢弃")
        return menu, it, acts

    def _open_refine_dialog(self):
        """[P34d] 右键「鉴定·洗练」入口：调场景页 open_refine 回调打开 RefineDialog。

        InventoryDialog 自身不持有 preset/svc，复用场景页已接好的 RefineDialog（含 save_world
        + refresh + world_changed）。无回调时提示用户走场景页「器物」按钮。
        """
        if self.open_refine is None:
            QMessageBox.information(
                self, "鉴定·洗练",
                "请关闭背包后，在场景页点「器物」按钮打开器物台进行鉴定/洗练。")
            return
        self.accept()  # 关闭背包让场景页弹 RefineDialog（避免模态对话框嵌套）
        self.open_refine()

    def _stash_item(self, it: Item):
        """[P34d] 右键「入仓」入口：把物品从背包移入仓库（home_engine.stash_deposit）。

        仓库不占负重（P24a）。需先有住宅：优先当前聚落的宅，否则任一自有宅。无宅提示去
        住宅页购置（走 open_home 回调或提示场景页「住宅」按钮）。
        """
        from src.services import home_engine as he
        home = self._player_home(he)
        if home is None:
            msg = "尚无住宅，无法入仓。"
            if self.open_home is not None:
                msg += "\n\n是否前往住宅页购置？"
                reply = QMessageBox.question(self, "无住宅", msg,
                                             QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if reply == QMessageBox.Yes:
                    self.accept()
                    self.open_home()
            else:
                msg += "\n请关闭背包后在场景页点「住宅」按钮购置。"
                QMessageBox.information(self, "无住宅", msg)
            return
        if not he.stash_deposit(home, self.world.player, it.id):
            QMessageBox.warning(self, "无法入仓", "仓库已满（购置储物家具可扩容）。")
            return
        self._save_and_refresh()
        QMessageBox.information(self, "入仓", f"已将「{it.name}」存入仓库「{home.name}」。")

    def _player_home(self, he):
        """取玩家可用住宅：当前聚落宅优先，否则任一自有宅。无宅返回 None。"""
        p = self.world.player
        # 当前聚落宅
        loc = next((l for l in self.world.locations
                    if l.id == getattr(p, "location_id", "")), None)
        if loc is not None:
            home = he.player_home_at(self.world, loc)
            if home is not None:
                return home
        # 任一自有宅（按 home_ids）
        for hid in (p.home_ids or []):
            home = next((h for h in self.world.homes if h.id == hid), None)
            if home is not None:
                return home
        return None

    def _resolve_target_slot(self, it: Item) -> str | None:
        """决定物品应装入哪个槽：优先 Item.slot，否则按 type 反查首个空槽。

        [!] 武器主手优先（用户指示 2026-08）：主手空时，攻击力>0 的武器（刀/剑/匕等主战兵器）
        一律入主手（兵刃），不看 Item.slot——避免「开局匕首在兵刃、卸下再装跑到暗器」的槽位跳变
        （根因：LLM 把短匕标 slot=off_hand，但匕首语义上是主战短兵）。盾/纯防御副武器（attack=0）
        不触发，仍按 slot 入副手；主手已有武器时也按 slot 入副手当副兵器。
        """
        equipped = self.world.player.equipped if isinstance(self.world.player.equipped, dict) else {}
        # 攻击型武器 + 主手空 -> 主手（兵刃）
        if it.type == "weapon" and it.attack > 0 and "main_hand" not in equipped:
            return "main_hand"
        # 优先 Item.slot
        if it.slot and it.slot in EQUIPMENT_SLOTS:
            # 校验 type 与 slot 兼容
            expected_type = SLOT_TO_TYPE.get(it.slot)
            # [修 2026-10-01 真机审计] off_hand 放行 armor：盾/纯防御副手件（LLM 合法产出
            # type=armor+slot=off_hand，武侠档实测 5 件盾类）曾被 type 校验拒绝 → 兜底
            # 「按 type 找首个空槽」把盾装进 head（头巾）槽。off_hand 已占时替换语义
            # 由 _equip_to_slot 处理（旧件回背包）。
            if expected_type == it.type or (it.slot == "off_hand" and it.type == "armor"):
                return it.slot
        # 按 type 找首个空槽
        for slot in EQUIPMENT_SLOTS:
            if SLOT_TO_TYPE.get(slot) == it.type and slot not in equipped:
                return slot
        # 都满了：返回该 type 的第一个槽（替换）
        for slot in EQUIPMENT_SLOTS:
            if SLOT_TO_TYPE.get(slot) == it.type:
                return slot
        return None

    def _equip_to_slot(self, it: Item, slot: str):
        """把物品装入指定槽（旧装备回背包）。"""
        if not isinstance(self.world.player.equipped, dict):
            self.world.player.equipped = {}
        old = self.world.player.equipped.get(slot)
        if old and old != it.id and old not in self.world.player.inventory:
            self.world.player.inventory.append(old)
        self.world.player.equipped[slot] = it.id
        if it.id in self.world.player.inventory:
            self.world.player.inventory.remove(it.id)
        self._save_and_refresh()

    def _on_slot_click(self, slot: str):
        """点击槽位：切换卸下按钮可见性。"""
        btn = self.slot_unequip_btns[slot]
        btn.setVisible(not btn.isVisible())

    def _on_unequip_slot(self, slot: str):
        """卸下指定槽的装备（回背包）。"""
        if not isinstance(self.world.player.equipped, dict):
            return
        iid = self.world.player.equipped.get(slot)
        if not iid:
            return
        if iid not in self.world.player.inventory:
            self.world.player.inventory.append(iid)
        del self.world.player.equipped[slot]
        self._save_and_refresh()

    def _on_use(self):
        it = self._selected_item()
        if it is None:
            return
        # [P7k2] 技能书：教会玩家技能（加 player.skills，去重 by name，消耗书）
        teach = getattr(it, "teach_skill", None)
        if isinstance(teach, dict) and teach:
            from src.models import Skill
            # [技能书个体差异 2026-09-10] 与 _resolve_use_item 同口径：书 id 盐抖动
            # power（品级定基准 + 每件确定性抖动；不改书内蓝图模板）
            from src.services.combat_engine import jitter_taught_skill
            sk = Skill.from_dict(jitter_taught_skill(
                teach, getattr(self.world, "id", ""), it.id)).to_dict()
            sk_name = sk.get("name", "技能")
            already = any(isinstance(s, dict) and s.get("name") == sk_name
                          for s in self.world.player.skills)
            if already:
                QMessageBox.information(self, "已学会", f"你已经掌握「{sk_name}」了。")
                return
            self.world.player.skills.append(sk)
            if it.id in self.world.player.inventory:
                self.world.player.inventory.remove(it.id)
            self._save_and_refresh()
            QMessageBox.information(self, "学会技能",
                                    f"学会了「{sk_name}」！可在场景页「技能」中装备到出战栏（最多 3 个）。")
            return
        if it.type != "consumable":
            return
        from src.services import combat_engine as ce
        # 优先 consume_effect（结构化效果：五维/回蓝等），其次百分比/整数回血
        ok, hint = ce.apply_consume_effect(self.world.player, it,
                                           equip_stat_bonus=self._equipped_stat_bonus())
        if ok:
            if it.id in self.world.player.inventory:
                self.world.player.inventory.remove(it.id)
            self._save_and_refresh()
            QMessageBox.information(self, "使用", hint)
            return
        if it.heal_pct <= 0 and it.heal_amount <= 0:
            if hint:
                QMessageBox.information(self, "使用", hint)
            return
        # [P7e] 治疗量叠加 stat_int 驱动的 heal_bonus（与 _resolve_use_item 口径一致，守 Req1）
        # [百分比回血 2026-09-01] heal_pct 优先（单一来源 item_heal_value）
        hp_before = self.world.player.hp
        snap = ce.compute_stats(self.world.player,
                                equip_stat_bonus=self._equipped_stat_bonus())
        ce.heal(self.world.player, ce.item_heal_value(it, self.world.player),
                bonus=snap.heal_bonus)
        actual = self.world.player.hp - hp_before
        # [P5c] 走 max_hp_for helper（与引擎权威公式一致）
        self.world.player.hp_max = ce.max_hp_for(self.world.player)
        if it.id in self.world.player.inventory:
            self.world.player.inventory.remove(it.id)
        self._save_and_refresh()
        QMessageBox.information(self, "使用", f"使用{it.name}，回复 {actual} HP。")

    def _on_drop(self):
        it = self._selected_item()
        if it is None:
            return
        # [P5c] 修 bug：丢弃前检查是否已装备（旧版会丢已装备物品但 equipped 字段残留）
        equipped = self.world.player.equipped if isinstance(self.world.player.equipped, dict) else {}
        equipped_slot = None
        for slot, iid in equipped.items():
            if iid == it.id:
                equipped_slot = slot
                break
        if equipped_slot:
            reply = QMessageBox.question(
                self, "确认丢弃",
                f"{it.name} 当前装备在「{self._slot_labels_map.get(equipped_slot, equipped_slot)}」槽。\n"
                f"丢弃会同时卸下该装备。确认继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            del self.world.player.equipped[equipped_slot]
        if it.id in self.world.player.inventory:
            self.world.player.inventory.remove(it.id)
        self._save_and_refresh()

    def _save_and_refresh(self):
        if self.storage is not None:
            self.storage.save_world(self.world)
        self._refresh()
        if self.on_changed:
            self.on_changed()
