"""[P7f] 地图拓展 worker（QThread）：点击迷雾块 -> LLM 生成新地点 + 引擎接入。

仿 ShopWorker / SceneWorker（守 §15 worker 生命周期：disconnect + cancel + wait）。
worker 在子线程跑 svc.expand_map_at（LLM 调用 + 引擎布局接入）；storage.save_world 与
地图刷新在主线程槽做（DB 锁 + Qt 对象主线程约束）。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class MapExpandWorker(QThread):
    """地图拓展后台任务。

    信号：
      finished_signal(bool, str, list) - ok / msg / new_loc_ids
    """

    finished_signal = Signal(bool, str, list)

    def __init__(self, world_sim_service, world, from_loc_id, direction, preset, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.world = world
        self.from_loc_id = from_loc_id
        self.direction = direction       # (dx, dy) 或 direction_name 字符串
        self.preset = preset
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            ok, msg, new_ids = self.svc.expand_map_at(
                self.world, self.from_loc_id, self.direction, self.preset,
                cancel_check=lambda: self._cancelled,
            )
            self.finished_signal.emit(bool(ok), msg or "", list(new_ids or []))
        except Exception as e:
            self.finished_signal.emit(False, f"拓展失败：{e}", [])
