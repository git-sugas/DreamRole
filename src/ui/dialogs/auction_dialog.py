"""[P34g] 拍卖会对话框（城市交易所 venue 在拍卖会 active 时进入，限时竞价）。

布局：顶部金币 + 拍卖会信息（城市/剩余天数）；拍品列表（品级/起拍价/当前价/竞价档位）；
竞价按钮（每次加一个 bid_step）。拍品未鉴定显示「未鉴定·名」。竞价调 auction_engine.place_bid
（押金模式：出价即扣，被超出退还上一竞拍者）。

[!] offscreen 模态阻塞：QMessageBox.information/warning 在 offscreen Qt 是模态会卡死事件循环，
测试用 _NoModalMB stub patch 被测模块 QMessageBox（守 P34d 沉淀不变量）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QMessageBox,
)

from src.models import World, AuctionEvent, AuctionLot
from src.models.world_sim_preset import GenreText
from src.services import auction_engine as aue


_RARITY_COLOR = {
    "common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
    "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555",
}


class AuctionDialog(QDialog):
    """拍卖会竞价：限时拍品列表 + 押金竞价。"""

    def __init__(self, world: World, auction: AuctionEvent, storage=None,
                 on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.auction = auction
        self.storage = storage
        self.on_changed = on_changed
        self._gt = GenreText(getattr(world, "config_overlay", None) or {})
        self.setWindowTitle("拍卖会")
        self.resize(640, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # 顶部：金币 + 拍卖会信息
        outer.addLayout(self._build_top_bar())

        # 拍品列表
        self.lot_list = QListWidget()
        self.lot_list.itemClicked.connect(self._on_lot_click)
        outer.addWidget(self.lot_list, 1)

        # 竞价区
        bid_row = QHBoxLayout()
        bid_row.addWidget(QLabel("出价（当前价 + 档位）:"))
        self.bid_btn = QPushButton("加价竞拍")
        self.bid_btn.setObjectName("primaryBtn")
        self.bid_btn.setEnabled(False)
        self.bid_btn.clicked.connect(self._on_bid)
        bid_row.addWidget(self.bid_btn)
        bid_row.addStretch()
        outer.addLayout(bid_row)

        cur = GenreText(getattr(self.world, "config_overlay", None) or {}).currency
        hint = QLabel(f"押金模式：出价即扣{cur}，被他人超出自动退还。未鉴定拍品属性未知，赌一把？")
        hint.setStyleSheet("color:#565f89; font-size:11px;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        close_btn = QPushButton("关闭")

        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._refresh()

    def _build_top_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        self.gold_label = QLabel("")
        self.gold_label.setStyleSheet("color:#e0af68; font-weight:bold;")
        row.addWidget(self.gold_label)
        row.addStretch()
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color:#7aa2f7;")
        row.addWidget(self.info_label)
        return row

    def _refresh(self):
        p = self.world.player
        self.gold_label.setText(f"{self._gt.currency}：{p.gold}")
        city = next((l for l in self.world.locations
                     if l.id == self.auction.city_location_id), None)
        city_name = city.name if city else "某城"
        remain = max(0, int(self.auction.end_day or 0) - int(self.world.day_count or 0))
        self.info_label.setText(f"{city_name} 拍卖会 | 剩余 {remain} 天")
        # 拍品列表
        self.lot_list.clear()
        for lot in self.auction.lots:
            if not isinstance(lot, AuctionLot):
                continue
            it = next((i for i in self.world.items if i.id == lot.item_id), None)
            if it is None:
                continue
            from src.ui.widgets.item_brief import display_name
            color = _RARITY_COLOR.get(it.rarity, "#c0caf5")
            name = display_name(it)  # 未鉴定 -> 「未鉴定·名」
            # [NPC 竞拍 2026-09-11] 三态：无人出价 / 你领先 / NPC 领先（旧二态会把 NPC
            # 领先误显示成「你领先」）。
            _kind, _who = aue.leader_name(self.world, lot)
            if not _kind:
                leader = "（无人出价）"
            elif _kind == "player":
                leader = "（你领先）"
            else:
                leader = f"（{_who} 领先）"
            text = (f"{name}  [{self._gt.rarity(it.rarity)}]  Lv{it.level}\n"
                    f"起拍 {lot.start_price} | 当前 {lot.current_bid} {leader}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, lot.item_id)
            item.setForeground(QColor(color))
            self.lot_list.addItem(item)
        self._on_lot_click()

    def _selected_lot(self) -> AuctionLot | None:
        row = self.lot_list.currentRow()
        if row < 0:
            return None
        iid = self.lot_list.item(row).data(Qt.UserRole)
        return next((l for l in self.auction.lots
                     if isinstance(l, AuctionLot) and l.item_id == iid), None)

    def _on_lot_click(self):
        lot = self._selected_lot()
        self.bid_btn.setEnabled(lot is not None and not lot.sold)

    def _on_bid(self):
        lot = self._selected_lot()
        if lot is None:
            return
        amount = int(lot.current_bid) + int(lot.bid_step)
        ok, msg = aue.place_bid(self.world, self.auction, lot, amount)
        if ok:
            self._save_and_refresh()
            QMessageBox.information(self, "竞拍", msg)
        else:
            QMessageBox.warning(self, "无法竞拍", msg)

    def _save_and_refresh(self):
        if self.storage is not None:
            self.storage.save_world(self.world)
        self._refresh()
        if self.on_changed:
            self.on_changed()
