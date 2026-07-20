"""Token 计数工具（基于 tiktoken）。"""
from __future__ import annotations
import functools


@functools.lru_cache(maxsize=4)
def _get_encoder(model: str = "gpt-4o"):
    try:
        import tiktoken
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """估算文本的 token 数。"""
    if not text:
        return 0
    enc = _get_encoder(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # 粗略回退：中文字符按 1 token，英文按 ~4 字符/token
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en = len(text) - cn
    return cn + en // 4


def count_messages_tokens(messages: list[dict], model: str = "gpt-4o") -> int:
    """估算消息列表的总 token 数（含角色标记开销）。"""
    total = 0
    for msg in messages:
        total += 4  # 每条消息的固定开销
        total += count_tokens(msg.get("content", ""), model)
        total += count_tokens(msg.get("role", ""), model)
        if msg.get("name"):
            total += count_tokens(msg["name"], model)
    total += 2  # priming
    return total