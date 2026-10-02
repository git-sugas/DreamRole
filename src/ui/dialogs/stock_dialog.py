"""[P34f] 交易所股市对话框（城市 auction shop 入口，每日 LLM 定价题材化大宗商品）。

布局：顶部金币 + 持仓市值；左 commodity 列表（名/当前价/涨跌%/mini 历史）+ 买入按钮；
右 玩家持仓列表 + 卖出按钮。买卖调 stock_engine 纯 Python 金币结算（无 LLM，LLM 只在
tick_stock_market 每日定价）。

[!] offscreen 模态阻塞：QMessageBox.information 在 offscreen Qt 是模态会卡死事件循环，
测试用 _NoModalMB stub patch 被测模块 QMessageBox（守 P34d 沉淀不变量）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QFrame, QSplitter, QMessageBox, QSpinBox, QWidget,
)

from src.models import World
from src.models.world_sim_preset import GenreText
from src.services import stock_engine as ske


class StockDialog(QDialog):
    """交易所股市：题材化大宗商品买卖（每日 LLM 定价，此对话框仅结算）。"""

    def __init__(self, world: World, storage=None, on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.storage = storage
        self.on_changed = on_changed
        self._gt = GenreText(getattr(world, "config_overlay", None) or {})
        self.setWindowTitle("交易所行情")
        self.resize(760, 600)

        from src.ui.widgets.game_widgets import (banner_rule, card_title,
                                                 GameCard)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        # [深改 2026-10-01] 页名大字 + 金笔分隔 + 徽章化顶栏（旧三段彩色裸文字）
        _title = QLabel("交易所行情")
        _title.setObjectName("bannerName")
        outer.addWidget(_title)
        outer.addWidget(banner_rule())
        outer.addSpacing(2)
        outer.addLayout(self._build_top_bar())

        # 主体：左行情 / 右持仓（GameCard 分区 + 金字区头）
        splitter = QSplitter(Qt.Horizontal)
        mk = GameCard()
        mk.add(card_title("行情"))
        mk.add(self._build_market_body())
        splitter.addWidget(mk)
        hd = GameCard()
        hd.add(card_title("持仓"))
        hd.add(self._build_holdings_body())
        splitter.addWidget(hd)
        splitter.setSizes([360, 360])   # [修对称 2026-10-01] 等宽分栏（旧 420/320 左宽右窄）
        outer.addWidget(splitter, 1)

        close_btn = QPushButton("关闭")

        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._refresh()

    # ============ 顶部 ============
    def _build_top_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        # [深改] 三统计徽章化：金色钱包 / 蓝市值 / 绿累计收入
        self.gold_label = QLabel("")
        self.gold_label.setObjectName("gameBadge")
        self.gold_label.setProperty("level", "gold")
        row.addWidget(self.gold_label)
        self.value_label = QLabel("")  # 持仓市值
        self.value_label.setObjectName("gameBadge")
        self.value_label.setProperty("level", "info")
        row.addWidget(self.value_label)
        self.realized_label = QLabel("")  # 累计已实现盈亏
        self.realized_label.setObjectName("gameBadge")
        self.realized_label.setProperty("level", "success")
        row.addWidget(self.realized_label)
        row.addStretch()
        return row

    # ============ 左：行情 ============
    def _build_market_body(self) -> QWidget:
        ll = QVBoxLayout()
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        hint_top = QLabel("点击商品选中，填数量买入")
        hint_top.setStyleSheet("color:#9aa5ce; font-size:11px;")
        ll.addWidget(hint_top)
        self.market_list = QListWidget()
        # [修裁字 2026-10-01] 行控件列表：竖向零内边距档（全局 ::item 8px 竖 padding
        # 会压缩 itemWidget 高度，紧凑两行行情文字被裁）；行间距走列表 spacing
        self.market_list.setObjectName("rowWidgetList")
        self.market_list.setSpacing(2)
        self.market_list.itemClicked.connect(self._on_market_click)
        ll.addWidget(self.market_list, 1)
        # 买入区
        buy_row = QHBoxLayout()
        buy_row.addWidget(QLabel("数量:"))
        self.buy_qty = QSpinBox()
        self.buy_qty.setRange(1, 9999)
        self.buy_qty.setValue(1)
        buy_row.addWidget(self.buy_qty)
        self.buy_btn = QPushButton("买入")
        self.buy_btn.setObjectName("primaryBtn")
        self.buy_btn.setEnabled(False)
        self.buy_btn.clicked.connect(self._on_buy)
        buy_row.addWidget(self.buy_btn)
        buy_row.addStretch()
        ll.addLayout(buy_row)
        hint = QLabel("价格每日变动（据世界事件/节日/经济）；涨跌%据昨日价。")
        hint.setStyleSheet("color:#565f89; font-size:11px;")
        hint.setWordWrap(True)
        ll.addWidget(hint)
        body = QWidget()
        body.setLayout(ll)
        return body

    # ============ 右：持仓 ============
    def _build_holdings_body(self) -> QWidget:
        rl = QVBoxLayout()
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        hint_top = QLabel("点击持仓选中，填数量卖出")
        hint_top.setStyleSheet("color:#9aa5ce; font-size:11px;")
        rl.addWidget(hint_top)
        self.hold_list = QListWidget()
        self.hold_list.itemClicked.connect(self._on_hold_click)
        rl.addWidget(self.hold_list, 1)
        sell_row = QHBoxLayout()
        sell_row.addWidget(QLabel("数量:"))
        self.sell_qty = QSpinBox()
        self.sell_qty.setRange(1, 9999)
        self.sell_qty.setValue(1)
        sell_row.addWidget(self.sell_qty)
        self.sell_btn = QPushButton("卖出")
        self.sell_btn.setObjectName("dangerBtn")
        self.sell_btn.setEnabled(False)
        self.sell_btn.clicked.connect(self._on_sell)
        sell_row.addWidget(self.sell_btn)
        sell_row.addStretch()
        rl.addLayout(sell_row)
        # [修对称 2026-10-01] 与左栏底部说明行对齐（否则买入/卖出按钮行高度错位）
        hint = QLabel("按当前行情价即时结算，所得立刻入袋。")
        hint.setStyleSheet("color:#565f89; font-size:11px;")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        body = QWidget()
        body.setLayout(rl)
        return body

    # ============ 刷新 ============
    def _refresh(self):
        p = self.world.player
        self.gold_label.setText(f"{self._gt.currency}：{p.gold}")
        # 行情列表（[深改] 行卡片化：名/价右对齐大字 + 涨跌%局部着色 + 走势箭头；
        # symbol 是底层 key 不上 UI（§23 显示名口径 + UI 禁英文））
        self.market_list.clear()
        sm = getattr(self.world, "stock_market", None)
        commodities = getattr(sm, "commodities", []) if sm else []
        for c in commodities:
            row_w = self._market_row(c)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, c.symbol)
            item.setSizeHint(row_w.sizeHint())
            self.market_list.addItem(item)
            self.market_list.setItemWidget(item, row_w)
        # 持仓列表
        self.hold_list.clear()
        holdings = p.stock_holdings if isinstance(p.stock_holdings, dict) else {}
        for sym, qty in holdings.items():
            c = sm.find(sym) if sm else None
            name = c.name if c else sym
            cur_price = c.price if c else 0
            value = int(qty) * int(cur_price)
            text = f"{name}  持有 {qty}\n当前价 {cur_price} | 市值 {value}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, sym)
            self.hold_list.addItem(item)
        # 顶部统计
        hv = ske.holdings_value(self.world)
        self.value_label.setText(f"持仓市值：{hv}")
        self.realized_label.setText(f"累计卖出收入：{getattr(p, 'stock_realized_gold', 0)}")
        # 按钮态
        self._on_market_click()
        self._on_hold_click()

    def _market_row(self, c) -> QWidget:
        """[深改] 单条行情行：左名/右价+%涨跌（涨红跌绿局部着色，不再整行变色）；
        第二行基准价 + 近期走势箭头。整行对鼠标透明，点击穿透回列表条目选中。"""
        row = QWidget()
        row.setAttribute(Qt.WA_TransparentForMouseEvents)
        col = QVBoxLayout(row)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        nm = QLabel(c.name)
        nm.setStyleSheet("color:#e0c48f; font-weight:bold; font-size:13px;")
        top.addWidget(nm)
        top.addStretch()
        price = QLabel(str(c.price))
        price.setStyleSheet("color:#c0caf5; font-weight:bold; font-size:14px;")
        top.addWidget(price)
        if c.prev_price > 0:
            pct = (c.price - c.prev_price) / c.prev_price * 100
            up = pct > 0
            pct_l = QLabel(f"{'+' if pct >= 0 else ''}{round(pct, 1)}%"
                           + ("▲" if up else ("▼" if pct < 0 else "—")))
            pct_l.setStyleSheet(
                f"color:{'#f7768e' if up else ('#9ece6a' if pct < 0 else '#565f89')};"
                "font-weight:bold; font-size:12px;")
            top.addWidget(pct_l)
        col.addLayout(top)
        sub = QHBoxLayout()
        sub.setSpacing(6)
        base = QLabel(f"基准 {c.base_price}")
        base.setStyleSheet("color:#9aa5ce; font-size:11px;")
        sub.addWidget(base)
        sub.addStretch()
        hist_tail = c.history[-5:] if c.history else []
        if hist_tail:
            seq = [c.prev_price] + list(hist_tail) if c.prev_price > 0 else list(hist_tail)
            spans = []
            for a, b in zip(seq, seq[1:]):
                mark = "▲" if b > a else ("▼" if b < a else "—")
                colr = "#f7768e" if b > a else ("#9ece6a" if b < a else "#565f89")
                spans.append(f"<span style='color:{colr};'>{mark}</span>")
            trend = QLabel("近期 " + "".join(spans))
            trend.setTextFormat(Qt.RichText)
            trend.setStyleSheet("font-size:11px;")
            sub.addWidget(trend)
        else:
            none_l = QLabel("近期 —")
            none_l.setStyleSheet("color:#565f89; font-size:11px;")
            sub.addWidget(none_l)
        col.addLayout(sub)
        return row

    # ============ 交互 ============
    def _selected_market_symbol(self) -> str | None:
        row = self.market_list.currentRow()
        if row < 0:
            return None
        return self.market_list.item(row).data(Qt.UserRole)

    def _selected_hold_symbol(self) -> str | None:
        row = self.hold_list.currentRow()
        if row < 0:
            return None
        return self.hold_list.item(row).data(Qt.UserRole)

    def _on_market_click(self):
        self.buy_btn.setEnabled(self._selected_market_symbol() is not None)

    def _on_hold_click(self):
        self.sell_btn.setEnabled(self._selected_hold_symbol() is not None)

    def _on_buy(self):
        sym = self._selected_market_symbol()
        if sym is None:
            return
        qty = int(self.buy_qty.value())
        ok, msg = ske.buy_stock(self.world, sym, qty)
        if ok:
            self._save_and_refresh()
            QMessageBox.information(self, "买入", msg)
        else:
            QMessageBox.warning(self, "无法买入", msg)

    def _on_sell(self):
        sym = self._selected_hold_symbol()
        if sym is None:
            return
        qty = int(self.sell_qty.value())
        ok, msg = ske.sell_stock(self.world, sym, qty)
        if ok:
            self._save_and_refresh()
            QMessageBox.information(self, "卖出", msg)
        else:
            QMessageBox.warning(self, "无法卖出", msg)

    def _save_and_refresh(self):
        if self.storage is not None:
            self.storage.save_world(self.world)
        self._refresh()
        if self.on_changed:
            self.on_changed()
