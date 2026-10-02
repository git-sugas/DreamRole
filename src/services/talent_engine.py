"""[P9] 天赋引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b/§23）：LLM 出天赋语义（name/desc/题材/element/effect 种子值/cost），
纯 Python 跑动态聚合（get_talent_mult 把玩家/NPC 的多个天赋 effects 叠乘/叠加）。
天赋分等级(rarity)和价格(cost)：强天赋 cost 高效果强，天赋点是购买预算（默认 10，QQ 码 99）。

== 天赋 effects 钩子（_TALENT_EFFECT_KEYS，world.py 定义）==
- skill_dmg_mult: 技能伤害乘数（天赋 element 非空时只对同系技能生效，雷天灵根只加成雷系功法）
- atk_mult: 普攻/总攻击乘数；gather_mult/craft_mult/check_mult/loot_mult: 各系统乘数
- luck_bonus: 幸运加值(int)；stat_bonus: {str/dex/int/vit/luk:加值}；mp_bonus: 法力上限加值(int)

== element 匹配规则 ==
- 天赋 element="" → 通用，skill_dmg_mult 对所有技能生效
- 天赋 element="thunder" → skill_dmg_mult 只对 skill.element=="thunder" 的技能生效
- atk_mult/gather_mult 等非技能乘数不受 element 限制
"""
from __future__ import annotations

from typing import Any, Optional


# [P9] 元素 key -> 中文短名（describe_talent/元素克制日志/档案页展示共用；单一来源防双份漂移）。
# combat_engine 经 te 引用并 re-export（combat -> talent 单向依赖，无循环）。
ELEMENT_ZH = {"fire": "火", "thunder": "雷", "ice": "冰", "wind": "风", "wood": "木",
              "metal": "金", "earth": "土", "light": "光", "dark": "暗", "physical": "物理"}


# ---- 6 题材兜底天赋池（LLM 失败/关闭时用；effects 强度按 rarity 缩放）----
# [数据量铁律] 每题材 14 个天赋，5 档 rarity 各 >=1，覆盖各元素 + 各 effects 钩子。
# LLM 生成时据题材偏好出更多变体，引擎只在此池兜底。cost 由 rarity 推算（_talent_cost_for_rarity）。
_GENRE_TALENT_TEMPLATES = {
    "xianxia": [
        {"name": "雷天灵根", "desc": "天生与雷霆共鸣，修雷系功法事半功倍", "rarity": "legendary", "element": "thunder", "effects": {"skill_dmg_mult": 1.4, "mp_bonus": 25}},
        {"name": "混沌道体", "desc": "返本归源的先天道体，万法不侵", "rarity": "legendary", "effects": {"stat_bonus": {"int": 3, "vit": 3}, "mp_bonus": 40}},
        {"name": "金丹残识", "desc": "识海中蛰伏着一缕金丹期残识，指点修行", "rarity": "epic", "effects": {"check_mult": 1.3, "mp_bonus": 30}},
        {"name": "火灵之体", "desc": "丹田蕴火，火系法术威力大增", "rarity": "rare", "element": "fire", "effects": {"skill_dmg_mult": 1.3, "atk_mult": 1.1}},
        {"name": "剑心通明", "desc": "剑意纯粹，出剑凌厉", "rarity": "rare", "effects": {"atk_mult": 1.15, "skill_dmg_mult": 1.1}},
        {"name": "冰肌玉骨", "desc": "天生寒气护体，冰系功法亲和", "rarity": "rare", "element": "ice", "effects": {"skill_dmg_mult": 1.3}},
        {"name": "丹道宗师", "desc": "炼丹造诣深厚，丹药成色更佳", "rarity": "uncommon", "effects": {"craft_mult": 1.3, "gather_mult": 1.2}},
        {"name": "天生道骨", "desc": "灵根资质上佳，灵力深厚", "rarity": "uncommon", "effects": {"mp_bonus": 30, "stat_bonus": {"int": 2}}},
        {"name": "御灵手", "desc": "驯兽御灵有术，妖兽亲近", "rarity": "uncommon", "effects": {"check_mult": 1.2, "gather_mult": 1.25}},
        {"name": "福运深厚", "desc": "天生命好，机缘不断", "rarity": "common", "effects": {"luck_bonus": 5, "loot_mult": 1.15}},
        {"name": "敏锐直觉", "desc": "对危险与机缘感知敏锐", "rarity": "common", "effects": {"check_mult": 1.15}},
        {"name": "蛮力过人", "desc": "力大无穷", "rarity": "common", "effects": {"stat_bonus": {"str": 3}}},
        {"name": "识海开阔", "desc": "神识修长，灵力回得快", "rarity": "common", "effects": {"mp_bonus": 20}},
        {"name": "市井耳目", "desc": "坊市消息灵通，总能打探机缘", "rarity": "common", "effects": {"check_mult": 1.15, "luck_bonus": 3}},
        {"name": "五灵同修", "desc": "五行灵力皆可炼化，博采众长", "rarity": "epic", "effects": {"mp_bonus": 30, "skill_dmg_mult": 1.2}},
        {"name": "丹毒不侵", "desc": "常年试丹炼就的百毒不侵之体", "rarity": "rare", "effects": {"stat_bonus": {"vit": 3}, "craft_mult": 1.15}},
        {"name": "御兽通灵", "desc": "能与妖兽心意相通", "rarity": "uncommon", "element": "wood", "effects": {"check_mult": 1.2, "gather_mult": 1.2}},
        {"name": "市井卦师", "desc": "粗通卦象，趋吉避凶", "rarity": "common", "effects": {"luck_bonus": 4, "check_mult": 1.1}},
        {"name": "无垢仙体", "desc": "万中无一的先天仙体，万法难侵", "rarity": "mythic", "effects": {"stat_bonus": {"int": 4, "vit": 3}, "skill_dmg_mult": 1.5}},
        # [装备新属性 2026-09-01] 概率型战斗天赋（combo/counter/lifesteal 乘数）
        {"name": "剑心连环", "desc": "剑势连绵，一击可追两击", "rarity": "rare", "effects": {"combo_rate": 1.3}},
        {"name": "斗转星移", "desc": "以彼之道还施彼身", "rarity": "epic", "effects": {"counter_rate": 1.4}},
        {"name": "噬灵诀", "desc": "伤敌即养己，真元随杀伐增长", "rarity": "rare", "effects": {"lifesteal": 1.35}},
    ],
    "wuxia": [
        {"name": "纯阳之体", "desc": "至阳内力，功力深厚", "rarity": "legendary", "effects": {"atk_mult": 1.3, "mp_bonus": 20}},
        {"name": "先天剑骨", "desc": "天生剑骨，学剑一日千里", "rarity": "legendary", "element": "physical", "effects": {"skill_dmg_mult": 1.4, "atk_mult": 1.1}},
        {"name": "百年功力", "desc": "机缘之下得前辈百年功力灌注", "rarity": "epic", "effects": {"atk_mult": 1.25, "mp_bonus": 30}},
        {"name": "寒冰真气", "desc": "至阴寒气，伤敌于无形", "rarity": "rare", "element": "ice", "effects": {"skill_dmg_mult": 1.3}},
        {"name": "飞檐走壁", "desc": "轻功卓越，身法如风", "rarity": "rare", "effects": {"stat_bonus": {"dex": 4}}},
        {"name": "金针渡穴", "desc": "一手金针渡穴的绝技，救死扶伤", "rarity": "rare", "effects": {"craft_mult": 1.3, "check_mult": 1.15}},
        {"name": "铁布衫", "desc": "横练硬功，刀枪难入", "rarity": "uncommon", "effects": {"stat_bonus": {"vit": 3}}},
        {"name": "医道世家", "desc": "世代行医，药理精通", "rarity": "uncommon", "effects": {"craft_mult": 1.25, "gather_mult": 1.2}},
        {"name": "耳聪目明", "desc": "听力目力远超常人，暗器无形", "rarity": "uncommon", "element": "physical", "effects": {"skill_dmg_mult": 1.2, "check_mult": 1.15}},
        {"name": "侠义心肠", "desc": "行侠仗义，人缘极佳", "rarity": "common", "effects": {"check_mult": 1.15, "luck_bonus": 3}},
        {"name": "神射手", "desc": "百步穿杨", "rarity": "common", "element": "wind", "effects": {"skill_dmg_mult": 1.2}},
        {"name": "莽汉", "desc": "力大无穷", "rarity": "common", "effects": {"stat_bonus": {"str": 3}}},
        {"name": "脚力健", "desc": "日行百里，赶路不成问题", "rarity": "common", "effects": {"stat_bonus": {"dex": 2, "vit": 1}}},
        {"name": "江湖人脉", "desc": "三教九流都有熟人", "rarity": "common", "effects": {"loot_mult": 1.15, "luck_bonus": 3}},
        {"name": "玄冰真气", "desc": "至阴寒气，伤敌于无形", "rarity": "epic", "element": "ice", "effects": {"skill_dmg_mult": 1.3, "atk_mult": 1.1}},
        {"name": "双全手", "desc": "左右开弓，快人一步", "rarity": "rare", "element": "physical", "effects": {"atk_mult": 1.15, "skill_dmg_mult": 1.1}},
        {"name": "酒剑仙", "desc": "醉意越浓剑意越盛", "rarity": "uncommon", "effects": {"skill_dmg_mult": 1.15, "check_mult": 1.15}},
        {"name": "铁口直断", "desc": "看相算命，行走江湖", "rarity": "common", "effects": {"luck_bonus": 4, "check_mult": 1.1}},
        {"name": "武圣之体", "desc": "武学巅峰的先天之体，内外兼修", "rarity": "mythic", "effects": {"atk_mult": 1.4, "skill_dmg_mult": 1.4}},
        # [装备新属性 2026-09-01] 概率型战斗天赋
        {"name": "连环快剑", "desc": "剑快到影子追不上，一招两式", "rarity": "rare", "effects": {"combo_rate": 1.3}},
        {"name": "借力打力", "desc": "四两拨千斤，敌人攻势反噬其身", "rarity": "epic", "effects": {"counter_rate": 1.4}},
        {"name": "龟息纳气", "desc": "以气养伤，越战气越长", "rarity": "rare", "effects": {"lifesteal": 1.35}},
    ],
    "modern": [
        {"name": "机械改造体", "desc": "全身义体改造，远超常人", "rarity": "legendary", "effects": {"atk_mult": 1.25, "stat_bonus": {"vit": 3}}},
        {"name": "基因优化者", "desc": "胚胎期基因编辑的天选之人", "rarity": "legendary", "effects": {"stat_bonus": {"str": 2, "dex": 2, "int": 2}}},
        {"name": "特级反应神经", "desc": "国家级运动员的反应速度", "rarity": "epic", "effects": {"atk_mult": 1.2, "check_mult": 1.25}},
        {"name": "黑客天赋", "desc": "电子世界的王者", "rarity": "rare", "effects": {"craft_mult": 1.3, "check_mult": 1.2}},
        {"name": "神枪手", "desc": "枪法如神", "rarity": "rare", "element": "physical", "effects": {"skill_dmg_mult": 1.3, "atk_mult": 1.1}},
        {"name": "过目不忘", "desc": "记忆力惊人，任何资料看一遍就记住", "rarity": "rare", "effects": {"check_mult": 1.25, "stat_bonus": {"int": 2}}},
        {"name": "战术大师", "desc": "临场判断精准", "rarity": "uncommon", "effects": {"atk_mult": 1.15, "check_mult": 1.15}},
        {"name": "急救专家", "desc": "现场救护娴熟", "rarity": "uncommon", "effects": {"craft_mult": 1.25}},
        {"name": "健身狂人", "desc": "常年泡健身房的一身腱子肉", "rarity": "uncommon", "effects": {"stat_bonus": {"str": 3, "vit": 1}}},
        {"name": "赌徒直觉", "desc": "逢赌必赢的第六感", "rarity": "common", "effects": {"luck_bonus": 6, "loot_mult": 1.2}},
        {"name": "街头智慧", "desc": "市井生存的机灵劲", "rarity": "common", "effects": {"check_mult": 1.2, "gather_mult": 1.15}},
        {"name": "体格健壮", "desc": "常年锻炼", "rarity": "common", "effects": {"stat_bonus": {"vit": 3}}},
        {"name": "夜猫子", "desc": "越到深夜越精神，行动隐秘", "rarity": "common", "effects": {"check_mult": 1.15, "gather_mult": 1.1}},
        {"name": "老司机", "desc": "任何载具都能开得飞起", "rarity": "common", "effects": {"check_mult": 1.15, "stat_bonus": {"dex": 2}}},
        {"name": "危机直觉", "desc": "生死边缘练出的直觉", "rarity": "epic", "effects": {"check_mult": 1.3, "atk_mult": 1.15}},
        {"name": "伪装大师", "desc": "易容乔装，来去无踪", "rarity": "rare", "effects": {"check_mult": 1.2, "gather_mult": 1.15}},
        {"name": "极限体能", "desc": "超越常人的体能储备", "rarity": "uncommon", "effects": {"stat_bonus": {"str": 2, "vit": 2}}},
        {"name": "网购达人", "desc": "总能淘到便宜好货", "rarity": "common", "effects": {"loot_mult": 1.2, "gather_mult": 1.1}},
        {"name": "超新星", "desc": "极限进化的人类样本，全面超越", "rarity": "mythic", "effects": {"atk_mult": 1.35, "stat_bonus": {"str": 2, "dex": 2, "int": 2}}},
        # [装备新属性 2026-09-01] 概率型战斗天赋
        {"name": "速射本能", "desc": "扣一次扳机的时间打出两发", "rarity": "rare", "effects": {"combo_rate": 1.3}},
        {"name": "近身反制", "desc": "格斗教官的条件反射，贴身即反打", "rarity": "epic", "effects": {"counter_rate": 1.4}},
        {"name": "肾上腺依赖", "desc": "越是受伤越是亢奋，伤口愈战愈合", "rarity": "rare", "effects": {"lifesteal": 1.35}},
    ],
    "scifi": [
        {"name": "纳米共生体", "desc": "纳米虫与神经共生", "rarity": "legendary", "effects": {"stat_bonus": {"vit": 3, "int": 2}, "mp_bonus": 25}},
        {"name": "实验体零号", "desc": "禁忌计划的完美适配者，机能全面超越", "rarity": "legendary", "effects": {"atk_mult": 1.3, "skill_dmg_mult": 1.3}},
        {"name": "量子直觉", "desc": "对概率分支的天然直觉", "rarity": "epic", "effects": {"check_mult": 1.3, "luck_bonus": 6}},
        {"name": "能量亲和", "desc": "与能量武器共鸣", "rarity": "rare", "element": "thunder", "effects": {"skill_dmg_mult": 1.3}},
        {"name": "神经接驳", "desc": "反应速度超人", "rarity": "rare", "effects": {"atk_mult": 1.2, "check_mult": 1.15}},
        {"name": "星图记忆", "desc": "过星图不忘，导航结算样样精通", "rarity": "rare", "effects": {"check_mult": 1.25, "gather_mult": 1.2}},
        {"name": "植入工程师", "desc": "义体调校精湛", "rarity": "uncommon", "effects": {"craft_mult": 1.3}},
        {"name": "数据感知", "desc": "对数据流敏感", "rarity": "uncommon", "effects": {"check_mult": 1.2, "gather_mult": 1.2}},
        {"name": "低重力适应", "desc": "在失重环境如履平地", "rarity": "uncommon", "effects": {"stat_bonus": {"dex": 3}, "check_mult": 1.1}},
        {"name": "幸运基因", "desc": "基因彩票中奖", "rarity": "common", "effects": {"luck_bonus": 5, "loot_mult": 1.15}},
        {"name": "战斗算法", "desc": "内置战斗协处理器", "rarity": "common", "element": "physical", "effects": {"skill_dmg_mult": 1.2}},
        {"name": "强化肌纤维", "desc": "人工强化肌肉", "rarity": "common", "effects": {"stat_bonus": {"str": 3}}},
        {"name": "冗余器官", "desc": "备用器官让生存率大增", "rarity": "common", "effects": {"stat_bonus": {"vit": 3}}},
        {"name": "废料淘宝眼", "desc": "一眼看出废料堆里什么值钱", "rarity": "common", "effects": {"gather_mult": 1.2, "loot_mult": 1.1}},
        {"name": "超频神经", "desc": "神经超频，思考加速", "rarity": "epic", "effects": {"check_mult": 1.3, "mp_bonus": 20}},
        {"name": "义体共鸣", "desc": "与义体深度共鸣", "rarity": "rare", "element": "thunder", "effects": {"skill_dmg_mult": 1.25, "atk_mult": 1.1}},
        {"name": "纳米医生", "desc": "纳米修复单元常驻体内", "rarity": "uncommon", "effects": {"craft_mult": 1.25, "stat_bonus": {"vit": 2}}},
        {"name": "回收狂人", "desc": "变废为宝的达人", "rarity": "common", "effects": {"gather_mult": 1.25, "loot_mult": 1.1}},
        {"name": "进化者", "desc": "星际基因工程的终极产物", "rarity": "mythic", "effects": {"stat_bonus": {"int": 3, "dex": 3}, "skill_dmg_mult": 1.5}},
        # [装备新属性 2026-09-01] 概率型战斗天赋
        {"name": "超频连射", "desc": "火控系统超频，一次锁定两次开火", "rarity": "rare", "effects": {"combo_rate": 1.3}},
        {"name": "镜面装甲", "desc": "反射涂层把一部分伤害原路送回", "rarity": "epic", "effects": {"counter_rate": 1.4}},
        {"name": "纳米修复场", "desc": "战斗中持续用击杀数据修补机体", "rarity": "rare", "effects": {"lifesteal": 1.35}},
    ],
    "apocalypse": [
        {"name": "变异体质", "desc": "辐射改造的完美适应", "rarity": "legendary", "effects": {"stat_bonus": {"vit": 4}, "atk_mult": 1.2}},
        {"name": "病毒共生者", "desc": "与尸变病毒达成共生，伤愈奇快", "rarity": "legendary", "effects": {"atk_mult": 1.25, "stat_bonus": {"vit": 3}}},
        {"name": "晶核吞噬者", "desc": "能直接吞噬晶核汲取能量", "rarity": "epic", "effects": {"mp_bonus": 35, "skill_dmg_mult": 1.25}},
        {"name": "晶核共鸣", "desc": "与变异晶核能量共鸣", "rarity": "rare", "element": "thunder", "effects": {"skill_dmg_mult": 1.3, "mp_bonus": 20}},
        {"name": "拾荒本能", "desc": "废墟中总能找到宝贝", "rarity": "rare", "effects": {"gather_mult": 1.35, "loot_mult": 1.2}},
        {"name": "猎手嗅觉", "desc": "隔着半条街就能闻到变异兽的气息", "rarity": "rare", "effects": {"check_mult": 1.25, "atk_mult": 1.1}},
        {"name": "废土医者", "desc": "能用废料制药", "rarity": "uncommon", "effects": {"craft_mult": 1.25}},
        {"name": "生存专家", "desc": "末世求生的本能", "rarity": "uncommon", "effects": {"check_mult": 1.2, "stat_bonus": {"vit": 2}}},
        {"name": "陷阱巧手", "desc": "布设陷阱捕捉猎物", "rarity": "uncommon", "effects": {"gather_mult": 1.2, "check_mult": 1.15}},
        {"name": "幸存者运气", "desc": "大难不死必有后福", "rarity": "common", "effects": {"luck_bonus": 6}},
        {"name": "蛮力变异", "desc": "力量型变异", "rarity": "common", "element": "physical", "effects": {"skill_dmg_mult": 1.2}},
        {"name": "抗毒体质", "desc": "对毒素免疫", "rarity": "common", "effects": {"stat_bonus": {"vit": 3}}},
        {"name": "饥饿耐受", "desc": "三天不进食也能保持体力", "rarity": "common", "effects": {"stat_bonus": {"vit": 2, "str": 1}}},
        {"name": "电台守夜人", "desc": "守着电台总能第一时间听到物资情报", "rarity": "common", "effects": {"check_mult": 1.15, "loot_mult": 1.1}},
        {"name": "夜行者", "desc": "黑夜中来去自如", "rarity": "epic", "effects": {"atk_mult": 1.2, "check_mult": 1.2}},
        {"name": "机械师", "desc": "能修好废土上任何机器", "rarity": "rare", "effects": {"craft_mult": 1.3, "gather_mult": 1.15}},
        {"name": "荒野向导", "desc": "认得废土上每一条路", "rarity": "uncommon", "effects": {"check_mult": 1.2, "stat_bonus": {"dex": 2}}},
        {"name": "囤积癖", "desc": "见什么都想捡回家", "rarity": "common", "effects": {"gather_mult": 1.25, "loot_mult": 1.1}},
        {"name": "变异至尊", "desc": "吞噬晶核进化的至尊变异体", "rarity": "mythic", "effects": {"atk_mult": 1.4, "stat_bonus": {"vit": 3, "str": 2}}},
        # [装备新属性 2026-09-01] 概率型战斗天赋
        {"name": "狂化连爪", "desc": "兽化狂态下爪影成串", "rarity": "rare", "effects": {"combo_rate": 1.3}},
        {"name": "尸变反噬", "desc": "被咬的人先死，病毒替你回敬", "rarity": "epic", "effects": {"counter_rate": 1.4}},
        {"name": "饮血晶核", "desc": "晶核吞噬进化出吸血本能", "rarity": "rare", "effects": {"lifesteal": 1.35}},
    ],
    "western_fantasy": [
        {"name": "龙血脉", "desc": "远古巨龙的血脉传承", "rarity": "legendary", "element": "fire", "effects": {"skill_dmg_mult": 1.4, "atk_mult": 1.15}},
        {"name": "古神血胤", "desc": "沉睡古神的血脉在体内苏醒", "rarity": "legendary", "effects": {"mp_bonus": 40, "stat_bonus": {"int": 3}}},
        {"name": "大法师遗承", "desc": "承袭大法师的魔力回路", "rarity": "epic", "effects": {"mp_bonus": 30, "skill_dmg_mult": 1.25}},
        {"name": "圣光眷顾", "desc": "圣光垂青的选民", "rarity": "rare", "element": "light", "effects": {"skill_dmg_mult": 1.3, "mp_bonus": 20}},
        {"name": "精灵血统", "desc": "精灵的敏捷传承", "rarity": "rare", "effects": {"stat_bonus": {"dex": 4}}},
        {"name": "星象解读", "desc": "从星辰运转中窥见先机", "rarity": "rare", "effects": {"check_mult": 1.25, "luck_bonus": 4}},
        {"name": "矮人锻造", "desc": "矮人的锻造天赋", "rarity": "uncommon", "effects": {"craft_mult": 1.3, "stat_bonus": {"str": 2}}},
        {"name": "自然亲和", "desc": "与自然之力相通", "rarity": "uncommon", "element": "wood", "effects": {"gather_mult": 1.3, "skill_dmg_mult": 1.15}},
        {"name": "教会唱诗班出身", "desc": "圣歌吟诵中藏着祝福之力", "rarity": "uncommon", "element": "light", "effects": {"mp_bonus": 20, "check_mult": 1.15}},
        {"name": "冒险者直觉", "desc": "老练冒险者的第六感", "rarity": "common", "effects": {"check_mult": 1.2, "luck_bonus": 3}},
        {"name": "战士体魄", "desc": "久经沙场的身体", "rarity": "common", "effects": {"stat_bonus": {"str": 3, "vit": 1}}},
        {"name": "商人血统", "desc": "经商世家的精明", "rarity": "common", "effects": {"loot_mult": 1.2, "luck_bonus": 3}},
        {"name": "马厩长大", "desc": "与坐骑打交道长大，骑术精湛", "rarity": "common", "effects": {"stat_bonus": {"dex": 2, "vit": 1}}},
        {"name": "酒馆传闻通", "desc": "酒馆里总能打听到有用的传闻", "rarity": "common", "effects": {"check_mult": 1.15, "gather_mult": 1.1}},
        {"name": "元素亲和", "desc": "与四大元素天然亲和", "rarity": "epic", "effects": {"skill_dmg_mult": 1.25, "mp_bonus": 25}},
        {"name": "游侠箭术", "desc": "百步穿杨的箭术", "rarity": "rare", "element": "wind", "effects": {"skill_dmg_mult": 1.25, "atk_mult": 1.1}},
        {"name": "草药学", "desc": "辨识百草的药师", "rarity": "uncommon", "effects": {"craft_mult": 1.25, "gather_mult": 1.2}},
        {"name": "兽语者", "desc": "能与野兽简单交流", "rarity": "common", "element": "wood", "effects": {"check_mult": 1.15, "gather_mult": 1.15}},
        {"name": "神裔", "desc": "神明直系后裔的稀世血脉", "rarity": "mythic", "effects": {"skill_dmg_mult": 1.5, "mp_bonus": 50}},
        # [装备新属性 2026-09-01] 概率型战斗天赋
        {"name": "双巧手", "desc": "左右手同样灵巧，一击接一击", "rarity": "rare", "effects": {"combo_rate": 1.3}},
        {"name": "决斗者的敏锐", "desc": "对手出招的瞬间就是破绽", "rarity": "epic", "effects": {"counter_rate": 1.4}},
        {"name": "血族私裔", "desc": "远祖里混过一位血族，伤口吮合", "rarity": "rare", "effects": {"lifesteal": 1.35}},
    ],
}


def fallback_talent_pool(genre_id: str) -> list:
    """[P9] 取题材兜底天赋池（LLM 失败时用）。返回 list[dict]（Talent.to_dict 格式）。

    每个天赋补 cost（按 rarity 推算）+ genre 字段。6 题材全覆盖，未知题材回退西幻。
    """
    from src.models.world import Talent, _talent_cost_for_rarity
    pool = _GENRE_TALENT_TEMPLATES.get(genre_id) or _GENRE_TALENT_TEMPLATES["western_fantasy"]
    out = []
    for t in pool:
        t2 = dict(t)
        t2["genre"] = genre_id
        t2.setdefault("cost", _talent_cost_for_rarity(t2.get("rarity", "common")))
        out.append(Talent.from_dict(t2).to_dict())
    return out


def _entity_talents(entity: Any) -> list:
    """安全读天赋 list。

    支持两种入参：
    - entity 有 .talents 属性（player/NPC）-> 读之
    - entity 本身就是 list[dict]（如 CombatSession.player_talents）-> 直接用
    """
    if isinstance(entity, list):
        return [t for t in entity if isinstance(t, dict)]
    tl = getattr(entity, "talents", None)
    if not isinstance(tl, list):
        return []
    return [t for t in tl if isinstance(t, dict)]


def get_talent_mult(entity: Any, key: str, element: Optional[str] = None) -> float:
    """聚合天赋乘数（叠乘所有匹配天赋的 effects[key]）。

    - entity: player 或 NPC（都有 talents list[dict]）
    - key: skill_dmg_mult/atk_mult/gather_mult/craft_mult/check_mult/loot_mult
    - element: 技能的元素（仅 skill_dmg_mult 用）。天赋 element 非空时只对匹配元素生效。
    返回乘数（默认 1.0，多个天赋叠乘）。
    """
    mult = 1.0
    for t in _entity_talents(entity):
        eff = t.get("effects") or {}
        if not isinstance(eff, dict) or key not in eff:
            continue
        try:
            v = float(eff[key])
        except (TypeError, ValueError):
            continue
        # element 匹配：skill_dmg_mult + 天赋有 element 时只对同系技能生效
        t_elem = t.get("element", "") or ""
        if key == "skill_dmg_mult" and t_elem:
            if element and t_elem == element:
                mult *= v
            # 元素不匹配 -> 该天赋不加成此技能
        else:
            mult *= v
    return mult


def get_talent_bonus(entity: Any, key: str) -> int:
    """聚合天赋加值（叠加所有匹配天赋的 effects[key]，int 型如 luck_bonus/mp_bonus）。"""
    total = 0
    for t in _entity_talents(entity):
        eff = t.get("effects") or {}
        if not isinstance(eff, dict) or key not in eff:
            continue
        try:
            total += int(eff[key])
        except (TypeError, ValueError):
            continue
    return total


def get_talent_stat_bonus(entity: Any) -> dict:
    """聚合天赋 stat_bonus（{str/dex/int/vit/luk: 加值}，多天赋同属性叠加）。"""
    out: dict[str, int] = {}
    for t in _entity_talents(entity):
        eff = t.get("effects") or {}
        sb = eff.get("stat_bonus") if isinstance(eff, dict) else None
        if not isinstance(sb, dict):
            continue
        for k, v in sb.items():
            try:
                out[k] = out.get(k, 0) + int(v)
            except (TypeError, ValueError):
                continue
    return out


def effective_stat(entity: Any, key: str) -> int:
    """供生活、探索与资源公式读取基础五维 + 天赋五维；不修改存档裸值。"""
    if key not in ("str", "dex", "int", "vit", "luk"):
        return 0
    try:
        base = int(getattr(entity, f"stat_{key}", 10))
    except (TypeError, ValueError):
        base = 10
    return base + int(get_talent_stat_bonus(entity).get(key, 0) or 0)


def talent_summary(entity: Any) -> str:
    """[P9] 天赋摘要（供 UI 展示一行）。"""
    tl = _entity_talents(entity)
    if not tl:
        return "无"
    return "、".join(str(t.get("name", "")) for t in tl if t.get("name"))


# [P16] 天赋效果 key -> 中文标签（describe_talent 用；乘数型后接 xN，加值型后接 +N）
_TALENT_EFFECT_LABELS = {
    "skill_dmg_mult": "技能伤害",
    "atk_mult": "攻击",
    "gather_mult": "采集",
    "craft_mult": "合成",
    "check_mult": "检定",
    "loot_mult": "掉落",
    "luck_bonus": "幸运",
    "mp_bonus": "法力上限",
}


def describe_talent(t: dict, rarity_names: dict | None = None,
                    stat_names: dict | None = None) -> str:
    """[P16] 单条天赋的一行人类可读描述（档案页/战斗旁白共用，纯函数）。

    形如「雷天灵根（稀有）：雷系技能伤害×1.5、幸运+3」。rarity_names/stat_names
    可传 GenreText 的题材化显示名（缺省用通用中文）。
    """
    if not isinstance(t, dict):
        return ""
    name = str(t.get("name", "") or "").strip()
    if not name:
        return ""
    rn = rarity_names if isinstance(rarity_names, dict) else {}
    sn = stat_names if isinstance(stat_names, dict) else {}
    rarity = str(t.get("rarity", "") or "")
    rarity_label = rn.get(rarity, rarity) if rarity else ""
    element = str(t.get("element", "") or "")
    eff_parts = []
    eff = t.get("effects") or {}
    if isinstance(eff, dict):
        for k, v in eff.items():
            if k == "stat_bonus" and isinstance(v, dict):
                for sk, sv in v.items():
                    try:
                        eff_parts.append(f"{sn.get(sk, {'str': '力', 'dex': '敏', 'int': '智', 'vit': '耐', 'luk': '运'}.get(sk, sk))}+{int(sv)}")
                    except (TypeError, ValueError):
                        continue
                continue
            label = _TALENT_EFFECT_LABELS.get(k)
            if not label:
                continue
            try:
                if k.endswith("_mult"):
                    if k == "skill_dmg_mult" and element:
                        eff_parts.append(f"{ELEMENT_ZH.get(element, element)}系{label}×{float(v):g}")
                    else:
                        eff_parts.append(f"{label}×{float(v):g}")
                else:
                    eff_parts.append(f"{label}+{int(v)}")
            except (TypeError, ValueError):
                continue
    desc = t.get("desc", "") or ""
    body = "、".join(eff_parts) if eff_parts else str(desc)
    head = f"{name}（{rarity_label}）" if rarity_label else name
    return f"{head}：{body}" if body else head


def describe_talents(talents: list, rarity_names: dict | None = None,
                     stat_names: dict | None = None) -> list[str]:
    """[P16] 一组天赋的描述行列表（空天赋返回 []）。"""
    if not isinstance(talents, list):
        return []
    out = []
    for t in talents:
        line = describe_talent(t, rarity_names=rarity_names, stat_names=stat_names)
        if line:
            out.append(line)
    return out
