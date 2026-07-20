"""消息气泡组件：用户消息、AI消息、总结、折叠块、图片。"""
from __future__ import annotations
import os
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QFont, QMouseEvent
from PySide6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout, QFrame, QSizePolicy,
    QPushButton, QMenu, QGraphicsOpacityEffect,
)

from src.models import Message
from src.config import paths
from src.utils.helpers import format_tokens
from src.utils.tokenizer import count_tokens
from src.utils.markup import render as render_markup
from src.ui.widgets.avatar_button import make_avatar_pixmap, render_avatar


class MessageBubble(QFrame):
    """单条消息气泡。"""
    context_menu_requested = Signal(Message, object)  # (message, global_pos)
    avatar_clicked = Signal(Message)  # 点击头像预览大图（message 携带 avatar/character_name）

    def __init__(self, message: Message, max_width: int = 600, avatar: str = "", parent=None):
        super().__init__(parent)
        self.message = message
        self.max_width = max_width
        self.avatar = avatar
        self._is_user = message.role == "user"
        self._is_summary = message.is_summary
        self._build_ui()

    def _build_ui(self):
        if self._is_summary:
            self._build_summary()
            return
        if self.message.is_image_only and self.message.image_path:
            self._build_image_only()
            return

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 头像（AI 左侧、用户右侧）
        self.avatar_label = QLabel()
        avatar_size = 36
        self.avatar_label.setFixedSize(avatar_size, avatar_size)
        render_avatar(
            self.avatar_label,
            self.message.character_name or "用户",
            # 用户消息现在也支持图 avatar（由 chat_view.set_user_avatar 传入）；
            # 无 avatar 时 make_avatar_pixmap/render_avatar 走首字占位图。
            self.avatar,
            avatar_size,
        )
        # 头像可点击预览大图：手型光标提示可点，左键点击发 avatar_clicked 信号。
        # [!] 无 avatar（首字占位）也响应点击（预览会提示无图），保持交互一致。
        self.avatar_label.setCursor(Qt.PointingHandCursor)
        self.avatar_label.mousePressEvent = self._on_avatar_click

        # 气泡容器
        bubble = QFrame()
        bubble.setObjectName("userBubble" if self._is_user else "aiBubble")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(10, 8, 10, 8)
        bubble_layout.setSpacing(8)

        # 发言者名 + 时间
        header = QHBoxLayout()
        name_label = QLabel(self.message.character_name or "用户")
        name_label.setObjectName("charNameLabel")
        name_label.setStyleSheet("font-size: 12px;")
        time_str = ""
        try:
            dt = datetime.fromisoformat(self.message.timestamp)
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass
        time_label = QLabel(time_str)
        time_label.setStyleSheet("color: #565f89; font-size: 11px;")
        header.addWidget(name_label)
        header.addWidget(time_label)
        header.addStretch()
        if self.message.tokens > 0:
            tk_label = QLabel(f"{format_tokens(self.message.tokens)} tok")
            tk_label.setStyleSheet("color: #565f89; font-size: 11px;")
            header.addWidget(tk_label)
        bubble_layout.addLayout(header)

        self._bubble_layout = bubble_layout  # 供动态角标使用

        # 「已停止」角标（构造时若消息已标记则立即显示）
        self._stopped_label: QLabel | None = None
        if self.message.is_stopped:
            self.set_stopped(True)

        # 内容（富文本：对话/旁白/心声/符号分色显示）
        self.content_label = QLabel()
        self.content_label.setWordWrap(True)
        self.content_label.setTextFormat(Qt.RichText)
        self.content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # [!] QLabel + RichText + WordWrap 默认按"自然宽度"换行（远小于 max_width
        # 就提前换行，右边空一大半）。设 minimumWidth 强制顶到接近 max_width，
        # 文本才会真正利用气泡宽度到接近上限才换行。下限 200 防窄窗溢出。
        self.content_label.setMinimumWidth(max(200, self.max_width - 20))
        self.content_label.setMaximumWidth(self.max_width)
        self.content_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.content_label.setText(render_markup(self.message.content, self._is_user))
        # 子控件右键需冒泡到气泡，由气泡统一触发上下文菜单
        self.content_label.mousePressEvent = self._child_mouse_press
        bubble_layout.addWidget(self.content_label)

        # 图片
        if self.message.image_path:
            img_label = self._make_image_label(self.message.image_path)
            if img_label:
                bubble_layout.addWidget(img_label)

        bubble.setMaximumWidth(self.max_width + 40)
        bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        if self._is_user:
            layout.addStretch()
            layout.addWidget(bubble)
            layout.addWidget(self.avatar_label, alignment=Qt.AlignTop)
        else:
            layout.addWidget(self.avatar_label, alignment=Qt.AlignTop)
            layout.addWidget(bubble)
            layout.addStretch()

    def _build_summary(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        bubble = QFrame()
        bubble.setObjectName("summaryBubble")
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 8)
        title = QLabel("[上文总结]")
        title.setStyleSheet("color: #e0af68; font-size: 12px; font-weight: bold;")
        bl.addWidget(title)
        content = QLabel()
        content.setWordWrap(True)
        content.setTextFormat(Qt.RichText)
        content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # [!] 与普通气泡同理：QLabel + RichText + WordWrap 默认按"自然宽度"换行
        # （远小于 max_width 就提前换行，右边空一大半）。设 minimumWidth 强制顶到
        # 接近 max_width，文本才会真正利用气泡宽度到接近上限才换行。
        content.setMinimumWidth(max(200, self.max_width - 20))
        content.setMaximumWidth(self.max_width)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        content.setText(render_markup(self.message.content, is_user=False))
        bl.addWidget(content)
        bubble.setMaximumWidth(self.max_width + 40)
        layout.addStretch()
        layout.addWidget(bubble)
        layout.addStretch()

    def _build_image_only(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        img_label = self._make_image_label(self.message.image_path)
        self._img_label = img_label  # 保存引用，供 update_image 刷新
        if img_label:
            bubble = QFrame()
            bubble.setObjectName("aiBubble")
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(8, 8, 8, 8)
            bl.addWidget(img_label)
            cap = QLabel(self.message.content or "生成的图片")
            cap.setStyleSheet("color: #565f89; font-size: 11px;")
            bl.addWidget(cap)
            bubble.setMaximumWidth(self.max_width + 40)
            layout.addStretch()
            layout.addWidget(bubble)
            layout.addStretch()

    def update_image(self, new_path: str):
        """刷新图片气泡的图片（重新生成后用）。"""
        self.message.image_path = new_path
        # 重建 img_label（尺寸可能不同，直接替换 pixmap 也要重算缩放，重建更简单）
        new_label = self._make_image_label(new_path)
        old_label = getattr(self, "_img_label", None)
        if new_label and old_label:
            # 替换布局中的 widget：找到 old_label 的位置插入 new_label 再删 old
            parent_layout = old_label.parent().layout()
            if parent_layout is not None:
                idx = parent_layout.indexOf(old_label)
                parent_layout.insertWidget(idx, new_label)
                parent_layout.removeWidget(old_label)
                old_label.setParent(None)
                old_label.deleteLater()
                self._img_label = new_label

    def _make_image_label(self, path: str) -> QLabel | None:
        if not os.path.exists(path):
            return None
        pix = QPixmap(path)
        if pix.isNull():
            return None
        if pix.width() > self.max_width:
            pix = pix.scaledToWidth(self.max_width, Qt.SmoothTransformation)
        label = QLabel()
        label.setPixmap(pix)
        label.setAlignment(Qt.AlignCenter)
        return label

    def update_content(self, text: str):
        """流式更新内容（富文本渲染）。"""
        if hasattr(self, "content_label"):
            self.content_label.setText(render_markup(text, self._is_user))

    def update_avatar(self, name: str, avatar: str = ""):
        """更新头像（占位气泡升级为正式消息时用）。"""
        self.avatar = avatar
        if hasattr(self, "avatar_label"):
            size = self.avatar_label.width() or 36
            render_avatar(
                self.avatar_label,
                name or "用户",
                # 用户消息同样允许 avatar（与构造处一致放开）
                avatar,
                size,
            )

    def set_stopped(self, stopped: bool):
        """显示/隐藏「已停止」角标（区分完整回复与被中断的部分回复）。"""
        if not hasattr(self, "_bubble_layout"):
            return  # 总结/纯图片气泡等无 bubble_layout 的不处理
        if self._stopped_label is None:
            lbl = QLabel("⏹ 已停止")
            lbl.setObjectName("stoppedLabel")
            lbl.setStyleSheet("color: #f7768e; font-size: 11px; font-style: italic;")
            self._stopped_label = lbl
        if stopped:
            if self._stopped_label.parent() is None:
                self._bubble_layout.addWidget(self._stopped_label)
            self._stopped_label.show()
        else:
            if self._stopped_label is not None:
                self._stopped_label.hide()

    def append_content(self, text: str):
        """流式追加内容（重新渲染整段）。"""
        if hasattr(self, "content_label"):
            # 缓存累积的纯文本，每 chunk 重新渲染整段保证闭合正确
            if not hasattr(self, "_stream_text"):
                self._stream_text = ""
            self._stream_text += text
            self.content_label.setText(render_markup(self._stream_text, self._is_user))

    def _child_mouse_press(self, event: QMouseEvent):
        """子控件（内容文本）右键冒泡到气泡，触发上下文菜单；其余事件走默认行为。"""
        if event.button() == Qt.RightButton:
            self.context_menu_requested.emit(self.message, event.globalPosition().toPoint())
        else:
            # 保留文本选择/交互
            QLabel.mousePressEvent(self.content_label, event)

    def _on_avatar_click(self, event: QMouseEvent):
        """点击头像：左键发 avatar_clicked 信号预览大图，右键走默认（不拦截）。"""
        if event.button() == Qt.LeftButton:
            self.avatar_clicked.emit(self.message)
        else:
            QLabel.mousePressEvent(self.avatar_label, event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.RightButton:
            self.context_menu_requested.emit(self.message, event.globalPosition().toPoint())
        super().mousePressEvent(event)


class CollapsedBlock(QFrame):
    """折叠楼层块：显示已折叠的消息数量，可展开查看。

    性能：折叠状态下**不渲染**子气泡（避免对大量被总结/折叠的消息逐条
    render_markup）。首次展开时才创建子 MessageBubble；收起后保留已创建
    的气泡（仅 setVisible(False)），再次展开直接显示，不重复渲染。
    """
    expand_requested = Signal(list)  # list of Message

    def __init__(self, messages: list[Message], reason: str = "auto_summary", parent=None):
        super().__init__(parent)
        self.messages = messages
        self.setObjectName("collapsedBlock")
        self._expanded = False
        self._built = False  # 子气泡是否已创建
        self._build_ui(reason)

    def _build_ui(self, reason: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        reason_text = "自动总结" if reason == "auto_summary" else "手动折叠"
        header = QHBoxLayout()
        label = QLabel(f"[{reason_text}] 已折叠 {len(self.messages)} 条消息")
        label.setStyleSheet("color: #565f89; font-size: 12px;")
        header.addWidget(label)
        header.addStretch()

        self.toggle_btn = QPushButton("展开" if not self._expanded else "收起")
        self.toggle_btn.setFixedWidth(60)
        self.toggle_btn.setStyleSheet("font-size: 12px; padding: 2px 8px;")
        self.toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self.toggle_btn)
        layout.addLayout(header)

        # 占位容器：折叠时不创建任何子气泡（省去逐条 render_markup 的开销），
        # 首次展开时才填充。
        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_widget.setVisible(False)
        layout.addWidget(self.content_widget)

    def _ensure_built(self):
        """首次展开时创建子气泡（懒渲染）。"""
        if self._built:
            return
        for msg in self.messages:
            mb = MessageBubble(msg, max_width=500)
            # [!] MessageBubble 构造时不连接 context_menu_requested 信号，故此处
            # 不再调 disconnect()（旧代码调了会触发 PySide6 RuntimeWarning:
            # "Failed to disconnect (None) from signal"）。折叠块内子气泡本就
            # 不需要响应右键菜单--构造时不连，自然不会 emit 到外部。若未来改为
            # 构造时自动连接，再在此处断开即可（届时已有连接，disconnect 不会警告）。
            self.content_layout.addWidget(mb)
        self._built = True

    def _toggle(self):
        self._expanded = not self._expanded
        if self._expanded:
            self._ensure_built()
        self.content_widget.setVisible(self._expanded)
        self.toggle_btn.setText("收起" if self._expanded else "展开")

    def refresh_rendered_bubbles(self):
        """用当前配色规则重新渲染已创建的子气泡（配色规则变更后调用）。

        未展开或未构建则无气泡可刷，安全跳过。
        """
        if not self._built:
            return
        for i in range(self.content_layout.count()):
            item = self.content_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None and hasattr(w, "message") and hasattr(w, "content_label") and hasattr(w, "_is_user"):
                w.content_label.setText(render_markup(w.message.content, w._is_user))