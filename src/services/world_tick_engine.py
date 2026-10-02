"""世界滴答确定性引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b，同 combat_engine）：
- LLM / _init_world_tick_state 给静态种子值（势力 power/wealth/aggressiveness、
  NPC is_key_npc/home_loc/alive、地点 owner_faction_id/stability）。
- 本模块跑动态推进：经济漂移 / 势力战 / 离屏杂兵 NPC / 阵亡重生 / 防漂移校验。
  全部纯 Python + SeededRng，LLM 不参与动态结果。同 world_id+tick+salt -> 同结果，
  可复现可单测（仿 combat_engine）。

调用方（WorldSimService.tick_world）为每个子阶段各自 seed_from(world.id, tick, salt)，
salt 区分 economy/faction_war/offscreen/cleanup/reconcile，互不串扰且各自可复现。

level（off/light/medium/heavy）语义：off=跳过、light=小幅少事件、medium=标准、heavy=大幅多事件。
lethality（off/light/medium/heavy）控势力战致杂兵死亡率（要角 is_key_npc 永不死亡）。
"""
from __future__ import annotations

from typing import Any

from src.models.world import World, WorldEvent
from src.services import npc_life_engine as nle
from src.services import quest_engine as _qe
from src.utils.rng import SeededRng


# ---- 强度系数表 ----
# (wealth 漂移幅度上限, 触发经济事件概率)
_ECONOMY_DRIFT = {
    "off": (0, 0.0),
    "light": (3, 0.08),
    "medium": (6, 0.18),
    "heavy": (12, 0.32),
}
# 势力战冲突触发概率倍率（作用于「敌对关系对」的基础冲突率）
# [P45 用户指示 2026-08-23] 整体调频 + 拉开档差：旧 light=medium=0.5（light 形同虚设），
# 默认 medium 下 -60 敌意对每 tick ~24% 开战、冷却仅 4 -> r4b 实测 18 tick 9 场战 3 夺城，
# 平均每 2 tick 一报，叠加 P45(1) 夺城 -25 稳定度会让世界长期物资短缺。新表等效频率
# 约为旧默认 40%（medium 0.4 + 冷却 8），light 才真「低频」，heavy 保留戏剧性。
_FACTION_WAR_RATE = {
    "off": 0.0,
    "light": 0.15,
    "medium": 0.4,
    "heavy": 1.2,
}
# [P] 同对势力开战后冷却 tick 数（防每回合刷战报 + 一胜雪球）。[P45] 4 -> 8：
# 同对势力 至少隔 8 tick 再战，多对敌对轮换下全局战报密度显著下降。
_FACTION_WAR_COOLDOWN = 8
# [P] 势力崩盘地板：任一势力 power <= 此值暂停对其开战（防把势力打成零领地空壳）。
_FACTION_COLLAPSE_FLOOR = 15


def _war_weight(power: int, aggressiveness: int) -> float:
    """[P] 势力战胜方权重：power^2 * 好战度加成（0.3~1.0）。

    好战方（aggressiveness 高）有战斗加成，防守方（低好战）难碾压强者——
    旧 power^1.5 纯实力加权下，弱防守镇也能靠随机连赢把强好战山贼打崩。
    """
    p = max(1, int(power))
    a = max(0, min(100, int(aggressiveness)))
    return (p ** 2) * (0.3 + 0.7 * a / 100)
# 致命度 -> 杂兵死亡率（仅在势力战冲突中作用于涉事地点的杂兵）
_LETHALITY_DEATH = {
    "off": 0.0,
    "light": 0.08,
    "medium": 0.2,
    "heavy": 0.38,
}
# 杂兵离屏移动概率
_MOB_MOVE_CHANCE = 0.3
# 杂兵离屏产生传闻概率
_RUMOR_CHANCE = 0.12
# 重生延迟（tick 数）默认值，若 NPC.respawn_at_tick 未由战斗显式设定则用此
_DEFAULT_RESPAWN_DELAY = 8


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _ev(category: str, severity: str, tick: int, title: str, desc: str,
        factions=None, locations=None, npcs=None) -> WorldEvent:
    """快捷构造 WorldEvent。"""
    return WorldEvent(
        tick=tick, category=category, severity=severity, title=title, desc=desc,
        factions=list(factions or []), locations=list(locations or []), npcs=list(npcs or []),
    )


# ---------------------------------------------------------------------------
# 势力实力聚合（供 reconcile 防漂移基准）
# ---------------------------------------------------------------------------
def compute_faction_power(world: World) -> dict[str, int]:
    """据结构事实（领地数 + 成员均等级）算各势力的「应有实力」快照，供 reconcile 校准。

    返回 {faction_id: power 0-100}。这是势力「客观实力」的估计值，
    与 Faction.power（可能因战事/经济漂移而偏离）做温和回归，防止长期漂移。
    """
    # npc 等级按 faction 聚合
    fac_levels: dict[str, list[int]] = {}
    fac_locs: dict[str, int] = {}
    for f in world.factions:
        fac_levels[f.id] = []
        fac_locs[f.id] = 0
    for npc in world.npcs:
        if not getattr(npc, "alive", True):
            continue  # permadeath 下死者不再抬高结构实力回归基准
        if npc.faction_id and npc.faction_id in fac_levels:
            fac_levels[npc.faction_id].append(max(0, int(getattr(npc, "level", 0) or 0)))
    for loc in world.locations:
        owner = loc.owner_faction_id or loc.faction_id
        if owner in fac_locs:
            fac_locs[owner] = fac_locs.get(owner, 0) + 1

    out: dict[str, int] = {}
    for f in world.factions:
        levels = fac_levels.get(f.id, [])
        mean_lvl = (sum(levels) / len(levels)) if levels else 0
        loc_count = fac_locs.get(f.id, 0)
        # 基础 40 + 成员均等级*3 + 领地*6，clamp 0-100
        power = _clamp(int(40 + mean_lvl * 3 + loc_count * 6), 0, 100)
        out[f.id] = power
    return out


# ---------------------------------------------------------------------------
# 经济滴答
# ---------------------------------------------------------------------------
def tick_economy(world: World, rng: SeededRng, level: str, tick: int) -> list[WorldEvent]:
    """各势力财富据 level 漂移；触底/触顶产出经济事件。原地改 world，返回事件列表。"""
    events: list[WorldEvent] = []
    amp, event_p = _ECONOMY_DRIFT.get(level, _ECONOMY_DRIFT["medium"])
    if amp <= 0:
        return events
    for f in world.factions:
        before = _clamp(f.wealth, 0, 100)
        delta = rng.roll(-amp, amp)
        after = _clamp(before + delta, 0, 100)
        f.wealth = after
        # 经济极端事件（破产/暴富）
        if rng.chance(event_p):
            if after <= 10:
                sev = "major" if after <= 0 else "minor"
                events.append(_ev("economy", sev, tick,
                                  f"{f.name}财政告急", f"{f.name}近期入不敷出，财富跌至 {after}。",
                                  factions=[f.id]))
            elif after >= 90:
                events.append(_ev("economy", "minor", tick,
                                  f"{f.name}商路兴旺", f"{f.name}财源广进，财富升至 {after}。",
                                  factions=[f.id]))
    return events


# ---------------------------------------------------------------------------
# 势力战
# ---------------------------------------------------------------------------
def tick_faction_war(world: World, rng: SeededRng, level: str, lethality: str,
                     tick: int, player_loc_id: str = "") -> list[WorldEvent]:
    """敌对势力对据关系/好战度 roll 冲突；胜方夺领地 + 实力增减；致命度控杂兵死亡。

    要角 NPC（is_key_npc=True）永不死于势力战（只杂兵伤亡）。
    [!] 玩家当前地点的 NPC 不卷入伤亡：站在玩家身边的同伴/在场 NPC 被一纸战报
    隐形击杀会改写玩家可见现场（守「世界滴答排除玩家地点 NPC」铁律）。
    """
    events: list[WorldEvent] = []
    rate_mult = _FACTION_WAR_RATE.get(level, 0.0)
    if rate_mult <= 0.0:
        return events
    death_p = _LETHALITY_DEATH.get(lethality, _LETHALITY_DEATH["medium"])

    loc_by_id = {l.id: l for l in world.locations}
    # 各势力控制的地点列表（动态归属）
    owned: dict[str, list[str]] = {f.id: [] for f in world.factions}
    for loc in world.locations:
        owner = loc.owner_faction_id or loc.faction_id
        if owner in owned:
            owned[owner].append(loc.id)

    checked: set[tuple[str, str]] = set()
    for fa in world.factions:
        for fb in world.factions:
            if fa.id == fb.id:
                continue
            key = tuple(sorted((fa.id, fb.id)))
            if key in checked:
                continue
            checked.add(key)
            # [P] 冷却：同对势力刚打过则跳过（world.faction_war_last_tick 水位）。
            # last_war=0 表示从未交战，不冷却（首战不受限）。
            cd_key = "|".join(sorted((fa.id, fb.id)))
            last_war = int((world.faction_war_last_tick or {}).get(cd_key, 0) or 0)
            if last_war and tick - last_war < _FACTION_WAR_COOLDOWN:
                continue
            # [P] 崩盘地板：任一势力已是空壳则不再对其开战（防打成零领地空壳）。
            if fa.power <= _FACTION_COLLAPSE_FLOOR or fb.power <= _FACTION_COLLAPSE_FLOOR:
                continue
            rel_a = fa.relations.get(fb.id, 0)
            rel_b = fb.relations.get(fa.id, 0)
            rel = (rel_a + rel_b) / 2.0
            # [P 验收] 中立/盟友（双边平均 rel >= 0）不开战：只有明确敌对才可能起冲突。
            # 世界书里「合作/同盟/互为表里」的派系经 gen relations 落地为正关系，
            # 此处直接拦下防盟友互打（OOC）；好战度只放大大敌意，不凭空制造战争。
            if rel >= 0:
                continue
            # 基础冲突率：关系越差越高，叠加双方好战度
            hostility = min(1.0, -rel / 100.0)          # 0~1
            aggr = (fa.aggressiveness + fb.aggressiveness) / 200.0  # 0~1
            base = hostility * 0.6 + aggr * 0.2         # 关系主导 + 好战加成
            chance = base * rate_mult
            if not rng.chance(chance):
                continue
            # 开战：胜方据 power^2 * 好战度加成加权（[P] 好战方有战斗加成，防守方难碾压强者）。
            wa = _war_weight(fa.power, fa.aggressiveness)
            wb = _war_weight(fb.power, fb.aggressiveness)
            winner, loser = (fa, fb) if rng.weighted([fa.id, fb.id], [wa, wb]) == fa.id else (fb, fa)
            # [P] 记冷却水位（开战即记，无论胜负）
            world.faction_war_last_tick = world.faction_war_last_tick or {}
            world.faction_war_last_tick[cd_key] = tick
            # 夺取败方一处领地
            loser_locs = owned.get(loser.id, [])
            stolen: list[str] = []
            if loser_locs:
                target_id = rng.pick(loser_locs)
                if target_id and target_id in loc_by_id:
                    tloc = loc_by_id[target_id]
                    tloc.owner_faction_id = winner.id
                    # [P45(1)] 夺城压低民生稳定度（-25 钳 0）——战乱地物资短缺的源头：
                    # 商店发料分档（tre.stability_supply_level）+ 深短缺系统货架衰减；
                    # 滴答规则检查每 tick +1 向 50 回归（战乱缓步恢复）。
                    tloc.stability = _clamp(int(tloc.stability) - 25, 0, 100)
                    stolen.append(target_id)
                    owned[loser.id] = [i for i in loser_locs if i != target_id]
                    owned.setdefault(winner.id, []).append(target_id)
                    # [!] 同步 Faction.territory（owner_faction_id 的真相源），
                    # 否则势力段 UI 的「领地数」与实际归属漂移。
                    if target_id in loser.territory:
                        loser.territory = [i for i in loser.territory if i != target_id]
                    if target_id not in winner.territory:
                        winner.territory.append(target_id)
            # 实力增减
            w_gain = rng.roll(3, 6)
            l_loss = rng.roll(3, 6)
            winner.power = _clamp(winner.power + w_gain, 0, 100)
            loser.power = _clamp(loser.power - l_loss, 0, 100)
            # 关系恶化
            cur = winner.relations.get(loser.id, 0)
            winner.relations[loser.id] = _clamp(cur - rng.roll(5, 15), -100, 100)
            loser.relations[winner.id] = _clamp(loser.relations.get(winner.id, 0) - rng.roll(5, 15), -100, 100)
            # 杂兵伤亡：涉事地点的杂兵（非要角 + combat_role 标注的战斗单位）。
            # [C6 修复 2026-08-25] 原 hp_max>0 过滤在 P37 平民基线后误伤平民（商人/平民
            # 与杂兵同列阵亡，商店随之停摆）——现按 P44「战斗身份唯一来源」只豁免 none。
            # 非法/漏标 combat_role 一律平民（同 _init_combat_stats 口径）。
            casualties: list[str] = []
            if death_p > 0 and stolen:
                for lid in stolen:
                    for npc in world.npcs:
                        if npc.location_id == lid and npc.alive and not npc.is_key_npc \
                                and str(getattr(npc, "combat_role", "") or "") in ("hostile", "friendly", "boss") \
                                and npc.location_id != player_loc_id:
                            if rng.chance(death_p):
                                # [P45 v3.1 审核修复] 势力战是第 4 个死亡点：主线发布人
                                # 豁免（hp=1 重伤，不死）+ 阵亡清理名下任务（否则
                                # giver_present 恒 False -> 任务既不能接也不能交，永久卡死）
                                if _qe.is_main_giver(world, npc):
                                    npc.hp = 1
                                    continue
                                _mark_npc_dead(npc, tick, rng)
                                _qe.cleanup_dead_giver_quests(world, npc)
                                casualties.append(npc.id)
            sev = "crisis" if (stolen and death_p > 0.3) else "major"
            desc_parts = [f"{winner.name} 击败 {loser.name}，实力此消彼长"]
            if stolen:
                names = [loc_by_id[i].name for i in stolen if i in loc_by_id]
                desc_parts.append(f"{winner.name} 夺取了「{'、'.join(names)}」")
                # [P45(1)] 民生后果入事件流（股市 faction_pressure 读「夺取了」识别易主，
                # 本句不动该关键词；短缺读数走商店横幅/发料分档）
                desc_parts.append(f"「{'、'.join(names)}」民生动荡、物资吃紧")
            if casualties:
                desc_parts.append(f"交战波及约 {len(casualties)} 名杂兵阵亡")
            # [势力战财富结算 2026-09-10 用户指示] 胜方缴获 +2~4 / 败方军费 -3~5（钳
            # 0-100；量级与 tick_economy 漂移同阶）——战争经济后果直接喂本地物价
            # （tre.wealth_price_factors）。插在 casualties 之后：本战 power/关系/伤亡
            # 的既有 rng 消费顺序不变（同 tick 多场战争时后续场的序列整体后移是接受
            # 的口径，与既有「插尾部」先例一致）。
            w_wealth = rng.roll(2, 4)
            l_wealth = rng.roll(3, 5)
            winner.wealth = _clamp(winner.wealth + w_wealth, 0, 100)
            loser.wealth = _clamp(loser.wealth - l_wealth, 0, 100)
            desc_parts.append(f"{winner.name}缴获充实军资，{loser.name}战费耗损府库")
            events.append(_ev("faction_war", sev, tick,
                              f"{winner.name} 与 {loser.name} 交战",
                              "。".join(desc_parts) + "。",
                              factions=[winner.id, loser.id], locations=stolen, npcs=casualties))
    return events


def _mark_npc_dead(npc: Any, tick: int, rng: SeededRng) -> None:
    """标记 NPC 阵亡 + 设重生 tick（默认延迟）。不删实体（叙事可描述尸体/搜刮）。"""
    npc.alive = False
    npc.hp = 0
    delay = rng.roll(_DEFAULT_RESPAWN_DELAY, _DEFAULT_RESPAWN_DELAY + 6)
    npc.respawn_at_tick = tick + delay


# ---------------------------------------------------------------------------
# 离屏杂兵 NPC
# ---------------------------------------------------------------------------
def tick_offscreen_npcs(world: World, rng: SeededRng, on: bool,
                        player_loc_id: str, tick: int) -> list[WorldEvent]:
    """推进玩家视线外地点的杂兵 NPC（非要角）：移动/回家/做日常，偶发传闻。

    要角 NPC（is_key_npc=True）不在此处理（由 service 层走 LLM 决策）。
    玩家当前地点的 NPC 不动（保持玩家可见现场稳定）。
    [P12] NPC 自主生活（npc_autonomous_enabled，overlay 门控缺省开）：
    离屏 NPC 会采集/采购更新背包。[P45 2026-09-12] 战斗 NPC 升级改走
    npc_life_engine 的打猎结算（do_hunt/daily_passive_encounters），此处不再被动抬级。
    """
    events: list[WorldEvent] = []
    if not on:
        return events
    autonomous = bool((getattr(world, "config_overlay", None) or {})
                      .get("npc_autonomous_enabled", True))
    loc_by_id = {l.id: l for l in world.locations}
    for npc in world.npcs:
        if not npc.alive or npc.is_key_npc:
            continue
        if not npc.location_id or npc.location_id == player_loc_id:
            continue
        cur = loc_by_id.get(npc.location_id)
        if cur is None:
            continue
        # [P39c] 作息移动（固定日夜规律；玩家所在地点不动守既有不变量）：
        # 夜晚 -> 有家者回家（市集冷清、宅所安歇）；清晨/白昼/黄昏 -> 商人回自家店铺看店
        # [2026-09-10 用户指示] 只有夜晚算打烊/归巢，清晨照常营业不再回家。
        phase = str(getattr(world, "time_phase", "day") or "day")
        if phase == "night":
            home = str(getattr(npc, "home_location_id", "") or "")
            if home and home in loc_by_id and home != npc.location_id:
                # [P27] 跨地点移动须重置 place_id 到目标默认场所——留旧地点场所 id 会让
                # 回家 NPC 从所有场所级在场列表消失一夜（_npcs_at_place 按.place_id 严格匹配）
                home_loc = loc_by_id[home]
                _move_npc(npc, npc.location_id, home, loc_by_id,
                          to_place_id=getattr(home_loc, "default_place_id", "") or "")
                continue
        elif phase in ("dawn", "day", "dusk") and (getattr(npc, "is_merchant", False) or getattr(npc, "shop_id", "")):
            shop = next((s for s in (getattr(world, "shops", None) or [])
                         if getattr(s, "id", "") == str(getattr(npc, "shop_id", "") or "")), None)
            if shop is not None:
                work = str(getattr(shop, "location_id", "") or "")
                if work and work in loc_by_id and work != npc.location_id:
                    work_loc = loc_by_id[work]
                    _move_npc(npc, npc.location_id, work, loc_by_id,
                              to_place_id=getattr(work_loc, "default_place_id", "") or "")
                    continue
        if autonomous and rng.chance(0.15):
            lived = _npc_live_a_bit(npc, cur, world, rng, tick)
            if lived is not None:
                events.append(lived)
            continue
        # 移动判定
        if rng.chance(_MOB_MOVE_CHANCE):
            # [P27] 场所化地点：有概率在地点内场所间移动（不跨地点），丰富在场动态
            places = getattr(cur, "places", None) or []
            if len(places) >= 2 and rng.chance(0.4):
                # 场所内随机移动（排除当前场所）
                cur_pid = (getattr(npc, "place_id", "") or "").strip() \
                    or (getattr(cur, "default_place_id", "") or "")
                other_places = [p for p in places if getattr(p, "id", "") and p.id != cur_pid]
                if other_places:
                    tgt_place = rng.pick(other_places)
                    if tgt_place is not None:
                        _move_npc_within_place(npc, cur, tgt_place)
                        continue
            # 优先回家，否则随机相邻合法地点
            home = npc.home_location_id or npc.location_id
            adj = [i for i in cur.connections if i in loc_by_id and i != cur.id]
            targets = ([home] if home and home in loc_by_id else []) + adj
            ambition = getattr(npc, "ambition", None) or {}
            if isinstance(ambition, dict) and ambition.get("kind") == "explore":
                unexplored = [i for i in adj
                              if i not in (getattr(npc, "visited_locations", None) or [])]
                if unexplored:
                    targets = sorted(unexplored)  # 游历心愿优先去未到过的相邻地点
            if not targets:
                continue
            tgt_id = rng.pick(targets)
            if tgt_id and tgt_id in loc_by_id and tgt_id != npc.location_id:
                tgt_loc = loc_by_id[tgt_id]
                _move_npc(npc, npc.location_id, tgt_id, loc_by_id,
                          to_place_id=getattr(tgt_loc, "default_place_id", "") or "")
                # 偶发传闻
                if rng.chance(_RUMOR_CHANCE):
                    tgt_name = loc_by_id[tgt_id].name
                    events.append(_ev("event", "trivial", tick,
                                      f"传闻：{npc.name}的动向",
                                      f"有人提到 {npc.name} 最近似乎去了「{tgt_name}」。",
                                      npcs=[npc.id], locations=[tgt_id]))
        else:
            # 设日常动作（叙事提示），据 role 取词
            npc.current_action = _flavor_action(npc, rng)
    return events


_MOB_ACTIONS = ("巡视周围", "与同伴闲谈", "整理物资", "在原地歇息", "注意着来往的人", "处理杂务")


def _npc_live_a_bit(npc: Any, loc: Any, world: Any, rng: SeededRng, tick: int):
    """[P12] NPC 单次自主生活（确定性，无 LLM）：

    - 野外点：走真实资源点采集结算（丰富度、冷却、掉落同源）。
    - 聚落：从真实货架采购；无可买货物只更新当前行为。
    [P45 2026-09-12] 原「战斗 NPC 小概率被动升级」已删（用户定稿：只有狩猎过才涨经验，
    成长走 npc_life_engine.do_hunt / daily_passive_encounters，不再有免费午餐）。
    """
    inv = getattr(npc, "inventory", None)
    if inv is None:
        inv = []
        npc.inventory = inv
    if getattr(loc, "kind", "wilderness") == "wilderness":
        gathered = nle.do_gather(world, npc, rng)
        npc.current_action = "采集山货" if gathered is not None else "巡察资源地"
        if gathered is not None:
            return gathered
    else:
        bought = nle.do_shop_errand(world, npc)
        if bought is not None:
            return bought
        npc.current_action = rng.pick(("打听物价", "清点家用", "处理杂务"))
    # [P45 2026-09-12 用户指示] 被动升级已删（原每 tick 8% 概率抬级 + 自动加点）：
    # NPC 要涨经验必须真去打怪（P36a hunt 动作 / _tick_npc_life 每日被动遭遇
    # daily_passive_encounters 的 do_hunt）。禁止在此复活任何「无战斗成长」通道。
    return None


def _flavor_action(npc: Any, rng: SeededRng) -> str:
    return rng.pick(_MOB_ACTIONS) or "在原地歇息"


def _move_npc(npc: Any, from_id: str, to_id: str, loc_by_id: dict,
              to_place_id: str = "") -> None:
    """移动 NPC：更新 location_id + 两地点 npc_ids（引擎层用，与 service 层 _move_npc_safe 同口径）。

    [P27] to_place_id 非空时同步设 npc.place_id（跨地点移动重置场所）；空串不变。
    """
    npc.location_id = to_id
    # [野心 2026-09-06] 游历判定：到访地点去重记录
    if to_id and to_id not in (getattr(npc, "visited_locations", None) or []):
        _vl = list(getattr(npc, "visited_locations", None) or [])
        _vl.append(to_id)
        npc.visited_locations = _vl[:60]
    if to_place_id:
        npc.place_id = to_place_id
    frm = loc_by_id.get(from_id)
    to = loc_by_id.get(to_id)
    if frm is not None and npc.id in frm.npc_ids:
        frm.npc_ids = [i for i in frm.npc_ids if i != npc.id]
    if frm is not None:
        for place in (getattr(frm, "places", None) or []):
            if npc.id in (getattr(place, "npc_ids", None) or []):
                place.npc_ids = [i for i in place.npc_ids if i != npc.id]
    if to is not None and npc.id not in to.npc_ids:
        to.npc_ids.append(npc.id)
    if to is not None:
        dest_place = next((p for p in (getattr(to, "places", None) or [])
                           if p.id == (to_place_id or getattr(npc, "place_id", ""))), None)
        if dest_place is not None and npc.id not in dest_place.npc_ids:
            dest_place.npc_ids.append(npc.id)


def _move_npc_within_place(npc: Any, loc: Any, to_place: Any) -> None:
    """[P27] 场所内移动：改 npc.place_id + 两场所 npc_ids 双向登记（与 service 层 _move_to_place 同口径）。

    不改 location_id（场所内移动地点不变）。to_place 为 None 或同场所则不动。
    """
    if to_place is None or not loc:
        return
    old_pid = (getattr(npc, "place_id", "") or "").strip()
    if not old_pid:
        old_pid = (getattr(loc, "default_place_id", "") or "").strip()
    if old_pid == getattr(to_place, "id", ""):
        return
    npc.place_id = to_place.id
    places = getattr(loc, "places", None) or []
    for p in places:
        pid = getattr(p, "id", "")
        if pid and pid == old_pid and npc.id in getattr(p, "npc_ids", []):
            p.npc_ids = [i for i in p.npc_ids if i != npc.id]
    if npc.id not in getattr(to_place, "npc_ids", []):
        to_place.npc_ids.append(npc.id)


# ---------------------------------------------------------------------------
# 阵亡清理与重生
# ---------------------------------------------------------------------------
def cleanup_and_respawn(world: World, rng: SeededRng, tick: int,
                        permadeath: bool = False) -> list[WorldEvent]:
    """处理阵亡 NPC 的重生（respawn_at_tick 到期）。玩家 HP=0 不在此处理（叙事承载）。
    [P36 用户指示] permadeath=True 时无人重生（respawn_at_tick 全部悬置不消费——含杂兵/
    商人，商店随之永久停摆；死亡路径照常写 respawn_at_tick，仅此处消费门控）。"""
    events: list[WorldEvent] = []
    if permadeath:
        return events
    loc_by_id = {l.id: l for l in world.locations}
    for npc in world.npcs:
        if npc.alive:
            continue
        rt = int(getattr(npc, "respawn_at_tick", 0) or 0)
        if rt <= 0:
            continue  # 不重生的永久阵亡（叙事/搜刮对象）
        if tick < rt:
            continue
        # 重生
        npc.alive = True
        npc.hp = int(getattr(npc, "hp_max", 0) or 0)
        try:
            npc.mp = int(getattr(npc, "mp_max", 0) or 0)  # 施法型 NPC 勿带空蓝复活
        except (AttributeError, TypeError):
            pass
        # 重生地点：respawn_location_id > home > 原 location
        respawn_loc = npc.respawn_location_id or npc.home_location_id or npc.location_id
        if respawn_loc and respawn_loc in loc_by_id and respawn_loc != npc.location_id:
            tgt_loc = loc_by_id[respawn_loc]
            _move_npc(npc, npc.location_id, respawn_loc, loc_by_id,
                      to_place_id=getattr(tgt_loc, "default_place_id", "") or "")
        elif respawn_loc and respawn_loc in loc_by_id:
            # 同地点重生，确保在 npc_ids 里
            loc_by_id[respawn_loc].npc_ids = list(dict.fromkeys(
                loc_by_id[respawn_loc].npc_ids + [npc.id]))
            # [P27] 同地点重生：place_id 重置为默认场所
            npc.place_id = getattr(loc_by_id[respawn_loc], "default_place_id", "") or ""
        npc.respawn_at_tick = 0
        events.append(_ev("npc", "minor", tick,
                          f"{npc.name}重新出现",
                          f"{npc.name} 不知从何处再次现身。",
                          npcs=[npc.id], locations=[npc.location_id]))
    return events


# ---------------------------------------------------------------------------
# 防漂移校验（纯规则兜底，LLM 校准是另一层）
# ---------------------------------------------------------------------------
def reconcile_world(world: World, rng: SeededRng) -> list[str]:
    """纯规则防漂移：钳制数值、去重无效 id、归属回退。返回修正项列表（供 LLM 校准参考）。

    温和地把 faction.power 向结构实力回归 10%（compute_faction_power），防长期漂移。
    """
    fixes: list[str] = []
    valid_loc_ids = {l.id for l in world.locations}
    valid_npc_ids = {n.id for n in world.npcs}
    valid_fac_ids = {f.id for f in world.factions}
    structural = compute_faction_power(world)

    # [!] territory 重同步：以 Location.owner_faction_id（真相源）为准重建各 faction.territory。
    # 夺地/归属变更多处发生，靠 reconcile 统一兜底防 Faction.territory 与实际归属漂移
    # （否则势力段 UI「领地数」与地图实际控制不符）。
    owned_by_fac: dict[str, list[str]] = {f.id: [] for f in world.factions}
    for loc in world.locations:
        owner = loc.owner_faction_id or loc.faction_id
        if owner in owned_by_fac:
            owned_by_fac[owner].append(loc.id)
    for f in world.factions:
        truth = owned_by_fac.get(f.id, [])
        if set(truth) != set(f.territory):
            if sorted(truth) != sorted(f.territory):
                fixes.append(f"{f.name}.territory 重同步为实际归属（{len(truth)}处）")
            f.territory = list(dict.fromkeys(truth))

    for f in world.factions:
        # 数值钳制
        for attr in ("power", "wealth", "aggressiveness"):
            # [修 2026-09-10] 0 是合法值（破产/无战意档），勿 `or 50` 吞：None 才是缺省。
            # 原写法 old 读成 50 后 new==old 不回写，当前侥幸无害，但任何人后续拿 old 参与
            # 回归/写入就会把档位 0 静默抬成 50（守 §11 防 0 吞）。
            _raw = getattr(f, attr, None)
            old = int(_raw) if _raw is not None else 50
            new = _clamp(old, 0, 100)
            if new != old:
                setattr(f, attr, new)
                fixes.append(f"{f.name}.{attr} 钳制 {old}->{new}")
        # power 向结构实力温和回归 10%
        target = structural.get(f.id, f.power)
        if abs(target - f.power) >= 5:
            blended = _clamp(int(f.power + (target - f.power) * 0.1), 0, 100)
            if blended != f.power:
                fixes.append(f"{f.name}.power 回归 {f.power}->{blended}")
                f.power = blended
        # territory 去重 + 有效
        before_t = list(f.territory)
        f.territory = list(dict.fromkeys(i for i in f.territory if i in valid_loc_ids))
        if len(f.territory) != len(before_t):
            fixes.append(f"{f.name}.territory 去重/清理")
        # members 去重 + 有效
        before_m = list(f.members)
        f.members = list(dict.fromkeys(i for i in f.members if i in valid_npc_ids))
        if len(f.members) != len(before_m):
            fixes.append(f"{f.name}.members 去重/清理")
        # relations 钳制 + 有效对端
        bad = []
        for oid, val in list(f.relations.items()):
            if oid not in valid_fac_ids or oid == f.id:
                bad.append(oid)
                f.relations.pop(oid, None)
                continue
            f.relations[oid] = _clamp(val, -100, 100)
        if bad:
            fixes.append(f"{f.name}.relations 清理 {len(bad)} 项无效对端")

    for loc in world.locations:
        # owner 失效回退 faction_id
        owner = loc.owner_faction_id
        if owner and owner not in valid_fac_ids:
            loc.owner_faction_id = loc.faction_id
            fixes.append(f"{loc.name}.owner_faction_id 失效回退")
        elif not owner:
            loc.owner_faction_id = loc.faction_id
        loc.stability = _clamp(loc.stability, 0, 100)
        # [P45(1)] 民生自发恢复：稳定度低于 50 每 tick +1 向中性回归（夺城 -25 之外，
        # build 时 danger 反推的低基线也缓慢爬升——稳定度此后由战乱/恢复动态驱动）
        if loc.stability < 50:
            loc.stability += 1
        # connections/npc_ids 去重 + 有效
        loc.connections = list(dict.fromkeys(i for i in loc.connections if i in valid_loc_ids and i != loc.id))
        loc.npc_ids = list(dict.fromkeys(i for i in loc.npc_ids if i in valid_npc_ids))

    # 玩家 reputation 钳制 + 有效对端
    rep = world.player.reputation
    bad_rep = []
    for fid, val in list(rep.items()):
        if fid not in valid_fac_ids:
            bad_rep.append(fid)
            rep.pop(fid, None)
            continue
        rep[fid] = _clamp(val, -100, 100)
    if bad_rep:
        fixes.append(f"player.reputation 清理 {len(bad_rep)} 项无效势力")

    return fixes


# ============ [P20] 市井传闻（B3：离屏世界氛围，确定性纯 Python，无 LLM） ============
# 与玩家无关的世界背景小事件，每 tick 按 SeededRng 产出 0-2 条 trivial 传闻直入 event_log，
# 经【近期世界动态】作旁白背景素材（「隐约听说…」），落实「活在世界中」的离屏氛围感。
# 题材按 config_overlay.attribute_template_id 选池，缺键回退西幻（守 §23）。{loc} 占位符
# 由引擎填随机已发现地点名（让传闻有地理落点）；无地点则自动改选无占位符模板。
# 容量守 §23 数据量铁律下限（tests/test_genre_data_volume.py 守护 >=10/题材）。
_GENRE_RUMOR_TEMPLATES: dict[str, list[str]] = {
    "wuxia": [
        "{loc}的粮价这几日涨了三成，赶集的乡民议论纷纷。",
        "城南来了个杂耍班子，连演三日，看客把街口围得水泄不通。",
        "听说镖局的镖车在官道上被人劫了，货主正四处打听消息。",
        "茶馆里的说书先生新开了段书，讲的是前朝剑仙旧事。",
        "比武招亲的告示贴到了城门口，江湖上的好手都在摩拳擦掌。",
        "城东药铺的坐堂大夫告假回乡，抓药的人排到了街尾。",
        "昨夜有夜行人在屋脊上飞檐走壁，更夫只当自己眼花了。",
        "据说有人在{loc}挖出一块古碑，字迹无人识得。",
        "两家武馆为了争徒弟闹翻了脸，今日在街头对峙。",
        "江洋大盗的悬赏告示换了新的，赏银比上月翻了一倍。",
        "戏班的名角儿嗓子哑了，今晚的场子换了个生面孔。",
        "{loc}的客栈住进了一队带刀客商，掌柜的一夜没敢合眼。",
        "城南戏楼的武生和打行起了冲突，茶客们看了一场热闹。",
        "{loc}的当铺收了一批来路不明的古玩，掌柜讳莫如深。",
        "有异乡客在集市摆摊卖艺，口音听着不像本地人。",
    ],
    "xianxia": [
        "有修士在{loc}上空御剑而过，凡人只道是天边流星。",
        "城南的丹炉半夜炸响，半条街都闻到了药香。",
        "听说北边山里有一处灵脉异动，各宗门的探子都出动了。",
        "仙门三年一度的开山收徒之期将近，求仙的人涌向山脚。",
        "{loc}集市上的灵石兑价又涨了，散修们叫苦不迭。",
        "有灵兽在深山出没的传闻甚嚣尘上，猎户都不敢进山。",
        "两位散修在城外斗法，雷火映红了半边天。",
        "千年灵药即将成熟的传言在坊市间流传，真假难辨。",
        "某处秘境现世的前兆被占星修士捕捉，消息被高价封存。",
        "炼气士云游至{loc}，为人开坛讲道，听者如云。",
        "有魔修踪迹出现在附近山岭，巡山弟子加强了戒备。",
        "山里的泉水一夜之间泛起灵光，汲水的人排成长队。",
        "有炼器师在坊市收残破法宝，说是能回炉重炼。",
        "夜里天边有异象，星落如雨，占星修士连夜观星。",
        "{loc}的灵田遭了妖兽践踏，佃农们愁眉不展。",
    ],
    "modern": [
        "{loc}新开了一家奶茶店，开业三天买一送一。",
        "地铁线路施工，几条主路早晚高峰堵得水泄不通。",
        "气象台发布暴雨预警，超市的伞卖断了货。",
        "网红餐厅被曝后厨卫生问题，门口冷清了不少。",
        "演唱会的票黄牛炒到天价，粉丝在体育馆外通宵排队。",
        "小区物业贴出停电检修通知，业主群炸开了锅。",
        "快递站点爆仓，取件的人从早排到晚。",
        "{loc}的夜市美食节开幕，摊贩们的叫卖声此起彼伏。",
        "有人中了彩票大奖，彩票店门口挂起了红横幅。",
        "共享单车的投放点被堆成了山，城管来了一趟又一趟。",
        "宠物店门口贴出寻猫启事，悬赏颇高。",
        "连锁健身房突然关门跑路，会员在门口讨说法。",
        "老街的旧书店要关门了，店主说房租又涨了。",
        "凌晨有外卖骑手在巷口被拦，警方正在调监控。",
        "{loc}新开的健身房搞促销，办卡送一年。",
    ],
    "scifi": [
        "星港的泊位费又涨了，货船主们联名抗议。",
        "{loc}的量子网络昨夜延迟骤升，工程师彻夜排查。",
        "一批仿生人被曝出固件缺陷，厂商紧急召回。",
        "废品站里翻出一具还能开机的旧义体，被人高价收走。",
        "黑市上新流出一批军用级芯片，治安队已经盯上。",
        "太空站提高了停靠费，走私船绕道而行。",
        "义体诊所周年庆，神经接口升级五折。",
        "有星盗在附近航路出没的传闻让商船改了航线。",
        "能源站输出不稳，{loc}的部分街区开始限电。",
        "新的殖民星开放移民申请，报名处排起长队。",
        "AI 气象模型预测今年将有罕见的离子风暴。",
        "悬浮车的充能桩开始收费，车主们骂声一片。",
        "轨道电梯例行检修，货运航道临时改道。",
        "{loc}的回收站发现了一批还能用的旧导航芯片。",
        "有太空地质学家预警小行星带的碎片逼近航线。",
    ],
    "apocalypse": [
        "变异兽群昨夜从{loc}外围迁徙而过，哨塔彻夜鸣警。",
        "幸存者营地的易货集市开张，药品成了硬通货。",
        "辐射雨警报拉响，家家户户封死门窗。",
        "罐头和净水片的价格翻了一倍，交易站前排起长队。",
        "有搜寻队在废墟里发现了未污染的水源，消息传得飞快。",
        "一支外出的车队至今未归，营地里人心惶惶。",
        "无线电里传来断断续续的求救信号，方位不明。",
        "变异植物蔓延到了{loc}西侧的街区，通道被封锁。",
        "哨站急需抗生素，广播里一遍遍重复着求援消息。",
        "有人在旧世界的地窖里找到了一批封存的种子。",
        "夜里的怪声越来越近，值夜的人不敢合眼。",
        "营地的发电机坏了，机修工说零件要下周才凑得齐。",
        "有幸存者声称在废墟里听到教堂钟声，没人敢去查看。",
        "{loc}附近的水源检出轻度辐射，打水的人少了一半。",
        "营地里来了个卖地图的，说知道哪里有旧军火库。",
    ],
    "western_fantasy": [
        "一个吟游诗人路过{loc}，在广场上弹唱精灵古谣。",
        "佣兵团的招募帐篷在城门口支了起来，赏金猎人们跃跃欲试。",
        "炼金药剂的价格这周涨了三成，药剂师说原料告急。",
        "有旅人声称看见龙影掠过天际，酒馆里众说纷纭。",
        "北边的矿洞里传来了地下城入口现世的传闻。",
        "商队在路上遭了哥布林伏击，损失了三车货物。",
        "神殿筹备周年庆典，城里的彩旗挂满了街道。",
        "精灵使节的船队即将抵达港口，码头开始戒严。",
        "魔法学院发布招生告示，贵族和平民子弟挤满了报名处。",
        "{loc}的酒馆进了新一批黑麦酒，酒客们赞不绝口。",
        "森林里有白鹿现身的消息传开，猎人们被警告不得捕杀。",
        "钟楼的齿轮卡住了，修表匠爬上去鼓捣了一整天。",
        "城门口的告示栏新贴了一张通缉令，赏金颇高。",
        "有位老妇人沿街叫卖护身符，说能避灾祸。",
        "{loc}的铁匠铺接了笔大单，说是在给骑士团赶工。",
    ],
}


def tick_rumors(world: World, rng: SeededRng, tick: int) -> list[WorldEvent]:
    """[P20] 产出本 tick 市井传闻（0-2 条 trivial，确定性：同 world+tick+salt 同结果）。

    纯氛围事件：不改任何世界状态，category=event、severity=trivial，直入 event_log
    作【近期世界动态】背景素材（不走 max_events_per_tick 关键事件限额，见调用方）。
    """
    overlay = world.config_overlay if isinstance(world.config_overlay, dict) else {}
    tid = overlay.get("attribute_template_id") or "western_fantasy"
    pool = _GENRE_RUMOR_TEMPLATES.get(tid) or _GENRE_RUMOR_TEMPLATES["western_fantasy"]
    if not pool:
        return []
    loc_names = [l.name for l in world.locations if getattr(l, "discovered", True)]
    no_loc_pool = [t for t in pool if "{loc}" not in t]
    out: list[WorldEvent] = []
    count = 1 if rng.chance(0.5) else 0
    if count and rng.chance(0.2):
        count = 2
    for _ in range(count):
        tmpl = rng.pick(pool)
        if "{loc}" in tmpl:
            if loc_names:
                tmpl = tmpl.format(loc=rng.pick(loc_names))
            else:
                tmpl = rng.pick(no_loc_pool) if no_loc_pool else ""
        if not tmpl:
            continue
        out.append(WorldEvent(
            tick=tick, category="event", severity="trivial",
            title="市井传闻", desc=tmpl,
        ))
    return out


# ============ [P24c] 好友联动（带话/寄礼/交情衰减；确定性纯 Python，无 LLM）============
# 每 3 tick 由 tick_world 调一次（salt="friends"）；事件走 event_log ->
# 【近期世界动态】被旁白自然提及。来访（好友移动到玩家所在地）v1 不做
#（与 [NPC] 命令行协议/在场同步交互复杂，路线图明确延后）。
#
# 单次 roll 分流（每好友每轮一次 rng.random）：<0.10 带话 / <0.16 寄礼 / 其余无事。
# 交情衰减独立于 roll（水位线法：每满 30 天未互动 -2，钳下限 50——好友身份在
# friend_npc_ids 不掉，只是「降温」；衰减只推进水位线不算互动）。
_GENRE_FRIEND_MESSAGE_TEMPLATES: dict[str, list[str]] = {
    "western_fantasy": [
        "他说山道的雪化了，商队又通了，盼你路过时进堡喝一杯。",
        "她惦记你上回受的伤，嘱咐你少逞英雄。",
        "酒馆新酿的黑麦酒开封了，给你留了一桶。",
        "北边林子有狼群出没，他提醒你夜里赶路当心。",
        "小侄儿学会了你教的把戏，天天念叨着你。",
        "她说院子里的苹果熟了，再不回来就只剩落果了。",
        "他托人捎话：欠你的那顿饭，一直记着呢。",
        "神殿的祭典快到了，她说想和你一起去看看。",
        "猎场围猎要开始了，他说给你备了副好弓。",
        "她晒了些野莓干，甜得很，给你留了一罐。",
        "河对岸的渡口新修了桥，他让你回来少绕路。",
        "他说梦见你回来了，醒来却只有风声。",
    ],
    "xianxia": [
        "道友说他闭关有所得，盼你再来论道一场。",
        "山中灵茶新焙，她给你留了一匣，说等你来取。",
        "宗门大比将至，他念着你若在，必要同去凑个热闹。",
        "她说洞府外的桃花开了，落英如雨，可惜你不在。",
        "坊市的灵石兑价又涨，他提醒你要换趁早。",
        "他炼丹炸了炉，没好意思声张，只盼你回来替他把把关。",
        "她抄了一卷功法注疏，说是给你留的，勿要转赠。",
        "长老又提起你，他怕你忘了，特意托人带话。",
        "他得了株罕见灵草，说等你回来一起炼。",
        "她说要闭关三月，怕你寻不见，提前捎个信。",
        "坊市新开了家灵食铺，他说你回来得去尝尝。",
        "长老又摆了一局残棋，说等你来解。",
    ],
    "wuxia": [
        "他说镖局接了大买卖，回来时绕道喝杯酒。",
        "她的伤好利索了，谢你当日出手，一直念着这份情。",
        "楼下说书的把你我编进了段子，他听了直乐。",
        "武馆新收了徒弟，他压不住场，盼你回来撑撑门面。",
        "她说巷口的桂花开了，酿的酒埋在老地方。",
        "官道上不太平，他嘱咐你夜里别赶路。",
        "他托人捎来一句话：那桩事有眉目了，见面细说。",
        "老掌柜念着你，说给你留了间上房。",
        "他说新得了一坛好酒，埋在老槐树下等你。",
        "她学着酿了你爱喝的梅子酒，说手艺不精。",
        "城东新开了间戏楼，他说你回来得去听场。",
        "他盘下了间小铺子，说等你回来挑件货。",
    ],
    "modern": [
        "她说新开的火锅店还不错，等你回来一起去。",
        "他升职了，说改天请客，就缺你一个。",
        "小区的猫又生了，她给你留了一只最乖的。",
        "他吐槽最近加班狠，羡慕你在外面的自由。",
        "家里一切都好，她说不用惦记，注意身体。",
        "球迷老友们约了球赛，他给你留了个位置。",
        "她说搬家了，新地址写在信封背面，得空来坐。",
        "他翻了翻老照片，说好久没见，怪想你的。",
        "她学会了做你爱吃的菜，说等你回来试菜。",
        "他说楼下新开了健身房，拉你回来一起办卡。",
        "老同学们又张罗聚会，他说就差你一个。",
        "她说手机里存着你的照片，想你时就翻翻。",
    ],
    "scifi": [
        "他说星港的泊位费又涨了，让你回来时提前订舱。",
        "她调去了新舱段，信号不好，托人捎句平安。",
        "旧飞船的零件淘到了，他修好了那盏灯，等你回来看。",
        "穹顶音乐会加场，她说给你留了两个位子。",
        "他提醒你义体该保养了，别等出毛病才想起。",
        "交易所的行情又疯了，他念叨你那批货该出手了。",
        "她说观测台最近的流星雨很美，可惜你不在。",
        "老兵们又聚了，他给你留了杯合成威士忌。",
        "他说观测塔新装了望远镜，等你回来一起看星。",
        "她配了新的神经接口，说速度快得离谱。",
        "黑市的芯片又降价了，他提醒你补货趁早。",
        "他说给你改装了把趁手的工具，就等你回来取。",
    ],
    "apocalypse": [
        "她说营地的收成不错，给你留了两罐存粮。",
        "哨塔修好了，他让你回来时看看新装的探照灯。",
        "电台里又听到你的传闻，她挺为你骄傲。",
        "他提醒你入冬前回来，别在外头硬扛。",
        "营地的新房分下来了，她说给你留了半间。",
        "他说净水器换了新滤芯，水是甜的，等你尝。",
        "孩子们的识字课开班了，她盼你回来讲讲外面的事。",
        "他托人带话：下个月的换防，别忘了回来看看。",
        "她说囤了些盐，够你和营地用好一阵。",
        "他修好了台收音机，说晚上能收到老歌。",
        "营地的孩子们做了把弹弓，说要送给你。",
        "他说在废墟里捡到本旧书，给你留着了。",
    ],
}

# 带话 / 寄礼概率（单次 roll 分流阈值）
_FRIEND_MSG_CHANCE = 0.10
_FRIEND_GIFT_CHANCE = 0.16          # roll 落在 [0.10, 0.16) 区间为寄礼
# 交情衰减：每满 30 天未互动 -2，钳下限 50（保好友身份只降温）。
_FRIEND_DECAY_DAYS = 30
_FRIEND_DECAY_STEP = 2
_FRIEND_DECAY_FLOOR = 50


def _pick_friend_gift(world: World, npc, rng: SeededRng):
    """挑好友寄礼：common-rare 非 key 物品，偏好命中 NPC 爱好（nre.hobby_match）
    或好友所在地的资源产出（「山里的朋友寄来山货」）；无偏好随机。确定性。"""
    from src.services import npc_reaction_engine as _nre
    candidates = [it for it in (getattr(world, "items", None) or [])
                  if getattr(it, "type", "") != "key"
                  and (getattr(it, "rarity", "common") or "common") in ("common", "uncommon", "rare")]
    if not candidates:
        return None
    preferred = [it for it in candidates if _nre.hobby_match(npc, it)]
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if l.id == getattr(npc, "location_id", "")), None)
    if loc is not None:
        local_ids = set()
        for rn in (getattr(loc, "resource_nodes", None) or []):
            for d in (getattr(rn, "drops", None) or []):
                if isinstance(d, dict) and d.get("item_id"):
                    local_ids.add(d["item_id"])
        local = [it for it in candidates if it.id in local_ids and it not in preferred]
        preferred = preferred + local
    pool = preferred or candidates
    return rng.pick(pool)


def tick_friends(world: World, rng: SeededRng, tick: int) -> list[WorldEvent]:
    """[P24c] 好友联动子阶段（每 3 tick 一次，确定性）：带话 / 寄礼 / 交情衰减。

    - 衰减先行（水位线法，幂等：同窗口不重复扣；last_interact_day=0 视作久远追补）。
    - 带话/寄礼单次 roll 分流互斥（一友一轮至多一件事，防事件刷屏）。
    - 寄礼直接入包走 try_add_to_inventory（守 §22 入包统一），背包满则本次作罢
      （不产事件不丢礼，下轮重roll）；入包同时记 codex_items。
    """
    events: list[WorldEvent] = []
    friend_ids = list(getattr(world.player, "friend_npc_ids", None) or [])
    if not friend_ids:
        return events
    overlay = world.config_overlay if isinstance(world.config_overlay, dict) else {}
    tid = overlay.get("attribute_template_id") or "western_fantasy"
    msg_pool = _GENRE_FRIEND_MESSAGE_TEMPLATES.get(tid) \
        or _GENRE_FRIEND_MESSAGE_TEMPLATES["western_fantasy"]
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    for fid in friend_ids:
        npc = next((n for n in (getattr(world, "npcs", None) or []) if n.id == fid), None)
        if npc is None or not getattr(npc, "alive", True):
            continue
        # ---- 交情衰减（每满 30 天 -2，钳 50；推进水位线不算互动）----
        # [!] last_interact_day=0（老档/未记录）视作水位线 0：久远追补（从第 31 天起算扣）。
        if int(getattr(npc, "affinity", 0) or 0) > _FRIEND_DECAY_FLOOR:
            while (day - int(npc.last_interact_day) >= _FRIEND_DECAY_DAYS
                   and int(npc.affinity) > _FRIEND_DECAY_FLOOR):
                npc.last_interact_day += _FRIEND_DECAY_DAYS
                npc.affinity = max(_FRIEND_DECAY_FLOOR, int(npc.affinity) - _FRIEND_DECAY_STEP)
                events.append(_ev(
                    "npc", "trivial", tick, f"许久未见{npc.name}",
                    f"许久未见，{npc.name}似有挂念，盼你得空去看看。",
                    npcs=[npc.id]))
        # ---- 带话 / 寄礼（单次 roll 分流，互斥）----
        # [P26a] sweetheart/spouse 离屏动态加权：在基础概率上叠加关系加成（更惦记玩家）
        from src.services import relation_engine as _re
        msg_bonus, gift_bonus = _re.friend_event_bonuses(world, npc)
        msg_chance = _FRIEND_MSG_CHANCE + msg_bonus
        gift_chance = _FRIEND_GIFT_CHANCE + msg_bonus + gift_bonus   # 保持互斥区间连续
        roll = rng.random()
        # [A3] 主动私聊概率（交情>=70 或恋人/配偶；同好友 >=2 天一条；cap 2 防堆积）。
        # 纯 roll 只标记 pending_friend_chats，LLM 生成在 svc._tick_friend_chats 阶段。
        chat_chance = gift_chance + (0.10 + msg_bonus if int(npc.affinity) >= 70 else 0.0)
        if roll < msg_chance:
            tmpl = rng.pick(msg_pool)
            if tmpl:
                events.append(_ev(
                    "npc", "trivial", tick, f"{npc.name}托人带话", tmpl, npcs=[npc.id]))
        elif roll < gift_chance:
            from src.services import combat_engine as _ce
            gift = _pick_friend_gift(world, npc, rng)
            if gift is not None and _ce.try_add_to_inventory(world.player, gift.id):
                if gift.id not in (getattr(world.player, "codex_items", None) or []):
                    world.player.codex_items.append(gift.id)
                events.append(_ev(
                    "npc", "minor", tick, f"收到{npc.name}寄来的包裹",
                    f"{npc.name}托人捎来一个包裹，里面是「{gift.name}」——"
                    "礼轻情重，朋友的挂念随物而至。",
                    npcs=[npc.id]))
        elif roll < chat_chance and int(npc.affinity) >= 70:
            pending = [x for x in (getattr(world, "pending_friend_chats", None) or []) if x]
            _last_pc = int(getattr(npc, "last_proactive_chat_day", 0) or 0)
            # last=0（从未主动私聊）首条不设间隔；否则 >=2 天一条
            if (npc.id not in pending and len(pending) < 2
                    and (_last_pc == 0 or day - _last_pc >= 2)):
                pending.append(npc.id)
                world.pending_friend_chats = pending
                npc.last_proactive_chat_day = day
    return events


def tick_dungeons(world: World) -> list[WorldEvent]:
    """[P25a] 秘境重生子阶段：封印到期（day_count >= cooldown_until_day）同 seed
    重铺（clears 已在通关时 +1，怪物难度随之抬升）。确定性纯 Python 无 LLM。"""
    from src.services import dungeon_engine as _dge
    try:
        return _dge.tick_regenerate(world)
    except Exception:
        return []
