"""应用初始化：创建所有服务实例。"""
from __future__ import annotations
import os
import sys

from src.services.storage import Storage
from src.services.context_builder import ContextBuilder
from src.services.memory_service import MemoryService
from src.services.summary_service import SummaryService
from src.services.stats_service import StatsService
from src.services.comfyui_service import ComfyUiService, load_comfyui_config
from src.services.danbooru_service import DanbooruService
from src.services.chat_orchestrator import ChatOrchestrator


def get_resource_path(relative_path: str) -> str:
    """获取资源路径，兼容开发模式、PyInstaller 与 Nuitka 打包。

    - 开发模式：用本文件所在目录的上层（项目根）。
    - PyInstaller / Nuitka onefile：sys.frozen=True，sys._MEIPASS 指向解压目录。
    - Nuitka standalone：不设 sys.frozen，改用 __compiled__ 检测；数据文件按原目录
      结构放在 exe 同级，故以 sys.argv[0] 所在目录为 base。
    """
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    elif "__compiled__" in dir(sys.modules.get("__main__", object())):
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


def load_theme() -> str:
    """加载 QSS 主题样式表。"""
    theme_path = get_resource_path(os.path.join("src", "ui", "theme.qss"))
    try:
        with open(theme_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def init_services() -> dict:
    """初始化并返回所有服务实例。"""
    storage = Storage()
    context_builder = ContextBuilder(storage)
    memory_service = MemoryService(storage)
    # 注入 memory_service 到 storage，供 delete_character 级联清理 ChromaDB + 计数文件
    storage.set_memory_service(memory_service)
    summary_service = SummaryService(storage)
    stats_service = StatsService()

    comfyui_config = load_comfyui_config()
    comfyui_service = ComfyUiService(comfyui_config)

    danbooru_service = DanbooruService(storage)

    orchestrator = ChatOrchestrator(
        storage=storage,
        context_builder=context_builder,
        memory_service=memory_service,
        summary_service=summary_service,
        stats_service=stats_service,
        comfyui_service=comfyui_service,
        danbooru_service=danbooru_service,
    )

    return {
        "storage": storage,
        "context_builder": context_builder,
        "memory": memory_service,
        "summary": summary_service,
        "stats": stats_service,
        "comfyui": comfyui_service,
        "danbooru": danbooru_service,
        "orchestrator": orchestrator,
    }