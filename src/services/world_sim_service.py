"""世界模拟服务（独立 SLG 系统）。

复用底层基础设施（LlmClient / ComfyuiService / DanbooruService / Storage），不自建一套。
三层职责（守设计铁律：LLM 出语义，引擎算结构）：
  1. 结算 LLM：世界骨架生成（JSON，schema 校验 + 重试兜底）
  2. 引擎层：build_world_from_skeleton 分配 uuid、关联 location/npc/faction、拷贝 config_overlay
  3. 文生图：danbooru 中文->英文 tag + comfyui 出图，按 preset 勾选项批量生成

v1 仅含世界生成（Phase 1）。场景交互（P2）/CRPG 数值（P3）/世界滴答（P4）后续扩展。
Phase 2/3/4 已实现：场景交互循环 + CRPG 数值 + 世界滴答（经济/势力战/离屏NPC/校准/事件日志）。
"""
from __future__ import annotations
import json
import os
import uuid
import re
from dataclasses import replace
from typing import Callable, Optional

from src.config import paths
from src.models import (
    ApiConfig, Preset, WorldSimPreset,
    World, WorldLore, Faction, Location, NPC, Item, Quest, PlayerState, WorldEvent,
    SceneLog,
    Shop, ShopStockEntry,
    ResourceNode,
    Recipe,
    Pet,
    Place,
    AuctionEvent,
    reputation_title,
    CUTOUT_BG_COLORS,
    DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT,
    DEFAULT_WORLDSIM_NARRATIVE_SYSTEM_PROMPT,
    DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT,
    DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT,
    DEFAULT_STOCK_MARKET_PROMPT,
    DEFAULT_AUCTION_GENERATE_PROMPT,
    WORLDSIM_NARRATIVE_IMG_REQ, WORLDSIM_NARRATIVE_IMG_TAIL,
)
from src.models.world import (_clean_elements, WorldBoss, _CONDITION_VALUES,
                              _TARGET_PATTERN_VALUES, StoryArc, StoryStage)
from src.services.llm_client import LlmClient, LlmUsage
from src.services.storage import Storage
from src.services import combat_engine as ce
from src.services import farm_engine as fe
from src.services import npc_life_engine as nle
from src.services import world_tick_engine as wte
from src.services import trade_engine as tre
from src.services import gather_engine as ge
from src.services import quest_engine as qe
from src.services import encounter_engine as ene
from src.services import talent_engine as te
from src.services import npc_reaction_engine as nre
from src.services import wilderness_engine as we
from src.services import home_engine as he
from src.services import calendar_engine as cale
from src.services import pet_engine as pex
from src.services import dungeon_engine as dge
from src.services import world_boss_engine as wbe
from src.services import relation_engine as reng
from src.services import stock_engine as ske
from src.services import auction_engine as aue
from src.services import outreach_engine as ore
from src.services import social_engine as soc
from src.services import name_resolver as nrs
from src.services import commission_engine as come
from src.services import chronicle_engine as che
from src.services import rumor_engine as rme
from src.services import domain_engine as de
from src.services import story_arc_engine as sae
from src.services import consequence_engine as cue
from src.utils.rng import SeededRng
from src.utils.debug import debug_log
from src.utils.helpers import parse_image_tags


# 世界骨架 JSON 必须包含的顶层键（缺则兜底空列表/空 dict）
_SK_REQUIRED_KEYS = ("worldbook", "factions", "locations", "npcs", "items", "recipes", "monsters")

# [修 2026-10-02 真机 OOC·货币串味] 外来货币禁词表（提示词禁令用；模块常量防
# test_currency_i18n 的 f-string 裸币种静态守护误报——「提及以禁」非「使用」）。
_FOREIGN_CURRENCY_WORDS = ("瓶盖", "金币", "银两", "灵石", "信用点")

# 意图 JSON 合法 intent_type（结算引擎校验用，防 LLM 输出脏值）
_INTENT_TYPE_VALUES = (
    "move", "talk", "observe", "interact", "use_item", "combat", "gather", "quest", "wait", "custom",
    "adventure",  # [P8] 奇遇/机缘（手动探测按钮触发；自动触发在 move 首次进入地点时内联）
    "gift",       # [修 2026-09-06 审计] 自由文本送礼引擎化（apply_player_gift 真结算）
    "trade",      # [P28] 交易识别（buy/sell 商人 + barter 任意 NPC 以物易物）
    "stock_buy",  "stock_sell",   # [B方案 2026-08-28] 交易所股市自由文本路由（stock_engine 结算）
)


# ---- [2026-09-25 用户指示] 人像/怪物图「纯色背景 -> 代码抠透明」 ----
# 背景色白名单在 models.world_sim_preset.CUTOUT_BG_COLORS（模型层约束，UI/服务共用）；
# LLM 加工输出里可能自带的背景 tag：抠图模式下先剥离，再统一追加纯色背景
# （否则「gradient background」这类会与抠图冲突；`_` 已在上游转空格，故同时容 `[ _]`）
_BG_TAG_RE = re.compile(
    r"\b(?:(?:dark|light|deep|pale|pastel|bright|off)[ -])?"
    r"(?:no|simple|solid|plain|white|black|grey|gray|blue|green|red|pink|purple|yellow|"
    r"orange|brown|gradient|blurry|detailed|complex|abstract|colorful|dark|light)[ _]background\b",
    re.IGNORECASE)
_SCENERY_TAG_RE = re.compile(r"\bscenery\b", re.IGNORECASE)
# 抠图模式下追加的负面词（压住复杂背景；与纯色背景正面 tag 同向）
_CUTOUT_NEGATIVE = ("complex background, detailed background, scenery, "
                    "gradient background, blurry background")
_FULL_BODY_POSITIVE = "full body, head to toe, feet visible, entire subject in frame"
_FULL_BODY_NEGATIVE = "cropped, out of frame, close-up, upper body, bust shot, cut off feet, cut off head"
_CROPPED_FRAMING_RE = re.compile(
    r"(?i)\b(?:upper[ _]body|cowboy[ _]shot|bust[ _]shot|headshot|close[ -]up|portrait|"
    r"feet[ _]out[ _]of[ _]frame)\b")


def _strip_background_tags(text: str) -> str:
    """剥离文本里的背景类 tag（抠图模式下改用代码指定的纯色背景）。

    可选前修饰词一并吃掉（`dark red background` 不会留下孤立的 `dark`）。
    """
    out = _BG_TAG_RE.sub("", text or "")
    out = _SCENERY_TAG_RE.sub("", out)
    out = re.sub(r"\s*,(?:\s*,)+", ",", out)      # 叠逗号
    out = re.sub(r"[ \t]*,[ \t]*", ", ", out)     # 统一逗号后空格
    return re.sub(r"^[\s,]+|[\s,]+$", "", out)


def _merge_negative(negative: str, extra: str) -> str:
    """追加负面词（剥尾部空白与逗号，防拼出 `,,`）。"""
    n = re.sub(r"[\s,]+$", "", negative or "")
    return f"{n}, {extra}" if n else (extra or "")


def _full_body_tags(positive: str) -> str:
    """消除半身构图词并在最终正向词末尾强制全身，避免加工 LLM 漏掉中文要求。"""
    base = _CROPPED_FRAMING_RE.sub("", positive or "")
    base = re.sub(r"\s*,(?:\s*,)+", ",", base).strip(" ,")
    return f"{base}, {_FULL_BODY_POSITIVE}" if base else _FULL_BODY_POSITIVE


# [审核修复 2026-09-13] 传闻浅印象按 category 分流：{rumor.category: (印象标签, trust 增量)}。
# 标签必须属于 models.world._IMPRESSION_TAG_VALUES（8 值白名单）；未列出的类别回退「危险」。
# 只收语义明确、不会误读的一类——宁可回退，也不给玩家乱贴标签。
_RUMOR_IMPRESSION = {
    "economy": ("富有", 2),        # 囤粮/炒股/生意上的风声 -> 「这人有钱」
    "faction_war": ("危险", -3),   # 卷入战事 -> 「这人惹不起」
}

# [审核修复 2026-09-13] 传闻浅印象细分规则表：(关键词元组, 印象标签, trust 增量)，命中即停。
# 匹配对象是传闻的 title（引擎拼的固定模板，未经 LLM 改写，如 f"{a.name}与{b.name}火并"）
# 与 text（LLM 口述化后的正文）——**匹配的是引擎自己的模板，不是猜 LLM 自由文本**，
# 故安全可维护；这也是比「给 WorldEvent 加结构化 kind 字段」更轻的等价做法。
# 标签必须属于 models.world._IMPRESSION_TAG_VALUES（8 值）。顺序：越具体越靠前。
_RUMOR_IMPRESSION_RULES = (
    (("火并", "仇家", "结下梁子", "拔刀相向", "恩断义绝"), "危险", -5),
    (("违约", "赖账", "失约", "背弃"), "负信", -4),
    (("履约", "如约", "交差", "交付"), "守信", 3),
    (("赠礼", "接济", "赈", "慷慨"), "慷慨", 4),
    (("和解", "结交", "深交", "挚友"), "仁善", 3),
    (("豪掷", "购下", "置下", "盘下", "拍得", "富甲一方"), "富有", 2),
    (("击败", "伏诛", "斩杀", "讨伐"), "无畏", 4),
    (("失守", "落败", "倾家", "破产", "砸掠"), "落魄", -2),
    (("夺取", "交战", "突袭", "围城"), "危险", -3),
    # [执念深化 2026-09-26] 达成毕生所愿的传闻正向印象（title 保留原文「毕生所愿达成·X」
    # 按 kind 命中；否则落「危险」-3 兜底——实现执念反而被当成恶徒）
    (("登峰造极", "百战之人", "踏遍山河", "名动一方"), "无畏", 3),
)


def _impression_for_rumor(rumor: dict) -> tuple:
    """[审核修复 2026-09-13] 一条传闻 -> (印象标签, trust 增量)。

    先按 _RUMOR_IMPRESSION_RULES 匹配（title + text，命中即停），未命中再按 category
    兜底（_RUMOR_IMPRESSION），都没有则回退「危险」-3（旧行为）。
    浅印象只影响没亲眼见过玩家的 NPC（_tick_impressions 已过滤），且道听途说永远浅。
    """
    blob = str(rumor.get("title", "") or "") + str(rumor.get("text", "") or "")
    for keys, tag, delta in _RUMOR_IMPRESSION_RULES:
        if any(k in blob for k in keys):
            return tag, delta
    return _RUMOR_IMPRESSION.get(str(rumor.get("category", "") or ""), ("危险", -3))


def _npc_by_name(world, name, npcs=None, alive_only: bool = False):
    """[P44 单一来源 2026-09-13] 按 LLM 输出的名字取 NPC（所有消费点一律走这里）。

    候选集锚定真实名单（解析不到即 None，绝不追认虚构名字）+ `nrs.resolve_name` 四层容错
    （精确 / 归一化 / 字符集等值 / 双向子串 + 编辑距离）。

    散写 `n.name == name` 精确等值是最容易漏的一类坑：LLM 给目标加头衔或一字之差
    （「捕头鲁大锤」「妖王大人」）就会整回合**无声失败**。送礼路径曾漏改一次
    （2026-09-13 审查 P1），此处收口以防下一个漏网。
    """
    pool = list(npcs) if npcs is not None else list(getattr(world, "npcs", None) or [])
    if alive_only:
        pool = [n for n in pool if getattr(n, "alive", True)]
    if not name or not pool:
        return None
    hit = nrs.resolve_name(str(name), [n.name for n in pool])
    if not hit:
        return None
    return next((n for n in pool if n.name == hit), None)

# ---- [P7g] 题材化资源点模板 + 采集动词 ----
# 每个题材承载典型资源点（名/类型/描述/驱动属性/所需工具 key）+ 采集动词。
# build_world 末尾 _init_resource_nodes 据 attribute_template_id 选模板挂载到地点；
# drops 由引擎从 world.items 池确定性挑选（不由 LLM 出），守数值范式铁律。
_GENRE_RESOURCE_TEMPLATES = {
    "xianxia": {
        "verb": "采集",
        "nodes": [
            {"name": "灵药田", "type": "herb", "desc": "几株散发淡淡灵气的草药在风中摇曳", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "灵矿脉", "type": "mine", "desc": "岩壁上嵌着点点灵矿，隐隐泛光", "stat_used": "str", "requires_tool": "mine"},
            {"name": "灵木林", "type": "wood", "desc": "古木参天，灵气氤氲其间", "stat_used": "str", "requires_tool": "wood"},
            {"name": "灵泉", "type": "water", "desc": "一汪清冽的灵泉，水面浮着灵雾", "stat_used": "int", "requires_tool": ""},
            {"name": "崖生石乳洞", "type": "mine", "desc": "洞顶垂着乳白钟乳，偶见灵乳凝珠", "stat_used": "str", "requires_tool": "mine"},
            {"name": "腐殖灵泥沼", "type": "herb", "desc": "沃土上生着成片喜阴的灵草", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "雷击木桩", "type": "wood", "desc": "遭雷火劈焦的古木，木心犹存雷意", "stat_used": "str", "requires_tool": "wood"},
            {"name": "聚灵阵残眼", "type": "energy", "desc": "废弃聚灵阵的阵眼仍渗着游离灵气", "stat_used": "int", "requires_tool": "energy"},
            {"name": "灵蚕桑林", "type": "herb", "desc": "桑林里灵蚕吐丝，桑叶可入药", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "寒潭灵水", "type": "water", "desc": "深潭寒水澄澈，隐有灵性", "stat_used": "int", "requires_tool": ""},
            {"name": "星陨铁坑", "type": "mine", "desc": "星陨铁坠地砸出的矿坑，坑壁泛乌光", "stat_used": "str", "requires_tool": "mine"},
            {"name": "灵雾沼", "type": "energy", "desc": "终年灵雾弥漫的沼地，游离灵气浓郁", "stat_used": "int", "requires_tool": "energy"},
        ],
    },
    "wuxia": {
        "verb": "采撷",
        "nodes": [
            {"name": "草药丛", "type": "herb", "desc": "山间野生的草药丛", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "铁矿山", "type": "mine", "desc": "裸露的铁矿层", "stat_used": "str", "requires_tool": "mine"},
            {"name": "古树林", "type": "wood", "desc": "一片可伐的古树", "stat_used": "str", "requires_tool": "wood"},
            {"name": "山泉", "type": "water", "desc": "清冽的山泉", "stat_used": "int", "requires_tool": ""},
            {"name": "断崖蜜巢", "type": "herb", "desc": "崖壁野蜂酿的蜜脾，可入药可充饥", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "河滩砂金", "type": "mine", "desc": "淘洗河沙或能得几粒砂金", "stat_used": "str", "requires_tool": "mine"},
            {"name": "竹林", "type": "wood", "desc": "一片可伐的毛竹，竹材韧而轻", "stat_used": "str", "requires_tool": "wood"},
            {"name": "荒祠供桌", "type": "scavenge", "desc": "无人荒祠的供桌上或有遗留之物", "stat_used": "dex", "requires_tool": ""},
            {"name": "药王谷", "type": "herb", "desc": "谷中遍生珍稀药草", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "铜矿山", "type": "mine", "desc": "山体裸露的铜矿层", "stat_used": "str", "requires_tool": "mine"},
            {"name": "松柏林", "type": "wood", "desc": "成片可伐的松柏林", "stat_used": "str", "requires_tool": "wood"},
            {"name": "断桥遗物", "type": "scavenge", "desc": "断桥下的货箱散落着遗留之物", "stat_used": "dex", "requires_tool": ""},
        ],
    },
    "modern": {
        "verb": "翻找",
        "nodes": [
            {"name": "厨房水池", "type": "water", "desc": "可以洗洗手、取点水", "stat_used": "dex", "requires_tool": ""},
            {"name": "冰箱", "type": "scavenge", "desc": "嗡嗡作响的冰箱，或许还藏着食物", "stat_used": "int", "requires_tool": ""},
            {"name": "药箱", "type": "scavenge", "desc": "一个落灰的急救药箱", "stat_used": "int", "requires_tool": ""},
            {"name": "储物柜", "type": "scavenge", "desc": "塞满杂物的储物柜", "stat_used": "dex", "requires_tool": ""},
            {"name": "快递驿站货架", "type": "scavenge", "desc": "无人认领的包裹堆里或有可用之物", "stat_used": "int", "requires_tool": ""},
            {"name": "报废车辆", "type": "scavenge", "desc": "路边报废车里有拆得动的零件", "stat_used": "str", "requires_tool": ""},
            {"name": "自动售货机", "type": "scavenge", "desc": "断电的售货机，撬开能捡到存货", "stat_used": "str", "requires_tool": ""},
            {"name": "楼顶水箱", "type": "water", "desc": "楼顶水箱还存着半箱水", "stat_used": "dex", "requires_tool": ""},
            {"name": "社区菜园", "type": "herb", "desc": "居民自留的菜园，能摘点蔬果", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "工地材料堆", "type": "mine", "desc": "工地的建材堆，有钢筋木料", "stat_used": "str", "requires_tool": "mine"},
            {"name": "公园树林", "type": "wood", "desc": "街心公园的小树林，可捡柴", "stat_used": "str", "requires_tool": "wood"},
            {"name": "废品回收站", "type": "scavenge", "desc": "成堆的废品里能淘出可用之物", "stat_used": "dex", "requires_tool": ""},
        ],
    },
    "scifi": {
        "verb": "采集",
        "nodes": [
            {"name": "能量节点", "type": "energy", "desc": "闪烁的能量结晶体", "stat_used": "int", "requires_tool": "energy"},
            {"name": "废料堆", "type": "scavenge", "desc": "堆积的工业废料，或有可用零件", "stat_used": "dex", "requires_tool": ""},
            {"name": "数据终端", "type": "scavenge", "desc": "一台可解析的数据终端", "stat_used": "int", "requires_tool": ""},
            {"name": "冷却水循环口", "type": "water", "desc": "舰体冷却循环口，可抽取冷凝水", "stat_used": "int", "requires_tool": ""},
            {"name": "培养舱残株", "type": "herb", "desc": "生态舱里幸存的药用培养株", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "陨矿露头", "type": "mine", "desc": "小行星地表裸露的陨铁矿", "stat_used": "str", "requires_tool": "mine"},
            {"name": "合金桁架", "type": "wood", "desc": "废弃舱段拆下的轻质合金桁架", "stat_used": "str", "requires_tool": "wood"},
            {"name": "备件柜", "type": "scavenge", "desc": "维修通道旁的备件柜，抽屉半开", "stat_used": "dex", "requires_tool": ""},
            {"name": "聚变燃料槽", "type": "energy", "desc": "反应堆外接的燃料槽，能量充沛", "stat_used": "int", "requires_tool": "energy"},
            {"name": "冷冻储水舱", "type": "water", "desc": "舱段冷冻储水舱，可化取净水", "stat_used": "int", "requires_tool": ""},
            {"name": "生境舱菌圃", "type": "herb", "desc": "生境舱里的药用菌圃", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "飞船残骸", "type": "scavenge", "desc": "坠毁飞船的残骸，可拆零件", "stat_used": "str", "requires_tool": ""},
        ],
    },
    "apocalypse": {
        "verb": "搜刮",
        "nodes": [
            {"name": "废墟堆", "type": "scavenge", "desc": "坍塌的废墟，可能藏着物资", "stat_used": "dex", "requires_tool": ""},
            {"name": "变异植物", "type": "herb", "desc": "扭曲生长的变异植物", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "残存物资箱", "type": "scavenge", "desc": "一个落灰的物资箱", "stat_used": "int", "requires_tool": ""},
            {"name": "雨水收集桶", "type": "water", "desc": "接雨水的破桶，沉淀后勉强能喝", "stat_used": "int", "requires_tool": ""},
            {"name": "锈蚀车场", "type": "scavenge", "desc": "成排锈死的车，油箱和零件可拆", "stat_used": "str", "requires_tool": ""},
            {"name": "变异菌毯", "type": "herb", "desc": "墙角蔓延的荧光菌毯，可入药", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "断裂钢筋丛", "type": "wood", "desc": "塌楼里伸出的一丛钢筋，可截取", "stat_used": "str", "requires_tool": "wood"},
            {"name": "超市废库房", "type": "scavenge", "desc": "超市塌了一半的库房，货架翻倒", "stat_used": "dex", "requires_tool": ""},
            {"name": "加油站地罐", "type": "energy", "desc": "加油站地下油罐，尚存燃油", "stat_used": "int", "requires_tool": "energy"},
            {"name": "河床滤水坑", "type": "water", "desc": "河床挖的滤水坑，水已澄清", "stat_used": "int", "requires_tool": ""},
            {"name": "变异藤蔓", "type": "wood", "desc": "疯长的变异藤蔓，可砍作柴薪", "stat_used": "str", "requires_tool": "wood"},
            {"name": "军械库残间", "type": "scavenge", "desc": "倒塌的军械库，或藏弹药", "stat_used": "int", "requires_tool": ""},
        ],
    },
    "western_fantasy": {
        "verb": "采集",
        "nodes": [
            {"name": "草药丛", "type": "herb", "desc": "生长着几株药草", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "矿脉", "type": "mine", "desc": "裸露的矿脉", "stat_used": "str", "requires_tool": "mine"},
            {"name": "古木林", "type": "wood", "desc": "一片古树林", "stat_used": "str", "requires_tool": "wood"},
            {"name": "清泉", "type": "water", "desc": "一汪清澈的泉水", "stat_used": "int", "requires_tool": ""},
            {"name": "苔洞蘑菇圈", "type": "herb", "desc": "阴湿苔洞里生着可食用的野菇", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "河床砾金", "type": "mine", "desc": "枯水河床的砾石间可淘出砂金", "stat_used": "str", "requires_tool": "mine"},
            {"name": "雷劈枯木", "type": "wood", "desc": "雷劈后枯死的硬木，好烧也好雕", "stat_used": "str", "requires_tool": "wood"},
            {"name": "旅人遗物堆", "type": "scavenge", "desc": "岔路口堆着前人丢弃的行囊残物", "stat_used": "dex", "requires_tool": ""},
            {"name": "蜂蜜树洞", "type": "herb", "desc": "古树洞里的野蜂巢，可采蜜蜡", "stat_used": "dex", "requires_tool": "herb"},
            {"name": "银矿露头", "type": "mine", "desc": "山体露出的银矿岩层", "stat_used": "str", "requires_tool": "mine"},
            {"name": "橡木林", "type": "wood", "desc": "成片的橡木林，木质坚硬", "stat_used": "str", "requires_tool": "wood"},
            {"name": "圣水溪", "type": "water", "desc": "流过教堂废墟的溪水，微有灵光", "stat_used": "int", "requires_tool": ""},
        ],
    },
}

# ---- [用户指示 2026-08-25] 物品属性 LLM 全权产出：品级系数/基值单一真相源 ----
# 提示词范围表(_item_attr_ranges_text)与 _fill_item_defaults 兜底共用同套常量，
# 改这里两边同步变（勿在提示词里手写数字）。
_RARITY_MULT = {"common": 1.0, "uncommon": 1.3, "rare": 1.7, "epic": 2.2, "legendary": 3.0, "mythic": 4.0}
# [用户指示 2026-09-06] NPC 初始携带（生成时随身+初始装备）稀有度封顶 epic（紫色）：
# 橙/红宝物不随世界生而入 NPC 之手（死亡掉落/搜刮会直接喂给玩家，开局即神装）；
# NPC 在游玩中经掉落/合成/购买获得橙红不受限（走各引擎正常路径，此处不拦）。
_RARITY_RANK = {r: i for i, r in enumerate(_RARITY_MULT)}
_NPC_START_RARITY_CAP = "epic"
# [修 2026-10-01] 怪物掉落品级锚定：danger 中值 -> 允许的掉落物品品级
_ALL_RARITIES = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
_MONSTER_LOOT_RARITY = {
    1: ("common", "uncommon"), 2: ("common", "uncommon"),
    3: ("uncommon", "rare"),   4: ("uncommon", "rare"),
    5: ("rare",),              6: ("rare",),
    7: ("rare", "epic"),       8: ("rare", "epic"),
    9: ("epic", "legendary"),  10: ("epic", "legendary"),
}

# [要角补档 2026-09-12] 剧情要角（is_key_npc）判据之一：role 命中这些关键词。
# 提到模块级是为了让 build_world_from_skeleton（等级派生，须早于 _init_combat_stats）
# 与 _init_world_tick_state（原标记处）共用同一张表，避免两处漂移。
_KEY_NPC_ROLES = ("首领", "boss", "Boss", "BOSS", "王", "将军", "市长", "族长",
                  "盟主", "长老", "领主", "主教", "大师", "主席")

# [R0/B02-F08 2026-09-30] 秘境内「深入」语义白名单：move_to 含这些词才推进房间探索
# （场景块教 LLM 的规范词是「深入」）；具名未知目的地（编造的房间/方向名）一律受阻
# 不消耗房间内容。出口不受此限——移动到入口名命中相邻地点走正常 move。
_DUNGEON_DEEP_WORDS = ("深入", "深入探索", "继续深入", "继续探索", "向深处前进", "探索", "前进", "深处", "下一", "下一间")


def _npc_start_item_ok(it) -> bool:
    """NPC 初始携带物品是否合法（稀有度 <= epic）。"""
    return _RARITY_RANK.get(str(getattr(it, "rarity", "common") or "common"), 0)         <= _RARITY_RANK[_NPC_START_RARITY_CAP]


_ITEM_BASE_ATTACK = 8     # weapon attack 基值 x 品级系数
_ITEM_BASE_DEFENSE = 5    # armor defense 基值
_ITEM_BASE_HEAL = 30      # consumable 回血基值
_JITTER_LO, _JITTER_HI = 0.8, 1.2   # 兜底抖动区间（与范围表口径一致）
_EQUIP_RARITY_LEVELS = {"common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5, "mythic": 6}

# [数值对齐 2026-09-06] 锻造/制造材料的 rarity 档种子价（引擎注入的 16 件功能材料用，
# 价格护栏豁免 category 非空物品所以不被品级曲线压平；用途定价含稀缺性溢价）。
_FORGE_MAT_PRICE = {"common": 30, "uncommon": 80, "rare": 200, "epic": 500, "legendary": 1200}
_CRAFT_MAT_PRICE = {"common": 25, "uncommon": 65, "rare": 170, "epic": 420, "legendary": 1000}


def _item_attr_ranges_text(stat_dims_hint: str = "") -> str:
    """[用户指示 2026-08-25] 从引擎同套常量生成「品级 x 属性范围」提示词文本。

    物品数值由 LLM 按此自填；_fill_item_defaults 仅对 0 值兜底（非 0 一律照用，
    守「物品属性全部按 LLM 给的来」）。数值由本函数从常量推导，勿手写。
    stat_dims_hint：题材装备偏好维度说明（地图拓展侧模板已知时注入）。
    """
    def _rng(base: float, mult: float) -> str:
        lo = max(1, int(base * mult * _JITTER_LO))
        hi = max(lo, int(base * mult * _JITTER_HI))
        return f"{lo}-{hi}"

    def _max(a, b):
        return a if a > b else b

    def _min(a, b):
        return a if a < b else b

    lines = []
    for r, m in _RARITY_MULT.items():
        # [百分比回血 2026-09-01] 回血改百分比档（_HEAL_PCT_BY_RARITY 同源）
        _pct = _HEAL_PCT_BY_RARITY.get(r, 10)
        lines.append(f"- {r}(x{m:g})：武器攻 {_rng(_ITEM_BASE_ATTACK, m)}｜防具防 {_rng(_ITEM_BASE_DEFENSE, m)}"
                     f"｜回血 heal_pct {_max(5, _pct - 4)}-{_min(100, _pct + 6)}%｜装备等级 {_EQUIP_RARITY_LEVELS[r]}")
    tip = (f"本题材装备偏好维度：{stat_dims_hint}。" if stat_dims_hint else "")
    return (
        "【物品属性范围表】物品数值由你按品级自填（系统仅对填 0 的字段兜底，非 0 一律照用）：\n"
        + "\n".join(lines)
        + f"\nstat_bonus（装备可选）：key 限 str/dex/int/vit/luk，选 1-2 维贴合物品设定，"
          f"每维数值约 1-2 x 品级系数（最低 1 封顶 8）。{tip}"
        "\nconsume_effect（消耗品可选：只给回蓝/五维/解毒/复活/食物——stat_bonus 每维 1-3"
        "（rare 1 维/epic 2 维/legendary+ 3 维）；heal_mp（回蓝）的 amount"
        " 一律填百分数 10-100，意义是「回复最大灵力的百分比」；"
        "填绝对数值（如「回复 30 点」）是错误填法；cure/revive 无参数。纯回血不填"
        " consume_effect、只填 heal_pct 顶层字段（与上表回血档同源：common 约 10-16、"
        "uncommon 约 15-21、rare 约 22-28、epic 约 30-36、legendary+ 约 40-50）。"
        "material/key 类不填数值。"
    )


# [防数值失控] 攻/防/回血的绝对上限：取最高品级（mythic）区间上限的 2 倍作慷慨护栏——
# 只拦「几百攻/几百回血」的明显越界，不误伤略高于品级区的合法值（如 15 攻的 common 武器）。
_ATTACK_CEIL = int(_ITEM_BASE_ATTACK * _RARITY_MULT["mythic"] * _JITTER_HI) * 2
_DEFENSE_CEIL = int(_ITEM_BASE_DEFENSE * _RARITY_MULT["mythic"] * _JITTER_HI) * 2
_HEAL_CEIL = int(_ITEM_BASE_HEAL * _RARITY_MULT["mythic"] * _JITTER_HI) * 2

# [百分比回血 2026-09-01] 旧整数回血换算基准：按「典型中期档玩家最大生命 ~250」
# 归一（30 回血 ≈ 12%）。品级阶梯同步映射到百分比档（common 10% -> legendary+ 45%）。
_HEAL_NORM_HP = 250
_HEAL_PCT_BY_RARITY = {"common": 10, "uncommon": 15, "rare": 22,
                       "epic": 30, "legendary": 40, "mythic": 45}


def _normalize_heal_pct(it) -> None:
    """[百分比回血 2026-09-01 -> heal_full 并入 2026-09-10] 把消耗品回血统一成 heal_pct 口径（单一真源）。

    规则（只对 type=consumable）：
    - heal_full 旧别名：consume_effect.type=="heal_full" 时 amount（百分数 1-100）
      迁入 heal_pct（heal_pct 已有值则以它为准），清空 consume_effect——此后纯回血药
      不再携带结构化效果（技能书/feed/stat_bonus/heal_mp/cure/revive 原样不动）。
    - 已有 heal_pct>0：只清零 heal_amount（双字段并存时 pct 优先，避免口径漂移）。
    - 只有 heal_amount>0（旧档/兜底产出）：按 _HEAL_NORM_HP 基准血换算成百分比
      （30 点 -> 12%），最低 8%。
    - 两者都无：品级兜底百分比档（common 10% ... legendary+ 45%）。
    技能书（teach_skill）不归一（非纯回血药）。
    """
    if getattr(it, "type", "") != "consumable":
        return
    if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill:
        return
    ce = getattr(it, "consume_effect", None)
    if isinstance(ce, dict) and ce and str(ce.get("type", "") or "") == "heal_full":
        try:
            amt = max(0, min(100, int(ce.get("amount", 0) or 0)))
        except (TypeError, ValueError):
            amt = 0
        if amt > 0 and int(getattr(it, "heal_pct", 0) or 0) <= 0:
            it.heal_pct = amt
        it.consume_effect = {}
        ce = {}
    if isinstance(ce, dict) and ce:
        return
    pct = int(getattr(it, "heal_pct", 0) or 0)
    amt = int(getattr(it, "heal_amount", 0) or 0)
    if pct > 0:
        it.heal_amount = 0
        return
    if amt > 0:
        it.heal_pct = max(8, min(100, round(amt / _HEAL_NORM_HP * 100)))
        it.heal_amount = 0
        return
    it.heal_pct = _HEAL_PCT_BY_RARITY.get(str(getattr(it, "rarity", "common") or "common"), 10)


def _fill_item_defaults(world: World, items: list,
                        preset: Optional[WorldSimPreset] = None):
    """[P11c] Item 数值兜底：rarity 缺省数值（攻/防/治疗）+ 题材化 stat_bonus。

    [提模块级 2026-10-01] 原为 WorldSimService._fill_item_defaults 方法体（零 self
    引用），拍卖品生成（auction_engine.start_auction，无 svc 实例）需共用同一口径，
    提为模块函数；方法保留为委托壳。

    从 _init_combat_stats 抽出，供世界生成与地图拓展新物品共用同口径。
    幂等：数值为 0 才补（LLM/老存档已给的不覆盖）。
    [用户指示 2026-08] 数值抖动：同品级同类型物品不再逐字节同构——每件按 item.id 派生
    SeededRng，在基准值 ±20% 区间抖动（确定性，同 id 同结果，可回放）。LLM 已给数值
    （非0）不抖动直接用；只对兜底补的数值抖。降重复度，符合 §23 数据量/世界感铁律。
    [用户指示 2026-08-25] 物品属性 LLM 全权产出：gen/拓展提示词带范围表让 LLM 自填，
    本函数退居「0 值兜底」——常量与提示词范围表同源（_RARITY_MULT/_ITEM_BASE_*）。
    """
    # [P7d] 题材化装备 stat_bonus（不同题材装备加不同属性：修仙法宝灵力/现代枪械反应/科幻能量武器神经）
    overlay = getattr(world, "config_overlay", None) or {}
    _tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
    _genre_bonus = _GENRE_EQUIPMENT_BONUS.get(_tid, _GENRE_EQUIPMENT_BONUS["western_fantasy"])
    for it in items:
        # [P13] 技能书（teach_skill 非空）不补治疗数值——heal_amount=0 是「研读而非饮用」的
        # 语义标记；误补会把书变治疗品（依赖调用时序的隐式不变量改为显式跳过）
        if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill:
            continue
        mult = _RARITY_MULT.get(it.rarity, 1.0)
        # [数值抖动] 每件物品按 id 派生 rng（确定性），兜底补值时在基准 ±20% 浮动
        from src.utils.rng import SeededRng as _SR
        ir = _SR.seed_from(world.id, 0, f"item_defaults_{it.id}")
        def _jitter(base: int) -> int:
            """基准值 × [0.8, 1.2) 抖动，最低保 1。"""
            return max(1, int(base * (ir.random() * 0.4 + 0.8)))
        if it.type == "weapon" and it.attack == 0:
            it.attack = _jitter(max(1, int(8 * mult)))
        elif it.type == "armor" and it.defense == 0:
            it.defense = _jitter(max(1, int(5 * mult)))
        elif it.type == "consumable" and it.heal_amount == 0:
            # [消耗品效果] LLM 未给 consume_effect 的高品消耗品兜底：rare+ 概率给
            # stat_bonus（题材化属性 + rarity 缩放）；common/uncommon 走回血兜底。
            eff = getattr(it, "consume_effect", None)
            if not isinstance(eff, dict) or not eff:
                if it.rarity in ("rare", "epic", "legendary", "mythic") \
                        and ir.chance(0.6):
                    _stats_map = _GENRE_CONSUME_STAT.get(_tid, _GENRE_CONSUME_STAT["western_fantasy"])
                    # [C4 修复 2026-08-25] _n 阶梯用于「维度数」（rare 1 维/epic 2 维/
                    # legendary+ 3 维，此前误用作单维增益值致「五维俱增」只加一维 1-3）
                    _n = 1 if it.rarity == "rare" else (2 if it.rarity == "epic" else 3)
                    _pool_keys = list(_stats_map.keys())
                    _stats = {}
                    for _ in range(min(_n, len(_pool_keys))):
                        _k = _pool_keys.pop(ir.roll(0, len(_pool_keys) - 1))
                        _stats[_k] = max(1, ir.roll(1, 3))
                    it.consume_effect = {"type": "stat_bonus",
                                         "stats": _stats}
                else:
                    # [百分比回血 2026-09-01] 兜底改百分比档（品级阶梯 + id 抖动 ±3）
                    _base_pct = _HEAL_PCT_BY_RARITY.get(it.rarity, 10)
                    it.heal_pct = max(5, min(100, _base_pct + ir.roll(-3, 3)))
        # [P7d] 题材化 stat_bonus：装备类（weapon/armor/accessory）按题材偏好 + rarity 缩放
        # 修仙法宝加 int（灵力），现代枪械加 dex（反应），科幻能量武器加 int（神经）等。
        if it.type in ("weapon", "armor", "accessory") and not it.stat_bonus:
            bonus_tmpl = _genre_bonus.get(it.type, {})
            if bonus_tmpl:
                # stat_bonus 也抖动：每属性在模板值 ±20% 区间，最低保 1
                it.stat_bonus = {k: max(1, _jitter(int(v * mult))) for k, v in bonus_tmpl.items()}
        # [P34a] level 兜底：装备类（weapon/armor/accessory）level==0 时据 rarity 派生品阶档
        # （common=1/uncommon=2/rare=3/epic=4/legendary=5），让世界生成的装备有基础品阶。
        # material/key/consumable/cultivate reagent 不派生 level（0=凡品，由生成处显式标）。
        # 钳制上限 item_max_level（preset 旋钮，0=不启用 level 体系退化凡品世界）。
        # [!] preset 经参数传入（与 _init_reagents 同口径）；缺省回退 6（WorldSimPreset 默认）。
        if it.type in ("weapon", "armor", "accessory") and it.level == 0:
            max_lvl = max(0, int(getattr(preset, "item_max_level", 6) or 0))
            if max_lvl > 0:
                rarity_lvl = _EQUIP_RARITY_LEVELS.get(it.rarity, 1)
                it.level = min(max_lvl, rarity_lvl)
        # [防数值失控] 数值填完后按品级钳制上下限（LLM 乱给数值在落库前被钳回），再写效果行
        _clamp_item_stats(it)
        # [百分比回血 2026-09-01] 新生成药：LLM 只给 heal_pct（或兜底出整数回血时）
        # 统一换算成百分比口径（heal_amount>0 且无 pct -> 按 250 基准血归一）
        _normalize_heal_pct(it)
        # [C2 修复 2026-08-25] 效果行照实生成：数值填完后按真实效果重写 effects
        # 展示行（LLM 文案保留在 desc）——「说什么」永远等于「算什么」，杜绝 OOC。
        _sync_item_effects_text(it, world)

def _clamp_item_stats(it) -> None:
    """[防数值失控 2026-08-25] 硬护栏：把物品数值钳回合理上限（防 LLM 乱给「几百攻/几十点五维」）。

    - 攻/防/回血：只钳绝对上限（最高品级区间上限 x2 的慷慨护栏），不误伤略高于品级区的合法值；
    - 装备 stat_bonus 每维封顶 8、丹药 stat_bonus 每维封顶 3（硬设计封顶，直接拦「几十点五维」）；
    - 0 值不动（由 _fill_item_defaults 兜底补 + 抖动）；material/key/技能书无数值，跳过。
    接入点：_fill_item_defaults 尾（世界生成 + 地图拓展新物品共口径）。
    """
    if getattr(it, "teach_skill", None):
        return

    def _pos_int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    t = str(getattr(it, "type", "") or "")
    if t == "weapon" and int(getattr(it, "attack", 0) or 0):
        it.attack = min(_ATTACK_CEIL, _pos_int(it.attack))
    elif t == "armor" and int(getattr(it, "defense", 0) or 0):
        it.defense = min(_DEFENSE_CEIL, _pos_int(it.defense))
    elif t == "consumable":
        # [百分比回血 2026-09-01] heal_pct 钳 0-100；旧 heal_amount 兼容（归一化在
        # _normalize_heal_pct，老档药在收编时换算；此处只防越界脏值）
        it.heal_pct = max(0, min(100, _pos_int(getattr(it, "heal_pct", 0))))
        if int(getattr(it, "heal_amount", 0) or 0):
            it.heal_amount = min(_HEAL_CEIL, _pos_int(it.heal_amount))
    # 装备 stat_bonus 每维封顶 8（最低 1；白名单外键与非正数丢弃）
    if t in ("weapon", "armor", "accessory") and isinstance(getattr(it, "stat_bonus", None), dict):
        sb = it.stat_bonus
        if sb:
            clean = {}
            for k, v in sb.items():
                if k not in ("str", "dex", "int", "vit", "luk"):
                    continue
                n = _pos_int(v)
                if n <= 0:
                    continue
                clean[k] = max(1, min(8, n))
            it.stat_bonus = clean
    # 丹药 consume_effect：stat_bonus 每维 1-3，heal_full/heal_mp amount 走回血上限
    ce = getattr(it, "consume_effect", None)
    if isinstance(ce, dict) and ce:
        etype = str(ce.get("type", "") or "")
        if etype == "stat_bonus" and isinstance(ce.get("stats"), dict):
            clean = {}
            for k, v in ce["stats"].items():
                if k not in ("str", "dex", "int", "vit", "luk"):
                    continue
                n = _pos_int(v)
                if n <= 0:
                    continue
                clean[k] = max(1, min(3, n))
            ce["stats"] = clean
        elif etype in ("heal_full", "heal_mp"):
            # [用户指示 2026-09-06] 恢复全对齐百分比：amount = 百分数，钳 1-100
            amt = _pos_int(ce.get("amount", 0))
            if amt > 0:
                ce["amount"] = max(1, min(100, amt))
    # [修 2026-08-28] base_price 品级曲线护栏（非对称钳制）：LLM 自填种子价偶发与品级
    # 严重倒挂（夜城档 legendary 售 91 < common 售 310、epic 售 51），长线经济 OOC
    # （神装白菜价、垃圾天价）。只防倒挂与天价，不抬便宜货——同品级内部的合法差异
    # 是 LLM 设计意图（材料便宜/装备贵），不掺和。锚 = RARITY_PRICE_MULT 品级乘数
    # x 品级基准（common 档 base=30）。窗口：下限 = 低一品级基准的 60%（防跨档倒挂），
    # 上限 = 高一品级基准 x1.5（重甲比杂货贵是合法跨档价差，只拦真离谱天价）。
    # [数值对齐 2026-09-06] 功能道具豁免（category 非空：鉴定卷轴/洗练石/资质丹/锻造
    # 材料/制造材料/宠物食品/种子作物）——用途定价远超同品级杂货曲线，护栏曾把鉴定
    # 卷轴 t3 的 2000 压到 495、forge 高阶材料压到与低阶同价。技能书已由开头
    # teach_skill 早退豁免。
    bp = _pos_int(getattr(it, "base_price", 0))
    if bp > 0 and not str(getattr(it, "category", "") or ""):
        from src.services.trade_engine import RARITY_PRICE_MULT
        rk = str(getattr(it, "rarity", "common") or "common")
        keys = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
        idx = keys.index(rk) if rk in keys else 0
        unit = 30.0
        lo_idx = max(0, idx - 1)
        hi_idx = min(len(keys) - 1, idx + 1)
        lo = RARITY_PRICE_MULT.get(keys[lo_idx], 1.0) * unit * 0.6
        hi = RARITY_PRICE_MULT.get(keys[hi_idx], 1.0) * unit * 1.5
        if bp < lo:
            it.base_price = max(1, int(lo))
        elif bp > hi:
            it.base_price = int(hi)


# ---- [C2 修复 2026-08-25] 效果行照实生成 ----
# LLM 写 desc（背景故事，风味保留）+ 数值由代码独立填充，两者从不交叉校验曾致
# 「丹药写五维俱增实际只回血」的 OOC。现约定单一真相源 = 数值：effects 展示行按
# 真实数值重写（consumable 有结构化真相恒重写；装备仅在 LLM 写过 effects 时重写，
# 避免为纯数值装备无中生有加文案行）。material/key/cultivate/技能书无数值谎言风险，跳过。
def _sync_item_effects_text(it, world) -> None:
    """按物品真实数值重写 effects 展示行（desc 不动）。"""
    if getattr(it, "teach_skill", None):
        return                                    # 技能书：研读语义，无数值行
    t = str(getattr(it, "type", "") or "")
    overlay = getattr(world, "config_overlay", None) or {}
    sdn = overlay.get("stat_display_names", {}) if isinstance(overlay, dict) else {}

    def _stat_zh(k: str) -> str:
        return (sdn.get(k) or {"str": "力量", "dex": "敏捷", "int": "智力",
                               "vit": "体魄", "luk": "机运"}.get(k, k))

    if t == "consumable":
        eff = getattr(it, "consume_effect", None)
        if isinstance(eff, dict) and eff:
            etype = str(eff.get("type", "") or "")
            if etype == "stat_bonus":
                st = eff.get("stats") or {}
                txt = "、".join(f"{_stat_zh(k)}+{v}" for k, v in st.items())
                it.effects = f"服用后{txt}（永久）" if txt else it.effects
            elif etype == "heal_mp":
                # [2026-09-10] 口径对齐百分比（契约：heal_mp amount = 百分数 1-100，
                # 旧绝对值语义已废除；消费端/护栏早已按百分比，仅此显示文案漏改）
                it.effects = f"服用后回复最大灵力{int(eff.get('amount', 0) or 0)}%"
            elif etype == "cure":
                it.effects = "服用后清除负面状态"
            elif etype == "revive":
                it.effects = "倒下时自动生效（复活）"
        elif int(getattr(it, "heal_pct", 0) or 0) > 0:
            it.effects = f"服用后回复最大生命{int(it.heal_pct)}%"
        elif int(getattr(it, "heal_amount", 0) or 0) > 0:
            it.effects = f"服用后回复{int(it.heal_amount)}点生命"
        return
    if t in ("weapon", "armor", "accessory"):
        if not str(getattr(it, "effects", "") or "").strip():
            return                                # LLM 未写效果行：不无中生有
        parts = []
        atk = int(getattr(it, "attack", 0) or 0)
        dfn = int(getattr(it, "defense", 0) or 0)
        if atk:
            parts.append(f"装备后攻击+{atk}")
        if dfn:
            parts.append(f"装备后防御+{dfn}")
        sb = getattr(it, "stat_bonus", None)
        if isinstance(sb, dict) and sb:
            parts.append("装备后" + "、".join(f"{_stat_zh(k)}+{v}" for k, v in sb.items()))
        if parts:
            it.effects = "；".join(parts)


# ---- [P7d] 题材化装备属性偏好（不同题材装备加不同 stat_bonus）----
# 引擎在 _init_combat_stats 据题材给 weapon/armor/accessory 注入 stat_bonus（rarity 缩放）。
# 让修仙法宝加灵力(int)、现代枪械加反应(dex)、科幻能量武器加神经(int) 等，跨题材差异化。
_GENRE_EQUIPMENT_BONUS = {
    "xianxia": {         # 修仙：法宝灵力(int) 法袍体魄(vit) 玉佩机缘(luk)
        "weapon": {"int": 2}, "armor": {"vit": 1}, "accessory": {"luk": 2, "int": 1}},
    "wuxia": {           # 武侠：刀剑臂力(str) 劲装轻功(dex) 暗器机缘(luk)
        "weapon": {"str": 2}, "armor": {"dex": 1}, "accessory": {"dex": 1, "luk": 1}},
    "modern": {          # 现代：枪械反应(dex) 防弹衣体魄(vit) 战术配件运气(luk)
        "weapon": {"dex": 2}, "armor": {"vit": 2}, "accessory": {"dex": 1, "luk": 1}},
    "scifi": {           # 科幻：能量武器神经(int) 外骨骼力量(str) 植入体神经(int)
        "weapon": {"int": 2}, "armor": {"str": 1, "vit": 1}, "accessory": {"int": 2}},
    "apocalypse": {      # 末日：改装武器力量(str) 防护服体魄(vit) 拾荒配件运气(luk)
        "weapon": {"str": 2}, "armor": {"vit": 2}, "accessory": {"luk": 1, "vit": 1}},
    "western_fantasy": {  # 西幻：常规剑力(str) 甲耐(vit) 饰品运(luk)
        "weapon": {"str": 1}, "armor": {"vit": 1}, "accessory": {"luk": 1}},
}

# [消耗品效果] 题材化属性偏好（兜底 stat_bonus 时按题材挑主属性，元素亲和口径）
_GENRE_CONSUME_STAT = {
    "xianxia": {"int": "stat_int", "vit": "stat_vit", "str": "stat_str"},
    "wuxia": {"str": "stat_str", "dex": "stat_dex", "vit": "stat_vit"},
    "modern": {"dex": "stat_dex", "vit": "stat_vit", "int": "stat_int"},
    "scifi": {"int": "stat_int", "vit": "stat_vit", "dex": "stat_dex"},
    "apocalypse": {"str": "stat_str", "vit": "stat_vit", "luk": "stat_luk"},
    "western_fantasy": {"str": "stat_str", "vit": "stat_vit", "luk": "stat_luk"},
}

# ---- [P7d2] 题材化兜底装备名（_ensure_equipment_coverage 用，确保 8 槽可填满）----
# 每题材每槽 7 个变体（rng/seed 确定性挑选；多值防拓展物品反复同名）。
# 世界生成时若某槽位无装备，补 common 兜底（题材化命名）。
_GENRE_EQUIPMENT_NAMES = {
    "xianxia": {"head": ["道冠", "木簪", "帷帽", "发带", "莲花冠", "玄铁发冠", "云纹抹额"], "chest": ["法袍", "葛布道衣", "云纹法衣", "鹤氅", "素纱道袍", "天蚕丝袍", "玄狐大氅"],
                "legs": ["束腿法裤", "内衬绔", "行山缚腿", "云缎裤", "白绸中裤", "月白绔", "缠腿丝绦"], "feet": ["云履", "麻鞋", "踏波靴", "凌波屐", "踏云靴", "棕编草履", "登山麻靴"],
                "main_hand": ["铁剑", "桃木剑", "青竹杖", "飞剑", "拂尘", "青锋软剑", "蟠龙杖"], "off_hand": ["木盾", "符盾", "八卦盘", "玉如意", "灵符册", "摄魂铃", "测灵罗盘"],
                # [2026-08-23 真人测试] 饰槽名字池与槽显示名对齐：accessory1=玉佩（坠/珠/玉/串），
                # accessory2=储物戒（戒/环/袋/匣）。旧池交叉（储物戒指在玉佩池、养魂玉在储物戒池）
                # 致「储物戒指装到玉佩栏」。
                "accessory1": ["玉佩", "平安扣", "灵珠串", "灵木串", "避水珠", "养魂玉", "辟邪玉"], "accessory2": ["储物戒指", "乾坤袋", "锁灵环", "扳指", "驱邪铃", "定魂灯", "清心香囊"]},
    "wuxia": {"head": ["头巾", "斗笠", "范阳毡帽", "青布巾", "凉帽", "英雄巾", "软纱缠头"], "chest": ["劲装", "粗布短打", "皮护胸", "夜行衣", "羊皮袄", "锁子软甲", "武馆号衣"],
              "legs": ["灯笼裤", "缚腿裤", "骑装马裤", "绑腿", "护膝裤", "踢腿快裤", "皮缠腿"], "feet": ["快靴", "麻底布鞋", "多耳麻鞋", "草鞋", "云头靴", "薄底快靴", "牛皮绑靴"],
              "main_hand": ["长刀", "镔铁剑", "齐眉棍", "朴刀", "判官笔", "厚背大刀", "水磨钢鞭"], "off_hand": ["短匕", "袖箭匣", "藤牌", "铁扇", "飞爪", "铁胎弓", "点穴橛"],
              # [2026-08-23 真人测试] 饰槽名字池与槽显示名对齐：accessory1=佩玉（玉/坠/佩/镜），
              # accessory2=护腕（腕/臂/环/指）。旧池交叉（佩玉池放铜环/汗巾、护腕池放香囊/葫芦）。
              "accessory1": ["佩玉", "玉玦", "平安扣", "护心铜镜", "同心结", "温玉坠", "玉簪坠"], "accessory2": ["护腕", "皮护腕", "铁护腕", "缠腕绳", "臂鞲", "扳指", "铜环"]},
    "modern": {"head": ["棒球帽", "针织毛线帽", "工装安全帽", "遮阳帽", "防寒耳罩", "渔夫帽", "骑行头盔"], "chest": ["厚外套", "工装夹克", "防风冲锋衣", "皮夹克", "战术背心", "连帽卫衣", "防弹内衬衣"],
               "legs": ["牛仔裤", "工装裤", "运动束脚裤", "运动长裤", "短裤", "卡其裤", "骑行裤"], "feet": ["运动鞋", "工靴", "登山鞋", "板鞋", "防水靴", "休闲皮鞋", "胶靴"],
               "main_hand": ["球棒", "钢管", "消防斧", "高尔夫球杆", "登山杖", "伸缩棍", "工兵铲"], "off_hand": ["撬棍", "臂盾", "高压电棍", "战术手电", "防狼喷雾", "防暴盾", "随身警棍"],
               "accessory1": ["手绳", "军牌", "智能手环", "护腕", "蓝牙耳机", "运动护额", "墨镜"], "accessory2": ["手表", "战术手套", "防刺背心片", "多功能刀", "对讲机", "充电宝", "折叠伞"]},
    "scifi": {"head": ["头盔", "维修工盔", "神经接驳环", "全息目镜", "环境适应盔", "外骨骼头环", "深空目镜"], "chest": ["防护服", "舱内作业服", "缓冲战斗服", "纳米护甲", "相变防护服", "液冷作战服", "防爆气密服"],
              "legs": ["作战裤", "磁力靴裤", "外骨骼腿甲", "动力腿甲", "柔性护腿", "磁吸工装裤", "缓冲作战裤"], "feet": ["军靴", "磁力靴", "缓冲垫战靴", "反重力靴", "密封靴", "重力补偿靴", "气密战靴"],
              "main_hand": ["电击棒", "切割器", "脉冲手枪", "电浆匕首", "声波枪", "射频刃", "粒子长枪"], "off_hand": ["战术盾", "便携力场发射器", "工具臂", "力场发生器", "无人机控制器", "悬浮盾", "点防无人机"],
              "accessory1": ["身份卡", "生物监测贴", "加密芯片", "神经接口", "环境扫描仪", "量子存储珠", "生物锁"], "accessory2": ["通讯器", "神经加速器", "储电芯", "应急信标", "能量电池", "冷聚变电池", "应急氧气囊"]},
    "apocalypse": {"head": ["破帽", "摩托头盔", "防毒面罩", "钢盔", "草帽", "布罩钢盔", "防暴面罩"], "chest": ["破旧外套", "防刺雨衣", "缝补帆布甲", "防弹衣残片", "兽皮坎肩", "轮胎皮甲", "雨布斗篷"],
                   "legs": ["破裤", "多口袋战术裤", "皮护腿", "迷彩裤", "工装裤", "补丁军裤", "皮护膝裤"], "feet": ["烂鞋", "缠铁丝军靴", "胶底雨鞋", "登山靴", "藤编鞋", "包裹布靴", "胶底布鞋"],
                   "main_hand": ["铁管", "消防斧", "钉板球棒", "砍刀", "长矛", "钢筋矛", "消防钩"], "off_hand": ["扳手", "锅盖盾", "刺刀短棍", "铁链", "短斧", "铁蒺藜盾", "短撬棍"],
                   "accessory1": ["破护身符", "求生哨", "自制护目镜", "狗牌", "指南针", "火柴铁盒", "废牌护符"], "accessory2": ["旧表", "盖格计数器", "多功能腰包", "打火机", "急救包", "酒精炉", "缝补包"]},
    "western_fantasy": {"head": ["皮帽", "兜帽", "旧军盔", "铁盔", "皮面甲帽", "锁链头巾", "皮风帽"], "chest": ["皮甲", "镶钉皮衣", "锁环背心", "骑士胸甲", "旅行者披风", "重型板甲", "旅法长袍"],
                        "legs": ["皮裤", "粗布马裤", "护胫绑腿", "锁甲护腿", "旅行马裤", "羊毛长裤", "骑士腿甲"], "feet": ["皮靴", "毡靴", "钉掌行军靴", "硬底靴", "行军草鞋", "骑马长靴", "鹿皮软靴"],
                        "main_hand": ["木棍", "短矛", "旧军刀", "长剑", "手斧", "双手巨剑", "战锤"], "off_hand": ["小盾", "圆木盾", "镶铁盾", "塔盾", "短剑", "鸢盾", "细剑"],
                        "accessory1": ["铜戒指", "骨雕坠", "圣徽", "护符", "银坠", "秘银戒指", "祈愿符"], "accessory2": ["铜链", "皮质护腕", "幸运硬币袋", "肩章", "行军水壶", "羊皮地图", "火绒袋"]},
}
_SLOT_TO_TYPE = {"head": "armor", "chest": "armor", "legs": "armor", "feet": "armor",
                 "main_hand": "weapon", "off_hand": "weapon",
                 "accessory1": "accessory", "accessory2": "accessory"}

# ---- [P34a] 器物体系题材化池（6 题材全覆盖，缺键回退西幻，登记 test_genre_data_volume.py 基线）----
# ---- [宠物食品 2026-08-24] 兜底宠物食品池（6 题材，type=consumable, category=pet_food）----
# 只此分类可投食宠物（feed_pet/find_feed_item 收口）。rarity 分档驱动 FEED_AFFINITY_BY_RARITY
# （common8/uncommon10/rare14/epic20/legendary30/mythic45）。定死不入 LLM（守数值范式）。
_GENRE_PET_FOOD: dict[str, list[dict]] = {
    "western_fantasy": [
        {"name": "麦香兽粮", "rarity": "common", "desc": "燕麦烘成的兽粮，牲口与猎犬都爱吃。"},
        {"name": "风干肉条", "rarity": "uncommon", "desc": "风干牛肉条，耐嚼管饱，驯兽师常备。"},
        {"name": "蜜渍鱼干", "rarity": "rare", "desc": "蜜糖渍过的鱼干，猫科魔兽难以抗拒。"},
        {"name": "狮鹫鲜肉", "rarity": "epic", "desc": "新鲜狮鹫肉，猛禽坐骑的上等饲料。"},
    ],
    "xianxia": [
        {"name": "灵谷穗", "rarity": "common", "desc": "沾着灵气的谷穗，灵宠啄食可温养经脉。"},
        {"name": "玉髓虫干", "rarity": "uncommon", "desc": "玉髓养出的灵虫干，鸟雀类灵宠最爱。"},
        {"name": "朱果", "rarity": "rare", "desc": "百年朱果，甘甜多汁，兽类食之气血两旺。"},
        {"name": "龙鳞鲤", "rarity": "epic", "desc": "带龙鳞的灵鲤，水族灵兽的无上珍馐。"},
    ],
    "wuxia": [
        {"name": "糙米肉干", "rarity": "common", "desc": "糙米拌肉干，江湖人喂马遛狗的实在吃食。"},
        {"name": "虎骨肉脯", "rarity": "uncommon", "desc": "虎骨汤浸过的肉脯，猛兽闻味即驯。"},
        {"name": "百年参果", "rarity": "rare", "desc": "深山参果，通灵猎犬食之耳目聪慧。"},
        {"name": "熊掌蜜炙", "rarity": "epic", "desc": "蜜炙熊掌，黑熊类灵兽的致命诱惑。"},
    ],
    "modern": [
        {"name": "狗粮袋装", "rarity": "common", "desc": "超市常见的袋装狗粮，营养均衡。"},
        {"name": "冻干猫粮", "rarity": "uncommon", "desc": "冻干鸡肉猫粮，挑嘴猫也买账。"},
        {"name": "高能营养膏", "rarity": "rare", "desc": "军规级高能营养膏，工作犬的快速补剂。"},
        {"name": "进口鲜肉罐", "rarity": "epic", "desc": "冷链进口鲜肉罐头，宠物界的奢侈品。"},
    ],
    "scifi": [
        {"name": "合成蛋白粒", "rarity": "common", "desc": "3D 打印的合成蛋白粒，标准宠物口粮。"},
        {"name": "营养凝胶", "rarity": "uncommon", "desc": "缓释营养凝胶，舱养生物的通用饲料。"},
        {"name": "仿生肉块", "rarity": "rare", "desc": "仿生培养肉块，肉食型异宠的最爱。"},
        {"name": "神经强化剂", "rarity": "epic", "desc": "强化神经反射的饲剂，军用异兽专用。"},
    ],
    "apocalypse": [
        {"name": "罐头剩肉", "rarity": "common", "desc": "废墟里翻出的罐头剩肉，末世里聊胜于无。"},
        {"name": "风干鼠肉", "rarity": "uncommon", "desc": "风干鼠肉串，变异犬的硬通货。"},
        {"name": "净水鱼干", "rarity": "rare", "desc": "净水区捞的鱼晒成的干，难得的安全口粮。"},
        {"name": "变异兽肉排", "rarity": "epic", "desc": "烤变异兽肉排，大型变异宠的盛宴。"},
    ],
}


def pet_food_pool(world: "World") -> list[dict]:
    """[宠物食品] 取当前世界题材的宠物食品池，缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    return list(_GENRE_PET_FOOD.get(tid) or _GENRE_PET_FOOD["western_fantasy"])


# ---- [饱食度 2026-09-06 食物题材兜底池（不走 LLM，纯引擎确定性）/
# 2026-09-08 食堂已删] 玩家与 NPC 恢复饱食度一律走食物店买食物吃
# （背包「使用」/ NPC 自动买食）。
# CANTEEN_MEAL_COST 保留（旧档/测试导入兼容），堂食逻辑已下线。
CANTEEN_MEAL_COST = 12
# _GENRE_CANTEEN 已删（食堂场所不再铺设；旧档残留 canteen 场所由叙事自然消化）。

# 食物：type=consumable（category 留空 -> 商店可正常进货/上架），吃下回饱食度
# （consume_effect {"type":"feed","hunger":N}，combat_engine.apply_consume_effect 结算）。
# [数据量 2026-09-08] 6 题材各 10 种三档：果腹档（feed 25-35/价 3-6，没钱保底，
# NPC 钱包瘪了也买得起）/ 正餐档（feed 40-55/价 8-22）/ 硬菜档（feed 60-80/
# 价 24-40，一次回满的消费出口）。容量下限守卫：tests/test_genre_data_volume.py
# TestFoodPool（每题材 >=10 + 三档全覆盖 + 名唯一）。
_GENRE_FOODS: dict[str, list[dict]] = {
    "xianxia": [
        {"name": "灵谷团子", "price": 4, "feed": 28, "desc": "灵谷捏的小团子，垫一口是一口。"},
        {"name": "清心茶点", "price": 5, "feed": 32, "desc": "配清心茶的粗点心，聊胜于无。"},
        {"name": "山药糕", "price": 8, "feed": 40, "desc": "蒸得软糯的山药糕，垫肚正合适。"},
        {"name": "灵米饭", "price": 10, "feed": 50, "desc": "掺了灵米的蒸饭，一碗下肚浑身舒坦。"},
        {"name": "灵芝炖汤", "price": 25, "feed": 55, "desc": "小火慢炖的灵芝汤，暖胃又顶饱。"},
        {"name": "辟谷丸", "price": 20, "feed": 60, "desc": "服一颗可抵一日之饥，赶路修士常备。"},
        {"name": "醉仙酿配灵笋宴", "price": 30, "feed": 68, "desc": "一席灵笋全宴佐醉仙酿，吃完飘飘欲仙。"},
        {"name": "九转金丹宴", "price": 38, "feed": 78, "desc": "以丹法烹制的压桌大宴，大补。"},
        {"name": "云雾灵粥", "price": 12, "feed": 45, "desc": "灵泉熬的粥，米香里带着灵气。"},
        {"name": "烤灵薯", "price": 6, "feed": 35, "desc": "灵田里刨出的薯，烤熟最香。"},
    ],
    "wuxia": [
        {"name": "粗面窝头", "price": 3, "feed": 25, "desc": "啃得动的窝头，穷汉的命根子。"},
        {"name": "茶叶蛋", "price": 4, "feed": 30, "desc": "卤得入味的茶叶蛋，两个顶一顿。"},
        {"name": "白面馒头", "price": 6, "feed": 40, "desc": "实在的大馒头，管饱。"},
        {"name": "酱肉包子", "price": 8, "feed": 45, "desc": "一屉酱香浓郁的肉包子。"},
        {"name": "阳春面", "price": 10, "feed": 50, "desc": "一碗清汤阳春面，热汤下肚暖洋洋。"},
        {"name": "酱牛肉", "price": 20, "feed": 60, "desc": "切一盘酱牛肉配饼，大侠也吃得满足。"},
        {"name": "叫花鸡", "price": 26, "feed": 66, "desc": "泥封煨出的叫花鸡，香飘半条街。"},
        {"name": "全羊宴", "price": 36, "feed": 76, "desc": "整只烤全羊摆上桌，豪气干云。"},
        {"name": "牛肉拉面", "price": 14, "feed": 52, "desc": "汤浓面劲道，大碗才过瘾。"},
        {"name": "烧饼夹肉", "price": 5, "feed": 33, "desc": "酥烧饼夹卤肉，边走边吃。"},
    ],
    "modern": [
        {"name": "白水煮蛋", "price": 3, "feed": 26, "desc": "便利店的煮蛋，便宜顶饿。"},
        {"name": "小面包", "price": 4, "feed": 30, "desc": "一袋小面包，凑合一顿。"},
        {"name": "吐司面包", "price": 8, "feed": 40, "desc": "一袋切片吐司，随手充饥。"},
        {"name": "能量棒", "price": 12, "feed": 45, "desc": "坚果压缩能量棒，户外常备。"},
        {"name": "外卖便当", "price": 15, "feed": 50, "desc": "两荤一素的便当，微波炉叮一下。"},
        {"name": "自热火锅", "price": 22, "feed": 55, "desc": "加水就开的火锅，热气腾腾。"},
        {"name": "烤肉自助", "price": 32, "feed": 70, "desc": "扶墙进扶墙出的烤肉自助。"},
        {"name": "海鲜大餐", "price": 40, "feed": 80, "desc": "现捞海鲜一桌，吃到扶肚。"},
        {"name": "牛肉盖饭", "price": 16, "feed": 53, "desc": "牛肉盖饭配味增汤，打工人的幸福。"},
        {"name": "关东煮", "price": 6, "feed": 35, "desc": "热乎的关东煮，冬夜续命。"},
    ],
    "scifi": [
        {"name": "基础口粮块", "price": 3, "feed": 25, "desc": "无味的口粮块，能量达标就行。"},
        {"name": "电解质饮", "price": 5, "feed": 30, "desc": "一瓶电解质饮，撑过轮班。"},
        {"name": "藻类沙拉", "price": 8, "feed": 40, "desc": "培养舱现收的藻类，清脆爽口。"},
        {"name": "营养膏", "price": 10, "feed": 45, "desc": "标准配比的管装营养膏，口味随机。"},
        {"name": "合成蛋白餐", "price": 18, "feed": 55, "desc": "打印成型的整份蛋白餐。"},
        {"name": "高能压缩粮", "price": 20, "feed": 60, "desc": "军队规格压缩粮，一小块顶一天。"},
        {"name": "舰长晚宴", "price": 30, "feed": 68, "desc": "舰桥餐厅的定制晚宴，有酒有肉。"},
        {"name": "基因定制大餐", "price": 38, "feed": 78, "desc": "按你基因调配的完美一餐。"},
        {"name": "蘑菇浓汤", "price": 12, "feed": 46, "desc": "真菌培养皿熬的浓汤，意外鲜美。"},
        {"name": "能量果冻", "price": 6, "feed": 34, "desc": "吸一包能量果冻，继续干活。"},
    ],
    "apocalypse": [
        {"name": "树皮汤", "price": 3, "feed": 25, "desc": "树皮熬的汤，能騙过肚子就行。"},
        {"name": "蚯蚓干", "price": 4, "feed": 30, "desc": "晒干的蛋白条，闭眼吃。"},
        {"name": "烤变异薯", "price": 8, "feed": 40, "desc": "火堆边烤熟的变异薯，味道意外不错。"},
        {"name": "压缩饼干", "price": 10, "feed": 45, "desc": "就水啃的压缩饼干，扛饿。"},
        {"name": "午餐肉罐头", "price": 15, "feed": 50, "desc": "战前生产的罐头，开罐即食。"},
        {"name": "净水干粮包", "price": 18, "feed": 55, "desc": "配净水的干粮套餐，聚居点硬通货。"},
        {"name": "变异兽烤肉", "price": 26, "feed": 65, "desc": "整条兽腿架火上烤，油香四溢。"},
        {"name": "首领盛宴", "price": 34, "feed": 75, "desc": "聚居点首领才吃得上的大餐。"},
        {"name": "野菜糊糊", "price": 5, "feed": 32, "desc": "野菜剁碎熬的糊糊，勉强果腹。"},
        {"name": "鼠肉串", "price": 7, "feed": 36, "desc": "烤得焦香的鼠肉串，真香。"},
    ],
    "western_fantasy": [
        {"name": "燕麦粥", "price": 3, "feed": 26, "desc": "稀稀的燕麦粥，暖暖胃。"},
        {"name": "硬面包干", "price": 4, "feed": 30, "desc": "硬到能当武器的面包干，配水咽。"},
        {"name": "黑麦面包", "price": 8, "feed": 40, "desc": "扎实的黑麦面包，越嚼越香。"},
        {"name": "烤肠", "price": 10, "feed": 45, "desc": "滋滋冒油的烤肠，市集常见小吃。"},
        {"name": "炖肉浓汤", "price": 18, "feed": 55, "desc": "大锅炖的肉汤，配面包最妙。"},
        {"name": "旅行干粮", "price": 20, "feed": 60, "desc": "冒险者行囊里的耐储干粮包。"},
        {"name": "烤全猪", "price": 28, "feed": 67, "desc": "篝火上的整只烤猪，庆典标配。"},
        {"name": "国王盛宴", "price": 36, "feed": 77, "desc": "宫廷规格的盛宴，吃完想封爵。"},
        {"name": "奶酪拼盘", "price": 12, "feed": 46, "desc": "几种奶酪配果酱，酒馆下酒菜。"},
        {"name": "苹果派", "price": 6, "feed": 34, "desc": "刚出炉的苹果派，香甜管饱。"},
    ],
}


# ---- [野心 2026-09-06 用户指示] 一生追求题材池（全员无差别；kind 抽象 key + 题材化 desc）----
# 8 类 x 6 题材各 >=1 条；判定全引擎纯 Python（wealth=钱包达标/revenge=目标死亡/
# courtship=与目标 social>=80/mastery=等级达标/fame=好友数>=4/explore=到访地点数/
# collect=背包稀有物品数/craft=累计合成件数）。
_GENRE_AMBITIONS: dict[str, dict[str, list[str]]] = {
    "xianxia": {
        "wealth": ["攒够灵石，在坊市盘下一间属于自己的铺面", "蓄资开辟一方洞府，安身立命"],
        "revenge": ["报当年被夺走功法之仇", "为惨死的同门讨回公道"],
        "courtship": ["攒足勇气与心意，向心上人表明心迹", "寻访失散多年的血亲下落"],
        "mastery": ["苦修突破当前境界，向大道再进一步", "练成本门失传的绝学"],
        "fame": ["扬名修真界，让名号传遍各州", "结交四方道友，闯出自己的名声"],
        "explore": ["游历名山大川，访遍天下灵迹", "行万里路，见识九州风物"],
        "collect": ["集齐一炉天材地宝，凑齐梦寐以求的药材", "收藏几件称心的灵物"],
        "craft": ["炼出属于自己的成名丹药", "炼器手艺精进，打出传世之作"],
    },
    "wuxia": {
        "wealth": ["攒够银子，盘下一间小小的店面安身", "置一份家业，不再漂泊江湖"],
        "revenge": ["手刃血仇，了结多年恩怨", "找当年灭门的仇家讨还血债"],
        "courtship": ["攒足勇气与心意，向心上人表明心迹", "寻访失散多年的亲人下落"],
        "mastery": ["练成一套压箱底的绝技，行走江湖更有底气", "武艺精进，突破瓶颈"],
        "fame": ["行侠仗义，让名号响彻武林", "结交五湖四海的朋友，闯出侠名"],
        "explore": ["仗剑走遍大江南北", "游历名山大泽，访遍天下奇景"],
        "collect": ["收藏几件趁手的兵器古玩", "搜罗一套梦寐以求的珍本"],
        "craft": ["打出一副传世的好兵器", "手艺精进，做出拿得出手的作品"],
    },
    "modern": {
        "wealth": ["攒够首付，在这座城市安个家", "存一笔启动资金，开一家自己的小店"],
        "revenge": ["讨回被坑骗的血汗钱", "让害过自己家人的人付出代价"],
        "courtship": ["攒足勇气向心上人告白", "寻找失联多年的旧友下落"],
        "mastery": ["考下那个心心念念的证书", "把手艺练到独当一面"],
        "fame": ["在圈子里做出名气", "多交朋友，人脉广了路好走"],
        "explore": ["存钱走遍想去的城市", "环游列国，看遍山海"],
        "collect": ["收藏一套心仪已久的物件", "集齐喜欢的系列"],
        "craft": ["做出一件拿得出手的作品", "手艺精进，开个个人工作室"],
    },
    "scifi": {
        "wealth": ["攒够信用点，买下心仪的舱位", "存钱置办一间自己的工坊"],
        "revenge": ["查清事故真相，让责任人付出代价", "向毁了家乡的人讨回公道"],
        "courtship": ["鼓起勇气向心仪之人表白", "寻找殖民船失散的亲人"],
        "mastery": ["完成义体升级，突破身体极限", "把驾驶技术练到顶尖"],
        "fame": ["在星域闯出名号", "结识各大势力的要人"],
        "explore": ["探索未测绘的星域", "走遍已知星域的每一座空间站"],
        "collect": ["收藏几件旧地球的遗物", "集齐一整套稀有样本"],
        "craft": ["造出属于自己的舰船", "研发出专利级的发明"],
    },
    "apocalypse": {
        "wealth": ["攒够物资，换一处安全的落脚点", "囤一批硬通货，乱世求安"],
        "revenge": ["向抢走同伴物资的人讨回公道", "查清害死家人的真相"],
        "courtship": ["在这乱世里，想和那个人好好活下去", "寻找失散的家人"],
        "mastery": ["把枪法练到百发百中", "学会一门乱世求生的硬本事"],
        "fame": ["在聚居点闯出值得信赖的名声", "多结盟友，乱世靠朋友"],
        "explore": ["摸清周边的安全区与资源点", "走遍废土寻找旧世界的遗产"],
        "collect": ["囤够过冬的物资储备", "收藏旧世界的记忆物件"],
        "craft": ["修好一台旧世界的机器", "手艺精进，做出庇护所需要的装备"],
    },
    "western_fantasy": {
        "wealth": ["攒够金币，盘下一间自己的作坊", "置一份家业，不再替人卖命"],
        "revenge": ["向毁掉家乡的恶徒复仇", "为逝去的战友讨回公道"],
        "courtship": ["攒足勇气向心上人求婚", "寻找战中失散的亲人"],
        "mastery": ["练成大师级的武艺或法术", "通过行会的最高考核"],
        "fame": ["受封骑士，名留青史", "结交各国的英雄豪杰"],
        "explore": ["走遍王国的每一座名城", "绘制自己的冒险地图"],
        "collect": ["收藏几件魔法造物", "集齐一套古代遗物"],
        "craft": ["锻造出刻着自己名字的神兵", "炼金术突破宗师之境"],
    },
}


def _ambition_pool(world: "World") -> dict:
    """[野心] 取当前题材的野心池，缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    return _GENRE_AMBITIONS.get(tid) or _GENRE_AMBITIONS["western_fantasy"]


def foods_pool(world: "World") -> list[dict]:
    """[饱食度] 取当前世界题材的食物池，缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    return list(_GENRE_FOODS.get(tid) or _GENRE_FOODS["western_fantasy"])


def canteen_template(world: "World") -> dict:
    """[2026-09-08 食堂已删] 保留签名兼容（旧调用），返回空模板。"""
    return {"name": "", "desc": ""}


def canteen_place_at(loc) -> "Place | None":
    """[2026-09-08 食堂已删] 保留签名兼容（旧调用/旧档残留场所查询），
    一律返回 None（= 无食堂）。旧档残留 canteen 场所由叙事自然消化。"""
    return None


# 培养类 reagent（鉴定卷轴/洗练石/资质丹）：type=consumable, category=cultivate。
# tier 1-3 对应物品 level 档（鉴定/洗练高 level 物品需高 tier reagent）。
# cultivate_kind: identify=鉴定卷轴 / refine=装备洗练石 / apt_atk/apt_def/apt_hp/apt_mp/apt_spd=宠物资质丹
#                 / apt_all=全资质洗练石（高 tier）。
_GENRE_CULTIVATE_ITEMS: dict[str, list[dict]] = {
    "western_fantasy": [
        {"kind": "identify", "tier": 1, "name": "劣质鉴定卷轴", "desc": "羊皮卷上潦草写着辨认咒文，能看清普通装备的属性。"},
        {"kind": "identify", "tier": 2, "name": "秘银鉴定卷轴", "desc": "秘银粉书写的卷轴，能洞察精良装备隐藏的属性。"},
        {"kind": "identify", "tier": 3, "name": "真知卷轴", "desc": "大法师封印的真知之卷，连传说神器的属性也无所遁形。"},
        {"kind": "refine", "tier": 1, "name": "粗磨石", "desc": "粗粝的磨石，能重洗普通装备的附魔词条。"},
        {"kind": "refine", "tier": 2, "name": "星辰砂", "desc": "蕴含星辰之力的细砂，可重洗精良装备的附魔。"},
        {"kind": "refine", "tier": 3, "name": "龙血洗炼石", "desc": "浸过古龙血的炼金石，能重铸传说装备的词条。"},
        {"kind": "apt_atk", "tier": 1, "name": "蛮力肉干", "desc": "喂食后重洗宠物的攻击资质。"},
        {"kind": "apt_def", "tier": 1, "name": "坚壳甲粉", "desc": "喂食后重洗宠物的防御资质。"},
        {"kind": "apt_hp", "tier": 1, "name": "生命果浆", "desc": "喂食后重洗宠物的体力资质。"},
        {"kind": "apt_mp", "tier": 1, "name": "魔力露水", "desc": "喂食后重洗宠物的法力资质。"},
        {"kind": "apt_spd", "tier": 1, "name": "疾风花粉", "desc": "喂食后重洗宠物的速度资质。"},
        {"kind": "apt_all", "tier": 3, "name": "万象归元丹", "desc": "上古丹药，喂食后重洗宠物全部资质。"},
        {"kind": "beast_skill", "tier": 2, "name": "兽语卷轴", "desc": "记载兽语咒文的卷轴，诵读可重洗宠物一项天生技能。"},
    ],
    "xianxia": [
        {"kind": "identify", "tier": 1, "name": "初阶探灵符", "desc": "黄纸朱砂的探灵符，能窥凡器灵性。"},
        {"kind": "identify", "tier": 2, "name": "中阶辨宝符", "desc": "灵纹密布的辨宝符，可识法宝玄机。"},
        {"kind": "identify", "tier": 3, "name": "天眼通灵符", "desc": "得道者亲绘的通灵符，仙器亦无所遁形。"},
        {"kind": "refine", "tier": 1, "name": "洗器粗砂", "desc": "矿砂磨洗法器，可重铸其灵纹词条。"},
        {"kind": "refine", "tier": 2, "name": "九天灵砂", "desc": "采自九天的灵砂，重洗法宝词条如臂使指。"},
        {"kind": "refine", "tier": 3, "name": "混沌洗炼液", "desc": "混沌之气凝成的洗炼液，仙器词条亦可重铸。"},
        {"kind": "apt_atk", "tier": 1, "name": "锻骨丹", "desc": "喂食后重洗灵宠的攻击资质（根骨之力）。"},
        {"kind": "apt_def", "tier": 1, "name": "护脉丹", "desc": "喂食后重洗灵宠的防御资质。"},
        {"kind": "apt_hp", "tier": 1, "name": "固本丹", "desc": "喂食后重洗灵宠的体力资质。"},
        {"kind": "apt_mp", "tier": 1, "name": "养灵丹", "desc": "喂食后重洗灵宠的法力资质（灵根之蕴）。"},
        {"kind": "apt_spd", "tier": 1, "name": "轻身丹", "desc": "喂食后重洗灵宠的速度资质。"},
        {"kind": "apt_all", "tier": 3, "name": "九转归元丹", "desc": "九转金丹，喂食后重洗灵宠全部资质。"},
        {"kind": "beast_skill", "tier": 2, "name": "灵兽诀玉简", "desc": "录着灵兽神通的玉简，催动可重洗灵宠一项天生技能。"},
    ],
    "wuxia": [
        {"kind": "identify", "tier": 1, "name": "江湖辨识录", "desc": "泛黄的小册子，记着辨识寻常兵刃的门道。"},
        {"kind": "identify", "tier": 2, "name": "名剑谱残页", "desc": "名剑谱残页，能识精良兵刃的来历与暗藏属性。"},
        {"kind": "identify", "tier": 3, "name": "千机鉴", "desc": "千机阁的鉴定秘典，神兵利器亦一览无余。"},
        {"kind": "refine", "tier": 1, "name": "粗磨刀石", "desc": "寻常磨刀石，能重洗普通兵刃的锋芒词条。"},
        {"kind": "refine", "tier": 2, "name": "寒铁粉", "desc": "寒铁磨成的细粉，重洗精良兵刃词条不在话下。"},
        {"kind": "refine", "tier": 3, "name": "玄铁重液", "desc": "玄铁熔炼的重液，神兵词条亦可重铸。"},
        {"kind": "apt_atk", "tier": 1, "name": "虎骨酒", "desc": "喂食后重洗灵兽的攻击资质。"},
        {"kind": "apt_def", "tier": 1, "name": "龟甲散", "desc": "喂食后重洗灵兽的防御资质。"},
        {"kind": "apt_hp", "tier": 1, "name": "参须汤", "desc": "喂食后重洗灵兽的体力资质。"},
        {"kind": "apt_mp", "tier": 1, "name": "清心茶", "desc": "喂食后重洗灵兽的内力资质。"},
        {"kind": "apt_spd", "tier": 1, "name": "燕窝膏", "desc": "喂食后重洗灵兽的速度资质。"},
        {"kind": "apt_all", "tier": 3, "name": "九花玉露丸", "desc": "宫廷秘药，喂食后重洗灵兽全部资质。"},
        {"kind": "beast_skill", "tier": 2, "name": "驯兽秘册", "desc": "驯兽斋传下的秘册，诵读可重洗灵兽一项技能。"},
    ],
    "modern": [
        {"kind": "identify", "tier": 1, "name": "便携检测仪", "desc": "巴掌大的检测仪，能读出普通装备的参数。"},
        {"kind": "identify", "tier": 2, "name": "光谱分析仪", "desc": "实验室级光谱仪，可解析精良装备的隐藏参数。"},
        {"kind": "identify", "tier": 3, "name": "量子扫描仪", "desc": "前沿量子扫描仪，传说装备的参数也无所遁形。"},
        {"kind": "refine", "tier": 1, "name": "改装工具包", "desc": "基础改装工具，能重洗普通装备的改装词条。"},
        {"kind": "refine", "tier": 2, "name": "纳米涂料", "desc": "纳米级改性涂料，重洗精良装备词条。"},
        {"kind": "refine", "tier": 3, "name": "量子重组液", "desc": "量子态重组液，传说装备词条亦可重铸。"},
        {"kind": "apt_atk", "tier": 1, "name": "高蛋白饲料", "desc": "喂食后重洗宠物的攻击资质。"},
        {"kind": "apt_def", "tier": 1, "name": "钙质补充剂", "desc": "喂食后重洗宠物的防御资质。"},
        {"kind": "apt_hp", "tier": 1, "name": "能量营养膏", "desc": "喂食后重洗宠物的体力资质。"},
        {"kind": "apt_mp", "tier": 1, "name": "神经活性剂", "desc": "喂食后重洗宠物的法力资质。"},
        {"kind": "apt_spd", "tier": 1, "name": "代谢加速剂", "desc": "喂食后重洗宠物的速度资质。"},
        {"kind": "apt_all", "tier": 3, "name": "基因重编程液", "desc": "前沿基因制剂，喂食后重洗宠物全部资质。"},
        {"kind": "beast_skill", "tier": 2, "name": "训练改写手册", "desc": "专业改写的训练手册，能重训宠物一项战斗技能。"},
    ],
    "scifi": [
        {"kind": "identify", "tier": 1, "name": "手持扫描模块", "desc": "标准手持扫描模块，能读出普通装备的参数。"},
        {"kind": "identify", "tier": 2, "name": "深层光谱探针", "desc": "深层光谱探针，可解析精良装备的隐藏参数。"},
        {"kind": "identify", "tier": 3, "name": "亚原子解析仪", "desc": "亚原子级解析仪，传说装备的参数也无所遁形。"},
        {"kind": "refine", "tier": 1, "name": "基础改造套件", "desc": "基础改造套件，能重洗普通装备的改造词条。"},
        {"kind": "refine", "tier": 2, "name": "纳米重组剂", "desc": "纳米级重组剂，重洗精良装备词条。"},
        {"kind": "refine", "tier": 3, "name": "暗物质重铸液", "desc": "暗物质重铸液，传说装备词条亦可重铸。"},
        {"kind": "apt_atk", "tier": 1, "name": "战斗子程序", "desc": "注入后重洗机械宠物的攻击资质。"},
        {"kind": "apt_def", "tier": 1, "name": "护甲补丁", "desc": "注入后重洗机械宠物的防御资质。"},
        {"kind": "apt_hp", "tier": 1, "name": "能源核心", "desc": "注入后重洗机械宠物的耐久资质。"},
        {"kind": "apt_mp", "tier": 1, "name": "计算单元", "desc": "注入后重洗机械宠物的算力资质。"},
        {"kind": "apt_spd", "tier": 1, "name": "伺服固件", "desc": "注入后重洗机械宠物的速度资质。"},
        {"kind": "apt_all", "tier": 3, "name": "全系统重置芯片", "desc": "高级重置芯片，注入后重洗机械宠物全部资质。"},
        {"kind": "beast_skill", "tier": 2, "name": "技能改写固件", "desc": "注入后可改写宠物一项战斗技能模块的固件。"},
    ],
    "apocalypse": [
        {"kind": "identify", "tier": 1, "name": "破旧检测器", "desc": "拼凑的检测器，勉强能读出普通装备的参数。"},
        {"kind": "identify", "tier": 2, "name": "军用扫描仪", "desc": "缴获的军用扫描仪，可解析精良装备的隐藏参数。"},
        {"kind": "identify", "tier": 3, "name": "战前分析仪", "desc": "战前遗存的高级分析仪，传说装备的参数也无所遁形。"},
        {"kind": "refine", "tier": 1, "name": "拆解工具箱", "desc": "拆解工具箱，能重洗普通装备的改装词条。"},
        {"kind": "refine", "tier": 2, "name": "工业改性剂", "desc": "工业级改性剂，重洗精良装备词条。"},
        {"kind": "refine", "tier": 3, "name": "变异催化剂", "desc": "变异催化剂，传说装备词条亦可重铸。"},
        {"kind": "apt_atk", "tier": 1, "name": "猛兽肉糜", "desc": "喂食后重洗变异兽的攻击资质。"},
        {"kind": "apt_def", "tier": 1, "name": "硬甲碎片", "desc": "喂食后重洗变异兽的防御资质。"},
        {"kind": "apt_hp", "tier": 1, "name": "罐头口粮", "desc": "喂食后重洗变异兽的体力资质。"},
        {"kind": "apt_mp", "tier": 1, "name": "变异腺体", "desc": "喂食后重洗变异兽的异能资质。"},
        {"kind": "apt_spd", "tier": 1, "name": "速效刺激素", "desc": "喂食后重洗变异兽的速度资质。"},
        {"kind": "apt_all", "tier": 3, "name": "完美基因血清", "desc": "战前遗留的基因血清，喂食后重洗变异兽全部资质。"},
        {"kind": "beast_skill", "tier": 2, "name": "变异技能腺体", "desc": "变异兽体内提取的腺体，喂食可重洗宠物一项变异技能。"},
    ],
}

# 锻造材料（矿石/皮革/灵铁等）：type=material, category=forge。
# 用于住宅建筑建造/升级耗材 + 锻造配方输入（P34c 联动）。每题材 >=4 种。
_GENRE_FORGE_MATERIALS: dict[str, list[dict]] = {
    "western_fantasy": [
        {"name": "铁矿石", "desc": "常见的铁矿石，锻造寻常兵刃的基底。"},
        {"name": "秘银锭", "desc": "蕴含微弱魔力的秘银，精良装备的辅材。"},
        {"name": "精金碎片", "desc": "坚硬无比的精金碎片，史诗装备的关键材料。"},
        {"name": "龙鳞片", "desc": "古龙蜕下的鳞片，传说装备的点睛之材。"},
        {"name": "厚皮", "desc": "鞣制过的厚兽皮，制作皮甲的常用材料。"},
        {"name": "星陨铁", "desc": "天外坠落的星陨铁，蕴含陨星之力。"},
        {"name": "魔化木", "desc": "被魔气浸润的硬木，可制杖芯。"},
        {"name": "龙牙", "desc": "古龙脱落的利牙，锻造上好兵刃的材料。"},
    ],
    "xianxia": [
        {"name": "玄铁精", "desc": "蕴含灵气的玄铁精，炼制法器的基底。"},
        {"name": "寒髓玉", "desc": "寒髓玉髓，精良法宝的辅材。"},
        {"name": "九天玄铁", "desc": "采自九天的玄铁，史诗法宝的关键材料。"},
        {"name": "混沌神铁", "desc": "混沌凝成的神铁，仙器的点睛之材。"},
        {"name": "妖兽皮", "desc": "妖兽之皮，炼制法袍的常用材料。"},
        {"name": "星辰砂", "desc": "自陨星中提炼的星辰砂，炼器佳品。"},
        {"name": "龙血木", "desc": "浸过蛟龙血的神木，可炼器。"},
        {"name": "太乙精金", "desc": "太乙真金，仙器级的珍稀材料。"},
    ],
    "wuxia": [
        {"name": "镔铁", "desc": "百炼镔铁，锻造寻常兵刃的基底。"},
        {"name": "寒铁", "desc": "寒铁矿石，精良兵刃的辅材。"},
        {"name": "陨铁", "desc": "天外陨铁，神兵的关键材料。"},
        {"name": "玄铁", "desc": "千年玄铁，绝世神兵的点睛之材。"},
        {"name": "牛皮", "desc": "坚韧牛皮，制作护具的常用材料。"},
        {"name": "天外陨铁", "desc": "坠于山巅的陨铁，铸神兵的上佳之选。"},
        {"name": "千年寒铁", "desc": "深埋雪山千年的寒铁。"},
        {"name": "兽王骨", "desc": "兽王之骨，可磨制骨兵。"},
    ],
    "modern": [
        {"name": "钢材", "desc": "工业钢材，改装普通装备的基底。"},
        {"name": "钛合金", "desc": "轻而坚的钛合金，精良装备的辅材。"},
        {"name": "碳纳米管", "desc": "高强度碳纳米管，史诗装备的关键材料。"},
        {"name": "超导合金", "desc": "常温超导合金，传说装备的点睛之材。"},
        {"name": "凯夫拉", "desc": "凯夫拉纤维，制作防弹护具的常用材料。"},
        {"name": "钨合金", "desc": "高密度钨合金，军用级材料。"},
        {"name": "石墨烯", "desc": "石墨烯薄膜，轻薄而坚韧。"},
        {"name": "钛镍合金", "desc": "记忆金属，可自修复。"},
    ],
    "scifi": [
        {"name": "合金坯", "desc": "标准合金坯，改造普通装备的基底。"},
        {"name": "晶格金属", "desc": "纳米晶格金属，精良装备的辅材。"},
        {"name": "简并态材料", "desc": "简并态物质，史诗装备的关键材料。"},
        {"name": "暗物质结晶", "desc": "暗物质凝聚的结晶，传说装备的点睛之材。"},
        {"name": "复合装甲片", "desc": "复合装甲片，制作护甲的常用材料。"},
        {"name": "超合金", "desc": "工业超合金，装甲级材料。"},
        {"name": "反物质晶格", "desc": "反物质约束晶格，特种工艺材料。"},
        {"name": "记忆金属", "desc": "形变自愈的记忆金属。"},
    ],
    "apocalypse": [
        {"name": "废铁", "desc": "回收的废铁，改装普通装备的基底。"},
        {"name": "枪械零件", "desc": "拆解的枪械零件，精良装备的辅材。"},
        {"name": "军用合金", "desc": "缴获的军用合金，史诗装备的关键材料。"},
        {"name": "战前精密件", "desc": "战前遗存的精密部件，传说装备的点睛之材。"},
        {"name": "防刺纤维", "desc": "防刺纤维布，制作护具的常用材料。"},
        {"name": "战车装甲", "desc": "报废战车拆下的装甲板。"},
        {"name": "钛合金残片", "desc": "战前钛合金残片，仍堪大用。"},
        {"name": "强化钢材", "desc": "军工厂库房里的强化钢材。"},
    ],
}

# 制造材料（布匹/木材/灵药草等）：type=material, category=craft。
# 用于炼丹/制造配方输入 + 建筑建造耗材（P34c 联动）。每题材 >=4 种。
_GENRE_CRAFT_MATERIALS: dict[str, list[dict]] = {
    "western_fantasy": [
        {"name": "亚麻布", "desc": "普通亚麻布，缝制法袍的基底。"},
        {"name": "魔纹布", "desc": "织有魔纹的布料，精良法袍的辅材。"},
        {"name": "月光草", "desc": "月光下采摘的药草，炼制初级药剂。"},
        {"name": "精灵之泪", "desc": "精灵凝结的泪珠，史诗药剂的关键材料。"},
        {"name": "橡木", "desc": "坚韧的橡木，制作法杖的常用材料。"},
        {"name": "丝绸", "desc": "贵族丝绸，缝制华服。"},
        {"name": "龙血草", "desc": "龙血浇灌的灵草，药力猛烈。"},
        {"name": "黑曜石", "desc": "黑曜石，制作法器的常用材料。"},
    ],
    "xianxia": [
        {"name": "灵蚕丝", "desc": "灵蚕吐的丝，缝制法袍的基底。"},
        {"name": "天蚕丝", "desc": "天蚕之丝，精良法袍的辅材。"},
        {"name": "百年灵芝", "desc": "百年灵芝，炼制初级丹药。"},
        {"name": "千年雪莲", "desc": "千年雪莲，史诗丹药的关键材料。"},
        {"name": "雷击木", "desc": "雷击之木，制作法杖的常用材料。"},
        {"name": "云锦", "desc": "云锦灵缎，缝制仙衣。"},
        {"name": "灵参", "desc": "千年灵参，炼丹佳品。"},
        {"name": "玄晶", "desc": "玄晶，制作灵器的常用材料。"},
    ],
    "wuxia": [
        {"name": "棉布", "desc": "普通棉布，缝制劲装的基底。"},
        {"name": "冰蚕丝", "desc": "冰蚕之丝，精良护具的辅材。"},
        {"name": "三七草", "desc": "常见药草，炼制金创药。"},
        {"name": "天山雪莲", "desc": "天山雪莲，名贵丹药的关键材料。"},
        {"name": "楠木", "desc": "坚韧楠木，制作兵器的常用材料。"},
        {"name": "绸缎", "desc": "上等绸缎，缝制华服。"},
        {"name": "人参", "desc": "老山参，吊命圣药。"},
        {"name": "沉香木", "desc": "沉香木，制作器具的常用材料。"},
    ],
    "modern": [
        {"name": "棉布料", "desc": "普通棉布料，缝制衣物的基底。"},
        {"name": "防弹纤维", "desc": "防弹纤维布，精良护具的辅材。"},
        {"name": "常用药材", "desc": "常用药材，炼制初级药剂。"},
        {"name": "稀有生物碱", "desc": "稀有生物碱，史诗药剂的关键材料。"},
        {"name": "工程塑料", "desc": "工程塑料，制作装备的常用材料。"},
        {"name": "涤纶布", "desc": "涤纶布料，缝制衣物。"},
        {"name": "抗生素", "desc": "抗生素原料，炼制药品。"},
        {"name": "玻璃纤维", "desc": "玻璃纤维，制作装备的材料。"},
    ],
    "scifi": [
        {"name": "合成纤维", "desc": "合成纤维布，缝制作业服的基底。"},
        {"name": "相变织物", "desc": "相变织物，精良防护服的辅材。"},
        {"name": "培养菌丝", "desc": "培养菌丝，炼制初级药剂。"},
        {"name": "异星孢子", "desc": "异星孢子，史诗药剂的关键材料。"},
        {"name": "复合树脂", "desc": "复合树脂，制作装备的常用材料。"},
        {"name": "纳米织物", "desc": "纳米织物，缝制高级作业服。"},
        {"name": "合成蛋白", "desc": "合成蛋白，炼制药剂。"},
        {"name": "晶态树脂", "desc": "晶态树脂，制作装备的材料。"},
    ],
    "apocalypse": [
        {"name": "破布条", "desc": "撕成的破布条，缝制衣物的基底。"},
        {"name": "防化纤维", "desc": "防化纤维布，精良护具的辅材。"},
        {"name": "干草药", "desc": "晒干的草药，炼制初级药剂。"},
        {"name": "变异孢子", "desc": "变异孢子，史诗药剂的关键材料。"},
        {"name": "废塑料", "desc": "回收的废塑料，制作装备的常用材料。"},
        {"name": "帆布", "desc": "结实的帆布，缝制衣物。"},
        {"name": "活性炭", "desc": "活性炭，净水炼药。"},
        {"name": "铝材", "desc": "回收的铝材，制作器具的材料。"},
    ],
}

# 装备前缀名池（P34b 鉴定/洗练赋前缀名单一来源）：按 affix 修饰维度分组。
# 维度 key：atk=攻击 / def=防御 / crit=暴击 / magic=魔法攻击 / stat=属性加成。
# 每维度每题材 >=4 个题材化前缀名。鉴定时 roll_affixes 据维度从此池取前缀名填 affix["name"]。
_GENRE_AFFIX_PREFIX_NAMES: dict[str, dict[str, list[str]]] = {
    "western_fantasy": {
        "atk": ["锋锐", "破甲", "裂石", "嗜血", "破军", "裂空"],
        "def": ["坚固", "守护", "磐石", "拒马", "铁壁", "不摧"],
        "crit": ["致命", "精准", "裂魂", "弑神", "贯心", "碎星"],
        "magic": ["灼焰", "霜寒", "雷霆", "秘能", "烈焰", "冰霜"],
        "stat": ["勇武", "坚韧", "迅捷", "睿智", "龙力", "疾风"],
        # [装备新属性 2026-09-01] 连击/反击/吸血/元素四维前缀（词缀生成用）
        "combo": ["连斩", "疾风连击", "双锋", "影袭"],
        "counter": ["回击", "反刃", "铁棘", "破势"],
        "lifesteal": ["噬血", "汲取", "贪欲", "生吞"],
        "element": ["焚锋", "霜刃", "雷击", "潮涌"],
    },
    "xianxia": {
        "atk": ["锋锐", "裂甲", "破天", "噬魂", "灭世", "裂天"],
        "def": ["固元", "护脉", "磐石", "镇邪", "不灭", "护道"],
        "crit": ["必杀", "斩灵", "裂魂", "诛仙", "断魂", "破妄"],
        "magic": ["焚天", "玄冰", "九雷", "混元", "焚寂", "玄雷"],
        "stat": ["力魄", "体魄", "身法", "灵慧", "仙骨", "道心"],
        "combo": ["连环", "剑影连绵", "连诀", "追风"],
        "counter": ["回击", "斗转", "星移", "气旋"],
        "lifesteal": ["噬血", "纳元", "夺精", "摄魂"],
        "element": ["焚锋", "玄霜", "紫雷", "御水"],
    },
    "wuxia": {
        "atk": ["锋利", "破甲", "裂石", "饮血", "断岳", "裂云"],
        "def": ["厚实", "护体", "铁壁", "金钟", "铜墙", "不破"],
        "crit": ["要害", "精准", "绝杀", "封喉", "无影", "断喉"],
        "magic": ["烈焰", "寒冰", "惊雷", "玄机", "离火", "巽风"],
        "stat": ["力士", "铜皮", "飞燕", "机敏", "铁骨", "猿臂"],
        "combo": ["连击", "快剑", "连绵", "双斩"],
        "counter": ["回手", "卸力", "反刺", "借力"],
        "lifesteal": ["吸血", "养气", "回春", "纳气"],
        "element": ["火淬", "冰淬", "雷淬", "水淬"],
    },
    "modern": {
        "atk": ["高伤", "穿甲", "破墙", "致死", "毁灭", "贯穿"],
        "def": ["加固", "防弹", "重装", "防爆", "钢铁", "堡垒"],
        "crit": ["精准", "致命", "爆头", "锁眼", "斩首", "锁喉"],
        "magic": ["燃烧", "冷冻", "电击", "辐射", "烈焰", "寒潮"],
        "stat": ["强力", "耐久", "敏捷", "智能", "体能", "灵巧"],
        "combo": ["连发", "速射", "双击", "压制"],
        "counter": ["反击", "回手", "反制", "格反"],
        "lifesteal": ["吸血", "汲取", "自愈", "掠夺"],
        "element": ["燃弹", "冻弹", "电弹", "水压"],
    },
    "scifi": {
        "atk": ["高能", "穿透", "裂解", "湮灭", "碎星", "贯穿"],
        "def": ["强化", "护盾", "重甲", "力场", "壁垒", "相位"],
        "crit": ["锁定", "精准", "核心", "过载", "核芯", "碎魂"],
        "magic": ["等离子", "低温", "电磁", "辐射", "反物质", "超导"],
        "stat": ["动力", "装甲", "推进", "运算", "机甲", "神经"],
        "combo": ["连锁", "连射", "并联", "过载连击"],
        "counter": ["回能", "反冲", "镜反", "逆流"],
        "lifesteal": ["吸取", "采集", "回收", "吞噬"],
        "element": ["离子", "低温", "电磁", "声呐"],
    },
    "apocalypse": {
        "atk": ["锐利", "破甲", "碎骨", "嗜血", "撕裂", "碎甲"],
        "def": ["加固", "防刺", "厚板", "铁皮", "堡垒", "钢板"],
        "crit": ["要害", "精准", "绝杀", "封喉", "斩首", "爆头"],
        "magic": ["燃烧", "腐蚀", "电击", "变异", "毒雾", "静电"],
        "stat": ["蛮力", "皮糙", "迅捷", "警觉", "暴走", "铁血"],
        "combo": ["连击", "撕咬", "连环", "扑杀"],
        "counter": ["反咬", "回击", "硬反", "暴起"],
        "lifesteal": ["噬血", "吞噬", "啃食", "回血"],
        "element": ["酸蚀", "灼烧", "电涌", "病毒"],
    },
}


def _p34a_genre_id(world: "World") -> str:
    """[P34a] 读世界题材 id（config_overlay.attribute_template_id，兜底 western_fantasy）。

    与 _genre_id/_gather_verb 等同口径；独立函数避免与现有 self._genre_id（实例方法）
    冲突，供模块级池查询 helper 复用。
    """
    ov = getattr(world, "config_overlay", None) or {}
    return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")


def cultivate_pool(world: "World") -> list[dict]:
    """[P34a] 取当前世界题材的培养 reagent 池（鉴定卷轴/洗练石/资质丹），缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    return list(_GENRE_CULTIVATE_ITEMS.get(tid) or _GENRE_CULTIVATE_ITEMS["western_fantasy"])


def forge_material_pool(world: "World") -> list[dict]:
    """[P34a] 取当前世界题材的锻造材料池，缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    return list(_GENRE_FORGE_MATERIALS.get(tid) or _GENRE_FORGE_MATERIALS["western_fantasy"])


def craft_material_pool(world: "World") -> list[dict]:
    """[P34a] 取当前世界题材的制造材料池，缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    return list(_GENRE_CRAFT_MATERIALS.get(tid) or _GENRE_CRAFT_MATERIALS["western_fantasy"])


def affix_prefix_pool(world: "World") -> dict[str, list[str]]:
    """[P34a] 取当前世界题材的装备前缀名池（按维度分组），缺键回退西幻。"""
    tid = _p34a_genre_id(world)
    src = _GENRE_AFFIX_PREFIX_NAMES.get(tid) or _GENRE_AFFIX_PREFIX_NAMES["western_fantasy"]
    return {k: list(v) for k, v in src.items()}

# ---- [D2 2026-08-29] 据点设施起名 LLM（calculator 小调用；内联常量不进 preset，
# 无 PROMPT_DEFAULTS_REV 版本门——一次性小调用，失败兜底题材池名）----
_DOMAIN_FACILITY_NAME_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的据点设施命名引擎。我会给你世界题材、据点信息、设施类型与命名风格参考，"
    "请为新落成的设施起名并写一句描写。\n\n"
    "只输出 JSON，不要输出任何说明或 markdown 代码块标记。\n\n"
    'JSON schema：\n{"name":"设施名","desc":"一句描写"}\n\n'
    "要求：\n"
    "1. name 2-6 字，贴合题材与据点风味。\n"
    "2. desc 一句 30 字内，写设施的样子，不写剧情。\n"
    "3. 不与命名风格参考中的名字完全重复。"
)

# ---- [D2 2026-08-29] 据点职员档案 LLM（单条；仿 P46 backfill 档案批，骨架已入职、
# 本调用只补语义档案；失败保留骨架不回滚）----
_DOMAIN_HIRE_PROFILE_SYSTEM_PROMPT = (
    "你是 SLG 游戏的 NPC 生成引擎。玩家据点招募了一名职员，请为其生成完整人物档案。"
    "输出严格 JSON，不要任何说明或代码块标记。\n\n"
    "JSON schema：\n"
    '{"profile": {"name": "姓名(2-12字，符合题材命名风格，可与占位名不同)", '
    '"role": "身份(一句话，须体现其职业：掌柜/伙计/打手/农夫的题材化说法)", '
    '"personality": "性格(30字内)", "goal": "目标(30字内)", "appearance": "外貌(60字内)", '
    '"speech_style": "说话风格(50字内：整体语气与用词倾向的泛义描述，如「语气冷硬、爱用反问」；与其他 NPC 有区分度；禁止写具体口头禅/固定台词/每轮都要说的某句话)", '
    '"notes": "1-2句背景小传(交代来历与为何受雇于玩家的据点)"}}\n\n'
    "要求：贴合世界观；性格/腔调可辨识有区分度；不编造与设定矛盾的内容。"
)

# ---- [P7d] 世界生成物品数量：走 preset.item_gen_count（设置对话框旋钮，钳 4-200）。
# 拆分生成后不再按规模静态分档，物品数量独立成旋钮（见 _build_items_user_message）。

# ---- [P7f] 地图拓展 LLM 提示词（边界迷雾块点击 -> 生成相邻新地点）----
_EXPAND_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的地图拓展引擎。我会给你一个已探索地点的信息 + 拓展方向 + 世界题材，"
    "请你生成若干相邻的新地点，风格延续当前区域。\n\n"
    "只输出 JSON，不要输出任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"locations":[{"name":"地点名","desc":"1-2句描述","region":"区域名",'
    '"danger":1到10的整数,"connections":["本次其他新地点名"]}]}\n\n'
    "要求：\n"
    "1. 新地点风格延续给定的区域/题材（修仙区域仍是灵山/洞府/坊市/密林；现代仍是街道/商铺/住宅；末日仍是废墟/避难所）。\n"
    "2. connections 只能引用本次生成的其他新地点名（横向连通）；与起点的连通由引擎自动建立，不要写起点名。\n"
    "3. danger 合理（边缘未知地带可略高于起点）。\n"
    "4. 按指定数量生成，地点名不与起点重复、彼此不重复、也不与已给出的「已有地点」列表中任何地名重复（避免重名混淆）。\n"
    "5. 内容遵循世界观基调与 NSFW 设定。"
)

# ---- [P12] 地图拓展 LLM 提示词更新（kind 分流 + 野外资源分层）----
_EXPAND_SYSTEM_PROMPT = _EXPAND_SYSTEM_PROMPT.replace(
    '"danger":1到10的整数,"connections"',
    '"danger":1到10的整数,"kind":"settlement或wilderness（聚落无resource；野外必须带resource）",'
    '"resource":{"name":"资源点名","type":"资源类型","desc":"1句描述","tier":1-5},'
    '"dungeon":{"name":"秘境名(可选,约四分之一的新野外地点适合带一个,题材化命名,如「上古剑冢」「废弃地铁隧道」)",'
    '"theme_hint":"主题一句话(可选)"},'
    '"places":[{"name":"场所名","type":"场所类型","desc":"1句描述","connections":["同地点其他场所名"]}],'
    '"connections"',
).replace(
    "3. danger 合理（边缘未知地带可略高于起点）。",
    "3. danger 合理（边缘未知地带可略高于起点）；resource.tier 随 danger 分层"
    "（1-2->1 / 3-4->2 / 5-6->3 / 7-8->4 / 9-10->5）；"
    "dungeon 可选，整批至多 1-2 个地点带（秘境 = 供玩家反复探索的地下空间）。",
).replace(
    "5. 内容遵循世界观基调与 NSFW 设定。",
    "5. 内容遵循世界观基调与 NSFW 设定。\n"
    "6. 每个新地点须给出 places 子结构：3-5 个场所（聚落出活动场所、野外出自然场所），"
    "名/类型/desc 题材化且本地点内不重名；places.connections 只能引用同地点内其他场所名（不跨地点、不引本次其他新地点）。",
)

# ---- [P12] 拓展伴随配方生成提示词（资源-配方联动）----
_EXPAND_RECIPE_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的配方生成引擎。我会给你新开辟地点的资源（层级 1-5）和现有物品清单，"
    "请生成消耗这些资源的新合成配方。\n\n"
    "只输出 JSON，不要输出任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"recipes":[{"name":"配方名","desc":"1句描述","inputs":["物品名","物品名"],'
    '"output":"产出物品名","difficulty":0-50的整数}]}\n\n'
    "要求：\n"
    "1. inputs/output 必须引用现有物品清单里的真实名字（material 优先作 inputs）。\n"
    "2. 每个新地点至少 1 个配方消耗其资源；产出贴合资源层级（高层级资源出高品级产出）。\n"
    "3. difficulty 按产出稀有度：common=0 / uncommon=8 / rare=20 / epic=35 / legendary=50。\n"
    "4. 名字贴合世界观基调。"
)

# ---- [P13] 题材技能兜底池（LLM player_start.skill_pool 缺失/非法时用；每题材 14 个覆盖
# attack/heal/buff、str/dex/int 缩放、多元素、5 档稀有度（common 4/uncommon 2/rare 3/epic 3/
# legendary 2）；power 按 rarity 档位给。skill_pool_size 足够大时全量入池，长线不重样----
_GENRE_SKILL_TEMPLATES: dict[str, list[dict]] = {
    "xianxia": [
        {"name": "御剑术", "desc": "凝灵力驭剑伤敌。", "type": "attack", "power": 14, "cost_mp": 6, "cooldown": 1, "rarity": "common", "element": "metal", "stat_scaling": "int"},
        {"name": "炼气诀", "desc": "调息回气。", "type": "heal", "power": 20, "cost_mp": 8, "cooldown": 2, "rarity": "common", "element": "", "stat_scaling": "int"},
        {"name": "尘遁术", "desc": "借土遁走位，护身周全。", "type": "buff", "power": 8, "cost_mp": 4, "cooldown": 3, "rarity": "common", "element": "earth", "stat_scaling": "dex"},
        {"name": "引气诀", "desc": "纳灵入体，回复灵力。", "type": "buff", "power": 12, "cost_mp": 3, "cooldown": 3, "rarity": "common", "element": "", "stat_scaling": "int"},
        {"name": "烈焰符", "desc": "掷出火符爆裂。", "type": "attack", "power": 18, "cost_mp": 8, "cooldown": 1, "rarity": "uncommon", "element": "fire", "stat_scaling": "int", "inflicts": [{"condition": "burning", "chance": 0.5, "duration": 3}]},
        {"name": "青木缠身咒", "desc": "灵藤自地而起缠敌。", "type": "attack", "power": 17, "cost_mp": 7, "cooldown": 2, "rarity": "uncommon", "element": "wood", "stat_scaling": "int", "inflicts": [{"condition": "entangled", "chance": 0.5, "duration": 2}]},
        {"name": "玄冰刺", "desc": "寒冰凝聚成刺。", "type": "attack", "power": 22, "cost_mp": 10, "cooldown": 1, "rarity": "rare", "element": "ice", "stat_scaling": "int", "inflicts": [{"condition": "chilled", "chance": 0.6, "duration": 2}, {"condition": "wet", "chance": 0.7, "duration": 2}]},
        {"name": "金钟罩", "desc": "真气护体。", "type": "buff", "power": 10, "cost_mp": 6, "cooldown": 3, "rarity": "rare", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "两仪剑阵", "desc": "双剑合璧，剑气交织。", "type": "attack", "power": 24, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "metal", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "雷法·天罚", "desc": "引九天雷殛。", "type": "attack", "power": 30, "cost_mp": 14, "cooldown": 2, "rarity": "epic", "element": "thunder", "stat_scaling": "int"},
        {"name": "枯木回春", "desc": "木灵生机疗伤。", "type": "heal", "power": 40, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "wood", "stat_scaling": "int"},
        {"name": "幽冥噬魂爪", "desc": "阴煞之气蚀敌神魂。", "type": "attack", "power": 29, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "dark", "stat_scaling": "int", "inflicts": [{"condition": "feared", "chance": 0.4, "duration": 2}, {"condition": "poisoned", "chance": 0.5, "duration": 3}]},
        {"name": "大衍神雷", "desc": "传承大神通，雷海覆敌。", "type": "attack", "power": 38, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "thunder", "stat_scaling": "int", "target_pattern": "aoe_enemy", "inflicts": [{"condition": "stunned", "chance": 0.2, "duration": 1}]},
        {"name": "太上忘情章", "desc": "太上忘情，万法不侵。", "type": "buff", "power": 16, "cost_mp": 14, "cooldown": 4, "rarity": "legendary", "element": "light", "stat_scaling": "int"},
        {"name": "御风诀", "desc": "御风而行，身轻如燕。", "type": "buff", "power": 9, "cost_mp": 4, "cooldown": 2, "rarity": "uncommon", "element": "wind", "stat_scaling": "dex"},
        {"name": "金虹贯日", "desc": "凝金虹剑气贯穿敌人。", "type": "attack", "power": 21, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "metal", "stat_scaling": "int"},
        {"name": "厚土遁甲", "desc": "以土灵护体，减伤固本。", "type": "buff", "power": 11, "cost_mp": 7, "cooldown": 3, "rarity": "rare", "element": "earth", "stat_scaling": "vit", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "青莲剑歌", "desc": "剑气如青莲绽放，斩灭群敌。", "type": "attack", "power": 39, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "wood", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "万法归一", "desc": "融万法于一炉，一剑破尽天下法。", "type": "attack", "power": 48, "cost_mp": 18, "cooldown": 3, "rarity": "mythic", "element": "metal", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
    ],
    "wuxia": [
        {"name": "基础剑招", "desc": "朴实剑招。", "type": "attack", "power": 13, "cost_mp": 3, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str"},
        {"name": "吐纳法", "desc": "内功调息。", "type": "heal", "power": 18, "cost_mp": 6, "cooldown": 2, "rarity": "common", "element": "", "stat_scaling": "int"},
        {"name": "稳马桩", "desc": "扎马固元，硬接招式。", "type": "buff", "power": 8, "cost_mp": 4, "cooldown": 3, "rarity": "common", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "撒手锏", "desc": "趁隙突袭的短打。", "type": "attack", "power": 14, "cost_mp": 4, "cooldown": 2, "rarity": "common", "element": "physical", "stat_scaling": "dex", "inflicts": [{"condition": "poisoned", "chance": 0.3, "duration": 3}]},
        {"name": "燕子三抄水", "desc": "轻功身法连击。", "type": "attack", "power": 17, "cost_mp": 4, "cooldown": 1, "rarity": "uncommon", "element": "physical", "stat_scaling": "dex"},
        {"name": "铁砂掌", "desc": "苦练掌力，拍石留印。", "type": "attack", "power": 19, "cost_mp": 6, "cooldown": 1, "rarity": "uncommon", "element": "physical", "stat_scaling": "str", "inflicts": [{"condition": "bleeding", "chance": 0.4, "duration": 3}]},
        {"name": "分筋错骨手", "desc": "近身擒拿重创。", "type": "attack", "power": 22, "cost_mp": 6, "cooldown": 2, "rarity": "rare", "element": "physical", "stat_scaling": "str", "inflicts": [{"condition": "entangled", "chance": 0.5, "duration": 2}]},
        {"name": "铁布衫", "desc": "横练硬功。", "type": "buff", "power": 10, "cost_mp": 5, "cooldown": 3, "rarity": "rare", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "流云飞袖", "desc": "长袖如鞭，拂穴封脉。", "type": "attack", "power": 23, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "physical", "stat_scaling": "dex", "inflicts": [{"condition": "blinded", "chance": 0.4, "duration": 2}]},
        {"name": "夺命十五剑", "desc": "杀招连绵。", "type": "attack", "power": 28, "cost_mp": 10, "cooldown": 2, "rarity": "epic", "element": "physical", "stat_scaling": "dex", "target_pattern": "aoe_enemy"},
        {"name": "易筋经", "desc": "内功至宝，洗髓回元。", "type": "heal", "power": 38, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "", "stat_scaling": "int"},
        {"name": "霸王卸甲", "desc": "刚猛绝技，破甲摧锋。", "type": "attack", "power": 30, "cost_mp": 11, "cooldown": 2, "rarity": "epic", "element": "physical", "stat_scaling": "str", "inflicts": [{"condition": "bleeding", "chance": 0.5, "duration": 3}, {"condition": "stunned", "chance": 0.25, "duration": 1}]},
        {"name": "独孤九剑", "desc": "无招胜有招。", "type": "attack", "power": 36, "cost_mp": 12, "cooldown": 2, "rarity": "legendary", "element": "physical", "stat_scaling": "str", "target_pattern": "aoe_enemy"},
        {"name": "金刚不坏体", "desc": "佛门神功，刀枪不入。", "type": "buff", "power": 15, "cost_mp": 13, "cooldown": 4, "rarity": "legendary", "element": "light", "stat_scaling": "vit", "inflicts": [{"condition": "enraged", "chance": 1.0, "duration": 3}]},
        {"name": "踏雪无痕", "desc": "轻功身法，来去如风。", "type": "buff", "power": 9, "cost_mp": 4, "cooldown": 2, "rarity": "uncommon", "element": "wind", "stat_scaling": "dex"},
        {"name": "亢龙有悔", "desc": "掌力浑厚，一掌拍出。", "type": "attack", "power": 24, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "physical", "stat_scaling": "str"},
        {"name": "回春散", "desc": "以内力催发药力疗伤。", "type": "heal", "power": 30, "cost_mp": 10, "cooldown": 2, "rarity": "rare", "element": "wood", "stat_scaling": "int"},
        {"name": "左右互搏", "desc": "双手分使两套招式，攻势如潮。", "type": "attack", "power": 37, "cost_mp": 12, "cooldown": 2, "rarity": "legendary", "element": "physical", "stat_scaling": "dex", "target_pattern": "aoe_enemy"},
        {"name": "六脉神剑", "desc": "无形剑气，六脉齐发，例无虚发。", "type": "attack", "power": 46, "cost_mp": 16, "cooldown": 3, "rarity": "mythic", "element": "physical", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
    ],
    "modern": [
        {"name": "格斗术", "desc": "军体拳格斗。", "type": "attack", "power": 13, "cost_mp": 3, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str"},
        {"name": "急救包扎", "desc": "战场急救。", "type": "heal", "power": 18, "cost_mp": 5, "cooldown": 2, "rarity": "common", "element": "", "stat_scaling": "int"},
        {"name": "低姿匍匐", "desc": "压低身形减少受创。", "type": "buff", "power": 8, "cost_mp": 3, "cooldown": 3, "rarity": "common", "element": "", "stat_scaling": "dex", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "肘击", "desc": "近身肘击要害。", "type": "attack", "power": 14, "cost_mp": 3, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str", "inflicts": [{"condition": "bleeding", "chance": 0.4, "duration": 3}]},
        {"name": "快速射击", "desc": "腰间拔枪速射。", "type": "attack", "power": 18, "cost_mp": 4, "cooldown": 1, "rarity": "uncommon", "element": "physical", "stat_scaling": "dex", "inflicts": [{"condition": "bleeding", "chance": 0.5, "duration": 3}]},
        {"name": "电击器突刺", "desc": "电击器直捅要害。", "type": "attack", "power": 17, "cost_mp": 5, "cooldown": 1, "rarity": "uncommon", "element": "thunder", "stat_scaling": "dex"},
        {"name": "战术翻滚", "desc": "规避反击。", "type": "buff", "power": 10, "cost_mp": 4, "cooldown": 3, "rarity": "rare", "element": "", "stat_scaling": "dex", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 2}]},
        {"name": "狙击要害", "desc": "冷静一击。", "type": "attack", "power": 24, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "physical", "stat_scaling": "dex", "inflicts": [{"condition": "bleeding", "chance": 0.6, "duration": 3}]},
        {"name": "震撼弹", "desc": "闪光震慑，趁乱输出。", "type": "attack", "power": 22, "cost_mp": 7, "cooldown": 2, "rarity": "rare", "element": "light", "stat_scaling": "int", "inflicts": [{"condition": "feared", "chance": 0.5, "duration": 2}, {"condition": "stunned", "chance": 0.3, "duration": 1}]},
        {"name": "CQB 突入", "desc": "近距作战连招。", "type": "attack", "power": 28, "cost_mp": 10, "cooldown": 2, "rarity": "epic", "element": "physical", "stat_scaling": "str", "target_pattern": "aoe_enemy"},
        {"name": "战地手术", "desc": "妙手回春。", "type": "heal", "power": 36, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "", "stat_scaling": "int"},
        {"name": "无人机蜂群", "desc": "召唤侦察无人机自爆打击。", "type": "attack", "power": 29, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "thunder", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "死亡风暴", "desc": "火力覆盖。", "type": "attack", "power": 36, "cost_mp": 14, "cooldown": 3, "rarity": "legendary", "element": "physical", "stat_scaling": "dex", "target_pattern": "aoe_enemy"},
        {"name": "肾上腺素鸡尾酒", "desc": "药剂全开，超水平爆发。", "type": "buff", "power": 15, "cost_mp": 13, "cooldown": 4, "rarity": "legendary", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "enraged", "chance": 1.0, "duration": 3}]},
        {"name": "闪避步法", "desc": "脚下灵活，闪避来袭。", "type": "buff", "power": 9, "cost_mp": 3, "cooldown": 2, "rarity": "uncommon", "element": "", "stat_scaling": "dex"},
        {"name": "破门炸药", "desc": "定点爆破破开防御。", "type": "attack", "power": 23, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "fire", "stat_scaling": "str", "inflicts": [{"condition": "burning", "chance": 0.5, "duration": 3}]},
        {"name": "镇定剂注射", "desc": "注射镇定剂稳住伤势。", "type": "heal", "power": 32, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "", "stat_scaling": "int"},
        {"name": "电磁脉冲弹", "desc": "释放 EMP 瘫痪电子设备。", "type": "attack", "power": 36, "cost_mp": 14, "cooldown": 3, "rarity": "legendary", "element": "thunder", "stat_scaling": "int", "target_pattern": "aoe_enemy", "inflicts": [{"condition": "stunned", "chance": 0.25, "duration": 1}]},
        {"name": "超限火力", "desc": "倾泻全部弹药火力的饱和打击。", "type": "attack", "power": 45, "cost_mp": 15, "cooldown": 3, "rarity": "mythic", "element": "physical", "stat_scaling": "dex", "target_pattern": "aoe_enemy"},
    ],
    "scifi": [
        {"name": "电击脉冲", "desc": "外骨骼释放电流。", "type": "attack", "power": 14, "cost_mp": 5, "cooldown": 1, "rarity": "common", "element": "thunder", "stat_scaling": "int"},
        {"name": "纳米修复", "desc": "纳米机器人疗伤。", "type": "heal", "power": 20, "cost_mp": 7, "cooldown": 2, "rarity": "common", "element": "", "stat_scaling": "int"},
        {"name": "过载护盾", "desc": "护盾短时过载增厚。", "type": "buff", "power": 8, "cost_mp": 4, "cooldown": 3, "rarity": "common", "element": "", "stat_scaling": "int", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "推进冲刺", "desc": "背包推进器瞬移贴脸。", "type": "attack", "power": 14, "cost_mp": 4, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str"},
        {"name": "磁轨狙击", "desc": "电磁加速弹头。", "type": "attack", "power": 19, "cost_mp": 6, "cooldown": 1, "rarity": "uncommon", "element": "physical", "stat_scaling": "dex", "inflicts": [{"condition": "bleeding", "chance": 0.5, "duration": 3}]},
        {"name": "声波震荡", "desc": "定向声波震慑内脏。", "type": "attack", "power": 17, "cost_mp": 6, "cooldown": 1, "rarity": "uncommon", "element": "wind", "stat_scaling": "int", "inflicts": [{"condition": "feared", "chance": 0.4, "duration": 2}, {"condition": "stunned", "chance": 0.25, "duration": 1}]},
        {"name": "力场护盾", "desc": "展开偏导力场。", "type": "buff", "power": 10, "cost_mp": 6, "cooldown": 3, "rarity": "rare", "element": "", "stat_scaling": "int", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "等离子切割", "desc": "高温等离子束。", "type": "attack", "power": 24, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "fire", "stat_scaling": "int", "inflicts": [{"condition": "burning", "chance": 0.5, "duration": 3}]},
        {"name": "腐蚀弹头", "desc": "酸蚀弹头持续溶甲。", "type": "attack", "power": 22, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "wood", "stat_scaling": "dex", "inflicts": [{"condition": "entangled", "chance": 0.5, "duration": 2}, {"condition": "poisoned", "chance": 0.5, "duration": 3}]},
        {"name": "冷冻光束", "desc": "绝对零度射线。", "type": "attack", "power": 28, "cost_mp": 11, "cooldown": 2, "rarity": "epic", "element": "ice", "stat_scaling": "int", "inflicts": [{"condition": "chilled", "chance": 0.6, "duration": 2}, {"condition": "wet", "chance": 0.7, "duration": 2}]},
        {"name": "基因再生", "desc": "激活再生序列。", "type": "heal", "power": 38, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "wood", "stat_scaling": "int"},
        {"name": "轨道支援打击", "desc": "呼叫轨道炮精准覆盖。", "type": "attack", "power": 30, "cost_mp": 13, "cooldown": 2, "rarity": "epic", "element": "light", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "反物质湮灭", "desc": "战术级毁灭。", "type": "attack", "power": 40, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "dark", "stat_scaling": "int", "inflicts": [{"condition": "feared", "chance": 0.5, "duration": 2}]},
        {"name": "相位偏移矩阵", "desc": "短时相位化，攻击难以命中。", "type": "buff", "power": 15, "cost_mp": 14, "cooldown": 4, "rarity": "legendary", "element": "", "stat_scaling": "dex", "inflicts": [{"condition": "enraged", "chance": 1.0, "duration": 3}]},
        {"name": "机动推进", "desc": "短距喷射机动。", "type": "buff", "power": 9, "cost_mp": 4, "cooldown": 2, "rarity": "uncommon", "element": "", "stat_scaling": "dex"},
        {"name": "裂解光束", "desc": "分子裂解射线切割目标。", "type": "attack", "power": 23, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "light", "stat_scaling": "int"},
        {"name": "维生凝胶", "desc": "注入维生凝胶修复组织。", "type": "heal", "power": 33, "cost_mp": 10, "cooldown": 2, "rarity": "rare", "element": "wood", "stat_scaling": "int"},
        {"name": "重力井", "desc": "生成重力井碾压敌人。", "type": "attack", "power": 38, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "dark", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "奇点湮灭", "desc": "制造微型奇点，吞噬范围内一切。", "type": "attack", "power": 50, "cost_mp": 18, "cooldown": 3, "rarity": "mythic", "element": "dark", "stat_scaling": "int", "target_pattern": "aoe_enemy", "inflicts": [{"condition": "feared", "chance": 0.5, "duration": 2}]},
    ],
    "apocalypse": [
        {"name": "拼刺", "desc": "磨尖钢筋突刺。", "type": "attack", "power": 13, "cost_mp": 3, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str"},
        {"name": "草药包扎", "desc": "野生草药处理伤口。", "type": "heal", "power": 18, "cost_mp": 5, "cooldown": 2, "rarity": "common", "element": "wood", "stat_scaling": "int"},
        {"name": "绷带缠身", "desc": "多层绷带护住要害。", "type": "buff", "power": 8, "cost_mp": 3, "cooldown": 3, "rarity": "common", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "泼沙迷眼", "desc": "一把沙子扰乱视线。", "type": "attack", "power": 13, "cost_mp": 3, "cooldown": 1, "rarity": "common", "element": "earth", "stat_scaling": "dex", "inflicts": [{"condition": "blinded", "chance": 0.5, "duration": 2}]},
        {"name": "投掷炸瓶", "desc": "燃烧瓶投掷。", "type": "attack", "power": 18, "cost_mp": 5, "cooldown": 1, "rarity": "uncommon", "element": "fire", "stat_scaling": "dex", "inflicts": [{"condition": "burning", "chance": 0.5, "duration": 3}]},
        {"name": "信号弹强光", "desc": "信号弹灼射双目。", "type": "attack", "power": 17, "cost_mp": 5, "cooldown": 1, "rarity": "uncommon", "element": "light", "stat_scaling": "dex", "inflicts": [{"condition": "feared", "chance": 0.4, "duration": 2}]},
        {"name": "变异体质", "desc": "辐射强化肉体。", "type": "buff", "power": 10, "cost_mp": 5, "cooldown": 3, "rarity": "rare", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "enraged", "chance": 1.0, "duration": 3}]},
        {"name": "废铁风暴", "desc": "碎片旋风。", "type": "attack", "power": 24, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "metal", "stat_scaling": "str", "inflicts": [{"condition": "bleeding", "chance": 0.5, "duration": 3}]},
        {"name": "毒藻飞刀", "desc": "淬毒变异藻片飞刀。", "type": "attack", "power": 22, "cost_mp": 7, "cooldown": 2, "rarity": "rare", "element": "wood", "stat_scaling": "dex", "inflicts": [{"condition": "entangled", "chance": 0.5, "duration": 2}, {"condition": "poisoned", "chance": 0.5, "duration": 3}]},
        {"name": "嗜血狂击", "desc": "痛觉屏蔽狂攻。", "type": "attack", "power": 28, "cost_mp": 10, "cooldown": 2, "rarity": "epic", "element": "physical", "stat_scaling": "str", "inflicts": [{"condition": "bleeding", "chance": 0.6, "duration": 3}]},
        {"name": "血清再生", "desc": "注入实验血清。", "type": "heal", "power": 36, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "", "stat_scaling": "int"},
        {"name": "酸液喷壶", "desc": "改装喷壶泼洒强酸。", "type": "attack", "power": 29, "cost_mp": 11, "cooldown": 2, "rarity": "epic", "element": "wood", "stat_scaling": "int", "inflicts": [{"condition": "entangled", "chance": 0.5, "duration": 2}]},
        {"name": "核能过载", "desc": "同位素心脏过载。", "type": "attack", "power": 38, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "thunder", "stat_scaling": "str", "target_pattern": "aoe_enemy", "inflicts": [{"condition": "stunned", "chance": 0.2, "duration": 1}]},
        {"name": "病毒母巢同化", "desc": "短暂同化变异群落，血肉重生。", "type": "heal", "power": 40, "cost_mp": 14, "cooldown": 3, "rarity": "legendary", "element": "dark", "stat_scaling": "vit"},
        {"name": "潜行贴近", "desc": "压低动静接近目标。", "type": "buff", "power": 9, "cost_mp": 3, "cooldown": 2, "rarity": "uncommon", "element": "", "stat_scaling": "dex"},
        {"name": "燃烧弹投掷", "desc": "自制燃烧弹轰击。", "type": "attack", "power": 23, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "fire", "stat_scaling": "dex", "inflicts": [{"condition": "burning", "chance": 0.5, "duration": 3}]},
        {"name": "战地缝合", "desc": "粗针缝合伤口止血。", "type": "heal", "power": 32, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "", "stat_scaling": "int"},
        {"name": "尸潮召唤", "desc": "短暂驱使尸潮冲击。", "type": "attack", "power": 37, "cost_mp": 14, "cooldown": 3, "rarity": "legendary", "element": "dark", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "末世审判", "desc": "引动尸潮与晶核能量，席卷残存世界。", "type": "attack", "power": 47, "cost_mp": 17, "cooldown": 3, "rarity": "mythic", "element": "dark", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
    ],
    "western_fantasy": [
        {"name": "斩击", "desc": "基础剑术。", "type": "attack", "power": 13, "cost_mp": 3, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str"},
        {"name": "治愈之光", "desc": "圣光疗伤。", "type": "heal", "power": 20, "cost_mp": 7, "cooldown": 2, "rarity": "common", "element": "light", "stat_scaling": "int"},
        {"name": "盾墙格挡", "desc": "举盾结墙硬抗。", "type": "buff", "power": 8, "cost_mp": 3, "cooldown": 3, "rarity": "common", "element": "", "stat_scaling": "vit", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 3}]},
        {"name": "破甲锤击", "desc": "战锤砸击护甲。", "type": "attack", "power": 14, "cost_mp": 4, "cooldown": 1, "rarity": "common", "element": "physical", "stat_scaling": "str", "inflicts": [{"condition": "bleeding", "chance": 0.4, "duration": 3}]},
        {"name": "火球术", "desc": "经典塑能法术。", "type": "attack", "power": 18, "cost_mp": 7, "cooldown": 1, "rarity": "uncommon", "element": "fire", "stat_scaling": "int", "inflicts": [{"condition": "burning", "chance": 0.5, "duration": 3}]},
        {"name": "荆棘缠绕", "desc": "藤蔓破土缠敌。", "type": "attack", "power": 17, "cost_mp": 6, "cooldown": 1, "rarity": "uncommon", "element": "wood", "stat_scaling": "int", "inflicts": [{"condition": "entangled", "chance": 0.5, "duration": 2}]},
        {"name": "疾风步", "desc": "风灵加身。", "type": "buff", "power": 10, "cost_mp": 5, "cooldown": 3, "rarity": "rare", "element": "wind", "stat_scaling": "dex", "inflicts": [{"condition": "protected", "chance": 1.0, "duration": 2}]},
        {"name": "寒冰箭", "desc": "冰霜穿刺。", "type": "attack", "power": 24, "cost_mp": 9, "cooldown": 2, "rarity": "rare", "element": "ice", "stat_scaling": "int", "inflicts": [{"condition": "chilled", "chance": 0.6, "duration": 2}, {"condition": "wet", "chance": 0.7, "duration": 2}]},
        {"name": "大地践踏", "desc": "震地波撼倒周围之敌。", "type": "attack", "power": 22, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "earth", "stat_scaling": "str", "target_pattern": "aoe_enemy"},
        {"name": "圣光裁决", "desc": "审判之锤。", "type": "attack", "power": 28, "cost_mp": 11, "cooldown": 2, "rarity": "epic", "element": "light", "stat_scaling": "int", "inflicts": [{"condition": "feared", "chance": 0.4, "duration": 2}, {"condition": "stunned", "chance": 0.25, "duration": 1}]},
        {"name": "生命祝福", "desc": "强效治疗。", "type": "heal", "power": 38, "cost_mp": 12, "cooldown": 2, "rarity": "epic", "element": "wood", "stat_scaling": "int"},
        {"name": "暗影噬咬", "desc": "影中獠牙撕扯要害。", "type": "attack", "power": 29, "cost_mp": 11, "cooldown": 2, "rarity": "epic", "element": "dark", "stat_scaling": "dex", "inflicts": [{"condition": "feared", "chance": 0.5, "duration": 2}, {"condition": "poisoned", "chance": 0.5, "duration": 3}]},
        {"name": "陨星术", "desc": "召唤陨星。", "type": "attack", "power": 40, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "fire", "stat_scaling": "int", "target_pattern": "aoe_enemy"},
        {"name": "巨龙咆哮", "desc": "龙威震慑，攻防俱涨。", "type": "buff", "power": 15, "cost_mp": 14, "cooldown": 4, "rarity": "legendary", "element": "fire", "stat_scaling": "vit", "inflicts": [{"condition": "enraged", "chance": 1.0, "duration": 3}]},
        {"name": "轻羽术", "desc": "风灵托举，身轻如燕。", "type": "buff", "power": 9, "cost_mp": 4, "cooldown": 2, "rarity": "uncommon", "element": "wind", "stat_scaling": "dex"},
        {"name": "冰锥术", "desc": "寒冰锥刺穿敌人。", "type": "attack", "power": 21, "cost_mp": 8, "cooldown": 2, "rarity": "rare", "element": "ice", "stat_scaling": "int", "inflicts": [{"condition": "chilled", "chance": 0.5, "duration": 2}]},
        {"name": "圣疗术", "desc": "圣光抚愈创伤。", "type": "heal", "power": 34, "cost_mp": 11, "cooldown": 2, "rarity": "rare", "element": "light", "stat_scaling": "int"},
        {"name": "大地之怒", "desc": "唤醒大地震裂群敌。", "type": "attack", "power": 39, "cost_mp": 15, "cooldown": 3, "rarity": "legendary", "element": "earth", "stat_scaling": "str", "target_pattern": "aoe_enemy"},
        {"name": "神临术", "desc": "召唤神明之力短暂降世，涤荡群敌。", "type": "attack", "power": 49, "cost_mp": 18, "cooldown": 3, "rarity": "mythic", "element": "light", "stat_scaling": "int", "target_pattern": "aoe_enemy", "inflicts": [{"condition": "feared", "chance": 0.5, "duration": 2}]},
    ],
}

# [P13] 题材化技能书后缀（书名 = 技能名 + 后缀；商店/掉落/奇遇的书都长这样）。
# 按技能 type 分化（attack/heal/buff），default 兜底未知类型 -> 书名更有品种感。
_GENRE_BOOK_SUFFIX = {
    "xianxia": {"attack": "秘籍", "heal": "丹方", "buff": "图谱", "default": "秘籍"},
    "wuxia": {"attack": "谱", "heal": "药典", "buff": "口诀", "default": "谱"},
    "modern": {"attack": "教程", "heal": "手册", "buff": "笔记", "default": "教程"},
    "scifi": {"attack": "芯片", "heal": "程序", "buff": "固件", "default": "芯片"},
    "apocalypse": {"attack": "手记", "heal": "偏方", "buff": "指南", "default": "手记"},
    "western_fantasy": {"attack": "卷轴", "heal": "祷文", "buff": "符文页", "default": "卷轴"},
}

# ---- [数据量铁律] gen 提示词命名参考：从引擎题材池确定性取样注入 ----
# 单一来源：池扩容/改名后提示词自动跟进，不再手写示例清单漂移。均匀取样覆盖各档位。
_GENRE_ZH = {"xianxia": "仙侠", "wuxia": "武侠", "modern": "现代",
             "scifi": "科幻", "apocalypse": "末日", "western_fantasy": "西幻"}


def _sample_names(items: list, n: int = 3) -> list[str]:
    """从题材池条目均匀确定性取样名（0/中位/末位），供提示词做命名风格参考。"""
    if not items:
        return []
    if len(items) <= n:
        idxs = range(len(items))
    else:
        idxs = [round(i * (len(items) - 1) / (n - 1)) for i in range(n)]
    return [str(items[i].get("name", "")) for i in idxs if isinstance(items[i], dict) and items[i].get("name")]


# ---- [P24d] 随从差异化：role 模板白名单 + 题材关键词兜底 ----
# judge LLM 可选字段 minion_roles 缺失/非法/漏名时按随从名关键词猜 role；未命中回退 balanced。
# balanced = 既有基线（五维 x0.7 + HP x0.6）不动；speed/tank 乘数在基线上叠加（见 _build_minion_unit）。
_MINION_ROLE_VALUES = ("speed", "tank", "balanced")
_MINION_ROLE_LABELS = {"balanced": "随从", "speed": "随从·迅捷", "tank": "随从·重装"}
_GENRE_MINION_ROLE_KEYWORDS = {
    "xianxia": {
        "speed": ("剑修", "影卫", "妖狸", "游隼", "轻功", "飞遁", "迅影", "流光"),
        "tank": ("力士", "甲士", "石魔", "蛮兽", "铁卫", "罡气", "磐石", "玄龟"),
    },
    "wuxia": {
        "speed": ("快剑", "飞贼", "游侠", "燕子", "轻功", "探子", "疾风", "掠影"),
        "tank": ("镖头", "铁塔", "棍僧", "壮汉", "大汉", "护院", "金刚", "罗汉"),
    },
    "modern": {
        "speed": ("跑酷", "飞车", "斥候", "猎犬", "信使", "快手", "疾走", "飞车党"),
        "tank": ("保镖", "重甲", "防爆", "打手", "壮汉", "拳手", "铁塔", "肌肉"),
    },
    "scifi": {
        "speed": ("侦察", "无人机", "悬浮", "轻装", "猎杀", "截击", "疾速", "拦截机"),
        "tank": ("装甲", "重型", "工程", "防暴", "炮台", "装载", "重盾", "堡垒"),
    },
    "apocalypse": {
        "speed": ("疾行", "嗅尸", "斥候", "夜枭", "猎犬", "窜行者", "疾风", "幽灵"),
        "tank": ("膨胀", "重殍", "巨汉", "龟甲", "铁皮", "碾压", "巨兽", "铁壁"),
    },
    "western_fantasy": {
        "speed": ("刺客", "斥候", "游侠", "幼狼", "迅捷", "猎手", "疾影", "风刃"),
        "tank": ("盾卫", "重甲", "铁卫", "蛮兵", "巨汉", "守卫", "铁壁", "重锤"),
    },
}


def _guess_minion_role(genre_id: str, name: str) -> str:
    """[P24d] 按随从名关键词猜 role（speed/tank；未命中 balanced）。缺键题材回退西幻。"""
    if not name:
        return "balanced"
    tbl = _GENRE_MINION_ROLE_KEYWORDS.get(genre_id) or _GENRE_MINION_ROLE_KEYWORDS["western_fantasy"]
    for role in ("speed", "tank"):
        for kw in tbl.get(role) or ():
            if kw and kw in name:
                return role
    return "balanced"


# ---- [数据量] NPC 姓名库（6 题材各 >=12 全名，纯引擎命名兜底/拓展 NPC 起名用；缺键回退西幻）----
# 底层各系统为 LLM 世界，NPC 名字是高频种子：库越大，拓展/滴答补 NPC 越不易重名。
# _genre_npc_name 确定性 SeededRng 挑选（同 world+tick+salt 同结果）。
_GENRE_NPC_NAMES: dict[str, list[str]] = {
    "xianxia": ["林清玄", "苏若雪", "顾天澜", "叶云舟", "沈青璃", "白灵犀", "萧无尘",
                "云慕白", "楚星野", "陆霜华", "秦长歌", "江惊鸿", "洛水寒", "裴照夜"],
    "wuxia": ["萧七郎", "段如烟", "慕容天磊", "令狐无双", "楚云轩", "燕若飞", "石铁心",
              "乔惊鸿", "林长风", "白小婉", "顾青竹", "秦傲霜", "柳如絮", "韩孤舟"],
    "modern": ["张建国", "李小雅", "王文轩", "陈雨桐", "林志强", "周晓琳", "赵明远",
               "吴思琪", "郑天佑", "孙嘉怡", "冯立群", "何婉婷", "许文杰", "高梦瑶"],
    "scifi": ["凯文森特", "诺瓦塔", "雷克斯", "艾达林", "卡尔顿", "莉亚娜", "维克托",
              "索伦", "安雅", "罗伊斯", "泽维尔", "塞拉芬娜", "达米安", "瑞雯"],
    "apocalypse": ["老周", "阿梅", "大壮", "铁牛", "石三刀", "顾老疤", "许半仙", "田鼠",
                   "何满囤", "纪二狗", "麻子", "愣子", "山娃", "阿满"],
    "western_fantasy": ["艾德温", "玛拉", "罗兰", "伊莲娜", "布林", "瑟兰迪尔", "盖伦",
                        "莉娜", "阿尔瓦", "格温", "洛汗", "艾琳", "达伦", "西尔维娅"],
}


def _genre_npc_name(genre_id: str, rng: Optional[SeededRng] = None) -> str:
    """从 NPC 姓名库确定性取一个全名（缺键题材回退西幻；rng 空则确定性取首名）。"""
    pool = _GENRE_NPC_NAMES.get(genre_id) or _GENRE_NPC_NAMES["western_fantasy"]
    if rng is None:
        return pool[0]
    return rng.pick(pool)


# ---- [数据量] 地名后缀库（6 题材各 >=10 后缀，地图拓展/兜底命名重名时题材化加后缀）----
_GENRE_PLACE_SUFFIXES: dict[str, list[str]] = {
    "xianxia": ["峰", "谷", "崖", "涧", "岭", "渊", "洞", "台", "坪", "泽"],
    "wuxia": ["镇", "渡", "关", "岭", "坡", "沟", "栈", "集", "湾", "岗"],
    "modern": ["区", "街", "巷", "桥", "园", "城", "湾", "港", "坊", "广场"],
    "scifi": ["舱", "港", "站", "域", "带", "哨", "环", "穹", "区", "井"],
    "apocalypse": ["镇", "营", "站", "堡", "区", "谷", "滩", "坑", "哨", "墟"],
    "western_fantasy": ["堡", "镇", "谷", "岭", "沼", "林", "港", "荒原", "哨", "隘"],
}


def _genre_place_suffix(genre_id: str, rng: Optional[SeededRng] = None) -> str:
    """从地名后缀库确定性取一个后缀（缺键题材回退西幻）。"""
    pool = _GENRE_PLACE_SUFFIXES.get(genre_id) or _GENRE_PLACE_SUFFIXES["western_fantasy"]
    if rng is None:
        return pool[0]
    return rng.pick(pool)


# ---- [P27] 题材化场所兜底池（地点内二层地图场所；LLM 漏出 places 时引擎确定性选 N 个建）----
# 仿 _GENRE_RESOURCE_TEMPLATES 口径：6 题材各 {settlement: >=4, wilderness: >=3}。
# 每个 dict 含 name/type/desc；name 在同题材同 kind 内唯一。_place_templates 缺键回退西幻。
_GENRE_PLACE_TEMPLATES: dict[str, dict[str, list[dict]]] = {
    "xianxia": {
        "settlement": [
            {"name": "坊市广场", "type": "广场", "desc": "坊市中央的石板广场，修士往来如织，灵光隐现。"},
            {"name": "丹药阁", "type": "丹房", "desc": "药香弥漫的丹阁，炉火明灭间炼着各色丹药。"},
            {"name": "藏经阁", "type": "书阁", "desc": "高耸的藏经阁，层层书架藏尽宗门功法典籍。"},
            {"name": "宗门客舍", "type": "客舍", "desc": "接待外来修士的客舍，灵气平和宜静修。"},
            {"name": "灵器坊", "type": "器坊", "desc": "叮当作响的灵器坊，匠师以灵火淬炼法器。"},
        ],
        "wilderness": [
            {"name": "灵药谷", "type": "林间空地", "desc": "谷中灵气氤氲，遍生着药草与异花。"},
            {"name": "灵泉溪畔", "type": "溪边", "desc": "一道灵泉顺石而下，水汽清冽沁人。"},
            {"name": "古松根部", "type": "山崖", "desc": "古松盘根的崖壁，可俯瞰整片山林。"},
            {"name": "断剑石台", "type": "空地", "desc": "荒废的论剑石台，残碑断剑诉说着旧事。"},
        ],
    },
    "wuxia": {
        "settlement": [
            {"name": "镇中茶馆", "type": "茶馆", "desc": "说书人惊堂木一响，江湖旧事随茶香散开。"},
            {"name": "铁匠铺", "type": "铁匠铺", "desc": "炉火通红的铁匠铺，叮当声里淬着兵刃。"},
            {"name": "客栈大堂", "type": "酒馆", "desc": "南来北往的江湖客在此歇脚打尖。"},
            {"name": "镖局院子", "type": "宅邸", "desc": "镖旗高悬的院落，趟子手在此候命。"},
            {"name": "镇口牌坊", "type": "广场", "desc": "镇口的石牌坊，是出入此地的必经之处。"},
        ],
        "wilderness": [
            {"name": "山道凉亭", "type": "空地", "desc": "半山腰的破凉亭，可远眺官道来人。"},
            {"name": "溪边乱石", "type": "溪边", "desc": "溪水击石，乱石间可歇马饮泉。"},
            {"name": "悬崖古松", "type": "山崖", "desc": "崖边一株古松，险峻处正宜瞭望。"},
            {"name": "林间空地", "type": "林间空地", "desc": "林中一片空地，落叶之上偶有兽迹。"},
        ],
    },
    "modern": {
        "settlement": [
            {"name": "街心广场", "type": "广场", "desc": "喷泉与霓虹交映的街心广场，行人匆匆。"},
            {"name": "便利超市", "type": "集市", "desc": "货品齐全的便利店，灯光彻夜不熄。"},
            {"name": "街角咖啡馆", "type": "酒馆", "desc": "飘着咖啡香的街角小店，适合小坐交谈。"},
            {"name": "汽车修理铺", "type": "铁匠铺", "desc": "满是油污的修车铺，工具墙一应俱全。"},
            {"name": "社区公寓楼", "type": "宅邸", "desc": "居民楼的门廊与信箱区，生活气息浓厚。"},
        ],
        "wilderness": [
            {"name": "废弃工地", "type": "空地", "desc": "停工的工地，钢筋脚手架间杂草丛生。"},
            {"name": "河岸步道", "type": "溪边", "desc": "沿河的步道，夜灯倒映在水面上。"},
            {"name": "高架桥下", "type": "空地", "desc": "高架桥的阴影里，是流浪者的避风处。"},
            {"name": "城郊荒坡", "type": "山崖", "desc": "城郊的荒坡，可俯瞰整片街区灯火。"},
        ],
    },
    "scifi": {
        "settlement": [
            {"name": "中转站大厅", "type": "广场", "desc": "空间站的中转大厅，全息告示不停滚动。"},
            {"name": "补给集市", "type": "集市", "desc": "霓虹闪烁的补给集市，机械臂来回搬运货箱。"},
            {"name": "能源酒吧", "type": "酒馆", "desc": "吧台闪着冷光的酒吧，各族旅人混坐其间。"},
            {"name": "机修车间", "type": "铁匠铺", "desc": "堆满备件的车间，焊花与全息图纸交织。"},
            {"name": "居住舱段", "type": "宅邸", "desc": "排列整齐的居住舱，气闸门此起彼伏。"},
        ],
        "wilderness": [
            {"name": "陨坑边缘", "type": "山崖", "desc": "陨石坑的边缘，可俯瞰坑底的全景。"},
            {"name": "冷却水渠", "type": "溪边", "desc": "循环冷却水的明渠，水汽蒸腾。"},
            {"name": "残骸空地", "type": "林间空地", "desc": "散落飞船残骸的开阔地，金属反光刺眼。"},
            {"name": "信号塔下", "type": "空地", "desc": "高耸信号塔的脚下，电磁嗡鸣不息。"},
        ],
    },
    "apocalypse": {
        "settlement": [
            {"name": "避难所大厅", "type": "广场", "desc": "避难所的中央大厅，幸存者在此聚集分配物资。"},
            {"name": "以物易物摊", "type": "集市", "desc": "幸存者摆出的交换摊，罐头与子弹并排。"},
            {"name": "篝火营地", "type": "酒馆", "desc": "围着篝火的营地，是难得的喘息之地。"},
            {"name": "拼装工坊", "type": "铁匠铺", "desc": "用废件拼装武器的工坊，叮当声不断。"},
            {"name": "庇护住宅区", "type": "宅邸", "desc": "加固过的住宅区，铁皮与木板层层叠叠。"},
        ],
        "wilderness": [
            {"name": "废墟空地", "type": "空地", "desc": "坍塌楼宇间的空地，瓦砾上长着变异草。"},
            {"name": "污水河岸", "type": "溪边", "desc": "浑浊的污水河边，偶有变异生物饮水。"},
            {"name": "断桥高台", "type": "山崖", "desc": "断裂高架桥的高台，可俯瞰废城全貌。"},
            {"name": "变异林缘", "type": "林间空地", "desc": "变异树木的边缘，扭曲枝干遮天蔽日。"},
        ],
    },
    "western_fantasy": {
        "settlement": [
            {"name": "镇中心广场", "type": "广场", "desc": "镇中心的石板广场，公告板前常聚着人。"},
            {"name": "铁匠铺", "type": "铁匠铺", "desc": "炉火通红的铁匠铺，叮当声里锻着刀剑。"},
            {"name": "麦穗酒馆", "type": "酒馆", "desc": "喧闹的酒馆，麦酒与烤肉的香气弥漫。"},
            {"name": "集市摊位", "type": "集市", "desc": "镇上的集市，商贩叫卖着各地货物。"},
            {"name": "镇长宅邸", "type": "宅邸", "desc": "镇长的石砌宅邸，门廊挂着族徽旗帜。"},
        ],
        "wilderness": [
            {"name": "林间空地", "type": "林间空地", "desc": "林中一片开阔空地，阳光透过树冠洒下。"},
            {"name": "溪边乱石", "type": "溪边", "desc": "溪水潺潺，乱石间可歇脚取水。"},
            {"name": "悬崖边", "type": "山崖", "desc": "陡峭的悬崖边，可远眺整片原野。"},
            {"name": "古战场遗迹", "type": "空地", "desc": "荒草丛生的古战场，残碑断刃半埋土中。"},
        ],
    },
}


def _place_templates(genre_id: str, kind: str) -> list[dict]:
    """[P27] 取题材场所兜底模板（kind 不在 settlement/wilderness 时按 settlement 取）。

    缺题材/缺 kind 回退西幻；返回的 list 不会被外部修改（调用方自行 copy）。
    """
    kind = kind if kind in ("settlement", "wilderness") else "settlement"
    tbl = _GENRE_PLACE_TEMPLATES.get(genre_id) or _GENRE_PLACE_TEMPLATES["western_fantasy"]
    pool = tbl.get(kind) or _GENRE_PLACE_TEMPLATES["western_fantasy"].get(kind) or []
    return pool


# [P34e] 城市 venue 兜底池：city 型聚落必含 3 类核心 venue（auction 交易所/weapon 锻造屋/alchemy 药店），
# 每题材再补 2+ 特色 venue（酒馆/集市等）。6 题材全覆盖（缺键回退西幻，守 §23）。
# 每条 {name, shop_type, desc}；shop_type 限 _SHOP_TYPE_VALUES 白名单。
_GENRE_CITY_VENUE_TEMPLATES: dict[str, list[dict]] = {
    "western_fantasy": [
        {"name": "交易大厅", "shop_type": "auction", "desc": "城中的交易大厅，商旅云集，珍奇货物在此竞价。"},
        {"name": "铁匠行会", "shop_type": "weapon", "desc": "炉火通明的铁匠行会，叮当声里锻着刀剑甲胄。"},
        {"name": "炼金工坊", "shop_type": "alchemy", "desc": "药香弥漫的炼金工坊，瓶瓶罐罐里调着各色药剂。"},
        {"name": "麦穗酒馆", "shop_type": "consumable", "desc": "喧闹的酒馆，麦酒与烤肉的香气弥漫。"},
        {"name": "集市广场", "shop_type": "material", "desc": "露天集市，商贩叫卖着各地货物与原料。"},
        {"name": "法师塔商会", "shop_type": "magic", "desc": "法师塔经营的商会，出售附魔卷轴与魔法器物。"},
    ],
    "xianxia": [
        {"name": "珍宝阁", "shop_type": "auction", "desc": "坊市最高的珍宝阁，修士在此竞价灵宝丹方。"},
        {"name": "法器阁", "shop_type": "weapon", "desc": "法器阁炉火明灭，炼器师锻着法宝飞剑。"},
        {"name": "丹药铺", "shop_type": "alchemy", "desc": "药香浓郁的丹药铺，丹炉里炼着各色灵丹。"},
        {"name": "灵食楼", "shop_type": "consumable", "desc": "灵食楼里灵茶灵酒飘香，修士歇脚论道。"},
        {"name": "灵材市", "shop_type": "material", "desc": "灵材市摊位林立，兜售着灵草矿石。"},
        {"name": "符箓斋", "shop_type": "magic", "desc": "专售符箓法器的斋铺，朱砂灵墨香气萦绕。"},
    ],
    "wuxia": [
        {"name": "英雄会黑市", "shop_type": "auction", "desc": "暗处的黑市，江湖人在此交易奇兵秘籍。"},
        {"name": "铁匠铺", "shop_type": "weapon", "desc": "炉火通红的铁匠铺，叮当声里锻着刀剑。"},
        {"name": "药铺", "shop_type": "alchemy", "desc": "药香四溢的药铺，掌柜抓着各色药材。"},
        {"name": "酒楼", "shop_type": "consumable", "desc": "热闹的酒楼，侠客歇脚饮酒论剑。"},
        {"name": "山货行", "shop_type": "material", "desc": "山货行里堆着皮毛药材山货。"},
        {"name": "武馆", "shop_type": "general", "desc": "传授武艺的武馆，兼卖拳脚护具。"},
    ],
    "modern": [
        {"name": "地下黑市", "shop_type": "auction", "desc": "隐秘的地下黑市，奇货可居，竞价激烈。"},
        {"name": "军品店", "shop_type": "weapon", "desc": "军品店货架整齐，售着战术装备与器械。"},
        {"name": "药房", "shop_type": "alchemy", "desc": "明亮整洁的药房，药剂师配着各色药品。"},
        {"name": "餐厅", "shop_type": "consumable", "desc": "餐厅里飘着饭菜香，食客往来不绝。"},
        {"name": "五金店", "shop_type": "material", "desc": "五金店堆着各类材料与工具。"},
        {"name": "电子城", "shop_type": "general", "desc": "数码电子产品一条街，改装件应有尽有。"},
    ],
    "scifi": [
        {"name": "星际拍卖网", "shop_type": "auction", "desc": "全息投影的星际拍卖厅，稀有科技在此竞价。"},
        {"name": "军械站", "shop_type": "weapon", "desc": "军械站能量武器陈列，机械臂装卸着火力模块。"},
        {"name": "药剂站", "shop_type": "alchemy", "desc": "药剂站合成舱嗡鸣，调配着纳米药剂。"},
        {"name": "餐厅", "shop_type": "consumable", "desc": "合成食物餐厅，营养餐与能量饮品供应不断。"},
        {"name": "材料站", "shop_type": "material", "desc": "材料站分拣着合金矿石与合成材料。"},
        {"name": "义体诊所", "shop_type": "magic", "desc": "改装义体的诊所，出售神经接口与植入体。"},
    ],
    "apocalypse": [
        {"name": "废土黑市", "shop_type": "auction", "desc": "围栏里的废土黑市，幸存者竞价着搜刮来的珍品。"},
        {"name": "军火商", "shop_type": "weapon", "desc": "军火商的弹药库，枪械与改造武器琳琅满目。"},
        {"name": "诊所", "shop_type": "alchemy", "desc": "简陋的诊所，医生用残存药品救治伤员。"},
        {"name": "食堂", "shop_type": "consumable", "desc": "避难所食堂，配给着罐头与净化水。"},
        {"name": "拾荒站", "shop_type": "material", "desc": "拾荒站堆着分拣过的废料与零件。"},
        {"name": "杂货铺", "shop_type": "general", "desc": "什么都收什么都卖的杂货铺，废土人的百货店。"},
    ],
}


def _city_venue_pool(genre_id: str) -> list[dict]:
    """[P34e] 取题材城市 venue 兜底池（缺题材回退西幻）。返回 list 不被外部修改。"""
    return _GENRE_CITY_VENUE_TEMPLATES.get(genre_id) or _GENRE_CITY_VENUE_TEMPLATES["western_fantasy"]


# [游商 2026-09-08 用户指示] 游商（假 NPC，只交易）身份称谓池：题材 x 店型 -> 多值列表，
# SeededRng 按店 id 确定性挑选（防长线重复）。显示名 = 「{店名}·{称谓}」（店名当招牌，
# 如「百炼堂·铁匠」）。容量下限守卫：tests/test_genre_data_volume.py TestTradePersonaPool。
_GENRE_TRADE_PERSONA_TITLES = {
    "western_fantasy": {
        "weapon": ("武器匠", "铁匠", "铸剑师"), "armor": ("甲胄匠", "盾匠", "护具师傅"),
        "alchemy": ("草药师", "药剂师", "炼金学徒"), "general": ("杂货商人", "店主", "伙计"),
        "material": ("皮革匠", "矿料商", "供货人"), "magic": ("法器商人", "奥术店主", "符文匠"),
        "auction": ("拍卖师", "典当官", "估价师"), "consumable": ("粮商", "行脚商", "食品贩"),
    },
    "xianxia": {
        "weapon": ("铸器师", "器坊坊主", "炼器长老"), "armor": ("炼甲师", "护具坊主", "织锦仙工"),
        "alchemy": ("丹师", "丹坊坊主", "采药道人"), "general": ("坊市掌柜", "灵物商", "杂役管事"),
        "material": ("灵矿商", "材料坊主", "收料道人"), "magic": ("法器阁主", "符箓师", "灵宝商人"),
        "auction": ("万宝楼执事", "拍卖掌仪", "造化阁管事"), "consumable": ("灵膳坊主", "粟行掌柜", "云游行商"),
    },
    "wuxia": {
        "weapon": ("铁匠", "铸剑师", "兵器铺主"), "armor": ("甲胄匠", "护具铺主", "皮具师傅"),
        "alchemy": ("掌柜", "坐堂大夫", "药师"), "general": ("掌柜", "杂货商", "伙计"),
        "material": ("山货行主", "料行掌柜", "行商"), "magic": ("古玩商人", "奇物铺主", "秘藏商人"),
        "auction": ("牙人", "朝奉", "拍卖行执事"), "consumable": ("粮商", "酒楼掌柜", "货栈管事"),
    },
    "modern": {
        "weapon": ("军品店主", "器材商", "户外装备店主"), "armor": ("防护装备商", "战术店主", "护具技师"),
        "alchemy": ("药剂师", "药房店长", "医药代表"), "general": ("便利店长", "超市经理", "杂货店主"),
        "material": ("建材店主", "五金店主", "供货商"), "magic": ("古董店主", "收藏商", "神秘店主"),
        "auction": ("拍卖行经理", "典当师", "鉴定师"), "consumable": ("食品批发商", "餐饮店主", "生鲜店长"),
    },
    "scifi": {
        "weapon": ("军械技师", "武器商", "装甲主管"), "armor": ("义体技师", "护甲工程师", "防具商"),
        "alchemy": ("药剂师", "合成舱操作员", "医疗商"), "general": ("补给站店长", "仓储主管", "配给官"),
        "material": ("材料商", "采矿代理", "回收站主管"), "magic": ("义体医师", "神经接口技师", "科技商人"),
        "auction": ("拍卖行执事", "星贸经纪人", "竞价师"), "consumable": ("合成食品商", "营养剂配送员", "配给站长"),
    },
    "apocalypse": {
        "weapon": ("军火贩子", "武器改装师", "械修师傅"), "armor": ("护甲匠", "废料改装师", "防具商"),
        "alchemy": ("诊所医生", "药品贩子", "医护主管"), "general": ("物资站长", "以物易物商", "杂货贩子"),
        "material": ("拾荒头目", "废料商", "回收主管"), "magic": ("科技遗物商", "旧世界商人", "神秘贩子"),
        "auction": ("黑市主持", "典当商", "鉴宝师"), "consumable": ("配给官", "罐头商", "净水贩子"),
    },
}


def _trade_persona_pool(genre_id: str) -> dict:
    """[游商] 取题材称谓池（缺题材回退西幻）。"""
    return _GENRE_TRADE_PERSONA_TITLES.get(genre_id) or _GENRE_TRADE_PERSONA_TITLES["western_fantasy"]


def trade_vendor_name(world, shop) -> str:
    """[游商 2026-09-08 用户指示] 游商显示名 = 「{店名}·{题材称谓}」（店名当招牌）。

    题材从 world.config_overlay.attribute_template_id 读（缺省 western_fantasy）；
    称谓从题材 x 店型池按店 id 确定性挑选（SeededRng 同店 id 同结果，长线稳定）；
    店名空时回退称谓本身。纯函数无 LLM/Qt 依赖（场景页/结算/测试共用）。
    """
    from src.utils.rng import SeededRng as _SR
    ov = getattr(world, "config_overlay", None) if world is not None else None
    gid = str((ov or {}).get("attribute_template_id", "") or "") or "western_fantasy"
    shop_name = str(getattr(shop, "name", "") or "").strip()
    st = str(getattr(shop, "shop_type", "") or "general").strip() or "general"
    titles = _trade_persona_pool(gid).get(st) or ("店主", "掌柜", "伙计")
    wid = str(getattr(world, "id", "") or "") if world is not None else ""
    try:
        rng = _SR.seed_from(wid, 0, f"vendor_{getattr(shop, 'id', '')}")
        title = rng.pick(list(titles))
    except Exception:
        title = titles[0]
    return f"{shop_name}·{title}" if shop_name else str(title)


# [P34e] 城市必含的 3 类核心 venue（shop_type）：交易所/锻造屋/药店。
_CITY_REQUIRED_VENUE_TYPES = ("auction", "weapon", "alchemy")


# [P27] default_place_id 关键词优先：名含这些词的场所优先作进入该地点的默认场所。
_DEFAULT_PLACE_KEYWORDS = ("广场", "城门", "入口", "空地", "牌坊", "大厅", "营地")


# [P27] NPC 初始场所按角色匹配：role 含「角色关键词」-> 匹配 place 的 name/type 含「场所关键词」。
# 匹配不到回退 location.default_place_id。防「郎中落在铁匠铺」「行商落在药铺」这类 OOC 场所错配。
# 纯启发式 best-effort（只影响建世界初始落点；旁白 [NPC] 命令与滴答仍可后续移动 NPC）。
_NPC_ROLE_PLACE_KEYWORDS = [
    (("铁匠", "锻", "铸", "兵器", "军械", "军火", "工坊"), ("铁匠", "锻造", "铸剑", "兵器", "军火")),
    (("药", "医", "丹", "诊所", "灵药", "草药"), ("郎中", "大夫", "药师", "炼丹", "医馆", "巫医")),
    (("客栈", "酒馆", "酒楼", "饭店", "茶馆", "食堂", "餐厅", "驿"), ("掌柜", "店主", "老板", "客栈", "酒馆", "酒楼", "饭店")),
    (("集市", "市", "杂货", "百货", "货栈", "摊", "商铺", "市场"), ("行商", "商贩", "小贩", "摊贩", "行脚", "货郎")),
    (("寨", "岗哨", "营", "堡", "堂口", "哨"), ("山贼", "首领", "寨主", "匪", "盗", "帮主", "头目", "马贼", "大当家")),
]


def _match_npc_place(npc, loc) -> "Optional[str]":
    """[P27] 按 NPC 角色匹配所在 location 的场所，返回 place.id（无匹配返回 None）。

    匹配 = role 含任一「角色关键词」且 place.name/type 含任一「场所关键词」。
    """
    role = (getattr(npc, "role", "") or "").strip()
    if not role or not getattr(loc, "places", None):
        return None
    for place_kws, role_kws in _NPC_ROLE_PLACE_KEYWORDS:
        if not any(k in role for k in role_kws):
            continue
        for p in loc.places:
            if not isinstance(p, Place):
                continue
            hay = f"{p.name} {p.type}"
            if any(k in hay for k in place_kws):
                return p.id
    return None


def _generate_places_fallback(loc, genre_id: str, count: int) -> list:
    """[P27] LLM 漏出 places 时的题材兜底：按 loc.kind + 题材确定性选 N 个场所建 Place。

    仿 _fill_item_defaults 口径：纯结构、确定性（seed 用 loc.id）、可复现。
    - count 钳制 [1, pool_size]（池不足时全取）。
    - id 确定性 f"place_{loc.id}_{i}"（同 loc 同结果）。
    - connections 环形连通：p0-p1-...-p_{n-1}-p0（单点时无连接）。
    - dungeon 内部地点不应调此（调用方 gate），此处 kind 非 settlement 视 wilderness 取。
    返回 list[Place]（不写回 loc，由调用方挂载 + 算 default_place_id）。
    """
    count = max(1, int(count))
    pool = list(_place_templates(genre_id, loc.kind))
    if not pool:
        return []
    n = min(count, len(pool))
    rng = SeededRng.seed_from(loc.id, 0, "places_fallback")
    picks = rng.sample(pool, n) if n < len(pool) else list(pool)
    places: list = []
    for i, t in enumerate(picks):
        p = Place(
            id=f"place_{loc.id}_{i}",
            name=str(t.get("name") or f"场所{i}").strip(),
            type=str(t.get("type") or "").strip(),
            desc=str(t.get("desc") or "").strip(),
            danger=max(0, min(10, int(getattr(loc, "danger", 0) or 0))) if loc.kind == "wilderness" else 0,
        )
        places.append(p)
    # 环形连通（>=2 个场所才连；单点场所自身即整地点）
    for i, p in enumerate(places):
        if len(places) >= 2:
            nxt = places[(i + 1) % len(places)]
            if nxt.id != p.id and nxt.id not in p.connections:
                p.connections.append(nxt.id)
    return places


def _build_places_for_location(loc, raw_loc: dict, genre_id: str,
                               places_enabled: bool, count: int) -> tuple:
    """[P27] 为单个 location 解析/兜底生成场所，返回 (list[Place], default_place_id)。

    数值范式：LLM 出场所骨架（名/类型/desc/connections 名），引擎算结构
    （id 生成、connections 名->id + 补反向边、default_place_id 选取）。
    - places_enabled=False -> ([], "")（退化单层地图兼容）。
    - kind=dungeon -> ([], "")（秘境内部本就是探索，不场所化）。
    - LLM 漏出/空 places -> _generate_places_fallback 兜底。
    - default_place_id：名含广场/城门/入口/空地等关键词的场所优先，否则取首个。
    """
    if not places_enabled:
        return [], ""
    if getattr(loc, "kind", "") == "dungeon":
        return [], ""
    # 1) 解析 LLM 出的 places（每条 raw dict -> Place；name 空跳过）
    raw_places = raw_loc.get("places") if isinstance(raw_loc, dict) else None
    if not isinstance(raw_places, list):
        raw_places = []
    places: list = []
    name_to_place: dict[str, object] = {}
    for pr in raw_places:
        if not isinstance(pr, dict):
            continue
        nm = str(pr.get("name") or "").strip()
        if not nm or nm in name_to_place:
            continue  # 空/重名跳过
        p = Place(
            name=nm,
            type=str(pr.get("type") or "").strip(),
            desc=str(pr.get("desc") or "").strip(),
            danger=max(0, min(10, int(pr.get("danger", 0) or 0))),
        )
        places.append(p)
        name_to_place[nm] = p
        # 暂存原始 connections 名待回填
        p._raw_conns = [str(c or "").strip() for c in (pr.get("connections") or [])]
    # 2) LLM 漏出 -> 题材兜底
    if not places:
        places = _generate_places_fallback(loc, genre_id, count)
        # 兜底场所已自带环形 connections（id 级），无需再回填名
        name_to_place = {}
    else:
        # 3) connections 名 -> id（同地点内）+ 补反向边（仿 ensure_bidirectional_connections 口径）
        for p in places:
            for cn in getattr(p, "_raw_conns", []) or []:
                tgt = name_to_place.get(cn)
                if tgt is not None and tgt.id != p.id and tgt.id not in p.connections:
                    p.connections.append(tgt.id)
                    if p.id not in tgt.connections:
                        tgt.connections.append(p.id)
        # 清理临时属性（不落库）
        for p in places:
            if hasattr(p, "_raw_conns"):
                try:
                    del p._raw_conns
                except AttributeError:
                    pass
    # 4) default_place_id：名含广场/城门/入口/空地等关键词优先，否则取首个
    default_pid = ""
    if places:
        kw_pick = next(
            (p for p in places
             if any(k in p.name for k in _DEFAULT_PLACE_KEYWORDS)),
            None,
        )
        default_pid = (kw_pick or places[0]).id
    return places, default_pid


def _genre_naming_ref(pool_dict: dict, n: int = 3) -> str:
    """把 {题材id: [条目]} 池拼成「仙侠=a/b/c；武侠=...」一行命名参考。"""
    return "；".join(f"{_GENRE_ZH.get(tid, tid)}={'/'.join(_sample_names(pool, n))}"
                    for tid, pool in pool_dict.items())


# ---- [P11c] 地图拓展伴随物品生成提示词（每次开新地点 -> 新物品入世）----
_EXPAND_ITEMS_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的物品生成引擎。我会给你世界题材、新开辟地点的信息和现有物品名清单，"
    "请为新地点生成与之相关的新物品（探索收获/地方特产/遗落物）。\n\n"
    "只输出 JSON，不要输出任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"items":[{"name":"物品名","type":"weapon|armor|consumable|material|accessory",'
    '"rarity":"common|uncommon|rare|epic|legendary|mythic","desc":"1句描述，点出与新地点的关联",'
    '"slot":"可装备时填 head|chest|legs|feet|main_hand|off_hand|accessory1|accessory2，否则留空",'
    '"attack":0,"defense":0,"heal_pct":0,'
    '"stat_bonus":"装备可选:{str或dex或int或vit或luk:加值} 1-2维",'
    '"level":"装备可选:品级档整数(见范围表)","base_price":0}]}\n\n'
    "要求：\n"
    "1. 物品贴合新地点特色与危险度（灵矿脉出矿石、废墟出旧物、险地出好货）。\n"
    "2. 数值由你按用户消息【物品属性范围表】自填（attack/defense/"
    "heal_pct/stat_bonus/level）——非 0 一律照用，填 0 系统才按品级兜底。"
    "消耗品回血填 heal_pct（回复最大生命百分比 5-50）。\n"
    "3. 品级以 common/uncommon 为主，危险度高的地点可给 rare，epic/legendary 极少。\n"
    "4. 名字不与现有物品重复、彼此不重复，贴合世界观基调与 NSFW 设定。\n"
    "5. desc 只写背景故事与氛围，不写数值承诺（如「攻击+100」「五维俱增」）——"
    "数值以你填的字段为准，写了必然不符（效果先行铁律）。\n"
    "6. 按指定数量生成。\n"
    "7. [!] type=consumable 只给「可服用/可敷用/使用即消耗」之物（丹药/药膏/药汤/食物/酒水/符纸/卷轴等有服用或敷用形态的东西）——它们会被玩家「服用」并消耗。器物类奇物（铜镜/罗盘/工具/兵器谱/宝盒这类只能拿在手里端详的）绝不判 consumable：可佩戴的归 accessory、把玩收藏的归 material；回血/治疗类效果（heal_pct 回血/heal_mp 回蓝/cure/revive）只许给可内服外敷之物（错误示例：把「照骨镜」生成成一喝回血的丹药）。\n"
)

# [数据量] 地图拓展怪物物种生成提示词（每次拓展新增怪物物种；danger 区间 + loot 引用材料名）。
_EXPAND_MONSTER_SYSTEM_PROMPT = (
    "你是一个 SLG 游戏的怪物生成引擎。我会给你世界题材、新开辟地点的信息和现有材料名清单，"
    "请为新地点生成与之相关的怪物物种（危险度适配地点，loot 引用现有材料名）。\n\n"
    "只输出 JSON，不要输出任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"monsters":[{"name":"怪物名","role":"野兽|妖兽|魔物|强盗|杀手|首领",'
    '"desc":"1句描述，点出与新地点的关联","danger_min":1,"danger_max":3,"loot":["材料名"]}]}\n\n'
    "要求：\n"
    "1. 怪物贴合新地点特色与危险度（灵矿脉出石怪、废墟出变异体、险地出首领怪）。\n"
    "2. danger_min/danger_max 在 1-10 区间，min<=max，覆盖地点危险度。\n"
    "3. loot 只引用我给出的材料名（1-2 个），不要编造物品名。\n"
    "4. 名字不与现有怪物重复、彼此不重复，贴合世界观基调与 NSFW 设定。\n"
    "5. 按指定数量生成。"
)

# ---- [拆分生成 2026-08-25] 按类型拆多次 LLM 调用（用户指示：细拆避免注意力稀释）----
# 每个常量聚焦一类数据，user 消息注入世界观锚 + 范围表 + 示例 + 引用清单（见 generate_world_skeleton）。
# 效果先行铁律在各 prompt 内重申；数值仍走引擎钳制（_clamp_item_stats / _sanitize_pool_skills）。

_GEN_ITEMS_SYSTEM_PROMPT = (
    "你是 SLG 游戏世界的「物品生成引擎」。我会给你世界题材与物品数量指引，"
    "请为这个世界生成全部物品（武器/防具/饰品/消耗品/材料/钥匙），输出严格 JSON。\n\n"
    "只输出 JSON，不要任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"items":[{"name":"物品名","type":"weapon|armor|consumable|material|key|accessory",'
    '"rarity":"common|uncommon|rare|epic|legendary|mythic","desc":"背景故事(不写数值承诺)",'
    '"slot":"可装备时填 head|chest|legs|feet|main_hand|off_hand|accessory1|accessory2，否则留空",'
    '"attack":0,"defense":0,"heal_pct":0,'
    '"stat_bonus":{"str或dex或int或vit或luk":加值},'
    '"level":0,"consume_effect":{},"category":"功能分类(规则8,普通物品留空)","reagent_kind":"培养道具子类型(规则8,非cultivate留空)"}]}\n\n'
    "要求：\n"
    "1. 按用户消息【物品数量指引】的类型配额生成（装备占大头且覆盖 8 槽位）。材料按配额出即可、"
    "稀有度任选——系统会自动补足各档次的锻造/制造材料（common~legendary 每档都有），"
    "无须你覆盖全部层级。功能性物品（鉴定/洗练/宠物资质道具、种子、宠物食品）由你按规则 8 生成。\n"
    "2. 先定结构化效果（consumable 填 consume_effect 或 heal_pct；装备填 "
    "attack/defense/stat_bonus），再写 desc——desc 只描述已填效果，绝不提及未填的额外功效"
    "（例：只填回血却写「五维俱增」是严重违规）。\n"
    "3. desc 不写具体数值承诺（如「攻击+100」），数值以你填的字段为准。\n"
    "4. 数值按用户消息【物品属性范围表】按品级自填（非 0 照用，填 0 系统才兜底）。\n"
    "5. 消耗品结构化效果格式见用户消息【消耗品效果】。\n"
    "6. 名字彼此不重复、贴合题材与 NSFW 设定。\n"
    "7. 按用户消息【物品数量指引】生成。\n"
    "8. 功能性物品（系统玩法依赖，必须生成；名字与 desc 用本题材的说法，如科幻题材鉴定道具叫「扫描模块」）：\n"
    "   a) cultivate 道具 x3（category=cultivate，type=consumable）：鉴定道具（reagent_kind=identify）、洗练道具（reagent_kind=refine）、宠物资质丹（reagent_kind=apt_all）各 1 件；\n"
    "   b) 种子 x2（category=seed，type=material）：可种植作物，desc 写「约N天成熟」；\n"
    "   c) 宠物食品 x1（category=pet_food，type=consumable）：可投喂宠物的食物。\n"
    "   这 6 件不计入【物品数量指引】的类型配额（是额外要求）；desc 勿写数值承诺。\n"
    "9. [!] type=consumable 只给「可服用/可敷用/使用即消耗」之物（丹药/药膏/药汤/食物/酒水/符纸/卷轴——有服用或敷用形态，玩家会把它「服用」掉）。器物类奇物（铜镜/罗盘/工具/兵器谱/宝盒这类只能拿在手里端详的）绝不判 consumable：可佩戴的归 accessory、把玩收藏的归 material；回血/治疗类效果（heal_pct 回血/heal_mp 回蓝/cure/revive）只许给可内服外敷之物（错误示例：把「照骨镜」生成成一喝回血的丹药）。\n"

)

_GEN_SKILLS_SYSTEM_PROMPT = (
    "你是 SLG 游戏世界的「技能与天赋生成引擎」。我会给你世界题材与数量指引，"
    "请生成玩家开局技能、可学习技能池、天赋池，输出严格 JSON。\n\n"
    "只输出 JSON，不要任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"player_skill":{"name":"开局技能名","type":"attack","power":10,"cost_mp":5,"cooldown":1,'
    '"element":"","stat_scaling":"str","inflicts":[],"target_pattern":"single"},'
    '"skill_pool":[{"name":"技能名","desc":"1句描述","type":"attack|heal|buff","power":10,'
    '"cost_mp":5,"cooldown":1,"rarity":"common|uncommon|rare|epic|legendary|mythic",'
    '"element":"","stat_scaling":"str|dex|int","inflicts":[],"target_pattern":"single|aoe_enemy"}],'
    '"talent_pool":[{"name":"天赋名","desc":"1句描述","rarity":"common|uncommon|rare|epic|legendary|mythic",'
    '"element":"","effects":{"stat_bonus":{"str":2},"skill_dmg_mult":1.2,"luck_bonus":3}}]}\n\n'
    "要求：\n"
    "1. player_skill 是玩家开局技能（只 1 个，type=attack、普通强度、贴合职业与题材）。\n"
    "2. skill_pool 按用户消息【技能池数量】生成，power 按 rarity 缩放（common 10-15 / uncommon 12-20 / "
    "rare 16-26 / epic 20-32 / legendary 25-40），cost_mp 0-15、cooldown 0-4。\n"
    "3. 技能三分池：攻击池=type attack（主伤害）；恢复池=type heal（回血/回蓝）；状态池=增益(type buff，"
    "inflicts 用 protected/enraged) 或 控制/减益(type attack 且带控制类 inflicts，power 压低 3-8)。\n"
    "4. inflicts.condition 限 burning/chilled/wet/entangled/poisoned/stunned/feared/protected/enraged/"
    "bleeding/blinded/shocked/frozen，chance 0.2-0.7、duration 2-3。元素与状态呼应：火附 burning、"
    "冰附 chilled/wet、木附 entangled/poisoned、雷附 stunned、光/暗附 feared。\n"
    "5. talent_pool 按用户消息【天赋池数量】生成，所有效果钩子（skill_dmg_mult/atk_mult/gather_mult/"
    "craft_mult/check_mult/loot_mult/luck_bonus/mp_bonus/stat_bonus）一律放在 effects 对象内——"
    "与 effects 平级是错误（stat_bonus 每维 1-4）；element 非空的 skill_dmg_mult 只加成同系技能。\n"
    "6. 命名贴合题材与 NSFW。"
)

_GEN_MONSTERS_SYSTEM_PROMPT = (
    "你是 SLG 游戏世界的「怪物生成引擎」。我会给你世界题材、材料名清单与数量指引，"
    "请生成怪物物种与怪物技能池，输出严格 JSON。\n\n"
    "只输出 JSON，不要任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"monsters":[{"name":"怪物名","role":"妖兽|魔物|野兽|强盗|首领","desc":"1句描述",'
    '"danger_min":1,"danger_max":10,"loot":["材料名"],"elements":[]}],'
    '"monster_skill_pool":[{"name":"技能名","type":"attack|heal|buff","element":"",'
    '"desc":"1句描述","inflicts":[]}]}\n\n'
    "要求：\n"
    "1. monsters 按用户消息【怪物池数量】生成，danger 覆盖 1-10，loot 只引用我给出的材料名（1-2 个）。\n"
    "2. monster_skill_pool 是怪物技能池（只给 name/type/element/desc/inflicts，power/cost_mp/cooldown "
    "由系统按怪物职能算，勿填）。三分：attack（伤害，可带 bleeding 等）、heal（自愈）、"
    "buff（增益 enraged/protected）或攻击带控制（stunned/frozen/entangled 等）。每职能至少 1 条。\n"
    "3. 怪物无数值（等级/属性由系统按地点+玩家等级动态生成）。命名贴题材与 NSFW。"
)

_GEN_NPCS_SYSTEM_PROMPT = (
    "你是 SLG 游戏世界的「NPC 生成引擎」。我会给你世界题材、地点名/势力名/物品名/天赋名清单与数量指引，"
    "请生成全部 NPC，输出严格 JSON。\n\n"
    "只输出 JSON，不要任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"npcs":[{"temp_id":"n1","name":"NPC名","role":"身份(守卫/首领/铁匠/医师...)",'
    '"faction":"势力名","location":"地点名","personality":"性格","goal":"目标",'
    '"appearance":"中文外貌(供生图)","combat_role":"boss|hostile|friendly|none",'
    '"inventory":["物品名"],"talent_names":["天赋名"],"elements":[]}]}\n\n'
    "要求：\n"
    "1. faction/location 只引用我给出的势力名/地点名；inventory 只引用物品名（1-3 件，"
    "且只能从【物品名清单】里稀有度紫色(epic)及以下的物品中挑——橙色(legendary)/红色(mythic)"
    "宝物绝不能放进 NPC 的初始随身，那些是玩家要靠冒险才能取得的东西，系统会强制剔除并降级)；"
    "talent_names 只引用天赋名（最多 2）。\n"
    "2. combat_role 必填：boss=敌对头目/首领级强敌、hostile=敌对战斗单位、friendly=有武力的友方/可助战、"
    "none=平民非战斗（铁匠/医师/村民等）。漏标按 none 平民处理。\n"
    "3. appearance 用中文写清外貌（性别/年龄/发型发色/瞳色/服装/标志特征）供文生图。\n"
    "4. elements 最多 2 个（fire/thunder/ice/wind/wood/metal/earth/light/dark），法系/元素生物给，普通人留空。\n"
    "5. 数量按用户消息【NPC 数量指引】，命名贴题材与 NSFW。\n"
    "6. [!] 商店已由各地点场所自营，不许生成开店经商的 NPC：role 一律不许写商人/掌柜/"
    "老板/店主/商贩/小贩/摊贩/店小二/货郎（铁匠/医师等手艺人可以有，但只做人物不许开店）。"
)

_GEN_WORLD_SYSTEM_PROMPT = (
    "你是 SLG 游戏世界的「世界骨架生成引擎」。我会给你世界题材，请生成世界书、势力、地点与世界名，"
    "以及玩家起始信息，输出严格 JSON。\n\n"
    "只输出 JSON，不要任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"worldbook":[{"key":"词条名","content":"1-3句","priority":1}],'
    '"factions":[{"name":"势力名","desc":"","ideology":"","leader_traits":"",'
    '"relations":[{"target":"其他势力名","value":-100到100的整数}]}],'
    '"locations":[{"name":"地点名","desc":"","region":"","faction":"势力名",'
    '"danger":1,"kind":"settlement|wilderness","settlement_size":"city|town|village",'
    '"venues":[{"name":"场所名","shop_type":"weapon|armor|alchemy|consumable|material|magic|auction","desc":""}],'
    '"resource":{"name":"","type":"","desc":"","tier":1},"connections":["相邻地点名"],'
    '"places":[{"name":"场所名","type":"","desc":"","connections":["同地点场所名"]}]}],'
    '"attribute_template_id":"题材模板id","player_location":"起始地点名",'
    '"suggested_class":"建议职业","background":"玩家身世一句话",'
    '"world_name":"世界名(4-8字)"}\n\n'
    "要求：\n"
    "1. factions.relations 只填有明确关系的势力对（正=盟友 +30~80，负=敌对 -30~-80），不要两两都塞值。\n"
    "2. locations.connections 只引用真实地点名；kind 规则：聚落=settlement（无 resource），"
    "野外=wilderness（必须带 resource）。\n"
    "3. resource.tier 1-5 随 danger 分层（1-2->1 / 3-4->2 / 5-6->3 / 7-8->4 / 9-10->5）。\n"
    "4. settlement_size 仅聚落填：city 必含 venues（auction/weapon/alchemy 三类）。\n"
    "5. places：每地点 3-5 个场所，connections 只引同地点内场所名。\n"
    "6. attribute_template_id 从用户消息【属性/装备模板库】选一个最贴题材的 id；player_location 从本批"
    "地点名里选一个低危聚落作出生地。\n"
    "7. 数量按用户消息【规模】。\n"
    "8. [!] 危险度铁律：聚落（settlement）的 danger 一律填 0——聚落是安全区，永不刷怪，"
    "非零值会被引擎强制归零；只有野外（wilderness）地点有危险度（1-10），危险度须拉开梯度"
    "（低危 1-3 供新手、高危 8-10 压轴）。若用户消息有【聚落与野外】数量指引，聚落/野外"
    "数量必须严格等于指引值。\n"
    "9. [!] 货币铁律：涉及钱款/物价/赏金的文字（含世界书词条——不要写「XX是通用货币」"
    "这类自造货币设定）一律用用户消息【题材货币】给出的货币名；未给该块时用所选模板的"
    "通行货币（不发明新货币名）。"
)

_GEN_RECIPES_QUESTS_SYSTEM_PROMPT = (
    "你是 SLG 游戏世界的「配方与任务生成引擎」。我会给你世界题材、物品名/NPC 名/地点名/怪物名清单与数量指引，"
    "请生成合成配方与任务，输出严格 JSON。\n\n"
    "只输出 JSON，不要任何说明或 markdown 代码块标记。\n\n"
    "JSON schema：\n"
    '{"recipes":[{"name":"配方名","desc":"","inputs":["物品名"],"output":"物品名","difficulty":0}],'
    '"quests":[{"chain":"main|side","title":"任务名","objective":"一句话目标","giver":"npc的temp_id",'
    '"reward":"奖励说明","objectives":[{"type":"kill|gather|talk|visit|collect","target":"目标名",'
    '"count":1,"desc":""}],"rewards":{"gold":0,"xp":0,"items":["物品名"]}}]}\n\n'
    "要求：\n"
    "1. recipes.inputs/output 只引用物品名（material 优先作 inputs）；配方覆盖各资源层级，产出以消耗品/装备为主。\n"
    "2. quests：主线 2-3 段（数组顺序=链顺序，首段给新手可完成目标）+ 支线 1-2 条；giver 只引用 NPC temp_id（n1/n2 这种，仅此字段用 temp_id）。"
    "[!] objective 与 objectives 里的 target/desc 一律写真实 NPC 名/地点名/怪物名/物品名，绝不要出现 temp_id（n1/n6 这种）；"
    "reward（奖励说明）的货币写用户消息【题材货币】给的题材货币名（如晶核/灵石/金币），不要写错成别的货币；"
    "rewards.items 只引用物品名，且 reward 文案里的物品名要与 rewards.items 一致（数量可写在文案里，但物品名要对得上）；"
    "[!] collect 与 deliver_items 的 target 必须是【可获取物品】里的具体物品名（如「净水芯片」），"
    "绝不能写「任务物品/材料/补给/科技材料」这类资源类型名——那是 gather 的 target 口径；"
    "desc 里点名的物品要与 target 一致（说「取回净水芯片」target 就写「净水芯片」）。\n"
    "3. 数量按用户消息【配方数量】。\n"
    "4. [!] 目标环环相扣铁律（系统按以下口径判进度，写错=任务永卡 0/N）："
    "kill 的 target 必填，从【怪物名清单】逐字选；gather 的 target 是资源点类型不是物品名，"
    "只能从【资源点类型清单】逐字选（清单空则不要出 gather 目标）；visit 的 target 从【地点名清单】"
    "逐字选；objective/desc 里不要发明清单外的地名/怪物名/物品名（「去某港浅滩」这种自造小地名会误导玩家）。"
)

# ---- [游商 2026-09-08：商人 NPC 已摘除] 商店类型只从场所类型来（place.type /
# 中文关键词场所），不再从 NPC role 文本推断。_SHOP_TYPE_ITEM_TYPES 仍是选品口径。
# shop_type -> 该店经营的 Item.type 集合（确定性补货/目录兜底选品/游商合成用）。
# [!] weapon/armor 互为副营（铁匠铺打刀也修甲，防具店也卖刀——大明档武器店货架
# 本来就混着 armor，反之亦然；副营只补卖光的单品，不追品类缺口，见 ranked 口径）。
_SHOP_TYPE_ITEM_TYPES = {
    "weapon": {"weapon", "armor"},
    "armor": {"armor", "weapon"},
    "alchemy": {"consumable"},
    "consumable": {"consumable", "material"},
    "material": {"material"},
    "magic": {"accessory", "weapon"},
    "general": {"weapon", "armor", "consumable", "material", "accessory"},
    # [P8] 拍卖/黑市：全品类池（_seed_shop_from_catalog 再优先 legendary/epic）
    "auction": {"weapon", "armor", "consumable", "material", "accessory"},
}


def _is_food_item(it) -> bool:
    """[酒楼分家 2026-09-10 用户指示] 是否吃喝（归 consumable 酒楼店）。

    判定 = type==consumable 且 consume_effect.type=="feed"（回饱食度的食物/酒水，
    _init_foods 题材池 + 宠物食品除外见下）。丹药（heal_pct/heal_mp/cure/revive/
    stat_bonus）一律 False，归 alchemy 药铺。技能书（teach_skill）与培养道具
    （cultivate）不由本函数判定（另有独立口径），调用方须先行排除。
    纯函数无 LLM/Qt 依赖（选品/合成/测试共用）。
    """
    if it is None or getattr(it, "type", "") != "consumable":
        return False
    eff = getattr(it, "consume_effect", None)
    return isinstance(eff, dict) and str(eff.get("type", "") or "") == "feed"


def _is_medicine_item(it) -> bool:
    """[酒楼分家 2026-09-10] 是否药品（归 alchemy 药铺，不归酒楼）。

    判定 = type==consumable 且（heal_pct>0 或 consume_effect.type in
    heal_full(旧别名)/heal_mp/cure/revive/stat_bonus）。feed 吃喝返回 False。
    技能书/cultivate/pet_food 由调用方先行排除（它们另有归属）。
    """
    if it is None or getattr(it, "type", "") != "consumable":
        return False
    if int(getattr(it, "heal_pct", 0) or 0) > 0:
        return True
    eff = getattr(it, "consume_effect", None)
    if not (isinstance(eff, dict) and eff):
        return False
    return str(eff.get("type", "") or "") in (
        "heal_full", "heal_mp", "cure", "revive", "stat_bonus")
# shop_type -> GenreText.shop_type 不命中时的兜底中文名。
_FALLBACK_SHOP_NAME = {
    "general": "杂货铺", "weapon": "兵器铺", "armor": "防具铺", "alchemy": "药铺",
    "consumable": "杂货铺", "material": "材料铺", "magic": "奇物店", "auction": "拍卖行",
}


def _auction_lot_ids(world) -> set[str]:
    """[拍品隔离 2026-09-09 用户指示] 全部拍卖拍品 item_id（active 在拍 / ended 已拍出都算
    ——后者仍留在 world.items 且归玩家，上架会造成同 id 复卖）。所有商店选品池一律排除：
    拍品只归拍卖会竞价，不上任何货架（含拍卖游商）。"""
    ids: set[str] = set()
    for a in (getattr(world, "auctions", None) or []):
        for lot in (getattr(a, "lots", None) or []):
            iid = str(getattr(lot, "item_id", "") or "")
            if iid:
                ids.add(iid)
    return ids

# [P15a1] 场景日志滚动总结提示词：把最老一批场景条目 + 既有前情摘要压成一段连贯摘要。
# 结算 LLM 小调用（输出上限 1 万起，期望值短只作护栏），失败/取消保留条目下轮重试。
_SCENE_SUMMARY_SYSTEM_PROMPT = (
    "你是 SLG 游戏的场景记录整理助手。我会给你该场景的既有前情摘要（可能为空）"
    "和一批较老的场景记录（玩家行动/旁白/系统提示），请把它们合并整理成一段连贯的前情摘要，"
    "供后续回合的结算与叙事参考。\n\n"
    "要求：\n"
    "1. 保留对后续剧情有影响的关键事实：去过哪些地方、见过/打过哪些人、获得的线索与承诺、"
    "重大战斗/奇遇的结果、任务进展、玩家立下的誓言或结下的仇怨。\n"
    "2. 删去氛围性描写与无关细节，不要逐条复述，写成 3-6 句连贯段落。\n"
    "3. 与既有摘要冲突时以较新的记录为准；不要编造记录中没有的信息。\n"
    "4. 只输出整理后的摘要段落本身，不要任何前后缀或解释。"
)

# [P20] NPC 说话风格补生成提示词（B1，老世界补字段用；新世界经 generate_npc_profiles 内联产出）
_NPC_SPEECH_SYS_PROMPT = (
    "你是 SLG 游戏的 NPC 说话风格生成引擎。我会给你若干 NPC 的基本信息（名字/身份/性格/目标/势力），"
    "请为每个 NPC 生成一句**泛义说话风格**（20 字内：整体语气/用词倾向/对人对事的态度，"
    "如「语气冷硬、爱用反问」「对熟人俏皮，对生人寡言」），"
    "须与性格一致且各 NPC 之间有明显区分度。\n"
    "[!] 禁止写具体口头禅/固定台词/每轮都要说的某句话（如「俺老孙来也！」）——"
    "风格是泛义倾向，由叙事 LLM 每轮自行遣词，不是被复读的台词。\n"
    "输出严格 JSON，不要任何说明或代码块标记。\n\n"
    "JSON schema：\n"
    '{"styles": [{"name": "NPC名", "speech_style": "1句泛义风格"}]}'
)


# [口癖软化 2026-09-11 用户指示] speech_style 是「泛义说话风格」不是「固定口头禅」：
# LLM 仍可能产出「俺老孙来也！」这类具体台词，落库前统一剥掉以 ！/？ 收尾的引号台词，
# 只留「语气冷硬、爱用反问」这类泛义描述，根治注入端偶发的复读机观感。
_SPEECH_QUOTE_CATCHPHRASE_RE = re.compile(
    r'(?:「[^」]{0,40}?[！!？?]」|『[^』]{0,40}?[！!？?]』|“[^”]{0,40}?[！!？?]”'
    r'|"[^"]{0,40}?[！!？?]"|\'[^\']{0,40}?[！!？?]\')'
)


def _sanitize_speech_style(raw) -> str:
    """[口癖软化] 去具体台词、只留泛义风格（引号内以 ！/？ 收尾者视为口头禅剥掉）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    s = _SPEECH_QUOTE_CATCHPHRASE_RE.sub("", s)
    s = re.sub(r"[；;、，,]\s*[；;、，,]", "、", s).strip("　 。；;、，,")
    return s


def _resolve_scale_counts(scale_id: str, preset: WorldSimPreset) -> "Optional[dict]":
    """[P5b] 按 scale_id 找规模档位的结构化数字 {id,label,locations,npcs,places}（找不到返回 None）。

    查找顺序：内置三档 (BUILTIN_SCALES) -> 用户自定义档 (preset.custom_world_scales)。
    """
    from src.models.world_sim_preset import BUILTIN_SCALES
    for c in BUILTIN_SCALES:
        if c.get("id") == scale_id:
            return dict(c)
    for it in (preset.custom_world_scales or []):
        if isinstance(it, dict) and it.get("id") == scale_id:
            return dict(it)
    return None


def _resolve_scale_hint(scale_id: str, preset: WorldSimPreset) -> str:
    """[P5b] 规模档位 -> 给 LLM 的规模提示词文本（由结构化数字经固定模板生成，防手写漂移）。"""
    from src.models.world_sim_preset import _scale_hint_text
    c = _resolve_scale_counts(scale_id, preset)
    if c is not None:
        return _scale_hint_text(c)
    # 兜底：通用规模字符串（防止 scale_id 来自旧预设/其他来源）
    return "按规模「{0}」产合适数量的地点/NPC/任务/物品".format(scale_id)


def _genre_tag_directive(tags: list) -> str:
    """[题材对齐 2026-10-01 用户拍板] 题材标签 -> attribute_template_id 硬指引。

    标签与内置六题材 1:1（GENRE_TAG_TEMPLATE_MAP 单一来源）：
    - 唯一命中：直接指定 template_id（防弱模型被 premise 带偏选错/落兜底西幻）；
    - 多标签：给候选集，按【世界观】正文的主导题材选；
    - 旧/自定义标签不在映射内：返回空（保持旧软信号行为，不误伤老预设）。
    供 _build_gen_user_message / _build_world_context 两处共用。"""
    from src.models.world_sim_preset import GENRE_TAG_TEMPLATE_MAP
    if not tags:
        return ""
    m = dict(GENRE_TAG_TEMPLATE_MAP)
    ids = {m[t] for t in tags if t in m}
    if len(ids) == 1:
        return (f"【题材模板指定】attribute_template_id 必须填 {ids.pop()}"
                "（题材标签唯一指定，不得改选其他 id）")
    if len(ids) > 1:
        return ("【题材模板指引】attribute_template_id 只能从以下候选中按【世界观】"
                "正文的主导题材选一个：" + "、".join(sorted(ids)))
    return ""


def _genre_currency_hint(tags: list) -> str:
    """[修 2026-10-02 真机 OOC·货币串味] 题材标签唯一命中模板时给出货币硬约束块。

    末日档世界书词条曾宣告「瓶盖=废土通用货币」而引擎/商店/奖励全用「晶核」——
    生成管线自始至终没告诉 LLM 货币名。标签唯一映射 -> 模板 currency_name（单一
    来源 GenreText 口径）；多标签/未知标签返回空串（模板未定，不猜货币）。
    供 _build_gen_user_message 注入（世界书/NPC/地点/物品/任务全段共享 ctx）。
    [!] 外来币词表是模块常量（非 f-string 字面量）——test_currency_i18n 静态守护
    扫 f-string 内裸币种名，禁词「提及」也不能写成字面量。"""
    from src.models.world_sim_preset import (GENRE_TAG_TEMPLATE_MAP,
                                             BUILTIN_ATTRIBUTES_TEMPLATES)
    if not tags:
        return ""
    m = dict(GENRE_TAG_TEMPLATE_MAP)
    ids = {m[t] for t in tags if t in m}
    if len(ids) != 1:
        return ""
    tid = next(iter(ids))
    tmpl = next((t for t in BUILTIN_ATTRIBUTES_TEMPLATES if t.get("id") == tid), None)
    cur = str((tmpl or {}).get("currency_name", "") or "").strip()
    if not cur:
        return ""
    banned = "、".join(_FOREIGN_CURRENCY_WORDS)
    return (f"【题材货币】本世界的通用货币是「{cur}」。世界书词条、势力/地点描述、"
            f"NPC 台词与一切涉及钱款的文字一律使用「{cur}」计数，"
            f"禁止出现{banned}等其他货币名。")


def _worldbook_npc_directive(worldbook) -> str:
    """[P1 修 2026-10-01 真机验收] NPC 段与世界书段两次 LLM 调用未交叉约束，
    同角色不同名（世界书写「残刀/沈墨/周铁山」，NPC 实体却叫「厉无咎/沈铁衣/赵铁山」）。

    世界书先于 NPC 生成且已定稿——把词条全文注入 NPC 段并硬约束复用其中具名人物，
    名字单一来源=世界书。词条为空返回空串（老档/极简世界不注入）。"""
    entries = [e for e in (worldbook or [])
               if isinstance(e, dict)
               and (str(e.get("key") or "").strip() or str(e.get("content") or "").strip())]
    if not entries:
        return ""
    lines = [f"  {str(e.get('key') or '').strip()}：{str(e.get('content') or '').strip()}"
             for e in entries]
    return ("【世界书词条（已定稿，人物名字以此为准）】\n" + "\n".join(lines)
            + "\n[!] 硬约束：词条中出现的具名人物，生成对应 NPC 时必须原样使用词条里的名字"
            "（一字不改、不加头衔、不换近义名），身份设定也以词条为准；词条人物不够 NPC 数量时，"
            "其余 NPC 才自由命名。")


def _normalize_scale_id(scale_id: str, preset: WorldSimPreset) -> str:
    """[P5b] 校验 + 兜底 scale_id：内置三档 / 自定义档 / 兜底里选一个有效值。

    来源不可信（form dict / 老预设 / 用户直接传）时调用，返回保证可用的 scale_id。
    """
    from src.models.world_sim_preset import BUILTIN_SCALES
    builtin_ids = {c["id"] for c in BUILTIN_SCALES}
    if scale_id in builtin_ids:
        return scale_id
    for it in (preset.custom_world_scales or []):
        if isinstance(it, dict) and it.get("id") == scale_id:
            return scale_id
    return preset.default_world_scale or "medium"


def _resolve_template_id(raw_template_id: str, preset: WorldSimPreset) -> str:
    """[P5c] 校验 + 兜底 attribute_template_id。

    LLM 输出可能为空 / 拼错 / 不在模板库里 -> 兜底 DEFAULT_ATTRIBUTE_TEMPLATE_ID（西幻）。
    """
    from src.models.world_sim_preset import (
        BUILTIN_ATTRIBUTES_TEMPLATES, DEFAULT_ATTRIBUTE_TEMPLATE_ID,
    )
    if not raw_template_id:
        return DEFAULT_ATTRIBUTE_TEMPLATE_ID
    # 内置
    if any(t["id"] == raw_template_id for t in BUILTIN_ATTRIBUTES_TEMPLATES):
        return raw_template_id
    # 用户自定义
    if any(isinstance(t, dict) and t.get("id") == raw_template_id
           for t in (preset.attribute_templates or [])):
        return raw_template_id
    return DEFAULT_ATTRIBUTE_TEMPLATE_ID


def _get_template_by_id(template_id: str, preset: WorldSimPreset) -> dict:
    """[P5c/P6] 按 template_id 取完整模板 dict。

    [P6] 返回前过 _normalize_template，保证调用方拿到的 dict 含全部 6 组题材化字段
    （stat/slot/currency/rarity/shop_type/item_type），缺失补西幻默认。
    找不到时兜底返回西幻模板。
    """
    from src.models.world_sim_preset import (
        BUILTIN_ATTRIBUTES_TEMPLATES, DEFAULT_ATTRIBUTE_TEMPLATE_ID, _normalize_template,
    )
    for t in BUILTIN_ATTRIBUTES_TEMPLATES:
        if t["id"] == template_id:
            return _normalize_template(t)
    for t in (preset.attribute_templates or []):
        if isinstance(t, dict) and t.get("id") == template_id:
            return _normalize_template(t)
    # 兜底
    for t in BUILTIN_ATTRIBUTES_TEMPLATES:
        if t["id"] == DEFAULT_ATTRIBUTE_TEMPLATE_ID:
            return _normalize_template(t)
    # 极端兜底（理论上不会到这）
    return _normalize_template(BUILTIN_ATTRIBUTES_TEMPLATES[0])


def _safe_int(val, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """安全转 int：非数值/None 返回 default，可选 [lo,hi] 钳制。防 LLM 输出脏类型。"""
    try:
        v = int(val)
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _name_lookup(table: dict, name):
    """[P44] 按名查表 + 统一解析器容错（LLM 写名常加修饰/别字/乱序）。

    骨架/拓展 JSON 内部交叉引用（配方 output/inputs、怪物 loot、NPC inventory、
    任务奖励物品名 -> item id）统一走此口径，未命中返回 None（调用方各自静默跳过）。
    """
    name = str(name or "").strip()
    if not name:
        return None
    hit = table.get(name)
    if hit is not None:
        return hit
    key = nrs.resolve_name(name, list(table.keys()))
    return table.get(key) if key else None


def _resolve_temp_ids(text: str, npc_by_temp: dict) -> str:
    """[修 2026-08-25] 把任务文本里的 NPC temp_id（n1/n2/...）替换成真实 NPC 名。

    LLM 生成任务时偶尔把 giver 用的 temp_id 误写进 objective/objectives.target/desc
    （如「找到巡逻队员n6」），展示会漏出 n6。这里按 temp_id 长度降序做字面替换
    （n10 先于 n1，防短 id 吞长 id 前缀），把 n6 -> NPC 名。
    """
    if not text or not npc_by_temp:
        return text
    for tid in sorted(npc_by_temp.keys(), key=len, reverse=True):
        npc = npc_by_temp.get(tid)
        if npc is not None:
            text = text.replace(tid, npc.name)
    return text


def _dedup_npc_name(name: str, seen: set) -> str:
    """[修 2026-09-10] LLM 给出的重名 NPC 加序号去重（与地图拓展命名同款口径）。

    理由同「拓展 NPC 命名」注释：cand_by_name / nrs.resolve_name / 任务 giver 锚定全依赖
    名字唯一；初建世界的 NPC 名由 LLM 直给、此前无去重守卫，两个同名 NPC 会让所有按名解析
    都命中第一个（真机「任务发布人错位」的可疑成因之一）。纯函数、确定性（首次出现保名）。
    """
    base = str(name or "").strip() or "未命名NPC"
    out = base
    k = 2
    while out in seen:
        out = f"{base}{k}"
        k += 1
    seen.add(out)
    return out


def _resolve_quest_giver(giver_raw: str, npc_by_temp: dict, dup_ids: set) -> str:
    """[修 2026-09-10] 任务发布者解析（守 [P44] 名字容错 + 歧义不猜）。

    骨架 schema 规范 giver 填 temp_id，但弱模型偶尔填真实名/带修饰名——原实现只做 dict 键
    精确查，填名会静默变「发布者：未知」。现改为：temp_id 精确命中（该 id 在骨架里重复过 =
    歧义，直接不猜，避免指到另一个人造成「发布人错位」）-> 回退按名容错解析（nrs）。
    解析不到返回空串——quest_engine 对空发布者豁免校验，任务仍可接领，不误导玩家跑错人。
    """
    raw = str(giver_raw or "").strip()
    if not raw:
        return ""
    if raw not in dup_ids:
        npc = npc_by_temp.get(raw)
        if npc is not None:
            return npc.id
    hit = nrs.resolve_name(raw, [x.name for x in npc_by_temp.values()])
    npc = next((x for x in npc_by_temp.values() if x.name == hit), None) if hit else None
    return npc.id if npc is not None else ""


def _sanitize_skill_inflicts(raw) -> list:
    """[P30/Q1] 清洗 LLM 技能的 inflicts 字段（condition 白名单 + chance 钳 0-1 + duration 钳 >=1）。

    与 Skill.from_dict 同口径：非 dict 项丢弃、condition 不在白名单丢弃、
    chance 钳 [0,1]、duration 钳 >=1。返回清洗后的 inflicts 列表（空列表=无附带）。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        if not isinstance(x, dict):
            continue
        ck = str(x.get("condition", "") or "").strip()
        if ck not in _CONDITION_VALUES or not ck:
            continue
        try:
            ch = max(0.0, min(1.0, float(x.get("chance", 1.0) or 1.0)))
        except (TypeError, ValueError):
            ch = 1.0
        try:
            dur = max(1, int(x.get("duration", 1) or 1))
        except (TypeError, ValueError):
            dur = 1
        out.append({"condition": ck, "chance": ch, "duration": dur})
    return out


def _sanitize_skill_target_pattern(raw) -> str:
    """[P30/Q1] 清洗 LLM 技能的 target_pattern 字段（白名单，非法回退 single）。"""
    tp = str(raw or "single").strip()
    return tp if tp in _TARGET_PATTERN_VALUES else "single"


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON：优先 ```json 代码块，否则尝试整段 json.loads。"""
    if not text:
        return None
    # 去 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text.strip()
    # 兜底：若仍含围栏残片，取首个 { 到末个 } 之间
    if raw.startswith("```"):
        s = raw.find("{")
        e = raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            raw = raw[s:e + 1]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # 再兜底：截取首个 { 到末个 }
        s = raw.find("{")
        e = raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(raw[s:e + 1])
            except (json.JSONDecodeError, ValueError):
                return None
        return None


# [P10c] 滴答/叙事共享的时间天气与近期事件格式化（单一来源，避免三处复制口径漂移）。
# 复用点：_build_scene_context（叙事）、_build_key_npc_user_message / _build_reconcile_user_message（滴答）。
_PHASE_ZH = {"dawn": "清晨", "day": "白昼", "dusk": "黄昏", "night": "夜晚"}
_WEATHER_ZH = {"clear": "晴朗", "cloud": "多云", "rain": "下雨", "fog": "起雾", "storm": "风暴"}
_EVENT_SEV_TAG = {"crisis": "★危机", "major": "◆重大", "minor": "·动态", "trivial": "·传闻"}


def _season_mult(world, key: str) -> float:
    """[季节玩法化] 取当前季节的玩法系数（overlay 关闭时恒 1.0；零 preset 依赖）。"""
    from src.services import calendar_engine as _cale
    return _cale.season_fx(world).get(key, 1.0)


def _fmt_time_weather(world) -> str:
    """【时间天气】行：历法日期（第N天） · 相位 · 天气 · 回合N（与 _build_scene_context 同口径）。

    [P24b] 第N天升级为历法日期（第X年春二月十五 · ...），LLM 获得年/季/月/日感知
    （节日名/收成/季节描写有锚点）。
    [修 2026-10-01] 括号补回数字天数（P24b 曾移除，"日期即其派生"）：剧情线
    （arc_due 水位/阶段 days/due_day）、日计划、议程全按 day_count 走，LLM 只有
    历法日期+回合数无法锚定"今天第几天"（day_count=tick//12+1 的换算从未告知 LLM）。
    单一来源，叙事/滴答/股市/私聊/剧情线规划与续写五个消费面同步生效。
    """
    phase_zh = _PHASE_ZH.get(world.time_phase, "白昼")
    weather_zh = _WEATHER_ZH.get(world.weather, "晴朗")
    cal = cale.calendar(world)
    day_count = max(1, int(getattr(world, "day_count", 1) or 1))
    line = (f"【时间天气】{cal['date_str']}（第{day_count}天） · {phase_zh} · "
            f"{weather_zh} · 回合{world.tick_count}")
    # [季节玩法化 2026-09-06] 季节玩法提示（冬粮价上浮/野兽觅食等——玩家与 LLM 共同知晓）
    try:
        from src.services import calendar_engine as _cale2
        _fx = _cale2.season_fx(world)
        if _fx.get("hint"):
            line += f"（{_fx['hint']}）"
    except Exception:
        pass
    return line


def fmt_time_weather_badge(world) -> str:
    """场景页左栏时间徽章文本：历法日期 · 相位 · 天气（不含回合，回合有独立徽章；UI 单一来源）。"""
    phase_zh = _PHASE_ZH.get(world.time_phase, "白昼")
    weather_zh = _WEATHER_ZH.get(world.weather, "晴朗")
    date_str = cale.calendar(world)["date_str"]
    return f"{date_str}·{phase_zh}·{weather_zh}"


# ============ [P18] 旁白首行 NPC 移动命令协议 ============
# 叙事 LLM 每次输出第一行必须是「[NPC] 名字→地点；名字→地点」（无移动写「[NPC] 无」），
# 引擎解析命令行同步 npc.location_id（与叙事声明的现场一致），命令行从显示/落盘文本剥离。
# 取代两代旧方案（settle effects 到场声明解析 / talk_to 兜底传唤）：旁白是现场的最终作者，
# 让它自己下移动命令，数据与叙事不再有第二条解释路径。
_NPC_CMD_PREFIX = "[NPC]"


def parse_npc_cmd_line(line: str) -> "Optional[list[tuple[str, str]]]":
    """解析单行是否为 NPC 移动命令行。非命令行返回 None；无移动（[NPC] 无）返回 []。

    [修 2026-09-06 真机] 前缀大小写不敏感：narrate LLM 偶发输出小写 `[npc] 无`，
    旧 `startswith("[NPC]")` 严格匹配致命令行整段泄漏进气泡与场景日志
    （P18「宽容降级原样返回」兜不住大小写变体）。两种写法前缀等长，切片不受影响。
    """
    t = (line or "").strip()
    if not t.lower().startswith(_NPC_CMD_PREFIX.lower()):
        return None
    body = t[len(_NPC_CMD_PREFIX):].strip()
    if not body or body in ("无", "无。", "无移动", "none", "NONE", "-"):
        return []
    cmds: "list[tuple[str, str]]" = []
    for part in re.split(r"[；;]", body):
        part = part.strip().rstrip("。.")
        if not part:
            continue
        m = re.match(r"^(.+?)(?:→|->|=>)(.+)$", part)
        if m:
            nm, dest = m.group(1).strip(), m.group(2).strip()
            if nm and dest:
                cmds.append((nm, dest))
    return cmds


class _NpcCmdStreamFilter:
    """叙事流头部过滤器（[P18]）：识别「前导空白行 + 首非空行 [NPC] 命令行」并整段吞掉
    （命令行及其后紧邻空白行不进 UI），正文一经确认即直通。首非空行不是命令行则整段原样
    放出（一字不丢）。正文内出现的 [NPC] 行不受影响（只认头部）。

    实测教训：MiniMax 类模型常在命令行前输出空行（think 收尾残留），旧实现只查「真正的
    第一行」，空行开头即漏剥——命令行混进旁白正文。
    """

    __slots__ = ("_emit", "_buf", "_state")

    def __init__(self, emit: "Callable[[str], None]"):
        self._emit = emit
        self._buf = ""
        self._state = "head"  # head -> pass | swallow（吞命令行后空白行直到正文）

    def feed(self, text: str):
        if not text:
            return
        if self._state == "pass":
            self._emit(text)
            return
        self._buf += text
        if self._state == "swallow":
            self._drain_swallow()
        else:
            self._drain_head()

    def _drain_head(self):
        """裁决首非空行：是命令行 -> 吞掉进入 swallow；否则整段放出进入 pass。"""
        buf = self._buf
        idx = 0
        while True:
            nl = buf.find("\n", idx)
            if nl < 0:
                return  # 首非空行尚未收齐换行（或全空白），继续缓冲
            seg = buf[idx:nl]
            if seg.strip():
                cmds = parse_npc_cmd_line(seg)
                head_end = nl + 1
                if cmds is None:
                    self._state = "pass"
                    self._buf = ""
                    self._emit(buf[:head_end])
                    if head_end < len(buf):
                        self._emit(buf[head_end:])
                else:
                    self._state = "swallow"
                    self._buf = buf[head_end:]
                    self._drain_swallow()
                return
            idx = nl + 1

    def _drain_swallow(self):
        """吞掉命令行后的空白行，直到出现正文首字符 -> 放出并转 pass。"""
        buf = self._buf
        idx = 0
        while True:
            nl = buf.find("\n", idx)
            seg = buf[idx:nl] if nl >= 0 else None
            if seg is not None:
                if seg.strip():
                    self._state = "pass"
                    self._buf = ""
                    self._emit(buf[idx:])
                    return
                idx = nl + 1
            else:
                rest = buf[idx:]
                if rest.strip():
                    self._state = "pass"
                    self._buf = ""
                    self._emit(rest)
                # 剩余全空白：继续缓冲等正文
                return

    def flush(self):
        """流结束：裁决未收齐换行的首非空行（命令行吞掉，正文放出）。"""
        if self._state == "pass" or not self._buf:
            self._buf = ""
            return
        buf = self._buf
        self._buf = ""
        if self._state == "swallow":
            # 剩余应是空白（非空白尾段已在 _drain_swallow 转 pass），丢弃
            return
        idx = 0
        while True:
            nl = buf.find("\n", idx)
            seg = buf[idx:nl] if nl >= 0 else buf[idx:]
            if seg.strip():
                if parse_npc_cmd_line(seg) is None:
                    self._emit(buf)
                else:
                    tail = buf[nl + 1:] if nl >= 0 else ""
                    if tail:
                        self._emit(tail)
                return
            if nl < 0:
                self._emit(buf)  # 全空白
                return
            idx = nl + 1


def _split_head_cmd(full: str) -> "tuple[Optional[list[tuple[str, str]]], str]":
    """头部命令剥离（_finalize_narrative 用，与流式过滤器同口径）：跳过前导空白行，
    首非空行是 [NPC] 命令行则返回 (cmds, 剥净正文——含命令行后紧邻空白行)，否则
    (None, 原文原样)。正文内的 [NPC] 行不受影响（只认头部）。"""
    if not full:
        return None, full
    lines = full.split("\n")
    first_i = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first_i is None:
        return None, full
    cmds = parse_npc_cmd_line(lines[first_i])
    if cmds is None:
        return None, full
    j = first_i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return cmds, "\n".join(lines[j:])


def scrub_npc_cmd_head(text: str) -> str:
    """清洗历史条目中泄漏的头部 [NPC] 命令行（旧版漏剥落盘数据，曾整行存进场景日志）。

    与 _split_head_cmd 同口径：首非空行是命令行则剥净（含其后紧邻空白行），否则原文
    原样返回。正文中部的 [NPC] 行不受影响。供回灌 LLM 的上下文（最近经历/前情摘要/
    NPC 记忆）使用，避免泄漏的命令行被 LLM 当成已执行的移动或指令。
    """
    cmds, body = _split_head_cmd(text or "")
    return body if cmds is not None else (text or "")


def _world_anchor(world) -> str:
    """[修 2026-09-10] 世界观锚行单一口径（守 §23「提示词-数据池联动」/ OOC 开发规则）：
    `【世界观】premise；基调：tone；题材：tags；货币：X`。新增 LLM 调用的 user 消息一律带此行
    （premise/tone/genre_tags 是防题材串味的最便宜手段）。world 为 None 返回空串。
    [修 2026-10-02 真机 OOC·货币串味] 追加货币名（GenreText 单一来源）——末日档世界书
    词条/剧情线/旁白三处写「瓶盖」而引擎全用「晶核」的口径分裂，锚行带货币名让
    全部 LLM 调用共享同一信号。"""
    if world is None:
        return ""
    premise = str(getattr(world, "premise", "") or "").strip()
    tone = str(getattr(world, "tone", "") or "").strip() or "默认"
    _tags = getattr(world, "genre_tags", None)
    genres = (", ".join(str(g) for g in _tags)
              if isinstance(_tags, (list, tuple)) and _tags else "通用")
    try:
        from src.models.world_sim_preset import GenreText
        currency = GenreText(getattr(world, "config_overlay", None) or {}).currency
    except Exception:
        currency = ""
    cur_part = f"；货币：{currency}" if currency else ""
    return f"【世界观】{premise}；基调：{tone}；题材：{genres}{cur_part}"


def _fmt_world_lore(world) -> str:
    """世界书词条一行（[2026-08-28 用户指示] 全量注入，不再 [:5] 截断）：`【世界书】key：content`。

    世界书是用户手写的稳定内容（每条几十字），全量注入也就几百 token 且在缓存前缀段；
    旧 [:5] 截断在多条同优先级时等于随机丢弃一半词条（夜城档 10 条主要角色人设丢 5 条）。
    空世界书返回空串（调用方跳过）。scene 上下文与滴答提示词共用，格式口径一致。"""
    if not getattr(world, "lore", None):
        return ""
    lore = sorted(world.lore, key=lambda e: e.priority)
    return "【世界书】\n" + "\n".join(f"  {e.key}：{e.content}" for e in lore)


def _fmt_user_identity(user) -> str:
    """[2026-08-23 用户卡绑定] 用户卡身份行：`姓名：X；人设：desc`。
    user 为 None / 姓名与人设皆空返回空串（调用方跳过）。供场景上下文、滴答要角
    消息、NPC 记忆整理共用——NPC 从此知道玩家叫什么，不再「那个男人」模糊指代。"""
    if user is None:
        return ""
    name = str(getattr(user, "name", "") or "").strip()
    desc = str(getattr(user, "description", "") or "").strip()
    if not name and not desc:
        return ""
    parts = []
    if name:
        parts.append(f"姓名：{name}")
    if desc:
        parts.append(f"人设：{desc}")
    return "；".join(parts)


# [P] 势力好战度/敌对立场的题材无关关键词（name+ideology+desc 三源合一）。
# 旧实现只认 ideology 且关键词极窄，LLM 一旦用同义词（如「占山为王」而非「侵略」）
# 就漏配 -> 镇 vs 山贼变中立 -> 势力战整套哑火。
_FACTION_AGGRESSIVE_KW = ("山贼", "劫掠", "掠夺", "占山", "聚敛", "征服", "扩张", "侵略",
                          "霸权", "战争", "吞并", "好战", "入侵", "征伐", "厮杀")
_FACTION_DEFENSIVE_KW = ("安居", "镇民", "守卫", "护卫", "防御", "和平", "中立", "保守",
                         "隐世", "守成", "自保", "共御", "农耕", "贸易")


def _faction_aggressiveness(f: "Faction") -> int:
    """据势力 name+ideology+desc 推断好战度（三源合一，比只认 ideology 稳）。"""
    hay = f"{f.name} {f.ideology or ''} {f.desc or ''}"
    if any(k in hay for k in _FACTION_AGGRESSIVE_KW):
        return 75
    if any(k in hay for k in _FACTION_DEFENSIVE_KW):
        return 30
    return 50


# [P22] 旁白漏声明到场兜底：离屏提及判定用。
# 名字出现处 ±_MENTION_WINDOW 字内出现这些标记词，视为「背景传闻/回忆/别处动态」式提及，
# 不代表人物在现场（守 narrative 规则 12 的离场方式措辞）。
_OFFSCREEN_MARKERS = (
    "传闻", "听说", "据说", "口信", "来信", "捎信", "捎话", "托人", "失踪",
    "忆起", "回想", "想起", "曾说", "当年", "临终",
    "不在", "已去", "远去", "远在", "离去", "离开", "告辞",
)
_MENTION_WINDOW = 6

# [2026-08-23 三次真人测试] 对话/心声/符号的成对定界符（叙事规则 1 要求 NPC 对话用引号
# 包裹；与聊天侧渲染规则同一套标记）。兜底同步扫正文判「在场式提及」时，名字只出现在
# 引号/括号内 = 只是被对话谈论（人不在现场），据此同步必 OOC——万宝楼谈铁蛟/病书生
# 竟把二人拽来。扫描前先把这些成对区间内容抹白，只用引号外的纯旁白做在场判定。
_DIALOGUE_PAIRS = (
    ("\u201c", "\u201d"),   # "…"（中文弯双引号）
    ("\u2018", "\u2019"),   # '…'（中文弯单引号）
    ("\u300c", "\u300d"),   # 「…」
    ("\u300e", "\u300f"),   # 『…』
    ("\u3010", "\u3011"),   # 【…】
    ("\u300a", "\u300b"),   # 《…》
    ("\uff08", "\uff09"),   # （…）（心声，同聊天渲染规则）
    ("[", "]"),
    ("(", ")"),
)
# 同字符成对引号（ASCII 直引号）：按出现次序奇开偶合配对
_DIALOGUE_SAME = ("\"", "'")


def _strip_dialogue_spans(text: str) -> str:
    """把对话/心声/符号的引号对内容连同定界符抹成空格（保留长度与字符位置，
    下游 ±窗口/「的」物性判定口径不变），只剩引号外纯旁白参与在场提及扫描。
    找不到闭引号（LLM 漏闭合）保守跳过该开引号不抹到段尾——宁漏同步不误抹整段正文。"""
    if not text:
        return text
    spans = []
    for opener, closer in _DIALOGUE_PAIRS:
        idx = 0
        while True:
            s = text.find(opener, idx)
            if s < 0:
                break
            e = text.find(closer, s + len(opener))
            if e < 0:
                break
            spans.append((s, e + len(closer)))
            idx = e + len(closer)
    for q in _DIALOGUE_SAME:
        pos = []
        idx = 0
        while True:
            p = text.find(q, idx)
            if p < 0:
                break
            pos.append(p)
            idx = p + 1
        for k in range(0, len(pos) - 1, 2):
            spans.append((pos[k], pos[k + 1] + 1))
    if not spans:
        return text
    chars = list(text)
    for s, e in spans:
        for i in range(s, min(e, len(chars))):
            chars[i] = " "
    return "".join(chars)


def _mention_is_off_screen(body: str, idx: int, name: str,
                           cur_loc_name: str, loc_names: "list[str]") -> bool:
    """[P22] 名字出现在 body[idx] 处是否为离屏提及：±6 字窗口内有离屏标记词，
    或出现「当前地点之外」的地名（人物被叙事绑定在别处）-> True。"""
    lo = max(0, idx - _MENTION_WINDOW)
    hi = min(len(body), idx + len(name) + _MENTION_WINDOW)
    win = body[lo:hi]
    if any(m in win for m in _OFFSCREEN_MARKERS):
        return True
    return any(ln != cur_loc_name and ln in win for ln in loc_names)


def _fmt_recent_events(world, n: int = 5) -> str:
    """近期事件行（不含标题块）：取 event_log 末 n 条，`  [回合|严重度标签] 标题：描述`。
    无事件返回空串。调用方按需拼【近期世界动态】标题±后缀（scene_context 带离屏提示后缀，
    滴答方法不带），以保持 scene_context 与旧版逐字等价。"""
    if not world.event_log:
        return ""
    recent = world.event_log[-n:]
    return "\n".join(f"  [回合{e.tick}|{_EVENT_SEV_TAG.get(e.severity, '·动态')}] {e.title}：{e.desc}"
                     for e in recent)


class WorldSimService:
    """世界模拟服务。"""

    def __init__(self, storage: Storage, comfyui_service, danbooru_service):
        self.storage = storage
        self.comfyui = comfyui_service
        self.danbooru = danbooru_service
        self._npc_mem_svc = None  # [P6c] 懒加载 NpcMemoryService

    def npc_memory(self):
        """[P6c] 懒加载 NPC 个人记忆服务（独立于角色记忆 memory_service）。"""
        if self._npc_mem_svc is None:
            from src.services.npc_memory_service import NpcMemoryService
            self._npc_mem_svc = NpcMemoryService(self.storage)
        return self._npc_mem_svc

    def _bound_user(self, world: World):
        """[2026-08-23 用户卡绑定] 读世界绑定的用户卡；未绑定/用户已删/无 storage
        返回 None（调用方退回无名玩家口径，不阻断）。"""
        uid = str(getattr(world, "user_id", "") or "")
        if not uid or self.storage is None:
            return None
        try:
            return self.storage.load_user(uid)
        except Exception:
            return None

    def _fmt_bound_user(self, world: World) -> str:
        """[2026-08-23] 绑定用户卡身份行（_fmt_user_identity 口径）；未绑返回空串。"""
        return _fmt_user_identity(self._bound_user(world))

    # ============ 基础工具 ============
    def _jb_prefix(self) -> str:
        """读取破限前缀：开关关或 prefix 空返回空串（LlmClient 收到空串不注入）。"""
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    def _resolve_api(self, api_id: str) -> Optional[ApiConfig]:
        """三级 API 兜底（仿 danbooru._resolve_llm_api）：api_id -> 首个 enabled。
        v1 世界生成不绑定会话，无 session_api 层。"""
        if api_id:
            api = self.storage.load_api(api_id)
            if api and api.enabled:
                return api
        for api in self.storage.load_all_apis():
            if api.enabled:
                return api
        return None

    # ============ 世界骨架生成（结算 LLM）============
    def generate_world_skeleton(
            self, form: dict, preset: WorldSimPreset,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[Optional[dict], str]:
        """拆分生成世界骨架（[2026-08-25 用户指示] 细拆避免注意力稀释）。

        按类型多次 LLM 调用：物品 -> 技能+天赋 -> 世界骨架 -> 怪物 -> NPC -> 配方任务，
        后段注入前段产出的真实名字清单（守交叉引用）。合并成一份骨架 dict，
        与 build_world_from_skeleton 消费的旧单次 schema 兼容。

        返回 (skeleton_dict|None, error_msg)。关键段（物品/技能/世界）失败即整体失败；
        非关键段（怪物/NPC/配方任务）失败降级空（引擎兜底池补）。
        """
        api = self._resolve_api(preset.calculator_api_id)
        if not api:
            return None, "未配置可用的 LLM API，请先在「设置 -> API 与预设」中添加并启用一个 API。"

        tmp_preset = Preset(
            name="world_sim_gen",
            system_prompt="",  # 每段用各自的 system prompt（_GEN_*_SYSTEM_PROMPT）
            temperature=preset.calculator_temperature,
            # [!] 下限 40000（reasoning 模型思考可吃 16-19k，25k 下限把 JSON 挤到截断）。
            max_tokens=max(40000, int(preset.calculator_max_tokens or 40000)),
            top_p=preset.calculator_top_p,
        )
        # [!] 世界生成读超时 600s（非流式大 JSON 可跑 2-5 分钟）。
        llm = LlmClient(api, tmp_preset, timeout=600.0, jailbreak_prefix=self._jb_prefix())

        ctx = self._build_world_context(form, preset)

        # 1) 物品池
        items_raw, err = self._gen_section(
            llm, _GEN_ITEMS_SYSTEM_PROMPT,
            self._build_items_user_message(form, preset, ctx), cancel_check, "物品")
        if items_raw is None:
            return None, f"物品生成失败：{err}"
        items = (items_raw.get("items") or []) if isinstance(items_raw, dict) else []

        # 2) 技能 + 天赋池（失败降级空：引擎 _init_skill_pool/_init_talent_pool 有题材兜底池，
        # 不拖垮整个世界生成——实机验证 apocalypse 曾因技能段两次非 JSON 输出致整体失败）
        skills_raw, err = self._gen_section(
            llm, _GEN_SKILLS_SYSTEM_PROMPT,
            self._build_skills_user_message(form, preset, ctx), cancel_check, "技能")
        if skills_raw is None:
            debug_log(lambda: f"[WorldSim] 技能生成失败({err})，回退题材兜底池")
            skills_raw = {}
        skills_raw = skills_raw if isinstance(skills_raw, dict) else {}

        # 3) 世界骨架（worldbook/factions/locations/attribute_template/起始）
        world_raw, err = self._gen_section(
            llm, _GEN_WORLD_SYSTEM_PROMPT,
            self._build_world_user_message(form, preset, ctx), cancel_check, "世界")
        if world_raw is None:
            return None, f"世界骨架生成失败：{err}"
        world_raw = world_raw if isinstance(world_raw, dict) else {}

        # 收集引用清单（阶段 2 注入）
        item_names = [
            (str(it.get("name"))
             + ("[禁随身]" if str(it.get("rarity", "common") or "common")
                in ("legendary", "mythic") else ""))
            for it in items if isinstance(it, dict) and it.get("name")]
        mat_names = [it.get("name") for it in items
                     if isinstance(it, dict) and str(it.get("type") or "") == "material" and it.get("name")]
        # [修 2026-10-01] 材料品级标注（怪物 LLM 之前只看到名字，不知道品级——
        # danger 1 野兔掉橙色材料、danger 10 Boss掉白装均源于此盲区）
        mat_rarities = {str(it.get("name")): str(it.get("rarity", "common") or "common")
                        for it in items
                        if isinstance(it, dict) and it.get("name")
                        and str(it.get("type") or "") == "material"}
        skill_names = [s.get("name") for s in (skills_raw.get("skill_pool") or [])
                       if isinstance(s, dict) and s.get("name")]
        talent_names = [t.get("name") for t in (skills_raw.get("talent_pool") or [])
                        if isinstance(t, dict) and t.get("name")]
        loc_names = [l.get("name") for l in (world_raw.get("locations") or [])
                     if isinstance(l, dict) and l.get("name")]
        fac_names = [f.get("name") for f in (world_raw.get("factions") or [])
                     if isinstance(f, dict) and f.get("name")]
        # [环环相扣 2026-09-13 用户指示] 后段调用注入引擎消费的语义词表（此前只给名字
        # 清单，语义维度裸奔致任务/怪物与世界脱节）：
        # - 野外 danger 档分布 -> 怪物段（danger_min/max 须覆盖至少一档，否则永不刷新）
        # - 资源点类型表 -> 任务段（gather target 只能从中选，否则引擎 type 匹配永不命中）
        _raw_locs = [l for l in (world_raw.get("locations") or []) if isinstance(l, dict)]
        wild_dangers = sorted({int(l.get("danger") or 0)
                               for l in _raw_locs
                               if str(l.get("kind") or "") == "wilderness" and l.get("danger")})
        res_types = []
        for l in _raw_locs:
            if str(l.get("kind") or "") != "wilderness":
                continue
            r = l.get("resource")
            if isinstance(r, dict):
                rt = str(r.get("type") or "").strip()
                if rt and rt not in res_types:
                    res_types.append(rt)

        # 4) 怪物 + 怪物技能池（注入材料名 + 野外 danger 档分布）
        mon_raw, merr = self._gen_section(
            llm, _GEN_MONSTERS_SYSTEM_PROMPT,
            self._build_monsters_user_message(form, preset, ctx, mat_names,
                                              wild_dangers=wild_dangers,
                                              mat_rarities=mat_rarities), cancel_check, "怪物")
        monsters, monster_skill_pool = [], []
        if mon_raw is None:
            debug_log(lambda: f"[WorldSim] 怪物生成失败({merr})，回退题材兜底池")
        elif isinstance(mon_raw, dict):
            monsters = mon_raw.get("monsters") or []
            monster_skill_pool = mon_raw.get("monster_skill_pool") or []

        # 5) NPC（注入物品/天赋/技能/地点/势力名 + 世界书词条[名字硬约束]）
        npc_raw, nerr = self._gen_section(
            llm, _GEN_NPCS_SYSTEM_PROMPT,
            self._build_npcs_user_message(form, preset, ctx, item_names, talent_names,
                                          loc_names, fac_names,
                                          worldbook=world_raw.get("worldbook") or []),
            cancel_check, "NPC")
        npcs = []
        if npc_raw is None:
            debug_log(lambda: f"[WorldSim] NPC 生成失败({nerr})，回退空（引擎补平民）")
        elif isinstance(npc_raw, dict):
            npcs = npc_raw.get("npcs") or []

        # 6) 配方 + 任务（注入物品/NPC/地点/怪物名 + 题材货币，防 reward 文案写错货币）
        npc_refs = [n.get("temp_id") or n.get("name") for n in npcs
                    if isinstance(n, dict) and (n.get("temp_id") or n.get("name"))]
        mon_names = [m.get("name") for m in monsters if isinstance(m, dict) and m.get("name")]
        currency_name = "金币"
        try:
            _tid = _resolve_template_id(str(world_raw.get("attribute_template_id") or ""), preset)
            _tmpl = _get_template_by_id(_tid, preset)
            if _tmpl:
                currency_name = str(_tmpl.get("currency_name") or "金币")
        except Exception:
            pass
        rq_raw, rqerr = self._gen_section(
            llm, _GEN_RECIPES_QUESTS_SYSTEM_PROMPT,
            self._build_recipes_quests_user_message(form, preset, ctx, item_names,
                                                    npc_refs, loc_names, mon_names,
                                                    currency_name,
                                                    res_types=res_types,
                                                    obtainable_names=self._compute_obtainable_names(
                                                        items, mon_raw, world_raw)),
            cancel_check, "配方任务")
        recipes, quests = [], []
        if rq_raw is None:
            debug_log(lambda: f"[WorldSim] 配方任务生成失败({rqerr})，回退引擎兜底")
        elif isinstance(rq_raw, dict):
            recipes = rq_raw.get("recipes") or []
            quests = rq_raw.get("quests") or []

        # 合并骨架（与旧单次 schema 兼容）
        player_skill = skills_raw.get("player_skill")
        player_skills = ([player_skill] if isinstance(player_skill, dict) and player_skill.get("name")
                         else [])
        sk = {
            "worldbook": world_raw.get("worldbook") or [],
            "factions": world_raw.get("factions") or [],
            "locations": world_raw.get("locations") or [],
            "npcs": npcs,
            "items": items,
            "recipes": recipes,
            "monsters": monsters,
            "monster_skill_pool": monster_skill_pool,
            "quests": quests,
            "player_start": {
                "location": world_raw.get("player_location") or "",
                "background": world_raw.get("background") or "",
                "suggested_class": world_raw.get("suggested_class") or "",
                "skills": player_skills,
                "skill_pool": skills_raw.get("skill_pool") or [],
                "talent_pool": skills_raw.get("talent_pool") or [],
                "attribute_template_id": world_raw.get("attribute_template_id") or "",
            },
            "world_name": world_raw.get("world_name") or "",
        }
        return sk, ""

    def _gen_section(self, llm, system_prompt: str, user_msg: str,
                     cancel_check, name: str) -> tuple[Optional[dict], str]:
        """单段 LLM 调用 + 重试一次（回灌错误）。返回 (dict|None, err)。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]
        sk, err = self._call_and_parse(llm, messages, cancel_check)
        if sk is not None:
            return sk, ""
        if cancel_check and cancel_check():
            return None, "已取消"
        retry_user = (
            user_msg
            + "\n\n[!] 你上次的输出无法解析为合法 JSON，错误："
            + (err or "JSON 格式错误")
            + "。请严格按 schema 重新输出纯 JSON，不要任何说明或代码块标记。"
        )
        sk2, err2 = self._call_and_parse(llm, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": retry_user},
        ], cancel_check)
        if sk2 is not None:
            return sk2, ""
        if cancel_check and cancel_check():
            return None, "已取消"
        return None, f"{name}生成失败：{err2 or err or '未知错误'}"

    def _build_gen_user_message(self, form: dict, preset: WorldSimPreset) -> str:
        """把表单数据拼成给 LLM 的 user 消息。"""
        parts = []
        parts.append(f"【世界观】\n{form.get('premise', '').strip()}")
        if form.get("extra"):
            parts.append(f"【补充设定】\n{form['extra'].strip()}")
        tags = form.get("genre_tags") or []
        if tags:
            parts.append(f"【题材标签】{', '.join(tags)}")
            # [题材对齐 2026-10-01] 标签命中映射时硬指定/收窄 template_id
            _directive = _genre_tag_directive(tags)
            if _directive:
                parts.append(_directive)
            # [修 2026-10-02 真机 OOC·货币串味] 标签唯一命中模板时注入货币硬约束
            _cur = _genre_currency_hint(tags)
            if _cur:
                parts.append(_cur)
        if form.get("tone"):
            parts.append(f"【基调】{form['tone']}")
        parts.append(f"【魔法等级】{form.get('magic_level', 0)}/100（0=无魔，100=高魔）")
        parts.append(f"【科技等级】{form.get('tech_level', 0)}/100（0=古代，100=未来）")
        # [P5b] 规模提示：按当前 scale_id 找 hint；找不到时回退内置/自定义提示。
        # 供 LLM 决定产出多少地点/NPC/任务/物品参考。
        scale_id = form.get("scale", preset.default_world_scale) or preset.default_world_scale
        scale_hint = _resolve_scale_hint(scale_id, preset)
        parts.append(f"【规模】{scale_id}（{scale_hint}）")
        # [P7d] 物品数量指引（走 preset.item_gen_count 旋钮；覆盖武器/防具/饰品/消耗品/材料）
        ic = max(4, min(200, int(getattr(preset, "item_gen_count", 12) or 12)))
        parts.append(f"【物品数量指引】{self._item_quota_text(ic)}")
        # [消耗品效果] 指引 LLM 给高品消耗品填 consume_effect（回蓝/五维/解毒/复活/食物）
        parts.append(
            "【消耗品效果】consumable 类物品可选填 consume_effect 字段（结构化效果，引擎据此结算；"
            "纯回血不填本字段、只填 heal_pct 顶层字段）："
            '{"type":"stat_bonus","stats":{"str":2,"int":1}} 永久加五维（str/dex/int/vit/luk，'
            '高档丹药如洗髓丹/筑基丹适合）；'
            '{"type":"heal_mp","amount":30} 回蓝；{"type":"cure"} 清负面状态；'
            '{"type":"revive"} 复活。回血类消耗品一律填 heal_pct（回复最大生命百分比 5-50：'
            '小还丹 10% / 续命丹 25% / 九转还魂丹 45%），'
            "rare+ 的丹药优先给 consume_effect 让效果名副其实（desc 写什么效果就填什么 type）。"
        )
        # [用户指示 2026-08-25] 物品属性 LLM 全权产出：字段说明 + 品级x属性范围表（引擎常量同源）。
        # LLM 按范围自填 attack/defense/heal_pct/stat_bonus/level；效果先行铁律防 desc 与数值不符；
        # 数值是唯一真相源（系统按真实数值生成 effects 展示行），从源头杜绝 OOC。
        parts.append(
            "【物品字段说明（防描述与效果不符）】物品各字段含义：name 名字；desc 背景故事与氛围文案；"
            "effects 效果展示行（可留空——系统会按你填的真实数值自动生成）；"
            "type 类型；rarity 品级；slot 装备槽；attack/defense/heal_pct/stat_bonus/level 数值字段"
            "（按下方的【物品属性范围表】按品级自填，填 0 系统才兜底补；回血类填 heal_pct 百分比 5-50）。"
            "生成顺序铁律：先定结构化效果"
            "（consumable 填 consume_effect 或 heal_pct；装备填 attack/defense/stat_bonus），"
            "再写 desc——desc 只许描述已填的效果，绝不提及未填的额外功效"
            "（例：只填了回血却写「五维俱增/起死回生」是严重违规）。"
            "装备 desc 不许写具体数值承诺（如「攻击+100」）——数值以你填的字段为准，写了文案必然不符。"
        )
        parts.append(_item_attr_ranges_text())
        parts.append(f"【NSFW】{'允许' if form.get('nsfw') else '不允许'}")
        # [P10c] NPC 性别倾向：不限=不注入（当前行为）；全女性/全男性=动态注入一句约束，
        # 让 LLM 在 npc.appearance（含性别）中统一该性别。纯提示词注入不改 schema/系统提示词默认值，
        # 故无 PROMPT_DEFAULTS_REV 版本门（守 §22）。怪物/随从等临时单位非此约束目标。
        g = (form.get("npc_gender") or "").strip()
        if g == "female":
            parts.append("【NPC 性别】世界中的全部 NPC 均为女性（含 appearance 的性别/外貌描写）。")
        elif g == "male":
            parts.append("【NPC 性别】世界中的全部 NPC 均为男性（含 appearance 的性别/外貌描写）。")
        # [P5c] 属性/装备模板库：注入内置 6 模板 + 用户自定义模板，让 LLM 选 template_id。
        # LLM 据题材标签挑最贴合的（如仙侠 -> xianxia），不选时引擎兜底 western_fantasy。
        from src.models.world_sim_preset import (
            BUILTIN_ATTRIBUTES_TEMPLATES, DEFAULT_ATTRIBUTE_TEMPLATE_ID,
        )
        tmpl_lines = []
        for t in BUILTIN_ATTRIBUTES_TEMPLATES:
            tmpl_lines.append(
                f"  - {t['id']}（{t['label']}）: "
                f"属性={list(t['stat_display_names'].values())}, "
                f"主手槽={t['slot_display_names']['main_hand']}"
            )
        for t in (preset.attribute_templates or []):
            if isinstance(t, dict) and t.get("id"):
                tmpl_lines.append(
                    f"  - {t['id']}（{t.get('label', t['id'])}）: "
                    f"属性={list(t.get('stat_display_names', {}).values())}, "
                    f"主手槽={t.get('slot_display_names', {}).get('main_hand', '?')}"
                )
        parts.append(
            "【属性/装备模板库】（请在 player_start.attribute_template_id 选一个 id，"
            "默认 " + DEFAULT_ATTRIBUTE_TEMPLATE_ID + "）\n" + "\n".join(tmpl_lines)
        )
        # [P9] 天赋池指引：数量走 preset.talent_pool_size（[P12] 可设）。
        # 题材化命名 + element + rarity + effects 种子值；引擎白名单钳制，缺失走题材兜底池。
        # [数据量铁律] 命名参考从引擎题材池动态取样（单一来源，池扩容提示词自动跟进）。
        tp_size = max(6, min(100, int(getattr(preset, "talent_pool_size", 10) or 10)))
        parts.append(
            f"【天赋池】请在 player_start.talent_pool 生成 {tp_size} 个天赋（题材化命名，"
            f"命名风格参考（各题材示例，可另创但保持同风味）：{_genre_naming_ref(te._GENRE_TALENT_TEMPLATES)}）。"
            "每条含 name/desc/rarity(common|uncommon|rare|epic|legendary|mythic)/element(空或 fire/thunder/ice/wind/wood/metal/light/dark/physical)"
            "/effects（钩子：skill_dmg_mult 技能伤害乘数/atk_mult 普攻乘数/gather_mult 采集/craft_mult 合成/check_mult 检定/"
            "loot_mult 掉落/luck_bonus 幸运加值/mp_bonus 法力加值/stat_bonus={属性:加值}；element 非空的 skill_dmg_mult 只加成同系技能）。"
            "effects 数值按 rarity 缩放（legendary 最强）。"
        )
        rc = self._effective_recipe_target(preset)
        parts.append(f"【配方数量】recipes 生成 {rc} 个（覆盖各资源层级，inputs/output 引用 items 真实物品名）。")
        # [P13] 技能池指引（preset.skill_pool_size 可设）；命名参考接引擎题材池
        sp_size = max(6, min(100, int(getattr(preset, "skill_pool_size", 10) or 10)))
        parts.append(f"【技能池数量】player_start.skill_pool 生成 {sp_size} 个可学习技能（不含开局 skills，"
                     "power 按 rarity 缩放；系统为每个技能生成技能书供玩家研读习得）。\n"
                     f"技能命名风格参考：{_genre_naming_ref(_GENRE_SKILL_TEMPLATES)}。")
        # [P12] 野外/资源指引（kind 分流 + 分层资源 + 怪物池）；怪物命名参考接引擎题材池
        mp_size = max(6, min(100, int(getattr(preset, "monster_pool_size", 8) or 8)))
        parts.append(
            "【地点与资源】locations 的 kind 必填：聚落（城/镇/村/坊市/驿站/港口/基地）= settlement 无 resource；"
            "野外（山林/矿脉/荒野/废墟/秘境）= wilderness 必须带 resource（tier 1-5 随危险度）。"
            "materials 要覆盖每个 tier（tier 对应稀有度 common/uncommon/rare/epic/legendary/mythic）。\n"
            f"monsters 出 {mp_size} 只题材怪物（danger_min/max 区间 + loot 引用材料名），"
            f"命名风格参考：{_genre_naming_ref(we.GENRE_MONSTER_TEMPLATES)}。"
        )
        parts.append("\n请严格按 schema 输出纯 JSON。")
        return "\n\n".join(parts)

    def _build_world_context(self, form: dict, preset: WorldSimPreset) -> list:
        """拆分生成的公共上下文前缀（世界观/题材/基调/魔法/科技/规模/NSFW/性别）。"""
        parts = []
        parts.append(f"【世界观】\n{form.get('premise', '').strip()}")
        if form.get("extra"):
            parts.append(f"【补充设定】\n{form['extra'].strip()}")
        tags = form.get("genre_tags") or []
        if tags:
            parts.append(f"【题材标签】{', '.join(tags)}")
            # [题材对齐 2026-10-01] 同 _build_gen_user_message：标签命中映射时硬指引
            _directive = _genre_tag_directive(tags)
            if _directive:
                parts.append(_directive)
        if form.get("tone"):
            parts.append(f"【基调】{form['tone']}")
        parts.append(f"【魔法等级】{form.get('magic_level', 0)}/100（0=无魔，100=高魔）")
        parts.append(f"【科技等级】{form.get('tech_level', 0)}/100（0=古代，100=未来）")
        scale_id = form.get("scale", preset.default_world_scale) or preset.default_world_scale
        scale_hint = _resolve_scale_hint(scale_id, preset)
        parts.append(f"【规模】{scale_id}（{scale_hint}）")
        # [2026-09-13 用户指示] 聚落数量设置（0=LLM 自由分配）：>0 注入硬性数量指引。
        # 野外保底 1 块（刷怪/采集全在野外）；超规模总地点数时按「留 1 块野外」钳制。
        _sc = max(0, int(getattr(preset, "gen_settlement_count", 0) or 0))
        if _sc > 0:
            _c = _resolve_scale_counts(scale_id, preset)
            _loc_n = int((_c or {}).get("locations") or 0)
            _eff = min(_sc, _loc_n - 1) if _loc_n > 1 else 0
            if _eff > 0:
                parts.append(f"【聚落与野外】共 {_loc_n} 个地点：聚落（settlement）恰好 {_eff} 个，"
                             f"野外（wilderness）恰好 {_loc_n - _eff} 个，两种都必须有。")
            else:
                parts.append("【聚落与野外】必须保留至少 1 个野外地点（wilderness）。")
        parts.append(f"【NSFW】{'允许' if form.get('nsfw') else '不允许'}")
        g = (form.get("npc_gender") or "").strip()
        if g == "female":
            parts.append("【NPC 性别】世界中的全部 NPC 均为女性（含 appearance 的性别/外貌描写）。")
        elif g == "male":
            parts.append("【NPC 性别】世界中的全部 NPC 均为男性（含 appearance 的性别/外貌描写）。")
        return parts

    @staticmethod
    def _item_quota_text(total: int) -> str:
        """[2026-08-28 修] 物品类型配额指引：材料系由引擎固定注入（reagent/种子/宠物食品
        ~45 件），LLM 配额应向装备倾斜（旧「五类全覆盖」平均分致装备 23 vs 材料 41 的
        稀释）。装备约占 45%（武器/防具/饰品约 1:2:1），消耗品 30%，材料 25%。
        武器数值须分层（旧实况 5 把全 atk=8 同数值，武器池没梯度）。"""
        t = max(4, int(total))
        eq = max(4, int(t * 0.45))
        we = max(1, eq // 4)
        ac = max(1, eq // 4)
        ar = max(2, eq - we - ac)
        cs = max(2, int(t * 0.30))
        mt = max(1, t - eq - cs)
        return (f"约 {t} 件：装备 {eq}（武器 {we}/防具 {ar}/饰品 {ac}，覆盖 8 槽位："
                f"head/chest/legs/feet/main_hand/off_hand/accessory1/accessory2 各至少 1 件）、"
                f"消耗品 {cs}、材料 {mt}（稀有度任选，系统自动补足各档次锻造/制造材料）。"
                f"武器数值分层拉开梯度"
                f"（common atk 6-10 / uncommon 10-14 / rare 14-18 / epic 18-24，勿全部同数值）；"
                f"防具 defense 按品级 4/6/8/10 递进")

    def _build_items_user_message(self, form: dict, preset: WorldSimPreset, ctx: list) -> str:
        ic = max(4, min(200, int(getattr(preset, "item_gen_count", 12) or 12)))
        parts = [
            f"【物品数量指引】{self._item_quota_text(ic)}",
            "【消耗品效果】consumable 类物品可选填 consume_effect 字段（结构化效果，引擎据此结算；"
            "纯回血不填本字段、只填 heal_pct 顶层字段）："
            '{"type":"stat_bonus","stats":{"str":2,"int":1}} 永久加五维（str/dex/int/vit/luk，'
            '高档丹药如洗髓丹/筑基丹适合）；'
            '{"type":"heal_mp","amount":30} 回蓝；{"type":"cure"} 清负面状态；'
            '{"type":"revive"} 复活。回血类消耗品一律填 heal_pct（回复最大生命百分比 5-50：'
            '小还丹 10% / 续命丹 25% / 九转还魂丹 45%），'
            "rare+ 的丹药优先给 consume_effect 让效果名副其实（desc 写什么效果就填什么 type）。",
            "【物品字段说明（防描述与效果不符）】name 名字；desc 背景故事与氛围文案；"
            "type 类型；rarity 品级；slot 装备槽；attack/defense/heal_pct/stat_bonus/level 数值字段"
            "（按下方【物品属性范围表】按品级自填，填 0 系统才兜底补；回血类填 heal_pct 百分比 5-50）。"
            "effects 展示行可留空（系统按"
            "真实数值自动生成）。生成顺序铁律：先定结构化效果再写 desc，desc 只许描述已填效果。",
            _item_attr_ranges_text(),
            "请严格按 schema 输出纯 JSON。",
        ]
        return "\n\n".join(ctx + parts)

    def _build_skills_user_message(self, form: dict, preset: WorldSimPreset, ctx: list) -> str:
        sp_size = max(6, min(100, int(getattr(preset, "skill_pool_size", 10) or 10)))
        tp_size = max(6, min(100, int(getattr(preset, "talent_pool_size", 10) or 10)))
        parts = [
            f"【技能池数量】skill_pool 生成 {sp_size} 个可学习技能（三分池：攻击/恢复/状态）。\n"
            f"技能命名风格参考（各题材示例，可另创但保持同风味）：{_genre_naming_ref(_GENRE_SKILL_TEMPLATES)}。",
            f"【天赋池数量】talent_pool 生成 {tp_size} 个天赋。\n"
            f"天赋命名风格参考：{_genre_naming_ref(te._GENRE_TALENT_TEMPLATES)}。",
            "请严格按 schema 输出纯 JSON。",
        ]
        return "\n\n".join(ctx + parts)

    def _build_monsters_user_message(self, form: dict, preset: WorldSimPreset, ctx: list,
                                     mat_names: list,
                                     wild_dangers: Optional[list] = None,
                                     mat_rarities: Optional[dict] = None) -> str:
        mp_size = max(6, min(100, int(getattr(preset, "monster_pool_size", 8) or 8)))
        parts = [
            f"【怪物池数量】monsters 出 {mp_size} 只题材怪物（danger 1-10 全覆盖）。\n"
            f"怪物命名风格参考：{_genre_naming_ref(we.GENRE_MONSTER_TEMPLATES)}。",
        ]
        # [环环相扣 2026-09-13] 注入本世界野外 danger 档分布：引擎按
        # danger_min<=地点danger<=danger_max 过滤刷出，区间与全部野外地无交集的怪
        # 永不刷新（其掉落与引用它的任务全断）。LLM 看不到地图就会按直觉写 1-2 档。
        if wild_dangers:
            parts.append(
                "【野外危险度分布】本世界野外地点的 danger 档：" + "、".join(str(d) for d in wild_dangers)
                + "。每只怪物的 danger_min/danger_max 区间必须至少覆盖其中一档"
                "（怪只刷在野外地；聚落 danger=0 永不刷怪，不要给怪配只覆盖聚落的区间）。")
        parts += [
            "【材料名清单】怪物的 loot 只能引用以下材料名（1-2 个，勿编造）："
            + ("、".join(mat_names) if mat_names else "（空——可只给元素/无掉落，引擎兜底材料）"),
            # [修 2026-10-01] 品级标注 + danger 档匹配指引（防 danger 1 野兔掉橙装）
            "【品级匹配铁律】loot 材料的品级须与怪物 danger 档匹配："
            "danger 1-2 只选白/绿；danger 3-4 选绿/蓝；danger 5-6 选蓝；"
            "danger 7-8 选蓝/紫；danger 9-10 选紫。禁止给低危怪配高级材料或反过来。"
            + ("各材料品级：" + "、".join(
                f"{nm}({'common' if r=='common' else 'uncommon' if r=='uncommon' else 'rare' if r=='rare' else 'epic' if r=='epic' else 'legendary' if r=='legendary' else 'mythic'})"
                for nm, r in sorted((mat_rarities or {}).items(), key=lambda x: x[1])
                if nm in (mat_names or []))
               if mat_rarities else ""),
            "请严格按 schema 输出纯 JSON。",
        ]
        return "\n\n".join(ctx + parts)

    def _build_npcs_user_message(self, form: dict, preset: WorldSimPreset, ctx: list,
                                 item_names: list, talent_names: list,
                                 loc_names: list, fac_names: list,
                                 worldbook: Optional[list] = None) -> str:
        # [P5b] NPC 数量指引：从规模档位结构化数字取 npcs（不再靠自由文本 hint 猜）
        scale_id = form.get("scale", preset.default_world_scale) or preset.default_world_scale
        c = _resolve_scale_counts(scale_id, preset)
        npc_n = int((c or {}).get("npcs") or 5)
        parts = [
            f"【NPC 数量指引】本世界约 {npc_n} 个 NPC（覆盖守卫/首领/手艺人/平民等角色；"
            "[!] 买卖全由各地点场所自营游商负责，不许生成开店经商的 NPC）。",
            "【地点名清单】" + ("、".join(loc_names) if loc_names else "（空）"),
            "【势力名清单】" + ("、".join(fac_names) if fac_names else "（空）"),
            # [用户指示 2026-09-06] 附稀有度标注：>epic 标「禁随身」，配合 NPC 初始携带封顶规则
            "【物品名清单（inventory 引用；带[禁随身]标记的橙/红品不得放进 NPC 初始 inventory）】"
            + ("、".join(item_names) if item_names else "（空）"),
            "【天赋名清单（talent_names 引用）】" + ("、".join(talent_names) if talent_names else "（空）"),
        ]
        # [P1 修 2026-10-01] 世界书人物名单硬约束（防同角色不同名）
        directive = _worldbook_npc_directive(worldbook)
        if directive:
            parts.append(directive)
        parts.append("请严格按 schema 输出纯 JSON。")
        return "\n\n".join(ctx + parts)

    def _build_world_user_message(self, form: dict, preset: WorldSimPreset, ctx: list) -> str:
        from src.models.world_sim_preset import (
            BUILTIN_ATTRIBUTES_TEMPLATES, DEFAULT_ATTRIBUTE_TEMPLATE_ID,
        )
        tmpl_lines = []
        for t in BUILTIN_ATTRIBUTES_TEMPLATES:
            tmpl_lines.append(
                f"  - {t['id']}（{t['label']}）: "
                f"属性={list(t['stat_display_names'].values())}, "
                f"主手槽={t['slot_display_names']['main_hand']}"
            )
        for t in (preset.attribute_templates or []):
            if isinstance(t, dict) and t.get("id"):
                tmpl_lines.append(
                    f"  - {t['id']}（{t.get('label', t['id'])}）: "
                    f"属性={list(t.get('stat_display_names', {}).values())}, "
                    f"主手槽={t.get('slot_display_names', {}).get('main_hand', '?')}"
                )
        parts = [
            "【属性/装备模板库】（attribute_template_id 从下面选一个最贴题材的 id，默认 "
            + DEFAULT_ATTRIBUTE_TEMPLATE_ID + "）\n" + "\n".join(tmpl_lines),
            "请严格按 schema 输出纯 JSON。",
        ]
        return "\n\n".join(ctx + parts)

    @staticmethod
    def _compute_obtainable_names(items: list, mon_raw: Optional[dict],
                                  world_raw: Optional[dict]) -> list:
        """[修 2026-10-01] 计算有获取渠道的物品名列表（任务 collect/deliver 目标用）。

        gen 阶段从 skeleton 数据推导；拓展阶段从实际 world 状态推导（另一入口）。
        渠道 = 怪物掉落 + 资源点产出 + 配方产出（商店为补充渠道，不在此列——
        商店备货在任务之后，且 ensure_quest_material_sources 兜底会保底上架）。
        """
        names = set()
        # 怪物掉落
        if isinstance(mon_raw, dict):
            for m in (mon_raw.get("monsters") or []):
                if not isinstance(m, dict):
                    continue
                for loot in (m.get("loot") or []):
                    nm = str(loot).strip() if isinstance(loot, str) else str(
                        loot.get("name", "") if isinstance(loot, dict) else "").strip()
                    if nm:
                        names.add(nm)
        # 资源点产出（从 world_raw 的 resource 类型推——gen 阶段 drops 尚未填，
        # 但 _init_resource_nodes 按 tier 从材料池确定性选，同 tier 材料全部算可获取）
        if isinstance(world_raw, dict):
            for loc in (world_raw.get("locations") or []):
                if not isinstance(loc, dict):
                    continue
                if str(loc.get("kind", "")) != "wilderness":
                    continue
                res = loc.get("resource")
                if isinstance(res, dict):
                    rt = str(res.get("type", "")).strip()
                    if rt:
                        names.add(rt)  # gather 目标按 type 匹配
        # 配方产出（gen 阶段配方尚未生成，但 _init_recipes 兜底会为
        # 「尚无配方的装备/消耗品」建配方 + material 也覆盖——四类全纳入宁多勿漏：
        # weapon/armor/consumable/material 均为可合成渠道可达）
        for it in (items or []):
            if isinstance(it, dict) and str(it.get("type") or "") in (
                    "material", "weapon", "armor", "consumable"):
                nm = str(it.get("name", "")).strip()
                if nm:
                    names.add(nm)
        return sorted(names)

    def _build_recipes_quests_user_message(self, form: dict, preset: WorldSimPreset, ctx: list,
                                           item_names: list, npc_refs: list,
                                           loc_names: list, mon_names: list,
                                           currency_name: str = "金币",
                                           res_types: Optional[list] = None,
                                           obtainable_names: Optional[list] = None) -> str:
        rc = self._effective_recipe_target(preset)
        parts = [
            f"【配方数量】recipes 生成 {rc} 个（覆盖各资源层级）。",
            f"【题材货币】本世界的货币是「{currency_name}」，任务 reward 文案里的货币一律写「{currency_name}」，不要写金币/灵石/信用点等别的货币。",
            "【物品名清单（inputs/output 引用；配方可用全部物品）】" + ("、".join(item_names) if item_names else "（空）"),
            # [修 2026-10-01] 任务 collect/deliver_items 目标只从有获取渠道的物品中选
            # （怪物掉落/资源点可采/配方产出——防「任务要的东西全世界拿不到」）
            "【可获取物品（任务的 collect / deliver_items 目标只能从中选）】"
            + ("、".join(obtainable_names) if obtainable_names else "（空——不要生成含 collect/deliver_items 目标的任务）"),
            "【NPC temp_id 清单（giver 引用）】" + ("、".join(npc_refs) if npc_refs else "（空）"),
            "【地点名清单（visit 目标引用；objective/desc 不得发明清单外的地名，如「某港浅滩」）】"
            + ("、".join(loc_names) if loc_names else "（空）"),
            "【怪物名清单（kill 目标引用，target 必填不得留空）】" + ("、".join(mon_names) if mon_names else "（空）"),
        ]
        # [环环相扣 2026-09-13] 注入资源点类型表：gather 进度按节点 type 匹配（不是物品名），
        # 任务 LLM 此前看不到类型表，拿物品名当 gather 目标 -> 永不命中（鲨鱼皮书）。
        if res_types:
            parts.append("【资源点类型清单（gather 的 target 只能从中选，逐字照抄）】"
                         + "、".join(res_types))
        else:
            parts.append("【资源点类型清单】本世界暂无野外资源点——不要生成含 gather 目标的任务。")
        parts.append("请严格按 schema 输出纯 JSON。")
        return "\n\n".join(ctx + parts)

    def _call_and_parse(self, llm, messages, cancel_check) -> tuple[Optional[dict], str]:
        """调一次 LLM 并解析 JSON，返回 (dict|None, error)。"""
        result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
        if result.cancelled:
            return None, "已取消"
        if result.error:
            return None, result.error
        if not result.content or not result.content.strip():
            return None, "LLM 返回空内容"
        sk = _extract_json(result.content)
        if sk is None:
            return None, "无法解析为 JSON"
        # schema 校验：补缺顶层键
        for k in _SK_REQUIRED_KEYS:
            if k not in sk:
                sk[k] = []
        if not isinstance(sk.get("worldbook"), list):
            sk["worldbook"] = []
        for k in ("factions", "locations", "npcs", "items"):
            if not isinstance(sk.get(k), list):
                sk[k] = []
        if not isinstance(sk.get("quests"), list):
            sk["quests"] = []
        if not isinstance(sk.get("player_start"), dict):
            sk["player_start"] = {}
        return sk, ""

    # ============ 引擎层：骨架 -> World（分配 uuid + 关联）============
    def build_world_from_skeleton(self, sk: dict, form: dict, preset: WorldSimPreset) -> World:
        """把骨架 dict 补全为 World 实体：分配 uuid、关联 location/npc/faction、拷贝 config_overlay。
        纯结构化操作，不调 LLM。v1 不生成 CRPG 数值（Phase 3）。"""
        # 势力：name -> Faction
        factions: list[Faction] = []
        faction_by_name: dict[str, Faction] = {}
        for f in sk.get("factions", []):
            if not isinstance(f, dict):
                continue
            fac = Faction(
                name=(f.get("name") or "未命名势力").strip(),
                desc=f.get("desc", "") or "",
                ideology=f.get("ideology", "") or "",
                leader_traits=f.get("leader_traits", "") or "",
            )
            factions.append(fac)
            faction_by_name[fac.name] = fac
        # [P 验收] 解析 factions.relations（LLM 显式两两关系；名 -> id，白名单 + 钳制）。
        # 世界观里的「合作/同盟/互为表里」信息由此落地为数据，滴答势力战据此只打敌对、
        # 不再凭空给所有势力对造负关系（守 §22 世界校准/势力战口径）。
        for f in sk.get("factions", []):
            if not isinstance(f, dict):
                continue
            fac = faction_by_name.get((f.get("name") or "").strip())
            if fac is None:
                continue
            for r in (f.get("relations") or []):
                if not isinstance(r, dict):
                    continue
                tgt = str(r.get("target") or "").strip()
                tgt_fac = faction_by_name.get(tgt)
                if tgt_fac is None or tgt_fac.id == fac.id:
                    continue
                try:
                    val = max(-100, min(100, int(r.get("value") or 0)))
                except (TypeError, ValueError):
                    continue
                fac.relations[tgt_fac.id] = val
                # [P 验收] 镜像到反向（仅当反向未显式声明）：tick_faction_war 取双边平均，
                # 单向 -80 会被腰斩成 -40、弱于引擎默认 -50；镜像后保持 LLM 显式敌意不被稀释。
                tgt_fac.relations.setdefault(fac.id, val)

        # NPC：temp_id -> uuid，name -> NPC；暂存 location/faction 名字待解析
        npc_by_temp: dict[str, NPC] = {}
        npc_raw: list[tuple[NPC, dict]] = []  # (npc, raw) 待解析 location/faction
        _seen_npc_names: set[str] = set()     # [修 2026-09-10] 重名去重（守按名解析唯一性）
        _dup_temp_ids: set[str] = set()       # [修 2026-09-10] 骨架里重复过的 temp_id（歧义）
        # [NPC 初始等级 2026-09-12 用户指示] 要角初始等级上限：世界成长天花板 100 级，但开局
        # 就 90+ 级让玩家的成长与追赶都失去意义。要角初始等级 = 1..npc_max_level 随机（默认
        # 1-30），不再看所在地 danger；[P45 2026-09-12] 之后靠狩猎成长（打过怪的才涨经验，
        # 见 npc_life_engine.do_hunt/daily_passive_encounters），原每日 8% 被动升级已删。
        _npc_cap = max(1, min(100, int(getattr(preset, "npc_max_level", 30) or 0)))
        for n in sk.get("npcs", []):
            if not isinstance(n, dict):
                continue
            npc = NPC(
                name=(n.get("name") or "未命名NPC").strip(),
                role=n.get("role", "") or "",
                personality=n.get("personality", "") or "",
                goal=n.get("goal", "") or "",
                appearance=n.get("appearance", "") or "",
                desc=n.get("desc", "") or "",
                # [P9] 元素构成（白名单清洗 + 最多 2 个；LLM 未给时 _init_combat_stats 从技能推断）
                elements=_clean_elements(n.get("elements")),
            )
            # [P 验收] combat_role 结构化标注 -> level/hostile 语义直解（LLM 出语义、引擎算数值，
            # 不再靠 role 文本关键词猜测——开放措辞永远猜不全）。[P44 2026-08-23] 关键词兜底路
            # 已删除：combat_role 缺失/非法一律按平民处理（见 _init_combat_stats）。
            cr = str(n.get("combat_role") or "").strip()
            if cr in ("boss", "hostile", "friendly", "none"):
                npc.combat_role = cr
                # [NPC 初始等级 2026-09-12 用户指示] 要角初始等级 = 1..npc_max_level 随机
                # （默认 1-30），**不再看所在地 danger**——世界成长天花板 100 级，开局就 90+
                # 级让玩家的成长与追赶都失去意义；[P45] 之后靠狩猎成长（8% 被动升级已删）。
                # 种子锚 npc.id（同一次生成内确定，不依赖尚未构建的 World）。
                _lrng = SeededRng.seed_from(str(getattr(npc, "id", "") or ""), 0,
                                            "npc_init_level")
                if cr == "none":
                    npc.level = 0      # 平民 -> _init_combat_stats 抬基线 1
                    npc.hostile = False
                else:
                    npc.level = int(_lrng.roll(1, max(1, _npc_cap)))
                    npc.hostile = cr in ("boss", "hostile")
            npc.name = _dedup_npc_name(npc.name, _seen_npc_names)
            tid = n.get("temp_id") or ""
            if tid:
                if tid in npc_by_temp:
                    # [修 2026-09-10] 重复 temp_id 不再静默覆盖（覆盖会让 giver="n3" 指到
                    # 另一个 NPC = 「发布人错位」）。记入歧义集：giver 引用它时不猜。
                    _dup_temp_ids.add(tid)
                else:
                    npc_by_temp[tid] = npc
            npc_raw.append((npc, n))

        # 地点：name -> Location；暂存 connections(名字)/npcs(temp_id) 待解析
        locations: list[Location] = []
        loc_by_name: dict[str, Location] = {}
        loc_raw: list[tuple[Location, dict]] = []
        # [P12] LLM 给的野外资源定义（loc.id -> resource dict），_init_resource_nodes 用
        pending_resources: dict[str, dict] = {}
        for l in sk.get("locations", []):
            if not isinstance(l, dict):
                continue
            kind = str(l.get("kind") or "").strip()
            # [P25a] "dungeon" 生成期一般不出（秘境内部地点由 dungeon_engine 建），白名单保口径一致
            if kind not in ("settlement", "wilderness", "dungeon"):
                kind = "wilderness"
            # [P34e] 聚落规模白名单回退（仅 settlement 有意义；wilderness/dungeon 留空）
            ss = str(l.get("settlement_size") or "").strip()
            if ss not in ("city", "town", "village"):
                ss = ""
            if kind != "settlement":
                ss = ""
            loc = Location(
                name=(l.get("name") or "未命名地点").strip(),
                desc=l.get("desc", "") or "",
                region=l.get("region", "") or "",
                danger=_safe_int(l.get("danger", 1), 1, 1, 10),
                kind=kind,
                settlement_size=ss,
            )
            fname = (l.get("faction") or "").strip()
            if fname and fname in faction_by_name:
                loc.faction_id = faction_by_name[fname].id
            # [P12] 野外点的 LLM 资源定义（tier/name/type/desc）；聚落忽略（不挂资源）
            if kind == "wilderness" and isinstance(l.get("resource"), dict) and l["resource"].get("name"):
                pending_resources[loc.id] = dict(l["resource"])
            locations.append(loc)
            loc_by_name[loc.name] = loc
            loc_raw.append((loc, l))

        # 回填 NPC 的 location_id / faction_id
        for npc, n in npc_raw:
            lname = (n.get("location") or "").strip()
            if lname and lname in loc_by_name:
                npc.location_id = loc_by_name[lname].id
            fname = (n.get("faction") or "").strip()
            if fname and fname in faction_by_name:
                npc.faction_id = faction_by_name[fname].id

        # 回填地点的 connections（名字 -> id）与 npc_ids（收集属于此地点的 NPC）
        for loc, l in loc_raw:
            conns = l.get("connections") or []
            for cn in conns:
                cn = (cn or "").strip()
                # 跳过自引用（LLM 可能把地点自身列入 connections）
                if cn and cn in loc_by_name and loc_by_name[cn].id != loc.id \
                        and loc_by_name[cn].id not in loc.connections:
                    loc.connections.append(loc_by_name[cn].id)
                    # [P10c] 补反向连接：LLM 常给单向 connections（A->B 不带 B->A），
                    # 不补反向会导致地图不可往返——玩家从 A 走到 B 后回不去 A
                    # （复活/移动后卡死在单向终点）。connections 应双向是地图模型不变量。
                    if loc.id not in loc_by_name[cn].connections:
                        loc_by_name[cn].connections.append(loc.id)
            for npc, _ in npc_raw:
                if npc.location_id == loc.id and npc.id not in loc.npc_ids:
                    loc.npc_ids.append(npc.id)

        # [P7f] 地图自动布局：给地点分配网格坐标（BFS 连通布局 + 孤岛补边，保证连通图）
        self._layout_locations(locations)

        # 物品（from_dict 自带 type/rarity 枚举白名单校验）
        items: list[Item] = [Item.from_dict(i) for i in sk.get("items", []) if isinstance(i, dict)]
        # [修 2026-09-14] 功能性物品收编前置：种子确定性 id 重写（fe.seed_id_of）必须
        # 发生在一切名字 -> id 解析之前——下方 item_by_name 供配方 inputs/output、怪物
        # loot、NPC inventory、任务奖励四个解析点共用，先解析后重写会让旧 uuid 悬空
        # （真机档「海藻糊糊」等三条药方永缺一件料）。题材 id 依赖 attribute_template_id，
        # 其解析同步前置（player_start 段直接复用，不再重复解析）。
        self._pending_template_id = _resolve_template_id(
            str((sk.get("player_start") or {}).get("attribute_template_id", "") or "").strip(),
            preset)
        _fn_minimums = self._normalize_functional_items(
            items, str(self._pending_template_id or "western_fantasy"))
        item_by_name = {it.name: it for it in items}

        # [P12] LLM 配方（inputs/output 名字 -> item id；引用失败跳过；数量不足由 _init_recipes 兜底补齐）
        recipes: list[Recipe] = []
        for r in (sk.get("recipes") or []):
            if not isinstance(r, dict):
                continue
            out_it = _name_lookup(item_by_name, r.get("output"))
            in_ids: list[str] = []
            for nm in (r.get("inputs") or []):
                nm = str(nm or "").strip()
                it2 = _name_lookup(item_by_name, nm)
                if it2 is not None and (out_it is None or it2.id != out_it.id):
                    in_ids.append(it2.id)
            if out_it is None or not in_ids:
                continue
            # [住宅网格 2026-08-24] LLM 配方同样按产出 type 门控建筑（forge/alchemy），
            # min_building_level 随产出品级升（与 _init_recipes._gate 同口径）。
            _t = getattr(out_it, "type", "")
            _rb = "forge" if _t in ("weapon", "armor") else ("alchemy" if _t == "consumable" else "")
            _ml = {"common": 1, "uncommon": 1, "rare": 2, "epic": 3,
                   "legendary": 4, "mythic": 5}.get(getattr(out_it, "rarity", "common"), 1) if _rb else 0
            recipes.append(Recipe(
                name=(str(r.get("name") or "").strip() or f"炼制{out_it.name}"),
                desc=str(r.get("desc") or "") or "",
                inputs=in_ids[:4], output_item_id=out_it.id, station="",
                difficulty=_safe_int(r.get("difficulty"), 0, 0, 50),
                required_building=_rb, min_building_level=_ml, output_level=_ml))

        # [P12] LLM 怪物池（loot 名字 -> item id；等级/属性由野外引擎按地点+玩家等级动态生成）
        monster_pool: list[dict] = []
        for m in (sk.get("monsters") or []):
            if not isinstance(m, dict) or not str(m.get("name") or "").strip():
                continue
            dmin = _safe_int(m.get("danger_min"), 1, 1, 10)
            dmax = _safe_int(m.get("danger_max"), 10, 1, 10)
            if dmax < dmin:
                dmax = dmin
            loot_ids = []
            for nm in (m.get("loot") or []):
                nm = str(nm or "").strip()
                it3 = _name_lookup(item_by_name, nm)
                if it3 is not None and it3.id not in loot_ids:
                    # [修 2026-10-01] 品级锚定：怪物掉落的材料品级须与 danger 档匹配
                    # （danger 1-2→白/绿, 3-4→绿/蓝, 5-6→蓝, 7-8→蓝/紫, 9-10→紫）。
                    # LLM 提示词已加品级标注，此处为引擎兜底——LLM 仍给 danger 1 野兔
                    # 配了橙色材料时，换成同池（material）内品级匹配的替代品。
                    _r = str(getattr(it3, "rarity", "common") or "common")
                    _allowed = _MONSTER_LOOT_RARITY.get((dmin + dmax) // 2, _ALL_RARITIES)
                    if _r not in _allowed and getattr(it3, "type", "") == "material":
                        # [!] 此处在 skeleton 解析阶段，world 尚未创建——用 item_by_name
                        _pool = [i for i in item_by_name.values()
                                 if getattr(i, "type", "") == "material"
                                 and str(getattr(i, "rarity", "common")) in _allowed]
                        if _pool:
                            it3 = min(_pool, key=lambda i: str(i.id))
                    loot_ids.append(it3.id)
            monster_pool.append({
                "name": str(m.get("name")).strip(),
                "role": str(m.get("role") or "魔物").strip(),
                "desc": str(m.get("desc") or "") or "",
                "danger_min": dmin, "danger_max": dmax, "loot": loot_ids,
                # [P9] 元素构成（白名单清洗；野外引擎建怪时带上，参与克制结算）
                "elements": _clean_elements(m.get("elements")),
            })

        # [P12] NPC 随身背包（LLM inventory 名字 -> 已有 item id，最多 3 件；送礼/搜刮有货可依）
        for npc, n in npc_raw:
            inv_raw = n.get("inventory") or []
            if not isinstance(inv_raw, list):
                continue
            for nm in inv_raw:
                nm = str(nm or "").strip()
                it4 = _name_lookup(item_by_name, nm)
                if it4 is not None and not _npc_start_item_ok(it4):
                    # [用户指示 2026-09-06] 初始携带封顶紫色：>epic 换同类 ≤epic 替代品
                    # （同 type 中阶次最高的合法件，确定性不引入 rng），无替代则不带。
                    _alts = sorted((c for c in item_by_name.values()
                                    if c.type == it4.type and _npc_start_item_ok(c)),
                                   key=lambda c: (-_RARITY_RANK.get(c.rarity, 0), c.id))
                    it4 = _alts[0] if _alts else None
                if it4 is not None and it4.id not in npc.inventory and len(npc.inventory) < 3:
                    npc.inventory.append(it4.id)

        # [P15b1] 任务体系：LLM quests 数组（主线 2-3 段链 + 支线 1-2 条）。
        # objectives/rewards 白名单清洗（同旧 opening_quest 口径）；rewards.items 是物品名 -> item id。
        # 链接：chain=main 按数组顺序 prev.chain_next_id = next.id，第 2 段起初始 locked
        # （前一环领奖时解锁为 available，见 quest_engine.claim_reward）。
        quests: list[Quest] = []
        main_chain: list[Quest] = []
        raw_quests = sk.get("quests") or []
        if not isinstance(raw_quests, list):
            raw_quests = []
        for rq in raw_quests:
            if not isinstance(rq, dict) or not str(rq.get("title", "") or "").strip():
                continue
            giver_raw = str(rq.get("giver", "") or "").strip()
            # [修 2026-09-10] 走容错解析（temp_id 优先；歧义 id 不猜；回退按名解析，守 [P44]）
            giver_id = _resolve_quest_giver(giver_raw, npc_by_temp, _dup_temp_ids)
            objectives = []
            raw_objs = rq.get("objectives") or []
            if not isinstance(raw_objs, list):
                raw_objs = []
            for ro in raw_objs:
                if isinstance(ro, dict) and ro.get("type") in (
                        "kill", "gather", "talk", "visit", "collect",
                        "investigate", "escort"):   # [S04 余项] 复合目标（target+target2）
                    tgt = _resolve_temp_ids(str(ro.get("target", "") or ""), npc_by_temp)
                    # [修 2026-08-28] talk/kill 目标锚定真实 NPC 名：LLM 常把 objective
                    # 目标写成名单外虚构名（夜城档主线「与马库斯·雷恩交谈」而世界无此人
                    # -> 任务永久卡死无法完成）。[P44] 容错解析锚真实名单；解析不到的
                    # talk 目标退 giver（发布者本就是任务剧情锚点，退给 giver 保可完成）；
                    # kill 目标解析不到则清空 target（空 target 匹配任意该 type 事件，
                    # 杀任意敌对即推进——宁可宽松不可卡死）。gather/visit/collect 的
                    # target 是资源类型/地点名/物品名，不在 NPC 名单解析范围。
                    if ro.get("type") in ("talk", "kill") and tgt:
                        from src.services import name_resolver as _nrs
                        hit = _nrs.resolve_name(tgt, [n.name for n in npc_by_temp.values()])
                        if hit:
                            tgt = hit
                        elif ro.get("type") == "talk":
                            # [修 2026-09-10] 兜底按已解析的 giver_id 反查（与
                            # _resolve_quest_giver 同源）：原 npc_by_temp.get(giver_raw) 对
                            # 「giver 填真实名」解析不到，对歧义 temp_id 却锚第一个重名 NPC，
                            # 与「歧义不猜」口径不一致。
                            _giver = next((x for x in npc_by_temp.values() if x.id == giver_id), None)
                            tgt = _giver.name if _giver is not None else ""
                        else:
                            # [修 2026-10-02 审查] kill 虚构名兜底（复原被误删的旧口径）：
                            # NPC 名不中再锚本批怪物池名（'野狼' 这类真怪物名原样保留），
                            # 仍不中清空——空 target 匹配任意击杀事件，宁可宽松不可卡死。
                            tgt = _nrs.resolve_name(
                                tgt, [str(m.get("name") or "") for m in monster_pool
                                      if isinstance(m, dict) and m.get("name")]) or ""
                    # [S04 余项] investigate/escort 的 target2=地点名（容错锚真实地点；
                    # 解析不到清空——地点目标空串会匹配任意到访，宁缺勿错）
                    tgt2 = ""
                    if ro.get("type") in ("investigate", "escort"):
                        _raw2 = _resolve_temp_ids(str(ro.get("target2", "") or ""), npc_by_temp)
                        _loc_names = list(loc_by_name.keys())
                        from src.services import name_resolver as _nrs2
                        tgt2 = _nrs2.resolve_name(_raw2, _loc_names) or ""
                    objectives.append({
                        "type": ro.get("type"),
                        "target": tgt,
                        "target2": tgt2,
                        "count": _safe_int(ro.get("count"),
                                           2 if ro.get("type") == "investigate" else 1, 1, 999),
                        "current": 0,
                        "desc": _resolve_temp_ids(str(ro.get("desc", "") or ""), npc_by_temp),
                    })
            if not objectives:
                # 兜底：给一个 talk 目标（无结构化时至少能接取/领奖）
                objectives = [{"type": "talk", "target": "", "count": 1, "current": 0,
                               "desc": "推进任务（与相关角色交谈/达成目标）"}]
            raw_rw = rq.get("rewards") or {}
            rw_items = []
            if isinstance(raw_rw, dict):
                for nm in (raw_rw.get("items") or []):
                    it5 = _name_lookup(item_by_name, nm)
                    # [任务奖励物品 2026-09-13] LLM 点名的奖励也过同一把闸（紫以下/非钥匙/
                    # 非培养道具/非技能书/品阶不超）——否则「橙红只从掉落锻造拍卖出」在
                    # 量最大的 LLM 主支线通道上形同虚设。找不到/不合规一律丢弃（既有风格）。
                    if it5 is not None and it5.id not in rw_items \
                            and qe.reward_item_allowed(it5, allow_skillbook=True):
                        rw_items.append(it5.id)
            rewards = {
                "items": rw_items,
                "gold": max(0, _safe_int(raw_rw.get("gold") if isinstance(raw_rw, dict) else 50, 50, 0, 10 ** 6)),
                "xp": max(0, _safe_int(raw_rw.get("xp") if isinstance(raw_rw, dict) else 30, 30, 0, 10 ** 6)),
            }
            q = Quest(
                title=str(rq.get("title", "") or "").strip(),
                objective=_resolve_temp_ids(str(rq.get("objective", "") or ""), npc_by_temp),
                giver_npc_id=giver_id,
                reward_text=str(rq.get("reward", "") or ""),
                status="available",
                objectives=objectives,
                rewards=rewards,
                chain="main" if str(rq.get("chain", "") or "").strip() == "main" else "side",
            )
            quests.append(q)
            if str(rq.get("chain", "") or "").strip() == "main":
                main_chain.append(q)
        for prev_q, next_q in zip(main_chain, main_chain[1:]):
            prev_q.chain_next_id = next_q.id
            next_q.status = "locked"
        # [审查修] 主线段稳定置前：world.quests[0]（主线任务块/开局保底技能书）假设首条是
        # 主线第一环；LLM 若把支线排前面，这里保序重排兜底（main 在前，相对顺序不变）。
        if main_chain and quests[0] is not main_chain[0]:
            quests.sort(key=lambda x: x not in main_chain)

        # 玩家起始
        ps = sk.get("player_start") or {}
        player = PlayerState(
            background=ps.get("background", "") or "",
            class_name=ps.get("suggested_class", "") or "",
            # [P12] LLM 出的开局技能（白名单钳制；为空时 _init_combat_stats 按职业兜底）
            skills=self._sanitize_player_skills(ps.get("skills")),
        )
        # [P13] 可学习技能池（白名单钳制 + rarity；空则 _init_skill_pool 题材兜底）
        skill_pool = self._sanitize_pool_skills(ps.get("skill_pool"))
        ploc = (ps.get("location") or "").strip()
        if ploc and ploc in loc_by_name:
            player.location_id = loc_by_name[ploc].id
        elif locations:
            player.location_id = locations[0].id
        # [!] 出生地必须标已发现+已探索：不标的话地图上出生地永远显示"已发现(暗格)"
        # 且周围不生成迷雾块——玩家移动后回看地图"还是初始方块"。玩家所在即已探索。
        start_loc = next((l for l in locations if l.id == player.location_id), None)
        if start_loc is not None:
            start_loc.discovered = True
            start_loc.explored = True
        # [P10c] 记录出生地 id：永久死亡关闭时玩家被击败 -> 复活送回此处（HP=1）。
        player.spawn_location_id = player.location_id
        # [P5c] attribute_template_id 已于物品收编前置处解析（种子 id 重写需要题材 id，
        # 见 items 段注释），此处不再重复解析。显示名（stat_display_names/
        # slot_display_names）落入 config_overlay 供 UI 渲染。
        # [P9] 暂存 LLM 生成的天赋池（player_start.talent_pool），_init_talent_pool 用；缺失走兜底
        raw_tp = ps.get("talent_pool")
        self._pending_talent_pool = raw_tp if isinstance(raw_tp, list) else None

        # ---- [P27] 二层地图：解析/兜底生成每个地点的场所 + 设 default_place_id ----
        # [!] places_enabled=False 跳过（退化单层）；dungeon 内部地点不场所化（_build 内 gate）。
        # genre_id 取已解析的题材模板 id（兜底西幻）。规模档可覆盖每地点场所数。
        places_enabled = bool(getattr(preset, "places_enabled", True))
        ppl = max(0, min(10, int(getattr(preset, "places_per_location", 4) or 0)))
        genre_id_places = str(self._pending_template_id or "western_fantasy") or "western_fantasy"
        for loc, l in loc_raw:
            places, default_pid = _build_places_for_location(
                loc, l, genre_id_places, places_enabled, ppl)
            loc.places = places
            loc.default_place_id = default_pid
        # NPC 初始 place_id：先按角色匹配场所（[P27] 防郎中落在铁匠铺），匹配不到回退默认场所。
        for npc, _ in npc_raw:
            if not npc.location_id:
                continue
            host = next((ll for ll in locations if ll.id == npc.location_id), None)
            if host is None or not host.default_place_id:
                continue
            npc.place_id = _match_npc_place(npc, host) or host.default_place_id
        # [P27] 玩家初始 place_id = 出生地 default_place_id（start_loc.default_place_id 已于上一步就绪）
        if start_loc is not None and start_loc.default_place_id:
            player.place_id = start_loc.default_place_id

        # 世界书词条
        lore: list[WorldLore] = []
        for w in sk.get("worldbook", []):
            if not isinstance(w, dict):
                continue
            lore.append(WorldLore(
                key=w.get("key", "") or "",
                content=w.get("content", "") or "",
                priority=_safe_int(w.get("priority", 100), 100),
            ))

        # 世界名：优先用 LLM 生成的 world_name（gen schema 题材化卡名），否则回退 premise 前 12 字
        premise = (form.get("premise") or "").strip()
        llm_name = str(sk.get("world_name") or "").strip()
        if llm_name:
            world_name = llm_name[:12]
        else:
            world_name = premise[:12] + ("…" if len(premise) > 12 else "") if premise else "新世界"

        # 拷贝 preset 的模拟/生图旋钮到 config_overlay（每世界可单独调）
        config_overlay = {
            "sim_enabled": preset.sim_enabled,
            "economy_sim": preset.economy_sim,
            "faction_war": preset.faction_war,
            "offscreen_npc_tick": preset.offscreen_npc_tick,
            "reconcile_interval": preset.reconcile_interval,
            "sim_budget_per_tick": preset.sim_budget_per_tick,
            "combat_system": preset.combat_system,
            "crpg_granularity": preset.crpg_granularity,
            "difficulty": preset.difficulty,
            # ---- P4 细分旋钮（每世界可覆盖）----
            # [!] 滴答 LLM 4 项（sim_api_id/sim_temperature/sim_max_tokens/sim_system_prompt）
            # 故意不进 overlay，走全局 preset（滴答 LLM 是全局模型选择，无需逐世界调）。
            "economy_volatility": preset.economy_volatility,
            "faction_war_lethality": preset.faction_war_lethality,
            "event_log_max": preset.event_log_max,
            "max_events_per_tick": preset.max_events_per_tick,
            "key_npc_decision_enabled": preset.key_npc_decision_enabled,
            "key_npc_budget": preset.key_npc_budget,
            # ---- [P10] 同场景 NPC 互动 + 好友 + 助战旋钮（每世界可覆盖）----
            "npc_reactions_enabled": preset.npc_reactions_enabled,
            "npc_gift_chance": preset.npc_gift_chance,
            "friend_affinity_threshold": preset.friend_affinity_threshold,
            "friend_chat_enabled": preset.friend_chat_enabled,
            "combat_allies_enabled": preset.combat_allies_enabled,
            "combat_max_allies": preset.combat_max_allies,
            "combat_encounter_judge_llm": preset.combat_encounter_judge_llm,
            "combat_max_enemies": preset.combat_max_enemies,
            # ---- [P10b] 同行旋钮（每世界可覆盖）----
            "companions_enabled": preset.companions_enabled,
            "companion_affinity_threshold": preset.companion_affinity_threshold,
            "companion_max_followers": preset.companion_max_followers,
            # ---- [P11c] 拓展物品数（每世界可覆盖）+ 任务开关（quest_engine 读 overlay 门控）----
            "map_expansion_item_count": preset.map_expansion_item_count,
            "quest_system_enabled": preset.quest_system_enabled,
            # ---- [P12] 野外怪物 + NPC 自主生活（每世界可覆盖）----
            "wilderness_monsters_enabled": preset.wilderness_monsters_enabled,
            "wilderness_monster_chance": preset.wilderness_monster_chance,
            "elite_monster_chance": preset.elite_monster_chance,
            "npc_autonomous_enabled": preset.npc_autonomous_enabled,
            # ---- [P34e] 探查/地图拓展三概率（每世界可覆盖）----
            "expand_new_city_chance": preset.expand_new_city_chance,
            "expand_new_npc_chance": preset.expand_new_npc_chance,
            "expand_new_faction_chance": preset.expand_new_faction_chance,
            "expand_new_quest_chance": preset.expand_new_quest_chance,
            # ---- [P34f] 交易所股市（每世界可覆盖）----
            "stock_market_enabled": preset.stock_market_enabled,
            "stock_price_drift_pct": preset.stock_price_drift_pct,
            # ---- [P34g] 拍卖会（每世界可覆盖）----
            "auction_enabled": preset.auction_enabled,
            "auction_interval_days": preset.auction_interval_days,
            "auction_duration_days": preset.auction_duration_days,
            "auction_lot_count": preset.auction_lot_count,
            # ---- [P14] 经济节奏（每世界可覆盖）----
            "economy_pace": preset.economy_pace,
            # [季节玩法化 2026-09-06] 引擎侧读 overlay 判定（缺键=关，老世界不受影响）
            "season_fx": bool(preset.season_effects_enabled),
            # [物价联动 2026-09-10] 本地势力财富定价开关（tre.wealth_price_factors 读
            # "wealth_price"，缺键=关护老世界；任务/委托/势力战结算喂 wealth）
            "wealth_price": bool(preset.faction_price_enabled),
            # [事件任务 2026-09-06] overlay 门控（缺键关——quest_engine._evt_quests_on）
            "event_quests_enabled": bool(preset.event_quests_enabled),
        }
        # [P5c/P6] 题材化模板：落入 template_id + 全部题材化显示名（供 UI 渲染 + LLM 上下文用）。
        # [P6] 除 P5c 的 stat/slot 外，新增 currency/rarity/shop_type/item_type 四组题材化名，
        # 让货币单位（金币/灵石/信用点）、品级名（传说/神器）、商店类型名、物品大类名全部题材适配。
        tmpl = _get_template_by_id(getattr(self, "_pending_template_id", ""), preset)
        config_overlay["attribute_template_id"] = tmpl["id"]
        config_overlay["stat_display_names"] = dict(tmpl["stat_display_names"])
        config_overlay["slot_display_names"] = dict(tmpl["slot_display_names"])
        config_overlay["currency_name"] = tmpl["currency_name"]
        config_overlay["rarity_display_names"] = dict(tmpl["rarity_display_names"])
        config_overlay["shop_type_names"] = dict(tmpl["shop_type_names"])
        config_overlay["item_type_display_names"] = dict(tmpl["item_type_display_names"])
        # [P34d] 物品次级分类名（背包 QTabWidget 分桶 tab 标题用）
        config_overlay["item_category_display_names"] = dict(tmpl["item_category_display_names"])

        world = World(
            name=world_name,
            premise=premise,
            genre_tags=list(form.get("genre_tags") or []),
            tone=form.get("tone", "") or "",
            magic_level=int(form.get("magic_level", 0) or 0),
            tech_level=int(form.get("tech_level", 0) or 0),
            nsfw=bool(form.get("nsfw", False)),
            scale=_normalize_scale_id(
                form.get("scale", preset.default_world_scale) or preset.default_world_scale,
                preset,
            ),
            config_overlay=config_overlay,
            lore=lore,
            factions=factions,
            locations=locations,
            npcs=[n for n, _ in npc_raw],
            items=items,
            quests=quests,
            recipes=recipes,
            monster_pool=monster_pool,
            skill_pool=skill_pool,
            monster_skill_pool=self._sanitize_monster_skill_pool(sk.get("monster_skill_pool")),
            player=player,
        )
        # ---- [P7d2] 装备覆盖度兜底：确保每个装备槽都有装备可填（8 槽全覆盖）----
        # [!] 必须在 _init_combat_stats 之前跑：数值推算遍历 world.items 按 rarity
        # 乘算 attack/defense + 题材 stat_bonus，后补的装备拿不到数值（全 0 白装）。
        # [P12] LLM 野外资源定义先挂到实例（_init_resource_nodes 读取后自清）
        self._pending_loc_resources = pending_resources
        self._ensure_equipment_coverage(world, preset)
        # ---- P3 数值推算（LLM 骨架不含数值，引擎据 role/rarity/class 推算静态默认值）----
        # [!] 数值范式：这里是「静态定义值」的默认推算，属引擎职责；动态计算（伤害/暴击/掉落）
        # 在 apply_intent combat 分支用 combat_engine + SeededRng 纯 Python 跑，LLM 不参与。
        # [要角补档 2026-09-12] 剧情要角（is_key_npc）也要拿到 1..npc_max_level 的随机初始
        # 等级——他们大多是 combat_role=none 的平民（盟主/族长/任务发布人），按旧口径恒为 1 级。
        # [!] 必须早于 _init_combat_stats：等级定了战斗数值/HP/掉落池才跟着算。
        self._assign_key_npc_initial_level(world, preset)
        self._init_combat_stats(world, preset)
        # ---- P4 世界滴答状态初始化（势力/NPC/地点/玩家种子值，纯引擎推算无 LLM）----
        self._init_world_tick_state(world, preset)
        # ---- P6 商人标记 + 空商店骨架（纯结构无 LLM；货架货物由 generate_shops_goods LLM 备货，
        #      失败回退 _seed_shop_from_catalog 确定性选品，保证商店永不空架）----
        # [修 2026-09-13] 建店统一收口在 venue 解析之后（与拓展 _apply_expansion 同口径）：
        # venue 商场所由 _ensure_city_venues 落成 Place——旧顺序在解析前建店扫不到 venue，
        # town 的商店类 venue「有门面零店」（真机档铁锚镇 weapon/consumable/material 三门面
        # 0 店），city 非必需类 venue（material/consumable）同漏；_ensure_city_shops 又只保
        # city 必需三类。现顺序：先落场所 -> _ensure_city_shops 补 city 必需三类 ->
        # _init_merchants_and_shops 单次扫全部商店类场所建店（幂等 (loc, type) 去重）->
        # _ensure_shop_places 绑定收尾。凡有商店类场所必有一店。
        raw_by_loc = {loc.id: raw for loc, raw in loc_raw}
        for loc in world.locations:
            if getattr(loc, "kind", "") == "settlement":
                self._ensure_city_venues(world, loc, raw_by_loc.get(loc.id, {}), preset)
                self._ensure_city_shops(world, loc, preset)
        self._init_merchants_and_shops(world, preset)
        # [游商 2026-09-08] 建店收尾：_ensure_shop_places 把店绑定场所/店名对齐（幂等）。
        self._ensure_shop_places(world)
        # ---- [P25a] 初始秘境 1-2 个（挂野外地点；题材池确定性建，入口后两个渠道见
        # 地图拓展 _apply_expansion 与奇遇 ruins 原型）----
        self._init_dungeons(world)
        # ---- [P34f] 交易所股市初始化（题材池确定性初始化 commodity + base_price）----
        ske.init_stock_market(world)
        # ---- [方案A 2026-08-28] LLM 功能性物品收编已前置到物品解析处（见 items 段注释：
        # id 重写必须早于一切名字 -> id 解析）；三个引擎注入层直接用前置收编的计数补差额。----
        # ---- [P34a] 培养类/锻造/制造 reagent 物品入世（题材池确定性生成入 world.items）。
        # 必须在 _init_recipes 之前——reagent 是配方输入候选；在 generate_shops_goods 之前——
        # 备货过滤排除 cultivate reagent（商店不卖鉴定洗练道具，只能肝/掉/拍）。入世后自动
        # 成为奇遇/任务/秘境/Boss 奖励候选（_pick_reward_item 从 world.items 抽）。----
        self._init_reagents(world, preset, minimums=_fn_minimums)
        from src.services import dungeon_tools as _dt
        _dt.init_catalog(world)
        # ---- [宠物食品 2026-08-24] 兜底宠物食品入世（6 题材确定池，category=pet_food）。
        # 在 _init_reagents 之后、_init_seeds/_init_recipes 之前，使 pet_food 亦成为
        # 商店/掉落/奖励候选。定死不入 LLM（守数值范式）。----
        self._init_pet_food(world, preset, minimums=_fn_minimums)
        # ---- [P35] 种植系统入世：种子/作物物品 + 野外资源点种子掉落（题材池确定性生成）。
        # 在 _init_recipes 之前（作物=craft 材料可成炼丹输入候选，种子被 mats 过滤排除
        # 不当材料烧）、generate_shops_goods 之前（低 rarity 种子可自然上架 material/general
        # 店，守 shop_max_item_level 口径：epic+ 种子 level 4+ 不上商店，走掉落/拍卖）。----
        self._init_seeds(world, preset, minimums=_fn_minimums)
        # ---- [饱食度 2026-09-06 用户指示] 题材食物入世（hunger_enabled 才注入；在
        # _init_recipes 之前（食物可成炼制候选）+ 商店备货之前（可上架））。----
        self._init_foods(world, preset)
        # ---- [P7g] 资源点挂载（据题材模板给每个地点挂 1 个可采集资源点；drops 确定性选品）。
        # [数值对齐 2026-09-06] 挪到 reagent/种子注入之后——drops 池按 tier 就近取材料，
        # 先挂会吃不到引擎 5 档阶梯材料（初始资源点永远掉不到 epic/legendary）----
        self._init_resource_nodes(world, preset)
        # ---- [P7k1] 合成配方挂载（据题材 + items 池确定性组合；LLM 已出则跳过）----
        self._init_recipes(world, preset)
        # ---- [住宅网格 2026-08-24] 建筑升级材料需求（LLM 生成/代码回退；老档空=旧口径）----
        self._generate_building_material_specs(world, preset)
        # ---- [P9] 天赋池挂载（LLM 题材化生成 player_start.talent_pool；缺失/非法回退题材兜底池）----
        self._init_talent_pool(world, preset)
        # ---- [野心 2026-09-06 用户指示] 一生追求全员分配（题材池确定性；纯引擎零 LLM）----
        self.assign_all_ambitions(world)
        # ---- [P12] NPC 天赋挂载（LLM talent_names 从池引用；缺失去池补 1）+ 怪物池兜底 ----
        self._wire_npc_talents(world)
        self._init_monster_pool(world, preset)
        # ---- [修 2026-09-13 用户指示] 世界生成一致性收口（聚落归零/零野外兜底/怪物池
        #      danger 对齐/kill 目标回填/gather 目标保底）。collect 材料保底上架不在此跑
        #      ——货架由 generate_shops_goods 在 build 后备货，此处上了架会把店标成
        #      「非空架」令 LLM 备货整店跳过（见 world_gen_worker 挂点）。----
        self._ensure_generation_coherence(world, preset)
        # [审 2026-09-13] 零野外降级出的新野外地错过了首轮流资源挂载（_init_resource_nodes
        # 已于上文跑过）——幂等补挂一次（已有节点跳过；聚落仍不挂；pending 已清空则按
        # danger 档从题材池兜底选品）。
        self._init_resource_nodes(world, preset)
        # ---- [P13] 技能池兜底 + 技能书入世（每池技能一本书；商店/掉落/奇遇可获取）----
        self._init_skill_pool(world, preset)
        self._ensure_skill_books(world)
        # [P13] Boss（level>=5）保底掉一本技能书。[!] 必须在书入库后跑（_init_combat_stats
        # 先于此执行，届时 world.items 还没有书——曾因顺序问题整块死代码）。
        self._guarantee_boss_book_loot(world)
        # [P13] 开场任务奖励没给物品时补一本技能书（新手教学循环起点：接任务->领奖->读书学技能）
        if quests and not (quests[0].rewards or {}).get("items"):
            books = [it for it in world.items
                     if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill]
            if books:
                rng_qb = SeededRng.seed_from(world.id, 0, "quest_book")
                quests[0].rewards = dict(quests[0].rewards or {}, items=[rng_qb.pick(books).id])
        # [修 2026-09-06] 商店-场所配对（venue 绑定/补场所；幂等，旧世界进世界自愈同函数）
        self._ensure_shop_places(world)
        return world

    def _guarantee_boss_book_loot(self, world: World):
        """[P13] Boss（level>=5）掉落表保底进一本技能书（rate 0.55 按公式 roll 掉落，
        非保底掉落——[用户指示 2026-08-21] 技能书一律走掉落公式；uncommon+ 优先）。

        确定性：seed 用 npc.id 的 md5（与 _init_combat_stats 的 loot_rng 同源可复现）。
        """
        import hashlib
        books = [it for it in world.items
                 if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill]
        if not books:
            return
        preferred = [it for it in books if (it.rarity or "common") != "common"] or books
        for npc in world.npcs:
            if int(npc.level or 0) < 5 or not npc.loot_table:
                continue
            h = int(hashlib.md5(npc.id.encode()).hexdigest(), 16)
            rng = SeededRng(h)
            book = preferred[rng.roll(0, len(preferred) - 1)]
            if all(e.get("id") != book.id for e in npc.loot_table):
                npc.loot_table.append({"id": book.id, "rate": 0.55})

    def _sanitize_pool_skills(self, raw) -> list:
        """[P13] 清洗 LLM 可学习技能池：白名单 + rarity + 数值钳制（与开局技能同口径）。

        power 按题材模板口径 10-40；去重（与开局技能同名也没关系——学习时按名去重跳过）。
        """
        if not isinstance(raw, list):
            return []
        type_wl = {"attack", "heal", "buff"}
        elem_wl = {"fire", "thunder", "ice", "wind", "wood", "metal", "earth", "light", "dark", "physical", ""}
        scale_wl = {"str", "dex", "int", "vit", ""}
        rarity_wl = {"common", "uncommon", "rare", "epic", "legendary"}
        out, seen = [], set()
        for s in raw[:20]:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name or name in seen:
                continue
            t = str(s.get("type") or "attack").strip()
            if t not in type_wl:
                t = "attack"
            elem = str(s.get("element") or "").strip()
            if elem not in elem_wl:
                elem = ""
            sc = str(s.get("stat_scaling") or "").strip()
            if sc not in scale_wl:
                sc = "str" if t != "heal" else "int"
            r = str(s.get("rarity") or "uncommon").strip()
            if r not in rarity_wl:
                r = "uncommon"
            seen.add(name)
            out.append({
                "id": f"pool_{len(out)}_{name[:8]}",
                "name": name,
                "desc": str(s.get("desc") or "") or "",
                "type": t,
                "power": _safe_int(s.get("power"), 15, 5, 40),
                "cost_mp": _safe_int(s.get("cost_mp"), 5, 0, 15),
                "cooldown": _safe_int(s.get("cooldown"), 1, 0, 4),
                "damage_type": "magical" if sc == "int" else "physical",
                "stat_scaling": sc,
                "element": elem,
                "target": "self" if t == "heal" else "enemy",
                "rarity": r,
                "level": 1, "xp": 0,
                "inflicts": _sanitize_skill_inflicts(s.get("inflicts")),
                "target_pattern": _sanitize_skill_target_pattern(s.get("target_pattern")),
            })
        return out

    def _sanitize_monster_skill_pool(self, raw) -> list:
        """清洗 LLM 怪物技能池：只保留语义字段（name/type/element/desc/inflicts）。

        数值（power/cost_mp/cooldown）由引擎按职能算，落库不存（守数值范式铁律）。
        type/element/inflicts 走 Skill.from_dict 白名单，非 dict/重名丢弃。
        """
        from src.models.world import Skill
        out, seen = [], set()
        for s in (raw or []):
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name or name in seen:
                continue
            sk = Skill.from_dict(s).to_dict()
            seen.add(name)
            out.append({
                "name": name,
                "type": sk.get("type", "attack"),
                "element": sk.get("element", ""),
                "desc": str(s.get("desc") or "").strip(),
                "inflicts": sk.get("inflicts") or [],
            })
        return out

    def _npc_skills_for(self, world: World, npc: NPC) -> list:
        """[NPC 技能池 2026-08-25] 按 combat_role 从技能池确定性抽 NPC 技能（LLM 出语义）。

        boss=3（攻击+恢复+状态）、hostile/friendly=1（攻击）、none=0（平民不给技能，守 P37）。
        池 = world.skill_pool（LLM 生成）优先，空则题材兜底 _GENRE_SKILL_TEMPLATES。
        数值（power/cost_mp/cooldown）取池内已钳制的值；target 对 heal/buff 规整为 self。
        返回干净技能 dict 列表；抽不到返回 []（调用方回退旧关键词兜底）。
        """
        from src.models.world import Skill
        cr = getattr(npc, "combat_role", "") or ""
        if cr not in ("boss", "hostile", "friendly"):
            return []
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        pool = [s for s in (getattr(world, "skill_pool", None) or []) if isinstance(s, dict)]
        if not pool:
            pool = [dict(s) for s in (_GENRE_SKILL_TEMPLATES.get(tid)
                                      or _GENRE_SKILL_TEMPLATES["western_fantasy"])]
        if not pool:
            return []
        rng = SeededRng.seed_from(world.id, 0, f"npc_skill_{npc.id}")
        by_type: dict[str, list] = {"attack": [], "heal": [], "buff": []}
        for s in pool:
            t = str(s.get("type") or "").strip()
            if t in by_type:
                by_type[t].append(s)
        want = ["attack", "heal", "buff"] if cr == "boss" else ["attack"]
        out = []
        for i, kind in enumerate(want):
            cands = by_type.get(kind)
            if not cands:
                continue
            tpl = cands[rng.roll(0, len(cands) - 1)]
            sk = Skill.from_dict(dict(tpl)).to_dict()
            sk["id"] = f"{npc.id}_sk{i}"
            if sk.get("type") in ("heal", "buff"):
                sk["target"] = "self"
            out.append(sk)
        return out

    def _init_skill_pool(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[P13] 技能池兜底：LLM 池为空时填题材模板（数量按 preset.skill_pool_size 截取）。"""
        if getattr(world, "skill_pool", None):
            return
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        pool = [dict(s) for s in (_GENRE_SKILL_TEMPLATES.get(tid)
                                  or _GENRE_SKILL_TEMPLATES["western_fantasy"])]
        size = max(6, min(100, int(getattr(preset, "skill_pool_size", 10) or 10))) if preset else 10
        if len(pool) > size:
            # 截取保梯度：低稀有度优先（入门书必须有），最高两档（legendary/epic）各恒保留
            # 1 条（epic 书是 Boss 掉落保底来源、legendary 进拍卖行；整档切掉会让兜底世界
            # 无高阶书可流通）。缺档就近跳过，head 取满后与保留尾部拼回 size 条。
            order = {"common": 0, "uncommon": 1, "rare": 2, "epic": 3, "legendary": 4, "mythic": 5}
            asc = sorted(pool, key=lambda x: order.get(x.get("rarity"), 1))
            keep_tail = []
            for r in ("legendary", "epic"):
                hit = next((s for s in asc if s.get("rarity") == r), None)
                if hit is not None:
                    keep_tail.append(hit)
            head = [s for s in asc if s not in keep_tail]
            pool = head[:max(0, size - len(keep_tail))] + keep_tail
        # 兜底模板走同一条清洗管线（补 id/target/level/xp，dict 形状与 LLM 池一致）
        world.skill_pool = self._sanitize_pool_skills(pool)

    def _ensure_skill_books(self, world: World):
        """[P13] 每个池技能保证有一本技能书入世（LLM 已有同名教学书则跳过）。

        书 = Item(type=consumable, heal_amount=0, teach_skill=技能 dict)，rarity 同技能
        （定价走品级公式）；书名 = 技能名 + 题材后缀（秘籍/谱/教程/芯片/手记/卷轴）。
        背包「使用」研读习得（去重 by name）；商店选品目录恒含书；精英怪/Boss 掉落含书。
        """
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        suffix_map = _GENRE_BOOK_SUFFIX.get(tid) or _GENRE_BOOK_SUFFIX["western_fantasy"]
        existing_books = {it.teach_skill.get("name") for it in world.items
                          if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill}
        for sk in (getattr(world, "skill_pool", None) or []):
            if not isinstance(sk, dict) or not sk.get("name"):
                continue
            if sk["name"] in existing_books:
                continue
            # 书后缀按技能类型分化（attack=秘籍/heal=丹方/buff=图谱 等题材化变体）
            suffix = suffix_map.get(str(sk.get("type") or ""), "") or suffix_map.get("default", "卷轴")
            # [审查修] 书按稀有度给种子价（不走「无数值消耗品」廉价档：legendary 书
            # 约 27 金会被开局 50 金买光，架空「从强敌身上抢书」的稀缺感）
            _BOOK_PRICE = {"common": 60, "uncommon": 120, "rare": 250,
                           "epic": 500, "legendary": 900, "mythic": 1600}
            # [题材化 2026-09-06 末日档真机抓出] 「修炼之法」是修仙措辞，末日/现代/
            # 科幻档出戏——按题材分词典（缺键回退中性「研习要诀」）。
            _GENRE_BOOK_VERB = {
                "xianxia": "修炼之法", "wuxia": "修炼之法",
                "western_fantasy": "研习之法",
                "modern": "训练要领", "scifi": "使用 Protocol",
                "apocalypse": "生存技巧",
            }
            _verb = _GENRE_BOOK_VERB.get(tid, "研习要诀")
            world.items.append(Item(
                name=f"{sk['name']}·{suffix}",
                type="consumable", rarity=str(sk.get("rarity") or "uncommon"),
                desc=f"记载着「{sk['name']}」{_verb}的{suffix}。研读可习得该技能。",
                heal_amount=0, teach_skill=dict(sk),
                base_price=_BOOK_PRICE.get(str(sk.get("rarity") or "uncommon"), 120),
            ))
            existing_books.add(sk["name"])

    def _sanitize_player_skills(self, raw) -> list:
        """[P12] 清洗 LLM 玩家开局技能：type/element/stat_scaling 白名单 + 数值钳制。
        [P 验收] 开局只留 1 个普通 attack 技能（其余招式靠技能书，守「开局一招」节奏）。"""
        if not isinstance(raw, list):
            return []
        type_wl = {"attack", "heal", "buff"}
        elem_wl = {"fire", "thunder", "ice", "wind", "wood", "metal", "earth", "light", "dark", "physical", ""}
        scale_wl = {"str", "dex", "int", ""}
        out = []
        for s in raw[:1]:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            t = str(s.get("type") or "attack").strip()
            if t not in type_wl:
                t = "attack"
            elem = str(s.get("element") or "").strip()
            if elem not in elem_wl:
                elem = ""
            sc = str(s.get("stat_scaling") or "").strip()
            if sc not in scale_wl:
                sc = "str" if t != "heal" else "int"
            out.append({
                "id": f"p_{len(out)}_{name[:8]}",
                "name": name,
                "type": t,
                "power": _safe_int(s.get("power"), 12, 5, 40),
                "cost_mp": _safe_int(s.get("cost_mp"), 5, 0, 15),
                "cooldown": _safe_int(s.get("cooldown"), 1, 0, 5),
                "damage_type": "magical" if sc == "int" else "physical",
                "stat_scaling": sc,
                "element": elem,
                "target": "self" if t == "heal" else "enemy",
                "level": 1, "xp": 0,
                "inflicts": _sanitize_skill_inflicts(s.get("inflicts")),
                "target_pattern": _sanitize_skill_target_pattern(s.get("target_pattern")),
            })
        return out

    def _wire_npc_talents(self, world: World):
        """[P12] 给无天赋的战斗 NPC 从世界天赋池确定性补 1 个（LLM 只给部分 NPC 配了 talent_names
        的情况已在骨架解析跳过——引擎兜底让所有战斗 NPC 都有天赋加成）。"""
        pool = [t for t in (getattr(world, "talent_pool", []) or []) if isinstance(t, dict)]
        if not pool:
            return
        rng = SeededRng.seed_from(world.id, 0, "npc_talents")
        for npc in world.npcs:
            if npc.talents or npc.level <= 0:
                continue
            pick = rng.pick(pool)
            if isinstance(pick, dict):
                npc.talents = [dict(pick)]

    def _init_monster_pool(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[P12] 怪物池兜底：LLM 池为空时填题材模板（loot 按危险度层级解析成 item id）。"""
        if getattr(world, "monster_pool", None):
            return
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        pool = []
        for m in (we.GENRE_MONSTER_TEMPLATES.get(tid) or we.GENRE_MONSTER_TEMPLATES["western_fantasy"]):
            dmid = (int(m["danger_min"]) + int(m["danger_max"])) // 2
            tier_pool = we.nearest_rarity_pool(world.items, we.danger_to_tier(dmid))
            loot = [it.id for it in tier_pool[:2]]
            pool.append(dict(m, loot=loot))
        world.monster_pool = pool

    def _init_talent_pool(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[P9] 据题材填世界天赋池（LLM player_start.talent_pool 优先；缺失回退 te.fallback_talent_pool）。

        天赋池供开局 TalentSelectDialog 选择 + [P16] 奇遇觉醒（后天唯一获得渠道）。每条经 Talent.from_dict
        白名单清洗。NPC 天赋由 LLM 骨架 npc.talents 填（from_dict 已读），缺失的敌对/要角 NPC 引擎
        据池随机补 1 个（让 NPC 战斗也有天赋加成）。
        """
        from src.models.world import Talent
        # 天赋池：LLM player_start.talent_pool 优先，否则题材兜底池
        genre_id = self._genre_id(world)
        raw_pool = getattr(self, "_pending_talent_pool", None)
        if not isinstance(raw_pool, list) or not raw_pool:
            raw_pool = te.fallback_talent_pool(genre_id)
        world.talent_pool = [Talent.from_dict(t).to_dict() for t in raw_pool if isinstance(t, dict)]
        # NPC 天赋兜底：敌对/要角 NPC 无天赋时从池随机补 1 个（确定性 SeededRng）
        if world.talent_pool:
            from src.utils.rng import SeededRng as _SR
            for npc in world.npcs:
                if npc.talents:
                    continue
                if not (npc.hostile or npc.is_key_npc):
                    continue  # 和平非要角 NPC 不强补
                rng = _SR.seed_from(world.id, 0, f"npc_talent_{npc.id}")
                pick = rng.pick(world.talent_pool)
                if isinstance(pick, dict):
                    npc.talents = [dict(pick)]

    def _init_combat_stats(self, world: World, preset: Optional[WorldSimPreset] = None):
        """世界生成后给 NPC/Item/玩家赋默认战斗数值（仅 crpg 模式；narrative 跳过）。

        NPC：[P44] level/hostile 由 build 的 combat_role 直解路定死（boss=段顶/
        hostile=段底+2/friendly=段中位），此处只补 hp/stats/loot_table；未标注
        combat_role 的 NPC 一律平民（level 1 基线）。
        Item：据 type/rarity 推算 attack/defense/heal_amount。
        玩家：5 维各 5 点凡人基线 + 据职业给初始技能 + hp_max（[P11b] 属性由开局
        AttributeAllocDialog 玩家分配，不再按职业预设）。
        """
        combat_system = self._per_world(world, "combat_system",
                                        preset.combat_system if preset else "crpg", preset)
        # narrative 模式不初始化数值（P2 纯叙事降级）
        if combat_system == "narrative":
            return
        # NPC 数值
        # [P44 用户指示 2026-08-23] 战斗身份唯一来源 = gen schema npc.combat_role 显式标注
        # （boss/hostile/friendly/none，build 直解路已按所在地段派生 level/hostile）。
        # 原 role 文本关键词推算路（_HOSTILE_ROLES/_FRIENDLY_COMBAT_ROLES/boss 词三张词表）
        # 彻底移除——开放措辞永远猜不全（触发案例：「魔头」不在 boss 词表），且词表双向
        # 误伤（友方「守卫」命中敌对词）。漏标/非法 combat_role 的 NPC 按非战斗处理
        # （level 0，下方平民基线段统一抬到 1），不再从 role 文本猜测。
        for npc in world.npcs:
            if npc.level <= 0 and not npc.combat_role:
                npc.level = 0   # 未标注战斗身份 -> 非战斗 NPC（平民基线段统一抬到 1）
            # 敌对判定不再看 role 文本：hostile 只来自 combat_role 直解路（build）或
            # 引擎显式生成（野外怪/拓展 NPC），[P44] 关键词推算路已移除（见上注释）。
            if npc.level > 0:
                # [数值抖动] 同 level NPC 不再逐字节同构：按 npc.id 派生 rng 在基线 ±1-2 抖动
                # （确定性，同 id 同结果）。基线 5+level，抖动后仍随 level 单调，保持强度梯度。
                from src.utils.rng import SeededRng as _SR2
                nr = _SR2.seed_from(world.id, 0, f"npc_stats_{npc.id}")
                jit = lambda base: max(1, base + nr.roll(-1, 2))  # [-1, +2] 偏正，保成长感
                npc.stat_str = max(npc.stat_str, jit(5 + npc.level))
                npc.stat_dex = max(npc.stat_dex, jit(5 + npc.level // 2))
                npc.stat_vit = max(npc.stat_vit, jit(5 + npc.level))
                npc.stat_int = max(npc.stat_int, jit(5 + npc.level // 2))  # 法师系 mp/法强随等级成长
                npc.stat_luk = max(npc.stat_luk, jit(5))
                npc.hp_max = ce.max_hp_for(npc)
                npc.hp = npc.hp_max
                # loot_table：默认空，据 world.items 确定性挂 1-3 件（高等级 NPC 掉落池更丰富）。
                # [P8] 多档化：等级越高掉落条目越多，且 boss（level>=5）偏好更高基础稀有度物品。
                # 稀有度的动态升档（luck+level）在 finish_combat 调 upgrade_drops 完成，此处只定静态池。
                if not npc.loot_table and world.items:
                    import hashlib
                    from src.utils.rng import SeededRng as _SR
                    h = int(hashlib.md5(npc.id.encode()).hexdigest(), 16)
                    candidates = [it for it in world.items
                                  if it.type in ("weapon", "armor", "consumable", "material", "accessory")]
                    if candidates:
                        # boss 偏好 uncommon+；杂兵全池
                        if npc.level >= 5:
                            preferred = [it for it in candidates
                                         if (it.rarity or "common") in ("uncommon", "rare", "epic", "legendary")]
                            pool = preferred or candidates
                            n_entries = 3
                        elif npc.level >= 3:
                            pool = candidates
                            n_entries = 2
                        else:
                            pool = candidates
                            n_entries = 1
                        loot_rng = _SR(h)
                        picked_ids = []
                        for _ in range(n_entries):
                            if not pool:
                                break
                            pick = pool[loot_rng.roll(0, len(pool) - 1)]
                            if pick.id not in picked_ids:
                                picked_ids.append(pick.id)
                        # rate 随等级略增（boss 0.55，杂兵 0.4）
                        rate = 0.55 if npc.level >= 5 else 0.4
                        npc.loot_table = [{"id": pid, "rate": rate} for pid in picked_ids]
            # [P37 用户指示 2026-08-22] 非战斗 NPC（商人/平民/显式 combat_role=none）给
            # 平民基线 level 1 + hp（生活模拟/详情展示需要真实数值；置于敌对判定之后——
            # 平民 role 不含敌对词，抬级不改变 hostile）。不给技能/掉落/AI——仍是非战斗
            # 单位；助战候选另有 affinity>=40 门，处到交情的平民弱鸡助战符合「NPC 像
            # 玩家一样生活」。
            if npc.level <= 0:
                npc.level = 1
                npc.hp_max = ce.max_hp_for(npc)
                npc.hp = npc.hp_max
            # [P7h] NPC 技能 + AI pattern + mp（仅战斗 NPC）
            # [NPC 技能池 2026-08-25] 技能优先从 world.skill_pool 按 combat_role 抽（LLM 出语义），
            # 抽不到回退旧 role 关键词兜底。ai_pattern（行为风格）仍按 role 关键词推算，与技能来源解耦。
            if not npc.skills:
                rl = (npc.role or "")
                if any(k in rl for k in ("法师", "术士", "巫", "魔", "祭司", "牧师")):
                    npc.ai_pattern = "caster"
                elif any(k in rl for k in ("首领", "boss", "Boss", "BOSS", "将军", "统领")):
                    npc.ai_pattern = "aggressive"
                elif any(k in rl for k in ("守卫", "士兵", "卫兵")):
                    npc.ai_pattern = "defensive"
                else:
                    npc.ai_pattern = "balanced"
                npc.skills = self._npc_skills_for(world, npc)
                if not npc.skills:
                    if any(k in rl for k in ("法师", "术士", "巫", "魔", "祭司", "牧师")):
                        npc.skills = [{"id": f"{npc.id}_magic", "name": "法术轰击", "type": "attack",
                                       "power": 15, "cost_mp": 8, "cooldown": 1, "damage_type": "magical",
                                       "stat_scaling": "int", "target": "enemy", "element": "fire"}]
                    elif any(k in rl for k in ("首领", "boss", "Boss", "BOSS", "将军", "统领")):
                        npc.skills = [{"id": f"{npc.id}_heavy", "name": "重击", "type": "attack",
                                       "power": 18, "cost_mp": 0, "cooldown": 2, "damage_type": "physical",
                                       "stat_scaling": "str", "target": "enemy", "element": "physical"}]
            # [P9] 元素兜底推断：LLM 未给 elements 的战斗 NPC，从技能元素取最常见一系
            # （法术轰击兜底技能 element=fire 也会推成火系，符合直觉）。已给则不覆盖。
            if not npc.elements:
                npc.elements = ce.infer_elements_from_skills(npc.skills)
            if npc.mp_max <= 0:
                # [C3 修复 2026-08-25] 天赋 mp_bonus 接线进 mp_max（对照 luck_bonus 口径）
                npc.mp_max = te.effective_stat(npc, "int") * 5 + npc.level * 2 \
                    + int(te.get_talent_bonus(npc, "mp_bonus") or 0)
                npc.mp = npc.mp_max
        # Item 数值（抽到 _fill_item_defaults，地图拓展新物品复用同口径）
        self._fill_item_defaults(world, world.items, preset)
        # [A1 2026-08-29] NPC 初始装备：战斗/治安 NPC 按等级从装备池确定性配装
        # （须在 _fill_item_defaults 之后——攻/防数值此时才可作强度排序依据）。
        for npc in world.npcs:
            self._equip_initial_gear(world, npc)
        # [P11b] 玩家数值：5 维各 5 点凡人基线（不再按职业预设属性）。
        # 属性改由创建流程的 AttributeAllocDialog 让玩家分配（基础 10 点；QQ 彩蛋码 +50）。
        # 职业仍决定初始技能（下方 P7h 分支）；未走分配流程的入口（脚本/老测试）落到此基线。
        p = world.player
        if p.level <= 0:
            p.level = 1
        cls = (p.class_name or "")
        for _k in ("str", "dex", "int", "vit", "luk"):
            setattr(p, f"stat_{_k}", 5)
        p.hp_max = ce.max_hp_for(p)
        p.hp = p.hp_max
        p.xp = 0
        p.xp_next = ce.xp_threshold(p.level)
        p.gold = 150  # [用户指示] 开局只给钱不给装备：玩家自行去商店买起步装具。
        # common 武器基准价约 95 金（standard）/133（hard）/190（hardcore），150 够 relaxed/standard/hard
        # 买下第一把武器 + 一瓶药；hardcore 故意偏紧（该节奏本就该难，可徒手/采集过渡）。
        # 裸奔不卡死：atk = str*2 + 武器攻，无武器仍有 str*2 基础攻击 + 职业技能（不依赖装备）。
        # [P7h] 玩家技能（据 class 给默认技能）+ mp/mp_max + 清冷却
        # [P12] 关键词扩充：武侠/仙侠职业（剑客/刀客/拳师/剑修/修士/医师…）拿到题材化
        # 技能而非通用「猛击」（LLM skills 缺失时的兜底路径）。
        if not p.skills:
            if any(k in cls for k in ("法师", "术士", "巫", "魔", "修士", "道人", "丹师",
                                      "符师", "祭司", "牧师", "医师", "方士")):
                p.skills = [
                    {"id": "p_fireball", "name": "法术轰击", "type": "attack", "power": 12,
                     "cost_mp": 5, "cooldown": 1, "damage_type": "magical",
                     "stat_scaling": "int", "target": "enemy", "element": "fire"},
                ]
            elif any(k in cls for k in ("盗贼", "刺客", "游侠", "弓", "射手", "捕快", "斥候")):
                p.skills = [{"id": "p_precise", "name": "精准刺击", "type": "attack", "power": 12,
                             "cost_mp": 0, "cooldown": 1, "damage_type": "physical",
                             "stat_scaling": "dex", "target": "enemy"}]
            elif any(k in cls for k in ("剑客", "刀客", "剑修", "拳师", "武者", "侠客")):
                # 武侠系：开局只给一招攻势，其余招式靠技能书
                p.skills = [
                    {"id": "p_sword", "name": "凌厉剑势", "type": "attack", "power": 12,
                     "cost_mp": 3, "cooldown": 1, "damage_type": "physical",
                     "stat_scaling": "str", "target": "enemy", "element": "physical"},
                ]
            elif any(k in cls for k in ("战士", "骑士", "武士", "蛮", "镖师", "护卫", "猎人")):
                p.skills = [{"id": "p_heavy", "name": "重击", "type": "attack", "power": 12,
                             "cost_mp": 0, "cooldown": 2, "damage_type": "physical",
                             "stat_scaling": "str", "target": "enemy"}]
            else:
                p.skills = [{"id": "p_strike", "name": "猛击", "type": "attack", "power": 10,
                             "cost_mp": 0, "cooldown": 1, "damage_type": "physical", "target": "enemy"}]
            for _s in p.skills:
                _s.setdefault("level", 1)
                _s.setdefault("xp", 0)
        if p.mp_max <= 0:
            # [C3 修复 2026-08-25] 天赋 mp_bonus 接线进 mp_max（对照 luck_bonus 口径）
            p.mp_max = te.effective_stat(p, "int") * 5 + p.level * 3 \
                + int(te.get_talent_bonus(p, "mp_bonus") or 0)
            p.mp = p.mp_max
        if not p.skill_cooldowns:
            p.skill_cooldowns = {}
        # [用户指示 2026-08] 开局不再自动给装备，只给钱（见上方 p.gold 注释）。
        # 旧逻辑「挑 common weapon/armor 塞 main_hand/chest」已删——它与手动装备的
        # _resolve_target_slot 槽位判定不一致（旧逻辑无视 Item.slot 硬塞 main_hand），
        # 导致「开局在兵刃、卸下再装跑到暗器」的槽位跳变。现在玩家裸奔开局，自行买/捡装备，
        # 装槽统一走 inventory_dialog._resolve_target_slot。

    def _fill_item_defaults(self, world: World, items: list,
                            preset: Optional[WorldSimPreset] = None):
        """[提模块级 2026-10-01] 委托模块函数 _fill_item_defaults（拍卖品生成等
        无 svc 实例的调用方共用同一口径）；历史签名/行为不变。"""
        return _fill_item_defaults(world, items, preset)

    # ============ [P56 毕生所愿 2026-09-25 双 PM 共识] 玩家的长期执念 ============
    # 白话：游戏给每个 NPC 都做了野心系统，唯独玩家没有——「无限世界 != 没有矢量」。
    # 开局（或任何时候）立一个毕生执念，进度全由既有系统承接（零新结算），
    # 达成产 major 事件上世界新闻，然后可以再立新的（无限世界定位）。
    _LIFE_GOAL_KINDS = ("wealth", "mastery", "slay", "explore", "fame")
    _LIFE_GOAL_TIERS = {          # (题材经济缩放基准) S/M/L 三档目标值
        "wealth": (2000, 10000, 50000),
        "mastery": (20, 40, 70),
        "slay": (30, 80, 200),
        "explore": (8, 16, 30),
        "fame": (30, 60, 90),
    }
    _LIFE_GOAL_LABELS = {
        "wealth": "富甲一方（攒下 {v} {cur}）",
        "mastery": "登峰造极（修为达到 {v} 级）",
        "slay": "百战之人（亲手击败 {v} 名敌人）",
        "explore": "踏遍山河（发现 {v} 处地点）",
        "fame": "名动一方（任一势力声望达到 {v}）",
    }
    def life_goal_tiers(self, world: World, kind: str) -> "tuple[int, int, int]":
        """目标三档（wealth 按经济节奏缩放：hard 档钱更难攒，目标更小）。"""
        s_, m_, l_ = self._LIFE_GOAL_TIERS.get(kind, (10, 20, 40))
        if kind == "wealth":
            mult = {"relaxed": 0.7, "standard": 1.0, "hard": 0.7, "hardcore": 0.5}.get(
                str((getattr(world, "config_overlay", None) or {}).get("economy_pace", "standard")), 1.0)
            return (int(s_ * mult), int(m_ * mult), int(l_ * mult))
        return (s_, m_, l_)

    def set_life_goal(self, world: World, kind: str, tier: int) -> "tuple[bool, str]":
        """立/改毕生所愿（三选一档位 S/M/L；更换会清进度——执念不可贪多）。"""
        if kind not in self._LIFE_GOAL_KINDS:
            return False, "没有这种执念"
        tiers = self.life_goal_tiers(world, kind)
        t = max(0, min(2, int(tier)))
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        cur = getattr(world.player, "life_goal", None) or {}
        done = str(cur.get("done_day", "") or "") == str(day) if cur.get("done_day") else False
        world.player.life_goal = {
            "kind": kind, "tier": t, "target_value": tiers[t],
            "created_day": day, "done_day": 0,
        }
        return True, ""

    def life_goal_progress(self, world: World) -> "tuple[int, int]":
        """当前进度 (current, target)（kind 分派到既有系统读数，零新结算）。"""
        goal = getattr(world.player, "life_goal", None) or {}
        kind = str(goal.get("kind", "") or "")
        tv = int(goal.get("target_value", 0) or 0)
        p = world.player
        if kind == "wealth":
            return int(getattr(p, "gold", 0) or 0), tv
        if kind == "mastery":
            return int(getattr(p, "level", 1) or 1), tv
        if kind == "slay":
            return int(getattr(p, "combat_wins", 0) or 0), tv
        if kind == "explore":
            n = sum(1 for l in (world.locations or []) if getattr(l, "discovered", False))
            return n, tv
        if kind == "fame":
            rep = getattr(p, "reputation", None) or {}
            best = max([int(v or 0) for v in rep.values()] or [0])
            return max(0, best), tv
        return 0, 0

    def life_goal_check(self, world: World) -> list:
        """每日检查（挂日结）：达成 -> major 事件上世界新闻 + 标记 done_day（可再立新愿）。

        [执念深化 2026-09-26 用户拍板 [M] 项] 达成时的社交圈反应（纯引擎零 LLM）：
        - 好友 + 交情>=50 存活 NPC（cap 8，交情降序确定性）交情 +2（为你高兴），
          财富执念亲眼见证记「富有」印象；
        - 道贺事件合并一条进动态栏（因果可见性）；
        - major 事件补关联字段（locations=玩家所在地 / npcs=道贺者）——传闻收编/
          编年史/坊间热议三条既有链免费承接；title 带 kind 短标签，传闻 title 保留
          原文 -> 浅印象按 kind 正向命中（富有/无畏），不落「危险」-3 兜底。
        """
        goal = getattr(world.player, "life_goal", None) or {}
        if not goal.get("kind") or int(goal.get("done_day", 0) or 0) > 0:
            return []
        cur, tv = self.life_goal_progress(world)
        if cur < tv:
            return []
        from src.models.world import WorldEvent as _WE
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        tick = int(getattr(world, "tick_count", 0) or 0)
        world.player.life_goal["done_day"] = day
        kind = str(goal.get("kind", "") or "")
        gt = self._genre_text(world).currency
        label = self._LIFE_GOAL_LABELS.get(kind, "达成执念").replace("{v}", str(tv)).replace("{cur}", gt)
        short = label.split("（")[0]
        # 社交圈：好友或交情 >= 50 的存活 NPC（cap 8，防刷屏；交情降序 = 最亲的先道贺）
        friend_ids = set(getattr(world.player, "friend_npc_ids", None) or [])
        circle = [n for n in (getattr(world, "npcs", None) or [])
                  if getattr(n, "alive", True)
                  and (n.id in friend_ids or int(getattr(n, "affinity", 0) or 0) >= 50)]
        circle.sort(key=lambda n: int(getattr(n, "affinity", 0) or 0), reverse=True)
        circle = circle[:8]
        for npc in circle:
            nre.add_affinity(npc, 2)
            if kind == "wealth":
                soc.update_impression(npc, "富有", 2)
        events = [_WE(tick=tick, category="npc", severity="major",
                      title=f"毕生所愿达成·{short}",
                      desc=f"玩家达成了自己的执念——{label}。这段事迹开始在坊间流传。",
                      locations=([world.player.location_id]
                                 if getattr(world.player, "location_id", "") else []),
                      npcs=[n.id for n in circle])]
        if circle:
            names = "、".join(n.name for n in circle[:5]) + ("等" if len(circle) > 5 else "")
            events.append(_WE(tick=tick, category="npc", severity="minor",
                              title=f"{names}前来道贺",
                              desc=f"你达成执念的消息传开，{names}特地前来道贺——交情更进了一步。",
                              npcs=[n.id for n in circle]))
        return events

    # ============ [P55 今日议程 2026-09-25 三 PM 共识] 玩家手头的事（零 LLM 纯读） ============
    def agenda_items(self, world: World, preset: Optional[WorldSimPreset] = None) -> list:
        """聚合「玩家手头的事」：任务/剧情线/委托/产线/Boss 窗口/伤情/记恨——
        每条 {kind, icon, title, detail, priority}（priority 0 最要紧）。
        [!] 零 LLM：全部读既有系统状态；场景页「议程」按钮与状态卡消费。
        [!] 这不是行动选项——不给按钮替玩家决策，只回答「现在有什么等着你」。"""
        items: list = []
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        gt = self._genre_text(world).currency

        # 0) 毕生所愿（玩家的矢量——排最前；已达成也可再立新愿）
        goal = getattr(world.player, "life_goal", None) or {}
        if goal.get("kind"):
            cur, tv = self.life_goal_progress(world)
            label = self._LIFE_GOAL_LABELS.get(str(goal.get("kind")), "").replace("{v}", str(tv))
            label = label.replace("{cur}", gt)
            done = int(goal.get("done_day", 0) or 0) > 0
            items.append({"kind": "goal", "icon": "★", "priority": 3 if done else 2,
                          "title": f"毕生所愿·{label}",
                          "detail": (f"进度 {cur}/{tv}" + ("——已达成！可再立新愿" if done else ""))})

        # 1) 进行中任务（主线/支线/危机；最多取 3 条进度最低的防刷屏）
        actives = [q for q in (getattr(world, "quests", None) or [])
                   if getattr(q, "status", "") == "active"]
        for q in sorted(actives, key=lambda x: int(getattr(x, "progress", 0) or 0))[:3]:
            arc = bool(getattr(q, "hook_arc_id", ""))
            items.append({"kind": "quest", "icon": "※", "priority": 1 if arc else 2,
                          "title": f"{'剧情线' if arc else '任务'}·{q.title}",
                          "detail": f"{(q.objective or '')[:36]}（{int(q.progress or 0)}%）",
                          "nav": "quests"})
        # 1b) [U01/F12 2026-09-30] 完成待领奖（原聚合只有 active——「做完没领」从议程看不到）
        _dones = [q for q in (getattr(world, "quests", None) or [])
                  if getattr(q, "status", "") == "completed"]
        for q in _dones[:2]:
            items.append({"kind": "quest_claim", "icon": "✔", "priority": 1,
                          "title": f"待领奖·{q.title}",
                          "detail": "目标已完成——找到发布者领取奖励",
                          "nav": "quests"})

        # 2) 可接的剧情线任务
        for q in (getattr(world, "quests", None) or []):
            if getattr(q, "status", "") == "available" and getattr(q, "hook_arc_id", ""):
                items.append({"kind": "arc", "icon": "▷", "priority": 1,
                              "title": f"剧情线可接·{q.title}",
                              "detail": (q.objective or "")[:36], "nav": "quests"})
                break

        # 3) 世界 Boss 窗口（[G04/R3 2026-09-30] 边界对齐引擎：day >= until 即关窗
        # （wbe.find/tick 同口径），议程用 day < until——原 <= 会在引擎已关闭的当天
        # 仍显示可讨伐；defeated 留档（tick 不清）但不再是待办）
        for wb in (getattr(world, "world_bosses", None) or []):
            until = int(getattr(wb, "window_until_day", 0) or 0)
            if day < until and not getattr(wb, "defeated", False):
                items.append({"kind": "boss", "icon": "☠", "priority": 0, "nav": "bosses",
                              "title": f"世界Boss·{getattr(wb, 'name', '?')}",
                              "detail": f"讨伐窗口还剩 {until - day} 天（过时不候）"})

        # 4) 据点：产线在制 / 可交付委托 / 资金告急
        # [U01/F15 2026-09-30] stalled（缺料停工）也算在制——停工的订单正等着玩家补料，
        # 口径与 domain order_view 的 open+stalled 一致（原只收 open 会把停工单从议程抹掉）。
        for dm in (getattr(world, "domains", None) or []):
            for o in (getattr(dm, "orders", None) or []):
                if str(o.get("state", "")) in ("open", "stalled"):
                    stalled = (str(o.get("state", "")) == "stalled"
                               or int(o.get("stall_days", 0) or 0) >= de._ORDER_STALL_DAYS)
                    items.append({"kind": "prod", "icon": "⚒", "nav": "domain",
                                  "priority": 1 if stalled else 2,
                                  "title": f"产线·{o.get('out_name', '?')}",
                                  "detail": f"{dm.name}：{o.get('made', 0)}/{o.get('qty', 1)} 件"
                                            + ("（缺料停工，待补料复工）" if stalled else "")})
            if int(getattr(dm, "funds", 0) or 0) < 200:
                items.append({"kind": "fund", "icon": "¤", "priority": 1, "nav": "domain",
                              "title": f"资金告急·{dm.name}",
                              "detail": f"资金池仅 {dm.funds} {gt}（欠薪 3 天员工全走）"})

        # 5) 伤情
        for it in (getattr(world.player, "injuries", None) or []):
            left = int(it.get("until_day", 0) or 0) - day
            if left > 0:
                items.append({"kind": "injury", "icon": "✚", "priority": 1,
                              "title": f"伤情·{it.get('name', '?')}",
                              "detail": f"还剩 {left} 天痊愈（休憩可加速）"})

        # 6) 记恨者（拒卖威胁——去送礼修复或换店）
        _grudges = []
        for n in (getattr(world, "npcs", None) or []):
            imp = getattr(n, "player_impression", None)
            if isinstance(imp, dict) and int(imp.get("trust", 0) or 0) <= -60                     and getattr(n, "alive", True) and getattr(n, "is_merchant", False):
                _grudges.append(str(getattr(n, "name", "") or "?"))
        if _grudges:
            items.append({"kind": "grudge", "icon": "✖", "priority": 1,
                          "title": f"商人记恨（{_grudges[0]}等 {len(_grudges)} 人）",
                          "detail": "他们不肯与你交易——送礼或办事可缓和"})

        # 7) [P57] 信使（NPC 上门 pending）：临期 priority 1，其余 2
        for o in (getattr(world, "outreaches", None) or []):
            if getattr(o, "state", "") != "pending":
                continue
            remain = int(getattr(o, "expires_day", 0) or 0) - day
            label = {"letter": "来信", "bounty": "悬赏", "duel": "约战", "plea": "求助"}.get(
                str(getattr(o, "kind", "")), "来信")
            when = "已过期（待世界推进结算）" if remain < 0 else ("今日截止" if remain <= 0 else f"剩 {remain} 天")
            txt = str(getattr(o, "text", "") or "")
            items.append({"kind": "outreach", "icon": "✉", "priority": 1 if remain <= 1 else 2,
                          "nav": "outreach",
                          "title": getattr(o, "title", "") or label,
                          "detail": f"{txt[:36]}…（{when}；场景页「信使」处理）"})

        # 7b) [U01/F12 2026-09-30] 剧情线概览（世界详情·剧情线 页的入口读数）
        try:
            _n_arcs = sum(1 for a in (getattr(world, "story_arcs", None) or [])
                          if getattr(a, "status", "") == "active")
            if _n_arcs:
                items.append({"kind": "arcs", "icon": "◈", "priority": 3,
                              "title": f"剧情线·{_n_arcs} 条进行中",
                              "detail": "世界正在发生的事——世界详情·「剧情线」页查看走向"})
        except Exception:
            pass
        # 7c) [U01/F12] 可交委托（背包已有货，走到挂靠聚落当面交付）
        try:
            from src.services import commission_engine as _cme
            for _ord in (getattr(world, "commissions", None) or [])[:12]:
                if _cme.deliverable_indices(world, world.player, _ord):
                    items.append({"kind": "commission", "icon": "▤", "priority": 1,
                                  "title": f"可交委托·{_ord.item_name}",
                                  "detail": f"背包已有货——到挂靠聚落「订单」板交付（"
                                            f"{getattr(_ord, 'unit_price', 0)}/件）",
                                  "nav": "commissions"})
                    break
        except Exception:
            pass

        # 7d) [U01 余项/R2 2026-09-30] 地区行情卡（G03 原因->结果：短缺/纾解可追）
        try:
            from src.services import region_pressure as _rp2
            _loc = self._current_location(world)
            if _loc is not None:
                _buy, _sell, _tag = _rp2.region_price_factors(world, str(_loc.id))
                if _tag:
                    _parts = []
                    for _m in (getattr(world, "region_modifiers", None) or []):
                        if not isinstance(_m, dict):
                            continue
                        if str(_m.get("location_id", "")) != str(_loc.id):
                            continue
                        _ud = int(_m.get("until_day", 0) or 0)
                        if _ud and day >= _ud:
                            continue
                        _k = str(_m.get("kind", ""))
                        _src = str(_m.get("source", "") or "")
                        _left = max(0, _ud - day) if _ud else 0
                        _eff = ("买贵卖贱" if _k == "shortage" else "买贱卖贵")
                        _parts.append(f"{_src}——{_eff}"
                                      + (f"（余{_left}天）" if _left else "（随威胁存续）"))
                    if _parts:
                        items.append({"kind": "region", "icon": "◉", "priority": 3,
                                      "title": f"本地行情·{_loc.name}",
                                      "detail": "；".join(_parts[:2])})
        except Exception:
            pass

        # 8) [U01/R0 2026-09-30] 待领滞留（满包没拿走的奖励——权益在，别忘了取）：
        #    任务奖励 pending_items（任务日志该条目「领取剩余物品」补领）+
        #    秘境房间滞留（封印中亦可「深入」补发）。
        for q in (getattr(world, "quests", None) or []):
            pend = [str(x) for x in (getattr(q, "pending_items", None) or []) if str(x)]
            if getattr(q, "status", "") == "claimed" and pend:
                names = "、".join(next(
                    (i.name for i in (getattr(world, "items", None) or [])
                     if getattr(i, "id", "") == pid), pid) for pid in pend[:3])
                items.append({"kind": "quest_pending", "icon": "◈", "priority": 1,
                              "title": f"奖励未领完·{q.title}",
                              "detail": f"滞留物品：{names}"
                                        f"（任务日志该条目可补领，清出背包即可）",
                              "nav": "quests"})
                break
        for dg in (getattr(world, "dungeons", None) or []):
            if not getattr(dg, "discovered", False):
                continue    # [D03] 未发现的不进议程
            rooms_pend = [(f, r) for f in (getattr(dg, "floors", None) or [])
                          for r in (getattr(f, "rooms", None) or [])
                          if getattr(r, "pending_items", None)]
            if rooms_pend:
                n_loot = sum(len(getattr(r, "pending_items", None) or []) for _, r in rooms_pend)
                items.append({"kind": "loot_pending", "icon": "◈", "priority": 1,
                              "title": f"秘境有未取走的收获·{dg.name}",
                              "detail": f"{len(rooms_pend)} 处共 {n_loot} 件滞留"
                                        f"（清出背包后进秘境「深入」即可取回）",
                              "nav": "dungeon"})
                break

        # 排序：要紧的在前
        items.sort(key=lambda i: (int(i.get("priority", 9)), i.get("kind", "")))
        return items[:12]   # [U01/F12] 可滚动议程视图承载完整聚合（原 QMessageBox 8 条上限）

    # ============ [G01/R3 2026-09-30] 远征准备与战后结算 ============
    def expedition_readiness(self, world: World) -> dict:
        """[G01] 战备读数（纯读零 LLM）：伤情/气血/药品/装备耐久/同伴疲劳/宠物/已知敌情。

        「隐藏未探明 Boss 的秘密」——只读已知信息（当前/相邻地点的在窗 Boss 概览，
        不透形态与机制明细之外的任何隐藏数据）。供议程顶部战备卡与后续整备面板消费。"""
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        p = world.player
        gt = self._genre_text(world)
        out = {"lines": []}
        injuries = [it for it in (getattr(p, "injuries", None) or [])
                    if isinstance(it, dict) and int(it.get("until_day", 0) or 0) > day]
        if injuries:
            out["lines"].append("伤情：" + "、".join(
                f"{it.get('name', '伤处')}（余{int(it.get('until_day', 0)) - day}天）"
                for it in injuries[:3]))
        out["lines"].append(f"气血 {p.hp}/{max(1, p.hp_max)} · 灵力 {p.mp}/{max(0, p.mp_max)}")
        # 药品（nle._is_heal_potion 单一口径）
        try:
            from src.services.npc_life_engine import _is_heal_potion
            potions = sum(1 for iid in (p.inventory or [])
                          if _is_heal_potion(next(
                              (i for i in world.items if getattr(i, "id", "") == iid), None)))
        except Exception:
            potions = 0
        out["potions"] = potions
        out["lines"].append(f"随身药品：{potions} 剂回血药" +
                            ("（一瓶都没有——远征前建议备药）" if potions == 0 else ""))
        # 装备耐久（<30% 预警）
        low = []
        for slot, iid in (getattr(p, "equipped", None) or {}).items():
            it = next((i for i in world.items if getattr(i, "id", "") == iid), None)
            if it is None or getattr(it, "type", "") not in ("weapon", "armor"):
                continue
            dm = int(getattr(it, "durability_max", 0) or 0)
            if dm > 0 and int(getattr(it, "durability", 0) or 0) <= dm * 0.3:
                low.append(f"{it.name}（{it.durability}/{dm}）")
        if low:
            out["lines"].append("装备告急：" + "、".join(low[:3]))
        # 同伴疲劳
        tired = []
        for cid in (getattr(p, "companion_npc_ids", None) or []):
            cn = next((n for n in world.npcs if n.id == cid), None)
            if cn is None or not getattr(cn, "alive", True):
                continue
            ft = int(getattr(cn, "companion_fatigue_until_tick", 0) or 0) - int(world.tick_count or 0)
            if ft > 0:
                tired.append(f"{cn.name}（歇{ft}回合）")
        if tired:
            out["lines"].append("同伴疲乏：" + "、".join(tired[:3]) + "（不宜出战）")
        # 宠物
        try:
            pet = pex.active_pet(world)
            if pet is not None:
                out["lines"].append(f"出战宠物：{getattr(pet, 'name', '?')}")
        except Exception:
            pass
        # 已知敌情（当前/相邻地点在窗 Boss——只报窗口，不透隐藏细节）
        try:
            from src.services import world_boss_engine as _wbe2
            cur = self._current_location(world)
            for loc in [cur] + (self._adjacent_locations(world, cur) if cur else []):
                if loc is None:
                    continue
                b = _wbe2.boss_at(world, loc)
                if b is not None:
                    left = max(0, int(b.window_until_day or 0) - day)
                    out["lines"].append(f"已知威胁：「{b.name}」盘踞{loc.name}"
                                        f"（余约{left}天，等级{b.level}）")
        except Exception:
            pass
        return out

    def battle_settlement(self, world: World, summary: dict) -> list:
        """[G01] 战后结算卡（纯读）：拾取/遗留奖励/任务变化/伤情/世界变化，来源可追。

        消费 summary（finish_combat 产物）+ 世界即时状态；返回逐行文本供战报尾部
        追加（真实消耗类药水使用不在 summary 里，v1 不虚构——只列可查事实）。"""
        lines: list = []
        if not isinstance(summary, dict):
            return lines
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        # 拾取与进账
        bits = []
        if summary.get("gold_gained"):
            bits.append(f"{summary['gold_gained']} 金钱")
        if summary.get("xp_gained"):
            bits.append(f"{summary['xp_gained']} 经验")
        grid = summary.get("loot_grid") or {}
        names = [e.get("name", "") for e in (grid.get("entries") or [])][:4]
        if names:
            bits.append("战利品：" + "、".join(x for x in names if x))
        if bits:
            lines.append("收获：" + "；".join(bits))
        # 遗留奖励（可回取）
        pend = sum(len(getattr(r, "pending_items", None) or [])
                   for d in (getattr(world, "dungeons", None) or [])
                   for f in (d.floors or []) for r in (f.rooms or []))
        qp = sum(len(getattr(q, "pending_items", None) or [])
                 for q in (getattr(world, "quests", None) or [])
                 if getattr(q, "status", "") == "claimed")
        if pend or qp:
            lines.append(f"遗留可领：秘境滞留 {pend} 件 · 任务滞留 {qp} 件（议程可查）")
        # 任务变化
        if summary.get("target_npc_defeated"):
            lines.append("击败目标：相关击杀/收集任务进度已由引擎推进（任务日志可查）")
        # 伤情
        inj = summary.get("injuries") or []
        if inj:
            lines.append("新伤：" + "、".join(str(i.get("name", "伤处")) for i in inj[:3])
                         + "（找医者诊治可缩短将养）")
        # 世界变化
        wbits = []
        if summary.get("dungeon_cleared"):
            wbits.append(f"秘境「{summary['dungeon_cleared'].get('dungeon', '?')}」肃清，"
                         f"入口地行情纾解")
        if summary.get("world_boss_defeated"):
            wbits.append(f"世界威胁「{summary['world_boss_defeated'].get('boss', '?')}」伏诛，"
                         f"商路复安")
        try:
            from src.services import region_pressure as _rp
            _b, _s, tag = _rp.region_price_factors(
                world, str(getattr(world.player, "location_id", "") or ""))
            if tag:
                wbits.append("本地行情：" + tag)
        except Exception:
            pass
        if wbits:
            lines.append("世界：" + "；".join(wbits))
        return lines

    # ============ [P57] NPC 上门（信使）============

    # ============ [P62 角色卡进世界 2026-09-26 用户拍板] ============

    IMPORT_CARD_CAP = 5

    def _card_npc_settings(self, world: World, char, preset) -> dict:
        """LLM 把人物卡转写为世界观 NPC 设定（叙事 LLM 单次调用；无 API/失败返回 {}）。

        [P62 口径修订] 酒馆卡本就可导成单聊卡——此处消费的是软件已有单聊人物卡；
        卡面语义（性格/经历/目标）必须落到本世界观里（题材化职业身份、可信目标），
        不复读卡面原文。守 §23 联动：user 消息带世界锚。"""
        api = self._resolve_api((preset.narrative_api_id or preset.calculator_api_id)
                                if preset else "")
        if api is None:
            return {}
        sys_prompt = (
            "你是世界模拟游戏的角色设定师。把一张人物卡转写为本世界观下的一名 NPC 设定。\n"
            "要求：保留卡面的性格底色与关键经历，但身份/职业/目标必须落到本世界观里"
            "（题材化、可信、像原住民）；小传 80-120 字；不要复读卡面原文；只输出 JSON。"
        )
        user = (
            _world_anchor(world) + f"（题材 id：{self._genre_id(world)}）\n"
            + f"【人物卡·姓名】{getattr(char, 'name', '')}\n"
            + f"【人物卡·描述】{str(getattr(char, 'description', '') or '')[:400]}\n"
            + f"【人物卡·性格】{str(getattr(char, 'personality', '') or '')[:200]}\n"
            + f"【人物卡·场景】{str(getattr(char, 'scenario', '') or '')[:200]}\n"
            + '输出 JSON 字面示例（照抄结构替换内容）：\n'
            + '{"role":"云游医师","goal":"寻找失踪的师兄","notes":"……","hobbies":"弈棋",'
            + '"speech_style":"温和而疏离，偶带医者口吻","current_goal":"在镇上落脚行医"}'
        )
        tmp = Preset(name="card_npc_settings", system_prompt=sys_prompt, temperature=0.8,
                     max_tokens=4000, top_p=0.95)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())
        res = llm.chat([{"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user}])
        content = str(getattr(res, "content", "") or "")
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return {}
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}

    def import_card_npc(self, world: World, char, source_name: str = "") -> tuple:
        """把单聊人物卡（Character，软件已有）投放为世界要角 NPC（市场 USP，[L] 项）。

        [P62 口径修订 2026-09-26 用户拍板] 酒馆卡本就可导成单聊卡——这里选**软件已有的
        单聊人物卡**，由系统据卡面生成世界模拟 NPC 设定（LLM 把卡面转写为适配本世界观
        的 role/goal/notes/hobbies/speech_style/current_goal；无 API/失败回退卡面直映射）。
        is_key_npc=True（要角待遇全链路）；affinity=50「与你相识」——导演偏置/信使/
        执念道贺圈自动覆盖；落点=玩家当前地点（立即可互动）；确定性补档 talents/current_goal
        （ensure_key_npc_fields，fill_profile=False——LLM 设定已覆盖语义字段）。
        cap：单世界至多 IMPORT_CARD_CAP 张（world.imported_card_npcs 记录）。
        返回 (ok, msg, npc)。"""
        imported = [str(x) for x in (getattr(world, "imported_card_npcs", None) or [])]
        if len(imported) >= self.IMPORT_CARD_CAP:
            return False, f"这个世界已导入 {len(imported)} 张角色卡（上限 {self.IMPORT_CARD_CAP}）", None
        name = _dedup_npc_name(str(getattr(char, "name", "") or ""), {
            str(getattr(n, "name", "") or "") for n in (world.npcs or [])})
        loc_id = str(getattr(getattr(world, "player", None), "location_id", "") or "")
        preset = None
        try:
            preset = self.storage.load_world_sim_preset()
        except Exception:
            preset = None
        settings = {}
        try:
            settings = self._card_npc_settings(world, char, preset) or {}
        except Exception as e:  # noqa: BLE001 - LLM 设定失败回退卡面直映射
            debug_log(lambda: f"[P62] 卡面 NPC 设定生成失败（回退直映射）: {e}")
        npc = NPC(
            id=str(uuid.uuid4()), name=name,
            role=str(settings.get("role", "") or "旅人")[:12],
            personality=str(settings.get("personality", "") or getattr(char, "personality", "") or "")[:120],
            notes=str(settings.get("notes", "") or getattr(char, "description", "") or "")[:300],
            goal=str(settings.get("goal", "") or getattr(char, "scenario", "") or "")[:60]
                 or "在这片世界寻找自己的位置",
            hobbies=str(settings.get("hobbies", "") or "")[:40],
            speech_style=str(settings.get("speech_style", "") or "")[:60],
            current_goal=str(settings.get("current_goal", "") or "")[:30],
            location_id=loc_id, level=max(1, int(getattr(world.player, "level", 1) or 1)),
            is_key_npc=True, hostile=False, alive=True, affinity=50,
        )
        npc.hp = npc.hp_max = ce.max_hp_for(npc)
        npc.wallet = 50
        npc.visited_locations = [loc_id] if loc_id else []
        world.npcs.append(npc)
        imported.append(npc.id)
        world.imported_card_npcs = imported[:self.IMPORT_CARD_CAP]
        try:
            self.ensure_key_npc_fields(world, npc, preset=None, fill_profile=False)
        except Exception as e:  # noqa: BLE001 - 确定性补档失败不阻断导入
            debug_log(lambda: f"[P62] ensure_key_npc_fields 失败（跳过）: {e}")
        loc = next((l for l in (world.locations or []) if l.id == loc_id), None)
        world.event_log.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0), category="npc", severity="minor",
            title=f"故人抵步：{name}",
            desc=(f"「{name}」来到了{getattr(loc, 'name', '') or '此地'}——"
                  "你们是旧识，故事从重逢开始。"),
            npcs=[npc.id], locations=[loc_id] if loc_id else []))
        src_note = f"（来自 {source_name}）" if source_name else ""
        return True, (f"「{name}」已投入到{getattr(loc, 'name', '') or '世界'}"
                      f"（要角，与你相识）{src_note}"), npc

    def outreach_accept(self, world: World, outreach_id: str) -> dict:
        """接受/收下一条上门（UI 信使对话框消费；返回 {action, msg} 供后续接线）。"""
        return ore.accept(world, outreach_id, int(getattr(world, "tick_count", 0) or 0))

    def outreach_decline(self, world: World, outreach_id: str) -> str:
        return ore.decline(world, outreach_id)

    def outreach_settle_duel(self, world: World, npc) -> dict:
        """[P57] 切磋结算（纯引擎点到即止；不走正式战斗——finish_combat 胜利必杀
        NPC / 一击制拒非敌对，两条既有通路都不适配比武）。返回 {won, hint}，
        hint 交叙事回合（preset_intent custom）写场面。"""
        return ore.settle_duel(world, npc, int(getattr(world, "tick_count", 0) or 0))

    def outreach_move_for_duel(self, world: World, npc_id: str) -> bool:
        """[P57] 约战「上门」：把应战的 NPC 挪到玩家当前地点（守 P18 移动口径：
        同步 location_id + 两地点 npc_ids + 场所重置）。返回是否可战。"""
        npc = next((n for n in (world.npcs or []) if n.id == npc_id), None)
        if npc is None or not getattr(npc, "alive", True):
            return False
        cur = self._current_location(world)
        if cur is None:
            return False
        if npc.location_id != cur.id:
            loc_by_id = {l.id: l for l in (world.locations or [])}
            _move_npc_safe(npc, npc.location_id, cur.id, loc_by_id,
                           getattr(cur, "default_place_id", "") or "")
        return True

    # ============ 文生图（复用 danbooru + comfyui）============
    def generate_image(
            self, cn_prompt: str, style_prefix: str,
            cancel_check: Optional[Callable[[], bool]] = None,
            natural: bool = False,
            width: Optional[int] = None, height: Optional[int] = None,
            cutout_bg: str = "",
            full_body: bool = False,
            positive_override: str = "",
            character_appearances: Optional[list[tuple[str, str]]] = None,
            on_positive: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """中文描述 -> danbooru 加工 -> comfyui 出图。返回图片文件名（存于 world_images_dir），失败返回 None。

        - natural=False：tag 模式（人物/物品，Danbooru tag 串 + 自然语言段）。
        - natural=True：无人场景全自然语言；有人事件插画用 tag 模式并注入 NPC 固定外貌。
        - cutout_bg：[2026-09-25 用户指示] 人像/怪物图「纯色背景抠透明」的背景 tag
          （如 `simple background, white background`；空串=不抠图）。非空时提示词侧剥离
          LLM 输出里已有的背景 tag 再统一追加、negative 追加压制复杂背景词，出图后调
          image_cutout 原地抠成透明 PNG（失败静默保留原图，安全阀见该模块）。
          [!] 只给 tag 模式的人像/怪物/宠物图接线（natural=True 时忽略本参）——场景图/
          背景图/图标透明化没有收益还会破坏观感。
        """
        if not self.comfyui:
            return None
        cn = (cn_prompt or "").strip()
        if not cn and not positive_override.strip():
            return None
        if positive_override.strip():
            positive = positive_override.strip()
            # 固定外貌跳过重新加工，但保留当前 Danbooru 预设的负面模板。
            try:
                negative = str(self.danbooru.storage.load_danbooru_preset().negative_prompt or "")
            except (AttributeError, OSError, ValueError):
                negative = ""
        else:
            try:
                positive, negative = self.danbooru.process_image_description(
                    cn, session_api=None, character_appearances=character_appearances,
                    cancel_check=cancel_check, scene_mode=natural,
                )
            except Exception as e:
                debug_log(lambda: f"[WorldSim] danbooru 加工失败: {e}")
                return None
        if cancel_check and cancel_check():
            return None
        if not positive:
            return None
        if full_body and not natural:
            positive = _full_body_tags(positive)
            negative = _merge_negative(negative, _FULL_BODY_NEGATIVE)
        # 保存角色身份词时不带当前画风和抠图底色；首次成功出图后由调用方决定是否落字段。
        identity_positive = _strip_background_tags(positive)
        do_cutout = bool(cutout_bg) and not natural
        if do_cutout:
            # 全为背景 tag 时剥完是空串 -> 不能拼出前导逗号（"style, , simple background"）
            base = _strip_background_tags(positive)
            positive = f"{base}, {cutout_bg}" if base else cutout_bg
            negative = _merge_negative(negative, _CUTOUT_NEGATIVE)
        if style_prefix:
            positive = f"{style_prefix}, {positive}"
        try:
            path = self.comfyui.generate(positive, negative, dest_dir=paths.world_images_dir(),
                                         width=width, height=height)
        except Exception as e:
            debug_log(lambda: f"[WorldSim] comfyui 生成失败: {e}")
            return None
        if not path:
            return None
        if on_positive is not None and identity_positive:
            on_positive(identity_positive)
        if do_cutout:
            self._safe_cutout(path)
        return os.path.basename(path)

    @staticmethod
    def _safe_cutout(path: str) -> None:
        """[2026-09-25] 抠图安全调用：任何失败都不影响生图主链路（图照出，只是不透明）。

        [!] 必须包这一层：image_cutout 顶层 `import numpy`，而 numpy 只是 chromadb 的
        传递依赖（requirements 未直接声明）——导入期异常或任何未捕获异常若逃出去，
        会打断整批世界生图。「抠图失败静默保留原图」是 image_cutout 自己的契约，
        这里再兜一次调用侧（import/环境级失败）。
        """
        try:
            from src.services.image_cutout import cutout_solid_background
            cutout_solid_background(path)
        except Exception as e:
            debug_log(lambda err=e: f"[WorldSim] 抠图跳过（不影响出图）: {err}")

    @staticmethod
    def _cutout_bg_tag(preset) -> str:
        """[2026-09-25 用户指示] 从 preset 解析「人像/怪物图纯色背景抠透明」的背景 tag。

        未开启返回空串（= 走旧行为，不追加背景 tag 也不抠图）。单一口径：世界生成批次/
        补图/单个重生成/怪物宠物预生成全部经此取值，保证同一开关下全链路行为一致；
        非法背景色按白名单回退白（脏存档不污染提示词）。
        """
        if preset is None or not getattr(preset, "image_cutout_enabled", False):
            return ""
        bg = str(getattr(preset, "image_cutout_bg", "") or "").strip().lower()
        if bg not in CUTOUT_BG_COLORS:
            bg = "white"
        return f"simple background, {bg} background"

    @staticmethod
    def _bg_hint(preset, default: str = "简洁背景") -> str:
        """[2026-09-25] 人像/怪物/宠物图的中文背景提示词口径。

        抠图开启时要求「纯色背景」（与代码抠图配套）；关闭时返回 default（旧口径）——
        让「关掉开关」真正等于回到改动前行为，不做静默的行为漂移。
        """
        return "纯色背景" if getattr(preset, "image_cutout_enabled", False) else default

    def regenerate_npc_avatar(self, world: World, npc, cancel_check=None) -> Optional[str]:
        """[体验] 按 NPC 外貌描述重新生成头像（NpcDetailDialog「重新生成头像」按钮）。

        走 generate_image 同管线（danbooru 转 tag + comfyui），成功写回 npc.avatar。
        返回文件名（不含路径）；appearance 为空/失败返回 None。落盘由调用方负责。
        """
        if not self.comfyui:
            return None
        cn = (getattr(npc, "appearance", "") or "").strip()
        fixed = (getattr(npc, "appearance_tags", "") or "").strip()
        if not cn and not fixed:
            return None
        preset = self.storage.load_world_sim_preset() if self.storage else None
        style = (getattr(preset, "image_style_prefix", "") or "") if preset else ""
        captured: list[str] = []
        fname = self.generate_image(cn, style, cancel_check=cancel_check,
                                    cutout_bg=self._cutout_bg_tag(preset), full_body=True,
                                    positive_override=fixed,
                                    on_positive=captured.append if not fixed else None)
        if fname:
            npc.avatar = fname
            if not fixed and captured:
                npc.appearance_tags = captured[0]
        return fname

    def regenerate_location_background(self, world: World, loc, cancel_check=None) -> Optional[str]:
        """[2026-08-27] 强制重生成地点背景图（覆盖已有图，用「无人」场景提示词）。

        走 generate_image natural=True（场景全自然语言，硬约束无人）。成功写回
        loc.background、删旧图文件并返回文件名；失败返回 None。落盘由调用方负责。
        """
        if not self.comfyui:
            return None
        name = (getattr(loc, "name", "") or "").strip()
        if not name:
            return None
        preset = self.storage.load_world_sim_preset() if self.storage else None
        style = (getattr(preset, "image_style_prefix", "") or "") if preset else ""
        cn = f"{name}，{getattr(loc, 'desc', '') or ''}，{getattr(loc, 'region', '') or '未知'}区域，场景背景，无人"
        old = (getattr(loc, "background", "") or "").strip()
        fname = self.generate_image(cn, style, cancel_check=cancel_check, natural=True)
        if fname:
            loc.background = fname
            # 删旧图文件（唯一引用，换图即孤儿）；best-effort 不阻断
            if old and old != fname:
                try:
                    os.remove(os.path.join(paths.world_images_dir(), old))
                except OSError:
                    pass
        return fname

    # ============ 怪物图预生成 + 战斗展示（[2026-08-21 用户指示] 生成世界/拓展时出好，游玩中只展示不渲染）============
    @staticmethod
    def _field(target, key: str) -> str:
        """NPC 对象 / 怪物模板 dict 双兼容取字段。"""
        if isinstance(target, dict):
            return str(target.get(key, "") or "")
        return str(getattr(target, key, "") or "")

    @classmethod
    def _combat_image_key(cls, target) -> Optional[str]:
        """怪物图内容键：md5(name|desc 前 60 字)。同名同貌复用同图；名字相同但描述
        不同的怪（同名不同种的精英/变体）分图。返回 None = 无名目标（不可生成）。"""
        import hashlib
        name = cls._field(target, "name").strip()
        if not name:
            return None
        desc = cls._field(target, "desc")[:60]
        return hashlib.md5(f"{name}|{desc}".encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _monster_templates_of(world: World) -> list[dict]:
        """本世界可遭遇的怪物模板全量：题材兜底池 + world.monster_pool（拓展新增物种）。"""
        from src.services import wilderness_engine as _we
        ov = getattr(world, "config_overlay", None) or {}
        tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) else "western_fantasy"
        pool = list(_we.GENRE_MONSTER_TEMPLATES.get(tid) or _we.GENRE_MONSTER_TEMPLATES["western_fantasy"])
        pool += [m for m in (getattr(world, "monster_pool", None) or []) if isinstance(m, dict)]
        return pool

    def _ensure_image_file(self, cn: str, style: str, dest_name: str,
                           cancel_check=None, natural: bool = False,
                           width: Optional[int] = None, height: Optional[int] = None,
                           cutout_bg: str = "", full_body: bool = False) -> Optional[str]:
        """按目标文件名出图（缓存命中即跳过）：生成 -> 改名落缓存。返回文件名，失败 None。
        natural=True 走全自然语言模式（无人场景/符号意象，同物品图标口径）。
        width/height：按本次调用覆盖出图尺寸（None=用配置默认）。
        cutout_bg：[2026-09-25] 纯色背景抠透明（空串=不抠，见 generate_image）。
        [!] 缓存命中时若开启抠图，对已有文件做一次**幂等**抠图（image_cutout 自带
          已抠跳过）——存量旧图不必重出图即可升级成透明底，重复调用无害。"""
        dest = os.path.join(paths.world_images_dir(), dest_name)
        if os.path.exists(dest):
            if cutout_bg:
                self._safe_cutout(dest)
            return dest_name
        src = self.generate_image(cn, style, cancel_check, natural=natural, width=width,
                                  height=height, cutout_bg=cutout_bg, full_body=full_body)
        if not src:
            return None
        try:
            os.replace(os.path.join(paths.world_images_dir(), src), dest)
            return dest_name
        except OSError:
            return src

    def cached_combat_image(self, target) -> Optional[str]:
        """查怪物图缓存（不生成）：优先 target.avatar（NPC 头像管线产物，敌对 NPC
        世界生成时已出图）；否则查内容键 combat_{key}.png（模板名稳定，
        跨战斗/跨世界复用）；仍未命中时剥精英前缀按基础怪键回退再查一次
        （精英 = 同生物强化版，复用基础怪预生成图）。命中返回文件名，未命中返回 None。"""
        av = self._field(target, "avatar").strip()
        if av and os.path.exists(os.path.join(paths.world_images_dir(), av)):
            return av
        key = self._combat_image_key(target)
        if key is None:
            return None
        fname = f"combat_{key}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return self._elite_base_image(target)

    def _elite_base_image(self, target) -> Optional[str]:
        """精英怪图回退：名字剥掉已知精英前缀后，用「基础名 + 原 desc」再查一次缓存。"""
        from src.services import wilderness_engine as _we
        base_name = _we.strip_elite_prefix(self._field(target, "name"))
        if not base_name:
            return None
        key = self._combat_image_key({"name": base_name, "desc": self._field(target, "desc")})
        if key is None:
            return None
        fname = f"combat_{key}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return None

    def pregenerate_monster_images(self, world: World, preset=None,
                                   cancel_check=None, only_pool: bool = False) -> int:
        """怪物图批量预生成（地图拓展等游玩外时机调用）：补齐缺失的怪物图缓存。

        - only_pool=False：题材池 + monster_pool 全量查缺（世界生成走 generate_world_images
          的 monster 任务，此参数供拓展后补新物种）。
        - only_pool=True：只处理 world.monster_pool（拓展新增物种，题材池在世界生成时已出过）。
        返回新生成数量（缓存命中不计）。"""
        made = 0
        templates = ([m for m in (getattr(world, "monster_pool", None) or []) if isinstance(m, dict)]
                     if only_pool else self._monster_templates_of(world))
        preset = preset or (self.storage.load_world_sim_preset() if self.storage else None)
        style = (getattr(preset, "image_style_prefix", "") or "") if preset else ""
        cutout_bg = self._cutout_bg_tag(preset)
        bg_hint = self._bg_hint(preset)
        for m in templates:
            if cancel_check and cancel_check():
                break
            key = self._combat_image_key(m)
            if key is None:
                continue
            cn = (f"{self._field(m, 'name')}，{self._field(m, 'role')}，"
                  f"{self._field(m, 'desc')}，怪物全身立绘，{bg_hint}")
            before = os.path.exists(os.path.join(paths.world_images_dir(), f"combat_{key}.png"))
            fname = self._ensure_image_file(cn, style, f"combat_{key}.png", cancel_check,
                                            cutout_bg=cutout_bg, full_body=True)
            if fname and not before:
                made += 1
        return made

    # ============ [2026-08-23 用户指示] 宠物图 + 技能图标预生成（怪物图同款管线） ============

    @staticmethod
    def _skill_icon_targets(world: World) -> "list[tuple[dict, str]]":
        """技能图标目标：世界技能池 + 玩家已学技能，按名去重（内容键同怪物口径）。"""
        seen: set[str] = set()
        out: list[tuple[dict, str]] = []
        pools = [s for s in (getattr(world, "skill_pool", None) or []) if isinstance(s, dict)]
        for sk in (getattr(getattr(world, "player", None), "skills", None) or []):
            if isinstance(sk, dict):
                pools.append(sk)
        for sk in pools:
            name = str(sk.get("name", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            key = WorldSimService._combat_image_key(sk)
            if key is not None:
                out.append((sk, key))
        # [宠物技能] 种族候选技能同池同键（md5(name|desc)）-> skill_ 图标一并登记，
        # PetDialog 技能按钮查 cached_skill_icon 命中
        for gid, pool in pex._GENRE_PET_SPECIES.items():
            for sp in pool:
                for nm in (sp.get("skill_candidates") or []):
                    nm = str(nm or "").strip()
                    if not nm or nm in seen:
                        continue
                    sk = next((s for s in (_GENRE_SKILL_TEMPLATES.get(gid) or [])
                               if s.get("name") == nm), None)
                    if sk is None:
                        continue
                    seen.add(nm)
                    key = WorldSimService._combat_image_key(sk)
                    if key is not None:
                        out.append((sk, key))
        return out

    def _pregen_gated(self, preset) -> "tuple[bool, str, str]":
        """预生成家族统一门控（image_enabled + image_monster）+ 风格前缀 + 抠图背景 tag。"""
        if preset is None:
            return False, "", ""
        if not (getattr(preset, "image_enabled", False)
                and getattr(preset, "image_monster", True)):
            return False, "", ""
        return (True, (getattr(preset, "image_style_prefix", "") or ""),
                self._cutout_bg_tag(preset))

    def pregenerate_pet_images(self, world: World, preset=None,
                               cancel_check=None) -> int:
        """宠物种族图批量预生成：题材种族池（9/题材）逐种出 pet_{key}.png 缓存。
        宠物经奇遇驯服/野外幼崽获得，获得时零渲染直接查缓存（cached_pet_image）。"""
        preset = preset or (self.storage.load_world_sim_preset() if self.storage else None)
        ok, style, cutout_bg = self._pregen_gated(preset)
        if not ok:
            return 0
        bg_hint = self._bg_hint(preset)
        made = 0
        for sp in pex.species_pool(_p34a_genre_id(world)):
            if cancel_check and cancel_check():
                break
            key = self._combat_image_key(sp)
            if key is None:
                continue
            cn = f"{sp.get('name')}，{sp.get('desc') or ''}，灵宠全身立绘，可爱，{bg_hint}"
            before = os.path.exists(os.path.join(paths.world_images_dir(), f"pet_{key}.png"))
            fname = self._ensure_image_file(cn, style, f"pet_{key}.png", cancel_check,
                                            cutout_bg=cutout_bg, full_body=True)
            if fname and not before:
                made += 1
        return made

    def pregenerate_skill_icons(self, world: World, preset=None,
                                cancel_check=None) -> int:
        """技能图标批量预生成：世界技能池 + 玩家已学技能（按名去重）出 skill_{key}.png
        缓存，全自然语言模式（无人符号意象，同物品图标口径）。战斗技能按钮直接查缓存。"""
        ok, style, _ = self._pregen_gated(
            preset or (self.storage.load_world_sim_preset() if self.storage else None))
        if not ok:
            return 0
        made = 0
        for sk, key in self._skill_icon_targets(world):
            if cancel_check and cancel_check():
                break
            cn = f"{sk.get('name')}，{str(sk.get('desc') or '')}，技能图标，意象化符号，简洁构图，无人"
            before = os.path.exists(os.path.join(paths.world_images_dir(), f"skill_{key}.png"))
            fname = self._ensure_image_file(cn, style, f"skill_{key}.png", cancel_check,
                                            natural=True)
            if fname and not before:
                made += 1
        return made

    def cached_pet_image(self, species) -> Optional[str]:
        """查宠物图缓存（不生成）：pet_{key}.png。species 传种族 dict 或 Pet 实体
        （经 find_species 解析）；未命中返回 None（UI 无图占位不挡功能）。"""
        sp = species if isinstance(species, dict) else \
            pex.find_species(getattr(species, "species", "") or "")
        if not sp:
            return None
        key = self._combat_image_key(sp)
        if key is None:
            return None
        fname = f"pet_{key}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return None

    def cached_skill_icon(self, skill) -> Optional[str]:
        """查技能图标缓存（不生成）：skill_{key}.png（战斗技能按钮/宠物面板用）。"""
        if not isinstance(skill, dict):
            return None
        key = self._combat_image_key(skill)
        if key is None:
            return None
        fname = f"skill_{key}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return None

    def cached_building_icon(self, world: World, kind: str) -> Optional[str]:
        """查建筑图标缓存（不生成）：building_{题材}_{kind}.png（HomeDialog 用）。"""
        fname = f"building_{_p34a_genre_id(world)}_{kind}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return None

    def cached_furniture_icon(self, world: World, kind: str) -> Optional[str]:
        """查家具图标缓存（不生成）：furniture_{题材}_{kind}.png（HomeDialog 用）。"""
        fname = f"furniture_{_p34a_genre_id(world)}_{kind}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return None

    def cached_crop_image(self, world: World, crop_name: str, stage: int) -> Optional[str]:
        """查作物生长阶段图缓存（不生成）：crop_{题材}_{key}_stage{0-3}.png。"""
        from src.services import farm_engine as fe
        tid = _p34a_genre_id(world)
        fname = f"crop_{tid}_{fe._crop_key(tid, crop_name)}_stage{int(stage)}.png"
        if os.path.exists(os.path.join(paths.world_images_dir(), fname)):
            return fname
        return None

    def pregenerate_farm_icons(self, world: World, preset=None, cancel_check=None) -> int:
        """作物生长阶段图批量预生成（每作物 4 阶段，natural 模式，命中即跳）。

        门控：image_enabled + image_home + comfyui（与建筑/家具图标同开关）。
        键 crop_{题材}_{key}_stage{0-3}.png，HomeDialog 田地块渲染用，无图回退徽章。"""
        from src.services import farm_engine as fe
        preset = preset or (getattr(self, "storage", None).load_world_sim_preset()
                            if getattr(self, "storage", None) else None)
        if (preset is None or not preset.image_enabled
                or not getattr(preset, "image_home", True)
                or not getattr(self, "comfyui", None)):
            return 0
        style = getattr(preset, "image_style_prefix", "") or ""
        tid = _p34a_genre_id(world)
        stage_zh = {0: "播种期（土壤与种子）", 1: "发芽期（嫩芽）",
                    2: "生长期（茂盛植株）", 3: "成熟期（饱满果实）"}
        made = 0
        for crop in fe.genre_crops(world):
            name = str(crop.get("name", "") or "")
            if not name:
                continue
            key = fe._crop_key(tid, name)
            for stage in range(4):
                if cancel_check and cancel_check():
                    return made
                dest = f"crop_{tid}_{key}_stage{stage}.png"
                before = os.path.exists(os.path.join(paths.world_images_dir(), dest))
                cn = (f"{name}，{stage_zh[stage]}，灵田作物生长阶段图，意象化，简洁构图，无人")
                fname = self._ensure_image_file(cn, style, dest, cancel_check, natural=True)
                if fname and not before:
                    made += 1
        return made

    def pregenerate_home_icons(self, world: World, preset=None, cancel_check=None) -> int:
        """建筑/家具图标批量预生成（kind+题材键固定文件名，natural 模式，命中即跳）。

        门控：image_enabled + image_home + comfyui（独立于 image_monster 开关）。
        老世界补图入口（生成批次未含 home 图标时手动/验收补）。
        """
        preset = preset or (self.storage.load_world_sim_preset() if self.storage else None)
        if (preset is None or not preset.image_enabled
                or not getattr(preset, "image_home", True) or not self.comfyui):
            return 0
        style = getattr(preset, "image_style_prefix", "") or ""
        tid = _p34a_genre_id(world)
        made = 0
        for prefix, catalog in (("building", he.building_catalog(world)),
                                ("furniture", he.furniture_catalog(world))):
            seen_kinds = set()
            for entry in catalog:
                kind = str(entry.get("kind", "") or "")
                if not kind or kind in seen_kinds:
                    continue
                seen_kinds.add(kind)
                if cancel_check and cancel_check():
                    return made
                cn = (f"{entry.get('name', '')}，{str(entry.get('desc') or '')}，"
                      f"{'建筑' if prefix == 'building' else '家具'}图标，意象化符号，简洁构图，无人")
                dest = f"{prefix}_{tid}_{kind}.png"
                before = os.path.exists(os.path.join(paths.world_images_dir(), dest))
                fname = self._ensure_image_file(cn, style, dest, cancel_check, natural=True,
                                               width=512, height=512)
                if fname and not before:
                    made += 1
        return made

    # ============ 场景事件生图（P2 场景交互循环，叙事 LLM 旁白 [img:...] 触发）============
    def process_scene_images(
            self, narration: str, world: World, preset: WorldSimPreset,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> list[str]:
        """从叙事 LLM 旁白中提取 [img:...] 标签，按限频策略出图。

        限频策略（顺序检查，任一不通过即跳过本次）：
          1. preset.narrative_image_event=False -> 整个跳过
          2. preset.image_enabled=False（场景生图也走这个总开关） -> 跳过
          3. preset.narrative_image_freq>0 且 (world.tick_count - state.last_image_tick) < freq -> 跳过
          4. preset.narrative_image_max_per_session>0 且 state.generated_count >= max -> 跳过
          5. 旁白中无 [img:...] 标签 -> 跳过

        通过后遍历标签逐个调 generate_image（danbooru 加工 + comfyui 出图，复用 §12 管线）。
        每张成功出图后更新 state.last_image_tick/generated_count 并 save_scene_image_state。
        cancel_check 透传到 generate_image，停止按钮可中断（仿 §5 守约）。

        返回生成的图片文件名列表（不含路径，存于 world_images_dir）。
        [!] 独立铁律：不动 ChatOrchestrator/ChatView/MessageBubble。
          仅复用 world_sim_service.generate_image + utils.parse_image_tags。
        """
        if not narration or not world or not preset:
            return []
        if not preset.narrative_image_event or not preset.image_enabled:
            return []
        # 加载场景生图状态（独立于 SceneLog，守 §21a 场景日志独立持久化）
        if self.storage is None:
            return []
        state = self.storage.load_scene_image_state(world.id)
        # 频次限频：距上次出图未满 N 回合 -> 跳过（LLM 即便在旁白里输出 [img:] 也不出图）
        if preset.narrative_image_freq > 0:
            if state.last_image_tick > 0 and (world.tick_count - state.last_image_tick) < preset.narrative_image_freq:
                debug_log(lambda: f"[WorldSim] 场景生图限频: tick={world.tick_count} last={state.last_image_tick} freq={preset.narrative_image_freq}")
                return []
        # 累计上限
        if preset.narrative_image_max_per_session > 0 and state.generated_count >= preset.narrative_image_max_per_session:
            debug_log(lambda: f"[WorldSim] 场景生图累计上限: count={state.generated_count} max={preset.narrative_image_max_per_session}")
            return []
        # 提取 [img:...] 标签
        tags = parse_image_tags(narration)
        if not tags:
            return []
        # 逐个出图（限频一次只取一个标签，多标签走多次回调）
        # 决策：每回合最多出 1 张图（既符合"关键事件"语义，也防 LLM 一次性输出多张刷屏）。
        # 多标签时取第一个；剩余丢弃（避免一回合多图）。
        pos_prompt, _neg_prompt = tags[0]
        if cancel_check and cancel_check():
            return []
        style = preset.image_style_prefix or ""
        # 只让画面出现本世界真实且被图像提示词点名的 NPC；无人画面仍走纯场景模式。
        pictured = [n for n in world.npcs if n.name and n.name in pos_prompt][:2]
        appearances = [(n.name, str(getattr(n, "appearance_tags", "") or n.appearance or ""))
                       for n in pictured]
        prompt = (f"人物事件插画，{pos_prompt}。画面只出现{'、'.join(n.name for n in pictured)}。"
                  if pictured else pos_prompt)
        fname = self.generate_image(prompt, style, cancel_check=cancel_check,
                                    natural=not pictured,
                                    character_appearances=appearances or None)
        if not fname:
            debug_log(lambda: f"[WorldSim] 场景生图失败: {pos_prompt[:30]}")
            return []
        # 更新状态并落盘
        state.last_image_tick = world.tick_count
        state.generated_count += 1
        try:
            self.storage.save_scene_image_state(state)
        except Exception as e:
            # 状态保存失败不阻断（图片已出，下次频次判断可能不准但不影响本次显示）
            debug_log(lambda: f"[WorldSim] scene_image_state 保存失败: {e}")
        debug_log(lambda: f"[WorldSim] 场景生图成功: {fname} (count={state.generated_count})")
        return [fname]

    def _world_image_tasks(self, world: World, preset: WorldSimPreset) -> list[tuple]:
        """[拆分 2026-08-29] 世界生图任务清单（generate_world_images 与
        regenerate_missing_images 共用；缓存类任务命中即跳过、DB 类任务按缺失过滤
        分别由执行方决定）。"""
        tasks: list[tuple[str, str, str]] = []  # (label, cn_prompt, target_kind+id)

        if preset.image_enabled and preset.image_world_banner:
            banner_cn = f"{world.premise}，{', '.join(world.genre_tags)}风格，{world.tone}基调，世界全景"
            tasks.append(("世界 banner", banner_cn, f"banner:{world.id}"))

        if preset.image_enabled and preset.image_location_bg:
            for loc in world.locations:
                cn = f"{loc.name}，{loc.desc}，{loc.region}区域，场景背景，无人"
                tasks.append((f"地点背景·{loc.name}", cn, f"location:{loc.id}"))

        if preset.image_enabled and preset.image_npc_avatar:
            for npc in world.npcs:
                cn = f"{npc.appearance or npc.name}，全身立绘，从头到脚完整入画，双脚可见"
                tasks.append((f"NPC头像·{npc.name}", cn, f"npc:{npc.id}"))

        if preset.image_enabled and preset.image_legendary_item:
            for item in world.items:
                if item.rarity in ("epic", "legendary"):
                    cn = f"{item.name}，{item.desc}，{item.type}类型，{item.rarity}稀有度，物品图标"
                    tasks.append((f"物品图·{item.name}", cn, f"item:{item.id}"))
        if preset.image_enabled and preset.image_normal_item:
            for item in world.items:
                if item.rarity not in ("epic", "legendary", "mythic"):
                    cn = f"{item.name}，{item.desc}，{item.type}类型，物品图标"
                    tasks.append((f"物品图·{item.name}", cn, f"item:{item.id}"))

        # [P5c] 玩家立绘：根据 class_name + genre_tags + tone 生成玩家全身像。
        # 走独立开关 image_player_avatar（默认 False 保持兼容）。
        if preset.image_enabled and preset.image_player_avatar:
            p = world.player
            pclass = p.class_name or "冒险者"
            pbg = p.background or ""
            cn = (f"玩家立绘，{pclass}职业，{pbg}，"
                  f"{', '.join(world.genre_tags)}风格，{world.tone or '默认'}基调，"
                  f"全身像，正面站姿，{self._bg_hint(preset, '无背景')}")
            tasks.append(("玩家立绘", cn, "player:player"))

        # [2026-08-21] 怪物图预生成（用户指示：游玩中不现场渲染，生成世界时批量出好）：
        # 题材怪物池 + 世界自定义 monster_pool 全量入任务（缓存命中即跳过，实际只补缺）。
        # [2026-08-23 用户指示] 宠物种族图 + 技能图标加入预生成家族（同 image_monster
        # 开关门控——该开关语义 = 游玩外批量预生成全家）。
        if preset.image_enabled and getattr(preset, "image_monster", True):
            _bg = self._bg_hint(preset)
            for m in self._monster_templates_of(world):
                key = self._combat_image_key(m)
                if key is None:
                    continue
                cn = f"{m.get('name')}，{m.get('role') or ''}，{m.get('desc') or ''}，怪物全身立绘，{_bg}"
                tasks.append((f"怪物图·{m.get('name')}", cn, f"monster:{key}"))
            for sp in pex.species_pool(_p34a_genre_id(world)):
                key = self._combat_image_key(sp)
                if key is None:
                    continue
                cn = f"{sp.get('name')}，{sp.get('desc') or ''}，灵宠全身立绘，可爱，{_bg}"
                tasks.append((f"宠物图·{sp.get('name')}", cn, f"pet:{key}"))
            for sk, key in self._skill_icon_targets(world):
                cn = f"{sk.get('name')}，{str(sk.get('desc') or '')}，技能图标，意象化符号，简洁构图，无人"
                tasks.append((f"技能图标·{sk.get('name')}", cn, f"skill:{key}"))

        # [住宅视觉] 建筑/家具图标预生成（题材+kind 键固定文件名，natural 模式；
        # HomeDialog 查 cached_building_icon/cached_furniture_icon，无图回退徽章）
        if preset.image_enabled and getattr(preset, "image_home", True):
            tid = _p34a_genre_id(world)
            for entry in he.building_catalog(world):
                kind = str(entry.get("kind", "") or "")
                if not kind:
                    continue
                cn = (f"{entry.get('name', '')}，{str(entry.get('desc') or '')}，"
                      f"建筑图标，意象化符号，简洁构图，无人")
                tasks.append((f"建筑图标·{entry.get('name', '')}", cn,
                              f"building:{tid}_{kind}"))
            for entry in he.furniture_catalog(world):
                kind = str(entry.get("kind", "") or "")
                if not kind:
                    continue
                cn = (f"{entry.get('name', '')}，{str(entry.get('desc') or '')}，"
                      f"家具图标，意象化符号，简洁构图，无人")
                tasks.append((f"家具图标·{entry.get('name', '')}", cn,
                              f"furniture:{tid}_{kind}"))
            # [住宅网格 2026-08-24] 作物生长阶段图（每作物 4 阶段，田地块渲染用）
            from src.services import farm_engine as fe
            _stage_zh = {0: "播种期", 1: "发芽期", 2: "生长期", 3: "成熟期"}
            for crop in fe.genre_crops(world):
                cname = str(crop.get("name", "") or "")
                if not cname:
                    continue
                ckey = fe._crop_key(tid, cname)
                for stage in range(4):
                    cn = (f"{cname}，{_stage_zh[stage]}，灵田作物生长阶段图，"
                          f"意象化，简洁构图，无人")
                    tasks.append((f"作物图·{cname}{_stage_zh[stage]}", cn,
                                  f"crop:{tid}_{ckey}_stage{stage}"))
        return tasks

    def _run_image_tasks(self, world: World, style: str, tasks: list,
                         progress_cb=None, cancel_check=None, cutout_bg: str = "") -> list[str]:
        """[拆分 2026-08-29] 世界生图任务执行段（重试/回写/失败收集；见
        generate_world_images docstring 契约）。返回 failed 标签列表。
        cutout_bg：[2026-09-25 用户指示] 人像/怪物/宠物图纯色背景抠透明的背景 tag
        （空串=不抠）；只作用于这几类 tag 模式目标，banner/地点背景/物品与各类图标不抠。"""
        total = len(tasks)
        failed: list[str] = []

        captured_positive: list[str] = []

        def _gen(kind: str, cn: str, eid: str) -> Optional[str]:
            """单任务出图（首跑与重试共用同一分支表，防两处分支漂移）。"""
            if kind == "monster":
                return self._ensure_image_file(cn, style, f"combat_{eid}.png", cancel_check,
                                               cutout_bg=cutout_bg, full_body=True)
            if kind == "pet":
                return self._ensure_image_file(cn, style, f"pet_{eid}.png", cancel_check,
                                               cutout_bg=cutout_bg, full_body=True)
            if kind == "skill":
                return self._ensure_image_file(cn, style, f"skill_{eid}.png", cancel_check,
                                               natural=True)
            if kind in ("building", "furniture"):
                return self._ensure_image_file(cn, style, f"{kind}_{eid}.png", cancel_check,
                                               natural=True)
            if kind == "crop":
                return self._ensure_image_file(cn, style, f"crop_{eid}.png", cancel_check,
                                               natural=True)
            # 走到这里的只剩四类：banner/地点背景/物品图是无人场景 -> 全自然语言模式；
            # NPC 头像/玩家立绘是 tag 模式人像 -> 抠图家族（其余已在上面 return）。
            natural = kind in ("banner", "location", "item")
            npc = next((n for n in world.npcs if n.id == eid), None) if kind == "npc" else None
            fixed = str(getattr(npc, "appearance_tags", "") or "").strip() if npc else ""
            return self.generate_image(cn, style, cancel_check, natural=natural,
                                       cutout_bg=cutout_bg if kind in ("npc", "player") else "",
                                       full_body=kind in ("npc", "player"),
                                       positive_override=fixed,
                                       on_positive=captured_positive.append if npc and not fixed else None)
        for i, (label, cn, target) in enumerate(tasks):
            if cancel_check and cancel_check():
                break
            if progress_cb:
                progress_cb(i, total, label)
            kind, eid = target.split(":", 1)
            captured_positive.clear()
            fname = _gen(kind, cn, eid)
            # [!] 单张失败重试 1 次（未取消时）：首图排队/瞬时抖动常见，第二机会捞回多数。
            if not fname and not (cancel_check and cancel_check()):
                fname = _gen(kind, cn, eid)
            if fname:
                if kind == "banner" and eid == world.id:
                    world.banner = fname
                elif kind == "location":
                    for loc in world.locations:
                        if loc.id == eid:
                            loc.background = fname
                            break
                elif kind == "npc":
                    for npc in world.npcs:
                        if npc.id == eid:
                            npc.avatar = fname
                            if not npc.appearance_tags and captured_positive:
                                npc.appearance_tags = captured_positive[-1]
                            break
                elif kind == "item":
                    for item in world.items:
                        if item.id == eid:
                            item.icon = fname
                            break
                elif kind == "monster":
                    pass   # 缓存文件即存储（combat_{key}.png），无需回写 world 字段
                elif kind in ("pet", "skill"):
                    pass   # 同怪物口径：pet_/skill_{key}.png 缓存即存储
                elif kind == "player":
                    # [P5c] 玩家立绘 -> PlayerState.avatar
                    world.player.avatar = fname
            else:
                failed.append(label)
        # cancel 中断时不发"完成"，避免 UI 误显示 100%
        if progress_cb and not (cancel_check and cancel_check()):
            if failed:
                progress_cb(total, total, f"完成（{len(failed)} 张失败）")
            else:
                progress_cb(total, total, "完成")
        return failed

    def generate_world_images(
            self, world: World, preset: WorldSimPreset,
            progress_cb: Optional[Callable[[int, int, str], None]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[World, list[str]]:
        """按 preset 生图勾选项批量生成 banner/地点背景/NPC 头像/传说物品图。逐个生成，可取消。
        成功的回写 world.banner/location.background/npc.avatar/item.icon。失败不阻断（留空，UI 占位）。

        [!] 单张失败重试 1 次（ComfyUI 首图排队/瞬时抖动常见，给第二机会避免「生图成功却没存」）；
        仍失败则记入 failed 列表返回，由调用方告知用户（旧版静默吞失败，用户只见进度走满「完成」
        却有 NPC 头像为空，误以为没存下来——实际是单张生成失败被跳过）。
        返回 (world, failed_labels)。"""
        style = preset.image_style_prefix or ""
        tasks = self._world_image_tasks(world, preset)
        failed = self._run_image_tasks(world, style, tasks, progress_cb, cancel_check,
                                       cutout_bg=self._cutout_bg_tag(preset))
        return world, failed

    def regenerate_missing_images(self, world: World, preset: WorldSimPreset,
                                  progress_cb=None, cancel_check=None) -> tuple[int, list[str]]:
        """[遗留#5 2026-08-29] 补缺失图：只重生成「文件缺失」的图。

        DB 类（banner/location/npc/item/player——文件名存 world 字段）只保留
        字段为空或磁盘文件缺失的任务；缓存类（monster/pet/skill/building/
        furniture/crop）全量入单——_ensure_image_file 命中缓存即跳过，天然只补缺。
        与世界生成同管线（同提示词/同重试/同回写）。返回 (补图张数, 失败标签)。
        落盘由调用方负责。

        [2026-09-25] 抠图开启时额外做「存量补抠」：已有的人像/怪物/宠物图不重出图，
        只做一次幂等抠图（缓存类走 _ensure_image_file 命中分支，DB 类里的 npc/player
        头像在本函数过滤循环里补抠）。[!] 补抠发生在 filtered 组装阶段（进度回调之前），
        张数不计入返回值——UI 进度条在这段不动属预期（本地处理，单张百毫秒级）。
        """
        import os
        from src.config import paths
        img_dir = paths.world_images_dir()
        cutout_bg = self._cutout_bg_tag(preset)
        # [2026-09-25] 抠图开启时，**已有**的人像/怪物/宠物图也顺带补抠一次（幂等，
        # 见 image_cutout）：存量旧存档不必重出图就能升级成透明底。缓存类图由
        # _ensure_image_file 命中分支处理，这里只管 DB 类里的 npc/player 两张头像。

        def _missing(fn) -> bool:
            return not fn or not os.path.exists(os.path.join(img_dir, fn))

        def _retrofit_cutout(fn: str):
            """对已有图补抠（幂等；失败静默保留原图）。"""
            if not cutout_bg or not fn:
                return
            self._safe_cutout(os.path.join(img_dir, fn))

        filtered: list = []
        for label, cn, target in self._world_image_tasks(world, preset):
            kind, eid = target.split(":", 1)
            if kind in ("monster", "pet", "skill", "building", "furniture", "crop"):
                filtered.append((label, cn, target))
            elif kind == "banner":
                if _missing(world.banner):
                    filtered.append((label, cn, target))
            elif kind == "location":
                loc = next((l for l in world.locations if l.id == eid), None)
                if loc is not None and _missing(getattr(loc, "background", "")):
                    filtered.append((label, cn, target))
            elif kind == "npc":
                n = next((x for x in world.npcs if x.id == eid), None)
                if n is None:
                    continue
                if _missing(getattr(n, "avatar", "")):
                    filtered.append((label, cn, target))
                else:
                    _retrofit_cutout(str(getattr(n, "avatar", "") or ""))
            elif kind == "item":
                it = next((x for x in world.items if x.id == eid), None)
                if it is not None and _missing(getattr(it, "icon", "")):
                    filtered.append((label, cn, target))
            elif kind == "player":
                av = str(getattr(world.player, "avatar", "") or "")
                if _missing(av):
                    filtered.append((label, cn, target))
                else:
                    _retrofit_cutout(av)
        if not filtered:
            return 0, []
        style = preset.image_style_prefix or ""
        attempted = {"n": 0}

        def _count_progress(i, total, label):
            attempted["n"] = max(attempted["n"], i + 1)   # 每任务开跑前回调一次
            if progress_cb:
                progress_cb(i, total, label)

        failed = self._run_image_tasks(world, style, filtered,
                                       _count_progress, cancel_check, cutout_bg=cutout_bg)
        # [审查修复] done 按实际尝试数计：cancel 中断时未跑任务不算补上
        return attempted["n"] - len(failed), failed

    # ================================================================
    # ============ Phase 2 场景交互循环（每回合 2 次 LLM）============
    # ================================================================
    # 时序：玩家行动 -> settle_action（结算 LLM 出意图 JSON）-> apply_intent（引擎算结构）
    #       -> narrate_outcome（叙事 LLM 流式旁白）-> 落场景日志。
    # 首回合（scene.log 空）只走 narrate_intro 开场旁白——[修 2026-09-13] settle 初始
    # 选项调用已摘除（rev 44 移除选项功能后其 intent 无消费方）。
    # 架构铁律：LLM 出名称/语义，引擎算 id/校验/数值。P2 唯一结构化状态变更 =
    #   world.player.location_id（移动）+ world.tick_count/scene.tick（自增）。

    def _per_world(self, world: World, key: str, default, preset: WorldSimPreset):
        """读每世界覆盖旋钮：world.config_overlay 优先，回退 preset 字段。"""
        ov = world.config_overlay if isinstance(world.config_overlay, dict) else {}
        if key in ov:
            return ov[key]
        return getattr(preset, key, default)

    def _difficulty(self, world: World, preset: Optional[WorldSimPreset] = None) -> str:
        """取全局难度档（easy/normal/hard；config_overlay 优先回退 preset）。"""
        return self._per_world(world, "difficulty",
                               preset.difficulty if preset else "normal", preset)

    def _genre_text(self, world: World):
        """[P6] 取题材化字符串 bundle（货币/品级/属性/槽/商店类型/物品大类名）。

        读 world.config_overlay（build_world_from_skeleton 已写入全部题材化字段）；
        老 P5c 世界缺 P6 新字段时 GenreText 内部补西幻默认。无 world 时返回默认 bundle。
        """
        from src.models.world_sim_preset import GenreText
        ov = world.config_overlay if isinstance(world, World) and isinstance(world.config_overlay, dict) else {}
        return GenreText(ov)

    def _current_location(self, world: World) -> Optional[Location]:
        """取玩家当前地点；location_id 失效回退首个地点。"""
        for loc in world.locations:
            if loc.id == world.player.location_id:
                return loc
        return world.locations[0] if world.locations else None

    def _npcs_at(self, world: World, loc: Location) -> list[NPC]:
        """取指定地点在场 NPC（按 npc.location_id 匹配）。"""
        if not loc:
            return []
        return [n for n in world.npcs if n.location_id == loc.id]

    def _record_codex_kill(self, world: World, npc, cause: str) -> None:
        """[修 2026-09-05 用户指示] 图鉴击杀记录（普通战斗/快速战斗两路径统一）：
        击败名单 codex_defeated + 遇到名单 + 死亡信息 codex_deaths（名/天/tick/地点/死因，
        同 id 覆写留最后一次）。[!] 临时野怪（不在 world.npcs）不入册——uuid 每次全新
        会让怪物页计数永久漂移（守原口径）。[快速战斗图鉴缺失修复] 旧快速路径只记
        掉落物品不记击败名单，同一场击杀两路径图鉴口径不一致。
        """
        try:
            if not any(n.id == npc.id for n in world.npcs):
                return
            if npc.id not in world.player.codex_defeated:
                world.player.codex_defeated.append(npc.id)
            if npc.id not in world.player.codex_npcs:
                world.player.codex_npcs.append(npc.id)
            loc = next((l for l in world.locations if l.id == getattr(npc, "location_id", "")), None)
            world.player.codex_deaths[str(npc.id)] = {
                "name": str(npc.name or ""),
                "day": max(1, int(getattr(world, "day_count", 1) or 1)),
                "tick": max(0, int(getattr(world, "tick_count", 0) or 0)),
                "loc": str(loc.name) if loc is not None else "未知",
                "cause": str(cause or "被你击败"),
            }
        except Exception:
            pass

    def _revive_at_spawn(self, world: World) -> str:
        """[P10c] 玩家被击败但永久死亡关闭 -> 复活：HP 置 1 并送回出生地。

        出生地优先取 player.spawn_location_id（build_world_from_skeleton 写入），失效或空回退
        world.locations[0]（与 _current_location 同口径）。出生地一并标 discovered+explored
        （守出生地契约）。返回出生地名称（无可用地点返回空串，仅回血不传送）。
        """
        spawn_id = (getattr(world.player, "spawn_location_id", "") or "").strip()
        spawn_loc = next((l for l in world.locations if l.id == spawn_id), None)
        if spawn_loc is None and world.locations:
            spawn_loc = world.locations[0]
        world.player.hp = 1
        if spawn_loc is not None:
            world.player.location_id = spawn_loc.id
            # [P27] 复活回出生地：place_id 重置为出生地默认场所
            world.player.place_id = getattr(spawn_loc, "default_place_id", "") or ""
            spawn_loc.discovered = True
            spawn_loc.explored = True
            return spawn_loc.name
        return ""

    def _adjacent_locations(self, world: World, loc: Location) -> list[Location]:
        """取当前地点的相邻可达地点（按 connections id 解析）。"""
        if not loc:
            return []
        out = []
        for cid in loc.connections:
            for l in world.locations:
                if l.id == cid and l.id != loc.id and l not in out:
                    out.append(l)
        return out

    # ============ [P27] 二层地图：场所级位置/移动 helper ============
    def _current_place(self, world: World) -> Optional[Place]:
        """玩家当前场所：place_id 不空查之，否则回退所在 location.default_place_id。"""
        loc = self._current_location(world)
        if not loc or not getattr(loc, "places", None):
            return None
        pid = (getattr(world.player, "place_id", "") or "").strip()
        if pid:
            for p in loc.places:
                if isinstance(p, Place) and p.id == pid:
                    return p
        # 回退 default_place_id
        did = (getattr(loc, "default_place_id", "") or "").strip()
        if did:
            for p in loc.places:
                if isinstance(p, Place) and p.id == did:
                    return p
        return loc.places[0] if loc.places else None

    def _same_place_as_player(self, world: World, npc) -> bool:
        """[用户定稿 2026-08-28] 场所级同场判定：location 相同且（地点无场所 或 place 相同）。

        引擎结算面（交易/委托/送礼/领奖/同伴）从地点级收紧到场所级，与【在场 NPC】
        感知口径统一；无场所地点（野外/秘境内部）自动退化地点级，兼容不受影响。"""
        if getattr(npc, "location_id", "") != getattr(world.player, "location_id", ""):
            return False
        loc = self._current_location(world)
        if loc is None or not getattr(loc, "places", None):
            return True                      # 无场所化地点：地点级即同场
        cp = self._current_place(world)
        np_ = self._place_of(world, npc)
        return (cp is None) or (np_ is None) or (cp.id == np_.id)

    def _place_of(self, world: World, npc) -> Optional[Place]:
        """NPC 当前场所（同 _current_place 口径，按 npc.place_id 回退 location.default_place_id）。"""
        loc = next((l for l in world.locations if l.id == getattr(npc, "location_id", "")), None)
        if not loc or not getattr(loc, "places", None):
            return None
        pid = (getattr(npc, "place_id", "") or "").strip()
        if pid:
            for p in loc.places:
                if isinstance(p, Place) and p.id == pid:
                    return p
        did = (getattr(loc, "default_place_id", "") or "").strip()
        if did:
            for p in loc.places:
                if isinstance(p, Place) and p.id == did:
                    return p
        return loc.places[0] if loc.places else None

    def _npcs_at_place(self, world: World, loc: Location, place: Optional[Place]) -> list[NPC]:
        """场所级在场 NPC：place 为 None 时回退 _npcs_at（无场所化地点兼容）。

        [!] 场所级过滤 = npc.location_id==loc.id 且 npc 当前场所==place（place_id 不空匹配，
        空回退 default_place_id 匹配）。place.npc_ids 是冗余索引，此处按 location_id+place_id
        权威读（与 _npcs_at 按 location_id 读同口径）。
        """
        if not loc:
            return []
        if place is None:
            return self._npcs_at(world, loc)
        pid = place.id
        out = []
        for n in world.npcs:
            if n.location_id != loc.id:
                continue
            npid = (getattr(n, "place_id", "") or "").strip()
            if not npid:
                # 回退 default_place_id
                npid = (getattr(loc, "default_place_id", "") or "").strip()
            if npid == pid:
                out.append(n)
        return out

    def _adjacent_places(self, loc: Location, place: Optional[Place]) -> list[Place]:
        """场所连通图相邻（同地点内 place.connections 解析）。无场所化返回空。"""
        if not loc or not place or not getattr(loc, "places", None):
            return []
        out = []
        for cid in place.connections:
            for p in loc.places:
                if isinstance(p, Place) and p.id == cid and p.id != place.id and p not in out:
                    out.append(p)
        return out

    def _move_to_place(self, world: World, npc, to_place: Place, loc: Location) -> bool:
        """场所内移动：改 npc.place_id + 两场所 npc_ids 双向登记（仿 _move_npc_safe 口径）。

        [!] 不改 location_id（场所内移动地点不变）；place_id 是位置精度细化。
        返回是否实际移动（同场所返回 False）。
        """
        if not to_place or not loc:
            return False
        old_pid = (getattr(npc, "place_id", "") or "").strip()
        if not old_pid:
            old_pid = (getattr(loc, "default_place_id", "") or "").strip()
        if old_pid == to_place.id:
            return False
        npc.place_id = to_place.id
        # 双向登记 npc_ids（仅 NPC 有 id；玩家不进 place.npc_ids，那是 NPC 在场索引）
        nid = getattr(npc, "id", "")
        if nid:
            for p in loc.places:
                if isinstance(p, Place) and p.id == old_pid and nid in p.npc_ids:
                    p.npc_ids = [i for i in p.npc_ids if i != nid]
            if nid not in to_place.npc_ids:
                to_place.npc_ids.append(nid)
        return True

    def _resolve_place_by_name(self, loc: Location, name: str) -> Optional[Place]:
        """按场所名解析（当前地点内）。None=未找到。[P44] 收编进统一解析器
        （原精确名优先 + 最长子串口径不变，叠加归一化/编辑距离容错）。"""
        if not loc or not getattr(loc, "places", None) or not name:
            return None
        places = [p for p in loc.places if isinstance(p, Place)]
        hit = nrs.resolve_name(name, [p.name for p in places])
        return next((p for p in places if p.name == hit), None) if hit else None

    def _fmt_npc_detail_line(self, n, world: World, preset,
                             combat_system: str, with_memory: bool,
                             query_hint: str = "") -> str:
        """[P23] NPC 详细信息一行（性格/目标/腔调/关系/小传/交情/经商/等级HP/可选记忆）。

        【在场 NPC】与【NPC 一览】共用：在场者 with_memory=True（含 recall），离场者
        with_memory=False（跳过 recall，避免全量 NPC 各调一次 embedding API 致 40 次/回合）。
        [P23] query_hint 附加到 recall query（NPC 名 + 最近玩家行动），让 embedding 召回
        命中上下文相关记忆（如玩家刚交易完，query 带交易语义 -> 召回交易记忆）。
        """
        line = f"  - {n.name}（{n.role}）：性格{n.personality}；目标{n.goal}"
        # [玩家印象 2026-09-06] 该 NPC 眼中的玩家（各自主观，深浅不同）——settle/narrate
        # 台词自然反映（仇人嘴里你和恩人嘴里你不是同一个人）
        try:
            _imp_ln = soc.impression_line(n)
            if _imp_ln:
                line += f"；{_imp_ln}"
        except Exception:
            pass
        # [A+B 2026-09-06] 此刻念头/今日目标（日计划批产出，随计划每日刷新）：给
        # settle/narrate 动机锚——旁白据此外化为言行神态（玩家不读心，看言行）；
        # 短期目标要角 tick 级 current_goal 优先。预算固定每行 +~30 字。
        _th = (getattr(n, "current_thought", "") or "").strip()
        if _th:
            line += f"；念头：{_th[:60]}"
        _dg = (getattr(n, "current_goal", "") or "").strip() or (getattr(n, "daily_goal", "") or "").strip()
        if _dg:
            line += f"；今日目标：{_dg[:60]}"
        if getattr(n, "personality_drift", ""):
            line += f"；近变：{n.personality_drift}"
        # [剧情线 P46] 该 NPC 卷入的线一行（读时派生，预算固定 <=48 字；给旁白动机锚，
        # 玩家从 NPC 言行感知世界在动。零持久化——不许写 daily_goal/current_goal）
        try:
            _arc_ln = sae.arc_line_for_npc(world, getattr(n, "id", ""))
            if _arc_ln:
                line += f"；{_arc_ln}"
        except Exception:
            pass
        if getattr(n, "speech_style", ""):
            line += f"；说话风格（泛义倾向，勿逐字复读）：{n.speech_style}"
        rels = getattr(n, "relationships", None) or []
        if isinstance(rels, list) and rels:
            rel_parts = []
            for r in rels:
                if isinstance(r, dict):
                    tn = str(r.get("target_name", "") or "").strip()
                    rl = str(r.get("relation", "") or "").strip()
                    if tn and rl:
                        rel_parts.append(f"{tn}({rl})")
            if rel_parts:
                # [2026-08-28 用户指示] cap 全量：关系不再 [:3] 截断
                line += "；关系：" + "、".join(rel_parts)
        notes = getattr(n, "notes", "") or ""
        if notes:
            # [2026-08-28 用户指示] cap 全量：小传不再 [:60] 截断
            line += f"；小传：{notes}"
        aff = int(getattr(n, "affinity", 0) or 0)
        comp_tag = "，正与玩家同行" if nre.is_companion(world, n) else ""
        # [P26a] 关系阶段称谓（义兄/道侣/夫君 等，题材化；空=无关系或 friend 阶段不重复标）
        rel_stage = reng.stage_of(world, n.id)
        rel_tag = ""
        if rel_stage in ("sworn", "sweetheart", "spouse"):
            rel_term = reng.relation_term(world, n, rel_stage)
            rel_tag = f"，是玩家的{rel_term}" if rel_term else ""
        # 结义/恋人/配偶已超越普通好友，rel_tag 非空时压制 friend_tag 避免双标冗余
        friend_tag = "" if rel_tag else ("，是玩家的好友" if nre.is_friend(world, n) else "")
        line += f"；与玩家交情：{nre.affinity_level(aff)}({aff}){friend_tag}{rel_tag}{comp_tag}"
        if n.is_merchant:
            shop = self.shop_for_npc(world, n.id)
            shop_note = f" | 经商:{shop.name}" if shop else " | 经商"
            if shop and shop.stock:
                shop_note += f"（{len(shop.stock)}件商品）"
            line += shop_note
        if combat_system == "crpg" and n.level > 0:
            line += f" | 等级:{n.level} HP:{n.hp}/{n.hp_max} 敌对:{'是' if n.hostile else '否'}"
            if n.hp <= 0:
                line += "（已倒下）"
        # [修 2026-09-10] 已倒下标注与 combat_system/level 解耦：叙事档（非 crpg）此前
        # 尸体在【在场 NPC】里无任何死亡标识，LLM 会把死人当在场活人写（OOC 通道）。
        # UI 契约是「尸体保留 + 已倒下标注」（world_scene_tab.py:1147 用户指示），此处补齐叙事侧。
        # [!] 死亡判定用 alive 而非 hp：叙事档 _init_combat_stats 早退，NPC.hp 保留模型默认 0，
        # 按 hp<=0 判定会把全场活人标成尸体（开场首回合、未跑 tick 时必现）。
        if not getattr(n, "alive", True) and not (combat_system == "crpg" and n.level > 0):
            line += "（已倒下）"
        if with_memory and preset is not None and getattr(preset, "npc_memory_enabled", True):
            try:
                query = (n.name + " " + query_hint).strip() if query_hint else n.name
                mem = self.npc_memory().recall(n, world, query, preset)
            except Exception:
                mem = ""
            if mem:
                # [2026-08-28 用户指示] 记忆召回不再 [:200] 硬截断——长度控制在记忆整理
                # 提示词侧约束（总结输出 <=2048 字，见 DEFAULT_NPC_MEMORY_*_PROMPT）。
                line += f"\n    记忆：{mem}"
        return line

    def _build_scene_context(self, world: World, scene: SceneLog,
                             preset: Optional[WorldSimPreset] = None,
                             mode: str = "full") -> str:
        """拼场景上下文摘要（送结算/叙事 LLM 的 user 消息主体）。

        [P15a2] mode="full"（叙事/开场用，信息全量）| "compact"（结算用，瘦身：[P17] 玩家数值/
        装备/背包/当前任务整块去掉——纯计数与标题列表对结算判意图无信息量，伤害/背包/任务进度
        全在引擎纯 Python 结算；[P22] 世界书两模式都保留，作意图判定的世界观锚点；属性特长/
        天赋本就 full-only；最近经历 2 条）。
        [P15a3] 前缀缓存友好排序（[2026-08-28 重排] 按变动频率升序）：永久稳定块（世界设定/
        题材基调/世界书/玩家身世）-> append-only 历史块（前情摘要+最近经历，跨回合只增不改，
        前缀缓存最大头）-> 罕变块（特长/天赋/声望/节日/宠物/地点列表）-> 游戏态块（地点/场所/
        秘境/Boss/资源/交易/NPC）-> 每回合必变块殿后（坊间热议/编年史/时间天气，失效范围最小）。
        [!] 缓存命中只发生在同角色跨回合调用间（settle(N)->settle(N+1)、narrate(N)->narrate(N+1)，
        两者 system 提示词不同，互相不共享前缀）；历史块前置让「稳定区+全部未压缩历史」
        成为可命中的长前缀（历史恰是上下文里最大的块）。
        """
        compact = (mode == "compact")
        loc = self._current_location(world)
        stable: list[str] = []
        volatile: list[str] = []
        # ---- 稳定块（回合间几乎不变）----
        stable.append(f"【世界设定】{world.premise}")
        if world.genre_tags:
            stable.append(f"题材：{', '.join(world.genre_tags)}；基调：{world.tone or '默认'}")
        # 世界书前 5 条优先级最高的（[P22] compact 也保留：世界书是意图判定的世界观锚点，
        # 数条短词条 token 开销极小；full 叙事保留全文）
        lore_line = _fmt_world_lore(world)
        if lore_line:
            stable.append(lore_line)
        # [修 2026-10-02 真机 OOC·货币串味] 货币硬规则行（settle/narrate 共用本上下文）：
        # 末日档旁白曾照世界书错误词条写「瓶盖一枚都没剩」——引擎账本只有 GenreText 货币。
        try:
            _cur_name = self._genre_text(world).currency
        except Exception:
            _cur_name = ""
        if _cur_name:
            stable.append(f"【货币口径】本世界货币是「{_cur_name}」——一切钱款/物价/赏金"
                          f"只用「{_cur_name}」表述，禁止其他货币名。")
        # 玩家（姓名/职业/身世稳定；回合数挪到易变区【时间天气】行）
        # [2026-08-23 用户卡绑定] 绑了用户卡则注入姓名/人设：NPC 从此知道玩家是谁
        # （否则只能「那个男人/那个白衣年轻人」模糊指代，NPC 记忆全是泛称）；
        # 未绑定退回无名口径（老世界行为不变）。块在稳定区，回合间不变前缀缓存友好。
        p = world.player
        _buser = self._bound_user(world)
        _p_head = f"职业：{p.class_name or '未定'}；身世：{p.background or '未定'}"
        if _buser is not None and (getattr(_buser, "name", "") or "").strip():
            _p_head = f"姓名：{_buser.name.strip()}；" + _p_head
        stable.append(f"【玩家】{_p_head}")
        if _buser is not None and (getattr(_buser, "description", "") or "").strip():
            stable.append(
                f"【玩家人设】{_buser.description.strip()}"
                "（这是玩家角色的外貌与人物设定；NPC 应按姓名指代/称呼玩家，"
                "描写玩家外貌时以此为据，勿另编形象）")
        # ---- [2026-08-28 重排] append-only 历史块前置（前缀缓存最大头）----
        # 前情摘要 + 最近经历跨回合只增不改（摘要压缩才重写、压缩点约每 8 回合一次），
        # 紧跟稳定区让「稳定区+全部未压缩历史」成为同角色跨回合调用的可命中长前缀
        # （历史是上下文里最大的块，旧序把它排在尾部、前面隔着每回合变的 NPC/交易块，
        # 稳定头部完全吃不到缓存）。历史在场 != 现在在场的警示随块前置照常生效。
        if scene is not None and getattr(scene, "summary", ""):
            volatile.append(f"【前情摘要】（此前经历的概括，据实织入，不要矛盾）\n{scene.summary}")
        # [P23] 历史注入：full/compact 都注入全部未压缩条目（压缩后 scene.log 即未总结
        # 全集，稳态峰值约 threshold+1 条）。settle 判意图也需完整剧情脉络避免 OOC
        # （如玩家说「把刚才换的罐头给林岚」，settle 需看到交易历史才知道「罐头」指什么）。
        if scene is None:
            recent = []
        else:
            recent = list(scene.log)
        if recent:
            log_lines = []
            for e in recent:
                tag = {"player": "玩家", "narrator": "旁白", "system": "系统"}.get(e.role, e.role)
                # [P21] 清洗旧版漏剥的头部 [NPC] 命令行——曾整行落盘进历史，被 LLM 当成
                # 已执行的移动/指令，导致后续回合不再为离场 NPC 下移动命令。
                log_lines.append(f"  [{tag}] {scrub_npc_cmd_head(e.content)}")
            # [P10c-fix] 防历史污染：旧旁白可能描写过当前并不在场的人物（数据/叙事曾脱钩），
            # 明确「历史在场 ≠ 现在在场」——当前现场以【在场 NPC】为唯一准绳。
            volatile.append("【最近经历】（历史记录仅供参考；其中出现的 NPC 若不在【在场 NPC】"
                            "列表，即已离场或从未到场，本回合不得视作在现场）\n" + "\n".join(log_lines))
        # ---- 易变块（按变动频率升序：罕变 -> 游戏态 -> 每回合必变殿后）----
        # [P23 用户指示] 主线任务/玩家数值/玩家装备/玩家背包四块去掉：这些是 Python 结算层
        # 用的（伤害/背包/消耗品/任务进度全在引擎纯 Python 结算 + UI 钩子），对 LLM 判意图/
        # 叙事影响不大，徒增 token + 随状态变损缓存。属性特长/玩家天赋保留（奇遇/检定/天赋
        # 共鸣的核心依据，去掉会严重 OOC；且在 volatile 区，前缀缓存命中本由 stable 区决定，
        # 它们变不变对命中无影响）。
        combat_system = self._per_world(world, "combat_system", preset.combat_system, preset) if preset else "crpg"
        # [P7d4] 属性特长检定（让叙事 LLM 据玩家属性触发奇遇/检定选项：属性够则可推开巨石/解读古卷等）
        # [2026-08-28 用户指示] compact 门控去掉：奇遇/检定选项由 settle 生成，结算侧必须
        # 看到特长才能判「有资格检定」；旧 compact 省略导致结算不知道玩家会不会开巨石。
        gt2 = self._genre_text(world) if hasattr(self, "_genre_text") else None
        sdn2 = gt2 if gt2 is not None else {}
        _nm = lambda k: (sdn2.stat(k) if gt2 is not None else {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"}.get(k, k))
        talents = []
        for key, ability in (
                ("str", "可徒手破障/推开巨石/蛮力"),
                ("dex", "可攀爬绝壁/潜行/巧手"),
                ("int", "可解读古卷/识破机关/参悟"),
                ("vit", "可涉毒/长途跋涉/硬抗"),
                ("luk", "可触发奇遇/避险/机缘")):
            value = te.effective_stat(p, key)
            if value >= 15:
                talents.append(f"{_nm(key)}({value}) {ability}")
        if talents:
            volatile.append("【属性特长】" + "；".join(talents) +
                            "（玩家具备这些特长时，可在旁白/选项中安排对应的奇遇、检定或专属互动；属性不足则描述受阻）")
        # [P9] 玩家天赋块（天赋剧情联动）：让叙事/结算 LLM 据玩家身怀天赋安排专属奇遇变体、
        # 传承共鸣、对话互动（雷天灵根遇雷系功法机缘、火灵之之体入火焰试炼变简单等）。
        # [2026-08-28 用户指示] compact 门控去掉（同上：奇遇变体是结算侧的活）。
        _tal_lines = te.describe_talents(
            [t for t in (p.talents or []) if isinstance(t, dict)])
        if _tal_lines:
            volatile.append(
                "【玩家天赋】" + "；".join(_tal_lines) +
                "（玩家身怀这些天赋：遇同元素/同题材的功法、宝物、试炼、高人时，"
                "优先安排天赋共鸣的专属奇遇变体或对话——如雷系天赋遇雷法传承概率提升、"
                "参悟更容易；无天赋相关时照常叙事，不要生硬提及）")
        # ---- [2026-08-28 用户指示] 恢复玩家状态四块（游戏态段：随操作频繁变化）----
        # 旧 [P17/P23] 精简批次把数值/装备/背包/任务四块整体删除（口径「引擎算数值，
        # LLM 不需要」）；实际损伤扮演质量——旁白写不出手持武器/身受重伤/身怀何物，
        # settle 判 use_item/quest/sell 意图也缺权威来源。现恢复：
        # 数值/装备 full-only（纯数值对结算判意图信息量低；HP 伤情已改走【伤情】动态块，见下）；
        # 背包/任务 compact/full 都给（判意图权威来源 + 叙事素材）。物品信息含 desc/等级
        # （[2026-08-28 用户指示] 物品不再是裸名字，LLM 需知道是什么/干什么用的）。
        _p_items = {it.id: it for it in (getattr(world, "items", []) or [])}

        def _fmt_item(iid: str) -> str:
            it = _p_items.get(iid)
            if it is None:
                return iid
            seg = f"{it.name}[{it.rarity}]"
            if getattr(it, "level", 0):
                seg += f" Lv{it.level}"
            if not getattr(it, "identified", True):
                seg += "（未鉴定）"
            d = (getattr(it, "desc", "") or "").strip()
            if d:
                seg += f"：{d}"
            return seg
        if not compact:
            gt3 = self._genre_text(world) if hasattr(self, "_genre_text") else None
            _sn = lambda k: (gt3.stat(k) if gt3 is not None else {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"}.get(k, k))
            # [伤情动态化 2026-09-07 用户指示] HP 不常驻注入（裸数字让 LLM 沿用历史重伤描写）：
            # 玩家伤势的唯一信号 = 气血不足五成时注入的【伤情】块，narrate 规则14 以此块为
            # 唯一依据（无块不写伤；最近经历/前情摘要/结算要点的旧伤不延续）。
            volatile.append(
                f"【玩家状态】Lv{p.level} MP {p.mp}/{p.mp_max} "
                f"{_sn('str')}{te.effective_stat(p, 'str')} {_sn('dex')}{te.effective_stat(p, 'dex')} "
                f"{_sn('int')}{te.effective_stat(p, 'int')} {_sn('vit')}{te.effective_stat(p, 'vit')} "
                f"{_sn('luk')}{te.effective_stat(p, 'luk')}")
            if p.hp * 2 < p.hp_max:
                volatile.append(
                    "【伤情】玩家气血不足五成，伤势不轻：动作与神态须体现伤患"
                    "（步履迟缓/牵动伤处/呼吸滞重）；濒死时行动明显受限，旁人可察觉其虚弱。")
        if not compact and (p.equipped or {}):
            eq_parts = [f"{slot}={_fmt_item(iid)}" for slot, iid in p.equipped.items()]
            if eq_parts:
                volatile.append("【玩家装备】" + "；".join(eq_parts))
        inv_parts = [_fmt_item(iid) for iid in (p.inventory or [])]
        volatile.append("【玩家背包】" + ("；".join(inv_parts) if inv_parts else "无"))
        _act_q = [q for q in (world.quests or []) if q.status == "active"]
        if _act_q:
            q_parts = []
            for q in _act_q:
                obj_desc = ""
                obs = getattr(q, "objectives", None) or []
                if obs:
                    o0 = obs[0] if isinstance(obs[0], dict) else {}
                    od = str(o0.get("desc", "") or "").strip()
                    if od:
                        obj_desc = f"（当前：{od}）"
                q_parts.append(f"{q.title}[{getattr(q, 'chain', '') or 'side'}]{obj_desc}")
            volatile.append("【当前任务】（进行中的任务，选项与旁白可自然推进）" + "；".join(q_parts))
        # [P15b2] 势力声望头衔（每 25 点一档；让结算/叙事 LLM 把握各势力对玩家的态度）
        rep_parts = []
        for fid, rv in (p.reputation or {}).items():
            fac_r = next((f for f in world.factions if f.id == fid), None)
            if fac_r is None or int(rv) == 0:
                continue
            rep_parts.append(f"{fac_r.name}:{reputation_title(rv)}({rv})")
        if rep_parts:
            volatile.append("【势力声望】（玩家在各势力的地位）" + "、".join(rep_parts))
        # [P7k5] 时间天气（叙事注入：让 LLM 据昼夜/天气描写氛围）——[2026-08-28 重排]
        # 行内含「回合N」每回合必变，已殿后到上下文末尾（与坊间热议/编年史同段），
        # 防止它切断后面罕变块（节日/地点列表/特长声望）的缓存前缀。
        # [P24d] 宠物块：出战跟随的宠物是叙事素材——旁白自然带出宠物言行。
        # [2026-08-28 用户指示] compact 门控去掉（settle 判「投喂/指挥宠物」意图也需要）。
        ap = pex.active_pet(world)
        if ap is not None:
            sp = pex.find_species(ap.species) or {}
            pl = pex.passive_label(world, ap)
            pet_line = (f"【宠物】「{ap.name}」（{sp.get('name', ap.species) or '未知种族'}）"
                        f"正跟随玩家：Lv.{ap.level}、亲和 {ap.affinity}/100"
                        + (f"，被动 {pl}" if pl else ""))
            others = [p.name for p in world.pets if isinstance(p, Pet) and p.id != ap.id]
            if others:
                pet_line += f"（另有{'、'.join(others)}在后方休整）"
            volatile.append(pet_line)
        # [P18] 全量地点参考表（命令行协议的地名权威来源，full/compact 同注入）：LLM 只看到
        # 【当前地点】+【相邻地点】时自造「柳家大宅门前」这类不存在的地名，命令被引擎忽略；
        # 全表 + 「★当前」标记让到场/离场命令有真实名可写。未发现地点标记防玩家视角剧透。
        loc_lines = []
        for l in world.locations:
            cur_mark = "★当前 " if (loc is not None and l.id == loc.id) else ""
            disc_mark = "" if getattr(l, "discovered", True) else "（未发现，玩家尚不知晓）"
            loc_lines.append(f"  - {l.name}{disc_mark}：危险度{getattr(l, 'danger', 0) or 0} {cur_mark}")
        volatile.append("【地点列表】（全量真实地点名，命令行只可用这里的名字，勿加「门前/郊外」等自造后缀）\n"
                        + "\n".join(loc_lines))
        # 当前地点
        if loc:
            fac = next((f.name for f in world.factions if f.id == loc.faction_id), "无")
            volatile.append(
                f"【当前地点】{loc.name}（区域：{loc.region or '未知'}；控制势力：{fac}；危险度：{loc.danger}）\n{loc.desc}"
            )
            # [P27] 二层地图：当前场所 + 场所列表 + 相邻场所（命令行场所名权威来源）。
            # places 为空（无场所化/秘境内部）则不注入场所块，退化单层兼容。
            cur_place = self._current_place(world)
            if cur_place is not None and getattr(loc, "places", None):
                volatile.append(
                    f"【当前场所】{cur_place.name}（类型：{cur_place.type or '未知'}）"
                    + (f"：{cur_place.desc}" if cur_place.desc else "")
                )
                place_lines = []
                for p in loc.places:
                    if not isinstance(p, Place):
                        continue
                    pm = "★当前 " if p.id == cur_place.id else ""
                    place_lines.append(f"  - {p.name}（{p.type or '场所'}）{pm}")
                volatile.append("【场所列表】（当前地点内场所，命令行可写「地点名/场所名」或纯场所名移动）\n"
                                + "\n".join(place_lines))
                # [用户定稿 2026-08-28] 地点内场所自由到达：全量列出本地点场所
                # （不再按连通图过滤；跨地点移动仍以【相邻地点】为准）
                other_places = [x for x in loc.places
                                if isinstance(x, object) and getattr(x, "id", "") and x.id != cur_place.id]
                if other_places:
                    volatile.append("【地点内场所】（当前地点的其他场所，可自由前往）"
                                    + "、".join(x.name for x in other_places))
                else:
                    volatile.append("【地点内场所】无")
            # [P25a] 秘境块：内部 -> 层/房进度 + 操作指引；入口 -> 提示一句。
            # [2026-08-28 用户指示] 入口提示的 compact 门控去掉（settle 判「进秘境探索」意图需要）。
            dg_in = dge.interior_dungeon(world, loc)
            if dg_in is not None:
                volatile.append(dge.scene_block(world, dg_in))
            else:
                dg_ent = dge.dungeon_at(world, loc)
                if dg_ent is not None:
                    if dg_ent.status == "open":
                        volatile.append(f"【秘境】此地藏有秘境「{dg_ent.name}」的入口"
                                        "（玩家可从功能按钮「秘境」进入探索）")
                    else:
                        left = max(0, int(dg_ent.cooldown_until_day or 0) - int(world.day_count or 1))
                        volatile.append(f"【秘境】此地秘境「{dg_ent.name}」的入口被封印着"
                                        f"（约 {left} 天后松动）")
            # [P25d] 世界 Boss 块（full/compact 都注入：settle 判「讨伐」意图 + 旁白感知威胁）。
            # 当前地点有未击败在窗 Boss 时提示其名/形态进度/窗口倒计时 + 按钮指引。
            wb_here = wbe.boss_at(world, loc)
            if wb_here is not None:
                volatile.append(wbe.scene_block(world, wb_here))
            # [P24a] 住宅块：宅名/档位/家具/陈列，旁白自然感知「回到家」。
            # [2026-08-28 用户指示] compact 门控去掉（settle 判「回家/取物」意图需要）。
            from collections import Counter as _Counter

            def _stock_line(ids):
                counts = _Counter(str(i) for i in (ids or []))
                return "、".join(
                    f"{getattr(_p_items.get(iid), 'name', iid)} x{count}"
                    for iid, count in counts.items())

            home = he.player_home_at(world, loc)
            if home is not None:
                item_by_id = {it.id: it for it in world.items}
                furn_names = [str(f.get("name")) for f in (home.furniture or [])
                              if isinstance(f, dict) and f.get("name")]
                shelf_names = [item_by_id[i].name for i in home.display_shelf if i in item_by_id]
                home_line = (f"【住宅】玩家在本地自有{he.tier_name(world, home.tier)}"
                             f"「{home.name}」：{home.desc or '安身之所'}")
                if furn_names:
                    seen: set = set()
                    uniq = [x for x in furn_names if not (x in seen or seen.add(x))]
                    home_line += f"；家有：{'、'.join(uniq)}"
                if shelf_names:
                    home_line += f"；陈列着：{'、'.join(shelf_names)}"
                _stash_cap = he.stash_capacity(home)
                if _stash_cap:
                    home_line += f"；仓库 {len(home.stash)}/{_stash_cap} 件"
                    if home.stash:
                        home_line += f"；库存：{_stock_line(home.stash)}"
                    home_line += "（私人库存；存取须在住宅面板操作，旁人未必知晓）"
                volatile.append(home_line)
            # 玩家已置下的据点经营进度进入结算与叙事上下文：只给当前地点的
            # 权威账本，叙事可回应「掌柜做到哪了」，但不得凭文字虚构出货。
            domain = de.player_domain_at(world, loc)
            if domain is not None:
                _dc = de.warehouse_cap(getattr(loc, "settlement_size", "") or "")
                volatile.append(f"【自有据点】「{domain.name}」；仓库 "
                                f"{len(domain.warehouse or [])}/{_dc} 件；"
                                f"职员 {len(de.roster_of(domain))} 人"
                                "（生产订单由据点面板发布，每日结算决定真实出货）")
                if domain.warehouse:
                    volatile.append("【据点仓库库存】" + _stock_line(domain.warehouse))
                _orders = [o for o in de.order_view(world, domain)
                           if o.get("state") in ("open", "stalled")]
                if _orders:
                    _dest = {"shelf": "商铺货架", "warehouse": "据点仓库", "home": "住宅仓库"}
                    _rows = []
                    for o in _orders[:3]:
                        _row = (f"{o.get('out_name', '货品')} {o.get('progress_txt', '0/?')}"
                                f"→{_dest.get(o.get('ship'), '仓库')}"
                                f"（{'停工待料' if o.get('state') == 'stalled' else '生产中'}")
                        if o.get("lacks"):
                            _row += f"；缺 {'、'.join(o['lacks'][:3])}"
                        if o.get("blocked"):
                            _row += f"；{o['blocked']}"
                        _rows.append(_row)
                    volatile.append("【据点产线】" + "；".join(_rows))
            # [P18][P23] 全量 NPC 参考表（存活 NPC 及所在地）：命令行的姓名权威来源。
            # [P23] 离场 NPC 注入详细信息（性格/腔调/关系/小传/交情/等级HP，无记忆——全量
            # recall 会 40 次/回合 embedding 过慢）：增强旁白对剧情与语气的理解。在场 NPC
            # 排除（其详情由【在场 NPC】块含记忆承载，避免重复注入）。地点名附在行尾供命令行抄写。
            # [P27] 同地点不同场所的 NPC 也列入（所在标「地点/场所」）——旧逻辑只列异地 NPC，
            # 玩家在悦来客栈时同在落霞镇的王铁匠会从【NPC 一览】与【在场 NPC】两个名单同时消失，
            # 结算 LLM 无法按名解析「把王铁匠叫来」。名字解析闭环比省这点 token 更重要。
            npcs = self._npcs_at_place(world, loc, cur_place)  # 在场 NPC（[P27] 场所级过滤，无场所回退地点级）
            in_field_ids = {n.id for n in npcs}
            all_npc_lines = []
            for n in world.npcs:
                if not n.alive or n.id in in_field_ids:
                    continue
                nloc = next((l2 for l2 in world.locations if l2.id == n.location_id), None)
                detail = self._fmt_npc_detail_line(n, world, preset, combat_system, with_memory=False)
                where = nloc.name if nloc else "未知地点"
                if nloc is not None and nloc.id == loc.id:
                    n_place = self._place_of(world, n)
                    if n_place is not None:
                        where = f"{nloc.name}/{n_place.name}"
                all_npc_lines.append(f"{detail}；所在：{where}")
            if all_npc_lines:
                volatile.append("【NPC 一览】（不在当前场所的存活 NPC 及其详情与所在地，命令行只可用这里的姓名）\n"
                                + "\n".join(all_npc_lines))
            else:
                volatile.append("【NPC 一览】无")
            if npcs:
                # [P6c][P23] 在场 NPC 详情 + 记忆召回（with_memory=True，让 NPC「记得」与玩家过往）
                # [P23] recall query 附最近玩家行动，让 embedding 召回命中上下文相关记忆
                _recent_player = ""
                if scene is not None:
                    for e in reversed(scene.log):
                        if e.role == "player" and (e.content or "").strip():
                            _recent_player = e.content.strip()[:40]
                            break
                npc_lines = [self._fmt_npc_detail_line(n, world, preset, combat_system, with_memory=True,
                                                        query_hint=_recent_player)
                             for n in npcs]
                volatile.append("【在场 NPC】\n" + "\n".join(npc_lines))
            else:
                volatile.append("【在场 NPC】无")
            # [④ 2026-08-30] 在场 NPC 所知传闻（每 NPC 所知不同 -> 不同 NPC 说不同的话 + 防 OOC）
            _rumor_lines = []
            for _n in npcs:
                if not getattr(_n, "alive", True):
                    continue      # [修 2026-09-10] 死者不参与「所知传闻」（尸体不说话）
                _rl = rme.rumor_lines_for(world, _n, rme.RUMOR_PER_NPC)
                if _rl:
                    _rumor_lines.append(f"  - {_n.name} 知：{'；'.join(_rl)}")
            if _rumor_lines:
                volatile.append("【在场 NPC 所知传闻】（各 NPC 私下知道的市井传闻；"
                                "NPC 只能提起自己知道的条目，不知道的不得说）\n"
                                + "\n".join(_rumor_lines))
            # 相邻地点（移动目标候选）
            adj = self._adjacent_locations(world, loc)
            if adj:
                volatile.append("【相邻地点】" + "、".join(f"{l.name}（危险{l.danger}）" for l in adj))
            else:
                volatile.append("【相邻地点】无（此地似乎与外界隔绝）")
            # [P7g] 可采集资源点（让叙事 LLM 知道此地有什么可采，可织入旁白/选项）
            if getattr(preset, "gathering_enabled", True) and loc.resource_nodes:
                rn_lines = []
                for rn in loc.resource_nodes:
                    status = ""
                    if rn.richness <= 0:
                        status = "（已枯竭" + ("，再生中" if rn.regenerates else "，耗尽") + "）"
                    elif rn.cooldown_tick > world.tick_count:
                        status = f"（冷却中，约{rn.cooldown_tick - world.tick_count}回合后可采）"
                    rn_lines.append(f"  - {rn.name}（{rn.type}，丰度{rn.richness}/100）：{rn.desc}{status}")
                if rn_lines:
                    volatile.append("【本地点可采集资源】\n" + "\n".join(rn_lines))
            # [P28] 交易块：玩家金币 + 本地点商人货架 + 在场 NPC 随身物品。
            # [2026-08-28 用户指示] 无条件注入金币行（旧「无商店则整块省略」让金币数
            # 时有时无）；玩家背包已前移为独立【玩家背包】块（含 desc/等级），此处不重复。
            # 物品名是 trade intent 的权威来源（仿【地点列表】for move），减少 LLM 从
            # 【最近经历】抄旁白编造物品名。compact/full 都注入（交易判意图核心信息）。
            shops_here = self.shops_at_location(world, loc.id)
            # [用户定稿 2026-08-28] barter 随身清单收紧到场所级（与在场口径一致）；
            # 货架仍地点级（[游商 2026-09-08] 商店挂地点常驻经营，玩家在店所在地点
            # 即可成交，见 _resolve_trade；旧档真商人仍须其本人在场）。
            _cur_place = self._current_place(world)
            barter_npcs = [n for n in self._npcs_at_place(world, loc, _cur_place)
                           if (n.inventory or []) and getattr(n, "alive", True)
                           and not getattr(n, "hostile", False)]
            if shops_here or barter_npcs:
                item_by_id = {it.id: it for it in (getattr(world, "items", []) or [])}
                rep_of = lambda n: int(world.player.reputation.get(getattr(n, "faction_id", "") or "", 0) or 0)
                pace = tre.world_pace(world)
                # [审核修复 2026-09-13] 币种走 GenreText（§23：绝不硬编码「金币」）
                _gt_cur = self._genre_text(world).currency
                tlines = [f"你的{_gt_cur}：{int(getattr(world.player, 'gold', 0) or 0)}"]
                # 本地点商人货架（买入价 + 库存 + 稀有度 + 描述，buy 的权威来源）
                # [2026-08-28 用户指示] 货架 cap8 取消（全量注入）+ 物品带 desc/等级。
                # [游商 2026-09-08] 货架署名游商（「{店名}·{称谓}」，常驻在场）——
                # 旧档已绑真商人的店仍署真商人名（后者可死亡/离场，settle 按需判在场）。
                for shop in shops_here:
                    # [修 2026-09-10] 商人解析走 _shop_merchant（= 结算/署名同源，含 shop_id
                    # 反向挂载回退），原来只按 merchant_npc_id 精确查 -> 反向挂载的真商人店
                    # 注入价会漏 trust 因子（显示价 ≠ 实收价）。
                    merchant = self._shop_merchant(world, shop)
                    if merchant is not None:
                        mname = getattr(merchant, "name", "") or shop.name or "商人"
                    else:
                        mname = trade_vendor_name(world, shop)
                    # [P45(1)] 注入价含治下/夜间 markup（与结算口径一致，防旁白报错价）
                    terr_buy, _, _ = tre.territory_price_factors(world, shop, world.player)
                    night_m = tre.NIGHT_MARKUP if not tre.shop_open(world, shop) else 1.0
                    # [修 2026-09-10] 注入价还须含商人对玩家的印象信任因子（>=50 折 0.95 /
                    # <=-50 溢 1.1，与 tre.buy 结算同源）——否则旁白/横幅显示价 ≠ 实收价。
                    _mimp = getattr(merchant, "player_impression", None) if merchant else None
                    _mtrust = int(_mimp.get("trust", 0) or 0) if isinstance(_mimp, dict) else 0
                    stock_parts = []
                    for e in shop.stock:
                        it = item_by_id.get(e.item_id)
                        if it is None:
                            continue
                        # [产地系数] 货架注入价含产地直供折扣（与结算口径一致防旁白错价）
                        prod_buy, _, _ = tre.production_price_factors(world, shop, it)
                        # [物价联动 2026-09-10] 季节/财富乘区（与结算口径一致防旁白错价）
                        mkt_buy, _, mkt_tags = tre.market_price_factors(world, shop, it)
                        price = tre.buy_price(e, it, shop, rep_of(merchant) if merchant else 0,
                                              pace, night_m * terr_buy * prod_buy * mkt_buy,
                                              merchant_trust=_mtrust)
                        stk = "无限" if e.stock == -1 else str(e.stock)
                        seg = f"{it.name}[{it.rarity}]"
                        if getattr(it, "level", 0):
                            seg += f" Lv{it.level}"
                        if not getattr(it, "identified", True):
                            seg += "（未鉴定）"
                        if mkt_tags:
                            seg += "（" + "/".join(mkt_tags) + "）"
                        d = (getattr(it, "desc", "") or "").strip()
                        stock_parts.append(f"{seg} 售{price} 库{stk}" + (f"：{d}" if d else ""))
                    if stock_parts:
                        shelf_note = ""
                        if terr_buy > 1.0:
                            shelf_note += "（敌对治下加价）"
                        if night_m > 1.0:
                            shelf_note += "（夜间敲门价）"
                        tlines.append(f"货架（{mname}）{shelf_note}："
                                      + "；".join(stock_parts))
                # 在场 NPC 随身物品（barter take 的权威来源）
                for n in barter_npcs:
                    np = []
                    for i in (n.inventory or []):
                        it = item_by_id.get(i)
                        if it is not None:
                            np.append(f"{it.name}[{it.rarity}]")
                    if np:
                        tlines.append(f"可换随身（{n.name}）：" + "、".join(np))
                volatile.append("【交易】\n" + "\n".join(tlines))
        # P4 [P39d] 坊间热议（近期大事按与玩家的相关性排序 top3——导演层：世界事件
        # 变成玩家面前的谈资；预算固定不随世界年龄涨）。[④ 2026-08-30] 优先用 LLM
        # 口述化的市井传闻文本（rme.hot_topic_lines），未口述化时回退模板 desc。
        hot_lines = rme.hot_topic_lines(world)
        if hot_lines:
            volatile.append(
                "【坊间热议】（近期大事，按与你相关的程度排序，可作 NPC 闲谈与旁白氛围素材；"
                "仅作背景谈资，不要把其中人物当作现场人物描写）\n" + "\n".join(hot_lines))
        # [P39e] 世界编年史（长线世界记忆：已沉淀大事摘要，最近 2 条）
        chron_lines = che.chronicle_lines(world, 2)
        if chron_lines:
            volatile.append("【世界编年史】（这个世界过去发生的大事，据实可引）\n  "
                            + "\n  ".join(chron_lines))
        # [剧情线 P46] 【当前剧情线】简报（volatile 段内=不伤前缀缓存；预算固定
        # 本地 2 + 远方 2 行，读时派生零新调用；玩家窝在一地也看得到世界在动）
        try:
            _arc_block = sae.arc_context_block(world)
        except Exception:
            _arc_block = ""
        if _arc_block:
            volatile.append(_arc_block)
        # [修 2026-10-01 用户报·NPC 不知道哪里有货] 任务物品风声：进行中任务的未完成
        # 收集类目标（gather/collect/deliver_items，上限 3 条——注入预算固定），逐目标
        # 给全城渠道摘要（qe.sourcing_hint 单一来源，与任务日志「有售/可采集」同口径）。
        # 叙事 LLM 据此让 NPC 在玩家打听时据实指路（此前 NPC 只看得到当前地点货架，
        # 全城渠道一概不知，问路只能编）。只作应答素材，不主动剧透任务。
        try:
            _rumor_lines = []
            for _q in (getattr(world, "quests", None) or []):
                if str(getattr(_q, "status", "")) != "active" or len(_rumor_lines) >= 3:
                    continue
                for _o in (getattr(_q, "objectives", None) or []):
                    if (isinstance(_o, dict)
                            and str(_o.get("type", "")) in ("gather", "collect", "deliver_items")
                            and int(_o.get("current", 0) or 0) < int(_o.get("count", 1) or 1)):
                        _t = str(_o.get("target", "") or "").strip()
                        _hint = qe.sourcing_hint(world, _t) if _t else ""
                        if _hint:
                            _rumor_lines.append(f"- {_t}：{_hint}")
                        break   # 每任务至多 1 条（预算固定）
            if _rumor_lines:
                volatile.append(
                    "【任务物品风声】（你进行中的收集目标在世间的获取渠道，玩家打听时可据实"
                    "指路——谁是卖家、哪里可采；仅应答时用，不要主动向玩家剧透任务）\n"
                    + "\n".join(_rumor_lines))
        except Exception:
            pass
        # [P7k5] 时间天气殿后：日期/相位/天气/回合N 四项全随 tick 变，是每回合必变的块，
        # 放最后让前面所有块（含前置的历史块）都能吃到跨回合缓存前缀。LLM 对末尾信息
        # 有 recency 加成，当前时态恰是每回合最需要「现在几点/在哪天」的判断锚点。
        volatile.append(_fmt_time_weather(world))
        return "\n\n".join(stable + volatile)

    def _build_settle_messages(
            self, world: World, scene: SceneLog, player_action: Optional[str],
            preset: WorldSimPreset,
    ) -> tuple[list[dict], str]:
        """构造结算 LLM 的 user 消息 + 返回 user_msg（供重试回灌）。

        player_action 必填（首回合不结算——2026-09-13 初始选项链路已摘除）。
        """
        # [P15a2] 结算走 compact 上下文（[P17] 瘦身：玩家数值/装备/背包/当前任务
        # 整块不进，结算判意图只需场景与在场者；世界书保留；最近经历 2 条）
        ctx = self._build_scene_context(world, scene, preset=preset, mode="compact")

        if player_action is None or not str(player_action).strip():
            # [修 2026-09-13 用户指示] 首回合「初始选项」链路已摘除（rev 44 移除选项功能
            # 后无消费方）：settle 只服务真实玩家行动，无行动直接拒——绝不替玩家编造
            # 开局意图（此前会给结算 LLM 发「场景的开端，尚未行动」白烧一次调用）。
            return None, "无玩家行动输入（首回合不结算）"
        action_desc = f"玩家本回合的行动：{player_action.strip()}"

        user_msg = (
            ctx
            + f"\n\n{action_desc}"
            + "\n\n请输出意图 JSON。地点名/NPC 名须用我给出的真实名称，普通移动的 move_to 须为相邻地点。"
              "若在【秘境】内部，去房间须抄已知相邻出口名，检查当前房填「深入」，上下层填「下层楼梯」或「上层楼梯」。"
              "抵达与互动分开，多个方向不要代玩家选路，不能把未开箱/未通过的房间写成已经处理。"
        )
        messages = [
            {"role": "system", "content": preset.settle_system_prompt or DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        return messages, user_msg

    def _call_and_parse_intent(
            self, llm, messages, cancel_check,
    ) -> tuple[Optional[dict], str]:
        """调一次结算 LLM 并解析意图 JSON，返回 (intent|None, error)。schema 补缺 + 校验。"""
        result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
        if result.cancelled:
            return None, "已取消"
        if result.error:
            return None, result.error
        if not result.content or not result.content.strip():
            return None, "LLM 返回空内容"
        intent = _extract_json(result.content)
        if intent is None or not isinstance(intent, dict):
            return None, "无法解析为 JSON"
        # schema 补缺 + 类型规整
        # [!] resolved 脏值解析：LLM 输出字符串 "false" 时 bool("false") == True，
        # 本应受阻的行动被当成功结算（方向恰好反了）——按真值语义解析。
        r_raw = intent.get("resolved", True)
        if isinstance(r_raw, bool):
            intent["resolved"] = r_raw
        elif isinstance(r_raw, str):
            intent["resolved"] = r_raw.strip().lower() not in ("false", "0", "no", "")
        else:
            intent["resolved"] = bool(r_raw)
        intent["reason"] = str(intent.get("reason", "") or "")
        it = intent.get("intent_type", "custom") or "custom"
        intent["intent_type"] = it if it in _INTENT_TYPE_VALUES else "custom"
        intent["move_to"] = str(intent.get("move_to", "") or "").strip()
        intent["talk_to"] = str(intent.get("talk_to", "") or "").strip()
        # [修 2026-09-06 审计] gift 字段同口径 str 规整（防脏类型半修改态 AttributeError）
        intent["gift_item"] = str(intent.get("gift_item", "") or "").strip()
        # [!] gather_node/target 同步规整为 str：LLM 输出 {"gather_node": {...}} 等脏类型时
        # apply_intent 的 .strip() 会抛 AttributeError，且此时 tick 已推进（半修改状态）。
        intent["gather_node"] = str(intent.get("gather_node", "") or "").strip()
        intent["target"] = str(intent.get("target", "") or "").strip()
        # [P28] 交易字段同口径 str 规整（防脏类型 AttributeError，tick 已推进半修改状态）
        intent["trade_mode"] = str(intent.get("trade_mode", "") or "").strip()
        intent["trade_item"] = str(intent.get("trade_item", "") or "").strip()
        intent["trade_offer"] = str(intent.get("trade_offer", "") or "").strip()
        # [B方案] 股市意图字段（symbol 走 trade_item 复用；qty 独立）
        intent["stock_qty"] = str(intent.get("stock_qty", "") or "").strip()
        effects = intent.get("effects") or []
        intent["effects"] = [str(x) for x in effects] if isinstance(effects, list) else []
        intent["narration_hint"] = str(intent.get("narration_hint", "") or "")
        # [P23] memory_worthy 脏值解析（同 resolved 的坑：bool("false")==True 会误触发 force
        # 记忆整理，绕过 interval 节流）——按真值语义解析。
        mw = intent.get("memory_worthy", False)
        if isinstance(mw, bool):
            intent["memory_worthy"] = mw
        elif isinstance(mw, str):
            intent["memory_worthy"] = mw.strip().lower() in ("true", "1", "yes", "是")
        else:
            intent["memory_worthy"] = bool(mw)
        # [用户指示 2026-09-05] next_options 已随选项功能移除；LLM 仍输出时静默丢弃
        intent.pop("next_options", None)
        return intent, ""

    def settle_action(
            self, world: World, scene: SceneLog, player_action: Optional[str],
            preset: WorldSimPreset,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[Optional[dict], str]:
        """调结算 LLM 出意图 JSON。失败重试一次（回灌错误反馈），仍失败返回 (None, msg)。

        返回的 intent 已 schema 补缺；player_action 空时返回 (None, 原因) 不调 LLM。
        """
        api = self._resolve_api(preset.calculator_api_id)
        if not api:
            return None, "未配置可用的结算 LLM API，请先在「设置 -> 世界模拟设置」中绑定或启用一个 API。"
        tmp_preset = Preset(
            name="world_sim_settle",
            system_prompt=preset.settle_system_prompt or DEFAULT_WORLDSIM_SETTLE_SYSTEM_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=preset.calculator_max_tokens,
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())

        messages, user_msg = self._build_settle_messages(world, scene, player_action, preset)
        intent, err = self._call_and_parse_intent(llm, messages, cancel_check)
        if intent is not None:
            return intent, ""
        if cancel_check and cancel_check():
            return None, "已取消"
        debug_log(lambda: f"[WorldSim] 结算首次解析失败: {err}，重试一次")
        # 重试一次：回灌错误反馈
        retry_user = (
            user_msg
            + "\n\n[!] 你上次的输出无法解析为合法 JSON，错误："
            + (err or "JSON 格式错误")
            + "。请严格按 schema 重新输出纯 JSON，不要包含任何说明文字或代码块标记。"
        )
        messages_retry = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": retry_user},
        ]
        intent2, err2 = self._call_and_parse_intent(llm, messages_retry, cancel_check)
        if intent2 is not None:
            return intent2, ""
        if cancel_check and cancel_check():
            return None, "已取消"
        return None, f"行动结算失败（重试仍出错）：{err2 or err or '未知错误'}"

    def _companions_follow(self, world: World, to_loc: Location) -> tuple[list[str], list[str]]:
        """[P10b] 玩家移动到 to_loc 时，同行 NPC 跟随（_move_npc_safe 同口径更新两地点 npc_ids）。

        自动剔除死亡/失踪/变敌对的「同伴」（如被世界滴答演化杀死）。
        返回 (跟随的 NPC 名列表, 掉队的 NPC 名列表)。纯 Python 无 LLM。
        """
        ids = getattr(world.player, "companion_npc_ids", None) or []
        if not ids:
            return [], []
        loc_by_id = {l.id: l for l in world.locations}
        moved: list[str] = []
        dropped: list[str] = []
        for cid in list(ids):
            npc = next((n for n in world.npcs if n.id == cid), None)
            if npc is None or not getattr(npc, "alive", True) or getattr(npc, "hostile", False):
                ids.remove(cid)
                dropped.append(npc.name if npc is not None else cid)
                continue
            if npc.location_id == to_loc.id:
                continue  # 已在目标地点（如被滴答挪走又恰好同地）
            _move_npc_safe(npc, npc.location_id, to_loc.id, loc_by_id)
            # [P27] 跨地点移动：同伴 place_id 重置为目标地点默认场所
            npc.place_id = getattr(to_loc, "default_place_id", "") or ""
            moved.append(npc.name)
        return moved, dropped

    def apply_intent(self, world: World, scene: SceneLog, intent: dict,
                     preset: Optional[WorldSimPreset] = None) -> dict:
        """引擎层纯 Python 结算意图（守架构铁律）。原地改 world/scene，返回结算摘要 dict。

        P2 处理 move（连通性校验 + 变更 player.location_id）；P3 加 combat（战斗引擎纯 Python
        + SeededRng）与 use_item（消耗品回血）。其余 intent_type 由叙事承载。
        tick_count/scene.tick 每回合自增。resolved=false 不变更世界状态（仍推进 tick，旁白描述受阻）。
        返回 summary dict 供 UI 刷新与旁白参考。
        """
        summary = self._apply_intent_impl(world, scene, intent, preset)
        # [修 2026-09-06 真机] 引擎侧补充的事实（奇遇奖励/探索 xp/隐藏察觉/场所移动等）只写
        # summary.narration_hint，而叙事 LLM 只读 intent.narration_hint（_build_narrate_messages
        # 口径）-> 玩家状态凭空变化、旁白一个字不提（首入临江府奇遇照骨镜+46银+38XP 静默入账）。
        # 统一在出口按「；」段去重并入 intent，双写路径（移动分支）不重复。
        extra = str(summary.get("narration_hint") or "").strip()
        if extra:
            base = str(intent.get("narration_hint") or "").strip()
            seen = [s.strip() for s in base.split("；") if s.strip()]
            add = [s for s in (x.strip() for x in extra.split("；"))
                   if s and s not in seen]
            if add:
                intent["narration_hint"] = "；".join(([base] if base else []) + add)
        return summary

    def _apply_location_arrival(self, world: World, target, summary: dict,
                                 intent: dict, preset=None) -> None:
        """[修 2026-09-06 真机 R40] 跨地点到达结算（move 分支与 non-move 前置消费共用）。

        相邻性由调用方保证（铁律：只解析相邻名单内的名字，防跨境传送）；
        到达副作用与 move_player 同口径：place 重置/发现标记/揭示相邻/
        奇遇 roll/隐藏检定/任务 visit 钩子/同伴跟随。
        """
        source_dungeon = dge.interior_dungeon(world, self._current_location(world))
        target_dungeon = dge.interior_dungeon(world, target)
        if source_dungeon is not None or target_dungeon is not None:
            if source_dungeon is not None and target.id != source_dungeon.location_id:
                ok, msg = False, "须从秘境第一层入口离开"
            else:
                ok, msg = (self.exit_dungeon(world) if source_dungeon is not None
                           else self.enter_dungeon(world, target_dungeon, summary=summary,
                                                   intent=intent, preset=preset))
            if not ok:
                intent["resolved"] = False
                summary["reason"] = msg
            else:
                arrived = self._current_location(world)
                summary.update({"moved": True, "world_changed": True,
                                "to": arrived.name if arrived is not None else target.name})
                if source_dungeon is not None:
                    summary["narration_hint"] = msg
            return
        world.player.location_id = target.id
        # [P27] 跨地点移动：place_id 重置为目标地点默认场所
        world.player.place_id = getattr(target, "default_place_id", "") or ""
        summary["moved"] = True
        summary["to"] = target.name
        # [P7f] 标记已发现 + 已探索（与 move_player 同口径，apply_intent 路径补标）
        target.discovered = True
        target.explored = True
        # [!] 到达后揭示新的相邻地点（与 move_player 同口径；旧 apply_intent 漏此步，
        # 地图移动路径走 apply_intent 后会少揭示相邻节点）。
        self.reveal_adjacent(world)
        # [D03/R2 2026-09-30] 到访触发秘境发现（知道有洞 -> scene_block/弹窗/议程可见）
        try:
            _found_dgs = dge.discover_on_visit(world, target)
            for _fd in _found_dgs:
                intent["narration_hint"] = (str(intent.get("narration_hint") or "")
                    + ("；" if intent.get("narration_hint") else "")
                    + f"你发现了一处隐秘的入口——「{_fd.name}」"
                      f"（{'危险' if _fd.danger >= 5 else '似乎有些危险'}）")
                summary["narration_hint"] = intent["narration_hint"]
                summary["world_changed"] = True
        except Exception:
            pass
        # [P8] 奇遇自动触发：首次进入该地点（encounter_done False）时 roll 一次，每地点只一次
        self._maybe_trigger_encounter(world, target, summary, mode="auto")
        # [P33] 隐藏检定：首次进入该地点时 roll 被动察觉（int/luk 取高 + check_mult），
        # 通过则注入【察觉】narration_hint 供叙事 LLM 描写隐藏发现（隐藏 NPC/资源点/线索）。
        self._maybe_hidden_check(world, target, summary, preset)
        # [P7i] 任务进度钩子：visit
        try:
            qe.update_progress(world, "visit", target.name,
                               aliases=[target.region])
        except Exception:
            pass
        # [P10b] 同行 NPC 跟随移动（交情达门槛邀请的同伴；死亡/敌对自动掉队）
        comp_moved, comp_dropped = self._companions_follow(world, target)
        for names, key, verb in ((comp_moved, "companions_followed", "与你同行抵达"),
                                 (comp_dropped, "companions_lost", "已不在你身边（掉队）")):
            if names:
                summary[key] = names
                hint_c = f"{'、'.join(names)}{verb}"
                prev = summary.get("narration_hint") or ""
                summary["narration_hint"] = (prev + "；" + hint_c).lstrip("；") if prev else hint_c

    def _apply_intent_impl(self, world: World, scene: SceneLog, intent: dict,
                           preset: Optional[WorldSimPreset] = None) -> dict:
        summary = {"moved": False, "to": "", "reason": ""}
        # 推进回合（无论是否 resolved，玩家尝试了行动即推进世界时间）
        # [!] _safe_int 防 tick_count 脏值（守审查 D5）
        world.tick_count = _safe_int(world.tick_count, 0) + 1
        scene.tick = world.tick_count

        if not intent.get("resolved", True):
            summary["reason"] = intent.get("reason", "") or "行动受阻"
            return summary

        itype = intent.get("intent_type", "custom")
        if (itype in ("go_home", "go_domain")
                and dge.interior_dungeon(world, self._current_location(world)) is not None):
            intent["resolved"] = False
            summary["reason"] = "须先沿秘境路线返回第一层入口并离开，再归宅或归返据点"
            return summary
        # ---- [修 2026-09-06 真机] 非 move 意图携带场所级 move_to 的前置消费 ----
        # settle 常把「去 X 看看/去 X 买」判成 observe/interact/trade 且同时填 move_to；
        # 旧逻辑只在 itype=="move" 分支消费 move_to，场所不动而旁白按行动文本写「已到场」，
        # 数据与叙事位置脱节（真机 R5-R7 三连抓出）。此处：move_to 解析为当前地点内场所时
        # 不论意图类型均先执行场所移动（跨地点 move_to 仍只归 move 意图，防 observe 跨境传送），
        # 随后继续走原意图分派（观察/交易等照常结算）。
        if itype != "move":
            _mv_raw = str(intent.get("move_to", "") or "").strip()
            if _mv_raw:
                _cur0 = self._current_location(world)
                _place_hit = None
                if "/" in _mv_raw:
                    _ln, _, _pn = _mv_raw.partition("/")
                    if (_cur0 is not None and _ln.strip() and _pn.strip()
                            and _ln.strip() == _cur0.name):
                        _place_hit = self._resolve_place_by_name(_cur0, _pn.strip())
                elif _cur0 is not None and getattr(_cur0, "places", None):
                    _place_hit = self._resolve_place_by_name(_cur0, _mv_raw)
                if _place_hit is not None:
                    if self._move_to_place(world, world.player, _place_hit, _cur0):
                        summary["moved"] = True
                        summary["to"] = f"{_cur0.name}/{_place_hit.name}"
                        summary["place_moved"] = True
                        _hint_p = f"玩家已移步至{_cur0.name}/{_place_hit.name}（场所移动已由引擎结算）"
                        _prev = summary.get("narration_hint") or ""
                        summary["narration_hint"] = (_prev + "；" + _hint_p).lstrip("；") if _prev else _hint_p
                    for _cid in (getattr(world.player, "companion_npc_ids", None) or []):
                        _cn = next((n for n in world.npcs if n.id == _cid), None)
                        if _cn and getattr(_cn, "alive", True) and not getattr(_cn, "hostile", False):
                            self._move_to_place(world, _cn, _place_hit, _cur0)
                else:
                    # [修 2026-09-06 真机 R40] 场所未命中 -> 相邻【地点级】move_to 也前置消费。
                    # 速查表令「去白水镇探矿营」判 observe+move_to=白水镇：旧逻辑跨地点只归
                    # move 分支 -> 移动被静默丢弃而旁白写「已抵达」（数据与叙事脱节）。
                    # 相邻铁律不变：nrs.resolve_name 锚定相邻名单，非相邻绝不放行。
                    _adj0 = self._adjacent_locations(world, _cur0) if _cur0 is not None else []
                    _mv_hit = nrs.resolve_name(_mv_raw, [l.name for l in _adj0]) if _adj0 else None
                    _loc_hit = next((l for l in _adj0 if l.name == _mv_hit), None) if _mv_hit else None
                    if _loc_hit is not None:
                        self._apply_location_arrival(world, _loc_hit, summary, intent, preset)
                        if (intent.get("resolved") is False or summary.get("dungeon_entered")
                                or summary.get("forced_combat") or summary.get("player_defeated")):
                            return summary
                        _hint_l = (f"玩家已先移动到「{_loc_hit.name}」（跨地点移动已由引擎结算），"
                                   f"随后执行原意图")
                        _prev = summary.get("narration_hint") or ""
                        summary["narration_hint"] = (_prev + "；" + _hint_l).lstrip("；") if _prev else _hint_l
        # 交谈由 LLM 识别人名，但能否当面交谈以当前场所为准。先消费本回合的移动，
        # 再校验目标，允许「走进酒馆找张三」一步完成，同时防远处 NPC 被凭空对话。
        if itype in ("talk", "interact", "quest"):
            talk_name = str(intent.get("talk_to") or "").strip()
            if talk_name:
                target_npc = _npc_by_name(world, talk_name)
                # interact 还承载查看尸体/遗物等动作；这类对象不必活着，
                # 但不会推进交谈任务，也不会写入 NPC 私人记忆。
                if itype == "interact" and (target_npc is None or not target_npc.alive):
                    target_npc = None
                elif target_npc is None or not getattr(target_npc, "alive", True):
                    intent["resolved"] = False
                    summary["reason"] = f"找不到可交谈的「{talk_name}」"
                    return summary
                if target_npc is not None and getattr(target_npc, "hostile", False):
                    intent["resolved"] = False
                    summary["reason"] = f"「{target_npc.name}」与你敌对，无法交谈"
                    return summary
                if target_npc is not None and not self._same_place_as_player(world, target_npc):
                    intent["resolved"] = False
                    summary["reason"] = f"「{target_npc.name}」不在你身边，请先到其所在场所"
                    return summary
        # 移动结算：name -> id 解析 + 连通性校验（与 move_player 同口径，守审查 D1）
        if itype == "move":
            move_name = intent.get("move_to", "")
            cur = self._current_location(world)
            if intent.get("dungeon_action") == "enter":
                dungeon = next((d for d in world.dungeons if d.id == intent.get("dungeon_id")), None)
                if dungeon is None or dungeon.run_id != intent.get("dungeon_run_id"):
                    intent["resolved"] = False
                    summary["reason"] = "这次秘境进入行动已失效，请重新查看入口"
                    return summary
                ok, msg = self.enter_dungeon(world, dungeon, summary=summary,
                                             intent=intent, preset=preset)
                if not ok:
                    intent["resolved"] = False
                    summary["reason"] = msg
                else:
                    arrived = self._current_location(world)
                    summary.update({"moved": True, "world_changed": True,
                                    "to": arrived.name if arrived is not None else dungeon.name})
                return summary
            if intent.get("dungeon_action") == "move":
                dungeon = dge.interior_dungeon(world, cur)
                if dungeon is None:
                    intent["resolved"] = False
                    summary["reason"] = "你不在秘境中，这次房间行动已失效"
                else:
                    dge.move_room(world, dungeon, str(intent.get("dungeon_room_id", "") or ""), intent, summary)
                    self._resolve_dungeon_defeat(world, intent, summary, preset)
                return summary
            if not move_name or not cur:
                summary["reason"] = "未指定目标地点或无当前地点"
                intent["resolved"] = False
                return summary
            # ---- [P27] 场所级移动：move_to 含「/」分隔（如「小镇/酒馆」）-> 地点内场所移动。
            # 格式 = 当前地点名/场所名（地点名须 == 当前地点名，防跨地点场所误判）。
            # 纯场所名（无 /）且当前地点有该场所 -> 亦判场所移动。
            if "/" in move_name:
                loc_name, _, place_name = move_name.partition("/")
                loc_name = loc_name.strip()
                place_name = place_name.strip()
                if loc_name and place_name and loc_name == cur.name:
                    target_place = self._resolve_place_by_name(cur, place_name)
                    if target_place is not None:
                        # [用户定稿 2026-08-28] 地点内场所自由到达：不校验场所连通性
                        # （同地点任意场所一步可达；跨地点移动仍走相邻地点校验）
                        did = self._move_to_place(world, world.player, target_place, cur)
                        if did:
                            summary["moved"] = True
                            summary["to"] = f"{cur.name}/{target_place.name}"
                            summary["place_moved"] = True
                        # 同行 NPC 跟随到同场所（同伴在地点内跟随玩家移动）
                        for cid in (getattr(world.player, "companion_npc_ids", None) or []):
                            cn = next((n for n in world.npcs if n.id == cid), None)
                            if cn and getattr(cn, "alive", True) and not getattr(cn, "hostile", False):
                                self._move_to_place(world, cn, target_place, cur)
                        return summary
            # 纯场所名（无 /）：当前地点有该场所 -> 场所移动（settle 可能只给场所名）
            if "/" not in move_name and getattr(cur, "places", None):
                target_place = self._resolve_place_by_name(cur, move_name)
                if target_place is not None:
                    # [用户定稿 2026-08-28] 地点内场所自由到达（同上，不校验连通）
                    did = self._move_to_place(world, world.player, target_place, cur)
                    if did:
                        summary["moved"] = True
                        summary["to"] = f"{cur.name}/{target_place.name}"
                        summary["place_moved"] = True
                    for cid in (getattr(world.player, "companion_npc_ids", None) or []):
                        cn = next((n for n in world.npcs if n.id == cid), None)
                        if cn and getattr(cn, "alive", True) and not getattr(cn, "hostile", False):
                            self._move_to_place(world, cn, target_place, cur)
                    return summary
            adj = self._adjacent_locations(world, cur)
            # [P44] 相邻地点名容错解析（候选锚定 adj 相邻名单，相邻性铁律不变）：
            # LLM 写「青石镇东门」/「望月涯」这类修饰/别字不再卡「无法抵达」
            mv_hit = nrs.resolve_name(move_name, [l.name for l in adj])
            target = next((l for l in adj if l.name == mv_hit), None) if mv_hit else None
            # ---- [P25a] 秘境内部特判：目标不是相邻真实地点 -> 推进房间探索。
            # 离开秘境走正常相邻移动（内部地点与入口双向连通，目标=入口名命中 target）。
            if target is None and dge.interior_dungeon(world, cur) is not None:
                dungeon = dge.interior_dungeon(world, cur)
                # [D01/R2] 具名相邻房间移动：move_to 命中当前位置邻接房间（名字容错
                # 锚定邻接名单）-> 纯位置变更（邻接/战斗门控在 move_room 内）；
                # 「深入」语义仍走 advance 推进探索。
                _dest = dge.resolve_dungeon_destination(world, dungeon, move_name)
                if _dest is not None:
                    dge.move_room(world, dungeon, str(_dest.id), intent, summary)
                    self._resolve_dungeon_defeat(world, intent, summary, preset)
                    return summary
                # [B02-F08] 只有「深入」语义推进房间；具名未知目的地（编造的房间/方向）
                # 受阻不消耗房间内容——旧行为把任何未知名都当「深入」推进，打错房名
                # 会白白结算下一间房。
                _mv = str(move_name or "").strip()
                if _mv in ("绕过机关", "绕过陷阱"):
                    _f, _r, _i = dge._target_room(dungeon)
                    expected_type = "check" if _mv == "绕过机关" else "trap"
                    if _r is None or _r.type != expected_type:
                        intent["resolved"] = False
                        summary["reason"] = "眼前没有这类可绕过的房间"
                        return summary
                    intent["dungeon_action"] = "bypass"
                elif _mv and _mv not in _DUNGEON_DEEP_WORDS:
                    ent = dge.entrance_location(world, dungeon)
                    _exits = dge.known_exits_line(world, dungeon)
                    summary["reason"] = (f"秘境内没有「{_mv}」这个去处（可「深入」继续探索"
                                         f"或移动到相邻房间{_exits}；"
                                         f"或移动到入口「{ent.name if ent else dungeon.name}」离开）")
                    intent["resolved"] = False
                    return summary
                # [!] 早退跳过尾部 npc reactions/野外怪：野外怪对 kind=dungeon 本就 no-op；
                # 同伴虽跟进秘境但氛围反应（打招呼/送礼）在秘境紧张探索中刻意抑制（有意设计）。
                dge.advance(world, dungeon, summary, intent, world.player.level)
                self._resolve_dungeon_defeat(world, intent, summary, preset)
                return summary
            if target is None:
                # [!] 不做全地点模糊匹配（与 move_player 严格相邻校验一致）：
                # LLM 给非相邻名应判定不可达，由叙事描述受阻，而非静默放行破坏连通性约束。
                summary["reason"] = f"无法抵达「{move_name}」（不与当前地点相邻）"
                intent["resolved"] = False
                return summary
            # [修 2026-09-06 真机 R40] 到达副作用抽 _apply_location_arrival（与前置消费共用）
            self._apply_location_arrival(world, target, summary, intent, preset)
            if (intent.get("resolved") is False or summary.get("dungeon_entered")
                    or summary.get("forced_combat") or summary.get("player_defeated")):
                return summary
        elif itype == "combat":
            # ---- P3 战斗结算（纯 Python + SeededRng，LLM 不参与动态结果）----
            # [!] 数值范式：LLM 只出 talk_to(NPC名) 语义，引擎算伤害/暴击/掉落/经验。
            # [P25a] 秘境内部无世界 NPC：combat 意图路由到房间推进（pending 战斗房
            # 重建同一只怪走 forced_combat；非战斗房按 advance 正常结算）。
            cur = self._current_location(world)
            dungeon = dge.interior_dungeon(world, cur)
            if dungeon is not None:
                if dungeon.status != "open" and not dge.has_pending_loot(dungeon):
                    intent["resolved"] = False
                    summary["reason"] = f"秘境「{dungeon.name}」已封印"
                    return summary
                dge.advance(world, dungeon, summary, intent, world.player.level)
                self._resolve_dungeon_defeat(world, intent, summary, preset)
                return summary
            else:
                self._resolve_combat(world, scene, intent, summary, preset)
        elif itype == "use_item":
            # 使用消耗品（heal）
            self._resolve_use_item(world, intent, summary, preset)
            if intent.get("dungeon_action") == "tool":
                return summary  # 与秘境 move 同口径，不在陷阱复活后追加野外遇怪
        elif itype == "gift":
            # [修 2026-09-06 审计] 自由文本送礼：物品真转移 + 交情/印象真变化，
            # 叙事与记忆所见即引擎所做（旧况判 talk → 记忆与背包永久分歧）
            self._resolve_gift(world, intent, summary)
        elif itype == "gather":
            # [P7g] 采集结算（纯 Python gather_engine，LLM 不参与动态结果）
            self._resolve_gather(world, scene, intent, summary, preset)
        elif itype == "adventure":
            # [P8] 奇遇手动探测结算（纯 Python encounter_engine，LLM 不参与动态结果）
            self._resolve_adventure(world, scene, intent, summary, preset)
        elif itype == "dine":
            # [2026-09-08 用户指示] dine 意图已下线（用餐按钮删除，食堂堂食取消）：
            # 恢复饱食度一律走食物店买食物吃。旧档残留 preset_intent 在此拦截，
            # 指引玩家去买食物，不静默失败。
            intent["resolved"] = False
            summary["reason"] = "食堂已歇业——饿了去食物店买吃的（背包「使用」进食）"
            return summary
        elif itype == "rest":
            # [P24a] 宅中休憩（UI 快捷行动 preset_intent 专用；不进 _INTENT_TYPE_VALUES
            # ——自由文本「原地歇会儿」仍由 settle 判 wait/custom 走叙事承载）。
            # 跳时 + 回复全在 home_engine 纯 Python；tick_world 由 scene_worker 照常
            # 在 narrate 后跑 1 次（新 tick 处），满足「休憩只跑 1 次滴答」。
            home = he.player_home_at(world, self._current_location(world))
            if home is None:
                intent["resolved"] = False
                summary["reason"] = "此处没有你的住宅（须在自有宅所在的聚落休憩）"
                return summary
            rest = he.rest_at_home(world, home)
            summary["rest"] = rest
            hint = f"你在自宅「{home.name}」安歇，伤势与气力尽复，一觉睡到第{rest['day']}天清晨"
            if rest.get("bed_xp"):
                hint += "（床铺舒宜，略有进益）"
            # [!] intent 与 summary 双写（叙事 LLM 只读 intent.narration_hint，守 §22 既有约定）
            existing = intent.get("narration_hint", "") or ""
            intent["narration_hint"] = (existing + "；" + hint) if existing else hint
            summary["narration_hint"] = intent["narration_hint"]
        elif itype == "go_home":
            # [P24a] 归宅快捷（UI 预设行动专用；不进 settle 白名单，同 rest 口径）。
            # 回自家不受相邻限制（玩家主动快速行动，购宅必到过该聚落），消耗 1 回合
            # 世界演化；成功移动与 move 分支同口径（place_id 重置/揭示相邻/同伴跟随）。
            home = next((h for h in world.homes if h.id == intent.get("home_id", "")), None)
            if home is None:
                intent["resolved"] = False
                summary["reason"] = "不存在该住宅"
                return summary
            target = next((l for l in world.locations if l.id == home.location_id), None)
            if target is None:
                intent["resolved"] = False
                summary["reason"] = "住宅所在聚落已不存在"
                return summary
            cur = self._current_location(world)
            if cur is not None and cur.id == target.id:
                hint = f"你已在自宅「{home.name}」所在的聚落"
            else:
                world.player.location_id = target.id
                # [P27] 跨地点移动：place_id 重置为目标地点默认场所（同 move 口径）
                world.player.place_id = getattr(target, "default_place_id", "") or ""
                target.discovered = True
                target.explored = True
                self.reveal_adjacent(world)
                summary["moved"] = True
                summary["to"] = target.name
                # 到达副作用同 move 口径（已访问聚落本就 no-op，兜齐首入边界）
                self._maybe_trigger_encounter(world, target, summary, mode="auto")
                self._maybe_hidden_check(world, target, summary, preset)
                try:
                    qe.update_progress(world, "visit", target.name,
                                       aliases=[target.region])
                except Exception:
                    pass
                # [P10b] 同行 NPC 跟随移动（同 move 分支口径）
                comp_moved, comp_dropped = self._companions_follow(world, target)
                for names, key, verb in ((comp_moved, "companions_followed", "与你同行归宅"),
                                         (comp_dropped, "companions_lost", "已不在你身边（掉队）")):
                    if names:
                        summary[key] = names
                        hint_c = f"{'、'.join(names)}{verb}"
                        prev = summary.get("narration_hint") or ""
                        summary["narration_hint"] = (prev + "；" + hint_c).lstrip("；") if prev else hint_c
                hint = f"你动身返回「{target.name}」的自宅「{home.name}」"
            # [!] intent 与 summary 双写（叙事 LLM 只读 intent.narration_hint，守 §22 既有约定）
            existing = intent.get("narration_hint", "") or ""
            intent["narration_hint"] = (existing + "；" + hint) if existing else hint
            prev = summary.get("narration_hint") or ""
            summary["narration_hint"] = (prev + "；" + hint) if prev else hint
        elif itype == "go_domain":
            # [D1] 归返据点快捷（UI 预设行动专用；不进 settle 白名单，同 go_home 口径）。
            # 回自家据点不受相邻限制（购地必到过该地），消耗 1 回合世界演化；成功移动与
            # move 分支同口径（place_id 重置到据点营门/揭示相邻/同伴跟随）。
            target = next((l for l in world.locations
                           if l.id == intent.get("domain_location_id", "")), None)
            if target is None or not getattr(target, "player_owned", False):
                intent["resolved"] = False
                summary["reason"] = "不存在该据点"
                return summary
            cur = self._current_location(world)
            if cur is not None and cur.id == target.id:
                hint = f"你已在自家据点「{target.name}」"
            else:
                world.player.location_id = target.id
                # [P27] 跨地点移动：place_id 重置为据点营门（default_place_id，buy_domain 铺设）
                world.player.place_id = getattr(target, "default_place_id", "") or ""
                target.discovered = True
                target.explored = True
                self.reveal_adjacent(world)
                summary["moved"] = True
                summary["to"] = target.name
                # 到达副作用同 move 口径（据点是聚落，聚落免怪；探遇/隐藏检定兜齐首入边界）
                self._maybe_trigger_encounter(world, target, summary, mode="auto")
                self._maybe_hidden_check(world, target, summary, preset)
                try:
                    qe.update_progress(world, "visit", target.name,
                                       aliases=[target.region])
                except Exception:
                    pass
                # [P10b] 同行 NPC 跟随移动（同 move 分支口径）
                comp_moved, comp_dropped = self._companions_follow(world, target)
                for names, key, verb in ((comp_moved, "companions_followed", "与你同行归返据点"),
                                         (comp_dropped, "companions_lost", "已不在你身边（掉队）")):
                    if names:
                        summary[key] = names
                        hint_c = f"{'、'.join(names)}{verb}"
                        prev = summary.get("narration_hint") or ""
                        summary["narration_hint"] = (prev + "；" + hint_c).lstrip("；") if prev else hint_c
                hint = f"你动身返回自家据点「{target.name}」"
            # [!] intent 与 summary 双写（叙事 LLM 只读 intent.narration_hint，守 §22 既有约定）
            existing = intent.get("narration_hint", "") or ""
            intent["narration_hint"] = (existing + "；" + hint) if existing else hint
            prev = summary.get("narration_hint") or ""
            summary["narration_hint"] = (prev + "；" + hint) if prev else hint
        elif itype in ("stock_buy", "stock_sell"):
            if self._resolve_stock(world, scene, intent, summary, preset):
                # [修 2026-09-06] 已回落普通交易（settle 误判股市）：复用 trade 分支的
                # 受阻早退口径（交易未成不再触发任务推进/NPC 反应/野外怪）。
                if not intent.get("resolved", True):
                    return summary
        elif itype == "trade":
            # [P28] 交易结算（纯 Python trade_engine，LLM 不参与动态结果）：
            # buy/sell 走商店 Shop 货架（[游商] 新世界常驻游商 + 旧档真商人兼容）；
            # barter 走任意 NPC 以物易物（交情+价值比门控）。
            # 容器权威解析（item_by_name 不唯一，按目标容器解析消歧 + 兜底虚构物品）。
            self._resolve_trade(world, scene, intent, summary, preset)
            # [!] 受阻早退（同 move 失败口径）：交易未成不应再触发任务推进/NPC 反应/野外怪。
            if not intent.get("resolved", True):
                return summary
        # [P7i] 任务进度钩子：talk（交谈/互动/任务/交易类意图，按 talk_to NPC 名匹配）
        if intent.get("resolved", True) and itype in ("talk", "interact", "quest", "trade", "gift"):
            talk_name = str(intent.get("talk_to") or "").strip()
            npc = _npc_by_name(world, talk_name, alive_only=True) if talk_name else None
            if (npc is not None and not getattr(npc, "hostile", False)
                    and self._same_place_as_player(world, npc)):
                try:
                    qe.update_progress(world, "talk", npc.name)
                except Exception:
                    pass
        # [P18] NPC 到场/离场改由叙事 LLM 首行移动命令驱动（narrate_outcome 内统一解析执行，
        # 见 _finalize_narrative）——settle effects 声明与 talk_to 兜底两代方案已移除
        #（LLM 侧声明通道永远追不上旁白自由发挥，直接让旁白自己下命令最可靠）。
        # [P10] 同场景 NPC 主动反应（打招呼/送礼/氛围行动/交情增长；确定性引擎，无 LLM）。
        # 放在主结算之后：送礼看谈话结果、打招呼看移动结果；产出的 narration_hint
        # 由叙事 LLM 据实织入旁白（守数值范式：结构变更全在本方法内完成）。
        try:
            nre.maybe_npc_reactions(world, scene, intent, summary, preset)
        except Exception:
            pass  # 反应失败不阻断回合
        # [P12] 野外怪物遭遇：野外地点每回合行动后判定（combat 意图跳过；post_combat
        # 战后叙事回合在 _maybe_wilderness_monster 内部门控）。命中产出
        # summary["forced_combat"]=临时怪物 NPC。
        try:
            if itype != "combat":
                self._maybe_wilderness_monster(world, intent, summary, preset, itype)
        except Exception:
            pass  # 遭遇判定失败不阻断回合
        # [C-lite 2026-10-02 用户指示] 同地点敌对 NPC 主动袭击（伏击落地）：旧档独眼彪
        # 自主跟进拦路却永远等玩家先动手——真实敌对 NPC 与玩家同地点时有概率扑上来。
        # 每回合最多一场（forced_combat 已弹则跳过）；战后叙事回合不追击。
        try:
            if (itype != "combat" and not intent.get("post_combat")
                    and not summary.get("forced_combat")):
                self._maybe_npc_ambush(world, intent, summary, preset)
        except Exception:
            pass  # 袭击判定失败不阻断回合
        return summary

    def _maybe_npc_ambush(self, world: World, intent: dict, summary: dict,
                          preset: Optional[WorldSimPreset] = None):
        """[C-lite 2026-10-02 用户指示] 同地点敌对 NPC 主动袭击判定（确定性 roll）。

        候选=与玩家同地点、存活、hostile 的真实 NPC（要角/喽啰一视同仁）；每个 NPC
        独立冷却 npc_ambush_last_tick（默认 24 tick=2 个世界日，防连环追杀把玩家钉死）；
        命中产出 summary["npc_ambush"]=NPC 实体（场景页 _on_finished 弹
        _fight_npc_dialog，与玩家主动攻击同一结算链：胜败/掉落/kill 进度全真实记账）。
        旋钮：npc_ambush_chance（默认 0.25/回合）、npc_ambush_cooldown_ticks（默认 24），
        均可被 config_overlay 按世界覆盖。
        """
        if preset is None:
            return
        if intent.get("post_combat"):
            return
        p = getattr(world, "player", None)
        if p is None:
            return
        # 与野怪遭遇同闸：叙事战斗模式/一击制没有对话框承载真实 NPC 多回合战
        if self._per_world(world, "combat_system",
                           preset.combat_system if preset else "crpg", preset) == "narrative":
            return
        if not self._per_world(world, "combat_player_controlled",
                               preset.combat_player_controlled, preset):
            return
        cands = [n for n in (getattr(world, "npcs", None) or [])
                 if getattr(n, "hostile", False) and getattr(n, "alive", True)
                 and str(getattr(n, "location_id", "") or "") == str(p.location_id or "")
                 and getattr(n, "name", "")]
        if not cands:
            return
        tick = int(getattr(world, "tick_count", 0) or 0)
        cooldown = max(1, int(self._per_world(world, "npc_ambush_cooldown_ticks",
                                              24, preset) or 24))
        chance = max(0.0, min(1.0, float(self._per_world(world, "npc_ambush_chance",
                                                         0.25, preset) or 0.0)))
        if chance <= 0:
            return
        last = dict(getattr(world, "npc_ambush_last_tick", None) or {})
        rng = SeededRng.seed_from(str(getattr(world, "id", "")), tick, "npc_ambush")
        for n in sorted(cands, key=lambda x: str(x.id)):
            if tick - int(last.get(str(n.id), -(10 ** 9)) or 0) < cooldown:
                continue
            if not rng.chance(chance):
                continue
            last[str(n.id)] = tick
            world.npc_ambush_last_tick = last
            summary["npc_ambush"] = n
            hint = f"{n.name}不再遮掩杀意，猛地向你扑来！"
            intent["narration_hint"] = ((intent.get("narration_hint") or "")
                                        + ("；" if intent.get("narration_hint") else "")
                                        + hint)
            return

    def _maybe_wilderness_monster(self, world: World, intent: dict, summary: dict,
                                  preset: Optional[WorldSimPreset], itype: str):
        """[P12] 野外遇怪判定（确定性 roll）。命中时 summary 带 forced_combat + 叙事提示。

        post_combat 打标的战后叙事回合跳过（防连环强制战斗：打完怪的事后旁白回合
        不再 roll 出下一只怪）。
        """
        if preset is None or intent.get("post_combat"):
            return
        combat_system = self._per_world(world, "combat_system",
                                        preset.combat_system if preset else "crpg", preset)
        if combat_system == "narrative":
            return
        # 强制战斗走 CombatDialog 回合制界面；老一击制（combat_player_controlled=False）
        # 无法承载临时怪物实体，跳过判定
        if not self._per_world(world, "combat_player_controlled",
                               preset.combat_player_controlled, preset):
            return
        if not self._per_world(world, "wilderness_monsters_enabled",
                               preset.wilderness_monsters_enabled, preset):
            return
        loc = self._current_location(world)
        if loc is None or getattr(loc, "kind", "wilderness") != "wilderness":
            return
        base = float(self._per_world(world, "wilderness_monster_chance",
                                     preset.wilderness_monster_chance, preset) or 0.0)
        # [P24d] 宠物预警被动：出战宠物亲和达标且种族被动为 alert 时遇怪率 x0.7
        if pex.passive_active(world, "alert"):
            base = base * 0.7
        elite = float(self._per_world(world, "elite_monster_chance",
                                      preset.elite_monster_chance, preset) or 0.0)
        rng = SeededRng.seed_from(world.id, int(world.tick_count or 0),
                                  f"wildmon_{loc.id}_{world.player.location_id}")
        from src.services import calendar_engine as _cale
        monster = we.roll_wilderness_monster(
            world, loc, world.player.level, True, base, elite, rng,
            is_gather=(itype == "gather"),
            season_mult=_cale.season_fx(world).get("monster_chance", 1.0))
        if monster is not None:
            summary["forced_combat"] = monster
            hint = f"野外暗处窜出{monster.name}拦住了去路（危险度{loc.danger}）！"
            # [!] intent 与 summary 双写（叙事 LLM 只读 intent.narration_hint——单写 summary
            # 旁白不会描写遇怪，战斗弹出无叙事铺垫）
            intent["narration_hint"] = ((intent.get("narration_hint") or "")
                                        + ("；" if intent.get("narration_hint") else "") + hint)
            summary["narration_hint"] = intent["narration_hint"]

    def _resolve_combat(self, world: World, scene: SceneLog, intent: dict, summary: dict,
                        preset: Optional[WorldSimPreset] = None):
        """战斗结算：玩家攻击 NPC -> NPC 反击 -> 掉落 -> 经验。回填 narration_hint 含数值。

        combat_system="narrative" 时降级 P2 纯叙事（不动数值）。
        """
        combat_system = self._per_world(world, "combat_system",
                                        preset.combat_system if preset else "crpg", preset)
        if combat_system == "narrative":
            # narrative 模式：combat 仅语义，数值由叙事 LLM 自由描写（P2 降级）
            summary["reason"] = "（叙事战斗模式，无数值结算）"
            return

        target_name = intent.get("talk_to", "").strip()
        if not target_name:
            summary["reason"] = "未指定攻击目标"
            intent["resolved"] = False
            return
        loc = self._current_location(world)
        if not loc:
            summary["reason"] = "无当前地点"
            intent["resolved"] = False
            return
        # 找在场 NPC（按名）。[P44] 候选集锚定 + 容错解析：LLM 常给目标名加修饰
        # （「独眼妖狼」「妖王大人」）或有一字之差，精确等值会静默打不到。
        npcs = self._npcs_at(world, loc)
        target = _npc_by_name(world, target_name, npcs=npcs)
        if target is None:
            # [A 回滚 2026-10-02 用户指示] 原「野外虚构目标 force 刷真怪接战」转化闸
            # 真机复测 OOC（打变异犬冒出人形掠夺者——怪物池按 danger 抽不认「犬」
            # 语义）已整套移除；保留分流提示语（别处真人/引导巡查）。
            anywhere = _npc_by_name(world, target_name, npcs=(world.npcs or []))
            if anywhere is not None:
                summary["reason"] = f"「{target_name}」不在当前地点（此刻在别处，须当面交手）"
            else:
                summary["reason"] = (f"「{target_name}」不在当前地点（野外可主动巡查搜寻猎物，"
                                     f"遭遇会真的找上门）")
            intent["resolved"] = False
            return
        if not target.hostile:
            summary["reason"] = f"「{target_name}」不是敌对目标，无法攻击"
            intent["resolved"] = False
            return
        if target.hp <= 0:
            summary["reason"] = f"「{target_name}」已经倒下"
            intent["resolved"] = False
            return

        # 玩家已倒下不能战斗
        if world.player.hp <= 0:
            summary["reason"] = "你已倒下，无法战斗"
            intent["resolved"] = False
            return

        difficulty = self._per_world(world, "difficulty",
                                     preset.difficulty if preset else "normal", preset)
        granularity = self._per_world(world, "crpg_granularity",
                                      preset.crpg_granularity if preset else "medium", preset)

        # 聚合玩家与 NPC 战斗属性（base + 装备 + 词缀）
        p_affixes = self._collect_equipped_affixes(world, world.player)
        p_w_atk, p_a_def = self._equipped_attack_defense(world, world.player)
        p_snap = ce.compute_stats(world.player, weapon_attack=p_w_atk, armor_defense=p_a_def,
                                  affixes=p_affixes,
                                  equip_stat_bonus=self._equipped_stat_bonus(world, world.player),
                                  hunger_mult=ce.hunger_stat_mult(world.player))
        # [A1 2026-08-29] NPC 装备聚合补洞（原「NPC 暂无装备」三层空洞之一：快速战斗）：
        # 与玩家/同伴同口径聚合 equipped 攻防/词缀/装备属性，难度乘数外裹不变。
        t_affixes = self._collect_equipped_affixes(world, target)
        t_w_atk, t_a_def = self._equipped_attack_defense(world, target)
        t_snap = ce.apply_monster_difficulty(
            ce.compute_stats(target, weapon_attack=t_w_atk, armor_defense=t_a_def,
                             affixes=t_affixes,
                             equip_stat_bonus=self._equipped_stat_bonus(world, target)),
            difficulty)

        # 种子 RNG（同 world+tick+attack 可复现）
        rng = SeededRng.seed_from(world.id, world.tick_count, "player_attack")

        # 玩家攻击 NPC
        atk_r = ce.resolve_attack(p_snap, t_snap, rng, difficulty=difficulty,
                                  granularity=granularity, is_player_attacker=True)
        ce.apply_damage(target, atk_r.damage)
        parts = []
        # [P47 后果层] 代价三件套（与 finish_combat 同口径，守「两路径一致」契约）
        defeat_toll: Optional[dict] = None
        reclaimed: list[str] = []
        reclaimed_back: list[str] = []      # 实际追回的（入包后对账，见 settle_reclaimed）
        witness_lines: list[str] = []
        _part_dmg: dict = {}                # [P52 伤疤] 玩家部位受击累计（快速战斗局部）
        if not atk_r.hit:
            parts.append(f"你的攻击未命中{target.name}")
        else:
            crit_txt = "暴击！" if atk_r.crit else ""
            parts.append(f"你{crit_txt}命中{target.name}，造成 {atk_r.damage} 点伤害（{target.name} HP {target.hp}/{target.hp_max}）")

        # [P58] 掉落改由 _collect_loot_grid 收集进 summary["loot_grid"]（不自动入包）
        xp_gain = 0
        leveled = False
        new_level = world.player.level
        # [既有 bug 修复 2026-09-25] 默认值：打赢「主线发布人」时 mg_spared_fast 分支会跳过
        # 整段 won 结算（掉落/经验/金币都不发），combat_gold2 不会被赋值；而下方 summary
        # 在 defender_defeated 为真时读它 -> NameError（快速战斗打死敌对主线发布人即触发）。
        combat_gold2 = 0
        if atk_r.defender_defeated:
            parts.append(f"{target.name}被击败")
            # [!] 快速战斗路径同样须置 alive=False + respawn（守 finish_combat 同口径，
            # 否则尸体继续活动且玩家击杀永不重生）
            # [P45 v3 2026-09-12 用户定稿] 死亡统一由 npc_permadeath 决定（原「要角
            # respawn=0 永久」特判已删）；主线发布人不死（quest_engine.is_main_giver）。
            # [P45 v3.1 审核修复] 主线发布人存活时**跳过全部 won 结算**（xp/金币/掉落/搜刮/
            # 图鉴/宠物奖励一律不发）——否则敌对主线发布人可被无限刷成无风险收益泵
            # （审核 review-qwen P1）。
            mg_spared_fast = qe.is_main_giver(world, target)
            if mg_spared_fast:
                target.hp = 1      # 主线气运护体：重伤濒死但留活口
                parts.append(f"{target.name}身负主线气运，重伤倒地却吊住了一口气"
                             f"（未死，无战利品可搜）")
            else:
                target.alive = False
                target.respawn_at_tick = world.tick_count + rng.roll(8, 14)
                qtxt = qe.cleanup_dead_giver_quests(world, target)
                if qtxt:
                    parts.append(qtxt)
                # [修 2026-09-05] 快速战斗补记图鉴击败 + 死亡信息（旧只记掉落，口径缺口）
                self._record_codex_kill(world, target, "被你击败")
                # [P47-B 后果层] 目击者记恨（与 finish_combat 同口径）
                witness_lines = cue.witness_grudge(world, target)
                _ev = cue.murder_event(world, target, witness_count=len(witness_lines))
                if _ev is not None:
                    world.event_log.append(_ev)
                    witness_lines.append("这件事恐怕很快就会传开")
            if not mg_spared_fast:
                # [P58 摸格子] 掉落收集（与 finish_combat won 分支共用 _collect_loot_grid
                # 单一来源：天赋 luck/loot_mult + 稀有度升档 + 敌对保底 + 搜尸 + 夺回 +
                # 未鉴定克隆——守「两路径一致」契约）。**不自动入包**：进 summary["loot_grid"]
                # 由玩家在场景页战后的 LootGridDialog 拾取。
                grid = self._collect_loot_grid(world, target, difficulty)
                drop_names = [e["name"] for e in grid["entries"]]
                if drop_names:
                    parts.append(f"战利品：{'、'.join(drop_names)}（战后摸格子拾取）")
                # 经验
                xp_gain = int((max(1, target.level) * 15) * ce.xp_difficulty_mult(difficulty))
                xp_r = ce.gain_xp(world.player, xp_gain)
                leveled = xp_r.leveled_up
                new_level = xp_r.new_level
                # [数值补全] 快速战斗同样发金币（与 finish_combat 同口径，守两路径一致）
                gold_rng2 = SeededRng.seed_from(world.id, world.tick_count, f"combat_gold_{target.id}")
                base_gold2 = max(1, target.level) * gold_rng2.roll(3, 8)
                combat_gold2 = int(base_gold2 * tre.gold_gain_mult(tre.world_pace(world)))
                world.player.gold = int(getattr(world.player, "gold", 0) or 0) + combat_gold2
                _gt_cur = self._genre_text(world).currency  # §23 币种走 GenreText
                if leveled:
                    # 升级回满 HP
                    self._level_up_recalc(world)
                    parts.append(f"获得 {xp_gain} 经验、{combat_gold2} {_gt_cur}，升级到 {new_level} 级，HP/MP 回满")
                else:
                    parts.append(f"获得 {xp_gain} 经验、{combat_gold2} {_gt_cur}（{world.player.xp}/{world.player.xp_next}）")
                summary["loot_grid"] = grid
                # [P24d] 出战宠物 xp + 临时野怪低概率遇幼崽（与 finish_combat won 同口径）
                self._pet_victory_rewards(world, target, parts, summary)

        # NPC 反击（若未阵亡）
        dmg_taken = 0
        if not atk_r.defender_defeated and target.hp > 0:
            rng2 = SeededRng.seed_from(world.id, world.tick_count, "npc_counter")
            t_snap2 = replace(t_snap, hp=target.hp, hp_max=target.hp_max)
            # [P7e] 玩家防御快照须带 dodge/parry，否则受击无法闪避/招架
            # 完整复制快照，包含头伤受暴击率、元素与概率词缀，避免新增属性漏拷。
            p_snap2 = replace(p_snap, hp=world.player.hp)
            cnt_r = ce.resolve_attack(t_snap2, p_snap2, rng2, difficulty=difficulty,
                                      granularity=granularity, is_player_attacker=False)
            ce.apply_damage(world.player, cnt_r.damage)
            if cnt_r.hit:
                # [P52 伤疤] 部位累计（快速战斗无 session，用局部 dict）
                _p = str(getattr(cnt_r, "body_part", "") or "")
                if _p:
                    _part_dmg[_p] = _part_dmg.get(_p, 0) + cnt_r.damage
            dmg_taken = cnt_r.damage
            if not cnt_r.hit:
                parts.append(f"{target.name}的反击未命中你")
            else:
                crit_txt = "暴击！" if cnt_r.crit else ""
                parts.append(f"{target.name}{crit_txt}反击造成 {cnt_r.damage} 点伤害（你 HP {world.player.hp}/{world.player.hp_max}）")
            if world.player.hp <= 0:
                # [P47 修复 2026-09-25] 复活丹自动生效：有则消耗并原地站起（HP 按丹效恢复，
                # 不判负、本回合结束，下一回合可继续打）。与 CombatDialog._finish 的拦截同口径。
                _rev = ce.try_auto_revive(world, world.player)
                if _rev:
                    parts.append(f"「{_rev}」在你怀中碎裂，一股暖流托着你重新站起"
                                 f"（HP {world.player.hp}/{world.player.hp_max}）")
                else:
                    parts.append("你已倒下！")
                    # [P10c] 无条件上报 player_defeated（与 finish_combat lost 分支口径一致，供调用方
                    # 据此 + permadeath 开关分流）。永久死亡关闭 -> 复活（与 finish_combat 对称）；
                    # 快速战斗不走 CombatDialog/finish_combat，须在此独立复活，否则玩家卡在 HP 0
                    # （apply_intent combat 守卫会拒绝后续战斗，世界无法推进）。
                    summary["player_defeated"] = True
                    pd = self._per_world(world, "permadeath_enabled",
                                         preset.permadeath_enabled if preset else False, preset)
                    if not pd:
                        spawn_name = self._revive_at_spawn(world)
                        if spawn_name:
                            parts.append(f"你在出生地「{spawn_name}」苏醒，捡回一条命（HP 1）")
                            summary["revived_at_spawn"] = spawn_name
                        # [P47-A 后果层] 败北代价（与 finish_combat 同口径同 salt 命名）
                        rng_toll = SeededRng.seed_from(world.id, world.tick_count,
                                                       f"toll_{getattr(target, 'id', '')}")
                        defeat_toll = cue.took_from_player(
                            world, target, rng_toll,
                            is_temp_monster=not any(n is target for n in (world.npcs or [])))
                        if defeat_toll["gold"]:
                            summary["lost_gold"] = defeat_toll["gold"]
                        if defeat_toll["items"]:
                            summary["lost_items"] = list(defeat_toll["items"])
                        # [P47-C 赃物闭环 a] 劫掠者记住这票买卖（进 chat_notes，下次记忆整理
                        # 并入正式记忆——私聊/召回自然带出，被抢的玩家可能听到他吹嘘）
                        _notes = []
                        if defeat_toll["gold"]:
                            _notes.append("一笔钱")
                        if defeat_toll["items"]:
                            _notes.append("几件随身物")
                        if _notes:
                            try:
                                self.npc_memory().record_chat_note(
                                    world, getattr(target, "id", ""),
                                    "我从那个玩家身上抢了" + "和".join(_notes) + "，别声张")
                            except Exception:
                                pass
                        # [P52 伤疤] 战败留痕（与 finish_combat 同口径）
                        rng_inj = SeededRng.seed_from(world.id, world.tick_count,
                                                      f"injury_{getattr(target, 'id', '')}")
                        _inj = ce.roll_injuries(world, world.player, _part_dmg,
                                                max(1, world.player.hp_max), rng_inj,
                                                min_one=True)
                        if _inj:
                            summary["injuries"] = _inj

        # [P47 后果层] 代价三件套最末追加，各合并成一条（与 finish_combat 同口径；
        # 本路径 narration_hint 不截断，保持两路径文案形状一致便于回归对比）
        if defeat_toll and defeat_toll["lines"]:
            summary["lost_text"] = "；".join(defeat_toll["lines"])
            parts.append(summary["lost_text"])
        if reclaimed_back:
            _rnames = "、".join(next((i.name for i in world.items if i.id == did), did)
                                for did in reclaimed_back)
            parts.append(f"你从{target.name}身上夺回了被抢走的：{_rnames}")
        if reclaimed and target.alive is False:
            _sold = cue.missing_stolen(target, reclaimed_back)
            if _sold:
                _snames = "、".join(next((i.name for i in world.items if i.id == did), did)
                                    for did in _sold)
                parts.append(f"至于他早前抢走的{_snames}——早已被他转手卖掉了")
        if witness_lines:
            _wl = "；".join(witness_lines[:2])
            if len(witness_lines) > 2:
                _wl += f"（另有 {len(witness_lines) - 2} 人也目睹了这一幕）"
            parts.append(_wl)
        # 回填 narration_hint（供叙事 LLM 据实描写，不编造数字）
        hint = "；".join(parts)
        existing = intent.get("narration_hint", "") or ""
        intent["narration_hint"] = (existing + " | " + hint) if existing else hint

        # [P58 摸格子] items_dropped 改从 loot_grid 取（快速战斗不再自动入包）
        _grid = summary.get("loot_grid") or {}
        _grid_items = [e.get("item_id", "") for e in (_grid.get("entries") or [])]
        summary.update({
            "damage_dealt": atk_r.damage,
            "damage_taken": dmg_taken,
            "target_npc_id": target.id,
            "target_npc_defeated": atk_r.defender_defeated,
            "xp_gained": xp_gain,
            "gold_gained": combat_gold2 if atk_r.defender_defeated else 0,
            "leveled_up": leveled,
            "new_level": new_level,
            "items_dropped": _grid_items,
            "loot_text": "、".join(
                (next((i.name for i in world.items if i.id == did), did) for did in _grid_items)
            ) if _grid_items else "",
        })

    # ================================================================
    # ====== [P7h] 回合制战斗（玩家可操作，CombatDialog 调用）======
    # ================================================================
    def start_combat(self, world: World, target_npc, preset: Optional[WorldSimPreset] = None,
                     allies: Optional[list] = None, minion_specs: Optional[list] = None):
        """[P7h] 创建回合制战斗会话（玩家 vs target_npc）。

        返回 ce.CombatSession（含双方聚合 snapshot + 技能 + mp + ai + 难度/粒度/回合上限）。
        纯 Python 聚合，无 LLM。CombatDialog 每回合调 player_action/ally_turn/enemy_turn 驱动。

        [P10] 多敌 + 助战：
        - allies：助战同伴 NPC 列表（同场景高交情 NPC，judge_encounter 选出）。
        - minion_specs：随从规格列表 [{"name": str, "role": str}]（LLM/引擎判定的增援杂兵；
          role 为 [P24d] 可选 speed/tank/balanced，非法/缺省走题材关键词兜底；None=单敌）。
          随从是主目标的战斗本地克隆（属性约 7 成、HP 约 6 成），不入世界 NPC 表；
          战斗结束按各自 loot_table 掉落、给经验。enemy_units[0] 是主敌包装（与 legacy
          字段共享引用），[1:] 是随从。
        """
        difficulty = self._per_world(world, "difficulty",
                                     preset.difficulty if preset else "normal", preset)
        granularity = self._per_world(world, "crpg_granularity",
                                      preset.crpg_granularity if preset else "medium", preset)
        # 玩家聚合（base + 装备 + 词缀）
        p_affixes = self._collect_equipped_affixes(world, world.player)
        p_w_atk, p_a_def = self._equipped_attack_defense(world, world.player)
        p_snap = ce.compute_stats(world.player, weapon_attack=p_w_atk, armor_defense=p_a_def,
                                  affixes=p_affixes,
                                  equip_stat_bonus=self._equipped_stat_bonus(world, world.player),
                                  hunger_mult=ce.hunger_stat_mult(world.player))
        # [A1 2026-08-29] NPC 装备聚合补洞（原「NPC 暂无装备」三层空洞之二：start_combat
        # 主敌快照）。难度乘数外裹不变（装备先聚合再吃难度，口径同快速战斗路径）。
        t_w_atk, t_a_def = self._equipped_attack_defense(world, target_npc)
        t_snap = ce.apply_monster_difficulty(
            ce.compute_stats(target_npc,
                             weapon_attack=t_w_atk, armor_defense=t_a_def,
                             affixes=self._collect_equipped_affixes(world, target_npc),
                             equip_stat_bonus=self._equipped_stat_bonus(world, target_npc)),
            difficulty)
        max_rounds = self._per_world(world, "combat_max_rounds",
                                     preset.combat_max_rounds if preset else 30, preset)
        # 多敌时回合上限按敌方数量放宽（每多一个敌人 +30%）
        # [P25a] 秘境 Boss 保底 1-2 小弟（judge LLM 关闭/未给随从时确定性派生；judge 有输出以其为准）
        dtag = getattr(target_npc, "dungeon_tag", None)
        if isinstance(dtag, dict) and dtag.get("boss") and not (minion_specs or []):
            rng_m = SeededRng.seed_from(world.id, world.tick_count, f"dgn_minions_{target_npc.id}")
            minion_specs = [{"name": f"{target_npc.name}的爪牙{i + 1}"}
                            for i in range(rng_m.roll(1, 2))]
        n_minions = len(minion_specs or [])
        if n_minions > 0:
            max_rounds = int(max_rounds * (1.0 + 0.3 * n_minions))
        # [技能页 2026-08-28] 出战技能栏过滤：equipped_skills 非空时战斗只出已装备技能
        # （用户定稿 3 槽）；空 list = 老档兼容全技能显示。
        _all_sk = [s for s in (world.player.skills or []) if isinstance(s, dict)]
        _eq_ids = {str(x) for x in (getattr(world.player, "equipped_skills", None) or [])}
        _battle_sk = ([s for s in _all_sk
                       if str(s.get("id") or s.get("name") or "") in _eq_ids]
                      if _eq_ids else _all_sk)
        session = ce.CombatSession(
            player=p_snap, enemy=t_snap,
            player_skills=_battle_sk,
            enemy_skills=[s for s in (target_npc.skills or []) if isinstance(s, dict)],
            enemy_ai=(target_npc.ai_pattern if target_npc.ai_pattern in
                      ("aggressive", "defensive", "caster", "balanced") else "balanced"),
            player_mp=max(0, world.player.mp),
            enemy_mp=max(0, target_npc.mp),
            # [修 2026-09-10] MP 上限注入（对撞「守住回 2% max MP」用）：玩家/主敌是 bare
            # 快照，快照上没有 mp_max，原先该奖励恒为 0 永不发放。
            player_mp_max=max(0, int(getattr(world.player, "mp_max", 0) or 0)),
            enemy_mp_max=max(0, int(getattr(target_npc, "mp_max", 0) or 0)),
            player_cd={},  # [P7h] 每场战斗独立冷却（标准 CRPG，不跨战斗保留）
            player_talents=[t for t in (world.player.talents or []) if isinstance(t, dict)],  # [P9]
            enemy_talents=[t for t in (target_npc.talents or []) if isinstance(t, dict)],     # [P9]
            difficulty=difficulty, granularity=granularity,
            max_rounds=max(5, int(max_rounds)),
            genre_id=self._genre_id(world),  # [P30] 状态显示名题材化
            world_id=world.id,  # [技能熟练随机 2026-09-10] 施展熟练度随机加点盐源
        )
        # ---- [P10] 敌方单位表：[0] 主敌包装（快照/cd 与 legacy 字段同引用）+ 随从 ----
        main_unit = ce.CombatUnit(
            name=target_npc.name, npc_id=target_npc.id, snapshot=t_snap,
            mp=max(0, target_npc.mp), mp_max=max(0, target_npc.mp_max),
            skills=session.enemy_skills, cd=session.enemy_cd, talents=session.enemy_talents,
            ai=session.enemy_ai, role_label="主敌",
        )
        # [P30] 主敌状态列表共享 session.enemy_conditions（仿 snapshot/cd 同引用口径）：
        # 读方（_main_enemy_act/player_action/resolve_skill caster_conds/UI）读 session.enemy_conditions，
        # 写方（_resolve_skill_targets/_unit_cast_skill 按 snapshot 身份路由）写 main_unit.conditions；
        # 两者须同一 list 对象，否则主敌控制/减益/增益失效。
        main_unit.conditions = session.enemy_conditions
        enemy_units = [main_unit]
        for i, spec in enumerate(minion_specs or []):
            spec = spec if isinstance(spec, dict) else {}
            m_name = str(spec.get("name") or f"{target_npc.name}的随从{i + 1}").strip()
            m_role = str(spec.get("role") or "").strip()
            if m_role not in _MINION_ROLE_VALUES:
                m_role = _guess_minion_role(self._genre_id(world), m_name)
            enemy_units.append(
                self._build_minion_unit(world, target_npc, m_name, difficulty, granularity, role=m_role))
        session.enemy_units = enemy_units
        # ---- [P10] 助战同伴单位表 ----
        session.ally_units = [self._build_ally_unit(world, n) for n in (allies or [])]
        # ---- [P24d] 出战宠物自动入阵（ally 单位，不占 companion 位；数值走种族模板）----
        pet = pex.active_pet(world)
        if pet is not None:
            session.ally_units.append(pex.build_pet_unit(world, pet))
        # [P26a] 结义联手加成预注入：ally 中 sworn 阶段 NPC 的 npc_id 集合 + 倍率（供普攻加成）
        try:
            ally_ids = [u.npc_id for u in session.ally_units if getattr(u, "npc_id", "")]
            session.sworn_npc_ids = reng.sworn_npc_ids_of(world, ally_ids)
            if session.sworn_npc_ids:
                session.sworn_damage_mult = reng.sworn_damage_mult(
                    world, session.sworn_npc_ids, preset)
        except Exception:
            pass
        # [P40] 战斗环境（70% 概率抽一个题材化险地入阵，确定性 seed）+ Boss 战判定
        #（dungeon boss 标记 / level>=5：启用 66%/33% 阶段机制）
        rng_env = SeededRng.seed_from(world.id, world.tick_count, f"env_{target_npc.id}")
        if rng_env.chance(0.7):
            session.environment = rng_env.pick(ce._ENV_TYPES)
        session.boss_fight = bool(isinstance(dtag, dict) and dtag.get("boss")) \
            or int(getattr(target_npc, "level", 0) or 0) >= 5
        # [Boss 机制库 2026-08-29] WorldBoss 机制初始化（target_npc.mechanics 由
        # prepare_combat 透传；transient 不落盘；须在 boss_fight 赋值后）
        _mechs = [str(x) for x in (getattr(target_npc, "mechanics", None) or [])
                  if x in ("shield", "summon_tide", "enrage_timer")]
        if _mechs and session.boss_fight:
            session.boss_mechanics = _mechs
            if "shield" in _mechs:
                session.boss_shield_max = max(1, int(t_snap.hp_max * 0.4))
                session.boss_shield = session.boss_shield_max
            if "enrage_timer" in _mechs:
                session.boss_enrage_round = 5
        # ---- [部位+布局 2026-08-31] 弱点 roll + 2-1-2 前后排布局 ----
        # 每个敌方单位（含主敌/随从）确定性 roll 一个部位弱点，写 CombatUnit.weakpoint
        # 与快照 weakpoint（resolve_attack 直读快照字段）。玩家排位 roll（player_row）；
        # 敌我双方单位经 assign_layout 随机 2-1-2 混排（同 world+tick 确定性）。
        rng_wp = SeededRng.seed_from(world.id, world.tick_count, f"wp_{target_npc.id}")
        for u in session.enemy_units:
            u.weakpoint = ce.roll_weakpoint(world.id, u.name, world.tick_count)
            u.snapshot.weakpoint = u.weakpoint
        session.player_row = 2 if rng_wp.chance(0.5) else 1
        ce.assign_layout(list(session.enemy_units or []), rng_wp)
        ce.assign_layout(list(session.ally_units or []), rng_wp)
        # 敌方立绘/头像：主敌经 cached_combat_image（NPC.avatar 优先，怪物图缓存
        # combat_{key}.png 兜底，游玩中不现场渲染）；随从无图（UI 占位绘制）。
        _main_img = self.cached_combat_image(target_npc) or ""
        for u in session.enemy_units:
            u.avatar = _main_img if not u.is_minion else ""
        for u in session.ally_units:
            _npc = next((n for n in world.npcs if n.id == u.npc_id), None)
            u.avatar = str(getattr(_npc, "avatar", "") or "") if _npc is not None else ""
        return session

    def _build_minion_unit(self, world: World, target_npc, name: str,
                           difficulty: str, granularity: str,
                           role: str = "balanced") -> "ce.CombatUnit":
        """[P10] 构建随从单位：主目标的战斗本地克隆（属性约 7 成、HP 约 6 成）。

        数值由 SeededRng 确定性缩放（同 world+tick+name 同结果），不经 LLM（守数值范式）。
        掉落表继承主目标（finish_combat 按半 rate roll，无保底）。
        [P24d] role 差异化（白名单 speed/tank/balanced，非法回退 balanced）：
        - balanced：既有基线（五维 x0.7 + HP x0.6）。
        - speed：基线上 stat_dex 再 x1.3（dex 驱动 speed/dodge/crit）、HP 再 x0.8。
        - tank：基线上 HP 再 x1.3、compute_stats 后 atk x0.8。
        """
        rng = SeededRng.seed_from(world.id, world.tick_count, f"minion_{name}")
        if role not in _MINION_ROLE_VALUES:
            role = "balanced"
        base_lvl = max(1, int(getattr(target_npc, "level", 1) or 1))
        lvl = max(1, base_lvl - 1)
        dex_mult = 1.3 if role == "speed" else 1.0
        clone = NPC(
            name=name, role=getattr(target_npc, "role", "") or "",
            level=lvl,
            stat_str=max(3, int(getattr(target_npc, "stat_str", 5) * 0.7)),
            stat_dex=max(3, int(getattr(target_npc, "stat_dex", 5) * 0.7 * dex_mult)),
            stat_int=max(3, int(getattr(target_npc, "stat_int", 5) * 0.7)),
            stat_vit=max(3, int(getattr(target_npc, "stat_vit", 5) * 0.7)),
            stat_luk=max(3, int(getattr(target_npc, "stat_luk", 5) * 0.7)),
            skills=[dict(s) for s in (getattr(target_npc, "skills", []) or []) if isinstance(s, dict)],
            ai_pattern=getattr(target_npc, "ai_pattern", "balanced") or "balanced",
            # [P9] 随从继承主敌元素构成（同源克隆，克制口径一致）
            elements=list(getattr(target_npc, "elements", None) or []),
        )
        snap = ce.compute_stats(clone)
        hp_mult = 0.6 * (0.8 if role == "speed" else 1.3 if role == "tank" else 1.0)
        snap.hp = max(1, int(snap.hp_max * hp_mult))
        snap.hp_max = max(1, int(snap.hp_max * hp_mult))
        if role == "tank":
            snap.atk = max(1, int(snap.atk * 0.8))
        # [P 验收] 随从同主敌吃怪物侧难度乘数（HP/攻/防）
        snap = ce.apply_monster_difficulty(snap, difficulty)
        mp_max = max(0, int(getattr(target_npc, "mp_max", 0) or 0) * 0.6)
        return ce.CombatUnit(
            name=name, snapshot=snap, mp=mp_max, mp_max=mp_max,
            skills=clone.skills, cd={}, talents=[t for t in (getattr(target_npc, "talents", []) or []) if isinstance(t, dict)],
            ai=clone.ai_pattern, role_label=_MINION_ROLE_LABELS.get(role, "随从"), is_minion=True,
            loot_table=[e for e in (getattr(target_npc, "loot_table", []) or []) if isinstance(e, dict)],
        )

    def _build_ally_unit(self, world: World, npc) -> "ce.CombatUnit":
        """[P10] 构建助战同伴单位：真实 NPC（装备/天赋/技能全聚合），战后 hp/mp 写回。"""
        affixes = self._collect_equipped_affixes(world, npc)
        w_atk, a_def = self._equipped_attack_defense(world, npc)
        snap = ce.compute_stats(npc, weapon_attack=w_atk, armor_defense=a_def, affixes=affixes,
                                equip_stat_bonus=self._equipped_stat_bonus(world, npc))
        return ce.CombatUnit(
            name=npc.name, npc_id=npc.id, snapshot=snap,
            mp=max(0, npc.mp), mp_max=max(0, npc.mp_max),
            skills=[s for s in (npc.skills or []) if isinstance(s, dict)],
            cd={}, talents=[t for t in (npc.talents or []) if isinstance(t, dict)],
            ai=(npc.ai_pattern if npc.ai_pattern in ("aggressive", "defensive", "caster", "balanced")
                else "balanced"),
            role_label="同伴",
        )

    def assist_candidates(self, world: World, target_npc, preset: Optional[WorldSimPreset] = None) -> list:
        """[P10] 助战候选：与玩家同场景、存活、非敌对、有战斗能力（level>0）、交情>=40、非战斗目标。

        [P10b] 同行 NPC 排在候选前列（受 combat_max_allies 上限时优先入选；
        确定性兜底 roll 也对同行 +0.3 概率）。
        [P25b] 战斗疲劳中的同伴不参战（is_fatigued）。
        """
        if preset is not None and not self._per_world(world, "combat_allies_enabled",
                                                      preset.combat_allies_enabled, preset):
            return []
        loc = self._current_location(world)
        if loc is None:
            return []
        comp_ids = set(getattr(world.player, "companion_npc_ids", None) or [])
        out = []
        for n in world.npcs:
            if n.id == target_npc.id or n.location_id != loc.id:
                continue
            if getattr(n, "hostile", False) or not getattr(n, "alive", True):
                continue
            if int(getattr(n, "level", 0) or 0) <= 0:
                continue  # 无战斗能力的和平 NPC 不参战
            if int(getattr(n, "affinity", 0)) < 40:
                continue
            # [P47-C2 记恨闸] 恨你的人不会为你拼命（trust <= -60；三模型共识：
            # 记恨账本接消费出口——助战是含金量最高的一个）
            if cue.trust_gate(n):
                continue
            if nre.is_fatigued(world, n):
                continue  # [P25b] 疲劳休整中
            # [P39a] 社交门：与已入选同伴互为仇敌（social <= -60）的 NPC 不同时入选
            if any(soc.social_stage(int((getattr(x, "social", None) or {})
                                        .get(str(n.id), 0) or 0)) == "sworn_enemy"
                   for x in out):
                continue
            out.append(n)
        # [P10b] 同行优先（稳定排序：同行在前，组内保持原顺序）
        out.sort(key=lambda n: 0 if n.id in comp_ids else 1)
        return out

    def judge_encounter(
            self, world: World, target_npc, preset: Optional[WorldSimPreset] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """[P10] 遭遇判定：敌人数量（含随从名）+ 谁来助战。

        LLM 出语义（一次 JSON 调用：据场景危险度/敌我态势/同伴交情判断规模与助战人选），
        引擎算结构（数量钳制、候选校验、随从克隆数值）。LLM 失败/关闭/取消回退确定性
        roll（ce.roll_encounter_size + ce.roll_assist_allies，守数值范式）。
        返回 {"enemy_count", "minion_specs": [{"name", "role"}], "allies": [NPC], "note": str}
        （[P24d] role = speed/tank/balanced，minion_roles 可选字段缺失时关键词兜底）。
        """
        max_enemies = int(self._per_world(world, "combat_max_enemies",
                                          preset.combat_max_enemies if preset else 3, preset) or 3)
        max_enemies = max(1, min(5, max_enemies))
        max_allies = int(self._per_world(world, "combat_max_allies",
                                         preset.combat_max_allies if preset else 2, preset))
        candidates = self.assist_candidates(world, target_npc, preset)
        rng = SeededRng.seed_from(world.id, world.tick_count, f"encounter_{target_npc.id}")
        loc = self._current_location(world)

        def _fallback(note: str) -> dict:
            count = ce.roll_encounter_size(rng, getattr(loc, "danger", 1) if loc else 1,
                                           target_npc.level, world.player.level,
                                           max_enemies=max_enemies)
            allies = ce.roll_assist_allies(
                rng, candidates, target_npc.level, max_allies=max_allies,
                companion_ids=set(getattr(world.player, "companion_npc_ids", None) or [])) if candidates else []
            gid = self._genre_id(world)
            specs = [{"name": f"{target_npc.name}的增援{i + 1}",
                      "role": _guess_minion_role(gid, f"{target_npc.name}的增援{i + 1}")}
                     for i in range(max(0, count - 1))]
            return {"enemy_count": count, "minion_specs": specs, "allies": allies, "note": note}

        # LLM 判定关闭 / 无候选也无增援空间 -> 直接确定性兜底
        llm_on = preset is not None and self._per_world(world, "combat_encounter_judge_llm",
                                                        preset.combat_encounter_judge_llm, preset)
        if not llm_on or max_enemies <= 1:
            return _fallback("")
        api = self._resolve_api(preset.calculator_api_id if preset else "")
        if api is None:
            return _fallback("")
        cand_lines = []
        for n in candidates:
            rel = nre.affinity_level(n.affinity)
            comp_tag = "；玩家的同行伙伴" if nre.is_companion(world, n) else ""
            cand_lines.append(f"- {n.name}（身份:{n.role or '未知'}；等级:{n.level}；"
                              f"交情:{rel}({n.affinity}){comp_tag}；性格:{n.personality or '未知'}）")
        sys_prompt = (
            "你是 SLG 游戏的遭遇判定引擎。玩家即将与某敌人开战，我会给你战场态势"
            "（地点危险度、敌人、玩家、在场可能助战的同伴），请你判断遭遇规模与助战人选，"
            "输出严格符合 schema 的 JSON。\n\n"
            "只输出 JSON，不要输出任何说明、解释、前后缀或 markdown 代码块标记。\n\n"
            "JSON schema：\n"
            "{\n"
            '  "enemy_count": 敌人总数(1到给定上限的整数，含主目标；据危险度/敌人习性判断，'
            '多数情况 1-2，敌众我寡或围攻场景才 3+),\n'
            '  "minion_names": ["增援敌人的名字(数量 = enemy_count-1，题材化命名，如「妖狼幼崽」「山贼喽啰」；'
            'enemy_count=1 时留空数组)"],\n'
            '  "minion_roles": {"增援敌人的名字": "该随从的定位(只能填 speed/tank/balanced 之一：'
            'speed=敏捷型(斥候/刺客/游侠类，迅捷但脆弱)、tank=重装型(卫士/力士类，耐打但输出低)、'
            'balanced=均衡型；按名字与敌人习性判定，每个随从名都要给；enemy_count=1 时留空对象)"},\n'
            '  "assist_names": ["愿意助战的同伴名(只能从候选名单选，可空数组；交情高/等级够/性格仗义才更可能助战；'
            '标注「玩家的同行伙伴」的应几乎必定助战)"],\n'
            '  "reason": "1句判定理由"\n'
            "}\n\n"
            "要求：名称必须用我给出的真实名称（同伴只能从候选名单选）；判断贴合敌人习性"
            "（狼群/匪帮会成群，独行的首领常单打独斗）与同伴性格交情。"
        )
        tmp = Preset(name="world_sim_encounter_judge", system_prompt=sys_prompt,
                     temperature=preset.calculator_temperature if preset else 0.3,
                     max_tokens=max(10000, preset.calculator_max_tokens if preset else 10000), top_p=0.9)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())
        user = (
            # [修 2026-09-10] 补世界观锚：本判定会命名题材化增援怪（妖狼幼崽/山贼喽啰），
            # 缺锚是题材串味的直接通道（一致性缺口，非新增调用）。
            _world_anchor(world) + "\n"
            f"地点：{loc.name if loc else '未知'}（危险度 {getattr(loc, 'danger', 1) if loc else 1}）\n"
            f"主目标敌人：{target_npc.name}（身份:{target_npc.role or '未知'}；等级:{target_npc.level}；"
            f"性格:{target_npc.personality or '未知'}）\n"
            f"玩家：等级 {world.player.level}（HP {world.player.hp}/{world.player.hp_max}）\n"
            f"敌人总数上限：{max_enemies}\n"
            f"助战候选名单（最多选 {max_allies} 个）：\n" + ("\n".join(cand_lines) if cand_lines else "（无）")
        )
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user}]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return _fallback("")
            try:
                result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                             block_labels=["系统", "用户"])
            except Exception:
                return _fallback("")
            if result.cancelled:
                return _fallback("")
            if result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return _fallback("")
            data = _extract_json(result.content)
            if not (isinstance(data, dict) and "enemy_count" in data):
                if attempt == 0:
                    # 回灌错误输出（与 _generate_shop_goods_llm/_llm_reconcile 同口径，提高重试命中率）
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 enemy_count 字段，"
                                                    "请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return _fallback("")
            count = _safe_int(data.get("enemy_count"), 1, 1, max_enemies)
            raw_names = data.get("minion_names")
            names = [str(x).strip() for x in raw_names if str(x).strip()] if isinstance(raw_names, list) else []
            # [P24d] minion_roles 可选字段：{随从名 -> speed/tank/balanced}，白名单清洗 +
            # 漏名/非法值走题材关键词兜底（与 _fallback 同口径）。
            raw_roles = data.get("minion_roles")
            roles = {str(k).strip(): str(v).strip() for k, v in raw_roles.items()} \
                if isinstance(raw_roles, dict) else {}
            gid = self._genre_id(world)
            specs = []
            for i in range(max(0, count - 1)):
                m_name = names[i] if i < len(names) else f"{target_npc.name}的增援{i + 1}"
                m_role = roles.get(m_name, "")
                if m_role not in _MINION_ROLE_VALUES:
                    m_role = _guess_minion_role(gid, m_name)
                specs.append({"name": m_name, "role": m_role})
            raw_assist = data.get("assist_names")
            assist_names = {str(x).strip() for x in raw_assist if str(x).strip()} if isinstance(raw_assist, list) else set()
            allies = [n for n in candidates if n.name in assist_names][:max(0, max_allies)]
            note = str(data.get("reason", "") or "").strip()
            return {"enemy_count": count, "minion_specs": specs, "allies": allies, "note": note}
        return _fallback("")

    def _pet_victory_rewards(self, world: World, target_npc, parts: list, summary: dict):
        """[P24d] 战斗胜利宠物结算（finish_combat won 与快速战斗击败两路径共用）：

        - 出战宠物获 xp（敌 level x2，pet_engine.gain_pet_xp），升级信息入叙事行。
        - 目标为临时野怪（不在 world.npcs，与图鉴口径一致）时低概率遇幼崽：复用
          tame_check（自动投食），成功 create_pet 入队。满员不 roll（省 rng 序列）。
        确定性：rng 种子 world+tick+petcub_{npc_id}。best-effort 不阻断战斗结算。
        """
        try:
            pet = pex.active_pet(world)
            if pet is not None:
                gained = max(1, int(getattr(target_npc, "level", 1) or 1)) * pex.PET_BATTLE_XP_PER_LEVEL
                levels = pex.gain_pet_xp(pet, gained)
                line = f"宠物「{pet.name}」获得 {gained} 经验"
                if levels > 0:
                    line += f"，升到 {pet.level} 级"
                parts.append(line)
                summary["pet_xp"] = {"name": pet.name, "gained": gained, "level": pet.level}
            if pex.pets_full(world):
                return
            if any(n.id == target_npc.id for n in world.npcs):
                return  # 世界内 NPC 战斗不遇幼崽（只认野外临时怪）
            rng = SeededRng.seed_from(world.id, int(world.tick_count or 0), f"petcub_{target_npc.id}")
            if not rng.chance(pex.CUB_CHANCE):
                return
            species = pex.roll_species(world, rng)
            loc = self._current_location(world)
            res = pex.tame_check(world, world.player, species,
                                 getattr(loc, "danger", 1) if loc else 1, rng)
            if res["success"]:
                cub = pex.create_pet(world, species)
                fed = f"（以「{res['fed_item']}」为饵）" if res["fed_item"] else ""
                parts.append(f"战场边缘发现一只{species.get('name', '幼崽')}幼崽，驯服入队「{cub.name}」{fed}")
                summary["pet_cub"] = {"name": cub.name, "species": cub.species}
            else:
                parts.append("战场边缘探出一只幼崽，警觉地望着你们，片刻后隐入荒野")
                summary["pet_cub"] = {"name": "", "species": ""}
        except Exception:
            pass

    # ============ [P25a] 秘境进出（DungeonDialog 调用；直改 + 调用方落盘）============
    def enter_dungeon(self, world: World, dungeon, *, summary: Optional[dict] = None,
                       intent: Optional[dict] = None,
                       preset: Optional[WorldSimPreset] = None) -> tuple[bool, str]:
        """进入秘境：切到内部地点（首次进入建 + 揭示 + 同伴跟随）。

        [R0/B02-F14] 服务级入口校验：玩家必须身在入口地点（世界地图/按钮/自由文本
        共用本口径），不能隔空传进秘境。"""
        if dungeon is None:
            return False, "此地没有秘境"
        if not getattr(dungeon, "discovered", False):
            return False, "你还未发现此地的秘境入口"
        if world.player.hp <= 0:
            return False, "你已倒下，无法进入秘境"
        if dungeon.status != "open" and not dge.has_pending_loot(dungeon):
            return False, (f"秘境「{dungeon.name}」已封印"
                           f"（约 {max(0, int(dungeon.cooldown_until_day or 0) - int(world.day_count or 1))} 天后重现）")
        cur_id = str(getattr(world.player, "location_id", "") or "")
        if cur_id != dungeon.location_id:
            # [!] 用原始 location_id 对比（_current_location 对失效 id 回退首个地点，
            # 会把「人在未知处」误判成在入口）
            ent = dge.entrance_location(world, dungeon)
            ent_name = ent.name if ent is not None else dungeon.name
            return False, f"你不在「{dungeon.name}」的入口（{ent_name}），先前往入口所在地再进入"
        dge.ensure_room_graph(dungeon)
        error = dge.room_graph_error(dungeon)
        if error:
            return False, error
        loc = dge.interior_location(world, dungeon)
        loc.discovered = True
        loc.explored = True
        world.player.location_id = loc.id
        # 从真实第一层入口进入；已保存的房间结果不重置，不传送到首个未探房。
        dungeon.current_room_id = dungeon.floors[0].entry_room_id if dungeon.floors else ""
        dge.reveal_room(dungeon, dungeon.current_room_id)
        # [P27] 进秘境：place_id 重置（秘境内部无场所，清空避免残留旧场所 id）
        world.player.place_id = getattr(loc, "default_place_id", "") or ""
        self.reveal_adjacent(world)
        self._companions_follow(world, loc)
        entry_summary = summary if summary is not None else {}
        entry_summary["dungeon_entered"] = dungeon.id
        entry_intent = intent if intent is not None else {"resolved": True}
        dge._hint(entry_intent, entry_summary, f"踏入「{dungeon.name}」")
        dge.trigger_arrival_trap(world, dungeon, entry_intent, entry_summary)
        self._resolve_dungeon_defeat(world, entry_intent, entry_summary, preset)
        dge.reveal_room(dungeon, dungeon.current_room_id)
        return True, entry_summary["narration_hint"]

    def exit_dungeon(self, world: World) -> tuple[bool, str]:
        """离开秘境：回到入口（房间进度保留，重进续探）。"""
        cur = self._current_location(world)
        dungeon = dge.interior_dungeon(world, cur)
        if dungeon is None:
            return False, "你不在秘境中"
        if world.player.hp <= 0:
            return False, "你已倒下，无法离开秘境"
        ok, reason = dge.can_exit_dungeon(dungeon)
        if not ok:
            return False, reason
        ent = dge.entrance_location(world, dungeon)
        if ent is None:
            return False, "秘境入口失效"
        world.player.location_id = ent.id
        # [P27] 出秘境：place_id 重置为入口地点默认场所（与同伴跟随同口径，防玩家/同伴分处不同场所）
        world.player.place_id = getattr(ent, "default_place_id", "") or ""
        self._companions_follow(world, ent)
        return True, f"撤出「{dungeon.name}」"

    # ============ [P58 摸格子 2026-09-26] 战利品格子（三角洲式拾取）============

    def _collect_loot_grid(self, world: World, target, difficulty: str,
                           extra_drops: Optional[list] = None) -> dict:
        """战利品格子收集（finish_combat / _resolve_combat 两路径共用单一来源）。

        roll 链路与旧自动入包完全同序（roll_loot -> upgrade_drops -> reclaim_candidates
        -> 敌对保底 -> salvage 50% -> [extra_drops 合并] -> materialize_unidentified_drops），
        唯一改动 = **不自动入包**：全部战利品进 grid 交给 LootGridDialog 由玩家取舍
        （1 件=1 格 + 堆叠<=3 + 负重，纯可视化既有背包语义，不改数据结构）。

        返回 {"entries": [{item_id, name, source}], "salvage_ids", "reclaimed_ids",
              "npc_id"(空=临时野怪，尸身不留), "npc_name", "bag_before"}。
        source: loot=纯掉落（未拿：NPC 尸体留尸可搜刮 / 野怪消散）；salvage=搜尸
        （物品本就在尸体上，未拿原地不动）；reclaim=P47 夺回（本就在劫掠者身上，
        未拿留原处下次仍可夺）。
        """
        rng_loot = SeededRng.seed_from(world.id, world.tick_count, f"combat_loot_{target.id}")
        eff_luck = te.effective_stat(world.player, "luk") + te.get_talent_bonus(world.player, "luck_bonus")
        eff_drop_rate = min(0.95, 0.4 * te.get_talent_mult(world.player, "loot_mult"))
        drops = ce.roll_loot(getattr(target, "loot_table", None) or [], rng_loot,
                             drop_rate=eff_drop_rate,
                             difficulty_mult=ce.loot_difficulty_mult(difficulty),
                             luck=eff_luck)
        drops = ce.upgrade_drops(drops, world, rng_loot,
                                 luck=eff_luck, monster_level=getattr(target, "level", 1))
        # [P47-A] 追索闭环（位置契约：升档后 / materialize 前；只取候选不动尸体）
        reclaimed = cue.reclaim_candidates(world, target)
        drops = drops + [d for d in reclaimed if d not in drops]
        if not drops and getattr(target, "hostile", False) and getattr(target, "loot_table", None):
            first = target.loot_table[0]
            if isinstance(first, dict):
                fid = first.get("id") or first.get("item_id") or ""
            elif isinstance(first, str):
                fid = first
            else:
                fid = ""
            if fid:
                drops = [str(fid)]
        npc_inv = list(getattr(target, "inventory", []) or [])
        salvage_ids: list = []
        if npc_inv and getattr(target, "hostile", False):
            rng_salv = SeededRng.seed_from(world.id, world.tick_count, f"salvage_{target.id}")
            for iid in npc_inv:
                if rng_salv.chance(0.5) and iid not in drops:
                    drops.append(iid)
                    salvage_ids.append(iid)
        if extra_drops:
            drops = drops + [d for d in extra_drops if d not in drops]
        bag_before = list(getattr(world.player, "inventory", None) or [])
        drops = ce.materialize_unidentified_drops(world, drops)
        reclaim_set, salvage_set = set(reclaimed), set(salvage_ids)
        entries = []
        for did in drops:
            base = str(did)[:-6] if str(did).endswith("__unid") else str(did)
            source = ("reclaim" if base in reclaim_set
                      else "salvage" if base in salvage_set else "loot")
            it = next((i for i in world.items if i.id == did), None)
            entries.append({"item_id": did, "source": source,
                            "name": (getattr(it, "name", "") or str(did))
                                    + ("（未鉴定）" if str(did).endswith("__unid") else "")})
        is_npc = any(n.id == getattr(target, "id", "") for n in (world.npcs or []))
        return {"entries": entries, "salvage_ids": salvage_ids, "reclaimed_ids": reclaimed,
                "npc_id": target.id if is_npc else "", "npc_name": getattr(target, "name", ""),
                "bag_before": bag_before}

    def loot_grid_take(self, world: World, item_id: str) -> bool:
        """摸格子点击拾取：统一走 try_add_to_inventory（堆叠<=3 + 负重），成功记图鉴 +
        collect 任务进度（[P58]「拿到才算获得」——原 finish_combat 的掉落即计钩子随
        摸格子化移到此处）。失败（满包/堆叠满）返回 False 由 UI 提示，物品留在格子（守恒）。"""
        if not ce.try_add_to_inventory(world.player, item_id):
            return False
        if item_id not in (world.player.codex_items or []):
            world.player.codex_items.append(item_id)
        try:
            it = next((i for i in world.items if i.id == item_id), None)
            if it is not None:
                qe.update_progress(world, "collect", it.name,
                                   aliases=[getattr(it, "rarity", "")])
                # [修 2026-10-01] 同物品名连发 gather：任务 gather 目标按物品名推进
                # （gather/collect 双口径——「收集到」无论拾取/买入/采集都算，玩家不卡死）。
                qe.update_progress(world, "gather", it.name)
        except Exception:
            pass
        return True

    def loot_grid_settle(self, world: World, grid: dict) -> dict:
        """摸格子关闭守恒收口（LootGridDialog done 时调用，幂等）。

        - salvage/reclaim 对账：按背包差集（bag_delta，判据含 {id}__unid 副本）只扣
          「确实进了背包」的；未拿的留尸体/劫掠者身上（物品守恒，守 P47/P8 契约）。
        - 纯 loot 未拿：NPC 尸体 -> append 进 npc.inventory（之后「搜刮」可再拿，
          重生随 inventory 原样保留）；临时野怪 -> 随尸身消散（UI 已确认）。
        返回 {"leftover": [名字], "reclaimed_back": [名字], "missing": [名字]} 供文案
        （夺回/下落不明事实随格子关闭结算，UI 据此补系统条目——原 finish_combat 内
        parts 生成随摸格子化顺延到此处）。
        [!] 幂等靠 grid["_settled"] 标记：纯 loot 未拿允许与尸体同 id 并存（各是各的
        一份，勿去重——去重会让掉落那份凭空消失），二次调用直接短路。"""
        if grid.get("_settled"):
            return {"leftover": [], "reclaimed_back": [], "missing": []}
        npc = next((n for n in (world.npcs or []) if n.id == (grid.get("npc_id") or "")), None) \
            if grid.get("npc_id") else None
        entries = list(grid.get("entries") or [])
        salvage_ids = [str(i) for i in (grid.get("salvage_ids") or [])]
        reclaimed_ids = [str(i) for i in (grid.get("reclaimed_ids") or [])]
        res = {"leftover": [], "reclaimed_back": [], "missing": []}
        if npc is not None:
            # salvage 对账（与旧入包后对账同款：delta 判据 + 原地不动未拿的）
            delta = cue.bag_delta(grid.get("bag_before"), world.player.inventory)
            _taken_sal = {i for i in salvage_ids
                          if delta.get(str(i), 0) > 0 or delta.get(f"{i}__unid", 0) > 0}
            if _taken_sal:
                npc.inventory = [i for i in (getattr(npc, "inventory", None) or [])
                                 if str(i) not in _taken_sal]
            # reclaim 对账（单一来源 cue.settle_reclaimed：delta 判据 + stolen_ids 清账）
            back = cue.settle_reclaimed(world, npc, reclaimed_ids, grid.get("bag_before"))
            res["reclaimed_back"] = [next((i.name for i in world.items if i.id == did), did)
                                     for did in back]
            # [P47-C 赃物闭环 b] 下落不明（同原 finish_combat 尾部口径，随结算顺延）
            if getattr(npc, "alive", True) is False:
                _sold = cue.missing_stolen(npc, back)
                res["missing"] = [next((i.name for i in world.items if i.id == did), did)
                                  for did in _sold]
            # 纯 loot 未拿 -> 留尸体（salvage/reclaim 的未拿本就在尸体，勿重复 append）
            _src_ids = set(salvage_ids) | set(reclaimed_ids)
            leftover = []
            for e in entries:
                did = str(e.get("item_id", ""))
                base = did[:-6] if did.endswith("__unid") else did
                if base in _src_ids:
                    continue
                leftover.append(did)
            for did in leftover:
                npc.inventory.append(did)
        else:
            leftover = [str(e.get("item_id", "")) for e in entries]   # 野怪尸身消散
        names = []
        for did in leftover:
            it = next((i for i in world.items if i.id == did), None)
            names.append(getattr(it, "name", "") or did)
        res["leftover"] = names
        grid["_settled"] = True
        return res

    def finish_combat(self, world: World, session, target_npc, preset: Optional[WorldSimPreset] = None) -> dict:
        """[P7h] 战斗结束结算：写回 hp/mp/技能冷却 + 掉落/经验/升级。返回 summary dict。

        session.state: won/lost/fled。summary 含 narration_hint（回合日志摘要）供叙事 LLM 描写。
        [P10] 多敌：随从单位各自掉落/给经验（半 rate，无保底）；主目标走原结算。
        [P10] 助战：同伴 hp/mp 写回 NPC（倒下保 1 血：助战同伴一律不死亡）+ 交情 +4；
        summary 带 allies。
        [P58 摸格子 2026-09-26] 掉落**不再自动入包**：roll 链路产出的战利品进
        summary["loot_grid"]，由玩家在 LootGridDialog 里拾取（loot_grid_take/settle
        守恒收口）。金币/经验/升级保持自动发放。

        [R0/B01-F07 2026-09-30] 收尾去重戳：同一 session 的第二次 finish_combat 直接
        短路返回（不写回、不发放）——重复回调不再重复结算经验/金币/掉落/击杀数。
        """
        # [B01-F07] 幂等闸（先于一切写回）：战斗收尾只会发生一次
        if getattr(session, "_settle_done", False):
            return {"world_changed": False, "combat_state": getattr(session, "state", ""),
                    "duplicate_settlement": True}
        try:
            session._settle_done = True
        except Exception:
            pass
        difficulty = session.difficulty
        # 写回 hp/mp/冷却
        world.player.hp = session.player.hp
        world.player.mp = session.player_mp
        world.player.skill_cooldowns = dict(session.player_cd)
        target_npc.hp = session.enemy.hp
        target_npc.mp = session.enemy_mp
        # [P47 后果层] 代价三件套（被夺 / 夺回 / 目击记恨）的载体：必须在下面的 won 分支
        # 调用 witness_grudge 之前初始化，否则会被随后的初始化语句清空（文案丢失）。
        # 三者在函数末统一追加到 parts——它们含数值事实，若被 parts[-8:] 截掉就会出现
        # 「玩家发现钱少了但旁白没写」的结算真相裂缝（P16 天赋提示同款先例）。
        defeat_toll: Optional[dict] = None
        reclaimed: list[str] = []
        reclaimed_back: list[str] = []      # 实际追回的（入包后对账，见 settle_reclaimed）
        witness_lines: list[str] = []
        # [玩家印象 2026-09-06] 玩家没赢（lost/fled）：幸存的对手亲眼领教过玩家，
        # 对玩家记「危险」（负信任）。won 时对手已倒不更新（死人无印象）。
        if session.state in ("lost", "fled") and getattr(target_npc, "alive", True):
            try:
                soc.update_impression(target_npc, "危险", trust_delta=-12, firsthand=True)
            except Exception:
                pass
        # [!] won 时必须置 alive=False + respawn：只写 hp=0 的话尸体仍被世界滴答
        # 挪动/产出传闻，且 cleanup_and_respawn 只处理 alive=False -> 玩家击杀永不重生
        # （与滴答击杀口径一致）。[P45 v3 2026-09-12 用户定稿] 死亡统一由 npc_permadeath
        # 决定（原「要角 respawn=0 永久」特判已删）；主线发布人不死。
        if session.state == "won":
            kill_rng = SeededRng.seed_from(world.id, world.tick_count, f"kill_{target_npc.id}")
            giver_quests_txt = ""
            mg_spared = False
            if qe.is_main_giver(world, target_npc):
                target_npc.hp = 1  # 主线气运护体：重伤未死，后续段落才解得开
                mg_spared = True   # [P45 v3.1 审核修复] 存活则跳过整段 won 结算（防无限刷）
            else:
                target_npc.alive = False
                target_npc.respawn_at_tick = world.tick_count + kill_rng.roll(8, 14)
                giver_quests_txt = qe.cleanup_dead_giver_quests(world, target_npc)
                # [P47-B 后果层] 目击者记恨：同地点同场所的存活非敌对 NPC 亲眼看到你动手
                # （零 LLM、零 rng；玩家印象 trust 下降 -> 喂买价与拒卖闸）。放函数末追加。
                witness_lines = cue.witness_grudge(world, target_npc)
                # [P47-C1 三模型共识] 命案上新闻：击杀「人」产 WorldEvent，喂活坊间热议/
                # 传闻口述化/浅印象扩散/NPC 动态栏四条既有链——玩家暴行从「只有目击者知道」
                # 变成「全城逐步知晓」（事件直加有观战敌袭先例，不吃 tick 事件预算）。
                _ev = cue.murder_event(world, target_npc, witness_count=len(witness_lines))
                if _ev is not None:
                    world.event_log.append(_ev)
                    witness_lines.append("这件事恐怕很快就会传开")
        parts = list(getattr(session, "log", []) or [])
        if session.state == "won" and giver_quests_txt:
            parts.append(giver_quests_txt)
        if session.state == "won" and mg_spared:
            parts.append(f"{target_npc.name}身负主线气运，重伤倒地却吊住了一口气"
                         f"（未死，无战利品可搜）")
        summary: dict = {"world_changed": True, "combat_rounds": session.round,
                         "combat_state": session.state}
        # ---- [P10] 助战同伴写回 + 交情 ----
        ally_names = []
        fat_rng = SeededRng.seed_from(world.id, world.tick_count, "ally_fatigue")
        for unit in (getattr(session, "ally_units", None) or []):
            npc = next((n for n in world.npcs if n.id == unit.npc_id), None)
            if npc is None:
                continue                      # 宠物单位（npc_id 空）在此被跳过，不写回不疲劳
            down = not unit.alive
            npc.hp = max(1, int(unit.snapshot.hp))          # 同伴不阵亡（倒下保 1 血）
            npc.hp_max = max(npc.hp_max, int(unit.snapshot.hp_max))
            npc.mp = max(0, int(unit.mp))
            ally_names.append(npc.name)
            nre.add_affinity(npc, 4)
            # [玩家印象 2026-09-06] 并肩作战亲眼见证：同伴对玩家记「无畏」
            try:
                soc.update_impression(npc, "无畏", trust_delta=4, firsthand=True)
            except Exception:
                pass
            nre.touch_friend_interact(world, npc)   # [P24c]
            # [P25b] 参战疲劳：真打过（won/lost，fled 脱战不计）的同伴 8-14 tick 不可再战
            if session.state in ("won", "lost"):
                npc.companion_fatigue_until_tick = int(world.tick_count or 0) + fat_rng.roll(8, 14)
                # [P26a] 共同战斗场次 +1（结义条件之一 >=3 场；与疲劳同 gate，fled 不计）
                npc.shared_combats = int(getattr(npc, "shared_combats", 0) or 0) + 1
            if down:
                parts.append(f"同伴{npc.name}在战斗中倒下受了伤（交情却更深了）")
            else:
                parts.append(f"同伴{npc.name}与你并肩作战（交情 +4）")
        if ally_names:
            summary["allies"] = ally_names
        # ---- [P10] 随从结算（击败的随从各自掉落/给经验，半 rate 无保底）----
        minion_loot: list[str] = []
        minion_xp = 0
        # [P45 v3.1 复查修复] 随从结算同样受主线护体门（否则残留小额刷取通道）
        if session.state == "won" and not mg_spared:
            for unit in (getattr(session, "enemy_units", None) or []):
                if not getattr(unit, "is_minion", False) or unit.alive:
                    continue
                m_lvl = max(1, int(getattr(unit.snapshot, "level", 1) or 1))
                minion_xp += m_lvl * 8
                rng_m = SeededRng.seed_from(world.id, world.tick_count, f"minion_loot_{unit.name}")
                eff_luck = te.effective_stat(world.player, "luk") + te.get_talent_bonus(world.player, "luck_bonus")
                drops_m = ce.roll_loot(unit.loot_table, rng_m, drop_rate=0.2,
                                       difficulty_mult=1.0, luck=eff_luck)
                # [P58 摸格子] 随从掉落不再即时入包：并入主格子由玩家拾取
                # （原「跨随从去重只展示一次」口径保留——同 id 只进一次格子）
                for did in drops_m:
                    if did not in minion_loot:
                        minion_loot.append(did)
        if session.state == "won" and not mg_spared:
            # [P45 v3.1 审核修复] 主线发布人存活（mg_spared）时跳过整段 won 结算——
            # 否则敌对主线发布人可被无限刷 xp/金币/掉落/图鉴/kill 进度（review-qwen P1）。
            # [P58 摸格子] 掉落收集（统一 _collect_loot_grid，不自动入包，玩家在
            # LootGridDialog 拾取；随从掉落并入同一格子）
            grid = self._collect_loot_grid(world, target_npc, difficulty,
                                           extra_drops=minion_loot)
            drop_names = [e["name"] for e in grid["entries"]]
            # 经验/升级（[P10] 主目标 + 击败的随从）
            xp_gain = int((max(1, target_npc.level) * 15 + minion_xp) * ce.xp_difficulty_mult(difficulty))
            xp_r = ce.gain_xp(world.player, xp_gain)
            leveled = xp_r.leveled_up
            if leveled:
                self._level_up_recalc(world)
            # [数值补全] 战斗金币掉落：旧版战斗零金币是结构性通缩主因（价格品级倍率 11x vs
            # 奖励 3x，后期攒钱买装备越来越难）。按 level*rng 发少量金币（Lv1 约 3-8 金，
            # boss 更多），乘 economy_pace 金币产出系数（hard/hardcore 自动砍）。精英怪经
            # level+2 + 专属掉率 0.75 + 技能书进掉落表（0.75 按公式 roll）多产出（NPC 无 rarity 字段，旧 rare+ 加成分支是死代码已删）。
            gold_rng = SeededRng.seed_from(world.id, world.tick_count, f"combat_gold_{target_npc.id}")
            base_gold = max(1, target_npc.level) * gold_rng.roll(3, 8)
            combat_gold = int(base_gold * tre.gold_gain_mult(tre.world_pace(world)))
            world.player.gold = int(getattr(world.player, "gold", 0) or 0) + combat_gold
            _gt_cur = self._genre_text(world).currency  # §23 币种走 GenreText
            parts.append(f"击败{target_npc.name}"
                         + (f"，战利品：{'、'.join(drop_names)}（战后摸格子拾取）" if drop_names else "")
                         + f"，获得 {xp_gain} 经验、{combat_gold} {_gt_cur}" + ("，升级！" if leveled else ""))
            summary.update({"target_npc_defeated": True, "xp_gained": xp_gain,
                            "gold_gained": combat_gold,
                            "leveled_up": leveled, "new_level": xp_r.new_level,
                            "items_dropped": [e["item_id"] for e in grid["entries"]],
                            "loot_text": "、".join(drop_names) if drop_names else "",
                            "loot_grid": grid})
            # [P15b2] 击杀声望变动（纯代码）：击杀有势力 NPC -> 该势力 -10 / 其敌对势力 +5。
            # 敌对判定与 UI 关系标签同口径（任一方向 relations <= -30）；声望钳 -100~100。
            try:
                vf = next((f for f in world.factions if f.id == target_npc.faction_id), None)
                if vf is not None:
                    p_rep = world.player.reputation
                    p_rep[vf.id] = max(-100, min(100, int(p_rep.get(vf.id, 0) or 0) - 10))
                    for other in world.factions:
                        if other.id == vf.id:
                            continue
                        rel_ov = int(other.relations.get(vf.id, 0) or 0)
                        rel_vo = int(vf.relations.get(other.id, 0) or 0)
                        if rel_ov <= -30 or rel_vo <= -30:
                            p_rep[other.id] = max(-100, min(100, int(p_rep.get(other.id, 0) or 0) + 5))
                    parts.append(f"此举令「{vf.name}」对你的声望下降（其敌对势力则对你刮目相看）")
            except Exception:
                pass
            # [P7i] 任务进度钩子：kill（按 NPC role/name/id 匹配）。collect（掉落物品）
            # [P58 摸格子] 移到 loot_grid_take——掉落不再自动入包，「拿到才算获得」。
            try:
                qe.update_progress(world, "kill", target_npc.role,
                                   aliases=[target_npc.name])
            except Exception:
                pass
            # [P7k6] 图鉴：击败 NPC + 战斗胜场。掉落物品入图鉴改在 loot_grid_take
            # （拾取时记「曾拥有」，守 P58 拿到才算获得口径）。
            # [P12] 临时野怪（不在 world.npcs）不进图鉴——uuid 每次全新会让怪物页
            # 计数与卡片永久漂移且无限增长；战斗胜场照记。
            try:
                self._record_codex_kill(world, target_npc, "被你击败")
                world.player.combat_wins = int(world.player.combat_wins or 0) + 1
            except Exception:
                pass
            # [P24d] 出战宠物 xp + 临时野怪低概率遇幼崽
            self._pet_victory_rewards(world, target_npc, parts, summary)
            # [P25a] 秘境怪收尾：房间 done / 层 cleared / Boss -> 保底奖励 + 封印
            try:
                dge.on_combat_victory(world, target_npc, parts, summary)
            except Exception:
                pass
            # [G03/R3 2026-09-30] 秘境通关 -> 入口地点 5 天纾解行情（矿路复通类）
            if summary.get("dungeon_cleared"):
                try:
                    from src.services import region_pressure as _rp
                    _dg = next((d for d in (world.dungeons or [])
                                if d.name == summary["dungeon_cleared"].get("dungeon")),
                               None)
                    if _dg is not None:
                        _rp.relief_from_dungeon_clear(world, _dg)
                except Exception:
                    pass
            # [P25d] 世界 Boss 收尾：非终形态 -> index+1 + 变身 hint；终形态 -> defeated + 奖励
            try:
                wbe.on_combat_victory(world, target_npc, parts, summary)
            except Exception:
                pass
            # [P58 审查修复] bag_before 重捕：秘境保底奖励仍在格子收集后入包，
            # 若撞 salvage/reclaim id 会被 settle 的差集判据误判成「已拾取」（复制/漏账）。
            # 奖励发完再拍快照，差集窗口收窄到「摸格子拾取」本身。
            if summary.get("loot_grid") is not None:
                summary["loot_grid"]["bag_before"] = list(
                    getattr(world.player, "inventory", None) or [])
            # [P52 伤疤] 惨胜留痕：胜但血量 < 35% -> 同款 roll 伤（部位累计）
            if world.player.hp / max(1, world.player.hp_max) < 0.35:
                rng_inj = SeededRng.seed_from(world.id, world.tick_count,
                                              f"injury_{getattr(target_npc, 'id', '')}")
                _inj = ce.roll_injuries(world, world.player,
                                        getattr(session, "player_part_dmg", {}) or {},
                                        max(1, session.player.hp_max or 1), rng_inj)
                if _inj:
                    summary["injuries"] = _inj
        elif session.state == "lost":
            parts.append(f"你被{target_npc.name}击败，已倒下")
            summary["player_defeated"] = True
            # [P10c] 永久死亡关闭 -> 复活：送回出生地，HP=1（与永久死亡开启 -> 删世界 对称）。
            # 调用方（world_scene_tab._fight_npc_dialog）据 player_defeated 分流：perm 开删世界，
            # perm 关此处已复活，战后叙事回合据 narration_hint 描写苏醒。
            pd = self._per_world(world, "permadeath_enabled",
                                 preset.permadeath_enabled if preset else False, preset)
            if not pd:
                spawn_name = self._revive_at_spawn(world)
                if spawn_name:
                    parts.append(f"你在出生地「{spawn_name}」苏醒，捡回一条命（HP 1）")
                    summary["revived_at_spawn"] = spawn_name
                # [P47-A 后果层] 败北代价：被夺金/物（守恒转移给胜者；野怪只夺金）。
                # [!] 新 salt 独立 rng，不污染既有 combat_loot_*/salvage_* 序列；
                #     文案在函数末统一追加（防 parts[-8:] 截断数值事实）。
                rng_toll = SeededRng.seed_from(world.id, world.tick_count,
                                               f"toll_{getattr(target_npc, 'id', '')}")
                defeat_toll = cue.took_from_player(
                    world, target_npc, rng_toll,
                    is_temp_monster=not any(n is target_npc for n in (world.npcs or [])))
                if defeat_toll["gold"]:
                    summary["lost_gold"] = defeat_toll["gold"]
                if defeat_toll["items"]:
                    summary["lost_items"] = list(defeat_toll["items"])
                # [P47-C 赃物闭环 a] 劫掠者记住这票买卖（进 chat_notes，下次记忆整理并入
                # 正式记忆——私聊/召回自然带出，被抢的玩家可能听到他吹嘘）
                _notes = []
                if defeat_toll["gold"]:
                    _notes.append("一笔钱")
                if defeat_toll["items"]:
                    _notes.append("几件随身物")
                if _notes:
                    try:
                        self.npc_memory().record_chat_note(
                            world, getattr(target_npc, "id", ""),
                            "我从那个玩家身上抢了" + "和".join(_notes) + "，别声张")
                    except Exception:
                        pass
                # [P52 伤疤] 战败留痕（部位累计 >= 25% 血量上限的最重部位 roll 伤）
                rng_inj = SeededRng.seed_from(world.id, world.tick_count,
                                              f"injury_{getattr(target_npc, 'id', '')}")
                _inj = ce.roll_injuries(world, world.player,
                                        getattr(session, "player_part_dmg", {}) or {},
                                        max(1, session.player.hp_max or world.player.hp_max),
                                        rng_inj, min_one=True)
                if _inj:
                    summary["injuries"] = _inj
        else:  # fled
            parts.append(f"与{target_npc.name}的战斗结束（脱战）")
        # [P7k3] 装备耐久损耗（won/lost 真打才损耗，fled 脱战不损耗）
        if session.state in ("won", "lost"):
            try:
                self._degrade_equipment(world, session.granularity)
                summary["world_changed"] = True
            except Exception:
                pass
        # [P16] NPC 天赋可感知：敌方天赋提示注入旁白要点（放最后追加，防被 parts[-8:] 截掉），
        # 叙事 LLM 据实把「对手身怀异禀」织入战斗描写。
        try:
            enemy_talents = [t for t in (getattr(target_npc, "talents", None) or [])
                             if isinstance(t, dict)]
            if enemy_talents:
                rn = self._genre_text(world).rarity_names()
                lines = te.describe_talents(enemy_talents, rarity_names=rn)
                if lines:
                    parts.append(f"交手中可感知到{target_npc.name}身怀："
                                 + "；".join(lines))
        except Exception:
            pass
        # [P47 后果层] 代价三件套最末追加，且各**合并成一条**（减少占位）：它们含数值事实
        # （丢了多少钱 / 拿回什么 / 谁记恨你），被截掉就会出现「玩家发现钱少了但旁白没写」
        # 的结算真相裂缝。窗口从 8 放宽到 11 —— 让三件套与原有 8 条数值要点同时在场，
        # 而不是互相挤占（P47 之前 parts[-8:] 的语义原样保留）。
        if defeat_toll and defeat_toll["lines"]:
            summary["lost_text"] = "；".join(defeat_toll["lines"])
            parts.append(summary["lost_text"])
        if reclaimed_back:
            _rnames = "、".join(next((i.name for i in world.items if i.id == did), did)
                                for did in reclaimed_back)
            parts.append(f"你从{target_npc.name}身上夺回了被抢走的：{_rnames}")
        # [P47-C 赃物闭环 b] 下落不明：被夺物既没追回也不在尸体上 = 已被转手卖掉
        # （结算真相：不追认不存在的物品，但也绝不让玩家的东西无声消失）
        if reclaimed and target_npc.alive is False:
            _sold = cue.missing_stolen(target_npc, reclaimed_back)
            if _sold:
                _snames = "、".join(next((i.name for i in world.items if i.id == did), did)
                                    for did in _sold)
                parts.append(f"至于他早前抢走的{_snames}——早已被他转手卖掉了")
        if witness_lines:
            _wl = "；".join(witness_lines[:2])
            if len(witness_lines) > 2:
                _wl += f"（另有 {len(witness_lines) - 2} 人也目睹了这一幕）"
            parts.append(_wl)
        summary["narration_hint"] = "；".join(parts[-11:])  # 最近 11 条（8 既有 + P47 三件套）
        # [战报 2026-08-28] 写入战斗历史（右侧栏「战报」页读；滚动保留最近 30 条防膨胀）
        try:
            world.combat_history = [h for h in (world.combat_history or [])
                                    if isinstance(h, dict)]
            world.combat_history.append({
                "tick": int(world.tick_count or 0),
                "day": max(1, int(getattr(world, "day_count", 1) or 1)),
                "enemy": str(getattr(target_npc, "name", "") or "?"),
                "level": max(1, int(getattr(target_npc, "level", 1) or 1)),
                "state": str(session.state or "?"),
                "rounds": int(session.round or 0),
                "dmg_dealt": int(getattr(session, "dmg_dealt", 0) or 0),
                "dmg_taken": int(getattr(session, "dmg_taken", 0) or 0),
                "xp": int(summary.get("xp_gained", 0) or 0),
                "gold": int(summary.get("gold_gained", 0) or 0),
                "loot": str(summary.get("loot_text", "") or ""),
                # [P47-A 后果层] 败北被夺读数（右侧「战报」页可见，因果可见性）
                "lost": str(summary.get("lost_text", "") or ""),
                "allies": list(summary.get("allies", []) or []),
            })
            del world.combat_history[:-30]
        except Exception:
            pass   # 战报 best-effort：写入失败不阻断战斗结算
        return summary

    def _resolve_stock(self, world: World, scene: SceneLog, intent: dict,
                       summary: dict, preset: Optional[WorldSimPreset] = None) -> bool:
        """[B方案 2026-08-28] 交易所股市自由文本结算（纯 Python stock_engine）。

        settle 判 stock_buy/stock_sell：trade_item 复用作商品名/symbol，stock_qty 数量
        （缺省 1）。须玩家在城市（交易所 venue）——与 StockDialog 按钮门控同口径；
        不在城市 resolved=false + reason 指路。成功回填 narration_hint（含持仓）。
        [修 2026-09-06 真机] 返回是否已回落普通交易（settle 常把「按行情价买 X」误判
        stock_buy，商品名不在交易所时改走 trade 结算，不再让整回合零效果蒸发）。
        """
        from src.services import stock_engine as ske
        mode = itype_buy = intent.get("intent_type") == "stock_buy"
        name = (intent.get("trade_item") or "").strip()
        qty_raw = str(intent.get("stock_qty") or "").strip() or "1"
        try:
            qty = max(1, min(999, int(qty_raw)))
        except (TypeError, ValueError):
            qty = 1
        # 城市门控：玩家当前地点 kind=settlement 且 settlement_size=city（含交易所 venue）
        loc = self._current_location(world)
        is_city = (loc is not None and getattr(loc, "kind", "") == "settlement"
                   and str(getattr(loc, "settlement_size", "") or "") == "city")
        if not is_city:
            intent["resolved"] = False
            intent["reason"] = "交易所只在大城市开设，请先前往城市（场景页「股市」按钮/自由输入均可操作）"
            return False
        sm = getattr(world, "stock_market", None)
        if sm is None or not getattr(sm, "commodities", None):
            intent["resolved"] = False
            intent["reason"] = "这个世界没有交易所"
            return False
        # 商品解析：symbol 精确 -> 名（P44 容错）
        from src.services import name_resolver as _nrs
        target = None
        for c in sm.commodities:
            if not getattr(c, "symbol", ""):
                continue
            if name.upper() == c.symbol or (name and _nrs.names_match(name, c.name)):
                target = c
                break
        if target is None:
            if itype_buy and name:
                # [修 2026-09-06 真机] 误判回落：商品名不在交易所（真机 2 连——「按行情价
                # 买铁背刀」被吞）时改走普通交易（匿名货架/商人均可接手），失败口径同 trade。
                intent["intent_type"] = "trade"
                intent["trade_mode"] = "buy"
                self._resolve_trade(world, scene, intent, summary, preset)
                return True
            syms = "、".join(f"{c.name}({c.symbol})" for c in sm.commodities[:6])
            intent["resolved"] = False
            intent["reason"] = f"交易所没有「{name or '?'}」；可交易：{syms}"
            return False
        if itype_buy:
            ok, msg = ske.buy_stock(world, target.symbol, qty)
        else:
            ok, msg = ske.sell_stock(world, target.symbol, qty)
        if not ok:
            intent["resolved"] = False
            intent["reason"] = msg
            return False
        summary["world_changed"] = True
        held = int((world.player.stock_holdings or {}).get(target.symbol, 0) or 0)
        act = "买入" if itype_buy else "卖出"
        hint = f"交易所{act}{target.name} x{qty}（{msg.strip('。')}），现持仓 {held}"
        summary["narration_hint"] = hint
        # [修 2026-09-05] 原 `existing or " | " + hint` 三元在 intent 已有 hint 时短路
        # 返回旧值——引擎股市事实被整个丢弃（结算要点只剩 LLM 旧文案）
        _existing = intent.get("narration_hint") or ""
        intent["narration_hint"] = (_existing + " | " + hint) if _existing else hint
        # 已学技能提示等公共尾不适用；任务钩子无股市目标，跳过
        return False

    def _resolve_trade(self, world: World, scene: SceneLog, intent: dict,
                       summary: dict, preset: Optional[WorldSimPreset] = None) -> None:
        """[P28] 交易结算（纯 Python trade_engine）。buy/sell 走商店 Shop 货架
        （新世界常驻游商 + 旧档真商人兼容）；barter 走任意 NPC。

        容器权威解析：item.name 不唯一，按目标容器（shop.stock/player.inventory/npc.inventory）
        解析消歧 + 兜底虚构物品（旁白编造/背包满丢弃的物品不在容器 -> resolved=false）。
        失败口径同 move：intent.resolved=False + summary.reason，不静默放行不虚构。
        """
        mode = (intent.get("trade_mode") or "").strip()
        item_name = (intent.get("trade_item") or "").strip()
        offer_name = (intent.get("trade_offer") or "").strip()
        talk_name = (intent.get("talk_to") or "").strip()
        # [P28 修 2026-08-25] 交易方式白名单硬闸：settle LLM 漏填 trade_mode（只判 resolved=true、
        # 旁白写「先达成交易窗口」却不给买/卖/换）时，旧逻辑三分支都不匹配 -> 交易静默 no-op。
        # 现缺/非法 mode 直接 resolved=false + reason（空 mode 显示「空」），不再「旁白演了交易、引擎没做交易」。
        if mode not in ("buy", "sell", "barter"):
            intent["resolved"] = False
            summary["reason"] = f"未知交易方式「{mode or '空'}」"
            return
        # 解析交易对象 NPC（须存活/非敌对/同地点）。[P44] 容错解析（LLM 常加
        # 「铁匠鲁大锤」式职业前缀/修饰），候选集锚定全体真实 NPC 名。
        # [修 2026-09-06 真机] buy 不在此硬拒 npc=None：talk_to 空/虚构名（真机「铁剑门
        # 兵器铺老掌柜」非真实 NPC）时改走 buy 分支的匿名货架回落；sell/barter 仍须真名 NPC。
        npc = _npc_by_name(world, talk_name)
        if npc is None and mode != "buy":
            intent["resolved"] = False
            summary["reason"] = f"找不到名叫「{talk_name}」的人"
            return
        if npc is not None:
            if not getattr(npc, "alive", True) or getattr(npc, "hostile", False):
                intent["resolved"] = False
                summary["reason"] = f"无法与「{talk_name}」交易"
                return
            if not self._same_place_as_player(world, npc):
                # [用户定稿 2026-08-28] 收紧到场所级：同地点不同场所也算不在身边
                intent["resolved"] = False
                summary["reason"] = f"「{talk_name}」不在你身边（不在当前场所）"
                return
        if not item_name or (mode == "barter" and not offer_name):
            intent["resolved"] = False
            summary["reason"] = "交易意图缺少物品名"
            return
        # id -> Item 解析表（容器权威解析用）
        item_by_id = {it.id: it for it in (getattr(world, "items", []) or [])}
        # [修 2026-09-10] 声望口径与【交易】块注入价同源：npc=None（游商名/虚构名/空 talk_to）
        # 时不再固定 rep=0，而是按**本店真商人**（_shop_merchant，含 shop_id 反向挂载）的势力
        # 声望算——注入价用 rep_of(merchant)，两端必须同源，否则反向挂载真商人的店会出现
        # 「旁白/货架报价 ≠ 实际扣款」。游商无真商人 -> 0（原口径不变）。
        # 注意：shop 在下面各分支才解析，故这里用闭包延迟取。

        def _rep_for(shop_obj) -> int:
            # [审查修复] 一律以**门店商人**为准（与【交易】块注入价的 rep_of(merchant) 严格同源）：
            # 玩家点名了某个真实 NPC 但该 NPC 无店、按店名回落到别家货架时，若取点名 NPC 的
            # 势力声望，仍会与注入价分叉。无店/无商人时回退点名 NPC（旧口径），都没有则 0。
            src = self._shop_merchant(world, shop_obj) if shop_obj is not None else None
            if src is None:
                src = npc
            if src is None:
                return 0
            return int(world.player.reputation.get(getattr(src, "faction_id", "") or "", 0) or 0)
        pace = tre.world_pace(world)

        def _item_name_of(iid: str) -> str:
            it = item_by_id.get(iid)
            return getattr(it, "name", "") or ""

        def _find_in_container(ids: list, want: str) -> "Optional[str]":
            """[P44] 容器内按名找物品 id（候选集锚定 + 容错解析）。

            容器权威不变：只在传入 ids 的名字里解析（不追认容器外物品）；
            LLM 常给物品名加「上品/破损的」修饰或有一字之差，精确等值会误报「没有此物」。
            """
            names = [_item_name_of(i) for i in ids]
            hit_name = nrs.resolve_name(want, names)
            if not hit_name:
                return None
            return next((i for i in ids if _item_name_of(i) == hit_name), None)

        night_knock = False   # [P39c] buy 分支内按营业门控置位（夜间敲门溢价）
        _bought_iid = ""      # [P47-C] buy 分支解析出的容器 item_id（赎回销账用）
        terr_note = ""        # [P45(1)] 敌对治下溢价/压价的叙事 hint（buy/sell 分支置位）
        mkt_note = ""         # [物价联动 2026-09-10] 季节/财富标签 hint（buy/sell 分支置位）
        if mode == "buy":
            # [游商 2026-09-08] 商店挂地点常驻经营（游商假 NPC 无移动/死亡/离场概念）：
            # buy 不再要求商人 NPC 在场——玩家在店所在地点即成交（同 UI 路径口径，
            # 与 2026-09-06 匿名货架回落同结论：文本路径也该买到）。talk_to 优先级：
            # (1) 点名真实商人 NPC 且其有店 -> 走该店（旧档商人在场检查仍在上面）；
            # (2) 点名游商名（「{店名}·{称谓}」）/店名/虚构名/空 -> 本地点货架寻店；
            # (3) 点名真实 NPC 但其无店 -> 「并非商人」。
            shop = None
            if npc is not None:
                shop = self.shop_for_npc(world, npc.id)
                if shop is None:
                    cur_l = self._current_location(world)
                    vendor_hit = next(
                        (s for s in (self.shops_at_location(world, cur_l.id)
                                     if cur_l is not None else [])
                         if talk_name and (talk_name == trade_vendor_name(world, s)
                                            or talk_name in str(getattr(s, "name", "") or ""))),
                        None)
                    if vendor_hit is not None:
                        shop = vendor_hit
                        _vh = f"交易在「{trade_vendor_name(world, shop)}」处完成"
                        _prev_v = summary.get("narration_hint") or ""
                        summary["narration_hint"] = ((_prev_v + "；" + _vh).lstrip("；")
                                                     if _prev_v else _vh)
                    else:
                        intent["resolved"] = False
                        summary["reason"] = f"「{talk_name}」并非商人"
                        return
            if shop is None:
                cur_l = self._current_location(world)
                shops_here = self.shops_at_location(world, cur_l.id) if cur_l is not None else []
                cand = ([s for s in shops_here if not (getattr(s, "merchant_npc_id", "") or "")]
                        + [s for s in shops_here if (getattr(s, "merchant_npc_id", "") or "")])
                shop = next((s for s in cand
                             if _find_in_container([e.item_id for e in s.stock], item_name)),
                            None)
                if shop is None:
                    intent["resolved"] = False
                    summary["reason"] = f"本地点的商店都不出售「{item_name}」"
                    return
                _anon_hint = f"交易在「{trade_vendor_name(world, shop)}」处完成"
                _prev_a = summary.get("narration_hint") or ""
                summary["narration_hint"] = (_prev_a + "；" + _anon_hint).lstrip("；") if _prev_a else _anon_hint
            # [P47-B 后果层] 记恨闸：门店商人 trust <= -60 -> 拒绝与你交易。
            # [!] 只拦玩家买入（卖出不拦——否则会把人逼进「没钱又没渠道」的死循环）；
            #     NPC 采购路径不调用本闸（守 §22「NPC 采购恒挂牌价」既有契约）。
            # [!] 必须走「行动受阻」块（resolved=False + reason）：有受阻块时旁白一律不得
            #     描写成交，否则会出现「旁白卖了、引擎拒绝了」的经典 OOC 裂缝。
            _refuse = cue.merchant_refuses(self._shop_merchant(world, shop))
            if _refuse:
                intent["resolved"] = False
                summary["reason"] = _refuse
                return
            # 容器权威：在 shop.stock 找 name 匹配的 entry
            hit_iid = _find_in_container([e.item_id for e in shop.stock], item_name)
            _bought_iid = str(hit_iid or "")
            entry = next((e for e in shop.stock if e.item_id == hit_iid), None) if hit_iid else None
            if entry is None:
                intent["resolved"] = False
                summary["reason"] = f"该商店不出售「{item_name}」"
                return
            item = item_by_id.get(entry.item_id)
            if item is None:
                intent["resolved"] = False
                summary["reason"] = f"「{item_name}」的物品数据缺失"
                return
            # [P39c] 打烊时段敲门强买：夜晚（auction 交易所除外）1.5x 溢价，不拒客
            night_knock = not tre.shop_open(world, shop)
            # [P45(1)] 敌对治下买入溢价（与敲门溢价乘法叠加；声望提上来即恢复原价）
            terr_buy, _, terr_tag = tre.territory_price_factors(world, shop, world.player)
            if terr_buy > 1.0:
                terr_note = f"（{terr_tag}治下溢价）"
            # [产地系数] 产地直供折扣乘入（与治下/夜间乘法叠加）
            prod_buy, _, _ = tre.production_price_factors(world, shop, item)
            # [物价联动 2026-09-10] 季节/财富乘区（购买时刻动态乘区，不写货架价）
            mkt_buy, _, mkt_tags = tre.market_price_factors(world, shop, item)
            if mkt_tags:
                mkt_note = "（" + "、".join(mkt_tags) + "）"
            r = tre.buy(world.player, shop, item, _rep_for(shop), pace,
                        merchant=self._shop_merchant(world, shop),
                        markup=(tre.NIGHT_MARKUP if night_knock else 1.0)
                        * terr_buy * prod_buy * mkt_buy)
            # [修 2026-10-01] 买入也算获得：图鉴收录 + collect/gather 双发（此前买入
            # 不推任何任务进度——collect:百年人参 靠购买达成的链路是断的）。
            if r.get("ok"):
                try:
                    if item.id not in (world.player.codex_items or []):
                        world.player.codex_items.append(item.id)
                    qe.update_progress(world, "collect", str(getattr(item, "name", "") or ""),
                                       aliases=[getattr(item, "rarity", "")])
                    qe.update_progress(world, "gather", str(getattr(item, "name", "") or ""))
                except Exception:
                    pass
        elif mode == "sell":
            # [游商 2026-09-08] sell 同 buy：点名游商名/店名/虚构名/空 -> 本地点货架
            # 寻店收购（游商常驻在场，无需 NPC 在场）；点名真实无店 NPC -> 并非商人。
            shop = self.shop_for_npc(world, npc.id) if npc is not None else None
            if shop is None:
                if npc is not None:
                    cur_ls = self._current_location(world)
                    vendor_hit = next(
                        (s for s in (self.shops_at_location(world, cur_ls.id)
                                     if cur_ls is not None else [])
                         if talk_name and (talk_name == trade_vendor_name(world, s)
                                            or talk_name in str(getattr(s, "name", "") or ""))),
                        None)
                    if vendor_hit is None:
                        intent["resolved"] = False
                        summary["reason"] = f"「{talk_name}」并非商人"
                        return
                    shop = vendor_hit
                else:
                    cur_ls = self._current_location(world)
                    shops_ls = (self.shops_at_location(world, cur_ls.id)
                                if cur_ls is not None else [])
                    shop = next(iter(shops_ls), None)
                    if shop is None:
                        intent["resolved"] = False
                        summary["reason"] = f"本地点没有可收购的商店"
                        return
            # 容器权威：在 player.inventory 找 name 匹配的 item（[P44] 容错解析）
            iid = _find_in_container(list(world.player.inventory), item_name)
            if not iid:
                intent["resolved"] = False
                summary["reason"] = f"你并没有「{item_name}」"
                return
            item = item_by_id.get(iid)
            if item is None:
                intent["resolved"] = False
                summary["reason"] = f"「{item_name}」的物品数据缺失"
                return
            # [P45(1)] 敌对治下压价收购
            _, terr_sell_f, terr_tag = tre.territory_price_factors(world, shop, world.player)
            if terr_sell_f < 1.0:
                terr_note = f"（{terr_tag}治下压价）"
            # [产地系数] 异地稀缺溢价乘入（本地也产则 1.0，原地倒卖必亏）
            _, prod_sell, prod_tag = tre.production_price_factors(world, shop, item)
            # [物价联动 2026-09-10] 季节/财富乘区（卖出镜像：穷 -> 压价，富 -> 抬价）
            _, mkt_sell, mkt_tags = tre.market_price_factors(world, shop, item)
            if mkt_tags:
                mkt_note = "（" + "、".join(mkt_tags) + "）"
            r = tre.sell(world.player, shop, item, _rep_for(shop), pace,
                         sell_factor=terr_sell_f * prod_sell * mkt_sell)
        elif mode == "barter":
            # 容器权威：give 在 player.inventory / take 在 npc.inventory（[P44] 容错解析）
            give_iid = _find_in_container(list(world.player.inventory), offer_name)
            take_iid = _find_in_container(list(npc.inventory or []), item_name)
            if not give_iid:
                intent["resolved"] = False
                summary["reason"] = f"你并没有「{offer_name}」"
                return
            if not take_iid:
                intent["resolved"] = False
                summary["reason"] = f"对方并没有「{item_name}」"
                return
            give = item_by_id.get(give_iid)
            take = item_by_id.get(take_iid)
            if give is None or take is None:
                intent["resolved"] = False
                summary["reason"] = "交易物品数据缺失"
                return
            stage = reng.stage_of(world, npc.id)
            r = tre.barter(world.player, npc, give, take, getattr(npc, "affinity", 0), stage)
        else:
            intent["resolved"] = False
            summary["reason"] = f"未知交易方式「{mode}」"
            return

        if not r.get("ok"):
            intent["resolved"] = False
            summary["reason"] = r.get("reason", "交易失败")
            return
        # 成功：summary + narration_hint（仿 rest/move 的 append 口径）
        summary["trade"] = r
        action = r.get("action", mode)
        if action == "buy":
            hint = f"花费 {r.get('price', 0)} 购入「{r.get('item_name', item_name)}」"
            if night_knock:
                hint += "（夜间敲门溢价）"
            if prod_buy < 1.0:
                hint += "（产地直供）"
            # [P47-C 赃物闭环 c] 买回即赎回：从原持有者 stolen_ids 销账（与 ShopDialog 同口径）
            _owner = cue.reclaim_via_purchase(world, _bought_iid) if _bought_iid else "" 
            if _owner:
                hint += f"（这正是{_owner}从你那里抢走的东西——赎回了）"
        elif action == "sell":
            hint = f"卖出「{r.get('item_name', item_name)}」得 {r.get('price', 0)}"
            if prod_tag:
                hint += f"（{prod_tag}）"
        else:  # barter
            hint = f"以「{r.get('give_item_name', offer_name)}」换得「{r.get('take_item_name', item_name)}」"
        if terr_note:
            hint += terr_note
        if mkt_note:
            hint += mkt_note
        prev = summary.get("narration_hint") or ""
        summary["narration_hint"] = (prev + "；" + hint).lstrip("；") if prev else hint
        # 交易算深度互动（刷新好友衰减水位线，与赠礼/私聊/同行/助战同口径）
        try:
            nre.touch_friend_interact(world, npc)
        except Exception:
            pass

    def _resolve_gift(self, world: World, intent: dict, summary: dict):
        """[修 2026-09-06 审计] gift 意图结算：玩家把背包物品送给在场 NPC（真转移）。

        走 nre.apply_player_gift 同一入口（UI 送礼面板同款）：物品出玩家包入 NPC 包、
        交情按品级/爱好涨、玩家印象打「慷慨」标签、定情信物跃迁检测——引擎做完再让
        叙事照 hint 写，记忆整理照实固化，三层（引擎/旁白/记忆）从此同源。
        门控：NPC 须存活/非敌对/与玩家同场所；物品须在背包且非任务关键道具。
        """
        from src.services import npc_reaction_engine as nre
        name = (intent.get("talk_to") or "").strip()
        item_name = (intent.get("gift_item") or "").strip()
        if not name:
            intent["resolved"] = False
            summary["reason"] = "送礼须说清送给谁"
            return
        if not item_name:
            # 未给物品名：从行动文本兜底一次（防弱模型漏填 gift_item）
            item_name = str(intent.get("target", "") or "").strip()
        if not item_name:
            intent["resolved"] = False
            summary["reason"] = "送礼须说清送什么（背包物品名）"
            return
        # [审核修复 2026-09-13] 送礼收件人走 nrs.resolve_name 四层容错（守 P44）：
        # combat/trade/move/quest-giver 均已迁移，唯独此处仍精确等值，LLM 写
        # 「捕头鲁大锤」「鲁大锤兄弟」这类带称谓写法会让整回合送礼无声失败。
        # 候选集锚定存活 NPC 名（不追认虚构，也不解析到死者身上）。
        npc = _npc_by_name(world, name, alive_only=True)
        if npc is None:
            intent["resolved"] = False
            summary["reason"] = f"找不到「{name}」其人"
            return
        if getattr(npc, "hostile", False):
            intent["resolved"] = False
            summary["reason"] = f"「{name}」与你敌对，不会收礼"
            return
        if not self._same_place_as_player(world, npc):
            intent["resolved"] = False
            summary["reason"] = f"「{name}」不在你身边（不在当前场所，送不到手上）"
            return
        inv_items = []
        for iid in list(getattr(world.player, "inventory", None) or []):
            it0 = next((i for i in world.items if i.id == iid), None)
            if it0 is not None:
                inv_items.append(it0)
        it = next((i for i in inv_items if i.name == item_name), None)
        if it is None and inv_items:
            hit = nrs.resolve_name(item_name, [i.name for i in inv_items])
            it = next((i for i in inv_items if i.name == hit), None) if hit else None
        if it is None:
            intent["resolved"] = False
            summary["reason"] = f"背包里没有「{item_name}」"
            return
        if it.type == "key":
            intent["resolved"] = False
            summary["reason"] = f"「{it.name}」是任务关键道具，无法送人"
            return
        result = nre.apply_player_gift(world, npc, it)
        if not result.get("ok"):
            intent["resolved"] = False
            summary["reason"] = str(result.get("reason", "赠送失败"))
            return
        summary["gift"] = {"npc": npc.name, "item": it.name, "gain": result.get("gain", 0)}
        hint = str(result.get("narration_hint", "") or "")
        # [!] intent 与 summary 双写（叙事 LLM 只读 intent.narration_hint，守 §22 口径）
        existing = intent.get("narration_hint", "") or ""
        intent["narration_hint"] = (existing + "；" + hint) if existing else hint
        summary["narration_hint"] = intent["narration_hint"]
        summary["world_changed"] = True

    def _resolve_dungeon_defeat(self, world: World, intent: dict, summary: dict,
                                preset: Optional[WorldSimPreset] = None):
        """陷阱致命伤沿用倒下契约；引擎已优先尝试复活丹，这里分流永久死亡/出生地复活。"""
        if not summary.get("player_defeated"):
            return
        pd = self._per_world(world, "permadeath_enabled",
                             preset.permadeath_enabled if preset else False, preset)
        if not pd:
            spawn_name = self._revive_at_spawn(world)
            summary["revived_at_spawn"] = spawn_name
            dge._hint(intent, summary, f"你在出生地「{spawn_name}」苏醒，捡回一条命（HP 1）")

    def _resolve_use_item(self, world: World, intent: dict, summary: dict,
                          preset: Optional[WorldSimPreset] = None):
        """使用消耗品（回血）/ 研读技能书。intent.effects/target 提示用哪个 item。"""
        # [P13] 技能书研读：intent.target/effects 点名书（书名或技能名任一命中）才学
        # （防"使用物品"误吃书；书名带题材后缀，玩家/LLM 说技能名也能匹配）
        hint_txt = f"{intent.get('target', '') or ''}{intent.get('effects', '') or ''}"
        from src.services import dungeon_tools as _dt
        tool_id = str(intent.get("dungeon_tool_id", "") or "")
        named = [it for it in world.items if it.name and it.name in hint_txt]
        # 完整/最长点名优先，防「酸囊（秘境耗材）」被短名药品「酸囊」抢占。
        longest = max((len(it.name) for it in named), default=0)
        named = [it for it in named if len(it.name) == longest]
        named_owned = [it for it in named if it.id in world.player.inventory]
        # 先按真实背包识别；目录里未持有的同名工具不能抢走普通消耗品的使用。
        tool_pool = world.items if tool_id else (named_owned or named)
        tool = next((it for it in tool_pool if it.tool_for in _dt.TOOL_KINDS
                     and (it.id == tool_id if tool_id else it.name and it.name in hint_txt)), None)
        if tool is not None or tool_id:
            intent["dungeon_action"] = "tool"
            dungeon = dge.interior_dungeon(world, self._current_location(world))
            if dungeon is None or tool is None:
                intent["resolved"] = False
                summary["reason"] = "此工具须在秘境内对应的房间互动中使用"
                return
            intent["dungeon_tool_id"] = tool.id
            dge.advance(world, dungeon, summary, intent, world.player.level)
            self._resolve_dungeon_defeat(world, intent, summary, preset)
            return  # 点名工具失败也不能误吃其他药品
        unavailable_hint = ""
        for iid in list(world.player.inventory):
            it = next((i for i in world.items if i.id == iid), None)
            if it is None:
                continue
            teach = getattr(it, "teach_skill", None)
            if isinstance(teach, dict) and teach and it.name \
                    and (it.name in hint_txt or str(teach.get("name", "")) in hint_txt):
                sk_name = teach.get("name", "技能")
                already = any(isinstance(s, dict) and s.get("name") == sk_name
                              for s in world.player.skills)
                if already:
                    summary["reason"] = f"已掌握「{sk_name}」，无需再读"
                    intent["resolved"] = False
                    return
                from src.models import Skill as _Sk
                # [技能书个体差异 2026-09-10] 品级定基准 + 每件确定性抖动（书 id 盐，
                # 仿装备 _fill_item_defaults；不改书内 teach_skill 蓝图模板）
                world.player.skills.append(_Sk.from_dict(
                    ce.jitter_taught_skill(teach, world.id, iid)).to_dict())
                world.player.inventory.remove(iid)
                hint = f"研读{it.name}，习得技能「{sk_name}」！"
                existing = intent.get("narration_hint", "") or ""
                intent["narration_hint"] = (existing + " | " + hint) if existing else hint
                # [!] world_changed 触发场景页 save_world + refresh（技能入列/书出包须落盘）
                summary.update({"learned_skill": sk_name, "used_item_id": iid,
                                "world_changed": True})
                return
        # 优先 consume_effect（结构化效果：五维/回蓝等），其次旧 heal_amount 回血
        # [P7e] 治疗量叠加 stat_int 驱动的 heal_bonus（compute_stats 算；
        # [C1] 同口径传装备顶层 stat_bonus——装备 int 加成也进治疗量）
        p_snap = ce.compute_stats(world.player,
                                  equip_stat_bonus=self._equipped_stat_bonus(world, world.player),
                                  hunger_mult=ce.hunger_stat_mult(world.player))
        heal_bonus = p_snap.heal_bonus
        # 点名药品时只尝试该物：不能因为它此刻不适用就误吃背包里排在前面的另一瓶。
        item_by_id = {i.id: i for i in world.items}
        named_ids = [iid for iid in world.player.inventory
                     if iid in item_by_id and item_by_id[iid].name
                     and item_by_id[iid].name in hint_txt]
        candidate_ids = named_ids or list(world.player.inventory)
        for iid in candidate_ids:
            it = next((i for i in world.items if i.id == iid), None)
            if not it or it.type != "consumable":
                continue
            ok, hint = ce.apply_consume_effect(
                world.player, it,
                equip_stat_bonus=self._equipped_stat_bonus(world, world.player))
            if ok:
                world.player.inventory.remove(iid)
                existing = intent.get("narration_hint", "") or ""
                intent["narration_hint"] = (existing + " | " + hint) if existing else hint
                summary.update({"used_item_id": iid, "world_changed": True})
                return
            if hint and not unavailable_hint:
                unavailable_hint = hint
        for iid in candidate_ids:
            it = next((i for i in world.items if i.id == iid), None)
            if it and it.type == "consumable" and (it.heal_pct > 0 or it.heal_amount > 0):
                hp_before = world.player.hp
                # [百分比回血 2026-09-01] heal_pct 优先（单一来源 item_heal_value）
                ce.heal(world.player, ce.item_heal_value(it, world.player), bonus=heal_bonus)
                world.player.inventory.remove(iid)
                world.player.hp_max = ce.max_hp_for(world.player)
                actual_heal = world.player.hp - hp_before
                hint = f"使用{it.name}，回复 {actual_heal} HP（{world.player.hp}/{world.player.hp_max}）"
                existing = intent.get("narration_hint", "") or ""
                intent["narration_hint"] = (existing + " | " + hint) if existing else hint
                summary.update({"healed": actual_heal, "used_item_id": iid,
                                "world_changed": True})
                return
        summary["reason"] = unavailable_hint or "背包里没有可用的消耗品"
        intent["resolved"] = False

    def _gather_verb(self, world: World) -> str:
        """[P7g] 题材化采集动词（据 attribute_template_id 查 _GENRE_RESOURCE_TEMPLATES）。"""
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        tmpl = _GENRE_RESOURCE_TEMPLATES.get(tid) or _GENRE_RESOURCE_TEMPLATES["western_fantasy"]
        return tmpl.get("verb", "采集")

    def gather_preview(self, world: World, node_name: str, preset: WorldSimPreset):
        """采集预览（主线程 UI 用）：解析资源点并算成功率。返回 (chance, node)；不可采返回 (None, None)。

        与 _resolve_gather 共用 ge.gather_chance 口径，保证 UI 显示概率与结算一致。
        """
        loc = self._current_location(world)
        if not loc or not loc.resource_nodes:
            return None, None
        node = None
        if node_name:
            hit = nrs.resolve_name(node_name, [n.name for n in loc.resource_nodes])
            node = next((n for n in loc.resource_nodes if n.name == hit), None) if hit else None
        if node is None:
            node = next((n for n in loc.resource_nodes if ge.can_gather(n, world.tick_count)), None)
        if node is None or not ge.can_gather(node, world.tick_count):
            return None, None
        stat_used = node.stat_used if node.stat_used in ("str", "dex", "int", "vit", "luk") else "dex"
        # [饱食度] 饥饿减半采集驱动属性（与战斗/合成同口径）
        stat_value = int(te.effective_stat(world.player, stat_used)
                         * ce.hunger_stat_mult(world.player))
        inv_items = [i for i in world.items if i.id in (world.player.inventory or [])]
        tool_bonus = ge.find_tool_bonus(inv_items, node.requires_tool)
        difficulty = max(0, min(35, int(getattr(loc, "danger", 1)) * 4))
        base_rate = getattr(preset, "gathering_base_rate", 0.6)
        base_rate = min(0.95, base_rate * te.get_talent_mult(world.player, "gather_mult"))
        pet_gb = 0.10 if pex.passive_active(world, "gather_bonus") else 0.0
        chance = ge.gather_chance(stat_value, tool_bonus, difficulty, base_rate=base_rate,
                                  chance_bonus=pet_gb,
                                  global_penalty=ce.check_difficulty_penalty(self._difficulty(world, preset)))
        return chance, node

    def _resolve_gather(self, world: World, scene: SceneLog, intent: dict, summary: dict,
                        preset: Optional[WorldSimPreset] = None):
        """[P7g] 采集结算：找资源点 -> 成功率 -> 产出进背包 -> 丰度递减 -> 冷却。

        纯 Python（gather_engine），LLM 不参与动态结果。守数值范式铁律。
        intent.gather_node / talk_to / target 任一为资源点名；未指定则取第一个可采点。
        """
        if preset is None or not getattr(preset, "gathering_enabled", True):
            summary["reason"] = "采集系统未开启"
            intent["resolved"] = False
            return
        loc = self._current_location(world)
        if not loc or not loc.resource_nodes:
            summary["reason"] = "当前地点没有可采集的资源"
            intent["resolved"] = False
            return
        node_name = (intent.get("gather_node") or intent.get("talk_to")
                     or intent.get("target") or "").strip()
        node = None
        if node_name:
            # [P44] 容错解析（LLM 常写「灵草丛」「那株灵草」）；未命中仍回退首个可采点
            hit = nrs.resolve_name(node_name, [n.name for n in loc.resource_nodes])
            node = next((n for n in loc.resource_nodes if n.name == hit), None) if hit else None
        if node is None:
            # 未指定名或未命中 -> 取第一个可采点
            node = next((n for n in loc.resource_nodes if ge.can_gather(n, world.tick_count)), None)
        if node is None or not ge.can_gather(node, world.tick_count):
            # [修 2026-10-02 真机 P3·采集 veto 零引导] 附本地点可用节点清单——真机档
            # 「翻找可回收的材料」被 LLM 编成「废金属」后 veto 只有一句不存在，整回合
            # 零效果且玩家不知道该说什么词（可用词=节点名，玩家不可见）。
            _avail = [n.name for n in loc.resource_nodes
                      if ge.can_gather(n, world.tick_count)]
            _hint = (f"；本地点可采集：{'、'.join(_avail[:6])}" if _avail
                     else "；本地点资源点均已枯竭或冷却中，稍后再来")
            summary["reason"] = (f"「{node_name or '资源'}」无法采集"
                                 f"（不存在/枯竭/冷却中）{_hint}")
            intent["resolved"] = False
            return
        # 驱动属性值（据 node.stat_used 从 player 读）
        stat_used = node.stat_used if node.stat_used in ("str", "dex", "int", "vit", "luk") else "dex"
        # [饱食度] 饥饿减半采集驱动属性（与战斗/合成同口径）
        stat_value = int(te.effective_stat(world.player, stat_used)
                         * ce.hunger_stat_mult(world.player))
        # 工具加成（背包查 tool_for 匹配 requires_tool）
        inv_items = [i for i in world.items if i.id in (world.player.inventory or [])]
        tool_bonus = ge.find_tool_bonus(inv_items, node.requires_tool)
        # 难度（[P8 体验反馈] danger*4 封顶 35：原 danger*5 时 danger 8 -> 40 ->
        # success_chance 钳到 0.05 近乎不可能；封顶后高危地点难但非绝望，
        # 属性/工具投入可翻盘）
        difficulty = max(0, min(35, int(getattr(loc, "danger", 1)) * 4))
        base_rate = getattr(preset, "gathering_base_rate", 0.6)
        # [P9] 天赋 gather_mult（种田好手/拾荒本能等采集加成，钳制 0.95 上限）
        base_rate = min(0.95, base_rate * te.get_talent_mult(world.player, "gather_mult"))
        decay = getattr(preset, "resource_richness_decay", 15)
        # 种子 RNG（确定性：同 world+tick+node 同结果）
        rng = SeededRng.seed_from(world.id, world.tick_count, f"gather_{node.id}")
        # [P24d] 宠物采集加成被动：出战宠物亲和达标且种族被动为 gather_bonus 时 +0.10
        pet_gb = 0.10 if pex.passive_active(world, "gather_bonus") else 0.0
        result = ge.gather(node, rng, stat_value, tool_bonus, world.tick_count,
                           difficulty=difficulty, base_rate=base_rate, richness_decay=decay,
                           chance_bonus=pet_gb,
                           global_penalty=ce.check_difficulty_penalty(self._difficulty(world, preset)),
                           dice=intent.get("dice"))
        # 产出进背包（inventory 去重，不堆叠数量）
        # [P8] 负重检查：物品种类数超 carry_capacity（=20+vit*2）则拒收新物品（vit 驱动）
        cap = ce.carry_capacity(world.player)
        gathered_names: list[str] = []
        bag_full = False
        for d in result.get("drops", []):
            iid = d.get("item_id")
            if not iid:
                continue
            it = next((i for i in world.items if i.id == iid), None)
            nm = it.name if it else iid
            # [堆叠 2026-09-13] 同种最多 _INV_STACK_LIMIT 件：达到上限后本次不再重复获得
            # （不算「背包满」——格数没满，只是这种物品带够了）。达上限/超载都不计进
            # gathered_names，避免旁白演出「获得了」却没真进包（守结算真相双闸）。
            if world.player.inventory.count(iid) >= ce._INV_STACK_LIMIT:
                continue
            elif len(world.player.inventory) >= cap:
                bag_full = True  # 超载拒收
                continue
            else:
                world.player.inventory.append(iid)
            if nm not in gathered_names:
                gathered_names.append(nm)
        if bag_full:
            result["bag_full"] = True
        # 回填 narration_hint（题材化 gather_verb）
        verb = self._gather_verb(world)
        if result["success"]:
            hint = (f"{verb}「{node.name}」获得：{'、'.join(gathered_names)}"
                    if gathered_names else f"{verb}「{node.name}」成功，但本次没有收获物品")
        else:
            hint = f"{verb}「{node.name}」失败，一无所获"
        if result["depleted"]:
            hint += "（资源点已枯竭" + ("，等待再生" if node.regenerates else "，永久耗尽") + "）"
        if result.get("bag_full"):
            hint += "（背包已满，部分物品无法携带）"
        existing = intent.get("narration_hint", "") or ""
        # [P9] 成功分支的 narration_hint 由下方 xp 段（拼完 xp 文本后）统一回填，
        # 这里只回填失败分支（失败不给 xp，不会走到下方）。
        if not result["success"]:
            intent["narration_hint"] = (existing + " | " + hint) if existing else hint
        summary.update({
            "gathered": gathered_names,
            "gather_success": result["success"],
            "gather_node": node.name,
            "richness_after": result["richness_after"],
            "world_changed": True,
        })
        # [P7i] 任务进度钩子：gather（按资源 type 匹配）
        # [修 2026-10-01 主线卡死] drops 物品名入 aliases：任务 gather 目标写的是
        # 物品名（LLM 直觉写法「收集止血草」），仅传 node.type（herb）永远匹配不上
        # ——真机档 gather:止血草×5 因此 0/5 卡死。采集掉落名一并作推进键。
        if result["success"]:
            try:
                _g_names = []
                for _d in (result.get("drops") or []):
                    _iid = _d.get("item_id") if isinstance(_d, dict) else None
                    _it = next((i for i in world.items if i.id == _iid), None)
                    if _it is not None and _it.name:
                        _g_names.append(str(_it.name))
                qe.update_progress(world, "gather", node.type,
                                   aliases=_g_names)
            except Exception:
                pass
            # [P7k6] 图鉴：采集计数 + 产出物品入曾拥有
            try:
                world.player.gathers_done = int(world.player.gathers_done or 0) + 1
                for d in result.get("drops", []):
                    iid = d.get("item_id")
                    if iid and iid not in world.player.codex_items:
                        world.player.codex_items.append(iid)
            except Exception:
                pass
            # [P9] 采集给 xp（生活玩家也能升级；xp 量按地点 danger，钳制非负）
            try:
                gp_xp = int(max(1, int(getattr(loc, "danger", 1) or 1) * 3)
                            * ce.xp_difficulty_mult(self._difficulty(world, preset)))
                xpr = ce.gain_xp(world.player, gp_xp)
                if xpr.leveled_up:
                    self._level_up_recalc(world)
                    summary["leveled_up"] = True
                    summary["new_level"] = xpr.new_level
                    summary["world_changed"] = True
                    hint += f"（+{gp_xp} 经验，升级到 {xpr.new_level} 级！可去加点）"
                else:
                    hint += f"（+{gp_xp} 经验）"
                intent["narration_hint"] = (existing + " | " + hint) if existing else hint
            except Exception:
                pass

    # ================================================================
    # ============ [P8] 奇遇/机缘（encounter_engine 接入）============
    # ================================================================
    def _genre_id(self, world: World) -> str:
        """[P8] 读当前世界题材 id（config_overlay.attribute_template_id，兜底 western_fantasy）。"""
        ov = getattr(world, "config_overlay", None) or {}
        if isinstance(ov, dict):
            return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")
        return "western_fantasy"

    def _roll_encounter(self, world: World, loc, mode: str, rng: SeededRng):
        """[P8] 奇遇触发+生成（不结算）。返回 (triggered, enc)。

        副作用：auto 置 loc.encounter_done（每地点只 roll 一次）。rng 由调用方传入：
        正常结算时同一 rng 继续用于奖励 roll（保确定性）；预览时传 tick+1 seed（对齐
        apply_intent 先自增 tick）。[定版裁剪 2026-09-05] 节日探测分流已随节日系统移除。
        """
        if mode == "auto" and getattr(loc, "encounter_done", False):
            return False, None
        # [P25a] 秘境内部不 roll 普通奇遇（探索节奏由房间推进承载）
        if getattr(loc, "kind", "") == "dungeon":
            return False, None
        triggered = ene.roll_encounter_trigger(world.player, loc, rng, mode=mode)
        if mode == "auto":
            loc.encounter_done = True   # 标记已 roll（无论命中）
        if not triggered:
            return False, None
        enc = ene.gen_encounter(world, loc, world.player, rng, self._genre_id(world))
        return True, enc

    def _finalize_encounter(self, world: World, summary: dict, result: dict):
        """[P8] 奇遇结算后的公共收尾：world_changed + xp 落地 + 叙事提示 + 任务钩子。"""
        summary["encounter"] = result
        summary["world_changed"] = True
        # [!] 奇遇 xp 须真正落地：resolve_encounter 只把 xp 写进返回 dict（旁白会播报
        # 「获得 N 经验」），不调 gain_xp 的话玩家进度分文未动（叙事对玩家撒谎）。
        try:
            enc_xp = int(result.get("xp") or 0)
            if enc_xp > 0:
                enc_xp = int(enc_xp * ce.xp_difficulty_mult(self._difficulty(world, None)))
                xpr = ce.gain_xp(world.player, enc_xp)
                # [审查修复] XpResult 是 dataclass 无 .get——原 xpr.get("level_up") 必抛
                # AttributeError 被 except 吞掉，奇遇升级分支从未生效（旁白谎报 HP 回满）。
                if xpr is not None and getattr(xpr, "leveled_up", False):
                    self._level_up_recalc(world)
                    result["narration_hint"] += "；你感到实力提升了（升级，HP/MP 回满）"
        except Exception:
            pass
        # 叙事提示拼到 summary.narration_hint（结算 + 叙事两段 LLM 据此生成完整旁白）
        prev = summary.get("narration_hint") or ""
        summary["narration_hint"] = (prev + "；" + result["narration_hint"]).lstrip("；") if prev \
            else result["narration_hint"]
        # [P7i] 任务进度钩子：collect（奇遇获物按 name/rarity 匹配）
        try:
            for nm in result.get("item_names", []):
                qe.update_progress(world, "collect", nm,
                                   aliases=[result["rarity"]])
        except Exception:
            pass

    def _maybe_trigger_encounter(self, world: World, loc, summary: dict, mode: str = "auto"):
        """[P8] 进入地点时 roll 自动奇遇（mode="auto" 每地点只一次，靠 encounter_done 防重）。

        命中则调 encounter_engine 抽取 + 结算，奖励入 summary 供叙事 LLM 据此生成旁白。
        未命中也回填一个轻提示（让叙事 LLM 知道探索过但无事），summary 不破坏既有结构。
        """
        try:
            rng = SeededRng.seed_from(world.id, world.tick_count, f"encounter_{loc.id}_{mode}")
            triggered, enc = self._roll_encounter(world, loc, mode, rng)
            if not triggered:
                # [P8] 首次进入无奇遇也给小奖励（xp），让探索永远有正反馈（守用户体验）
                if mode == "auto":
                    try:
                        xp_small = int(max(5, int(getattr(loc, "danger", 1) or 1) * 3)
                                       * ce.xp_difficulty_mult(self._difficulty(world, None)))
                        from src.services import combat_engine as _ce
                        _ce.gain_xp(world.player, xp_small)
                        summary["explore_xp"] = xp_small
                        summary["narration_hint"] = (
                            (summary.get("narration_hint") or "")
                            + f"；探索「{loc.name}」未见奇遇，但有所心得（+{xp_small} 经验）"
                        ).lstrip("；")
                    except Exception:
                        pass
                return
            result = ene.resolve_encounter(enc, world, world.player, rng)
            self._finalize_encounter(world, summary, result)
        except Exception:
            pass

    def encounter_preview(self, world: World, loc, mode: str = "probe") -> dict:
        """[三骰取二] 主线程预生成奇遇（触发+生成，不结算），返回 {triggered, enc, chance}。

        chance 为检定成功率（无检定 archetype 返回 None）；调用方据此弹 DiceCheckOverlay
        并把 enc + dice 透传给 _resolve_adventure。用 tick+1 对齐 apply_intent 先自增 tick。
        """
        rng = SeededRng.seed_from(world.id, world.tick_count + 1, f"encounter_{loc.id}_{mode}")
        triggered, enc = self._roll_encounter(world, loc, mode, rng)
        if not triggered or enc is None:
            return {"triggered": False, "enc": None, "chance": None}
        check_stat = enc.get("check_stat", "")
        if not check_stat:
            return {"triggered": True, "enc": enc, "chance": None}
        sv = te.effective_stat(world.player, check_stat)
        danger = int(enc.get("danger", 1))
        check_base = float(enc.get("check_base", 1.0))
        chance = ce.success_chance(sv, tool_bonus=0, difficulty=danger * 5, base=check_base,
                                   global_penalty=ce.difficulty_penalty_of(world))
        chance = min(0.95, chance * te.get_talent_mult(world.player, "check_mult"))
        return {"triggered": True, "enc": enc, "chance": chance}

    def _maybe_hidden_check(self, world: World, loc, summary: dict,
                            preset: Optional[WorldSimPreset] = None):
        """[P33] 进入地点时 roll 被动察觉检定（int/luk 取高 + check_mult；纯 Python+SeededRng）。

        首次进入该地点（explored 此前 False）才 roll，避免每次重访刷屏。通过则注入【察觉】
        narration_hint 提示叙事 LLM 描写隐藏发现（隐藏 NPC/资源点/线索），由 LLM 承载语义。
        [!] 检定结果不持久化（走 narration_hint）；开关 preset.hidden_check_enabled（默认开）。
        [!] 与 _maybe_trigger_encounter 独立 rng seed（不破坏奇遇确定性）。
        """
        try:
            if not self._per_world(world, "hidden_check_enabled", True, preset):
                return
            # 秘境内部不 roll 地点隐藏（由 dungeon_engine 房间检定承载）
            if getattr(loc, "kind", "") == "dungeon":
                return
            # 首次进入才 roll（explored 已被 apply_intent 设 True，但首次进入前是 False；
            # 用 encounter_done 作首次标志——奇遇已 roll 过的地点也不再 roll 隐藏察觉，
            # 避免与奇遇播报重叠；二者同窗口由 _maybe_trigger_encounter 先跑置 encounter_done）
            if getattr(loc, "encounter_done", False):
                return
            rng = SeededRng.seed_from(world.id, world.tick_count, f"hidden_check_{loc.id}")
            danger = max(1, int(getattr(loc, "danger", 1) or 1))
            if ene.roll_hidden_check(rng, world, difficulty=danger * 5, base=0.4):
                prev = summary.get("narration_hint") or ""
                hint = f"你察觉到「{loc.name}」似有隐秘之处（暗藏的人物或线索）"
                summary["narration_hint"] = (prev + "；" + hint).lstrip("；") if prev else hint
        except Exception:
            pass

    def _resolve_adventure(self, world: World, scene: SceneLog, intent: dict, summary: dict,
                           preset: Optional[WorldSimPreset] = None):
        """[P8] 手动探测结算（intent_type="adventure"，场景页「探测/搜索」按钮触发）。

        可重复触发（不查 encounter_done），但概率较低（base 0.08）。命中走奇遇引擎，
        未命中回填「一无所获」提示。主线程已预生成（intent["encounter_preview"]）则直接
        结算预生成 enc + 预掷 dice，跳过重触发/重生成。
        """
        loc = self._current_location(world)
        if loc is None:
            summary["reason"] = "无当前地点，无法探测"
            intent["resolved"] = False
            return
        pre = intent.get("encounter_preview")
        dice = intent.get("dice")
        if pre is not None:
            enc = pre.get("enc")
            if enc:
                rng = SeededRng.seed_from(world.id, world.tick_count,
                                          f"encounter_{loc.id}_probe_resolve")
                result = ene.resolve_encounter(enc, world, world.player, rng, dice=dice)
                self._finalize_encounter(world, summary, result)
            else:
                summary["adventure_miss"] = True
                summary["narration_hint"] = (
                    (summary.get("narration_hint") or "")
                    + f"；在「{loc.name}」仔细搜寻，并未发现异样").lstrip("；")
            return
        self._maybe_trigger_encounter(world, loc, summary, mode="probe")
        if not summary.get("encounter"):
            summary["adventure_miss"] = True
            summary["narration_hint"] = (
                (summary.get("narration_hint") or "") + f"；在「{loc.name}」仔细搜寻，并未发现异样"
            ).lstrip("；")

    def _init_resource_nodes(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[P12] 野外地点挂资源点（聚落 kind=settlement 不挂），tier 按危险度 1-5 分层。

        LLM location.resource（name/type/desc/tier）优先，tier 引擎钳制；drops 引擎按
        tier 从对应稀有度材料池确定性挑选（缺档就近取），守数值范式铁律。
        老世界（无 resource_nodes）from_dict 补空 list，不在此补（仅新建世界挂载）。
        """
        if preset is None or not getattr(preset, "gathering_enabled", True):
            return
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        tmpl = _GENRE_RESOURCE_TEMPLATES.get(tid) or _GENRE_RESOURCE_TEMPLATES["western_fantasy"]
        nodes_tmpl = tmpl["nodes"]
        cd_duration = getattr(preset, "resource_cooldown_ticks", 3)
        pending = getattr(self, "_pending_loc_resources", None) or {}
        wild_idx = 0
        for loc in world.locations:
            # 已有资源点则跳过（防重复挂载）
            if loc.resource_nodes:
                continue
            # [P12] 聚落（城/镇/村/坊市）无资源点：采集是野外行为
            if getattr(loc, "kind", "wilderness") == "settlement":
                continue
            res = pending.get(loc.id) or {}
            node_tmpl = nodes_tmpl[wild_idx % len(nodes_tmpl)]
            wild_idx += 1
            tier = _safe_int(res.get("tier"), we.danger_to_tier(loc.danger), 1, 5)
            rng = SeededRng.seed_from(world.id, 0, f"resource_{loc.id}")
            # 分层产出：tier 对应稀有度的材料（缺档就近向低/高档取）
            pool = we.nearest_rarity_pool(world.items, tier)
            picks = rng.sample(pool, 2) if pool else []
            qty = 2 if tier >= 3 else 1
            drops = [{"item_id": it.id, "qty": qty, "rate": 0.4} for it in picks]
            loc.resource_nodes.append(ResourceNode(
                name=str(res.get("name") or node_tmpl["name"]),
                type=str(res.get("type") or node_tmpl["type"]),
                desc=str(res.get("desc") or node_tmpl["desc"]),
                tier=tier,
                drops=drops, richness=100, richness_max=100,
                cooldown_tick=0, cooldown_duration=cd_duration,
                regenerates=True, requires_tool=node_tmpl.get("requires_tool", ""),
                stat_used=node_tmpl.get("stat_used", "dex"),
            ))
        self._pending_loc_resources = {}

    def _init_dungeons(self, world: World):
        """[P25a] 新世界初始秘境 1-2 个（挂野外地点，题材池确定性建，无 LLM）。

        [!] 老档不补（仅新建世界挂载，与 _init_resource_nodes 同口径）；每地点限一个。
        """
        rng = SeededRng.seed_from(world.id, 0, "init_dungeons")
        cands = [l for l in world.locations
                 if getattr(l, "kind", "") == "wilderness"
                 and dge.dungeon_at(world, l) is None]
        if not cands:
            return
        n = rng.roll(1, 2)
        picked: list = []
        while len(picked) < min(n, len(cands)):
            c = cands[rng.roll(0, len(cands) - 1)]
            if c not in picked:
                picked.append(c)
        for loc in picked:
            _dg = dge.build_dungeon(world, loc.id, getattr(loc, "danger", 1), rng=rng)
            _dg.discovered = False   # [D03] 新秘境隐藏——到访入口触发发现
            _dg.discovery_source = "worldgen"

    def _init_reagents(self, world: World, preset: Optional[WorldSimPreset] = None,
                       minimums: Optional[dict] = None):
        """[P34a] 培养类/锻造/制造 reagent 物品入世（题材池确定性生成入 world.items）。

    [方案A 2026-08-28] minimums={"cultivate": N}：LLM 已生成 N 件 cultivate 时引擎池
    只补差额（LLM 功能物品经 _normalize_functional_items 收编后计入；None=旧行为全量）。

        三类 reagent：
        - cultivate（鉴定卷轴/洗练石/资质丹）：type=consumable, category=cultivate, identified=True
          （reagent 无鉴定态意义）。供 P34b 鉴定洗练消耗 + 商店备货排除（商店不卖）。
        - forge（锻造材料）：type=material, category=forge。供 P34c 建筑建造/升级耗材 + 锻造配方输入。
        - craft（制造材料）：type=material, category=craft。供 P34c 炼制配方输入 + 建筑耗材。
        [!] 派生 id 确定性（同 world+reagent 名同结果，守 §22 派生 id 不变量 L294）：复用
            rng 派生但用稳定字符串 id 避免与 LLM 物品 id 冲突。reagent 物品按 (题材, kind, tier, 名)
            唯一化去重（幂等：重跑不重复入世）。
        [!] 数值：reagent 是消耗道具非装备，attack/defense/heal_amount 留 0（鉴定洗练道具不回血），
            base_price 据 tier 给种子价（商店排除但仍需定价用于掉落/奖励价值参考）。
        """
        max_lvl = max(0, int(getattr(preset, "item_max_level", 6) or 0))
        # 已存在的 reagent 名集合（幂等去重）
        existing = {it.name for it in world.items
                    if getattr(it, "category", "") in ("cultivate", "forge", "craft")}

        # [方案A] minimums.cultivate=LLM 已有件数 -> 引擎补差额到目标 9（题材池全量；
        # LLM 供足 9 时零注入。None=旧行为全量注入）
        _CULTIVATE_TARGET = 9
        _cultivate_quota = (None if minimums is None
                            else max(0, _CULTIVATE_TARGET - int(minimums.get("cultivate", 0) or 0)))

        def _add_reagent(name: str, category: str, item_type: str, rarity: str,
                         desc: str, tier: int, base_price: int,
                         reagent_kind: str = "") -> None:
            nonlocal _cultivate_quota
            if name in existing:
                return
            # 派生确定性 id：reagent_{题材}_{category}_{name 稳定哈希}（避免 uuid4 破坏可回放性）
            import hashlib as _hl
            tid = _p34a_genre_id(world)
            h = _hl.md5(f"{tid}|{category}|{name}".encode("utf-8")).hexdigest()[:8]
            rid = f"reagent_{h}"
            # tier 1-3 对应 level 档（鉴定/洗练高 level 物品需高 tier reagent）；钳 item_max_level
            lvl = min(max_lvl, tier) if max_lvl > 0 else tier
            it = Item(
                id=rid, name=name, type=item_type, rarity=rarity, desc=desc,
                category=category, level=lvl, identified=True,
                base_price=base_price,
                # [P34b] cultivate reagent 子类型（identify/refine/apt_*）用于鉴定/洗练/资质丹区分
                reagent_kind=reagent_kind if category == "cultivate" else "",
            )
            world.items.append(it)
            existing.add(name)
            if category == "cultivate" and _cultivate_quota is not None:
                _cultivate_quota -= 1

        # cultivate reagent：鉴定卷轴/洗练石/资质丹（rarity 据 tier：t1 uncommon/t2 rare/t3 epic）
        _tier_rarity = {1: "uncommon", 2: "rare", 3: "epic"}
        _tier_price = {1: 80, 2: 400, 3: 2000}
        for entry in cultivate_pool(world):
            kind = entry.get("kind", "")
            if _cultivate_quota is not None and _cultivate_quota <= 0:
                break                       # LLM 已供足 cultivate，引擎不补（forge/craft 仍注入）
            tier = max(1, min(3, int(entry.get("tier", 1) or 1)))
            # apt_all 走 tier3 价格档但 rarity epic；其余 cultivate 按 tier 档
            _add_reagent(
                name=entry.get("name", ""),
                category="cultivate",
                item_type="consumable",
                rarity=_tier_rarity.get(tier, "uncommon"),
                desc=entry.get("desc", ""),
                tier=tier,
                base_price=_tier_price.get(tier, 80),
                reagent_kind=kind,  # identify/refine/apt_atk/apt_def/apt_hp/apt_mp/apt_spd/apt_all
            )
        # forge 材料（[数值对齐 2026-09-06] rarity 按池语义阶梯覆盖 5 档：池模板跨题材
        # 同构（idx0 基底/1 精良辅材/2 史诗关键/3 传说点睛/4 常用/5-6 中档/7 材料），
        # 阶梯按 desc 语义对齐；tier 传 rarity 档 -> level 跟档（epic/legendary=4/5 超过
        # shop_max_item_level 不进普通商店，高阶材料走采集/掉落/锻造）。旧全 common 让
        # tier4/5 资源点与高层配方断档（高危区就近掉新手材料）；base_price 按 rarity 档
        # （护栏豁免 category 非空物品，价格保持设计意图）。
        _MAT_RARITY_TIER = {"common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5}
        _MAT_LADDER8 = ("common", "uncommon", "epic", "legendary",
                        "common", "rare", "rare", "uncommon")
        for i, entry in enumerate(forge_material_pool(world)):
            _rar = _MAT_LADDER8[min(i, len(_MAT_LADDER8) - 1)]
            _add_reagent(
                name=entry.get("name", ""),
                category="forge",
                item_type="material",
                rarity=_rar,
                desc=entry.get("desc", ""),
                tier=_MAT_RARITY_TIER.get(_rar, 1),
                base_price=_FORGE_MAT_PRICE.get(_rar, 30),
            )
        # craft 材料（同款语义阶梯覆盖，价格档略低于 forge）
        for i, entry in enumerate(craft_material_pool(world)):
            _rar = _MAT_LADDER8[min(i, len(_MAT_LADDER8) - 1)]
            _add_reagent(
                name=entry.get("name", ""),
                category="craft",
                item_type="material",
                rarity=_rar,
                desc=entry.get("desc", ""),
                tier=_MAT_RARITY_TIER.get(_rar, 1),
                base_price=_CRAFT_MAT_PRICE.get(_rar, 25),
            )

    def _init_pet_food(self, world: World, preset: Optional[WorldSimPreset] = None,
                       minimums: Optional[dict] = None):
        """[宠物食品 2026-08-24] 兜底宠物食品入世（6 题材确定池，幂等按名去重）。
        [方案A 2026-08-28] minimums={"pet_food": N}：LLM 已生成 N 件时只补差额（>=3 件全跳过）。

        type=consumable, category=pet_food, identified=True；rarity 取池内分档（驱动投食
        亲和分档）；base_price 据 rarity 种子价。确定性 id petfood_{md5}（可回放）。"""
        import hashlib as _hl
        existing = {it.name for it in world.items
                    if getattr(it, "category", "") == "pet_food"}
        _price = {"common": 20, "uncommon": 60, "rare": 200,
                  "epic": 800, "legendary": 2500, "mythic": 8000}
        tid = _p34a_genre_id(world)
        _pf_quota = None if minimums is None else max(0, 3 - int(minimums.get("pet_food", 0) or 0))
        for entry in pet_food_pool(world):
            if _pf_quota is not None and _pf_quota <= 0:
                break                       # LLM 已供足宠物食品，引擎不补
            name = entry.get("name", "")
            if not name or name in existing:
                continue
            h = _hl.md5(f"{tid}|pet_food|{name}".encode("utf-8")).hexdigest()[:8]
            rar = entry.get("rarity", "common")
            world.items.append(Item(
                id=f"petfood_{h}", name=name, type="consumable", rarity=rar,
                desc=entry.get("desc", ""), category="pet_food", identified=True,
                base_price=_price.get(rar, 20),
            ))
            existing.add(name)
            if _pf_quota is not None:
                _pf_quota -= 1

    def _init_foods(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[饱食度 2026-09-06 用户指示] 题材食物入世（hunger_enabled 才注入；纯引擎无 LLM）。

        type=consumable、category 留空（普通消耗品：商店备货可进货、吃了回饱食度——
        consume_effect {"type":"feed","hunger":N} 由 apply_consume_effect 结算）。
        确定性 id food_{md5}（可回放，仿 pet_food 口径）；幂等按名去重。
        [2026-09-08 修] 同名已存在但 consume_effect 缺 hunger（旧 _parse_consume_effect
        丢字段写盘的 amount:0 坏数据）-> 原地修复 hunger，不跳过。
        在 _init_recipes 之前（食物可成炼制候选）+ generate_shops_goods 之前（可上架）。
        """
        if not bool(getattr(preset, "hunger_enabled", False) or False):
            return
        import hashlib as _hl
        tid = _p34a_genre_id(world)
        by_name = {}
        for it in world.items:
            by_name.setdefault(it.name, it)
        for entry in foods_pool(world):
            name = entry.get("name", "")
            if not name:
                continue
            want = max(1, int(entry.get("feed", 45) or 45))
            old = by_name.get(name)
            if old is not None:
                eff = getattr(old, "consume_effect", None)
                if isinstance(eff, dict) and eff.get("type") == "feed" \
                        and int(eff.get("hunger", 0) or 0) <= 0:
                    eff["hunger"] = want       # 原地修复坏数据（引用即 world.items 成员）
                continue
            h = _hl.md5(f"{tid}|food|{name}".encode("utf-8")).hexdigest()[:8]
            world.items.append(Item(
                id=f"food_{h}", name=name, type="consumable", rarity="common",
                desc=entry.get("desc", ""), identified=True,
                base_price=max(1, int(entry.get("price", 10) or 10)),
                consume_effect={"type": "feed", "hunger": want},
            ))
            by_name[name] = world.items[-1]

    def _npc_buy_food(self, world: World, npc):
        """[修 2026-09-06 审计] NPC 在「当前地 + 相邻」商店买最便宜的 feed 食物（真实交易）。

        返回 (食物名, feed值, 实付价) 或 None（无店/无货/买不起）。
        与 daily_medicine_run 同口径：tre.buy_price 全价、wallet 扣款、货架 -1、
        货款入店主钱包（无店主则钱 sink）。食物即买即吃（不占 NPC 背包）。
        [审核修复 2026-09-13] 搜索半径原只认当前地点，比买药（当前地 + 相邻）窄一半——
        荒野/小型聚落的 NPC 明明隔壁村镇有吃的却白饿着。现与买药同口径，且按
        「当前地优先」显式排序候选店（买不到近的才去邻村）。
        """
        from src.services import trade_engine as tre
        from src.services.trade_engine import world_pace
        item_by_id = {getattr(i, "id", ""): i for i in (getattr(world, "items", None) or [])}
        loc = next((l for l in (getattr(world, "locations", None) or [])
                    if l.id == getattr(npc, "location_id", "")), None)
        if loc is None:
            return None
        wallet = int(getattr(npc, "wallet", 0) or 0)
        best = None   # (price, shop, entry, item)
        _allowed = {str(loc.id)} | {str(i) for i in (getattr(loc, "connections", None) or [])}
        _shops = [s for s in (getattr(world, "shops", None) or [])
                  if str(getattr(s, "location_id", "") or "") in _allowed]
        _shops.sort(key=lambda s: 0 if str(getattr(s, "location_id", "") or "") == str(loc.id) else 1)
        for shop in _shops:
            for entry in list(shop.stock or []):
                if int(getattr(entry, "stock", 0) or 0) <= 0:
                    continue
                it = item_by_id.get(entry.item_id)
                eff = getattr(it, "consume_effect", None) if it is not None else None
                if not (isinstance(eff, dict) and str(eff.get("type", "") or "") == "feed"):
                    continue
                price = tre.buy_price(entry, it, shop, 0, world_pace(world))
                if price <= 0 or price > wallet:
                    continue
                if best is None or price < best[0]:
                    best = (price, shop, entry, it)
        if best is None:
            return None
        price, shop, entry, it = best
        npc.wallet -= price
        entry.stock = int(entry.stock) - 1
        merchant = next((m for m in (getattr(world, "npcs", None) or [])
                         if getattr(m, "id", "") == getattr(shop, "merchant_npc_id", "")
                         and getattr(m, "alive", False)), None)
        if merchant is not None:
            merchant.wallet = int(getattr(merchant, "wallet", 0) or 0) + price
        feed_val = int((it.consume_effect or {}).get("hunger", 45) or 45)
        return it.name, feed_val, price

    def _ensure_food_supply(self, world: World, preset: Optional[WorldSimPreset] = None) -> bool:
        """[修 2026-09-06 审计] 食物自愈（旧档）：后开 hunger 的世界没跑过 _init_foods
        （生成时 hunger_enabled=False），全图既无食物物品也无货架食物——非城市 NPC
        买食路径无物可买。幂等：注入缺失食物物品 + 给无食物货架的聚落商店上 2 件
        最便宜食物（确定性排序）。返回是否改动（调用方据此落盘）。
        """
        if not bool(getattr(preset, "hunger_enabled", False) or False):
            return False
        self._init_foods(world, preset)
        foods_all = [i for i in (getattr(world, "items", None) or [])
                     if isinstance(getattr(i, "consume_effect", None), dict)
                     and str(i.consume_effect.get("type", "") or "") == "feed"]
        if not foods_all:
            return False
        foods = sorted(foods_all, key=lambda i: (int(getattr(i, "base_price", 99) or 99), i.id))[:2]
        food_ids = {i.id for i in foods}
        # [!] 不按地点 kind 过滤：沧浪渡这类 wilderness 渡口也有药店/货摊（有店即有市），
        # 白水镇无商店则由觅食兜底——按 settlement 过滤会把渡口 NPC 排除在买食之外
        dirty = False
        for shop in (getattr(world, "shops", None) or []):
            has_food = any(e.item_id in food_ids and int(e.stock or 0) != 0
                           for e in (shop.stock or []))
            if has_food:
                continue
            for it in foods:
                if any(e.item_id == it.id for e in (shop.stock or [])):
                    continue
                from src.models.shop import ShopStockEntry
                shop.stock.append(ShopStockEntry(
                    item_id=it.id, price=max(1, int(getattr(it, "base_price", 10) or 10)),
                    stock=3, max_stock=3))
                dirty = True
        return dirty

    def _tick_daily_regen(self, world: World, tick: int,
                          preset: Optional[WorldSimPreset] = None) -> list:
        """[自然恢复 2026-09-06 用户指示] 每日跨日自然恢复（纯引擎零 LLM，静默）。

        玩家与全存活 NPC 各回 hp_max 的 daily_hp_regen_pct%（默认 30；0=关），
        钳上限。水位 last_regen_day 每日 1 次；不追补（跳天只按 1 日回——自然
        恢复是「静养自愈」的世界规则，离线快进不叠层）。不产事件（HP 条可见）。
        [饱食度闸 2026-09-10 用户指示] hunger_enabled 且该单位饱食度 <50（半饱
        以下）当日不恢复——饿着不回血；hunger None=满食按 100 口径（同
        _tick_hunger），hunger_enabled 关闭的世界不受此闸影响。
        """
        pct = max(0, min(100, int(self._per_world(
            world, "daily_hp_regen_pct",
            getattr(preset, "daily_hp_regen_pct", 30) if preset is not None else 30,
            preset) or 0)))
        if pct <= 0:
            return []
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        last = int(getattr(world, "last_regen_day", 0) or 0)
        if day <= last:
            return []
        world.last_regen_day = day
        amt = lambda hp_max: max(1, (int(hp_max) * pct + 99) // 100)   # 向上取整

        hunger_gate = bool(self._per_world(
            world, "hunger_enabled",
            getattr(preset, "hunger_enabled", False) if preset is not None else False,
            preset))

        def _starving(u) -> bool:
            if not hunger_gate:
                return False
            h = getattr(u, "hunger", None)
            return int(h if h is not None else 100) < 50   # 恰好 50 照常回（严格小于才拦）

        p = getattr(world, "player", None)
        if p is not None and not _starving(p):
            hp_max = max(1, int(getattr(p, "hp_max", 1) or 1))
            if int(getattr(p, "hp", 0) or 0) < hp_max:
                p.hp = min(hp_max, int(getattr(p, "hp", 0) or 0) + amt(hp_max))
        for n in (getattr(world, "npcs", None) or []):
            if not getattr(n, "alive", True) or _starving(n):
                continue
            hp_max = max(1, int(getattr(n, "hp_max", 1) or 1))
            if int(getattr(n, "hp", 0) or 0) < hp_max:
                n.hp = min(hp_max, int(getattr(n, "hp", 0) or 0) + amt(hp_max))
        # [P52 伤疤] 伤情痊愈：按 day 水位清过期伤（until_day <= today 即痊愈）
        for u in [world.player] + [n for n in (world.npcs or []) if getattr(n, "alive", True)]:
            inj = getattr(u, "injuries", None)
            if isinstance(inj, list) and inj:
                keep = [i for i in inj if isinstance(i, dict)
                        and int(i.get("until_day", 0) or 0) > day]
                if len(keep) != len(inj):
                    u.injuries = keep
        # [P56 毕生所愿] 每日检查执念达成（major 事件上世界新闻）
        return self.life_goal_check(world)

    def _tick_hunger(self, world: World, tick: int) -> list:
        """[饱食度] 每日结算：饱食度衰减 + NPC 自动进食（纯引擎无 LLM）。

        - 衰减：day_count 水位（last_hunger_day），每日 1 次全存活 NPC + 玩家 -34
          （约 3 天见底）；跳天追补 cap 2 天（离线回归不把全图饿趴）。
        - NPC 进食（衰减后同段跑，hunger < 60 触发）：先吃背包食物（feed 类 +50），
          否则去当前地点商店买食物（真实交易；无店/无货/没钱则觅食兜底）。
          产 minor 事件进动态日志（守因果可见性）。
        """
        events: list = []
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        last = int(getattr(world, "last_hunger_day", 0) or 0)
        elapsed = min(2, day - last) if last > 0 else 1   # 追补 cap 2；首日(0->1)按 1 天
        if day <= last:
            return events
        decay = 34 * max(1, elapsed)
        world.last_hunger_day = day
        # 玩家衰减（不吃就掉到饥饿线以下触发全属性减半）
        try:
            p = world.player
            # [修 2026-09-06] 饱食 0 是合法值——`x or 100` 会把 0 当缺省翻成 100，
            # 饿到 0 反而被减出 66（凭空回饱）；None 感知缺省才是对的原义
            _ph = getattr(p, "hunger", None)
            p.hunger = max(0, min(100, int(_ph if _ph is not None else 100) - decay))
        except Exception:
            pass
        # NPC 衰减 + 用餐
        from src.models.world import WorldEvent
        ate_log = []
        for npc in world.npcs:
            if not getattr(npc, "alive", True):
                continue
            _nh = getattr(npc, "hunger", None)
            npc.hunger = max(0, min(100, int(_nh if _nh is not None else 100) - decay))
            if npc.hunger >= 60:
                continue
            # 1) 背包有食物先吃（+50）
            ate_food = None
            for iid in list(getattr(npc, "inventory", None) or []):
                it = next((i for i in world.items if i.id == iid), None)
                eff = getattr(it, "consume_effect", None) if it is not None else None
                if isinstance(eff, dict) and str(eff.get("type", "") or "") == "feed":
                    ate_food = it
                    break
            if ate_food is not None:
                npc.inventory.remove(ate_food.id)
                npc.hunger = min(100, npc.hunger + int(ate_food.consume_effect.get("hunger", 45) or 45))
                ate_log.append(f"{npc.name}吃了{ate_food.name}")
                continue
            # 2) [2026-09-08 用户指示] 食堂堂食已删：饿了去当前地点商店买食物
            #     （钱包扣款/货架 -1/货款 sink——与买药同口径经济闭环）；
            #     无店/无货/没钱 -> 觅食兜底（确定性 roll，失败饿着留叙事余地）
            if int(npc.hunger) < 60:
                bought = self._npc_buy_food(world, npc)
                if bought is not None:
                    food_name, feed_val, paid = bought
                    npc.hunger = min(100, int(npc.hunger) + feed_val)
                    # [修 2026-10-02 §23] 币种零硬编码：货币名走 GenreText（末日=晶核，
                    # 真机档事件文案「花3钱」曾与题材货币名不一致）。
                    ate_log.append(f"{npc.name}花{paid}"
                                   f"{self._genre_text(world).currency}"
                                   f"买了{food_name}充饥")
                else:
                    frng = SeededRng.seed_from(getattr(world, "id", ""), tick,
                                               f"forage_{npc.id}")
                    if frng.chance(0.65):
                        npc.hunger = min(100, int(npc.hunger) + 40)
                        ate_log.append(f"{npc.name}在附近觅得吃食充饥")
        if ate_log:
            events.append(WorldEvent(tick=tick, category="npc", severity="minor",
                                     title="NPC 进食", desc="；".join(ate_log[:6])))
        return events

    # ---------- [野心 2026-09-06 用户指示] 一生追求（全员无差别）----------
    def _assign_ambition(self, world: World, npc, rng) -> bool:
        """给 NPC 抽一个新野心（题材池确定性挑选；revenge/courtship 需目标，无合适
        目标跳过该类）。已有未完成野心/冷却中返回 False。"""
        if not getattr(npc, "alive", True):
            return False
        amb = getattr(npc, "ambition", None)
        if isinstance(amb, dict) and amb.get("kind"):
            return False
        if isinstance(amb, dict) and int(amb.get("done_day", 0) or 0) > 0:
            day = max(1, int(getattr(world, "day_count", 1) or 1))
            if day - int(amb.get("done_day", 0) or 0) < 2:
                return False                       # 完成 2 天后再追新目标
        pool = _ambition_pool(world)
        kinds = [k for k in pool if pool.get(k)]
        if not kinds:
            return False
        # 确定性起始偏移轮询（SeededRng 无 shuffle；同 NPC 同 day 同结果）
        offset = rng.roll(0, len(kinds) - 1) if len(kinds) > 1 else 0
        kinds = kinds[offset:] + kinds[:offset]
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        others = [n for n in world.npcs if n.id != npc.id and getattr(n, "alive", True)]
        for kind in kinds:
            if kind == "revenge":
                foes = [n for n in others
                        if int((getattr(npc, "social", None) or {}).get(str(n.id), 0) or 0) <= -25]
                if not foes:
                    continue
                tgt = sorted(foes, key=lambda n: n.id)[0]
                npc.ambition = {"kind": "revenge", "target_npc_id": tgt.id,
                                "target_value": 0, "born_day": day, "done_day": 0,
                                "desc": rng.pick(pool["revenge"])}
                return True
            if kind == "courtship":
                dear = [n for n in others
                        if int((getattr(npc, "social", None) or {}).get(str(n.id), 0) or 0) >= 25]
                tgt = (sorted(dear, key=lambda n: n.id)[0] if dear
                       else (sorted(others, key=lambda n: n.id)[0] if others else None))
                if tgt is None:
                    continue
                npc.ambition = {"kind": "courtship", "target_npc_id": tgt.id,
                                "target_value": 80, "born_day": day, "done_day": 0,
                                "desc": rng.pick(pool["courtship"])}
                return True
            target_value = {"wealth": 500 + max(1, int(getattr(npc, "level", 1) or 1)) * 100,
                            "mastery": max(1, int(getattr(npc, "level", 1) or 1)) + 5,
                            "fame": 4, "explore": 5, "collect": 3, "craft": 5}.get(kind, 0)
            npc.ambition = {"kind": kind, "target_npc_id": "", "target_value": target_value,
                            "born_day": day, "done_day": 0,
                            "desc": rng.pick(pool[kind])}
            return True
        return False

    def assign_all_ambitions(self, world: World) -> int:
        """[野心] 全员分配（build 挂载；确定性 seed 逐 NPC）。返回分配数。"""
        rng = SeededRng.seed_from(world.id, 0, "ambitions")
        n = 0
        for npc in world.npcs:
            if self._assign_ambition(world, npc, rng):
                n += 1
        return n

    def _ambition_progress(self, world: World, npc) -> str:
        """[野心] 进度文案（详情/注入共用；无野心返回空串）。"""
        amb = getattr(npc, "ambition", None)
        if not isinstance(amb, dict) or not amb.get("kind"):
            return ""
        kind = str(amb.get("kind", ""))
        tv = int(amb.get("target_value", 0) or 0)
        if kind == "wealth":
            return f"（{int(getattr(npc, 'wallet', 0) or 0)}/{tv} 钱）"
        if kind == "revenge":
            t = next((n for n in world.npcs if n.id == str(amb.get("target_npc_id", ""))), None)
            if t is None:
                return "（仇人已不在人世）"
            return f"（目标：{t.name}{'，已得报' if not t.alive else ''}）"
        if kind == "courtship":
            t = next((n for n in world.npcs if n.id == str(amb.get("target_npc_id", ""))), None)
            if t is None:
                return "（心系之人已不在）"
            soc = int((getattr(npc, "social", None) or {}).get(str(t.id), 0)
                      or (getattr(t, "social", None) or {}).get(str(npc.id), 0) or 0)
            return f"（与{t.name}的情谊 {soc}/{tv}）"
        if kind == "mastery":
            return f"（当前 {max(1, int(getattr(npc, 'level', 1) or 1))}/{tv} 级）"
        if kind == "fame":
            friends = sum(1 for v in (getattr(npc, "social", None) or {}).values()
                          if int(v or 0) >= 60)
            return f"（挚友 {friends}/{tv}）"
        if kind == "explore":
            return f"（已到访 {len(getattr(npc, 'visited_locations', None) or [])}/{tv} 地）"
        if kind == "collect":
            inv = getattr(npc, "inventory", None) or []
            rares = sum(1 for iid in inv
                        if getattr(next((i for i in world.items if i.id == iid), None),
                                   "rarity", "") in ("rare", "epic", "legendary", "mythic"))
            return f"（珍藏 {rares}/{tv} 件）"
        if kind == "craft":
            return f"（已制成 {max(0, int(getattr(npc, 'crafted_count', 0) or 0))}/{tv} 件）"
        return ""

    def _ambition_done(self, world: World, npc) -> bool:
        amb = getattr(npc, "ambition", None)
        if not isinstance(amb, dict) or not amb.get("kind"):
            return False
        kind = str(amb.get("kind", ""))
        tv = int(amb.get("target_value", 0) or 0)
        if kind == "wealth":
            return int(getattr(npc, "wallet", 0) or 0) >= max(1, tv)
        if kind == "revenge":
            t = next((n for n in world.npcs if n.id == str(amb.get("target_npc_id", ""))), None)
            return t is None or not getattr(t, "alive", True)
        if kind == "courtship":
            t = next((n for n in world.npcs if n.id == str(amb.get("target_npc_id", ""))), None)
            if t is None:
                return False
            soc = int((getattr(npc, "social", None) or {}).get(str(t.id), 0)
                      or (getattr(t, "social", None) or {}).get(str(npc.id), 0) or 0)
            return soc >= max(1, tv)
        if kind == "mastery":
            return max(1, int(getattr(npc, "level", 1) or 1)) >= max(1, tv)
        if kind == "fame":
            return sum(1 for v in (getattr(npc, "social", None) or {}).values()
                       if int(v or 0) >= 60) >= max(1, tv)
        if kind == "explore":
            return len(getattr(npc, "visited_locations", None) or []) >= max(1, tv)
        if kind == "collect":
            inv = getattr(npc, "inventory", None) or []
            return sum(1 for iid in inv
                       if getattr(next((i for i in world.items if i.id == iid), None),
                                  "rarity", "") in ("rare", "epic", "legendary", "mythic")) >= max(1, tv)
        if kind == "craft":
            return max(0, int(getattr(npc, "crafted_count", 0) or 0)) >= max(1, tv)
        return False

    def _tick_ambitions(self, world: World, tick: int) -> list:
        """[野心] 日结算：完成判定 + 产事件 + 冷却后抽新（day 水位；纯引擎零 LLM）。"""
        events: list = []
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        last = int(getattr(world, "last_ambition_day", 0) or 0)
        if day <= last:
            return []
        world.last_ambition_day = day
        rng = SeededRng.seed_from(world.id, day, "ambitions_tick")
        from src.models.world import WorldEvent
        done_log = []
        for npc in world.npcs:
            if not getattr(npc, "alive", True):
                continue
            amb = getattr(npc, "ambition", None)
            if isinstance(amb, dict) and amb.get("kind"):
                if self._ambition_done(world, npc):
                    desc = str(amb.get("desc", "") or "多年的心愿")
                    # [P47-C3 复仇闭环 三模型共识] 玩家手刃其仇人 -> 三重感激：交情 +15、
                    # 印象「无畏」+15、记忆笔记（私聊/召回自然带出）。原实现对「是谁干的」
                    # 完全无感——仇人死了就是死了，替人报仇的玩家零反馈。
                    _avenged = False
                    if str(amb.get("kind", "")) == "revenge":
                        _tgt = str(amb.get("target_npc_id", "") or "")
                        _deaths = getattr(world.player, "codex_deaths", None) or {}
                        _avenged = bool(_tgt) and any(str(k) == _tgt for k in _deaths)
                    done_log.append(f"{npc.name}了却了心愿——{desc}"
                                    + ("（据悉，他的仇人正是死于玩家之手）" if _avenged else ""))
                    npc.ambition = {"kind": "", "done_day": day}
                    if _avenged:
                        try:
                            nre.add_affinity(npc, 15)
                            soc.update_impression(npc, "无畏", trust_delta=15, firsthand=True)
                            try:
                                self.npc_memory().record_chat_note(
                                    world, str(getattr(npc, "id", "") or ""),
                                    "玩家替我报了血仇，这份恩情我记下了")
                            except Exception:
                                pass
                        except Exception:
                            pass
                    # 完成的喜悦外溢：同地熟人小额认可（社交 +2）
                    try:
                        for other in world.npcs:
                            if other.id != npc.id and getattr(other, "alive", True) \
                                    and other.location_id == npc.location_id \
                                    and abs(soc._social_get(other, str(npc.id))) >= 25:
                                # [修 2026-09-10] 走 _social_set 双向写：原实现只写 other
                                # 单向，破坏 NPC.social 双向对称不变量（估值 +2 钳 ±100）。
                                soc._social_set(npc, other, max(
                                    -100, min(100, soc._social_get(other, str(npc.id)) + 2)))
                    except Exception:
                        pass
                continue
            self._assign_ambition(world, npc, rng)      # 空槽/冷却到 -> 抽新
        if done_log:
            events.append(WorldEvent(tick=tick, category="npc", severity="minor",
                                     title="心愿得偿", desc="；".join(done_log[:6])))
        return events

    def _tick_impressions(self, world: World, tick: int) -> list:
        """[玩家印象 2026-09-06 用户指示] 传闻浅印象日批：关于玩家的传闻（desc 含玩家名）
        扩散后，知道该传闻且对玩家无 firsthand 印象的 NPC 打浅「危险」标签（不加熟悉度
        ——道听途说永远浅、可能过时；已有深印象者不被传闻覆盖）。day 水位每日一次。"""
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        last = int(getattr(world, "last_impression_day", 0) or 0)
        if day <= last:
            return []
        world.last_impression_day = day
        try:
            from src.services import rumor_engine as rum
        except Exception:
            return []
        pname = str(getattr(getattr(world, "player", None), "name", "") or "").strip()
        # [修 2026-09-10] 字段名对齐 rumor_engine.commit 的写入键（text）——原来读 "desc"
        # 恒空，整条「传闻浅印象」链路是死代码（player_rumors 空则直接 return）。
        # [审核修复 2026-09-13] 命中条件再放宽两处——原写法在真实存档里仍几乎恒空：
        #   1) 未绑用户卡时 pname 为空即 return（实测存档 player.name=null）；
        #   2) 引擎事件文案一律写字面「玩家」（domain/home/relation，全仓无一处用
        #      player.name），而 shared_rumors.text 是 **LLM 口述化后** 的文本（"玩家"
        #      常被改写成代称），只有 title 保留原文（如「婚礼：玩家与X」「开辟据点」）。
        # 故关键词 = 玩家真名 + 字面「玩家」，匹配范围 = text + title。
        _keys = [k for k in (pname, "玩家") if k]
        if not _keys:
            return []
        player_rumors = [r for r in (getattr(world, "shared_rumors", None) or [])
                         if isinstance(r, dict)
                         and any(k in (str(r.get("text", "") or "") + str(r.get("title", "") or ""))
                                 for k in _keys)]
        if not player_rumors:
            return []
        for npc in world.npcs:
            if not getattr(npc, "alive", True) or getattr(npc, "hostile", False):
                continue
            imp = getattr(npc, "player_impression", None)
            if isinstance(imp, dict) and int(imp.get("familiarity", 0) or 0) > 0:
                continue                      # 已有亲眼印象，传闻不覆盖
            for r in player_rumors[:2]:
                try:
                    if rum.knows(npc, r):
                        # [审核修复 2026-09-13] 传闻浅印象细分（原一律「危险」）：
                        # 同一件事在不同 NPC 心里本该不同——听说你火并结仇的觉得你「危险」，
                        # 听说你置产豪掷的觉得你「富有」，听说你赈济接济的觉得你「慷慨」。
                        _tag, _delta = _impression_for_rumor(r)
                        soc.update_impression(npc, _tag, trust_delta=_delta, firsthand=False)
                        break
                except Exception:
                    break
        return []

    def _normalize_functional_items(self, items: list, tid: str) -> dict:
        """[方案A 2026-08-28] LLM 功能性物品收编校验层（在 _init_reagents/_init_seeds/
        _init_pet_food 之前跑，build_world_from_skeleton 于物品解析后立即调）。

        LLM 按物品生成提示词规则 8 产出功能性物品（名字/desc 贴合题材），引擎在此钉契约：
        - cultivate：type 纠正为 consumable、identified=True、reagent_kind 非法时按 desc
          关键词兜底（鉴定/扫瞄->identify，洗练/重铸->refine，资质/洗髓->apt_all），
          仍非法则按类别配额轮转（identify/refine/apt_all 顺序）；
        - seed：type 纠正为 material、重写确定性 id = fe.seed_id_of(题材,名)（farm 的
          crop_of_seed 按此 id 反查，LLM 随机 id 会致播种「不属于本地风土」）；同名作物
          一并入世（category=craft），否则收获产物无物可给；
        - pet_food：type 纠正为 consumable、rarity 回退 common（投食分档按 rarity 查表）；
        - 非功能物品误标 category（如武器标 cultivate）：category 清空退普通物品。

        [修 2026-09-14] 接口从 (world) 改为 (items, tid)：本层会**原地重写种子物品 id**，
        必须发生在一切名字 -> id 解析（配方 inputs/output、怪物 loot、NPC inventory、
        任务奖励）之前——旧顺序在 build 后段才跑，先解析出的旧 uuid 全成悬空引用
        （真机档「海藻糊糊」等三条药方永缺一件料）。调用点已前置到 items 解析之后、
        item_by_name 构建之前；此处只做物品级收编，不接 world。

        返回各功能类已有计数 {"cultivate": N, "seed": N, "pet_food": N}，
        引擎注入层据此只补差额（LLM 漏生成时引擎池兜底，不空窗也不双份）。"""
        counts = {"cultivate": 0, "seed": 0, "pet_food": 0}
        kind_cycle = ["identify", "refine", "apt_all"]
        # [!] 迭代中不可 append items（生成器永不终止）——待加作物先缓存
        _pending_crops: list = []
        _seen_crop_ids = {getattr(i, "id", "") for i in items}
        for it in items:
            cat = str(getattr(it, "category", "") or "")
            # LLM 未标 category 但 desc 强指示功能类的，收编进对应类
            if cat not in ("cultivate", "seed", "pet_food"):
                desc = str(getattr(it, "desc", "") or "")
                rk = str(getattr(it, "reagent_kind", "") or "")
                if rk in ("identify", "refine", "apt_atk", "apt_def", "apt_hp",
                          "apt_mp", "apt_spd", "apt_all", "beast_skill"):
                    cat = "cultivate"
                elif "种子" in str(getattr(it, "name", "")) or "播种" in desc:
                    cat = "seed"
                else:
                    continue
            if cat == "cultivate":
                it.type = "consumable"
                it.identified = True
                it.category = "cultivate"
                rk = str(getattr(it, "reagent_kind", "") or "")
                if rk not in ("identify", "refine", "apt_atk", "apt_def", "apt_hp",
                              "apt_mp", "apt_spd", "apt_all", "beast_skill"):
                    desc = str(getattr(it, "desc", "") or "") + str(getattr(it, "name", "") or "")
                    if any(k in desc for k in ("鉴定", "扫描", "侦测", "分析")):
                        it.reagent_kind = "identify"
                    elif any(k in desc for k in ("洗练", "重铸", "重锻", "改造")):
                        it.reagent_kind = "refine"
                    elif any(k in desc for k in ("资质", "洗髓", "培养", "宠物")):
                        it.reagent_kind = "apt_all"
                    else:
                        it.reagent_kind = kind_cycle[counts["cultivate"] % len(kind_cycle)]
                if not it.base_price:
                    it.base_price = 80          # cultivate 种子价（商店仍排除）
                counts["cultivate"] += 1
            elif cat == "seed":
                it.type = "material"
                it.category = "seed"
                it.identified = True
                # [!] 确定性 id 重写：farm.crop_of_seed 按 seed_id_of(题材,名) 反查，
                # LLM 随机 uuid id 会让播种永久「不属于本地风土」
                from src.services import farm_engine as _fe
                new_id = _fe.seed_id_of(tid, it.name)
                if new_id and new_id != it.id:
                    it.id = new_id
                # 同名作物并入世（收获产物；无则 harvest 给不出东西）；缓存待加防迭代变异
                crop_id = _fe.crop_id_of(tid, it.name)
                if crop_id and crop_id not in _seen_crop_ids:
                    _seen_crop_ids.add(crop_id)
                    _pending_crops.append(Item(
                        id=crop_id, name=it.name, type="material", category="craft",
                        rarity=it.rarity or "common", level=max(1, it.level or 1),
                        identified=True,
                        base_price=max(10, int(getattr(it, "base_price", 0) or 10) * 2),
                        desc=f"亲手种出的{it.name}，可入药炼制或出售。"))
                counts["seed"] += 1
            elif cat == "pet_food":
                it.type = "consumable"
                it.category = "pet_food"
                it.identified = True
                if it.rarity not in ("common", "uncommon", "rare", "epic", "legendary", "mythic"):
                    it.rarity = "common"       # 投食分档按 rarity 查表
                if not it.base_price:
                    it.base_price = {"common": 20, "uncommon": 60, "rare": 200}.get(it.rarity, 20)
                counts["pet_food"] += 1
        if _pending_crops:
            items.extend(_pending_crops)
        return counts

    def _init_seeds(self, world: World, preset: Optional[WorldSimPreset] = None,
                    minimums: Optional[dict] = None):
        """[P35] 种植入世：种子（category=seed）+ 作物（category=craft）物品入 world.items
        （幂等按 id），tier<=2 野外资源点 40%（确定性）追加种子掉落。守数值范式：纯引擎
        确定性生成，数值零 LLM。farm_enabled=False 跳过。
        [方案A 2026-08-28] minimums={"seed": N}：LLM 已生成 N 种种子时引擎池补差额到 6
        （题材作物池基线；LLM 满供时零注入）。"""
        if preset is not None and not getattr(preset, "farm_enabled", True):
            return
        tid = fe._genre_id(world)
        existing = {getattr(it, "id", "") for it in (getattr(world, "items", None) or [])}
        rarity_lvl = {"common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5, "mythic": 6}
        seed_candidates = []
        # [方案A] minimums.seed 余额（LLM 已出 N 种 -> 引擎补 6-N；None=旧行为全量）
        _seed_quota = None if minimums is None else max(0, 6 - int(minimums.get("seed", 0) or 0))
        for crop in fe.genre_crops(world):
            sid = fe.seed_id_of(tid, crop["name"])
            cid = fe.crop_id_of(tid, crop["name"])
            if _seed_quota is not None and _seed_quota <= 0:
                seed_candidates.append(sid)     # 掉落候选仍收（LLM 种子也在池）
                continue
            if sid not in existing and cid not in existing:
                world.items.append(Item(
                    id=sid, name=f"{crop['name']}种子", type="material", category="seed",
                    rarity=crop["rarity"], level=rarity_lvl.get(crop["rarity"], 1),
                    identified=True,
                    # [修 2026-10-02 P3] 田名题材化（原硬编码「灵田」——仙侠词泄漏进末日）
                    desc=f"{crop['name']}的种子，播种于{fe.field_display_name(world)}"
                         f"约 {crop['base_days']} 天成熟。"))
                world.items.append(Item(
                    id=cid, name=crop["name"], type="material", category="craft",
                    rarity=crop["rarity"], level=rarity_lvl.get(crop["rarity"], 1),
                    identified=True,
                    desc=f"亲手种出的{crop['name']}，可入药炼制或出售。"))
                if _seed_quota is not None:
                    _seed_quota -= 1
            seed_candidates.append(sid)
        # 野外资源点种子掉落（tier<=2 节点 40% 确定性追加一颗低 rarity 种子）
        rng = SeededRng.seed_from(getattr(world, "id", ""), 0, "seed_drops")
        for loc in (getattr(world, "locations", None) or []):
            for node in (getattr(loc, "resource_nodes", None) or []):
                tier = int(getattr(node, "tier", 1) or 1)
                if tier > 2 or rng.chance(0.6):
                    continue
                drops = getattr(node, "drops", None)
                if not isinstance(drops, list):
                    continue
                sid = rng.pick(seed_candidates)
                if sid and not any(isinstance(d, dict) and d.get("item_id") == sid for d in drops):
                    drops.append({"item_id": sid, "qty": 1, "rate": 0.3})

    @staticmethod
    def _effective_recipe_target(preset) -> int:
        """[数值对齐 2026-09-06] 配方目标联动物品量：gen_recipe_count 旋钮是下限，实际目标
        自动抬高到 max(8, item_gen_count // 2)——旧固定 8 条在物品池扩容（可调到 200）时
        锻造/炼制覆盖率断崖（60 件物品仍 8 条配方）。钳 3-100 与旋钮同口径。"""
        ic = max(4, min(200, int(getattr(preset, "item_gen_count", 12) or 12)))
        rc = max(3, min(100, int(getattr(preset, "gen_recipe_count", 8) or 8)))
        return min(100, max(rc, max(8, ic // 2)))

    def _init_recipes(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[P12] 合成配方：LLM 配方优先，引擎兜底补齐到目标数且覆盖各资源层级。

        覆盖性：tier 1-5 每层的材料至少被 1 个配方消耗（该层没配方才补）；
        [数值对齐 2026-09-06] 目标数 = max(gen_recipe_count, 物品数//2)（旋钮为下限），
        数量补齐段优先为「尚无配方的装备/消耗品」建配方（真覆盖物品而非凑数）。
        守数值范式铁律（配方定义静态，craft 动态纯 Python）。
        """
        if not getattr(preset, "crafting_enabled", True):
            return
        # [!] 方法级 import 必须在使用点之前——下方 mats/materials 过滤（P35 排除种子）与
        # 建筑门控段都引用它；原先只在方法尾 if 块内 import，头部引用成自由变量 NameError。
        from src.models.world import effective_category
        target = self._effective_recipe_target(preset)
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        station = {"xianxia": "炼丹炉", "wuxia": "药臼", "modern": "工作台",
                   "scifi": "合成终端", "apocalypse": "修补台",
                   "western_fantasy": "工坊"}.get(tid, "工坊")
        # LLM 配方缺 station 的补题材工坊名
        for r in world.recipes:
            if not getattr(r, "station", ""):
                r.station = station
        # [P34c 修复 2026-08-21] 勿在 LLM 配方数达标时提前 return：下方「层级覆盖/数量补齐」
        # 两段自带 >= target 守卫会自然跳过，但 [P34c] 建筑门控配方段必须无条件执行——
        # LLM 出满 8 条配方就早退会让真实世界 0 条门控配方，锻造渠道（高 level 物品三来源
        # 之一）整体断裂（p34r2 验收发现：forge/alchemy 配方 0 条）。
        rng = SeededRng.seed_from(world.id, 0, "recipes")
        # [P8] 配方难度据产出稀有度：common=0, uncommon=8, rare=20, epic=35, legendary=50。
        _RARITY_DIFF = {"common": 0, "uncommon": 8, "rare": 20, "epic": 35, "legendary": 50, "mythic": 65}
        _TIER_RAR = {1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary"}

        def _diff(out_it):
            return _RARITY_DIFF.get(getattr(out_it, "rarity", "common") or "common", 0)

        # [住宅网格 2026-08-24] 配方按建筑等级解锁：产出 type 决定门控建筑
        # （weapon/armor->forge，consumable->alchemy），min_building_level 随产出品级升。
        _GATE_LVL = {"common": 1, "uncommon": 1, "rare": 2, "epic": 3,
                     "legendary": 4, "mythic": 5}

        def _gate(out_it):
            if not getattr(preset, "buildings_enabled", True):
                return "", 0, 0
            t = getattr(out_it, "type", "")
            rb = "forge" if t in ("weapon", "armor") else ("alchemy" if t == "consumable" else "")
            if not rb:
                return "", 0, 0
            ml = _GATE_LVL.get(getattr(out_it, "rarity", "common") or "common", 1)
            return rb, ml, ml

        def _add(inputs: list, out_it, required_building: str = "",
                 min_building_level: int = 0, output_level: int = 0):
            names = "、".join(i.name for i in inputs)
            world.recipes.append(Recipe(
                name=f"炼制{out_it.name}",
                desc=f"于{station}用{names}炼制{out_it.name}",
                inputs=[i.id for i in inputs], output_item_id=out_it.id, station=station,
                difficulty=_diff(out_it),
                required_building=required_building,
                min_building_level=min_building_level,
                output_level=output_level))

        # ---- 层级覆盖：每层材料至少进 1 个配方（层内挑 2 材料 -> 对应稀有度产出）----
        # [数值对齐 2026-09-06] 产出池排除功能道具（鉴定卷轴/宠物食品/技能书不作配方产出）
        # 与 teach_skill 同因：鉴定洗练道具只能肝/掉/拍（P34a 经济闭环）。
        consumables = [it for it in world.items if it.type == "consumable"
                       and not (isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill)
                       and not str(getattr(it, "category", "") or "")]
        weapons = [it for it in world.items if it.type == "weapon"]
        for tier in range(1, 6):
            if len(world.recipes) >= target:
                break
            mats = [it for it in world.items if it.type == "material"
                    and it.rarity == _TIER_RAR[tier]
                    and effective_category(it) != "seed"]   # [P35] 种子留作播种不进配方
            if not mats:
                continue
            # 该层材料已被现有配方消耗则跳过
            used = {iid for r in world.recipes for iid in (r.inputs or [])}
            if any(m.id in used for m in mats):
                continue
            out_rar = _TIER_RAR[tier]
            outs = [c for c in consumables if c.rarity == out_rar] or \
                   [w for w in weapons if w.rarity == out_rar] or \
                   [c for c in consumables] or weapons
            if not outs:
                continue
            _o = rng.pick(outs)
            # [修 2026-10-01] inputs 按产出类型匹配：锻造(weapon/armor)优先吃
            # forge 类材料，炼丹(consumable)优先吃 craft 类——武侠档
            # 「锻造全用药材」源于此前不分混抽
            _forge_mats = [m for m in mats if str(getattr(m, "category", "") or "") == "forge"]
            _craft_mats = [m for m in mats if str(getattr(m, "category", "") or "") == "craft"]
            if _o.type in ("weapon", "armor"):
                _pool = _forge_mats if len(_forge_mats) >= 2 else mats
            else:
                _pool = _craft_mats if len(_craft_mats) >= 2 else mats
            ins = rng.sample(_pool, min(2, len(_pool)))
            _rb, _ml, _ol = _gate(_o)
            _add(ins, _o, required_building=_rb, min_building_level=_ml, output_level=_ol)
        # ---- 数量补齐：[数值对齐 2026-09-06] 优先为「尚无配方的装备/消耗品」建配方
        # （材料按产出品级档就近匹配），全部覆盖过再退回通用组合（2 材料 -> 随机消耗品）。
        # guard 随 target 缩放（旧固定 40 在大 target 时凑不满）。----
        materials = [it for it in world.items if it.type == "material"
                     and effective_category(it) != "seed"]  # [P35] 种子不当材料
        _covered = {r.output_item_id for r in world.recipes}
        outs_pending = [it for it in world.items
                        if it.type in ("weapon", "armor", "accessory", "consumable")
                        and not (isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill)
                        and it.id not in _covered]
        _RAR_TIER = {rar: tier for tier, rar in _TIER_RAR.items()}
        guard = 0
        while len(world.recipes) < target and len(materials) >= 2 and \
                (outs_pending or consumables) and guard < target * 4:
            guard += 1
            if outs_pending:
                out = outs_pending.pop(0)
                # 材料就近匹配产出品级档（rare 产出优先吃 rare/uncommon 材料）
                want = _RAR_TIER.get(out.rarity)
                pref = [m for m in materials if _RAR_TIER.get(m.rarity) == want] \
                    if want else []
                pool_m = pref if len(pref) >= 2 else materials
                ins = rng.sample(pool_m, min(2, len(pool_m)))
            else:
                ins = rng.sample(materials, 2)
                out = rng.pick(consumables)
                if any(r.output_item_id == out.id and set(r.inputs or []) == {i.id for i in ins}
                       for r in world.recipes):
                    continue
            _rb, _ml, _ol = _gate(out)
            _add(ins, out, required_building=_rb, min_building_level=_ml, output_level=_ol)
        # ---- [P34c] 建筑门控配方：高 level 物品需对应建筑（forge 出 weapon/armor、alchemy 出
        # consumable/药）。产出 identified=False 需鉴定（craft 引擎强制）。buildings_enabled=False 跳过。
        # [数值对齐 2026-09-06] 门控数量随装备/消耗品池自适应（min 3 -> max 6；旧固定各 3 条
        # 在物品池扩容后锻造覆盖过窄）。
        max_lvl = max(0, int(getattr(preset, "item_max_level", 6) or 0))
        forge_mats = [it for it in world.items if it.type == "material"
                      and effective_category(it) == "forge"]
        craft_mats = [it for it in world.items if it.type == "material"
                      and effective_category(it) == "craft"]
        weapons_all = [it for it in world.items if it.type == "weapon"]
        armors_all = [it for it in world.items if it.type == "armor"]
        consumables_all = [it for it in world.items if it.type == "consumable"
                           and not (isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill)]
        # forge 配方：weapon/armor 产出，需 forge 建筑，output_level 据 rarity
        forge_outs = weapons_all + armors_all
        forge_n = min(6, max(3, len(forge_outs) // 4)) if forge_outs else 0
        alch_n = min(6, max(3, len(consumables_all) // 3)) if consumables_all else 0
        gate_cap = target + forge_n + alch_n
        if getattr(preset, "buildings_enabled", True) and len(world.recipes) < gate_cap:
            for out_it in (rng.sample(forge_outs, min(forge_n, len(forge_outs))) if forge_outs else []):
                if len(world.recipes) >= gate_cap:
                    break
                ins = rng.sample(forge_mats + craft_mats, min(2, len(forge_mats + craft_mats)))
                if not ins:
                    break
                out_lvl = min(max_lvl, {"common": 1, "uncommon": 2, "rare": 3,
                                    "epic": 4, "legendary": 5, "mythic": 6}.get(out_it.rarity, 1)) if max_lvl > 0 else 0
                min_bld = max(1, out_lvl)  # 高 level 产出需同 level 建筑
                _add(ins, out_it, required_building="forge",
                     min_building_level=min_bld, output_level=out_lvl)
            # alchemy 配方：consumable 产出，需 alchemy 建筑
            for out_it in (rng.sample(consumables_all, min(alch_n, len(consumables_all))) if consumables_all else []):
                if len(world.recipes) >= gate_cap:
                    break
                ins = rng.sample(craft_mats + forge_mats, min(2, len(craft_mats + forge_mats)))
                if not ins:
                    break
                _add(ins, out_it, required_building="alchemy", min_building_level=1, output_level=0)

    # ---- [住宅网格 2026-08-24] 建筑升级材料需求（LLM 生成 + 代码回退）----
    _BMAT_SYSTEM_PROMPT = (
        "你是一个 SLG 游戏的建筑升级材料规划引擎。我会给你世界题材、建筑种类、各等级和现有材料名清单。\n"
        "为每种建筑（forge/alchemy/refine/study/garden/warehouse）的每个等级（1 起）规划升级所需材料。\n"
        "要求：1. 材料名只能从我给出的现有材料名中挑选，不要编造；2. 每级 2-3 个材料；"
        "3. 等级越高选越稀有的材料（common<uncommon<rare<epic<legendary<mythic）；"
        "4. 选材尽量贴合建筑用途（锻造屋偏矿石/锭材，炼丹房偏草药/灵材，仓库偏木料/布料/石材等建材）。\n"
        '只输出 JSON：{"建筑kind": {"等级": ["材料名", ...], ...}, ...}，不要任何其他文字。'
    )

    def _generate_building_material_specs(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[住宅网格] 生成建筑升级材料需求存 world.building_material_specs。

        LLM 成功：{kind: {level: [{item_id,name,rarity}]}}（具体材料，贴合建筑）。
        LLM 失败/无 API：置 {"_enabled":1} 标记 -> home_engine._spec_for 走品级回退
        （{rarity,count}，玩家任意同品级材料可满足）。老档 specs 为空 -> 旧 1forge+1craft。
        数值范式：LLM 只出「材料名清单」语义，扣料结算纯引擎（home_engine）。"""
        if not getattr(preset, "buildings_enabled", True):
            return
        max_lvl = max(1, int(getattr(preset, "buildings_max_level", 5) or 5))
        kinds = ["forge", "alchemy", "refine", "study", "garden", "warehouse"]
        mats = [it for it in (getattr(world, "items", None) or [])
                if getattr(it, "type", "") == "material"]
        if not mats:
            world.building_material_specs = {"_enabled": 1}
            return
        parsed: dict = {}
        # [!] 测试/无 storage 场景 self.storage 可能缺失或 None（_resolve_api 会解引用
        # storage），此时跳过 LLM 直接走品级回退，保证离线生成不崩。
        if getattr(self, "storage", None) is not None:
            api = self._resolve_api(getattr(preset, "calculator_api_id", "")
                                    or getattr(preset, "narrative_api_id", ""))
            if api is not None:
                parsed = self._bmat_llm(world, preset, kinds, max_lvl, mats)
        world.building_material_specs = parsed if parsed else {"_enabled": 1}

    def _bmat_llm(self, world: World, preset: WorldSimPreset, kinds: list,
                  max_lvl: int, mats: list) -> dict:
        """调 LLM 出建筑升级材料名清单并解析成 item_id（失败返回 {}）。"""
        api = self._resolve_api(getattr(preset, "calculator_api_id", "")
                                or getattr(preset, "narrative_api_id", ""))
        if api is None:
            return {}
        tmp_preset = Preset(
            name="world_sim_building_materials",
            system_prompt=self._BMAT_SYSTEM_PROMPT,
            temperature=getattr(preset, "calculator_temperature", 0.3),
            max_tokens=max(8000, int(getattr(preset, "calculator_max_tokens", 4000) or 4000)),
            top_p=getattr(preset, "calculator_top_p", 0.9),
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        mat_desc = "、".join(f"{it.name}({getattr(it, 'rarity', 'common')})" for it in mats[:80])
        user = (f"世界题材：{', '.join(getattr(world, 'genre_tags', []) or []) or '未知'}\n"
                f"建筑种类：{'、'.join(kinds)}；等级 1-{max_lvl}\n"
                f"现有材料（只能引用这些名字）：{mat_desc}\n"
                f"请为每种建筑每个等级规划 2-3 个升级材料。")
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user}]
        result = llm.chat(messages)
        if result.error or not (result.content or "").strip():
            return {}
        data = _extract_json(result.content)
        if not isinstance(data, dict):
            return {}
        item_by_name = {it.name: it for it in mats}
        out: dict = {}
        for kind in kinds:
            kblock = data.get(kind)
            if not isinstance(kblock, dict):
                continue
            out[kind] = {}
            for lvl in range(1, max_lvl + 1):
                names = kblock.get(str(lvl)) or kblock.get(lvl) or []
                ents = []
                for nm in names:
                    it = item_by_name.get(str(nm).strip())
                    if it is not None and it.id not in [e["item_id"] for e in ents]:
                        ents.append({"item_id": it.id, "name": it.name,
                                     "rarity": getattr(it, "rarity", "common")})
                if ents:
                    out[kind][str(lvl)] = ents
        return out

    def _ensure_equipment_coverage(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[P7d2] 确保每个装备槽（8 槽）都有至少 1 件装备可填（覆盖装备栏）。

        检查 head/chest/legs/feet/main_hand/off_hand/accessory1/accessory2 各槽：
        若该槽无装备（slot 字段匹配或同 type 无 slot），补 1 件 common 兜底装备（题材化命名）。
        补的装备走 _init_combat_stats 数值推算 + 题材化 stat_bonus（_GENRE_EQUIPMENT_BONUS）。
        """
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        names = _GENRE_EQUIPMENT_NAMES.get(tid, _GENRE_EQUIPMENT_NAMES["western_fantasy"])
        for slot, itype in _SLOT_TO_TYPE.items():
            # 该槽已有装备？（slot 精确匹配，或同 type 且 slot 为空可填任意槽）
            has = any(it.slot == slot for it in world.items) or \
                  any(it.type == itype and not it.slot for it in world.items)
            if not has:
                variants = names.get(slot) or [f"普通{itype}"]
                rng = SeededRng.seed_from(world.id, 0, f"equip_cov_{slot}")
                nm = rng.pick(variants) or variants[0]
                world.items.append(Item(
                    name=nm, type=itype, rarity="common", slot=slot,
                    desc=f"一件寻常的{nm}，聊胜于无。"))

    def _level_up_recalc(self, world: World):
        """[遗留修复 2026-08-29] 升级重算：hp_max 回满 + mp_max 同步重算回满。

        原 mp_max 只在开局算一次（stat_int x5 + level x3 + 天赋 mp_bonus），升级时
        只回 HP 不动 MP——玩家法力上限被冻结在开局等级，等级成长与 mp_bonus 天赋
        （雷天灵根/混沌道体）全程不生效。各升级点统一走本 helper。
        [!] 玩家专用公式（level x3）；NPC 段是 stat_int x5 + level x2
        （_init_combat_stats NPC 段口径）勿混用。
        """
        p = world.player
        p.hp_max = ce.max_hp_for(p)
        p.hp = p.hp_max
        p.mp_max = max(0, te.effective_stat(p, "int") * 5 + int(p.level) * 3
                       + int(te.get_talent_bonus(p, "mp_bonus") or 0))
        p.mp = p.mp_max

    def _equip_initial_gear(self, world: World, npc):
        """[A1 2026-08-29] NPC 初始装备：战斗/治安 NPC（combat_role in boss/hostile/
        friendly 且 level>0）按等级从 world.items 装备池确定性配 main_hand + chest
        （boss 加 accessory1）。已有装备不覆盖（幂等）。

        装备与玩家共用蓝图 item id（Item 是模板目录，多实体同 id 引用安全——
        equipped 只存引用，死亡掉落走 loot_table 不动装备）。等级门槛 item.level <= 
        NPC 等级 +2；每槽按攻防总分降序取 top3 rng 挑一（SeededRng 锚 npc.id，确定性）。
        """
        if getattr(npc, "combat_role", "") not in ("boss", "hostile", "friendly"):
            return
        lvl = int(getattr(npc, "level", 0) or 0)
        if lvl <= 0 or (getattr(npc, "equipped", None) or {}):
            return
        pool = [it for it in world.items
                if getattr(it, "type", "") in ("weapon", "armor", "accessory")
                and getattr(it, "slot", "")
                and int(getattr(it, "level", 0) or 0) <= lvl + 2
                and _npc_start_item_ok(it)]   # [用户指示 2026-09-06] 初始装备封顶紫色

        # [审查统一] 强度口径与日换装一致（nle._gear_score：攻防+词缀+属性加成）
        _score = nle._gear_score
        rng = SeededRng.seed_from(world.id, 0, f"npc_gear_{npc.id}")
        slots = ["main_hand", "chest"] + (["accessory1"] if npc.combat_role == "boss" else [])
        for slot in slots:
            cands = sorted((it for it in pool if it.slot == slot),
                           key=lambda x: (-_score(x), x.id))
            if not cands:
                continue
            pick = rng.pick(cands[:3]) if len(cands) > 1 else cands[0]
            npc.equipped[slot] = pick.id

    @staticmethod
    def _durability_mult(it) -> float:
        """[P7k3] 装备耐久比例（0.0-1.0）：durability/durability_max。

        [P45 2026-09-12] 委托 combat_engine.durability_mult 纯函数（单一来源：玩家/NPC
        战斗、NPC 打猎、UI 共用）；本方法保留为兼容壳（既有直调点/测试不变）。
        """
        return ce.durability_mult(it)

    @staticmethod
    def _scale_affix(affix: dict, mult: float) -> dict:
        """[P7k3] 按耐久比例缩放 affix 数值字段（atk/def/magic_atk/crit）。满耐久原样返回。"""
        if mult >= 1.0 or not isinstance(affix, dict):
            return affix
        scaled = dict(affix)
        for k in ("atk", "def", "magic_atk"):
            if k in scaled:
                try:
                    scaled[k] = int(int(scaled[k]) * mult)
                except (TypeError, ValueError):
                    pass
        if "crit" in scaled:
            try:
                scaled["crit"] = float(scaled["crit"]) * mult
            except (TypeError, ValueError):
                pass
        _sb = scaled.get("stat_bonus")
        if isinstance(_sb, dict):
            scaled["stat_bonus"] = {k: max(0, int(v * mult)) for k, v in _sb.items()
                                    if isinstance(v, (int, float))}
        mods = scaled.get("mods")
        if isinstance(mods, dict):
            new_mods = dict(mods)
            for k in ("atk", "def", "magic_atk"):
                if k in new_mods:
                    try:
                        new_mods[k] = int(int(new_mods[k]) * mult)
                    except (TypeError, ValueError):
                        pass
            if "crit" in new_mods:
                try:
                    new_mods["crit"] = float(new_mods["crit"]) * mult
                except (TypeError, ValueError):
                    pass
            _msb = new_mods.get("stat_bonus")
            if isinstance(_msb, dict):
                new_mods["stat_bonus"] = {k: max(0, int(v * mult)) for k, v in _msb.items()
                                          if isinstance(v, (int, float))}
            scaled["mods"] = new_mods
        return scaled

    def _collect_equipped_affixes(self, world: World, entity) -> list[dict]:
        """聚合 entity 已装备物品的 affixes 列表。

        [P5c] 8 槽天然兼容：equipped dict 的 key 是 8 槽 id（head/chest/...），
        遍历全 key 即可，不再硬编码 weapon/armor/accessory 三槽。
        [P7k3] affix 数值按装备耐久比例缩放（耐久低则词缀衰减）。
        """
        affixes = []
        equipped = getattr(entity, "equipped", None) or {}
        if not isinstance(equipped, dict):
            return affixes
        for slot, iid in equipped.items():
            it = next((i for i in world.items if i.id == iid), None)
            if it and it.affixes:
                mult = self._durability_mult(it)
                for a in it.affixes:
                    if isinstance(a, dict):
                        affixes.append(self._scale_affix(a, mult))
        return affixes

    def _equipped_attack_defense(self, world: World, entity) -> tuple[int, int]:
        """取 entity 已装备的武器 attack + 防具 defense 总和（[P7k3] 按耐久比例生效）。

        [P5c] 8 槽天然兼容：遍历 equipped dict 全部 key，按 Item.type 分流。
        - 多把 weapon（main_hand + off_hand）的 attack 自动累加（各按耐久比例）
        - 多件 armor（head/chest/legs/feet）的 defense 自动累加（各按耐久比例）
        - accessory 走 _collect_equipped_affixes（这里不计）
        """
        # [P45 2026-09-12] 委托 combat_engine.equipped_attack_defense 纯函数（单一来源：
        # 玩家/NPC 战斗与 NPC 打猎胜率共用；本方法保留为兼容壳，行为不变）。
        return ce.equipped_attack_defense(world, entity)

    def _equipped_stat_bonus(self, world: World, entity) -> dict:
        """[C1 修复 2026-08-25] 汇总已装备物品顶层 stat_bonus（此前只显示不生效）。

        [修 2026-09-06] 委托 combat_engine.equipped_stat_bonus 纯函数（战斗侧与
        UI 显示共用单一来源，防口径分裂）。
        """
        return ce.equipped_stat_bonus(world, entity)

    def _degrade_equipment(self, world: World, granularity: str):
        """[P7k3] 战斗后装备耐久损耗（仅 weapon/armor，按粒度）。

        light=损耗1/medium=2/heavy=3。durability_max<=0（不朽）不损耗。耐久到 0 装备失效。
        """
        decay = {"light": 1, "medium": 2, "heavy": 3}.get(granularity, 2)
        if decay <= 0:
            return
        equipped = getattr(world.player, "equipped", None) or {}
        if not isinstance(equipped, dict):
            return
        for slot, iid in equipped.items():
            it = next((i for i in world.items if i.id == iid), None)
            if it is None or it.type not in ("weapon", "armor"):
                continue
            # [!] 不用 `or 100`：合法 durability_max=0（不朽）会被 falsy 吞成 100（守 §11）
            try:
                dm = int(getattr(it, "durability_max", 100))
            except (TypeError, ValueError):
                dm = 100
            if dm <= 0:
                continue  # 不朽
            try:
                cur = int(getattr(it, "durability", dm))
            except (TypeError, ValueError):
                cur = dm
            it.durability = max(0, cur - decay)

    def _tick_time_phase(self, world: World, tick: int):
        """[P7k5] 日夜天气推进（确定性，由 tick 推导相位 + 周期性天气变化）。

        time_phase 由 tick//3 推导（每 3 回合一相位，4 相位=1 天）；day_count = tick//12+1。
        天气每 5 tick 据 SeededRng 可能变化（40% 概率换天气）；[P24b] 换天气从
        cale.seasonal_weather_pool 季节加权池抽（春多雨/夏多晴雷/秋多云雾/冬多风暴）。
        纯推进无 LLM。
        """
        phases = ["dawn", "day", "dusk", "night"]
        try:
            world.time_phase = phases[(tick // 3) % 4]
        except Exception:
            pass
        try:
            world.day_count = tick // 12 + 1
        except Exception:
            pass
        if tick % 5 == 0:
            try:
                rng = SeededRng.seed_from(world.id, tick, "weather")
                if rng.chance(0.4):
                    world.weather = rng.pick(cale.seasonal_weather_pool(world)) or "clear"
            except Exception:
                pass

    def _tick_friend_chats(self, world: World, preset: WorldSimPreset, tick: int,
                           cancel_check, budget_avail: bool = True) -> list:
        """[A3 2026-08-28] 好友主动私聊 LLM 阶段：消费 tick_friends 标记的 pending 队列。

        每次至多生成 1 条（防刷屏/控预算）：复用 npc_private_chat 的人设上下文构建，
        但要求 NPC 主动发消息（无玩家上句）；生成结果 append 进私聊记录
        （assistant 消息，玩家在私聊界面可见可回），minor 事件播报预览。
        预算不足/无 API/失败：保留 pending 下轮重试（不丢不漏）。
        """
        events: list[WorldEvent] = []
        pending = [x for x in (getattr(world, "pending_friend_chats", None) or [])
                   if isinstance(x, str) and x]
        if not pending:
            return events
        if not budget_avail or (cancel_check and cancel_check()):
            return events                      # 预算/取消：pending 保留重试
        api = self._resolve_api((preset.narrative_api_id or preset.calculator_api_id) if preset else "")
        if api is None or self.storage is None:
            world.pending_friend_chats = pending[:0] if api is None and self.storage is None else pending
            if api is None:
                # 无 API：丢弃防死循环堆积（与股市无 API 口径不同——私聊非关键链路）
                world.pending_friend_chats = []
            return events
        npc_id = pending[0]
        npc = next((n for n in (getattr(world, "npcs", None) or []) if n.id == npc_id), None)
        if npc is None or not getattr(npc, "alive", True):
            world.pending_friend_chats = pending[1:]
            return events
        # 上下文复用 npc_private_chat（history=None -> 无玩家上句，主动开场）
        sys_prompt = self._build_proactive_chat_prompt(world, npc, preset)
        tmp_preset = Preset(
            name="world_sim_friend_chat",
            system_prompt=sys_prompt,
            temperature=(preset.narrative_temperature if preset else 0.8),
            max_tokens=max(4000, int(preset.narrative_max_tokens if preset else 4000) or 4000),
            top_p=(preset.narrative_top_p if preset else 0.95),
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content":
                     f"（{npc.name}主动给玩家发来一条私聊消息，请以其口吻写出这条开场消息）"}]
        result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                     block_labels=["系统", "用户"])
        if result.cancelled:
            return events                      # 取消：pending 保留
        if result.error or not (result.content or "").strip():
            # 失败：丢弃本条防死循环（保留其余 pending）；下轮 roll 可能再触发
            world.pending_friend_chats = pending[1:]
            return events
        text = result.content.strip()
        # 落私聊记录（玩家在私聊界面可见可回）
        try:
            hist = self.storage.load_npc_chat(world.id, npc.id) or []
            hist.append({"role": "assistant", "content": text,
                         "read": False})   # [记忆化私聊] 未读标记（开窗显示后清）
            self.storage.save_npc_chat(world.id, npc.id, hist)
        except Exception:
            pass                                # best-effort：记录失败仍播事件
        world.pending_friend_chats = pending[1:]
        events.append(WorldEvent(
            tick=tick, category="npc", severity="minor",
            title=f"{npc.name}发来私聊",
            desc=f"{npc.name}给你发来一条私聊消息：「{text[:60]}」…"
                 "（好友页打开私聊可回复）",
            npcs=[npc.id]))
        return events

    def _build_proactive_chat_prompt(self, world: World, npc, preset) -> str:
        """[A3] 主动私聊 system：复用 npc_private_chat 的人设口径（档案/交情/腔调/记忆/
        世界观/文风），但要求主动开场而非回复。"""
        aff = int(getattr(npc, "affinity", 0) or 0)
        rel = nre.affinity_level(aff)
        mem_text = ""
        if preset is not None and getattr(preset, "npc_memory_enabled", True):
            try:
                mem_text = self.npc_memory().recall(npc, world, npc.name, preset)
            except Exception:
                mem_text = ""
        _uid = self._fmt_bound_user(world)
        return (
            f"你是 SLG 游戏世界里的 NPC「{npc.name}」，主动给你的好友（玩家）发一条私聊消息。\n"
            f"身份：{npc.role or '未知'}；性格：{npc.personality or '未知'}；目标：{npc.goal or '未知'}。\n"
            + (f"说话风格（泛义参考：学其整体语气与用词倾向，不要逐字复读，"
               f"也不要每轮用相同句式开场）：{npc.speech_style}\n"
               if getattr(npc, "speech_style", "") else "")
            + (f"背景小传：{npc.notes}\n" if getattr(npc, "notes", "") else "")
            + f"你与玩家的交情：{rel}（{aff}/100），请保持这个亲疏程度与性格口吻。\n"
            + (f"玩家身份：{_uid}。按姓名称呼玩家。\n" if _uid else "")
            + (f"你对玩家的记忆：\n{mem_text}\n" if mem_text else "")
            + f"世界观背景：{world.premise}\n"
            + ((f"\n【文风偏好】\n{preset.narrative_style_prompt.strip()}\n")
               if preset is not None and getattr(preset, "narrative_style_prompt", "").strip() else "")
            + "\n要求：以第一人称写一条 1-3 句的开场私聊——像是你想起了玩家、"
            "有事相告/闲聊/关心近况，贴合你的性格与近期处境；不要 JSON、不要选项、"
            "不要旁白动作以外的格式。"
        )

    def _check_companion_interject(self, world: World, scene: SceneLog,
                                   intent: Optional[dict],
                                   preset: Optional[WorldSimPreset] = None) -> str:
        """[P32] 同伴插话门控检定（纯 Python + SeededRng；引擎算"何时说"，LLM 写"说什么"）。

        门控链路：
        1. 开关 preset.companion_interject_enabled + 同伴在场（companion_npc_ids 非空且同伴在当前地点/场所）
        2. intent_type 门控：combat/adventure 高触发率、quest/talk 中、use_item/gather 低、move/observe 几乎不
        3. 频率水位线：每同伴 last_interject_tick，间隔不够（preset.companion_interject_interval）不再触发
        4. 好感极值加成：affinity>=80（强烈认同）或<=20 且非敌对（强烈反对）触发率提升
        5. SeededRng 检定通过 -> 返回【同伴反应】块文本注入 narrate user 消息（同伴须显式署名，
           避免 talk_to 无主语引号误归因）；不通过返回 ""（LLM 不知道有同伴可说话，不会自作主张发言）
        [!] 检定通过才更新 last_interject_tick（落盘随 NPC JSON）；不通过不改。
        """
        try:
            if not self._per_world(world, "companion_interject_enabled", True, preset):
                return ""
            comp_ids = list(getattr(world.player, "companion_npc_ids", None) or [])
            if not comp_ids:
                return ""
            # 同伴在场：当前地点/场所的存活非敌对同伴
            loc = self._current_location(world)
            if loc is None:
                return ""
            place = self._current_place(world)
            present = [n for n in self._npcs_at_place(world, loc, place)
                       if n.id in comp_ids and getattr(n, "alive", True)
                       and not getattr(n, "hostile", False)]
            if not present:
                return ""
            # intent_type 门控基础触发率
            itype = str((intent or {}).get("intent_type", "custom") or "custom")
            base_rate = {
                "combat": 0.45, "adventure": 0.40,
                "quest": 0.25, "talk": 0.25, "trade": 0.20,
                "use_item": 0.12, "gather": 0.12, "craft": 0.12,
                "move": 0.05, "observe": 0.05,
            }.get(itype, 0.10)
            interval = max(1, int(self._per_world(world, "companion_interject_interval", 5, preset)))
            tick = int(getattr(world, "tick_count", 0) or 0)
            rng = SeededRng.seed_from(world.id, tick, "companion_interject")
            # 遍历在场同伴，取第一个通过门控 + 检定的
            for npc in present:
                # 频率水位线
                last = int(getattr(npc, "last_interject_tick", 0) or 0)
                if last > 0 and (tick - last) < interval:
                    continue
                # 好感极值加成
                aff = int(getattr(npc, "affinity", 0) or 0)
                rate = base_rate
                if aff >= 80 or (aff <= 20 and aff >= 0):
                    rate = min(0.85, rate + 0.20)
                if rng.chance(rate):
                    # 检定通过 -> 更新水位线 + 构造【同伴反应】块
                    npc.last_interject_tick = tick
                    tendency = ("认同" if aff >= 60 else "反对" if aff <= 30 else "中立")
                    style = str(getattr(npc, "speech_style", "") or "").strip()
                    seg = f"【同伴反应】本回合同伴「{npc.name}」有话要说（好感倾向：{tendency}"
                    if style:
                        seg += f"；说话风格（泛义参考）：{style}"
                    seg += "）。可在旁白中让其发表一句简短插话，须显式署名（如「{name}插话道：「…」」）。".format(name=npc.name)
                    return seg
            return ""
        except Exception:
            return ""

    # ---- [⑦ 2026-08-30] 旁听 NPC-NPC 对话（概率注入，零新增 LLM 调用）----
    def _shared_rumor_topic(self, world: World, a, b) -> str:
        """两人都「知道」的一条传闻文本（供旁听话题）。"""
        for r in (getattr(world, "shared_rumors", None) or []):
            if rme.knows(a, r) and rme.knows(b, r):
                return str(r.get("text", "") or r.get("title", "") or "")
        return ""

    # ---------- [旁听对话 2026-09-06 用户指示] NPC-NPC 完整对话（档案制）----------
    _NPC_DIALOGUE_SYSTEM_PROMPT = (
        "你是一个 RPG 世界的 NPC 对话生成器。给你两个 NPC 的档案、他们之间的关系和一个"
        "共同话题，写一段他们之间的自然对话（4-6 轮交替，每轮一两句话）。\n"
        "只输出 JSON：{\"turns\":[{\"who\":\"a或b\",\"text\":\"台词\"}]}，"
        "不要任何其他文字。台词要贴各自的性格和说话习惯，像真人闲聊，"
        "可以聊到话题但不必刻意总结。"
    )

    def _gen_npc_dialogue(self, world: World, a, b, topic: str, soc: int,
                          preset: Optional[WorldSimPreset] = None,
                          cancel_check=None) -> Optional[list]:
        """[旁听对话] 生成双人完整对话（1 次 LLM）。失败返回 None（调用方回退旁白模式）。"""
        api = self._resolve_api(
            self._per_world(world, "narrative_api_id", preset.narrative_api_id, preset)
            or self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id) if preset else self._resolve_api(None)
        if not api:
            return None
        from src.models.preset import Preset as _Pr
        tmp = _Pr(name="npc_dialogue",
                  system_prompt=self._NPC_DIALOGUE_SYSTEM_PROMPT,
                  # [修 2026-09-10] preset 为 Optional：原写法直接解引用 preset.narrative_max_tokens，
                  # preset=None（上方分支明确允许）时会 AttributeError 崩。
                  temperature=0.8,
                  max_tokens=max(4000, int((preset.narrative_max_tokens if preset else 4000) or 4000)),
                  top_p=0.95)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())
        rel = "相熟" if soc >= 0 else "不和"
        user = (
            f"世界基调：{world.tone or ''}；{self._fmt_world_lore(world)[:200]}\n"
            f"NPC A：{a.name}（{a.role}）性格：{a.personality}；说话风格（泛义，勿复读同一句）：{a.speech_style or '自然'}；"
            f"近期念头：{getattr(a, 'current_thought', '') or ''}\n"
            f"NPC B：{b.name}（{b.role}）性格：{b.personality}；说话风格（泛义，勿复读同一句）：{b.speech_style or '自然'}；"
            f"近期念头：{getattr(b, 'current_thought', '') or ''}\n"
            f"两人关系：{rel}（社交值 {soc}）；共同话题：{topic}\n"
            f"请写他们的对话（who 用 a/b 指代对应 NPC）。"
        )
        try:
            r = llm.chat_cancelable([
                {"role": "system", "content": tmp.system_prompt},
                {"role": "user", "content": user},
            ], cancel_check=cancel_check, block_labels=["系统", "用户"])
        except Exception:
            return None
        if getattr(r, "cancelled", False) or getattr(r, "error", "") or not (getattr(r, "content", "") or "").strip():
            return None
        data = _extract_json(r.content)
        turns = data.get("turns") if isinstance(data, dict) else None
        if not isinstance(turns, list):
            return None
        out = []
        for t in turns:
            if not isinstance(t, dict):
                continue
            who = str(t.get("who", "")).strip().lower()
            txt = str(t.get("text", "") or "").strip()
            if who in ("a", "b") and txt:
                out.append({"who": who, "text": txt[:300]})
        return out or None

    def _store_npc_dialogue(self, world: World, a, b, turns: list) -> None:
        """[旁听对话] 存对话档案（world.npc_dialogues 滚动 cap 40）+ 未总结超阈值触发压缩。"""
        tick = int(getattr(world, "tick_count", 0) or 0)
        world.npc_dialogues.append({
            "a_id": str(a.id), "b_id": str(b.id), "a_name": a.name, "b_name": b.name,
            "tick": tick, "day": max(1, int(getattr(world, "day_count", 1) or 1)),
            "turns": turns, "summary": "", "summarized": False,
        })
        if len(world.npc_dialogues) > 40:
            del world.npc_dialogues[:len(world.npc_dialogues) - 40]

    def _compress_npc_dialogues(self, world: World, preset=None, cancel_check=None) -> bool:
        """[旁听对话] 压缩：未总结条数 > 10 时把最老的 5 条按对话对合并成一段 summary
        （1 次 LLM），删掉原文（summarized=True 保 summary）。失败保留原文下次再试。
        挂生成后的内联检查（频率 = 旁听命中频率 / 5，token 可预期）。"""
        pending = [d for d in (getattr(world, "npc_dialogues", None) or [])
                   if isinstance(d, dict) and not d.get("summarized")]
        if len(pending) <= 10:
            return False
        batch = pending[:5]
        api = self._resolve_api(
            self._per_world(world, "narrative_api_id", preset.narrative_api_id, preset)
            or preset.calculator_api_id) if preset else self._resolve_api(None)
        if not api:
            return False
        from src.models.preset import Preset as _Pr2
        tmp = _Pr2(name="npc_dialogue_sum",
                   system_prompt="你是一个 RPG 世界的史官。把给你的几段 NPC 对话压缩成一段简短纪要"
                                 "（谁和谁、聊了什么、有什么值得记住的），不超过 120 字。只输出纪要正文。",
                   # [修 2026-09-10] 原 1000 是全项目唯一无条件低于「世界模拟所有 LLM 参数
                   # >= 10000」的调用（裸 Preset 绕过 from_dict 钳制）：reasoning 模型思考可
                   # 吃满 1000 -> content 空 -> 本函数返回 False，NPC 对话压缩静默永不成功。
                   temperature=0.4, max_tokens=10000, top_p=0.9)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())
        parts = []
        for d in batch:
            conv = "／".join(f"{(d['a_name'] if t['who'] == 'a' else d['b_name'])}：{t['text']}"
                            for t in (d.get("turns") or [])[:6])
            parts.append(f"【{d['a_name']}与{d['b_name']}（第{d.get('day', '?')}天）】{conv}")
        try:
            r = llm.chat_cancelable([
                {"role": "system", "content": tmp.system_prompt},
                {"role": "user", "content": "\n\n".join(parts)},
            ], cancel_check=cancel_check, block_labels=["系统", "用户"])
        except Exception:
            return False
        txt = (getattr(r, "content", "") or "").strip()
        if getattr(r, "cancelled", False) or getattr(r, "error", "") or not txt:
            return False
        for d in batch:
            d["summarized"] = True
            d["turns"] = []
            d["summary"] = txt[:400]
        return True

    def _check_overheard(self, world: World, scene: SceneLog,
                         preset: Optional[WorldSimPreset] = None) -> str:
        """[⑦] 旁听门控检定（纯 Python + SeededRng；引擎算"何时/谁"，LLM 写"说什么"）。

        门控链路：
        1. 开关 preset.overheard_enabled + 冷却水位（last_overheard_tick >= interval 才再触发）
        2. SeededRng 概率检定（overheard_chance）
        3. 挑两个在场存活非敌对 NPC：有社交链（abs(social)>=25）且同知一条市井传闻
        4. 命中 -> 返回【旁听】块注入 narrate user 消息（LLM 写两人对话）；不命中返回 ""
        [!] 复用 narrate LLM 写对话，零新增调用；话题来自 ④ 的传闻池（无传闻则自然不触发）。
        """
        try:
            if not self._per_world(world, "overheard_enabled", True, preset):
                return ""
            tick = int(getattr(world, "tick_count", 0) or 0)
            interval = max(1, int(self._per_world(world, "overheard_cooldown_tick", 6, preset)))
            last = int(getattr(world, "last_overheard_tick", 0) or 0)
            if last > 0 and (tick - last) < interval:
                return ""
            rng = SeededRng.seed_from(world.id, tick, "overheard_npc_chat")
            if not rng.chance(float(self._per_world(world, "overheard_chance", 0.20, preset))):
                return ""
            loc = self._current_location(world)
            if loc is None:
                return ""
            place = self._current_place(world)
            present = [n for n in self._npcs_at_place(world, loc, place)
                       if getattr(n, "alive", True) and not getattr(n, "hostile", False)]
            if len(present) < 2:
                return ""
            for i in range(len(present)):
                a = present[i]
                for b in present[i + 1:]:
                    # 社交底账双向对称，但读双向防脏数据（引擎写对称，旧档/桩可能单边；social 可能为 None）
                    soc = int((getattr(a, "social", None) or {}).get(str(b.id), 0)
                              or (getattr(b, "social", None) or {}).get(str(a.id), 0) or 0)
                    if abs(soc) < 25:
                        continue
                    topic = self._shared_rumor_topic(world, a, b)
                    if not topic:
                        continue
                    world.last_overheard_tick = tick
                    rel = "相熟" if soc >= 0 else "不和"
                    # [旁听对话 2026-09-06 用户指示] 升级：生成完整对话存 NPC 档案，
                    # 主页面只留一行轻提示（不渲染对话内容）；失败/关闭回退旁白两句模式。
                    if bool(self._per_world(world, "overheard_dialog_enabled", True, preset)):
                        try:
                            turns = self._gen_npc_dialogue(world, a, b, topic, soc, preset)
                        except Exception:
                            turns = None          # 桩/异常环境防御：回退旁白模式
                        if turns:
                            self._store_npc_dialogue(world, a, b, turns)
                            try:
                                self._compress_npc_dialogues(world, preset)
                            except Exception:
                                pass
                            return (
                                f"【旁听】{a.name} 与 {b.name} 正在一旁低声交谈"
                                f"（两人关系{rel}，话题隐约和他们最近关心的事有关——"
                                f"完整对话可在两人的档案里查看）。旁白轻带一笔即可，"
                                f"不要复述对话内容，也不要改变两人的位置。"
                            )
                    return (
                        f"【旁听】本回合在场 NPC「{a.name}」与「{b.name}」在闲聊"
                        f"（两人关系：{rel}），话题是：{topic}。请在旁白中写一段玩家"
                        f"正好听到的对话，各带口吻，点到即止、不喧宾夺主，也不要改变两人的位置。"
                    )
            return ""
        except Exception:
            return ""

    def _build_narrate_messages(
            self, world: World, scene: SceneLog, player_action: Optional[str],
            intent: Optional[dict], preset: WorldSimPreset, is_intro: bool = False,
    ) -> list[dict]:
        """构造叙事 LLM 的 messages。"""
        # [P15a2] 叙事维持 full 上下文（装备/背包/属性特长/世界书全文都保留，供 immersive 描写）
        ctx = self._build_scene_context(world, scene, preset=preset, mode="full")
        if is_intro:
            user = (
                ctx
                + "\n\n这是场景的开端：玩家刚到达当前地点，尚未行动。"
                + "请生成开场旁白：描写玩家初到此地的所见所闻、环境氛围、可能引起注意的人或物，"
                + "为后续探索埋下钩子。不要替玩家做决定或发言。"
            )
        else:
            action = (player_action or "").strip()
            hint = intent.get("narration_hint", "") if intent else ""
            effects = intent.get("effects", []) if intent else []
            resolved = intent.get("resolved", True) if intent else True
            reason = intent.get("reason", "") if intent else ""
            parts = [ctx, f"玩家本回合的行动：{action}"]
            if hint:
                parts.append(f"结算要点：{hint}")
            if effects:
                parts.append("结构化后果：" + "；".join(effects))
            if not resolved and reason:
                # [修 2026-09-05] rstrip 句号防「。。」叠加（reason 常自带句尾标点）
                parts.append(f"行动受阻：{reason.rstrip('。')}。请据此描写受阻的情境。")
            # [P32] 同伴插话：引擎门控检定通过时注入【同伴反应】块（LLM 写"说什么"）
            interject = self._check_companion_interject(world, scene, intent, preset)
            if interject:
                parts.append(interject)
            # [⑦ 2026-08-30] 旁听：引擎门控通过时注入【旁听】块（LLM 写"两人聊什么"）
            overheard = self._check_overheard(world, scene, preset)
            if overheard:
                parts.append(overheard)
            parts.append("请展开成沉浸旁白。")
            user = "\n\n".join(parts)
        # [场景事件生图] 双闸开着才教 LLM 输出 [img:...]（前要求块+末强制行）；
        # 与 process_scene_images 同一双闸（narrative_image_event × image_enabled），
        # 总开关关时不逼 LLM 白写标签。开场/回合旁白共用本 system，一次注入两处生效。
        # [修 2026-10-01 文风层死值] base 从规则层改为 narrative_system_full()：
        # 原实现只发规则层，【文风偏好】层从未进过 messages（narrate_outcome/intro 里
        # tmp_preset.system_prompt=narrative_system_full() 是无人读的死值——LlmClient
        # 不读 preset.system_prompt，只透传 messages）。
        sys_prompt = preset.narrative_system_full()
        if preset.narrative_image_event and preset.image_enabled:
            sys_prompt = WORLDSIM_NARRATIVE_IMG_REQ + sys_prompt + "\n" + WORLDSIM_NARRATIVE_IMG_TAIL
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ]

    # ---------- [P18] 旁白统一管线：首行 NPC 移动命令解析执行 + 剥离 ----------

    def _resolve_dest(self, world: World, dest: str) -> "Optional[Location]":
        """命令行目的地解析。[P44] 收编进统一解析器 nrs.resolve_name（原「精确名
        优先 + 最长真实地名子串」是全链范式，本方法即其出处；叠加归一化/编辑距离
        容错，LLM 常加「门前/郊外」后缀或有一字之差）。"""
        hit = nrs.resolve_name(dest, [l.name for l in world.locations])
        return next((l for l in world.locations if l.name == hit), None) if hit else None

    def _resolve_dest_full(self, world: World, dest: str, cur_loc: Optional[Location]
                           ) -> "tuple[Optional[Location], Optional[Place]]":
        """[P27] 命令行目的地解析（场所感知）：返回 (location, place)。

        dest 格式：
        - 「地点名/场所名」-> 跨地点到目标地点的场所（地点名精确/子串，场所名精确/子串）
        - 「场所名」（无 /）-> 当前地点内场所移动（cur_loc 内解析场所）
        - 「地点名」（无 / 且非场所）-> 现有 _resolve_dest 口径（location, None）
        返回 (None, None) = 无法解析。
        """
        if not dest:
            return None, None
        # 含 / -> 地点/场所
        if "/" in dest:
            loc_name, _, place_name = dest.partition("/")
            loc_name = loc_name.strip()
            place_name = place_name.strip()
            if not loc_name or not place_name:
                return None, None
            loc = self._resolve_dest(world, loc_name)
            if loc is None:
                return None, None
            place = self._resolve_place_by_name(loc, place_name)
            return loc, place  # place 可能 None（地点有但场所名未命中）
        # 无 / -> 先试当前地点场所，再试地点名
        if cur_loc is not None:
            place = self._resolve_place_by_name(cur_loc, dest)
            if place is not None:
                return cur_loc, place
        loc = self._resolve_dest(world, dest)
        return loc, None

    def _apply_npc_cmds(self, world: World, cmds: "list[tuple[str, str]]") -> "list[str]":
        """兑现旁白声明的 NPC 移动：名字须真实存活 NPC，地点/场所经 _resolve_dest_full 解析。

        叙事 LLM 按协议在旁白首行下「[NPC] 名字→地点」或「[NPC] 名字→地点/场所」命令
        （到场写玩家地点、离场写去处），引擎在此同步 location_id + place_id + 两地点/场所
        npc_ids（_move_npc_safe / _move_to_place 口径），数据与叙事强制一致。
        编造名/不可解析地点/死亡 NPC 忽略（宁可不移动也不错移）。
        """
        if not cmds:
            return []
        loc_by_id = {l.id: l for l in world.locations}
        done = []
        # [P44] NPC 名容错解析（候选集锚定存活名单；LLM 偶加「大人/殿下」敬称）。
        # 编造名仍不命中（解析器只认真实名单，守「宁可不移动也不错移」）。
        for name, dest in cmds:
            npc = _npc_by_name(world, name, alive_only=True)
            if npc is None:
                continue
            cur_loc = loc_by_id.get(npc.location_id)
            tgt_loc, tgt_place = self._resolve_dest_full(world, dest, cur_loc)
            if tgt_loc is None:
                continue
            # 场所级移动（同地点 + 有场所目标）
            if tgt_place is not None and tgt_loc.id == npc.location_id:
                if tgt_place.id != (npc.place_id or getattr(tgt_loc, "default_place_id", "") or ""):
                    self._move_to_place(world, npc, tgt_place, tgt_loc)
                    done.append(f"{name}→{tgt_loc.name}/{tgt_place.name}")
                continue
            # 跨地点移动
            if tgt_loc.id != npc.location_id:
                _move_npc_safe(npc, npc.location_id, tgt_loc.id, loc_by_id,
                               to_place_id=getattr(tgt_loc, "default_place_id", "") or "")
                done.append(f"{name}→{tgt_loc.name}")
        if done:
            debug_log(lambda: f"[WorldSim][P18] 旁白NPC移动命令已执行: {'；'.join(done)}")
        return done

    def _sync_undeclared_npcs(self, world: World, body: str,
                              cmds: "Optional[list[tuple[str, str]]]") -> "list[str]":
        """[P22] 旁白漏声明到场兜底：正文把「不在玩家当前地点、且未在命令行声明」的存活
        NPC 写成现场（如低智 LLM 不守协议输出 [NPC] 无/干脆不写命令行），视为漏写到场
        命令，自动同步到玩家当前地点——数据与叙事强制一致的最后一道闸。

        保守过滤（宁可漏同步不错移，与 _apply_npc_cmds 同哲学）：
        - [2026-08-23 三次真人测试] 先抹掉对话/心声/符号引号对内容：名字只出现在引号/
          括号内 = 只是被对话谈论（万宝楼谈铁蛟/病书生竟被拽来 OOC 事故），不作在场依据；
        - 已在玩家当前地点 / 死亡 / 命令行已声明（到场或离场均以声明为准）的 NPC 跳过；
        - 名字出现处 ±6 字内有离屏标记（传闻/口信/不在/离开/想起…）或他处地点名 ->
          该处视为离屏提及，跳过；
        - 名字后紧跟「的」（物性提及，如「柳氏的香囊」不代表在场）跳过。
        全部出现处均为离屏/对话提及才不同步；任一处为在场式提及即同步。
        """
        if not body:
            return []
        player_loc = next((l for l in world.locations if l.id == world.player.location_id), None)
        if player_loc is None:
            return []
        # 扫描前剔除正文中的 [NPC] 命令行（协议外位置的命令行：只算调度声明不算叙事提及，
        # 名字出现在命令行里不代表人物在现场——避免把「[NPC] 柳父→正厅」误判为柳父在场）
        scan_lines = [l for l in body.split("\n") if parse_npc_cmd_line(l) is None]
        scan_text = "\n".join(scan_lines)
        # [2026-08-23] 再抹掉对话/引号/括号区间：只出现在引号内的名字 = 被谈论而非在场。
        # 抹白（保长度）而非删除，保证下方 ±窗口 / 「的」物性判定位置口径不变。
        scan_text = _strip_dialogue_spans(scan_text)
        if not scan_text.strip():
            return []
        loc_names = [l.name for l in world.locations]
        declared = {nm for nm, _ in (cmds or [])}
        loc_by_id = {l.id: l for l in world.locations}
        moved: "list[str]" = []
        for npc in world.npcs:
            # [P44] declared 比对走容错（命令行名字经 _apply_npc_cmds 容错解析兑现，
            # 原文可能带敬称修饰；精确比对会把已声明离场的 NPC 又被兜底同步拽回）
            if not npc.alive or npc.location_id == player_loc.id \
                    or npc.name in declared \
                    or any(nrs.names_match(npc.name, d) for d in declared):
                continue
            name = npc.name
            if not name:
                continue
            idx = 0
            present_style = False
            while True:
                i = scan_text.find(name, idx)
                if i < 0:
                    break
                idx = i + len(name)
                if idx < len(scan_text) and scan_text[idx] == "的":
                    continue  # 物性提及（「柳氏的香囊」）不代表在场
                if _mention_is_off_screen(scan_text, i, name, player_loc.name, loc_names):
                    continue
                present_style = True
                break
            if not present_style:
                continue
            _move_npc_safe(npc, npc.location_id, player_loc.id, loc_by_id,
                           to_place_id=getattr(player_loc, "default_place_id", "") or "")
            moved.append(name)
        if moved:
            debug_log(lambda: f"[WorldSim][P22] 旁白漏声明到场，已兜底同步: {'；'.join(moved)}")
        return moved

    def _finalize_narrative(self, world: World, full: str) -> str:
        """旁白全文收尾：头部空白行后首非空行是 [NPC] 命令行则解析执行并剥离（含前后空白行）。

        非命令行首行（LLM 未遵守协议）原样返回不动世界——协议靠提示词要求，引擎宽容降级。
        与流式过滤器同用 _split_head_cmd 口径，保证落盘正文 = 显示正文。
        [P22] 无论协议是否遵守，收尾都跑一遍漏声明到场兜底（正文提及未声明的离场 NPC
        -> 自动同步），防低智 LLM 不输出命令行时数据/叙事脱钩。兜底只认引号外的纯旁白
        提及——只出现在对话引号内的名字视为被谈论不触发（见 _sync_undeclared_npcs）。
        """
        cmds, body = _split_head_cmd(full)
        if cmds is None:
            body = full
        else:
            self._apply_npc_cmds(world, cmds)
        self._sync_undeclared_npcs(world, body, cmds)
        return body

    def _narrate_stream(self, world: World, llm: LlmClient, api, messages: list[dict],
                        on_chunk: "Optional[Callable[[str], None]]",
                        cancel_check: "Optional[Callable[[], bool]]",
                        err_label: str) -> "tuple[str, str, Optional[LlmUsage]]":
        """叙事流式/非流式统一管线（narrate_outcome/narrate_intro 共用）。

        on_chunk 经 _NpcCmdStreamFilter（首行命令行不进 UI 流）；全文收尾经
        _finalize_narrative（执行移动 + 剥离命令行），返回的 full_text 即纯正文。
        """
        full = ""
        usage: "Optional[LlmUsage]" = None
        flt = _NpcCmdStreamFilter(on_chunk) if on_chunk else None

        def _emit(t: str):
            if flt is not None and not (cancel_check and cancel_check()):
                flt.feed(t)

        if getattr(api, "streaming", True):
            try:
                for event_type, data in llm.chat_stream(
                        messages, cancel_check=cancel_check, block_labels=["系统", "用户"],
                ):
                    if event_type == "text":
                        full += data
                        _emit(data)
                    elif event_type == "usage":
                        usage = data
                    elif event_type == "error":
                        return self._finalize_narrative(world, full), data, usage
                    elif event_type == "cancelled":
                        if flt is not None:
                            flt.flush()
                        return self._finalize_narrative(world, full), "已取消", usage
            except Exception as e:
                debug_log(lambda: f"[WorldSim] {err_label}流式异常: {e}")
                return self._finalize_narrative(world, full), f"{err_label}生成异常：{e}", usage
            if flt is not None:
                flt.flush()
        else:
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return "", "已取消", result.usage
            if result.error:
                return "", result.error, result.usage
            full = result.content
            usage = result.usage
            if flt is not None and full:
                flt.feed(full)
                flt.flush()
        return self._finalize_narrative(world, full), "", usage

    def _narrate_with_retry(
            self, world: World, llm: LlmClient, api, messages: list[dict],
            on_chunk: "Optional[Callable[[str], None]]",
            cancel_check: "Optional[Callable[[], bool]]",
            err_label: str) -> "tuple[str, str, Optional[LlmUsage]]":
        """叙事调用 + 空正文瞬时错误重试一次（settle 已有同款重试，narrate 缺失）。

        [P 验收] 中转站瞬时 502/503 会整回合丢旁白（体验致命）。只要「纯正文为空 + 错误非取消」
        就重试一次；已收到部分正文则不重试（避免重复/串行文）。"""
        full, err, usage = self._narrate_stream(
            world, llm, api, messages, on_chunk, cancel_check, err_label)
        if err and err != "已取消" and not full.strip() and not (cancel_check and cancel_check()):
            debug_log(lambda: f"[WorldSim] {err_label}失败且无正文（{err}），重试一次")
            full2, err2, usage2 = self._narrate_stream(
                world, llm, api, messages, on_chunk, cancel_check, err_label)
            if full2.strip():
                return full2, "", usage2
            return full2, err2 or err, usage2 or usage
        return full, err, usage

    def narrate_outcome(
            self, world: World, scene: SceneLog, player_action: Optional[str],
            intent: Optional[dict], preset: WorldSimPreset,
            on_chunk: Optional[Callable[[str], None]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[str, str, Optional[LlmUsage]]:
        """调叙事 LLM 流式生成旁白。on_chunk 透传正文片段（[NPC] 命令行已被管线剥离）。

        返回 (full_text, error, usage)，full_text 为纯正文。取消返回已收正文 + error="已取消"。
        非流式 API 兜底（一次性返回，on_chunk 调一次）。
        """
        api = self._resolve_api(preset.narrative_api_id or preset.calculator_api_id)
        if not api:
            return "", "未配置可用的叙事 LLM API。", None
        tmp_preset = Preset(
            name="world_sim_narrate",
            system_prompt=preset.narrative_system_full(),
            temperature=preset.narrative_temperature,
            max_tokens=preset.narrative_max_tokens,
            top_p=preset.narrative_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        messages = self._build_narrate_messages(world, scene, player_action, intent, preset, is_intro=False)
        return self._narrate_with_retry(world, llm, api, messages, on_chunk, cancel_check, "叙事")

    def narrate_intro(
            self, world: World, scene: SceneLog, preset: WorldSimPreset,
            on_chunk: Optional[Callable[[str], None]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[str, str, Optional[LlmUsage]]:
        """首回合开场旁白（scene.log 空时调）。与 narrate_outcome 同走 [P18] 管线。"""
        api = self._resolve_api(preset.narrative_api_id or preset.calculator_api_id)
        if not api:
            return "", "未配置可用的叙事 LLM API。", None
        tmp_preset = Preset(
            name="world_sim_intro",
            system_prompt=preset.narrative_system_full(),
            temperature=preset.narrative_temperature,
            max_tokens=preset.narrative_max_tokens,
            top_p=preset.narrative_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        messages = self._build_narrate_messages(world, scene, None, None, preset, is_intro=True)
        return self._narrate_with_retry(world, llm, api, messages, on_chunk, cancel_check, "开场旁白")

    def compress_scene_history(
            self, world: World, scene: SceneLog, preset: WorldSimPreset,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[bool, Optional[LlmUsage]]:
        """[P15a1] 场景日志滚动总结：len(log) 超阈值时把最老 len-12 条压成前情摘要。

        阈值 = preset.scene_summary_threshold（overlay 可覆盖；0=关）。LLM 收到
        既有 summary + 待总结条目，输出合并后的新摘要（输出上限 1 万起，期望 3-6 句短摘要）。
        成功：scene.summary 重写 + 删已总结条目 + touch，返回 (True, usage)。
        失败/取消/未触发/无 API：条目原样保留（下轮重试），返回 (False, usage|None)。
        [!] 只在 SceneWorker 回合收尾调用（子线程）；删除条目不回收 UI 气泡（视觉史保留）。
        """
        threshold = int(self._per_world(world, "scene_summary_threshold",
                                        getattr(preset, "scene_summary_threshold", 20), preset))
        if threshold <= 0 or len(scene.log) <= threshold:
            return False, None
        n = len(scene.log) - 12
        if n <= 0:
            return False, None
        if cancel_check and cancel_check():
            return False, None
        api = self._resolve_api(preset.calculator_api_id)
        if not api:
            return False, None
        # [用户反馈 2026-08-23] 摘要字数上限可配置：注入提示词作硬约束（0=不限）
        max_chars = int(self._per_world(world, "scene_summary_max_chars",
                                        getattr(preset, "scene_summary_max_chars", 400), preset))
        sys_prompt = _SCENE_SUMMARY_SYSTEM_PROMPT
        if max_chars > 0:
            sys_prompt += (f"\n5. 总长度控制在 {max_chars} 字以内"
                           "（超出须进一步浓缩，宁删细节不超字数）。")
        tmp_preset = Preset(
            name="world_sim_scene_summary",
            system_prompt=sys_prompt,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, int(preset.calculator_max_tokens or 10000)),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        old = scene.log[:n]  # [P29] 保存待折叠条目引用（del 后 scene.log 切片失效，old 独立持有）
        lines = []
        for e in old:
            tag = {"player": "玩家", "narrator": "旁白", "system": "系统"}.get(e.role, e.role)
            content = scrub_npc_cmd_head(e.content or "").strip()
            if not content:
                continue
            lines.append(f"[{tag}|回合{e.tick}] {content[:400]}")
        user_msg = (
            # [修 2026-09-10] 补世界观锚：本摘要经 scene.summary 每回合回流进 settle/narrate
            # 上下文，缺锚会让题材串味被永久放大（一致性缺口，非新增调用）。
            _world_anchor(world) + "\n\n"
            + "【既有前情摘要】\n" + (scene.summary or "（无，首次总结）")
            + "\n\n【待总结的场景记录】（按时间从旧到新）\n" + "\n".join(lines)
            + "\n\n请输出合并整理后的前情摘要。"
        )
        result = llm.chat_cancelable(
            [{"role": "system", "content": tmp_preset.system_prompt},
             {"role": "user", "content": user_msg}],
            cancel_check=cancel_check, block_labels=["系统", "用户"],
        )
        if result.cancelled or result.error or not (result.content or "").strip():
            return False, result.usage
        scene.summary = result.content.strip()
        del scene.log[:n]
        scene.touch()
        # [P29] 折叠成功后批量整理在场 NPC 记忆（最后抢救：这些旧旁白即将永远消失）。
        # best-effort：失败不影响折叠结果（summary 已成功）。复用本次 worker 线程不阻塞 UI。
        fold_usage = result.usage
        try:
            if (not (cancel_check and cancel_check())
                    and getattr(preset, "npc_memory_enabled", True)):
                fr = self.npc_memory().consolidate_fold(
                    world, scene, preset, cancel_check=cancel_check,
                    folded_entries=old, npc_summary=scene.summary)
                if fr.get("usage") is not None:
                    fold_usage = fr["usage"]  # 记忆整理的 usage 覆盖（更晚的调用更相关）
        except Exception:
            pass
        return True, fold_usage

    # ============ [记忆化私聊 2026-08-29] 关窗总结：会话 + 既有记忆 -> 合并整理 ============

    def summarize_chat_session(self, world: World, npc, session: list,
                               preset: Optional[WorldSimPreset] = None,
                               cancel_check: Optional[Callable[[], bool]] = None
                               ) -> tuple[bool, str]:
        """[记忆化私聊 2026-09-08 重构] 关窗时把私聊会话并入 NPC 记忆（一次 LLM 调用）。

        复用 NpcMemoryService.consolidate_chat（= check_and_update 的 _consolidate_
        summary / _consolidate_hybrid 整条链路）：同一套 preset 提示词（summary=段落 /
        hybrid=[triggers] 条目）、「现有记忆全文 + 素材 -> 合并整理（保留仍有效旧
        记忆）」语义、npc_memory API 兜底链、玩家身份行——与场景整理口径对齐。

        旧版（2026-08-29）独立硬编码「5 分类」提示词 + merge_chat_summary 覆盖写
        summary，且既有记忆输入走 recent_lines（按分号切段每段 60 字）——真机存档
        实测：私聊过的 NPC（陈骁）丢了兽王骨/认兄弟/矿道之约等全部关键场景事实
        （合并输入只见旧记忆碎块且整段覆盖），分类结构也无任何引擎消费，故弃用
        （merge_chat_summary 及 5 分类解析已删）。

        素材 = 私聊全文（P23 硬规则：不预抽取不裁剪；私聊双方发言互在场互可感知，
        无旁白全知问题）。best-effort：失败/记忆 off 不阻断关窗。返回 (ok, msg)。
        """
        msgs = [m for m in (session or [])
                if isinstance(m, dict) and str(m.get("content", "") or "").strip()]
        if len(msgs) < 2:
            return True, ""
        preset = preset or WorldSimPreset()
        mem_svc = self.npc_memory()
        if mem_svc._mode(preset) == "off":
            return True, ""          # 记忆总开关关闭：静默跳过（不是失败）
        # 素材拼装：双方发言带说话人标签，全文不截（P23：不预抽取不裁剪）
        npc_name = npc.name or "对方"
        chat_text = "\n".join(
            f"{'玩家' if m.get('role') == 'user' else npc_name}：{str(m.get('content', '') or '')}"
            for m in msgs)
        material = (
            "【本次私聊对话全文】\n"
            f"（这是一场 {npc_name} 与玩家的一对一私聊记录，按时间从旧到新。「玩家：」"
            f"开头的行是玩家当面亲口所说，「{npc_name}：」开头的是 {npc_name} 本人亲口"
            "所说——私聊中双方发言互相都直接听见、都在场，不存在该 NPC 感知不到的内容；"
            "本素材无旁白、无结算结构化后果，全部可直接作为事实整理。）\n"
            + chat_text
        )
        ok = mem_svc.consolidate_chat(npc, world, material, preset, cancel_check)
        if not ok:
            debug_log(lambda: "[WorldSim] 私聊关窗记忆整理失败（无 API/LLM 失败/已取消）")
            return False, "记忆整理失败"
        return True, ""

    def npc_private_chat(
            self, world: World, npc, history: list, preset: Optional[WorldSimPreset],
            on_chunk: Optional[Callable[[str], None]] = None,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[str, str, Optional[LlmUsage]]:
        """[P10] 与好友 NPC 私聊（叙事 LLM 流式回复）。

        人设上下文 = NPC 档案（身份/性格/目标/小传）+ 交情档位 + 世界观一句话 +
        NPC 个人记忆召回（若开启）+ 最近若干条私聊历史。返回 (full, err, usage)。
        取消返回已收文本 + err="已取消"。非流式 API 兜底一次性返回。
        """
        api = self._resolve_api((preset.narrative_api_id or preset.calculator_api_id) if preset else "")
        if not api:
            return "", "未配置可用的叙事 LLM API，无法私聊。", None
        aff = int(getattr(npc, "affinity", 0))
        rel = nre.affinity_level(aff)
        # 记忆召回（best-effort，失败不影响私聊）
        mem_text = ""
        if preset is not None and getattr(preset, "npc_memory_enabled", True):
            try:
                last_user = next((h.get("content", "") for h in reversed(history or [])
                                  if h.get("role") == "user"), "")
                mem_text = self.npc_memory().recall(npc, world, last_user or npc.name, preset)
            except Exception:
                mem_text = ""
        # [2026-08-23 用户卡绑定] 玩家身份行：知道玩家姓名/人设，按名字称呼不泛称。
        _uid = self._fmt_bound_user(world)
        sys_prompt = (
            f"你是 SLG 游戏世界里的 NPC「{npc.name}」，正在与玩家（你的"
            f"{'好友' if nre.is_friend(world, npc) else '熟人'}）私下交谈。\n"
            f"身份：{npc.role or '未知'}；性格：{npc.personality or '未知'}；目标：{npc.goal or '未知'}。\n"
            + (f"说话风格（泛义参考：学其整体语气与用词倾向，不要逐字复读，"
               f"更不要每轮用相同句式开场）：{npc.speech_style}\n"
               if getattr(npc, "speech_style", "") else "")
            + (f"背景小传：{npc.notes}\n" if getattr(npc, "notes", "") else "")
            + f"你与玩家的交情：{rel}（{aff}/100）。请始终保持这个亲疏程度与性格口吻。\n"
            + (f"玩家身份：{_uid}。请按此姓名称呼玩家，勿用泛称。\n" if _uid else "")
            # [P9] 天赋剧情联动：NPC 知晓玩家身怀天赋，对话可自然提及/惊叹/请教
            # （雷天灵根的朋友会聊起雷法见闻）——由 LLM 据交情自行织入，不强制。
            + (f"你对玩家身怀天赋的了解：{te.talent_summary(world.player)}。\n"
               if (getattr(world.player, "talents", None) or []) else "")
            + (f"你对玩家的记忆：\n{mem_text}\n" if mem_text else "")
            + f"世界观背景：{world.premise}\n"
            # [P19] 文风层：私聊与旁白同语体（用户清空则不注入）
            + ((f"\n【文风偏好】\n{preset.narrative_style_prompt.strip()}\n")
               if preset is not None and getattr(preset, "narrative_style_prompt", "").strip() else "")
            + "\n"
            "要求：以第一人称用「{npc_name}」的口吻直接回复，语气贴合性格与交情；"
            "不要输出旁白动作描写以外的 JSON/选项；每次回复 1-4 句，像熟人间的私聊。"
        ).replace("{npc_name}", npc.name)
        tmp_preset = Preset(
            name="world_sim_npc_chat",
            system_prompt=sys_prompt,
            temperature=(preset.narrative_temperature if preset else 0.8),
            max_tokens=(preset.narrative_max_tokens if preset else 10000),
            top_p=(preset.narrative_top_p if preset else 0.95),
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        messages = [{"role": "system", "content": sys_prompt}]
        for h in (history or [])[-12:]:
            role = h.get("role", "user")
            content = str(h.get("content", "") or "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        full = ""
        usage: Optional[LlmUsage] = None
        if getattr(api, "streaming", True):
            try:
                for event_type, data in llm.chat_stream(
                        messages, cancel_check=cancel_check, block_labels=["系统", "用户"],
                ):
                    if event_type == "text":
                        full += data
                        if on_chunk and not (cancel_check and cancel_check()):
                            on_chunk(data)
                    elif event_type == "usage":
                        usage = data
                    elif event_type == "error":
                        return full, data, usage
                    elif event_type == "cancelled":
                        return full, "已取消", usage
            except Exception as e:
                debug_log(lambda: f"[WorldSim] NPC 私聊流式异常: {e}")
                return full, f"私聊生成异常：{e}", usage
        else:
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return "", "已取消", result.usage
            if result.error:
                return "", result.error, result.usage
            full = result.content
            usage = result.usage
            if on_chunk and full:
                on_chunk(full)
        return full, "", usage

    def move_player(self, world: World, target_loc_id: str) -> tuple[bool, str]:
        """玩家经地图对话框前往相邻地点。校验连通性，变更 player.location_id。

        与 apply_intent 的 move 分支同口径（连通性校验），但供 UI 地图按钮直接调。
        返回 (ok, msg)。不推进 tick（移动的叙事由后续回合承载，或 UI 单独起一轮）。
        """
        cur = self._current_location(world)
        if not cur:
            return False, "无当前地点"
        adj = self._adjacent_locations(world, cur)
        target = next((l for l in adj if l.id == target_loc_id), None)
        if target is None:
            return False, "该地点不与当前地点相邻，无法直接抵达"
        source_dungeon = dge.interior_dungeon(world, cur)
        target_dungeon = dge.interior_dungeon(world, target)
        if source_dungeon is not None:
            if target.id != source_dungeon.location_id:
                return False, "须从秘境第一层入口离开"
            return self.exit_dungeon(world)
        if target_dungeon is not None:
            return self.enter_dungeon(world, target_dungeon)
        world.player.location_id = target.id
        # [P27] 跨地点移动：place_id 重置为目标地点默认场所
        world.player.place_id = getattr(target, "default_place_id", "") or ""
        # [P7f] 标记目标地点已发现 + 已探索（地图节点视觉 + 迷雾边界更新）
        target.discovered = True
        target.explored = True
        # [!] 到达后揭示新的相邻地点（走近才发现，探索感）
        self.reveal_adjacent(world)
        # [P10b] 同行 NPC 跟随移动（与 apply_intent move 分支同口径）
        comp_moved, comp_dropped = self._companions_follow(world, target)
        # [P8] 奇遇自动触发：首次进入该地点（encounter_done False）时 roll 一次，每地点只一次。
        # move_player 不推进 tick（按既有契约），奇遇结算用当前 tick_count 作种子（确定性可复现）。
        enc_summary: dict = {}
        self._maybe_trigger_encounter(world, target, enc_summary, mode="auto")
        # [P7i] 任务进度钩子：visit（按地点名 + region 匹配）
        try:
            qe.update_progress(world, "visit", target.name,
                               aliases=[target.region])
        except Exception:
            pass
        msg = f"已前往「{target.name}」"
        # [P10b] 同行提示（跟随/掉队）
        if comp_moved:
            msg += f"（{'、'.join(comp_moved)}与你同行）"
        if comp_dropped:
            msg += f"（{'、'.join(comp_dropped)}已掉队）"
        # [P8] 若命中奇遇，把提示附在返回消息（UI 场景页可即时提示玩家）
        if enc_summary.get("encounter"):
            msg += f"（{enc_summary['encounter'].get('narration_hint', '')}）"
        elif enc_summary.get("explore_xp"):
            msg += f"（探索心得 +{enc_summary['explore_xp']} 经验）"
        return True, msg

    # ================================================================
    # ============ [P7f] 无限地图拓展（坐标布局 + 迷雾拓展）============
    # ================================================================
    def _layout_locations(self, locations: list):
        """[P7f] 给地点分配网格坐标（x/y），保证连通图。

        BFS 布局：从首个地点出发，邻居按 8 方向轮转找最近空位；孤岛分量（与主图不连通）
        补一条边到最近的已布局地点，让整个世界连通（避免地图有不可达孤岛）。
        纯算法无 LLM，确定性。
        """
        if not locations:
            return
        by_id = {loc.id: loc for loc in locations}
        # 无向邻接表（反向补边：A 连 B 则 B 也连 A）
        adj = {loc.id: set(c for c in loc.connections if c in by_id) for loc in locations}
        for lid in list(adj):
            for c in list(adj[lid]):
                adj[c].add(lid)
        occupied = {}      # (x,y) -> loc_id
        placed = set()
        dirs = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
        from collections import deque

        def free_slot(px: int, py: int, hint: int = 0):
            # 先试 8 邻居（距离 1），按 hint 起始轮转
            for i in range(8):
                ddx, ddy = dirs[(hint + i) % 8]
                if (px + ddx, py + ddy) not in occupied:
                    return px + ddx, py + ddy
            # 螺旋外扩（切比雪夫距离环）
            for r in range(2, 60):
                for ddx in range(-r, r + 1):
                    for ddy in range(-r, r + 1):
                        if max(abs(ddx), abs(ddy)) == r and (px + ddx, py + ddy) not in occupied:
                            return px + ddx, py + ddy
            return px + 1, py + 1

        for start_loc in locations:
            start = start_loc.id
            if start in placed:
                continue
            # 分量起点：首个分量放 (0,0)，后续分量放原点附近空位
            sx, sy = (0, 0) if not occupied else free_slot(0, 0)
            by_id[start].x, by_id[start].y = sx, sy
            occupied[(sx, sy)] = start
            placed.add(start)
            # 孤岛补边：连到最近的已布局地点（让世界连通，move_player 相邻校验才可达）
            if len(placed) > 1:
                best, best_d = None, float("inf")
                for (ox, oy), oid in occupied.items():
                    if oid == start:
                        continue
                    d = abs(ox - sx) + abs(oy - sy)
                    if d < best_d:
                        best_d, best = d, oid
                if best and best not in adj[start]:
                    adj[start].add(best)
                    adj[best].add(start)
                    if best not in by_id[start].connections:
                        by_id[start].connections.append(best)
                    if start not in by_id[best].connections:
                        by_id[best].connections.append(start)
            # BFS 分量内布局
            queue = deque([start])
            while queue:
                lid = queue.popleft()
                px, py = by_id[lid].x, by_id[lid].y
                hint = 0
                for c in adj.get(lid, []):
                    if c in placed:
                        continue
                    nx, ny = free_slot(px, py, hint)
                    hint = (hint + 1) % 8
                    by_id[c].x, by_id[c].y = nx, ny
                    occupied[(nx, ny)] = c
                    placed.add(c)
                    queue.append(c)

    # 题材化迷雾名（地图边界未探索区域的称呼，据 attribute_template_id）
    _MIST_NAMES = {
        "xianxia": "未探禁地", "wuxia": "未踏江湖", "modern": "未知区域",
        "scifi": "信号盲区", "apocalypse": "废土深处", "western_fantasy": "未知荒野",
    }

    def mist_name(self, world: World) -> str:
        """[P7f] 题材化迷雾名。"""
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        return self._MIST_NAMES.get(tid, "未知区域")

    def fog_directions(self, world: World, loc: Location) -> list:
        """[P7f] 已探索地点的可拓展迷雾方向（4 正方向邻居格无地点的方向）。

        返回 [(direction_name, dx, dy), ...]，供地图 UI 在地点边界画迷雾块。
        """
        if loc is None:
            return []
        occupied = {(l.x, l.y) for l in world.locations}
        result = []
        for dname, (dx, dy) in [("north", (0, -1)), ("south", (0, 1)),
                                 ("east", (1, 0)), ("west", (-1, 0))]:
            if (loc.x + dx, loc.y + dy) not in occupied:
                result.append((dname, dx, dy))
        return result

    def _generate_expansion_llm(self, world: World, from_loc: Location,
                                direction_name: str, batch_size: int,
                                preset: WorldSimPreset,
                                cancel_check: Optional[Callable[[], bool]]) -> list:
        """[P7f] LLM 生成新地点骨架。返回 list[dict]（失败/取消回退 []）。"""
        api = self._resolve_api(preset.calculator_api_id or preset.narrative_api_id)
        if not api:
            return []
        tmp_preset = Preset(
            name="world_sim_expand",
            system_prompt=_EXPAND_SYSTEM_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        genre = ", ".join(getattr(world, "genre_tags", []) or [])
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "") or ""
        # [2026-08-28 用户定稿] premise 与世界书【完整】注入拓展（不截断/不限条数）：
        # 世界结构类设定（如「每场所=一栋楼」）只经世界观控制，新开地图必须延续同一语义；
        # _fmt_world_lore 只取优先级前 5 条，此处全量展开（守「世界观是唯一生成方向」）。
        _lore_entries = sorted(getattr(world, "lore", None) or [],
                               key=lambda e: getattr(e, "priority", 5))
        _lore_full = "【世界书】\n" + "\n".join(
            f"  {e.key}：{e.content}" for e in _lore_entries) if _lore_entries else ""
        user_parts = [
            f"世界题材：{genre or tid or '未知'}",
            # [修 2026-09-10] 锚行收敛到 `_world_anchor` 单一来源（原手拼 premise + 单独【基调】）
            _world_anchor(world),
        ]
        if _lore_full:
            user_parts.append(_lore_full)
        user_parts += [
            f"当前区域：{from_loc.region or '未知'}",
            f"起点地点：「{from_loc.name}」（{from_loc.desc}，危险度{from_loc.danger}）",
            f"拓展方向：{direction_name}（向未知地带探索）",
            f"已有地点（勿重名）：{'、'.join(l.name for l in world.locations)}",
            "[!] 世界观与世界书中的结构性设定（地点/场所的语义与命名规则）对新地点及其 "
            "places 同样生效，新地点与新场所必须延续既有语义，不得输出与世界观矛盾的内容。",
            f"请生成 {batch_size} 个相邻新地点（名称须与上述已有地点不同）。",
        ]
        user = "\n".join(user_parts)
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user}]
        for _ in range(2):
            if cancel_check and cancel_check():
                return []
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled or result.error or not (result.content or "").strip():
                continue
            data = _extract_json(result.content)
            if isinstance(data, dict) and isinstance(data.get("locations"), list):
                locs = [l for l in data["locations"] if isinstance(l, dict) and (l.get("name") or "").strip()]
                if locs:
                    return locs
        return []

    def _fallback_expansion(self, world: World, from_loc: Location, batch_size: int) -> list:
        """[P7f] LLM 失败时的确定性回退模板（保证拓展总有结果，不空手）。"""
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        # 据题材给通用新地点名模板
        templates = {
            "xianxia": ["无名山谷", "荒废洞府", "灵木林边", "迷雾小径"],
            "wuxia": ["荒野客栈", "山间小道", "废弃村落", "竹林深处"],
            "modern": ["陌生街区", "废弃仓库", "小巷深处", "无人住宅"],
            "scifi": ["废弃站点", "未知舱段", "信号盲区", "残骸地带"],
            "apocalypse": ["坍塌废墟", "变异丛林", "荒芜公路", "避难所遗迹"],
            "western_fantasy": ["荒野小径", "无名林地", "废弃哨站", "迷雾山谷"],
        }.get(tid, ["未知地带", "荒野", "小径", "边缘"])
        out = []
        for i in range(min(batch_size, len(templates))):
            nm = templates[i] if i < len(templates) else f"未知地带{i}"
            out.append({"name": nm, "desc": "一片尚未探索的未知地带。", "region": from_loc.region or "未知",
                        "danger": max(1, min(10, from_loc.danger + (1 if i % 2 else 0))), "connections": []})
        return out

    def _apply_expansion(self, world: World, from_loc: Location, dx: int, dy: int,
                         new_locs_data: list, preset: WorldSimPreset) -> list:
        """[P7f] 引擎接入：分配坐标 + 连通 + 创建 Location（标 discovered）。返回新 location id 列表。"""
        if not new_locs_data:
            return []
        occupied = {(l.x, l.y) for l in world.locations}
        created = []
        name_to_loc = {}
        # 第一个新地点放 from_loc 沿方向的相邻格
        base_x, base_y = from_loc.x + dx, from_loc.y + dy
        # 若该格被占，螺旋找空位
        if (base_x, base_y) in occupied:
            for r in range(1, 30):
                found = False
                for ddx in range(-r, r + 1):
                    for ddy in range(-r, r + 1):
                        if max(abs(ddx), abs(ddy)) == r and (from_loc.x + dx + ddx, from_loc.y + dy + ddy) not in occupied:
                            base_x, base_y = from_loc.x + dx + ddx, from_loc.y + dy + ddy
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
        for idx, ld in enumerate(new_locs_data):
            if idx == 0:
                nx, ny = base_x, base_y
            else:
                # 后续新地点沿 base 螺旋找空位
                nx, ny = base_x, base_y
                for r in range(1, 30):
                    found = False
                    for ddx in range(-r, r + 1):
                        for ddy in range(-r, r + 1):
                            if max(abs(ddx), abs(ddy)) == r and (base_x + ddx, base_y + ddy) not in occupied:
                                nx, ny = base_x + ddx, base_y + ddy
                                found = True
                                break
                        if found:
                            break
                    if found:
                        break
            # [地名去重] 引擎兜底：LLM 违规生成重名时加序号后缀，防两个同名不同 id 地点
            # 致 apply_intent 按名 first-match 移错地点、name_to_loc 后者覆盖前者。
            raw_name = (ld.get("name") or f"未知地带{idx}").strip()
            existing_names = {l.name for l in world.locations} | set(name_to_loc.keys())
            loc_name = raw_name
            if loc_name in existing_names:
                suffix = 2
                while f"{raw_name}{suffix}" in existing_names:
                    suffix += 1
                loc_name = f"{raw_name}{suffix}"
            loc = Location(
                name=loc_name,
                desc=ld.get("desc", "") or "一片尚未探索的未知地带。",
                region=ld.get("region", "") or from_loc.region or "未知",
                danger=_safe_int(ld.get("danger", from_loc.danger), from_loc.danger, 1, 10),
                # [P12] 拓展地点类型（聚落/野外）；默认野外（探索未知地带）
                kind=("settlement" if str(ld.get("kind") or "").strip() == "settlement" else "wilderness"),
                # [P34e] 解析 LLM 给的 settlement_size（白名单回退；仅 settlement 有意义）
                settlement_size=(str(ld.get("settlement_size") or "").strip()
                                 if (str(ld.get("kind") or "").strip() == "settlement"
                                     and str(ld.get("settlement_size") or "").strip()
                                     in ("city", "town", "village")) else ""),
                # [!] 新拓展区域默认「未发现」（地图不显示、保持神秘感）：
                # 与玩家相邻的由 reveal_adjacent 在拓展完成后自动揭示（保证可达可见），
                # 更远的走近才逐个发现。
                x=nx, y=ny, discovered=False, explored=False,
            )
            world.locations.append(loc)
            occupied.add((nx, ny))
            name_to_loc[loc.name] = loc
            created.append(loc)
        # 连通：from_loc <-> 第一个新地点；新地点之间按 connections 名字连
        if created:
            if created[0].id not in from_loc.connections:
                from_loc.connections.append(created[0].id)
            if from_loc.id not in created[0].connections:
                created[0].connections.append(from_loc.id)
        # ---- [P25a] 秘境入口挂载（入口渠道 b）：LLM dungeon 字段优先（每批至多 1 个，
        # 防刷屏）；LLM 未给时 25% 兜底挂一个新野外地点（题材池命名，确定性）。----
        try:
            rng_d = SeededRng.seed_from(world.id, world.tick_count, "expand_dgn")
            attached = False
            wild_new = [l for l in created if getattr(l, "kind", "") == "wilderness"
                        and dge.dungeon_at(world, l) is None]
            for ld, loc in zip(new_locs_data, created):
                if attached or loc not in wild_new:
                    continue
                ddata = ld.get("dungeon") if isinstance(ld, dict) else None
                if isinstance(ddata, dict) and str(ddata.get("name") or "").strip():
                    d_name = str(ddata.get("name")).strip()
                    # theme_hint/名称匹配题材池主题（LLM 命名「废弃地铁隧道」配现代主题文案）
                    theme_id = dge.match_theme(self._genre_id(world),
                                               str(ddata.get("theme_hint") or "") or d_name)
                    _nd = dge.build_dungeon(world, loc.id, loc.danger, theme_id=theme_id,
                                      name=d_name, rng=rng_d)
                    _nd.discovered = False   # [D03]
                    attached = True
            if not attached and wild_new:
                dgn_chance = max(0.0, min(1.0, float(self._per_world(
                    world, "map_expansion_dungeon_chance",
                    getattr(preset, "map_expansion_dungeon_chance", 0.25), preset) or 0.0)))
                if dgn_chance > 0 and rng_d.chance(dgn_chance):
                    loc = wild_new[rng_d.roll(0, len(wild_new) - 1)]
                    _nd2 = dge.build_dungeon(world, loc.id, loc.danger, rng=rng_d)
                    _nd2.discovered = False   # [D03]
        except Exception:
            pass
        for ld, loc in zip(new_locs_data, created):
            for cn in (ld.get("connections") or []):
                cn = (cn or "").strip()
                if cn and cn in name_to_loc:
                    target = name_to_loc[cn]
                    if target.id != loc.id:
                        # [P7f] 地点连接双向回填（玩家需能往返，连通性校验才正确）
                        if target.id not in loc.connections:
                            loc.connections.append(target.id)
                        if loc.id not in target.connections:
                            target.connections.append(loc.id)
        # 给新地点挂资源点（[P12] 仅野外点 + tier 按危险度分层 + 产出对应稀有度材料；
        # LLM location.resource 的 name/type/desc/tier 优先于题材模板）
        if getattr(preset, "gathering_enabled", True):
            overlay = getattr(world, "config_overlay", None) or {}
            tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
            tmpl = _GENRE_RESOURCE_TEMPLATES.get(tid) or _GENRE_RESOURCE_TEMPLATES["western_fantasy"]
            nodes_tmpl = tmpl["nodes"]
            for idx, (loc, ld) in enumerate(zip(created, new_locs_data)):
                if getattr(loc, "kind", "wilderness") != "wilderness":
                    continue   # [P12] 聚落不挂资源点
                res = ld.get("resource") if isinstance(ld, dict) else None
                res = res if isinstance(res, dict) else {}
                node_tmpl = nodes_tmpl[(len(world.locations) + idx) % len(nodes_tmpl)]
                rng = SeededRng.seed_from(world.id, world.tick_count, f"expand_res_{loc.id}")
                tier = _safe_int(res.get("tier"), we.danger_to_tier(loc.danger), 1, 5)
                pool = we.nearest_rarity_pool(world.items, tier)
                picks = rng.sample(pool, 2) if pool else []
                qty = 2 if tier >= 3 else 1
                drops = [{"item_id": it.id, "qty": qty, "rate": 0.4} for it in picks]
                loc.resource_nodes.append(ResourceNode(
                    name=str(res.get("name") or node_tmpl["name"]),
                    type=str(res.get("type") or node_tmpl["type"]),
                    desc=str(res.get("desc") or node_tmpl["desc"]),
                    tier=tier,
                    drops=drops, richness=100, richness_max=100,
                    cooldown_tick=0, cooldown_duration=getattr(preset, "resource_cooldown_ticks", 3),
                    regenerates=True, requires_tool=node_tmpl.get("requires_tool", ""),
                    stat_used=node_tmpl.get("stat_used", "dex"),
                ))
        # ---- [P27] 新地点场所生成（同 build_world 口径：LLM 出 + 兜底）----
        # places_enabled=False 跳过；dungeon 内部地点不场所化（_build 内 gate）。
        if getattr(preset, "places_enabled", True):
            ppl = max(0, min(10, int(getattr(preset, "places_per_location", 4) or 0)))
            gid = self._genre_id(world)
            for ld, loc in zip(new_locs_data, created):
                places, default_pid = _build_places_for_location(
                    loc, ld if isinstance(ld, dict) else {}, gid, True, ppl)
                loc.places = places
                loc.default_place_id = default_pid
                # [P34e] 城市预留场所：city/town 补 venue place（city 必含 3 类核心）
                self._ensure_city_venues(world, loc, ld if isinstance(ld, dict) else {}, preset)
            # [游商 2026-09-08] 拓展新地点按同口径建店（幂等，同地点同类不重复）+
            # 店场所绑定收尾——否则非 city 拓展地即使有商店类场所也永远零店。
            self._init_merchants_and_shops(world, preset)
            self._ensure_shop_places(world)
        # ---- [P34e] 探查/拓展概率：roll 新城市/新 NPC/新势力（SeededRng 独立 salt 确定性）----
        self._apply_expand_chances(world, created, new_locs_data, preset)
        # [修 2026-09-13] 拓展产物同样过生成一致性收口（新地点 danger 归零/新怪 danger
        # 对齐/新任务 kill 回填 + gather 目标保底 + collect 材料保底上架——拓展也出新任务与怪物）。
        self._ensure_generation_coherence(world, preset)
        self.ensure_quest_material_sources(world, preset)
        return [loc.id for loc in created]

    def expand_map_at(self, world: World, from_loc_id: str, direction,
                      preset: WorldSimPreset,
                      cancel_check: Optional[Callable[[], bool]] = None) -> tuple:
        """[P7f] 在 from_loc 指定方向拓展新地点（无限增殖沙盒核心）。

        direction: (dx, dy) 方向向量或 direction_name 字符串。
        流程：LLM 生成新地点骨架 -> 引擎分配坐标 + 接入连通图 + 标 discovered + 挂资源点。
        LLM 失败/取消回退确定性模板（保证拓展总有结果）。
        返回 (ok, msg, new_loc_ids)。
        """
        from_loc = next((l for l in world.locations if l.id == from_loc_id), None)
        if from_loc is None:
            return False, "起始地点不存在", []
        # 方向解析
        dir_map = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}
        if isinstance(direction, str):
            dx, dy = dir_map.get(direction, (1, 0))
            direction_name = direction
        else:
            dx, dy = direction
            direction_name = next((k for k, v in dir_map.items() if v == (dx, dy)), "east")
        batch = max(1, min(5, int(getattr(preset, "map_expansion_batch_size", 2))))
        # LLM 生成（失败回退模板）
        new_data = self._generate_expansion_llm(world, from_loc, direction_name, batch, preset, cancel_check)
        if not new_data:
            new_data = self._fallback_expansion(world, from_loc, batch)
        new_ids = self._apply_expansion(world, from_loc, dx, dy, new_data, preset)
        if not new_ids:
            return False, "拓展失败（无新地点）", []
        # [!] 相邻自动揭示：拓展起点连通的新地点立即可见（保证可达），
        # 其余保持未发现的迷雾状态，走近才逐个揭示。
        # 揭示锚点是 from_loc（玩家从哪个地点向外探索），不是当前所在地点——
        # 地图迷雾格挂在每个已探索地点上，可能从非当前地点向外拓展。
        revealed = self.reveal_adjacent(world, anchor=from_loc)
        new_names = [next((l.name for l in world.locations if l.id == i), i)
                     for i in new_ids if i in revealed]
        if new_names:
            msg = f"向{direction_name}探索，发现：{'、'.join(new_names)}" + \
                  ("（更远处仍有未知区域）" if len(new_names) < len(new_ids) else "")
        else:
            msg = f"向{direction_name}探索，迷雾散开了一些（走近才能看清新区域）"
        # [!] 新地点补场景背景图（口径同世界生成：名字+描述+区域，场景背景，无人）：
        # MapExpandWorker 是后台线程，此处逐张生成不卡 UI；cancel 可中断；图挂
        # loc.background 随 world 持久化（未揭示的地点走到时直接有图）。
        self.ensure_location_backgrounds(world, new_ids, preset, cancel_check)
        # [P11c] 开新地点伴随生成新物品（物品入世主渠道——商店只从已有物品进货，
        # 新物品靠探索获得；数量 map_expansion_item_count/新地点，0=关）。
        new_locs = [l for l in world.locations if l.id in set(new_ids)]
        n_items = self._generate_expansion_items(world, new_locs, preset, cancel_check)
        # [P12] 据新地点资源补新配方（资源-配方联动：新层级材料要有配方可消耗）
        n_recipes = self._generate_expansion_recipes(world, new_locs, preset, cancel_check)
        # [数据量] 开新地点伴随新增怪物物种（地图拓展数据随地图增长）
        n_monsters = self._generate_expansion_monsters(world, new_locs, preset, cancel_check)
        # [2026-08-21] 拓展新增物种的怪物图同步预生成（游玩外时机，不在战斗中现场渲染）；
        # best-effort：失败不阻断拓展（战斗 UI 无图留空展示）。
        if n_monsters > 0 and getattr(preset, "image_enabled", False) \
                and getattr(preset, "image_monster", True) and self.comfyui:
            try:
                self.pregenerate_monster_images(world, preset, cancel_check, only_pool=True)
            except Exception as e:
                debug_log(lambda: f"[WorldSim] 拓展怪物图预生成失败（忽略）: {e}")
        if n_items > 0:
            msg += f"，新物品入库 {n_items} 件"
        if n_recipes > 0:
            msg += f"，新增配方 {n_recipes} 个"
        if n_monsters > 0:
            msg += f"，新增怪物物种 {n_monsters} 只"
        return True, msg, new_ids

    def _generate_expansion_items(self, world: World, new_locs: list,
                                  preset: WorldSimPreset,
                                  cancel_check: Optional[Callable[[], bool]] = None) -> int:
        """[P11c] 开新地点伴随生成新物品（物品入世主渠道）。

        每个新地点 map_expansion_item_count 件（0=关）。LLM 出物品定义 ->
        引擎白名单清洗 + 数值兜底（_fill_item_defaults）；失败/取消回退题材装备模板。
        新物品按生图开关过滤后补 icon（legendary 开关管 epic/legendary，
        normal 开关管其余，口径同世界生成）。返回新增物品数。
        """
        if cancel_check and cancel_check():
            return 0
        per_loc = _safe_int(self._per_world(world, "map_expansion_item_count",
                                            getattr(preset, "map_expansion_item_count", 2), preset), 2, 0, 10)
        locs = [l for l in (new_locs or []) if getattr(l, "id", "")]
        if per_loc <= 0 or not locs:
            return 0
        made = self._generate_expansion_items_llm(world, locs, per_loc * len(locs), preset, cancel_check)
        if not made:
            made = self._fallback_expansion_items(world, locs, per_loc)
        if not made:
            return 0
        # [!] narrative 世界不填战斗数值（与 _init_combat_stats 的 narrative 早退同口径）
        combat_system = self._per_world(world, "combat_system",
                                        preset.combat_system if preset else "crpg", preset)
        if combat_system != "narrative":
            self._fill_item_defaults(world, made, preset)
        legendary_on = bool(getattr(preset, "image_legendary_item", False))
        normal_on = bool(getattr(preset, "image_normal_item", False))
        want_icons = [it for it in made
                      if (legendary_on if it.rarity in ("epic", "legendary") else normal_on)]
        if want_icons:
            self.ensure_item_icons(world, preset, cancel_check, limit=3, items=want_icons)
        return len(made)

    def _generate_expansion_items_llm(self, world: World, locs: list, total: int,
                                      preset: WorldSimPreset,
                                      cancel_check: Optional[Callable[[], bool]]) -> list:
        """调结算 LLM 为新地点生成物品定义（失败/取消返回 []，走兜底）。"""
        api = self._resolve_api(preset.calculator_api_id or preset.narrative_api_id)
        if not api:
            return []
        tmp_preset = Preset(
            name="world_sim_expand_items",
            system_prompt=_EXPAND_ITEMS_SYSTEM_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        gt = self._genre_text(world)
        genre = ", ".join(getattr(world, "genre_tags", []) or [])
        loc_lines = [f"- {l.name}（{(l.desc or '')[:50]}，危险度{l.danger}，区域{l.region or '未知'}）"
                     for l in locs]
        existing = [it.name for it in world.items[:60]]
        # [用户指示 2026-08-25] 注入范围表（题材模板已知，带装备偏好维度提示）
        _ov = getattr(world, "config_overlay", None) or {}
        _tid = _ov.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        _gb = _GENRE_EQUIPMENT_BONUS.get(_tid) or _GENRE_EQUIPMENT_BONUS["western_fantasy"]
        _dims = "；".join(f"{t}偏{'/'.join(ks)}" for t, ks in _gb.items() if ks)
        user = (
            f"世界题材：{genre or '未知'}；题材货币：{gt.currency}\n"
            f"新开辟的地点：\n" + "\n".join(loc_lines) + "\n"
            f"现有物品名（避免重复）：{'、'.join(existing) if existing else '（暂无）'}\n"
            + _item_attr_ranges_text(_dims) + "\n"
            f"请新增 {total} 件物品。"
        )
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user}]
        for _ in range(2):
            if cancel_check and cancel_check():
                return []
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled or result.error or not (result.content or "").strip():
                continue
            data = _extract_json(result.content)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                items = self._sanitize_new_items(world, data["items"], total)
                if items:
                    return items
        return []

    def _sanitize_new_items(self, world: World, raw: list, limit: int) -> list:
        """清洗 LLM 新物品定义：type/rarity/slot 白名单 + 名字去重（与现有 & 批内）。"""
        type_wl = {"weapon", "armor", "consumable", "material", "accessory"}
        rarity_wl = {"common", "uncommon", "rare", "epic", "legendary"}
        from src.models.world import EQUIPMENT_SLOTS
        slot_wl = set(EQUIPMENT_SLOTS)
        out = []
        names = {it.name for it in world.items}
        for g in raw:
            if len(out) >= limit:
                break
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            if not name or name in names:
                continue
            t = str(g.get("type") or "material").strip()
            if t not in type_wl:
                t = "material"
            r = str(g.get("rarity") or "common").strip()
            if r not in rarity_wl:
                r = "common"
            slot = str(g.get("slot") or "").strip()
            if slot not in slot_wl:
                slot = ""
            # [用户指示 2026-08-25] LLM 自填的 stat_bonus/level/consume_effect 透传（白名单清洗）
            sb = g.get("stat_bonus")
            if isinstance(sb, dict):
                sb = {k: max(1, min(8, _safe_int(v, 1, 0, 8)))
                      for k, v in sb.items() if k in ("str", "dex", "int", "vit", "luk")}
            else:
                sb = {}
            from src.models.world import _CONSUME_EFFECT_TYPES
            ce = g.get("consume_effect")
            if not (isinstance(ce, dict)
                    and str(ce.get("type", "") or "") in _CONSUME_EFFECT_TYPES
                    and str(ce.get("type", "") or "") != ""):
                ce = None
            it = Item(name=name, type=t, rarity=r,
                      desc=str(g.get("desc") or "") or "",
                      slot=slot,
                      attack=_safe_int(g.get("attack"), 0, 0, 9999),
                      defense=_safe_int(g.get("defense"), 0, 0, 9999),
                      # [heal_full 并入 heal_pct 2026-09-10] 拓展物品回血只收 heal_pct；
                      # 旧 heal_amount 输入兼容（LLM/旧缓存残留）+ heal_full 别名统一
                      # 经 _fill_item_defaults 尾的 _normalize_heal_pct 迁移。
                      heal_pct=max(0, min(100, _safe_int(g.get("heal_pct"), 0, 0, 100))),
                      heal_amount=_safe_int(g.get("heal_amount"), 0, 0, 9999),
                      stat_bonus=sb,
                      level=max(0, min(6, _safe_int(g.get("level"), 0, 0, 6))),
                      consume_effect=ce,
                      effects=str(g.get("effects") or "") or "",
                      base_price=max(0, _safe_int(g.get("base_price"), 0, 0, 999999)))
            world.items.append(it)
            out.append(it)
            names.add(name)
        return out

    def _fallback_expansion_items(self, world: World, locs: list, per_loc: int) -> list:
        """LLM 失败时的确定性兜底：题材装备模板 + 品级 roll（SeededRng 可复现）。"""
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        names_tmpl = _GENRE_EQUIPMENT_NAMES.get(tid) or _GENRE_EQUIPMENT_NAMES["western_fantasy"]
        gt = self._genre_text(world)
        keys = list(names_tmpl.keys())
        existing = {it.name for it in world.items}
        out = []
        for loc in locs:
            rng = SeededRng.seed_from(world.id, world.tick_count, f"expand_item_{loc.id}")
            for _ in range(per_loc):
                slot = keys[rng.roll(0, len(keys) - 1)]
                rar = rng.pick(["common", "common", "uncommon", "uncommon", "rare"])
                variants = names_tmpl.get(slot) or ["无名之物"]
                base = rng.pick(variants) or variants[0]
                # [!] 重名闭环：品级后缀再撞则追加序号，保证全库唯一（name 兜底匹配依赖唯一名）
                name = base
                if name in existing:
                    name = f"{base}·{gt.rarity(rar)}"
                    n = 2
                    while name in existing:
                        name = f"{base}·{gt.rarity(rar)}{n}"
                        n += 1
                it = Item(name=name, type=_SLOT_TO_TYPE.get(slot, "material"), rarity=rar,
                          desc=f"探索{loc.name}所得。", slot=slot)
                world.items.append(it)
                out.append(it)
                existing.add(name)
        return out

    def _generate_expansion_recipes(self, world: World, new_locs: list,
                                    preset: WorldSimPreset,
                                    cancel_check: Optional[Callable[[], bool]] = None) -> int:
        """[P12] 据新地点资源补新配方（资源-配方联动）。

        LLM 出配方（inputs/output 引用现有物品名）；失败/取消回退引擎组合
        （新地点层级材料 x2 -> 同稀有度产出）。返回新增配方数。
        """
        if not getattr(preset, "crafting_enabled", True):
            return 0
        if cancel_check and cancel_check():
            return 0   # [审查修] 取消与物品路径同口径：不落兜底配方
        target = max(0, min(10, int(getattr(preset, "map_expansion_recipe_count", 2) or 0)))
        if target <= 0:
            return 0
        locs = [l for l in (new_locs or [])
                if getattr(l, "kind", "wilderness") == "wilderness" and l.resource_nodes]
        if not locs:
            return 0
        made = self._generate_expansion_recipes_llm(world, locs, preset, cancel_check, target)
        if not made:
            made = self._fallback_expansion_recipes(world, locs, target)
        world.recipes.extend(made)
        return len(made)

    def _generate_expansion_recipes_llm(self, world: World, locs: list,
                                        preset: WorldSimPreset,
                                        cancel_check: Optional[Callable[[], bool]],
                                        target: int = 2) -> list:
        """调结算 LLM 为新地点资源出配方（失败/取消返回 []，走兜底）。"""
        api = self._resolve_api(preset.calculator_api_id or preset.narrative_api_id)
        if not api:
            return []
        tmp_preset = Preset(
            name="world_sim_expand_recipes",
            system_prompt=_EXPAND_RECIPE_SYSTEM_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        gt = self._genre_text(world)
        res_lines = []
        for l in locs:
            for node in (l.resource_nodes or []):
                tier = getattr(node, "tier", 1) or 1
                names = ", ".join(
                    next((it.name for it in world.items if it.id == d.get("item_id")), d.get("item_id", ""))
                    for d in (node.drops or []) if isinstance(d, dict))
                res_lines.append(f"- {l.name}（危险度{l.danger}）：{node.name}（层级{tier}）产出 {names}")
        existing_items = [f"{it.name}({it.type}/{it.rarity})" for it in world.items[:60]]
        user = (
            f"世界题材：{', '.join(getattr(world, 'genre_tags', []) or []) or '未知'}；货币：{gt.currency}\n"
            f"新开辟地点的资源：\n" + "\n".join(res_lines) + "\n"
            f"现有物品（inputs/output 只能引用这些名字）：{'、'.join(existing_items)}\n"
            f"请新增 {target} 个配方（消耗新地点资源，inputs/output 只能引用现有物品名）。"
        )
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user}]
        for _ in range(2):
            if cancel_check and cancel_check():
                return []
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled or result.error or not (result.content or "").strip():
                continue
            data = _extract_json(result.content)
            if isinstance(data, dict) and isinstance(data.get("recipes"), list):
                item_by_name = {it.name: it for it in world.items}
                out = []
                known_out = {r.output_item_id for r in world.recipes}
                for r in data["recipes"]:
                    if not isinstance(r, dict):
                        continue
                    out_it = _name_lookup(item_by_name, r.get("output"))
                    if out_it is not None and getattr(out_it, "teach_skill", None):
                        continue   # [P13] 技能书不可合成（读书要从强敌/商店/奇遇获取）
                    in_ids = []
                    for nm in (r.get("inputs") or []):
                        it2 = _name_lookup(item_by_name, nm)
                        if it2 is not None and (out_it is None or it2.id != out_it.id):
                            in_ids.append(it2.id)
                    if out_it is None or not in_ids or out_it.id in known_out:
                        continue
                    out.append(Recipe(
                        name=(str(r.get("name") or "").strip() or f"炼制{out_it.name}"),
                        desc=str(r.get("desc") or "") or "",
                        inputs=in_ids[:4], output_item_id=out_it.id, station="",
                        difficulty=_safe_int(r.get("difficulty"), 0, 0, 50)))
                    known_out.add(out_it.id)
                if out:
                    return out
        return []

    def _fallback_expansion_recipes(self, world: World, locs: list, target: int = 2) -> list:
        """LLM 失败时的确定性兜底：新地点层级材料 x2 -> 同稀有度产出（最多 target 个）。"""
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        station = {"xianxia": "炼丹炉", "wuxia": "药臼", "modern": "工作台",
                   "scifi": "合成终端", "apocalypse": "修补台",
                   "western_fantasy": "工坊"}.get(tid, "工坊")
        _RARITY_DIFF = {"common": 0, "uncommon": 8, "rare": 20, "epic": 35, "legendary": 50, "mythic": 65}
        _TIER_RAR = {1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary"}
        consumables = [it for it in world.items if it.type == "consumable"]
        out = []
        for loc in locs:
            if len(out) >= target:
                break
            node = (loc.resource_nodes or [None])[0]
            if node is None:
                continue
            tier = getattr(node, "tier", 1) or 1
            rng = SeededRng.seed_from(world.id, world.tick_count, f"expand_rec_{loc.id}")
            mats = [it for it in world.items
                    if it.type == "material" and it.rarity == _TIER_RAR.get(tier, "common")]
            if not mats:
                mats = we.nearest_rarity_pool(world.items, tier)
            ins = rng.sample(mats, min(2, len(mats)))
            # [P13] 技能书不可作配方产出（合成白嫖技能）；书 heal_amount=0 也不该是炼制目标
            consumables_nb = [c for c in consumables if not getattr(c, "teach_skill", None)]
            outs = [c for c in consumables_nb if c.rarity == _TIER_RAR.get(tier, "common")] or consumables_nb
            if not ins or not outs:
                continue
            out_it = rng.pick(outs)
            if any(r.output_item_id == out_it.id and
                   set(r.inputs or []) == {i.id for i in ins} for r in world.recipes):
                continue
            out.append(Recipe(
                name=f"炼制{out_it.name}",
                desc=f"于{station}用{'、'.join(i.name for i in ins)}炼制{out_it.name}（出自{loc.name}的收获）",
                inputs=[i.id for i in ins], output_item_id=out_it.id, station=station,
                difficulty=_RARITY_DIFF.get(out_it.rarity, 0)))
        return out

    def _generate_expansion_monsters(self, world: World, new_locs: list,
                                     preset: WorldSimPreset,
                                     cancel_check: Optional[Callable[[], bool]] = None) -> int:
        """[数据量] 开新地点伴随新增怪物物种（地图拓展数据随地图增长）。

        每次拓展 map_expansion_monster_count 只（0=关）。LLM 出怪物定义（danger 区间 +
        loot 引用材料名）-> 引擎白名单清洗 + loot 解析成 item id；失败/取消回退题材
        怪物兜底池确定性补。新怪物 append 进 world.monster_pool（野外引擎据此抽怪）。
        返回新增怪物数。
        """
        if cancel_check and cancel_check():
            return 0
        target = max(0, min(5, int(getattr(preset, "map_expansion_monster_count", 1) or 0)))
        if target <= 0:
            return 0
        locs = [l for l in (new_locs or []) if getattr(l, "id", "")]
        if not locs:
            return 0
        made = self._generate_expansion_monsters_llm(world, locs, target, preset, cancel_check)
        if not made:
            made = self._fallback_expansion_monsters(world, locs, target)
        world.monster_pool.extend(made)
        return len(made)

    def _generate_expansion_monsters_llm(self, world: World, locs: list, total: int,
                                         preset: WorldSimPreset,
                                         cancel_check: Optional[Callable[[], bool]]) -> list:
        """调结算 LLM 为新地点生成怪物物种（失败/取消返回 []，走兜底）。"""
        api = self._resolve_api(preset.calculator_api_id or preset.narrative_api_id)
        if not api:
            return []
        tmp_preset = Preset(
            name="world_sim_expand_monsters",
            system_prompt=_EXPAND_MONSTER_SYSTEM_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        genre = ", ".join(getattr(world, "genre_tags", []) or [])
        loc_lines = [f"- {l.name}（{(l.desc or '')[:40]}，危险度{l.danger}）" for l in locs]
        mat_names = [it.name for it in world.items if it.type == "material"][:40]
        existing = [str(m.get("name", "") or "") for m in (getattr(world, "monster_pool", None) or [])]
        user = (
            f"世界题材：{genre or '未知'}\n"
            f"新开辟的地点：\n" + "\n".join(loc_lines) + "\n"
            f"现有材料名（loot 只能引用这些）：{'、'.join(mat_names) if mat_names else '（暂无）'}\n"
            f"现有怪物名（避免重复）：{'、'.join(existing) if existing else '（暂无）'}\n"
            f"请新增 {total} 只怪物物种。"
        )
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user}]
        for _ in range(2):
            if cancel_check and cancel_check():
                return []
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled or result.error or not (result.content or "").strip():
                continue
            data = _extract_json(result.content)
            if isinstance(data, dict) and isinstance(data.get("monsters"), list):
                out = self._sanitize_expansion_monsters(world, data["monsters"], total)
                if out:
                    return out
        return []

    def _sanitize_expansion_monsters(self, world: World, raw: list, limit: int) -> list:
        """清洗 LLM 新怪物定义：danger 区间钳 1-10 + loot 名字 -> item id + 名字去重。"""
        role_wl = {"野兽", "妖兽", "魔物", "强盗", "杀手", "首领"}
        out = []
        existing = {str(m.get("name", "") or "") for m in (getattr(world, "monster_pool", None) or [])}
        item_by_name = {it.name: it for it in world.items}
        for g in raw:
            if len(out) >= limit:
                break
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            if not name or name in existing:
                continue
            dmin = _safe_int(g.get("danger_min"), 1, 1, 10)
            dmax = _safe_int(g.get("danger_max"), dmin, 1, 10)
            if dmin > dmax:
                dmin, dmax = dmax, dmin
            role = str(g.get("role") or "野兽").strip()
            if role not in role_wl:
                role = "野兽"
            loot = []
            for nm in (g.get("loot") or []):
                it = _name_lookup(item_by_name, nm)
                if it is not None and it.id not in loot:
                    loot.append(it.id)
            if not loot:
                tier_pool = we.nearest_rarity_pool(world.items, we.danger_to_tier((dmin + dmax) // 2))
                loot = [it.id for it in tier_pool[:2]]
            out.append({"name": name, "role": role, "desc": str(g.get("desc") or "") or "",
                        "danger_min": dmin, "danger_max": dmax, "loot": loot})
            existing.add(name)
        return out

    def _fallback_expansion_monsters(self, world: World, locs: list, target: int) -> list:
        """LLM 失败时的确定性兜底：题材怪物池挑不在现有池的物种补足（SeededRng 可复现）。"""
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        tmpl = we.GENRE_MONSTER_TEMPLATES.get(tid) or we.GENRE_MONSTER_TEMPLATES["western_fantasy"]
        existing = {str(m.get("name", "") or "") for m in (getattr(world, "monster_pool", None) or [])}
        candidates = [m for m in tmpl if m.get("name") not in existing]
        if not candidates:
            return []
        rng = SeededRng.seed_from(world.id, world.tick_count, "expand_mon")
        out = []
        for m in rng.sample(candidates, min(target, len(candidates))):
            dmid = (int(m["danger_min"]) + int(m["danger_max"])) // 2
            tier_pool = we.nearest_rarity_pool(world.items, we.danger_to_tier(dmid))
            loot = [it.id for it in tier_pool[:2]]
            out.append(dict(m, loot=loot))
        return out

    def ensure_location_backgrounds(self, world: World, loc_ids: list,
                                    preset: Optional[WorldSimPreset],
                                    cancel_check: Optional[Callable[[], bool]] = None) -> int:
        """给指定地点中无背景图的补生成（写回 loc.background）。返回生成张数。"""
        if not self.comfyui or not getattr(preset, "image_enabled", True) \
                or not getattr(preset, "image_location_bg", True):
            return 0
        style = getattr(preset, "image_style_prefix", "") or ""
        made = 0
        for lid in (loc_ids or []):
            if cancel_check and cancel_check():
                break
            loc = next((l for l in world.locations if l.id == lid), None)
            if loc is None or getattr(loc, "background", ""):
                continue
            cn = f"{loc.name}，{loc.desc}，{loc.region or '未知'}区域，场景背景，无人"
            fname = self.generate_image(cn, style, cancel_check=cancel_check, natural=True)
            if fname:
                loc.background = fname
                made += 1
        return made

    def reveal_adjacent(self, world: World, anchor: Optional[Location] = None) -> set:
        """把与指定地点连通（相邻）的未发现地点标为已发现。

        anchor 缺省取玩家当前地点。「你站在隔壁，当然知道那边有个地方」——同时保证
        拓展出的新区域可达可见（拓展时 anchor 应为 from_loc，因迷雾格可挂在非当前地点）。
        返回本次新揭示的 location id 集合。
        """
        cur = anchor if anchor is not None else self._current_location(world)
        if cur is None:
            return set()
        revealed: set = set()
        for cid in cur.connections:
            loc = next((l for l in world.locations if l.id == cid), None)
            if loc is not None and not loc.discovered:
                loc.discovered = True
                revealed.add(loc.id)
        return revealed

    def ensure_bidirectional_connections(self, world: World) -> bool:
        """[P10c] 补全单向连接的反向边（connections 应双向是地图模型不变量）。

        早期版本 build_world_from_skeleton 只回填前向连接（A->B 不补 B->A），老存档因此存在
        不可往返的单向边——玩家从 A 走到 B 后回不去 A（复活/移动后卡死在单向终点）。
        本方法遍历所有连接，缺反向边则补，返回是否有改动（world_scene_tab.load_world 据此落盘）。
        幂等：双向边已是完整的不会重复 append；失效 id（指向已删地点）跳过。
        """
        loc_by_id = {l.id: l for l in world.locations}
        dirty = False
        for loc in world.locations:
            for cid in list(loc.connections):
                other = loc_by_id.get(cid)
                if other is None or other.id == loc.id:
                    continue
                if loc.id not in other.connections:
                    other.connections.append(loc.id)
                    dirty = True
        return dirty

    # ================================================================
    # ============ Phase 4 世界滴答（回合制离屏演化 + 事件日志）============
    # ================================================================
    # 时序：apply_intent 推进 tick_count 后 -> SceneWorker 调 tick_world。
    # 集中入口 tick_world 串联：确定性引擎（经济/势力战/离屏NPC/重生/规则防漂移）
    #   + LLM 要角决策（受 key_npc_budget/sim_budget_per_tick 双重约束）
    #   + LLM 世界校准（每 reconcile_interval 回合，防长期漂移）。
    # 架构铁律（守 §21b 数值范式）：LLM 只出「决策语义」与「校准建议」，
    #   引擎算实际变更 + SeededRng roll + 钳制。LLM 不直接决定最终数值。
    # 失败/取消均有规则兜底，不阻断回合（与 P3 战斗同口径）。

    def _assign_key_npc_initial_level(self, world: World,
                                      preset: Optional[WorldSimPreset] = None) -> int:
        """[要角补档 2026-09-12] 剧情要角（is_key_npc）的初始等级 = 1..npc_max_level 随机。

        多数要角是 combat_role=none 的平民（盟主/族长/长老/任务发布人），按 [NPC 初始等级
        2026-09-12] 的口径只有 1 级——与「要角应该有分量」矛盾。[!] 本函数在
        _init_combat_stats 之前跑，等级定了战斗数值/HP/掉落池才会跟着算。

        标记口径与 _init_world_tick_state 一致（role 关键词 _KEY_NPC_ROLES / quest giver），
        后者用 `or` 累加，故此处先标不冲突。返回本函数抬过级的 NPC 数。
        """
        cap = max(1, min(100, int(getattr(preset, "npc_max_level", 30) or 0))) \
            if preset is not None else 30
        giver_ids = {q.giver_npc_id for q in (getattr(world, "quests", None) or [])
                     if getattr(q, "giver_npc_id", "")}
        raised = 0
        for npc in (getattr(world, "npcs", None) or []):
            role = str(getattr(npc, "role", "") or "")
            if not getattr(npc, "is_key_npc", False) and (
                    npc.id in giver_ids or any(k in role for k in _KEY_NPC_ROLES)):
                npc.is_key_npc = True
            if not getattr(npc, "is_key_npc", False):
                continue
            if int(getattr(npc, "level", 0) or 0) > 1:
                continue          # 已有战斗身份的已随机过，不重复
            rng = SeededRng.seed_from(str(getattr(npc, "id", "") or ""), 0,
                                      "npc_init_level")
            npc.level = int(rng.roll(1, cap))
            raised += 1
        return raised

    def ensure_key_npc_fields(self, world: World, npc, preset=None,
                              fill_profile: bool = True) -> list:
        """[要角补档 2026-09-12] 把「要角有而非要角没有」的字段补上，返回补了哪些字段名。

        存档对比结论（data/worlds 实测，15 NPC / 6 要角）：
          current_goal 要角 6/6、非要角 0/9；hobbies 100% vs 11%；
          notes/relationships/talents 100% vs 44-56%（后三者的非要角多半是商人/战斗单位）。
        - 确定性补齐：talents（world.talent_pool 按 npc.id 种子挑，同 _init_combat_stats 口径）、
          current_goal（用长期 goal 做种子，之后每回合 LLM 要角决策会覆盖）。
        - 叙事三件套 notes/hobbies/relationships 走 generate_npc_profiles（单个 NPC；
          无 API / 失败时静默留空，档案页已有优雅兜底）。
        """
        filled = []
        # 1) talents：与 _init_combat_stats「敌对/要角强补」同口径（种子锚 npc.id 保确定性）
        if not (getattr(npc, "talents", None) or []) and (getattr(world, "talent_pool", None) or []):
            rng = SeededRng.seed_from(str(getattr(world, "id", "") or ""), 0,
                                      f"npc_talent_{getattr(npc, 'id', '')}")
            pick = rng.pick(world.talent_pool)
            if isinstance(pick, dict):
                npc.talents = [dict(pick)]
                filled.append("talents")
        # 2) current_goal：先用长期目标做种子（LLM 要角决策每回合都会刷新它）
        if not str(getattr(npc, "current_goal", "") or "").strip():
            seed = (str(getattr(npc, "goal", "") or "").strip()
                    or str(getattr(npc, "daily_goal", "") or "").strip()
                    or "审时度势，等待下一个机会")
            npc.current_goal = seed
            filled.append("current_goal")
        # 3) 叙事三件套：只补空缺的字段（generate_npc_profiles 内部会跳过已填全的）
        if fill_profile:
            try:
                from src.models.world_sim_preset import WorldSimPreset as _WSP
                before = (str(getattr(npc, "notes", "") or "").strip(),
                          list(getattr(npc, "hobbies", None) or []),
                          list(getattr(npc, "relationships", None) or []))
                self.generate_npc_profiles(world, preset or _WSP(),
                                           only_npc_ids=[str(getattr(npc, "id", "") or "")])
                after = (str(getattr(npc, "notes", "") or "").strip(),
                         list(getattr(npc, "hobbies", None) or []),
                         list(getattr(npc, "relationships", None) or []))
                for name, b, a in zip(("notes", "hobbies", "relationships"), before, after):
                    if not b and a:
                        filled.append(name)
            except Exception:  # noqa: BLE001 - 补档失败不该影响勾选本身
                pass
        return filled

    def _init_world_tick_state(self, world: World, preset: Optional[WorldSimPreset] = None):
        """世界生成后初始化 P4 滴答状态（纯引擎推算，无 LLM）。

        - Faction：power/wealth 据 members + NPC 均等级推算；aggressiveness 据 ideology
          关键词；territory/members 收集；relations 据 ideology 对立初始化。
        - NPC：is_key_npc 据 role 关键词/quest giver 标记；home_location_id=location_id；
          alive=True；last_seen_tick=0。
        - Location：owner_faction_id=faction_id；stability 据 danger 反推。
        - World：last_reconcile_tick=0。
        老世界（P2/P3 存档）缺这些字段时 from_dict 已补默认，本方法仅在新建时强化推算。
        """
        # 势力成员 + 地点归属聚合
        for f in world.factions:
            members = [n.id for n in world.npcs if n.faction_id == f.id]
            f.members = list(dict.fromkeys(members))
            territory = [loc.id for loc in world.locations
                         if (loc.faction_id == f.id or loc.owner_faction_id == f.id)]
            f.territory = list(dict.fromkeys(territory))
            # power/wealth 据 members 均等级 + 领地数推算（仅当仍是默认 50 时覆盖）
            levels = [max(0, int(getattr(n, "level", 0) or 0)) for n in world.npcs if n.faction_id == f.id]
            mean_lvl = (sum(levels) / len(levels)) if levels else 0
            est = _safe_int(40 + mean_lvl * 3 + len(f.territory) * 6, 50, 0, 100)
            if f.power == 50:
                f.power = est
            if f.wealth == 50:
                f.wealth = _safe_int(est + 5, 50, 0, 100)
            # 好战度据 name+ideology+desc 关键词推断（[P] 三源合一，防 LLM 用同义词漏配）
            if f.aggressiveness == 50:
                f.aggressiveness = _faction_aggressiveness(f)

        # 势力关系：据好战度对立初始化（仅当 relations 为空时）。
        # [P 验收] 只在明确的「好战 vs 防守」对立（如 山贼 vs 镇民）给 -50；其余保持 0（中立）。
        # 世界观里的盟友/合作信息由 gen LLM 显式写进 factions.relations（build 解析），
        # 引擎不再凭空给所有势力对造负关系——否则世界书里的「互为表里」派系会被
        # 滴答势力战拉去打盟友（OOC）。默认 0 = 中立不开战。
        fac_list = world.factions
        for i, fa in enumerate(fac_list):
            for fb in fac_list[i + 1:]:
                if fa.relations.get(fb.id, 0) == 0 and fb.relations.get(fa.id, 0) == 0:
                    a_agg = int(fa.aggressiveness or 0)
                    b_agg = int(fb.aggressiveness or 0)
                    if (a_agg >= 70 and b_agg <= 35) or (b_agg >= 70 and a_agg <= 35):
                        fa.relations[fb.id] = -50
                        fb.relations[fa.id] = -50

        # 要角标记：role 关键词 / quest giver（_KEY_NPC_ROLES 已提到模块级，build 期同表）
        giver_ids = {q.giver_npc_id for q in world.quests if q.giver_npc_id}
        for npc in world.npcs:
            if not npc.home_location_id:
                npc.home_location_id = npc.location_id
            npc.alive = True
            npc.last_seen_tick = 0
            role_low = (npc.role or "")
            npc.is_key_npc = bool(
                npc.is_key_npc or npc.id in giver_ids
                or any(k in role_low for k in _KEY_NPC_ROLES)
            )

        # 地点动态归属 + 稳定度
        for loc in world.locations:
            if not loc.owner_faction_id:
                loc.owner_faction_id = loc.faction_id
            if loc.stability == 50:
                loc.stability = _safe_int(100 - loc.danger * 8, 50, 0, 100)

        world.last_reconcile_tick = 0

    # ==================== [P6] 商店与经济系统 ====================
    def _shop_themed_name(self, shop_type: str, gt, merchant_name: str = "") -> str:
        """商店题材化显示名：GenreText.shop_type 优先，回退 _FALLBACK_SHOP_NAME。

        [游商 2026-09-08] merchant_name 保留形参（旧档路径偶传真商人名，新店传空）——
        传空时就是题材店名本身（调用方随后会对齐场所名）。
        """
        base = gt.shop_type(shop_type) if gt is not None else ""
        if not base or base == shop_type:
            base = _FALLBACK_SHOP_NAME.get(shop_type, "杂货铺")
        if merchant_name:
            return f"{merchant_name}{base}"
        return base

    def _init_merchants_and_shops(self, world: World, preset: WorldSimPreset):
        """[P6 -> 游商 2026-09-08] 为各地点聚落商店场所建空商店骨架（纯结构，无 LLM）。

        - [用户指示 2026-09-08] 不再产商人 NPC：NPC 名额不浪费给可写死的交易功能。
          买卖全由商店场所自营的「游商」（假 NPC，只交易，不参与 NPC 逻辑）负责。
        - 建店来源 = 各地点已有的商店类场所（place.type == shop_type，或 LLM 中文类型
          关键词场所），一店一 Place；店名对齐场所名（店名当招牌，游商显示名由
          virtual_vendor_name 按「{店名}·{题材称谓}」派生）。
        - LLM 若生成了经商 role 的 NPC（旧提示词世界/补回），视为普通平民：不标记
          is_merchant、不发开业本金、不建店（其 role 文本不动，仅不参与交易系统）。
        - 商店骨架（空货架）：shop_type/题材名/location/place/restock_interval 写入；
          货架货物由 generate_shops_goods（LLM 备货，失败回退目录选品）后续填充。
        - 幂等（同地点同类店不重复建，旧档已落商人店不动）+ shops_enabled 关 -> 跳过。
        """
        if not self._per_world(world, "shops_enabled", preset.shops_enabled, preset):
            return
        # 旧档已绑商人的店：保留 merchant_npc_id（开局只跑一次，重跑 build 不碰旧档）。
        # [!] getattr 兜底：拓展/测试桩 world 可能是 SimpleNamespace（无 shops/locations）。
        existing_types = {(str(getattr(s, "location_id", "") or ""),
                           str(getattr(s, "shop_type", "") or ""))
                          for s in (getattr(world, "shops", None) or [])}
        interval = int(self._per_world(world, "shops_restock_interval",
                                        preset.shops_restock_interval, preset)) or 5
        loc_by_id = {loc.id: loc for loc in (getattr(world, "locations", None) or [])}
        for loc in (getattr(world, "locations", None) or []):
            for place in (getattr(loc, "places", None) or []):
                st = str(getattr(place, "type", "") or "").strip()
                if st not in self._VENUE_SHOP_TYPES:
                    # 二级：LLM 中文类型关键词场所（铁匠铺/药铺…）
                    kws_hit = ""
                    for cand_st, kws in self._VENUE_TYPE_KEYWORDS.items():
                        if any(k in (place.name or "") for k in kws):
                            kws_hit = cand_st
                            break
                    st = kws_hit
                if not st or (loc.id, st) in existing_types:
                    continue
                shop = Shop(
                    merchant_npc_id="", location_id=loc.id, place_id=place.id,
                    shop_type=st, restock_interval=interval, last_restock_tick=0,
                    name=self._shop_themed_name(st, self._genre_text(world), ""),
                )
                if str(getattr(shop, "name", "") or "") != place.name:
                    shop.name = place.name
                try:
                    world.shops.append(shop)
                except AttributeError:
                    world.shops = [shop]   # 桩 world.shops 缺失/不可写时建列表
                existing_types.add((loc.id, st))

    def heal_missing_vendor_shops(self, world: World, preset: WorldSimPreset) -> bool:
        """[修 2026-09-13] 建店缺口自愈（旧档进世界调用）：有商店类场所但零店的地点补建游商店。

        背景：build 期 venue 解析曾晚于 _init_merchants_and_shops（2026-09-13 已改单次收口），
        该窗口生成的旧档 town「有门面零店」（如铁锚镇 weapon/consumable/material 三门面 0 店），
        场景页看不到游商。本方法重跑 _init_merchants_and_shops（幂等 (loc, type) 去重，已绑
        真商人的店不动）并给**新建**的空架店即时目录备货（_seed_shop_from_catalog，零 LLM，
        进世界路径同步调用不阻塞；空架不备会「暂无商品」，tick 补货远水不解近渴）。
        返回是否有变更（调用方据此落盘）。
        """
        before = {(str(getattr(s, "location_id", "") or ""),
                   str(getattr(s, "shop_type", "") or ""))
                  for s in (getattr(world, "shops", None) or [])}
        self._init_merchants_and_shops(world, preset)
        created = [s for s in (getattr(world, "shops", None) or [])
                   if (str(getattr(s, "location_id", "") or ""),
                       str(getattr(s, "shop_type", "") or "")) not in before]
        if not created:
            return False
        rng = SeededRng.seed_from(world.id, int(world.tick_count or 0), "vendor_heal")
        tick = int(world.tick_count or 0)
        for s in created:
            if not s.stock:
                self._seed_shop_from_catalog(s, world, rng, preset)
                s.last_restock_tick = tick
                s.touch()
        return True

    def _ensure_generation_coherence(self, world: World,
                                     preset: Optional[WorldSimPreset] = None) -> bool:
        """[修 2026-09-13 用户指示] 世界生成一致性收口（build 收尾 / 拓展收尾 / 进世界自愈共用）。

        世界骨架（地点 danger）与怪物池/任务由多次独立 LLM 调用产出，互相不引用——真机档
        实锤四类断供：怪物 danger 区间与所有野外地无交集（漂流恶犬 1-2 vs 野外 4-9 -> 永不
        刷新，其掉落物与引用它的任务全断）；聚落 danger 被填了非 0 值；kill 目标 target 空
        串（名只写在 desc）；gather 目标 type 与全世界资源点无一匹配（任务让你采的东西
        不存在——鲨鱼皮 vs 节点全是不存在 类型 金属/遗物/矿物）。本方法硬保证五个不变量：
        1) 安全区：聚落 danger 一律 0（不信任 LLM 填值；刷怪本就只认 kind=wilderness，
           这里把「安全」落到数据层，防危险度驱动的逻辑把城市当风险区）。
        2) 零野外兜底：LLM 可能全出聚落 -> 把 danger 最高的聚落降级 wilderness（并列取
           (danger, id) 最大者，确定性），保住打怪/采集阵地；降级后 danger<=0 给中等档 4。
        3) 怪物池对齐：danger 区间与所有野外地无交集的怪 -> 平移到最近野外档（保持区间
           宽度，new_lo=最近档）。覆盖判定只认 kind=wilderness 的地点。
        4) kill 目标回填：target 空串时从 desc/objective 文本反查怪物池名/敌对 NPC 名
           （长名优先防子串误命中，desc 优先于总述），查到才回填，查不到不虚构。
        5) gather 目标保底：目标 type 与全部资源点 type 无一匹配（含子串双向）-> 在
           发布者所在地相邻的最低危野外地补挂同名资源点（type=目标名，tier 按地 danger，
           drops 按档就近取材料），保「要采的东西真有得采」。
        6) 悬空物品引用清扫（[修 2026-09-14] 旧顺序遗产兜底：功能性物品收编曾晚于
           名字 -> id 解析，种子重写前的旧 uuid 留在配方 inputs 里）：配方 input 悬空
           按 desc 反查回填或丢弃、output 悬空整条移除；NPC 背包/掉落表、怪物 loot、
           任务奖励、商店货架的悬空 id 条目一律移除（详见 _heal_dangling_item_refs）。
        7) [修 2026-10-02 真机 P2] 前提遗赠落地：前提宣称「交给玩家」的物品（真名同句
           命中 / 唯一传承武器名字错位兜底）入玩家背包 + 货架下架孤品（详见
           _land_premise_gifts）——修「旁白写你背着步枪、数据却两手空空且遗枪在售」。
        8) [修 2026-10-02 用户指示] 新手野地保底：出生地相邻野地至少一个 danger=1
           （生成端危险度梯度软约束下 LLM 常从 2 起步，1 级区从未出现）。
        9) [修 2026-10-02 用户拍板 3A] 开局治疗保底：出生地药店开局保底 3 件最便宜
           低品级治疗品（详见 _ensure_heal_supply）——修「开局败北后全图买不到药」。
        返回是否有变更（调用方据此落盘）。
        """
        changed = False
        locs = list(getattr(world, "locations", None) or [])
        # 1)+2) 零野外兜底（须在聚落归零前做——降级对象按 LLM 原始 danger 挑）
        wilds = [l for l in locs if str(getattr(l, "kind", "") or "") == "wilderness"]
        if not wilds and locs:
            # 并列时取 (danger, id) 最大者（max 语义，确定性）
            best = max(locs, key=lambda l: (int(getattr(l, "danger", 0) or 0), str(getattr(l, "id", ""))))
            best.kind = "wilderness"
            if int(getattr(best, "danger", 0) or 0) <= 0:
                best.danger = 4
            changed = True
        for l in locs:
            if (str(getattr(l, "kind", "") or "") == "settlement"
                    and int(getattr(l, "danger", 0) or 0) != 0):
                l.danger = 0
                # [审 2026-09-13] stability 与 danger 同口径（100 - danger*8）：归零时把
                # build 期按 LLM 旧 danger 算出的残留稳定度一并拉满 100；只在归零当次改，
                # 不覆盖游玩期被事件演化的稳定度（二次调用 danger 已 0 跳过）。
                l.stability = 100
                changed = True
        # 2b) [修 2026-10-02 用户指示·新手野地保底] 出生地相邻至少一个 danger=1 野地：
        # 生成提示词对危险度只有软约束（低危 1-3），真机两档（净水残响/大明江湖录）
        # LLM 都把梯度起点放在 2——1 级新手野地从未出现。选点=出生地相邻野地中
        # danger 最低者降为 1（新手出门第一站就是 1 级区）；无出生点锚（合成世界/
        # 脏档）或已有 danger<=1 野地则不动。降档后怪物池对齐（不变量 3）随之生效。
        if not any(str(getattr(l, "kind", "") or "") == "wilderness"
                   and int(getattr(l, "danger", 0) or 0) <= 1 for l in locs):
            _pl = getattr(world, "player", None)
            _spawn_id = str(getattr(_pl, "spawn_location_id", "") or
                            getattr(_pl, "location_id", "") or "")
            _spawn = next((l for l in locs if str(l.id) == _spawn_id), None)
            if _spawn is not None:
                _cons = set(getattr(_spawn, "connections", None) or [])
                _cands = [l for l in locs
                          if str(getattr(l, "kind", "") or "") == "wilderness"
                          and str(l.id) in _cons]
                if _cands:
                    host = min(_cands, key=lambda l: (int(getattr(l, "danger", 0) or 0),
                                                      str(l.id)))
                    host.danger = 1
                    changed = True
                    debug_log(lambda hn=str(getattr(host, "name", "")):
                              f"[一致性收口] 新手野地保底：{hn} 危险度降为 1")
        wild_dangers = sorted({int(getattr(l, "danger", 0) or 0) for l in locs
                               if str(getattr(l, "kind", "") or "") == "wilderness"})
        # 3) 怪物池 danger 对齐
        for m in (getattr(world, "monster_pool", None) or []):
            if not isinstance(m, dict):
                continue
            try:
                lo = max(1, int(m.get("danger_min", 1) or 1))
                hi = max(lo, int(m.get("danger_max", lo) or lo))
            except (TypeError, ValueError):
                continue
            if not wild_dangers or any(lo <= d <= hi for d in wild_dangers):
                continue
            # 最近野外档：与区间的距离最小者（并列取更小档），保持区间宽度平移
            d_star = min(wild_dangers, key=lambda d: (max(0, lo - d, d - hi), d))
            new_lo, new_hi = d_star, min(10, d_star + (hi - lo))
            if m.get("danger_min") != new_lo or m.get("danger_max") != new_hi:
                m["danger_min"], m["danger_max"] = new_lo, new_hi
                changed = True
        # 4) kill 目标空串回填
        cand_names = [str(m.get("name") or "") for m in (getattr(world, "monster_pool", None) or [])
                      if isinstance(m, dict) and m.get("name")]
        cand_names += [str(getattr(n, "name", "") or "") for n in (getattr(world, "npcs", None) or [])
                       if getattr(n, "name", "") and getattr(n, "hostile", False)]
        cand_names.sort(key=len, reverse=True)   # 长名优先（防「恶犬」先于「漂流恶犬」命中）
        for q in (getattr(world, "quests", None) or []):
            for o in (getattr(q, "objectives", None) or []):
                if not isinstance(o, dict) or str(o.get("type") or "") != "kill":
                    continue
                # [修 2026-10-01] 非空目标先锚定怪物池：名字优先，同职唯一才允许 role 回写。
                # [修 2026-10-02 审查] 锚不中且非真实 NPC 名 -> desc 反查回填真实怪名；
                # 仍查不到**原样保留**（既有契约「非空不动」：test_generation_coherence
                # 锁定；虚构名兜底在 build 端怪物池锚定+清空，旧档残留接受）。
                from src.services import name_resolver as _nrs_kill
                cur = str(o.get("target") or "").strip()
                anchored = False
                if cur:
                    mons = [m for m in (getattr(world, "monster_pool", None) or [])
                            if isinstance(m, dict) and m.get("name")]
                    by_name = [m for m in mons if _nrs_kill.names_match(cur, str(m.get("name") or ""))]
                    by_role = [m for m in mons if _nrs_kill.names_match(cur, str(m.get("role") or ""))]
                    mon_hit = by_name[0] if by_name else (by_role[0] if len(by_role) == 1 else None)
                    if mon_hit is not None:
                        if o.get("target") != mon_hit.get("name"):
                            o["target"] = str(mon_hit.get("name"))
                            changed = True
                        anchored = True
                    elif _nrs_kill.resolve_name(cur, [str(getattr(n, "name", "") or "")
                                                      for n in (getattr(world, "npcs", None) or [])]):
                        anchored = True   # 真实 NPC 名（kill 目标可以是人）：原样保留
                if anchored:
                    continue
                # 空/锚不中的 target：desc 优先反查真实怪名（长名优先防子串误命中），
                # 回退任务总述；查不到保持原值（非空）/留空（空）。
                desc_t = str(o.get("desc") or "")
                obj_t = str(getattr(q, "objective", "") or "")
                hit = next((nm for nm in cand_names if nm and nm in desc_t), None)
                if hit is None and obj_t:
                    hit = next((nm for nm in cand_names if nm and nm in obj_t), None)
                if hit is not None:
                    o["target"] = hit
                    changed = True
        #    金属/遗物/矿物——目标类型与资源点各写各的，永远 0/N）。
        node_types = [str(getattr(rn, "type", "") or "")
                      for l in locs for rn in (getattr(l, "resource_nodes", None) or [])]
        # [修 2026-10-02 整体对齐] 节点掉落物品名集合：物品真名目标（锚定产物/LLM 直写）
        # 命中任一节点掉落即已有采集渠道，不得再走补挂（否则每次自愈多挂一个错位节点
        # ——资源点膨胀 + 叙述地点外的假渠道）。
        _item_by_id_g = {str(getattr(it, "id", "") or ""): it
                         for it in (getattr(world, "items", None) or [])}
        node_drop_names = [str(getattr(_item_by_id_g.get(str(d0.get("item_id") or "")), "name", "") or "")
                           for l in locs for rn in (getattr(l, "resource_nodes", None) or [])
                           for d0 in (getattr(rn, "drops", None) or [])
                           if isinstance(d0, dict)]
        for q in (getattr(world, "quests", None) or []):
            for o in (getattr(q, "objectives", None) or []):
                if not isinstance(o, dict) or str(o.get("type") or "") != "gather":
                    continue
                t = str(o.get("target") or "").strip()
                if not t:
                    continue
                # [修 2026-10-02 审查] 物品池真名不做锚定：buy/拾取事件按物品名计数、
                # 采集事件 aliases 带掉落名——三通道本来都通（真机档 target='白芷' 被
                # 锚成节点 type='兽皮'，买药通道被砍即此回归）。锚定只救池外统称
                # （「草药」这类）。锚定顺序（用户指示 2026-10-02「整体要对，不然 OOC」）：
                # ① 叙述里点名的地点优先——锚该地点节点的 material 掉落真名（本地采集
                #    别名命中 + 购买双通道；无掉落退化 node.type 保本地采集）。否则任务
                #    让你在 A 地采、引擎只在 B 地计数（寒潭采药去断魂崖才动的地理 OOC）。
                # ② 无地点线索才全图扫（节点 name/type/掉落名容错命中 -> 该节点 type）。
                if t not in {str(getattr(it, "name", "") or "")
                             for it in (getattr(world, "items", None) or [])}:
                    from src.services import name_resolver as _nrs_g
                    item_by_id = {str(getattr(it, "id", "") or ""): it
                                  for it in (getattr(world, "items", None) or [])}
                    anchored = None
                    _narr = f"{o.get('desc') or ''} {getattr(q, 'objective', '') or ''}"
                    _hosts = [l for l in locs
                              if l.name and l.name in _narr
                              and (getattr(l, "resource_nodes", None) or [])]
                    if _hosts:
                        # 叙述里出现最早的有资源地点（多地点时贴叙述主场景）
                        host = min(_hosts, key=lambda l: _narr.index(l.name))
                        rn0 = host.resource_nodes[0]
                        _drop = next(
                            (item_by_id[str(d0.get("item_id") or "")]
                             for d0 in (getattr(rn0, "drops", None) or [])
                             if isinstance(d0, dict)
                             and item_by_id.get(str(d0.get("item_id") or ""))
                             and getattr(item_by_id.get(str(d0.get("item_id") or "")), "type", "") == "material"),
                            None)
                        anchored = (str(getattr(_drop, "name", "") or "")
                                    if _drop is not None
                                    else (str(getattr(rn0, "type", "") or "") or t))
                    else:
                        for loc0 in locs:
                            for rn0 in (getattr(loc0, "resource_nodes", None) or []):
                                labels = [str(getattr(rn0, "type", "") or ""),
                                          str(getattr(rn0, "name", "") or "")]
                                for d0 in (getattr(rn0, "drops", None) or []):
                                    iid = d0.get("item_id") if isinstance(d0, dict) else ""
                                    it0 = item_by_id.get(str(iid or ""))
                                    if it0 is not None:
                                        labels.append(str(getattr(it0, "name", "") or ""))
                                if any(lb and _nrs_g.names_match(t, lb) for lb in labels):
                                    anchored = str(getattr(rn0, "type", "") or "") or t
                                    break
                            if anchored:
                                break
                    if anchored and o.get("target") != anchored:
                        o["target"] = anchored
                        t = anchored
                        changed = True
                # 已有资源点匹配（与 gather 进度钩子的宽松口径同向：双向子串；物品真名
                # 另认节点掉落渠道——见上方 node_drop_names 注释）
                if (any(nt and (t == nt or t in nt or nt in t) for nt in node_types)
                        or any(dn and (t == dn or t in dn or dn in t) for dn in node_drop_names)):
                    continue
                # 挂点：发布者所在地的相邻野外地优先（就地可采），退化取最低危野外地
                gid = str(getattr(q, "giver_npc_id", "") or "")
                giver = next((n for n in (getattr(world, "npcs", None) or [])
                              if str(getattr(n, "id", "") or "") == gid), None)
                anchor = None
                if giver is not None:
                    anchor = next((l for l in locs
                                   if str(getattr(l, "id", "") or "") == str(getattr(giver, "location_id", "") or "")), None)
                host = None
                cand = []
                if anchor is not None:
                    cand = [l for l in locs if l.id in (getattr(anchor, "connections", None) or [])
                            and str(getattr(l, "kind", "") or "") == "wilderness"]
                if not cand:
                    cand = [l for l in locs if str(getattr(l, "kind", "") or "") == "wilderness"]
                if cand:
                    host = min(cand, key=lambda l: (int(getattr(l, "danger", 0) or 0), str(l.id)))
                if host is None:
                    debug_log(lambda t=t: f"[一致性收口] gather 目标「{t}」无野外地可挂，跳过")
                    continue
                tier = we.danger_to_tier(int(getattr(host, "danger", 1) or 1))
                rng = SeededRng.seed_from(str(getattr(world, "id", "")), 0, f"qgather_{t}")
                pool = we.nearest_rarity_pool(getattr(world, "items", None) or [], tier)
                picks = rng.sample(pool, 2) if pool else []
                qty = 2 if tier >= 3 else 1
                host.resource_nodes.append(ResourceNode(
                    name=t, type=t,
                    desc=f"任务相关：可在此采集{t}。",
                    tier=tier,
                    drops=[{"item_id": it.id, "qty": qty, "rate": 0.4} for it in picks],
                    richness=100, richness_max=100,
                    cooldown_tick=0,
                    cooldown_duration=max(1, int(getattr(preset, "resource_cooldown_ticks", 3)
                                                  if preset is not None else 3)),
                    regenerates=True, requires_tool="", stat_used="dex",
                ))
                node_types.append(t)
                changed = True
                debug_log(lambda t=t, hn=str(getattr(host, "name", "")):
                          f"[一致性收口] gather 目标「{t}」无匹配资源点，已补挂于 {hn}")
        changed = self._heal_dangling_item_refs(world) or changed
        changed = self._land_premise_gifts(world) or changed
        changed = self._ensure_heal_supply(world) or changed
        return changed

    def _ensure_heal_supply(self, world: World) -> bool:
        """[修 2026-10-02 用户拍板 3A·开局治疗保底] 出生地开局保底 3 件最便宜治疗品。

        审计实锤（净水残响档）：22 种治疗消耗品开局全图零直售（渠道要等商店补货
        周期/配方链/任务奖励），叠加「败北→1 血→没药→只能干等自然恢复」的开局
        死循环；中后期补货会自然铺开，本收口只兜「出生地开局有药」。镜像
        _ensure_food_supply 先例：选店=出生地 alchemy/consumable/general（类型偏好，
        出生地无店回退全局同型店）；上架=最便宜 3 件低品级治疗品（rarity<=rare 守
        「高品不上店」口径），幂等（货架已有同 id 跳过）。price 走 compute_item_price
        公式价（首周期后由价格漂移接管，与既有货架同口径）。
        """
        p = getattr(world, "player", None)
        if p is None:
            return False
        heals = [i for i in (getattr(world, "items", None) or [])
                 if str(getattr(i, "type", "") or "") == "consumable"
                 and (int(getattr(i, "heal_amount", 0) or 0) > 0
                      or int(getattr(i, "heal_pct", 0) or 0) > 0)
                 and str(getattr(i, "rarity", "") or "") in ("common", "uncommon", "rare")]
        if not heals:
            return False
        heals.sort(key=lambda i: (tre.compute_item_price(i), str(i.id)))
        picks = heals[:3]
        spawn_id = str(getattr(p, "spawn_location_id", "") or
                       getattr(p, "location_id", "") or "")
        shops = [s for s in (getattr(world, "shops", None) or [])]
        if not shops:
            return False
        _TYPE_PREF = ("alchemy", "consumable", "general")

        def _rank(s):
            at_spawn = str(getattr(s, "location_id", "") or "") == spawn_id
            st = str(getattr(s, "shop_type", "") or "")
            return (0 if at_spawn and st in _TYPE_PREF else
                    1 if at_spawn else
                    2 if st in _TYPE_PREF else 3,
                    _TYPE_PREF.index(st) if st in _TYPE_PREF else 9,
                    str(getattr(s, "id", "") or ""))

        shop = min(shops, key=_rank)
        dirty = False
        for it in picks:
            if any(str(getattr(e, "item_id", "") or "") == it.id
                   for e in (getattr(shop, "stock", None) or [])):
                continue
            from src.models.shop import ShopStockEntry
            try:
                price = max(1, int(tre.compute_item_price(it)))
            except Exception:
                price = 10
            shop.stock.append(ShopStockEntry(item_id=it.id, price=price,
                                             stock=3, max_stock=5))
            shop.touch()
            dirty = True
            debug_log(lambda n=it.name, sn=str(getattr(shop, "name", "")):
                      f"[一致性收口] 开局治疗保底：{sn} 上架 {n}")
        return dirty

    def _land_premise_gifts(self, world: World) -> bool:
        """[修 2026-10-02 真机 P2·前提道具未落地] 不变量 7：前提宣称「交给你」的
        遗赠真落到玩家背包（且从货架下架同件）。

        背景（净水残响档）：前提与开场旁白反复写「长老把最后一支步枪交给你/你背上的
        枪」，引擎侧开局背包空、装备 0/8，而 LLM 据前提造的「长老的遗枪」(atk22) 挂在
        军械库/拍卖行出售——叙事宣称与数据相反，玩家也赤手空拳吃难度倒挂的亏。

        匹配两级（都要给与语义，防「找回净水芯片」这类任务目标被误发）：
        A. 物品池真名出现在前提里**且**所在句含给与动词（交给你/留给你/传给你/赠你/给你）；
        B. 前提含「交给/留给/传给你」+ 武器词（枪/炮/刀/剑/弓），且池中**唯一**一件
           rare+ 武器的 desc 带传承标记（遗/传下/历任/传承）——覆盖「前提写步枪、
           物品叫长老的遗枪」的名字错位（真机实案）。
        发放：玩家背包 append 一件（生成期授予，不走上限）+ 全商店货架/拍卖下架同 id
        （「最后一支」的孤品不能再满街卖）。幂等：已在背包或已装备则跳过。上限 3 件。
        """
        import re as _re
        premise = str(getattr(world, "premise", "") or "")
        if not premise:
            return False
        has_give = bool(_re.search(r"(交|留|传|赠|递|托付)给(你|玩家)", premise))
        if not has_give:
            return False
        items = list(getattr(world, "items", None) or [])
        if not items:
            return False

        def _sentences(text: str) -> list:
            # 分句到「子句」粒度（含逗号）：前提常是一整个长句，粗粒度会让
            # 「交给你」与隔壁子句的任务目标（找回净水芯片）同句误发（真机案）。
            return [s for s in _re.split(r"[。！？；\n，,]", text) if s.strip()]

        gift_ids: list[str] = []
        # A. 真名 + 同句给与动词
        for it in items:
            nm = str(getattr(it, "name", "") or "").strip()
            if not nm or len(nm) < 2 or it.id in gift_ids:
                continue
            for s in _sentences(premise):
                if nm in s and _re.search(r"(交|留|传|赠|递|托付)给(你|玩家)", s):
                    gift_ids.append(it.id)
                    break
        # B. 武器词 + 唯一传承武器（名字错位兜底）
        # [标记载紧 2026-10-02] 裸「遗」会误中「防暴队的遗物」（电击警棍真机案）——
        # 传承标记须带交接语义（传下/历任/传承/祖传/遗赠/托付）。
        if not gift_ids and _re.search(r"枪|炮|刀|剑|弓|武器", premise):
            heirs = [it for it in items
                     if str(getattr(it, "type", "") or "") == "weapon"
                     and str(getattr(it, "rarity", "") or "") in ("rare", "epic",
                                                                 "legendary", "mythic")
                     and _re.search(r"传下|历任|传承|祖传|遗赠|托付",
                                    str(getattr(it, "desc", "") or "")
                                    + str(getattr(it, "name", "") or ""))]
            if len(heirs) == 1:
                gift_ids.append(heirs[0].id)
        if not gift_ids:
            return False
        gift_ids = gift_ids[:3]
        p = getattr(world, "player", None)
        if p is None:
            return False
        inv = getattr(p, "inventory", None)
        equipped = {v for v in (getattr(p, "equipped", None) or {}).values() if v}
        changed = False
        for iid in gift_ids:
            if iid in equipped or (inv is not None and iid in inv):
                continue    # 幂等：已持有不重复发放
            if inv is not None:
                inv.append(iid)
                changed = True
                debug_log(lambda i=iid: f"[一致性收口] 前提遗赠落地入包: {i}")
            # 孤品下架：全店货架/拍卖移除同 id（「最后一支」不能满街卖）
            for sh in (getattr(world, "shops", None) or []):
                before = len(getattr(sh, "stock", None) or [])
                sh.stock = [e for e in (getattr(sh, "stock", None) or [])
                            if str(getattr(e, "item_id", "") or "") != iid]
                if len(sh.stock) != before:
                    sh.touch()
                    changed = True
        return changed

    def _heal_dangling_item_refs(self, world: World) -> bool:
        """[修 2026-09-14] 悬空物品引用自愈（挂 _ensure_generation_coherence 不变量 6，
        build 尾 / 拓展尾 / 旧档进世界三挂点共用）。

        成因：功能性物品收编把种子物品 id 原地重写为确定性 seed_id_of(题材,名)，旧顺序
        里重写晚于 LLM 配方的名字 -> id 解析——先解析出的旧 uuid 在重写后悬空（真机档
        「海藻糊糊/抗生素药片/再生药膏」三条药方各引用一个失效孢子 id，永缺一件料）。
        新档已把收编前置根治；本方法兜底既有档：

        - 配方 output 悬空 -> 整条移除（产出是配方存在意义，无替代）。
        - 配方 input 悬空 -> 配方 desc/name 文本反查物品名回填（长名优先；排除产出名
          与仍有效 input 的名——兜底配方 desc 是「用X、Y炼制Z」句式，产出/有效料名
          再现会误配回自己）；反查不到丢弃该 input；inputs 因此清空则整条移除
          （空 inputs = 免费合成，绝不能留）。
        - NPC inventory/loot_table、怪物 loot、任务 rewards.items、商店 stock 的悬空
          id 条目一律移除（查无此物的条目在任何 UI/引擎路径都只会静默失败）。

        确定性（零 rng，不碰任何 SeededRng 序列），幂等（清扫后无悬空，再跑无变更）。
        返回是否有变更。
        """
        all_items = list(getattr(world, "items", None) or [])
        item_ids = {str(getattr(it, "id", "") or "") for it in all_items}
        if not item_ids:
            return False
        by_id = {str(getattr(it, "id", "") or ""): it for it in all_items}
        # [!] 同名取后出现者：与 build 的 item_by_name（dict 推导，后者覆盖）同口径——
        # 收编把同名作物 append 在种子之后，配方的「速生海藻」应解析到作物（作物=
        # craft 材料可烧，种子留给播种，引擎配方本就排除 seed 类当材料）。
        by_name = {str(getattr(it, "name", "") or ""): it
                   for it in all_items if str(getattr(it, "name", "") or "")}
        changed = False
        kept_recipes = []
        for r in (getattr(world, "recipes", None) or []):
            ins = [str(i) for i in (getattr(r, "inputs", None) or [])]
            out_id = str(getattr(r, "output_item_id", "") or "")
            if out_id not in item_ids:
                changed = True
                debug_log(lambda rn=str(getattr(r, "name", "")):
                          f"[一致性收口] 配方「{rn}」产出物品悬空，整条移除")
                continue
            valid = [i for i in ins if i in item_ids]
            dangling = [i for i in ins if i not in item_ids]
            if not dangling:
                kept_recipes.append(r)
                continue
            # 反查候选：desc 优先（材料叙述在 desc），name 兜底；长名优先防子串误命中
            excluded = {str(getattr(by_id.get(i), "name", "") or "") for i in valid}
            excluded.add(str(getattr(by_id.get(out_id), "name", "") or ""))
            cands: list = []
            for txt in (str(getattr(r, "desc", "") or ""), str(getattr(r, "name", "") or "")):
                hits = [nm for nm in by_name if nm not in excluded and nm not in cands and nm in txt]
                hits.sort(key=len, reverse=True)
                cands.extend(hits)
            for _ in dangling:
                if cands:
                    cand = cands.pop(0)
                    if by_name[cand].id not in valid:
                        valid.append(by_name[cand].id)
                changed = True   # 回填或丢弃，都算变更
            if valid:
                r.inputs = valid
                kept_recipes.append(r)
                debug_log(lambda rn=str(getattr(r, "name", "")):
                          f"[一致性收口] 配方「{rn}」悬空材料已按描述回填/剔除")
            else:
                debug_log(lambda rn=str(getattr(r, "name", "")):
                          f"[一致性收口] 配方「{rn}」材料全失效，整条移除")
        if changed:
            world.recipes = kept_recipes
        # NPC 背包 / 掉落表（loot_table 条目 dict(id=..)/纯 id 两形态与既有读取口径一致）
        for n in (getattr(world, "npcs", None) or []):
            inv = getattr(n, "inventory", None)
            if inv:
                keep = [i for i in inv if str(i) in item_ids]
                if len(keep) != len(inv):
                    n.inventory = keep
                    changed = True
            lt = getattr(n, "loot_table", None)
            if lt:
                keep = [dd for dd in lt
                        if (str(dd.get("id") or "") if isinstance(dd, dict) else str(dd)) in item_ids]
                if len(keep) != len(lt):
                    n.loot_table = keep
                    changed = True
        for m in (getattr(world, "monster_pool", None) or []):
            if isinstance(m, dict) and isinstance(m.get("loot"), list):
                keep = [i for i in m["loot"] if str(i) in item_ids]
                if len(keep) != len(m["loot"]):
                    m["loot"] = keep
                    changed = True
        for q in (getattr(world, "quests", None) or []):
            rw = getattr(q, "rewards", None)
            if isinstance(rw, dict) and rw.get("items"):
                keep = [i for i in (rw.get("items") or []) if str(i) in item_ids]
                if len(keep) != len(rw.get("items") or []):
                    rw["items"] = keep
                    changed = True
        for s in (getattr(world, "shops", None) or []):
            stock = getattr(s, "stock", None)
            if stock:
                keep = [e for e in stock
                        if str(getattr(e, "item_id", "") or "") in item_ids]
                if len(keep) != len(stock):
                    s.stock = keep
                    s.touch()
                    changed = True
        return changed

    def ensure_quest_material_sources(self, world: World,
                                      preset: Optional[WorldSimPreset] = None) -> list:
        """[修 2026-09-13 用户指示] 任务 collect 材料可得性收口：目标材料若三条渠道全无
        （商店货架 / 资源点产出 / 怪物+NPC 掉落 / 配方产出），保底上架到出生地材料行/
        杂货铺（stock = max(3, 任务需求量)，防「任务要 5 件只上 3 件」）。

        任务生成只校验「物品名在池里」，不校验获取渠道——真机档漂流木板全渠道断供。
        新档在 generate_shops_goods 之后调用（不阻塞 LLM 差异化备货）；旧档进世界自愈 /
        拓展收尾调用。物品池查无此名时不虚构（提示词已要求真实物品名），仅记 debug。
        返回保底上架的物品名列表。
        """
        items = {it.id: it for it in (getattr(world, "items", None) or [])}
        by_name = {}
        for it in items.values():
            by_name.setdefault(str(it.name or ""), it)
        need = {}
        need_types: dict[str, set] = {}
        for q in (getattr(world, "quests", None) or []):
            for o in (getattr(q, "objectives", None) or []):
                # [修 2026-09-30] deliver_items 同 collect 口径——无渠道的任务不可完成
                # [修 2026-10-01 用户报·主线卡死] gather 纳入收口：真机档 gather:止血草×5
                # 全世界仅 1 株在 NPC 身上（无店/无资源点/无掉落），凑不齐永卡主线。
                if not isinstance(o, dict) or str(o.get("type") or "") not in (
                        "gather", "collect", "deliver_items"):
                    continue
                t = str(o.get("target") or "").strip()
                if not t:
                    continue
                it = by_name.get(t)
                if it is None:
                    cands = [v for k, v in by_name.items() if t in k or k in t]
                    it = cands[0] if len(cands) == 1 else None
                if it is None and str(o.get("type") or "") in ("collect", "deliver_items"):
                    # [修 2026-10-02 真机 P2·类型名冒充物品名] collect/deliver 目标写成
                    # 资源点类型名（净水残响档主线「任务物品」——LLM 照抄了类型清单字样），
                    # 而 desc/总目标里点名了物品池真名（「取回净水芯片」）-> 锚回真名：
                    # 任务日志不再显示「任务物品 0/1」，玩家也能真拿到任务物品本体
                    # （锚后无渠道由下方保底逻辑兜住）。最长命中优先（防短名误中）。
                    _hay = f"{o.get('desc') or ''} {getattr(q, 'objective', '') or ''}"
                    _hits = sorted((k for k in by_name if k and k in _hay),
                                   key=len, reverse=True)
                    if _hits:
                        o["target"] = _hits[0]
                        it = by_name[_hits[0]]
                        debug_log(lambda a=_hits[0], b=t: (
                            f"[一致性收口] 收集目标「{b}」锚回真名「{a}」"))
                if it is None:
                    debug_log(lambda t=t: f"[一致性收口] 收集类目标「{t}」不在物品池，跳过")
                    continue
                need[it.id] = max(need.get(it.id, 0), max(1, int(o.get("count", 1) or 1)))
                need_types.setdefault(it.id, set()).add(str(o.get("type")))
        if not need:
            return []
        reachable = set()
        for s in (getattr(world, "shops", None) or []):
            for e in (getattr(s, "stock", None) or []):
                reachable.add(str(getattr(e, "item_id", "") or ""))
        for l in (getattr(world, "locations", None) or []):
            for rn in (getattr(l, "resource_nodes", None) or []):
                for dd in (getattr(rn, "drops", None) or []):
                    reachable.add(str(dd.get("item_id") or ""))
        for m in (getattr(world, "monster_pool", None) or []):
            if isinstance(m, dict):
                for dd in (m.get("loot") or []):
                    reachable.add(str(dd.get("item_id") or "") if isinstance(dd, dict) else str(dd))
        for n in (getattr(world, "npcs", None) or []):
            for dd in (getattr(n, "loot_table", None) or []):
                reachable.add(str(dd.get("id") or "") if isinstance(dd, dict) else str(dd))
        for r in (getattr(world, "recipes", None) or []):
            reachable.add(str(getattr(r, "output_item_id", "") or ""))
        missing = sorted((iid for iid in need if iid not in reachable),
                         key=lambda x: str(items[x].name or x))
        if not missing:
            return []
        # [审 2026-09-13] 品级分渠：legendary/mythic 任务材料绝不上店货架（守「游商最高
        # 蓝/epic 至多 1」契约），改挂野外地资源点掉落（肝系渠道，与「高级货只走掉落/
        # 拍卖」经济口径一致）；epic 及以下走店铺保底。资源点全无时金材料回退店铺
        # （任务可完成性优先于货架观感，此情形理论罕见）。
        # [修 2026-10-01] gather 目标优先挂资源点（采集是 gather 的语义渠道；只上架
        # 店铺玩家能买但买入推不动 gather 进度——配合入包 gather 别名连发双保险）。
        gather_ids = sorted(iid for iid in set(missing)
                            if "gather" in need_types.get(iid, set()))
        pre_attached = self._attach_quest_drops_to_wilderness(world, gather_ids) \
            if gather_ids else []
        remaining = [iid for iid in missing if iid not in set(pre_attached)]
        gold = [iid for iid in remaining
                if getattr(items[iid], "rarity", "") in ("legendary", "mythic")]
        attached = list(pre_attached) + (
            self._attach_quest_drops_to_wilderness(world, gold) if gold else [])
        shop_ids = [iid for iid in remaining if iid not in set(attached)]
        names = [str(items[i].name or i) for i in attached]
        if not shop_ids:
            if names:
                debug_log(lambda: f"[一致性收口] 高品任务材料挂资源点掉落: {names}")
            return names

        # 选店：出生地的材料行/杂货铺优先 -> 全局材料行/杂货铺 -> 任意店（确定性排序）
        player_loc = str(getattr(getattr(world, "player", None), "location_id", "") or "")

        def _shop_rank(s):
            st = str(getattr(s, "shop_type", "") or "")
            at = str(getattr(s, "location_id", "") or "") == player_loc
            return (0 if at else 1, 0 if st in ("material", "general") else 1,
                    str(getattr(s, "id", "") or ""))

        shops = sorted((getattr(world, "shops", None) or []), key=_shop_rank)
        if not shops:
            debug_log("[一致性收口] 全世界无商店，collect 材料无法保底上架")
            return []
        shop = shops[0]
        for iid in shop_ids:
            stock = max(3, int(need.get(iid, 1)))
            shop.stock.append(ShopStockEntry(item_id=iid, price=0,
                                             stock=stock, max_stock=stock + 5))
        shop.touch()
        names += [str(items[iid].name or iid) for iid in shop_ids]
        debug_log(lambda: f"[一致性收口] collect 材料无渠道，保底上架 {shop.name}: {names}")
        return names

    def _attach_quest_drops_to_wilderness(self, world: World, ids: list) -> list:
        """[审 2026-09-13] 把高品任务材料挂为野外地资源点额外掉落（rate 0.5，qty 1）。

        选第一个有资源点的野外地（locations 顺序，确定性）。无可挂资源点返回 []（调用方
        回退店铺）。ResourceNode.drops 条目形状与 _init_resource_nodes 口径一致。"""
        for l in (getattr(world, "locations", None) or []):
            if str(getattr(l, "kind", "") or "") != "wilderness":
                continue
            nodes = getattr(l, "resource_nodes", None) or []
            if not nodes:
                continue
            node = nodes[0]
            for iid in (ids or []):
                node.drops.append({"item_id": iid, "qty": 1, "rate": 0.5})
            return list(ids or [])
        return []

    def heal_generation_gaps(self, world: World,
                             preset: Optional[WorldSimPreset] = None) -> bool:
        """[修 2026-09-13] 生成一致性自愈入口（旧档进世界调用）：聚落归零 / 零野外兜底 /
        怪物池 danger 对齐 / kill 目标回填 / gather 目标保底 + collect 材料保底上架。
        幂等，无变更返回 False（调用方据此决定落盘）。"""
        changed = self._ensure_generation_coherence(world, preset)
        shelved = self.ensure_quest_material_sources(world, preset)
        return bool(changed or shelved)

    # ============ [P34e] 城市预留场所 + 三类 Shop ============

    def _ensure_city_venues(self, world: World, loc: Location, raw_loc: dict,
                            preset: WorldSimPreset):
        """[P34e] city 型聚落保证 places 含 3 类核心 venue（auction/weapon/alchemy）。

        - LLM 在 raw_loc["venues"] 出的 venue 优先（解析成 Place，shop_type 记到 place.type 旁的
          私有属性供 _ensure_city_shops 读；[!] Place 无 shop_type 字段，用一个 ad-hoc 映射传）。
        - 缺失的核心类型从题材兜底池 _GENRE_CITY_VENUE_TEMPLATES 确定性补齐（SeededRng 选名）。
        - town 型聚落仅补 LLM 出的 venue（不强制 3 类）；village/野外不动。
        - places_enabled=False 时跳过（退化单层，venue 概念不适用）。
        """
        if not bool(getattr(preset, "places_enabled", True)):
            return
        ss = getattr(loc, "settlement_size", "") or ""
        if ss not in ("city", "town"):
            return
        gid = self._genre_id(world)
        pool = _city_venue_pool(gid)
        # 解析 LLM venues -> [{name, shop_type, desc}]（白名单 shop_type）
        llm_venues = []
        if isinstance(raw_loc, dict):
            rv = raw_loc.get("venues")
            if isinstance(rv, list):
                for v in rv:
                    if not isinstance(v, dict):
                        continue
                    st = str(v.get("shop_type") or "").strip()
                    if st not in ("general", "weapon", "armor", "alchemy",
                                  "consumable", "material", "magic", "auction"):
                        continue
                    nm = str(v.get("name") or "").strip()
                    if not nm:
                        continue
                    llm_venues.append({"name": nm, "shop_type": st,
                                       "desc": str(v.get("desc") or "")})
        # 已有 places 的 type 集合（按 venue shop_type 标记；place.type 存的是中文场所类型名，
        # 这里用 name 反查 LLM venue 与兜底池名匹配判断是否已存在核心类型）
        existing_names = {p.name for p in loc.places}
        # 核心类型补齐：city 必含 3 类，town 不强制
        required = list(_CITY_REQUIRED_VENUE_TYPES) if ss == "city" else []
        # 从 LLM venues 先消费（标记已覆盖的 shop_type）
        covered: set[str] = set()
        for v in llm_venues:
            covered.add(v["shop_type"])
        # 把 LLM venues 落成 Place（去重：同名跳过）
        for v in llm_venues:
            if v["name"] in existing_names:
                continue
            pid = f"venue_{loc.id}_{len(loc.places)}"
            loc.places.append(Place(id=pid, name=v["name"], type=v["shop_type"],
                                    desc=v["desc"], danger=0))
            existing_names.add(v["name"])
        # 缺的核心类型从兜底池确定性补（同 type 取池里第一个未重名的）
        rng = SeededRng.seed_from(world.id, 0, f"city_venue_{loc.id}")
        for st in required:
            if st in covered:
                continue
            # 池里找该 shop_type 的候选
            cands = [c for c in pool if c.get("shop_type") == st]
            if not cands:
                continue
            # 确定性选一个未重名的
            pick = None
            for _ in range(len(cands) * 3):
                c = cands[rng.roll(0, len(cands) - 1)]
                if c["name"] not in existing_names:
                    pick = c
                    break
            if pick is None:
                pick = cands[0]
            pid = f"venue_{loc.id}_{len(loc.places)}"
            loc.places.append(Place(id=pid, name=pick["name"], type=st,
                                    desc=pick["desc"], danger=0))
            existing_names.add(pick["name"])
            covered.add(st)
        # default_place_id 空时取首个 venue 或首个 place
        if not loc.default_place_id and loc.places:
            loc.default_place_id = loc.places[0].id
        # [2026-09-08 用户指示] 食堂已删：主城不再铺食堂场所（_ensure_canteen 已 no-op，
        # 此处调用保留无副作用；恢复饱食度走食物店买食物吃）。

    def _ensure_city_shops(self, world: World, loc: Location, preset: WorldSimPreset):
        """[P34e -> 游商 2026-09-08] city 型聚落保证有 auction/weapon/alchemy 三类 Shop。

        - [游商] _init_merchants_and_shops 已改纯场所建店（游商自营）；此处补 city 缺类
          （merchant_npc_id 留空 = 游商店）。expansion 聚落由拓展收尾统一建店。
        - shops_enabled 关 -> 跳过。已在 world.shops 里的同地点同类店不重复建。
        - [!] merchant_npc_id 留空是合法的（Shop 不强制商贩存活；交易/拍卖 UI 走 shop_type 不查商贩）。
        """
        if not self._per_world(world, "shops_enabled", preset.shops_enabled, preset):
            return
        ss = getattr(loc, "settlement_size", "") or ""
        if ss != "city":
            return
        gt = self._genre_text(world)
        interval = int(self._per_world(world, "shops_restock_interval",
                                        preset.shops_restock_interval, preset)) or 5
        # 该地点已有的 shop_type 集合
        existing_types = {s.shop_type for s in world.shops if s.location_id == loc.id}
        for st in _CITY_REQUIRED_VENUE_TYPES:
            if st in existing_types:
                continue
            shop = Shop(
                merchant_npc_id="", location_id=loc.id, shop_type=st,
                restock_interval=interval, last_restock_tick=0,
                name=self._shop_themed_name(st, gt, ""),
            )
            world.shops.append(shop)
            existing_types.add(st)

    def _ensure_city_venues_and_shops(self, world: World, loc: Location, raw_loc: dict,
                                      preset: WorldSimPreset):
        """[P34e] 城市预留场所统一入口（venue + shop 一起补，build_world + expansion 共用）。"""
        self._ensure_city_venues(world, loc, raw_loc, preset)
        self._ensure_city_shops(world, loc, preset)
        self._ensure_shop_places(world)

    _VENUE_SHOP_TYPES = ("general", "weapon", "armor", "alchemy",
                         "consumable", "material", "magic", "auction")
    # LLM 场所的中文类型关键词 -> shop_type（二级配对：LLM 常写「铁匠铺/药铺」而非 weapon/alchemy；
    # 关键词场所绑定时保留店名——它不是占位 venue，店主名常在店名里）
    _VENUE_TYPE_KEYWORDS = {
        "weapon": ("铁匠铺", "兵器铺", "打铁铺", "锻造铺"),
        "armor": ("甲胄铺", "护具铺"),
        "alchemy": ("药铺", "药堂", "医馆", "药庐"),
        "general": ("杂货铺", "杂货", "商铺", "百货"),
        "auction": ("当铺", "拍卖行", "黑市"),
        "magic": ("法器铺", "法宝阁"),
        "material": ("原料行", "材料行"),
        "consumable": ("货栈",),
    }

    def _ensure_shop_places(self, world: World) -> bool:
        """[修 2026-09-06 真机用户抓出] 每家商店都要有可走进的「场所」。返回是否变更（自愈落盘用）。

        旧况：城市三类占位 Shop 用题材名自命名（百炼堂），与 venue 场所（铁剑门兵器铺，
        place.type='weapon'）两张皮——货架能买但场所按钮永远没有这家店，旁白「你寻到
        百炼堂」无实体可指。修：
        - 同地点存在未被占用的同类 venue 场所（place.type == shop.shop_type）-> 绑定
          （shop.place_id = venue.id）且店名对齐场所名（一个门面一个身份）；
        - 无同类 venue（如商人自建杂货铺）-> 按店名补一个 Place（type=shop_type）；
        - 幂等：place_id 已指向有效场所的跳过。挂点：build 尾 / 城市预留后 / 进世界自愈。
        """
        changed = False
        try:
            loc_by_id = {l.id: l for l in (getattr(world, "locations", None) or [])}
            taken: set = set()
            for shop in (getattr(world, "shops", None) or []):
                st = str(getattr(shop, "shop_type", "") or "").strip()
                if st not in self._VENUE_SHOP_TYPES:
                    continue
                loc = loc_by_id.get(str(getattr(shop, "location_id", "") or ""))
                if loc is None or not getattr(loc, "places", None):
                    continue
                if any(p.id == str(getattr(shop, "place_id", "") or "")
                       for p in loc.places):
                    continue
                changed = True
                # 一级：真 venue 场所（place.type == shop_type）——绑定且店名对齐场所名
                venue = next((p for p in loc.places
                              if str(getattr(p, "type", "") or "") == st
                              and p.id not in taken), None)
                if venue is not None:
                    taken.add(venue.id)
                    shop.place_id = venue.id
                    if str(getattr(shop, "name", "") or "") != venue.name:
                        shop.name = venue.name
                    continue
                # 二级：LLM 中文类型关键词场所（铁匠铺/药铺…）——绑定但保留店名
                kws = self._VENUE_TYPE_KEYWORDS.get(st) or ()
                kw_place = next((p for p in loc.places
                                 if str(getattr(p, "type", "") or "") in kws
                                 and p.id not in taken), None)
                if kw_place is not None:
                    taken.add(kw_place.id)
                    shop.place_id = kw_place.id
                    continue
                same = next((p for p in loc.places if p.name == shop.name), None)
                if same is not None:
                    shop.place_id = same.id
                    continue
                pid = f"shopplace_{loc.id}_{len(loc.places)}"
                loc.places.append(Place(id=pid, name=str(shop.name or "商铺"),
                                        type=st, desc="", danger=0))
                shop.place_id = pid
        except Exception:
            pass
        return changed

    # [P34e] 探查新 NPC 题材化命名池（小功能表，不进数据量基线——非内容池，仅给 roll 出的
    # 拓展 NPC 起名用；6 题材各 4 个名 + 4 个身份，确定性 rng.pick 选）。
    # [游商 2026-09-08] 身份池已摘经商身份（流浪商人/坊市掌柜/客栈掌柜/便利店老板/
    # 夜市摊主/自由商人/黑市掮客）——新世界不再产商人 NPC，代之以中性平民身份。
    _GENRE_EXPAND_NPC_NAMES = {
        "western_fantasy": (("艾德温", "玛拉", "罗兰", "伊莲娜"),
                            ("退役守卫", "酒馆伙计", "寻宝猎人", "吟游学徒")),
        "xianxia": (("陈师兄", "苏师姐", "李道友", "周散修"),
                    ("游方散修", "护院武者", "采药人", "守山弟子")),
        "wuxia": (("萧七", "柳如烟", "段公子", "韩老五"),
                  ("江湖游侠", "镖师", "说书人", "郎中")),
        "modern": (("老张", "林姐", "阿杰", "小薇"),
                   ("出租车司机", "外卖骑手", "保安", "快递员")),
        "scifi": (("凯尔", "诺娃", "雷恩", "艾达"),
                  ("赏金猎人", "维修技师", "情报贩子", "空间站技工")),
        "apocalypse": (("老周", "阿梅", "大头", "小六"),
                       ("拾荒者", "车队头领", "诊所护工", "守卫")),
    }
    # [P34e] 探查新势力题材化命名池（6 题材各 4 势力名 + 理念）。
    _GENRE_EXPAND_FACTION_NAMES = {
        "western_fantasy": (("铁血商会", "重商利"), ("黎明骑士团", "护弱小"),
                            ("暗影同盟", "谋私利"), ("森林游侠团", "守边疆")),
        "xianxia": (("青云商盟", "聚灵财"), ("剑修会", "求剑道"),
                    ("散修联盟", "互助利"), ("丹药世家", "炼丹术")),
        "wuxia": (("漕帮", "控水路"), ("镖局联盟", "护商旅"),
                  ("丐帮分舵", "济贫弱"), ("黑道堂口", "争地盘")),
        "modern": (("商会联合", "做生意"), ("安保公司", "接保镖"),
                   ("地下帮派", "收保护费"), ("出租车队", "跑运输")),
        "scifi": (("星际商行", "贸易网"), ("雇佣兵团", "接任务"),
                  ("科技公会", "搞研发"), ("走私集团", "走私货")),
        "apocalypse": (("拾荒者联盟", "搜物资"), ("车队帮", "跑运输"),
                       ("避难所议会", "守幸存者"), ("掠夺者团", "抢资源")),
    }
    # [P] 探查新任务题材化标题池（6 题材各 3 条，{loc} 替换为新地点名）。
    _GENRE_EXPAND_QUEST_TITLES = {
        "western_fantasy": ("探访{loc}", "{loc}的传闻", "{loc}探险"),
        "xianxia": ("探访{loc}", "{loc}寻缘", "{loc}历练"),
        "wuxia": ("探访{loc}", "{loc}打探", "{loc}行侠"),
        "modern": ("探访{loc}", "{loc}调查", "{loc}考察"),
        "scifi": ("探访{loc}", "{loc}勘察", "{loc}探索"),
        "apocalypse": ("探访{loc}", "{loc}搜刮", "{loc}侦察"),
    }

    def _apply_expand_chances(self, world: World, created: list,
                              new_locs_data: list, preset: WorldSimPreset):
        """[P34e] 探查/拓展概率：每个新地点 roll 新城市/新 NPC/新势力（确定性独立 salt）。

        - 三概率各用独立 salt SeededRng（seed_from(world.id, tick, f"<salt>_{loc.id}")），
          不破坏既有 rng 序列（仿 P33 hidden_check / wilderness_monster 口径）。
        - 新城市概率命中 -> 强制 kind=settlement + settlement_size=city + 跑城市 venue/shop 预留。
        - 新 NPC 概率命中 -> 生成 1 友好 NPC 挂新地点 default_place（题材命名池纯引擎，无 LLM）。
        - 新势力概率命中 -> 生成 1 Faction 挂新地点 faction_id/owner_faction_id。
        - best-effort：单步 try/except 不阻断拓展（守 tick 子阶段隔离口径）。
        """
        try:
            tick = int(getattr(world, "tick_count", 0) or 0)
            gid = self._genre_id(world)
            key_spawn_ids: list = []         # [要角补档] 本轮拓展出的要角，末尾一次性补档案
            for loc in created:
                # [P34e] 新城市概率（仅当此地点当前非聚落时 roll——聚落已定性不再改）
                try:
                    if getattr(loc, "kind", "") != "settlement":
                        p_city = float(self._per_world(
                            world, "expand_new_city_chance",
                            preset.expand_new_city_chance, preset) or 0.0)
                        if p_city > 0:
                            rng_c = SeededRng.seed_from(
                                getattr(world, "id", ""), tick, f"expand_city_{loc.id}")
                            if rng_c.chance(p_city):
                                loc.kind = "settlement"
                                loc.settlement_size = "city"
                                # 补城市 venue + 三类 shop（expansion 聚落现状零商店）
                                self._ensure_city_venues(
                                    world, loc, {}, preset)
                                self._ensure_city_shops(world, loc, preset)
                except Exception:
                    pass
                # [P34e] 新 NPC 概率（命中后按 expand_new_npc_count 生成 N 个）
                spawned_npc = None  # 本轮生成的首个 NPC（供任务发布用）
                try:
                    p_npc = float(self._per_world(
                        world, "expand_new_npc_chance",
                        preset.expand_new_npc_chance, preset) or 0.0)
                    if p_npc > 0:
                        rng_n = SeededRng.seed_from(
                            getattr(world, "id", ""), tick, f"expand_npc_{loc.id}")
                        if rng_n.chance(p_npc):
                            n_count = max(1, min(10, int(self._per_world(
                                world, "expand_new_npc_count",
                                getattr(preset, "expand_new_npc_count", 1), preset) or 1)))
                            for _ in range(n_count):
                                npc = self._spawn_expand_npc(world, loc, rng_n, gid,
                                                              preset)
                                if spawned_npc is None:
                                    spawned_npc = npc
                                if npc is not None and getattr(npc, "is_key_npc", False):
                                    key_spawn_ids.append(str(npc.id))
                except Exception:
                    pass
                # [P34e] 新势力概率
                try:
                    p_fac = float(self._per_world(
                        world, "expand_new_faction_chance",
                        preset.expand_new_faction_chance, preset) or 0.0)
                    if p_fac > 0:
                        rng_f = SeededRng.seed_from(
                            getattr(world, "id", ""), tick, f"expand_fac_{loc.id}")
                        if rng_f.chance(p_fac):
                            self._spawn_expand_faction(world, loc, rng_f, gid)
                except Exception:
                    pass
                # [P] 新任务概率（由新 NPC 发布，探访新地点支线；无 NPC 则现场造一个）
                try:
                    p_q = float(self._per_world(
                        world, "expand_new_quest_chance",
                        preset.expand_new_quest_chance, preset) or 0.0)
                    if p_q > 0:
                        rng_q = SeededRng.seed_from(
                            getattr(world, "id", ""), tick, f"expand_quest_{loc.id}")
                        if rng_q.chance(p_q):
                            self._spawn_expand_quest(world, loc, rng_q, gid,
                                                     giver_npc=spawned_npc)
                except Exception:
                    pass
            # [要角补档 2026-09-12] 本轮拓展出的要角一次性补叙事档案（小传/爱好/关系）。
            # [!] 合并成单次批量 LLM 调用，不是每个 NPC 一次——拓展一次最多出 10 个 NPC。
            # 用户指示：这些内容本来就该由 LLM 提供。失败/无 API 静默留空（档案页有兜底）。
            if key_spawn_ids:
                try:
                    self.generate_npc_profiles(world, preset,
                                               only_npc_ids=key_spawn_ids)
                except Exception:
                    pass
        except Exception:
            pass  # 整段 best-effort 不阻断拓展

    def _spawn_expand_npc(self, world: World, loc: Location, rng, genre_id: str,
                          preset: Optional[WorldSimPreset] = None):
        """[P34e] 探查生成 1 友好 NPC 挂新地点 default_place（题材命名池纯引擎无 LLM）。

        [数据量] 名字走 _GENRE_NPC_NAMES 大库（14 名/题材），身份沿用 _GENRE_EXPAND_NPC_NAMES
        角色表；确定性 SeededRng 挑，不破坏既有 rng 序列。

        [要角补档 2026-09-12] 按 preset.expand_new_npc_key_chance 概率生成为剧情要角，
        并补上要角该有的字段（确定性部分；叙事三件套不在 tick 内调 LLM）。
        """
        names_tbl = self._GENRE_EXPAND_NPC_NAMES.get(genre_id) or \
            self._GENRE_EXPAND_NPC_NAMES["western_fantasy"]
        roles = names_tbl[1]
        name = _genre_npc_name(genre_id, rng)
        role = rng.pick(roles) if roles else "路人"
        # [P45 2026-09-12 用户指示] 拓展 NPC 等级 = 1..npc_max_level 随机（与 build 的
        # 战斗身份/要角同区间；原 max(1, min(10, danger)) 与世界生成 1..30 不同源）。
        _cap = max(1, min(100, int(getattr(preset, "npc_max_level", 30) or 30))) \
            if preset is not None else 30
        npc = NPC(name=name, role=role, desc=f"在「{loc.name}」遇到的{role}。",
                  level=int(rng.roll(1, _cap)),
                  location_id=loc.id, place_id=loc.default_place_id or "",
                  alive=True, hostile=False)
        # [要角补档 2026-09-12] 概率成为剧情要角（拓展出的新地点也能长出有分量的人物）
        _p_key = float(self._per_world(world, "expand_new_npc_key_chance",
                                       getattr(preset, "expand_new_npc_key_chance", 0.25),
                                       preset) or 0.0) if preset is not None else 0.0
        npc.is_key_npc = bool(_p_key > 0 and rng.chance(_p_key))
        # [P45 2026-09-12 用户指示] 属性走 _init_combat_stats 同源基线（5+level 抖动），
        # 不再用固定 4/3 基线——同为 10 级，旧口径 atk≈8/HP≈90 与 build 的 atk≈30/HP≈200
        # 不同源，拓展 NPC 落地即「残废」。
        from src.services import combat_engine as ce
        _nr = SeededRng.seed_from(str(getattr(world, "id", "") or ""), 0,
                                  f"expand_stats_{npc.id}")
        jit = lambda base: max(1, base + _nr.roll(-1, 2))
        npc.stat_str = jit(5 + npc.level)
        npc.stat_vit = jit(5 + npc.level)
        npc.stat_dex = jit(5 + npc.level // 2)
        npc.stat_int = jit(5 + npc.level // 2)
        npc.stat_luk = jit(5)
        npc.hp_max = ce.max_hp_for(npc)
        npc.hp = npc.hp_max
        npc.mp_max = te.effective_stat(npc, "int") * 5 + npc.level * 2 \
            + int(te.get_talent_bonus(npc, "mp_bonus") or 0)  # [C3]
        npc.mp = npc.mp_max
        world.npcs.append(npc)
        if npc.id not in loc.npc_ids:
            loc.npc_ids.append(npc.id)
        # 同步到所在场所的 npc_ids（场所化地点）
        if loc.default_place_id:
            for p in loc.places:
                if p.id == loc.default_place_id and npc.id not in p.npc_ids:
                    p.npc_ids.append(npc.id)
                    break
        if npc.is_key_npc:
            # 只补确定性字段（talents/current_goal）；叙事三件套不在 tick 内发 LLM
            try:
                self.ensure_key_npc_fields(world, npc, preset, fill_profile=False)
            except Exception:  # noqa: BLE001 - 补档失败不该丢掉新 NPC
                pass
        return npc

    def _spawn_expand_faction(self, world: World, loc: Location, rng, genre_id: str):
        """[P34e] 探查生成 1 Faction 挂新地点（题材命名池纯引擎无 LLM）。"""
        fac_tbl = self._GENRE_EXPAND_FACTION_NAMES.get(genre_id) or \
            self._GENRE_EXPAND_FACTION_NAMES["western_fantasy"]
        i = rng.roll(0, len(fac_tbl) - 1)
        fname, ideology = fac_tbl[i]
        fac = Faction(name=fname, desc=f"在「{loc.name}」一带活动的势力。", ideology=ideology)
        world.factions.append(fac)
        if not loc.faction_id:
            loc.faction_id = fac.id
        if not loc.owner_faction_id:
            loc.owner_faction_id = fac.id
        fac.territory.append(loc.id)

    def _spawn_expand_quest(self, world: World, loc: Location, rng, genre_id: str,
                            giver_npc=None):
        """[P] 探查生成 1 支线任务（探访新地点 + 可选采集目标），纯引擎确定性。

        - 标题走题材化池（{loc} 替换新地点名）。
        - objective 固定 visit 新地点；新地点有资源点则追加 gather 目标。
        - giver_npc 非空作发布者；空则现场造 1 个 NPC 发布（玩家到新地点找其接取）。
        - 奖励 gold/xp 随地点危险度缩放。
        """
        if giver_npc is None:
            giver_npc = self._spawn_expand_npc(world, loc, rng, genre_id)
        titles = self._GENRE_EXPAND_QUEST_TITLES.get(genre_id) or \
            self._GENRE_EXPAND_QUEST_TITLES["western_fantasy"]
        title = (rng.pick(list(titles)) or titles[0]).format(loc=loc.name)
        objectives = [{"type": "visit", "target": loc.name, "count": 1,
                       "desc": f"前往「{loc.name}」一探究竟"}]
        if getattr(loc, "resource_nodes", None):
            rn = loc.resource_nodes[0]
            # [修 2026-09-13 审计抓出] target 用节点 type 而非 name——gather 进度按
            # node.type 匹配（与 LLM 任务段刚立的【资源点类型清单】约定同口径）；旧写法
            # 用 name（「沉船货箱」）对 type（「遗物」），能否命中全看名字里碰巧含类型词。
            objectives.append({"type": "gather", "target": str(getattr(rn, "type", "") or rn.name),
                               "count": 1,
                               "desc": f"采集「{loc.name}」的{rn.name}"})
        danger = max(1, min(10, int(getattr(loc, "danger", 1) or 1)))
        gold = max(10, danger * 5)
        xp = max(10, danger * 8)
        q = Quest(title=title, objective=objectives[0]["desc"], giver_npc_id=giver_npc.id,
                  reward_text=f"{gold} 金 + {xp} 经验", status="available",
                  objectives=objectives, rewards={"gold": gold, "xp": xp, "items": []},
                  chain="side")
        world.quests.append(q)
        return q

    def shop_for_npc(self, world: World, npc_id: str) -> Optional[Shop]:
        """取 NPC 绑定的商店（无则 None）。"""
        for s in world.shops:
            if s.merchant_npc_id == npc_id:
                return s
        return None

    # ==================== [P6c] NPC 档案生成（人际关系/爱好/小传）====================
    def generate_npc_profiles(self, world: World, preset: WorldSimPreset,
                              cancel_check: Optional[Callable[[], bool]] = None,
                              only_npc_ids: Optional[list] = None):
        """[P6c] 批量给要角/商售 NPC 生成 notes/hobbies/relationships（一次 LLM 调用）。

        世界生成后置（WorldGenWorker 调）；只挑 is_key_npc 或 is_merchant 的 NPC。
        [P20] 不再限 12 个：输出侧支持 1-2 万 token（用户预设 calculator_max_tokens 30000），
        单次 JSON 覆盖全部候选，分批纯属多余保守。LLM 出 JSON，引擎应用；失败/取消
        best-effort 不阻断（档案字段留空，档案页优雅兜底）。

        [要角补档 2026-09-12] only_npc_ids：只补这几个人（玩家勾选要角时单 NPC 补档用）。
        [!] 已填全 notes+hobbies+relationships 的一律跳过——避免重跑把玩家/LLM 已有的档案覆盖。
        """
        wanted = {str(i) for i in (only_npc_ids or []) if i}
        _fill_gaps = bool(wanted)      # 单 NPC 补档模式：只填空缺，不覆盖已有档案

        def _gap(n) -> bool:
            return not (str(getattr(n, "notes", "") or "").strip()
                        and (getattr(n, "hobbies", None) or [])
                        and (getattr(n, "relationships", None) or []))

        cands = [n for n in world.npcs if (n.is_key_npc or n.is_merchant) and n.alive
                 and (not wanted or str(n.id) in wanted) and _gap(n)]
        if not cands:
            return
        api = self._resolve_api(preset.calculator_api_id)
        if not api:
            return
        sys_prompt = (
            "你是 SLG 游戏的 NPC 档案生成引擎。我会给你若干 NPC 的基本信息（名字/身份/性格/目标/势力），"
            "请你为每个 NPC 补全：个人小传(notes)/爱好(hobbies)/人际关系(relationships)/说话风格(speech_style)。"
            "输出严格 JSON，不要任何说明或代码块标记。\n\n"
            "JSON schema：\n"
            '{"profiles": [{"name": "NPC名", "notes": "1-2句背景小传", '
            '"hobbies": ["爱好1","爱好2"], "relationships": [{"target": "其他NPC名或势力名", '
            '"relation": "关系类型(亲人/朋友/师徒/敌对/上下级/旧识...)", "desc": "一句关系说明"}], '
            '"speech_style": "1句泛义说话风格（50字内：整体语气与用词倾向，如「对陌生人语气冷硬、爱用反问，对熟人说话俏皮」；禁止写具体口头禅/固定台词）"}]}\n\n'
            "要求：关系要呼应世界设定与势力立场；爱好贴合身份；speech_style 须与性格一致且可辨识"
            "（不同 NPC 风格应有区分度）；风格是泛义倾向不是被复读的台词；不要编造与性格/目标矛盾的内容。"
        )
        tmp = Preset(name="world_sim_npc_profile", system_prompt=sys_prompt,
                     temperature=preset.calculator_temperature,
                     max_tokens=preset.calculator_max_tokens, top_p=preset.calculator_top_p)
        # [P20] 去 12 上限后 30+ 要角 + thinking 模型输出可上数 k token，读超时对齐骨架生成 600s
        # （默认 120s 大 JSON 易超时 -> 整批档案退化兜底小传）
        llm = LlmClient(api, tmp, timeout=600.0, jailbreak_prefix=self._jb_prefix())
        names = ", ".join(n.name for n in cands)
        fac_by_id = {f.id: f.name for f in world.factions}
        cand_lines = []
        for n in cands:
            fac = fac_by_id.get(n.faction_id, "无")
            cand_lines.append(f"- {n.name}（身份:{n.role or '未知'}；势力:{fac}；性格:{n.personality}；目标:{n.goal}）")
        user_msg = (_world_anchor(world) + "\n"
                    "需要补全档案的 NPC：\n" + "\n".join(cand_lines)
                    + f"\n（其他可引用的 NPC 名：{names}）\n请输出 profiles JSON。")
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}]
        # [!] 失败重试一次（回灌错误，仿骨架生成）：此前只调一次，一次 JSON 围栏
        # 失误即整批档案留空，导致「有的 NPC 有小传/爱好/关系、有的没有」的不一致。
        profiles = None
        for attempt in range(2):
            if cancel_check and cancel_check():
                return
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return
            if not (result.error or "") and (result.content or "").strip():
                data = _extract_json(result.content)
                cand_profiles = data.get("profiles") if isinstance(data, dict) else None
                if isinstance(cand_profiles, list):
                    profiles = cand_profiles
                    break
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出无法解析为合法 JSON，请严格按 schema 重新输出纯 JSON。"},
                ]
        if not profiles:
            profiles = []
        by_name = {n.name: n for n in world.npcs}
        covered: set[str] = set()
        for p in profiles:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "").strip()
            npc = by_name.get(pname)
            if npc is None:
                # [P44] 容错解析：LLM 写名常加修饰/别字，精确查会整份档案静默丢弃
                hit = nrs.resolve_name(pname, list(by_name.keys()))
                npc = by_name.get(hit) if hit else None
            if npc is None:
                continue
            covered.add(npc.name)
            notes = str(p.get("notes") or "").strip()
            if notes and not (_fill_gaps and str(getattr(npc, "notes", "") or "").strip()):
                npc.notes = notes
            hobs = p.get("hobbies") or []
            if isinstance(hobs, list) and not (_fill_gaps and (getattr(npc, "hobbies", None) or [])):
                npc.hobbies = [str(h) for h in hobs if h][:8]
            rels = p.get("relationships") or []
            if isinstance(rels, list) and not (_fill_gaps and (getattr(npc, "relationships", None) or [])):
                # [P38a] target_id 按名反查（社交演化 upsert/移除按 target_id 去重；
                # 指向势力名或无名 NPC 时留空——展示层按 target_name 兜底）。
                # [P44] 名字容错解析（LLM 写关系对象常加修饰/别字）
                npc_by_name = {str(n2.name): str(n2.id) for n2 in cands}
                clean = []
                for r in rels:
                    if not isinstance(r, dict):
                        continue
                    tn = str(r.get("target") or "").strip()
                    tid = npc_by_name.get(tn, "")
                    if not tid:
                        hit = nrs.resolve_name(tn, list(npc_by_name.keys()))
                        tid = npc_by_name.get(hit, "") if hit else ""
                    clean.append({
                        "target_id": tid,
                        "target_name": tn,
                        "relation": str(r.get("relation") or "").strip(),
                        "desc": str(r.get("desc") or "").strip(),
                    })
                npc.relationships = [r for r in clean if r["target_name"]][:10]
            # [P20] 说话风格（B1）：LLM 给的 1 句风格经软化清洗后落字段（空/脏值跳过，老字段保持空）
            ss = _sanitize_speech_style(p.get("speech_style"))
            if ss and not (_fill_gaps and str(getattr(npc, "speech_style", "") or "").strip()):
                npc.speech_style = ss[:60]
        # [!] 引擎兜底小传：LLM 批量输出漏掉的候选 NPC（或整体失败）也要有基础小传，
        # 保证档案页字段一致（关系/爱好无信息源不瞎编，留空由 UI 优雅兜底）。
        for n in cands:
            if n.name in covered or getattr(n, "notes", ""):
                continue
            bits = [n.role or "身份不明之人", n.personality or "性情难测"]
            n.notes = "，".join(bits) + "。"
            if n.goal:
                n.notes += f"心中所念：{n.goal}。"

    def ensure_npc_speech_styles(self, world: World, preset: WorldSimPreset,
                                 cancel_check: Optional[Callable[[], bool]] = None) -> bool:
        """[P20] 老世界 NPC 说话风格补生成（B1）：一次批量 LLM，幂等，QThread 内调用。

        只对「存活且 speech_style 为空」的 NPC 生成（新世界经 generate_npc_profiles 已带腔调，
        此路径覆盖存量世界）；无缺失/无 API 快速返回 False。SceneWorker 每回合开头调用，
        补完即不再触发（幂等）；失败/取消静默返回 False 下回合重试。
        返回本次是否实际生成（False 也可能是「无需生成」，调用方不区分）。
        """
        # [P20] 单次全量补齐不设候选上限：输出侧支持 1-2 万 token，批量纯属多余保守；
        # max_tokens 直接沿用 calculator_max_tokens（用户已配 30000），只给 10000 下限
        # 防有人把结算输出上限调得过低导致 JSON 截断。
        cands = [n for n in world.npcs if n.alive and not getattr(n, "speech_style", "")]
        if not cands:
            return False
        api = self._resolve_api(preset.calculator_api_id)
        if not api:
            return False
        tmp_preset = Preset(
            name="world_sim_speech_style",
            system_prompt=_NPC_SPEECH_SYS_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, int(preset.calculator_max_tokens or 10000)),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        fac_by_id = {f.id: f.name for f in world.factions}
        cand_lines = [
            f"- {n.name}（身份:{n.role or '未知'}；势力:{fac_by_id.get(n.faction_id, '无')}；"
            f"性格:{n.personality}；目标:{n.goal}）"
            for n in cands
        ]
        user_msg = (_world_anchor(world) + "\n"
                    "需要生成说话风格的 NPC：\n" + "\n".join(cand_lines)
                    + "\n请输出 styles JSON。")
        messages = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        styles = None
        for attempt in range(2):
            if cancel_check and cancel_check():
                return False
            result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                         block_labels=["系统", "用户"])
            if result.cancelled:
                return False
            if not (result.error or "") and (result.content or "").strip():
                data = _extract_json(result.content)
                cand_styles = data.get("styles") if isinstance(data, dict) else None
                if isinstance(cand_styles, list):
                    styles = cand_styles
                    break
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出无法解析为合法 JSON，请严格按 schema 重新输出纯 JSON。"},
                ]
        if not styles:
            return False
        by_name = {n.name: n for n in cands}
        filled = 0
        for s in styles:
            if not isinstance(s, dict):
                continue
            sname = str(s.get("name") or "").strip()
            npc = by_name.get(sname)
            if npc is None:
                # [P44] 容错解析（同 generate_npc_profiles 口径，防腔调静默丢失）
                hit = nrs.resolve_name(sname, list(by_name.keys()))
                npc = by_name.get(hit) if hit else None
            ss = _sanitize_speech_style(s.get("speech_style"))
            if npc is None or not ss or getattr(npc, "speech_style", ""):
                continue
            npc.speech_style = ss[:60]
            filled += 1
        if filled:
            debug_log(lambda: f"[WorldSim][P20] 补生成 NPC 说话风格 {filled} 个")
        return filled > 0

    def shops_at_location(self, world: World, loc_id: str) -> list[Shop]:
        """取地点上所有商店（玩家在此可交易）。"""
        return [s for s in world.shops if s.location_id == loc_id]

    def ensure_event_image(self, world: World, events: list,
                           preset: Optional[WorldSimPreset],
                           cancel_check: Optional[Callable[[], bool]] = None) -> int:
        """[P11d] 关键事件插图：给本 tick 最严重的新事件生成一张配图，写回 event.image。

        门控：image_enabled（生图总开关）+ image_event（本开关）+ ComfyUI 可用；
        只给 severity >= major 的事件配图（minor/trivial 太琐碎）；每 tick 限 1 张；
        只给已落 event_log 的事件配（被 max_events_per_tick/log_max 截掉的不管）。
        提示词织入事件相关地点/人物/势力名。返回生成张数。
        """
        if not self.comfyui or not getattr(preset, "image_enabled", True) \
                or not getattr(preset, "image_event", False):
            return 0
        in_log = set(id(e) for e in world.event_log)
        sev_order = {"crisis": 0, "major": 1}
        cands = sorted(
            (e for e in (events or [])
             if id(e) in in_log
             and getattr(e, "severity", "") in sev_order
             and not getattr(e, "image", "")),
            key=lambda e: sev_order.get(e.severity, 9),
        )
        if not cands:
            return 0
        if cancel_check and cancel_check():
            return 0
        e = cands[0]
        loc_names = [l.name for l in world.locations if l.id in (e.locations or [])][:2]
        npc_names = [n.name for n in world.npcs if n.id in (e.npcs or [])][:2]
        pictured = [n for n in world.npcs if n.id in (e.npcs or [])][:2]
        fac_names = [f.name for f in world.factions if f.id in (e.factions or [])][:2]
        cn = f"{e.title}，{e.desc}"
        if loc_names:
            cn += f"，场景：{'、'.join(loc_names)}"
        if npc_names:
            cn += f"，人物：{'、'.join(npc_names)}"
        if fac_names:
            cn += f"，势力：{'、'.join(fac_names)}"
        cn += "，世界事件插画，无文字"
        if pictured:
            cn += f"，画面只出现{'、'.join(n.name for n in pictured)}，保留人物固定外貌"
        style = getattr(preset, "image_style_prefix", "") or ""
        appearances = [(n.name, str(getattr(n, "appearance_tags", "") or n.appearance or ""))
                       for n in pictured]
        fname = self.generate_image(cn, style, cancel_check=cancel_check,
                                    natural=not pictured,
                                    character_appearances=appearances or None)
        if fname:
            e.image = fname
            return 1
        return 0

    def _seed_shop_from_catalog(self, shop: Shop, world: World, rng,
                                preset: Optional[WorldSimPreset] = None) -> int:
        """[P6] 确定性目录选品兜底：从 world.items 选 8-15 件填货架，每类至少 1 件。

        [2026-08-31 商人不分类] 不再按 shop_type 硬过滤——先保证 weapon/armor/consumable/
        material/accessory 每类至少 1 件，再从主营类型（_SHOP_TYPE_ITEM_TYPES）优先补齐到
        8-15 件（专营店本类多、其余类各 1-2 件）。LLM 备货失败 / LLM 关闭时用，保证商店
        永不空架且品类覆盖全。price=0 让 trade_engine 公式算。
        [拍品隔离 2026-09-09] 拍卖拍品不上货架（拍卖店也不行——拍品只走竞价）。
        """
        from src.models.world import effective_category
        wanted = _SHOP_TYPE_ITEM_TYPES.get(shop.shop_type,
                                           {"weapon", "armor", "consumable", "material"})
        # [P34a] 商店等级上限 + 培养类 reagent 排除：高 level 物品绝不出商店（唯一来源=掉落/
        # 锻造/拍卖会），鉴定洗练道具（cultivate）也不在普通商店卖（只能肝/掉/拍）。
        max_lvl = max(0, int(getattr(preset, "shop_max_item_level", 3) or 0)
                      if preset is not None else 3)
        auc_ids = _auction_lot_ids(world)
        # [修 2026-09-13 用户指示] 非拍卖行 rarity 闸：legendary/mythic 绝不上普通游商店
        # 货架，epic 至多 1 件（与 LLM 备货提示词「epic/legendary 极少 1-2 件顶天」对齐）。
        # 技能书一并受闸——「全店可见」特例不再越过品级闸，金/红书只走拍卖行/掉落
        # （曾实测 legendary 教程被随机抽进普通店）。拍卖行维持传奇轮换店设计不受限。
        _is_auction_shop = str(getattr(shop, "shop_type", "") or "") == "auction"

        def _rarity_ok(it) -> bool:
            if _is_auction_shop:
                return True
            return getattr(it, "rarity", "") not in ("legendary", "mythic")

        def _shop_eligible(it) -> bool:
            if it.type == "key":
                return False
            if effective_category(it) == "cultivate":
                return False
            if it.id in auc_ids:
                return False
            if max_lvl > 0 and getattr(it, "level", 0) > max_lvl:
                return False
            return _rarity_ok(it)

        pool = [it for it in world.items if _shop_eligible(it)]
        # [P13] 技能书全店可见（法器阁/杂货铺/拍卖行都可能卖书——超出类型过滤的常备品）
        books = [it for it in world.items
                 if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill]
        pool = pool + [b for b in books if b not in pool and _rarity_ok(b)]
        # epic 至多 1 件：池内多件 epic 时 rng 确定性保 1（非拍卖行；拍卖行不受限）
        if not _is_auction_shop:
            epics = [it for it in pool if getattr(it, "rarity", "") == "epic"]
            if len(epics) > 1:
                keep = rng.pick(sorted(epics, key=lambda x: x.id))
                pool = [it for it in pool if getattr(it, "rarity", "") != "epic"] + [keep]
        # [P7k7] 拍卖/黑市：优先 legendary/epic 物品（传奇物品轮换店）
        if _is_auction_shop:
            legendary_pool = [it for it in pool if it.rarity in ("legendary", "epic")]
            if legendary_pool:
                pool = legendary_pool
        if not pool:
            pool = [it for it in world.items
                    if it.type != "key" and effective_category(it) != "cultivate"
                    and it.id not in auc_ids
                    and (max_lvl <= 0 or getattr(it, "level", 0) <= max_lvl)
                    and _rarity_ok(it)]
        if not pool:
            return 0
        pool = sorted(pool, key=lambda it: it.id)
        # [2026-08-31] 先每类至少 1 件，再主营类型优先补齐到 8-15 件
        _TYPES = ("weapon", "armor", "consumable", "material", "accessory")
        by_type = {t: [it for it in pool if it.type == t] for t in _TYPES}
        # [酒楼分家 2026-09-10] consumable 店（酒楼/餐厅/食堂）的 consumable 位只拿
        # 吃喝（feed）：丹药归药铺。其他店型 consumable 位不动（药铺照卖丹药）。
        # 过滤作用于三段选品（每类首件/主营补齐/全类型补齐）：主营/全类型两段走 pool，
        # 须同步排除丹药，否则首件拿对、补齐又把药塞回来。
        _is_food_shop = str(getattr(shop, "shop_type", "") or "") == "consumable"
        if _is_food_shop:
            foods = [it for it in by_type.get("consumable", []) if _is_food_item(it)]
            if foods:
                by_type["consumable"] = foods
            pool = [it for it in pool if not _is_medicine_item(it)]
        target = rng.roll(8, 15) if hasattr(rng, "roll") else 12
        picked: list[str] = []
        seen: set[str] = set()

        def _take(it) -> None:
            if it is not None and it.id not in seen:
                picked.append(it.id)
                seen.add(it.id)

        # 1) 每类各挑 1 件（该类非空时）
        for t in _TYPES:
            _take(rng.pick(by_type.get(t) or []))
        # 2) 主营类型优先补齐
        main_rest = [it for it in pool if it.id not in seen and it.type in wanted]
        while len(picked) < target and main_rest:
            _take(main_rest.pop(rng.roll(0, len(main_rest) - 1)))
        # 3) 仍不足从全类型补齐
        all_rest = [it for it in pool if it.id not in seen]
        while len(picked) < target and all_rest:
            _take(all_rest.pop(rng.roll(0, len(all_rest) - 1)))
        # 落架
        by_id = {it.id: it for it in pool}
        added = 0
        for iid in picked:
            it = by_id.get(iid)
            if it is None:
                continue
            shop.stock.append(ShopStockEntry(
                item_id=it.id, price=0,
                stock=rng.roll(1, 5), max_stock=rng.roll(5, 10),
            ))
            added += 1
        return added

    def generate_shops_goods(self, world: World, preset: WorldSimPreset,
                             cancel_check: Optional[Callable[[], bool]] = None):
        """[P6] 为所有空货架商店备货（世界生成后置 + 墙钟/按需重生成入口）。

        - LLM 备货开启：每店调 _generate_shop_goods_llm 出差异化货架；失败回退目录选品。
        - LLM 关闭：直接目录选品。
        - 已有货架空过（重生成场景）：调用方应先清 shop.stock 再调本方法。
        取消（cancel_check）中断保留已备货部分（仿 §5）。
        """
        if not self._per_world(world, "shops_enabled", preset.shops_enabled, preset):
            return
        llm_on = self._per_world(world, "shops_llm_restock_enabled",
                                  preset.shops_llm_restock_enabled, preset)
        seed_rng = SeededRng.seed_from(world.id, int(world.tick_count or 0), "shop_init")
        for shop in world.shops:
            if shop.stock:
                continue
            if cancel_check and cancel_check():
                break
            ok = False
            if llm_on:
                ok = self._generate_shop_goods_llm(shop, world, preset, cancel_check)
            if not ok:
                self._seed_shop_from_catalog(shop, world, seed_rng, preset)
            from src.services import dungeon_tools as _dt
            _dt.stock_tools(world, shop, max_level=preset.shop_max_item_level)
            shop.last_restock_tick = int(world.tick_count or 0)
            shop.touch()
        # [P12g->P11c] 货架物品补图：备货已改为只从已有物品选品（不再新建物品），
        # 但世界生成/拓展时生图关闭或限额漏掉的物品仍可能无 icon，扫货架逐步补；
        # 每轮限 3 张防拖太久，cancel 可中断。
        self.ensure_item_icons(world, preset, cancel_check, limit=3)

    def ensure_item_icons(self, world: World, preset: Optional[WorldSimPreset],
                          cancel_check: Optional[Callable[[], bool]] = None,
                          limit: int = 3, items: Optional[list] = None) -> int:
        """给无 icon 的物品补生图（写回 item.icon）。

        items 传入时只处理该列表（地图拓展新物品）；默认扫全部商店货架
        （历史漏网无图物品逐步补上）。返回本轮生成张数。生图关闭/ComfyUI 未配置/
        取消时返回 0（不阻断）。持久化由调用方 save_world 负责。
        """
        if not self.comfyui or not getattr(preset, "image_enabled", True):
            return 0
        style = getattr(preset, "image_style_prefix", "") or ""
        made = 0
        seen: set[str] = set()
        if items is not None:
            candidates = [it for it in (items or [])]
        else:
            candidates = []
            for shop in world.shops:
                for entry in (shop.stock or []):
                    it = next((i for i in world.items if i.id == entry.item_id), None)
                    if it is not None:
                        candidates.append(it)
        for it in candidates:
            if made >= limit:
                return made
            if cancel_check and cancel_check():
                return made
            if it.id in seen or getattr(it, "icon", ""):
                continue
            seen.add(it.id)
            cn = f"{it.name}，{getattr(it, 'desc', '') or ''}，{it.type}类型，物品图标"
            fname = self.generate_image(cn, style, cancel_check=cancel_check, natural=True)
            if fname:
                it.icon = fname
                made += 1
        return made

    def generate_trade_reply(self, world: World, shop: Shop, trades: list,
                             preset: WorldSimPreset,
                             cancel_check: Optional[Callable[[], bool]] = None) -> Optional[str]:
        """[P14] 交易后店主回应：玩家关店时以店主口吻对本次买卖说 1-2 句话。

        仅真商人店（merchant 可解析）调用：游商店无真商人 NPC，ShopDialog 关店时
        直接跳过不调这里（用户指示 2026-09-09，省一次叙事 LLM）。此前游商名 +
        题材称谓作店主身份的兼容分支已不再被调用，保留签名不动。
        trades = [{action: buy|sell, name, rarity, price}]（ShopDialog 记账）。
        用叙事 LLM 一次性小调用（输出上限 1 万起，期望 60 字内短回复）；无 API/无交易返回 None。
        数值结算已由 trade_engine 完成，这里只出风味文本（守数值范式）。
        """
        api = self._resolve_api(preset.narrative_api_id or preset.calculator_api_id)
        merchant = next((n for n in world.npcs if n.id == shop.merchant_npc_id), None)
        if not api or not trades:
            return None
        gt = self._genre_text(world)
        trade_lines = []
        for t in trades[:8]:
            act = "买走" if t.get("action") == "buy" else "卖给店里"
            trade_lines.append(f"- 玩家{act}「{t.get('name', '')}」（{t.get('rarity', 'common')}档，"
                               f"{t.get('price', 0)} {gt.currency}）")
        system = (
            "你是 SLG 游戏里的商店店主。玩家刚在你店里做完几笔交易准备离开，"
            "请以店主的口吻说 1-2 句收摊的话：可以对大买卖表示惊喜、可以夸玩家的眼光、"
            "可以在玩家卖了好货时约定「有货再来找我」、也可以为玩家买的东西给句叮嘱，"
            "贴合店主的性格与世界观题材，像活人说话，不要客套模板。\n"
            "只输出店主说的话本身（可带动作神态描写，60 字以内），不要任何前后缀。"
        )
        user = (
            f"店主：{(merchant.name if merchant else trade_vendor_name(world, shop))}"
            f"（身份：{(merchant.role if merchant else '') or '掌柜'}；"
            f"性格：{(merchant.personality if merchant else '') or '随和'}）\n"
            f"店铺：{shop.name or '商店'}\n"
            f"世界观：{world.premise}；题材：{', '.join(world.genre_tags) or '通用'}\n"
            f"本次交易：\n" + "\n".join(trade_lines) + "\n"
            f"玩家现有 {world.player.gold} {gt.currency}。"
        )
        tmp_preset = Preset(
            name="world_sim_trade_reply", system_prompt=system,
            temperature=preset.narrative_temperature,
            max_tokens=max(10000, preset.narrative_max_tokens),
            top_p=preset.narrative_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        if cancel_check and cancel_check():
            return None
        result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                     block_labels=["系统", "用户"])
        if result.cancelled or result.error:
            return None
        text = (result.content or "").strip().strip('"“”“”')
        return text or None

    def regen_shops(self, world: World, shop_ids: list[str], preset: WorldSimPreset,
                    cancel_check: Optional[Callable[[], bool]] = None) -> list[str]:
        """[P6] 强制重生成指定商店货架（清空 -> LLM/目录重新备货）。

        墙钟定时器（QTimer）触发 / 玩家打开过期商店按需触发。返回被重置的商店名列表。
        取消保留已重置部分（仿 §5）。与 generate_shops_goods 共用 LLM/目录双路备货。
        [游商手动刷新 2026-09-09] 本方法是真商人店 LLM 链：游商店（无真商人 NPC）
        的手动刷新走 manual_vendor_refresh（确定性本地补货，零 LLM），不调这里。
        """
        if not self._per_world(world, "shops_enabled", preset.shops_enabled, preset):
            return []
        target = set(shop_ids or [])
        cleared = []
        for shop in world.shops:
            if shop.id in target:
                shop.stock = []
                cleared.append(shop.name or shop.id)
        if cleared:
            self.generate_shops_goods(world, preset, cancel_check)
        return cleared

    def manual_vendor_refresh(self, world: World, shop_id: str,
                              preset: Optional[WorldSimPreset] = None) -> str:
        """[游商手动刷新 2026-09-09 用户指示] 游商店点「刷新商品」-> 确定性本地补货，零 LLM。

        语义 = 替玩家立刻跑一轮游商离线周期（_vendor_restock 进货+合成+上架 + 价格漂移），
        不清空现有货架（与 tick_shops 同口径：增量补货不是推倒重建）；last_restock_tick
        推进到当前 tick（本次手动算一次补货，tick 周期不重复补）。
        真商人店（_shop_merchant 可解析）返回 ""——调用方应走 regen_shops/ShopWorker
        LLM 链。返回被刷新的商店名（"" = 未处理：店不存在/开关关闭/真商人店）。
        纯 Python 同步调用（瞬时完成，不进 QThread，无 cancel_check）。
        [拍品隔离] 经 _vendor_restock/_seed 口径，拍品不上架（与离线周期同）。
        """
        if preset is not None:
            if not self._per_world(world, "shops_enabled", preset.shops_enabled, preset):
                return ""
        shop = next((s for s in (getattr(world, "shops", None) or [])
                     if getattr(s, "id", "") == shop_id), None)
        if shop is None:
            return ""
        try:
            merchant = self._shop_merchant(world, shop)
        except Exception:
            merchant = None
        if merchant is not None:
            return ""  # 真商人店：走 LLM 链，不归本方法管
        # [拍品隔离存量清扫] 进补货前先清本店旧拍品条目（刷新按钮点下去即时生效，
        # 不必等下一 tick）。
        self.sweep_auction_lots_from_shelves(world, shop_id=shop.id)
        # [酒楼分家存量清扫 2026-09-10] 本店是酒楼时旧丹药条目一并清掉。
        self.sweep_medicine_from_food_shops(world, shop_id=shop.id)
        tick = int(getattr(world, "tick_count", 0) or 0)
        rng = SeededRng.seed_from(str(getattr(world, "id", "") or ""), tick,
                                  f"shop_{shop.id}_manual")
        self._vendor_restock(world, shop, rng, preset=preset)
        if preset is not None:
            drift_level = self._per_world(world, "shops_price_drift",
                                          preset.shops_price_drift, preset)
        else:
            drift_level = "medium"
        if drift_level != "off":
            item_map = {it.id: it for it in (getattr(world, "items", None) or [])}
            # 找不到 item 时返回 0（让 drift_prices 跳过），勿传 None 致伪基准
            tre.drift_prices(shop, lambda iid: (tre.compute_item_price(item_map[iid])
                             if iid in item_map else 0),
                             str(getattr(world, "id", "") or ""), tick, drift_level)
        shop.last_restock_tick = tick
        shop.touch()
        return shop.name or shop.id

    # ============ [P24a] 住宅：购宅（引擎结算全在 home_engine）============

    def purchase_home(self, world: World, loc, tier: int, preset: WorldSimPreset = None,
                      name: str = "", cancel_check: Optional[Callable[[], bool]] = None
                      ) -> tuple[bool, str]:
        """[P24a] 购宅编排：玩家自命名 -> he.buy_home 校验/扣费/落宅/事件（纯引擎，无 LLM）。

        [2026-08-31 改自命名] name 由 UI 弹输入框玩家填写（可预填题材池名作建议），
        空名回退题材池确定性兜底（he.buy_home 内 fallback_home_name）。
        preset/cancel_check 保留签名兼容（旧调用/测试传 preset），当前不再用 LLM。
        """
        return he.buy_home(world, loc, tier, name, "")

    # ============ [敌袭观战 2026-09-06 用户指示] 据点敌袭观战会话 ============

    def build_domain_siege_session(self, world: World, preset: Optional[WorldSimPreset] = None):
        """[敌袭观战] 从 world.pending_domain_siege 组装观战 CombatSession（玩家不参战）。

        - 玩家位 = 守方队长（驻守打手之首）快照，session.spectate=True + player_label=
          队长名（引擎 lost 判定改守方全灭，日志文案不写「你」）；
        - ally_units = 其余守方（含哨卫）；enemy_units = pending 定格的攻方怪；
        - 弱点/布局/怪物立绘仿 start_combat 尾段同口径（确定性 seed 挂 domain+tick）。
        返回 (session, domain) 或 (None, None)（pending 缺失/据点不存在）。
        """
        pd = getattr(world, "pending_domain_siege", None)
        if not isinstance(pd, dict) or not pd.get("domain_id"):
            return None, None
        domain = next((d for d in (getattr(world, "domains", None) or [])
                       if str(getattr(d, "id", "")) == str(pd.get("domain_id"))), None)
        if domain is None:
            world.pending_domain_siege = {}
            return None, None
        dfd, staff_pairs, atk = de.rebuild_siege_lines(world, domain)
        if not dfd or not atk:
            world.pending_domain_siege = {}
            return None, None
        tick = int(pd.get("tick", 0) or getattr(world, "tick_count", 0) or 0)
        leader_unit = dfd[0]
        session = ce.CombatSession(
            player=leader_unit.snapshot,
            enemy=atk[0].snapshot,
            player_skills=[], enemy_skills=[],
            spectate=True, player_label=str(leader_unit.name),
            # [修 2026-09-10] 观战同口径注入 MP 上限（队长=玩家位 / 来袭首领=主敌位）
            player_mp=int(getattr(leader_unit, "mp", 0) or 0),
            enemy_mp=int(getattr(atk[0], "mp", 0) or 0),
            player_mp_max=max(0, int(getattr(leader_unit, "mp_max", 0) or 0)),
            enemy_mp_max=max(0, int(getattr(atk[0], "mp_max", 0) or 0)),
            genre_id=self._genre_id(world),
            world_id=world.id,  # [技能熟练随机 2026-09-10] 与 start_combat 同口径（观战无玩家施展，仅统一会话口径）
        )
        # 敌方单位表：[0] 主敌包装（快照同引用）；攻方全为独立快照
        atk[0].role_label = "来袭首领"
        session.enemy_units = list(atk)
        # 守方：队长（玩家位）不入 ally；其余打手/哨卫入 ally（npc_id 供结算回写）
        session.ally_units = list(dfd[1:])
        # 弱点 + 2-1-2 布局 + 立绘（仿 start_combat 尾段，确定性 seed 挂据点敌袭）
        rng_wp = SeededRng.seed_from(world.id, tick, f"siege_wp_{domain.id}")
        for u in session.enemy_units:
            u.weakpoint = ce.roll_weakpoint(world.id, u.name, tick)
            u.snapshot.weakpoint = u.weakpoint
        session.player_row = 2 if rng_wp.chance(0.5) else 1
        ce.assign_layout(list(session.enemy_units or []), rng_wp)
        ce.assign_layout(list(session.ally_units or []) + [leader_unit], rng_wp)
        # 攻方怪物立绘：题材怪物图缓存（游玩中不现场渲染；无图 UI 占位绘制）
        for u in session.enemy_units:
            _m = NPC(name=u.name, role="魔物", hostile=True, level=int(u.snapshot.level or 1))
            u.avatar = self.cached_combat_image(_m) or ""
        for u in session.ally_units:
            _n = next((n for n in world.npcs if n.id == u.npc_id), None)
            u.avatar = (self.cached_combat_image(_n) or "") if _n is not None else ""
        return session, domain

    def settle_spectated_siege(self, world: World, domain, session) -> list:
        """[敌袭观战] 观战结束结算（缴获/繁荣/负伤回写；清 pending），domain_engine 同口径。"""
        tick = int(getattr(world, "tick_count", 0) or 0)
        return de.settle_spectated_siege(world, domain, session, tick)

    # ============ [D1] 据点：购地（引擎结算全在 domain_engine）============

    def purchase_domain(self, world: World, loc, preset: WorldSimPreset = None,
                        name: str = "", cancel_check: Optional[Callable[[], bool]] = None
                        ) -> tuple[bool, str]:
        """[D1] 购地编排：玩家自命名 -> de.buy_domain 校验/扣费/地点改造/铺场所/事件（纯引擎，无 LLM）。

        [2026-08-31 改自命名] name 由 UI 弹输入框玩家填写（可预填题材池名作建议），
        空名回退题材池确定性兜底（de.buy_domain 内 fallback_domain_name）。
        preset/cancel_check 保留签名兼容（旧调用/测试传 preset），当前不再用 LLM。
        [2026-09-08 用户指示] 食堂已删：据点不再铺食堂（调用保留无副作用）。
        """
        ok, err = de.buy_domain(world, loc, name, "")
        return ok, err

    def _ensure_canteen(self, world: World, loc: Location, preset: WorldSimPreset):
        """[2026-09-08 用户指示] 食堂已删：本函数保留签名兼容（旧调用/旧档残留），
        一律 no-op 不再铺食堂场所。恢复饱食度走食物店买食物吃。"""

    # ============ [D2] 据点：设施建造起名 / 职员招募档案编排 ============

    def build_domain_facility(self, world: World, domain, kind: str,
                              preset: WorldSimPreset,
                              cancel_check: Optional[Callable[[], bool]] = None
                              ) -> tuple[bool, str]:
        """[D2] 建设施编排（供 UI worker 子线程调用，仿 purchase_domain）：起名小调用
        （calculator API，失败回退题材池名）-> de.build_facility 校验/扣费扣料/铺场所/事件。

        取消不建造（返回 err）；起名失败空名兜底题材池照常建造（口径同购地）。
        """
        name, desc = "", ""
        api = self._resolve_api(preset.calculator_api_id)
        if api:
            tmp_preset = Preset(
                name="world_sim_domain_facility",
                system_prompt=_DOMAIN_FACILITY_NAME_SYSTEM_PROMPT,
                temperature=preset.calculator_temperature,
                max_tokens=preset.calculator_max_tokens,
                top_p=preset.calculator_top_p,
            )
            llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
            messages = [
                {"role": "system", "content": tmp_preset.system_prompt},
                {"role": "user", "content": self._build_facility_name_user_message(world, domain, kind)},
            ]
            for attempt in range(2):
                if cancel_check and cancel_check():
                    return False, "已取消"
                result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                             block_labels=["系统", "用户"])
                if result.cancelled:
                    return False, "已取消"
                if result.error or not (result.content or "").strip():
                    if attempt == 0:
                        messages = messages + [
                            {"role": "assistant", "content": result.content or ""},
                            {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                        ]
                        continue
                    break
                data = _extract_json(result.content)
                if not (data and isinstance(data, dict) and str(data.get("name") or "").strip()):
                    if attempt == 0:
                        messages = messages + [
                            {"role": "assistant", "content": result.content or ""},
                            {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 name 字段，请严格按 schema 输出纯 JSON。"},
                        ]
                        continue
                    break
                name = str(data.get("name") or "").strip()[:20]
                desc = str(data.get("desc") or "").strip()[:100]
                break
        return de.build_facility(world, domain, kind, name, desc)

    def _build_facility_name_user_message(self, world: World, domain, kind: str) -> str:
        """[D2] 设施起名 user 消息（守「提示词-数据池联动」：带世界观 + 题材设施名池参考）。"""
        gt = self._genre_text(world)
        entry = next((f for f in de.facility_catalog(world) if f.get("kind") == kind), None)
        names_ref = "、".join((entry or {}).get("names") or [])
        type_zh = (entry or {}).get("type") or kind
        parts = [
            f"任务：为玩家据点新落成的{type_zh}起名并写一句描写。",
            _world_anchor(world),
            f"【据点】{domain.name}（{domain.desc or '玩家的私人据点'}）",
            f"【题材货币】{gt.currency}（本次建造价 {de.facility_price(world, entry or {})} {gt.currency}）",
            f"【命名风格参考（同题材{type_zh}名，可另创但保持同风味）】{names_ref}",
            f"请按 schema 输出纯 JSON（name 2-6 字；desc 一句 30 字内，写这座{type_zh}的样子）。",
        ]
        return "\n".join(parts)

    def hire_domain_staff(self, world: World, domain, role_key: str,
                          preset: WorldSimPreset,
                          cancel_check: Optional[Callable[[], bool]] = None
                          ) -> tuple[bool, str]:
        """[D2] 招募职员编排（供 UI worker 子线程调用）：占位名题材姓名库挑选（确定性去重）
        -> de.hire_staff 骨架落世（先保证功能接续）-> LLM 语义档案批补（真名/身份/性格/
        目标/外貌/腔调/小传，仿 P46 backfill；失败/取消保留骨架不回滚——职员已入职）。

        返回 (ok, err)；取消发生在 LLM 段时职员仍以骨架入职（返回 ok，err 空串）。
        """
        existing = {n.name for n in world.npcs}
        genre_id = (world.config_overlay or {}).get("attribute_template_id") or "western_fantasy"
        rng = SeededRng.seed_from(world.id, int(getattr(world, "tick_count", 0) or 0),
                                  f"domain_hire_name_{role_key}_{domain.id}")
        pool = [x for x in (_GENRE_NPC_NAMES.get(genre_id)
                            or _GENRE_NPC_NAMES["western_fantasy"])
                if x not in existing]
        if pool:
            ph_name = rng.pick(pool)
        else:
            base = rng.pick(list(_GENRE_NPC_NAMES.get(genre_id)
                                 or _GENRE_NPC_NAMES["western_fantasy"]))
            ph_name = f"{base}2"
            while ph_name in existing:
                ph_name += "·新"
        npc, err = de.hire_staff(world, domain, role_key, ph_name)
        if npc is None:
            return False, err
        # ---- LLM 语义档案批补（P46 口径：API 两级兜底 sim -> calculator）----
        # [!] 全段包异常（审查加固）：档案批只是锦上添花，任何意外（网络栈抛出等）都
        # 不得把「骨架已入职」误报成失败——返回 ok，骨架档案保留。
        try:
            return self._hire_profile_llm(world, domain, npc, role_key, preset, cancel_check)
        except Exception:  # noqa: BLE001
            return True, ""

    def _hire_profile_llm(self, world: World, domain, npc, role_key: str,
                          preset: WorldSimPreset,
                          cancel_check: Optional[Callable[[], bool]] = None
                          ) -> tuple[bool, str]:
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api or (cancel_check and cancel_check()):
            return True, ""
        sys_prompt = _DOMAIN_HIRE_PROFILE_SYSTEM_PROMPT
        tmp = Preset(name="world_sim_domain_hire", system_prompt=sys_prompt,
                     temperature=preset.calculator_temperature,
                     max_tokens=max(10000, preset.calculator_max_tokens),
                     top_p=preset.calculator_top_p)
        llm = LlmClient(api, tmp, timeout=600.0, jailbreak_prefix=self._jb_prefix())
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": self._build_hire_profile_user_message(world, domain, npc, role_key)},
        ]
        result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                     block_labels=["系统", "用户"])
        if result.cancelled or (result.error or "") or not (result.content or "").strip():
            return True, ""      # 失败/取消保留骨架档案
        data = _extract_json(result.content)
        prof = data.get("profile") if isinstance(data, dict) else None
        if not isinstance(prof, dict):
            return True, ""
        ev = next((e for e in reversed(world.event_log)
                   if e.category == "npc" and npc.id in (e.npcs or [])), None)
        new_name = str(prof.get("name") or "").strip()
        # [!] existing 在本方法内重建（重构自 hire_domain_staff 拆出时曾误引用外层局部）
        existing = {n.name for n in world.npcs}
        if 2 <= len(new_name) <= 12 and new_name != npc.name and new_name not in existing:
            old = npc.name
            npc.name = new_name
            if ev is not None:   # 已发入职事件同步改名（引擎段先发用占位名）
                ev.title = ev.title.replace(old, new_name)
                ev.desc = (ev.desc or "").replace(old, new_name)
        for field, maxlen in (("role", 30), ("personality", 30), ("goal", 30),
                              ("appearance", 60), ("speech_style", 50), ("notes", 100)):
            if field == "speech_style":
                v = _sanitize_speech_style(prof.get(field))   # [口癖软化] 与其余落库点同口径
            else:
                v = str(prof.get(field) or "").strip()
            if v:
                setattr(npc, field, v[:maxlen])
        return True, ""

    def _build_hire_profile_user_message(self, world: World, domain, npc, role_key: str) -> str:
        """[D2] 职员档案 user 消息（守「提示词-数据池联动」：带世界观锚 + 据点/职业上下文）。"""
        label = de.ROLE_LABELS.get(role_key, role_key)
        parts = [
            f"任务：为玩家据点新招募的{label}生成完整人物档案。",
            _world_anchor(world),
            f"【据点】{domain.name}（{domain.desc or '玩家的私人据点'}；繁荣度 {domain.prosperity}/100）",
            f"【职业】{label}（占位名：{npc.name}，可另起真名）",
            f"【等级】{npc.level}（日薪 {de.daily_wage_of(npc)}，由据点资金池支付）",
            "请输出 profile JSON。",
        ]
        return "\n".join(parts)

    def _build_shop_user_message(self, shop: Shop, world: World) -> str:
        """构造商店备货 LLM user 消息（题材类型/货币/地点/店主/基调 + 现有物品清单选品）。

        [P11c] 备货只从现有物品清单里选（item_id 引用），商店不再发明新物品。
        [游商 2026-09-08] 店主 = 真商人 NPC（旧档）或游商显示名（「{店名}·{称谓}」，
        merchant 查不到时用游商名代替店主，避免 LLM 备厂货时面对「店主」空名）。
        [拍品隔离 2026-09-09] 拍卖拍品不入清单——LLM 选不到，拍品只走拍卖会竞价。
        """
        gt = self._genre_text(world)
        merchant = next((n for n in world.npcs if n.id == shop.merchant_npc_id), None)
        if merchant is not None:
            keeper_line = (f"【店主】{merchant.name}"
                           f"（身份：{merchant.role or '掌柜'}；性格：{merchant.personality or '随和'}）")
        else:
            keeper_line = f"【店主】{trade_vendor_name(world, shop)}（该店自营游商）"
        loc = next((l for l in world.locations if l.id == shop.location_id), None)
        parts = [
            "任务：为一家商店从现有物品清单中挑选上架商品。",
            f"【商店类型】{shop.shop_type}（题材显示名：{gt.shop_type(shop.shop_type)}）",
            f"【题材货币】{gt.currency}（玩家持有 {world.player.gold} {gt.currency}）",
            f"【地点】{loc.name if loc else '未知'}（{loc.desc if loc else ''}）",
            keeper_line,
            _world_anchor(world),
        ]
        auc_ids = _auction_lot_ids(world)
        catalog = [it for it in world.items if it.type != "key" and it.id not in auc_ids]
        # [酒楼分家 2026-09-10] consumable 店（酒楼/餐厅/食堂）清单行给吃喝打标，
        # 配合 shops_system_prompt 规则 2 的「酒楼只选吃喝」约束（LLM 按标选品；
        # 落架应用层另有 _is_medicine_item 硬拦，双保险）。
        _is_food_shop = str(getattr(shop, "shop_type", "") or "") == "consumable"
        # [!] 截断保尾部：探索新物品 append 在 world.items 尾部（最新），头部截断会把
        # 「探索->新物品->商店上架」闭环截断；超 80 件时保留头 40 + 尾 40（新物品必可见）。
        shown = catalog[:80]
        truncated = 0
        if len(catalog) > 80:
            shown = catalog[:40] + catalog[-40:]
            truncated = len(catalog) - len(shown)
        lines = []
        for it in shown:
            slot = f"/{it.slot}" if getattr(it, "slot", "") else ""
            desc = (getattr(it, "desc", "") or "")[:30]
            mark = "｜吃喝" if (_is_food_shop and _is_food_item(it)) else ""
            lines.append(f"{it.id} | {it.name} | {it.type}{slot} | {it.rarity} | {desc}{mark}")
        if lines:
            body = "\n".join(lines)
            if truncated > 0:
                body += f"\n（清单共 {len(catalog)} 件，展示较早 40 件 + 最新 40 件，省略 {truncated} 件）"
            parts.append("【现有物品清单（goods 只能引用这里的 item_id）】\n" + body)
        else:
            parts.append("【现有物品清单】（世界暂无物品，输出空 goods 数组）")
        parts.append("请按 schema 输出纯 JSON（goods 只含清单内 item_id + stock + base_price）。")
        return "\n".join(parts)

    def _generate_shop_goods_llm(self, shop: Shop, world: World, preset: WorldSimPreset,
                                 cancel_check: Optional[Callable[[], bool]]) -> bool:
        """调结算 LLM 给单店备货，从现有物品清单选品落 ShopStockEntry。返回是否成功。"""
        api = self._resolve_api(preset.calculator_api_id)
        if not api:
            return False
        tmp_preset = Preset(
            name="world_sim_shop",
            system_prompt=preset.shops_system_prompt or DEFAULT_WORLDSIM_SHOP_SYSTEM_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=preset.calculator_max_tokens,
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        messages = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": self._build_shop_user_message(shop, world)},
        ]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return False
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return False
            if result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return False
            data = _extract_json(result.content)
            if not (data and isinstance(data, dict) and isinstance(data.get("goods"), list)):
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 goods 字段，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return False
            # 解析成功 -> 应用
            sn = str(data.get("shop_name") or "").strip()
            if sn:
                shop.name = sn
            self._apply_shop_goods(shop, world, data["goods"])
            # [2026-08-31] 兜底补齐品类（LLM 未守「每类至少 1 件」时）
            self._ensure_type_coverage(shop, world)
            return bool(shop.stock)
        return False

    def _apply_shop_goods(self, shop: Shop, world: World, goods: list):
        """[P11c] 把 LLM 选品落成 ShopStockEntry——只从已有物品进货，不新建 Item。

        LLM 返回 item_id（schema 首选）；容忍 name 兜底匹配；清单外的一律跳过。
        世界的新物品只经「地图拓展」渠道进入（守数值范式：备货是选品不是创造）。
        [拍品隔离 2026-09-09] 拍品即便被 LLM 点名（幻觉/旧清单记忆）也跳过——
        应用层兜底，与 _build_shop_user_message 清单排除双保险。
        """
        shop.stock = []
        from src.models.world import effective_category
        auc_ids = _auction_lot_ids(world)
        seen_ids: set[str] = set()
        for g in goods:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("item_id") or "").strip()
            gname = str(g.get("name") or "").strip()
            it = None
            if gid:
                it = next((i for i in world.items if i.id == gid), None)
            if it is None and gname:
                # [P44] name 兜底走统一容错解析（锚定 world.items，清单外仍跳过）
                g_hit = nrs.resolve_name(gname, [i.name for i in world.items])
                it = next((i for i in world.items if i.name == g_hit), None) if g_hit else None
            # [P34a] key + cultivate reagent 不上架（鉴定洗练道具不在普通商店卖，只能肝/掉/拍）
            # [酒楼分家 2026-09-10] consumable 店（酒楼/餐厅/食堂）拒收丹药
            # （_is_medicine_item：heal_pct/回蓝/解毒/复活/五维药归药铺；feed 吃喝放行；
            # 技能书/cultivate/pet_food 另有口径，不在此拦）。
            if it is None or it.id in seen_ids or it.type == "key" \
                    or it.id in auc_ids \
                    or effective_category(it) == "cultivate":
                continue
            if (str(getattr(shop, "shop_type", "") or "") == "consumable"
                    and _is_medicine_item(it)):
                continue
            seen_ids.add(it.id)
            stock = _safe_int(g.get("stock"), 1, 0, 99) or 1
            shop.stock.append(ShopStockEntry(
                item_id=it.id,
                price=max(0, _safe_int(g.get("base_price"), 0, 0, 999999)),
                stock=stock, max_stock=min(99, stock + 3),
            ))

    def _ensure_type_coverage(self, shop: Shop, world: World) -> int:
        """[2026-08-31] 兜底：货架缺哪类补哪类（weapon/armor/consumable/material/accessory
        每类至少 1 件，cap 15 件）。LLM 备货未守「每类至少 1 件」约束时确定性补齐
        （按 id 排序取该类首件）。返回补的件数。
        [拍品隔离 2026-09-09] 候选排除拍卖拍品（拍品只走竞价，不上货架）。
        [酒楼分家 2026-09-10] consumable 店的 consumable 缺口只拿吃喝（feed）：
        无吃喝可补时留空，不拿丹药凑数（丹药归药铺）。"""
        from src.models.world import effective_category
        auc_ids = _auction_lot_ids(world)
        _TYPES = ("weapon", "armor", "consumable", "material", "accessory")
        on_shelf = {e.item_id for e in shop.stock}
        present = {next((it.type for it in world.items if it.id == iid), "")
                   for iid in on_shelf}
        added = 0
        _is_food_shop = str(getattr(shop, "shop_type", "") or "") == "consumable"
        for t in _TYPES:
            if t in present:
                continue
            if len(shop.stock) >= 15:
                break
            cands = sorted(
                [it for it in world.items
                 if it.type == t and it.id not in on_shelf
                 and it.id not in auc_ids
                 and effective_category(it) != "cultivate"],
                key=lambda it: it.id,
            )
            if _is_food_shop and t == "consumable":
                cands = [it for it in cands if _is_food_item(it)]
            if not cands:
                continue
            it = cands[0]
            shop.stock.append(ShopStockEntry(
                item_id=it.id, price=0, stock=1, max_stock=4))
            on_shelf.add(it.id)
            added += 1
        return added

    def sweep_auction_lots_from_shelves(self, world: World, shop_id: str = None) -> int:
        """[拍品隔离存量清扫 2026-09-09 补] 删掉货架上 item_id 命中 _auction_lot_ids 的条目。

        背景：隔离只堵了新增 6 口（选品清单/落架应用/品类补齐/目录兜底/游商料池+配方/
        真商发料），09-09 之前已上架的旧条目一直残留——tick/手动刷新全是增量补货，
        从不清旧货。本方法幂等、无 LLM、无 rng 消耗；只删货架条目，不动 world.items
        与 auction lots（拍品仍可竞价；ended 已拍出归玩家的也不受影响）。
        shop_id 传入时只扫该店（手动刷新按钮用），缺省扫全店（tick_shops 用）。
        返回删掉的条目数。调用点：tick_shops 头（全店，每 tick）+
        manual_vendor_refresh（进补货前，刷新按钮即时生效）。
        """
        auc_ids = _auction_lot_ids(world)
        if not auc_ids:
            return 0
        shops = [s for s in (getattr(world, "shops", None) or [])
                 if shop_id is None or getattr(s, "id", "") == shop_id]
        removed = 0
        for shop in shops:
            stock = getattr(shop, "stock", None) or []
            kept = [e for e in stock
                    if str(getattr(e, "item_id", "") or "") not in auc_ids]
            if len(kept) != len(stock):
                shop.stock = kept
                shop.touch()
                removed += len(stock) - len(kept)
        return removed

    def sweep_medicine_from_food_shops(self, world: World, shop_id: str = None) -> int:
        """[酒楼分家存量清扫 2026-09-10] 删掉 consumable 店货架上的丹药条目。

        背景：分家前 consumable 店（酒楼/餐厅/食堂）铺底/LLM 备货混装丹药，
        分家后只卖吃喝——旧丹药条目不清会一直残留（增量补货从不清旧货）。
        只删 consumable 店货架上 _is_medicine_item 命中的条目（feed 吃喝/技能书/
        cultivate/pet_food 不动）；不动 world.items。幂等、无 LLM、无 rng。
        shop_id 传入时只扫该店（手动刷新用），缺省扫全店（tick_shops 用）。
        返回删掉的条目数。
        """
        item_by_id = {it.id: it for it in (getattr(world, "items", None) or [])}
        shops = [s for s in (getattr(world, "shops", None) or [])
                 if (shop_id is None or getattr(s, "id", "") == shop_id)
                 and str(getattr(s, "shop_type", "") or "") == "consumable"]
        removed = 0
        for shop in shops:
            stock = getattr(shop, "stock", None) or []
            kept = [e for e in stock
                    if not _is_medicine_item(item_by_id.get(
                        str(getattr(e, "item_id", "") or "")))]
            if len(kept) != len(stock):
                shop.stock = kept
                shop.touch()
                removed += len(stock) - len(kept)
        return removed

    def tick_shops(self, world: World, preset: WorldSimPreset, tick: int) -> list[WorldEvent]:
        """[P6 -> P36b -> P45(1) -> 游商 2026-09-08] 商店周期子阶段。

        - 旧档真商人店：P36b 口径不变——系统退位，每周期只给存活商人发基础材料
          （其后合成/上架由 npc_life 子阶段自主完成）；商人死亡店停摆。
        - [游商] 游商店（无真商人）：走「系统直补」离线逻辑 _vendor_restock——进货
          （批发买料逻辑）+ 合成（基础配方需求驱动）+ 上架（vendor_made），全确定性
          纯 Python，零 LLM。游商不死不停摆；深短缺（稳定度 <25）同样衰减系统货源架。
        - 价格漂移：跳过非空 tag 条目（npc_made/vendor_made 定价权归生产者）。
        [P45(1)] 发料/进货按所在地稳定度分档（>=50 全额 2 件 / 25-49 紧张 1 件 /
        <25 短缺 0 件 + 系统货源架每周期 -1 + minor 短缺事件）。
        """
        if not self._per_world(world, "shops_enabled", preset.shops_enabled, preset):
            return []
        # [拍品隔离存量清扫] 先清后补：删 09-09 隔离前漏上架的旧拍品条目（幂等）。
        self.sweep_auction_lots_from_shelves(world)
        # [酒楼分家存量清扫 2026-09-10] consumable 店货架旧丹药条目一并清掉。
        self.sweep_medicine_from_food_shops(world)
        events: list[WorldEvent] = []
        loc_by_id = {l.id: l for l in world.locations}
        for shop in world.shops:
            if not tre.is_stale(shop, tick):
                continue
            rng = SeededRng.seed_from(world.id, tick, f"shop_{shop.id}")
            # [P45(1)] 稳定度分档：>=50 正常 2 件 / 25-49 紧张 1 件 / <25 短缺 0 件
            loc = loc_by_id.get(getattr(shop, "location_id", ""))
            supply = tre.stability_supply_level(getattr(loc, "stability", 50) if loc else 50)
            merchant = self._shop_merchant(world, shop)
            if merchant is not None and getattr(merchant, "alive", False):
                # [P36b] 旧档真商人：材料发放（其后的合成/上架由 npc_life 自主完成）。
                # [!] supply=0 须跳过：_grant_shop_materials 的 max(1, count or 2) 会把 0 吞成 2
                if supply > 0:
                    self._grant_shop_materials(world, merchant, rng, count=supply)
            elif supply > 0:
                # [游商] 游商店：系统直补离线逻辑（进货+合成+上架一条龙）。
                self._vendor_restock(world, shop, rng, count=supply, preset=preset)
            # [P45(1)] 深短缺（稳定度 <25）：系统货源架每周期 -1（战乱物资被征调/哄抢；
            # npc_made/vendor_made 条目不动——生产者亲手产的货归其所有）
            if supply <= 0:
                decayed = False
                for e in shop.stock:
                    if (getattr(e, "tag", "") or "") == "" and e.stock > 0:
                        e.stock -= 1
                        decayed = True
                if decayed:
                    shop.touch()
                    events.append(WorldEvent(
                        tick=tick, category="economy", severity="minor",
                        title=f"物资短缺：{shop.name}",
                        desc=f"「{loc.name if loc else '该地'}」战事未平，{shop.name}货架空了大半。",
                        locations=[getattr(loc, "id", "")] if loc else []))
            # [价格漂移] 补货周期同步漂移货架价格（模拟市场供需波动，破「价格僵死」）
            drift_level = self._per_world(world, "shops_price_drift", preset.shops_price_drift, preset)
            if drift_level != "off":
                item_map = {it.id: it for it in world.items}
                # 找不到 item 时返回 0（让 drift_prices 跳过），勿传 None 致 compute_item_price 算伪基准
                tre.drift_prices(shop, lambda iid: (tre.compute_item_price(item_map[iid])
                                     if iid in item_map else 0),
                                 world.id, tick, drift_level)
            shop.last_restock_tick = tick
            shop.touch()
        return events

    # [游商 2026-09-08] 游商离线进货/合成/上架相关常量（与 npc_life_engine P37/A2 同口径）：
    # - 合成蓝封顶：与 _BASIC_RARITIES 同集合（游商只造基础货）；
    # - 上架日上限：与 do_stock_shop 日 3 件同口径。
    # [!] _VENDOR_WHOLESALE_MULT 已废弃（进货改随机抽料不再按批发价排序挑）——删常量
    # 会动旧注释引用，保留空值仅防外部导入，勿再使用。
    _VENDOR_WHOLESALE_MULT = 0.6
    _VENDOR_MADE_TAG = "vendor_made"

    def _vendor_restock(self, world: World, shop, rng, count: int = 2,
                          preset: Optional[WorldSimPreset] = None) -> int:
        """[游商 2026-09-08] 游商店系统直补：虚拟进货 + 确定性合成 + 上架（纯 Python，零 LLM）。

        语义 = 假人替真商人跑完「进货->合成->上架」整条离线链（此前的 shops_llm_restock
        退位给据点真商人 NPC 用的那条 LLM 链，游商店不走）：
        1. 进货：按稳定度档位随机抽白绿材料（>=50 进 4 / 25-49 进 3 / <25 进 2，
           rng.pick 确定性可回放；游商无钱包/无背包概念——料直接进虚拟库存 dict，
           不落盘；深短缺不断供，短缺体感由系统架衰减+事件承担）。
        2. 合成：需求驱动选配方（[2026-09-08 修] 无视建筑门控 + 品级蓝封顶 +
           产出 level<=shop_max_item_level + 产出 type 属本店主营 _SHOP_TYPE_ITEM_TYPES
           优先 + 货架缺货优先；料够即成，成功率 100%——游商是功能不是人物，不 roll
           失败/大成功；门控只卡玩家手搓不卡进货渠道，高级货仍只走掉落/拍卖）。
        3. 上架：成品以 tag="vendor_made" 上架（stock=1/max=1；同品不叠；每周期
           最多 3 件；漂移/短缺衰减跳过——与 npc_made 同待遇）。
        4. 余料：合成剩下的料直接并入货架（按店型主营 material 口径当普通材料卖，
           系统条目 tag=""，参与漂移/衰减）。
        [拍品隔离 2026-09-09] 料池排除拍卖拍品（common 材料型拍品也不进不卖）。
        返回上架件数。best-effort：无配方/无材料返回 0 不抛异常。
        """
        # --- 虚拟库存：料（材料 id -> 份数）---
        # [!] 料口径 = type=="material" + 白绿品（与 npc_life do_buy_materials 同）——
        # forge/craft 细分只看显式 category，无标注的材料 default_category 回 "material"，
        # 若硬卡 forge/craft 会把未标注材料全排除导致游商永远无料可进。
        from src.models.world import effective_category
        stock: dict[str, int] = {}
        item_by_id = {it.id: it for it in (getattr(world, "items", None) or [])}
        auc_ids = _auction_lot_ids(world)   # [拍品隔离] 拍卖拍品不当材料进/上架

        def _is_shop_mat(it) -> bool:
            if getattr(it, "type", "") != "material":
                return False
            if getattr(it, "rarity", "") not in ("common", "uncommon"):
                return False
            if it.id in auc_ids:
                return False
            c = effective_category(it)
            # [!] 种子（category=seed）留作播种不当材料（与 _init_recipes P35 口径同）
            if c == "seed":
                return False
            return c in ("forge", "craft", "material")

        # [游商 2026-09-08] 进货档位 4/3/2（用户指示：旧 2/1/0 太少，游商是功能不是
        # 人物，供货要跟上买速）：稳定度>=50 进 4 件 / 25-49 进 3 件 / <25 进 2 件
        # （深短缺不断供——游商不死不停摆，与真商人「<25 停发」不同；短缺体感由系统
        # 货源架衰减 + 短缺事件承担）。count 入参忽略，档位由本函数按稳定度自算。
        loc_here = next((l for l in (getattr(world, "locations", None) or [])
                         if getattr(l, "id", "") == str(getattr(shop, "location_id", "") or "")),
                        None)
        _stab = int(getattr(loc_here, "stability", 50) if loc_here is not None else 50)
        _quota = 4 if _stab >= 50 else (3 if _stab >= 25 else 2)
        # --- 候选配方先行（进货按配方需求拿料，而非纯随机碰运气）---
        # [2026-09-08 修] 无视建筑门控 + 品级蓝封顶 + 产出 level<=shop_max_item_level
        # （默认 3，与 _seed_shop_from_catalog 同口径）——游商背后是整条商路作坊，
        # 门控只卡玩家手搓不卡进货渠道；高级货仍只走掉落/拍卖。
        max_lvl = max(0, int(getattr(preset, "shop_max_item_level", 3) or 0)
                      if preset is not None else 3)

        def _recipe_ok(r) -> bool:
            o = item_by_id.get(str(getattr(r, "output_item_id", "") or ""))
            if o is None:
                return False
            if o.id in auc_ids:   # [拍品隔离] 配方产出指向拍品 -> 不造
                return False
            if getattr(o, "rarity", "") not in ("common", "uncommon", "rare"):
                return False
            if max_lvl > 0 and int(getattr(o, "level", 0) or 0) > max_lvl:
                return False
            return True

        recipes = [r for r in (getattr(world, "recipes", None) or []) if _recipe_ok(r)]
        # [酒楼分家 2026-09-10] consumable 店只造吃喝配方（产出 feed）：丹药配方
        # 归药铺造。技能书/宠物食品配方本来就不在游商料口径内，不受影响。
        if str(getattr(shop, "shop_type", "") or "") == "consumable":
            recipes = [r for r in recipes
                       if _is_food_item(item_by_id.get(
                           str(getattr(r, "output_item_id", "") or "")))]
        wanted_types = _SHOP_TYPE_ITEM_TYPES.get(
            str(getattr(shop, "shop_type", "") or "general"),
            {"weapon", "armor", "consumable", "material"})
        mats = [it for it in item_by_id.values() if _is_shop_mat(it)]
        # 1) 进货：需求驱动拿料——缺货配方（主营对口 + 货架缺货优先，与合成同排序）
        # 所缺的输入优先拿；缺口补满后剩余配额随机抽（rng 确定性可回放）。
        # [!] 需求料不受 quota 截断：quota 只管随机抽的部分——否则需求单 6 种输入、
        # quota 4 时后两种永远拿不到，能做的配方永远做不出（大明档武器店实锤）。
        # 同种材料可重复拿 = 多备几份同种料，契合多份输入的配方。
        _need_order = self._vendor_need_order(shop, recipes, wanted_types, item_by_id,
                                              max_lvl=max_lvl)
        for iid in _need_order:
            if any(it.id == iid for it in mats):
                stock[iid] = stock.get(iid, 0) + 1
        for _ in range(max(0, _quota - sum(stock.values()))):
            pick = rng.pick(mats) if mats else None
            if pick is None:
                break
            stock[pick.id] = stock.get(pick.id, 0) + 1
        if not stock:
            return 0
        if not recipes:
            # 无配方可造 -> 余料直接上架（当普通材料卖）
            return self._vendor_shelf_leftovers(world, shop, stock)
        stocked_ids = set()
        shelf_types = set()
        for e in (getattr(shop, "stock", None) or []):
            if int(getattr(e, "stock", 0) or 0) <= 0:
                continue               # 售罄不算「在架」
            stocked_ids.add(str(getattr(e, "item_id", "") or ""))
            it = item_by_id.get(str(getattr(e, "item_id", "") or ""))
            if it is not None:
                shelf_types.add(str(getattr(it, "type", "") or ""))

        def _out_type(r) -> str:
            o = item_by_id.get(str(getattr(r, "output_item_id", "") or ""))
            return str(getattr(o, "type", "") or "") if o is not None else ""

        # 缺货配方排序（进货拿料与合成选品共用同一优先级：主营对口 + 货架缺类
        # 优先 + 同层配方名稳定排序，确定性）。
        # [!] general/auction 杂货店主营全品类：gap 永空（货架必含各 type），直接走
        # 主营内按配方名排序——杂货店不追品类缺口，只补卖光的单品（rest 分支）。
        ordered = sorted(recipes, key=lambda r: str(getattr(r, "name", "") or ""))
        _st = str(getattr(shop, "shop_type", "") or "general")
        if _st in ("general", "auction"):
            ranked = [r for r in ordered
                      if str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
        else:
            gap = [r for r in ordered
                   if _out_type(r) in wanted_types and _out_type(r) not in shelf_types
                   and str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
            main = [r for r in ordered
                    if _out_type(r) in wanted_types
                    and str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
            rest = [r for r in ordered
                    if str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
            ranked = gap or main or rest

        def _can_make(r) -> bool:
            return all(stock.get(str(i), 0) > 0 for iid in (getattr(r, "inputs", None) or [])
                       for i in [str(iid)])
        # 2) 合成：缺货优先，收集全部料够的配方后 rng 随机挑 1 个（[!] 不能只取
        # 排序首个——头部配方缺料（rare 材料进不了货）时会堵死后面能做的，大明档
        # 武器店实锤；也不能全做——游商每周期 1 件成品，货架靠多周期轮换）。
        made = 0
        _makeable = [x for x in ranked if _can_make(x)]
        _pick = rng.pick(_makeable) if _makeable else None
        for r in [_pick] if _pick is not None else []:
            for iid in (getattr(r, "inputs", None) or []):
                k = str(iid)
                stock[k] = stock.get(k, 1) - 1
                if stock[k] <= 0:
                    stock.pop(k, None)
            out_id = str(getattr(r, "output_item_id", "") or "")
            out = item_by_id.get(out_id)
            if out is None:
                continue
            # 售罄的同品条目（系统条或旧 vendor_made）-> 直接补 1 售卖，不另起条目
            refill = next((e for e in (getattr(shop, "stock", None) or [])
                           if str(getattr(e, "item_id", "") or "") == out_id
                           and int(getattr(e, "stock", 0) or 0) <= 0
                           and int(getattr(e, "max_stock", 0) or 0) > 0), None)
            if refill is not None:
                refill.stock = min(int(refill.max_stock or 1), int(refill.stock or 0) + 1)
            elif out_id not in stocked_ids:
                shop.stock.append(ShopStockEntry(item_id=out_id, price=0, stock=1,
                                                 max_stock=1, tag=self._VENDOR_MADE_TAG))
                stocked_ids.add(out_id)
            else:
                continue
            made += 1
            if made >= 3:
                break
        # 3) 上架 vendor_made（touch 由调用方统一做，此处只记数）
        # 4) 余料并入货架（当普通材料卖）
        left = self._vendor_shelf_leftovers(world, shop, stock)
        if made:
            shop.touch()
        return made + left

    @staticmethod
    def _vendor_need_order(shop, recipes: list, wanted_types: set, item_by_id: dict,
                           max_lvl: int = 3) -> list:
        """[游商] 进货需求单：缺货配方排序（主营对口 + 货架缺类优先 + 配方名稳定排序，
        与合成选品同优先级）各自所需输入 id 依次展开。纯函数（进货拿料用；合成直接
        用 ranked 列表，排序口径见 _vendor_restock）。

        [!] 只排「可造配方」（与合成候选同口径：蓝封顶 + 产出 level<=max_lvl）——
        否则天罡碎岳枪（mythic，需星辰铁/龙骨石）这类永远造不出的配方会把需求单
        头部堵死，真正能做的配方输入永远拿不到料（大明档武器店实锤）。
        """
        stocked_ids = set()
        shelf_types = set()
        for e in (getattr(shop, "stock", None) or []):
            if int(getattr(e, "stock", 0) or 0) <= 0:
                continue               # 售罄不算「在架」
            stocked_ids.add(str(getattr(e, "item_id", "") or ""))
            it = item_by_id.get(str(getattr(e, "item_id", "") or ""))
            if it is not None:
                shelf_types.add(str(getattr(it, "type", "") or ""))

        def _out_type(r) -> str:
            o = item_by_id.get(str(getattr(r, "output_item_id", "") or ""))
            return str(getattr(o, "type", "") or "") if o is not None else ""

        def _craftable(r) -> bool:
            o = item_by_id.get(str(getattr(r, "output_item_id", "") or ""))
            if o is None:
                return False
            if getattr(o, "rarity", "") not in ("common", "uncommon", "rare"):
                return False
            if max_lvl > 0 and int(getattr(o, "level", 0) or 0) > max_lvl:
                return False
            return True

        ordered = sorted((r for r in recipes if _craftable(r)),
                         key=lambda r: str(getattr(r, "name", "") or ""))
        # [!] general/auction 杂货店不追品类缺口（主营全品类，gap 永空无意义），
        # 直接按配方名排序补卖光的单品——与 _vendor_restock 的 ranked 同口径。
        _st = str(getattr(shop, "shop_type", "") or "general")
        if _st in ("general", "auction"):
            ranked = [r for r in ordered
                      if str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
        else:
            gap = [r for r in ordered
                   if _out_type(r) in wanted_types and _out_type(r) not in shelf_types
                   and str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
            main = [r for r in ordered
                    if _out_type(r) in wanted_types
                    and str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
            rest = [r for r in ordered
                    if str(getattr(r, "output_item_id", "") or "") not in stocked_ids]
            ranked = gap or main or rest
        need: list = []
        for r in ranked:
            for iid in (getattr(r, "inputs", None) or []):
                k = str(iid)
                if k not in need:
                    need.append(k)
        return need

    def _vendor_shelf_leftovers(self, world: World, shop, stock: dict) -> int:
        """[游商] 虚拟库存余料并入货架（普通系统条目 tag=""，参与漂移/衰减）。

        同品已有条目 -> 按余料份数补（不超 max_stock）；无条目 -> 新建
        （stock=余料份数钳 max_stock=4）。
        材料只上本店主营口径（_SHOP_TYPE_ITEM_TYPES 含 material 的店：consumable/
        material/general/magic/auction；weapon/armor/alchemy 专营店余料不出售，
        直接丢弃——专营店只卖成品，符合「卖什么的卖什么」）。
        """
        wanted_types = _SHOP_TYPE_ITEM_TYPES.get(
            str(getattr(shop, "shop_type", "") or "general"),
            {"weapon", "armor", "consumable", "material"})
        if "material" not in wanted_types:
            return 0
        item_by_id = {it.id: it for it in (getattr(world, "items", None) or [])}
        added = 0
        for iid in sorted(stock.keys()):
            qty = int(stock.get(iid, 0) or 0)
            if qty <= 0:
                continue
            it = item_by_id.get(iid)
            if it is None or getattr(it, "type", "") != "material":
                continue
            entry = next((e for e in (getattr(shop, "stock", None) or [])
                          if str(getattr(e, "item_id", "") or "") == iid), None)
            if entry is not None:
                room = int(getattr(entry, "max_stock", 0) or 0) - int(getattr(entry, "stock", 0) or 0)
                put = max(0, min(qty, room))
                if put > 0:
                    entry.stock = int(entry.stock or 0) + put
                    added += put
            else:
                put = max(1, min(qty, 4))
                shop.stock.append(ShopStockEntry(item_id=iid, price=0, stock=put,
                                                 max_stock=4))
                added += put
        if added:
            shop.touch()
        return added

    def _shop_merchant(self, world: World, shop) -> Optional["NPC"]:
        """[P36b] 商店的商人 NPC（merchant_npc_id 优先，回退 is_merchant 且 shop_id 挂钩者）。"""
        mid = str(getattr(shop, "merchant_npc_id", "") or "")
        for n in (getattr(world, "npcs", None) or []):
            if getattr(n, "id", "") == mid:
                return n
        for n in (getattr(world, "npcs", None) or []):
            if getattr(n, "shop_id", "") == getattr(shop, "id", "")                     and getattr(n, "is_merchant", False):
                return n
        return None

    def _grant_shop_materials(self, world: World, merchant, rng, count: int = 2) -> int:
        """[P36b] 向商人发放基础材料（系统源头，替代系统直接上架；背包 cap 内去重发放）。
        [拍品隔离 2026-09-09] 料池排除拍卖拍品（拍品只归拍卖会，不发不卖）。"""
        from src.models.world import effective_category
        # [!] 空列表 or [] 会换成新引用致追加落空（防 0 吞同族）——None 才重建并写回
        inv = getattr(merchant, "inventory", None)
        if inv is None:
            inv = []
            merchant.inventory = inv
        auc_ids = _auction_lot_ids(world)
        mats = [i.id for i in (getattr(world, "items", None) or [])
                if getattr(i, "type", "") == "material"
                and effective_category(i) in ("forge", "craft")
                and i.id not in auc_ids
                and getattr(i, "rarity", "") in ("common", "uncommon")]
        if not mats:
            return 0
        given = 0
        for _ in range(max(1, int(count or 2))):
            if len(inv) >= nle._NPC_INV_CAP:
                break
            mid = rng.pick(mats)
            if mid and mid not in inv:
                inv.append(mid)
                given += 1
        return given

    def _backfill_dead_npcs(self, world: World, preset: Optional[WorldSimPreset] = None,
                            tick: Optional[int] = None,
                            spawned_out: Optional[list] = None) -> list:
        """[P46 用户指示 2026-08-23] NPC 死亡补员：npc_permadeath 开启时死者由新人顶替。

        功能判断口径（用户授权自行判断）：
        - [游商 2026-09-08] 旧档真商人（店铺绑定可解析，merchant_npc_id/shop_id 挂钩）
          -> 接班人接手店铺（同旧口径，商店不停摆）；新世界游商店无商人 NPC，店不
          停摆（常驻经营），死者走下面的平民分支——游商假 NPC 本来就不死。
        - 其余非敌对非要角 -> 同角色随机新人（题材姓名库去重挑选）补到死者地点，
          平民基线（ensure_life_baseline）；
        - 敌对单位不补（玩家战果不刷新，野外怪另有动态供给）；要角不补（剧情死亡
          留空由叙事消化，任务链不动）。
        时机：死者 respawn_at_tick 到期时补（permadeath 下该水位悬置不消费，正好
        作「新人到岗延迟」）；World.backfilled_npc_ids 记已补者防重复。
        引擎只出功能骨架（姓名库占位名 + 模板小传）；语义档案（真名/性格/目标/外貌/
        腔调/小传）由 _generate_backfill_profiles LLM 批量重生成（tick LLM 段，预算
        门控；失败/取消回退引擎默认）——守「LLM 出语义、引擎算结构」铁律。
        spawned_out 非空时收集 {succ, event, context, loc} 供 LLM 段消费。
        返回 WorldEvent 列表（category="npc" minor，进右侧 NPC 动态栏）。
        """
        events: list[WorldEvent] = []
        spawned: list = []
        if not self._per_world(world, "npc_permadeath",
                               preset.npc_permadeath if preset else False, preset):
            return events   # 关=走正常重生，无需补员
        if not self._per_world(world, "npc_backfill_enabled",
                               preset.npc_backfill_enabled if preset else True, preset):
            return events
        t = int(tick if tick is not None else world.tick_count)
        done_ids = set(world.backfilled_npc_ids or [])
        genre_id = _p34a_genre_id(world)
        loc_by_id = {l.id: l for l in world.locations}
        existing_names = {n.name for n in world.npcs}
        from src.utils.rng import SeededRng as _SRB
        for npc in list(world.npcs):
            if npc.alive or npc.id in done_ids:
                continue
            if getattr(npc, "hostile", False) or getattr(npc, "is_key_npc", False):
                continue                     # 敌对/要角不补（见 docstring 口径）
            rt = int(getattr(npc, "respawn_at_tick", 0) or 0)
            if rt <= 0 or t < rt:
                continue                     # 未到「到岗」时点
            rng = _SRB.seed_from(str(world.id), t, f"backfill_{npc.id}")
            # 商店绑定解析（merchant_npc_id 正向 / shop_id 反向）。[审查加固] 反向解析
            # 跳过已由其他存活商人接手的店（同店双死者边界：防二次生成接班人）。
            shop = next((s for s in world.shops
                         if s.merchant_npc_id == npc.id), None)
            if shop is None and getattr(npc, "shop_id", ""):
                shop = next((s for s in world.shops
                             if s.id == npc.shop_id
                             and (not s.merchant_npc_id
                                  or s.merchant_npc_id == npc.id
                                  or not next((n2.alive for n2 in world.npcs
                                               if n2.id == s.merchant_npc_id), False))), None)
            # 姓名去重挑选（题材库滤掉现存名；耗尽时加序号后缀兜底——重名会破坏按名
            # 解析的唯一性：cand_by_name / nrs.resolve_name / 任务 giver 锚定全依赖名字唯一）
            pool = [x for x in (_GENRE_NPC_NAMES.get(genre_id)
                                or _GENRE_NPC_NAMES["western_fantasy"])
                    if x not in existing_names]
            if pool:
                new_name = rng.pick(pool)
            else:
                base = rng.pick(list(_GENRE_NPC_NAMES.get(genre_id)
                                     or _GENRE_NPC_NAMES["western_fantasy"]))
                _n = 2
                new_name = f"{base}{_n}"
                while new_name in existing_names:
                    _n += 1
                    new_name = f"{base}{_n}"
            existing_names.add(new_name)
            if shop is not None:
                # 接班人：接手店铺（货底继承 + 开业本金，P37 冷启动口径）
                loc = loc_by_id.get(shop.location_id)
                succ = NPC(
                    name=new_name, role=(npc.role or "商人"),
                    desc=f"接手「{shop.name}」的新店主。",
                    location_id=shop.location_id,
                    place_id=(getattr(loc, "default_place_id", "") or ""),
                    faction_id=(npc.faction_id or ""), alive=True, hostile=False,
                )
                succ.is_merchant = True
                succ.shop_id = shop.id
                succ.shop_type = (getattr(npc, "shop_type", "") or shop.shop_type or "general")
                succ.inventory = list(npc.inventory or [])[:nle._NPC_INV_CAP]   # 货底交接
                succ.wallet = rng.roll(100, 300)
                shop.merchant_npc_id = succ.id
                shop.touch()
                world.npcs.append(succ)
                if loc is not None:
                    if succ.id not in loc.npc_ids:
                        loc.npc_ids.append(succ.id)
                    for pl in loc.places:
                        if isinstance(pl, Place) and pl.id == succ.place_id \
                                and succ.id not in pl.npc_ids:
                            pl.npc_ids.append(succ.id)
                            break
                nle.ensure_life_baseline(succ)
                ev = WorldEvent(
                    tick=t, category="npc", severity="minor",
                    title=f"新面孔：{succ.name}",
                    desc=f"{succ.name} 接手了{shop.name}，{npc.name}留下的生意后继有人。",
                    npcs=[succ.id], locations=[shop.location_id])
                events.append(ev)
                spawned.append({"succ": succ, "event": ev,
                                "loc": (loc.name if loc is not None else "某地"),
                                "context": f"接替死者{npc.name}（{npc.role or '商人'}）接手店铺「{shop.name}」"})
            else:
                # 非功能 NPC：同角色随机新人补位
                loc = loc_by_id.get(npc.location_id)
                loc_name = loc.name if loc is not None else "此地"
                succ = NPC(
                    name=new_name, role=(npc.role or "平民"),
                    desc=f"来到{loc_name}的{npc.role or '平民'}，填补了{npc.name}留下的空缺。",
                    location_id=npc.location_id, place_id=(npc.place_id or ""),
                    faction_id=(npc.faction_id or ""), alive=True, hostile=False,
                )
                world.npcs.append(succ)
                if loc is not None and succ.id not in loc.npc_ids:
                    loc.npc_ids.append(succ.id)
                nle.ensure_life_baseline(succ)
                ev = WorldEvent(
                    tick=t, category="npc", severity="minor",
                    title=f"新面孔：{succ.name}",
                    desc=f"{succ.name} 来到{loc_name}，顶替了{npc.name}（{npc.role or '平民'}）的位置。",
                    npcs=[succ.id], locations=[npc.location_id])
                events.append(ev)
                spawned.append({"succ": succ, "event": ev, "loc": loc_name,
                                "context": f"顶替死者{npc.name}（{npc.role or '平民'}）留下的空缺"})
            done_ids.add(npc.id)
            npc.respawn_at_tick = 0   # 已补员，清悬置水位（保持「永久死亡」语义一致）
        if done_ids:
            world.backfilled_npc_ids = sorted(done_ids | set(world.backfilled_npc_ids or []))
        if spawned_out is not None:
            spawned_out.extend(spawned)
        return events

    def _generate_backfill_profiles(self, world: World, preset: WorldSimPreset,
                                    entries: list,
                                    cancel_check: Optional[Callable[[], bool]] = None
                                    ) -> "Optional[bool]":
        """[P46 用户指示 2026-08-23] 接班人/补位新人档案 LLM 批量重生成（一次调用）。

        引擎骨架已保证功能接续（店铺绑定/数值），本调用只补语义档案：真名/身份/
        性格/目标/外貌/腔调/小传——与世界生成时的 NPC 档案同质量（用户指示：新的
        NPC 相关信息应由 LLM 重新生成）。user 消息带世界观锚（守 §23 联动）。
        改名同步修正已发事件标题/描述（引擎段先发事件用占位名）。
        返回 None=取消/无 API（不占预算）；True=已调用（应用成功或失败回退占位档案）。
        """
        # API 两级兜底与其余 tick LLM 段同口径（每世界 sim_api_id 优先，缺省回退计算器 API）
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api or (cancel_check and cancel_check()):
            return None
        fac_by_id = {f.id: f.name for f in world.factions}
        lines = []
        for ent in entries:
            succ = ent["succ"]
            lines.append(f"- 占位名:{succ.name}（{ent['context']}；所在:{ent['loc']}；"
                         f"势力:{fac_by_id.get(succ.faction_id, '无')}）")
        sys_prompt = (
            "你是 SLG 游戏的 NPC 生成引擎。某地有人死去，需要为其接班人/顶替者生成完整人物档案。"
            "输出严格 JSON，不要任何说明或代码块标记。\n\n"
            "JSON schema：\n"
            '{"successors": [{"ref": "占位名(原样返回)", "name": "新姓名(2-12字，符合题材命名风格)", '
            '"role": "身份(一句话，如 铁匠商人/游方郎中；接手店铺者须体现店主身份)", '
            '"personality": "性格(30字内)", "goal": "目标(30字内)", "appearance": "外貌(60字内)", '
            '"speech_style": "说话风格(50字内：整体语气与用词倾向的泛义描述，如「语气冷硬、爱用反问」；与其他 NPC 有区分度；禁止写具体口头禅/固定台词/每轮都要说的某句话)", '
            '"notes": "1-2句背景小传(交代来历与为何到此顶替)"}]}\n\n'
            "要求：贴合世界观与势力立场；性格/腔调可辨识有区分度；小传呼应其顶替对象的空缺，"
            "不编造与设定矛盾的内容。"
        )
        tmp = Preset(name="world_sim_backfill_profile", system_prompt=sys_prompt,
                     temperature=preset.calculator_temperature,
                     max_tokens=max(10000, preset.calculator_max_tokens),
                     top_p=preset.calculator_top_p)
        # 读超时对齐档案生成 600s（大 JSON + thinking 模型口径同 generate_npc_profiles）
        llm = LlmClient(api, tmp, timeout=600.0, jailbreak_prefix=self._jb_prefix())
        user = (_world_anchor(world) + "\n"
                "需要生成档案的新人：\n" + "\n".join(lines)
                + "\n请输出 successors JSON。")
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user}]
        result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                     block_labels=["系统", "用户"])
        if result.cancelled:
            return None
        if (result.error or "") or not (result.content or "").strip():
            return True   # 失败也计一次预算（口径同股市三态：防下 tick 重复撞）
        data = _extract_json(result.content)
        succ_list = data.get("successors") if isinstance(data, dict) else None
        if not isinstance(succ_list, list):
            return True
        by_ref = {ent["succ"].name: ent for ent in entries}
        existing = {n.name for n in world.npcs}
        for p in succ_list:
            if not isinstance(p, dict):
                continue
            ref = str(p.get("ref") or "").strip()
            ent = by_ref.get(ref)
            if ent is None:
                hit = nrs.resolve_name(ref, list(by_ref.keys()))
                ent = by_ref.get(hit) if hit else None
            if ent is None:
                continue
            succ = ent["succ"]
            new_name = str(p.get("name") or "").strip()
            if 2 <= len(new_name) <= 12 and new_name != succ.name and new_name not in existing:
                existing.add(new_name)
                old = succ.name
                succ.name = new_name
                ev = ent.get("event")
                if ev is not None:   # 已发事件同步改名（引擎段先发用的占位名）
                    ev.title = ev.title.replace(old, new_name)
                    ev.desc = (ev.desc or "").replace(old, new_name)
            role = str(p.get("role") or "").strip()
            if role:
                succ.role = role[:30]
            personality = str(p.get("personality") or "").strip()
            if personality:
                succ.personality = personality[:60]
            goal = str(p.get("goal") or "").strip()
            if goal:
                succ.goal = goal[:60]
            appearance = str(p.get("appearance") or "").strip()
            if appearance:
                succ.appearance = appearance[:120]
            ss = _sanitize_speech_style(p.get("speech_style"))
            if ss:
                succ.speech_style = ss[:60]
            notes = str(p.get("notes") or "").strip()
            if notes:
                succ.notes = notes[:200]
        return True

    # ---- [⑥ 2026-08-30] NPC 人格演化（全体 NPC；引擎判转折点，LLM 写「他因此变成了什么样」）----
    def _collect_drift_candidates(self, world: World, tick: int,
                                  preset: WorldSimPreset) -> list:
        """[⑥] 转折点检测：扫描 event_log 新增（水位后）major/crisis 事件，把涉事 NPC 列为候选。

        返回 [(npc, 触发事件摘要)]。每 NPC 冷却（last_drift_tick）过滤防频繁重写。
        [!] 不推进 drift_watermark_tick（由 _tick_personality_drift 在 LLM 尝试后推进），
        避免取消/无 API 时吞掉候选。
        """
        wm = int(getattr(world, "drift_watermark_tick", 0) or 0)
        cands = {}  # npc_id -> 触发事件摘要（取最新一条）
        for e in (getattr(world, "event_log", None) or []):
            t = int(getattr(e, "tick", 0) or 0)
            if t <= wm or str(getattr(e, "severity", "") or "") not in ("major", "crisis"):
                continue
            for nid in (getattr(e, "npcs", None) or []):
                cands[str(nid)] = f"{getattr(e, 'title', '')}：{getattr(e, 'desc', '')}"[:100]
        interval = max(1, int(self._per_world(world, "personality_drift_interval", 12, preset)))
        out = []
        for n in world.npcs:
            nid = str(n.id)
            if nid not in cands or not getattr(n, "alive", True):
                continue
            last = int(getattr(n, "last_drift_tick", 0) or 0)
            if last > 0 and (tick - last) < interval:
                continue
            out.append((n, cands[nid]))
        return out

    def _arc_knobs(self, world: World, preset: WorldSimPreset) -> tuple:
        """[剧情线 P46] 读旋钮并钳制，返回 (enabled, interval, max_active, max_new)。
        [!] wiring 在 _phase 之外求值，脏 overlay 值必须在此吞下（不炸 tick_world）。"""
        def _iv(key, dflt, lo, hi):
            try:
                return max(lo, min(hi, int(self._per_world(world, key, dflt, preset))))
            except (TypeError, ValueError):
                return dflt
        enabled = bool(self._per_world(world, "story_arcs_enabled",
                                       preset.story_arcs_enabled, preset))
        return (enabled, _iv("arc_interval_days", 5, 1, 30),
                _iv("arc_max_active", 4, 0, 12), _iv("arc_max_new", 2, 1, 4))

    def _tick_story_arcs_gen(self, world: World, preset: WorldSimPreset, tick: int,
                             cancel_check=None) -> bool:
        """[剧情线 P46] 剧情线规划一轮（批量 1 次 LLM；三态仿 _plan_npc_life_llm/股市）。
        返回是否计 1 LLM 预算。"""
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        if cancel_check and cancel_check():
            return False                    # 取消：不推水位不出线（同 tick 内轻量重试）
        _, _, _, max_new = self._arc_knobs(world, preset)
        tpl_rng = SeededRng.seed_from(world.id, day, "story_arc_tpl")

        def _fallback() -> None:
            # [!] 水位先记：兜底取材若抛（脏 world 数据）也绝不形成「每 tick 撞 LLM」风暴
            world.last_arc_day = day
            try:
                made = sae.plan_fallback_arcs(world, tpl_rng)
                if made:
                    # [P46 v1.1 复查修复 qwen] 兜底受 **并行上限** 约束（原错拿 max_new：
                    # 无 API 世界 active>=2 后兜底永久不出线）
                    _room = max(0, self._arc_knobs(world, preset)[2]
                                - len(sae.active_arcs(world)))
                    world.story_arcs.extend(made[:max(0, _room)])
            except Exception as e:  # noqa: BLE001
                debug_log(lambda: f"[WorldSim] 剧情线模板兜底异常（跳过）: {e}")

        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if api is None:
            _fallback()                     # 无 API：重试也是空转，直接保底故事；不计预算
            return False
        from src.models.world_sim_preset import DEFAULT_STORY_ARC_PROMPT
        tmp_preset = Preset(
            name="world_sim_story_arcs",
            system_prompt=str(getattr(preset, "story_arc_prompt", "") or "")
                          or DEFAULT_STORY_ARC_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        assigned = sae._pick_archetypes(world, tpl_rng, 2)   # [!] 须在 user_msg 之前
        try:
            user_msg = self._build_arc_gen_user_message(
                world, assigned, interval_days=self._arc_knobs(world, preset)[1])
        except Exception as e:  # noqa: BLE001
            debug_log(lambda: f"[WorldSim] 剧情线消息拼装异常（走兜底）: {e}")
            _fallback()
            return True
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user_msg}]
        # [P46.1 v1.2 审核修复] 标题查重覆盖全部线（含终态）
        # [P46.1 v1.2 审核修复 GLM P1-1 / qwen P1①] 本批原型指派：引擎选 2 个
        # 冷却外可发性原型写进 prompt（附骨架说明），LLM 只做世界化填充并回带
        # archetype 字段——防同质化从 prompt 到 sanitize 全链贯通。
        # [审核修复 2026-09-13] 标题查重失败只降级为空集（不查重），绝不向上抛——
        # 此处抛异常会被 tick_world._phase 静默吞掉且不推水位，等于剧情线永久停产。
        try:
            existing = {a.title for a in (getattr(world, "story_arcs", None) or [])
                        if isinstance(a, StoryArc)}
        except Exception as e:  # noqa: BLE001
            debug_log(lambda: f"[WorldSim] 剧情线标题查重异常（本批不查重）: {e}")
            existing = set()
        # [审核修复 2026-09-13] 规划主链整段兜底：LLM 调用/解析/清洗任一步抛异常时，
        # 都退到模板线并推水位——绝不让异常冒到 tick_world._phase 被静默吞掉
        # （那会导致水位不推进 + 剧情线永久停产，玩家侧零读数）。
        try:
            for attempt in range(2):
                if cancel_check and cancel_check():
                    return False
                result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                             block_labels=["系统", "用户"])
                if result.cancelled:
                    return False            # 取消：不动水位（本轮作废，下 tick 重试）
                data = None
                if not (result.error or not (result.content or "").strip()):
                    data = _extract_json(result.content)
                if (isinstance(data, dict) and data.get("mode") == "story_arc"
                        and isinstance(data.get("arcs"), list)):
                    made = sae.sanitize_llm_arcs(world, data, tpl_rng, day, existing,
                                                 assigned=assigned)
                    # 清洗后全空 = 有效产出但全部无据（宁缺毋滥，不复活兜底）
                    _room = max(0, self._arc_knobs(world, preset)[2] - len(sae.active_arcs(world)))
                    world.story_arcs.extend(made[:min(max_new, _room)])
                    world.last_arc_day = day
                    return True
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 mode/arcs "
                                                    "字段，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
        except Exception as e:  # noqa: BLE001
            debug_log(lambda: f"[WorldSim] 剧情线规划主链异常（走模板兜底）: {e}")
        _fallback()                         # 两次失败/异常：模板线兜底 + 推水位 + 计 1 预算
        return True

    def _build_arc_replan_user_message(self, world: World, arc: StoryArc) -> str:
        """S03：只给固定规模的已发生事实与未开始阶段，不随世界年龄增长。"""
        stages = [s for s in (arc.stages or [])[int(arc.current_stage):]
                  if isinstance(s, StoryStage) and not s.done and s.started_tick < 0]
        people = {str(n.id): n for n in (getattr(world, "npcs", None) or [])}
        member_lines = []
        for nid in [str(arc.mastermind_id)] + [str(x) for x in (arc.participant_ids or [])]:
            n = people.get(nid)
            if n is not None:
                member_lines.append(f"- {n.name}（id={nid}；"
                                    f"{'在世' if getattr(n, 'alive', True) else '已故'}；"
                                    f"所在地={getattr(n, 'location_id', '')}）")
        done_lines = []
        for s in [x for x in (arc.stages or []) if isinstance(x, StoryStage) and x.done][-3:]:
            done_lines.append(f"- id={s.id}；{s.name}；实际结果={s.outcome or '未定'}；"
                              f"结算=第{int(s.resolved_tick) // 12 + 1}天"
                              f"（回合{s.resolved_tick}）；事实={s.desc[:100]}")
        qs = {str(q.id): q for q in (getattr(world, "quests", None) or [])}
        contributions = []
        for item in (arc.interventions or [])[-3:]:
            q = qs.get(str(item.get("quest_id", "") or ""))
            contributions.append(f"- 阶段 {item.get('stage_id', '')}："
                                 f"{'帮对立方' if item.get('stance') == 'oppose' else '帮策划者'}；"
                                 f"任务={getattr(q, 'title', '') or item.get('quest_id', '')}；"
                                 f"发生于第{int(item.get('tick', 0)) // 12 + 1}天"
                                 f"（回合{item.get('tick', 0)}）")
        loc_names = [str(l.name) for l in (getattr(world, "locations", None) or [])
                     if getattr(l, "name", "")][:8]
        mat_names = [str(i.name) for i in (getattr(world, "items", None) or [])
                     if getattr(i, "type", "") == "material" and getattr(i, "name", "")][:8]
        dg_names = [str(d.name) for d in (getattr(world, "dungeons", None) or [])
                    if getattr(d, "name", "") and str(getattr(d, "status", "")) == "open"][:6]
        offered_people = [str(n.name) for n in people.values()
                          if getattr(n, "alive", True) and getattr(n, "name", "")][:12]
        for nid in [str(arc.mastermind_id)] + [str(x) for x in (arc.participant_ids or [])]:
            n = people.get(nid)
            if n is not None and getattr(n, "alive", True) and str(n.name) not in offered_people:
                offered_people.append(str(n.name))
        future_lines = [f"- id={s.id}；原名={s.name}；原内容={s.desc[:100]}；"
                        f"kind={s.kind}；days={s.days}；"
                        f"截止=第{int(s.due_tick) // 12 + 1}天（回合{s.due_tick}）；"
                        f"原介入点={s.hook or '无'}" for s in stages]
        return "\n\n".join([
            f"{_world_anchor(world)}\n【世界时间】{_fmt_time_weather(world)}",
            f"【续写对象】arc_id={arc.id}；revision={arc.revision}；标题={arc.title}；"
            f"前提={arc.premise}；原型={arc.archetype}；续写原因={arc.replan_reason or '事态变化'}",
            "【当事人当前状态】\n" + ("\n".join(member_lines) or "无"),
            "【已发生，绝不可改写】\n" + ("\n".join(done_lines) or "尚无已结算阶段"),
            "【玩家已确认介入】\n" + ("\n".join(contributions) or "无"),
            "【可用目标名单】人物=" + ("、".join(offered_people) or "无")
            + "；地点=" + ("、".join(loc_names) or "无")
            + "；材料=" + ("、".join(mat_names) or "无")
            + "；开放秘境=" + ("、".join(dg_names) or "无"),
            "【只许续写这些未开始阶段】\n" + "\n".join(future_lines),
            "输出一个 JSON 对象：mode=story_arc_replan、arc_id 与 revision 原样照抄，"
            "stages 数组逐一照抄上述 id，只改 name/desc，可选 hook/condition。"
            "举例：若上一段已确认玩家交付药材，下一段可以写药铺恢复供应后的交涉；"
            "不能把交付写成未发生，也不能让已故者来赴约。",
        ])

    def _tick_story_arc_replan(self, world: World, preset: WorldSimPreset, tick: int,
                               cancel_check=None) -> bool:
        """每线每日最多一次续写尝试；失败保留原阶段，绝不覆盖已发生事实。"""
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        pending = sae.replan_due_arcs(world, day)
        if not pending or (cancel_check and cancel_check()):
            return False
        arc = pending[0]
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if api is None:
            arc.last_replan_day = day
            return False
        from src.models.world_sim_preset import DEFAULT_STORY_ARC_REPLAN_PROMPT
        tmp_preset = Preset(
            name="world_sim_story_arc_replan",
            system_prompt=DEFAULT_STORY_ARC_REPLAN_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(4000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        try:
            user_msg = self._build_arc_replan_user_message(world, arc)
            result = llm.chat_cancelable(
                [{"role": "system", "content": tmp_preset.system_prompt},
                 {"role": "user", "content": user_msg}],
                cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return False
            arc.last_replan_day = day
            if not (result.error or not (result.content or "").strip()):
                data = _extract_json(result.content)
                sae.apply_replan(world, arc, data, day)
        except Exception as e:  # noqa: BLE001 - LLM/解析失败只跳过续写，不打断世界推进
            arc.last_replan_day = day
            debug_log(lambda: f"[WorldSim] 剧情线续写失败（保留原阶段）: {e}")
        return True

    def _build_arc_gen_user_message(self, world: World,
                              assigned: Optional[list] = None,
                              interval_days: int = 5) -> str:
        """[剧情线 P46] 规划 user 消息。[!] JSON 字面量前缀用普通字符串拼接
        （f-string 内 {"mode":...} 会被当格式说明符——P34g 事故契约）。"""
        parts = ['{"mode":"story_arc"} 任务：为这个世界规划 1-2 条正在酝酿/推进的剧情线。']
        gt = self._genre_text(world)
        parts.append(f"{_world_anchor(world)}（题材 id：{self._genre_id(world)}，"
                     f"货币：{gt.currency}）")
        # [修 2026-10-02 真机 OOC·货币串味] 硬规则行：末日档剧情线 premise 两处写
        # 「瓶盖」（锚行软信号不够压住）——premise/阶段 desc 的钱款字眼一律用题材货币。
        _banned = "、".join(_FOREIGN_CURRENCY_WORDS)
        parts.append(f"[!] 货币铁律：premise 与阶段 desc 涉及钱款/物价/抽成一律只写"
                     f"「{gt.currency}」，禁止{_banned}等其他货币名。")
        # [S01/F02 2026-09-30] 规划读到真实时间：当前历法日期/相位/天气/回合 + 本批
        # 有效期（下次规划前世界将按此窗口推进）。此前 day 1 与 day 99 生成文本完全相同，
        # 剧情线接不住世界已发生的时间跨度；阶段 days 现在有时令锚点可贴合。
        # [!] interval 经参数传入（self.preset 恒 None，守 _build_* helper 契约）。
        _iv = max(1, min(30, int(interval_days or 5)))
        _next_day = max(1, int(getattr(world, "day_count", 1) or 1)) + _iv
        parts.append(
            f"【世界时间】{_fmt_time_weather(world)}\n"
            f"本批剧情自今日起约 {_iv} 个世界日内推进（下次整体规划约在第 "
            f"{_next_day} 天）；阶段内容须与当前时令/昼夜相位贴合"
            f"（春耕秋收、冬雪夏汛、夜行昼市等），不得出现与季节相悖的场景。")
        loc_by_id = {l.id: l for l in (getattr(world, "locations", None) or [])}
        fac_by_id = {f.id: f for f in (getattr(world, "factions", None) or [])}
        alive = [n for n in (getattr(world, "npcs", None) or []) if getattr(n, "alive", True)]
        # [导演偏置 2026-09-26 [M] 项] 社交圈成员（好友+交情>=50）在 cap 内优先入围并
        # 显式标注，配合导演偏好指令引导 LLM 让剧情线与玩家认识的人交织。
        circle = sae._social_circle_ids(world)
        # 候选名单预算固定 cap 12（要角优先 -> 社交圈次之；不随世界规模涨——注入预算固定原则）
        alive.sort(key=lambda n: (not getattr(n, "is_key_npc", False),
                                  0 if str(n.id) in circle else 1, str(n.id)))
        lines = []
        for n in alive[:12]:
            loc = loc_by_id.get(n.location_id)
            fac = fac_by_id.get(getattr(n, "faction_id", "") or "")
            amb = str((getattr(n, "ambition", None) or {}).get("desc", "") or "")[:40]
            cg = str(getattr(n, "current_goal", "") or getattr(n, "goal", "") or "")[:40]
            lines.append(f"- {n.name}（{n.role or '平民'}｜势力：{fac.name if fac else '无'}｜"
                         f"在：{loc.name if loc else '未知'}｜毕生所愿：{amb or '无'}｜"
                         f"当前目标：{cg or '无'}｜与玩家："
                         f"{'相识' if str(n.id) in circle else '陌生'}）")
        parts.append("【候选人物】（人名唯一权威来源，名单外一律被丢弃）\n" + "\n".join(lines))
        parts.append("【导演偏好】标注「相识」的人物与玩家有旧——每条线至少让一位相识者"
                     "担任策划者或参与者，让剧情线与玩家认识的人交织（其余仍按世界张力挑选）。")
        # [S04/R2 2026-09-30] 开放秘境清单：hook 引用秘境的唯一合法名单（编造秘境名
        # 会被清洗丢弃；封印中的不列——不可介入）。无秘境不注入该块。
        _dgs = [d for d in (getattr(world, "dungeons", None) or [])
                if getattr(d, "name", "") and str(getattr(d, "status", "")) == "open"
                and getattr(d, "discovered", True)]   # [D03] 未发现不入名单
        if _dgs:
            _loc_by_id = {l.id: l for l in (getattr(world, "locations", None) or [])}
            _dlines = []
            for _d in _dgs[:6]:
                _ent = _loc_by_id.get(str(getattr(_d, "location_id", "") or ""))
                _dlines.append(f"- {_d.name}（危险度{_d.danger}"
                               + (f"｜入口：{_ent.name}" if _ent is not None else "") + "）")
            parts.append("【可介入秘境】（hook 的 dungeon_room_resolved/dungeon_cleared "
                         "target 只能从这里照抄）\n" + "\n".join(_dlines))
        frels = []
        facs = list(getattr(world, "factions", None) or [])
        for i, fa in enumerate(facs):
            for fb in facs[i + 1:]:
                rel = (int(fa.relations.get(fb.id, 0) or 0)
                       + int(fb.relations.get(fa.id, 0) or 0)) / 2.0
                frels.append(f"- {fa.name} ↔ {fb.name}：{rel:+.0f}"
                             "（负=敌对；>=0 的双方不得互为敌手）")
        if frels:
            parts.append("【势力关系】\n" + "\n".join(frels[:15]))
        ev = _fmt_recent_events(world, 8)
        if ev:
            parts.append("【近期大事】（矛盾素材来源，premise 须引用其中真实张力）\n" + ev)
        act = sae.active_arcs(world)
        parts.append("【已有剧情线】（禁止重名/近义）\n"
                     + ("、".join(f"《{a.title}》" for a in act) if act else "无"))
        # [P46.1 v1.2 审核修复 GLM P1-1 / qwen P1①] 本批原型指派：引擎选好 2 个冷却外
        # 可发性原型写进 prompt（附 kind 序列骨架），LLM 只做世界化填充并回带 archetype
        # 字段——防同质化从 prompt 到 sanitize 全链贯通（LLM 不再输出 kind）。
        if assigned:
            parts.append("【本批原型指派】（每条线必须回带对应的 archetype 字段；"
                         "阶段类型由引擎按原型自动生成，你无需输出 kind）\n"
                         + "\n".join(
                             f"- 【{sae._ARC_ARCHETYPES[k]['name']}】archetype={k}："
                             f"阶段类型 {'->'.join(sae._ARC_ARCHETYPES[k]['seq'])}"
                             for k in assigned))
        parts.append(
            "输出 schema 字面示例（照抄结构，替换内容；每段 days 必填 1-5 整数）：\n"
            + '{"mode":"story_arc","arcs":[{"title":"盐路之争","premise":"两帮为盐路抽税'
            + '摩擦已摆上台面。","mastermind":"张三","participants":["李四","王五"],'
            + '"faction":"黑风寨","archetype":"faction_rivalry",'
            + '"stages":[{"name":"扣货","desc":"商队在界碑外被截","days":2,'
            + '"hook":{"type":"visit","target":"名单内真实地点名","count":1,'
            + '"stance":"support"}},'
            + '{"name":"摊牌","desc":"两拨人货栈前对峙","days":3}]}]}')
        parts.append(
            "硬规则回顾：人名/地名/物品名只用给定名单；策划者与至少 1 名参与者同地点；"
            "标题禁含「陨落」「据点失守」；[!] 不要输出 kind 字段（阶段类型由引擎按"
            "本批原型自动生成，输出 kind 会被忽略）；每线 2-5 段、每段 days 必填 1-5、"
            "总天数 5-15；conflict 只给有真实敌意的双方；阶段内容贴合【世界时间】的"
            "时令与昼夜。hook.stance=oppose 仅限可站边原型；condition 若写只能是"
            "npc_dead + 在世参与者名字，绝不要求玩家杀人。只输出纯 JSON。")
        return "\n\n".join(parts)

    def _llm_personality_drift(self, world: World, preset: WorldSimPreset,
                               candidates: list, tick: int,
                               cancel_check=None) -> "Optional[bool]":
        """[⑥] 批量给候选 NPC 写「性格偏移」（一次调用，仿 _generate_backfill_profiles）。

        返回 None=取消/无 API（不计数）；True=已调用（成功写偏移或失败回退不改）。
        """
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api or (cancel_check and cancel_check()):
            return None
        lines = [f"- {n.name}（当前性格：{n.personality or '未定'}；经历：{trig}）"
                 for n, trig in candidates]
        tmp = Preset(name="world_sim_personality_drift",
                     system_prompt=self._PERSONALITY_DRIFT_SYS_PROMPT,
                     temperature=preset.calculator_temperature,
                     max_tokens=max(10000, preset.calculator_max_tokens),
                     top_p=preset.calculator_top_p)
        llm = LlmClient(api, tmp, timeout=600.0, jailbreak_prefix=self._jb_prefix())
        user = (_world_anchor(world) + "\n"
                "需写性格偏移的 NPC：\n" + "\n".join(lines)
                + "\n请输出 drifts JSON。")
        messages = [{"role": "system", "content": tmp.system_prompt},
                    {"role": "user", "content": user}]
        result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                     block_labels=["系统", "用户"])
        if result.cancelled:
            return None
        if (result.error or "") or not (result.content or "").strip():
            return True  # 失败也计一次预算（口径同 backfill：防下 tick 重复撞）
        data = _extract_json(result.content)
        drift_list = data.get("drifts") if isinstance(data, dict) else None
        if not isinstance(drift_list, list):
            return True
        by_name = {n.name: n for n, _ in candidates}
        for p in drift_list:
            if not isinstance(p, dict):
                continue
            ref = str(p.get("ref") or "").strip()
            npc = by_name.get(ref)
            if npc is None:
                hit = nrs.resolve_name(ref, list(by_name.keys()))
                npc = by_name.get(hit) if hit else None
            if npc is None:
                continue
            drift = str(p.get("drift") or "").strip()
            if drift:
                npc.personality_drift = drift[:40]
                npc.last_drift_tick = tick
        return True

    def _tick_personality_drift(self, world: World, preset: WorldSimPreset, tick: int,
                                cancel_check=None) -> bool:
        """[⑥] 人格演化子阶段：转折点检测 + 批量 LLM 写偏移。返回是否消耗 1 预算。"""
        interval = max(1, int(self._per_world(world, "personality_drift_interval", 12, preset)))
        last_batch = int(getattr(world, "last_drift_batch_tick", 0) or 0)
        if last_batch > 0 and (tick - last_batch) < interval:
            return False  # 批量间隔未到，不扫不写
        cands = self._collect_drift_candidates(world, tick, preset)
        if not cands:
            world.drift_watermark_tick = tick
            return False
        res = self._llm_personality_drift(world, preset, cands, tick, cancel_check)
        if res is None:
            return False  # 取消/无 API：不推进水位，下 tick 重试（候选不吞）
        # 成功或失败回退都推进水位：事件已被其它系统（编年史/传闻）消费，防每 tick 重扫
        world.drift_watermark_tick = tick
        world.last_drift_batch_tick = tick
        return True

    def tick_world(
            self, world: World, scene: Optional[SceneLog], preset: WorldSimPreset,
            cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """P4 世界滴答集中入口：玩家每回合行动后调用，推进离屏世界演化。

        流程：
          1. sim_enabled 关 -> 直接返回空 report。
          2. 确定性引擎（全 SeededRng，salt 区分子阶段）跑经济/势力战/离屏NPC/重生/规则防漂移。
          3. 要角 LLM 决策（若启用 + 预算剩余）：批量一次出 JSON，引擎应用。
          4. LLM 世界校准（若到 interval + 预算剩余）：一次出 JSON patch，引擎应用。
          5. 事件截 max_events_per_tick，append world.event_log（超 event_log_max 截尾）。
        返回 tick_report = {"events":[...to_dict], "faction_changes":[...], "npc_changes":[...], "tick":N}。

        [!] 规则部分即时不可取消；LLM 部分走 cancel_check 可取消，取消跳过该 LLM 步骤（规则结果已保留）。
        [!] LLM 调用计数受 sim_budget_per_tick 约束（计数器）。
        """
        report = {"events": [], "faction_changes": [], "npc_changes": [], "tick": world.tick_count}
        if not self._per_world(world, "sim_enabled", preset.sim_enabled, preset):
            return report
        tick = world.tick_count
        budget = max(0, int(self._per_world(world, "sim_budget_per_tick", preset.sim_budget_per_tick, preset)))
        # [!] budget 计的是「逻辑决策数」而非原始 HTTP 次数：一次要角决策（含其内部重试）
        # 算 1 个预算单位，一次世界校准（含重试）算 1 个。默认 8 远超每 tick 最多 2 个决策，
        # 主要作防失控护栏，而非精确成本计量。
        llm_calls = 0  # 本 tick 已用 LLM 决策数

        # ---- 1. 确定性引擎（纯 Python，即时不可取消）----
        # [!] 每个子阶段独立 try/except：单个引擎异常只跳过该阶段（debug_log 可查），
        # 不中断整 tick / 吞掉本回合旁白（scene_worker 总 try 会把整回合标失败）。
        events: list[WorldEvent] = []

        # [审核修复 2026-09-13] 阶段异常可见化：原实现只写 debug 日志，玩家侧与日志栏
        # 全无读数——某个子系统「静默死掉」无人知晓（本次 P0 剧情线停产出即由此潜伏数日）。
        # 现把 (阶段名, 异常摘要) 一并记进 report，供场景页右侧动态日志栏显示。
        skipped: list[dict] = []

        def _phase(name: str, fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - 阶段隔离兜底
                debug_log(lambda: f"[WorldSim] tick 阶段 {name} 异常（跳过）: {e}")
                skipped.append({"phase": name, "error": f"{type(e).__name__}: {e}"[:160]})
                return []

        econ_level = self._per_world(world, "economy_sim", preset.economy_sim, preset)
        events += _phase("economy", wte.tick_economy,
                         world, SeededRng.seed_from(world.id, tick, "economy"), econ_level, tick)

        fw_level = self._per_world(world, "faction_war", preset.faction_war, preset)
        fw_lethal = self._per_world(world, "faction_war_lethality", preset.faction_war_lethality, preset)
        events += _phase("faction_war", wte.tick_faction_war,
                         world, SeededRng.seed_from(world.id, tick, "faction_war"), fw_level, fw_lethal, tick,
                         world.player.location_id)

        offscreen_on = self._per_world(world, "offscreen_npc_tick", preset.offscreen_npc_tick, preset)
        events += _phase("offscreen", wte.tick_offscreen_npcs,
                         world, SeededRng.seed_from(world.id, tick, "offscreen"),
                         offscreen_on, world.player.location_id, tick)

        events += _phase("cleanup", wte.cleanup_and_respawn,
                         world, SeededRng.seed_from(world.id, tick, "cleanup"), tick,
                         permadeath=bool(self._per_world(world, "npc_permadeath",
                                                          preset.npc_permadeath, preset)))

        # [P46 用户指示 2026-08-23] NPC 死亡补员（permadeath 开启时死者到岗期由
        # 接班人/新人顶替；敌对/要角不补）——置于 cleanup 后（先确定谁真死透了）；
        # 引擎只出功能骨架，语义档案在下方 LLM 段批量重生成（预算门控）
        backfill_spawned: list = []
        events += _phase("backfill", self._backfill_dead_npcs,
                         world, preset, tick, backfill_spawned)

        # [P6] 商店确定性补货（过期店货架向 max_stock 靠拢，纯 Python 无 LLM）
        events += _phase("shops", self.tick_shops, world, preset, tick)

        # [P35] 种植每日推进（浇水/雨天生长 + 枯萎 + 虫害/灵雨事件，纯引擎无 LLM）
        if self._per_world(world, "farm_enabled", preset.farm_enabled, preset):
            events += _phase("farm", fe.advance_farms, world,
                             SeededRng.seed_from(world.id, tick, "farm"),
                             max(0, int(self._per_world(world, "farm_wither_days",
                                                         preset.farm_wither_days, preset) or 0)))

        # [D2 2026-08-29] 据点日结算（工资/欠薪/离职；per-day 水位在函数内，纯 Python）。
        # 放 npc_life 之前：当日工资先进 NPC.wallet，生活模拟同日可花（系统互指）。
        events += _phase("domain", de.daily_settle, world, preset)

        # [饱食度 2026-09-06 用户指示] 每日衰减 + NPC 自动用餐（hunger_enabled 才跑；
        # 放 domain 后（工资先进钱包当日可花饭钱）+ npc_life 前）。
        if self._per_world(world, "hunger_enabled", preset.hunger_enabled, preset):
            events += _phase("hunger", self._tick_hunger, world, tick)
        # [自然恢复 2026-09-06 用户指示] 每日跨日：玩家+存活 NPC 回 hp_max 的
        # daily_hp_regen_pct%（默认 30，0=关）；静默日结（HP 条 UI 可见）
        events += _phase("regen", self._tick_daily_regen, world, tick, preset)
        # [野心 2026-09-06 用户指示] 一生追求日结算（纯引擎零 LLM）
        events += _phase("ambitions", self._tick_ambitions, world, tick)
        # [玩家印象 2026-09-06] 传闻浅印象日批（纯引擎无 LLM）
        events += _phase("impressions", self._tick_impressions, world, tick)
        # [事件任务 2026-09-06 用户指示] major 事件派生后续任务（纯引擎零 LLM）
        events += _phase("event_quests", qe.maybe_generate_event_quests, world, tick)
        # [P57 NPC 上门 2026-09-26] 信使系统（纯引擎零 LLM）：每日 roll + 旱涝保底生成 +
        # 过期不候结算。过期扫描每 tick 幂等（state 置位防重），生成走每日水位。
        events += _phase("outreach", ore.maybe_generate, world,
                         SeededRng.seed_from(world.id, tick, "outreach"), tick)
        events += _phase("outreach_expire", ore.expire_pending, world, tick)
        # [剧情线 P46 2026-09-12] 前瞻导演层每日推进（零 LLM；阶段到期产事件/hook 派生/
        # 死亡分支/软结算/终态滚动全在引擎内。arc 每 tick 公开事件 <=2；超额公告
        # 随结算持久排队、下 tick 优先补发，队列满才顺延新结算）。
        _arc_events: list = []
        if self._per_world(world, "story_arcs_enabled", preset.story_arcs_enabled, preset):
            # [P46 v1.1 审核修复 qwen] arc 事件不走全局 events 池——max_events_per_tick=3
            # 的 severity 截断会在 done 已置后把阶段/结局事件静默裁掉（热议/传闻/编年史
            # 全漏）。改为入库段之后直入 event_log（仿 rumors 先例），自限每 tick 2 条。
            # [!] 副作用即设计意图：arc 事件不触发 crisis 任务与世界 Boss 派生（不借危机
            # 通道扩权），仍进热议/传闻/编年史/旁白块（后者消费 event_log）。
            _arc_events = _phase(
                "arc_advance", sae.advance, world,
                SeededRng.seed_from(world.id, tick, "story_arc"), tick,
                sae.ARC_EVENT_CAP_PER_TICK)
            # S03：阶段刚结算的同 tick 优先续写下段；下一 tick 开始后便不再改它。
            # 每线每日最多一次，预算不足则原计划照常推进，不阻塞世界时间。
            if budget > llm_calls and sae.replan_due_arcs(world, world.day_count):
                if _phase("arc_replan", self._tick_story_arc_replan,
                          world, preset, tick, cancel_check):
                    llm_calls += 1

        # [P36a] NPC 生活每日推进（per-day 水位 + roster 轮换 + LLM 批量日计划/规则回退）
        if self._per_world(world, "npc_autonomy_enabled", preset.npc_autonomy_enabled, preset):
            events += _phase("npc_life", self._tick_npc_life, world, preset, tick, cancel_check)
            # [G02/R3 2026-09-30] 探伤赠药：玩家带伤时相熟 NPC 携真药登门（守恒转移，
            # 每 NPC 7 天冷却 + 全图每日 1 次；零 LLM 零 rng）
            events += _phase("care_visits", nre.tick_care_visits, world)
            # [G02 续/R3] 结义之请：共同战斗 >=3 且交情达门槛 -> 信使提议信（零 LLM）
            events += _phase("sworn_proposals", nre.tick_sworn_proposals, world,
                             preset)
            # [G03/R3] 地区修正过期清理 + 行情回落事件
            from src.services.region_pressure import tick_expire as _rp_tick_expire
            events += _phase("region_modifiers", _rp_tick_expire, world)

        # [P7g] 资源点冷却再生（确定性，纯 Python 无 LLM）：冷却到期的资源点恢复丰度
        if getattr(preset, "gathering_enabled", True):
            regen = 0

            def _regen_nodes(loc):
                n = 0
                try:
                    n += ge.tick_resource_nodes(loc.resource_nodes, tick,
                                                regen_mult=_season_mult(world, "gather_regen"))
                except Exception as e:  # noqa: BLE001
                    debug_log(lambda: f"[WorldSim] tick 资源再生异常（跳过地点）: {e}")
                return n

            for loc in world.locations:
                regen += _regen_nodes(loc)
            # 资源再生不产出 WorldEvent（太琐碎），只在 tick_report 计数
            if regen > 0:
                report["resources_regen"] = regen

        # 规则防漂移（兜底，不产出 WorldEvent，只修正状态）
        _phase("reconcile", wte.reconcile_world,
               world, SeededRng.seed_from(world.id, tick, "reconcile"))

        # [P7k5] 日夜天气推进（确定性，由 tick 推导相位 + 周期性天气变化）
        _phase("time_phase", self._tick_time_phase, world, tick)

        # [P24c] 好友联动：每 3 tick 一次（带话/寄礼/交情衰减，确定性）
        if tick % 3 == 0:
            events += _phase("friends", wte.tick_friends, world,
                             SeededRng.seed_from(world.id, tick, "friends"), tick)
            # [A3 2026-08-28] 好友主动私聊（LLM 阶段：消费 friends 标记的 pending，预算闸；
            # 成功生成一条占 1 预算单位，失败/取消/无 pending 不计）
            if self._per_world(world, "friend_chat_proactive_enabled",
                               getattr(preset, "friend_chat_proactive_enabled", True), preset):
                _fc_before = len(events)
                events += _phase("friend_chat", self._tick_friend_chats, world, preset, tick,
                                 cancel_check, budget > llm_calls)
                if any(e.title.endswith("发来私聊") for e in events[_fc_before:]):
                    llm_calls += 1
            # [P25a] 秘境重生：封印到期同 seed 重铺（clears 提难度）
            events += _phase("dungeons", wte.tick_dungeons, world)
            # [P25d] 世界 Boss 窗口清理：到期未击败移出 + 传闻事件（不强行终结）
            events += _phase("world_boss", wbe.tick_world_boss, world)

        # [P38a] NPC 社交演化：同地 NPC 结交/深交/结仇/和解（纯引擎，阶段跨越才产事件）
        # [P39a] 每日社会交互：生活开销 + 互市 + 仇敌冲突 + 挚友互赠（day 水位在函数内）
        # [P42a] 「闲谈：」事件分流进双方 NPC 记忆（玩家不可知，不进 event_log）
        if self._per_world(world, "social_enabled", preset.social_enabled, preset):
            events += _phase("social", soc.tick_social, world,
                             SeededRng.seed_from(world.id, tick, "social"), tick)

            def _social_daily_phase():
                evs = soc.daily_interactions(
                    world, SeededRng.seed_from(world.id, tick, "social_daily"), tick)
                chats = [e for e in evs if str(e.title).startswith("闲谈：")]
                for c in chats:
                    try:
                        for nid in (c.npcs or []):
                            self.npc_memory().record_chat_note(world, nid, c.desc)
                    except Exception:
                        pass  # best-effort：记忆写入失败不阻断社交阶段
                return [e for e in evs if not str(e.title).startswith("闲谈：")]
            events += _phase("social_daily", _social_daily_phase)

        # [P39b] 委托订单板：每日生成/过期清理（静默不产事件，订单板 UI 自身可见）
        if self._per_world(world, "commissions_enabled",
                           preset.commissions_enabled, preset):
            _phase("commissions", come.maybe_spawn_commissions, world,
                   SeededRng.seed_from(world.id, tick, "commissions"), tick)

        # [P42c] 日常跑腿任务：每聚落每日传话/捎物（静默；任务日志可见，talk 钩子自动推进）
        if self._per_world(world, "errands_enabled", preset.errands_enabled, preset):
            _phase("errands", qe.maybe_spawn_errands, world,
                   SeededRng.seed_from(world.id, tick, "errands"), tick)

        # [P39d+e] 剧情导演层 + 世界编年史：收编 major/crisis 入 pending，累积触发 LLM 沉淀
        #（[定稿] 40 压 30 / 回退拼接同推进水位 / 滚 8 条；静默不产事件——注入场景上下文可见）
        if self._per_world(world, "chronicle_enabled", preset.chronicle_enabled, preset):
            _phase("chronicle", self._tick_chronicle, world, preset, tick, cancel_check)

        # [④ 2026-08-30] 共享传闻收编（纯引擎水位增量；口述化在下方 LLM 预算段）
        if self._per_world(world, "rumor_enabled", preset.rumor_enabled, preset):
            _phase("rumor_collect", rme.collect, world)

        # ---- 2. 要角 LLM 决策（若启用 + 预算剩余）----
        key_enabled = self._per_world(world, "key_npc_decision_enabled",
                                      preset.key_npc_decision_enabled, preset)
        if key_enabled and budget > llm_calls:
            key_events = self._llm_key_npc_decisions(world, preset, tick, cancel_check)
            if key_events is not None:  # None = 取消或无可用 API，跳过不计数
                llm_calls += 1
                events += key_events

        # [P46] 接班人/补位新人档案 LLM 重生成（本 tick 有补员才调；一次批量；
        # None=取消/无 API 不计数，True=已调用占 1 预算——口径同要角决策）
        if backfill_spawned and budget > llm_calls:
            if self._generate_backfill_profiles(world, preset, backfill_spawned,
                                                cancel_check) is not None:
                llm_calls += 1

        # [④ 2026-08-30] 共享传闻口述化（pending 达阈值 + 预算闸；一次批量，占 1 预算）
        if (self._per_world(world, "rumor_enabled", preset.rumor_enabled, preset)
                and len(getattr(world, "rumor_pending", None) or []) >= rme.RUMOR_TRIGGER
                and budget > llm_calls):
            if self._tick_rumor_voice(world, preset, tick, cancel_check):
                llm_calls += 1

        # [⑥ 2026-08-30] NPC 人格演化（转折点检测 + 批量 LLM 写性格偏移，占 1 预算）
        if (self._per_world(world, "personality_evolution_enabled",
                            preset.personality_evolution_enabled, preset)
                and budget > llm_calls):
            if self._tick_personality_drift(world, preset, tick, cancel_check):
                llm_calls += 1

        # [剧情线 P46] 剧情线规划（每 arc_interval_days 一次，批量 1 次 LLM 占 1 预算）。
        # 三态：取消 -> 不推水位不计数；无 API -> 模板线+推水位不计数；
        # 解析失败 -> 模板线+推水位计 1；成功 -> LLM 线+推水位计 1。走 _phase 隔离
        # （异常按未计预算处理，水位纪律在方法内部保证：任何非取消出口都推水位）。
        if (self._per_world(world, "story_arcs_enabled", preset.story_arcs_enabled, preset)
                and budget > llm_calls
                and sae.gen_due(world, world.day_count, self._arc_knobs(world, preset)[1])
                and len(sae.active_arcs(world)) < self._arc_knobs(world, preset)[2]):
            if _phase("arc_gen", self._tick_story_arcs_gen, world, preset, tick,
                      cancel_check):
                llm_calls += 1

        # ---- 3. LLM 世界校准（若到 interval + 预算剩余）----
        # note 三态：None=取消/无API（不计数不推进，下个 interval 再试）；
        #           ""=解析失败回退（计数 + 推进，避免每 tick 撞同一失败）；
        #           非空串=成功（计数 + 推进 + 入事件）。
        reconcile_interval = int(self._per_world(world, "reconcile_interval",
                                                 preset.reconcile_interval, preset))
        if (reconcile_interval > 0
                and tick - world.last_reconcile_tick >= reconcile_interval
                and budget > llm_calls):
            note = self._llm_reconcile(world, preset, tick, cancel_check)
            if note is None:
                pass  # 取消/无 API：不计数、不推进，下个 interval 重试
            else:
                # ""（失败回退）与非空串（成功）都计数 + 推进 last_reconcile_tick
                llm_calls += 1
                world.last_reconcile_tick = tick
                if note:
                    events.append(WorldEvent(
                        tick=tick, category="event", severity="trivial",
                        title="世界校准", desc=note))

        # ---- [P34f] 3b. 交易所股市每日 LLM 定价（day_count 变化时触发，独立 _phase）----
        # 三态同 reconcile：None=取消/无API（不推进 last_stock_priced_day，不计预算，下日重试）；
        # ""=失败兜底（推进 + 计预算）；非空=成功（推进 + 计预算 + 入事件）。
        if (bool(self._per_world(world, "stock_market_enabled",
                                 preset.stock_market_enabled, preset))
                and world.day_count != world.last_stock_priced_day
                and budget > llm_calls):
            stock_note = self._phase_or_call_stock(world, preset, tick, cancel_check)
            if stock_note is not None:
                llm_calls += 1
                world.last_stock_priced_day = world.day_count
                if stock_note:
                    events.append(WorldEvent(
                        tick=tick, category="economy", severity="trivial",
                        title="交易所行情", desc=stock_note[:200]))

        # ---- [P34g] 3c. 拍卖会事件（每日 1 次：end 到期 / start 到期 LLM 出拍品 / 周期生成）----
        # 纯引擎部分（end/spawn）不耗 LLM 预算；start_auction 的 LLM 骨架注入耗预算。
        # _tick_auctions 自管 try/except（含 LLM 调用），返回 (events, llm_calls_used)。
        if bool(self._per_world(world, "auction_enabled",
                                preset.auction_enabled, preset)):
            auc_events, auc_llm = self._tick_auctions(
                world, preset, tick, cancel_check, budget > llm_calls)
            events += auc_events
            llm_calls += auc_llm

        # ---- 4. 事件入库 ----
        max_per_tick = int(self._per_world(world, "max_events_per_tick", preset.max_events_per_tick, preset))
        if max_per_tick > 0 and len(events) > max_per_tick:
            # 保留严重度高的（crisis>major>minor>trivial），同严重度取靠前
            sev_order = {"crisis": 0, "major": 1, "minor": 2, "trivial": 3}
            events = sorted(events, key=lambda e: (sev_order.get(e.severity, 9),))[:max_per_tick]
        for e in events:
            world.event_log.append(e)
        log_max = int(self._per_world(world, "event_log_max", preset.event_log_max, preset))
        if log_max > 0 and len(world.event_log) > log_max:
            del world.event_log[:len(world.event_log) - log_max]
        # [P20] 市井传闻（B3）：确定性背景小事件在关键事件入库后直入 event_log——
        # 不走 max_events_per_tick 截断（纯氛围不应被关键事件限额挤压），
        # 只受 event_log_max 尾裁剪；阶段隔离 try/except 同其它阶段。
        rumor_evts = _phase("rumors", wte.tick_rumors, world,
                            SeededRng.seed_from(world.id, tick, "rumor"), tick)
        for e in rumor_evts:
            world.event_log.append(e)
        if log_max > 0 and len(world.event_log) > log_max:
            del world.event_log[:len(world.event_log) - log_max]
        # [剧情线 P46 v1.1 审核修复 qwen] arc 事件在全局截断之后直入 event_log（仿 rumors
        # 先例）——阶段/结局叙事不与其它系统挤 max_events_per_tick=3 的池子（否则 done
        # 已置后事件被静默裁掉，热议/传闻/编年史全漏）。[!] 副作用即设计意图：arc 事件
        # 不触发 crisis 任务与世界 Boss 派生（不借危机通道扩权），仍进热议/传闻/编年史
        # /旁白块（后者直接消费 event_log）。受 event_log_max 尾裁剪约束。
        for e in _arc_events:
            world.event_log.append(e)
        if log_max > 0 and len(world.event_log) > log_max:
            del world.event_log[:len(world.event_log) - log_max]
        # [P15b2] 势力剧情线：本 tick 有 crisis 级事件入库时生成 1 条「世界危机任务」
        # （objectives 指向事件相关 NPC/地点，奖励高档；同一时间只挂一条危机任务防刷屏）。
        # 纯代码派生（事件结构 -> 任务定义），关联经 WorldEvent.factions/locations/npcs + 目标名。
        try:
            crisis_q = self._maybe_crisis_quest(world, events, tick)
            if crisis_q is not None:
                report["new_quests"] = [crisis_q.to_dict()]
        except Exception as e_:  # noqa: BLE001 - 阶段隔离兜底
            debug_log(lambda: f"[WorldSim] crisis 任务生成异常（跳过）: {e_}")
        # [P25d] 世界 Boss：本 tick 有 major/crisis 事件关联高危险地点(danger>=阈值)时生成
        # 1 个世界 Boss（挂该地点，窗口 N 天，多形态递进讨伐；同时只一未击败在窗 Boss 防刷屏）。
        # 纯代码派生（镜像危机任务门控）；生成 major 事件入 event_log（旁白/世界动态感知）。
        try:
            self._maybe_spawn_world_boss(world, events, preset)
        except Exception as e_:  # noqa: BLE001 - 阶段隔离兜底
            debug_log(lambda: f"[WorldSim] 世界 Boss 生成异常（跳过）: {e_}")
        # [P11d] 关键事件插图：本 tick 最严重的新事件配一张图（severity>=major，限 1 张/回合）。
        # SceneWorker 后台线程内执行不卡 UI；best-effort（异常/取消不吞整回合，守 tick 分相隔离）。
        try:
            self.ensure_event_image(world, events, preset, cancel_check)
        except Exception as e_:  # noqa: BLE001
            debug_log(lambda: f"[WorldSim] 事件插图异常（跳过）: {e_}")

        # ---- 5. 汇总 report ----
        report["events"] = [e.to_dict() for e in events]
        report["faction_changes"] = [
            {"id": f.id, "name": f.name, "power": f.power, "wealth": f.wealth}
            for f in world.factions
        ]
        report["npc_changes"] = [
            {"id": n.id, "name": n.name, "alive": n.alive, "location_id": n.location_id}
            for n in world.npcs if not n.alive or n.current_action
        ]
        # [审核修复 2026-09-13] 跳过的阶段（异常可见化；空列表不写，保持旧 report 形状）
        if skipped:
            report["skipped_phases"] = list(skipped)
        return report

    def _maybe_crisis_quest(self, world: World, events: list, tick: int) -> Optional[Quest]:
        """[P15b2] crisis 事件 -> 世界危机任务（纯代码，无 LLM）。

        目标：优先 kill 事件相关存活 NPC，否则 visit 事件相关地点；两者皆无则不生成。
        奖励高档：gold 200 / xp 150 + 一件目录物品（紫以下，高档权重——见
        qe.pick_reward_item；奖励文案同步带物品名）。
        防刷屏：已有 available/active 的危机任务（标题前缀「世界危机：」）时不再生成。
        """
        if not qe._quests_on(world):
            return None
        ev = next((e for e in events if getattr(e, "severity", "") == "crisis"), None)
        if ev is None:
            return None
        if any(str(q.title).startswith("世界危机：") and q.status in ("available", "active")
               for q in (world.quests or [])):
            return None
        title = f"世界危机：{ev.title}"
        if any(q.title == title for q in (world.quests or [])):
            return None
        objectives = []
        npc_by_id = {n.id: n for n in world.npcs}
        if ev.npcs:
            target_name = next((npc_by_id[i].name for i in ev.npcs
                                if i in npc_by_id and npc_by_id[i].alive), "")
            if target_name:
                objectives = [{"type": "kill", "target": target_name, "count": 1, "current": 0,
                               "desc": f"平息事态：击败与事件相关的{target_name}"}]
        if not objectives and ev.locations:
            loc_by_id = {l.id: l for l in world.locations}
            target_loc = next((loc_by_id[i] for i in ev.locations if i in loc_by_id), None)
            if target_loc is not None:
                objectives = [{"type": "visit", "target": target_loc.name, "count": 1, "current": 0,
                               "desc": f"赶赴{target_loc.name}处理此次危机"}]
        if not objectives:
            return None
        # [任务奖励物品 2026-09-13] 原实现取「目录里第一件 epic」——每次危机都是同一件，
        # 且 legendary 兜底越过了紫上限；改走共享挑选器（高档权重，仍封 epic）。
        reward_item = qe.pick_reward_item(
            world, SeededRng.seed_from(str(getattr(world, "id", "") or ""),
                                       int(tick), "crisis_item"),
            tier="high", exclude_ids=qe.player_owned_ids(world))
        q = Quest(
            title=title,
            objective=(ev.desc or ev.title or "处理一场世界级危机"),
            giver_npc_id="",
            reward_text=("世界危机悬赏（高档）"
                         + (f" + {reward_item.name}" if reward_item is not None else "")),
            status="available",
            objectives=objectives,
            rewards={"items": [str(reward_item.id)] if reward_item is not None else [],
                     "gold": 200, "xp": 150},
        )
        world.quests.append(q)
        return q

    def _maybe_spawn_world_boss(self, world: World, events: list, preset: WorldSimPreset) -> None:
        """[P25d] major/crisis 事件关联高危险地点 -> 生成 1 个世界 Boss（纯代码，无 LLM）。

        门控（镜像危机任务口径）：world_boss_enabled 开关；已有未击败在窗 Boss 不再生成
        （同时只一未击败在窗 Boss）；扫 events 找 severity in (major, crisis) 且关联地点
        danger >= min_danger 的事件。命中 -> spawn + append world.world_bosses + 落 major
        事件（旁白/世界动态感知「世界级威胁降临」）。
        """
        if not self._per_world(world, "world_boss_enabled",
                               preset.world_boss_enabled, preset):
            return
        # 同时只一未击败在窗 Boss（防刷屏）
        day = int(getattr(world, "day_count", 1) or 1)
        if any(not wb.defeated and day < int(wb.window_until_day or 0)
               for wb in (getattr(world, "world_bosses", None) or [])
               if isinstance(wb, WorldBoss)):
            return
        min_danger = self._per_world(world, "world_boss_min_danger",
                                     preset.world_boss_min_danger, preset)
        window_days = self._per_world(world, "world_boss_window_days",
                                      preset.world_boss_window_days, preset)
        loc_by_id = {l.id: l for l in (getattr(world, "locations", None) or [])}
        chosen_loc = None
        chosen_ev = None
        for ev in events:
            sev = getattr(ev, "severity", "")
            if sev not in ("major", "crisis"):
                continue
            locs = getattr(ev, "locations", None) or []
            if not locs:
                continue
            loc = loc_by_id.get(locs[0])
            if loc is None:
                continue
            if int(getattr(loc, "danger", 1) or 1) >= int(min_danger):
                # 该地点无在窗 Boss（避免一地点叠多 Boss）
                if any(not wb.defeated and day < int(wb.window_until_day or 0)
                       and wb.location_id == loc.id
                       for wb in (getattr(world, "world_bosses", None) or [])
                       if isinstance(wb, WorldBoss)):
                    continue
                chosen_loc = loc
                chosen_ev = ev
                break
        if chosen_loc is None or chosen_ev is None:
            return
        boss = wbe.spawn_world_boss(world, chosen_loc, chosen_ev, window_days, min_danger)
        world.world_bosses.append(boss)
        world.event_log.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0),
            category="event", severity="major",
            title=f"世界级威胁降临：{boss.name}",
            desc=f"「{boss.name}」盘踞「{chosen_loc.name}」（{boss.desc}），威胁渐起。",
            locations=[boss.location_id],
            factions=[boss.faction_id] if boss.faction_id else [],
        ))

    def prepare_world_boss_combat(self, world: World, boss: WorldBoss,
                                  preset: Optional[WorldSimPreset] = None) -> Optional[dict]:
        """[P25d] 构建世界 Boss 当前形态战斗数据（UI 调 -> CombatDialog skip_judge）。

        返回 {target_npc, allies, minion_specs, note}，喂入 CombatDialog(skip_judge_with=...)。
        Boss 已击败或无形态返回 None。详见 world_boss_engine.prepare_combat。
        """
        if boss is None:
            return None
        return wbe.prepare_combat(world, boss, preset)

    # ---------- LLM 要角决策 ----------
    def _collect_key_npc_candidates(self, world: World, budget: int) -> list[NPC]:
        """收集本 tick 待决策的要角 NPC（非玩家当前地点、存活、限 budget 个）。"""
        player_loc = world.player.location_id
        cands = [n for n in world.npcs
                 if n.is_key_npc and n.alive and n.location_id != player_loc and n.location_id]
        return cands[:max(0, budget)]

    def _llm_key_npc_decisions(
            self, world: World, preset: WorldSimPreset, tick: int,
            cancel_check: Optional[Callable[[], bool]],
    ) -> Optional[list[WorldEvent]]:
        """调滴答 LLM 给要角 NPC 出批量决策 JSON，引擎应用。返回事件列表。

        返回 None 表示取消/无可用 API/无候选/解析失败（调用方据此不计数、不阻断）。
        失败重试一次（回灌错误），仍失败回退空（保持原状，不丢回合）。
        """
        key_budget = int(self._per_world(world, "key_npc_budget", preset.key_npc_budget, preset))
        cands = self._collect_key_npc_candidates(world, key_budget)
        if not cands:
            return None
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            return None
        tmp_preset = Preset(
            name="world_sim_tick_keynpc",
            system_prompt=preset.sim_system_prompt or DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT,
            temperature=preset.sim_temperature,
            max_tokens=preset.sim_max_tokens,
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        user_msg = self._build_key_npc_user_message(world, cands, tick)
        messages = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return None
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return None
            if result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return []
            data = _extract_json(result.content)
            if data and isinstance(data, dict) and data.get("mode") == "key_npc":
                decisions = data.get("decisions") or []
                if isinstance(decisions, list):
                    return self._resolve_llm_key_npc_decisions(world, decisions, tick, cands)
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 mode 字段，请严格按 schema 输出纯 JSON。"},
                ]
                continue
        return []

    def _build_key_npc_user_message(self, world: World, cands: list[NPC], tick: int) -> str:
        loc_by_id = {l.id: l for l in world.locations}
        fac_by_id = {f.id: f for f in world.factions}
        parts = ['{"mode": "key_npc"} 任务：为以下要角 NPC 决定本回合的离屏行动意图。']
        parts.append(f"当前世界回合：{tick}")
        parts.append(_world_anchor(world))
        # [P22] 世界书注入（与场景上下文同口径前 5 条）：要角决策锚定世界观细节，
        # 避免与世界观设定矛盾的离屏行动
        lore_line = _fmt_world_lore(world)
        if lore_line:
            parts.append(lore_line)
        # [P10c] 注入时间天气 + 近期事件（复用 _build_scene_context 格式，口径一致）。
        # 让要角决策有世界背景锚点：知道昼夜/天气氛围、知道近期势力大战等大事，
        # 避免决策「漂浮」（旧版要角与近期事件脱节，反复在无关地点横跳）。
        parts.append(_fmt_time_weather(world))
        ev_lines = _fmt_recent_events(world, 5)
        if ev_lines:
            parts.append("【近期世界动态】\n" + ev_lines)
        # [P10c] 玩家近况（让关系型决策有落点，如「潜在友李白」类关系；候选已排除玩家地点 NPC，
        # 引擎侧 move 强制相邻校验，注入玩家位置不诱导 NPC 挪到玩家处——守滴答铁律）。
        # [2026-08-23 用户卡绑定] 带姓名：要角决策提及玩家时用真名不泛称。
        ploc = loc_by_id.get(world.player.location_id)
        active_titles = [q.title for q in (world.quests or []) if q.status == "active"][:5]
        _uid_line = self._fmt_bound_user(world)
        parts.append(f"【玩家近况】{(_uid_line + '；') if _uid_line else ''}"
                     f"所在地点：{ploc.name if ploc else '未知'}；等级：{world.player.level}；"
                     f"HP：{world.player.hp}/{world.player.hp_max}；"
                     f"进行中任务：{'、'.join(active_titles) if active_titles else '无'}")
        # [记忆驱动 2026-08-29] 注入各要角最近记忆（2 条 x 60 字，预算固定）——
        # 与 _fmt_npc_detail_line 的档案口径并列，决策有「经历过什么」的锚。
        try:
            _mem_svc = self.npc_memory()
            _mem_parts = []
            for n in cands:
                _m = _mem_svc.recent_lines(world, n, 2)
                if _m:
                    _mem_parts.append(f"{n.name}：{'；'.join(_m)}")
            if _mem_parts:
                parts.append("【近期记忆】（各要角亲历，决策应与之连贯）\n"
                             + "\n".join(_mem_parts))
        except Exception:
            pass
        lines = []
        for n in cands:
            loc = loc_by_id.get(n.location_id)
            adj = "、".join(loc_by_id[c].name for c in (loc.connections if loc else []) if c in loc_by_id)
            fac = fac_by_id.get(n.faction_id)
            # [P10c] NPC 行补关系/小传/上一回合行动/交情（复用 _build_scene_context:2004-2027 格式），
            # 让 interact/scheme 决策有目标（关系）、连续性（上一回合行动）、态度（交情）锚点。
            seg = [f"- {n.name}（身份:{n.role or '未定'}；性格:{n.personality or '未定'}；"
                   f"目标:{n.goal or '未定'}；"
                   f"当前目标:{getattr(n, 'current_goal', '') or n.goal or '未定'}；"
                   f"势力:{fac.name if fac else '无'}"
                   + (f"（{fac.ideology}）" if fac and fac.ideology else "")
                   + f"；所在地点:{loc.name if loc else '未知'}；相邻地点:{adj or '无'}"]
            # [⑥ 2026-08-30] 性格偏移注入（要角决策看到「他因此变成了什么样」）
            _drift = getattr(n, "personality_drift", "")
            if _drift:
                seg.append(f"；近变:{_drift}")
            # [P27] 注入场所上下文：当前场所 + 场所列表（[2026-08-28] 地点内自由到达，不再注相邻）
            if loc is not None and getattr(loc, "places", None):
                cur_pl = None
                npid = (getattr(n, "place_id", "") or "").strip()
                if npid:
                    cur_pl = next((p for p in loc.places if getattr(p, "id", "") == npid), None)
                if cur_pl is None and getattr(loc, "default_place_id", ""):
                    cur_pl = next((p for p in loc.places
                                   if getattr(p, "id", "") == loc.default_place_id), None)
                if cur_pl is not None:
                    place_names = "、".join(getattr(p, "name", "") for p in loc.places if getattr(p, "name", ""))
                    seg.append(f"；当前场所:{cur_pl.name}；场所列表:{place_names or '无'}（地点内可自由前往）")
            rels = getattr(n, "relationships", None) or []
            if isinstance(rels, list) and rels:
                rel_parts = []
                for r in rels:
                    if isinstance(r, dict):
                        tn = str(r.get("target_name", "") or "").strip()
                        rl = str(r.get("relation", "") or "").strip()
                        if tn and rl:
                            rel_parts.append(f"{tn}({rl})")
                if rel_parts:
                    seg.append("；关系：" + "、".join(rel_parts[:3]))
            notes = getattr(n, "notes", "") or ""
            if notes:
                seg.append(f"；小传：{notes[:60]}")
            ca = (getattr(n, "current_action", "") or "").strip()
            if ca:
                seg.append(f"；上一回合：{ca}")
            # [剧情线 P46] 要角决策朝自己卷入的线发力（注入而非写 current_goal——
            # 那字段被要角决策系统自己每 tick 覆写）
            _arc_ln = sae.arc_line_for_npc(world, n.id)
            if _arc_ln:
                seg.append(f"；{_arc_ln}")
            aff = int(getattr(n, "affinity", 0) or 0)
            seg.append(f"；与玩家交情：{nre.affinity_level(aff)}({aff})")
            seg.append("）")
            lines.append("".join(seg))
        parts.append("【要角名单】\n" + "\n".join(lines))
        parts.append("请为每个要角输出 decisions（action 限 move|stay|interact|scheme，"
                     "move 的 target_location 必须是其相邻地点之一（跨地点移动）；"
                     "若 NPC 所在地点有场所列表，target_location 也可填「当前地点名/场所名」或纯场所名"
                     "（地点内场所移动，同地点任意场所均可）；interact 的 target_npc 须是其关系对象之一）。"
                     "只输出纯 JSON。")
        return "\n\n".join(parts)

    def _is_hostile_territory(self, world: World, npc, loc) -> bool:
        """[P] 判断 loc 是否对该 NPC 敌对（被其势力 relations<=-30 的势力控制）。

        战斗型 NPC（level>0 或 hostile）不受拦——他们本就会去敌对地盘打架；
        平民型 NPC（郎中/掌柜等）不主动走进敌对势力地盘，防被要角 LLM 决策挪进山贼老巢。
        """
        if loc is None:
            return False
        if int(getattr(npc, "level", 0) or 0) > 0 or getattr(npc, "hostile", False):
            return False
        owner = getattr(loc, "owner_faction_id", "") or getattr(loc, "faction_id", "")
        fid = getattr(npc, "faction_id", "") or ""
        if not owner or not fid or owner == fid:
            return False
        for f in world.factions:
            if f.id == owner:
                return int(f.relations.get(fid, 0) or 0) <= -30
        return False

    def _resolve_llm_key_npc_decisions(
            self, world: World, decisions: list, tick: int, cands: Optional[list[NPC]] = None,
    ) -> list[WorldEvent]:
        """把 LLM 要角决策 JSON 应用到 world（name->id 解析 + 连通性/alive 校验）。

        返回事件列表。非法决策静默跳过（不崩、不阻断）。
        [!] name 解析只认候选集（cands）：LLM 复述名单外 NPC（如同伴/玩家地点 NPC）
        时不得命中——否则可把玩家身边的 NPC（含同伴）离屏挪走，违反「世界滴答
        排除玩家地点 NPC」铁律。
        """
        events: list[WorldEvent] = []
        loc_by_id = {l.id: l for l in world.locations}
        if cands is None:
            cands = self._collect_key_npc_candidates(world, 10 ** 9)
        cand_by_name = {n.name: n for n in cands}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name", "") or "").strip()
            npc = cand_by_name.get(name)
            if npc is None:
                # [P44] 容错解析（锚定 cands 不变：名单外名字仍不命中，守下方铁律）
                k_hit = nrs.resolve_name(name, list(cand_by_name.keys()))
                npc = cand_by_name.get(k_hit) if k_hit else None
            if not npc or not npc.alive:
                continue
            action = str(d.get("action", "") or "").strip()
            cur = loc_by_id.get(npc.location_id)
            moved = False
            if action == "move" and cur:
                tgt_name = str(d.get("target_location", "") or "").strip()
                # [P27] 场所级移动：target_location 含 / 或匹配当前地点场所
                tgt_loc, tgt_place = self._resolve_dest_full(world, tgt_name, cur)
                if tgt_place is not None and tgt_loc is not None and tgt_loc.id == npc.location_id:
                    # [用户定稿 2026-08-28] 地点内场所自由到达（原连通校验移除，与玩家同口径）
                    self._move_to_place(world, npc, tgt_place, cur)
                    moved = True
                elif tgt_loc is not None and tgt_loc.id in cur.connections and tgt_loc.id != cur.id:
                    # 跨地点移动（连通性校验，与 apply_intent move 分支同口径）
                    if self._is_hostile_territory(world, npc, tgt_loc):
                        pass  # [P] 非战斗 NPC 不主动进敌对势力地盘（跳过移动，仍保留 narration_hint）
                    else:
                        _move_npc_safe(npc, npc.location_id, tgt_loc.id, loc_by_id,
                                       to_place_id=getattr(tgt_loc, "default_place_id", "") or "")
                        moved = True
            hint = str(d.get("narration_hint", "") or "").strip()
            if hint:
                npc.current_action = hint
            # [P] 短期目标每回合可更新；长期目标仅在 LLM 判定已达成时经 new_goal 替换（勿频繁换）。
            cg = str(d.get("current_goal", "") or "").strip()
            if cg:
                npc.current_goal = cg
            ng = str(d.get("new_goal", "") or "").strip()
            if ng:
                npc.goal = ng
            sev = "minor" if moved else "trivial"
            title = f"要角动态：{npc.name}"
            desc = hint or (f"{npc.name} 前往了「{loc_by_id[npc.location_id].name}」" if moved
                            else f"{npc.name} 在{loc_by_id[npc.location_id].name if npc.location_id in loc_by_id else '某处'}活动")
            events.append(WorldEvent(
                tick=tick, category="npc", severity=sev, title=title, desc=desc,
                npcs=[npc.id], locations=[npc.location_id]))
        return events

    # ---------- LLM 世界校准 ----------
    def _llm_reconcile(
            self, world: World, preset: WorldSimPreset, tick: int,
            cancel_check: Optional[Callable[[], bool]],
    ) -> Optional[str]:
        """调滴答 LLM 出世界校准 JSON patch，引擎应用。返回 world_note（可能空串）。

        返回 None 表示取消/无 API（调用方不计数、不推进 last_reconcile_tick-if-cancelled）。
        解析失败返回 ""（已计数、推进 last_reconcile_tick 避免反复撞同一失败）。
        """
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            return None
        tmp_preset = Preset(
            name="world_sim_tick_reconcile",
            system_prompt=preset.sim_system_prompt or DEFAULT_WORLDSIM_SIM_SYSTEM_PROMPT,
            temperature=preset.sim_temperature,
            max_tokens=preset.sim_max_tokens,
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        user_msg = self._build_reconcile_user_message(world, tick)
        messages = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return None
            result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
            if result.cancelled:
                return None
            if result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return ""
            data = _extract_json(result.content)
            if data and isinstance(data, dict) and data.get("mode") == "reconcile":
                return self._apply_llm_reconcile(world, data)
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 mode 字段，请严格按 schema 输出纯 JSON。"},
                ]
                continue
        return ""

    def _build_reconcile_user_message(self, world: World, tick: int) -> str:
        parts = ['{"mode": "reconcile"} 任务：据世界快照给出小幅势力调整建议（防长期漂移）。']
        parts.append(f"当前世界回合：{tick}；基调：{world.tone or '默认'}")
        # [P22] 世界书注入（前 5 条）：new_quests 派生与势力调整锚定世界观细节
        lore_line = _fmt_world_lore(world)
        if lore_line:
            parts.append(lore_line)
        # [P10c] 时间天气 + 近期事件（复用 _build_scene_context 格式）。
        # prompt 要求「呼应近期事件」派生 new_quests，但旧版快照根本没给事件 -> 派生无依据；补上。
        parts.append(_fmt_time_weather(world))
        ev_lines = _fmt_recent_events(world, 5)
        if ev_lines:
            parts.append("【近期世界动态】\n" + ev_lines)
        fac_by_id = {f.id: f for f in world.factions}
        fac_lines = []
        for f in world.factions:
            # [P10c] relations 的 key 是 faction_id（uuid），拼给 LLM 看到的是 uuid 而非势力名，
            # 违反 prompt「名称必须真实存在」要求。映射成势力名对齐口径（纯展示层修正，不动数据）。
            rels = ", ".join(
                f"{fac_by_id[other].name}: {v}" for other, v in f.relations.items()
                if other in fac_by_id
            ) or "无"
            fac_lines.append(
                f"- {f.name}（理念:{f.ideology or '未定'}；实力:{f.power}；财富:{f.wealth}；"
                f"好战度:{f.aggressiveness}；领地数:{len(f.territory)}；关系: {rels}）"
            )
        parts.append("【势力快照】\n" + "\n".join(fac_lines))
        # [P15b1] 玩家近况（供 new_quests 派生判断：任务要贴合玩家所在地与进行中目标）
        # [2026-08-23 用户卡绑定] 带姓名：派生任务文案提及玩家用真名。
        loc = next((l for l in world.locations if l.id == world.player.location_id), None)
        active_titles = [q.title for q in (world.quests or []) if q.status == "active"][:5]
        _uid_line = self._fmt_bound_user(world)
        parts.append(f"【玩家近况】{(_uid_line + '；') if _uid_line else ''}"
                     f"所在地点：{loc.name if loc else '未知'}；等级：{world.player.level}；"
                     f"进行中任务：{'、'.join(active_titles) if active_titles else '无'}")
        parts.append("请输出 faction_adjust + relation_adjust（delta 为建议方向，引擎会概率判定 + 钳制）。"
                     "若世界走向适合给玩家派生新任务（呼应近期事件/势力变化），可在 new_quests 给最多 1 条"
                     "（giver 用真实 NPC 名，objectives 引用真实名称）；平常回合给空数组。只输出纯 JSON。")
        return "\n\n".join(parts)

    def _apply_llm_reconcile(self, world: World, patch: dict) -> str:
        """应用 LLM 校准 patch（delta 概率应用 + 钳制）。返回 world_note。

        [!] 数值范式：LLM 给的是「建议 delta」，引擎据 delta 符号 + 幅度做概率 roll
            （幅度 >=3 才考虑、60% 概率采纳），再钳制。LLM 不直接定终值。
        """
        rng = SeededRng.seed_from(world.id, int(world.tick_count), "reconcile_apply")
        fac_by_name = {f.name: f for f in world.factions}
        for adj in (patch.get("faction_adjust") or []):
            if not isinstance(adj, dict):
                continue
            f = fac_by_name.get(str(adj.get("name", "") or "").strip())
            if not f:
                continue
            for k in ("power_delta", "wealth_delta"):
                delta = _safe_int(adj.get(k), 0, -50, 50)
                if delta == 0:
                    continue
                # 幅度>=3 且概率命中才采纳
                if abs(delta) >= 3 and rng.chance(0.6):
                    attr = "power" if "power" in k else "wealth"
                    setattr(f, attr, _safe_int(getattr(f, attr) + delta, getattr(f, attr), 0, 100))
        # 关系调整
        for rel in (patch.get("relation_adjust") or []):
            if not isinstance(rel, dict):
                continue
            a = fac_by_name.get(str(rel.get("a", "") or "").strip())
            b = fac_by_name.get(str(rel.get("b", "") or "").strip())
            delta = _safe_int(rel.get("delta"), 0, -100, 100)
            if not a or not b or delta == 0:
                continue
            if abs(delta) >= 3 and rng.chance(0.6):
                a.relations[b.id] = _safe_int(a.relations.get(b.id, 0) + delta,
                                              a.relations.get(b.id, 0), -100, 100)
                b.relations[a.id] = _safe_int(b.relations.get(a.id, 0) + delta,
                                              b.relations.get(a.id, 0), -100, 100)
        # [P15b1] 滴答派生新任务：new_quests 白名单清洗入库（最多 1 条/次，available）。
        # 数值范式：LLM 出静态任务定义（目标/奖励种子），进度判定仍全代码（quest_engine）。
        try:
            new_q = self._apply_reconcile_new_quest(world, patch, int(world.tick_count))
        except Exception:
            new_q = None
        # [!] 不在此再跑 wte.reconcile_world：本 tick 主流程 reconcile 阶段已跑过，patch 的
        # delta 又已逐条 _safe_int 钳制 0-100/-100..100——二次调用会让稳定度 +2、power 单 tick
        # 双重回归（10% 融合两次），扰动 P45 稳定度 50 阈值跨越口径。
        note = str(patch.get("world_note", "") or "").strip()
        if new_q is not None:
            note = (note + "；" if note else "") + f"世界派生了新任务「{new_q.title}」"
        return note

    # ============ [P34f] 交易所股市每日定价 ============

    def _phase_or_call_stock(self, world, preset, tick, cancel_check):
        """[P34f] tick_stock_market 的 _phase 隔离包装：异常归 ""（推进水位避免下 tick 重复撞）。"""
        try:
            return self.tick_stock_market(world, preset, tick, cancel_check)
        except Exception as e:  # noqa: BLE001 - 阶段隔离兜底
            debug_log(lambda: f"[WorldSim] tick 阶段 stock_market 异常（跳过）: {e}")
            return ""

    def _tick_npc_life(self, world: World, preset: WorldSimPreset, tick: int,
                       cancel_check=None) -> list:
        """[P36a] NPC 生活每日推进：per-day 水位（last_life_day）+ roster 执行。
        LLM 批量日计划每 npc_life_llm_interval 天一次（单次调用出全部活跃 NPC 计划，
        仿 P29 批量记忆模式）；间隔日/失败/取消回退规则 AI（引擎内）。"""
        day = max(1, int(getattr(world, "day_count", 1) or 1))
        last = max(0, int(getattr(world, "last_life_day", 0) or 0))
        if last == day:
            return []
        world.last_life_day = day
        # [P37] 池口径只看 alive（原 level>=1 排除商人/平民）；level 0 成员补生活基线
        pool = [n for n in (getattr(world, "npcs", None) or []) if getattr(n, "alive", False)]
        for n in pool:
            nle.ensure_life_baseline(n)
        if not pool:
            return []
        # 全体普通居民有基础谋生；日计划 roster 是额外的具体行动，二者不抢同一工资。
        livelihood_evts = nle.daily_civilian_livelihood(
            world, social_on=bool(self._per_world(world, "social_enabled",
                                                 preset.social_enabled, preset)))
        # [D3 2026-08-29] 赠品使用闭环：全存活 NPC 每日真实用掉随身物品
        # （低血喝药/五维药/技能书研读，先于当日行动——救命不等排班）。
        # [A1 2026-08-29] 装备自理：换更优装备 + 旧装备出清（商人上架/折价换钱）。
        usage_evts = []
        for n in pool:
            ev = nle.daily_item_usage(world, n)
            if ev is not None:
                usage_evts.append(ev)
            ev2 = nle.daily_equip_check(world, n)
            if ev2 is not None:
                usage_evts.append(ev2)
            # [A2 2026-08-29] 缺血自知：仍伤且无药 -> 找药店买（喝药之后跑，闭环
            # 「买药(D1 天) -> 喝药(次日 D3)」）。
            ev3 = nle.daily_medicine_run(world, n)
            if ev3 is not None:
                usage_evts.append(ev3)
            # [记忆驱动] 危难求援：好友重伤无药时向玩家传讯（含地点，赠药闭环 D3）
            ev4 = nle.daily_help_call(world, n)
            if ev4 is not None:
                usage_evts.append(ev4)
        cap = max(1, int(self._per_world(world, "npc_life_per_day", preset.npc_life_per_day, preset) or 6))
        plans: dict = {}
        interval = max(0, int(self._per_world(world, "npc_life_llm_interval",
                                              preset.npc_life_llm_interval, preset) or 0))
        if interval > 0 and (day % interval == 0 or last == 0):
            plans = self._plan_npc_life_llm(world, preset, cap, cancel_check) or {}
        # [P45 2026-09-12 v3] 材料出清（用户定稿）：背包 >8 件 -> 卖最近商店当场结算 +
        # npc_made 上架。放在当日行动之前：腾出槽位给当日采集/合成/打猎产出
        # （_inv_add 在 _NPC_INV_CAP=16 满仓时对一切新增返回 False）。
        for n in pool:
            try:
                ev_sell = nle.daily_sell_surplus(world, n)
            except Exception as e5:  # noqa: BLE001 - 单 NPC 出清异常不吞整日
                debug_log(lambda: f"[WorldSim] NPC 材料出清异常（跳过）: {e5}")
                ev_sell = None
            if ev_sell is not None:
                usage_evts.append(ev_sell)
        rng = SeededRng.seed_from(getattr(world, "id", ""), tick, "npc_life")
        result = livelihood_evts + usage_evts + nle.advance_life(world, plans, rng, max_n=cap)
        # [P45 2026-09-12 方案 A2] 每日被动遭遇：战斗身份 NPC 各自按「附近野外地点 danger」
        # roll 一次遇怪，命中即开打（复用 encounter_chance + roll_wilderness_monster，
        # 不再依赖 6 人 roster）。放在当日行动之后：roster 打完的伤者被半血门槛拦住，
        # 尸体不再参与（alive 门）；双开关与玩家侧同源——wilderness_monsters_enabled
        # （野外怪物总闸）+ npc_autonomous_enabled（离屏 NPC 自主行为闸）。
        try:
            if bool(self._per_world(world, "npc_autonomous_enabled",
                                    preset.npc_autonomous_enabled, preset)):
                _base = float(self._per_world(world, "wilderness_monster_chance",
                                              preset.wilderness_monster_chance, preset) or 0.0)
                _mon_on = bool(self._per_world(world, "wilderness_monsters_enabled",
                                               preset.wilderness_monsters_enabled, preset))
                _season = 1.0
                try:
                    from src.services import calendar_engine as _cale
                    _season = float(_cale.season_fx(world).get("monster_chance", 1.0) or 1.0)
                except Exception:  # noqa: BLE001 - 季节模块不可用时按无季节系数
                    _season = 1.0
                result += nle.daily_passive_encounters(world, base_chance=_base,
                                                       monsters_on=_mon_on,
                                                       season_mult=_season)
        except Exception as e6:  # noqa: BLE001 - 被动遭遇段整体隔离，不炸穿 tick_world
            debug_log(lambda: f"[WorldSim] NPC 每日被动遭遇异常（跳过）: {e6}")
        return result

    def _plan_npc_life_llm(self, world: World, preset: WorldSimPreset, cap: int,
                           cancel_check=None) -> Optional[dict]:
        """[P36a] LLM 批量日计划（单次调用；三态仿 _start_auction_llm：None=取消/无API/失败
        -> 调用方走规则 AI）。[!] user 消息带世界观锚（守 OOC 开发规则 + §23 联动）。"""
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            return None
        from src.models.world_sim_preset import DEFAULT_NPC_LIFE_PLAN_PROMPT
        tmp_preset = Preset(
            name="world_sim_npc_life",
            system_prompt=preset.npc_life_plan_prompt or DEFAULT_NPC_LIFE_PLAN_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        # [P37] 候选池与 advance_life 同口径（只看 alive + 生活基线），LLM 名单与执行 roster 一致
        cands = [n for n in (getattr(world, "npcs", None) or []) if getattr(n, "alive", False)]
        for n in cands:
            nle.ensure_life_baseline(n)
        if not cands:
            return None
        roster_rng = SeededRng.seed_from(getattr(world, "id", ""),
                                         int(getattr(world, "day_count", 1) or 1), "life_roster")
        k = max(1, min(int(cap or 6), len(cands)))
        roster = roster_rng.sample(cands, k) if len(cands) > k else list(cands)
        # [记忆驱动 2026-08-29] 注入最近记忆（无 LLM 直读，预算 2 条 x 60 字）——
        # 「上次玩家卖我的货好卖」影响今日进货/去向，世界开始记得发生过什么。
        try:
            mem_svc = self.npc_memory()
        except Exception:
            mem_svc = None
        lines = []
        for n in roster:
            ln = (f"- {n.name}：{n.role or '平民'}｜{n.current_goal or n.goal or '日常度日'}"
                  f"｜所在地 {next((l.name for l in world.locations if l.id == n.location_id), '未知')}"
                  f"｜钱包 {n.wallet}｜HP {n.hp}/{n.hp_max}"
                  f"｜随身 {[next((i.name for i in world.items if i.id == iid), iid) for iid in (n.inventory or [])[:4]]}")
            loc = next((l for l in world.locations if l.id == n.location_id), None)
            ln += f"｜可采资源点 {sum(1 for node in (getattr(loc, 'resource_nodes', None) or []) if int(getattr(node, 'richness', 0) or 0) > 0 and int(getattr(node, 'cooldown_tick', 0) or 0) <= int(getattr(world, 'tick_count', 0) or 0))}"
            ln += f"｜本地商店 {sum(1 for s in world.shops if s.location_id == n.location_id)}"
            ln += f"｜现可制作 {[r.name for r in nle._basic_recipes(world, n)[:3]]}"
            own_shop = nle._own_shop(world, n)
            if own_shop is not None:
                ln += f"｜自家店 {own_shop.name}"
            if (getattr(n, "current_thought", "") or "").strip():
                ln += f"｜此刻惦记：{str(n.current_thought)[:50]}"
            # [野心 2026-09-06] 一生追求注入（日计划顺带，零新增调用——今日行动可服务于
            # 长期目标：攒钱的去卖货、游历的出远门、匠心的去合成）
            amb_desc = str((getattr(n, "ambition", None) or {}).get("desc", "") or "")
            if amb_desc:
                ln += f"｜毕生所愿：{amb_desc}"
            # [剧情线 P46] 参与者日计划贴阶段（ambition 注入同模式，零新增调用）
            _arc_ln = sae.arc_line_for_npc(world, n.id)
            if _arc_ln:
                ln += f"｜{_arc_ln}"
            if mem_svc is not None:
                try:
                    mem = mem_svc.recent_lines(world, n, 2)
                except Exception:
                    mem = []
                if mem:
                    ln += f"｜记忆：{'；'.join(mem)}"
            lines.append(ln)
        gt = self._genre_text(world)
        user_msg = (
            '{"mode":"npc_life"} 任务：为下列 NPC 规划今日行动。\n'
            # [修 2026-09-10] 锚行收敛到 `_world_anchor` 单一来源（此处原是全文件最后一处手拼：
            # premise 截 60 字 + 题材逗号用「、」，与其它调用点口径不一致）
            f"{_world_anchor(world)}（题材 id：{self._genre_id(world)}，货币：{gt.currency}）\n"
            "候选名单：\n" + "\n".join(lines) +
            "\n动作白名单：gather/craft/stock_shop/buy_materials/hunt/rest；"
            "craft 可附 target（今日想造的物品名，从随身材料与业态取材）；"
            "请按钱包、库存、伤势、本地资源和长期心愿选可执行的行动。"
            "可采资源点为 0 时不要选 gather；现可制作为空时不要选 craft。\n"
            "只输出纯 JSON（plans 覆盖全部名单）。"
        )
        messages = [{"role": "system", "content": tmp_preset.system_prompt},
                    {"role": "user", "content": user_msg}]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return None
            result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                         block_labels=["系统", "用户"])
            if result.cancelled or result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return None
            data = _extract_json(result.content)
            if data and isinstance(data, dict) and isinstance(data.get("plans"), list):
                plans = {}
                for p in data["plans"]:
                    if isinstance(p, dict):
                        nm = str(p.get("name") or "").strip()
                        act = str(p.get("action") or "").strip()
                        if nm and act:
                            # [P37] target（可选）：今日想造/想卖的物品名，craft 选配方用
                            # （pick_craft_recipe 名匹配，不中回退确定性优先级）
                            tgt = str(p.get("target") or "").strip()
                            # [A+B 2026-09-06] 念头/目标落 NPC 持久字段（advance_life 落值）
                            th = str(p.get("thought") or "").strip()[:60]
                            gl = str(p.get("goal") or "").strip()[:60]
                            plans[nm] = {"action": act, "target": tgt,
                                         "thought": th, "goal": gl}
                return plans
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 plans 字段，请严格按 schema 输出纯 JSON。"},
                ]
                continue
        return None

    # ---- [P39d+e] 剧情导演层 + 世界编年史 ----
    # 一次性小调用不提供用户定制面（仿购宅起名先例：无 PROMPT_DEFAULTS_REV 版本门，失败回退拼接）。
    _CHRONICLE_SYS_PROMPT = (
        "你是这个世界的史官。把给定时期的大事记压缩成一条编年史条目：只用一段不超过 120 字的"
        "中文散文，按时间顺序概括最重要的事件脉络；若其中有无名冒险者（玩家）参与的壮举，"
        "用「那位冒险者」指代并保留其存在感；不列清单、不出 JSON、不加标题。"
    )

    # [④ 2026-08-30] 市井传闻口述化（一次性小调用，无版本门，仿编年史先例）。
    _RUMOR_SYS_PROMPT = (
        "你是这个世界的说书人。把每条大事写成一句市井传闻：用市井口吻（茶馆/街头/货栈里人们"
        "会怎么传），一句不超过 40 字的中文，保留关键人物与地点，不评判、不夸张到失真；"
        "涉及无名冒险者（玩家）时用「那位冒险者」指代。只输出 JSON：{\"rumors\": [\"传闻1\", ...]}，"
        "条数与输入一致、顺序一致，不要任何说明或代码块标记。"
    )

    # [⑥ 2026-08-30] NPC 性格偏移（引擎判转折点，LLM 只写「他因此变成了什么样」）。
    _PERSONALITY_DRIFT_SYS_PROMPT = (
        "你是 SLG 游戏的 NPC 人格演化助手。每个 NPC 刚经历了一件重大变故（输入已给出），"
        "请为其写一句「性格偏移」：只描述「他/她因此变成了什么样的人、待人或说话方式有何变化」，"
        "一句话不超过 40 字，必须锚在给出的经历上，不得编造经历之外的变化；变化要克制，"
        "多数人经历大事后只是轻微改变，不要人人黑化。只输出 JSON："
        "{\"drifts\": [{\"ref\": \"NPC名(原样返回)\", \"drift\": \"性格偏移一句\"}]}，"
        "不要任何说明或代码块标记。"
    )

    def _tick_chronicle(self, world: World, preset: WorldSimPreset, tick: int,
                        cancel_check=None) -> list:
        """[P39d+e] 编年史子阶段：收编 pending -> 累积 40 触发沉淀（[定稿] 压最老 30）。

        LLM 三态仿股市：None=取消（不沉淀，pending 保留下 tick 重试）；""=无 API/失败
        （回退引擎拼接标题，同样推进水位防死循环）；非空=成功。静默不产事件。"""
        che.collect_pending(world)
        batch = che.take_batch(world)
        if not batch:
            return []
        text = self._llm_chronicle_summary(world, preset, batch, cancel_check)
        if text is None:
            return []
        che.commit_chronicle(world, batch, text or che.fallback_summary(batch))
        return []

    def _llm_chronicle_summary(self, world: World, preset: WorldSimPreset, batch: list,
                               cancel_check=None) -> Optional[str]:
        """把一批大事记总结成 <=120 字编年史条目（失败回退在调用方）。"""
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            return ""
        tmp_preset = Preset(
            name="world_sim_chronicle",
            system_prompt=self._CHRONICLE_SYS_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        gt = self._genre_text(world)
        lines = [f"- [第{che.tick_day(int(b.get('tick', 0) or 0))}天] "
                 f"{b.get('title', '')}：{b.get('desc', '')}"
                 for b in batch if isinstance(b, dict)]
        user_msg = (
            f"{_world_anchor(world)}（货币：{gt.currency}）\n"
            "时期大事记：\n" + "\n".join(lines) +
            "\n只输出这一条编年史条目（<=120 字中文散文）。"
        )
        result = llm.chat_cancelable(
            [{"role": "system", "content": tmp_preset.system_prompt},
             {"role": "user", "content": user_msg}],
            cancel_check=cancel_check, block_labels=["系统", "用户"])
        if result.cancelled:
            return None
        if result.error or not (result.content or "").strip():
            return ""
        text = str(result.content).strip().strip('"“”「」`').strip()
        return text[:240] or ""

    # ---- [④ 2026-08-30] 市井传闻口述化（计数触发 + 预算闸，仿编年史三态）----
    def _tick_rumor_voice(self, world: World, preset: WorldSimPreset, tick: int,
                          cancel_check=None) -> bool:
        """[④] 传闻口述化：take_batch -> LLM 批量口述 -> commit。返回是否消耗 1 预算单位。"""
        batch = rme.take_batch(world)
        if not batch:
            return False
        texts = self._llm_rumor_voice(world, preset, batch, cancel_check)
        if texts is None:
            return False  # 取消/无 API：不沉淀不计数（pending 保留下 tick 重试）
        rme.commit(world, batch, texts)  # 失败回退 desc 文本，同样推进水位
        return True

    def _llm_rumor_voice(self, world: World, preset: WorldSimPreset, batch: list,
                         cancel_check=None) -> "Optional[list]":
        """把一批 major/crisis 事件口述化成「市井传闻」列表（按序）。None=取消/无 API。"""
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            return None
        tmp_preset = Preset(
            name="world_sim_rumor",
            system_prompt=self._RUMOR_SYS_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        gt = self._genre_text(world)
        lines = [f"{i + 1}. [{rme.tick_day(int(b.get('tick', 0) or 0))}日] "
                 f"{b.get('title', '')}：{b.get('desc', '')}"
                 for i, b in enumerate(batch) if isinstance(b, dict)]
        user_msg = (
            f"{_world_anchor(world)}（货币：{gt.currency}）\n"
            "近期大事：\n" + "\n".join(lines) +
            "\n只输出 JSON：{\"rumors\": [...]}，条数与顺序与输入一致。"
        )
        result = llm.chat_cancelable(
            [{"role": "system", "content": tmp_preset.system_prompt},
             {"role": "user", "content": user_msg}],
            cancel_check=cancel_check, block_labels=["系统", "用户"])
        if result.cancelled:
            return None
        if result.error or not (result.content or "").strip():
            return []
        data = _extract_json(result.content)
        rumors = data.get("rumors") if isinstance(data, dict) else None
        if not isinstance(rumors, list):
            return []
        return [str(r or "").strip() for r in rumors]

    def tick_stock_market(self, world: World, preset: WorldSimPreset, tick: int,
                          cancel_check: Optional[Callable[[], bool]] = None) -> Optional[str]:
        """[P34f] 每日 1 次 LLM 行情定价（镜像 _llm_reconcile 三态）。

        三态返回：
        - None = 取消/无 API（调用方不推进 last_stock_priced_day，不计 LLM 预算，下日重试）。
        - ""  = 解析失败走 SeededRng 兜底（推进水位 + 计预算，避免下 tick 重复撞同一失败）。
        - 非空串 = 成功（推进水位 + 计预算 + 返回行情变动摘要）。

        [!] ±drift_pct 钳制防崩盘（apply_llm_prices 内部锚 prev_price）；兜底 drift_prices_fallback
        锚 base_price 防随机游走（仿 trade_engine.drift_prices）。
        """
        if not self._per_world(world, "stock_market_enabled",
                               preset.stock_market_enabled, preset):
            return None
        sm = getattr(world, "stock_market", None)
        if sm is None or not getattr(sm, "commodities", None):
            return None  # 无商品（未初始化），不定价
        drift_pct = float(self._per_world(world, "stock_price_drift_pct",
                                          preset.stock_price_drift_pct, preset) or 0.0)
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            # 无 API 走兜底（推进水位；返回 "" 会被调用方计 1 预算——与解析失败同口径，
            # 防无 API 时每 tick 重复撞本分支）
            rng = SeededRng.seed_from(getattr(world, "id", ""),
                                      int(getattr(world, "day_count", 0) or 0), "stock_drift")
            ske.drift_prices_fallback(world, rng, drift_pct)
            ske.apply_faction_pressure(world)      # [P41] 势力局势确定性层
            return ""
        tmp_preset = Preset(
            name="world_sim_tick_stock",
            system_prompt=preset.stock_market_prompt or DEFAULT_STOCK_MARKET_PROMPT,
            temperature=preset.sim_temperature,
            max_tokens=preset.sim_max_tokens,
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        user_msg = self._build_stock_user_message(world, preset)
        messages = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return None
            result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                         block_labels=["系统", "用户"])
            if result.cancelled:
                return None
            if result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                # 两轮失败 -> 兜底
                rng = SeededRng.seed_from(getattr(world, "id", ""),
                                          int(getattr(world, "day_count", 0) or 0), "stock_drift")
                ske.drift_prices_fallback(world, rng, drift_pct)
                ske.apply_faction_pressure(world)  # [P41] 势力局势确定性层
                return ""
            data = _extract_json(result.content)
            if data and isinstance(data, dict) and data.get("mode") == "stock":
                if ske.apply_llm_prices(world, data, drift_pct):
                    ske.apply_faction_pressure(world)  # [P41] 势力局势确定性层（最后落地）
                    return ske.market_summary(world)
                # mode 对但 prices 未命中任何 commodity -> 兜底
                rng = SeededRng.seed_from(getattr(world, "id", ""),
                                          int(getattr(world, "day_count", 0) or 0), "stock_drift")
                ske.drift_prices_fallback(world, rng, drift_pct)
                ske.apply_faction_pressure(world)  # [P41] 势力局势确定性层
                return ""
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 mode 字段，请严格按 schema 输出纯 JSON。"},
                ]
                continue
        # 两轮都未拿到合法 JSON -> 兜底
        rng = SeededRng.seed_from(getattr(world, "id", ""),
                                  int(getattr(world, "day_count", 0) or 0), "stock_drift")
        ske.drift_prices_fallback(world, rng, drift_pct)
        ske.apply_faction_pressure(world)  # [P41] 势力局势确定性层（5 个兜底出口全补齐，防漏）
        return ""

    def _build_stock_user_message(self, world: World, preset: WorldSimPreset) -> str:
        """[P34f] 股市定价 LLM user 消息：近期事件 + 时间天气 + 经济节奏 + 当前行情。"""
        parts = ['{"mode": "stock"} 任务：据世界形势为每件商品定当日新价（prices 覆盖全部 symbol）。']
        parts.append(f"基调：{world.tone or '默认'}；经济节奏：{self._per_world(world, 'economy_pace', 'standard', preset)}")
        # 时间天气 + 历法（复用 calendar_engine 纯函数）
        try:
            parts.append(_fmt_time_weather(world))
            cal = cale.calendar(world)
            if cal:
                parts.append(f"历法：{cal.get('date_str', '')}")
        except Exception:
            pass
        ev_lines = _fmt_recent_events(world, 5)
        if ev_lines:
            parts.append("【近期世界动态】\n" + ev_lines)
        # [P41] 势力局势驱动定价
        _fp = ske.faction_pressure(world)
        if _fp["battles"] or _fp["flips"]:
            parts.append(f"【势力局势】近一日战事 {_fp['battles']} 场、领土易主 {_fp['flips']} 次。"
                         "战事推高[军需]类、战时征调令[民生]类短缺小涨、乱局使[奢侈]类贬值——"
                         "你的定价须与该方向一致（幅度自定）。")
        parts.append("【当前行情】\n" + ske.market_summary(world))
        parts.append("请输出 prices（覆盖全部 symbol，新价为正整数）。系统会按 ±% 上限钳制防崩盘。只输出纯 JSON。")
        return "\n\n".join(parts)

    # ============ [P34g] 拍卖会事件 ============

    def _tick_auctions(self, world: World, preset: WorldSimPreset, tick: int,
                       cancel_check, llm_budget_avail: bool) -> tuple[list, int]:
        """[P34g] 拍卖会每日子阶段：end 到期拍卖 + start 到期拍卖（LLM 出拍品或兜底）+ 周期生成。

        返回 (events, llm_calls_used)。自管 try/except（含 LLM 调用），异常归 ([], 0) 不阻断 tick。
        start_auction 的 LLM 骨架注入走 _start_auction_llm（耗预算）；无预算/失败走兜底池。
        """
        try:
            interval = max(1, int(self._per_world(world, "auction_interval_days",
                                                  preset.auction_interval_days, preset) or 0))
            duration = max(1, int(self._per_world(world, "auction_duration_days",
                                                  preset.auction_duration_days, preset) or 0))
            lot_count = max(1, int(self._per_world(world, "auction_lot_count",
                                                   preset.auction_lot_count, preset) or 0))
            day = int(getattr(world, "day_count", 1) or 1)
            llm_used = 0
            # end 到期拍卖（[审查修复] 结算事件须收集返回——拍品入包/流拍 sink/退押金的
            # 叙事反馈靠它进 event_log/编年史，丢弃=玩家拍得物品凭空入包零反馈）
            events: list = []
            for auction in list(getattr(world, "auctions", None) or []):
                if not isinstance(auction, AuctionEvent):
                    continue
                if auction.status == "active" and day >= int(auction.end_day or 0):
                    events.extend(aue.end_auction(world, auction))
            # start 到期拍卖：LLM 骨架注入（耗预算）+ 兜底
            for auction in list(getattr(world, "auctions", None) or []):
                if not isinstance(auction, AuctionEvent):
                    continue
                if auction.status == "upcoming" and day >= int(auction.start_day or 0):
                    skeletons = None
                    if llm_budget_avail:
                        skeletons = self._start_auction_llm(world, auction, preset,
                                                            cancel_check)
                        if skeletons is not None:
                            llm_used += 1  # [!] 累加——多场同日开场是多次独立 LLM 调用
                    aue.start_auction(world, auction, lot_count,
                                      llm_skeletons=skeletons,
                                      item_max_level=int(getattr(preset, "item_max_level", 6) or 0),
                                      preset=preset)
            # [NPC 竞拍 2026-09-11] NPC 同场竞价：每日 1 次（水位 last_auction_bid_day）。
            # 放在 start 之后（当日新开的拍品已有 lot）、end 之后（结束当天不再跑这条路）。
            # 零 LLM；竞价过程不产事件（否则每天 N 件撞 max_events_per_tick）。
            if int(getattr(world, "last_auction_bid_day", 0) or 0) != day:
                world.last_auction_bid_day = day
                for auction in list(getattr(world, "auctions", None) or []):
                    if not isinstance(auction, AuctionEvent) \
                            or str(getattr(auction, "status", "") or "") != "active":
                        continue
                    aue.npc_bidding_round(
                        world, auction,
                        SeededRng.seed_from(getattr(world, "id", ""), day,
                                            f"auction_bid_{auction.id}"))
            # 周期生成新拍卖（纯引擎）
            rng = SeededRng.seed_from(getattr(world, "id", ""), tick, "auction")
            spawn_events = aue.maybe_spawn_auction(world, rng, interval, duration)
            return events + spawn_events, llm_used
        except Exception as e:  # noqa: BLE001 - 阶段隔离兜底
            debug_log(lambda: f"[WorldSim] tick 阶段 auctions 异常（跳过）: {e}")
            return [], 0

    def _start_auction_llm(self, world: World, auction: AuctionEvent,
                           preset: WorldSimPreset,
                           cancel_check) -> Optional[list]:
        """[P34g] LLM 出拍品骨架（镜像 tick_stock_market 三态，但这里返回骨架 list 或 None）。

        None = 取消/无 API/失败 -> 调用方走兜底池（start_auction 传 llm_skeletons=None）。
        非 None = LLM 骨架 [{name,type,rarity,level,desc}]（可能为空列表）。
        """
        api = self._resolve_api(
            self._per_world(world, "sim_api_id", preset.sim_api_id, preset)
            or preset.calculator_api_id)
        if not api:
            return None
        lot_count = max(1, int(self._per_world(world, "auction_lot_count",
                                               preset.auction_lot_count, preset) or 0))
        tmp_preset = Preset(
            name="world_sim_auction_gen",
            system_prompt=preset.auction_generate_prompt or DEFAULT_AUCTION_GENERATE_PROMPT,
            temperature=preset.calculator_temperature,
            max_tokens=max(10000, preset.calculator_max_tokens),
            top_p=preset.calculator_top_p,
        )
        llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
        user_msg = self._build_auction_user_message(world, lot_count)
        messages = [
            {"role": "system", "content": tmp_preset.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        for attempt in range(2):
            if cancel_check and cancel_check():
                return None
            result = llm.chat_cancelable(messages, cancel_check=cancel_check,
                                         block_labels=["系统", "用户"])
            if result.cancelled or result.error or not (result.content or "").strip():
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": result.content or ""},
                        {"role": "user", "content": "[!] 上次输出无法解析，请严格按 schema 输出纯 JSON。"},
                    ]
                    continue
                return None
            data = _extract_json(result.content)
            if data and isinstance(data, dict) and data.get("mode") == "auction" \
                    and isinstance(data.get("lots"), list):
                return data["lots"]
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": "[!] 上次输出不是合法 JSON 或缺 mode 字段，请严格按 schema 输出纯 JSON。"},
                ]
                continue
        return None

    def _build_auction_user_message(self, world: World, lot_count: int) -> str:
        """[P34g] 拍卖拍品 LLM user 消息。[!] JSON 字面量前缀必须用普通字符串——
        f-string 里 `f'{"mode":"auction"}'` 会把 :"auction" 解析成格式说明符直接抛
        Invalid format specifier（p34r2 验收发现：异常被 _tick_auctions 阶段隔离吞掉，
        拍卖会自上线以来一直静默走兜底池、从未用上 LLM 拍品）。镜像 _build_stock_user_message。"""
        gt = self._genre_text(world)
        gid = self._genre_id(world)
        return (
            '{"mode":"auction"} 任务：为一场拍卖会生成 '
            f"{lot_count} 件拍品骨架。\n"
            f"题材：{gid}（货币：{gt.currency}）；玩家等级：{world.player.level}。\n"
            "请输出 lots 数组（覆盖全部拍品，品级以 rare/epic 为主，legendary 压轴，level 可超商店上限）。"
        )

    def _apply_reconcile_new_quest(self, world: World, patch: dict, tick: int) -> Optional[Quest]:
        """解析 reconcile 的 new_quests（只取字面第 1 条，最多 1 条/次）清洗入库。

        白名单：title 非空且不与世界已有任务重名；objectives type 白名单（空则整条跳过）；
        giver 限存活真实 NPC 名；rewards.items 名字解析成 item id（找不到丢弃）；数值钳制。
        第 1 条非法时不回看第 2 条（与「平常回合最多派生 1 条」的提示词口径一致）。
        """
        if not qe._quests_on(world):
            return None
        raw = patch.get("new_quests") or []
        if not isinstance(raw, list):
            return None
        item_by_name = {i.name: i for i in world.items}
        for rq in raw[:1]:
            if not isinstance(rq, dict):
                continue
            title = str(rq.get("title", "") or "").strip()
            if not title or any(x.title == title for x in (world.quests or [])):
                continue
            giver_name = str(rq.get("giver", "") or "").strip()
            # [P44] 容错解析（LLM 写 NPC 名常加修饰/别字，精确查会丢发布者变无主任务）
            giver = _npc_by_name(world, giver_name, alive_only=True)
            objectives = []
            raw_objs = rq.get("objectives") or []
            if not isinstance(raw_objs, list):
                raw_objs = []
            for ro in raw_objs:
                if isinstance(ro, dict) and ro.get("type") in (
                        "kill", "gather", "talk", "visit", "collect",
                        "investigate", "escort"):   # [S04 余项] 复合目标（target+target2）
                    objectives.append({
                        "type": ro.get("type"),
                        "target": str(ro.get("target", "") or ""),
                        "count": _safe_int(ro.get("count"), 1, 1, 999),
                        "current": 0,
                        "desc": str(ro.get("desc", "") or ""),
                    })
            if not objectives:
                continue  # 无可判定目标的新任务是死数据，整条跳过
            raw_rw = rq.get("rewards") or {}
            rw_items = []
            if isinstance(raw_rw, dict):
                for nm in (raw_rw.get("items") or [])[:3]:
                    # [P44] 容错解析（奖励物品名锚定 world.items，找不到仍丢弃）
                    # [任务奖励物品] 与 LLM 开局任务同口径：不合规的奖励物品一并丢弃
                    it = _name_lookup(item_by_name, nm)
                    if it is not None and it.id not in rw_items \
                            and qe.reward_item_allowed(it, allow_skillbook=True):
                        rw_items.append(it.id)
            rewards = {
                "items": rw_items,
                "gold": _safe_int(raw_rw.get("gold") if isinstance(raw_rw, dict) else None, 50, 0, 10 ** 6),
                "xp": _safe_int(raw_rw.get("xp") if isinstance(raw_rw, dict) else None, 40, 0, 10 ** 6),
            }
            q = Quest(
                title=title,
                objective=str(rq.get("objective", "") or ""),
                giver_npc_id=giver.id if giver is not None else "",
                reward_text=str(rq.get("reward", "") or ""),
                status="available",
                objectives=objectives,
                rewards=rewards,
            )
            world.quests.append(q)
            return q
        return None


def _move_npc_safe(npc, from_id: str, to_id: str, loc_by_id: dict, to_place_id: str = ""):
    """移动 NPC：更新 location_id + 两地点 npc_ids（service 层用，与引擎层 _move_npc 同口径）。

    [P27] to_place_id 非空时同步设 npc.place_id（跨地点移动重置场所）；
    空串时 place_id 不变（调用方按需传）。场所级 npc_ids 双向登记由 _move_to_place 负责。
    """
    npc.location_id = to_id
    # [野心 2026-09-06] 游历判定：到访地点去重记录（visited_locations cap 60）
    if to_id and to_id not in (getattr(npc, "visited_locations", None) or []):
        vl = list(getattr(npc, "visited_locations", None) or [])
        vl.append(to_id)
        npc.visited_locations = vl[:60]
    if to_place_id:
        npc.place_id = to_place_id
    frm = loc_by_id.get(from_id)
    to = loc_by_id.get(to_id)
    if frm is not None and npc.id in frm.npc_ids:
        frm.npc_ids = [i for i in frm.npc_ids if i != npc.id]
    if to is not None and npc.id not in to.npc_ids:
        to.npc_ids.append(npc.id)

