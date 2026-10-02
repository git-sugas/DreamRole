"""[P6] 世界模拟商店对话框（双栏交易，纯代码结算 + [P14] 关店店主 LLM 回应，仅真商人店）。

玩家点商店选购买/卖出 -> 直接走 trade_engine 扣金币/挪物品（数值不经 LLM）。
布局：顶栏（店名 + 玩家金币 + 声望折扣 + 刷新商品）+ QSplitter（左=商店货架·买入 / 右=你的背包·卖出）。
- 买入：左栏选中 -> 购买选中；卖出：右栏选中 -> 卖出选中。每次交易 save_world + on_changed 刷新场景页。
- 声望影响定价：trade_engine.apply_reputation_factor（高声望打折/低声望加价），顶栏显示当前档。
- [P14] 经济节奏：economy_pace（overlay）乘入买/卖价（trade_engine.pace_factors）。
- [P14] 交易记账 + 关店回应（仅真商人店，游商店跳过不调 LLM）：每笔买卖记入 _trades；
  关闭对话框时（有交易 + trade_reply_enabled）后台 _TradeReplyWorker 调叙事 LLM
  出一句店主收摊回应，以 toast 浮在场景页显示。
- 刷新商品：on_regen_request 回调（场景页提供 -> ShopWorker LLM 换货）；无回调则隐藏。

守 §21 完全独立铁律 + 底层字段不变（货币题材化只改显示，gold 仍是数值）。零 emoji。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QTimer, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QSplitter, QFrame, QMessageBox,
)

from src.models import World, Item, Shop
from src.models.world_sim_preset import GenreText
from src.services import trade_engine as tre
from src.services import consequence_engine as cue
from src.ui.widgets.rarity_frame import RarityFrame


_RARITY_COLOR = {
    "common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
    "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555",
}


class _TradeReplyWorker(QThread):
    """[P14] 关店后店主回应后台任务（仿 npc_chat 的轻量 LLM 调用）。

    worker parent=None 自管（守 §15：dialog 即将关闭，parent=dialog 会级联析构运行中线程）；
    引用由 toast 宿主（场景页）持有防 GC。done_signal(str) 到主线程弹 toast。
    """
    done_signal = Signal(str)

    def __init__(self, svc, world, shop, trades, preset, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.shop = shop
        self.trades = trades
        self.preset = preset
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            line = self.svc.generate_trade_reply(
                self.world, self.shop, self.trades, self.preset,
                cancel_check=lambda: self._cancelled,
            )
            if line:
                self.done_signal.emit(line)
        except Exception:
            pass  # 回应是风味增强，失败静默


class ShopDialog(QDialog):
    """商店交易对话框（代码结算 + 关店店主回应，仅真商人店）。"""

    def __init__(self, world: World, shop: Shop, storage=None, on_changed=None,
                 on_regen_request=None, parent=None,
                 world_sim_service=None, preset=None):
        super().__init__(parent)
        self.world = world
        self.shop = shop
        self.storage = storage
        self.on_changed = on_changed
        self.on_regen_request = on_regen_request  # 场景页提供 -> 触发 ShopWorker 换货
        self.svc = world_sim_service              # [P14] 店主回应 LLM 调用
        self.preset = preset
        self._gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
        # [P14] 经济节奏（overlay 写入；买/卖价统一乘）+ 交易记账
        self._pace = tre.world_pace(world)
        self._trades: list[dict] = []
        self._reply_worker = None
        # [游商 2026-09-08] 标题署游商名（「{店名}·{称谓}」；旧档真商人店沿用店名）——
        # 玩家点开的是「人」不是「货架」，标题即身份。
        try:
            from src.services.world_sim_service import trade_vendor_name
            _vname = trade_vendor_name(world, shop)
        except Exception:
            _vname = ""
        self.setWindowTitle(_vname or shop.name or "商店")
        self.resize(960, 660)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        outer.addLayout(self._build_top_bar())
        hint = QLabel("左栏买入店主的货 · 右栏是你背包里的物品，选中后点对应按钮卖出换钱")
        hint.setStyleSheet("color:#7dcfff; font-size:12px;")
        outer.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_shop_panel())
        splitter.addWidget(self._build_inventory_panel())
        splitter.setSizes([480, 420])
        outer.addWidget(splitter, 1)

        close_btn = QPushButton("离开商店")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._refresh()

    # ============ 依赖 ============
    def _merchant(self):
        for n in self.world.npcs:
            if n.id == self.shop.merchant_npc_id:
                return n
        return None

    def _is_vendor_shop(self) -> bool:
        """[游商手动刷新 2026-09-09] 是否游商店（无真商人 NPC）。

        真商人判定口径与 svc._shop_merchant 一致（含 shop_id 反向挂载回退），
        svc 缺失时退化为 merchant_npc_id 正向查找。
        """
        try:
            if getattr(self.svc, "_shop_merchant", None) is not None:
                return self.svc._shop_merchant(self.world, self.shop) is None
        except Exception:
            pass
        return self._merchant() is None

    def _reputation(self) -> int:
        # [修 2026-09-10] 与 _merchant_trust 同源解析（反向挂载的真商人店此前读不到 faction）
        m = self._merchant_npc()
        if not m or not m.faction_id:
            return 0
        return int(self.world.player.reputation.get(m.faction_id, 0) or 0)

    def _merchant_npc(self):
        """真商人 NPC（口径与结算 tre.buy 的 merchant 参数一致：svc._shop_merchant 优先，
        缺失回退正向查找；游商店返回 None）。"""
        try:
            if getattr(self.svc, "_shop_merchant", None) is not None:
                return self.svc._shop_merchant(self.world, self.shop)
        except Exception:
            pass
        return self._merchant()

    def _merchant_trust(self) -> int:
        """[修 2026-09-10] 商人对玩家的印象信任度（与 tre.buy 结算同源）。

        原货架卡/余额校验不传该因子 -> 有真商人 NPC 的档「显示价 ≠ 实收价」
        （trust>=50 相熟 0.95 折 / <=-50 忌惮 1.1 抬价）。游商店无商人 -> 0，行为不变。
        """
        m = self._merchant_npc()
        imp = getattr(m, "player_impression", None) if m is not None else None
        return int(imp.get("trust", 0) or 0) if isinstance(imp, dict) else 0

    def _rep_tier(self) -> str:
        rep = self._reputation()
        if rep >= 50:
            return f"崇敬(-{int(0.30 * rep / 100 * 100)}%)"
        if rep >= 20:
            return "友好"
        if rep <= -50:
            return "敌视(加价)"
        if rep <= -20:
            return "冷淡"
        return "中立"

    def _item(self, item_id: str) -> Item | None:
        for it in self.world.items:
            if it.id == item_id:
                return it
        return None

    # ============ 顶栏 ============
    def _build_top_bar(self) -> QHBoxLayout:
        """[UI 改造 2026-10-01 第三批] 店招大名 + 金徽章货币 + 状态徽章行
        （打烊/敌对治下/战后短缺原是散落彩字，统一徽章化；乘号×）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        self.title_label = QLabel(self.shop.name or "商店")
        self.title_label.setObjectName("bannerName")
        row.addWidget(self.title_label)
        row.addStretch()
        self.gold_label = QLabel("")
        self.gold_label.setObjectName("gameBadge")
        self.gold_label.setProperty("level", "gold")
        row.addWidget(self.gold_label)
        self.rep_label = QLabel("")
        self.rep_label.setObjectName("gameBadge")
        self.rep_label.setProperty("level", "info")
        row.addWidget(self.rep_label)
        # [P39c] 打烊提示（auction 交易所 24h；夜间敲门可买但 1.5x）
        if not tre.shop_open(self.world, self.shop):
            from src.ui.widgets.game_widgets import game_badge
            night_label = game_badge("已打烊·敲门价 1.5×", "warn")
            night_label.setToolTip("打烊时段仍可敲门购买，价格上浮五成。")
            row.addWidget(night_label)
        # [P45(1)] 势力治下/物资短缺横幅（夺城后果做到每次购物可见）
        from src.ui.widgets.game_widgets import game_badge as _badge
        fac_by_id = {f.id: f for f in self.world.factions}
        loc = next((l for l in self.world.locations
                    if l.id == self.shop.location_id), None)
        terr_buy, terr_sell, terr_tag = tre.territory_price_factors(
            self.world, self.shop, self.world.player)
        if terr_tag:
            owner = fac_by_id.get((getattr(loc, "owner_faction_id", "") or
                                   getattr(loc, "faction_id", "") or ""), None)
            oname = owner.name if owner is not None else "当地势力"
            terr_label = _badge(f"「{oname}」治下·买贵卖贱（×{terr_buy:g}/×{terr_sell:g}）", "danger")
            terr_label.setToolTip("商店所在地点被敌视你的势力占领：买入溢价、卖出压价。提升该势力声望（任务/击杀其敌人）可恢复原价。")
            row.addWidget(terr_label)
        # [修 2026-09-10] 稳定度 0 合法（夺城会把 stability 压到 0）：原 `or 50` 把 0 吞成
        # 常态档 -> 战后短缺横幅永不显示（守 §11 防 0 吞；引擎侧 stability_supply_level 本就正确）。
        stab = 50
        if loc is not None:
            _s = getattr(loc, "stability", None)
            if _s is not None:
                try:
                    stab = int(_s)
                except (TypeError, ValueError):
                    stab = 50
        if stab < 50:
            stab_label = _badge("战后短缺·补货受限" if stab < 25 else "物资紧张", "warn")
            stab_label.setToolTip(f"地点稳定度 {stab}/100：战乱压低民生，商人补货减少、系统货源枯竭。局势平息后逐日恢复。")
            row.addWidget(stab_label)
        if self.on_regen_request is not None:
            self.regen_btn = QPushButton("刷新商品")
            # [游商手动刷新 2026-09-09] 游商店走确定性本地补货（同步，零 LLM），
            # 真商人店保留原来走 LLM 换货——tooltip 按店类型区分。
            if self._is_vendor_shop():
                self.regen_btn.setToolTip("游商就地补货（本地规则进货+上架，立即完成，不调大模型）")
            else:
                self.regen_btn.setToolTip("让店主重新进货（调用大模型，可能需要数十秒）")
            self.regen_btn.clicked.connect(self._on_regen)
            row.addWidget(self.regen_btn)
        else:
            self.regen_btn = None
        return row

    # ============ 左：商店货架 ============
    def _build_shop_panel(self) -> QFrame:
        left = QFrame()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        hdr = QLabel(f"商店货架 · 买入（价 = {self._gt.currency}）")
        hdr.setStyleSheet("color:#7aa2f7; font-weight:bold;")
        hdr.setToolTip("店主的存货，选中后点「购买选中」付钱买入（受经济节奏与声望影响定价）。")
        ll.addWidget(hdr)
        self.shop_list = QListWidget()
        self.shop_list.itemClicked.connect(lambda *a: self._update_buttons())
        ll.addWidget(self.shop_list, 1)
        self.buy_btn = QPushButton("购买选中")
        self.buy_btn.setObjectName("primaryBtn")
        self.buy_btn.setEnabled(False)
        self.buy_btn.clicked.connect(self._on_buy)
        ll.addWidget(self.buy_btn)
        return left

    # ============ 右：玩家可售背包 ============
    def _build_inventory_panel(self) -> QFrame:
        right = QFrame()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        hdr = QLabel(f"你的背包 · 卖出（收购价 = {self._gt.currency}）")
        hdr.setStyleSheet("color:#9ece6a; font-weight:bold;")
        hdr.setToolTip("你随身携带的物品，选中后点「卖出选中」换钱（key 类任务道具不可卖；卖价约为基准价五折再乘经济节奏）。")
        rl.addWidget(hdr)
        self.inv_list = QListWidget()
        self.inv_list.itemClicked.connect(lambda *a: self._update_buttons())
        rl.addWidget(self.inv_list, 1)
        self.sell_btn = QPushButton("卖出选中")
        self.sell_btn.setStyleSheet("QPushButton{background:#1f3a2a; color:#9ece6a;}")
        self.sell_btn.setEnabled(False)
        self.sell_btn.clicked.connect(self._on_sell)
        rl.addWidget(self.sell_btn)
        return right

    # ============ 卡片 ============
    def _make_shop_card(self, entry, item: Item) -> RarityFrame:
        """货架卡：RarityFrame 品级边框 + [物品图 | 名字 + 价格/库存]，属性悬浮。"""
        from src.ui.widgets.item_brief import ItemBriefRow
        rf = RarityFrame(item.rarity)
        markup = tre.NIGHT_MARKUP if not tre.shop_open(self.world, self.shop) else 1.0
        # [P45(1)] 敌对治下溢价（与敲门溢价乘法叠加）
        terr_buy, _, _ = tre.territory_price_factors(self.world, self.shop, self.world.player)
        markup *= terr_buy
        # [产地系数] 产地直供折扣（本地资源点出产的货物便宜）
        prod_buy, _, prod_tag = tre.production_price_factors(self.world, self.shop, item)
        markup *= prod_buy
        # [物价联动 2026-09-10] 季节/财富乘区（与结算口径一致）
        mkt_buy, _, mkt_tags = tre.market_price_factors(self.world, self.shop, item)
        markup *= mkt_buy
        price = tre.buy_price(entry, item, self.shop, self._reputation(), self._pace,
                              markup, merchant_trust=self._merchant_trust())
        stock_txt = "无限" if entry.stock == -1 else f"库存 {entry.stock}"
        sub = f"{price} {self._gt.currency} | {stock_txt}"
        if not tre.shop_open(self.world, self.shop):
            sub += " | 夜间敲门价"
        if terr_buy > 1.0:
            sub += " | 敌对治下价"
        if prod_buy < 1.0:
            sub += f" | {prod_tag}"
        for t in mkt_tags:
            sub += f" | {t}"
        # [P47-C 赃物闭环 c] 认出自家失物：这件货在任何 NPC 的 stolen_ids 里 -> 标注
        if self._is_stolen_good(item.id):
            sub += " | ★你遗失之物（买回即赎回）"
        rf.addWidget(ItemBriefRow(self.world, item, sub_text=sub, gt=self._gt))
        return rf

    def _is_stolen_good(self, item_id: str) -> bool:
        """该货架物品是否是某个 NPC 从玩家手里抢走的（read-time 判断，集合极小）。"""
        for n in (self.world.npcs or []):
            if item_id in [str(i) for i in (getattr(n, "stolen_ids", None) or [])]:
                return True
        return False

    def _make_inv_card(self, item: Item) -> RarityFrame:
        """可售背包卡：RarityFrame + [物品图 | 名字 + 售价]，属性悬浮。"""
        from src.ui.widgets.item_brief import ItemBriefRow
        rf = RarityFrame(item.rarity)
        # [P45(1)] 敌对治下压价收购
        _, terr_sell, _ = tre.territory_price_factors(self.world, self.shop, self.world.player)
        # [产地系数] 异地稀缺溢价（别处产的货在此地能卖更高——跑商利润来源）
        _, prod_sell, prod_tag = tre.production_price_factors(self.world, self.shop, item)
        # [物价联动 2026-09-10] 季节/财富乘区（卖出镜像：穷 -> 压价，富 -> 抬价）
        _, mkt_sell, mkt_tags = tre.market_price_factors(self.world, self.shop, item)
        price = tre.sell_price(self.shop, item, self._reputation(), self._pace,
                               sell_factor=terr_sell * prod_sell * mkt_sell)
        sellable = item.type != "key"
        price_txt = f"售 {price} {self._gt.currency}" if sellable else "不可售"
        if sellable and terr_sell < 1.0:
            price_txt += " | 敌对治下压价"
        if sellable and prod_sell > 1.0:
            price_txt += f" | {prod_tag}"
        if sellable:
            for t in mkt_tags:
                price_txt += f" | {t}"
        rf.addWidget(ItemBriefRow(
            self.world, item, sub_text=price_txt, gt=self._gt))
        return rf

    # ============ 刷新 ============
    def _refresh(self):
        p = self.world.player
        self.gold_label.setText(f"{self._gt.currency}：{p.gold}")
        self.rep_label.setText(f"声望：{self._rep_tier()}")
        self.rep_label.style().unpolish(self.rep_label)
        self.rep_label.style().polish(self.rep_label)
        # 货架
        self.shop_list.clear()
        for entry in self.shop.stock:
            it = self._item(entry.item_id)
            if it is None or entry.stock == 0:
                continue
            wid = self._make_shop_card(entry, it)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, entry.item_id)
            item.setSizeHint(QSize(0, self._row_height(self.shop_list, wid)))
            self.shop_list.addItem(item)
            self.shop_list.setItemWidget(item, wid)
        # 玩家可售背包
        self.inv_list.clear()
        for iid in p.inventory:
            it = self._item(iid)
            if it is None:
                continue
            wid = self._make_inv_card(it)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, iid)
            item.setSizeHint(QSize(0, self._row_height(self.inv_list, wid)))
            self.inv_list.addItem(item)
            self.inv_list.setItemWidget(item, wid)
        self._update_buttons()

    def refresh_view(self):
        """[游商店实时回显 2026-09-10] 供外部通知重渲染：补货/换货改的就是本对话框
        持有的同一 world/shop 对象（ShopWorker 后台改 + manual_vendor_refresh 主线程
        改），数据已变——不重渲染货架要退出去重进才更新。"""
        self._refresh()

    def _row_height(self, lw, wid) -> int:
        # 行高实测 + 12px 下边距：heightForWidth 只量内容布局，不含 QSS 边框与
        # epic+ 流光外层柔光（paintEvent 画出 widget 边界），边距不足卡片底边
        # 被下一行遮（6px 实测仍裁，三次真人测试反馈加到 12）。
        wid_w = max(140, lw.viewport().width() - 12)
        if wid.hasHeightForWidth():
            return max(74, wid.heightForWidth(wid_w) + 12)
        return max(74, wid.sizeHint().height() + 12)

    def _update_buttons(self):
        self.buy_btn.setEnabled(self.shop_list.currentRow() >= 0)
        cur = self.inv_list.currentItem()
        sellable = False
        if cur is not None:
            it = self._item(cur.data(Qt.UserRole))
            sellable = it is not None and it.type != "key"
        self.sell_btn.setEnabled(sellable)

    # ============ 交易（纯代码，不经 LLM）============
    def _on_buy(self):
        cur = self.shop_list.currentItem()
        if cur is None:
            return
        it = self._item(cur.data(Qt.UserRole))
        if it is None:
            return
        # [P47-B 后果层] 记恨闸：门店商人 trust <= -60 -> 拒绝交易。
        # [!] 与 settle 路径（_resolve_trade buy 分支）同源同文案——两处必须一致，
        #     否则会出现「对话框不让买、自由键入却买成了」的口径分裂。
        _refuse = cue.merchant_refuses(self._merchant_npc())
        if _refuse:
            QMessageBox.information(self, "拒绝交易", _refuse)
            return
        rep = self._reputation()
        entry = self.shop.find_entry(it.id)
        # [P39c] 打烊时段敲门溢价（auction 交易所除外）；不拒客只加价
        markup = tre.NIGHT_MARKUP if not tre.shop_open(self.world, self.shop) else 1.0
        # [P45(1)] 敌对治下溢价（乘法叠加）+ [产地系数] 直供折扣
        terr_buy, _, terr_tag = tre.territory_price_factors(self.world, self.shop, self.world.player)
        markup *= terr_buy
        markup *= tre.production_price_factors(self.world, self.shop, it)[0]
        # [物价联动 2026-09-10] 季节/财富乘区（与货架显示口径一致）
        markup *= tre.market_price_factors(self.world, self.shop, it)[0]
        # 预算校验提示（友好反馈）；[修 2026-09-10] 含印象信任因子（与 tre.buy 实收价一致）
        price = tre.buy_price(entry, it, self.shop, rep, self._pace, markup,
                              merchant_trust=self._merchant_trust())
        if int(self.world.player.gold or 0) < price:
            QMessageBox.warning(self, "余额不足", f"需要 {price} {self._gt.currency}，你只有 {self.world.player.gold}。")
            return
        r = tre.buy(self.world.player, self.shop, it, rep, self._pace,
                    merchant=self.svc._shop_merchant(self.world, self.shop)
                    if getattr(self.svc, "_shop_merchant", None) else None,
                    markup=markup)
        if not r["ok"]:
            QMessageBox.information(self, "无法购买", r.get("reason", "未知原因"))
            return
        self._trades.append({"action": "buy", "name": it.name, "rarity": it.rarity, "price": r["price"]})
        # [P47-C 赃物闭环 c] 买回即赎回：从原持有者 stolen_ids 销账（「你遗失之物」闭环收口）
        _owner = cue.reclaim_via_purchase(self.world, it.id)
        if _owner:
            note += f"（这正是{_owner}从你那里抢走的东西——赎回了）"
        self._save_and_refresh()
        note = "（夜间敲门溢价）" if not tre.shop_open(self.world, self.shop) else ""
        if terr_buy > 1.0:
            note += f"（{terr_tag}治下溢价）"
        QMessageBox.information(self, "购买成功",
                                f"花费 {r['price']} {self._gt.currency} 购入 {it.name}。{note}")

    def _on_sell(self):
        cur = self.inv_list.currentItem()
        if cur is None:
            return
        it = self._item(cur.data(Qt.UserRole))
        if it is None:
            return
        rep = self._reputation()
        # [P45(1)] 敌对治下压价收购 + [产地系数] 异地稀缺
        _, terr_sell, terr_tag = tre.territory_price_factors(self.world, self.shop, self.world.player)
        _, prod_sell, prod_tag = tre.production_price_factors(self.world, self.shop, it)
        # [物价联动 2026-09-10] 季节/财富乘区（卖出镜像）
        _, mkt_sell, _ = tre.market_price_factors(self.world, self.shop, it)
        r = tre.sell(self.world.player, self.shop, it, rep, self._pace,
                     sell_factor=terr_sell * prod_sell * mkt_sell)
        if not r["ok"]:
            QMessageBox.information(self, "无法出售", r.get("reason", "未知原因"))
            return
        self._trades.append({"action": "sell", "name": it.name, "rarity": it.rarity, "price": r["price"]})
        self._save_and_refresh()
        note = f"（{terr_tag}治下压价）" if terr_sell < 1.0 else ""
        QMessageBox.information(self, "出售成功",
                                f"卖出 {it.name}，获得 {r['price']} {self._gt.currency}。{note}")

    def _on_regen(self):
        if self.on_regen_request is None:
            return
        # 交由场景页启动 ShopWorker（避免 dialog 内自管 worker 的生命周期复杂度）
        self.on_regen_request(self.shop.id)

    def _save_and_refresh(self):
        if self.storage is not None:
            self.shop.touch()
            self.storage.save_world(self.world)
        self._refresh()
        if self.on_changed:
            self.on_changed()

    # ============ [P14] 关店店主回应（仅真商人店） ============
    def _maybe_trade_reply(self):
        """关闭时若有交易 + 开关开 + 有服务：起后台 worker 出店主回应，toast 显示。

        worker 引用挂到 toast 宿主（场景页）上防 GC——dialog 即将销毁不能持有。
        [2026-09-09 用户指示] 游商店跳过：无真商人 NPC（merchant 解析为 None），
        “店主”只是题材称谓断，无口吻可演，每次关店一次 LLM 纯浪费。真商人店保留。
        """
        if (not self._trades or self.svc is None or self.preset is None
                or not getattr(self.preset, "trade_reply_enabled", True)):
            return
        try:
            _merchant = (self.svc._shop_merchant(self.world, self.shop)
                         if getattr(self.svc, "_shop_merchant", None) is not None
                         else self._merchant())
        except Exception:
            _merchant = None
        if _merchant is None:
            return  # 游商店：无店主，不调 LLM
        host = self.parentWidget() if self.parentWidget() is not None else None
        if host is None:
            return  # 无宿主（无场景页上下文）时跳过，不值得为此弹模态框
        w = _TradeReplyWorker(self.svc, self.world, self.shop, self._trades, self.preset, None)
        w.done_signal.connect(lambda line: self._show_toast(host, line))
        w.finished.connect(w.deleteLater)
        host._trade_reply_worker = w   # 防 GC；下一个 worker 会覆盖（旧的已 finished 自回收）
        w.start()

    @staticmethod
    def _show_toast(host, line: str):
        """店主回应以非模态 toast 浮在场景页顶部，6 秒后自毁（不打断游玩）。"""
        if host is None or not line:
            return
        merchant = None
        toast = QLabel(line, host)
        toast.setObjectName("tradeToast")
        toast.setWordWrap(True)
        toast.setStyleSheet(
            "QLabel{background:#1f2335; color:#c0caf5; border:1px solid #7aa2f7;"
            "border-radius:8px; padding:10px 14px; font-size:13px;}")
        toast.adjustSize()
        toast.setMinimumWidth(min(520, max(260, toast.width())))
        toast.move(16, 12)
        toast.show()
        toast.raise_()
        QTimer.singleShot(6000, toast.deleteLater)

    def accept(self):
        self._maybe_trade_reply()
        super().accept()

    def reject(self):
        self._maybe_trade_reply()
        super().reject()
