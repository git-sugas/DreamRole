"""P47 后果层：把战斗失败与暴力行为的代价落到可读、可追索的实体状态上。

[系统互指]（守 read.md「新系统必须回答：消费谁的输出 / 产出喂给谁」）
- 消费：战斗结算结果（finish_combat 正式战斗 / _resolve_combat 快速战斗 两路径）、
  PlayerState.gold+inventory、NPC.wallet+inventory+stolen_ids、NPC.player_impression、
  NPC.social、world.items、调用方传入的 SeededRng（新 salt）。
- 产出：胜者钱包/背包（守恒转移——钱继续在世界经济里流通，不蒸发）、stolen_ids
  （保证夺回的清单）、目击者印象 trust（喂既有 merchant_trust 买价与新增拒卖闸）、
  叙事要点行（喂 narrate 据实描写）、战报栏字段（喂右侧「战报」页读数）。

[!] 零 LLM：本模块不产出任何需要 LLM 决定的数值、不发 HTTP；全部纯 Python +
    调用方传入的 SeededRng（独立 salt，不污染既有 rng 序列，守「确定性可回放」）。
[!] 不 import world_sim_service（防循环 import；测试可直接 import 本模块）。
[!] 币种一律 GenreText.currency（§23 币种显示名零硬编码，静态守护 test_currency_i18n）。

[设计口径 2026-09-25 用户拍板「第一批后果层」]
- 只夺 consumable/material：装备是构筑载体（夺装接近删档级惩罚，且会与
  materialize_unidentified_drops 的克隆口径打架）、key 是任务道具（夺走会让任务不可完成）。
- 野怪（临时单位，不入 world.npcs）只夺金：它没有 wallet/inventory 生命周期，夺物会凭空湮灭。
- fled（脱战）不夺：留一个「识时务」的出口——逃跑保财、硬拼可能全输，这是本批最便宜的真决策点。
- 夺回走 stolen_ids 通道而非既有搜刮：非敌对劫掠者的遗物不在 P8 搜刮门内（hostile 门控），
  且「夺回」的语义是拿回同一件（不参与稀有度升档/未鉴定克隆）。
"""
from __future__ import annotations

from src.services.social_engine import update_impression

# 败北被夺：钱包比例与单次上限（上限防后期一败清空 -> 挫败螺旋；后期损失的绝对值仍大）
GOLD_TAKE_PCT = 25
GOLD_TAKE_CAP = 500
# 野怪（临时单位）只夺金，且比例/上限更低
MONSTER_GOLD_PCT = 10
MONSTER_GOLD_CAP = 120
# 单次最多夺几件随身物
ITEM_TAKE_MAX = 2
# 记恨线：商人对玩家 trust <= 此值 -> 拒绝交易（既有买价两档在 0.95/1.1，本闸不动它们）
REFUSE_TRUST = -60
# 可被夺走的物品大类（不含装备/key，见模块 docstring 设计口径）
STEALABLE_TYPES = ("consumable", "material")
# stolen_ids 滚动上限（防无限增长；超出的旧条目视为已脱手）
_STOLEN_CAP = 8
# 目击击杀的印象扣减：被害者亲友更重
_GRUDGE_TRUST_CLOSE = -18
_GRUDGE_TRUST_OTHER = -8
# 「亲友」判定线（与 social_engine 的交情分档同源：>=25 好友）
_CLOSE_SOCIAL = 25


def _currency(world) -> str:
    """题材币种显示名（§23 零硬编码：西幻=金币/仙侠=灵石/武侠=银两/现代=元/科幻=信用点/末日=晶核）。"""
    from src.models.world_sim_preset import GenreText
    ov = getattr(world, "config_overlay", None)
    return GenreText(ov if isinstance(ov, dict) else {}).currency


def _item_name(world, item_id: str) -> str:
    """item_id -> 显示名（查不到回退 id 本身，不抛）。"""
    for i in (getattr(world, "items", None) or []):
        if getattr(i, "id", "") == item_id:
            return str(getattr(i, "name", "") or item_id)
    return str(item_id)


def took_from_player(world, winner_npc, rng, *, is_temp_monster: bool = False) -> dict:
    """败北结算：从玩家身上夺取金/物（守恒转移给胜者）。返回 {"gold", "items", "lines"}。

    [!] rng 由调用方以新 salt 构造（`toll_{npc.id}`），与既有 combat_loot_*/salvage_*/
        combat_gold_* 序列并列，不改任何既有序列。
    [!] 守恒：钱进胜者 wallet（野怪路径按 sink 处理，叙事写明去向）；物进胜者背包用
        直接 append——守 §22「只约束新获得」，helper 拒收会让物品凭空湮灭。
    [!] 抽物前先 sorted(pool) 再逐件 pick+remove：同 world+tick 同结果（可回放），且不重复抽同一件。
    """
    out: dict = {"gold": 0, "items": [], "lines": []}
    p = getattr(world, "player", None)
    if p is None:
        return out
    cur = _currency(world)
    winner_name = str(getattr(winner_npc, "name", "") or "") if winner_npc is not None else ""

    # ---- 夺金 ----
    have = max(0, _as_int(getattr(p, "gold", 0)))
    pct = MONSTER_GOLD_PCT if is_temp_monster else GOLD_TAKE_PCT
    cap = MONSTER_GOLD_CAP if is_temp_monster else GOLD_TAKE_CAP
    take = min(cap, have * pct // 100)
    if take > 0:
        p.gold = have - take
        out["gold"] = take
        if winner_npc is not None and not is_temp_monster:
            winner_npc.wallet = _as_int(getattr(winner_npc, "wallet", 0)) + take
            out["lines"].append(f"{winner_name}搜走了你身上的 {take} {cur}")
        else:
            # 野怪：钱散落当场（sink）。不写「被夺走」以免暗示能从它身上要回来。
            out["lines"].append(f"你的钱袋在混乱中散落，丢了 {take} {cur}")

    # ---- 夺物（仅真 NPC 胜者：野怪无背包生命周期）----
    if winner_npc is not None and not is_temp_monster:
        by_id = {getattr(i, "id", ""): i for i in (getattr(world, "items", None) or [])}
        inv = [str(i) for i in (getattr(p, "inventory", None) or [])]
        pool = sorted(iid for iid in inv
                      if getattr(by_id.get(iid), "type", "") in STEALABLE_TYPES)
        if pool:
            n = min(len(pool), ITEM_TAKE_MAX, max(1, rng.roll(1, ITEM_TAKE_MAX)))
            taken: list[str] = []
            for _ in range(n):
                if not pool:
                    break
                iid = str(rng.pick(pool))
                pool.remove(iid)
                if iid not in (getattr(p, "inventory", None) or []):
                    continue
                p.inventory.remove(iid)
                winner_npc.inventory = list(getattr(winner_npc, "inventory", None) or []) + [iid]
                stolen = [str(x) for x in (getattr(winner_npc, "stolen_ids", None) or [])]
                if iid not in stolen:
                    stolen.append(iid)
                winner_npc.stolen_ids = stolen[-_STOLEN_CAP:]
                taken.append(iid)
            if taken:
                out["items"] = taken
                names = "、".join(_item_name(world, i) for i in taken)
                out["lines"].append(f"{winner_name}还夺走了你的：{names}")
    return out


def _eff_place(world, loc_id: str, place_id: str) -> str:
    """有效场所（守 P27 口径）：place_id 为空时回退该地点的 default_place_id。

    与 commission_engine 的既有口径同源（`pid or loc.default_place_id`）。老档/NPC 未设
    place_id 时按空串直接比较会让「同场所」判定漏掉一半人。
    """
    if place_id:
        return place_id
    for loc in (getattr(world, "locations", None) or []):
        if getattr(loc, "id", "") == loc_id:
            return str(getattr(loc, "default_place_id", "") or "").strip()
    return ""


def _same_place(world, a, b) -> bool:
    """两个实体是否同场（守 P27，与 npc_reaction_engine._same_place 同口径）：
    地点相同 + 有效场所相同；**地点未场所化时地点级即同场**（旧档/野外点没有 places）。
    """
    la = str(getattr(a, "location_id", "") or "")
    lb = str(getattr(b, "location_id", "") or "")
    if not la or la != lb:
        return False
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if getattr(l, "id", "") == la), None)
    if loc is None or not getattr(loc, "places", None):
        return True                       # 无场所化地点：地点级即同场
    return (_eff_place(world, la, str(getattr(a, "place_id", "") or ""))
            == _eff_place(world, lb, str(getattr(b, "place_id", "") or "")))


def _as_int(v, default: int = 0) -> int:
    """脏值安全的 int 转换（存档被手改/旧档污染时不抛异常打断结算）。"""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


def reclaim_candidates(world, npc) -> list[str]:
    """列出「可夺回」的物品 id（**不改任何状态**）：仍在 npc.inventory 里的 stolen_ids。

    [!] 刻意只返回候选、由调用方在入包后对账（见 settle_reclaimed）——照既有搜刮
        （salvage/_taken）模式：背包满或同种满 3 件时 `try_add_to_inventory` 会拒收，
        若此处就先把物品移出尸体，被拒收的那件既不在尸体也不在玩家背包 = **物品湮灭**，
        而旁白还会照样播报「夺回了」——正是结算真相双闸禁止的裂缝。
    [!] 只列仍在背包里的：已被 daily_sell_surplus 卖掉/消耗的视为下落不明（真实损失，
        不追认不存在的物品）。
    """
    ids = [str(x) for x in (getattr(npc, "stolen_ids", None) or [])]
    if not ids:
        return []
    inv = {str(i) for i in (getattr(npc, "inventory", None) or [])}
    return [i for i in ids if i in inv]


def bag_delta(bag_before, bag_after) -> dict:
    """本次入包**真正新增**的 id 计数（bag_after - bag_before）。

    [!] 判断「这件到底进没进背包」不能用 `iid in bag`：玩家本来就有同 id 物品时，
        即使本次因堆叠上限/负重被 `try_add_to_inventory` 拒收，`iid in bag` 仍为真 ->
        会被误判成「已入包」而从尸体扣除，物品凭空湮灭（既有搜刮逻辑同款漏洞的根因）。
    """
    from collections import Counter
    before = Counter(str(i) for i in (bag_before or []))
    after = Counter(str(i) for i in (bag_after or []))
    return {k: v for k, v in (after - before).items() if v > 0}


def settle_reclaimed(world, npc, reclaimed: list[str], bag_before) -> list[str]:
    """入包后对账：只从劫掠者身上扣除**确实进了玩家背包**的夺回物，返回真正追回的 id。

    [!] 未追回的（背包满/堆叠满被拒收）留在 npc.inventory 与 stolen_ids 里——玩家清包后
        再击败他一次仍可夺回（物品守恒，不湮灭）；调用方也据此生成文案，保证「说了夺回就真夺回」。
    [!] 用 bag_delta 而非 `iid in bag` 判定（见 bag_delta 说明）；装备类夺回物会被 materialize
        克隆成 `{id}__unid`，故判据含副本 id（同 salvage 口径）。
    """
    if not reclaimed:
        return []
    delta = bag_delta(bag_before, getattr(getattr(world, "player", None), "inventory", None))
    back = [i for i in reclaimed if delta.get(str(i), 0) > 0 or delta.get(f"{i}__unid", 0) > 0]
    if not back:
        return []
    drop = {str(i) for i in back}
    npc.inventory = [i for i in (getattr(npc, "inventory", None) or []) if str(i) not in drop]
    npc.stolen_ids = [str(i) for i in (getattr(npc, "stolen_ids", None) or [])
                      if str(i) not in drop]
    return back


def merchant_refuses(merchant) -> str:
    """商人记恨闸：trust <= REFUSE_TRUST -> 返回拒绝理由（"" = 可交易）。

    [!] 只由「玩家侧」两个买入入口调用（ShopDialog 购买 / _resolve_trade 的 buy）。
        卖出不拒（避免把玩家逼进「没钱没渠道」的死循环）；NPC 采购路径不调用
        （守 §22「NPC 采购恒挂牌价不吃 market 因子」既有契约）。
    [!] 消解通道：送礼（慷慨 +5）/ 委托交付（守信 +4）/ 任务领奖（守信）都是既有钩子，
        玩家把 trust 拉回 -60 以上即可恢复交易——「修复关系」是可玩的。
    """
    if merchant is None:
        return ""
    imp = getattr(merchant, "player_impression", None)
    if not isinstance(imp, dict):
        return ""
    if _as_int(imp.get("trust", 0)) <= REFUSE_TRUST:
        return (f"「{getattr(merchant, 'name', '他')}」对你心存戒备，不肯与你交易"
                f"（送礼或替他办事或可缓和）")
    return ""


def trust_gate(npc, name: str = "") -> str:
    """记恨闸单一来源：npc 对玩家 trust <= REFUSE_TRUST -> 返回拒绝理由（"" = 放行）。

    [P47-B2 2026-09-25 三模型共识] 记恨此前只有拒卖一个出口；现在助战/同行/加好友等
    既有门控一律先过本闸——「他怕你」从此影响「他帮不帮你」。
    [!] 阈值复用 REFUSE_TRUST 单一来源（不得另立第二阈值）；任务发布人/主线相关门控
        **不接本闸**（防卡主线进度，DeepSeek 方案明确反对）。
    """
    if npc is None:
        return ""
    imp = getattr(npc, "player_impression", None)
    if not isinstance(imp, dict):
        return ""
    if _as_int(imp.get("trust", 0)) <= REFUSE_TRUST:
        who = name or str(getattr(npc, "name", "") or "他")
        return f"「{who}」对你心存戒备，不愿与你打交道（送礼或替他办事或可缓和）"
    return ""


def murder_event(world, victim, witness_count: int = 0):
    """[P47-C1 三模型共识] 玩家击杀「人」产命案事件 -> 喂活四条既有消费链。

    产出一条 WorldEvent（category="npc"）进 world.event_log 后，自动流经：
    坊间热议（chronicle_engine.hot_topics）/ 传闻口述化（rumor_engine.collect）/
    浅印象扩散（_tick_impressions 读 shared_rumors 打「危险」标签）/ 右侧 NPC 动态栏。
    ——玩家暴行从「只有目击者知道」变成「全城逐步知晓」。

    [!] 门控与 witness_grudge 同口径：临时野怪（不在 world.npcs）/ 敌对 NPC 不产
        （杀怪不是新闻；敌对者的死由既有击杀声望逻辑处理）。
    [!] title 必须含字面「玩家」：_tick_impressions 的命中条件是玩家真名或字面「玩家」。
    [!] severity：有目击者或要角之死 = major（上头条），否则 minor。
    [!] 本函数只构造不落盘；调用方 append 进 world.event_log（直加有先例——观战敌袭，
        不吃 tick 的 max_events_per_tick 预算）。
    """
    if victim is None:
        return None
    # 临时野怪门控（与 witness_grudge 同口径）
    if not any(n is victim for n in (getattr(world, "npcs", None) or [])):
        return None
    if getattr(victim, "hostile", False):
        return None
    from src.models.world import WorldEvent
    vid = str(getattr(victim, "id", "") or "")
    vname = str(getattr(victim, "name", "") or "?")
    sev = "major" if (witness_count or getattr(victim, "is_key_npc", False)) else "minor"
    desc = (f"有人目睹玩家与{vname}发生致命冲突" + ("，多名在场者愿意作证" if witness_count else "")
            + f"。{vname}的死讯开始在坊间流传")
    return WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0),
        category="npc",
        severity=sev,
        title=f"命案：玩家杀了{vname}",
        desc=desc,
        npcs=[vid] if vid else [],
        locations=[str(getattr(victim, "location_id", "") or "")] if getattr(victim, "location_id", "") else [],
    )


def reclaim_via_purchase(world, item_id: str) -> str:
    """玩家从货架买回自己的失物 -> 从持有者 stolen_ids 销账（赃物闭环收口）。

    返回原持有者名（"" = 该物品不是任何已知赃物）。[!] 只清账不退钱——赎回的代价
    就是买价（赃物随行情定价，往往比原价高：销赃者要赚差价，这也是"销赃"的恶意）。
    """
    target = str(item_id)
    for n in (getattr(world, "npcs", None) or []):
        stolen = [str(i) for i in (getattr(n, "stolen_ids", None) or [])]
        if target in stolen:
            n.stolen_ids = [i for i in stolen if i != target]
            return str(getattr(n, "name", "") or "?")
    return ""


def missing_stolen(npc, reclaimed: list[str]) -> list[str]:
    """被夺物中「既没追回、也不在劫掠者身上」的部分 = 已被转手卖掉/消耗（下落不明）。

    供文案层提示「他已把你的X转手卖掉了」（结算真相：不追认不存在的物品，
    也不许旁白谎报夺回）。"""
    got = {str(i) for i in (reclaimed or [])}
    inv = {str(i) for i in (getattr(npc, "inventory", None) or [])}
    return [str(i) for i in (getattr(npc, "stolen_ids", None) or [])
            if str(i) not in got and str(i) not in inv]


def witness_grudge(world, victim) -> list[str]:
    """玩家击杀 NPC 时，同地点同场所目击者的印象反应（无 rng，全确定性）。返回叙事行。

    [!] 只写同地点同场所的存活、非敌对 NPC（守 P27 场所口径 + 旁白只写在场人物）——
        别处的人不可能目睹。被害者亲友（social >= _CLOSE_SOCIAL）扣得更重。
    [!] 零 LLM 调用、零 token：只动既有 player_impression，由既有 UI（NPC 详情印象行）
        与既有消费点（买价 / 新增拒卖闸）自然反映。
    [!] best-effort：单个 NPC 更新失败不影响其他目击者。
    """
    out: list[str] = []
    if victim is None:
        return out
    # [!] 临时野怪（不在 world.npcs 的荒野怪）不算「人」：杀怪不构成恶行，不该让旁边的
    #     猎户因此记恨你（与 took_from_player 的 is_temp_monster 门控同口径）。
    if not any(n is victim for n in (getattr(world, "npcs", None) or [])):
        return out
    vid = str(getattr(victim, "id", "") or "")
    vname = str(getattr(victim, "name", "") or "他")
    for w in (getattr(world, "npcs", None) or []):
        try:
            if str(getattr(w, "id", "") or "") == vid:
                continue
            if not getattr(w, "alive", True) or getattr(w, "hostile", False):
                continue
            if not _same_place(world, w, victim):        # 守 P27：只写同场目击者
                continue
            close = _as_int((getattr(w, "social", None) or {}).get(vid, 0)) >= _CLOSE_SOCIAL
            update_impression(w, "危险",
                              trust_delta=(_GRUDGE_TRUST_CLOSE if close else _GRUDGE_TRUST_OTHER),
                              firsthand=True)
            if close:
                out.append(f"「{w.name}」眼睁睁看着你杀了他的故交{vname}，眼神里结了仇")
            else:
                out.append(f"「{w.name}」目睹了这一幕，看你的眼神变了")
        except Exception:
            continue
    return out
