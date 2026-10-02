"""[P34f] 交易所股市引擎（纯 Python + SeededRng + 1 次 LLM 定价由 world_sim_service 调）。

题材化大宗商品期货：城市交易所（auction shop，P34e 保证城市必有）内挂大宗商品股市，
玩家买卖，每日 LLM 据世界事件/经济/节日定价（±% 钳制防崩盘），失败走 SeededRng
确定性兜底（锚定 base_price 防随机游走）。

数值范式铁律：LLM 出当日新价（语义），引擎算金币结算/持仓/±% 钳制（结构）。
- buy_stock/sell_stock 纯 Python 金币结算（price*qty），持仓 holdings[symbol]+=qty。
- apply_llm_prices 解析 LLM 输出 + ±drift_pct 钳制（单日 ±30% 防崩盘）+ history push cap 30。
- drift_prices_fallback LLM 失败兜底：各 commodity price 按 base_price ±drift_pct SeededRng
  派生（锚定 base_price 防随机游走，仿 trade_engine.drift_prices 口径）。

[!] 完全独立铁律（§21）：不动单聊/群聊/会话/远程模块；仅 import 复用 World/PlayerState/
Commodity/StockMarket 模型 + SeededRng。
"""
from __future__ import annotations

from src.models.world import World, Commodity, StockMarket
from src.utils.rng import SeededRng


# [P34f] 交易所商品题材化兜底池：6 题材各 >=4 商品（abstract symbol 大写 + 题材化 name + desc）。
# symbol 是底层抽象 key（永不变，守 §23），name 走题材化显示名。缺键回退西幻。
# 每题材 >=4 + symbol 全局唯一（登记 test_genre_data_volume.py 基线）。
_GENRE_STOCK_COMMODITIES: dict[str, list[dict]] = {
    "western_fantasy": [
        {"symbol": "IRON", "name": "精铁锭", "cat": "war", "desc": "锻造兵器甲胄的基础金属，战时需求激增。"},
        {"symbol": "LEATHER", "name": "皮革", "cat": "war", "desc": "护甲与马鞍的原料，狩猎季丰收则价跌。"},
        {"symbol": "GRAIN", "name": "麦粮", "cat": "civil", "desc": "民以食为天，旱涝灾荒即飞涨。"},
        {"symbol": "SPICE", "name": "香料", "cat": "lux", "desc": "远洋贸易的暴利货物，航路断绝即稀缺。"},
        {"symbol": "GEM", "name": "宝石", "cat": "lux", "desc": "贵族追捧的奢侈品，盛世价高乱世贬值。"},
        {"symbol": "TIMBER", "name": "木材", "cat": "civil", "desc": "建筑与车船的主材，伐木禁令则价升。"},
    ],
    "xianxia": [
        {"symbol": "YAODAN", "name": "妖丹", "cat": "war", "desc": "妖兽内丹，猎妖旺季涌入市场则价跌。"},
        {"symbol": "LINGYAO", "name": "灵药", "cat": "civil", "desc": "炼丹主材，药田歉收即稀缺。"},
        {"symbol": "FULU", "name": "符箓", "cat": "war", "desc": "符纸朱砂的市价，妖魔横行时需求大增。"},
        {"symbol": "LINGCAI", "name": "灵材", "cat": "civil", "desc": "炼器炼丹通用灵材，宗门大开山门时走俏。"},
        {"symbol": "YAOMU", "name": "妖木", "cat": "lux", "desc": "万年妖木，秘境出世时涌入市场。"},
        {"symbol": "FANGSHI", "name": "坊市地契", "cat": "lux", "desc": "坊市铺面地契，商盟兴衰的晴雨表。"},
    ],
    "wuxia": [
        {"symbol": "YAOCI", "name": "药材", "cat": "civil", "desc": "江湖人疗伤续命的本钱，瘟疫时暴涨。"},
        {"symbol": "SILK", "name": "丝绸", "cat": "lux", "desc": "江南贡品，漕运通畅则价稳。"},
        {"symbol": "TEA", "name": "茶叶", "cat": "civil", "desc": "边贸硬货，茶马道断绝即缺货。"},
        {"symbol": "TIEKUANG", "name": "铁料", "cat": "war", "desc": "兵器锻造的命脉，朝廷禁铁则黑市飞涨。"},
        {"symbol": "YAN", "name": "官盐", "cat": "civil", "desc": "朝廷专营，私盐案发则官盐走俏。"},
        {"symbol": "WARHORSE", "name": "战马", "cat": "war", "desc": "边关战事一起则供不应求的军需。"},
    ],
    "modern": [
        {"symbol": "TECH", "name": "科技股", "cat": "lux", "desc": "科技巨头市值，新品发布则涨。"},
        {"symbol": "ENERGY", "name": "能源股", "cat": "war", "desc": "石油天然气，地缘冲突即飙升。"},
        {"symbol": "FOOD", "name": "粮食期货", "cat": "civil", "desc": "主粮期货，气候异常则波动。"},
        {"symbol": "METAL", "name": "有色金属", "cat": "war", "desc": "工业金属，基建潮则需求旺。"},
        {"symbol": "PHARMA", "name": "医药股", "cat": "civil", "desc": "药企市值，疫情时一枝独秀。"},
        {"symbol": "REALESTATE", "name": "地产股", "cat": "lux", "desc": "楼市冷暖，政策风向决定涨跌。"},
    ],
    "scifi": [
        {"symbol": "ALLOY", "name": "合金", "cat": "war", "desc": "星舰装甲原料，战时管制则紧张。"},
        {"symbol": "ENERGYC", "name": "能量块", "cat": "war", "desc": "通用能源，跃迁航线繁忙则价升。"},
        {"symbol": "DATA", "name": "数据晶片", "cat": "lux", "desc": "信息货币，数据风暴时稀缺。"},
        {"symbol": "RAREORE", "name": "稀有矿", "cat": "war", "desc": "外星稀有矿，矿脉发现则暴跌。"},
        {"symbol": "BIOSYN", "name": "生物合成剂", "cat": "civil", "desc": "基因改造原料，瘟疫时疯涨。"},
        {"symbol": "ANTIMATTER", "name": "反物质燃料", "cat": "lux", "desc": "曲率引擎燃料，顶级航线垄断则价高。"},
    ],
    "apocalypse": [
        {"symbol": "GRAIN_A", "name": "粮食", "cat": "civil", "desc": "末日硬通货，荒年即人命。"},
        {"symbol": "AMMO", "name": "弹药", "cat": "war", "desc": "废土保命的本钱，掠食潮则抢手。"},
        {"symbol": "MEDS", "name": "药品", "cat": "civil", "desc": "残存西药，疫病爆发即天价。"},
        {"symbol": "JINGHE", "name": "晶核", "cat": "war", "desc": "变异兽能量结晶，硬通货兼燃料。"},
        {"symbol": "SCRAP", "name": "废铁", "cat": "civil", "desc": "拾荒原料，聚落大兴土木时需求旺。"},
        {"symbol": "FUEL", "name": "燃油", "cat": "war", "desc": "发电机与车辆的命脉，油井枯竭即紧缺。"},
    ],
}

def _stock_commodity_pool(genre_id: str) -> list[dict]:
    """[P34f] 取题材股市商品兜底池（缺题材回退西幻）。返回 list 不被外部修改。"""
    return _GENRE_STOCK_COMMODITIES.get(genre_id) or _GENRE_STOCK_COMMODITIES["western_fantasy"]


def _genre_id(world: World) -> str:
    """读世界题材 id（config_overlay.attribute_template_id，兜底 western_fantasy）。"""
    ov = getattr(world, "config_overlay", None) or {}
    return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")


# [P34f] 价格历史 cap（UI mini 折线用，过长裁剪保留最近 N 条）。
_HISTORY_CAP = 30


def init_stock_market(world: World):
    """建世界时据题材池初始化 StockMarket.commodities（确定性 base_price 派生）。

    - 题材池每条 -> Commodity(symbol/name/desc)。
    - base_price 据题材池顺序确定性派生（SeededRng 锚 world.id，同 world 同结果）：
      基准 50-200 区间抖动，price=prev_price=base_price，history=[]。
    - [!] 幂等：已初始化（commodities 非空）不重置（防重复调用覆盖玩家持仓相关价格）。
    - [P41 修] 防货币重名：与题材货币同名的商品跳过（仙侠旧池「灵石」曾出现
      「用灵石买灵石」的自指；守卫兜住自定义题材的同类撞名）。
    """
    sm = getattr(world, "stock_market", None)
    if sm is None or not isinstance(sm, StockMarket):
        sm = StockMarket()
        world.stock_market = sm
    if sm.commodities:
        return  # 已初始化
    from src.models.world_sim_preset import GenreText
    currency = GenreText(getattr(world, "config_overlay", None) or {}).currency
    gid = _genre_id(world)
    pool = _stock_commodity_pool(gid)
    rng = SeededRng.seed_from(getattr(world, "id", ""), 0, "stock_init")
    for entry in pool:
        if currency and str(entry.get("name", "")) == currency:
            continue                                   # [P41] 货币重名守卫
        base = 50 + rng.roll(0, 150)  # 50-200
        sm.commodities.append(Commodity(
            symbol=str(entry.get("symbol", "")),
            name=str(entry.get("name", "")),
            desc=str(entry.get("desc", "")),
            cat=str(entry.get("cat", "") or "civil"),
            price=base, prev_price=base, base_price=base, history=[],
        ))


def buy_stock(world: World, symbol: str, qty: int) -> tuple[bool, str]:
    """买入商品：扣金币 price*qty + holdings[symbol]+=qty。

    [!] 金币不足/非法 symbol/qty<=0 拒；防负（qty 钳 >=0）。
    """
    q = int(qty)
    if q <= 0:
        return False, "数量须为正整数。"
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket):
        return False, "此地无交易所。"
    c = sm.find(symbol)
    if c is None:
        return False, "无此商品。"
    cost = int(c.price) * q
    p = world.player
    if int(p.gold) < cost:
        from src.models.world_sim_preset import GenreText as _GT
        return False, f"持有 {_GT(getattr(world, 'config_overlay', None) or {}).currency}不足（{p.gold}），需 {cost}。"
    p.gold = int(p.gold) - cost
    if not isinstance(p.stock_holdings, dict):
        p.stock_holdings = {}
    p.stock_holdings[symbol] = int(p.stock_holdings.get(symbol, 0) or 0) + q
    from src.models.world_sim_preset import GenreText as _GT
    return True, f"买入 {c.name} x{q}，花费 {cost} {_GT(getattr(world, 'config_overlay', None) or {}).currency}。"


def sell_stock(world: World, symbol: str, qty: int) -> tuple[bool, str]:
    """卖出商品：holdings[symbol]-=qty + gold += price*qty + realized_gold 累计盈亏。

    [!] holdings 校验防负（qty > 持有拒）；非法 symbol/qty<=0 拒。
    realized_gold = 累计（卖出收入 - 买入成本），但买入成本不单独追踪——这里简化为
    卖出收入直接累加进 realized_gold（作「累计卖出收入」统计，UI 显示总盈亏时配合持仓浮盈）。
    """
    q = int(qty)
    if q <= 0:
        return False, "数量须为正整数。"
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket):
        return False, "此地无交易所。"
    c = sm.find(symbol)
    if c is None:
        return False, "无此商品。"
    p = world.player
    holdings = p.stock_holdings if isinstance(p.stock_holdings, dict) else {}
    held = int(holdings.get(symbol, 0) or 0)
    if q > held:
        return False, f"持有 {held} 股，不足以卖出 {q}。"
    gain = int(c.price) * q
    p.gold = int(p.gold) + gain
    holdings[symbol] = held - q
    if holdings[symbol] <= 0:
        holdings.pop(symbol, None)
    p.stock_holdings = holdings
    p.stock_realized_gold = int(getattr(p, "stock_realized_gold", 0) or 0) + gain
    from src.models.world_sim_preset import GenreText as _GT
    return True, f"卖出 {c.name} x{q}，获得 {gain} {_GT(getattr(world, 'config_overlay', None) or {}).currency}。"


def _clamp_price(new_price: int, prev_price: int, base_price: int, drift_pct: float) -> int:
    """单日 ±drift_pct 钳制（防崩盘）：new_price 钳在 [prev*(1-d), prev*(1+d)] 内，且 >=1。

    [!] 钳制锚定 prev_price（单日波动上限），不是 base_price——base_price 是长期回归锚，
    单日波动用 prev 防止一天内暴涨暴跌。drift_pct 是 0-1 的小数（如 0.30 = ±30%）。
    """
    d = max(0.0, min(1.0, float(drift_pct or 0.0)))
    lo = max(1, int(round(prev_price * (1.0 - d))))
    hi = max(lo, int(round(prev_price * (1.0 + d))))
    p = max(1, int(new_price))
    if p < lo:
        return lo
    if p > hi:
        return hi
    return p


def apply_llm_prices(world: World, data: dict, drift_pct: float) -> bool:
    """应用 LLM 定价输出：解析 prices dict，各 commodity 新价 ±drift_pct 钳制 + history push。

    - data 形如 {"mode":"stock","prices":{"SYMBOL":新价,...}}（mode 校验由调用方做）。
    - [!] 钳制锚定 prev_price（单日 ±drift_pct）；prev_price <- 旧 price；price <- 新价。
    - history push 新 price，cap _HISTORY_CAP（保留最近 N 条）。
    - 返回是否至少更新了一个 commodity（全未命中返回 False）。
    """
    if not isinstance(data, dict):
        return False
    prices = data.get("prices")
    if not isinstance(prices, dict):
        return False
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket):
        return False
    updated = False
    for c in sm.commodities:
        if not isinstance(c, Commodity):
            continue
        nv = prices.get(c.symbol)
        if nv is None or isinstance(nv, bool):  # bool 是 int 子类，LLM 输出 true 会变 1 当新价
            continue
        try:
            nv = int(nv)
        except (TypeError, ValueError):
            continue
        new_p = _clamp_price(nv, c.price, c.base_price, drift_pct)
        c.prev_price = int(c.price)
        c.price = new_p
        c.history.append(new_p)
        if len(c.history) > _HISTORY_CAP:
            c.history = c.history[-_HISTORY_CAP:]
        updated = True
    return updated


def drift_prices_fallback(world: World, rng: SeededRng, drift_pct: float) -> bool:
    """LLM 失败兜底：各 commodity price 按 base_price ±drift_pct SeededRng 派生（防随机游走）。

    [!] 锚定 base_price（非 prev_price）——兜底是「回归基准价 + 随机波动」，防 LLM 失败时
    价格沿 prev 随机游走漂离基准（仿 trade_engine.drift_prices 锚定公式价口径）。
    """
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket):
        return False
    d = max(0.0, min(1.0, float(drift_pct or 0.0)))
    for c in sm.commodities:
        if not isinstance(c, Commodity):
            continue
        base = max(1, int(c.base_price))
        lo = max(1, int(round(base * (1.0 - d))))
        hi = max(lo, int(round(base * (1.0 + d))))
        new_p = rng.roll(lo, hi) if hi > lo else lo
        c.prev_price = int(c.price)
        c.price = new_p
        c.history.append(new_p)
        if len(c.history) > _HISTORY_CAP:
            c.history = c.history[-_HISTORY_CAP:]
    return True


# ---- [P41] 势力局势驱动定价（确定性层，叠加在 LLM 定价/兜底漂移之后） ----
# 用户指示 2026-08-22：势力战要能体现在模拟数值上——战事军需走俏、乱世奢侈品贬值、
# 战时民生短缺。纯函数读世界状态，不耗 rng 不走 LLM（守数值范式铁律）。

_PRESSURE_WINDOW = 12      # 近 12 tick（=1 个游戏日）的势力战事件窗口
_WAR_UP_PCT = 4            # 每场战事：军需类 +4%（日上限 12%）
_CIVIL_UP_PCT = 2          # 每场战事：民生类 +2%（战时征调短缺，日上限 8%）
_LUX_DOWN_PCT = 3          # 每次易主：奢侈类 -3%（+每场战事 -2%，日下限 -10%）


def faction_pressure(world: World) -> dict:
    """近窗口势力局势读数：{"battles": 战斗场次, "flips": 领土易主次数}（确定性）。

    battles 统计 category=faction_war 且 severity major/crisis 的事件；
    flips 按 desc「夺取了」（引擎 tick_faction_war 固定文案）识别领土易主。"""
    tick = int(getattr(world, "tick_count", 0) or 0)
    battles = flips = 0
    for e in (getattr(world, "event_log", None) or [])[-40:]:
        if str(getattr(e, "category", "") or "") != "faction_war":
            continue
        if tick - int(getattr(e, "tick", 0) or 0) > _PRESSURE_WINDOW:
            continue
        if str(getattr(e, "severity", "") or "") in ("major", "crisis"):
            battles += 1
            if "夺取了" in str(getattr(e, "desc", "") or ""):
                flips += 1
    return {"battles": battles, "flips": flips}


def faction_pct_for(cat: str, pressure: dict) -> int:
    """某类商品的势力局势日涨跌幅（%，正=涨负=跌；0=无战事不动）。"""
    b = int((pressure or {}).get("battles", 0) or 0)
    f = int((pressure or {}).get("flips", 0) or 0)
    if b <= 0 and f <= 0:
        return 0
    cat = cat if cat in ("war", "civil", "lux") else "civil"
    if cat == "war":
        return min(12, _WAR_UP_PCT * b)
    if cat == "civil":
        return min(8, _CIVIL_UP_PCT * b)
    return -min(10, _LUX_DOWN_PCT * f + 2 * b)


def apply_faction_pressure(world: World) -> str:
    """把势力局势确定性地压到当日价格上（在 LLM 定价/兜底漂移之后施加，保证势力
    影响必然落地，不因 LLM 漠视而消失）。返回变动摘要（空串=无战事无变动）。"""
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket) or not sm.commodities:
        return ""
    pressure = faction_pressure(world)
    changed = []
    for c in sm.commodities:
        if not isinstance(c, Commodity):
            continue
        pct = faction_pct_for(getattr(c, "cat", "civil"), pressure)
        if pct == 0:
            continue
        old = int(c.price)
        c.price = max(1, int(round(old * (1.0 + pct / 100.0))))
        # [!] prev_price 不动：定价层（apply_llm_prices/drift_prices_fallback）已把它设为
        # 「昨日收盘价」——压力层再写会让涨跌% 只显示压力增量、抹掉当日 LLM 定价的真实
        # 涨跌（market_summary 与次日 LLM 定价输入双双失真）。压力计入当日收盘而非独立一跳。
        # history 同日只留一条：定价层刚 push 的当日价原地替换为压力后终价（无当日条目才 append，
        # 兜住独立调用场景），防 cap 30 的折线窗口被双写腰斩。
        if c.history and int(c.history[-1]) == old:
            c.history[-1] = c.price
        else:
            c.history.append(c.price)
        if len(c.history) > _HISTORY_CAP:
            del c.history[:len(c.history) - _HISTORY_CAP]
        changed.append(f"{c.name}{pct:+d}%")
    war_part = f"战事{pressure['battles']}场/易主{pressure['flips']}次：{'、'.join(changed)}" if changed else ""
    # [季节玩法化 2026-09-06] 季节层（在此函数尾部内联——tick_stock_market 五个兜底出口
    # 都经它落地，自动全覆盖；仿势力压力「计入当日收盘/prev_price 不动」口径，但锚
    # base_price*季节系数温和收敛——直接每日乘系数会复利发散，冬季粮价会指数暴涨）。
    from src.services import calendar_engine as _cale
    fx = _cale.season_fx(world)
    for c in sm.commodities:
        if not isinstance(c, Commodity):
            continue
        mod = fx["stock_price"].get(getattr(c, "cat", "civil"), 1.0)
        if abs(mod - 1.0) < 1e-6:
            continue
        target = max(1, int(round(c.base_price * mod)))
        old_p = int(c.price)
        pull = (target - old_p) / max(1, old_p)
        pct2 = max(-0.15, min(0.15, pull * 0.5))    # 每日最多向季节目标靠 15%
        if abs(pct2) < 0.005:
            continue
        c.price = max(1, int(round(old_p * (1.0 + pct2))))
        if c.history and int(c.history[-1]) == old_p:
            c.history[-1] = c.price
        else:
            c.history.append(c.price)
        if len(c.history) > _HISTORY_CAP:
            del c.history[:len(c.history) - _HISTORY_CAP]
    return war_part


def market_summary(world: World) -> str:
    """[P34f] 股市行情文本摘要（LLM user 消息输入用 + UI tooltip 用）。

    列各 commodity symbol/name/当前价/基准价/涨跌%（据 prev_price）。
    """
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket) or not sm.commodities:
        return "（无商品）"
    lines = []
    _CAT_ZH = {"war": "军需", "civil": "民生", "lux": "奢侈"}
    for c in sm.commodities:
        if not isinstance(c, Commodity):
            continue
        cat_tag = f"[{_CAT_ZH.get(getattr(c, 'cat', 'civil'), '民生')}] "
        pct = ""
        if c.prev_price > 0:
            pct = f"（{('+' if c.price >= c.prev_price else '')}{round((c.price - c.prev_price) / c.prev_price * 100, 1)}%）"
        lines.append(f"{c.symbol}/{c.name}{cat_tag}：当前 {c.price}，基准 {c.base_price}{pct}")
    return "\n".join(lines)


def holdings_value(world: World) -> int:
    """玩家持仓当前市值（sum(holdings[symbol] * price)）。UI 浮盈显示用。"""
    sm = getattr(world, "stock_market", None)
    if not isinstance(sm, StockMarket):
        return 0
    holdings = world.player.stock_holdings if isinstance(world.player.stock_holdings, dict) else {}
    total = 0
    for sym, qty in holdings.items():
        c = sm.find(sym)
        if c is not None and isinstance(c, Commodity):
            total += int(qty) * int(c.price)
    return total
