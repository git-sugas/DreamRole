"""[P6] 商店重生成 worker（QThread）：墙钟定时器 / 按需触发时后台调 LLM 给过期商店换货。

仿 WorldGenWorker（QThread + finished_signal + deleteLater）+ SceneWorker（cancel 标志 + cancel_check）。
不阻塞主线程；调用方（场景页）closeEvent/cleanup 需 disconnect + cancel + wait（守 §15）。

守 §21 完全独立铁律：世界模拟专用，复用 svc.regen_shops，零单聊/群聊/会话/远程/TTS 依赖。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from src.models import WorldSimPreset


class ShopWorker(QThread):
    """商店货架重生成后台任务。

    信号：
      finished_signal(bool, object) - 成功传被重置的商店名 list，失败传 error str
    """

    finished_signal = Signal(bool, object)

    def __init__(self, world_sim_service, world, preset: WorldSimPreset,
                 shop_ids: list[str], parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.world = world
        self.preset = preset
        self.shop_ids = list(shop_ids or [])
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            # 子线程做 LLM 调用 + 引擎重置（world 是 Python 对象，主线程 finish 槽负责 save）
            cleared = self.svc.regen_shops(
                self.world, self.shop_ids, self.preset,
                cancel_check=lambda: self._cancelled,
            )
            if self._cancelled:
                # 取消仍保留已重置部分（仿 §5），告知调用方保存
                self.finished_signal.emit(True, cleared)
                return
            self.finished_signal.emit(True, cleared)
        except Exception as e:
            self.finished_signal.emit(False, f"商店补货失败：{e}")
