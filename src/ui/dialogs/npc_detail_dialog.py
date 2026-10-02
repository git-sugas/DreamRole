"""[P6c] 世界模拟 NPC 档案对话框（详情与固定外貌 tag 编辑）。

需求来源：read.md §22 Phase C「NPC 应有自己的介绍页，存储人际关系/爱好/装备/背包/个人记忆」。
档案默认折叠隐藏细节、点「查看记忆」才显示记忆（沉浸感：正常不看，调试时再看）。
除固定外貌 tag 与头像外，生活资料只读；外貌 tag 修改时保存到世界存档。

[UI 改造 2026-10-01] 样板批次重排（暗金古卷设计系统，组件见 widgets/game_widgets.py）：
- 头部档案带：大头像（金环）+ 大名 + 身份副题 + 状态徽章行（势力/地点/住址/元素/标签）
- 生机卡：气血/经验/饱食度数值条（statBar）替代纯文字；钱款/印象等键值行
- 人生目标金顶线强调卡；出身关系/相处现状分列，相处现状带分档徽章
- 其余段（天赋/装备/背包/行动脉络/交谈/记忆/外貌 tag）同套卡片规范
数据与交互逻辑（头像 worker/外貌落盘/要角开关/记忆折叠）零变化，守既有测试契约。
守 §21 完全独立铁律。零 emoji。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea,
    QGridLayout, QMessageBox, QCheckBox, QPlainTextEdit, QWidget,
)

from src.config import paths
from src.models import World, NPC, Item
from src.models.world import EQUIPMENT_SLOTS
from src.services.talent_engine import ELEMENT_ZH
from src.services import quest_engine as qe
from src.ui.widgets.card_grid import make_card_pixmap
from src.ui.widgets.rarity_frame import RarityFrame
from src.ui.widgets.game_widgets import (
    GameCard, StatBar, game_badge, kv_row, banner_rule, card_sub,
)

_RARITY_COLOR = {
    "common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
    "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555",
}

_PORTRAIT = 96   # 头部大头像边长（金环描边由样式绘制）


def npc_action_trace_lines(world: World, npc: NPC, limit: int = 6) -> list[str]:
    """只用已结算事件串起 NPC 的起因、结果与可追踪货物，不补编世界事实。"""
    events = [e for e in (getattr(world, "event_log", None) or [])
              if npc.id in (getattr(e, "npcs", None) or [])
              and (getattr(e, "cause", "") or getattr(e, "outcome", ""))]
    if not events:
        return []
    recent = events[-max(1, limit):]
    shops = {s.id: s for s in (getattr(world, "shops", None) or [])}
    locs = {l.id: l for l in (getattr(world, "locations", None) or [])}
    items = {i.id: i for i in (getattr(world, "items", None) or [])}
    lines = []
    for index in range(len(recent) - 1, -1, -1):
        event = recent[index]
        bits = [f"第 {event.tick} 回合 · {event.title}"]
        if getattr(event, "cause", ""):
            bits.append(f"起因：{event.cause}")
        if getattr(event, "outcome", ""):
            bits.append(f"结果：{event.outcome}")
        involved = set(getattr(event, "item_ids", None) or [])
        previous = next((old for old in reversed(recent[:index])
                         if involved.intersection(getattr(old, "item_ids", None) or [])), None)
        if previous is not None:
            bits.append(f"承接：第 {previous.tick} 回合「{previous.title}」涉及同批物品。")
        shop = shops.get(str(getattr(event, "shop_id", "") or ""))
        if shop is not None:
            location = locs.get(str(getattr(shop, "location_id", "") or ""))
            place = f"{location.name}·{shop.name}" if location else shop.name
            stocked = [items[iid].name for iid in involved if iid in items
                       and any(str(getattr(st, "item_id", "") or "") == iid
                               and int(getattr(st, "stock", 0) or 0) > 0
                               for st in (getattr(shop, "stock", None) or []))]
            bits.append(f"现可前往 {place} 查看在售的{'、'.join(sorted(stocked))}。"
                        if stocked else f"关联店铺：{place}。")
        lines.append("\n".join(bits))
    return lines


class _AvatarRegenWorker(QThread):
    """重新生成头像 worker（danbooru + comfyui，阻塞不可取消段走 §5 口径）。"""
    finished_signal = Signal(object)   # str 文件名 / "" 失败

    def __init__(self, svc, world, npc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.npc = npc
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            fname = self.svc.regenerate_npc_avatar(
                self.world, self.npc, cancel_check=lambda: self._cancelled)
            self.finished_signal.emit(fname or "")
        except Exception as e:  # noqa: BLE001 - worker 兜底不崩 UI
            from src.utils.debug import debug_log
            debug_log(lambda: f"[WorldSim] 头像重生成异常: {e}")
            self.finished_signal.emit("")


class NpcDetailDialog(QDialog):
    """NPC 档案（除固定外貌 tag 和头像外，其余生活资料只读）。"""

    def __init__(self, world: World, npc: NPC, world_sim_service=None, parent=None,
                 on_changed=None):
        super().__init__(parent)
        self.world = world
        self.npc = npc
        self.svc = world_sim_service
        self.on_changed = on_changed   # 头像重生成落库后通知调用方刷新场景页
        self._avatar_worker = None
        self.setWindowTitle(f"档案 · {npc.name}")
        self.resize(780, 780)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        outer.addWidget(self._build_header())
        outer.addWidget(banner_rule())
        body = self._build_body()
        outer.addWidget(body, 1)
        close_btn = QPushButton("合上档案")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        outer.addLayout(row)

    # ============ 头部档案带 ============
    def _set_avatar(self) -> None:
        self.av_label.setPixmap(make_card_pixmap(
            self.npc.name, self.npc.avatar, paths.world_images_dir(), _PORTRAIT))

    def _build_header(self) -> QFrame:
        head = QFrame()
        h = QHBoxLayout(head)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(14)
        av = QLabel()
        av.setFixedSize(_PORTRAIT, _PORTRAIT)
        av.setStyleSheet(f"border-radius:{_PORTRAIT // 2}px;"
                         "border:2px solid #6b5a35; background:#1d1e2a;")
        self.av_label = av
        self._set_avatar()
        h.addWidget(av, 0, Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(4)
        name = QLabel(self.npc.name)
        name.setObjectName("bannerName")
        info.addWidget(name)
        # 身份副题（职业 + 当前所在地）
        fac = next((f.name for f in self.world.factions if f.id == self.npc.faction_id), "")
        loc = next((l.name for l in self.world.locations if l.id == self.npc.location_id), "—")
        sub_bits = [self.npc.role or "平民"]
        if fac:
            sub_bits.append(f"{fac} 门下" if not self.npc.hostile else f"{fac}（敌对）")
        sub_bits.append(f"现居 {loc}")
        info.addWidget(card_sub("　".join(sub_bits), wrap=False))
        # 状态徽章行（金/色语义：身份标签一眼可辨）
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        if not self.npc.alive:
            badge_row.addWidget(game_badge("已倒下", "danger"))
        elif self.npc.hostile:
            badge_row.addWidget(game_badge("敌对", "danger"))
        if self.npc.is_key_npc:
            badge_row.addWidget(game_badge("要角", "gold"))
        if self.npc.is_merchant:
            badge_row.addWidget(game_badge("商售", "success"))
        _els = [e for e in (getattr(self.npc, "elements", None) or []) if e in ELEMENT_ZH]
        for e in _els:
            badge_row.addWidget(game_badge(f"元素·{ELEMENT_ZH[e]}", "info"))
        home = next((l for l in self.world.locations
                     if l.id == (getattr(self.npc, "home_location_id", "") or "")), None)
        if home is not None and home.name != loc:
            badge_row.addWidget(game_badge(f"家在 {home.name}", "warn"))
        badge_row.addStretch()
        wrap = QWidget()
        wrap.setLayout(badge_row)
        info.addWidget(wrap)
        info.addWidget(self._build_residence_line())
        h.addLayout(info, 1)

        # 重新生成头像（按 NPC 外貌描述走 danbooru+comfyui 同管线）
        btn_col = QVBoxLayout()
        btn_col.setSpacing(6)
        self.regen_avatar_btn = QPushButton("重绘头像")
        self.regen_avatar_btn.setToolTip("优先按下方固定外貌标签重绘全身像；字段为空时先用中文外貌描述加工，并保存首次成功的提示词。")
        self.regen_avatar_btn.clicked.connect(self._on_regen_avatar)
        btn_col.addWidget(self.regen_avatar_btn)
        btn_col.addStretch()
        h.addLayout(btn_col)
        return head

    # ============ 重新生成头像（守 §5/§15 worker 生命周期）============
    def _on_regen_avatar(self):
        if self._avatar_worker is not None:
            return
        if not self.svc or not getattr(self.svc, "comfyui", None):
            QMessageBox.warning(self, "提示", "ComfyUI 文生图未配置，无法生成头像。")
            return
        # 用户在编辑框改了外貌后直接点重绘，应先落盘并用刚填写的 tag，不能悄悄用旧值。
        if self.appearance_tags_edit.toPlainText().strip() != str(getattr(self.npc, "appearance_tags", "") or ""):
            if not self._persist_appearance_tags(notify=False):
                return
        if not ((getattr(self.npc, "appearance", "") or "").strip()
                or (getattr(self.npc, "appearance_tags", "") or "").strip()):
            QMessageBox.warning(self, "提示", "该人物没有外貌描述或固定外貌标签，无法生成头像。")
            return
        self.regen_avatar_btn.setEnabled(False)
        self.appearance_tags_edit.setReadOnly(True)
        self.save_appearance_tags_btn.setEnabled(False)
        self.regen_avatar_btn.setText("生成中…")
        self._avatar_worker = _AvatarRegenWorker(self.svc, self.world, self.npc, parent=None)
        self._avatar_worker.finished_signal.connect(self._on_avatar_done)
        self._avatar_worker.start()

    def _on_avatar_done(self, fname):
        w = self._avatar_worker
        self._avatar_worker = None
        if w is not None:
            if w.isFinished():
                w.deleteLater()
            else:
                try:
                    w.finished.connect(w.deleteLater)
                except (RuntimeError, TypeError):
                    w.deleteLater()
        self.regen_avatar_btn.setEnabled(True)
        self.appearance_tags_edit.setReadOnly(False)
        self.save_appearance_tags_btn.setEnabled(True)
        self.appearance_tags_edit.setPlainText(str(getattr(self.npc, "appearance_tags", "") or ""))
        self.regen_avatar_btn.setText("重绘头像")
        if not fname:
            QMessageBox.warning(self, "生成失败", "头像生成失败（Danbooru/ComfyUI 任一环节出错或被取消），详情见控制台日志。")
            return
        # 刷新头部头像 + 落盘 + 通知调用方
        self._set_avatar()
        save_error = None
        try:
            if self.svc and getattr(self.svc, "storage", None):
                self.svc.storage.save_world(self.world)
        except Exception as e:
            save_error = e
        if self.on_changed:
            self.on_changed()
        if save_error is not None:
            QMessageBox.warning(self, "保存失败", f"头像已生成，但世界存档保存失败：{save_error}")

    def _cleanup_avatar_worker(self):
        w = self._avatar_worker
        if w is None:
            return
        try:
            w.finished_signal.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        w.wait(5000)
        self._avatar_worker = None
        if w.isFinished():
            w.deleteLater()
        else:
            try:
                w.finished.connect(w.deleteLater)
            except (RuntimeError, TypeError):
                w.deleteLater()

    def accept(self):
        # [!] 「合上档案」按钮走 accept 不触发 closeEvent/reject，须同样清理 worker
        self._cleanup_avatar_worker()
        super().accept()

    def reject(self):
        self._cleanup_avatar_worker()
        super().reject()

    def closeEvent(self, event):
        self._cleanup_avatar_worker()
        super().closeEvent(event)

    # ============ 主体（滚动卡片流）============
    def _build_residence_line(self) -> QLabel:
        """住址行：家（home_location_id，有则显）+ 当前所在地点·场所（弱化副文，与徽章互补）。"""
        cur = next((l for l in self.world.locations
                    if l.id == self.npc.location_id), None)
        cur_place = ""
        if cur is not None and getattr(self.npc, "place_id", ""):
            pl = next((p for p in (getattr(cur, "places", None) or [])
                       if p.id == self.npc.place_id), None)
            if pl is not None:
                cur_place = f"·{pl.name}"
        cur_txt = f"{cur.name}{cur_place}" if cur is not None else "—"
        lbl = QLabel(f"此刻在：{cur_txt}")
        lbl.setObjectName("gameCardSub")
        lbl.setWordWrap(True)
        return lbl

    def _build_appearance_tags_editor(self) -> QFrame:
        box = QFrame()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(4)
        hint = card_sub("固定外貌标签——首次成功生成头像时自动填写；修改后，重绘头像与人物事件插画沿用这里的外貌。")
        lay.addWidget(hint)
        self.appearance_tags_edit = QPlainTextEdit()
        self.appearance_tags_edit.setPlaceholderText("例如：1girl, long black hair, blue eyes, ...")
        self.appearance_tags_edit.setPlainText(str(getattr(self.npc, "appearance_tags", "") or ""))
        self.appearance_tags_edit.setFixedHeight(82)
        lay.addWidget(self.appearance_tags_edit)
        self.save_appearance_tags_btn = QPushButton("保存外貌标签")
        self.save_appearance_tags_btn.clicked.connect(self._save_appearance_tags)
        lay.addWidget(self.save_appearance_tags_btn, alignment=Qt.AlignRight)
        return box

    def _save_appearance_tags(self):
        self._persist_appearance_tags(notify=True)

    def _persist_appearance_tags(self, *, notify: bool) -> bool:
        tags = self.appearance_tags_edit.toPlainText().strip()
        if len(tags) > 4000:
            QMessageBox.warning(self, "提示", "固定外貌标签最多 4000 字。")
            return False
        old = str(getattr(self.npc, "appearance_tags", "") or "")
        self.npc.appearance_tags = tags
        try:
            if self.svc is None or getattr(self.svc, "storage", None) is None:
                raise RuntimeError("世界存储不可用")
            self.svc.storage.save_world(self.world)
        except Exception as e:
            self.npc.appearance_tags = old
            QMessageBox.warning(self, "保存失败", str(e))
            return False
        if self.on_changed:
            self.on_changed()
        if notify:
            QMessageBox.information(self, "已保存", "固定外貌标签已保存到世界存档。")
        return True

    def _build_body(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QFrame()
        col = QVBoxLayout(inner)
        col.setSpacing(10)
        col.setAlignment(Qt.AlignTop)

        # ---- 生机卡：数值条 + 键值行（替代旧「生活状态」纯文字段）----
        col.addWidget(self._vitals_card())
        # ---- 人生目标（金顶线强调卡）----
        _amb_desc = str((getattr(self.npc, "ambition", None) or {}).get("desc", "") or "")
        if _amb_desc:
            _prog = ""
            try:
                if self.svc is not None and hasattr(self.svc, "_ambition_progress"):
                    _prog = self.svc._ambition_progress(self.world, self.npc)
            except Exception:
                _prog = ""
            amb = GameCard(gold=True)
            amb.set_title("毕生所愿")
            body = QLabel(f"{_amb_desc}{_prog}")
            body.setWordWrap(True)
            body.setStyleSheet("color:#e8d5ae;")
            amb.add(body)
            col.addWidget(amb)
        # ---- 身世性情（短字段合并卡：性格/目标/爱好）----
        short_rows = []
        if self.npc.personality:
            short_rows.append(kv_row("性情", self.npc.personality))
        if self.npc.goal:
            short_rows.append(kv_row("目标", self.npc.goal))
        if self.npc.hobbies:
            short_rows.append(kv_row("爱好", "、".join(self.npc.hobbies)))
        if short_rows:
            card = GameCard("身世性情")
            for r in short_rows:
                card.add(r)
            col.addWidget(card)
        # ---- 外貌 + 固定外貌 tag 编辑 ----
        if (self.npc.appearance or "").strip():
            look = GameCard("外貌")
            t = QLabel(self.npc.appearance)
            t.setWordWrap(True)
            look.add(t)
            look.content.setSpacing(8)
            look.add(self._build_appearance_tags_editor())
            col.addWidget(look)
            self._appearance_card = look
        else:
            # 外貌为空也要有编辑入口（重绘依赖该 tag）
            look = GameCard("外貌")
            look.add(self._build_appearance_tags_editor())
            col.addWidget(look)
            self._appearance_card = look
        # ---- 备注 / 小传 ----
        for k, v in [("备注", self.npc.desc), ("小传", self.npc.notes)]:
            if v:
                c = GameCard(k)
                t = QLabel(v)
                t.setWordWrap(True)
                c.add(t)
                col.addWidget(c)
        # ---- 人际关系（出身叙事 + 相处现状分档徽章）----
        rel_card = self._relations_card()
        if rel_card is not None:
            col.addWidget(rel_card)
        # ---- 天赋（题材化品级/属性名）----
        from src.services import talent_engine as _te
        talents = [t for t in (getattr(self.npc, "talents", None) or []) if isinstance(t, dict)]
        if talents:
            gt = None
            try:
                from src.models.world_sim_preset import GenreText
                gt = GenreText(self.world.config_overlay)
            except Exception:  # noqa: BLE001 - overlay 脏数据回退通用名
                gt = None
            rn = gt.rarity_names() if gt is not None else None
            sn = {k: gt.stat(k) for k in ("str", "dex", "int", "vit", "luk")} if gt is not None else None
            c = GameCard("天赋")
            for line in _te.describe_talents(talents, rarity_names=rn, stat_names=sn):
                t = QLabel(line)
                t.setWordWrap(True)
                c.add(t)
            col.addWidget(c)
        # ---- 装备（8 槽 RarityFrame mini，契约不变）----
        equipped = self.npc.equipped if isinstance(self.npc.equipped, dict) else {}
        if equipped:
            col.addWidget(self._equipped_section(equipped))
        # ---- 要角开关（改动即落盘，交互不变）----
        col.addWidget(self._build_key_npc_toggle())
        # ---- 行动脉络 ----
        trace_lines = npc_action_trace_lines(self.world, self.npc)
        if trace_lines:
            c = GameCard("行动脉络")
            for ln in trace_lines:
                t = QLabel(ln)
                t.setWordWrap(True)
                c.add(t)
            col.addWidget(c)
        # ---- 近来的交谈 ----
        dlg_lines = self._dialogue_lines()
        if dlg_lines:
            c = GameCard("近来的交谈")
            for ln in dlg_lines:
                t = QLabel(ln)
                t.setWordWrap(True)
                c.add(t)
            col.addWidget(c)
        # ---- 随身背包 ----
        if self.npc.inventory:
            col.addWidget(self._inventory_section())
        # ---- 个人记忆（默认隐藏）----
        col.addWidget(self._memory_section())
        col.addStretch()

        scroll.setWidget(inner)
        return scroll

    def _vitals_card(self) -> GameCard:
        """生机卡：气血/经验/饱食度数值条 + 等级/钱款/可分配点/印象/动向键值行。"""
        from src.services import combat_engine as _ce
        try:
            from src.models.world_sim_preset import GenreText
            _gt = GenreText(self.world.config_overlay
                            if isinstance(self.world.config_overlay, dict) else {})
        except Exception:  # noqa: BLE001
            _gt = None
        card = GameCard("生机")
        card.add(StatBar(_gt.hp if _gt is not None else "气血",
                         self.npc.hp, self.npc.hp_max or 1, tone="red"))
        xp_next = self.npc.xp_next or _ce.xp_threshold(max(1, int(self.npc.level or 1)))
        card.add(StatBar("历练", self.npc.xp, xp_next, tone="green"))
        _hunger_on = False
        if self.svc is not None:
            try:
                _hunger_on = bool(self.svc._per_world(self.world, "hunger_enabled", False))
            except Exception:
                _hunger_on = False
        if _hunger_on:
            # [修 2026-09-06] 饱食 0 是合法值（`or 100` 会把 0 翻成 100）
            _raw = getattr(self.npc, "hunger", None)
            hg = max(0, min(100, int(_raw if _raw is not None else 100)))
            card.add(StatBar("饱食", hg, 100, tone="gold",
                             suffix="%" + ("（饿着肚子）" if hg < 30 else "")))
        kv = [f"等级 {self.npc.level} 级",
              f"随身钱款 {self.npc.wallet} {qe._currency(self.world)}"]
        if self.npc.stat_points:
            kv.append(f"可分配点 {self.npc.stat_points}")
        card.add(kv_row("底细", "　".join(kv)))
        try:
            from src.services import social_engine as _se
            _imp = _se.impression_line(self.npc)
            if _imp:
                card.add(kv_row("眼中的你", _imp))
        except Exception:
            pass
        act = str(getattr(self.npc, "current_action", "") or "").strip()
        if act:
            card.add(kv_row("近期动向", act[:60]))
        return card

    def _relations_card(self):
        """人际关系卡：出身叙事（世界生成播的种）+ 相处现状（社交底账，分档徽章）。"""
        rels = self.npc.relationships or []
        narr_rows = []
        for r in rels:
            if not isinstance(r, dict):
                continue
            nm = r.get("target_name") or r.get("target_id") or "?"
            rel = r.get("relation", "")
            desc = r.get("desc", "")
            narr_rows.append((nm, rel, desc))
        cur = self._relation_lines()
        if not narr_rows and not cur:
            return None
        card = GameCard("人际关系")
        if narr_rows:
            card.add(card_sub("—— 出身与经历 ——", wrap=False))
            for nm, rel, desc in narr_rows:
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(8)
                h.addWidget(game_badge(rel or "故旧", "info"))
                txt = QLabel(f"{nm}——{desc}" if desc else nm)
                txt.setWordWrap(True)
                h.addWidget(txt, 1)
                card.add(row)
        if cur:
            if narr_rows:
                card.content.addSpacing(4)
            card.add(card_sub("—— 相处现状（随日常涨落）——", wrap=False))
            for name, band, level in cur:
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(8)
                h.addWidget(game_badge(band, level))
                txt = QLabel(name)
                h.addWidget(txt, 1)
                card.add(row)
        return card

    def _relation_lines(self) -> list:
        """[关系图谱] social 底账 -> (名字, 分档, 徽章色)（按值倒序）。"""
        def _band(v: int) -> str:
            if v >= 60:
                return "挚友"
            if v >= 25:
                return "好友"
            if v <= -60:
                return "仇敌"
            if v <= -25:
                return "疏远"
            return "点头之交"

        def _level(band: str) -> str:
            return {"挚友": "success", "好友": "gold"}.get(
                band, {"仇敌": "danger", "疏远": "warn"}.get(band, "info"))

        rows = []
        for nid, val in sorted((self.npc.social or {}).items(), key=lambda kv: -int(kv[1])):
            n = next((x for x in self.world.npcs if x.id == str(nid)), None)
            if n is None or not getattr(n, "alive", True):
                continue
            band = _band(int(val))
            rows.append((n.name, band, _level(band)))
        return rows[:12]

    def _dialogue_lines(self) -> list:
        """[旁听对话] 该人物相关的对话档案行（未总结在前显全文，已总结显纪要）。"""
        nid = str(self.npc.id)
        rows = []
        for d in reversed(getattr(self.world, "npc_dialogues", None) or []):
            if not isinstance(d, dict) or nid not in (str(d.get("a_id", "")), str(d.get("b_id", ""))):
                continue
            other = d.get("a_name", "") if str(d.get("b_id", "")) == nid else d.get("b_name", "")
            if d.get("summarized"):
                rows.append(f"与{other}（第{d.get('day', '?')}天，已归档）：{d.get('summary', '')}")
            else:
                conv = "；".join(
                    f"{(d.get('a_name', '') if t.get('who') == 'a' else d.get('b_name', ''))}：{t.get('text', '')}"
                    for t in (d.get("turns") or []) if isinstance(t, dict))
                rows.append(f"与{other}（第{d.get('day', '?')}天）：{conv[:400]}")
            if len(rows) >= 6:
                break
        return rows

    def _build_key_npc_toggle(self) -> QCheckBox:
        """[要角勾选 2026-09-06 用户指示] 玩家可控制该 NPC 是否走 LLM 深度决策（要角）。

        勾上=进入要角决策轮换池（更有主见，token 略增）；取消=纯规则模拟（省 token），
        且不再享受要角豁免（势力战争中可能阵亡、死后由系统补员顶替）。改动即落盘。"""
        cb = QCheckBox("深度模拟（要角）——由大模型离屏决策，更有主见")
        cb.setChecked(bool(getattr(self.npc, "is_key_npc", False)))
        cb.setToolTip(
            "勾选：该人物参与每回合的大模型离屏深度决策（按要角预算轮换），行为更有主见。\n"
            "取消：走纯规则模拟（省 token）；且不再享受要角豁免——势力战争中可能阵亡，\n"
            "死后会被系统补员顶替。世界生成时系统标注过的会自动勾上，你随时可以改。")

        def _toggle(checked: bool):
            self.npc.is_key_npc = bool(checked)
            # [要角补档 2026-09-12] 刚升为要角的要把「要角有而非要角没有」的字段补上：
            # talents/current_goal（确定性）+ 小传/爱好/关系（有 API 时补一次 LLM）。
            if checked and self.svc is not None:
                try:
                    self.svc.ensure_key_npc_fields(self.world, self.npc)
                except Exception:
                    pass
            try:
                if self.svc and getattr(self.svc, "storage", None):
                    self.svc.storage.save_world(self.world)
            except Exception:
                pass
            if getattr(self, "on_changed", None):
                try:
                    self.on_changed()
                except Exception:
                    pass

        cb.toggled.connect(_toggle)
        return cb

    def _item(self, item_id: str) -> Item | None:
        return next((i for i in self.world.items if i.id == item_id), None)

    def _equipped_section(self, equipped: dict) -> QFrame:
        card = GameCard("装备（八槽）")
        grid = QGridLayout()
        grid.setSpacing(6)
        # [UI 禁英文] 槽位名走 GenreText 题材化（武侠：头巾/劲装…；此前裸 head/chest）
        try:
            from src.models.world_sim_preset import GenreText
            _gt = GenreText(self.world.config_overlay
                            if isinstance(self.world.config_overlay, dict) else {})
        except Exception:  # noqa: BLE001
            _gt = None
        _slot_zh = (lambda s: _gt.slot(s)) if _gt is not None else (lambda s: s)
        for i, slot in enumerate(EQUIPMENT_SLOTS):
            iid = equipped.get(slot)
            it = self._item(iid) if iid else None
            # [修 2026-09-06 真机] 装备槽改用真 RarityFrame：此前普通 QFrame 只挂
            # rarity 属性，而 QSS 对紫/橙/红特意 border:none（特效框靠 RarityFrame
            # paintEvent 自绘流光）——普通 QFrame 无自绘，金/红/紫装备槽完全无边框。
            cell = RarityFrame(it.rarity if it else "common")
            cell._content.setContentsMargins(6, 4, 6, 4)   # mini 槽紧凑边距（默认 8）
            cell._content.setSpacing(1)
            cl = cell._content
            cl.addWidget(QLabel(f"<span style='color:#565f89;font-size:11px'>{_slot_zh(slot)}</span>"))
            nm = QLabel(it.name if it else "（空）")
            nm.setStyleSheet(f"color:{_RARITY_COLOR.get(it.rarity, '#c0caf5') if it else '#565f89'}; font-size:12px;")
            cl.addWidget(nm)
            grid.addWidget(cell, i // 4, i % 4)
            if it is not None:
                from src.ui.widgets.item_brief import item_tooltip
                cell.setToolTip(item_tooltip(self.world, it))
                cell.setToolTipDuration(10000)
        card.content.addLayout(grid)
        return card

    def _inventory_section(self) -> QFrame:
        card = GameCard("随身背包")
        # [!] 网格换行（每行 4 个）：横排 HBox 放多件 ItemBriefRow 会溢出档案页宽
        grid = QGridLayout()
        grid.setSpacing(6)
        from src.ui.widgets.item_brief import ItemBriefRow
        idx = 0
        for iid in (self.npc.inventory or []):
            it = self._item(iid)
            if it is None:
                continue
            rf = RarityFrame(it.rarity)
            rf.addWidget(ItemBriefRow(self.world, it, sub_text="", icon_size=36))
            grid.addWidget(rf, idx // 4, idx % 4)
            idx += 1
        if idx == 0:
            grid.addWidget(QLabel("（空）"), 0, 0)
        card.content.addLayout(grid)
        return card

    def _memory_section(self) -> QFrame:
        card = GameCard()
        self._mem_box = card
        head = QHBoxLayout()
        head.addWidget(card.card_title_label("个人记忆"))
        head.addStretch()
        self._mem_toggle = QPushButton("查看记忆")
        self._mem_toggle.setToolTip("展示该人物已沉淀的记忆（默认隐藏，供调试/沉浸回看）")
        self._mem_toggle.clicked.connect(self._toggle_memory)
        head.addWidget(self._mem_toggle)
        card.content.addLayout(head)
        self._mem_content = QLabel("（点击「查看记忆」展开）")
        self._mem_content.setStyleSheet("color:#565f89; font-size:12px;")
        self._mem_content.setWordWrap(True)
        self._mem_content.hide()
        card.add(self._mem_content)
        return card

    def _toggle_memory(self):
        if self._mem_content.isVisible():
            self._mem_content.hide()
            self._mem_toggle.setText("查看记忆")
            return
        # 取已存储的记忆（不触发 LLM/召回）
        text = "（尚无记忆。与该人物多次互动后会自动沉淀。）"
        if self.svc is not None:
            try:
                info = self.svc.npc_memory().get_memory_info(self.npc.id, self.world.id)
                summary = info.get("summary", "")
                entries = info.get("entries", [])
                parts = []
                if summary:
                    parts.append(f"[总结] {summary}")
                for e in entries:
                    parts.append(f"[{e.get('triggers', '')}] {e.get('detail', '')}")
                if parts:
                    text = "\n".join(parts)
                elif entries or summary:
                    text = "（记忆为空）"
            except Exception as ex:
                text = f"（读取记忆失败：{ex}）"
        self._mem_content.setText(text)
        self._mem_content.show()
        self._mem_toggle.setText("收起记忆")
