"""[三骰取二判定 2026-08-26] 纯逻辑判定（零 Qt 零 storage）。

把游戏的「一次 pass/fail 判定（成功概率 p）」升级为「3 骰取最好 2」的演出化判定：
- 每骰读数 = 100 - floor(r*100) ∈ [1,100]，越高越好（r 越小越成功 -> 读数越大）。
- 每骰成功线 = 101 - round(p*100)；每骰大成功线 = crit_thresh（默认 95）。
- 定档（成功/大成功优先，天然 1 只在失败时降级）：
    crit   = 最好两骰都 >= crit_thresh
    ok     = 最好两骰 2 成功，或 1 大成功 + 1 成功
    fail   = 其余
    fumble = fail 且 3 骰中出现读数 1（天然大失败）
- 纯函数：给定 rolls 结果确定（测试/预掷/SeededRng 派生）；rolls=None 用 random.random()
  真随机三枚（玩家可见骰）。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

TIER_CRIT = "crit"
TIER_OK = "ok"
TIER_FAIL = "fail"
TIER_FUMBLE = "fumble"

CRIT_THRESHOLD = 95  # 单骰大成功线（读数 >= 95，即约 6% 概率）


@dataclass
class DiceRoll:
    """一次三骰判定的完整结果。"""
    rolls: list[float]              # 3 个原始随机数 [0,1)
    reads: list[int]                # 3 个读数 1-100
    faces: list[tuple[int, int]]    # 每骰 (十位, 个位)，读数 mod 100（00+0=100 惯例）
    best_reads: list[int]           # 最好两骰读数（降序）
    tier: str                       # crit / ok / fail / fumble
    success_line: int               # 成功线（读数 >= 此线即成功）
    crit_thresh: int                # 大成功线


def _roll_to_read(r: float) -> int:
    """[0,1) -> 1-100 读数（越高越好）。r=0 -> 100（天然满骰），r→1 -> 1。"""
    r = max(0.0, min(1.0, float(r)))
    return 100 - min(99, int(r * 100))


def roll_dice_check(p: float, crit_thresh: int = CRIT_THRESHOLD,
                    rolls: list[float] | None = None) -> DiceRoll:
    """三骰取二判定。`rolls` 为 None 时真随机三枚；否则用给定值（长度不足补随机）。

    返回的 tier 语义见模块 docstring。成功线由 `p` 推出，与旧的 `r < p` 口径数学等价
    （对整数百分比严格成立）：读数 >= 101-round(p*100) ⟺ r < p。
    """
    if rolls is None:
        rolls = [random.random() for _ in range(3)]
    rolls = [float(r) for r in rolls[:3]]
    while len(rolls) < 3:
        rolls.append(random.random())

    reads = [_roll_to_read(r) for r in rolls]
    faces = [(read % 100 // 10, read % 10) for read in reads]

    p_pct = max(0, min(100, int(round(float(p) * 100))))
    success_line = 101 - p_pct
    crit_thresh = max(success_line, min(100, int(crit_thresh)))

    best = sorted(reads, reverse=True)[:2]
    n_crit = sum(1 for rd in best if rd >= crit_thresh)
    n_ok = sum(1 for rd in best if success_line <= rd < crit_thresh)

    if n_crit >= 2:
        tier = TIER_CRIT
    elif n_crit == 1 and n_ok >= 1:
        tier = TIER_OK
    elif n_ok >= 2:
        tier = TIER_OK
    else:
        tier = TIER_FAIL
        if any(rd == 1 for rd in reads):
            tier = TIER_FUMBLE

    return DiceRoll(rolls=rolls, reads=reads, faces=faces,
                    best_reads=best, tier=tier,
                    success_line=success_line, crit_thresh=crit_thresh)
