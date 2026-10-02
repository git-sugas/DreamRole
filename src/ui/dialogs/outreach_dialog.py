"""[P57 NPC 上门] 信使对话框：处理 NPC 主动上门（信件/悬赏/战书/求助）。

展示 pending 卡片（标题/正文/剩余时限）+ 接受/拒绝按钮；最近处理记录一行带过。
纯展示 + 调 svc.outreach_accept/decline；约战的实际开战由场景页在对话框关闭后
接手（dlg.duel_npc_id 传出 npc_id），防对话框开着时弹战斗对话框嵌套。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame, QMessageBox, QWidget,
)

_KIND_CN = {"letter": "来信", "bounty": "悬赏", "duel": "战书", "plea": "求助"}
_STATE_CN = {"accepted": "已接受", "declined": "已婉拒", "expired": "已过期", "done": "已处理"}


class OutreachDialog(QDialog):
    """信使对话框。changed=True 表示处理过至少一条（调用方落盘+刷新）；
    duel_npc_id 非空 = 有约战被接受，调用方负责 NPC 上门 + 开战。"""

    def __init__(self, svc, world, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.changed = False
        self.duel_npc_id = ""

        self.setWindowTitle("信使")
        self.resize(560, 620)
        self.setMinimumSize(480, 480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        head = QLabel("托人带给你的消息——过期不候")
        head.setObjectName("bannerName")
        outer.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        self._cards_host = QWidget()
        self._cards_lay = QVBoxLayout(self._cards_host)
        self._cards_lay.setContentsMargins(0, 0, 0, 0)
        self._cards_lay.setSpacing(8)
        scroll.setWidget(self._cards_host)
        outer.addWidget(scroll, 1)

        self._history_lbl = QLabel("")
        self._history_lbl.setWordWrap(True)
        self._history_lbl.setStyleSheet("color:#565f89; font-size:12px;")
        outer.addWidget(self._history_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        self._rebuild()

    def _rebuild(self):
        """全量重建卡片（守面板状态刷新纪律：状态回调后必须自重建）。"""
        while self._cards_lay.count():
            item = self._cards_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        outreaches = list(getattr(self.world, "outreaches", None) or [])
        pending = [o for o in outreaches if o.state == "pending"]
        finished = [o for o in outreaches if o.state != "pending"]

        if not pending:
            empty = QLabel("眼下没有捎来的消息。\n\n世界隔几天就会有人找你——"
                           "继续你的旅程，信使随时会来敲门。")
            empty.setWordWrap(True)
            empty.setStyleSheet("color:#565f89; padding:24px 8px;")
            self._cards_lay.addWidget(empty)

        for o in pending:
            self._cards_lay.addWidget(self._build_card(o))

        if finished:
            rows = []
            for o in finished:
                st = _STATE_CN.get(o.state, o.state)
                rows.append(f"· {o.title}（{st}）")
            self._history_lbl.setText("最近处理：\n" + "\n".join(rows[-8:]))
        else:
            self._history_lbl.setText("")

    def _build_card(self, o) -> QFrame:
        card = QFrame()
        card.setObjectName("outreachCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        title = QLabel(o.title or _KIND_CN.get(o.kind, "消息"))
        title.setStyleSheet("font-weight:bold; font-size:14px; color:#c0caf5;")
        lay.addWidget(title)

        text = QLabel(o.text or "")
        text.setWordWrap(True)
        text.setStyleSheet("color:#a9b1d6;")
        lay.addWidget(text)

        day = max(1, int(getattr(self.world, "day_count", 1) or 1))
        remain = int(o.expires_day or 0) - day
        when = "今日截止" if remain <= 0 else f"还剩 {remain} 天"
        meta = QLabel(f" {_KIND_CN.get(o.kind, '消息')} · {when}")
        meta.setStyleSheet("color:#e0af68; font-size:12px;")
        lay.addWidget(meta)

        btns = QHBoxLayout()
        btns.addStretch()
        accept_text = "收下" if o.kind == "letter" else "接受"
        accept_btn = QPushButton(accept_text)
        accept_btn.setObjectName("primaryBtn")
        accept_btn.clicked.connect(lambda _=False, oo=o: self._on_accept(oo))
        btns.addWidget(accept_btn)
        if o.kind != "letter":
            decline_btn = QPushButton("拒绝")
            decline_btn.clicked.connect(lambda _=False, oo=o: self._on_decline(oo))
            btns.addWidget(decline_btn)
        lay.addLayout(btns)
        return card

    def _on_accept(self, o):
        res = self.svc.outreach_accept(self.world, o.id)
        action = str(res.get("action", ""))
        if action in ("missing_item", "missing_gold"):
            QMessageBox.information(self, "差些什么", str(res.get("msg", "条件未满足。")))
            return  # 条件不满足保持 pending（守 §24：不追认没做成的承诺）
        self.changed = True
        if action == "duel":
            self.duel_npc_id = str(res.get("npc_id", "") or "")
            self.accept()   # 约战直接关对话框，开战交给场景页
            return
        QMessageBox.information(self, "信使", str(res.get("msg", "已处理。")))
        self._rebuild()

    def _on_decline(self, o):
        msg = self.svc.outreach_decline(self.world, o.id)
        self.changed = True
        QMessageBox.information(self, "信使", msg)
        self._rebuild()
