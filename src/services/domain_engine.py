"""[D1 2026-08-29] 玩家据点引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守 §21b）：LLM 只出据点名/一句描写的语义（WorldSimService.purchase_domain
起名小调用，失败兜底本模块题材池名），购地价/翻转改造/场所铺设/资金池存取全在本模块
纯 Python + SeededRng 确定性结算（同 world+tick+salt 同结果）。

购地 = 把 kind=wilderness 野外地点改造为玩家私人据点：kind 翻转 settlement +
settlement_size 定档 village（居民上限 6，升档 town/city 是 D2/D5）+ 清空势力归属
（无主私有地；势力侵袭快防是 D4）+ 地点改名 = 据点名（地图立即可见，因果可见性）+
铺设「营门/营地广场」2 初始场所（互连并接原默认场所，default_place_id=营门，守 P27
双向连接不变量）。原野外资源点保留（买地含矿脉，据点内可采集是福利）。每地点限购
一次（Location.player_owned 门控）；多地点可多据点（仿多宅）。

题材化池容量守 §23 数据量铁律（tests/test_genre_data_volume.py::TestDomainPool 守护：
据点名 >=6/题材、营门名 >=4/题材、广场名 >=4/题材）。
"""
from __future__ import annotations

from typing import Optional

from src.models.world import (World, PlayerDomain, Place, WorldEvent, NPC, Shop,
                              effective_category, _DOMAIN_FACILITY_KIND_VALUES)
from src.models.shop import ShopStockEntry
from src.models.world_sim_preset import GenreText
from src.services import combat_engine as ce
from src.services import npc_life_engine as nle
from src.services.trade_engine import pace_factors, world_pace
from src.services import trade_engine as tre
from src.utils.rng import SeededRng

# ---- 购地基价（用户定稿 2026-08-28）：20000 x 2^(danger-1)。danger 1 = 2 万，
# danger 10 = 1024 万（天价，后期财富沉淀方向）。公式即定义，不乘 economy_pace。
_DOMAIN_BASE_PRICE = 20000

# [D1 定稿] 居民上限按聚落档：village 6 / town 12 / city 20（升档入口在 D2/D5）。
_RESIDENT_LIMITS = {"village": 6, "town": 12, "city": 20}

# ---- [D2] 职业与工资（用户定稿 2026-08-28）----
# 职业按场所类型定：掌柜/伙计挂 shop 设施，打手挂 guard，农夫挂 farm。
ROLE_LABELS = {"shopkeeper": "掌柜", "handyman": "伙计", "guard": "打手", "farmer": "农夫"}


# ---- [P61 据点小人剧 + 专精定策 2026-09-26 用户拍板] ----
_STAFF_SPECIALTIES = {
    # 同角色两个专精共享同一机械加成（Flavor 区分；入职时确定性赋予）
    "shopkeeper": ("账目清明", "进退有度"),   # 日收入 +5%
    "handyman": ("采买眼力", "肩挑背扛"),     # 日产 +1
    "guard": ("操练严格", "胆气过人"),        # 历练成功率 +0.1
    "farmer": ("选种好手", "勤于灌溉"),       # 日产 +1
}
_POLICY_VALUES = ("", "output", "military", "quality")
_POLICY_LABELS = {"": "无为而治", "output": "增产", "military": "武备", "quality": "重质"}

_STAFF_VIGNETTE_CHANCE = 0.15   # 每职员每日一条小剧的概率
_STAFF_VIGNETTE_CAP = 2         # 每据点每日至多 2 条（防刷屏）

# 小人剧池：6 题材 x 4 角色 x 3 变体（{name}/{domain} 占位；数据量铁律 [P61]）
_GENRE_STAFF_VIGNETTES: dict = {
    "xianxia": {
        "shopkeeper": ["{name}翻着账册嘀咕：这月的灵石流水比上月厚实了几分。",
                       "{name}把柜上最上乘的丹药擦了一遍又一遍，说是贵客爱看体面。",
                       "{name}同一位老主顾攀谈半晌，记下了几桩想要的东西。"],
        "handyman": ["{name}挑着担子进山去了，回来时扁担压得咯吱作响。",
                     "{name}把库里灵材细细分拣，说是乱堆的料子伤品相。",
                     "{name}打听了三处货源，夸口下批货能省两成开销。"],
        "guard": ["{name}在院里扎了一早上的马步，汗把青石板都洇湿了。",
                  "{name}擦拭着刀刃，念叨着近来山里狼群不太平。",
                  "{name}同路过的修行者过了两招，回来时满面红光。"],
        "farmer": ["{name}蹲在灵田边捻着土，盘算着该添些肥料了。",
                   "{name}引了半日灵泉浇灌，田垄齐整得能当镜子。",
                   "{name}念叨着天时，把几畦幼苗挪到了向阳处。"],
    },
    "western_fantasy": {
        "shopkeeper": ["{name}数着铜币打烊，嘴里哼着跑调的小曲。",
                       "{name}把柜台擦得锃亮，说体面能招来体面的客人。",
                       "{name}同一位老主顾攀谈半晌，记下了几桩想要的东西。"],
        "handyman": ["{name}扛着半车木料回来，肩膀上磨出了新的茧。",
                     "{name}把库房码得整整齐齐，说乱堆的货会自己烂掉。",
                     "{name}打听了三处货源，夸口下批货能省两成开销。"],
        "guard": ["{name}在栅栏边巡逻到深夜，火把换了两支。",
                  "{name}磨了一早上长矛，念叨着林子里狼群不太平。",
                  "{name}同路过的佣兵掰了掰手腕，回来时满面红光。"],
        "farmer": ["{name}蹲在田埂上捻着土，盘算着该添些肥料了。",
                   "{name}牵着水牛犁了半日地，垄沟直得像用墨线弹过。",
                   "{name}念叨着天时，把几畦菜苗挪到了向阳处。"],
    },
    "wuxia": {
        "shopkeeper": ["{name}拨着算盘核账，说这月镖货的抽成厚了几分。",
                       "{name}把柜台上的兵器擦了又擦，说是行家爱看锋口。",
                       "{name}同一位老主顾攀谈半晌，记下了几桩想要的东西。"],
        "handyman": ["{name}挑着担子跑遍了半座镇子，回来时扁担压弯了。",
                     "{name}把库房里的皮货药材分拣利落，说乱堆伤品相。",
                     "{name}打听了三处货源，夸口下批货能省两成开销。"],
        "guard": ["{name}在后院扎了一早上的桩，汗把青砖洇湿了一片。",
                  "{name}擦着朴刀念叨：近来道上不太平，手上得见真章。",
                  "{name}同过路的镖师拆了几招，回来时满面红光。"],
        "farmer": ["{name}蹲在田头捻着土，盘算着该添些肥了。",
                   "{name}引水灌了半日垄，泥浪平得像熨过。",
                   "{name}念叨着节气，把几畦菜苗挪到了向阳处。"],
    },
    "modern": {
        "shopkeeper": ["{name}对着账本皱眉又舒展：这月流水总算涨了。",
                       "{name}把货架重新码了一遍，说陈列就是无声的招牌。",
                       "{name}同一位老主顾攀谈半晌，记下了几桩想要的东西。"],
        "handyman": ["{name}开着小货车跑了两趟仓库，后备箱塞得满满当当。",
                     "{name}把库存台账更新到最新，说糊涂账吃人。",
                     "{name}比了三家供应商的报价，夸口下批货能省两成开销。"],
        "guard": ["{name}绕着院子走了十圈，说是值班也得保持体能。",
                  "{name}检查了每一处摄像头，念叨着最近片区不太平。",
                  "{name}同巡逻的片警聊了半天防范，回来时满面红光。"],
        "farmer": ["{name}在大棚里忙到晌午，说温度计比闹钟还准。",
                   "{name}给滴灌管换了个新滤芯，水珠匀得像下雨。",
                   "{name}翻着农情预报，把秧盘挪到了向阳的垄上。"],
    },
    "scifi": {
        "shopkeeper": ["{name}核对着补给清单，说本季度的配额宽裕了些。",
                       "{name}把展柜里的货品标价重新校准，说信息就是利润。",
                       "{name}同一位老主顾攀谈半晌，记下了几桩想要的东西。"],
        "handyman": ["{name}驾驶搬运艇在库区穿梭了整班，说效率就是生命。",
                     "{name}给库存芯片重新编目，说乱码的仓库会吃人。",
                     "{name}比对了三家的报价，夸口下批货能省两成开销。"],
        "guard": ["{name}在气闸口站了一班岗，甲胄的伺服电机嗡嗡作响。",
                  "{name}校准了哨戒炮的火控，念叨着外环近来不太平。",
                  "{name}同巡逻机甲的驾驶员过了两招模拟战，回来时满面红光。"],
        "farmer": ["{name}蹲在培养舱前看营养液流速，说作物比人娇贵。",
                   "{name}调整了光照光谱，说这一茬苗能壮两成。",
                   "{name}查着气象矩阵，把几盘秧苗挪到了高光照层。"],
    },
    "apocalypse": {
        "shopkeeper": ["{name}用弹壳在账板上做记号，说这月的进项能换两箱罐头。",
                       "{name}把最耐放的口粮摆在最顺手的位置，说方便就是安全。",
                       "{name}同一位老主顾攀谈半晌，记下了几桩想要的东西。"],
        "handyman": ["{name}推着改装手推车出了废弃区，回来时车斗堆得冒尖。",
                     "{name}把库房里的罐头按保质期重排，说乱堆会要命。",
                     "{name}打听了三处废墟，夸口下批货能省两成开销。"],
        "guard": ["{name}在瞭望位蹲了一整夜，枪管擦得能照出人影。",
                  "{name}加固了大门的横闩，念叨着夜里的爪印不太对劲。",
                  "{name}同路过的武装商队掰了掰腕子，回来时满面红光。"],
        "farmer": ["{name}在温室棚膜上补了三块补丁，说这点绿比金子金贵。",
                   "{name}提着接的雨水浇了垄，说天不下雨就得自己想辙。",
                   "{name}翻着植物手册，把几盘苗挪到了光照最好的架子。"],
    },
}
# 职业 -> 依赖设施 kind（None = 无设施依赖；现四职业全依赖设施）。
ROLE_FACILITY = {"shopkeeper": "shop", "handyman": "shop", "guard": "guard", "farmer": "farm"}
_WAGE_PER_LEVEL = 2          # 日工资 = 2 x 等级（发进 NPC.wallet，喂 P39a 生活开销/互市）
_UNPAID_LEAVE_DAYS = 3       # 连续欠薪 3 天全员离职
_POACH_FEE_PER_LEVEL = 200   # 挖角签约金 = 200 x 等级（从资金池扣）
# 建造耗材：锻造类 x2 + 制造类 x2（背包按类别扣；比住宅建筑 1+1 多——整座场所）。
_BUILD_MATS = (("forge", 2), ("craft", 2))
# 打手入职战力基线（引擎公式，随玩家等级成长便于 D4 防务跟得上）：等级 = max(3, 玩家等级)。
_GUARD_MIN_LEVEL = 3

# ---- [D5] 客流经济常量（用户定稿 2026-08-28：客流 = 基数 x 档位 x (1+繁荣%) x 声望 x 竞争）----
_FLOW_BASE = 20                   # 客流基数（人/日）
_TIER_FLOW = {"village": 1.0, "town": 1.8, "city": 3.0}   # 聚落档客流倍率
_SPEND_PER_VISITOR = 4            # 人均消费基数（题材货币/客）
_NO_SHOP_FLOW_CUT = 0.2           # 无营业商铺时客流消费折减（路人零星）
_FACILITY_SPEND_BONUS = 0.15      # 每座设施人均消费 +15%（设施丰富留住客）
_FACILITY_UPKEEP = {"shop": 20, "guard": 12, "farm": 8}    # 设施日维护费
_VISIT_CHANCE = 0.15              # 相邻地点每 NPC 每日来访概率
_VISIT_CAP = 4                    # 每日具名来访 NPC 上限（防事件刷屏）
# [D5] 升聚落档：village->town / town->city（资金池价 + 繁荣度门槛；居民上限 6/12/20 联动）
_TIER_ORDER = ("village", "town", "city")
_TIER_UPGRADE = {"village": (8000, 25), "town": (30000, 50)}   # 当前档: (升档价, 繁荣度>=)

# ---- [D4 2026-08-29] 防务练级（用户定稿：train 日动作 + 闹事武力结算 + 势力侵袭快防）----
_TRAIN_CHANCE = 0.7            # 打手每日外出历练概率
_TRAIN_XP_BASE = 10            # 历练 xp = 10 + 等级（等级越高阅历要求越高）
_TRAIN_INJURY_CHANCE = 0.15    # 历练负伤概率（hp -15~25%，保 1 不死）
_TROUBLE_CHANCE = 0.08         # 每日闹事概率（客流大是非多：闹事战力 = 15 + 客流//2）
_TROUBLE_FAIL_PROSPERITY = 2   # 闹事弹压失败掉繁荣
_RAID_CHANCE = 0.05            # 每日势力侵袭概率（须存在敌对势力）
_RAID_HOSTILE_REP = -25        # 玩家对该势力声望 <= 此值算敌对（与 P45 商店/社交阈值同口径）
_RAID_ENEMY_BASE = 40          # 敌方战力 = 40 + 势力 power(0-100) + 抖动 0-20
_RAID_FAIL_PROSPERITY = 5      # 侵袭失守掉繁荣
_RAID_FAIL_FUNDS_PCT = 0.10    # 侵袭失守资金池被劫一成
_RAID_WIN_REP = 2              # 击退来犯 +2 声望（打服了：敌意随胜绩消退，钳 -100..100）
# 城防（快防防御加成）：岗哨设施 +25；聚落档 10/20/30（升格的防务回报）
_CITY_DEF_GUARD_FAC = 25
_CITY_DEF_TIER = {"village": 10, "town": 20, "city": 30}

# ---- [D6 2026-08-29] 委托板（用户定稿：资金池出委托 + 附近 NPC 判断值不值 + 交付入账/过期退款）----
_COMMISSION_TOIL = 1.3          # 辛劳系数：报酬 >= 货价 x 数量 x 1.3 NPC 才觉得值
_COMMISSION_CONSIDER = 0.3      # 候选 NPC 每日琢磨委托的概率
_COMMISSION_OPEN_CAP = 3        # 同时挂单上限（防资金池被押空）
_COMMISSION_QTY_MAX = 3         # 单笔数量上限
_COMMISSION_DAYS_MAX = 3        # 挂单天数上限（1-3 天过期）
_COMMISSION_KEEP = 8            # done/expired 记录保留条数（滚动裁剪防膨胀）

# ---- 据点题材池（6 题材全覆盖，缺键回退西幻，守 §23）----
# names: 据点名兜底池（LLM 起名失败时确定性挑选）；gate/yard: 初始 2 场所名池 +
# gate_type/yard_type 场所类型关键词（叙事/二层地图展示用）。
_GENRE_DOMAIN_TEMPLATES: dict[str, dict] = {
    "western_fantasy": {
        "names": ["白鹿庄园", "晨曦堡", "橡木要塞", "银泉营地", "灰鹰堡垒", "月桂庄园", "暮色庄园", "赤岩堡"],
        "gate": ["城门", "木寨门", "石拱门", "吊闸门"],
        "gate_type": "大门",
        "yard": ["集市广场", "演兵场", "中央庭院", "营地空地"],
        "yard_type": "广场",
        "events_buy": [
            "{a}在{shop}挑走了{item}，临走还夸这里货真价实。",
            "风尘仆仆的{a}慕名而来，在{shop}买下{item}便匆匆赶路。",
            "{a}在{shop}用零钱包袱换来{item}，说是替家里人捎的。",
            "老主顾{a}又来{shop}了，这回相中了{item}，价钱都没还。",
        ],
        "events_visit": [
            "商旅们口耳相传，{domain}的名头在官道上越传越远。",
            "几个行脚商人绕道前来{domain}歇脚，顺带打听这里的行情。",
            "吟游诗人在酒馆唱起{domain}的故事，引来阵阵打听。",
            "{domain}的炊烟升起，路过的旅人纷纷驻足张望。",
        ],
        "facilities": [
            {"kind": "shop", "type": "商铺", "names": ["白鹿百货", "旅人商栈", "百宝集市", "杂货铺子"], "desc": "贩卖杂货与冒险补给的商铺。", "base_price": 2400},
            {"kind": "guard", "type": "岗哨", "names": ["哨塔", "木栅岗哨", "值夜岗亭", "瞭望塔"], "desc": "守夜瞭望的岗哨。", "base_price": 1500},
            {"kind": "farm", "type": "农田", "names": ["南亩田", "药草园圃", "麦浪农场", "东菜畦"], "desc": "耕作收获的农田。", "base_price": 1000},
        ],
    },
    "xianxia": {
        "names": ["灵峰寨", "青云坞", "玄天福地", "紫霞山庄", "灵犀谷", "方寸洞天", "聚灵坞", "星陨谷"],
        "gate": ["山门", "阵门", "石牌坊", "灵光门"],
        "gate_type": "山门",
        "yard": ["演武场", "坊市街", "云台广场", "灵田广场"],
        "yard_type": "广场",
        "events_buy": [
            "{a}在{shop}购得{item}，直叹此地灵货难得。",
            "赶路的{a}落脚{domain}，在{shop}选购{item}后连夜赶往坊市。",
            "{a}在{shop}以灵石换来{item}，说是闭关前最后一趟采购。",
            "熟客{a}再度登门{shop}，点名要了{item}。",
        ],
        "events_visit": [
            "往来修士提及{domain}，都道此地灵气渐旺、值得一行。",
            "有散修绕远路来{domain}采买，直说比坊市省了一半脚程。",
            "坊间传言{domain}有高人坐镇，引得修士纷纷来投。",
            "云雾间的{domain}灯火渐盛，路过的修士皆驻足观望。",
        ],
        "facilities": [
            {"kind": "shop", "type": "坊铺", "names": ["灵宝阁", "坊市铺面", "灵材铺", "云来商号"], "desc": "售卖灵材丹方的坊铺。", "base_price": 2400},
            {"kind": "guard", "type": "岗哨", "names": ["护山哨位", "值功殿", "巡山岗哨", "阵法哨位"], "desc": "巡山护派的岗哨。", "base_price": 1500},
            {"kind": "farm", "type": "灵田", "names": ["灵田一亩", "灵植圃", "药园", "灵谷田"], "desc": "培育灵植的田亩。", "base_price": 1000},
        ],
    },
    "wuxia": {
        "names": ["落雁山庄", "听雨别院", "铁剑门", "风陵渡口", "卧虎寨", "栖霞坞", "听松山庄", "断云坞"],
        "gate": ["寨门", "庄门", "牌坊", "木栅门"],
        "gate_type": "大门",
        "yard": ["演武场", "前院", "校场", "石坪"],
        "yard_type": "广场",
        "events_buy": [
            "{a}在{shop}买下{item}，抱拳称谢后翻身上马。",
            "江湖客{a}路过{domain}，在{shop}添置{item}以备远行。",
            "{a}在{shop}数出银钱换{item}，说是在下受了此地恩惠。",
            "镖师{a}照旧来{shop}补货，这回带走了{item}。",
        ],
        "events_visit": [
            "江湖上开始流传{domain}的名号，过往豪杰多来投宿。",
            "几名游侠特意绕道{domain}，只为见识此地气象。",
            "茶肆说书人把{domain}的故事编成了新段子。",
            "{domain}门前车马渐多，过路侠客皆来讨碗水喝。",
        ],
        "facilities": [
            {"kind": "shop", "type": "商铺", "names": ["悦来铺面", "镖局柜面", "百货行", "山货铺"], "desc": "南来北往货物的商铺。", "base_price": 2400},
            {"kind": "guard", "type": "岗哨", "names": ["护院岗", "巡夜棚", "岗楼", "演武哨位"], "desc": "看家护院的岗哨。", "base_price": 1500},
            {"kind": "farm", "type": "田庄", "names": ["庄子田", "稻香田", "药圃", "菜园"], "desc": "庄户耕作的田地。", "base_price": 1000},
        ],
    },
    "modern": {
        "names": ["星野营地", "拾光农庄", "山语墅园", "蓝湾基地", "青枫庄园", "岚山别业", "云顶营地", "半岛庄园"],
        "gate": ["大门", "入口岗亭", "电动门", "接待处"],
        "gate_type": "入口",
        "yard": ["中央草坪", "露天广场", "营火空地", "庭院"],
        "yard_type": "广场",
        "events_buy": [
            "{a}在{shop}结账买下{item}，还扫码给了个五星好评。",
            "专程驱车前来的{a}在{shop}选购了{item}，后备箱塞得满满当当。",
            "{a}在{shop}刷手机付了{item}的账，说要发朋友圈推荐{domain}。",
            "回头客{a}又来{shop}补货，顺手带走了{item}。",
        ],
        "events_visit": [
            "社交平台上{domain}的打卡帖多了起来，访客络绎不绝。",
            "附近居民开始习惯绕来{domain}采购周末的补给。",
            "本地博主探访{domain}的攻略帖下留言火热。",
            "{domain}的停车场难得有了排队的时候。",
        ],
        "facilities": [
            {"kind": "shop", "type": "商铺", "names": ["便利超市", "补给小站", "社区商店", "杂货铺子"], "desc": "日用补给的商店。", "base_price": 2400},
            {"kind": "guard", "type": "岗哨", "names": ["门岗亭", "安保哨位", "监控岗", "值班室"], "desc": "安保值守的岗亭。", "base_price": 1500},
            {"kind": "farm", "type": "农田", "names": ["温室大棚", "家庭农场", "菜地", "苗圃"], "desc": "耕种作物的农地。", "base_price": 1000},
        ],
    },
    "scifi": {
        "names": ["曙光前哨", "星环基地", "七号殖民地", "苍穹站", "静海前哨", "方舟营区", "晨星基地", "远地点站"],
        "gate": ["气闸门", "闸机口", "哨戒门", "隔离闸"],
        "gate_type": "入口",
        "yard": ["中央舱厅", "集合平台", "能源广场", "露天甲板"],
        "yard_type": "广场",
        "events_buy": [
            "{a}在{shop}以信用点换得{item}，随即接驳下一班穿梭机。",
            "殖民商{a}专程降落{domain}，从{shop}提走了{item}。",
            "{a}的终端在{shop}完成支付，{item}由无人机送达舱门口。",
            "老客户{a}向{shop}发来订单，照例补齐了{item}。",
        ],
        "events_visit": [
            "星港的航讯里开始出现{domain}的名字，泊位渐渐紧张。",
            "跑船的商队把{domain}列入固定补给线。",
            "殖民地网络的论坛上，{domain}的口碑持续走高。",
            "{domain}的信号灯在轨道上愈发显眼，来往飞船频频致意。",
        ],
        "facilities": [
            {"kind": "shop", "type": "补给站", "names": ["贸易终端", "物资配给处", "交换站", "补给柜台"], "desc": "物资交易与配给的补给站。", "base_price": 2400},
            {"kind": "guard", "type": "岗哨", "names": ["哨戒岗", "防卫哨位", "安保节点", "警戒塔"], "desc": "戒备防御的哨位。", "base_price": 1500},
            {"kind": "farm", "type": "种植舱", "names": ["水培舱农场", "培植舱", "生态温室", "种植平台"], "desc": "水培育植的种植舱。", "base_price": 1000},
        ],
    },
    "apocalypse": {
        "names": ["磐石避难所", "灰烬堡垒", "荆棘营地", "灯塔据点", "方舟废墟站", "锈铁要塞", "绿洲营区", "曙光定居点"],
        "gate": ["防波闸门", "铁丝网门", "哨塔入口", "防爆门"],
        "gate_type": "入口",
        "yard": ["集散广场", "篝火空地", "种植平台", "中央营地"],
        "yard_type": "广场",
        "events_buy": [
            "{a}用攒下的物资券在{shop}换走{item}，千恩万谢。",
            "风尘满面{a}来到{domain}，在{shop}用旧世界的硬币买下{item}。",
            "{a}在{shop}以物易物换来{item}，说要带回去给聚落的孩子们。",
            "老住户{a}又来{shop}领配给，这回拿了{item}。",
        ],
        "events_visit": [
            "幸存者营地里传开了{domain}物资充足的消息，投奔者渐多。",
            "路过的商队把{domain}当作安全中继站，交换着各处的消息。",
            "电台里有人念叨{domain}的名号，说那里还有秩序。",
            "{domain}的探照灯彻夜亮着，废土上的旅人循光而来。",
        ],
        "facilities": [
            {"kind": "shop", "type": "交换铺", "names": ["以物易物铺", "配给站", "旧货摊", "物资交换点"], "desc": "幸存者交换物资的铺面。", "base_price": 2400},
            {"kind": "guard", "type": "岗哨", "names": ["哨塔", "铁丝网岗", "瞭望位", "拒马哨"], "desc": "防备威胁的哨位。", "base_price": 1500},
            {"kind": "farm", "type": "净化圃", "names": ["净化圃", "种植平台", "辐射温室", "菜窖田"], "desc": "净化土壤后的种植圃。", "base_price": 1000},
        ],
    },
}


def genre_pool(world: World) -> dict:
    """读当前世界的题材据点池（attribute_template_id 选池，缺键回退西幻）。"""
    tid = (getattr(world, "config_overlay", None) or {}).get("attribute_template_id") \
        or "western_fantasy"
    return _GENRE_DOMAIN_TEMPLATES.get(tid) or _GENRE_DOMAIN_TEMPLATES["western_fantasy"]


def domain_price(loc) -> int:
    """购地价 = 20000 x 2^(danger-1)（用户定稿公式即定义；danger 钳 1-10）。"""
    try:
        danger = max(1, min(10, int(getattr(loc, "danger", 1) or 1)))
    except (TypeError, ValueError):
        danger = 1
    return _DOMAIN_BASE_PRICE * (2 ** (danger - 1))


def resident_limit(settlement_size: str) -> int:
    """居民上限：village 6 / town 12 / city 20（空/未知按 village 基档）。"""
    return _RESIDENT_LIMITS.get(str(settlement_size or ""), 6)


# ---- [据点仓库 2026-09-07 用户指示] 仓库容量/日产出/合成上限 ----
_WAREHOUSE_CAPS = {"village": 30, "town": 60, "city": 90}
_WAREHOUSE_DAILY = 5             # 伙计/农夫日产件数（原材料只出白绿；掌柜可合蓝成品）
_WH_RARITIES = ("common", "uncommon")
_WH_CRAFT_PER_DAY = 3           # 掌柜日合成上限（防货架堆积，同 do_stock_shop 日 3 件口径）


def warehouse_cap(settlement_size: str) -> int:
    """据点仓库容量：village 30 / town 60 / city 90（随升档扩容，居民上限同套档位）。"""
    return _WAREHOUSE_CAPS.get(str(settlement_size or ""), 30)


def warehouse_deposit(world: World, domain: PlayerDomain, item_id: str) -> bool:
    """玩家入仓（背包 -> 仓库，容量校验；仓库不占负重，同住宅 stash 口径）。"""
    inv = getattr(world.player, "inventory", None)
    if not isinstance(inv, list) or item_id not in inv:
        return False
    loc = _domain_loc(world, domain)
    cap = warehouse_cap(getattr(loc, "settlement_size", "") or "") if loc is not None else 0
    if len(domain.warehouse or []) >= cap:
        return False
    inv.remove(item_id)
    domain.warehouse.append(item_id)
    return True


def warehouse_withdraw(world: World, domain: PlayerDomain, item_id: str) -> bool:
    """玩家出仓（仓库 -> 背包）。[!] 必须走 try_add_to_inventory（负重校验，守 §22 入包铁律）。"""
    wh = domain.warehouse if isinstance(domain.warehouse, list) else []
    if item_id not in wh:
        return False
    if not ce.try_add_to_inventory(world.player, item_id):
        return False
    wh.remove(item_id)
    return True


def _crop_item_pool(world: World) -> list:
    """本题材作物物品（farm_engine 作物池确定性 id 反查 world.items；种子/缺项跳过）。"""
    from src.services import farm_engine as fe
    tid = (getattr(world, "config_overlay", None) or {}).get("attribute_template_id") \
        or "western_fantasy"
    by_id = {it.id: it for it in (world.items or [])}
    out = []
    for c in (fe.genre_crops(world) or []):
        it = by_id.get(fe.crop_id_of(str(tid), str(c.get("name") or "")))
        if it is not None and it.rarity in _WH_RARITIES:
            out.append(it)
    return out


def _tick_warehouse(world: World, domain: PlayerDomain, loc, eff_day: int) -> list:
    """[据点仓库 2026-09-07 用户指示] 日产出：伙计采 5 件白绿材料 / 农夫收 5 件白绿作物
    入仓（仓库满则当项停产）+ 掌柜从仓库取料合成（无建筑门控配方，蓝封顶，日 3 件）
    无订单时成品上架商铺；订单可定向送商铺、据点仓库或同地住宅仓库。

    独立 salt SeededRng（world.id + eff_day + domain.id），不消费日结算既有 rng 序列
    （守「rng 消费在日结算序列尾部不漂移」契约）；事件走因果可见性（右侧动态栏一行）。
    """
    evts: list = []
    wh = domain.warehouse if isinstance(domain.warehouse, list) else []
    domain.warehouse = wh
    cap = warehouse_cap(getattr(loc, "settlement_size", "") or "")
    rng_w = SeededRng.seed_from(world.id, int(eff_day), f"domain_wh_{domain.id}")
    staff = {str(r.get("role") or ""): roster_npc(world, str(r.get("npc_id") or ""))
             for r in roster_of(domain)}
    parts: list[str] = []
    # [P48 订单化 2026-09-25] 有 open 单时 80% 定向采缺料（不再纯随机囤货）——
    # 「伙计采的料正对着订单缺的料」，生产从黑箱变成可控。
    need_ids = _order_needs(world, domain)

    def _gather(pool, who_label: str, noun: str, verb: str, worker, days: int = _WAREHOUSE_DAILY) -> int:
        got = 0
        targeted = False
        for _ in range(days):
            if len(wh) >= cap or not pool:
                break
            it = None
            if need_ids:
                hits = [x for x in pool if x.id in need_ids]
                if hits and rng_w.chance(0.8):
                    it = rng_w.pick(hits)
                    targeted = True
            if it is None:
                it = rng_w.pick(pool)
            if it is not None:
                wh.append(it.id)
                if it.id in need_ids:
                    need_ids[it.id] -= 1
                    if need_ids[it.id] <= 0:
                        del need_ids[it.id]
                got += 1
        if got:
            # 文案保留品类词（「作物」「材料」是既有测试与玩家读数的锚）
            parts.append(f"{who_label}{worker.name}{verb}得 {got} 件{noun}入仓"
                         + ("（按订单定向）" if targeted else ""))
        return got

    # ---- 伙计：日产 5 件白绿材料（排除农田种子/作物——那是农夫的产线）----
    # [P61 专精定策] 专精/「增产」策略各 +1（采买眼力·肩挑背扛 / 增产）
    handy = staff.get("handyman")
    if handy is not None and handy.alive:
        pool = [it for it in (world.items or [])
                if getattr(it, "type", "") == "material"
                and getattr(it, "rarity", "") in _WH_RARITIES
                and not str(getattr(it, "id", "")).startswith(("seed_", "crop_"))]
        handy_days = _WAREHOUSE_DAILY + (1 if _staff_specialty(domain, "handyman") else 0)             + (1 if getattr(domain, "policy", "") == "output" else 0)
        _gather(pool, "伙计", "材料", "采", handy, days=handy_days)

    # ---- 农夫：日产 5 件白绿作物入仓 ----
    farmer = staff.get("farmer")
    if farmer is not None and farmer.alive:
        farmer_days = _WAREHOUSE_DAILY + (1 if _staff_specialty(domain, "farmer") else 0)             + (1 if getattr(domain, "policy", "") == "output" else 0)
        _gather(_crop_item_pool(world), "农夫", "作物", "收", farmer, days=farmer_days)

    # ---- 掌柜：仓库取料合成 -> 成品上架据点货架（同品不叠，do_stock_shop 口径）----
    keeper = staff.get("shopkeeper")
    if keeper is not None and keeper.alive:
        shop = next((s for s in (world.shops or [])
                     if s.location_id == loc.id and s.shop_type == "general"), None)
        if shop is not None:
            item_by_id = {it.id: it for it in (world.items or [])}
            # [拍品隔离 2026-09-09] 配方产出指向拍品（拓展配方名解析进拍名）-> 不造不上架。
            from src.services.world_sim_service import _auction_lot_ids
            _auc = _auction_lot_ids(world)
            crafted: list[str] = []
            done_txt: list[str] = []
            stall_flag = False
            open_orders = _order_active(domain)
            made_today = 0
            # [P48 订单化] 先按 open 单合成（逐件出货：上架或入仓；到量发完工事件，
            # 连续 3 日断料转 stalled 停工待料）——订单就是产线的调度单。
            for o in open_orders:
                if made_today >= _WH_CRAFT_PER_DAY:
                    break
                r = next((r for r in (getattr(world, "recipes", None) or [])
                          if getattr(r, "id", "") == str(o.get("recipe_id", ""))), None)
                out = item_by_id.get(str(getattr(r, "output_item_id", "") or "")) if r is not None else None
                if (r is None or out is None
                        or getattr(out, "rarity", "") not in nle._BASIC_RARITIES
                        or max(1, int(getattr(r, "output_qty", 1) or 1)) != 1):
                    o["state"] = "stalled"
                    continue
                if out.id in _auc:
                    continue                     # 拍品隔离期间暂停，不能借订单绕过隔离
                need: dict = {}
                for iid in (r.inputs or []):
                    need[iid] = need.get(iid, 0) + 1
                have: dict = {}
                for iid in wh:
                    have[iid] = have.get(iid, 0) + 1
                if any(have.get(i, 0) < n for i, n in need.items()):
                    if o.get("state") != "stalled":
                        o["stall_days"] = int(o.get("stall_days", 0) or 0) + 1
                        if o["stall_days"] >= _ORDER_STALL_DAYS:
                            o["state"] = "stalled"
                            stall_flag = True    # 每日至多一条断料事件（防刷屏）
                    continue
                if o.get("state") == "stalled":
                    o["state"] = "open"
                    o["stall_days"] = 0
                    parts.append(f"{o.get('out_name', '产线')}补料复工")
                to_wh = str(o.get("ship", "")) == "warehouse"
                to_home = str(o.get("ship", "")) == "home"
                home = _order_home(world, domain) if to_home else None
                if to_wh and len(wh) - sum(need.values()) + 1 > cap:
                    continue                     # 扣料后仍仓满：原料不动
                if to_home:
                    from src.services import home_engine as he
                    if home is None or len(home.stash) >= he.stash_capacity(home):
                        continue                 # 家中仓满/已拆：原料不动，等待处理
                for i, n in need.items():
                    for _ in range(n):
                        wh.remove(i)
                made = int(o.get("made", 0) or 0) + 1
                o["made"] = made
                o["stall_days"] = 0
                made_today += 1
                if to_wh:
                    wh.append(out.id)
                elif to_home:
                    home.stash.append(out.id)
                else:
                    price = max(1, int(tre.compute_item_price(out)))
                    shop.stock.append(ShopStockEntry(
                        item_id=out.id, price=price, stock=1, max_stock=1, tag="npc_made"))
                    shop.touch()
                if made >= max(1, int(o.get("qty", 1))):
                    o["state"] = "done"
                    done_txt.append(f"{getattr(out, 'name', out.id)} x{made} 已"
                                    + ("送到住宅" if to_home else ("入仓" if to_wh else "上架")))
                else:
                    crafted.append(f"{getattr(out, 'name', out.id)}（{made}/{o.get('qty', 1)}）")
            # 只在完全无待办订单时扫配方池，避免订单料被默认产线抢走。
            if not open_orders:
                for r in (getattr(world, "recipes", None) or []):
                    if made_today >= _WH_CRAFT_PER_DAY:
                        break
                    if (getattr(r, "required_building", "")
                            or max(1, int(getattr(r, "output_qty", 1) or 1)) != 1):
                        continue
                    out = item_by_id.get(str(getattr(r, "output_item_id", "") or ""))
                    if out is None or out.rarity not in nle._BASIC_RARITIES:
                        continue
                    if out.id in _auc:
                        continue
                    if any(e.item_id == out.id for e in (shop.stock or [])):
                        continue
                    need: dict = {}
                    for iid in (r.inputs or []):
                        need[iid] = need.get(iid, 0) + 1
                    have: dict = {}
                    for iid in wh:
                        have[iid] = have.get(iid, 0) + 1
                    if any(have.get(i, 0) < n for i, n in need.items()):
                        continue
                    for i, n in need.items():
                        for _ in range(n):
                            wh.remove(i)
                    price = max(1, int(tre.compute_item_price(out)))
                    shop.stock.append(ShopStockEntry(
                        item_id=out.id, price=price, stock=1, max_stock=1, tag="npc_made"))
                    shop.touch()
                    crafted.append(out.name)
                    made_today += 1
            if done_txt:
                parts.append("；".join(done_txt))
            if stall_flag:
                parts.append("产线停工待料（订单缺料，去补货或撤单）")
            if crafted:
                parts.append(f"掌柜{keeper.name}以仓中材料制得 {'、'.join(crafted)}"
                             + ("上架售卖" if not open_orders else ""))

    if parts:
        evts.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0),
            category="npc", severity="minor", title="据点仓储",
            desc=f"「{domain.name}」{'；'.join(parts)}。",
            locations=[loc.id],
        ))
    return evts


def domain_at(world: World, loc_id: str) -> Optional[PlayerDomain]:
    """取该地点的据点实体（每地点至多一处）。"""
    for dm in world.domains:
        if dm.location_id == loc_id:
            return dm
    return None


def player_domain_at(world: World, loc) -> Optional[PlayerDomain]:
    """「在据点」判定：当前地点是玩家据点（loc 为 None 返回 None）。"""
    if loc is None or not getattr(loc, "player_owned", False):
        return None
    return domain_at(world, loc.id)


def fallback_domain_name(world: World, loc) -> str:
    """LLM 起名失败/不可用时的题材据点名兜底（SeededRng 确定性：同 world+tick+loc 同名）。"""
    rng = SeededRng.seed_from(world.id, int(getattr(world, "tick_count", 0) or 0),
                              f"domain_name_{getattr(loc, 'id', '')}")
    pool = genre_pool(world).get("names") or ["无名据点"]
    return rng.pick(pool) or "无名据点"


def _pick_place_name(world: World, loc, key: str, salt: str) -> str:
    """初始场所名确定性挑选（同 world+tick+loc+key 同名）。"""
    rng = SeededRng.seed_from(world.id, int(getattr(world, "tick_count", 0) or 0),
                              f"domain_{salt}_{getattr(loc, 'id', '')}")
    pool = genre_pool(world).get(key) or []
    return rng.pick(pool) if pool else ""


def buy_domain(world: World, loc, name: str = "", desc: str = "") -> tuple[bool, str]:
    """购地开辟据点（纯 Python）：校验野外/未购/金币 -> 扣费 -> 地点改造（kind 翻转/
    定档 village/清势力归属/改名）+ 铺 2 初始场所 + 建 PlayerDomain + 世界事件。

    返回 (ok, err)；成功 err 为空串。据点名空串时题材池兜底（确定性）。
    """
    # [!] 已购检查先于 kind 检查：购后 kind 已翻 settlement，先查 kind 会把重复购
    # 误报成「不是野外之地」（玩家视角困惑）。
    if getattr(loc, "player_owned", False) or domain_at(world, loc.id) is not None:
        return False, f"「{getattr(loc, 'name', '此地')}」已是你的据点"
    if getattr(loc, "kind", "") != "wilderness":
        return False, f"「{getattr(loc, 'name', '此地')}」不是野外之地，无法开辟据点"
    price = domain_price(loc)
    p = world.player
    if int(p.gold) < price:
        return False, f"{GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {}).currency}不足"
    p.gold = int(p.gold) - price
    old_name = loc.name
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})

    # ---- 地点改造 ----
    nm = (name or "").strip() or fallback_domain_name(world, loc)
    ds = (desc or "").strip()
    loc.player_owned = True
    loc.kind = "settlement"
    loc.settlement_size = "village"
    # 清空势力归属：无主私有地（原控制势力视作已售地退场；势力侵袭/防务是 D4）。
    loc.faction_id = ""
    loc.owner_faction_id = ""
    loc.name = nm
    if ds:
        loc.desc = ds

    # ---- 铺设初始 2 场所（营门 <-> 营地广场；营门接原默认场所，守 P27 双向连接）----
    pool = genre_pool(world)
    old_default = getattr(loc, "default_place_id", "") or ""
    gate = Place(
        name=_pick_place_name(world, loc, "gate", "gate") or "营门",
        desc=f"「{nm}」的门户，往来的必经之处。",
        type=pool.get("gate_type") or "大门",
    )
    yard = Place(
        name=_pick_place_name(world, loc, "yard", "yard") or "营地广场",
        desc=f"「{nm}」的中心空地，待建设的基业之地。",
        type=pool.get("yard_type") or "广场",
    )
    gate.connections = [yard.id]
    yard.connections = [gate.id]
    if old_default:
        gate.connections.append(old_default)
        for pl in loc.places:
            if isinstance(pl, Place) and pl.id == old_default and gate.id not in pl.connections:
                pl.connections.append(gate.id)
    loc.places = list(loc.places or []) + [gate, yard]
    loc.default_place_id = gate.id

    # ---- 据点实体 + 世界事件（major：建城大事，进坊间热议/编年史）----
    domain = PlayerDomain(
        name=nm, desc=ds, location_id=loc.id,
        purchased_day=max(1, int(getattr(world, "day_count", 1) or 1)),
    )
    world.domains.append(domain)
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="event", severity="major",
        title="开辟据点",
        desc=(f"玩家豪掷 {price} {gt.currency}购下「{old_name}」，辟为私人据点「{nm}」，"
              f"营门与{yard.name}初立，此地成为一方新兴聚落。"),
        locations=[loc.id],
    ))
    return True, ""


# ---------------------------------------------------------------------------
# 资金池存取（D1：金币 <-> 据点资金池；D5 客流收入/工资支出同走 funds 字段）
# ---------------------------------------------------------------------------
def deposit_funds(world: World, domain: PlayerDomain, amount: int) -> tuple[bool, str]:
    """存入资金池：校验正数/玩家金币足够 -> 真实扣款入池。"""
    amt = int(amount or 0)
    if amt <= 0:
        return False, "存入金额须为正数"
    p = world.player
    if int(p.gold) < amt:
        return False, f"{GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {}).currency}不足"
    p.gold = int(p.gold) - amt
    domain.funds = int(domain.funds) + amt
    return True, ""


def withdraw_funds(world: World, domain: PlayerDomain, amount: int) -> tuple[bool, str]:
    """取出资金池：校验正数/池内足够 -> 出池入玩家金币。"""
    amt = int(amount or 0)
    if amt <= 0:
        return False, "取出金额须为正数"
    if int(domain.funds) < amt:
        return False, "资金池余额不足"
    domain.funds = int(domain.funds) - amt
    world.player.gold = int(world.player.gold) + amt
    return True, ""


# ===========================================================================
# [D2 2026-08-29] 建设与职员（设施建造 / 招募新 NPC / 挖角 / 工资日结算）
# ===========================================================================
def facility_catalog(world: World) -> list[dict]:
    """题材设施目录（建造 UI 数据源；每 kind 每据点限一座）。"""
    return [dict(f) for f in (genre_pool(world).get("facilities") or [])]


def facility_of(domain: PlayerDomain, kind: str) -> Optional[dict]:
    """取据点已建的某类设施（每类限一座）。"""
    for f in (domain.facilities or []):
        if isinstance(f, dict) and f.get("kind") == kind:
            return f
    return None


def facility_price(world: World, entry: dict) -> int:
    """设施建造价 = 池基价 x economy_pace 买入倍率（从据点资金池扣）。"""
    try:
        raw = int((entry or {}).get("base_price", 0) or 0)
    except (TypeError, ValueError):
        raw = 0
    return max(1, int(round(raw * pace_factors(world_pace(world))[0])))


def _consume_build_materials(world: World) -> tuple[bool, str]:
    """按类别扣背包建造材料（先校验后扣，失败零副作用；去重背包按件数点）。

    [据点仓库 2026-09-07] 豁免农田口粮（seed_/crop_ 前缀）：农夫收的作物 effective
    口径是 craft，不豁免会被建造当建材吃掉，击穿「伙计采建造料/农夫收作物」产线隔离。
    """
    inv = getattr(world.player, "inventory", None)
    if not isinstance(inv, list):
        return False, "背包不可用"
    item_by_id = {getattr(it, "id", ""): it for it in (world.items or [])}

    def _build_ok(iid: str) -> bool:
        it = item_by_id.get(iid)
        return it is not None and not str(getattr(it, "id", "")).startswith(("seed_", "crop_"))

    for cat, cnt in _BUILD_MATS:
        have = [iid for iid in inv
                if _build_ok(iid) and effective_category(item_by_id.get(iid)) == cat]
        if len(have) < cnt:
            zh = {"forge": "锻造类", "craft": "制造类"}[cat]
            return False, f"材料不足：需 {cnt} 件{zh}材料（背包仅 {len(have)} 件）"
    for cat, cnt in _BUILD_MATS:
        for _ in range(cnt):
            hit = next((iid for iid in inv
                        if _build_ok(iid)
                        and effective_category(item_by_id.get(iid)) == cat), None)
            if hit is not None:
                inv.remove(hit)
    return True, ""


def fallback_facility_name(world: World, loc, kind: str) -> str:
    """LLM 起名失败时的题材设施名兜底（SeededRng 确定性）。"""
    rng = SeededRng.seed_from(world.id, int(getattr(world, "tick_count", 0) or 0),
                              f"domain_fac_{kind}_{getattr(loc, 'id', '')}")
    entry = next((f for f in facility_catalog(world) if f.get("kind") == kind), None)
    pool = (entry or {}).get("names") or ["新设施"]
    return rng.pick(pool) or "新设施"


def _domain_loc(world: World, domain: PlayerDomain):
    """据点地点实体（不存在/已非私有返回 None）。"""
    loc = next((l for l in world.locations if l.id == domain.location_id), None)
    if loc is None or not getattr(loc, "player_owned", False):
        return None
    return loc


def _hub_place_of(world: World, loc) -> Optional[Place]:
    """中枢广场场所：按题材 yard_type 匹配；找不到回退 default_place_id（营门）。"""
    yard_type = str(genre_pool(world).get("yard_type") or "广场")
    gate_id = getattr(loc, "default_place_id", "") or ""
    for pl in (getattr(loc, "places", None) or []):
        if isinstance(pl, Place) and pl.id != gate_id and str(pl.type or "") == yard_type:
            return pl
    for pl in (getattr(loc, "places", None) or []):
        if isinstance(pl, Place) and pl.id == gate_id:
            return pl
    return None


def build_facility(world: World, domain: PlayerDomain, kind: str,
                   name: str = "", desc: str = "") -> tuple[bool, str]:
    """建设施（纯 Python）：校验类型/重复/资金/材料 -> 扣费扣料 -> 铺真 Place（连中枢广场，
    双向）+ shop 类同步建真 Shop（merchant 留空待掌柜入职）+ 记 domain.facilities + 事件。

    名空串时题材池兜底（确定性）。资金从据点资金池扣；材料从玩家背包按类别扣。
    """
    if kind not in _DOMAIN_FACILITY_KIND_VALUES:   # 单一来源（模型层白名单）
        return False, "未知设施类型"
    loc = _domain_loc(world, domain)
    if loc is None:
        return False, "据点地点已不存在"
    if facility_of(domain, kind) is not None:
        return False, f"「{domain.name}」已建该类设施（每类限一座）"
    entry = next((f for f in facility_catalog(world) if f.get("kind") == kind), None)
    if entry is None:
        return False, "该题材暂无此类设施目录"
    price = facility_price(world, entry)
    if int(domain.funds) < price:
        return False, "资金池余额不足"
    ok, err = _consume_build_materials(world)
    if not ok:
        return False, err
    domain.funds = int(domain.funds) - price
    nm = (name or "").strip() or fallback_facility_name(world, loc, kind)
    ds = (desc or "").strip() or str(entry.get("desc") or "")
    place = Place(name=nm, desc=ds, type=str(entry.get("type") or kind))
    hub = _hub_place_of(world, loc)
    if hub is not None:
        place.connections = [hub.id]
        if place.id not in hub.connections:
            hub.connections.append(place.id)
    loc.places = list(loc.places or []) + [place]
    domain.facilities = list(domain.facilities or []) + [{
        "place_id": place.id, "kind": kind, "name": nm,
        "day": max(1, int(getattr(world, "day_count", 1) or 1)),
    }]
    if kind == "shop":
        shop = Shop(name=nm, location_id=loc.id, shop_type="general",
                    merchant_npc_id="")
        shop.touch()
        world.shops.append(shop)
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="event", severity="minor",
        title="据点建设",
        desc=f"「{domain.name}」新落成一座{entry.get('type', '')}「{nm}」，耗资 {price} {gt.currency}。",
        locations=[loc.id],
    ))
    return True, ""


# ---- 职员招募 / 挖角 ----
def roster_npc(world: World, npc_id: str) -> Optional[NPC]:
    return next((n for n in world.npcs if n.id == npc_id), None)


def _pick_specialty(world: World, npc_id: str, role: str) -> str:
    """[P61 专精] 入职确定性赋予专精（盐 = world.id + tick + npc.id；同角色两选一）。"""
    pool = _STAFF_SPECIALTIES.get(str(role or ""))
    if not pool:
        return ""
    rng = SeededRng.seed_from(str(getattr(world, "id", "")),
                              int(getattr(world, "tick_count", 0) or 0),
                              f"staff_spec_{str(npc_id or '')}")
    return str(rng.pick(pool))


def _staff_specialty(domain: PlayerDomain, role: str) -> str:
    """[P61 专精] 读某角色职员的专精（roster 条目 specialty 字段；空 = 未赋予/旧档）。"""
    for r in roster_of(domain):
        if str(r.get("role") or "") == str(role):
            return str(r.get("specialty", "") or "")
    return ""


def set_policy(domain: PlayerDomain, policy: str) -> tuple[bool, str]:
    """[P61 定策] 设定据点经营策略（一键一策：增产/武备/重质/无为，白名单校验）。"""
    p = str(policy or "")
    if p not in _POLICY_VALUES:
        return False, "没有这种策略"
    domain.policy = p
    return True, ""


def _staff_vignettes(world: World, domain: PlayerDomain, loc, eff_day: int) -> list:
    """[P61 小人剧] 员工每日小事件（纯引擎确定性，零 LLM；进右侧动态栏）。

    每职员 15%/日 roll（独立盐 vignette_{day}_{domain.id}，不消费生产/训练的 rng 序列），
    每据点每日至多 2 条。纯风味无机械效果（与「带话/场间闲谈」同类先例）。"""
    rng = SeededRng.seed_from(str(getattr(world, "id", "")), int(eff_day or 1),
                              f"vignette_{str(getattr(domain, 'id', ''))}")
    ov = getattr(world, "config_overlay", None) or {}
    tid = str(ov.get("attribute_template_id", "western_fantasy")
              if isinstance(ov, dict) else "western_fantasy") or "western_fantasy"
    evts = []
    for r in roster_of(domain):
        if len(evts) >= _STAFF_VIGNETTE_CAP:
            break
        npc = roster_npc(world, str(r.get("npc_id") or ""))
        if npc is None or not getattr(npc, "alive", True):
            continue
        role = str(r.get("role") or "")
        pool = ((_GENRE_STAFF_VIGNETTES.get(tid) or {}).get(role)
                or _GENRE_STAFF_VIGNETTES["western_fantasy"].get(role))
        if not pool or rng.random() >= _STAFF_VIGNETTE_CHANCE:
            continue
        text = str(rng.pick(pool)).format(name=str(getattr(npc, "name", "?")),
                                          domain=str(getattr(domain, "name", "")))
        evts.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0), category="npc",
            severity="trivial", title=f"{ROLE_LABELS.get(role, '职员')}·{npc.name}",
            desc=text, npcs=[npc.id], locations=[getattr(loc, "id", "")]))
    return evts


def roster_of(domain: PlayerDomain) -> list[dict]:
    return [r for r in (domain.roster or []) if isinstance(r, dict)]


def roster_full(world: World, domain: PlayerDomain) -> bool:
    loc = _domain_loc(world, domain)
    limit = resident_limit(getattr(loc, "settlement_size", "") or "village")
    return len(roster_of(domain)) >= limit


def role_available(world: World, domain: PlayerDomain, role_key: str) -> tuple[bool, str]:
    """职业入职前置校验：白名单 / 依赖设施已建 / 掌柜唯一（绑定 shop 商人位）/ 名册未满。"""
    if role_key not in ROLE_LABELS:
        return False, "未知职业"
    need = ROLE_FACILITY.get(role_key) or ""
    if need and facility_of(domain, need) is None:
        zh = {"shop": "商铺", "guard": "岗哨", "farm": "农田"}.get(need, need)
        return False, f"须先在据点建{zh}（{ROLE_LABELS[role_key]}的任职场所）"
    if role_key == "shopkeeper":
        if any(r.get("role") == "shopkeeper" for r in roster_of(domain)):
            return False, "商铺已有一位掌柜"
    if roster_full(world, domain):
        loc = _domain_loc(world, domain)
        limit = resident_limit(getattr(loc, "settlement_size", "") or "village")
        return False, f"名册已满（居民上限 {limit}，升聚落档可扩容）"
    return True, ""


def _workplace_place(world: World, domain: PlayerDomain, role_key: str) -> Optional[Place]:
    """职业的任职场所（shop 职业在商铺场所；其余在各自设施场所）。"""
    need = ROLE_FACILITY.get(role_key) or ""
    fac = facility_of(domain, need) if need else None
    if fac is None:
        return None
    loc = _domain_loc(world, domain)
    if loc is None:
        return None
    return next((p for p in (loc.places or []) if isinstance(p, Place)
                 and p.id == fac.get("place_id")), None)


def _bind_npc_at(npc: NPC, loc, place: Optional[Place]) -> None:
    """把 NPC 落到地点/场所的 npc_ids 双层登记（去重）。"""
    if loc is not None and npc.id not in (loc.npc_ids or []):
        loc.npc_ids.append(npc.id)
    if place is not None and npc.id not in (place.npc_ids or []):
        place.npc_ids.append(npc.id)


def _unbind_npc_at(npc: NPC, loc, place: Optional[Place]) -> None:
    """对称解除地点/场所登记（离职/挖角离场用）。"""
    if loc is not None and npc.id in (loc.npc_ids or []):
        loc.npc_ids.remove(npc.id)
    if place is not None and npc.id in (place.npc_ids or []):
        place.npc_ids.remove(npc.id)


def _release_shop_binding(world: World, npc: NPC) -> None:
    """解除 NPC 的商人绑定（双向清空；原店停摆待新掌柜/新人接手，P46 口径）。"""
    if not getattr(npc, "shop_id", ""):
        return
    shop = next((s for s in world.shops if s.id == npc.shop_id), None)
    if shop is not None and shop.merchant_npc_id == npc.id:
        shop.merchant_npc_id = ""
        shop.touch()
    npc.is_merchant = False
    npc.shop_id = ""
    npc.shop_type = ""


def hire_staff(world: World, domain: PlayerDomain, role_key: str,
               name: str = "") -> tuple[Optional[NPC], str]:
    """招募新职员（纯 Python 骨架；语义档案由服务层 LLM 批补，失败保留骨架）。

    骨架：姓名库占位名（svc 传入；空串用「{据点名}新面孔」兜底）+ 平民基线（打手按
    玩家等级给战力基线）+ 常驻据点（home/location = 据点，place = 任职场所）->
    P36a 生活模拟自动接管（商人走 stock_shop 上架自家店）。
    """
    ok, err = role_available(world, domain, role_key)
    if not ok:
        return None, err
    loc = _domain_loc(world, domain)
    if loc is None:
        return None, "据点地点已不存在"
    rng = SeededRng.seed_from(world.id, int(getattr(world, "tick_count", 0) or 0),
                              f"domain_hire_{role_key}_{domain.id}_{len(roster_of(domain))}")
    nm = (name or "").strip() or f"{domain.name}新面孔"
    place = _workplace_place(world, domain, role_key)
    label = ROLE_LABELS[role_key]
    npc = NPC(
        name=nm, role=label, desc=f"受雇于「{domain.name}」的{label}。",
        location_id=loc.id, place_id=(place.id if place is not None else ""),
        home_location_id=loc.id, alive=True, hostile=False,
        wallet=rng.roll(50, 150),
    )
    if role_key == "guard":
        # 打手战力基线：随玩家等级成长（D4 防务跟得上），四维 = 8 + 等级//3。
        # combat_role="friendly"（P44：战斗身份唯一标注来源，助战/快防候选靠它）。
        lvl = max(_GUARD_MIN_LEVEL, min(100, int(getattr(world.player, "level", 1) or 1)))
        npc.level = lvl
        npc.combat_role = "friendly"
        base = 8 + lvl // 3
        npc.stat_str, npc.stat_dex = base, base
        npc.stat_vit, npc.stat_int = base, max(5, base - 2)
        npc.hp_max = ce.max_hp_for(npc)
        npc.hp = npc.hp_max
    else:
        npc.combat_role = "none"
        nle.ensure_life_baseline(npc)
    if role_key == "shopkeeper":
        fac = facility_of(domain, "shop")
        shop = next((s for s in world.shops
                     if s.location_id == loc.id and s.shop_type == "general"
                     and not s.merchant_npc_id), None)
        if shop is not None:
            npc.is_merchant = True
            npc.shop_id = shop.id
            npc.shop_type = "general"
            shop.merchant_npc_id = npc.id
            shop.touch()
            if fac is not None and shop.name != fac.get("name"):
                shop.name = str(fac.get("name") or shop.name)
    world.npcs.append(npc)
    _bind_npc_at(npc, loc, place)
    domain.roster = list(domain.roster or []) + [{
        "npc_id": npc.id, "role": role_key,
        "hired_day": max(1, int(getattr(world, "day_count", 1) or 1)),
        # [P61 专精] 入职确定性赋予专精（同角色两选一；机械加成见 _STAFF_SPECIALTIES）
        "specialty": _pick_specialty(world, npc.id, role_key),
    }]
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="npc", severity="minor",
        title=f"新面孔：{npc.name}",
        desc=f"{npc.name} 入职「{domain.name}」成为{label}。",
        npcs=[npc.id], locations=[loc.id],
    ))
    return npc, ""


def poach_npc(world: World, domain: PlayerDomain, npc: NPC,
              role_key: str) -> tuple[bool, str]:
    """挖角已有 NPC（纯 Python）：交情 >=60 + 签约金 200x等级（资金池扣）+ 职业前置同招募。

    挖角商人同步解除原店绑定（原聚落商店停摆待接手，P46 口径）；NPC 常驻/工作场所
    迁到据点；交情保持不动（挖来的是朋友）。
    """
    if npc is None or not getattr(npc, "alive", False):
        return False, "该 NPC 已不在人世"
    if getattr(npc, "hostile", False):
        return False, "敌对之人无法雇佣"
    if any(r.get("npc_id") == npc.id for d in world.domains for r in roster_of(d)):
        return False, "该 NPC 已在据点名册中"
    if int(getattr(npc, "affinity", 0) or 0) < 60:
        return False, "交情不足（须达到朋友，60+）"
    ok, err = role_available(world, domain, role_key)
    if not ok:
        return False, err
    loc = _domain_loc(world, domain)
    if loc is None:
        return False, "据点地点已不存在"
    fee = _POACH_FEE_PER_LEVEL * max(1, int(getattr(npc, "level", 1) or 1))
    if int(domain.funds) < fee:
        gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
        return False, f"资金池余额不足（签约金 {fee} {gt.currency}）"
    domain.funds = int(domain.funds) - fee
    # 迁出原地：旧地点/场所解绑 + 商人绑定解除（原店停摆）
    old_loc = next((l for l in world.locations if l.id == npc.location_id), None)
    old_place = None
    if old_loc is not None:
        old_place = next((p for p in (old_loc.places or [])
                          if isinstance(p, Place) and p.id == getattr(npc, "place_id", "")), None)
    _unbind_npc_at(npc, old_loc, old_place)
    _release_shop_binding(world, npc)
    # 落户据点
    place = _workplace_place(world, domain, role_key)
    npc.location_id = loc.id
    npc.place_id = place.id if place is not None else ""
    npc.home_location_id = loc.id
    npc.role = ROLE_LABELS[role_key]
    nle.ensure_life_baseline(npc)
    _bind_npc_at(npc, loc, place)
    if role_key == "shopkeeper":
        shop = next((s for s in world.shops
                     if s.location_id == loc.id and s.shop_type == "general"
                     and not s.merchant_npc_id), None)
        if shop is not None:
            npc.is_merchant = True
            npc.shop_id = shop.id
            npc.shop_type = "general"
            shop.merchant_npc_id = npc.id
            shop.touch()
    domain.roster = list(domain.roster or []) + [{
        "npc_id": npc.id, "role": role_key,
        "hired_day": max(1, int(getattr(world, "day_count", 1) or 1)),
        # [P61 专精] 入职确定性赋予专精（同角色两选一；机械加成见 _STAFF_SPECIALTIES）
        "specialty": _pick_specialty(world, npc.id, role_key),
    }]
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="npc", severity="minor",
        title=f"新面孔：{npc.name}",
        desc=f"{npc.name} 收下 {fee} {gt.currency}签约金，入伙「{domain.name}」担任{ROLE_LABELS[role_key]}。",
        npcs=[npc.id], locations=[loc.id],
    ))
    return True, ""


# ---- 工资日结算（tick domain 子阶段；per-day 水位 World.last_domain_day）----
def daily_wage_of(npc: NPC) -> int:
    """日工资 = 2 x 等级（等级随 P36a 生活成长，工资水涨船高）。"""
    return _WAGE_PER_LEVEL * max(1, int(getattr(npc, "level", 1) or 1))


def _staff_leave(world: World, domain: PlayerDomain, reason: str) -> list[WorldEvent]:
    """全员离职：名册清空；NPC 迁去别的聚落（确定性挑选；无别处可去时留在原地但不领薪）；
    掌柜解除店绑定（据点商铺停摆待新掌柜）。返回事件列表。"""
    loc = _domain_loc(world, domain)
    evts: list[WorldEvent] = []
    others = [l for l in world.locations
              if getattr(l, "kind", "") == "settlement"
              and not getattr(l, "player_owned", False)]
    names: list[str] = []
    for r in roster_of(domain):
        npc = roster_npc(world, str(r.get("npc_id") or ""))
        if npc is None:
            continue
        _release_shop_binding(world, npc)
        dest = None
        if others:
            rng = SeededRng.seed_from(world.id, int(getattr(world, "day_count", 1) or 1),
                                      f"domain_leave_{npc.id}")
            dest = rng.pick(others)
        if dest is not None:
            old_place = None
            if loc is not None:
                old_place = next((p for p in (loc.places or [])
                                  if isinstance(p, Place) and p.id == npc.place_id), None)
            _unbind_npc_at(npc, loc, old_place)
            npc.location_id = dest.id
            npc.place_id = getattr(dest, "default_place_id", "") or ""
            npc.home_location_id = dest.id
            # [!] 双层登记（审查修复）：只挂 loc.npc_ids 会漏场所层在场列表，
            # 与 _bind_npc_at/_move_npc 的双层语义不对称（下次移动自愈但当场漏人）。
            dest_place = next((p for p in (dest.places or [])
                               if isinstance(p, Place) and p.id == npc.place_id), None)
            _bind_npc_at(npc, dest, dest_place)
        npc.role = "平民"
        names.append(npc.name)
    domain.roster = []
    domain.unpaid_days = 0
    if names and loc is not None:
        evts.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0), category="npc", severity="minor",
            title="据点职员出走",
            desc=f"「{domain.name}」{reason}，{'、'.join(names[:6])}收拾行装离开了据点。",
            locations=[loc.id],
        ))
    return evts


def daily_settle(world: World, preset=None) -> list[WorldEvent]:
    """据点日结算（纯 Python，tick domain 子阶段）：结算日按 day_count 水位推进
    （跳天追补上限 30 天，仿 farm 口径防异常档跳爆）。

    [D5] 每日经济回路：抽象客流收入（客流 = 基数x档位x(1+繁荣%)x声望x竞争 -> 收入入池）
    + 具名来访消费（相邻地点 NPC roll 到访，真实扣 wallet / 货架 -1 / 店入账资金池）
    -> 工资 = sum(2x等级) 出池入各 NPC.wallet -> 设施维护费出池 -> 账单存 last_bill
    （净额 >=0 繁荣度 +1 否则 -1，钳 0-100）。职员侧：清阵亡职员；发不出 unpaid_days+1
    （首次发 minor 事件进右侧 NPC 动态栏），付清清零；连欠薪 >=3 天全员离职
    （迁去别的聚落 + 店铺停摆）。
    """
    evts: list[WorldEvent] = []
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    last = int(getattr(world, "last_domain_day", 0) or 0)
    if day <= last:
        return evts
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    for i in range(min(day - last, 30)):        # 每个漏结日逐日结算
        eff_day = last + i + 1                  # 结算日（rng/事件锚定用，追补日确定性）
        for domain in list(world.domains):
            loc = _domain_loc(world, domain)
            if loc is None:
                continue
            rng = SeededRng.seed_from(world.id, eff_day, f"domain_econ_{domain.id}")
            # ---- [D5] 抽象客流收入（无职员也结算：据点本身在营业）----
            flow = visitor_flow(world, domain, loc, eff_day)
            income = _abstract_income(world, domain, loc, flow)
            # [P61 专精定策] 掌柜专精（账目清明·进退有度）收入 +5%；「重质」策略 +10%
            # （乘在入池源头——funds 入账与账单读数同源，勿在账单处二次乘）
            income = int(income * (1.0 + (0.05 if _staff_specialty(domain, "shopkeeper") else 0.0)
                                   + (0.10 if getattr(domain, "policy", "") == "quality" else 0.0)))
            domain.funds = int(domain.funds) + income
            domain.visitors_total = int(domain.visitors_total) + flow
            domain.income_total = int(domain.income_total) + income
            # ---- [D5] 具名来访消费（相邻地点 NPC roll；掌柜在职且有货才成交）。
            # [!] _visitor_purchases 内部已逐笔入账资金池（审查修复：此处再加一次
            # buy_income 会双入账印钱）；这里只累计营业统计与事件。----
            buy_evts, buy_income = _visitor_purchases(world, domain, loc, rng)
            domain.income_total = int(domain.income_total) + buy_income
            evts.extend(buy_evts)
            # ---- [D6] 委托板日推进（承接/交付/过期退款——退款先进池可付当日工资）----
            evts.extend(_commission_phase(world, domain, loc, rng, eff_day))
            # ---- [D5] 到访传闻（有客流才 roll，每日至多 1 条防刷屏）----
            if flow > 0 and rng.random() < 0.35:
                pool = genre_pool(world).get("events_visit") or []
                if pool:
                    tpl = rng.pick(pool)
                    evts.append(WorldEvent(
                        tick=int(getattr(world, "tick_count", 0) or 0),
                        category="npc", severity="trivial",
                        title="据点来客",
                        desc=str(tpl).format(domain=domain.name),
                        locations=[loc.id],
                    ))
            # ---- [D2] 阵亡职员静默出册（复活契约归 cleanup；据点不养ghost）。----
            # [!] 出册须同步解掌柜的店绑定（审查修复）：否则复活者（permadeath 关）或
            # P46 接班人占着 merchant_npc_id，再招掌柜永远找不到空位店可绑。
            dead = [r for r in roster_of(domain)
                    if (roster_npc(world, str(r.get("npc_id") or "")) or NPC(alive=False)).alive is False]
            if dead:
                for r in dead:
                    dead_npc = roster_npc(world, str(r.get("npc_id") or ""))
                    if dead_npc is not None:
                        _release_shop_binding(world, dead_npc)
                domain.roster = [r for r in roster_of(domain) if r not in dead]
            staff = [(r, roster_npc(world, str(r.get("npc_id") or "")))
                     for r in roster_of(domain)]
            staff = [(r, n) for r, n in staff if n is not None]
            # ---- [D2] 工资（收入已先进池：当日营收可救当日工资）----
            wages_paid = 0
            wages_due = 0
            if not staff:
                domain.unpaid_days = 0
            else:
                wages = sum(daily_wage_of(n) for _, n in staff)
                wages_due = wages
                if int(domain.funds) >= wages:
                    domain.funds = int(domain.funds) - wages
                    wages_paid = wages
                    for _, n in staff:
                        n.wallet = int(getattr(n, "wallet", 0) or 0) + daily_wage_of(n)
                    if int(domain.unpaid_days) > 0:
                        domain.unpaid_days = 0
                else:
                    domain.unpaid_days = int(domain.unpaid_days) + 1
                    if int(domain.unpaid_days) == 1:
                        evts.append(WorldEvent(
                            tick=int(getattr(world, "tick_count", 0) or 0),
                            category="npc", severity="minor", title="据点欠薪",
                            desc=(f"「{domain.name}」资金池发不出今日工资（需 {wages} "
                                  f"{gt.currency}），职员们面露不豫。"),
                            locations=[loc.id],
                        ))
                    if int(domain.unpaid_days) >= _UNPAID_LEAVE_DAYS:
                        evts.extend(_staff_leave(
                            world, domain, f"连欠薪 {_UNPAID_LEAVE_DAYS} 天"))
            # ---- [D5] 设施维护费（付得起多少扣多少；不触发欠薪逻辑）----
            maint_due = daily_maintenance(domain)
            maint_paid = min(int(domain.funds), maint_due)
            domain.funds = int(domain.funds) - maint_paid
            # ---- [D5] 账单 + 繁荣度（净额驱动涨落，钳 0-100）----
            # [!] 专精/策略的收入乘区已在入池源头（income 处）生效，账单随源头一致
            total_income = income + buy_income
            net = total_income - wages_paid - maint_paid
            domain.last_bill = {
                "day": eff_day, "flow": flow, "income": total_income,
                "wages": wages_paid, "maintenance": maint_paid, "net": net,
            }
            # [P48] 近 7 日账单快照（DomainDialog 走势读数；最新在前滚动 6 条历史）
            domain.bill_history = ([dict(domain.last_bill)]
                                   + [b for b in (domain.bill_history or [])
                                      if isinstance(b, dict)][:6])
            # [!] 繁荣度按「应付口径」净额（审查修复）：欠薪日按实付算净额偏正，
            # 赖账反而涨繁荣是反向激励——应付工资/维护没结清就掉繁荣。
            net_due = total_income - wages_due - maint_due
            domain.prosperity = max(0, min(100, int(domain.prosperity)
                                           + (1 if net_due >= 0 else -1)))
            # ---- [D4] 防务练级：打手历练 -> 闹事弹压 -> 势力侵袭快防（全确定性）。
            # 放账单繁荣度之后：闹事/侵袭的繁荣度增减压在当日经营涨落之上。----
            evts.extend(_train_guards(world, domain, rng))
            evts.extend(_trouble_check(world, domain, rng, flow))
            evts.extend(_raid_check(world, domain, rng))
            # ---- [D7] 每日随机遭遇：敌袭自动战（打手+哨卫 vs 题材怪，无玩家参与）。
            # 挂在侵袭之后：rng 消费在日结算序列尾部，train/闹事/侵袭确定性不漂移。----
            evts.extend(_siege_check(world, domain, loc, rng, preset))
            # ---- [据点仓库 2026-09-07] 日产出尾挂（独立 salt rng，不消费上方序列）：
            # 伙计采料/农夫收作入仓 + 掌柜取料合成上架——放最后保既有日结算确定性。----
            evts.extend(_tick_warehouse(world, domain, loc, eff_day))
            # ---- [P61 小人剧] 员工每日小事件（独立 salt，纯风味；进右侧动态栏）----
            evts.extend(_staff_vignettes(world, domain, loc, eff_day))
    world.last_domain_day = day
    return evts


# ---- [D5] 客流经济（公式全纯 Python 确定性；具名来访走相邻地点真实 NPC）----
def rep_factor(world: World) -> float:
    """声望因子：玩家对各势力声望均值（-50..100 钳制）映射 0.75..1.5；无势力 1.0。

    名声好 -> 客人放心上门；声名狼藉 -> 路人绕道（但不会归零——黑店也有客）。
    """
    raw = getattr(getattr(world, "player", None), "reputation", None) or {}
    reps = []
    for v in (raw.values() if isinstance(raw, dict) else []):
        try:
            reps.append(int(v))
        except (TypeError, ValueError):
            continue                     # 脏值跳过（_load_reputation 已钳，此处兜直构脏档）
    if not reps:
        return 1.0
    mean = sum(reps) / len(reps)
    clamped = max(-50.0, min(100.0, mean))
    return 1.0 + clamped / 200.0


def competition_factor(world: World, loc) -> float:
    """竞争因子：相邻聚落每个 -8%（钳 0.7——隔壁就是大城，客人被分流）。"""
    n = 0
    for cid in (getattr(loc, "connections", None) or []):
        other = next((l for l in world.locations if l.id == cid), None)
        if other is not None and getattr(other, "kind", "") == "settlement":
            n += 1
    return max(0.7, 1.0 - 0.08 * n)


def visitor_flow(world: World, domain: PlayerDomain, loc, eff_day: int) -> int:
    """[D5] 日客流 = 基数 20 x 档位(1.0/1.8/3.0) x (1+繁荣%) x 声望 x 竞争
    x 日抖动 +-10%（SeededRng 锚 (world, day, domain) 确定性）。"""
    tier = _TIER_FLOW.get(getattr(loc, "settlement_size", "") or "village", 1.0)
    pros = 1.0 + max(0, min(100, int(domain.prosperity))) / 100.0
    base = _FLOW_BASE * tier * pros * rep_factor(world) * competition_factor(world, loc)
    # [P48-D 宅邸互指 2026-09-25] 宅邸陈列撑场面：来客更信你有实力（钳 +10%，读数在总览）
    base *= parlor_factor(world)
    rng = SeededRng.seed_from(world.id, int(eff_day), f"domain_flow_{domain.id}")
    jitter = 0.9 + rng.random() * 0.2
    return max(0, int(round(base * jitter)))


def _shopkeeper_on_duty(world: World, domain: PlayerDomain) -> bool:
    """掌柜在职 = 名册有 shopkeeper 且存活（伙计顶班不算——掌柜才管开张）。"""
    for r in roster_of(domain):
        if r.get("role") != "shopkeeper":
            continue
        n = roster_npc(world, str(r.get("npc_id") or ""))
        if n is not None and n.alive:
            return True
    return False


def _abstract_income(world: World, domain: PlayerDomain, loc, flow: int) -> int:
    """抽象客流收入 = 客流 x 人均消费（设施每座 +15%；无营业商铺折减 0.2——路人零星）。"""
    spend = _SPEND_PER_VISITOR * (1.0 + _FACILITY_SPEND_BONUS * len(domain.facilities or []))
    factor = 1.0 if _shopkeeper_on_duty(world, domain) else _NO_SHOP_FLOW_CUT
    return max(0, int(round(flow * spend * factor)))


def daily_maintenance(domain: PlayerDomain) -> int:
    """设施日维护费 = 各建成设施 upkeep 之和。"""
    total = 0
    for f in (domain.facilities or []):
        if isinstance(f, dict):
            total += _FACILITY_UPKEEP.get(str(f.get("kind") or ""), 0)
    return total


def _adjacent_candidates(world: World, loc) -> list:
    """相邻地点存活非敌对 NPC（按 id 稳定排序保确定性；D5 来访/D6 委托共用候选池）。"""
    cands: list[NPC] = []
    for cid in (getattr(loc, "connections", None) or []):
        other = next((l for l in world.locations if l.id == cid), None)
        if other is None:
            continue
        for nid in (other.npc_ids or []):
            n = roster_npc(world, nid)
            if n is not None and n.alive and not n.hostile:
                cands.append(n)
    return sorted(cands, key=lambda x: x.id)


def _visitor_purchases(world: World, domain: PlayerDomain, loc, rng) -> tuple[list, int]:
    """[D5] 具名来访消费：相邻地点 NPC roll 到访（cap 4/日），在据点商铺真实购物
    （wallet 扣款 / 货架 -1 / 收入入资金池 / 物品进 NPC 背包容量内）。

    返回 (事件列表, 营业收入)；掌柜不在职或无货不发一枪。事件文案走题材池
    events_buy（{a}访客 {item}货品 {shop}店名 {domain}据点名）。
    """
    evts: list = []
    income = 0
    if not _shopkeeper_on_duty(world, domain):
        return evts, 0
    shop = next((s for s in world.shops
                 if s.location_id == loc.id and s.shop_type == "general"), None)
    if shop is None:
        return evts, 0
    item_by_id = {it.id: it for it in world.items}
    stock = [e for e in (shop.stock or []) if e.stock != 0]
    if not stock:
        return evts, 0
    pool = genre_pool(world).get("events_buy") or []
    cands = _adjacent_candidates(world, loc)
    visitors: list[NPC] = []
    for n in cands:
        if len(visitors) >= _VISIT_CAP:
            break
        if rng.random() < _VISIT_CHANCE:
            visitors.append(n)
    for n in visitors:
        # 每客至多买 2 件（钱包 8 成内付得起的货架；同条货架可买多件——[!] for 迭代
        # 单条目只访问一次，须内层 while 逐件购）
        bought = 0
        for entry in list(shop.stock or []):
            while bought < 2 and entry.stock != 0:
                it = item_by_id.get(entry.item_id)
                if it is None:
                    break
                price = tre.buy_price(entry, it, shop, 0, world_pace(world))
                if price <= 0 or int(getattr(n, "wallet", 0) or 0) * 0.8 < price:
                    break                          # 付不起这条，看下一条
                inv = getattr(n, "inventory", None)
                if inv is None or len(inv) >= nle._NPC_INV_CAP:
                    break                          # 背包满不再买（钱留作下次）
                n.wallet = int(getattr(n, "wallet", 0) or 0) - price
                domain.funds = int(domain.funds) + price
                income += price
                if entry.stock != -1:
                    entry.stock = max(0, int(entry.stock) - 1)
                inv.append(entry.item_id)
                shop.touch()
                bought += 1
                if pool:
                    tpl = rng.pick(pool)
                    evts.append(WorldEvent(
                        tick=int(getattr(world, "tick_count", 0) or 0),
                        category="npc", severity="minor", title="据点来客",
                        desc=str(tpl).format(a=n.name, item=it.name,
                                             shop=shop.name, domain=domain.name),
                        npcs=[n.id], locations=[loc.id],
                    ))
            if bought >= 2:
                break
    return evts, income


# ---- [D4] 防务练级（打手战力 = P39a hunt 同口径 level*10+str+vit；快防全引擎确定性）----
def _alive_guards(world: World, domain: PlayerDomain) -> list:
    """名册内在职存活的打手列表。"""
    out = []
    for r in roster_of(domain):
        if r.get("role") != "guard":
            continue
        n = roster_npc(world, str(r.get("npc_id") or ""))
        if n is not None and n.alive:
            out.append(n)
    return out


def guard_power(world: World, domain: PlayerDomain) -> int:
    """打手总战力 = sum(等级x10 + 力 + 耐)（P39a 互市冲突/狩猎同口径）。"""
    from src.services import talent_engine as te
    return sum(max(1, int(getattr(g, "level", 1) or 1)) * 10
               + te.effective_stat(g, "str")
               + te.effective_stat(g, "vit")
               for g in _alive_guards(world, domain))


def city_defense(world: World, domain: PlayerDomain) -> int:
    """城防 = 岗哨设施 25 + 聚落档 10/20/30（设施与升格的防务回报）。"""
    loc = _domain_loc(world, domain)
    base = _CITY_DEF_TIER.get(getattr(loc, "settlement_size", "") or "village", 10)
    if facility_of(domain, "guard") is not None:
        base += _CITY_DEF_GUARD_FAC
    return base


def _train_guards(world: World, domain: PlayerDomain, rng) -> list:
    """[D4] 打手日动作 train：外出历练得 xp（nle._on_gain_xp 自动加点 + HP 重算）；
    15% 负伤（hp -15~25% 保 1）。日常训练不产事件（等级成长在职员区可见），
    负伤发 trivial 进右侧动态栏。"""
    evts = []
    # [P61 专精定策] 操练严格·胆气过人 / 「武备」策略：历练成功率 +0.1（钳 0.9）
    train_chance = min(0.9, _TRAIN_CHANCE
                       + (0.1 if _staff_specialty(domain, "guard") else 0.0)
                       + (0.1 if getattr(domain, "policy", "") == "military" else 0.0))
    for g in _alive_guards(world, domain):
        if rng.random() >= train_chance:
            continue
        nle._on_gain_xp(world, g, _TRAIN_XP_BASE + max(1, int(getattr(g, "level", 1) or 1)))
        if rng.random() < _TRAIN_INJURY_CHANCE:
            hp_max = max(1, int(getattr(g, "hp_max", 1) or 1))
            loss_pct = 0.15 + rng.random() * 0.10
            g.hp = max(1, int(getattr(g, "hp", 1) or 1) - max(1, int(hp_max * loss_pct)))
            evts.append(WorldEvent(
                tick=int(getattr(world, "tick_count", 0) or 0),
                category="npc", severity="trivial", title="护卫负伤",
                desc=f"「{domain.name}」的护卫{g.name}外出历练时挂了彩，回营休养。",
                npcs=[g.id], locations=[domain.location_id],
            ))
    return evts


def _trouble_check(world: World, domain: PlayerDomain, rng, flow: int) -> list:
    """[D4] 闹事事件武力结算：每日 8% roll，闹事战力 = 15 + 客流//2（客流大是非多）。
    打手战力 >= 闹事战力 -> 弹压（护卫得实战 xp）；否则繁荣度 -2（无人弹压）。
    """
    if rng.random() >= _TROUBLE_CHANCE:
        return []
    trouble = 15 + max(0, int(flow)) // 2
    guards = _alive_guards(world, domain)
    gp = guard_power(world, domain)
    if gp >= trouble:
        for g in guards:
            nle._on_gain_xp(world, g, 6)
        return [WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0),
            category="npc", severity="minor", title="平息闹事",
            desc=(f"有醉汉在「{domain.name}」寻衅，护卫们三两下将其拿下"
                  f"（战力 {gp} 对 {trouble}），看客喝彩。"),
            npcs=[g.id for g in guards], locations=[domain.location_id],
        )]
    domain.prosperity = max(0, min(100, int(domain.prosperity) - _TROUBLE_FAIL_PROSPERITY))
    return [WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0),
        category="npc", severity="minor", title="无人弹压",
        desc=(f"有闹事者在「{domain.name}」搅局无人能制（护卫战力 {gp} 对 {trouble}），"
              f"客人们扫兴而散。"),
        locations=[domain.location_id],
    )]


def _raid_check(world: World, domain: PlayerDomain, rng) -> list:
    """[D4] 势力侵袭快防：存在敌对势力（玩家声望 <= -25）时每日 5% roll。
    防御 = 打手战力 + 城防 vs 敌方 = 40 + 势力 power + 抖动 0-20（全确定性快防，
    不走 CombatDialog——「防御走快速战斗」定稿口径）。胜：护卫得 xp + 该势力声望 +2
    （打服了）；败：繁荣 -5 + 资金池被劫一成 + 商铺被砸（货架清空）。
    """
    hostile = []
    for f in (world.factions or []):
        try:
            rep_v = int((getattr(world.player, "reputation", None) or {}).get(f.id, 0) or 0)
        except (TypeError, ValueError):
            continue                     # 脏值跳过（同 rep_factor 容错口径）
        if rep_v <= _RAID_HOSTILE_REP:
            hostile.append(f)
    if not hostile or rng.random() >= _RAID_CHANCE:
        return []
    fac = rng.pick(sorted(hostile, key=lambda x: x.id))
    enemy = _RAID_ENEMY_BASE + max(0, min(100, int(getattr(fac, "power", 0) or 0))) \
        + rng.roll(0, 20)
    guards = _alive_guards(world, domain)
    defense = guard_power(world, domain) + city_defense(world, domain)
    if defense >= enemy:
        for g in guards:
            nle._on_gain_xp(world, g, 8)
        rep = (getattr(world.player, "reputation", None) or {})
        cur = int(rep.get(fac.id, 0) or 0)
        rep[fac.id] = max(-100, min(100, cur + _RAID_WIN_REP))
        who = "护卫与城防" if guards else "城防"
        return [WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0),
            category="event", severity="minor", title="击退来犯",
            desc=(f"「{fac.name}」的人马来犯「{domain.name}」，被{who}击退"
                  f"（防 {defense} 对 敌 {enemy}）；对方忌惮之余，敌意稍减。"),
            npcs=[g.id for g in guards], locations=[domain.location_id],
        )]
    # 失守：掉繁荣 + 资金池被劫一成 + 砸店（货架全毁——P34g 后首次 shop sink）
    domain.prosperity = max(0, min(100, int(domain.prosperity) - _RAID_FAIL_PROSPERITY))
    robbed = int(int(domain.funds) * _RAID_FAIL_FUNDS_PCT)
    domain.funds = int(domain.funds) - robbed
    shop = next((s for s in world.shops
                 if s.location_id == domain.location_id and s.shop_type == "general"), None)
    smashed = ""
    if shop is not None and shop.stock:
        shop.stock = []
        shop.touch()
        smashed = "，商铺货品被砸掠一空"
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    return [WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0),
        category="event", severity="major", title="据点遇袭",
        desc=(f"「{fac.name}」的人马夜袭「{domain.name}」，城防不支（防 {defense} 对 敌 {enemy}）："
              f"繁荣受损、资金池被劫 {robbed} {gt.currency}{smashed}。"),
        locations=[domain.location_id],
    )]


# ---- [D6] 委托板（资金池出委托收购物资；附近 NPC 权衡接单；交付货进商铺货架）----
def commission_unit_price(world: World, item) -> int:
    """委托货品基准单价 = compute_item_price（题材经济公式价）。"""
    return max(1, int(tre.compute_item_price(item)))


def suggested_reward(world: World, item, qty: int) -> int:
    """建议报酬 = 单价 x 数量 x 辛劳系数 1.3（UI 引导；NPC 心里的「值」门槛）。"""
    return max(1, int(round(commission_unit_price(world, item) * max(1, int(qty))
                            * _COMMISSION_TOIL)))


def post_commission(world: World, domain: PlayerDomain, item_id: str,
                    qty: int, reward: int, expire_days: int) -> tuple[bool, str]:
    """[D6] 发布委托（纯 Python）：校验商铺设施/物品/数量/报酬/押金 -> 报酬出资金池
    （押金模式，仿 P34g 拍卖：过期退回防空头单）-> 入板 state=open。

    委托须挂商铺（货交货架）；同时挂单上限 3。
    """
    if facility_of(domain, "shop") is None:
        return False, "须先在据点建商铺（委托的货物交进货架）"
    item = next((it for it in world.items if it.id == item_id), None)
    if item is None:
        return False, "该物品不存在"
    if getattr(item, "type", "") == "key":
        return False, "关键道具不可收购"
    # [拍品隔离 2026-09-09] 拍卖拍品不收委托——交付上架 = 绕过竞价同 id 复卖。
    # 惰性导入防环（world_sim_service 模块级 import 本引擎）。
    from src.services.world_sim_service import _auction_lot_ids
    if item.id in _auction_lot_ids(world):
        return False, "拍卖珍品走拍卖会竞价，不收委托"
    q = max(1, min(_COMMISSION_QTY_MAX, int(qty or 1)))
    days = max(1, min(_COMMISSION_DAYS_MAX, int(expire_days or 1)))
    rw = max(1, int(reward or 0))
    open_now = [c for c in (domain.commissions or []) if c.get("state") == "open"]
    if len(open_now) >= _COMMISSION_OPEN_CAP:
        return False, f"同时挂单上限 {_COMMISSION_OPEN_CAP} 笔（等现有委托交付或过期）"
    if int(domain.funds) < rw:
        gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
        return False, f"资金池余额不足（报酬 {rw} {gt.currency} 发布即扣押）"
    domain.funds = int(domain.funds) - rw
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    domain.commissions = list(domain.commissions or []) + [{
        "id": f"dc{day}_{len(domain.commissions or []) + 1}",
        "item_id": item.id, "item_name": item.name,
        "qty": q, "reward": rw,
        "posted_day": day, "expire_day": day + days,
        "deliver_day": 0, "taker_npc_id": "",
        "state": "open",
    }]
    return True, ""


def _commission_phase(world: World, domain: PlayerDomain, loc, rng, eff_day: int) -> list:
    """[D6] 委托板日推进（纯引擎）：open -> 相邻 NPC 权衡承接（报酬 >= 货价x数量x1.3
    且钱包垫得起货款才接，30%/日琢磨概率）-> taken 次日送达（货进商铺货架 + 报酬入
    承接者钱包，路费已含货价自付）；过期/承接者亡 -> 押金退回资金池。
    done/expired 记录滚动保留 8 条。
    """
    evts: list = []
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    item_by_id = {it.id: it for it in world.items}
    shop = next((s for s in world.shops
                 if s.location_id == loc.id and s.shop_type == "general"), None)
    if shop is None:
        return evts
    cands = _adjacent_candidates(world, loc)
    remaining: list = []
    for c in (domain.commissions or []):
        state = c.get("state", "open")
        if state == "open":
            if eff_day > int(c.get("expire_day", 0) or 0):
                domain.funds = int(domain.funds) + int(c.get("reward", 0) or 0)
                c["state"] = "expired"
                evts.append(WorldEvent(
                    tick=int(getattr(world, "tick_count", 0) or 0),
                    category="npc", severity="trivial", title="委托过期",
                    desc=f"「{domain.name}」收购{c.get('item_name', '')}的委托过了期，"
                         f"押金 {c.get('reward', 0)} {gt.currency}退回资金池。",
                    locations=[loc.id],
                ))
                remaining.append(c)
                continue
            item = item_by_id.get(str(c.get("item_id") or ""))
            if item is None:
                # [审查修复] 物品已不存在（如拍卖 sink 移除）：押金退回，不得静默蒸发
                domain.funds = int(domain.funds) + int(c.get("reward", 0) or 0)
                c["state"] = "expired"
                evts.append(WorldEvent(
                    tick=int(getattr(world, "tick_count", 0) or 0),
                    category="npc", severity="trivial", title="委托告吹",
                    desc=f"「{domain.name}」收购{c.get('item_name', '')}的委托无从办起，"
                         f"押金退回资金池。",
                    locations=[loc.id],
                ))
                remaining.append(c)
                continue
            cost = commission_unit_price(world, item) * int(c.get("qty", 1) or 1)
            worth = int(c.get("reward", 0) or 0) >= int(round(cost * _COMMISSION_TOIL))
            taker = None
            if worth:
                for n in cands:
                    if rng.random() >= _COMMISSION_CONSIDER:
                        continue
                    if int(getattr(n, "wallet", 0) or 0) >= cost:
                        taker = n
                        break
            if taker is not None:
                c["state"] = "taken"
                c["taker_npc_id"] = taker.id
                c["deliver_day"] = eff_day + 1
                evts.append(WorldEvent(
                    tick=int(getattr(world, "tick_count", 0) or 0),
                    category="npc", severity="minor", title="承接委托",
                    desc=f"{taker.name} 觉得「{domain.name}」收购{c.get('item_name', '')}"
                         f"的报酬划算，接了单子去办货。",
                    npcs=[taker.id], locations=[loc.id],
                ))
            remaining.append(c)
        elif state == "taken":
            taker = roster_npc(world, str(c.get("taker_npc_id") or ""))
            if taker is None or not taker.alive:
                # 承接者亡：押金退回（货未到不算账）
                domain.funds = int(domain.funds) + int(c.get("reward", 0) or 0)
                c["state"] = "expired"
                evts.append(WorldEvent(
                    tick=int(getattr(world, "tick_count", 0) or 0),
                    category="npc", severity="trivial", title="委托告吹",
                    desc=f"「{domain.name}」的委托承接者没能回来，押金退回资金池。",
                    locations=[loc.id],
                ))
                remaining.append(c)
                continue
            if eff_day >= int(c.get("deliver_day", 0) or 0):
                item = item_by_id.get(str(c.get("item_id") or ""))
                if item is None:
                    # [审查修复] 承接途中物品消失：退款告吹（不放假「已交付领酬」事件）
                    domain.funds = int(domain.funds) + int(c.get("reward", 0) or 0)
                    c["state"] = "expired"
                    evts.append(WorldEvent(
                        tick=int(getattr(world, "tick_count", 0) or 0),
                        category="npc", severity="trivial", title="委托告吹",
                        desc=f"「{domain.name}」的委托货物已无从采买，押金退回资金池。",
                        locations=[loc.id],
                    ))
                    remaining.append(c)
                    continue
                cost = commission_unit_price(world, item) * int(c.get("qty", 1) or 1)
                if int(getattr(taker, "wallet", 0) or 0) < cost:
                    # [审查修复] 承接者当日钱包被动用垫不起货款：顺延一天（不得
                    # max(0,...) 吞差额——白得报酬凭空造钱）；超期 3 天仍未凑齐退款
                    if eff_day > int(c.get("expire_day", 0) or 0) + 3:
                        domain.funds = int(domain.funds) + int(c.get("reward", 0) or 0)
                        c["state"] = "expired"
                        evts.append(WorldEvent(
                            tick=int(getattr(world, "tick_count", 0) or 0),
                            category="npc", severity="trivial", title="委托告吹",
                            desc=f"{taker.name} 迟迟凑不齐货款，委托告吹押金退回。",
                            npcs=[taker.id], locations=[loc.id],
                        ))
                        remaining.append(c)
                        continue
                    c["deliver_day"] = eff_day + 1
                    remaining.append(c)
                    continue
                taker.wallet = int(getattr(taker, "wallet", 0) or 0) - cost
                taker.wallet = int(taker.wallet) + int(c.get("reward", 0) or 0)
                # 货进据点商铺货架（tag=npc_made：NPC 办的货不走系统漂价/短缺衰减，
                # price=0 走公式价；max_stock=qty 售完即止）——守 P36b 商人定价权口径
                entry = ShopStockEntry(
                    item_id=item.id, price=0,
                    stock=int(c.get("qty", 1) or 1),
                    max_stock=int(c.get("qty", 1) or 1),
                )
                entry.tag = "npc_made"
                shop.stock = list(shop.stock or []) + [entry]
                shop.touch()
                c["state"] = "done"
                evts.append(WorldEvent(
                    tick=int(getattr(world, "tick_count", 0) or 0),
                    category="npc", severity="minor", title="委托交付",
                    desc=f"{taker.name} 把{c.get('item_name', '')} x{c.get('qty', 1)}"
                         f"送进了「{domain.name}」的货架，领走了 {c.get('reward', 0)} "
                         f"{gt.currency}报酬。",
                    npcs=[taker.id], locations=[loc.id],
                ))
            remaining.append(c)
        else:
            remaining.append(c)              # done/expired 保留展示
    # 滚动裁剪：done/expired 只留最近 8 条
    finished = [c for c in remaining if c.get("state") in ("done", "expired")]
    if len(finished) > _COMMISSION_KEEP:
        drop_ids = {c.get("id") for c in finished[:-_COMMISSION_KEEP]}
        remaining = [c for c in remaining if c.get("id") not in drop_ids]
    domain.commissions = remaining
    return evts


def next_tier_info(world: World, domain: PlayerDomain) -> Optional[tuple]:
    """升档信息 (下一档名, 资金池价, 繁荣度门槛)；已是城市返回 None。"""
    loc = _domain_loc(world, domain)
    if loc is None:
        return None
    size = getattr(loc, "settlement_size", "") or "village"
    if size not in _TIER_UPGRADE:
        return None
    price, pros = _TIER_UPGRADE[size]
    nxt = _TIER_ORDER[min(_TIER_ORDER.index(size) + 1, len(_TIER_ORDER) - 1)]
    return nxt, price, pros


def upgrade_tier(world: World, domain: PlayerDomain) -> tuple[bool, str]:
    """[D5] 升聚落档（village->town->city）：资金池付账 + 繁荣度门槛；居民上限
    （6/12/20）与客流档位倍率随之抬升。"""
    if _domain_loc(world, domain) is None:
        return False, "据点地点已不存在"      # 先于 next_tier_info：避免误报「已到顶」
    info = next_tier_info(world, domain)
    if info is None:
        return False, "已是最高聚落档（城市）"
    nxt, price, pros_need = info
    if int(domain.prosperity) < pros_need:
        return False, f"繁荣度不足（需 >= {pros_need}，现 {domain.prosperity}/100）"
    if int(domain.funds) < price:
        gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
        return False, f"资金池余额不足（扩容费 {price} {gt.currency}）"
    loc = _domain_loc(world, domain)
    domain.funds = int(domain.funds) - price
    loc.settlement_size = nxt
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="event", severity="minor",
        title="据点扩容",
        desc=(f"「{domain.name}」大兴土木，升格为{ {'village': '村庄', 'town': '城镇', 'city': '城市'}[nxt] }"
              f"（居民上限 {resident_limit(nxt)}），耗资 {price} {gt.currency}。"),
        locations=[loc.id],
    ))
    return True, ""


# ---- [D7 2026-09-05 用户指示] 据点每日随机遭遇：敌袭自动战 ----
# 有概率（domain_siege_chance，默认 8%/日）敌队来犯据点，守方阵容 = 驻守打手（roster
# 的 guard，真实属性）+ 岗哨哨卫（建筑派生 pseudo 单位），攻方 = 题材怪物池 1-3 只
# （危险度分段 + 繁荣度抬等级——肥羊招贼）。两边纯引擎自动战斗（resolve_attack 逐合
# 对轰，无玩家参与、无 CombatDialog、无 LLM），胜负落结算：胜=缴获入资金池+繁荣+1+
# 打手 xp；败=繁荣-4+资金池被劫+砸半店+打手负伤保 1。rng 消费在日结算序列尾部
# （train/闹事/侵袭之后），既有确定性序列不漂移。
_SIEGE_CHANCE = 0.08            # 默认日遇袭概率（preset.domain_siege_chance 可调）
_SIEGE_MAX_ROUNDS = 10          # 自动战斗回合上限（相持判守方胜——主场优势）
_SIEGE_SPOILS_PER_LVL = 15      # 胜利缴获 = 敌队等级和 x 此值（入资金池）
_SIEGE_FAIL_PROSPERITY = 4      # 失守繁荣度损失
_SIEGE_FAIL_FUNDS_PCT = 0.15    # 失守资金池被劫比例


def _siege_chance(preset) -> float:
    """遇袭概率（preset 旋钮，缺省回退默认；钳 0-1）。

    [!] 0=关 合法：回退用 is None 判定而非 or（0.0 or 0.08 会把关闭吞成默认值——
    真人回归抓出，与 preset from_dict 同坑）。"""
    raw = _SIEGE_CHANCE if preset is None else getattr(preset, "domain_siege_chance", _SIEGE_CHANCE)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = _SIEGE_CHANCE
    return max(0.0, min(1.0, v))


def _siege_attacker_units(world: World, domain: PlayerDomain, loc, rng,
                          anchor_lvl: int = 0) -> "list":
    """攻方阵容：题材怪物池 1-3 只临时单位（不入 world.npcs）。

    等级锚定守方平均等级 ±2 抖动（空防据点锚玩家等级——买高危地自带风险），叠加
    繁荣诱饵 +0~4 级（肥羊招贼）；[!] 不走 band_level 段钳——段底兜底会让新据点
    被段位怪平推（danger3 段底 21 级日日围攻，首版测试抓出）。危险度改为影响
    来袭数量（1 + danger//3 封顶 3）。威胁随驻军成长水涨船高、有来有回可守住。"""
    from src.services import wilderness_engine as we
    units = []
    danger = int(getattr(loc, "danger", 1) or 1)
    n_max = max(1, min(3, 1 + danger // 3))
    n = rng.roll(1, n_max)
    anchor = max(1, int(anchor_lvl or 0) or 1)
    target = max(1, min(100, anchor + int(domain.prosperity) // 25))
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    for _i in range(n):
        tmpl = we._pick_template(world, loc, rng)
        if tmpl is None:
            break
        lvl = max(1, min(100, target + rng.roll(-2, 2)))
        m = NPC(name=str(tmpl.get("name", "魔物") or "魔物"),
                role=str(tmpl.get("role") or "魔物"), hostile=True, level=lvl)
        # [数值抖动/flaky 修复 2026-09-06] 逐怪独立 rng，种子挂稳定键（世界+天+怪名+序号），
        # 不污染主序列——原挂临时怪 uuid4（每次构建全新），同日同怪两次构建属性都不同，
        # 违背「同 world+entity 同结果」且把确定性测试搅成间歇性失败
        wr = SeededRng.seed_from(world.id, day, f"siege_mon_{m.name}_{_i}")
        jit = lambda base: max(1, base + wr.roll(-1, 2))
        m.stat_str = jit(4 + lvl)
        m.stat_vit = jit(4 + lvl)
        m.stat_dex = jit(3 + lvl // 2)
        m.stat_int = 3
        m.stat_luk = 3
        m.hp_max = ce.max_hp_for(m)
        m.hp = m.hp_max
        snap = ce.compute_stats(m)
        units.append(ce.CombatUnit(name=m.name, snapshot=snap, ai="aggressive",
                                   role_label="来袭"))
    return units


def _siege_defender_units(world: World, domain: PlayerDomain):
    """守方阵容：驻守打手（真实属性快照，战后 hp 回写保 1）+ 岗哨哨卫（建筑派生）。

    返回 (units, staff_pairs)；staff_pairs = [(npc, unit)] 供战后 hp 回写。
    哨卫等级随城防值成长（城防//5），聚落升格加一名——建筑与升格的防务可视化。"""
    units = []
    staff_pairs = []
    for g in _alive_guards(world, domain):
        snap = ce.compute_stats(g)
        u = ce.CombatUnit(name=g.name, snapshot=snap, ai="defensive",
                          npc_id=str(g.id), role_label="驻守")
        units.append(u)
        staff_pairs.append((g, u))
    guard_fac = facility_of(domain, "guard")
    if guard_fac is not None:
        cd = city_defense(world, domain)
        sentry_lvl = max(3, cd // 5)
        sentry_n = 2 if str(getattr(_domain_loc(world, domain), "settlement_size", "")
                            or "") == "city" else 1
        for i in range(sentry_n):
            s = NPC(name=f"{guard_fac.get('name', '岗哨')}哨卫", role="哨卫",
                    level=sentry_lvl)
            wr = SeededRng.seed_from(world.id, 0, f"siege_sentry_{domain.id}_{i}")
            s.stat_str = max(1, 5 + sentry_lvl + wr.roll(-1, 2))
            s.stat_vit = max(1, 5 + sentry_lvl + wr.roll(-1, 2))
            s.stat_dex = max(1, 3 + sentry_lvl // 2)
            s.stat_int, s.stat_luk = 3, 3
            s.hp_max = ce.max_hp_for(s)
            s.hp = s.hp_max
            units.append(ce.CombatUnit(name=s.name, snapshot=ce.compute_stats(s),
                                       ai="defensive", role_label="哨卫"))
    return units, staff_pairs


def _siege_unit_spec(u) -> dict:
    """[敌袭观战] 攻方单位定格序列化（CombatUnit 快照参数 -> dict；不含 runtime cd 等）。"""
    sp = u.snapshot
    return {
        "name": u.name, "ai": u.ai,
        "hp": int(sp.hp), "hp_max": int(sp.hp_max),
        "atk": int(sp.atk), "def_": int(sp.def_), "magic_atk": int(sp.magic_atk),
        "crit_rate": float(sp.crit_rate), "speed": int(sp.speed),
        "level": int(sp.level),
    }


def _siege_unit_from_spec(spec: dict):
    """[敌袭观战] 攻方单位从 pending 规格重建（CombatSnapshot + CombatUnit，攻击型 AI）。"""
    from src.services.combat_engine import CombatSnapshot
    sp = CombatSnapshot(
        hp=int(spec.get("hp", 1)), hp_max=int(spec.get("hp_max", 1)),
        atk=int(spec.get("atk", 5)), def_=int(spec.get("def_", 2)),
        magic_atk=int(spec.get("magic_atk", 0)),
        crit_rate=float(spec.get("crit_rate", 0.05)),
        speed=int(spec.get("speed", 10)), level=int(spec.get("level", 1)),
    )
    return ce.CombatUnit(name=str(spec.get("name", "魔物")), snapshot=sp,
                         ai=str(spec.get("ai", "aggressive")), role_label="来袭")


def rebuild_siege_lines(world: World, domain: PlayerDomain):
    """[敌袭观战] 从 world.pending_domain_siege 重建攻守两军（守方实时真实属性）。

    返回 (dfd_units, staff_pairs, atk_units)；pending 缺失/不合法返回 (None, None, None)。
    供 world_sim_service.build_domain_siege_session 组装观战 CombatSession。"""
    pd = getattr(world, "pending_domain_siege", None)
    if not isinstance(pd, dict) or not pd.get("atk"):
        return None, None, None
    if str(pd.get("domain_id", "")) != str(domain.id):
        return None, None, None
    dfd, staff_pairs = _siege_defender_units(world, domain)
    if not dfd:
        return None, None, None
    atk = [_siege_unit_from_spec(x) for x in pd["atk"] if isinstance(x, dict)]
    return dfd, staff_pairs, (atk or None)


def settle_spectated_siege(world: World, domain: PlayerDomain, session, tick: int) -> list:
    """[敌袭观战] 观战战斗（spectate CombatSession）结束结算——与后台 _auto_battle 路
    同口径：胜=缴获入资金池+繁荣+1+打手 xp+8；败=_siege_fail（繁荣 -4/资金池被劫/砸店）。
    守方打手 hp 按战后快照回写保 1（npc_id 匹配 ally_units；玩家位=守方队长 staff_pairs[0]）。
    清 pending_domain_siege。"""
    pd = getattr(world, "pending_domain_siege", None)
    world.pending_domain_siege = {}
    if session is None:
        return []
    # [审核修复 2026-09-13] 原「无打手即 return []」把整场结算吞掉：pending 已在上面清空，
    # 于是「建了岗哨但打手未雇/全灭/欠薪离职」时玩家看完一整场观战，胜无缴获、败无惩罚，
    # 与后台 _auto_battle 口径分裂。岗哨哨卫只进 units 不进 staff_pairs（哨卫是建筑派生的
    # 临时单位，无 npc 可回写），故此处改为：无打手时跳过 hp 回写与 xp，但照常结算胜负。
    dfd, staff_pairs = _siege_defender_units(world, domain)
    atk_lvl_sum = 0
    if isinstance(pd, dict) and isinstance(pd.get("atk"), list):
        atk_lvl_sum = sum(int(x.get("level", 1) or 1) for x in pd["atk"] if isinstance(x, dict))
    atk_desc = "、".join(str(x.get("name", "魔物")) for x in (pd or {}).get("atk", [])
                         if isinstance(x, dict)) or "来敌"
    # hp 回写：玩家位 -> 队长（staff_pairs[0]）；ally 按 npc_id 匹配（无打手时整段跳过）
    leader_npc = None
    if staff_pairs:
        leader_npc, _leader_unit = staff_pairs[0]
        if leader_npc is not None:
            leader_npc.hp = max(1, min(int(leader_npc.hp_max or 1),
                                       int(session.player.hp)))
    for npc, _u in staff_pairs[1:]:
        u2 = next((x for x in (session.ally_units or [])
                   if str(getattr(x, "npc_id", "")) == str(npc.id)), None)
        if u2 is not None:
            npc.hp = max(1, min(int(npc.hp_max or 1), int(u2.snapshot.hp)))
        else:
            npc.hp = max(1, int(npc.hp) // 2)          # 未上阵/找不到：按负伤处理
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    staff = [n for n, _ in staff_pairs]
    if session.state == "won":
        spoils = _SIEGE_SPOILS_PER_LVL * atk_lvl_sum
        domain.funds = int(domain.funds) + spoils
        domain.prosperity = max(0, min(100, int(domain.prosperity) + 1))
        for npc in staff:
            nle._on_gain_xp(world, npc, 8)
        alive = "、".join([leader_npc.name] + [u.name for u in (session.ally_units or [])
                                               if u.alive]) if leader_npc is not None else "残阵"
        return [WorldEvent(
            tick=tick, category="event", severity="minor", title="击退敌袭",
            desc=(f"「{atk_desc}」袭击「{domain.name}」，驻守阵容当场迎战破敌（剩 {alive}），"
                  f"缴获 {spoils} {gt.currency} 入资金池，繁荣度 +1。"),
            npcs=[n.id for n in staff], locations=[domain.location_id],
        )]
    return _siege_fail(world, domain, [], staff, atk_desc, gt, tick)


def _auto_battle(atk_units, dfd_units, rng, max_rounds: int = _SIEGE_MAX_ROUNDS):
    """[D7] 纯引擎自动战斗（无玩家/无 LLM）：守方先手（主场），逐合互殴——
    每个存活单位随机选一名存活敌人普攻（resolve_attack 含部位骰），直至一方全灭
    或回合上限（相持判守方胜）。返回 (winner: "defend"|"attack", 战报行 list)。"""
    log: list[str] = []
    for _round in range(1, max_rounds + 1):
        for units, foes in ((dfd_units, atk_units), (atk_units, dfd_units)):
            for u in units:
                if not u.alive:
                    continue
                targets = [f for f in foes if f.alive]
                if not targets:
                    break
                tgt = rng.pick(targets)
                r = ce.resolve_attack(u.snapshot, tgt.snapshot, rng, granularity="medium")
                tgt.snapshot.hp = r.defender_hp_after
                line = f"{u.name}攻击{tgt.name}，造成 {r.damage} 点伤害"
                if not r.hit:
                    line = f"{u.name}的攻击未命中{tgt.name}"
                if r.defender_defeated:
                    line += "（倒下）"
                log.append(line)
        if not any(u.alive for u in atk_units):
            return "defend", log
        if not any(u.alive for u in dfd_units):
            return "attack", log
    return "defend", log          # 相持：守方主场优势守住


def _siege_check(world: World, domain: PlayerDomain, loc, rng, preset=None) -> list:
    """[D7] 据点敌袭日判定：roll 中 -> 双方阵容自动战斗 -> 胜负结算 + 战报事件。"""
    # [季节玩法化 2026-09-06] 冬季饥匪更活跃（季节系数，season_fx 关闭时恒 1.0）
    from src.services import calendar_engine as _cale
    _seige_fx = _cale.season_fx(world).get("siege_chance", 1.0)
    if rng.random() >= min(1.0, _siege_chance(preset) * _seige_fx):
        return []
    tick = int(getattr(world, "tick_count", 0) or 0)
    gt = GenreText(world.config_overlay if isinstance(world.config_overlay, dict) else {})
    dfd, staff_pairs = _siege_defender_units(world, domain)
    anchor = (sum(int(u.snapshot.level) for u in dfd) // len(dfd)) if dfd         else max(1, int(getattr(world.player, "level", 1) or 1))
    atk = _siege_attacker_units(world, domain, loc, rng, anchor_lvl=anchor)
    if not atk:
        return []
    atk_desc = "、".join(u.name for u in atk)
    if not dfd:
        # 空防据点：不战而下（惩罚性后果 + 事件，逼玩家建岗哨/雇打手）
        return _siege_fail(world, domain, atk, [], atk_desc, gt, tick, no_defense=True)
    # [敌袭观战 2026-09-06 用户指示] 玩家恰在据点时不后台速算——攻方阵容定格存
    # pending（tick 在 worker 线程，UI 由场景页回合完成后弹观战战斗实时打）。
    if str(getattr(world.player, "location_id", "") or "") == str(domain.location_id):
        world.pending_domain_siege = {
            "domain_id": str(domain.id), "tick": tick,
            "atk": [_siege_unit_spec(u) for u in atk],
        }
        return [WorldEvent(
            tick=tick, category="event", severity="major", title="敌袭爆发",
            desc=(f"「{atk_desc}」袭击「{domain.name}」！驻守阵容（"
                  f"{'、'.join(u.name for u in dfd)}）列阵迎战——战斗一触即发。"),
            npcs=[n.id for n, _ in staff_pairs], locations=[domain.location_id],
        )]
    winner, log = _auto_battle(atk, dfd, rng)
    # 打手 hp 回写（战斗快照独立于 npc；保 1 不死——据点战役不阵亡，负伤休养）
    for npc, unit in staff_pairs:
        npc.hp = max(1, int(unit.snapshot.hp))
    if winner == "defend":
        spoils = _SIEGE_SPOILS_PER_LVL * sum(int(u.snapshot.level) for u in atk)
        domain.funds = int(domain.funds) + spoils
        domain.prosperity = max(0, min(100, int(domain.prosperity) + 1))
        for npc, _u in staff_pairs:
            nle._on_gain_xp(world, npc, 8)
        brief = _siege_brief(log)
        return [WorldEvent(
            tick=tick, category="event", severity="minor", title="击退敌袭",
            desc=(f"「{atk_desc}」夜袭「{domain.name}」，驻守阵容迎战破敌"
                  f"（剩 {'、'.join(u.name for u in dfd if u.alive) or '残阵'}），"
                  f"缴获 {spoils} {gt.currency} 入资金池，繁荣度 +1。{brief}"),
            npcs=[n.id for n, _ in staff_pairs], locations=[domain.location_id],
        )]
    return _siege_fail(world, domain, atk, [n for n, _ in staff_pairs],
                       atk_desc, gt, tick)


def _siege_fail(world: World, domain: PlayerDomain, atk, staff, atk_desc, gt, tick,
                no_defense: bool = False) -> list:
    """[D7] 失守结算：繁荣 -4 + 资金池被劫一成半 + 商铺货架砸半 + 打手负伤保 1。"""
    domain.prosperity = max(0, min(100, int(domain.prosperity) - _SIEGE_FAIL_PROSPERITY))
    robbed = int(int(domain.funds) * _SIEGE_FAIL_FUNDS_PCT)
    domain.funds = int(domain.funds) - robbed
    shop = next((s for s in world.shops
                 if s.location_id == domain.location_id and s.shop_type == "general"), None)
    smashed = ""
    if shop is not None and shop.stock:
        half = len(shop.stock) // 2
        shop.stock = shop.stock[half:] if half else []
        shop.touch()
        smashed = "，商铺货品被砸掠近半"
    lead = "据点无人驻守，来敌长驱直入" if no_defense else "驻守阵容力战不支"
    return [WorldEvent(
        tick=tick, category="event", severity="major", title="据点失守",
        desc=(f"「{atk_desc}」袭击「{domain.name}」，{lead}：繁荣度 -{_SIEGE_FAIL_PROSPERITY}，"
              f"资金池被劫 {robbed} {gt.currency}{smashed}"
              + ("，驻守打手带伤退守。" if staff else "。")),
        npcs=[n.id for n in staff], locations=[domain.location_id],
    )]


def _siege_brief(log: list) -> str:
    """战报节选（首 2 合 + 末 1 合，控制事件文案长度；全量战况在战斗日志）。"""
    if not log:
        return ""
    picks = log[:2] + (log[-1:] if len(log) > 2 else [])
    return "战报：" + "；".join(picks) + "。"


# ============ [P48 据点产线 2026-09-25 用户指示] 订单化生产 ============
_ORDER_MAX_OPEN = 3          # 同时在制订单上限
_ORDER_QTY_MAX = 6           # 单笔数量上限
_ORDER_STALL_DAYS = 3        # 连续断料天数 -> stalled（停工待料）


def post_order(world: World, domain: PlayerDomain, recipe_id: str, qty: int,
               ship: str = "shelf") -> "tuple[bool, str]":
    """发布生产订单（DomainDialog 产线区）。返回 (ok, err)。

    校验与 _tick_warehouse 掌柜口径同源：required_building 空（掌柜不做建筑门控配方）、
    产出 rarity 白绿蓝（nle._BASIC_RARITIES）、产出不在拍品隔离清单（_auction_lot_ids）、
    同时待办单（open/stalled）<= 3、qty 1-6；多件产出配方留给玩家亲手制造。
    订单不扣钱——代价是「定向采料占用」：有单在身时
    伙计/农夫不再随机囤货，而是对着订单缺口采。
    """
    if ship not in ("shelf", "warehouse", "home"):
        return False, "出货去向只能是货架、据点仓库或同地住宅仓库"
    if ship == "home" and _order_home(world, domain) is None:
        return False, "同地住宅尚未建仓库，无法送货上门"
    open_n = len(_order_active(domain))
    if open_n >= _ORDER_MAX_OPEN:
        return False, f"在制订单已满（{_ORDER_MAX_OPEN} 单），先等完工或撤单"
    try:
        qty = max(1, min(_ORDER_QTY_MAX, int(qty)))
    except (TypeError, ValueError):
        return False, "数量不合法"
    r = next((r for r in (world.recipes or [])
              if getattr(r, "id", "") == str(recipe_id or "")), None)
    if r is None:
        return False, "配方不存在"
    if max(1, int(getattr(r, "output_qty", 1) or 1)) != 1:
        return False, "这张配方一炉产出多件，需你亲自在住宅工坊制作"
    if getattr(r, "required_building", ""):
        return False, "该配方需要专门建筑，掌柜做不了"
    item_by_id = {it.id: it for it in (world.items or [])}
    out = item_by_id.get(str(getattr(r, "output_item_id", "") or ""))
    if out is None:
        return False, "配方产出物不存在"
    if getattr(out, "rarity", "") not in nle._BASIC_RARITIES:
        return False, "掌柜只能造白绿蓝的东西，好货得你自己来"
    from src.services.world_sim_service import _auction_lot_ids
    if out.id in _auction_lot_ids(world):
        return False, "该产出正被拍卖行隔离，不能进产线"
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    seq = len(domain.orders or []) + 1
    domain.orders = list(domain.orders or []) + [{
        "id": f"po{day}_{seq}", "recipe_id": str(r.id), "out_id": str(out.id),
        "out_name": str(getattr(out, "name", "") or out.id), "qty": qty, "made": 0,
        "ship": ship, "state": "open", "created_day": day, "stall_days": 0,
    }]
    return True, ""


def cancel_order(domain: PlayerDomain, order_id: str) -> bool:
    """撤单（玩家主动）：停止定向采料与按单合成。已上架/入仓的成品不回收。"""
    orders = list(domain.orders or [])
    hit = next((o for o in orders if str(o.get("id", "")) == str(order_id or "")
                and str(o.get("state", "")) in ("open", "stalled")), None)
    if hit is None:
        return False
    orders.remove(hit)
    domain.orders = orders
    return True


def _order_active(domain: PlayerDomain) -> list:
    """待生产或待补料订单；停工单仍占产线并参与定向备料。"""
    return [o for o in (domain.orders or [])
            if str(o.get("state", "")) in ("open", "stalled")]


def _order_home(world: World, domain: PlayerDomain):
    """同地且属于玩家、已建仓库的住宅才可接据点生产货。"""
    from src.services import home_engine as he
    owned = set(getattr(world.player, "home_ids", None) or [])
    return next((h for h in (getattr(world, "homes", None) or [])
                 if h.id in owned and h.location_id == domain.location_id
                 and he.stash_capacity(h) > 0), None)


def _order_needs(world: World, domain: PlayerDomain) -> dict:
    """汇总所有待办单的缺料 {item_id: 缺多少}（定向采料目标清单）。

    只算「仓库现有 vs 订单还差」的缺口；伙计/农夫各自在自己的采集池里优先命中。
    """
    wh = domain.warehouse if isinstance(domain.warehouse, list) else []
    have: dict = {}
    for iid in wh:
        have[str(iid)] = have.get(str(iid), 0) + 1
    need: dict = {}
    for o in _order_active(domain):
        r = next((r for r in (world.recipes or [])
                  if getattr(r, "id", "") == str(o.get("recipe_id", ""))), None)
        if r is None:
            continue
        remain = max(0, int(o.get("qty", 1)) - int(o.get("made", 0)))
        if remain <= 0:
            continue
        cnt: dict = {}
        for iid in (r.inputs or []):
            cnt[str(iid)] = cnt.get(str(iid), 0) + 1
        for iid, n in cnt.items():
            need[iid] = need.get(iid, 0) + n * remain
    return {iid: max(0, total - have.get(iid, 0))
            for iid, total in need.items() if total > have.get(iid, 0)}


def order_view(world: World, domain: PlayerDomain) -> list:
    """产线订单读数（DomainDialog 产线区数据源，纯函数）：进度/缺料明细/预计完工日。"""
    out = []
    available: dict = {}
    for iid in (domain.warehouse if isinstance(domain.warehouse, list) else []):
        available[str(iid)] = available.get(str(iid), 0) + 1
    for o in (domain.orders or []):
        o = dict(o)
        o["progress_txt"] = f"{int(o.get('made', 0))}/{int(o.get('qty', 1))}"
        o["lacks"] = []
        o["eta_days"] = 0
        if str(o.get("state", "")) in ("open", "stalled"):
            r = next((r for r in (world.recipes or [])
                      if getattr(r, "id", "") == str(o.get("recipe_id", ""))), None)
            keeper = next((roster_npc(world, str(x.get("npc_id") or ""))
                           for x in roster_of(domain) if x.get("role") == "shopkeeper"), None)
            if keeper is None or not keeper.alive:
                o["blocked"] = "缺少在职掌柜"
            elif not any(s.location_id == domain.location_id and s.shop_type == "general"
                         for s in (world.shops or [])):
                o["blocked"] = "据点商铺尚未建好"
            remain = max(0, int(o.get("qty", 1)) - int(o.get("made", 0)))
            if r is not None:
                item_by_id = {it.id: it for it in (world.items or [])}
                per: dict = {}
                for iid in (r.inputs or []):
                    per[str(iid)] = per.get(str(iid), 0) + 1
                max_lack_batches = 0
                for iid, n in per.items():
                    required = n * remain
                    allocated = min(required, available.get(iid, 0))
                    available[iid] = available.get(iid, 0) - allocated
                    lack = required - allocated
                    if lack > 0:
                        it = item_by_id.get(iid)
                        o["lacks"].append(f"{getattr(it, 'name', iid)} x{lack}")
                        max_lack_batches = max(max_lack_batches, lack)
                # 预计完工：按「日产能 1 件 + 每日最多补齐一批料」粗估
                o["eta_days"] = remain + (1 if max_lack_batches else 0)
            if str(o.get("ship", "")) == "home":
                from src.services import home_engine as he
                home = _order_home(world, domain)
                if home is None:
                    o["blocked"] = "同地住宅仓库不可用"
                elif len(home.stash) >= he.stash_capacity(home):
                    o["blocked"] = "住宅仓库已满"
        out.append(o)
    return out


def parlor_factor(world: World) -> float:
    """[P48-D 住宅互指] 宅邸陈列撑场面：各住宅陈列件数 -> 据点客流加成（钳 +10%）。"""
    bonus = 0.0
    home_ids = getattr(world.player, "home_ids", None) or []
    homes = getattr(world, "homes", None)
    for hid in home_ids:
        h = next((h for h in (homes or []) if getattr(h, "id", "") == hid), None)
        n = len(getattr(h, "display_shelf", None) or []) if h is not None else 0
        bonus += min(n, 5)
    return 1.0 + 0.02 * min(bonus, 5.0)
