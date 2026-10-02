"""[P24a] 住宅与仓库引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b）：LLM 只出宅名/一句描写的语义（WorldSimService.purchase_home 起名
小调用，失败兜底本模块题材池名），购宅/家具价格、仓库/陈列容量、休憩回复与跳时全在
本模块纯 Python + SeededRng 确定性结算（同 world+tick+salt 同结果）。

住宅挂在聚落（Location.kind=="settlement"）上不新建 Location：「在住宅」= 玩家当前地点
是聚落且在此聚落拥有宅（player_home_at）。每聚落限一宅，多聚落可多宅；仓库/陈列存
item_id 引用不占负重，出仓/取回统一走 combat_engine.try_add_to_inventory（守 §22 入包
不变量）。题材化档名/宅名/家具池容量守 §23 数据量铁律（tests/test_genre_data_volume.py
TestHomePool 守护：档名 3/题材、宅名 >=6/题材、家具 >=10/题材且四类各 >=2）。
"""
from __future__ import annotations

from typing import Optional

from src.models.world import World, Home, WorldEvent
from src.models.world_sim_preset import GenreText
from src.services import combat_engine as ce
from src.services import npc_reaction_engine as nre
from src.services import social_engine as soc
from src.services.trade_engine import pace_factors, world_pace
from src.utils.rng import SeededRng

# ---- 档位基价（tier 1-3：题材化档名见 _GENRE_HOME_TEMPLATES；价格再乘
# economy_pace 买入倍率 + 聚落物价系数）----
_TIER_BASE_PRICE = (300, 1200, 4000)
# 仓库容量 = 基础 30 + 储物(storage)家具数 x 10；陈列容量 = 陈列(display)家具数 x 2
# （[网格v3] 陈列必须建造陈列柜才有格子，基础 0）。
_BASE_STASH = 30
_STASH_PER_STORAGE = 10
_BASE_DISPLAY = 0
_DISPLAY_PER_CASE = 2
# 床铺(bed)家具：宅中休憩附小 XP 奖（有床即可，多床不叠加——安歇一夜只消一次）。
_REST_BED_XP = 5

# ---- [P34c] 住宅内建筑目录（6 题材全覆盖，缺键回退西幻，守 §23）----
# 每题材 6 种建筑（garden/forge/alchemy/refine/study/warehouse 各 1），{name,kind,desc,base_price}。
# 建造/升级价 = base_price x economy_pace 买入倍率 x (1 + 0.5 x 当前等级)；升级每级 +50% 价。
# [审核修复 2026-09-13] 原写「x 聚落物价系数」，但 building_price 不接 loc、实现只乘
# pace_factors——文档与实现二选一，此处按实现订正（建筑价不随聚落波动是既定简化）。
# 锻造/炼制等级门控配方；洗练室当前只需 1 级，书房研读待接线。
_GENRE_BUILDING_CATALOG: dict[str, list[dict]] = {
    "western_fantasy": [
        {"name": "药圃", "kind": "garden", "desc": "篱笆围起的药圃，可种植草药作物。", "base_price": 500},
        {"name": "铁匠铺", "kind": "forge", "desc": "炉火通明的铁匠铺，可锻造兵刃护甲。", "base_price": 800},
        {"name": "炼金工坊", "kind": "alchemy", "desc": "瓶瓶罐罐的炼金台，可炼制药剂。", "base_price": 800},
        {"name": "附魔室", "kind": "refine", "desc": "铭刻符文的附魔室，可洗练装备词条。", "base_price": 1000},
        {"name": "藏书阁", "kind": "study", "desc": "静谧的藏书阁，书架上摆满旧典籍。", "base_price": 600},
        {"name": "仓库", "kind": "warehouse", "desc": "干燥的仓库，建造后才能使用仓库存取。", "base_price": 400},
    ],
    "xianxia": [
        {"name": "灵田", "kind": "garden", "desc": "灵脉滋养的灵田，可培育灵植。", "base_price": 500},
        {"name": "炼器阁", "kind": "forge", "desc": "地火涌动的炼器阁，可炼制法器兵刃。", "base_price": 800},
        {"name": "丹房", "kind": "alchemy", "desc": "药香弥漫的丹房，可炼制丹药。", "base_price": 800},
        {"name": "洗炼密室", "kind": "refine", "desc": "灵纹密布的洗炼室，可重铸法器灵纹。", "base_price": 1000},
        {"name": "藏经阁", "kind": "study", "desc": "清幽的藏经阁，层层经架散着墨香。", "base_price": 600},
        {"name": "储物阁", "kind": "warehouse", "desc": "禁制护持的储物阁，建造后才能开仓存取。", "base_price": 400},
    ],
    "wuxia": [
        {"name": "药园", "kind": "garden", "desc": "精耕细作的药园，可种植药材。", "base_price": 500},
        {"name": "铁匠炉", "kind": "forge", "desc": "锤声不绝的铁匠炉，可锻造兵刃。", "base_price": 800},
        {"name": "药庐", "kind": "alchemy", "desc": "草药飘香的药庐，可熬制膏丹。", "base_price": 800},
        {"name": "淬剑室", "kind": "refine", "desc": "寒气森森的淬剑室，可重铸兵刃锋芒。", "base_price": 1000},
        {"name": "书房", "kind": "study", "desc": "笔墨纸砚齐备的书房，桌上摊着武学札记。", "base_price": 600},
        {"name": "库房", "kind": "warehouse", "desc": "厚墙深锁的库房，建造后才能开仓存取。", "base_price": 400},
    ],
    "modern": [
        {"name": "屋顶温室", "kind": "garden", "desc": "阳光板温室，可种植果蔬。", "base_price": 500},
        {"name": "改装车间", "kind": "forge", "desc": "工具齐全的改装车间，可改造装备。", "base_price": 800},
        {"name": "配药室", "kind": "alchemy", "desc": "无菌配药室，可配制药剂。", "base_price": 800},
        {"name": "纳米改造台", "kind": "refine", "desc": "纳米级改造台，可重 roll 装备词条。", "base_price": 1000},
        {"name": "书房", "kind": "study", "desc": "安静的书房，灯下摆着几本技能手册。", "base_price": 600},
        {"name": "储藏室", "kind": "warehouse", "desc": "带门禁的储藏室，建造后才能开仓存取。", "base_price": 400},
    ],
    "scifi": [
        {"name": "水培舱", "kind": "garden", "desc": "循环营养液的水培舱，可培育作物。", "base_price": 500},
        {"name": "制造舱", "kind": "forge", "desc": "3D 打印制造舱，可制造装备。", "base_price": 800},
        {"name": "合成舱", "kind": "alchemy", "desc": "分子合成舱，可合成药剂。", "base_price": 800},
        {"name": "纳米重组台", "kind": "refine", "desc": "纳米重组台，可重铸装备词条。", "base_price": 1000},
        {"name": "数据终端", "kind": "study", "desc": "高速数据终端，屏幕上滚动着旧档案。", "base_price": 600},
        {"name": "储物舱", "kind": "warehouse", "desc": "真空储物舱，建造后才能开仓存取。", "base_price": 400},
    ],
    "apocalypse": [
        {"name": "净化圃", "kind": "garden", "desc": "滤辐射的净化圃，可种植抗性作物。", "base_price": 500},
        {"name": "焊接工坊", "kind": "forge", "desc": "拼凑的焊接工坊，可改装装备。", "base_price": 800},
        {"name": "配药台", "kind": "alchemy", "desc": "简陋配药台，可配制草药药剂。", "base_price": 800},
        {"name": "拆解台", "kind": "refine", "desc": "拆解重组台，可重 roll 装备词条。", "base_price": 1000},
        {"name": "书桌", "kind": "study", "desc": "破旧书桌，上面压着几页残存手稿。", "base_price": 600},
        {"name": "物资间", "kind": "warehouse", "desc": "铁丝网围的物资间，建造后才能开仓存取。", "base_price": 400},
    ],
}

# ---- 题材化住宅池（6 题材全覆盖，缺键回退西幻，守 §23）----
# tiers: 档位显示名 x3（小/中/大三档）| names: 宅名兜底池（LLM 起名失败时 SeededRng 挑选）
# furniture: 家具目录 [{name,kind,price,desc}]，kind 四类：storage(+10 仓库)/bed(休憩 XP)/
# decor(叙事素材)/display(+2 陈列)。价格再乘 economy_pace 买入倍率（购入时折算）。
_GENRE_HOME_TEMPLATES: dict[str, dict] = {
    "western_fantasy": {
        "tiers": ["农舍小屋", "城镇宅院", "贵族庄园"],
        "names": ["橡树庭院", "暮色小筑", "磨坊人家", "石墙别院", "泉水庄园", "鸦木宅邸", "金麦农庄"],
        "furniture": [
            {"name": "橡木储物柜", "kind": "storage", "price": 140, "desc": "厚实的橡木柜，能塞下不少行囊。"},
            {"name": "地窖货架", "kind": "storage", "price": 120, "desc": "通往地窖的木货架，阴凉干燥。"},
            {"name": "旅行储物箱", "kind": "storage", "price": 100, "desc": "带铜锁的行李箱，远行归来随手一放。"},
            {"name": "羽绒大床", "kind": "bed", "price": 160, "desc": "鹅绒填充的被褥，一夜安睡到天明。"},
            {"name": "阁楼小床", "kind": "bed", "price": 90, "desc": "阁楼上的木床，胜在安静。"},
            {"name": "壁挂织毯", "kind": "decor", "price": 80, "desc": "织着河谷风景的挂毯，为厅堂添色。"},
            {"name": "鹿角装饰", "kind": "decor", "price": 70, "desc": "猎来的鹿角钉在墙上，粗犷气派。"},
            {"name": "诗人画像", "kind": "decor", "price": 110, "desc": "一位吟游诗人的油画像，眼神狡黠。"},
            {"name": "橡木陈列柜", "kind": "display", "price": 130, "desc": "玻璃门展示柜，摆战利品正合适。"},
            {"name": "银烛台展台", "kind": "display", "price": 100, "desc": "带烛照的小展台，宝贝在光下生辉。"},
            {"name": "徽章挂板", "kind": "display", "price": 90, "desc": "软木挂板，钉上冒险收来的徽章印记。"},
        ],
    },
    "xianxia": {
        "tiers": ["洞府别院", "灵宅", "仙府"],
        "names": ["听雨小筑", "云隐洞府", "青竹雅居", "丹霞仙府", "落星别院", "灵泉山庄", "紫气东来府"],
        "furniture": [
            {"name": "储物玉匣", "kind": "storage", "price": 150, "desc": "内藏乾坤的玉匣，收纳杂物绰绰有余。"},
            {"name": "灵木药柜", "kind": "storage", "price": 130, "desc": "百格药柜，灵草药材料各归其位。"},
            {"name": "芥子木箱", "kind": "storage", "price": 110, "desc": "以芥子纳须弥之法制的小木箱。"},
            {"name": "聚灵玉榻", "kind": "bed", "price": 170, "desc": "睡卧其间灵气自聚，行功事半功倍。"},
            {"name": "蒲团软榻", "kind": "bed", "price": 90, "desc": "素面蒲团拼成的卧榻，清简安神。"},
            {"name": "山水屏风", "kind": "decor", "price": 90, "desc": "绘着云山雾海的六扇屏风。"},
            {"name": "灵兽雕像", "kind": "decor", "price": 120, "desc": "灵狐石像一尊，栩栩如生。"},
            {"name": "云纹挂帘", "kind": "decor", "price": 80, "desc": "素纱挂帘绣云纹，风过微动。"},
            {"name": "玉架展台", "kind": "display", "price": 140, "desc": "白玉雕成的展架，衬得宝物温润生光。"},
            {"name": "丹炉摆台", "kind": "display", "price": 110, "desc": "小巧丹炉造型的摆台，仙气缭绕。"},
            {"name": "剑器悬架", "kind": "display", "price": 130, "desc": "悬剑木架，将旧剑供起作纪念。"},
        ],
    },
    "wuxia": {
        "tiers": ["客栈雅间", "四合宅院", "山水庄园"],
        "names": ["听风小院", "柳岸人家", "燕子坞别院", "剑影山居", "杏花别院", "镖局老宅", "醉月山庄"],
        "furniture": [
            {"name": "樟木大衣箱", "kind": "storage", "price": 120, "desc": "樟木打的衣箱，防虫防潮。"},
            {"name": "后院库房架", "kind": "storage", "price": 140, "desc": "后院搭起的货架，行李兵器分门别类。"},
            {"name": "暗格木柜", "kind": "storage", "price": 110, "desc": "带暗格的木柜，贵重物件有处安放。"},
            {"name": "楠木拔步床", "kind": "bed", "price": 170, "desc": "楠木大床，冬暖夏凉。"},
            {"name": "湘妃竹榻", "kind": "bed", "price": 80, "desc": "竹榻一张，夏夜纳凉正好。"},
            {"name": "水墨中堂", "kind": "decor", "price": 90, "desc": "名家水墨中堂，题着「静观」。"},
            {"name": "青瓷花瓶", "kind": "decor", "price": 100, "desc": "案头青瓷瓶，插一枝时令花。"},
            {"name": "素面屏风", "kind": "decor", "price": 70, "desc": "木框绢面屏风，隔出一方静室。"},
            {"name": "兵器架子", "kind": "display", "price": 130, "desc": "十八般兵器的木架，威风堂堂。"},
            {"name": "多宝阁", "kind": "display", "price": 150, "desc": "错落多宝阁，奇珍异玩各有其龛。"},
            {"name": "匾额悬板", "kind": "display", "price": 90, "desc": "悬一块手书木匾，纪念江湖行。"},
        ],
    },
    "modern": {
        "tiers": ["单身公寓", "两居室", "花园洋房"],
        "names": ["阳光小筑", "临江华庭 901", "梧桐小院", "蓝湾公寓", "翠湖天地", "半山别墅", "老巷民宿"],
        "furniture": [
            {"name": "组合储物柜", "kind": "storage", "price": 130, "desc": "整面墙的组合柜，收纳力拉满。"},
            {"name": "顶柜置物架", "kind": "storage", "price": 110, "desc": "加装的顶柜，换季杂物全上墙。"},
            {"name": "玄关鞋柜", "kind": "storage", "price": 100, "desc": "带换鞋凳的玄关柜，出门入户不狼狈。"},
            {"name": "实木大床", "kind": "bed", "price": 160, "desc": "实木床架加乳胶垫，睡眠质量起飞。"},
            {"name": "沙发客床", "kind": "bed", "price": 100, "desc": "展开是床、收起是沙发，两用省地。"},
            {"name": "落地灯组", "kind": "decor", "price": 90, "desc": "暖光落地灯，客厅瞬间有了氛围。"},
            {"name": "挂画摆件", "kind": "decor", "price": 80, "desc": "一组装饰挂画，墙不再空荡。"},
            {"name": "绿植角", "kind": "decor", "price": 70, "desc": "龟背竹与琴叶榕，屋里有了生气。"},
            {"name": "玻璃展示柜", "kind": "display", "price": 140, "desc": "带射灯的玻璃柜，手办收藏的归宿。"},
            {"name": "洞洞板墙", "kind": "display", "price": 90, "desc": "洞洞板配挂钩，陈列旅行纪念品。"},
            {"name": "证书相框墙", "kind": "display", "price": 100, "desc": "把奖章与合影裱起来挂上墙。"},
        ],
    },
    "scifi": {
        "tiers": ["舱室单元", "轨道公寓", "星港庄园"],
        "names": ["B-7 居住舱", "静默轨道屋", "第七区胶囊宅", "环形舱公寓", "新星家园", "泊位 9 号宅", "观星穹顶屋"],
        "furniture": [
            {"name": "壁嵌储物单元", "kind": "storage", "price": 130, "desc": "舱壁内嵌储物格，空间利用率极高。"},
            {"name": "磁悬浮置物架", "kind": "storage", "price": 120, "desc": "悬浮在半空的置物架，科技感十足。"},
            {"name": "货舱扩展箱", "kind": "storage", "price": 100, "desc": "标准货柜改造的收纳箱，量大管饱。"},
            {"name": "深眠休眠舱", "kind": "bed", "price": 180, "desc": "民用版休眠舱，睡眠效率提升三成。"},
            {"name": "记忆棉床垫", "kind": "bed", "price": 110, "desc": "温感记忆棉床组，贴合每一寸疲惫。"},
            {"name": "全息水族箱", "kind": "decor", "price": 130, "desc": "全息投影的鱼缸，虚鱼游得悠然。"},
            {"name": "舷窗观景幕", "kind": "decor", "price": 100, "desc": "整面墙的星域实况屏，流星常客。"},
            {"name": "旧地球摆件", "kind": "decor", "price": 80, "desc": "一枚地球时代的沙漏，提醒时间仍在流。"},
            {"name": "悬浮展示台", "kind": "display", "price": 140, "desc": "反重力展台，藏品缓缓自转。"},
            {"name": "标本保藏柜", "kind": "display", "price": 120, "desc": "恒温恒湿展柜，存放异星标本。"},
            {"name": "勋章铭牌墙", "kind": "display", "price": 90, "desc": "历次任务的铭牌与勋章依次排开。"},
        ],
    },
    "apocalypse": {
        "tiers": ["避难隔间", "加固居所", "避难所庄园"],
        "names": ["3 号避难隔间", "变电站小屋", "加油站里屋", "废弃公寓 502", "净水站旁院", "哨塔居室", "温室棚屋"],
        "furniture": [
            {"name": "铁皮储物柜", "kind": "storage", "price": 120, "desc": "从旧办公楼拆来的铁皮柜，结实耐用。"},
            {"name": "弹药箱货架", "kind": "storage", "price": 130, "desc": "弹药箱码成的货架，能扛能装。"},
            {"name": "货柜扩展仓", "kind": "storage", "price": 140, "desc": "回收集装箱辟出的仓储区。"},
            {"name": "加固板床", "kind": "bed", "price": 100, "desc": "钢管焊的床架，铺上捡来的床垫。"},
            {"name": "睡袋铺位", "kind": "bed", "price": 70, "desc": "军规睡袋直接铺地，凑合但暖和。"},
            {"name": "旧世界海报", "kind": "decor", "price": 50, "desc": "褪色的电影海报，钉上墙就是念想。"},
            {"name": "电台角落", "kind": "decor", "price": 90, "desc": "修好的旧电台，夜里放着杂音歌单。"},
            {"name": "手绘地图墙", "kind": "decor", "price": 60, "desc": "手绘的安全区地图，钉满图钉。"},
            {"name": "战利品铁架", "kind": "display", "price": 110, "desc": "焊的铁架，摆着变异兽骨与旧徽章。"},
            {"name": "防爆展示柜", "kind": "display", "price": 130, "desc": "防弹玻璃柜，最珍贵的物资配最硬的柜。"},
            {"name": "弹壳风铃架", "kind": "display", "price": 80, "desc": "弹壳做的风铃挂在门口，风过作响。"},
        ],
    },
}


# ---------------------------------------------------------------------------
# 题材化读取（缺键回退西幻，守 §23）
# ---------------------------------------------------------------------------
def genre_pool(world: World) -> dict:
    """读当前世界的题材住宅池（attribute_template_id 选池，缺键回退西幻）。"""
    tid = (getattr(world, "config_overlay", None) or {}).get("attribute_template_id") \
        or "western_fantasy"
    return _GENRE_HOME_TEMPLATES.get(tid) or _GENRE_HOME_TEMPLATES["western_fantasy"]


def tier_name(world: World, tier: int) -> str:
    """档位题材化显示名（tier 1-3）。"""
    tiers = genre_pool(world).get("tiers") or []
    i = max(0, min(2, int(tier) - 1))
    return tiers[i] if i < len(tiers) else f"{int(tier)} 级住宅"


def fallback_home_name(world: World, loc) -> str:
    """LLM 起名失败/不可用时的题材宅名兜底（SeededRng 确定性：同 world+tick+loc 同名）。"""
    rng = SeededRng.seed_from(world.id, int(getattr(world, "tick_count", 0) or 0),
                              f"home_name_{getattr(loc, 'id', '')}")
    pool = genre_pool(world).get("names") or ["无名宅院"]
    return rng.pick(pool) or "无名宅院"


def furniture_catalog(world: World) -> list[dict]:
    """题材家具目录（家具购置 UI 列表数据源；成交价按 furniture_price 折算）。"""
    return [dict(f) for f in (genre_pool(world).get("furniture") or [])]


# ---------------------------------------------------------------------------
# 价格与容量
# ---------------------------------------------------------------------------
def settlement_price_factor(loc) -> float:
    """聚落物价系数：稳定度 50 为基准 1.0，每点 +/-0.5%，钳 0.75-1.25（确定性，无新字段）。

    [!] 稳定度 0 是合法值（废弃聚落最便宜），不可 `or 50` 兜底——会把 0 吞成 50
    （守 §11 防 0 吞铁律，同 _gi 语义）。
    """
    v = getattr(loc, "stability", 50)
    if v is None:
        v = 50
    try:
        stability = int(v)
    except (TypeError, ValueError):
        stability = 50
    return max(0.75, min(1.25, 1.0 + (stability - 50) / 200.0))


def home_price(world: World, loc, tier: int) -> int:
    """购宅价 = 档位基价 x economy_pace 买入倍率 x 聚落物价系数。"""
    base = _TIER_BASE_PRICE[max(0, min(2, int(tier) - 1))]
    buy_mult = pace_factors(world_pace(world))[0]
    return max(1, int(round(base * buy_mult * settlement_price_factor(loc))))


def max_home_tier(loc) -> int:
    """[P34e] 聚落允许的最高宅邸 tier：village 限 1，city/town/未指定 全档 3。

    叠加维度（读 loc.settlement_size，不改 kind）；buy_home + UI 共用此口径。
    """
    ss = getattr(loc, "settlement_size", "") or ""
    return 1 if ss == "village" else 3


def furniture_price(world: World, entry: dict) -> int:
    """家具成交价 = 池价 x economy_pace 买入倍率。"""
    try:
        raw = int((entry or {}).get("price", 0) or 0)
    except (TypeError, ValueError):
        raw = 0
    return max(1, int(round(raw * pace_factors(world_pace(world))[0])))


def stash_capacity(home: Home) -> int:
    """仓库容量：必须建 warehouse 建筑才有仓（未建=0）。

    容量 = 30 + 10*(仓库等级-1) + 储物家具数 x 10。存量档经 ensure_home_grid 自动补建
    仓库（level1），故老档容量不缩水（30 + 储物家具 x10）。"""
    b = building_of(home, "warehouse")
    if b is None:
        return 0
    lvl = max(1, int(b.get("level", 1) or 1))
    n = sum(1 for f in (home.furniture or [])
            if isinstance(f, dict) and f.get("kind") == "storage")
    return _BASE_STASH + 10 * (lvl - 1) + n * _STASH_PER_STORAGE


def display_capacity(home: Home) -> int:
    """陈列容量 = 陈列家具数 x 2（[网格v3] 必须建陈列柜才有格子，基础 0）。"""
    n = sum(1 for f in (home.furniture or [])
            if isinstance(f, dict) and f.get("kind") == "display")
    return _BASE_DISPLAY + n * _DISPLAY_PER_CASE


# ---------------------------------------------------------------------------
# 购宅 / 在宅判定
# ---------------------------------------------------------------------------
def home_at(world: World, loc_id: str) -> Optional[Home]:
    """取该聚落已有住宅（每聚落限一宅）。"""
    for h in world.homes:
        if h.location_id == loc_id:
            return h
    return None


def player_home_at(world: World, loc) -> Optional[Home]:
    """「在住宅」判定：当前地点是聚落且在此聚落拥有宅（loc 为 None/非聚落返回 None）。"""
    if loc is None or getattr(loc, "kind", "") != "settlement":
        return None
    return home_at(world, loc.id)


def buy_home(world: World, loc, tier: int, name: str = "", desc: str = "") -> tuple[bool, str]:
    """购宅（纯 Python）：校验聚落/重复/金币 -> 扣费 -> 建 Home 入 world.homes + home_ids
    + 世界事件。返回 (ok, err)；成功 err 为空串。宅名空串时题材池兜底（确定性）。

    [P34e] tier 受聚落规模门控：village 限 tier1，city/town/未指定全档 1-3。
    """
    if getattr(loc, "kind", "") != "settlement":
        return False, f"「{getattr(loc, 'name', '此地')}」不是聚落，无法置宅"
    if home_at(world, loc.id) is not None:
        return False, f"你在「{loc.name}」已有住宅（每处聚落限一宅）"
    mt = max_home_tier(loc)
    t = max(1, min(mt, int(tier)))
    if int(tier) > mt:
        return False, f"「{loc.name}」是村庄，仅能购置小宅（一档）。"
    price = home_price(world, loc, t)
    p = world.player
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    if int(p.gold) < price:
        return False, f"{gt.currency}不足"
    p.gold = int(p.gold) - price
    nm = (name or "").strip() or fallback_home_name(world, loc)
    home = Home(
        name=nm, desc=(desc or "").strip(), location_id=loc.id, tier=t,
        purchased_day=max(1, int(getattr(world, "day_count", 1) or 1)),
    )
    world.homes.append(home)
    if home.id not in (p.home_ids or []):
        p.home_ids.append(home.id)
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="event", severity="minor",
        title="购置住宅",
        desc=f"玩家在「{loc.name}」置下{tier_name(world, t)}「{home.name}」，耗资 {price} {gt.currency}。",
        locations=[loc.id],
    ))
    return True, ""


# ---------------------------------------------------------------------------
# 仓库 / 陈列（物品移动；出向必须走 try_add_to_inventory 守负重，§22 不变量）
# ---------------------------------------------------------------------------
def stash_deposit(home: Home, player, item_id: str) -> bool:
    """物品入仓（背包 -> 仓库，按件占格；同种物品可存多件）。"""
    inv = getattr(player, "inventory", None)
    if not inv or item_id not in inv:
        return False
    if len(home.stash) >= stash_capacity(home):
        return False
    inv.remove(item_id)
    home.stash.append(item_id)
    return True


def stash_withdraw(home: Home, player, item_id: str) -> bool:
    """物品出仓（仓库 -> 背包）。[!] 必须走 try_add_to_inventory（负重校验，守 §22）。"""
    if item_id not in home.stash:
        return False
    if not ce.try_add_to_inventory(player, item_id):
        return False  # 背包满拒收（调用方提示先整理背包）
    home.stash.remove(item_id)
    return True


def shelf_put(home: Home, player, item_id: str) -> bool:
    """物品上陈列架（背包 -> 陈列，容量校验）。"""
    inv = getattr(player, "inventory", None)
    if not inv or item_id not in inv or item_id in home.display_shelf:
        return False
    if len(home.display_shelf) >= display_capacity(home):
        return False
    inv.remove(item_id)
    home.display_shelf.append(item_id)
    return True


def shelf_take(home: Home, player, item_id: str) -> bool:
    """取回陈列（陈列 -> 背包，走 try_add_to_inventory 守负重）。"""
    if item_id not in home.display_shelf:
        return False
    if not ce.try_add_to_inventory(player, item_id):
        return False
    home.display_shelf.remove(item_id)
    return True


# ---------------------------------------------------------------------------
# 家具购置 / 休憩
# ---------------------------------------------------------------------------
def buy_furniture(world: World, home: Home, entry: dict, slot: int = -1) -> tuple[bool, str]:
    """购置家具并摆上网格（题材池条目；持续金币 sink + 容量扩容）。同款可重复购买
    （储物/陈列按件数叠加扩容），每件占一格。slot=-1 自动找空位。返回 (ok, err)。
    """
    if not isinstance(entry, dict) or entry.get("kind") not in \
            ("storage", "bed", "decor", "display"):
        return False, "无效家具"
    ensure_home_grid(home)
    if slot < 0:
        slot = next((s for s in range(GRID_SLOTS) if slot_free(home, s)), -1)
        if slot < 0:
            return False, "宅内格子已满"
    elif not slot_free(home, slot):
        return False, "该格子已被占用"
    price = furniture_price(world, entry)
    p = world.player
    if int(p.gold) < price:
        return False, f"{GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {}).currency}不足"
    p.gold = int(p.gold) - price
    home.furniture.append({
        "name": str(entry.get("name", "") or ""),
        "kind": str(entry.get("kind", "")),
        "price": price,
        "slot": int(slot),
        "day": max(1, int(getattr(world, "day_count", 1) or 1)),
    })
    return True, ""


def remove_furniture(home: Home, slot: int) -> tuple[bool, str]:
    """[网格v2] 拆除指定格子的家具（容量随之缩；不退款）。返回 (ok, err)。"""
    f = furniture_at_slot(home, slot)
    if f is None:
        return False, "该格子没有家具"
    home.furniture.remove(f)
    return True, ""


def rest_at_home(world: World, home: Home) -> dict:
    """宅中休憩（纯 Python）：HP/MP 回满 + 跳到次日清晨 + 床铺小 XP。

    [!] 跳时 = tick 跳至下一个 12 倍数边界（相位/天数由 tick 派导：12 tick = 1 天，
    边界处 tick//3%4 == 0 即 dawn）；此处同步直写 time_phase/day_count，让 narrate
    （在 tick_world 之前跑）的【时间天气】上下文立即见晨——不直写则旁白仍写旧夜色。
    [!] 只跳时不额外跑滴答：tick_world 由场景回合照常在 narrate 后跑 1 次（新 tick 处），
    满足「休憩只跑 1 次滴答」防多轮 LLM 过载。返回 summary（apply_intent 汇总用）。
    """
    p = world.player
    p.hp = max(1, int(p.hp_max))
    p.mp = max(0, int(getattr(p, "mp_max", 0) or 0))
    tick = int(getattr(world, "tick_count", 0) or 0)
    new_tick = (tick // 12 + 1) * 12
    world.tick_count = new_tick
    world.time_phase = "dawn"
    world.day_count = new_tick // 12 + 1
    out = {"rested": True, "home": home.name, "day": world.day_count, "bed_xp": 0}
    has_bed = any(isinstance(f, dict) and f.get("kind") == "bed"
                  for f in (home.furniture or []))
    if has_bed:
        try:
            # [P60 cozy 2026-09-26] 舒适度加成床铺小 XP（陈列+家具撑起的居所气息）
            xp = int(_REST_BED_XP * (100 + home_comfort(home)) // 100)
            res = ce.gain_xp(p, xp)
            out["bed_xp"] = xp
            out["level_up"] = bool(getattr(res, "leveled_up", False))
        except Exception:
            pass
    out["comfort"] = home_comfort(home)
    return out


# ---------------------------------------------------------------------------
# [P34c] 住宅内建筑（带等级，门控锻造/炼制/洗练高 level 物品）
# ---------------------------------------------------------------------------
def building_catalog(world: World) -> list[dict]:
    """题材建筑目录（建造 UI 列表数据源；成交价按 building_price 折算）。
    [P35] 每题材 5 种：forge/alchemy/refine/study + garden 灵田（Lv1=2 块每级+1）。"""
    tid = (getattr(world, "config_overlay", None) or {}).get("attribute_template_id") \
        or "western_fantasy"
    return [dict(b) for b in (_GENRE_BUILDING_CATALOG.get(tid)
                              or _GENRE_BUILDING_CATALOG["western_fantasy"])]


def building_kind_name(world: World, kind: str) -> str:
    """建筑 kind（英文 key）-> 题材化中文名（缺键回退通用短标签）。"""
    for b in building_catalog(world):
        if b.get("kind") == kind:
            return str(b.get("name") or kind)
    _FALLBACK = {"forge": "锻造屋", "alchemy": "炼丹房", "refine": "洗练室",
                 "study": "书房", "garden": "灵田", "warehouse": "仓库"}
    return _FALLBACK.get(kind, kind)


def building_price(world: World, entry: dict, current_level: int = 0) -> int:
    """建筑建造/升级价 = 池基价 x economy_pace 买入倍率 x 聚落物价系数 x (1 + 0.5*当前等级)。

    升级每级 +50%（高 level 建筑更贵，金币 sink 递进）。current_level=0 即新建。
    """
    try:
        raw = int((entry or {}).get("base_price", 0) or 0)
    except (TypeError, ValueError):
        raw = 0
    # 聚落物价系数需 loc——building_price 不传 loc，用 1.0 基线（建筑价不随聚落波动，简化）
    mult = pace_factors(world_pace(world))[0] * (1.0 + 0.5 * max(0, int(current_level)))
    return max(1, int(round(raw * mult)))


def building_of(home: Home, kind: str) -> Optional[dict]:
    """取宅内指定 kind 的建筑 entry（无则 None）。每 kind 限一建筑（升级而非重复建）。"""
    for b in (home.buildings or []):
        if isinstance(b, dict) and b.get("kind") == kind:
            return b
    return None


def building_level(home: Home, kind: str) -> int:
    """宅内指定 kind 建筑的等级（无则 0）。crafting_engine.can_craft 门控用。"""
    b = building_of(home, kind)
    if b is None:
        return 0
    try:
        return max(0, int(b.get("level", 0) or 0))
    except (TypeError, ValueError):
        return 0


def has_building(home: Home, kind: str, min_level: int = 1) -> bool:
    """宅内是否有指定 kind 且 level >= min_level 的建筑。"""
    return building_level(home, kind) >= max(1, int(min_level))


# ---- [住宅 5x5 网格 2026-08-24] 格子占用 + 升级材料 spec ----
GRID_SIZE = 5                      # 5x5 固定网格
GRID_SLOTS = GRID_SIZE * GRID_SIZE  # 25 格
_RARITY = ("common", "uncommon", "rare", "epic", "legendary", "mythic")


def ensure_building_slots(home: Home) -> None:
    """[网格迁移] 老档建筑无 slot 时按序补 0..24（去重），幂等。新建造直接带 slot。"""
    used = {int(b.get("slot")) for b in (home.buildings or [])
            if isinstance(b, dict) and isinstance(b.get("slot"), int)
            and 0 <= int(b.get("slot")) < GRID_SLOTS}
    for b in (home.buildings or []):
        if not isinstance(b, dict):
            continue
        s = b.get("slot")
        if isinstance(s, int) and 0 <= s < GRID_SLOTS:
            continue
        slot = 0
        while slot in used:
            slot += 1
        if slot >= GRID_SLOTS:
            break
        b["slot"] = slot
        used.add(slot)


def building_at_slot(home: Home, slot: int) -> Optional[dict]:
    """取占用指定格子的建筑 entry（无则 None）。"""
    for b in (home.buildings or []):
        if isinstance(b, dict) and int(b.get("slot", -1)) == int(slot):
            return b
    return None


def furniture_at_slot(home: Home, slot: int) -> Optional[dict]:
    """[网格v2] 取占用指定格子的家具 entry（无则 None）。"""
    for f in (home.furniture or []):
        if isinstance(f, dict) and int(f.get("slot", -1)) == int(slot):
            return f
    return None


def entity_at_slot(home: Home, slot: int) -> tuple:
    """[网格v2] 格子占用统一查询：返回 ("building",b)/("furniture",f)/(None,None)。"""
    b = building_at_slot(home, slot)
    if b is not None:
        return "building", b
    f = furniture_at_slot(home, slot)
    if f is not None:
        return "furniture", f
    return None, None


def slot_free(home: Home, slot: int) -> bool:
    """格子是否空闲（在 0..24 内且无建筑/家具占用）。"""
    if not (0 <= int(slot) < GRID_SLOTS):
        return False
    typ, _ = entity_at_slot(home, slot)
    return typ is None


def ensure_furniture_slots(home: Home, used: set) -> None:
    """[网格v2] 老档家具无 slot 时按序补空位（避开 used 集合），幂等。"""
    for f in (home.furniture or []):
        if not isinstance(f, dict):
            continue
        s = f.get("slot")
        if isinstance(s, int) and 0 <= s < GRID_SLOTS and s not in used:
            used.add(s)
            continue
        slot = 0
        while slot in used:
            slot += 1
        if slot >= GRID_SLOTS:
            break
        f["slot"] = slot
        used.add(slot)


def ensure_home_grid(home: Home) -> None:
    """[网格v2] 网格迁移（grid_version==0 老档）：补建筑/家具 slot + 自动补仓库建筑。

    存量档（grid_version 缺省 0）自动补建 warehouse（level1）以免锁存量仓库物品；
    新档 buy_home 置 grid_version=1 不自动建（仓库需自建）。幂等。"""
    if int(getattr(home, "grid_version", 1) or 0) >= 1:
        return
    # 建筑 slot
    used = {int(b.get("slot")) for b in (home.buildings or [])
            if isinstance(b, dict) and isinstance(b.get("slot"), int)
            and 0 <= int(b.get("slot")) < GRID_SLOTS}
    ensure_building_slots(home)
    used = {int(b.get("slot")) for b in (home.buildings or [])
            if isinstance(b, dict) and isinstance(b.get("slot"), int)}
    # 存量档自动补仓库（不锁存量物品）
    if building_of(home, "warehouse") is None:
        slot = 0
        while slot in used:
            slot += 1
        if slot < GRID_SLOTS:
            home.buildings.append({"name": "仓库", "kind": "warehouse", "level": 1,
                                   "price": 0, "slot": slot,
                                   "day": max(1, int(getattr(home, "purchased_day", 1) or 1))})
            used.add(slot)
    # 家具 slot（避开建筑）
    ensure_furniture_slots(home, used)
    home.grid_version = 1


def _spec_for(world: World, kind: str, level: int) -> list:
    """取 (kind, level) 的升级材料清单。

    返回 [] 表示「无 spec 体系」（老档/未生成）-> 调用方回退旧 1forge+1craft。
    world.building_material_specs 非空（新世界已生成）时：优先 LLM 的具体材料
    （{item_id,name}），该 (kind,level) 缺失则代码按品级回退（{rarity,count}）。"""
    specs = getattr(world, "building_material_specs", None) or {}
    if not specs:
        return []  # 老档/建筑系统未生成 spec -> 走旧耗材口径，保兼容
    ks = specs.get(kind) or {}
    lst = ks.get(str(level)) or ks.get(level) or []
    if isinstance(lst, list) and lst:
        exact = [e for e in lst if isinstance(e, dict) and e.get("item_id")]
        if exact:
            return exact
    return _fallback_spec(level)


def _fallback_spec(level: int) -> list:
    """[回退] 代码按品级出「品级+数量」需求（不指定具体物品，玩家任意同品级材料可满足）。

    品级 mix：lv1=白x2+绿x1 / lv2=绿x2+蓝x1 / lv3=蓝x2+紫x1 / lv4=紫x2+橙x1 /
    lv5+=橙x2+红x1（钳顶）。确定性纯函数（无 rng——同 level 同需求）。"""
    top = len(_RARITY) - 1
    base = max(0, min(top, level - 1))
    hi = min(top, base + 1)
    return [{"rarity": _RARITY[base], "count": 2},
            {"rarity": _RARITY[hi], "count": 1}]


def _select_spec_materials(world: World, player, spec: list) -> tuple[list[str], str]:
    """只读挑选本次要扣的每件材料；同 id 堆叠和混合需求都按件分配。"""
    inv = getattr(player, "inventory", None)
    if not isinstance(inv, list):
        return [], "背包不可用"
    gt = GenreText(getattr(world, "config_overlay", None) or {})
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    remaining = list(inv)
    selected: list[str] = []
    # 具体物品优先占位，再从余下的实体里选指定品级，避免一件料同时满足两项。
    for e in spec:
        if e.get("item_id"):
            iid = str(e["item_id"])
            if iid not in remaining:
                return [], f"材料不足：缺「{e.get('name') or iid}」"
            remaining.remove(iid)
            selected.append(iid)
    for e in spec:
        if e.get("item_id"):
            continue
        rar = str(e.get("rarity", "common") or "common")
        try:
            cnt = max(1, int(e.get("count", 1)))
        except (TypeError, ValueError):
            cnt = 1
        have = [iid for iid in remaining
                if getattr(item_by_id.get(iid), "rarity", "") == rar
                and getattr(item_by_id.get(iid), "type", "") == "material"]
        if len(have) < cnt:
            return [], f"材料不足：需 {cnt} 件{gt.rarity(rar)}材料，背包现有 {len(have)} 件"
        for iid in have[:cnt]:
            remaining.remove(iid)
            selected.append(iid)
    return selected, ""


def _consume_spec_materials(world: World, player, spec: list) -> tuple[bool, str]:
    """先完整配齐，再逐件扣除；失败时背包保持原样。"""
    selected, err = _select_spec_materials(world, player, spec)
    if err:
        return False, err
    inv = player.inventory
    for iid in selected:
        inv.remove(iid)
    return True, ""


def building_material_status(world: World, kind: str, level: int) -> tuple[bool, str]:
    """住宅建造按钮与引擎共用的只读材料校验。"""
    spec = _spec_for(world, kind, level)
    if spec:
        _, err = _select_spec_materials(world, world.player, spec)
    else:
        _, err = _select_legacy_materials(world, world.player)
    return not err, err


def build_building(world: World, home: Home, entry: dict, preset=None,
                   slot: int = -1) -> tuple[bool, str]:
    """建造住宅内建筑（首次建造 level=1；扣金币 + 消耗升级材料 spec）。

    [!] 每 kind 限一建筑——已存在则调 upgrade_building 升级而非重复建。
    [!] 5x5 网格：slot 指定落位格子（0-24，须空闲；-1=自动找空位）。
    [!] 建造耗材：读 (kind,1) 材料 spec（LLM 生成/代码回退），无 spec 回退旧 1forge+1craft。
    """
    if not isinstance(entry, dict) or entry.get("kind") not in ("forge", "alchemy", "refine", "study", "garden", "warehouse"):
        return False, "无效建筑"
    if preset is not None and not getattr(preset, "buildings_enabled", True):
        return False, "建筑系统未开启"
    kind = str(entry.get("kind", ""))
    if building_of(home, kind) is not None:
        return False, "已有该建筑，请升级"
    # 等级上限门与升级侧同口径（buildings_max_level=0 禁建筑——新建也须拦，防只拦升级的半门控）
    _bm_raw = getattr(preset, "buildings_max_level", 5) if preset is not None else 5
    max_lvl = max(0, int(_bm_raw) if _bm_raw is not None else 5)  # [!] 0=禁建筑合法值勿吞；None 防御同升级侧
    if max_lvl < 1:
        return False, "建筑系统未开启"
    # 格子落位：-1 自动找空位；指定则须空闲
    ensure_home_grid(home)
    if slot < 0:
        slot = next((s for s in range(GRID_SLOTS) if slot_free(home, s)), -1)
        if slot < 0:
            return False, "宅内格子已满"
    elif not slot_free(home, slot):
        return False, "该格子已被占用"
    p = world.player
    # [I3 修复 2026-08-25] 金币校验提前到扣料之前（防「金币不足但材料已扣」的丢料路径）
    price = building_price(world, entry, 0)
    if int(p.gold) < price:
        return False, f"{GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {}).currency}不足"
    # 建造耗材：(kind,1) spec；无则回退旧 1forge+1craft
    spec = _spec_for(world, kind, 1)
    if spec:
        ok, err = _consume_spec_materials(world, p, spec)
        if not ok:
            return False, err
    else:
        ok, err = _consume_legacy_materials(world, p)
        if not ok:
            return False, err
    # 扣金币
    p.gold = int(p.gold) - price
    home.buildings.append({
        "name": str(entry.get("name", "") or ""),
        "kind": kind,
        "level": 1,
        "price": price,
        "slot": int(slot),
        "day": max(1, int(getattr(world, "day_count", 1) or 1)),
    })
    return True, ""


def _consume_legacy_materials(world: World, player) -> tuple[bool, str]:
    """旧建造/升级耗材：1 件 forge 材料 + 1 件 craft 材料（spec 缺失时回退）。"""
    selected, err = _select_legacy_materials(world, player)
    if err:
        return False, err
    for iid in selected:
        player.inventory.remove(iid)
    return True, ""


def _select_legacy_materials(world: World, player) -> tuple[list[str], str]:
    """旧口径材料只读选件，供 UI 预检与结算共用。"""
    inv = getattr(player, "inventory", None)
    if not isinstance(inv, list):
        return [], "背包不可用"
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    from src.models.world import effective_category
    forge_id = next((iid for iid in inv if effective_category(item_by_id.get(iid)) == "forge"), None)
    craft_id = next((iid for iid in inv if effective_category(item_by_id.get(iid)) == "craft"), None)
    if not forge_id or not craft_id:
        return [], "材料不足：需锻造类与制造类材料各 1 件"
    return [forge_id, craft_id], ""


def upgrade_building(world: World, home: Home, kind: str, preset=None) -> tuple[bool, str]:
    """升级住宅内建筑（level+1，钳 buildings_max_level；扣金币 + 消耗材料）。

    [!] 升级价 = building_price(当前等级)（每级 +50%）；耗材同建造（1 forge + 1 craft 材料）。
    """
    b = building_of(home, kind)
    if b is None:
        return False, "无该建筑"
    if preset is not None and not getattr(preset, "buildings_enabled", True):
        return False, "建筑系统未开启"
    if kind in ("study", "refine"):
        return False, "此建筑升级尚未开放"
    _bm_raw = getattr(preset, "buildings_max_level", 5) if preset is not None else 5
    max_lvl = max(0, int(_bm_raw) if _bm_raw is not None else 5)  # [!] 0=禁建筑合法值勿 or 复活
    cur_lvl = max(1, int(b.get("level", 1) or 1))
    if cur_lvl >= max_lvl:
        return False, "建筑已达最高等级"
    p = world.player
    # [I3 修复 2026-08-25] 金币校验提前到扣料之前（防「金币不足但材料已扣」的丢料路径）
    price = building_price(world, {"base_price": _catalog_base_price(world, kind)}, cur_lvl)
    if int(p.gold) < price:
        return False, f"{GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {}).currency}不足"
    # 升级耗材：(kind, cur+1) spec；无则回退旧 1forge+1craft
    spec = _spec_for(world, kind, cur_lvl + 1)
    if spec:
        ok, err = _consume_spec_materials(world, p, spec)
        if not ok:
            return False, err
    else:
        ok, err = _consume_legacy_materials(world, p)
        if not ok:
            return False, err
    p.gold = int(p.gold) - price
    b["level"] = cur_lvl + 1
    b["price"] = price
    b["day"] = max(1, int(getattr(world, "day_count", 1) or 1))
    return True, ""


def _catalog_base_price(world: World, kind: str) -> int:
    """从题材建筑目录取指定 kind 的 base_price（upgrade_building 算升级价用）。"""
    for b in building_catalog(world):
        if b.get("kind") == kind:
            try:
                return int(b.get("base_price", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


# ============ [P60 cozy + 宴请 2026-09-26] 舒适度与宅中设宴 ============

def home_comfort(home: Home) -> int:
    """宅邸舒适度（0-30 百分点）：陈列件数 x2 + 家具件数 x1，钳 30。

    陈列品撑场面（P48 宅邸陈列->据点客流的宅内镜像）+ 家具添置居所气息。
    消费点：休憩床铺 XP 加成（rest_at_home）+ 宴请宾客容量（host_banquet）。"""
    display_n = len(getattr(home, "display_shelf", None) or [])
    furn_n = len([f for f in (getattr(home, "furniture", None) or []) if isinstance(f, dict)])
    return min(30, display_n * 2 + furn_n)


_BANQUET_COST_BASE = 120       # 设宴基础花销（另按宅邸档位每档 +80）
_BANQUET_COST_PER_TIER = 80
_BANQUET_COOLDOWN_DAYS = 5     # 设宴冷却（防天天摆酒刷交情）
_BANQUET_AFFINITY_MIN = 25     # 赴宴门槛（交情 >=25 的同聚落熟识）
_BANQUET_AFFINITY_GAIN = 4


def banquet_cost(home: Home) -> int:
    return _BANQUET_COST_BASE + max(0, int(getattr(home, "tier", 1) or 1) - 1) * _BANQUET_COST_PER_TIER


def _banquet_candidates(world: World, home: Home) -> list:
    """同地、存活、非敌对且交情足够的熟识；预检与实际赴宴同口径。"""
    return [n for n in (getattr(world, "npcs", None) or [])
            if getattr(n, "alive", True)
            and str(getattr(n, "location_id", "") or "") == str(home.location_id)
            and not getattr(n, "hostile", False)
            and int(getattr(n, "affinity", 0) or 0) >= _BANQUET_AFFINITY_MIN]


def can_host_banquet(world: World, home: Home) -> tuple[bool, str]:
    """设宴前置：冷却、金币和至少一位愿赴宴的熟识。"""
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    last = int(getattr(home, "last_banquet_day", 0) or 0)
    if last > 0 and day - last < _BANQUET_COOLDOWN_DAYS:
        return False, f"近日才设过宴（还差 {_BANQUET_COOLDOWN_DAYS - (day - last)} 天）"
    cost = banquet_cost(home)
    if int(getattr(world.player, "gold", 0) or 0) < cost:
        return False, f"身上的钱不够（需 {cost}，持 {int(getattr(world.player, 'gold', 0) or 0)}）"
    if not _banquet_candidates(world, home):
        return False, "这座聚落里没有愿意赴宴的熟识（交情 >=25 的在场者）"
    return True, ""


def host_banquet(world: World, home: Home) -> dict:
    """[P60 宴请 2026-09-26] 宅中设宴（纯引擎零 LLM，金币 sink + 社交收益）。

    花费 banquet_cost；宾客 = 同聚落存活、非敌对、交情 >=25（cap 4 + 舒适度//15，
    交情降序确定性取前）。每人交情 +4 + 亲眼见证记「慷慨」印象；宅邸所在势力声望 +1
    （无主之地跳过）。产 1 条 minor 事件进动态栏。返回 {ok, msg, guests:[名]}。"""
    from src.models.world import WorldEvent as _WE
    ok, reason = can_host_banquet(world, home)
    if not ok:
        return {"ok": False, "msg": reason}
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    comfort = home_comfort(home)
    cap = 4 + comfort // 15
    guests = _banquet_candidates(world, home)
    guests.sort(key=lambda n: int(getattr(n, "affinity", 0) or 0), reverse=True)
    guests = guests[:cap]
    cost = banquet_cost(home)
    world.player.gold = int(getattr(world.player, "gold", 0) or 0) - cost
    home.last_banquet_day = day
    for n in guests:
        nre.add_affinity(n, _BANQUET_AFFINITY_GAIN)
        soc.update_impression(n, "慷慨", 2)
    loc = next((l for l in (getattr(world, "locations", None) or [])
                if l.id == home.location_id), None)
    fid = str(getattr(loc, "faction_id", "") or "") if loc is not None else ""
    rep_note = ""
    if fid:
        rep = getattr(world.player, "reputation", None) or {}
        if isinstance(rep, dict) and fid in rep:
            rep[fid] = max(-100, min(100, int(rep[fid] or 0) + 1))
            world.player.reputation = rep
            rep_note = "（该势力声望 +1）"
    names = "、".join(str(getattr(n, "name", "?")) for n in guests)
    world.event_log.append(_WE(
        tick=int(getattr(world, "tick_count", 0) or 0), category="npc", severity="minor",
        title=f"「{getattr(home, 'name', '') or '宅邸'}」宅中设宴",
        desc=f"你在{home.name}设下宴席（{cost} 花销），{names}等 {len(guests)} 人前来赴宴"
             "——宾主尽欢，交情更进了一步。",
        locations=[home.location_id], npcs=[n.id for n in guests]))
    return {"ok": True, "msg": f"宴席圆满——{names}等 {len(guests)} 人赴宴，"
                               f"每人交情 +{_BANQUET_AFFINITY_GAIN}、记「慷慨」印象{rep_note}",
            "guests": [str(getattr(n, "name", "?")) for n in guests]}
