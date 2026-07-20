"""关于对话框：声明本项目开发目的与免责条款。

本项目开发目的仅为方便用户使用 LLM 进行正常的互动聊天。用户自行将本工具用于
非法、色情、暴力等用途，由用户自行承担全部责任，与开发者无关。

有 bug / 问题联系作者 QQ：1965699077
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QDialogButtonBox, QFrame,
)


class AboutDialog(QDialog):
    """关于对话框（纯展示，无状态）。"""

    # 联系方式（常量，便于以后统一修改）
    AUTHOR_QQ = "1965699077"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于")
        self.resize(520, 460)
        self.setMinimumSize(460, 380)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # 标题
        title = QLabel("DreamRole")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("基于 LLM 的角色扮演聊天工具")
        subtitle.setStyleSheet("font-size: 12px; color: #8a8d9b;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # 开发目的
        purpose_title = QLabel("开发目的")
        purpose_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(purpose_title)

        purpose = QLabel(
            "本项目的开发目的只是为了方便用户使用 LLM 进行正常的互动聊天。"
        )
        purpose.setWordWrap(True)
        layout.addWidget(purpose)

        # 免责声明
        disclaimer_title = QLabel("免责声明")
        disclaimer_title.setStyleSheet("font-weight: bold; color: #e0a060;")
        layout.addWidget(disclaimer_title)

        disclaimer = QLabel(
            "用户自行将本工具用于非法、色情、暴力等行为，由用户本人承担全部责任，"
            "与开发者无关。请遵守当地法律法规，在合法合规的前提下使用本软件。"
        )
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: #c0caf5;")
        layout.addWidget(disclaimer)

        # 联系方式
        contact_title = QLabel("联系方式")
        contact_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(contact_title)

        contact = QLabel(f"有 bug / 问题联系作者 QQ：{self.AUTHOR_QQ}")
        contact.setWordWrap(True)
        layout.addWidget(contact)

        layout.addStretch(1)

        # 关闭按钮
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        btn_box.accepted.connect(self.accept)
        layout.addWidget(btn_box)
