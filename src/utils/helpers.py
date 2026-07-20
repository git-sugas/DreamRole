"""通用辅助函数。"""
from __future__ import annotations
import re
from typing import Any


def truncate(text: str, max_len: int = 100) -> str:
    """截断文本用于显示。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def format_tokens(n: int) -> str:
    """格式化 token 数字显示。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def parse_image_tags(text: str) -> list[tuple[str, str]]:
    """
    解析 AI 回复中的图片标签。
    格式: [img:中文场景描述]（仅支持单段描述，不再支持 [img:正面|负面] 分隔）
    返回 [(正面提示词, 负面提示词), ...]

    负面提示词恒为空串：固定负面由 DanbooruPreset.negative_prompt 统一控制，
    LLM 只负责输出画面内容描述（含视角词），不输出负面。
    """
    pattern = r'\[img:([^\]]+)\]'
    results = []
    for match in re.finditer(pattern, text):
        content = match.group(1).strip()
        if content:
            results.append((content, ""))
    return results


def remove_image_tags(text: str) -> str:
    """移除文本中的图片标签。"""
    return re.sub(r'\s*\[img:[^\]]+\]\s*', '', text)


def contains_chinese(text: str) -> bool:
    """判断文本是否含中文字符（用于决定是否走 Danbooru tag 加工）。

    含中文 → 走 emb+LLM 加工；纯英文/纯 tag → 透传 ComfyUI。
    """
    if not text:
        return False
    return bool(re.search(r'[\u4e00-\u9fff]', text))


def safe_get(d: dict, *keys, default: Any = None) -> Any:
    """安全嵌套取值。"""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def gen_id() -> str:
    import uuid
    return str(uuid.uuid4())