"""聊天视图：消息展示区域，支持流式更新、折叠块、图片。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QScrollArea, QWidget, QVBoxLayout, QLabel, QMenu, QMessageBox,
)

from src.models import Message
from src.ui.widgets.message_bubble import MessageBubble, CollapsedBlock
from src.utils.markup import render as render_markup


class ChatView(QScrollArea):
    """聊天消息展示区域。"""

    message_context_menu = Signal(Message, object)  # (message, global_pos)
    avatar_clicked = Signal(Message)  # 点击头像预览大图

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(8)
        self._layout.addStretch()
        self.setWidget(self._container)

        self._bubbles: dict[str, MessageBubble] = {}  # message_id -> bubble
        self._collapsed_blocks: list[CollapsedBlock] = []  # 当前已渲染的折叠块（用于刷新子气泡）
        self._current_streaming: MessageBubble | None = None
        self._streaming_placeholder_id: str | None = None  # 占位气泡的临时 id
        self._max_bubble_width = 600
        self._character_avatars: dict[str, str] = {}  # character_id -> avatar 文件名
        self._user_avatar: str = ""                    # 用户头像文件名（会话绑定 User 时设置）

    def set_character_avatars(self, avatars: dict[str, str]):
        """设置角色 id -> avatar 文件名映射，供气泡显示头像。"""
        self._character_avatars = avatars or {}

    def set_user_avatar(self, avatar: str):
        """设置当前会话的用户头像文件名（绑定 User 实体且 User 有头像时调用）。
        用户消息气泡右侧会显示此头像；为空则走 make_avatar_pixmap 的首字占位。"""
        self._user_avatar = avatar or ""

    def _avatar_for(self, msg: Message) -> str:
        """根据消息类型取头像文件名。
        用户消息走 _user_avatar；AI/角色消息走 character_id 映射。"""
        if msg.role == "user":
            return self._user_avatar
        return self._character_avatars.get(msg.character_id, "")

    def set_max_bubble_width(self, width: int):
        # 气泡上限取视口宽的 3/4（预留头像 + 边距 + 右侧留白），
        # 下限 300 防窄窗下塌成一行字。
        self._max_bubble_width = max(300, int(width * 0.75))

    def clear_messages(self):
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bubbles.clear()
        self._collapsed_blocks.clear()
        self._current_streaming = None

    def load_messages(self, messages: list[Message]):
        """加载完整消息列表，自动分组折叠消息。"""
        self.clear_messages()
        i = 0
        while i < len(messages):
            msg = messages[i]
            # 检测连续折叠的非总结消息
            if msg.collapsed and not msg.is_summary and not msg.is_image_only:
                collapsed_group = []
                reason = msg.collapsed_reason
                while (
                        i < len(messages)
                        and messages[i].collapsed
                        and not messages[i].is_summary
                        and not messages[i].is_image_only
                ):
                    collapsed_group.append(messages[i])
                    i += 1
                if collapsed_group:
                    block = CollapsedBlock(collapsed_group, reason)
                    self._collapsed_blocks.append(block)
                    self._insert_widget(block)
                continue
            self.add_message(msg, scroll=False)
            i += 1
        self.scroll_to_bottom()

    def add_message(self, msg: Message, scroll: bool = True):
        """添加单条消息。"""
        bubble = MessageBubble(msg, self._max_bubble_width, avatar=self._avatar_for(msg))
        bubble.context_menu_requested.connect(self._on_context_menu)
        bubble.avatar_clicked.connect(self._on_avatar_clicked)
        self._bubbles[msg.id] = bubble
        self._insert_widget(bubble)
        if scroll:
            self.scroll_to_bottom()

    def add_message_before_streaming(self, msg: Message, scroll: bool = True):
        """添加一条消息，插到当前流式占位气泡「之前」。

        延迟存储下，本轮 user 消息在 LLM 回复完成后才入库，但此时流式占位
        气泡已显示在底部。若直接 add_message 会把 user 气泡插到占位之后，
        造成 UI 顺序「LLM -> 用户」（历史正确顺序应是「用户 -> LLM」）。
        本方法把 user 气泡插到占位气泡之前，保证视觉顺序与历史顺序一致。
        无流式占位时退化为普通 add_message（插到末尾）。
        """
        bubble = MessageBubble(msg, self._max_bubble_width, avatar=self._avatar_for(msg))
        bubble.context_menu_requested.connect(self._on_context_menu)
        bubble.avatar_clicked.connect(self._on_avatar_clicked)
        self._bubbles[msg.id] = bubble
        placeholder = self._current_streaming
        if placeholder is not None:
            # 插到占位气泡之前（indexOf 返回占位在 layout 中的位置）
            idx = self._layout.indexOf(placeholder)
            if idx >= 0:
                self._layout.insertWidget(idx, bubble)
            else:
                self._insert_widget(bubble)  # 兜底：找不到占位就插末尾
        else:
            self._insert_widget(bubble)
        if scroll:
            self.scroll_to_bottom()

    def _insert_widget(self, widget):
        # 插入到 stretch 之前
        self._layout.insertWidget(self._layout.count() - 1, widget)

    def start_streaming(self, msg: Message):
        """开始流式消息（先创建占位气泡）。"""
        bubble = MessageBubble(msg, self._max_bubble_width, avatar=self._avatar_for(msg))
        bubble.context_menu_requested.connect(self._on_context_menu)
        bubble.avatar_clicked.connect(self._on_avatar_clicked)
        self._bubbles[msg.id] = bubble
        self._current_streaming = bubble
        self._streaming_placeholder_id = msg.id
        self._insert_widget(bubble)
        self.scroll_to_bottom()

    def finalize_streaming_to(self, real_msg: Message) -> bool:
        """
        将流式占位气泡「升级」为正式消息气泡：
        - 用正式消息内容替换占位消息内容
        - 把气泡登记键从占位 id 改为正式 id
        之后对正式消息的编辑/重试/删除即可定位到该气泡。
        """
        if not self._current_streaming or not self._streaming_placeholder_id:
            return False
        placeholder_id = self._streaming_placeholder_id
        bubble = self._bubbles.get(placeholder_id)
        if bubble is None:
            return False
        # 替换消息对象 & 重新渲染（含最终富文本）
        bubble.message = real_msg
        if hasattr(bubble, "content_label"):
            bubble.content_label.setText(render_markup(real_msg.content, bubble._is_user))
        # 更新头像（占位气泡用默认头像，正式消息有 character_id 可查真实头像）
        bubble.update_avatar(real_msg.character_name or "", self._avatar_for(real_msg))
        # 同步「已停止」角标（被中断并保存的部分回复）
        bubble.set_stopped(real_msg.is_stopped)
        # 更新登记键
        self._bubbles.pop(placeholder_id, None)
        self._bubbles[real_msg.id] = bubble
        self._current_streaming = None
        self._streaming_placeholder_id = None
        return True

    def update_streaming(self, text: str):
        """更新当前流式消息内容。"""
        if self._current_streaming:
            self._current_streaming.update_content(text)
            self.scroll_to_bottom()

    def append_streaming(self, chunk: str):
        """追加流式片段。"""
        if self._current_streaming:
            self._current_streaming.append_content(chunk)
            self.scroll_to_bottom()

    def finish_streaming(self):
        """结束流式。"""
        self._current_streaming = None
        self._streaming_placeholder_id = None

    def cancel_streaming(self):
        """
        取消生成时的占位气泡清理。
        - 若占位气泡内容为空（用户在收到任何文本前停止）→ 删除占位气泡，避免残留空气泡。
        - 若已有部分文本，则保留（orchestrator 会保存部分内容并通过 finalize_streaming_to 升级）。
        """
        placeholder_id = self._streaming_placeholder_id
        bubble = self._current_streaming
        # 判断占位气泡是否为空
        content_empty = True
        if bubble and hasattr(bubble, "_stream_text"):
            content_empty = not bubble._stream_text.strip()
        elif bubble and hasattr(bubble, "content_label"):
            content_empty = not bubble.message.content.strip()
        if content_empty and placeholder_id and placeholder_id in self._bubbles:
            self.delete_bubble(placeholder_id)
        self._current_streaming = None
        self._streaming_placeholder_id = None

    def add_image(self, image_path: str, caption: str = ""):
        """添加纯图片消息（不入上下文）。"""
        msg = Message(
            role="assistant",
            content=caption,
            image_path=image_path,
            is_image_only=True,
        )
        self.add_message(msg)

    def add_summary_block(self, msg: Message):
        """添加总结消息。"""
        self.add_message(msg)

    def refresh_bubble(self, msg: Message):
        """原地更新某条消息气泡的显示内容（编辑后用）。"""
        bubble = self._bubbles.get(msg.id)
        if not bubble:
            return
        # 更新气泡持有的消息对象内容，并重新渲染富文本
        bubble.message = msg
        # 图片气泡：刷新图片（重新生成后 image_path 已变）
        if msg.is_image_only and msg.image_path and hasattr(bubble, "update_image"):
            bubble.update_image(msg.image_path)
            return
        if hasattr(bubble, "content_label"):
            bubble.content_label.setText(
                render_markup(msg.content, bubble._is_user)
            )
        # 编辑后通常不再是「已停止」状态（内容已被人工修改）
        bubble.set_stopped(msg.is_stopped)

    def refresh_all_bubbles(self):
        """用当前配色规则重新渲染所有已显示气泡（配色规则变更后调用）。

        顶层气泡 + 已展开折叠块内的子气泡均刷新；未展开折叠块天然无渲染，
        展开时按新规则首次渲染，无需此处处理。
        """
        for bubble in self._bubbles.values():
            if hasattr(bubble, "content_label") and hasattr(bubble, "message"):
                bubble.content_label.setText(
                    render_markup(bubble.message.content, bubble._is_user)
                )
        for block in self._collapsed_blocks:
            block.refresh_rendered_bubbles()

    def delete_bubble(self, msg_id: str):
        """删除某条消息气泡（重试/删除后用）。"""
        bubble = self._bubbles.pop(msg_id, None)
        if bubble:
            bubble.setParent(None)
            bubble.deleteLater()

    def scroll_to_bottom(self, delay: bool = True):
        """滚动到底部。

        delay=True（默认）：延迟到事件循环下一轮执行，确保新插入的气泡完成
        布局后再取 verticalScrollBar().maximum()，避免切换会话/加载历史时布局
        未完成导致滚不到底。再追一次 50ms 的二次保险（应对图片等异步加载）。
        """
        if delay:
            QTimer.singleShot(0, lambda: self._do_scroll())
            QTimer.singleShot(50, lambda: self._do_scroll())
        else:
            self._do_scroll()

    def _do_scroll(self):
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_context_menu(self, msg: Message, pos):
        self.message_context_menu.emit(msg, pos)

    def _on_avatar_clicked(self, msg: Message):
        self.avatar_clicked.emit(msg)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_max_bubble_width(self.viewport().width())