"""聊天输入栏。"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QTextEdit, QPushButton, QLabel,
    QCheckBox, QComboBox,
)


class ChatInput(QWidget):
    """消息输入栏：文本框 + 发送按钮 + 群聊控制。"""

    send_requested = Signal(str)
    continue_requested = Signal()           # 群聊继续（导演选下一个）
    mode_changed = Signal(str)              # "auto" | "manual"
    stop_requested = Signal()              # 停止生成

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 群聊控制行
        self.group_controls = QWidget()
        gc_layout = QHBoxLayout(self.group_controls)
        gc_layout.setContentsMargins(0, 0, 0, 0)
        gc_layout.setSpacing(8)

        self.mode_label = QLabel("发言模式:")
        self.mode_label.setStyleSheet("color: #565f89; font-size: 12px;")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("手动（点击头像）", "manual")
        self.mode_combo.addItem("自动（API选择）", "auto")
        self.mode_combo.setFixedWidth(180)
        self.mode_combo.currentIndexChanged.connect(
            lambda: self.mode_changed.emit(self.mode_combo.currentData())
        )

        self.continue_btn = QPushButton("继续对话")
        self.continue_btn.setToolTip("让导演API选择下一个发言者")
        self.continue_btn.clicked.connect(self.continue_requested)

        gc_layout.addWidget(self.mode_label)
        gc_layout.addWidget(self.mode_combo)
        gc_layout.addStretch()
        gc_layout.addWidget(self.continue_btn)

        layout.addWidget(self.group_controls)
        self.group_controls.setVisible(False)

        # 输入行
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("输入消息... (Enter 发送, Shift+Enter 换行)")
        self.text_edit.setFixedHeight(100)
        self.text_edit.installEventFilter(self)

        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primaryBtn")
        self.send_btn.setFixedSize(100, 100)
        self.send_btn.clicked.connect(self._on_send)

        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setObjectName("dangerBtn")
        self.stop_btn.setFixedSize(100, 100)
        self.stop_btn.setToolTip("停止当前生成")
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        self.stop_btn.setVisible(False)

        input_row.addWidget(self.text_edit)
        input_row.addWidget(self.send_btn)
        input_row.addWidget(self.stop_btn)
        layout.addLayout(input_row)

    def eventFilter(self, obj, event):
        if obj == self.text_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                # [!] IME 组合态（拼音/日文选词中）让输入法处理 Enter（确认候选词），
                # 否则会把候选词未确认的文本误发送。inputMethod().isVisible() 在
                # Windows/Linux 上反映 IME 是否处于组合态。
                im = QGuiApplication.inputMethod()
                if im is not None and im.isVisible():
                    return False
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _on_send(self):
        text = self.text_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self.text_edit.clear()

    def set_group_mode(self, visible: bool):
        self.group_controls.setVisible(visible)

    def set_current_mode(self, mode: str):
        idx = 0 if mode == "manual" else 1
        self.mode_combo.setCurrentIndex(idx)

    def get_mode(self) -> str:
        return self.mode_combo.currentData()

    def set_enabled(self, enabled: bool):
        self.text_edit.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        self.continue_btn.setEnabled(enabled)

    def set_generating(self, generating: bool):
        """切换生成中状态：生成中显示停止按钮、隐藏发送按钮；完成后恢复。"""
        self.send_btn.setVisible(not generating)
        self.stop_btn.setVisible(generating)
        # 生成中输入框禁用（停止按钮仍可点）
        self.text_edit.setEnabled(not generating)
        self.continue_btn.setEnabled(not generating)
        if generating:
            # 停止按钮始终可点（防发送按钮残留的 disabled 状态）
            self.stop_btn.setEnabled(True)