"""[UI 改造 2026-10-01] 游戏化设计系统组件（暗金古卷层）。

样板批次（NPC 档案/议程）先行，验收后按同一规范铺开其余对话框。
全部组件零 emoji、纯 QSS 驱动（objectName + 动态属性命中 theme.qss 设计系统段），
不引入图片资源（真机/离屏渲染一致），守 §21 完全独立铁律。

组件一览：
- GameCard        档案卡片（gold=True 金顶线精品卡；prio=0/1 议程优先级左边线）
- StatBar         数值条（label + 条 + 数值；tone: red/gold/green/blue/purple）
- game_badge      状态徽章（level: gold/info/warn/danger/success）
- kv_row          键值行（左灰标签右正文，横向排布）
- banner_rule     金色渐隐分隔线
- card_title/card_sub 卡片标题/副文 QLabel 工厂
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget,
)


def card_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("gameCardTitle")
    return lbl


def card_sub(text: str, wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("gameCardSub")
    lbl.setWordWrap(wrap)
    return lbl


def game_badge(text: str, level: str = "info") -> QLabel:
    """状态徽章。level: gold/info/warn/danger/success（theme.qss #gameBadge[level]）。"""
    lbl = QLabel(text)
    lbl.setObjectName("gameBadge")
    lbl.setProperty("level", level)
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


def banner_rule() -> QFrame:
    """金色渐隐分隔线（对话框头部大名下方 / 区块之间）。"""
    line = QFrame()
    line.setObjectName("bannerRule")
    line.setFixedHeight(2)
    return line


def page_header(title: str) -> "QWidget":
    """整页页头（设置类对话框顶部通用件）：bannerName 大名 + 金笔分隔。

    [深改 2026-10-01] 设置类对话框批量页头化用——一行 layout.addWidget(page_header("…"))
    即完成，替代逐个手拼。"""
    host = QWidget()
    col = QVBoxLayout(host)
    col.setContentsMargins(0, 0, 0, 2)
    col.setSpacing(6)
    name = QLabel(title)
    name.setObjectName("bannerName")
    col.addWidget(name)
    col.addWidget(banner_rule())
    return host


class GameCard(QFrame):
    """档案卡片：可选标题行 + 内容列。

    gold=True  金顶线精品卡（人生目标/战备顶卡等强调内容）
    prio=0/1   议程优先级卡（红/金左边线；None 普通卡）
    """

    def __init__(self, title: str = "", gold: bool = False, prio=None, parent=None):
        super().__init__(parent)
        self.setObjectName("gameCard")
        if gold:
            self.setProperty("gold", True)
        if prio is not None:
            self.setProperty("prio", int(prio))
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(12, 10, 12, 10)
        self._lay.setSpacing(6)
        self._title: QLabel | None = None
        if title:
            self.set_title(title)

    def set_title(self, text: str) -> None:
        if self._title is not None:
            self._title.setText(text)
            return
        self._title = card_title(text)
        self._lay.insertWidget(0, self._title)

    def card_title_label(self, text: str = "") -> QLabel:
        """取标题 QLabel 引用（自定头部布局用；无标题时先创建）。"""
        if self._title is None:
            self._title = QLabel("")
            self._title.setObjectName("gameCardTitle")
            self._lay.insertWidget(0, self._title)
        if text:
            self._title.setText(text)
        return self._title

    @property
    def content(self) -> QVBoxLayout:
        return self._lay

    def add(self, w: QWidget, stretch: int = 0) -> None:
        self._lay.addWidget(w, stretch)

    def add_stretch(self) -> None:
        self._lay.addStretch()


def kv_row(key: str, val: str) -> QWidget:
    """键值行：左灰标签（固定收 84px 对齐）右正文。"""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(8)
    k = QLabel(key)
    k.setObjectName("kvKey")
    k.setFixedWidth(84)
    v = QLabel(val)
    v.setObjectName("kvVal")
    v.setWordWrap(True)
    h.addWidget(k)
    h.addWidget(v, 1)
    return row


class StatBar(QWidget):
    """数值条：名称 + 进度条（含数值文本）+ 末尾数值，tone 五色。

    value/max 支持 int；bar 上直接显数值（如 24/24），行尾不再重复（留白给调用方
    附加说明，如分档名）。
    """

    def __init__(self, label: str, value: int, maximum: int, tone: str = "gold",
                 suffix: str = "", parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        name = QLabel(label)
        name.setObjectName("kvKey")
        name.setFixedWidth(84)
        h.addWidget(name)
        self.bar = QProgressBar()
        self.bar.setObjectName("statBar")
        self.bar.setProperty("tone", tone)
        self._max = max(1, int(maximum))
        self.bar.setRange(0, self._max)
        self.bar.setValue(max(0, min(int(value), self._max)))
        self.bar.setFormat(f"{int(value)}/{self._max}{suffix}")
        self.bar.setAlignment(Qt.AlignCenter)
        h.addWidget(self.bar, 1)
