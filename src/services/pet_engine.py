"""[P24d] 宠物驯服与养成引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21）：宠物是常驻世界实体（World.pets，区别于战斗临时随从单位）。
获取两渠道（奇遇 tame 原型 / 野外胜利低概率遇幼崽）都走本模块 tame_check 纯检定
（dex/luk 取高 + 投食加成消耗消耗品）；种族池 6 题材各 >=6（tests/test_genre_data_volume.py
TestPetPool 守护），数值成长模板挂种族（基础五维 + 每级成长），等级封顶 10。

出战 = PlayerState.active_pet_id 指向（战斗自动入 ally 单位走 build_pet_unit，
不占 companion 位）；亲和经投食提升，>=50 解锁种族被动（采集加成/预警/寻宝，
挂点在 world_sim_service/encounter_engine 调 passive_active）。宠物技能：种族挂
skill_candidates（取自同题材技能池），槽位 = 1 + Lv4 + Lv8（钳 3），兽诀卷
（reagent_kind=beast_skill）重 roll 单槽（refine_engine.reroll_pet_skill）；繁殖 v1
不做（路线图 P24d 留扩展位）。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from src.models.world import World, Pet, effective_category
from src.services import combat_engine as ce
from src.services import talent_engine as te
from src.utils.rng import SeededRng

# ---- 数值旋钮（纯引擎常量，确定性）----
PET_MAX = 3                # 宠物上限（超出后 tame/幼崽渠道直接跳过）
PET_LEVEL_CAP = 10         # 等级封顶
PET_XP_BASE = 50           # 升级曲线 xp_next = 50 * level ** 1.15
# 投食按品质分档：高品消耗品提升更快（common = 旧 +8 基线，老测试/老档口径不跳）
FEED_AFFINITY_BY_RARITY = {
    "common": 8, "uncommon": 10, "rare": 14,
    "epic": 20, "legendary": 30, "mythic": 45,
}
FEED_AFFINITY = FEED_AFFINITY_BY_RARITY["common"]  # 兼容旧引用（common 档）
AFFINITY_PASSIVE_AT = 50   # 亲和 >= 50 解锁种族被动
PET_INIT_AFFINITY = 10     # 驯服入队初始亲和
TAME_BASE = 0.45           # 驯服检定基础成功率（difficulty = danger*5）
TAME_FEED_BONUS = 0.15     # 投食加成（背包有消耗品时自动投食 1 件，成败都消耗）
CUB_CHANCE = 0.10          # 野外战斗胜利后遇幼崽概率
PET_BATTLE_XP_PER_LEVEL = 2  # 出战胜利宠物 xp = 敌 level x 2

# 被动白名单（挂点：gather_bonus=采集成功率+0.10 / alert=野外遇怪率x0.7 / treasure=奇遇金币x1.15）
_PASSIVE_VALUES = ("gather_bonus", "alert", "treasure")
PASSIVE_ZH = {"gather_bonus": "采集加成", "alert": "预警", "treasure": "寻宝"}

# ---- 题材化宠物种族池（6 题材全覆盖，缺键回退西幻，守 §23 数据量铁律）----
# 每条：id（全局唯一）/name/desc/tier 1-3/stats 基础五维/growth 每级成长/
# passive 种族被动（白名单三选一）/elements 可选元素构成（走 _ELEMENT_VALUES 白名单）。
# 出战单位数值 = stats + growth x (level-1) 经 compute_stats 聚合（build_pet_unit）。
_GENRE_PET_SPECIES: dict[str, list[dict]] = {
    "western_fantasy": [
        {"id": "wf_dragonling", "name": "幼火龙", "tier": 3, "elements": ["fire"],
         "desc": "鳞片还泛着炉火色光泽的幼龙，打盹时鼻孔会冒出细小火星。",
         "stats": {"str": 14, "dex": 9, "int": 10, "vit": 13, "luk": 8},
         "growth": {"str": 2, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "treasure"},
        {"id": "wf_griffon", "name": "狮鹫幼雏", "tier": 3, "elements": ["wind"],
         "desc": "绒毛未褪的狮鹫雏鸟，鹰喙已能敲开硬壳坚果，眼神锐利。",
         "stats": {"str": 12, "dex": 14, "int": 9, "vit": 11, "luk": 10},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "wf_bearcub", "name": "穴居熊崽", "tier": 2,
         "desc": "圆滚滚的小熊，爪子已很有力，最爱翻石头找蜂蜜。",
         "stats": {"str": 13, "dex": 7, "int": 7, "vit": 14, "luk": 9},
         "growth": {"str": 2, "dex": 1, "int": 0, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "wf_wolf", "name": "森林狼崽", "tier": 1,
         "desc": "灰扑扑的狼崽，耳朵总竖着，夜里会守在篝火边。",
         "stats": {"str": 9, "dex": 11, "int": 8, "vit": 9, "luk": 9},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "wf_faycat", "name": "精灵猫灵", "tier": 1, "elements": ["light"],
         "desc": "瞳孔映着微光的小猫，总能在奇怪的地方叼回亮晶晶的小玩意。",
         "stats": {"str": 7, "dex": 11, "int": 9, "vit": 8, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "wf_deer", "name": "月纹小鹿", "tier": 1, "elements": ["wood"],
         "desc": "背上有月牙纹路的小鹿，能嗅出林中珍稀药草的位置。",
         "stats": {"str": 8, "dex": 10, "int": 10, "vit": 10, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 2, "vit": 1, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "wf_owlcat", "name": "鸮豹幼崽", "tier": 2, "elements": ["dark"],
         "desc": "长着猫头鹰面孔的豹崽，夜里瞳孔金黄，暗处的动静瞒不过它。",
         "stats": {"str": 11, "dex": 13, "int": 9, "vit": 9, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "wf_emberfox", "name": "焰尾狐", "tier": 2, "elements": ["fire"],
         "desc": "尾尖像蘸了炭火的红狐，对山洞里的宝石矿脉有近乎执拗的嗅觉。",
         "stats": {"str": 9, "dex": 12, "int": 10, "vit": 8, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 2},
         "passive": "treasure"},
        {"id": "wf_stonegoat", "name": "洞穴石羊", "tier": 1, "elements": ["earth"],
         "desc": "蹄子能扒开碎石的小山羊，总能从岩缝里啃出带矿苗的草。",
         "stats": {"str": 9, "dex": 9, "int": 7, "vit": 12, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
    ],
    "xianxia": [
        {"id": "xx_spiritfox", "name": "九尾灵狐", "tier": 3, "elements": ["fire"],
         "desc": "尾尖才泛一点火色的灵狐幼崽，通人性，最爱灵石的光。",
         "stats": {"str": 9, "dex": 13, "int": 14, "vit": 10, "luk": 11},
         "growth": {"str": 1, "dex": 2, "int": 2, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "xx_thundermarten", "name": "雷貂", "tier": 3, "elements": ["thunder"],
         "desc": "皮毛间有细小电弧跳动的灵貂，快得只余一道残影。",
         "stats": {"str": 8, "dex": 15, "int": 11, "vit": 9, "luk": 10},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 2},
         "passive": "alert"},
        {"id": "xx_turtle", "name": "玄甲灵龟", "tier": 2, "elements": ["earth"],
         "desc": "龟甲刻着天然云纹的幼龟，慢悠悠的，甲壳硬得能挡剑。",
         "stats": {"str": 10, "dex": 7, "int": 10, "vit": 15, "luk": 8},
         "growth": {"str": 2, "dex": 0, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "xx_crane", "name": "灵鹤", "tier": 2, "elements": ["wind"],
         "desc": "丹顶未红的幼鹤，清晨会随雾气起舞，警觉非常。",
         "stats": {"str": 8, "dex": 13, "int": 10, "vit": 9, "luk": 11},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "xx_serpent", "name": "蛟纹幼蛇", "tier": 2, "elements": ["ice"],
         "desc": "鳞片有淡蓝蛟纹的小蛇，缠在腕上冰凉，对灵物极敏感。",
         "stats": {"str": 11, "dex": 12, "int": 9, "vit": 9, "luk": 10},
         "growth": {"str": 2, "dex": 1, "int": 1, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "xx_rabbit", "name": "灵玉兔", "tier": 1, "elements": ["wood"],
         "desc": "额有一点玉色的兔子，最会辨认灵草嫩芽。",
         "stats": {"str": 7, "dex": 11, "int": 9, "vit": 9, "luk": 12},
         "growth": {"str": 1, "dex": 1, "int": 2, "vit": 1, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "xx_fledgling", "name": "雏凤", "tier": 3, "elements": ["fire"],
         "desc": "绒羽里透出金红光泽的雏凤，睡熟时周身暖得像小暖炉，对灵物天生存感。",
         "stats": {"str": 11, "dex": 12, "int": 13, "vit": 11, "luk": 12},
         "growth": {"str": 2, "dex": 2, "int": 2, "vit": 1, "luk": 2},
         "passive": "treasure"},
        {"id": "xx_iceclam", "name": "冰魄蚕", "tier": 2, "elements": ["ice"],
         "desc": "通体如凝冰的灵蚕，吐的丝寒气不散，最爱啃食雪岭灵叶。",
         "stats": {"str": 7, "dex": 9, "int": 11, "vit": 12, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 2, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "xx_windweasel", "name": "灵鼬", "tier": 1, "elements": ["wind"],
         "desc": "一身黄褐短毛的小鼬，窜起来贴地飞，方圆里的动静它先竖毛。",
         "stats": {"str": 8, "dex": 13, "int": 8, "vit": 8, "luk": 11},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
    ],
    "wuxia": [
        {"id": "wx_falcon", "name": "猎隼", "tier": 2, "elements": ["wind"],
         "desc": "驯鹰人遗落的幼隼，盘旋高空，百里外的风吹草动都逃不过它。",
         "stats": {"str": 9, "dex": 14, "int": 9, "vit": 8, "luk": 10},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "wx_mastiff", "name": "藏獒幼犬", "tier": 2,
         "desc": "狮头未立的獒崽，护主心切，夜里从不熟睡。",
         "stats": {"str": 12, "dex": 9, "int": 8, "vit": 13, "luk": 8},
         "growth": {"str": 2, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "alert"},
        {"id": "wx_blackbear", "name": "黑熊崽", "tier": 2,
         "desc": "山林猎户养熟的黑熊崽，一巴掌能拍碎青竹，会拱出埋土的药材。",
         "stats": {"str": 13, "dex": 8, "int": 7, "vit": 12, "luk": 9},
         "growth": {"str": 2, "dex": 1, "int": 0, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "wx_whitefox", "name": "雪狐", "tier": 2, "elements": ["ice"],
         "desc": "雪山下来的白狐，通体无杂色，对金银细软格外留心。",
         "stats": {"str": 8, "dex": 12, "int": 10, "vit": 8, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 2},
         "passive": "treasure"},
        {"id": "wx_monkey", "name": "灵猴", "tier": 1, "elements": ["wood"],
         "desc": "会学人作揖的小猴，爬树摘果一溜烟，最爱甜果子。",
         "stats": {"str": 8, "dex": 13, "int": 10, "vit": 7, "luk": 10},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "wx_palmcat", "name": "狸花猫", "tier": 1,
         "desc": "市井里捡来的狸花猫，白天睡觉夜里巡街，能叼回丢的钱袋。",
         "stats": {"str": 7, "dex": 12, "int": 9, "vit": 8, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "wx_eagleowl", "name": "雕鸮", "tier": 2, "elements": ["dark"],
         "desc": "夜行的大猫头鹰，落桩无声，镖局夜哨都愿养一只看场子。",
         "stats": {"str": 10, "dex": 12, "int": 10, "vit": 9, "luk": 10},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "wx_badger", "name": "獾崽", "tier": 1, "elements": ["earth"],
         "desc": "爱刨土的小獾，山里埋的茯苓党参常被它一爪子拱出来。",
         "stats": {"str": 9, "dex": 9, "int": 7, "vit": 12, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "wx_magpie", "name": "灰喜鹊", "tier": 1, "elements": ["wind"],
         "desc": "檐下养熟的喜鹊，见了碎银珠玉就往主人袖袋里衔。",
         "stats": {"str": 6, "dex": 12, "int": 9, "vit": 7, "luk": 13},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 2},
         "passive": "treasure"},
    ],
    "modern": [
        {"id": "md_collie", "name": "边牧幼犬", "tier": 2,
         "desc": "聪明得像懂人话的边牧幼犬，一个眼神就知道该往哪边警戒。",
         "stats": {"str": 9, "dex": 11, "int": 12, "vit": 10, "luk": 9},
         "growth": {"str": 1, "dex": 2, "int": 2, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "md_doberman", "name": "杜宾幼犬", "tier": 2,
         "desc": "立耳还没完全竖起来的杜宾，护场子时凶得很，认主。",
         "stats": {"str": 12, "dex": 12, "int": 8, "vit": 10, "luk": 8},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "md_bengal", "name": "孟加拉豹猫", "tier": 2,
         "desc": "一身豹纹的猫，弹跳惊人，楼道里有点动静它先炸毛。",
         "stats": {"str": 10, "dex": 13, "int": 9, "vit": 9, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "md_shiba", "name": "柴犬", "tier": 1,
         "desc": "笑容标志的柴犬，出门爱捡东西回来——树枝、瓶盖、偶尔是钱包。",
         "stats": {"str": 9, "dex": 10, "int": 8, "vit": 10, "luk": 11},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "md_orangecat", "name": "橘猫", "tier": 1, "elements": ["light"],
         "desc": "十只橘猫九只胖的橘猫，趴在收银台边特别招财。",
         "stats": {"str": 8, "dex": 9, "int": 9, "vit": 10, "luk": 12},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 2},
         "passive": "treasure"},
        {"id": "md_hamster", "name": "金丝熊", "tier": 1,
         "desc": "腮帮子能塞下半个核桃的仓鼠，热衷把亮闪闪的小物搬回窝。",
         "stats": {"str": 6, "dex": 11, "int": 8, "vit": 8, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "md_greyparrot", "name": "灰鹦鹉", "tier": 2, "elements": ["wind"],
         "desc": "会学门铃和警报声的灰鹦鹉，陌生人上楼它先开口喝问。",
         "stats": {"str": 7, "dex": 11, "int": 13, "vit": 9, "luk": 10},
         "growth": {"str": 1, "dex": 2, "int": 2, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "md_gsp", "name": "波音猎犬", "tier": 2,
         "desc": "指哪打哪的猎鸟犬，公园里一趟遛弯能叼回来仨球两瓶盖。",
         "stats": {"str": 11, "dex": 13, "int": 10, "vit": 10, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "md_hedgehog", "name": "迷你刺猬", "tier": 1, "elements": ["earth"],
         "desc": "巴掌大的小刺猬，窝里总藏着纽扣、硬币和不知哪来的耳钉。",
         "stats": {"str": 7, "dex": 9, "int": 8, "vit": 11, "luk": 12},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 2},
         "passive": "treasure"},
    ],
    "scifi": [
        {"id": "sf_cybercheetah", "name": "仿生猎豹", "tier": 3, "elements": ["thunder"],
         "desc": "军工级仿生猎豹原型机，冲刺时关节溢出蓝色电弧。",
         "stats": {"str": 12, "dex": 15, "int": 10, "vit": 10, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "sf_robotdog", "name": "机械警犬", "tier": 2, "elements": ["metal"],
         "desc": "退役的安保机器犬，鼻子换了光谱探头，对贵金属气味灵敏。",
         "stats": {"str": 11, "dex": 12, "int": 10, "vit": 11, "luk": 8},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "sf_drillpangolin", "name": "穿山基因兽", "tier": 2, "elements": ["earth"],
         "desc": "改造自穿山甲的挖掘生物，爪部合金化，矿石层一挖一个准。",
         "stats": {"str": 12, "dex": 8, "int": 8, "vit": 13, "luk": 9},
         "growth": {"str": 2, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "sf_cargoturtle", "name": "载重甲龟机", "tier": 2, "elements": ["earth"],
         "desc": "六足运输机器人，背甲是货舱，跟着勘探队捡拾矿样。",
         "stats": {"str": 11, "dex": 7, "int": 9, "vit": 14, "luk": 8},
         "growth": {"str": 2, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "sf_microdrone", "name": "侦察蜂群", "tier": 1, "elements": ["thunder"],
         "desc": "巴掌大的无人机群，嗡嗡地织成一张移动警戒网。",
         "stats": {"str": 6, "dex": 12, "int": 11, "vit": 7, "luk": 10},
         "growth": {"str": 1, "dex": 2, "int": 2, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "sf_hoverjelly", "name": "悬浮水母体", "tier": 1, "elements": ["light"],
         "desc": "发光的悬浮生物体，对金属矿脉有趋光般的感应。",
         "stats": {"str": 6, "dex": 10, "int": 11, "vit": 8, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 2, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "sf_laserhawk", "name": "光棱猎隼", "tier": 3, "elements": ["light"],
         "desc": "义眼换成了聚光棱镜的改造隼，俯冲时翼下洒下一线灼白光束。",
         "stats": {"str": 12, "dex": 14, "int": 11, "vit": 10, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "sf_nanofox", "name": "纳米狐", "tier": 3, "elements": ["metal"],
         "desc": "银灰色流体聚成的狐形机体，受伤时会自行重组，偏爱收集稀有合金。",
         "stats": {"str": 11, "dex": 13, "int": 13, "vit": 11, "luk": 10},
         "growth": {"str": 2, "dex": 2, "int": 2, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "sf_geoslime", "name": "培养黏菌", "tier": 1, "elements": ["wood"],
         "desc": "培养舱里长出的荧光黏菌团，会缓缓爬向矿化沉积物并包裹取样。",
         "stats": {"str": 6, "dex": 8, "int": 9, "vit": 12, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
    ],
    "apocalypse": [
        {"id": "ap_twoheaded", "name": "双头犬崽", "tier": 3, "elements": ["dark"],
         "desc": "两个头轮流打盹的变异犬，永远有一双眼睛睁着。",
         "stats": {"str": 14, "dex": 9, "int": 7, "vit": 13, "luk": 8},
         "growth": {"str": 2, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "alert"},
        {"id": "ap_glowfox", "name": "荧光狐", "tier": 3, "elements": ["light"],
         "desc": "皮毛泛着幽绿荧光的变异狐，夜里的废墟中如一盏游灯。",
         "stats": {"str": 9, "dex": 13, "int": 12, "vit": 9, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 2, "vit": 1, "luk": 2},
         "passive": "treasure"},
        {"id": "ap_mutantwolf", "name": "变异狼崽", "tier": 2, "elements": ["dark"],
         "desc": "獠牙外露的狼崽，嗅得出废墟下有没有活物。",
         "stats": {"str": 12, "dex": 12, "int": 7, "vit": 10, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "alert"},
        {"id": "ap_armadillo", "name": "龟甲犰狳", "tier": 2, "elements": ["earth"],
         "desc": "甲壳厚得能挡流弹的犰狳，最会拱开瓦砾找罐头。",
         "stats": {"str": 10, "dex": 8, "int": 7, "vit": 14, "luk": 9},
         "growth": {"str": 2, "dex": 1, "int": 0, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "ap_raven", "name": "变异渡鸦", "tier": 1, "elements": ["wind"],
         "desc": "翅展半米的乌鸦，对反光的东西有执念，会叼回戒指和钥匙。",
         "stats": {"str": 7, "dex": 12, "int": 9, "vit": 8, "luk": 11},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 1},
         "passive": "treasure"},
        {"id": "ap_roach", "name": "辐射蟑螂", "tier": 1, "elements": ["earth"],
         "desc": "巴掌大的蟑螂，命硬得很，总能在废墟里翻出能用的东西。",
         "stats": {"str": 7, "dex": 9, "int": 6, "vit": 12, "luk": 10},
         "growth": {"str": 1, "dex": 1, "int": 1, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "ap_tigerkit", "name": "变异虎崽", "tier": 3, "elements": ["fire"],
         "desc": "条纹间透着灼红的小虎，呼噜声像闷雷，夜里隔着半条街都能觉出活物。",
         "stats": {"str": 14, "dex": 12, "int": 8, "vit": 12, "luk": 9},
         "growth": {"str": 2, "dex": 2, "int": 1, "vit": 2, "luk": 1},
         "passive": "alert"},
        {"id": "ap_shovelmole", "name": "铁铲鼹鼠", "tier": 2, "elements": ["earth"],
         "desc": "前爪角质化成铲板的鼹鼠，拱开的瓦砾下常藏着还能用的物资。",
         "stats": {"str": 10, "dex": 9, "int": 7, "vit": 13, "luk": 9},
         "growth": {"str": 2, "dex": 1, "int": 0, "vit": 2, "luk": 1},
         "passive": "gather_bonus"},
        {"id": "ap_phosphormoth", "name": "磷光蛾", "tier": 1, "elements": ["light"],
         "desc": "翅粉发着幽蓝磷光的蛾子，绕着值钱的旧世界遗物打转不肯走。",
         "stats": {"str": 6, "dex": 11, "int": 9, "vit": 8, "luk": 12},
         "growth": {"str": 1, "dex": 2, "int": 1, "vit": 1, "luk": 2},
         "passive": "treasure"},
    ],
}


# ---- 宠物技能：种族候选表（名字全部取自同题材 _GENRE_SKILL_TEMPLATES 现有条目，
# 元素亲和优先；不新建贫血池，守 §23 数据量铁律；test_genre_data_volume 守护每只 >=3）----
# [!] 每只 >=4 候选：技能槽上限 3，洗技能须「不与他槽重复」的候选非空（4 候选保底）。
_SPECIES_SKILL_CANDIDATES = {
    # 西幻
    "wf_dragonling": ["火球术", "巨龙咆哮", "陨星术", "斩击"],
    "wf_griffon": ["疾风步", "轻羽术", "斩击", "圣光裁决"],
    "wf_bearcub": ["大地践踏", "盾墙格挡", "斩击", "破甲锤击"],
    "wf_wolf": ["斩击", "荆棘缠绕", "疾风步", "盾墙格挡"],
    "wf_faycat": ["治愈之光", "轻羽术", "圣疗术", "圣光裁决"],
    "wf_deer": ["荆棘缠绕", "治愈之光", "生命祝福", "斩击"],
    "wf_owlcat": ["暗影噬咬", "冰锥术", "疾风步", "斩击"],
    "wf_emberfox": ["火球术", "轻羽术", "斩击", "陨星术"],
    "wf_stonegoat": ["大地践踏", "盾墙格挡", "治愈之光", "破甲锤击"],
    # 仙侠
    "xx_spiritfox": ["烈焰符", "御剑术", "炼气诀", "幽冥噬魂爪"],
    "xx_thundermarten": ["雷法·天罚", "大衍神雷", "御风诀", "御剑术"],
    "xx_turtle": ["尘遁术", "厚土遁甲", "金钟罩", "炼气诀"],
    "xx_crane": ["御风诀", "御剑术", "引气诀", "尘遁术"],
    "xx_serpent": ["玄冰刺", "幽冥噬魂爪", "尘遁术", "厚土遁甲"],
    "xx_rabbit": ["青木缠身咒", "炼气诀", "枯木回春", "御风诀"],
    "xx_fledgling": ["烈焰符", "雷法·天罚", "青莲剑歌", "御风诀"],
    "xx_iceclam": ["玄冰刺", "引气诀", "厚土遁甲", "炼气诀"],
    "xx_windweasel": ["御风诀", "御剑术", "尘遁术", "青木缠身咒"],
    # 武侠
    "wx_falcon": ["燕子三抄水", "踏雪无痕", "基础剑招", "流云飞袖"],
    "wx_mastiff": ["基础剑招", "铁砂掌", "稳马桩", "铁布衫"],
    "wx_blackbear": ["铁布衫", "霸王卸甲", "基础剑招", "吐纳法"],
    "wx_whitefox": ["踏雪无痕", "分筋错骨手", "吐纳法", "回春散"],
    "wx_monkey": ["基础剑招", "回春散", "燕子三抄水", "吐纳法"],
    "wx_palmcat": ["基础剑招", "撒手锏", "吐纳法", "稳马桩"],
    "wx_eagleowl": ["分筋错骨手", "夺命十五剑", "踏雪无痕", "铁砂掌"],
    "wx_badger": ["稳马桩", "铁布衫", "基础剑招", "吐纳法"],
    "wx_magpie": ["燕子三抄水", "踏雪无痕", "吐纳法", "基础剑招"],
    # 现代
    "md_collie": ["格斗术", "闪避步法", "急救包扎", "低姿匍匐"],
    "md_doberman": ["格斗术", "快速射击", "低姿匍匐", "肘击"],
    "md_bengal": ["格斗术", "肘击", "闪避步法", "战术翻滚"],
    "md_shiba": ["格斗术", "急救包扎", "低姿匍匐", "闪避步法"],
    "md_orangecat": ["震撼弹", "急救包扎", "格斗术", "闪避步法"],
    "md_hamster": ["低姿匍匐", "急救包扎", "闪避步法", "格斗术"],
    "md_greyparrot": ["闪避步法", "快速射击", "镇定剂注射", "低姿匍匐"],
    "md_gsp": ["格斗术", "破门炸药", "急救包扎", "镇定剂注射"],
    "md_hedgehog": ["战术翻滚", "格斗术", "急救包扎", "低姿匍匐"],
    # 科幻
    "sf_cybercheetah": ["电击脉冲", "磁轨狙击", "机动推进", "过载护盾"],
    "sf_robotdog": ["磁轨狙击", "力场护盾", "纳米修复", "电击脉冲"],
    "sf_drillpangolin": ["等离子切割", "过载护盾", "推进冲刺", "纳米修复"],
    "sf_cargoturtle": ["力场护盾", "纳米修复", "过载护盾", "推进冲刺"],
    "sf_microdrone": ["电击脉冲", "机动推进", "声波震荡", "裂解光束"],
    "sf_hoverjelly": ["裂解光束", "纳米修复", "过载护盾", "机动推进"],
    "sf_laserhawk": ["裂解光束", "轨道支援打击", "磁轨狙击", "机动推进"],
    "sf_nanofox": ["纳米修复", "相位偏移矩阵", "裂解光束", "电击脉冲"],
    "sf_geoslime": ["腐蚀弹头", "维生凝胶", "基因再生", "过载护盾"],
    # 末日
    "ap_twoheaded": ["尸潮召唤", "嗜血狂击", "病毒母巢同化", "拼刺"],
    "ap_glowfox": ["信号弹强光", "核能过载", "草药包扎", "潜行贴近"],
    "ap_mutantwolf": ["嗜血狂击", "拼刺", "潜行贴近", "废铁风暴"],
    "ap_armadillo": ["泼沙迷眼", "绷带缠身", "废铁风暴", "拼刺"],
    "ap_raven": ["潜行贴近", "拼刺", "信号弹强光", "草药包扎"],
    "ap_roach": ["泼沙迷眼", "毒藻飞刀", "草药包扎", "绷带缠身"],
    "ap_tigerkit": ["投掷炸瓶", "燃烧弹投掷", "嗜血狂击", "拼刺"],
    "ap_shovelmole": ["泼沙迷眼", "废铁风暴", "绷带缠身", "潜行贴近"],
    "ap_phosphormoth": ["信号弹强光", "草药包扎", "潜行贴近", "拼刺"],
}
for _pool in _GENRE_PET_SPECIES.values():
    for _sp in _pool:
        _sp["skill_candidates"] = list(_SPECIES_SKILL_CANDIDATES.get(_sp["id"], []))

# 被动功效说明（UI/tooltip 单一来源；数值挂点见 passive_active 三调用处）
PASSIVE_DESC = {
    "gather_bonus": "采集成功率 +10%",
    "alert": "野外遇怪率 x0.7",
    "treasure": "奇遇金币 x1.15",
}


def species_pool(genre_id: str) -> list[dict]:
    """题材种族池（缺键/空池回退西幻，守 §23）。"""
    pool = _GENRE_PET_SPECIES.get(genre_id)
    if not pool:
        pool = _GENRE_PET_SPECIES["western_fantasy"]
    return pool


def find_species(species_id: str) -> Optional[dict]:
    """按种族 id 全池查找（id 全局唯一；找不到返回 None，调用方兜底）。"""
    sid = str(species_id or "").strip()
    if not sid:
        return None
    for pool in _GENRE_PET_SPECIES.values():
        for sp in pool:
            if sp.get("id") == sid:
                return sp
    return None


def genre_id_of(world: Any) -> str:
    """读世界题材 id（config_overlay.attribute_template_id，兜底西幻）。"""
    ov = getattr(world, "config_overlay", None) or {}
    if isinstance(ov, dict):
        return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")
    return "western_fantasy"


def roll_species(world: Any, rng: SeededRng) -> dict:
    """确定性抽一只种族（均匀抽取；题材回退西幻）。"""
    return rng.pick(species_pool(genre_id_of(world)))


# ---- 宠物技能 ----
def species_skill_candidates(species: dict) -> list[str]:
    """种族技能候选名表（挂 species dict 的 skill_candidates，注入见模块级候选表）。"""
    return list((species or {}).get("skill_candidates") or [])


def species_skill_def(genre_id: str, name: str) -> Optional[dict]:
    """按名从同题材技能池解析宠物技能定义（缺键回退西幻；找不到 None）。

    [!] 懒导入 world_sim_service：模块级互引会环（world_sim_service 顶部 import 本模块）。
    """
    from src.services.world_sim_service import _GENRE_SKILL_TEMPLATES
    pool = _GENRE_SKILL_TEMPLATES.get(genre_id) or _GENRE_SKILL_TEMPLATES["western_fantasy"]
    for sk in pool:
        if sk.get("name") == name:
            return sk
    return None


def pet_skill_slots(level: int) -> int:
    """宠物技能槽：驯服 1 槽 + Lv4 + Lv8 各开 1（钳 3）。"""
    lv = max(1, int(level))
    return min(3, 1 + (1 if lv >= 4 else 0) + (1 if lv >= 8 else 0))


def fill_pet_skill_slots(pet: Pet) -> None:
    """把技能补到等级应得槽数（确定性：盐仅 pet.id+槽号，跨进程同结果）。

    候选撞已有时按候选表序取下一个未占（有界）；种族无候选表则不填。
    """
    want = pet_skill_slots(pet.level)
    skills = [str(s) for s in (pet.skills or []) if isinstance(s, str) and s]
    cands = species_skill_candidates(find_species(pet.species) or {})
    for slot in range(len(skills), want):
        if not cands:
            break
        rng = SeededRng.seed_from(pet.id, slot, "pet_skill")
        name = rng.pick(cands)
        if name in skills:
            alt = [c for c in cands if c not in skills]
            if not alt:
                break
            name = alt[0]
        skills.append(name)
    pet.skills = skills[:3]


def pet_stats(world: Any, pet: Pet) -> dict:
    """宠物五维面板（种族 base + growth x (level-1)，乘资质系数；build_pet_unit 同源）。

    资质维度 -> stat：atk->str / spd->dex / mp->int / max(def,hp)->vit / luk 基线 1.0。
    """
    sp = find_species(pet.species) or species_pool("western_fantasy")[0]
    base = sp.get("stats") or {}
    growth = sp.get("growth") or {}
    lv = max(1, min(PET_LEVEL_CAP, int(pet.level)))
    from src.services import refine_engine as rfe
    mults = {
        "str": rfe.aptitude_mult(pet, "atk"), "dex": rfe.aptitude_mult(pet, "spd"),
        "int": rfe.aptitude_mult(pet, "mp"),
        "vit": max(rfe.aptitude_mult(pet, "def"), rfe.aptitude_mult(pet, "hp")),
        "luk": 1.0,
    }
    out = {}
    for key in ("str", "dex", "int", "vit", "luk"):
        b = max(1, int(base.get(key, 8) or 8))
        g = max(0, int(growth.get(key, 0) or 0))
        out[key] = max(1, int((b + g * (lv - 1)) * mults[key]))
    return out


def pet_count(world: Any) -> int:
    pl = getattr(world, "player", None)
    return len([x for x in (getattr(pl, "pet_ids", None) or []) if isinstance(x, str)])


def pets_full(world: Any) -> bool:
    return pet_count(world) >= PET_MAX


def create_pet(world: World, species: dict) -> Pet:
    """驯服入队：建 Pet 实体并挂 World.pets + player.pet_ids；首只自动设为出战跟随。

    [!] 调用前须自证未满员（pets_full）——满员由渠道层拦截（奇遇 tame 满员不出、
    野外幼崽满员不 roll），引擎不在此重复钳制。
    """
    pet = Pet(
        name=str(species.get("name", "") or "宠物"),
        species=str(species.get("id", "") or ""),
        tier=max(1, min(3, int(species.get("tier", 1) or 1))),
        level=1, xp=0, affinity=PET_INIT_AFFINITY,
    )
    world.pets.append(pet)
    world.player.pet_ids.append(pet.id)
    if not getattr(world.player, "active_pet_id", ""):
        world.player.active_pet_id = pet.id
    fill_pet_skill_slots(pet)  # 驯服即得槽 1 技能（确定性）
    return pet


def pet_xp_next(level: int) -> int:
    """升级阈值曲线（同 world+公式确定性，封顶前有效）。"""
    return int(PET_XP_BASE * (max(1, int(level)) ** 1.15))


def gain_pet_xp(pet: Pet, amount: int) -> int:
    """出战胜利经验：跨多级连升，封顶后 xp 清零。返回本次升了几级。

    升级顺带补开技能槽（fill_pet_skill_slots 幂等，未升槽时 no-op）。
    """
    pet.xp += max(0, int(amount))
    levels = 0
    while pet.level < PET_LEVEL_CAP and pet.xp >= pet_xp_next(pet.level):
        pet.xp -= pet_xp_next(pet.level)
        pet.level += 1
        levels += 1
    if pet.level >= PET_LEVEL_CAP:
        pet.xp = 0
    if levels:
        fill_pet_skill_slots(pet)
    return levels


def _player_item(world: Any, player: Any, item_id: str):
    for it in (getattr(world, "items", None) or []):
        if getattr(it, "id", "") == item_id:
            return it
    return None


def feed_affinity_gain(item: Any) -> int:
    """投食亲和提升按品质分档；越界/缺 rarity 回退 common 档（守白名单回退口径）。"""
    r = str(getattr(item, "rarity", "") or "")
    return int(FEED_AFFINITY_BY_RARITY.get(r, FEED_AFFINITY))


def feed_pet(world: Any, player: Any, pet: Pet, item_id: str) -> tuple[bool, str]:
    """投食：消耗背包 1 件宠物食品（category=="pet_food"），亲和按品质提升钳 100。

    返回 (是否成功, 提示消息)。口径：只宠物食品可投食（丹药/鉴定卷等消耗品不可当宠粮，
    收口「默认喂第一件消耗品」不合理）；提升量 feed_affinity_gain（品质分档）。"""
    iid = str(item_id or "").strip()
    inv = getattr(player, "inventory", None)
    if not isinstance(inv, list) or iid not in inv:
        return False, "背包中没有该物品"
    it = _player_item(world, player, iid)
    if it is None or effective_category(it) != "pet_food":
        return False, "只能投食宠物食品"
    inv.remove(iid)
    gain = feed_affinity_gain(it)
    before = int(pet.affinity)
    pet.affinity = max(0, min(100, before + gain))
    return True, (f"投喂了「{getattr(it, 'name', iid)}」（+{gain}），"
                  f"亲和 {before} -> {pet.affinity}")


def active_pet(world: Any) -> Optional[Pet]:
    """出战/跟随中的宠物（active_pet_id 指向且实体存在；失效引用返回 None）。

    [!] world 无 player 属性（SimpleNamespace 测试夹具）时返回 None，不抛。
    """
    pl = getattr(world, "player", None)
    if pl is None:
        return None
    pid = str(getattr(pl, "active_pet_id", "") or "")
    if not pid:
        return None
    for p in (getattr(world, "pets", None) or []):
        if isinstance(p, Pet) and p.id == pid:
            return p
    return None


def passive_active(world: Any, passive: str) -> bool:
    """种族被动是否生效：出战宠物亲和 >= 50 且其种族被动匹配。"""
    if passive not in _PASSIVE_VALUES:
        return False
    pet = active_pet(world)
    if pet is None or pet.affinity < AFFINITY_PASSIVE_AT:
        return False
    sp = find_species(pet.species)
    return bool(sp) and sp.get("passive") == passive


def passive_label(world: Any, pet: Pet) -> str:
    """宠物被动状态一行（场景上下文/对话框共用单一来源）。"""
    sp = find_species(pet.species) or {}
    name = PASSIVE_ZH.get(str(sp.get("passive", "")), "")
    if not name:
        return ""
    unlocked = pet.affinity >= AFFINITY_PASSIVE_AT
    return f"{name}({'已生效' if unlocked else f'亲和{AFFINITY_PASSIVE_AT}解锁'})"


def build_pet_unit(world: Any, pet: Pet) -> "ce.CombatUnit":
    """出战战斗单位：种族模板五维（pet_stats 同源面板）+ 宠物技能（冷却制）。

    仿 _build_ally_unit 的己方口径（npc_id 留空——宠物不是 NPC，战后不写回 hp），
    role_label="宠物"；数值确定性（同种族同等级同结果，无 rng 抖动——宠物是玩家
    长线养成资产，面板应可预期）。技能 = pet.skills 按名解析题材技能池，cost_mp
    强制 0（宠物 mp_max=0，走冷却制；combat_engine 的 cost 门 0>0 恒过）。
    """
    sp = find_species(pet.species) or species_pool("western_fantasy")[0]
    lv = max(1, min(PET_LEVEL_CAP, int(pet.level)))
    stats = pet_stats(world, pet)
    ent = SimpleNamespace(
        name=pet.name, level=lv,
        stat_str=stats["str"], stat_dex=stats["dex"],
        stat_int=stats["int"], stat_vit=stats["vit"],
        stat_luk=stats["luk"],
        elements=list(sp.get("elements") or []),
    )
    snap = ce.compute_stats(ent)
    skills = []
    for nm in (pet.skills or []):
        sk = species_skill_def(genre_id_of(world), str(nm))
        if sk is None:
            continue
        sk2 = dict(sk)
        sk2["cost_mp"] = 0  # 宠物无蓝，冷却制
        skills.append(sk2)
    return ce.CombatUnit(
        name=pet.name, snapshot=snap, mp=0, mp_max=0,
        skills=skills, cd={}, talents=[], ai="balanced",
        role_label="宠物",
    )


def find_feed_item(world: Any, player: Any):
    """背包里第一件宠物食品（驯服自动投食用；无则 None）。"""
    item_by_id = {getattr(it, "id", ""): it for it in (getattr(world, "items", None) or [])}
    for iid in (getattr(player, "inventory", None) or []):
        it = item_by_id.get(iid)
        if it is not None and effective_category(it) == "pet_food":
            return it
    return None


def tame_check(world: Any, player: Any, species: dict, danger: int,
               rng: SeededRng) -> dict:
    """驯服检定：dex/luk 取高驱动 success_chance（difficulty = danger x 5）。

    背包有消耗品时自动投食 1 件 +0.15（成败都消耗——诱饵本就是代价）。
    返回 {success, chance, fed_item}（fed_item 为投食物品名，空串 = 未投食）。
    [!] 调用前须自证未满员（pets_full），本函数不做容量检查。
    """
    stat = max(te.effective_stat(player, "dex"), te.effective_stat(player, "luk"))
    chance = ce.success_chance(stat, tool_bonus=0, difficulty=max(0, int(danger)) * 5,
                               base=TAME_BASE, global_penalty=ce.difficulty_penalty_of(world))
    fed = find_feed_item(world, player)
    fed_name = ""
    if fed is not None:
        chance = min(0.95, chance + TAME_FEED_BONUS)
        iid = getattr(fed, "id", "")
        inv = getattr(player, "inventory", None)
        if isinstance(inv, list) and iid in inv:
            inv.remove(iid)
            fed_name = getattr(fed, "name", iid)
    success = rng.chance(chance)
    return {"success": bool(success), "chance": round(chance, 4), "fed_item": fed_name}
