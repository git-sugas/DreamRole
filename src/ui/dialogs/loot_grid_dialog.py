"""[P58 摸格子 2026-09-26] 战利品格子对话框（三角洲式拾取，战斗胜利后弹出）。

[暗金重绘+翻牌 2026-10-02 用户指示] 每格初始是「？」——点击后短暂翻找动画，再揭示
是什么（旧版一眼看穿全部掉落，没有摸尸的悬念感）；**揭示不自动拾取**（用户指示：
只揭示是什么）——复点才拿，背包放不下则保持翻开、腾格子后再点。视觉走暗金古卷
体系（page_header + lootCell 卡）。

守恒收口（结算真相）：关闭时 loot_grid_settle——未拿的 salvage/reclaim 物品原地不动
（尸体/劫掠者身上，之后可搜刮或再战夺回），未拿的纯掉落 NPC 尸体留尸可「搜刮」再拿、
临时野怪随尸身消散（关闭前二次确认）。金币/经验已在战斗结算时自动发放，不进格子。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame, QMessageBox, QWidget, QGridLayout,
)

from src.ui.widgets.game_widgets import page_header, GameCard

# 翻找动画：揭示前的时间（毫秒）。0 = 同步揭示（离屏测试口径）。
_REVEAL_MS = 650
_BUSY_TICK_MS = 140


class _LootCell(QFrame):
    """单个战利品格：hidden(？) -> busy(翻找动画) -> revealed(品级卡) -> taken。

    hidden/revealed 状态点击进入拾取流（revealed 复点=直接拾取，不重播动画）；
    taken 状态不可点。状态经 dynamic property `state` 交给 QSS 渲染。
    """

    def __init__(self, item_id: str, name: str, rarity: str, reclaim: bool,
                 on_take, reveal_ms: int = _REVEAL_MS, parent=None):
        super().__init__(parent)
        self._item_id = item_id
        self._name = name
        self._rarity = rarity
        self._reclaim = reclaim
        self._on_take = on_take          # (item_id) -> bool（True=已入包）
        self._reveal_ms = max(0, int(reveal_ms))
        self._busy_step = 0
        self._busy_timer = None
        self.setObjectName("lootCell")
        self.setProperty("state", "hidden")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点击翻开")
        self.setMinimumHeight(64)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 8, 10, 8)
        col.setSpacing(2)
        self._main_lbl = QLabel("？")
        self._main_lbl.setObjectName("lootCellMain")
        self._main_lbl.setAlignment(Qt.AlignCenter)
        self._main_lbl.setWordWrap(True)
        col.addWidget(self._main_lbl, 1)
        self._sub_lbl = QLabel("点击翻开")
        self._sub_lbl.setObjectName("lootCellSub")
        self._sub_lbl.setAlignment(Qt.AlignCenter)
        col.addWidget(self._sub_lbl)

    # ---- 状态机 ----

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._activate()
        super().mousePressEvent(event)

    def _activate(self):
        state = str(self.property("state") or "")
        if state in ("busy", "taken"):
            return
        if state == "revealed":
            self._try_take()
            return
        # hidden -> busy -> reveal
        self._set_state("busy")
        self._busy_step = 0
        self._main_lbl.setText("？")
        self._sub_lbl.setText("翻找中")
        self.setToolTip("正在翻找…")
        if self._reveal_ms <= 0:
            self._reveal()
            return
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(_BUSY_TICK_MS)
        self._busy_timer.timeout.connect(self._on_busy_tick)
        self._busy_timer.start()
        QTimer.singleShot(self._reveal_ms, self._reveal)

    def _on_busy_tick(self):
        self._busy_step = (self._busy_step + 1) % 4
        self._main_lbl.setText("？" + "·" * self._busy_step)

    def _stop_busy(self):
        if self._busy_timer is not None:
            self._busy_timer.stop()
            self._busy_timer = None

    def _reveal(self):
        if str(self.property("state") or "") != "busy":
            return                          # 已被重复触发/关闭
        self._stop_busy()
        self._set_state("revealed")
        if self._rarity:
            self.setProperty("rarity", self._rarity)
        self._polish()
        title = ("★你遗失之物·" if self._reclaim else "") + self._name
        self._main_lbl.setText(title)
        self._sub_lbl.setText("点击拾取")
        self.setToolTip("点击拾取（负重不足或同种已满 3 件会被拒收）")
        # [用户指示 2026-10-02] 揭示只亮名牌，不自动拾取——拿不拿玩家说了算

    def _try_take(self):
        if self._on_take(self._item_id):
            self._set_state("taken")
            self._main_lbl.setText("✓ 已入包")
            self._sub_lbl.setText("")
            self.setToolTip("")
            self.setCursor(Qt.ArrowCursor)

    def _set_state(self, state: str):
        self.setProperty("state", state)
        self._polish()

    def _polish(self):
        self.style().unpolish(self)
        self.style().polish(self)
        for c in (self._main_lbl, self._sub_lbl):
            c.style().unpolish(c)
            c.style().polish(c)


class LootGridDialog(QDialog):
    """战利品格子。grid 参数 = finish_combat/_resolve_combat 产出的 summary["loot_grid"]。

    拾取经 svc.loot_grid_take（try_add 单一来源），关闭经 svc.loot_grid_settle
    （守恒收口，done() 统一入口保证 Esc/X/按钮三条路径都结算且只结算一次）。
    reveal_ms=0 时翻牌同步揭示（离屏测试口径）。
    """

    def __init__(self, svc, world, grid: dict, parent=None, reveal_ms: int = _REVEAL_MS):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.grid = grid
        self.entries = list(grid.get("entries") or [])
        self._settled = False
        self._reveal_ms = reveal_ms
        self.result_lines: list[str] = []   # 结算事实（夺回/下落不明），调用方补系统条目

        self.setWindowTitle("战利品")
        self.resize(680, 540)
        self.setMinimumSize(540, 440)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        outer.addWidget(page_header(f"{grid.get('npc_name') or '敌人'}的遗物"))
        hint = QLabel("每格都还没翻开——点「？」摸一摸看是什么，再点一次拾取（背包有负重与同种上限）")
        hint.setStyleSheet("color:#8f9ab3; font-size:12px;")
        outer.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(12)

        # 左：战利品格子
        left = QVBoxLayout()
        left_label = QLabel("战利品格子")
        left_label.setStyleSheet("color:#e0c48f; font-weight:bold;")
        left.addWidget(left_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        self._grid_host = QWidget()
        self._grid_lay = QGridLayout(self._grid_host)
        self._grid_lay.setContentsMargins(0, 0, 0, 0)
        self._grid_lay.setSpacing(8)
        scroll.setWidget(self._grid_host)
        left.addWidget(scroll, 1)
        body.addLayout(left, 3)

        # 右：背包读数（暗金卡）
        right = QVBoxLayout()
        right_label = QLabel("你的背包")
        right_label.setStyleSheet("color:#e0c48f; font-weight:bold;")
        right.addWidget(right_label)
        bag_card = GameCard()
        self._bag_lbl = QLabel("")
        self._bag_lbl.setWordWrap(True)
        self._bag_lbl.setAlignment(Qt.AlignTop)
        self._bag_lbl.setStyleSheet("color:#c8bfa8;")
        bag_card.content.addWidget(self._bag_lbl)
        right.addWidget(bag_card, 1)
        bag_btn = QPushButton("整理背包")
        bag_btn.setObjectName("optionBtn")
        bag_btn.setToolTip("打开背包，可丢弃物品腾出格子后继续拾取")
        bag_btn.clicked.connect(self._open_bag)
        right.addWidget(bag_btn)
        body.addLayout(right, 2)

        outer.addLayout(body, 1)

        btn_row = QHBoxLayout()
        self._hint_lbl = QLabel("")
        self._hint_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
        btn_row.addWidget(self._hint_lbl, 1)
        close_btn = QPushButton("收工")
        close_btn.setObjectName("primaryBtn")
        close_btn.setToolTip("拾取完成。未拿取的战利品：NPC 尸体留在原地可回头搜刮；野怪尸身会消散")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        self._rebuild()

    # ---- 渲染 ----

    def _item_info(self, e: dict) -> tuple:
        did = str(e.get("item_id", ""))
        base = did[:-6] if did.endswith("__unid") else did
        label = str(e.get("name", "") or did)
        rarity = ""
        it = next((i for i in (self.world.items or []) if i.id == base), None)
        if it is not None:
            rarity = str(getattr(it, "rarity", "") or "")
        return did, label, rarity

    def _rebuild(self):
        while self._grid_lay.count():
            item = self._grid_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        reclaim_set = set(self.grid.get("reclaimed_ids") or [])
        for i, e in enumerate(self.entries):
            did, label, rarity = self._item_info(e)
            base = did[:-6] if did.endswith("__unid") else did
            cell = _LootCell(did, label, rarity, base in reclaim_set,
                             on_take=self._on_take, reveal_ms=self._reveal_ms)
            self._grid_lay.addWidget(cell, i // 2, i % 2)
        if not self.entries:
            empty = QLabel("（没有可拾取的东西）")
            empty.setStyleSheet("color:#565f89; padding:16px;")
            self._grid_lay.addWidget(empty, 0, 0)
        self._refresh_bag()

    def _refresh_bag(self):
        inv = list(getattr(self.world.player, "inventory", None) or [])
        from src.services.combat_engine import carry_capacity
        capacity = carry_capacity(self.world.player)
        names = []
        for iid in inv:
            it = next((i for i in (self.world.items or []) if i.id == iid), None)
            names.append(getattr(it, "name", "") or iid)
        self._bag_lbl.setText(f"占用 {len(inv)}/{capacity} 格\n\n" + "、".join(names[:40])
                              + ("…" if len(names) > 40 else ""))

    # ---- 交互 ----

    def _open_bag(self):
        from src.ui.dialogs.inventory_dialog import InventoryDialog
        InventoryDialog(self.world, parent=self).exec()
        self._refresh_bag()

    def _on_take(self, item_id: str) -> bool:
        ok = self.svc.loot_grid_take(self.world, item_id)
        if not ok:
            self._hint_lbl.setText("拿不下：背包满了或同种已到 3 件——先丢点别的再拿。")
            return False
        self._hint_lbl.setText("")
        self.entries = [e for e in self.entries if str(e.get("item_id")) != item_id]
        self.grid["entries"] = self.entries
        # 延迟重建：让「✓ 已入包」状态先显示一小会儿再收格（同步测试口径立即）
        QTimer.singleShot(420 if self._reveal_ms > 0 else 0, self._rebuild)
        return True

    def _left_count(self) -> int:
        return len(self.entries)

    def _settle_once(self):
        if self._settled:
            return
        self._settled = True
        self.grid["entries"] = self.entries
        res = self.svc.loot_grid_settle(self.world, self.grid)
        # [P47 因果可见性] 夺回/下落不明事实随结算产生：调用方经 result_lines 补系统条目
        npc_name = str(self.grid.get("npc_name") or "对方")
        if res.get("reclaimed_back"):
            self.result_lines.append(
                f"你从{npc_name}身上夺回了被抢走的：{'、'.join(res['reclaimed_back'])}")
        if res.get("missing"):
            self.result_lines.append(
                f"至于他早前抢走的{'、'.join(res['missing'])}——早已被他转手卖掉了")
        left = res.get("leftover") or []
        if left:
            self.result_lines.append(
                f"留在尸身上的东西：{'、'.join(left[:8])}" + ("…" if len(left) > 8 else "")
                + "（回头可以「搜刮」再拿）")

    def done(self, r):
        """统一收口：按钮/Esc/X 三条路径都守恒结算且只结算一次。"""
        if not self._settled and self._left_count() > 0 and not self.grid.get("npc_id"):
            ret = QMessageBox.question(
                self, "战利品",
                "野怪尸身即将消散——未拾取的战利品会一并消失。\n确定不拿了吗？",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
        self._settle_once()
        super().done(r)
