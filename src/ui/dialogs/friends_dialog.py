"""[P10] 好友页（FriendsDialog）：好友列表 + 添加好友 + 私聊入口。

交情达门槛（默认 60，preset.friend_affinity_threshold）的 NPC 可添加为好友；
好友可私聊（NpcChatDialog）与更积极的同场景互动（npc_reaction_engine 已接交情档位）。
只读契约：本对话框只改 player.friend_npc_ids（加/删好友）+ 落 save_world，
其余世界状态不碰；变化经 changed 信号通知场景页刷新。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QScrollArea, QWidget, QFrame,
)

from src.config import paths
from src.ui.widgets.card_grid import make_card_pixmap
from src.services import npc_reaction_engine as nre


class FriendsDialog(QDialog):
    """[P10] 好友管理页。"""

    changed = Signal()   # 加/删好友后发（场景页刷新）

    def __init__(self, world, world_sim_service, storage, preset, parent=None):
        super().__init__(parent)
        self.world = world
        self.svc = world_sim_service
        self.storage = storage
        self.preset = preset
        self.setWindowTitle("好友")
        self.resize(560, 640)
        self._chat_dlg = None           # [私聊单例 2026-09-07] 同 NPC 私聊窗引用

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        hint = QLabel("与人物交谈/私聊/并肩作战可提升交情；交情达「朋友」档即可添加好友。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(hint)

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
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._rebuild()

    # ---------- 构建 ----------
    def _threshold(self) -> int:
        if self.svc is not None and self.world is not None and self.preset is not None:
            return int(self.svc._per_world(self.world, "friend_affinity_threshold",
                                           self.preset.friend_affinity_threshold, self.preset))
        return 60

    def _rebuild(self):
        # 清空（保留末尾 stretch）
        while self.body.count() > 1:
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        threshold = self._threshold()
        friends = []
        candidates = []
        for n in self.world.npcs:
            if not getattr(n, "alive", True) or getattr(n, "hostile", False):
                continue
            if nre.is_friend(self.world, n):
                friends.append(n)
            elif int(getattr(n, "affinity", 0)) >= threshold:
                candidates.append(n)
        # 已是好友
        hdr1 = self._section_label(f"我的好友（{len(friends)}）")
        self.body.insertWidget(self.body.count() - 1, hdr1)
        if not friends:
            self.body.insertWidget(self.body.count() - 1, self._empty_label("还没有好友。多和其他人物聊聊吧。"))
        for n in friends:
            self.body.insertWidget(self.body.count() - 1, self._make_row(n, is_friend=True))
        # 可结交
        hdr2 = self._section_label(f"可结交（交情达 {threshold}，共 {len(candidates)}）")
        self.body.insertWidget(self.body.count() - 1, hdr2)
        if not candidates:
            self.body.insertWidget(self.body.count() - 1,
                                   self._empty_label("暂无交情达标的人物。交谈/送礼/并肩作战都能拉近距离。"))
        for n in candidates:
            self.body.insertWidget(self.body.count() - 1, self._make_row(n, is_friend=False))

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#7aa2f7; font-weight:bold; margin-top:4px;")
        return lbl

    @staticmethod
    def _empty_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#565f89; font-size:12px;")
        return lbl

    def _make_row(self, npc, is_friend: bool) -> QFrame:
        row = QFrame()
        row.setObjectName("gameCard")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)
        # 头像
        av = QLabel()
        av.setFixedSize(44, 44)
        av.setPixmap(make_card_pixmap(npc.name, npc.avatar, paths.world_images_dir(), 44))
        av.setStyleSheet("border-radius:22px; border:1px solid #2a2e44;")
        lay.addWidget(av)
        # 名字/身份/交情条
        info = QVBoxLayout()
        info.setSpacing(2)
        aff = int(getattr(npc, "affinity", 0))
        nm = QLabel(f"{npc.name}（{npc.role or '未知'}）")
        nm.setStyleSheet("color:#c0caf5; font-weight:bold;")
        info.addWidget(nm)
        bar = QProgressBar()
        bar.setObjectName("hpBar")
        bar.setTextVisible(True)
        bar.setFixedHeight(12)
        bar.setRange(0, 100)
        bar.setValue(aff)
        bar.setFormat(f"交情 {nre.affinity_level(aff)} {aff}/100")
        bar.setProperty("hpLevel", "low" if aff < 40 else "mid" if aff < 70 else "high")
        bar.style().unpolish(bar)
        bar.style().polish(bar)
        info.addWidget(bar)
        lay.addLayout(info, 1)
        # 操作按钮
        if is_friend:
            chat_btn = QPushButton("私聊")
            chat_btn.setObjectName("primaryBtn")
            chat_btn.setEnabled(bool(self.preset is None or
                                     self.svc._per_world(self.world, "friend_chat_enabled",
                                                          self.preset.friend_chat_enabled, self.preset)))
            chat_btn.clicked.connect(lambda _=False, n=npc: self._on_chat(n))
            lay.addWidget(chat_btn)
            del_btn = QPushButton("删除好友")
            del_btn.clicked.connect(lambda _=False, n=npc: self._on_remove_friend(n))
            lay.addWidget(del_btn)
        else:
            add_btn = QPushButton("加为好友")
            add_btn.setObjectName("primaryBtn")
            add_btn.clicked.connect(lambda _=False, n=npc: self._on_add_friend(n))
            lay.addWidget(add_btn)
        return row

    # ---------- 动作 ----------
    def _on_add_friend(self, npc):
        if not nre.can_add_friend(self.world, npc, self._threshold()):
            return
        if npc.id not in self.world.player.friend_npc_ids:
            self.world.player.friend_npc_ids.append(npc.id)
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        self._rebuild()
        self.changed.emit()

    def _on_remove_friend(self, npc):
        if npc.id in self.world.player.friend_npc_ids:
            self.world.player.friend_npc_ids.remove(npc.id)
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        self._rebuild()
        self.changed.emit()

    def _on_chat(self, npc):
        from src.ui.dialogs.npc_chat_dialog import NpcChatDialog
        # [私聊单例 2026-09-07] 走类级 open_for：好友页/场景页任一入口同 NPC
        # 只开一个窗（双窗口各持一份 _session = “对话被清空”观感）。
        dlg, _new = NpcChatDialog.open_for(
            self.world, npc, self.svc, self.storage, self.preset, parent=self)
        self._chat_dlg = dlg          # 兼容旧引用（判活/前置已由 open_for 内聚）
        dlg.exec()
        # 私聊会 +交情，回来刷新显示
        self._rebuild()
        self.changed.emit()
