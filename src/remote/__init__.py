"""远程服务（手机端联动）模块。

PC 端 exe 在线运行时，后台起 fastapi/uvicorn 服务监听 0.0.0.0:port，
手机浏览器访问 PC 端局域网 IP:端口可查看 PC 创建的会话并聊天。

[!] 零 PySide6 依赖：本模块只 import fastapi/uvicorn/标准库/项目 service 层，
    main.py 启停用 try/except 兜底，import/启动失败均不影响 GUI。
[!] 复用 init_services() 返回的 orchestrator 单例（Storage 线程安全），
    不新建一套服务，避免记忆/统计/总结状态分裂。
[!] 流式跟随 api.streaming：编排器已内置 if api.streaming 分支判断
    （chat_orchestrator.py:228-271），服务端 on_chunk 回调把 chunk 推队列，
    SSE generator 从队列读推浏览器，天然跟随设置。
"""
from src.remote.server import (
    start_server, stop_server, restart_server,
    is_running, get_status, get_local_ip,
)

__all__ = [
    "start_server", "stop_server", "restart_server",
    "is_running", "get_status", "get_local_ip",
]