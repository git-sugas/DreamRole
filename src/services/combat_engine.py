"""CRPG 战斗引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

数值范式（守用户确认）：
- LLM 产静态定义数值（NPC 等级/HP/属性、物品 attack/defense/词缀、玩家属性初值）
  -> 代码解析为强类型 + 钳制。
- 动态计算（伤害/暴击/命中/掉落/经验/升级）纯 Python 公式 + SeededRng，
  LLM 不参与动态结果。同种子同结果，可复现可单测。

公式：
- atk_total = (stat_str + stat_bonus.str) * 2 + weapon_attack + sum(affix atk)
- def_total = (stat_vit + stat_bonus.vit) + armor_defense + sum(affix def)
- magic_atk = (stat_int + stat_bonus.int) * 2 + sum(affix magic_atk)  [P7e] 法术/技能伤害基础
- crit_rate = 0.05 + stat_dex*0.005 + stat_luk*0.002 + sum(affix crit)（[P8] luk 副导暴击）
- crit_dmg = 1.5 + stat_str*0.02
- hp_max = max_hp_for(entity) = stat_vit*10 + level*5（[P5c] 抽 helper，6 处复制消除）
- dodge = clamp(stat_dex*0.004, 0, 0.4)  [P7e] 闪避率（命中后抵消）
- parry = clamp(stat_str*0.003, 0, 0.3)  [P7e] 招架率（物理命中后减伤 50%）
- heal_bonus = int(stat_int*0.5)  [P7e] 治疗量加成（heal 时叠加）
- 逃跑 = clamp(0.45 + (atk_speed-def_speed)*0.02 + luk*0.003, 0.1, 0.9)  [P8] luk 显式加成
- 负重 carry_capacity(entity) = 20 + stat_vit*2  [P8] 背包物品种类上限（vit 驱动）
- 伤害 = max(1, int(atk_total * (crit_dmg if crit else 1.0) - def_total * 0.5))
- 命中 = clamp((0.9 + (atk_speed - def_speed)*0.02) * (1 - defender.dodge), 0.05, 0.99)  [P7e] dodge 抵消
- 升级阈值 = int(100 * level^1.5)
- 掉落率 = clamp(base_rate * difficulty_mult * (1 + luck*0.01), 0, 0.95)  [P7e] stat_luk 进 roll_loot
- 掉落稀有度升档 roll_rarity_upgrade(rng, base, luck, level_bonus)  [P8] luck+level 驱动升档概率
- 通用成功率 success_chance(stat, tool, difficulty, base) = clamp(base + stat*0.01 + tool*0.05 - difficulty*0.02, 0.05, 0.95)  [P7e] 采集钩子

[P5c] affixes 双口径兼容：顶层 {"atk":3} 或嵌套 {"mods":{"atk":3}} 均识别。
[P5c] stat_bonus 字段在 compute_stats 内聚合进属性（修隐藏 bug：原先未生效）。
[P7e] 5 维属性全部进公式：stat_str(攻/暴伤/招架)/stat_dex(命中/暴击/速度/闪避)/stat_int(法攻/治疗)/stat_vit(防/HP)/stat_luk(速度/掉落)。

难度系数（[P 验收] 整体上调一档）：easy 无修正（= 一般游戏普通难度）；normal 玩家伤害*0.85/受击*1.2（= 困难）；hard 玩家伤害*0.7/受击*1.4（更困难）。满级 100 级（见 gain_xp）。
粒度：light 无暴击无命中判定（必中必伤）；medium 标准；heavy 加抗性减免。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from src.utils.rng import SeededRng
from src.services import talent_engine as te


# ---- 数据快照（引擎算出的聚合值，不持久化，每回合重算）----
@dataclass
class CombatSnapshot:
    """战斗属性快照（base + 装备 + 词缀聚合后的有效值）。"""
    hp: int = 0
    hp_max: int = 0
    atk: int = 0
    def_: int = 0
    magic_atk: int = 0       # [P7e] 法术攻击 = stat_int*2 + Σ词缀magic_atk（技能/法术伤害基础）
    crit_rate: float = 0.0
    crit_dmg: float = 1.5
    speed: int = 0
    dodge: float = 0.0       # [P7e] 闪避率（钳制 0~0.4），命中后抵消判定
    parry: float = 0.0       # [P7e] 招架率（钳制 0~0.3），物理命中后减伤 50%
    heal_bonus: int = 0      # [P7e] 治疗加成（heal 时叠加到回血量）
    incoming_crit_bonus: float = 0.0  # 头伤：防守时额外受暴击率
    heal_received_mult: float = 1.0   # 躯干伤：实际获得的治疗倍率
    level: int = 1
    luck: int = 0            # [P8] 幸运快照（逃跑加成用；speed 已含 luk//2，此处留全值）
    elements: tuple = ()     # [P9] 元素构成（从 entity.elements 聚合；元素克制矩阵结算用）
    # [部位 2026-08-31] 该快照的部位弱点 key（空=无弱点）。玩家快照留空（敌方打玩家
    # 无弱点可打）；敌方快照由 start_combat 按 unit 确定性 roll 后注入（transient，
    # 快照是战斗本地对象不落盘）。bare 主敌 snapshot 同样携带（resolve_attack 直读）。
    weakpoint: str = ""
    # [装备新属性 2026-09-01] 词缀/天赋聚合出的概率型战斗属性（钳 0~0.5，防堆叠满值）。
    counter_rate: float = 0.0    # 反击率：被普攻命中后概率反伤 30% 本次伤害
    combo_rate: float = 0.0      # 连击率：普攻命中后概率同伤害追击一次（同部位）
    lifesteal_rate: float = 0.0  # 吸血率：造成伤害后按比例回自身 HP


@dataclass
class AttackResult:
    """单次攻击结算结果。"""
    hit: bool = False           # 是否命中
    crit: bool = False          # 是否暴击
    damage: int = 0             # 造成伤害（未命中为 0）
    defender_hp_after: int = 0  # 被攻击方剩余 HP
    defender_defeated: bool = False  # 被攻击方是否阵亡
    # [部位 2026-08-31] 本次攻击命中的部位（"head"/"torso"/"arm"/"legs" 之一；未命中为空串）
    body_part: str = ""
    weakpoint: bool = False     # 是否命中弱点（伤害加成已计入 damage）


# ---- [部位 2026-08-31] 攻击部位系统 ----
# 部位 key -> (中文名, 基础命中率, 伤害倍率)。命中率经 defender 体型修正后归一。
# 部位伤害倍率不固定（躯干 1.0 / 头 1.6 / 手臂 0.7 / 下半身 0.5），命中部位经
# rng roll（骰子），roll 到弱点部位时额外 x1.5 且可暴击（弱点暴击独立 roll）。
BODY_PARTS: dict[str, tuple[str, float, float]] = {
    "head":  ("头部", 0.10, 1.6),
    "torso": ("躯干", 0.50, 1.0),
    "arm":   ("手臂", 0.20, 0.7),
    "legs":  ("下半身", 0.20, 0.5),
}
BODY_PART_KEYS = tuple(BODY_PARTS.keys())
_WEAKPOINT_MULT = 1.5      # 弱点命中伤害倍率
_WEAKPOINT_CRIT_BONUS = 0.25  # 弱点部位暴击率加成（叠在攻击者 crit_rate 上）


def roll_body_part(rng: SeededRng) -> str:
    """按基础命中率 roll 部位（骰子；确定性）。"""
    r = rng.random()
    acc = 0.0
    for key in BODY_PART_KEYS:
        acc += BODY_PARTS[key][1]
        if r < acc:
            return key
    return "torso"


def part_display(key: str) -> str:
    """部位 key -> 中文显示名。"""
    return BODY_PARTS.get(key, ("",))[0] or key


def part_mult(key: str) -> float:
    """部位 key -> 伤害倍率。"""
    return BODY_PARTS.get(key, ("", 0.0, 1.0))[2] or 1.0


def apply_body_part(rng: SeededRng, base_dmg: int, part: str,
                    weakpoint: str, crit_rate: float,
                    crit_dmg: float) -> "tuple[int, bool, bool]":
    """[部位] 对一次已命中的伤害套部位倍率与弱点判定。返回 (伤害, 是否弱点, 是否暴击)。

    - 倍率先乘（部位 x 弱点），结果保 >=1（命中不空刀）。
    - 弱点命中：额外暴击 roll（crit_rate + 弱点加成），暴击再乘 crit_dmg。
    - 非弱点不引入额外暴击 roll（保持既有 crit 判定序列不变，仅弱点路径多掷一骰）。
    """
    dmg = max(1, int(base_dmg * part_mult(part)))
    is_weak = bool(weakpoint) and part == weakpoint
    crit = False
    if is_weak:
        dmg = max(1, int(dmg * _WEAKPOINT_MULT))
        crit = rng.chance(max(0.0, min(0.95, crit_rate + _WEAKPOINT_CRIT_BONUS)))
        if crit:
            dmg = max(1, int(dmg * crit_dmg))
    return dmg, is_weak, crit


@dataclass
class XpResult:
    """经验结算结果。"""
    leveled_up: bool = False
    new_level: int = 0
    xp: int = 0
    xp_next: int = 0
    stat_points_gained: int = 0   # [P7d2] 本次升级获得的可分配属性点（每级 +STAT_POINTS_PER_LEVEL）


# ---- 难度/粒度系数 ----
# [P 验收] 整体难度上调一档 + 难度挂钩整个世界（不只玩家伤害）：
# easy = 其他游戏一般难度（无修正）、normal = 困难、hard = 更困难。满级 100 级（见 gain_xp）。
# 每档一组乘数：player_atk/def=玩家伤害/受击；monster_hp/atk/def=怪物 HP/攻/法攻/防；
# loot=掉落；xp=经验获取；check_penalty=判定难度偏移（锻造/采集/奇遇/隐藏检定成功率基准扣减）。
_DIFF_MULT = {
    "easy": {"player_atk": 1.0, "player_def": 1.0,
             "monster_hp": 1.0, "monster_atk": 1.0, "monster_def": 1.0,
             "loot": 1.0, "xp": 1.0, "check_penalty": 0},
    "normal": {"player_atk": 0.85, "player_def": 1.2,
               "monster_hp": 1.2, "monster_atk": 1.15, "monster_def": 1.15,
               "loot": 0.9, "xp": 1.1, "check_penalty": 5},
    "hard": {"player_atk": 0.7, "player_def": 1.4,
             "monster_hp": 1.5, "monster_atk": 1.3, "monster_def": 1.3,
             "loot": 0.8, "xp": 1.25, "check_penalty": 10},
}


def diff_cfg(difficulty: str) -> dict:
    """难度配置表查询（未知档回退 normal）。"""
    return _DIFF_MULT.get(difficulty, _DIFF_MULT["normal"])


def apply_monster_difficulty(snap: "CombatSnapshot", difficulty: str) -> "CombatSnapshot":
    """怪物侧难度乘数：HP/攻/法攻/防 分别乘 monster_hp/atk/def。返回新 snapshot（不动原对象）。

    敌方单位（主敌/随从/快速战斗目标）构建 snapshot 后调用；玩家/同伴不调（走玩家侧倍率）。
    """
    c = diff_cfg(difficulty)
    mh, ma, md = c["monster_hp"], c["monster_atk"], c["monster_def"]
    return replace(
        snap,
        hp=int(snap.hp * mh), hp_max=int(snap.hp_max * mh),
        atk=int(snap.atk * ma), def_=int(snap.def_ * md),
        magic_atk=int(snap.magic_atk * ma),
    )


def xp_difficulty_mult(difficulty: str) -> float:
    """经验获取难度倍率（困难给更多经验作为风险补偿）。"""
    return diff_cfg(difficulty)["xp"]


def loot_difficulty_mult(difficulty: str) -> float:
    """掉落难度倍率。"""
    return diff_cfg(difficulty)["loot"]


def check_difficulty_penalty(difficulty: str) -> int:
    """判定难度偏移（加到 success_chance 的 difficulty，困难下更难通过）。"""
    return diff_cfg(difficulty)["check_penalty"]


def difficulty_penalty_of(world: Any) -> int:
    """从 world.config_overlay 取全局判定难度偏移（各引擎 success_chance 调用统一用）。"""
    ov = getattr(world, "config_overlay", None)
    if not isinstance(ov, dict):
        ov = {}
    return check_difficulty_penalty(ov.get("difficulty", "normal"))

# ---- [P9] 元素克制矩阵（技能 element vs 目标 NPC.elements 参与伤害增减）----
# 方向：攻击元素 -> 它克制的防御元素集合（题材无关的机制层，显示名走 GenreText/中文映射）。
# 结算规则（element_damage_mult）：
#   攻击元素 克 目标元素     x1.35（「克制」）
#   目标元素 克 攻击元素     x0.65（「被抗」：火蜥蜴天然抗冰）
#   同元素                   x0.75（「同系吸收」：火打火蜥蜴）
#   无关系 / physical        x1.0（纯物理不吃元素克制，含 physical==physical）
# 多元素目标各自独立乘（如火+地双系敌人吃火克时也吃地系关系）。
_ELEMENT_COUNTERS = {
    "fire":    ("ice", "metal"),   # 烈火融冰、火炼真金
    "ice":     ("wood", "earth"),  # 寒冰封木、冻裂厚土
    "thunder": ("metal", "wood", "water"),  # 雷霆熔金、天雷劈木、[P30] 雷击湿身（wet 暴露 water）
    "wood":    ("earth",),         # 草木破土
    "earth":   ("thunder",),       # 大地引雷入地
    "metal":   ("wind",),          # 金锋裂空
    "wind":    ("ice",),           # 罡风蚀冰
    "light":   ("dark",),          # 光明驱散黑暗
    "dark":    ("light",),         # 黑暗吞噬光明
}
_COUNTER_MULT = 1.35   # 克制增伤
_RESIST_MULT = 0.65    # 被克减伤（反向克制）
_SAME_MULT = 0.75      # 同系吸收

# [P9] 元素 key -> 中文短名（单一来源在 talent_engine，此处 re-export 供 UI/日志用）
ELEMENT_ZH = te.ELEMENT_ZH


def element_damage_mult(attack_element: str, defender_elements) -> tuple[float, str]:
    """[P9] 据元素克制矩阵算技能伤害乘数。返回 (乘数, 提示标签)。

    提示标签：""（无关系）/ "克制" / "被抗" / "同系吸收"（多种关系并存时取最显著：
    克制 > 同系 > 被抗）。attack_element 空 / physical、defender 无元素 -> (1.0, "")。
    纯函数无 rng，确定性。
    """
    ae = str(attack_element or "").strip()
    if not ae or ae == "physical" or ae not in _ELEMENT_COUNTERS:
        return 1.0, ""
    des = [str(e or "").strip() for e in (defender_elements or [])]
    if not des:
        return 1.0, ""
    mult = 1.0
    tag = ""
    for de in des:
        if not de:
            continue
        if de in _ELEMENT_COUNTERS.get(ae, ()):
            mult *= _COUNTER_MULT
            tag = "克制"   # 克制无条件最高优先（后到也覆盖先到的同系/被抗）
        elif ae in _ELEMENT_COUNTERS.get(de, ()):
            mult *= _RESIST_MULT
            if tag != "克制":
                tag = tag or "被抗"
        elif de == ae:
            mult *= _SAME_MULT
            # 同系 > 被抗：覆盖先到的被抗，但让位于克制
            if tag != "克制":
                tag = "同系吸收"
    return mult, tag


_ELEMENT_TAG_ZH = {"克制": "克制！效果拔群", "被抗": "对方抗性较大", "同系吸收": "同源之力被吸收"}


def element_tag_text(tag: str) -> str:
    """[P9] 提示标签 -> 战斗日志后缀（空标签返回空串）。"""
    t = _ELEMENT_TAG_ZH.get(tag)
    return f"（{t}）" if t else ""


def element_countered_by(elem: str) -> str:
    """[方案 B 2026-09-10 用户拍板] 元素 elem 被哪个攻击元素克制（无则空串）。

    背景：武器元素词缀注入快照 elements 后**双向生效**——既让普攻/技能按该元素吃克制
    增伤，也让该单位受击时按同一元素吃克制/被克/同系。UI 需要把「附火伤」补成
    「附火伤（受冰克）」形态，玩家才能预期自己多出来的弱点。
    按 _ELEMENT_COUNTERS 声明序扫描（确定性：同一元素恒返回同一克制者，不引入 rng）。
    """
    e = str(elem or "").strip()
    if not e or e not in _ELEMENT_COUNTERS:
        return ""
    for atk, countered in _ELEMENT_COUNTERS.items():
        if atk != e and e in countered:
            return atk
    return ""


def infer_elements_from_skills(skills) -> list[str]:
    """[P9] 从实体技能推断元素构成（LLM 未给 elements 时的引擎兜底）。

    取非空非 physical 元素中出现次数最多的 1 个（并列取首个出现）；无 -> 空。
    纯函数确定性（不 roll）。
    """
    counts: dict[str, int] = {}
    for sk in (skills or []):
        if not isinstance(sk, dict):
            continue
        el = str(sk.get("element", "") or "").strip()
        if el and el != "physical":
            counts[el] = counts.get(el, 0) + 1
    if not counts:
        return []
    best = max(counts.items(), key=lambda kv: kv[1])[0]
    return [best]


# ============ [P30] 状态效果系统 ============
# 状态定义：key -> {type, dot_dmg_pct, skip_action, dmg_taken_mult, stat_mods, cleanse_by}
# type: dot(回合末扣血) / control(跳过行动/限制) / debuff(减益) / buff(增益) / marker(元素前置无直接效果)
# dot_dmg_pct: DoT 每回合扣 max_hp 的百分比（0.0-1.0）
# skip_action: True=跳过本回合行动（stunned/frozen）
# dmg_taken_mult: 受击伤害乘数（frozen=1.5 冰封易碎，protected=0.5 护盾减伤）
# speed_mult: 速度乘数（shocked=0.5 雷慑迟钝，chilled=0.7 减速）
# hit_mult: 命中乘数（blinded=0.7 致盲）
# flee_blocked: True=不能逃跑（entangled 缠绕）
# atk_mult/def_mult: 攻防乘数（enraged 攻1.3防0.7）
# cleanse_by: 可被哪些元素清除（wet 被 fire/thunder 消耗，burning 被 wet/ice 清除）
_CONDITION_DEFS = {
    "poisoned":  {"type": "dot",     "dot_dmg_pct": 0.06},
    "burning":   {"type": "dot",     "dot_dmg_pct": 0.08, "cleanse_by": ("wet", "ice")},
    "bleeding":  {"type": "dot",     "dot_dmg_pct": 0.05},
    "stunned":   {"type": "control", "skip_action": True},
    "frozen":    {"type": "control", "skip_action": True, "dmg_taken_mult": 1.5, "cleanse_by": ("fire",)},
    "feared":    {"type": "control", "flee_only": True},
    "shocked":   {"type": "debuff",  "speed_mult": 0.5, "dodge_mult": 0.0},
    "blinded":   {"type": "debuff",  "hit_mult": 0.7},
    "chilled":   {"type": "debuff",  "speed_mult": 0.7},
    "entangled": {"type": "debuff",  "flee_blocked": True, "speed_mult": 0.5},
    "protected": {"type": "buff",    "dmg_taken_mult": 0.5},
    "enraged":   {"type": "buff",    "atk_mult": 1.3, "def_mult": 0.7},
    "wet":       {"type": "marker",  "cleanse_by": ("fire", "thunder")},  # 元素前置，被火/雷消耗
}

# 元素互动表：(目标已有 condition, 技能 element) -> 结果 condition（附加到目标，消耗前置）。
# 这是 BG3 式涌现的核心：wet + thunder -> shocked；ice + wet -> frozen；wet douses burning 等。
# [Q2 涌现深化] 扩展状态间互动：bleeding+fire->burning、chilled+thunder->shocked、
# entangled+fire->解除、poisoned+wind->吹散、frozen+fire->融冰。
_ELEMENT_REACTIONS = {
    ("wet", "thunder"): "shocked",     # 湿身遭雷 -> 雷慑
    ("wet", "ice"):     "frozen",      # 湿身遇寒 -> 冰封
    ("wet", "fire"):    None,           # 湿身遇火 -> 蒸汽（仅消耗 wet，无新状态）
    ("burning", "ice"): None,           # 灼烧遇冰 -> 灭火（仅清除 burning）
    ("burning", "wet"): None,           # 灼烧遇水 -> 灭火（仅清除 burning）-- wet 在施加时也触发
    ("bleeding", "fire"): "burning",    # [Q2] 点燃伤口血 -> 灼烧（消耗 bleeding 升级）
    ("chilled", "thunder"): "shocked",  # [Q2] 冷体导电 -> 雷慑（消耗 chilled）
    ("entangled", "fire"): None,        # [Q2] 烧藤解除（火攻解开缠绕，消耗 entangled）
    ("poisoned", "wind"): None,         # [Q2] 风吹散毒（风系技能清 poisoned，消耗 poisoned）
    ("frozen", "fire"): None,           # [Q2] 火融冰（火技能主动解冻，消耗 frozen）
}

# [P30] 6 题材状态显示名（GenreText；抽象 key -> 题材化中文；western_fantasy 兜底）。
# [!] 多题材兼容重点：状态逻辑用抽象 key，显示层题材化。
_CONDITION_GENRE_ZH = {
    "xianxia": {
        "poisoned": "中毒", "burning": "焚灼", "bleeding": "流血", "stunned": "眩晕",
        "frozen": "冰封", "feared": "惊惧", "shocked": "雷殛", "blinded": "致盲",
        "chilled": "寒滞", "entangled": "缠绕", "protected": "护体", "enraged": "狂暴", "wet": "浸水",
    },
    "wuxia": {
        "poisoned": "中毒", "burning": "灼伤", "bleeding": "流血", "stunned": "眩晕",
        "frozen": "冰封", "feared": "惊惧", "shocked": "雷慑", "blinded": "致盲",
        "chilled": "寒滞", "entangled": "束缚", "protected": "护体", "enraged": "狂暴", "wet": "淋湿",
    },
    "modern": {
        "poisoned": "中毒", "burning": "燃烧", "bleeding": "流血", "stunned": "眩晕",
        "frozen": "冰冻", "feared": "恐惧", "shocked": "触电", "blinded": "致盲",
        "chilled": "受寒", "entangled": "缠绕", "protected": "护盾", "enraged": "暴怒", "wet": "湿透",
    },
    "scifi": {
        "poisoned": "毒素", "burning": "灼烧", "bleeding": "漏液", "stunned": "瘫痪",
        "frozen": "冷冻", "feared": "系统恐慌", "shocked": "短路", "blinded": "感测失灵",
        "chilled": "低温", "entangled": "力场束缚", "protected": "能量护盾", "enraged": "过载", "wet": "浸水",
    },
    "apocalypse": {
        "poisoned": "中毒", "burning": "燃烧", "bleeding": "流血", "stunned": "眩晕",
        "frozen": "冰冻", "feared": "恐惧", "shocked": "触电", "blinded": "致盲",
        "chilled": "失温", "entangled": "缠绕", "protected": "护盾", "enraged": "狂暴", "wet": "淋湿",
    },
    "western_fantasy": {
        "poisoned": "中毒", "burning": "燃烧", "bleeding": "流血", "stunned": "眩晕",
        "frozen": "冰封", "feared": "恐惧", "shocked": "触电", "blinded": "致盲",
        "chilled": "寒冷", "entangled": "缠绕", "protected": "护盾", "enraged": "狂暴", "wet": "湿身",
    },
}


def condition_display(key: str, genre_id: str = "western_fantasy") -> str:
    """[P30] 状态 key -> 题材化显示名（缺题材/缺 key 回退 western_fantasy，再缺回退 key 本身）。"""
    pool = _CONDITION_GENRE_ZH.get(str(genre_id or "western_fantasy"), _CONDITION_GENRE_ZH["western_fantasy"])
    return pool.get(str(key or ""), str(key or ""))


def condition_dot_damage(key: str, hp_max: int) -> int:
    """[P30] DoT 状态本回合扣血量（按 max_hp 百分比；非 DoT 返回 0）。"""
    d = _CONDITION_DEFS.get(str(key or ""), {})
    if d.get("type") != "dot":
        return 0
    return max(1, int((getattr(d, "dot_dmg_pct", 0) or d.get("dot_dmg_pct", 0)) * max(1, int(hp_max or 1))))


def _conds_of(session, side) -> list:
    """[P30] 统一取某方状态列表（原地可改，与原引用同对象）。

    side: "player" -> session.player_conditions；"enemy" -> session.enemy_conditions；
    CombatUnit 实例 -> unit.conditions。玩家/主敌是 bare CombatSnapshot，状态挂 session。
    """
    if side == "player":
        return session.player_conditions
    if side == "enemy":
        return session.enemy_conditions
    return side.conditions  # CombatUnit 实例


def _has_cond(conds, key) -> bool:
    """[P30] 某方是否处于指定状态。"""
    key = str(key or "")
    return any(isinstance(c, dict) and c.get("key") == key for c in (conds or []))


def _remove_cond(conds, key) -> bool:
    """[P30] 移除某状态（全部同名实例），返回是否移除了至少一个。原地改 list。"""
    key = str(key or "")
    before = len(conds)
    conds[:] = [c for c in conds if not (isinstance(c, dict) and c.get("key") == key)]
    return len(conds) < before


def _cond_duration(conds, key) -> int:
    """[P30] 取某状态剩余回合数（无则 0）。"""
    key = str(key or "")
    for c in (conds or []):
        if isinstance(c, dict) and c.get("key") == key:
            try:
                return max(0, int(c.get("duration", 0) or 0))
            except (TypeError, ValueError):
                return 0
    return 0


def _effective_elements(snapshot, conds) -> tuple:
    """[P30] 目标有效元素构成 = 原生 elements + wet 暴露的 water（被雷克）。

    [!] 玩家本体无 elements 是设计性豁免，但 [方案 B 2026-09-10 用户拍板] 武器元素词缀
    会经 compute_stats 并入快照 elements -> 玩家受击时也吃元素关系（UI 标「附X伤（受Y克）」）；
    wet 状态让元素对玩家另有一层意义：被泼水后遭雷击吃 x1.35 克制（thunder counters water）
    + 触发 wet+thunder->shocked。
    """
    els = tuple(str(e or "").strip() for e in (getattr(snapshot, "elements", ()) or ())
                if str(e or "").strip())
    if _has_cond(conds, "wet"):
        els = els + ("water",)
    return els


def _cond_mult(conds, field: str, default: float = 1.0) -> float:
    """[P30] 从状态列表累乘 _CONDITION_DEFS 中某乘数字段。

    field: dmg_taken_mult / atk_mult / def_mult / speed_mult / dodge_mult / hit_mult。
    无该字段的状态不影响（跳过）。多个同字段状态累乘。
    """
    m = default
    for c in (conds or []):
        if not isinstance(c, dict):
            continue
        d = _CONDITION_DEFS.get(str(c.get("key", "") or ""), {})
        v = d.get(field)
        if v is None:
            continue
        try:
            m *= float(v)
        except (TypeError, ValueError):
            continue
    return m


def _inflict_condition(session, side, cond_key: str, duration: int,
                       source_element: str = "", rng=None, chance: float = 1.0) -> str:
    """[P30] 对某方附加状态（经 chance 检定 + 元素互动检查）。返回日志片段（空串=未附加）。

    元素互动（读 _ELEMENT_REACTIONS）：施加 wet 到 burning 目标 -> 清 burning（灭火）。
    其余互动（wet+thunder->shocked 等）由 _react_elements 在技能命中时按技能元素单独触发。
    同 key 已存在则刷新 duration 取较大。duration 钳 >=1。状态非法（不在 _CONDITION_DEFS）忽略。
    """
    cond_key = str(cond_key or "")
    if not cond_key or cond_key not in _CONDITION_DEFS:
        return ""
    if rng is not None and chance < 1.0:
        if not rng.chance(max(0.0, min(1.0, float(chance)))):
            return ""
    conds = _conds_of(session, side)
    genre = getattr(session, "genre_id", "western_fantasy") or "western_fantasy"
    logs = []
    # 元素互动：施加 wet 到 burning 目标 -> 灭火（_ELEMENT_REACTIONS[("burning","wet")] -> None）
    if cond_key == "wet" and _has_cond(conds, "burning"):
        _remove_cond(conds, "burning")
        logs.append(f"{condition_display('burning', genre)}被浇灭")
    # 附加 / 刷新（同 key 取较大 duration）
    dur = max(1, int(duration) if duration else 1)
    existing = next((c for c in conds if isinstance(c, dict) and c.get("key") == cond_key), None)
    if existing is not None:
        if int(existing.get("duration", 0) or 0) < dur:
            existing["duration"] = dur
    else:
        conds.append({"key": cond_key, "duration": dur,
                      "source_element": str(source_element or "")})
    logs.append(f"陷入{condition_display(cond_key, genre)}（{dur} 回合）")
    return "，".join(logs)


def _react_elements(session, side, attack_element: str, rng) -> str:
    """[P30] 技能命中后：技能元素 vs 目标已有状态的元素互动（BG3 涌现核心）。

    检查 _ELEMENT_REACTIONS[(已有状态, 技能元素)]：
      wet + thunder -> shocked（消耗 wet）/ wet + ice -> frozen / wet + fire -> 蒸汽（仅消耗 wet）
      burning + ice -> 灭火（仅清 burning）
    返回日志片段（空串=无互动）。结果状态（shocked/frozen）确定性附加，不经 chance。
    """
    ae = str(attack_element or "").strip()
    if not ae:
        return ""
    conds = _conds_of(session, side)
    if not conds:
        return ""
    genre = getattr(session, "genre_id", "western_fantasy") or "western_fantasy"
    logs = []
    for (existing_cond, atk_el), result in _ELEMENT_REACTIONS.items():
        if atk_el != ae or not _has_cond(conds, existing_cond):
            continue
        _remove_cond(conds, existing_cond)  # 消耗前置（wet 被火/雷/冰消耗；burning 被冰清）
        disp = condition_display(existing_cond, genre)
        if result is None:
            logs.append(f"{disp}被{ae}消解" if existing_cond == "wet" else f"{disp}被扑灭")
        else:
            # 结果状态同 key 去重（刷新 duration 取较大）——_cond_mult 累乘，重复 append 会
            # 叠出双重 frozen 2.25x 等超设计上限（与 _inflict_condition 口径一致）
            exist_res = next((c for c in conds if isinstance(c, dict) and c.get("key") == result), None)
            if exist_res is not None:
                if int(exist_res.get("duration", 0) or 0) < 2:
                    exist_res["duration"] = 2
            else:
                conds.append({"key": result, "duration": 2, "source_element": ae})
            logs.append(f"{disp}遇{ae}化为{condition_display(result, genre)}")
    return "，".join(logs)


def _apply_skill_inflicts(session, side, skill, rng) -> str:
    """[P30] 技能命中后读 skill.inflicts 逐条附加状态到目标。返回日志片段（空串=无 inflicts）。"""
    raw = skill.get("inflicts") if isinstance(skill, dict) else None
    if not raw or not isinstance(raw, list):
        return ""
    genre = getattr(session, "genre_id", "western_fantasy") or "western_fantasy"
    logs = []
    for inf in raw:
        if not isinstance(inf, dict):
            continue
        ck = str(inf.get("condition", "") or "")
        if not ck:
            continue
        try:
            ch = float(inf.get("chance", 1.0) or 1.0)
        except (TypeError, ValueError):
            ch = 1.0
        dur = max(1, int(inf.get("duration", 1) or 1))
        seg = _inflict_condition(session, side, ck, dur, source_element=str(skill.get("element", "") or ""),
                                 rng=rng, chance=ch)
        if seg:
            logs.append(seg)
    # [P31] cleanse 反应：友方被施加负面状态后，有 heal 技能的同伴概率清除一个
    if logs:
        conds = _conds_of(session, side)
        # [!] wrapper（打随从路径）与玩家共享 snapshot/conditions，须识别为玩家侧——
        # 否则主敌路径（side="player"）触发的净化在随从路径静默丢失（口径分裂）；
        # 敌方随从 snapshot 是主敌克隆，is 判定不会误认。
        is_friend = (side == "player") or (
            isinstance(side, CombatUnit) and (
                side in (session.ally_units or [])
                or side.snapshot is session.player))
        if is_friend and conds:
            cr = _check_reactions(session, "on_condition_applied_friend", rng,
                                  target_side=side, conds=conds)
            if cr and cr.get("cleared"):
                cleaner = cr["unit"]
                cl = cr["cleared"]
                target_name = "你" if side == "player" else getattr(side, "name", "同伴")
                logs.append(f"{cleaner.name}施展净化，清除了{target_name}的{condition_display(cl, genre)}")
    return "；".join(logs)


# ---- [P51 战意 2026-09-25] Battle Brothers 式士气（transient，零 rng 主干）----
# 增量全确定性（钩子单一入口收口，防口径分裂）；档位乘区经 resolve_attack 的
# morale_mult 可选参（默认 1.0 -> 既有调用零改动、既有测试零位移）。
_MORALE_BASE = 60
_MORALE_ALLY_DOWN = -18      # 己方单位倒下：全队 -
_MORALE_ENEMY_DOWN = 10      # 敌方倒下：我方 +（镜像）
_MORALE_CRIT_TAKEN = -8      # 被暴击
_MORALE_HEALED = 4           # 被治疗/护体
_MORALE_REGEN = 2            # 回合末向 60 均值自然回归
_MORALE_FLEE_CHANCE = 0.6    # 敌方杂兵崩溃溃逃概率


def _morale_of(session, side: str) -> int:
    """读某侧战意（side: player/enemy；玩家的同伴单位各读自身 morale）。"""
    if side == "player":
        return int(getattr(session, "player_morale", _MORALE_BASE))
    return int(getattr(session, "enemy_morale", _MORALE_BASE))

# 战意档位题材词（6 题材；西幻回退）——科幻=系统稳定度、仙侠=道心这类题材味
_MORALE_GENRE_ZH: dict = {
    "western_fantasy": {"high": "士气高昂", "stable": "沉着", "shaken": "动摇", "broken": "崩溃"},
    "xianxia": {"high": "道心通明", "stable": "心湖平稳", "shaken": "心浮气躁", "broken": "道心蒙尘"},
    "wuxia": {"high": "气势如虹", "stable": "沉稳", "shaken": "心慌", "broken": "胆寒"},
    "modern": {"high": "斗志昂扬", "stable": "镇定", "shaken": "慌乱", "broken": "崩溃"},
    "scifi": {"high": "系统超频", "stable": "运行正常", "shaken": "过载降频", "broken": "系统死机"},
    "apocalypse": {"high": "杀红了眼", "stable": "硬撑", "shaken": "手抖", "broken": "吓破了胆"},
}


def morale_label(session, side: str, genre_id: str = "western_fantasy") -> str:
    # 战意档位显示词（UI 用；题材池缺键回退西幻）
    tier = _morale_tier(_morale_of(session, side))
    pool = _MORALE_GENRE_ZH.get(str(genre_id or "") or "western_fantasy") \
        or _MORALE_GENRE_ZH["western_fantasy"]
    return pool.get(tier, "稳定")


def _set_morale(session, side: str, v: int) -> int:
    v = max(0, min(100, int(v)))
    if side == "player":
        session.player_morale = v
    else:
        session.enemy_morale = v
    return v


def _shift_morale(session, side: str, delta: int) -> None:
    _set_morale(session, side, _morale_of(session, side) + delta)


def _morale_tier(v: int) -> str:
    """战意档：high 高昂 / stable 稳定 / shaken 动摇 / broken 崩溃。"""
    if v >= 80:
        return "high"
    if v >= 35:
        return "stable"
    if v <= 15:
        return "broken"
    return "shaken"


def _morale_mult(v: int) -> float:
    """战意伤害乘区（resolve_attack 的 morale_mult 入参取值）。"""
    t = _morale_tier(v)
    if t == "high":
        return 1.10
    if t == "shaken":
        return 0.85
    if t == "broken":
        return 0.70           # 崩溃（主敌/Boss 免疫溃逃时的惩罚）
    return 1.0


_MORALE_CRIT_BONUS = 0.05   # [P51 战意暴击钩] 战意高昂攻击方暴击率加成（resolve_attack 消费）

# [P59 counterplay 2026-09-26] 动作意图覆写的反制（读【敌情】预告做反制的收益）：
# 打断（控制命中已预告的敌人）士气奖励——技能蓄力被打断 +5 / 普攻 +3
_INTERRUPT_MORALE_SKILL = 5
_INTERRUPT_MORALE_ATTACK = 3
_COUNTERPARRY_SKILL_REDUCE = 0.7   # 预判卸力：预告技能 vs 玩家举盾 -> 伤害 x0.7


def _morale_on_hit(session, side: str, crit: bool) -> None:
    """[单一入口] 结算点命中后战意钩子（crit 才掉；四处暴击点统一走这里）。"""
    if crit:
        _shift_morale(session, side, _MORALE_CRIT_TAKEN)


def _morale_on_death(session, side_down: str) -> None:
    """[单一入口] 单位倒下：倒下一方全队 -18、对面 +10。"""
    if side_down == "player":
        _shift_morale(session, "player", _MORALE_ALLY_DOWN)
        _shift_morale(session, "enemy", _MORALE_ENEMY_DOWN)
    else:
        _shift_morale(session, "enemy", _MORALE_ALLY_DOWN)
        _shift_morale(session, "player", _MORALE_ENEMY_DOWN)


def _tick_morale(session) -> str:
    """回合末战意结算（挂 _apply_conditions 后）：自然回归 + 杂兵崩溃溃逃判定。

    返回溃逃日志（""=无）。崩溃的敌方随从 60% 溃逃离场（不入掉落）；主敌/Boss
    免疫溃逃只吃 x0.7 惩罚；我方同伴崩溃由下一回合强制防御表现（UI 提示）。
    溃逃 roll 用独立盐 morale{round}——不碰既有 rng 流（守确定性回放铁律）。
    """
    log = ""
    for side in ("player", "enemy"):
        # 自然回归：低于 60 的向均值 +2（高于不衰减——高昂是奖励态）
        v = _morale_of(session, side)
        if v < _MORALE_BASE:
            _set_morale(session, side, v + _MORALE_REGEN)
    # 敌方随从崩溃溃逃（主敌/Boss 免疫——只吃 x0.7 惩罚）
    if _morale_of(session, "enemy") <= 15:
        rng = SeededRng.seed_from(str(getattr(session, "world_id", "") or "w"),
                                  session.round, f"morale{session.round}")
        kept = []
        for u in (session.enemy_units or [])[1:]:
            if getattr(u, "alive", True) and rng.chance(_MORALE_FLEE_CHANCE):
                log += f"{u.name}士气崩溃，溃逃出了战场！"
            else:
                kept.append(u)
        session.enemy_units = [session.enemy_units[0]] + kept
    return log


def _apply_conditions(session) -> str:
    """[P30] 回合末状态 tick：DoT 扣血 + duration 递减 + 过期清除 + 反应预算重置。

    对玩家/主敌/全部单位（随从+同伴）逐一 tick。DoT（poisoned/burning/bleeding）按 max_hp 百分比
    扣血（condition_dot_damage）；所有状态 duration-1，<=0 清除。[P31] 反应预算（reaction_used/
    player_reaction_used）回合末重置。无 rng（DoT 确定性）。返回日志（空串=无状态变动）。
    """
    genre = getattr(session, "genre_id", "western_fantasy") or "western_fantasy"
    # 收集全部参战者：(snapshot, conds, 显示名)
    # [!] 主敌包装 unit 的 snapshot is session.enemy 且 conditions 共享 session.enemy_conditions
    # （start_combat 设 main_unit.conditions = session.enemy_conditions），须跳过防双 tick；
    # 玩家不在 unit 列表但同样防御性跳过 snapshot is session.player 的单位。
    fighters = [
        (session.player, session.player_conditions, "你"),
        (session.enemy, session.enemy_conditions, "敌人"),
    ]
    for u in list((session.enemy_units or []) + (session.ally_units or [])):
        if isinstance(u, CombatUnit) and u.alive:
            if u.snapshot is session.enemy or u.snapshot is session.player:
                continue  # 已被上方玩家/主敌对覆盖（共享 conds 列表，跳过防双 tick）
            fighters.append((u.snapshot, u.conditions, u.name))
    logs = []
    for snap, conds, name in fighters:
        if not conds:
            continue
        tick_log = []
        for c in list(conds):
            if not isinstance(c, dict):
                continue
            key = str(c.get("key", "") or "")
            if not key:
                continue
            d = _CONDITION_DEFS.get(key, {})
            if d.get("type") == "dot":
                dmg = condition_dot_damage(key, getattr(snap, "hp_max", 1) or 1)
                if dmg > 0:
                    snap.hp = max(0, (getattr(snap, "hp", 0) or 0) - dmg)
                    tick_log.append(f"{name}因{condition_display(key, genre)}损失 {dmg} 点生命")
            try:
                c["duration"] = max(0, int(c.get("duration", 0) or 0) - 1)
            except (TypeError, ValueError):
                c["duration"] = 0
            if int(c.get("duration", 0) or 0) <= 0:
                tick_log.append(f"{name}的{condition_display(key, genre)}消退")
        # 清除过期状态
        conds[:] = [c for c in conds if isinstance(c, dict) and int(c.get("duration", 0) or 0) > 0]
        if tick_log:
            logs.append("；".join(tick_log))
    # [P31] 反应预算回合末重置（P30 一并重置无副作用）
    session.player_reaction_used = False
    for u in list((session.enemy_units or []) + (session.ally_units or [])):
        if isinstance(u, CombatUnit):
            u.reaction_used = False
    return "\n".join(l for l in logs if l)


# [P7d2] 每升一级获得的可分配属性点数（玩家手动分配到 str/dex/int/vit/luk）
STAT_POINTS_PER_LEVEL = 3


def _stat(entity: Any, name: str, default: int = 0) -> int:
    """安全读 entity 的属性值（防脏数据）。"""
    v = getattr(entity, name, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# [饱食度 2026-09-06 用户指示] 饥饿线：玩家 hunger 低于此值全属性 x0.5（吃饭/食物恢复）。
HUNGER_STARVE = 30


def hunger_stat_mult(entity: Any) -> float:
    """[饱食度] 玩家饥饿惩罚系数（五维乘数）。hunger < 30 -> 0.5，否则 1.0。

    无 hunger 字段（临时怪/桩）或饱食度关闭的世界（hunger 恒 100）都返回 1.0。
    只在玩家侧属性消费点显式乘（战斗快照/合成/采集/检定）；NPC 不罚（饥饿只驱动行为）。
    """
    h = _stat(entity, "hunger", 100)
    return 0.5 if h < HUNGER_STARVE else 1.0


# ---- [P52 伤疤 2026-09-25] 战损留痕：injuries=[{part, until_day, name}]，痊愈按 day 水位清 ----
# 生效单一出口（仿 hunger_stat_mult 模式）：arm 攻 / legs 闪避速度 / head 受暴 / torso 疗效。
# 数值宜轻（x0.9 档）——伤是“余波”不是“废人”。
_INJURY_PART_FACETS = ("arm", "legs", "head", "torso")


def active_injuries(entity: Any, day: int = 0) -> list:
    """未痊愈的伤列表（[{part, until_day, name}]；无字段/过期/脏值安全）。"""
    out = []
    for it in (getattr(entity, "injuries", None) or []):
        if not isinstance(it, dict):
            continue
        if day and int(it.get("until_day", 0) or 0) <= int(day):
            continue
        out.append(it)
    return out


def injury_stat_mult(entity: Any, facet: str, day: int = 0) -> float:
    """伤疤属性乘数（单一出口）。facet: atk/dodge/head_crit/heal。

    [!] 只在对应属性消费点显式乘（compute_stats/狩猎等）；NPC 与玩家同罚
        （伤是身体的，不分你我——与饥饿「NPC 不罚」不同：NPC 也会带伤打猎变差）。
    """
    day = int(day or getattr(entity, "_cur_day", 0) or 0)
    mult = 1.0
    for it in active_injuries(entity, day):
        part = str(it.get("part", ""))
        if part == "arm" and facet == "atk":
            mult *= 0.9
        elif part == "legs" and facet in ("dodge", "speed"):
            mult *= 0.85
        elif part == "head" and facet == "head_crit":
            mult *= 1.0            # head 的效果在受暴击率（+0.05/伤），不乘区
        elif part == "torso" and facet == "heal":
            mult *= 0.8
    return mult


def head_extra_crit_rate(entity: Any, day: int = 0) -> float:
    """头伤：受暴击率加成（每处头伤 +0.05）。"""
    extra = 0.0
    for it in active_injuries(entity, day):
        if str(it.get("part", "")) == "head":
            extra += 0.05
    return extra


def equipped_stat_bonus(world: Any, entity: Any) -> dict:
    """[修 2026-09-06 真机用户抓出] 已装备物品顶层 stat_bonus 合计（纯函数单一来源）。

    战斗侧（compute_stats equip_stat_bonus）与 UI 显示（场景页面板/背包属性面板）共用，
    防「结算生效了、面板还打裸值」的口径分裂。world.items 是蓝图目录，按 equipped 的
    id 引用聚合；sb 非 dict / 值非整数一律跳过。
    """
    merged: dict = {}
    equipped = getattr(entity, "equipped", None) or {}
    if not isinstance(equipped, dict):
        return merged
    items = getattr(world, "items", None) or []
    for iid in equipped.values():
        it = next((i for i in items if getattr(i, "id", "") == iid), None)
        sb = getattr(it, "stat_bonus", None) if it is not None else None
        if not isinstance(sb, dict):
            continue
        for k, v in sb.items():
            try:
                merged[k] = merged.get(k, 0) + int(v)
            except (TypeError, ValueError):
                continue
    return merged


def primary_stat_display(world: Any, entity: Any, key: str) -> str:
    """五维常态值及来源，供场景页和背包页共用。饥饿/伤势另在战斗时结算。"""
    if key not in ("str", "dex", "int", "vit", "luk"):
        return "0"
    base = _stat(entity, f"stat_{key}", 10)
    equip = _safe_int_sb(equipped_stat_bonus(world, entity), key)
    talent = _safe_int_sb(te.get_talent_stat_bonus(entity), key)
    intrinsic_bonus = getattr(entity, "stat_bonus", None)
    intrinsic = _safe_int_sb(intrinsic_bonus, key) if isinstance(intrinsic_bonus, dict) else 0
    affix = 0
    equipped = getattr(entity, "equipped", None) or {}
    if isinstance(equipped, dict):
        items = getattr(world, "items", None) or []
        for iid in equipped.values():
            item = next((it for it in items if getattr(it, "id", "") == iid), None)
            for entry in (getattr(item, "affixes", None) or []):
                if not isinstance(entry, dict):
                    continue
                bonus = entry.get("stat_bonus")
                if not isinstance(bonus, dict):
                    mods = entry.get("mods")
                    bonus = mods.get("stat_bonus") if isinstance(mods, dict) else None
                if isinstance(bonus, dict):
                    affix += _safe_int_sb(bonus, key)
    parts = [("装", equip), ("赋", talent), ("固", intrinsic), ("词", affix)]
    details = "/".join(f"{name}{value:+d}" for name, value in parts if value)
    total = base + sum(value for _, value in parts)
    return f"{total}({details})" if details else str(total)


def durability_mult(item: Any) -> float:
    """[P45 2026-09-12] 装备耐久比例（0.0-1.0）：durability / durability_max。

    durability_max<=0 表示不朽（不损耗，全额 1.0）；耐久越低装备属性按比例衰减，
    耐久 0 = 装备失效（属性 0）。非装备物品默认满耐久，无影响。
    [!] 不可用 `or default`：合法 durability=0 会被 falsy 吞（守 §11）。
    （自 world_sim_service._durability_mult 提取为纯函数单一来源：服务方法委托本函数，
    NPC 打猎战力聚合同一口径，防两处漂移。）
    """
    try:
        dm = int(getattr(item, "durability_max", 100))
    except (TypeError, ValueError):
        dm = 100
    if dm <= 0:
        return 1.0
    try:
        cur = int(getattr(item, "durability", dm))
    except (TypeError, ValueError):
        cur = dm
    return max(0.0, min(1.0, cur / dm))


def equipped_attack_defense(world: Any, entity: Any) -> tuple[int, int]:
    """[P45 2026-09-12] 已装备武器 attack + 防具 defense 总和（[P7k3] 按耐久比例生效）。

    [P5c] 8 槽天然兼容：遍历 equipped dict 全部 key，按 Item.type 分流。
    - 多把 weapon（main_hand + off_hand）的 attack 自动累加（各按耐久比例）
    - 多件 armor（head/chest/legs/feet）的 defense 自动累加（各按耐久比例）
    - accessory 走 equipped_stat_bonus / _collect_equipped_affixes（这里不计）
    （自 world_sim_service._equipped_attack_defense 提取为纯函数单一来源：玩家/NPC 战斗
    与 NPC 打猎共用，防「服务侧算了装备、引擎侧裸算」的口径分裂。）
    """
    w_atk = 0
    a_def = 0
    equipped = getattr(entity, "equipped", None) or {}
    if not isinstance(equipped, dict):
        return 0, 0
    items = getattr(world, "items", None) or []
    for _slot, iid in equipped.items():
        it = next((i for i in items if getattr(i, "id", "") == iid), None)
        if not it:
            continue
        mult = durability_mult(it)
        if getattr(it, "type", "") == "weapon":
            w_atk += int(int(getattr(it, "attack", 0) or 0) * mult)
        elif getattr(it, "type", "") == "armor":
            a_def += int(int(getattr(it, "defense", 0) or 0) * mult)
    return w_atk, a_def


def compute_stats(
        entity: Any,
        weapon_attack: int = 0,
        armor_defense: int = 0,
        affixes: Optional[list[dict]] = None,
        equip_stat_bonus: Optional[dict] = None,
        hunger_mult: float = 1.0,
) -> CombatSnapshot:
    """聚合 base stats + 装备 + 词缀，得有效战斗属性。

    entity 须有 hp/hp_max/level/stat_str/stat_dex/stat_int/stat_vit/stat_luk 字段
    （PlayerState 或 NPC 均可，鸭子类型）。

    [P5c] 装备属性加成 stat_bonus 在 entity 上聚合（修隐藏 bug：原先此字段完全不生效）。
    [P5c] affixes 双口径兼容：顶层 {"atk":3} 或嵌套 {"mods":{"atk":3}} 均识别。
    [C1 修复 2026-08-25] equip_stat_bonus：已装备物品顶层 stat_bonus 的合计 dict 由调用方
    传入聚合（此前只显示不生效——tooltip 写「灵根+20」战斗端恒丢）。
    [饱食度 2026-09-06] 玩家饥饿惩罚在此单一出口生效（NPC 不罚，仅行为驱动）：
    hunger < 30 时五维 x0.5（hunger_stat_mult）。
    """
    affixes = affixes or []
    level = max(1, _stat(entity, "level", 1))
    # [饱食度 2026-09-06] 玩家饥饿惩罚（五维 x0.5）：hunger_mult 由玩家侧调用点显式传
    # （hunger_stat_mult）；NPC 路径默认 1.0 不罚（NPC 饥饿只驱动行为）。
    s_str = int(_stat(entity, "stat_str", 10) * hunger_mult)
    s_dex = int(_stat(entity, "stat_dex", 10) * hunger_mult)
    s_int = int(_stat(entity, "stat_int", 10) * hunger_mult)   # [P7e] stat_int 进公式
    s_vit = int(_stat(entity, "stat_vit", 10) * hunger_mult)
    s_luk = int(_stat(entity, "stat_luk", 10) * hunger_mult)
    # [P52 伤疤 2026-09-25] 臂伤攻降 / 腿伤闪避速度降（实体自带 injuries；
    # 痊愈由 _tick_daily_regen 按 day 清过期——此处 day=0 即全量生效）
    s_str = int(s_str * injury_stat_mult(entity, "atk"))
    s_dex = int(s_dex * injury_stat_mult(entity, "dodge"))

    # [P5c] 装备属性加成 stat_bonus 生效：把 {"str":2,"dex":1} 聚合进对应属性。
    # 仅当 entity 有 stat_bonus 字段（dict）时生效；NPC 无此字段跳过。
    sb = getattr(entity, "stat_bonus", None)
    if isinstance(sb, dict) and sb:
        s_str += _safe_int_sb(sb, "str")
        s_dex += _safe_int_sb(sb, "dex")
        s_int += _safe_int_sb(sb, "int")     # [P7e] stat_int 也聚合
        s_vit += _safe_int_sb(sb, "vit")
        s_luk += _safe_int_sb(sb, "luk")

    # [C1 修复 2026-08-25] 已装备物品顶层 stat_bonus 聚合（与 entity sb 同口径）
    if isinstance(equip_stat_bonus, dict) and equip_stat_bonus:
        s_str += _safe_int_sb(equip_stat_bonus, "str")
        s_dex += _safe_int_sb(equip_stat_bonus, "dex")
        s_int += _safe_int_sb(equip_stat_bonus, "int")
        s_vit += _safe_int_sb(equip_stat_bonus, "vit")
        s_luk += _safe_int_sb(equip_stat_bonus, "luk")

    # [P9] 天赋 stat_bonus 聚合（雷天灵根/蛮力过人等加属性；te 处理 entity 无 talents 的情况）
    tal_sb = te.get_talent_stat_bonus(entity)
    if tal_sb:
        s_str += int(tal_sb.get("str", 0))
        s_dex += int(tal_sb.get("dex", 0))
        s_int += int(tal_sb.get("int", 0))
        s_vit += int(tal_sb.get("vit", 0))
        s_luk += int(tal_sb.get("luk", 0))

    # [P5c] 词缀加成：兼容顶层 {"atk":3} 和嵌套 {"mods":{"atk":3}} 两种格式。
    aff_atk = sum(_affix_val(a, "atk", int) for a in affixes if isinstance(a, dict))
    aff_def = sum(_affix_val(a, "def", int) for a in affixes if isinstance(a, dict))
    aff_crit = sum(_affix_val(a, "crit", float) for a in affixes if isinstance(a, dict))
    aff_magic = sum(_affix_val(a, "magic_atk", int) for a in affixes if isinstance(a, dict))  # [P7e]
    # [装备新属性 2026-09-01] 概率型词缀直加（基础 0，天赋乘法后钳 0~0.5）
    aff_counter = sum(_affix_val(a, "counter_rate", float) for a in affixes if isinstance(a, dict))
    aff_combo = sum(_affix_val(a, "combo_rate", float) for a in affixes if isinstance(a, dict))
    aff_lifesteal = sum(_affix_val(a, "lifesteal", float) for a in affixes if isinstance(a, dict))
    counter_rate = aff_counter * te.get_talent_mult(entity, "counter_rate")
    combo_rate = aff_combo * te.get_talent_mult(entity, "combo_rate")
    lifesteal_rate = aff_lifesteal * te.get_talent_mult(entity, "lifesteal")
    counter_rate = min(0.5, max(0.0, counter_rate))
    combo_rate = min(0.5, max(0.0, combo_rate))
    lifesteal_rate = min(0.5, max(0.0, lifesteal_rate))
    # [装备新属性] 武器元素词缀：首个非空 element 值并入快照 elements（普攻吃克制矩阵）
    _w_elements = [str(v).strip() for v in
                   (_affix_str(a, "element") for a in affixes if isinstance(a, dict))
                   if v and v != "physical"]
    # [P34b] 词缀 stat_bonus 字典聚合（洗练 stat 维度词条：随机一维属性加值；
    # 顶层与 mods 嵌套双口径，与 _affix_val 同结构——漏读会让 stat 词条只显示不生效）
    for a in affixes:
        if not isinstance(a, dict):
            continue
        sb_a = a.get("stat_bonus")
        if not isinstance(sb_a, dict):
            mods_a = a.get("mods")
            sb_a = mods_a.get("stat_bonus") if isinstance(mods_a, dict) else None
        if isinstance(sb_a, dict) and sb_a:
            s_str += _safe_int_sb(sb_a, "str")
            s_dex += _safe_int_sb(sb_a, "dex")
            s_int += _safe_int_sb(sb_a, "int")
            s_vit += _safe_int_sb(sb_a, "vit")
            s_luk += _safe_int_sb(sb_a, "luk")

    atk = s_str * 2 + weapon_attack + aff_atk
    # [P9] 天赋 atk_mult（神枪手/纯阳之体等普攻加成乘进 atk）
    atk = int(atk * te.get_talent_mult(entity, "atk_mult"))
    def_ = s_vit + armor_defense + aff_def
    magic_atk = s_int * 2 + aff_magic        # [P7e] 法术攻击（技能/法术伤害基础）
    # [P8] crit_rate 纳入 luk（幸运影响暴击，不只是掉落）：dex 主导 + luk 副导
    crit_rate = 0.05 + s_dex * 0.005 + s_luk * 0.002 + aff_crit
    crit_dmg = 1.5 + s_str * 0.02
    hp_max = max_hp_for(entity)
    speed = s_dex + s_luk // 2
    dodge = min(0.4, max(0.0, s_dex * 0.004))   # [P7e] 闪避率
    parry = min(0.3, max(0.0, s_str * 0.003))   # [P7e] 招架率
    heal_bonus = int(s_int * 0.5)               # [P7e] 治疗加成

    hp = _stat(entity, "hp", hp_max)
    if hp_max > 0:
        hp = min(hp, hp_max)

    # [P9] 元素构成聚合（entity 无 elements 字段——如 PlayerState——得空 tuple，不吃克制）。
    # [装备新属性 2026-09-01] 武器元素词缀并入（去重保序；玩家武器附火 -> 普攻也吃克制矩阵）
    _els = tuple(str(e) for e in (getattr(entity, "elements", None) or [])
                 if str(e or "").strip())
    if _w_elements:
        _els = tuple(dict.fromkeys(_els + tuple(_w_elements[:2])))

    return CombatSnapshot(
        hp=hp, hp_max=hp_max, atk=max(0, atk), def_=max(0, def_),
        magic_atk=max(0, magic_atk),
        crit_rate=min(0.95, max(0.0, crit_rate)),
        crit_dmg=crit_dmg, speed=max(0, speed), level=level,
        dodge=dodge, parry=parry, heal_bonus=heal_bonus, luck=max(0, s_luk),
        incoming_crit_bonus=min(0.95, head_extra_crit_rate(entity)),
        heal_received_mult=injury_stat_mult(entity, "heal"),
        elements=_els,
        counter_rate=counter_rate, combo_rate=combo_rate,
        lifesteal_rate=lifesteal_rate,
    )


def max_hp_for(entity: Any) -> int:
    """[P5c] 权威 HP 上限公式：(基础耐力 + 天赋耐力) * 10 + level * 5。

    抽出此 helper 消除 6 处复制（compute_stats / _init_combat_stats 2 处 /
    _resolve_combat 升级重算 / _resolve_use_item 重算 / InventoryDialog use_item 重算）。
    修改 HP 公式时只改这一处。
    """
    s_vit = te.effective_stat(entity, "vit")
    level = max(1, _stat(entity, "level", 1))
    return s_vit * 10 + level * 5


def sync_player_talent_resources(player: Any) -> bool:
    """天赋觉醒/旧存档补齐新增长的 HP、MP 上限，保留原有伤势。"""
    bonus = te.get_talent_stat_bonus(player)
    if not (bonus.get("vit") or bonus.get("int")
            or te.get_talent_bonus(player, "mp_bonus")):
        return False
    changed = False
    hp_max = max_hp_for(player)
    old_hp_max = max(0, _stat(player, "hp_max", 0))
    if old_hp_max > 0 and hp_max > old_hp_max:
        old_hp = _stat(player, "hp", 0)
        player.hp_max = hp_max
        if old_hp > 0:
            player.hp = min(hp_max, old_hp + hp_max - old_hp_max)
        changed = True
    mp_max = max(0, te.effective_stat(player, "int") * 5
                 + max(1, _stat(player, "level", 1)) * 3
                 + te.get_talent_bonus(player, "mp_bonus"))
    old_mp_max = max(0, _stat(player, "mp_max", 0))
    if old_mp_max > 0 and mp_max > old_mp_max:
        old_mp = _stat(player, "mp", 0)
        player.mp_max = mp_max
        if old_mp > 0:
            player.mp = min(mp_max, old_mp + mp_max - old_mp_max)
        changed = True
    return changed


def _safe_int_sb(sb: dict, key: str) -> int:
    """[P5c] 安全读 stat_bonus dict 里的 int 值（防脏数据）。"""
    try:
        return int(sb.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _affix_val(affix: dict, key: str, cast_type: type) -> int | float:
    """[P5c] 读 affix 的数值字段，兼容顶层和 mods 嵌套两种口径。

    - 顶层口径: {"atk": 3, "def": 1, "crit": 0.1}
    - 嵌套口径: {"slot": "prefix", "name": "锋利", "mods": {"atk": 3}}
    两种都识别，统一返回数值。
    """
    # 顶层优先
    if key in affix:
        v = affix.get(key)
    else:
        mods = affix.get("mods")
        v = mods.get(key) if isinstance(mods, dict) else None
    if v is None:
        return 0
    try:
        return cast_type(v)
    except (TypeError, ValueError):
        return 0


def _affix_str(affix: dict, key: str) -> str:
    """[装备新属性 2026-09-01] 读 affix 的字符串字段（element 词缀等），双口径同 _affix_val。"""
    if key in affix:
        v = affix.get(key)
    else:
        mods = affix.get("mods")
        v = mods.get(key) if isinstance(mods, dict) else None
    return str(v).strip() if v is not None else ""


def resolve_attack(
        attacker: CombatSnapshot,
        defender: CombatSnapshot,
        rng: SeededRng,
        difficulty: str = "normal",
        granularity: str = "medium",
        is_player_attacker: bool = True,
        attacker_conds: Optional[list] = None,
        defender_conds: Optional[list] = None,
        morale_mult: float = 1.0,
        attacker_morale: int = _MORALE_BASE,
) -> AttackResult:
    """结算一次攻击：命中 -> 暴击 -> 伤害 -> 钳制。

    is_player_attacker=True 时套用难度系数（玩家伤害/受击调整）。
    granularity=light 必中无暴击；medium 标准；heavy 加抗性减免（def 减伤比例提升）。
    [P30] attacker_conds/defender_conds：状态减益/增益算入结算（enraged 攻防、shocked/chilled
    速度与闪避、blinded 命中、frozen/protected 受击伤害、entangled 速度）。默认空=不影响。
    """
    diff = _DIFF_MULT.get(difficulty, _DIFF_MULT["normal"])
    player_mult = diff["player_atk"] if is_player_attacker else diff["player_def"]
    ac = attacker_conds or []
    dc = defender_conds or []
    # [P30] 状态乘数（enraged 攻 x1.3、shocked/chilled/entangled 速度减、blinded 命中减）
    a_speed = max(0, int(attacker.speed)) * _cond_mult(ac, "speed_mult", 1.0)
    d_speed = max(0, int(defender.speed)) * _cond_mult(dc, "speed_mult", 1.0)
    a_atk = max(0, int(attacker.atk)) * _cond_mult(ac, "atk_mult", 1.0)
    d_def = max(0, int(defender.def_)) * _cond_mult(dc, "def_mult", 1.0)
    d_dodge = max(0.0, float(defender.dodge)) * _cond_mult(dc, "dodge_mult", 1.0)

    # 命中判定（light 必中）
    if granularity == "light":
        hit = True
    else:
        hit_chance = 0.9 + (a_speed - d_speed) * 0.02
        hit_chance = max(0.3, min(0.99, hit_chance))
        # [P7e] 防御者闪避抵消：dodge 越高命中越低（stat_dex 驱动）
        hit_chance = hit_chance * (1.0 - d_dodge)
        # [P30] 攻击者 blinded 命中减
        hit_chance = hit_chance * _cond_mult(ac, "hit_mult", 1.0)
        hit_chance = max(0.05, min(0.99, hit_chance))
        hit = rng.chance(hit_chance)

    if not hit:
        return AttackResult(hit=False, crit=False, damage=0,
                            defender_hp_after=defender.hp, defender_defeated=False)

    # 暴击判定（light 无暴击）[P52] 防御者头伤 -> 受暴击率 +0.05/处
    # [P51 战意暴击钩 2026-09-26] 攻击方战意高昂（>=80）-> 暴击率 +0.05（士气高涨刀刀要害；
    # 默认 _MORALE_BASE=60 恒 0，未接线调用零位移）
    crit = False
    if granularity != "light":
        crit = rng.chance(attacker.crit_rate
                          + (_MORALE_CRIT_BONUS if _morale_tier(attacker_morale) == "high" else 0.0)
                          + defender.incoming_crit_bonus)

    # 伤害公式
    base = a_atk
    if crit:
        base = int(base * attacker.crit_dmg)
    def_reduction = d_def * 0.5
    if granularity == "heavy":
        def_reduction = d_def * 0.7  # heavy 加抗性减免
    dmg = max(1, int((base - def_reduction) * player_mult * morale_mult))
    # player_mult 已据 is_player_attacker 取系数：玩家攻击用 player_atk（easy 加成），
    # NPC 攻玩家用 player_def（easy 减伤），无需此处再分支
    # [装备新属性 修 2026-09-10] 武器元素词缀参与普攻：攻击方元素 vs 守方有效元素吃克制矩阵。
    # 原实现只有技能两路读 skill["element"]，快照 elements 无人消费 -> 武器元素是死属性。
    # 与技能路径同源 element_damage_mult；攻击方无元素/守方无元素 = 1.0（原有行为不变）。
    # 纯查表零 rng 消费——插入不漂移既有判定序列。
    _atk_elem = next((str(e).strip() for e in (getattr(attacker, "elements", ()) or ())
                      if str(e).strip()), "")
    if _atk_elem and dmg > 0:
        _elem_mult, _ = element_damage_mult(_atk_elem, _effective_elements(defender, dc))
        dmg = max(1, int(dmg * _elem_mult))

    # [P7e] 招架判定（物理）：防御者 roll parry（stat_str 驱动），成功则物理伤害减半
    if granularity != "light" and dmg > 0:
        if rng.chance(defender.parry):
            dmg = max(1, int(dmg * 0.5))

    # [P30] 受击伤害乘数（frozen 易碎 x1.5、protected 减伤 x0.5）
    if dmg > 0:
        dmg = max(1, int(dmg * _cond_mult(dc, "dmg_taken_mult", 1.0)))

    # [部位 2026-08-31] roll 部位骰 + 弱点判定（弱点来自 defender.weakpoint 快照字段或
    # 主敌包装单位——resolve_attack 无 session 参，快照上由调用方注入 weakpoint）。
    # 倍率后于状态乘数（招架/状态折减先算再乘部位——「打在哪」是最后一层修正）。
    part = roll_body_part(rng)
    weak = str(getattr(defender, "weakpoint", "") or "")
    dmg, is_weak, wp_crit = apply_body_part(rng, dmg, part, weak,
                                            attacker.crit_rate + defender.incoming_crit_bonus,
                                            attacker.crit_dmg)
    if wp_crit:
        crit = True

    # [伤害浮动] 命中后 ±10% 抖动（破「非暴击伤害完全可预测」）。light 模式豁免保零消费。
    if granularity != "light" and dmg > 0:
        dmg = max(1, int(dmg * (rng.random() * 0.2 + 0.9)))

    hp_after = max(0, defender.hp - dmg)
    return AttackResult(
        hit=True, crit=crit, damage=dmg,
        defender_hp_after=hp_after, defender_defeated=(hp_after <= 0),
        body_part=part, weakpoint=is_weak,
    )


def part_log(r) -> str:
    """[部位 2026-08-31] 普攻日志的部位后缀（部位名 + 弱点标记）。"""
    part = str(getattr(r, "body_part", "") or "")
    if not part:
        return ""
    return f"（部位：{part_display(part)}" + ("·弱点！" if getattr(r, "weakpoint", False) else "") + "）"


# ---- [对撞 2026-09-01] 防御骰子对撞系统 ----
# 触发：防御中的单位被普攻命中时，攻守双方各掷 5d6 对撞（技能/借势绕过，同 Boss 护盾口径）。
# 判定：牌型高者胜（豹子>五连顺>四同>葫芦>三同>两对>一对>散牌）；同级比五骰总和；再平攻方胜。
# 后果：攻方胜=破防（全额伤害 + 部位固定躯干）；守方胜=守住（x0.25 + 回 2% max MP）；
# 攻方豹子=破防 + x1.25；守方豹子=完全格挡 + 反震攻方 10% 守方 max HP（钳 1）。
CLASH_HAND_RANK: dict[str, int] = {
    "five": 8, "straight": 7, "four": 6, "full": 5,
    "three": 4, "two_pair": 3, "pair": 2, "high": 1,
}
CLASH_HAND_ZH: dict[str, str] = {
    "five": "豹子！", "straight": "五连顺！", "four": "四同！",
    "full": "葫芦", "three": "三同", "two_pair": "两对",
    "pair": "一对", "high": "散牌",
}
_CLASH_DEFEND_MULT = 0.25    # 守方胜减伤系数
_CLASH_FIVE_MULT = 1.25      # 攻方豹子加成
_CLASH_MP_RECOVER = 0.02     # 守方胜回 MP 比例（max MP）
_CLASH_SHOCK_PCT = 0.10      # 守方豹子反震比例（守方 max HP）


def roll_clash_dice(rng: SeededRng) -> list:
    """掷一方的 5 骰（d6；确定性）。"""
    return [rng.roll(1, 6) for _ in range(5)]


def clash_hand(dice: list) -> "tuple[str, int]":
    """[对撞] 5 骰 -> (牌型 key, 五骰总和)。纯函数无 rng。

    豹子=five 五同；straight=五连号（1-5 / 2-6）；four=四同；full=葫芦（三+对）；
    three=三同；two_pair=两对；pair=一对；high=散牌。
    """
    dice = sorted(int(d) for d in (dice or [])[:5])
    if len(dice) < 5:
        return "high", sum(dice)
    total = sum(dice)
    # 计数分布 -> 降序（如五同 [5]，葫芦 [3,2]，两对 [2,2,1]）
    counts = sorted((dice.count(d) for d in set(dice)), reverse=True)
    uniq = sorted(set(dice))
    if counts[0] == 5:
        return "five", total
    if counts[0] == 4:
        return "four", total
    if counts[0] == 3 and counts[1] == 2:
        return "full", total
    if counts[0] == 3:
        return "three", total
    if counts[0] == 2 and counts[1] == 2:
        return "two_pair", total
    if counts[0] == 2:
        return "pair", total
    # 全单张：五连号判定（uniq 恰 5 个且连续）
    if len(uniq) == 5 and uniq[4] - uniq[0] == 4:
        return "straight", total
    return "high", total


def clash_decide(a_dice: list, d_dice: list) -> "tuple[str, str, str]":
    """[对撞] 比牌定胜负。返回 (结果 key, 攻方牌型, 守方牌型)。

    结果 key："attacker"（攻方胜·破防） / "defender"（守方胜·守住） /
    "attacker_five"（攻方豹子·破防加成） / "defender_five"（守方豹子·完全格挡）。
    平局（同牌型同总和）判攻方胜——守方需明确赢下对撞才有减伤。
    """
    a_hand, a_sum = clash_hand(a_dice)
    d_hand, d_sum = clash_hand(d_dice)
    a_rank = CLASH_HAND_RANK.get(a_hand, 1)
    d_rank = CLASH_HAND_RANK.get(d_hand, 1)
    if a_rank > d_rank or (a_rank == d_rank and a_sum >= d_sum):
        return ("attacker_five" if a_hand == "five" else "attacker"), a_hand, d_hand
    return ("defender_five" if d_hand == "five" else "defender"), a_hand, d_hand


def _dice_text(dice: list) -> str:
    return "-".join(str(int(d)) for d in (dice or [])[:5])


def clash_result_log(a_dice: list, d_dice: list, outcome: str,
                     a_name: str, d_name: str) -> str:
    """[对撞] 结果日志后缀（ Paren 风格同 part_log；败方名字可省）。"""
    a_hand, a_sum = clash_hand(a_dice)
    d_hand, d_sum = clash_hand(d_dice)
    tail = {
        "attacker": "破防！",
        "attacker_five": "破防！豹子加成 x1.25",
        "defender": "守方胜·减伤75%",
        "defender_five": "守方豹子·完全格挡！",
    }.get(outcome, "")
    return (f"（对撞：{a_name}【{_dice_text(a_dice)}·{CLASH_HAND_ZH.get(a_hand)}】{a_sum}"
            f" vs {d_name}【{_dice_text(d_dice)}·{CLASH_HAND_ZH.get(d_hand)}】{d_sum}"
            f" -> {tail}）")


def resolve_defended_attack(session: "CombatSession", r, rng: SeededRng,
                            attacker_snap: CombatSnapshot,
                            defender_snap: CombatSnapshot,
                            defender_is_player: bool,
                            a_name: str = "攻方", d_name: str = "守方",
                            defender_unit=None):
    """[对撞 2026-09-01] 防御中被普攻命中的对撞结算（替代旧 dmg x0.5 硬编码）。

    调用时机：普攻已 resolve_attack 出结果 r（hit=True）后。light 粒度豁免对撞
    （回退旧行为 x0.5 减半——保测试桩断言口径，不消费 rng）。
    返回 (折算后伤害, 日志后缀)。副作用：守方胜回 MP / 守方豹子反震写攻方 HP。
    [!] rng 经济：10 骰仅在本函数消费（即守方防御且被命中时）；非防御态零消费。
    [修 2026-09-10] 新增 defender_unit：守方是随从/同伴单位时传其 CombatUnit——
    单位 mp 挂单位上（快照无 mp 字段），不传则守方胜的回 MP 对该侧不生效。
    """
    if session.granularity == "light":
        dmg = max(1, int(r.damage * 0.5))
        return dmg, "（举盾减半）"
    a_dice = roll_clash_dice(rng)
    d_dice = roll_clash_dice(rng)
    outcome, a_hand, d_hand = clash_decide(a_dice, d_dice)
    log = clash_result_log(a_dice, d_dice, outcome, a_name, d_name)
    if outcome == "defender":
        defend_mp_recover(session, defender_snap, defender_is_player, defender_unit)
        return max(1, int(r.damage * _CLASH_DEFEND_MULT)), log
    if outcome == "defender_five":
        # 完全格挡：0 伤害 + 反震攻方 10% 守方 max HP
        shock = max(1, int(defender_snap.hp_max * _CLASH_SHOCK_PCT))
        attacker_snap.hp = max(0, attacker_snap.hp - shock)
        log += f"（{d_name}反震，{a_name}受 {shock} 点伤害）"
        return 0, log
    # 攻方胜（含豹子）：全额伤害
    dmg = r.damage
    if outcome == "attacker_five":
        dmg = max(1, int(dmg * _CLASH_FIVE_MULT))
    return dmg, log


def defend_mp_recover(session: "CombatSession", defender_snap: CombatSnapshot,
                      defender_is_player: bool, defender_unit=None) -> None:
    """[对撞] 守方胜的 2% max MP 回复（钳 max）。

    [修 2026-09-10] 原实现读 `defender_snap.mp_max`——而玩家/主敌都是 bare CombatSnapshot
    （无 mp/mp_max 字段），`mp_max` 恒 0 -> 整个函数空转，契约里「守住 = x0.25 + 回 2%
    max MP」的奖励**从未发放过**（与「武器元素死属性」同类病；旧测试只断言了不回复那一侧）。
    现在按守方身份分派上限与回写目标：
    - 玩家 -> session.player_mp / player_mp_max
    - 主敌 -> session.enemy_mp / enemy_mp_max（两者均由 start_combat 注入）
    - 随从/同伴单位 -> 单位自身的 mp/mp_max（mp 本就挂在 CombatUnit 上）
    - legacy 快照自带 mp_max 时仍按其回写（测试桩/未来包装快照）
    [!] [审查修复] 主敌判据必须**先于** defender_unit：随从/同伴路径会无条件把 tgt 传进来，
    而 tgt 常常就是主敌包装单位（enemy_units[0]）——若先走 unit 分支，回蓝会写进
    main_unit.mp 这个「从未回写 session」的副本（奖励再次丢失）。
    """
    if defender_is_player:
        mp_max = max(0, int(getattr(session, "player_mp_max", 0) or 0))
        if mp_max > 0:
            gain = max(1, int(mp_max * _CLASH_MP_RECOVER))
            session.player_mp = min(mp_max, int(session.player_mp or 0) + gain)
        return
    if defender_snap is getattr(session, "enemy", None):
        mp_max = max(0, int(getattr(session, "enemy_mp_max", 0) or 0))
        if mp_max > 0:
            gain = max(1, int(mp_max * _CLASH_MP_RECOVER))
            session.enemy_mp = min(mp_max, int(session.enemy_mp or 0) + gain)
        return
    if defender_unit is not None:
        mp_max = max(0, int(getattr(defender_unit, "mp_max", 0) or 0))
        if mp_max > 0:
            gain = max(1, int(mp_max * _CLASH_MP_RECOVER))
            defender_unit.mp = min(mp_max, int(getattr(defender_unit, "mp", 0) or 0) + gain)
        return
    mp_max = max(0, int(getattr(defender_snap, "mp_max", 0) or 0))
    if mp_max > 0:
        gain = max(1, int(mp_max * _CLASH_MP_RECOVER))
        cur = int(getattr(defender_snap, "mp", 0) or 0)
        defender_snap.mp = min(mp_max, cur + gain)


def c_dmg_guard_note(clash_txt: str) -> bool:
    """[对撞] 对撞文本是否为「完全格挡」结局（守方豹子）。"""
    return "完全格挡" in (clash_txt or "")


# ---- [装备新属性 2026-09-01] 普攻后处理：连击/反击/吸血（全走 SeededRng，概率 0 零消费）----
_COMBO_RATE = "combo_rate"      # 普攻命中后概率同伤害追击
_COUNTER_RATE = "counter_rate"  # 被普攻命中后概率反伤 30% 本次伤害
_LIFESTEAL_RATE = "lifesteal_rate"  # 造成伤害后按比例回自身 HP
_COUNTER_PCT = 0.3              # 反击反伤比例


def apply_combo(session: "CombatSession", attacker_snap: CombatSnapshot,
                defender_snap: CombatSnapshot, dmg: int, rng: SeededRng,
                defender_name: str) -> "tuple[int, str]":
    """[连击] 普攻命中后 roll combo_rate：成功 -> 同伤害追击一次（同部位，不重掷）。

    返回 (追加伤害, 日志后缀)；未触发返回 (0, "")。追击伤害直接扣血（与本次独立）。
    [!] light 粒度豁免（保测试口径，零 rng 消费）。
    """
    if session.granularity == "light":
        return 0, ""
    rate = max(0.0, min(0.5, float(getattr(attacker_snap, "combo_rate", 0.0) or 0.0)))
    if rate <= 0.0 or dmg <= 0 or defender_snap.hp <= 0:
        return 0, ""
    if not rng.chance(rate):
        return 0, ""
    defender_snap.hp = max(0, defender_snap.hp - dmg)
    return dmg, f"（连击！追击 {dmg}）"


def apply_counter(counter_snap: CombatSnapshot, attacker_snap: CombatSnapshot,
                  dmg: int, rng: SeededRng,
                  counter_name: str, attacker_name: str) -> str:
    """[反击] 被普攻命中且存活 -> roll counter_rate：成功 -> 反伤 30% 本次伤害。

    返回日志后缀（未触发空串）。counter_snap=被击中方快照（反伤出手方）。
    [!] 概率 0 零消费；存活才反击（倒下不反击）。
    """
    rate = max(0.0, min(0.5, float(getattr(counter_snap, "counter_rate", 0.0) or 0.0)))
    if rate <= 0.0 or dmg <= 0 or counter_snap.hp <= 0:
        return ""
    if not rng.chance(rate):
        return ""
    c_dmg = max(1, int(dmg * _COUNTER_PCT))
    attacker_snap.hp = max(0, attacker_snap.hp - c_dmg)
    return f"（{counter_name}反击{attacker_name}，造成 {c_dmg} 点伤害）"


def apply_lifesteal(attacker_snap: CombatSnapshot, dmg: int) -> str:
    """[吸血] 造成伤害后按 lifesteal_rate 回自身 HP（无 roll，纯比例）。返回日志后缀。"""
    rate = max(0.0, min(0.5, float(getattr(attacker_snap, "lifesteal_rate", 0.0) or 0.0)))
    if rate <= 0.0 or dmg <= 0:
        return ""
    gain = max(1, int(dmg * rate))
    if attacker_snap.hp >= attacker_snap.hp_max:
        return ""
    before = attacker_snap.hp
    heal(attacker_snap, gain)
    return f"（吸血+{attacker_snap.hp - before}）"


def apply_basic_attack_post(session: "CombatSession", attacker_snap: CombatSnapshot,
                            defender_snap: CombatSnapshot, dmg: int, rng: SeededRng,
                            attacker_name: str, defender_name: str) -> str:
    """[修 2026-09-10] 普攻后处理三件套的**单一来源**：守方反击 -> 攻方连击 -> 攻方吸血。

    四路普攻（玩家 / 同伴 / 随从 / 主敌）共用同一实现。原实现只在「玩家普攻」接了
    连击+吸血、「主敌普攻」接了吸血、「玩家被主敌普攻」接了玩家反击——同伴与随从的词缀
    装备（A1 NPC 装备自主化）全是面板装饰，且四路各抄一份正是契约警告的「只接一处必漏」。
    [!] 顺序固定（保同 seed 可回放）：反击（守方，需存活）-> 连击（攻方，需守方存活）
    -> 吸血（攻方，纯比例无 roll）。
    [!] 概率 0 零 rng 消费（apply_* 内部 chance 短路），无词缀单位的行为与旧版逐字节一致。
    [!] light 粒度下 apply_combo 自豁免（保测试桩口径）。
    [!] [审查修复] 守方反击可能反杀攻方——攻方已死时不再连击/吸血，否则
    `apply_lifesteal` 会把 hp=0 的死者按比例回血「复活」（hp>0 即 alive）。
    """
    log = ""
    if dmg > 0 and int(getattr(defender_snap, "hp", 0) or 0) > 0:
        log += apply_counter(defender_snap, attacker_snap, dmg, rng,
                             defender_name, attacker_name)
    attacker_alive = int(getattr(attacker_snap, "hp", 0) or 0) > 0
    if attacker_alive:
        c_add, combo_txt = apply_combo(session, attacker_snap, defender_snap, dmg, rng,
                                       defender_name)
        if c_add:
            log += combo_txt
        if dmg > 0:
            log += apply_lifesteal(attacker_snap, dmg)
    return log


def apply_boss_shield(session: "CombatSession", hp_before: int, damage: int) -> "tuple[int, bool]":
    """[Boss 机制库] 护盾：主敌带盾时【玩家普攻】承伤 x0.3，盾按未折减伤害衰减。

    [设计口径] 只挂普攻路径（技能/同伴/环境借势绕过护盾——鼓励技能流破盾打法；
    技能/同伴结算函数无 session 参，接线成本高于收益）。UI 提示带「普」标注。

    返回 (折减后 hp, 盾是否在本击破碎)。无盾机制/盾已碎返回 (hp_before - damage, False)。
    """
    if session.boss_shield <= 0 or "shield" not in (session.boss_mechanics or []):
        return max(0, hp_before - damage), False
    if damage <= 0:
        return hp_before, False      # [审查修复] 未命中零伤：不扣血不掉盾
    reduced = max(0, hp_before - max(1, int(damage * 0.3)))
    session.boss_shield = max(0, session.boss_shield - max(1, damage))
    broken = session.boss_shield <= 0
    return reduced, broken


def _assign_summon_slot(session: "CombatSession", unit: "CombatUnit") -> None:
    """[修 2026-09-05] 战斗中途召唤的单位分配槽位（原实现不设——默认 slot 0 与既有
    单位同位重叠，UI 立绘按 slot 摆位会叠在一起）。确定性无 rng：优先空槽
    （前排 0/1 先，后排 2/3/4 后）；全被占用（含尸体槽也算）则回落 slot 0
    （5 槽全满才允许同槽叠加，召唤侧已用 _summon_has_free_slot 守住拒召，
    此处保留兜底兼容老调用方）。row 随 slot 联动（前排门控读）。

    [修 2026-09-08 真机] 槽占用按"该槽上有任何单位（死活不论）"计，避免新单位跟
    尸体叠在同位造成视觉诈尸。召唤走 _check_boss_phase/_boss_mechanics_tick 前
    会先 _summon_has_free_slot 预检，5 槽全满时直接拒召、不走到本函数。
    """
    used_slots = {int(getattr(u, "slot", 0) or 0)
                  for u in (session.enemy_units or [])}
    for slot in (0, 1, 2, 3, 4):
        if slot not in used_slots:
            unit.slot = slot
            unit.row = 1 if slot in (0, 1) else 2
            return
    unit.slot = 0
    unit.row = 1


def _boss_minion(session: "CombatSession") -> "CombatUnit":
    """[Boss 机制库] 召唤潮爪牙：主敌快照 5 成克隆（纯引擎，无 svc 依赖）。"""
    e = session.enemy
    from copy import deepcopy
    snap = deepcopy(e)
    snap.hp = max(1, int((getattr(snap, "hp_max", 1) or 1) * 0.5))
    snap.hp_max = max(1, int((getattr(snap, "hp_max", 1) or 1) * 0.5))
    snap.atk = max(1, int((getattr(snap, "atk", 1) or 1) * 0.5))
    return CombatUnit(name="潮涌爪牙", snapshot=snap, mp=0, mp_max=0,
                      skills=[], cd={}, talents=[], ai="aggressive",
                      role_label="爪牙", is_minion=True)


def _boss_name(session: "CombatSession") -> str:
    units = session.enemy_units or []
    return getattr(units[0], "name", "敌人") if units else "敌人"


def boss_mechanics_tick(session: "CombatSession") -> list:
    """[Boss 机制库] 敌方回合末结算：召唤潮（每 3 回合 1 只 cap2）+ 狂暴计时（atk x1.5）。

    返回日志行列表（调用方并入回合日志）。transient 全程不落盘。
    """
    out: list = []
    if session.state != "active":
        return out
    mechs = session.boss_mechanics or []
    if "summon_tide" in mechs and session.enemy.hp > 0:
        alive_minions = [u for u in (session.enemy_units or [])[1:] if u.alive]
        # [修 2026-09-10] 补空槽预检（与 _check_boss_phase 同口径）：_assign_summon_slot 的
        # docstring 声称两处调用方都预检，实际这里只判存活数——尸体累积占满 5 槽后会落回
        # slot 0 与尸体叠位（视觉诈尸）。
        if (session.round > 0 and session.round % 3 == 0 and len(alive_minions) < 2
                and _summon_has_free_slot(session)):
            unit = _boss_minion(session)
            _assign_summon_slot(session, unit)
            session.enemy_units = list(session.enemy_units or []) + [unit]
            out.append(f"{_boss_name(session)}的潮涌爪牙加入战局！")
    # [修 2026-09-05] 主敌已倒下不播狂暴（召唤潮有 hp>0 守卫，此处漏了：主敌被击败
    # 但随从存活 -> 尸体每回合照发「进入狂暴！」日志）。
    if ("enrage_timer" in mechs and session.boss_enrage_round > 0
            and session.enemy.hp > 0
            and not session.boss_enrage_active and session.round >= session.boss_enrage_round):
        session.boss_enrage_active = True
        session.enemy.atk = max(1, int((getattr(session.enemy, "atk", 1) or 1) * 1.5))
        # [修 2026-09-10] 措辞避开「攻击」二字：UI _enemy_action_anims 以 `"攻击" in line`
        # 判定普攻出手，狂暴行含「攻击」会被误判成普攻多播一次冲刺（契约：蓄势/狂暴原地抖动）。
        out.append(f"{_boss_name(session)}进入狂暴！攻势大幅提升")
    return out


def apply_damage(entity: Any, amount: int) -> int:
    """原地扣 HP（钳制 [0, hp_max]），返回扣后 HP。"""
    amount = max(0, int(amount))
    hp = max(0, _stat(entity, "hp", 0) - amount)
    hp_max = _stat(entity, "hp_max", 0)
    if hp_max > 0:
        hp = min(hp, hp_max)
    try:
        entity.hp = hp
    except (AttributeError, TypeError):
        pass
    return hp


def heal(entity: Any, amount: int, bonus: int = 0) -> int:
    """原地回 HP（钳制 [0, hp_max]），返回回后 HP。

    [P7e] bonus 为治疗加成（如 stat_int 驱动的 heal_bonus），叠加到回血量。
    """
    received_mult = (entity.heal_received_mult if isinstance(entity, CombatSnapshot)
                     else injury_stat_mult(entity, "heal"))
    base_amount = max(0, int(amount) + max(0, int(bonus)))
    amount = max(1, int(base_amount * received_mult)) if base_amount and received_mult > 0 else 0
    hp = _stat(entity, "hp", 0) + amount
    hp_max = _stat(entity, "hp_max", 0)
    if hp_max > 0:
        hp = min(hp, hp_max)
    try:
        entity.hp = hp
    except (AttributeError, TypeError):
        pass
    return hp


def item_heal_value(item: Any, entity: Any) -> int:
    """[百分比回血 2026-09-01] 消耗品回血值单一来源：heal_pct 优先（最大生命百分比），
    旧档 heal_amount 兼容归一化。

    - heal_pct > 0：回 entity 最大生命的 pct%（用 max_hp_for 口径取上限，快照/实体皆可）。
    - 否则 heal_amount：旧整数口径原样（无归一化数据时兜底）。
    - [!] 归一化（_normalize_heal_pct in world_sim_service）只在物品收编时做一次：
      老药 heal_amount=30 且生成档基准血 250 -> 写 heal_pct=12。此处不重复归一化，
      未被收编路径处理的遗留药按整数回（安全降级，不崩）。
    返回回血量（int，调用方再走 heal() 加成/钳制）。
    """
    pct = max(0, min(100, int(getattr(item, "heal_pct", 0) or 0)))
    if pct > 0:
        hp_max = _stat(entity, "hp_max", 0)
        if hp_max <= 0:
            hp_max = max_hp_for(entity)
        return max(1, int(hp_max * pct / 100))
    return max(0, int(getattr(item, "heal_amount", 0) or 0))


def combat_item_usable(item: Any) -> bool:
    """Only effects that can be applied to a transient combat session may spend a turn."""
    if getattr(item, "teach_skill", None):
        return False
    eff = getattr(item, "consume_effect", None)
    if isinstance(eff, dict) and eff:
        kind = str(eff.get("type", "") or "")
        if kind in ("heal_mp", "cure"):
            return True
        if kind == "heal_full":
            return bool(int(eff.get("amount", 0) or 0) > 0 or
                        int(getattr(item, "heal_pct", 0) or 0) > 0 or
                        int(getattr(item, "heal_amount", 0) or 0) > 0)
        return False
    return bool(int(getattr(item, "heal_pct", 0) or 0) > 0 or
                int(getattr(item, "heal_amount", 0) or 0) > 0)


# 玩家五维 stat 字段名 -> 属性 key（consume_effect.stats 用）
_CONSUME_STAT_FIELD = {"str": "stat_str", "dex": "stat_dex", "int": "stat_int",
                       "vit": "stat_vit", "luk": "stat_luk"}
_CONSUME_STAT_ZH = {"str": "力", "dex": "敏", "int": "智", "vit": "耐", "luk": "运"}


def apply_consume_effect(player: Any, item: Any,
                         equip_stat_bonus: Optional[dict] = None) -> "tuple[bool, str]":
    """[消耗品结构化效果] 结算 item.consume_effect 到 player，返回 (是否生效, 提示)。

    纯 Python 无 LLM。type 白名单见 models.world._CONSUME_EFFECT_TYPES：
    - stat_bonus：player.stat_xxx += stats 值（永久），hp_max 重算。
    - heal_full：回复最大生命的 pct%——[用户指示 2026-09-06] 物品恢复全对齐百分比，
      amount 字段 = 百分数 1-100（旧档绝对值语义废除，amount=12 现按 12% 结算）；
      缺省回退 item_heal_value（heal_pct 纯回血口径）。
    - heal_mp：回复最大灵力的 pct%（amount 同为百分数 1-100）。
    - cure/revive：战斗外没有目标，不消耗物品。
    无 consume_effect 或 type 空 -> (False, "") 由调用方走旧 heal_amount 回退。
    """
    eff = getattr(item, "consume_effect", None)
    if not isinstance(eff, dict) or not eff:
        return False, ""
    etype = str(eff.get("type", "") or "")
    name = getattr(item, "name", "") or "物品"
    if etype == "stat_bonus":
        stats = eff.get("stats") or {}
        gained = []
        for k, field in _CONSUME_STAT_FIELD.items():
            v = int(stats.get(k, 0) or 0)
            if v:
                try:
                    setattr(player, field, int(getattr(player, field, 0)) + v)
                    gained.append(f"{_CONSUME_STAT_ZH.get(k, k)}+{v}")
                except (AttributeError, TypeError):
                    pass
        if not gained:
            return False, ""
        try:
            player.hp_max = max_hp_for(player)
        except (AttributeError, TypeError):
            pass
        return True, f"服用{name}，{' '.join(gained)}（永久）"
    if etype == "heal_full":
        # [heal_full 并入 heal_pct 2026-09-10] 旧别名：正常流程中 _normalize_heal_pct
        # 已在收编/读档时迁入 heal_pct 并清空本分支，此处只接旧档漏网（amount>0 且
        # heal_pct==0）：按 amount 百分数结算，不再读旧 heal_amount 回退。
        hp_max = max_hp_for(player)
        pct = int(eff.get("amount", 0) or 0)
        if pct <= 0:
            qty = item_heal_value(item, player)
            pct = round(qty * 100 / hp_max) if hp_max > 0 else 0
        else:
            pct = max(1, min(100, pct))
            qty = int(hp_max * pct / 100)
        hp_before = _stat(player, "hp", 0)
        snap = compute_stats(player, equip_stat_bonus=equip_stat_bonus)
        heal(player, qty, bonus=snap.heal_bonus)
        actual = _stat(player, "hp", 0) - hp_before
        return True, f"服用{name}，回复 {actual} HP（最大生命{pct}%）"
    if etype == "heal_mp":
        mp_max = _stat(player, "mp_max", 0)
        pct = max(1, min(100, int(eff.get("amount", 0) or 0)))
        mp_before = _stat(player, "mp", 0)
        try:
            player.mp = min(mp_max, mp_before + int(mp_max * pct / 100))
        except (AttributeError, TypeError):
            pass
        actual = _stat(player, "mp", 0) - mp_before
        # [C9 修复 2026-08-25] 报实际回蓝量（钳 mp_max 后），对齐 heal_full 的 actual 口径
        return True, f"服用{name}，回复 {actual} 灵力（最大灵力{pct}%）"
    if etype == "cure":
        return False, f"{name}用于战斗中清除负面状态，此时无需服用"
    if etype == "revive":
        return False, f"{name}会在战斗中倒下时自动生效，此时无需服用"
    if etype == "feed":
        # [饱食度 2026-09-06] 食物：回饱食度（+N 钳 100；关闭饱食度的世界 hunger 恒 100
        # 吃了也无感——引擎照常结算，实际值不变）。
        gain = int(eff.get("hunger", 0) or 0)
        h_before = _stat(player, "hunger", 100)
        try:
            player.hunger = max(0, min(100, h_before + gain))
        except (AttributeError, TypeError):
            pass
        actual = _stat(player, "hunger", 100) - h_before
        return True, f"吃了{name}，饱食度 +{actual}"
    return False, ""


# [P52 伤疤 2026-09-25] 题材伤名池（4 部位 x >=2 变体 x 6 题材，守 §23 数据量基线）
_GENRE_INJURY_NAMES: dict = {
    "western_fantasy": {"arm": ["持械臂筋扭伤", "挥剑臂裂创"], "legs": ["膝弯旧患", "腿骨裂纹"],
                        "head": ["脑震荡", "额角裂伤"], "torso": ["肋骨隐痛", "胸口淤伤"]},
    "xianxia": {"arm": ["经脉挫伤", "持剑臂灵息紊乱"], "legs": ["灵足麻痹", "步罡腿伤"],
                "head": ["气海震伤", "神魂刺痛"], "torso": ["丹田隐痛", "胸口灵淤"]},
    "wuxia": {"arm": ["腕肘旧伤", "持刀臂劳损"], "legs": ["轻功腿伤", "膝弯淤血"],
              "head": ["晕眩症", "额上刀疤肿痛"], "torso": ["内腑震伤", "肋下旧创"]},
    "modern": {"arm": ["腕管综合征", "手臂肌肉拉伤"], "legs": ["膝盖软组织损伤", "踝扭伤"],
               "head": ["脑震荡后遗症", "偏头痛"], "torso": ["肋骨挫伤", "软组织淤血"]},
    "scifi": {"arm": ["义体接口排异", "神经传导磨损"], "legs": ["传动轴磨损", "腿部伺服过载"],
              "head": ["神经接口过热", "视觉缓存紊乱"], "torso": ["装甲压伤", "内脏缓冲失效"]},
    "apocalypse": {"arm": ["腐蚀性擦伤", "挥击肌腱拉伤"], "legs": ["溃烂小腿", "旧骨折未愈"],
                   "head": ["辐射性眩晕", "耳膜嗡鸣"], "torso": ["辐射淤伤", "肋骨裂痕"]},
}


def _note_part_hit(session, result, dmg: int) -> None:
    """[P52] 战斗期累计玩家各部位受击伤害（transient；伤疤结算的原料）。"""
    part = str(getattr(result, "body_part", "") or "")
    if not part:
        return
    acc = getattr(session, "player_part_dmg", None)
    if not isinstance(acc, dict):
        acc = {}
    acc[part] = acc.get(part, 0) + max(0, int(dmg))
    session.player_part_dmg = acc


def roll_injuries(world: Any, entity: Any, part_dmg: dict, hp_max: int,
                  rng: SeededRng, min_one: bool = False) -> list:
    """战损结算（finish_combat / 快速战斗两路径同口径调用）。

    part_dmg = {part: 累计伤害}（战斗期累计）；取累计伤害 >= max_hp 25% 的最重部位
    roll 一处伤（天数 2-4，伤名走题材池）。返回新增伤列表。
    min_one=True（战败保底）：即使无部位累计到阈值，也留一处躯干伤——战败必有代价
    （P47 主题一致性：不会输得毫无痕迹）。
    [!] 独立盐 injury_{entity.id} 由调用方构造传入 rng——不碰既有流。
    """
    ov = getattr(world, "config_overlay", None) or {}
    tid = str(ov.get("attribute_template_id", "") or "western_fantasy")
    pool = _GENRE_INJURY_NAMES.get(tid) or _GENRE_INJURY_NAMES["western_fantasy"]
    part = None
    heavy = sorted([(str(p), int(d or 0)) for p, d in (part_dmg or {}).items()
                    if int(d or 0) >= max(1, int(hp_max or 1) * 0.25)],
                   key=lambda x: (-x[1], x[0]))
    if heavy:
        part = heavy[0][0]
    elif min_one:
        part = "torso" if "torso" in pool else next(iter(pool), None)
    if part is None or part not in pool:
        return []
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    days = rng.roll(2, 4)
    name = rng.pick(pool[part])
    injuries = [i for i in (getattr(entity, "injuries", None) or [])
                if isinstance(i, dict)]
    injuries.append({"part": part, "until_day": day + days, "name": str(name)})
    entity.injuries = injuries
    return [{"part": part, "until_day": day + days, "name": str(name)}]


def try_auto_revive(world: Any, player: Any, hp_target: Any = None,
                    consumed_ids: Optional[list] = None) -> str:
    """[P47 修复 2026-09-25] 玩家倒下时自动使用复活丹（consume_effect.type == "revive"）。

    效果：消耗背包里第一颗复活丹，按 amount%（缺省 30%）恢复 HP 并重新站起。
    返回使用的丹药名（"" = 背包里没有）。战斗外手动「使用」仍走 apply_consume_effect
    的提示（复活丹的语义就是倒下时自动生效，站着吃不消耗）。

    [!] 此前 revive 只有文案没有机制（物品说明承诺「倒下时自动生效」，真倒下什么也不发生
        还白扣物品）——生成提示词要求产出 revive 类道具、说明文案在骗玩家（评估 F-1）。
    """
    inv = list(getattr(player, "inventory", None) or [])
    items = getattr(world, "items", None) or []
    for iid in inv:
        it = next((i for i in items if getattr(i, "id", "") == iid), None)
        if it is None or getattr(it, "type", "") != "consumable":
            continue
        eff = getattr(it, "consume_effect", None)
        if not isinstance(eff, dict) or str(eff.get("type", "") or "") != "revive":
            continue
        try:
            player.inventory.remove(iid)
        except ValueError:
            continue
        pct = max(1, min(100, int(eff.get("amount", 0) or 30)))
        target = hp_target if hp_target is not None else player
        hp_max = max(1, _stat(target, "hp_max", 1))
        target.hp = max(1, int(hp_max * pct / 100))
        if consumed_ids is not None:
            consumed_ids.append(iid)
        return str(getattr(it, "name", "") or "复活丹")
    return ""


def roll_loot(
        loot_table: list,
        rng: SeededRng,
        drop_rate: float = 0.3,
        difficulty_mult: float = 1.0,
        luck: int = 0,
) -> list:
    """据掉落表 roll 掉落 item_id 列表。

    loot_table 元素可为 str(item_id) 或 dict{id, rate}。每个独立 roll。
    [P7e] luck（stat_luk）影响掉落率：effective_rate = clamp(rate*mult*(1+luck*0.01), 0, 0.95)。
    """
    luck_factor = 1.0 + max(0, int(luck)) * 0.01
    out = []
    for entry in (loot_table or []):
        if isinstance(entry, str):
            item_id, rate = entry, drop_rate
        elif isinstance(entry, dict):
            item_id = entry.get("id") or entry.get("item_id") or ""
            # [!] 不可用 `or drop_rate`：合法 rate=0.0 会被 falsy 吞成 drop_rate
            rate = entry.get("rate", drop_rate)
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                rate = drop_rate
        else:
            continue
        if not item_id:
            continue
        effective = max(0.0, min(0.95, rate * difficulty_mult * luck_factor))
        if rng.chance(effective):
            out.append(item_id)
    return out


def success_chance(
        stat_value: int,
        tool_bonus: int = 0,
        difficulty: int = 0,
        base: float = 0.5,
        global_penalty: int = 0,
) -> float:
    """[P7e] 通用成功率钩子（采集/炼制/生活技能复用）。

    公式：clamp(base + stat*0.01 + tool*0.05 - (difficulty + global_penalty)*0.02, 0.05, 0.95)。
    - stat_value：驱动属性值（采集用 stat_dex/int，炼制用 stat_int）
    - tool_bonus：工具加成档位（0=徒手，1=普通工具，2=精良工具...）
    - difficulty：节点/配方难度（0-100，内容难度：地点危险度/配方）
    - base：基础成功率（默认 0.5）
    - global_penalty：[P 验收] 全局难度偏移（check_difficulty_penalty），困难下所有判定更难通过
    纯函数无 rng，调用方拿到概率后自行 rng.chance。
    """
    eff_difficulty = max(0, int(difficulty)) + max(0, int(global_penalty))
    chance = base + max(0, int(stat_value)) * 0.01 + max(0, int(tool_bonus)) * 0.05 - eff_difficulty * 0.02
    return max(0.05, min(0.95, chance))


# ---- [P8] 幸运 -> 掉落/产出稀有度升档（通用，复用于战斗掉落/采集/合成大成功）----
_RARITY_TIERS = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
_RARITY_MULT = {"common": 1.0, "uncommon": 1.3, "rare": 1.7, "epic": 2.2, "legendary": 3.0, "mythic": 4.0}
_RARITY_SUFFIX = {"uncommon": "·精良", "rare": "·稀有", "epic": "·史诗", "legendary": "·传说", "mythic": "·神话"}


def roll_rarity_upgrade(rng: SeededRng, base_rarity: str, luck: int, level_bonus: int = 0,
                        cap: float = 0.4) -> str:
    """[P8] 据幸运 + 等级 bonus roll 稀有度升档，返回升档后的稀有度（可连续升，封顶 legendary）。

    升档概率 = clamp(luck*0.008 + level_bonus*0.005, 0, cap)。
    level_bonus：怪物等级（掉落）/ 地点 danger（奇遇）/ 配方难度（合成）等驱动量。
    base_rarity 不在档位序列则原样返回。
    纯函数（用传入的 rng），确定性可复现。
    """
    if base_rarity not in _RARITY_TIERS:
        return base_rarity
    idx = _RARITY_TIERS.index(base_rarity)
    upgrade_chance = max(0.0, min(cap, max(0, int(luck)) * 0.008 + max(0, int(level_bonus)) * 0.005))
    while idx < len(_RARITY_TIERS) - 1 and rng.chance(upgrade_chance):
        idx += 1
    return _RARITY_TIERS[idx]


def upgrade_item_rarity(item: Any, new_rarity: str, rarity_names: Optional[dict] = None) -> Any:
    """[P8] 克隆 item 改 rarity + 按比例缩放数值，返回新 Item（新 id + 稀有度后缀名）。不改原 item。

    数值缩放按 新稀有度mult / 原稀有度mult 比例（attack/defense/stat_bonus 同口径，
    复用 _RARITY_MULT 与 _init_combat_stats 一致）。new_rarity==原档或非法返回 None。
    用 copy.copy 克隆（保留原 Item 类，鸭子类型不 import world.Item 防循环依赖）。
    [P8 题材化] rarity_names（题材品级名 dict，如 {epic:"高级"}）传入时，后缀用题材名
    （手枪·高级），否则回退通用 _RARITY_SUFFIX（手枪·史诗）。避免跨题材命名串味。
    """
    import copy
    cur = getattr(item, "rarity", "common") or "common"
    if new_rarity == cur or new_rarity not in _RARITY_MULT:
        return None
    new = copy.copy(item)
    try:
        # [!] 确定性派生 id（原 id + 目标档）：uuid4 会破坏「同 world+tick+salt 同结果」
        # 的可回放铁律（同 seed 重放得到不同 id）；派生 id 还天然去重——同一原物品
        # 多次升到同档只会得到同一个克隆，不堆积。
        new.id = f"{getattr(item, 'id', '')}__{new_rarity}"
    except Exception:
        pass
    try:
        new.rarity = new_rarity
    except Exception:
        pass
    # [P8 题材化] 后缀优先用题材品级名（rarity_names），否则通用 _RARITY_SUFFIX
    # 统一格式 base·后缀：题材名无前缀直接用；通用 _RARITY_SUFFIX 自带「·」前缀需 lstrip 防双 ·
    if isinstance(rarity_names, dict) and rarity_names.get(new_rarity):
        suffix = str(rarity_names.get(new_rarity))
    else:
        suffix = _RARITY_SUFFIX.get(new_rarity, "").lstrip("·")
    try:
        base_name = getattr(item, "name", "") or ""
        new.name = f"{base_name}·{suffix}" if suffix else base_name
    except Exception:
        pass
    old_mult = _RARITY_MULT.get(cur, 1.0)
    new_mult = _RARITY_MULT.get(new_rarity, 1.0)
    factor = (new_mult / old_mult) if old_mult > 0 else 1.0
    try:
        new.attack = int(getattr(item, "attack", 0) * factor)
    except Exception:
        pass
    try:
        new.defense = int(getattr(item, "defense", 0) * factor)
    except Exception:
        pass
    sb = getattr(item, "stat_bonus", None)
    if isinstance(sb, dict) and sb:
        try:
            new.stat_bonus = {k: max(1, int(v * factor)) for k, v in sb.items()}
        except Exception:
            pass
    # 耐久满（新装备初始状态）；不朽装备（durability_max<=0）置 0，防显示 100/0 错乱
    try:
        dmax_raw = getattr(item, "durability_max", 100)
        dmax = int(dmax_raw) if dmax_raw is not None else 100
        new.durability = max(0, dmax)
    except Exception:
        pass
    return new


def upgrade_drops(drops: list, world: Any, rng: SeededRng,
                  luck: int = 0, monster_level: int = 0) -> list:
    """[P8] 对已掉落的 item_id 列表 roll 稀有度升档。

    升档的物品克隆入 world.items 并替换为新 id（原 item 定义不动，世界池只增不减）。
    升档概率 = clamp(luck*0.008 + monster_level*0.005, 0, 0.4)，命中上一档可连续升。
    [P8 题材化] 从 world.config_overlay.rarity_display_names 读题材品级名做后缀（防跨题材命名串味，
    仅用于命名显示不参与数值）。返回新 item_id 列表（含升档克隆）。
    """
    if not drops:
        return []
    items_pool = getattr(world, "items", None)
    if not isinstance(items_pool, list):
        items_pool = []
    # [P8 题材化] 读该世界题材品级名（无则 upgrade_item_rarity 回退通用后缀）
    overlay = getattr(world, "config_overlay", None)
    rarity_names = None
    if isinstance(overlay, dict) and isinstance(overlay.get("rarity_display_names"), dict):
        rarity_names = overlay["rarity_display_names"]
    by_id = {getattr(i, "id", ""): i for i in items_pool}
    out = []
    for did in (drops or []):
        did = str(did)
        it = by_id.get(did)
        if it is None:
            out.append(did)
            continue
        cur = getattr(it, "rarity", "common") or "common"
        new_rar = roll_rarity_upgrade(rng, cur, luck=luck, level_bonus=monster_level, cap=0.4)
        if new_rar != cur:
            upgraded = upgrade_item_rarity(it, new_rar, rarity_names=rarity_names)
            if upgraded is not None:
                # [!] 派生 id 天然幂等：克隆已在池中（同源同档）不重复 append
                up_id = getattr(upgraded, "id", did)
                if up_id not in by_id:
                    items_pool.append(upgraded)
                    by_id[up_id] = upgraded
                out.append(up_id)
                continue
        out.append(did)
    return out


def materialize_unidentified_drops(world: Any, drops: list) -> list:
    """[P34a] 把掉落列表里的装备类模板 id 克隆成「未鉴定」副本并替换 id。

    玩家拾取的装备应默认未鉴定（identified=False，属性打码需用鉴定卷轴揭示，守 P34a 用户定稿
    「掉落即未鉴定」）。因 player.inventory 存的是 item_id 而非 Item 实例，且世界物品池是共享
    模板（同 id 装备会被多次掉落/上架），不能直接改模板的 identified——必须克隆成独立副本入
    world.items 再换 id。
    [!] 只克隆装备类（weapon/armor/accessory）；material/key/consumable/cultivate reagent 不克隆
        （无鉴定态意义，原样透传）。
    [!] 派生 id 确定性 `{origid}__unid`（守 §22 派生 id 不变量 L294）：同源装备多次掉落复用同一
        未鉴定副本（幂等，不堆积）；uuid4 会破坏可回放性。
    [!] 原地改 drops 并返回（调用方拿到新列表即可；drops 是 list 引用，就地替换元素）。
    [!] 已是未鉴定的物品（identified=False）不二次克隆（避免拍卖会/锻造产出的未鉴定物被再克隆）。
    """
    items_pool = getattr(world, "items", None)
    if not isinstance(items_pool, list) or not drops:
        return drops
    by_id = {getattr(i, "id", ""): i for i in items_pool}
    out = []
    for did in (drops or []):
        did = str(did)
        it = by_id.get(did)
        if it is None:
            out.append(did)
            continue
        # 只克隆装备类 + 当前已鉴定（identified=True）的模板；未鉴定物透传
        if getattr(it, "type", "") not in ("weapon", "armor", "accessory") \
                or getattr(it, "identified", True) is False:
            out.append(did)
            continue
        new_id = f"{did}__unid"
        if new_id in by_id:
            # 幂等：未鉴定副本已存在（同源装备多次掉落），复用
            out.append(new_id)
            continue
        import copy as _copy
        clone = _copy.copy(it)
        clone.id = new_id
        clone.identified = False
        items_pool.append(clone)
        by_id[new_id] = clone
        out.append(new_id)
    return out


# ================================================================
# ====== [P7h] 回合制战斗状态机（多回合 + 技能 + 敌人 AI）======
# ================================================================
# 数值范式铁律（守 §21b / Req4）：战斗完全纯 Python，不用 LLM 打架。
# LLM 只在战后据回合日志摘要描写旁白。参考最终幻想/勇者斗恶龙/博德之门回合制。
#
# [P10] 多敌 + 同伴：
# - CombatUnit 是「随从敌人 / 助战同伴」的战斗单位包装（快照 + mp/技能/冷却/AI）。
# - 主敌人仍走 legacy 字段（enemy/enemy_mp/enemy_skills/enemy_cd/enemy_ai/enemy_talents），
#   start_combat 构建会话时约定 enemy_units[0] 是主敌包装（snapshot 与 session.enemy 同引用），
#   enemy_units[1:] 是随从（is_minion=True，战斗本地克隆不入世界 NPC 表）。
# - 回合顺序：玩家 -> 同伴（ally_turn）-> 敌方（enemy_turn：主敌 + 随从逐个行动）。
# - 胜利判定：主敌倒下且随从全灭；同伴倒下不判负（战后 hp 写回至少 1，好友不阵亡）。


@dataclass
class CombatUnit:
    """[P10] 战斗单位（敌方随从 / 助战同伴通用）。

    snapshot 与外界共享引用（NPC 的聚合快照），本单位对 hp 的写直接反映到快照。
    alive 是动态属性（hp > 0），倒下后不再行动/不再被选为目标。
    """
    name: str = ""
    npc_id: str = ""           # 关联世界 NPC id（随从克隆为空串）
    morale: int = 60           # [P51 战意 2026-09-25] 0-100（60 稳定；transient 不落盘）
    snapshot: CombatSnapshot = field(default_factory=CombatSnapshot)
    mp: int = 0
    mp_max: int = 0
    skills: list = field(default_factory=list)    # list[dict] 技能定义
    cd: dict = field(default_factory=dict)        # {skill_id -> 剩余冷却}
    talents: list = field(default_factory=list)   # [P9] 天赋 list[dict]
    ai: str = "balanced"                          # aggressive/defensive/caster/balanced
    role_label: str = ""                          # 显示标签（如「随从」/「同伴」）
    is_minion: bool = False                       # True=战斗本地克隆的杂兵（不入世界表）
    loot_table: list = field(default_factory=list)  # [P10] 该单位被击败时的掉落表（随从用）
    conditions: list = field(default_factory=list)  # [P30] 状态 [{key,duration,source_element}]（随从/同伴单位）
    reaction_used: bool = False                    # [P31] 反应预算（本回合是否已用过反应，回合末重置）
    # [部位 2026-08-31] 该单位的部位弱点 key（"head"/"torso"/"arm"/"legs"；空=无弱点）。
    # start_combat 按 unit+world 确定性 roll；弱点命中 x1.5 且可暴击。
    weakpoint: str = ""
    # [布局 2026-08-31] 前后排位：row=1 前排 / row=2 后排；slot 0-4（2-1-2 布局）。
    # 敌我双方前排（row=1）全灭前不得攻击对方后排（row=2）。
    row: int = 1
    slot: int = 0
    avatar: str = ""             # 头像/立绘图文件名（world_images_dir 内；空=占位绘制）

    @property
    def alive(self) -> bool:
        return self.snapshot is not None and self.snapshot.hp > 0

    @property
    def hp_ratio(self) -> float:
        mx = max(1, getattr(self.snapshot, "hp_max", 1) or 1)
        return max(0.0, min(1.0, (getattr(self.snapshot, "hp", 0) or 0) / mx))


@dataclass
class CombatSession:
    """[P7h] 回合制战斗会话（玩家 vs 单敌，多回合循环）。

    player/enemy 是聚合后的 CombatSnapshot（战斗中改 .hp）。
    state: active(进行中)/won(玩家胜)/lost(玩家败)/fled(逃跑或回合超限脱战)。
    [P10] enemy_units：全部敌方单位（enemy_units[0] 为主敌包装，[1:] 为随从；
    空列表 = legacy 单敌会话，主敌走 legacy 字段）。ally_units：助战同伴。
    """
    player: CombatSnapshot
    enemy: CombatSnapshot
    player_skills: list = field(default_factory=list)   # list[dict] 玩家技能定义
    enemy_skills: list = field(default_factory=list)    # list[dict] 敌人技能定义
    enemy_ai: str = "balanced"                          # aggressive/defensive/caster/balanced
    player_mp: int = 0
    enemy_mp: int = 0
    # [修 2026-09-10] MP 上限：玩家/主敌是 bare CombatSnapshot（无 mp/mp_max 字段），
    # 对撞「守住回 2% max MP」原来读快照恒取到 0 -> 奖励从未发放。上限由 start_combat 注入。
    player_mp_max: int = 0
    enemy_mp_max: int = 0
    player_cd: dict = field(default_factory=dict)       # {skill_id -> 剩余冷却}
    # [观战 2026-09-06 用户指示] 敌袭观战（守军 vs 来敌，玩家不参战）：spectate=True 时
    # 玩家位=守方队长——lost 判定改「玩家位+同伴全灭」（队长倒地守军仍战，见 _side_defeated）；
    # player_label 是玩家位显示名（正常战斗恒"你"，观战=守方队长名，战斗日志文案用）。
    spectate: bool = False
    player_label: str = "你"
    # [P47-C2 敌情预告 2026-09-25] transient：本回合敌方意图（锁定目标确定性推演 + 威胁档）。
    # 规划走独立 salt 流（combat_intent{round}），不碰既有 combat_* 序列；目标推演与
    # _main_enemy_act 的既有目标选择同规则 -> 预告与执行一致（预告为真）。
    enemy_intents: list = field(default_factory=list)
    enemy_intents_round: int = 0
    # [P51 战意 2026-09-25] 玩家/主敌战意（transient；同伴/随从挂 CombatUnit.morale）
    player_morale: int = 60
    enemy_morale: int = 60
    # [P52 伤疤] 玩家各部位累计受击伤害 {part: dmg}（transient；伤疤结算原料）
    player_part_dmg: dict = field(default_factory=dict)
    enemy_cd: dict = field(default_factory=dict)
    player_talents: list = field(default_factory=list)  # [P9] 玩家天赋 list[dict]（resolve_skill 算 skill_dmg_mult）
    enemy_talents: list = field(default_factory=list)   # [P9] 敌人天赋 list[dict]
    enemy_units: list = field(default_factory=list)     # [P10] 敌方单位（[0] 主敌包装 + 随从）
    # [Boss 机制库 2026-08-29] transient：start_combat 据 target_npc.mechanics 初始化
    boss_mechanics: list = field(default_factory=list)   # shield/summon_tide/enrage_timer
    boss_shield: int = 0            # 护盾剩余吸收量（>0 时主敌承伤 x0.3）
    boss_shield_max: int = 0
    boss_enrage_round: int = 0      # 狂暴计时（>0 = 第 N 回合触发；0=无此机制）
    boss_enrage_active: bool = False
    ally_units: list = field(default_factory=list)      # [P10] 助战同伴单位
    # [P25b] 同伴指令 {npc_id -> focus/defend/retreat/free}（free=现状 AI 默认，未登记
    # 即 free）；player_target = 玩家本回合目标单位（focus 集火依据，player_action 刷新）。
    ally_orders: dict = field(default_factory=dict)
    player_target: Optional["CombatUnit"] = None
    round: int = 1
    state: str = "active"
    player_defending: bool = False                      # 玩家本回合防御（受击对撞判定）
    # [对撞 2026-09-01] 敌方（主敌+随从统一）本回合防御：玩家/同伴普攻命中时对撞判定。
    enemy_defending: bool = False
    log: list = field(default_factory=list)             # 回合日志累积（finish_combat 读）
    difficulty: str = "normal"
    granularity: str = "medium"
    max_rounds: int = 30
    # [P26a] 结义同伴 npc_id 集合（start_combat 预注入；非空则玩家与结义同伴普攻享 sworn_damage_bonus）。
    sworn_npc_ids: set = field(default_factory=set)
    # [P26a] 结义联手普攻伤害倍率（start_combat 据 preset.sworn_damage_bonus 预注入；1.0=无加成）。
    sworn_damage_mult: float = 1.0
    # [P30] 玩家/主敌是 bare CombatSnapshot（非 CombatUnit），状态挂 session。transient 不落盘。
    player_conditions: list = field(default_factory=list)   # [{key,duration,source_element}]
    enemy_conditions: list = field(default_factory=list)    # [{key,duration,source_element}]
    # [P31] 玩家反应预算（本回合是否已用过反应，回合末 _apply_conditions 重置）
    player_reaction_used: bool = False
    # [P30] 题材 id（start_combat 注入；状态日志/显示名走 condition_display(key, genre_id) 题材化）
    genre_id: str = "western_fantasy"
    # [技能熟练随机 2026-09-10] 世界 id（start_combat 注入）：施展熟练度随机加点的
    # 确定性盐源（零战斗 rng 序列消费）。空串=测试桩（退化为技能名+状态盐，仍确定）。
    world_id: str = ""
    # ---- [P40] 战斗环境 + Boss 阶段（transient 不落盘）----
    environment: str = ""            # 环境型 key（白名单 _ENV_TYPES；空=平地无环境）
    environment_used: bool = False   # 借势环境一次性（用完无势可借）
    boss_fight: bool = False         # Boss 战（dungeon boss / world boss / level>=5）：启用阶段机制
    boss_phase: int = 0              # 已进入的阶段（0=未触发；1=66% 狂暴；2=33% 召唤狂卫）
    # ---- [战报 2026-08-28] 伤害累计器（transient 不落盘；finish_combat 读入战报历史）----
    # 口径：玩家侧输出=player_action/ally_turn 对敌方造成的伤害；承受=enemy_turn 对玩家侧伤害。
    # 结算点在三个回合函数出口按 hp 快照差值累计（快照共享引用，差值即本回合净伤害）。
    dmg_dealt: int = 0
    dmg_taken: int = 0
    # ---- [布局 2026-08-31] 前后排（2-1-2，每侧最多 5 位；transient 不落盘）----
    # player_row：玩家所在排（1=前排/2=后排）。layout_seed 用于 UI 复现布局 roll。
    # 敌我双方：前排（row=1）全灭前不得攻击对方后排（row=2）——见 front_row_alive/
    # filter_targetable。玩家/主敌是 bare snapshot 不挂 CombatUnit.row，攻后排门控时
    # 视作各自所在排（player_row / 主敌包装 unit.row）。
    player_row: int = 1


# ---- [P10] 多敌/同伴单位辅助 ----
def all_enemy_combatants(session: CombatSession) -> list:
    """全部敌方单位。enemy_units 非空时原样返回（约定 [0] 为主敌包装）；
    空（legacy 单敌会话）时临时包装主敌（快照共享引用）。"""
    if session.enemy_units:
        return list(session.enemy_units)
    return [CombatUnit(name="enemy", snapshot=session.enemy)]


def living_enemy_units(session: CombatSession) -> list:
    """存活敌方单位列表。"""
    return [u for u in all_enemy_combatants(session) if u.alive]


def living_ally_units(session: CombatSession) -> list:
    """存活同伴单位列表。"""
    return [u for u in (session.ally_units or []) if u.alive]


def _living_minions(session: CombatSession) -> list:
    """存活随从（不含主敌）。"""
    return [u for u in (session.enemy_units or [])
            if getattr(u, "is_minion", False) and u.alive]


# ---- [布局 2026-08-31] 前后排（2-1-2）----
def front_row_alive(session: CombatSession, side: str) -> bool:
    """side 方前排（row=1）是否还有存活单位。

    side="enemy"：主敌包装 + 随从中 row==1 的存活者（主敌倒下不挡后排——按单位判）。
    side="ally"：玩家（session.player_row==1 视作前排存活）+ 同伴单位中 row==1 的存活者。
    """
    if side == "enemy":
        return any(u.alive and getattr(u, "row", 1) == 1
                   for u in all_enemy_combatants(session))
    if getattr(session, "player_row", 1) == 1 and session.player.hp > 0:
        return True
    return any(u.alive and getattr(u, "row", 1) == 1
               for u in (session.ally_units or []))


def player_targetable(session: CombatSession) -> bool:
    """[修 2026-09-10] 玩家当前是否可被敌方选中（前排门控）——**单一来源**。

    契约要求门控五路共用 helper，但此前有 3 处各抄一遍
    `player_row==1 or not front_row_alive(session,"ally")`，而契约点名的
    `targetable_ally_side` 生产代码零调用（只被测试引用）——正是契约自己警告的
    「只改一处必漏」。现统一到本函数，_main_enemy_act / _minion_act /
    _resolve_skill_targets / targetable_ally_side 共用。
    判据：玩家存活 且（玩家在前排 或 我方前排已清）。
    """
    return (session.player.hp > 0
            and (getattr(session, "player_row", 1) == 1
                 or not front_row_alive(session, "ally")))


def unit_targetable(session: CombatSession, unit: CombatUnit, side: str) -> bool:
    """该单位当前是否可被对方选中：存活 + （对方前排存活时仅前排可选）。

    side = 该单位所属方（"enemy" 判攻敌方视角、"ally" 判攻玩家方视角）。
    """
    if not unit.alive:
        return False
    if getattr(unit, "row", 1) == 2 and front_row_alive(session, side):
        return False   # 对方前排未清空，后排不可选
    return True


def targetable_enemy_units(session: CombatSession) -> list:
    """玩家方可选的敌方单位（前排门控过滤）。"""
    return [u for u in living_enemy_units(session) if unit_targetable(session, u, "enemy")]


def targetable_ally_side(session: CombatSession):
    """敌方 AI 可打的玩家侧目标：前排门控下玩家或同伴单位。

    返回 ("player", None) 或 ("ally", CombatUnit)；无可选目标返回 (None, None)
    （理论不可达——前排全灭时后排必然可选）。
    """
    if player_targetable(session):      # 单一来源门控（见 player_targetable）
        return "player", None
    cands = [u for u in living_ally_units(session) if unit_targetable(session, u, "ally")]
    if cands:
        return "ally", cands[0]
    # 前排门控下无解（数据异常兜底：无视门控返回玩家）
    return ("player", None) if session.player.hp > 0 else (None, None)


def assign_layout(units: list, rng: SeededRng) -> list:
    """[布局] 给一组单位随机分配 2-1-2 前后排（确定性）。

    位置语义：slot 0/1=前排左右，slot 2=中轴，slot 3/4=后排左右。
    row 约定：slot 0/1 -> row=1（前排）；slot 2/3/4 -> row=2（后排）。
    单位数 <=2 全前排；3-5 时按 roll 混排（保证前排至少 1）。返回同列表（原地改）。
    """
    n = len(units)
    if n <= 0:
        return units
    if n <= 2:
        plan = list(range(min(n, 2)))
    else:
        # 3-5 人：前排 1-2 个 + 其余后排（随机切分，保前排非空）。
        # [!] 前排下限 n-3：后排只有 3 个物理位（slot 2/3/4），n=5 时前排必为 2
        # （否则 plan 缺位 -> 有单位漏分配而残留默认 slot 0，两位重叠同位）。
        front = rng.roll(max(1, n - 3), min(2, n - 1))
        plan = [0, 1][:front] + [2, 3, 4][:n - front]
    assert len(plan) == n, "layout plan must cover every unit"
    order = list(range(n))
    # 洗牌后按 plan 依次落位
    for i in range(n):
        j = rng.roll(i, n - 1)
        order[i], order[j] = order[j], order[i]
    for idx, slot in enumerate(plan):
        u = units[order[idx]]
        u.slot = int(slot)
        u.row = 1 if slot in (0, 1) else 2
    return units


def roll_weakpoint(world_id: str, unit_key: str, tick: int) -> str:
    """[部位] 为单位 roll 一个部位弱点（确定性：world+key+tick 同结果）。"""
    rng = SeededRng.seed_from(str(world_id), int(tick), f"weak_{unit_key}")
    return rng.pick(list(BODY_PART_KEYS)) or "torso"


def _side_defeated(session: CombatSession) -> bool:
    """[观战 2026-09-06] 玩家方全灭判定：正常战斗玩家位倒即败（原语义不变）；
    spectate 模式（敌袭观战，玩家位=守方队长）须玩家位 + 全部同伴倒下才算败。"""
    if not getattr(session, "spectate", False):
        return session.player.hp <= 0
    if session.player.hp > 0:
        return False
    return all(not u.alive for u in (session.ally_units or []))


def _refresh_battle_state(session: CombatSession):
    """[P10] 战斗结束判定：玩家倒下 lost；主敌倒下且随从全灭 won。
    [P40] 尾部驱动 Boss 阶段检查（主敌 Hp 阈值跨越触发狂暴/召唤，每次行动后经此刷新）。"""
    # [P51 战意] 倒下钩子（单一收口）：倒下一方全队掉战意、对面 +。
    # [!] 边沿检测（_player_down_noted/_enemy_down_noted transient 标记）：hp<=0 是持续
    #     状态，每次刷新都触发会把战意瞬间打穿——只在「未倒 -> 倒下」瞬间结算一次。
    p_down = (getattr(session, "player", None) is not None and session.player.hp <= 0)
    e_down = (getattr(session, "enemy", None) is not None and session.enemy.hp <= 0)
    if p_down and not getattr(session, "_player_down_noted", False):
        session._player_down_noted = True
        _morale_on_death(session, "player")
    if e_down and not getattr(session, "_enemy_down_noted", False):
        session._enemy_down_noted = True
        _morale_on_death(session, "enemy")
    if _side_defeated(session):
        session.state = "lost"
    elif session.enemy.hp <= 0 and not _living_minions(session):
        session.state = "won"
    phase_log = _check_boss_phase(session)
    if phase_log:
        session.log.append(phase_log)


# ---- [P40] 战斗环境（借势一次性指令；纯 Python 确定性，数值走引擎不走 LLM） ----

_ENV_TYPES = ("cliff", "oil", "vines", "rocks")

# [数据量铁律] 环境题材化命名池（4 型 x >=2 变体 x 6 题材；基线测试守护）
_ENV_GENRE_NAMES: dict[str, dict[str, list[str]]] = {
    "xianxia": {"cliff": ["断魂崖", "万丈云崖"], "oil": ["丹炉火盆", "炼器炉火"],
                "vines": ["缠灵古藤", "噬灵荆棘"], "rocks": ["崩落灵岩", "悬空巨石"]},
    "wuxia": {"cliff": ["舍身崖", "断龙涧"], "oil": ["灶台热油", "灯笼火盆"],
              "vines": ["老树盘藤", "青蔓竹丛"], "rocks": ["塌方山石", "檐角飞石"]},
    "modern": {"cliff": ["天台边缘", "高架断口"], "oil": ["加油站油桶", "后厨油锅"],
               "vines": ["缠绕电缆", "脚手架网"], "rocks": ["坠落钢梁", "松动砖墙"]},
    "scifi": {"cliff": ["气闸断桥", "反应堆深渊"], "oil": ["冷却剂罐", "燃料储罐"],
              "vines": ["失控导缆", "维护臂锁具"], "rocks": ["坍塌舱段", "松动装甲板"]},
    "apocalypse": {"cliff": ["废楼断口", "坍塌天井"], "oil": ["锈蚀油桶", "篝火堆"],
                   "vines": ["枯藤废墙", "铁丝网丛"], "rocks": ["危墙碎砖", "摇摇欲坠的招牌"]},
    "western_fantasy": {"cliff": ["风啸崖畔", "裂谷边缘"], "oil": ["火把架油桶", "壁炉火盆"],
                        "vines": ["密林古藤", "荆棘丛"], "rocks": ["松动巨石", "塌方岩壁"]},
}


def env_display_name(env: str, genre_id: str = "western_fantasy", seed: int = 0) -> str:
    """环境题材化显示名（确定性挑选：同 seed 同名）。"""
    pool = (_ENV_GENRE_NAMES.get(genre_id) or _ENV_GENRE_NAMES["western_fantasy"]) \
        .get(env) or []
    if not pool:
        return {"cliff": "悬崖", "oil": "油桶", "vines": "藤蔓", "rocks": "巨石"}.get(env, "险地")
    return pool[abs(int(seed)) % len(pool)]


def _unit_burn(unit, key: str, duration: int) -> None:
    """给随从单位直挂状态（主敌走 _inflict_condition 的 session 侧；结构同 {key,duration}）。"""
    if key not in _CONDITION_DEFS:
        return
    conds = getattr(unit, "conditions", None)
    if conds is None:
        return
    for c in conds:
        if isinstance(c, dict) and c.get("key") == key:
            c["duration"] = max(int(c.get("duration", 1) or 1), duration)
            return
    conds.append({"key": key, "duration": max(1, duration), "source_element": ""})


def use_environment(session: CombatSession, rng: SeededRng) -> dict:
    """[P40] 借势环境（玩家专属一次性指令）。检定/效果全引擎确定性：

    - cliff 悬崖：等级差检定 -> 主敌 Hp 大残（-50% 当前值保 1）；非 Boss 单体直接败退
      脱战（[定稿] Boss 不败退防秒杀破坏数值曲线）；
    - oil 油火：全体敌人立即火焰伤 + burning 2 回合（失败仅 main 敌 burning 1 回合）；
    - vines 藤蔓：全体敌人 entangled 1 回合（受困无法行动）；
    - rocks 落石：全体敌人确定性大额物理伤。
    返回 {"ok", "desc"}；效果日志写 session.log。"""
    env = str(getattr(session, "environment", "") or "")
    if env not in _ENV_TYPES or getattr(session, "environment_used", False) \
            or session.state != "active":
        return {"ok": False, "desc": "此处已无可借之势"}
    session.environment_used = True
    p, e = session.player, session.enemy
    name = env_display_name(env, session.genre_id, seed=session.round)
    enemies = living_enemy_units(session)
    if env == "cliff":
        chance = max(0.25, min(0.85, 0.55 + (p.level - e.level) * 0.05))
        if not rng.chance(chance):
            desc = f"你试图把敌手逼向{name}，却被其稳住身形化解了。"
            session.log.append(desc)
            return {"ok": True, "desc": desc}
        dmg = max(1, int(session.enemy.hp) // 2)
        session.enemy.hp = max(1, int(session.enemy.hp) - dmg)
        if not session.boss_fight and not _living_minions(session):
            session.state = "fled"
            desc = f"你把敌手逼落{name}！其摔得大残（-{dmg}），仓皇败退而去。"
        else:
            desc = f"你把敌手逼向{name}！其坠落受创（-{dmg}），狼狈爬起。"
        session.log.append(desc)
        return {"ok": True, "desc": desc}
    if env == "oil":
        ok = rng.chance(0.7)
        if ok:
            dmg = 8 + int(p.level) * 3 + rng.roll(0, 6)
            parts = []
            for u in enemies:
                u.snapshot.hp = max(0, int(u.snapshot.hp) - dmg)
                _unit_burn(u, "burning", 2)
                parts.append(f"{u.name} -{dmg}")
            _inflict_condition(session, "enemy", "burning", 2, source_element="fire")
            desc = f"你掀翻{name}，烈焰轰然吞没敌阵：{'、'.join(parts)}，敌人陷入燃烧。"
        else:
            _inflict_condition(session, "enemy", "burning", 1, source_element="fire")
            desc = f"你点燃了{name}，火势只燎到了主敌。"
        session.log.append(desc)
        _refresh_battle_state(session)
        return {"ok": True, "desc": desc}
    if env == "vines":
        if rng.chance(0.65):
            for u in enemies:
                _unit_burn(u, "entangled", 1)
            _inflict_condition(session, "enemy", "entangled", 1)
            desc = f"你诱敌深入{name}，敌人被死死缠住，动弹不得。"
        else:
            desc = f"你试图借{name}缠敌，却被对方抢先挣脱了。"
        session.log.append(desc)
        return {"ok": True, "desc": desc}
    # rocks
    if rng.chance(0.75):
        dmg = 12 + int(p.level) * 4 + rng.roll(0, 6)
        parts = [f"{u.name} -{dmg}" for u in enemies]
        for u in enemies:
            u.snapshot.hp = max(0, int(u.snapshot.hp) - dmg)
        desc = f"你撬动{name}，乱石轰然砸落：{'、'.join(parts)}。"
    else:
        desc = f"你撬动{name}，落石砸了个空，只扬起一片烟尘。"
    session.log.append(desc)
    _refresh_battle_state(session)
    return {"ok": True, "desc": desc}


# ---- [P40] Boss 战斗内阶段（66% 狂暴 / 33% 召唤狂卫；引擎驱动，无 LLM） ----

def _check_boss_phase(session: CombatSession) -> str:
    """主敌 Hp 阈值跨越各触发一次（仅 boss_fight 且战斗进行中）。返回日志片段（空=未触发）。

    phase1（<=66%）：狂暴——atk/magic_atk x1.25（+1 防 0 取整归零）。
    phase2（<=33%）：召唤 1 名狂卫（主敌快照 6 成克隆，is_minion 随从口径）。
    [!] 挂在 _refresh_battle_state 尾部：玩家/同伴/敌方任一行动后都会刷新状态，天然全覆盖。
    [修 2026-09-05] 主敌 hp<=0 不触发（被击杀瞬间 ratio=0 会让尸体「怒吼暴涨」并
    召狂卫——同 boss_mechanics_tick 死 Boss 口径）。
    [修 2026-09-08 真机] 满槽拒召 + 召唤叙事日志：phase2 准备召唤前先看场上是否还有
    空槽（含尸体槽），5 个槽位全被单位占满时 -> 拒绝召唤并播「感受到危机却无力召唤」
    日志（不让新单位跟尸体/活人叠在同位造成"诈尸叠影"）。召唤成功路径加两行日志
    让玩家看清「是谁感受到了危险 + 召出了谁 + 多少血」。"""
    if not getattr(session, "boss_fight", False) or session.state != "active":
        return ""
    if session.enemy.hp <= 0:
        return ""
    boss_name = (session.enemy_units or [None])[0]
    boss_name = getattr(boss_name, "name", "") or "敌人"
    ratio = int(session.enemy.hp) / max(1, int(session.enemy.hp_max))
    if session.boss_phase == 0 and ratio <= 0.66:
        session.boss_phase = 1
        session.enemy.atk = int(session.enemy.atk * 1.25) + 1
        session.enemy.magic_atk = int(session.enemy.magic_atk * 1.25)
        return f"{boss_name}怒吼一声，气息陡然暴涨（攻击提升）！"
    if session.boss_phase <= 1 and ratio <= 0.33:
        # [修 2026-09-08 真机] 满槽拒召：先看是否还有空槽可落，5 槽全被单位占用（含
        # 尸体槽）-> 拒召，不让新单位跟尸体/活人叠位造成视觉诈尸
        if _summon_has_free_slot(session):
            session.boss_phase = 2
            import dataclasses as _dc
            clone = _dc.replace(
                session.enemy,
                hp=max(1, int(session.enemy.hp_max * 0.6)),
                hp_max=max(1, int(session.enemy.hp_max * 0.6)),
                atk=max(1, int(session.enemy.atk * 0.6)),
                magic_atk=max(1, int(session.enemy.magic_atk * 0.6)),
                def_=max(1, int(session.enemy.def_ * 0.6)),
            )
            guard = CombatUnit(name=f"{boss_name}的狂卫", npc_id="",
                               snapshot=clone, mp=0, mp_max=0, skills=[],
                               cd={}, talents=[], ai="aggressive", role_label="狂卫")
            guard.is_minion = True
            # [!] 直接 append 到 session.enemy_units 本体——`or []` 在空表时 append 落到
            # 一次性新 list，Boss 33% 召唤会静默丢失
            if not isinstance(session.enemy_units, list):
                session.enemy_units = []
            _assign_summon_slot(session, guard)
            session.enemy_units.append(guard)
            # 两行日志：让玩家看清「是谁感受到了危险 + 召出了谁 + 多少血」；
            # \n 让 _refresh_battle_state 拼回合日志时分行展示
            return (f"{boss_name}感受到危机，振臂长啸——一名狂卫应声加入战团！\n"
                    f"　召唤出「{guard.name}」（{clone.hp}HP）")
        # 满槽拒召：不召，只播一行叙事让玩家看到发生了什么；
        # boss_phase 不递增，下次跨过死亡线 won 即可（避免重复触发 phase2）
        return f"{boss_name}感受到危机，却再也无力召唤同伴"
    return ""


def _summon_has_free_slot(session: "CombatSession") -> bool:
    """[修 2026-09-08 真机] 召唤前槽位预检：返回 True 表示至少还有一个真空槽可落。

    5 个槽（0/1/2/3/4 = 2-1-2 布局）。"真空"= 槽位上没有任何单位（不论死活——尸体槽
    也算占用，避免新单位跟尸体叠位造成视觉诈尸）。全部占用 -> False（拒召）。
    """
    used = {int(getattr(u, "slot", 0) or 0)
            for u in (session.enemy_units or [])}
    return any(s not in used for s in (0, 1, 2, 3, 4))


def _tick_cd(cd_dict: dict):
    """该 entity 行动后：所有技能冷却 -1，到 0 移除。"""
    for k in list(cd_dict.keys()):
        try:
            v = int(cd_dict[k]) - 1
        except (TypeError, ValueError):
            v = 0
        if v <= 0:
            cd_dict.pop(k, None)
        else:
            cd_dict[k] = v


def _flee_chance(player_speed: int, enemy_speed: int, base: float = 0.45,
                 luck: int = 0) -> float:
    """逃跑成功率：clamp(base + (玩家速度-敌人速度)*0.02 + luck*0.003, 0.1, 0.9)。

    [P8] luk 显式加成（speed 已含 luk//2，此处再补全值 luk 的零头，让高运角色更易脱战）。
    dex 经 speed（= dex + luk//2）已生效。
    """
    luck_bonus = max(0, int(luck)) * 0.003
    return max(0.1, min(0.9, base + (int(player_speed) - int(enemy_speed)) * 0.02 + luck_bonus))


# [堆叠 2026-09-13 用户定稿] 背包同种物品堆叠上限：每件占 1 格，第 4 件起拒收。
# 不区分物品类型（消耗品/材料/装备一视同仁），玩家与 NPC 背包同口径。
_INV_STACK_LIMIT = 3


def carry_capacity(entity: Any) -> int:
    """[P8] 负重上限 = 20 + stat_vit*2 格（vit 越高背越多）。

    player.inventory 是 item_id 列表，**同种最多 _INV_STACK_LIMIT 件、每件占 1 格**，
    故 len(inv) 是「件数」而非「种类数」（总格数上限不变，只是允许同种重复）。
    超过上限时采集/掉落/奇遇获物会被拒收（调用方据返回值提示「背包已满」）。
    纯函数，vit 驱动。
    """
    s_vit = te.effective_stat(entity, "vit")
    return 20 + max(0, s_vit) * 2


def try_add_to_inventory(player: Any, item_id: str) -> bool:
    """[!] 入包统一口径（同种堆叠上限 + 负重上限）：战斗掉落/任务奖励/买入/合成产出/
    搜刮走此 helper——此前只有采集/奇遇/送礼查 carry_capacity，战斗等主渠道直接
    append 可无限超载，负重系统被架空。返回 False = 背包满拒收（调用方提示）。

    [堆叠 2026-09-13 用户定稿] 同种物品最多 _INV_STACK_LIMIT(=3) 件，每件占 1 格。
    [!] 本 helper 只管「新获得」路径；互赠/互市付货款/遗失物归还这类**守恒转移**
    直接 append 不走这里——给转移加上限会让物品凭空湮灭。
    """
    inv = getattr(player, "inventory", None)
    if inv is None or not item_id:
        return False
    if inv.count(item_id) >= _INV_STACK_LIMIT:
        return False
    if len(inv) >= carry_capacity(player):
        return False
    inv.append(item_id)
    return True


# ---- [P12] 技能成长（熟练度体系）----
_SKILL_MAX_LEVEL = 5   # [用户定稿 2026-08-28] 满级 5


def skill_level(skill: dict) -> int:
    """读技能等级（缺省 1，脏值钳 1-5）。"""
    try:
        return max(1, min(_SKILL_MAX_LEVEL, int((skill or {}).get("level", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def skill_power_mult(skill: dict) -> float:
    """技能威力随等级缩放：每级 +15%（Lv1=1.0 ... Lv5=1.6）。"""
    return 1.0 + 0.15 * (skill_level(skill) - 1)


def skill_xp_needed(level: int) -> int:
    """升到下一级所需熟练度：100 + 50*当前等级（[2026-09-10 用户指示] 施展升级
    再慢一档：平均 +12/次 -> Lv1→2 需 150≈13 次，满级共 900≈75 次（约 12-15 场
    战斗），与 cap5 配套；旧 80+40（36 次）整体放慢约 2 倍）。"""
    return 100 + 50 * max(1, int(level))


# [2026-09-10 用户指示] 每次施展随机加点（替代固定 +20）：区间 8-16（平均 12），
# 与阈值放慢配套（进度随机 + 更慢，长线养成感）。
_SKILL_XP_GAIN_RANGE = (8, 16)


def roll_skill_xp_gain(skill: dict, world_id: str = "") -> int:
    """本次施展的熟练度增量（确定性随机 8-16，零战斗 rng 序列消费）。

    盐 = 世界 id + 技能名 + 当前等级 + 当前熟练度——熟练度每次施展都变，天然
    「盐含尝试次数」（守 §15 UI 连点锁死教训），同状态同结果可回放。
    """
    try:
        lo, hi = _SKILL_XP_GAIN_RANGE
        from src.utils.rng import SeededRng as _SR
        r = _SR.seed_from(str(world_id or "combat"), 0,
                          f"skillxp_{skill.get('name', '') if isinstance(skill, dict) else ''}"
                          f"_{skill_level(skill)}_{max(0, int(skill.get('xp', 0) or 0))}")
        return r.roll(lo, hi)
    except Exception:
        return ( _SKILL_XP_GAIN_RANGE[0] + _SKILL_XP_GAIN_RANGE[1]) // 2


def jitter_taught_skill(teach: dict, world_id: str = "", book_item_id: str = "") -> dict:
    """[技能书个体差异 2026-09-10 用户指示] 品级定基准 + 每件确定性抖动（仿
    _fill_item_defaults 口径）：书内技能 power 按书 item.id 派生 SeededRng 在基准
    ±20% 抖动——同技能不同副本威力不同（同 id 同结果可回放）。

    返回抖动后的技能 dict 副本；[!] 不改书内 teach_skill 模板（Item 是蓝图目录，
    多实体同 id 共享，原地改会串味所有持有者）。power<=0 保留（from_dict 钳制兜底）。
    """
    sk = dict(teach or {})
    base = int(sk.get("power", 0) or 0)
    if base <= 0:
        return sk
    from src.utils.rng import SeededRng as _SR
    r = _SR.seed_from(str(world_id or "w"), 0, f"skillbook_{book_item_id}")
    # int() 截断与装备 _jitter 同口径（不用 round——20x1.19 截 23 不进 24）
    sk["power"] = max(1, int(base * (0.8 + 0.4 * r.random())))
    return sk


def grant_skill_xp(skill: dict, amount: int = 20) -> str:
    """给技能加熟练度；满阈值升级（cap Lv5）。返回升级日志（未升级返回空串）。

    技能 dict 是 PlayerState.skills 里的引用，升级原地生效随 save_world 持久化。
    """
    if not isinstance(skill, dict):
        return ""
    lv = skill_level(skill)
    if lv >= _SKILL_MAX_LEVEL:
        return ""
    try:
        xp = max(0, int(skill.get("xp", 0) or 0)) + max(0, int(amount))
    except (TypeError, ValueError):
        xp = max(0, int(amount))
    if xp < skill_xp_needed(lv):
        skill["xp"] = xp
        return ""
    skill["xp"] = xp - skill_xp_needed(lv)
    skill["level"] = lv + 1
    name = str(skill.get("name", "技能")) or "技能"
    return f"（{name}熟练度提升 -> {lv + 1} 级，威力 +15%）"


def resolve_skill(session: CombatSession, skill: dict, caster: str,
                  rng: SeededRng, telegraphed: bool = False) -> str:
    """[P7h] 结算技能释放。caster="player"/"enemy"。原地改 session（mp/hp/cd/state）。返回日志。

    telegraphed：本动作在【敌情】预告过（P49 覆写路径）——敌方技能瞄准玩家且玩家举盾时
    触发「预判卸力」x0.7（[P59 counterplay] 技能本绕对撞口径，预告让防御有了意义）。
    默认 False 既有调用零位移。

    技能效果（纯 Python）：
    - attack 类：基础 = power + 缩放属性（str->atk/2, int->magic_atk, dex->atk/3），暴击走 crit_rate，
      伤害 = max(1, base*(crit_dmg if crit) - target.def*0.4)，技能必中（强技能特点）。
    - heal 类：治疗量 = power + caster.heal_bonus，clamp hp_max。
    - buff 类：暂作小幅增益描述（后续可扩展临时 atk/def 提升）。
    """
    is_player = caster == "player"
    snap_c = session.player if is_player else session.enemy
    name = str(skill.get("name", "技能")) or "技能"
    stype = skill.get("type", "attack") or "attack"
    power = max(0, int(skill.get("power", 10)))
    # [P12] 技能成长：威力随熟练度等级缩放（Lv.n 每级 +15%）
    power = int(power * skill_power_mult(skill))
    scaling = skill.get("stat_scaling", "") or ""
    # 扣 mp（调用方应已校验够 mp）
    cost = max(0, int(skill.get("cost_mp", 0) or 0))
    if is_player:
        session.player_mp = max(0, session.player_mp - cost)
    else:
        session.enemy_mp = max(0, session.enemy_mp - cost)
    # 设冷却
    cd = max(0, int(skill.get("cooldown", 0) or 0))
    sid = str(skill.get("id", name))
    if cd > 0:
        (session.player_cd if is_player else session.enemy_cd)[sid] = cd
    who = "你" if is_player else "敌人"
    caster_side = "player" if is_player else "enemy"
    if stype == "heal":
        before = snap_c.hp
        heal(snap_c, power, bonus=snap_c.heal_bonus)
        amount = snap_c.hp - before
        # [C10 修复 2026-08-25] heal 类技能同样施加 inflicts（对施法者，与 buff 分支同口径）
        inf_log = _apply_skill_inflicts(session, caster_side, skill, rng)
        log = f"{who}施展{name}，回复 {amount} 点生命（{snap_c.hp}/{snap_c.hp_max}）"
        if inf_log:
            log += f"（{inf_log}）"
        return log
    if stype == "buff":
        # [P30] buff 技能读 inflicts 对施法者自身附加状态（protected/enraged 等增益）
        inf_log = _apply_skill_inflicts(session, caster_side, skill, rng)
        log = f"{who}施展{name}，进入增益状态"
        if inf_log:
            log += f"（{inf_log}）"
        return log
    # attack 类
    base = power
    if scaling == "str":
        base += snap_c.atk // 2
    elif scaling == "int":
        base += snap_c.magic_atk
    elif scaling == "dex":
        base += snap_c.atk // 3
    # [P9] 天赋 skill_dmg_mult（按 skill.element 匹配天赋 element：雷天灵根只加成雷系功法）
    sk_element = str(skill.get("element", "") or "")
    tal_mult = te.get_talent_mult(
        session.player_talents if is_player else session.enemy_talents, "skill_dmg_mult", sk_element)
    base = int(base * tal_mult)
    # [P30] 解析目标（single / aoe_enemy / aoe_all / random_n）
    pattern = str(skill.get("target_pattern", "single") or "single")
    targets = _resolve_skill_targets(session, caster, pattern, rng, skill)  # [(snap, conds, tname, side), ...]
    caster_conds = _conds_of(session, caster_side)
    logs = []
    for idx, (t_snap, t_conds, t_name, t_side) in enumerate(targets):
        # [P25b] 敌方攻击技能 vs 玩家：defend 同伴概率挡刀（仅单体路径；AOE 不挡）
        if not is_player and pattern == "single" and t_side == "player" and idx == 0:
            guard = _defend_guard(session, rng)
            if guard is not None:
                # 挡刀：伤害按本目标结算后减半打守护者（确定性，guard 不吃 inflicts/react）
                crit = rng.chance(snap_c.crit_rate + guard.snapshot.incoming_crit_bonus)
                b = int(base * _cond_mult(caster_conds, "atk_mult", 1.0))
                if crit:
                    b = int(b * snap_c.crit_dmg)
                elem_mult, elem_tag = element_damage_mult(sk_element, _effective_elements(t_snap, t_conds))
                dmg = max(1, int((b - t_snap.def_ * 0.4) * elem_mult * (rng.random() * 0.2 + 0.9)))
                g_dmg = max(1, int(dmg * 0.5))
                guard.snapshot.hp = max(0, guard.snapshot.hp - g_dmg)
                g_log = (f"{guard.name}挺身替你挡下「{name}」，受到 {g_dmg} 点伤害"
                         + ("（暴击！）" if crit else "") + element_tag_text(elem_tag))
                if guard.snapshot.hp <= 0:
                    g_log += f"（{guard.name}被击倒）"
                return g_log
        # 单目标伤害结算（含状态乘数 + effective_elements）
        crit = rng.chance(snap_c.crit_rate + t_snap.incoming_crit_bonus)
        b = int(base * _cond_mult(caster_conds, "atk_mult", 1.0))  # [P30] enraged 攻 x1.3
        if crit:
            b = int(b * snap_c.crit_dmg)
        elem_mult, elem_tag = element_damage_mult(sk_element, _effective_elements(t_snap, t_conds))
        # [P30] def_mult（enraged 防御方防 x0.7）+ dmg_taken_mult（frozen x1.5 / protected x0.5）
        dmg = max(1, int((b - t_snap.def_ * 0.4 * _cond_mult(t_conds, "def_mult", 1.0))
                         * elem_mult))
        dmg = max(1, int(dmg * _cond_mult(t_conds, "dmg_taken_mult", 1.0)))
        # [P59 counterplay] 预判卸力：技能本无视防御（绕对撞口径），但本动作在【敌情】
        # 预告过且玩家举盾 -> 伤害 x0.7（读预告做反制的收益；无 rng 消费零位移）
        _tell = ""
        if (telegraphed and not is_player and t_side == "player"
                and getattr(session, "player_defending", False)):
            dmg = max(1, int(dmg * _COUNTERPARRY_SKILL_REDUCE))
            _tell = "（预判卸力）"
        # [部位 2026-08-31] 技能同掷部位骰（弱点同判；倍率后乘）
        part = roll_body_part(rng)
        weak = str(getattr(t_snap, "weakpoint", "") or "")
        dmg, is_weak, wp_crit = apply_body_part(rng, dmg, part, weak,
                                                snap_c.crit_rate + t_snap.incoming_crit_bonus,
                                                snap_c.crit_dmg)
        if wp_crit:
            crit = True
        # [修 2026-09-10] ±10% 浮动殿后（契约：先部位/弱点判定、最后才浮动）——原实现把浮动
        # 放在部位骰之前，与 resolve_attack 及 read.md 不变量区顺序契约相反（同 seed 抽签顺序不同）。
        if dmg > 0:
            dmg = max(1, int(dmg * (rng.random() * 0.2 + 0.9)))
        t_snap.hp = max(0, t_snap.hp - dmg)
        seg = f"对{t_name}造成 {dmg} 点伤害"
        if part:
            seg += f"（部位：{part_display(part)}" + ("·弱点！" if is_weak else "") + "）"
        if crit:
            seg += "（暴击！）"
        seg += _tell
        seg += element_tag_text(elem_tag)
        # [P30] 命中后：附加 inflicts + 元素互动反应（wet+thunder->shocked 等）
        inf_log = _apply_skill_inflicts(session, t_side, skill, rng)
        react_log = _react_elements(session, t_side, sk_element, rng)
        extra = "；".join(x for x in (inf_log, react_log) if x)
        if extra:
            seg += f"（{extra}）"
        # [P31] counter 反应：敌方技能命中同伴 -> 被击中同伴概率反击（反伤施法者一半伤害）
        if (not is_player and dmg > 0 and isinstance(t_side, CombatUnit)
                and t_side in (session.ally_units or []) and t_snap.hp > 0):
            cr = _check_reactions(session, "on_ally_hit", rng,
                                  attacker_snap=snap_c, damage=dmg, ally_unit=t_side)
            if cr:
                seg += f"；{t_name}反击{who}，造成 {cr['counter_dmg']} 点伤害"
                if snap_c.hp <= 0:
                    seg += f"（{who}被击倒）"
        if t_snap.hp <= 0:
            if is_player:
                _refresh_battle_state(session)
                if session.state == "won":
                    seg += f"（{t_name}被击败）"
                else:
                    seg += f"（{t_name}被击败，其余敌人仍在围攻）"
            else:
                if t_side == "player":
                    if _side_defeated(session):
                        session.state = "lost"
                    seg += f"（{session.player_label}被击倒）"
                else:
                    seg += f"（{t_name}被击倒）"
        logs.append(seg)
    if not logs:
        return f"{who}施展{name}，但没有可作用的目标"
    log = f"{who}施展{name}，" + "；".join(logs)
    return log


def _resolve_skill_targets(session: CombatSession, caster: str, pattern: str,
                           rng: SeededRng, skill: Optional[dict] = None) -> list:
    """[P30] 解析技能目标列表，返回 [(snap, conds, name, side), ...]。

    side 用于 _apply_skill_inflicts/_react_elements 取目标状态（"player"/"enemy"/CombatUnit）。
    - single：对方主目标（玩家方->主敌，敌方->玩家）
    - aoe_enemy：对方全体存活单位
    - aoe_all：敌我双方全体存活单位（双刃范围技）
    - random_n：对方 n 个随机存活单位（skill.target_count，默认 2）
    """
    is_player = caster == "player"
    if is_player:
        # [布局 2026-08-31] 敌方可选目标经前排门控过滤（后排在前排存活时不可选）
        opp = [(u.snapshot, u.conditions, u.name, u) for u in targetable_enemy_units(session)]
        if not opp:  # legacy 单敌：主敌挂 session.enemy_conditions
            opp = [(session.enemy, session.enemy_conditions, "敌人", "enemy")]
    else:
        # [修 2026-09-10] 敌方技能目标同走前排门控（原实现无条件 [玩家]+全体存活同伴，
        # 前排同伴未清时主敌技能照样糊后排玩家——契约把「技能目标」列为五路门控之一）。
        # player 判定与 _main_enemy_act 普攻分支 player_ok 同口径；空表兜底回玩家防无目标。
        opp = []
        if player_targetable(session):      # 单一来源门控（见 player_targetable）
            opp.append((session.player, session.player_conditions, "你", "player"))
        opp += [(u.snapshot, u.conditions, u.name, u)
                for u in living_ally_units(session)
                if unit_targetable(session, u, "ally")]
        if not opp and session.player.hp > 0:      # 数据异常兜底（理论不可达）
            opp = [(session.player, session.player_conditions, "你", "player")]
    if pattern == "aoe_all":
        # 追加己方存活单位（双刃：连自己人一起打）
        if is_player:
            opp += [(u.snapshot, u.conditions, u.name, u) for u in living_ally_units(session)]
        else:
            opp += [(u.snapshot, u.conditions, u.name, u) for u in living_enemy_units(session)]
        return opp
    if pattern == "random_n":
        n = 2
        if isinstance(skill, dict):
            try:
                n = max(1, int(skill.get("target_count", 2) or 2))
            except (TypeError, ValueError):
                n = 2
        picks = rng.sample(opp, min(n, len(opp))) if opp else []
        return picks
    if pattern == "aoe_enemy":
        return opp
    # single：仅主目标（opp[0]）
    return opp[:1]


def _skill_attack(caster_snap: CombatSnapshot, caster_talents: list, skill: dict,
                  target_snap: CombatSnapshot, rng: SeededRng,
                  caster_name: str, target_name: str,
                  caster_conds: Optional[list] = None,
                  target_conds: Optional[list] = None) -> tuple[str, bool]:
    """[P10] attack 类技能通用结算（玩家/同伴/随从共用）。

    不扣 mp/不设冷却（调用方负责），只算伤害与目标倒下。返回 (日志, 目标是否倒下)。
    [P30] caster_conds/target_conds：状态减益/增益算入结算（enraged/shocked/blinded/frozen/
    protected 等），并经 _effective_elements 让 wet 目标吃雷克。调用方负责 inflicts/react（_unit_cast_skill）。
    """
    name = str(skill.get("name", "技能")) or "技能"
    power = max(0, int(skill.get("power", 10)))
    # [P12] 技能成长：威力随熟练度等级缩放（多敌/同伴路径同口径）
    power = int(power * skill_power_mult(skill))
    scaling = skill.get("stat_scaling", "") or ""
    base = power
    if scaling == "str":
        base += caster_snap.atk // 2
    elif scaling == "int":
        base += caster_snap.magic_atk
    elif scaling == "dex":
        base += caster_snap.atk // 3
    sk_element = str(skill.get("element", "") or "")
    tal_mult = te.get_talent_mult(caster_talents, "skill_dmg_mult", sk_element)
    base = int(base * tal_mult)
    crit = rng.chance(caster_snap.crit_rate + target_snap.incoming_crit_bonus)
    if crit:
        base = int(base * caster_snap.crit_dmg)
    # [P30] 攻击方状态（enraged 攻 x1.3）+ 目标状态 def_mult（enraged 防 x0.7）/ dmg_taken_mult（frozen/protected）
    base = int(base * _cond_mult(caster_conds, "atk_mult", 1.0))
    # [P9] 元素克制矩阵（多敌/同伴路径同口径；[P30] wet 经 _effective_elements 暴露 water 吃雷克）
    elem_mult, elem_tag = element_damage_mult(sk_element, _effective_elements(target_snap, target_conds))
    dmg = max(1, int((base - target_snap.def_ * 0.4 * _cond_mult(target_conds, "def_mult", 1.0))
                     * elem_mult))
    dmg = max(1, int(dmg * _cond_mult(target_conds, "dmg_taken_mult", 1.0)))
    # [部位 2026-08-31] 技能同掷部位骰（弱点同判）
    part = roll_body_part(rng)
    weak = str(getattr(target_snap, "weakpoint", "") or "")
    dmg, is_weak, wp_crit = apply_body_part(rng, dmg, part, weak,
                                            caster_snap.crit_rate + target_snap.incoming_crit_bonus,
                                            caster_snap.crit_dmg)
    if wp_crit:
        crit = True
    # [修 2026-09-10] ±10% 浮动殿后，与 resolve_attack/resolve_skill 同序（契约：部位在前浮动在后）
    if dmg > 0:
        dmg = max(1, int(dmg * (rng.random() * 0.2 + 0.9)))
    target_snap.hp = max(0, target_snap.hp - dmg)
    log = f"{caster_name}施展{name}，对{target_name}造成 {dmg} 点伤害"
    if part:
        log += f"（部位：{part_display(part)}" + ("·弱点！" if is_weak else "") + "）"
    if crit:
        log += "（暴击！）"
    log += element_tag_text(elem_tag)
    return log, target_snap.hp <= 0


def _unit_available_skills(unit: CombatUnit) -> list:
    """该单位当前可用技能（mp 够 + 冷却好）。"""
    out = []
    for sk in (unit.skills or []):
        if not isinstance(sk, dict):
            continue
        if int(sk.get("cost_mp", 0) or 0) > unit.mp:
            continue
        if unit.cd.get(str(sk.get("id", sk.get("name", ""))), 0) > 0:
            continue
        out.append(sk)
    return out


def choose_unit_action(unit: CombatUnit, rng: SeededRng) -> tuple:
    """[P10] 单位 AI 决策（随从/同伴通用，4 pattern 与主敌同口径）。

    返回 (action, skill|None)。action: attack/skill/defend。
    """
    ai = unit.ai if unit.ai in ("aggressive", "defensive", "caster", "balanced") else "balanced"
    hp_ratio = unit.hp_ratio
    available = _unit_available_skills(unit)
    if ai == "defensive":
        if hp_ratio < 0.3:
            heal_sk = next((s for s in available if s.get("type") == "heal"), None)
            if heal_sk:
                return "skill", heal_sk
            # [对撞 2026-09-01] 防御 AI 低血改 35% 概率防御（原 100%——对撞后防御收益
            # 跳变，全程龟缩会拖节奏；不再每次必防）
            if rng.chance(0.35):
                return "defend", None
            return "attack", None
        return "attack", None
    if ai == "caster":
        magic = [s for s in available if s.get("damage_type") == "magical"]
        if magic:
            return "skill", rng.pick(magic)
        atk = [s for s in available if s.get("type") == "attack"]
        return ("skill", rng.pick(atk)) if atk else ("attack", None)
    if ai == "aggressive":
        # [对撞 2026-09-01] 激进 AI 低血 10% 偶发防御（挣扎一下）
        if hp_ratio < 0.2 and rng.chance(0.10):
            return "defend", None
        atk_sk = [s for s in available if s.get("type") == "attack"]
        if atk_sk and rng.chance(0.6):
            return "skill", rng.pick(atk_sk)
        return "attack", None
    # balanced
    if available and rng.chance(0.4):
        return "skill", rng.pick(available)
    if hp_ratio < 0.2 and rng.chance(0.3):
        return "defend", None
    return "attack", None


def _unit_pay_skill_cost(unit: CombatUnit, skill: dict):
    """单位释放技能的代价结算（扣 mp + 设冷却）。"""
    unit.mp = max(0, unit.mp - max(0, int(skill.get("cost_mp", 0) or 0)))
    cd = max(0, int(skill.get("cooldown", 0) or 0))
    sid = str(skill.get("id", skill.get("name", "")))
    if cd > 0:
        unit.cd[sid] = cd


def _unit_cast_skill(session: CombatSession, unit: CombatUnit, skill: dict,
                     target_snap: CombatSnapshot, target_name: str, rng: SeededRng,
                     on_player_side: bool) -> str:
    """单位（随从/同伴）释放技能：heal 自愈 / attack 打目标。返回日志。"""
    _unit_pay_skill_cost(unit, skill)
    stype = skill.get("type", "attack") or "attack"
    if stype == "heal":
        before = unit.snapshot.hp
        heal(unit.snapshot, max(0, int(skill.get("power", 10))),
             bonus=unit.snapshot.heal_bonus)
        amount = unit.snapshot.hp - before
        # [C10 修复 2026-08-25] heal 类技能同样施加 inflicts（与 buff 分支/主路径同口径）
        inf_log = _apply_skill_inflicts(session, unit, skill, rng)
        log = f"{unit.name}施展{skill.get('name', '治疗')}，回复 {amount} 点生命（{unit.snapshot.hp}/{unit.snapshot.hp_max}）"
        if inf_log:
            log += f"（{inf_log}）"
        return log
    if stype == "buff":
        # [P30] buff 技能读 inflicts 对施法者自身附加状态（protected/enraged 等）
        inf_log = _apply_skill_inflicts(session, unit, skill, rng)
        log = f"{unit.name}施展{skill.get('name', '强化')}，进入增益状态"
        if inf_log:
            log += f"（{inf_log}）"
        return log
    # [P30] 目标状态：on_player_side=True 打敌方 -> target_snap 找敌方单位的 conds（命中 session.player 则 player_conditions）
    if target_snap is session.player:
        t_side = "player"
        t_conds = session.player_conditions
    else:
        t_unit = next((u for u in (session.enemy_units or []) + (session.ally_units or [])
                       if getattr(u, "snapshot", None) is target_snap), None)
        # [!] 主敌 unit.snapshot is session.enemy 且 conditions 共享 session.enemy_conditions（start_combat 注入），
        # 找到 unit 用其 conditions（=enemy_conditions）；未找到（target 是 session.enemy 但无单位包装）回退 enemy_conditions。
        t_side = t_unit if t_unit is not None else "enemy"
        t_conds = t_unit.conditions if t_unit is not None else session.enemy_conditions
    log, down = _skill_attack(unit.snapshot, unit.talents, skill, target_snap, rng,
                              unit.name, target_name,
                              caster_conds=unit.conditions, target_conds=t_conds)
    # [P30] 命中后附加 inflicts + 元素互动反应
    sk_element = str(skill.get("element", "") or "")
    inf_log = _apply_skill_inflicts(session, t_side, skill, rng)
    react_log = _react_elements(session, t_side, sk_element, rng)
    extra = "；".join(x for x in (inf_log, react_log) if x)
    if extra:
        log += f"（{extra}）"
    if down:
        if on_player_side:
            _refresh_battle_state(session)
            if session.state == "won":
                log += f"（{target_name}被击败）"
        else:
            if target_snap is session.player:
                if _side_defeated(session):
                    session.state = "lost"
                log += f"（{session.player_label}被击倒）"
            else:
                log += f"（{target_name}被击倒）"
    return log


def _defend_guard(session: CombatSession, rng: SeededRng) -> Optional[CombatUnit]:
    """[P25b/P31] defend 指令同伴替玩家挡刀（generalize 为 block 反应，走反应预算）。

    走 _check_reactions(trigger="on_enemy_attack_player")：查 defend 同伴 + 50% roll + 反应预算
    （每同伴每回合一次，block/counter/cleanse 共享）。通过则置 reaction_used=True 返回守护者。
    多守护者取第一个（排队挡刀）。[!] 无 defend 同伴时不消费 rng（保战斗回放与旧版逐位一致）。
    """
    res = _check_reactions(session, "on_enemy_attack_player", rng)
    return res.get("guard") if res else None


# ============ [P31] 反应系统 ============
# 反应注册表：抽象 key -> {trigger, chance}。v1 三种：block（挡刀）/counter（反击）/cleanse（净化）。
# 反应能力来源：block=ally_orders=="defend"；counter=有 attack 技能；cleanse=有 heal 技能。
# 反应预算：每同伴每回合一次（block/counter/cleanse 共享 unit.reaction_used；回合末 _apply_conditions 重置）。
_REACTION_DEFS = {
    "block":   {"trigger": "on_enemy_attack_player",        "chance": 0.5},
    "counter": {"trigger": "on_ally_hit",                    "chance": 0.5},
    "cleanse": {"trigger": "on_condition_applied_friend",    "chance": 0.5},
}


def _unit_has_skill_type(unit: CombatUnit, stype: str) -> bool:
    """单位是否拥有指定 type 的技能（反应能力来源判断）。"""
    return any(isinstance(s, dict) and s.get("type") == stype for s in (unit.skills or []))


def _unit_can_react(unit: CombatUnit) -> bool:
    """[P31/Q3] 单位是否可反应（未被 stunned/frozen 控制住）。

    被震慑/冰封的单位无法做出反应动作（挡刀/反击/净化），与"被控制无法行动"语义一致。
    """
    return not (_has_cond(unit.conditions, "stunned") or _has_cond(unit.conditions, "frozen"))


def _check_reactions(session: CombatSession, trigger: str, rng: SeededRng,
                     **ctx) -> Optional[dict]:
    """[P31] 统一反应触发入口。遍历存活同伴，按 trigger 匹配反应类型 + 能力来源 + 预算，
    roll 通过则触发，置 reaction_used=True，返回结果 dict（None=无反应）。

    - trigger="on_enemy_attack_player"（block）：找第一个 defend 同伴 -> roll -> 返回
      {"type":"block","guard":unit}。[!] block 不消费 reaction_used——挡刀是 defend 指令核心
      职责（每回合可多次挡，保 P25b 行为不回归）；只有 counter/cleanse 走每回合一次预算。
    - trigger="on_ally_hit"（counter）：ctx 须传 attacker_snap/damage/ally_unit。被击中同伴
      （ally_unit）若有 attack 技能 + 未用预算 -> roll -> 反伤攻击者，返回
      {"type":"counter","unit":ally_unit,"counter_dmg":int}。counter 是被击中者自己反击。
    - trigger="on_condition_applied_friend"（cleanse）：ctx 须传 target_side/conds。找第一个有 heal
      技能 + 未用预算的同伴 -> roll -> 清除 target_side 一个负面状态（control>dot>debuff 优先），
      返回 {"type":"cleanse","unit":unit,"cleared":key or None}。
    """
    chance = _REACTION_DEFS.get(
        next((k for k, v in _REACTION_DEFS.items() if v["trigger"] == trigger), ""),
        {}, ).get("chance", 0.5)
    # block：遍历找守护者（不消费预算，保 P25b 多次挡刀行为）
    if trigger == "on_enemy_attack_player":
        for u in living_ally_units(session):
            if str((session.ally_orders or {}).get(u.npc_id, "free") or "free") != "defend":
                continue
            if not _unit_can_react(u):  # [Q3] 被控制的同伴不能挡刀
                continue
            if rng.chance(chance):
                return {"type": "block", "guard": u}
        return None
    # counter：被击中者自己反击（不遍历；走预算）
    if trigger == "on_ally_hit":
        ally = ctx.get("ally_unit")
        if not isinstance(ally, CombatUnit) or not ally.alive or ally.reaction_used:
            return None
        if not _unit_can_react(ally):  # [Q3] 被控制的同伴不能反击
            return None
        if not _unit_has_skill_type(ally, "attack"):
            return None
        if rng.chance(chance):
            dmg = max(1, int(ctx.get("damage", 0) or 0) // 2)
            attacker_snap = ctx.get("attacker_snap")
            if attacker_snap is not None:
                attacker_snap.hp = max(0, attacker_snap.hp - dmg)
            ally.reaction_used = True
            return {"type": "counter", "unit": ally, "counter_dmg": dmg}
        return None
    # cleanse：遍历找净化者（走预算）
    if trigger == "on_condition_applied_friend":
        conds = ctx.get("conds") or []
        if not conds:
            return None
        # 候选负面状态（control>dot>debuff 优先清除）
        priority = {"control": 0, "dot": 1, "debuff": 2}
        neg = []
        for c in conds:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key", "") or "")
            d = _CONDITION_DEFS.get(key, {})
            t = d.get("type", "")
            if t in priority:
                neg.append((priority[t], key))
        if not neg:
            return None
        for u in living_ally_units(session):
            if u.reaction_used or not _unit_has_skill_type(u, "heal"):
                continue
            if not _unit_can_react(u):  # [Q3] 被控制的同伴不能净化
                continue
            if rng.chance(chance):
                neg.sort(key=lambda x: x[0])
                cleared = neg[0][1]
                _remove_cond(conds, cleared)
                u.reaction_used = True
                return {"type": "cleanse", "unit": u, "cleared": cleared}
        return None
    return None


def _apply_block(guard: CombatUnit, attacker_name: str, r) -> str:
    """[P25b] 挡刀结算（普攻路径）：未命中原样带过；命中伤害减半打守护同伴。"""
    if not r.hit:
        return f"{guard.name}替你挡下了{attacker_name}的攻击（未命中）"
    dmg = max(1, int(r.damage * 0.5))
    guard.snapshot.hp = max(0, guard.snapshot.hp - dmg)
    log = (f"{guard.name}挺身替你挡下{attacker_name}的攻击，受到 {dmg} 点伤害"
           + ("（暴击！）" if r.crit else ""))
    if guard.snapshot.hp <= 0:
        log += f"（{guard.name}被击倒）"
    return log


def _apply_sworn_bonus(session: "CombatSession", target_snap, r, bonus: float) -> int:
    """[P26a] 结义联手加成：对普攻结果 r 乘 bonus（>1.0），重算 hp/defeated 标志。

    放大 r.damage，回扣 r.defender_hp_after（hp 钳>=0），并据新 hp 重算
    r.defender_defeated（放大可能把「未击杀」推过击杀线，须同步否则 _refresh_battle_state
    漏触发、0 血敌人继续行动）。target_snap.hp 由调用方据 r.defender_hp_after 覆盖，此处不动。
    返回新伤害值（供日志展示）。bonus<=1.0 / 未命中 / 0 伤害时直接返回原值不改血量。
    [!] 仅普攻路径调用（技能路径 _skill_attack/resolve_skill 内部结算难干净后处理，v1 豁免，
    保「加成必生效」口径统一，仿 P25b defend 挡刀随从技能不挡）。
    """
    if bonus <= 1.0 or not r.hit or r.damage <= 0:
        return r.damage
    new_dmg = max(1, int(r.damage * bonus))
    delta = new_dmg - int(r.damage)
    r.damage = new_dmg
    r.defender_hp_after = max(0, int(getattr(r, "defender_hp_after", 0) or 0) - delta)
    # [!] 同步重算 defeated 标志：放大后越线则击杀生效（调用方据此 _refresh_battle_state）
    try:
        r.defender_defeated = (r.defender_hp_after <= 0)
    except (AttributeError, TypeError):
        pass
    return new_dmg


def ally_turn(session: CombatSession, rng: SeededRng) -> str:
    """[P10] 同伴回合：玩家行动后、敌方行动前，每个存活同伴行动一次。

    行为：低血且会治疗 -> 优先治疗（自己或玩家中血量比例更低者）；否则攻击存活敌人
    （优先血量最低的敌方单位）。同伴伤害按玩家方难度系数（is_player_attacker=True）。
    [P25b] 指令（ally_orders[npc_id]）：retreat=撤出战斗不再行动（从 ally_units 移除，
    战后不写回不疲劳）；defend=纯守势：不攻击也不治疗，敌方攻击玩家（主敌普攻/主敌
    技能/随从普攻三路）时 50% 挡刀且伤害减半；focus=集火玩家本回合目标（目标倒下
    回退最低血）；free=上述默认 AI。
    返回日志（多行拼接，已 append 到 session.log）。
    """
    if session.state != "active":
        return ""
    # [战报] 行动前敌方 hp 快照（同伴输出也计入玩家侧 dmg_dealt）
    _ehps = _enemy_hps(session)
    logs = []
    enemies = living_enemy_units(session)
    for unit in list(session.ally_units or []):
        if session.state != "active" or not enemies:
            break
        if not unit.alive:
            continue
        # [P30] 控制状态：stunned/frozen 跳过行动
        if _has_cond(unit.conditions, "stunned") or _has_cond(unit.conditions, "frozen"):
            logs.append(f"{unit.role_label or '同伴'}{unit.name}陷入{condition_display('stunned', session.genre_id) if _has_cond(unit.conditions,'stunned') else condition_display('frozen', session.genre_id)}，无法行动")
            _tick_cd(unit.cd)
            continue
        # [P25b] 指令分发（宠物单位 npc_id 空 -> 未登记 -> free，不受指令控制）
        order = str((session.ally_orders or {}).get(unit.npc_id, "free") or "free")
        if order == "retreat":
            if unit in (session.ally_units or []):
                session.ally_units.remove(unit)
            logs.append(f"{unit.role_label or '同伴'}{unit.name}奉命撤出了战斗")
            continue
        if order == "defend":
            logs.append(f"{unit.role_label or '同伴'}{unit.name}持守势护在你身前")
            _tick_cd(unit.cd)
            continue
        # 治疗判定：玩家或自己血量比例 < 0.4 且有治疗技能
        heal_sk = next((s for s in _unit_available_skills(unit) if s.get("type") == "heal"), None)
        p_ratio = session.player.hp / max(1, session.player.hp_max)
        want_heal = heal_sk is not None and min(p_ratio, unit.hp_ratio) < 0.4
        if want_heal:
            _unit_pay_skill_cost(unit, heal_sk)
            power = max(0, int(heal_sk.get("power", 10)))
            if p_ratio <= unit.hp_ratio:
                before = session.player.hp
                heal(session.player, power, bonus=unit.snapshot.heal_bonus)
                amount = session.player.hp - before
                logs.append(f"{unit.role_label or '同伴'}{unit.name}施展{heal_sk.get('name', '治疗')}，为你回复 {amount} 点生命")
            else:
                before = unit.snapshot.hp
                heal(unit.snapshot, power, bonus=unit.snapshot.heal_bonus)
                amount = unit.snapshot.hp - before
                logs.append(f"{unit.role_label or '同伴'}{unit.name}施展{heal_sk.get('name', '治疗')}，回复自己 {amount} 点生命")
        else:
            # 攻击：优先血量比例最低的存活敌人
            # [布局 2026-08-31] 前排门控：同伴只打可选单位（对方前排未清不打后排）
            enemies = targetable_enemy_units(session)
            if not enemies:
                break
            tgt = min(enemies, key=lambda u: u.hp_ratio)
            # [P25b] focus：集火玩家本回合目标（目标已倒/无效回退最低血）
            if order == "focus" and session.player_target is not None \
                    and getattr(session.player_target, "alive", False) \
                    and session.player_target in enemies:
                tgt = session.player_target
            action, skill = choose_unit_action(unit, rng)
            if action == "skill" and isinstance(skill, dict) and skill.get("type") == "attack":
                log = _unit_cast_skill(session, unit, skill, tgt.snapshot, tgt.name, rng,
                                       on_player_side=True)
                logs.append(log)
            elif action == "skill" and isinstance(skill, dict) and skill.get("type") == "heal":
                # balanced AI 抽到治疗技：按血量比例低者回复（旧代码此处静默降级普攻，白掷一骰）
                _unit_pay_skill_cost(unit, skill)
                power = max(0, int(skill.get("power", 10)))
                # [C10 修复 2026-08-25] heal 类技能同样施加 inflicts（与 buff 分支/主路径同口径）
                inf_log = _apply_skill_inflicts(session, unit, skill, rng)
                if p_ratio <= unit.hp_ratio:
                    before = session.player.hp
                    heal(session.player, power, bonus=unit.snapshot.heal_bonus)
                    amount = session.player.hp - before
                    logs.append(f"{unit.role_label or '同伴'}{unit.name}施展{skill.get('name', '治疗')}，为你回复 {amount} 点生命"
                                + (f"（{inf_log}）" if inf_log else ""))
                else:
                    before = unit.snapshot.hp
                    heal(unit.snapshot, power, bonus=unit.snapshot.heal_bonus)
                    amount = unit.snapshot.hp - before
                    logs.append(f"{unit.role_label or '同伴'}{unit.name}施展{skill.get('name', '治疗')}，回复自己 {amount} 点生命"
                                + (f"（{inf_log}）" if inf_log else ""))
            elif action == "skill" and isinstance(skill, dict) and skill.get("type") == "buff":
                # balanced AI 抽到增益技：对自身附加 inflicts 状态（与 _unit_cast_skill buff 分支同口径）
                _unit_pay_skill_cost(unit, skill)
                inf_log = _apply_skill_inflicts(session, unit, skill, rng)
                log = f"{unit.role_label or '同伴'}{unit.name}施展{skill.get('name', '强化')}，进入增益状态"
                if inf_log:
                    log += f"（{inf_log}）"
                logs.append(log)
            else:
                r = resolve_attack(unit.snapshot, tgt.snapshot, rng,
                                   difficulty=session.difficulty, granularity=session.granularity,
                                   is_player_attacker=True,
                                   attacker_conds=unit.conditions, defender_conds=tgt.conditions,
                                   morale_mult=_morale_mult(int(getattr(unit, "morale", _MORALE_BASE))),
                                   attacker_morale=int(getattr(unit, "morale", _MORALE_BASE)))
                # [P26a] 结义同伴普攻联手加成
                if unit.npc_id in session.sworn_npc_ids:
                    _apply_sworn_bonus(session, tgt.snapshot, r, session.sworn_damage_mult)
                clash_txt = ""
                if session.enemy_defending and r.hit and r.damage > 0:
                    # [对撞 2026-09-01] 敌方防御被同伴普攻命中 -> 对撞（攻方=同伴）
                    c_dmg, clash_txt = resolve_defended_attack(
                        session, r, rng, unit.snapshot, tgt.snapshot,
                        defender_is_player=False, a_name=unit.name, d_name=tgt.name,
                        defender_unit=tgt)
                    r.damage = c_dmg
                    tgt.snapshot.hp = max(0, tgt.snapshot.hp - c_dmg)
                    r.defender_hp_after = tgt.snapshot.hp
                    r.defender_defeated = tgt.snapshot.hp <= 0
                else:
                    tgt.snapshot.hp = r.defender_hp_after
                if not r.hit:
                    logs.append(f"{unit.role_label or '同伴'}{unit.name}的攻击未命中{tgt.name}")
                else:
                    if c_dmg_guard_note(clash_txt):
                        log = f"{unit.role_label or '同伴'}{unit.name}的攻击被{tgt.name}完全格挡" + clash_txt
                    else:
                        log = (f"{unit.role_label or '同伴'}{unit.name}攻击{tgt.name}，造成 {r.damage} 点伤害"
                               + part_log(r) + ("（暴击！）" if r.crit else "") + clash_txt)
                        # [装备新属性 2026-09-10] 三件套单一来源（同伴作攻方：连击/吸血；
                        # 守方敌人：反击）——原实现同伴这三项全无接线（面板可见但不生效）。
                        log += apply_basic_attack_post(session, unit.snapshot, tgt.snapshot,
                                                       r.damage, rng, unit.name, tgt.name)
                        # [审查修复] 连击可能补刀致死 -> 必须刷新 r 的结算字段，
                        # 否则下面的 r.defender_defeated 分支被跳过（不刷战斗状态、不写「被击败」）
                        r.defender_hp_after = tgt.snapshot.hp
                        r.defender_defeated = tgt.snapshot.hp <= 0
                        if unit.snapshot.hp <= 0:
                            log += f"（{unit.name}被反击击倒）"
                            _refresh_battle_state(session)
                    if r.defender_defeated:
                        _refresh_battle_state(session)
                        if session.state == "won":
                            log += f"（{tgt.name}被击败）"
                        else:
                            log += f"（{tgt.name}被击败，战斗仍在继续）"
                    logs.append(log)
        _tick_cd(unit.cd)
    _accumulate_damage_dealt(session, _ehps)
    text = "\n".join(l for l in logs if l)
    if text:
        session.log.append(text)
    return text


def _minion_act(session: CombatSession, unit: CombatUnit, rng: SeededRng) -> str:
    """[P10] 随从敌人行动：可打玩家或存活同伴（有同伴时 40% 概率转向同伴）。"""
    # [P30] 控制状态：stunned/frozen 跳过行动
    if _has_cond(unit.conditions, "stunned") or _has_cond(unit.conditions, "frozen"):
        cd_name = condition_display("stunned", session.genre_id) if _has_cond(unit.conditions, "stunned") \
            else condition_display("frozen", session.genre_id)
        _tick_cd(unit.cd)
        return f"{unit.name}陷入{cd_name}，无法行动"
    allies = [u for u in living_ally_units(session) if unit_targetable(session, u, "ally")]
    # [布局 2026-08-31] 玩家也是可选目标：前排门控下（玩家后排且我方前排存活 -> 不可打）
    player_ok = player_targetable(session)      # 单一来源门控
    if allies and rng.chance(0.4):
        tgt_unit = rng.pick(allies)
        tgt_snap = tgt_unit.snapshot
        tgt_name = f"{tgt_unit.role_label or '同伴'}{tgt_unit.name}"
        is_player_target = False
        t_conds = tgt_unit.conditions
    elif player_ok:
        tgt_snap = session.player
        tgt_name = "你"
        is_player_target = True
        t_conds = session.player_conditions
    elif allies:
        tgt_unit = allies[0]
        tgt_snap = tgt_unit.snapshot
        tgt_name = f"{tgt_unit.role_label or '同伴'}{tgt_unit.name}"
        is_player_target = False
        t_conds = tgt_unit.conditions
    else:
        # 门控下无解（数据异常兜底：无视门控直打玩家，防死锁）
        tgt_snap = session.player
        tgt_name = "你"
        is_player_target = True
        t_conds = session.player_conditions
    action, skill = choose_unit_action(unit, rng)
    if action == "defend":
        _tick_cd(unit.cd)
        # [对撞 2026-09-01] 敌方随从摆防御：置敌方防御位（玩家/同伴普攻命中时对撞）
        session.enemy_defending = True
        return f"{unit.name}摆出防御姿态"
    # [P25b] defend 挡刀：随从普攻攻玩家时守护同伴概率接下（伤害减半）。
    # [!] 随从技能路径不挡（_unit_cast_skill 内部结算伤害无法干净减半，为保
    # 「挡刀必减半」口径统一放弃该路径——主敌普攻/主敌技能/随从普攻三路可挡）。
    guard = _defend_guard(session, rng) if is_player_target else None
    if action == "skill" and isinstance(skill, dict):
        if skill.get("type") == "heal":
            log = _unit_cast_skill(session, unit, skill, unit.snapshot, unit.name, rng,
                                   on_player_side=False)
        else:
            log = _unit_cast_skill(session, unit, skill, tgt_snap, tgt_name, rng,
                                   on_player_side=False)
        _tick_cd(unit.cd)
        return log
    if guard is not None:   # 普攻被挡
        r = resolve_attack(unit.snapshot, guard.snapshot, rng,
                           difficulty=session.difficulty, granularity=session.granularity,
                           is_player_attacker=False,
                           attacker_conds=unit.conditions, defender_conds=guard.conditions,
                           morale_mult=_morale_mult(int(getattr(unit, "morale", _MORALE_BASE))),
                           attacker_morale=int(getattr(unit, "morale", _MORALE_BASE)))
        _tick_cd(unit.cd)
        return _apply_block(guard, unit.name, r)
    # 普攻
    r = resolve_attack(unit.snapshot, tgt_snap, rng,
                       difficulty=session.difficulty, granularity=session.granularity,
                       is_player_attacker=False,
                       attacker_conds=unit.conditions, defender_conds=t_conds,
                       morale_mult=_morale_mult(int(getattr(unit, "morale", _MORALE_BASE))),
                       attacker_morale=int(getattr(unit, "morale", _MORALE_BASE)))
    dmg = r.damage
    prefix = ""
    clash_txt = ""
    if is_player_target and session.player_defending and dmg > 0:
        # [对撞 2026-09-01] 玩家防御被随从普攻命中 -> 5d6 对撞（替代旧 x0.5 减半）
        dmg, clash_txt = resolve_defended_attack(
            session, r, rng, unit.snapshot, session.player,
            defender_is_player=True, a_name=unit.name, d_name="你")
        prefix = "你举盾迎击，"
        tgt_snap.hp = max(0, tgt_snap.hp - dmg)
    else:
        tgt_snap.hp = r.defender_hp_after
    if not r.hit:
        _tick_cd(unit.cd)
        return f"{unit.name}的攻击未命中{tgt_name}"
    log = (f"{prefix}{unit.name}攻击{tgt_name}，造成 {dmg} 点伤害" + part_log(r)
           + ("（暴击！）" if r.crit else "") + clash_txt)
    # [装备新属性 2026-09-10] 三件套单一来源（随从作攻方：连击/吸血；守方：反击）
    log += apply_basic_attack_post(session, unit.snapshot, tgt_snap, dmg, rng,
                                   unit.name, tgt_name)
    if unit.snapshot.hp <= 0:
        log += f"（{unit.name}被反击击倒）"
        _refresh_battle_state(session)
    # [P31] counter 反应：随从普攻命中同伴 -> 被击中同伴概率反击（反伤攻击者一半伤害）
    if not is_player_target and dmg > 0 and tgt_unit is not None:
        cr = _check_reactions(session, "on_ally_hit", rng,
                              attacker_snap=unit.snapshot, damage=dmg, ally_unit=tgt_unit)
        if cr:
            log += f"；{tgt_unit.name}反击{unit.name}，造成 {cr['counter_dmg']} 点伤害"
            if unit.snapshot.hp <= 0:
                log += f"（{unit.name}被击倒）"
    if is_player_target and session.player.hp <= 0:
        if _side_defeated(session):
            session.state = "lost"
        log += f"（{session.player_label}被击倒）"
    elif not is_player_target and tgt_snap.hp <= 0:
        log += f"（{tgt_name}被击倒）"
    _tick_cd(unit.cd)
    return log



def _accumulate_damage_dealt(session: CombatSession, before: list) -> None:
    """[战报 2026-08-28] 按「行动前各敌方单位 hp 快照」差值累计玩家侧输出（含同伴/宠物）。"""
    units = all_enemy_combatants(session)
    for u, b in zip(units, before):
        session.dmg_dealt += max(0, b - u.snapshot.hp)


def _enemy_hps(session: CombatSession) -> list:
    return [u.snapshot.hp for u in all_enemy_combatants(session)]


def player_action(session: CombatSession, action: str, rng: SeededRng,
                  skill: Optional[dict] = None, item_heal: int = 0,
                  target: Optional[CombatUnit] = None) -> str:
    """[P7h] 玩家本回合行动。action: attack/skill/item/defend/flee。

    [P10] target：指定攻击的敌方单位（多敌遭遇时的目标选择）；None = 主敌（legacy），
    主敌已倒下时自动选第一个存活随从。
    返回日志文本。自动 tick 玩家技能冷却（行动后）。不改敌人状态（敌人回合由 enemy_turn）。
    """
    if session.state != "active":
        return ""
    if action == "item" and hasattr(item_heal, "consume_effect") and not combat_item_usable(item_heal):
        return ""
    # [战报] 行动前敌方 hp 快照（出口差值累计输出）
    _ehps = _enemy_hps(session)
    # [P30] 控制状态：stunned/frozen 跳过本回合行动；feared 只能 flee；entangled 拒逃
    p_conds = session.player_conditions
    genre = getattr(session, "genre_id", "western_fantasy") or "western_fantasy"
    if _has_cond(p_conds, "stunned") or _has_cond(p_conds, "frozen"):
        cd_name = (condition_display("stunned", genre) if _has_cond(p_conds, "stunned")
                   else condition_display("frozen", genre))
        _tick_cd(session.player_cd)
        session.log.append(f"你陷入{cd_name}，无法行动")
        return f"你陷入{cd_name}，无法行动"
    feared = _has_cond(p_conds, "feared")
    entangled = _has_cond(p_conds, "entangled")
    # [P10] 目标解析：未指定且主敌已倒 -> 自动切到存活随从（防止打空气）
    tgt = target
    if tgt is not None and not tgt.alive:
        tgt = None
    # [布局 2026-08-31] 前排门控：玩家点选后排但敌方前排未清空时，强制改选可选前排
    if tgt is not None and not unit_targetable(session, tgt, "enemy"):
        tgt = None
    if tgt is None:
        alive = targetable_enemy_units(session)
        tgt = alive[0] if alive else None
        if tgt is None and session.enemy.hp > 0 and not session.enemy_units:
            tgt = None  # legacy 单敌会话无包装单位：主敌路径直打（下方 tgt None 分支）
    # [P25b] 记录玩家本回合目标（focus 指令集火依据；None -> 主敌包装）
    session.player_target = tgt if tgt is not None else (
        session.enemy_units[0] if session.enemy_units else None)
    log = ""
    if feared and action != "flee":
        # [P30] feared 状态只能 flee，其他行动强制改为 flee
        action = "flee"
        skill = None
    if action == "skill" and isinstance(skill, dict):
        if tgt is not None and tgt.snapshot is not session.enemy:
            # [P10] 技能打指定随从单位（主敌仍走 legacy resolve_skill 保持口径）。
            # 包装单位与玩家共享 snapshot/cd dict 引用（技能冷却直接落到 session.player_cd）；
            # mp 是 int 值拷贝，需在下方手动同步扣减 session.player_mp。
            # [!] conditions 必须共享 session.player_conditions——buff inflicts 与
            # caster 攻击加成经 _conds_of(unit) 读 unit.conditions，挂到一次性新列表
            # 会让玩家增益白放（打随从路径与打主敌路径口径一致）。
            wrapper = CombatUnit(
                name="你", snapshot=session.player, mp=session.player_mp,
                skills=session.player_skills, cd=session.player_cd,
                talents=session.player_talents,
                conditions=session.player_conditions,
            )
            log = _unit_cast_skill(session, wrapper, skill, tgt.snapshot, tgt.name, rng,
                                   on_player_side=True)
            session.player_mp = wrapper.mp
        else:
            log = resolve_skill(session, skill, "player", rng)
        # [P12 -> 2026-09-10] 技能熟练度：每次施展随机 +8~16（roll_skill_xp_gain 确定性，
        # 盐含技能状态防连点锁死），升级即时生效并进战斗日志
        lvl_log = grant_skill_xp(skill, roll_skill_xp_gain(skill, getattr(session, "world_id", "")))
        if lvl_log:
            log = (log or "") + lvl_log
    elif action == "item":
        # [百分比回血 2026-09-01] item_heal 传 Item 时走 item_heal_value（heal_pct 优先），
        # int 旧口径照用（测试桩/快路径）
        # [P47 修复 2026-09-25] 带 consume_effect 的丹药战斗内真实生效：heal_mp 回蓝 /
        # cure 清负面状态（原 item 分支只会回血——回蓝丹吃了个寂寞、解毒丹无效果，
        # 物品页原筛选还把这两类直接挡在门外）。
        it = item_heal if hasattr(item_heal, "consume_effect") else None
        eff = getattr(it, "consume_effect", None) if it is not None else None
        if isinstance(eff, dict) and str(eff.get("type", "") or "") == "heal_mp":
            mp_max = max(0, session.player_mp_max)
            pct = max(1, min(100, int(eff.get("amount", 0) or 0)))
            mp_before = session.player_mp
            session.player_mp = min(mp_max, mp_before + int(mp_max * pct / 100))
            log = (f"你服用{getattr(it, 'name', '丹药')}，回复 {session.player_mp - mp_before}"
                   f" 点灵力（{session.player_mp}/{mp_max}）")
        elif isinstance(eff, dict) and str(eff.get("type", "") or "") == "cure":
            conds = session.player_conditions
            cleared = [c for c in conds if isinstance(c, dict) and
                       _CONDITION_DEFS.get(str(c.get("key", "") or ""), {}).get("type") != "buff"]
            conds[:] = [c for c in conds if c not in cleared]
            log = (f"你服用{getattr(it, 'name', '丹药')}，"
                   + ("清除了身上的负面状态" if cleared else "周身一轻（并无异状）"))
        else:
            if isinstance(eff, dict) and str(eff.get("type", "") or "") == "heal_full" and eff.get("amount"):
                pct = max(1, min(100, int(eff["amount"])))
                amount = max(1, int(session.player.hp_max * pct / 100))
            elif hasattr(item_heal, "heal_pct") or hasattr(item_heal, "heal_amount"):
                amount = item_heal_value(item_heal, session.player)
            else:
                amount = max(0, int(item_heal))
            before = session.player.hp
            heal(session.player, amount, bonus=session.player.heal_bonus)
            amount = session.player.hp - before
            log = f"你使用物品，回复 {amount} 点生命（{session.player.hp}/{session.player.hp_max}）"
    elif action == "defend":
        session.player_defending = True
        log = "你举起武器进入防御姿态（受击时触发骰子对撞）"
    elif action == "flee":
        # [P30] entangled 状态不能逃跑（缠绕束缚）
        if entangled:
            log = f"你被{condition_display('entangled', genre)}束缚，无法逃脱"
        elif rng.chance(_flee_chance(session.player.speed, session.enemy.speed,
                                     luck=getattr(session.player, "luck", 0))):
            session.state = "fled"
            log = "你成功脱战逃离！"
        else:
            log = "你试图逃跑但失败了"
    else:  # attack（默认）
        # [对撞 2026-09-01] 敌方防御中：玩家普攻命中后对撞判定（攻方=你）
        def _clash_enemy(dmg_r, defender_snap, defender_name, defender_unit=None):
            # [修 2026-09-10] 透传 defender_unit：守方是随从单位时，守方胜回 MP 要写单位 mp
            # （玩家/主敌是 bare 快照，mp 在 session 上，走 defender_is_player/主敌分支）。
            dmg, ctx = resolve_defended_attack(
                session, dmg_r, rng, session.player, defender_snap,
                defender_is_player=False, a_name="你", d_name=defender_name,
                defender_unit=defender_unit)
            return dmg, ctx
        if tgt is not None and tgt.snapshot is not session.enemy:
            # [P10] 普攻打指定随从单位
            r = resolve_attack(session.player, tgt.snapshot, rng,
                               difficulty=session.difficulty, granularity=session.granularity,
                               is_player_attacker=True,
                               attacker_conds=p_conds, defender_conds=tgt.conditions,
                               morale_mult=_morale_mult(_morale_of(session, "player")),
                               attacker_morale=_morale_of(session, "player"))
            # [P26a] 结义同伴在场则玩家普攻享联手加成
            if session.sworn_npc_ids:
                _apply_sworn_bonus(session, tgt.snapshot, r, session.sworn_damage_mult)
            clash_txt = ""
            if session.enemy_defending and r.hit and r.damage > 0:
                # [对撞] 敌方随从防御被命中 -> 对撞改写伤害（破防=全额+躯干/守住=x0.25）
                c_dmg, clash_txt = _clash_enemy(r, tgt.snapshot, tgt.name, tgt)
                r.damage = c_dmg
                tgt.snapshot.hp = max(0, tgt.snapshot.hp - c_dmg)
                r.defender_hp_after = tgt.snapshot.hp
                r.defender_defeated = tgt.snapshot.hp <= 0
            else:
                tgt.snapshot.hp = r.defender_hp_after
            if not r.hit:
                log = f"你的攻击未命中{tgt.name}"
            else:
                log = (f"你攻击{tgt.name}，造成 {r.damage} 点伤害" + part_log(r)
                       + ("（暴击！）" if r.crit else "") + clash_txt)
                # [装备新属性 2026-09-10] 三件套单一来源（守方反击 -> 攻方连击 -> 攻方吸血）：
                # 此前「随从作为守方的反击」与「同伴/随从作为攻方的连击吸血」完全没接线。
                log += apply_basic_attack_post(session, session.player, tgt.snapshot,
                                               r.damage, rng, "你", tgt.name)
                r.defender_hp_after = tgt.snapshot.hp
                r.defender_defeated = tgt.snapshot.hp <= 0
                if session.player.hp <= 0:
                    log += "（你被反击击倒）"      # 守方反击反伤攻方，可能反杀玩家
                    _refresh_battle_state(session)
                if r.defender_defeated:
                    _refresh_battle_state(session)
                    if session.state == "won":
                        log += f"（{tgt.name}被击败）"
                    else:
                        log += f"（{tgt.name}被击败，战斗仍在继续）"
        else:
            r = resolve_attack(session.player, session.enemy, rng,
                               difficulty=session.difficulty, granularity=session.granularity,
                               is_player_attacker=True,
                               attacker_conds=p_conds, defender_conds=session.enemy_conditions,
                               morale_mult=_morale_mult(_morale_of(session, "player")),
                               attacker_morale=_morale_of(session, "player"))
            # [P26a] 结义同伴在场则玩家普攻享联手加成
            if session.sworn_npc_ids:
                _apply_sworn_bonus(session, session.enemy, r, session.sworn_damage_mult)
            # [Boss 机制库] 护盾折减（改写 hp 并回写 r 保持下游 defeated 判定一致）。
            # [!] 此处 session.enemy.hp 仍是攻击前值（原代码直接赋 defender_hp_after），
            # 勿再 +damage（会双扣后抵消 = 伤害全免）。
            # [对撞修 2026-09-10] 先由对撞定最终伤害，护盾再按该伤害折减。原实现拿
            # 「已扣全额的血」又减一次 (r.damage - c_dmg)：守方胜时血量掉成
            # _hp_pre - 2*dmg + c_dmg（0.25x 反成约 1.75x，举盾比不举更痛），攻方豹子
            # 1.25x 也被 else 静默丢弃。改 final_dmg 单一来源，对撞/护盾互不重复扣。
            _hp_pre = session.enemy.hp
            clash_txt = ""
            final_dmg = int(r.damage) if r.hit else 0
            if session.enemy_defending and r.hit and r.damage > 0:
                # [对撞] 主敌防御被命中 -> 对撞决定最终伤害（护盾再按该伤害折减）
                c_dmg, clash_txt = _clash_enemy(r, session.enemy, "敌人")
                final_dmg = int(c_dmg)
                r.damage = c_dmg
            # 完全格挡（c_dmg=0）走 apply_boss_shield 的 damage<=0 早退：不落血不掉盾
            _hp_new, _broken = apply_boss_shield(session, _hp_pre, final_dmg)
            session.enemy.hp = _hp_new
            r.defender_hp_after = _hp_new
            r.defender_defeated = _hp_new <= 0
            if not r.hit:
                log = "你的攻击未命中"
            else:
                if c_dmg_guard_note(clash_txt):
                    log = "你的攻击被敌方完全格挡" + clash_txt
                else:
                    log = (f"你攻击敌人，造成 {r.damage} 点伤害" + part_log(r)
                           + ("（暴击！）" if r.crit else "") + clash_txt)
                    # [装备新属性 2026-09-10] 三件套单一来源（与其它普攻路径同序）
                    log += apply_basic_attack_post(session, session.player, session.enemy,
                                                   r.damage, rng, "你", "敌人")
                    r.defender_hp_after = session.enemy.hp
                    r.defender_defeated = session.enemy.hp <= 0
                    if session.player.hp <= 0:
                        log += "（你被反击击倒）"
                        _refresh_battle_state(session)
                if final_dmg > 0 and session.boss_shield_max > 0 and session.boss_shield > 0:
                    log += "（护盾减伤）"     # 完全格挡/未命中（final_dmg=0）不标，防自相矛盾文案
                if _broken:
                    log += "（护盾破碎！）"
                if r.defender_defeated:
                    # [P10] 主敌倒下仍可能有随从存活，统一走结束判定
                    _refresh_battle_state(session)
                    if session.state == "won":
                        log += "（敌人被击败）"
    _accumulate_damage_dealt(session, _ehps)
    _tick_cd(session.player_cd)
    if log:
        session.log.append(log)
    return log


def choose_enemy_action(session: CombatSession, rng: SeededRng) -> tuple:
    """[P7h] 敌人 AI 决策。返回 (action, skill|None)。

    4 pattern：
    - aggressive：优先攻击技能（60% 概率），否则普攻
    - defensive：HP<30% 时优先治疗/防御，否则普攻
    - caster：mp 够时优先法术技能，否则普攻
    - balanced：40% 用技能，HP<20% 时 30% 防御，否则普攻

    [P10] 委托给 choose_unit_action（主敌包装成单位，随从/同伴共用同一 AI）。
    """
    unit = CombatUnit(name="enemy", snapshot=session.enemy, mp=session.enemy_mp,
                      skills=session.enemy_skills, cd=session.enemy_cd,
                      talents=session.enemy_talents, ai=session.enemy_ai)
    return choose_unit_action(unit, rng)


def _main_enemy_act(session: CombatSession, rng: SeededRng) -> str:
    """主敌行动（原 enemy_turn 主体，[P10] 抽出供多敌回合复用）。"""
    # [修 2026-09-01] 主敌已倒下（玩家/同伴阶段击杀但随从存活 -> 战斗仍 active）
    # 不再出手：原实现只有回合末 _refresh_battle_state 才判 won，亡灵主敌会照常攻击。
    if session.enemy.hp <= 0:
        return ""
    # [P59 counterplay] 先取预告：控制打断需要知道预告的动作类型（打断奖励）
    intent = _pop_intent(session, "敌方主力")
    # [P30] 控制状态：stunned/frozen 跳过行动（主敌状态在 session.enemy_conditions）
    e_conds = session.enemy_conditions
    genre = getattr(session, "genre_id", "western_fantasy") or "western_fantasy"
    if _has_cond(e_conds, "stunned") or _has_cond(e_conds, "frozen"):
        cd_name = (condition_display("stunned", genre) if _has_cond(e_conds, "stunned")
                   else condition_display("frozen", genre))
        # [P59 counterplay 2026-09-26] 打断奖励：预告动作被控制打断 -> 我方士气提升
        # （读【敌情】预告做反制的收益；无预告=legacy 路径维持原样）
        if intent is not None:
            bonus = (_INTERRUPT_MORALE_SKILL if str(intent.get("action", "")) == "skill"
                     else _INTERRUPT_MORALE_ATTACK)
            _shift_morale(session, "player", bonus)
            _it_skill = intent.get("skill")
            if str(intent.get("action", "")) == "skill" and isinstance(_it_skill, dict):
                return (f"敌人陷入{cd_name}，无法行动——你打断了它"
                        f"「{_it_skill.get('name', '技能')}」的蓄力！（我方士气 +{bonus}）")
            return (f"敌人陷入{cd_name}，无法行动——你打断了它的动作！"
                    f"（我方士气 +{bonus}）")
        return f"敌人陷入{cd_name}，无法行动"
    action, skill = choose_enemy_action(session, rng)
    # [P49 动作覆写 2026-09-25] 决策照旧消费（执行流逐位不变），结果被预规划意图覆写——
    # 【敌情】预告的动作就是本回合真发生的动作（预告=事实，守结算真相）。
    # 意图不存在（引擎直构 session 的单测/未规划）= 100% legacy 路径。
    if intent is not None:
        action = str(intent.get("action", "") or action)
        skill = intent.get("skill") if action == "skill" else (skill if action == "attack" else None)
    log = ""
    if action == "skill" and isinstance(skill, dict):
        log = resolve_skill(session, skill, "enemy", rng, telegraphed=(intent is not None))
    elif action == "defend":
        # [对撞 2026-09-01] 主敌摆防御：置敌方防御位（玩家/同伴普攻命中时对撞）
        session.enemy_defending = True
        log = "敌人摆出防御姿态"
    else:  # attack
        # [布局 2026-08-31] 前排门控：主敌在玩家方前排存活时只打前排目标（玩家 row=1
        # 或前排同伴）；前排全灭才可越排打后排同伴/后排玩家。
        allies_front = [u for u in living_ally_units(session)
                        if unit_targetable(session, u, "ally")]
        player_ok = player_targetable(session)      # 单一来源门控
        # [P25b] defend 挡刀：守护同伴概率替玩家接下普攻（伤害减半）
        guard = _defend_guard(session, rng) if player_ok else None
        if guard is not None:
            r = resolve_attack(session.enemy, guard.snapshot, rng,
                               difficulty=session.difficulty, granularity=session.granularity,
                               is_player_attacker=False,
                               attacker_conds=e_conds, defender_conds=guard.conditions,
                               morale_mult=_morale_mult(_morale_of(session, "enemy")),
                               attacker_morale=_morale_of(session, "enemy"))
            return _apply_block(guard, "敌人", r)
        if not player_ok and allies_front:
            # 玩家在后排且我方前排未清 -> 打前排同伴
            tgt = allies_front[0]
            r = resolve_attack(session.enemy, tgt.snapshot, rng,
                               difficulty=session.difficulty, granularity=session.granularity,
                               is_player_attacker=False,
                               attacker_conds=e_conds, defender_conds=tgt.conditions,
                               morale_mult=_morale_mult(_morale_of(session, "enemy")),
                               attacker_morale=_morale_of(session, "enemy"))
            tgt.snapshot.hp = r.defender_hp_after
            if not r.hit:
                return f"敌人的攻击未命中{tgt.name}"
            log = f"敌人攻击{tgt.name}，造成 {r.damage} 点伤害" + part_log(r) + ("（暴击！）" if r.crit else "")
            if tgt.snapshot.hp <= 0:
                log += f"（{tgt.name}被击倒）"
            return log
        r = resolve_attack(session.enemy, session.player, rng,
                           difficulty=session.difficulty, granularity=session.granularity,
                           is_player_attacker=False,
                           attacker_conds=e_conds, defender_conds=session.player_conditions,
                           morale_mult=_morale_mult(_morale_of(session, "enemy")),
                           attacker_morale=_morale_of(session, "enemy"))
        dmg = r.damage
        clash_txt = ""
        if session.player_defending and dmg > 0:
            # [对撞 2026-09-01] 玩家防御被主敌普攻命中 -> 5d6 对撞（替代旧 x0.5 减半）
            dmg, clash_txt = resolve_defended_attack(
                session, r, rng, session.enemy, session.player,
                defender_is_player=True, a_name="敌人", d_name="你")
            log_prefix = "你举盾迎击，"
            session.player.hp = max(0, session.player.hp - dmg)
        else:
            log_prefix = ""
            session.player.hp = r.defender_hp_after
            if r.hit:
                _note_part_hit(session, r, r.damage)   # [P52 伤疤] 部位累计
        if not r.hit:
            log = "敌人的攻击未命中你"
        else:
            if dmg <= 0:
                # 守方豹子完全格挡（对撞文本已有说明）
                log = f"敌人的攻击被你完全格挡" + clash_txt
            else:
                log = (f"{log_prefix}敌人攻击造成 {dmg} 点伤害" + part_log(r)
                       + ("（暴击！）" if r.crit else "") + clash_txt)
            # [装备新属性 2026-09-10] 三件套单一来源（守方=玩家反击 -> 攻方=主敌连击/吸血）。
            # 原实现此路只接了「玩家反击 + 主敌吸血」，主敌的连击没接。
            log += apply_basic_attack_post(session, session.enemy, session.player, dmg, rng,
                                           "敌人", "你")
            if session.enemy.hp <= 0:
                log += "（敌人被反击败）"
            if session.player.hp <= 0:
                _refresh_battle_state(session)
            if _side_defeated(session):
                session.state = "lost"
                log += f"（{session.player_label}被击倒）"
    return log


def enemy_turn_begin(session: CombatSession) -> tuple:
    """[依次出手 2026-10-02 用户指示] 敌方回合开局快照（承受累计口径：玩家+同伴 hp）。

    与 enemy_acts/enemy_turn_finalize 配套供对话框逐单位演出；单次口径 enemy_turn
    内部同样走这三段（行为与 rng 消费顺序和旧实现完全一致）。"""
    return (session.player.hp, [u.snapshot.hp for u in (session.ally_units or [])])


def enemy_acts(session: CombatSession, rng: SeededRng):
    """[依次出手 2026-10-02] 逐单位产出敌方行动日志（生成器：主敌 -> 存活随从）。

    只做行动结算并逐条入 session.log；回合末收口（DoT/战意/CD/Boss/回合推进/
    承受累计）在 enemy_turn_finalize。rng 消费顺序 = 主敌 -> 随从（与旧单次口径
    逐调用一致，同盐同结果）。"""
    if session.state != "active":
        return
    log = _main_enemy_act(session, rng)
    if log:
        session.log.append(log)
    yield log
    # [P10] 随从依次行动（enemy_units[1:]，[0] 是主敌包装）
    for unit in list(session.enemy_units or [])[1:]:
        if session.state != "active":
            break
        if isinstance(unit, CombatUnit) and unit.alive:
            log = _minion_act(session, unit, rng)
            if log:
                session.log.append(log)
            yield log


def enemy_turn_finalize(session: CombatSession, snap: tuple) -> list:
    """[依次出手 2026-10-02] 敌方回合收口：DoT/战意/状态刷新/CD/Boss 机制/回合推进/
    承受累计。返回收口日志行（已入 session.log）。snap = enemy_turn_begin 的快照。"""
    logs = []
    # [P30] 回合末状态 tick（DoT 扣血 + duration 递减 + 过期清除 + 反应预算重置）
    cond_log = _apply_conditions(session)
    # [P51 战意] 回合末战意结算：自然回归 + 敌方杂兵崩溃溃逃（独立盐，不碰既有流）
    morale_log = _tick_morale(session)
    if cond_log:
        logs.append(cond_log)
    if morale_log:
        logs.append(morale_log)
    _refresh_battle_state(session)  # [P30] DoT 可能击杀，重判战斗状态
    session.player_defending = False
    session.enemy_defending = False   # [对撞 2026-09-01] 敌方防御同回合清（只保一回合）
    _tick_cd(session.enemy_cd)
    # [Boss 机制库] 敌方回合末结算（召唤潮/狂暴计时；round 推进前用当前回合号）
    logs.extend(boss_mechanics_tick(session))
    session.round += 1
    if session.round > session.max_rounds and session.state == "active":
        session.state = "fled"
        logs.append("（回合超限，双方脱战）")
    # [战报] 承受累计（玩家 + 同伴差值；同伴倒下保 1 血前的扣血也计入）
    _p_hp, _a_hps = snap
    session.dmg_taken += max(0, _p_hp - session.player.hp)
    for u, b in zip(session.ally_units or [], _a_hps):
        session.dmg_taken += max(0, b - u.snapshot.hp)
    logs = [l for l in logs if l]
    session.log.extend(logs)
    return logs


def enemy_turn(session: CombatSession, rng: SeededRng) -> str:
    """[P7h] 敌人回合：AI 决策 -> 结算 -> 检查结束 -> 推进回合。

    [P10] 多敌遭遇：主敌先行动，随后每个存活随从各行动一次（随从可打玩家或同伴）。
    玩家防御在本回合生效（受击减半），回合末清零。tick 敌人技能冷却。
    [依次出手 2026-10-02] 单次调用口径（快速战斗/测试）：内部走 begin/acts/finalize
    三段，行为与 rng 消费顺序与旧实现完全一致；对话框侧改为逐步消费 enemy_acts
    实现逐单位演出（不再多敌同时扑向玩家）。
    """
    if session.state != "active":
        return ""
    snap = enemy_turn_begin(session)
    acts = [l for l in enemy_acts(session, rng) if l]
    logs = acts + enemy_turn_finalize(session, snap)
    return "\n".join(logs)


def xp_threshold(level: int) -> int:
    """升级所需经验阈值：int(80 * level^1.35)。

    [P9] 从 100*level^1.5 调缓到 80*level^1.35（早期提速明显，中后期不再拖沓）。
    实测：Lv1->2 需 80xp（打 Lv1 怪 6 只 -> 5 只），Lv10 需 80*10^1.35≈1795（原 3162，近乎减半）。
    """
    level = max(1, int(level))
    return int(80 * (level ** 1.35))


def gain_xp(player: Any, amount: int) -> XpResult:
    """加经验，判定升级（可能连升多级），原地改 player.level/xp/xp_next。

    返回 XpResult（含是否升级、新等级）。不动 HP（升级回满由调用方决定）。
    [P7d2] 升级时给可分配属性点（每级 +STAT_POINTS_PER_LEVEL），原地加到 player.stat_points。
    """
    amount = max(0, int(amount))
    level = max(1, _stat(player, "level", 1))
    xp = _stat(player, "xp", 0) + amount
    xp_next = xp_threshold(level)
    leveled_up = False
    levels_gained = 0
    # [P 验收] 满级 100：封顶不再升级（原 999 无意义上限，用户指示世界设定满级 100 级）。
    while xp >= xp_next and level < 100:
        xp -= xp_next
        level += 1
        leveled_up = True
        levels_gained += 1
        xp_next = xp_threshold(level)
    stat_pts = 0
    try:
        player.level = level
        player.xp = xp
        player.xp_next = xp_next
        if levels_gained > 0:
            # [P7d2] 每升一级给 STAT_POINTS_PER_LEVEL 个可分配属性点
            stat_pts = STAT_POINTS_PER_LEVEL * levels_gained
            player.stat_points = int(getattr(player, "stat_points", 0) or 0) + stat_pts
    except (AttributeError, TypeError):
        pass
    return XpResult(leveled_up=leveled_up, new_level=level, xp=xp, xp_next=xp_next,
                    stat_points_gained=stat_pts)


# ---- [P10] 遭遇规模 + 助战人选的确定性兜底（LLM 判定失败/关闭时用）----
def roll_encounter_size(rng: SeededRng, location_danger: int, target_level: int,
                        player_level: int, max_enemies: int = 3) -> int:
    """[P10] 确定性遭遇规模：1 + 据地点危险度/敌方等级 roll 增援，钳制 [1, max_enemies]。

    增援概率 = clamp(0.12 + danger*0.03 + max(0, target_level - player_level)*0.05, 0, 0.55)，
    每个增援位独立 roll（最多 max_enemies-1 个增援）。同 rng 同结果（可复现可单测）。
    """
    max_enemies = max(1, min(5, int(max_enemies)))
    danger = max(0, min(10, int(location_danger or 0)))
    try:
        tl = max(0, int(target_level))
        pl = max(1, int(player_level))
    except (TypeError, ValueError):
        tl, pl = 1, 1
    p = max(0.0, min(0.55, 0.12 + danger * 0.03 + max(0, tl - pl) * 0.05))
    count = 1
    for _ in range(max_enemies - 1):
        if rng.chance(p):
            count += 1
    return count


def roll_assist_allies(rng: SeededRng, candidates: list, enemy_level: int,
                       max_allies: int = 2, companion_ids: Optional[set] = None) -> list:
    """[P10] 确定性助战兜底：从候选 NPC（同场景非敌对存活且交情>=40）roll 谁参战。

    参战概率 = clamp(0.25 + (affinity-40)*0.008 + max(0, npc.level - enemy_level)*0.03, 0, 0.85)；
    [P10b] companion_ids 中的同行 NPC 概率 +0.3（同伴几乎必定并肩作战）。
    返回参战 NPC 列表（最多 max_allies 个，保持传入顺序；调用方已把同行排在候选前列）。
    同 rng 同结果。
    """
    max_allies = max(0, min(4, int(max_allies)))
    if max_allies <= 0 or not candidates:
        return []
    try:
        el = max(0, int(enemy_level))
    except (TypeError, ValueError):
        el = 1
    comp_ids = companion_ids or set()
    out = []
    for npc in candidates:
        if len(out) >= max_allies:
            break
        try:
            aff = int(getattr(npc, "affinity", 0))
            nl = max(0, int(getattr(npc, "level", 0) or 0))
        except (TypeError, ValueError):
            continue
        if aff < 40:
            continue
        comp_bonus = 0.3 if getattr(npc, "id", "") in comp_ids else 0.0
        p = max(0.0, min(0.95, 0.25 + (aff - 40) * 0.008 + max(0, nl - el) * 0.03 + comp_bonus))
        if rng.chance(p):
            out.append(npc)
    return out


# ============ [P47-C2 敌情预告 2026-09-25] 锁定目标预告（Into the Breach 式信息先行）============
def estimate_threat(atk_snap: Any, def_snap: Any) -> str:
    """威胁档粗估（纯展示，无 rng 无副作用）：攻击力相对防御的比值分档 轻/中/重。"""
    atk = max(1, _stat(atk_snap, "atk", 1) + _stat(atk_snap, "magic_atk", 0) // 2)
    dfn = max(1, _stat(def_snap, "def_", 1))
    ratio = atk / dfn
    if ratio >= 1.2:
        return "重"
    if ratio >= 0.6:
        return "中"
    return "轻"


def _pop_intent(session: CombatSession, unit_name: str) -> Optional[dict]:
    """取并移除本单位的预规划意图（无则 None）——执行段的「兼容消费 + 结果覆写」口。"""
    intents = getattr(session, "enemy_intents", None)
    if not intents:
        return None
    for i, it in enumerate(intents):
        if str(it.get("unit_name", "")) == str(unit_name):
            return intents.pop(i)
    return None


def estimate_damage_range(atk_snap: Any, def_snap: Any, skill: Any = None) -> "tuple[int, int]":
    """伤害区间粗估（纯展示，无 rng 无副作用）：基础 = atk - def*0.5，
    端点按部位倍率 0.5x/1.6x（BODY_PARTS 现值）；技能加 power 后同法。"""
    atk = max(1, _stat(atk_snap, "atk", 1))
    dfn = max(0, _stat(def_snap, "def_", 0))
    base = max(1, atk - int(dfn * 0.5))
    if skill is not None:
        base = max(1, base + max(0, int(getattr(skill, "power", 0) or 0)
                                 if hasattr(skill, "power") else
                                 int(skill.get("power", 0) or 0)))
    return max(1, int(base * 0.5)), int(base * 1.6)


def plan_enemy_intents(session: CombatSession, world_id: str, tick: int) -> None:
    """为本回合规划敌方主力的完整意图（动作 + 目标 + 伤害区间），幂等（同回合一次）。

    [P49 升级 2026-09-25 用户拍板「123 继续」] 动作类型预告（独立 salt roll 与执行同
    函数 choose_enemy_action）+ **执行覆写**（_main_enemy_act 决策照旧消费、结果用意图
    覆写）——「玩家看到的敌情就是本回合真会发生的事」（预告=事实）。
    [!] 执行流 rng 消费顺序逐位不变（决策照旧 roll 后丢弃）-> 既有确定性回放的序列
        不漂移；被覆写的只是「选了什么动作」。
    [!] 随从动作是概率 roll（40% 转向同伴），第一批不预告随从；目标推演与 _main_enemy_act
        同规则（player_targetable 单一来源）。
    """
    if session.enemy_intents_round == session.round and session.enemy_intents:
        return                                    # 本回合已规划（幂等水位）
    intents: list = []
    main = getattr(session, "enemy", None)
    if main is not None and main.hp > 0:
        if player_targetable(session):
            tgt_name = str(getattr(session, "player_label", "") or "你")
            tgt_snap = session.player
        else:
            front = [u for u in living_ally_units(session)
                     if unit_targetable(session, u, "ally")]
            if front:
                tgt_name = str(front[0].name or "?")
                tgt_snap = front[0].snapshot
            else:
                tgt_name = ""
                tgt_snap = None
        if tgt_name and tgt_snap is not None:
            # [P49] 动作预告：独立 salt 流 roll（与执行同函数；执行时被本意图覆写）
            rng_i = SeededRng.seed_from(world_id, int(tick), f"combat_intent{session.round}")
            action, skill = choose_enemy_action(session, rng_i)
            it = {"unit_name": "敌方主力", "target": tgt_name, "action": action,
                  "skill": skill if action == "skill" else None}
            if action == "defend":
                it["label"] = "摆出防御姿态"
            elif action == "skill" and isinstance(skill, dict):
                lo, hi = estimate_damage_range(main, tgt_snap, skill)
                hint = "；举盾可减伤" if tgt_snap is session.player else ""
                it["label"] = (f"酝酿「{skill.get('name', '技能')}」瞄准 {tgt_name}"
                               f"（预计 {lo}~{hi} 伤）{hint}")
            else:
                lo, hi = estimate_damage_range(main, tgt_snap)
                it["label"] = f"锁定 {tgt_name}（预计 {lo}~{hi} 伤）"
            intents.append(it)
    session.enemy_intents = intents
    session.enemy_intents_round = session.round


# ============ [G01/R3 2026-09-30] 伤情主动处置：求医（药师治疗）============
DOCTOR_ROLE_KEYS = ("药师", "医师", "大夫", "郎中", "医")
TREAT_COST_BASE = 20        # 求医基础诊金（+10/处伤）
TREAT_DAYS_SHORTEN = 3      # 每次诊治缩短伤期天数（回满 HP 不等于部位伤痊愈）


def _is_doctor(npc) -> bool:
    role = str(getattr(npc, "role", "") or "")
    desc = str(getattr(npc, "desc", "") or "")[:40]
    return any(k in role or k in desc for k in DOCTOR_ROLE_KEYS)


def treat_injury(world, doctor) -> "tuple[bool, str]":
    """[G01/R3] 求医：花诊金让药师缩短一处伤期（HP 不动——回血靠服药，部位伤靠养）。

    门控：医者存活非敌对且与玩家同地点；玩家有未愈伤情；诊金 = 20 + 10x伤处数
    （gold 不足拒）。效果：最临近痊愈的一处伤 until_day -3（<=今日则痊愈移除）。
    零 rng（确定性）。返回 (ok, msg)。"""
    p = getattr(world, "player", None)
    if p is None or doctor is None:
        return False, "无人可医"
    if not getattr(doctor, "alive", True) or getattr(doctor, "hostile", False):
        return False, "对方无法诊治"
    if not _is_doctor(doctor):
        return False, "对方并非医者"
    if str(getattr(doctor, "location_id", "") or "") != str(getattr(p, "location_id", "") or ""):
        return False, "医者不在跟前，须当面诊治"
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    injuries = [it for it in (getattr(p, "injuries", None) or [])
                if isinstance(it, dict) and int(it.get("until_day", 0) or 0) > day]
    if not injuries:
        return False, "你身上没有未愈的伤"
    cost = TREAT_COST_BASE + 10 * len(injuries)
    gold = int(getattr(p, "gold", 0) or 0)
    if gold < cost:
        return False, f"诊金要 {cost}，你带的钱不够"
    p.gold = gold - cost
    injuries.sort(key=lambda it: int(it.get("until_day", 0) or 0))
    tgt = injuries[0]
    old_until = int(tgt.get("until_day", 0) or 0)
    new_until = old_until - TREAT_DAYS_SHORTEN
    tgt["until_day"] = new_until
    cured = new_until <= day
    if cured:
        p.injuries = [it for it in (getattr(p, "injuries", None) or [])
                      if it is not tgt]
    # 医者收诊金入钱包（守恒——G02 药师经济同口径）
    if isinstance(getattr(doctor, "wallet", None), int):
        doctor.wallet = int(doctor.wallet or 0) + cost
    name = str(tgt.get("name", "伤处"))
    if cured:
        return True, (f"{doctor.name}收下 {cost} 诊金，为你敷药施针——"
                      f"「{name}」已告痊愈（气血仍须服药将养）。")
    return True, (f"{doctor.name}收下 {cost} 诊金，为你敷药施针——"
                  f"「{name}」好转，约还需 {new_until - day} 天将养。")
