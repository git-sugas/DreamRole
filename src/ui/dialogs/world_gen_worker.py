"""世界生成 worker（QThread）：跑骨架生成（骨架 -> 构建 -> 备货 -> 档案），带取消。

[修 2026-09-05 用户指示] 生图移出生成管线：此前批量出图（banner/背景/头像，每张
danbooru+ComfyUI）阻塞在预览前，第一次进世界极慢；图一开始没生成不影响玩。现生成
worker 只出文字世界，保存后由 main_window 起 _MissingImagesWorker 后台补图（regenerate_
missing_images 只喂缺失文件任务，文件名在建世界时已定，后台生成零改动落盘）。

仿 _TtsTestWorker（QThread + finished_signal + deleteLater）+ ChatWorker（cancel 标志 + cancel_check）。
不阻塞主线程；主窗口/对话框 closeEvent 需 cancel + wait（守 §15 worker 生命周期）。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from src.models import WorldSimPreset


class WorldGenWorker(QThread):
    """世界生成后台任务。

    信号：
      stage(str)              - 当前阶段文本（"生成世界骨架…" / "生成图片 i/total…"）
      progress(int, int, str) - (done, total, label) 文生图进度
      finished_signal(bool, object) - 成功传 World，失败传 error str
    """

    stage = Signal(str)
    progress = Signal(int, int, str)
    finished_signal = Signal(bool, object)

    def __init__(self, world_sim_service, form: dict, preset: WorldSimPreset, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.form = form
        self.preset = preset
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self.stage.emit("正在生成世界骨架（调用 LLM，可能需要数十秒）…")
            sk, err = self.svc.generate_world_skeleton(
                self.form, self.preset, cancel_check=lambda: self._cancelled,
            )
            if self._cancelled:
                self.finished_signal.emit(False, "已取消")
                return
            if sk is None:
                self.finished_signal.emit(False, err or "世界骨架生成失败")
                return

            self.stage.emit("正在构建世界结构…")
            world = self.svc.build_world_from_skeleton(sk, self.form, self.preset)

            # [P6] 为空货架商店备货（LLM 差异化备货，失败回退目录选品；取消保留已备货部分）
            if world.shops:
                self.stage.emit("正在为商店备货（调用 LLM）…")
                self.svc.generate_shops_goods(world, self.preset, cancel_check=lambda: self._cancelled)
            # [修 2026-09-13 用户指示] collect 材料可得性收口：任务目标材料若全渠道
            # 断供（不掉落/不采集/不上架/不可合成），保底上架出生地材料行/杂货铺；
            # 金/红材料挂野外地资源点掉落。必须在备货之后跑——先上会把空架店标成
            # 非空令 LLM 差异化备货整店跳过。不 gate 在 if world.shops 内：无商店世界
            # 的金材料仍需挂资源点（方法内部对无店/无野外安全返回）。
            self.svc.ensure_quest_material_sources(world, self.preset)

            # [P6c] 给要角/商售 NPC 生成档案（人际关系/爱好/小传，一次 LLM；失败 best-effort）
            if any(n.is_key_npc or n.is_merchant for n in world.npcs):
                self.stage.emit("正在生成 NPC 档案…")
                self.svc.generate_npc_profiles(world, self.preset, cancel_check=lambda: self._cancelled)

            if self._cancelled:
                self.finished_signal.emit(False, "已取消（部分结果未保存）")
                return

            self.finished_signal.emit(True, world)
        except Exception as e:
            self.finished_signal.emit(False, f"内部错误：{e}")
