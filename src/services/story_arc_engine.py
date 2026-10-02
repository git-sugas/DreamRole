"""[P46 剧情线] Story Arc 引擎（纯 Python，零 LLM 零 Qt，可单测）。

世界的「前瞻导演层」：LLM 周期规划（或模板兜底）若干条剧情线（策划者 + 参与者 +
阶段序列），本引擎每日推进——阶段到期产事件（喂编年史/传闻/坊间热议三条既有消费
管线）、hook 派生玩家任务（隐性站边：claimed 计数）、终局软结算（势力 power、参与者
social；不夺地不杀 NPC 不动玩家钱包）。

铁律对齐：
- LLM 只出语义，引擎算数值：本文件所有数值变化只消费传入的 SeededRng（只用
  roll/chance/pick 三个方法），同 world+tick 双实例可回放。
- [!] 事件纪律「先判产出后标 done」（event_quests 先判在途再记账的教训）：阶段 done
  旗标必须在事件确实产出之后置位；arc 事件预算（每 tick <= ARC_EVENT_CAP_PER_TICK）
  不足时当日不置位、下 tick/次日重试。幂等靠 done 旗标，advance 每 tick 调用安全。
- [!] arc 事件 category 一律 "npc"：右侧动态栏 world_scene_tab._npc_log_entries 只渲染
  category=="npc"（"event" 玩家看不见）。
- [!] 阶段事件必须引擎回填 npcs/locations/factions 关联字段：rumor_engine.collect 只
  收编带关联字段的 major/crisis；locations 取当事参与者当下 location_id，禁采信 LLM 地名。
- [!] NPC 零新字段、零写 daily_goal/current_goal：arc 对 NPC 的一切可见性走
  arc_line_for_npc / arc_context_block 读时派生（零持久化、零新增 LLM 调用）。
- 死亡单向消费：mastermind 死 -> aborted；参与者死 -> 除名续跑。不夺
  Location.owner_faction_id / territory，不杀 NPC，不动玩家金币物品。
- 事件 severity 封顶 major（不产 crisis——crisis 会被 _maybe_crisis_quest 双轨追踪）。
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from src.models.world import (
    Quest, StoryArc, StoryStage, WorldEvent,
    _ARC_STAGE_KIND_VALUES, _gi,
)
from src.services import name_resolver as nrs
from src.services import outreach_engine as ore
from src.services import quest_engine as qe
from src.services import social_engine as soc
from src.services.story_arc_templates import _GENRE_ARC_TEMPLATES

# ---- 引擎常量（集中，便于测试钉值）----
ARC_EVENT_CAP_PER_TICK = 2      # 每 tick arc 事件硬闸（全局 max_events_per_tick=3 之下的自限）
TERMINAL_KEEP = 8               # 终态 arc 滚动保留（按 end_day 留最近，active 全保）
GRACE_DAYS = 2                  # 超时宽限：全段应完日 + 2 仍未终态 -> failed
HOOK_CHANCE_PER_TICK = 0.03     # hook 派生概率/tick（≈ 每日 30%：12 tick/天，1-0.97^12≈0.31）
HOOK_PER_ARC_LIVE = 1           # 单 arc 在途 hook 上限
HOOK_PER_ARC_LIFETIME = 2       # 单 arc 终身 hook 上限
HOOK_WORLD_LIVE = 3             # 全局在途 arc hook 上限
SMALL_WORLD_MIN_ALIVE = 6       # 小世界闸：全图存活 NPC 下限
SMALL_WORLD_MIN_KEY = 2         # 小世界闸：存活要角下限
_MAX_STAGE_DAYS = 5             # 单段天数钳制
_MAX_STAGES = 5
_MIN_STAGES = 2
_MAX_TOTAL_DAYS = 15            # 单线累计天数上限（超长截尾段）
_BRANCH_ARCHETYPES = frozenset(("faction_rivalry", "mediation", "trade_war"))

# LLM 允许的 hook objective 类型（不给 gather：资源 type 名不在名单注入面，易死任务）
# [S04/R2 2026-09-30] hook 目标类型扩三件：物资交付（deliver_items，物品名）+
# 秘境房探明/秘境攻克（dungeon_room_resolved/dungeon_cleared，秘境名）——
# 剧情线介入点从此能引用真实秘境与真实交付（守 §3.1/§3.2 验收线）。
_HOOK_OBJ_TYPES = ("kill", "talk", "visit", "collect", "deliver_items",
                  "dungeon_room_resolved", "dungeon_cleared")


# ============================================================
# 基础查询 / 读时派生注入（零持久化）
# ============================================================

def _npcs(world) -> list:
    return list(getattr(world, "npcs", None) or [])


def _npc_by_id(world) -> dict:
    return {str(n.id): n for n in _npcs(world)}


def _alive_npcs(world) -> list:
    return [n for n in _npcs(world) if getattr(n, "alive", True)]


def _day(world) -> int:
    return max(1, int(getattr(world, "day_count", 1) or 1))


def active_arcs(world) -> list:
    """status==active 的剧情线（保持 world.story_arcs 内的插入序）。"""
    return [a for a in (getattr(world, "story_arcs", None) or [])
            if isinstance(a, StoryArc) and a.status == "active"]


# [S02/F01 2026-09-30] 剧情线公开视图（世界详情页/议程共用只读查询，零 LLM 零 Qt）
_ARC_STATE_ZH = {"active": "进行中", "succeeded": "已成局", "failed": "已受挫",
                 "aborted": "已中断"}


def arc_public_view(world) -> list:
    """剧情线 -> 公开信息 dict 列表（active 在前、终态随后，各保持插入序）。

    保密口径（守开发计划 S02 第 4 条）：只给标题/前提/原型/人物/势力/当前段与已发生
    段名/起止日/介入任务标题；未来阶段只给计数（「后续 N 段尚未展开」），完整剧本
    不进 UI。策划者已死显示「（已故/未知）」不追认编造名。"""
    npc_by_id = {n.id: n for n in (getattr(world, "npcs", None) or [])}
    fac_by_id = {f.id: f for f in (getattr(world, "factions", None) or [])}
    quests = [q for q in (getattr(world, "quests", None) or []) if q is not None]

    def _view(a: StoryArc) -> dict:
        stages = [s for s in (a.stages or []) if isinstance(s, StoryStage)]
        mm = npc_by_id.get(str(a.mastermind_id or ""))
        parts = [npc_by_id.get(str(pid)) for pid in (a.participant_ids or [])]
        fac = fac_by_id.get(str(a.faction_id or ""))
        cur = stages[a.current_stage] if 0 <= a.current_stage < len(stages) else None
        # [F04] 已作废（failed/abandoned）的 hook 任务不再作为「相关任务」展示
        hooks = [q for q in quests
                 if str(getattr(q, "hook_arc_id", "") or "").startswith(f"{a.id}#")
                 and str(getattr(q, "status", "")) not in ("failed", "abandoned")]
        return {
            "id": a.id, "title": a.title, "premise": a.premise,
            "status": a.status,
            "state_zh": _ARC_STATE_ZH.get(a.status, a.status),
            "archetype_zh": (_ARC_ARCHETYPES.get(a.archetype, {})
                             .get("name", a.archetype or "未定")),
            "mastermind": mm.name if mm is not None else "（已故/未知）",
            "participants": [n.name for n in parts if n is not None],
            "faction": fac.name if fac is not None else "",
            "stage_idx": int(a.current_stage), "stage_total": len(stages),
            "stage_name": (cur.name if cur is not None else ""),
            "done_stage_names": [s.name for s in stages[:max(0, a.current_stage)]],
            # [S03/R2] 已发生段的结果标记（success 空 / compromise 妥协 / setback 受挫）
            "done_stage_outcomes": [str(s.outcome or "") for s in
                                    stages[:max(0, a.current_stage)]],
            "done_stage_days": [(int(s.resolved_tick) // 12 + 1)
                                if int(s.resolved_tick) >= 0 else 0
                                for s in stages[:max(0, a.current_stage)]],
            "future_stage_count": max(0, len(stages) - a.current_stage - (1 if cur else 0)),
            "start_day": int(a.start_day), "end_day": int(a.end_day),
            "outcome": str(a.outcome or ""), "player_helped": int(a.player_helped or 0),
            "player_opposed": int(a.player_opposed or 0),
            "hook_quest_titles": [str(q.title) for q in hooks],
        }

    arcs = [a for a in (getattr(world, "story_arcs", None) or [])
            if isinstance(a, StoryArc)]
    return ([_view(a) for a in arcs if a.status == "active"]
            + [_view(a) for a in arcs if a.status != "active"])


def gen_due(world, day: int, interval: int) -> bool:
    """规划水位：last==0（新世界第一天就有故事）或 day-last>=interval。"""
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 5
    interval = max(1, interval)
    last = int(getattr(world, "last_arc_day", 0) or 0)
    return last == 0 or int(day) - last >= interval


def _cur_stage(arc: StoryArc) -> Optional[StoryStage]:
    if not arc.stages:
        return None
    i = min(max(0, int(arc.current_stage)), len(arc.stages) - 1)
    return arc.stages[i]


def _stage_days(st: StoryStage) -> int:
    try:
        d = int(st.days)
    except (TypeError, ValueError):
        d = 2
    return max(1, min(_MAX_STAGE_DAYS, d))


def _stage_start_day(arc: StoryArc, i: int) -> int:
    return int(arc.start_day) + sum(_stage_days(s) for s in arc.stages[:i])


def _stage_due_day(arc: StoryArc, i: int) -> int:
    return _stage_start_day(arc, i) + _stage_days(arc.stages[i])


def _ensure_stage_schedule(arc: StoryArc) -> None:
    """阶段 ID 与截止回合只从原始顺序派生一次；续写不得增删/重排。"""
    arc.__post_init__()
    for idx, stage in enumerate(arc.stages or []):
        if isinstance(stage, StoryStage) and int(stage.due_tick) < 0:
            stage.due_tick = max(0, (_stage_due_day(arc, idx) - 1) * 12)


def _sanitize_stage_condition(world, arc: StoryArc, raw) -> dict:
    """LLM 只能提议“既有参与者死亡”这一实体变化，不执行表达式或编造对象。"""
    if not isinstance(raw, dict) or str(raw.get("type", "") or "") != "npc_dead":
        return {}
    people = [n for n in _alive_npcs(world)
              if str(n.id) in set(_arc_member_ids(arc))
              and str(n.id) != str(arc.mastermind_id)]
    names = [str(n.name) for n in people if getattr(n, "name", "")]
    hit = nrs.resolve_name(str(raw.get("target", "") or ""), names)
    target = next((n for n in people if n.name == hit), None)
    return {"type": "npc_dead", "target_id": str(target.id)} if target is not None else {}


def _arc_member_ids(arc: StoryArc) -> list:
    """策划者 + 参与者（去重保序）。"""
    ids: list = []
    for nid in [arc.mastermind_id] + list(arc.participant_ids or []):
        nid = str(nid or "")
        if nid and nid not in ids:
            ids.append(nid)
    return ids


def arcs_for_npc(world, npc_id: str) -> list:
    nid = str(npc_id or "")
    if not nid:
        return []
    return [a for a in active_arcs(world)
            if a.mastermind_id == nid or nid in (a.participant_ids or [])]


def arc_line_for_npc(world, npc_id: str) -> str:
    """NPC 的参与/目击/听说分级读时派生（<=48 字），不额外赋予幕后知识。

    三处注入共用此来源；不许写 NPC.daily_goal/current_goal（他主写权字段）。
    目击仅认同回合、同地点的已发布事件；听说只认 rumor_engine.knows 判定的传闻。
    """
    nid = str(npc_id or "")
    npc = _npc_by_id(world).get(nid)
    if npc is None or not getattr(npc, "alive", True):
        return ""
    arcs = sorted(active_arcs(world), key=lambda a: (int(a.start_day), str(a.id)))
    direct = [a for a in arcs if nid in _arc_member_ids(a)]
    if direct:
        a = direct[0]
        st = _cur_stage(a)
        if st is not None:
            return f"剧情线《{a.title}》：参与「{st.name}」；{str(st.desc)[:20]}"[:48]
    now = int(getattr(world, "tick_count", 0) or 0)
    loc = str(getattr(npc, "location_id", "") or "")
    recent_events = (getattr(world, "event_log", None) or [])[-24:]
    if loc:
        for a in arcs:
            prefix = f"剧情线·{a.title}："
            for ev in reversed(recent_events):
                if (int(getattr(ev, "tick", -1)) == now
                        and str(getattr(ev, "title", "") or "").startswith(prefix)
                        and loc in (getattr(ev, "locations", None) or [])):
                    return f"剧情线《{a.title}》：目击{str(ev.desc or '')[:24]}"[:48]
    from src.services import rumor_engine as rme
    for a in arcs:
        prefix = f"剧情线·{a.title}："
        for rumor in reversed((getattr(world, "shared_rumors", None) or [])[-24:]):
            if (isinstance(rumor, dict)
                    and str(rumor.get("title", "") or "").startswith(prefix)
                    and rme.knows(npc, rumor)):
                return f"剧情线《{a.title}》：听说{str(rumor.get('text', '') or '')[:24]}"[:48]
    return ""


def _arc_anchor_loc(world, arc: StoryArc):
    """锚定地点（远方行 / visit 目标 / aborted 事件兜底 location）：
    势力治下 danger 最高地 -> 参与者当下地点（策划者优先）-> 任意危险地。"""
    locs = {str(l.id): l for l in (getattr(world, "locations", None) or [])}
    if arc.faction_id:
        owned = [l for l in locs.values()
                 if getattr(l, "owner_faction_id", "") == arc.faction_id
                 or getattr(l, "faction_id", "") == arc.faction_id]
        if owned:
            return min(owned, key=lambda l: (-int(getattr(l, "danger", 0) or 0), str(l.id)))
    nid_by_id = _npc_by_id(world)
    for nid in _arc_member_ids(arc):
        n = nid_by_id.get(str(nid))
        if n is not None and getattr(n, "alive", True):
            loc = locs.get(str(getattr(n, "location_id", "") or ""))
            if loc is not None:
                return loc
    wilds = [l for l in locs.values() if int(getattr(l, "danger", 0) or 0) > 0]
    if wilds:
        return min(wilds, key=lambda l: (-int(getattr(l, "danger", 0) or 0), str(l.id)))
    return None


# ============================================================
# LLM 输出清洗（贴合度防线集中地）
# ============================================================

def _faction_min_rel(world, fid: str) -> Optional[float]:
    """势力 fid 对他方的最低双边平均关系（镜像 tick_faction_war :196-203 谓词）。"""
    facs = list(getattr(world, "factions", None) or [])
    f = next((x for x in facs if str(x.id) == str(fid)), None)
    if f is None:
        return None
    rels = []
    for g in facs:
        if str(g.id) == str(fid):
            continue
        rels.append((int(f.relations.get(g.id, 0) or 0)
                     + int(g.relations.get(f.id, 0) or 0)) / 2.0)
    return min(rels) if rels else None


def _conflict_grounded(world, faction_id: str) -> bool:
    """conflict 段是否有真实敌意背书：无势力（私人冲突）放行；有势力则要求其
    最低双边平均关系 < 0（关系全 >=0 的「盟友互殴」降级 scheme）。"""
    if not faction_id:
        return True
    mr = _faction_min_rel(world, faction_id)
    if mr is None:
        return True
    return mr < 0


def _enemy_faction(world, faction_id: str):
    """arc 势力最敌对的他方势力对象（双边平均最负，平手按 id 稳定序）；无负关系 None。"""
    facs = list(getattr(world, "factions", None) or [])
    f = next((x for x in facs if str(x.id) == str(faction_id)), None)
    if f is None:
        return None
    scored = []
    for g in facs:
        if str(g.id) == str(faction_id):
            continue
        rel = (int(f.relations.get(g.id, 0) or 0) + int(g.relations.get(f.id, 0) or 0)) / 2.0
        if rel < 0:
            scored.append((rel, str(g.id), g))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def _sanitize_hook(world: Any, raw: Any, alive_names: list, name2npc: dict) -> dict:
    """stage 可选 hook 清洗：type 白名单 + 目标存在性解析，解析不到 -> {}（丢 hook 保 stage）。

    [!] 绝不做泛化改写（event_quests 式「击杀{势力名}成员」只出现在引擎自派生模板）。
    """
    if not isinstance(raw, dict):
        return {}
    ot = str(raw.get("type", "") or "").strip()
    if ot not in _HOOK_OBJ_TYPES:
        return {}
    tgt = str(raw.get("target", "") or "").strip()
    if not tgt:
        return {}
    if ot in ("kill", "talk"):
        hit = nrs.resolve_name(tgt, alive_names)
        if hit is None or hit not in name2npc:
            return {}
        # [P46.1 v1.3 用户问询补防] kill 目标不得是主线发布人——主线发布人不死
        # （quest_engine.is_main_giver），击杀进度永不命中 → 死任务。
        if ot == "kill":
            from src.services.quest_engine import is_main_giver as _img
            _t = name2npc.get(hit)
            if _t is not None and _img(world, _t):
                return {}
        norm = hit
    elif ot == "visit":
        loc_names = [str(l.name) for l in (getattr(world, "locations", None) or [])
                     if getattr(l, "name", "")]
        norm = nrs.resolve_name(tgt, loc_names)
        if norm is None:
            return {}
    elif ot in ("dungeon_room_resolved", "dungeon_cleared"):
        # [S04] 秘境名锚定现存秘境（只认 open + discovered——封印/未发现不出现在介入点）
        dg_names = [str(d.name) for d in (getattr(world, "dungeons", None) or [])
                    if getattr(d, "name", "") and str(getattr(d, "status", "")) == "open"
                    and getattr(d, "discovered", True)]
        norm = nrs.resolve_name(tgt, dg_names)
        if norm is None:
            return {}
    elif ot == "deliver_items":
        # [S04] 交付目标同 collect 口径：现存 material 名（获取链路闭合才可交）
        item_names = [str(i.name) for i in (getattr(world, "items", None) or [])
                      if getattr(i, "name", "")
                      and getattr(i, "type", "") == "material"]
        norm = nrs.resolve_name(tgt, item_names)
        if norm is None:
            return {}
    else:  # collect：必须是 world.items 现存 material 名（锚定真实货架 + 三渠道可复得）
        # [P46.1 v1.3] 非 material 物品（装备/消耗品）可能无稳定获取渠道（不在商店/
        # 不在掉落表/不可采集）→ 限定 material（资源点可采集，获取链路闭合）。
        item_names = [str(i.name) for i in (getattr(world, "items", None) or [])
                      if getattr(i, "name", "")
                      and getattr(i, "type", "") == "material"]
        norm = nrs.resolve_name(tgt, item_names)
        if norm is None:
            return {}
    count = max(1, min(5, _gi(raw, "count", 3 if ot == "collect" else 1)))
    return {"type": ot, "target": str(norm)[:24], "count": count,
            "desc": str(raw.get("desc", "") or "")[:60],
            "stance": "oppose" if raw.get("stance") == "oppose" else "support"}


def sanitize_llm_arcs(world, data, rng, day, existing_titles,
                      assigned: Optional[list] = None) -> list:
    """LLM arcs JSON -> list[StoryArc]（非法条目静默丢弃，绝不抛）。

    防线顺序（定稿 §2）：人名 resolve_name 锚定存活名单 -> 势力解析（失败置空不丢线）
    -> conflict 无真实敌意降级 scheme -> kind/days/段数/总时长钳制 -> 标题查重
    -> hook 目标存在性校验。mastermind 解析不到 / 参与者解析后 <2 / 段数 <2 -> 丢整线。
    参与者去重、剔 mastermind、剔已在其它 active 线者；同人同批至多一条线。
    """
    if not isinstance(data, dict):
        return []
    raw_arcs = data.get("arcs")
    if not isinstance(raw_arcs, list):
        return []
    day = max(1, int(day))
    existing = {str(t) for t in (existing_titles or set()) if str(t)}
    alive = _alive_npcs(world)
    # 要角排前：resolve_name 平手时优先命中 is_key_npc（策划者担纲资格）
    alive_sorted = sorted(alive, key=lambda n: (not getattr(n, "is_key_npc", False), str(n.id)))
    alive_names = [n.name for n in alive_sorted if getattr(n, "name", "")]
    name2npc = {n.name: n for n in alive_sorted if getattr(n, "name", "")}
    fac_names = [f.name for f in (getattr(world, "factions", None) or []) if getattr(f, "name", "")]
    busy: set = set()
    for a in active_arcs(world):
        busy.update(_arc_member_ids(a))
    out: list = []
    for raw in raw_arcs:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "") or "").strip()[:48]
        if not title or title in existing:
            continue
        mm_hit = nrs.resolve_name(str(raw.get("mastermind", "") or ""), alive_names)
        if mm_hit is None or mm_hit not in name2npc:
            continue
        mm = name2npc[mm_hit]
        p_raw = raw.get("participants")
        if not isinstance(p_raw, list):
            continue
        resolved: list = []
        for pn in p_raw[:6]:
            h = nrs.resolve_name(str(pn), alive_names)
            if h is not None and h not in resolved:
                resolved.append(h)
        if len(resolved) < 2:
            continue
        parts: list = []
        for h in resolved:
            n = name2npc.get(h)
            if n is None:
                continue
            nid = str(n.id)
            if nid == str(mm.id) or nid in busy or nid in parts:
                continue
            parts.append(nid)
        if not parts:
            continue
        faction_id = ""
        if fac_names and str(raw.get("faction", "") or "").strip():
            f_hit = nrs.resolve_name(str(raw.get("faction", "") or ""), fac_names)
            if f_hit:
                faction_id = str(next((f.id for f in (getattr(world, "factions", None) or [])
                                       if f.name == f_hit), "") or "")
        # [P46.1 用户定稿] 原型指派：LLM 输出的 archetype 仅作提名，最终以冷却外
        # 可发性原型为准；**阶段 kind 由原型 seq 单一权威回填**——LLM 不再输出 kind，
        # 防同质化从源头收口（schema 亦不再要求 kind 字段）。
        # [P46.1 v1.2] 原型指派：LLM 回带的 archetype 有效则用之；缺省时从引擎本批
        # 指派（assigned，已过冷却+可发性）取一个；direct 调用（assigned=None）回退
        # _pick_archetypes。同批内不重复（防两线同原型）。
        arch_key = str(raw.get("archetype", "") or "").strip()
        if arch_key not in _ARC_ARCHETYPE_VALUES:
            arch_key = ""
        if not arch_key:
            if assigned:
                arch_key = assigned.pop(0)
            else:
                _p = _pick_archetypes(world, rng, 1)
                arch_key = _p[0] if _p else ""
        if not arch_key:
            continue
        assigned = [k for k in (assigned or []) if k != arch_key]
        seq = _ARC_ARCHETYPES[arch_key]["seq"]
        grounded = _conflict_grounded(world, faction_id)
        raw_st = raw.get("stages")
        if not isinstance(raw_st, list):
            continue
        stages: list = []
        for i, s in enumerate(raw_st[:_MAX_STAGES + 2]):
            if not isinstance(s, dict):
                continue
            kind = seq[i] if i < len(seq) else seq[-1]
            if kind == "conflict" and not grounded:
                kind = "scheme"          # 无据敌意降级（盟友互殴 OOC 防线）
            stages.append(StoryStage(
                name=(str(s.get("name", "") or "").strip() or "暗流")[:24],
                desc=str(s.get("desc", "") or "").strip()[:120],
                kind=kind,
                days=max(1, min(_MAX_STAGE_DAYS, _gi(s, "days", 2))),
                hook=_sanitize_hook(world, s.get("hook"), alive_names, name2npc),
                condition=(dict(s.get("condition")) if isinstance(s.get("condition"), dict)
                           else {}),
            ))
        if len(stages) < _MIN_STAGES:
            continue
        total = 0
        kept: list = []
        for s in stages:
            if kept and total + _stage_days(s) > _MAX_TOTAL_DAYS:
                break                    # 超长截尾段（保前段）
            total += _stage_days(s)
            kept.append(s)
        stages = kept[:_MAX_STAGES]
        if len(stages) < _MIN_STAGES:
            continue
        arc = StoryArc(
            title=title, premise=str(raw.get("premise", "") or "").strip()[:120],
            mastermind_id=str(mm.id), participant_ids=parts,
            stages=stages, start_day=day, faction_id=faction_id, source="llm",
            archetype=arch_key,
        )
        for st in stages:
            st.condition = _sanitize_stage_condition(world, arc, st.condition)
            if arch_key not in _BRANCH_ARCHETYPES and st.hook:
                st.hook["stance"] = "support"
        _ensure_stage_schedule(arc)
        # [P46 v1.1] 同批 arc 首段天数 +rng(0,1) 轻微错开——防多条线同日到期集中挤
        # max_events_per_tick 管道（GLM 建议采纳；rng 由此进入确定性链条）
        if out and stages:
            stages[0].days = max(1, min(_MAX_STAGE_DAYS, stages[0].days + rng.roll(0, 1)))
            # 首段错开后，重算所有尚未开始阶段的截止回合。
            for stage in stages:
                stage.due_tick = -1
            _ensure_stage_schedule(arc)
        out.append(arc)
        existing.add(title)
        busy.update(_arc_member_ids(arc))
    return out


# ============================================================
# 引擎兜底线：只从真实敌意取材（势力负关系 / 仇敌 social<=-60）
# ============================================================

def _worst_hostile_pair(world):
    """双边平均关系最负的势力对（镜像 tick_faction_war 谓词；无负关系 None）。
    返回按 faction.id 稳定序定向的 (fa, fb)。"""
    facs = list(getattr(world, "factions", None) or [])
    best = None
    for i, fa in enumerate(facs):
        for fb in facs[i + 1:]:
            rel = (int(fa.relations.get(fb.id, 0) or 0)
                   + int(fb.relations.get(fa.id, 0) or 0)) / 2.0
            if rel < 0:
                key = (rel, str(fa.id), str(fb.id))
                if best is None or key < best[0]:
                    best = (key, fa, fb)
    if best is None:
        return None
    fa, fb = best[1], best[2]
    return (fa, fb) if str(fa.id) <= str(fb.id) else (fb, fa)


def _worst_feud_pair(world) -> Optional[tuple]:
    """social 底账 <=-60 的存活仇敌 NPC 对（P38a 数据；取最负）。"""
    alive = _alive_npcs(world)
    best = None
    for i, a in enumerate(alive):
        for b in alive[i + 1:]:
            v = soc._social_get(a, str(b.id))
            if best is None or v < best[0]:
                best = (v, a, b)
    if best is None or best[0] > -60:
        return None
    return (best[1], best[2])


def _social_circle_ids(world) -> set:
    """[导演偏置 2026-09-26 [M] 项] 玩家社交圈 id 集（好友 + 交情>=50 存活）。

    与 P56 执念道贺圈 / P57 信使候选同一口径——「玩家的故事线要和玩家认识的人交织」，
    治第 40 小时+的「世界与我无关」。"""
    friend_ids = set(getattr(getattr(world, "player", None), "friend_npc_ids", None) or [])
    return {str(n.id) for n in _alive_npcs(world)
            if str(n.id) in friend_ids or int(getattr(n, "affinity", 0) or 0) >= 50}


def _fill_participants(world, mm, base_ids: list, busy: set, cap: int = 3) -> list:
    """兜底线参与者补足（[导演偏置] 优先社交圈 -> 同地熟人 -> 要角 -> 任意存活；
    剔重/剔 busy/剔 mm）。"""
    out = [str(x) for x in base_ids
           if str(x) and str(x) != str(mm.id) and str(x) not in busy]
    seen = set(out)
    circle = _social_circle_ids(world)
    others = [n for n in _alive_npcs(world)
              if str(n.id) != str(mm.id) and str(n.id) not in busy and str(n.id) not in seen]
    others.sort(key=lambda n: (0 if str(n.id) in circle else 1,
                               0 if getattr(n, "location_id", "") == getattr(mm, "location_id", "") else 1,
                               not getattr(n, "is_key_npc", False), str(n.id)))
    for n in others:
        if len(out) >= cap:
            break
        out.append(str(n.id))
    return out


# ---- [P46.1 2026-09-12 用户定稿] 剧情线原型体系（4 族 21 种）----
# seq = 阶段 kind 序列（引擎单一权威：sanitize 回填、模板按序对位）；
# hooks = 介入点偏好类型；axis = 软结算主轴（power/social/wealth/stability）；
# need = 可发性谓词（兜底取材前提，小世界按谓词降级）。
_ARC_ARCHETYPES: dict = {
    "faction_rivalry": {"name": "势力摩擦", "seq": ("scheme", "conflict", "conflict"),
                        "hooks": ("visit", "kill"), "axis": "power", "need": "hostile_pair"},
    "usurpation": {"name": "夺位阴谋", "seq": ("scheme", "scheme", "conflict"),
                   "hooks": ("talk", "visit"), "axis": "power", "need": "faction"},
    "dark_market": {"name": "暗市谍影", "seq": ("scheme", "econ", "conflict"),
                    "hooks": ("visit", "collect"), "axis": "power", "need": "shop_or_hostile"},
    "escort_raid": {"name": "护送劫夺", "seq": ("econ", "conflict", "conflict"),
                    "hooks": ("visit", "kill"), "axis": "wealth", "need": "hostile_pair"},
    "murder_case": {"name": "血案追凶", "seq": ("disaster", "scheme", "conflict"),
                    "hooks": ("talk", "kill"), "axis": "social", "need": "npcs4"},
    "traitor_hunt": {"name": "叛徒清查", "seq": ("scheme", "social", "conflict"),
                     "hooks": ("talk", "visit"), "axis": "power", "need": "faction"},
    "treasure_hunt": {"name": "寻宝探秘", "seq": ("scheme", "conflict", "econ"),
                      "hooks": ("visit", "collect"), "axis": "wealth", "need": "wilderness"},
    "love_entangle": {"name": "情爱纠葛", "seq": ("social", "social", "conflict"),
                      "hooks": ("talk", "collect"), "axis": "social", "need": "npcs3"},
    "mediation": {"name": "恩怨调解", "seq": ("social", "conflict", "social"),
                  "hooks": ("talk", "visit"), "axis": "social", "need": "feud_or_hostile"},
    "inheritance": {"name": "传承之争", "seq": ("social", "scheme", "conflict"),
                    "hooks": ("talk", "collect"), "axis": "social", "need": "faction"},
    "alliance": {"name": "联姻结盟", "seq": ("social", "scheme", "social"),
                 "hooks": ("talk", "collect"), "axis": "social", "need": "two_factions"},
    "trade_war": {"name": "商机争夺", "seq": ("econ", "scheme", "econ"),
                  "hooks": ("collect", "visit"), "axis": "wealth", "need": "shop"},
    "craft_arena": {"name": "匠心竞艺", "seq": ("econ", "social", "econ"),
                    "hooks": ("collect", "talk"), "axis": "wealth", "need": "recipes"},
    "disaster_resp": {"name": "灾祸应对", "seq": ("disaster", "social", "econ"),
                      "hooks": ("collect", "visit"), "axis": "stability", "need": "danger_loc"},
    "rare_cure": {"name": "疗救奇药", "seq": ("disaster", "econ", "social"),
                  "hooks": ("collect", "visit"), "axis": "stability", "need": "settlement"},
    "faith_strife": {"name": "信仰纷争", "seq": ("social", "conflict", "conflict"),
                     "hooks": ("talk", "visit"), "axis": "power", "need": "two_factions"},
    "rescue": {"name": "营救", "seq": ("conflict", "scheme", "social"),
               "hooks": ("visit", "collect"), "axis": "social", "need": "settlement"},
    "riot": {"name": "民变", "seq": ("social", "econ", "conflict"),
             "hooks": ("talk", "visit"), "axis": "stability", "need": "crowd"},
    "festival": {"name": "节庆竞会", "seq": ("social", "econ", "social"),
                 "hooks": ("visit", "collect"), "axis": "social", "need": "settlement"},
    "grand_work": {"name": "大工程", "seq": ("social", "econ", "social"),
                   "hooks": ("collect", "visit"), "axis": "power", "need": "settlement"},
    "scandal": {"name": "丑闻风波", "seq": ("scheme", "social", "social"),
                "hooks": ("talk", "visit"), "axis": "social", "need": "npcs4"},
}
_ARC_ARCHETYPE_VALUES = tuple(_ARC_ARCHETYPES)

# [P46.1] 结局句池：按成败取（rng 定句），防"所图竟成/所图成空"两句走天下（GLM 咨询）
_OUTCOME_POOL: dict = {
    "succeeded": ["尘埃落定", "局面就此定下", "风波终于平息", "各方的算盘都打到了明处",
                  "一桩心事就此了结", "这件事在酒桌上被讲成了传奇"],
    "failed": ["所图成空", "功败垂成", "风波过境，什么都没留下", "各方不欢而散",
               "这条线断在了最不该断的地方", "再没人愿意提起这件事"],
}

_COOLDOWN_WINDOW = 5


def _cooldown_archetypes(world) -> set:
    """[P46.1] 原型冷却：最近 N 条已规划线（含终态）的原型不重复——防"老是出一样的剧情线"。"""
    arcs = [a for a in (getattr(world, "story_arcs", None) or []) if isinstance(a, StoryArc)]
    arcs.sort(key=lambda a: (int(a.start_day), str(a.id)))
    recent = arcs[-_COOLDOWN_WINDOW:]
    return {str(a.archetype) for a in recent if str(getattr(a, "archetype", "") or "")}


def _derivability(world, need: str) -> bool:
    """[P46.1] 可发性谓词：该原型在当前世界是否取得到素材（兜底与指派共同过滤）。"""
    alive = _alive_npcs(world)
    locs = list(getattr(world, "locations", None) or [])
    if need == "hostile_pair":
        return _worst_hostile_pair(world) is not None
    if need == "feud_or_hostile":
        return _worst_hostile_pair(world) is not None or _worst_feud_pair(world) is not None
    if need == "faction":
        return any(sum(1 for n in alive
                       if str(getattr(n, "faction_id", "") or "") == str(f.id)) >= 2
                   for f in (getattr(world, "factions", None) or []))
    if need == "two_factions":
        return len(getattr(world, "factions", None) or []) >= 2
    if need == "shop_or_hostile":
        return bool(getattr(world, "shops", None) or []) or _worst_hostile_pair(world) is not None
    if need == "shop":
        return bool(getattr(world, "shops", None) or [])
    if need == "recipes":
        return bool(getattr(world, "recipes", None) or [])
    if need == "npcs4":
        return len(alive) >= 4
    if need == "npcs3":
        return len(alive) >= 3
    if need == "wilderness":
        return any(str(getattr(l, "kind", "") or "") == "wilderness" for l in locs)
    if need == "danger_loc":
        return any(int(getattr(l, "danger", 0) or 0) >= 4 for l in locs)
    if need == "settlement":
        return any(str(getattr(l, "kind", "") or "") == "settlement" for l in locs)
    if need == "crowd":
        return any(sum(1 for n in alive
                       if str(getattr(n, "location_id", "") or "") == str(l.id)) >= 3
                   for l in locs)
    return True


def _pick_archetypes(world, rng, k: int) -> list:
    """[P46.1] 从冷却外的可发性原型里挑 k 个（rng 确定性；不足按实数）。"""
    cooldown = _cooldown_archetypes(world)
    cands = [key for key in _ARC_ARCHETYPE_VALUES
             if key not in cooldown and _derivability(world, _ARC_ARCHETYPES[key]["need"])]
    if not cands:
        # 全冷却（极端）：放开冷却，仅按可发性
        cands = [key for key in _ARC_ARCHETYPE_VALUES
                 if _derivability(world, _ARC_ARCHETYPES[key]["need"])]
    if not cands:
        return []
    if len(cands) <= k:
        return list(cands)
    # [!] 只用 roll：引擎约定 rng 面仅 chance/roll/pick（_Rng 桩无 sample）
    out: list = []
    cpool = list(cands)
    while len(out) < k and cpool:
        it = cpool[rng.roll(0, len(cpool) - 1)]
        out.append(it)
        cpool.remove(it)
    return out



def _npc_name_safe(npc_by_id: dict, nid: str, fallback: str) -> str:
    n = npc_by_id.get(str(nid))
    return str(getattr(n, "name", "") or fallback) if n is not None else fallback


def plan_fallback_arcs(world, rng) -> list:
    """无 LLM 兜底规划（[P46.1] 原型驱动）：每周期 1 条，原型经冷却+可发性双重挑选。

    - 冷却：最近 _COOLDOWN_WINDOW 条已规划线用过的原型不重复（防同质）。
    - 可发性谓词：该原型在当前世界取得到素材才进场（小世界按谓词降级，绝不硬凑）。
    - 模板池 _GENRE_ARC_TEMPLATES[题材][原型] 只提供措辞（占位符 {A}{B}{C}{loc}），
      阶段 kind 由 _ARC_ARCHETYPES[原型]["seq"] 单一权威回填。
    - 取材映射：power/wealth 族优先势力敌意对；social/stability 族优先仇敌对；无据返回 []。
    """
    alive = _alive_npcs(world)
    key_alive = [n for n in alive if getattr(n, "is_key_npc", False)]
    if len(alive) < SMALL_WORLD_MIN_ALIVE or len(key_alive) < SMALL_WORLD_MIN_KEY:
        return []
    ov = getattr(world, "config_overlay", None) or {}
    tid = str(ov.get("attribute_template_id", "western_fantasy")
              if isinstance(ov, dict) else "western_fantasy") or "western_fantasy"
    # [P46.1 v1.2 审核修复] 标题查重覆盖**全部线**（含终态）——旧口径只查 active，
    # 终态标题在冷却窗滑出后即可复活同名线（review GLM P1-2 / qwen P1② 双命中）。
    used_titles = {a.title for a in (getattr(world, "story_arcs", None) or [])
                   if isinstance(a, StoryArc)}
    busy: set = set()
    for a in active_arcs(world):
        busy.update(_arc_member_ids(a))
    npc_by_id = _npc_by_id(world)

    # 原型挑选：冷却 + 可发性 + 模板池有货
    pool = _GENRE_ARC_TEMPLATES.get(tid) or _GENRE_ARC_TEMPLATES["western_fantasy"]
    picked = None
    for key in _pick_archetypes(world, rng, 1):
        variants = pool.get(key) or []
        unused = [v for v in variants if v[0] not in used_titles]
        if unused:
            picked = (key, unused[0])
            break
    if picked is None:
        return []
    arch_key, variant = picked
    seq = _ARC_ARCHETYPES[arch_key]["seq"]

    def _mk(mm_id: str, parts: list, faction_id: str, fmt: dict) -> StoryArc:
        stages = []
        for i, (nm, ds) in enumerate(variant[2]):
            kind = seq[i] if i < len(seq) else seq[-1]
            stages.append(StoryStage(name=str(nm)[:24],
                                     desc=str(ds).format(**fmt)[:120],
                                     kind=kind, days=int(rng.roll(1, 3))))
        total = sum(_stage_days(s) for s in stages)
        if total < 5:
            stages[0].days = _stage_days(stages[0]) + (5 - total)
        if sum(_stage_days(s) for s in stages) > _MAX_TOTAL_DAYS and len(stages) > _MIN_STAGES:
            stages = stages[:_MIN_STAGES]
        return StoryArc(title=str(variant[0])[:48],
                        premise=str(variant[1]).format(**fmt)[:120],
                        mastermind_id=mm_id, participant_ids=parts,
                        stages=stages, start_day=_day(world),
                        faction_id=faction_id, source="template", archetype=arch_key)

    # 取材：{A}{B} 由真实敌意填充——优先势力敌对对，其次仇敌对（social 底账）。
    # 原型的 axis 只影响结局结算，不影响取材（否则 social/stability 族在小世界永无出口）。
    axis = _ARC_ARCHETYPES[arch_key]["axis"]
    circle = _social_circle_ids(world)
    pair = _worst_hostile_pair(world)
    if pair is not None:
        fa, fb = pair
        mem_a = [n for n in alive if str(getattr(n, "faction_id", "") or "") == str(fa.id)
                 and str(n.id) not in busy]
        mem_b = [n for n in alive if str(getattr(n, "faction_id", "") or "") == str(fb.id)
                 and str(n.id) not in busy]
        # [导演偏置] 策划者优先要角，同资格下优先玩家社交圈（世界的故事与玩家交织）
        pool_mm = sorted(mem_a, key=lambda n: (not getattr(n, "is_key_npc", False),
                                               0 if str(n.id) in circle else 1, str(n.id)))
        if pool_mm:
            mm = pool_mm[0]
            base = [str(n.id) for n in sorted(
                mem_b, key=lambda n: (0 if str(n.id) in circle else 1,
                                      not getattr(n, "is_key_npc", False), str(n.id)))[:2]]
            parts = _fill_participants(world, mm, base, busy | {str(mm.id)})
            if len(parts) >= 2:
                locs = list(getattr(world, "locations", None) or [])
                owned = [l for l in locs if getattr(l, "owner_faction_id", "") == str(fa.id)
                         or getattr(l, "faction_id", "") == str(fa.id)]
                anchor = (min(owned, key=lambda l: (-int(getattr(l, "danger", 0) or 0), str(l.id)))
                          if owned else None)
                if anchor is None:
                    anchor = _arc_anchor_loc(
                        world, StoryArc(mastermind_id=str(mm.id), participant_ids=list(parts)))
                loc_name = str(anchor.name) if anchor is not None else "边境"
                c_name = _npc_name_safe(npc_by_id, parts[0], mm.name)
                return [_mk(str(mm.id), parts, str(fa.id),
                            {"A": fa.name, "B": fb.name, "C": c_name, "loc": loc_name})]
    feud = _worst_feud_pair(world)
    if feud is not None:
        a, b = feud
        if str(a.id) not in busy and str(b.id) not in busy:
            parts = _fill_participants(world, a, [str(b.id)], busy | {str(a.id)})
            if len(parts) >= 2:
                anchor = _arc_anchor_loc(
                    world, StoryArc(mastermind_id=str(a.id), participant_ids=list(parts)))
                loc_name = str(anchor.name) if anchor is not None else "旧地"
                c_name = _npc_name_safe(npc_by_id, parts[0], a.name)
                return [_mk(str(a.id), parts, "",
                            {"A": a.name, "B": b.name, "C": c_name, "loc": loc_name})]
    return []


def replan_due_arcs(world, day: int) -> list:
    """需要续写且今天未尝试的线；只允许改动还没开始的阶段。"""
    out = []
    for arc in active_arcs(world):
        _ensure_stage_schedule(arc)
        future = [s for s in (arc.stages or [])[int(arc.current_stage):]
                  if isinstance(s, StoryStage) and not s.done and s.started_tick < 0]
        if arc.replan_pending and future and int(arc.last_replan_day or 0) < int(day):
            out.append(arc)
    return sorted(out, key=lambda a: (int(a.start_day), str(a.id)))


def apply_replan(world, arc: StoryArc, data: Any, day: int) -> bool:
    """整份候选先校验再一次写入；仅更新 id 对应的未开始阶段。

    阶段数、顺序、kind、days、已发生内容和任务归属均保持不变。错误 JSON/过期
    revision/编造 hook 或人物一律返回 False，现有剧情仍按原计划推进。
    """
    if not isinstance(data, dict) or data.get("mode") != "story_arc_replan":
        return False
    if str(data.get("arc_id", "") or "") != str(arc.id):
        return False
    if _gi(data, "revision", -1) != int(arc.revision):
        return False
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        return False
    _ensure_stage_schedule(arc)
    eligible = {s.id: s for s in arc.stages[int(arc.current_stage):]
                if isinstance(s, StoryStage) and not s.done and s.started_tick < 0}
    if not eligible or len(raw_stages) != len(eligible):
        return False
    alive = _alive_npcs(world)
    names = [str(n.name) for n in alive if getattr(n, "name", "")]
    name2npc = {str(n.name): n for n in alive if getattr(n, "name", "")}
    updates = {}
    for raw in raw_stages:
        if not isinstance(raw, dict):
            return False
        sid = str(raw.get("id", "") or "")
        if sid not in eligible or sid in updates:
            return False
        name = str(raw.get("name", "") or "").strip()[:24]
        desc = str(raw.get("desc", "") or "").strip()[:120]
        if not name or not desc:
            return False
        # 旧提案的目标可能刚好已经死亡或封闭；重规划不能把失效介入点带进下段。
        old_hook = eligible[sid].hook
        hook = (_sanitize_hook(world, old_hook, names, name2npc)
                if isinstance(old_hook, dict) and old_hook.get("type") else {})
        if "hook" in raw:
            if raw["hook"] is None:
                hook = {}
            else:
                hook = _sanitize_hook(world, raw["hook"], names, name2npc)
                if not hook:
                    return False
        if hook and str(arc.archetype or "") not in _BRANCH_ARCHETYPES:
            hook = dict(hook)
            hook["stance"] = "support"
        old_condition = eligible[sid].condition
        condition = {}
        if isinstance(old_condition, dict) and old_condition.get("type") == "npc_dead":
            target = next((n for n in alive if str(n.id) == str(old_condition.get("target_id", ""))), None)
            if target is not None:
                condition = _sanitize_stage_condition(
                    world, arc, {"type": "npc_dead", "target": str(target.name)})
        if "condition" in raw:
            if raw["condition"] is None:
                condition = {}
            else:
                condition = _sanitize_stage_condition(world, arc, raw["condition"])
                if not condition:
                    return False
        updates[sid] = (name, desc, dict(hook), dict(condition))
    for sid, (name, desc, hook, condition) in updates.items():
        stage = eligible[sid]
        stage.name = name
        stage.desc = desc
        stage.hook = hook
        stage.condition = condition
    arc.revision = max(0, int(arc.revision)) + 1
    arc.replan_pending = False
    arc.replan_reason = ""
    arc.last_replan_day = max(1, int(day))
    return True

# ============================================================
# 每日推进（advance）——零 LLM；每 tick 调用，幂等靠 done 旗标
# ============================================================

def _hook_quests(world, arc_id: str = "", in_flight_only: bool = True) -> list:
    out = []
    prefix = f"{arc_id}#" if arc_id else ""
    for q in (getattr(world, "quests", None) or []):
        hid = str(getattr(q, "hook_arc_id", "") or "")
        if not hid:
            continue
        if prefix and not hid.startswith(prefix):
            continue
        if in_flight_only and str(getattr(q, "status", "")) not in ("available", "active", "completed"):
            continue
        out.append(q)
    return out


def _currency(world) -> str:
    try:
        from src.models.world_sim_preset import GenreText
        return GenreText(getattr(world, "config_overlay", None) or {}).currency
    except Exception:
        return "金币"


def _engine_hook_objectives(world, arc: StoryArc) -> list:
    """LLM 未给（或没通过清洗）合法 hook 时，引擎用可判定模板派生单目标 hook。

    优先 kill 敌对势力成员（有真实敌意势力对）> visit 锚定地点 > collect 常见材料 x3。
    目标全部取引擎真实名单（势力名/地点名/物品名）——绝不引用 LLM 编造名。
    """
    enemy = _enemy_faction(world, arc.faction_id) if arc.faction_id else None
    if enemy is not None:
        # [P46 v1.1 审核修复] kill 进度按 NPC role/name 匹配（world_sim_service:7324
        # 只传 role+name 别名），势力名永不命中 -> 死任务。目标改为敌对势力下真实的
        # 存活 NPC 名（优先 hostile）；无人可选则回退 visit/collect。
        members = [n for n in _alive_npcs(world)
                   if str(getattr(n, "faction_id", "") or "") == str(enemy.id)]
        hostiles = [n for n in members if getattr(n, "hostile", False)] or members
        if hostiles:
            victim = min(hostiles, key=lambda n: str(n.id))
            return [{"type": "kill", "target": str(victim.name)[:24], "count": 1,
                     "current": 0,
                     "desc": f"解决{enemy.name}的{victim.name}（0/1）"}]
    anchor = _arc_anchor_loc(world, arc)
    # [S04/R2] 锚定地点挂开放秘境 -> 探房介入（剧情线引用真实秘境，§3.2 验收线）
    if anchor is not None:
        try:
            from src.services import dungeon_engine as _dge
            _dg = _dge.dungeon_at(world, anchor, discovered_only=True)
            if _dg is not None and str(getattr(_dg, "name", "") or "")                     and str(getattr(_dg, "status", "")) == "open":
                return [{"type": "dungeon_room_resolved", "target": str(_dg.name)[:24],
                         "count": 3, "current": 0,
                         "desc": f"深入「{_dg.name}」探明三处房间（0/3）"}]
        except Exception:
            pass
    if anchor is not None and getattr(anchor, "name", ""):
        return [{"type": "visit", "target": str(anchor.name)[:24], "count": 1, "current": 0,
                 "desc": f"前往「{anchor.name}」打探（0/1）"}]
    mats = [i for i in (getattr(world, "items", None) or [])
            if getattr(i, "type", "") == "material" and getattr(i, "rarity", "") == "common"
            and getattr(i, "name", "")]
    if mats:
        m = min(mats, key=lambda i: str(i.id))
        # [S04/R2] wealth 轴线优先「交付」介入（真实扣物入发布者容器，§3.1 验收线）
        arch0 = _ARC_ARCHETYPES.get(str(getattr(arc, "archetype", "") or ""), None)
        if arch0 is not None and arch0.get("axis") == "wealth":
            return [{"type": "deliver_items", "target": str(m.name)[:24],
                     "count": 2, "current": 0,
                     "desc": f"把两份{m.name}送到托付人手中（0/2）"}]
        return [{"type": "collect", "target": str(m.name)[:24], "count": 3, "current": 0,
                 "desc": f"收集{m.name}（0/3）"}]
    return []


# [任务奖励物品 2026-09-13] 阶段 kind / arc 结算轴 -> 奖励物品类型偏好（软偏好：命中
# 就优先，未命中回退全池——宁可给件不相干的，也不给不出）。让「夺灵脉线给灵石矿样本、
# 灾祸线给防疫草药」这种贴合感来自已落库的 archetype 结构，而不是对 LLM 文案做关键词
# 模糊匹配（后者确定性差且易被「石」这类短词误命中）。
_STAGE_KIND_ITEM_TYPES: dict = {
    "conflict": ("weapon", "armor"),
    "econ": ("material", "accessory"),
    "disaster": ("consumable", "material"),
    "social": ("accessory", "consumable"),
    "scheme": ("material", "accessory"),
}
_AXIS_ITEM_TYPES: dict = {
    "power": ("weapon", "armor"),
    "wealth": ("material", "accessory"),
    "social": ("accessory", "consumable"),
    "stability": ("consumable", "material"),
}


def _hook_reward_item_types(arc: StoryArc, st) -> tuple:
    """hook 任务奖励的物品类型偏好：当前阶段 kind 优先，回退 arc 结算轴。"""
    t = _STAGE_KIND_ITEM_TYPES.get(str(getattr(st, "kind", "") or ""))
    if t:
        return t
    arch = _ARC_ARCHETYPES.get(str(getattr(arc, "archetype", "") or ""), None)
    return _AXIS_ITEM_TYPES.get(str(arch.get("axis")) if arch else "", ())


def _maybe_derive_hook(world, arc: StoryArc, rng, tick: int) -> None:
    """当前 stage 的 hook 派生（≈ 每日 30% roll；限量三闸；不产事件不占事件预算）。"""
    # [P46 v1.1 审核修复 qwen] 任务系统总闸关闭时不再派 hook 任务（与 event_quests 同口径）
    if not (getattr(world, "config_overlay", None) or {}).get("quest_system_enabled", True):
        return
    idx = int(arc.current_stage)
    if idx >= len(arc.stages):
        return
    st = arc.stages[idx]
    # [P46.1 用户定稿] 介入门槛放宽到全部阶段类型（原 scheme/conflict 限定让
    # econ/social/disaster 段的介入偏好成为空话）；hook 类型按原型偏好取。
    arch = _ARC_ARCHETYPES.get(str(getattr(arc, "archetype", "") or ""), None)
    if not st.hook.get("type"):
        st.hook["type"] = (arch["hooks"][0] if arch and arch["hooks"] else "visit")
    if "stance" not in st.hook:
        # 模板剧情也能给出两种站边机会；未显式指定时前两段分别支持、阻挠。
        st.hook["stance"] = ("oppose" if str(arc.archetype or "") in _BRANCH_ARCHETYPES
                             and idx % 2 == 1 else "support")
    if _day(world) < _stage_start_day(arc, idx):        # 阶段尚未开始
        return
    if st.hook.get("derived"):
        return      # [P46 v1.1 审核修复 qwen] 本段已派生过（giver 死亡会连任务一起删，
        #           扫描法会击穿终身闸——derived 标记随 stage 序列化，防重派死循环）
    _ensure_stage_schedule(arc)
    hid = f"{arc.id}#{st.id}"
    if any(str(getattr(q, "hook_arc_id", "") or "") == hid
           for q in (getattr(world, "quests", None) or [])):
        return                                          # 每阶段终身至多一条（防失效重派死循环）
    if len(_hook_quests(world, arc.id)) >= HOOK_PER_ARC_LIVE:
        return
    if len(_hook_quests(world, arc.id, in_flight_only=False)) >= HOOK_PER_ARC_LIFETIME:
        return
    if len(_hook_quests(world)) >= HOOK_WORLD_LIVE:
        return
    if not rng.chance(HOOK_CHANCE_PER_TICK):
        return
    npc_by_id = _npc_by_id(world)
    main_ids = qe.main_giver_ids(world)                 # 主线发布人不当 arc hook giver（身份混淆）
    mm = npc_by_id.get(str(arc.mastermind_id))
    cands = [npc_by_id[p] for p in arc.participant_ids if p in npc_by_id
             and getattr(npc_by_id[p], "alive", True) and p not in main_ids]
    if not cands and mm is not None and getattr(mm, "alive", True) \
            and str(arc.mastermind_id) not in main_ids:
        cands = [mm]
    if not cands:
        return
    mm_loc = str(getattr(mm, "location_id", "") or "") if mm is not None else ""
    opposing = (str(arc.archetype or "") in _BRANCH_ARCHETYPES
                and st.hook.get("stance") == "oppose")
    mm_fac = str(getattr(mm, "faction_id", "") or "") if mm is not None else ""
    cands.sort(key=lambda n: (0 if opposing and mm_fac and
                             str(getattr(n, "faction_id", "") or "") not in ("", mm_fac)
                             else 1,
                             0 if mm_loc and str(n.location_id) == mm_loc else 1,
                             str(n.id)))
    giver = cands[0]
    if isinstance(st.hook, dict) and st.hook.get("type") in _HOOK_OBJ_TYPES and st.hook.get("target"):
        objs = [dict(st.hook)]
    else:
        objs = _engine_hook_objectives(world, arc)
    if not objs:
        return
    gold = int(rng.roll(120, 220))
    xp = int(rng.roll(30, 60))
    # [任务奖励物品 2026-09-13] 奖励兼给一件目录物品（紫以下，按当前阶段主题挑类型）。
    # 放在全部早退之后：不派生的分支不额外消费 rng（守同 world+tick 回放）。
    # [!] 挑中的分支会消费共享 arc rng 两个随机数，同 tick 后续 arc 的抽签相对旧版位移
    # （同版本内仍确定性可回放）——这是既定取舍，不要误读成「零位移」。
    item = qe.pick_reward_item(world, rng, tier="mid",
                               prefer_types=_hook_reward_item_types(arc, st),
                               exclude_ids=qe.player_owned_ids(world))
    # [修 2026-09-30] collect/deliver_items 目标获取渠道校验（heal 链只在建世/拓展/
    # 读档跑，运行时 hook 创建的任务漏检——无渠道物品上架到最近商店保底可完成）
    _collect_targets = [str(o.get("target","")).strip() for o in objs
                        if isinstance(o, dict) and str(o.get("type","")) in ("collect","deliver_items")
                        and str(o.get("target","")).strip()]
    if _collect_targets:
        try:
            from src.services import trade_engine as _tre_ch
            _by_name = {str(i.name): i for i in (getattr(world, "items", None) or [])
                        if getattr(i, "name", "")}
            _reachable = set()
            for _s in (getattr(world, "shops", None) or []):
                for _e in (getattr(_s, "stock", None) or []):
                    _reachable.add(str(getattr(_e, "item_id", "") or ""))
            for _l in (getattr(world, "locations", None) or []):
                for _rn in (getattr(_l, "resource_nodes", None) or []):
                    for _dd in (getattr(_rn, "drops", None) or []):
                        _reachable.add(str(_dd.get("item_id") or ""))
            for _m2 in (getattr(world, "monster_pool", None) or []):
                if isinstance(_m2, dict):
                    for _dd in (_m2.get("loot") or []):
                        _reachable.add(str(_dd.get("item_id") or "") if isinstance(_dd, dict) else str(_dd))
            for _r in (getattr(world, "recipes", None) or []):
                _reachable.add(str(getattr(_r, "output_item_id", "") or ""))
            for _t in _collect_targets:
                _it = _by_name.get(_t)
                if _it is None or str(_it.id) in _reachable:
                    continue
                # 无渠道：丢弃该 collect 目标（丢 hook 不丢线——与 _sanitize_hook 既有口径一致）
                for _o in objs:
                    if isinstance(_o, dict) and str(_o.get("type","")) in ("collect","deliver_items")                             and str(_o.get("target","")).strip() == _t:
                        objs.remove(_o)
                        break
        except Exception:
            pass

    world.quests.append(Quest(
        title=(f"剧情线·{arc.title}：{'阻止' if opposing else '协助'}{st.name}")[:64],
        objective=f"{arc.premise[:60]}；{st.desc[:80]}".strip("；"),
        giver_npc_id=str(giver.id),
        reward_text=(f"{gold} {_currency(world)} + {xp} 经验 + {giver.name}的交情"
                     + (f" + {item.name}" if item is not None else "")),
        status="available", started_at_tick=int(tick),
        objectives=objs,
        rewards={"gold": gold, "xp": xp,
                 "items": [str(item.id)] if item is not None else []},
        chain="side", hook_arc_id=hid,
    ))
    # [P46 v1.1 审核修复] 派生标记随 stage 序列化：giver 死亡会连任务一起删（available
    # 被清理），仅靠扫 quests 会击穿终身闸重派死循环
    try:
        st.hook["derived"] = True
    except Exception:  # noqa: BLE001 - 脏 hook dict 不炸派生
        pass
    # [导演偏置 2026-09-26 [M] 项] 高潮段 hook 升格「求援上门」：终段任务派生时，giver
    # 托信使送来求援信（P57 信使通道复用，过期不候——任务仍在任务日志，只是上门这一刻
    # 错过）。非终段不推（低频介入照旧从任务日志发现）。
    if idx == len(arc.stages) - 1:
        try:
            ore.push_system_letter(
                world, str(giver.id),
                title=f"求援·{arc.title}"[:64],
                text=(f"「{arc.title}」已到紧要关头——{giver.name}需要你的力量。"
                      f"任务「剧情线·{arc.title}：{st.name}」已托付给你，"
                      "成与不成，就看这一程。"),
                payload={"arc_id": str(arc.id), "hook": hid})
        except Exception:  # noqa: BLE001 - 信使通道故障绝不炸派生
            pass


def _involved_alive(world, arc: StoryArc) -> list:
    npc_by_id = _npc_by_id(world)
    return [nid for nid in _arc_member_ids(arc)
            if nid in npc_by_id and getattr(npc_by_id[nid], "alive", True)]


# [S03/R2 2026-09-30] 阶段到期评估：三结果 + 介入/凋零修正（纯函数，可测）
_STAGE_OUTCOMES_GLOBAL = ("success", "compromise", "setback")
_STAGE_OUTCOME_VALUES_GLOBAL = _STAGE_OUTCOMES_GLOBAL   # _complete_stage 白名单引用
_STAGE_OUTCOME_ZH = {"success": "进展顺利", "compromise": "不了了之，各让一步",
                     "setback": "受挫折损"}
_PENDING_ARC_EVENT_CAP = 32


def _can_record_arc_event(world, budget: list) -> bool:
    return budget[0] > 0 or len(getattr(world, "pending_arc_events", []) or []) < _PENDING_ARC_EVENT_CAP


def _record_arc_event(world, event: WorldEvent, events: list, budget: list) -> None:
    """额度满时持久排队；下一回合先发旧公告，保留事件发生时的 tick。"""
    if budget[0] > 0:
        budget[0] -= 1
        events.append(event)
    else:
        world.pending_arc_events.append(event)


def _arc_commit_snapshot(world, arc: StoryArc, events: list, budget: list) -> tuple:
    """保存一条剧情结算可能改动的对象与事件位置，供异常时原位回滚。"""
    member_ids = set(_arc_member_ids(arc))
    changed = ([arc] + list(getattr(world, "factions", None) or [])
               + list(getattr(world, "locations", None) or [])
               + [n for n in _npcs(world) if str(n.id) in member_ids]
               + [q for q in (getattr(world, "quests", None) or [])
                  if str(getattr(q, "hook_arc_id", "") or "").startswith(f"{arc.id}#")])
    return ([(obj, copy.deepcopy(obj.__dict__)) for obj in changed],
            budget[0], len(events), len(world.pending_arc_events))


def _arc_commit_rollback(world, snapshot: tuple, events: list, budget: list) -> None:
    states, old_budget, old_event_count, old_pending_count = snapshot
    for obj, state in states:
        obj.__dict__.clear()
        obj.__dict__.update(state)
    budget[0] = old_budget
    del events[old_event_count:]
    del world.pending_arc_events[old_pending_count:]


def _stage_outcome_chances(arc: StoryArc) -> tuple:
    """(success, compromise, setback) 概率（归一化三元组，纯读 arc）。

    介入修正：帮助策划者把概率从 setback 挪给 success，帮助对立方反向搬移；
    参与者不足两人也使 setback 增多。每方影响最多 0.20。"""
    s, c, x = 0.55, 0.30, 0.15
    helped = int(getattr(arc, "player_helped", 0) or 0)
    opposed = int(getattr(arc, "player_opposed", 0) or 0)
    lift = min(0.20, 0.10 * max(0, helped)) - min(0.20, 0.10 * max(0, opposed))
    s += lift
    x -= lift
    if len([p for p in (arc.participant_ids or [])]) < 2:
        x += 0.10
        s -= 0.10
    tot = max(1e-6, s + c + x)
    return (max(0.0, s) / tot, max(0.0, c) / tot, max(0.0, x) / tot)


def _evaluate_stage(arc: StoryArc, idx: int) -> tuple:
    """确定性评估本段结果（独立盐 rng——不消费 advance 的共享序列，回放稳定；
    同线同段同结果）。[!] 种子用 title+start_day+idx 而非 arc.id：id 是 uuid4，
    同种子重建两次会得到不同结局（TestDeterminism 守护）；title 有查重保障唯一性，
    存档回放（title 落盘）与从零重建（模板/LLM 标题确定）都稳定。"""
    from src.utils.rng import SeededRng
    s, c, x = _stage_outcome_chances(arc)
    rng = SeededRng.seed_from(str(arc.title or arc.id), int(arc.start_day) + int(idx),
                              "arc_stage_outcome")
    r = rng.random()
    key = ("success" if r < s else ("compromise" if r < s + c else "setback"))
    return key, _STAGE_OUTCOME_ZH.get(key, "")


def _cleanup_terminal_hook_quests(world, arc: StoryArc, tick: int,
                                  events: list, budget: list) -> int:
    """[S04 切片/F04 2026-09-30] 线终态时收口 hook 任务生命周期。

    available/active（未接/未完成）-> failed（「剧情已收束」，释放全局在途额度——
    原实现终态后旧任务残留，既误导玩家又占 HOOK_WORLD_LIVE=3 名额堵住新剧情）；
    completed（待领奖）与 claimed（含满包滞留）不动——贡献与领取权益保留（守 R0）。
    返回收口数；事件受预算闸（不足则只改状态不出事件，可见性让位正确性）。"""
    n = 0
    titles: list = []
    for q in (getattr(world, "quests", None) or []):
        hid = str(getattr(q, "hook_arc_id", "") or "")
        if not hid.startswith(f"{arc.id}#"):
            continue
        if str(getattr(q, "status", "")) in ("available", "active"):
            q.status = "failed"
            q.completed_at_tick = int(tick)
            n += 1
            titles.append(str(getattr(q, "title", "") or "?"))
    if n and budget[0] > 0:
        budget[0] -= 1
        events.append(WorldEvent(
            tick=int(tick), category="npc", severity="minor",
            title=f"剧情线·{arc.title}：尘埃落定"[:64],
            desc=f"随着这条线收束，{len(titles)} 项托付随之作废："
                 f"{'、'.join(titles[:3])}" + ("…" if len(titles) > 3 else ""),
            npcs=_involved_alive(world, arc),
            factions=[arc.faction_id] if arc.faction_id else [],
        ))
    return n


def _expire_stage_hooks(world, arc: StoryArc, idx: int) -> list:
    """[S04 余项/R2 2026-09-30] 阶段推进时收口该阶段 hook：介入时机已过，过期不候。

    available/active -> failed（错过这一段的托付）；completed/claimed 不动（贡献与
    领取权益保留，守 R0）。返回作废任务标题列表（事件尽力出——预算不足只改状态）。"""
    hid = f"{arc.id}#{arc.stages[idx].id}"
    titles: list = []
    for q in (getattr(world, "quests", None) or []):
        if str(getattr(q, "hook_arc_id", "") or "") != hid:
            continue
        if str(getattr(q, "status", "")) in ("available", "active"):
            q.status = "failed"
            titles.append(str(getattr(q, "title", "") or "?"))
    return titles


def _stage_event(world, arc: StoryArc, idx: int, tick: int, day: int, extra: str = "") -> WorldEvent:
    """阶段事件（引擎回填三关联字段——rumor/编年史/动态栏收编前提）。"""
    st = arc.stages[idx]
    npc_by_id = _npc_by_id(world)
    involved = _involved_alive(world, arc)
    loc_ids: list = []
    for nid in involved:
        lid = str(getattr(npc_by_id[nid], "location_id", "") or "")
        if lid and lid not in loc_ids:
            loc_ids.append(lid)
    sev = "major" if idx >= len(arc.stages) - 1 else "minor"
    desc = str(st.desc)[:120]
    if extra:
        desc = f"{desc}（{extra}）" if desc else extra
    return WorldEvent(
        tick=int(tick), category="npc", severity=sev,
        title=f"剧情线·{arc.title}：{st.name}"[:64],
        desc=desc[:200],
        npcs=involved, locations=loc_ids,
        factions=[arc.faction_id] if arc.faction_id else [],
    )


def settle_arc_outcome(world, arc, rng, tick) -> None:
    """结局软结算（一次，[P46.1] 三轴版）：power（零和：策划者方 +，对手方 -）+
    social（参与者两两同向）+ stability（民生轴：灾祸/民变线锚定地点）+ wealth
    （Faction.wealth 零和小额）。幅度按原型的 axis 定主次。

    钳制：power/wealth 0-100、social -100~100、stability 0-100。写 arc.outcome 一句话
    （从结局句池按原型族取，随 major 事件进编年史）。

    [!] 不碰 Location.owner_faction_id / Faction.territory / 玩家金币物品 / NPC 存活。
    rng 消费顺序固定：power -> stability -> wealth -> social（按 (id_a, id_b) 排序逐对）。
    """
    sign = 1 if arc.status == "succeeded" else -1
    mag = int(rng.roll(3, 5)) if sign > 0 else int(rng.roll(2, 3))
    if sign > 0 and int(getattr(arc, "player_helped", 0) or 0) > 0:
        mag = min(5, mag + 1)
    notes = []
    facs = list(getattr(world, "factions", None) or [])
    fac = next((f for f in facs if str(f.id) == str(arc.faction_id)), None)

    def _fac_by_name(name: str):
        return next((f for f in facs if f.name == name), None)

    # ---- power（零和：对手方反向等额）----
    if fac is not None:
        old_p = int(getattr(fac, "power", 0) or 0)
        fac.power = max(0, min(100, old_p + sign * mag))
        d = int(fac.power) - old_p
        if d:
            notes.append(f"{fac.name}实力{'+' if d > 0 else ''}{d}")
        # 零和：对手方取敌对关系最差的一方（GLM 咨询：非零和是同质化漏洞之一）
        rival = _enemy_faction(world, arc.faction_id)
        if rival is not None and d:
            old_r = int(getattr(rival, "power", 0) or 0)
            rival.power = max(0, min(100, old_r - d))
            if rival.power != old_r:
                notes.append(f"{rival.name}实力{rival.power - old_r:+d}")

    # ---- stability（民生轴：锚定地点，阈值感知——失败下限 10，防 reconcile 每日 +1 抹平）----
    arch = _ARC_ARCHETYPES.get(str(getattr(arc, "archetype", "") or ""), None)
    if arch and arch["axis"] == "stability":
        anchor = _arc_anchor_loc(world, arc)
        if anchor is not None:
            d_st = (sign * int(rng.roll(3, 6))) if sign > 0 else -int(rng.roll(10, 16))
            # [审核修复 2026-09-13] stability 0 是合法值（夺城 -25 钳 0 可达），`or 50`
            # 会把它吞成 50，于是「失败的剧情线」反把民生从 0 抬到 35+（方向反了还记假账）。
            # 守 §11 防 0 吞：只有 None 才取缺省 50（同 world_tick_engine.reconcile_world 口径）。
            _raw_st = getattr(anchor, "stability", None)
            old_st = int(_raw_st) if _raw_st is not None else 50
            anchor.stability = max(0, min(100, old_st + d_st))
            ds = int(anchor.stability) - old_st
            if ds:
                notes.append(f"{anchor.name}民生{ds:+d}")

    # ---- wealth（Faction.wealth 小额增减，仅策划者势力——wealth 无天然对手方，非零和）----
    if fac is not None and arch and arch["axis"] == "wealth":
        d_w = sign * int(rng.roll(1, 3))
        # [审核修复 2026-09-13] wealth 0 是合法破产档（§22 物价联动条明令「勿 or 50」），
        # 吞掉会让一条弧把破产势力直接拉回 ~50 并立刻改写全城物价。
        _raw_w = getattr(fac, "wealth", None)
        old_w = int(_raw_w) if _raw_w is not None else 50
        fac.wealth = max(0, min(100, old_w + d_w))
        dw = int(fac.wealth) - old_w
        if dw:
            notes.append(f"{fac.name}财计{dw:+d}")

    # ---- social（参与者两两同向）----
    npc_by_id = _npc_by_id(world)
    ids = sorted(nid for nid in _arc_member_ids(arc)
                 if nid in npc_by_id and getattr(npc_by_id[nid], "alive", True))
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = npc_by_id[ids[i]], npc_by_id[ids[j]]
            d = sign * int(rng.roll(1, 5))
            soc._social_set(a, b, max(-100, min(100, soc._social_get(a, str(b.id)) + d)))

    # ---- 结局句池（按成败取，rng 定句）——防"所图竟成/所图成空"两句走天下
    pool = _OUTCOME_POOL["succeeded" if sign > 0 else "failed"]
    tail = pool[rng.roll(0, len(pool) - 1)] if pool else ("所图竟成" if sign > 0 else "所图成空")
    arc.outcome = tail + (f"：{'、'.join(notes)}" if notes else "")


def _complete_stage(world, arc: StoryArc, idx: int, rng, tick: int,
                    events: list, budget: list, extra: str = "",
                    outcome_hint: str = "", claimed_quest=None,
                    stance: str = "support") -> bool:
    """标记 stage.done + current_stage 前移 + 产事件（额度不足先持久排队；队列满才
    返回 False 延后结算）。末段完成 -> 软结算（结算句入 desc）。

    [S03/R2 2026-09-30] 到期先评估结果再定终态：outcome_hint（claimed 提前完成路径
    按站边传 success/setback）缺省走 _evaluate_stage 三结果（success/compromise/setback）写
    stage.outcome；末段 setback -> 线 failed（策划者受挫），success/compromise ->
    succeeded——「到期」不再等于「得逞」（F03）。
    """
    if not _can_record_arc_event(world, budget):
        return False
    _ensure_stage_schedule(arc)
    day = _day(world)
    is_last = idx >= len(arc.stages) - 1
    st = arc.stages[idx]
    key = outcome_hint if outcome_hint in _STAGE_OUTCOME_VALUES_GLOBAL else ""
    if not key:
        key, ev_txt = _evaluate_stage(arc, idx)
        extra = (extra + "；" if extra else "") + ev_txt
    # 结算、任务失效和事件产出作为同一内存提交；任一步抛异常时恢复受影响对象。
    snapshot = _arc_commit_snapshot(world, arc, events, budget)
    try:
        if claimed_quest is not None:
            side = "oppose" if stance == "oppose" else "support"
            if side == "oppose":
                arc.player_opposed = max(0, int(arc.player_opposed or 0)) + 1
            else:
                arc.player_helped = max(0, int(arc.player_helped or 0)) + 1
            arc.interventions.append({"stage_id": st.id,
                                      "quest_id": str(getattr(claimed_quest, "id", "") or ""),
                                      "stance": side, "tick": int(tick)})
            arc.interventions = arc.interventions[-10:]
        st.outcome = key
        st.done = True
        if st.started_tick < 0:
            st.started_tick = int(tick)
        st.resolved_tick = int(tick)
        arc.current_stage = idx + 1
        if is_last:
            arc.status = "failed" if key == "setback" else "succeeded"
            arc.end_day = day
            arc.outcome = ""
            settle_arc_outcome(world, arc, rng, tick)
            extra = (extra + "；" if extra else "") + str(arc.outcome or "")
            arc.replan_pending = False
            arc.replan_reason = ""
        else:
            arc.replan_pending = True
            arc.replan_reason = f"第{idx + 1}段已结算：{key}"[:80]
        # 过期任务合并进本段事件；阶段事件先产出，再处理终态其他任务。
        gone = _expire_stage_hooks(world, arc, idx)
        if gone:
            extra = (extra + "；" if extra else "") + \
                    f"{len(gone)} 项托付过了时候（{'、'.join(gone[:2])}）"
        _record_arc_event(world, _stage_event(world, arc, idx, tick, day, extra=extra),
                          events, budget)
        if is_last:
            _cleanup_terminal_hook_quests(world, arc, tick, events, budget)
    except Exception:
        _arc_commit_rollback(world, snapshot, events, budget)
        raise
    return True


def advance(world, rng, tick, event_cap: int = ARC_EVENT_CAP_PER_TICK) -> list:
    """每日推进（tick_world 确定性段每 tick 调用；幂等）。返回本 tick arc 事件（<=2 条）。

    event_cap：本回合最多发布的剧情事件数；超额事件随状态结算一起入持久待发布队列，
    下回合先发布旧事件。队列满时才延迟新结算，避免事件永久丢失。

    顺序（定稿 §2）：死亡扫描 -> claimed 提前到期 -> 阶段到期 -> hook 派生 ->
    超时 failed -> 终态滚动。稳定序：active arc 按 (start_day, id) 排。
    """
    arcs = getattr(world, "story_arcs", None)
    day = _day(world)
    npc_by_id = _npc_by_id(world)
    events: list = []
    budget = [max(0, int(event_cap if event_cap is not None else ARC_EVENT_CAP_PER_TICK))]
    pending = getattr(world, "pending_arc_events", None)
    if not isinstance(pending, list):
        pending = []
        world.pending_arc_events = pending
    publish_count = min(budget[0], len(pending))
    if publish_count:
        events.extend(pending[:publish_count])
        budget[0] -= publish_count
    if not isinstance(arcs, list) or not arcs:
        del pending[:publish_count]
        return events

    def is_alive(nid: str) -> bool:
        n = npc_by_id.get(str(nid))
        return n is not None and bool(getattr(n, "alive", True))

    actives = sorted([a for a in arcs if isinstance(a, StoryArc) and a.status == "active"],
                     key=lambda a: (int(a.start_day), str(a.id)))
    for arc in actives:
        _ensure_stage_schedule(arc)
        # 1. 死亡扫描：mastermind 死 -> aborted（major 事件；额度不足则持久排队）；
        #    普通参与者死 -> 除名续跑（后续事件 npcs/desc 自然反映，v1 不做补叙）
        if not is_alive(arc.mastermind_id):
            if _can_record_arc_event(world, budget):
                involved = [str(p) for p in (arc.participant_ids or []) if is_alive(p)]
                loc_ids: list = []
                for nid in involved:
                    lid = str(getattr(npc_by_id[nid], "location_id", "") or "")
                    if lid and lid not in loc_ids:
                        loc_ids.append(lid)
                anchor = _arc_anchor_loc(world, arc)
                if anchor is not None and str(anchor.id) not in loc_ids:
                    loc_ids.append(str(anchor.id))
                ev = WorldEvent(
                    tick=int(tick), category="npc", severity="major",
                    title=f"剧情线·{arc.title}：中折"[:64],
                    desc=f"策划者身死，这条线的图谋随之消散。（第{day}天）",
                    npcs=involved, locations=loc_ids,
                    factions=[arc.faction_id] if arc.faction_id else [],
                )
                snapshot = _arc_commit_snapshot(world, arc, events, budget)
                try:
                    _record_arc_event(world, ev, events, budget)
                    arc.status = "aborted"
                    arc.end_day = day
                    arc.outcome = "中道而废"
                    if 0 <= int(arc.current_stage) < len(arc.stages):
                        st = arc.stages[int(arc.current_stage)]
                        st.outcome = "interrupted"
                        st.resolved_tick = int(tick)
                    arc.replan_pending = False
                    arc.replan_reason = ""
                    # [F04] 中折也收口 hook 任务（未接/未完成 -> failed，释放额度）
                    _cleanup_terminal_hook_quests(world, arc, tick, events, budget)
                except Exception:
                    _arc_commit_rollback(world, snapshot, events, budget)
                    raise
            continue
        before_people = list(arc.participant_ids or [])
        arc.participant_ids = [p for p in before_people if is_alive(p)]
        if len(arc.participant_ids) != len(before_people):
            arc.replan_pending = True
            arc.replan_reason = "参与者离世，后续阶段须核对"[:80]
        if arc.stages and 0 <= int(arc.current_stage) < len(arc.stages):
            active_stage = arc.stages[int(arc.current_stage)]
            if not active_stage.done and active_stage.started_tick < 0:
                active_stage.started_tick = int(tick)
        # 2. claimed 提前到期（completed 不算——防剧情抢跑）；兑现时计 player_helped（v2 站边钩子）
        if arc.stages and arc.current_stage < len(arc.stages) \
                and _can_record_arc_event(world, budget):
            stage = arc.stages[int(arc.current_stage)]
            hid = f"{arc.id}#{stage.id}"
            cq = next((q for q in (getattr(world, "quests", None) or [])
                       if str(getattr(q, "hook_arc_id", "") or "") == hid
                       and str(getattr(q, "status", "")) == "claimed"), None)
            if cq is not None:
                gname = str(getattr(npc_by_id.get(str(getattr(cq, "giver_npc_id", "") or "")),
                                    "name", "") or "")
                side = ("oppose" if str(arc.archetype or "") in _BRANCH_ARCHETYPES
                        and stage.hook.get("stance") == "oppose" else "support")
                _complete_stage(world, arc, int(arc.current_stage), rng, tick, events, budget,
                                extra=("有人当面完成了对立方的托付" if side == "oppose"
                                       else "有人当面完成了托付")
                                      + (f"（{gname}）" if gname else ""),
                                outcome_hint="setback" if side == "oppose" else "success",
                                claimed_quest=cq, stance=side)
        # 3. 实体状态变化：已指定的参与者死亡可使当前段提前受挫；策划者死亡
        #    已在死亡扫描中整线中折。未满足时仍按正常到期评估。
        if arc.status == "active" and arc.stages and arc.current_stage < len(arc.stages) \
                and _can_record_arc_event(world, budget):
            idx = int(arc.current_stage)
            condition = arc.stages[idx].condition
            if (isinstance(condition, dict) and condition.get("type") == "npc_dead"
                    and condition.get("target_id") and not is_alive(condition["target_id"])):
                _complete_stage(world, arc, idx, rng, tick, events, budget,
                                extra="相关人物离世，原计划受挫", outcome_hint="setback")
        # 4. 时间到期推进（同日可能连完多段，受事件预算闸约束；未产出的不置 done）
        while arc.status == "active" and arc.stages and arc.current_stage < len(arc.stages):
            idx = int(arc.current_stage)
            if arc.stages[idx].done:                    # 不变量自愈
                arc.current_stage = idx + 1
                continue
            if day < _stage_due_day(arc, idx) or not _can_record_arc_event(world, budget):
                break
            _complete_stage(world, arc, idx, rng, tick, events, budget)
        # 5. hook 派生（仅 active 且当前段存续；不产事件）
        if arc.status == "active" and arc.stages and arc.current_stage < len(arc.stages):
            _maybe_derive_hook(world, arc, rng, int(tick))
        # 6. 超时 failed（事件额度不足则排队；stages 空的脏线也由此兜底终结）
        if arc.status == "active" and _can_record_arc_event(world, budget):
            due_all = (_stage_due_day(arc, len(arc.stages) - 1) if arc.stages
                       else int(arc.start_day))
            if day > due_all + GRACE_DAYS:
                snapshot = _arc_commit_snapshot(world, arc, events, budget)
                try:
                    arc.status = "failed"
                    arc.end_day = day
                    arc.outcome = ""
                    settle_arc_outcome(world, arc, rng, tick)
                    ev = _stage_event_timeout(world, arc, tick, day)
                    _record_arc_event(world, ev, events, budget)
                    # [F04] 超时终局同样收口 hook 任务
                    _cleanup_terminal_hook_quests(world, arc, tick, events, budget)
                except Exception:
                    _arc_commit_rollback(world, snapshot, events, budget)
                    raise
        if arc.status == "active" and arc.replan_pending and not any(
                isinstance(s, StoryStage) and not s.done and s.started_tick < 0
                for s in arc.stages[int(arc.current_stage):]):
            arc.replan_pending = False
            arc.replan_reason = ""
    _roll_terminals(world)
    del pending[:publish_count]
    return events


def _stage_event_timeout(world, arc: StoryArc, tick: int, day: int) -> WorldEvent:
    """超时 failed 的终局事件（major；三关联字段照填）。"""
    involved = _involved_alive(world, arc)
    npc_by_id = _npc_by_id(world)
    loc_ids: list = []
    for nid in involved:
        lid = str(getattr(npc_by_id[nid], "location_id", "") or "")
        if lid and lid not in loc_ids:
            loc_ids.append(lid)
    anchor = _arc_anchor_loc(world, arc)
    if anchor is not None and str(anchor.id) not in loc_ids:
        loc_ids.append(str(anchor.id))
    return WorldEvent(
        tick=int(tick), category="npc", severity="major",
        title=f"剧情线·{arc.title}：不了了之"[:64],
        desc=f"这条线拖着拖着就散了。（第{day}天；{arc.outcome}）"[:200],
        npcs=involved, locations=loc_ids,
        factions=[arc.faction_id] if arc.faction_id else [],
    )


def _roll_terminals(world, keep: int = TERMINAL_KEEP) -> None:
    """终态滚动：active 全保 + 终态按 end_day 留最近 keep 条（仿 commissions cap 口径）。"""
    arcs = getattr(world, "story_arcs", None)
    if not isinstance(arcs, list):
        return
    act = [a for a in arcs if isinstance(a, StoryArc) and a.status == "active"]
    term = [a for a in arcs if isinstance(a, StoryArc) and a.status != "active"
            and isinstance(a, StoryArc)]
    others = [a for a in arcs if not isinstance(a, StoryArc)]
    if len(term) > keep:
        term.sort(key=lambda a: (-int(a.end_day), str(a.id)))
        term = term[:keep]
    world.story_arcs = act + term + others


# ============================================================
# 【当前剧情线】上下文块（旁白沉浸；纯函数，服务层判非空 append 进 volatile 段）
# ============================================================

def arc_context_block(world, player_loc_id: str = "") -> str:
    """本地线（锚定地==玩家位置，或有参与者此刻在场）至多 2 条给在场细节；
    其余 active 线以「远方」一行各至多 2 条——玩家窝在一地也能看到世界在动。

    护栏写进块头：只可提及/铺垫，不得推进阶段或宣布结局（防 LLM 抢导演权）。
    注入预算固定（<=4 行，不随世界年龄涨；读时派生零持久化零新调用）。
    """
    arcs = sorted(active_arcs(world), key=lambda a: (int(a.start_day), str(a.id)))
    if not arcs:
        return ""
    ploc = player_loc_id or str(getattr(getattr(world, "player", None), "location_id", "") or "")
    npc_by_id = _npc_by_id(world)
    local_lines: list = []
    far_lines: list = []
    for arc in arcs:
        st = _cur_stage(arc)
        if st is None:
            continue
        present = []
        if ploc:
            for nid in _arc_member_ids(arc):
                n = npc_by_id.get(str(nid))
                if (n is not None and getattr(n, "alive", True)
                        and str(getattr(n, "location_id", "") or "") == ploc):
                    present.append(str(n.name))
        anchor = _arc_anchor_loc(world, arc)
        is_local = bool(present) or (anchor is not None and ploc and str(anchor.id) == ploc)
        if is_local:
            if len(local_lines) < 2:
                who = "、".join(present[:4]) if present else (
                    str(anchor.name) if anchor is not None else "暗流")
                local_lines.append(f"- 《{arc.title}》阶段「{st.name}」：{str(st.desc)[:36]}（在场：{who}）")
        elif len(far_lines) < 2 and anchor is not None:
            far_lines.append(f"- 远方：《{arc.title}》正在「{anchor.name}」发酵")
    lines = local_lines + far_lines
    if not lines:
        return ""
    return ("【当前剧情线】（正在这个世界发生的事，可作旁白氛围；"
            "NPC 仅按亲历、同地目击或已知传闻发言，不得仅凭本块知晓内幕；"
            "不得自行推进阶段或宣布结局——剧情走向以世界推进为准）\n"
            + "\n".join(lines))


# ============================================================
# 题材模板池（兜底线措辞；§23：6 题材 x {conflict, scheme} 各 >=6 变体，守卫测试钉量）
# 占位符只允许 {A}=甲（conflict: 策划势力 / scheme: 策划人）、{B}=乙、{C}=核心人物、
# {loc}=锚定地点。变体 = (title, premise, ((n1, d1), (n2, d2), (n3, d3)))。
# ============================================================

# 旧 2-kind 模板池已迁移至 story_arc_templates.py（21 原型 × 6 题材）。

