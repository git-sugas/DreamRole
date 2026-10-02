"""世界模拟标签页（第 4 个顶部标签，独立 SLG 系统）。

自带二级 QStackedWidget：
  page 0 浏览页：标题 + 搜索 + [新建世界][设置] + 世界卡墙（CardGrid，img=banner）
  page 1 详情页：banner 大图 + QTabWidget（世界观/地点/NPC/势力/物品/任务/玩家）

完全独立于单聊/群聊/会话三页，仅复用 CardGrid / make_card_pixmap / QSS 主题。
点世界卡 -> world_selected(id) 给 MainWindow 决定如何展示（v1 直接切详情页）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QSize, QThread
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QProgressDialog,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QScrollArea, QFrame,
    QMessageBox, QMenu, QListWidget, QDialog,
)

from src.config import paths
from src.models import World, reputation_title
from src.models.world_sim_preset import GenreText
from src.services import home_engine as he
from src.ui.widgets.card_grid import CardGrid, make_card_pixmap


_RARITY_COLOR = {
    "common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
    "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555",
}


class _MissingImagesWorker(QThread):
    """[遗留#5] 补缺失图后台任务：svc.regenerate_missing_images（ComfyUI 阻塞）。"""

    finished_signal = Signal(int, object)   # (补图张数, failed 标签 list)

    def __init__(self, world_sim_service, world, preset, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.world = world
        self.preset = preset
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            done, failed = self.svc.regenerate_missing_images(
                self.world, self.preset,
                cancel_check=lambda: self._cancelled)
            self.finished_signal.emit(int(done), failed or [])
        except Exception as e:  # noqa: BLE001
            from src.utils.debug import debug_log
            debug_log(lambda: f"[WorldSim] 补缺失图异常: {e}")
            self.finished_signal.emit(0, [f"异常: {e}"])


class _LocationBgRegenWorker(QThread):
    """地点背景图强制重生成 worker（danbooru + comfyui，阻塞不可取消段走 §5 口径）。"""
    finished_signal = Signal(str, str)   # (loc_id, 文件名 / "" 失败)

    def __init__(self, svc, world, loc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.loc = loc
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            fname = self.svc.regenerate_location_background(
                self.world, self.loc, cancel_check=lambda: self._cancelled)
            self.finished_signal.emit(getattr(self.loc, "id", ""), fname or "")
        except Exception as e:  # noqa: BLE001 - worker 兜底不崩 UI
            from src.utils.debug import debug_log
            debug_log(lambda: f"[WorldSim] 地点背景重生成异常: {e}")
            self.finished_signal.emit(getattr(self.loc, "id", ""), "")


class WorldDetailView(QWidget):
    """世界详情页（只读）：顶栏 + mini HUD + banner + 侧导航 + 内容栈（P5）。"""

    back_requested = Signal()
    scene_requested = Signal()   # P2：进入场景交互页
    delete_requested = Signal()  # 删除当前世界（WorldSimBrowseTab 二次确认并落盘）
    npc_detail_requested = Signal(str)  # [P6c] 查看 NPC 档案（传 npc_id）
    user_change_requested = Signal()  # [2026-08-23] 更换绑定玩家卡（数据变更由 browse 层落盘）
    location_bg_regen_requested = Signal(str)  # [2026-08-27] 强制重生成地点背景图（传 loc_id）
    missing_images_regen_requested = Signal()  # [遗留#5] 补全部缺失图（browse 层 worker）
    import_card_requested = Signal()  # [P62 角色卡进世界] NPC 区导入酒馆卡（browse 层弹窗+落库）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._world: World | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 12)
        layout.setSpacing(10)

        # 顶部：返回 + 世界名 + 元信息 + 进入场景 + 删除
        header = QHBoxLayout()
        header.setSpacing(8)
        self.back_btn = QPushButton("← 返回")
        self.back_btn.clicked.connect(self.back_requested)
        header.addWidget(self.back_btn)
        self.title_label = QLabel("")
        self.title_label.setObjectName("titleLabel")
        header.addWidget(self.title_label, 1)
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color:#7aa2f7;")
        header.addWidget(self.meta_label)
        # [遗留#5 2026-08-29] 补缺失图按钮（emit 信号，browse 层起 worker 落盘——守只读契约）
        self.img_fix_btn = QPushButton("补缺失图")
        self.img_fix_btn.setToolTip("扫描全世界的图引用（banner/背景/头像/物品图/预生成族），"
                                    "只重生成文件缺失的部分——与世界生成同管线同提示词")
        self.img_fix_btn.clicked.connect(self.missing_images_regen_requested)
        header.addWidget(self.img_fix_btn)
        # P2：进入场景按钮
        self.scene_btn = QPushButton("进入场景")
        self.scene_btn.setObjectName("primaryBtn")
        self.scene_btn.clicked.connect(self.scene_requested)
        header.addWidget(self.scene_btn)
        # [!] 删除按钮：emit delete_requested，由 WorldSimBrowseTab 二次确认并落盘。
        # 不在详情页直接调 storage，保持 WorldDetailView 只读契约（数据变更集中到 tab 层）。
        # [!] 走 dangerBtn QSS（theme.qss:46，粉红 #f7768e）与 primaryBtn 主操作区分，凸显危险操作。
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setObjectName("dangerBtn")
        self.delete_btn.clicked.connect(self.delete_requested)
        header.addWidget(self.delete_btn)
        layout.addLayout(header)

        # [P5] mini HUD 行：回合数 / 规模 / 危险度 / 战斗模式（4 块小徽章）
        self.hud_row = QWidget()
        hud_l = QHBoxLayout(self.hud_row)
        hud_l.setContentsMargins(0, 0, 0, 0)
        hud_l.setSpacing(8)
        self.hud_round = QLabel("回合 0")
        self.hud_round.setObjectName("hudLabel")
        self.hud_round.setProperty("hudLevel", "info")
        self.hud_scale = QLabel("规模 -")
        self.hud_scale.setObjectName("hudLabel")
        self.hud_scale.setProperty("hudLevel", "info")
        self.hud_danger = QLabel("危险度 -")
        self.hud_danger.setObjectName("hudLabel")
        self.hud_danger.setProperty("hudLevel", "info")
        self.hud_combat = QLabel("战斗 -")
        self.hud_combat.setObjectName("hudLabel")
        self.hud_combat.setProperty("hudLevel", "info")
        for b in (self.hud_round, self.hud_scale, self.hud_danger, self.hud_combat):
            hud_l.addWidget(b)
        hud_l.addStretch()
        layout.addWidget(self.hud_row)

        # banner 区（占位 + 玩家位置角标）
        banner_box = QFrame()
        banner_box.setObjectName("bannerPlaceholder")
        banner_box.setFixedHeight(180)
        bb_l = QVBoxLayout(banner_box)
        bb_l.setContentsMargins(0, 0, 0, 0)
        bb_l.setStretch(0, 1)
        self.banner_label = QLabel()
        self.banner_label.setAlignment(Qt.AlignCenter)
        self.banner_label.setStyleSheet("background:transparent; color:#565f89; font-size:14px;")
        self.banner_label.setText("（无 banner 图）")
        bb_l.addWidget(self.banner_label, 1)
        # 右下角玩家位置小角标
        self.banner_loc = QLabel("")
        self.banner_loc.setObjectName("bannerPlayerLoc")
        self.banner_loc.hide()
        # 用一个空白 stretch + 角标实现右下贴边：把 banner_box 内的 label 重叠不可能
        # （QLayout 不能重叠）；改用 QWidget 内手动 resizeEvent 不值得，简化：
        # 把 banner_loc 放在 banner_box 内的右下角方案用 QLabel 套在 banner_label 同一层。
        # 这里用最简做法：把 banner_loc 嵌到 banner_box 顶角靠右下用绝对定位（QWidget.pos()），更简单：
        # 直接用 QLabel 当 banner 容器里的贴角小标签 —— banner_box 内部子控件位置由 layout
        # 控制，但 QLabel 可手动 setGeometry。这里直接用重叠布局：banner_label 跨整个 box，
        # banner_loc 浮在右下。
        # 简化：banner_loc 设为 banner_box 子控件，call show + 绝对位置走 banner_box.resizeEvent。
        # 但不想动 resizeEvent，采用另外方案：直接把 banner_loc 放在主 layout 的 QHBoxLayout
        # 中叠在 banner_box 上方。先把 banner_loc 放进 banner_box 的右下角（用 QHBoxLayout
        # 嵌一个 stretch + label 结构）。
        # 但 banner_label 也占整个 box，须妥协。最简方案：banner_loc 放到 banner_box 内的
        # 外层 QHBoxLayout 套，bottom-right 走 nested layout 不可行。
        # 妥协：banner_loc 固定放在 banner_box 右下角，通过 banner_box 内部底部 HBox 包裹。
        # 实施：把 banner_box 内部切两层：上层 banner_label (stretch=1)，下层一行 QHBoxLayout
        # 含 stretch + banner_loc。两者一起填满 box。
        bb_l_inner = QHBoxLayout()
        bb_l_inner.setContentsMargins(8, 0, 8, 6)
        bb_l_inner.addStretch(1)
        bb_l_inner.addWidget(self.banner_loc)
        bb_l.addLayout(bb_l_inner)
        layout.addWidget(banner_box)

        # [P5] 中部：QSplitter 水平（左侧 200px 导航 + 右侧内容栈）
        from PySide6.QtWidgets import QSplitter
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(4)
        self.splitter.setChildrenCollapsible(False)

        # 左侧导航
        self.nav_list = QListWidget()
        self.nav_list.setObjectName("worldNav")
        self.nav_list.setFixedWidth(200)
        self.nav_list.setMinimumWidth(180)
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        # 关闭水平滚动条；由 setItemWidget 自定义项
        self.nav_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.splitter.addWidget(self.nav_list)

        # 右侧内容栈
        self.stack = QStackedWidget()
        self.splitter.addWidget(self.stack)
        # 初始比例：左 200 / 右 stretch
        self.splitter.setSizes([200, 800])
        layout.addWidget(self.splitter, 1)

    def _on_nav_changed(self, idx: int):
        """侧导航切换 -> 内容栈切到对应段。"""
        if 0 <= idx < self.stack.count():
            self.stack.setCurrentIndex(idx)

    def show_world(self, world: World, bound_user=None):
        # [2026-08-23] bound_user 由 browse 层经 storage 解析传入（详情页只读不碰存储）
        self._bound_user = bound_user
        self._world = world
        self.title_label.setText(world.name)
        tags = ", ".join(world.genre_tags) if world.genre_tags else "无"
        self.meta_label.setText(
            f"规模:{world.scale} | 魔法:{world.magic_level} | 科技:{world.tech_level} | "
            f"基调:{world.tone or '默认'} | 题材:{tags}"
        )

        # [P5] mini HUD 刷新
        self.hud_round.setText(f"回合 {world.tick_count}")
        self.hud_scale.setText(f"规模 {world.scale}")
        # 危险度：取玩家当前所在 loc.danger（玩家位置）
        cur_loc = next((l for l in world.locations if l.id == world.player.location_id), None)
        if cur_loc:
            danger = int(getattr(cur_loc, "danger", 0) or 0)
            if danger >= 7:
                self.hud_danger.setText(f"危险度 {danger}")
                self.hud_danger.setProperty("hudLevel", "danger")
            elif danger >= 4:
                self.hud_danger.setText(f"危险度 {danger}")
                self.hud_danger.setProperty("hudLevel", "warn")
            else:
                self.hud_danger.setText(f"危险度 {danger}")
                self.hud_danger.setProperty("hudLevel", "info")
        else:
            self.hud_danger.setText("危险度 -")
            self.hud_danger.setProperty("hudLevel", "info")
        for b in (self.hud_round, self.hud_scale, self.hud_danger):
            b.style().unpolish(b)
            b.style().polish(b)
        # 战斗模式（走 preset 的 combat_system）
        # 这里 我们没传 preset 到 detail view，从 world.config_overlay 读，
        # 缺省回落 narrative。
        overlay = world.config_overlay or {}
        combat = overlay.get("combat_system") or "narrative"
        self.hud_combat.setText(f"战斗 {combat}")
        self.hud_combat.setProperty("hudLevel", "info")
        self.hud_combat.style().unpolish(self.hud_combat)
        self.hud_combat.style().polish(self.hud_combat)

        # banner 图（叠加在 bannerPlaceholder 上，banner_label 透明叠加）
        if world.banner:
            pm = QPixmap(os.path.join(paths.world_images_dir(), world.banner))
            if not pm.isNull():
                self.banner_label.clear()
                self.banner_label.setText("")
                self.banner_label.setPixmap(pm.scaled(
                    self.banner_label.width() or 800, 180,
                    Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                ))
            else:
                self.banner_label.setText("（banner 图加载失败）")
        else:
            self.banner_label.setText("（无 banner 图，生成时未启用或已跳过）")

        # banner 右下角玩家位置角标
        if cur_loc:
            self.banner_loc.setText(f"当前位置: {cur_loc.name}")
            self.banner_loc.show()
        else:
            self.banner_loc.hide()

        # [P5] 清旧导航 + 重建 8 段导航项 + 8 段内容栈
        self.nav_list.clear()
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()

        # 段定义: (key, title, desc, badge, builder)
        # [S02/F01 2026-09-30] 剧情线导航（与世界观同级；badge = 活跃线数，空不显）
        _active_arcs = sum(1 for a in (world.story_arcs or [])
                           if getattr(a, "status", "") == "active")
        sections = [
            ("lore", "世界观", "世界的本质、规则与背景", "", self._build_lore_tab),
            ("arcs", "剧情线", "正在发生的故事与走向",
             str(_active_arcs) if _active_arcs else "", self._build_story_arcs_tab),
            ("locations", "地点", "可探索的位置与连通性", str(len(world.locations)), self._build_locations_tab),
            ("npcs", "NPC", "登场角色与关键人物", str(len(world.npcs)), self._build_npcs_tab),
            ("factions", "势力", "阵营实力与领土关系", str(len(world.factions)), self._build_factions_tab),
            ("items", "物品", "可获取物品与传说装备", str(len(world.items)), self._build_items_tab),
            ("recipes", "配方", "世界生成的合成配方与门控", str(len(world.recipes)), self._build_recipes_tab),
            ("buildings", "建筑", "住宅建筑与升级材料路线", str(len(world.homes)), self._build_buildings_tab),
            ("quests", "任务", "可接取的任务与进度", str(len(world.quests)), self._build_quests_tab),
            ("player", "玩家", "角色状态与属性", "", self._build_player_tab),
            ("events", "事件", "世界动态与传闻日志",
             str(len(world.event_log)) if world.event_log else "", self._build_events_tab),
        ]
        for key, title, desc, badge, builder in sections:
            # 导航项：自定义 QWidget（标题 + 描述 + 数字徽章）
            cell = QWidget()
            cell.setObjectName("worldNavCell")
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(6)
            text_col = QWidget()
            tc_l = QVBoxLayout(text_col)
            tc_l.setContentsMargins(0, 0, 0, 0)
            tc_l.setSpacing(1)
            title_l = QLabel(title)
            title_l.setObjectName("navTitle")
            desc_l = QLabel(desc)
            desc_l.setObjectName("navDesc")
            desc_l.setWordWrap(True)
            tc_l.addWidget(title_l)
            tc_l.addWidget(desc_l)
            cl.addWidget(text_col, 1)
            if badge:
                badge_l = QLabel(badge)
                badge_l.setObjectName("navBadge")
                badge_l.setAlignment(Qt.AlignCenter)
                cl.addWidget(badge_l, 0, Qt.AlignVCenter)
            # 用 QListWidgetItem 装 QWidget
            from PySide6.QtWidgets import QListWidgetItem
            item = QListWidgetItem(self.nav_list)
            # [!] QSS 的 item padding(8px*2)+margin(2px*2) 会压缩条目内容区但不扩大
            # sizeHint，须手动补进高度，否则 cell 被压缩、导航项挤在一起
            hint = cell.sizeHint()
            item.setSizeHint(QSize(hint.width(), hint.height() + 20))
            self.nav_list.addItem(item)
            self.nav_list.setItemWidget(item, cell)
            # 内容栈对应：包一层 _wrap_scroll
            self.stack.addWidget(self._wrap_scroll(builder(world)))

        # 默认选第一段（世界观）
        if self.nav_list.count() > 0:
            self.nav_list.setCurrentRow(0)

    @staticmethod
    def _wrap_scroll(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def _build_lore_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        if not world.lore:
            lay.addWidget(QLabel("（无世界观词条）"))
        for e in world.lore:
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            t = QLabel(f"【{e.key}】")
            t.setObjectName("gameCardTitle")
            bl.addWidget(t)
            c = QLabel(e.content)
            c.setWordWrap(True)
            c.setStyleSheet("color:#c0caf5;")
            bl.addWidget(c)
            lay.addWidget(box)
        lay.addStretch()
        return w

    def _build_story_arcs_tab(self, world: World) -> QWidget:
        """[S02/F01 2026-09-30] 剧情线总览（只读；未来阶段不剧透，无「刷新生成」按钮）。

        数据走 sae.arc_public_view 单一来源（纯查询，议程可复用）；详情页只读契约
        不改存档不调 LLM。
        [深改 2026-10-01] 暗金古卷重排：分态 GameCard（进行中金顶线/受挫红边线/
        终态素卡）+ 状态·原型·人物·势力徽章行 + 阶段步进条（已发生段带天数与
        结果标记，未来段只显「？」守保密口径），取代旧版单行富文本泥潭。"""
        from src.services import story_arc_engine as sae
        from src.ui.widgets.game_widgets import GameCard, game_badge, card_sub, kv_row
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        views = sae.arc_public_view(world)
        if not views:
            hint_card = GameCard(gold=True)
            hint_card.set_title("故事尚未酝酿")
            hint_card.add(card_sub(
                "这个世界还没有酝酿出故事。剧情线由世界自己生长：每隔约 5 个"
                "世界日，世界的张力（人物目标冲突、势力恩怨、近期大事）会酝酿出"
                "新的剧情线。多出门走动、与镇上的人多打交道、留意大事动向，"
                "故事自然会找上门。"))
            lay.addWidget(hint_card)
            lay.addStretch()
            return w
        # 页头概览：徽章化计数（旧版一行蓝字平铺）
        n_active = sum(1 for v in views if v["status"] == "active")
        n_done = len(views) - n_active
        sum_row = QWidget()
        sh = QHBoxLayout(sum_row)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(8)
        sh.addWidget(game_badge(f"进行中 {n_active} 条", "gold"))
        sh.addWidget(game_badge(f"已收束 {n_done} 条", "info"))
        tip = QLabel("未来阶段保密，随世界推进逐步揭晓")
        tip.setStyleSheet("color:#565f89; font-size:11px;")
        sh.addWidget(tip)
        sh.addStretch()
        lay.addWidget(sum_row)
        # 状态徽章档：进行中金 / 已成局绿 / 已受挫折红 / 已中断黄
        _state_badge = {"active": ("进行中", "gold"),
                        "succeeded": ("已成局", "success"),
                        "failed": ("已受挫", "danger"),
                        "aborted": ("已中断", "warn")}
        for v in views:
            active = v["status"] == "active"
            card = GameCard(gold=active,
                            prio=(0 if v["status"] == "failed" else None))
            card.card_title_label(f"《{v['title']}》")
            head_row = QWidget()
            hl = QHBoxLayout(head_row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(8)
            hl.addStretch()
            _lbl, _lvl = _state_badge.get(v["status"], (v["state_zh"], "info"))
            hl.addWidget(game_badge(_lbl, _lvl))
            card.add(head_row)
            card.add(card_sub(v["premise"]))
            # 原型/势力/人物/起止/玩家站边 徽章行
            badge_row = QWidget()
            bl = QHBoxLayout(badge_row)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(6)
            bl.addWidget(game_badge(v["archetype_zh"], "info"))
            if v["faction"]:
                bl.addWidget(game_badge(v["faction"], "info"))
            bl.addWidget(game_badge(f"主事 {v['mastermind']}", "warn"))
            for p in v["participants"]:
                bl.addWidget(game_badge(p, "warn"))
            bl.addWidget(game_badge(f"起始 第{v['start_day']}天", "info"))
            if v["end_day"]:
                bl.addWidget(game_badge(f"收束 第{v['end_day']}天", "info"))
            if v["player_helped"]:
                bl.addWidget(game_badge(f"助策划者 ×{v['player_helped']}", "success"))
            if v["player_opposed"]:
                bl.addWidget(game_badge(f"助对立方 ×{v['player_opposed']}", "danger"))
            bl.addStretch()
            card.add(badge_row)
            card.add(self._arc_stage_stepper(v, active))
            if v["outcome"]:
                card.add(kv_row("结局", v["outcome"]))
            if v["hook_quest_titles"]:
                hq = QLabel("相关任务：" + "、".join(v["hook_quest_titles"])
                            + "（任务日志查看）")
                hq.setWordWrap(True)
                hq.setStyleSheet("color:#e0af68; font-size:12px;")
                card.add(hq)
            lay.addWidget(card)
        lay.addStretch()
        return w

    @staticmethod
    def _arc_stage_stepper(v: dict, active: bool) -> QWidget:
        """阶段步进条（#arcStep）：已发生段=名+天数+结果标记，当前段=金字亮框，
        未来段=「？」（保密口径只给计数）。终态线无当前段，最后落定段按已发生展示。"""
        _mark = {"compromise": "妥协", "setback": "受挫", "interrupted": "中断"}
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        done = v["stage_idx"]
        names = v["done_stage_names"]
        outs = v.get("done_stage_outcomes") or []
        days = v.get("done_stage_days") or []

        def _pill(state: str, name: str, sub: str = "") -> QFrame:
            pill = QFrame()
            pill.setObjectName("arcStep")
            pill.setProperty("state", state)
            pl = QVBoxLayout(pill)
            pl.setContentsMargins(8, 4, 8, 4)
            pl.setSpacing(0)
            nm = QLabel(name)
            nm.setAlignment(Qt.AlignCenter)
            pl.addWidget(nm)
            if sub:
                sb = QLabel(sub)
                sb.setAlignment(Qt.AlignCenter)
                pl.addWidget(sb)
            return pill

        for i in range(v["stage_total"]):
            if i < done:
                sub = " · ".join(p for p in (
                    (f"第{days[i]}天" if i < len(days) and days[i] else ""),
                    _mark.get(outs[i], "") if i < len(outs) else "") if p)
                h.addWidget(_pill("done", names[i] if i < len(names) else "？", sub))
            elif i == done and active:
                h.addWidget(_pill("cur", v["stage_name"] or "（酝酿中）", "当前段"))
            elif i == done:
                h.addWidget(_pill("done", v["stage_name"] or "终段", "已落定"))
            else:
                h.addWidget(_pill("future", "？"))
        h.addStretch()
        if active and v["future_stage_count"] > 0:
            note = QLabel(f"后续 {v['future_stage_count']} 段尚未展开")
            note.setStyleSheet("color:#565f89; font-size:11px;")
            h.addWidget(note, 0, Qt.AlignVCenter)
        return row

    def _build_locations_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        if not world.locations:
            lay.addWidget(QLabel("（无地点）"))
            return w
        # [!] auto_height：详情页整页已滚动（_wrap_scroll），内嵌滚动区会撑出
        # 过高空白把下方详情文字推远（图文距离过大）
        grid = CardGrid(image_size=180, auto_height=True)
        items = [(loc.id, loc.name, loc.background, paths.world_images_dir()) for loc in world.locations]
        grid.set_items(items)
        lay.addWidget(grid)
        # 下方详情列表
        for loc in world.locations:
            fac = next((f.name for f in world.factions if f.id == loc.faction_id), "无")
            row = QHBoxLayout()
            row.setSpacing(6)
            info = QLabel(
                f"【{loc.name}】 区域:{loc.region or '未知'} | 势力:{fac} | 危险度:{loc.danger} | "
                f"NPC:{len(loc.npc_ids)} | 连通:{len(loc.connections)}\n{loc.desc}"
            )
            info.setWordWrap(True)
            info.setStyleSheet("QLabel{background:#1f2335; border-radius:6px; padding:8px; color:#c0caf5;}")
            row.addWidget(info, 1)
            btn = QPushButton("重新生成背景")
            btn.setToolTip("用「无人」场景提示词强制重生成该地点背景图（覆盖当前图，走 ComfyUI）")
            btn.clicked.connect(lambda _=False, lid=loc.id: self.location_bg_regen_requested.emit(lid))
            row.addWidget(btn, 0, Qt.AlignTop)
            lay.addLayout(row)
        lay.addStretch()
        return w

    def _build_npcs_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        # [P62 角色卡进世界] 导入人物卡：选软件已有的单聊人物卡，系统据卡面生成世界
        # NPC 设定（LLM 转写）投放为要角（与你相识，落玩家所在地）
        _imp_row = QHBoxLayout()
        _imp_btn = QPushButton("导入人物卡")
        _n_imp = len(getattr(world, "imported_card_npcs", None) or [])
        _imp_btn.setToolTip("从软件已有的单聊人物卡中选一张，系统据卡面生成世界 NPC 设定、\n"
                            "投放为要角（性格/小传来自卡面转写，默认与你相识——已导入 {}/5）".format(_n_imp))
        _imp_btn.setEnabled(_n_imp < 5)
        _imp_btn.clicked.connect(self.import_card_requested)
        _imp_row.addWidget(_imp_btn)
        _imp_row.addStretch()
        lay.addLayout(_imp_row)
        if not world.npcs:
            lay.addWidget(QLabel("（无 NPC）"))
            lay.addStretch()
            return w
        for npc in world.npcs:
            loc = next((l.name for l in world.locations if l.id == npc.location_id), "未知")
            fac = next((f.name for f in world.factions if f.id == npc.faction_id), "无")
            _is_card = npc.id in (getattr(world, "imported_card_npcs", None) or [])
            row = QHBoxLayout()
            row.setSpacing(10)
            # 头像
            av = QLabel()
            av.setFixedSize(56, 56)
            pm = make_card_pixmap(npc.name, npc.avatar, paths.world_images_dir(), 56)
            av.setPixmap(pm)
            av.setStyleSheet("border-radius:28px; border:1px solid #2a2e44;")
            row.addWidget(av)
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 6, 10, 6)
            bl.setSpacing(2)
            # [UI 深改 2026-10-01] 人物卡信息密度：大名 + 徽章行（身份/等级/所在地/势力/状态）
            # （原一行文本堆砌；📖 是 emoji 违例，换「人物卡」徽章）
            t = QLabel(npc.name)
            t.setStyleSheet("color:#e8e8f5; font-weight:bold; font-size:14px;")
            bl.addWidget(t)
            _badge_row = QWidget()
            _bh = QHBoxLayout(_badge_row)
            _bh.setContentsMargins(0, 0, 0, 0)
            _bh.setSpacing(6)
            from src.ui.widgets.game_widgets import game_badge
            _bh.addWidget(game_badge(npc.role or "平民", "info"))
            _bh.addWidget(game_badge(f"{int(getattr(npc, 'level', 1) or 1)}级", "warn"))
            _bh.addWidget(game_badge(f"在 {loc}", "warn"))
            if fac and fac != "无":
                _bh.addWidget(game_badge(fac, "info"))
            if not getattr(npc, "alive", True):
                _bh.addWidget(game_badge("已倒下", "danger"))
            elif getattr(npc, "hostile", False):
                _bh.addWidget(game_badge("敌对", "danger"))
            if getattr(npc, "is_key_npc", False):
                _bh.addWidget(game_badge("要角", "gold"))
            if _is_card:
                _bh.addWidget(game_badge("人物卡", "gold"))
            _bh.addStretch()
            bl.addWidget(_badge_row)
            if npc.personality or npc.goal:
                d = QLabel(f"性格:{npc.personality}  目标:{npc.goal}")
                d.setWordWrap(True)
                d.setStyleSheet("color:#a9b1d6; font-size:12px;")
                bl.addWidget(d)
            row.addWidget(box, 1)
            # [P6c] 查看档案按钮（emit npc_detail_requested(npc_id)，由 browse tab 开 NpcDetailDialog）
            pf = QPushButton("档案")
            pf.setToolTip("查看该 NPC 的完整档案（人际关系/爱好/装备/背包/个人记忆）")
            pf.clicked.connect(lambda _=False, nid=npc.id: self.npc_detail_requested.emit(nid))
            row.addWidget(pf)
            lay.addLayout(row)
        lay.addStretch()
        return w

    def _build_factions_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        if not world.factions:
            lay.addWidget(QLabel("（无势力）"))
            return w
        fac_by_id = {f.id: f for f in world.factions}
        for f in world.factions:
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            t = QLabel(f"【{f.name}】")
            t.setStyleSheet("color:#bb9af7; font-weight:bold;")
            bl.addWidget(t)
            d = QLabel(f"{f.desc}\n理念:{f.ideology}  首领特质:{f.leader_traits}")
            d.setWordWrap(True)
            d.setStyleSheet("color:#c0caf5;")
            bl.addWidget(d)
            # P4：实力/财富进度条
            stats_row = QHBoxLayout()
            stats_row.setSpacing(12)
            stats_row.addWidget(self._make_faction_bar("实力", f.power))
            stats_row.addWidget(self._make_faction_bar("财富", f.wealth))
            bl.addLayout(stats_row)
            # P4：领地/成员/好战度
            meta = QLabel(
                f"好战度:{f.aggressiveness} | 领地:{len(f.territory)}处 | 成员:{len(f.members)}名"
            )
            meta.setStyleSheet("color:#9aa5ce; font-size:12px;")
            bl.addWidget(meta)
            # [P15b2] 玩家在该势力的声望头衔（每 25 点一档）
            rep_v = int((getattr(world.player, "reputation", None) or {}).get(f.id, 0) or 0)
            rep_lbl = QLabel(f"你的地位：{reputation_title(rep_v)}（声望 {rep_v}）")
            rep_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
            bl.addWidget(rep_lbl)
            # P4：对外关系简表
            if f.relations:
                rel_parts = []
                for oid, val in f.relations.items():
                    of = fac_by_id.get(oid)
                    if of:
                        tag = "盟友" if val >= 30 else ("敌对" if val <= -30 else "中立")
                        rel_parts.append(f"{of.name}:{val}({tag})")
                if rel_parts:
                    rel_lbl = QLabel("关系：" + "、".join(rel_parts))
                    rel_lbl.setWordWrap(True)
                    rel_lbl.setStyleSheet("color:#7dcfff; font-size:12px;")
                    bl.addWidget(rel_lbl)
            # P4：该势力相关的近期事件（最多 2 条）
            fac_events = [e for e in world.event_log if f.id in e.factions][-2:]
            if fac_events:
                ev_lbl = QLabel("近期动态：" + " / ".join(
                    f"[回合{e.tick}]{e.title}" for e in reversed(fac_events)))
                ev_lbl.setWordWrap(True)
                ev_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
                bl.addWidget(ev_lbl)
            lay.addWidget(box)
        lay.addStretch()
        return w

    @staticmethod
    def _make_faction_bar(label: str, value: int):
        """势力数值进度条（0-100，三色渐变 + 数值文本显示 P5）。"""
        from PySide6.QtWidgets import QFrame as _F, QProgressBar, QLabel, QVBoxLayout
        wrap = _F()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(2)
        cap = QLabel(f"{label} {value}")
        cap.setStyleSheet("color:#9aa5ce; font-size:11px;")
        wl.addWidget(cap)
        bar = QProgressBar()
        bar.setObjectName("factionBar")
        bar.setRange(0, 100)
        bar.setValue(max(0, min(100, int(value))))
        bar.setTextVisible(True)
        bar.setFormat(f"{value}")
        bar.setFixedHeight(10)
        # [P5] 动态属性 barLevel 切 theme.qss 渐变档位
        if value < 30:
            bar.setProperty("barLevel", "low")
        elif value < 60:
            bar.setProperty("barLevel", "mid")
        else:
            bar.setProperty("barLevel", "high")
        wl.addWidget(bar)
        return wrap

    def _build_events_tab(self, world: World) -> QWidget:
        """P4 世界事件/传闻日志（倒序，最近在上）。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        if not world.event_log:
            lay.addWidget(QLabel("（暂无世界事件。进入场景与世界滴答推进后，离屏动态会在此记录。）"))
            lay.addStretch()
            return w
        sev_color = {
            "crisis": "#f7768e", "major": "#e0af68",
            "minor": "#9ece6a", "trivial": "#7dcfff",
        }
        cat_label = {
            "faction_war": "势力战", "economy": "经济", "npc": "NPC",
            "event": "传闻", "quest": "任务",
        }
        # 倒序（最近在上）
        for e in reversed(world.event_log):
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 6, 10, 6)
            bl.setSpacing(2)
            cat = cat_label.get(e.category, e.category)
            head = QLabel(f"[回合{e.tick}] {cat} · {e.title}")
            head.setStyleSheet(f"color:{sev_color.get(e.severity, '#c0caf5')}; font-weight:bold;")
            bl.addWidget(head)
            body = QLabel(e.desc)
            body.setWordWrap(True)
            body.setStyleSheet("color:#c0caf5;")
            bl.addWidget(body)
            # [P11d] 关键事件插图（severity>=major 的事件在世界滴答时配图）
            img = getattr(e, "image", "") or ""
            if img:
                img_path = os.path.join(paths.world_images_dir(), img)
                pm = QPixmap(img_path)
                if not pm.isNull():
                    row = QHBoxLayout()
                    thumb = QLabel()
                    thumb.setPixmap(pm.scaledToWidth(280, Qt.SmoothTransformation))
                    thumb.setStyleSheet("background:transparent; border-radius:4px;")
                    thumb.setCursor(Qt.PointingHandCursor)
                    thumb.setToolTip("点击查看大图")
                    thumb.mousePressEvent = lambda _ev, p=img_path: self._preview_image(p)
                    row.addWidget(thumb)
                    row.addStretch()
                    bl.addLayout(row)
            lay.addWidget(box)
        lay.addStretch()
        return w

    def _preview_image(self, path: str):
        """[P11d] 事件插图点击预览大图（独立窗口，口径仿头像预览）。"""
        pm = QPixmap(path)
        if pm.isNull():
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("事件插图")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(8, 8, 8, 8)
        lbl = QLabel()
        lbl.setPixmap(pm.scaled(1000, 700, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(lbl)
        dlg.exec()

    def _item_icon_label(self, it) -> QLabel:
        """物品图 or 首字头像（无图时用名字首字 + 稀有度配色）。"""
        av = QLabel()
        av.setFixedSize(44, 44)
        av.setAlignment(Qt.AlignCenter)
        icon = getattr(it, "icon", "") or ""
        if icon:
            p = os.path.join(paths.world_images_dir(), icon)
            pix = QPixmap(p)
            if not pix.isNull():
                av.setPixmap(pix.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return av
        ch = (getattr(it, "name", "") or "物")[0]
        color = _RARITY_COLOR.get(getattr(it, "rarity", "common"), "#c0caf5")
        av.setText(ch)
        av.setStyleSheet(f"QLabel{{background:#2a2f3a; color:{color}; border-radius:8px;"
                         f" font-size:20px; font-weight:bold;}}")
        return av

    def _build_items_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        if not world.items:
            lay.addWidget(QLabel("（无物品）"))
            return w
        for it in world.items:
            color = _RARITY_COLOR.get(it.rarity, "#c0caf5")
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QHBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(10)
            bl.addWidget(self._item_icon_label(it), 0, Qt.AlignVCenter)
            info = QVBoxLayout()
            info.setSpacing(4)
            t = QLabel(f"【{it.name}】 {it.type} | {it.rarity}")
            t.setStyleSheet(f"color:{color}; font-weight:bold;")
            info.addWidget(t)
            d = QLabel(f"{it.desc}" + (f"\n效果:{it.effects}" if it.effects else ""))
            d.setWordWrap(True)
            d.setStyleSheet("color:#c0caf5;")
            info.addWidget(d)
            bl.addLayout(info, 1)
            lay.addWidget(box)
        lay.addStretch()
        return w

    def _build_recipes_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        if not world.recipes:
            lay.addWidget(QLabel("（无配方）"))
            return w
        item_by_id = {it.id: it for it in world.items}
        for r in world.recipes:
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            out = item_by_id.get(getattr(r, "output_item_id", ""))
            out_name = out.name if out is not None else (getattr(r, "output_item_id", "") or "?")
            color = _RARITY_COLOR.get(getattr(out, "rarity", "common"), "#c0caf5") if out is not None else "#c0caf5"
            t = QLabel(f"【{getattr(r, 'name', '配方') or out_name}】")
            t.setStyleSheet(f"color:{color}; font-weight:bold;")
            bl.addWidget(t)
            mats = "、".join(
                (item_by_id[iid].name if iid in item_by_id else str(iid))
                for iid in (getattr(r, "inputs", None) or []))
            lines = [f"材料：{mats or '无'}",
                     f"产出：{out_name} x{max(1, int(getattr(r, 'output_qty', 1) or 1))}"]
            rb = getattr(r, "required_building", "") or ""
            if rb:
                ml = max(1, int(getattr(r, "min_building_level", 0) or 0))
                lines.append(f"门控：{he.building_kind_name(world, rb)} {ml} 级")
            else:
                lines.append("门控：通用（无建筑要求）")
            if getattr(r, "difficulty", 0):
                lines.append(f"难度：{getattr(r, 'difficulty', 0)}")
            d = QLabel("\n".join(lines))
            d.setWordWrap(True)
            d.setStyleSheet("color:#c0caf5;")
            bl.addWidget(d)
            lay.addWidget(box)
        lay.addStretch()
        return w

    def _build_buildings_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        gt = GenreText(getattr(world, "config_overlay", None) or {})
        # 已建等级：{kind: level}（多宅取最高）
        built = {}
        for home in (world.homes or []):
            for b in (home.buildings or []):
                if isinstance(b, dict) and b.get("kind"):
                    built[b["kind"]] = max(built.get(b["kind"], 0), int(b.get("level", 1) or 1))
        for kind in ("forge", "alchemy", "refine", "study", "garden", "warehouse"):
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            lvl = built.get(kind)
            lvl_txt = f"（当前 {lvl} 级）" if lvl else "（未建造）"
            t = QLabel(f"【{he.building_kind_name(world, kind)}】{lvl_txt}")
            t.setObjectName("gameCardTitle")
            bl.addWidget(t)
            for lv in range(1, 6):
                spec = he._spec_for(world, kind, lv)
                if not spec:
                    line = f"{lv}级：旧口径耗材（锻造材料×1 + 制造材料×1）"
                else:
                    parts = []
                    for e in spec:
                        if e.get("item_id"):
                            parts.append(f"「{e.get('name', e['item_id'])}」")
                        else:
                            parts.append(f"{e.get('count', 1)}件{gt.rarity(e.get('rarity', 'common'))}")
                    line = f"{lv}级：{'、'.join(parts)}"
                d = QLabel(line)
                d.setWordWrap(True)
                d.setStyleSheet("color:#c0caf5;")
                bl.addWidget(d)
            lay.addWidget(box)
        lay.addStretch()
        return w

    def _build_quests_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        # [P15b1] locked = 任务链未解锁段，不展示（与任务日志口径一致，防剧透）
        quests = [q for q in world.quests if getattr(q, "status", "") != "locked"]
        if not quests:
            lay.addWidget(QLabel("（无任务）"))
            return w
        for q in quests:
            giver = next((n.name for n in world.npcs if n.id == q.giver_npc_id), "未知")
            box = QFrame()
            box.setObjectName("gameCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            t = QLabel(f"【{q.title}】 委托人:{giver}")
            t.setStyleSheet("color:#e0af68; font-weight:bold;")
            bl.addWidget(t)
            d = QLabel(f"目标:{q.objective}" + (f"\n奖励:{q.reward_text}" if q.reward_text else ""))
            d.setWordWrap(True)
            d.setStyleSheet("color:#c0caf5;")
            bl.addWidget(d)
            lay.addWidget(box)
        lay.addStretch()
        return w

    def _build_player_tab(self, world: World) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        p = world.player
        loc = next((l.name for l in world.locations if l.id == p.location_id), "未知")
        box = QFrame()
        box.setObjectName("gameCard")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(4)
        bl.addWidget(QLabel(f"当前位置:{loc}"))
        bl.addWidget(QLabel(f"职业:{p.class_name or '未定'}"))
        bl.addWidget(QLabel(f"身世:{p.background or '未定'}"))
        lay.addWidget(box)

        # [2026-08-23 用户卡绑定] 玩家卡区：绑定的用户卡（姓名/人设注入叙事与 NPC 记忆）。
        # 换绑按钮 emit user_change_requested，落盘在 browse 层（详情页只读契约）。
        ubox = QFrame()
        ubox.setObjectName("gameCard")
        ul = QHBoxLayout(ubox)
        ul.setContentsMargins(10, 8, 10, 8)
        ul.setSpacing(10)
        u = getattr(self, "_bound_user", None)
        if u is not None:
            av = QLabel()
            av.setPixmap(make_card_pixmap(u.name or "?", u.avatar, paths.avatars_dir(), 56))
            ul.addWidget(av)
            info = QVBoxLayout()
            info.setSpacing(2)
            name_lbl = QLabel(f"玩家卡:{u.name or '未命名'}")
            name_lbl.setStyleSheet("font-weight:bold;")
            info.addWidget(name_lbl)
            desc = (getattr(u, "description", "") or "").strip()
            if desc:
                desc_lbl = QLabel(desc[:80] + ("…" if len(desc) > 80 else ""))
                desc_lbl.setStyleSheet("color:#9aa5ce; font-size:12px;")
                desc_lbl.setWordWrap(True)
                info.addWidget(desc_lbl)
            ul.addLayout(info, 1)
        else:
            hint = QLabel("玩家卡:未绑定（NPC 只能以「那位少侠」之类泛称指代玩家）")
            hint.setStyleSheet("color:#9aa5ce;")
            hint.setWordWrap(True)
            ul.addWidget(hint, 1)
        change_btn = QPushButton("更换…" if u is not None else "绑定…")
        change_btn.setToolTip("选择一张用户卡作为玩家化身：姓名/人设将注入场景叙事与 NPC 记忆，"
                              "NPC 会按名字认识玩家；也可解除绑定。")
        change_btn.clicked.connect(self.user_change_requested)
        ul.addWidget(change_btn)
        lay.addWidget(ubox)

        lay.addStretch()
        return w


class WorldSimBrowseTab(QWidget):
    """世界模拟浏览页：内部 QStackedWidget（浏览 / 详情 / 场景）。"""

    new_world_requested = Signal()
    settings_requested = Signal()
    world_selected = Signal(str)     # 点世界卡（v1 内部直接切详情，外部可监听做后续扩展）
    # [修 2026-09-05 用户指示] 新世界保存后的后台补图完成通知（world_id, 补图张数, 失败数）
    # ——main_window 接到后在状态栏非阻塞提示（世界立即可玩，图异步到位）。
    # [修 2026-09-06] 信号原误挂 WorldDetailView（main_window 连的是本类实例 -> 启动即崩）
    image_backfill_done = Signal(str, int, int)

    def __init__(self, world_sim_service=None, storage=None, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.storage = storage
        self._all_items: list[tuple[str, str, str, str]] = []
        self._scene_view = None   # 惰性创建（P2 场景交互页）
        # [修 2026-09-10] 补图/地点背景 worker 单槽 + 已删世界 id 集（防 worker 收尾
        # touch+save_world 把已删世界写回复活）+ 退役 worker 保活列表（parent=None
        # 时丢掉唯一引用会在运行中被 GC -> QThread Destroyed while running 崩溃）。
        self._img_fix_worker = None
        self._bg_regen_worker = None
        self._deleted_world_ids: set[str] = set()
        self._orphan_workers: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()

        # ---- page 0: 浏览 ----
        browse = QWidget()
        bl = QVBoxLayout(browse)
        bl.setContentsMargins(28, 20, 28, 16)
        bl.setSpacing(16)
        header = QHBoxLayout()
        header.setSpacing(12)
        title = QLabel("世界模拟 · 生成或选择一个世界")
        title.setObjectName("titleLabel")
        header.addWidget(title)
        header.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索…")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self._apply_filter)
        header.addWidget(self.search)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.clicked.connect(self.settings_requested)
        header.addWidget(self.settings_btn)
        self.new_btn = QPushButton("+ 新建世界")
        self.new_btn.setObjectName("primaryBtn")
        self.new_btn.clicked.connect(self.new_world_requested)
        header.addWidget(self.new_btn)
        bl.addLayout(header)

        self.grid = CardGrid(image_size=210)
        self.grid.activated.connect(self._on_card)
        # [!] 卡墙右键菜单：聚合自 CardCell.context_menu_requested。
        self.grid.context_menu_requested.connect(self._on_card_context_menu)
        bl.addWidget(self.grid, 1)

        self.stack.addWidget(browse)

        # ---- page 1: 详情 ----
        self.detail = WorldDetailView()
        self.detail.back_requested.connect(self.show_browse)
        # P2：详情页「进入场景」-> 切场景页
        self.detail.scene_requested.connect(self._on_scene_requested)
        # 详情页「删除」-> 弹确认 + 落盘（共用 _delete_world）。
        self.detail.delete_requested.connect(self._on_detail_delete)
        # [P6c] 详情页 NPC「档案」-> 开 NpcDetailDialog（只读档案）
        self.detail.npc_detail_requested.connect(self._on_npc_detail_requested)
        # [P62 角色卡进世界] 详情页 NPC 区导入酒馆卡（只读契约：detail 只 emit 信号）
        self.detail.import_card_requested.connect(self._on_import_card)
        # [2026-08-23 用户卡绑定] 详情页「更换玩家卡」-> 选择对话框 + 落盘（详情页只读）
        self.detail.user_change_requested.connect(self._on_user_change)
        # [2026-08-27 地点背景强制重生成] 详情页地点行「重新生成背景」-> 起 worker + 落盘刷新
        self.detail.location_bg_regen_requested.connect(self._on_location_bg_regen)
        self.detail.missing_images_regen_requested.connect(self._on_missing_images_regen)
        self.stack.addWidget(self.detail)

        # ---- page 2: 场景交互（惰性，show_scene 时创建）----

        layout.addWidget(self.stack, 1)

    def populate(self, worlds):
        self._all_items = [(w.id, w.name, w.banner, paths.world_images_dir()) for w in worlds]
        self._apply_filter()

    def _apply_filter(self):
        kw = self.search.text().strip().lower()
        items = [it for it in self._all_items if not kw or kw in (it[1] or "").lower()]
        self.grid.set_items(items)

    def _on_card(self, eid: str):
        self.world_selected.emit(eid)

    def _on_card_context_menu(self, eid: str, pos):
        """卡墙右键弹菜单：删除世界（共用 _delete_world 走 storage.delete_world）。"""
        # 防御性：search 框过滤后 eid 仍在 _all_items 里（卡墙只显示已过滤项），
        # 但用户可能在弹菜单前改了 search 文案——以当前 _all_items 为准查找 name。
        name = next((it[1] for it in self._all_items if it[0] == eid), "")
        menu = QMenu(self)
        act_del = menu.addAction("删除世界")
        chosen = menu.exec(pos)
        if chosen is act_del:
            self._delete_world(eid, world_name=name)

    def _on_detail_delete(self):
        """详情页「删除」按钮：取当前 _world，调 _delete_world 并切回浏览页。"""
        if self.detail._world is None:
            return
        self._delete_world(self.detail._world.id, world_name=self.detail._world.name, from_detail=True)

    def _delete_world(self, world_id: str, *, world_name: str = "", from_detail: bool = False):
        """统一删除入口：二次确认 -> storage.delete_world -> 重刷卡墙（详情页删除后回浏览）。
        storage.delete_world 已级联清理场景日志/NPC 记忆/私聊/引用图片
        （P12k：图片文件名带时间戳+uuid 唯一不复用，引用图随世界删除）。
        """
        if self.storage is None:
            QMessageBox.warning(self, "提示", "存储未初始化，无法删除。")
            return
        display = world_name or world_id
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定删除世界「{display}」？\n将级联清理场景日志。该操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # [修 2026-09-10] 删世界前先退役在跑的补图/地点背景 worker：它们的 _on_done 会
        # world.touch()+storage.save_world(world)——世界 JSON 刚被级联删掉，内存副本写回
        # = 世界复活，但场景日志/记忆/图片已删（半身世界）+ 详情页还会把它重新显示出来。
        # 标记已删 id 让 _on_done 兜底 return（双保险），退役不等待（comfyui 阻塞不可取消）。
        self._deleted_world_ids.add(str(world_id))
        self._retire_world_worker("_img_fix_worker")
        self._retire_world_worker("_bg_regen_worker")
        # [P6c] 删世界前清 NPC 个人记忆的 ChromaDB collection（JSON 目录由 storage.delete_world 级联删）。
        # best-effort：清库失败不阻断删世界。
        if self.svc is not None:
            try:
                w = next((x for x in self.storage.load_all_worlds() if x.id == world_id), None)
                if w is not None:
                    self.svc.npc_memory().clear_world_memory(w)
            except Exception:
                pass
        self.storage.delete_world(world_id)
        self.populate(self.storage.load_all_worlds())
        if from_detail:
            # 详情页删除后：清空 _world 野指针 + 弹出到卡墙，避免内存状态与磁盘脱节。
            self.detail._world = None
            self.show_browse()

    def _resolve_bound_user(self, world: World):
        """[2026-08-23] 解析世界绑定的玩家卡（未绑/已删返回 None；best-effort 不抛）。"""
        uid = str(getattr(world, "user_id", "") or "")
        if not uid or self.storage is None:
            return None
        try:
            return self.storage.load_user(uid)
        except Exception:
            return None

    def show_detail(self, world: World):
        self.detail.show_world(world, self._resolve_bound_user(world))
        self.stack.setCurrentIndex(1)

    def show_browse(self):
        self.stack.setCurrentIndex(0)

    def _on_user_change(self):
        """[2026-08-23 用户卡绑定] 详情页「更换玩家卡」：弹选择对话框 -> 写 world.user_id
        + save_world + 重显详情页（选中态即时可见）。"""
        world = self.detail._world
        if world is None or self.storage is None:
            return
        try:
            users = self.storage.load_all_users()
        except Exception:
            users = []
        dlg = _UserSelectDialog(users, str(getattr(world, "user_id", "") or ""), self)
        if dlg.exec() != QDialog.Accepted:
            return
        new_uid = dlg.get_selected_id()   # 空串 = 解除绑定
        if new_uid == (getattr(world, "user_id", "") or ""):
            return
        world.user_id = new_uid
        try:
            world.touch()
            self.storage.save_world(world)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"玩家卡绑定未能保存：{e}")
            return
        self.detail.show_world(world, self._resolve_bound_user(world))

    # ============ P2 场景交互页 ============
    def _on_scene_requested(self):
        """详情页「进入场景」按钮：用详情页当前世界切场景页。"""
        if self.detail._world is None:
            return
        self.show_scene(self.detail._world)

    def _on_import_card(self):
        """[P62 角色卡进世界] 从软件已有的单聊人物卡中选一张 -> 系统据卡面生成
        世界模拟 NPC 设定（LLM 转写，失败回退卡面直映射）-> 要角 NPC 落世。"""
        from PySide6.QtWidgets import QInputDialog
        if self.detail._world is None or self.svc is None or self.storage is None:
            return
        world = self.detail._world
        try:
            chars = self.storage.load_all_characters()
        except Exception:
            chars = []
        if not chars:
            QMessageBox.information(
                self, "导入人物卡", "还没有单聊人物卡——先在单聊里创建或导入酒馆卡。")
            return
        names = [c.name or "未命名" for c in chars]
        picked, ok = QInputDialog.getItem(
            self, "导入人物卡",
            f"选择要投放到「{world.name}」的单聊人物卡：\n"
            "（系统将据卡面生成世界 NPC 设定，投放为要角、与你相识）",
            names, 0, False)
        if not ok:
            return
        char = chars[names.index(picked)]
        ok, msg, _npc = self.svc.import_card_npc(world, char,
                                                 source_name=getattr(char, "name", ""))
        if not ok:
            QMessageBox.warning(self, "导入失败", msg)
            return
        self.storage.save_world(world)
        self.detail.show_world(world)
        QMessageBox.information(self, "导入成功", msg)

    def _on_npc_detail_requested(self, npc_id: str):
        """[P6c] 详情页 NPC「档案」-> 开 NpcDetailDialog（只读档案 + 记忆默认隐藏）。"""
        world = self.detail._world
        if world is None:
            return
        npc = next((n for n in world.npcs if n.id == npc_id), None)
        if npc is None:
            return
        from src.ui.dialogs.npc_detail_dialog import NpcDetailDialog
        dlg = NpcDetailDialog(world, npc, world_sim_service=self.svc, parent=self)
        dlg.exec()

    def _on_missing_images_regen(self):
        """[遗留#5] 补缺失图：worker 跑 regenerate_missing_images -> 落盘 + 刷新 + 结果反馈。"""
        world = self.detail._world
        if world is None or self.svc is None or self.storage is None:
            return
        if getattr(self, "_img_fix_worker", None) is not None:
            QMessageBox.information(self, "生成中", "已有补图任务在跑，请稍候。")
            return
        preset = self.storage.load_world_sim_preset()
        worker = _MissingImagesWorker(self.svc, world, preset, parent=None)
        self._img_fix_worker = worker
        # [审查修复] 进度对话框 + 可取消（原静默不可取消；ComfyUI 补图可达分钟级）
        prog = QProgressDialog("正在补缺失图……（ComfyUI 出图，可取消）", "取消", 0, 0, self)
        prog.setWindowTitle("补缺失图")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.show()
        prog.canceled.connect(worker.cancel)

        def _on_done(done: int, failed):
            self._img_fix_worker = None
            prog.cancel()
            if str(world.id) in self._deleted_world_ids:
                return          # [修 2026-09-10] 世界已删：不得 save_world 复活
            try:
                world.touch()
                self.storage.save_world(world)
            except Exception as e:
                QMessageBox.warning(self, "保存失败", f"补图结果保存失败：{e}")
            self.detail.show_world(world, self._resolve_bound_user(world))
            if done <= 0 and not failed:
                QMessageBox.information(self, "无缺失", "扫描完毕：所有图引用齐全，无需补图。")
            elif failed:
                QMessageBox.warning(
                    self, "补图完成（部分失败）",
                    f"补上 {done} 张；{len(failed)} 张失败："
                    + "\n".join(str(x) for x in failed[:12]))
            else:
                QMessageBox.information(self, "补图完成", f"补上 {done} 张缺失图。")

        worker.finished_signal.connect(_on_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def start_auto_backfill(self, world):
        """[修 2026-09-05 用户指示] 新世界保存后的静默后台补图（复用 _MissingImagesWorker
        + regenerate_missing_images 只喂缺失文件任务；文件名建世界时已定，后台生成零
        改动落盘）。与手动「补缺失图」共用 _img_fix_worker 单槽防并发；无进度弹窗
        （世界已可玩，弹窗反而挡操作），完成后 touch+save + 刷新详情（若仍在本世界）
        + 发 image_backfill_done 供状态栏提示。
        """
        if world is None or self.svc is None or self.storage is None:
            return
        if getattr(self, "_img_fix_worker", None) is not None:
            return                      # 已有补图任务在跑（手动或前次自动），静默让位
        preset = self.storage.load_world_sim_preset()
        if not preset.image_enabled:
            return
        worker = _MissingImagesWorker(self.svc, world, preset, parent=None)
        self._img_fix_worker = worker
        wid = str(world.id)

        def _on_done(done: int, failed):
            self._img_fix_worker = None
            if str(world.id) in self._deleted_world_ids:
                return          # [修 2026-09-10] 世界已删：不得 save_world 复活
            try:
                world.touch()
                self.storage.save_world(world)
            except Exception:
                pass                    # 补图落盘 best-effort（文件已在，下轮详情页可再保存）
            # 仍在本世界才刷新详情（玩家可能已切走）
            cur = getattr(self.detail, "_world", None)
            if cur is not None and str(getattr(cur, "id", "")) == wid:
                self.detail.show_world(world, self._resolve_bound_user(world))
            self.image_backfill_done.emit(wid, int(done or 0), len(failed or []))

        worker.finished_signal.connect(_on_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_location_bg_regen(self, loc_id: str):
        """[2026-08-27] 地点背景强制重生成：起 worker（comfyui 阻塞）-> 成功落盘 + 刷新详情。"""
        world = self.detail._world
        if world is None or self.svc is None or self.storage is None:
            return
        loc = next((l for l in world.locations if l.id == loc_id), None)
        if loc is None:
            return
        if getattr(self, "_bg_regen_worker", None) is not None:
            QMessageBox.information(self, "生成中", "已有地点背景正在生成，请稍候。")
            return
        worker = _LocationBgRegenWorker(self.svc, world, loc, parent=None)
        self._bg_regen_worker = worker

        def _on_done(_lid, fname):
            self._bg_regen_worker = None
            if str(world.id) in self._deleted_world_ids:
                return          # [修 2026-09-10] 世界已删：不得 save_world 复活
            if fname:
                try:
                    world.touch()
                    self.storage.save_world(world)
                except Exception as e:
                    QMessageBox.warning(self, "保存失败", f"地点背景保存失败：{e}")
                self.detail.show_world(world, self._resolve_bound_user(world))
            else:
                QMessageBox.warning(self, "生成失败", "该地点背景图重新生成失败（ComfyUI 未启用或出图失败）。")

        worker.finished_signal.connect(_on_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def show_scene(self, world: World):
        """切到场景交互页（page 2），惰性创建 WorldSceneView。"""
        if self.svc is None or self.storage is None:
            QMessageBox.warning(self, "提示", "世界模拟服务未初始化，无法进入场景。")
            return
        if self._scene_view is None:
            from src.ui.tabs.world_scene_tab import WorldSceneView
            self._scene_view = WorldSceneView(self.svc, self.storage, self)
            self._scene_view.back_requested.connect(self._on_scene_back)
            self._scene_view.permadeath.connect(self._on_permadeath)   # [P7k8] 永久死亡回首页
            self.stack.addWidget(self._scene_view)   # page 2
        self._scene_view.load_world(world)
        self.stack.setCurrentIndex(2)

    def _on_scene_back(self):
        """场景页返回：重载世界（可能移动过）并回详情页。"""
        if self._scene_view is not None:
            if self._scene_view.cleanup() is False:
                return  # 线程尚未停止时不可从旧存档重载世界
        if self.detail._world is not None:
            # 重载最新世界状态（移动/回合已变）
            w = self.storage.load_world(self.detail._world.id)
            if w is not None:
                self.detail.show_world(w, self._resolve_bound_user(w))
        self.stack.setCurrentIndex(1)

    def _on_permadeath(self, world_id: str):
        """[P7k8] 永久死亡：世界已被 scene_tab 删除，清理场景页 + 回浏览页重刷卡墙。"""
        if self._scene_view is not None:
            self._scene_view.cleanup()
        # 详情页持有的 world 已被删，清空避免野指针
        if getattr(self.detail, "_world", None) is not None and self.detail._world.id == world_id:
            self.detail._world = None
        self.show_browse()

    def cleanup_scene(self):
        """外部切页前调用：停止场景 worker（守 §15）。"""
        if self._scene_view is not None:
            return self._scene_view.cleanup()
        return True

    # ---- [修 2026-09-10] 世界补图/地点背景 worker 生命周期 ----
    @staticmethod
    def _worker_running(w) -> bool:
        """worker 是否仍在跑（C++ 对象已被 deleteLater 销毁时安全返回 False）。"""
        if w is None:
            return False
        try:
            from shiboken6 import isValid
            if not isValid(w):
                return False
        except Exception:
            pass        # shiboken6 缺失/非 QObject：退回 isRunning 探测
        try:
            return bool(w.isRunning())
        except (RuntimeError, AttributeError):
            return False

    def _retire_world_worker(self, attr: str):
        """世界已删：退役该槽的世界 worker（断开完成槽 + cancel，不等待）。

        [!] 断 finished_signal 是防 _on_done 跑 touch+save_world 把已删世界写回复活；
        [!] 不等待是因为 comfyui.generate 阻塞不可取消（等待会卡 UI 分钟级）；
        [!] 引用移入 _orphan_workers 保活——worker parent=None，丢掉唯一引用会在运行中
        被 GC（QThread: Destroyed while still running 崩溃）。
        """
        w = getattr(self, attr, None)
        self.__dict__[attr] = None
        # 顺手修剪已完成/已销毁的孤儿（防长会话反复删世界时列表无限增长）
        self._orphan_workers = [x for x in self._orphan_workers if self._worker_running(x)]
        if w is None:
            return
        try:
            w.finished_signal.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            w.cancel()
        except Exception:
            pass
        if self._worker_running(w):
            self._orphan_workers.append(w)

    def _wait_worker(self, w):
        """cancel + wait（守 §15：先 disconnect 防已销毁接收者的槽崩溃）。"""
        if not self._worker_running(w):
            return
        try:
            w.finished_signal.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            w.cancel()
        except Exception:
            pass
        try:
            w.wait(120000)      # 与 §15 图片 worker 同档（comfyui 阻塞不可取消）
        except RuntimeError:
            pass

    def cleanup_workers(self):
        """[修 2026-09-10] 关窗前清理世界补图/地点背景 worker（守 §15 通用契约）。

        main_window.closeEvent 原只清聊天/TTS/图片与场景 worker；这两个世界 worker 以
        parent=None 创建、只被本 tab 持引用，关窗时若仍在跑会随对象销毁被 GC -> 崩溃。
        """
        for attr in ("_img_fix_worker", "_bg_regen_worker"):
            w = getattr(self, attr, None)
            self.__dict__[attr] = None
            if w is not None:
                self._wait_worker(w)
        for w in list(self._orphan_workers):
            self._wait_worker(w)
        self._orphan_workers = []


class _UserSelectDialog(QDialog):
    """[2026-08-23 用户卡绑定] 玩家卡选择对话框（单选可空）：
    选中一张用户卡作玩家化身；点「解除绑定」或确定时未选 = 返回空串。"""

    def __init__(self, users, current_uid: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择玩家卡")
        self.resize(440, 360)
        self._sel = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        head = QLabel("选择一张用户卡作为玩家化身")
        head.setObjectName("titleLabel")
        lay.addWidget(head)
        hint = QLabel("绑定后玩家姓名/人设会注入场景叙事与 NPC 记忆，NPC 将按名字认识玩家。"
                      "不选 = 解除绑定（NPC 以泛称指代玩家）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7dcfff; font-size:12px;")
        lay.addWidget(hint)

        from src.ui.widgets.avatar_grid_selector import AvatarGridSelector
        self.grid = AvatarGridSelector(multi=False, columns=4, avatar_size=64)
        self.grid.set_items([(u.id, u.name or "未命名用户", u.avatar) for u in (users or [])])
        if current_uid:
            self.grid.select_exactly(current_uid, emit=False)
            self._sel = current_uid
        self.grid.current_id_changed.connect(self._on_sel)
        lay.addWidget(self.grid, 1)

        if not users:
            empty = QLabel("（暂无用户卡：可先到「会话 -> 用户管理」新建）")
            empty.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(empty)

        row = QHBoxLayout()
        row.addStretch()
        unbind_btn = QPushButton("解除绑定")
        unbind_btn.setToolTip("清除本世界的玩家卡绑定。")
        unbind_btn.clicked.connect(self._on_unbind)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.clicked.connect(self.accept)
        for b in (unbind_btn, cancel_btn, ok_btn):
            row.addWidget(b)
        lay.addLayout(row)

    def _on_sel(self, uid: str):
        self._sel = uid or ""

    def _on_unbind(self):
        self._sel = ""
        self.grid.set_selected_ids([])
        self.accept()

    def get_selected_id(self) -> str:
        return self._sel
