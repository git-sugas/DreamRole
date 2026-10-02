"""[④ 2026-08-30] 共享世界知识分层——市井传闻引擎（纯 Python；低频 LLM 口述化在 world_sim_service）。

把 event_log 里的 major/crisis 事件水位增量收编进 rumor_pending，累积触发后由 LLM 口述化成
「市井传闻」（一句有风味、市井口吻、仍中性的传闻），存 shared_rumors。knows(npc, rumor) 按
NPC 势力/地点/社交链/商人身份过滤「该 NPC 知不知道这条传闻」，实现信息不对称——不同 NPC
说不同的话。

[!] 注入预算固定：在场 NPC 所知传闻每人 cap RUMOR_PER_NPC；shared_rumors 滚动保留 RUMOR_KEEP。
[!] 传闻需要关联字段（factions/locations/npcs）才能做 knows 过滤；无关联字段的 major/crisis
    事件不收编（tick_rumors 的 trivial 传闻不填关联字段，本就不在收编范围）。
"""
from __future__ import annotations

from typing import Any

from src.services import chronicle_engine as che

# [④ 定稿] 计数触发参数
RUMOR_TRIGGER = 20          # pending 累积到 20 条触发口述化
RUMOR_BATCH = 15            # 每次口述化最老 15 条
RUMOR_KEEP = 24             # shared_rumors 滚动保留 24 条
_PENDING_CAP = 40           # pending 存储上限
RUMOR_PER_NPC = 2           # 每 NPC 注入传闻条数上限
_DESC_LEN = 80              # 回退模板 desc 截断
_TEXT_LEN = 60              # 口述化传闻文本截断


def tick_day(tick: int) -> int:
    """tick -> day（tick//12+1，与 chronicle_engine 同口径）。"""
    return max(1, int(tick) // 12 + 1)


def collect(world: Any) -> int:
    """把 event_log 新增的 major/crisis 事件（带关联字段）收编进 rumor_pending；返回新增数。

    [!] 水位增量收编 + 按 (tick,title) 去重，免疫 event_log 尾裁剪（仿 chronicle_engine）。
    """
    wm = int(getattr(world, "rumor_watermark_tick", 0) or 0)
    pend = getattr(world, "rumor_pending", None)
    if pend is None:
        pend = []
        world.rumor_pending = pend
    seen = {(int(p.get("tick", 0) or 0), str(p.get("title", "") or "")) for p in pend}
    added = 0
    max_tick = wm
    for e in (getattr(world, "event_log", None) or []):
        t = int(getattr(e, "tick", 0) or 0)
        if t > max_tick:
            max_tick = t
        if t <= wm or str(getattr(e, "severity", "") or "") not in ("major", "crisis"):
            continue
        facs = list(getattr(e, "factions", None) or [])
        locs = list(getattr(e, "locations", None) or [])
        involved = list(getattr(e, "npcs", None) or [])
        if not facs and not locs and not involved:
            continue  # 无关联字段，无法做 knows 过滤，不收编
        key = (t, str(getattr(e, "title", "") or ""))
        if key in seen:
            continue
        seen.add(key)
        pend.append({
            "tick": t,
            "title": str(getattr(e, "title", "") or ""),
            "desc": str(getattr(e, "desc", "") or "")[:_DESC_LEN],
            "category": str(getattr(e, "category", "") or ""),
            "factions": facs, "locations": locs, "npcs": involved,
        })
        added += 1
    world.rumor_watermark_tick = max_tick
    if len(pend) > _PENDING_CAP:
        del pend[:len(pend) - _PENDING_CAP]
    return added


def take_batch(world: Any) -> list:
    """取出待口述化的最老一批（不足触发线返回空）。"""
    pend = getattr(world, "rumor_pending", None) or []
    if len(pend) < RUMOR_TRIGGER:
        return []
    return list(pend[:RUMOR_BATCH])


def commit(world: Any, batch: list, texts: list) -> None:
    """把 LLM 口述化文本按序写进 shared_rumors（失败回退 desc），推进 pending 水位。"""
    rumors = getattr(world, "shared_rumors", None)
    if rumors is None:
        rumors = []
        world.shared_rumors = rumors
    for i, b in enumerate(batch):
        if not isinstance(b, dict):
            continue
        raw = texts[i] if i < len(texts) else ""
        text = str(raw or "").strip()
        if not text:
            text = str(b.get("desc", "") or "")
        rumors.append({
            "tick": int(b.get("tick", 0) or 0),
            "title": str(b.get("title", "") or ""),
            "text": text[:_TEXT_LEN],
            "category": str(b.get("category", "") or ""),
            "factions": list(b.get("factions") or []),
            "locations": list(b.get("locations") or []),
            "npcs": list(b.get("npcs") or []),
        })
    if len(rumors) > RUMOR_KEEP:
        del rumors[:len(rumors) - RUMOR_KEEP]
    del world.rumor_pending[:len(batch)]


def knows(npc: Any, rumor: dict) -> bool:
    """该 NPC 是否知道这条传闻：同势力 / 同地点 / 与涉事 NPC 有社交链 / 商人认经济类。"""
    if not isinstance(rumor, dict):
        return False
    fid = str(getattr(npc, "faction_id", "") or "")
    lid = str(getattr(npc, "location_id", "") or "")
    if fid and fid in (rumor.get("factions") or []):
        return True
    if lid and lid in (rumor.get("locations") or []):
        return True
    social = getattr(npc, "social", None) or {}
    nid = str(getattr(npc, "id", "") or "")
    for inv in (rumor.get("npcs") or []):
        if str(inv) != nid and int(social.get(str(inv), 0) or 0) != 0:
            return True
    if getattr(npc, "is_merchant", False) and str(rumor.get("category", "") or "") == "economy":
        return True
    return False


def rumor_lines_for(world: Any, npc: Any, cap: int = RUMOR_PER_NPC) -> list:
    """该 NPC 所知的传闻行列表（截 cap）。"""
    out = []
    for r in (getattr(world, "shared_rumors", None) or []):
        if knows(npc, r):
            out.append(f"「{r.get('title', '')}」{r.get('text', '')}")
        if len(out) >= cap:
            break
    return out


def voiced_text(world: Any, ev: Any) -> str:
    """若事件已被口述化进 shared_rumors，返回其传闻文本；否则空串。"""
    key = (int(getattr(ev, "tick", 0) or 0), str(getattr(ev, "title", "") or ""))
    for r in (getattr(world, "shared_rumors", None) or []):
        if (int(r.get("tick", 0) or 0), str(r.get("title", "") or "")) == key:
            return str(r.get("text", "") or "")
    return ""


def hot_topic_lines(world: Any) -> list:
    """【坊间热议】注入行（优先用已口述化的传闻文本；复用 chronicle 的相关性排序）。"""
    _TAG = {"crisis": "★危机", "major": "◆重大", "minor": "·动态"}
    out = []
    for e in che.hot_topics(world):
        day = tick_day(int(getattr(e, "tick", 0) or 0))
        tag = _TAG.get(str(getattr(e, "severity", "") or ""), "·动态")
        text = voiced_text(world, e)
        if not text:
            text = str(getattr(e, "desc", "") or "")[:_DESC_LEN]
        out.append(f"  [第{day}天|{tag}] {getattr(e, 'title', '')}：{text}")
    return out
