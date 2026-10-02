"""[P35] 种植系统引擎（纯 Python + SeededRng，零 LLM/Qt）。

数值范式（守 §21）：生长/枯萎/产量/变异全引擎确定性计算；LLM 不参与。
种子->作物映射靠确定性 id（`seed_{key}`/`crop_{key}`，key=md5(题材|作物名)[:8]），
零模型字段（Item 只带 category=seed / craft，商店/背包/价格钩子天然工作）。

生长模型：
- plot.grown = 累计生长天（浇水当日 +1；雨天免浇自动 +1；stage = grown // stage_len 钳 3）。
- stage_len = max(1, round(base_days * 季节系数 / 3))；宜季 0.7 / 全季 1.0 / 非宜季 1.5
  （季节由 day_count 纯推导：1 年 4 季 x 3 月 x 30 日，守 calendar_engine 口径）。
- 枯萎：连续 wither_days 天既没浇也不是雨 -> 清地块 + 事件。
- 收获：产量 = base x 勤奋系数 x rng 抖动 [0.8,1.2]（虫害 x0.5，浇水除虫）；背包为去重
  id 语义——首件入包，多余份数就地按市价折现成金币；变异 roll（0.08 + luk*0.004 钳
  0.05-0.25）产高一档 rarity 克隆作物（`{crop_id}__var`）+ 返还 1 颗同种种子。
- [!] 事件必须返回 WorldEvent 实体（tick 消费链 e.severity 排序 + e.to_dict() 落盘，
  混入裸 dict 必崩且污染存档）。
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from src.models.world_sim_preset import GenreText
from src.utils.rng import SeededRng

_STAGE_NAMES = ("播种", "发芽", "生长", "成熟")
_RARITY_ORDER = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
_WITHER_DEFAULT = 3

# ---- 题材作物池（数据量铁律：6 题材 x >=6；season 0-3 春夏秋冬，None=全季）----
_GENRE_FARM_CROPS: dict[str, list[dict]] = {
    "xianxia": [
        {"name": "灵稻", "base_days": 3, "rarity": "common", "season": 1},
        {"name": "凝露草", "base_days": 4, "rarity": "common", "season": 0},
        {"name": "紫金葫芦", "base_days": 5, "rarity": "uncommon", "season": 1},
        {"name": "九叶灵芝", "base_days": 6, "rarity": "rare", "season": 2},
        {"name": "冰心莲", "base_days": 7, "rarity": "rare", "season": 3},
        {"name": "龙血藤", "base_days": 8, "rarity": "epic", "season": 1},
    ],
    "wuxia": [
        {"name": "白芷", "base_days": 3, "rarity": "common", "season": 0},
        {"name": "金银花", "base_days": 4, "rarity": "common", "season": 1},
        {"name": "川贝", "base_days": 5, "rarity": "uncommon", "season": 2},
        {"name": "何首乌", "base_days": 6, "rarity": "uncommon", "season": 2},
        {"name": "天山雪莲", "base_days": 7, "rarity": "rare", "season": 3},
        {"name": "千年人参", "base_days": 8, "rarity": "epic", "season": 2},
    ],
    "western_fantasy": [
        {"name": "小麦", "base_days": 3, "rarity": "common", "season": 1},
        {"name": "胡萝卜", "base_days": 3, "rarity": "common", "season": 0},
        {"name": "薰衣草", "base_days": 4, "rarity": "common", "season": 1},
        {"name": "葡萄", "base_days": 5, "rarity": "uncommon", "season": 2},
        {"name": "月光草", "base_days": 6, "rarity": "rare", "season": 0},
        {"name": "曼德拉草", "base_days": 8, "rarity": "epic", "season": 2},
    ],
    "modern": [
        {"name": "生菜", "base_days": 3, "rarity": "common", "season": 0},
        {"name": "番茄", "base_days": 4, "rarity": "common", "season": 1},
        {"name": "草莓", "base_days": 4, "rarity": "uncommon", "season": 0},
        {"name": "香菇", "base_days": 5, "rarity": "uncommon", "season": 2},
        {"name": "藏红花", "base_days": 7, "rarity": "rare", "season": 2},
        {"name": "黑松露", "base_days": 8, "rarity": "epic", "season": 3},
    ],
    "scifi": [
        {"name": "营养藻", "base_days": 3, "rarity": "common", "season": None},
        {"name": "月面土豆", "base_days": 4, "rarity": "common", "season": None},
        {"name": "发光菌菇", "base_days": 5, "rarity": "uncommon", "season": None},
        {"name": "零重力葡萄", "base_days": 6, "rarity": "rare", "season": None},
        {"name": "星尘麦", "base_days": 6, "rarity": "rare", "season": None},
        {"name": "异星灵果", "base_days": 8, "rarity": "epic", "season": None},
    ],
    "apocalypse": [
        {"name": "变异土豆", "base_days": 3, "rarity": "common", "season": None},
        {"name": "净化苔", "base_days": 4, "rarity": "common", "season": 0},
        {"name": "辐射小麦", "base_days": 5, "rarity": "uncommon", "season": 2},
        {"name": "抗性玉米", "base_days": 5, "rarity": "uncommon", "season": 1},
        {"name": "血果", "base_days": 6, "rarity": "rare", "season": 1},
        {"name": "净世花", "base_days": 8, "rarity": "epic", "season": 0},
    ],
}

# ---- 题材事件文案池（数据量铁律：6 题材 x >=4，虫害/灵雨各 >=2；{crop} 占位）----
_GENRE_FARM_EVENTS: dict[str, dict] = {
    "xianxia": {
        "pest": ["灵虫啃食{crop}的灵叶，长势受阻。", "一只噬灵幼虫钻进{crop}根部。"],
        "rain": ["灵雨普降，{crop}贪婪吸收天地灵气。", "甘霖自云端垂落，{crop}舒展枝叶。"],
        "wither": ["{crop}灵气断绝枯萎，化作尘土。"],
    },
    "wuxia": {
        "pest": ["蚜虫爬满{crop}嫩叶。", "蛀虫蛀空{crop}茎秆。"],
        "rain": ["一场春雨润泽{crop}。", "细雨绵绵，{crop}长势喜人。"],
        "wither": ["无人照料的{crop}枯死田中。"],
    },
    "western_fantasy": {
        "pest": ["蝗虫过境啃食{crop}。", "地精偷啃{crop}的根须。"],
        "rain": ["雨水滋润着{crop}。", "云层聚拢降下甘霖，{crop}焕发生机。"],
        "wither": ["{crop}在烈日下枯萎了。"],
    },
    "modern": {
        "pest": ["菜青虫盯上了{crop}。", "蜗牛夜间啃秃{crop}。"],
        "rain": ["一场夜雨浇透{crop}。", "梅雨季的{crop}格外水灵。"],
        "wither": ["忘记浇水的{crop}干枯发黄。"],
    },
    "scifi": {
        "pest": ["寄生孢子侵染{crop}培养舱。", "舱内霉菌污染了{crop}。"],
        "rain": ["冷凝水循环补给，{crop}细胞加速分裂。", "人工降雨程序启动，{crop}长势良好。"],
        "wither": ["{crop}营养液枯竭而退化。"],
    },
    "apocalypse": {
        "pest": ["变异甲虫咬穿{crop}表皮。", "酸蚀孢子在{crop}上蔓延。"],
        "rain": ["一场灰雨意外滋养了{crop}。", "辐射云落下稀薄雨水，{crop}挺直茎秆。"],
        "wither": ["{crop}在辐射尘中枯槁而死。"],
    },
}


def _genre_id(world: Any) -> str:
    ov = getattr(world, "config_overlay", None) or {}
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    return str(tid or "western_fantasy")


# [修 2026-10-02 真机 P3·灵田跨题材泄漏] 种子描述「播种于灵田」硬编码仙侠词——
# 末日世界四种种子描述全中招（§23 显示名题材适配）。田名与 domain 池 farm type 同词
# （仙侠=灵田/武侠=田亩/西幻=农田/现代=田地/科幻=种植舱/末日=净化圃），缺键回退西幻。
_GENRE_FIELD_NAME: dict[str, str] = {
    "xianxia": "灵田",
    "wuxia": "田亩",
    "western_fantasy": "农田",
    "modern": "田地",
    "scifi": "种植舱",
    "apocalypse": "净化圃",
}


def field_display_name(world: Any) -> str:
    """本题材的田地显示名（种子描述/种植文案用，缺键回退西幻「农田」）。"""
    return _GENRE_FIELD_NAME.get(_genre_id(world), "农田")


def genre_crops(world: Any) -> list[dict]:
    """本题材作物池（缺键回退西幻，守 §23）。"""
    return list(_GENRE_FARM_CROPS.get(_genre_id(world))
                or _GENRE_FARM_CROPS["western_fantasy"])


def _crop_key(tid: str, name: str) -> str:
    return hashlib.md5(f"{tid}|{name}".encode("utf-8")).hexdigest()[:8]


def seed_id_of(tid: str, name: str) -> str:
    return f"seed_{_crop_key(tid, name)}"


def crop_id_of(tid: str, name: str) -> str:
    return f"crop_{_crop_key(tid, name)}"


def crop_of_seed(world: Any, seed_id: str) -> Optional[dict]:
    """种子 id -> 本题材作物定义（含派生 seed_id/crop_id）。跨题材种子返回 None。"""
    tid = _genre_id(world)
    for c in _GENRE_FARM_CROPS.get(tid) or _GENRE_FARM_CROPS["western_fantasy"]:
        c = dict(c)
        c["seed_id"] = seed_id_of(tid, c["name"])
        c["crop_id"] = crop_id_of(tid, c["name"])
        if c["seed_id"] == seed_id:
            return c
    return None


def season_of(world: Any) -> int:
    """当前季节 0-3（春夏秋冬）；1 年 4 季 x 3 月 x 30 日，由 day_count 纯推导。"""
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    month = (day - 1) // 30 + 1          # 1-12
    return (month - 1) // 3              # 0-3


def season_mult(world: Any, crop: dict) -> float:
    s = crop.get("season", None)
    if s is None:
        return 1.0
    return 0.7 if int(s) == season_of(world) else 1.5


def stage_len(world: Any, crop: dict) -> int:
    need = max(1, int(crop.get("base_days", 4) or 4)) * season_mult(world, crop)
    return max(1, int(round(need / 3)))


def plot_capacity(home: Any) -> int:
    """地块数 = garden 建筑等级 + 1（Lv1=2 块）；无建筑 0。"""
    lvl = _garden_level(home)
    return (lvl + 1) if lvl > 0 else 0


def _garden_level(home: Any) -> int:
    from src.services import home_engine as hm
    return hm.building_level(home, "garden")


def _event_text(world: Any, kind: str, crop_name: str) -> str:
    tid = _genre_id(world)
    pool = (_GENRE_FARM_EVENTS.get(tid) or _GENRE_FARM_EVENTS["western_fantasy"]).get(kind) or []
    if not pool:
        return kind
    rng = SeededRng.seed_from(str(getattr(world, "id", "")),
                              int(getattr(world, "day_count", 1) or 1), f"farm_txt_{kind}")
    return rng.pick(pool).replace("{crop}", crop_name)


def _mk_event(world: Any, title: str, desc: str, severity: str = "minor"):
    """[!] 必须返回 WorldEvent 实体——tick_world 消费链对 events 做 e.severity 排序与
    e.to_dict() 落盘，混入裸 dict 会崩整轮 tick 且污染 event_log 存档（审查发现）。"""
    from src.models.world import WorldEvent
    return WorldEvent(tick=int(getattr(world, "tick_count", 0) or 0),
                      category="economy", severity=severity, title=title, desc=desc)


def is_seed_item(item: Any) -> bool:
    return (getattr(item, "category", "") or "") == "seed"


# ---------- 玩家操作（UI 即时调用；返回 (ok, msg)） ----------

def plant(world: Any, home: Any, seed_item_id: str) -> tuple[bool, str]:
    """播种：校验 garden 建筑/空地块/种子在包 -> 消耗种子入地块。"""
    p = world.player
    if _garden_level(home) <= 0:
        return False, "需先建造灵田（garden 建筑）"
    if len(home.garden) >= plot_capacity(home):
        return False, "田地已满，升级灵田扩地块"
    item = next((i for i in (getattr(world, "items", None) or [])
                 if getattr(i, "id", "") == seed_item_id), None)
    if item is None or not is_seed_item(item):
        return False, "该物品不是种子"
    inv = getattr(p, "inventory", None) or []
    if seed_item_id not in inv:
        return False, "背包里没有这颗种子"
    crop = crop_of_seed(world, seed_item_id)
    if crop is None:
        return False, "此种子不属于本地风土（题材不符）"
    inv.remove(seed_item_id)
    home.garden.append({
        "seed_id": seed_item_id, "planted_day": max(1, int(getattr(world, "day_count", 1) or 1)),
        "stage": 0, "grown": 0, "watered": False, "dry_days": 0, "pest": False,
    })
    # [C8 修复 2026-08-25] 提示与实际成熟同口径（stage_len*3），不再用 round(base*mult) 另算一套
    return True, f"播下{item.name}（{crop['name']}），约需 {stage_len(world, crop) * 3} 天成熟。"


def water(world: Any, home: Any, idx: int) -> tuple[bool, str]:
    """浇水：每块地每日一次；顺手除虫（pest 清除）。成熟地块拒绝（无需再浇）。"""
    if idx < 0 or idx >= len(home.garden):
        return False, "没有这块地"
    plot = home.garden[idx]
    if int(plot.get("stage", 0) or 0) >= 3:
        return False, "已经成熟，等待收获"
    if plot.get("watered"):
        return False, "今天已经浇过了"
    plot["watered"] = True
    if plot.get("pest"):
        plot["pest"] = False
        return True, "浇水时顺手除了虫，作物恢复长势。"
    return True, "浇过水了，作物今日稳步生长。"


def harvest(world: Any, home: Any, idx: int, rng: Optional[SeededRng] = None) -> dict:
    """收获：成熟才可收；产量含勤奋/虫害/变异修正；入包统一 try_add_to_inventory。"""
    from src.services import combat_engine as ce
    if idx < 0 or idx >= len(home.garden):
        return {"ok": False, "reason": "没有这块地"}
    plot = home.garden[idx]
    if int(plot.get("stage", 0) or 0) < 3:
        return {"ok": False, "reason": "尚未成熟"}
    seed_id = str(plot.get("seed_id", "") or "")
    crop = crop_of_seed(world, seed_id)
    if crop is None:
        home.garden.pop(idx)
        return {"ok": False, "reason": "种子数据异常，地块已清理"}
    if rng is None:
        rng = SeededRng.seed_from(str(getattr(world, "id", "")),
                                  int(getattr(world, "day_count", 1) or 1), f"farm_hv_{seed_id}_{idx}")
    p = world.player
    base = {"common": 2, "uncommon": 2, "rare": 1, "epic": 1, "legendary": 1, "mythic": 1}.get(
        crop["rarity"], 2)
    diligence = 1.0 + 0.15 * max(0, _WITHER_DEFAULT - int(plot.get("dry_days", 0) or 0))
    mult = 0.8 + rng.random() * 0.4                       # [0.8, 1.2) 抖动
    if plot.get("pest"):
        mult *= 0.5
    count = max(1, int(round(base * diligence * mult)))
    variant = rng.chance(_variant_chance(p))
    home.garden.pop(idx)
    # [!] 背包是去重 id 语义（try_add 对已拥有 id 返 False）：首件入包，多余份数就地按
    # 市价折现成金币（产量公式落到金币上，经济自洽；防「复收颗粒无收」审查 bug）。
    out_id = crop["crop_id"]
    items = getattr(world, "items", None) or []
    added = 1 if ce.try_add_to_inventory(p, out_id) else 0
    gold_extra = 0
    if count > added:
        from src.services import trade_engine as tre
        out_item = next((i for i in items if i.id == out_id), None)
        unit_price = tre.compute_item_price(out_item) if out_item is not None else 0
        gold_extra = max(0, int(unit_price) * (count - added))
        p.gold = int(p.gold) + gold_extra
    # 变异：产高一档 rarity 克隆（确定性 id `{crop_id}__var`，独立 id 天然可再入包）+ 返种子
    variant_item_id = ""
    if variant:
        idx_r = _RARITY_ORDER.index(crop["rarity"]) if crop["rarity"] in _RARITY_ORDER else 0
        up = _RARITY_ORDER[min(len(_RARITY_ORDER) - 1, idx_r + 1)]
        variant_item_id = f"{out_id}__var"
        import copy as _copy
        base_item = next((i for i in (getattr(world, "items", None) or []) if i.id == out_id), None)
        if base_item is not None and not any(i.id == variant_item_id for i in world.items):
            clone = _copy.copy(base_item)
            clone.id = variant_item_id
            clone.rarity = up
            clone.name = f"变异{base_item.name}"
            clone.desc = "灵田里种出的变异株，品质更胜寻常。"
            world.items.append(clone)
        ce.try_add_to_inventory(p, variant_item_id)
        ce.try_add_to_inventory(p, seed_id)
    note = f"收获{crop['name']} x{count}"
    if added == 0:
        note += "（已有存货，全部就地出售）"
    elif gold_extra > 0:
        # [审核修复 2026-09-13] §23 币种走 GenreText，勿硬编码「金币」
        _gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
        note += f"（自留 1 份，多出 {count - added} 份出售得 {gold_extra} {_gt.currency}）"
    if variant:
        note += "——种出了变异株并返还一颗种子！"
    return {"ok": True, "crop": crop["name"], "count": count, "added": added,
            "gold_extra": gold_extra, "variant": variant, "variant_item_id": variant_item_id,
            "reason": note}


def _variant_chance(p: Any) -> float:
    from src.services import talent_engine as te
    luk = te.effective_stat(p, "luk")
    return max(0.05, min(0.25, 0.08 + luk * 0.004))


# ---------- tick 推进（per-day 水位） ----------

def advance_farms(world: Any, rng: Optional[SeededRng], wither_days: int = _WITHER_DEFAULT) -> list:
    """每日推进（tick farm 子阶段调）：浇水/雨天生长 +1、枯萎判定、虫害/灵雨事件。

    [!] per-day 水位：world.last_farm_day == day_count 直接返回（休憩跳多天时按
    差值逐日补推——生长是日粒度函数，跳天不能白长也不能跳枯）。
    [!] 事件 roll 按日锚定盐（`farm_rain`/`farm_pest_{seed_id}`），同 world+day 同结果
    与调用顺序无关；rng 参数仅保留签名兼容（harvest 抖动用自己的默认盐）。
    返回事件 dict 列表。
    """
    events: list = []
    homes = [h for h in (getattr(world, "homes", None) or []) if getattr(h, "garden", None)]
    if not homes:
        world.last_farm_day = max(1, int(getattr(world, "day_count", 1) or 1))
        return events
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    last = max(0, int(getattr(world, "last_farm_day", 0) or 0))
    if last == day:
        return events
    wd = max(0, int(wither_days))
    steps = min(30, day - last) if last > 0 else 1     # 补推上限 30 天防异常档跳爆
    for i in range(steps):
        _advance_one_day(world, (day - steps + 1 + i) if last > 0 else day, wd, events)
    world.last_farm_day = day
    return events


def _rain_today(world: Any, day: int) -> bool:
    """灵雨事件 roll（每宅每日 3%，按日锚定）。"""
    return SeededRng.seed_from(str(getattr(world, "id", "")), day, "farm_rain").chance(0.03)


def _pest_today(world: Any, seed_id: str, day: int) -> bool:
    """虫害 roll（每块每日 5%，按日锚定）。"""
    return SeededRng.seed_from(str(getattr(world, "id", "")), day,
                               f"farm_pest_{seed_id}").chance(0.05)


def _advance_one_day(world: Any, day: int, wither_days: int, events: list) -> None:
    raining = str(getattr(world, "weather", "") or "") == "rain"
    for home in (getattr(world, "homes", None) or []):
        garden = getattr(home, "garden", None) or []
        if not garden:
            continue
        # 灵雨事件：全田 grown +2
        if _rain_today(world, day):
            for plot in garden:
                plot["grown"] = int(plot.get("grown", 0) or 0) + 2
            first_crop = _crop_name_of(world, garden[0])
            events.append(_mk_event(
                world, "灵雨润田", _event_text(world, "rain", first_crop or "作物")))
        for plot in list(garden):
            crop = crop_of_seed(world, str(plot.get("seed_id", "") or ""))
            crop_name = crop["name"] if crop else "作物"
            watered = bool(plot.get("watered"))
            if watered or raining:
                plot["grown"] = int(plot.get("grown", 0) or 0) + 1
                plot["dry_days"] = 0
                plot["watered"] = False           # 新的一天，可再浇
            else:
                plot["dry_days"] = int(plot.get("dry_days", 0) or 0) + 1
            # 虫害 roll（每块每日 5%，已生虫不叠）
            if not plot.get("pest") and _pest_today(world, str(plot.get("seed_id", "") or ""), day):
                plot["pest"] = True
                events.append(_mk_event(
                    world, "田间虫害", _event_text(world, "pest", crop_name)))
            # 枯萎判定（wither_days=0 关闭）
            if wither_days > 0 and int(plot.get("dry_days", 0) or 0) > wither_days:
                garden.remove(plot)
                events.append(_mk_event(
                    world, "作物枯萎", _event_text(world, "wither", crop_name)))
                continue
            # 阶段推进
            if crop is not None:
                sl = stage_len(world, crop)
                plot["stage"] = min(3, int(plot.get("grown", 0) or 0) // sl)


def _crop_name_of(world: Any, plot: dict) -> str:
    crop = crop_of_seed(world, str(plot.get("seed_id", "") or ""))
    return crop["name"] if crop else ""


def farm_summary(world: Any, home: Any) -> list[dict]:
    """UI 田地行数据：每块 {idx, crop, stage_name, stage, grown, need, watered, dry_days, pest}。"""
    rows = []
    for idx, plot in enumerate(getattr(home, "garden", None) or []):
        crop = crop_of_seed(world, str(plot.get("seed_id", "") or ""))
        grown = int(plot.get("grown", 0) or 0)
        stage = int(plot.get("stage", 0) or 0)
        need = stage_len(world, crop) * 3 if crop else 0
        rows.append({
            "idx": idx, "crop": (crop["name"] if crop else "未知作物"),
            "stage": stage, "stage_name": _STAGE_NAMES[min(3, stage)],
            "grown": grown, "need": need,
            "watered": bool(plot.get("watered")),
            "dry_days": int(plot.get("dry_days", 0) or 0),
            "pest": bool(plot.get("pest")),
        })
    return rows
