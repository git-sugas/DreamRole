"""可点击角色头像按钮。"""
from __future__ import annotations
import os
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QBrush, QPen, QMovie
from PySide6.QtWidgets import QPushButton, QLabel

from src.config import paths

# 文字头像背景色调色板
_AVATAR_COLORS = ["#7aa2f7", "#bb9af7", "#9ece6a", "#e0af68", "#f7768e", "#7dcfff"]


def is_gif_avatar(avatar: str) -> bool:
    """判断头像文件名是否为 GIF 动图且存在。"""
    if not avatar:
        return False
    if not avatar.lower().endswith(".gif"):
        return False
    return os.path.exists(os.path.join(paths.avatars_dir(), avatar))


def _make_circle_mask(size: int) -> QPixmap:
    """生成圆形透明遮罩，用于逐帧裁剪 GIF。"""
    mask = QPixmap(size, size)
    mask.fill(Qt.transparent)
    mp = QPainter(mask)
    mp.setRenderHint(QPainter.Antialiasing)
    mp.setBrush(QBrush(Qt.white))
    mp.setPen(Qt.NoPen)
    mp.drawEllipse(0, 0, size, size)
    mp.end()
    return mask


def render_avatar(target, name: str, avatar: str, size: int):
    """
    将静态或 GIF 头像渲染到 QLabel（setPixmap）或 QPushButton（setIcon），均做圆形裁剪。
    - 静态图（png/jpg/webp）：直接用 make_avatar_pixmap 生成单帧。
    - GIF：用 QMovie 逐帧圆形裁剪后推送到目标控件。
    同一 target 再次调用会先停掉旧 QMovie，避免泄漏与残留动画。
    """
    # 停止并清除上一次绑定的 QMovie
    old = getattr(target, "_avatar_movie", None)
    if old is not None:
        old.stop()
        try:
            old.frameChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        target._avatar_movie = None

    is_btn = isinstance(target, QPushButton)

    if is_gif_avatar(avatar):
        path = os.path.join(paths.avatars_dir(), avatar)
        movie = QMovie(path)
        if not movie.isValid():
            # GIF 损坏则回退静态占位
            pix = make_avatar_pixmap(name, avatar, size)
            if is_btn:
                target.setIcon(pix)
                target.setIconSize(pix.size())
            else:
                target.setPixmap(pix)
            return
        movie.start()
        target._avatar_movie = movie
        mask = _make_circle_mask(size)

        def _on_frame():
            pix = movie.currentPixmap()
            if pix.isNull():
                return
            pix = pix.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            pix.setMask(mask.mask())
            if is_btn:
                target.setIcon(pix)
                target.setIconSize(pix.size())
            else:
                target.setPixmap(pix)

        movie.frameChanged.connect(_on_frame)
        _on_frame()  # 立即触发首帧
        return

    # 静态头像或无头像
    pix = make_avatar_pixmap(name, avatar, size)
    if is_btn:
        target.setIcon(pix)
        target.setIconSize(pix.size())
    else:
        target.setPixmap(pix)


def make_avatar_pixmap(name: str, avatar: str = "", size: int = 56) -> QPixmap:
    """
    生成圆形头像 QPixmap。
    - 优先加载 avatar 文件名（存于 avatars 目录）并圆形裁剪；
    - 无头像时回退为背景色 + 首字母。
    供 AvatarButton / MessageBubble / 角色编辑器预览共用。
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    avatar_loaded = False
    if avatar:
        avatar_path = os.path.join(paths.avatars_dir(), avatar)
        if os.path.exists(avatar_path):
            img = QPixmap(avatar_path)
            if not img.isNull():
                img = img.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                # 圆形裁剪
                mask = QPixmap(size, size)
                mask.fill(Qt.transparent)
                mp = QPainter(mask)
                mp.setRenderHint(QPainter.Antialiasing)
                mp.setBrush(QBrush(Qt.white))
                mp.setPen(Qt.NoPen)
                mp.drawEllipse(0, 0, size, size)
                mp.end()
                img.setMask(mask.mask())
                painter.drawPixmap(0, 0, img)
                avatar_loaded = True

    if not avatar_loaded:
        color = _AVATAR_COLORS[hash(name) % len(_AVATAR_COLORS)] if name else "#565f89"
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, size, size)
        painter.setPen(QColor("#1a1b26"))
        font = QFont("Microsoft YaHei", size // 3, QFont.Bold)
        painter.setFont(font)
        char = name[0] if name else "?"
        painter.drawText(pix.rect(), Qt.AlignCenter, char)

    painter.end()
    return pix


class AvatarButton(QPushButton):
    """圆形头像按钮，点击触发角色发言（群聊手动模式）。"""

    clicked_character = Signal(str)  # character_id

    def __init__(self, character_id: str = "", name: str = "", avatar: str = "", size: int = 56, parent=None):
        super().__init__(parent)
        self.character_id = character_id
        self.name = name
        self.avatar = avatar
        self._size = size
        self.setFixedSize(size, size)
        self.setObjectName("avatarBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(name)
        self._refresh()

    def _refresh(self):
        render_avatar(self, self.name, self.avatar, self._size)
        self.setText("")

    def set_selected(self, selected: bool):
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def update_character(self, character_id: str, name: str, avatar: str = ""):
        self.character_id = character_id
        self.name = name
        self.avatar = avatar
        self.setToolTip(name)
        self._refresh()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.character_id:
            self.clicked_character.emit(self.character_id)
        super().mousePressEvent(event)