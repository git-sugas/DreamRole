"""消息气泡组件：用户消息、AI消息、总结、折叠块、图片。"""
from __future__ import annotations
import os
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QPixmap, QMouseEvent
from PySide6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout, QFrame, QSizePolicy,
    QPushButton, QTextBrowser, QApplication,
)

from src.models import Message
from src.utils.helpers import format_tokens
from src.utils.markup import render_to_document
from src.ui.widgets.avatar_button import render_avatar


def _max_image_display_height() -> int:
    """图片气泡显示高度上限：屏幕可用高 * 0.40（1080 屏约 420px）。

    原只按气泡宽度上限 scaledToWidth，竖图（如 832x1216）在 1920 窗口下高度
    远超可视区（一张图滚一屏都看不全）。加高度上限后按 KeepAspectRatio 双向
    约束缩放，宽高任一超界即缩，图片始终一屏内可见。
    """
    screen = QApplication.primaryScreen()
    if screen is not None:
        return int(screen.availableGeometry().height() * 0.40)
    return 420


def _max_image_display_width(bubble_max_width: int) -> int:
    """图片气泡显示宽度上限：min(气泡宽度上限, 460)。

    方图/横图若顶到气泡宽度上限（1920 窗口中栏约 1000px+）依然过大，
    图片展示宽度独立收一档，与高度上限共同约束。
    """
    return min(bubble_max_width, 460)


class AutoHeightTextBrowser(QTextBrowser):
    """高度自适应的只读富文本浏览器（替代气泡里的 QLabel）。

    [!] QLabel + RichText 会按内容自动撑高；QTextBrowser 默认是固定高度带滚动条的
    「视口」控件，直接用会让气泡塌成一行高 + 出现内嵌滚动条。本子类：
      - 隐藏滚动条（内容应撑高气泡而非滚动）
      - 接 documentLayout().documentSizeChanged -> setFixedHeight，让高度随内容增长
      - resizeEvent 同步 document textWidth 到 viewport 宽度，保证文本按气泡宽度换行
    [!] documentSizeChanged 在 setMarkdown/setHtml 后会触发；setFixedHeight 后再次
    resize -> resizeEvent 重设 textWidth -> 可能再触发一次 documentSizeChanged（高度已
    稳定不会再变），不会无限循环。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._height_sync_pending = False
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)  # 防 AI 输出恶意链接自动打开
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # [!] 透明无边框：QTextBrowser 在 Windows 上有硬编码 8px frame（frameWidth 改不了，
        # setFrameShape(NoFrame)/QSS border:0/setViewportMargins 都无效），viewport 永远偏移
        # (8,6)。frame 区默认显示 widget 自身背景（被全局 QWidget{background:#1a1b26} 染成
        # 主窗口色），叠在气泡上极突兀。解法：让 widget + viewport 都透明（autoFillBackground
        # =False + WA_TranslucentBackground + QSS background:transparent），frame 区透出父级
        # 气泡背景色，整片与气泡融合。必须三管齐下，少一个 frame 区都会显示主窗口色。
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWidth(0)
        self.setMidLineWidth(0)
        self.setStyleSheet("QTextBrowser { background: transparent; border: 0; }")
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self.setLineWrapMode(QTextBrowser.WidgetWidth)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # document textWidth 初始给个值，避免首次布局前为 -1
        self.document().setTextWidth(self.viewport().width() or 400)
        # 高度自适应：内容尺寸变化 -> 设固定高度
        self.document().documentLayout().documentSizeChanged.connect(self._on_doc_size_changed)

    def _on_doc_size_changed(self, size: QSize):
        # [!] 不可直接用信号携带的 size：QTextDocument 布局是渐进的，
        # documentSizeChanged 在排版未完成时会带中间值提前触发（实测 setMarkdown
        # 长文后固定高度 302 而文档最终 498，或 markdown 模式下固定高度比文档
        # 大 ~200px 致气泡文字下方空一截）。延迟到事件循环下一轮读
        # document().size()（强制完成全部布局）再定高，并用标志位合并同轮
        # 多次触发（流式 append 每 chunk 都改文档，避免一帧内反复定高）。
        if self._height_sync_pending:
            return
        self._height_sync_pending = True
        # singleShot 带 receiver：widget 已销毁时自动不投递，不会调到野指针
        QTimer.singleShot(0, self, self._sync_height_to_doc)

    def _sync_height_to_doc(self):
        """事件循环空闲时按文档最终高度定固定高度。"""
        self._height_sync_pending = False
        # 内容高度 + 2*frameWidth 作为控件高度。QTextBrowser 在 Windows 上 frameWidth=8
        # （QSS 引擎硬编码，frameShape=NoFrame 也改不了），viewport 比控件小 2*frameWidth，
        # 若不加 frameWidth 会导致 viewport 装不下内容出现内嵌滚动条。frame 区已透明
        # （透出气泡色），多出的高度不留白突兀。
        h = int(self.document().size().height()) + 2 * self.frameWidth()
        # 下限 24 防空气泡塌没
        self.setFixedHeight(max(24, h))

    def resizeEvent(self, event):
        # [!] 关键：viewport 宽度变化时同步 document textWidth，否则文本不按气泡宽度换行
        # （QTextBrowser 默认 document textWidth 不跟随 viewport，会按内容自然宽度排版）
        super().resizeEvent(event)
        vw = self.viewport().width()
        if vw > 0:
            self.document().setTextWidth(vw)


class MessageBubble(QFrame):
    """单条消息气泡。"""
    context_menu_requested = Signal(Message, object)  # (message, global_pos)
    avatar_clicked = Signal(Message)  # 点击头像预览大图（message 携带 avatar/character_name）
    image_clicked = Signal(Message)  # 点击气泡内图片放大查看（message 携带 image_path）
    segment_play_clicked = Signal(str, str)  # 点击 TTS 段播放（msg_id, audio_path）

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
        self._bubble = bubble  # 存引用，供 set_max_width 回溯改 maximumWidth
        # [!] autoFillBackground=True：QSS 的 #aiBubble/#userBubble background 只在 widget 自身
        # 绘制时画一下，不是「真实填充」。子控件（header 的角色名/时间/tok QLabel、content_label
        # 的 frame 区）透明透传背景时拿不到 QSS 背景，会透出顶层 QWidget{background:#1a1b26}
        # 主窗口色，导致 header 那行背景与气泡色不融合（角色名区显示 #1c1d28 混合色而非气泡色）。
        # autoFillBackground 让 QSS 背景真实填充到 palette Window 角色，子控件透明即可透出气泡色。
        bubble.setAutoFillBackground(True)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(10, 8, 10, 8)
        bubble_layout.setSpacing(8)

        # 发言者名 + 时间
        # [!] header 的 QLabel 须设 WA_TranslucentBackground：全局 QSS QWidget{background:#1a1b26}
        # 给所有 widget 的 palette Window 染了主窗口色，QLabel 即便 autoFillBackground=False
        # 也会用它清背景，导致角色名/时间/tok 那行背景是主窗口色而非气泡色，与气泡不融合。
        # WA_TranslucentBackground 让 QLabel 真透明，透出 bubble（已 autoFillBackground）的气泡色。
        header = QHBoxLayout()
        name_label = QLabel(self.message.character_name or "用户")
        name_label.setObjectName("charNameLabel")
        name_label.setStyleSheet("font-size: 12px;")
        name_label.setAttribute(Qt.WA_TranslucentBackground)
        time_str = ""
        try:
            dt = datetime.fromisoformat(self.message.timestamp)
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass
        time_label = QLabel(time_str)
        time_label.setStyleSheet("color: #565f89; font-size: 11px;")
        time_label.setAttribute(Qt.WA_TranslucentBackground)
        header.addWidget(name_label)
        header.addWidget(time_label)
        header.addStretch()
        # [!] name_label/tk_label 存为属性：流式占位气泡升级为正式消息时
        # (finalize_streaming_to) 需更新角色名 + 补 token 标签（占位构造时 tokens=0
        # 没建 tk_label，升级后正式消息有了 tokens 须补上），不存引用无法原地改。
        self._name_label = name_label
        self._tk_label: QLabel | None = None
        if self.message.tokens > 0:
            self._tk_label = QLabel(f"{format_tokens(self.message.tokens)} tok")
            self._tk_label.setStyleSheet("color: #565f89; font-size: 11px;")
            self._tk_label.setAttribute(Qt.WA_TranslucentBackground)
            header.addWidget(self._tk_label)
        bubble_layout.addLayout(header)

        self._bubble_layout = bubble_layout  # 供动态角标使用

        # 「已停止」角标（构造时若消息已标记则立即显示）
        self._stopped_label: QLabel | None = None
        if self.message.is_stopped:
            self.set_stopped(True)

        # 内容（富文本：对话/旁白/心声/符号分色显示 + 可选 Markdown 结构化）
        # [!] 用 AutoHeightTextBrowser 取代 QLabel：markdown 模式需要 QTextDocument
        # 供 QTextCursor 二次着色；QLabel 无 document() 做不了。AutoHeightTextBrowser
        # 内部接 documentSizeChanged 让高度随内容自适应（QLabel 自动撑高，QTextBrowser 需手动）。
        self.content_label = AutoHeightTextBrowser()
        # [!] minimumWidth 强制顶到接近 max_width（QTextBrowser+WordWrap 同样有「自然宽度
        # 提前换行」问题，与 QLabel 一致），下限 200 防窄窗溢出。
        self.content_label.setMinimumWidth(max(200, self.max_width - 20))
        self.content_label.setMaximumWidth(self.max_width)
        self.content_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        render_to_document(self.message.content, self._is_user, self.content_label.document())
        # 子控件右键需冒泡到气泡，由气泡统一触发上下文菜单
        self.content_label.mousePressEvent = self._child_mouse_press
        bubble_layout.addWidget(self.content_label)

        # 图片
        if self.message.image_path:
            img_label = self._make_image_label(self.message.image_path)
            if img_label:
                bubble_layout.addWidget(img_label)

        # TTS 段播放列表（仅 assistant 文本消息且有 tts_segments 时渲染）：
        # 每段一行 QLabel（▶ 前缀 + 净文本），手型光标 + hover 高亮，点击播放该段音频。
        # [!] 放在正文/图片下方独立容器，不干扰正文文本选择与右键菜单。仅普通文本气泡
        # （非总结/非纯图片）才渲染；折叠块子气泡构造时不连信号故点击无效（与 context_menu 一致）。
        self._tts_segments_container: QWidget | None = None
        if (not self._is_user and not self._is_summary
                and getattr(self.message, "has_tts", False)
                and getattr(self.message, "tts_segments", None)):
            self._build_tts_segments()

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
        self._bubble = bubble  # 存引用，供 set_max_width 回溯改 maximumWidth
        bubble.setAutoFillBackground(True)  # 同普通气泡：让子控件透明透出气泡背景色
        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 8)
        title = QLabel("[上文总结]")
        title.setStyleSheet("color: #e0af68; font-size: 12px; font-weight: bold;")
        title.setAttribute(Qt.WA_TranslucentBackground)  # 同 header label：透出气泡色
        bl.addWidget(title)
        content = AutoHeightTextBrowser()
        self.content_label = content  # 统一属性名，供 set_max_width 回溯
        # [!] 与普通气泡同理：minimumWidth 强制顶到接近 max_width，文本才会真正利用
        # 气泡宽度到接近上限才换行（QTextBrowser+WordWrap 与 QLabel 一致有此问题）。
        content.setMinimumWidth(max(200, self.max_width - 20))
        content.setMaximumWidth(self.max_width)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        render_to_document(self.message.content, is_user=False, doc=content.document())
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
            self._bubble = bubble  # 存引用，供 set_max_width 回溯改 maximumWidth
            bubble.setAutoFillBackground(True)  # 同普通气泡：让子控件透明透出气泡背景色
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(8, 8, 8, 8)
            bl.addWidget(img_label)
            cap = QLabel(self.message.content or "生成的图片")
            cap.setStyleSheet("color: #565f89; font-size: 11px;")
            cap.setAttribute(Qt.WA_TranslucentBackground)  # 同 header label：透出气泡色
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
        # [!] 缓存原始 pixmap：set_max_width 回溯重缩放时复用，避免每次 resize 都
        # 从磁盘重读图片（拖边框连续 resize 会因重 I/O 卡顿）。
        self._orig_pixmap = pix
        max_w = _max_image_display_width(self.max_width)
        max_h = _max_image_display_height()
        if pix.width() > max_w or pix.height() > max_h:
            pix = pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label = QLabel()
        label.setPixmap(pix)
        label.setAlignment(Qt.AlignCenter)
        # 点击图片放大查看：手型光标提示可点，左键发 image_clicked 信号；右键不拦截而
        # 冒泡到气泡的上下文菜单（查看提示词/重新生成/删除，与文本 content_label 的
        # _child_mouse_press 一致）。lambda 用默认参数捕获 label，避免闭包晚期引用问题。
        label.setCursor(Qt.PointingHandCursor)
        label.mousePressEvent = lambda event, lbl=label: self._on_image_click(event, lbl)
        return label

    def _on_image_click(self, event: QMouseEvent, label: QLabel):
        """点击气泡内图片：左键发 image_clicked 信号放大查看，右键冒泡到气泡触发
        上下文菜单，其余按钮走 QLabel 默认行为。"""
        if event.button() == Qt.LeftButton:
            self.image_clicked.emit(self.message)
        elif event.button() == Qt.RightButton:
            self.context_menu_requested.emit(self.message, event.globalPosition().toPoint())
        else:
            QLabel.mousePressEvent(label, event)

    def update_content(self, text: str):
        """流式更新内容（富文本渲染）。"""
        if hasattr(self, "content_label"):
            render_to_document(text, self._is_user, self.content_label.document())

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

    def set_max_width(self, max_width: int):
        """回溯更新气泡宽度上限（resize 后已有气泡跟随新宽度）。

        - 文本气泡：更新 content_label 的 min/max + _bubble.maximumWidth，并
          setTextWidth 触发重排换行。
        - 图片气泡：无 content_label，重缩放 _img_label 的 pixmap 到新 max_width
          （图片按 max_width 缩放，pixmap 尺寸固定，不重缩放则回溯后图片仍按旧
          宽度显示，进设置退出重建时会按新宽度缩放致「图片变大」）+ _bubble.maximumWidth。
        折叠块子气泡同样适用。
        """
        self.max_width = max_width
        if hasattr(self, "content_label") and self.content_label is not None:
            self.content_label.setMinimumWidth(max(200, max_width - 20))
            self.content_label.setMaximumWidth(max_width)
            # 文本宽度变了须同步 document textWidth 触发重排换行
            vw = self.content_label.viewport().width()
            if vw > 0:
                self.content_label.document().setTextWidth(vw)
        # 图片气泡：重缩放 pixmap（图片按 max_width 缩放、pixmap 尺寸固定，不重缩放
        # 则回溯后图片仍按旧宽度显示，进设置退出 load_messages 重建时按新宽度缩放
        # 致「图片变大」）。[!] 用 _orig_pixmap 缓存重缩放，不重读盘（避免拖边框卡顿）。
        if hasattr(self, "_img_label") and self._img_label is not None and hasattr(self, "_orig_pixmap") \
                and self._orig_pixmap is not None:
            pix = self._orig_pixmap
            max_w = _max_image_display_width(max_width)
            max_h = _max_image_display_height()
            if pix.width() > max_w or pix.height() > max_h:
                pix = pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._img_label.setPixmap(pix)
        if hasattr(self, "_bubble") and self._bubble is not None:
            self._bubble.setMaximumWidth(max_width + 40)

    def update_header(self):
        """流式占位气泡升级为正式消息后，同步 header 的角色名 + token 标签。

        占位气泡构造时 tokens=0 且角色名用占位值（_pending_speaker 或首个角色），
        升级为正式消息后须：(1) 更新角色名为正式消息的 character_name；
        (2) 若正式消息 tokens>0 而占位时没建 tk_label，补建一个插入 header 末尾。
        时间标签不动（占位创建时间与正式消息保存时间差异可忽略）。
        """
        if not hasattr(self, "_name_label"):
            return  # 总结/纯图片气泡无 header，跳过
        self._name_label.setText(self.message.character_name or "用户")
        if self.message.tokens > 0 and self._tk_label is None:
            self._tk_label = QLabel(f"{format_tokens(self.message.tokens)} tok")
            self._tk_label.setStyleSheet("color: #565f89; font-size: 11px;")
            self._tk_label.setAttribute(Qt.WA_TranslucentBackground)
            # tk_label 应插在 header 行末尾（stretch 之后）。header 是 _bubble_layout
            # 的第一个子 layout，取它追加 tk_label。
            self._bubble_layout.itemAt(0).layout().addWidget(self._tk_label)
        elif self.message.tokens > 0 and self._tk_label is not None:
            self._tk_label.setText(f"{format_tokens(self.message.tokens)} tok")

    def set_stopped(self, stopped: bool):
        """显示/隐藏「已停止」角标（区分完整回复与被中断的部分回复）。"""
        if not hasattr(self, "_bubble_layout"):
            return  # 总结/纯图片气泡等无 bubble_layout 的不处理
        if self._stopped_label is None:
            lbl = QLabel("⏹ 已停止")
            lbl.setObjectName("stoppedLabel")
            lbl.setStyleSheet("color: #f7768e; font-size: 11px; font-style: italic;")
            lbl.setAttribute(Qt.WA_TranslucentBackground)  # 同 header label：透出气泡色
            self._stopped_label = lbl
        if stopped:
            if self._stopped_label.parent() is None:
                self._bubble_layout.addWidget(self._stopped_label)
            self._stopped_label.show()
        else:
            if self._stopped_label is not None:
                self._stopped_label.hide()

    def _build_tts_segments(self):
        """构建 TTS 段播放列表（正文下方的可点击段列表）。

        每段一行 QLabel：▶ 前缀 + 净文本（过长省略），手型光标 + hover 浅色高亮，
        点击左键 emit segment_play_clicked(msg_id, path) 播放该段音频。
        [!] 文本经 HTML 转义防注入；段文本是 TTS 净文本（已去定界符），与正文展示的
        富文本可能略有差异（属预期：这里显示的是实际配音内容）。
        """
        segments = getattr(self.message, "tts_segments", None) or []
        if not segments:
            return
        container = QWidget()
        container.setAttribute(Qt.WA_TranslucentBackground)
        cl = QVBoxLayout(container)
        cl.setContentsMargins(0, 6, 0, 0)
        cl.setSpacing(2)
        for seg in segments:
            path = seg.get("path", "")
            text = seg.get("text", "") or ""
            if not path:
                continue  # 无音频文件不可播
            # 文本过长省略（避免单段占满整屏）
            display = text if len(text) <= 60 else text[:57] + "..."
            lbl = QLabel(f"▶ {display}")
            lbl.setObjectName("ttsSegmentLabel")
            # hover 高亮 + 手型光标（QSS :hover 改 background）
            lbl.setStyleSheet(
                "QLabel#ttsSegmentLabel { color: #9aa5ce; font-size: 12px; padding: 1px 4px;"
                " border-radius: 3px; }"
                "QLabel#ttsSegmentLabel:hover { background: #2a2e44; color: #c0caf5; }"
            )
            lbl.setAttribute(Qt.WA_TranslucentBackground)
            lbl.setCursor(Qt.PointingHandCursor)
            # tooltip 显示完整文本（省略时可见全文）
            if len(text) > 60:
                lbl.setToolTip(text)
            # [!] lambda 默认参数捕获 path + label，避免闭包晚期引用问题（仿 _make_image_label）
            lbl.mousePressEvent = lambda event, p=path, l=lbl: self._on_segment_click(event, p, l)
            cl.addWidget(lbl)
        if cl.count() == 0:
            container.deleteLater()
            return
        self._tts_segments_container = container
        self._bubble_layout.addWidget(container)

    def _on_segment_click(self, event, path: str, label: QLabel):
        """点击 TTS 段：左键播放该段音频，右键冒泡到气泡触发上下文菜单，其余走 QLabel 默认。"""
        if event.button() == Qt.LeftButton:
            self.segment_play_clicked.emit(self.message.id, path)
        elif event.button() == Qt.RightButton:
            self.context_menu_requested.emit(self.message, event.globalPosition().toPoint())
        else:
            # [!] 须传 label 而非 self：QLabel.mousePressEvent 第一个参数期望 QLabel 实例
            # （self 是 MessageBubble/QFrame，传错会抛 TypeError），仿 _on_image_click 传 label。
            QLabel.mousePressEvent(label, event)

    def set_tts_segments(self, segments: list):
        """原地刷新 TTS 段列表（TTS 生成完成后调用，避免重建气泡）。

        先移除旧容器（若有），再按新 segments 构建。segments 为空则移除不重建。
        """
        # [!] 防御：总结/纯图片气泡无 _bubble_layout（走 _build_summary/_build_image_only
        # 不设该属性），调用此方法时直接跳过（与 set_stopped/update_header 同防御风格）。
        if not hasattr(self, "_bubble_layout"):
            return
        # 同步 message 对象上的字段（调用方已设，此处兜底）
        self.message.tts_segments = segments or []
        self.message.has_tts = bool(segments)
        old = getattr(self, "_tts_segments_container", None)
        if old is not None:
            self._bubble_layout.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
            self._tts_segments_container = None
        if segments:
            self._build_tts_segments()

    def append_content(self, text: str):
        """流式追加内容（重新渲染整段）。"""
        if hasattr(self, "content_label"):
            # 缓存累积的纯文本，每 chunk 重新渲染整段保证闭合正确
            if not hasattr(self, "_stream_text"):
                self._stream_text = ""
            self._stream_text += text
            render_to_document(self._stream_text, self._is_user, self.content_label.document())

    def _child_mouse_press(self, event: QMouseEvent):
        """子控件（内容文本）右键冒泡到气泡，触发上下文菜单；其余事件走默认行为。"""
        if event.button() == Qt.RightButton:
            self.context_menu_requested.emit(self.message, event.globalPosition().toPoint())
        else:
            # 保留文本选择/交互
            QTextBrowser.mousePressEvent(self.content_label, event)

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

    def __init__(self, messages: list[Message], reason: str = "auto_summary",
                 max_width: int = 500, parent=None):
        super().__init__(parent)
        self.messages = messages
        self.setObjectName("collapsedBlock")
        self._expanded = False
        self._built = False  # 子气泡是否已创建
        self._max_width = max_width  # 子气泡宽度上限，由外部 set_max_width 回溯更新
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
            mb = MessageBubble(msg, max_width=self._max_width)
            # [!] MessageBubble 构造时不连接 context_menu_requested 信号，故此处
            # 不再调 disconnect()（旧代码调了会触发 PySide6 RuntimeWarning:
            # "Failed to disconnect (None) from signal"）。折叠块内子气泡本就
            # 不需要响应右键菜单--构造时不连，自然不会 emit 到外部。若未来改为
            # 构造时自动连接，再在此处断开即可（届时已有连接，disconnect 不会警告）。
            self.content_layout.addWidget(mb)
        self._built = True

    def set_max_width(self, max_width: int):
        """回溯子气泡宽度上限（resize 后已展开折叠块内的子气泡跟随新宽度）。

        更新 self._max_width（供后续展开首次构造子气泡用）+ 已构建子气泡的
        set_max_width。未展开折叠块天然无渲染，展开时按当前 _max_width 构造。
        """
        self._max_width = max_width
        if not self._built:
            return
        for i in range(self.content_layout.count()):
            item = self.content_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None and hasattr(w, "set_max_width"):
                w.set_max_width(max_width)

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
                render_to_document(w.message.content, w._is_user, w.content_label.document())