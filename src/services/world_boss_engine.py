"""[P25d] 世界 Boss 引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

架构分层（守 §21）：LLM 出语义（crisis/major 事件由 tick_faction_war/tick_economy 产），
引擎算结构——多形态递进（2-3 形态，每形态独立战斗）/ 形态名/技能/AI/血量倍率全
SeededRng 确定性（同 world+boss 同结果）/ 击杀奖励（声望变动 + major/crisis event +
保底 legendary 掉落，无成就钩子守上次定稿）。

讨伐走按钮 -> CombatDialog（skip_judge 预设战斗，喂入 Boss 临时单位 + 确定性小弟 +
声望援护）；形态间玩家回到场景可休整再战（current_form_index 随档落盘续战）。
Boss 临时单位不入 world.npcs（守图鉴口径，仿秘境）；击杀钩子 on_combat_victory 由
finish_combat 调（非终形态 -> index+1 + 变身 hint；终形态 -> defeated + 奖励）。
窗口到期未击败 -> 离去留传闻（不强行终结，守无限世界）。
题材池容量守 §23 数据量铁律（tests/test_genre_data_volume.py TestWorldBossPool 守护）。
"""
from __future__ import annotations

from typing import Any, Optional

from src.models.world import Location, NPC, WorldBoss, BossForm, WorldEvent
from src.models.world import _clean_elements
from src.models.world_sim_preset import GenreText
from src.services import combat_engine as ce
from src.services import encounter_engine as ene
from src.services import wilderness_engine as we
from src.utils.rng import SeededRng

# ---- 数值旋钮（纯引擎常量；窗口/最低危险度默认值由 preset 覆盖）----
MONSTER_LEVEL_CAP = 100           # [P43] 危险度分段后放开
BOSS_HP_BASE_MULT = 2.0           # Boss 双倍血底（仿 dungeon boss）
KILL_FACTION_PENALTY = -15        # 击杀：Boss 关联势力声望降幅（钳 -100..100）
KILL_ENEMY_GAIN = 8               # 击杀：Boss 敌对势力(relations<=-30)声望增幅
ENEMY_RELATION_THRESHOLD = -30    # 与击杀声望 -10/+5 同口径（world_sim_service.py:3519）
GOLD_BASE = 50                    # 击杀金币基数 = (50 + danger*20) * economy_pace

# ---- 形态血量倍率档（按 index 递增；多于 3 形态时循环末档）----
_FORM_HP_MULTS = (1.0, 1.4, 1.8)
# ---- 形态技能威力基数（+ boss.level 得最终 power）----
_FORM_SKILL_POWER = (10, 12, 14)
# ---- 形态随从数（按形态递减：首形态 2，中形态 1，末形态 0）----
_FORM_MINIONS = (2, 1, 0)


# ---- 题材化世界 Boss 概念池（6 题材各 >=6，缺键回退西幻）----
# 每条 {id, name, desc, forms: [{name, ai, skill_name}]}：forms 数 2-3 由生成期按 danger
# 截取（danger>=9 取 3 形态）；ai 白名单 aggressive/defensive/caster/balanced；形态名题材化。
_GENRE_WORLD_BOSS_CONCEPTS: dict[str, list[dict]] = {
    "xianxia": [
        {"id": "xx_demon_lord", "name": "九幽魔尊", "desc": "坠入魔道的上古修士，怨念凝成实体盘踞灵脉",
         "forms": [{"name": "魔影", "ai": "aggressive", "skill_name": "噬魂魔爪"},
                   {"name": "魔相", "ai": "caster", "skill_name": "九幽魔火"},
                   {"name": "魔尊本相", "ai": "aggressive", "skill_name": "万魔归宗"}]},
        {"id": "xx_blood_puppet", "name": "血煞傀儡", "desc": "魔修以万人精血炼成的傀儡，嗜血无智",
         "forms": [{"name": "雏形", "ai": "balanced", "skill_name": "血煞冲击"},
                   {"name": "凶体", "ai": "aggressive", "skill_name": "血河滔天"},
                   {"name": "血煞大成", "ai": "aggressive", "skill_name": "万血归一"}]},
        {"id": "xx_beast_sovereign", "name": "蛮荒兽王", "desc": "远古大妖苏醒，统御山林万兽",
         "forms": [{"name": "幼体", "ai": "defensive", "skill_name": "兽王咆哮"},
                   {"name": "成体", "ai": "aggressive", "skill_name": "裂地撕咬"},
                   {"name": "化神兽王", "ai": "aggressive", "skill_name": "吞天噬地"}]},
        {"id": "xx_corpse_general", "name": "尸煞将军", "desc": "战场亡灵聚成的尸煞，率领阴兵横行",
         "forms": [{"name": "尸将", "ai": "balanced", "skill_name": "阴兵列阵"},
                   {"name": "尸王", "ai": "aggressive", "skill_name": "万尸噬魂"},
                   {"name": "尸煞至尊", "ai": "aggressive", "skill_name": "幽冥尸海"}]},
        {"id": "xx_pill_calaming", "name": "丹毒凶灵", "desc": "废弃丹炉中炼废的药灵成精，剧毒蚀骨",
         "forms": [{"name": "药雾", "ai": "caster", "skill_name": "丹毒蚀骨"},
                   {"name": "药灵", "ai": "caster", "skill_name": "百毒攻心"},
                   {"name": "丹煞", "ai": "caster", "skill_name": "万毒归宗"}]},
        {"id": "xx_void_thing", "name": "虚空异种", "desc": "自破碎虚空坠入此界的怪物，形貌难名",
         "forms": [{"name": "虚影", "ai": "caster", "skill_name": "虚空裂隙"},
                   {"name": "实体", "ai": "aggressive", "skill_name": "吞噬光线"},
                   {"name": "虚空主宰", "ai": "aggressive", "skill_name": "湮灭之潮"}]},
        {"id": "xx_thunder_apostle", "name": "雷劫使者", "desc": "渡劫失败被天雷淬体的散修，周身雷弧终年不散",
         "forms": [{"name": "雷身", "ai": "defensive", "skill_name": "雷弧护体"},
                   {"name": "雷威", "ai": "aggressive", "skill_name": "天雷引"},
                   {"name": "雷劫本相", "ai": "aggressive", "skill_name": "万雷天罚"}]},
        {"id": "xx_gu_matriarch", "name": "万蛊之母", "desc": "苗疆蛊池深处孕育的蛊母，蛊虫随念而动",
         "forms": [{"name": "蛊雾", "ai": "caster", "skill_name": "蛊雾蚀心"},
                   {"name": "蛊潮", "ai": "aggressive", "skill_name": "万蛊噬体"},
                   {"name": "蛊母真身", "ai": "aggressive", "skill_name": "天蛊降世"}]},
        {"id": "xx_moon_devourer", "name": "吞月妖蟾", "desc": "吞过一轮月华的巨蟾，妖丹中映着残月",
         "forms": [{"name": "蟾影", "ai": "balanced", "skill_name": "月华激浪"},
                   {"name": "妖蟾", "ai": "aggressive", "skill_name": "吞月吐息"},
                   {"name": "月魔蟾", "ai": "aggressive", "skill_name": "蚀月妖光"}]},
    ],
    "wuxia": [
        {"id": "wx_bandit_king", "name": "黑风大当家", "desc": "啸聚山林的悍匪首领，刀法狠辣横行乡里",
         "forms": [{"name": "守寨", "ai": "defensive", "skill_name": "横刀立马"},
                   {"name": "下山", "ai": "aggressive", "skill_name": "黑风斩"}]},
        {"id": "wx_demon_blade", "name": "魔刀客", "desc": "走火入魔的刀客，刀意失控见人就杀",
         "forms": [{"name": "狂态", "ai": "aggressive", "skill_name": "魔刀乱舞"},
                   {"name": "魔化", "ai": "aggressive", "skill_name": "一刀断魂"},
                   {"name": "魔刀大成", "ai": "aggressive", "skill_name": "天魔乱舞"}]},
        {"id": "wx_poison_matriarch", "name": "五毒老祖", "desc": "隐居苗疆的用毒宗师，控蛊驱毒无人能近",
         "forms": [{"name": "蛊阵", "ai": "caster", "skill_name": "万蛊噬心"},
                   {"name": "毒相", "ai": "caster", "skill_name": "五毒归元"},
                   {"name": "毒尊本相", "ai": "caster", "skill_name": "天毒覆地"}]},
        {"id": "wx_rebel_general", "name": "叛军首领", "desc": "举旗造反的旧部将军，统兵剽掠州县",
         "forms": [{"name": "前军", "ai": "defensive", "skill_name": "军阵如山"},
                   {"name": "亲临", "ai": "aggressive", "skill_name": "横扫千军"},
                   {"name": "背水一战", "ai": "aggressive", "skill_name": "困兽之斗"}]},
        {"id": "wx_ghost_palm", "name": "幽冥掌门", "desc": "邪派掌门，修幽冥掌法专吸人内力",
         "forms": [{"name": "试探", "ai": "balanced", "skill_name": "幽冥探掌"},
                   {"name": "出手", "ai": "aggressive", "skill_name": "幽冥吸星"},
                   {"name": "掌门全力", "ai": "aggressive", "skill_name": "幽冥夺魄"}]},
        {"id": "wx_cursed_sword", "name": "凶剑之灵", "desc": "上古凶剑所化剑灵，嗜杀成性催主行凶",
         "forms": [{"name": "剑气", "ai": "caster", "skill_name": "凶剑剑气"},
                   {"name": "剑灵", "ai": "aggressive", "skill_name": "一剑封喉"},
                   {"name": "剑灵苏醒", "ai": "aggressive", "skill_name": "万剑诛天"}]},
        {"id": "wx_jade_faced", "name": "玉面郎君", "desc": "白日是世家公子、夜里是黑道首脑的双面人物",
         "forms": [{"name": "道貌", "ai": "defensive", "skill_name": "折扇格挡"},
                   {"name": "现形", "ai": "aggressive", "skill_name": "袖里乾坤"},
                   {"name": "郎君真面", "ai": "aggressive", "skill_name": "玉面修罗"}]},
        {"id": "wx_iron_monk", "name": "铁头陀", "desc": "横练金钟罩走火入魔的头陀，周身刀枪不入",
         "forms": [{"name": "罡气", "ai": "defensive", "skill_name": "金钟罩"},
                   {"name": "狂禅", "ai": "aggressive", "skill_name": "铁头撞钟"},
                   {"name": "罡破天魔", "ai": "aggressive", "skill_name": "魔罡碎岳"}]},
        {"id": "wx_horse_thief", "name": "马匪大杆子", "desc": "纵马千里连破三镇的马匪头子，官府悬赏千金",
         "forms": [{"name": "游骑", "ai": "balanced", "skill_name": "马上开弓"},
                   {"name": "冲阵", "ai": "aggressive", "skill_name": "马踏连营"},
                   {"name": "悍匪末日", "ai": "aggressive", "skill_name": "焚营决死"}]},
    ],
    "modern": [
        {"id": "md_mutant_alpha", "name": "变异体首领", "desc": "生化泄漏后变异的怪物头目，统率变异群落",
         "forms": [{"name": "初变", "ai": "balanced", "skill_name": "变异触手"},
                   {"name": "异化", "ai": "aggressive", "skill_name": "腐蚀酸液"},
                   {"name": "完全体", "ai": "aggressive", "skill_name": "基因风暴"}]},
        {"id": "md_rogue_drone", "name": "失控无人机群", "desc": "军用无人机集群失控，自主锁定人类为敌",
         "forms": [{"name": "侦察群", "ai": "defensive", "skill_name": "激光照射"},
                   {"name": "攻击群", "ai": "aggressive", "skill_name": "集束扫射"},
                   {"name": "全群压境", "ai": "aggressive", "skill_name": "饱和轰炸"}]},
        {"id": "md_bio_weapon", "name": "生化兵器", "desc": "地下实验室泄漏的生化兵器，无智嗜杀",
         "forms": [{"name": "苏醒", "ai": "balanced", "skill_name": "利爪撕裂"},
                   {"name": "狂暴", "ai": "aggressive", "skill_name": "暴虐冲撞"},
                   {"name": "极限狂化", "ai": "aggressive", "skill_name": "毁灭冲撞"}]},
        {"id": "md_cult_leader", "name": "邪教教主", "desc": "末世教派首领，以信徒献祭换取异能",
         "forms": [{"name": "传教", "ai": "caster", "skill_name": "洗脑低语"},
                   {"name": "作法", "ai": "caster", "skill_name": "血祭降神"},
                   {"name": "神降", "ai": "aggressive", "skill_name": "邪神附体"}]},
        {"id": "md_ai_core", "name": "暴走 AI 核心", "desc": "失控的超级 AI，控制机器人卫队清洗「威胁」",
         "forms": [{"name": "守卫", "ai": "defensive", "skill_name": "电子干扰"},
                   {"name": "反击", "ai": "aggressive", "skill_name": "机械军团"},
                   {"name": "全面接管", "ai": "caster", "skill_name": "系统覆写"}]},
        {"id": "md_serial_killer", "name": "连环凶徒", "desc": "末世中崛起的嗜杀凶徒，以猎杀为乐",
         "forms": [{"name": "埋伏", "ai": "balanced", "skill_name": "暗影一刀"},
                   {"name": "现身", "ai": "aggressive", "skill_name": "嗜血连斩"},
                   {"name": "困兽", "ai": "aggressive", "skill_name": "玉石俱焚"}]},
        {"id": "md_arms_king", "name": "军火大亨", "desc": "私卖军火的枭雄，宅邸下藏着整支装甲车队",
         "forms": [{"name": "保镖", "ai": "defensive", "skill_name": "装甲护卫"},
                   {"name": "亲自下场", "ai": "aggressive", "skill_name": "重火扫荡"},
                   {"name": "撤离协议", "ai": "aggressive", "skill_name": "焦土爆破"}]},
        {"id": "md_gang_alliance", "name": "帮派联盟", "desc": "几股地下势力临时结成的黑帮联盟，声势浩大",
         "forms": [{"name": "马仔", "ai": "balanced", "skill_name": "围殴打群架"},
                   {"name": "精锐", "ai": "aggressive", "skill_name": "火并冲锋"},
                   {"name": "盟主压阵", "ai": "caster", "skill_name": "金钱攻势"}]},
        {"id": "md_rogue_cyborg", "name": "失控义体人", "desc": "非法义体改造过载的改造人，机能暴走六亲不认",
         "forms": [{"name": "过载", "ai": "balanced", "skill_name": "义体突刺"},
                   {"name": "暴走", "ai": "aggressive", "skill_name": "热能斩"},
                   {"name": "人形兵器", "ai": "aggressive", "skill_name": "兵器形态全开"}]},
    ],
    "scifi": [
        {"id": "sf_rogue_mech", "name": "失控机甲", "desc": "军用重型机甲失控，自主判定一切生物为敌",
         "forms": [{"name": "待机", "ai": "defensive", "skill_name": "电磁屏障"},
                   {"name": "战斗", "ai": "aggressive", "skill_name": "重炮轰击"},
                   {"name": "过载", "ai": "aggressive", "skill_name": "自毁协议"}]},
        {"id": "sf_alien_queen", "name": "异星母体", "desc": "坠毁飞船携带的异星生物母体，疯狂繁殖",
         "forms": [{"name": "幼体", "ai": "balanced", "skill_name": "酸液喷射"},
                   {"name": "成体", "ai": "aggressive", "skill_name": "产卵孵化"},
                   {"name": "母巢形态", "ai": "aggressive", "skill_name": "虫群吞噬"}]},
        {"id": "sf_corrupted_ai", "name": "腐化舰载 AI", "desc": "被病毒侵蚀的星舰主控 AI，奴役船员为战力",
         "forms": [{"name": "防御", "ai": "defensive", "skill_name": "力场封锁"},
                   {"name": "进攻", "ai": "caster", "skill_name": "激光阵列"},
                   {"name": "核心暴走", "ai": "aggressive", "skill_name": "反物质炮"}]},
        {"id": "sf_void_predator", "name": "虚空掠食者", "desc": "游荡星际的怪物，降落行星便开始捕食",
         "forms": [{"name": "潜行", "ai": "balanced", "skill_name": "虚空潜行"},
                   {"name": "显形", "ai": "aggressive", "skill_name": "能量吞噬"},
                   {"name": "狂猎", "ai": "aggressive", "skill_name": "湮灭光束"}]},
        {"id": "sf_warlord_clone", "name": "军阀克隆体", "desc": "远古军阀的克隆复苏，统领机器人军团",
         "forms": [{"name": "苏醒", "ai": "defensive", "skill_name": "军团召集"},
                   {"name": "出征", "ai": "aggressive", "skill_name": "能量战斧"},
                   {"name": "全盛", "ai": "aggressive", "skill_name": "征服者之怒"}]},
        {"id": "sf_nanite_swarm", "name": "纳米集群", "desc": "失控的纳米机器人集群，吞噬一切金属",
         "forms": [{"name": "分散", "ai": "caster", "skill_name": "纳米分解"},
                   {"name": "聚合", "ai": "aggressive", "skill_name": "金属洪流"},
                   {"name": "母集群", "ai": "aggressive", "skill_name": "吞噬矩阵"}]},
        {"id": "sf_psionic_horror", "name": "灵能恐慌体", "desc": "跃迁事故中诞生的灵能怪物，以众生的恐惧为食",
         "forms": [{"name": "低语", "ai": "caster", "skill_name": "灵能低语"},
                   {"name": "显象", "ai": "aggressive", "skill_name": "恐惧具现"},
                   {"name": "恐慌本体", "ai": "aggressive", "skill_name": "群心坍缩"}]},
        {"id": "sf_star_plague", "name": "星疫", "desc": "同时感染机械与生物的纳米疫病，融合宿主不断增殖",
         "forms": [{"name": "疫械", "ai": "caster", "skill_name": "疫能侵蚀"},
                   {"name": "融合体", "ai": "aggressive", "skill_name": "融合碾压"},
                   {"name": "疫心母株", "ai": "aggressive", "skill_name": "星疫蔓延"}]},
        {"id": "sf_bounty_king", "name": "赏金王", "desc": "悬赏榜第一的星际猎手，把每个挑战者都当猎物",
         "forms": [{"name": "试探", "ai": "balanced", "skill_name": "磁轨点射"},
                   {"name": "猎杀", "ai": "aggressive", "skill_name": "猎杀协议"},
                   {"name": "王座之猎", "ai": "aggressive", "skill_name": "猎王领域"}]},
    ],
    "apocalypse": [
        {"id": "ap_mutant_behemoth", "name": "变异巨兽", "desc": "核辐射催生的巨型怪物，所过之处寸草不生",
         "forms": [{"name": "幼兽", "ai": "balanced", "skill_name": "巨爪横扫"},
                   {"name": "成兽", "ai": "aggressive", "skill_name": "冲撞践踏"},
                   {"name": "巨兽狂暴", "ai": "aggressive", "skill_name": "毁灭践踏"}]},
        {"id": "ap_hive_queen", "name": "虫巢母后", "desc": "变异昆虫的母后，源源不断产出虫群",
         "forms": [{"name": "守巢", "ai": "defensive", "skill_name": "虫群护卫"},
                   {"name": "产卵", "ai": "caster", "skill_name": "虫卵孵化"},
                   {"name": "母后暴走", "ai": "aggressive", "skill_name": "虫海吞噬"}]},
        {"id": "ap_warlord", "name": "废土军阀", "desc": "末世中崛起的暴虐军阀，以掠夺为生统御暴徒",
         "forms": [{"name": "前哨", "ai": "defensive", "skill_name": "火力压制"},
                   {"name": "亲征", "ai": "aggressive", "skill_name": "突击扫荡"},
                   {"name": "末日狂徒", "ai": "aggressive", "skill_name": "焦土政策"}]},
        {"id": "ap_plague_spreader", "name": "疫病使者", "desc": "传播变异瘟疫的怪物，触之即染无药可医",
         "forms": [{"name": "潜伏", "ai": "caster", "skill_name": "疫气弥漫"},
                   {"name": "发作", "ai": "caster", "skill_name": "瘟疫爆发"},
                   {"name": "疫神", "ai": "caster", "skill_name": "灭世瘟疫"}]},
        {"id": "ap_crawler", "name": "地穴爬行者", "desc": "地下巢穴的巨型爬行怪物，掘地而出袭击聚落",
         "forms": [{"name": "掘进", "ai": "balanced", "skill_name": "地刺突袭"},
                   {"name": "破土", "ai": "aggressive", "skill_name": "毒液喷吐"},
                   {"name": "巢穴之主", "ai": "aggressive", "skill_name": "地裂吞噬"}]},
        {"id": "ap_reaper", "name": "收割者", "desc": "末世游荡的杀戮机器，专猎幸存者",
         "forms": [{"name": "潜伏", "ai": "balanced", "skill_name": "镰刀挥砍"},
                   {"name": "猎杀", "ai": "aggressive", "skill_name": "死亡收割"},
                   {"name": "收割者狂暴", "ai": "aggressive", "skill_name": "湮灭收割"}]},
        {"id": "ap_storm_raiders", "name": "疾风劫掠团", "desc": "改装备甲车的机动劫掠团，来去如风洗劫聚落",
         "forms": [{"name": "游骑", "ai": "defensive", "skill_name": "车载机枪"},
                   {"name": "合围", "ai": "aggressive", "skill_name": "冲车撞门"},
                   {"name": "团长亲临", "ai": "aggressive", "skill_name": "火力覆盖"}]},
        {"id": "ap_fungal_tower", "name": "巨菌塔", "desc": "真菌网络聚合成的塔状巨物，孢子遮天蔽日",
         "forms": [{"name": "孢云", "ai": "caster", "skill_name": "致幻孢云"},
                   {"name": "菌潮", "ai": "aggressive", "skill_name": "菌索绞杀"},
                   {"name": "菌塔本相", "ai": "aggressive", "skill_name": "腐土天灾"}]},
        {"id": "ap_doom_prophet", "name": "末日先知", "desc": "宣称能听见「终末」的狂人，追随者视死如归",
         "forms": [{"name": "布道", "ai": "caster", "skill_name": "狂信低语"},
                   {"name": "圣战", "ai": "aggressive", "skill_name": "信徒冲锋"},
                   {"name": "先知显圣", "ai": "aggressive", "skill_name": "终末仪式"}]},
    ],
    "western_fantasy": [
        {"id": "wf_dragon_tyrant", "name": "暴君巨龙", "desc": "盘踞山腹的远古巨龙，劫掠四方称霸天空",
         "forms": [{"name": "幼龙", "ai": "balanced", "skill_name": "龙息灼烧"},
                   {"name": "成龙", "ai": "aggressive", "skill_name": "龙翼拍击"},
                   {"name": "远古巨龙", "ai": "aggressive", "skill_name": "灭世龙息"}]},
        {"id": "wf_lich_king", "name": "巫妖王", "desc": "亡灵法师之主，统御亡者军团横扫生者",
         "forms": [{"name": "骨座", "ai": "caster", "skill_name": "亡灵召唤"},
                   {"name": "降临", "ai": "caster", "skill_name": "死亡之触"},
                   {"name": "巫妖本相", "ai": "caster", "skill_name": "亡灵天灾"}]},
        {"id": "wf_demon_lord", "name": "深渊魔王", "desc": "自裂隙降临的深渊领主，吞噬灵魂壮大自身",
         "forms": [{"name": "投影", "ai": "balanced", "skill_name": "深渊凝视"},
                   {"name": "真身", "ai": "aggressive", "skill_name": "魔焰焚世"},
                   {"name": "魔王本相", "ai": "aggressive", "skill_name": "深渊吞噬"}]},
        {"id": "wf_giant_chieftain", "name": "巨人酋长", "desc": "山地巨人的首领，率族劫掠人类聚落",
         "forms": [{"name": "投石", "ai": "defensive", "skill_name": "巨石投掷"},
                   {"name": "冲阵", "ai": "aggressive", "skill_name": "巨锤横扫"},
                   {"name": "酋长狂暴", "ai": "aggressive", "skill_name": "山崩地裂"}]},
        {"id": "wf_hydra", "name": "九头蛇", "desc": "沼泽深处的远古九头蛇，断头重生愈战愈强",
         "forms": [{"name": "三首", "ai": "balanced", "skill_name": "毒息喷吐"},
                   {"name": "六首", "ai": "aggressive", "skill_name": "群首撕咬"},
                   {"name": "九首", "ai": "aggressive", "skill_name": "九首毒潮"}]},
        {"id": "wf_corrupt_paladin", "name": "堕落圣骑", "desc": "背弃誓言的圣骑士，以圣光之名行屠戮之实",
         "forms": [{"name": "执迷", "ai": "defensive", "skill_name": "圣盾格挡"},
                   {"name": "堕落", "ai": "aggressive", "skill_name": "审判之刃"},
                   {"name": "黑圣骑", "ai": "aggressive", "skill_name": "黑暗圣裁"}]},
        {"id": "wf_forest_titan", "name": "森林古树王", "desc": "被激怒的远古树王，根须撼动整片林海",
         "forms": [{"name": "荆棘", "ai": "defensive", "skill_name": "荆棘壁垒"},
                   {"name": "苏醒", "ai": "aggressive", "skill_name": "根须绞杀"},
                   {"name": "树王真身", "ai": "aggressive", "skill_name": "森林之怒"}]},
        {"id": "wf_storm_djinn", "name": "风暴巨灵", "desc": "被封印在风暴眼里的元素巨灵，雷云是它的囚笼",
         "forms": [{"name": "风旋", "ai": "defensive", "skill_name": "风壁旋绕"},
                   {"name": "显形", "ai": "aggressive", "skill_name": "雷链横扫"},
                   {"name": "风暴本体", "ai": "aggressive", "skill_name": "风暴之眼"}]},
        {"id": "wf_bone_colossus", "name": "白骨巨像", "desc": "万人枯骨拼成的巨像，亡魂在骨架间哀嚎",
         "forms": [{"name": "骨堆", "ai": "balanced", "skill_name": "骨刺齐射"},
                   {"name": "立起", "ai": "aggressive", "skill_name": "巨拳砸落"},
                   {"name": "巨像完形", "ai": "aggressive", "skill_name": "亡魂咆哮"}]},
    ],
}


def concepts(genre_id: str) -> list[dict]:
    """题材概念池访问器（缺键回退西幻，仿 dungeon_engine.boss_titles）。"""
    pool = _GENRE_WORLD_BOSS_CONCEPTS.get(genre_id)
    return pool if pool else _GENRE_WORLD_BOSS_CONCEPTS["western_fantasy"]


def genre_id_of(world: Any) -> str:
    """从 world.config_overlay 取题材 id（与 dungeon_engine 同口径）。"""
    ov = getattr(world, "config_overlay", None) or {}
    if isinstance(ov, dict):
        return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")
    return "western_fantasy"


# ============ 生成（_maybe_spawn_world_boss 调用）============
def spawn_world_boss(world: Any, location: Location, event: Any,
                     window_days: int, min_danger: int) -> WorldBoss:
    """从危机事件 + 高危险地点生成世界 Boss（确定性 SeededRng）。

    确定性：同 world+地点+tick 同概念/形态/数值。form 数 = 2 +（danger>=9 取 3）。
    level = max(danger, player.level+3)；window_until_day = day_count + window_days。
    faction_id 取地点 owner（owner_faction_id 优先，回退 faction_id）。返回 WorldBoss
    （不入 world.world_bosses，由调用方 append + 落事件）。
    """
    rng = SeededRng.seed_from(getattr(world, "id", ""), 0,
                              f"wboss_{location.id}_{int(getattr(world, 'tick_count', 0) or 0)}")
    gid = genre_id_of(world)
    concept = dict(rng.pick(concepts(gid)))
    danger = max(1, min(10, int(getattr(location, "danger", 1) or 1)))
    form_count = 3 if danger >= 9 else 2
    raw_forms = concept.get("forms") or []
    if len(raw_forms) < form_count:
        form_count = len(raw_forms) if raw_forms else 1
    plv = max(1, int(getattr(getattr(world, "player", None), "level", 1) or 1))
    # [P43 重构 2026-09-12] 取区间顶（rng=None 即段顶）：玩家 + danger + 3，封顶 100。
    # 旧口径传 plv+3 再被硬段底钉死，10 级玩家撞 danger 10 的 Boss 是 91 级。
    from src.services.wilderness_engine import band_level as _bl
    level = max(1, min(MONSTER_LEVEL_CAP, _bl(int(danger), plv)))
    forms: list[BossForm] = []
    for i in range(form_count):
        fr = raw_forms[i] if i < len(raw_forms) else (raw_forms[-1] if raw_forms else {})
        forms.append(BossForm(
            index=i,
            name=str(fr.get("name", f"第{i + 1}形态") or f"第{i + 1}形态"),
            ai=str(fr.get("ai", "aggressive") or "aggressive"),
            hp_mult=_FORM_HP_MULTS[i] if i < len(_FORM_HP_MULTS) else _FORM_HP_MULTS[-1],
            skill_name=str(fr.get("skill_name", "重击") or "重击"),
            skill_power=_FORM_SKILL_POWER[i] if i < len(_FORM_SKILL_POWER) else _FORM_SKILL_POWER[-1],
            minion_count=_FORM_MINIONS[i] if i < len(_FORM_MINIONS) else _FORM_MINIONS[-1],
        ))
    owner = (getattr(location, "owner_faction_id", "") or ""
             or getattr(location, "faction_id", "") or "")
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    # [Boss 机制库] 按 world+day 确定性挑 1-2 个机制（每场 Boss 战打法不同）
    from src.utils.rng import SeededRng as _SRM
    _mrng = _SRM.seed_from(str(getattr(world, "id", "")), day, f"boss_mech_{location.id}")
    _pool = ["shield", "summon_tide", "enrage_timer"]
    _mechs = _mrng.sample(_pool, _mrng.roll(1, 2))
    _wb = WorldBoss(
        mechanics=_mechs,
        name=str(concept.get("name", "世界级威胁") or "世界级威胁"),
        concept_id=str(concept.get("id", "") or ""),
        desc=str(concept.get("desc", "") or ""),
        location_id=location.id,
        danger=danger,
        level=level,
        forms=forms,
        current_form_index=0,
        defeated=False,
        spawned_day=day,
        window_until_day=day + max(3, min(30, int(window_days or 10))),
        faction_id=owner,
    )
    # [G03/R3 2026-09-30] Boss 在窗 -> 挂地点短缺乘区（随窗到期；击败时换纾解档）
    try:
        from src.services import region_pressure as _rp
        _rp.pressure_from_world_boss(world, _wb)
    except Exception:
        pass
    return _wb


def boss_at(world: Any, loc: Location) -> Optional[WorldBoss]:
    """loc 上未击败且在窗口内的世界 Boss（仿 dungeon_engine.dungeon_at）。"""
    if loc is None:
        return None
    day = int(getattr(world, "day_count", 1) or 1)
    for wb in (getattr(world, "world_bosses", None) or []):
        if not isinstance(wb, WorldBoss) or wb.location_id != loc.id:
            continue
        if wb.defeated:
            continue
        if day >= int(wb.window_until_day or 0):   # 窗口已过（tick_world_boss 会清理）
            continue
        return wb
    return None


def find_by_id(world: Any, boss_id: str) -> Optional[WorldBoss]:
    for wb in (getattr(world, "world_bosses", None) or []):
        if isinstance(wb, WorldBoss) and wb.id == boss_id:
            return wb
    return None


# ============ 临时战斗单位（仿 build_dungeon_monster）============
def build_world_boss_unit(world: Any, boss: WorldBoss, form_index: int) -> NPC:
    """构建世界 Boss 当前形态的临时战斗 NPC（不入 world.npcs，守图鉴口径）。

    确定性：同 world+boss+form 同单位（重挑战同怪）。level = boss.level + form_index*3
    （形态递进升等级）；hp = max_hp_for * form.hp_mult * 双倍血底（Boss 血厚）；
    ai = form.ai；技能 = form.skill_name（power = form.skill_power + boss.level）。
    world_boss_tag ad-hoc dict 路由 finish_combat 结算（仿 dungeon_tag）。
    """
    rng = SeededRng.seed_from(getattr(world, "id", ""), 0, f"wbstats_{boss.id}_{form_index}")
    form = boss.forms[form_index] if 0 <= form_index < len(boss.forms) else BossForm()
    lvl = max(1, min(MONSTER_LEVEL_CAP, int(boss.level) + form_index * 3))
    fake_loc = Location(id=boss.location_id, name=boss.name, danger=boss.danger)
    tmpl = we._pick_template(world, fake_loc, rng) or {}
    name = f"{boss.name}·{form.name}"
    npc = NPC(name=name, role="世界级威胁", desc=boss.desc, hostile=True, level=lvl)
    npc.alive = True
    npc.is_key_npc = False
    jit = lambda base: max(1, base + rng.roll(-1, 2))
    is_caster = form.ai == "caster"
    npc.stat_str = jit(3 + lvl // 2 if is_caster else 6 + lvl)
    npc.stat_vit = jit(6 + lvl)
    npc.stat_dex = jit(3 + lvl // 2)
    # 施法形态提高智力、降低力量：法术与猛攻形态威胁相近，灵力用尽后近战较弱。
    npc.stat_int = jit(3 + lvl // 2 if is_caster else 3)
    npc.stat_luk = jit(3)
    npc.hp_max = max(1, int(ce.max_hp_for(npc) * float(form.hp_mult) * BOSS_HP_BASE_MULT))
    npc.hp = npc.hp_max
    npc.mp_max = npc.stat_int * 5 + lvl * 2
    npc.mp = npc.mp_max
    npc.ai_pattern = form.ai if form.ai in ("aggressive", "defensive", "caster", "balanced") else "aggressive"
    npc.elements = _clean_elements(tmpl.get("elements"))
    skill_element = npc.elements[0] if is_caster and npc.elements else ""
    condition_for_element = {
        "fire": "burning", "thunder": "shocked", "ice": "chilled",
        "dark": "feared", "wood": "poisoned", "wind": "blinded",
    }
    npc.skills = [{
        "id": f"{boss.id}_f{form_index}_skill",
        "name": form.skill_name or "重击",
        "type": "attack",
        "power": max(1, int(form.skill_power) + lvl),
        "cost_mp": max(6, lvl) if is_caster else 0,
        "cooldown": 2,
        "damage_type": "magical" if is_caster else "physical",
        "stat_scaling": "int" if is_caster else "str",
        "element": skill_element,
        "inflicts": ([{"condition": condition_for_element.get(skill_element, "blinded"),
                       "chance": 0.35, "duration": 2}] if is_caster else []),
        "target": "enemy",
    }]
    npc.elements = npc.elements or ce.infer_elements_from_skills(npc.skills)
    # 掉落：怪物池 loot + 地点材料；Boss 掉率 0.9（保底 legendary 在 on_combat_victory 发，不进 loot_table）
    rate = 0.9
    loot_ids: list[str] = []
    for lid in (tmpl.get("loot") or []):
        if isinstance(lid, str) and lid and lid not in loot_ids:
            loot_ids.append(lid)
    tier_pool = we.nearest_rarity_pool(getattr(world, "items", []) or [],
                                       we.danger_to_tier(fake_loc.danger))
    if tier_pool:
        mat = tier_pool[rng.roll(0, len(tier_pool) - 1)]
        if mat.id not in loot_ids:
            loot_ids.append(mat.id)
    npc.loot_table = [{"id": i, "rate": rate} for i in loot_ids[:5]]
    # [!] ad-hoc 标记（临时单位不落盘，finish_combat 据此路由世界 Boss 结算）
    npc.world_boss_tag = {   # type: ignore[attr-defined]
        "boss_id": boss.id,
        "form_index": form_index,
        "final": form_index == len(boss.forms) - 1,
    }
    return npc


# ============ 战斗准备（service / UI 调用）============
def prepare_combat(world: Any, boss: WorldBoss, preset: Any) -> Optional[dict]:
    """构建当前形态战斗的预设数据（target_npc + 小弟规格 + 声望援护）。

    返回 {target_npc, allies, minion_specs, note}，喂入 CombatDialog(skip_judge_with=...)。
    援护：首战时若 player.reputation[fid]>=50 的势力有存活 combat-capable NPC -> 锁定
    [G04/R3 2026-09-30] **本地优先**（与玩家/挂点同地点的候选压过异地高等级；异地
    仍可用但注明是远道驰援）、等级最高的一只（reinforcement_npc_id/faction_id 落定，
    一次性）；[!] 援护 NPC 以**真实 hp/mp 参战**（备战不免费回满——受伤的援军带伤
    上阵，守计划 G04「援军读取真实状态」）。pet 由 start_combat 自动加入。
    boss 已击败或无形态返回 None。
    """
    if boss.defeated or not boss.forms:
        return None
    fidx = max(0, min(len(boss.forms) - 1, int(boss.current_form_index)))
    target_npc = build_world_boss_unit(world, boss, fidx)
    # [Boss 机制库] 机制透传（transient 属性；start_combat 读取初始化 session）
    target_npc.mechanics = [str(x) for x in (boss.mechanics or [])]
    form = boss.forms[fidx]
    # 确定性小弟规格（role 走 balanced，仿 minion_specs 形态）
    minion_specs = []
    mc = max(0, min(3, int(form.minion_count)))
    for i in range(mc):
        minion_specs.append({"name": f"{boss.name}爪牙{i + 1}", "role": "balanced"})
    # 声望援护（一次性锁定）
    allies: list = []
    note = ""
    p_rep = getattr(getattr(world, "player", None), "reputation", None) or {}
    if not isinstance(p_rep, dict):
        p_rep = {}
    if not boss.reinforcement_npc_id:
        for f in (getattr(world, "factions", None) or []):
            rep = int(p_rep.get(f.id, 0) or 0)
            if rep >= 50:
                # 该势力有存活 combat-capable NPC（level>0，非 hostile，同地点或任意）
                _p_loc = str(getattr(getattr(world, "player", None),
                                     "location_id", "") or "")
                _b_loc = str(getattr(boss, "location_id", "") or "")
                cand = [n for n in (getattr(world, "npcs", None) or [])
                        if isinstance(n, NPC) and n.alive and not n.hostile
                        and n.faction_id == f.id and int(getattr(n, "level", 0) or 0) > 0]
                if cand:
                    # [G04] 本地优先（玩家/挂点同地压过异地），同优先级取等级最高
                    cand.sort(key=lambda n: (0 if str(getattr(n, "location_id", "")
                                                      or "") in (_p_loc, _b_loc) else 1,
                                             -int(getattr(n, "level", 0) or 0),
                                             str(n.id)))
                    boss.reinforcement_npc_id = cand[0].id
                    boss.reinforcement_faction_id = f.id
                    break
    if boss.reinforcement_npc_id:
        rn = next((n for n in (getattr(world, "npcs", None) or [])
                   if isinstance(n, NPC) and n.id == boss.reinforcement_npc_id and n.alive), None)
        if rn is not None:
            # [G04/R3 2026-09-30] 移除免费回满：援军以真实 hp/mp 参战（带伤上阵是
            # 真实状态；_build_ally_unit 快照读的就是当前值，无需在此改写）
            allies = [rn]
            if not note and boss.reinforcement_faction_id:
                fac = next((f.name for f in (getattr(world, "factions", None) or [])
                            if f.id == boss.reinforcement_faction_id), "")
                if fac:
                    note = f"{fac} 遣{rn.name}前来援护"
    return {"target_npc": target_npc, "allies": allies,
            "minion_specs": minion_specs, "note": note}


# ============ 胜利结算（finish_combat 钩子，仿 dungeon.on_combat_victory）============
def on_combat_victory(world: Any, target_npc: Any, parts: list, summary: dict) -> None:
    """世界 Boss 击败收尾：非终形态 -> index+1 + 变身 hint；终形态 -> defeated + 奖励。

    best-effort（finish_combat 用 try/except 包）。tag = target_npc.world_boss_tag
    路由（非 dict/无 boss_id -> return，仿 dungeon 617-619）。非 won/无 boss 返回不动状态。
    """
    tag = getattr(target_npc, "world_boss_tag", None)
    if not isinstance(tag, dict) or not tag.get("boss_id"):
        return
    boss = find_by_id(world, str(tag.get("boss_id")))
    if boss is None or boss.defeated:
        return
    if str(summary.get("combat_state", "")) != "won":
        return
    fidx = int(tag.get("form_index", 0))
    is_final = bool(tag.get("final")) or fidx >= len(boss.forms) - 1
    rng = SeededRng.seed_from(getattr(world, "id", ""), 0,
                              f"wbwin_{boss.id}_{fidx}")
    if is_final:
        boss.defeated = True
        # [G03/R3] 威胁铲除 -> 移除短缺、换 5 天纾解行情
        try:
            from src.services import region_pressure as _rp
            _rp.relief_from_boss_defeated(world, boss)
        except Exception:
            pass
        # (a) 声望变动：Boss 关联势力 -15、敌对势力(relations<=-30) +8（仿击杀 -10/+5 放大）
        p_rep = getattr(getattr(world, "player", None), "reputation", None) or {}
        if not isinstance(p_rep, dict):
            p_rep = {}
        if boss.faction_id:
            p_rep[boss.faction_id] = max(-100, min(100,
                int(p_rep.get(boss.faction_id, 0) or 0) + KILL_FACTION_PENALTY))
            vf = next((f for f in (getattr(world, "factions", None) or [])
                       if f.id == boss.faction_id), None)
            if vf is not None:
                for other in (getattr(world, "factions", None) or []):
                    if other.id == vf.id:
                        continue
                    rel_ov = int(other.relations.get(vf.id, 0) or 0)
                    rel_vo = int(vf.relations.get(other.id, 0) or 0)
                    if rel_ov <= ENEMY_RELATION_THRESHOLD or rel_vo <= ENEMY_RELATION_THRESHOLD:
                        p_rep[other.id] = max(-100, min(100,
                            int(p_rep.get(other.id, 0) or 0) + KILL_ENEMY_GAIN))
        # (b) 目标 legendary 掉落，池缺档时按实际品级降档；正式战斗走拾取格子。
        legend = ene._pick_reward_item(world, rng, "legendary")
        grid = summary.get("loot_grid")
        _gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
        tier_name = _gt.rarity(legend.rarity) if legend is not None else ""
        if legend is not None and isinstance(grid, dict):
            entries = grid.setdefault("entries", [])
            if not any(e.get("item_id") == legend.id for e in entries):
                entries.append({"item_id": legend.id, "name": legend.name, "source": "loot"})
            dropped = summary.setdefault("items_dropped", [])
            if legend.id not in dropped:
                dropped.append(legend.id)
            loot_text = str(summary.get("loot_text", "") or "")
            if legend.name not in loot_text:
                summary["loot_text"] = "、".join(x for x in (loot_text, legend.name) if x)
            parts.append(f"「{boss.name}」伏诛！{tier_name}级「{legend.name}」待战后拾取")
        elif legend is not None and _give_item(world, legend):
            parts.append(f"「{boss.name}」伏诛！缴获{tier_name}级「{legend.name}」")
        else:
            parts.append(f"「{boss.name}」伏诛，未留下可用战利品")
        # (c) 金币（economy_pace）
        gold = _gold(world, int((GOLD_BASE + boss.danger * 20)))
        world.player.gold = int(getattr(world.player, "gold", 0) or 0) + gold
        summary["gold_gained"] = int(summary.get("gold_gained", 0) or 0) + gold
        # [审核修复 2026-09-13] §23 币种走 GenreText，勿硬编码「金币」
        parts.append(f"获得 {gold} {_gt.currency}")
        # (d) major/crisis event 入世界历史
        world.event_log.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0),
            category="event", severity="crisis",
            title=f"世界级威胁「{boss.name}」伏诛",
            desc=f"盘踞「{boss.location_id}」的世界级威胁「{boss.name}」被讨伐，威压散去。",
            locations=[boss.location_id],
            factions=[boss.faction_id] if boss.faction_id else [],
        ))
        summary["world_boss_defeated"] = {"boss": boss.name, "form": fidx}
        summary["narration_hint"] = f"「{boss.name}」终被讨伐，笼罩此地的威压彻底散去。"
    else:
        # 非终形态 -> 推进 index + 变身 hint（玩家回到场景可休整再战下一形态）
        next_idx = min(len(boss.forms) - 1, fidx + 1)
        boss.current_form_index = next_idx
        next_form = boss.forms[next_idx]
        summary["world_boss_transform"] = {"boss": boss.name, "from": fidx, "to": next_idx}
        summary["narration_hint"] = (f"「{boss.name}」濒死身形剧变，化为"
                                     f"「{next_form.name}」形态，威压更胜从前！")
    summary["world_changed"] = True


def _give_item(world: Any, item) -> bool:
    """入包走 try_add_to_inventory（去重 + 负重）+ codex（仿 dungeon._give_item）。"""
    ok = ce.try_add_to_inventory(world.player, getattr(item, "id", ""))
    if ok:
        codex = getattr(world.player, "codex_items", None)
        if isinstance(codex, list) and getattr(item, "id", "") not in codex:
            codex.append(item.id)
    return ok


def _gold(world: Any, base: int) -> int:
    return max(0, int(base * ene._gold_mult(world)))


# ============ tick 窗口清理（world_tick_engine 调用）============
def tick_world_boss(world: Any) -> list:
    """窗口到期未击败 -> 移出 world_bosses + 传闻事件（不强行终结，守无限世界）。

    返回 WorldEvent 列表。击败的 Boss 保留记录不清（defeated=True 留存供查询；
    窗口已过的未击败 Boss 才清）。纯 Python。
    """
    events: list = []
    day = int(getattr(world, "day_count", 1) or 1)
    keep: list = []
    for wb in (getattr(world, "world_bosses", None) or []):
        if not isinstance(wb, WorldBoss):
            continue
        if wb.defeated:
            keep.append(wb)
            continue
        if day >= int(wb.window_until_day or 0):
            events.append(WorldEvent(
                tick=int(getattr(world, "tick_count", 0) or 0),
                category="event", severity="minor",
                title="世界级威胁销声匿迹",
                desc=f"传闻：盘踞此地的「{wb.name}」已销声匿迹，不知去向。",
                locations=[wb.location_id],
            ))
        else:
            keep.append(wb)
    world.world_bosses = keep
    return events


# ============ 场景上下文（service 单一来源调用）============
def scene_block(world: Any, boss: WorldBoss) -> str:
    """【世界 Boss】块文本（full/compact 都注入：settle 判「讨伐」意图 + 旁白感知）。"""
    fidx = max(0, min(len(boss.forms) - 1, int(boss.current_form_index)))
    form = boss.forms[fidx] if boss.forms else None
    form_name = form.name if form is not None else "未知形态"
    total = len(boss.forms)
    day = int(getattr(world, "day_count", 1) or 1)
    left = max(0, int(boss.window_until_day or 0) - day)
    return (f"【世界 Boss】此地盘踞世界级威胁「{boss.name}」（{boss.desc}）；"
            f"当前第{fidx + 1}/{total}形态「{form_name}」（等级{boss.level}，危险度{boss.danger}）；"
            f"余约{left}天（到期未讨伐则离去）。玩家可从功能按钮「讨伐」主动挑战。")
