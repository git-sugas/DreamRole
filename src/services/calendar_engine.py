"""[P24b] 历法引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b）：历法纯推导无 LLM 无新状态——1 年 = 4 季 x 3 月 x 30 日 = 360 日，
day_count（已有字段）-> (年, 季, 月, 日) 单向派生 + 季节天气池 + 日夜/天气氛围描述池。

[定版裁剪 2026-09-05 用户指示] 节日系统整体移除（节日池/今日节日上下文/节日货架/
节庆活动探测/节日事件全删，题材池与守护测试同步清）；历法日夜天气保留。
"""
from __future__ import annotations

from typing import Optional

from src.utils.rng import SeededRng


# ---- 历法常量：1 年 = 4 季 x 3 月 x 30 日 = 360 日 ----
DAYS_PER_MONTH = 30
MONTHS_PER_YEAR = 12
DAYS_PER_YEAR = MONTHS_PER_YEAR * DAYS_PER_MONTH   # 360
_SEASON_ZH = ("春", "夏", "秋", "冬")
_MONTH_IN_SEASON_ZH = ("一", "二", "三")

# ---- 季节 -> 天气权重池（_tick_time_phase 周期换天气用；权重展开成列表 rng.pick）----
# 春多雨、夏多晴与雷暴、秋多云雾、冬多风暴（题材化的「雪」由叙事承载，底层 key 不变）。
_SEASON_WEATHER_WEIGHTS = {
    0: {"clear": 3, "cloud": 2, "rain": 3, "fog": 1, "storm": 1},   # 春
    1: {"clear": 4, "cloud": 2, "rain": 1, "fog": 0, "storm": 3},   # 夏
    2: {"clear": 3, "cloud": 3, "rain": 1, "fog": 3, "storm": 0},   # 秋
    3: {"clear": 2, "cloud": 3, "rain": 1, "fog": 2, "storm": 4},   # 冬
}

# ---- [数据量] 日夜/天气氛围描述池（6 题材 x 9 键（4 时段 + 5 天气）各 >=3 句）----
# 供叙事 LLM 语境/氛围注入（时间天气行后的氛围补充），底层 phase/weather key 与
# _PHASE_ZH/_WEATHER_ZH 一致不变；缺键题材回退西幻。weather_flavor 确定性 SeededRng 取。
_GENRE_WEATHER_DESC: dict[str, dict[str, list[str]]] = {
    "xianxia": {
        "dawn": ["东方泛白，紫气东来", "晨雾初散，山间灵气最盛", "残月未落，霞光染红云海"],
        "day": ["日轮当空，灵气流转", "天光正盛，云卷云舒", "白日如练，山色空濛"],
        "dusk": ["夕阳西沉，晚霞似锦", "暮色四合，倦鸟归林", "残阳如血，染红半片天际"],
        "night": ["月华如水，星河璀璨", "夜色深沉，灵光点点", "万籁俱寂，月照中天"],
        "clear": ["万里无云，天地澄澈", "晴空如洗，远山如黛", "天高气爽，灵光朗照"],
        "cloud": ["云层翻涌，遮了半面天", "阴云低垂，山风渐起", "云海茫茫，如临仙境"],
        "rain": ["灵雨如丝，润泽万物", "细雨绵绵，洗尽铅华", "雨落如帘，溪水渐涨"],
        "fog": ["灵雾弥漫，十步不见人", "白雾锁山，如入迷境", "浓雾重重，隐有兽影"],
        "storm": ["雷声滚滚，紫电裂空", "天威浩荡，暴雨倾盆", "狂风呼啸，如雷劫压顶"],
    },
    "wuxia": {
        "dawn": ["鸡鸣破晓，晨露未干", "东方既白，官道渐明", "炊烟初起，天色微亮"],
        "day": ["日上三竿，市集喧闹", "天光大亮，行人渐多", "晴日高照，酒旗招展"],
        "dusk": ["夕阳斜照，倦鸟归巢", "暮色四合，客栈掌灯", "残阳如血，马蹄声远"],
        "night": ["月黑风高，更鼓声沉", "星垂平野，万籁俱寂", "夜深人静，打更声由远及近"],
        "clear": ["天朗气清，惠风和畅", "晴空万里，适合赶路", "艳阳高照，尘土轻扬"],
        "cloud": ["乌云压城，山雨欲来", "阴云密布，凉风习习", "天阴欲雨，行人匆匆"],
        "rain": ["细雨斜织，青石板路泛光", "骤雨如注，行人避于檐下", "雨打芭蕉，别有一番愁绪"],
        "fog": ["晨雾朦胧，远山若隐若现", "大雾封路，只闻人语声", "雾气缭绕，剑影难辨"],
        "storm": ["狂风骤起，飞沙走石", "电闪雷鸣，暴雨倾盆", "风卷残云，天地变色"],
    },
    "modern": {
        "dawn": ["天蒙蒙亮，城市初醒", "晨光熹微，早班车驶过", "东方泛白，街灯渐灭"],
        "day": ["车水马龙，城市喧嚣", "艳阳高照，人潮如织", "晴空下高楼林立"],
        "dusk": ["华灯初上，晚高峰来临", "夕阳西斜，霓虹渐亮", "暮色中车流如河"],
        "night": ["霓虹闪烁，夜生活开始", "夜色深沉，路灯昏黄", "万家灯火，星子稀疏"],
        "clear": ["晴空万里，适合出门", "天朗气清，能见度极好", "蓝天白云，微风不燥"],
        "cloud": ["阴天转多云，体感微凉", "云层低垂，天色灰蒙", "乌云聚拢，似要变天"],
        "rain": ["雨点敲窗，路面湿滑", "细雨蒙蒙，行人打伞", "暴雨如注，街道积水"],
        "fog": ["雾霾弥漫，能见度低", "大雾笼罩，高楼隐没", "晨雾缭绕，红绿灯朦胧"],
        "storm": ["狂风大作，广告牌摇晃", "雷暴过境，交通受阻", "台风天，风急雨骤"],
    },
    "scifi": {
        "dawn": ["恒星升起，穹顶渐亮", "基地晨钟，舱内灯光转明", "主星破晓，地平线泛金"],
        "day": ["恒星当空，能量充沛", "白昼舱段，秩序井然", "星光被主星遮去，天空明亮"],
        "dusk": ["恒星西沉，穹顶转暗", "暮色浸入舱体，警示灯亮起", "主星落山，天际一线橙红"],
        "night": ["星河璀璨，深空寂寥", "夜幕笼罩，只有仪器轻鸣", "暗夜无月，星辰格外清晰"],
        "clear": ["深空澄澈，星光可辨", "无云无尘，观测条件极佳", "大气稀薄，天空湛蓝"],
        "cloud": ["气态巨行星的云带翻涌", "云层厚重，遮蔽星光", "薄云如纱，缓缓飘过"],
        "rain": ["冷凝雨滴敲击舱壳", "人工雨幕浇灌生态舱", "酸性细雨，腐蚀警示"],
        "fog": ["迷雾弥漫，传感器失灵", "冷凝雾气笼罩舱道", "薄雾中指示灯忽明忽暗"],
        "storm": ["磁暴来袭，通讯中断", "粒子风暴，护盾过载", "离子狂风，天昏地暗"],
    },
    "apocalypse": {
        "dawn": ["天边泛灰，废墟初醒", "晨光穿透尘埃，微弱发亮", "荒原破晓，寒意料峭"],
        "day": ["惨白日头，晒裂大地", "尘土飞扬，荒原灼热", "天光惨淡，废土无边"],
        "dusk": ["血色残阳，染红废墟", "暮色降临，变异生物开始活动", "黄昏时分，风沙渐起"],
        "night": ["漆黑如墨，只有风声", "废土之夜，危机四伏", "寒夜无声，星空被尘埃遮蔽"],
        "clear": ["天空惨白，尘土味呛人", "无云的荒原，日头毒辣", "晴日无风，热浪翻涌"],
        "cloud": ["灰云压顶，如末日重临", "阴云密布，光线昏暗", "乌云翻涌，透不下一丝阳光"],
        "rain": ["酸雨落下，滋滋作响", "冷雨浇在废墟上", "泥雨倾盆，地面泥泞"],
        "fog": ["辐射雾弥漫，能见度极低", "灰雾吞没废墟，方向难辨", "毒雾笼罩，呼吸不畅"],
        "storm": ["沙暴遮天，砂砾打脸", "风暴卷起瓦砾，天昏地暗", "雷暴夹着酸雨，如末日审判"],
    },
    "western_fantasy": {
        "dawn": ["晨星未隐，东方微明", "薄雾散尽，雀鸟初啼", "第一缕阳光穿过林隙"],
        "day": ["阳光普照，原野明亮", "白昼晴朗，集市喧闹", "艳阳高照，山峦苍翠"],
        "dusk": ["夕阳染红天际，晚钟悠扬", "暮色四合，篝火将起", "晚霞如锦，牛羊归栏"],
        "night": ["月色溶溶，星河横亘", "夜幕低垂，猫头鹰啼", "静夜无风，篝火噼啪"],
        "clear": ["晴空万里，适合远行", "天朗气清，惠风和畅", "蓝天如洗，白云悠悠"],
        "cloud": ["云层翻涌，遮天蔽日", "阴云低垂，山雨欲来", "灰云压顶，风势渐紧"],
        "rain": ["细雨润泽，草木青翠", "骤雨敲打石板路", "雨水如帘，溪流欢腾"],
        "fog": ["晨雾如纱，笼罩林间", "大雾弥漫，方向难辨", "雾锁古堡，钟声朦胧"],
        "storm": ["雷声轰鸣，闪电裂空", "狂风暴雨，树摇欲折", "风暴席卷，天地变色"],
    },
}


def weather_flavor(genre_id: str, key: str, rng: Optional[SeededRng] = None) -> str:
    """从日夜/天气描述池确定性取一句氛围描述（缺题材/缺 key 回退西幻/白昼）。"""
    tbl = _GENRE_WEATHER_DESC.get(genre_id) or _GENRE_WEATHER_DESC["western_fantasy"]
    pool = tbl.get(key) or tbl.get("day") or [""]
    if rng is None:
        return pool[0]
    return rng.pick(pool)

# ---------------------------------------------------------------------------
# 历法推导（纯函数）
# ---------------------------------------------------------------------------
def _day_cn(n: int) -> str:
    """日数 -> 中文（初一..初十/十一..十九/二十/廿一..廿九/三十）。"""
    n = max(1, min(30, int(n)))
    if n <= 10:
        return "初" + "一二三四五六七八九十"[n - 1]
    if n < 20:
        return "十" + "一二三四五六七八九"[n - 11]
    if n == 20:
        return "二十"
    if n < 30:
        return "廿" + "一二三四五六七八九"[n - 21]
    return "三十"


def calendar(world) -> dict:
    """day_count -> (年/季/月/日) 纯推导（day 从 1 起；1 年 360 日）。

    返回 {year, season, season_idx, month, day, date_str}；date_str 如
    「第1年春二月十五」供徽章/上下文单一来源。
    """
    day_count = max(1, int(getattr(world, "day_count", 1) or 1))
    doy = (day_count - 1) % DAYS_PER_YEAR            # 0-359
    year = (day_count - 1) // DAYS_PER_YEAR + 1
    season_idx = doy // 90                            # 0-3 -> 春夏秋冬
    month = doy // DAYS_PER_MONTH + 1                 # 1-12
    day = doy % DAYS_PER_MONTH + 1                    # 1-30
    season = _SEASON_ZH[season_idx]
    date_str = (f"第{year}年{season}{_MONTH_IN_SEASON_ZH[(month - 1) % 3]}月"
                f"{_day_cn(day)}")
    return {"year": year, "season": season, "season_idx": season_idx,
            "month": month, "day": day, "date_str": date_str}


def seasonal_weather_pool(world) -> list[str]:
    """季节加权天气池（展开成带重复的列表供 rng.pick；夏偏晴/雷暴、冬偏风暴）。"""
    season_idx = calendar(world)["season_idx"]
    weights = _SEASON_WEATHER_WEIGHTS.get(season_idx) or _SEASON_WEATHER_WEIGHTS[0]
    pool: list[str] = []
    for weather, w in weights.items():
        pool.extend([weather] * max(0, int(w)))
    return pool or ["clear"]




# ---- [季节玩法化 2026-09-06 用户指示] 季节驱动经济/采集/遇怪/敌袭（纯推导，零 LLM）----
# 季节 -> 玩法系数表（春生/夏长/秋收/冬藏）：
#   stock_price：股市大宗商品季节系数（按商品 cat 标签：war 军需/civil 民生/lux 奢侈）；
#   [!] cat key 必须与 Commodity.cat 白名单（war/civil/lux）一致——曾误写 luxury 致
#   奢侈品季节系数恒取默认 1.0（2026-09-11 测试补写时抓出，已修）。
#   gather_regen：资源点丰度恢复系数（冬 0.5——万物蛰伏）；
#   monster_chance：野外遇怪概率系数（冬 1.3——野兽饥饿觅食出没更频）；
#   siege_chance：据点敌袭概率系数（冬 1.5——饥荒催匪）；
#   hint：注入【时间天气】行尾的一句玩法提示（LLM 与玩家共同知晓）。
_SEASON_EFFECTS = {
    0: {"stock_price": {"war": 1.0, "civil": 0.95, "lux": 1.05},
        "gather_regen": 1.0, "monster_chance": 1.0, "siege_chance": 1.0,
        "hint": "春回大地，百草萌发"},
    1: {"stock_price": {"war": 1.05, "civil": 1.0, "lux": 1.05},
        "gather_regen": 1.2, "monster_chance": 1.1, "siege_chance": 1.0,
        "hint": "盛夏物产丰茂，出行人多兽也多"},
    2: {"stock_price": {"war": 1.0, "civil": 1.0, "lux": 1.1},
        "gather_regen": 1.1, "monster_chance": 0.9, "siege_chance": 0.9,
        "hint": "秋收时节，粮价回落行商繁忙"},
    3: {"stock_price": {"war": 1.05, "civil": 1.15, "lux": 0.9},
        "gather_regen": 0.5, "monster_chance": 1.3, "siege_chance": 1.5,
        "hint": "寒冬肃杀，粮价上浮野兽觅食，据点易遭饥匪袭扰"},
}


def season_effects(world) -> dict:
    """[季节玩法化] 当前季节的玩法系数（纯推导；preset.season_effects_enabled=False
    时全 1.0 关闭——由调用方预判，此函数只算系数不读开关）。"""
    return dict(_SEASON_EFFECTS.get(calendar(world)["season_idx"])
                or _SEASON_EFFECTS[0])


def season_fx(world) -> dict:
    """[季节玩法化] 带开关判定的季节系数（world.config_overlay['season_fx'] 为 False 时
    全 1.0 + 空 hint——build 时从 preset.season_effects_enabled 写入；
    [!] 老世界**缺键即关**（ov.get("season_fx", False)），与 §22「缺键=关不惊扰老世界」同口径。
    [纠偏 2026-09-10] 旧注释写「缺键默认开」，与实现相反。）
    各玩法接线点统一读本函数（引擎函数保持零 preset 依赖）。"""
    on = False
    try:
        ov = getattr(world, "config_overlay", None)
        on = bool(ov.get("season_fx", False)) if isinstance(ov, dict) else False
    except Exception:
        on = False
    if not on:
        return {"stock_price": {"war": 1.0, "civil": 1.0, "lux": 1.0},
                "gather_regen": 1.0, "monster_chance": 1.0, "siege_chance": 1.0,
                "hint": ""}
    return season_effects(world)
