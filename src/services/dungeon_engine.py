"""[P25a] 秘境地牢引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

架构分层（守 §21）：LLM 出语义（名称/主题经地图拓展渠道注入，缺省题材池兜底），
引擎算结构——层数 3-5 / 每层 3-4 房间（类型分布 战斗40% 检定20% 宝藏15% 陷阱15%
休息10%）/ Boss 层固定 1 Boss 房（小弟 1-2 由 start_combat 兜底派生）全 SeededRng
确定性铺层（同 world+dungeon 同布局；重生同 seed，clears 提升怪物难度）。

探索复用场景循环：秘境内部 = 单个 kind="dungeon" Location（interior_location 建，
连到入口）；「深入」= apply_intent move 特判 -> advance 逐房推进（战斗房产
forced_combat 临时怪，不入 world.npcs 守图鉴口径）；胜利钩子 on_combat_victory
由 finish_combat 调（Boss 击杀 -> 技能书按公式 roll + epic 保底掉落 + 封印冷却）。
中途退出保留房间状态（done/pending 随 World JSON 落盘），重进续探。
题材池容量守 §23 数据量铁律（tests/test_genre_data_volume.py TestDungeonPool 守护）。
"""
from __future__ import annotations

from typing import Any, Optional

from src.models.world import World, Location, NPC, Dungeon, DungeonFloor, DungeonRoom
from src.models.world import _clean_elements
from src.models.world_sim_preset import GenreText
from src.services import combat_engine as ce
from src.services import encounter_engine as ene
from src.services import talent_engine as te
from src.services import wilderness_engine as we
from src.services import dungeon_tools as dt
from src.utils.rng import SeededRng

# ---- 数值旋钮（纯引擎常量）----
FLOOR_MIN, FLOOR_MAX = 3, 5          # 层数 3-5
ROOMS_MIN, ROOMS_MAX = 3, 4          # 每层房间数（Boss 层固定 1 Boss 房）
ROOM_WEIGHTS = (("combat", 40), ("check", 20), ("treasure", 15), ("trap", 15), ("rest", 10))
COOLDOWN_DAYS = 5                    # 通关封印天数（到期 tick 重铺）
MONSTER_LEVEL_CAP = 100           # [P43] 危险度分段后放开（原 12 防数值爆炸）
TRAP_DAMAGE_PCT = 0.12               # 陷阱失败 HP 损耗占 hp_max 比例
TRAP_GUARD_CHANCE = 0.35             # 陷阱失败惊动守卫（强制战斗）概率

# ---- 题材化秘境主题池（6 题材各 >=6，缺键回退西幻）----
_GENRE_DUNGEON_THEMES: dict[str, list[dict]] = {
    "xianxia": [
        {"id": "xx_sword_tomb", "name": "上古剑冢", "desc": "万剑归葬之地，剑鸣昼夜不绝"},
        {"id": "xx_alchemy_cave", "name": "废弃丹窟", "desc": "丹炉余温未散，药香混着焦糊"},
        {"id": "xx_demon_tower", "name": "锁妖塔基", "desc": "倒塌古塔的地基，封印符箓层层剥落"},
        {"id": "xx_cultivator_manor", "name": "魔修洞府", "desc": "阴气森森的洞府，石壁上残留血色功法"},
        {"id": "xx_spirit_mine", "name": "灵矿深脉", "desc": "被采空的灵矿支脉，深处仍有微光流转"},
        {"id": "xx_teleport_rune", "name": "古传送阵", "desc": "失效的上古传送阵，阵纹间空间微微扭曲"},
        {"id": "xx_battlefield_relic", "name": "古战场遗迹", "desc": "上古修士大战之地，剑气余威未消"},
        {"id": "xx_beast_garden", "name": "灵兽园废墟", "desc": "宗门豢养灵兽的园子，兽栏大多空了"},
    ],
    "wuxia": [
        {"id": "wx_bandit_fort", "name": "黑风寨旧址", "desc": "官军剿灭的山寨，刀痕箭孔犹在"},
        {"id": "wx_sword_villa", "name": "剑庄废墟", "desc": "名门剑庄的残垣，演武场青砖染血"},
        {"id": "wx_stone_grotto", "name": "武藏石窟", "desc": "刻满拳谱剑招的石窟，守窟人早已不在"},
        {"id": "wx_granary", "name": "太平粮仓", "desc": "前朝官仓，地窖深处的米香变成了霉味"},
        {"id": "wx_master_tomb", "name": "师祖墓陵", "desc": "武学圣地禁地，陪葬的不止兵器"},
        {"id": "wx_lonely_temple", "name": "黄山孤观", "desc": "荒废道观，三清像后别有洞天"},
        {"id": "wx_water_jail", "name": "漕帮水牢", "desc": "漕帮私设的水牢，铁栅泡得发黑"},
        {"id": "wx_silver_mine", "name": "私矿矿营", "desc": "私开银矿的营盘，工棚下暗道纵横"},
    ],
    "modern": [
        {"id": "md_subway_tunnel", "name": "废弃地铁隧道", "desc": "停运多年的隧道，应急灯忽明忽暗"},
        {"id": "md_lab", "name": "生物实验室", "desc": "封锁的生物实验室，培养舱里泡着不明物体"},
        {"id": "md_dead_mall", "name": "歇业商场", "desc": "卷帘门半落的商场，扶梯积灰结网"},
        {"id": "md_civil_bunker", "name": "人防工事", "desc": "冷战时期的人防工程，铁门后一间连一间"},
        {"id": "md_old_manor", "name": "凶宅老宅", "desc": "传闻闹鬼的民国老宅，地板下有暗格"},
        {"id": "md_factory", "name": "停工工厂", "desc": "破产工厂车间，机器上还挂着工牌"},
        {"id": "md_nightclub", "name": "停业夜总会", "desc": "撤场一半的夜总会，包厢里账本散落"},
        {"id": "md_old_school", "name": "迁走的老校舍", "desc": "合并后废弃的校舍，生物教室标本仍在"},
    ],
    "scifi": [
        {"id": "sf_derelict_hull", "name": "漂流残骸", "desc": "失联货船的残骸段，气闸还能手动开"},
        {"id": "sf_research_station", "name": "荒废科考站", "desc": "断电的行星科考站，日志停在三十年前"},
        {"id": "sf_mining_rig", "name": "采矿平台", "desc": "废弃的深坑采矿平台，升降机卡在半途"},
        {"id": "sf_military_vault", "name": "军用仓库", "desc": "封锁的军需仓库，库门需要残存权限"},
        {"id": "sf_data_crypt", "name": "数据墓场", "desc": "报废服务器的存放库，散热扇偶有转动"},
        {"id": "sf_crash_zone", "name": "坠落区", "desc": "坠毁穿梭机的残骸散落带，货舱较完整"},
        {"id": "sf_terra_lab", "name": "地化改造站", "desc": "半途而废的地化改造站，菌毯爬满墙面"},
        {"id": "sf_orbital_prison", "name": "轨道囚舱", "desc": "脱离管理的轨道监狱，牢门半开"},
    ],
    "apocalypse": [
        {"id": "ap_subway_bunker", "name": "地铁避难所", "desc": "幸存者放弃的地铁站点，沙袋后一片狼藉"},
        {"id": "ap_sewer_nest", "name": "下水道巢穴", "desc": "变异生物聚居的排水系统，壁上有抓痕"},
        {"id": "ap_radio_tower", "name": "广播大厦", "desc": "还能供电的广播楼，顶层发射着无人应答的信号"},
        {"id": "ap_dead_supermarket", "name": "大型超市", "desc": "被搬空的卖场，货架夹层或还有漏网之鱼"},
        {"id": "ap_gas_ruins", "name": "加油站废墟", "desc": "半塌的加油站，地下油罐口敞着"},
        {"id": "ap_plague_hospital", "name": "传染病院", "desc": "疫情时挤爆的医院，走廊推床横陈"},
        {"id": "ap_cold_storage", "name": "冷库废场", "desc": "断电的肉类冷库，冻品化成了黑水"},
        {"id": "ap_church_shelter", "name": "教堂避难点", "desc": "堆满行囊的教堂地下室，长椅排成了铺"},
    ],
    "western_fantasy": [
        {"id": "wf_barrow", "name": "古冢地宫", "desc": "先王长眠的地宫，甬道两侧立着风化石像"},
        {"id": "wf_wizard_tower", "name": "巫师塔遗迹", "desc": "坍塌一半的法塔，底层密室封着魔法锁"},
        {"id": "wf_dragon_lair", "name": "龙巢", "desc": "巨龙弃巢，金堆深处仍有灼热气息"},
        {"id": "wf_saint_crypt", "name": "圣者陵寝", "desc": "圣者的地下墓穴，圣光石的光辉日渐微弱"},
        {"id": "wf_deep_shaft", "name": "矿坑深处", "desc": "废弃矿坑的深层巷道，矿车轨道锈死"},
        {"id": "wf_elf_ruin", "name": "精灵废殿", "desc": "藤蔓缠绕的精灵神殿，月光石依旧发亮"},
        {"id": "wf_smuggler_cave", "name": "走私者洞窟", "desc": "走私犯开凿的洞窟，暗格里藏着货"},
        {"id": "wf_arena_pit", "name": "斗兽场地下室", "desc": "古斗兽场的地下兽栏，铁链锈成一体"},
    ],
}

# ---- 题材化房间文案池（6 题材各 >=12，五类房型各 >=2）----
# 每条 {type, name, desc}：铺层时按 type 抽名；name/desc 纯展示（玩家层 + 叙事 LLM 素材）。
_GENRE_DUNGEON_ROOM_TEXTS: dict[str, list[dict]] = {
    "xianxia": [
        {"type": "combat", "name": "剑冢傀儡", "desc": "守冢剑傀提剑而立，剑锋齐指来人"},
        {"type": "combat", "name": "丹窟火妖", "desc": "丹火凝成的妖物在药渣间游荡"},
        {"type": "combat", "name": "锁妖石兽", "desc": "镇塔石兽挣脱半边封印，低声咆哮"},
        {"type": "trap", "name": "剑气禁制", "desc": "甬道中残留剑气纵横，稍有不慎便被割伤"},
        {"type": "trap", "name": "丹毒瘴气", "desc": "积年药毒化成的瘴气在洞口盘旋"},
        {"type": "trap", "name": "坠石机关", "desc": "头顶碎石被踏机牵动，簌簌作响"},
        {"type": "check", "name": "古阵谜纹", "desc": "地上阵纹缺了一角，需以灵识推演补全"},
        {"type": "check", "name": "功法石壁", "desc": "石壁功法图谱残缺，参悟需过目不忘"},
        {"type": "treasure", "name": "遗府宝匣", "desc": "前人遗府的储宝匣静卧案头"},
        {"type": "treasure", "name": "灵石矿脉", "desc": "未采尽的灵石矿脉在壁上泛光"},
        {"type": "rest", "name": "聚灵静室", "desc": "洞府深处的静室灵气尚存，宜打坐调息"},
        {"type": "rest", "name": "洞府丹房", "desc": "丹房蒲团完好，可歇脚恢复"},
        {"type": "combat", "name": "尸傀巡卫", "desc": "洞府巡卫尸傀闻声转身，关节咔咔作响"},
        {"type": "combat", "name": "护阵灵禽", "desc": "守阵灵禽振翅，风刃随羽而落"},
        {"type": "trap", "name": "幻阵迷径", "desc": "雾气中路径来回打转，须凝神破幻"},
        {"type": "check", "name": "残缺棋局", "desc": "石桌上残局暗含杀机，落错一子满盘皆输"},
        {"type": "treasure", "name": "前辈遗蜕", "desc": "蒲团上端坐的遗蜕前摆着储物袋"},
        {"type": "rest", "name": "灵泉眼", "desc": "洞底灵泉汩汩，饮之能缓疲乏"},
    ],
    "wuxia": [
        {"type": "combat", "name": "黑风残匪", "desc": "漏网的残匪占了地道，见人就抢"},
        {"type": "combat", "name": "护庄武师", "desc": "殉庄武师的尸身不腐，仍守着旧主家业"},
        {"type": "combat", "name": "石窟武僧", "desc": "守窟武僧入了魔障，拳风虎虎"},
        {"type": "trap", "name": "暗器连弩", "desc": "墙内连弩蓄势待发，机簧声细不可闻"},
        {"type": "trap", "name": "翻板陷坑", "desc": "青砖下是丈深陷坑，坑底竹签泛黑"},
        {"type": "trap", "name": "毒烟地道", "desc": "地道的风口残留迷烟，闻之头昏"},
        {"type": "check", "name": "武学壁画", "desc": "壁画招式环环相扣，须拆解方能通过"},
        {"type": "check", "name": "机关铜人", "desc": "木人巷的铜人阵须寻隙闯过"},
        {"type": "treasure", "name": "庄主密库", "desc": "庄主的私藏密库门扉半开"},
        {"type": "treasure", "name": "兵器架子", "desc": "落灰的兵器架上仍有几件好货"},
        {"type": "rest", "name": "演武偏房", "desc": "演武场偏房尚有干净草垫"},
        {"type": "rest", "name": "石窟禅房", "desc": "石窟深处的禅房清幽无扰"},
        {"type": "combat", "name": "掘金贼伙", "desc": "摸进墓陵掘金的贼伙抄起洛阳铲"},
        {"type": "combat", "name": "夺宝镖客", "desc": "同为寻宝而来的镖客拔刀相向"},
        {"type": "trap", "name": "连环翻板", "desc": "前脚刚过翻板便连响，得贴墙走"},
        {"type": "check", "name": "断句剑谱", "desc": "剑谱缺了口诀次序，须重新断句"},
        {"type": "treasure", "name": "暗格佛龛", "desc": "佛像背后的暗格藏着的不止经卷"},
        {"type": "rest", "name": "守墓耳房", "desc": "守墓人住过的耳房灶膛还能生火"},
    ],
    "modern": [
        {"type": "combat", "name": "失控保安", "desc": "值守保安神情异常，抄起警棍逼近"},
        {"type": "combat", "name": "实验体", "desc": "培养舱破开的实验体正在觅食"},
        {"type": "combat", "name": "地盘流浪汉", "desc": "占地为王的流浪汉团伙围了上来"},
        {"type": "trap", "name": "漏电配电室", "desc": "配电室积水带电，电缆噼啪作响"},
        {"type": "trap", "name": "坍塌扶梯", "desc": "锈蚀的扶梯随时会垮塌"},
        {"type": "trap", "name": "瓦斯房间", "desc": "房间里煤气浓度极高，一点火星就炸"},
        {"type": "check", "name": "加密门禁", "desc": "门禁键盘需要推算出管理员密码"},
        {"type": "check", "name": "档案密码柜", "desc": "档案柜的转锁暗藏规律"},
        {"type": "treasure", "name": "仓库存货", "desc": "后仓整箱的货还没来得及搬走"},
        {"type": "treasure", "name": "金库遗物", "desc": "撬开一半的保险柜里另有夹层"},
        {"type": "rest", "name": "值班室", "desc": "值班室的床铺还算干净，可以歇脚"},
        {"type": "rest", "name": "急诊病房", "desc": "留观的病床躺下就能眯一会"},
        {"type": "combat", "name": "看场打手", "desc": "受雇看场的打手拎着钢管堵住通道"},
        {"type": "combat", "name": "守库黑背", "desc": "前任主人留下的猛犬守着库门"},
        {"type": "trap", "name": "电梯井", "desc": "电梯门虚掩着，井道深不见底"},
        {"type": "check", "name": "监控主机", "desc": "监控主机还开着，回放里有线索"},
        {"type": "treasure", "name": "柜台现金盒", "desc": "收银柜台下的现金盒没人动过"},
        {"type": "rest", "name": "库房纸箱", "desc": "成摞的纸箱铺开能凑合一晚"},
    ],
    "scifi": [
        {"type": "combat", "name": "安保机甲", "desc": "巡防机甲识别失效，将你列为入侵者"},
        {"type": "combat", "name": "变异实验体", "desc": "基因泄漏区的变异体循声而来"},
        {"type": "combat", "name": "拾荒无人机群", "desc": "拾荒无人机群锁定了你身上的每件装备"},
        {"type": "trap", "name": "电弧走廊", "desc": "走廊电缆裸露，电弧按节拍炸响"},
        {"type": "trap", "name": "失压舱段", "desc": "舱段气密失效，稍停便觉缺氧"},
        {"type": "trap", "name": "激光栅栏", "desc": "安保激光栅栏仍未断电"},
        {"type": "check", "name": "加密终端", "desc": "终端需要破解残存的访问口令"},
        {"type": "check", "name": "机械谜锁", "desc": "舱门的齿轮谜锁需按序拨动"},
        {"type": "treasure", "name": "物资舱", "desc": "封存的物资舱清单还贴在门上"},
        {"type": "treasure", "name": "军械柜", "desc": "军械柜的电子锁电量将尽"},
        {"type": "rest", "name": "休眠舱", "desc": "空置的休眠舱正好补一觉"},
        {"type": "rest", "name": "维修站", "desc": "维修站供氧正常，宜稍作休整"},
        {"type": "combat", "name": "防御炮塔", "desc": "穹顶的自动炮塔转过来锁定热源"},
        {"type": "combat", "name": "狂热拾荒队", "desc": "红了眼的拾荒队要扒你身上每颗螺丝"},
        {"type": "trap", "name": "冷却剂管廊", "desc": "白雾滚滚的冷却剂管廊，冻得刺骨"},
        {"type": "check", "name": "基因锁样本", "desc": "舱门要管理员基因样本才能解锁"},
        {"type": "treasure", "name": "数据黑匣", "desc": "坠毁段的黑匣还在读写槽里"},
        {"type": "rest", "name": "生态舱绿洲", "desc": "小生态舱植物仍活，空气清甜"},
    ],
    "apocalypse": [
        {"type": "combat", "name": "变异犬群", "desc": "瘦得脱形的犬群红着眼围拢"},
        {"type": "combat", "name": "拾荒者据点", "desc": "据守此处的拾荒者不欢迎外人"},
        {"type": "combat", "name": "腐烂感染者", "desc": "听到响动的感染者从阴影里爬出"},
        {"type": "trap", "name": "绊线陷阱", "desc": "幸存者布的绊线连着霰弹枪"},
        {"type": "trap", "name": "坍塌楼板", "desc": "上方楼板悬而未落，脚下吱呀作响"},
        {"type": "trap", "name": "毒雾房间", "desc": "化学泄漏的房间绿雾不散"},
        {"type": "check", "name": "保险柜暗码", "desc": "办公室保险柜的暗码藏在遗物里"},
        {"type": "check", "name": "药房清点", "desc": "药房储柜的钥匙得从值班表推"},
        {"type": "treasure", "name": "物资仓库", "desc": "上锁的仓库里码着整托盘物资"},
        {"type": "treasure", "name": "售货机存货", "desc": "被撬开的自动售货机夹层还有存货"},
        {"type": "rest", "name": "临时营地", "desc": "前人留下的营地还能遮风"},
        {"type": "rest", "name": "病房角落", "desc": "病房角落的床垫避开了穿堂风"},
        {"type": "combat", "name": "巨鼠女王", "desc": "鼠群簇拥的巨鼠女王龇出橙色的牙"},
        {"type": "combat", "name": "变异猎犬", "desc": "套着烂皮项圈的猎犬变体低吼逼近"},
        {"type": "trap", "name": "烧穿地板", "desc": "烧穿的地板下是塌了半边的地下室"},
        {"type": "check", "name": "电台频段", "desc": "旧电台要调对频段才能收到广播"},
        {"type": "treasure", "name": "应急储藏间", "desc": "贴着封条的应急储藏间门锁完好"},
        {"type": "rest", "name": "天台温室", "desc": "住户搭的天台温室挡风又透气"},
    ],
    "western_fantasy": [
        {"type": "combat", "name": "骷髅守卫", "desc": "披甲的骷髅守卫从壁龛中站起"},
        {"type": "combat", "name": "兽人掠夺者", "desc": "占了地宫的兽人正在分赃"},
        {"type": "combat", "name": "石化蜥蜴", "desc": "洞穴蜥蜴的鳞片泛着岩石光泽"},
        {"type": "trap", "name": "毒镖机关", "desc": "墙孔毒镖与踏板相连"},
        {"type": "trap", "name": "塌陷地砖", "desc": "地砖下是深坑，尘网掩着边界"},
        {"type": "trap", "name": "诅咒祭坛", "desc": "黑曜石祭坛的诅咒波纹隐隐作痛"},
        {"type": "check", "name": "符文谜门", "desc": "石门的符文须按月相排列"},
        {"type": "check", "name": "古代铭文", "desc": "古语铭文记载着开门的祷词"},
        {"type": "treasure", "name": "宝箱密室", "desc": "密室的宝箱覆着百年尘埃"},
        {"type": "treasure", "name": "龙巢金堆", "desc": "金堆边缘散落着未熔的宝石"},
        {"type": "rest", "name": "祭司静室", "desc": "静室残留祝福之光，可安神休整"},
        {"type": "rest", "name": "篝火残厅", "desc": "先行者留下的篝火尚有余温"},
        {"type": "combat", "name": "地宫蝠群", "desc": "惊起的蝠群遮蔽火把，利爪掠颈"},
        {"type": "combat", "name": "尸鬼掘墓者", "desc": "掘墓的尸鬼拖着半袋陪葬品扑来"},
        {"type": "trap", "name": "尖刺坠网", "desc": "绊索一响，天花板的坠网兜头罩下"},
        {"type": "check", "name": "星象机关", "desc": "穹顶星图缺了主星，需拨正星盘"},
        {"type": "treasure", "name": "祭祀金器", "desc": "祭坛后的金器在火光下发亮"},
        {"type": "rest", "name": "岗哨旧铺", "desc": "哨位旁的旧铺盖还能将就一晚"},
    ],
}

# ---- 题材化 Boss 头衔池（6 题材各 >=4；Boss 名 = 主题名 + 头衔）----
_GENRE_DUNGEON_BOSS_TITLES: dict[str, list[str]] = {
    "xianxia": ["剑灵", "妖主", "魔尊", "老祖", "妖皇", "魔将"],
    "wuxia": ["寨主", "庄主", "长老", "魔头", "帮主", "剑魔"],
    "modern": ["首脑", "大佬", "主谋", "狂人", "首犯", "枭雄"],
    "scifi": ["核心机", "主宰单元", "失控AI", "中枢", "主机", "统治者单元"],
    "apocalypse": ["尸王", "巢主", "头目", "母体", "暴君", "瘟疫源"],
    "western_fantasy": ["墓主", "巫妖", "恶龙", "看守者", "大巫妖", "深渊领主"],
}


def theme_pool(genre_id: str) -> list[dict]:
    pool = _GENRE_DUNGEON_THEMES.get(genre_id)
    return pool if pool else _GENRE_DUNGEON_THEMES["western_fantasy"]


def room_texts(genre_id: str, rtype: str = "") -> list[dict]:
    pool = _GENRE_DUNGEON_ROOM_TEXTS.get(genre_id) or _GENRE_DUNGEON_ROOM_TEXTS["western_fantasy"]
    if rtype:
        pool = [t for t in pool if t.get("type") == rtype]
    return pool


def boss_titles(genre_id: str) -> list[str]:
    pool = _GENRE_DUNGEON_BOSS_TITLES.get(genre_id)
    return pool if pool else _GENRE_DUNGEON_BOSS_TITLES["western_fantasy"]


def genre_id_of(world: Any) -> str:
    ov = getattr(world, "config_overlay", None) or {}
    if isinstance(ov, dict):
        return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")
    return "western_fantasy"


def _hidden_check_enabled(world: Any) -> bool:
    """[P33] 隐藏检定开关（preset.hidden_check_enabled；world.config_overlay 可覆盖；默认开）。"""
    ov = getattr(world, "config_overlay", None) or {}
    if isinstance(ov, dict) and "hidden_check_enabled" in ov:
        return bool(ov["hidden_check_enabled"])
    return True


def floor_mult(floor_index: int) -> float:
    """深层奖励倍率（按层序 1 起：1.0 / 1.2 / 1.4 ...）。"""
    return 1.0 + 0.2 * max(0, int(floor_index) - 1)


# ============ 查找 ============
def dungeon_at(world: Any, loc: Any, discovered_only: bool = False) -> Optional[Dungeon]:
    """入口挂在本地点的秘境（每地点限一个）。discovered_only=True 只返回已发现的
    （D03：未发现的不进 UI/上下文；服务层内部用默认 False 保持建图/拓展逻辑）。"""
    if loc is None:
        return None
    lid = getattr(loc, "id", "")
    for d in (getattr(world, "dungeons", None) or []):
        if isinstance(d, Dungeon) and d.location_id == lid:
            if discovered_only and not getattr(d, "discovered", False):
                continue
            return d
    return None


def interior_dungeon(world: Any, loc: Any) -> Optional[Dungeon]:
    """loc 是秘境内部地点时返回对应秘境。"""
    if loc is None or getattr(loc, "kind", "") != "dungeon":
        return None
    for d in (getattr(world, "dungeons", None) or []):
        if isinstance(d, Dungeon) and d.interior_id == loc.id:
            return d
    return None


def entrance_location(world: Any, dungeon: Dungeon) -> Optional[Location]:
    return next((l for l in (getattr(world, "locations", None) or []) if l.id == dungeon.location_id), None)


def interior_location(world: World, dungeon: Dungeon) -> Location:
    """取/建秘境内部地点（每秘境一个，kind="dungeon"，与入口双向连通；幂等）。"""
    loc = next((l for l in world.locations if l.id == dungeon.interior_id), None)
    if loc is not None:
        return loc
    ent = entrance_location(world, dungeon)
    loc = Location(
        id=dungeon.interior_id, name=f"{dungeon.name}·内部",
        desc=f"{dungeon.name}的深处：{dungeon_theme_desc(world, dungeon)}",
        region=(ent.region if ent is not None else ""),
        danger=dungeon.danger, kind="dungeon",
        connections=([ent.id] if ent is not None else []),
        x=(ent.x if ent is not None else 0), y=(ent.y if ent is not None else 0) + 1,
        discovered=False, explored=False,   # 首次进入才揭示（世界地图不预刷）
    )
    world.locations.append(loc)
    if ent is not None and loc.id not in ent.connections:
        ent.connections.append(loc.id)
    return loc


def dungeon_theme_desc(world: Any, dungeon: Dungeon) -> str:
    for pool in _GENRE_DUNGEON_THEMES.values():
        for t in pool:
            if t.get("id") == dungeon.theme:
                return str(t.get("desc", ""))
    return ""


# ============ 生成 ============
def _lay_floors(world: Any, dungeon: Dungeon) -> None:
    """确定性铺层：层数 3-5、每层 3-4 房间按权重分布、末层固定 1 Boss 房。

    种子 = world.id + dungeon.id（重生同 seed 同布局；clears 只抬怪物难度不改布局）。
    """
    rng = SeededRng.seed_from(getattr(world, "id", ""), 0, f"dungeon_{dungeon.id}")
    gid = genre_id_of(world)
    n_floors = rng.roll(FLOOR_MIN, FLOOR_MAX)
    total_w = sum(w for _, w in ROOM_WEIGHTS)
    floors: list[DungeonFloor] = []
    for fi in range(1, n_floors + 1):
        if fi == n_floors:  # Boss 层：固定 1 Boss 房（小弟由 start_combat 兜底派生）
            # [修 2026-09-06 真机] Boss 房名不再带 `{主题名}·` 前缀——秘境名/层头已示上下文，
            # 前缀叠加（随从·X）（后排·受保护）把战斗名条挤爆（用户报遮挡）。
            title = rng.pick(boss_titles(gid)) or "看守者"
            floors.append(DungeonFloor(index=fi, rooms=[
                DungeonRoom(type="boss", name=title,
                            desc=f"最深处的主宰盘踞于此：{dungeon_theme_desc(world, dungeon)}")
            ]))
            continue
        n_rooms = rng.roll(ROOMS_MIN, ROOMS_MAX)
        used_names: set = set()
        rooms: list[DungeonRoom] = []
        for _ in range(n_rooms):
            rtype = _weighted_pick(rng, total_w)
            texts = room_texts(gid, rtype)
            t = texts[rng.roll(0, len(texts) - 1)] if texts else {"name": rtype, "desc": ""}
            name = str(t.get("name", rtype))
            # 同层房名去重：有界重试（[!] 池内同类型文案可能只有 2 条而层内同型房 >=3，
            # 无界 while 会死循环——重试 8 次后加序号后缀兜底）
            for _try in range(8):
                if name not in used_names:
                    break
                t = texts[rng.roll(0, len(texts) - 1)] if texts else t
                name = str(t.get("name", rtype))
            if name in used_names:
                name = f"{name}{len(used_names) + 1}"
            used_names.add(name)
            rooms.append(DungeonRoom(type=rtype, name=name, desc=str(t.get("desc", ""))))
        floors.append(DungeonFloor(index=fi, rooms=rooms))
    dungeon.floors = floors
    layout_rng = SeededRng.seed_from(getattr(world, "id", ""), 0, f"dungeon_layout_{dungeon.id}")
    for floor in floors[:-1]:
        floor.layout = layout_rng.pick(["loop", "locked"] + (["branch"] if len(floor.rooms) == 4 else []))
    ensure_room_graph(dungeon)   # [D01] 稳定 id + 邻接边（主路 + 捷径）随铺层落定
    # D02: 有机关的层将首尾环边改为受控捷径；主路始终畅通，开关不会锁在门后。
    for floor in floors:
        if floor.layout == "locked" and len(floor.rooms) >= 3:
            floor.rooms[-1].gate_requires = [floor.rooms[1].id]
        switch = next((r for r in floor.rooms if r.type == "check"), None)
        if switch is not None and len(floor.rooms) >= 3:
            first, last = floor.rooms[0], floor.rooms[-1]
            switch.shortcut = [first.id, last.id]
            first.adjacent = [rid for rid in first.adjacent if rid != last.id]
            last.adjacent = [rid for rid in last.adjacent if rid != first.id]


def _weighted_pick(rng: SeededRng, total_w: int) -> str:
    roll = rng.roll(1, total_w)
    acc = 0
    for rtype, w in ROOM_WEIGHTS:
        acc += w
        if roll <= acc:
            return rtype
    return ROOM_WEIGHTS[-1][0]


def boss_room_display(world: Any, dungeon: Dungeon, room: Any) -> str:
    """[修 2026-09-06 真机] Boss 房显示名：剥旧档 `{主题名/秘境名}·` 前缀（新生成已无前缀）。

    战斗名条宽有限，前缀与职能/位置标注叠加会遮挡等级文字；房名列表同理
    （层头已带秘境名，重复无信息量）。幂等：无前缀原样返回。
    """
    nm = str(getattr(room, "name", "") or "")
    for pref in (f"{_theme_name(world, dungeon)}·", f"{dungeon.name}·"):
        if nm.startswith(pref):
            return nm[len(pref):]
    return nm


def _theme_name(world: Any, dungeon: Dungeon) -> str:
    for pool in _GENRE_DUNGEON_THEMES.values():
        for t in pool:
            if t.get("id") == dungeon.theme:
                return str(t.get("name", dungeon.name))
    return dungeon.name


def build_dungeon(world: World, location_id: str, danger: int, theme_id: str = "",
                  name: str = "", rng: Optional[SeededRng] = None) -> Dungeon:
    """建秘境并挂 world.dungeons（[!] 调用方自证该地点无秘境——每地点限一个）。

    theme/name 缺省走题材池确定性抽取（rng 为空用 location_id 派生种子）。
    """
    gid = genre_id_of(world)
    if rng is None:
        rng = SeededRng.seed_from(getattr(world, "id", ""), 0, f"dgn_{location_id}")
    themes = theme_pool(gid)
    theme = next((t for t in themes if t.get("id") == theme_id), None)
    if theme is None:
        theme = rng.pick(themes)
    dungeon = Dungeon(
        name=(name or str(theme.get("name", "秘境"))),
        theme=str(theme.get("id", "")),
        location_id=location_id,
        danger=max(1, min(10, int(danger))),
    )
    _lay_floors(world, dungeon)
    # [D03] 默认 discovered=True 兼容直构调用方；世界生成/拓展路径在 build 后显式置 False
    world.dungeons.append(dungeon)
    return dungeon


def regenerate(world: Any, dungeon: Dungeon) -> None:
    """通关封印到期重生：同 seed 重铺（布局不变），clears 已在通关时 +1（难度随 clears 抬）。"""
    dungeon.floors = []
    _lay_floors(world, dungeon)
    dungeon.status = "open"
    dungeon.cooldown_until_day = 0
    # [B01-F07] 新开放周期：旧周期残留的延迟收尾（dungeon_tag.run_id 落后）一律拒绝
    dungeon.run_id = max(1, int(getattr(dungeon, "run_id", 1) or 1)) + 1
    # [D02] 新周期首领威慑归零（机关削 Boss 只在本周期内有效）
    dungeon.boss_relief = 0
    dungeon.current_room_id = ""


# ============ 进度 ============
def _floor_check_cleared(floor: Optional[DungeonFloor]) -> None:
    """全部房间 done 即全探索；层间移动另由实际出口条件决定。"""
    if floor is not None and not floor.cleared and floor.rooms \
            and all(r.done for r in floor.rooms):
        floor.cleared = True


def current_floor(dungeon: Dungeon) -> Optional[DungeonFloor]:
    """物理所在层；cleared 是全探索标志，不是自动下层指针。"""
    return current_room(dungeon)[0]


def current_room(dungeon: Dungeon) -> tuple[Optional[DungeonFloor], Optional[DungeonRoom], int]:
    """(实际所在层, 当前房间, 序号)。查询不改变玩家站位或跳过已探索房。"""
    ensure_room_graph(dungeon)
    f, r = room_by_id(dungeon, dungeon.current_room_id)
    if r is not None:
        return f, r, f.rooms.index(r)
    if not dungeon.floors:
        return None, None, -1
    f = dungeon.floors[0]
    r = next((r for r in f.rooms if r.id == f.entry_room_id), None)
    return f, r, f.rooms.index(r) if r is not None else -1


# ============ [D01/R2 2026-09-30] 房间图与位置（走哪条路/回到哪间房）============
def _link_rooms(a: DungeonRoom, b: DungeonRoom) -> None:
    if a is b:
        return
    if not isinstance(a.adjacent, list):
        a.adjacent = []
    if not isinstance(b.adjacent, list):
        b.adjacent = []
    if b.id and b.id not in a.adjacent:
        a.adjacent.append(b.id)
    if a.id and a.id not in b.adjacent:
        b.adjacent.append(a.id)


def _shortcut_rooms(floor: DungeonFloor, switch: DungeonRoom) -> tuple:
    if len(switch.shortcut) != 2:
        return None, None
    rooms = list(floor.rooms)
    by_id = {r.id: r for r in rooms}
    a, b = (by_id.get(rid) for rid in switch.shortcut)
    # 捷径只控制非主路边；坏引用/自环/主路相邻边不能关断正常路线。
    if a is None or b is None or a is b or abs(rooms.index(a) - rooms.index(b)) <= 1:
        return None, None
    return a, b


def ensure_room_graph(dungeon: Dungeon) -> None:
    """初始化稳定 ID/布局边/层间楼梯，清除自环与悬空引用，零随机数消费。"""
    seen_ids: set = set()
    for f in (dungeon.floors or []):
        rooms = [r for r in (f.rooms or []) if isinstance(r, DungeonRoom)]
        for i, r in enumerate(rooms):
            rid = str(r.id or "").strip()
            if not rid or rid in seen_ids:
                rid = f"f{int(f.index)}r{i}"
                while rid in seen_ids:
                    rid += "x"
                r.id = rid
            seen_ids.add(rid)
        if not rooms:
            continue
        if not f.entry_room_id:
            f.entry_room_id = rooms[0].id
        if not f.exit_room_id:
            f.exit_room_id = rooms[-1].id
        pairs = ([(0, 1), (1, 3), (1, 2)] if f.layout == "branch" and len(rooms) == 4
                 else [(i, i + 1) for i in range(len(rooms) - 1)])
        for a, b in pairs:
            _link_rooms(rooms[a], rooms[b])
        gated = [r for r in rooms if _shortcut_rooms(f, r)[0] is not None]
        if len(rooms) >= 3 and not gated and f.layout != "branch":
            _link_rooms(rooms[0], rooms[-1])
        for switch in gated:
            a, b = _shortcut_rooms(f, switch)
            if switch.shortcut_open:
                _link_rooms(a, b)
            else:
                a.adjacent = [rid for rid in a.adjacent if rid != b.id]
                b.adjacent = [rid for rid in b.adjacent if rid != a.id]
    by_id = {r.id: r for f in dungeon.floors for r in f.rooms}
    for r in by_id.values():
        r.adjacent = list(dict.fromkeys(a for a in r.adjacent if a in by_id and a != r.id))
    for r in by_id.values():
        for aid in list(r.adjacent):
            _link_rooms(r, by_id[aid])
    for a, b in zip(dungeon.floors, dungeon.floors[1:]):
        ar = next((r for r in a.rooms if r.id == a.exit_room_id), None)
        br = next((r for r in b.rooms if r.id == b.entry_room_id), None)
        if ar is not None and br is not None:
            _link_rooms(ar, br)


def room_by_id(dungeon: Dungeon, rid: str) -> tuple[Optional[DungeonFloor], Optional[DungeonRoom]]:
    rid = str(rid or "")
    if not rid:
        return None, None
    for f in (dungeon.floors or []):
        for r in (f.rooms or []):
            if isinstance(r, DungeonRoom) and str(r.id or "") == rid:
                return f, r
    return None, None


def room_graph_error(dungeon: Dungeon) -> str:
    """拒绝无效端点、自引用/悬空前置与无法从入口处理的条件门；不迁移或猜测坏引用。"""
    if not dungeon.floors:
        return "秘境尚无可进入的房间"
    for floor in dungeon.floors:
        rooms = {r.id: r for r in floor.rooms}
        if floor.entry_room_id not in rooms or floor.exit_room_id not in rooms:
            return f"第{floor.index}层入口或出口不属于该层"
        if rooms[floor.entry_room_id].gate_requires:
            return f"第{floor.index}层入口不能锁在自己的门后"
        for room in rooms.values():
            if any(rid not in rooms or rid == room.id for rid in room.gate_requires):
                return f"第{floor.index}层通道条件引用无效"
        reachable = {floor.entry_room_id}
        while True:
            extra = {aid for rid in reachable for aid in rooms[rid].adjacent
                     if aid in rooms and set(rooms[aid].gate_requires).issubset(reachable)}
            if extra.issubset(reachable):
                break
            reachable.update(extra)
        if reachable != set(rooms):
            return f"第{floor.index}层有无法从入口处理的条件门"
    return ""


def position_room_id(dungeon: Dungeon) -> str:
    """当前位置房间 id：显式站位优先；尚未设置时取第一层入口。"""
    ensure_room_graph(dungeon)
    rid = str(getattr(dungeon, "current_room_id", "") or "")
    if rid:
        _f, r = room_by_id(dungeon, rid)
        if r is not None:
            return rid
    _f2, room, _i = current_room(dungeon)
    return str(room.id) if room is not None else ""


def _target_room(dungeon: Dungeon) -> tuple[Optional[DungeonFloor], Optional[DungeonRoom], int]:
    """互动只发生在玩家站立的房间，已处理也不跳到别处开箱。"""
    return current_room(dungeon)


def room_resolved(room: DungeonRoom) -> bool:
    """互动已结算与物品入包分开：滞留物不重新封住已经处理的出口。"""
    return not room.pending and (room.done or bool(room.pending_items))


def _passage_reason(dungeon: Dungeon, source: DungeonRoom, target: DungeonRoom) -> str:
    if source.pending:
        return f"「{source.name}」的守敌未除，先击败它才能挪步"
    if source.type in ("combat", "boss") and not room_resolved(source) and not target.visited:
        return f"「{source.name}」的守敌阻路，先挑战并击败它；也可退回已抵达的房间"
    sf, _ = room_by_id(dungeon, source.id)
    tf, _ = room_by_id(dungeon, target.id)
    if sf is not tf:
        floors = dungeon.floors
        if (sf not in floors or tf not in floors
                or abs(floors.index(sf) - floors.index(tf)) != 1):
            return "这里没有通往目标房间的楼梯"
        down = floors.index(tf) > floors.index(sf)
        upper, lower = (sf, tf) if down else (tf, sf)
        if {source.id, target.id} != {upper.exit_room_id, lower.entry_room_id}:
            return "须经本层楼梯进入相邻楼层"
        exit_room = room_by_id(dungeon, upper.exit_room_id)[1]
        if down and not room_resolved(exit_room):
            return f"楼梯/首领门未开，先处理本层出口「{exit_room.name}」"
    for required_id in target.gate_requires:
        _f, required = room_by_id(dungeon, required_id)
        if required is None or not room_resolved(required):
            name = required.name if required and (required.discovered or required.seen or required.done) else "尚未探明的房间"
            return f"通道尚未打开，须先处理「{name}」"
    return ""


def room_known(dungeon: Dungeon, room: DungeonRoom) -> bool:
    if room.discovered or room.done or room.seen or room.pending or room.pending_items:
        return True
    pf, pr, _ = current_room(dungeon)
    if pr is None:
        return False
    tf, _ = room_by_id(dungeon, room.id)
    return room.id == pr.id or (room.id in pr.adjacent and (pf is tf or not _passage_reason(dungeon, pr, room)))


def reveal_room(dungeon: Dungeon, rid: str) -> None:
    f, room = room_by_id(dungeon, rid)
    if room is None:
        return
    room.discovered = True
    room.visited = True
    for aid in room.adjacent:
        af, ar = room_by_id(dungeon, aid)
        if ar is not None and (af is f or not _passage_reason(dungeon, room, ar)):
            ar.discovered = True


def known_room_path(dungeon: Dungeon, rid: str) -> list[str]:
    """只查已知且可通行路线；不移动、不代玩家执行多回合。"""
    start = position_room_id(dungeon)
    if room_graph_error(dungeon):
        return []
    queue = [(start, [start])]
    visited = {start}
    for at, path in queue:
        if at == rid:
            return path
        _f, source = room_by_id(dungeon, at)
        if source is None or source.pending:
            continue
        for aid in source.adjacent:
            _af, target = room_by_id(dungeon, aid)
            if (target is not None and aid not in visited and room_known(dungeon, target)
                    and not _passage_reason(dungeon, source, target)):
                visited.add(aid)
                queue.append((aid, path + [aid]))
    return []


def can_move_room(dungeon: Dungeon, rid: str) -> tuple[bool, str]:
    """房间移动门控：目标存在 + 与当前位置相邻 + 当前房无未解决战斗。"""
    ensure_room_graph(dungeon)
    error = room_graph_error(dungeon)
    if error:
        return False, error
    _f, room = room_by_id(dungeon, rid)
    if room is None:
        return False, "秘境内没有这个去处"
    pos = position_room_id(dungeon)
    if pos == str(rid):
        return False, f"你已在「{room.name}」"
    _pf, pr = room_by_id(dungeon, pos)
    if pr is not None and pr.pending:
        return False, f"「{pr.name}」的守敌未除，先击败它才能挪步"
    adj_ids = [str(x) for x in (pr.adjacent if pr is not None else []) or []]
    if str(rid) not in adj_ids:
        return False, "目标不与当前位置相通（只能沿一条相邻通道移动）"
    why = _passage_reason(dungeon, pr, room)
    if why:
        return False, why
    if not room_known(dungeon, room):
        return False, "尚未发现这个房间的入口"
    return True, ""


def move_room(world: Any, dungeon: Dungeon, rid: str, intent: dict, summary: dict,
              *, notify_progress: bool = True) -> bool:
    """房间移动：门控过 -> 站位落定 + hint 双写；首次踩入陷阱另执行被动检定。

    到达已探索房只重见描述（不重发奖励——done 房的奖励早已结算/滞留件走补发）；
    普通未探索房不自动结算（探索仍走「深入」/advance，守 §4.2 探索粒度）。"""
    if world.player.hp <= 0 or _stale_room_request(dungeon, intent):
        intent["resolved"] = False
        summary["reason"] = "房间行动已失效或你已倒下，请查看当前站位"
        return False
    ok, why = can_move_room(dungeon, rid)
    if not ok:
        intent["resolved"] = False
        summary["reason"] = why
        return False
    _f, room = room_by_id(dungeon, rid)
    dungeon.current_room_id = str(rid)
    reveal_room(dungeon, rid)
    state = "已探明" if room.done else ("战斗一触即发" if room.pending else "未探明")
    _hint(intent, summary, f"你移步至{dungeon.name}的「{room.name}」（{state}）——{room.desc}")
    summary["world_changed"] = True
    summary["room_moved"] = str(rid)
    trigger_arrival_trap(world, dungeon, intent, summary, notify_progress=notify_progress)
    return True


def trigger_arrival_trap(world: Any, dungeon: Dungeon, intent: dict, summary: dict,
                         *, notify_progress: bool = True) -> None:
    """入口与逐房移动共用踩入检定；不重复处理已发现、已结算或已有守卫的陷阱。"""
    _floor, room, _index = current_room(dungeon)
    if (dungeon.status == "open" and room is not None and room.type == "trap"
            and not room_resolved(room) and not room.seen and not room.pending):
        before = _rooms_done(dungeon)
        arrival_intent = {"resolved": True, "narration_hint": ""}
        _advance_impl(world, dungeon, summary, arrival_intent, world.player.level)
        extra = summary.get("narration_hint", "")
        if extra:
            intent["narration_hint"] = extra
        if notify_progress and _rooms_done(dungeon) > before:
            _quest_hook(world, dungeon, "dungeon_room_resolved")


def _stale_room_request(dungeon: Dungeon, intent: dict) -> bool:
    expected = {"dungeon_id": dungeon.id, "dungeon_run_id": dungeon.run_id,
                "dungeon_from_room_id": position_room_id(dungeon)}
    return any(intent[key] != value for key, value in expected.items() if key in intent)


def resolve_dungeon_destination(world: Any, dungeon: Dungeon, name: str) -> Optional[DungeonRoom]:
    """具名目的地 -> 当前位置的相邻房间（名字容错锚定邻接名单，编造名不追认）。"""
    from src.services import name_resolver as nrs
    _pf, pr = room_by_id(dungeon, position_room_id(dungeon))
    if pr is None:
        return None
    by_id = {str(r.id): r for f in (dungeon.floors or []) for r in (f.rooms or [])}
    cands: list[DungeonRoom] = []
    for aid in [str(x) for x in (pr.adjacent or [])]:
        r = by_id.get(aid)
        if r is not None and r not in cands and room_known(dungeon, r):
            cands.append(r)
    pf, _ = room_by_id(dungeon, pr.id)
    text = str(name or "").strip()
    direction = (1 if text in ("下层", "下楼", "下层楼梯", "下一层", "首领门", "Boss门")
                 else -1 if text in ("上层", "上楼", "上层楼梯") else 0)
    if direction:
        index = dungeon.floors.index(pf) + direction
        if 0 <= index < len(dungeon.floors):
            floor = dungeon.floors[index]
            _f, target = room_by_id(dungeon, floor.entry_room_id if direction > 0 else floor.exit_room_id)
            if target is not None and target.id in pr.adjacent:
                return target
        return None
    if not cands:
        return None
    qualified = {f"第{room_by_id(dungeon, r.id)[0].index}层/{r.name}": r for r in cands}
    if text in qualified:
        return qualified[text]
    hit = nrs.resolve_name(text, [r.name for r in cands])
    if not hit:
        return None
    matches = [r for r in cands if r.name == hit]
    return matches[0] if len(matches) == 1 else None


def discover_on_visit(world: Any, location: Any) -> list:
    """[D03/R2 2026-09-30] 到访入口地点 -> 发现该地点全部秘境（含封印中的——知道有洞
    与能不能进是两件事）。返回新发现的 Dungeon 列表（空=无可发现/已发现过）。"""
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    found = []
    for d in (getattr(world, "dungeons", None) or []):
        if not isinstance(d, Dungeon):
            continue
        if str(getattr(d, "location_id", "") or "") != str(getattr(location, "id", "") or ""):
            continue
        if getattr(d, "discovered", False):
            continue
        d.discovered = True
        d.discovery_day = day
        d.discovery_source = "visit"
        found.append(d)
    return found


def known_exits_line(world: Any, dungeon: Dungeon) -> str:
    """已知出口行（scene_block/秘境弹窗共用：邻接房间名 + 状态）。"""
    _pf, pr = room_by_id(dungeon, position_room_id(dungeon))
    if pr is None:
        return ""
    by_id = {str(r.id): r for f in (dungeon.floors or []) for r in (f.rooms or [])}
    parts: list = []
    for aid in [str(x) for x in (pr.adjacent or [])][:6]:
        r = by_id.get(aid)
        if r is None:
            continue
        rf, _ = room_by_id(dungeon, r.id)
        pf, _ = room_by_id(dungeon, pr.id)
        why = _passage_reason(dungeon, pr, r)
        if not room_known(dungeon, r):
            if rf is not pf:
                parts.append("下层楼梯（" + why + "）")
            continue
        st = "已探" if r.done else ("有敌" if r.pending else "未探")
        parts.append(f"{r.name}（{why or st}）")
    if not parts:
        return ""
    return "已知出口：" + "、".join(parts)


def can_exit_dungeon(dungeon: Dungeon) -> tuple[bool, str]:
    _f, room, _ = current_room(dungeon)
    if room is None:
        return False, "秘境入口房间失效"
    if room.pending:
        return False, "眼前守敌未除，先结束战斗"
    entry = dungeon.floors[0].entry_room_id
    if room.id != entry:
        path = known_room_path(dungeon, entry)
        names = [room_by_id(dungeon, rid)[1].name for rid in path[:6]]
        return False, "须沿已知路线返回第一层入口再离开" + ("：" + " -> ".join(names) if names else "")
    return True, ""


def _advance_from_done(world: Any, dungeon: Dungeon, room: DungeonRoom, intent: dict, summary: dict) -> None:
    floor, _ = room_by_id(dungeon, room.id)
    candidates = []
    for aid in room.adjacent:
        tf, target = room_by_id(dungeon, aid)
        if target is None:
            continue
        downward = dungeon.floors.index(tf) > dungeon.floors.index(floor)
        if (not target.done or downward) and can_move_room(dungeon, aid)[0]:
            candidates.append(target)
    if dungeon.status == "open" and len(candidates) == 1:
        move_room(world, dungeon, candidates[0].id, intent, summary, notify_progress=False)
        return
    intent["resolved"] = False
    summary["reason"] = ("秘境已封印，请沿已知路线返回入口；" if dungeon.status != "open"
                         else "有多个可行方向，请选相邻房间；" if len(candidates) > 1
                         else "当前房间已探索，请选择已知路线；") + known_exits_line(world, dungeon)


# ============ 怪物 ============
def _room_rng(world: Any, dungeon: Dungeon, floor_idx: int, room_idx: int) -> SeededRng:
    return SeededRng.seed_from(getattr(world, "id", ""), 0,
                               f"droom_{dungeon.id}_{floor_idx}_{room_idx}")


def build_dungeon_monster(world: Any, dungeon: Dungeon, floor: DungeonFloor,
                          room: DungeonRoom, room_idx: int, player_level: int) -> NPC:
    """秘境临时怪（不入 world.npcs）：等级 = max(danger+层数, 玩家-1) + clears + Boss 加成。

    确定性：同房间同种子同怪（重进续探重建同一只）；属性抖动走独立 rng 不污染房序。
    boss=True 时 loot 必含技能书线索（技能书在 on_combat_victory 按公式 roll，不进 loot_table）。
    """
    rng = _room_rng(world, dungeon, floor.index, room_idx)
    gid = genre_id_of(world)
    boss = room.type == "boss"
    # [P43 重构 2026-09-12] 等级 = [玩家, 玩家 + (危险度+层序) + 3] 段内随机；通关数加深；Boss 加成。
    # 层数首次真正起作用：旧 soften 软底把层数差异全抹平（10 级玩家在 danger 3 秘境
    # 第 1 层和第 5 层遇到的都是 13-15 级）。Boss 仍保 +clears*2 + max(3, base//10)。
    _d2 = max(1, min(10, int(dungeon.danger) + floor.index))
    # [!] Boss 传 rng=None 取区间顶（否则段内随机会让 Boss 被同层小怪的运气值反超）。
    base_lvl = we.band_level(_d2, int(player_level), None if boss else rng)
    lvl = max(1, min(MONSTER_LEVEL_CAP, base_lvl + int(dungeon.clears) * 2
                     + (max(3, base_lvl // 10) if boss else 0)))
    fake_loc = Location(id=dungeon.interior_id, name=dungeon.name, danger=min(10, dungeon.danger + floor.index))
    tmpl = we._pick_template(world, fake_loc, rng) or {}
    # [修 2026-09-06 真机] Boss 怪名走 boss_room_display 剥前缀（旧档自愈）
    name = (boss_room_display(world, dungeon, room) if boss
            else str(tmpl.get("name") or room.name))
    npc = NPC(name=name, role=(str(tmpl.get("role") or "秘境守卫") if not boss else "秘境首领"),
              desc=room.desc, hostile=True, level=lvl)
    npc.alive = True
    npc.is_key_npc = False
    wr = SeededRng.seed_from(getattr(world, "id", ""), 0, f"dgn_stats_{dungeon.id}_{floor.index}_{room_idx}")
    jit = lambda base: max(1, base + wr.roll(-1, 2))
    npc.stat_str = jit((5 if boss else 4) + lvl)
    npc.stat_vit = jit((5 if boss else 4) + lvl)
    npc.stat_dex = jit(3 + lvl // 2)
    npc.stat_int = jit(3 + (1 if boss else 0))
    npc.stat_luk = jit(3)
    npc.hp_max = ce.max_hp_for(npc) * (2 if boss else 1)   # Boss 双倍血条
    # [D02/R2 2026-09-30] 机关削 Boss：本周期每级已解除威慑 -15% 血量（钳 0-2 级，
    # 与 scene_block/弹窗预告同口径——预告说多少，结算就是多少）
    _relief = max(0, min(2, int(getattr(dungeon, "boss_relief", 0) or 0)))
    if boss and _relief:
        npc.hp_max = max(1, int(npc.hp_max * (1.0 - 0.15 * _relief)))
    npc.hp = npc.hp_max
    npc.mp_max = npc.stat_int * 5 + lvl * 2
    npc.mp = npc.mp_max
    # [P42f] 职能化技能：Boss 3 技（强攻+回复+增益）、房间怪单职能（同 wilderness 口径）
    from src.services.wilderness_engine import monster_role_skills
    _tid = (getattr(world, "config_overlay", None) or {}).get(
        "attribute_template_id", "western_fantasy") if isinstance(
        getattr(world, "config_overlay", None), dict) else "western_fantasy"
    npc.ai_pattern, npc.skills = monster_role_skills(
        str(_tid), str(getattr(world, "id", "")), npc.id, ("首领" if boss else "怪"), lvl,
        elite=False, skill_pool=getattr(world, "monster_skill_pool", None))
    npc.elements = _clean_elements(tmpl.get("elements")) or ce.infer_elements_from_skills(npc.skills)
    # 掉落：怪物池 loot + 秘境层级材料；Boss 掉率 0.9 / 精英感
    rate = 0.9 if boss else 0.5
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
    # [!] ad-hoc 标记（临时单位不落盘，finish_combat 据此路由秘境结算）；run_id 供
    # on_combat_victory 拒绝旧周期怪延迟收尾（regenerate 已重铺，勿结算新周期房间）
    npc.dungeon_tag = {"dungeon_id": dungeon.id, "floor": floor.index,
                       "room": room_idx, "boss": boss,
                       "run_id": max(1, int(getattr(dungeon, "run_id", 1) or 1))}   # type: ignore[attr-defined]
    return npc


# ============ 推进（apply_intent move 特判调用）============
def _give_item(world: Any, item) -> bool:
    ok = ce.try_add_to_inventory(world.player, getattr(item, "id", ""))
    if ok:
        codex = getattr(world.player, "codex_items", None)
        if isinstance(codex, list) and getattr(item, "id", "") not in codex:
            codex.append(item.id)
        # [B01-F13] 秘境房间奖励也算「实际取得物品」：collect 任务进度与摸格子
        # 拾取同口径（拿到才算获得）。物品从房间直接入包，不与 loot_grid_take 双计。
        try:
            from src.services import quest_engine as _qe
            _qe.update_progress(world, "collect", getattr(item, "name", ""),
                                aliases=[getattr(item, "rarity", "")])
        except Exception:
            pass
    return ok


def has_pending_loot(dungeon: Dungeon) -> bool:
    """[B01-F06] 任一房间尚有满包滞留的奖励（封印中补领放行的判据）。"""
    return any(getattr(r, "pending_items", None) for f in dungeon.floors for r in f.rooms)


def _reclaim_room(world: Any, dungeon: Dungeon, floor: DungeonFloor, room: DungeonRoom,
                  intent: dict, summary: dict) -> None:
    """[B01-F06] 补发滞留奖励：只给物品（金币/经验在首次结算已发放，绝不重发）。

    全部取走才清列表并置 done（treasure/check 房由此推进；Boss 房本就 done）。
    仍放不下留在原处，幂等可重试。"""
    tag = f"{dungeon.name}第{floor.index}层"
    remaining: list = []
    for iid in list(getattr(room, "pending_items", None) or []):
        item = next((i for i in (getattr(world, "items", None) or [])
                     if getattr(i, "id", "") == iid), None)
        if item is None:
            continue   # 物品已不在世界（不应发生），滞留件放弃，不留死引用
        if _give_item(world, item):
            _hint(intent, summary, f"{tag}你腾出空间，取回了先前留在「{room.name}」的「{item.name}」")
        else:
            remaining.append(iid)
            _hint(intent, summary, f"{tag}背包仍放不下「{item.name}」，它还留在「{room.name}」")
    room.pending_items = remaining
    if not remaining and not room.done:
        room.done = True
        _floor_check_cleared(floor)
    summary["world_changed"] = True


def _gold(world: Any, base: int) -> int:
    return max(0, int(base * ene._gold_mult(world)))


def _currency(world: Any) -> str:
    """[审核修复 2026-09-13] 币种显示名单一来源（§23：UI/hint 一律走 GenreText，
    绝不硬编码「金币」——仙侠要出「灵石」、武侠要出「银两」）。"""
    return GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {}).currency


def _quest_hook(world: Any, dungeon: Dungeon, event_type: str) -> None:
    """[S04/R2 2026-09-30] 秘境进度喂任务目标（best-effort：dungeon_room_resolved/
    dungeon_cleared，target=秘境名走 _matches 容错匹配）。"""
    try:
        from src.services import quest_engine as _qe
        _qe.update_progress(world, event_type, str(getattr(dungeon, "name", "") or ""))
    except Exception:
        pass


def _rooms_done(dungeon: Dungeon) -> int:
    return sum(1 for f in (dungeon.floors or []) for r in (f.rooms or [])
               if getattr(r, "done", False))


def interaction_tools(world: Any, dungeon: Dungeon) -> list:
    """当前互动可选的实际背包工具；UI 只展示，消费仍在 advance 中。"""
    _f, room, _i = _target_room(dungeon)
    if room is None or room.done or room.pending or room.check_result or room.pending_items:
        return []
    if room.type in ("check", "trap") and room.seen:
        return dt.available_tools(world, dt.MECHANISM_TOOL)
    if room.type == "treasure" and not room.hidden_result and _hidden_check_enabled(world):
        return dt.available_tools(world, dt.SEARCH_TOOL)
    return []


def _check_actor(world: Any, dungeon: Dungeon, keys: tuple[str, ...], *,
                 base: float = 0.5, tool_bonus: int = 0):
    """玩家或一位有效随行同伴做检定；按实际属性、饱食与天赋挑更擅长者，不叠人数。"""
    actors = [world.player]
    if world.player.location_id == dungeon.interior_id:
        companions = set(world.player.companion_npc_ids or [])
        actors += [n for n in world.npcs if n.id in companions and n.alive
                   and not n.hostile and n.hp > 0 and n.location_id == dungeon.interior_id
                   and n.companion_fatigue_until_tick <= world.tick_count]

    def stat(actor):
        hunger_mult = ce.hunger_stat_mult(actor) if actor is world.player else 1.0
        return int(max(te.effective_stat(actor, key) for key in keys) * hunger_mult)

    def score(actor):
        return min(0.95, ce.success_chance(stat(actor), tool_bonus=tool_bonus, base=base,
                    difficulty=dungeon.danger * 5,
                    global_penalty=ene.difficulty_penalty_of(world))
                   * te.get_talent_mult(actor, "check_mult"))

    actor = max(actors, key=score)
    return actor, stat(actor)


def _hidden_roll(rng: SeededRng, world: Any, dungeon: Dungeon, base: float,
                 tool_bonus: int, intent: dict, summary: dict) -> bool:
    actor, _sv = _check_actor(world, dungeon, ("int", "luk"), base=base, tool_bonus=tool_bonus)
    if actor is not world.player:
        _hint(intent, summary, f"随行的「{actor.name}」协助察觉隐患")
    kwargs = {"tool_bonus": tool_bonus, "actor": actor} if tool_bonus or actor is not world.player else {}
    return ene.roll_hidden_check(rng, world, difficulty=dungeon.danger * 5, base=base, **kwargs)


def _open_shortcut(floor: DungeonFloor, room: DungeonRoom) -> str:
    a, b = _shortcut_rooms(floor, room)
    if a is None:
        return ""
    room.shortcut_open = True
    _link_rooms(a, b)
    return f"；捷径已开启：「{a.name}」与「{b.name}」现在直接相通"


def advance(world: Any, dungeon: Dungeon, summary: dict, intent: dict,
            player_level: int) -> None:
    """推进当前房间（结算 + hint 双写；战斗房产 forced_combat）。

    [S04/R2 2026-09-30] 薄包装：房间 done 数差分 > 0 -> 喂 dungeon_room_resolved 任务
    进度（覆盖全部结算分支含滞留补发收口；战斗房另走 on_combat_victory 的房间钩子）。
    确定性：每房独立种子（_room_rng），重试/重进同结果；差分计数纯读不改状态。"""
    _before = _rooms_done(dungeon)
    _advance_impl(world, dungeon, summary, intent, player_level)
    if intent.get("resolved") is not False:
        reveal_room(dungeon, position_room_id(dungeon))
    if _rooms_done(dungeon) > _before:
        _quest_hook(world, dungeon, "dungeon_room_resolved")


def _advance_impl(world: Any, dungeon: Dungeon, summary: dict, intent: dict,
                  player_level: int) -> None:
    """advance 实现体（推进语义与门控见 advance 包装层注释）。"""
    action = str(intent.get("dungeon_action", "") or "")
    floor, room, ridx = _target_room(dungeon)
    requested_room = str(intent.get("dungeon_room_id", "") or "")
    if world.player.hp <= 0:
        intent["resolved"] = False
        summary["reason"] = "你已倒下，无法探索秘境"
        return
    if (action not in ("", "tool", "bypass") or _stale_room_request(dungeon, intent)
            or (requested_room and (room is None or room.id != requested_room))):
        intent["resolved"] = False
        summary["reason"] = "这次房间互动已失效，请查看当前房间"
        return
    tool = None
    if action:
        if (room is None or room.done or room.pending or room.check_result or room.pending_items
                or dungeon.status != "open"):
            intent["resolved"] = False
            summary["reason"] = "当前房间不能进行这项互动（战斗/奖励须先处理）"
            return
        if action == "bypass" and not (room.type in ("check", "trap") and room.seen):
            intent["resolved"] = False
            summary["reason"] = "须先观察机关或发现陷阱，才能安全绕行"
            return
        if action == "tool":
            iid = str(intent.get("dungeon_tool_id", "") or "")
            tool = next((it for it in interaction_tools(world, dungeon) if it.id == iid), None)
            if tool is None:
                intent["resolved"] = False
                summary["reason"] = "没有适用于当前互动的工具（机关/陷阱须先观察，暗格只能检查一次）"
                return
    # 补领发生在真实来源房间；别处遗留物保持，不隔空拉回或阻止选路。
    if room is not None and room.pending_items:
        _reclaim_room(world, dungeon, floor, room, intent, summary)
        return
    if room is not None and str(room.id or ""):
        dungeon.current_room_id = str(room.id)   # [D01] 站位随结算落定（回放/读档一致）
    if room is None:
        dungeon.current_room_id = ""
        _hint(intent, summary, f"{dungeon.name}已无未探索之处（击败最深处的首领才算通关）")
        return
    if room.done:
        _advance_from_done(world, dungeon, room, intent, summary)
        return
    if dungeon.status != "open":
        intent["resolved"] = False
        summary["reason"] = "秘境已封印，可沿已知路线返回或补领遗留物"
        return
    if action == "bypass":
        room.check_result = "bypassed"
        room.done = True
        _floor_check_cleared(floor)
        summary["world_changed"] = True
        _hint(intent, summary, f"你沿安全主路绕过「{room.name}」，没有取得这里的奖励"
              + ("，机关捷径仍关闭，首领威势未减" if room.type == "check" else "，未触发陷阱"))
        return
    tool_bonus = 0
    if tool is not None:
        world.player.inventory.remove(tool.id)  # 验证全过才消费；失败检定也消耗一件
        room.interaction_tool_id = tool.id
        tool_bonus = 1
        summary.update({"used_item_id": tool.id, "world_changed": True})
        _hint(intent, summary, f"消耗一件「{tool.name}」，本次检定基础成功率 +5 个百分点（再计天赋与上限）")
    rng = _room_rng(world, dungeon, floor.index, ridx)
    fm = floor_mult(floor.index)
    tag = f"{dungeon.name}第{floor.index}层"
    # [P 验收] 全局难度经验倍率（困难下秘境经验同战斗经验一并上调作风险补偿）
    _ov = world.config_overlay if isinstance(world.config_overlay, dict) else {}
    xp_mult = ce.xp_difficulty_mult(_ov.get("difficulty", "normal"))
    gp = ce.check_difficulty_penalty(_ov.get("difficulty", "normal"))
    # [review] pending 房重入守卫（战斗房/陷阱惊动守卫后未击败）：重建同一只怪强制战斗，
    # 不重新 roll（防守卫凭空消失/重复扣血）。
    if room.pending:
        monster = build_dungeon_monster(world, dungeon, floor, room, ridx, player_level)
        summary["forced_combat"] = monster
        _hint(intent, summary, f"{tag}{room.name}的守敌仍在，须先击败它才能推进")
        return

    if room.type in ("combat", "boss"):
        room.pending = True
        monster = build_dungeon_monster(world, dungeon, floor, room, ridx, player_level)
        summary["forced_combat"] = monster
        summary["world_changed"] = True
        # [D02] 战斗威胁可读：等级随遭遇提示（玩家判断打不打/先绕）
        _hint(intent, summary,
              f"深入{tag}，遭遇「{monster.name}」（等级 {monster.level}）！")
        return

    if room.type == "trap":
        # D02: 被动发现保存线索后交由玩家选择解除/工具/绕行，不替玩家自动完成房间。
        # [!] 用独立 rng seed（不消费房间 _room_rng），保既有陷阱/惊动守卫 rng 序列不变（回归守护）。
        if not room.seen and not room.hidden_result and _hidden_check_enabled(world):
            h_rng = SeededRng.seed_from(getattr(world, "id", ""), 0,
                                        f"dhid_{dungeon.id}_{floor.index}_{ridx}")
            found = _hidden_roll(h_rng, world, dungeon, 0.5, 0, intent, summary)
            room.hidden_result = "found" if found else "missed"
            if found:
                room.seen = True
                summary["world_changed"] = True
                _hint(intent, summary,
                      f"{tag}察觉到「{room.name}」的隐患（{room.desc}）。可解除、消耗工具，或沿安全路线绕行")
                return
        if not room.hidden_result:
            room.hidden_result = "disabled"
        actor, dex = _check_actor(world, dungeon, ("dex",), tool_bonus=tool_bonus)
        chance = min(0.95, ce.success_chance(dex, tool_bonus=tool_bonus, difficulty=dungeon.danger * 5,
                     base=0.5, global_penalty=gp) * te.get_talent_mult(actor, "check_mult"))
        passed = rng.chance(chance)
        room.check_result = "success" if passed else "failure"
        if actor is not world.player:
            _hint(intent, summary, f"随行的「{actor.name}」协助解除陷阱")
        if passed:
            room.done = True
            _floor_check_cleared(floor)
            xp = int((10 + dungeon.danger * 4) * fm * xp_mult)
            ce.gain_xp(world.player, xp)
            _hint(intent, summary, f"{tag}识破「{room.name}」（{room.desc}），全身而过，获得 {xp} 经验")
        else:
            hp_max = max(1, ce.max_hp_for(world.player))
            dmg = max(1, int(hp_max * TRAP_DAMAGE_PCT))
            world.player.hp = max(0, int(world.player.hp) - dmg)
            if world.player.hp <= 0:
                _degrade_durability(world, 1)
                revived = ce.try_auto_revive(world, world.player)
                room.done = True
                _floor_check_cleared(floor)
                summary["world_changed"] = True
                _hint(intent, summary, f"{tag}触发「{room.name}」受 {dmg} 点伤，装备有所磨损，你倒下了"
                      + (f"；「{revived}」自动生效，恢复至 HP {world.player.hp}" if revived else ""))
                if not revived:
                    summary["player_defeated"] = True
                return   # 零血不再强制拉起守卫战斗
            if rng.chance(TRAP_GUARD_CHANCE):
                room.pending = True   # 惊动守卫：转战斗房
                monster = build_dungeon_monster(world, dungeon, floor, room, ridx, player_level)
                summary["forced_combat"] = monster
                summary["world_changed"] = True
                _degrade_durability(world, 1)  # 陷阱失败两分支同口径：装备耐久 -1
                _hint(intent, summary,
                      f"{tag}触发「{room.name}」受 {dmg} 点伤，装备有所磨损，还惊动了「{monster.name}」！")
            else:
                _degrade_durability(world, 1)
                room.done = True
                _floor_check_cleared(floor)
                _hint(intent, summary,
                      f"{tag}触发「{room.name}」受 {dmg} 点伤，装备也有所磨损"
                      + ("（可经已探明的通道绕开此房）" if len(room.adjacent or []) > 2 else ""))
        summary["world_changed"] = True
        return

    if room.type == "check":
        # [D02/R2 2026-09-30] 两段式机关：第一步「观察线索」（seen 落盘不结算不耗房，
        # 同一次互动结果固定保存——重开窗口/重试不重 roll 观察态）；再次深入才破解。
        if not getattr(room, "seen", False):
            room.seen = True
            summary["world_changed"] = True
            _hint(intent, summary,
                  f"{tag}细看「{room.name}」的机关构造——{room.desc}。"
                  f"破解须谨慎着手（再次深入即动手）")
            return
        stat_key = rng.pick(("int", "dex"))
        actor, sv = _check_actor(world, dungeon, (stat_key,), tool_bonus=tool_bonus)
        chance = min(0.95, ce.success_chance(sv, tool_bonus=tool_bonus, difficulty=dungeon.danger * 5,
                     base=0.5, global_penalty=gp) * te.get_talent_mult(actor, "check_mult"))
        passed = rng.chance(chance)
        room.check_result = "success" if passed else "failure"
        if actor is not world.player:
            _hint(intent, summary, f"随行的「{actor.name}」协助破解机关")
        if passed:
            shortcut_hint = _open_shortcut(floor, room)
            rarity = we.tier_to_rarity(min(5, we.danger_to_tier(dungeon.danger) + (1 if floor.index >= 3 else 0)))
            item = ene._pick_reward_item(world, rng, rarity)
            gold = _gold(world, int((12 + dungeon.danger * 6) * fm))
            world.player.gold = int(getattr(world.player, "gold", 0) or 0) + gold
            got = ""
            hold_ids: list = []
            if item is not None:
                if _give_item(world, item):
                    got = f"，获得「{item.name}」"
                else:
                    # [B01-F06] 满包滞留：物品留在房间可再取，房间不置 done（不吞奖励）
                    hold_ids.append(item.id)
                    got = f"，「{item.name}」放不下暂留此处（清出背包再深入取回）"
            xp = int((15 + dungeon.danger * 5) * fm * xp_mult)
            ce.gain_xp(world.player, xp)
            summary["world_changed"] = True
            if hold_ids:
                room.pending_items = list(getattr(room, "pending_items", None) or []) + hold_ids
            else:
                room.done = True
                _floor_check_cleared(floor)
            # [D02] 破解成功 -> 削本周期首领威慑（每级 Boss 血量 -15%，钳 2 级；
            # 预告/结算/build_dungeon_monster 三处同口径）
            _relief_old = max(0, min(2, int(getattr(dungeon, "boss_relief", 0) or 0)))
            if _relief_old < 2:
                dungeon.boss_relief = _relief_old + 1
                _hint(intent, summary,
                      f"机关应声而解——最深处首领的威势被削弱了一分"
                      f"（首领血量 -15%x{dungeon.boss_relief}）")
            _cur = _currency(world)  # §23 币种走 GenreText，勿硬编码「金币」
            _hint(intent, summary,
                  f"{tag}破解「{room.name}」（{room.desc}）{got}，获得 {gold} {_cur}、{xp} 经验{shortcut_hint}")
        else:
            room.done = True
            _floor_check_cleared(floor)
            xp = int((8 + dungeon.danger * 3) * fm * xp_mult)
            ce.gain_xp(world.player, xp)
            summary["world_changed"] = True
            _hint(intent, summary, f"{tag}未能破解「{room.name}」，只得绕行，获得 {xp} 经验")
        return

    if room.type == "treasure":
        rarity = we.tier_to_rarity(min(5, we.danger_to_tier(dungeon.danger) + (1 if floor.index >= 3 else 0)))
        item = ene._pick_reward_item(world, rng, rarity)
        gold = _gold(world, int((15 + dungeon.danger * 7) * fm))
        world.player.gold = int(getattr(world.player, "gold", 0) or 0) + gold
        got = ""
        held: list = []
        if item is not None:
            if _give_item(world, item):
                got = f"，获得「{item.name}」"
            else:
                # [B01-F06] 满包滞留：物品留在房间可再取，房间不置 done（不吞奖励）
                held.append(item.id)
                got = f"，「{item.name}」放不下暂留此处（清出背包再深入取回）"
        xp = int((10 + dungeon.danger * 4) * fm * xp_mult)
        ce.gain_xp(world.player, xp)
        summary["world_changed"] = True
        # [P33] 隐藏检定发现额外宝藏：int/luk 取高 + check_mult 通过 -> 额外稀有物品 + 金币。
        # [!] 检定与奖励挑选都用独立 h_rng（不消费房间 _room_rng），保既有宝藏 rng 序列不变
        # （[C12 修复 2026-08-25] 奖励挑选原误传房间 rng，与注释承诺不符，现对齐）。
        hidden_bonus = ""
        if not room.hidden_result and _hidden_check_enabled(world):
            h_rng = SeededRng.seed_from(getattr(world, "id", ""), 0,
                                        f"dhit_{dungeon.id}_{floor.index}_{ridx}")
            found = _hidden_roll(h_rng, world, dungeon, 0.45, tool_bonus, intent, summary)
            room.hidden_result = "found" if found else "missed"
            if found:
                h_rarity = we.tier_to_rarity(min(5, we.danger_to_tier(dungeon.danger) + 1))
                h_item = ene._pick_reward_item(world, h_rng, h_rarity)
                h_gold = _gold(world, int((10 + dungeon.danger * 4) * fm))
                world.player.gold = int(getattr(world.player, "gold", 0) or 0) + h_gold
                _cur = _currency(world)
                if h_item is not None and _give_item(world, h_item):
                    hidden_bonus = f"；察觉暗格，另获「{h_item.name}」与 {h_gold} {_cur}"
                else:
                    if h_item is not None:
                        held.append(h_item.id)
                    hidden_bonus = f"；察觉暗格，另获 {h_gold} {_cur}" \
                                   + ("（暗格物品放不下暂留此处）" if h_item is not None else "")
            else:
                hidden_bonus = "；暗格检查结束，未发现额外收获（本周期不再检定）"
        if not room.hidden_result:
            room.hidden_result = "disabled"
        if held:
            # [B01-F06] 有滞留件：房间保持未 done（推进指针停在此，深入即补发）
            room.pending_items = list(getattr(room, "pending_items", None) or []) + held
        else:
            room.done = True
            _floor_check_cleared(floor)
        _hint(intent, summary,
              f"{tag}搜刮「{room.name}」{got}，获得 {gold} {_currency(world)}、{xp} 经验{hidden_bonus}")
        return

    if room.type == "rest":
        room.done = True
        _floor_check_cleared(floor)
        p = world.player
        p.hp_max = max(1, ce.max_hp_for(p))
        p.hp = p.hp_max
        p.mp = max(0, int(getattr(p, "mp_max", 0) or 0))
        xp = int((5 + dungeon.danger * 2) * fm * xp_mult)
        ce.gain_xp(world.player, xp)
        summary["world_changed"] = True
        # [D02] 休整一次性明示（done 后重访不再回满，防「反复经过无限回满」误解）
        _hint(intent, summary,
              f"{tag}在「{room.name}」稍作休整，HP/MP 全回复（一次性，用过即止），"
              f"获得 {xp} 经验")
        return

    room.done = True   # 未知类型防御性跳过
    _floor_check_cleared(floor)
    _hint(intent, summary, f"{tag}的「{room.name}」静悄悄的，无事发生")


def _hint(intent: dict, summary: dict, text: str) -> None:
    """[!] intent 与 summary 双写（叙事 LLM 只读 intent.narration_hint）。"""
    prev = (intent.get("narration_hint") or "")
    intent["narration_hint"] = (prev + "；" + text) if prev else text
    summary["narration_hint"] = intent["narration_hint"]


def _degrade_durability(world: Any, decay: int) -> None:
    """陷阱磨损：已装备武器/护甲耐久 -decay（不朽 durability_max<=0 跳过；仿 _degrade_equipment）。"""
    eq = getattr(world.player, "equipped", None)
    if not isinstance(eq, dict):
        return
    for iid in eq.values():
        it = next((i for i in (getattr(world, "items", None) or []) if getattr(i, "id", "") == iid), None)
        if it is None or getattr(it, "type", "") not in ("weapon", "armor"):
            continue
        if int(getattr(it, "durability_max", 0) or 0) <= 0:
            continue
        try:
            it.durability = max(0, int(getattr(it, "durability", 0) or 0) - int(decay))
        except Exception:
            pass


# ============ 胜利结算（finish_combat 钩子）============
def on_combat_victory(world: Any, target_npc: Any, parts: list, summary: dict) -> None:
    """秘境怪击败收尾：房间 done / 层 cleared / Boss -> 保底奖励 + 封印。best-effort。

    [B01-F07] 双重去重（幂等）：(1) 怪的 run_id 落后于秘境当前周期 = 旧周期延迟收尾，
    拒绝（新周期房间不得被旧战果结算）；(2) 目标房间已 done 且非 pending = 同一胜利
    重复回调，拒绝（不再发钱/经验/通关数）。外层 finish_combat 另有 session 收尾戳。
    """
    tag = getattr(target_npc, "dungeon_tag", None)
    if not isinstance(tag, dict) or not tag.get("dungeon_id"):
        return
    dungeon = next((d for d in (getattr(world, "dungeons", None) or [])
                    if isinstance(d, Dungeon) and d.id == tag.get("dungeon_id")), None)
    if dungeon is None:
        return
    # [B01-F07] 旧周期怪的收尾拒绝（regenerate 已把 run_id +1 且重铺 floors）
    if int(tag.get("run_id", 1) or 1) != max(1, int(getattr(dungeon, "run_id", 1) or 1)):
        return
    fidx = int(tag.get("floor", 1))
    floor = next((f for f in dungeon.floors if f.index == fidx), None)
    room = None
    _rooms_before = _rooms_done(dungeon)   # [S04] 战斗房 done 差分喂任务进度
    if floor is not None:
        ridx = int(tag.get("room", -1))
        if 0 <= ridx < len(floor.rooms):
            room = floor.rooms[ridx]
            # [B01-F07] 已结算房间（done 且非 pending）重复收尾拒绝
            if room.done and not room.pending:
                return
            room.done = True
            room.pending = False
        _floor_check_cleared(floor)   # 非 boss 层全房 done -> cleared（多层推进）
    rng = SeededRng.seed_from(getattr(world, "id", ""), 0,
                              f"dgnwin_{dungeon.id}_{fidx}_{tag.get('room')}")
    fm = floor_mult(fidx)

    if tag.get("boss"):
        if floor is not None:
            floor.cleared = True
        last = (floor is not None and floor.index == len(dungeon.floors))
        # Boss 奖励：技能书 + epic 物品 + 大额金币（深层倍率）。
        # [用户指示 2026-08-21] 技能书不保底，按掉落公式 roll（基础率 0.75 x 幸运系数，
        # 钳 0.95——与 roll_loot 同口径，difficulty 维度此处不可得按 1.0）；epic 物品仍是保底
        # （高 level 物品三渠道之一，非技能书）。
        book, _prefer = ene._pick_skill_book(world, rng)
        held: list = []
        if book is not None:
            luk = te.effective_stat(world.player, "luk") + te.get_talent_bonus(world.player, "luck_bonus")
            rate = max(0.0, min(0.95, 0.75 * (1 + luk * 0.01)))
            if rng.chance(rate):
                if _give_item(world, book):
                    parts.append(f"首领被击败！缴获传承「{book.name}」")
                else:
                    held.append(book.id)
                    parts.append(f"首领遗落的传承「{book.name}」你暂时拿不下（背包已满，稍后再来取）")
        epic = ene._pick_reward_item(world, rng, "epic")
        if epic is not None:
            if _give_item(world, epic):
                parts.append(f"首领土崩瓦解，掉落了珍贵的「{epic.name}」")
            else:
                held.append(epic.id)
                parts.append(f"首领掉落的「{epic.name}」你暂时拿不下（背包已满，稍后再来取）")
        if held and room is not None:
            # [B01-F06] 满包滞留：记在 Boss 房上，深入补发（封印中亦放行，见 service 门控）
            room.pending_items = list(getattr(room, "pending_items", None) or []) + held
            summary["dungeon_pending_loot"] = [h for h in room.pending_items]
        gold = _gold(world, int((30 + dungeon.danger * 15) * fm * (1 + 0.15 * dungeon.clears)))
        world.player.gold = int(getattr(world.player, "gold", 0) or 0) + gold
        parts.append(f"获得 {gold} {_currency(world)}")
        summary["dungeon_boss"] = {"dungeon": dungeon.name, "floor": fidx}
        if last:
            dungeon.clears = int(dungeon.clears or 0) + 1
            dungeon.status = "sealed"
            dungeon.cooldown_until_day = int(getattr(world, "day_count", 1) or 1) + COOLDOWN_DAYS
            parts.append(f"秘境「{dungeon.name}」被彻底攻克（第 {dungeon.clears} 次）！"
                         f"入口将封印 {COOLDOWN_DAYS} 天后重现")
            summary["dungeon_cleared"] = {"dungeon": dungeon.name, "clears": dungeon.clears}
            _quest_hook(world, dungeon, "dungeon_cleared")   # [S04] 通关喂任务
        else:
            parts.append(f"秘境「{dungeon.name}」第{fidx}层肃清，更深处已在脚下")
    else:
        parts.append(f"{dungeon.name}第{fidx}层的「{target_npc.name}」被击败，通路洞开")
    if _rooms_done(dungeon) > _rooms_before:               # [S04] 战斗房 done 钩子
        _quest_hook(world, dungeon, "dungeon_room_resolved")
    summary["world_changed"] = True


# ============ tick 重生（world_tick_engine 调用）============
def tick_regenerate(world: Any) -> list:
    """封印到期重生（确定性同 seed 重铺）。返回 WorldEvent 列表。"""
    from src.models.world import WorldEvent
    events: list = []
    day = int(getattr(world, "day_count", 1) or 1)
    for d in (getattr(world, "dungeons", None) or []):
        if not isinstance(d, Dungeon):
            continue
        if d.status != "sealed" or day < int(d.cooldown_until_day or 0):
            continue
        if world.player.location_id == d.interior_id or has_pending_loot(d):
            continue
        regenerate(world, d)
        events.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0), category="event", severity="minor",
            title="秘境封印松动",
            desc=f"「{d.name}」的封印到期松动，深处重新有了动静（难度提升）",
            locations=[d.location_id],
        ))
    return events


# ============ 场景上下文（service 单一来源调用）============
def scene_block(world: Any, dungeon: Dungeon) -> str:
    """【秘境】块文本（full/compact 都注入：settle 判「深入」意图需要）。"""
    floor, room, ridx = _target_room(dungeon)
    total_rooms = sum(len(f.rooms) for f in dungeon.floors)
    done_rooms = sum(1 for f in dungeon.floors for r in f.rooms if r.done)
    ent = entrance_location(world, dungeon)
    # [B01-F06] 满包滞留奖励提示（玩家可见，因果可见性；清包后「深入」即补发）
    pend_names: list = []
    for _f in dungeon.floors:
        for _r in _f.rooms:
            for _iid in (getattr(_r, "pending_items", None) or []):
                _it = next((i for i in (getattr(world, "items", None) or [])
                            if getattr(i, "id", "") == _iid), None)
                if _it is not None:
                    pend_names.append(f"「{_it.name}」（{_r.name}）")
    _pend_line = (f"此处尚有未取走的收获：{'、'.join(pend_names[:6])}——"
                  f"清出背包并返回来源房间，用「深入」取回" if pend_names else "")
    if floor is None:   # 全清（通关后尚未离开的窗口期）
        base = (f"【秘境】「{dungeon.name}」内部已被探索殆尽（{done_rooms}/{total_rooms}），"
                f"可移动回入口「{ent.name if ent else dungeon.name}」离开")
        return base + (f"。{_pend_line}" if _pend_line else "")
    lines = [f"【秘境】你身处「{dungeon.name}」内部（第{floor.index}/{len(dungeon.floors)}层，"
             f"探索进度 {done_rooms}/{total_rooms}）"]
    # [D01] 当前位置 + 已知出口（玩家看得到「走哪条路」）
    _pf, _pr = None, None
    _pos = position_room_id(dungeon)
    if _pos:
        _pf, _pr = next(((f2, r2) for f2 in dungeon.floors for r2 in f2.rooms
                         if str(getattr(r2, "id", "") or "") == _pos), (None, None))
    if _pr is not None:
        _st = "已探明" if _pr.done else ("战斗一触即发" if _pr.pending else "未探明")
        lines.append(f"你此刻在：「{_pr.name}」（{_st}）")
    _exits = known_exits_line(world, dungeon)
    if _exits:
        lines.append(_exits + "（移动只走一条通道；抵达后再搜索。多个方向须选择，不能隔空结算）")
    if room is not None and not room.done:
        state = "战斗一触即发" if room.pending else "未探索"
        if room.type == "check" and getattr(room, "seen", False) and not room.done:
            state = "已观察（可破解）"          # [D02] 机关两段式中间态
        if room.type == "trap" and room.seen and not room.done:
            state = "已发现（可解除或绕行）"
        lines.append(f"前方：{room.name}（{state}）——{room.desc}")
        if room.pending:
            lines.append("眼前之敌未除，无法继续深入（击败它才能推进）")
        if room.type in ("check", "trap") and room.seen and not room.check_result:
            lines.append("再次深入即尝试解除；安全绕行用 move_to 填「"
                         + ("绕过机关" if room.type == "check" else "绕过陷阱") + "」")
        if room.shortcut:
            lines.append("机关控制的捷径：" + ("已开启" if room.shortcut_open else "关闭，破解成功才开通"))
        tools = interaction_tools(world, dungeon)
        if tools:
            lines.append("可消耗的工具：" + "、".join(f"「{it.name}」" for it in tools[:3])
                         + "；使用物品意图 use_item，target 填工具名（耗一件，基础成功率 +5 个百分点）")
        if room.type == "treasure":
            lines.append("开箱时一并检查暗格，本周期只检查一次；额外收获也可满包留存后取回")
    else:
        lines.append("当前房间已探明；深入只在唯一可行未探方向时移动一步，多个方向须明确选择")
    if floor is not None and room is not None:
        if room.id == floor.exit_room_id and floor is not dungeon.floors[-1]:
            lines.append("下层楼梯/首领门：" + ("已打开，可移动到「下层楼梯」" if room_resolved(room) else "须先处理当前出口房间"))
        if room.id == floor.entry_room_id and floor is not dungeon.floors[0]:
            lines.append("返回上一层用 move_to 填「上层楼梯」")
    # [D02] 首领威慑预告（与 build_dungeon_monster 结算同口径：每级血量 -15%）
    _relief = max(0, min(2, int(getattr(dungeon, "boss_relief", 0) or 0)))
    if _relief:
        lines.append(f"已破解 {_relief} 处机关——最深处首领的威势被削弱"
                     f"（首领血量 -15%x{_relief}）")
    if _pend_line:
        lines.append(_pend_line)
    ok_exit, exit_reason = can_exit_dungeon(dungeon)
    lines.append(f"要互动当前房间，意图用移动（move_to 填「深入」）；离开须逐房返回第一层入口，再移动到「{ent.name if ent else dungeon.name}」"
                 + ("（现在可离开）" if ok_exit else f"；{exit_reason}"))
    return "\n".join(lines)


def match_theme(genre_id: str, text: str) -> str:
    """[P25a] 据拓展 LLM 的 theme_hint/名称匹配题材池主题 id（双向子串；未命中空串）。"""
    t = str(text or "").strip()
    if not t:
        return ""
    for theme in theme_pool(genre_id):
        name = str(theme.get("name", ""))
        if name and (name in t or t in name):
            return str(theme.get("id", ""))
    return ""
