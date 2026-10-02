"""[P7k1] 合成/炼制引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b）：LLM 出静态配方定义（inputs/output 题材包装 + difficulty 种子），
纯 Python 跑动态校验/消耗/产出/成败。运行时零 LLM。

inputs 是 item_id 列表（每种材料消耗 1 个，配合去重背包设计：player.inventory 是
item_id 去重 list，同 id 只占一个 slot）。craft 校验背包含全部 inputs ->
[三骰取二] 定档（每骰成功线由 craft_chance 的概率推出）：
  - 成功/大成功：扣 inputs -> 加 output；大成功产出稀有度升一档（克隆入 world.items）。
  - 失败：随机扣 1 件 input、其余退回；大失败：全扣 inputs、无产出。
"""
from __future__ import annotations

from typing import Any, Optional

from src.utils.rng import SeededRng
from src.utils.dice_check import roll_dice_check, DiceRoll, TIER_CRIT, TIER_OK, TIER_FUMBLE
from src.services.combat_engine import upgrade_item_rarity, _RARITY_TIERS, difficulty_penalty_of
from src.services.combat_engine import try_add_to_inventory
from src.services import talent_engine as te


def _item_name(world, item_id: str) -> str:
    it = next((i for i in getattr(world, "items", []) if i.id == item_id), None)
    return it.name if it is not None else item_id


def missing_inputs(player: Any, recipe: Any, world: Any) -> list:
    """[P49 合成手册 2026-09-25] 缺料全列（can_craft 只报第一个；手册/UI 需要全量）。

    返回 [(材料名, 缺少数), ...]，材料齐全返回 []。
    """
    from collections import Counter
    inv = Counter(str(i) for i in (getattr(player, "inventory", []) or []))
    need = Counter(str(i) for i in (getattr(recipe, "inputs", []) or []))
    return [(_item_name(world, iid), count - inv[iid])
            for iid, count in need.items() if inv[iid] < count]


def home_material_plan(player: Any, recipe: Any, home: Any, world: Any) -> tuple[bool, str, list[str]]:
    """从本地自宅仓库补齐配方料的原子计划；只读预检，供 UI 按钮与执行共用。"""
    from collections import Counter
    from src.services import home_engine as he
    from src.services.combat_engine import _INV_STACK_LIMIT, carry_capacity
    if (home is None or home.id not in (getattr(player, "home_ids", None) or [])
            or home.location_id != getattr(player, "location_id", "")
            or he.stash_capacity(home) <= 0):
        return False, "需在自有住宅仓库取料", []
    inv = list(getattr(player, "inventory", None) or [])
    have = Counter(str(i) for i in inv)
    required = Counter(str(i) for i in (getattr(recipe, "inputs", None) or []))
    missing = {iid: count - have[iid] for iid, count in required.items()
               if count > have[iid]}
    if not missing:
        return False, "背包材料已齐", []
    stash = Counter(str(i) for i in (getattr(home, "stash", None) or []))
    for iid, count in missing.items():
        if stash[iid] < count:
            return False, f"住宅仓库缺少 {_item_name(world, iid)} x{count - stash[iid]}", []
        if have[iid] + count > _INV_STACK_LIMIT:
            return False, f"{_item_name(world, iid)}超过背包同种堆叠上限", []
    picks: list[str] = []
    for raw in (getattr(recipe, "inputs", None) or []):
        iid = str(raw)
        if missing.get(iid, 0) > 0:
            picks.append(iid)
            missing[iid] -= 1
    if len(inv) + len(picks) > carry_capacity(player):
        return False, "背包空间不足，无法从仓库取齐材料", []
    return True, "", picks


def prepare_home_materials(player: Any, recipe: Any, home: Any, world: Any) -> tuple[bool, str, int]:
    """自宅仓库 -> 背包一次取齐配方料；任一入包失败则整批退回。"""
    ok, reason, picks = home_material_plan(player, recipe, home, world)
    if not ok:
        return False, reason, 0
    moved: list[str] = []
    for iid in picks:
        home.stash.remove(iid)
        if not try_add_to_inventory(player, iid):
            home.stash.extend(moved + [iid])
            for old in moved:
                player.inventory.remove(old)
            return False, "背包无法容纳全部材料（已退回仓库）", 0
        moved.append(iid)
    return True, f"已从住宅仓库取出 {len(moved)} 件配方材料", len(moved)


def can_craft(player: Any, recipe: Any, world: Any, preset: Any = None) -> tuple:
    """校验玩家背包是否含配方全部 inputs + [P34c] 建筑门控。返回 (ok, missing_reason)。

    [!] 同时预检全部产物的负重与堆叠上限：合成会先扣材料再给产出，
    事后拒收产出等于白扣材料——须在动手前拦下（守统一入包口径）。
    [P34c] 建筑门控：若 recipe.required_building 非空，校验玩家当前聚落有宅且宅内有该 kind
    建筑 level >= recipe.min_building_level。buildings_enabled=False 或 preset 未传时跳过（兼容）。
    """
    inv = list(getattr(player, "inventory", []) or [])
    inputs = getattr(recipe, "inputs", []) or []
    missing = missing_inputs(player, recipe, world)
    if missing:
        name, count = missing[0]
        return False, f"缺少材料：{name}" + (f" x{count}" if count > 1 else "")
    out_id = str(getattr(recipe, "output_item_id", "") or "")
    if not out_id:
        return False, "配方没有产出物"
    out_qty = max(1, int(getattr(recipe, "output_qty", 1) or 1))
    # 非装备产物直接按同 id 堆叠；装备模板会为每件派生独立 id。
    if out_id and world is not None:
        out_item = next((i for i in getattr(world, "items", []) if getattr(i, "id", "") == out_id), None)
        if out_item is not None:
            will_clone = (getattr(out_item, "type", "") in ("weapon", "armor", "accessory")
                          and getattr(out_item, "identified", True) is not False)
            if not will_clone:
                from src.services.combat_engine import _INV_STACK_LIMIT
                if inv.count(out_id) - sum(str(i) == out_id for i in inputs) + out_qty > _INV_STACK_LIMIT:
                    return False, (f"「{_item_name(world, out_id)}」合成后会超过同种 "
                                   f"{_INV_STACK_LIMIT} 件堆叠上限")
    if out_id:
        from src.services.combat_engine import carry_capacity
        # 同种堆叠每件占一格；配方可能多次消耗同一 id，须按实际件数预检。
        if len(inv) - len(inputs) + out_qty > carry_capacity(player):
            return False, "背包已满，无法容纳产出"
    # [P34c] 建筑门控
    req_bld = getattr(recipe, "required_building", "") or ""
    if req_bld:
        bld_enabled = bool(getattr(preset, "buildings_enabled", True)) if preset is not None else True
        if bld_enabled and world is not None:
            from src.services import home_engine as hm
            loc_id = getattr(player, "location_id", "") or ""
            loc = next((l for l in getattr(world, "locations", []) if getattr(l, "id", "") == loc_id), None)
            home = hm.player_home_at(world, loc)
            if home is None:
                min_lvl = max(1, int(getattr(recipe, "min_building_level", 0) or 0))
                return False, f"需在住宅建造 {min_lvl} 级{hm.building_kind_name(world, req_bld)}"
            cur_lvl = hm.building_level(home, req_bld)
            min_lvl = max(1, int(getattr(recipe, "min_building_level", 0) or 0))
            if cur_lvl < min_lvl:
                return False, f"需 {hm.building_kind_name(world, req_bld)} 建筑 {min_lvl} 级（当前 {cur_lvl} 级）"
    return True, ""


# ---- [P53 锻造修行 2026-09-25] 合成熟练成长线（匠徒 -> 匠手 -> 匠师 -> 巨匠）----
# 经验公式（纯计数零 rng）：成功 +1+difficulty//15；crit 再 +2；fail/fumble +1（学费）。
# 阈值 [0,6,18,40] -> L0-L3；每级成功率 +2%（craft_chance 单一来源，总钳 0.95 不变）。
_CRAFT_EXP_THRESHOLDS = (0, 6, 18, 40)
_GENRE_CRAFT_TITLES: dict = {
    "western_fantasy": ["匠徒", "匠手", "匠师", "巨匠"],
    "xianxia": ["炼器学徒", "炼器师", "炼器大师", "炼器宗师"],
    "wuxia": ["学徒", "巧匠", "老师傅", "一代宗匠"],
    "modern": ["学徒", "技工", "工程师", "首席技师"],
    "scifi": ["实习技师", "装配技师", "系统工程师", "总工程师"],
    "apocalypse": ["修补匠", "改装工", "废土技师", "传奇工匠"],
}


def craft_prof_level(player: Any) -> int:
    exp = int(getattr(player, "craft_exp", 0) or 0)
    lvl = 0
    for i, t in enumerate(_CRAFT_EXP_THRESHOLDS):
        if exp >= t:
            lvl = i
    return lvl


def craft_prof_progress(player: Any) -> tuple[int, int, int]:
    """锻造修行进度（[P60 熟练线 UI 2026-09-26]）：(当前阶 0-3, 已有 exp, 下一阶还差 exp)。

    满阶（3）返回 (3, exp, 0)。UI 行与 tooltip 消费，引擎零新状态。"""
    exp = int(getattr(player, "craft_exp", 0) or 0)
    lvl = craft_prof_level(player)
    if lvl >= len(_CRAFT_EXP_THRESHOLDS) - 1:
        return len(_CRAFT_EXP_THRESHOLDS) - 1, exp, 0
    return lvl, exp, max(0, _CRAFT_EXP_THRESHOLDS[lvl + 1] - exp)


SCRAP_RECYCLE_COUNT = 3


def scrap_count(player: Any) -> int:
    """背包里的残料件数（id 以 scrap_ 前缀；[P49] 合成大失败产物，此前零消费）。"""
    return sum(1 for i in (getattr(player, "inventory", None) or [])
               if str(i).startswith("scrap_"))


def recycle_scrap(world: Any, player: Any) -> tuple[bool, str]:
    """[P60 残料联动 2026-09-26] 器物台回炉：3 件残料 -> 1 件世界已有的 common 材料。

    大失败残料（_spawn_scrap）此前纯占背包死物——回炉给它们再利用出口（重铸回
    基础材料再投入合成，形成「失败也留价值」的闭环）。确定性零 rng：产出取
    world.items 中按名排序的首件 common 材料。入包走 try_add_to_inventory 单一
    来源；满包拒收时残料返还（物品守恒）。"""
    scrap_ids = [str(i) for i in (getattr(player, "inventory", None) or [])
                 if str(i).startswith("scrap_")]
    if len(scrap_ids) < SCRAP_RECYCLE_COUNT:
        return False, (f"残料不足（{len(scrap_ids)}/{SCRAP_RECYCLE_COUNT}）"
                       "——合成失败会留下残料")
    mats = [i for i in (getattr(world, "items", None) or [])
            if str(getattr(i, "type", "")) == "material"
            and str(getattr(i, "rarity", "")) == "common"]
    if not mats:
        return False, "这个世界没有可回炉出的基础材料"
    out = sorted(mats, key=lambda i: str(getattr(i, "name", "")))[0]
    for iid in scrap_ids[:SCRAP_RECYCLE_COUNT]:
        player.inventory.remove(iid)
    if not try_add_to_inventory(player, out.id):
        for iid in scrap_ids[:SCRAP_RECYCLE_COUNT]:
            player.inventory.append(iid)
        return False, "背包已满，装不下回炉出的材料（残料已返还）"
    return True, f"回炉成功——{SCRAP_RECYCLE_COUNT} 件残料重铸成「{getattr(out, 'name', '材料')}」"


def craft_prof_title(player: Any, world: Any = None) -> str:
    tid = "western_fantasy"
    if world is not None:
        ov = getattr(world, "config_overlay", None) or {}
        tid = str(ov.get("attribute_template_id", "") or "western_fantasy")
    pool = _GENRE_CRAFT_TITLES.get(tid) or _GENRE_CRAFT_TITLES["western_fantasy"]
    return pool[max(0, min(3, craft_prof_level(player)))]


def craft_prof_bonus(player: Any) -> float:
    return 0.02 * craft_prof_level(player)


def craft_chance(player: Any, recipe: Any, world: Any = None, preset: Any = None) -> float:
    """[P8] 算合成成功率（供 UI 显示，不 roll）。

    craft 专用公式（比通用 success_chance 温和，难度系数 0.01 而非 0.02，base 0.85）：
      clamp(0.85 + stat_int*0.01 - difficulty*0.01, 0.05, 0.95)
    参考量：common(diff 0)/int10=95% · rare(diff 20)/int15=80% · legendary(diff 50)/int25=60%。
    stat_int 是核心驱动（高智力炼制更稳）；大成功（三骰取二 crit 档）产出升档不改变成功概率。
    [P34c] 建筑等级加成：建筑 level 高于配方要求时每高出 1 级 +3%（钳 0.95）。
    """
    difficulty = max(0, min(100, int(getattr(recipe, "difficulty", 0) or 0)))
    # [P 验收] 全局难度判定偏移：困难下锻造更难成功（world 传入时生效）
    if world is not None:
        difficulty += difficulty_penalty_of(world)
    # [饱食度 2026-09-06] 饥饿减半智力驱动（与战斗/采集同口径；无 hunger 字段恒 1.0）
    from src.services.combat_engine import hunger_stat_mult as _hsm
    stat_int = int(te.effective_stat(player, "int") * _hsm(player))
    chance = 0.85 + max(0, stat_int) * 0.01 - difficulty * 0.01
    # [P9] 天赋 craft_mult（丹道宗师/黑客天赋等合成加成，钳制 0.95 上限）
    chance = min(0.95, chance * te.get_talent_mult(player, "craft_mult"))
    # [P34c] 建筑等级加成
    req_bld = getattr(recipe, "required_building", "") or ""
    if req_bld and world is not None:
        bld_enabled = bool(getattr(preset, "buildings_enabled", True)) if preset is not None else True
        if bld_enabled:
            from src.services import home_engine as hm
            loc_id = getattr(player, "location_id", "") or ""
            loc = next((l for l in getattr(world, "locations", []) if getattr(l, "id", "") == loc_id), None)
            home = hm.player_home_at(world, loc)
            if home is not None:
                cur_lvl = hm.building_level(home, req_bld)
                min_lvl = max(1, int(getattr(recipe, "min_building_level", 0) or 0))
                bonus = max(0, cur_lvl - min_lvl) * 0.03
                chance = min(0.95, chance + bonus)
    # [P53 锻造修行] 熟练度加成（每级 +2%，最高 +6%——亲手炼的唯一成长线）
    chance = min(0.95, chance + craft_prof_bonus(player))
    return max(0.05, min(0.95, chance))


def _spawn_scrap(world: Any, recipe: Any) -> Any:
    """[P54] 产 1 件本题材残料（确定性 id 幂等入 world.items；forge 类配方出锻造残料）。

    type=material + category=forge + rarity=common：天然被 npc_life 的「配方料」扫描
    视为有用料留着（残料进入经济循环），但绝不出现在 NPC craft 产出里。
    """
    import hashlib
    ov = getattr(world, "config_overlay", None) or {}
    tid = str(ov.get("attribute_template_id", "") or "western_fantasy")
    is_forge = str(getattr(recipe, "required_building", "") or "") == "forge"
    names = {"forge": [f"{n}残料" for n in ("铁屑", "碎甲片")],
             "generic": [f"{n}" for n in ("药渣", "碎布角", "报废件", "炉灰结块", "碎木屑", "焦炭渣")]}
    name = (names["forge"][0] if is_forge else names["generic"][0])
    iid = f"scrap_{hashlib.md5(f'{tid}|{name}'.encode('utf-8')).hexdigest()[:12]}"
    pool = getattr(world, "items", None) or []
    it = next((i for i in pool if getattr(i, "id", "") == iid), None)
    if it is not None:
        return it
    from src.models.world import Item as _Item
    it = _Item(id=iid, name=name, rarity="common", type="material",
               category="forge", desc="炼制失败留下的残料，或可当低阶材料用。")
    it.base_price = 2
    pool.append(it)
    return it


def craft(player: Any, recipe: Any, world: Any, rng: Optional[SeededRng] = None,
          preset: Any = None, dice: Optional[DiceRoll] = None) -> dict:
    """合成：校验 -> [三骰取二] 定档 -> 扣 inputs -> 加 output。返回 summary。

    dice 为 None 时走 SeededRng 派生三枚（NPC/批量/旧调用方；rng=None 仍必成兜底）；
    传入 dice（UI 已掷好并演出）则直接用其档位，不再重掷。
    preset 透传给 craft_chance（[P34c] 建筑加成 + buildings_enabled 开关；[P 验收]
    全局难度偏移经 world 生效——实际概率须与 UI 显示同口径）。
    定档：crit/ok 全扣 inputs + 产出（crit 产出稀有度升一档）；fumble 全扣 inputs；
    fail 随机扣 1 件 input、其余退回。
    返回 {success, output_item_id, output_qty, output_name, reason, chance, crit, tier}。
    """
    ok, reason = can_craft(player, recipe, world, preset)
    if not ok:
        return {"success": False, "reason": reason, "chance": craft_chance(player, recipe, world, preset)}
    inputs = list(getattr(recipe, "inputs", []) or [])
    inv = player.inventory
    chance = craft_chance(player, recipe, world, preset)
    crit = False

    if dice is not None:
        tier = dice.tier
    elif rng is None:
        tier = TIER_OK  # 必成兜底（老调用方/无 rng 场景）
    else:
        tier = roll_dice_check(chance, rolls=[rng.random() for _ in range(3)]).tier

    out_id = str(getattr(recipe, "output_item_id", "") or "")
    out_qty = max(1, int(getattr(recipe, "output_qty", 1) or 1))
    # [P53 锻造修行] 熟练经验（玩家专属：NPC 无 craft_exp 字段不吃线）。
    # 成功 +1+difficulty//15、crit 再 +2；fail/fumble +1（学费不白交）。
    # 晋阶产 minor 事件进 event_log（坊间热议/动态栏免费消费）。
    _exp_before = int(getattr(player, "craft_exp", 0) or 0) if hasattr(player, "craft_exp") else 0
    _prof_before = craft_prof_level(player) if hasattr(player, "craft_exp") else -1
    if hasattr(player, "craft_exp") and world is not None:
        _gain = 1 + max(0, int(getattr(recipe, "difficulty", 0) or 0)) // 15
        if tier == TIER_CRIT:
            _gain += 2
        try:
            player.craft_exp = int(getattr(player, "craft_exp", 0) or 0) + _gain
        except (AttributeError, TypeError):
            pass

    if tier in (TIER_CRIT, TIER_OK):
        # 成功：消耗全部 inputs（记录实扣清单，供入包被拒时兜底退还）
        removed: list[str] = []
        for iid in inputs:
            iid = str(iid)
            if iid in inv:
                inv.remove(iid)
                removed.append(iid)
        # 大成功（crit）：产出稀有度升一档
        upgraded_added = None
        if tier == TIER_CRIT and out_id and world is not None:
            it = next((i for i in getattr(world, "items", []) if i.id == out_id), None)
            if it is not None:
                cur = getattr(it, "rarity", "common") or "common"
                idx = _RARITY_TIERS.index(cur) if cur in _RARITY_TIERS else 0
                if idx < len(_RARITY_TIERS) - 1:
                    # [P8 题材化] 读世界题材品级名做后缀（防跨题材命名串味）
                    _ov = getattr(world, "config_overlay", None)
                    _rn = _ov.get("rarity_display_names") if isinstance(_ov, dict) else None
                    upgraded = upgrade_item_rarity(it, _RARITY_TIERS[idx + 1], rarity_names=_rn)
                    if upgraded is not None:
                        # [!] 派生 id 幂等：克隆已在世界池不重复 append
                        _pool = getattr(world, "items", [])
                        if not any(getattr(x, "id", "") == upgraded.id for x in _pool):
                            _pool.append(upgraded)
                            upgraded_added = upgraded
                        out_id = upgraded.id
                        crit = True
        # 配方的 output_qty 是实际一炉产量。装备每件派生独立 id；普通物品
        # 按 id 堆叠。任一件意外拒收入包时整炉回滚，避免扣料后只给部分产物。
        added_ids: list[str] = []
        clones: list = []
        if out_id:
            it = next((i for i in getattr(world, "items", []) if i.id == out_id), None) \
                if world is not None else None
            clone_equip = (it is not None and getattr(it, "type", "")
                           in ("weapon", "armor", "accessory")
                           and getattr(it, "identified", True) is not False)
            for _ in range(out_qty):
                piece_id = out_id
                if clone_equip:
                    import copy as _copy
                    _tick = int(getattr(world, "tick_count", 0) or 0)
                    _pool = getattr(world, "items", [])
                    base_craft_id = f"{out_id}__craft{_tick}"
                    piece_id = base_craft_id
                    _n = 1
                    while any(getattr(x, "id", "") == piece_id for x in _pool) or piece_id in inv:
                        _n += 1
                        piece_id = f"{base_craft_id}x{_n}"
                    clone = _copy.copy(it)
                    clone.id = piece_id
                    clone.identified = False
                    out_lvl = max(0, int(getattr(recipe, "output_level", 0) or 0))
                    if out_lvl > 0:
                        clone.level = out_lvl
                    _pool.append(clone)
                    clones.append(clone)
                if not try_add_to_inventory(player, piece_id):
                    for added in added_ids:
                        inv.remove(added)
                    inv.extend(removed)
                    if hasattr(player, "craft_exp"):
                        player.craft_exp = _exp_before
                    if world is not None:
                        for clone in clones:
                            world.items.remove(clone)
                        if upgraded_added is not None:
                            world.items.remove(upgraded_added)
                    return {"success": False,
                            "reason": (f"「{_item_name(world, out_id)}」已达堆叠上限或背包已满，"
                                       f"无法容纳全部产出（材料已返还）"),
                            "chance": chance, "crit": False, "tier": tier}
                added_ids.append(piece_id)
        # [P7k6] 图鉴：产出物品入曾拥有
        try:
            codex = getattr(player, "codex_items", None)
            if isinstance(codex, list):
                for iid in added_ids:
                    if iid not in codex:
                        codex.append(iid)
        except Exception:
            pass
        # [P53] 晋阶检查：跨过阈值产 minor 事件（坊间热议/动态栏免费消费）
        if world is not None:
            _after = craft_prof_level(player)
            if _after > _prof_before >= 0:
                _tid = str((getattr(world, "config_overlay", None) or {})
                           .get("attribute_template_id", "") or "western_fantasy")
                _titles = _GENRE_CRAFT_TITLES.get(_tid) or _GENRE_CRAFT_TITLES["western_fantasy"]
                try:
                    from src.models.world import WorldEvent as _WE
                    world.event_log.append(_WE(
                        tick=int(getattr(world, "tick_count", 0) or 0),
                        category="npc", severity="minor",
                        title="技艺精进",
                        desc=f"玩家在反复锤炼中晋为「{_titles[_after]}」（合成成功率 +{_after * 2}%）",
                        locations=[str(getattr(world.player, "location_id", "") or "")]))
                except Exception:
                    pass
        return {
            "success": True,
            "output_item_id": added_ids[0] if added_ids else "",
            "output_item_ids": added_ids,
            "output_qty": len(added_ids),
            "output_name": _item_name(world, out_id) if out_id else "",
            "reason": "",
            "chance": chance,
            "crit": crit,
            "tier": tier,
        }
    elif tier == TIER_FUMBLE:
        # 大失败：损失全部 inputs
        for iid in inputs:
            iid = str(iid)
            if iid in inv:
                inv.remove(iid)
        return {
            "success": False,
            "reason": "炼制大失败，材料尽毁",
            "chance": chance,
            "crit": False,
            "tier": tier,
            "materials_all_lost": True,
        }
    else:
        # 失败：随机扣 1 件 input，其余退回
        present = [str(iid) for iid in inputs if str(iid) in inv]
        if present:
            # [审核修复 2026-09-13] 原用全局 random.choice——同世界同配方同骰面会出两种
            # 丢料结果，破坏「同 world+tick 可回放」铁律（§21）。
            # 改用**独立 salt 派生**的 rng（不消费传入 rng）：守「不漂移既有序列」口径——
            # 传入 rng 是调用方（NPC 日计划等）的共享序列，在这里多吃一格会让它后续
            # 掷骰整体位移。salt 含 world/tick/制作者/配方，同输入同结果可回放。
            _r = None
            if world is not None:
                _salt = (f"craft_fail::{getattr(player, 'id', '') or 'player'}"
                         f"::{out_id}")
                _r = SeededRng.seed_from(str(getattr(world, "id", "") or ""),
                                         int(getattr(world, "tick_count", 0) or 0),
                                         _salt)
            inv.remove(_r.pick(present) if _r is not None else present[0])
        # [P54 残料 2026-09-25] 炼崩了也留点废料（无 roll 必然产出——避免「同 tick 连点
        # roll 锁死」新问题；入包满拒则不产，守恒不吞不爆）。失败有回响，学费看得见。
        _scrap = _spawn_scrap(world, recipe)
        if _scrap is not None and try_add_to_inventory(player, _scrap.id):
            return {"success": False, "scrap": _scrap.name,
                    "reason": "炼制失败，损失了部分材料，但炉前留下了些残料",
                    "chance": chance, "crit": False, "tier": tier,
                    "materials_partial_lost": True}
        return {
            "success": False,
            "reason": "炼制失败，损失了部分材料",
            "chance": chance,
            "crit": False,
            "tier": tier,
            "materials_partial_lost": True,
        }
