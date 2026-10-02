"""[P39d+e] 剧情导演层 + 世界编年史引擎（纯 Python + SeededRng；零 LLM 零 Qt）。

导演层（零 LLM）：把世界事件流从「按时间罗列」升级为「按与玩家的相关性排序」注入
场景上下文【坊间热议】——相关性权重 [定稿]：地点 3 > 关系 2 > 历史行为 1（引擎常量
可调）。世界事件自动变成玩家面前的谈资。

编年史（低频 LLM）：World.chronicle_pending 收编 event_log 的 major/crisis 事件
（按 tick 水位增量收编，免疫 event_log 尾裁剪）；累积 40 条触发一次 LLM 总结最老 30 条
成一条编年史（<=120 字；失败回退引擎拼接事件标题，同样推进水位防死循环——仿场景
前情压缩「计数触发压最老」模式 [用户定稿]）；滚动保留 8 条注入【世界编年史】最近 2 条。

[!] 注入预算固定（不随世界年龄涨）：热议 top3 x desc 截 80 + 编年史 2 条 x text 截 120。
"""
from __future__ import annotations

from typing import Any

# [定稿 2026-08-22] 计数触发参数
CHRONICLE_TRIGGER = 40     # pending 累积到 40 条触发沉淀
CHRONICLE_BATCH = 30       # 每次沉淀最老 30 条
CHRONICLE_KEEP = 8         # 编年史滚动保留 8 条
_PENDING_CAP = 80          # pending 存储上限（触发线之上的余量）

# [定稿] 相关性权重（地点 > 关系 > 历史；常量可调）
W_LOC, W_REL, W_BASE = 3, 2, 1
_HOT_WINDOW = 30           # 热议候选窗口（近 30 条 minor+ 事件）
_HOT_TOP_N = 3
_DESC_LEN = 80
_TEXT_LEN = 120


def tick_day(tick: int) -> int:
    """tick -> day（tick//12+1，与 _tick_time_phase 换算同口径）。"""
    return max(1, int(tick) // 12 + 1)


def collect_pending(world: Any) -> int:
    """把 event_log 新增的 major/crisis 事件收编进 pending（按 tick 水位增量；返回新增数）。

    [!] 收编后存 pending 副本，免疫 event_log 尾裁剪；去重按 (tick, title)。
    """
    wm = int(getattr(world, "chronicle_watermark_tick", 0) or 0)
    pend = getattr(world, "chronicle_pending", None)
    if pend is None:
        pend = []
        world.chronicle_pending = pend
    seen = {(int(p.get("tick", 0) or 0), str(p.get("title", "") or "")) for p in pend}
    added = 0
    max_tick = wm
    for e in (getattr(world, "event_log", None) or []):
        t = int(getattr(e, "tick", 0) or 0)
        if t > max_tick:
            max_tick = t
        if t <= wm or str(getattr(e, "severity", "") or "") not in ("major", "crisis"):
            continue
        key = (t, str(getattr(e, "title", "") or ""))
        if key in seen:
            continue
        seen.add(key)
        pend.append({"tick": t, "title": str(getattr(e, "title", "") or ""),
                     "desc": str(getattr(e, "desc", "") or "")[:_DESC_LEN]})
        added += 1
    world.chronicle_watermark_tick = max_tick
    if len(pend) > _PENDING_CAP:
        del pend[:len(pend) - _PENDING_CAP]
    return added


def relevance_score(world: Any, ev: Any) -> int:
    """事件与玩家的相关性：基础 1 + 地点 3（涉玩家当前地）+ 关系 2（涉好友/同伴/有声望势力）。"""
    p = getattr(world, "player", None)
    if p is None:
        return W_BASE
    score = W_BASE
    if str(getattr(p, "location_id", "") or "") in (getattr(ev, "locations", None) or []):
        score += W_LOC
    related = set(getattr(p, "friend_npc_ids", None) or []) \
        | set(getattr(p, "companion_npc_ids", None) or [])
    if related & set(getattr(ev, "npcs", None) or []):
        score += W_REL
    rep = getattr(p, "reputation", None)
    if isinstance(rep, dict) and set(rep.keys()) & set(getattr(ev, "factions", None) or []):
        score += W_REL
    return score


def hot_topics(world: Any, n: int = _HOT_TOP_N) -> list:
    """【坊间热议】候选：近窗口 minor+ 事件按（相关性 desc, tick desc）取 top n。"""
    pool = [e for e in (getattr(world, "event_log", None) or [])[-_HOT_WINDOW:]
            if str(getattr(e, "severity", "") or "") in ("minor", "major", "crisis")]
    ranked = sorted(pool, key=lambda e: (-relevance_score(world, e), -int(getattr(e, "tick", 0) or 0)))
    return ranked[:max(1, int(n))]


def fallback_summary(batch: list) -> str:
    """LLM 不可用/失败的引擎拼接：事件标题串联（确定性；同样推进水位防死循环）。"""
    titles = [str(b.get("title", "") or "") for b in batch if b.get("title")]
    if not titles:
        return "这段岁月风平浪静，无事可记。"
    days = [tick_day(int(b.get("tick", 0) or 0)) for b in batch]
    span = f"第{min(days)}-{max(days)}天" if min(days) != max(days) else f"第{min(days)}天"
    step = max(1, len(titles) // 6)                          # 至多引 6 个标题控预算
    picked = titles[::step][:6]
    return f"{span}：{'；'.join(picked)}等大事接连发生。"


def take_batch(world: Any) -> list:
    """取出待沉淀的最老一批（<=CHRONICLE_BATCH；不足触发线返回空）。"""
    pend = getattr(world, "chronicle_pending", None) or []
    if len(pend) < CHRONICLE_TRIGGER:
        return []
    return list(pend[:CHRONICLE_BATCH])


def commit_chronicle(world: Any, batch: list, text: str) -> None:
    """沉淀一条编年史并推进 pending 水位（滚动保留 CHRONICLE_KEEP 条）。"""
    text = str(text or "").strip()[:_TEXT_LEN * 2] or fallback_summary(batch)
    days = [tick_day(int(b.get("tick", 0) or 0)) for b in batch] or [1]
    chron = getattr(world, "chronicle", None)
    if chron is None:
        chron = []
        world.chronicle = chron
    chron.append({"start_day": min(days), "end_day": max(days), "text": text})
    if len(chron) > CHRONICLE_KEEP:
        del chron[:len(chron) - CHRONICLE_KEEP]
    del world.chronicle_pending[:len(batch)]


def chronicle_lines(world: Any, n: int = 2) -> list:
    """【世界编年史】注入行：最近 n 条 `第X-Y天：text 截 120`。"""
    chron = (getattr(world, "chronicle", None) or [])[-max(1, int(n)):]
    return [f"第{int(c.get('start_day', 1) or 1)}-{int(c.get('end_day', 1) or 1)}天："
            f"{str(c.get('text', '') or '')[:_TEXT_LEN]}" for c in chron if isinstance(c, dict)]


def hot_topic_lines(world: Any) -> list:
    """【坊间热议】注入行：`[第X天|标签] 标题：desc 截 80`。"""
    _TAG = {"crisis": "★危机", "major": "◆重大", "minor": "·动态"}
    out = []
    for e in hot_topics(world):
        day = tick_day(int(getattr(e, "tick", 0) or 0))
        tag = _TAG.get(str(getattr(e, "severity", "") or ""), "·动态")
        desc = str(getattr(e, "desc", "") or "")[:_DESC_LEN]
        out.append(f"  [第{day}天|{tag}] {getattr(e, 'title', '')}：{desc}")
    return out
