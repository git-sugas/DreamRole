"""[P6] 世界模拟交易引擎（纯 Python，无 LLM/Qt/DB 依赖，可单测）。

需求来源：read.md §22 Phase B「核心计算系统尽可能都通过代码实现」。
玩家在商店买卖时**不经 LLM**：扣金币/挪物品/改库存全由本模块纯 Python 算（Req 4）。

数值范式铁律（延续 §21b）：LLM 只产静态种子（Item.base_price / Shop 货架初始库存），
动态计算（定价/声望折扣/买卖结算/补货）纯 Python。同种子可复现可单测。

公开函数：
- compute_item_price(item)            物品基准单价（base_price>0 用之，否则公式算）
- apply_reputation_factor(price, rep) 声望 -100..100 -> 价格倍率（高声望打折/低声望加价）
- buy_price / sell_price              算最终买/卖单价（含商店倍率 + 声望）
- buy(player, shop, item, rep)        买入：扣 gold / 入 inventory / 减 stock（原地改）
- sell(player, shop, item, rep)       卖出：加 gold / 出 inventory / 商店收购（回购）
- barter(player, npc, give, take, aff, stage)  以物易物：玩家给 give 换 npc 的 take（交情+价值比门控）
- restock_shop(shop, rng, level)      确定性补货：货架向 max_stock 靠拢（off=不补）
- is_stale(shop, current_tick)        是否到补货期（current_tick - last_restock >= interval）

实体鸭子类型：player 需 .gold(int)/.inventory(list[str])；shop 需是 Shop；item 需是 Item。
底层字段不变铁律：货币题材化（金币/灵石）只改显示，本引擎只认数字 PlayerState.gold。
"""
from __future__ import annotations

from src.models.world import effective_category as _eff_cat


# 品级 -> 价格倍率（与 combat_engine 的 _RARITY_MULT 区分：那是战斗数值倍率，这是经济倍率）。
RARITY_PRICE_MULT = {
    "common": 1.0, "uncommon": 1.8, "rare": 3.2, "epic": 6.0, "legendary": 11.0, "mythic": 20.0,
}

# [P14] 经济节奏难度开关（economy_pace）：拉长游戏寿命。
# 高级道具不应"随便打几个怪就能买"——紧缩/硬核档同时抬买价、压卖价、砍金币产出，
# 让传奇装备需要真攒钱（或真运气摸到实物）。standard 全 1.0；卖出系数叠在
# 商店自身 sell_price_mult（默认 0.5）之上，不是替代。
_ECONOMY_PACE = {
    #              (买入倍率, 卖出倍率, 金币产出倍率)
    "relaxed":    (0.8, 1.3, 1.3),    # 宽裕：练级向，快买快卖
    "standard":   (1.0, 1.0, 1.0),    # 标准：不改变既有手感
    "hard":       (1.4, 0.7, 0.7),    # 紧缩：高阶货要攒一阵
    "hardcore":   (2.0, 0.5, 0.5),    # 硬核：买价翻倍，卖价/金币产出腰斩
}
ECONOMY_PACE_VALUES = tuple(_ECONOMY_PACE.keys())


def pace_factors(pace: str = "standard") -> tuple:
    """读节奏三系数（买/卖/金币产出）；非法值回退 standard。"""
    return _ECONOMY_PACE.get(str(pace or "standard"), _ECONOMY_PACE["standard"])


def gold_gain_mult(pace: str = "standard") -> float:
    """金币产出倍率（任务奖励/奇遇金币等入钱口统一乘）。"""
    return pace_factors(pace)[2]


def world_pace(world) -> str:
    """读 world.config_overlay 的经济节奏（build 时从 preset 写入 economy_pace 键）。"""
    return (getattr(world, "config_overlay", None) or {}).get("economy_pace", "standard")
# 补货幅度档位（每条货架最多补多少）。
_RESTOCK_AMT = {"off": 0, "light": 1, "medium": 2, "heavy": 4}


def compute_item_price(item) -> int:
    """物品基准单价。base_price>0 直接用；否则按品级 + 战斗数值公式算。

    material/key/无数值消耗品给廉价基准；武器/防具按 (攻+防+回血+Σ属性加成) * 品级倍率 * 10。
    [P34a] 扩展：(1) 物品 level 每级 +20% base（高 level 物品更贵）；(2) affixes mods 求和
    计入 power（鉴定/洗练产出的词条影响价值）；(3) 未鉴定装备（identified=False）价格不折
    买入价（商店按品级标价），但卖出价大幅折扣（赌徒经济 sink，见 sell_price）。
    [!] 本函数返回的是「无折扣基准价」；未鉴定折扣只在 sell_price 路径应用，避免 buy 路径
    被折扣后玩家低买高卖套利（买入按品级、卖出按折扣，赌徒只能赌鉴定后自用或亏本卖）。
    """
    bp = int(getattr(item, "base_price", 0) or 0)
    if bp > 0:
        # [P34a] base_price 也叠加 level 倍率（高 level 定价物品更贵），但不叠 affix
        # （base_price 是 LLM/生成处给的种子价，已含其设计意图，不二次叠加词条）。
        lvl = max(0, int(getattr(item, "level", 0) or 0))
        if lvl > 0:
            return max(1, int(bp * (1.0 + 0.2 * lvl)))
        return bp
    mult = RARITY_PRICE_MULT.get(getattr(item, "rarity", "common") or "common", 1.0)
    atk = int(getattr(item, "attack", 0) or 0)
    dfn = int(getattr(item, "defense", 0) or 0)
    # [百分比回血 2026-09-01] heal_pct 优先换算等效绝对值（250 基准血，与归一化同锚）
    _pct = int(getattr(item, "heal_pct", 0) or 0)
    heal = int(_pct * 250 / 100) if _pct > 0 else int(getattr(item, "heal_amount", 0) or 0)
    sb = getattr(item, "stat_bonus", None)
    bonus = 0
    if isinstance(sb, dict):
        for v in sb.values():
            try:
                bonus += int(v)
            except (TypeError, ValueError):
                continue
    # [P34a] affixes mods 求和计入 power（兼容两种 affix 结构：顶层 {atk:3} 与嵌套 {mods:{atk:3}}）
    # [C5 修复 2026-08-25] 补读 affix stat_bonus 维度（与 combat_engine._affix_val 同口径——
    # 战斗侧 str 词条生效而价格侧不认会让洗练 stat 词条白洗）
    aff_bonus = 0
    affs = getattr(item, "affixes", None)
    if isinstance(affs, list):
        for a in affs:
            if not isinstance(a, dict):
                continue
            mods = a.get("mods") if isinstance(a.get("mods"), dict) else a
            for k in ("atk", "def", "magic_atk", "crit"):
                try:
                    aff_bonus += int(mods.get(k, 0) or 0)
                except (TypeError, ValueError):
                    continue
            sb_m = mods.get("stat_bonus")
            if isinstance(sb_m, dict):
                for v in sb_m.values():
                    try:
                        aff_bonus += int(v)
                    except (TypeError, ValueError):
                        continue
    power = atk + dfn + heal + bonus + aff_bonus
    if power <= 0:
        # [修 2026-08-28] 无数值物品按品级阶梯定价（原 mult*8+3 廉价基准在高品级上严重
        # 倒挂：epic 材料卖 51、legendary 卖 91——材料/芯片类无数值但品级本身代表稀有度，
        # 曲线 = RARITY_PRICE_MULT x 30 基准，与 _clamp_item_stats 的护栏锚同口径）。
        base = int(mult * 30)
    else:
        base = int(power * mult * 10) + 5
    # [P34a] level 倍率：每级 +20% base
    lvl = max(0, int(getattr(item, "level", 0) or 0))
    if lvl > 0:
        base = int(base * (1.0 + 0.2 * lvl))
    return max(1, base)


def _unidentified_sell_discount(item) -> float:
    """[P34a] 未鉴定装备卖出折扣系数。identified=False 的装备卖出价 x0.25（赌徒 sink）。

    非装备（material/key/cultivate reagent）不受影响（恒返回 1.0）——只有 weapon/armor/
    accessory 才有鉴定态意义。商店买入未鉴定装备按品级正常价（不折），玩家卖出才折，
    形成「赌徒只能赌鉴定后自用或亏本卖」的经济闭环。
    """
    if getattr(item, "identified", True) is False:
        t = getattr(item, "type", "") or ""
        if t in ("weapon", "armor", "accessory"):
            return 0.25
    return 1.0


def _rep_factor(reputation) -> float:
    """声望 -100..100 -> 价格倍率。>=0 每 100 点打 30% 折；<0 每 100 点加 20% 价。"""
    try:
        rep = int(reputation or 0)
    except (TypeError, ValueError):
        rep = 0
    rep = max(-100, min(100, rep))
    if rep >= 0:
        return 1.0 - 0.30 * (rep / 100.0)
    return 1.0 + 0.20 * (-rep / 100.0)


def apply_reputation_factor(price: int, reputation) -> int:
    """对单价施加声望倍率（买入/卖出通用）。

    [!] 输入 0 保持 0：sell_price_mult=0.0（商店不收购）的合法语义不得被 max(1,·)
    抬成 1（照卖会白拿物品 0-1 金入账）。"""
    if int(price) <= 0:
        return 0
    return max(1, int(round(int(price) * _rep_factor(reputation))))


def shop_open(world, shop) -> bool:
    """[P39c] 商店营业门控：仅 night 打烊（auction 交易所 24h 例外，[定稿]；
    [2026-09-10 用户指示] 清晨/白昼/黄昏均营业，只有夜晚算打烊）。

    打烊不拒客：玩家可敲门强买，走 buy 的 night_markup 溢价（夜间急用药是真实需求，
    防门控变烦人）。"""
    if str(getattr(shop, "shop_type", "") or "") == "auction":
        return True
    return str(getattr(world, "time_phase", "day") or "day") != "night"


NIGHT_MARKUP = 1.5   # [P39c] 打烊时段敲门溢价（乘在声望倍率后）

# [P45(1)] 势力治下价格系数：归属势力敌视玩家 -> 买入溢价 + 卖出压价（纯引擎确定性；
# 玩家提声望可翻盘，夺城易主后物价立变——因果可见性）。阈值与 P39a 社交负值拒交易同口径。
HOSTILE_REP_BUY_MARKUP = 1.5      # 声望 < -50（仇视）买入 1.5x
UNFRIENDLY_REP_BUY_MARKUP = 1.3   # -50 <= 声望 < -25（敌对）买入 1.3x
HOSTILE_SELL_FACTOR = 0.7         # 声望 < -25 卖出压价 0.7x


PRODUCTION_BUY_FACTOR = 0.8     # [产地系数] 产地直供买入折扣
SCARCITY_SELL_FACTOR = 1.8     # [产地系数] 异地稀缺卖出溢价（乘在 sell_price_mult 上，
                               # 0.5 x 1.8 = 0.9 有效卖价 > 0.8 产地买价 -> 跑商有利可图）


_PROD_CACHE: dict = {}   # {(world.id, loc.id, tick): (ids, cats)} 逐回合缓存


def _produced_sets(world, loc):
    """地点产出集合：(item_id 集, effective_category 集)——node drops 直接映射。

    [审查修复] 按 (world.id, loc.id, tick_count) 缓存：settle 货架注入逐 entry 调用、
    ShopDialog 刷卡逐卡调用，原每调用全表扫描（大世界每回合上下文显著变慢）；节点
    drops 只在地图拓展时变（tick 推进兜底失效）。
    """
    key = (str(getattr(world, "id", "")), str(getattr(loc, "id", "")),
           int(getattr(world, "tick_count", 0) or 0))
    hit = _PROD_CACHE.get(key)
    if hit is not None:
        return hit
    ids, cats = set(), set()
    item_by_id = {getattr(x, "id", ""): x for x in (getattr(world, "items", None) or [])}
    for node in (getattr(loc, "resource_nodes", None) or []):
        for d in (getattr(node, "drops", None) or []):
            if not (isinstance(d, dict) and d.get("item_id")):
                continue
            iid = str(d["item_id"])
            ids.add(iid)
            it = item_by_id.get(iid)
            if it is not None:
                cats.add(_eff_cat(it))
    if len(_PROD_CACHE) > 512:
        _PROD_CACHE.clear()
    _PROD_CACHE[key] = (ids, cats)
    return ids, cats


def production_price_factors(world, shop, item) -> "tuple[float, float, str]":
    """[产地系数 2026-08-29] 跑商价差来源（纯函数；与治下/夜间因子乘法叠加）：

    - item 产自本店地点（node drops 同 id 或同类别）-> 买入 x0.8（产地直供）。
    - item 在世界任一地点可产但不产自本地 -> 卖出 x1.8（异地稀缺，玩家把货运到
      不产此地能卖更高——价差 = 跑商利润；本地也产时卖出 1.0，原地倒卖必亏）。
    - 无处可产的普通货 -> (1.0, 1.0, "")。
    返回 (买入markup, 卖出factor, 标签)。
    """
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if getattr(l, "id", "") == getattr(shop, "location_id", "")), None)
    if loc is None or item is None:
        return 1.0, 1.0, ""
    here_ids, here_cats = _produced_sets(world, loc)
    iid = str(getattr(item, "id", "") or "")
    icat = _eff_cat(item)
    produced_here = iid in here_ids or (icat and icat in here_cats)
    if produced_here:
        return PRODUCTION_BUY_FACTOR, 1.0, "产地直供"
    for other in (getattr(world, "locations", None) or []):
        o_ids, o_cats = _produced_sets(world, other)
        if iid in o_ids or (icat and icat in o_cats):
            return 1.0, SCARCITY_SELL_FACTOR, "异地稀缺"
    return 1.0, 1.0, ""


def territory_price_factors(world, shop, player) -> "tuple[float, float, str]":
    """[P45(1)] 商店所在地点的势力治下价格系数：返回 (买入markup, 卖出factor, 档位说明)。

    归属势力（Location.owner_faction_id 回退 faction_id，与势力战夺城同一真相源）对
    玩家声望 <= -25 判敌对：买入溢价（敌对 1.3x / 仇视 1.5x）+ 卖出压价 0.7x。
    无归属势力 / 非敌对返回 (1.0, 1.0, "")。与 P39c 夜间敲门 markup 乘法叠加。
    """
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if getattr(l, "id", "") == getattr(shop, "location_id", "")), None)
    owner = (getattr(loc, "owner_faction_id", "") or
             getattr(loc, "faction_id", "") or "")
    if not owner or player is None:
        return 1.0, 1.0, ""
    rep = int((getattr(player, "reputation", None) or {}).get(owner, 0) or 0)
    if rep < -50:
        return HOSTILE_REP_BUY_MARKUP, HOSTILE_SELL_FACTOR, "仇视"
    if rep <= -25:   # <= -25 判敌对（与 P39a 社交负值阈值同口径，含 -25 本值）
        return UNFRIENDLY_REP_BUY_MARKUP, HOSTILE_SELL_FACTOR, "敌对"
    return 1.0, 1.0, ""


# ---------------------------------------------------------------------------
# [物价联动 2026-09-10 用户指示] 题材季节货架 + 本地势力财富档位。
# 购买时刻动态乘区（不写 entry.price，漂移锚公式价机制不动）；零 rng 消费（纯状态
# 读取，不碰任何 SeededRng 序列）；两开关各自 overlay 门控（build 写入，缺键=关护老
# 世界）。单因子各自钳顶、不做总乘区钳顶（用户拍板：极端叠加贵得都有名有姓）。
# ---------------------------------------------------------------------------

# 势力 wealth（0-100）七档 -> (buy_mult, sell_mult)：穷 -> 买贵卖压（物资紧缺囤货
# 居奇），富 -> 买贱卖抬（货源充足让利）。钳 0.85-1.20。
_WEALTH_BANDS = (
    (10, 1.20, 0.85),    # 破产边缘
    (30, 1.10, 0.92),    # 拮据
    (45, 1.05, 0.97),    # 紧巴
    (60, 1.00, 1.00),    # 常态（无 tag）
    (75, 0.97, 1.03),    # 殷实
    (90, 0.94, 1.06),    # 富庶
    (101, 0.90, 1.10),   # 豪富
)

# 题材季节货架系数（6 题材 x 4 季：品类 mults + 行情短标签；缺键回退西幻，守 §23）。
# 品类 key = Item.type（weapon/armor/consumable/material/accessory；key/未知 = 1.0）。
# 单因子钳 0.80-1.25。
_GENRE_SEASON_SHELF = {
    "western_fantasy": {
        0: {"mults": {"consumable": 0.95, "material": 0.95}, "tag": "春日货贱"},
        1: {"mults": {"consumable": 1.05}, "tag": "盛夏药贵"},
        2: {"mults": {"material": 0.95, "accessory": 1.05}, "tag": "秋获丰盈"},
        3: {"mults": {"consumable": 1.15, "material": 1.10, "weapon": 1.05}, "tag": "寒冬物价扬"},
    },
    "xianxia": {
        0: {"mults": {"consumable": 0.95, "material": 0.95}, "tag": "春回百草贱"},
        1: {"mults": {"consumable": 1.05}, "tag": "暑气丹贵"},
        2: {"mults": {"material": 0.95, "weapon": 1.05}, "tag": "秋演兵刃热"},
        3: {"mults": {"consumable": 1.15, "material": 1.10}, "tag": "冬寒进补贵"},
    },
    "wuxia": {
        0: {"mults": {"material": 0.95}, "tag": "春汛货通"},
        1: {"mults": {"consumable": 1.05}, "tag": "酷暑药贵"},
        2: {"mults": {"material": 0.95, "weapon": 1.05}, "tag": "秋操兵贵"},
        3: {"mults": {"consumable": 1.15, "material": 1.10, "weapon": 1.05}, "tag": "风雪价扬"},
    },
    "modern": {
        0: {"mults": {}, "tag": "春季价稳"},
        1: {"mults": {"consumable": 1.05}, "tag": "高温季涨价"},
        2: {"mults": {"material": 0.95}, "tag": "补货季走低"},
        3: {"mults": {"consumable": 1.10, "material": 1.05}, "tag": "寒冬物资涨"},
    },
    "scifi": {
        0: {"mults": {"material": 0.95}, "tag": "物流恢复价低"},
        1: {"mults": {"consumable": 1.05}, "tag": "能耗季涨"},
        2: {"mults": {"material": 0.95, "weapon": 1.05}, "tag": "招标季军需热"},
        3: {"mults": {"consumable": 1.10, "material": 1.10}, "tag": "低温季物资涨"},
    },
    "apocalypse": {
        0: {"mults": {"material": 0.95, "consumable": 0.95}, "tag": "回暖物资回流"},
        1: {"mults": {"weapon": 1.10, "consumable": 1.05}, "tag": "变异体活跃"},
        2: {"mults": {"material": 1.05}, "tag": "入秋囤冬"},
        3: {"mults": {"consumable": 1.25, "material": 1.15, "weapon": 1.10, "armor": 1.05}, "tag": "严寒物资暴涨"},
    },
}

# 势力财富档位行情短语（poor/rich 各 3 变体 x 6 题材，按 day_count 轮换防复读；
# 缺键回退西幻）。poor/rich 方向由档位决定（买入倍率 >1 为 poor）。
_GENRE_WEALTH_TAGS = {
    "western_fantasy": {"poor": ["王国库空虚·物价上浮", "领主征税·货物紧缺", "战费拖累·商路萧条"],
                        "rich": ["王库充盈·让利揽客", "商路鼎盛·货源充沛", "领主富庶·平价惠民"]},
    "xianxia": {"poor": ["门库空虚·灵物价高", "宗门征敛·坊市紧缺", "灵石耗竭·药价上浮"],
                "rich": ["灵矿丰收·丹药让利", "宗门富庶·平价施药", "坊市兴旺·货源充足"]},
    "wuxia": {"poor": ["镖局亏空·货价上浮", "帮派征粮·物资紧缺", "赋税沉重·物价高涨"],
              "rich": ["银库充盈·让利乡邻", "商号鼎盛·货源充足", "帮派富庶·平价惠民"]},
    "modern": {"poor": ["企业亏损·物价上浮", "财政紧缩·供应紧缺", "资金链紧张·进货价高"],
               "rich": ["现金流充裕·促销让利", "供应链稳定·货源充足", "财大气粗·平价销售"]},
    "scifi": {"poor": ["联邦财政告急·物价上浮", "配额收紧·物资紧缺", "舰队军费拖累·货价高涨"],
              "rich": ["联邦金库充盈·让利", "产能全开·货源充足", "财团富庶·平价供应"]},
    "apocalypse": {"poor": ["据点崩坏·物资管控", "补给线断绝·物价暴涨", "幸存者搜刮殆尽·有价无市"],
                   "rich": ["据点稳固·敞开供应", "物资充裕·平价配给", "缴获丰厚·让利幸存者"]},
}


def _overlay_flag(world, key: str) -> bool:
    """overlay 布尔开关（缺键=关——老世界不受新机制影响，build 时才写入）。"""
    ov = getattr(world, "config_overlay", None)
    return bool(ov.get(key, False)) if isinstance(ov, dict) else False


def _shelf_genre(world) -> str:
    """世界题材 id（config_overlay.attribute_template_id；缺键/自定义回退 western_fantasy）。"""
    ov = getattr(world, "config_overlay", None)
    gid = str(ov.get("attribute_template_id", "") or "") if isinstance(ov, dict) else ""
    return gid or "western_fantasy"


def wealth_price_factors(world, shop) -> "tuple[float, float, str]":
    """[势力财富定价 2026-09-10 用户指示] 本地势力财富 -> (买入markup, 卖出factor, 行情标签)。

    商店所在地点 owner_faction_id 治下势力的 wealth（0-100；tick_economy 每 tick 漂移
    + 任务领奖/委托交付/势力战结算喂它）分七档：穷 -> 买贵卖压（物资紧缺囤货居奇），
    富 -> 买贱卖抬（货源充足让利）。无主地点/玩家据点/查无势力 = (1.0, 1.0, "")。
    overlay "wealth_price" 缺键=关（护老世界；build 从 preset.faction_price_enabled 写入）。
    纯状态读取零 rng；行情标签按 day_count 轮换防复读。
    """
    if not _overlay_flag(world, "wealth_price"):
        return 1.0, 1.0, ""
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if getattr(l, "id", "") == getattr(shop, "location_id", "")), None)
    owner = (getattr(loc, "owner_faction_id", "") or
             getattr(loc, "faction_id", "") or "") if loc is not None else ""
    if not owner:
        return 1.0, 1.0, ""
    fac = next((f for f in (getattr(world, "factions", None) or [])
                if getattr(f, "id", "") == owner), None)
    if fac is None:
        return 1.0, 1.0, ""
    # [!] 防 0 吞（守不变量区 _gi 口径）：wealth=0 是合法值（破产边缘最穷档），
    # `x or 50` 会把 0 当缺省翻成 50——None 才是缺省（沿用 hunger 同款 None 感知）。
    raw_w = getattr(fac, "wealth", None)
    w = max(0, min(100, int(raw_w if raw_w is not None else 50)))
    for cap, buy_m, sell_f in _WEALTH_BANDS:
        if w <= cap:
            if buy_m == 1.0 and sell_f == 1.0:
                return 1.0, 1.0, ""
            pools = _GENRE_WEALTH_TAGS.get(_shelf_genre(world)) \
                or _GENRE_WEALTH_TAGS["western_fantasy"]
            pool = pools.get("poor" if buy_m > 1.0 else "rich") \
                or _GENRE_WEALTH_TAGS["western_fantasy"]["poor"]
            day = max(1, int(getattr(world, "day_count", 1) or 1))
            return buy_m, sell_f, pool[(day - 1) % len(pool)]
    return 1.0, 1.0, ""


def shelf_season_factors(world, item) -> "tuple[float, str]":
    """[物价联动 2026-09-10 用户指示] 题材季节 -> 该物品品类货架系数 (mult, 标签)。

    挂季节玩法同一 overlay 门控（season_fx 缺键=关，老世界不受影响）；按世界题材
    （attribute_template_id，缺键回退西幻）取 _GENRE_SEASON_SHELF 当前季的品类系数
    （Item.type 查表；key/未知品类 = 1.0 无标签），钳 0.80-1.25。纯推导零 rng。
    """
    from src.services import calendar_engine as _cale
    if not _cale.season_fx(world).get("hint"):
        return 1.0, ""
    rows = _GENRE_SEASON_SHELF.get(_shelf_genre(world)) \
        or _GENRE_SEASON_SHELF["western_fantasy"]
    idx = max(0, min(3, int(_cale.calendar(world).get("season_idx", 0) or 0)))
    row = rows.get(idx) or rows[0]
    raw = (row.get("mults") or {}).get(str(getattr(item, "type", "") or ""))
    if not raw or raw == 1.0:
        return 1.0, ""
    return max(0.80, min(1.25, float(raw))), (row.get("tag", "") or "")


def market_price_factors(world, shop, item) -> "tuple[float, float, list]":
    """[物价联动 2026-09-10] 季节 x 财富合成购买时刻动态乘区：返回 (buy, sell, tags)。

    四个动态接线点的单一入口（ShopDialog 卡片/买卖、_resolve_trade、settle【交易】
    块）：调用方把 buy 乘进 markup 链、sell 乘进 sell_factor、tags 拼进行尾/叙事
    hint。NPC 采购路径（A2 买药/D5 来访/据点掌柜）不调本函数——恒挂牌价口径不变。
    """
    bw, sw, wtag = wealth_price_factors(world, shop)
    bs, stag = shelf_season_factors(world, item)   # 季节系数买卖同镜像
    # [G03/R3 2026-09-30] 地区压力/纾解乘区（Boss 在窗短缺、击败/交付/通关纾解；
    # 有期限有来源，只读时相乘不写回基础价；单因子钳 0.80-1.30）
    from src.services import region_pressure as _rp
    br, sr, rtag = _rp.region_price_factors(world, str(getattr(shop, "location_id", "") or ""))
    tags = [t for t in (wtag, stag, rtag) if t]
    return bw * bs * br, sw * bs * sr, tags


def stability_supply_level(stability: int) -> int:
    """[P45(1)] 地点稳定度 -> 补给档位：>=50 正常（发 2 件）/ 25-49 紧张（1 件）/
    <25 短缺（0 件 + 系统货架衰减）。稳定度由势力战夺城压低、滴答向 50 回归。"""
    s = int(stability or 0)
    if s >= 50:
        return 2
    if s >= 25:
        return 1
    return 0



def buy_price(entry, item, shop, reputation=0, pace: str = "standard",
              markup: float = 1.0, merchant_trust: int = 0) -> int:
    """玩家买入单价 = (货架固定价 or 物品基准价) * 商店买入倍率 * 节奏倍率 * 声望倍率
    * markup（[P39c] 夜间敲门溢价 1.5，默认 1.0 不影响既有口径）。
    [玩家印象 2026-09-06] merchant_trust = 商人对玩家的印象信任度（-100~100）：
    >=50 相熟信任 0.95 折；<=-50 忌惮抬价 1.1（同一玩家在不同商人眼里价不一样）。"""
    base = int(getattr(entry, "price", 0) or 0)
    if base <= 0:
        base = compute_item_price(item)
    marked = int(round(base * float(getattr(shop, "buy_price_mult", 1.0) or 1.0)
                       * pace_factors(pace)[0]))
    priced = apply_reputation_factor(marked, reputation)
    if float(markup or 1.0) != 1.0:
        priced = max(1, int(round(priced * float(markup or 1.0))))
    if int(merchant_trust or 0) >= 50:
        priced = max(1, int(round(priced * 0.95)))
    elif int(merchant_trust or 0) <= -50:
        priced = max(1, int(round(priced * 1.1)))
    return priced


def sell_price(shop, item, reputation=0, pace: str = "standard",
               sell_factor: float = 1.0) -> int:
    """玩家卖出单价 = 物品基准价 * 商店卖出倍率（默认 0.5 打折）* 节奏倍率 * 声望倍率
    * sell_factor（[P45(1)] 敌对治下压价 0.7，默认 1.0 不影响既有口径）。

    卖出走 item 基准价（不走货架固定价），保证不同商店收购同一物品基准一致。
    [P34a] 未鉴定装备（identified=False）卖出价再乘 0.25 折扣（赌徒经济 sink）——商店按品级
    收但压价收未鉴定赌货，玩家要么鉴定后自用/卖出，要么亏本卖未鉴定。
    """
    base = compute_item_price(item)
    # [!] 显式 None 判断（不可 `or 0.5`）：sell_price_mult=0.0 是合法值（商店不收购），
    # or 会吞成 0.5 让玩家凭空得钱（守 §11 防吞 0）。
    mult = getattr(shop, "sell_price_mult", 0.5)
    mult = 0.5 if mult is None else float(mult)
    val = int(round(base * mult * pace_factors(pace)[1]))
    val = apply_reputation_factor(val, reputation)
    if float(sell_factor or 1.0) != 1.0:
        val = max(0, int(round(val * float(sell_factor or 1.0))))
    # [P34a] 未鉴定装备卖出折扣
    val = int(round(val * _unidentified_sell_discount(item)))
    return max(0, val)


def buy(player, shop, item, reputation=0, pace: str = "standard", merchant=None,
        markup: float = 1.0) -> dict:
    # [玩家印象] merchant 的印象信任直读（buy_price 折/溢）
    """买入：扣 gold / 入 inventory / 减 stock。原地改 player 与 shop 货架，返回结算 dict。
    [P36b] merchant 传入且条目 tag=="npc_made"（商人亲手制的货）时，货款进商人钱包
    （NPC 经济内循环：卖货赚钱再投资）。
    [P39c] markup：打烊时段敲门溢价（trade_engine.NIGHT_MARKUP），货款同额入账。"""
    entry = shop.find_entry(getattr(item, "id", ""))
    if entry is None:
        return {"ok": False, "reason": "该商店不出售此物品"}
    if entry.stock == 0:
        return {"ok": False, "reason": "已售罄", "price": 0}
    _imp = getattr(merchant, "player_impression", None) if merchant is not None else None
    _trust = int(_imp.get("trust", 0) or 0) if isinstance(_imp, dict) else 0
    price = buy_price(entry, item, shop, reputation, pace, markup, merchant_trust=_trust)
    if int(getattr(player, "gold", 0) or 0) < price:
        return {"ok": False, "reason": "余额不足", "price": price}
    # [!] 买入也查负重上限（统一入包口径）：背包满时拒绝购买，不扣钱
    from src.services.combat_engine import try_add_to_inventory
    if not try_add_to_inventory(player, str(getattr(item, "id", ""))):
        return {"ok": False, "reason": "背包已满", "price": price}
    player.gold = int(player.gold) - price
    if entry.stock != -1:
        entry.stock = max(0, int(entry.stock) - 1)
    if getattr(entry, "tag", "") == "npc_made" and merchant is not None:
        try:  # best-effort：商人钱包入账失败不影响交易
            merchant.wallet = int(getattr(merchant, "wallet", 0) or 0) + price
        except Exception:
            pass
    return {
        "ok": True, "action": "buy", "item_id": item.id, "item_name": getattr(item, "name", ""),
        "price": price, "gold_after": player.gold, "stock_after": entry.stock,
    }


def sell(player, shop, item, reputation=0, pace: str = "standard",
         sell_factor: float = 1.0) -> dict:
    """卖出：加 gold / 出 inventory / 商店收购（已有货架 +1，否则建回购条目）。

    key 类道具不可售（任务道具）。回购条目 max_stock=1，玩家可原价附近买回（买入倍率加价）。
    [P45(1)] sell_factor：敌对治下压价（territory_price_factors），货款同额入账。
    """
    if getattr(item, "type", "") == "key":
        return {"ok": False, "reason": "关键道具无法出售"}
    inv = list(getattr(player, "inventory", []) or [])
    if item.id not in inv:
        return {"ok": False, "reason": "背包中没有此物品"}
    price = sell_price(shop, item, reputation, pace, sell_factor)
    # [!] 0 收购商店拒收（sell_price_mult=0.0 或压价到 0）：照卖会「物品没了 0 金入账」
    if price <= 0:
        return {"ok": False, "reason": "此店不收购该物品", "price": 0}
    inv.remove(item.id)
    player.inventory = inv
    player.gold = int(getattr(player, "gold", 0) or 0) + price
    # 商店收购：已有条目 +1（不超 max_stock），否则建回购条目
    entry = shop.find_entry(item.id)
    if entry is None:
        from src.models.shop import ShopStockEntry
        bp = compute_item_price(item)
        shop.stock.append(ShopStockEntry(item_id=item.id, price=bp, stock=1, max_stock=1))
    elif entry.stock != -1 and entry.max_stock != -1:
        cap = entry.max_stock if entry.max_stock > 0 else entry.stock + 1
        entry.stock = min(cap, int(entry.stock) + 1)
    return {
        "ok": True, "action": "sell", "item_id": item.id, "item_name": getattr(item, "name", ""),
        "price": price, "gold_after": player.gold,
    }


# [P28] 以物易物门控 ----------------------------------------------------------
# barter 不走金币/货架，是玩家与任意 NPC 的物品对换。经济性靠两层门控：
# (1) 交情门槛——纯陌生防骗拒（点头之交以下不肯换随身之物）；
# (2) 价值比——交情越低越像正经商人（要赚），至亲愿吃亏。
# 高交情折扣需先投资关系（谈话/送礼/共战），farming 被关系成本门控。
BARTER_AFFINITY_MIN = 30  # 点头之交以上才肯换（纯陌生防骗拒）


def barter_value_threshold(affinity: int, stage: str = "") -> float:
    """玩家给出价值 / NPC 给出价值 须 >= 此阈值。交情越低越像正经商人（要赚）。

    返回 <1 表示玩家可以给得比对方少（至亲/挚友愿吃亏）；>1 表示要给得更多。
    """
    if stage in ("spouse", "sweetheart", "sworn"):
        return 0.5  # 至亲愿吃亏
    aff = max(0, min(100, int(affinity or 0)))
    if aff >= 80:
        return 0.7  # 挚友
    if aff >= 60:
        return 0.9  # 朋友（近平价）
    if aff >= 40:
        return 1.1  # 相熟（微赚）
    return 1.3  # 点头之交（赚 30%）


def barter_willingness(player_item, npc_item, affinity=0, stage: str = "") -> tuple:
    """NPC 是否愿换：(bool, reason)。交情门槛 + 价值比双层门控。"""
    if int(affinity or 0) < BARTER_AFFINITY_MIN:
        return False, "对方与你交情尚浅，不愿贸然交换随身之物"
    pv = compute_item_price(player_item)
    nv = compute_item_price(npc_item)
    if pv < max(1, nv) * barter_value_threshold(int(affinity or 0), stage):
        return False, "对方觉得这笔交换不划算，摇头不肯"
    return True, ""


def barter(player, npc, give_item, take_item, affinity=0, stage: str = "") -> dict:
    """以物易物：玩家给 give_item 换 npc 的 take_item。原地改双方 inventory。

    key 类道具不可换（双向）；归属校验（give 在 player 背包 / take 在 npc 背包）；
    交情+价值比门控。交换为去重 remove/append（与 sell 的 list.remove 口径一致，
    净 -1+1=0 无需负重校验）。返回结算 dict（仿 buy/sell 形状）。
    """
    # key 守卫（双向）
    if getattr(give_item, "type", "") == "key":
        return {"ok": False, "reason": "关键道具无法交易"}
    if getattr(take_item, "type", "") == "key":
        return {"ok": False, "reason": "关键道具无法交易"}
    give_id = str(getattr(give_item, "id", "") or "")
    take_id = str(getattr(take_item, "id", "") or "")
    if not give_id or not take_id:
        return {"ok": False, "reason": "物品无效"}
    # 归属校验
    p_inv = list(getattr(player, "inventory", []) or [])
    n_inv = list(getattr(npc, "inventory", []) or [])
    if give_id not in p_inv:
        return {"ok": False, "reason": "你的背包中没有此物品"}
    if take_id not in n_inv:
        return {"ok": False, "reason": "对方没有此物品"}
    # 交情 + 价值比门控
    ok, reason = barter_willingness(give_item, take_item, affinity, stage)
    if not ok:
        return {"ok": False, "reason": reason}
    # 交换（去重 remove/append；净 -1+1=0 无需负重校验）
    p_inv.remove(give_id)
    n_inv.remove(take_id)
    if take_id not in p_inv:
        p_inv.append(take_id)
    if give_id not in n_inv:
        n_inv.append(give_id)
    player.inventory = p_inv
    npc.inventory = n_inv
    give_val = compute_item_price(give_item)
    take_val = compute_item_price(take_item)
    return {
        "ok": True, "action": "barter",
        "give_item_id": give_id, "give_item_name": getattr(give_item, "name", ""),
        "take_item_id": take_id, "take_item_name": getattr(take_item, "name", ""),
        "give_value": give_val, "take_value": take_val,
    }


def restock_shop(shop, rng, level: str = "medium") -> int:
    """确定性补货：货架向 max_stock 靠拢。返回总补货数（off=0 即不变）。

    无限库存(stock=-1/max_stock=-1)跳过；rng.roll 决定每条补多少（同 rng 同结果，可单测）。
    """
    amt = _RESTOCK_AMT.get(level, 2)
    if amt <= 0:
        return 0
    total = 0
    for e in shop.stock:
        if getattr(e, "tag", ""):
            continue               # [P36b] npc_made 标记条目不走系统补货
        if e.stock == -1 or e.max_stock == -1:
            continue
        if e.max_stock <= 0 or e.stock >= e.max_stock:
            continue
        refill = int(rng.roll(max(1, amt - 1), amt + 1)) if hasattr(rng, "roll") else amt
        new_stock = min(e.max_stock, e.stock + max(1, refill))
        total += new_stock - e.stock
        e.stock = new_stock
    return total


def is_stale(shop, current_tick: int) -> bool:
    """是否到补货期：current_tick - last_restock_tick >= restock_interval。"""
    try:
        cur = int(current_tick)
    except (TypeError, ValueError):
        cur = 0
    return (cur - int(getattr(shop, "last_restock_tick", 0) or 0)) >= int(getattr(shop, "restock_interval", 1) or 1)


# [价格漂移] 幅度档 -> 浮动百分比（基准价 × [1-pct, 1+pct]）。
_DRIFT_PCT = {"off": 0.0, "light": 0.05, "medium": 0.10, "heavy": 0.15}


def drift_prices(shop, base_price_fn, world_id: str, tick: int, level: str = "medium") -> int:
    """[价格漂移] 货架价格随 tick 确定性浮动，模拟市场供需波动。

    旧版货架 price 由 LLM/目录写死后永不变（补货只补数量），长线经济僵死。
    每补货周期对每条 entry 按 (world_id, tick, shop_id, item_id) 派生 SeededRng，
    在基准价 ±pct 区间浮动后写回 entry.price。
    - 基准价：**一律锚 base_price_fn(item) 公式价**（原文写「entry.price>0 用之」与实现相反，
      2026-09-10 纠偏）。锚公式价而非 entry.price 是为了防复利随机游走——LLM 种子价仅在
      首周期被覆盖，之后 entry.price = 公式价 x 浮动。
    - 新 salt shopdrift_{shop.id}_{item_id}，不复用补货 rng（破坏补货确定性序列）。
    - 确定性：同 world+tick+shop+item 同结果，可回放。
    返回被漂移的条目数（off 档返回 0）。
    """
    pct = _DRIFT_PCT.get(str(level or "medium"), 0.10)
    if pct <= 0:
        return 0
    from src.utils.rng import SeededRng
    changed = 0
    for e in shop.stock:
        # [P36b] NPC 手制货（tag="npc_made"）由商人定价（compute_item_price 定价权归 NPC），
        # 系统漂移不得覆写——一律跳过非空 tag 条目。
        if getattr(e, "tag", ""):
            continue
        # [!] 锚定公式价（base_price_fn 算）而非 entry.price，防复利随机游走：
        # 若锚=entry.price，每周期 ±pct 叠乘是无均值回归的漂移，长局几百 tick 后价格
        # 可漂到公式价数倍或零头。改为固定锚（公式价），每周期围绕它浮动，LLM 种子价
        # 仅在首周期被覆盖（之后 entry.price = 公式价 × 浮动）。
        if base_price_fn is None:
            continue
        base = int(base_price_fn(getattr(e, "item_id", "")) or 0)
        if base <= 0:
            continue
        r = SeededRng.seed_from(world_id, tick, f"shopdrift_{shop.id}_{e.item_id}")
        mult = 1.0 + (r.random() * 2 - 1) * pct  # [1-pct, 1+pct]
        e.price = max(1, int(round(base * mult)))
        changed += 1
    return changed
