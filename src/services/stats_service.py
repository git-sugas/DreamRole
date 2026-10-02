"""统计服务：按 API 记录 token 消耗与缓存命中、估算费用。"""
from __future__ import annotations
from src.services.storage import Storage
from src.services.llm_client import LlmUsage
from src.models import ApiStats, ApiConfig


class StatsService:
    """管理各 API 的统计数据。"""

    def __init__(self):
        self.storage = Storage()

    def record_usage(self, api_id: str, usage: LlmUsage):
        """记录一次 API 调用的 usage（原子自增，防并发读-改-写竞态）。"""
        self.storage.increment_stats(
            api_id, usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens,
        )

    def get_stats(self, api_id: str) -> ApiStats:
        return self.storage.get_stats(api_id)

    def get_all_stats(self) -> list[ApiStats]:
        return self.storage.get_all_stats()

    def reset(self, api_id: str):
        self.storage.reset_stats(api_id)

    def reset_all(self):
        for stats in self.get_all_stats():
            self.reset(stats.api_id)

    # ============ 费用估算 ============
    @staticmethod
    def compute_cost(stats: ApiStats, api: ApiConfig | None) -> float:
        """按当前费率估算已消耗费用（人民币元）。

        公式：
          cost = (prompt - cached) * input_price / 1e6
                + cached * cache_price / 1e6
                + completion * output_price / 1e6
        单位：费率为「元/百万 token」。费率缺失/无 api 配置时返回 0。
        注意：按当前费率对全部历史 token 估算，改费率后历史费用会随之变化（非累计快照）。
        """
        if api is None:
            return 0.0
        uncached_prompt = max(0, stats.total_prompt_tokens - stats.total_cached_tokens)
        cost = (
            uncached_prompt * api.input_price
            + stats.total_cached_tokens * api.cache_price
            + stats.total_completion_tokens * api.output_price
        ) / 1_000_000.0
        return round(cost, 6)

    def get_cost(self, api_id: str) -> float:
        """获取单个 API 的已消耗费用（按其当前费率）。"""
        stats = self.storage.get_stats(api_id)
        api = self.storage.load_api(api_id)
        return self.compute_cost(stats, api)

    def get_total_cost(self, api_ids: list[str] | None = None) -> float:
        """获取多个 API 的合计费用。api_ids 为 None 时取全部 API。"""
        if api_ids is None:
            api_ids = [s.api_id for s in self.get_all_stats()]
        total = 0.0
        for aid in api_ids:
            total += self.get_cost(aid)
        return round(total, 6)