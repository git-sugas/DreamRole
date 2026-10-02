"""技能图标组件（战斗按钮 / 技能页 / 宠物技能共用）。

口径对齐物品 `item_brief.full_item_pixmap`：有预生成图（skill_{key}.png）用图，
无图用类型色占位块 + 技能名首字，保证按钮永远有图标不秃。

内容键与 WorldSimService._combat_image_key 同口径（md5(name|desc[:60])[:16]），
此处本地实现避免循环 import（widgets <- services 方向不可反）。
"""
from __future__ import annotations
import hashlib
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QColor, QBrush, QPainter, QFont

from src.config import paths

_SKILL_TYPE_COLOR = {
    "attack": "#f7768e",   # 攻击红（呼应战斗房/暴击色）
    "heal": "#9ece6a",     # 恢复绿（呼应喝药/休整色）
    "buff": "#7aa2f7",     # 增益蓝（呼应机关/护盾色）
}

_SKILL_TYPE_ZH = {"attack": "攻击", "heal": "恢复", "buff": "增益"}


def skill_type_color(sk_type: str) -> str:
    """技能类型色（未知回退中性灰）。"""
    return _SKILL_TYPE_COLOR.get(str(sk_type or ""), "#565f89")


def _skill_field(sk, key: str) -> str:
    if isinstance(sk, dict):
        return str(sk.get(key, "") or "")
    return str(getattr(sk, key, "") or "")


def skill_image_key(sk) -> str | None:
    """内容键（同 WorldSimService._combat_image_key）：无名返回 None。"""
    name = _skill_field(sk, "name").strip()
    if not name:
        return None
    desc = _skill_field(sk, "desc")[:60]
    return hashlib.md5(f"{name}|{desc}".encode("utf-8")).hexdigest()[:16]


def resolve_skill_icon_file(sk, icon_file: str | None = None) -> str | None:
    """解出可用的技能图标全路径（有图返回路径，无图 None）。

    icon_file 兼容两种输入：全路径 / world_images 下文件名 / None（按内容键自查）。
    """
    if icon_file:
        if os.path.isabs(icon_file):
            return icon_file if os.path.exists(icon_file) else None
        cand = os.path.join(paths.world_images_dir(), icon_file)
        if os.path.exists(cand):
            return cand
        # 文件名未命中时继续按内容键自查（svc 缓存键漂移时兜底）
    key = skill_image_key(sk)
    if key is None:
        return None
    cand = os.path.join(paths.world_images_dir(), f"skill_{key}.png")
    return cand if os.path.exists(cand) else None


def skill_icon_pixmap(sk, size: int, icon_file: str | None = None) -> QPixmap:
    """技能图标（等比完整显示版）：有图用图，无图类型色 + 首字兜底（同物品口径）。"""
    name = _skill_field(sk, "name").strip()
    sk_type = _skill_field(sk, "type").strip() or "attack"
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    loaded = False
    fp = resolve_skill_icon_file(sk, icon_file)
    if fp:
        img = QPixmap(fp)
        if not img.isNull():
            img = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap((size - img.width()) // 2, (size - img.height()) // 2, img)
            loaded = True
    if not loaded:
        p.setBrush(QBrush(QColor(skill_type_color(sk_type))))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, size, size, 8, 8)
        p.setPen(QColor("#1a1b26"))
        p.setFont(QFont("Microsoft YaHei", size // 2, QFont.Bold))
        p.drawText(pix.rect(), Qt.AlignCenter, (name[0] if name else "?"))
    p.end()
    return pix


def skill_type_zh(sk_type: str) -> str:
    """技能类型中文名（未知透传原文）。"""
    t = str(sk_type or "")
    return _SKILL_TYPE_ZH.get(t, t or "攻击")
