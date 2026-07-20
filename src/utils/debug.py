"""调试日志开关。

全局开关 DEBUG：True 时打印 [API-DEBUG] 前缀的调试日志，False 时彻底静默。
切换方式：直接改本文件的 DEBUG 常量后重新运行程序（纯源码开关，无 UI / 配置依赖）。

用法：
    from src.utils.debug import debug_log

    # f-string 必须包成 lambda：开关关闭时 lambda 不调用，f-string 不求值（零开销）。
    debug_log(lambda: f"[LLM.chat] 入参 body: {json.dumps(body)}")

    # 纯字符串常量本就不求值，可直接传（无需 lambda）。
    debug_log("[FTS5.query] 分词后为空，跳过查询")

前缀 [API-DEBUG] 由 debug_log 统一添加，调用方只需写 [模块.方法] 具体内容部分。
"""
from __future__ import annotations
from typing import Callable, Union

# ============ 调试开关：改这里 ============
# [!] 默认 False：发行版不开调试日志（避免泄露破限词/完整请求体到 stdout）。
# 排查问题时临时改 True 后重跑程序。详见 read.md §19。
DEBUG: bool = False
# ==========================================


_MsgArg = Union[str, Callable[[], str]]


def debug_log(msg: _MsgArg) -> None:
    """调试日志输出。

    - 开关关闭（DEBUG=False）：直接 return，msg 若为 lambda 则不会被调用，
      其内部的 f-string 不求值，json.dumps 等也不执行 -> 彻底零开销。
    - 开关开启（DEBUG=True）：msg 若为 callable 则先调用求值，再统一加
      [API-DEBUG] 前缀后 print。

    因此高频路径（如流式逐帧、FTS5 查询）务必把 f-string 包成 lambda 传入，
    确保关闭时无字符串拼接 / 序列化开销。纯字符串常量可直接传。
    """
    if not DEBUG:
        return
    text = msg() if callable(msg) else msg
    print(f"[API-DEBUG]{text}")
