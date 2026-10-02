"""[P24d] 宠物管理对话框（列表 / 出战切换 / 投食）。

纯直改模式（仿 AttributeDialog/HomeDialog 管理区）：调 pet_engine 引擎函数 ->
storage.save_world -> changed.emit（场景页 refresh_state + world_changed）。
无 LLM 调用故无 worker（守 §15 worker 生命周期只约束有 QThread 的对话框）。
投食口径：下拉选背包消耗品，亲和提升按品质分档（pet_engine.feed_affinity_gain；
驯服自动投食仍走 find_feed_item 第一件，作诱饵不加亲和）。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QWidget,
    QPushButton, QSizePolicy, QMessageBox, QComboBox,
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPixmap

from src.models.world import Pet
from src.models.world_sim_preset import GenreText
from src.services import pet_engine as pex
from src.services import refine_engine as rfe
from src.utils.rng import SeededRng


def _pet_image_file(sp: dict):
    """[2026-08-23 用户指示] 查宠物种族预生成图缓存（pet_{key}.png，不生成；
    键口径同怪物图 md5(name|desc[:60])）。无图返回 None（卡片纯文字如旧）。"""
    import os
    from src.config import paths
    from src.services.world_sim_service import WorldSimService
    if not sp:
        return None
    key = WorldSimService._combat_image_key(sp)
    if key is None:
        return None
    p = os.path.join(paths.world_images_dir(), f"pet_{key}.png")
    return p if os.path.exists(p) else None


def _skill_icon_path(sk: dict):
    """查技能图标缓存（skill_{key}.png，预生成管线登记过宠物候选技能；无图 None）。"""
    import os
    from src.config import paths
    from src.services.world_sim_service import WorldSimService
    if not isinstance(sk, dict):
        return None
    key = WorldSimService._combat_image_key(sk)
    if key is None:
        return None
    p = os.path.join(paths.world_images_dir(), f"skill_{key}.png")
    return p if os.path.exists(p) else None


class PetDialog(QDialog):
    """[P24d] 宠物页：我的宠物（出战切换/投食）+ 获取渠道提示。"""

    changed = Signal()  # 出战切换/投食后发（场景页刷新；落盘已在本对话框做）

    def __init__(self, world, storage, parent=None):
        super().__init__(parent)
        self.world = world
        self.storage = storage
        self.setWindowTitle("宠物")
        self.resize(640, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        hint = QLabel("驯服的伙伴会跟随你冒险：出战自动参战（不占同伴位）、获经验成长；"
                      "投食可选消耗品，亲和按品质提升（30 可洗技能 / 50 解锁种族被动）；"
                      "升级 4级/8级 开新技能槽，兽诀卷可重洗技能；资质鉴定/洗练在器物台。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa5ce; font-size:12px;")
        outer.addWidget(hint)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#e0af68; font-size:12px;")
        outer.addWidget(self.status_lbl)

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
    def _rebuild(self):
        while self.body.count() > 1:
            item = self.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        pets = [p for p in (self.world.pets or []) if isinstance(p, Pet)]
        self._insert_section(f"我的宠物（{len(pets)}/{pex.PET_MAX}）")
        if not pets:
            self._insert_empty("尚未拥有宠物。获取渠道：探索奇遇中遇到野兽幼崽/灵宠并驯服"
                               "（携带消耗品可投食提高成功率）；野外击败怪物后有几率遇到寻亲的幼崽。")
            return
        active_id = str(getattr(self.world.player, "active_pet_id", "") or "")
        for pet in pets:
            self._insert_widget(self._pet_row(pet, pet.id == active_id))

    def _insert_section(self, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#7aa2f7; font-weight:bold; margin-top:4px;")
        self.body.insertWidget(self.body.count() - 1, lbl)

    def _insert_empty(self, text: str):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#565f89; font-size:12px;")
        self.body.insertWidget(self.body.count() - 1, lbl)

    def _insert_widget(self, w):
        self.body.insertWidget(self.body.count() - 1, w)

    def _bag_consumables(self):
        """背包宠物食品（去重种类，投食下拉列表用；只认 category=pet_food）。"""
        from src.models.world import effective_category
        item_by_id = {it.id: it for it in (self.world.items or [])}
        out, seen = [], set()
        for iid in (self.world.player.inventory or []):
            if iid in seen:
                continue
            it = item_by_id.get(iid)
            if it is not None and effective_category(it) == "pet_food":
                out.append(it)
                seen.add(iid)
        return out

    _PET_REAGENT_KINDS = ("identify", "apt_atk", "apt_def", "apt_hp",
                          "apt_mp", "apt_spd", "apt_all")

    def _bag_pet_reagents(self):
        """背包宠物洗练道具（鉴定卷轴/资质丹，去重种类；洗练下拉列表用）。"""
        from src.models.world import effective_category
        item_by_id = {it.id: it for it in (self.world.items or [])}
        out, seen = [], set()
        for iid in (self.world.player.inventory or []):
            if iid in seen:
                continue
            it = item_by_id.get(iid)
            if (it is not None and effective_category(it) == "cultivate"
                    and getattr(it, "reagent_kind", "") in self._PET_REAGENT_KINDS):
                out.append(it)
                seen.add(iid)
        return out

    def _pet_row(self, pet: Pet, is_active: bool) -> QFrame:
        row = QFrame()
        row.setObjectName("gameCard")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        sp = pex.find_species(pet.species) or {}
        # [2026-08-24] 头像移到右侧：有种族图正常展示，无图回退「首字」占位（仿无图 NPC 头像）
        img_path = _pet_image_file(sp)
        info = QVBoxLayout()
        info.setSpacing(2)
        nm = QLabel(f"「{pet.name}」" + ("　◈ 出战跟随中" if is_active else ""))
        nm.setStyleSheet("color:#c0caf5; font-weight:bold;")
        info.addWidget(nm)
        at_cap = pet.level >= pex.PET_LEVEL_CAP
        xp_txt = "MAX" if at_cap else f"{pet.xp}/{pex.pet_xp_next(pet.level)}"
        sub = QLabel(
            f"{sp.get('name', '未知种族')} · {sp.get('desc', '')}\n"
            f"Lv.{pet.level}（xp {xp_txt}） · 亲和 {pet.affinity}/100"
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#9aa5ce; font-size:11px;")
        info.addWidget(sub)
        # ---- 属性面板（五维题材化显示名 + 战斗聚合值，pet_stats/build_pet_unit 同源）----
        gt = GenreText(self.world.config_overlay
                       if isinstance(self.world.config_overlay, dict) else {})
        stats = pex.pet_stats(self.world, pet)
        unit = pex.build_pet_unit(self.world, pet)
        s = unit.snapshot
        stat_txt = " / ".join(
            f"{gt.stat(k)} {stats[k]}" for k in ("str", "dex", "int", "vit", "luk"))
        stats_lbl = QLabel(
            f"属性：{stat_txt}\n"
            f"战斗：HP {s.hp_max} · 攻 {s.atk} · 防 {s.def_} · 暴 {int(s.crit_rate)}%")
        stats_lbl.setWordWrap(True)
        stats_lbl.setStyleSheet("color:#7dcfff; font-size:11px;")
        info.addWidget(stats_lbl)
        # ---- 资质（未鉴定打码）----
        if pet.identified:
            apt = pet.aptitude or {}
            apt_txt = " / ".join(f"{k} {apt.get(k, 1.0):.2f}"
                                 for k in ("atk", "def", "hp", "mp", "spd"))
            apt_lbl = QLabel(f"资质：{apt_txt}（资质丹可洗练）")
        else:
            apt_lbl = QLabel("资质：未鉴定（鉴定卷轴揭示）")
        apt_lbl.setWordWrap(True)
        apt_lbl.setStyleSheet("color:#bb9af7; font-size:11px;")
        info.addWidget(apt_lbl)
        # ---- 被动功效说明 ----
        passive_key = str(sp.get("passive", "") or "")
        pl = pex.passive_label(self.world, pet)
        if passive_key:
            desc = pex.PASSIVE_DESC.get(passive_key, "")
            pas_lbl = QLabel(f"被动：{pl}" + (f" —— {desc}" if desc else ""))
            pas_lbl.setWordWrap(True)
            pas_lbl.setStyleSheet("color:#9ece6a; font-size:11px;")
            info.addWidget(pas_lbl)
        # ---- 技能行（图标按钮 + 洗技能；空槽灰徽标解锁等级）----
        sk_row = QHBoxLayout()
        sk_row.setSpacing(4)
        sk_row.addWidget(self._mk_label("技能：", "#e0af68"))
        genre = pex.genre_id_of(self.world)
        has_scroll = self._beast_scroll_id() is not None
        for slot in range(3):
            name = pet.skills[slot] if slot < len(pet.skills or []) else ""
            if name:
                sk = pex.species_skill_def(genre, name) or {}
                chip = QPushButton(name)
                chip.setObjectName("optionBtn")
                # [技能图标] 同战斗/技能页统一口径（有图用图，无图类型色 + 首字兜底）
                try:
                    from PySide6.QtGui import QIcon
                    from PySide6.QtCore import QSize
                    from src.ui.widgets.skill_brief import skill_icon_pixmap
                    chip.setIcon(QIcon(skill_icon_pixmap(sk or {"name": name}, 20)))
                    chip.setIconSize(QSize(20, 20))
                except Exception:
                    icon_path = _skill_icon_path(sk)
                    if icon_path:
                        from PySide6.QtGui import QIcon
                        from PySide6.QtCore import QSize
                        chip.setIcon(QIcon(icon_path))
                        chip.setIconSize(QSize(20, 20))
                tip = self._skill_tooltip(sk)
                chip.setToolTip(tip)
                chip.clicked.connect(lambda _=False, p=pet, i=slot: self._on_reroll_skill(p, i))
                chip.setEnabled(has_scroll and pet.affinity >= rfe.PET_SKILL_REROLL_AFFINITY_AT)
                if not chip.isEnabled():
                    chip.setToolTip(tip + "\n（洗技能需：兽诀卷 + 亲和 >= 30，点击即洗）")
                sk_row.addWidget(chip)
            else:
                need = 4 if slot == 1 else 8
                lock = QLabel(f"槽{slot + 1}·{need}级解锁")
                lock.setObjectName("atmosphereBadge")
                lock.setProperty("badgeLevel", "info")
                sk_row.addWidget(lock)
        sk_note = QLabel("（点技能名 = 兽诀卷洗该槽）")
        sk_note.setStyleSheet("color:#565f89; font-size:10px;")
        sk_row.addWidget(sk_note)
        sk_row.addStretch()
        info.addLayout(sk_row)
        # ---- 投食行：下拉选消耗品，亲和提升按品质分档 ----
        consumables = self._bag_consumables()
        feed_row = QHBoxLayout()
        feed_row.setSpacing(4)
        feed_row.addWidget(self._mk_label("投食：", "#e0af68"))
        combo = QComboBox()
        for it in consumables:
            combo.addItem(
                f"{it.name}（{gt.rarity(it.rarity)} +{pex.feed_affinity_gain(it)}）", it.id)
        combo.setEnabled(bool(consumables))
        feed_row.addWidget(combo, 1)
        feed_btn = QPushButton("投 食")
        feed_btn.setEnabled(bool(consumables))
        feed_btn.setToolTip("消耗所选消耗品，亲和按品质提升（"
                            f"白 +{pex.FEED_AFFINITY_BY_RARITY['common']} … "
                            f"红 +{pex.FEED_AFFINITY_BY_RARITY['mythic']}）")
        feed_btn.clicked.connect(
            lambda _=False, p=pet, c=combo: self._on_feed(p, c.currentData()))
        feed_row.addWidget(feed_btn)
        info.addLayout(feed_row)
        # ---- [2026-08-24] 洗练行：下拉选鉴定卷轴/资质丹使用（仿投食），取代原「鉴定·洗练资质」按钮 ----
        reagents = self._bag_pet_reagents()
        rf_row = QHBoxLayout()
        rf_row.setSpacing(4)
        rf_row.addWidget(self._mk_label("洗练：", "#bb9af7"))
        rf_combo = QComboBox()
        for it in reagents:
            rf_combo.addItem(f"{it.name}（{getattr(it, 'reagent_kind', '')}）", it.id)
        rf_combo.setEnabled(bool(reagents))
        rf_row.addWidget(rf_combo, 1)
        rf_btn = QPushButton("使 用")
        rf_btn.setEnabled(bool(reagents))
        rf_btn.setToolTip("鉴定卷轴=揭示资质；资质丹=洗练资质（消耗所选物品）")
        rf_btn.clicked.connect(
            lambda _=False, p=pet, c=rf_combo: self._on_use_pet_reagent(p, c.currentData()))
        rf_row.addWidget(rf_btn)
        info.addLayout(rf_row)
        lay.addLayout(info, 1)

        btns = QVBoxLayout()
        btns.setSpacing(6)
        # 右侧头像：有种族图展示图，无图回退「首字」占位（仿无图 NPC 头像）
        av = QLabel()
        av.setFixedSize(96, 96)
        av.setAlignment(Qt.AlignCenter)
        pix = None
        if img_path:
            pix = QPixmap(img_path)
            if pix.isNull():
                pix = None
        if pix is not None:
            av.setPixmap(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            av.setText(pet.name[0] if pet.name else "宠")
            av.setStyleSheet("background:#2a2e44; border-radius:6px; color:#c0caf5;"
                             "font-size:44px; font-weight:bold;")
        btns.addWidget(av, 0, Qt.AlignHCenter)
        act_btn = QPushButton("收回" if is_active else "设为出战")
        act_btn.setToolTip("出战 = 跟随玩家并自动参战（不占同伴名额）；收回 = 留在后方休整")
        act_btn.clicked.connect(lambda _=False, p=pet: self._on_toggle_active(p))
        btns.addWidget(act_btn)
        btns.addStretch()
        lay.addLayout(btns)
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return row

    def _mk_label(self, text: str, color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{color}; font-size:11px;")
        return lbl

    def _skill_tooltip(self, sk: dict) -> str:
        """技能全字段 tooltip（供宠物技能按钮）：desc + 类型/威力/冷却/物理法术/缩放/元素/群/状态。"""
        from src.services import combat_engine as ce
        gt = GenreText(self.world.config_overlay
                       if isinstance(self.world.config_overlay, dict) else {})
        genre = pex.genre_id_of(self.world)
        parts = [str(sk.get("desc", "") or "")]
        line = []
        t = str(sk.get("type", "") or "")
        type_zh = {"attack": "攻击", "heal": "治疗", "buff": "增益"}.get(t, t)
        line.append(f"类型 {type_zh}")
        if int(sk.get("power", 0) or 0):
            line.append(f"威力 {sk.get('power')}")
        cd = int(sk.get("cooldown", 0) or 0)
        line.append(f"冷却 {cd}回合" if cd else "无冷却")
        dt = str(sk.get("damage_type", "") or "")
        if t == "attack":
            line.append("法术" if dt == "magical" else "物理")  # 缺省 physical
        sc = str(sk.get("stat_scaling", "") or "")
        if sc:
            line.append(f"缩放{gt.stat(sc)}")
        el = str(sk.get("element", "") or "")
        if el:
            line.append(f"元素{el}")
        tp = str(sk.get("target_pattern", "") or "")
        if tp and tp != "single":
            line.append("群/AOE" if tp.startswith("aoe") else tp)
        parts.append(" · ".join(line))
        # 附带状态（inflicts）
        conds = sk.get("inflicts") or []
        if isinstance(conds, list) and conds:
            cond_txt = "、".join(
                f"{ce.condition_display(str(c.get('condition', '')), genre)}"
                f"({int(c.get('chance', 0) * 100)}%/{c.get('duration', '')}回)"
                for c in conds if isinstance(c, dict) and c.get("condition"))
            if cond_txt:
                parts.append(f"附带：{cond_txt}")
        return "\n".join(parts)

    def _beast_scroll_id(self):
        """背包里第一件兽诀卷（reagent_kind=beast_skill）；无则 None。"""
        from src.models.world import effective_category
        item_by_id = {it.id: it for it in (self.world.items or [])}
        for iid in (self.world.player.inventory or []):
            it = item_by_id.get(iid)
            if it is not None and effective_category(it) == "cultivate" \
                    and getattr(it, "reagent_kind", "") == "beast_skill":
                return iid
        return None

    # ---------- 动作（直改 + 即时落盘）----------
    def _after_change(self, msg: str):
        if self.storage is not None:
            try:
                self.storage.save_world(self.world)
            except Exception:
                pass
        self.status_lbl.setText(msg)
        self._rebuild()
        self.changed.emit()

    def _on_toggle_active(self, pet: Pet):
        cur = str(getattr(self.world.player, "active_pet_id", "") or "")
        if cur == pet.id:
            self.world.player.active_pet_id = ""
            self._after_change(f"「{pet.name}」收起休整（不再参战/跟随）")
        else:
            self.world.player.active_pet_id = pet.id
            self._after_change(f"「{pet.name}」设为出战（跟随玩家并自动参战）")

    def _on_feed(self, pet: Pet, item_id):
        if not item_id:
            self.status_lbl.setText("背包里没有消耗品，无从投食。")
            return
        ok, msg = pex.feed_pet(self.world, self.world.player, pet, str(item_id))
        if ok:
            self._after_change(f"「{pet.name}」{msg}")
        else:
            self.status_lbl.setText(msg)

    def _on_use_pet_reagent(self, pet: Pet, item_id):
        """[2026-08-24] 洗练行使用：鉴定卷轴->appraise_pet；资质丹->refine_pet。"""
        if not item_id:
            self.status_lbl.setText("背包里没有鉴定卷轴/资质丹。")
            return
        it = next((i for i in self.world.items if i.id == item_id), None)
        kind = getattr(it, "reagent_kind", "") if it is not None else ""
        rng = SeededRng.seed_from(self.world.id, self.world.tick_count,
                                  f"pet_rf_{pet.id}_{item_id}")
        if kind == "identify":
            r = rfe.appraise_pet(self.world, self.world.player, pet.id, item_id, rng=rng)
        else:
            r = rfe.refine_pet(self.world, self.world.player, pet.id, item_id, rng=rng)
        if r.get("ok"):
            self._after_change(f"「{pet.name}」{r.get('reason') or '洗练完成'}")
        else:
            self.status_lbl.setText(r.get("reason") or "使用失败")

    def _on_reroll_skill(self, pet: Pet, slot: int):
        """洗技能：确认 -> 消耗兽诀卷重 roll 指定槽（引擎确定性）。"""
        scroll_id = self._beast_scroll_id()
        if scroll_id is None:
            self.status_lbl.setText("背包里没有兽诀卷（商店不卖：掉落/拍卖/奇遇可得）。")
            return
        if pet.affinity < rfe.PET_SKILL_REROLL_AFFINITY_AT:
            self.status_lbl.setText(f"亲和不足{rfe.PET_SKILL_REROLL_AFFINITY_AT}，无法洗技能。")
            return
        old = pet.skills[slot] if slot < len(pet.skills or []) else ""
        ret = QMessageBox.question(
            self, "确认洗技能",
            f"消耗 1 张兽诀卷重洗「{pet.name}」的技能槽 {slot + 1}"
            f"（当前：{old or '无'}）？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        r = rfe.reroll_pet_skill(self.world, self.world.player, pet.id, slot, scroll_id)
        if r.get("ok"):
            self._after_change(f"「{pet.name}」洗技能：{old or '无'} -> {r.get('skill', '')}")
        else:
            self.status_lbl.setText(r.get("reason", "洗技能失败"))
