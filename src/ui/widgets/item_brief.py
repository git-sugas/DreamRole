"""世界模拟物品简报组件（图片 + 名称 + 悬浮全属性）。

[P12e] 背包/商店/图鉴/NPC 档案/合成共用的物品展示单元：
横排「物品图（有 icon 用图，无图品级色占位）| 名字 + 自定义副行」，完整属性
（品级/攻防/加成/耐久/描述/特效）走 setToolTip——行内不挤数值。

[P12f] 物品图完整显示：ComfyUI 生成的物品图常是竖构图全身图，
make_card_pixmap 的封面式居中裁剪会把图裁得只剩上半段；物品场景统一用
full_item_pixmap（等比缩放到 size 内、透明底居中、不裁剪内容）。
"""
from __future__ import annotations
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QColor, QBrush, QPainter, QFont
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel

from src.config import paths
from src.models.world import Item
from src.models.world_sim_preset import GenreText

_RARITY_COLOR = {
    "common": "#d7dae0", "uncommon": "#9ece6a", "rare": "#7aa2f7",
    "epic": "#bb9af7", "legendary": "#e0a860", "mythic": "#e05555",
}

_CARD_COLORS = ["#2d3561", "#33467a", "#3b4261", "#2a4a5a", "#443a6b"]


def display_name(it: Item) -> str:
    """[P34b] 物品显示名：未鉴定装备打码 + 鉴定后装备前缀名前置。

    - 未鉴定装备（identified=False 且 type in weapon/armor/accessory）-> "未鉴定·{name}"。
    - 已鉴定装备有 affix 前缀名 -> "{前缀名}{name}"（prefix 前置；多 prefix 取首个）。
    - 其余（材料/消耗品/已鉴定无 affix）-> 原 name。
    [!] 纯展示，不写回 item.name（守 §13 纯展示契约）。
    """
    name = getattr(it, "name", "") or ""
    t = getattr(it, "type", "") or ""
    is_equip = t in ("weapon", "armor", "accessory")
    if is_equip and getattr(it, "identified", True) is False:
        return f"未鉴定·{name}"
    if is_equip:
        affs = getattr(it, "affixes", None) or []
        for a in affs:
            if isinstance(a, dict) and a.get("slot") == "prefix" and a.get("name"):
                return f"{a['name']}{name}"
    return name


def kind_label(world, it: Item, gt: GenreText | None = None) -> str:
    """[2026-08-23 三次真人测试] 物品类别显示名：可装备（slot 非空）一律按 slot
    题材名显示——与装备栏纸娃娃槽名严格一致（玉佩/储物戒/本命法宝/法袍…），
    不再用笼统 type 名（灵饰/法宝 这类统称会让玩家对不上槽位）；不可装备才按
    type 题材名。全题材通用（gt.slot 读各题材槽名）。"""
    if gt is None:
        gt = GenreText(getattr(world, "config_overlay", None) or {})
    slot = getattr(it, "slot", "") or ""
    if slot:
        return gt.slot(slot)
    return gt.item_type(getattr(it, "type", "") or "")


def full_item_pixmap(name: str, image_filename: str, size: int) -> QPixmap:
    """物品缩略图（完整显示版）：等比缩放进 size x size 透明底居中，不裁剪内容。

    无图时回退占位色块 + 名字首字（与 make_card_pixmap 无图分支同观感）。
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    loaded = False
    if image_filename:
        path = os.path.join(paths.world_images_dir(), image_filename)
        if os.path.exists(path):
            img = QPixmap(path)
            if not img.isNull():
                img = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                p.drawPixmap((size - img.width()) // 2, (size - img.height()) // 2, img)
                loaded = True
    if not loaded:
        color = _CARD_COLORS[hash(name) % len(_CARD_COLORS)] if name else "#565f89"
        p.setBrush(QBrush(QColor(color)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, size, size, 8, 8)
        p.setPen(QColor("#1a1b26"))
        p.setFont(QFont("Microsoft YaHei", size // 2, QFont.Bold))
        p.drawText(pix.rect(), Qt.AlignCenter, (name[0] if name else "?"))
    p.end()
    return pix


def item_tooltip(world, it: Item, gt: GenreText | None = None) -> str:
    """物品完整属性悬浮提示文本（题材化品级/类型名）。"""
    if gt is None:
        gt = GenreText(getattr(world, "config_overlay", None) or {})
    lines = [f"【{display_name(it)}】 {gt.rarity(it.rarity)} · {kind_label(world, it, gt)}"]
    # [P34b] 未鉴定装备属性打码（不显示攻防/词条，仅品级+类型）
    is_equip = getattr(it, "type", "") in ("weapon", "armor", "accessory")
    if is_equip and getattr(it, "identified", True) is False:
        lines.append("?? 未鉴定，属性未知 ??")
        if getattr(it, "desc", ""):
            lines.append(it.desc)
        return "\n".join(lines)
    stats = []
    if it.attack:
        stats.append(f"攻+{it.attack}")
    if it.defense:
        stats.append(f"防+{it.defense}")
    # [百分比回血 2026-09-01] heal_pct 优先显示（旧 heal_amount 兜底）
    if int(getattr(it, "heal_pct", 0) or 0) > 0:
        stats.append(f"回血+{it.heal_pct}%")
    elif it.heal_amount:
        stats.append(f"回血+{it.heal_amount}")
    if it.stat_bonus:
        sdn = (getattr(world, "config_overlay", {}) or {}).get("stat_display_names", {})
        stats.append(" ".join(f"{sdn.get(k, k)}+{v}" for k, v in it.stat_bonus.items()))
    if stats:
        lines.append("  ".join(stats))
    # [P34b] affix 词条显示（已鉴定装备）
    affs = getattr(it, "affixes", None) or []
    if is_equip and affs:
        aff_lines = []
        for a in affs:
            if not isinstance(a, dict):
                continue
            an = a.get("name", "")
            mods = a.get("mods") if isinstance(a.get("mods"), dict) else a
            parts = []
            # [!] mods key 与 combat_engine._affix_val 一致：atk/def/crit/magic_atk + stat_bonus
            if mods.get("atk"):
                parts.append(f"攻+{mods['atk']}")
            if mods.get("def"):
                parts.append(f"防+{mods['def']}")
            if mods.get("magic_atk"):
                parts.append(f"法攻+{mods['magic_atk']}")
            if mods.get("crit"):
                parts.append(f"暴击+{round(float(mods['crit'])*100,1)}%")
            # [装备新属性 2026-09-01] 概率型三件套 + 武器元素（key 与 compute_stats 一致）
            if mods.get("combo_rate"):
                parts.append(f"连击+{round(float(mods['combo_rate'])*100,1)}%")
            if mods.get("counter_rate"):
                parts.append(f"反击+{round(float(mods['counter_rate'])*100,1)}%")
            if mods.get("lifesteal"):
                parts.append(f"吸血+{round(float(mods['lifesteal'])*100,1)}%")
            _el = str(mods.get("element", "") or "").strip()
            if _el:
                # [方案 B 2026-09-10 用户拍板] 元素双向生效 -> 附伤行同时标出受克元素，
                # 让玩家能预期「武器附火也会让我在挨打时吃元素关系」；脏值（非串/空）跳过。
                from src.services.combat_engine import ELEMENT_ZH, element_countered_by
                _el_zh = ELEMENT_ZH.get(_el, _el)
                _cv = element_countered_by(_el)
                parts.append(f"附{_el_zh}伤" + (f"（受{ELEMENT_ZH.get(_cv, _cv)}克）" if _cv else ""))
            sb = mods.get("stat_bonus")
            if isinstance(sb, dict) and sb:
                sdn = (getattr(world, "config_overlay", {}) or {}).get("stat_display_names", {})
                parts.append(" ".join(f"{sdn.get(k,k)}+{v}" for k, v in sb.items()))
            aff_lines.append(f"{an}（{' '.join(parts)}）" if parts else an)
        if aff_lines:
            lines.append("词条：" + " / ".join(aff_lines))
    dm = int(getattr(it, "durability_max", 0) or 0)
    if dm > 0:
        lines.append(f"耐久 {int(getattr(it, 'durability', 0) or 0)}/{dm}")
    elif dm == 0 and getattr(it, "durability", 100) != 100:
        lines.append("不朽")
    if getattr(it, "desc", ""):
        lines.append(it.desc)
    # [消耗品结构化效果] 显示 consume_effect（题材化属性名）
    # [heal_full 并入 heal_pct 2026-09-10] 纯回血只看顶层「回血+N%」行（112 行），
    # 此处不再为 heal_full 旧别名单独立分支（正常流程已迁空；漏网旧档由 effects 行兜底）。
    eff = getattr(it, "consume_effect", None)
    if isinstance(eff, dict) and eff:
        etype = str(eff.get("type", "") or "")
        if etype == "stat_bonus":
            st = eff.get("stats") or {}
            txt = " ".join(f"{gt.stat(k)}+{v}" for k, v in st.items())
            lines.append(f"使用：{txt}（永久）" if txt else "使用：无效果")
        elif etype == "heal_mp":
            # [修 2026-09-10] 原同一行连写两遍 + 缺 %（契约 heal_mp amount = 百分数 1-100）
            lines.append(f"使用：回蓝+{eff.get('amount', 0)}%")
        elif etype == "cure":
            lines.append("使用：清除负面状态")
        elif etype == "revive":
            lines.append("使用：复活（倒下时自动生效）")
    if getattr(it, "effects", ""):
        lines.append(f"效果：{it.effects}")
    return "\n".join(lines)


class ItemBriefRow(QWidget):
    """物品简报行：[icon | 名字 + 副行]，悬浮全属性。副行由调用方给（价格/库存等）。"""

    def __init__(self, world, it: Item, sub_text: str = "", icon_size: int = 40,
                 gt: GenreText | None = None, parent=None):
        super().__init__(parent)
        # [!] 布局保命：真实 Windows 字体比离屏高，行高不足时 fixed 尺寸的 icon
        # 会溢出父边界被裁剪（只显示上半截）——给自身最小高度兜底。
        self.setMinimumHeight(icon_size + 8)
        if gt is None:
            gt = GenreText(getattr(world, "config_overlay", None) or {})
        self._gt = gt
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.icon_lbl = QLabel()
        self.icon_lbl.setFixedSize(icon_size, icon_size)
        self.icon_lbl.setAlignment(Qt.AlignCenter)
        self.icon_lbl.setPixmap(full_item_pixmap(it.name, getattr(it, "icon", ""), icon_size))
        row.addWidget(self.icon_lbl, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.name_lbl = QLabel(display_name(it))
        self.name_lbl.setStyleSheet(
            f"color:{_RARITY_COLOR.get(it.rarity, '#c0caf5')}; font-weight:bold;")
        self.name_lbl.setWordWrap(True)
        col.addWidget(self.name_lbl)
        self.sub_lbl = QLabel(sub_text)
        self.sub_lbl.setStyleSheet("color:#9aa5ce; font-size:11px;")
        self.sub_lbl.setWordWrap(True)
        col.addWidget(self.sub_lbl)
        row.addLayout(col, 1)
        tip = item_tooltip(world, it, gt)
        self.setToolTip(tip)
        self.setToolTipDuration(10000)
