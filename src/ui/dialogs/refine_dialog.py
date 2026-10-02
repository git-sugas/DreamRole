"""[P34b] 鉴定/洗练对话框：装备鉴定洗练 + 宠物资质洗练（纯 Python 引擎，不用 LLM）。

入口：背包右键菜单「鉴定·洗练」（world_scene_tab._on_refine）。玩家从背包选目标物品/宠物 + reagent，引擎结算。
住宅建筑内另有内联器物台（home_dialog._panel_refine），不经过本对话框。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QWidget, QListWidget, QListWidgetItem, QTabWidget, QMessageBox,
)

from src.services import refine_engine as rfe
from src.models.world import effective_category
from src.ui.widgets.item_brief import display_name, item_tooltip
from src.models.world_sim_preset import GenreText
from src.utils.rng import SeededRng
from src.ui.dialogs.dice_check_overlay import DiceCheckOverlay


class RefineDialog(QDialog):
    """器物台：鉴定/洗练装备 + 洗练宠物资质。"""

    def __init__(self, world, on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.on_changed = on_changed
        self._gt = GenreText(getattr(world, "config_overlay", None) or {})
        self.setWindowTitle("器物台（鉴定·洗练）")
        self.resize(620, 600)
        from src.ui.widgets.game_widgets import banner_rule, card_sub
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        # [深改 2026-10-01] 页名大字 + 金笔分隔（旧 titleLabel 无页头感）
        title = QLabel("器物台")
        title.setObjectName("bannerName")
        outer.addWidget(title)
        outer.addWidget(banner_rule())
        outer.addSpacing(2)
        hint = card_sub("选目标装备/宠物 + 鉴定卷轴/洗练石/资质丹，点按钮结算。"
                        "未鉴定装备需先鉴定揭示属性；洗练重 roll 词条/资质。")
        outer.addWidget(hint)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_equip_tab(), "装备鉴定·洗练")
        self.tabs.addTab(self._build_pet_tab(), "宠物资质洗练")
        outer.addWidget(self.tabs, 1)

        close_btn = QPushButton("关闭")

        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

    # ---------- 装备 tab ----------
    def _build_equip_tab(self) -> QWidget:
        from src.ui.widgets.game_widgets import GameCard, card_title, card_sub
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        # 目标装备卡（未鉴定 + 已鉴定装备）
        eq_card = GameCard()
        eq_card.add(card_title("目标装备"))
        eq_card.add(card_sub("背包内的装备；灰名（未鉴定）须先鉴定", wrap=False))
        self.equip_list = QListWidget()
        self.equip_list.currentItemChanged.connect(self._on_equip_selected)
        eq_card.add(self.equip_list)
        lay.addWidget(eq_card, 1)
        # 道具卡（鉴定卷轴 + 洗练石）
        rg_card = GameCard()
        rg_card.add(card_title("道具"))
        rg_card.add(card_sub("鉴定卷轴 / 洗练石", wrap=False))
        self.equip_reagent_list = QListWidget()
        self.equip_reagent_list.currentItemChanged.connect(self._on_equip_reagent_selected)
        rg_card.add(self.equip_reagent_list)
        lay.addWidget(rg_card, 1)
        # 操作按钮（鉴定=主行动）
        btn_row = QHBoxLayout()
        self.appraise_btn = QPushButton("鉴定")
        self.appraise_btn.setObjectName("primaryBtn")
        self.appraise_btn.clicked.connect(self._on_appraise)
        self.refine_btn = QPushButton("洗练")
        self.refine_btn.setObjectName("optionBtn")
        self.refine_btn.clicked.connect(self._on_refine)
        btn_row.addWidget(self.appraise_btn)
        btn_row.addWidget(self.refine_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        self.equip_info = card_sub("选装备与道具后点鉴定/洗练。")
        lay.addWidget(self.equip_info)
        self._refresh_equip_tab()
        return w

    def _refresh_equip_tab(self):
        self.equip_list.clear()
        self.equip_reagent_list.clear()
        inv = getattr(self.world.player, "inventory", None) or []
        item_by_id = {getattr(it, "id", ""): it for it in (getattr(self.world, "items", None) or [])}
        for iid in inv:
            it = item_by_id.get(iid)
            if it is None:
                continue
            if getattr(it, "type", "") in ("weapon", "armor", "accessory"):
                lbl = display_name(it)
                tag = "（未鉴定）" if getattr(it, "identified", True) is False else ""
                item = QListWidgetItem(f"{lbl}{tag}")
                item.setData(Qt.UserRole, iid)
                item.setToolTip(item_tooltip(self.world, it, self._gt))
                # [深改] 未鉴定暗金调（神秘感）/已鉴定常规色
                from PySide6.QtGui import QColor
                item.setForeground(QColor("#bfa878" if tag else "#c0caf5"))
                self.equip_list.addItem(item)
            if effective_category(it) == "cultivate":
                item = QListWidgetItem(it.name)
                item.setData(Qt.UserRole, iid)
                item.setToolTip(it.desc)
                self.equip_reagent_list.addItem(item)
        self.equip_info.setText("选装备与道具后点鉴定/洗练。")
        self.appraise_btn.setEnabled(False)
        self.refine_btn.setEnabled(False)

    def _on_equip_selected(self, cur, prev):
        self._update_equip_buttons()

    def _on_equip_reagent_selected(self, cur, prev):
        self._update_equip_buttons()

    def _update_equip_buttons(self):
        ei = self.equip_list.currentItem()
        ri = self.equip_reagent_list.currentItem()
        # [!] bool() 必包：data() 返回 id 字符串，双选齐时 `has_e and has_r` 是 str，
        # PySide6 setEnabled(str) 抛 TypeError（槽崩 -> 鉴定/洗练按钮点不动）
        has_e = bool(ei is not None and ei.data(Qt.UserRole))
        has_r = bool(ri is not None and ri.data(Qt.UserRole))
        self.appraise_btn.setEnabled(has_e and has_r)
        self.refine_btn.setEnabled(has_e and has_r)

    def _selected_equip_id(self):
        ei = self.equip_list.currentItem()
        return ei.data(Qt.UserRole) if ei else None

    def _selected_equip_reagent_id(self):
        ri = self.equip_reagent_list.currentItem()
        return ri.data(Qt.UserRole) if ri else None

    def _on_appraise(self):
        eid = self._selected_equip_id()
        rid = self._selected_equip_reagent_id()
        if not eid or not rid:
            return
        ok, reason = rfe.appraise_check(self.world, self.world.player, eid, rid)
        if not ok:
            QMessageBox.information(self, "鉴定", reason)
            return
        item = next((i for i in (self.world.items or []) if getattr(i, "id", "") == eid), None)
        reagent = next((i for i in (self.world.items or []) if getattr(i, "id", "") == rid), None)
        overlay = DiceCheckOverlay(rfe.appraise_chance(item, reagent), parent=self)
        overlay.exec()
        if overlay.result is None:
            return
        r = rfe.appraise(self.world, self.world.player, eid, rid, dice=overlay.result)
        if r.get("ok"):
            affs = r.get("affixes") or []
            names = ", ".join(a.get("name", "") for a in affs if isinstance(a, dict)) or "无词条"
            QMessageBox.information(self, "鉴定成功", f"鉴定成功！词条：{names}")
            self._notify_changed()
            self._refresh_equip_tab()
        else:
            QMessageBox.information(self, "鉴定", r.get("reason", "鉴定失败"))

    def _on_refine(self):
        eid = self._selected_equip_id()
        rid = self._selected_equip_reagent_id()
        if not eid or not rid:
            return
        ok, reason = rfe.refine_check(self.world, self.world.player, eid, rid)
        if not ok:
            QMessageBox.information(self, "洗练", reason)
            return
        item = next((i for i in (self.world.items or []) if getattr(i, "id", "") == eid), None)
        reagent = next((i for i in (self.world.items or []) if getattr(i, "id", "") == rid), None)
        overlay = DiceCheckOverlay(rfe.appraise_chance(item, reagent), parent=self)
        overlay.exec()
        if overlay.result is None:
            return
        r = rfe.refine(self.world, self.world.player, eid, rid, dice=overlay.result)
        if r.get("ok"):
            affs = r.get("affixes") or []
            names = ", ".join(a.get("name", "") for a in affs if isinstance(a, dict)) or "无词条"
            QMessageBox.information(self, "洗练成功", f"洗练成功！新词条：{names}")
            self._notify_changed()
            self._refresh_equip_tab()
        else:
            QMessageBox.information(self, "洗练", r.get("reason", "洗练失败"))

    # ---------- 宠物 tab ----------
    def _build_pet_tab(self) -> QWidget:
        from src.ui.widgets.game_widgets import GameCard, card_title, card_sub
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        pet_card = GameCard()
        pet_card.add(card_title("目标宠物"))
        self.pet_list = QListWidget()
        self.pet_list.currentItemChanged.connect(self._on_pet_selected)
        pet_card.add(self.pet_list)
        lay.addWidget(pet_card, 1)
        rg_card = GameCard()
        rg_card.add(card_title("道具"))
        rg_card.add(card_sub("鉴定卷轴 / 资质丹", wrap=False))
        self.pet_reagent_list = QListWidget()
        self.pet_reagent_list.currentItemChanged.connect(self._on_pet_reagent_selected)
        rg_card.add(self.pet_reagent_list)
        lay.addWidget(rg_card, 1)
        btn_row = QHBoxLayout()
        self.pet_appraise_btn = QPushButton("鉴定宠物")
        self.pet_appraise_btn.setObjectName("primaryBtn")
        self.pet_appraise_btn.clicked.connect(self._on_pet_appraise)
        self.pet_refine_btn = QPushButton("洗练资质")
        self.pet_refine_btn.setObjectName("optionBtn")
        self.pet_refine_btn.clicked.connect(self._on_pet_refine)
        btn_row.addWidget(self.pet_appraise_btn)
        btn_row.addWidget(self.pet_refine_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        self.pet_info = card_sub("选宠物与道具后点鉴定/洗练。亲和>=50 才能洗练资质。")
        lay.addWidget(self.pet_info)
        self._refresh_pet_tab()
        return w

    def _refresh_pet_tab(self):
        self.pet_list.clear()
        self.pet_reagent_list.clear()
        pet_ids = getattr(self.world.player, "pet_ids", None) or []
        pet_by_id = {getattr(p, "id", ""): p for p in (getattr(self.world, "pets", None) or [])}
        for pid in pet_ids:
            pet = pet_by_id.get(pid)
            if pet is None:
                continue
            tag = "（未鉴定）" if getattr(pet, "identified", True) is False else ""
            aff = int(getattr(pet, "affinity", 0) or 0)
            item = QListWidgetItem(f"{pet.name}{tag}（亲和{aff}）")
            item.setData(Qt.UserRole, pid)
            self.pet_list.addItem(item)
        # reagent（cultivate 类，鉴定卷轴 + 资质丹通用）
        inv = getattr(self.world.player, "inventory", None) or []
        item_by_id = {getattr(it, "id", ""): it for it in (getattr(self.world, "items", None) or [])}
        for iid in inv:
            it = item_by_id.get(iid)
            if it is not None and effective_category(it) == "cultivate":
                item = QListWidgetItem(it.name)
                item.setData(Qt.UserRole, iid)
                item.setToolTip(it.desc)
                self.pet_reagent_list.addItem(item)
        self.pet_info.setText("选宠物与道具后点鉴定/洗练。亲和>=50 才能洗练资质。")
        self.pet_appraise_btn.setEnabled(False)
        self.pet_refine_btn.setEnabled(False)

    def _on_pet_selected(self, cur, prev):
        self._update_pet_buttons()

    def _on_pet_reagent_selected(self, cur, prev):
        self._update_pet_buttons()

    def _update_pet_buttons(self):
        pi = self.pet_list.currentItem()
        ri = self.pet_reagent_list.currentItem()
        has_p = bool(pi is not None and pi.data(Qt.UserRole))
        has_r = bool(ri is not None and ri.data(Qt.UserRole))
        self.pet_appraise_btn.setEnabled(has_p and has_r)
        self.pet_refine_btn.setEnabled(has_p and has_r)

    def _selected_pet_id(self):
        pi = self.pet_list.currentItem()
        return pi.data(Qt.UserRole) if pi else None

    def _selected_pet_reagent_id(self):
        ri = self.pet_reagent_list.currentItem()
        return ri.data(Qt.UserRole) if ri else None

    def _on_pet_appraise(self):
        pid = self._selected_pet_id()
        rid = self._selected_pet_reagent_id()
        if not pid or not rid:
            return
        rng = SeededRng.seed_from(getattr(self.world, "id", ""),
                                  int(getattr(self.world, "tick_count", 0) or 0),
                                  f"appraise_pet_ui_{pid}")
        r = rfe.appraise_pet(self.world, self.world.player, pid, rid, rng)
        if r.get("ok"):
            apt = r.get("aptitude") or {}
            apt_str = " ".join(f"{k}:{v}" for k, v in apt.items()) or "无"
            QMessageBox.information(self, "宠物鉴定", f"鉴定成功！资质：{apt_str}")
            self._notify_changed()
            self._refresh_pet_tab()
        else:
            QMessageBox.information(self, "宠物鉴定", r.get("reason", "鉴定失败"))

    def _on_pet_refine(self):
        pid = self._selected_pet_id()
        rid = self._selected_pet_reagent_id()
        if not pid or not rid:
            return
        rng = SeededRng.seed_from(getattr(self.world, "id", ""),
                                  int(getattr(self.world, "tick_count", 0) or 0),
                                  f"refine_pet_ui_{pid}")
        r = rfe.refine_pet(self.world, self.world.player, pid, rid, rng)
        if r.get("ok"):
            apt = r.get("aptitude") or {}
            apt_str = " ".join(f"{k}:{v}" for k, v in apt.items())
            QMessageBox.information(self, "资质洗练", f"洗练成功！新资质：{apt_str}")
            self._notify_changed()
            self._refresh_pet_tab()
        else:
            QMessageBox.information(self, "资质洗练", r.get("reason", "洗练失败"))

    # ---------- 公共 ----------
    def _notify_changed(self):
        if callable(self.on_changed):
            self.on_changed()
