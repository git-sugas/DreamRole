"""种子化随机数生成器（世界模拟 P3 战斗引擎用）。

确定性可复现：同 world_id + tick + action 序列 -> 同结果。
避免 LLM 不可复现的随机，让战斗结果可单测、可回放。

设计：
- SeededRng 包装 random.Random(seed)，不污染全局 random 状态。
- seed_from(world_id, tick) 生成稳定种子（hash 字符串）。
- 提供 roll/chance/pick/weighted 四个语义化方法。
"""
from __future__ import annotations

import random
from typing import Any, Sequence


class SeededRng:
    """种子化随机数生成器（确定性，可复现）。"""

    def __init__(self, seed):
        self._r = random.Random(seed)

    @classmethod
    def seed_from(cls, world_id: str, tick: int, salt: str = "") -> "SeededRng":
        """据 world_id + tick（+ 可选 salt）生成稳定种子。

        同 world + 同 tick + 同 salt -> 同序列随机数，战斗结果可复现。
        salt 用于区分同一回合内的多次掷骰（如玩家攻击 vs NPC 反击）。
        """
        key = f"{world_id}|{tick}|{salt}"
        # 用字符串 hash 作种子（Python hash 跨进程不稳，改用稳定 hash）
        import hashlib
        h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % (2 ** 32)
        return cls(h)

    def roll(self, lo: int, hi: int) -> int:
        """[lo, hi] 闭区间整数。"""
        return self._r.randint(lo, hi)

    def chance(self, p: float) -> bool:
        """以概率 p(0.0-1.0) 返回 True。"""
        if p <= 0.0:
            return False
        if p >= 1.0:
            return True
        return self._r.random() < p

    def pick(self, items: Sequence[Any]) -> Any:
        """等概率随机选一个；空列表返回 None。"""
        if not items:
            return None
        return self._r.choice(items)

    def weighted(self, items: Sequence[Any], weights: Sequence[float]) -> Any:
        """按权重随机选一个；空返回 None。"""
        if not items:
            return None
        if len(weights) != len(items):
            return self._r.choice(items)
        return self._r.choices(items, weights=weights, k=1)[0]

    def sample(self, items: Sequence[Any], k: int) -> list:
        """[P7g] 从 items 随机选 k 个（不重复）；k>=len 时返回全部打乱。空列表返回 []。"""
        if not items:
            return []
        k = max(0, min(int(k), len(items)))
        return self._r.sample(list(items), k)

    def random(self) -> float:
        """[0.0, 1.0) 浮点。"""
        return self._r.random()
