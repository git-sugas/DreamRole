"""[P12] 野外怪物遭遇引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

野外地点（Location.kind == wilderness）玩家每回合行动后判定遇怪：命中则强制触发战斗。
- 触发概率：基础 + 危险度加成（采集行动更高），全确定性 roll（同 world+tick+salt 同结果）。
- 怪物等级：max(地点危险度, 玩家等级-1) 动态成长（跟随玩家等级）。
- 精英怪：概率触发 +2 级 + 掉落率提升 + 题材前缀（凶悍的/妖化的…）。
- 怪物实体：临时 NPC（不入 world.npcs，战后丢弃），属性全代码结算；
  掉落 = 怪物池 loot（LLM 生成/题材兜底）+ 所在地点资源层级材料。

守数值范式铁律：怪物属性/掉落全引擎算，LLM 只在世界生成时出静态怪物池定义。
"""
from __future__ import annotations

from typing import Any, Optional

from src.models.world import NPC, _CONDITION_VALUES, _clean_elements
from src.services.combat_engine import infer_elements_from_skills, max_hp_for
from src.utils.rng import SeededRng

# 资源层级 <-> 稀有度 双射（T1=common ... T5=legendary）
_TIER_RARITY = {1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary"}
_RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary"]


# [P43 重构 2026-09-12 用户指示] 危险度语义：danger = 比主体「凶多少级」，不再是
# 「这个地区属于 1-100 的哪一段」。怪物等级 = [主体等级, 主体等级 + danger + 3] 段内随机，
# 封顶 100（满级不被打破）：1 级玩家进 danger 10 区遇 1..14 级，100 级玩家仍是 100 级。
_LEVEL_CAP = 100


def band_level(danger: int, actor_level: int, rng=None, cap: int = _LEVEL_CAP) -> int:
    """[P43 重构 2026-09-12] 遭遇怪物等级：区间 [主体等级, 主体等级 + danger + 3]，段内随机。

    换掉旧「danger 定 1-100 段 + 段内跟随玩家-1」口径的两个毛病：
    - soften 软底（floor=min(段底, 玩家+3)）让 10 级玩家在 danger 3~10 遇到的**全是
      13-15 级**，深山与绝地毫无区别（地区强弱层次名存实亡）；
    - 硬段底路径（世界 Boss / NPC 打猎）被地段钉死——5 级 NPC 在 danger 5 区要打 41 级
      怪，NPC 根本升不上去（与「NPC 低起步慢慢升级」的设定直接冲突）。

    rng=None 时不随机、直接取段顶（世界 Boss 等要「最凶一档」的调用方）。
    """
    base = max(1, min(int(cap), int(actor_level or 1)))
    width = max(0, int(danger or 0) + 3)
    top = min(int(cap), base + width)
    if rng is None:
        return max(1, top)
    return max(1, rng.roll(base, max(base, top)))


def elite_level_bonus(base_lvl: int) -> int:
    """精英加成：+15%（至少 +2），封顶 100。"""
    return max(2, int(round(base_lvl * 0.15)))


def danger_to_tier(danger: int) -> int:
    """危险度 1-10 -> 资源层级 1-5（1-2->1 / 3-4->2 / 5-6->3 / 7-8->4 / 9-10->5）。"""
    try:
        d = int(danger)
    except (TypeError, ValueError):
        d = 1
    return max(1, min(5, (max(1, min(10, d)) + 1) // 2))


def tier_to_rarity(tier: int) -> str:
    return _TIER_RARITY.get(max(1, min(5, int(tier or 1))), "common")


def nearest_rarity_pool(items: list, tier: int) -> list:
    """按层级取对应稀有度的 material 池；缺档向低/高档就近取；全空回退 material+consumable。"""
    mats: dict[str, list] = {}
    for it in items:
        if getattr(it, "type", "") == "material":
            mats.setdefault(getattr(it, "rarity", "common") or "common", []).append(it)
    i = _RARITY_ORDER.index(tier_to_rarity(tier))
    for d in range(len(_RARITY_ORDER)):
        for j in ((i - d), (i + d)):
            if 0 <= j < len(_RARITY_ORDER) and mats.get(_RARITY_ORDER[j]):
                return mats[_RARITY_ORDER[j]]
    return [it for it in items if getattr(it, "type", "") in ("material", "consumable")]


# ---- [P12] 题材怪物兜底池（LLM monsters 缺失/全非法时用；6 题材各 10 只覆盖危险度区间
# 每档危险度约 2 只，野外长线游玩不易重复；按知名小说体系命名，去 RPG 梗）----
GENRE_MONSTER_TEMPLATES: dict[str, list[dict]] = {
    "xianxia": [
        {"name": "妖狼", "role": "妖兽", "desc": "通体乌黑的妖狼，双目泛绿光。", "danger_min": 1, "danger_max": 3},
        {"name": "灵田鼠", "role": "妖兽", "desc": "偷食灵谷出栏的硕鼠，隐有妖气。", "danger_min": 1, "danger_max": 2},
        {"name": "血蝠", "role": "妖兽", "desc": "成群出没于洞府深处的血蝙蝠。", "danger_min": 2, "danger_max": 4},
        {"name": "藤妖", "role": "妖兽", "desc": "盘踞灵木林的食人老藤，善缠猎物。", "danger_min": 3, "danger_max": 5},
        {"name": "石魔", "role": "魔物", "desc": "灵石矿脉中孕育的石甲魔物。", "danger_min": 4, "danger_max": 6},
        {"name": "尸傀", "role": "魔物", "desc": "被邪修炼尸术驱使的行尸傀儡。", "danger_min": 4, "danger_max": 6},
        {"name": "邪修", "role": "强盗", "desc": "盘踞荒山夺人机缘的邪修。", "danger_min": 5, "danger_max": 8},
        {"name": "鹰首妖将", "role": "首领", "desc": "占山为王的鹰首人身妖将，爪携雷光。", "danger_min": 6, "danger_max": 8},
        {"name": "千年蛟蟒", "role": "首领", "desc": "镇守灵物古老蛟蟒，妖气冲天。", "danger_min": 8, "danger_max": 10},
        {"name": "魔渊厉鬼", "role": "魔物", "desc": "自魔渊渗出的厉鬼，神识攻击无形。", "danger_min": 8, "danger_max": 10},
        {"name": "幽魂", "role": "魔物", "desc": "荒坟野冢间飘荡的怨魂，逢生人便缠。", "danger_min": 1, "danger_max": 3},
        {"name": "毒灵蛛", "role": "妖兽", "desc": "吐丝结网的毒灵蛛，蛛网能蚀灵罡。", "danger_min": 3, "danger_max": 5},
        {"name": "铜甲尸", "role": "魔物", "desc": "邪修炼尸术炼就的铜皮僵尸，刀剑难伤。", "danger_min": 4, "danger_max": 6},
        {"name": "剑冢游魂", "role": "魔物", "desc": "剑冢深处徘徊的执念游魂，剑气森然。", "danger_min": 7, "danger_max": 9},
        {"name": "化形妖君", "role": "首领", "desc": "可化人形的积年大妖，统御一方妖族。", "danger_min": 9, "danger_max": 10},
        {"name": "妖蜂群", "role": "妖兽", "desc": "灵花谷筑巢的赤尾妖蜂，成群追螫来犯者。", "danger_min": 2, "danger_max": 4},
        {"name": "雾魈", "role": "魔物", "desc": "藏身瘴雾的鬼面魔物，虚实难辨，专袭落单行人。", "danger_min": 5, "danger_max": 7},
        {"name": "山魈巨妖", "role": "妖兽", "desc": "独居断崖的赤毛山魈，力能裂石，通晓几句人言。", "danger_min": 7, "danger_max": 9},
    ],
    "wuxia": [
        {"name": "野狼", "role": "野兽", "desc": "荒山野岭成群游荡的饿狼。", "danger_min": 1, "danger_max": 3},
        {"name": "疯牛", "role": "野兽", "desc": "受惊发狂的耕牛，横冲直撞。", "danger_min": 1, "danger_max": 2},
        {"name": "山贼", "role": "强盗", "desc": "剪径劫道的小股山贼。", "danger_min": 2, "danger_max": 4},
        {"name": "恶犬", "role": "野兽", "desc": "恶霸庄园里放出的凶猛猎犬。", "danger_min": 2, "danger_max": 4},
        {"name": "黑衣杀手", "role": "杀手", "desc": "受雇于人埋伏官道的杀手。", "danger_min": 4, "danger_max": 6},
        {"name": "毒蝎帮众", "role": "强盗", "desc": "使毒的帮派匪众，兵刃淬蝎毒。", "danger_min": 4, "danger_max": 6},
        {"name": "马匪头目", "role": "首领", "desc": "纵马劫掠的匪帮头目。", "danger_min": 6, "danger_max": 8},
        {"name": "漕帮打行", "role": "强盗", "desc": "把持水路的打行好手，群起围攻。", "danger_min": 6, "danger_max": 8},
        {"name": "独行魔头", "role": "首领", "desc": "身负血债隐居山林的魔头。", "danger_min": 8, "danger_max": 10},
        {"name": "宫廷死士", "role": "杀手", "desc": "奉密令出动的死士，武功狠辣。", "danger_min": 8, "danger_max": 10},
        {"name": "豺狗群", "role": "野兽", "desc": "荒山野岭里叼人骨头的成群豺狗。", "danger_min": 1, "danger_max": 3},
        {"name": "醉汉泼皮", "role": "强盗", "desc": "街头寻衅的泼皮，借酒壮胆拦路。", "danger_min": 2, "danger_max": 4},
        {"name": "采花大盗", "role": "杀手", "desc": "夜间出没的采花贼，轻功了得。", "danger_min": 3, "danger_max": 5},
        {"name": "五毒教众", "role": "强盗", "desc": "放蛊下毒的邪教匪众，手段阴狠。", "danger_min": 5, "danger_max": 7},
        {"name": "血刀老祖", "role": "首领", "desc": "隐世魔头，血刀一出必见生死。", "danger_min": 9, "danger_max": 10},
        {"name": "响马", "role": "强盗", "desc": "北地边口纵马劫货的响马队，来去如风。", "danger_min": 3, "danger_max": 5},
        {"name": "黑店贼伙", "role": "强盗", "desc": "荒道客栈里蒙汗药加闷棍的黑店伙计。", "danger_min": 2, "danger_max": 4},
        {"name": "绑票悍匪", "role": "强盗", "desc": "跨县流窜的绑票团伙，人多势众下手黑。", "danger_min": 5, "danger_max": 7},
    ],
    "modern": [
        {"name": "流浪恶犬", "role": "野兽", "desc": "废弃街区游荡的凶猛犬群。", "danger_min": 1, "danger_max": 3},
        {"name": "醉汉流氓", "role": "强盗", "desc": "深夜街头寻衅的醉汉。", "danger_min": 1, "danger_max": 2},
        {"name": "小混混", "role": "强盗", "desc": "巷子里拦路勒索的小混混。", "danger_min": 2, "danger_max": 4},
        {"name": "看门猛犬", "role": "野兽", "desc": "非法大院里拴着的护卫猛犬。", "danger_min": 2, "danger_max": 4},
        {"name": "黑打手", "role": "杀手", "desc": "收钱办事的黑衣打手。", "danger_min": 4, "danger_max": 6},
        {"name": "地下拳手", "role": "杀手", "desc": "黑拳场上打出来的亡命之徒。", "danger_min": 4, "danger_max": 6},
        {"name": "佣兵小队", "role": "首领", "desc": "火力凶悍的雇佣兵小队。", "danger_min": 6, "danger_max": 8},
        {"name": "复仇劫匪", "role": "强盗", "desc": "携枪劫掠金店的亡命劫匪。", "danger_min": 6, "danger_max": 8},
        {"name": "实验体", "role": "魔物", "desc": "非法实验室逃出的改造实验体。", "danger_min": 8, "danger_max": 10},
        {"name": "私人军队", "role": "首领", "desc": "财团豢养的私人武装小队。", "danger_min": 8, "danger_max": 10},
        {"name": "偷车贼", "role": "强盗", "desc": "街头撬锁偷车的惯犯，随身带撬棍。", "danger_min": 2, "danger_max": 4},
        {"name": "高利贷打手", "role": "强盗", "desc": "替债主收债的打手，出手狠辣。", "danger_min": 3, "danger_max": 5},
        {"name": "走私枪贩", "role": "杀手", "desc": "暗巷里交易黑枪的亡命之徒。", "danger_min": 5, "danger_max": 7},
        {"name": "失控改造人", "role": "魔物", "desc": "军改计划失败的失控强化人。", "danger_min": 7, "danger_max": 9},
        {"name": "顶级杀手", "role": "杀手", "desc": "受过严苛训练的幽灵杀手，一击致命。", "danger_min": 9, "danger_max": 10},
        {"name": "持刀劫匪", "role": "强盗", "desc": "深夜尾随取款人的持刀劫匪。", "danger_min": 2, "danger_max": 4},
        {"name": "讨债团伙", "role": "强盗", "desc": "上门泼漆堵锁的讨债团伙，成群逼人。", "danger_min": 3, "danger_max": 5},
        {"name": "邪教狂徒", "role": "杀手", "desc": "地下集会的邪教狂徒，行事不顾后果。", "danger_min": 7, "danger_max": 9},
    ],
    "scifi": [
        {"name": "清道夫机械", "role": "魔物", "desc": "回收废料的失控清扫机器人。", "danger_min": 1, "danger_max": 3},
        {"name": "故障家政机", "role": "魔物", "desc": "程序错乱的老式家政机器人，见人就缠。", "danger_min": 1, "danger_max": 2},
        {"name": "变异鼠群", "role": "野兽", "desc": "管道里成群的变异巨鼠。", "danger_min": 2, "danger_max": 4},
        {"name": "轨道窃贼", "role": "强盗", "desc": "在货运舱段摸包的星际窃贼。", "danger_min": 2, "danger_max": 4},
        {"name": "掠夺者", "role": "强盗", "desc": "袭击过路飞船的太空掠夺者。", "danger_min": 4, "danger_max": 6},
        {"name": "走私护卫", "role": "杀手", "desc": "押运违禁品的走私船护卫。", "danger_min": 4, "danger_max": 6},
        {"name": "作战无人机", "role": "杀手", "desc": "遗弃战场仍在执行指令的无人机。", "danger_min": 6, "danger_max": 8},
        {"name": "机甲佣兵", "role": "首领", "desc": "驾驶改装动力甲的星际佣兵。", "danger_min": 6, "danger_max": 8},
        {"name": "异星巨兽", "role": "首领", "desc": "货舱里苏醒的异星掠食巨兽。", "danger_min": 8, "danger_max": 10},
        {"name": "叛变智械", "role": "首领", "desc": "夺取哨站武装的叛变人工智能。", "danger_min": 8, "danger_max": 10},
        {"name": "垃圾场机械蟹", "role": "魔物", "desc": "废品回收场里横行的机械蟹，钳口锋利。", "danger_min": 1, "danger_max": 3},
        {"name": "黑客无人机", "role": "魔物", "desc": "被病毒劫持的侦察无人机，见人就袭。", "danger_min": 3, "danger_max": 5},
        {"name": "星盗斥候", "role": "强盗", "desc": "为星盗舰队打前哨的武装斥候。", "danger_min": 4, "danger_max": 6},
        {"name": "基因突变兽", "role": "野兽", "desc": "殖民星球上基因实验的突变掠食兽。", "danger_min": 7, "danger_max": 9},
        {"name": "战争泰坦", "role": "首领", "desc": "遗弃的战争泰坦机甲，尚存毁灭指令。", "danger_min": 9, "danger_max": 10},
        {"name": "异星孢体", "role": "魔物", "desc": "货舱夹层渗出的异星孢子团，触之蚀甲。", "danger_min": 2, "danger_max": 4},
        {"name": "遗弃哨戒炮", "role": "魔物", "desc": "殖民地废墟里仍在识别热源的自动哨戒炮。", "danger_min": 5, "danger_max": 7},
        {"name": "星盗头目", "role": "首领", "desc": "独眼星盗头目及其登舰小队，火力老辣。", "danger_min": 7, "danger_max": 9},
    ],
    "apocalypse": [
        {"name": "变异犬", "role": "野兽", "desc": "辐射区游荡的变异犬。", "danger_min": 1, "danger_max": 3},
        {"name": "腐鸦群", "role": "野兽", "desc": "以腐肉为食的变异鸦群，成片俯冲。", "danger_min": 1, "danger_max": 2},
        {"name": "拾荒匪", "role": "强盗", "desc": "为半瓶水拼命的拾荒匪帮。", "danger_min": 2, "danger_max": 4},
        {"name": "腐烂者", "role": "魔物", "desc": "尸变后游荡的腐烂行尸。", "danger_min": 4, "danger_max": 6},
        {"name": "酸液喷吐者", "role": "魔物", "desc": "喉部囊袋喷吐酸液的畸形尸变体。", "danger_min": 3, "danger_max": 5},
        {"name": "铁网偷猎者", "role": "强盗", "desc": "布设铁丝网陷阱的废土偷猎者。", "danger_min": 4, "danger_max": 6},
        {"name": "变异熊", "role": "野兽", "desc": "体型翻倍的辐射变异熊。", "danger_min": 6, "danger_max": 8},
        {"name": "教团狂信徒", "role": "杀手", "desc": "末日教团的狂信徒，自爆冲锋。", "danger_min": 6, "danger_max": 8},
        {"name": "巢穴母体", "role": "首领", "desc": "地下巢穴深处的变异母体。", "danger_min": 8, "danger_max": 10},
        {"name": "军团逃兵", "role": "首领", "desc": "携重武器流窜的避难所军团逃兵。", "danger_min": 8, "danger_max": 10},
        {"name": "变异鼠群", "role": "野兽", "desc": "下水道里成群的辐射变异鼠。", "danger_min": 1, "danger_max": 3},
        {"name": "辐射丧尸犬", "role": "野兽", "desc": "感染后犬齿畸长的丧尸犬。", "danger_min": 3, "danger_max": 5},
        {"name": "剥皮者", "role": "魔物", "desc": "披着人皮的畸形尸变体，伺机伏击。", "danger_min": 5, "danger_max": 7},
        {"name": "掠夺者摩托队", "role": "强盗", "desc": "骑着改装摩托劫掠幸存者的掠夺帮。", "danger_min": 6, "danger_max": 8},
        {"name": "巨型畸变体", "role": "首领", "desc": "吞噬了整座避难所的巨型畸变体。", "danger_min": 9, "danger_max": 10},
        {"name": "变异蜥群", "role": "野兽", "desc": "晒台上晒着晒着就围过来的变异蜥蜴群。", "danger_min": 2, "danger_max": 4},
        {"name": "骸骨融合体", "role": "魔物", "desc": "无数碎骨拼成的游荡怪物，行走时咔咔作响。", "danger_min": 5, "danger_max": 7},
        {"name": "废土屠夫", "role": "杀手", "desc": "以切割拾荒者为乐的废土屠夫，链锯不离手。", "danger_min": 6, "danger_max": 8},
    ],
    "western_fantasy": [
        {"name": "野狼", "role": "野兽", "desc": "林地间成群的灰狼。", "danger_min": 1, "danger_max": 3},
        {"name": "荒原野猪", "role": "野兽", "desc": "拱食草根的暴怒野猪，獠牙凶悍。", "danger_min": 1, "danger_max": 2},
        {"name": "哥布林", "role": "强盗", "desc": "手持粗劣武器的哥布林劫掠者。", "danger_min": 2, "danger_max": 4},
        {"name": "强盗团斥候", "role": "强盗", "desc": "在商道游弋的强盗团斥候。", "danger_min": 2, "danger_max": 4},
        {"name": "骷髅兵", "role": "魔物", "desc": "古战场苏醒的骸骨士兵。", "danger_min": 4, "danger_max": 6},
        {"name": "沼泽巨蛙", "role": "野兽", "desc": "吞整只羊的沼泽巨蛙，舌击如鞭。", "danger_min": 3, "danger_max": 5},
        {"name": "食人魔", "role": "魔物", "desc": "占据桥洞的暴怒食人魔。", "danger_min": 6, "danger_max": 8},
        {"name": "森林树妖", "role": "魔物", "desc": "伪装成枯树的树妖，根须缠人。", "danger_min": 6, "danger_max": 8},
        {"name": "幼龙", "role": "首领", "desc": "守着巢穴宝藏的年轻火龙。", "danger_min": 8, "danger_max": 10},
        {"name": "堕落骑士", "role": "首领", "desc": "背弃誓言的堕落骑士，甲胄渗黑雾。", "danger_min": 8, "danger_max": 10},
        {"name": "巨型蜘蛛", "role": "野兽", "desc": "林间结网捕猎的巨型狼蛛。", "danger_min": 1, "danger_max": 3},
        {"name": "兽人劫掠者", "role": "强盗", "desc": "南下劫掠村镇的兽人散兵。", "danger_min": 3, "danger_max": 5},
        {"name": "石像鬼", "role": "魔物", "desc": "教堂檐上苏醒的石像鬼，俯冲扑食。", "danger_min": 5, "danger_max": 7},
        {"name": "死灵法师", "role": "魔物", "desc": "操控亡者的死灵法师，黑魔法莫测。", "danger_min": 7, "danger_max": 9},
        {"name": "深渊恶魔", "role": "首领", "desc": "自裂缝爬出的深渊恶魔，魔焰滔天。", "danger_min": 9, "danger_max": 10},
        {"name": "鱼人掠劫者", "role": "强盗", "desc": "趁夜爬上河滩劫掠渔村的鱼人。", "danger_min": 3, "danger_max": 5},
        {"name": "影爪狼", "role": "魔物", "desc": "皮毛融入阴影的魔狼，爪痕带蚀骨寒意。", "danger_min": 5, "danger_max": 7},
        {"name": "荒野狮鹫", "role": "野兽", "desc": "盘踞高崖的成年狮鹫，俯冲时能掀翻马车。", "danger_min": 7, "danger_max": 9},
    ],
}

# 精英怪题材前缀（多值 rng.pick，长线游玩精英名不重复）
_GENRE_ELITE_PREFIX = {
    "xianxia": ["妖化的", "渡劫失败的", "吞了妖丹的", "开了灵智的", "化形的", "血祭的",
                "金丹碎裂的", "夺舍重生的"],
    "wuxia": ["狂化的", "服了暴血丹的", "走火入魔的", "嗜血的", "练了邪功的", "嗑了禁药的",
              "淬毒的", "断魂的"],
    "modern": ["改造的", "磕了违禁药的", "穿着外骨骼的", "杀红眼的", "戴着义体的", "注射强化剂的",
               "绑着炸药的", "嗑药过量的"],
    "scifi": ["过载的", "超频的", "装甲强化型", "失控协议的", "量子纠缠的", "自我进化的",
              "搭载蜂群的", "军用原型机"],
    "apocalypse": ["变异头目", "辐射王", "三阶变异", "吞噬同类的", "异化首领", "母体眷顾的",
                   "锈疫感染的", "核尘淬炼的"],
    "western_fantasy": ["凶悍的", "狂暴的", "深渊眷顾的", "疤面首领", "血月下的", "堕落的",
                        "瘟疫染身的", "龙血浸染的"],
}


def strip_elite_prefix(name: str) -> Optional[str]:
    """剥掉名字头部的已知精英前缀，返回基础名；无前缀/剥后为空返回 None。

    供战斗图缓存回退：精英名 = 前缀直接拼接基础名（roll_wilderness_monster），内容键含全名故查不到
    基础怪预生成图；剥前缀后按基础名重查即复用（精英 = 同生物强化版）。全题材池都试——
    前缀是各题材 distinctive 词，跨题材误剥可忽略，自定义题材无专属池也兜得住。"""
    n = (name or "").strip()
    for prefixes in _GENRE_ELITE_PREFIX.values():
        for p in prefixes:
            if p and n.startswith(p) and len(n) > len(p):
                return n[len(p):]
    return None


def _pick_template(world: Any, loc: Any, rng: SeededRng) -> Optional[dict]:
    """按地点危险度从怪物池选模板（LLM 池优先，兜底题材表）。确定性。"""
    danger = int(getattr(loc, "danger", 1) or 1)
    pool = [m for m in (getattr(world, "monster_pool", None) or [])
            if isinstance(m, dict)
            and int(m.get("danger_min", 1) or 1) <= danger <= int(m.get("danger_max", 10) or 10)]
    if not pool:
        overlay = getattr(world, "config_overlay", None) or {}
        tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
        pool = [dict(m) for m in (GENRE_MONSTER_TEMPLATES.get(tid)
                                  or GENRE_MONSTER_TEMPLATES["western_fantasy"])]
    if not pool:
        return None
    return dict(rng.pick(pool))


def encounter_chance(loc: Any, base: float, is_gather: bool) -> float:
    """遇怪概率 = 基础 + 危险度加成（+采集加成），钳 5%-60%。"""
    base = max(0.0, min(1.0, float(base or 0.0)))
    danger = int(getattr(loc, "danger", 1) or 1)
    ch = base + danger * 0.02 + (0.10 if is_gather else 0.0)
    return max(0.05, min(0.6, ch))


# [P42f 用户定稿 2026-08-23] 怪物技能职能化：杂兵单职能（攻击/回复/增益各 1 技），
# 精英 2 技（攻击+辅助），首领/Boss 3 技（强攻+回复+增益）。职能按怪物 id 派生
# SeededRng 确定（同怪同职能可回放）；敌方 heal/buff 走战斗引擎既有管线
# （defensive AI 低血自愈 / buff 经 inflicts 施加）。
_MOB_SKILL_NAMES: dict[str, dict[str, list[str]]] = {
    "xianxia": {"attack": ["妖爪撕袭", "煞气冲击"], "heal": ["吞吐月华", "妖息回元"],
                "buff": ["妖嚎震慑", "煞气护体"]},
    "wuxia": {"attack": ["狂暴扑击", "夺命撕咬"], "heal": ["龟息回气", "舐伤自疗"],
              "buff": ["凶性大发", "蓄势低吼"]},
    "modern": {"attack": ["疯狂扑袭", "重撞"], "heal": ["肾上腺素", "强韧体质"],
               "buff": ["暴走状态", "嘶吼威吓"]},
    "scifi": {"attack": ["穿刺打击", "过载冲撞"], "heal": ["纳米自修", "能量再充"],
              "buff": ["超频模式", "威慑脉冲"]},
    "apocalypse": {"attack": ["撕扯啃咬", "撞骨冲撞"], "heal": ["变异再生", "噬肉回血"],
                   "buff": ["狂化嚎叫", "硬质表皮"]},
    "western_fantasy": {"attack": ["凶猛撕咬", "蛮力冲撞"], "heal": ["舔舐伤口", "野性恢复"],
                        "buff": ["战意嚎叫", "凶悍威吓"]},
}


def mob_skill_name(tid: str, kind: str, seed: int) -> str:
    pool = (_MOB_SKILL_NAMES.get(tid) or _MOB_SKILL_NAMES["western_fantasy"]).get(kind)         or _MOB_SKILL_NAMES["western_fantasy"][kind]
    return pool[abs(int(seed)) % len(pool)]


def monster_role_skills(tid: str, world_id: str, monster_id: str, role: str,
                        lvl: int, elite: bool = False,
                        skill_pool: Optional[list] = None) -> tuple:
    """按职能构建怪物技能。返回 (ai_pattern, skills)。

    - 杂兵：单职能（attack 50% 倾向 / heal / buff 各 1 技）——同 id 确定性；
    - 精英：攻击 + 辅助（heal 或 buff）2 技；
    - 首领/Boss（role 含 首领/boss）：强攻 + 回复 + 增益 3 技，aggressive。
    [怪物技能池 2026-08-25] skill_pool 非空时：name/element/inflicts/desc 从池确定性抽取
    （LLM 出语义），power/cost_mp/cooldown/stat_scaling/target 仍引擎算（守数值范式铁律）；
    池缺失或该职能无条目时回退 _MOB_SKILL_NAMES。
    """
    from src.utils.rng import SeededRng as _SR
    sr = _SR.seed_from(str(world_id), 0, f"mob_role_{monster_id}")
    lvl = max(1, int(lvl or 1))
    boss = any(k in (role or "") for k in ("首领", "boss", "Boss"))

    # 池按职能分桶（attack/heal/buff）
    pool_by_kind: dict[str, list] = {}
    for sk in (skill_pool or []):
        if isinstance(sk, dict) and str(sk.get("type", "") or "") in ("attack", "heal", "buff"):
            pool_by_kind.setdefault(str(sk.get("type")), []).append(sk)

    def _pick(kind: str):
        cands = pool_by_kind.get(kind)
        if not cands:
            return None
        return cands[sr.roll(0, len(cands) - 1)]

    def _clean_inflicts(raw) -> list:
        out = []
        for x in (raw or []):
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

    def _sk(kind: str, power: int, cd: int, cost: int = 0, cond: str = ""):
        tpl = _pick(kind)
        if tpl is not None:
            name = str(tpl.get("name") or "").strip() or mob_skill_name(tid, kind, sr.roll(0, 99))
            element = str(tpl.get("element") or "").strip()
            inflicts = _clean_inflicts(tpl.get("inflicts"))
            desc = str(tpl.get("desc") or "").strip()
        else:
            name = mob_skill_name(tid, kind, sr.roll(0, 99))
            element = ""
            inflicts = []
            desc = ""
        sk = {"id": f"{monster_id}_{kind}", "name": name,
              "type": kind if kind != "attack" else "attack",
              "power": power, "cost_mp": cost, "cooldown": cd,
              "damage_type": "physical", "stat_scaling": "str", "target": "enemy"}
        if kind == "heal":
            sk["target"] = "self"
            sk["damage_type"] = "magical"
        if kind == "buff":
            sk["target"] = "self"
            sk["damage_type"] = "magical"
            sk["inflicts"] = inflicts or [{"condition": (cond or "enraged"), "chance": 1.0,
                                           "duration": 2}]
            sk["power"] = 0
        elif inflicts:
            sk["inflicts"] = inflicts
        if element:
            sk["element"] = element
        if desc:
            sk["desc"] = desc
        return sk

    if boss:
        return "aggressive", [
            _sk("attack", 12 + lvl, 2),
            _sk("heal", 6 + lvl * 2, 3, cost=6),
            _sk("buff", 0, 3, cost=4, cond="enraged"),
        ]
    support = sr.pick(("heal", "buff"))
    if elite:
        ai = "aggressive" if support == "buff" else "balanced"
        return ai, [
            _sk("attack", 10 + lvl, 1),
            _sk("heal", 5 + lvl * 2, 3, cost=5) if support == "heal"
            else _sk("buff", 0, 3, cost=4, cond="protected"),
        ]
    # 杂兵：单职能（attack 倾向 1/2）
    kind = sr.pick(("attack", "attack", "heal", "buff"))
    if kind == "attack":
        return "balanced", [_sk("attack", 8 + lvl, 1)]
    if kind == "heal":
        return "defensive", [_sk("heal", 5 + lvl * 2, 2, cost=4)]
    return "balanced", [_sk("buff", 0, 3, cost=3, cond="protected")]


def roll_wilderness_monster(world: Any, loc: Any, player_level: int,
                            monsters_on: bool, base_chance: float, elite_chance: float,
                            rng: SeededRng, is_gather: bool = False,
                            season_mult: float = 1.0, force: bool = False) -> Optional[NPC]:
    """判定并构建野外怪（未命中/门控关闭返回 None）。临时 NPC 不入 world.npcs。

    [P43 重构 2026-09-12] 等级 = [玩家等级, 玩家等级 + danger + 3] 段内随机；精英 +15%（至少 +2）。
    [P45 2026-09-12] force=True 跳过 encounter_chance 门：NPC 打猎（P36a 计划 / A2 每日
    被动遭遇）是「已决定开打」，只借本函数拿「同源生成的怪 + 真实 loot_table + 掉率表」；
    遇怪概率由调用方自己的入口把关（A2 入口另走本函数默认路径复用 encounter_chance）。
    玩家侧调用不传（默认 False），行为完全不变。
    """
    if not monsters_on or not loc or getattr(loc, "kind", "wilderness") != "wilderness":
        return None
    # [季节玩法化 2026-09-06] 季节遇怪系数（冬 1.3 野兽觅食；调用方从 season_fx 传入）
    if not force and not rng.chance(min(0.95, encounter_chance(loc, base_chance, is_gather) * season_mult)):
        return None
    tmpl = _pick_template(world, loc, rng)
    if tmpl is None:
        return None
    try:
        plv = max(1, int(player_level or 1))
    except (TypeError, ValueError):
        plv = 1
    base_lvl = band_level(int(getattr(loc, "danger", 1) or 1), plv, rng)
    # [修 2026-10-02 真机 P1·开局难度倒挂] 低等级主体（<5 级）上界钳 主体+3：
    # P43 区间上界（主体+danger+3）在开局爬坡段产生 5-6 级差碾压——Lv1 玩家在
    # danger 2 野外撞 6 级主怪+5 级随从（净水残响档实测 4 回合被击倒），与旧
    # 「野狼案」同源（G04-5 基线在案的 level>=5 门槛陡崖）。钳制只作用于 <5 级
    # 主体（玩家开局段/NPC 低起步段，随从是主怪 7 成克隆连带收敛）；5 级起地带
    # 强弱层次（danger 拉开上界）保持 P43 原口径不变。世界 Boss（rng=None 取段顶）
    # 与秘境（soften 已有独立口径）不经本分支。
    if plv < 5:
        base_lvl = min(base_lvl, plv + 3)
    elite = rng.chance(max(0.0, min(1.0, float(elite_chance or 0.0))))
    lvl = min(_LEVEL_CAP, base_lvl + (elite_level_bonus(base_lvl) if elite else 0))
    overlay = getattr(world, "config_overlay", None) or {}
    tid = overlay.get("attribute_template_id", "western_fantasy") or "western_fantasy"
    name = tmpl.get("name", "魔物")
    if elite:
        prefixes = _GENRE_ELITE_PREFIX.get(tid) or _GENRE_ELITE_PREFIX["western_fantasy"]
        name = f"{rng.pick(prefixes) or '凶悍的'}{name}"
    npc = NPC(name=name, role=tmpl.get("role") or "魔物",
              desc=tmpl.get("desc") or "", hostile=True, level=lvl)
    npc.alive = True
    npc.is_key_npc = False
    # [数值抖动] 同 level 野怪不再逐字节同构：按 npc.id 派生独立 rng 抖动（不污染主 rng 序列，
    # 保后续掉落 roll 确定性）。基线 4+lvl，抖动 [-1,+2] 偏正保持随 level 单调。
    from src.utils.rng import SeededRng as _WR
    wr = _WR.seed_from(getattr(world, "id", ""), 0, f"wild_stats_{npc.id}")
    jit = lambda base: max(1, base + wr.roll(-1, 2))
    npc.stat_str = jit(4 + lvl)
    npc.stat_vit = jit(4 + lvl)
    npc.stat_dex = jit(3 + lvl // 2)
    npc.stat_int = jit(3)
    npc.stat_luk = jit(3)
    npc.hp_max = max_hp_for(npc)  # 与 ce.max_hp_for 权威公式同口径（vit*10+lvl*5），消除内联复制
    npc.hp = npc.hp_max
    npc.mp_max = npc.stat_int * 5 + lvl * 2
    npc.mp = npc.mp_max
    # [P42f] 技能/AI 职能化：杂兵单职能、精英 2 技、首领 3 技（同 id 确定性）
    npc.ai_pattern, npc.skills = monster_role_skills(
        tid, str(getattr(world, "id", "")), npc.id, npc.role or "", lvl, elite=elite,
        skill_pool=getattr(world, "monster_skill_pool", None))
    # 掉落：怪物池 loot（精英掉率提升）+ 所在地点资源层级材料（怪物守着资源点）
    # [P9] 元素构成：LLM 怪物池模板优先（_clean_elements 清洗，与存档 round-trip 同口径）；
    # 未给则从技能推断（兜底技能全 physical 推不出 -> 无元素）
    npc.elements = _clean_elements(tmpl.get("elements")) or infer_elements_from_skills(npc.skills)
    rate = 0.75 if elite else 0.5
    loot_ids: list[str] = []
    for lid in (tmpl.get("loot") or []):
        if isinstance(lid, str) and lid and lid not in loot_ids:
            loot_ids.append(lid)
    # 追加地点层级资源（LLM 池 loot 未覆盖该材料时）
    loc_tier = danger_to_tier(getattr(loc, "danger", 1))
    tier_pool = nearest_rarity_pool(getattr(world, "items", []) or [], loc_tier)
    if tier_pool:
        mat = tier_pool[rng.roll(0, len(tier_pool) - 1)]
        if mat.id not in loot_ids:
            loot_ids.append(mat.id)
    # [P13] 精英怪掉落表追加一本技能书（进表按精英率 0.75 roll，非保底——[用户指示 2026-08-21] 技能书一律按掉落公式）
    book_id = ""
    if elite:
        books = [it for it in (getattr(world, "items", []) or [])
                 if isinstance(getattr(it, "teach_skill", None), dict) and it.teach_skill
                 and getattr(it, "id", "") not in loot_ids]
        if books:
            book = books[rng.roll(0, len(books) - 1)]
            loot_ids.append(book.id)
            book_id = book.id
    # [C7 修复 2026-08-25] 技能书固定第 5 槽保位：模板 loot+材料最多占前 4 槽，
    # 书排在其后不被 [:5] 切掉（此前模板 4 件+材料 1 件占满时书掉率变 0）
    if book_id:
        loot_ids = [i for i in loot_ids if i != book_id][:4] + [book_id]
    npc.loot_table = [{"id": i, "rate": rate} for i in loot_ids[:5]]
    return npc
