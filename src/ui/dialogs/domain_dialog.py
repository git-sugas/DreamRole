"""[D1+D2 2026-08-29] 据点页（DomainDialog）：购地 / 据点总览 / 资金池存取 / 建设与职员。

玩家据点经营：在野外之地斥巨资购地开辟私人据点（D1 基座）；D2 加设施建造（LLM 起名
挂真 Place，shop 类同步建真 Shop）/ 招募职员（骨架引擎落世 + LLM 档案批补）/ 挖角
（交情 >=60 + 签约金）/ 工资日结算（tick domain 阶段，欠薪 3 天离职）。
引擎结算全在 domain_engine（纯 Python），本对话框只做展示与调用；变更落 save_world +
changed 信号通知场景页刷新。购地/建造/招募的 LLM 小调用走 _DomainOpWorker 子线程
（守 §15 worker 生命周期：parent=None 自管 + dialog 持引用防 GC + done() 清理
disconnect/cancel/wait；同一时刻仅一个在跑操作，期间整窗禁用）。
「前往」不在本对话框内结算：发 travel_requested(domain_location_id) 信号 + accept
关窗，由场景页起 go_domain 叙事回合（不受相邻限制，消耗 1 回合）。
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QSize
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QMessageBox, QSpinBox, QComboBox, QInputDialog,
)

from src.models.world import NPC, effective_category
from src.services import domain_engine as de

# [P61 定策] 策略说明（domain 策略下拉 tooltip）
_POLICY_TIPS = {
    "": "无为而治：不额外干预经营（默认）",
    "output": "增产：伙计/农夫每日产出 +1 件",
    "military": "武备：打手历练成功率 +0.1（钳 0.9）",
    "quality": "重质：据点每日收入 +10%（精工细作出好价）",
}


class _DomainOpWorker(QThread):
    """据点操作后台任务（设施起名/职员档案等 LLM 小调用；购地已改同步自命名不走此 worker）。

    fn(cancel_check) -> (ok, payload)：成功 payload 为结果对象；失败 payload 为 err str。
    [!] 引擎结算都在子线程完成后一次性发生（LLM 失败/取消不扣费——招募例外：骨架已
    入职不回滚），主线程 finish 槽负责 save_world——world 是 Python 对象无 Qt 依赖。
    """

    finished_signal = Signal(bool, object)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self.fn = fn
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            ok, payload = self.fn(cancel_check=lambda: self._cancelled)
            self.finished_signal.emit(bool(ok), payload)
        except Exception as e:  # noqa: BLE001
            self.finished_signal.emit(False, f"操作失败：{e}")


class DomainDialog(QDialog):
    """[D1+D2] 据点管理页（单视图：购地区 + 据点卡列表 + 在据点管理区）。

    - 当前地点是野外（未购）-> 顶部出「购下此地」区（价格 = 20000 x 2^(危险度-1)）。
    - 每处据点一张卡（名/所在地/聚落档/居民上限/资金池/繁荣度）；不在据点时「前往」
      发 travel_requested，在据点时卡下展开管理区（资金池存取 + 设施 + 职员 + 招募 + 挖角）。
    """

    changed = Signal()                  # 购地/存取款/建造/招募/挖角后发（场景页刷新）
    travel_requested = Signal(str)      # 点「前往」发（据点地点 id），场景页起 go_domain 回合

    def __init__(self, world, world_sim_service, storage, preset, parent=None):
        super().__init__(parent)
        self.world = world
        self.svc = world_sim_service
        self.storage = storage
        self.preset = preset
        self._op_worker = None
        self._tab_index = 0             # 当前页签（_rebuild 重建后还原，操作不跳回总览）
        self.setWindowTitle("据点")
        self.resize(860, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        hint = QLabel("在野外之地购下一片基业：营门与广场立起，此地便成你的私人据点。"
                      "资金池供建设与工资周转；建商铺/岗哨/农田、招募职员，据点便有了人气。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(hint)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
        outer.addWidget(self.status_lbl)

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
        bottom.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        outer.addLayout(bottom)

        self._rebuild()

    # ---------- 生命周期（守 §15：accept/reject/关闭统一经 done() 清理 worker）----------
    def done(self, r):
        w = self._op_worker
        if w is not None:
            try:
                w.finished_signal.disconnect()
            except Exception:
                pass
            w.cancel()
            w.wait(3000)
            # [!] 不在此 deleteLater：LLM 长阻塞时线程可能仍在跑，销毁运行中 QThread 会
            # 崩溃——创建时已连 finished -> deleteLater 自清（跑完必清）。
            self._op_worker = None
        super().done(r)

    # ---------- 便捷读取 ----------
    def _gt(self):
        try:
            return self.svc._genre_text(self.world)
        except Exception:
            from src.models.world_sim_preset import GenreText
            return GenreText(self.world.config_overlay if isinstance(self.world.config_overlay, dict) else {})

    def _cur(self):
        loc = self.svc._current_location(self.world)
        return loc, de.player_domain_at(self.world, loc)

    def _loc(self, loc_id: str):
        return next((l for l in self.world.locations if l.id == loc_id), None)

    def _npc(self, npc_id: str):
        return next((n for n in self.world.npcs if n.id == npc_id), None)

    def _item(self, item_id: str):
        return next((i for i in self.world.items if i.id == item_id), None)

    # ---------- 渲染 ----------
    def _insert_section(self, text: str):
        l = QLabel(text)
        l.setStyleSheet("color:#7dcfff; font-size:13px; font-weight:bold;")
        # [!] 一律 insertWidget(count-1) 插在末尾 stretch 之前（守 home_dialog 口径）：
        # addWidget 会追加到 stretch 之后，_rebuild 的 takeAt(0) 循环保留末项的假设即失效
        # （内容沉底 + 每轮 rebuild 残留一张陈旧卡片）。
        self.body.insertWidget(self.body.count() - 1, l)

    def _insert_empty(self, text: str):
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet("color:#565f89; font-size:12px;")
        self.body.insertWidget(self.body.count() - 1, l)

    def _rebuild(self):
        # [!] 保持滚动位置（守 §15 全量重建式对话框契约）：存取款/购地/建造/招募重建后
        # 滚动条不跳底——记旧值，布局完成后还原。
        sb = self._scroll.verticalScrollBar()
        old_pos = sb.value()
        while self.body.count() > 1:  # 末尾 stretch 保留
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                # [!] setParent(None) 先于 deleteLater：立即脱离父级（否则 deleteLater
                # 排队期间 findChildren/渲染仍见陈旧卡片，同 home_dialog 口径）。
                w.setParent(None)
                w.deleteLater()
        gt = self._gt()
        loc, _ = self._cur()

        # ---- 购地区（当前是未购野外之地才有）----
        if loc is not None and getattr(loc, "kind", "") == "wilderness" \
                and not getattr(loc, "player_owned", False):
            self._insert_buy_section(loc, gt)

        # ---- 据点卡列表 ----
        self._insert_section(f"我的据点（{len(self.world.domains)}）")
        if not self.world.domains:
            self._insert_empty("尚无据点。亲至一片野外之地，即可在此页购地开辟。")
        for dm in self.world.domains:
            self._insert_domain_card(dm, loc, gt)
        # 布局完成后还原滚动位置（singleShot 等 sizeHint 生效；内容变短时自动钳回合法范围）
        QTimer.singleShot(0, lambda: sb.setValue(old_pos))

    def _insert_buy_section(self, loc, gt):
        price = de.domain_price(loc)
        card = QFrame()
        card.setObjectName("gameCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        title = QLabel(f"购下「{loc.name}」")
        title.setStyleSheet("color:#e0af68; font-size:14px; font-weight:bold;")
        lay.addWidget(title)
        desc = QLabel(
            f"{loc.desc or '一片野外之地'}（危险度 {loc.danger}）\n"
            f"购地价：{price} {gt.currency}（持有 {self.world.player.gold} {gt.currency}）\n"
            f"购下后此地成为你的私人据点：聚落初立（村庄档，居民上限 "
            f"{de.resident_limit('village')}），营门与营地广场立起，此地以据点名见于地图。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#c0caf5; font-size:12px;")
        lay.addWidget(desc)
        buy_btn = QPushButton("购下此地（开辟据点）")
        buy_btn.setObjectName("primaryBtn")
        buy_btn.setEnabled(int(self.world.player.gold) >= price)
        if not buy_btn.isEnabled():
            _gt_cur = self._gt().currency  # §23 币种走 GenreText
            buy_btn.setToolTip(f"{_gt_cur}不足——购地是天价买卖，价格随危险度翻倍")
        buy_btn.clicked.connect(lambda _=False, l=loc: self._on_buy(l))
        lay.addWidget(buy_btn)
        self.body.insertWidget(self.body.count() - 1, card)

    def _insert_domain_card(self, dm, cur_loc, gt):
        dloc = self._loc(dm.location_id)
        here = cur_loc is not None and cur_loc.id == dm.location_id
        card = QFrame()
        card.setObjectName("gameCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        size = getattr(dloc, "settlement_size", "") or "village"
        title = QLabel(f"「{dm.name}」{'（在此）' if here else ''}")
        title.setStyleSheet("color:#7aa2f7; font-size:14px; font-weight:bold;")
        lay.addWidget(title)
        stats = QLabel(
            f"所在地：{dloc.name if dloc else '未知之地'}（{self._size_label(size)}，"
            f"居民上限 {de.resident_limit(size)}）\n"
            f"资金池：{dm.funds} {gt.currency} ｜ 繁荣度：{dm.prosperity}/100"
            f" ｜ 职员/居民：{len(de.roster_of(dm))}"
            + (f"\n{dm.desc}" if dm.desc else ""))
        stats.setWordWrap(True)
        stats.setStyleSheet("color:#c0caf5; font-size:12px;")
        lay.addWidget(stats)
        if not here:
            row = QHBoxLayout()
            go_btn = QPushButton("前往据点")
            go_btn.setToolTip("动身返回据点（不受相邻限制，消耗 1 回合世界演化）")
            go_btn.clicked.connect(lambda _=False, lid=dm.location_id: self._on_travel(lid))
            row.addStretch()
            row.addWidget(go_btn)
            lay.addLayout(row)
        else:
            lay.addWidget(self._manage_panel(dm, gt))
        self.body.insertWidget(self.body.count() - 1, card)

    @staticmethod
    def _size_label(size: str) -> str:
        return {"village": "村庄", "town": "城镇", "city": "城市"}.get(size, "村庄")

    # ---------- 管理区（在据点时）----------
    def _mat_counts(self) -> dict:
        """背包按类别的材料计数（建造需求校验用）。"""
        item_by_id = {it.id: it for it in self.world.items}
        counts = {"forge": 0, "craft": 0}
        for iid in (self.world.player.inventory or []):
            it = item_by_id.get(iid)
            if it is None:
                continue
            c = effective_category(it)
            if c in counts:
                counts[c] += 1
        return counts

    def _manage_panel(self, dm, gt) -> QWidget:
        """在据点的管理区（页签化 2026-09-07 用户指示：单页长卷改 QTabWidget 五页）。

        页签：总览（指标卡 + 存取 + 升格 + 防务）/ 建设（设施建造）
        / 人员（职员招募 + 挖角）/ 仓库（存取 + 产线）/ 委托（委托板）。
        各面板函数原样复用，只改装配方式（守「不动结算/动作逻辑」）。
        """
        from PySide6.QtWidgets import QTabWidget as _QTW
        tabs = _QTW()
        tabs.setObjectName("domain_tabs")
        tabs.setDocumentMode(True)
        # 总览页自带滚动（指标 + 存取 + 升格 + 防务一屏放不下时可滚）
        tabs.addTab(self._wrap_tab_scroll(self._overview_panel(dm, gt)), "总览")
        tabs.addTab(self._wrap_tab_scroll(self._facility_panel(dm, gt)), "建设")
        staff_tab = QWidget()
        staff_lay = QVBoxLayout(staff_tab)
        staff_lay.setContentsMargins(0, 0, 0, 0)
        staff_lay.setSpacing(8)
        staff_lay.addWidget(self._staff_panel(dm, gt))
        staff_lay.addWidget(self._poach_panel(dm, gt))
        tabs.addTab(self._wrap_tab_scroll(staff_tab), "人员")
        tabs.addTab(self._wrap_tab_scroll(self._warehouse_panel(dm, gt)), "仓库")
        tabs.addTab(self._wrap_tab_scroll(self._commission_panel(dm, gt)), "委托")
        # [页签图标 2026-09-07 用户指示] 主流经营 UI 口径：页签带小图标。
        # 纯 Qt 标准图标（零新资源）：总览=信息、建设=建造、人员=人像、
        # 仓库=存档盒、委托=文档——跨平台风格统一，无图资源依赖。
        from PySide6.QtWidgets import QStyle as _QS
        style = self.style()
        tabs.setTabIcon(0, style.standardIcon(_QS.SP_MessageBoxInformation))
        tabs.setTabIcon(1, style.standardIcon(_QS.SP_FileDialogNewFolder))
        tabs.setTabIcon(2, style.standardIcon(_QS.SP_DialogYesButton))
        tabs.setTabIcon(3, style.standardIcon(_QS.SP_DriveHDIcon))
        tabs.setTabIcon(4, style.standardIcon(_QS.SP_FileIcon))
        tabs.setIconSize(QSize(16, 16))
        # [页签记忆 2026-09-07 用户指示] _after_change 会整窗 _rebuild，页签重建后
        # 恢复操作前的页（否则每次存取/建造/招募都跳回总览）；切换时记最新值。
        tabs.setCurrentIndex(max(0, min(4, int(getattr(self, "_tab_index", 0) or 0))))
        tabs.currentChanged.connect(self._on_tab_changed)
        return tabs

    def _on_tab_changed(self, idx: int):
        self._tab_index = int(idx or 0)

    @staticmethod
    def _wrap_tab_scroll(page: QWidget) -> QWidget:
        """页签内容包一层滚动（守 §15 单页内容多用 QScrollArea 兜底口径）。"""
        from PySide6.QtWidgets import QScrollArea as _QSA
        sc = _QSA()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(page)
        return sc

    def _overview_panel(self, dm, gt) -> QWidget:
        """[总览 2026-09-07] 关键指标卡：五个数字 + 一句盈亏提示，先知道赚没赚。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)
        # ---- 指标卡 ----
        card = QFrame()
        card.setObjectName("gameCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(4)
        bill = dict(dm.last_bill or {})
        net = int(bill.get("net", 0) or 0)
        net_txt = f"{net:+d}" if net != 0 else "0"
        if bill:
            verdict = ("昨日盈利" if net > 0 else ("昨日持平" if net == 0 else "昨日亏损"))
        else:
            verdict = "尚未开张结算"
        dloc = self._loc(dm.location_id)
        wh_len = len(getattr(dm, "warehouse", None) or [])
        wh_cap = de.warehouse_cap(getattr(dloc, "settlement_size", "") or "") if dloc else 0
        title = QLabel(f"「{dm.name}」{verdict}（昨日净额 {net_txt} {gt.currency}）")
        title.setStyleSheet("color:#7aa2f7; font-size:14px; font-weight:bold;")
        title.setWordWrap(True)
        cl.addWidget(title)
        grid = QLabel(
            f"资金池 {dm.funds} {gt.currency} ｜ 繁荣度 {dm.prosperity}/100\n"
            f"职员 {len(de.roster_of(dm))} 人 ｜ 仓库 {wh_len}/{wh_cap}\n"
            f"累计访客 {dm.visitors_total} ｜ 累计营收 {dm.income_total} {gt.currency}")
        grid.setWordWrap(True)
        grid.setStyleSheet("color:#c0caf5; font-size:13px;")
        cl.addWidget(grid)
        detail = QLabel(
            f"昨日账单：客流 {bill.get('flow', 0)} ｜ 营收 +{bill.get('income', 0)} ｜ "
            f"工资 -{bill.get('wages', 0)} ｜ 维护 -{bill.get('maintenance', 0)}"
            if bill else "每日自动结算：营业收入 - 工资 - 维护 入资金池。")
        detail.setWordWrap(True)
        detail.setStyleSheet("color:#9aa5ce; font-size:11px;")
        detail.setToolTip("客流 = 20 x 聚落档(1.0/1.8/3.0) x (1+繁荣%) x 声望 x 竞争(相邻聚落分流)"
                          "；人均消费随设施数 +15%；掌柜在职才全额营业（否则 2 成路人生意）。"
                          "相邻地点 NPC 还会慕名上门真实购物（其钱包扣款、货架出货）。")
        cl.addWidget(detail)
        lay.addWidget(card)
        # ---- 存取 + 欠薪警告（原 _funds_panel 内联，不再独立成区）----
        lay.addWidget(self._funds_panel(dm, gt))
        # ---- 升格 + 防务（原 _ops_panel 尾部两段内联，不再独立成区）----
        lay.addWidget(self._tier_defense_panel(dm, gt))
        return w

    def _tier_defense_panel(self, dm, gt) -> QWidget:
        """升格入口 + 防务行（从旧 _ops_panel 尾部拆出，归总览页）。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        gp = de.guard_power(self.world, dm)
        cd = de.city_defense(self.world, dm)
        guard_row = QLabel(f"护卫战力 {gp} ｜ 城防 {cd}（快防 = 两者之和；打手每日历练成长，"
                           f"敌对势力夜袭由快防结算）")
        guard_row.setWordWrap(True)
        guard_row.setStyleSheet("color:#9aa5ce; font-size:12px;")
        lay.addWidget(guard_row)
        info = de.next_tier_info(self.world, dm)
        if info is not None:
            nxt, price, pros_need = info
            row = QHBoxLayout()
            tier_zh = {"village": "村庄", "town": "城镇", "city": "城市"}
            info_l = QLabel(f"升格{tier_zh.get(nxt, nxt)}（居民上限 {de.resident_limit(nxt)}）"
                            f"：{price} {gt.currency} + 繁荣度 >= {pros_need}")
            info_l.setStyleSheet("color:#9aa5ce; font-size:12px;")
            row.addWidget(info_l, 1)
            btn = QPushButton("升格")
            btn.setEnabled(int(dm.funds) >= price and int(dm.prosperity) >= pros_need)
            if not btn.isEnabled():
                btn.setToolTip("资金池或繁荣度未达门槛（繁荣度靠每日账单净额盈余攒）")
            btn.clicked.connect(lambda _=False, d=dm: self._on_upgrade(d))
            row.addWidget(btn)
            lay.addLayout(row)
        else:
            cap = QLabel("已是最高聚落档（城市，居民上限 20）")
            cap.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(cap)
        return w

    def _ops_panel(self, dm, gt) -> QWidget:
        """[D5 旧入口·已并入总览] 保留防误删/旧测试引用：内容 = 指标卡精简版。

        总览页用 _overview_panel（含指标卡+存取+升格+防务）；本函数仅供不经过
        _manage_panel 的旧调用兜底（当前无生产调用）。"""
        return self._tier_defense_panel(dm, gt)

    def _warehouse_panel(self, dm, gt) -> QWidget:
        """[据点仓库 + 美化 2026-09-07 用户指示] 仓储区：计数 + 两栏物品行（仿背包口径）。

        仓中栏 / 背包栏：每件一行 = [图标 | 名字·品级]（RarityFrame 品级边框 + tooltip
        全属性，无图首字占位）+ 行尾存/取按钮。双击行也可存取。
        """
        from src.ui.widgets.rarity_frame import RarityFrame
        from src.ui.widgets.item_brief import (full_item_pixmap, display_name,
                                               kind_label)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel("仓库")
        head.setStyleSheet("color:#9ece6a; font-size:13px; font-weight:bold;")
        lay.addWidget(head)
        dloc = self._loc(dm.location_id)
        size = (getattr(dloc, "settlement_size", "") or "village") if dloc else "village"
        cap = de.warehouse_cap(size)
        wh = list(getattr(dm, "warehouse", None) or [])
        body = QLabel(
            f"仓中 {len(wh)}/{cap}（不占负重；升格聚落档扩容）\n"
            f"产线：伙计每日采 5 件白绿材料 / 农夫每日收 5 件白绿作物入仓；"
            f"掌柜每日以仓中材料制货（蓝封顶配方、日 3 件），按订单送货架、据点仓库或同地住宅仓库。")
        body.setWordWrap(True)
        body.setStyleSheet("color:#c0caf5; font-size:12px;")
        lay.addWidget(body)
        # ---- [P48 产线] 发布订单 + 在制订单（定向采料/按单合成/完工上架或入仓）----
        active_n = len(de._order_active(dm))
        lay.addWidget(self._wh_section_label(
            f"产线订单（{active_n}/{de._ORDER_MAX_OPEN} 在制）"))
        order_rows = de.order_view(self.world, dm)
        if not order_rows:
            note = QLabel("暂无订单。选配方、数量和去向后发布，职员会按单备料与制造。")
            note.setWordWrap(True)
            note.setStyleSheet("color:#9aa5ce; font-size:12px;")
            lay.addWidget(note)
        for o in order_rows:
            state = str(o.get("state", ""))
            dest = {"shelf": "货架", "warehouse": "据点仓库", "home": "住宅仓库"}.get(
                o.get("ship"), "仓库")
            if state in ("open", "stalled"):
                condition = (o.get("blocked") or
                             (f"缺：{'、'.join(o['lacks'])}" if o.get("lacks")
                              else "料齐，掌柜赶工中"))
                txt = (f"[{o.get('progress_txt')}] {o.get('out_name', '?')} -> "
                       f"{dest}｜{condition}")
                if state == "stalled":
                    txt += "｜停工待料，补齐后自动复工"
            elif state == "done":
                txt = f"[完成] {o.get('out_name', '?')} ×{o.get('qty', 1)}"
            else:
                txt = f"[{state or '未知'}] {o.get('out_name', '?')}"
            row = QFrame()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 2, 8, 2)
            rl.addWidget(QLabel(txt), 1)
            if state in ("open", "stalled"):
                btn = QPushButton("撤单")
                btn.setFixedWidth(52)
                oid = str(o.get("id", ""))

                def _cancel(oid=oid):
                    de.cancel_order(dm, oid)
                    self._after_change("已撤单（定向采料停止，已出的货不回收）")

                btn.clicked.connect(_cancel)
                rl.addWidget(btn)
            row.setStyleSheet("QFrame{background:#1a1b26; border-radius:4px;}"
                              "QLabel{color:#c0caf5; font-size:12px;}")
            lay.addWidget(row)
        # 发布表单：配方（可造的）+ 数量 + 去向
        recipes = [r for r in (self.world.recipes or [])
                   if not getattr(r, "required_building", "")
                   and max(1, int(getattr(r, "output_qty", 1) or 1)) == 1]
        item_by_id = {it.id: it for it in (self.world.items or [])}
        from src.services.world_sim_service import _auction_lot_ids
        _auc = _auction_lot_ids(self.world)
        ok_recipes = [r for r in recipes
                      if getattr(item_by_id.get(str(r.output_item_id)), "rarity", "")
                      in ("common", "uncommon", "rare") and r.output_item_id not in _auc]
        if ok_recipes:
            form = QHBoxLayout()
            form.setContentsMargins(8, 2, 8, 2)
            self._order_recipe = QComboBox()
            from collections import Counter
            for r in ok_recipes:
                nm = getattr(item_by_id.get(str(r.output_item_id)), "name",
                             r.output_item_id)
                mats = Counter(str(i) for i in (r.inputs or []))
                names = "、".join(
                    f"{getattr(item_by_id.get(iid), 'name', iid)} ×{count}"
                    for iid, count in mats.items()) or "无需材料"
                self._order_recipe.addItem(f"{nm}（{names}）", str(r.id))
            qty = QSpinBox()
            qty.setRange(1, 6)
            qty.setValue(2)
            ship = QComboBox()
            ship.addItem("出货到货架", "shelf")
            ship.addItem("存入据点仓库", "warehouse")
            if de._order_home(self.world, dm) is not None:
                ship.addItem("送到同地住宅仓库", "home")

            def _post():
                rid = self._order_recipe.currentData()
                okk, errr = de.post_order(self.world, dm, rid, qty.value(),
                                          ship.currentData())
                if not okk:
                    QMessageBox.information(self, "发布失败", errr)
                else:
                    self._after_change("产线订单已发布：职员定向备料，掌柜按单赶工；断料补齐后自动复工")

            post_btn = QPushButton("发布订单")
            post_btn.setEnabled(active_n < de._ORDER_MAX_OPEN)
            if not post_btn.isEnabled():
                post_btn.setToolTip("在制订单已满；等一单完工或撤单后可继续发布。")
            post_btn.clicked.connect(_post)
            form.addWidget(self._order_recipe, 1)
            form.addWidget(QLabel("×"))
            form.addWidget(qty)
            form.addWidget(ship)
            form.addWidget(post_btn)
            lay.addLayout(form)
            if active_n >= de._ORDER_MAX_OPEN:
                tip = QLabel("在制订单已满；等一单完工或撤单后可继续发布。")
                tip.setWordWrap(True)
                tip.setStyleSheet("color:#e0af68; font-size:12px;")
                lay.addWidget(tip)
        else:
            tip = QLabel("当前没有适合掌柜的配方：产线只接单件产出、无需住宅工坊的白绿蓝配方。")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#9aa5ce; font-size:12px;")
            lay.addWidget(tip)
        if not wh and not self.world.player.inventory:
            empty = QLabel("仓库与背包都是空的。")
            empty.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(empty)
            return w
        # 仓中栏（出仓）
        if wh:
            lay.addWidget(self._wh_section_label(f"仓中（{len(wh)}）"))
            for iid in wh:
                it = self._item(iid)
                if it is None:
                    continue
                lay.addWidget(self._wh_item_row(dm, it, gt, take=True))
        # 背包栏（入仓）
        inv = list(self.world.player.inventory or [])
        if inv:
            lay.addWidget(self._wh_section_label(f"背包（{len(inv)}）"))
            for iid in inv:
                it = self._item(iid)
                if it is None:
                    continue
                lay.addWidget(self._wh_item_row(dm, it, gt, take=False))
        return w

    @staticmethod
    def _wh_section_label(text: str) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet("color:#7dcfff; font-size:12px; font-weight:bold;")
        return l

    def _wh_item_row(self, dm, it, gt, take: bool) -> QFrame:
        """仓库物品行：RarityFrame 品级边框 + [图标 | 名字·品级] + 存/取按钮。

        口径仿 inventory_dialog._make_item_widget（图标 40 + 品级色名 + 类别小字 +
        item_tooltip 全属性悬浮，含未鉴定打码）；take=True 出仓 / False 入仓。
        """
        from src.ui.widgets.rarity_frame import RarityFrame
        from src.ui.widgets.item_brief import (full_item_pixmap, display_name,
                                               kind_label, item_tooltip,
                                               _RARITY_COLOR)
        rf = RarityFrame(it.rarity)
        row = QHBoxLayout()
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(8)
        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(full_item_pixmap(it.name, getattr(it, "icon", "") or "", 40))
        row.addWidget(icon, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        name = QLabel(display_name(it))
        color = _RARITY_COLOR.get(it.rarity, "#c0caf5")
        name.setStyleSheet(f"color:{color}; font-weight:bold;")
        name.setWordWrap(True)
        col.addWidget(name)
        sub = QLabel(f"{gt.rarity(it.rarity)} · {kind_label(self.world, it, gt)}")
        sub.setStyleSheet("color:#9aa5ce; font-size:11px;")
        col.addWidget(sub)
        row.addLayout(col, 1)
        btn = QPushButton("← 出仓" if take else "入仓 →")
        btn.setToolTip("取出进背包（负重校验）" if take else "存入仓库（满仓拒收）")
        if take:
            btn.clicked.connect(lambda _=False, d=dm, i=it.id: self._on_wh_take(d, i))
        else:
            btn.clicked.connect(lambda _=False, d=dm, i=it.id: self._on_wh_put(d, i))
        row.addWidget(btn)
        rf.addLayout(row)
        rf.setToolTip(item_tooltip(self.world, it, gt))
        rf.setToolTipDuration(10000)
        return rf

    def _on_wh_take(self, dm, item_id: str):
        if not item_id:
            return
        it = self._item(item_id)
        if not de.warehouse_withdraw(self.world, dm, item_id):
            QMessageBox.warning(self, "无法出仓", "背包已满（负重上限），先整理背包或入仓其他物品。")
            return
        self._after_change(f"已出仓：{it.name if it else item_id}")

    def _on_wh_put(self, dm, item_id: str):
        if not item_id:
            return
        it = self._item(item_id)
        if not de.warehouse_deposit(self.world, dm, item_id):
            QMessageBox.warning(self, "无法入仓", "仓库已满（升格聚落档可扩容）。")
            return
        self._after_change(f"已入仓：{it.name if it else item_id}")

    def _funds_panel(self, dm, gt) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("金额："))
        self._amount_spin = QSpinBox()
        self._amount_spin.setRange(1, 999_999_999)
        self._amount_spin.setValue(100)
        row.addWidget(self._amount_spin, 1)
        dep_btn = QPushButton("存入资金池")
        dep_btn.clicked.connect(lambda _=False, d=dm: self._on_deposit(d))
        row.addWidget(dep_btn)
        wd_btn = QPushButton("取出")
        wd_btn.clicked.connect(lambda _=False, d=dm: self._on_withdraw(d))
        row.addWidget(wd_btn)
        lay.addLayout(row)
        if int(dm.unpaid_days) > 0:
            warn = QLabel(f"[!] 已连续欠薪 {dm.unpaid_days} 天——发不出工资满 3 天职员将集体出走"
                          f"（今日工资合计见职员区，及时存入资金池）。")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#e05555; font-size:12px;")
            lay.addWidget(warn)
        return w

    def _facility_panel(self, dm, gt) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel("设施")
        head.setStyleSheet("color:#9ece6a; font-size:13px; font-weight:bold;")
        lay.addWidget(head)
        mats = self._mat_counts()
        mat_txt = "、".join(f"{zh} ×{n}" for (cat, n), zh in
                            zip(de._BUILD_MATS, ("锻造类", "制造类")))
        for entry in de.facility_catalog(self.world):
            kind = entry.get("kind", "")
            type_zh = entry.get("type") or kind
            built = de.facility_of(dm, kind)
            row = QHBoxLayout()
            row.setSpacing(8)
            if built is not None:
                chip = QLabel(f"{type_zh}「{built.get('name', '')}」（已建成）")
                chip.setStyleSheet("color:#9ece6a; font-size:12px;")
                row.addWidget(chip, 1)
            else:
                price = de.facility_price(self.world, entry)
                info = QLabel(f"{type_zh}：{entry.get('desc', '')}\n"
                              f"建造价 {price} {gt.currency}（资金池 {dm.funds}）+ 材料 {mat_txt}"
                              f"（背包：锻造类 {mats['forge']} / 制造类 {mats['craft']}）")
                info.setWordWrap(True)
                info.setStyleSheet("color:#c0caf5; font-size:12px;")
                row.addWidget(info, 1)
                btn = QPushButton("建造")
                affordable = int(dm.funds) >= price \
                    and mats["forge"] >= de._BUILD_MATS[0][1] \
                    and mats["craft"] >= de._BUILD_MATS[1][1]
                btn.setEnabled(affordable)
                if not affordable:
                    btn.setToolTip("资金池或背包材料不足")
                btn.clicked.connect(lambda _=False, k=kind: self._on_build(dm, k))
                row.addWidget(btn)
            lay.addLayout(row)
        return w

    def _on_policy_changed(self, dm):
        """[P61 定策] 切换经营策略：引擎白名单校验 + 落盘重建（combo 信号带 dm 引用）。"""
        if dm is None or self._policy_combo is None:
            return
        key = str(self._policy_combo.currentData() or "")
        ok, err = de.set_policy(dm, key)
        if not ok:
            QMessageBox.warning(self, "经营策略", err)
            return
        self.storage.save_world(self.world)
        self._policy_combo.setToolTip(_POLICY_TIPS.get(key, ""))
        self._rebuild()

    def _staff_panel(self, dm, gt) -> QWidget:
        """职员与招募（美化 2026-09-07 用户指示）：每行 = 头像 + 信息 + 档案按钮。"""
        from src.ui.widgets.avatar_button import make_avatar_pixmap
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel("职员与招募")
        head.setStyleSheet("color:#9ece6a; font-size:13px; font-weight:bold;")
        lay.addWidget(head)
        roster = de.roster_of(dm)
        wages = 0
        if not roster:
            tip = QLabel("尚无职员。建好设施后即可招募（掌柜/伙计驻商铺，打手驻岗哨，农夫驻农田）。")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(tip)
        else:
            for r in roster:
                npc = self._npc(str(r.get("npc_id") or ""))
                if npc is None:
                    continue
                wage = de.daily_wage_of(npc)
                wages += wage
                row = QFrame()
                row.setObjectName("gameCard")
                rl = QHBoxLayout(row)
                rl.setContentsMargins(8, 6, 8, 6)
                rl.setSpacing(8)
                av = QLabel()
                av.setFixedSize(40, 40)
                av.setPixmap(make_avatar_pixmap(
                    npc.name, getattr(npc, "avatar", "") or "", 40))
                av.setStyleSheet("border-radius:20px; border:1px solid #2a2e44;")
                rl.addWidget(av)
                info = QLabel(
                    f"{npc.name}（{de.ROLE_LABELS.get(r.get('role', ''), '职员')}"
                    f" {npc.level}级·日薪 {wage} {gt.currency}）{npc.role or ''}"
                    + (f"｜专精：{r.get('specialty')}" if r.get("specialty") else ""))
                info.setWordWrap(True)
                info.setStyleSheet("color:#c0caf5; font-size:12px;")
                rl.addWidget(info, 1)
                arc = QPushButton("档案")
                arc.setToolTip(f"查看 {npc.name} 的档案（住址/装备/背包/记忆）")
                arc.clicked.connect(lambda _=False, n=npc: self._open_npc_detail(n))
                rl.addWidget(arc)
                lay.addWidget(row)
            total = QLabel(f"今日工资合计 {wages} {gt.currency}（每日自动从资金池发薪入职员钱袋）")
            total.setStyleSheet("color:#9aa5ce; font-size:12px;")
            lay.addWidget(total)
        # [P61 定策] 经营策略（一键一策：增产/武备/重质/无为）
        prow = QHBoxLayout()
        prow.setSpacing(6)
        plabel = QLabel("经营策略：")
        plabel.setStyleSheet("color:#9aa5ce; font-size:12px;")
        prow.addWidget(plabel)
        self._policy_combo = QComboBox()
        for key, label in de._POLICY_LABELS.items():
            self._policy_combo.addItem(label, key)
            if str(getattr(dm, "policy", "") or "") == key:
                self._policy_combo.setCurrentIndex(self._policy_combo.count() - 1)
        cur_key = str(getattr(dm, "policy", "") or "")
        self._policy_combo.setToolTip(_POLICY_TIPS.get(cur_key, ""))
        self._policy_combo.currentIndexChanged.connect(
            lambda _i, d=dm: self._on_policy_changed(d))
        prow.addWidget(self._policy_combo, 1)
        lay.addLayout(prow)
        # 招募按钮行（按职业门控提示）
        row = QHBoxLayout()
        row.setSpacing(6)
        for role_key, label in de.ROLE_LABELS.items():
            ok, err = de.role_available(self.world, dm, role_key)
            btn = QPushButton(f"招募{label}")
            btn.setEnabled(ok and self._op_worker is None)
            btn.setToolTip(err if not ok else f"招募一位新{label}（档案自动生成，日薪 2×等级）")
            btn.clicked.connect(lambda _=False, rk=role_key: self._on_hire(dm, rk))
            row.addWidget(btn)
        lay.addLayout(row)
        return w

    def _commission_panel(self, dm, gt) -> QWidget:
        """[D6] 委托板：资金池出收购委托（押金扣押），附近 NPC 觉得划算会接单，
        次日把货送进商铺货架领报酬；过期押金退回。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel("委托板")
        head.setStyleSheet("color:#9ece6a; font-size:13px; font-weight:bold;")
        lay.addWidget(head)
        if de.facility_of(dm, "shop") is None:
            tip = QLabel("建好商铺后可挂收购委托：附近 NPC 觉得报酬划算会接单办货，次日送进货架。")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(tip)
            return w
        # 发布表单：物品 + 数量 + 报酬 + 挂单天数
        from PySide6.QtWidgets import QComboBox, QSpinBox as _SB
        form = QHBoxLayout()
        form.setSpacing(6)
        combo = QComboBox()
        # [拍品隔离 2026-09-09] 委托候选排除拍卖拍品（引擎 post_commission 亦拦，双保险）。
        from src.services.world_sim_service import _auction_lot_ids
        _auc = _auction_lot_ids(self.world)
        sellable = [it for it in self.world.items
                    if getattr(it, "type", "") != "key"
                    and it.id not in _auc][:200]
        for it in sellable:
            combo.addItem(f"{it.name}（建议 {de.suggested_reward(self.world, it, 1)}）",
                          it.id)
        form.addWidget(combo, 2)
        qty_spin = _SB()
        qty_spin.setRange(1, de._COMMISSION_QTY_MAX)
        qty_spin.setValue(1)
        qty_spin.setToolTip("收购数量（1-3）")
        form.addWidget(qty_spin)
        reward_spin = _SB()
        reward_spin.setRange(1, 999_999)
        reward_spin.setValue(100)
        reward_spin.setToolTip("报酬（发布即从资金池扣押，过期退回；NPC 心里的值 = 货价 x 数量 x 1.3）")
        form.addWidget(reward_spin)
        days_spin = _SB()
        days_spin.setRange(1, de._COMMISSION_DAYS_MAX)
        days_spin.setValue(2)
        days_spin.setToolTip("挂单天数（超期无人接/未送达即退款）")
        form.addWidget(days_spin)
        post_btn = QPushButton("挂单")
        post_btn.setObjectName("primaryBtn")
        post_btn.setEnabled(int(dm.funds) >= int(reward_spin.value()))
        post_btn.setToolTip("发布收购委托（报酬从资金池扣押，交付入货架，过期退款）")
        post_btn.clicked.connect(
            lambda _=False, d=dm, cb=combo, qs=qty_spin, rs=reward_spin, ds=days_spin:
            self._on_post_commission(d, cb, qs, rs, ds))
        form.addWidget(post_btn)
        lay.addLayout(form)
        # 挂单列表（美化 2026-09-07 用户指示）：每行 = 物品图标 + 信息卡片。
        from src.ui.widgets.item_brief import full_item_pixmap
        state_zh = {"open": "待接", "taken": "承接中", "done": "已交付", "expired": "已过期退款"}
        state_color = {"open": "#9ece6a", "taken": "#7aa2f7",
                       "done": "#565f89", "expired": "#e05555"}
        for c in (dm.commissions or []):
            it = self._item(str(c.get("item_id") or ""))
            row = QFrame()
            row.setObjectName("gameCard")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 6, 8, 6)
            rl.setSpacing(8)
            icon = QLabel()
            icon.setFixedSize(40, 40)
            icon.setAlignment(Qt.AlignCenter)
            if it is not None:
                icon.setPixmap(full_item_pixmap(it.name, getattr(it, "icon", "") or "", 40))
            else:
                icon.setText("?")
                icon.setStyleSheet("color:#565f89; font-size:18px; font-weight:bold;")
            rl.addWidget(icon, 0, Qt.AlignVCenter)
            st = str(c.get("state", "open") or "open")
            line = f"{c.get('item_name', '?')} ×{c.get('qty', 1)} ｜ 报酬 {c.get('reward', 0)}" \
                   f" {gt.currency}"
            if st == "open":
                left = int(c.get("expire_day", 0) or 0) - int(getattr(self.world, "day_count", 1) or 1)
                line += f"（剩 {max(0, left)} 天）"
            elif st == "taken":
                taker = self._npc(str(c.get("taker_npc_id") or ""))
                line += f"（{taker.name if taker else '有人'}承办，货在途中）"
            info = QLabel(line)
            info.setWordWrap(True)
            info.setStyleSheet("color:#c0caf5; font-size:12px;")
            rl.addWidget(info, 1)
            badge = QLabel(state_zh.get(st, "?"))
            badge.setStyleSheet(
                f"color:{state_color.get(st, '#c0caf5')}; font-size:11px; font-weight:bold;")
            rl.addWidget(badge)
            lay.addWidget(row)
        if not (dm.commissions or []):
            empty = QLabel("暂无委托。挂一单收购，让附近的行商为你的货架办货。")
            empty.setWordWrap(True)
            empty.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(empty)
        return w

    def _on_post_commission(self, dm, combo, qty_spin, reward_spin, days_spin):
        item_id = str(combo.currentData() or "")
        ok, err = de.post_commission(
            self.world, dm, item_id, int(qty_spin.value()),
            int(reward_spin.value()), int(days_spin.value()))
        if not ok:
            QMessageBox.warning(self, "挂单失败", err or "条件不满足。")
            return
        it = next((x for x in self.world.items if x.id == item_id), None)
        gt = self._gt()
        rw = int(reward_spin.value())
        QMessageBox.information(
            self, "委托已挂出",
            f"收购「{it.name if it else '货物'}」x{int(qty_spin.value())}的委托已挂出"
            f"（押金 {rw} {gt.currency}）。附近的人觉得划算就会接单。")
        self._after_change(f"委托挂出（- {rw} {gt.currency} 押金）")

    def _poach_panel(self, dm, gt) -> QWidget:
        """挖角区：交情 >=60 的 NPC 可签入据点（签约金 200x等级 从资金池扣）。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        head = QLabel("挖角（交情达朋友 60+ 的人物）")
        head.setStyleSheet("color:#9ece6a; font-size:13px; font-weight:bold;")
        lay.addWidget(head)
        hired = {str(r.get("npc_id")) for d in self.world.domains for r in de.roster_of(d)}
        cands = [n for n in self.world.npcs
                 if n.alive and not n.hostile and not n.is_key_npc
                 and n.id not in hired
                 and int(getattr(n, "affinity", 0) or 0) >= 60]
        if not cands:
            tip = QLabel("暂无可挖角之人——与各地 NPC 相处至「朋友」（交情 60+）后可签约入伙。")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#565f89; font-size:12px;")
            lay.addWidget(tip)
            return w
        # 职业选择（须对应设施已建）
        combo = QComboBox()
        for rk, label in de.ROLE_LABELS.items():
            ok, _err = de.role_available(self.world, dm, rk)
            combo.addItem(label, rk)
            if not ok:
                i = combo.count() - 1
                combo.setItemData(i, f"{label}（缺设施或已满）", Qt.DisplayRole)
        lay.addWidget(combo)
        for n in cands[:12]:
            fee = de._POACH_FEE_PER_LEVEL * max(1, int(getattr(n, "level", 1) or 1))
            row = QHBoxLayout()
            info = QLabel(f"{n.name}（{n.role or '平民'} {n.level}级·交情 {n.affinity}）"
                          f"签约金 {fee} {gt.currency}")
            info.setStyleSheet("color:#c0caf5; font-size:12px;")
            row.addWidget(info, 1)
            btn = QPushButton("挖角")
            btn.setEnabled(int(dm.funds) >= fee)
            btn.setToolTip("签走这位人物：原职解除（若为商人其店铺停摆），常驻你的据点")
            btn.clicked.connect(lambda _=False, nn=n, cb=combo: self._on_poach(dm, nn, cb))
            row.addWidget(btn)
            lay.addLayout(row)
        return w

    # ---------- 动作 ----------
    def _after_change(self, msg: str):
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        self.status_lbl.setText(msg)
        self._rebuild()
        self.changed.emit()

    def _funds_amount(self) -> int:
        """资金池存取金额：取当前树上活的 spin（页签化后 _rebuild 重建控件，
        实例属性 _amount_spin 指向的是已 deleteLater 的旧控件）。

        [!] 判活：deleteLater 排队期间旧控件仍在 findChildren 树上，须用
        shiboken6.isValid 过滤 + 取最后建的。"""
        import shiboken6 as _sbk
        cands = [s for s in self.findChildren(QSpinBox)
                 if s.maximum() > 999_999 and _sbk.isValid(s)]
        if not cands:
            return 0
        return int(cands[-1].value())

    def _on_deposit(self, dm):
        amt = self._funds_amount()
        gt = self._gt()
        ok, err = de.deposit_funds(self.world, dm, amt)
        if not ok:
            # [审核修复 2026-09-13] 币种走 GenreText（domain_engine 已返回题材化 err，此处兜底）
            QMessageBox.warning(self, "无法存入", err or f"{gt.currency}不足。")
            return
        self._after_change(f"已存入 {amt} {gt.currency}（资金池 {dm.funds} {gt.currency}）")

    def _on_withdraw(self, dm):
        amt = self._funds_amount()
        gt = self._gt()
        ok, err = de.withdraw_funds(self.world, dm, amt)
        if not ok:
            QMessageBox.warning(self, "无法取出", err or "资金池余额不足。")
            return
        self._after_change(f"已取出 {amt} {gt.currency}（资金池余 {dm.funds} {gt.currency}）")

    def _on_upgrade(self, dm):
        """[D5] 升聚落档：纯引擎同步结算（资金池付账 + 繁荣度门槛；居民上限/客流档位抬升）。"""
        info = de.next_tier_info(self.world, dm)
        if info is None:
            return
        nxt, price, pros_need = info
        gt = self._gt()
        ret = QMessageBox.question(
            self, "确认升格",
            f"斥资 {price} {gt.currency}（资金池）把「{dm.name}」升格为"
            f"{'城镇' if nxt == 'town' else '城市'}（居民上限 {de.resident_limit(nxt)}）？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        ok, err = de.upgrade_tier(self.world, dm)
        if not ok:
            QMessageBox.warning(self, "无法升格", err or "条件不满足。")
            return
        QMessageBox.information(
            self, "升格完成",
            f"「{dm.name}」升格完成——居民上限 {de.resident_limit(nxt)}，客流档位随之抬升。")
        self._after_change(f"「{dm.name}」升格（- {price} {gt.currency}）")

    # ---------- LLM 操作（购地/建造/招募共用一个 worker 槽位）----------
    def _start_op(self, status_txt: str, fn, on_done: Callable):
        """启动后台操作（守 §15）：整窗禁用 + parent=None 自管 + finished 双连。"""
        if self._op_worker is not None:
            return
        self.status_lbl.setText(status_txt)
        self.setEnabled(False)
        self._op_worker = _DomainOpWorker(fn, parent=None)
        self._op_worker.finished_signal.connect(on_done)
        self._op_worker.finished.connect(self._op_worker.deleteLater)
        self._op_worker.start()

    def _op_done_base(self) -> Optional[object]:
        """finish 槽公共头：复位 worker/窗口态；返回 worker 有效载荷前先调用。"""
        w, self._op_worker = self._op_worker, None
        self.setEnabled(True)
        self.status_lbl.setText("")
        return w

    def _on_buy(self, loc):
        gt = self._gt()
        price = de.domain_price(loc)
        # [2026-08-31 改自命名] 玩家自命名：预填题材池名作建议，可改；取消则不购
        name, ok = QInputDialog.getText(
            self, "开辟据点",
            f"斥资 {price} {gt.currency} 购下「{loc.name}」开辟为你的私人据点"
            f"（持有 {self.world.player.gold} {gt.currency}；购地是天价买卖，不可退）。请为据点命名：",
            text=de.fallback_domain_name(self.world, loc),
        )
        if not ok:
            return
        ok2, err = de.buy_domain(self.world, loc, (name or "").strip(), "")
        if not ok2:
            QMessageBox.warning(self, "购地失败", err or "未知错误")
            self._rebuild()
            return
        dm = de.domain_at(self.world, loc.id)
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        QMessageBox.information(
            self, "开辟据点",
            f"「{dm.name}」的营门立起来了——此地自此是你的私人据点。")
        self._rebuild()
        self.changed.emit()

    def _on_build(self, dm, kind: str):
        gt = self._gt()
        entry = next((f for f in de.facility_catalog(self.world) if f.get("kind") == kind), None)
        if entry is None:
            return
        price = de.facility_price(self.world, entry)
        mat_txt = "、".join(f"{zh} ×{n}" for (cat, n), zh in
                            zip(de._BUILD_MATS, ("锻造类", "制造类")))
        ret = QMessageBox.question(
            self, "确认建造",
            f"在「{dm.name}」建造{entry.get('type', '')}？耗资 {price} {gt.currency}"
            f"（资金池）+ 材料 {mat_txt}（背包）。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return

        def fn(cancel_check):
            ok, err = self.svc.build_domain_facility(
                self.world, dm, kind, self.preset, cancel_check=cancel_check)
            if not ok:
                return False, err or "建造失败"
            return True, de.facility_of(dm, kind)

        self._start_op("建造中…（正在为新设施起名）", fn, self._on_build_done)

    def _on_build_done(self, ok: bool, payload):
        self._op_done_base()
        if not ok:
            QMessageBox.warning(self, "建造失败", str(payload or "未知错误"))
            self._rebuild()
            return
        fac = payload
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        QMessageBox.information(
            self, "建设完成",
            f"「{fac.get('name', '')}」落成了——招募相应职员即可开门营业/值守。")
        self._rebuild()
        self.changed.emit()

    def _on_hire(self, dm, role_key: str):
        label = de.ROLE_LABELS.get(role_key, "职员")

        def fn(cancel_check):
            ok, err = self.svc.hire_domain_staff(
                self.world, dm, role_key, self.preset, cancel_check=cancel_check)
            if not ok:
                return False, err or "招募失败"
            return True, None

        self._start_op(f"招募中…（正在为{label}立档案）", fn, self._on_hire_done)

    def _on_hire_done(self, ok: bool, payload):
        self._op_done_base()
        if not ok:
            QMessageBox.warning(self, "招募失败", str(payload or "未知错误"))
            self._rebuild()
            return
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        QMessageBox.information(self, "新职员入职",
                                "一位新面孔入职了你的据点（详情见职员区与右侧 NPC 动态栏）。")
        self._rebuild()
        self.changed.emit()

    def _on_poach(self, dm, npc: NPC, combo: QComboBox):
        gt = self._gt()
        role_key = combo.currentData() if combo is not None else ""
        label = de.ROLE_LABELS.get(role_key, "职员")
        fee = de._POACH_FEE_PER_LEVEL * max(1, int(getattr(npc, "level", 1) or 1))
        ret = QMessageBox.question(
            self, "确认挖角",
            f"以 {fee} {gt.currency}签约金（资金池）签走 {npc.name}，"
            f"担任{label}并常驻「{dm.name}」？（若其为商人，原店将停摆）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        ok, err = de.poach_npc(self.world, dm, npc, role_key)
        if not ok:
            QMessageBox.warning(self, "挖角失败", err or "条件不满足。")
            return
        self._after_change(f"{npc.name} 入伙「{dm.name}」担任{label}（签约金 {fee} {gt.currency}）")

    # ---------- 前往 ----------
    def _open_npc_detail(self, npc):
        """人员页「档案」：NpcDetailDialog 只读查看（头像重生成落库后经 on_changed 重刷本窗）。"""
        from src.ui.dialogs.npc_detail_dialog import NpcDetailDialog
        if npc is None:
            return
        dlg = NpcDetailDialog(self.world, npc, world_sim_service=self.svc,
                              parent=self, on_changed=lambda: self._after_change(""))
        dlg.exec()

    def _on_travel(self, domain_location_id: str):
        """「前往」：确认后发 travel_requested + 关窗，场景页起 go_domain 回合。"""
        dm = next((d for d in self.world.domains
                   if d.location_id == domain_location_id), None)
        if dm is None:
            return
        ret = QMessageBox.question(
            self, "确认前往据点",
            f"现在动身返回据点「{dm.name}」？（消耗 1 回合世界演化）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self.travel_requested.emit(domain_location_id)
        self.accept()
