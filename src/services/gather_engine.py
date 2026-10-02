"""[P7g] 采集引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b / §23 总纲）：LLM 出静态定义（资源点的 drops/richness/requires_tool/
stat_used），纯 Python 跑动态结算（成功率/产出/丰度递减/冷却再生），SeededRng 确定性。
LLM 不参与「这次采到什么」的动态结果。

公式：
- 成功率 = combat_engine.success_chance(stat, tool_bonus, difficulty, base)
    = clamp(base + stat*0.01 + tool*0.05 - difficulty*0.02, 0.05, 0.95)
- 产出：drops 每条独立 rng.chance(rate) -> {item_id, qty}
- 丰度：每次采集 richness -= richness_decay（无论成败，体现翻找/开采消耗），到 0 枯竭
- 冷却：regenerates=True 且 cooldown_duration>0 时，采后 cooldown_tick = now + duration
- 再生：tick 驱动，冷却到期恢复 richness 到 richness_max（gather_engine.regenerate_node）

题材适配：本引擎只认抽象字段（type/drops/stat_used），题材化名称（灵药田/水池）由
world_sim_service 据题材预设挂载，引擎不感知题材。这让仙侠/现代/科幻共用一套引擎。
"""
from __future__ import annotations

from typing import Any, Optional

from src.utils.rng import SeededRng
from src.utils.dice_check import roll_dice_check, DiceRoll, TIER_CRIT, TIER_OK
from src.services.combat_engine import success_chance


def can_gather(node: Any, current_tick: int) -> bool:
    """资源点当前是否可采（丰度>0 且冷却到期）。"""
    try:
        richness = int(getattr(node, "richness", 0))
    except (TypeError, ValueError):
        richness = 0
    if richness <= 0:
        return False
    try:
        cd = int(getattr(node, "cooldown_tick", 0))
    except (TypeError, ValueError):
        cd = 0
    return cd <= current_tick


def gather_chance(stat_value: int, tool_bonus: int, difficulty: int,
                  base_rate: float = 0.6, chance_bonus: float = 0.0,
                  global_penalty: int = 0) -> float:
    """采集成功率（纯函数，供结算与 UI 预览共用同一口径）。"""
    chance = success_chance(stat_value, tool_bonus, difficulty, base=base_rate,
                            global_penalty=global_penalty)
    # [P24d] 宠物采集加成被动（chance_bonus 直加，钳 0.95 上限）
    if chance_bonus > 0:
        chance = min(0.95, chance + chance_bonus)
    return chance


def gather(
        node: Any,
        rng: SeededRng,
        stat_value: int,
        tool_bonus: int,
        current_tick: int,
        difficulty: int = 0,
        base_rate: float = 0.6,
        richness_decay: int = 15,
        chance_bonus: float = 0.0,
        global_penalty: int = 0,
        dice: Optional[DiceRoll] = None,
) -> dict:
    """采集结算（原地改 node.richness/cooldown_tick/last_gathered_tick）。

    - node: ResourceNode（鸭子类型：richness/richness_max/drops/cooldown_tick/
            cooldown_duration/regenerates）
    - stat_value: 驱动属性值（调用方据 node.stat_used 从 player 读，如 dex/int/str）
    - tool_bonus: 工具加成档位（0=徒手，1=普通工具，2=精良工具...；调用方查背包 tool_for）
    - difficulty: 节点难度（默认用地点 danger * 5，或 0）
    - base_rate: 基础成功率（默认 0.6）
    - chance_bonus: [P24d] 成功率直加项（宠物采集加成被动 +0.10；钳 0.95 上限）
    - global_penalty: [P 验收] 全局难度判定偏移（check_difficulty_penalty，困难下采集更难）
    - dice: [三骰取二] 预掷结果（UI 已演出）；None 则用 SeededRng 派生三枚（NPC/批量）

    返回 summary：
        {success:bool, tier:str, drops:[{item_id,qty}], richness_after:int, depleted:bool,
         chance:float, reason:str}
    """
    # 校验可采
    try:
        richness = int(getattr(node, "richness", 0))
    except (TypeError, ValueError):
        richness = 0
    if richness <= 0:
        return {"success": False, "drops": [], "richness_after": 0, "depleted": True,
                "chance": 0.0, "reason": "枯竭"}
    try:
        cd = int(getattr(node, "cooldown_tick", 0))
    except (TypeError, ValueError):
        cd = 0
    if cd > current_tick:
        return {"success": False, "drops": [], "richness_after": richness,
                "depleted": False, "chance": 0.0, "reason": "冷却中"}

    # 成功率
    chance = gather_chance(stat_value, tool_bonus, difficulty, base_rate=base_rate,
                           chance_bonus=chance_bonus, global_penalty=global_penalty)
    # [三骰取二] 定档（dice 预掷或 SeededRng 派生）
    if dice is not None:
        tier = dice.tier
    else:
        tier = roll_dice_check(chance, rolls=[rng.random() for _ in range(3)]).tier
    success = tier in (TIER_CRIT, TIER_OK)

    # 产出
    drops: list[dict] = []
    if success:
        for entry in (getattr(node, "drops", None) or []):
            if not isinstance(entry, dict):
                continue
            rate = entry.get("rate", 0)
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                continue
            if rate <= 0:
                continue
            # 大成功：所有 drop 条目必中（满载而归）；否则逐条独立 roll
            hit = True if tier == TIER_CRIT else rng.chance(max(0.0, min(1.0, rate)))
            if hit:
                item_id = entry.get("item_id") or entry.get("id") or ""
                try:
                    qty = max(1, int(entry.get("qty", 1)))
                except (TypeError, ValueError):
                    qty = 1
                if item_id:
                    drops.append({"item_id": item_id, "qty": qty})

    # 丰度递减（无论成败，体现翻找/开采消耗资源点）
    try:
        decay = max(0, int(richness_decay))
    except (TypeError, ValueError):
        decay = 15
    new_richness = max(0, richness - decay)
    try:
        node.richness = new_richness
        node.last_gathered_tick = current_tick
    except (AttributeError, TypeError):
        pass
    depleted = new_richness <= 0

    # 设冷却（regenerates=True 且有 duration 才冷却再生；否则采到枯竭就永久空）
    regen = bool(getattr(node, "regenerates", True))
    try:
        duration = max(0, int(getattr(node, "cooldown_duration", 0)))
    except (TypeError, ValueError):
        duration = 0
    if regen and duration > 0:
        try:
            node.cooldown_tick = current_tick + duration
        except (AttributeError, TypeError):
            pass

    return {
        "success": success,
        "tier": tier,
        "drops": drops,
        "richness_after": new_richness,
        "depleted": depleted,
        "chance": chance,
        "reason": "" if success else "采集失败，一无所获",
    }


def regenerate_node(node: Any, current_tick: int) -> bool:
    """冷却到期恢复丰度到上限（tick 驱动，确定性）。

    返回是否实际恢复了（True=丰度从 <max 恢复到 max）。
    """
    if not bool(getattr(node, "regenerates", True)):
        return False
    try:
        richness = int(getattr(node, "richness", 0))
        rmax = int(getattr(node, "richness_max", 100))
        cd = int(getattr(node, "cooldown_tick", 0))
    except (TypeError, ValueError):
        return False
    if rmax <= 0:
        return False
    if richness >= rmax:
        return False
    if cd > current_tick:
        return False  # 仍在冷却
    try:
        node.richness = rmax
    except (AttributeError, TypeError):
        return False
    return True


def tick_resource_nodes(nodes: list, current_tick: int, regen_mult: float = 1.0) -> int:
    """批量再生资源点（tick_world 调用）。返回恢复的数量。

    [季节玩法化 2026-09-06] regen_mult 季节恢复系数（冬 0.5——万物蛰伏）：mult < 1 时
    恢复概率按比例折减（rng-free 确定性：按 (tick 节点冷却取模) 阈值判定），mult >= 1
    行为不变（老口径）。"""
    count = 0
    for n in (nodes or []):
        try:
            if regen_mult < 1.0:
                # 确定性折减（md5 稳定哈希——内置 hash() 跨进程随机，违确定性铁律）：
                # 节点名 + 冷却轮次 -> 命中阈值才恢复
                import hashlib as _hl
                key = f"{getattr(n, 'name', '')}|{current_tick // max(1, int(getattr(n, 'cooldown_duration', 3) or 3))}"
                h = int(_hl.md5(key.encode("utf-8")).hexdigest()[:8], 16) % 100
                if h >= int(regen_mult * 100):
                    continue
            if regenerate_node(n, current_tick):
                count += 1
        except Exception:
            continue
    return count


def find_tool_bonus(inventory_items: list, required_tool: str) -> int:
    """查背包里是否有匹配 required_tool（tool_for key）的工具，返回加成档位。

    简化规则：背包有 tool_for==required_tool 的物品 -> 档位 1（普通工具）。
    未来可据 rarity 升档（rare->2, epic->3）。当前统一 1，徒手 0。
    inventory_items: list[Item-like]（鸭子类型，须有 tool_for 字段）。
    """
    if not required_tool:
        return 0
    for it in (inventory_items or []):
        if getattr(it, "tool_for", "") == required_tool:
            # 据稀有度升档：common/uncommon->1, rare->2, epic->3, legendary->4
            rarity = getattr(it, "rarity", "common") or "common"
            return {"common": 1, "uncommon": 1, "rare": 2, "epic": 3, "legendary": 4, "mythic": 5}.get(rarity, 1)
    return 0
