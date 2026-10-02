"""[P39b] 委托订单引擎（纯 Python + SeededRng；零 LLM 零 Qt）。

订单三来源（全引擎化，[P39 定稿]）：
- restock 商人补货：货架缺的品类（weapon/armor/consumable/accessory 中未上架的）
  -> 从 world.items 挑该品类订单（只收已有物品，守「LLM 商店备货只从已有物品进货」同族契约）；
- urgent 生活急缺：背包无药水的带伤战斗 NPC -> 消耗品急单（出价上浮最高）。

出价 = compute_item_price x 品类系数（restock 1.2 / urgent 1.5，[定稿]
让生产有利可图）；epic+ 订单交付须 identified（赌徒经济闭环）；按日过期（3-7 天）防囤积。
挂靠聚落须有存活 NPC 作发布人（交付当面的契约感）；玩家在挂靠聚落才能交付。

交付结算：qty 件移出背包（同 item_name 匹配，克隆件可交付）-> gold 入账 ->
发布人交情 +3（nre.touch_friend_interact）-> 声望 +1（钳 -100..100）->
物品尝试入发布人背包（商人随后可上架复用；满则就地消费湮灭）。
"""
from __future__ import annotations

from typing import Any

from src.services import npc_life_engine as nle
from src.utils.rng import SeededRng

_ORDER_CAP = 6            # 全世界同时在架订单上限
_PER_SETTLEMENT_CAP = 3   # 每聚落在架上限
_QTY_RANGE = (1, 3)
_EXPIRE_DAYS = (3, 7)
_KIND_MULT = {"restock": 1.2, "urgent": 1.5}
# [P45 用户指示 2026-08-23] 委托交付 -> 发布人所属势力 power 增量（小额高频）
COMMISSION_FACTION_POWER_GAIN = 1
_SELLABLE_TYPES = ("weapon", "armor", "consumable", "accessory")

# [数据量铁律] 订单风味文案池（{npc}{item} 占位；2 类 x >=3/题材，基线测试守护）
# [定版裁剪 2026-09-05] festival 节庆订单类型随节日系统移除
_GENRE_COMMISSION_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "xianxia": {
        "restock": ["{npc}的铺子货架吃紧，愿以{currency}收购{item}救急。",
                    "{npc}托坊市同行传话：店中{item}早已售罄，求购补货。",
                    "来往修士渐多，{npc}的铺子急需{item}，价格好商量。"],
        "urgent": ["{npc}近日历练负伤，急需{item}疗伤，愿出急价。",
                   "{npc}的同伴伤了元气，四处求购{item}。",
                   "山道不太平，{npc}想尽快备下{item}以防万一。"],
    },
    "wuxia": {
        "restock": ["{npc}的镖局耗材将尽，愿出{currency}收购{item}。",
                    "{npc}在客栈贴出条子：店里{item}断了货，求购补上。",
                    "江湖朋友常来常往，{npc}的铺子急需{item}。"],
        "urgent": ["{npc}押镖挂了彩，急寻{item}疗伤。",
                   "{npc}的门人练功受了伤，急需{item}。",
                   "仇家扬言寻仇，{npc}想尽快备好{item}。"],
    },
    "modern": {
        "restock": ["{npc}的店里{item}卖断货了，高价回收。",
                    "{npc}在社区群发消息：店里急收{item}。",
                    "{npc}的门店补货渠道断了，现金收{item}。"],
        "urgent": ["{npc}家里有人住院，急寻{item}，价格好说。",
                   "{npc}深夜发圈：谁有{item}，急用，重谢。",
                   "{npc}赶项目伤了身体，急需{item}。"],
    },
    "scifi": {
        "restock": ["{npc}的补给站{item}库存告罄，收购价上浮。",
                    "{npc}在殖民地频道广播：急收{item}。",
                    "货运船期延误，{npc}的站点需要{item}补位。"],
        "urgent": ["{npc}在气闸事故中受伤，急需{item}。",
                   "{npc}的外勤队员伤情不稳，急收{item}。",
                   "辐射尘暴将至，{npc}想尽快备好{item}。"],
    },
    "apocalypse": {
        "restock": ["{npc}的据点仓库缺{item}，拿物资券来换。",
                    "{npc}在交易墙留了字条：长期收{item}。",
                    "商队几个月没来了，{npc}的据点急需{item}。"],
        "urgent": ["{npc}在搜刮时被咬伤，急需{item}，出急价。",
                   "{npc}的队员伤口感染了，到处找{item}。",
                   "据点防线告急，{npc}想尽快备下{item}。"],
    },
    "western_fantasy": {
        "restock": ["{npc}的店铺{item}卖光了，愿出{currency}收购。",
                    "{npc}在公会布告栏贴出告示：急收{item}。",
                    "商路不太平，{npc}的店需要{item}补货。"],
        "urgent": ["{npc}在猎魔时负了伤，急需{item}疗伤。",
                   "{npc}的随行牧师耗尽了药剂，四处求购{item}。",
                   "兽潮预警未除，{npc}想尽快备好{item}。"],
    },
}


def _templates(world: Any) -> dict:
    ov = getattr(world, "config_overlay", None)
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    return _GENRE_COMMISSION_TEMPLATES.get(tid) or _GENRE_COMMISSION_TEMPLATES["western_fantasy"]


def _currency(world: Any) -> str:
    """[修 2026-08-25] 题材货币名（world.config_overlay -> GenreText.currency），缺键回退金币。

    委托 reason 里的「愿以金币/灵石/银两收购」改为 {currency} 占位后在此注入，
    单一真相源（币名只在 BUILTIN_ATTRIBUTES_TEMPLATES），自定义题材回退也不再串币。"""
    try:
        from src.models.world_sim_preset import GenreText
        return GenreText(getattr(world, "config_overlay", None)).currency
    except Exception:
        return "金币"


def _item(world: Any, iid: str):
    return next((i for i in (getattr(world, "items", None) or [])
                 if getattr(i, "id", "") == iid), None)


def _alive_in(world: Any, loc_id: str) -> list:
    return [n for n in (getattr(world, "npcs", None) or [])
            if getattr(n, "alive", False) and str(getattr(n, "location_id", "") or "") == loc_id]


def _pick_order_item(world: Any, rng: SeededRng, kind: str, issuer) -> Any:
    """按来源挑订单物品（只收已有物品；确定性）。返回 Item 或 None。"""
    items = [i for i in (getattr(world, "items", None) or [])
             if getattr(i, "type", "") in _SELLABLE_TYPES]
    if kind == "restock":
        # 货架缺的品类（复用「缺」口径：issuer 自家店或聚落商店未上架的 type）
        stocked_types = set()
        shops = [s for s in (getattr(world, "shops", None) or [])
                 if str(getattr(s, "location_id", "") or "") == str(issuer.location_id or "")]
        for s in shops:
            for e in (getattr(s, "stock", None) or []):
                it = _item(world, str(getattr(e, "item_id", "") or ""))
                if it is not None:
                    stocked_types.add(str(getattr(it, "type", "") or ""))
        gap = [i for i in items if str(getattr(i, "type", "") or "") not in stocked_types]
        return rng.pick(gap) if gap else None
    # urgent：消耗品向（疗伤急用）
    cons = [i for i in items if str(getattr(i, "type", "") or "") == "consumable"]
    return rng.pick(cons) if cons else None


def maybe_spawn_commissions(world: Any, rng: SeededRng, tick: int) -> list:
    """每日订单生成 + 过期清理（day 水位 last_commission_day；静默不产事件——
    订单板 UI 自身可见，不占事件流）。"""
    from src.models.world import CommissionOrder
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    if int(getattr(world, "last_commission_day", 0) or 0) == day:
        return []
    world.last_commission_day = day
    # 过期清理（防囤积；过期单静默下架）
    world.commissions = [c for c in (getattr(world, "commissions", None) or [])
                         if int(getattr(c, "expire_day", 0) or 0) >= day]
    tmpls = _templates(world)
    settlements = [l for l in (getattr(world, "locations", None) or [])
                   if getattr(l, "kind", "") == "settlement"]
    for loc in settlements:
        by_loc = [c for c in world.commissions
                  if str(getattr(c, "location_id", "") or "") == loc.id]
        if len(by_loc) >= _PER_SETTLEMENT_CAP or len(world.commissions) >= _ORDER_CAP:
            continue
        npcs = _alive_in(world, loc.id)
        if not npcs:
            continue
        # 来源权重：有带伤缺药 NPC -> urgent；否则 restock
        issuer = rng.pick(npcs)
        kind = "restock"
        hurt = [n for n in npcs
                if int(getattr(n, "level", 0) or 0) >= 2
                and int(getattr(n, "hp", 1) or 1) < int(getattr(n, "hp_max", 1) or 1) * 0.7
                and not any((_item(world, i) is not None
                             and _item(world, i).type == "consumable")
                            for i in (getattr(n, "inventory", None) or []))]
        if hurt:
            kind = "urgent"
            issuer = rng.pick(hurt)
        it = _pick_order_item(world, rng, kind, issuer)
        if it is None:
            continue
        from src.services import trade_engine as tre
        unit = max(1, int(tre.compute_item_price(it) * _KIND_MULT[kind]))
        pool = tmpls.get(kind) or tmpls["restock"]
        reason = pool[rng.roll(0, len(pool) - 1)].format(
            npc=issuer.name, item=it.name, currency=_currency(world))
        world.commissions.append(CommissionOrder(
            issuer_npc_id=str(issuer.id), location_id=str(loc.id),
            item_id=str(it.id), item_name=str(it.name),
            qty=rng.roll(*_QTY_RANGE), unit_price=unit, kind=kind, reason=reason,
            created_day=day, expire_day=day + rng.roll(*_EXPIRE_DAYS)))
        if len(world.commissions) >= _ORDER_CAP:
            break
    return []


def deliverable_indices(world: Any, player: Any, order: Any) -> list:
    """玩家背包中可交付的下标列表（同 item_name；epic+ 须 identified）。"""
    out = []
    inv = list(getattr(player, "inventory", None) or [])
    for idx, iid in enumerate(inv):
        it = _item(world, iid)
        if it is None or str(getattr(it, "name", "") or "") != str(order.item_name):
            continue
        if str(getattr(it, "rarity", "common") or "common") in ("epic", "legendary", "mythic") \
                and getattr(it, "identified", True) is False:
            continue
        out.append(idx)
    return out


def deliver_commission(world: Any, player: Any, order_id: str) -> tuple:
    """交付订单（须玩家在挂靠聚落当面）。返回 (ok, err/结算 dict)。"""
    from src.services import npc_reaction_engine as nre
    order = next((c for c in (getattr(world, "commissions", None) or [])
                  if str(getattr(c, "id", "") or "") == str(order_id)), None)
    if order is None:
        return False, "订单不存在或已下架"
    if str(getattr(player, "location_id", "") or "") != str(order.location_id):
        return False, "须到发布聚落当面交付"
    # [2026-08-28 用户定稿] 发布人须在当前场所（同地点不同场所不可当面交付；
    # issuer 不存在/无发布人豁免——订单板语义）。纯函数本地判定（同 quest_engine 口径）。
    issuer = next((n for n in (getattr(world, "npcs", None) or [])
                   if n.id == str(getattr(order, "issuer_npc_id", "") or "")), None)
    if issuer is not None and getattr(issuer, "alive", True):
        loc = next((l for l in (getattr(world, "locations", None) or [])
                    if l.id == getattr(player, "location_id", "")), None)
        if loc is not None and getattr(loc, "places", None):
            def _pid(o):
                pid = (getattr(o, "place_id", "") or "").strip()
                return pid or (getattr(loc, "default_place_id", "") or "").strip()
            if _pid(player) != _pid(issuer):
                pl_name = next((p.name for p in loc.places
                                if getattr(p, "id", "") == _pid(issuer)), None)
                return False, (f"发布人不在当前场所"
                               + (f"（在「{pl_name}」）" if pl_name else "") + "，找到本人当面交付")
    idxs = deliverable_indices(world, player, order)
    if len(idxs) < int(order.qty):
        return False, f"背包中可交付的「{order.item_name}」不足 {int(order.qty)} 件"
    inv = list(getattr(player, "inventory", None) or [])
    taken = [inv[i] for i in sorted(idxs, reverse=True)[:int(order.qty)]]
    for i in sorted(idxs, reverse=True)[:int(order.qty)]:
        inv.pop(i)
    player.inventory = inv
    payout = int(order.unit_price) * int(order.qty)
    player.gold = int(getattr(player, "gold", 0) or 0) + payout
    issuer = next((n for n in (getattr(world, "npcs", None) or [])
                   if str(getattr(n, "id", "") or "") == str(order.issuer_npc_id)), None)
    affinity_gain = 0
    if issuer is not None:
        issuer.affinity = max(0, min(100, int(getattr(issuer, "affinity", 0) or 0) + 3))
        affinity_gain = 3
        # [玩家印象 2026-09-06] 当面交付：发布人对玩家记「守信」
        try:
            from src.services import social_engine as _se
            _se.update_impression(issuer, "守信", trust_delta=4, firsthand=True)
        except Exception:
            pass
        try:
            nre.touch_friend_interact(world, issuer)
        except Exception:
            pass
        # 物品入发布人背包（商人随后可上架；战斗 NPC 自用；满则就地消费湮灭）
        ii = getattr(issuer, "inventory", None)
        if ii is None:
            ii = []
            issuer.inventory = ii
        for iid in taken:
            # [P45 2026-09-12] 容量引 npc_life_engine 单一来源（原散写 8：背包扩 16 后
            # 委托交付给发布人的收货口仍按 8 停——审核漏网点，本次补齐）
            if len(ii) < nle._NPC_INV_CAP and iid not in ii:
                ii.append(iid)
    rep = getattr(player, "reputation", None)
    if isinstance(rep, dict):
        fac = str(getattr(issuer, "faction_id", "") or "") if issuer else ""
        if fac:
            rep[fac] = max(-100, min(100, int(rep.get(fac, 0) or 0) + 1))
    # [P45 用户指示 2026-08-23] 委托交付养势力：发布人所属势力 power +1（小额高频，
    # 与任务领奖 QUEST_FACTION_POWER_GAIN=2 同族；_war_weight 按 power^2 加权胜算）
    faction_power_gain = 0
    faction_wealth_gain = 0
    if issuer is not None:
        fid = str(getattr(issuer, "faction_id", "") or "")
        fac_obj = next((f for f in getattr(world, "factions", []) if f.id == fid), None) \
            if fid else None
        if fac_obj is not None:
            old_p = int(getattr(fac_obj, "power", 0) or 0)
            fac_obj.power = max(0, min(100, old_p + COMMISSION_FACTION_POWER_GAIN))
            # [物价联动 2026-09-10 用户指示] 交付兼养财富 +1（委托货款本就进发布人
            # 背包，补给最自然；量级与 tick_economy 漂移同阶，钳 0-100）。
            old_w = int(getattr(fac_obj, "wealth", 0) or 0)
            fac_obj.wealth = max(0, min(100, old_w + 1))
            faction_power_gain = fac_obj.power - old_p
            faction_wealth_gain = fac_obj.wealth - old_w
            if faction_power_gain or faction_wealth_gain:
                from src.models.world import WorldEvent
                world.event_log.append(WorldEvent(
                    tick=int(getattr(world, "tick_count", 0) or 0), category="faction_war",
                    severity="trivial", title=f"声势渐涨：{fac_obj.name}",
                    desc=f"冒险者交付「{order.item_name}」x{order.qty}，{fac_obj.name}补给与"
                         f"府库充实（实力 {old_p} -> {fac_obj.power}，财富 {old_w} -> {fac_obj.wealth}）。",
                    factions=[fac_obj.id]))
    world.commissions = [c for c in world.commissions if c is not order]
    out = {"payout": payout, "qty": int(order.qty), "item": order.item_name,
           "issuer": (issuer.name if issuer else ""), "affinity_gain": affinity_gain}
    if faction_power_gain or faction_wealth_gain:
        out["faction_power"] = (f"{fac_obj.name}实力+{faction_power_gain}"
                                f" 财富+{faction_wealth_gain}")
    return True, out
