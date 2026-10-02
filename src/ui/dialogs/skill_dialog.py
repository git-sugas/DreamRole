# -*- coding: utf-8 -*-
"""[技能页 2026-08-28 用户定稿] 玩家技能对话框：左栏玩家面板「技能」按钮进入。

- 已学技能列表：名称/类型/威力（含熟练度等级加成）/耗蓝/冷却/熟练度进度条（Lv1-5）
- 3 个装备槽：战斗只出已装备技能；点槽位弹出已学技能选择；再点卸下
- 研读区：背包里的技能书（teach_skill）一键研读学入（闭环修复：买书后无处学）
- 熟练度：战斗施展随机 +8~16 xp（确定性），阈值 100+50*lv，满级 5（威力每级 +15%，Lv5=1.6x；
  2026-09-10 用户指示：施展升级随机化 + 整体放慢约 2 倍）
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QWidget, QGridLayout, QFrame, QMessageBox, QProgressBar, QSizePolicy,
)

from src.services import combat_engine as ce
from src.ui.widgets.skill_brief import skill_icon_pixmap, skill_type_zh

SKILL_SLOTS = 3


class SkillDialog(QDialog):
    def __init__(self, world, on_changed=None, parent=None):
        super().__init__(parent)
        self.world = world
        self.on_changed = on_changed
        self.setWindowTitle("技能")
        self.resize(540, 640)
        from src.ui.widgets.game_widgets import (banner_rule, card_title,
                                                 card_sub, GameCard)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        # [深改 2026-10-01] 页名大字 + 金笔分隔 + 金字区头统一（旧三色区头杂乱）
        title = QLabel("技能")
        title.setObjectName("bannerName")
        outer.addWidget(title)
        outer.addWidget(banner_rule())
        outer.addSpacing(2)

        # ---- 装备槽区 ----
        slot_card = GameCard(gold=True)
        slot_card.add(card_title("出战技能栏"))
        slot_card.add(card_sub("战斗中只可使用已装备的技能，最多 3 个", wrap=False))
        self.slot_row = QHBoxLayout()
        self.slot_row.setSpacing(8)
        slot_host = QWidget()
        slot_host.setLayout(self.slot_row)
        slot_card.add(slot_host)
        outer.addWidget(slot_card)

        # ---- 已学列表 ----
        outer.addWidget(self._sec_header("已学技能",
                                         "施展积熟练度，满 5 级威力 +60%"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        # [!] 横向滚动恒关：技能卡内容只许纵向排，撑宽即布局 bug（定宽条/长文不换行），
        # 关掉让问题直接暴露为换行/压缩而不是一条拖动条。
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(6)
        scroll.setWidget(self.list_host)
        outer.addWidget(scroll, 1)

        # ---- 研读区 ----
        book_card = GameCard()
        book_card.add(card_title("可研读的技能书"))
        book_card.add(card_sub("背包内的技能书，研读即习得", wrap=False))
        self.book_lay = QVBoxLayout()
        self.book_lay.setContentsMargins(0, 0, 0, 0)
        self.book_lay.setSpacing(4)
        book_host = QWidget()
        book_host.setLayout(self.book_lay)
        book_card.add(book_host)
        outer.addWidget(book_card)

        close_btn = QPushButton("关闭")

        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)
        self._refresh()

    @staticmethod
    def _sec_header(title: str, sub: str) -> QWidget:
        """金字区头 + 灰副说明（统一暗金区头语言）。"""
        from src.ui.widgets.game_widgets import card_title, card_sub
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(1)
        col.addWidget(card_title(title))
        col.addWidget(card_sub(sub, wrap=False))
        return host

    # ------------------------------------------------------------------
    def _skill_id(self, sk) -> str:
        return str(sk.get("id") or sk.get("name") or "")

    def _equipped_ids(self) -> list:
        return [str(s) for s in (getattr(self.world.player, "equipped_skills", None) or [])]

    def _refresh(self):
        p = self.world.player
        # 清空重建
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        while self.book_lay.count():
            it = self.book_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        while self.slot_row.count():
            it = self.slot_row.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        # ---- 装备槽 ----
        eq_ids = self._equipped_ids()
        by_id = {self._skill_id(s): s for s in (p.skills or []) if isinstance(s, dict)}
        for i in range(SKILL_SLOTS):
            sid = eq_ids[i] if i < len(eq_ids) else ""
            sk = by_id.get(sid)
            btn = QPushButton(f"槽{i + 1}：{sk.get('name', '?') if sk else '（空）'}")
            btn.setMinimumHeight(44)
            btn.setObjectName("optionBtn")
            if sk is not None:
                # [技能图标] 出战槽图标（有图用图，无图类型色 + 首字兜底）
                try:
                    from PySide6.QtCore import QSize as _QSize
                    btn.setIcon(QIcon(skill_icon_pixmap(sk, 28)))
                    btn.setIconSize(_QSize(28, 28))
                except Exception:
                    pass
                btn.setStyleSheet("text-align:left; padding-left:10px;")
                btn.clicked.connect(lambda _=False, idx=i: self._cycle_slot(idx))
            else:
                btn.setStyleSheet("text-align:left; padding-left:10px; color:#565f89;")
                btn.clicked.connect(lambda _=False, idx=i: self._cycle_slot(idx))
            self.slot_row.addWidget(btn)

        # ---- 已学列表 ----
        if not p.skills:
            empty = QLabel("尚未学会任何技能——可从技能书研读、技能书商店购买或奇遇传承习得。")
            empty.setStyleSheet("color:#565f89;")
            empty.setWordWrap(True)
            self.list_lay.addWidget(empty)
        for sk in (p.skills or []):
            if not isinstance(sk, dict):
                continue
            self.list_lay.addWidget(self._skill_card(sk, by_id))

        # ---- 研读区 ----
        item_by_id = {it.id: it for it in (getattr(self.world, "items", None) or [])}
        books = [item_by_id[iid] for iid in (p.inventory or [])
                 if isinstance(item_by_id.get(iid), object) and item_by_id[iid] is not None
                 and getattr(item_by_id[iid], "teach_skill", None)]
        if not books:
            # [修 2026-09-10] 后缀示例按本世界题材取自 _GENRE_BOOK_SUFFIX（单一来源）——
            # 原文案硬编码「·芯片/·固件/秘籍」是科幻/修仙措辞，武侠档真机报「跨题材文案残留」。
            from src.services.world_sim_service import _GENRE_BOOK_SUFFIX
            _gid = (getattr(self.world, "config_overlay", {}) or {}).get(
                "attribute_template_id", "")
            _m = _GENRE_BOOK_SUFFIX.get(_gid) or _GENRE_BOOK_SUFFIX["western_fantasy"]
            _kinds = "/".join(f"·{s}" for s in dict.fromkeys(
                _m.get(k, "") for k in ("attack", "heal", "buff", "default") if _m.get(k)))
            nb = QLabel(f"背包里没有技能书（技能书可从商店「{_kinds}」类商品购得，或战斗掉落）")
            nb.setStyleSheet("color:#565f89;")
            nb.setWordWrap(True)
            self.book_lay.addWidget(nb)
        for b in books:
            row = QHBoxLayout()
            info = QLabel(f"《{b.name}》 — 可习得「{(b.teach_skill or {}).get('name', '?')}」")
            info.setStyleSheet("color:#c0caf5;")
            row.addWidget(info, 1)
            read_btn = QPushButton("研读")
            read_btn.clicked.connect(lambda _=False, book=b: self._read_book(book))
            row.addWidget(read_btn)
            host = QWidget()
            host.setLayout(row)
            self.book_lay.addWidget(host)

    def _skill_card(self, sk: dict, by_id: dict) -> QFrame:
        card = QFrame()
        # [深改] gameCard 化（旧 #11131c 底与面板无对比，卡浮不起来）
        card.setObjectName("gameCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay = QGridLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(2)
        # [!] 列拉伸：中间文字列吃掉多余宽度，图标列/按钮列只取最小——卡片总宽恒等于
        # 视口宽，横向滚动条无内容可滚（此前熟练度条 setFixedSize(300, 8) 把第 0 列
        # 撑到 300，三列相加超视口即出横向滚动条）。
        lay.setColumnStretch(0, 0)
        lay.setColumnStretch(1, 1)
        lay.setColumnStretch(2, 0)
        lv = ce.skill_level(sk)
        xp = max(0, int(sk.get("xp", 0) or 0))
        need = ce.skill_xp_needed(lv) if lv < ce._SKILL_MAX_LEVEL else 0
        stype = skill_type_zh(sk.get("type"))
        # [技能图标] 卡片首列图标（36px，有图用图，无图类型色 + 首字兜底）
        try:
            icon_lbl = QLabel()
            icon_lbl.setFixedSize(36, 36)
            icon_lbl.setAlignment(Qt.AlignCenter)
            _pm = skill_icon_pixmap(sk, 36)
            icon_lbl.setPixmap(_pm)
            lay.addWidget(icon_lbl, 0, 0, 2, 1, Qt.AlignTop)
        except Exception:
            pass
        name_lbl = QLabel(f"{sk.get('name', '技能')}　Lv{lv}")
        name_lbl.setStyleSheet("color:#e0af68; font-weight:bold; font-size:14px;")
        name_lbl.setWordWrap(True)
        lay.addWidget(name_lbl, 0, 1)
        meta = QLabel(f"{stype} · 威力 {int(sk.get('power', 0) or 0)}"
                      + (f"（实际 {int((sk.get('power', 0) or 0) * ce.skill_power_mult(sk))}）" if lv > 1 else "")
                      + f" · {int(sk.get('cost_mp', 0) or 0)}蓝 · 冷却{int(sk.get('cooldown', 0) or 0)}")
        meta.setStyleSheet("color:#9aa5ce;")
        meta.setWordWrap(True)
        lay.addWidget(meta, 1, 1)
        # 熟练度行（容器占中间列整宽：条自适应拉伸，满格即列宽，不再定宽撑出横向滚动）
        if need > 0:
            pct = int(xp * 100 / need)
            prow = QWidget()
            play = QHBoxLayout(prow)
            play.setContentsMargins(0, 0, 0, 0)
            play.setSpacing(8)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(max(0, min(100, pct)))
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            bar.setStyleSheet(
                "QProgressBar{background:#1a1b26; border-radius:4px;}"
                "QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 #9ece6a, stop:1 #73daca); border-radius:4px;}")
            play.addWidget(bar, 1)
            xp_lbl = QLabel(f"熟练 {xp}/{need}")
            xp_lbl.setStyleSheet("color:#565f89; font-size:11px;")
            play.addWidget(xp_lbl, 0)
            lay.addWidget(prow, 2, 1)
        else:
            maxed = QLabel("熟练度已满")
            maxed.setStyleSheet("color:#9ece6a; font-size:11px;")
            lay.addWidget(maxed, 2, 1)
        # 装卸按钮
        sid = self._skill_id(sk)
        eq = self._equipped_ids()
        if sid in eq:
            btn = QPushButton("卸下")
            btn.setFixedWidth(56)
            btn.clicked.connect(lambda _=False, s=sid: self._unequip(s))
        else:
            btn = QPushButton("装备")
            btn.setFixedWidth(56)
            btn.setEnabled(len(eq) < SKILL_SLOTS)
            btn.clicked.connect(lambda _=False, s=sid: self._equip(s))
        lay.addWidget(btn, 0, 2, 2, 1)
        return card

    # ------------------------------------------------------------------
    def _equip(self, sid: str):
        p = self.world.player
        eq = self._equipped_ids()
        if sid not in eq and len(eq) < SKILL_SLOTS:
            eq.append(sid)
            p.equipped_skills = eq
        self._changed()
        self._refresh()

    def _unequip(self, sid: str):
        p = self.world.player
        p.equipped_skills = [s for s in self._equipped_ids() if s != sid]
        self._changed()
        self._refresh()

    def _cycle_slot(self, idx: int):
        """点已占用槽：弹选择（换成其他已学技能或卸下）。点空槽：直接装第一个未装的。"""
        p = self.world.player
        eq = self._equipped_ids()
        unlearned = [self._skill_id(s) for s in (p.skills or [])
                     if isinstance(s, dict) and self._skill_id(s) not in eq]
        if idx < len(eq) and eq[idx]:
            # 已占用：卸下（简单交互；换装走列表「装备」按钮先卸后装）
            self._unequip(eq[idx])
            return
        if unlearned:
            self._equip(unlearned[0])

    def _read_book(self, book):
        p = self.world.player
        ts = getattr(book, "teach_skill", None)
        if not isinstance(ts, dict) or not ts.get("name"):
            QMessageBox.information(self, "无法研读", "这本书似乎不是技能书。")
            return
        if any(isinstance(s, dict) and s.get("name") == ts.get("name") for s in (p.skills or [])):
            QMessageBox.information(self, "已掌握", f"你已学会「{ts.get('name')}」，无需重复研读。")
            return
        from src.models.world import Skill
        # [技能书个体差异 2026-09-10] 与 _resolve_use_item 同口径：书 id 盐抖动 power
        # （品级定基准 + 每件确定性抖动；不改书内蓝图模板）
        from src.services.combat_engine import jitter_taught_skill
        sk = Skill.from_dict(jitter_taught_skill(ts, getattr(self.world, "id", ""), book.id))
        p.skills.append(sk.to_dict())
        if book.id in p.inventory:
            p.inventory.remove(book.id)      # 书消耗
        self._changed()
        QMessageBox.information(self, "研读成功",
                                f"习得技能「{sk.name}」！可在上方列表装备到出战栏。")
        self._refresh()

    def _changed(self):
        if self.on_changed:
            try:
                self.on_changed()
            except Exception:
                pass
