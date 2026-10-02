"""[P10] 同场景 NPC 反应引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

需求来源：read.md「同场景 NPC 互动」——关系足够好时，玩家行动后同场景 NPC 也可以行动：
看见玩家在场会上前打招呼；聊天完成后可能送给玩家一些东西；高交情 NPC 会有氛围小行动。
非同场景 NPC 走世界滴答（world_tick_engine，互不重叠：滴答只处理 location != player 的 NPC）。

数值范式铁律（守 §21b）：本引擎只做确定性 roll（SeededRng，同 world+tick+npc 同结果）
与结构变更（交情增减/物品转移），产出 narration_hint 引导叙事 LLM 据实描写，
LLM 不参与「是否反应/送什么」的动态判定（省 token，行为可复现可单测）。

交情档位（affinity 0-100，初始 0 陌生——[P16 用户指示] 原默认 20 改 0）：
  0-19 陌生 / 20-39 点头之交 / 40-59 相熟 / 60-79 朋友 / 80-100 挚友
  达好友门槛（默认 60）可添加好友（PlayerState.friend_npc_ids）。

交情增长口径（全部引擎确定性结算）：
  谈话/互动 +2、送礼 +2~9（apply_player_gift 按品级/爱好）、打招呼 +1、私聊 +1、
  任务领奖 +8（quest_engine 领奖处）、助战 +4。
"""
from __future__ import annotations

from typing import Any, Optional

from src.utils.rng import SeededRng
from src.services import name_resolver as nrs
from src.services import consequence_engine as cue


def _same_place(world, npc) -> bool:
    """[2026-08-28] 场所级同场判定（纯函数，供送礼/NPC 反应等本引擎模块用；
    service 层有 _same_place_as_player 同口径实现）。"""
    p = getattr(world, "player", None)
    if p is None or getattr(npc, "location_id", "") != getattr(p, "location_id", ""):
        return False
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if l.id == getattr(p, "location_id", "")), None)
    if loc is None or not getattr(loc, "places", None):
        return True                      # 无场所化地点：地点级即同场
    valid = {getattr(x, "id", "") for x in loc.places}
    default = getattr(loc, "default_place_id", "") or ""
    if default not in valid:
        default = getattr(loc.places[0], "id", "")
    def _pid_of(pid_field_owner):
        pid = (getattr(pid_field_owner, "place_id", "") or "").strip()
        return pid if pid in valid else default
    return _pid_of(p) == _pid_of(npc)


# ---- 交情档位（阈值降序，取首个 <= 的）----
_AFFINITY_LEVELS = (
    (80, "挚友"),
    (60, "朋友"),
    (40, "相熟"),
    (20, "点头之交"),
    (0, "陌生"),
)

# 氛围小行动池（高交情 NPC 玩家行动后「也在做事」的素材，供叙事 LLM 展开）
_AMBIENT_ACTIONS = (
    "凑近看了看你在做的事，低声议论了几句",
    "在附近替你留意着四周的动静",
    "停下手里的事，朝你这边望了望",
    "拍了拍你的肩膀，似乎想说些什么",
    "默默替你挡开了人群",
)

# 谈话后交情增量（每次 talk/interact 意图 +2，钳制 0-100）
_TALK_AFFINITY_GAIN = 2
_GREET_AFFINITY_GAIN = 1
# 打招呼最低交情（低于此 NPC 不认识玩家，不会主动上前）
_GREET_MIN_AFFINITY = 25
# 送礼最低交情（低于此还不到「愿意掏东西」的程度）
_GIFT_MIN_AFFINITY = 55
# 氛围行动最低交情
_AMBIENT_MIN_AFFINITY = 45


def affinity_level(affinity: int) -> str:
    """交情档位名（0-100 -> 陌生/点头之交/相熟/朋友/挚友）。"""
    try:
        v = int(affinity)
    except (TypeError, ValueError):
        v = 0
    for threshold, name in _AFFINITY_LEVELS:
        if v >= threshold:
            return name
    return "陌生"


def add_affinity(npc, delta: int) -> int:
    """增减 NPC 交情（钳制 0-100），返回改后的值。原地改 npc.affinity。"""
    try:
        cur = int(getattr(npc, "affinity", 0))
    except (TypeError, ValueError):
        cur = 0
    npc.affinity = max(0, min(100, cur + int(delta)))
    return npc.affinity


def touch_friend_interact(world, npc) -> None:
    """[P24c] 刷新最近互动日（好友交情衰减的水位线）。

    赠礼/私聊/同行/助战四处调用（路线图口径：谈话是萍水相逢不算深互动，不刷）；
    写 world.day_count 当前日，重置 tick_friends 的 30 天衰减计时。
    """
    if npc is None:
        return
    try:
        npc.last_interact_day = max(1, int(getattr(world, "day_count", 1) or 1))
    except (TypeError, ValueError, AttributeError):
        npc.last_interact_day = 1


def is_friend(world, npc) -> bool:
    """该 NPC 是否已是玩家好友。[!] getattr 防御：测试用 SimpleNamespace 模拟 player。"""
    return bool(npc) and npc.id in (getattr(world.player, "friend_npc_ids", None) or [])


def can_add_friend(world, npc, threshold: int = 60) -> bool:
    """交情达门槛且尚未是好友 -> 可添加好友。敌对/死亡 NPC 不可。
    [P47-C2 记恨闸] 恨你的人（trust <= -60）不想跟你做朋友——先缓和关系再来。"""
    if not npc or getattr(npc, "hostile", False) or not getattr(npc, "alive", True):
        return False
    if cue.trust_gate(npc):
        return False
    if is_friend(world, npc):
        return False
    try:
        aff = int(getattr(npc, "affinity", 0))
    except (TypeError, ValueError):
        aff = 20
    return aff >= max(0, min(100, int(threshold)))


# ---- [P10b] 同行（邀请 NPC 跟随移动，点对话退队）----
def is_companion(world, npc) -> bool:
    """该 NPC 是否正与玩家同行。[!] getattr 防御：测试用 SimpleNamespace 模拟 player。"""
    return bool(npc) and npc.id in (getattr(world.player, "companion_npc_ids", None) or [])


def is_fatigued(world, npc) -> bool:
    """[P25b] 同伴战斗疲劳中（参战后 8-14 tick：不可邀请同行/助战）。"""
    try:
        return int(getattr(npc, "companion_fatigue_until_tick", 0) or 0) > int(getattr(world, "tick_count", 0) or 0)
    except (TypeError, ValueError):
        return False


def can_invite_companion(world, npc, threshold: int = 50, max_followers: int = 2) -> bool:
    """可邀请同行：交情达门槛 + 未在队中 + 队伍未满 + 存活非敌对 + 与玩家同地点。

    战斗能力（level>0）不作要求：和平 NPC 也能结伴旅行（只是战斗时不会助战）。
    [P25b] 战斗疲劳中不可邀请（is_fatigued）。
    """
    if not npc or getattr(npc, "hostile", False) or not getattr(npc, "alive", True):
        return False
    # [P47-C2 记恨闸] 恨你的人不肯与你同行——谁愿意把后背交给仇人
    if cue.trust_gate(npc):
        return False
    if is_companion(world, npc):
        return False
    if is_fatigued(world, npc):
        return False
    # [P26a] 配偶豁免 companion 位限制：counting 时排除已是配偶的同行 NPC（限 1 名配偶常驻同行）
    from src.services import relation_engine as _re
    companions = getattr(world.player, "companion_npc_ids", None) or []
    effective = [cid for cid in companions if _re.stage_of(world, cid) != "spouse"]
    if len(effective) >= max(0, int(max_followers)):
        return False
    if not _same_place(world, npc):   # [2026-08-28] 场所级（原地点级）
        return False
    try:
        aff = int(getattr(npc, "affinity", 0))
    except (TypeError, ValueError):
        aff = 20
    return aff >= max(0, min(100, int(threshold)))


def invite_companion(world, npc) -> bool:
    """邀请 NPC 加入同行（调用方应先 can_invite_companion 校验）。返回是否成功。"""
    if npc is None or npc.id in (getattr(world.player, "companion_npc_ids", None) or []):
        return False
    world.player.companion_npc_ids.append(npc.id)
    touch_friend_interact(world, npc)   # [P24c] 同行算深互动，刷新衰减水位线
    return True


def dismiss_companion(world, npc) -> bool:
    """让 NPC 退队（留在当前地点，不再是同行）。返回是否成功。"""
    if npc is None or npc.id not in (getattr(world.player, "companion_npc_ids", None) or []):
        return False
    world.player.companion_npc_ids.remove(npc.id)
    return True


def _pick_gift_item(world, npc) -> Optional[Any]:
    """选一份礼物：优先 NPC 随身物品（消耗品/材料/饰品），否则从世界物品池挑
    common/uncommon 的消耗品/材料（玩家没有的）。找不到返回 None。"""
    # 1) NPC 随身物品
    for iid in list(getattr(npc, "inventory", []) or []):
        it = next((i for i in world.items if i.id == iid), None)
        if it is not None and getattr(it, "type", "") in ("consumable", "material", "accessory"):
            return it
    # 2) 世界池：玩家未持有的低品级消耗品/材料
    owned = set(world.player.inventory or [])
    pool = [i for i in world.items
            if getattr(i, "type", "") in ("consumable", "material")
            and (getattr(i, "rarity", "common") or "common") in ("common", "uncommon")
            and i.id not in owned]
    if not pool:
        return None
    rng = SeededRng.seed_from(world.id, int(world.tick_count or 0), f"gift_pick_{npc.id}")
    return rng.pick(pool)


# ---- [P16] 玩家送礼（NPC 送玩家的对称渠道；纯代码确定性结算）----
# 交情增量 = 基础 2 + 品级加值（common 2 -> legendary 6）+ 爱好命中 +3。
# 参照口径：谈话 +2 / 任务领奖 +8；稀有 + 爱好命中 = 9，与领奖同级（好礼物该有分量）。
_GIFT_BASE_AFFINITY = 2
_GIFT_RARITY_AFFINITY = {"common": 2, "uncommon": 3, "rare": 4, "epic": 5, "legendary": 6, "mythic": 8}
_GIFT_HOBBY_BONUS = 3


# 爱好字级匹配时忽略的泛用字（防「下棋」撞「下品药材」这类假阳性）
_HOBBY_STOP_CHARS = set("的之人物上下中好坏小大各种品类")


def hobby_match(npc, item) -> bool:
    """玩家送礼的爱好匹配：NPC 任一爱好与物品相关。

    两级匹配（LLM 爱好是自由短语，如「品茶」，物品名常写「雨前龙井/茶叶」）：
    1. 爱好整词出现在物品名/描述/类型里（最强信号）；
    2. 字级交集：爱好（>=2 字）与物品名/描述共享任一非泛用字（品茶 vs 茶叶 -> 茶 命中）。
    [!] 匹配不到不影响送礼，只是少了 +3 加成；宁可漏配不误配（泛用字已滤）。
    """
    hobbies = [str(h).strip() for h in (getattr(npc, "hobbies", None) or []) if str(h).strip()]
    if not hobbies:
        return False
    name = str(getattr(item, "name", "") or "")
    desc = str(getattr(item, "desc", "") or "")
    typ = str(getattr(item, "type", "") or "")
    full = " ".join((name, desc, typ))
    name_desc = name + desc
    for h in hobbies:
        if h in full:
            return True
        if len(h) >= 2:
            for ch in h:
                if ch not in _HOBBY_STOP_CHARS and ch in name_desc:
                    return True
    return False


def apply_player_gift(world, npc, item) -> dict:
    """[P16] 玩家把背包物品送给 NPC：物品转移（玩家 -> NPC）+ 交情增益。返回摘要 dict。

    校验：物品须在玩家背包；key 类物品不可送（任务关键道具）；NPC 存活非敌对。
    成功返回 {"ok": True, "item_name", "gain", "affinity", "hobby_matched", "narration_hint"}；
    失败返回 {"ok": False, "reason"}。纯 Python 无 LLM（守数值范式）。
    """
    if item is None or getattr(item, "id", "") not in (world.player.inventory or []):
        return {"ok": False, "reason": "该物品不在你的背包里"}
    if getattr(item, "type", "") == "key":
        return {"ok": False, "reason": "关键物品不可送人"}
    if npc is None or getattr(npc, "hostile", False) or not getattr(npc, "alive", True):
        return {"ok": False, "reason": "无法赠予对方"}
    # [D3 2026-08-29] 技能书全局闸：对方技能满 3 门收了也学不会（研读同口径拒），
    # 送礼入口直接拦下并提示（书留给用得上的人）。
    if getattr(item, "teach_skill", None):
        from src.services.npc_life_engine import NPC_SKILL_CAP
        _skills = npc.skills if isinstance(getattr(npc, "skills", None), list) else []
        if len(_skills) >= NPC_SKILL_CAP:
            return {"ok": False,
                    "reason": f"对方已参悟满 {NPC_SKILL_CAP} 门技艺，此书赠了也是明珠暗投"}
        _ts = item.teach_skill if isinstance(item.teach_skill, dict) else {}
        if _ts.get("name") and any(isinstance(s, dict) and s.get("name") == _ts.get("name")
                                   for s in _skills):
            return {"ok": False,
                    "reason": f"对方已掌握「{_ts.get('name')}」，换一本Ta没读过的吧"}
    # [用户定稿 2026-08-28] 场所级同场校验（与在场 NPC/交易口径统一）：
    # 同地点不同场所不可当面送礼；无场所地点（野外）自动退化地点级。
    if not _same_place(world, npc):
        return {"ok": False, "reason": f"对方不在当前场所（在 {(getattr(npc, 'place_id', '') and '别处场所') or '别处地点'}），当面才能送礼"}
    # [I4 修复 2026-08-25] NPC 背包容量门（与 npc_life_engine._NPC_INV_CAP 同口径）：
    # 满员拒收，防绕过上限把 NPC 背包撑到 9+ 件、下游 _inv_add 对其永久返 False 半停摆
    from src.services.npc_life_engine import _NPC_INV_CAP
    if len(npc.inventory or []) >= _NPC_INV_CAP:
        return {"ok": False, "reason": "对方随身物品已经拿不下了，改日再送吧"}
    world.player.inventory.remove(item.id)
    if item.id not in (npc.inventory or []):
        npc.inventory.append(item.id)
    try:
        gain = _GIFT_BASE_AFFINITY + _GIFT_RARITY_AFFINITY.get(item.rarity, 2)
    except (AttributeError, TypeError):
        gain = _GIFT_BASE_AFFINITY + 2
    matched = hobby_match(npc, item)
    if matched:
        gain += _GIFT_HOBBY_BONUS
    new_aff = add_affinity(npc, gain)
    touch_friend_interact(world, npc)   # [P24c] 赠礼算深互动，刷新衰减水位线
    hint = (f"你把「{item.name}」送给了{npc.name}，对方收下了（交情 +{gain}"
            + ("，正合其爱好，格外欢喜" if matched else "")
            + f"，现交情 {new_aff}）")
    # [P26a] 定情信物跃迁：送出 is_courtship_gift + 交情>=90 + 已结义(sworn) -> 恋人(sweetheart)
    extra = {}
    if bool(getattr(item, "is_courtship_gift", False)):
        from src.services import relation_engine as _re
        if _re.stage_of(world, npc.id) == "sworn" and new_aff >= 90:
            sweetheart_hint = _re.make_sweetheart(world, npc)
            hint += f"；{sweetheart_hint}"
            extra["sweetheart"] = True
    # [玩家印象 2026-09-06] 亲眼互动：收礼者对玩家记「慷慨」
    try:
        from src.services import social_engine as _se
        _se.update_impression(npc, "慷慨", trust_delta=5, firsthand=True)
    except Exception:
        pass
    out = {"ok": True, "item_name": item.name, "gain": gain, "affinity": new_aff,
           "hobby_matched": matched, "narration_hint": hint}
    out.update(extra)
    return out


def maybe_npc_reactions(world, scene, intent: dict, summary: dict,
                        preset: Optional[Any] = None) -> list[dict]:
    """玩家行动结算后调（apply_intent 末尾）：同场景 NPC 的主动反应。

    只处理与玩家同场所的存活 NPC（敌对 NPC 不参与友好反应；其攻击行为走战斗系统）。
    产出 reactions list 并把文本追加到 summary["narration_hint"]（叙事 LLM 据此描写），
    同时把结构变更（交情/背包/好友候选提示）落到 world。确定性（SeededRng），无 LLM。
    """
    reactions: list[dict] = []
    if preset is not None and not getattr(preset, "npc_reactions_enabled", True):
        return reactions
    if not world or not intent or not intent.get("resolved", True):
        return reactions
    loc = None
    for l in world.locations:
        if l.id == world.player.location_id:
            loc = l
            break
    if loc is None:
        return reactions
    tick = int(world.tick_count or 0)
    itype = intent.get("intent_type", "custom")
    talked_name = (intent.get("talk_to") or "").strip()
    moved = bool(summary.get("moved"))
    gift_chance = 0.25
    if preset is not None:
        try:
            gift_chance = float(getattr(preset, "npc_gift_chance", 0.25))
        except (TypeError, ValueError):
            gift_chance = 0.25
    gift_chance = max(0.0, min(1.0, gift_chance))

    from src.services import combat_engine as ce  # 延迟 import 防循环

    # [P44 修复 2026-09-25] 玩家点名的谈话对象走容错解析（锚定**在场**名单）：
    # 原 `talked_name == npc.name` 精确等值——玩家带称呼（「张老板」vs 全名「张三」）时
    # 「交谈涨交情 / 回礼」整条静默失效（无报错、无反应，玩家只觉「聊了怎么没反应」）。
    # 只在本地点在场的存活非敌对 NPC 里解析，编造名不追认（守 [P44] 单一来源口径）。
    _talked_npc = None
    if talked_name:
        _pool = [n for n in (world.npcs or [])
                 if _same_place(world, n) and getattr(n, "alive", True)
                 and not getattr(n, "hostile", False)]
        _hit = nrs.resolve_name(talked_name, [n.name for n in _pool]) if _pool else None
        if _hit:
            _talked_npc = next((n for n in _pool if n.name == _hit), None)

    for npc in world.npcs:
        if not _same_place(world, npc) or not getattr(npc, "alive", True):
            continue
        if getattr(npc, "hostile", False):
            continue  # 敌对 NPC 的「反应」是战斗，不在此处理
        aff = int(getattr(npc, "affinity", 0))
        _is_talked = _talked_npc is not None and _talked_npc.id == npc.id

        # ---- 谈话对象：交情 +2（每次谈话都涨，交情靠互动积累）----
        if itype in ("talk", "interact", "quest") and _is_talked:
            add_affinity(npc, _TALK_AFFINITY_GAIN)
            reactions.append({"npc_id": npc.id, "npc_name": npc.name, "kind": "talk",
                              "text": f"与{npc.name}的交谈让你们的关系更近了一步（交情 {npc.affinity}）"})
            aff = npc.affinity

        # ---- 打招呼：玩家刚移动到 NPC 所在地，交情够高可能上前打招呼 ----
        if moved and itype == "move" and aff >= _GREET_MIN_AFFINITY:
            rng = SeededRng.seed_from(world.id, tick, f"greet_{npc.id}")
            if rng.chance(min(0.55, 0.12 + aff * 0.005)):
                add_affinity(npc, _GREET_AFFINITY_GAIN)
                friend_tag = "老朋友般" if aff >= 60 else ""
                text = f"{npc.name}看见你，{friend_tag}主动上前打招呼"
                reactions.append({"npc_id": npc.id, "npc_name": npc.name, "kind": "greet", "text": text})

        # ---- 送礼：刚与该 NPC 聊完且交情够高，可能送玩家东西 ----
        if (itype in ("talk", "interact") and _is_talked
                and aff >= _GIFT_MIN_AFFINITY):
            rng = SeededRng.seed_from(world.id, tick, f"gift_{npc.id}")
            # 交情越高越可能掏东西（55 交情约 0.35x 概率，100 交情满额）
            scaled = gift_chance * (0.5 + aff / 200.0)
            if rng.chance(scaled):
                it = _pick_gift_item(world, npc)
                if it is not None:
                    # 负重检查（与采集同口径）：满则不送（提示留待下回）
                    if len(world.player.inventory or []) >= ce.carry_capacity(world.player):
                        reactions.append({"npc_id": npc.id, "npc_name": npc.name, "kind": "gift_skip",
                                          "text": f"{npc.name}似乎想送你些什么，但你的背包已经装不下"})
                    elif it.id in (world.player.inventory or []):
                        # [I5 修复 2026-08-25] 玩家已持有同款：不重复入包（防重复 item_id
                        # 让下游去重口径永久打架），本次静默不送
                        pass
                    else:
                        world.player.inventory.append(it.id)
                        if it.id in (npc.inventory or []):
                            npc.inventory.remove(it.id)
                        text = f"{npc.name}聊得高兴，把「{it.name}」送给了你"
                        reactions.append({"npc_id": npc.id, "npc_name": npc.name, "kind": "gift",
                                          "text": text, "item_id": it.id, "item_name": it.name})

        # ---- 氛围行动：高交情 NPC 在玩家行动时也「在做自己的事」----
        if aff >= _AMBIENT_MIN_AFFINITY:
            rng = SeededRng.seed_from(world.id, tick, f"ambient_{npc.id}")
            if rng.chance(0.18):
                act = rng.pick(list(_AMBIENT_ACTIONS))
                reactions.append({"npc_id": npc.id, "npc_name": npc.name, "kind": "ambient",
                                  "text": f"{npc.name}{act}"})

    if reactions:
        # 汇入 summary：结构化列表 + narration_hint（叙事 LLM 据实织入旁白）
        prev = summary.get("narration_hint") or ""
        joined = "；".join(r["text"] for r in reactions)
        summary["narration_hint"] = (prev + "；" + joined).lstrip("；") if prev else joined
        summary["npc_reactions"] = reactions
        summary["world_changed"] = True
    return reactions


# ============ [G02/R3 2026-09-30] NPC 记得你的近况：探伤赠药 ============
CARE_VISIT_COOLDOWN_DAYS = 7   # 每 NPC 冷却（同一人不会天天来）
CARE_AFFINITY_GATE = 50        # 相熟以上才登门（好友/交情>=50，与 P56/P57 同口径）


def _heal_item_in_bag(world, npc):
    """NPC 背包里第一件可赠的回血药（复用 nle._is_heal_potion 单一口径）。"""
    from src.services.npc_life_engine import _is_heal_potion
    for iid in list(getattr(npc, "inventory", None) or []):
        it = next((i for i in (getattr(world, "items", None) or [])
                   if getattr(i, "id", "") == str(iid)), None)
        if _is_heal_potion(it):
            return str(iid), it
    return "", None


def tick_care_visits(world) -> list:
    """[G02/R3 2026-09-30] 探伤赠药（Nemesis 式「NPC 记得你的近况」并付诸行动）。

    触发源全是真实状态（守计划 G02 第 2 条）：玩家带未痊愈的伤（injuries 未过期）、
    候选 NPC 存活非敌对、与玩家同地点、交情 >= CARE_AFFINITY_GATE、**背包里真有回血药**
    （不是旁白承诺——药从其背包真实扣出，A2 买药链路供血）。
    结算：try_add_to_inventory 入玩家包（满包则 NPC 留药改日再送，交情照涨——来访即情谊）；
    交情 +2 + minor 事件进动态栏（category="npc" 自动流经 P36c）。
    频率闸：每 NPC 7 天冷却（npc.last_care_day）+ 全图每日至多 1 次（world.last_care_day）
    ——避免「每次受伤全城同时探访」（守 G02 第 6 条）。零 LLM 零 rng，候选按
    （交情降序, npc.id）确定性挑选。返回 WorldEvent 列表。"""
    from src.models.world import WorldEvent
    from src.services.combat_engine import try_add_to_inventory

    p = getattr(world, "player", None)
    if p is None:
        return []
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    if int(getattr(world, "last_care_day", 0) or 0) >= day:
        return []                                   # 今日已有人探望
    injuries = [it for it in (getattr(p, "injuries", None) or [])
                if isinstance(it, dict)
                and int(it.get("until_day", 0) or 0) > day]
    if not injuries:
        return []                                   # 无未愈伤势不触发
    p_loc = str(getattr(p, "location_id", "") or "")
    cands = []
    for n in (getattr(world, "npcs", None) or []):
        if not getattr(n, "alive", True) or getattr(n, "hostile", False):
            continue
        if str(getattr(n, "location_id", "") or "") != p_loc:
            continue
        if not _same_place(world, n):
            continue
        _lcd = int(getattr(n, "last_care_day", 0) or 0)
        if _lcd > 0 and (day - _lcd) < CARE_VISIT_COOLDOWN_DAYS:
            continue
        aff = int(getattr(n, "affinity", 0) or 0)
        if aff < CARE_AFFINITY_GATE and str(n.id) not in (
                getattr(p, "friend_npc_ids", None) or []):
            continue
        iid, _it = _heal_item_in_bag(world, n)
        if not iid:
            continue
        cands.append((aff, str(n.id), n, iid))
    if not cands:
        return []
    cands.sort(key=lambda t: (-t[0], t[1]))
    _aff, _nid, npc, iid = cands[0]
    npc.last_care_day = day
    world.last_care_day = day
    it = next((i for i in (getattr(world, "items", None) or [])
               if getattr(i, "id", "") == iid), None)
    name = str(getattr(it, "name", iid)) if it is not None else iid
    delivered = ""
    if try_add_to_inventory(p, iid):
        npc.inventory = [x for x in (getattr(npc, "inventory", None) or [])
                         if str(x) != str(iid)]        # NPC 背包真实扣出（守恒）
        delivered = f"，留下了一剂「{name}」"
        note = f"「{name}」已入你的背包（来自{npc.name}的相赠）"
    else:
        delivered = f"，本想留下「{name}」但你的行囊已满（他先收回了，改日再来）"
        note = "行囊已满，药留在对方处"
    add_affinity(npc, 2)
    inj_names = "、".join(str(i.get("name", "伤处")) for i in injuries[:2])
    ev = WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="npc",
        severity="minor",
        title=f"探望·{npc.name}"[:64],
        desc=f"{npc.name}听闻你带着伤（{inj_names}），亲自携药前来探望{delivered}。"
             f"（交情 +2；{note}）",
        npcs=[str(npc.id)], locations=[p_loc] if p_loc else [],
    )
    return [ev]


# ============ [G02 续/R3 2026-09-30] 结义之请（共同经历 -> 真实提议）============
def tick_sworn_proposals(world, preset=None) -> list:
    """共同战斗 >=3 场 + 交情达结义门槛 + 尚未结义 -> 经信使通道发「结义之请」信。

    [G02 验收线「结义者会针对共同经历提出一项真实委托」的引子：先有提议，后续共同
    目标挂在结义之后。] 反应源全是真实账目（shared_combats 由 finish_combat 逐场 +1、
    affinity/关系阶段真实）；信是提议不是结算——真结义仍走 NPC 面板（can_sworn 全门
    控复检，信不会造成旁白承诺先行）。去重：任一 pending「结义之请」存在即不再发；
    每日至多 1 封（world.last_sworn_proposal_day 水位）。零 LLM 零 rng。"""
    from src.services import relation_engine as reng
    from src.services import outreach_engine as ore
    from src.models.world import WorldEvent

    day = max(1, int(getattr(world, "day_count", 1) or 1))
    if int(getattr(world, "last_sworn_proposal_day", 0) or 0) >= day:
        return []
    for o in (getattr(world, "outreaches", None) or []):
        if getattr(o, "state", "") == "pending" and \
                str(getattr(o, "title", "") or "").startswith("结义之请"):
            return []                                    # 已有一封在途，不重复提议
    cands = []
    for n in (getattr(world, "npcs", None) or []):
        if not getattr(n, "alive", True) or getattr(n, "hostile", False):
            continue
        if int(getattr(n, "shared_combats", 0) or 0) < 3:
            continue
        if int(getattr(n, "affinity", 0) or 0) < reng.SWORN_AFFINITY:
            continue
        ok, _why = reng.can_sworn(world, n, preset)     # 全门控复检（阶段/开关/存活）
        if not ok:
            continue
        cands.append((int(getattr(n, "affinity", 0) or 0), str(n.id), n))
    if not cands:
        return []
    cands.sort(key=lambda t: (-t[0], t[1]))
    _a, _i, npc = cands[0]
    world.last_sworn_proposal_day = day
    sent = ore.push_system_letter(
        world, str(npc.id), "结义之请",
        f"并肩至今已历三阵，{npc.name}在信中提议与你八拜之交、结为异姓骨肉——"
        f"若你有意，去找{npc.name}当面盟誓（NPC 面板「结义」）。",
        payload={"kind": "sworn_proposal", "npc_id": str(npc.id)},
        expires_days=5)
    if not sent:
        return []
    return [WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="npc",
        severity="minor", title=f"来信·结义之请"[:64],
        desc=f"{npc.name}托人捎来一封信，提议与你结为异姓骨肉（场景页「信使」查看）。",
        npcs=[str(npc.id)],
    )]
