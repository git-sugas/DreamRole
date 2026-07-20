"""API 统计数据模型（token 消耗 / 缓存命中）。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


@dataclass
class ApiStats:
    api_id: str = ""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    request_count: int = 0
    last_reset: str = field(default_factory=_now)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.total_cached_tokens / self.total_prompt_tokens

    @property
    def saved_tokens(self) -> int:
        """缓存节省的 token（按缓存 token 计）。"""
        return self.total_cached_tokens

    def to_dict(self) -> dict:
        return {
            "api_id": self.api_id,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "request_count": self.request_count,
            "last_reset": self.last_reset,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ApiStats:
        return cls(
            api_id=d.get("api_id", "") or "",
            total_prompt_tokens=int(d.get("total_prompt_tokens", 0) or 0),
            total_completion_tokens=int(d.get("total_completion_tokens", 0) or 0),
            total_cached_tokens=int(d.get("total_cached_tokens", 0) or 0),
            request_count=int(d.get("request_count", 0) or 0),
            last_reset=d.get("last_reset", _now()),
        )

    def reset(self):
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cached_tokens = 0
        self.request_count = 0
        self.last_reset = _now()