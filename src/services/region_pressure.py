"""[G03 初版/R3 2026-09-30] 地区压力与纾解引擎（纯 Python，零 LLM 零 rng）。

有期限、有来源的地区修正（守计划 G03 #1：「用有期限、有来源的地区修正记录表达
短缺/恢复；不能直接永久乘写基础价格，也不能每 tick 复利相乘」）：

- 数据：`World.region_modifiers` list[dict] {id, kind(shortage/recovery), scope,
  location_id, buy_mult, sell_mult, start_day, until_day, source}——**只在本乘区读时
  即时相乘，绝不写回 entry.price/base_price**（漂移锚公式价机制不动）。
- 来源（首批三条，均真实事件）：
  (1) 世界 Boss 在窗 -> shortage（buy 1.25 / sell 0.85，随窗到期）；
      Boss 被击败 -> 换 recovery（buy 0.90 / sell 1.10，5 天，来源「威胁铲除」）。
  (2) deliver_items 任务真实交付 -> 小额 recovery（buy 0.95 / sell 1.05，4 天）。
  (3) 秘境通关（矿路复通类）-> recovery（buy 0.92 / sell 1.08，5 天）。
- 消费：`tre.market_price_factors` 单一入口追加 `region_price_factors`（与季节/
  财富同座）；单因子各自钳 0.80-1.30，不做总钳（守 §22 物价联动契约）。NPC 采购
  恒挂牌价不调本乘区（既有豁免天然成立）。
- 过期：tick 清理 + minor 事件（短缺缓解/行情回落——因果可见性）。
- 去重：同 (location, kind, scope) 只留最新一条（替换旧档，防叠乘复利）。
"""
from __future__ import annotations

from typing import Any, Optional

_KIND_VALUES = ("shortage", "recovery")
# 单因子钳（乘区口径；shortage 与 recovery 各自钳顶，不做总钳）
_CLAMP = (0.80, 1.30)

BOSS_SHORTAGE = {"buy": 1.25, "sell": 0.85, "days": None}       # days=None 随 Boss 窗
BOSS_RELIEF = {"buy": 0.90, "sell": 1.10, "days": 5}
DELIVER_RELIEF = {"buy": 0.95, "sell": 1.05, "days": 4}
DUNGEON_RELIEF = {"buy": 0.92, "sell": 1.08, "days": 5}


def _clamp(v: float) -> float:
    return max(_CLAMP[0], min(_CLAMP[1], float(v)))


def add_modifier(world: Any, kind: str, scope: str, location_id: str,
                 buy_mult: float, sell_mult: float, days: Optional[int],
                 source: str, day: Optional[int] = None) -> Optional[dict]:
    """落一条地区修正（同 location+kind+scope 去重替换；days=None 永久至被替换/移除）。"""
    if kind not in _KIND_VALUES:
        return None
    d = max(1, int(day if day is not None else getattr(world, "day_count", 1) or 1))
    mods = getattr(world, "region_modifiers", None)
    if not isinstance(mods, list):
        mods = []
        world.region_modifiers = mods
    mods[:] = [m for m in mods if not (
        isinstance(m, dict) and str(m.get("location_id", "")) == str(location_id)
        and str(m.get("kind", "")) == kind and str(m.get("scope", "")) == str(scope))]
    entry = {
        "kind": kind, "scope": str(scope)[:24], "location_id": str(location_id),
        "buy_mult": round(_clamp(buy_mult), 3), "sell_mult": round(_clamp(sell_mult), 3),
        "start_day": d,
        "until_day": (d + max(1, int(days))) if days is not None else 0,   # 0=无期（随源）
        "source": str(source)[:60],
    }
    mods.append(entry)
    return entry


def remove_modifiers(world: Any, location_id: str, scope: str, kind: str = "") -> int:
    """移除匹配修正（Boss 离去/击败换档用）。返回移除数。"""
    mods = getattr(world, "region_modifiers", None) or []
    keep = []
    n = 0
    for m in mods:
        hit = (isinstance(m, dict)
               and str(m.get("location_id", "")) == str(location_id)
               and str(m.get("scope", "")) == str(scope)
               and (not kind or str(m.get("kind", "")) == kind))
        if hit:
            n += 1
        else:
            keep.append(m)
    world.region_modifiers = keep
    return n


def region_price_factors(world: Any, location_id: str) -> "tuple[float, float, str]":
    """某地点的修正乘区（buy, sell, tag）。零 rng；过期条即时视作不存在。

    [!] tag 只在有修正时非空（拼进 market note/叙事 hint——玩家看得见「为什么贵」）。"""
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    buy = sell = 1.0
    notes: list = []
    for m in (getattr(world, "region_modifiers", None) or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("location_id", "")) != str(location_id):
            continue
        ud = int(m.get("until_day", 0) or 0)
        if ud and day >= ud:
            continue                                    # 已过期（tick 会物理清理）
        buy *= float(m.get("buy_mult", 1.0) or 1.0)
        sell *= float(m.get("sell_mult", 1.0) or 1.0)
        kind = str(m.get("kind", ""))
        src = str(m.get("source", "") or "")
        notes.append(("战乱短缺" if kind == "shortage" else "货源纾解") +
                     (f"（{src}）" if src else ""))
    # 多条同地相乘后再各自语义钳一次（同 location 通常至多 1-2 条；不做总钳的口径
    # 指「单因子来源各自钳顶」，此处对合成值再钳回 0.80-1.30 防极端叠乘）
    buy = _clamp(buy)
    sell = _clamp(sell)
    return buy, sell, ("、".join(dict.fromkeys(notes)) if notes else "")


def tick_expire(world: Any) -> list:
    """过期物理清理 + minor 事件（挂 tick_world；返回 WorldEvent 列表）。"""
    from src.models.world import WorldEvent
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    keep, events = [], []
    for m in (getattr(world, "region_modifiers", None) or []):
        if not isinstance(m, dict):
            continue
        ud = int(m.get("until_day", 0) or 0)
        if ud and day >= ud:
            kind = str(m.get("kind", ""))
            loc = next((str(l.name) for l in (getattr(world, "locations", None) or [])
                        if str(getattr(l, "id", "")) == str(m.get("location_id", ""))), "")
            events.append(WorldEvent(
                tick=int(getattr(world, "tick_count", 0) or 0), category="event",
                severity="minor",
                title="行情回落" if kind == "recovery" else "短缺缓解",
                desc=f"{loc or '此地'}的" + ("优惠行情已回落" if kind == "recovery"
                                             else "紧缺行情有所缓解") + "。",
                locations=[str(m.get("location_id", ""))],
            ))
        else:
            keep.append(m)
    world.region_modifiers = keep
    return events


# ============ 来源接线（真实事件 -> 修正）============
def pressure_from_world_boss(world: Any, boss: Any) -> Optional[dict]:
    """Boss 在窗 -> 挂 shortage（随窗无期，窗由 Boss 侧管理；离去/击败时移除/换档）。"""
    until = int(getattr(boss, "window_until_day", 0) or 0)
    m = add_modifier(world, "shortage", "boss", str(getattr(boss, "location_id", "")),
                     BOSS_SHORTAGE["buy"], BOSS_SHORTAGE["sell"], days=None,
                     source=f"世界级威胁「{getattr(boss, 'name', '?')}」盘踞")
    if m is not None:
        m["until_day"] = max(0, until)                  # 对齐 Boss 窗（0=无期兜底）
    return m


def relief_from_boss_defeated(world: Any, boss: Any) -> Optional[dict]:
    """Boss 被击败 -> 移除 shortage、换 recovery（5 天优惠，来源「威胁铲除」）。"""
    loc = str(getattr(boss, "location_id", ""))
    remove_modifiers(world, loc, "boss", kind="shortage")
    return add_modifier(world, "recovery", "boss_defeated", loc,
                        BOSS_RELIEF["buy"], BOSS_RELIEF["sell"],
                        days=BOSS_RELIEF["days"], source="威胁铲除，商路复安")


def relief_from_delivery(world: Any, location_id: str, item_name: str) -> Optional[dict]:
    """deliver_items 真实交付 -> 小额 recovery（4 天；守恒交付的行情回响）。"""
    return add_modifier(world, "recovery", "delivery", str(location_id),
                        DELIVER_RELIEF["buy"], DELIVER_RELIEF["sell"],
                        days=DELIVER_RELIEF["days"],
                        source=f"货到了：{item_name}")


def relief_from_dungeon_clear(world: Any, dungeon: Any) -> Optional[dict]:
    """秘境通关（矿路复通类）-> recovery（5 天；入口地点行情）。"""
    return add_modifier(world, "recovery", "dungeon_clear",
                        str(getattr(dungeon, "location_id", "")),
                        DUNGEON_RELIEF["buy"], DUNGEON_RELIEF["sell"],
                        days=DUNGEON_RELIEF["days"],
                        source=f"「{getattr(dungeon, 'name', '秘境')}」肃清，道路复通")
