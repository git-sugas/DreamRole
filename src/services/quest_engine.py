"""[P7i] 任务引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b / §23 总纲）：LLM 出静态任务定义（objectives/rewards 是种子），
纯 Python 跑动态进度判定（update_progress/check_completion/claim_reward）。运行时零 LLM。

状态机：available(未接取) -> active(进行中) -> completed(已完成待领奖) -> claimed(已领奖)
        active 也可 -> abandoned(放弃)；completed 也可 -> failed(失败)

objective.type 白名单（代码可判定的事件）：
- kill：击败 NPC（target=NPC role/id/name）
- gather：采集资源（target=资源 type，如 herb/mine）
- talk：与 NPC 对话（target=NPC name）
- visit：到达地点（target=地点 id/name/region）
- collect：获得物品（target=物品 id/name/rarity）
"""
from __future__ import annotations

from typing import Optional      # [修 2026-09-10] update_progress 注解用了 Optional（lazy 注解不炸，但 lint 报未定义）

from src.models.world import Quest

# [S04/R2 2026-09-30] 事件驱动目标三件：秘境房探明/秘境攻克（引擎钩子喂秘境名）、
# 物资交付（deliver_progress 主动交付，不走事件广播——扣物动作只认玩家操作）。
_QUEST_EVENT_TYPES = ("kill", "gather", "talk", "visit", "collect",
                      "dungeon_room_resolved", "dungeon_cleared")
_OBJ_LABELS = {"kill": "击败", "gather": "采集", "talk": "交谈", "visit": "到达", "collect": "收集",
               "dungeon_room_resolved": "探明秘境", "dungeon_cleared": "攻克秘境",
               "deliver_items": "交付", "investigate": "调查", "escort": "护送"}
# [P45 用户指示 2026-08-23] 任务领奖 -> 发布者所属势力 power 增量（钳 0-100）；
# 委托订单交付走 commission_engine 的 COMMISSION_FACTION_POWER_GAIN（+1 小额高频）。
QUEST_FACTION_POWER_GAIN = 2


def _currency(world) -> str:
    """[修 2026-08-25] 题材货币名（world.config_overlay -> GenreText.currency），缺键回退金币。

    任务 reward 文案不再硬编码「金币」——末日是晶核、仙侠是灵石、科幻是信用点。"""
    try:
        from src.models.world_sim_preset import GenreText
        return GenreText(getattr(world, "config_overlay", None)).currency
    except Exception:
        return "金币"


def accept_quest(world, quest, current_tick: int) -> bool:
    """接取任务：available -> active。返回是否成功。

    [任务改造] giver 在场校验：发布者 NPC 非空时须与玩家同地点且存活（找发布者当面接取）。
    无发布者（giver_npc_id 空，如危机任务）豁免校验。
    [修 2026-09-13] 接取瞬间回算 visit：发布者就在目标城市是常态（如「抵达澜港市，
    向码头管事报到」），而 visit 进度只由移动事件结算——人在城里接任务会卡 0/1，
    得出村再进村才跳。此处按移动钩子同口径（id/name/region 别名）回算一次。
    """
    if getattr(quest, "status", "") != "available":
        return False
    if not giver_present(world, quest):
        return False
    quest.status = "active"
    quest.started_at_tick = max(0, int(current_tick))
    player = getattr(world, "player", None)
    cur = next((l for l in (getattr(world, "locations", None) or [])
                if getattr(l, "id", "") == str(getattr(player, "location_id", "") or "")), None)
    if cur is not None:
        update_progress(world, "visit", getattr(cur, "name", ""),
                        aliases=[getattr(cur, "id", ""), getattr(cur, "region", "")])
    return True


def abandon_quest(quest) -> bool:
    """放弃任务：active -> abandoned。"""
    if getattr(quest, "status", "") != "active":
        return False
    quest.status = "abandoned"
    return True


def main_giver_ids(world) -> set:
    """[P45 v3 2026-09-12] 主线（chain=="main"）任务的发布者 id 集合。

    主线发布人不死（用户定稿）——否则链式主线的后续段（chain_next_id，初始 locked）
    永远解不开，整条主线断掉。"""
    out: set = set()
    # [P45 v3.1 审核修复 GLM] 只保护**未完结**的主线环：claimed/completed/failed/
    # abandoned 的环不再护体（否则主线全清后其发布人永远杀不死）。
    # completed = 目标已达成、待当面领奖——此窗内发布人死了 claim 恒 need_giver，
    #   chain_next_id 永不解锁（review-glm-recheck），同样护体。
    _UNDONE = ("available", "locked", "active", "completed")
    for q in (getattr(world, "quests", None) or []):
        if str(getattr(q, "chain", "") or "") != "main":
            continue
        if str(getattr(q, "status", "") or "") not in _UNDONE:
            continue
        gid = str(getattr(q, "giver_npc_id", "") or "")
        if gid:
            out.add(gid)
    return out


def is_main_giver(world, npc) -> bool:
    """[P45 v3] 该 NPC 是否当前主线任务的发布者（主线发布人不死的判定单一来源）。"""
    nid = str(getattr(npc, "id", "") or "")
    return bool(nid) and nid in main_giver_ids(world)


def cleanup_dead_giver_quests(world, npc) -> str:
    """[P45 v3 2026-09-12 用户定稿] 发布人身亡 -> 名下任务清理，返回叙事短句（无则空串）。

    - active（玩家已接）-> abandoned（保留记录，任务板可见，不凭空消失）；
    - available / locked（未接/未解锁）-> 直接从 world.quests 移除（否则任务板永远挂着
      死人的委托，且 giver_present=False 永远接不了）；
    - done / abandoned 等终态保留；
    - 主线（chain=="main"）兜底跳过——主线发布人不死（is_main_giver 豁免），不该走到这。

    [!] 现状缺口：发布人死后 giver_present（:88）恒 False -> 任务既不能接（:196 附近）
    也不能交（:195 need_giver），永远卡死。本函数在死亡点接线堵住该缺口。
    """
    nid = str(getattr(npc, "id", "") or "")
    if not nid:
        return ""
    names: list = []
    kept: list = []
    changed = False
    for q in (getattr(world, "quests", None) or []):
        if str(getattr(q, "giver_npc_id", "") or "") != nid:
            kept.append(q)
            continue
        if str(getattr(q, "chain", "") or "") == "main":
            kept.append(q)                 # 主线兜底豁免（主线发布人不死，正常不会走到这）
            continue
        title = str(getattr(q, "title", "") or "无名委托")
        st = str(getattr(q, "status", "") or "")
        if st == "active":
            q.status = "abandoned"
            kept.append(q)
            names.append(title)
            changed = True
        elif st in ("available", "locked"):
            names.append(title)            # 移除（不进 kept）
            changed = True
        else:
            kept.append(q)                 # 终态保留
    if not changed:
        return ""
    world.quests = kept
    shown = "》《".join(names[:3])
    more = "等任务" if len(names) > 3 else "任务" if len(names) > 1 else ""
    return (f"{getattr(npc, 'name', '') or '死者'}生前托付的《{shown}》{more}就此作罢。")


def _matches(objective_target: str, event_target: str) -> bool:
    """目标匹配：空 target 匹配任意（该 type 的任何事件都计数）；否则相等/包含。

    [P44] 在相等/单向包含之上叠加统一名称判定（归一化/双向子串/编辑距离容错）——
    LLM 出的 objective 措辞与引擎事件 target 名常有一字之差或修饰差
    （objective「黑风寨妖狼」vs 事件「妖狼」、「妖狠」vs「妖狼」），精确口径会卡进度。
    """
    ot = str(objective_target or "").strip()
    if not ot:
        return True
    et = str(event_target or "")
    if ot == et or ot in et:
        return True
    from src.services.name_resolver import names_match
    return names_match(ot, et)


def _quests_on(world) -> bool:
    """[P11c] 任务系统开关（overlay.quest_system_enabled，build 时从 preset 写入）。

    关闭 = 任务仅作叙事提示：进度不推进、领奖不发奖励，任务列表仍可见。
    """
    return bool((getattr(world, "config_overlay", None) or {}).get("quest_system_enabled", True))


def giver_present(world, quest) -> bool:
    """[任务改造] 发布者 NPC 是否在场（[2026-08-28 用户定稿] 场所级：同地点且同场所）。

    giver_npc_id 为空（危机任务等无发布者）-> True（豁免，无发布者无处可找）。
    否则查 world.npcs 找 giver，要求 alive 且与玩家同地点；地点有场所时还须同场所
    （place_id 空回退地点 default_place_id）；无场所地点（野外）退化地点级。
    """
    gid = str(getattr(quest, "giver_npc_id", "") or "")
    if not gid:
        return True  # 无发布者豁免
    p = getattr(world, "player", None)
    if p is None:
        return False
    giver = next((n for n in getattr(world, "npcs", []) if n.id == gid), None)
    if giver is None or not getattr(giver, "alive", True):
        return False
    if str(getattr(giver, "location_id", "") or "") != str(getattr(p, "location_id", "") or ""):
        return False
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if l.id == getattr(p, "location_id", "")), None)
    if loc is None or not getattr(loc, "places", None):
        return True                      # 无场所化地点：地点级即同场
    def _pid(o):
        pid = (getattr(o, "place_id", "") or "").strip()
        return pid or (getattr(loc, "default_place_id", "") or "").strip()
    return _pid(p) == _pid(giver)


def giver_name(world, quest) -> str:
    """[任务改造] 发布者 NPC 显示名（找不到返回空串）。"""
    gid = str(getattr(quest, "giver_npc_id", "") or "")
    if not gid:
        return ""
    giver = next((n for n in getattr(world, "npcs", []) if n.id == gid), None)
    return str(getattr(giver, "name", "") or "") if giver is not None else ""


def update_progress(world, event_type: str, target: str = "", qty: int = 1,
                     aliases: Optional[list] = None) -> list:
    """更新所有 active 任务的匹配 objective 进度。返回本次刚完成的任务列表。

    event_type 必须在白名单内（否则返回 []，不动状态）。
    target: 事件目标（kill=NPC role/id/name、gather=资源 type、talk=NPC name、
            visit=地点 id/name/region、collect=物品 id/name/rarity）。
    aliases: 同一动作的其余识别名（kill 的 role+name、visit 的 name+region、collect 的
            name+rarity）——[!] 一次动作至多推进每个 objective 一步：调用方分两次
            update_progress 会让同时匹配 role 与 name 的目标一次 +2（进度翻倍）。
    """
    if event_type not in _QUEST_EVENT_TYPES:
        return []
    if not _quests_on(world):
        return []
    names = [str(target)] + [str(a) for a in (aliases or []) if a and str(a) != str(target)]
    completed = []
    for q in getattr(world, "quests", []) or []:
        if getattr(q, "status", "") != "active":
            continue
        for obj in (q.objectives or []):
            if not isinstance(obj, dict) or obj.get("type") != event_type:
                continue
            cur = int(obj.get("current", 0) or 0)
            cnt = int(obj.get("count", 1) or 1)
            if cur >= cnt:
                continue  # 此目标已完成
            if any(_matches(obj.get("target", ""), nm) for nm in names):
                obj["current"] = min(cnt, cur + max(1, int(qty)))
                # any() 已保证该 objective 至多推进一步（多别名不重复计）；不可 break——
                # 同任务可有多个 objective（kill 3 妖狼 + kill 2 山贼），break 会漏推后续目标
        # [S04 余项 2026-09-30] 复合目标（消费既有 talk/visit 事件，不新增事件类型）：
        # investigate 两段式（0:与 target 交谈取线索 -> 1:到访 target2 查证，count 恒 2）；
        # escort（target 同行状态下到访 target2 即完成，count 恒 1）。
        for obj in (q.objectives or []):
            if not isinstance(obj, dict):
                continue
            ot2 = str(obj.get("type", "") or "")
            cur = int(obj.get("current", 0) or 0)
            cnt = int(obj.get("count", 1) or 1)
            if cur >= cnt:
                continue
            if ot2 == "investigate":
                if cur == 0 and event_type == "talk" and _matches(obj.get("target", ""), names[0]):
                    obj["current"] = min(cnt, cur + 1)
                elif cur >= 1 and event_type == "visit" and _matches(obj.get("target2", ""), names[0]):
                    obj["current"] = min(cnt, cur + 1)
            elif ot2 == "escort" and event_type == "visit" and _matches(obj.get("target2", ""), names[0]):
                # 护送门控：目标 NPC 须正在同行（companion_npc_ids）——到访时人在身边才算送达
                p_ = getattr(world, "player", None)
                cnames = []
                for cid in (getattr(p_, "companion_npc_ids", None) or []):
                    cn = next((n for n in (getattr(world, "npcs", None) or [])
                               if str(getattr(n, "id", "")) == str(cid)), None)
                    if cn is not None and getattr(cn, "alive", True):
                        cnames.append(str(getattr(cn, "name", "")))
                if any(_matches(obj.get("target", ""), c) for c in cnames):
                    obj["current"] = cnt
        # [修 2026-10-01 用户报·线索型目标永冻] visit 兜底「物品池外的 collect 目标」：
        # LLM 常把调查类目标写成 collect:某某线索——线索无实体物品（池外名，引擎
        # 不虚构物品），掉落/购买永不发生，目标永冻。语义上「线索」靠走访查证获得：
        # 玩家到访任意地点时，同任务内池外线索型 collect 目标 +1（至多一步，与既有
        # 口径一致）。物品池以名字精确匹配为准（容错会把真物品别名误判成线索）。
        if event_type == "visit":
            _pool = {str(getattr(i, "name", "") or "")
                     for i in (getattr(world, "items", None) or [])}
            for obj in (q.objectives or []):
                if (isinstance(obj, dict) and obj.get("type") == "collect"
                        and int(obj.get("current", 0) or 0) < int(obj.get("count", 1) or 1)
                        and str(obj.get("target", "") or "").strip()
                        and str(obj.get("target", "")).strip() not in _pool):
                    obj["current"] = min(int(obj.get("count", 1) or 1),
                                         int(obj.get("current", 0) or 0) + 1)
        # 整任务完成判定 + 进度刷新
        if check_completion(q):
            q.status = "completed"
            q.progress = 100
            completed.append(q)
        else:
            q.progress = progress_percent(q)
    return completed


def check_completion(quest) -> bool:
    """任务是否所有 objectives 完成。无结构化目标 -> 不自动判定（靠叙事/手动）。"""
    objs = [o for o in (getattr(quest, "objectives", []) or []) if isinstance(o, dict)]
    if not objs:
        return False
    return all(int(o.get("current", 0)) >= int(o.get("count", 1) or 1) for o in objs)


def progress_percent(quest) -> int:
    """结构化目标进度百分比（0-100）。无目标则沿用 quest.progress。"""
    objs = [o for o in (getattr(quest, "objectives", []) or []) if isinstance(o, dict)]
    if not objs:
        return max(0, min(100, int(getattr(quest, "progress", 0) or 0)))
    total = sum(int(o.get("count", 1) or 1) for o in objs)
    done = sum(min(int(o.get("current", 0)), int(o.get("count", 1) or 1)) for o in objs)
    return max(0, min(100, int(done * 100 / total) if total > 0 else 0))


# ============================================================
# [任务奖励物品 2026-09-13] 任务奖励挑一件目录物品（纯引擎，确定性）
# ============================================================
# 定位：任务奖励是「低门槛渠道」——顺手可得的渠道不给神装。稀有度封顶 epic（紫），
# 品阶 level 上限默认值取 3（与 preset.shop_max_item_level 的**默认值**相同，但不跟随
# per-world 配置——引擎侧 sae.advance 拿不到 preset），橙红与高阶货仍只从战斗掉落 /
# 锻造 / 拍卖会三个渠道出（守 P34a「高 level 物品绝不出商店」与 NPC 初始携带封顶 epic
# 的同源思路）。
#
# 两段式挑选：先按权重 roll 一个稀有度档，再在该档内等概率取一件。
# [!] 不能对整池等概率 pick——目录里 common 占绝大多数，epic 实际上永远抽不中，
# 「紫以下」这条规则会退化成「永远给白装」，所以稀有度档必须是一等公民。
REWARD_RARITY_CAP = "epic"      # 紫（含）以下——橙红只从掉落/锻造/拍卖出
_RARITY_FULL_ORDER = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
# 上限驱动：改 REWARD_RARITY_CAP 即改入池档位，别处不再硬编码「紫以下」
_REWARD_RARITY_ORDER = _RARITY_FULL_ORDER[:_RARITY_FULL_ORDER.index(REWARD_RARITY_CAP) + 1]
# 任务档位 -> 各稀有度档权重（档位只挪分布，不放宽 epic 上限）
REWARD_RARITY_WEIGHTS: dict = {
    "low":  {"common": 60, "uncommon": 30, "rare": 10, "epic": 0},
    "mid":  {"common": 40, "uncommon": 30, "rare": 20, "epic": 10},
    "high": {"common": 10, "uncommon": 25, "rare": 35, "epic": 30},
}
# [!] 默认值与 preset.shop_max_item_level 的默认值相同，但**不跟随 per-world 配置**——
# 引擎侧（sae.advance）拿不到 preset。语义对齐商店：0 = 不限，>0 = 品阶上限。
REWARD_ITEM_MAX_LEVEL = 3


def player_owned_ids(world) -> set:
    """玩家已持有的物品 id（背包 + 已装备）——奖励不该挑玩家已经有了的东西
    （`try_add_to_inventory` 去重会让「已持有」也归 False，玩家等于白做一次任务）。"""
    p = getattr(world, "player", None)
    ids = {str(i) for i in (getattr(p, "inventory", None) or []) if i}
    eq = getattr(p, "equipped", None) or {}
    if isinstance(eq, dict):
        ids |= {str(v) for v in eq.values() if v}
    return ids


def reward_item_allowed(item, max_level: int = REWARD_ITEM_MAX_LEVEL,
                        allow_skillbook: bool = False) -> bool:
    """单件物品能否当任务奖励（与 reward_item_pool 同口径，供 LLM 任务清洗复用）。

    [!] 不接 world：调用方之一是 `build_world_from_skeleton`，那时 world 还没建出来
    （只有待落库的 items 列表），且本函数只看物品自身字段。
    [!] `allow_skillbook`：随机挑选池不开技能书第五渠道（默认），但 **LLM 点名的任务
    奖励要放行**——read.md 定稿的技能书渠道里「开场任务」就是合法出口，把 LLM 点名的
    书闸掉会直接断掉「接任务->领奖->读书学技能」的新手教学起点。


    LLM 主/支线任务的奖励物品是 LLM 点名的名字解析出来的，**不走挑选器**，
    必须在落库前用同一把闸过滤，否则「橙红只从掉落/锻造/拍卖出」在主干任务上形同虚设。
    """
    from src.models.world import effective_category
    if item is None or not str(getattr(item, "name", "") or "").strip():
        return False                                # 空名：文案会挂半截「+」
    if str(getattr(item, "type", "") or "") == "key":
        return False
    if not allow_skillbook and isinstance(getattr(item, "teach_skill", None), dict) \
            and item.teach_skill:
        return False                                # 随机池不开技能书第五渠道
    if effective_category(item) == "cultivate":
        return False
    if str(getattr(item, "rarity", "common") or "common") not in _REWARD_RARITY_ORDER:
        return False
    top = max(0, int(max_level))
    return not (top > 0 and int(getattr(item, "level", 0) or 0) > top)


def reward_item_pool(world, max_level: int = REWARD_ITEM_MAX_LEVEL,
                     prefer_types=None, exclude_ids=None) -> list:
    """任务奖励候选池（world.items 过滤版）。

    - 排除 key：钥匙是剧情锁，白送等于拆掉门控。
    - 排除 cultivate：鉴定/洗练道具只能肝掉落拍，不作任务赠品（否则任务变成养成资源农场，
      削弱 P34b 未鉴定赌徒经济）。
    - 排除拍品 id：拍品只走竞价（与 _seed_shop_from_catalog 六处排除同源口径）。
    - 排除技能书：技能书四渠道（商店/Boss 掉落/精英/秘境）是定稿清单，任务不偷偷开第五渠道。
    - 稀有度只留 common..epic（橙红不入池）；level 过闸的剔除（0 = 不限）。
    - exclude_ids：玩家已持有（背包 + 已装备）的 id，避免承诺一件他已经有的东西。
    - prefer_types 是「主题贴合」软偏好，且**弱于已鉴定**（偏好∩已鉴定 > 已鉴定全集）：
      宁可给件不相干的已鉴定货，也不给一件属性打码、玩家看不出价值的未鉴定偏好品。
    - 已鉴定优先：未鉴定装备属性打码，作为任务奖励看不出价值；全池皆未鉴定时退回全池。
    """
    from src.models.world import effective_category
    try:                                            # [!] 只包 import：延迟导入避环（wss 依赖 qe）
        from src.services.world_sim_service import _auction_lot_ids
    except Exception:  # noqa: BLE001 - 导入失败退化为不排除
        _lot_ids = None
    else:
        _lot_ids = _auction_lot_ids
    # [审核 P2] 扫描函数本身不许被 except 吞掉：静默退化会放行拍品、击穿隔离不变量
    auc_ids = _lot_ids(world) if _lot_ids is not None else set()
    skip = {str(i) for i in (exclude_ids or ()) if i}
    out = []
    for it in (getattr(world, "items", None) or []):
        if str(getattr(it, "id", "") or "") in auc_ids or str(getattr(it, "id", "") or "") in skip:
            continue
        if not reward_item_allowed(it, max_level=max_level):
            continue
        out.append(it)
    idn = [it for it in out if bool(getattr(it, "identified", True))]
    base = idn or out
    wanted = {str(t) for t in (prefer_types or ()) if t}
    if not wanted:
        return base

    return [it for it in base
            if str(getattr(it, "type", "") or "") in wanted] or base


def pick_reward_item(world, rng, tier: str = "mid", prefer_types=None,
                     max_level: int = REWARD_ITEM_MAX_LEVEL, exclude_ids=None):
    """挑一件任务奖励物品；池空（或只剩本档位权重为 0 的货）返回 None。

    只消费传入的 rng（守数值范式：同 world+tick 双实例可回放）。
    """
    pool = reward_item_pool(world, max_level=max_level, prefer_types=prefer_types,
                            exclude_ids=exclude_ids)
    if not pool:
        return None
    weights = REWARD_RARITY_WEIGHTS.get(tier) or REWARD_RARITY_WEIGHTS["mid"]
    by_rarity: dict = {}
    for it in pool:
        by_rarity.setdefault(str(getattr(it, "rarity", "common") or "common"), []).append(it)
    tiers = [r for r in _REWARD_RARITY_ORDER if by_rarity.get(r)]
    if not tiers:
        return None
    ws = [max(0, int(weights.get(r, 0) or 0)) for r in tiers]
    if sum(ws) <= 0:
        # [审核 P2] 池里只剩本档位权重为 0 的货（如 low 档只剩紫）——宁可不发，
        # 也不把「low 不给紫」的档位语义悄悄降级成等权发放。
        return None
    # [!] 只消费 roll/pick（不用 weighted）：story_arc 引擎约定 rng 只暴露 roll/chance/
    # pick 三个方法，测试桩也只实现这三个——多用一个方法会让既有桩全线 AttributeError。
    draw = int(rng.roll(1, int(sum(ws))))
    acc = 0
    chosen = tiers[-1]
    for r, w in zip(tiers, ws):
        acc += w
        if draw <= acc:
            chosen = r
            break
    return rng.pick(by_rarity.get(str(chosen)) or by_rarity[tiers[-1]])


def item_names(world, item_ids) -> list:
    """物品 id 列表 -> 名字列表（领奖摘要展示用，避免把 uuid 直接甩给玩家）。"""
    by_id = {str(getattr(it, "id", "")): str(getattr(it, "name", "") or "")
             for it in (getattr(world, "items", None) or [])}
    return [by_id.get(str(i)) or str(i) for i in (item_ids or [])]


def claim_reward(world, quest, current_tick: int, gain_xp_fn=None) -> dict:
    """领取奖励：completed -> claimed，发 rewards（items/gold/xp）。返回发放摘要。

    gain_xp_fn: 可选 (player, xp) 回调（调用方传 combat_engine.gain_xp 处理经验/升级）。
    """
    if getattr(quest, "status", "") != "completed":
        return {}
    if not _quests_on(world):
        # 任务系统关闭：不发奖励也不转 claimed（任务仅作叙事提示）
        return {}
    # [任务改造] giver 在场校验：发布者 NPC 非空时须与玩家同地点且存活（找发布者当面领奖）
    if not giver_present(world, quest):
        return {"error": "need_giver"}
    # [玩家印象 2026-09-06] 当面领奖：发布者对玩家记「守信」
    try:
        from src.services import social_engine as _se
        _g = next((n for n in (getattr(world, "npcs", None) or [])
                   if n.id == str(getattr(quest, "giver_npc_id", "") or "")), None)
        if _g is not None and getattr(_g, "alive", True):
            _se.update_impression(_g, "守信", trust_delta=4, firsthand=True)
    except Exception:
        pass
    rw = quest.rewards or {}
    if not isinstance(rw, dict):
        rw = {}
    items = [str(x) for x in (rw.get("items") or []) if isinstance(x, str)]
    gold = max(0, int(rw.get("gold", 0) or 0))
    # [P14] 金币产出乘经济节奏系数（overlay.economy_pace）
    from src.services import trade_engine as _tre
    gold = int(gold * _tre.gold_gain_mult(_tre.world_pace(world)))
    xp = max(0, int(rw.get("xp", 0) or 0))
    p = getattr(world, "player", None)
    bag_full = False
    already: list = []
    granted: list = []
    unclaimed: list = []   # [B01-F10] 满包没拿走的（保留领取权益，不再静默吞掉）
    if p is not None:
        # [!] 入包走统一口径（去重 + 负重上限，同战斗掉落/采集）
        # [审核 P1] try_add_to_inventory 对「已持有该蓝图 id」与「背包满」都返回 False，
        # 混成一个 bag_full 会把「你已经有这件了」误报成「背包满」——先分流再入包。
        from src.services.combat_engine import try_add_to_inventory
        bag_full = False
        owned = player_owned_ids(world)     # 背包 + 已装备（装备会把 id 移出背包）
        for iid in items:
            if not iid:
                continue
            if str(iid) in owned:
                already.append(iid)         # 已持有：别甩锅给「背包满」
                continue
            if try_add_to_inventory(p, iid):
                granted.append(iid)
            else:
                bag_full = True
                unclaimed.append(iid)
        if gold > 0:
            p.gold = int(p.gold or 0) + gold
        if xp > 0 and gain_xp_fn is not None:
            gain_xp_fn(p, xp)
    quest.status = "claimed"
    quest.claimed_at_tick = max(0, int(current_tick))
    # [B01-F10] 满包滞留的物品记在任务上（贡献确认/金币/经验已一次性发放不重发）；
    # claim_pending_items 补领，取空清列表——claimed 后任务页仍有领取入口。
    quest.pending_items = unclaimed
    # [P15b1] 任务链：领奖解锁下一环（locked -> available，玩家可在任务日志接取）
    unlocked_next = ""
    next_id = str(getattr(quest, "chain_next_id", "") or "")
    if next_id:
        nxt = next((x for x in getattr(world, "quests", []) if x.id == next_id), None)
        if nxt is not None and getattr(nxt, "status", "") == "locked":
            nxt.status = "available"
            unlocked_next = nxt.title
    # [P10] 领奖交情：任务发布者 NPC 若存活在场于世界，与玩家交情增长（守约的正反馈）。
    # [P42c 规格] 跑腿是每日可重复差事，交情 +3（普通任务一次性 +8）——多条/日 × +8 会
    # 把交情推得远超「小额差事」定位。
    giver = next((n for n in getattr(world, "npcs", []) if n.id == quest.giver_npc_id), None)
    if giver is not None and getattr(giver, "alive", True):
        try:
            from src.services import npc_reaction_engine as _nre
            _nre.add_affinity(giver, 3 if getattr(quest, "chain", "") == "errand" else 8)
        except Exception:
            pass
    # [P45 用户指示 2026-08-23] 势力任务反馈：领奖时发布者所属势力 power +QUEST_FACTION_POWER_GAIN
    # （钳 0-100）——玩家做势力相关任务直接增长该势力实力；_war_weight 按 power^2 加权，
    # 亲近势力战胜算更大（系统互指：任务完成 -> 势力实力 -> 战争胜负 -> 夺城/物价/股市）。
    faction_gain_note = ""
    fid = str(getattr(giver, "faction_id", "") or "") if giver is not None else ""
    if fid:
        fac = next((f for f in getattr(world, "factions", []) if f.id == fid), None)
        if fac is not None:
            from src.models.world import WorldEvent
            old_power = int(getattr(fac, "power", 0) or 0)
            fac.power = max(0, min(100, old_power + QUEST_FACTION_POWER_GAIN))
            # [物价联动 2026-09-10 用户指示] 领奖兼养势力财富：collect 经济类 +2、其余 +1
            # （送货本质是给势力供货；量级与 tick_economy 漂移同阶小推力，钳 0-100）。
            old_wealth = int(getattr(fac, "wealth", 0) or 0)
            wealth_gain = 2 if any(
                isinstance(o, dict) and str(o.get("type", "")) == "collect"
                for o in (getattr(quest, "objectives", None) or [])) else 1
            fac.wealth = max(0, min(100, old_wealth + wealth_gain))
            if fac.power != old_power or fac.wealth != old_wealth:
                faction_gain_note = (f"{fac.name}实力+{fac.power - old_power}"
                                     f" 财富+{fac.wealth - old_wealth}")
                world.event_log.append(WorldEvent(
                    tick=max(0, int(current_tick)), category="faction_war",
                    severity="minor", title=f"声势渐涨：{fac.name}",
                    desc=f"受冒险者相助（完成「{quest.title}」），{fac.name}实力与财富增长"
                         f"（实力 {old_power} -> {fac.power}，财富 {old_wealth} -> {fac.wealth}）。",
                    factions=[fac.id]))
    # [任务奖励物品] items 仍是 id（程序口径）；*_names 供 UI 直显——不要把 uuid 甩给玩家。
    # granted_names 才是「这次真拿到手的」，UI 的「领取奖励：」只列它，避免先说领到再说早有。
    out = {"items": items, "item_names": item_names(world, items),
           "granted_names": item_names(world, granted),
           "already_owned": item_names(world, already),
           "gold": gold, "xp": xp, "bag_full": bag_full}
    if unclaimed:
        # [B01-F10] UI 据此提示「物品已保留，可稍后领取」而不是静默丢失
        out["pending_names"] = item_names(world, unclaimed)
    if unlocked_next:
        out["unlocked_next"] = unlocked_next
    if faction_gain_note:
        out["faction_power"] = faction_gain_note
    return out


def claim_pending_items(world, quest) -> dict:
    """[B01-F10] 补领上次因背包满滞留的任务奖励物品（claimed 后任务页入口）。

    只发物品：金币/经验/交情/势力反馈在 claim_reward 已一次性发放，绝不重发；
    不要求发布者当面（贡献早已确认，这是同一笔领奖的收尾）。幂等：滞留取空后
    再调返回 {}。"""
    if str(getattr(quest, "status", "")) != "claimed":
        return {}
    pend = [str(x) for x in (getattr(quest, "pending_items", None) or []) if str(x)]
    if not pend:
        return {}
    p = getattr(world, "player", None)
    granted: list = []
    bag_full = False
    if p is not None:
        from src.services.combat_engine import try_add_to_inventory
        for iid in pend:
            if try_add_to_inventory(p, iid):
                granted.append(iid)
            else:
                bag_full = True
    quest.pending_items = [iid for iid in pend if iid not in granted]
    return {"items": list(pend), "granted_names": item_names(world, granted),
            "pending_names": item_names(world, quest.pending_items),
            "bag_full": bag_full}


def _find_item_by_name(world, name: str):
    """按名容错锚定 world.items（deliver_items 目标解析用；空名返回 None）。"""
    from src.services.name_resolver import resolve_name
    nm = str(name or "").strip()
    if not nm:
        return None
    names = [str(i.name) for i in (getattr(world, "items", None) or [])
             if getattr(i, "name", "")]
    hit = resolve_name(nm, names)
    if not hit:
        return None
    return next((i for i in (getattr(world, "items", None) or [])
                 if str(i.name) == hit), None)


def deliverable_now(world, quest) -> list:
    """[S04/R2] deliver_items 目标当前可交付读数（UI 门控用，纯读）：
    [{item_name, have, need_left, obj_index}]——have>0 才出现。"""
    out = []
    if str(getattr(quest, "status", "")) != "active":
        return out
    p = getattr(world, "player", None)
    if p is None:
        return out
    for idx, obj in enumerate(getattr(quest, "objectives", None) or []):
        if not isinstance(obj, dict) or obj.get("type") != "deliver_items":
            continue
        need = max(0, int(obj.get("count", 1) or 1) - int(obj.get("current", 0) or 0))
        if need <= 0:
            continue
        item = _find_item_by_name(world, str(obj.get("target", "") or ""))
        if item is None:
            continue
        have = sum(1 for iid in (p.inventory or []) if str(iid) == str(item.id))
        if have > 0:
            out.append({"item_name": str(item.name), "have": have,
                        "need_left": need, "obj_index": idx})
    return out


def deliver_progress(world, quest) -> dict:
    """[S04/R2 2026-09-30] deliver_items 目标主动交付：背包真实扣物 -> 发布者容器。

    - 部分交付合法（记录真实交付数量——「2/3」是真的）；全目标完成转 completed
      （与 update_progress 同口径刷新 progress）。
    - [!] 守恒转移：物品 append 进发布者 inventory 不走 try_add_to_inventory
      （守 §11「只约束新获得」——转移不是获得，加上限会凭空湮灭）。
    - 门控：任务 active + 发布者存活当面（同接取/领奖口径）。
    返回 {"moved": n, "items": "名字x件…", "remaining": 还差件数, "error": ""}。"""
    err = {"moved": 0, "items": "", "remaining": 0, "error": "失败"}
    if str(getattr(quest, "status", "")) != "active" or not _quests_on(world):
        err["error"] = "任务不在进行中"
        return err
    giver = next((n for n in (getattr(world, "npcs", None) or [])
                  if str(getattr(n, "id", "")) == str(getattr(quest, "giver_npc_id", "") or "")), None)
    if giver is None or not getattr(giver, "alive", True):
        err["error"] = "发布者已不在"
        return err
    if not giver_present(world, quest):
        err["error"] = "need_giver"
        return err
    p = getattr(world, "player", None)
    if p is None:
        err["error"] = "无玩家"
        return err
    moved_bits: list = []
    total = 0
    for obj in (getattr(quest, "objectives", None) or []):
        if not isinstance(obj, dict) or obj.get("type") != "deliver_items":
            continue
        need = max(0, int(obj.get("count", 1) or 1) - int(obj.get("current", 0) or 0))
        if need <= 0:
            continue
        item = _find_item_by_name(world, str(obj.get("target", "") or ""))
        if item is None:
            continue
        have = sum(1 for iid in (p.inventory or []) if str(iid) == str(item.id))
        move = min(have, need)
        if move <= 0:
            continue
        for _ in range(move):
            p.inventory.remove(str(item.id))          # 背包真实扣物
        if not isinstance(getattr(giver, "inventory", None), list):
            giver.inventory = []
        giver.inventory.extend([str(item.id)] * move)  # 守恒转移（append 不走上限）
        obj["current"] = int(obj.get("current", 0) or 0) + move
        moved_bits.append(f"{item.name}x{move}")
        total += move
    if not total:
        err["error"] = "背包里没有可交付的物资"
        return err
    # [G03/R3 2026-09-30] 真实交付 -> 发布者所在地点 4 天小额纾解行情（守恒交付的
    # 行情回响；best-effort 不影响交付本身）
    try:
        from src.services import region_pressure as _rp
        _rp.relief_from_delivery(world, str(getattr(giver, "location_id", "") or ""),
                                 "、".join(moved_bits))
    except Exception:
        pass
    if check_completion(quest):
        quest.status = "completed"
        quest.progress = 100
    else:
        quest.progress = progress_percent(quest)
    remaining = sum(max(0, int(o.get("count", 1) or 1) - int(o.get("current", 0) or 0))
                    for o in (getattr(quest, "objectives", None) or [])
                    if isinstance(o, dict) and o.get("type") == "deliver_items")
    return {"moved": total, "items": "、".join(moved_bits), "remaining": remaining,
            "error": ""}


def anchor_hint(world, obj: dict) -> str:
    """[修 2026-10-02 用户拍板 C] 目标真实锚点注脚（任务日志 UI 用，纯查表零 LLM）。

    LLM 写的目标 desc 偶尔发明地图上不存在的场景地名（真机案「清理避难所通风井
    外的巨鼠」——世界没有通风井这个地点），玩家照文案找路会扑空；本函数按引擎
    实际计数口径生成永真的锚点行：kill=按怪物名任意击杀计数 + 出没野外地清单
    （引擎按 danger 区间过滤刷怪，映射永真）、visit=真实地点/场所名、talk=真实
    NPC。锚点解析不到（脏数据/类型不适用）返回空串——不输出比输出错的好。
    gather/collect/deliver 的渠道提示走 sourcing_hint（另一条 ↳ 行），本函数不与之重复。
    """
    t = str(obj.get("target", "") or "").strip()
    t2 = str(obj.get("target2", "") or "").strip()
    typ = str(obj.get("type", "") or "")
    if not t:
        return ""
    if typ == "kill":
        mon = next((m for m in (world.monster_pool or [])
                    if isinstance(m, dict) and str(m.get("name") or "") == t), None)
        if mon is not None:
            # [修 2026-10-02 用户指示·出没地补全] 引擎只在 danger 落在怪物区间内的
            # 野外地刷该怪（wilderness_engine._pick_template 同款过滤）——把这个
            # 永真映射说出来，玩家不再「知道要杀什么却不知道去哪杀」。
            try:
                lo = int(mon.get("danger_min", 1) or 1)
                hi = int(mon.get("danger_max", 10) or 10)
            except (TypeError, ValueError):
                lo, hi = 1, 10
            haunts = [str(l.name or "") for l in (world.locations or [])
                      if str(getattr(l, "kind", "") or "") == "wilderness"
                      and lo <= int(getattr(l, "danger", 0) or 0) <= hi
                      and str(l.name or "")]
            if haunts:
                shown = "、".join(haunts[:4]) + ("等" if len(haunts) > 4 else "")
                return f"击杀任意「{t}」即可计数（多出没于：{shown}）"
            return f"击杀任意「{t}」即可计数（野外随机遭遇出没）"
        npc_names = {str(n.name or "") for n in (world.npcs or [])}
        if t in npc_names:
            return f"击败「{t}」（需与其在同一地点相遇）"
        return ""
    if typ == "visit":
        for l in (world.locations or []):
            if t == str(l.name or ""):
                return f"地点「{t}」（世界地图可前往）"
        for l in (world.locations or []):
            for pl in (l.places or []):
                if t == str(getattr(pl, "name", "") or ""):
                    return f"场所「{t}」（位于「{l.name}」内）"
        npc_names = {str(n.name or "") for n in (world.npcs or [])}
        if t in npc_names:
            return f"找到「{t}」即可（与其同地点）"
        return ""
    if typ == "talk":
        npc_names = {str(n.name or "") for n in (world.npcs or [])}
        if t in npc_names:
            return f"与「{t}」交谈（需与其同地点）"
        return ""
    if typ == "investigate" and t2:
        return f"先向「{t}」打听线索，再前往「{t2}」查证"
    if typ == "escort" and t2:
        return f"与「{t}」同行，护送到「{t2}」"
    return ""


def sourcing_hint(world, target_name: str, limit: int = 4) -> str:
    """[修 2026-10-01 用户报·NPC 不知道哪里有货] 全城扫货架/资源点/NPC 随身，给收集
    类目标出获取渠道摘要（纯查表零 LLM）。

    单一来源：任务日志 UI 的「↳ 有售/可采集」提示与叙事上下文【任务物品风声】块
    共用本函数（原逻辑长在 QuestLogDialog._source_hint 里，注入侧复用不了）。
    """
    if not target_name:
        return ""
    from src.services.name_resolver import names_match
    loc_by_id = {l.id: l.name for l in (world.locations or [])}
    item_by_id = {i.id: i for i in (world.items or [])}
    # 1) 商店货架（有货）
    shop_hits = []
    for s in (world.shops or []):
        for e in (s.stock or []):
            it = item_by_id.get(e.item_id)
            if it is not None and names_match(target_name, it.name) and int(e.stock or 0) > 0:
                shop_hits.append(f"{loc_by_id.get(s.location_id, '?')}·{s.name}(余{e.stock})")
    if shop_hits:
        return "有售：" + "、".join(shop_hits[:limit])
    # 2) 资源点掉落
    node_hits = []
    for l in (world.locations or []):
        for rn in (l.resource_nodes or []):
            for d in (rn.drops or []):
                did = d.get("item_id") if isinstance(d, dict) else d
                it = item_by_id.get(did)
                if it is not None and names_match(target_name, it.name):
                    # [修 2026-10-02 真机验收 UX] 节点名≠目标物品名时标注可获物——
                    # 「死亡爪峡谷·死亡爪蛋」节点产出其实是「变异孢子」，不标注玩家
                    # 看提示不知道采它会出目标。
                    tag = "" if str(rn.name or "") == it.name else f"（可获{it.name}）"
                    node_hits.append(f"{l.name}·{rn.name}{tag}")
                    break
    if node_hits:
        return "可采集：" + "、".join(node_hits[:limit])
    # 3) NPC 随身（barter 可换）
    npc_hits = []
    for n in (world.npcs or []):
        for iid in (n.inventory or []):
            it = item_by_id.get(iid)
            if it is not None and names_match(target_name, it.name):
                npc_hits.append(n.name)
    if npc_hits:
        return "可向这些人换取/打听：" + "、".join(npc_hits[:limit])
    return ""


def objective_text(obj: dict) -> str:
    """单个目标的展示文本（供 UI/叙事）。"""
    if not isinstance(obj, dict):
        return ""
    t = obj.get("type", "")
    target = obj.get("target", "")
    cur = int(obj.get("current", 0) or 0)
    cnt = int(obj.get("count", 1) or 1)
    desc = obj.get("desc", "")
    if desc:
        return f"{desc}（{cur}/{cnt}）"
    verb = _OBJ_LABELS.get(t, "完成")
    tgt2 = str(obj.get("target2", "") or "")
    if t == "investigate" and tgt2:
        return f"向「{target}」打听线索，再前往「{tgt2}」查证 {cur}/{cnt}"
    if t == "escort" and tgt2:
        return f"与「{target}」同行，护送到「{tgt2}」 {cur}/{cnt}"
    tgt = f"「{target}」" if target else ""
    return f"{verb}{tgt} {cur}/{cnt}"


# ---- [P42c 用户指示 2026-08-23] 日常跑腿任务：城市传话/捎物小差事，简单奖励 ----
# 开局不打怪的替代玩法：跑腿赚小钱 + 发布人交情。engine 生成（零 LLM），talk 目标
# 走既有 update_progress 钩子自动推进；claim 当面领奖（giver_present 校验复用）。

_GENRE_ERRAND_TEMPLATES: dict[str, list[str]] = {
    "xianxia": [("传话", "替{a}给{b}捎一句口信", "顺路给{b}带句话"),
                ("捎物", "替{a}把一包灵草捎给{b}", "一包待送的灵草"),
                ("传话", "替{a}向{b}询问一味药引的下落", "问个信儿"),
                ("捎物", "把{a}修好的一柄旧剑送还{b}", "送还旧剑")],
    "wuxia": [("传话", "替{a}给{b}带一句江湖口信", "捎句口信"),
              ("捎物", "替{a}把一封信送到{b}手上", "一封要紧的信"),
              ("传话", "替{a}向{b}打听镖期的变动", "打听镖期"),
              ("捎物", "把{a}订做的护腕捎给{b}", "捎一对护腕")],
    "modern": [("传话", "替{a}给{b}带个话", "带句话"),
               ("捎物", "替{a}把一份文件带给{b}", "一份文件"),
               ("传话", "替{a}通知{b}开会改期", "通知改期"),
               ("捎物", "把{a}落在店里的伞捎给{b}", "捎一把伞")],
    "scifi": [("传话", "替{a}给{b}传一条讯息", "传条讯息"),
              ("捎物", "替{a}把一个数据晶片交给{b}", "一枚数据晶片"),
              ("传话", "替{a}向{b}确认班次变更", "确认班次"),
              ("捎物", "把{a}的备用零件捎给{b}", "捎备用零件")],
    "apocalypse": [("传话", "替{a}给{b}捎个信", "捎个信"),
                   ("捎物", "替{a}把半袋口粮带给{b}", "半袋口粮"),
                   ("传话", "替{a}通知{b}换岗时间", "通知换岗"),
                   ("捎物", "把{a}攒的药品捎给{b}", "捎几支药品")],
    "western_fantasy": [("传话", "替{a}给{b}带句口信", "带句口信"),
                        ("捎物", "替{a}把一个包裹送到{b}手上", "一个包裹"),
                        ("传话", "替{a}向{b}确认商队行程", "确认行程"),
                        ("捎物", "把{a}修好的马具送还{b}", "送还马具")],
}

_ERRANDS_PER_SETTLEMENT = 2
_ERRAND_GOLD = (20, 60)
_ERRAND_XP = (10, 30)


def maybe_spawn_errands(world, rng, tick: int) -> list:
    """每日跑腿生成（day 水位 last_errand_day）：每聚落 1-2 条传话/捎物。

    昨日未完成的跑腿自动 abandoned（日常差事过期作废，不占任务栏）；奖励 = 小额
    金币（乘 economy_pace）+ 少量经验 + 领奖时发布人交情 +3（claim_reward 内）。"""
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    if int(getattr(world, "last_errand_day", 0) or 0) == day:
        return []
    world.last_errand_day = day
    # 旧跑腿清出任务表（每日差事只当日有效：available/active/completed 未领一律作废，
    # 连同历史 abandoned/claimed 一起移除——[审查修复] 只标 abandoned 不清理会按
    # 每聚落每日 1-2 条线性堆积死任务，长线存档任务表无限膨胀）。
    quests = getattr(world, "quests", None)
    if isinstance(quests, list):
        quests[:] = [q for q in quests if getattr(q, "chain", "") != "errand"]
    from src.models.world import Quest
    ov = getattr(world, "config_overlay", None) or {}
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    pool = _GENRE_ERRAND_TEMPLATES.get(tid) or _GENRE_ERRAND_TEMPLATES["western_fantasy"]
    alive = [n for n in (getattr(world, "npcs", None) or []) if getattr(n, "alive", False)]
    settlements = [l for l in (getattr(world, "locations", None) or [])
                   if getattr(l, "kind", "") == "settlement"]
    for loc in settlements:
        locals_ = [n for n in alive if n.location_id == loc.id]
        if len(locals_) < 1:
            continue
        others = [n for n in alive if n not in locals_] or locals_
        if not others:
            continue
        for _ in range(rng.roll(1, _ERRANDS_PER_SETTLEMENT)):
            issuer = rng.pick(locals_)
            target = rng.pick([n for n in others if n.id != issuer.id] or others)
            if target.id == issuer.id:
                continue
            kind, title_tpl, obj_desc = pool[rng.roll(0, len(pool) - 1)]
            gold = rng.roll(*_ERRAND_GOLD)
            xp = rng.roll(*_ERRAND_XP)
            world.quests.append(Quest(
                title=title_tpl.format(a=issuer.name, b=target.name),
                objective=obj_desc.format(b=target.name),
                giver_npc_id=issuer.id,
                reward_text=f"{gold} {_currency(world)} + {xp} 经验 + {issuer.name}的交情",
                chain="errand",
                objectives=[{"type": "talk", "target": target.name, "count": 1,
                             "current": 0, "desc": f"找到{target.name}完成{kind}"}],
                rewards={"gold": gold, "xp": xp, "items": []},
            ))
    return []


# ---- [事件任务 2026-09-06 用户指示] major 事件自动派生后续任务（纯引擎零 LLM）----
# 规则表：扫 event_log 增量（水位 last_event_quest_tick），major/crisis 事件按关键词
# 匹配规则 -> 组装 Quest（objectives 复用五类钩子）。频率闸：距上次 >=7 天且在途
# 事件任务 <=2（防任务爆炸）。文案走题材化模板（GenreText 货币名）。
def _evt_quests_on(world) -> bool:
    ov = getattr(world, "config_overlay", None)
    return bool(ov.get("event_quests_enabled", False)) if isinstance(ov, dict) else False


def _active_event_quests(world) -> list:
    # 事件任务识别：giver_npc_id 带 "evt_" 前缀的派生任务（giver 空/前缀均可领奖口径不冲突——
    # giver_present 对不存在 id 豁免）
    return [q for q in (getattr(world, "quests", None) or [])
            if str(getattr(q, "giver_npc_id", "")).startswith("evt_")
            and getattr(q, "status", "") in ("available", "active", "completed")]


def maybe_generate_event_quests(world, current_tick: int) -> list:
    """[事件任务] tick 日水位扫描 major/crisis 事件 -> 派生任务。返回新产 WorldEvent 通知。

    三条起步规则：势力夺城->复国讨伐；商人死亡->货品托付；据点失守->重建征集。
    挂 tick_world（_phase 阶段隔离）；确定性（无 rng 消费——挑目标用 max/first 稳定序）。"""
    if not _evt_quests_on(world):
        return []
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    last_day = int(getattr(world, "last_event_quest_day", 0) or 0)
    last_tick = int(getattr(world, "last_event_quest_tick", 0) or 0)
    if last_day > 0 and day - last_day < 7:
        return []                                  # 首次（0）不限，产过才起 7 天闸
    events = getattr(world, "event_log", None) or []
    fresh = [e for e in events if int(getattr(e, "tick", 0) or 0) > last_tick
             and str(getattr(e, "severity", "")) in ("major", "crisis")]
    if not fresh:
        return []
    # [修 2026-09-10 用户指示] 在途满 2 条时先「存着」：**先判在途、再记账**。原实现先推进
    # 水位（last_event_quest_day/tick）再判在途，导致这批 major/crisis 事件被永久消费——
    # 任务栏腾空后也不会再为它们派生，还白白烧掉 7 天名额。现在不动水位，等有空位再产
    # （fresh 以 tick > last_tick 判定，未记账的事件下个 tick 会原样再来）。
    if len(_active_event_quests(world)) >= 2:
        return []
    world.last_event_quest_day = day
    world.last_event_quest_tick = int(getattr(events[-1], "tick", 0) or 0) if events else current_tick
    from src.models.world_sim_preset import GenreText
    gt = GenreText(getattr(world, "config_overlay", None) or {})
    tick = int(current_tick or 0)
    made: list = []
    notes: list = []

    def _add(title, objective, giver, objs, rewards, reward_text):
        q = Quest(title=title, objective=objective, giver_npc_id=giver,
                  reward_text=reward_text, objectives=objs, rewards=rewards,
                  status="available", started_at_tick=tick)
        world.quests.append(q)
        made.append(q)

    for e in fresh:
        cat = str(getattr(e, "category", "") or "")
        title = str(getattr(e, "title", "") or "")
        desc = str(getattr(e, "desc", "") or "")
        blob = title + desc
        loc_ids = [str(x) for x in (getattr(e, "locations", None) or [])]
        if cat == "faction_war" and any(k in blob for k in ("攻陷", "夺下", "易主", "占领")) \
                and not made:
            # 复国讨伐：击杀该敌对势力战斗成员 x3 + 重访失陷城
            loc = next((l for l in (getattr(world, "locations", None) or [])
                        if l.id in loc_ids), None)
            if loc is None:
                continue
            foes = [n for n in (getattr(world, "npcs", None) or [])
                    if getattr(n, "alive", True) and getattr(n, "hostile", False)
                    and getattr(n, "faction_id", "")]
            if not foes:
                continue
            fac_id = foes[0].faction_id
            fac_name = next((f.name for f in (getattr(world, "factions", None) or [])
                             if f.id == fac_id), "敌军")
            giver = next((n.id for n in (getattr(world, "npcs", None) or [])
                          if getattr(n, "alive", True) and getattr(n, "is_key_npc", False)
                          and not getattr(n, "hostile", False)), "")
            _add(f"复国之刃：讨伐{fac_name}",
                 f"「{loc.name}」失陷于{fac_name}之手。击杀{fac_name}的战斗成员 3 名，"
                 f"并重返「{loc.name}」宣示反击。",
                 giver or "evt_faction",
                 [{"type": "kill", "target": fac_name, "count": 3, "current": 0,
                   "desc": f"击杀{fac_name}战斗成员（0/3）"},
                  {"type": "visit", "target": loc.name, "count": 1, "current": 0,
                   "desc": f"到达「{loc.name}」"}],
                 {"gold": 220, "xp": 60},
                 f"220 {gt.currency} + 60 经验 + 该势力声望上浮")
            notes.append(f"{loc.name}失陷后有志之士发出了讨伐{fac_name}的征集")
        elif ("陨落" in blob or "击败" in title) and (getattr(e, "npcs", None) or []) \
                and cat in ("npc", "event") and not made:
            # 货品托付：逝去商人的未竟补货 -> 玩家代为收集
            dead_id = str((getattr(e, "npcs", None) or [""])[0])
            dead = next((n for n in (getattr(world, "npcs", None) or []) if n.id == dead_id), None)
            if dead is None or "商" not in f"{getattr(dead, 'role', '')}{getattr(dead, 'goal', '')}":
                continue
            mats = [i for i in (getattr(world, "items", None) or [])
                    if getattr(i, "type", "") == "material" and getattr(i, "rarity", "") == "common"]
            if not mats:
                continue
            mat = mats[0]
            _add(f"未竟的托付：{dead.name}的货源",
                 f"商贾{dead.name}离世，一批要补的货没了着落。收集 {mat.name} x3 交付给"
                 f"城中同行，完成这笔未竟的生意。",
                 "evt_merchant",
                 [{"type": "collect", "target": mat.name, "count": 3, "current": 0,
                   "desc": f"收集{mat.name}（0/3）"}],
                 {"gold": 120, "xp": 30},
                 f"120 {gt.currency} + 30 经验")
            notes.append(f"有人提起了{dead.name}未完成的生意")
        elif title == "据点失守" and not made:
            mats = [i for i in (getattr(world, "items", None) or [])
                    if getattr(i, "type", "") == "material"
                    and (getattr(i, "category", "") in ("forge", "craft")
                         or getattr(i, "rarity", "") == "common")]
            if not mats:
                continue
            mat = mats[0]
            _add("废墟上的重建",
                 f"据点遭劫，断壁残垣亟待修缮。收集 {mat.name} x4 送到据点，"
                 f"帮守军把家园重新立起来。",
                 "evt_domain",
                 [{"type": "collect", "target": mat.name, "count": 4, "current": 0,
                   "desc": f"收集{mat.name}（0/4）"}],
                 {"gold": 150, "xp": 40},
                 f"150 {gt.currency} + 40 经验 + 据点守军感激")
            notes.append("据点的废墟等着修缮的材料")
    if not made:
        return []
    from src.models.world import WorldEvent
    return [WorldEvent(tick=tick, category="quest", severity="minor",
                       title="事件的余波", desc="；".join(notes) + "（任务日志可查）")]
