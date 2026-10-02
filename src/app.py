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
from src.services.world_sim_service import WorldSimService


def get_resource_path(relative_path: str) -> str:
    """获取资源路径，兼容开发模式和 PyInstaller 打包。"""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
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
    # [修 2026-09-05] 计费 sink 注册到 LlmClient 层：一切走 LlmClient 的调用（含世界
    # 模拟/记忆/总结/生图加工等）统一记入统计页——此前仅聊天链路手工 record，世界
    # 模拟全部漏计（统计页零计费）。见 llm_client._report_usage。
    from src.services import llm_client as _llm_client
    _llm_client.set_usage_recorder(stats_service.record_usage)

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

    # 世界模拟服务（独立 SLG 系统，复用 storage/comfyui/danbooru 基础设施）
    world_sim_service = WorldSimService(storage, comfyui_service, danbooru_service)

    # 全局配置单例：启动时主动加载，供 main.py 据此决定是否起远程服务，
    # 也供 MainWindow 状态栏展示远程服务状态/对话框回显表单。
    app_config = storage.load_app_config()

    return {
        "storage": storage,
        "context_builder": context_builder,
        "memory": memory_service,
        "summary": summary_service,
        "stats": stats_service,
        "comfyui": comfyui_service,
        "danbooru": danbooru_service,
        "orchestrator": orchestrator,
        "world_sim": world_sim_service,
        "app_config": app_config,
    }