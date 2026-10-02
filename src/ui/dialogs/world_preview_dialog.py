"""世界预览对话框：生成完成后展示世界并可微调，确认后保存。

仿 WorldDetailView 的展示风格，但加：世界名可改 + [保存世界] / [放弃]。
v1 不做逐条重生成（保留接口位置，后续 Phase 扩展），只支持改世界名 + 直接保存。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTabWidget, QScrollArea, QFrame, QMessageBox, QWidget,
)

from src.config import paths
from src.models import World
from src.ui.widgets.card_grid import make_card_pixmap


class WorldPreviewDialog(QDialog):
    """世界预览/微调对话框。保存后调用方落库。"""

    def __init__(self, world: World, parent=None):
        super().__init__(parent)
        self.world = world
        self.setWindowTitle("世界预览")
        self.resize(900, 720)
        self.setMinimumSize(760, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(10)

        # 顶部：世界名（可编辑）
        top = QHBoxLayout()
        top.addWidget(QLabel("世界名:"))
        self.name_edit = QLineEdit(world.name)
        top.addWidget(self.name_edit, 1)
        layout.addLayout(top)

        # banner
        self.banner_label = QLabel()
        self.banner_label.setAlignment(Qt.AlignCenter)
        self.banner_label.setFixedHeight(200)
        self.banner_label.setStyleSheet("background:#1a1b26; border-radius:8px; color:#565f89;")
        if world.banner:
            pm = QPixmap(os.path.join(paths.world_images_dir(), world.banner))
            if not pm.isNull():
                self.banner_label.setPixmap(pm.scaled(
                    self.banner_label.width() or 800, 200,
                    Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                ))
            else:
                self.banner_label.setText("（banner 图加载失败）")
        else:
            self.banner_label.setText("（未生成 banner）")
        layout.addWidget(self.banner_label)

        # 分段
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._fill_tabs()

        # 底部
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("放弃")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存世界")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _fill_tabs(self):
        w = self.world
        self.tabs.addTab(self._wrap(self._lore_tab()), f"世界观({len(w.lore)})")
        self.tabs.addTab(self._wrap(self._locations_tab()), f"地点({len(w.locations)})")
        self.tabs.addTab(self._wrap(self._npcs_tab()), f"NPC({len(w.npcs)})")
        self.tabs.addTab(self._wrap(self._factions_tab()), f"势力({len(w.factions)})")
        self.tabs.addTab(self._wrap(self._items_tab()), f"物品({len(w.items)})")
        self.tabs.addTab(self._wrap(self._quests_tab()), f"任务({len(w.quests)})")
        self.tabs.addTab(self._wrap(self._player_tab()), "玩家")

    @staticmethod
    def _wrap(widget) -> QScrollArea:
        s = QScrollArea()
        s.setWidgetResizable(True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        s.setFrameShape(QFrame.NoFrame)
        s.setWidget(widget)
        return s

    @staticmethod
    def _info_box(title: str, body: str, color="#c0caf5") -> QFrame:
        box = QFrame()
        box.setObjectName("gameCard")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(4)
        t = QLabel(title)
        t.setStyleSheet(f"color:{color}; font-weight:bold;")
        bl.addWidget(t)
        if body:
            b = QLabel(body)
            b.setWordWrap(True)
            b.setStyleSheet("color:#c0caf5;")
            bl.addWidget(b)
        return box

    def _lore_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        for e in self.world.lore:
            lay.addWidget(self._info_box(f"【{e.key}】", e.content, "#7aa2f7"))
        if not self.world.lore:
            lay.addWidget(QLabel("（无）"))
        lay.addStretch()
        return w

    def _locations_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        for loc in self.world.locations:
            fac = next((f.name for f in self.world.factions if f.id == loc.faction_id), "无")
            body = (f"区域:{loc.region or '未知'} | 势力:{fac} | 危险度:{loc.danger} | "
                    f"NPC:{len(loc.npc_ids)} | 连通:{len(loc.connections)}\n{loc.desc}")
            lay.addWidget(self._info_box(f"【{loc.name}】", body, "#9ece6a"))
        if not self.world.locations:
            lay.addWidget(QLabel("（无）"))
        lay.addStretch()
        return w

    def _npcs_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        for npc in self.world.npcs:
            loc = next((l.name for l in self.world.locations if l.id == npc.location_id), "未知")
            fac = next((f.name for f in self.world.factions if f.id == npc.faction_id), "无")
            row = QHBoxLayout()
            av = QLabel()
            av.setFixedSize(52, 52)
            av.setPixmap(make_card_pixmap(npc.name, npc.avatar, paths.world_images_dir(), 52))
            av.setStyleSheet("border-radius:26px; border:1px solid #2a2e44;")
            row.addWidget(av)
            body = f"身份:{npc.role} | 地点:{loc} | 势力:{fac}\n性格:{npc.personality}  目标:{npc.goal}"
            row.addWidget(self._info_box(f"【{npc.name}】", body, "#7aa2f7"), 1)
            lay.addLayout(row)
        if not self.world.npcs:
            lay.addWidget(QLabel("（无）"))
        lay.addStretch()
        return w

    def _factions_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        for f in self.world.factions:
            lay.addWidget(self._info_box(f"【{f.name}】", f"{f.desc}\n理念:{f.ideology}  首领特质:{f.leader_traits}", "#bb9af7"))
        if not self.world.factions:
            lay.addWidget(QLabel("（无）"))
        lay.addStretch()
        return w

    def _items_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        colors = {"common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
                  "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555"}
        for it in self.world.items:
            body = f"{it.desc}" + (f"\n效果:{it.effects}" if it.effects else "")
            lay.addWidget(self._info_box(f"【{it.name}】 {it.type} | {it.rarity}", body, colors.get(it.rarity, "#c0caf5")))
        if not self.world.items:
            lay.addWidget(QLabel("（无）"))
        lay.addStretch()
        return w

    def _quests_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        # [P15b1] locked = 任务链未解锁段，不展示（与任务日志口径一致，防剧透）
        quests = [q for q in self.world.quests if getattr(q, "status", "") != "locked"]
        for q in quests:
            giver = next((n.name for n in self.world.npcs if n.id == q.giver_npc_id), "未知")
            body = f"目标:{q.objective}\n委托人:{giver}" + (f"\n奖励:{q.reward_text}" if q.reward_text else "")
            lay.addWidget(self._info_box(f"【{q.title}】", body, "#e0af68"))
        if not quests:
            lay.addWidget(QLabel("（无）"))
        lay.addStretch()
        return w

    def _player_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        p = self.world.player
        loc = next((l.name for l in self.world.locations if l.id == p.location_id), "未知")
        lay.addWidget(self._info_box("玩家起始", f"当前位置:{loc}\n职业:{p.class_name or '未定'}\n身世:{p.background or '未定'}", "#7dcfff"))
        lay.addStretch()
        return w

    def _on_save(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "世界名不能为空。")
            return
        self.world.name = name
        self.accept()

    def get_world(self) -> World:
        return self.world
