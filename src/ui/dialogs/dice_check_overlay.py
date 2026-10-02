"""[三骰取二 2026-08-26] 判定演出 overlay（模态）。

用法（照 CombatDialog.exec() -> get_summary() 模式）:
    overlay = DiceCheckOverlay(p=chance)
    overlay.exec()                 # 打开即自动掷骰 + 动画（点一次「合成」即触发）
    tier = overlay.result.tier     # "crit"/"ok"/"fail"/"fumble"
    reads = overlay.result.reads

动画期间用模态 exec() 的嵌套事件循环阻塞主流程；快速战斗/批量 tick 不调用本 overlay。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton)

from src.ui.widgets.dice_die import DieFace, ValueTrack, TIER_INFO
from src.utils.dice_check import roll_dice_check, DiceRoll, CRIT_THRESHOLD


class DiceCheckOverlay(QDialog):
    """三骰取二判定弹窗：点开即掷（真随机），三骰左中右排开，落定后显示定档。"""

    def __init__(self, p: float, crit_thresh: int = CRIT_THRESHOLD, parent=None):
        super().__init__(parent)
        self.p = float(p)
        self.crit_thresh = int(crit_thresh)
        self.result: DiceRoll | None = None
        self._landed_count = 0
        self._build_ui()
        # 打开后自动掷一次（点一次「合成/采集」即触发；延迟等窗口先显示）
        QTimer.singleShot(120, self._do_roll)

    def _build_ui(self):
        self.setWindowTitle("判定")
        self.setMinimumWidth(560)
        self.setStyleSheet("""
            QDialog { background: #101218; }
            QLabel { color: #d8dce4; }
            QLabel#result { font-size: 30px; font-weight: bold; }
            QLabel#sub { color: #aeb6c2; font-size: 12px; }
            QPushButton {
                background: #262b36; color: #e6eaf1; border: 1px solid #3a4150;
                border-radius: 6px; padding: 8px 24px; font-weight: bold;
            }
            QPushButton:hover { background: #313847; }
            QPushButton:disabled { color: #5b6270; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 20, 26, 20)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.addStretch(1)
        self.dice = []
        for i, name in enumerate(("左", "中", "右")):
            die = DieFace(f"d{i}", i, min_value=1, max_value=100, label=name)
            die.landed.connect(self._on_die_landed)
            self.dice.append(die)
            row.addWidget(die)
            if i < 2:
                row.addSpacing(8)
        row.addStretch(1)
        lay.addLayout(row)

        self.result_label = QLabel("……")
        self.result_label.setObjectName("result")
        self.result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.result_label)

        p_pct = max(1, min(99, int(round(self.p * 100))))
        self.track = ValueTrack()
        self.track.set_lines(p_pct, self.crit_thresh)
        lay.addWidget(self.track)

        self.sub = QLabel(f"成功线 >= {101 - p_pct}，大成功 >= {self.crit_thresh}")
        self.sub.setObjectName("sub")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.sub)

        btn = QPushButton("确 定")
        btn.setEnabled(False)
        btn.clicked.connect(self.accept)
        self.btn_ok = btn
        lay.addWidget(btn)

    def _do_roll(self):
        self.result = roll_dice_check(self.p, self.crit_thresh)  # 真随机三枚
        self._landed_count = 0
        self.btn_ok.setEnabled(False)
        self.result_label.setText("……")
        self.result_label.setStyleSheet("color:#8a919e;")
        for die, read in zip(self.dice, self.result.reads):
            die.roll(read)

    def _on_die_landed(self, _v):
        self._landed_count += 1
        if self._landed_count < len(self.dice):
            return
        res = self.result
        if res is None:
            return
        name, color = TIER_INFO[res.tier]
        self.result_label.setText(name)
        self.result_label.setStyleSheet(f"color:{color.name()};")
        for die, read in zip(self.dice, res.reads):
            die.set_frame("gold" if (read >= res.crit_thresh or res.tier == "crit") else
                          ("red" if (read == 1 or res.tier == "fumble") else "gray"))
        self.track.animate_to(res.best_reads[0])
        self.btn_ok.setEnabled(True)
