"""[P7i] 任务日志对话框：展示任务（按状态分组）+ 接取/放弃/领奖操作。

完全代码驱动（quest_engine），不用 LLM。领奖经验走 combat_engine.gain_xp。
入口：场景页玩家区「任务」按钮 + NPC 对话（任务发布者）。

[UI 改造 2026-10-01 第二批] 暗金古卷设计系统重排：
- 待领奖=金顶线卡（最抓眼）；状态徽章（待领奖/进行中/已完成/失败）
- 结构化目标带迷你进度条（cur/cnt）；完成目标绿色对勾；来源提示保留
- 主线/支线/日常跑腿分区块头；动作按钮右对齐（行为与提示文案不变）
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QWidget, QMessageBox, QProgressBar,
)
from PySide6.QtCore import Qt

from src.services import quest_engine as qe
from src.services import combat_engine as ce
from src.ui.widgets.game_widgets import GameCard, game_badge, card_sub, banner_rule

# [P15b1] 下一环状态中文（active/completed 等态的链段提示尾注）
# [UI 禁英文 2026-10-01] available/locked 补映射——此前漏映射时徽章直接显示英文原值
_STATUS_ZH = {"active": "进行中", "completed": "待领奖", "claimed": "已完成",
              "failed": "失败", "abandoned": "已放弃",
              "available": "可接取", "locked": "未解锁"}
_STATUS_LEVEL = {"active": "info", "completed": "gold", "claimed": "success",
                 "failed": "danger", "abandoned": "warn",
                 "available": "info", "locked": "warn"}

# 链区头（chain -> (标题, 徽章色)）
_CHAIN_SECTIONS = [
    ("main", "【主线】", "#e0af68"),
    ("side", "【支线】", "#9ece6a"),
    ("errand", "【日常跑腿】（当日有效，与发布人交谈接取/交差）", "#7dcfff"),
]


class QuestLogDialog(QDialog):
    """任务日志：可接取 / 进行中 / 待领奖 / 已完成。"""

    def __init__(self, world, on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.on_changed = on_changed      # 操作后回调（save_world + refresh_state）
        self.setWindowTitle("任务日志")
        self.resize(640, 700)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)
        title = QLabel("任务日志")
        title.setObjectName("bannerName")
        outer.addWidget(title)
        outer.addWidget(banner_rule())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.content = QWidget()
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(2, 2, 2, 2)
        self.content_lay.setSpacing(10)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

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
        # 清空
        while self.content_lay.count():
            it = self.content_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        world = self.world
        groups = [
            ("completed", "待领奖（前往任务发布者处领取）"),
            ("active", "进行中"),
            ("available", "可接取（与发布者同地点时可直接点「接取任务」，否则先去找发布者）"),
            ("claimed", "已完成"),
            ("failed", "失败"),
            ("abandoned", "已放弃"),
        ]
        # [任务改造] 主线/支线/日常分区：main 置顶，每区内按 status 分组
        by_chain = {
            "main": [q for q in (world.quests or []) if getattr(q, "chain", "side") == "main"
                     and getattr(q, "status", "") != "locked"],
            "side": [q for q in (world.quests or []) if getattr(q, "chain", "side") == "side"
                     and getattr(q, "status", "") != "locked"],
            # [P42c] 日常跑腿独立分区（当日有效，过期自动作废不展示）
            "errand": [q for q in (world.quests or []) if getattr(q, "chain", "") == "errand"
                       and q.status in ("available", "active", "completed")],
        }
        any_shown = False
        for chain_key, head_text, head_color in _CHAIN_SECTIONS:
            qs_chain = by_chain[chain_key]
            if not qs_chain:
                continue
            any_shown = True
            head = QLabel(head_text)
            head.setStyleSheet(f"color:{head_color}; font-weight:bold; font-size:14px;")
            self.content_lay.addWidget(head)
            for status, label in groups:
                qs = [q for q in qs_chain if q.status == status]
                if not qs:
                    continue
                hdr = QLabel(label + f"（{len(qs)}）")
                hdr.setStyleSheet("color:#7aa2f7; font-weight:bold; margin-top:4px;")
                self.content_lay.addWidget(hdr)
                for q in qs:
                    self.content_lay.addWidget(self._quest_card(q, status))
        if not any_shown:
            empty = QLabel("暂无任务。探索世界、与 NPC 交谈可能触发任务。")
            empty.setStyleSheet("color:#9aa5ce;")
            empty.setWordWrap(True)
            self.content_lay.addWidget(empty)
        self.content_lay.addStretch()

    def _quest_card(self, q, status: str) -> QFrame:
        card = GameCard(gold=(status == "completed"))
        # 头部：任务名 + 状态徽章 + 发布者/链环徽章
        head = QWidget()
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(8)
        title = QLabel(q.title)
        title.setStyleSheet("color:#e8e8f5; font-weight:bold;")
        title.setWordWrap(True)
        hh.addWidget(title, 1)
        hh.addWidget(game_badge(_STATUS_ZH.get(status, status),
                                _STATUS_LEVEL.get(status, "info")))
        card.add(head)
        # 发布者 / 主线链环 / 目标（副文行）
        giver_name = next((n.name for n in self.world.npcs if n.id == q.giver_npc_id), "未知")
        meta_bits = [f"发布者：{giver_name}"]
        # [任务改造] 主线任务显示链进度（第 X 环）
        if getattr(q, "chain", "side") == "main":
            main_chain = [x for x in (self.world.quests or [])
                          if getattr(x, "chain", "side") == "main"]
            if q in main_chain:
                meta_bits.append(f"主线第 {main_chain.index(q) + 1} 环")
        if q.objective:
            meta_bits.append(f"目标：{q.objective}")
        card.add(card_sub("　".join(meta_bits)))
        # [P15b1] 任务链：链上任务提示下一环（已解锁/未解锁都只显标题）
        next_id = str(getattr(q, "chain_next_id", "") or "")
        if next_id:
            nxt = next((x for x in (self.world.quests or []) if x.id == next_id), None)
            if nxt is not None:
                if nxt.status == "locked":
                    tail = "（领取本环奖励后解锁）"
                elif nxt.status == "available":
                    tail = "（已解锁，可接取）"
                else:
                    tail = f"（{_STATUS_ZH.get(nxt.status, nxt.status)}）"
                card.add(card_sub(f"下一环：{nxt.title}{tail}"))
        # 结构化目标进度（文本 + 迷你进度条）
        objs = [o for o in (q.objectives or []) if isinstance(o, dict)]
        for o in objs:
            cur = int(o.get("current", 0) or 0)
            cnt = int(o.get("count", 1) or 1)
            done = cur >= cnt
            txt = qe.objective_text(o)
            row = QWidget()
            oh = QHBoxLayout(row)
            oh.setContentsMargins(0, 0, 0, 0)
            oh.setSpacing(8)
            ol = QLabel(("✓ " if done else "·  ") + txt)
            ol.setStyleSheet("color:#9ece6a;" if done else "color:#c0caf5; font-size:12px;")
            ol.setWordWrap(True)
            oh.addWidget(ol, 1)
            if cnt > 1:
                bar = QProgressBar()
                bar.setObjectName("statBar")
                bar.setProperty("tone", "green" if done else "gold")
                bar.setFixedSize(84, 14)
                bar.setRange(0, cnt)
                bar.setValue(min(cur, cnt))
                bar.setFormat(f"{cur}/{cnt}")
                oh.addWidget(bar, 0, Qt.AlignVCenter)
            card.add(row)
            # [修3 2026-08-28] gather/collect 未完成目标附「哪里有售/哪里可采」提示
            # （30 回合实测主线材料买不齐的信息断层——货架在别的区，玩家不可知）
            if not done and o.get("type") in ("gather", "collect"):
                hint = self._source_hint(str(o.get("target", "") or ""))
                if hint:
                    hl = QLabel("　↳ " + hint)
                    hl.setStyleSheet("color:#7aa2f7; font-size:11px;")
                    hl.setWordWrap(True)
                    card.add(hl)
            # [修 2026-10-02 用户拍板 C] kill/visit/talk 等目标附引擎生成的真实锚点
            # 注脚——LLM 的 desc 偶尔发明地图上不存在的地名（「通风井外」真机案），
            # 本行按引擎计数口径永远为真（kill 在哪杀都算 / visit 是真实地点名）。
            if not done:
                ahint = qe.anchor_hint(self.world, o)
                if ahint:
                    ah = QLabel("　↳ " + ahint)
                    ah.setStyleSheet("color:#7aa2f7; font-size:11px;")
                    ah.setWordWrap(True)
                    card.add(ah)
        # 奖励
        rw = q.rewards or {}
        if isinstance(rw, dict) and (rw.get("gold") or rw.get("xp") or rw.get("items")):
            parts = []
            if rw.get("gold"):
                parts.append(f"{rw['gold']} {qe._currency(self.world)}")
            if rw.get("xp"):
                parts.append(f"{rw['xp']} 经验")
            ritems = [next((i.name for i in self.world.items if i.id == iid), iid)
                      for iid in (rw.get("items") or [])]
            if ritems:
                parts.append("物品：" + "、".join(ritems))
            rw_lbl = QLabel("奖励：" + "，".join(parts))
            rw_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
            rw_lbl.setWordWrap(True)
            card.add(rw_lbl)
        # 操作按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        if status == "available":
            b = QPushButton("接取任务")
            b.setObjectName("optionBtn")
            b.clicked.connect(lambda _=False, _q=q: self._accept(_q))
            btn_row.addWidget(b)
        elif status == "active":
            b = QPushButton("放弃")
            b.setObjectName("optionBtn")
            b.clicked.connect(lambda _=False, _q=q: self._abandon(_q))
            btn_row.addWidget(b)
            # [S04/R2 2026-09-30] 交付物资：deliver_items 目标 + 背包有货 + 发布者当面
            # 才出按钮（部分交付合法——「2/3」是真的；扣物入发布者容器守恒）
            _dl = qe.deliverable_now(self.world, q)
            if _dl and qe.giver_present(self.world, q):
                db = QPushButton("交付物资")
                db.setObjectName("optionBtn")
                db.setStyleSheet("QPushButton{background:#1f3a2a; color:#9ece6a;}")
                db.setToolTip("交付：" + "；".join(
                    f"{d['item_name']}（背包 {d['have']}，还差 {d['need_left']}）" for d in _dl))
                db.clicked.connect(lambda _=False, _q=q: self._deliver(_q))
                btn_row.addWidget(db)
        elif status == "completed":
            b = QPushButton("领取奖励")
            b.setObjectName("primaryBtn")
            b.clicked.connect(lambda _=False, _q=q: self._claim(_q))
            btn_row.addWidget(b)
        elif status == "claimed" and getattr(q, "pending_items", None):
            # [B01-F10] 满包没拿走的奖励物品：补领入口（只发物品，不重发金币/经验）
            b = QPushButton("领取剩余物品")
            b.setObjectName("optionBtn")
            b.setStyleSheet("QPushButton{background:#3a321f; color:#e0af68;}")
            b.setToolTip("上次领奖时背包已满，部分物品保留在任务奖励中——清出背包后在此领取")
            b.clicked.connect(lambda _=False, _q=q: self._claim_pending(_q))
            btn_row.addWidget(b)
        card.content.addLayout(btn_row)
        return card

    def _source_hint(self, target_name: str) -> str:
        """[修3] 收集目标获取提示（薄委托 quest_engine.sourcing_hint——UI 与叙事
        上下文【任务物品风声】块单一来源）。"""
        return qe.sourcing_hint(self.world, target_name)

    def _accept(self, q):
        ok = qe.accept_quest(self.world, q, int(getattr(self.world, "tick_count", 0) or 0))
        if not ok and getattr(q, "status", "") == "available":
            # [任务改造] giver 不在场拒接 -> 提示去找发布者
            gn = qe.giver_name(self.world, q)
            loc_name = self._giver_location_name(q)
            hint = f"需要找到发布者「{gn}」才能接取此任务。" if gn else "需要找到任务发布者才能接取。"
            if loc_name:
                hint += f"\n发布者可能在：{loc_name}"
            QMessageBox.information(self, "无法接取", hint)
            return
        self._changed()

    def _abandon(self, q):
        qe.abandon_quest(q)
        self._changed()

    def _claim(self, q):
        result = qe.claim_reward(self.world, q, int(getattr(self.world, "tick_count", 0) or 0),
                                 gain_xp_fn=ce.gain_xp)
        if not result and getattr(q, "status", "") == "completed":
            # [P11c] 任务系统关闭（世界模拟设置）：任务仅作叙事提示，领奖被门控拦截
            QMessageBox.information(
                self, "任务系统已关闭",
                "本世界的结构化任务已关闭（世界模拟设置 -> 启用结构化任务），\n"
                "任务仅作叙事提示，不推进进度、不发放奖励。")
            return
        if isinstance(result, dict) and result.get("error") == "need_giver":
            # [任务改造] giver 不在场拒领 -> 提示去找发布者
            gn = qe.giver_name(self.world, q)
            loc_name = self._giver_location_name(q)
            hint = f"需要找到发布者「{gn}」才能领取奖励。" if gn else "需要找到任务发布者才能领取奖励。"
            if loc_name:
                hint += f"\n发布者可能在：{loc_name}"
            QMessageBox.information(self, "无法领奖", hint)
            return
        # [任务奖励物品 2026-09-13] 任务日志是危机悬赏（giver 空）唯一的领奖入口，
        # 原来这条路径完全不读 bag_full——奖励物品入包失败会零提示静默丢失。
        if isinstance(result, dict) and (result.get("bag_full") or result.get("already_owned")):
            bits = []
            if result.get("bag_full"):
                # [B01-F10] 物品不再被吞：滞留件保留在任务上，本条目出「领取剩余物品」入口
                bits.append("背包已满，部分物品保留在任务奖励中（本条目上可随时补领）")
            if result.get("already_owned"):
                bits.append("已持有：" + "、".join(result["already_owned"]))
            QMessageBox.information(self, "领取奖励", "；".join(bits) + "。")
        # [P15b1] 链上任务领奖解锁下一环
        fac_note = ""
        if isinstance(result, dict) and result.get("faction_power"):
            # [P45->2026-09-10] 势力任务反馈可见：领奖养势力（引擎 note 自带
            # 「实力+X 财富+Y」双增量，UI 直显整句不再 split 解析——格式解耦防再回归）。
            fac_note = f"\n{str(result['faction_power'])}。"
        if isinstance(result, dict) and result.get("unlocked_next"):
            # [任务改造] 解锁提示含下一环发布者位置引导
            next_q = next((x for x in getattr(self.world, "quests", [])
                           if x.id == getattr(q, "chain_next_id", "")), None)
            extra = ""
            if next_q is not None:
                ngn = qe.giver_name(self.world, next_q)
                nloc = self._giver_location_name(next_q)
                if ngn:
                    extra = f"\n发布者：「{ngn}」"
                    if nloc:
                        extra += f"，所在地：{nloc}"
            QMessageBox.information(
                self, "新任务解锁",
                f"任务链推进：下一环「{result['unlocked_next']}」已解锁。{extra}\n"
                f"到达发布者所在地点后，在任务日志点「接取任务」即可接取。{fac_note}")
        elif fac_note:
            QMessageBox.information(self, "势力声势渐涨",
                                    f"你完成任务相助，{fac_note.strip()}")
        self._changed()

    def _deliver(self, q):
        """[S04/R2 2026-09-30] deliver_items 目标主动交付（真实扣物入发布者容器）。"""
        result = qe.deliver_progress(self.world, q)
        if result.get("error") == "need_giver":
            gn = qe.giver_name(self.world, q)
            QMessageBox.information(
                self, "无法交付",
                f"需要找到托付人「{gn}」当面交付。" if gn else "需要找到托付人当面交付。")
            return
        if result.get("error"):
            QMessageBox.information(self, "无法交付", str(result["error"]))
            return
        msg = f"交付：{result['items']}（任务进度 {qe.progress_percent(q)}%）"
        if result.get("remaining"):
            msg += f"\n还差 {result['remaining']} 件凑齐，凑齐后再来交付。"
        else:
            msg += "\n交付目标已全部完成。"
        QMessageBox.information(self, "交付物资", msg)
        self._changed()

    def _claim_pending(self, q):
        """[B01-F10] 补领满包滞留的奖励物品（只发物品；金币/经验早已发放不重发）。"""
        result = qe.claim_pending_items(self.world, q)
        if not result:
            return
        if result.get("granted_names"):
            msg = "取回奖励物品：" + "、".join(result["granted_names"])
        else:
            msg = "背包仍然放不下。"
        if result.get("pending_names"):
            msg += "\n仍未领取（背包已满）：" + "、".join(result["pending_names"])
        else:
            msg += "\n本次奖励已全部领取完毕。"
        QMessageBox.information(self, "领取剩余物品", msg)
        self._changed()

    def _giver_location_name(self, q) -> str:
        """[任务改造] 发布者 NPC 所在地点名（找不到返回空串，供引导提示）。"""
        gid = str(getattr(q, "giver_npc_id", "") or "")
        if not gid:
            return ""
        giver = next((n for n in getattr(self.world, "npcs", []) if n.id == gid), None)
        if giver is None:
            return ""
        loc_id = str(getattr(giver, "location_id", "") or "")
        loc = next((l for l in getattr(self.world, "locations", []) if l.id == loc_id), None)
        return str(getattr(loc, "name", "") or "") if loc is not None else ""

    def _changed(self):
        if self.on_changed is not None:
            try:
                self.on_changed()
            except Exception:
                pass
        self._refresh()
