"""[P34b] 装备鉴定/洗练 + 宠物资质洗练引擎（纯 Python + SeededRng，无 LLM/Qt）。

呼应梦幻「洗出不同属性/技能」：装备鉴定揭示隐藏 affix + 赋题材化前缀名；装备洗练重 roll
affix/前缀；宠物资质系统——资质挂钩属性成长系数，不同资质丹洗不同维度。

数值范式铁律（守 §22）：零 LLM，前缀名/资质档来自题材化池（P34a _GENRE_AFFIX_PREFIX_NAMES），
所有 roll 走 SeededRng 确定性（同 world+item+tick+salt 同结果）。

[!] 派生 id 后缀按用途隔离（守 read.md P34a 不变量）：升档 `__{rarity}`、未鉴定掉落 `__unid`、
鉴定 `__idn`、洗练 `__rr{salt}`——同源物品经不同子系统派生不撞 id。
"""
from __future__ import annotations

from typing import Any, Optional

from src.utils.rng import SeededRng
from src.utils.dice_check import roll_dice_check, DiceRoll, TIER_CRIT, TIER_OK, TIER_FUMBLE


# ---- 装备 affix 配置（roll_affixes 用）----

# 稀有度 -> affix 条数范围 (min, max)。common 多半无 affix，legendary 必有 2-3 条。
_RARITY_AFFIX_COUNT = {
    "common": (0, 1),
    "uncommon": (1, 1),
    "rare": (1, 2),
    "epic": (2, 2),
    "legendary": (2, 3),
}

# affix 维度 -> mods key 映射（roll_affixes 据维度生成 mods）。
# [!] mods key 必须与 combat_engine._affix_val 读取的 key 一致（atk/def/crit/magic_atk），
# 否则 affix 只显示不生效（compute_stats._affix_val 读 mods.get(key)）。
# atk->atk / def->def / crit->crit(暴击率小数) / magic->magic_atk(法攻) /
# stat->stat_bonus(随机一维属性，compute_stats 直接读 stat_bonus 字典)。
# [装备新属性 2026-09-01] counter/combo/lifesteal 概率小数；element 是字符串 key
# （非 numeric——compute_stats 经 _affix_str 读入快照 elements 吃克制矩阵）。
_AFFIX_DIM_MODKEYS = {
    "atk": "atk",
    "def": "def",
    "crit": "crit",
    "magic": "magic_atk",
    "stat": "stat_bonus",
    "counter": "counter_rate",
    "combo": "combo_rate",
    "lifesteal": "lifesteal",
    "element": "element",
}

# affix 维度 -> stat_bonus 字段（stat 维度随机选 str/dex/int/vit/luk；crit/magic 不走 stat_bonus）
_AFFIX_DIM_STATKEY = {
    "stat": "",  # roll 时随机选 str/dex/int/vit/luk
}
_STAT_KEYS_LIST = ("str", "dex", "int", "vit", "luk")

# affix 维度 -> 基准值（按 rarity 缩放：base * rarity_mult）
# crit 是暴击率小数（0.02-0.06 档），其余是整数加成。
# [装备新属性] counter/combo/lifesteal 同为概率小数（rarity 缩放后保 3 位）。
_AFFIX_DIM_BASE = {
    "atk": 3,
    "def": 2,
    "crit": 0.03,
    "magic": 3,
    "stat": 2,
    "counter": 0.04,
    "combo": 0.05,
    "lifesteal": 0.04,
}

# [装备新属性] element 词缀的元素池（roll 随机取一；key 同元素克制矩阵白名单）
_AFFIX_ELEMENT_POOL = ("fire", "ice", "thunder", "wood", "earth", "metal", "wind", "light", "dark")

# 稀有度 -> affix 数值倍率（与 combat_engine _RARITY_MULT 同口径）
_RARITY_MULT = {"common": 1.0, "uncommon": 1.3, "rare": 1.7, "epic": 2.2, "legendary": 3.0, "mythic": 4.0}

# 鉴定成功率：低 tier 卷轴鉴定高 level 物品成功率降。
# base 0.95，每 level 差 -0.10，钳 [0.40, 0.98]。
_APPRAISE_LEVEL_PENALTY = 0.10


def _roll_affixes(rng: SeededRng, item: Any, world: Any, extra_affix: bool = False) -> list[dict]:
    """[P34b] 据物品 rarity/level roll affixes + 赋题材化前缀名。

    返回 affix list，每条 {slot, name, mods}。slot=prefix/suffix；name 从题材前缀名池取；
    mods 按维度 + rarity 缩放（attack/defense 写对应字段，crit/magic/stat 写 stat_bonus 子字段）。
    [!] 确定性：同 world+item.id+salt 同结果（rng 由调用方传独立 seed，不破坏既有序列）。
    [!] affix 数量据 rarity：common 0-1 / legendary 2-3。level 高的物品 affix 数值更强
        （base * rarity_mult * (1 + level*0.1)）。
    """
    from src.services.world_sim_service import affix_prefix_pool

    rarity = getattr(item, "rarity", "common") or "common"
    lvl = max(0, int(getattr(item, "level", 0) or 0))
    lo, hi = _RARITY_AFFIX_COUNT.get(rarity, (0, 1))
    n = rng.roll(lo, hi) if hi > 0 else 0
    if extra_affix and hi > 0:
        n = max(n, min(hi, lo + 1))  # 大成功：词缀保底多一条（封顶 hi）
    if n <= 0:
        return []
    pool = affix_prefix_pool(world)  # {dim: [前缀名...]}
    dims = list(pool.keys()) if pool else list(_AFFIX_DIM_MODKEYS.keys())
    if not dims:
        return []
    rmult = _RARITY_MULT.get(rarity, 1.0)
    lvl_factor = 1.0 + lvl * 0.1
    out = []
    used_dims = set()
    for _ in range(n):
        # 选维度（不重复，affix 维度多样）
        avail = [d for d in dims if d not in used_dims] or dims
        dim = avail[rng.roll(0, len(avail) - 1)]
        used_dims.add(dim)
        names = pool.get(dim) or []
        name = names[rng.roll(0, len(names) - 1)] if names else dim
        base = _AFFIX_DIM_BASE.get(dim, 2)
        modkey = _AFFIX_DIM_MODKEYS.get(dim, "stat_bonus")
        # [!] crit 是暴击率小数（0.03 档），按 rarity/lvl 缩放后保留 3 位小数；
        # 其余维度是整数加成（max 1 保底）。
        # [装备新属性 2026-09-01] counter/combo/lifesteal 同概率小数档；element 是
        # 字符串 key（随机元素，非 numeric 缩放）。
        if modkey == "crit":
            val = round(base * rmult * lvl_factor, 3)
            mods = {modkey: val}
        elif modkey in ("counter_rate", "combo_rate", "lifesteal"):
            # 概率钳 0.5 上限（与 compute_stats 终值钳同口径，防 epic+ 单词条超限）
            val = min(0.5, round(base * rmult * lvl_factor, 3))
            mods = {modkey: val}
        elif modkey == "element":
            mods = {"element": _AFFIX_ELEMENT_POOL[rng.roll(0, len(_AFFIX_ELEMENT_POOL) - 1)]}
        elif modkey == "stat_bonus":
            statkey = _STAT_KEYS_LIST[rng.roll(0, len(_STAT_KEYS_LIST) - 1)]
            val = max(1, int(base * rmult * lvl_factor))
            mods = {"stat_bonus": {statkey: val}}
        else:
            val = max(1, int(base * rmult * lvl_factor))
            mods = {modkey: val}
        slot = "prefix"  # v1 统一前缀（前缀名前置显示）；suffix 留扩展位
        out.append({"slot": slot, "name": name, "mods": mods})
    return out


def _derived_id(item: Any, suffix: str, salt: str = "") -> str:
    """[P34b] 派生确定性 id：{origid}__{suffix}{salt}（守 §22 派生 id 不变量）。"""
    base = str(getattr(item, "id", "") or "")
    return f"{base}__{suffix}{salt}"


def _find_item_in_inventory(world: Any, player: Any, item_id: str) -> Optional[Any]:
    """从 world.items 按 id 找物品。"""
    for it in (getattr(world, "items", None) or []):
        if getattr(it, "id", "") == item_id:
            return it
    return None


# ---- 装备鉴定（appraise）----

def _degrade_item_durability(world: Any, item_id: str, decay: int = 1):
    """[三骰取二] 大失败惩罚：该物品耐久 -decay（不朽 durability_max<=0 跳过）。"""
    it = next((i for i in (getattr(world, "items", None) or [])
               if getattr(i, "id", "") == item_id), None)
    if it is None:
        return
    try:
        dmax = int(getattr(it, "durability_max", 0) or 0)
        if dmax <= 0:
            return
        it.durability = max(0, int(getattr(it, "durability", dmax) or 0) - int(decay))
    except (TypeError, ValueError, AttributeError):
        pass


def appraise_chance(item: Any, reagent: Any) -> float:
    """鉴定/洗练成功率（纯函数，供结算与 UI 预览共用同一口径）。"""
    reagent_tier = max(1, min(3, int(getattr(reagent, "level", 1) or 1)))
    item_lvl = max(0, int(getattr(item, "level", 0) or 0))
    diff = max(0, item_lvl - reagent_tier)
    return max(0.40, min(0.98, 0.95 - diff * _APPRAISE_LEVEL_PENALTY))


def appraise_check(world: Any, player: Any, item_id: str, reagent_id: str) -> tuple:
    """鉴定前置校验（不消耗、不 roll）。返回 (ok, reason)。UI 弹骰前先调。"""
    from src.models.world import effective_category
    from src.services import combat_engine as ce
    item = _find_item_in_inventory(world, player, item_id)
    if item is None:
        return False, "找不到该物品"
    if item_id not in (getattr(player, "inventory", None) or []):
        return False, "该物品不在背包（装备中请先卸下，入库请先取回）"
    if getattr(item, "type", "") not in ("weapon", "armor", "accessory"):
        return False, "只有装备可以鉴定"
    if getattr(item, "identified", True) is not False:
        return False, "该物品已鉴定"
    reagent = _find_item_in_inventory(world, player, reagent_id)
    if reagent is None or reagent_id not in (getattr(player, "inventory", None) or []):
        return False, "没有该鉴定卷轴"
    if effective_category(reagent) != "cultivate" or getattr(reagent, "reagent_kind", "") != "identify":
        return False, "该物品不是鉴定卷轴"
    if len(getattr(player, "inventory", None) or []) > ce.carry_capacity(player):
        return False, "背包超容，请先整理再鉴定"
    return True, ""


def refine_check(world: Any, player: Any, item_id: str, reagent_id: str) -> tuple:
    """洗练前置校验（不消耗、不 roll）。返回 (ok, reason)。UI 弹骰前先调。"""
    from src.models.world import effective_category
    from src.services.combat_engine import carry_capacity
    item = _find_item_in_inventory(world, player, item_id)
    if item is None:
        return False, "找不到该物品"
    if item_id not in (getattr(player, "inventory", None) or []):
        return False, "该物品不在背包（装备中请先卸下，入库请先取回）"
    if getattr(item, "type", "") not in ("weapon", "armor", "accessory"):
        return False, "只有装备可以洗练"
    if getattr(item, "identified", True) is False:
        return False, "该物品未鉴定，请先鉴定"
    reagent = _find_item_in_inventory(world, player, reagent_id)
    if reagent is None or reagent_id not in (getattr(player, "inventory", None) or []):
        return False, "没有该洗练石"
    if len(getattr(player, "inventory", None) or []) > carry_capacity(player):
        return False, "背包超容，请先整理再洗练"
    if effective_category(reagent) != "cultivate" or getattr(reagent, "reagent_kind", "") != "refine":
        return False, "该物品不是洗练石"
    return True, ""


def appraise(world: Any, player: Any, item_id: str, scroll_item_id: str,
             rng: Optional[SeededRng] = None, dice: Optional[DiceRoll] = None) -> dict:
    """[P34b] 装备鉴定：消耗鉴定卷轴揭示未鉴定装备的 affix + 赋前缀名。

    流程：(1) 校验目标物品是装备 + 未鉴定；(2) 校验卷轴是 cultivate/identify 类 + 在背包；
    (3) 据卷轴 tier vs 物品 level 算成功率（低 tier 卷轴鉴定高 level 物品成功率降）；
    (4) roll 成功 -> 克隆物品（id `{origid}__idn`）identified=True + roll_affixes 赋前缀名，
    幂等入 world.items，背包换 id；(5) 失败 -> 卷轴消耗物品不变（可再试）。
    返回 {ok, reason, identified, new_item_id, affixes}。
    [!] 鉴定产 clone 走 try_add_to_inventory 换 id（守 §22 入包统一不变量）；卷轴消耗从背包移除。
    [!] 已鉴定物品拒绝（防重复鉴定刷 affix）。
    """
    from src.services import combat_engine as ce

    ok, reason = appraise_check(world, player, item_id, scroll_item_id)
    if not ok:
        item = _find_item_in_inventory(world, player, item_id)
        identified = False if item is None else (getattr(item, "identified", True) is not False)
        return {"ok": False, "reason": reason, "identified": identified}
    item = _find_item_in_inventory(world, player, item_id)
    scroll = _find_item_in_inventory(world, player, scroll_item_id)
    # 卷轴 tier（=level）vs 物品 level 算成功率
    chance = appraise_chance(item, scroll)
    # 消耗卷轴（无论成败）
    inv = getattr(player, "inventory", None)
    if isinstance(inv, list) and scroll_item_id in inv:
        inv.remove(scroll_item_id)
    if rng is None:
        rng = SeededRng.seed_from(getattr(world, "id", ""), int(getattr(world, "tick_count", 0) or 0),
                                  f"appraise_{item_id}")
    if dice is not None:
        tier = dice.tier
    else:
        tier = roll_dice_check(chance, rolls=[rng.random() for _ in range(3)]).tier
    if tier not in (TIER_CRIT, TIER_OK):
        if tier == TIER_FUMBLE:
            _degrade_item_durability(world, item_id)
            return {"ok": False, "reason": f"鉴定大失败（成功率{int(chance*100)}%），卷轴已消耗，装备耐久-1",
                    "identified": False, "chance": chance, "tier": tier}
        return {"ok": False, "reason": f"鉴定失败（成功率{int(chance*100)}%），卷轴已消耗",
                "identified": False, "chance": chance, "tier": tier}
    # 成功：克隆物品 identified=True + roll affixes
    import copy as _copy
    new_id = _derived_id(item, "idn")
    # 幂等：已存在克隆复用（同源装备多次鉴定——理论上不会，因已鉴定会被拒，但防御性）
    existing = next((i for i in (getattr(world, "items", None) or []) if getattr(i, "id", "") == new_id), None)
    if existing is not None:
        clone = existing
    else:
        clone = _copy.copy(item)
        clone.id = new_id
        clone.identified = True
        clone.affixes = _roll_affixes(rng, item, world, extra_affix=(tier == TIER_CRIT))
        getattr(world, "items").append(clone)
    # 背包换 id
    if isinstance(inv, list) and item_id in inv:
        inv.remove(item_id)
    # [I2] 入包失败兜底（超容 / 玩家已持有同源克隆 id）：还原旧 id，绝不净丢一件还报成功
    if not ce.try_add_to_inventory(player, new_id):
        if isinstance(inv, list) and item_id not in inv:
            inv.append(item_id)
        reason = ("已持有同源鉴定产物，物品已还原" if new_id in (inv or [])
                  else "背包容量异常，物品已还原")
        return {"ok": False, "reason": reason, "identified": False}
    return {"ok": True, "reason": "鉴定成功", "identified": True,
            "new_item_id": new_id, "affixes": clone.affixes, "chance": chance, "tier": tier}


# ---- 装备洗练（refine）----

def refine(world: Any, player: Any, item_id: str, stone_item_id: str,
           rng: Optional[SeededRng] = None, dice: Optional[DiceRoll] = None) -> dict:
    """[P34b] 装备洗练：消耗洗练石重 roll 装备的全部 affixes + 前缀名。

    流程：(1) 校验目标物品是装备 + 已鉴定（未鉴定先鉴定）；(2) 校验洗练石 cultivate/refine 类
    + 在背包 + tier 匹配物品 level；(3) 消耗洗练石；(4) 克隆物品（id `{origid}__rr{salt}`）
    重 roll affixes + 前缀名，幂等入 world.items，背包换 id。
    [!] 洗练是「重 roll」（替换全部 affix），不是「追加」——呼应梦幻洗出不同属性。
    [!] 洗练石 tier < 物品 level 时成功率降（仿鉴定口径）；成功才重 roll。
    [!] bag-full 预检（仿 can_craft）：背包满则不消耗洗练石拒绝（防洗出物品入不了包丢）。
    """
    from src.services import combat_engine as ce

    ok, reason = refine_check(world, player, item_id, stone_item_id)
    if not ok:
        return {"ok": False, "reason": reason}
    item = _find_item_in_inventory(world, player, item_id)
    stone = _find_item_in_inventory(world, player, stone_item_id)
    chance = appraise_chance(item, stone)
    inv = getattr(player, "inventory", None)
    # 消耗洗练石
    if isinstance(inv, list) and stone_item_id in inv:
        inv.remove(stone_item_id)
    if rng is None:
        rng = SeededRng.seed_from(getattr(world, "id", ""), int(getattr(world, "tick_count", 0) or 0),
                                  f"refine_{item_id}")
    if dice is not None:
        tier = dice.tier
    else:
        tier = roll_dice_check(chance, rolls=[rng.random() for _ in range(3)]).tier
    if tier not in (TIER_CRIT, TIER_OK):
        if tier == TIER_FUMBLE:
            _degrade_item_durability(world, item_id)
            return {"ok": False, "reason": f"洗练大失败（成功率{int(chance*100)}%），洗练石已消耗，装备耐久-1",
                    "chance": chance, "tier": tier}
        return {"ok": False, "reason": f"洗练失败（成功率{int(chance*100)}%），洗练石已消耗",
                "chance": chance, "tier": tier}
    # 成功：克隆重 roll affixes
    import copy as _copy
    salt = str(int(getattr(world, "tick_count", 0) or 0))
    new_id = _derived_id(item, "rr", salt)
    existing = next((i for i in (getattr(world, "items", None) or []) if getattr(i, "id", "") == new_id), None)
    if existing is not None:
        clone = existing
    else:
        clone = _copy.copy(item)
        clone.id = new_id
        clone.identified = True  # 洗练保持鉴定态
        clone.affixes = _roll_affixes(rng, item, world, extra_affix=(tier == TIER_CRIT))
        getattr(world, "items").append(clone)
    # 背包换 id
    if isinstance(inv, list) and item_id in inv:
        inv.remove(item_id)
    # [I2] 入包失败兜底（超容 / 玩家已持有同源克隆 id）：还原旧 id，绝不净丢一件还报成功
    if not ce.try_add_to_inventory(player, new_id):
        if isinstance(inv, list) and item_id not in inv:
            inv.append(item_id)
        reason = ("已持有同源洗练产物，物品已还原" if new_id in (inv or [])
                  else "背包容量异常，物品已还原")
        return {"ok": False, "reason": reason}
    return {"ok": True, "reason": "洗练成功", "new_item_id": new_id,
            "affixes": clone.affixes, "chance": chance, "tier": tier}


# ---- 宠物资质系统（aptitude）----

# 资质维度 key（与 build_pet_unit 五维对应：atk->str/def->vit/hp->vit/mp->int/spd->dex）
# 资质值 = 成长系数档（0.8x-1.5x），决定 build_pet_unit 同 level 属性强弱。
_PET_APTITUDE_KEYS = ("atk", "def", "hp", "mp", "spd")

# 资质维度 -> 影响的 stat key（build_pet_unit 聚合时乘资质系数）
_APTITUDE_STAT = {
    "atk": "str",
    "def": "vit",
    "hp": "vit",   # hp 与 def 都影响 vit（hp 资质额外影响 hp_max，build_pet_unit 聚合）
    "mp": "int",
    "spd": "dex",
}

# 资质档位范围（roll 时在此区间抖动，2 位小数）
_APTITUDE_MIN = 0.80
_APTITUDE_MAX = 1.50

# 资质丹维度 key -> 对应 _PET_APTITUDE_KEYS 的一个；apt_all = 全部
_APTITUDE_PILL_DIMS = ("apt_atk", "apt_def", "apt_hp", "apt_mp", "apt_spd", "apt_all")


def default_aptitude() -> dict:
    """[P34b] 新宠物默认资质（全 1.0 基线，create_pet 用）。"""
    return {k: 1.0 for k in _PET_APTITUDE_KEYS}


def roll_aptitude(rng: SeededRng, dims: list[str] = None) -> dict:
    """[P34b] roll 指定维度的资质（重 roll 替换）。dims=None 重 roll 全部。

    返回 {dim: 资质值}（仅含 roll 的维度）。资质值在 [_APTITUDE_MIN, _APTITUDE_MAX] 区间
    2 位小数抖动。确定性（同 rng 同结果）。
    """
    dims = dims or list(_PET_APTITUDE_KEYS)
    out = {}
    for d in dims:
        if d not in _PET_APTITUDE_KEYS:
            continue
        v = _APTITUDE_MIN + rng.random() * (_APTITUDE_MAX - _APTITUDE_MIN)
        out[d] = round(v, 2)
    return out


def appraise_pet(world: Any, player: Any, pet_id: str, scroll_item_id: str,
                 rng: Optional[SeededRng] = None) -> dict:
    """[P34b] 宠物鉴定：消耗鉴定卷轴揭示未鉴定宠物的资质 + 种族被动。

    [!] 宠物是唯一实体（不克隆，直接改 pet.identified=True）。与装备鉴定不同——装备掉落是
    模板克隆，宠物驯服时已建唯一实体。
    """
    from src.models.world import effective_category

    pet = _find_pet(world, player, pet_id)
    if pet is None:
        return {"ok": False, "reason": "找不到该宠物"}
    if getattr(pet, "identified", True) is not False:
        return {"ok": False, "reason": "该宠物已鉴定", "identified": True}
    scroll = _find_item_in_inventory(world, player, scroll_item_id)
    if scroll is None or scroll_item_id not in (getattr(player, "inventory", None) or []):
        return {"ok": False, "reason": "没有该鉴定卷轴"}
    # [P34b] 子类型校验：宠物鉴定同样需 reagent_kind=="identify"
    if effective_category(scroll) != "cultivate" or getattr(scroll, "reagent_kind", "") != "identify":
        return {"ok": False, "reason": "该物品不是鉴定卷轴"}
    # 宠物鉴定不区分 tier（宠物无 level 概念，用 tier 档）；成功率固定 0.95
    inv = getattr(player, "inventory", None)
    if isinstance(inv, list) and scroll_item_id in inv:
        inv.remove(scroll_item_id)
    if rng is None:
        rng = SeededRng.seed_from(getattr(world, "id", ""), int(getattr(world, "tick_count", 0) or 0),
                                  f"appraise_pet_{pet_id}")
    if not rng.chance(0.95):
        return {"ok": False, "reason": "鉴定失败，卷轴已消耗", "identified": False}
    pet.identified = True
    return {"ok": True, "reason": "鉴定成功", "identified": True, "aptitude": pet.aptitude}


def refine_pet(world: Any, player: Any, pet_id: str, pill_item_id: str,
               rng: Optional[SeededRng] = None) -> dict:
    """[P34b] 宠物资质洗练：消耗资质丹重 roll 对应维度的资质。

    资质丹维度（pill_item_id 对应 cultivate reagent 的 kind）：apt_atk/apt_def/apt_hp/apt_mp/
    apt_spd 各重 roll 对应维度；apt_all 重 roll 全部。
    [!] 亲和阈值门控：affinity >= 50 才解锁洗练（与 P24d 被动解锁口径一致）。
    [!] 宠物是唯一实体，直接改 pet.aptitude（不克隆）。
    """
    from src.models.world import effective_category

    pet = _find_pet(world, player, pet_id)
    if pet is None:
        return {"ok": False, "reason": "找不到该宠物"}
    if getattr(pet, "identified", True) is False:
        return {"ok": False, "reason": "该宠物未鉴定，请先鉴定"}
    if int(getattr(pet, "affinity", 0) or 0) < 50:
        return {"ok": False, "reason": "亲和不足50，无法洗练资质"}
    pill = _find_item_in_inventory(world, player, pill_item_id)
    if pill is None or pill_item_id not in (getattr(player, "inventory", None) or []):
        return {"ok": False, "reason": "没有该资质丹"}
    pill_kind = getattr(pill, "reagent_kind", "") or ""
    # [P34b] 子类型校验：资质丹须 reagent_kind 以 apt_ 开头（apt_atk/def/hp/mp/spd/apt_all）
    if effective_category(pill) != "cultivate" or not pill_kind.startswith("apt_"):
        return {"ok": False, "reason": "该物品不是资质丹"}
    # 资质丹维度：apt_all -> 全 roll；apt_atk/apt_def/... -> 重 roll 对应单维度
    _APT_KIND_DIM = {"apt_atk": "atk", "apt_def": "def", "apt_hp": "hp",
                     "apt_mp": "mp", "apt_spd": "spd"}
    dims = None if pill_kind == "apt_all" else [_APT_KIND_DIM.get(pill_kind, "")]
    dims = [d for d in (dims or []) if d] or None
    inv = getattr(player, "inventory", None)
    if isinstance(inv, list) and pill_item_id in inv:
        inv.remove(pill_item_id)
    if rng is None:
        rng = SeededRng.seed_from(getattr(world, "id", ""), int(getattr(world, "tick_count", 0) or 0),
                                  f"refine_pet_{pet_id}")
    new_apt = roll_aptitude(rng, dims)  # apt_all 全 roll，单维度丹只 roll 对应维度
    pet.aptitude.update(new_apt)
    return {"ok": True, "reason": "资质洗练成功", "aptitude": dict(pet.aptitude)}


# 洗技能亲和门槛（低于资质洗练的 50——用户定稿：洗技能比洗资质宽松）
PET_SKILL_REROLL_AFFINITY_AT = 30


def reroll_pet_skill(world: Any, player: Any, pet_id: str, slot: int,
                     scroll_item_id: str, rng: Optional[SeededRng] = None) -> dict:
    """宠物洗技能：消耗兽诀卷（reagent_kind=beast_skill）重 roll 指定槽技能。

    新技能从种族 skill_candidates 挑与当前不同者（rng 确定性，盐同 refine_pet 口径
    `beast_skill_{pet_id}_{slot}`）。返回 {ok, reason, skill}。
    [!] 亲和门槛 >= 30（PET_SKILL_REROLL_AFFINITY_AT）；槽须已解锁（pet_skill_slots）。
    """
    from src.models.world import effective_category
    from src.services import pet_engine as pex

    pet = _find_pet(world, player, pet_id)
    if pet is None:
        return {"ok": False, "reason": "找不到该宠物", "skill": ""}
    if int(getattr(pet, "affinity", 0) or 0) < PET_SKILL_REROLL_AFFINITY_AT:
        return {"ok": False, "reason": f"亲和不足{PET_SKILL_REROLL_AFFINITY_AT}，无法洗技能",
                "skill": ""}
    pex.fill_pet_skill_slots(pet)  # 老档/升级边界归一化槽位
    slot = max(0, int(slot or 0))
    if slot >= pex.pet_skill_slots(pet.level):
        return {"ok": False, "reason": "该技能槽未解锁", "skill": ""}
    scroll = _find_item_in_inventory(world, player, scroll_item_id)
    if scroll is None or scroll_item_id not in (getattr(player, "inventory", None) or []):
        return {"ok": False, "reason": "没有兽诀卷", "skill": ""}
    if effective_category(scroll) != "cultivate" \
            or getattr(scroll, "reagent_kind", "") != "beast_skill":
        return {"ok": False, "reason": "该物品不是兽诀卷", "skill": ""}
    inv = getattr(player, "inventory", None)
    if isinstance(inv, list) and scroll_item_id in inv:
        inv.remove(scroll_item_id)
    if rng is None:
        rng = SeededRng.seed_from(getattr(world, "id", ""),
                                  int(getattr(world, "tick_count", 0) or 0),
                                  f"beast_skill_{pet_id}_{slot}")
    cands = pex.species_skill_candidates(pex.find_species(pet.species) or {})
    if not cands:
        return {"ok": False, "reason": "该种族无技能候选", "skill": ""}
    skills = list(pet.skills or [])
    cur = skills[slot] if slot < len(skills) else ""
    # 候选排除全部已占技能（防洗出与他槽重复）；全占满时退化为排除当前槽
    alt = [c for c in cands if c not in skills]
    if not alt:
        alt = [c for c in cands if c != cur] or cands
    new = rng.pick(alt)
    if slot < len(skills):
        skills[slot] = new
    else:
        skills.append(new)
    pet.skills = skills
    return {"ok": True, "reason": "洗技能成功", "skill": new}


def _find_pet(world: Any, player: Any, pet_id: str) -> Optional[Any]:
    """从 world.pets 按 id 找宠物（须在 player.pet_ids）。"""
    if pet_id not in (getattr(player, "pet_ids", None) or []):
        return None
    for p in (getattr(world, "pets", None) or []):
        if getattr(p, "id", "") == pet_id:
            return p
    return None


def aptitude_mult(pet: Any, dim: str) -> float:
    """[P34b] 取宠物某维度资质系数（build_pet_unit 聚合用）。缺省 1.0。

    dim 是 _PET_APTITUDE_KEYS 之一；返回资质值（0.8-1.5），build_pet_unit 据此缩放对应属性。
    """
    apt = getattr(pet, "aptitude", None)
    if not isinstance(apt, dict):
        return 1.0
    try:
        return float(apt.get(dim, 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0
