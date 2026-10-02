"""[U01 完整版 2026-09-30] 今日议程视图（可滚动清单 + 每项「前往」导航）。

替代旧 QMessageBox 文本清单（F12：点一件事不能直接找到对应页面）。数据源
`svc.agenda_items`（零 LLM 纯读）；导航只是打开对应对话框/页面——**不消耗游戏时间、
不结算、不改存档**（守计划 U01 第 3 条）。对话框不持有 world 引用（items 是纯 dict
快照），导航回调由场景页注入（quests/outreach/dungeon/commissions/domain/bosses）。

[UI 改造 2026-10-01] 样板批次重排（暗金古卷设计系统）：
- 战备读数收进金顶线「今日战备」卡；条目卡按 priority 上左边线（红=要紧/金=次之）
- 图标收进徽章、标题/副文两级字阶；「前往」按钮右对齐（行为契约不变）
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QWidget,
    QPushButton, QComboBox,
)
from PySide6.QtCore import Qt

from src.ui.widgets.game_widgets import GameCard, card_sub, game_badge

_NAV_LABELS = {"quests": "任务日志", "outreach": "信使", "dungeon": "秘境",
               "commissions": "订单板", "domain": "据点", "bosses": "讨伐"}


class AgendaDialog(QDialog):
    """议程清单：按 priority 排序的待办卡片流；带 nav 的条目有「前往」按钮。"""

    # [U01 余项 2026-09-30] 筛选档（kind 归类；scene_tab 持久化选择跨开窗保留）
    FILTERS = ("全部", "要紧", "剧情", "经济")
    _FILTER_KINDS = {"要紧": {"boss", "quest_claim", "quest_pending", "loot_pending",
                              "outreach", "injury", "grudge", "fund"},
                     "剧情": {"quest", "arc", "arcs"},
                     "经济": {"prod", "commission", "region"}}

    def __init__(self, items: list, nav_callbacks: dict, parent=None,
                 readiness_lines: list = None, initial_filter: str = "全部",
                 on_filter_changed=None):
        super().__init__(parent)
        self._nav = {k: cb for k, cb in (nav_callbacks or {}).items() if callable(cb)}
        self._on_filter = on_filter_changed if callable(on_filter_changed) else None
        self._all_items = list(items)
        self.setWindowTitle("今日议程")
        self.resize(600, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        # 顶部一行：说明（左）+ 筛选（右），不再各占一整行
        top = QHBoxLayout()
        top.setSpacing(10)
        hint = QLabel("现在有什么等着你——点「前往」直达对应页面（查看不消耗时间）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        top.addWidget(hint, 1)
        self._filter_box = QComboBox()
        self._filter_box.addItems(list(self.FILTERS))
        _ix = self._filter_box.findText(initial_filter if initial_filter in self.FILTERS else "全部")
        if _ix >= 0:
            self._filter_box.setCurrentIndex(_ix)
        self._filter_box.currentTextChanged.connect(self._apply_filter)
        top.addWidget(self._filter_box, 0, Qt.AlignVCenter)
        outer.addLayout(top)

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

        close_btn = QPushButton("关闭")

        close_btn.setObjectName("primaryBtn")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        outer.addLayout(row)

        # [G01/R3 2026-09-30] 战备读数（纯读零导航：伤情/药品/耐久/同伴疲劳/已知敌情）
        self._ready_lines = list(readiness_lines or [])
        self._apply_filter(self._filter_box.currentText())

    def _apply_filter(self, txt: str) -> None:
        """按筛选档重绘清单；回调通知 scene_tab 持久化选择。"""
        if self._on_filter is not None:
            try:
                self._on_filter(txt)
            except Exception:
                pass
        kinds = self._FILTER_KINDS.get(txt)
        items = self._all_items if not kinds else             [it for it in self._all_items if str(it.get("kind", "")) in kinds]
        # 清空重建条目区（战备顶卡随 _render_readiness 一并重插）
        while self.body.count() > 1:
            w_ = self.body.takeAt(0).widget()
            if w_ is not None:
                w_.setParent(None)
                w_.deleteLater()
        self._render_readiness()
        if not items:
            empty = GameCard()
            t = QLabel("眼下天下太平，没有等着你处理的事。\n\n"
                       "自由输入任何行动都可以——探索、闲逛、找人聊天，世界都会回应。")
            t.setWordWrap(True)
            t.setStyleSheet("color:#9aa5ce;")
            empty.add(t)
            self._add_row(empty)
            return
        for it in items:
            self._add_item(it)

    def _render_readiness(self) -> None:
        """战备顶卡（金顶线）：多行读数收进一张卡，不再逐行裸飘。"""
        if not self._ready_lines:
            return
        card = GameCard(gold=True)
        card.set_title("今日战备")
        for ln in self._ready_lines:
            rl = QLabel(str(ln))
            rl.setWordWrap(True)
            rl.setStyleSheet("color:#7dcfff; font-size:12px;")
            card.add(rl)
        self._add_row(card)

    def _add_row(self, w) -> None:
        self.body.insertWidget(self.body.count() - 1, w)

    def _add_item(self, it: dict) -> None:
        pr = int(it.get("priority", 9))
        card = GameCard(prio=pr if pr in (0, 1) else None)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        # 图标徽章（方形色块内放字符图标，与文字标题分离）
        icon = game_badge(str(it.get("icon") or "·"),
                          "danger" if pr == 0 else ("warn" if pr == 1 else "gold"))
        icon.setFixedWidth(30)
        lay.addWidget(icon, 0, Qt.AlignVCenter)
        text_col = QWidget()
        tc = QVBoxLayout(text_col)
        tc.setContentsMargins(0, 0, 0, 0)
        tc.setSpacing(2)
        title = QLabel(f"<span style='color:#e8e8f5; font-weight:bold;'>{it.get('title', '')}</span>")
        title.setTextFormat(Qt.RichText)
        title.setWordWrap(True)
        tc.addWidget(title)
        det = str(it.get("detail", "") or "")
        if det:
            tc.addWidget(card_sub(det))
        lay.addWidget(text_col, 1)
        nav = str(it.get("nav", "") or "")
        if nav in self._nav:
            b = QPushButton(f"前往{_NAV_LABELS.get(nav, '')}")
            b.setObjectName("optionBtn")
            b.setToolTip("打开对应页面（只查看，不消耗游戏时间）")
            b.clicked.connect(lambda _=False, _n=nav: self._go(_n))
            lay.addWidget(b, 0, Qt.AlignVCenter)
        card.content.addLayout(lay)
        self._add_row(card)

    def _go(self, nav: str) -> None:
        """执行导航回调后关闭议程（目标页操作完由场景页刷新，避免清单过期误导）。"""
        cb = self._nav.get(nav)
        if cb is None:
            return
        self.accept()
        try:
            cb()
        except Exception:
            pass
