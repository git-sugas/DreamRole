"""[P36a] NPC 生命模拟引擎（纯 Python + SeededRng；LLM 只出当日计划，执行零 LLM）。

数值范式：采集/合成/上架复用既有引擎（gather_engine / crafting_engine / trade_engine
compute_item_price / combat_engine gain_xp+roll_loot，NPC 与 PlayerState 鸭子类型同构）；
打怪是轻量确定性模拟（战力比 -> 胜率 roll，胜得经验掉落、败掉血，重伤回城疗养 + 概率遗失
背包物品，阵亡走 alive=False + respawn 既有契约）。全部行为产 WorldEvent(category="npc")
供右侧「NPC 动态」日志栏消费。

动作白名单：gather 采集 / craft 合成（无建筑门控的 common~rare 基础配方，蓝封顶；
[P37] 配方挑选走 pick_craft_recipe 需求驱动优先级，LLM 计划可带 target 点名想造之物）/
stock_shop 上架自家商店 / buy_materials 钱包批发买料扩产（[P37] 经济闭环再投资）/
hunt 打怪 / rest 疗伤。建设类（建造/种植等）明确不做（用户定稿）。

[P45 2026-09-12 NPC 成长体系改造]（用户定稿 v3）：
1. 删被动升级（原 world_tick 每 tick 8% 抬级）——NPC 要涨经验必须真打怪；
2. 打猎与玩家同口径：猎物用 wilderness_engine.roll_wilderness_monster 生成（真实
   loot_table），经验 = 等级 x15 x 难度系数，掉落 roll_loot(drop_rate=0.4) + upgrade_drops。
   **不打猎金币**——NPC 靠卖东西赚钱（打猎金币曾实测 300 天让战斗 NPC 钱包 +12 万、
   拍卖会被包圆——multi-agent 交叉审核结论，已按用户定稿移除）；
3. 每日被动遭遇（daily_passive_encounters）：战斗身份 NPC 各自按所在地/最近野外 danger
   roll 一次遇怪（复用 encounter_chance），命中就开打——不再依赖被抽进 6 人 roster。
打猎胜率计入 NPC 真实装备攻防（combat_engine.equipped_attack_defense），**不稀释等级差**
（怪物系数保持 12，danger 梯度保留；打不过用装备顶）。背包 cap 8 -> 16，且背包 >8 件时
材料出清给最近商店（卖价五成当场进 wallet，货以 tag="npc_made" 上架，每店上限 6 件，
满闸只给钱、物品吞掉）。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from src.utils.rng import SeededRng

# 商店可上架口径（用户定稿：蓝封顶，紫色 epic+ 绝不出店，守 P34a）
_BASIC_RARITIES = ("common", "uncommon", "rare")
# [P45 2026-09-12] 背包 8 -> 16（用户定稿）：容纳打猎掉落/合成材料；>8 件触发材料出清
_NPC_INV_CAP = 16
# [P45 2026-09-12] 经验与玩家同口径（玩家 = 目标等级 x15 x 难度系数），原 12 已废
_HUNT_XP_PER_LVL = 15
_RETURN_HP_RATIO = 0.30     # HP 低于此比例 -> 回城疗养
_LOSE_ITEM_CHANCE = 0.35    # 战败且重伤时遗失一件背包物品的概率
_ACTION_VALUES = ("gather", "craft", "stock_shop", "buy_materials", "hunt", "rest")
# [P37] buy_materials 经济参数：批发折扣（相对 compute_item_price，商人进货价低于零售）/
# 启动门槛（钱包低于此数不进货，攒到再投资）/ 每日进货上限（防一夜清空钱包）
_WHOLESALE_MULT = 0.6
_BUY_WALLET_MIN = 50
_BUY_PER_DAY = 4
# shop_type -> 对口产出物品 type（craft 优先级 2 用；general/material 等杂货业态不限）
_SHOP_TYPE_CRAFT = {"weapon": {"weapon"}, "armor": {"armor"}, "alchemy": {"consumable"}}

# ---- [P45 2026-09-12 v3] 打猎数值（经验/掉落与玩家 combat 结算同参数；金币不出）----
_HUNT_DROP_RATE = 0.4                   # roll_loot 的 drop_rate（玩家同款；条目自带 rate 优先）
_HUNT_MON_COEF = 12                     # 怪物等级战力系数（=旧口径原值，**不稀释**等级差）
_HUNT_GEAR_ATK_W = 0.8                  # 装备武器攻 -> 战力权重
_HUNT_GEAR_DEF_W = 0.4                  # 装备防具防 -> 战力权重
# ---- [P45 2026-09-12] 每日被动遭遇（A2）----
_PASSIVE_ENC_BASE = 0.25                # 基础遇怪率回退值（服务层传世界旋钮 wilderness_monster_chance）
_PASSIVE_FIGHTS_CAP = 6                 # 每日被动遭遇最多 6 场（防事件洪水/性能爆炸）
_PASSIVE_HP_MIN = 0.5                   # 半血以上才出门打猎（重伤者优先 rest/买药）
# ---- [P45 2026-09-12 v3] 材料出清（背包 >8 件 -> 卖最近商店；打猎无金币后这是主收入）----
_SURPLUS_THRESHOLD = 8                  # 超过此件数触发出清（用户定稿 v3：大于 8 件就考虑卖）
_SURPLUS_PER_DAY = 3                    # 单 NPC 单日最多卖 3 件（防钱包/货架单日暴增）
_NPC_MADE_PER_SHOP = 6                  # 每店 npc_made 条目上限（用户定稿）


def _loc_of(world: Any, npc: Any):
    lid = str(getattr(npc, "location_id", "") or "")
    return next((l for l in (getattr(world, "locations", None) or []) if getattr(l, "id", "") == lid), None)


def _mk_event(world: Any, npc: Any, title: str, desc: str, severity: str = "trivial",
              *, cause: str = "", outcome: str = "", shop_id: str = "",
              item_ids: Optional[list[str]] = None):
    """[!] 返回 WorldEvent 实体（tick 消费链 e.severity/e.to_dict()，裸 dict 必崩）。"""
    from src.models.world import WorldEvent
    return WorldEvent(tick=int(getattr(world, "tick_count", 0) or 0), category="npc",
                      severity=severity, title=title, desc=desc,
                      npcs=[str(getattr(npc, "id", "") or "")],
                      locations=[str(getattr(npc, "location_id", "") or "")],
                      cause=cause, outcome=outcome, shop_id=shop_id,
                      item_ids=list(item_ids or []))


def _inv_add(npc: Any, item_id: str) -> bool:
    """NPC 入包（同种堆叠上限 + 格数上限），与 ce.try_add_to_inventory 同口径。

    [堆叠 2026-09-13] 原为「同种最多 1 件」的去重写法，NPC 采药/打猎/互市攒不下材料；
    现改为同种最多 ce._INV_STACK_LIMIT 件，_NPC_INV_CAP 的总格数约束不变。
    [!] 只管「新获得」；互赠/付货款/遗失物归还等守恒转移直接 append 不走这里。
    """
    from src.services import combat_engine as ce
    inv = getattr(npc, "inventory", None)
    if inv is None or not item_id:
        return False
    if inv.count(item_id) >= ce._INV_STACK_LIMIT:
        return False
    if len(inv) >= _NPC_INV_CAP:
        return False
    inv.append(item_id)
    return True


def _item(world: Any, iid: str):
    return next((i for i in (getattr(world, "items", None) or []) if getattr(i, "id", "") == iid), None)


def _role_weight(npc: Any) -> str:
    """职能归类（自动加点权重用）：combat / craft / balanced。"""
    if getattr(npc, "is_merchant", False):
        return "craft"
    role = str(getattr(npc, "role", "") or "") + str(getattr(npc, "shop_type", "") or "")
    if getattr(npc, "hostile", False) or getattr(npc, "combat_role", "") in ("boss", "hostile", "friendly"):
        return "combat"
    if any(k in role for k in ("商", "掌柜", "匠", "药", "general", "weapon", "armor", "alchemy", "material")):
        return "craft"
    return "balanced"


def is_combat_identity(npc: Any) -> bool:
    """[P45 2026-09-12] 战斗身份判定（每日被动遭遇/打猎门槛的唯一来源）：

    与 P36a 现有 hunt 条件/初始配装（_equip_initial_gear）同口径——hostile 或
    combat_role in (boss/hostile/friendly) 才算战斗单位；平民（combat_role 空/none）
    不外出打猎（[P44] role 关键词推算路已废弃，不得在此复活）。"""
    return _role_weight(npc) == "combat"


def auto_allocate(npc: Any) -> int:
    """升级自动加点（职能加权；NPC 不囤点，返回消耗点数）。"""
    pts = int(getattr(npc, "stat_points", 0) or 0)
    if pts <= 0:
        return 0
    w = _role_weight(npc)
    plan = {"combat": ["stat_str", "stat_vit", "stat_str", "stat_dex", "stat_luk"],
            "craft": ["stat_int", "stat_luk", "stat_dex", "stat_int", "stat_vit"],
            "balanced": ["stat_str", "stat_vit", "stat_int", "stat_dex", "stat_luk"]}[w]
    for i in range(pts):
        setattr(npc, plan[i % len(plan)], int(getattr(npc, plan[i % len(plan)], 5) or 5) + 1)
    npc.stat_points = 0
    return pts


def _on_gain_xp(world: Any, npc: Any, amount: int) -> bool:
    """NPC 加经验 + 升级收尾（自动加点 + HP 上限重算回满）。返回是否升级。"""
    from src.services import combat_engine as ce
    r = ce.gain_xp(npc, max(0, int(amount)))
    if r.leveled_up:
        auto_allocate(npc)
        npc.hp_max = ce.max_hp_for(npc)
        npc.hp = npc.hp_max
    return bool(r.leveled_up)


# ---------- 动作执行（每日每 NPC 一个动作；返回事件或 None） ----------

def do_gather(world: Any, npc: Any, rng: SeededRng) -> Optional[Any]:
    loc = _loc_of(world, npc)
    nodes = [n for n in (getattr(loc, "resource_nodes", None) or [])
             if int(getattr(n, "richness", 0) or 0) > 0
             and int(getattr(n, "cooldown_tick", 0) or 0) <= int(getattr(world, "tick_count", 0) or 0)]
    if loc is None or not nodes:
        return None
    node = rng.pick(nodes)
    stat_key = str(getattr(node, "stat_used", "dex") or "dex")
    from src.services import gather_engine as ge
    res = ge.gather(node, rng, stat_value=int(getattr(npc, f"stat_{stat_key}", 5) or 0),
                    tool_bonus=0, current_tick=int(getattr(world, "tick_count", 0) or 0),
                    difficulty=int(getattr(loc, "danger", 1) or 0) * 5)
    got = []
    for d in (res.get("drops") or []):
        for _ in range(int(d.get("qty", 1) or 1)):
            if _inv_add(npc, str(d.get("item_id", "") or "")):
                got.append(str(d.get("item_id", "") or ""))
    if not got:
        return None
    names = "、".join((_item(world, g).name if _item(world, g) else g) for g in got[:3])
    return _mk_event(world, npc, f"{npc.name}采集", f"{npc.name}在{loc.name}采得{names}。",
                     severity="trivial",
                     cause=f"前往{loc.name}的「{node.name}」采集资源。",
                     outcome=f"{names}已进入随身背包。", item_ids=list(dict.fromkeys(got)))


def _basic_recipes(world: Any, npc: Any) -> list:
    inv = Counter(getattr(npc, "inventory", None) or [])
    out = []
    for r in (getattr(world, "recipes", None) or []):
        if getattr(r, "required_building", ""):
            continue                                   # NPC 无宅，建筑门控配方不参与
        out_item = _item(world, str(getattr(r, "output_item_id", "") or ""))
        if out_item is None or out_item.rarity not in _BASIC_RARITIES:
            continue                                   # 基础配方蓝封顶（用户定稿）
        if not (Counter(r.inputs or []) - inv):
            out.append(r)
    return out


def _needed_material_ids(world: Any, npc: Any) -> set[str]:
    """基础配方还缺哪些材料，按背包中的实际件数计算。"""
    inv = Counter(getattr(npc, "inventory", None) or [])
    needed = set()
    for recipe in (getattr(world, "recipes", None) or []):
        if getattr(recipe, "required_building", ""):
            continue
        output = _item(world, str(getattr(recipe, "output_item_id", "") or ""))
        if output is None or output.rarity not in _BASIC_RARITIES:
            continue
        for iid, qty in Counter(getattr(recipe, "inputs", None) or []).items():
            if inv[iid] < qty and _item(world, iid) is not None:
                needed.add(iid)
    return needed


def _buyable_materials(world: Any, npc: Any, needed: set[str]) -> list:
    from src.services import combat_engine as ce
    inv = Counter(getattr(npc, "inventory", None) or [])
    return [it for it in (getattr(world, "items", None) or [])
            if getattr(it, "type", "") == "material"
            and getattr(it, "rarity", "") in ("common", "uncommon")
            and (inv[it.id] == 0 or (it.id in needed and inv[it.id] < ce._INV_STACK_LIMIT))]


def _can_buy_materials(world: Any, npc: Any) -> bool:
    from src.services import trade_engine as tre
    wallet = max(0, int(getattr(npc, "wallet", 0) or 0))
    if wallet < _BUY_WALLET_MIN or len(getattr(npc, "inventory", None) or []) >= _NPC_INV_CAP:
        return False
    needed = _needed_material_ids(world, npc)
    mats = _buyable_materials(world, npc, needed)
    if _own_shop(world, npc) is None and not getattr(npc, "is_merchant", False):
        mats = [it for it in mats if it.id in needed]
    return any(max(1, int(tre.compute_item_price(it) * _WHOLESALE_MULT)) <= wallet
               for it in mats)


def _own_shop(world: Any, npc: Any):
    """自家商店（双向解析：npc.shop_id 正向挂载优先，或 shop.merchant_npc_id 反向挂载——
    P34e 占位店补挂商人后的形态；方向与优先级同服务 _shop_merchant 的镜像）。"""
    shop_id = str(getattr(npc, "shop_id", "") or "")
    shop = next((s for s in (getattr(world, "shops", None) or [])
                 if getattr(s, "id", "") == shop_id), None)
    if shop is None:
        shop = next((s for s in (getattr(world, "shops", None) or [])
                     if getattr(s, "merchant_npc_id", "") == getattr(npc, "id", "")), None)
    return shop


def pick_craft_recipe(world: Any, npc: Any, recipes: list, target: str = "",
                      rng: Optional[SeededRng] = None):
    """[P37] 确定性需求驱动选配方（替代候选池随机挑；纯函数可单测，零 LLM）。

    优先级（高到低，同层多候选按配方名稳定排序取首——同 world+npc+货架 同结果）：
    1. target 命中——LLM 日计划的「今日想造」（配方名或产出物品名精确/包含匹配）；
    2. 自家货架缺的品类——产出 type 不在 shop.stock 现货品类中的配方（补缺货）；
    3. shop_type 匹配——商人业态对口（weapon 店造武器，杂货业态不限）；
    4. 材料最充足——背包 multiset 可支撑份数最多的配方；
    5. 随机兜底（rng 传入时）。
    """
    if not recipes:
        return None
    ordered = sorted(recipes, key=lambda r: str(getattr(r, "name", "") or ""))
    inv = list(getattr(npc, "inventory", None) or [])
    item_by_id = {getattr(i, "id", ""): i for i in (getattr(world, "items", None) or [])}

    def _out_item(r):
        return item_by_id.get(str(getattr(r, "output_item_id", "") or ""))

    # 1) target 命中（精确名 -> 短名在长名中的包含匹配）
    tgt = str(target or "").strip()
    if tgt:
        exact = [r for r in ordered
                 if str(getattr(r, "name", "") or "") == tgt
                 or str(getattr(_out_item(r), "name", "") or "") == tgt]
        if exact:
            return exact[0]
        fuzzy = [r for r in ordered
                 if tgt in str(getattr(r, "name", "") or "")
                 or tgt in str(getattr(_out_item(r), "name", "") or "")
                 or str(getattr(r, "name", "") or "") in tgt
                 or str(getattr(_out_item(r), "name", "") or "") in tgt]
        if fuzzy:
            return fuzzy[0]
    # 2) 自家货架缺的品类
    shop = _own_shop(world, npc)
    if shop is not None:
        have_types = set()
        for e in (getattr(shop, "stock", None) or []):
            it = item_by_id.get(str(getattr(e, "item_id", "") or ""))
            if it is not None:
                have_types.add(str(getattr(it, "type", "") or ""))
        gap = [r for r in ordered if str(getattr(_out_item(r), "type", "") or "") not in have_types]
        if gap:
            return gap[0]
    # 3) shop_type 对口
    st = str(getattr(npc, "shop_type", "") or "")
    if not st and shop is not None:
        st = str(getattr(shop, "shop_type", "") or "")
    want_types = _SHOP_TYPE_CRAFT.get(st)
    if want_types:
        matched = [r for r in ordered
                   if str(getattr(_out_item(r), "type", "") or "") in want_types]
        if matched:
            return matched[0]
    # 4) 材料最充足（multiset 可支撑份数 = min(各材料现存数)；同分取首）
    def _craftable_batches(r) -> int:
        needed = Counter(getattr(r, "inputs", None) or [])
        if not needed:
            return 0
        return min(inv.count(iid) // qty for iid, qty in needed.items())
    best = max(ordered, key=_craftable_batches)
    if _craftable_batches(best) > 0:
        return best
    # 5) 随机兜底（全部零份时材料维度无区分；纯调用无 rng 取首条保确定性）
    return rng.pick(ordered) if rng is not None else ordered[0]


def do_craft(world: Any, npc: Any, rng: SeededRng, target: str = "") -> Optional[Any]:
    recipes = _basic_recipes(world, npc)
    if not recipes:
        return None
    from src.services import crafting_engine as cra
    recipe = pick_craft_recipe(world, npc, recipes, target=target, rng=rng)
    if recipe is None:
        return None
    # craft 鸭子类型吃 npc（inventory/stat_int 同 PlayerState 口径）；失败材料折损与玩家同价。
    # [!] 容量门：craft 内 try_add_to_inventory 用玩家口径 carry_capacity(20+vit*2)，
    # 会把 NPC 背包撑破 _NPC_INV_CAP -> 此后 _inv_add 对一切新增返回 False，互市/互赠/
    # 进货/采集对该 NPC 全部停摆——产出是新 id 且包满时须先拦下。产出落新 id 三条路径：
    # 模板不在包 / 装备克隆（identified 模板）/ 大成功升档（非 mythic 可升）；
    # 有材料可扣（腾槽）时放行——扣后 +1 恰回 cap 不超限（与玩家侧 can_craft 同口径）。
    _inv = getattr(npc, "inventory", None)
    _out_pre = str(getattr(recipe, "output_item_id", "") or "")
    if isinstance(_inv, list) and len(_inv) >= _NPC_INV_CAP:
        _ins = getattr(recipe, "inputs", None) or []
        _freed = len({str(i) for i in _ins if str(i) in _inv})
        _out_it = _item(world, _out_pre) if _out_pre else None
        _new_id = bool(_out_pre) and ((_out_pre not in _inv) or (_out_it is not None and (
            (getattr(_out_it, "type", "") in ("weapon", "armor", "accessory")
             and getattr(_out_it, "identified", True) is not False)
            or getattr(_out_it, "rarity", "common") != "mythic")))
        if _new_id and _freed == 0:
            return None
    r = cra.craft(npc, recipe, world, rng=rng)
    out = _item(world, str(r.get("output_item_id") or ""))
    if not r.get("success") or out is None:
        return None
    # [野心 2026-09-06] 匠心判定：累计合成件数
    try:
        npc.crafted_count = max(0, int(getattr(npc, "crafted_count", 0) or 0)) + 1
    except Exception:
        pass
    return _mk_event(world, npc, f"{npc.name}合成", f"{npc.name}着手炼制，做出「{out.name}」。",
                     severity="minor",
                     cause=f"背包备齐「{recipe.name}」所需材料，决定制作{out.name}。",
                     outcome=f"消耗材料后产出「{out.name}」，已进入随身背包。",
                     shop_id=str(getattr(_own_shop(world, npc), "id", "") or ""),
                     item_ids=list(dict.fromkeys(list(recipe.inputs or []) + [out.id])))


def do_stock_shop(world: Any, npc: Any, rng: SeededRng) -> Optional[Any]:
    shop = _own_shop(world, npc)
    if shop is None:
        return None
    from src.services import trade_engine as tre
    from src.models.shop import ShopStockEntry
    stocked = []
    stocked_ids = []
    inv = list(getattr(npc, "inventory", None) or [])
    for iid in inv:
        it = _item(world, iid)
        if it is None or it.type not in ("weapon", "armor", "consumable", "accessory"):
            continue
        if it.rarity not in _BASIC_RARITIES:
            continue                                   # 蓝封顶（用户定稿）
        if any(e.item_id == iid for e in (shop.stock or [])):
            continue
        price = max(1, int(tre.compute_item_price(it)))
        # [P36b] tag="npc_made"：NPC 手制货——系统漂移/补货不碰，玩家购买货款进商人钱包
        shop.stock.append(ShopStockEntry(item_id=iid, price=price, stock=1, max_stock=1,
                                          tag="npc_made"))
        npc.inventory.remove(iid)
        stocked.append(it.name)
        stocked_ids.append(iid)
        if len(stocked) >= 3:
            break
    if not stocked:
        return None
    try:
        shop.touch()
    except Exception:
        pass
    return _mk_event(world, npc, f"{npc.name}上架补货",
                     f"{npc.name}把亲手制得的{'、'.join(stocked)}摆上了货架。", severity="minor",
                     cause=f"随身背包中有{'、'.join(stocked)}可供出售。",
                     outcome=f"{'、'.join(stocked)}已从背包转入「{shop.name}」货架，玩家可在店内购买。",
                     shop_id=str(shop.id), item_ids=stocked_ids)


# ===========================================================================
# [P45 2026-09-12] 打猎：与玩家同口径（真实野怪实体 + 同参数奖励 + 装备进胜率）
# ===========================================================================

def _wilderness_locs(world: Any) -> list:
    return [l for l in (getattr(world, "locations", None) or [])
            if str(getattr(l, "kind", "") or "") == "wilderness"]


def _nearest_wilderness(world: Any, npc: Any, wilds: Optional[list] = None):
    """NPC 的「附近狩猎地」解析：当前地点是野外 -> 就地；否则取最近野外点。

    [P45] 用户口径「NPC 像玩家一样也是附近地点随机遇怪」——聚落内不刷怪（守
    wilderness_engine 的野外门），NPC 视作当日往返最近野外。距离用 (x,y) 平方距离，
    同距离按 id 稳定序取首（确定性，不耗 rng）。世界无野外点 -> None（该 NPC 不狩猎）。
    """
    if wilds is None:
        wilds = _wilderness_locs(world)
    if not wilds:
        return None
    cur = _loc_of(world, npc)
    if cur is not None and str(getattr(cur, "kind", "") or "") == "wilderness":
        return cur
    if cur is None:
        return sorted(wilds, key=lambda l: str(getattr(l, "id", "")))[0]
    cx, cy = int(getattr(cur, "x", 0) or 0), int(getattr(cur, "y", 0) or 0)
    return sorted(wilds, key=lambda l: ((int(getattr(l, "x", 0) or 0) - cx) ** 2
                                        + (int(getattr(l, "y", 0) or 0) - cy) ** 2,
                                        str(getattr(l, "id", ""))))[0]


def hunt_win_chance(npc: Any, monster: Any, npc_atk: int, npc_def: int,
                    rng: SeededRng, atk_mult: float = 1.0) -> float:
    """[P45 2026-09-12 v3] 打猎胜率（含装备；**不稀释等级差**——用户定稿「打不过用装备顶」）。

    NPC 战力 = 等级 x10 + 力 x atk_mult + 耐 + 装备武器攻 x0.8 x atk_mult + 装备防具防 x0.4；
    怪物战力 = 猎物等级 x12 + rng(0,8)（与旧口径同系数——怪物战力是隐含装备的抽象值，
    不再另算怪物属性，避免给怪物二次加成）。胜率 = clamp(0.5 + 0.5 x 差/怪物战力, 0.05, 0.95)。

    [口径] 同级时 NPC 略优（12L+10+装备 vs 12L+4）；猎物在 [L, L+danger+3] 均匀抽、平均高
    (danger+3)/2 级，故平均胜率天然 <50% 且随 danger 递减——这是刻意保留的 danger 梯度。
    NPC 的成长杠杆 = 装备（do_craft 造 / 打猎掉落 / 初始配装），不靠公式放水。
    消耗 rng 一次 rng.roll(0,8)（确定性；同 rng 同结果）。

    [P52 伤疤狩猎消费点 2026-09-26] atk_mult = 伤疤攻击侧乘数（臂伤 0.9，injury_stat_mult
    单一出口）作用到力量贡献与装备武器攻——带伤狩猎变差；默认 1.0 既有调用零位移。
    """
    lvl = max(1, int(getattr(npc, "level", 1) or 1))
    m_lvl = max(1, int(getattr(monster, "level", 1) or 1))
    from src.services import talent_engine as te
    npc_power = lvl * 10 + int(te.effective_stat(npc, "str") * atk_mult) \
        + te.effective_stat(npc, "vit") \
        + int(int(npc_atk) * _HUNT_GEAR_ATK_W * atk_mult) + int(int(npc_def) * _HUNT_GEAR_DEF_W)
    mon_power = m_lvl * _HUNT_MON_COEF + rng.roll(0, 8)
    return max(0.05, min(0.95, 0.5 + 0.5 * (npc_power - mon_power) / max(10, mon_power)))


def _world_currency(world: Any) -> str:
    """题材货币显示名（卖钱文案用；异常回退「钱」）。"""
    try:
        from src.models.world_sim_preset import GenreText
        return str(GenreText(getattr(world, "config_overlay", None) or {}).currency or "钱")
    except Exception:  # noqa: BLE001 - 文案兜底不影响结算
        return "钱"


def do_hunt(world: Any, npc: Any, rng: SeededRng,
            monster: Optional[Any] = None, hunt_loc: Optional[Any] = None) -> Optional[Any]:
    """打猎（与玩家同口径的轻量确定性模拟）。

    [P45 2026-09-12 用户指示] 猎物改用 wilderness_engine.roll_wilderness_monster 生成
    （真实 loot_table/精英口径；怪物等级 = band_level(danger, NPC 等级) 守 P43），奖励与
    玩家 combat 结算同参数：经验 = 猎物等级 x15 x 难度系数、掉落 roll_loot(drop_rate=0.4,
    luck) + upgrade_drops。**不打猎金币**（用户定稿 v3：NPC 靠卖东西赚钱——打猎金币曾实测
    让 NPC 钱包 300 天 +12 万、拍卖会被包圆——交叉审核结论，已按用户定稿移除）。
    胜率含 NPC 真实装备攻防（combat_engine.equipped_attack_defense），**不稀释等级差**
    （用户定稿：打不过用装备顶，danger 梯度保留）。

    胜 -> 经验/掉落入包；败 -> 掉血；重伤回城 + 概率遗失物品；阵亡 -> alive=False +
    respawn（守「击败 NPC 必须置 alive=False」契约）。
    [P45 v3] 死亡统一走 npc_permadeath（原「要角 respawn=0 永久」特判已删，三处口径
    见 world_sim_service 两处击杀点）；主线发布人（quest_engine.is_main_giver）不死。
    monster/hunt_loc 可预传（每日被动遭遇路径已生成怪物并完成遇怪判定，避免二次消费 rng）。
    """
    from src.services import combat_engine as ce
    from src.services import wilderness_engine as we
    # [P45 v3.1 审核修复] 野外怪物总闸对两条狩猎入口同口径：玩家关档（无怪世界）时
    # NPC 计划 hunt 不得凭空造怪开打（与 A2 的 monsters_on 门一致）。
    if not (getattr(world, "config_overlay", None) or {}).get("wilderness_monsters_enabled", True):
        return None
    loc = hunt_loc if hunt_loc is not None else _nearest_wilderness(world, npc)
    if loc is None:
        return None
    if monster is None:
        # force=True：打猎是「已决定开打」，只借 roll_wilderness_monster 的「同源生成怪 +
        # 真实 loot_table」；遇怪概率由 P36a 计划/被动遭遇两条入口各自把关，不重复 roll。
        monster = we.roll_wilderness_monster(
            world, loc, max(1, int(getattr(npc, "level", 1) or 1)),
            True, 1.0, 0.0, rng, force=True)
        if monster is None:
            return None
    m_lvl = max(1, int(getattr(monster, "level", 1) or 1))
    mname = str(getattr(monster, "name", "怪物") or "怪物")
    w_atk, a_def = ce.equipped_attack_defense(world, npc)
    # [P52 伤疤狩猎消费点 2026-09-26] 带伤狩猎变差（injury_stat_mult docstring 承诺的
    # 消费点此前从未接线）：atk_mult 作用到攻击侧（力量贡献+装备武器攻），臂伤 0.9；
    # 腿/躯干/头伤不影响狩猎口径
    atk_mult = ce.injury_stat_mult(npc, "atk", int(getattr(world, "day_count", 1) or 1))
    if rng.chance(hunt_win_chance(npc, monster, w_atk, a_def, rng, atk_mult=atk_mult)):
        # ---- 胜：经验/掉落与玩家 combat 结算同参数（金币不出，钱走出清卖货）----
        overlay = getattr(world, "config_overlay", None) or {}
        difficulty = str(overlay.get("difficulty", "normal") or "normal") \
            if isinstance(overlay, dict) else "normal"
        from src.services import talent_engine as te
        xp = int(m_lvl * _HUNT_XP_PER_LVL * ce.xp_difficulty_mult(difficulty))
        leveled = _on_gain_xp(world, npc, xp)
        # 掉落：猎物真实 loot_table + 天赋 luck_bonus/loot_mult（与玩家两路结算同口径）
        eff_luck = te.effective_stat(npc, "luk") + int(te.get_talent_bonus(npc, "luck_bonus") or 0)
        eff_rate = min(0.95, _HUNT_DROP_RATE * float(te.get_talent_mult(npc, "loot_mult") or 1.0))
        got = ce.roll_loot(getattr(monster, "loot_table", None) or [], rng,
                           drop_rate=eff_rate, difficulty_mult=ce.loot_difficulty_mult(difficulty),
                           luck=eff_luck)
        # [P45 v3.1 审核修复] 先按「有用性」分层再升档：upgrade_drops 会把材料克隆成
        # {id}__rarity 新 id，若先升档再判定，配方料必然不在 _craftable_recipe_inputs
        # （存的是配方原始 inputs id）-> 本该留着合成的料被误卖。材料不参与升档
        # （素材是商品，无稀有度收益），装备/消耗品照旧升档。
        useful = _craftable_recipe_inputs(world)
        mats = [g for g in got if (_item(world, g) is not None
                                   and getattr(_item(world, g), "type", "") == "material")]
        others = [g for g in got if g not in mats]
        others = ce.upgrade_drops(others, world, rng, luck=eff_luck, monster_level=m_lvl)
        # [P45 v3.1 用户口径] NPC 靠卖东西赚钱：掉落中「用不上的材料」（不在任何可造配方
        # inputs 里）当场折价（五成）脱手给收购商；配方料与装备/消耗品入包留着自用。
        # [!] 必须在狩猎当场变现——若只靠「背包 >8 件出清」，实测 30 天 12 名战斗 NPC
        # 出货 0 次（掉落太慢，背包到不了阈值），NPC 零收入。
        from src.services import trade_engine as _tre
        kept: list = []
        cash = 0
        cash_names: list = []
        for g in mats + others:
            it = _item(world, g)
            if it is not None and getattr(it, "type", "") == "material" \
                    and str(g) not in useful:
                part = max(1, int(_tre.compute_item_price(it)) // 2)
                npc.wallet = int(getattr(npc, "wallet", 0) or 0) + part
                cash += part
                cash_names.append(str(getattr(it, "name", "") or g))
            elif _inv_add(npc, g):
                kept.append(g)
            else:
                # [P45 v3.1 审核修复 GLM] 包满/同 id 去重时不再静默湮灭：按废料五成兜底
                # 变现（守 social_engine「物品守恒，勿湮灭」先例）。
                part = max(1, int(_tre.compute_item_price(it)) // 2) if it is not None else 1
                npc.wallet = int(getattr(npc, "wallet", 0) or 0) + part
                cash += part
                cash_names.append(str(getattr(it, "name", "") or g) if it is not None else g)
        desc = f"{npc.name}猎杀了{mname}（{m_lvl}级），获得 {xp} 经验"
        if kept:
            names = "、".join((_item(world, g).name if _item(world, g) else g) for g in kept[:3])
            desc += f"，拾得{names}"
        if cash > 0:
            desc += f"，{('、'.join(cash_names[:3]))}顺手折给了收购商得 {cash} {_world_currency(world)}"
        if leveled:
            desc += f"，晋升 {npc.level} 级！"
        return _mk_event(world, npc, f"{npc.name}狩猎", desc + "。", severity="minor",
                         cause=f"在{loc.name}遭遇{mname}并交战。", outcome=desc + "。",
                         item_ids=list(dict.fromkeys(kept)))
    # ---- 败 ----
    dmg = m_lvl * 3 + rng.roll(0, 5)
    npc.hp = max(0, int(getattr(npc, "hp", 1) or 1) - dmg)
    from src.services import quest_engine as _qe
    if npc.hp <= 0 and _qe.is_main_giver(world, npc):
        npc.hp = 1          # [P45 v3 主线保护] 主线发布人不死：濒死保留 1 HP，走下方重伤回城
    if npc.hp <= 0:
        npc.alive = False
        # [P45 v3 用户定稿] 死亡统一由 npc_permadeath 决定：True -> cleanup_and_respawn
        # 不消费水位（无人重生）；False -> 到期重生。原「要角 respawn=0 永久」特判已删。
        npc.respawn_at_tick = int(getattr(world, "tick_count", 0) or 0) + rng.roll(8, 14)
        npc.respawn_location_id = str(getattr(npc, "home_location_id", "") or
                                      getattr(npc, "location_id", "") or "")
        ev = _mk_event(world, npc, f"{npc.name}阵亡",
                       f"{npc.name}狩猎{mname}不敌，伤重倒下。", severity="major",
                       cause=f"在{loc.name}遭遇{mname}并交战。",
                       outcome="狩猎落败，气血归零。")
        qtxt = _qe.cleanup_dead_giver_quests(world, npc)
        if qtxt:
            ev.desc = (ev.desc or "") + qtxt
        return ev
    # 重伤回城疗养 + 概率遗失一件
    ratio = npc.hp / max(1, int(getattr(npc, "hp_max", 1) or 1))
    desc = f"{npc.name}狩猎{mname}落败，负伤"
    if ratio < _RETURN_HP_RATIO:
        home = str(getattr(npc, "home_location_id", "") or "") or _nearest_settlement_id(world, npc)
        if home:
            _move_npc(world, npc, home)
            desc += f"，被同伴随护送回{(_loc_of(world, npc).name if _loc_of(world, npc) else '城镇')}疗养"
        if npc.inventory and rng.chance(_LOSE_ITEM_CHANCE):
            lost = npc.inventory.pop(rng.roll(0, len(npc.inventory) - 1))
            li = _item(world, lost)
            desc += f"，途中遗失了「{li.name if li else lost}」"
    return _mk_event(world, npc, f"{npc.name}负伤", desc + "。", severity="minor",
                     cause=f"在{loc.name}遭遇{mname}并交战。", outcome=desc + "。")


def daily_passive_encounters(world: Any, base_chance: float = _PASSIVE_ENC_BASE,
                             monsters_on: bool = True, season_mult: float = 1.0,
                             max_fights: int = _PASSIVE_FIGHTS_CAP) -> list:
    """[P45 2026-09-12 方案 A2] 每日被动遭遇：战斗身份 NPC 各自 roll 一次遇怪。

    用户口径「让 NPC 像玩家一样也是附近地点随机遇怪」——不再依赖被抽进 6 人 roster：
    每个战斗身份（is_combat_identity）存活 NPC 每日按「附近野外地点 danger」经
    wilderness_engine.roll_wilderness_monster 内部复用 encounter_chance(loc, base,
    is_gather=False) 判定一次；命中即用同一 rng 开打（do_hunt 复用已生成的怪，不重复消费）。
    精英率传 0：批量 NPC 不打精英（防技能书/高品掉落批量涌出，守经济闸）。

    确定性：每 NPC 独立 rng = seed_from(world_id, day, f"npc_enc_{npc.id}")，结果与
    world.npcs 顺序无关、同 world+day 可回放。单 NPC 异常不吞整日（action 粒度隔离）；
    max_fights 上限防「大世界 x 高危区」事件洪水与性能爆炸（默认 6 场/日）。
    返回 WorldEvent 列表（每场最多 1 条，无遭遇不产事件）。
    """
    events: list = []
    if not monsters_on:
        return events
    from src.services import wilderness_engine as we
    day = int(getattr(world, "day_count", 1) or 1)
    wilds = _wilderness_locs(world)
    if not wilds:
        return events
    cap = max(0, int(max_fights or 0))
    fought = 0
    for npc in (getattr(world, "npcs", None) or []):
        if fought >= cap:
            break
        try:
            if not getattr(npc, "alive", False) or not is_combat_identity(npc):
                continue
            hp_max = max(1, int(getattr(npc, "hp_max", 1) or 1))
            if int(getattr(npc, "hp", 0) or 0) < _PASSIVE_HP_MIN * hp_max:
                continue                       # 重伤者不出门（先疗伤/买药，防送死）
            loc = _nearest_wilderness(world, npc, wilds)
            if loc is None:
                continue
            rng = SeededRng.seed_from(str(getattr(world, "id", "") or ""), day,
                                      f"npc_enc_{getattr(npc, 'id', '')}")
            monster = we.roll_wilderness_monster(
                world, loc, max(1, int(getattr(npc, "level", 1) or 1)), True,
                base_chance, 0.0, rng, is_gather=False, season_mult=season_mult)
            if monster is None:
                continue                       # 未遇怪（encounter_chance 未命中）
            ev = do_hunt(world, npc, rng, monster=monster, hunt_loc=loc)
            fought += 1
            if ev is not None:
                events.append(ev)
        except Exception:  # noqa: BLE001 - 单 NPC 异常不吞整日（与执行器同口径）
            continue
    return events


def do_rest(world: Any, npc: Any, rng: SeededRng) -> Optional[Any]:
    hp_max = max(1, int(getattr(npc, "hp_max", 1) or 1))
    old_hp = int(getattr(npc, "hp", 1) or 1)
    heal = min(hp_max - old_hp, max(1, hp_max // 3))
    if heal <= 0:
        return None
    npc.hp = old_hp + heal
    return _mk_event(world, npc, f"{npc.name}休整",
                     f"{npc.name}歇息了一日，恢复 {heal} 点气血。", severity="trivial",
                     cause=f"气血仅剩 {old_hp}/{hp_max}，需要休整。",
                     outcome=f"恢复 {heal} 点气血，现为 {npc.hp}/{hp_max}。")


def do_shop_errand(world: Any, npc: Any) -> Optional[Any]:
    """在本地货架买一件真正有库存的日用品；结算与玩家买货同源。"""
    if not getattr(npc, "alive", False):
        return None
    from src.services import trade_engine as tre
    loc_id = str(getattr(npc, "location_id", "") or "")
    owned = set(getattr(npc, "inventory", None) or [])
    for iid in owned:
        carried = _item(world, iid)
        eff = getattr(carried, "consume_effect", None)
        if (getattr(carried, "type", "") == "consumable"
                and not (isinstance(eff, dict) and eff.get("type") == "feed")):
            return None  # 手上已有备用品，不重复扫货
    wallet = max(0, int(getattr(npc, "wallet", 0) or 0))
    # 留足至少两日基本开销，避免为买一瓶药当天断粮。
    from src.services.social_engine import living_cost
    reserve = 2 * living_cost(npc)
    if wallet <= reserve or len(getattr(npc, "inventory", None) or []) >= _NPC_INV_CAP:
        return None
    offers = []
    for shop in (getattr(world, "shops", None) or []):
        if str(getattr(shop, "location_id", "") or "") != loc_id:
            continue
        if not tre.shop_open(world, shop):
            continue
        bound_owner = str(getattr(shop, "merchant_npc_id", "") or "")
        keepers = [n for n in (getattr(world, "npcs", None) or [])
                   if (bound_owner and str(getattr(n, "id", "") or "") == bound_owner)
                   or (getattr(n, "is_merchant", False)
                       and str(getattr(n, "shop_id", "") or "") == str(shop.id))]
        if keepers and not any(getattr(n, "alive", False) for n in keepers):
            continue
        if (bound_owner == str(getattr(npc, "id", "") or "")
                or (getattr(npc, "is_merchant", False)
                    and str(getattr(npc, "shop_id", "") or "") == str(shop.id))):
            continue
        for entry in (getattr(shop, "stock", None) or []):
            if int(getattr(entry, "stock", 0) or 0) == 0 or entry.item_id in owned:
                continue
            it = _item(world, entry.item_id)
            if it is None or getattr(it, "type", "") != "consumable" \
                    or getattr(it, "rarity", "") not in ("common", "uncommon"):
                continue
            eff = getattr(it, "consume_effect", None) or {}
            # 食物与伤药已有专用日结管线，这里采购可备用的消耗品。
            if isinstance(eff, dict) and eff.get("type") == "feed":
                continue
            price = tre.buy_price(entry, it, shop, 0, tre.world_pace(world))
            if 0 < price <= wallet - reserve:
                offers.append((price, str(shop.id), str(it.id), shop, entry, it))
    if not offers:
        return None
    price, _, _, shop, entry, it = min(offers, key=lambda x: x[:3])
    if not _inv_add(npc, it.id):
        return None
    npc.wallet = wallet - price
    if entry.stock != -1:
        entry.stock = max(0, int(entry.stock) - 1)
    shop.touch()
    keeper = next((n for n in (getattr(world, "npcs", None) or [])
                   if getattr(n, "alive", False) and n.id == shop.merchant_npc_id), None)
    if keeper is None:
        keeper = next((n for n in (getattr(world, "npcs", None) or [])
                       if getattr(n, "alive", False) and getattr(n, "is_merchant", False)
                       and getattr(n, "shop_id", "") == shop.id), None)
    if keeper is not None:
        keeper.wallet = max(0, int(getattr(keeper, "wallet", 0) or 0)) + price
    npc.current_action = f"在{shop.name}添置日用品"
    return _mk_event(world, npc, f"{npc.name}采购",
                     f"{npc.name}在{shop.name}花 {price} {_world_currency(world)}买下{it.name}备用。",
                     cause="背包缺少备用日用品，预留生活费后仍能支付。",
                     outcome=f"{it.name}进入背包；支付 {price} {_world_currency(world)}，余款 {npc.wallet}。",
                     shop_id=str(shop.id), item_ids=[str(it.id)])


def daily_civilian_livelihood(world: Any, social_on: bool = True) -> list:
    """普通居民靠本地零工维持生计；据点雇员、店主已有各自收入，不重复发薪。"""
    from src.models.world import WorldEvent
    from src.services.social_engine import living_cost
    employed = {str(r.get("npc_id") or "") for d in (getattr(world, "domains", None) or [])
                for r in (getattr(d, "roster", None) or []) if isinstance(r, dict)}
    locs = {str(l.id): l for l in (getattr(world, "locations", None) or [])}
    paid = []
    coworkers: dict[str, list] = {}
    for npc in (getattr(world, "npcs", None) or []):
        loc = locs.get(str(getattr(npc, "location_id", "") or ""))
        if not getattr(npc, "alive", False) or getattr(npc, "is_key_npc", False) \
                or getattr(npc, "is_merchant", False) or getattr(npc, "shop_id", "") \
                or is_combat_identity(npc) or str(npc.id) in employed \
                or loc is None or getattr(loc, "kind", "") != "settlement":
            continue
        wallet = max(0, int(getattr(npc, "wallet", 0) or 0))
        npc.current_action = f"在{loc.name}做本行零工"
        coworkers.setdefault(str(loc.id), []).append(npc)
        ambition = getattr(npc, "ambition", None) or {}
        ambition_desc = str(ambition.get("desc", "") or "")[:20] if isinstance(ambition, dict) else ""
        kind = str(ambition.get("kind", "") or "") if isinstance(ambition, dict) else ""
        # 攒钱心愿不能被普通居民的 20 钱防通胀线卡死；单日工资仍封顶 8。
        try:
            work_limit = max(20, int(ambition.get("target_value", 20) or 20)) \
                if kind == "wealth" else 20
        except (TypeError, ValueError):
            work_limit = 20
        if ambition_desc:
            npc.current_thought = f"还惦记着{ambition_desc}"[:30]
            if kind in ("wealth", "craft", "collect", "explore"):
                npc.daily_goal = f"攒盘缠推进{ambition_desc}"[:30]
            elif kind == "courtship":
                npc.daily_goal = f"忙完去寻{ambition_desc}"[:30]
            else:
                npc.daily_goal = f"忙完继续{ambition_desc}"[:30]
        else:
            npc.current_thought = "手头还得留些生活钱" if wallet < work_limit else "忙完再顾自己的事"
            npc.daily_goal = "靠今日工钱维持生活" if wallet < work_limit else "守住手头的积蓄"
        if wallet >= work_limit:
            continue
        wage = min(8, max(3, living_cost(npc) + (3 if kind == "wealth" else 1)))
        npc.wallet = wallet + wage
        paid.append((npc, wage, loc))
    tick = int(getattr(world, "tick_count", 0) or 0)
    events = []
    if paid:
        names = "、".join(n.name for n, _, _ in paid[:5])
        suffix = f"等 {len(paid)} 人" if len(paid) > 5 else ""
        events.append(WorldEvent(
            tick=tick, category="npc", severity="trivial", title="居民营生",
            desc=f"{names}{suffix}在住处附近接了零工，各自拿到当日工钱。",
            npcs=[str(n.id) for n, _, _ in paid],
            locations=list(dict.fromkeys(str(l.id) for _, _, l in paid))))
    if social_on:
        from src.services import social_engine as soc
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        for loc_id, group in sorted(coworkers.items()):
            group = sorted(group, key=lambda n: str(n.id))
            if len(group) < 2:
                continue
            by_id = {str(n.id): n for n in group}
            sought = [(n, by_id[str(n.ambition.get("target_npc_id", ""))]) for n in group
                      if isinstance(getattr(n, "ambition", None), dict)
                      and n.ambition.get("kind") == "courtship"
                      and str(n.ambition.get("target_npc_id", "")) in by_id
                      and str(n.ambition.get("target_npc_id", "")) != str(n.id)]
            if sought:
                a, b = sought[0]
            else:
                idx = (day - 1) % (len(group) - 1)
                a, b = group[idx], group[idx + 1]
            old = soc._social_get(a, str(b.id))
            if old < 0 or old >= 100:
                continue
            new = min(100, old + 2)
            soc._social_set(a, b, new)
            if soc.social_stage(old) != soc.social_stage(new):
                stage = soc.social_stage(new)
                if stage:
                    label = soc.stage_names(world).get(stage, "熟人")
                    soc._upsert_relation(a, str(b.id), b.name, label)
                    soc._upsert_relation(b, str(a.id), a.name, label)
                events.append(WorldEvent(
                    tick=tick, category="npc", severity="minor", title="同事交情",
                    desc=f"{a.name}与{b.name}在{locs[loc_id].name}一道做工，渐渐成了{soc.stage_names(world).get(stage, '熟人')}。",
                    npcs=[str(a.id), str(b.id)], locations=[loc_id]))
    return events


def do_buy_materials(world: Any, npc: Any, rng: SeededRng) -> Optional[Any]:
    """[P37] 商人钱包再投资：花 wallet 批发采购基础材料扩产，闭环
    「玩家买货 -> 商人赚钱 -> 买料 -> 扩产」。需求驱动——优先补自家可造配方缺的料，
    余款按价格升序囤通用料；批发价 = compute_item_price x _WHOLESALE_MULT。"""
    needed = _needed_material_ids(world, npc)
    mats = _buyable_materials(world, npc, needed)
    if _own_shop(world, npc) is None and not getattr(npc, "is_merchant", False):
        # [A2 2026-08-29] 扩非商人合成者：仅当有可造基础配方（无建筑门控 + 蓝封顶）
        # 且缺料时进场（需求驱动，不是全民囤料；do_craft 本就不限商人，采购对称放开）。
        # [!] 不可用 _basic_recipes 判缺料——它只返回「料已集齐」的配方，恒无缺口。
        mats = [it for it in mats if it.id in needed]
        if not mats:
            return None
    from src.services import trade_engine as tre
    wallet = int(getattr(npc, "wallet", 0) or 0)
    if wallet < _BUY_WALLET_MIN:
        return None
    if not mats:
        return None
    # 采购单：需求料优先（价格升序），其后通用料（价格升序）——确定性排序不耗 rng
    need_ids = {i.id for i in mats if i.id in needed}
    pool = sorted(mats, key=lambda i: (0 if i.id in need_ids else 1,
                                       max(1, int(tre.compute_item_price(i) * _WHOLESALE_MULT)),
                                       str(i.id)))
    bought: list = []
    bought_ids: list[str] = []
    spent = 0
    for it in pool:
        if len(bought) >= _BUY_PER_DAY:
            break
        price = max(1, int(tre.compute_item_price(it) * _WHOLESALE_MULT))
        if spent + price > wallet:
            continue
        if not _inv_add(npc, it.id):
            continue                                   # 背包满（_inv_add 已带 cap/去重）
        spent += price
        bought.append(it.name)
        bought_ids.append(it.id)
        # [审查修复] 留底语义严格化：采购后余额落回启动线以下即收手（原前置检查对
        # 首/末件不严格——恰好 50 的钱包能花穿底线）
        if wallet - spent <= _BUY_WALLET_MIN:
            break
    if not bought:
        return None
    npc.wallet = wallet - spent
    needed_names = [it.name for it in pool if it.id in need_ids and it.id in bought_ids]
    cause = (f"基础配方缺少{'、'.join(needed_names)}，从场外批发渠道补料。"
             if needed_names else "为后续制作或售卖备料，从场外批发渠道采购。")
    own_shop = _own_shop(world, npc)
    return _mk_event(world, npc, f"{npc.name}进货",
                     f"{npc.name}斥资 {spent} 批进了{'、'.join(bought[:3])}"
                     f"{'等' if len(bought) > 3 else ''}，备料扩产。", severity="minor",
                     cause=cause,
                     outcome=f"支付 {spent} {_world_currency(world)}；材料进入背包，余款 {npc.wallet}。",
                     shop_id=str(getattr(own_shop, "id", "") or ""), item_ids=bought_ids)


def _npc_made_count(shop: Any) -> int:
    """[P45] 货架 npc_made 条目计数（每店上限 6 的判定口径）。"""
    return sum(1 for e in (getattr(shop, "stock", None) or [])
               if str(getattr(e, "tag", "") or "") == "npc_made")


def _craftable_recipe_inputs(world: Any) -> set:
    """[P45 v3] 这个世界里「NPC 用得上」的材料 id 集：所有无建筑门控、蓝封顶配方的 inputs。

    有用性分层的依据——NPC 打到/采到的材料若能进配方就该留着合成（配合 buy_materials 的
    需求驱动进货，构成「卖废料 -> 换钱 -> 买缺料 -> 造装备 -> 胜率提升」闭环）；
    不在任何配方里的材料才是真正的废品。全量一次计算，单 NPC 调用不贵（配方 8~16 条）。"""
    out: set = set()
    for r in (getattr(world, "recipes", None) or []):
        if getattr(r, "required_building", ""):
            continue
        out_item = _item(world, str(getattr(r, "output_item_id", "") or ""))
        if out_item is None or out_item.rarity not in _BASIC_RARITIES:
            continue
        for iid in (getattr(r, "inputs", None) or []):
            if isinstance(iid, str) and iid:
                out.add(iid)
    return out


def daily_sell_surplus(world: Any, npc: Any) -> Optional[Any]:
    """[P45 2026-09-12 v3 用户指示] 背包 >8 件时的材料出清（打猎无金币后的主收入来源）。

    口径（用户定稿）：超过 8 件 -> 把多余材料卖给最近商店，卖价五成当场进 npc.wallet；
    物品以 tag="npc_made" 挂上该店货架（每店上限 6 件，超限换下一家店）。
    - **只卖 material**：药水是 D3 喝药管线口粮、技能书是研读管线，替 NPC 清了等于拆别的系统
      （装备件走 daily_equip_check 的换装/折价口径）。
    - **有用性分层**（NPC 知道什么对自己有用）：出现在可造配方 inputs 里的材料留（+30），
      其余先卖（0）；同分卖最便宜的（价格升序 + id 稳定序，确定性不耗 rng）。
    - **满闸吞货**（用户定稿 v3）：全城货架 npc_made 满时照样卖——wallet 照加、物品 sink，
      不再让 NPC 背包卡死（原「今日不卖」会造成采集/掉落静默丢失）。
    每次调用上限 3 件（服务层每 NPC 每日只调一次）；「最近商店」= 有可解析地点的店按
    (x,y) 平方距离升序（同距 id 稳定序）。无商店/无候选 -> None 不产事件。
    """
    if not getattr(npc, "alive", False):
        return None
    inv = getattr(npc, "inventory", None)
    if not isinstance(inv, list) or len(inv) <= _SURPLUS_THRESHOLD:
        return None
    shops = list(getattr(world, "shops", None) or [])
    if not shops:
        return None
    from src.models.shop import ShopStockEntry
    from src.services import trade_engine as tre
    useful = _craftable_recipe_inputs(world)
    cur = _loc_of(world, npc)
    cx, cy = int(getattr(cur, "x", 0) or 0), int(getattr(cur, "y", 0) or 0)
    # 候选店：按到 NPC 所在地的距离升序（同距离 id 稳定序）——确定性，不耗 rng
    shop_loc = {str(getattr(s, "id", "")): _loc_by_id(world, str(getattr(s, "location_id", "") or ""))
                for s in shops}
    ordered = sorted(
        shops,
        key=lambda s: (((int(getattr(shop_loc.get(str(getattr(s, "id", "")), None), "x", 0) or 0) - cx) ** 2
                        + (int(getattr(shop_loc.get(str(getattr(s, "id", "")), None), "y", 0) or 0) - cy) ** 2)
                       if shop_loc.get(str(getattr(s, "id", ""))) is not None else 10 ** 9,
                       str(getattr(s, "id", ""))))
    cands = []
    for iid in list(inv):
        it = _item(world, iid)
        if it is None or getattr(it, "type", "") != "material":
            continue                       # v3：只卖材料（药水/技能书/装备各有归属管线）
        score = 30 if str(iid) in useful else 0    # 配方料留着合成，废料先卖
        cands.append((score, max(1, int(tre.compute_item_price(it))), str(iid), it))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], t[1], t[2]))  # 先卖「没用」的，同分卖最便宜的
    sold_names: list = []
    sunk = 0
    sold_count = 0        # [!] 上架 + 吞货都计入单日上限（否则满闸日会无限吞、钱包暴增）
    gained = 0
    for score, price, iid, it in cands:
        if len(inv) <= _SURPLUS_THRESHOLD or sold_count >= _SURPLUS_PER_DAY:
            break        # 出清到 8 件即停（保底 8 件随身）；单日上限 3 件（含吞货）
        # [P45 v3.1 审核修复 GLM] 跳过已含同 item_id 条目的店（防跨日重复上架同款）
        shop = next((s for s in ordered if _npc_made_count(s) < _NPC_MADE_PER_SHOP
                     and not any(getattr(e, "item_id", "") == iid
                                 for e in (getattr(s, "stock", None) or []))), None)
        inv.remove(iid)
        part = max(1, price // 2)
        npc.wallet = int(getattr(npc, "wallet", 0) or 0) + part
        gained += part
        sold_count += 1
        if shop is None:
            # [P45 v3 用户定稿] 全城货架 npc_made 满（每店 6）-> 照卖：钱照给、物品吞掉
            # （sink），不让 NPC 背包卡死。
            sunk += 1
            continue
        shop.stock.append(ShopStockEntry(item_id=iid, price=price, stock=1, max_stock=1,
                                         tag="npc_made"))
        try:
            shop.touch()
        except Exception:  # noqa: BLE001 - 时间戳失败不影响交易
            pass
        sold_names.append(it.name if getattr(it, "name", "") else iid)
    if not sold_names and not sunk:
        return None
    if not sold_names:
        # 纯吞货：全部店都满闸，物品 sink、只有进账
        return _mk_event(world, npc, f"{npc.name}出货",
                         f"{npc.name}把 {sunk} 件多余杂货脱手给了行商，得 {gained} "
                         f"{_world_currency(world)}（货太杂，没人肯收，直接进了炉子）。",
                         severity="trivial")
    tail = f"（另有 {sunk} 件杂货店里收不下，直接脱手）" if sunk else ""
    return _mk_event(world, npc, f"{npc.name}出货",
                     f"{npc.name}把多余的{'、'.join(sold_names)}折给了商店，得 {gained} "
                     f"{_world_currency(world)}。{tail}", severity="trivial")


def _loc_by_id(world: Any, lid: str):
    return next((l for l in (getattr(world, "locations", None) or [])
                 if getattr(l, "id", "") == lid), None)


def _nearest_settlement_id(world: Any, npc: Any) -> str:
    cur = _loc_of(world, npc)
    homes = [l for l in (getattr(world, "locations", None) or [])
             if getattr(l, "kind", "") == "settlement"]
    if not homes:
        return str(getattr(npc, "location_id", "") or "")
    if cur is not None:
        homes.sort(key=lambda l: (l.x - cur.x) ** 2 + (l.y - cur.y) ** 2)
    return homes[0].id


def _move_npc(world: Any, npc: Any, dest_id: str) -> None:
    """离屏挪动（不走 [P18] 命令行协议——那是旁白通道；这里直接改 + 同步 npc_ids）。"""
    src = _loc_of(world, npc)
    dst = next((l for l in (getattr(world, "locations", None) or [])
                if getattr(l, "id", "") == dest_id), None)
    if dst is None:
        return
    if src is not None and getattr(npc, "id", "") in (getattr(src, "npc_ids", None) or []):
        src.npc_ids.remove(npc.id)
    if src is not None:
        for place in (getattr(src, "places", None) or []):
            if npc.id in (getattr(place, "npc_ids", None) or []):
                place.npc_ids = [i for i in place.npc_ids if i != npc.id]
    if getattr(npc, "id", "") not in (getattr(dst, "npc_ids", None) or []):
        dst.npc_ids.append(npc.id)
    npc.location_id = dest_id
    npc.place_id = getattr(dst, "default_place_id", "") or ""
    dest_place = next((p for p in (getattr(dst, "places", None) or [])
                       if p.id == npc.place_id), None)
    if dest_place is not None and npc.id not in dest_place.npc_ids:
        dest_place.npc_ids.append(npc.id)


# ---------- 计划与每日推进 ----------

# [A+B 2026-09-06] 规则计划回退的念头/目标题材池（LLM 不可用时用；确定性 id 派生挑选，
# 同 NPC 同日同句。每题材念头/目标各 >=4 句，守数据量铁律）
_GENRE_THOUGHT_POOL: dict[str, dict[str, list[str]]] = {
    "xianxia": {"thought": ["算算这月灵石还差多少", "昨夜修炼似有滞涩，得缓缓", "山下的传闻不知真假", "丹炉的火候总差一分"],
                "goal": ["把今日功课做完", "攒够一瓶丹药的本钱", "探探那条新现的山道", "寻一处清静处打坐"]},
    "wuxia": {"thought": ["旧伤隐隐作痛，怕要变天", "江湖上又起了风声", "那套刀法还有个破绽", "盘缠快见底了"],
              "goal": ["寻个正经营生", "访一位故人", "把伤养利索再赶路", "练熟那记杀招"]},
    "modern": {"thought": ["房租又该交了", "昨晚加班的活还没收尾", "朋友圈那条动态什么意思", "地铁上那人有点眼熟"],
               "goal": ["把手头的活结了", "约朋友吃顿饭", "去健身房练一次", "把这个月账单理清"]},
    "scifi": {"thought": ["能源配额又降了", "舰载 AI 昨晚的日志有异常", "旧区的传闻越传越邪", "义体该做保养了"],
              "goal": ["攒够一次跃迁的燃料", "查清那段异常日志", "去黑市换些零件", "把维修舱修好"]},
    "apocalypse": {"thought": ["罐头只剩三听", "昨晚围墙外有动静", "水源快撑不过一周", "那批物资的传闻可信吗"],
                   "goal": ["再搜刮一栋楼", "加固东侧围墙", "找一处干净水源", "和其他聚落换点药"]},
    "western_fantasy": {"thought": ["炉火边的账总对不上", "北边商队怎么还没到", "旧剑该磨了", "酒馆里那新面孔来路不明"],
                        "goal": ["备齐过冬的粮", "接一单像样的委托", "把武器送到铁匠那修", "打听到北边的路情"]},
}


def rule_thought_goal(world: Any, npc: Any) -> "tuple[str, str]":
    """[A+B] 规则兜底的（念头, 目标）：题材池按 npc.id 派生确定性挑选（同 NPC 同句，
    换 NPC 不同句）；缺题材回退西幻。"""
    ov = getattr(world, "config_overlay", None) or {}
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict)         else "western_fantasy"
    pool = _GENRE_THOUGHT_POOL.get(tid) or _GENRE_THOUGHT_POOL["western_fantasy"]
    r = SeededRng.seed_from(str(getattr(world, "id", "")), 0, f"tg_{getattr(npc, 'id', '')}")
    return r.pick(pool["thought"]), r.pick(pool["goal"])


def rule_plan(world: Any, npc: Any) -> str:
    """规则 AI（LLM 不可用/失败/未到决策日的回退）：按状态优先级选当日动作。"""
    loc = _loc_of(world, npc)
    can_gather = any(int(getattr(node, "richness", 0) or 0) > 0
                     and int(getattr(node, "cooldown_tick", 0) or 0)
                     <= int(getattr(world, "tick_count", 0) or 0)
                     for node in (getattr(loc, "resource_nodes", None) or []))
    hp_ratio = int(getattr(npc, "hp", 1) or 1) / max(1, int(getattr(npc, "hp_max", 1) or 1))
    if hp_ratio < 0.4:
        return "rest"
    if getattr(npc, "is_merchant", False) or getattr(npc, "shop_id", ""):
        if _basic_recipes(world, npc):
            return "craft"
        inv = [i for i in (getattr(npc, "inventory", None) or [])
               if (_item(world, i) is not None
                   and _item(world, i).type in ("weapon", "armor", "consumable", "accessory")
                   and _item(world, i).rarity in _BASIC_RARITIES)]
        # [P37 审查修复] 与 do_stock_shop 同用 _own_shop 双向解析（原内联正向查找对
        # 反向挂载商人判 shop=None，规划器与执行器口径分裂）
        if inv and _own_shop(world, npc) is not None:
            return "stock_shop"
        # [P37] 钱包再投资：材料断档且钱包过启动线 -> 批发买料扩产（先于采集）
        if _can_buy_materials(world, npc):
            return "buy_materials"
        return "gather" if can_gather else "rest"
    # [P45 2026-09-12] 放开 level>=2 门槛：1 级战斗 NPC 也能打猎（用户定稿；
    # 胜率已含装备，低级别多打少赢慢慢升）
    if is_combat_identity(npc):
        monsters_on = (getattr(world, "config_overlay", None) or {}).get(
            "wilderness_monsters_enabled", True)
        return "hunt" if monsters_on and _wilderness_locs(world) else "rest"
    if _basic_recipes(world, npc):
        return "craft"
    return "gather" if can_gather and rng_free_chance(world, npc) else "rest"


def rng_free_chance(world: Any, npc: Any) -> bool:
    """无 rng 的确定性小波动（规则 AI 不持 rng，用 id 派生日序号取模避免全员同动作）。
    [P37 审查修复] 内建 hash() 跨进程加盐不确定——改 md5（与 SeededRng.seed_from 同法）。"""
    import hashlib
    h = int(hashlib.md5(
        f"{getattr(world, 'id', '')}|{npc.id}|{getattr(world, 'day_count', 1)}".encode()
    ).hexdigest(), 16) % 10
    return h < 7


def ensure_life_baseline(npc: Any) -> None:
    """[P37] 生活基线（幂等）：非战斗 NPC（商人/平民）level<=0 抬到平民基线 1、
    hp_max<=0 按 vit 补 hp——与 build 时 _init_combat_stats 平民基线段同口径，此处兜底
    旧档/直构 NPC。已达标者不动（不回血不加级）。"""
    lvl_ok = int(getattr(npc, "level", 0) or 0) >= 1
    hp_ok = int(getattr(npc, "hp_max", 0) or 0) > 0
    if lvl_ok and hp_ok:
        return
    if not lvl_ok:
        npc.level = 1
    if not hp_ok:
        from src.services import combat_engine as ce
        if int(getattr(npc, "stat_vit", 0) or 0) <= 0:
            npc.stat_vit = 5
        npc.hp_max = ce.max_hp_for(npc)
        npc.hp = npc.hp_max


_EXECUTORS = {"gather": do_gather, "craft": do_craft, "stock_shop": do_stock_shop,
              "buy_materials": do_buy_materials, "hunt": do_hunt, "rest": do_rest}


def execute_action(world: Any, npc: Any, action, rng: SeededRng):
    """执行单动作（白名单外回退规则 AI）。action 兼容 str 与 LLM 计划 dict
    （{"action": ..., "target": ...}——target 只有 craft 消费，见 pick_craft_recipe）。
    返回 WorldEvent 或 None（无事发生不记日志）。"""
    if isinstance(action, dict):
        act = str(action.get("action") or "").strip()
        target = str(action.get("target") or "").strip()
        # [A+B 2026-09-06] 念头/目标落值（LLM 日计划批顺带产出；截 60 字防超长脏值；
        # 空串不覆盖旧值——LLM 漏字段时保留昨日念头比清空好）
        _th = str(action.get("thought") or "").strip()[:60]
        _gl = str(action.get("goal") or "").strip()[:60]
        if _th:
            npc.current_thought = _th
        if _gl:
            npc.daily_goal = _gl
    else:
        act, target = str(action or "").strip(), ""
        # [A+B] 规则回退路径：题材池兜底念头/目标（仅当 NPC 尚无念头时补——不覆盖
        # LLM 昨日所写，回退日保留旧念头更自然）
        if not (getattr(npc, "current_thought", "") or "").strip():
            _th, _gl = rule_thought_goal(world, npc)
            npc.current_thought = _th
            npc.daily_goal = _gl
    if act not in _ACTION_VALUES:
        act = rule_plan(world, npc)
    if isinstance(action, dict):
        # LLM 可能给出看似合法却无法执行的计划。只对确定无效的前置条件回退；
        # 合成失败/采集未出货属于真实结果，不能再赠送第二次行动。
        loc = _loc_of(world, npc)
        if ((act == "gather" and not any(
                int(getattr(node, "richness", 0) or 0) > 0
                and int(getattr(node, "cooldown_tick", 0) or 0)
                <= int(getattr(world, "tick_count", 0) or 0)
                for node in (getattr(loc, "resource_nodes", None) or [])))
                or (act == "craft" and not _basic_recipes(world, npc))
                or (act == "stock_shop" and (
                    _own_shop(world, npc) is None or not any(
                        (_item(world, iid) is not None
                         and _item(world, iid).type in ("weapon", "armor", "consumable", "accessory")
                         and _item(world, iid).rarity in _BASIC_RARITIES)
                        for iid in (getattr(npc, "inventory", None) or []))))
                or (act == "buy_materials" and not _can_buy_materials(world, npc))
                or (act == "hunt" and (not is_combat_identity(npc)
                                       or not _wilderness_locs(world)
                                       or not (getattr(world, "config_overlay", None) or {}).get(
                                           "wilderness_monsters_enabled", True)))):
            act, target = rule_plan(world, npc), ""
    npc.current_action = {
        "gather": "在附近采集", "craft": "试着制作物品", "stock_shop": "整理店铺货架",
        "buy_materials": "采买原料", "hunt": "外出狩猎", "rest": "在住处歇息",
    }.get(act, "处理日常事务")
    try:
        if act == "craft" and target:
            return do_craft(world, npc, rng, target=target)
        if act == "hunt" and not is_combat_identity(npc):
            # [P45 v3.1 审核修复 qwen] LLM 日计划对平民排 hunt 的硬闸（提示词只是软措辞）；
            # hunt 已是唯一成长通道，收益/风险权重变大，不能白名单直通。
            fallback = rule_plan(world, npc)
            return _EXECUTORS[fallback](world, npc, rng)
        return _EXECUTORS[act](world, npc, rng)
    except Exception:
        return None      # 单 NPC 异常不吞整日（动作粒度隔离）


def advance_life(world: Any, plans: dict, rng: SeededRng, max_n: int = 6) -> list:
    """每日推进：活跃 NPC 轮换（确定性 roster）-> 逐个执行计划动作 -> 事件列表。

    roster 用 day 派生种子抽样（同 world+day 同名单，与执行 rng 解耦）；敌对 NPC 同样
    参与生活（他们会狩猎/休整——「NPC 像玩家一样活动」，用户定稿）。[P37] 池口径改
    只看 alive（原 level>=1 把商人/平民等非战斗 NPC 永久排除在生活模拟外），成员入池时
    ensure_life_baseline 抬平民基线（level 1 + hp，无技能/掉落仍非战斗单位）。"""
    events: list = []
    day = int(getattr(world, "day_count", 1) or 1)
    roster_rng = SeededRng.seed_from(str(getattr(world, "id", "")), day, "life_roster")
    pool = [n for n in (getattr(world, "npcs", None) or []) if getattr(n, "alive", False)]
    for n in pool:
        ensure_life_baseline(n)
    if not pool:
        return events
    k = max(1, min(int(max_n or 1), len(pool)))
    roster = roster_rng.sample(pool, k) if len(pool) > k else list(pool)
    # [P44] LLM 计划键名容错解析：LLM 写名常加修饰/有别字（「铁匠鲁大锤」/「鲁大锥」），
    # 精确 dict 查会静默丢该 NPC 的当日计划；经统一解析器锚定真实名单再查。
    from src.services.name_resolver import resolve_name as _resolve_name
    plan_keys = [str(k_) for k_ in (plans or {}).keys()] if plans else []
    for npc in roster:
        key = _resolve_name(str(getattr(npc, "name", "")), plan_keys) if plan_keys else None
        plan = (plans or {}).get(key, "") if key else ""
        ev = execute_action(world, npc, plan, rng)
        if ev is not None:
            npc.current_action = str(getattr(ev, "title", "") or "").removeprefix(
                str(getattr(npc, "name", "") or ""))
            events.append(ev)
    return events


# ===========================================================================
# [D3 2026-08-29] 赠品使用闭环（用户定稿：NPC 日 tick 真实用掉随身物品）
# ===========================================================================
# [!] 技能 <=3 全局闸：满 3 门拒研读/拒赠书（送礼入口同口径校验提示）。
NPC_SKILL_CAP = 3


def _inv_remove(npc: Any, item_id: str) -> None:
    inv = getattr(npc, "inventory", None)
    if isinstance(inv, list) and item_id in inv:
        inv.remove(item_id)


def daily_item_usage(world: Any, npc: Any) -> Optional[Any]:
    """[D3] NPC 日使用随身物品（纯 Python 确定性，每日至多 1 件，先救命后进取）：

    优先级：低血喝药（hp < 50% 且有治疗消耗品 -> 真实回血）> 五维药（stat_bonus
    consume_effect -> apply_consume_effect 真实应用 NPC 属性）> 技能书研读
    （teach_skill 学入 npc.skills，同名去重 + 技能 <3 闸）。物品消耗出背包；
    产 minor「npc」事件进右侧动态栏（因果可见）。返回 WorldEvent 或 None。
    """
    if not getattr(npc, "alive", False):
        return None
    inv = getattr(npc, "inventory", None)
    if not isinstance(inv, list) or not inv:
        return None
    from src.models.world import Skill as _Skill, WorldEvent as _WE
    from src.services import combat_engine as ce
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    tick = int(getattr(world, "tick_count", 0) or 0)

    def _evt(title: str, desc: str) -> Any:
        return _WE(tick=tick, category="npc", severity="minor", title=title,
                   desc=desc, npcs=[getattr(npc, "id", "")],
                   locations=[getattr(npc, "location_id", "")])

    # ---- 1) 低血喝药（救命优先）----
    hp = int(getattr(npc, "hp", 0) or 0)
    hp_max = max(1, int(getattr(npc, "hp_max", 1) or 1))
    if hp * 2 < hp_max:
        for iid in list(inv):
            it = item_by_id.get(iid)
            # [单一来源] _is_heal_potion（cure/heal_mp 混合脏数据不当血药误饮）
            if not _is_heal_potion(it):
                continue
            # [heal_full 并入 heal_pct 2026-09-10] 优先结构化/顶层口径结算，
            # 旧 heal_amount 漏网走 item_heal_value 换算兜底。
            eff = getattr(it, "consume_effect", None)
            etype = str((eff or {}).get("type", "") or "") if isinstance(eff, dict) else ""
            if etype == "heal_full" and int((eff or {}).get("amount", 0) or 0) > 0:
                hp_max = max(1, int(getattr(npc, "hp_max", 1) or 1))
                pct = max(1, min(100, int((eff or {}).get("amount", 0) or 0)))
                amount = int(hp_max * pct / 100)
            else:
                amount = ce.item_heal_value(it, npc)
            ce.heal(npc, max(1, amount))
            _inv_remove(npc, iid)
            return _evt("危急救药",
                        f"{npc.name} 服下随身携带的{it.name}，气色好了不少"
                        f"（{hp} -> {npc.hp}）。")

    # ---- 2) 五维药（stat_bonus 真实加属性）----
    for iid in list(inv):
        it = item_by_id.get(iid)
        if it is None or getattr(it, "type", "") != "consumable":
            continue
        eff = getattr(it, "consume_effect", None)
        if not (isinstance(eff, dict) and str(eff.get("type", "") or "") == "stat_bonus"):
            continue
        ok, _msg = ce.apply_consume_effect(npc, it)
        if not ok:
            continue
        _inv_remove(npc, iid)
        return _evt("服药炼体", f"{npc.name} 服下了{it.name}，只觉气息绵长了一分。")

    # ---- 3) 技能书研读（<=3 闸 + 同名去重；与玩家 _read_book 同管线）----
    for iid in list(inv):
        it = item_by_id.get(iid)
        ts = getattr(it, "teach_skill", None) if it is not None else None
        if not (isinstance(ts, dict) and ts.get("name")):
            continue
        skills = npc.skills if isinstance(getattr(npc, "skills", None), list) else []
        if len(skills) >= NPC_SKILL_CAP:
            return None                     # 满 3 门：书留在包里（转赠/卖出由玩家定）
        if any(isinstance(s, dict) and s.get("name") == ts.get("name") for s in skills):
            continue                        # 已掌握：换下一本
        # [技能书个体差异 2026-09-10] 与玩家研读同口径：书 id 盐确定性抖动 power
        # （品级定基准 + 每件抖动；不改书内蓝图模板）
        from src.services.combat_engine import jitter_taught_skill as _jit
        sk = _Skill.from_dict(_jit(ts, getattr(world, "id", ""), iid))
        npc.skills = list(skills) + [sk.to_dict()]
        _inv_remove(npc, iid)
        return _evt("研读技艺", f"{npc.name} 潜心研读《{it.name}》，习得了「{sk.name}」。")
    return None


# ===========================================================================
# [A1 2026-08-29] NPC 装备自主化：日自动换装 + 旧装备出清（纯 Python 确定性）
# ===========================================================================
def _gear_score(it: Any) -> int:
    """装备强度 = 攻 + 防 + 词缀合计 + 属性加成合计（排序/比较用单一口径）。"""
    v = int(getattr(it, "attack", 0) or 0) + int(getattr(it, "defense", 0) or 0)
    for a in (getattr(it, "affixes", None) or []):
        if isinstance(a, dict):
            mods = a.get("mods") if isinstance(a.get("mods"), dict) else a
            if isinstance(mods, dict):
                v += sum(int(x or 0) for x in mods.values()
                         if isinstance(x, (int, float)))
    sb = getattr(it, "stat_bonus", None)
    if isinstance(sb, dict):
        v += sum(int(x or 0) for x in sb.values() if isinstance(x, (int, float)))
    return v


def daily_equip_check(world: Any, npc: Any) -> Optional[Any]:
    """[A1] NPC 日装备自理（每日至多 1 次换装 + 1 次出清，trivial 事件因果可见）：

    1) 换装：背包装备件（type weapon/armor/accessory 且 slot 非空）比当前同槽
       （含空槽=0 分）强度高 -> 穿上，旧件回背包。
    2) 出清：背包里弱于已装备同槽的装备件——商人挂自家货架（tag=npc_made 商人定价
       权口径，P36b）；非商人按卖价五成换钱（wallet += price//2，物品出背包）。
    """
    if not getattr(npc, "alive", False):
        return None
    inv = getattr(npc, "inventory", None)
    if not isinstance(inv, list) or not inv:
        return None
    from src.models.world import WorldEvent as _WE
    from src.services import trade_engine as tre
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    tick = int(getattr(world, "tick_count", 0) or 0)
    equipped = getattr(npc, "equipped", None)
    equipped = equipped if isinstance(equipped, dict) else {}

    def _cur_score(slot: str) -> int:
        cur = item_by_id.get(str(equipped.get(slot, "") or ""))
        return _gear_score(cur) if cur is not None else 0

    def _evt(title: str, desc: str) -> Any:
        return _WE(tick=tick, category="npc", severity="trivial", title=title,
                   desc=desc, npcs=[getattr(npc, "id", "")],
                   locations=[getattr(npc, "location_id", "")])

    ev: Any = None
    # ---- 1) 换装（严格更优才换；slot 空直接穿）----
    for iid in list(inv):
        it = item_by_id.get(iid)
        slot = str(getattr(it, "slot", "") or "") if it is not None else ""
        if it is None or slot == "" \
                or getattr(it, "type", "") not in ("weapon", "armor", "accessory"):
            continue
        if _gear_score(it) > _cur_score(slot):
            old = str(equipped.get(slot, "") or "")
            npc.equipped = dict(equipped)
            npc.equipped[slot] = iid
            equipped = npc.equipped   # [审查修复] 同步局部引用——出清基线须按换装后的新槽件算
            inv.remove(iid)
            if old and old != iid and old not in inv:
                inv.append(old)                  # 旧件回背包（下次出清/转卖）
            ev = _evt("更换装备", f"{npc.name} 换上了更称手的{it.name}。")
            break
    # ---- 2) 出清（弱于已装备同槽的装备件；每日至多 1 件）----
    for iid in list(inv):
        it = item_by_id.get(iid)
        slot = str(getattr(it, "slot", "") or "") if it is not None else ""
        if it is None or slot == "" or iid in (npc.equipped or {}).values() \
                or getattr(it, "type", "") not in ("weapon", "armor", "accessory"):
            continue
        if _gear_score(it) > _cur_score(slot):
            continue                              # 比槽位强的是候选换装件，不清
        shop = next((s for s in (getattr(world, "shops", None) or [])
                     if s.id == str(getattr(npc, "shop_id", "") or "")), None)
        # [审核修复 2026-09-13] 出清上架同守每店 npc_made 上限（同仓 daily_sell_surplus
        # 已按 _NPC_MADE_PER_SHOP 选店，唯独此路径无封顶 -> 长线货架无界膨胀）。
        if shop is not None and _npc_made_count(shop) < _NPC_MADE_PER_SHOP:
            from src.models.shop import ShopStockEntry
            entry = ShopStockEntry(item_id=iid,
                                   price=max(1, int(tre.compute_item_price(it))),
                                   stock=1, max_stock=1)
            entry.tag = "npc_made"
            shop.stock = list(shop.stock or []) + [entry]
            shop.touch()
            desc = f"{npc.name} 把不趁手的{it.name}挂上了自家货架。"
        else:
            gain = max(1, int(tre.compute_item_price(it)) // 2)
            npc.wallet = int(getattr(npc, "wallet", 0) or 0) + gain
            desc = f"{npc.name} 把闲置的{it.name}折价卖了（+{gain}）。"
        inv.remove(iid)
        return ev if ev is not None else _evt("出清旧装备", desc)
    return ev


# ===========================================================================
# [A2 2026-08-29] NPC 自主消费：缺血自知买药（推荐口径：相邻地点找店）+ 合成者补料
# ===========================================================================
def _is_heal_potion(it: Any) -> bool:
    """血药判定单一来源（D3 喝药 / A2 查包 / A2 货架三处共用，防口径漂移）：
    [heal_full 并入 heal_pct 2026-09-10] 纯回血看 heal_pct>0（heal_full 旧别名已由
    _normalize_heal_pct 在收编/读档时迁入并清空）；旧 heal_amount 保留回退（未经理
    归一的测试桩/旧档漏网）；cure/heal_mp 等非空结构化效果不当血药误饮。"""
    if it is None or getattr(it, "type", "") != "consumable":
        return False
    eff = getattr(it, "consume_effect", None)
    etype = str((eff or {}).get("type", "") or "") if isinstance(eff, dict) else ""
    if etype and etype != "heal_full":
        return False
    if etype == "heal_full":
        return (int((eff or {}).get("amount", 0) or 0) > 0
                or int(getattr(it, "heal_pct", 0) or 0) > 0)
    return (int(getattr(it, "heal_pct", 0) or 0) > 0
            or int(getattr(it, "heal_amount", 0) or 0) > 0)


def _has_heal_potion(world: Any, npc: Any) -> bool:
    """背包里是否已有治疗消耗品。"""
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    return any(_is_heal_potion(item_by_id.get(iid))
               for iid in (getattr(npc, "inventory", None) or []))


def daily_medicine_run(world: Any, npc: Any) -> Optional[Any]:
    """[A2] 缺血自知（每日，_tick_npc_life 与 D3/A1 同循环，在喝药之后跑——包里有药
    D3 已喝，这里只管「没药去买」）：hp < 50% 且无药 -> 找药店真实购买。

    找店口径（用户推荐）：当前地点优先，其后相邻地点（按 id 稳定序）；只认 alchemy
    药店。购买 = tre.buy_price 全价、wallet 扣款、货架 -1、药入背包（_NPC_INV_CAP 门）、
    货款入店主钱包（merchant_npc_id 解析存活者；无店主则钱 sink）。minor 事件因果可见。
    买不起/无店/无货/背包满 -> 没钱改采药：do_gather 蹭一口（无资源点安全 None）。
    """
    if not getattr(npc, "alive", False):
        return None
    hp = int(getattr(npc, "hp", 0) or 0)
    hp_max = max(1, int(getattr(npc, "hp_max", 1) or 1))
    if hp * 2 >= hp_max or _has_heal_potion(world, npc):
        return None
    from src.models.world import WorldEvent as _WE
    from src.services import trade_engine as tre
    from src.services.trade_engine import world_pace
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    tick = int(getattr(world, "tick_count", 0) or 0)
    loc = _loc_of(world, npc)
    # 候选地点：当前 -> 相邻（id 稳定序）
    cand_loc_ids = [str(getattr(npc, "location_id", "") or "")]
    if loc is not None:
        cand_loc_ids += sorted(str(c) for c in (getattr(loc, "connections", None) or []))
    # [审查修复] 「当前地优先」须强制：按候选序（当前地 -> 相邻 id 稳定序）排序，
    # 不能沿 world.shops 原序逛（相邻店靠前时会先被逛到）。
    shops = sorted(
        (s for s in (getattr(world, "shops", None) or [])
         if s.shop_type == "alchemy" and s.location_id in cand_loc_ids),
        key=lambda s: (cand_loc_ids.index(s.location_id), str(s.id)))
    wallet = int(getattr(npc, "wallet", 0) or 0)
    for shop in shops:
        for entry in list(shop.stock or []):
            if entry.stock == 0:
                continue
            it = item_by_id.get(entry.item_id)
            if not _is_heal_potion(it):
                continue
            price = tre.buy_price(entry, it, shop, 0, world_pace(world))
            if price <= 0 or price > wallet or not _inv_add(npc, entry.item_id):
                continue
            npc.wallet = wallet - price
            if entry.stock != -1:
                entry.stock = max(0, int(entry.stock) - 1)
            shop.touch()
            # 店入账：货款进店主钱包（无店主/店主亡则 sink）
            # [审查修复] 店主双向解析（对齐 _shop_merchant 口径）：正向
            # merchant_npc_id 优先，回退反向挂载（npc.shop_id 指店 + is_merchant），
            # 否则 P34e 反向挂载的活店主拿不到货款。
            keeper = next(
                (n for n in (getattr(world, "npcs", None) or [])
                 if n.id == str(getattr(shop, "merchant_npc_id", "") or "")
                 and getattr(n, "alive", False)),
                None)
            if keeper is None:
                keeper = next(
                    (n for n in (getattr(world, "npcs", None) or [])
                     if getattr(n, "is_merchant", False)
                     and str(getattr(n, "shop_id", "") or "") == shop.id
                     and getattr(n, "alive", False)),
                    None)
            if keeper is not None:
                keeper.wallet = int(getattr(keeper, "wallet", 0) or 0) + price
            return _WE(tick=tick, category="npc", severity="minor", title="抓药",
                       desc=f"{npc.name} 身子不济，到{shop.name}抓了副{it.name}"
                            f"（花费 {price}）。",
                       npcs=[getattr(npc, "id", "")],
                       locations=[getattr(npc, "location_id", "")])
    # 没钱改采药：无店/无货/买不起 -> 蹭一口采集（无资源点安全跳过）
    rng = SeededRng.seed_from(str(getattr(world, "id", "")),
                              int(getattr(world, "day_count", 1) or 1),
                              f"med_gather_{getattr(npc, 'id', '')}")
    return do_gather(world, npc, rng)


# ===========================================================================
# [记忆驱动 2026-08-29] 危难求援：交情深厚的好友重伤时会主动向玩家求援
# ===========================================================================
_HELP_HP_RATIO = 0.30      # HP < 30% 视为危难
_HELP_AFFINITY_MIN = 60    # 交情 >= 60（朋友）才会开口求援
_HELP_CHANCE = 0.30        # 每日求援概率（cap 一次，防刷屏）


def daily_help_call(world: Any, npc: Any) -> Optional[Any]:
    """[记忆驱动] 危难求援（纯 Python 确定性，盐 = day + npc.id）：

    好友（affinity >= 60）HP < 30% 且没药可喝（A2 买不到时）-> 30%/日 向玩家
    传讯求援（minor npc 事件进右侧动态栏，含所在地点——玩家可循线送药，NPC 次日
    经 D3 喝掉）。因果链：交情 -> 求援 -> 玩家赠药 -> 记忆里记下这份恩情。
    """
    if not getattr(npc, "alive", False):
        return None
    hp = int(getattr(npc, "hp", 0) or 0)
    hp_max = max(1, int(getattr(npc, "hp_max", 1) or 1))
    if hp * 100 >= int(_HELP_HP_RATIO * 100) * hp_max:
        return None
    if int(getattr(npc, "affinity", 0) or 0) < _HELP_AFFINITY_MIN:
        return None
    if _has_heal_potion(world, npc):
        return None                      # 有药自己喝（D3 管线），不必求人
    rng = SeededRng.seed_from(str(getattr(world, "id", "")),
                              int(getattr(world, "day_count", 1) or 1),
                              f"help_call_{getattr(npc, 'id', '')}")
    if rng.random() >= _HELP_CHANCE:
        return None
    from src.models.world import WorldEvent as _WE
    loc = _loc_of(world, npc)
    loc_name = getattr(loc, "name", "") or "远方"
    return _WE(tick=int(getattr(world, "tick_count", 0) or 0),
               category="npc", severity="minor", title="好友求援",
               desc=f"{npc.name} 传讯：伤势沉重困于「{loc_name}」，盼你送些伤药去。",
               npcs=[getattr(npc, "id", "")],
               locations=[getattr(npc, "location_id", "")])
