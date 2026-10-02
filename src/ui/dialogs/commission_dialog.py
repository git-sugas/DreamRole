"""[P39b] 委托订单板对话框（聚落入口：NPC 收购单列表 + 当面交付）。

列表条目：[类型徽章] 品名 x 数量 | 单价/总价 | 发布人@聚落 | 剩余天数 | 风味描述。
非当前聚落的订单可见但交付被引擎拒绝（须当面）。交付调 commission_engine.
deliver_commission（金币入账 + 交情 + 声望 + 发布人背包收货）。

[!] offscreen 模态阻塞：QMessageBox 在 offscreen 测试须 stub（守 P34d 沉淀不变量）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QMessageBox,
)

from src.models import World
from src.services import commission_engine as come
from src.services import quest_engine as qe

_KIND_TAG = {"restock": "补货", "urgent": "急缺"}


class CommissionDialog(QDialog):
    """委托订单板：在架收购单 + 交付结算。"""

    def __init__(self, world: World, storage=None, on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.storage = storage
        self.on_changed = on_changed
        self.setWindowTitle("委托订单")
        self.resize(680, 540)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        # [UI 改造 2026-10-01 第四批] 页名大字 + 金笔分隔
        from src.ui.widgets.game_widgets import banner_rule
        _title = QLabel("委托订单板")
        _title.setObjectName("bannerName")
        outer.addWidget(_title)
        outer.addWidget(banner_rule())
        outer.addSpacing(2)

        # 顶部：金币 + 提示
        top = QHBoxLayout()
        p = world.player
        self.gold_label = QLabel(f"{qe._currency(self.world)}：{int(getattr(p, 'gold', 0) or 0)}")
        self.gold_label.setObjectName("gameBadge")
        self.gold_label.setProperty("level", "gold")
        top.addWidget(self.gold_label)
        top.addStretch(1)
        hint = QLabel("到发布聚落当面交付；紫以上装备须先鉴定")
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        top.addWidget(hint)
        outer.addLayout(top)

        self.order_list = QListWidget()
        self.order_list.itemClicked.connect(self._on_order_click)
        outer.addWidget(self.order_list, 1)

        # 交付区
        row = QHBoxLayout()
        self.detail_label = QLabel("选择一笔订单查看详情")
        self.detail_label.setWordWrap(True)
        row.addWidget(self.detail_label, 1)
        self.deliver_btn = QPushButton("交付")
        self.deliver_btn.setObjectName("primaryBtn")
        self.deliver_btn.setEnabled(False)
        self.deliver_btn.clicked.connect(self._on_deliver)
        row.addWidget(self.deliver_btn)
        outer.addLayout(row)

        self._reload()

    # ---- 数据渲染 ----

    def _reload(self):
        self.order_list.clear()
        self.deliver_btn.setEnabled(False)
        self.detail_label.setText("选择一笔订单查看详情")
        day = max(1, int(getattr(self.world, "day_count", 1) or 1))
        p = self.world.player
        cur_loc = str(getattr(p, "location_id", "") or "")
        orders = list(getattr(self.world, "commissions", None) or [])
        if not orders:
            self.order_list.addItem(QListWidgetItem("（暂无在架订单——明日再来看看）"))
            return
        for o in orders:
            left = max(0, int(getattr(o, "expire_day", 0) or 0) - day)
            here = "★本城" if str(getattr(o, "location_id", "") or "") == cur_loc else ""
            issuer = next((n.name for n in (getattr(self.world, "npcs", None) or [])
                           if str(getattr(n, "id", "") or "") == str(o.issuer_npc_id)), "神秘客")
            loc_name = next((l.name for l in (getattr(self.world, "locations", None) or [])
                             if str(getattr(l, "id", "") or "") == str(o.location_id)), "")
            can = len(come.deliverable_indices(self.world, p, o)) >= int(o.qty) and here
            it = QListWidgetItem(
                f"[{_KIND_TAG.get(o.kind, '补货')}] {o.item_name} ×{o.qty} "
                f"| 单价 {o.unit_price}（合计 {o.unit_price * int(o.qty)}） "
                f"| {issuer}·{loc_name} {here} | 剩 {left} 天"
                + ("  ✦可交付" if can else ""))
            it.setToolTip(f"{o.reason}\n剩余 {left} 天到期；{'在本城可交付' if here else '须前往该聚落当面交付'}")
            it.setData(0x0100, str(o.id))   # Qt.UserRole
            it.setData(0x0100 + 1, bool(can))
            # 可交付高亮（绿），其余常规灰白
            it.setForeground(self._fg("#9ece6a" if can else "#d7dae0"))
            self.order_list.addItem(it)
        self.gold_label.setText(f"{qe._currency(self.world)}：{int(getattr(p, 'gold', 0) or 0)}")

    def _fg(self, hex_color: str):
        from PySide6.QtGui import QColor
        return QColor(hex_color)

    # ---- 交互 ----

    def _on_order_click(self, item: QListWidgetItem):
        oid = item.data(0x0100)
        o = next((c for c in (getattr(self.world, "commissions", None) or [])
                  if str(getattr(c, "id", "") or "") == str(oid)), None)
        if o is None:
            return
        self.detail_label.setText(o.reason)
        self.deliver_btn.setEnabled(bool(item.data(0x0100 + 1)))

    def _on_deliver(self):
        item = self.order_list.currentItem()
        if item is None:
            return
        oid = str(item.data(0x0100) or "")
        ok, r = come.deliver_commission(self.world, self.world.player, oid)
        if not ok:
            QMessageBox.warning(self, "无法交付", str(r))
            return
        QMessageBox.information(
            self, "交付完成",
            f"交付 {r['qty']} 件「{r['item']}」，入账 {r['payout']} {qe._currency(self.world)}"
            + (f"，{r['issuer']}的交情 +{r['affinity_gain']}" if r.get("issuer") else ""))
        if self.on_changed:
            self.on_changed()
        self._reload()
