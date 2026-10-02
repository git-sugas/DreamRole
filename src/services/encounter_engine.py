"""[P8] 奇遇/机缘引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b / §23 总纲）：LLM 出静态语义（奇遇旁白文本），纯 Python 跑动态结算
（触发判定/类型抽取/稀有度升档/奖励发放/属性检定），SeededRng 确定性。
LLM 不参与「这次奇遇给什么」的动态结果，只在结算后据引擎产出的奖励摘要生成旁白。

== 触发模型（用户确认）==
- 自动触发：move_player 首次进入地点（explored False->True）时 roll 一次，每地点只一次
  （Location.encounter_done 标记防重）。触发率 = base(0.12) + luck*0.005 + danger*0.01。
- 手动探测：场景页「探测/搜索」按钮 -> intent_type="adventure"，可重复触发但概率较低
  （base 0.08 + luck*0.004 + danger*0.008），消耗 1 回合。

== 奇遇原型（archetype，题材无关的机制）==
- treasure     宝藏/遗物：直接奖励（item + gold + xp），无检定。
- trap         陷阱伴宝：dex 检定躲避；失败受小伤但仍得部分奖励。
- trial        秘境试炼：str 或 int 检定；通过得大奖，失败受伤。
- scripture    秘籍/传承：int 检定参悟；通过习得技能书/配方，失败只得 xp。
- spring       灵泉/异变：vit 抗性检定；回血 + 额外奖励物品。
- mystery      谜题机关：int 检定破解；通过得稀有物品，失败空手。
- caravan      商旅奇遇：无检定，得金币 + 中概率稀有物品（题材化「捡漏」）。
- hidden_npc   隐世高人：int 检定；通过得传承大奖（技能 + 物品），失败得忠告（小 xp）。
- tame         [P24d] 驯服奇遇：遇野兽幼崽/灵宠，dex/luk 取高检定 + 自动投食加成
               （消耗 1 件消耗品 +0.15，成败都消耗）；成功宠物入队，失败幼崽离去
               （检定在 pet_engine.tame_check 专项做，不走模板级 check_stat）。
- ruins        [P25a] 秘境入口发现：在野外发现秘境入口（dungeon_engine.build_dungeon
               挂当前地点，每地点限一个）；已有限/在聚落 -> 只得线索（小 xp）。无检定。

== 稀有度升档（luck + danger 驱动，复用于掉落）==
基础档据 archetype 风险定（trap/trial 风险高基础档高），再 roll：
  升档概率 = clamp(luck*0.008 + danger*0.01, 0, 0.5)
命中升档则 rarity 上一档（common->uncommon->...->legendary 封顶），可连续升。

== 属性全面织入 ==
- str：trial 试炼的破阵检定（高 str 直接通过秘境试炼）。
- dex：trap 陷阱躲避检定（高 dex 闪避陷阱无伤拿宝）。
- int：scripture/mystery/hidden_npc 的参悟/破解/传承检定。
- vit：spring 异变的抗性检定（高 vit 抵抗灵泉异变副作用，全额奖励）。
- luk：触发率权重 + 稀有度升档 + 商旅捡漏概率。
"""
from __future__ import annotations

from typing import Any, Optional

from src.utils.rng import SeededRng
from src.utils.dice_check import roll_dice_check, DiceRoll, TIER_CRIT, TIER_OK
from src.services.combat_engine import (
    success_chance, max_hp_for, carry_capacity, _RARITY_TIERS, _RARITY_MULT,
    difficulty_penalty_of,
)


# ---- 稀有度档位复用 combat_engine（避免双份维护不同步；见 review-changes 反馈）----
# _RARITY_TIERS / _RARITY_MULT 从 combat_engine 导入，本文件不重复定义。


# ---- archetype -> 基础稀有度档 + 风险（失败伤害比例）+ 检定属性 ----
# base_tier：奖励物品的起步稀有度（风险高的奇遇起步档高，平衡风险收益）。
# risk：检定失败时受伤害占 hp_max 的比例（0=无伤，0.25=受 25% 伤害）。
# check_stat：检定驱动属性（""= 无检定）。
# check_base：检定基础成功率（调用 success_chance 叠加 stat*0.01）。
_ARCHETYPE_CONFIG = {
    "treasure":   {"base_tier": "uncommon", "risk": 0.0,  "check_stat": "",    "check_base": 1.0},
    "trap":       {"base_tier": "rare",     "risk": 0.20, "check_stat": "dex", "check_base": 0.5},
    "trial":      {"base_tier": "epic",     "risk": 0.30, "check_stat": "str", "check_base": 0.45},
    "scripture":  {"base_tier": "rare",     "risk": 0.0,  "check_stat": "int", "check_base": 0.5},
    "spring":     {"base_tier": "uncommon", "risk": 0.15, "check_stat": "vit", "check_base": 0.6},
    "mystery":    {"base_tier": "epic",     "risk": 0.0,  "check_stat": "int", "check_base": 0.4},
    "caravan":    {"base_tier": "uncommon", "risk": 0.0,  "check_stat": "",    "check_base": 1.0},
    "hidden_npc": {"base_tier": "epic",     "risk": 0.10, "check_stat": "int", "check_base": 0.4},
    # [P24b] 节庆活动：默认档温和（uncommon 起步 + 低风险），检定参数由节庆活动池
    # 逐条覆盖（比试 str / 猜谜 int / 竞速 dex / 宴饮 vit），复用检定 + 奖励框架。
    # [!] 模板池在 calendar_engine._GENRE_FESTIVAL_ACTIVITIES（不进主奇遇池，避免
    # 普通探索随机抽到节庆活动——节庆只在节日天聚落出）。
    # [P24d] 驯服：检定在 resolve 的 tame 分支走 pet_engine.tame_check（dex/luk 双属性
    # 取高 + 投食加成），模板级 check_stat 留空防双检定；base_tier/risk 仅供稀有度 roll。
    "tame":       {"base_tier": "common",   "risk": 0.05, "check_stat": "",    "check_base": 1.0},
    # [P25a] 秘境入口发现：无检定（入口即奖励，建 Dungeon 挂当前野外地点）。
    "ruins":      {"base_tier": "uncommon", "risk": 0.0,  "check_stat": "",    "check_base": 1.0},
    # [P26a] 定情信物奇遇：幸运检定（低风险，rare 起步），通过发 is_courtship_gift=True
    # 信物入包（信物即奖励，豁免金币下限，仿 tame/ruins）；失败小 xp + 提示。
    "romance":    {"base_tier": "rare",     "risk": 0.10, "check_stat": "luk", "check_base": 0.5},
}


# ---- 6 题材奇遇模板池（每题材 11 原型各 4 条题材化变体，共 44 条；rng.pick 抽取）----
# resolve 后叙事 LLM 据 archetype + name + desc + 奖励摘要生成旁白文本（LLM 出语义）。
# 引擎只用 archetype 机制 + rarity，name/desc 纯展示给 LLM/玩家。
_GENRE_ENCOUNTER_TEMPLATES = {
    "xianxia": [
        {"archetype": "treasure",   "name": "古修遗府", "desc": "一处尘封的古修洞府，阵法已散，似有遗物留存"},
        {"archetype": "treasure",   "name": "坠星坑", "desc": "陨星坠地砸出的深坑，坑底隐见宝光流转"},
        {"archetype": "trap",       "name": "禁制陷阱", "desc": "残存的禁制突然激发，需以身法闪避"},
        {"archetype": "trap",       "name": "噬灵藤阵", "desc": "脚下灵藤暴起缠人，需趁合围前脱身"},
        {"archetype": "trial",      "name": "秘境试炼", "desc": "一道古老的禁制之力，需以蛮力破之"},
        {"archetype": "trial",      "name": "镇妖石门", "desc": "镇压妖物的沉重石门，推开需过人膂力"},
        {"archetype": "scripture",  "name": "残破玉简", "desc": "半截玉简浮于灵光之中，内含功法残篇"},
        {"archetype": "scripture",  "name": "壁上剑痕", "desc": "崖壁刻着前人剑意，观之可悟上乘剑诀"},
        {"archetype": "spring",     "name": "灵泉眼", "desc": "地底灵泉涌出，灵气氤氲却暗含异变"},
        {"archetype": "spring",     "name": "血色灵乳", "desc": "石乳凝成的一泓灵液，饮之或益或损"},
        {"archetype": "mystery",    "name": "古阵谜题", "desc": "一座古老阵法封锁着宝物，需以灵识参悟"},
        {"archetype": "mystery",    "name": "锁灵棋局", "desc": "残局困着一件灵物，破棋方可取出"},
        {"archetype": "caravan",    "name": "游方散修", "desc": "一位游方散修愿低价出让手中的天材地宝"},
        {"archetype": "caravan",    "kind": "settlement", "name": "坊市夜商", "desc": "收摊的坊市商人贱卖压箱底的货物"},
        {"archetype": "hidden_npc", "name": "隐世老祖", "desc": "一位隐世老祖显化，欲择有缘人传道"},
        {"archetype": "hidden_npc", "name": "守墓剑灵", "desc": "剑冢深处的守墓剑灵，欲寻新主托付衣钵"},
        {"archetype": "tame",       "kind": "wilderness", "name": "受伤灵兽幼崽", "desc": "草丛里蜷着一只受伤的灵兽幼崽，警惕地注视来人"},
        {"archetype": "tame",       "kind": "wilderness", "name": "贪吃小妖", "desc": "一只贪吃的小妖闻到干粮香气，探头探脑不肯走远"},
        {"archetype": "ruins",      "name": "塌陷的洞府入口", "desc": "山体塌陷露出一座古修洞府的入口，禁制早已失效"},
        {"archetype": "ruins",      "name": "矿道深处的阵门", "desc": "废弃矿道尽头立着一道刻满符文的石门"},
        {"archetype": "romance",    "name": "古修定情玉佩", "desc": "废墟石台上静卧一枚玉佩，温润如初，似藏前人情意"},
        {"archetype": "romance",    "name": "灵犀同心结", "desc": "一缕灵气凝成同心结，绳结微动似有灵犀"},
        {"archetype": "treasure",   "name": "灵瀑水府", "desc": "灵瀑之后藏着一座水府，宝气隐隐外泄"},
        {"archetype": "trap",       "name": "迷踪阵", "desc": "误入一座迷踪阵法，方位颠倒需辨明生门"},
        {"archetype": "trial",      "name": "试剑石", "desc": "一块试剑石，需全力一击验明剑意"},
        {"archetype": "scripture",  "name": "丹火炉铭", "desc": "废弃丹炉的炉铭刻着失传丹方"},
        {"archetype": "spring",     "name": "地肺灵泉", "desc": "地肺深处涌出的灵泉，泉边灵雾成霞"},
        {"archetype": "mystery",    "name": "星象谜阵", "desc": "地面星象错乱，需推演天机方得解法"},
        {"archetype": "caravan",    "name": "行脚丹商", "desc": "一位行脚丹商愿以物易物换取灵材"},
        {"archetype": "hidden_npc", "name": "剑冢剑灵", "desc": "剑冢之中一缕剑灵显化，欲择人传剑"},
        {"archetype": "tame",       "kind": "wilderness", "name": "灵鹤幼雏", "desc": "崖边巢里一只灵鹤幼雏，正扑棱着翅膀"},
        {"archetype": "tame",       "kind": "settlement", "name": "檐下狸奴", "desc": "客栈檐下蜷着一只通灵的小狸奴，见你便竖起尾巴轻声叫唤"},
        {"archetype": "ruins",      "name": "湖底古洞", "desc": "湖心漩涡下露出一座上古洞府的门楣"},
        {"archetype": "romance",    "name": "月老红绳", "desc": "古树下一条月老红绳，两端似有缘人"},
        {"archetype": "treasure",   "name": "浮空殿残阁", "desc": "一截断殿悬浮半空，殿阁残破却宝光流转，似上古宗门遗存"},
        {"archetype": "trap",       "name": "傀儡石卫", "desc": "山道旁的石人骤然睁眼，千年傀儡禁制被脚步声唤醒"},
        {"archetype": "trial",      "name": "封灵锁链", "desc": "一条儿臂粗的锁链封锁洞口，链上符纹灼灼，需蛮力扯断"},
        {"archetype": "scripture",  "name": "浮屠塔影", "desc": "七层浮屠虚影中隐有诵经声，静坐塔下可悟禅功"},
        {"archetype": "spring",     "name": "山腹灵雾泉", "desc": "山腹温泉泛着灵雾，浸之似可洗经伐髓"},
        {"archetype": "mystery",    "name": "甲骨天书", "desc": "一片兽骨上刻着无人能识的天书古篆，参悟可得机缘"},
        {"archetype": "caravan",    "kind": "settlement", "name": "坊市散摊", "desc": "坊市散市的游商收摊，愿贱价出让压箱灵物"},
        {"archetype": "hidden_npc", "name": "失明琴师", "desc": "破庙里的失明琴师一曲抚尽人间事，欲寻人传音修之道"},
        {"archetype": "ruins",      "name": "火口古窑", "desc": "熄火千年的古窑之下，藏着一处炼器师的洞府入口"},
        {"archetype": "romance",    "name": "同心玉锁", "desc": "摊上一把旧锁，锁身刻着两行小字，钥匙却不知流落天涯何处"}
    ],
    "wuxia": [
        {"archetype": "treasure",   "name": "藏宝古洞", "desc": "山壁间隐现一处藏宝洞，似有前辈遗物"},
        {"archetype": "treasure",   "name": "沉船货舱", "desc": "河湾搁浅的沉船露出货舱，箱笼半掩"},
        {"archetype": "trap",       "name": "暗器机括", "desc": "误触机关，暗器如雨射出，需以轻功躲避"},
        {"archetype": "trap",       "name": "翻板陷坑", "desc": "脚下翻板骤然开启，坑底竹签寒光闪闪"},
        {"archetype": "trial",      "name": "石门试炼", "desc": "一道千斤石门挡路，需以臂力推开"},
        {"archetype": "trial",      "name": "铁锁吊桥", "desc": "锈死的铁锁吊桥，需硬拉绞盘放桥通行"},
        {"archetype": "scripture",  "name": "武学残页", "desc": "墙角散落几页泛黄残页，记着失传武学"},
        {"archetype": "scripture",  "name": "掌门遗札", "desc": "密室铁匣藏着一封掌门遗札，附内功心法"},
        {"archetype": "spring",     "name": "灵药奇泉", "desc": "一泓异香扑鼻的灵泉，饮之或有所得"},
        {"archetype": "spring",     "name": "百年药酒", "desc": "酒窖深处埋着百年药酒，一坛值千金"},
        {"archetype": "mystery",    "name": "机关谜匣", "desc": "一只精巧的机关匣，需以内功参透机括"},
        {"archetype": "mystery",    "name": "藏宝图残卷", "desc": "半张藏宝图，需勘破暗语方知埋宝处"},
        {"archetype": "caravan",    "name": "江湖商队", "desc": "一队江湖商旅在此歇脚，愿低价出货"},
        {"archetype": "caravan",    "kind": "settlement", "name": "镖局甩货", "desc": "镖队愿贱价甩掉累赘货担轻身上路"},
        {"archetype": "hidden_npc", "name": "隐世高人", "desc": "一位扫地老者气度不凡，似在等人"},
        {"archetype": "hidden_npc", "name": "退隐镖头", "desc": "退隐的老镖头在此独居，欲收个关门弟子"},
        {"archetype": "tame",       "kind": "wilderness", "name": "离群幼鹰", "desc": "一只离群的幼鹰落在断枝上，振翅欲飞又舍不得肉干"},
        {"archetype": "tame",       "kind": "settlement", "name": "市集弃犬", "desc": "市集角落一只被遗弃的幼犬，怯生生地摇着尾巴"},
        {"archetype": "ruins",      "name": "荒草掩映的寨门", "desc": "荒草中露出半截山寨石门，门后甬道幽深"},
        {"archetype": "ruins",      "name": "崖壁石窟暗道", "desc": "崖壁的佛像后藏着一条向下延伸的石窟暗道"},
        {"archetype": "romance",    "name": "旧坊市鸳鸯佩", "desc": "旧坊市摊上有一对鸳鸯玉佩，色泽温润似有前缘"},
        {"archetype": "romance",    "name": "剑匣定情穗", "desc": "无主剑匣里系着一根亲手编的红穗，绣着并蒂莲"},
        {"archetype": "treasure",   "name": "义庄藏银", "desc": "义庄停尸床下暗藏一箱白银"},
        {"archetype": "trap",       "name": "地刺翻板", "desc": "石板下弹出地刺，需提气纵身避开"},
        {"archetype": "trial",      "name": "抱柱力士", "desc": "石柱需双臂合抱方可撼动"},
        {"archetype": "scripture",  "name": "残谱剑诀", "desc": "酒旗内衬里抄着一式失传剑诀"},
        {"archetype": "spring",     "name": "药王泉", "desc": "深山药王泉，饮之可疗暗伤"},
        {"archetype": "mystery",    "name": "铜锁九连环", "desc": "一把九连环铜锁，需心细解之"},
        {"archetype": "caravan",    "kind": "settlement", "name": "走镖余货", "desc": "镖师愿把走镖余货低价出清"},
        {"archetype": "hidden_npc", "name": "扫地僧", "desc": "寺前扫地老僧，掌风扫叶暗藏禅机"},
        {"archetype": "tame",       "kind": "settlement", "name": "偷酒小猴", "desc": "一只偷酒吃醉的小猴，抱着酒葫芦不肯撒手"},
        {"archetype": "ruins",      "name": "断崖密道", "desc": "断崖藤蔓后是一条人工开凿的密道"},
        {"archetype": "romance",    "name": "并蒂发簪", "desc": "无主妆奁里有支并蒂发簪，簪尾刻着誓言"},
        {"archetype": "treasure",   "name": "枯井官银", "desc": "废弃枯井底沉着一只官银箱，锁头早已锈死"},
        {"archetype": "trap",       "name": "悬梁坠石", "desc": "梁上悬石轰然砸落，需就地翻滚避开"},
        {"archetype": "trial",      "name": "千斤石磨", "desc": "千斤石磨堵住墓道，需抱住磨盘挪开一条缝隙"},
        {"archetype": "scripture",  "name": "绣楼藏谱", "desc": "绣楼夹墙里藏着一册针法暗合穴道的武学秘谱"},
        {"archetype": "spring",     "name": "雪水醒神汤", "desc": "高山雪水熬的醒神汤，一碗下肚困顿尽消"},
        {"archetype": "mystery",    "name": "亭柱联句", "desc": "亭柱上刻着半副对联，对上方显机关"},
        {"archetype": "caravan",    "kind": "settlement", "name": "当铺死当", "desc": "当铺清死当柜，压箱货论件甩卖"},
        {"archetype": "hidden_npc", "name": "茶馆说书人", "desc": "茶馆说书先生惊堂木一拍，说的竟全是真功夫门道"},
        {"archetype": "tame",       "kind": "settlement", "name": "镖局狸花猫", "desc": "镖局门口的小狸花猫，天天蹲着看武师们练功"},
        {"archetype": "ruins",      "name": "运河沉仓", "desc": "运河清淤露出的官仓地窖，入口封着铁条"},
        {"archetype": "romance",    "name": "绣帕并蒂莲", "desc": "风掀起一方绣帕，角上绣着并蒂莲，针脚细密如新"}
    ],
    "modern": [
        {"archetype": "treasure",   "name": "遗忘保险柜", "desc": "墙后藏着一个遗忘的保险柜，似有贵重物品"},
        {"archetype": "treasure",   "name": "拆迁楼遗物", "desc": "待拆老楼里房主没搬走的旧物，夹层藏着硬货"},
        {"archetype": "trap",       "name": "警报陷阱", "desc": "误触警报系统，需以反应迅速脱身"},
        {"archetype": "trap",       "name": "看门狼狗", "desc": "仓库里扑出的狼狗，需身手敏捷甩开"},
        {"archetype": "trial",      "name": "加固铁门", "desc": "一道加固铁门挡路，需以体能破开"},
        {"archetype": "trial",      "name": "堵死消防通道", "desc": "被杂物堵死的通道，需凭力气清出通路"},
        {"archetype": "scripture",  "name": "加密 U 盘", "desc": "一只落灰的加密 U 盘，内含关键数据"},
        {"archetype": "scripture",  "name": "私人笔记", "desc": "行家留下的训练笔记，记着真传技法"},
        {"archetype": "spring",     "name": "异变药剂", "desc": "一瓶来路不明的药剂，饮下或有机缘"},
        {"archetype": "spring",     "name": "偏方药膳", "desc": "老店秘传的药膳汤，喝下通体舒泰"},
        {"archetype": "mystery",    "name": "电子密码锁", "desc": "一道电子密码锁封锁着宝物，需以智力破解"},
        {"archetype": "mystery",    "name": "谜题保险箱", "desc": "老式转盘保险箱，破译生日暗码方能打开"},
        {"archetype": "caravan",    "kind": "settlement", "name": "黑市线人", "desc": "一名黑市线人愿低价出让手中的稀货"},
        {"archetype": "caravan",    "name": "尾货甩卖", "desc": "收摊的老板论堆甩卖仓库尾货"},
        {"archetype": "hidden_npc", "name": "神秘专家", "desc": "一位深藏不露的专家，似在寻接班人"},
        {"archetype": "hidden_npc", "name": "退隐老师傅", "desc": "弄堂里的修车老师傅，一身手艺无人继承"},
        {"archetype": "tame",       "kind": "settlement", "name": "纸箱里的猫", "desc": "便利店门口的纸箱里，一只小猫冲你喵喵叫"},
        {"archetype": "tame",       "kind": "settlement", "name": "流浪犬救助", "desc": "一只脖圈磨破的流浪犬远远跟着你，不敢靠近"},
        {"archetype": "ruins",      "name": "围挡下的地库入口", "desc": "拆迁围挡后是一处通向地下的隐秘入口"},
        {"archetype": "ruins",      "name": "锈死的防洪闸", "desc": "河道防洪闸后竟藏着一整片废弃空间"},
        {"archetype": "romance",    "name": "古董店情侣对戒", "desc": "古董店橱窗里一对旧式对戒，内侧刻着相同的名字缩写"},
        {"archetype": "romance",    "name": "旧书夹的情书", "desc": "旧书摊的一本诗集里夹着未寄出的情书，信纸尚香"},
        {"archetype": "treasure",   "name": "废弃金库", "desc": "银行地下废弃金库，锁芯已锈"},
        {"archetype": "trap",       "name": "红外警戒", "desc": "房间布满红外警戒线，需猫腰穿过"},
        {"archetype": "trial",      "name": "防爆门", "desc": "一扇防爆门卡死，需撬杠硬开"},
        {"archetype": "scripture",  "name": "维修图册", "desc": "老技师的维修图册，画满独门技巧"},
        {"archetype": "spring",     "name": "雾化理疗舱", "desc": "一台仍能运转的理疗舱，蒸汽氤氲"},
        {"archetype": "mystery",    "name": "指纹锁箱", "desc": "指纹锁箱，需还原指纹痕迹解密"},
        {"archetype": "caravan",    "kind": "settlement", "name": "夜市甩货摊", "desc": "夜市收摊的老板论斤甩卖尾货"},
        {"archetype": "hidden_npc", "name": "隐居匠人", "desc": "楼顶隐居的老匠人，一身绝活无人继承"},
        {"archetype": "tame",       "kind": "settlement", "name": "纸箱弃猫", "desc": "楼道纸箱里一窝小奶猫，正嘤嘤叫着"},
        {"archetype": "tame",       "kind": "wilderness", "name": "郊野流浪犬", "desc": "郊野小路边一只瘦得脱相的流浪犬，远远跟着你不敢靠近"},
        {"archetype": "ruins",      "name": "防空洞入口", "desc": "小区角落的防空洞入口，铁门虚掩"},
        {"archetype": "romance",    "name": "影院旧票根", "desc": "旧影院座位缝里夹着一张双人票根，已泛黄"},
        {"archetype": "treasure",   "name": "无人仓储箱", "desc": "拍卖的无人储物仓里，压着一只上锁的旧皮箱"},
        {"archetype": "trap",       "name": "水泵房漏电", "desc": "水泵房电缆漏电，积水的地面跨步电压步步惊心"},
        {"archetype": "trial",      "name": "卡死卷帘门", "desc": "防盗卷帘门卡死半空，需撬开一条能钻身的缝"},
        {"archetype": "scripture",  "name": "退役教官手记", "desc": "退役教官的手记，记着一套巷战近身要诀"},
        {"archetype": "spring",     "name": "深山私汤", "desc": "深山民宿的私汤温泉，泡完浑身通透"},
        {"archetype": "mystery",    "name": "乱码二维码", "desc": "铁盒上贴着扫出乱码的二维码，需反向破译才能打开"},
        {"archetype": "caravan",    "kind": "settlement", "name": "周末跳蚤市场", "desc": "周末跳蚤市场，老物件论堆卖"},
        {"archetype": "hidden_npc", "name": "车库拳师", "desc": "地下车库练拳的老者，出手快得看不清"},
        {"archetype": "ruins",      "name": "地铁废线", "desc": "封闭的地铁废线尽头，藏着一扇焊死的铁门"},
        {"archetype": "romance",    "name": "老胶卷合照", "desc": "旧相机店冲洗出的老胶卷，定格着一对陌生男女的笑脸"}
    ],
    "scifi": [
        {"archetype": "treasure",   "name": "异星遗迹", "desc": "一处异星文明遗迹，能量读数异常"},
        {"archetype": "treasure",   "name": "漂浮货柜", "desc": "脱离航道的漂浮货柜，封条内货物完好"},
        {"archetype": "trap",       "name": "防御无人机", "desc": "遗迹防御无人机激活，需以敏捷闪避"},
        {"archetype": "trap",       "name": "激光网格", "desc": "走廊骤然亮起激光网格，需抓住间隙穿过"},
        {"archetype": "trial",      "name": "能量屏障", "desc": "一道能量屏障封锁通路，需以力量破开"},
        {"archetype": "trial",      "name": "变形舱门", "desc": "故障卡死的舰体舱门，需硬拉液压杆开启"},
        {"archetype": "scripture",  "name": "数据晶体", "desc": "一枚数据晶体闪烁，内含失落科技"},
        {"archetype": "scripture",  "name": "黑匣子", "desc": "坠落穿梭机的黑匣子，存着绝密航行档案"},
        {"archetype": "spring",     "name": "纳米舱", "desc": "一座废弃纳米舱，注入或能改造体质"},
        {"archetype": "spring",     "name": "基因制剂", "desc": "一支来路不明的基因制剂，注射或有机缘"},
        {"archetype": "mystery",    "name": "加密终端", "desc": "一台加密终端封锁着物资，需以神经接口破解"},
        {"archetype": "mystery",    "name": "量子密钥盒", "desc": "需解开纠缠态谜题才能开启的密钥盒"},
        {"archetype": "caravan",    "name": "星际商船", "desc": "一艘星际商船在此补给，愿低价出货"},
        {"archetype": "caravan",    "name": "走私甩单", "desc": "赶在巡逻舰到来前，走私贩急于脱手货物"},
        {"archetype": "hidden_npc", "name": "觉醒 AI", "desc": "一个觉醒的 AI 意识，欲寻继承者"},
        {"archetype": "hidden_npc", "name": "流亡科学家", "desc": "隐居舱段的流亡科学家，欲托付毕生研究"},
        {"archetype": "tame",       "name": "失控实验体", "desc": "一只逃出实验室的小型实验体，正嗅着你的补给包"},
        {"archetype": "tame",       "name": "废弃舱段异兽", "desc": "废弃舱段的通风口里，探出一双好奇的眼睛"},
        {"archetype": "ruins",      "name": "沙埋的舱门", "desc": "黄沙半掩着一扇完好的舱门，门缝透出微光"},
        {"archetype": "ruins",      "name": "岩层下的电梯井", "desc": "塌方的探井底还有一部可用的升降平台"},
        {"archetype": "romance",    "name": "休眠舱的吊坠", "desc": "休眠舱内挂着一枚全息吊坠，循环播放着一段誓言"},
        {"archetype": "romance",    "name": "数据流情书", "desc": "残存数据流里浮出一封加密情书，解之得一信物"},
        {"archetype": "treasure",   "name": "陨金矿脉", "desc": "小行星裂缝里露出稀有的陨金矿脉"},
        {"archetype": "trap",       "name": "力场陷阱", "desc": "走廊布着力场陷阱，需算准时机通过"},
        {"archetype": "trial",      "name": "液压闸门", "desc": "失压的液压闸门，需外力撬动"},
        {"archetype": "scripture",  "name": "技师日志", "desc": "维修技师的加密日志，记着独门技术"},
        {"archetype": "spring",     "name": "再生培养舱", "desc": "营养液尚存的培养舱，浸泡可愈伤"},
        {"archetype": "mystery",    "name": "星图谜锁", "desc": "需对照星图解开坐标才能开启的谜锁"},
        {"archetype": "caravan",    "kind": "settlement", "name": "黑市货栈", "desc": "黑市货栈愿低价清理一批禁运品"},
        {"archetype": "hidden_npc", "name": "流亡博士", "desc": "流亡的退休博士，欲托付未完成的研究"},
        {"archetype": "tame",       "name": "机械幼犬", "desc": "一只报废改装的机械幼犬，围着你转圈"},
        {"archetype": "ruins",      "name": "信号源入口", "desc": "荒漠中一处持续发信的古文明入口"},
        {"archetype": "romance",    "name": "星图对坠", "desc": "一对星图对坠，合璧才显完整航道"},
        {"archetype": "treasure",   "name": "环带沉舱", "desc": "沉入行星环带的货舱，信标仍在闪烁"},
        {"archetype": "trap",       "name": "纳米蜂群", "desc": "门后纳米蜂群苏醒，需以磁场干扰驱散"},
        {"archetype": "trial",      "name": "失压气闸", "desc": "双层气闸外门失压卡死，需手动泄压才能开启"},
        {"archetype": "scripture",  "name": "废弃实验室日志", "desc": "废弃实验室的加密日志，记着一套基因编辑范式"},
        {"archetype": "spring",     "name": "零重力疗养舱", "desc": "零重力疗养舱尚有余能，漂浮其中可缓深空骨损"},
        {"archetype": "mystery",    "name": "引力波密文", "desc": "终端循环播放一段引力波形，暗藏开门密钥"},
        {"archetype": "caravan",    "kind": "settlement", "name": "殖民集市", "desc": "边境殖民地的周末集市，旧科技换新电池"},
        {"archetype": "hidden_npc", "name": "冬眠舱老人", "desc": "提前苏醒的冬眠者，掌握着一段失落的航线记忆"},
        {"archetype": "tame",       "name": "报废清洁机器人", "desc": "报废的清洁机器人仍固执地跟着你，螺丝松了也不肯停"},
        {"archetype": "ruins",      "name": "融毁反应堆", "desc": "融毁反应堆之下，竟有一层完好的避难实验室"},
        {"archetype": "romance",    "name": "侦察机求婚航拍", "desc": "侦察无人机的存储里，存着一场未完成的求婚航拍"}
    ],
    "apocalypse": [
        {"archetype": "treasure",   "name": "军需箱", "desc": "废墟下埋着一只军需箱，锈迹斑斑"},
        {"archetype": "treasure",   "name": "避难所储物间", "desc": "撬开民防门的储物间，物资码放整齐"},
        {"archetype": "trap",       "name": "变异陷阱", "desc": "触发了一只变异兽的巢穴陷阱，需敏捷脱身"},
        {"archetype": "trap",       "name": "绊线霰弹", "desc": "幸存者布的绊线连着霰弹枪，需伏身避过"},
        {"archetype": "trial",      "name": "坍塌通道", "desc": "一段坍塌的通道挡路，需以体能清理"},
        {"archetype": "trial",      "name": "变形卷帘门", "desc": "地震变形的卷帘门，需撬出缝隙钻过"},
        {"archetype": "scripture",  "name": "幸存者日记", "desc": "一本幸存者日记，记着关键生存技艺"},
        {"archetype": "scripture",  "name": "军用维修手册", "desc": "前线部队的维修手册，枪械改装真传"},
        {"archetype": "spring",     "name": "变异源泉", "desc": "一汪散发异光的变异源泉，接触或有所得"},
        {"archetype": "spring",     "name": "净化滤芯水", "desc": "仍在运转的净水器滤出的一泓清水"},
        {"archetype": "mystery",    "name": "废墟谜锁", "desc": "一道机械谜锁封锁着物资，需以智力破解"},
        {"archetype": "mystery",    "name": "保险柜暗码", "desc": "银行废墟的保险柜，需推理死者暗码"},
        {"archetype": "caravan",    "name": "拾荒商队", "desc": "一队拾荒商队在此扎营，愿低价出货"},
        {"archetype": "caravan",    "name": "电台以物易物", "desc": "火腿电台里的老主顾，愿拿存货换晶核"},
        {"archetype": "hidden_npc", "name": "末日老兵", "desc": "一位末日老兵隐居于此，似在寻传人"},
        {"archetype": "hidden_npc", "name": "避难所医生", "desc": "留守的避难所医生，医术无人传承"},
        {"archetype": "tame",       "kind": "wilderness", "name": "瑟缩幼兽", "desc": "废墟夹缝里一只瑟缩的变异幼兽，饿得直舔爪子"},
        {"archetype": "tame",       "kind": "settlement", "name": "翻垃圾的小家伙", "desc": "一个小家伙正翻着你的背包残渣，被发现后僵在原地"},
        {"archetype": "ruins",      "name": "瓦砾下的地堡门", "desc": "清理瓦砾时撬开了一扇厚重的地堡防爆门"},
        {"archetype": "ruins",      "name": "地铁站台的裂缝", "desc": "站台墙面的裂缝后传出风声，似有空间相通"},
        {"archetype": "romance",    "name": "废墟里的相框", "desc": "瓦砾下半埋着一个相框，框中两人的合影已成孤本"},
        {"archetype": "romance",    "name": "求生者遗的戒指", "desc": "避难所角落有枚刻字戒指，似是某对恋人离散的信物"},
        {"archetype": "treasure",   "name": "军火储备室", "desc": "民防地图标记的军火储备室"},
        {"archetype": "trap",       "name": "弩箭机关", "desc": "幸存者设的弩箭机关，需贴地爬过"},
        {"archetype": "trial",      "name": "坍塌梁柱", "desc": "坍塌的楼板梁柱挡路，需挪开"},
        {"archetype": "scripture",  "name": "药剂师笔记", "desc": "药剂师留下的配方笔记"},
        {"archetype": "spring",     "name": "净水车", "desc": "还能出水的净水车，水质清冽"},
        {"archetype": "mystery",    "name": "密码锁箱", "desc": "军需箱的密码锁，需推理密码"},
        {"archetype": "caravan",    "name": "车队补给点", "desc": "过路车队的临时补给点，愿以物易物"},
        {"archetype": "hidden_npc", "name": "遗世老兵", "desc": "隐居哨塔的老兵，欲传一手枪法"},
        {"archetype": "tame",       "kind": "settlement", "name": "洞中幼犬", "desc": "废屋里一窝幼犬，饿得呜呜叫"},
        {"archetype": "ruins",      "name": "工厂地库", "desc": "化工厂后门的地库入口，铁链已锈"},
        {"archetype": "romance",    "name": "求生对戒", "desc": "废墟里有对磨花的对戒，刻着两人名字"},
        {"archetype": "treasure",   "name": "加油站地库", "desc": "加油站地库的铁门后，整箱物资原封未动"},
        {"archetype": "trap",       "name": "腐朽地板", "desc": "腐朽的地板一踩就塌，需贴墙快步通过"},
        {"archetype": "trial",      "name": "集装箱堆", "desc": "倒塌的集装箱堆成死墙，需徒手攀越翻过"},
        {"archetype": "scripture",  "name": "猎人陷阱册", "desc": "老猎人手绘的陷阱册，页页都是活命的经验"},
        {"archetype": "spring",     "name": "天台净水塔", "desc": "天台自制的净化塔积了半箱清水"},
        {"archetype": "mystery",    "name": "电台坐标暗语", "desc": "断续的电台信号里，藏着一段坐标暗语"},
        {"archetype": "caravan",    "kind": "settlement", "name": "幸存者市集", "desc": "幸存者据点的周末市集，以物易物"},
        {"archetype": "hidden_npc", "name": "地铁工程师", "desc": "守着供电系统的老工程师，能让整条线路复明"},
        {"archetype": "tame",       "kind": "wilderness", "name": "落单小狼", "desc": "变异狼群走后落下一只小狼，对着你的火堆呜呜叫"},
        {"archetype": "ruins",      "name": "教堂地宫", "desc": "塌了半边的教堂祭坛下，藏着一道地宫石阶"},
        {"archetype": "romance",    "name": "未寄出的婚柬", "desc": "邮局废墟里一封盖了邮戳却永未寄出的婚柬"}
    ],
    "western_fantasy": [
        {"archetype": "treasure",   "name": "远古宝箱", "desc": "废墟中静卧一只远古宝箱，魔法已散"},
        {"archetype": "treasure",   "name": "龙窟藏金", "desc": "巨龙旧巢的阴影里，金币与骸骨同眠"},
        {"archetype": "trap",       "name": "魔法陷阱", "desc": "一道魔法符阵突然激发，需敏捷闪避"},
        {"archetype": "trap",       "name": "塌陷地窖", "desc": "修道院地窖骤然塌陷，需踩梁柱跃出"},
        {"archetype": "trial",      "name": "封印石门", "desc": "一道封印石门挡路，需以蛮力推开"},
        {"archetype": "trial",      "name": "断桥残索", "desc": "吊桥已断只剩铁索，需臂力攀援而过"},
        {"archetype": "scripture",  "name": "魔法卷轴", "desc": "一卷褪色的魔法卷轴，记着失传法术"},
        {"archetype": "scripture",  "name": "法师手札", "desc": "旅法师的手札残页，绘着秘法阵图"},
        {"archetype": "spring",     "name": "神圣泉水", "desc": "一汪神圣泉水，饮之或受赐福"},
        {"archetype": "spring",     "name": "月光井", "desc": "月圆之夜才涌出的古井灵水，饮之有异象"},
        {"archetype": "mystery",    "name": "符文谜题", "desc": "一道符文谜题封锁着宝物，需以智力参悟"},
        {"archetype": "mystery",    "name": "精灵棋局", "desc": "精灵石桌上摆着未完的棋局，胜之得宝"},
        {"archetype": "caravan",    "name": "旅行商人", "desc": "一位旅行商人愿低价出让奇货"},
        {"archetype": "caravan",    "name": "补给车队", "desc": "前往领堡的补给车队甩卖冗余辎重"},
        {"archetype": "hidden_npc", "name": "隐世法师", "desc": "一位隐世法师现身，欲择有缘人传艺"},
        {"archetype": "hidden_npc", "name": "退役圣殿骑士", "desc": "卸甲的老骑士守着小神龛，欲传守护之志"},
        {"archetype": "tame",       "kind": "wilderness", "name": "巢中幼兽", "desc": "空巢里只剩一只幼兽，正咬着你的行囊带子不放"},
        {"archetype": "tame",       "kind": "wilderness", "name": "衔食小灵鸟", "desc": "一只小灵鸟衔走了你的口粮，又落在不远处的树枝上等你"},
        {"archetype": "ruins",      "name": "苔痕斑驳的墓门", "desc": "古冢的墓门虚掩着，一股陈年凉气扑面"},
        {"archetype": "ruins",      "name": "塔基的暗梯", "desc": "巫师塔废墟的地砖下藏着一道旋转暗梯"},
        {"archetype": "romance",    "name": "林间定情玉佩", "desc": "林间青苔下埋着半枚玉佩，刻着交颈鸳鸯"},
        {"archetype": "romance",    "name": "古市相思结", "desc": "古市摊上一只红绳相思结，无主却仍带余温"},
        {"archetype": "treasure",   "name": "遗迹宝匣", "desc": "石台上一只魔法宝匣，机关已失效"},
        {"archetype": "trap",       "name": "飞镖石像", "desc": "石像口中暗藏飞镖，需侧身闪避"},
        {"archetype": "trial",      "name": "断龙石", "desc": "一道断龙石封路，需合力撬动"},
        {"archetype": "scripture",  "name": "咒文石板", "desc": "半埋的石板刻满古老咒文"},
        {"archetype": "spring",     "name": "生命之泉", "desc": "精灵遗迹的生命之泉，泉水泛着微光"},
        {"archetype": "mystery",    "name": "星盘谜锁", "desc": "星盘错位，需对齐星辰方开"},
        {"archetype": "caravan",    "name": "驼队余货", "desc": "路过的驼队愿甩卖冗余辎重"},
        {"archetype": "hidden_npc", "name": "隐修女巫", "desc": "林中木屋的隐修女巫，欲收个学徒"},
        {"archetype": "tame",       "kind": "wilderness", "name": "林间幼鹿", "desc": "林间一只离群的幼鹿，怯怯地望着你"},
        {"archetype": "tame",       "kind": "settlement", "name": "马厩幼驹", "desc": "旅店马厩里一匹刚断奶的小马驹，正用湿漉漉的眼睛望着你"},
        {"archetype": "ruins",      "name": "荒塔地门", "desc": "荒废法师塔的地门，刻着符文"},
        {"archetype": "romance",    "name": "月桂银戒", "desc": "月桂树下埋着一枚银戒，内刻誓词"},
        {"archetype": "treasure",   "name": "浅滩沉船", "desc": "浅滩沉船的货舱里，木箱泡在水中仍未漏底"},
        {"archetype": "trap",       "name": "吹孔毒镖", "desc": "墙壁吹孔喷出毒镖，需举盾护身通过"},
        {"archetype": "trial",      "name": "挡路石像", "desc": "巨石像堵住神殿入口，需合力推动底座"},
        {"archetype": "scripture",  "name": "吟游诗篇", "desc": "吟游诗人的手抄诗篇，暗合一支失传战歌"},
        {"archetype": "spring",     "name": "苔藓圣池", "desc": "精灵圣林的苔藓池，浸之可愈合伤口"},
        {"archetype": "mystery",    "name": "壁画谜语", "desc": "神殿壁画的场景顺序，暗藏石门的开启顺序"},
        {"archetype": "caravan",    "kind": "settlement", "name": "收摊集市", "desc": "集市收摊的商贩，愿半价出清陶器与干货"},
        {"archetype": "hidden_npc", "name": "退休盗贼", "desc": "酒馆角落的退休老盗贼，愿意教你开锁手艺"},
        {"archetype": "ruins",      "name": "葡萄园墓窖", "desc": "葡萄园塌陷露出地下墓窖，石阶向下延伸"},
        {"archetype": "romance",    "name": "精灵耳坠", "desc": "当铺柜台深处一对精灵耳坠，据说戴上能听见彼此心跳"}
    ],
}


# ---- [P26a] 题材化定情信物名池（6 题材各 >=6，缺键回退西幻）----
# romance 奇遇检定通过时从中确定性取名，构造 Item(type="accessory", rarity="rare",
# is_courtship_gift=True) 入包。信物即奖励（豁免金币下限）。
_GENRE_COURTSHIP_GIFT_NAMES: dict[str, list[str]] = {
    "xianxia": ["鸳鸯玉佩", "同心结", "并蒂莲簪", "定情香囊", "双股钗", "灵犀玉", "龙凤对镯", "青丝结"],
    "wuxia": ["鸳鸯佩", "红绳结", "并蒂发簪", "定情穗子", "双玉环", "同心锁", "龙凤对佩", "青丝带"],
    "modern": ["情侣对戒", "定情项链", "情侣手链", "情侣钥匙扣", "心形吊坠", "对表", "情侣对表", "定制吊牌"],
    "scifi": ["全息誓言吊坠", "共振手环", "量子纠缠对饰", "数据信物芯片", "星图项链", "记忆晶体坠", "双星对坠", "神经链对戒"],
    "apocalypse": ["相框遗照", "刻字戒指", "幸存者手绳", "旧照片坠", "求生哨对链", "锈色对牌", "双生手环", "刻名子弹壳"],
    "western_fantasy": ["定情玉佩", "相思结", "并蒂莲簪", "心形吊坠", "双环戒指", "誓言项链", "双月对戒", "同心徽章"],
}


def _courtship_gift_names(genre_id: str) -> list[str]:
    pool = _GENRE_COURTSHIP_GIFT_NAMES.get(genre_id)
    return pool if pool else _GENRE_COURTSHIP_GIFT_NAMES["western_fantasy"]


def _give_courtship_gift(world: Any, rng: SeededRng) -> Optional[Any]:
    """[P26a] 生成一件定情信物入包（确定性 SeededRng 取名 + 构造 Item）。

    Item: type="accessory"（可正常赠送/上架），rarity="rare"，is_courtship_gift=True。
    确定性 id = f"courtship_{world.id}_{tick}"（守派生 id 确定性口径）。入包走
    try_add_to_inventory + codex；背包满返回 None（调用方降级小 xp）。
    """
    from src.models.world import Item
    from src.services import combat_engine as _ce
    ov = getattr(world, "config_overlay", None) or {}
    gid = str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy") if isinstance(ov, dict) else "western_fantasy"
    name = rng.pick(_courtship_gift_names(gid)) or "定情信物"
    tick = int(getattr(world, "tick_count", 0) or 0)
    base_id = f"courtship_{getattr(world, 'id', '')}_{tick}"
    # 同 tick 第二次命中派生序号防撞 id（撞 id 会被去重背包误拒 + world.items 出现重复 id）
    items = getattr(world, "items", None)
    gid_final = base_id
    _n = 1
    while isinstance(items, list) and any(getattr(x, "id", "") == gid_final for x in items):
        _n += 1
        gid_final = f"{base_id}x{_n}"
    it = Item(id=gid_final,
              name=name, rarity="rare", type="accessory", is_courtship_gift=True,
              desc="定情信物：送予交情>=90且已结义的同伴，可结为恋人")
    # [!] 先入包再进世界池：背包满直接返回，防「已 append 进 world.items 却没人持有」的
    # 孤儿物品（try_add_to_inventory 只查背包容量不查 world.items，可安全前置）
    if not _ce.try_add_to_inventory(world.player, it.id):
        return None   # 背包满
    if isinstance(items, list):
        items.append(it)
    codex = getattr(world.player, "codex_items", None)
    if isinstance(codex, list) and it.id not in codex:
        codex.append(it.id)
    return it


def _gold_mult(world) -> float:
    """[P14] 奇遇金币乘经济节奏系数（overlay economy_pace）。"""
    from src.services import trade_engine as _tre
    return _tre.gold_gain_mult(_tre.world_pace(world))


# ---- [P9 天赋奇遇获得] 机缘觉醒 ----
# [P16 用户指示] 天赋不能后天加点购买：开局选完即定，此后唯一获得渠道是奇遇概率觉醒。
# 只有机缘/传承/异变类原型（scripture 参悟 / hidden_npc 传承 / spring 异变）检定通过才可能觉醒；
# 概率 = 0.35 + luck*0.004 + danger*0.01（钳 0.05-0.60），高危 + 高幸运更易触发。
_AWAKEN_ARCHETYPES = ("scripture", "hidden_npc", "spring")


def _pick_awaken_talent(world, player, rng: SeededRng, danger: int):
    """从世界天赋池挑一个玩家未拥有的天赋（确定性；高危偏好高品级）。

    danger >= 7 时优先在 epic/legendary 子集里挑（没有才回退全池）；
    池空 / 已全拥有返回 None。题材兼容：talent_pool 由世界生成按题材出
    （LLM 优先 + te.fallback_talent_pool 六题材兜底，自定义题材回退西幻）。
    """
    pool = [t for t in (getattr(world, "talent_pool", None) or []) if isinstance(t, dict)]
    if not pool:
        return None
    owned = {str(t.get("name", "")) for t in (getattr(player, "talents", None) or [])
             if isinstance(t, dict)}
    cands = [t for t in pool if str(t.get("name", "")) not in owned]
    if not cands:
        return None
    if int(danger) >= 7:
        high = [t for t in cands if t.get("rarity") in ("epic", "legendary")]
        if high:
            cands = high
    return rng.pick(cands)



def _stat_value(player: Any, key: str) -> int:
    """安全读玩家基础五维与天赋加成（key: str/dex/int/vit/luk）。"""
    if not key:
        return 0
    from src.services import talent_engine as te
    return te.effective_stat(player, key)


def roll_hidden_check(rng: SeededRng, world: Any, difficulty: int,
                      base: float = 0.5, *, tool_bonus: int = 0,
                      actor: Any = None) -> bool:
    """[P33] 隐藏/被动察觉检定（纯 Python + SeededRng 确定性）。

    检定属性 = stat_int 与 stat_luk 取高（洞察 + 运气，属性特长>=15 现在对探索有意义），
    复用 success_chance 公式 + talent check_mult（敏锐直觉/街头智慧等检定加成，钳 0.95 上限）。
    调用方拿到 bool 自行决定隐藏内容是否揭示（秘境陷阱预发现/宝藏隐藏奖励/地点隐藏 NPC/线索）。
    actor 可指定实际参与的随行同伴，tool_bonus 复用普通工具档位；缺省仍是玩家徒手口径。
    [!] 纯函数无副作用；调用方传独立 rng seed 不破坏既有 rng 序列；difficulty 0-100；
    开关由调用方据 preset.hidden_check_enabled 预判（此函数不读开关）。
    """
    player = actor if actor is not None else getattr(world, "player", None)
    if player is None:
        return False
    # [饱食度 2026-09-06] 饥饿减半检定属性（与战斗/采集/合成同口径）
    from src.services.combat_engine import hunger_stat_mult as _hsm
    hunger_mult = _hsm(player) if player is getattr(world, "player", None) else 1.0  # NPC 饥饿只驱动行为
    sv = int(max(_stat_value(player, "int"), _stat_value(player, "luk")) * hunger_mult)
    chance = success_chance(sv, tool_bonus=tool_bonus, difficulty=difficulty, base=base,
                            global_penalty=difficulty_penalty_of(world))
    from src.services import talent_engine as _te
    chance = min(0.95, chance * _te.get_talent_mult(player, "check_mult"))
    tier = roll_dice_check(chance, rolls=[rng.random() for _ in range(3)]).tier
    return tier in (TIER_CRIT, TIER_OK)


def _location_danger(loc: Any) -> int:
    """安全读地点 danger（1-10）。"""
    try:
        return max(1, min(10, int(getattr(loc, "danger", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def roll_encounter_trigger(player: Any, loc: Any, rng: SeededRng, mode: str = "auto") -> bool:
    """据 luck + danger roll 是否触发奇遇。

    mode="auto"（首次进入，每地点一次）：base 0.18 + luck*0.005 + danger*0.01
    mode="probe"（手动探测，可重复）：base 0.10 + luck*0.004 + danger*0.008
    [P8 调参] auto 基础率从 0.12 提到 0.18（据体验：3 地点常 0 命中，作为获取渠道需更可见）。
    """
    luck = _stat_value(player, "luk")
    danger = _location_danger(loc)
    if mode == "probe":
        chance = 0.10 + luck * 0.004 + danger * 0.008
    else:
        chance = 0.18 + luck * 0.005 + danger * 0.01
    # 高 danger 地点封顶高一点（险地机缘多）
    chance = max(0.02, min(0.6, chance))
    return rng.chance(chance)


def roll_rarity_tier(rng: SeededRng, base_tier: str, luck: int, danger: int) -> str:
    """据 luck + danger roll 稀有度升档（复用于掉落/采集/合成大成功）。

    升档概率 = clamp(luck*0.008 + danger*0.01, 0, 0.5)，命中则上一档，可连续升。
    base_tier 不在档位序列则原样返回。
    """
    if base_tier not in _RARITY_TIERS:
        return base_tier
    idx = _RARITY_TIERS.index(base_tier)
    upgrade_chance = max(0.0, min(0.5, luck * 0.008 + danger * 0.01))
    while idx < len(_RARITY_TIERS) - 1 and rng.chance(upgrade_chance):
        idx += 1
    return _RARITY_TIERS[idx]


def _pick_reward_item(world: Any, rng: SeededRng, target_rarity: str) -> Optional[Any]:
    """从 world.items 池挑一件目标稀有度物品（无精确档则取最接近的较低档）。

    优先 target_rarity；缺失则向下逐档找；全无则返回 None。
    只排除 key 类（任务信物不作奇遇奖励）。[用户指示 2026-09-06] 不排除 NPC 随身/
    货架/玩家已有物品——Item 是蓝图目录，多实体同 id 引用是既定设计（同 A1 初始
    装备/掉落 loot_table 口径，共享蓝图耐久）；NPC 身上的宝物能被奇遇「撞见」合理。
    """
    items = list(getattr(world, "items", []) or [])
    pool = [it for it in items if getattr(it, "type", "") != "key"]
    if not pool:
        return None
    by_rarity: dict[str, list] = {r: [] for r in _RARITY_TIERS}
    for it in pool:
        r = getattr(it, "rarity", "common") or "common"
        if r in by_rarity:
            by_rarity[r].append(it)
    if target_rarity not in _RARITY_TIERS:
        target_rarity = "common"
    idx = _RARITY_TIERS.index(target_rarity)
    # 从目标档向下找第一档有库存的
    while idx >= 0:
        if by_rarity[_RARITY_TIERS[idx]]:
            return rng.pick(by_rarity[_RARITY_TIERS[idx]])
        idx -= 1
    # 全空（理论上不会，pool 已过滤），兜底返回任一
    return rng.pick(pool)


def _filter_templates_by_kind(tmpls: list, loc_kind: str) -> list:
    """[P] 按地点类型过滤奇遇模板：settlement/wilderness 专属模板只在对应地点保留。

    无 kind 字段 = "any"（中性模板任意地点可出）。loc_kind 非 settlement/wilderness 原样返回。
    过滤后为空（理论上不会：每个题材池都有大量 any 模板）则回退全池防空选。
    """
    if loc_kind not in ("settlement", "wilderness"):
        return tmpls
    filtered = [t for t in tmpls if t.get("kind", "any") in ("any", loc_kind)]
    return filtered or tmpls


def gen_encounter(world: Any, loc: Any, player: Any, rng: SeededRng, genre_id: str = "",
                  templates: Optional[list] = None) -> dict:
    """抽取一个奇遇（题材化原型 + 稀有度档）。

    templates 非空时用它替代题材主池（[P24b] 节庆活动池走此参数，原型走

    返回 dict（运行时用，不持久化）：
        {archetype, name, desc, rarity, luck, danger, check_stat, check_base, risk}
    纯抽取不改 player/world，结算由 resolve_encounter 完成。
    """
    if templates:
        tmpls = templates
    else:
        tmpls = _GENRE_ENCOUNTER_TEMPLATES.get(genre_id) or _GENRE_ENCOUNTER_TEMPLATES["western_fantasy"]
        # [P] 按地点类型过滤：settlement/wilderness 专属模板只在对应地点抽。
        # 防「市集弃犬」这类聚落专属奇遇在野外蹦出（驯服/商队等题材模板有明确聚落/野外意象）。
        tmpls = _filter_templates_by_kind(tmpls, getattr(loc, "kind", "") or "")
    tmpl = rng.pick(tmpls) or _GENRE_ENCOUNTER_TEMPLATES["western_fantasy"][0]
    archetype = tmpl.get("archetype", "treasure")
    cfg = dict(_ARCHETYPE_CONFIG.get(archetype, _ARCHETYPE_CONFIG["treasure"]))
    # [P24b] 模板级检定参数覆盖（节庆活动池逐条自带；主池模板无这些键不受影响）
    for k in ("check_stat", "check_base", "risk", "base_tier"):
        if k in tmpl:
            cfg[k] = tmpl[k]
    luck = _stat_value(player, "luk")
    danger = _location_danger(loc)
    rarity = roll_rarity_tier(rng, cfg["base_tier"], luck, danger)
    return {
        "archetype": archetype,
        "name": tmpl.get("name", "奇遇"),
        "desc": tmpl.get("desc", ""),
        "rarity": rarity,
        "luck": luck,
        "danger": danger,
        "check_stat": cfg["check_stat"],
        "check_base": cfg["check_base"],
        "risk": cfg["risk"],
    }


def resolve_encounter(encounter: dict, world: Any, player: Any, rng: SeededRng,
                      dice: Optional[DiceRoll] = None) -> dict:
    """结算奇遇：属性检定 + 奖励发放（原地改 player.inventory/gold/hp + codex）。

    返回 summary：
        {triggered:True, archetype, name, rarity, check_passed:bool|None,
         items:[item_id], item_names:[str], gold:int, xp:int, skill_book:bool,
         damage:int, healed:int, narration_hint:str, world_changed:bool}

    奖励物品优先 target_rarity；技能书（scripture/hidden_npc 通过）从背包里 skill-book 类挑
    或从世界物品池挑 teach_skill 非空的；金币/经验据 rarity + danger 缩放。
    """
    archetype = encounter.get("archetype", "treasure")
    rarity = encounter.get("rarity", "uncommon")
    danger = int(encounter.get("danger", 1))
    check_stat = encounter.get("check_stat", "")
    check_base = float(encounter.get("check_base", 1.0))
    risk = float(encounter.get("risk", 0.0))
    name = encounter.get("name", "奇遇")

    rarity_mult = _RARITY_MULT.get(rarity, 1.0)

    # 属性检定（check_stat 空则必过）
    check_passed: Optional[bool] = None
    check_crit: bool = False
    if check_stat:
        sv = _stat_value(player, check_stat)
        chance = success_chance(sv, tool_bonus=0, difficulty=danger * 5, base=check_base,
                                global_penalty=difficulty_penalty_of(world))
        # [P9] 天赋 check_mult（敏锐直觉/街头智慧等检定加成，钳制 0.95 上限）
        from src.services import talent_engine as _te
        chance = min(0.95, chance * _te.get_talent_mult(player, "check_mult"))
        tier = dice.tier if dice is not None else \
            roll_dice_check(chance, rolls=[rng.random() for _ in range(3)]).tier
        check_passed = tier in (TIER_CRIT, TIER_OK)
        check_crit = (tier == TIER_CRIT)

    items: list[str] = []
    item_names: list[str] = []
    gold = 0
    xp = 0
    skill_book = False
    # [P9] 天赋剧情联动：玩家天赋元素集（奇遇技能书偏好 + 共鸣提示用；确定性无 roll）
    prefer_elements: list[str] = []
    for t in (getattr(player, "talents", None) or []):
        if isinstance(t, dict):
            el = str(t.get("element", "") or "")
            if el and el != "physical" and el not in prefer_elements:
                prefer_elements.append(el)
    skill_book_resonance = False
    damage = 0
    healed = 0
    # [P24d] tame 分支专用（其余原型保持空串/None）
    tamed = ""
    tame_line = ""
    # [P25a] ruins 分支专用（同上）
    ruins_line = ""
    # [P26a] romance 分支专用（定情信物奇遇）
    romance_line = ""

    def _award_item(target_rarity: str):
        """挑一件目标稀有度物品入背包（去重），记录名。"""
        it = _pick_reward_item(world, rng, target_rarity)
        if it is None:
            return
        iid = getattr(it, "id", "")
        if not iid:
            return
        inv = getattr(player, "inventory", None)
        # [P8] 负重检查：物品种类数超 carry_capacity（=20+vit*2）则拒收（vit 驱动）
        if isinstance(inv, list):
            if iid in inv:
                pass  # 已有
            elif len(inv) >= carry_capacity(player):
                return  # 超载拒收
            else:
                inv.append(iid)
        items.append(iid)
        item_names.append(getattr(it, "name", iid))
        # 图鉴：曾拥有
        try:
            codex = getattr(player, "codex_items", None)
            if isinstance(codex, list) and iid not in codex:
                codex.append(iid)
        except Exception:
            pass

    def _award_skill_book(prefer: Optional[list] = None) -> bool:
        """挑一本技能书入背包（带 carry_capacity 检查 + 去重 + codex）。返回是否真发放。

        [P9] prefer 非空时优先挑同元素技能书（雷天灵根参悟玉简 -> 雷系功法书）；
        命中偏好且真发放（入包/已有）才置 skill_book_resonance（narration_hint 播报共鸣，
        背包满载拒收时不播报防「说共鸣却没给书」）。无偏好书则全池挑。
        """
        nonlocal skill_book_resonance
        sb, matched = _pick_skill_book(world, rng, prefer)
        if sb is None:
            return False
        iid = getattr(sb, "id", "")
        if not iid:
            return False
        inv = getattr(player, "inventory", None)
        if isinstance(inv, list):
            if iid in inv:
                pass  # 已有（去重，仍记旁白）
            elif len(inv) >= carry_capacity(player):
                return False  # 超载拒收
            else:
                inv.append(iid)
        items.append(iid)
        item_names.append(getattr(sb, "name", iid))
        # [P9] 命中元素偏好且真发放 -> 共鸣标记（迟到置位，防满载拒收误播报）
        if matched:
            skill_book_resonance = True
        try:
            codex = getattr(player, "codex_items", None)
            if isinstance(codex, list) and iid not in codex:
                codex.append(iid)
        except Exception:
            pass
        return True

    # ---- 各原型结算 ----
    # [数值补全] 所有金币分支统一乘 _gold_mult(world)（economy_pace 金币产出系数）。
    # 旧版只 treasure/兜底乘了，trap/trial/scripture/spring/mystery/caravan/hidden_npc 漏乘，
    # 致 hard/hardcore 档「砍金币产出」对这些原型形同虚设。统一后节奏开关全原型生效。
    if archetype == "treasure":
        # 直接奖励
        _award_item(rarity)
        gold = int((20 + danger * 8) * rarity_mult * _gold_mult(world))
        xp = int((15 + danger * 6) * rarity_mult)

    elif archetype == "trap":
        # dex 检定；失败受 risk 伤但仍得部分奖励
        if check_passed:
            _award_item(rarity)
            gold = int((15 + danger * 6) * rarity_mult * _gold_mult(world))
            xp = int((12 + danger * 5) * rarity_mult)
        else:
            hp_max = max(1, max_hp_for(player))
            damage = max(1, int(hp_max * risk))
            _award_item("common")  # 失败仍捡到点东西
            gold = int((8 + danger * 4) * rarity_mult * _gold_mult(world))
            xp = int((8 + danger * 3) * rarity_mult)

    elif archetype == "trial":
        # str 检定；通过大奖，失败重伤
        if check_passed:
            _award_item(rarity)
            _award_item("uncommon")
            gold = int((30 + danger * 10) * rarity_mult * _gold_mult(world))
            xp = int((25 + danger * 8) * rarity_mult)
        else:
            hp_max = max(1, max_hp_for(player))
            damage = max(1, int(hp_max * risk))
            gold = int((10 + danger * 4) * rarity_mult * _gold_mult(world))
            xp = int((15 + danger * 5) * rarity_mult)

    elif archetype == "scripture":
        # int 检定；通过习得技能书/配方，失败只得 xp
        if check_passed:
            # 优先发技能书（带负重检查 + 元素天赋偏好），无技能书则发稀有物
            if _award_skill_book(prefer_elements):
                skill_book = True
            else:
                _award_item(rarity)
            xp = int((25 + danger * 8) * rarity_mult)
            gold = int((10 + danger * 4) * rarity_mult * _gold_mult(world))
        else:
            xp = int((12 + danger * 4) * rarity_mult)

    elif archetype == "spring":
        # vit 抗性检定；回血 + 失败时异变副作用（净伤害 = side - healed，由末尾统一扣血）
        heal_amt = int((15 + danger * 5) * rarity_mult)
        hp_max = max(1, max_hp_for(player))
        cur_hp = int(getattr(player, "hp", 0) or 0)
        new_hp = min(hp_max, cur_hp + heal_amt)
        healed = new_hp - cur_hp
        try:
            player.hp = new_hp      # 先回血
        except (AttributeError, TypeError):
            pass
        _award_item("uncommon")
        if not check_passed and risk > 0:
            # 抗性失败：异变副作用。damage 只算数值，不在此扣血（末尾统一 if damage>0 扣，
            # 读 player.hp 此时是已回血后的值，自然实现「先回血再受副作用」语义，避免双重扣血）
            side = max(1, int(hp_max * risk))
            damage = max(0, side - healed)
        xp = int((12 + danger * 4) * rarity_mult)
        gold = int((12 + danger * 4) * rarity_mult * _gold_mult(world))

    elif archetype == "mystery":
        # int 检定；通过得稀有物品，失败空手
        if check_passed:
            _award_item(rarity)
            _award_item("uncommon")
            gold = int((25 + danger * 8) * rarity_mult * _gold_mult(world))
            xp = int((20 + danger * 6) * rarity_mult)
        else:
            xp = int((8 + danger * 3) * rarity_mult)

    elif archetype == "caravan":
        # 无检定；金币 + luk 影响捡漏稀有物概率
        _award_item("uncommon")
        luck = _stat_value(player, "luk")
        if rng.chance(max(0.0, min(0.6, 0.1 + luck * 0.01))):
            _award_item(rarity)
        gold = int((25 + danger * 8) * rarity_mult * _gold_mult(world))
        xp = int((10 + danger * 3) * rarity_mult)

    elif archetype == "hidden_npc":
        # int 检定；通过传承大奖（技能书 + 稀有物），失败得忠告（小 xp）
        if check_passed:
            if _award_skill_book(prefer_elements):
                skill_book = True
            _award_item(rarity)
            gold = int((30 + danger * 10) * rarity_mult * _gold_mult(world))
            xp = int((30 + danger * 10) * rarity_mult)
        else:
            xp = int((15 + danger * 5) * rarity_mult)

    elif archetype == "tame":
        # [P24d] 驯服奇遇：宠物即奖励（无物品/金币）。dex/luk 检定 + 自动投食在
        # pet_engine.tame_check 专项做（模板级 check_stat 留空，check_passed 保持 None
        # 防末尾 hint 输出空属性检定行）。满员（PET_MAX）不出检定，幼崽自行离去。
        from src.services import pet_engine as _pex
        if _pex.pets_full(world):
            tame_line = "随行宠物已满，只能目送幼崽回到林间"
            xp = int((10 + danger * 3) * rarity_mult)
        else:
            species = _pex.roll_species(world, rng)
            res = _pex.tame_check(world, player, species, danger, rng)
            fed_line = f"以「{res['fed_item']}」为饵" if res["fed_item"] else ""
            if res["success"]:
                pet = _pex.create_pet(world, species)
                tamed = pet.name
                tame_line = f"驯服了「{pet.name}」入队" + (f"（{fed_line}）" if fed_line else "")
                xp = int((20 + danger * 6) * rarity_mult)
            else:
                tame_line = "幼崽警觉离去，未能驯服" + (f"（{fed_line}，被它叼走了）" if fed_line else "")
                xp = int((10 + danger * 3) * rarity_mult)

    elif archetype == "ruins":
        # [P25a] 秘境入口发现：入口即奖励（无物品/金币）。挂在玩家当前野外地点
        # （每地点限一个）；已有限/在聚落/在秘境内部 -> 只得线索（小 xp）。
        from src.services import dungeon_engine as _dge
        loc = next((l for l in (getattr(world, "locations", None) or [])
                    if getattr(l, "id", "") == getattr(player, "location_id", "")), None)
        if loc is None or getattr(loc, "kind", "") != "wilderness" or _dge.dungeon_at(world, loc) is not None:
            ruins_line = "隐约察觉到远方还有秘境的踪迹，但此地并无新的入口"
            xp = int((10 + danger * 3) * rarity_mult)
        else:
            dg_new = _dge.build_dungeon(world, loc.id, max(1, int(getattr(loc, "danger", 1) or 1)),
                                        rng=rng)
            ruins_line = f"发现了秘境「{dg_new.name}」的入口（{getattr(loc, 'name', '此地')}，可随时前来探索）"
            xp = int((15 + danger * 5) * rarity_mult)

    elif archetype == "romance":
        # [P26a] 定情信物奇遇：幸运检定通过 -> 发一件 is_courtship_gift 信物入包
        # （信物即奖励，豁免金币下限，仿 tame/ruins）；失败小 xp + 提示。
        if check_passed is not False:
            gift_it = _give_courtship_gift(world, rng)
            if gift_it is not None:
                romance_line = f"寻得定情信物「{gift_it.name}」（送予交情>=90且已结义的同伴可结为恋人）"
                xp = int((20 + danger * 6) * rarity_mult)
                items.append(gift_it.id)
                item_names.append(gift_it.name)
            else:
                romance_line = "寻得一件信物，奈何行囊已满，只能怅然离去"
                xp = int((10 + danger * 3) * rarity_mult)
        else:
            romance_line = "缘分未到，信物终是擦肩而过"
            xp = int((10 + danger * 3) * rarity_mult)

    else:  # 兜底当 treasure
        _award_item(rarity)
        gold = int((20 + danger * 8) * rarity_mult * _gold_mult(world))
        xp = int((15 + danger * 6) * rarity_mult)

    # ---- [P9] 天赋机缘觉醒（后天唯一获得渠道，概率触发；详见 _AWAKEN_ARCHETYPES 注释）----
    talent_awakened = ""
    if archetype in _AWAKEN_ARCHETYPES and check_passed is not False:
        from src.services import talent_engine as _te
        luck = _stat_value(player, "luk")
        chance = max(0.05, min(0.60, 0.35 + luck * 0.004 + danger * 0.01))
        if rng.chance(chance):
            pick = _pick_awaken_talent(world, player, rng, danger)
            if pick is not None:
                from src.models.world import Talent as _T
                clean = _T.from_dict(pick).to_dict()
                tl = getattr(player, "talents", None)
                if isinstance(tl, list):
                    tl.append(clean)
                    from src.services import combat_engine as _ce
                    _ce.sync_player_talent_resources(player)
                    talent_awakened = str(clean.get("name", ""))
                    line = _te.describe_talent(clean)
                    xp += 10   # 觉醒附带少量经验（身体/心性的蜕变）

    # [P8 体验反馈] 奇遇金币下限 max(gold, danger*5)：scripture/mystery 失败、spring 等
    # 分支原本 0 金币，奖励体感单薄；下限让高危奇遇即使检定失败也有保底收入。
    # [P24d] tame / [P25a] ruins 豁免：奖励是宠物/秘境入口本身，捡到金币反而出戏。
    if archetype not in ("tame", "ruins", "romance"):
        # [审核修复 2026-09-13] 下限也须乘 economy_pace：原写法在 hard/hardcore（0.7/0.5）
        # 下用未打折的 danger*5 当地板，等于让节奏系数对低额分支彻底失效。
        gold = max(gold, int(danger * 5 * _gold_mult(world)))

    # [P24d] 宠物寻宝被动：出战宠物亲和达标且种族被动为 treasure 时奇遇金币 x1.15。
    from src.services import pet_engine as _pete
    if gold > 0 and _pete.passive_active(world, "treasure"):
        gold = int(gold * 1.15)

    # [三骰取二] 奇遇大成功（检定 crit）：成功且额外奖励（金币 x1.5、经验 x1.2）
    if check_crit and check_passed is True:
        gold = int(gold * 1.5)
        xp = int(xp * 1.2)

    # 结算伤害（统一在最后扣，避免上面分支重复处理 hp 已扣的情况混乱）
    if damage > 0:
        cur_hp = int(getattr(player, "hp", 0) or 0)
        try:
            player.hp = max(0, cur_hp - damage)
        except (AttributeError, TypeError):
            pass

    # 发金币/经验
    try:
        player.gold = int(getattr(player, "gold", 0) or 0) + gold
    except (AttributeError, TypeError):
        pass

    # 叙事提示（LLM 据此 + archetype/desc 生成完整旁白）
    hint_parts = [f"触发奇遇【{name}】"]
    if tame_line:
        hint_parts.append(tame_line)
    if ruins_line:
        hint_parts.append(ruins_line)
    if romance_line:
        hint_parts.append(romance_line)
    if check_passed is True:
        hint_parts.append(f"{check_stat.upper()}检定通过")
    elif check_passed is False:
        hint_parts.append(f"{check_stat.upper()}检定失败")
    if item_names:
        hint_parts.append(f"获得：{'、'.join(item_names)}")
    if skill_book:
        hint_parts.append("习得传承")
    if skill_book_resonance:
        hint_parts.append("传承与你身怀的元素天赋共鸣，正合所长（天赋与奇遇联动）")
    if talent_awakened:
        hint_parts.append(f"机缘造化，觉醒天赋「{talent_awakened}」（此为后天获得天赋的唯一途径）")
    if gold:
        from src.models.world_sim_preset import GenreText as _GT
        hint_parts.append(f"获得 {gold} {_GT(getattr(world, 'config_overlay', None) or {}).currency}")
    if xp:
        hint_parts.append(f"获得 {xp} 经验")
    if damage:
        hint_parts.append(f"受到 {damage} 点伤害")
    if healed:
        hint_parts.append(f"回复 {healed} 点生命")

    return {
        "triggered": True,
        "archetype": archetype,
        "name": name,
        "desc": encounter.get("desc", ""),
        "rarity": rarity,
        "check_stat": check_stat,
        "check_passed": check_passed,
        "items": items,
        "item_names": item_names,
        "gold": gold,
        "xp": xp,
        "skill_book": skill_book,
        "skill_book_resonance": skill_book_resonance,
        "talent_awakened": talent_awakened,
        "tamed": tamed,
        "ruins_found": bool(ruins_line and "发现了秘境" in ruins_line),
        "damage": damage,
        "healed": healed,
        "narration_hint": "；".join(hint_parts),
        "world_changed": True,
    }


def _pick_skill_book(world: Any, rng: SeededRng,
                     prefer_elements: Optional[list] = None) -> tuple[Optional[Any], bool]:
    """从 world.items 挑一件 teach_skill 非空的技能书；无则返回 (None, False)。

    [P9] prefer_elements 非空时优先挑同元素技能书（命中返回 matched=True；偏好池空回退全池
    且 matched=False）。确定性（rng.pick），无偏好时行为与旧版一致。
    """
    items = list(getattr(world, "items", []) or [])
    books = [it for it in items if getattr(it, "teach_skill", None)]
    if not books:
        return None, False
    if prefer_elements:
        prefer = [it for it in books
                  if str((it.teach_skill or {}).get("element", "") or "") in prefer_elements]
        if prefer:
            return rng.pick(prefer), True
    return rng.pick(books), False
