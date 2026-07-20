"""API 配置数据模型。"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field


# ---- 思考级别（reasoning_effort）可选值 ----
# "none" 表示不发送该参数（适配非思考型模型，如 gpt-4o / deepseek-chat 普通模式）
# 其余值按 OpenAI o-series 约定随请求体发送 reasoning_effort 字段，
# 兼容多数采纳该约定的 OpenAI 兼容服务。
THINKING_NONE = "none"
THINKING_LEVELS = ["none", "minimal", "low", "medium", "high"]
THINKING_LABELS = {
    "none": "关闭（普通模型）",
    "minimal": "极简 (minimal)",
    "low": "低 (low)",
    "medium": "中 (medium)",
    "high": "高 (high)",
}


@dataclass
class ApiConfig:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    base_url: str = ""             # OpenAI 兼容地址，如 https://api.openai.com/v1
    api_key: str = ""
    model: str = ""                # 默认模型
    preset_id: str = ""            # 绑定的预设 id
    enabled: bool = True
    # ---- 生成行为（适配主流模型，按 API 维度配置）----
    streaming: bool = True             # 是否使用流式传输
    reasoning_effort: str = THINKING_NONE  # 思考级别: none/minimal/low/medium/high
    # ---- Embedding（可单独配置，留空则复用上方 base_url/api_key）----
    embedding_model: str = ""          # embedding 模型名
    embedding_base_url: str = ""       # 留空则用 base_url
    embedding_api_key: str = ""        # 留空则用 api_key
    # ---- 计费费率（人民币 元/百万 token，用于估算已消耗费用）----
    # 留空/0 表示不计费；缓存命中部分按 cache_price 单独计（通常更低甚至免费）。
    input_price: float = 0.0           # 输入 token 单价（元/百万）
    output_price: float = 0.0          # 输出 token 单价（元/百万）
    cache_price: float = 0.0           # 缓存命中 token 单价（元/百万）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "preset_id": self.preset_id,
            "enabled": self.enabled,
            "streaming": self.streaming,
            "reasoning_effort": self.reasoning_effort,
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": self.embedding_api_key,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "cache_price": self.cache_price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ApiConfig:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            base_url=d.get("base_url", "") or "",
            api_key=d.get("api_key", "") or "",
            model=d.get("model", "") or "",
            preset_id=d.get("preset_id", "") or "",
            enabled=bool(d.get("enabled", True)),
            streaming=bool(d.get("streaming", True)),
            reasoning_effort=(
                d.get("reasoning_effort", THINKING_NONE)
                if d.get("reasoning_effort") in THINKING_LEVELS
                else THINKING_NONE
            ),
            embedding_model=d.get("embedding_model", "") or "",
            embedding_base_url=d.get("embedding_base_url", "") or "",
            embedding_api_key=d.get("embedding_api_key", "") or "",
            input_price=float(d.get("input_price", 0.0) or 0.0),
            output_price=float(d.get("output_price", 0.0) or 0.0),
            cache_price=float(d.get("cache_price", 0.0) or 0.0),
        )

    @property
    def effective_embedding_base_url(self) -> str:
        return self.embedding_base_url or self.base_url

    @property
    def effective_embedding_api_key(self) -> str:
        return self.embedding_api_key or self.api_key