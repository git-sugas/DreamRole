"""[P25a] 秘境管理对话框（层图/进度/进入/深入/离开/封印倒计时）。

进入/房间移动/互动提交预设意图，由场景回合统一验证、结算、落盘；离开调
svc.exit_dungeon -> storage.save_world -> changed.emit；「深入探索」发 explore_requested
信号由场景页起叙事回合（preset_intent move 深入 -> apply_intent 秘境特判 ->
dungeon_engine.advance），对话框随即关闭防与回合并发改状态。
无 LLM 调用故无 worker。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QWidget,
    QPushButton, QProgressBar, QComboBox,
)
from PySide6.QtCore import Signal, Qt

from src.models.world import Dungeon
from src.services import dungeon_engine as dge

_ROOM_MARK = {"done": "✓", "pending": "✦", "undo": "·"}
_ROOM_ZH = {"combat": "战斗", "check": "机关", "treasure": "宝藏", "trap": "陷阱",
            "rest": "休整", "boss": "首领"}

# [美化] 房型配色（深色底可读，呼应 theme.qss 品级色系）
_ROOM_FG = {"combat": "#f7768e", "check": "#7aa2f7", "treasure": "#e0af68",
            "trap": "#ff9e64", "rest": "#9ece6a", "boss": "#bb9af7"}


def _danger_color(danger: int) -> str:
    try:
        d = int(danger)
    except Exception:
        d = 1
    if d >= 9:
        return "#f7768e"
    if d >= 7:
        return "#ff9e64"
    if d >= 4:
        return "#e0af68"
    return "#9ece6a"


def _danger_pips(danger: int) -> str:
    try:
        d = max(1, min(10, int(danger)))
    except Exception:
        d = 1
    full = (d + 1) // 2  # 10 档压成 5 格：1-2=1格 … 9-10=5格
    return "●" * full + "○" * (5 - full)


class DungeonDialog(QDialog):
    """[P25a] 秘境页：入口（进入）/ 内部（深入/离开）/ 封印（倒计时）三态。"""

    changed = Signal()            # 进入/离开后发（场景页刷新；落盘已在本对话框做）
    explore_requested = Signal(str)  # 「深入探索」发（秘境名），场景页起 move=深入 叙事回合
    interaction_requested = Signal(dict)  # 工具/绕行均提交预设意图，窗口不直接扣物或结算
    # 进入由预设移动回合结算；离开仍先改状态再发 custom 叙事回合，均耗一个回合。
    enter_requested = Signal(dict)
    exit_requested = Signal(str)

    def __init__(self, world, world_sim_service, storage, parent=None):
        super().__init__(parent)
        self.world = world
        self.svc = world_sim_service
        self.storage = storage
        self.setWindowTitle("秘境")
        self.resize(620, 700)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        hint = QLabel("点击已知相邻房间走一步，到达后再探索；远房点击查看路线，未知房不会提前揭晓。"
                      "处理出口即可下层，可留下支路。离开须沿路线返回第一层入口，探索结果保留。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(hint)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
        outer.addWidget(self.status_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        self.body.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self.bottom = QHBoxLayout()
        self.bottom.setSpacing(8)
        self.bottom.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        self.bottom.addWidget(close_btn)
        outer.addLayout(self.bottom)
        self._action_btns: list = []

        self._rebuild()

    # ---------- 便捷读取 ----------
    def _cur_dungeon(self):
        loc = self.svc._current_location(self.world)
        # [D03] 玩家已在内部时 interior 命中（不受 discovered 门控）；在入口时
        # 只返回已发现的秘境——未发现的不弹详情（场景页秘境按钮另有 discovered 门控）
        return dge.interior_dungeon(self.world, loc) or dge.dungeon_at(
            self.world, loc, discovered_only=True)

    def _inside(self) -> bool:
        loc = self.svc._current_location(self.world)
        return dge.interior_dungeon(self.world, loc) is not None

    # ---------- 构建 ----------
    def _rebuild(self):
        while self.body.count() > 1:
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # [review] 动作按钮按引用清理（takeAt(0) 会先取走 stretch 致旧按钮残留）
        for b in self._action_btns:
            try:
                self.bottom.removeWidget(b)
            except Exception:
                pass
            b.setParent(None)
            b.deleteLater()
        self._action_btns = []
        dungeon = self._cur_dungeon()
        if not isinstance(dungeon, Dungeon):
            self._insert_empty("此地没有秘境。探索未知地带或在奇遇中寻找秘境入口。")
            return
        inside = self._inside()
        day = int(getattr(self.world, "day_count", 1) or 1)
        status_txt = ("可探索" if dungeon.status == "open" else
                      f"已封印（约 {max(0, int(dungeon.cooldown_until_day or 0) - day)} 天后重生）")
        if dungeon.status == "sealed" and day >= dungeon.cooldown_until_day:
            status_txt = "重生延后：须先离开内部并取完遗留物"
        self._insert_header(dungeon, inside, status_txt,
                            dungeon.status == "open")
        # ---- 探索总进度 ----
        total_rooms = sum(len(list(getattr(f, "rooms", None) or [])) for f in (dungeon.floors or []))
        done_rooms = sum(1 for f in (dungeon.floors or [])
                         for r in (list(getattr(f, "rooms", None) or []))
                         if getattr(r, "done", False))
        self._insert_progress(done_rooms, total_rooms)
        # ---- 层图 ----
        cur_floor = dge.current_floor(dungeon)
        for f in dungeon.floors:
            self._insert_floor_card(f, cur=(cur_floor is not None and f.index == cur_floor.index),
                                    dungeon=dungeon)
        # ---- 动作 ----
        if dungeon.status != "open":
            # [R0/B02-F09] 封印但玩家仍在内部（Boss 伏诛瞬间的窗口期）：保留离开路径，
            # 不能只剩「关闭」把人闷在里面；未取走的滞留奖励也提示「深入」可补发。
            if inside:
                self._insert_empty("首领已伏诛，沿已知房间返回入口；遗留物须回来源房间补领。")
                if dge.has_pending_loot(dungeon):
                    loot = QPushButton("领取当前房间遗留物")
                    loot.setEnabled(bool(dge.current_room(dungeon)[1] and dge.current_room(dungeon)[1].pending_items))
                    loot.clicked.connect(self._on_explore)
                    self._add_action(loot)
                btn2 = QPushButton("离开秘境")
                btn2.setObjectName("primaryBtn")
                btn2.setToolTip("撤回入口（房间进度保留）")
                ok, why = dge.can_exit_dungeon(dungeon)
                btn2.setEnabled(ok)
                if why:
                    btn2.setToolTip(why)
                btn2.clicked.connect(self._on_exit)
                self._add_action(btn2)
            else:
                if dge.has_pending_loot(dungeon):
                    self._insert_empty("秘境有遗留物，可沿原路线返回来源房间领取；封印期间不能探索新房间。")
                    reclaim = QPushButton("返回领取遗留物")
                    reclaim.clicked.connect(self._on_enter)
                    self._add_action(reclaim)
                else:
                    self._insert_empty("封印中的秘境无法进入，等待其重生（难度将提升）。")
            return
        if not inside:
            btn = QPushButton("进入秘境")
            btn.setObjectName("primaryBtn")
            btn.setToolTip("踏入秘境内部（同伴跟随；内部无法购宅/休憩跳时）")
            btn.clicked.connect(self._on_enter)
            self._add_action(btn)
        else:
            _f, room, _i = dge._target_room(dungeon)
            tools = dge.interaction_tools(self.world, dungeon)
            if tools:
                row = QWidget()
                lay = QHBoxLayout(row)
                lay.setContentsMargins(0, 0, 0, 0)
                combo = QComboBox()
                for tool in tools:
                    qty = self.world.player.inventory.count(tool.id)
                    combo.addItem(f"{tool.name} x{qty}", tool.id)
                lay.addWidget(combo, 1)
                tool_btn = QPushButton("消耗工具开箱" if room.type == "treasure" else "消耗工具解除")
                tool_btn.setToolTip("消耗一件，基础成功率 +5 个百分点（再计天赋与上限）；失败也消耗，本次结果保存")
                tool_btn.clicked.connect(lambda _=False, c=combo, r=room:
                                         self._on_interaction("tool", r.id, c.currentData()))
                lay.addWidget(tool_btn)
                self.body.insertWidget(self.body.count() - 1, row)
            if (room is not None and room.type in ("check", "trap") and room.seen
                    and not room.done and not room.check_result and not room.pending and not room.pending_items):
                bypass = QPushButton("安全绕行")
                bypass.setToolTip("结束该房互动，不取奖励；机关捷径不会开启，首领不会削弱")
                bypass.clicked.connect(lambda _=False, r=room: self._on_interaction("bypass", r.id))
                self._add_action(bypass)
            btn = QPushButton("深入探索")
            btn.setObjectName("primaryBtn")
            btn.setToolTip("互动当前房间；已探索时只沿唯一可行未探方向移动一步，多个方向须点击选路")
            btn.clicked.connect(self._on_explore)
            self._add_action(btn)
            btn2 = QPushButton("离开秘境")
            btn2.setToolTip("撤回入口（房间进度保留，重进续探）")
            ok, why = dge.can_exit_dungeon(dungeon)
            btn2.setEnabled(ok)
            if why:
                btn2.setToolTip(why)
            btn2.clicked.connect(self._on_exit)
            self._add_action(btn2)

    def _add_action(self, btn):
        self._action_btns.append(btn)
        self.bottom.insertWidget(self.bottom.count() - 1, btn)

    def _insert_header(self, dungeon: Dungeon, inside: bool, status_txt: str,
                       status_open: bool):
        """[深改 2026-10-01] 秘境头部：金顶线卡 + bannerName 大名 + 金线 + 徽章行
        （旧版一行富文本塞名字/危险度/层数/通关/状态/机关，像数据转储）。"""
        from src.ui.widgets.game_widgets import GameCard, game_badge, banner_rule
        try:
            _dg = int(dungeon.danger)
        except (TypeError, ValueError):
            _dg = 1
        card = GameCard(gold=True)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        name = QLabel(f"「{dungeon.name}」")
        name.setObjectName("bannerName")
        hl.addWidget(name)
        if inside:
            hl.addWidget(game_badge("◈ 你在秘境中", "gold"))
        hl.addStretch()
        card.add(head)
        card.add(banner_rule())
        brow = QWidget()
        bl = QHBoxLayout(brow)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        bl.addWidget(game_badge(
            f"危险度 {dungeon.danger}　{_danger_pips(dungeon.danger)}",
            "danger" if _dg >= 8 else ("warn" if _dg >= 5 else "info")))
        bl.addWidget(game_badge(f"{len(dungeon.floors)} 层", "info"))
        bl.addWidget(game_badge(f"通关 {dungeon.clears} 次", "info"))
        bl.addWidget(game_badge(status_txt, "success" if status_open else "warn"))
        if int(getattr(dungeon, "boss_relief", 0) or 0):
            n = int(dungeon.boss_relief)
            bl.addWidget(game_badge(f"机关已破 {n} 处（首领血量 -15%×{n}）", "gold"))
        bl.addStretch()
        card.add(brow)
        self.body.insertWidget(self.body.count() - 1, card)

    def _insert_progress(self, done: int, total: int):
        """[美化] 探索总进度条（房间 done/total）。"""
        if total <= 0:
            return
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(8)
        bar = QProgressBar()
        bar.setObjectName("factionBar")
        bar.setRange(0, total)
        bar.setValue(done)
        bar.setTextVisible(False)
        bar.setFixedHeight(10)
        bar.setProperty("barLevel", "high" if done >= total else ("mid" if done * 2 >= total else "low"))
        lay.addWidget(bar, 1)
        pct = QLabel(f"探索 {done}/{total}")
        pct.setStyleSheet("color:#9aa5ce; font-size:11px;")
        lay.addWidget(pct)
        self.body.insertWidget(self.body.count() - 1, row)

    def _insert_floor_card(self, f, cur: bool, dungeon) -> None:
        """[美化] 单层卡片：层头状态徽章 + 房间纵向节点行（纯展示，不改推进逻辑）。
        [深改 2026-10-01] GameCard 化：当前层金顶线、层头金字标题 + 状态徽章
        （旧蓝灰底/蓝边框与暗金古卷脱节）。"""
        from src.ui.widgets.game_widgets import GameCard, game_badge
        card = GameCard(gold=cur)
        lay = card.content
        # 层头
        if getattr(f, "cleared", False):
            badge, badge_lvl = "✓ 已肃清", "success"
        elif cur:
            badge, badge_lvl = "▶ 探索中", "gold"
        else:
            badge, badge_lvl = "未至", "info"
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        hl.addWidget(card.card_title_label(f"第 {f.index} 层"))
        hl.addStretch()
        hl.addWidget(game_badge(badge, badge_lvl))
        lay.addWidget(head)
        # 房间节点：纵向每行一个（窄窗不挤，悬浮看 desc）
        rooms = list(getattr(f, "rooms", None) or [])
        cur_pos = dge.position_room_id(dungeon) if self._inside() else ""
        by_id = {str(getattr(r, "id", "") or ""): r for ff in (dungeon.floors or [])
                 for r in (ff.rooms or [])}
        for i, r in enumerate(rooms):
            if not dge.room_known(dungeon, r):
                unknown = QLabel(f"{i + 1:02d}　未知房间")
                unknown.setStyleSheet("color:#565f89; padding:4px 8px;")
                lay.addWidget(unknown)
                continue
            fg = _ROOM_FG.get(getattr(r, "type", ""), "#9aa5ce")
            zh = _ROOM_ZH.get(getattr(r, "type", ""), getattr(r, "type", ""))
            rname = (dge.boss_room_display(self.world, dungeon, r)
                     if getattr(r, "type", "") == "boss" else (getattr(r, "name", "") or zh))
            if getattr(r, "done", False):
                chip_bg, chip_bd, st, st_fg = "#1c2b22", "#3d5a45", "✓ 已探", "#9ece6a"
            elif getattr(r, "pending", False):
                chip_bg, chip_bd, st, st_fg = "#2a2133", fg, "✦ 进行中", fg
            elif (getattr(r, "type", "") == "check" and getattr(r, "seen", False)):
                chip_bg, chip_bd, st, st_fg = "#1d2742", "#7aa2f7", "◉ 已观察（可破解）", "#7aa2f7"
            else:
                chip_bg, chip_bd, st, st_fg = "#1d1e2a", "#34313f", "· 未至", "#565f89"
            # [B01-F06] 滞留奖励标记（有满包没拿走的物品，清包后「深入」补发）
            if getattr(r, "pending_items", None):
                st = f"◈ 有物品未取走（{len(r.pending_items)}）"
                st_fg = "#e0af68"
            # [D01] 玩家站位标记 + 邻接通道（走哪条路看得见）
            here = cur_pos and str(getattr(r, "id", "") or "") == cur_pos
            if here:
                st = "▶ 你在这里 · " + st
            adj_names = [by_id[a].name for a in r.adjacent if a in by_id and dge.room_known(dungeon, by_id[a])]
            tip = f"{zh}·{rname}"
            if getattr(r, "desc", ""):
                tip += f"\n{r.desc}"
            if adj_names:
                tip += "\n通道：" + "、".join(adj_names)
            if getattr(r, "shortcut", None):
                tip += "\n机关捷径：" + ("已开通" if r.shortcut_open else "关闭")
            if r.hidden_result:
                tip += "\n隐藏检查：" + {"found": "已发现", "missed": "未发现（不会重抽）", "disabled": "未启用"}.get(r.hidden_result, "")
            if r.type == "trap" and r.seen and not r.done and not r.pending and not r.pending_items:
                st = "已发现（可解除/绕行）" + (" · 你在这里" if here else "")
            if r.check_result and r.type in ("check", "trap"):
                tip += "\n互动结果：" + {"success": "成功", "failure": "失败", "bypassed": "已绕过"}.get(r.check_result, "")
            row = QLabel(
                f"<span style='color:#3b4261;'>{(i + 1):02d}</span>　"
                f"<span style='color:{fg}; font-weight:bold;'>{zh}</span>"
                f"<span style='color:#c0caf5;'>　{rname}</span>"
                f"　<span style='color:{st_fg}; font-size:11px;'>{st}</span>"
                + ("<span style='color:#c9a86a;'>　◈</span>" if here else ""))
            row.setTextFormat(Qt.RichText)
            if self._inside() and not here:
                row.setText(f'<a href="{r.id}" style="text-decoration:none;">{row.text()}</a>')
                row.linkActivated.connect(lambda rid, d=dungeon: self._on_room_clicked(d, rid))
                row.setCursor(Qt.PointingHandCursor)
            row.setWordWrap(True)
            row.setToolTip(tip)
            row.setStyleSheet(f"background:{chip_bg}; border:1px solid {chip_bd}; "
                              f"border-radius:6px; padding:4px 8px; font-size:12px;"
                              + ("outline:1px solid #c9a86a;" if here else ""))
            lay.addWidget(row)
        exit_room = dge.room_by_id(dungeon, f.exit_room_id)[1]
        if exit_room is not None and dge.room_known(dungeon, exit_room) and f is not dungeon.floors[-1]:
            gate = QLabel("楼梯/首领门：" + ("出口已处理，可下层" if dge.room_resolved(exit_room) else f"须处理出口「{exit_room.name}」"))
            gate.setWordWrap(True)
            gate.setStyleSheet("color:#9aa5ce; font-size:11px;")
            lay.addWidget(gate)
        self.body.insertWidget(self.body.count() - 1, card)

    def _insert_empty(self, text: str):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#565f89; font-size:12px;")
        self.body.insertWidget(self.body.count() - 1, lbl)

    def _insert_label(self, text: str, cur: bool = False):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(("color:#c0caf5; background:#24283b; border-radius:4px; padding:4px;" if cur
                           else "color:#9aa5ce; font-size:12px;"))
        self.body.insertWidget(self.body.count() - 1, lbl)

    # ---------- 动作 ----------
    def _save(self):
        if self.storage is not None:
            try:
                self.storage.save_world(self.world)
            except Exception:
                pass

    def _on_enter(self):
        dungeon = self._cur_dungeon()
        if dungeon is None:
            return
        self.enter_requested.emit({"intent_type": "move", "resolved": True,
                                   "dungeon_action": "enter", "dungeon_id": dungeon.id,
                                   "dungeon_run_id": dungeon.run_id, "move_to": dungeon.name,
                                   "narration_hint": ""})
        self.accept()

    def _on_exit(self):
        ok, msg = self.svc.exit_dungeon(self.world)
        self.status_lbl.setText(msg)
        if ok:
            self._save()
            self.changed.emit()
            # [F14] 出秘境同理（房间进度保留，叙事写撤出场面）
            self.exit_requested.emit(msg)
            self.accept()

    def _on_explore(self):
        dungeon = self._cur_dungeon()
        self._save()   # 房间 pending 状态等先落盘（战后由 finish_combat 链路再落）
        self.explore_requested.emit(dungeon.name if dungeon is not None else "")
        self.accept()

    def _on_interaction(self, action: str, room_id: str, tool_id: str = ""):
        dungeon = self._cur_dungeon()
        if dungeon is None:
            return
        intent = {"intent_type": "move", "move_to": "深入", "resolved": True,
                  "narration_hint": "", "dungeon_action": action, "dungeon_room_id": room_id,
                  "dungeon_id": dungeon.id, "dungeon_run_id": dungeon.run_id,
                  "dungeon_from_room_id": dge.position_room_id(dungeon)}
        if action == "tool":
            tool = next((it for it in self.world.items if it.id == tool_id), None)
            if tool is None:
                return
            intent.update({"intent_type": "use_item", "target": tool.name, "dungeon_tool_id": tool.id})
        self.interaction_requested.emit(intent)
        self.accept()

    def _on_room_clicked(self, dungeon, rid: str):
        if not self._inside():
            return
        ok, why = dge.can_move_room(dungeon, rid)
        if not ok:
            path = dge.known_room_path(dungeon, rid)
            if len(path) > 2:
                names = [dge.room_by_id(dungeon, at)[1].name for at in path]
                why = "路线：" + " -> ".join(names) + f"；下一步请点击「{names[1]}」"
            self.status_lbl.setText(why)
            return
        _f, room = dge.room_by_id(dungeon, rid)
        self.interaction_requested.emit({"intent_type": "move", "move_to": room.name,
            "resolved": True, "narration_hint": "", "dungeon_action": "move",
            "dungeon_room_id": rid, "dungeon_from_room_id": dge.position_room_id(dungeon),
            "dungeon_run_id": dungeon.run_id, "dungeon_id": dungeon.id})
        self.accept()
