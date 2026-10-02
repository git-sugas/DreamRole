"""世界模拟数据模型（独立于 Character/WorldBook）。

一个 World 实体聚合：世界观词条 / 势力 / 地点 / NPC / 物品 / 任务 / 玩家状态 / 世界事件日志。
一世界一 JSON 文件（data/worlds/{id}.json），嵌套子实体随外层一起序列化（仿 WorldBook 模式）。

P3 加 CRPG 数值（属性/HP/装备/词缀/掉落），P4 加世界滴答状态（势力 power/wealth/territory/
relations、NPC alive/respawn/home/is_key_npc、地点 owner_faction_id/stability、任务 status/progress、
玩家 reputation、World.event_log）。所有子实体均 dataclass + to_dict/from_dict + 枚举白名单 +
强制类型 + or 兜底（守 §11），老 JSON 缺字段自动补默认（向后兼容）。
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.models.shop import Shop


def _now() -> str:
    return datetime.now().isoformat()


def _load_reputation(d: Any) -> dict[str, int]:
    """加载 reputation dict（对 faction_id 声望 -100~100）。防脏数据 + 钳制。"""
    if not isinstance(d, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in d.items():
        try:
            out[str(k)] = max(-100, min(100, int(v)))
        except (TypeError, ValueError):
            continue
    return out


def _load_relations(d: Any) -> dict[str, dict]:
    """[P26a] 加载 relations dict（{npc_id -> {stage, since_day}}）。stage 白名单 + 钳制。"""
    if not isinstance(d, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in d.items():
        if not isinstance(v, dict):
            continue
        stage = str(v.get("stage", "") or "")
        if stage not in _RELATION_STAGE_VALUES:
            continue
        try:
            since = max(1, int(v.get("since_day", 1) or 1))
        except (TypeError, ValueError):
            since = 1
        out[str(k)] = {"stage": stage, "since_day": since}
    return out


def _clamp_affinity(v: Any, default: int = 0) -> int:
    """[P10] 交情值钳制 [0, 100]（非数值回退 default，合法 0 保留）。"""
    if v is None:
        return default
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return default


def reputation_title(rep: Any) -> str:
    """[P15b2] 玩家在某势力的声望头衔（每 25 点一档，纯代码无 LLM）。

    <25 外人 / 25-49 熟人 / 50-74 自己人 / 75-99 骨干 / >=100 高层；
    负声望（与势力交恶）也归「外人」档（头衔只表达亲近层级，敌意由数值本身表达）。
    """
    try:
        v = int(rep)
    except (TypeError, ValueError):
        v = 0
    if v >= 100:
        return "高层"
    if v >= 75:
        return "骨干"
    if v >= 50:
        return "自己人"
    if v >= 25:
        return "熟人"
    return "外人"


# 枚举白名单
_ITEM_TYPE_VALUES = ("weapon", "armor", "consumable", "material", "key", "accessory")
_ITEM_RARITY_VALUES = ("common", "uncommon", "rare", "epic", "legendary", "mythic")
# [P34a] 物品次级分类白名单（与 type 正交，专用于背包分类/reagent 匹配/商店备货过滤；
# type 按 §23 铁律永不变，category 是叠加维度）。空=未指定按 default_category 推断 -> misc。
_ITEM_CATEGORY_VALUES = ("consume", "skillbook", "weapon", "armor", "accessory",
                         "material", "cultivate", "forge", "craft", "seed", "key",
                         "misc", "pet_food")
# [P34b] cultivate reagent 子类型白名单（identify=鉴定卷轴/refine=装备洗练石/
# apt_atk/apt_def/apt_hp/apt_mp/apt_spd=宠物资质丹(单维度)/apt_all=全资质洗练石）。
# 非 cultivate 物品 reagent_kind 留空。
_REAGENT_KIND_VALUES = ("", "identify", "refine", "apt_atk", "apt_def", "apt_hp",
                        "apt_mp", "apt_spd", "apt_all", "beast_skill")
# [消耗品结构化效果] type 白名单：stat_bonus=永久加五维 / heal_mp=回蓝 /
# cure=清状态(战斗外 no-op) / revive=复活(战斗外 no-op) / feed=食物回饱食。
# heal_full=旧回血别名（已并入 heal_pct：保留在白名单仅为解析旧档/旧 LLM 输出，
# 读档/收编即迁入 heal_pct 并清空，新提示词不再产出）。
_CONSUME_EFFECT_TYPES = ("", "stat_bonus", "heal_full", "heal_mp", "cure", "revive", "feed")

# [玩家印象 2026-09-06] 印象标签白名单（引擎按互动行为映射，LLM 只在台词里引用不发明）
_IMPRESSION_TAG_VALUES = ("慷慨", "仁善", "守信", "负信", "危险", "无畏", "富有", "落魄")


def _imd(v):
    """player_impression 字段兜底（脏值回退空 dict）。"""
    return v if isinstance(v, dict) else {}

_AMBITION_KIND_VALUES = ("", "wealth", "revenge", "courtship", "mastery",
                         "fame", "explore", "collect", "craft")


def _amd(v):
    """ambition 字段兜底（脏值回退空 dict）。"""
    return v if isinstance(v, dict) else {}
_CONSUME_STAT_KEYS = ("str", "dex", "int", "vit", "luk")
# [P34e] 聚落规模白名单（叠加维度，不改 kind 冻结枚举；空=未指定按 town 处理）。
# city=城市（必含交易所/锻造屋/药店 venue + 三类 Shop + 可购宅全档）/ town=城镇 / village=村庄（限 tier1 宅）。
_SETTLEMENT_SIZE_VALUES = ("city", "town", "village", "")
# ---- P4 世界滴答枚举白名单（守 §11）----
_EVENT_CATEGORY_VALUES = ("faction_war", "economy", "npc", "event", "quest")
_EVENT_SEVERITY_VALUES = ("trivial", "minor", "major", "crisis")
# [D2 2026-08-29] 据点设施类型白名单（shop 商铺含真 Shop/guard 岗哨/farm 农田）
_DOMAIN_FACILITY_KIND_VALUES = ("shop", "guard", "farm")
# [D6 2026-08-29] 据点委托状态白名单（open 待接/taken 承接/done 已交付/expired 过期退款）
_DOMAIN_COMMISSION_STATES = ("open", "taken", "done", "expired")


def _trim_domain_commissions(raw) -> list:
    """[D6] 委托列表落盘清洗：白名单过滤 + 在途（open/taken）全保 + finished 滚动留 6。

    [!] 在途单挂着押金（发布即出资金池），裁掉在途单 = 押金凭空蒸发，必须全保；
    done/expired 只是展示记录，滚动保留即可。
    """
    if not isinstance(raw, list):
        return []
    clean = [dict(c) for c in raw
             if isinstance(c, dict) and c.get("state") in _DOMAIN_COMMISSION_STATES]
    live = [c for c in clean if c.get("state") in ("open", "taken")]
    finished = [c for c in clean if c.get("state") in ("done", "expired")]
    return live + finished[-6:]
# [P26a] 关系阶段白名单（friend 好友 / sworn 结义 / sweetheart 恋人 / spouse 配偶）
_RELATION_STAGE_VALUES = ("friend", "sworn", "sweetheart", "spouse")
# [P15b1] locked = task chain locked segment (unlocked to available after previous ring claims reward)
_QUEST_STATUS_VALUES = ("available", "active", "completed", "failed", "abandoned", "claimed", "locked")
# [P7i] 任务目标类型白名单（代码可判定的进度事件）
# [S04/R2 2026-09-30] 事件驱动目标：秘境房探明/秘境攻克（target=秘境名，dungeon_engine
# 进度钩子喂）+ 物资交付（target=物品名，qe.deliver_progress 真实扣物入发布者容器）
_QUEST_OBJECTIVE_TYPES = ("kill", "gather", "talk", "visit", "collect",
                          "dungeon_room_resolved", "dungeon_cleared", "deliver_items",
                          "investigate", "escort")   # [S04 余项] investigate=两段调查; escort=护送
# [任务改造] 任务链类型白名单（main=主线链段 / side=支线独立；区分用于 UI 分区 + 主线引导）
_QUEST_CHAIN_VALUES = ("main", "side", "errand")   # [P42c] errand=日常跑腿

# ---- [剧情线 P46 2026-09-12] 阶段类型/线状态/来源白名单（守 §11 防脏数据）----
_ARC_STAGE_KIND_VALUES = ("scheme", "conflict", "social", "econ", "disaster")
_ARC_STATUS_VALUES = ("active", "succeeded", "failed", "aborted")
_ARC_SOURCE_VALUES = ("llm", "template")
# [S03/R2 2026-09-30] 阶段结算结果白名单（引擎到期评估写入 StoryStage.outcome）
_STAGE_OUTCOME_VALUES = ("success", "compromise", "setback", "interrupted")
# ---- P5c 8 槽装备体系 ----
# 装备槽 id（equipped dict 的 key 集合）。
EQUIPMENT_SLOTS = ("head", "chest", "legs", "feet",
                   "main_hand", "off_hand", "accessory1", "accessory2")
# 槽 id -> Item.type 大类（引擎按槽反查 type 决定加成分流）。
SLOT_TO_TYPE = {
    "head": "armor", "chest": "armor", "legs": "armor", "feet": "armor",
    "main_hand": "weapon", "off_hand": "weapon",
    "accessory1": "accessory", "accessory2": "accessory",
}
# 老 equipped key (weapon/armor/accessory) -> 新 8 槽 key 迁移映射。
_LEGACY_SLOT_TO_NEW = {
    "weapon": "main_hand", "armor": "chest", "accessory": "accessory1",
}


def _gi(d: dict, key: str, default: int) -> int:
    """[!] 安全读 int（守 §11）：缺失/None/脏类型（手改 JSON）回默认，合法 0 保留。
    `int(d.get(k, def) or def)` 会把 0 吞成默认——耐久耗尽/丰度枯竭/不朽装备等
    合法 0 状态重载即复活，引擎侧修过的吞 0 不能在模型层 round-trip 吐回去。"""
    v = d.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _gi_map(raw) -> dict:
    """安全读 {str: int} map（守 §11）：非 dict 或脏值丢弃，合法 0 保留。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _parse_consume_effect(raw) -> dict:
    """[消耗品结构化效果] 白名单 type + stats 钳正整数；非法回退空 dict（走旧 heal 回退）。"""
    if not isinstance(raw, dict) or not raw:
        return {}
    etype = str(raw.get("type", "") or "")
    if etype not in _CONSUME_EFFECT_TYPES or etype == "":
        return {}
    out = {"type": etype}
    if etype == "stat_bonus":
        stats = raw.get("stats") or {}
        clean = {}
        if isinstance(stats, dict):
            for k in _CONSUME_STAT_KEYS:
                try:
                    v = int(stats.get(k, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if v:
                    clean[k] = v
        if clean:
            out["stats"] = clean
        else:
            return {}  # 无有效属性加成 -> 视作无效
    else:
        # heal_full/heal_mp/cure/revive 才有 amount（stat_bonus 不写 amount，保 round-trip 干净）；
        # feed 另有 hunger（饱食度恢复量，apply_consume_effect 读 eff["hunger"]——
        # 2026-09-08 修：旧解析只留 amount 丢 hunger，吃食物恒回 0 的根因）。
        if etype == "feed":
            try:
                out["hunger"] = max(0, int(raw.get("hunger", 0) or 0))
            except (TypeError, ValueError):
                out["hunger"] = 0
            return out
        amount = raw.get("amount", 0)
        try:
            out["amount"] = max(0, int(amount))
        except (TypeError, ValueError):
            out["amount"] = 0
    return out


def _migrate_equipped_to_8slots(raw: dict) -> dict:
    """[P5c] 老存档 equipped dict 迁移到 8 槽 key。

    规则：
    - 老格式 {"weapon": id, "armor": id, "accessory": id} -> 迁移到 main_hand/chest/accessory1
    - 已是新 8 槽 key（head/chest/legs/...）-> 保留原样
    - 未知 key（既不在老映射也不在 8 槽白名单）-> 丢弃（防脏数据）
    - 已迁移过的老存档（即 key 已是 main_hand 等）走「新 8 槽 key」分支保留
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if v is None:
            continue
        # 老 key 迁移
        if k in _LEGACY_SLOT_TO_NEW:
            new_k = _LEGACY_SLOT_TO_NEW[k]
            # 不覆盖已存在的新 key（防重复）
            if new_k not in out:
                out[new_k] = v
        # 新 key 保留
        elif k in EQUIPMENT_SLOTS:
            out[k] = v
        # 未知 key 丢弃
    return out


@dataclass
class WorldLore:
    """世界书词条（独立于 WorldBookEntry，简化版）。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    key: str = ""                 # 词条名
    content: str = ""             # 设定内容
    priority: int = 100           # 展示排序（小的在前）

    def to_dict(self) -> dict:
        return {"id": self.id, "key": self.key, "content": self.content, "priority": self.priority}

    @classmethod
    def from_dict(cls, d: dict) -> "WorldLore":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            key=d.get("key", "") or "",
            content=d.get("content", "") or "",
            priority=_gi(d, "priority", 100),
        )


@dataclass
class WorldEvent:
    """世界滴答产生的事件/传闻（P4）。存 World.event_log，独立于 SceneLog 对话流。

    category=faction_war 势力战 / economy 经济 / npc NPC 异动 / event 传闻 / quest 任务进展。
    severity 从 trivial（无关紧要）到 crisis（世界级危机），供 UI 色标与叙事权重参考。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tick: int = 0                    # 发生时的世界回合号
    category: str = "event"          # 白名单 _EVENT_CATEGORY_VALUES
    severity: str = "minor"          # 白名单 _EVENT_SEVERITY_VALUES
    title: str = ""
    desc: str = ""
    factions: list[str] = field(default_factory=list)    # 相关 faction_id
    locations: list[str] = field(default_factory=list)   # 相关 location_id
    npcs: list[str] = field(default_factory=list)        # 相关 npc_id
    created_at: str = field(default_factory=_now)
    image: str = ""                # [P11d] 关键事件插图文件名（world_images_dir 下；空=无图）
    cause: str = ""                # NPC 行动的可核实起因（空=事件未提供）
    outcome: str = ""              # 行动后的实际结果（空=仅有 desc）
    shop_id: str = ""              # 相关商店，用于 NPC 生产/进货/上架链路
    item_ids: list[str] = field(default_factory=list)  # 实际涉及的物品 id

    def to_dict(self) -> dict:
        return {
            "id": self.id, "tick": self.tick, "category": self.category,
            "severity": self.severity, "title": self.title, "desc": self.desc,
            "factions": list(self.factions), "locations": list(self.locations),
            "npcs": list(self.npcs), "created_at": self.created_at,
            "image": self.image,
            "cause": self.cause, "outcome": self.outcome,
            "shop_id": self.shop_id, "item_ids": list(self.item_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorldEvent":
        if not d or not isinstance(d, dict):
            return cls()
        cat = d.get("category", "event") or "event"
        if cat not in _EVENT_CATEGORY_VALUES:
            cat = "event"
        sev = d.get("severity", "minor") or "minor"
        if sev not in _EVENT_SEVERITY_VALUES:
            sev = "minor"

        def _i(key: str, default: int) -> int:
            v = d.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return cls(
            id=d.get("id", str(uuid.uuid4())),
            tick=max(0, _i("tick", 0)),
            category=cat,
            severity=sev,
            title=d.get("title", "") or "",
            desc=d.get("desc", "") or "",
            factions=list(d.get("factions") or []),
            locations=list(d.get("locations") or []),
            npcs=list(d.get("npcs") or []),
            created_at=d.get("created_at", _now()),
            image=d.get("image", "") or "",
            cause=str(d.get("cause", "") or "")[:500],
            outcome=str(d.get("outcome", "") or "")[:500],
            shop_id=str(d.get("shop_id", "") or ""),
            item_ids=[x for x in d.get("item_ids", []) if isinstance(x, str)][:20]
            if isinstance(d.get("item_ids"), list) else [],
        )


@dataclass
class Faction:
    """势力。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    desc: str = ""
    ideology: str = ""            # 理念/目标
    leader_traits: str = ""       # 首领特质简述
    # ---- P4 世界滴答状态（默认 50 中位，0-100；老 JSON 缺字段自动补默认）----
    power: int = 50               # 综合实力 0-100
    wealth: int = 50              # 财富 0-100
    aggressiveness: int = 50      # 好战度 0-100（高则更易主动开战）
    territory: list[str] = field(default_factory=list)   # 控制 location_id 列表（动态归属）
    members: list[str] = field(default_factory=list)     # 所属 npc_id 列表
    relations: dict[str, int] = field(default_factory=dict)  # 对 faction_id 关系 -100~100

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc,
            "ideology": self.ideology, "leader_traits": self.leader_traits,
            "power": self.power, "wealth": self.wealth, "aggressiveness": self.aggressiveness,
            "territory": list(self.territory), "members": list(self.members),
            "relations": {str(k): int(v) for k, v in self.relations.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Faction":
        if not d or not isinstance(d, dict):
            return cls()

        def _i(key: str, default: int) -> int:
            v = d.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        rel = d.get("relations") or {}
        if not isinstance(rel, dict):
            rel = {}
        rel_clean = {}
        for k, v in rel.items():
            try:
                rel_clean[str(k)] = max(-100, min(100, int(v)))
            except (TypeError, ValueError):
                continue
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            ideology=d.get("ideology", "") or "",
            leader_traits=d.get("leader_traits", "") or "",
            power=max(0, min(100, _i("power", 50))),
            wealth=max(0, min(100, _i("wealth", 50))),
            aggressiveness=max(0, min(100, _i("aggressiveness", 50))),
            territory=list(d.get("territory") or []),
            members=list(d.get("members") or []),
            relations=rel_clean,
        )


@dataclass
class ResourceNode:
    """[P7g] 可采集资源点（灵药田/灵矿脉/水池/搜刮点等，题材化）。

    引擎层纯 Python 结算成功率 + 产出 + 丰度递减 + 冷却再生（gather_engine）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""                # 资源点名（题材化显示，如"灵药田"/"厨房水池"）
    type: str = ""                # 资源类型 key（herb/mine/wood/scavenge/water/energy/...）
    desc: str = ""                # 描述（场景页展示）
    tier: int = 1                 # [P12] 资源层级 1-5（对应产出材料稀有度 common->legendary）
    drops: list = field(default_factory=list)   # 产出表 [{item_id, qty, rate}]（rate 0-1）
    richness: int = 100           # 当前丰度 0-100（递减后枯竭，0=不可采）
    richness_max: int = 100       # 丰度上限（再生恢复到此）
    cooldown_tick: int = 0        # 下次可采的 tick（0=立即可采；>current_tick=冷却中）
    cooldown_duration: int = 3    # 采完后冷却多少 tick 再生（0=每次可采不冷却）
    regenerates: bool = True      # 是否可再生（False=一次性，采完消失）
    requires_tool: str = ""       # 需要的工具 Item.tool_for key（空=徒手可采）
    stat_used: str = "dex"        # 采集成功率驱动的属性（str/dex/int/vit/luk 白名单）
    last_gathered_tick: int = 0   # 上次被采的 tick

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "type": self.type, "desc": self.desc,
            "tier": self.tier,
            "drops": [d for d in self.drops if isinstance(d, dict)],
            "richness": self.richness, "richness_max": self.richness_max,
            "cooldown_tick": self.cooldown_tick, "cooldown_duration": self.cooldown_duration,
            "regenerates": bool(self.regenerates), "requires_tool": self.requires_tool,
            "stat_used": self.stat_used, "last_gathered_tick": self.last_gathered_tick,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceNode":
        stat = d.get("stat_used", "dex") or "dex"
        if stat not in ("str", "dex", "int", "vit", "luk"):
            stat = "dex"
        drops = d.get("drops") or []
        if not isinstance(drops, list):
            drops = []
        drops = [x for x in drops if isinstance(x, dict)]
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            type=d.get("type", "") or "",
            desc=d.get("desc", "") or "",
            tier=max(1, min(5, _gi(d, "tier", 1))),
            drops=drops,
            richness=max(0, min(100, _gi(d, "richness", 100))),
            richness_max=max(1, _gi(d, "richness_max", 100)),
            cooldown_tick=max(0, _gi(d, "cooldown_tick", 0)),
            cooldown_duration=max(0, _gi(d, "cooldown_duration", 3)),
            regenerates=bool(d.get("regenerates", True)),
            requires_tool=d.get("requires_tool", "") or "",
            stat_used=stat,
            last_gathered_tick=max(0, int(d.get("last_gathered_tick", 0) or 0)),
        )


@dataclass
class Location:
    """地点。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    desc: str = ""
    region: str = ""              # 所属区域
    faction_id: str = ""          # 控制势力 id
    danger: int = 1               # 危险度 1-10
    # [P12] 地点类型：settlement=聚落（城/镇/村/坊市，无资源点无野怪）|
    # wilderness=野外（有分层资源点 + 野外怪物遭遇）。LLM 生成时标注。
    kind: str = "wilderness"
    connections: list[str] = field(default_factory=list)   # 相邻地点 id 列表
    npc_ids: list[str] = field(default_factory=list)       # 此地点的 NPC id 列表
    background: str = ""          # 背景图文件名（存于 world_images_dir）
    # ---- P4 世界滴答状态 ----
    owner_faction_id: str = ""    # 动态归属（势力战可变；默认 = faction_id，init 时同步）
    stability: int = 50           # 稳定度 0-100（低则易生暴乱/事件）
    # ---- [P7g] 可采集资源点 ----
    resource_nodes: list = field(default_factory=list)   # list[ResourceNode]
    # ---- [P7f] 地图坐标 + 探索状态（无限增殖沙盒）----
    # x/y 网格坐标（build_world 自动布局分配；expand_map_at 沿方向偏移拓展）。
    # discovered=已被发现（地图可见，玩家探索到边界迷雾即标记）；explored=已进入过（move 时标记）。
    x: int = 0
    y: int = 0
    discovered: bool = True     # 新建世界地点默认已发现；拓展生成的新地点也标 discovered
    explored: bool = False      # 玩家是否进入过（move_player 标记，影响地图节点视觉）
    # [P8] 奇遇：该地点是否已 roll 过自动奇遇（首次 explored False->True 时 roll 一次）。
    # 守「每地点只自动触发一次」契约：手动探测按钮不查此标记（可重复触发，概率较低）。
    encounter_done: bool = False
    # ---- [P27] 二层地图：地点内场所 ----
    # places 场所列表（settlement/wilderness 生成；dungeon 内部地点不生成）。
    # default_place_id 进入该地点的默认场所（聚落=广场/城门，野外=林间空地；空=无场所化）。
    # npc_ids 保留为地点级聚合（= 其所有场所 npc_ids 的并集，reconcile 维护）。
    places: list = field(default_factory=list)   # list[Place]
    default_place_id: str = ""
    # [P34e] 聚落规模（叠加维度，不改 kind）：city/town/village，空=未指定。城市预留交易所/
    # 锻造屋/药店 venue + 三类 Shop + 可购宅全档；村庄限 tier1 宅。仅 kind=settlement 有意义。
    settlement_size: str = ""
    # [D1 2026-08-29] 玩家据点标记：该地点已被玩家购下改造为私人据点（kind 已翻转为
    # settlement）。每地点只能被购一次；据点实体/资金池/繁荣度/名册在 World.domains。
    player_owned: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc, "region": self.region,
            "faction_id": self.faction_id, "danger": self.danger, "kind": self.kind,
            "settlement_size": self.settlement_size if self.settlement_size in _SETTLEMENT_SIZE_VALUES else "",
            "player_owned": bool(self.player_owned),
            "connections": self.connections, "npc_ids": self.npc_ids, "background": self.background,
            "owner_faction_id": self.owner_faction_id, "stability": self.stability,
            "resource_nodes": [r.to_dict() for r in self.resource_nodes if isinstance(r, ResourceNode)],
            "x": self.x, "y": self.y,
            "discovered": bool(self.discovered), "explored": bool(self.explored),
            "encounter_done": bool(self.encounter_done),
            "places": [p.to_dict() for p in self.places if isinstance(p, Place)],
            "default_place_id": self.default_place_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Location":
        rn_raw = d.get("resource_nodes") or []
        if not isinstance(rn_raw, list):
            rn_raw = []
        kind = d.get("kind", "wilderness") or "wilderness"
        # [P25a] "dungeon" = 秘境内部地点（每秘境一个，连到入口；kind 走白名单防脏数据）
        if kind not in ("settlement", "wilderness", "dungeon"):
            kind = "wilderness"
        # [P34e] 聚落规模白名单回退（叠加维度，不改 kind；缺键=空串=未指定）
        ss = str(d.get("settlement_size", "") or "")
        if ss not in _SETTLEMENT_SIZE_VALUES:
            ss = ""
        places_raw = d.get("places") or []
        if not isinstance(places_raw, list):
            places_raw = []
        # [修 2026-09-13] 聚落（安全区）允许 danger=0：_ensure_generation_coherence 归零
        # 落盘后重读档不得被钳回 1，否则进世界自愈每次误报 dirty。野外仍 >=1。
        # [!] 判空用 is not None，不能用 or 1——合法的 0 会被当 falsy 吞掉。
        _danger_floor = 0 if kind == "settlement" else 1
        _raw_danger = d.get("danger")
        if _raw_danger is None:
            _danger = 0 if kind == "settlement" else 1
        else:
            try:
                _danger = max(_danger_floor, min(10, int(_raw_danger)))
            except (TypeError, ValueError):
                _danger = _danger_floor
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            region=d.get("region", "") or "",
            faction_id=d.get("faction_id", "") or "",
            danger=_danger,
            kind=kind,
            connections=list(d.get("connections") or []),
            npc_ids=list(d.get("npc_ids") or []),
            background=d.get("background", "") or "",
            owner_faction_id=d.get("owner_faction_id", "") or "",
            stability=max(0, min(100, _gi(d, "stability", 50))),
            resource_nodes=[ResourceNode.from_dict(r) for r in rn_raw if isinstance(r, dict)],
            x=int(d.get("x", 0) or 0),
            y=int(d.get("y", 0) or 0),
            discovered=bool(d.get("discovered", True)),
            explored=bool(d.get("explored", False)),
            encounter_done=bool(d.get("encounter_done", False)),
            places=[Place.from_dict(p) for p in places_raw if isinstance(p, dict)],
            default_place_id=d.get("default_place_id", "") or "",
            settlement_size=ss,
            player_owned=bool(d.get("player_owned", False)),
        )


@dataclass
class Place:
    """[P27] 地点内场所（二层地图：地点 -> 场所）。

    Location.places 是场所列表；场所间靠 connections 构成地点内连通图。
    玩家/NPC 的精确位置 = (location_id, place_id)；place_id 空时回退 location.default_place_id。
    引擎管连通图/位置/移动校验，LLM 管场所骨架（名/类型/desc）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""                # 场所名（酒馆/铁匠铺/林间空地 等，题材化）
    desc: str = ""                # 一句描述
    type: str = ""                # 场所类型关键词（酒馆/铁匠铺/宅邸/集市/林间空地/溪边...）
    danger: int = 0               # 场所级危险度（聚落场所通常 0；野外场所可>0，仅叙事参考）
    connections: list[str] = field(default_factory=list)   # 同地点内相邻场所 id
    npc_ids: list[str] = field(default_factory=list)      # 此场所的 NPC id（场所级在场）

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc, "type": self.type,
            "danger": max(0, min(10, int(self.danger))),
            "connections": list(self.connections),
            "npc_ids": list(self.npc_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Place":
        if not d or not isinstance(d, dict):
            return cls()
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            type=d.get("type", "") or "",
            danger=max(0, min(10, _gi(d, "danger", 0))),
            connections=list(d.get("connections") or []),
            npc_ids=list(d.get("npc_ids") or []),
        )


@dataclass
class NPC:
    """NPC（独立于 Character，P3 加战斗数值）。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    avatar: str = ""              # 头像文件名（存于 world_images_dir）
    role: str = ""                # 身份（商人/守卫/首领...）
    faction_id: str = ""          # 所属势力 id
    location_id: str = ""         # 所在地点 id
    personality: str = ""         # 性格
    speech_style: str = ""        # [P20] 说话风格（泛义倾向：语气/用词习惯，1 句内；空=未生成）
    goal: str = ""                # 目标
    appearance: str = ""          # 中文外貌描述（供生图）
    appearance_tags: str = ""     # 初次成功头像的英文正向 tag；玩家可在档案锁定/编辑
    desc: str = ""                # 补充描述
    # ---- P3 战斗数值（默认 0/False 表示非战斗 NPC；build_world 据 role 推算）----
    level: int = 0
    hp: int = 0
    hp_max: int = 0
    stat_str: int = 5            # 力（攻击/暴击伤害/招架率）
    stat_dex: int = 5            # 敏（命中/暴击率/速度/闪避率）
    stat_int: int = 5            # 智（法术攻击/治疗加成）[P7e]
    stat_vit: int = 5            # 耐（HP 上限/防御）
    stat_luk: int = 5            # 运（速度/掉落率）[P7e]
    hostile: bool = False         # 是否敌对（可被攻击）
    loot_table: list = field(default_factory=list)   # 掉落表 [{id,rate} 或 item_id]
    # ---- [P7h] 回合制战斗（敌人技能 + AI + 法力）----
    skills: list = field(default_factory=list)        # list[dict] 敌人技能（老空 list 兼容）
    ai_pattern: str = "balanced"                      # aggressive/defensive/caster/balanced（敌人 AI 决策风格）
    mp: int = 0                                       # 法力（技能消耗）
    mp_max: int = 0
    # [P36a] NPC 生命模拟：经验/可分配点/钱包（与 PlayerState 同口径，gain_xp 鸭子类型直用；
    # 升级自动按职能加点，stat_points 通常为 0 过渡态）
    xp: int = 0
    xp_next: int = 0
    stat_points: int = 0
    wallet: int = 0
    # [饱食度 2026-09-06 用户指示] 0-100；hunger_enabled 世界每日衰减，饿了去食堂堂食
    # （npc_life_engine.daily_dining）或吃背包食物。NPC 饥饿只驱动行为，不削属性（仅玩家罚）。
    hunger: int = 100
    # [野心 2026-09-06 用户指示] 一生追求（全员无差别，不分公司要角与平民）：
    # {kind, desc, target_npc_id, target_value, born_day}；空 dict = 待抽新；
    # 完成后置 {kind: "", done_day: N}（2 天后抽下一个——人的一生有很多目标）。
    # kind 白名单 8 类：wealth 攒钱置业/revenge 复仇/courtship 求偶寻亲/mastery 练成绝技/
    # fame 扬名/explore 游历/collect 收藏/craft 匠心（判定全引擎纯 Python）。
    ambition: dict = field(default_factory=dict)
    # [野心] 游历判定用：到过的地点 id（移动处去重记录）；匠心判定用：累计合成件数。
    visited_locations: list = field(default_factory=list)
    crafted_count: int = 0
    # [玩家印象 2026-09-06 用户指示] 每个 NPC 各自维护对玩家的主观印象（非全局声誉）：
    # {tags: [白名单标签], trust: -100~100, familiarity: 0+}。亲眼互动（赠送/战斗/任务/
    # 委托）= 熟悉度+1 印象深；道听途说（传闻）= 只动 tags 不加熟悉度（浅、可能过时）。
    # 台词/定价/详情消费——同一玩家在不同 NPC 眼里不是同一个人。
    player_impression: dict = field(default_factory=dict)
    # ---- P4 世界滴答状态 ----
    alive: bool = True            # 是否存活（阵亡标记，叙事可描述尸体/搜刮，不删实体）
    respawn_at_tick: int = 0      # 0=不重生；>0=在该 tick 重生（cleanup_and_respawn 用）
    respawn_location_id: str = "" # 重生地点（默认回 home）
    home_location_id: str = ""    # 常驻地点（外出后回家；默认 = location_id）
    last_seen_tick: int = 0       # 玩家上次见到该 NPC 的 tick（offscreen 判定参考）
    is_key_npc: bool = False      # 要角标记（True 走 LLM 决策；False 杂兵走规则聚合）
    current_action: str = ""      # offscreen 时在做什么（叙事提示用）
    # [P] 短期动态目标：要角 LLM 决策每回合可更新的「当前正在推进的短期目标」（跨回合锚，
    # 防 NPC 决策乱漂）。空 = 尚未生成（回退 goal 作默认）。
    current_goal: str = ""
    # [A+B 2026-09-06 用户指示] 此刻念头/今日目标（让 NPC 像玩家一样活在世界里）：
    # 日计划 LLM 批顺带产出（零新增调用），每日随计划刷新；规则计划回退题材化模板句。
    # 要角 tick 级 current_goal 优先级更高（注入时先取）。当天未进活跃名单者保留旧值。
    current_thought: str = ""     # 此刻念头（<=20 字内心戏：在惦记什么/为何发愁）
    daily_goal: str = ""          # 今日短期目标（<=20 字：今天想达成什么）
    # [⑥ 2026-08-30] 性格偏移注记（引擎判转折点 + LLM 批量写；空=未漂移）。独立于 personality
    # 累积「经历→变化」，不改原 personality（守确定性）；注入场景/决策上下文时追加在性格之后。
    personality_drift: str = ""
    last_drift_tick: int = 0       # 上次改写性格偏移的 tick（每 NPC 冷却防频繁重写）
    # ---- P6 商人字段 ----
    is_merchant: bool = False     # 是否商售 NPC（True 则有商店；build_world 据 role 关键词标记）
    shop_id: str = ""             # 反向指向 Shop.id（NPC 与商店一一对应）
    shop_type: str = ""           # 商店类型 key（general/weapon/...；空=引擎据 role 推断）
    # ---- P6c NPC 档案字段（人际关系/爱好/装备/随身背包/个人小传；档案页展示 + 调试用）----
    notes: str = ""               # LLM 生成的个人小传（背景故事片段）
    hobbies: list[str] = field(default_factory=list)   # 爱好/兴趣
    relationships: list = field(default_factory=list)  # [{target_id,target_name,relation,desc}]
    # [P38a] NPC-NPC 交情底账 {target_id: -100..100}（社交演化引擎双向对称写；
    # relationships 是展示层，social_engine 跨越阶段时 upsert，与 LLM 播种条目共存）
    social: dict[str, int] = field(default_factory=dict)
    equipped: dict = field(default_factory=dict)       # 8 槽 {slot_id->item_id}（战斗 NPC 用）
    inventory: list[str] = field(default_factory=list)  # 随身物品 item_id（击败可搜刮，独立于 loot_table）
    # [P47 后果层] 该 NPC 从玩家手里夺走的物品 id：玩家击败他时保证夺回（同 id），
    # 并被排除在既有搜刮之外（夺回走 stolen_ids 通道，见 consequence_engine.reclaim_stolen）。
    stolen_ids: list[str] = field(default_factory=list)
    # [P52 伤疤] NPC 同款战损留痕（带伤打猎 hunt_win_chance 下降——队友真的会留痕）。
    injuries: list = field(default_factory=list)
    talents: list = field(default_factory=list)        # [P9] NPC 天赋 list[dict]（LLM 填，effects 参与战斗计算）
    # ---- [P10] 交情（同场景 NPC 主动行为 + 好友 + 助战的驱动值）----
    # 0-100，初始 0（陌生——萍水相逢；[P16 用户指示] 原默认 20 改 0，老档已存值不受影响）。
    # 0-19 陌生 / 20-39 点头之交 / 40-59 相熟 / 60-79 朋友 / 80-100 挚友。
    # 谈话/送礼/私聊/任务领奖/助战增长（引擎确定性结算）；达到好友门槛（默认 60）可加好友。
    affinity: int = 0
    # ---- [P24c] 最近互动日（赠礼/私聊/同行/助战刷新；world_tick_engine.tick_friends
    # 交情衰减的水位线——每满 30 天未互动 -2 钳 50。0 = 从未记录（视作久远，衰减追补）。）
    last_interact_day: int = 0
    # [P25b] 同伴战斗疲劳：参战 NPC 战后 8-14 tick 内不可邀请同行/助战（0=无疲劳）。
    companion_fatigue_until_tick: int = 0
    # [P26a] 与玩家共同战斗场次（结义条件之一：>=3 场方可结义）。
    shared_combats: int = 0
    # [G02/R3 2026-09-30] 上次探伤赠药日（tick_care_visits 每 NPC 冷却 7 天）
    last_care_day: int = 0
    # [A3 2026-08-28] 最近主动私聊玩家的日子（friend_chat 节流：同好友 >=2 天一条，防刷屏）。
    last_proactive_chat_day: int = 0
    # ---- [P9] 元素构成（元素克制矩阵用；空=无元素身份，不吃克制增减伤）----
    # 如冰霜巨人 ["ice"]、火蜥蜴 ["fire", "earth"]。LLM 生成可填；引擎兜底从 skills 元素推断
    # （combat_engine.infer_elements_from_skills）。结算规则见 combat_engine.element_damage_mult。
    elements: list[str] = field(default_factory=list)
    # ---- [P27] 二层地图：NPC 当前场所（空=回退所在 location 的 default_place_id）----
    place_id: str = ""
    # [P32] 同伴插话频率水位线：上次插话的 tick（间隔不够不再触发，防连续刷屏；0=从未）。
    last_interject_tick: int = 0
    # [P 验收] gen 骨架显式战斗角色标注（boss/hostile/friendly/none）。[P44] 空串=未标注，
    # 一律按平民处理（role 关键词兜底路已删除，战斗身份唯一来源 = 显式标注）。
    combat_role: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "avatar": self.avatar, "role": self.role,
            "faction_id": self.faction_id, "location_id": self.location_id,
            "personality": self.personality, "speech_style": self.speech_style, "goal": self.goal,
            "appearance": self.appearance, "appearance_tags": self.appearance_tags,
            "desc": self.desc,
            "level": self.level, "hp": self.hp, "hp_max": self.hp_max,
            "stat_str": self.stat_str, "stat_dex": self.stat_dex, "stat_int": self.stat_int,
            "stat_vit": self.stat_vit, "stat_luk": self.stat_luk,
            "hostile": self.hostile, "loot_table": list(self.loot_table),
            "skills": [dict(s) for s in self.skills if isinstance(s, dict)],
            "ai_pattern": self.ai_pattern, "mp": self.mp, "mp_max": self.mp_max,
            "xp": max(0, int(self.xp or 0)), "xp_next": max(0, int(self.xp_next or 0)),
            "stat_points": max(0, int(self.stat_points or 0)), "wallet": max(0, int(self.wallet or 0)),
            "hunger": max(0, min(100, int(self.hunger or 0))),
            "player_impression": {
                "tags": [str(t) for t in (self.player_impression or {}).get("tags", [])
                         if t in _IMPRESSION_TAG_VALUES],
                "trust": max(-100, min(100, int((self.player_impression or {}).get("trust", 0) or 0))),
                "familiarity": max(0, int((self.player_impression or {}).get("familiarity", 0) or 0)),
            },
            "ambition": {k: v for k, v in (self.ambition or {}).items()
                        if k in ("kind", "desc", "target_npc_id", "target_value", "born_day", "done_day")},
            "visited_locations": [str(x) for x in (self.visited_locations or [])][:60],
            "crafted_count": max(0, int(self.crafted_count or 0)),
            "alive": self.alive, "respawn_at_tick": self.respawn_at_tick,
            "respawn_location_id": self.respawn_location_id, "home_location_id": self.home_location_id,
            "last_seen_tick": self.last_seen_tick, "is_key_npc": self.is_key_npc,
            "current_action": self.current_action,
            "current_goal": self.current_goal,
            "current_thought": self.current_thought,
            "daily_goal": self.daily_goal,
            "personality_drift": self.personality_drift,
            "last_drift_tick": max(0, int(self.last_drift_tick)),
            "is_merchant": self.is_merchant, "shop_id": self.shop_id, "shop_type": self.shop_type,
            "notes": self.notes,
            "hobbies": list(self.hobbies),
            "relationships": [r for r in self.relationships if isinstance(r, dict)],
            "social": {str(k): max(-100, min(100, int(v)))
                       for k, v in (self.social or {}).items()
                       if isinstance(k, str) and isinstance(v, (int, float))},
            "equipped": _migrate_equipped_to_8slots(self.equipped) if self.equipped else {},
            "inventory": list(self.inventory),
            "stolen_ids": [str(i) for i in (self.stolen_ids or [])],
            "injuries": [dict(i) for i in (self.injuries or []) if isinstance(i, dict)],
            "talents": [Talent.from_dict(t).to_dict() for t in self.talents if isinstance(t, dict)],
            "affinity": _clamp_affinity(getattr(self, "affinity", 0)),
            "last_interact_day": max(0, int(self.last_interact_day)),
            "companion_fatigue_until_tick": max(0, int(self.companion_fatigue_until_tick)),
            "shared_combats": max(0, int(self.shared_combats)),
            "last_care_day": max(0, int(self.last_care_day)),
            "last_proactive_chat_day": max(0, int(getattr(self, "last_proactive_chat_day", 0) or 0)),
            "elements": _clean_elements(self.elements),
            "place_id": self.place_id,
            "last_interject_tick": max(0, int(self.last_interject_tick)),
            "combat_role": self.combat_role,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NPC":
        if not d or not isinstance(d, dict):
            return cls()

        def _i(key: str, default: int) -> int:
            v = d.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            avatar=d.get("avatar", "") or "",
            role=d.get("role", "") or "",
            faction_id=d.get("faction_id", "") or "",
            location_id=d.get("location_id", "") or "",
            personality=d.get("personality", "") or "",
            speech_style=d.get("speech_style", "") or "",
            goal=d.get("goal", "") or "",
            appearance=d.get("appearance", "") or "",
            appearance_tags=str(d.get("appearance_tags", "") or "")[:4000],
            desc=d.get("desc", "") or "",
            level=max(0, _i("level", 0)),
            hp=max(0, _i("hp", 0)),
            hp_max=max(0, _i("hp_max", 0)),
            stat_str=_i("stat_str", 5),
            stat_dex=_i("stat_dex", 5),
            stat_int=_i("stat_int", 5),
            stat_vit=_i("stat_vit", 5),
            stat_luk=_i("stat_luk", 5),
            hostile=bool(d.get("hostile", False)),
            loot_table=list(d.get("loot_table") or []),
            skills=_migrate_skills(d.get("skills")),
            ai_pattern=(d.get("ai_pattern") or "balanced") if (d.get("ai_pattern") or "balanced") in _AI_PATTERN_VALUES else "balanced",
            mp=max(0, _i("mp", 0)),
            mp_max=max(0, _i("mp_max", 0)),
            xp=max(0, _i("xp", 0)),
            xp_next=max(0, _i("xp_next", 0)),
            stat_points=max(0, _i("stat_points", 0)),
            wallet=max(0, _i("wallet", 0)),
            hunger=max(0, min(100, _i("hunger", 100))),
            player_impression={
                "tags": [str(t) for t in (_imd(d.get("player_impression")).get("tags", []))
                         if t in _IMPRESSION_TAG_VALUES],
                "trust": max(-100, min(100, int(_imd(d.get("player_impression")).get("trust", 0) or 0))),
                "familiarity": max(0, int(_imd(d.get("player_impression")).get("familiarity", 0) or 0)),
            },
            ambition={
                "kind": str(_amd(d.get("ambition")).get("kind", "") or ""),
                "desc": str(_amd(d.get("ambition")).get("desc", "") or "")[:120],
                "target_npc_id": str(_amd(d.get("ambition")).get("target_npc_id", "") or ""),
                "target_value": max(0, int(_amd(d.get("ambition")).get("target_value", 0) or 0)),
                "born_day": max(0, int(_amd(d.get("ambition")).get("born_day", 0) or 0)),
                "done_day": max(0, int(_amd(d.get("ambition")).get("done_day", 0) or 0)),
            },
            visited_locations=[str(x) for x in (d.get("visited_locations")
                                                if isinstance(d.get("visited_locations"), list) else [])][:60],
            crafted_count=max(0, _i("crafted_count", 0)),
            alive=bool(d.get("alive", True)),
            respawn_at_tick=max(0, _i("respawn_at_tick", 0)),
            respawn_location_id=d.get("respawn_location_id", "") or "",
            home_location_id=d.get("home_location_id", "") or "",
            last_seen_tick=max(0, _i("last_seen_tick", 0)),
            is_key_npc=bool(d.get("is_key_npc", False)),
            current_action=d.get("current_action", "") or "",
            current_goal=d.get("current_goal", "") or "",
            current_thought=str(d.get("current_thought", "") or "")[:120],
            daily_goal=str(d.get("daily_goal", "") or "")[:120],
            personality_drift=d.get("personality_drift", "") or "",
            last_drift_tick=max(0, _gi(d, "last_drift_tick", 0)),
            is_merchant=bool(d.get("is_merchant", False)),
            shop_id=d.get("shop_id", "") or "",
            shop_type=d.get("shop_type", "") or "",
            notes=d.get("notes", "") or "",
            hobbies=[str(h) for h in (d.get("hobbies") or []) if h],
            relationships=[r for r in (d.get("relationships") or []) if isinstance(r, dict)],
            social={str(k): max(-100, min(100, int(v)))
                    for k, v in (d.get("social") or {}).items()
                    if isinstance(k, str) and isinstance(v, (int, float))},
            equipped=_migrate_equipped_to_8slots(dict(d.get("equipped") or {})),
            inventory=[str(i) for i in (d.get("inventory") or []) if i],
            stolen_ids=[str(i) for i in (d.get("stolen_ids") or []) if i],
            injuries=[dict(i) for i in (d.get("injuries") or []) if isinstance(i, dict)],
            talents=[Talent.from_dict(t).to_dict() for t in (d.get("talents") or []) if isinstance(t, dict)],
            affinity=_clamp_affinity(d.get("affinity", 0)),
            last_interact_day=max(0, _gi(d, "last_interact_day", 0)),
            companion_fatigue_until_tick=max(0, _gi(d, "companion_fatigue_until_tick", 0)),
            shared_combats=max(0, _gi(d, "shared_combats", 0)),
            last_care_day=max(0, _gi(d, "last_care_day", 0)),
            last_proactive_chat_day=max(0, _gi(d, "last_proactive_chat_day", 0)),
            # [P9] 元素构成：白名单 + 去重 + 最多 2 个（防 LLM/手改 JSON 塞满）
            elements=_clean_elements(d.get("elements")),
            place_id=d.get("place_id", "") or "",
            last_interject_tick=max(0, _gi(d, "last_interject_tick", 0)),
            combat_role=(d.get("combat_role") or "") if (d.get("combat_role") or "") in _COMBAT_ROLE_VALUES else "",
        )


@dataclass
class Item:
    """物品/装备（P3 加属性/词缀：attack/defense/stat_bonus/heal_amount/affixes）。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    type: str = "material"        # weapon|armor|consumable|material|key|accessory
    rarity: str = "common"        # common|uncommon|rare|epic|legendary|mythic
    desc: str = ""
    effects: str = ""             # 效果说明（自然语言）
    icon: str = ""                # 图标文件名（存于 world_images_dir）
    # ---- P3 数值（武器/防具/消耗品；material/key 不用）----
    attack: int = 0               # 武器基础攻击
    defense: int = 0              # 防具基础防御
    stat_bonus: dict = field(default_factory=dict)   # {"str":2,"dex":1} 装备属性加成
    heal_amount: int = 0          # 消耗品回血量（旧口径，整数；新档走 heal_pct）
    # [百分比回血 2026-09-01] 回最大生命的百分比（5-100，int）。>0 优先于 heal_amount；
    # 旧档只填了 heal_amount 的药在读取层归一化成 heal_pct（_normalize_heal_pct）。
    heal_pct: int = 0
    affixes: list = field(default_factory=list)      # [{"slot":"prefix","name":"锋利","mods":{"atk":3}}]
    # [P5c] 装备槽位 id（head/chest/legs/feet/main_hand/off_hand/accessory1/accessory2 之一）。
    # LLM 生成时填，明确这件装备属于哪个槽。空串=未指定（引擎按 type 兜底反查）。
    slot: str = ""
    # [P6] 基础单价（>0 用此固定价；0 = trade_engine.compute_item_price 公式算）。
    # LLM 生成世界骨架/商店备货时可给个种子价，交易/补货定价纯 Python 算（不经 LLM）。
    base_price: int = 0
    # [P7g] 作为采集工具时的加成类型 key（herb/mine/wood/scavenge/water/energy...；空=非工具）。
    # 背包里有对应 tool_for 的物品时，采集成功率获 tool_bonus 加成（gather_engine 用）。
    tool_for: str = ""
    # [P7k3] 装备耐久：durability 当前耐久（0=失效），durability_max 上限（默认 100）。
    # 装备属性（attack/defense）按 durability/durability_max 比例生效（_durability_mult）。
    # durability_max<=0 表示不朽（不损耗，全额生效）；非装备（材料/钥匙/消耗品）默认满耐久不损耗。
    durability: int = 100
    durability_max: int = 100
    # [P7k2] 技能书：teach_skill 非空时，使用此物品教会玩家该技能（加 player.skills）。
    # 格式同 Skill dict（name/type/power/cost_mp/cooldown/damage_type/target/stat_scaling）。
    teach_skill: dict = field(default_factory=dict)
    # [P26a] 定情信物：送出此物 + 交情>=90（且已结义）-> 恋人跃迁。type 仍为 accessory 可正常赠送/上架。
    is_courtship_gift: bool = False
    # ---- [P34a] 器物体系三字段（与现有字段正交，不影响战斗/装备/交易分支）----
    # category: 次级分类（背包分类/reagent 匹配/商店备货过滤用）。空=default_category 推断。
    #   cultivate=鉴定卷轴/洗练石/资质丹（type=consumable 可消耗可出售）；forge/craft=锻造/制造
    #   材料（type=material）；skillbook=技能书（type=consumable+teach_skill）；misc=兜底。
    #   [!] type 按 §23 铁律永不变；category 是叠加维度，所有 if item.type== 分支不受影响。
    category: str = ""
    # level: 物品品阶档（与 rarity 正交）。0=凡品/无等级；1-N 钳制由 preset item_max_level 上限。
    #   高 level 物品只能在高 level 建筑锻造/炼制（P34c）、需对应 tier reagent 鉴定/洗练（P34b）、
    #   绝不出现在任何商店（P34a shop_max_item_level 过滤）。rarity=品质档，level=品阶档。
    level: int = 0
    # identified: 鉴定态。True=已鉴定（属性可见，旧档物品默认 True 守现有物品不受影响）；
    #   False=未鉴定（新掉落/新锻造装备默认 False，属性打码、前缀名未赋，必须用鉴定卷轴揭示，
    #   卖出价大幅折扣赌徒经济 sink）。非装备（material/key/cultivate reagent）恒 True 不受影响。
    identified: bool = True
    # ---- [P34b] reagent 子类型（cultivate reagent 专用：identify/refine/apt_*）----
    # 用于鉴定/洗练/资质丹的子类型区分（Item 无 kind 字段时无法区分鉴定卷轴 vs 资质丹）。
    # 非 cultivate 物品留空。白名单 _REAGENT_KIND_VALUES（from_dict 非法回退空串）。
    reagent_kind: str = ""
    # ---- [消耗品结构化效果] consume_effect：替代纯文学 desc，引擎 use_item 据此结算。
    # 结构 {"type":"stat_bonus"|"heal_full"|"heal_mp"|"cure"|"revive","stats":{"str":2,...},"amount":N}。
    # 与 heal_amount 二选一（有 consume_effect 时 heal_amount 留 0）；旧档无字段=空 dict 走旧 heal 回退。
    consume_effect: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "type": self.type, "rarity": self.rarity,
            "desc": self.desc, "effects": self.effects, "icon": self.icon,
            "attack": self.attack, "defense": self.defense,
            "stat_bonus": dict(self.stat_bonus),
            "heal_amount": self.heal_amount,
            "heal_pct": self.heal_pct,
            "affixes": [a for a in self.affixes if isinstance(a, dict)],
            "slot": self.slot,
            "base_price": self.base_price,
            "tool_for": self.tool_for,
            "durability": self.durability, "durability_max": self.durability_max,
            "teach_skill": dict(self.teach_skill) if isinstance(self.teach_skill, dict) else {},
            "is_courtship_gift": bool(self.is_courtship_gift),
            # [P34a] 器物体系三字段（白名单/钳制在 from_dict；to_dict 透传当前值）
            "category": self.category if self.category in _ITEM_CATEGORY_VALUES else "",
            "level": max(0, int(self.level)),
            "identified": bool(self.identified),
            # [P34b] reagent 子类型（白名单回退空串）
            "reagent_kind": self.reagent_kind if self.reagent_kind in _REAGENT_KIND_VALUES else "",
            "consume_effect": dict(self.consume_effect) if isinstance(self.consume_effect, dict) and self.consume_effect else {},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        t = d.get("type", "material") or "material"
        if t not in _ITEM_TYPE_VALUES:
            t = "material"
        r = d.get("rarity", "common") or "common"
        if r not in _ITEM_RARITY_VALUES:
            r = "common"
        sb = d.get("stat_bonus") or {}
        if not isinstance(sb, dict):
            sb = {}
        aff = d.get("affixes") or []
        if not isinstance(aff, list):
            aff = []
        aff = [a for a in aff if isinstance(a, dict)]
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            type=t,
            rarity=r,
            desc=d.get("desc", "") or "",
            effects=d.get("effects", "") or "",
            icon=d.get("icon", "") or "",
            attack=int(d.get("attack", 0) or 0),
            defense=int(d.get("defense", 0) or 0),
            stat_bonus=dict(sb),
            heal_amount=int(d.get("heal_amount", 0) or 0),
            # [百分比回血 2026-09-01] 钳 0-100；旧档只填 heal_amount 的在消费层归一化
            heal_pct=max(0, min(100, int(d.get("heal_pct", 0) or 0))),
            affixes=aff,
            # [!] slot 白名单（守 §11）：非法槽值（LLM/手改 JSON 给 "ring"）既不被
            # 任何槽使用又不触发装备覆盖兜底，静默坏档——回退空串走引擎按 type 反查。
            slot=(str(d.get("slot", "") or "") if str(d.get("slot", "") or "")
                  in EQUIPMENT_SLOTS else ""),
            base_price=max(0, int(d.get("base_price", 0) or 0)),
            tool_for=str(d.get("tool_for", "") or ""),
            durability=max(0, _gi(d, "durability", 100)),
            durability_max=max(0, _gi(d, "durability_max", 100)),
            teach_skill=dict(d.get("teach_skill")) if isinstance(d.get("teach_skill"), dict) and d.get("teach_skill") else {},
            is_courtship_gift=bool(d.get("is_courtship_gift", False)),
            # [P34a] 器物体系三字段：category 白名单回退空串（引擎读时按 default_category 推断），
            # level 钳制 >=0（上限由 preset item_max_level 在生成处钳，from_dict 不强制上限守
            # [P14] 一行式），identified 默认 True 守旧档物品不受影响。
            category=(str(d.get("category", "") or "")
                      if str(d.get("category", "") or "") in _ITEM_CATEGORY_VALUES else ""),
            level=max(0, _gi(d, "level", 0)),
            identified=bool(d.get("identified", True)),
            # [P34b] reagent 子类型白名单回退空串（非 cultivate 物品留空）
            reagent_kind=(str(d.get("reagent_kind", "") or "")
                          if str(d.get("reagent_kind", "") or "") in _REAGENT_KIND_VALUES else ""),
            # [消耗品结构化效果] 白名单 type + stats 钳正整数；非法回退空 dict 走旧 heal 回退
            consume_effect=_parse_consume_effect(d.get("consume_effect")),
        )


def default_category(item: "Item") -> str:
    """[P34a] 据 type+teach_skill 推断物品默认 category（item.category 为空时用）。

    与 type 正交的次级分类，专用于背包分类/reagent 匹配/商店备货过滤：
    - consumable + teach_skill 非空 -> skillbook（技能书）
    - weapon -> weapon / armor -> armor / accessory -> accessory
    - material -> material（forge/craft 细分由生成处显式标，不在此推断）
    - consumable -> consume（普通消耗品；cultivate reagent 由生成处显式标 cultivate）
    - key -> key / 兜底 -> misc
    [!] 只在 category 为空时调用方回退此函数；显式标的 category 优先（forge/craft/cultivate
    无法仅凭 type 推断，必须生成处显式赋值）。
    """
    t = getattr(item, "type", "") or ""
    if getattr(item, "teach_skill", None) and t == "consumable":
        return "skillbook"
    return {
        "weapon": "weapon", "armor": "armor", "accessory": "accessory",
        "material": "material", "key": "key", "consumable": "consume",
    }.get(t, "misc")


def effective_category(item: "Item") -> str:
    """[P34a] 取物品有效 category：显式标用显式值，否则 default_category 推断。

    背包分类/商店备货过滤/reagent 匹配统一走此函数，避免每处重复推断逻辑。
    """
    c = getattr(item, "category", "") or ""
    if c in _ITEM_CATEGORY_VALUES and c:
        return c
    return default_category(item)


# ---- [P7h] 战斗技能 + 敌人 AI ----
_SKILL_TYPE_VALUES = ("attack", "heal", "buff")
_DAMAGE_TYPE_VALUES = ("physical", "magical")
_AI_PATTERN_VALUES = ("aggressive", "defensive", "caster", "balanced")
# [P 验收] NPC.combat_role 白名单（gen 骨架显式战斗角色标注；空=未标注走关键词兜底）。
_COMBAT_ROLE_VALUES = ("boss", "hostile", "friendly", "none")
_STAT_KEYS = ("str", "dex", "int", "vit", "luk")
# [P9] 元素/系别（Skill.element + Talent.element 匹配用；空串=无系别/通用）。
# 让「雷天灵根」只加成雷系技能：talent.element 匹配 skill.element 才触发 skill_dmg_mult。
_ELEMENT_VALUES = ("", "fire", "thunder", "ice", "wind", "wood", "metal", "earth", "light", "dark", "physical")

# [P30] 状态效果 condition key 白名单（抽象 key，题材化显示名在 combat_engine._CONDITION_GENRE_ZH）。
# DoT: poisoned/burning/bleeding；控制: stunned/frozen/feared；减益: shocked/blinded/chilled/entangled；
# 增益: protected/enraged；元素前置: wet（无直接效果，元素互动触发器）。
_CONDITION_VALUES = ("", "poisoned", "burning", "bleeding", "stunned", "frozen",
                     "feared", "shocked", "blinded", "chilled", "entangled",
                     "protected", "enraged", "wet")

# [P30] 技能目标模式（single=单体，aoe_enemy=全体敌，aoe_all=全体双方，random_n=随机 N 敌）
_TARGET_PATTERN_VALUES = ("single", "aoe_enemy", "aoe_all", "random_n")


def _clean_elements(raw) -> list[str]:
    """[P9] NPC.elements 白名单清洗：滤非法 key + 去重 + 保序，最多 2 个。

    physical 不收（纯物理不是元素身份，克制矩阵对 physical 恒 1.0）。
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for e in raw:
        e = str(e or "").strip()
        if e in _ELEMENT_VALUES and e not in ("", "physical") and e not in out:
            out.append(e)
    return out[:2]


@dataclass
class Skill:
    """[P7h] 战斗技能（攻击/治疗/buff，纯 Python 结算，combat_engine 用）。

    玩家/NPC 的 skills 字段是 list[dict]（内联技能定义，自包含不查表）。
    本 dataclass 提供 to_dict/from_dict 用于规范化技能 dict（校验白名单 + 强制类型）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    desc: str = ""
    type: str = "attack"        # attack/heal/buff（白名单）
    power: int = 10             # 威力系数（attack=伤害倍率基数，heal=基础治疗量）
    cost_mp: int = 0            # 法力消耗（0=不耗蓝）
    cooldown: int = 0           # 冷却回合数（0=无冷却，>0=用后需等 N 回合）
    damage_type: str = "physical"  # physical/magical（attack 类用：决定用 atk 还是 magic_atk 缩放）
    target: str = "enemy"       # self/enemy（heal/buff 通常 self，attack 通常 enemy）
    stat_scaling: str = ""      # 缩放属性 key（str/dex/int；空=不缩放，用 power 固定值）
    element: str = ""           # [P9] 元素/系别（fire/thunder/...；空=无系别；talent.element 匹配触发 skill_dmg_mult）
    level: int = 1              # [P12] 熟练度等级 1-5（威力每级 +15%；cap 5，ce._SKILL_MAX_LEVEL）
    xp: int = 0                 # [P12] 当前熟练度（施展随机 +8~16；满 100+50*level 升级）
    inflicts: list = field(default_factory=list)       # [P30] 附带状态 [{condition,chance,duration}]
    target_pattern: str = "single"  # [P30] 目标模式 single/aoe_enemy/aoe_all/random_n

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc, "type": self.type,
            "power": self.power, "cost_mp": self.cost_mp, "cooldown": self.cooldown,
            "damage_type": self.damage_type, "target": self.target,
            "stat_scaling": self.stat_scaling, "element": self.element,
            "level": self.level, "xp": self.xp,
            "inflicts": [dict(x) for x in self.inflicts if isinstance(x, dict)],
            "target_pattern": self.target_pattern,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        if not isinstance(d, dict):
            return cls()
        t = d.get("type", "attack") or "attack"
        if t not in _SKILL_TYPE_VALUES:
            t = "attack"
        dt = d.get("damage_type", "physical") or "physical"
        if dt not in _DAMAGE_TYPE_VALUES:
            dt = "physical"
        tgt = d.get("target", "enemy") or "enemy"
        if tgt not in ("self", "enemy"):
            tgt = "enemy"
        ss = d.get("stat_scaling", "") or ""
        if ss not in _STAT_KEYS:
            ss = ""
        el = d.get("element", "") or ""
        if el not in _ELEMENT_VALUES:
            el = ""
        # [P30] inflicts 规整 + target_pattern 白名单
        raw_inf = d.get("inflicts") or []
        clean_inf = []
        if isinstance(raw_inf, list):
            for x in raw_inf:
                if not isinstance(x, dict):
                    continue
                ck = str(x.get("condition", "") or "").strip()
                if ck not in _CONDITION_VALUES or not ck:
                    continue
                clean_inf.append({
                    "condition": ck,
                    "chance": max(0.0, min(1.0, float(x.get("chance", 1.0) or 1.0))),
                    "duration": max(1, _gi(x, "duration", 1)),
                })
        tp = d.get("target_pattern", "single") or "single"
        if tp not in _TARGET_PATTERN_VALUES:
            tp = "single"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            type=t,
            power=max(0, _gi(d, "power", 10)),
            cost_mp=max(0, _gi(d, "cost_mp", 0)),
            cooldown=max(0, _gi(d, "cooldown", 0)),
            # [P12] 熟练度 round-trip（缺省 1/0；钳制防脏数据）
            level=max(1, min(10, _gi(d, "level", 1))), xp=max(0, _gi(d, "xp", 0)),
            damage_type=dt,
            target=tgt,
            stat_scaling=ss,
            element=el,
            inflicts=clean_inf,
            target_pattern=tp,
        )


def _skill_to_clean_dict(d) -> dict:
    """把任意技能 dict/str/Skill 规范化为干净 dict（迁移老 list[str] + 校验）。"""
    if isinstance(d, Skill):
        return d.to_dict()
    if isinstance(d, str):
        # [P7h] 老 list[str] 技能名 -> 默认攻击技能（向后兼容）
        return {"id": f"legacy_{d}", "name": d, "desc": "", "type": "attack",
                "power": 10, "cost_mp": 0, "cooldown": 0,
                "damage_type": "physical", "target": "enemy", "stat_scaling": ""}
    if not isinstance(d, dict):
        return {}
    return Skill.from_dict(d).to_dict()


def _migrate_skills(raw) -> list:
    """[P7h] skills 字段迁移：list[str|dict|Skill] -> list[dict]（规范化）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        cd = _skill_to_clean_dict(x)
        if cd:
            out.append(cd)
    return out


# ---- [P9] 天赋 + 元素亲和 ----
# effects 钩子白名单（LLM 出种子值，引擎钳制防脏数据；get_talent_mult 据此聚合）。
# skill_dmg_mult: 技能伤害乘数（element 匹配时只对该系技能生效）
# atk_mult: 普攻/总攻击乘数；gather_mult/craft_mult/check_mult/loot_mult: 各系统乘数
# luck_bonus: 幸运加值（int）；stat_bonus: {str/dex/int/vit/luk: 加值}；mp_bonus: 法力上限加值
_TALENT_EFFECT_KEYS = (
    "skill_dmg_mult", "atk_mult", "gather_mult", "craft_mult", "check_mult",
    "loot_mult", "luck_bonus", "stat_bonus", "mp_bonus",
    # [装备新属性 2026-09-01] 概率型战斗属性乘数（词缀直加后乘天赋，compute_stats 聚合）
    "counter_rate", "combo_rate", "lifesteal",
)


def _talent_cost_for_rarity(rarity: str) -> int:
    """[P9] 据天赋等级推算消耗天赋点（cost 为 0 时用此）。

    common=1 / uncommon=2 / rare=3 / epic=4 / legendary=5。强天赋贵，效果也强
    （_GENRE_TALENT_TEMPLATES 兜底池按 rarity 给 effects 强度）。默认 10 点能买约 3-4 个稀有天赋。
    """
    return {"common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5, "mythic": 6}.get(rarity, 1)


@dataclass
class Talent:
    """[P9] 天赋（LLM 据题材生成的角色特质，effects 参与各种游戏内计算）。

    题材化命名（仙侠=雷天灵根/剑心通明，古代=种田好手/铁匠传人，现代=神枪手...）。
    effects 由 LLM 出种子值，引擎白名单钳制；element 非空时 skill_dmg_mult 只加成同系技能。
    玩家开局「随机锁死 1 个 + 天赋点自选」（talent_points 默认 10，QQ 码解锁 60，[P12] 与属性彩蛋同口径 +50）；
    NPC 天赋由 LLM 在骨架生成时填（player.npc 不参与选择流程）。
    [!] 天赋分等级(rarity)和价格(cost)：强天赋 cost 高（legendary cost 5，common cost 1），
    天赋点是购买预算不是固定 1 个 1 点。cost 由 rarity 推算（_talent_cost_for_rarity），
    LLM 可显式给 cost 覆盖（钳制 1-5）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    desc: str = ""
    genre: str = ""             # 题材 id（xianxia/modern/...；""=通用）
    element: str = ""           # 元素亲和（thunder/fire/...；空=无系别，skill_dmg_mult 全局生效）
    rarity: str = "common"      # 天赋等级（common/uncommon/rare/epic/legendary/mythic；影响效果强度档）
    cost: int = 0               # [P9] 价格（消耗天赋点；0=按 rarity 自动推算 _talent_cost_for_rarity）
    effects: dict = field(default_factory=dict)   # 见 _TALENT_EFFECT_KEYS 钩子白名单
    is_random: bool = True      # True=可随机锁死/自选；False=仅自选（保留扩展）

    def effective_cost(self) -> int:
        """实际消耗天赋点：cost>0 用之，否则按 rarity 推算。"""
        return _talent_cost_for_rarity(self.rarity) if int(self.cost or 0) <= 0 else max(1, min(5, int(self.cost)))

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc,
            "genre": self.genre, "element": self.element, "rarity": self.rarity,
            "cost": max(0, int(self.cost)),
            "effects": dict(self.effects) if isinstance(self.effects, dict) else {},
            "is_random": bool(self.is_random),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Talent":
        if not isinstance(d, dict):
            return cls()
        el = d.get("element", "") or ""
        if el not in _ELEMENT_VALUES:
            el = ""
        rar = d.get("rarity", "common") or "common"
        if rar not in _ITEM_RARITY_VALUES:
            rar = "common"
        # effects 白名单清洗：只保留 _TALENT_EFFECT_KEYS 的键 + 强制类型
        raw_eff = d.get("effects") or {}
        if not isinstance(raw_eff, dict):
            raw_eff = {}
        eff = {}
        for k in _TALENT_EFFECT_KEYS:
            if k not in raw_eff:
                continue
            v = raw_eff[k]
            if k == "stat_bonus":
                if isinstance(v, dict):
                    eff[k] = {sk: int(round(sv)) for sk, sv in v.items()
                              if sk in _STAT_KEYS and isinstance(sv, (int, float))}
            elif k == "luck_bonus" or k == "mp_bonus":
                try:
                    eff[k] = int(v)
                except (TypeError, ValueError):
                    continue
            else:  # 乘数类（float）
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                # 钳制合理区间防脏数据（乘数 0.5~5.0；上限 5.0 给 LLM 强天赋留空间，见 review-changes）
                eff[k] = max(0.5, min(5.0, fv))
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            genre=str(d.get("genre", "") or ""),
            element=el,
            rarity=rar,
            cost=max(0, _gi(d, "cost", 0)),
            effects=eff,
            is_random=bool(d.get("is_random", True)),
        )


@dataclass
class Recipe:
    """[P7k1] 合成配方（inputs -> output，纯 Python 校验/消耗/产出，不经 LLM）。

    inputs 是 item_id 列表（每种材料消耗 1 个，配合去重背包设计）。output 进背包。
    station 是题材化工作台名（炼丹炉/工作台/灶台，仅展示不强制）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    desc: str = ""
    inputs: list = field(default_factory=list)   # [item_id, ...] 输入材料（每种消耗 1 个）
    output_item_id: str = ""
    output_qty: int = 1
    station: str = ""                             # 题材化工作台名（展示用）
    # [P8] 配方难度 0-100（驱动 crafting_engine success_chance，stat_int + 工作台加成抵消难度）。
    # 0=必成（老配方兼容）；越高越难但大成功（roll>=0.9）时产出稀有度升档。LLM 可给种子值。
    difficulty: int = 0
    # ---- [P34c] 建筑门控（住宅内建筑等级门控锻造/炼制高 level 物品）----
    # required_building: 建筑种类 key（forge/alchemy/refine/study；空=无门控，老配方兼容）。
    # min_building_level: 所需建筑最低等级（玩家宅内该 kind 建筑 level >= 此值才能锻造）。
    # output_level: 产出物品的 level（0=不指定走 output 物品自身 level；高 level 配方需高 level 建筑）。
    required_building: str = ""
    min_building_level: int = 0
    output_level: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc,
            "inputs": [str(x) for x in self.inputs if isinstance(x, str)],
            "output_item_id": self.output_item_id, "output_qty": max(1, int(self.output_qty)),
            "station": self.station,
            "difficulty": max(0, min(100, int(self.difficulty))),
            "required_building": self.required_building if self.required_building in _BUILDING_KIND_VALUES else "",
            "min_building_level": max(0, int(self.min_building_level)),
            "output_level": max(0, int(self.output_level)),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        if not isinstance(d, dict):
            return cls()
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            inputs=[str(x) for x in (d.get("inputs") or []) if isinstance(x, str)],
            output_item_id=str(d.get("output_item_id", "") or ""),
            output_qty=max(1, _gi(d, "output_qty", 1)),
            station=str(d.get("station", "") or ""),
            difficulty=max(0, min(100, _gi(d, "difficulty", 0))),
            required_building=(str(d.get("required_building", "") or "")
                               if str(d.get("required_building", "") or "") in _BUILDING_KIND_VALUES else ""),
            min_building_level=max(0, _gi(d, "min_building_level", 0)),
            output_level=max(0, _gi(d, "output_level", 0)),
        )


@dataclass
class Quest:
    """任务（[P7i] 加结构化 objectives/rewards + 接取/领奖状态机）。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    objective: str = ""
    giver_npc_id: str = ""
    reward_text: str = ""         # 奖励说明（自然语言，展示用）
    # ---- P4 任务进度状态 ----
    status: str = "available"     # available|active|completed|failed|abandoned|claimed|locked（白名单；locked=任务链未解锁段）
    progress: int = 0             # 0-100 进度（兼容老；结构化 objectives 时由代码算）
    started_at_tick: int = 0
    completed_at_tick: int = 0
    # ---- [P7i] 结构化目标 + 奖励 ----
    # objectives: [{type: kill|gather|talk|visit|collect, target, count, current, desc}]
    # type 决定 update_progress 的事件匹配：kill=击败 NPC(role/id/name)、gather=采集资源 type、
    # talk=与 NPC 对话(name)、visit=到达地点(id/name/region)、collect=获得物品(id/name/rarity)。
    objectives: list = field(default_factory=list)
    rewards: dict = field(default_factory=dict)   # {items:[item_id], gold:int, xp:int}
    claimed_at_tick: int = 0
    # ---- [P15b1] task chain: id of the next chain segment (unlocked to available after claiming reward at this ring; empty = not on chain) ----
    chain_next_id: str = ""
    # [任务改造] 任务链类型：main=主线链段（链式递进）/ side=支线（独立）。区分用于 UI 分区 + 主线引导。
    chain: str = "side"
    # [剧情线 P46] 归属 arc hook 任务：f"{arc_id}#{stage_idx}"（单一权威，stage 不存
    # quest_id，arc 引擎每日扫此前缀反查任务状态）；空=非 arc 任务。
    hook_arc_id: str = ""
    # [R0/B01-F10 2026-09-30] 领奖满包滞留的奖励物品 id（claim_reward 入包失败时落此，
    # 贡献确认/gold/xp 已一次性发放不重发；claim_pending_items 补领，取空清列表）。
    pending_items: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "objective": self.objective,
            "giver_npc_id": self.giver_npc_id, "reward_text": self.reward_text,
            "status": self.status, "progress": self.progress,
            "started_at_tick": self.started_at_tick, "completed_at_tick": self.completed_at_tick,
            "objectives": [o for o in self.objectives if isinstance(o, dict)],
            "rewards": dict(self.rewards) if isinstance(self.rewards, dict) else {},
            "claimed_at_tick": self.claimed_at_tick,
            "chain_next_id": self.chain_next_id,
            "chain": self.chain,
            "hook_arc_id": str(self.hook_arc_id or ""),
            "pending_items": [str(x) for x in (self.pending_items or [])],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Quest":
        st = d.get("status", "available") or "available"
        if st not in _QUEST_STATUS_VALUES:
            st = "available"
        ch = str(d.get("chain", "side") or "side")
        if ch not in _QUEST_CHAIN_VALUES:
            ch = "side"

        def _i(key: str, default: int) -> int:
            v = d.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        # [P7i] objectives 清洗：type 白名单 + 强制类型
        raw_obj = d.get("objectives") or []
        if not isinstance(raw_obj, list):
            raw_obj = []
        objectives = []
        for o in raw_obj:
            if not isinstance(o, dict):
                continue
            ot = o.get("type", "") or ""
            if ot not in _QUEST_OBJECTIVE_TYPES:
                continue
            objectives.append({
                "type": ot,
                "target": str(o.get("target", "") or ""),
                # [S04 余项] investigate/escort 的第二目标（地点名）——其余类型忽略
                "target2": str(o.get("target2", "") or "")[:24],
                "count": max(1, _gi(o, "count", 1)),
                "current": max(0, _gi(o, "current", 0)),
                "desc": str(o.get("desc", "") or ""),
            })
        raw_rw = d.get("rewards") or {}
        if not isinstance(raw_rw, dict):
            raw_rw = {}
        rewards = {
            "items": [str(x) for x in (raw_rw.get("items") or []) if isinstance(x, str)],
            "gold": max(0, int(raw_rw.get("gold", 0) or 0)),
            "xp": max(0, int(raw_rw.get("xp", 0) or 0)),
        }
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d.get("title", "") or "",
            objective=d.get("objective", "") or "",
            giver_npc_id=d.get("giver_npc_id", "") or "",
            reward_text=d.get("reward_text", "") or "",
            status=st,
            progress=max(0, min(100, _i("progress", 0))),
            started_at_tick=max(0, _i("started_at_tick", 0)),
            completed_at_tick=max(0, _i("completed_at_tick", 0)),
            objectives=objectives,
            rewards=rewards,
            claimed_at_tick=max(0, _i("claimed_at_tick", 0)),
            chain_next_id=str(d.get("chain_next_id", "") or ""),
            chain=ch,
            hook_arc_id=str(d.get("hook_arc_id", "") or "")[:64],
            pending_items=[str(x) for x in (d.get("pending_items") or []) if isinstance(x, str)],
        )


# ---- [P24a] 家具四类白名单（储物/床铺/装饰/陈列，题材化命名见 home_engine 池）----
# ---- [剧情线 P46 2026-09-12] 前瞻导演层数据模型（推进/生成见 story_arc_engine）----

@dataclass
class StoryStage:
    """剧情线单阶段（天数钳 1-5；done 旗标 = 每日推进幂等的唯一记账，
    [!] 引擎「事件已发布或已持久排队后才标 done」——公告不可丢）。
    hook：LLM 提议且经引擎清洗的介入点（{type,target,count,desc}，可空 dict；
    任务归属单一权威在 Quest.hook_arc_id，本字段不存 quest_id）。
    outcome：[S03/R2 2026-09-30] 本段结算结果（success/compromise/setback，引擎到期
    评估写入；末段 setback -> 线 failed 而非 succeeded——「策划者得逞」与「线走完」
    从此是两件事，守开发计划 5.1）。"""
    name: str = ""
    desc: str = ""
    kind: str = "scheme"                 # _ARC_STAGE_KIND_VALUES
    days: int = 2                        # 1-5（_gi 防 0 吞后再钳）
    done: bool = False
    hook: dict = field(default_factory=dict)
    outcome: str = ""                    # ""|success|compromise|setback|interrupted
    id: str = ""                         # 同一剧情线内稳定 ID；续写不增删或重排阶段
    condition: dict = field(default_factory=dict)  # 可选实体条件 {type:npc_dead,target_id}
    started_tick: int = -1
    due_tick: int = -1
    resolved_tick: int = -1

    def to_dict(self) -> dict:
        return {"name": self.name, "desc": self.desc, "kind": self.kind,
                "days": int(self.days), "done": bool(self.done),
                "hook": dict(self.hook) if isinstance(self.hook, dict) else {},
                "outcome": self.outcome if self.outcome in _STAGE_OUTCOME_VALUES else "",
                "id": str(self.id or "")[:16],
                "condition": dict(self.condition) if isinstance(self.condition, dict) else {},
                "started_tick": max(-1, int(self.started_tick)),
                "due_tick": max(-1, int(self.due_tick)),
                "resolved_tick": max(-1, int(self.resolved_tick))}

    @classmethod
    def from_dict(cls, d: dict) -> "StoryStage":
        if not d or not isinstance(d, dict):
            return cls()
        kind = d.get("kind", "scheme") or "scheme"
        if kind not in _ARC_STAGE_KIND_VALUES:
            kind = "scheme"
        raw_hook = d.get("hook")
        hook = raw_hook if isinstance(raw_hook, dict) else {}
        ot = str(hook.get("type", "") or "")
        if ot not in ("kill", "talk", "visit", "collect", "deliver_items",
                      "dungeon_room_resolved", "dungeon_cleared"):
            hook = {}
        else:
            hook = {"type": ot,
                    "target": str(hook.get("target", "") or "")[:24],
                    "count": max(1, min(5, _gi(hook, "count", 1))),
                    "desc": str(hook.get("desc", "") or "")[:60],
                    "stance": ("oppose" if hook.get("stance") == "oppose" else "support"),
                    # [P46 v1.1 复查修复 qwen] derived 标记必须随序列化存活——否则
                    # 存/读档后防重派标记丢失，giver 死亡删任务后 hook 被重派
                    "derived": bool(hook.get("derived", False))}
        raw_condition = d.get("condition")
        condition = {}
        if isinstance(raw_condition, dict) and raw_condition.get("type") == "npc_dead":
            target_id = str(raw_condition.get("target_id", "") or "")[:64]
            if target_id:
                condition = {"type": "npc_dead", "target_id": target_id}
        ot2 = str(d.get("outcome", "") or "")
        return cls(
            name=str(d.get("name", "") or "")[:24],
            desc=str(d.get("desc", "") or "")[:120],
            kind=kind,
            days=max(1, min(5, _gi(d, "days", 2))),
            done=bool(d.get("done", False)),
            hook=hook,
            outcome=ot2 if ot2 in _STAGE_OUTCOME_VALUES else "",
            id=str(d.get("id", "") or "")[:16],
            condition=condition,
            started_tick=max(-1, _gi(d, "started_tick", -1)),
            due_tick=max(-1, _gi(d, "due_tick", -1)),
            resolved_tick=max(-1, _gi(d, "resolved_tick", -1)),
        )


@dataclass
class StoryArc:
    """一条剧情线：策划者 + 参与者 + 2-5 阶段。到期日 = start_day + 各段 days 累计
    （单一真相源，不逐段存 due_day）。终态（succeeded/failed/aborted）由引擎滚动
    保留最近 8 条（story_arc_engine._roll_terminals）。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    premise: str = ""                    # 一句话前提（LLM 出，截 120）
    mastermind_id: str = ""              # 策划者（死亡 -> 线 aborted）
    participant_ids: list = field(default_factory=list)
    stages: list = field(default_factory=list)   # list[StoryStage]，2-5 段
    current_stage: int = 0
    start_day: int = 1
    status: str = "active"               # _ARC_STATUS_VALUES
    end_day: int = 0
    archetype: str = ""                  # [P46.1] 原型 key（_ARC_ARCHETYPE_VALUES；防同质轮换）
    faction_id: str = ""                 # 关联势力（可空；软结算受益方）
    outcome: str = ""                    # 终局一句话（随 major 事件进编年史）
    source: str = "llm"                  # llm | template
    player_helped: int = 0               # hook 任务被 claimed 领奖计数（v2 站边钩子）
    player_opposed: int = 0              # 对立方已确认介入数
    interventions: list = field(default_factory=list)  # [{stage_id,quest_id,stance,tick}]
    revision: int = 0                    # 仅未来阶段重规划成功后递增
    replan_pending: bool = False
    replan_reason: str = ""
    last_replan_day: int = 0             # 每线每天最多尝试一次

    def __post_init__(self) -> None:
        used = set()
        for idx, stage in enumerate(self.stages or []):
            if not isinstance(stage, StoryStage):
                continue
            sid = str(stage.id or "")[:16]
            if not sid or sid in used:
                sid = str(idx)
                suffix = 0
                while sid in used:
                    suffix += 1
                    sid = f"stage{idx}_{suffix}"
            stage.id = sid
            used.add(sid)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "premise": self.premise,
            "mastermind_id": str(self.mastermind_id or ""),
            "participant_ids": [str(x) for x in (self.participant_ids or [])
                                if isinstance(self.participant_ids, list) and str(x)],
            "stages": [s.to_dict() for s in (self.stages or []) if isinstance(s, StoryStage)],
            "current_stage": int(self.current_stage), "start_day": int(self.start_day),
            "status": self.status, "end_day": int(self.end_day),
            "archetype": str(self.archetype or ""),
            "faction_id": str(self.faction_id or ""), "outcome": str(self.outcome or ""),
            "source": self.source, "player_helped": max(0, int(self.player_helped or 0)),
            "player_opposed": max(0, int(self.player_opposed or 0)),
            "interventions": [dict(x) for x in self.interventions if isinstance(x, dict)][-10:],
            "revision": max(0, int(self.revision or 0)),
            "replan_pending": bool(self.replan_pending),
            "replan_reason": str(self.replan_reason or "")[:80],
            "last_replan_day": max(0, int(self.last_replan_day or 0)),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StoryArc":
        if not d or not isinstance(d, dict):
            return cls()
        status = d.get("status", "active") or "active"
        if status not in _ARC_STATUS_VALUES:
            status = "active"
        source = d.get("source", "llm") or "llm"
        if source not in _ARC_SOURCE_VALUES:
            source = "llm"
        raw_st = d.get("stages")
        stages = [StoryStage.from_dict(s)
                  for s in (raw_st if isinstance(raw_st, list) else [])
                  if isinstance(s, dict)][:5]
        cur = max(0, _gi(d, "current_stage", 0))
        cur = min(cur, len(stages) if status != "active" else len(stages) - 1) if stages else 0
        raw_p = d.get("participant_ids")
        return cls(
            id=str(d.get("id", "") or "") or str(uuid.uuid4()),
            title=str(d.get("title", "") or "")[:48],
            premise=str(d.get("premise", "") or "")[:120],
            mastermind_id=str(d.get("mastermind_id", "") or ""),
            # [!] isinstance 入口防御（守 §11）：非 list 垃圾（手改 42/字符串）直接落空，
            # 绝不迭代（int 迭代炸 from_dict -> load_all_worlds 整档失联）。
            participant_ids=[str(x) for x in (raw_p if isinstance(raw_p, list) else [])
                             if isinstance(x, str) and x][:8],
            stages=stages,
            current_stage=cur,
            start_day=max(1, _gi(d, "start_day", 1)),
            status=status,
            archetype=str(d.get("archetype", "") or "")[:32],
            end_day=max(0, _gi(d, "end_day", 0)),
            faction_id=str(d.get("faction_id", "") or ""),
            outcome=str(d.get("outcome", "") or "")[:80],
            source=source,
            player_helped=max(0, _gi(d, "player_helped", 0)),
            player_opposed=max(0, _gi(d, "player_opposed", 0)),
            interventions=[{"stage_id": str(x.get("stage_id", "") or "")[:16],
                            "quest_id": str(x.get("quest_id", "") or "")[:64],
                            "stance": "oppose" if x.get("stance") == "oppose" else "support",
                            "tick": max(0, _gi(x, "tick", 0))}
                           for x in (d.get("interventions") if isinstance(d.get("interventions"), list) else [])
                           if isinstance(x, dict)][-10:],
            revision=max(0, _gi(d, "revision", 0)),
            replan_pending=bool(d.get("replan_pending", False)),
            replan_reason=str(d.get("replan_reason", "") or "")[:80],
            last_replan_day=max(0, _gi(d, "last_replan_day", 0)),
        )


_FURNITURE_KIND_VALUES = ("storage", "bed", "decor", "display")
# ---- [P34c] 住宅内建筑种类白名单（带等级，门控锻造/炼制/洗练高 level 物品）----
# forge=锻造屋（门控 weapon/armor 配方）/ alchemy=炼丹房（门控 consumable/药/丹配方）/
# refine=洗练室（门控 P34b 洗练/鉴定的 tier 上限）/ study=书房（门控 skillbook 研读/技能升级，v1 预留）/
# garden=[P35] 灵田（等级=地块数，种植系统载体）/ warehouse=[网格v2] 仓库（等级=仓库容量档，
# 必须建造才能用仓库；存量档 ensure_home_grid 自动补建）。
_BUILDING_KIND_VALUES = ("", "forge", "alchemy", "refine", "study", "garden", "warehouse")


def _farm_plot_dict(p) -> Optional[dict]:
    """[P35] 种植地块白名单规整（to_dict/from_dict 共用）：seed_id 非空才保留，数值钳制。"""
    if not isinstance(p, dict):
        return None
    seed_id = str(p.get("seed_id", "") or "")
    if not seed_id:
        return None
    return {
        "seed_id": seed_id,
        "planted_day": max(1, _gi(p, "planted_day", 1)),
        "stage": max(0, min(3, _gi(p, "stage", 0))),
        "grown": max(0, _gi(p, "grown", 0)),
        "watered": bool(p.get("watered", False)),
        "dry_days": max(0, _gi(p, "dry_days", 0)),
        "pest": bool(p.get("pest", False)),
        "variant": bool(p.get("variant", False)),
    }


@dataclass
class Home:
    """[P24a] 住宅（挂在聚落上，不新建 Location）：私人仓库 / 休憩 / 家具 / 陈列架。

    「在住宅」= 玩家当前地点是聚落且在此聚落拥有宅（多聚落可多宅，每聚落限一宅）。
    stash/display_shelf 存 item_id 引用（不占负重，容量由家具扩容）；出仓/取回必须走
    combat_engine.try_add_to_inventory（守 §22 入包统一不变量）。tier 1-3 的题材化
    档名（小屋/宅院/庄园 -> 仙家洞府/灵宅/仙府 等）由 home_engine._GENRE_HOME_TEMPLATES 翻译。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    desc: str = ""
    location_id: str = ""        # 所在聚落 id
    tier: int = 1                # 1-3（白名单钳制）
    purchased_day: int = 1       # 购宅日（world.day_count 快照）
    stash: list[str] = field(default_factory=list)          # 仓库按件存 item_id（可重复；容量 30 + 储物家具 x10）
    furniture: list = field(default_factory=list)           # [{name,kind,price,day}]（kind 白名单）
    display_shelf: list[str] = field(default_factory=list)  # 陈列 item_id（容量 4 + 陈列家具 x2）
    # [P34c] 住宅内建筑（带等级，门控锻造/炼制/洗练高 level 物品）。entry {name,kind,level,price,day}。
    # kind 白名单 _BUILDING_KIND_VALUES（forge/alchemy/refine/study + [P35] garden）；level 1-N 钳 buildings_max_level。
    # 与 furniture 区分：furniture 扩容仓库/陈列（容量耦合），buildings 门控配方/洗练 tier（生产力耦合）。
    buildings: list = field(default_factory=list)
    # [P35] 种植地块（garden 建筑解锁，等级=地块数）。plot 字段见 _farm_plot_dict；farm_engine 驱动。
    garden: list = field(default_factory=list)
    # [住宅网格v2 2026-08-24] 网格迁移版本：0=老档（加载时 ensure_home_grid 补建筑/家具 slot
    # 并自动补仓库建筑），1=新档（buy_home 置 1，仓库需自建）。幂等迁移标记。
    grid_version: int = 1
    # [P60 宴请 2026-09-26] 上次设宴的 day_count（冷却 5 天，防天天摆酒刷交情）。
    last_banquet_day: int = 0
    # [P50 地点网格 2026-09-25] 宅邸在地点网格中的安置格 "gx,gy"（空=未安置）。
    # [!] 网格几何是纯函数推导（place_grid_engine.grid_for_location），零落盘零迁移；
    #     唯一持久化就是这一个安置坐标。
    grid_cell: str = "" 

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc,
            "location_id": self.location_id,
            "tier": max(1, min(3, int(self.tier))),
            "purchased_day": max(1, int(self.purchased_day)),
            "stash": [str(x) for x in (self.stash or []) if isinstance(x, str)],
            "furniture": [dict(f) for f in (self.furniture or []) if isinstance(f, dict)],
            "display_shelf": [str(x) for x in (self.display_shelf or []) if isinstance(x, str)],
            "buildings": [dict(b) for b in (self.buildings or []) if isinstance(b, dict)
                          and b.get("kind") in _BUILDING_KIND_VALUES],
            "garden": [pd for pd in (_farm_plot_dict(p) for p in (self.garden or []))
                       if pd is not None],
            "grid_version": max(0, int(self.grid_version)),
            "last_banquet_day": max(0, int(self.last_banquet_day)),
            "grid_cell": str(self.grid_cell or ""),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Home":
        if not isinstance(d, dict):
            return cls()
        stash = d.get("stash") or []
        if not isinstance(stash, list):
            stash = []
        shelf = d.get("display_shelf") or []
        if not isinstance(shelf, list):
            shelf = []
        furn = d.get("furniture") or []
        if not isinstance(furn, list):
            furn = []
        bldgs = d.get("buildings") or []
        if not isinstance(bldgs, list):
            bldgs = []
        plots = d.get("garden") or []
        if not isinstance(plots, list):
            plots = []
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            location_id=d.get("location_id", "") or "",
            tier=max(1, min(3, _gi(d, "tier", 1))),
            purchased_day=max(1, _gi(d, "purchased_day", 1)),
            stash=[str(x) for x in stash if isinstance(x, str)],
            furniture=[dict(f) for f in furn
                       if isinstance(f, dict) and f.get("kind") in _FURNITURE_KIND_VALUES],
            display_shelf=[str(x) for x in shelf if isinstance(x, str)],
            buildings=[dict(b) for b in bldgs
                       if isinstance(b, dict) and b.get("kind") in _BUILDING_KIND_VALUES],
            garden=[pd for pd in (_farm_plot_dict(p) for p in plots) if pd is not None],
            grid_version=_gi(d, "grid_version", 0),  # 老档缺省 0 -> ensure_home_grid 迁移
            grid_cell=str(d.get("grid_cell") or ""),
            last_banquet_day=max(0, _gi(d, "last_banquet_day", 0)),
        )


@dataclass
class PlayerDomain:
    """[D1 2026-08-29] 玩家据点（购下野外地点翻转 settlement，据点经营主体）。

    地点侧改造在 Location（player_owned/kind/settlement_size/场所）；本实体持经营状态：
    funds 资金池（存取款 + D5 客流收入/工资支出）、prosperity 繁荣度 0-100（D5 客流加成，
    D4 防务失败扣）、roster 职员/居民名册（D2 招募；list[dict] {npc_id, role, ...}）。
    居民上限由所在地点 settlement_size 决定（domain_engine.resident_limit：6/12/20）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""                 # 据点名（购地 LLM 起名，同写入 Location.name）
    desc: str = ""                 # 一句描写（非空时覆写 Location.desc）
    location_id: str = ""          # 据点地点 id
    funds: int = 0                 # 资金池（题材货币计价；存取款/经营收支走 domain_engine）
    prosperity: int = 0            # 繁荣度 0-100
    roster: list = field(default_factory=list)   # list[dict] 职员名册（D2 招募写入）
    # [P61 定策 2026-09-26] 经营策略（一键一策：""/output/military/quality；domain_engine.set_policy）。
    policy: str = ""
    # [D2 2026-08-29] 已建设施 list[dict] {place_id, kind, name, day}；kind 白名单
    # _DOMAIN_FACILITY_KIND_VALUES（shop 商铺/guard 岗哨/farm 农田，每类限一座；
    # domain_engine 建造校验同引用此常量，单一来源）。
    # shop 设施同时在 world.shops 建真 Shop（掌柜入职绑定，P36a 生活模拟自动上架）。
    facilities: list = field(default_factory=list)
    # [D2] 连续欠薪天数（日结算发不出工资 +1，付清清零；>=3 全员离职）。
    unpaid_days: int = 0
    # [D5 2026-08-29] 经营统计：累计访客数 / 累计营业收入（账单 UI 展示；日结算累加）。
    visitors_total: int = 0
    income_total: int = 0
    # [D5] 最近一次日结算账单 {day, flow, income, wages, maintenance, net}（账单 UI 单源）。
    last_bill: dict = field(default_factory=dict)
    # [D6 2026-08-29] 据点委托板 list[dict]：{id, item_id, item_name, qty, reward,
    # posted_day, expire_day, deliver_day, taker_npc_id, state}；state 白名单
    # open 待接 / taken 承接 / done 已交付 / expired 过期退款。发布即押金出资金池，
    # 交付货款给承接 NPC、货进据点商铺货架；过期退回资金池。
    commissions: list = field(default_factory=list)
    purchased_day: int = 1         # 购地日（world.day_count 快照）
    # [据点仓库 2026-09-07 用户指示] 仓库 item_id 列表（同住宅 stash 口径；容量随聚落档位
    # domain_engine.warehouse_cap：village 30/town 60/city 90）。伙计日产 5 件白绿材料、
    # 农夫日产 5 件白绿作物入仓；掌柜每日从仓库取料合成（无建筑门控配方，蓝封顶，日 3 件）
    # 成品上架据点商铺货架售卖；玩家经 DomainDialog 存取（出仓走 try_add_to_inventory）。
    warehouse: list = field(default_factory=list)
    # [P48 据点产线 2026-09-25 用户指示] 生产订单 list[dict]：{id, recipe_id, out_id, qty,
    # made, ship("shelf"|"warehouse"|"home"), state("open"|"done"|"stalled"), created_day, stall_days}。
    # 有 open 单时伙计/农夫定向采订单缺料（不再纯随机）、掌柜按单合成（不再扫配方池）；
    # 玩家在 DomainDialog 发布/撤单——「看着世界的需求安排自家生产」（与 P39b 委托板互指）。
    orders: list = field(default_factory=list)
    # [P48] 近 7 日账单快照（bill UI 走势；daily_settle 尾部滚动写入）。
    bill_history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc,
            "location_id": self.location_id,
            "funds": max(0, int(self.funds)),
            "prosperity": max(0, min(100, int(self.prosperity))),
            "roster": [dict(r) for r in (self.roster or []) if isinstance(r, dict)],
            "policy": str(self.policy or ""),
            "facilities": [dict(f) for f in (self.facilities or []) if isinstance(f, dict)
                           and f.get("kind") in _DOMAIN_FACILITY_KIND_VALUES],
            "unpaid_days": max(0, int(self.unpaid_days)),
            "visitors_total": max(0, int(self.visitors_total)),
            "income_total": max(0, int(self.income_total)),
            "last_bill": dict(self.last_bill) if isinstance(self.last_bill, dict) else {},
            # [D6] 存档裁剪优先保活在途（open/taken 各至多 3+3），finished 只留最近 6
            #（[审查修复] 原样 [:12] 留最旧——14 条可达时会把最新 open 单连同押金裁掉）
            "commissions": _trim_domain_commissions(self.commissions),
            "purchased_day": max(1, int(self.purchased_day)),
            "warehouse": [str(x) for x in (self.warehouse or []) if isinstance(x, str)],
            "orders": [dict(o) for o in (self.orders or []) if isinstance(o, dict)],
            "bill_history": [dict(b) for b in (self.bill_history or []) if isinstance(b, dict)],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerDomain":
        if not isinstance(d, dict):
            return cls()
        roster = d.get("roster") or []
        if not isinstance(roster, list):
            roster = []
        facs = d.get("facilities") or []
        if not isinstance(facs, list):
            facs = []
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            location_id=d.get("location_id", "") or "",
            funds=max(0, _gi(d, "funds", 0)),
            prosperity=max(0, min(100, _gi(d, "prosperity", 0))),
            roster=[dict(r) for r in roster if isinstance(r, dict)],
            facilities=[dict(f) for f in facs
                        if isinstance(f, dict) and f.get("kind") in _DOMAIN_FACILITY_KIND_VALUES],
            unpaid_days=max(0, _gi(d, "unpaid_days", 0)),
            visitors_total=max(0, _gi(d, "visitors_total", 0)),
            income_total=max(0, _gi(d, "income_total", 0)),
            last_bill=dict(d.get("last_bill") or {}) if isinstance(d.get("last_bill"), dict) else {},
            commissions=_trim_domain_commissions(d.get("commissions")),
            purchased_day=max(1, _gi(d, "purchased_day", 1)),
            warehouse=[str(x) for x in (d.get("warehouse") or [])
                       if isinstance(x, str)],
            orders=[dict(o) for o in (d.get("orders") or []) if isinstance(o, dict)],
            bill_history=[dict(b) for b in (d.get("bill_history") or [])
                          if isinstance(b, dict)],
        )


@dataclass
class Pet:
    """[P24d] 宠物（常驻世界实体，区别于战斗临时随从单位）。

    获取两渠道：奇遇 tame 原型驯服 / 野外战斗胜利低概率遇幼崽（pet_engine.tame_check）。
    species 是 pet_engine._GENRE_PET_SPECIES 的种族 id（数值成长模板挂种族上，不随档存）；
    出战 = PlayerState.active_pet_id 指向本宠物（战斗自动入 ally 单位，不占 companion 位）。
    affinity 阈值解锁种族被动（采集加成/预警/寻宝，见 pet_engine.passive_active）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""        # 宠物名（默认种族中文名）
    species: str = ""     # 种族 id（种族池键，非自由文本）
    tier: int = 1         # 种族档位 1-3（白名单钳制）
    level: int = 1        # 1-10（出战获 xp 升级，成长走种族模板）
    xp: int = 0
    affinity: int = 0     # 亲和 0-100（投食提升；>=50 解锁种族被动）
    # ---- [P34b] 资质系统（资质挂钩属性成长系数，资质丹洗练）----
    # aptitude: {atk/def/hp/mp/spd: 资质值 0.8-1.5}，build_pet_unit 据此缩放对应属性。
    # 驯服时 default_aptitude() 全 1.0 基线；资质丹重 roll（refine_engine.refine_pet）。
    # identified: 鉴定态。False=资质/种族被动隐藏，鉴定卷轴揭示（refine_engine.appraise_pet）。
    # 默认 True 守现有宠物（老档/驯服即鉴定）；新驯服可设 False 进未鉴定态（v1 驯服即鉴定保简单）。
    aptitude: dict = field(default_factory=lambda: {"atk": 1.0, "def": 1.0, "hp": 1.0, "mp": 1.0, "spd": 1.0})
    identified: bool = True
    # ---- 宠物技能（存技能名，定义运行时按名从题材技能池解析，守 §23 不新建贫血池）----
    # 槽位 = pet_engine.pet_skill_slots(level)（1 + Lv4 + Lv8，钳 3）；候选挂种族
    # skill_candidates；兽诀卷重 roll 单槽（refine_engine.reroll_pet_skill）。
    skills: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "species": self.species,
            "tier": max(1, min(3, int(self.tier))),
            "level": max(1, min(10, int(self.level))),
            "xp": max(0, int(self.xp)),
            "affinity": max(0, min(100, int(self.affinity))),
            "aptitude": {k: round(float(v), 2) for k, v in (self.aptitude or {}).items()
                         if k in ("atk", "def", "hp", "mp", "spd")},
            "identified": bool(self.identified),
            "skills": [str(s) for s in (self.skills or []) if isinstance(s, str) and s],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Pet":
        if not isinstance(d, dict):
            return cls()
        raw_apt = d.get("aptitude") or {}
        apt = {}
        if isinstance(raw_apt, dict):
            for k in ("atk", "def", "hp", "mp", "spd"):
                try:
                    apt[k] = round(float(raw_apt.get(k, 1.0) or 1.0), 2)
                except (TypeError, ValueError):
                    apt[k] = 1.0
        else:
            apt = {"atk": 1.0, "def": 1.0, "hp": 1.0, "mp": 1.0, "spd": 1.0}
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            species=d.get("species", "") or "",
            tier=max(1, min(3, _gi(d, "tier", 1))),
            level=max(1, min(10, _gi(d, "level", 1))),
            xp=max(0, _gi(d, "xp", 0)),
            affinity=max(0, min(100, _gi(d, "affinity", 0))),
            aptitude=apt,
            identified=bool(d.get("identified", True)),
            skills=[str(s) for s in (d.get("skills") or []) if isinstance(s, str) and s],
        )


# ---- [P25a] 秘境房间类型白名单（引擎只认抽象 key，题材差异在文案池）----
_DUNGEON_ROOM_VALUES = ("combat", "check", "treasure", "trap", "rest", "boss")
_DUNGEON_STATUS_VALUES = ("open", "sealed")


@dataclass
class DungeonRoom:
    """[P25a] 秘境房间（推进单位）：type 决定结算分支（dungeon_engine.advance）。

    done = 已结算（战斗房 = 怪被击败；陷阱/检定/宝藏/休息 = 效果已发）。
    pending = 战斗房怪物已拉起尚未击败（重进续探时据此重建同一只怪，确定性种子）。
    id/adjacent：[D01/R2 2026-09-30] 层内稳定房间 id（f"f{层}r{序}"，engine 幂等补）+
    秘境邻接房间 id（主路 + 已开捷径 + 层间楼梯）。
    """
    type: str = "combat"       # 白名单 _DUNGEON_ROOM_VALUES
    name: str = ""
    desc: str = ""
    done: bool = False
    pending: bool = False
    # [B01-F06] 满包滞留的房间奖励（宝藏/机关/暗格/Boss 掉落的 item id）：
    # 非空时房间不算取尽（treasure/check 不置 done；Boss 房 done 但滞留可补领），
    # advance 优先补发（不重发金币/经验），取空清列表。
    pending_items: list = field(default_factory=list)
    id: str = ""                # 稳定 id（空 = 未初始化，engine ensure_room_graph 补）
    adjacent: list = field(default_factory=list)   # 秘境邻接房间 id（含层间楼梯）
    # [D02/R2 2026-09-30] 机关房两段式第一步：已「观察」获线索（未结算——破解是第二步；
    # 其余房型不走此态）。同一次互动结果固定保存，重开窗口不重 roll。
    seen: bool = False
    # D02: 一次性互动结果与机关控制的同层捷径，均随房间落盘。
    check_result: str = ""       # success/failure/bypassed
    hidden_result: str = ""      # found/missed/disabled（空 = 尚未检定）
    interaction_tool_id: str = ""
    shortcut: list = field(default_factory=list)  # 两端房间 id；不是默认可通行边
    shortcut_open: bool = False
    discovered: bool = False    # 抵达/邻接揭示；与机关的 seen 分开
    visited: bool = False       # 真正抵达过；守卫前可沿到过的房间退回
    gate_requires: list = field(default_factory=list)  # 处理这些房间后才可进入（不消耗物品）

    def to_dict(self) -> dict:
        return {
            "type": self.type if self.type in _DUNGEON_ROOM_VALUES else "combat",
            "name": self.name, "desc": self.desc,
            "done": bool(self.done), "pending": bool(self.pending),
            "pending_items": [str(x) for x in (self.pending_items or [])],
            "id": str(self.id or ""),
            "adjacent": [str(x) for x in (self.adjacent or []) if str(x)],
            "seen": bool(self.seen),
            "check_result": self.check_result,
            "hidden_result": self.hidden_result,
            "interaction_tool_id": str(self.interaction_tool_id or ""),
            "shortcut": [str(x) for x in (self.shortcut or [])],
            "shortcut_open": bool(self.shortcut_open),
            "discovered": bool(self.discovered),
            "visited": bool(self.visited),
            "gate_requires": list(self.gate_requires),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DungeonRoom":
        if not isinstance(d, dict):
            return cls()
        return cls(
            type=str(d.get("type", "combat")) if d.get("type") in _DUNGEON_ROOM_VALUES else "combat",
            name=d.get("name", "") or "",
            desc=d.get("desc", "") or "",
            done=bool(d.get("done", False)),
            pending=bool(d.get("pending", False)),
            pending_items=[str(x) for x in (d.get("pending_items") or []) if isinstance(x, str)],
            id=str(d.get("id", "") or ""),
            adjacent=[str(x) for x in (d.get("adjacent") or []) if isinstance(x, str)],
            seen=bool(d.get("seen", False)),
            check_result=d.get("check_result", "") if d.get("check_result") in ("success", "failure", "bypassed") else "",
            hidden_result=d.get("hidden_result", "") if d.get("hidden_result") in ("found", "missed", "disabled") else "",
            interaction_tool_id=str(d.get("interaction_tool_id", "") or ""),
            shortcut=[x for x in d.get("shortcut", []) if isinstance(x, str)][:2] if isinstance(d.get("shortcut"), list) else [],
            shortcut_open=bool(d.get("shortcut_open", False)),
            discovered=bool(d.get("discovered", True)),
            visited=bool(d.get("visited", False)),
            gate_requires=[x for x in d.get("gate_requires", []) if isinstance(x, str) and x] if isinstance(d.get("gate_requires"), list) else [],
        )


@dataclass
class DungeonFloor:
    """cleared 仅表示全探索；楼梯读取出口房结果，不依赖所有支路 done。"""
    index: int = 1
    rooms: list[DungeonRoom] = field(default_factory=list)
    cleared: bool = False
    layout: str = "loop"        # loop / branch / locked
    entry_room_id: str = ""
    exit_room_id: str = ""

    def to_dict(self) -> dict:
        return {
            "index": max(1, int(self.index)),
            "rooms": [r.to_dict() for r in self.rooms if isinstance(r, DungeonRoom)],
            "cleared": bool(self.cleared),
            "layout": self.layout,
            "entry_room_id": self.entry_room_id,
            "exit_room_id": self.exit_room_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DungeonFloor":
        if not isinstance(d, dict):
            return cls()
        return cls(
            index=max(1, _gi(d, "index", 1)),
            rooms=[DungeonRoom.from_dict(r) for r in (d.get("rooms") or []) if isinstance(r, dict)],
            cleared=bool(d.get("cleared", False)),
            layout=d.get("layout", "loop") if d.get("layout") in ("loop", "branch", "locked") else "loop",
            entry_room_id=str(d.get("entry_room_id", "") or ""),
            exit_room_id=str(d.get("exit_room_id", "") or ""),
        )


@dataclass
class Dungeon:
    """[P25a] 秘境（挂真实入口地点，每地点限一个；内部 = 单个 kind="dungeon" Location）。

    探索复用场景循环：内部地点 id = interior_id（f"dgin_{id}"）；「深入」= apply_intent
    move 特判 -> dungeon_engine.advance 逐房推进。中途退出保留房间状态，重进续探。
    通关 = boss 房击败 -> status="sealed" + cooldown_until_day；tick 到期同 seed 重铺
    （clears 提升怪物难度）。秘境怪物为临时单位不入 world.npcs（图鉴不漂移，
    通关记录走 dungeon.clears + 图鉴「秘境讨伐」段）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    theme: str = ""            # 题材池主题 id
    location_id: str = ""      # 入口所在真实地点 id
    danger: int = 1            # 基础危险度（怪物等级 = danger + 层数 + clears 缩放）
    floors: list[DungeonFloor] = field(default_factory=list)
    status: str = "open"       # open（可探索）/ sealed（通关封印冷却）
    cooldown_until_day: int = 0
    clears: int = 0
    # [R0/B01-F07 2026-09-30] 开放周期标识：regenerate 时 +1。秘境怪 dungeon_tag 记
    # 出生周期，旧周期怪延迟收尾（新周期已重铺）一律拒绝，防旧回调结算新周期房间。
    run_id: int = 1
    # 玩家在秘境内的真实房间 ID；空 = 尚未入内，进入从第一层入口落位。
    # move_room 维护；current_room 和互动读取同一站位，不跳到首个未 done 房。
    current_room_id: str = ""
    # [D02/R2 2026-09-30] 机关破解累计解除的首领威慑级数（每级 Boss 血量 -15%，钳 0-2；
    # regenerate 新周期重置）。机关成功 -> 削 Boss 是本层真实战斗状态，预告与结算一致。
    boss_relief: int = 0
    # [D03/R2 2026-09-30] 发现状态：区分「入口已存在」vs「玩家已发现」。未发现不进
    # scene_block/弹窗/议程；到访入口地点/接受 hook/传闻触发发现。
    discovered: bool = True     # [D03] 默认 True=直构/旧档兼容；build_dungeon 显式置 False
    discovery_day: int = 0
    discovery_source: str = ""    # worldgen|visit|hook|ruins|rumor

    @property
    def interior_id(self) -> str:
        return f"dgin_{self.id}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "theme": self.theme,
            "location_id": self.location_id,
            "danger": max(1, min(10, int(self.danger))),
            "floors": [f.to_dict() for f in self.floors if isinstance(f, DungeonFloor)],
            "status": self.status if self.status in _DUNGEON_STATUS_VALUES else "open",
            "cooldown_until_day": max(0, int(self.cooldown_until_day)),
            "clears": max(0, int(self.clears)),
            "run_id": max(1, int(self.run_id or 1)),
            "current_room_id": str(self.current_room_id or ""),
            "boss_relief": max(0, min(2, int(self.boss_relief or 0))),
            "discovered": bool(self.discovered),
            "discovery_day": max(0, int(self.discovery_day)),
            "discovery_source": str(self.discovery_source)[:16],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Dungeon":
        if not isinstance(d, dict):
            return cls()
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            theme=d.get("theme", "") or "",
            location_id=d.get("location_id", "") or "",
            danger=max(1, min(10, _gi(d, "danger", 1))),
            floors=[DungeonFloor.from_dict(f) for f in (d.get("floors") or []) if isinstance(f, dict)],
            status=str(d.get("status", "open")) if d.get("status") in _DUNGEON_STATUS_VALUES else "open",
            cooldown_until_day=max(0, _gi(d, "cooldown_until_day", 0)),
            clears=max(0, _gi(d, "clears", 0)),
            run_id=max(1, _gi(d, "run_id", 1)),
            current_room_id=str(d.get("current_room_id", "") or ""),
            boss_relief=max(0, min(2, _gi(d, "boss_relief", 0))),
            discovered=bool(d.get("discovered", True)),
            discovery_day=max(0, _gi(d, "discovery_day", 0)),
            discovery_source=str(d.get("discovery_source", "") or "")[:16],
        )


# ---- [P25d] 世界 Boss 形态 AI 白名单 ----
_WORLD_BOSS_AI_VALUES = ("aggressive", "defensive", "caster", "balanced")


@dataclass
class BossForm:
    """[P25d] 世界 Boss 的一个形态（每个形态是一场独立战斗）。

    形态递进：current_form_index 推进到下一形态（名/技能/AI/血量倍率变化）。
    非终形态击杀 -> index+1 + 变身 narration_hint；终形态击杀 -> defeated + 奖励。
    """
    index: int = 0
    name: str = ""             # 形态名（题材化，如「幼体/成体/究极体」）
    ai: str = "aggressive"     # aggressive/defensive/caster/balanced
    hp_mult: float = 1.0       # 形态血量倍率（按 index 递增 1.0/1.4/1.8）
    skill_name: str = ""       # 形态主技能名（题材化）
    skill_power: int = 10      # 技能威力（+ boss.level）
    minion_count: int = 0      # 随从数（按形态递减，终形态常为 0）

    def to_dict(self) -> dict:
        return {
            "index": max(0, int(self.index)),
            "name": self.name or "",
            "ai": self.ai if self.ai in _WORLD_BOSS_AI_VALUES else "aggressive",
            "hp_mult": max(0.1, float(self.hp_mult)),
            "skill_name": self.skill_name or "",
            "skill_power": max(0, int(self.skill_power)),
            "minion_count": max(0, min(5, int(self.minion_count))),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BossForm":
        if not isinstance(d, dict):
            return cls()
        return cls(
            index=max(0, _gi(d, "index", 0)),
            name=d.get("name", "") or "",
            ai=str(d.get("ai", "aggressive")) if d.get("ai") in _WORLD_BOSS_AI_VALUES else "aggressive",
            hp_mult=max(0.1, float(d.get("hp_mult", 1.0) or 1.0)),
            skill_name=d.get("skill_name", "") or "",
            skill_power=max(0, _gi(d, "skill_power", 10)),
            minion_count=max(0, min(5, _gi(d, "minion_count", 0))),
        )


@dataclass
class WorldBoss:
    """[P25d] 世界 Boss（挂危险地点，开放窗口 N 天，多形态递进讨伐）。

    来源：tick crisis/major 事件关联地点 danger >= 阈值时生成（镜像危机任务门控，
    同时只一未击败在窗 Boss）。讨伐走按钮 -> CombatDialog（skip_judge 预设战斗）；
    形态间玩家回到场景可休整再战（current_form_index 随档落盘续战）。窗口到期
    未击败 -> 离去留传闻（不强行终结，守无限世界）。击杀 = 声望变动 + major/crisis
    event + 保底 legendary 掉落（无成就钩子，守上次定稿）。纯引擎确定性生成无 LLM。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""             # Boss 名（题材池概念）
    concept_id: str = ""       # 题材概念 id
    desc: str = ""
    location_id: str = ""      # 挂的危险地点 id（crisis 事件地点）
    danger: int = 1            # 1-10（地点 danger 快照）
    level: int = 1             # = max(danger, player.level+3) @ 生成时
    forms: list[BossForm] = field(default_factory=list)
    # [Boss 机制库 2026-08-29] 机制白名单 shield 护盾（承伤 x0.3 至破碎）/
    # summon_tide 召唤潮（每 3 回合补 1 爪牙 cap2）/ enrage_timer 狂暴计时（第 5
    # 回合 atk x1.5）；spawn 时按 world+day+location.id 确定性挑 1-2 个，战斗内 transient 不落盘。
    mechanics: list = field(default_factory=list)
    current_form_index: int = 0
    defeated: bool = False
    spawned_day: int = 1       # day_count 快照
    window_until_day: int = 0  # spawned_day + window_days
    faction_id: str = ""       # Boss 关联势力（地点 owner，击杀声望结算）
    reinforcement_npc_id: str = ""       # 声望>=50 势力派的一次性援护 NPC（首战锁定）
    reinforcement_faction_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "concept_id": self.concept_id,
            "desc": self.desc, "location_id": self.location_id,
            "danger": max(1, min(10, int(self.danger))),
            "level": max(1, int(self.level)),
            "forms": [f.to_dict() for f in self.forms if isinstance(f, BossForm)],
            "mechanics": [str(x) for x in (self.mechanics or [])
                          if x in ("shield", "summon_tide", "enrage_timer")],
            "current_form_index": max(0, min(max(0, len(self.forms) - 1), int(self.current_form_index))),
            "defeated": bool(self.defeated),
            "spawned_day": max(1, int(self.spawned_day)),
            "window_until_day": max(0, int(self.window_until_day)),
            "faction_id": self.faction_id or "",
            "reinforcement_npc_id": self.reinforcement_npc_id or "",
            "reinforcement_faction_id": self.reinforcement_faction_id or "",
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorldBoss":
        if not isinstance(d, dict):
            return cls()
        forms = [BossForm.from_dict(f) for f in (d.get("forms") or []) if isinstance(f, dict)]
        cfi = max(0, _gi(d, "current_form_index", 0))
        if forms:
            cfi = min(cfi, len(forms) - 1)
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            concept_id=d.get("concept_id", "") or "",
            desc=d.get("desc", "") or "",
            location_id=d.get("location_id", "") or "",
            danger=max(1, min(10, _gi(d, "danger", 1))),
            level=max(1, _gi(d, "level", 1)),
            forms=forms,
            mechanics=[str(x) for x in (d.get("mechanics") or [])
                       if x in ("shield", "summon_tide", "enrage_timer")],
            current_form_index=cfi,
            defeated=bool(d.get("defeated", False)),
            spawned_day=max(1, _gi(d, "spawned_day", 1)),
            window_until_day=max(0, _gi(d, "window_until_day", 0)),
            faction_id=d.get("faction_id", "") or "",
            reinforcement_npc_id=d.get("reinforcement_npc_id", "") or "",
            reinforcement_faction_id=d.get("reinforcement_faction_id", "") or "",
        )


@dataclass
class Commodity:
    """[P34f] 大宗商品（股市交易标的）。symbol 抽象 key（大写），name 题材化显示名。

    price=当前价 / prev_price=上一日价（涨跌%用）/ base_price=基准价（兜底锚定防随机游走）/
    history=价格历史（cap 30，UI mini 折线用）。
    """
    symbol: str = ""        # 抽象 key（如 IRON/LINGSHI），白名单无（题材池决定）
    name: str = ""          # 题材化显示名（如 精铁锭/灵石）
    desc: str = ""
    cat: str = "civil"      # [P41] 商品类别 war(军需)/civil(民生)/lux(奢侈)——势力局势驱动定价用
    price: int = 0          # 当前价（金币）
    prev_price: int = 0     # 上一日价（涨跌%计算用）
    base_price: int = 0     # 基准价（兜底锚定 + ±% 钳制参考）
    history: list = field(default_factory=list)   # list[int] 价格历史（cap 30）

    def to_dict(self) -> dict:
        return {
            "symbol": str(self.symbol or ""), "name": str(self.name or ""),
            "desc": str(self.desc or ""),
            "cat": self.cat if self.cat in ("war", "civil", "lux") else "civil",
            "price": max(0, int(self.price)),
            "prev_price": max(0, int(self.prev_price)),
            "base_price": max(0, int(self.base_price)),
            "history": [int(h) for h in (self.history or []) if isinstance(h, (int, float))],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Commodity":
        if not isinstance(d, dict):
            return cls()
        _cat = str(d.get("cat", "") or "")
        return cls(
            symbol=str(d.get("symbol", "") or ""),
            name=str(d.get("name", "") or ""),
            desc=str(d.get("desc", "") or ""),
            cat=_cat if _cat in ("war", "civil", "lux") else "civil",
            price=max(0, _gi(d, "price", 0)),
            prev_price=max(0, _gi(d, "prev_price", 0)),
            base_price=max(0, _gi(d, "base_price", 0)),
            history=[int(h) for h in (d.get("history") or [])
                     if isinstance(h, (int, float))],
        )


@dataclass
class StockMarket:
    """[P34f] 交易所股市（挂城市 auction shop，每日 LLM 定价题材化大宗商品）。

    commodities=商品列表（init_stock_market 据题材池初始化）；每日 tick_stock_market
    推进 price/prev_price/history。玩家持仓在 PlayerState.stock_holdings。
    """
    commodities: list = field(default_factory=list)   # list[Commodity]

    def to_dict(self) -> dict:
        return {
            "commodities": [c.to_dict() for c in self.commodities
                            if isinstance(c, Commodity)],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StockMarket":
        if not isinstance(d, dict):
            return cls()
        return cls(
            commodities=[Commodity.from_dict(c) for c in (d.get("commodities") or [])
                         if isinstance(c, dict)],
        )

    def find(self, symbol: str) -> "Commodity | None":
        """按 symbol 查商品（无则 None）。"""
        s = str(symbol or "")
        for c in self.commodities:
            if isinstance(c, Commodity) and c.symbol == s:
                return c
        return None


# [P34g] 拍卖会状态白名单（upcoming=预告/active=进行中/ended=已结算）。
_AUCTION_STATUS_VALUES = ("upcoming", "active", "ended")
# [NPC 竞拍 2026-09-11] 出价者类别白名单（player=玩家 / npc=NPC）。老档缺字段回落 player。
_AUCTION_BIDDER_KINDS = ("player", "npc")


@dataclass
class AuctionLot:
    """[P34g] 拍卖会拍品（item_id 指向 world.items 里 identified=False 的临时生成物）。

    start_price=起拍价（compute_item_price 公式）/ current_bid=当前最高出价（押金已扣）/
    bid_step=竞价最小加价档 / min_level=建议玩家等级 / sold=是否拍出。
    current_bidder=当前最高出价者 id（玩家为 p.id 或 "player"，NPC 为 npc.id；空=无人出价，
    current_bid=start_price 初始）/ bidder_kind=出价者类别（[NPC 竞拍 2026-09-11] 决定押金
    退还与落槌交割走玩家 gold 还是 NPC wallet）。
    """
    item_id: str = ""
    start_price: int = 0
    current_bid: int = 0
    bid_step: int = 0
    min_level: int = 0
    sold: bool = False
    current_bidder: str = ""      # 出价者 id（空=无人出价）
    bidder_kind: str = "player"   # player / npc

    def to_dict(self) -> dict:
        return {
            "item_id": str(self.item_id or ""),
            "start_price": max(0, int(self.start_price)),
            "current_bid": max(0, int(self.current_bid)),
            "bid_step": max(1, int(self.bid_step)),
            "min_level": max(0, int(self.min_level)),
            "sold": bool(self.sold),
            "current_bidder": str(self.current_bidder or ""),
            "bidder_kind": (self.bidder_kind if self.bidder_kind in _AUCTION_BIDDER_KINDS
                            else "player"),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuctionLot":
        if not isinstance(d, dict):
            return cls()
        kind = str(d.get("bidder_kind", "") or "")
        return cls(
            item_id=str(d.get("item_id", "") or ""),
            start_price=max(0, _gi(d, "start_price", 0)),
            current_bid=max(0, _gi(d, "current_bid", 0)),
            bid_step=max(1, _gi(d, "bid_step", 1)),
            min_level=max(0, _gi(d, "min_level", 0)),
            sold=bool(d.get("sold", False)),
            current_bidder=str(d.get("current_bidder", "") or ""),
            bidder_kind=(kind if kind in _AUCTION_BIDDER_KINDS else "player"),
        )


_COMMISSION_KIND_VALUES = ("restock", "urgent")  # [定版裁剪 2026-09-05] festival 随节日系统移除


@dataclass
class CommissionOrder:
    """[P39b] 委托订单（挂聚落的 NPC 收购单：玩家生产 -> 交付 -> 赚钱/交情/声望）。

    item_name 是交付匹配权威（同 name 的克隆件——锻造/鉴定/洗练产物——均可交付；
    引擎不按 item_id 死匹配）。kind 白名单：restock 商人补货 / urgent 生活急缺。epic+ 订单交付须 identified=True（赌徒经济闭环：未鉴定赌货不收）。
    expire_day 按 day_count 过期（防囤积）。unit_price = compute_item_price x 品类系数
    （restock 1.2 / urgent 1.5，[P39 定稿] 让生产有利可图）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    issuer_npc_id: str = ""     # 发布 NPC（交付须当面：玩家在挂靠聚落）
    location_id: str = ""       # 挂靠聚落
    item_id: str = ""           # 模板物品 id（展示/定价参考）
    item_name: str = ""         # 交付匹配权威（同 name 克隆件可交付）
    qty: int = 1
    unit_price: int = 0
    kind: str = "restock"
    reason: str = ""            # 风味描述（题材文案池，零 LLM）
    created_day: int = 0
    expire_day: int = 0

    def to_dict(self) -> dict:
        return {
            "id": str(self.id or ""), "issuer_npc_id": str(self.issuer_npc_id or ""),
            "location_id": str(self.location_id or ""), "item_id": str(self.item_id or ""),
            "item_name": str(self.item_name or ""), "qty": max(1, int(self.qty or 1)),
            "unit_price": max(0, int(self.unit_price or 0)),
            "kind": self.kind if self.kind in _COMMISSION_KIND_VALUES else "restock",
            "reason": str(self.reason or ""),
            "created_day": max(0, int(self.created_day or 0)),
            "expire_day": max(0, int(self.expire_day or 0)),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CommissionOrder":
        if not isinstance(d, dict):
            return cls()
        return cls(
            id=str(d.get("id", "") or "") or str(uuid.uuid4()),
            issuer_npc_id=str(d.get("issuer_npc_id", "") or ""),
            location_id=str(d.get("location_id", "") or ""),
            item_id=str(d.get("item_id", "") or ""),
            item_name=str(d.get("item_name", "") or ""),
            qty=max(1, _gi(d, "qty", 1)),
            unit_price=max(0, _gi(d, "unit_price", 0)),
            kind=str(d.get("kind", "") or "") if str(d.get("kind", "") or "")
            in _COMMISSION_KIND_VALUES else "restock",
            reason=str(d.get("reason", "") or ""),
            created_day=max(0, _gi(d, "created_day", 0)),
            expire_day=max(0, _gi(d, "expire_day", 0)),
        )


@dataclass
class AuctionEvent:
    """[P34g] 拍卖会事件（挂城市型聚落，tick 周期生成，限时竞价）。

    状态机：upcoming(预告,start_day 到 -> active) -> active(竞价,end_day 到 -> ended) -> ended(结算)。
    lots=拍品列表（start_auction 时生成 Item 入 world.items + 挂 AuctionLot）。
    city_location_id=举办城市 id（入口 gate：玩家在此城 + 有 active 拍卖会）。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    city_location_id: str = ""
    start_day: int = 1        # day_count 到此 -> active
    end_day: int = 1          # day_count 到此 -> ended
    status: str = "upcoming"  # 白名单 _AUCTION_STATUS_VALUES
    lots: list = field(default_factory=list)   # list[AuctionLot]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "city_location_id": str(self.city_location_id or ""),
            "start_day": max(1, int(self.start_day)),
            "end_day": max(1, int(self.end_day)),
            "status": self.status if self.status in _AUCTION_STATUS_VALUES else "upcoming",
            "lots": [lot.to_dict() for lot in self.lots if isinstance(lot, AuctionLot)],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuctionEvent":
        if not isinstance(d, dict):
            return cls()
        st = str(d.get("status", "upcoming") or "upcoming")
        if st not in _AUCTION_STATUS_VALUES:
            st = "upcoming"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            city_location_id=str(d.get("city_location_id", "") or ""),
            start_day=max(1, _gi(d, "start_day", 1)),
            end_day=max(1, _gi(d, "end_day", 1)),
            status=st,
            lots=[AuctionLot.from_dict(x) for x in (d.get("lots") or [])
                  if isinstance(x, dict)],
        )


_OUTREACH_KIND_VALUES = ("letter", "bounty", "duel", "plea")
_OUTREACH_STATE_VALUES = ("pending", "accepted", "declined", "expired", "done")


@dataclass
class NPCOutreach:
    """[P57 NPC 上门 2026-09-26] NPC 主动找上玩家的事（信件/买凶/约战/求助）。

    引擎生成 + 过期结算（零 LLM 结构，文案走题材模板池）；玩家经场景页「信使」
    处理（接受/拒绝/收下）。过期不候：pending 跨过 expires_day 自动结算后果。
    payload 按类型（白名单透传）：
      letter: {}                                    纯信（收下即交情+1）
      bounty: {target_npc_id, target_name, gold}    买凶（接受=生成 kill 任务，回去找发布人领赏）
      duel:   {}                                    约战（接受=上门切磋，纯荣誉无赌注）
      plea:   {item_id, item_name, qty} 或 {gold}   求助（缺药/缺钱；接受=守恒转移+交情）
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "letter"           # letter|bounty|duel|plea（白名单）
    npc_id: str = ""               # 上门者/寄信人
    title: str = ""
    text: str = ""
    state: str = "pending"         # pending|accepted|declined|expired|done（白名单）
    created_day: int = 1
    expires_day: int = 3           # 过期日（created_day + N）；「过期不候」
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind if self.kind in _OUTREACH_KIND_VALUES else "letter",
            "npc_id": str(self.npc_id or ""),
            "title": str(self.title or ""),
            "text": str(self.text or ""),
            "state": self.state if self.state in _OUTREACH_STATE_VALUES else "pending",
            "created_day": max(1, int(self.created_day)),
            "expires_day": max(1, int(self.expires_day)),
            "payload": dict(self.payload or {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NPCOutreach":
        if not isinstance(d, dict):
            return cls()
        kind = str(d.get("kind", "letter") or "letter")
        if kind not in _OUTREACH_KIND_VALUES:
            kind = "letter"
        state = str(d.get("state", "pending") or "pending")
        if state not in _OUTREACH_STATE_VALUES:
            state = "pending"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            kind=kind,
            npc_id=str(d.get("npc_id", "") or ""),
            title=str(d.get("title", "") or ""),
            text=str(d.get("text", "") or ""),
            state=state,
            created_day=max(1, _gi(d, "created_day", 1)),
            expires_day=max(1, _gi(d, "expires_day", 3)),
            payload=dict(d.get("payload") or {}) if isinstance(d.get("payload"), dict) else {},
        )


@dataclass
class PlayerState:
    """玩家状态（P3 加 CRPG 数值：等级/经验/HP/属性/技能/装备/背包/金币）。"""
    location_id: str = ""         # 当前所在地点 id
    background: str = ""          # 玩家身世
    class_name: str = ""          # 职业
    # ---- P3 CRPG 数值（静态定义由 LLM/默认值给，动态计算由 combat_engine 纯 Python）----
    level: int = 1
    xp: int = 0
    xp_next: int = 100
    hp: int = 100
    hp_max: int = 100
    stat_str: int = 10            # 力（攻击/暴击伤害/招架率）[P7e]
    stat_dex: int = 10            # 敏（命中/暴击率/速度/闪避率）[P7e]
    stat_int: int = 10            # 智（法术攻击/治疗加成）[P7e] 真正进公式（不再占位）
    stat_vit: int = 10            # 耐（防御/HP 上限）
    stat_luk: int = 10            # 运（速度/掉落率）[P7e] 真正影响掉落（不再仅速度）
    stat_points: int = 0          # [P7d2] 可分配属性点（升级获得，玩家手动分配到 str/dex/int/vit/luk）
    skills: list = field(default_factory=list)               # [P7h] list[dict] 结构化技能（老 list[str] 自动迁移）
    mp: int = 0                                               # [P7h] 法力/灵力/内力（题材化显示名，战斗技能消耗）
    mp_max: int = 0
    skill_cooldowns: dict = field(default_factory=dict)      # [P7h] {skill_id -> 剩余冷却回合}
    # [技能页 2026-08-28 用户定稿] 装备技能栏（最多 3 槽，存技能 id/name；战斗只出已装备技能）。
    # 空 list = 未启用（老档兼容：战斗回退全技能显示）。
    equipped_skills: list = field(default_factory=list)
    equipped: dict = field(default_factory=dict)             # {slot_id -> item_id}，8 槽
    inventory: list[str] = field(default_factory=list)       # item_id 列表（含消耗品）
    gold: int = 0
    # [饱食度 2026-09-06 用户指示] 0-100（100=吃饱）；hunger_enabled=False 的世界恒 100 不参与。
    # 低于 30（饥饿线）玩家全属性 x0.5（combat_engine.hunger_stat_mult 单一出口）。
    hunger: int = 100
    # ---- P4 世界滴答状态 ----
    reputation: dict[str, int] = field(default_factory=dict)  # 对 faction_id 声望 -100~100
    # [P5c] 玩家立绘文件名（world_images_dir 下），世界生成时由 ComfyUI 出图。
    # 空串=无立绘（纸娃娃 UI 走占位）。
    avatar: str = ""
    # ---- [P7k6] 图鉴累计统计（探索驱动解锁，纯代码记录，零 LLM）----
    codex_npcs: list = field(default_factory=list)       # 已遇到 NPC id
    codex_defeated: list = field(default_factory=list)   # 已击败 NPC id
    codex_items: list = field(default_factory=list)      # 曾拥有物品 id
    # [修 2026-09-05 用户指示] 图鉴死亡信息：{npc_id: {name/day/tick/loc/cause}}（最后一次
    # 被玩家击败的记录；杂兵重生不清除——历史击败记录，当前存活状态图鉴页动态读 alive）。
    codex_deaths: dict = field(default_factory=dict)
    combat_wins: int = 0                                  # 累计战斗胜利次数
    gathers_done: int = 0                                 # 累计采集成功次数
    # ---- [P9] 天赋（开局随机锁死1 + 天赋点自选；effects 参与各种计算）----
    talents: list = field(default_factory=list)           # list[dict] 已选天赋（Talent.to_dict）
    talent_points: int = 10                               # 剩余天赋点（默认10；QQ码 1965699077 解锁60）
    # ---- [P10] 好友（交情达门槛的 NPC 可添加；好友可私聊/更积极互动）----
    friend_npc_ids: list[str] = field(default_factory=list)   # 已添加好友的 NPC id 列表
    # ---- [P10b] 同行（交情达门槛可邀请 NPC 跟随移动；点对话退队）----
    companion_npc_ids: list[str] = field(default_factory=list)  # 同行中 NPC id（玩家移动时跟随）
    # ---- [P24a] 住宅（World.homes 实体的 id 引用；购宅时 home_engine.buy_home 同步写入）----
    home_ids: list[str] = field(default_factory=list)
    # [P52 伤疤 2026-09-25] 战损留痕：[{part("arm"|"legs"|"head"|"torso"), until_day, name}]。
    # 战败/惨胜结算（combat_engine.injury_stat_mult 消费；痊愈按 day 水位清）。
    injuries: list = field(default_factory=list)
    # [P53 锻造修行 2026-09-25] 合成熟练经验（成功+1+difficulty//15、crit 再+2、失败+1；
    # 阈值 [0,6,18,40] 四阶；每级成功率 +2%。NPC 无此字段不吃线）。
    craft_exp: int = 0
    # [P56 毕生所愿 2026-09-25 三 PM 共识] 玩家的长期执念（镜像 NPC.ambition——
    # 「NPC 都有野心，玩家不能没有」）。{kind, target_value, tier, title, done_day}；
    # kind 白名单：wealth/mastery/slay/explore/fame（进度全由既有系统承接，零新结算）。
    life_goal: dict = field(default_factory=dict)
    # ---- [P24d] 宠物（World.pets 实体的 id 引用；驯服时 pet_engine.create_pet 同步写入）----
    pet_ids: list[str] = field(default_factory=list)
    # [P24d] 出战/跟随中的宠物 id（空 = 收起；设为某宠物 id 后战斗自动入 ally 单位，
    # 不占 companion 位；平时跟随由场景上下文【宠物】块呈现）。
    active_pet_id: str = ""
    # ---- [P10c] 出生地（永久死亡关闭时玩家被击败 -> 复活送回此处，HP=1）----
    # build_world_from_skeleton 据开局地点写入；from_dict 缺键回退空串，复活时回退 world.locations[0]。
    spawn_location_id: str = ""
    # ---- [P26a] 关系阶段机 {npc_id -> {stage, since_day}}（friend/sworn/sweetheart/spouse）----
    # stage 白名单 _RELATION_STAGE_VALUES；引擎管阈值与状态机，LLM 管仪式叙事。
    relations: dict[str, dict] = field(default_factory=dict)
    # ---- [P27] 二层地图：玩家当前场所（空=回退所在 location 的 default_place_id）----
    place_id: str = ""
    # ---- [P34f] 交易所股市持仓 ----
    stock_holdings: dict[str, int] = field(default_factory=dict)   # {symbol -> 持有股数}
    stock_realized_gold: int = 0                                   # 累计卖出所得金币（收入口径，非盈亏差值；UI「累计落袋」同口径）

    def to_dict(self) -> dict:
        return {
            "location_id": self.location_id,
            "background": self.background,
            "class_name": self.class_name,
            "level": self.level,
            "xp": self.xp,
            "xp_next": self.xp_next,
            "hp": self.hp,
            "hp_max": self.hp_max,
            "stat_str": self.stat_str,
            "stat_dex": self.stat_dex,
            "stat_int": self.stat_int,
            "stat_vit": self.stat_vit,
            "stat_luk": self.stat_luk,
            "stat_points": max(0, int(self.stat_points)),
            "skills": [dict(s) for s in self.skills if isinstance(s, dict)],
            "mp": self.mp,
            "mp_max": self.mp_max,
            "skill_cooldowns": {str(k): int(v) for k, v in self.skill_cooldowns.items()},
            "equipped_skills": [str(s) for s in (self.equipped_skills or []) if str(s).strip()][:3],
            "equipped": dict(self.equipped),
            "inventory": list(self.inventory),
            "gold": self.gold,
            "hunger": max(0, min(100, int(self.hunger or 0))),
            "reputation": {str(k): int(v) for k, v in self.reputation.items()},
            "avatar": self.avatar,
            "codex_npcs": list(self.codex_npcs),
            "codex_defeated": list(self.codex_defeated),
            "codex_items": list(self.codex_items),
            "codex_deaths": {str(k): dict(v) for k, v in (self.codex_deaths or {}).items()
                             if isinstance(v, dict)},
            "combat_wins": max(0, int(self.combat_wins)),
            "gathers_done": max(0, int(self.gathers_done)),
            "talents": [Talent.from_dict(t).to_dict() for t in self.talents if isinstance(t, dict)],
            "talent_points": max(0, int(self.talent_points)),
            "friend_npc_ids": [str(x) for x in (self.friend_npc_ids or []) if isinstance(x, str)],
            "companion_npc_ids": [str(x) for x in (self.companion_npc_ids or []) if isinstance(x, str)],
            "home_ids": [str(x) for x in (self.home_ids or []) if isinstance(x, str)],
            "injuries": [dict(i) for i in (self.injuries or []) if isinstance(i, dict)],
            "craft_exp": max(0, int(self.craft_exp or 0)),
            "life_goal": dict(self.life_goal) if isinstance(self.life_goal, dict) else {},
            "pet_ids": [str(x) for x in (self.pet_ids or []) if isinstance(x, str)],
            "active_pet_id": self.active_pet_id,
            "spawn_location_id": self.spawn_location_id,
            "relations": {str(k): dict(v) for k, v in (self.relations or {}).items()
                          if isinstance(v, dict)},
            "place_id": self.place_id,
            # [P34f] 股市持仓
            "stock_holdings": {str(k): int(v) for k, v in (self.stock_holdings or {}).items()
                               if isinstance(v, (int, float))},
            "stock_realized_gold": int(self.stock_realized_gold),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerState":
        if not d or not isinstance(d, dict):
            return cls()

        def _i(key: str, default: int) -> int:
            """读 int 字段：缺失/None 用 default，合法 0 保留（防 `0 or default` 吞 0）。"""
            v = d.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        # [P5c] equipped dict 老存档迁移：weapon/armor/accessory -> 8 槽 key
        # 老世界 JSON 的 equipped 是 {"weapon": ..., "armor": ..., "accessory": ...}，
        # 加载时一次性迁移到 8 槽 key（main_hand/chest/accessory1），让引擎/UI 统一处理。
        raw_equipped = dict(d.get("equipped") or {})
        equipped = _migrate_equipped_to_8slots(raw_equipped)

        return cls(
            location_id=d.get("location_id", "") or "",
            background=d.get("background", "") or "",
            class_name=d.get("class_name", "") or "",
            level=max(1, _i("level", 1)),
            xp=max(0, _i("xp", 0)),
            xp_next=max(1, _i("xp_next", 100)),
            hp=max(0, _i("hp", 100)),
            hp_max=max(1, _i("hp_max", 100)),
            stat_str=_i("stat_str", 10),
            stat_dex=_i("stat_dex", 10),
            stat_int=_i("stat_int", 10),
            stat_vit=_i("stat_vit", 10),
            stat_luk=_i("stat_luk", 10),
            stat_points=max(0, _i("stat_points", 0)),
            skills=_migrate_skills(d.get("skills")),
            mp=max(0, _i("mp", 0)),
            mp_max=max(0, _i("mp_max", 0)),
            # [!] isinstance 防御：手改 JSON 传 list 时 .items() 抛 AttributeError
            equipped_skills=[str(s) for s in (d.get("equipped_skills") or [])
                             if str(s).strip()][:3],
            skill_cooldowns={str(k): max(0, int(v))
                             for k, v in ((d.get("skill_cooldowns")
                                           if isinstance(d.get("skill_cooldowns"), dict) else {}).items())
                             if isinstance(v, (int, float))},
            equipped=equipped,
            inventory=list(d.get("inventory") or []),
            gold=max(0, _i("gold", 0)),
            hunger=max(0, min(100, _i("hunger", 100))),
            reputation=_load_reputation(d.get("reputation")),
            avatar=str(d.get("avatar", "") or ""),
            codex_npcs=[str(x) for x in (d.get("codex_npcs") or []) if isinstance(x, str)],
            codex_defeated=[str(x) for x in (d.get("codex_defeated") or []) if isinstance(x, str)],
            codex_items=[str(x) for x in (d.get("codex_items") or []) if isinstance(x, str)],
            codex_deaths={str(k): dict(v) for k, v in (d.get("codex_deaths") or {}).items()
                          if isinstance(k, str) and isinstance(v, dict)},
            combat_wins=max(0, _i("combat_wins", 0)),
            gathers_done=max(0, _i("gathers_done", 0)),
            talents=[Talent.from_dict(t).to_dict() for t in (d.get("talents") or []) if isinstance(t, dict)],
            talent_points=max(0, _i("talent_points", 10)),
            friend_npc_ids=[str(x) for x in (d.get("friend_npc_ids") or []) if isinstance(x, str)],
            companion_npc_ids=[str(x) for x in (d.get("companion_npc_ids") or []) if isinstance(x, str)],
            home_ids=[str(x) for x in (d.get("home_ids") or []) if isinstance(x, str)],
            injuries=[dict(i) for i in (d.get("injuries") or []) if isinstance(i, dict)],
            craft_exp=max(0, int(d.get("craft_exp") or 0)),
            life_goal=dict(d.get("life_goal") or {}) if isinstance(d.get("life_goal"), dict) else {},
            pet_ids=[str(x) for x in (d.get("pet_ids") or []) if isinstance(x, str)],
            active_pet_id=str(d.get("active_pet_id", "") or ""),
            spawn_location_id=str(d.get("spawn_location_id", "") or ""),
            relations=_load_relations(d.get("relations")),
            place_id=str(d.get("place_id", "") or ""),
            # [P34f] 股市持仓（dict[str,int] isinstance 守卫 + 本地 _i 兜底）
            stock_holdings={str(k): int(v)
                            for k, v in ((d.get("stock_holdings")
                                          if isinstance(d.get("stock_holdings"), dict) else {}).items())
                            if isinstance(v, (int, float))},
            stock_realized_gold=_i("stock_realized_gold", 0),
        )


@dataclass
class World:
    """世界（顶层实体，一世界一 JSON）。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    premise: str = ""             # 用户填的世界观一句话
    banner: str = ""              # banner 图文件名（存于 world_images_dir）
    genre_tags: list[str] = field(default_factory=list)
    tone: str = ""                # 基调
    magic_level: int = 0          # 0-100（无魔 ~ 高魔）
    tech_level: int = 0           # 0-100（古代 ~ 未来）
    nsfw: bool = False
    scale: str = "medium"         # small | medium | large
    config_overlay: dict = field(default_factory=dict)   # 从 preset 拷贝的每世界覆盖
    tick_count: int = 0           # 世界时间（回合数）
    # ---- P4 世界滴答状态 ----
    event_log: list[WorldEvent] = field(default_factory=list)  # 世界事件/传闻（独立于 SceneLog 对话流）
    last_reconcile_tick: int = 0  # 上次 LLM 世界校准的 tick
    # [A3 2026-08-28] 待生成的好友主动私聊 npc_id 队列（tick_friends 纯 roll 侧标记，
    # tick friend_chat LLM 阶段消费并清空；预算不足/失败留存下轮重试，cap 2 防堆积）。
    pending_friend_chats: list = field(default_factory=list)
    # [战报 2026-08-28] 战斗历史（finish_combat 写入，右侧栏「战报」页读）：list[dict]
    # {tick, day, enemy, level, state(won/lost/fled), rounds, dmg_dealt, dmg_taken,
    #  xp, gold, loot, allies}；滚动保留最近 30 条（长线防膨胀）。
    combat_history: list = field(default_factory=list)
    # [P46 用户指示 2026-08-23] NPC 死亡补员标记：npc_permadeath 开启时死者由接班人/新人
    # 顶替（商人接店铺/平民随机补位），此列表记已补员的死者 id 防每 tick 重复补。
    backfilled_npc_ids: list[str] = field(default_factory=list)
    # [P] 势力战冷却水位：{"排序后的fid|fid": last_war_tick}，防死敌势力每回合刷战报滚雪球。
    faction_war_last_tick: dict = field(default_factory=dict)
    # ---- [P7k5] 日夜天气（tick 驱动，叙事注入用；纯推进无 LLM）----
    time_phase: str = "day"      # dawn/day/dusk/night
    day_count: int = 1           # 第几天（4 相位 = 1 天）
    weather: str = "clear"       # clear/cloud/rain/fog/storm
    lore: list[WorldLore] = field(default_factory=list)
    factions: list[Faction] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    npcs: list[NPC] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    quests: list[Quest] = field(default_factory=list)
    # [P7k1] 合成配方（LLM 世界生成时出，或引擎兜底从 items 组合）
    recipes: list = field(default_factory=list)
    # [P6] 商店列表（入 world JSON，交易/tick/LLM 重生成三路都改，随 save_world 原子写）。
    shops: list[Shop] = field(default_factory=list)
    # [P24a] 住宅列表（购宅时 home_engine.buy_home 追加；item 引用 stash/display_shelf，随 save_world 落盘）。
    homes: list[Home] = field(default_factory=list)
    # [D1 2026-08-29] 玩家据点列表（购地时 domain_engine.buy_domain 追加；每地点一处，
    # 地点改造状态在 Location.player_owned/kind/settlement_size）。
    domains: list[PlayerDomain] = field(default_factory=list)
    # [住宅网格 2026-08-24] 建筑升级材料需求 spec（LLM 生成/代码回退）：
    # {kind: {level: [{"item_id","name","rarity"}]}}，home_engine._spec_for 消费。随档落盘。
    building_material_specs: dict = field(default_factory=dict)
    # [P24d] 宠物列表（驯服时 pet_engine.create_pet 追加；数值成长模板挂种族池不随档存）。
    pets: list[Pet] = field(default_factory=list)
    # [P25a] 秘境列表（入口三渠道建：世界生成初始/地图拓展/奇遇 ruins；随 save_world 落盘）。
    dungeons: list[Dungeon] = field(default_factory=list)
    # [P25d] 世界 Boss 列表（tick crisis/major 事件高危险地点生成；随 save_world 落盘）。
    world_bosses: list[WorldBoss] = field(default_factory=list)
    # [P9] 天赋池（LLM 题材化生成或引擎兜底；开局 TalentSelectDialog 供玩家选 + [P16] 奇遇觉醒池，每条 Talent.to_dict）
    talent_pool: list = field(default_factory=list)
    # [P12] 题材怪物池（LLM 生成 name/role/desc/danger 区间/loot 资源名；野外遭遇引擎按
    # 地点+玩家等级动态缩放属性，loot 名已解析成 item id）。兜底 _GENRE_MONSTER_TEMPLATES。
    monster_pool: list = field(default_factory=list)
    # [P13] 技能池（LLM 题材化生成，供技能书教学：每池技能保证有一本书入世，经商店/
    # 精英怪掉落/奇遇/任务奖励获取）。兜底 _GENRE_SKILL_TEMPLATES。
    skill_pool: list = field(default_factory=list)
    # [怪物技能池 2026-08-25] 题材化怪物技能池（LLM 生成 name/type/element/inflicts/desc；
    # 数值 power/cost_mp/cooldown 由引擎按职能算）。wilderness_engine.monster_role_skills 抽取，
    # 兜底 _MOB_SKILL_NAMES。三分池：attack/heal/buff（状态池 = buff + attack 带控制 inflicts）。
    monster_skill_pool: list = field(default_factory=list)
    # [P34f] 交易所股市（城市 auction shop 内挂题材化大宗商品；每日 LLM 定价）。
    stock_market: StockMarket = field(default_factory=StockMarket)
    last_stock_priced_day: int = 0  # 上次行情定价的 day_count（每日 1 次水位，仿 last_reconcile_tick）
    # [P34g] 拍卖会事件列表（tick 周期在城市型聚落生成；随 save_world 落盘）。
    auctions: list[AuctionEvent] = field(default_factory=list)
    last_auction_spawn_day: int = 0  # 上次生成拍卖会的 day_count（周期水位）
    # [NPC 竞拍 2026-09-11] 上次 NPC 同场竞价的 day_count（每日 1 次水位，仿 last_life_day）。
    last_auction_bid_day: int = 0
    # [P39b] 委托订单板：挂聚落的 NPC 收购订单（玩家生产 -> 交付 -> 赚钱/交情/声望）。
    commissions: list = field(default_factory=list)  # list[dict]（CommissionOrder.to_dict）
    last_commission_day: int = 0    # 上次订单生成/过期清理的 day_count（周期水位）
    # [P42c] 上次日常跑腿生成的 day_count（quest_engine.maybe_spawn_errands 消费）。
    last_errand_day: int = 0
    # [P57 NPC 上门 2026-09-26] 信使系统：NPC 主动上门（信件/买凶/约战/求助）。
    # pending cap 6 / finished 留 8（outreach_engine 裁剪）；last_outreach_day 每日水位。
    outreaches: list = field(default_factory=list)   # list[NPCOutreach]
    last_outreach_day: int = 0
    # [G02/R3] 探伤赠药每日水位（全图每日至多 1 次探望，防全城同时上门）
    last_care_day: int = 0
    # [G02 续] 结义之请每日水位（每日至多 1 封提议信）
    last_sworn_proposal_day: int = 0
    # [G03/R3 2026-09-30] 有期限地区修正（region_pressure 引擎；只读时相乘不写回
    # 基础价——kind shortage/recovery 白名单，单因子钳 0.80-1.30）
    region_modifiers: list = field(default_factory=list)
    # [P39d+e] 世界编年史（长线世界记忆；chronicle_engine 消费）。
    chronicle: list = field(default_factory=list)          # list[dict] {start_day,end_day,text} 滚 8 条
    chronicle_pending: list = field(default_factory=list)  # list[dict] {tick,title,desc} 未沉淀 major/crisis
    chronicle_watermark_tick: int = 0                      # event_log 已收编进 pending 的最大 tick
    # [④ 2026-08-30] 共享世界知识分层（市井传闻；rumor_engine 消费）。
    shared_rumors: list = field(default_factory=list)      # list[dict] {tick,title,text,category,factions,locations,npcs} 滚 RUMOR_KEEP
    rumor_pending: list = field(default_factory=list)      # list[dict] 未口述化 major/crisis（含关联字段）
    rumor_watermark_tick: int = 0                          # event_log 已收编进 rumor_pending 的最大 tick
    # [⑦ 2026-08-30] 旁听 NPC-NPC 对话冷却水位（每世界）。
    last_overheard_tick: int = 0
    # [⑥ 2026-08-30] NPC 人格演化水位。
    drift_watermark_tick: int = 0                          # event_log 已扫描人格转折点的最大 tick
    last_drift_batch_tick: int = 0                         # 上次批量性格偏移 LLM 的 tick
    # [P35] 上次种植推进的 day_count（per-day 水位口径仿股市；tick farm 子阶段消费）。
    last_farm_day: int = 0
    # [P36a] 上次 NPC 生活推进的 day_count（tick npc_life 子阶段消费）。
    last_life_day: int = 0
    # [P39a] 上次每日社会交互（互市/冲突/互赠/生活开销）的 day_count（social_engine 消费）。
    last_social_day: int = 0
    # [D2 2026-08-29] 上次据点日结算（工资/欠薪/离职）的 day_count（domain_engine 消费）。
    last_domain_day: int = 0
    # [饱食度 2026-09-06] 上次饱食度日结算的 day_count（每日 1 次水位；跳天追补 cap 2 防离线饿死全图）
    last_hunger_day: int = 0
    # [自然恢复 2026-09-06] 上次自然恢复日结算的 day_count（每日 1 次水位；不追补）
    last_regen_day: int = 0
    # [玩家印象 2026-09-06] 传闻浅印象日批水位
    last_impression_day: int = 0
    # [野心 2026-09-06] 一生追求日结算水位
    last_ambition_day: int = 0
    # [事件任务 2026-09-06] major 事件派生任务的水位（tick 扫描 + day 频率闸）
    last_event_quest_day: int = 0
    last_event_quest_tick: int = 0
    # [C-lite 2026-10-02 用户指示] 同地点敌对 NPC 主动袭击（伏击落地）的水位：
    # npc_id -> 上次主动袭击的 tick（冷却防连环追杀；round-trip 同 faction_war_last_tick）。
    npc_ambush_last_tick: dict = field(default_factory=dict)
    # [剧情线 P46 2026-09-12] 前瞻导演层：story_arcs 列表 + 规划水位（推进幂等靠
    # stage.done，不设第二水位；终态滚动在 story_arc_engine.advance 内做）。
    story_arcs: list = field(default_factory=list)   # list[StoryArc]
    last_arc_day: int = 0                            # 上次剧情线规划的 day_count
    pending_arc_events: list = field(default_factory=list)  # 已结算、待写日志的剧情事件
    # [旁听对话 2026-09-06 用户指示] NPC-NPC 完整对话档案（不进主页面，NPC 档案里看；
    # 超阈值 LLM 压缩成 summary 并删原文）：[{a_id,b_id,a_name,b_name,tick,day,
    # turns:[{who,text}],summary,summarized}]
    npc_dialogues: list = field(default_factory=list)
    # [敌袭观战 2026-09-06 用户指示] 玩家在据点时敌袭不再后台速算，存规格待场景页弹
    # 观战战斗（CombatDialog spectate）：{domain_id, atk: [怪参数 dict], tick}；空 dict=无。
    pending_domain_siege: dict = field(default_factory=dict)
    # [P62 角色卡进世界 2026-09-26 用户拍板] 酒馆卡投放的要角 npc_id（cap 5，
    # world_sim_service.import_card_npc 消费；详情页 NPC 区加「酒馆卡」标记）。
    imported_card_npcs: list = field(default_factory=list)   # list[str]
    # [2026-08-23 用户卡绑定] 玩家绑定的用户卡（id 引用 data/users/；空=未绑定玩家无名）。
    # 姓名/人设注入场景上下文与 NPC 记忆，防 NPC 只知「那个男人/白衣年轻人」模糊指代。
    user_id: str = ""
    player: PlayerState = field(default_factory=PlayerState)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "premise": self.premise,
            "banner": self.banner,
            "genre_tags": list(self.genre_tags),
            "tone": self.tone,
            "magic_level": self.magic_level,
            "tech_level": self.tech_level,
            "nsfw": self.nsfw,
            "scale": self.scale,
            "config_overlay": dict(self.config_overlay),
            "tick_count": self.tick_count,
            "event_log": [e.to_dict() for e in self.event_log],
            "pending_friend_chats": [str(x) for x in (self.pending_friend_chats or []) if str(x)],
            "combat_history": [dict(h) for h in (self.combat_history or []) if isinstance(h, dict)],
            "backfilled_npc_ids": list(self.backfilled_npc_ids or []),
            "last_reconcile_tick": self.last_reconcile_tick,
            "faction_war_last_tick": {str(k): int(v) for k, v in self.faction_war_last_tick.items()},
            "time_phase": self.time_phase if self.time_phase in ("dawn", "day", "dusk", "night") else "day",
            "day_count": max(1, int(self.day_count)),
            "weather": self.weather if self.weather in ("clear", "cloud", "rain", "fog", "storm") else "clear",
            "lore": [e.to_dict() for e in self.lore],
            "factions": [e.to_dict() for e in self.factions],
            "locations": [e.to_dict() for e in self.locations],
            "npcs": [e.to_dict() for e in self.npcs],
            "items": [e.to_dict() for e in self.items],
            "quests": [e.to_dict() for e in self.quests],
            "recipes": [r.to_dict() for r in self.recipes if isinstance(r, Recipe)],
            "shops": [e.to_dict() for e in self.shops],
            "homes": [h.to_dict() for h in self.homes if isinstance(h, Home)],
            "domains": [dm.to_dict() for dm in self.domains if isinstance(dm, PlayerDomain)],
            "building_material_specs": dict(self.building_material_specs or {}),
            "pets": [p.to_dict() for p in self.pets if isinstance(p, Pet)],
            "dungeons": [dg.to_dict() for dg in self.dungeons if isinstance(dg, Dungeon)],
            "world_bosses": [wb.to_dict() for wb in self.world_bosses if isinstance(wb, WorldBoss)],
            "talent_pool": [Talent.from_dict(t).to_dict() for t in self.talent_pool if isinstance(t, dict)],
            "monster_pool": [dict(m) for m in self.monster_pool if isinstance(m, dict)],
            "skill_pool": [dict(m) for m in self.skill_pool if isinstance(m, dict)],
            "monster_skill_pool": [dict(m) for m in self.monster_skill_pool if isinstance(m, dict)],
            # [P34f] 股市
            "stock_market": self.stock_market.to_dict(),
            "last_stock_priced_day": max(0, int(self.last_stock_priced_day)),
            # [P34g] 拍卖会
            "auctions": [a.to_dict() for a in self.auctions if isinstance(a, AuctionEvent)],
            "last_auction_spawn_day": max(0, int(self.last_auction_spawn_day)),
            "last_auction_bid_day": max(0, int(self.last_auction_bid_day)),
            "commissions": [c.to_dict() for c in self.commissions
                            if isinstance(c, CommissionOrder)],
            "last_commission_day": max(0, int(self.last_commission_day)),
            "last_errand_day": max(0, int(self.last_errand_day)),
            # [P57] NPC 上门（pending cap 6 / finished 留 8）
            "outreaches": [o.to_dict() for o in self.outreaches
                           if isinstance(o, NPCOutreach)],
            "last_outreach_day": max(0, int(self.last_outreach_day)),
            "last_care_day": max(0, int(self.last_care_day)),
            "last_sworn_proposal_day": max(0, int(self.last_sworn_proposal_day)),
            "region_modifiers": [dict(m) for m in self.region_modifiers
                                  if isinstance(m, dict)][:12],
            "chronicle": [dict(c) for c in self.chronicle if isinstance(c, dict)][:8],
            "chronicle_pending": [dict(c) for c in self.chronicle_pending
                                  if isinstance(c, dict)][:80],
            "chronicle_watermark_tick": max(0, int(self.chronicle_watermark_tick)),
            # [④ 2026-08-30] 共享传闻 + [⑦] 旁听 + [⑥] 人格演化水位
            "shared_rumors": [dict(r) for r in self.shared_rumors if isinstance(r, dict)][:24],
            "rumor_pending": [dict(r) for r in self.rumor_pending if isinstance(r, dict)][:40],
            "rumor_watermark_tick": max(0, int(self.rumor_watermark_tick)),
            "last_overheard_tick": max(0, int(self.last_overheard_tick)),
            "drift_watermark_tick": max(0, int(self.drift_watermark_tick)),
            "last_drift_batch_tick": max(0, int(self.last_drift_batch_tick)),
            "last_farm_day": max(0, int(self.last_farm_day)),
            "last_life_day": max(0, int(self.last_life_day)),
            "last_social_day": max(0, int(self.last_social_day)),
            "last_domain_day": max(0, int(self.last_domain_day)),
            "last_hunger_day": max(0, int(self.last_hunger_day)),
            "last_regen_day": max(0, int(self.last_regen_day)),
            "last_impression_day": max(0, int(self.last_impression_day)),
            "last_ambition_day": max(0, int(self.last_ambition_day)),
            "last_event_quest_day": max(0, int(self.last_event_quest_day)),
            "last_event_quest_tick": max(0, int(self.last_event_quest_tick)),
            "npc_ambush_last_tick": {str(k): int(v) for k, v in
                                     (self.npc_ambush_last_tick or {}).items()},
            "story_arcs": [a.to_dict() for a in self.story_arcs if isinstance(a, StoryArc)],
            "last_arc_day": max(0, int(self.last_arc_day)),
            "pending_arc_events": [e.to_dict() for e in self.pending_arc_events
                                   if isinstance(e, WorldEvent)],
            "npc_dialogues": [dict(x) for x in (self.npc_dialogues or []) if isinstance(x, dict)][:40],
            "pending_domain_siege": dict(self.pending_domain_siege or {}),
            "imported_card_npcs": [str(x) for x in (self.imported_card_npcs or []) if str(x)][:5],
            "user_id": self.user_id,
            "player": self.player.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "World":
        # [!] isinstance 入口防御（守 §11）：JSON 文件整体被手改成数组时 .get 抛
        # AttributeError，load_all_worlds 的 try 兜底依赖 from_dict 抛可捕异常。
        if not isinstance(d, dict):
            d = {}
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            premise=d.get("premise", "") or "",
            banner=d.get("banner", "") or "",
            genre_tags=list(d.get("genre_tags") or []),
            tone=d.get("tone", "") or "",
            magic_level=_gi(d, "magic_level", 0),
            tech_level=_gi(d, "tech_level", 0),
            nsfw=bool(d.get("nsfw", False)),
            scale=d.get("scale", "medium") or "medium",
            config_overlay=dict(d.get("config_overlay") or {}),
            tick_count=_gi(d, "tick_count", 0),
            event_log=[WorldEvent.from_dict(e) for e in (d.get("event_log") or []) if isinstance(e, dict)],
            pending_friend_chats=[str(x) for x in (d.get("pending_friend_chats") or []) if str(x)][:2],
            combat_history=[dict(h) for h in (d.get("combat_history") or []) if isinstance(h, dict)],
            backfilled_npc_ids=[str(x) for x in (d.get("backfilled_npc_ids") or []) if x],
            last_reconcile_tick=_gi(d, "last_reconcile_tick", 0),
            faction_war_last_tick=_gi_map(d.get("faction_war_last_tick")),
            time_phase=(d.get("time_phase") or "day") if (d.get("time_phase") or "day") in ("dawn", "day", "dusk", "night") else "day",
            day_count=max(1, int(d.get("day_count", 1) or 1)),
            weather=(d.get("weather") or "clear") if (d.get("weather") or "clear") in ("clear", "cloud", "rain", "fog", "storm") else "clear",
            lore=[WorldLore.from_dict(e) for e in (d.get("lore") or []) if isinstance(e, dict)],
            factions=[Faction.from_dict(e) for e in (d.get("factions") or []) if isinstance(e, dict)],
            locations=[Location.from_dict(e) for e in (d.get("locations") or []) if isinstance(e, dict)],
            npcs=[NPC.from_dict(e) for e in (d.get("npcs") or []) if isinstance(e, dict)],
            items=[Item.from_dict(e) for e in (d.get("items") or []) if isinstance(e, dict)],
            quests=[Quest.from_dict(e) for e in (d.get("quests") or []) if isinstance(e, dict)],
            recipes=[Recipe.from_dict(r) for r in (d.get("recipes") or []) if isinstance(r, dict)],
            shops=[Shop.from_dict(e) for e in (d.get("shops") or []) if isinstance(e, dict)],
            homes=[Home.from_dict(h) for h in (d.get("homes") or []) if isinstance(h, dict)],
            domains=[PlayerDomain.from_dict(dm) for dm in (d.get("domains") or [])
                     if isinstance(dm, dict)],
            building_material_specs=(d.get("building_material_specs")
                                     if isinstance(d.get("building_material_specs"), dict) else {}),
            pets=[Pet.from_dict(p) for p in (d.get("pets") or []) if isinstance(p, dict)],
            dungeons=[Dungeon.from_dict(dg) for dg in (d.get("dungeons") or []) if isinstance(dg, dict)],
            world_bosses=[WorldBoss.from_dict(wb) for wb in (d.get("world_bosses") or []) if isinstance(wb, dict)],
            talent_pool=[Talent.from_dict(t).to_dict() for t in (d.get("talent_pool") or []) if isinstance(t, dict)],
            monster_pool=[dict(m) for m in (d.get("monster_pool") or []) if isinstance(m, dict)],
            skill_pool=[dict(m) for m in (d.get("skill_pool") or []) if isinstance(m, dict)],
            monster_skill_pool=[dict(m) for m in (d.get("monster_skill_pool") or []) if isinstance(m, dict)],
            # [P34f] 股市
            stock_market=StockMarket.from_dict(d.get("stock_market") or {}),
            last_stock_priced_day=max(0, _gi(d, "last_stock_priced_day", 0)),
            # [P34g] 拍卖会
            auctions=[AuctionEvent.from_dict(a) for a in (d.get("auctions") or [])
                      if isinstance(a, dict)],
            last_auction_spawn_day=max(0, _gi(d, "last_auction_spawn_day", 0)),
            last_auction_bid_day=max(0, _gi(d, "last_auction_bid_day", 0)),
            # [P39b] 委托订单（白名单透传，cap 12）
            commissions=[CommissionOrder.from_dict(c) for c in (d.get("commissions") or [])
                         if isinstance(c, dict)][:12],
            last_commission_day=max(0, _gi(d, "last_commission_day", 0)),
            last_errand_day=max(0, _gi(d, "last_errand_day", 0)),
            # [P57] NPC 上门（白名单透传）
            outreaches=[NPCOutreach.from_dict(o) for o in (d.get("outreaches") or [])
                        if isinstance(o, dict)],
            last_outreach_day=max(0, _gi(d, "last_outreach_day", 0)),
            last_care_day=max(0, _gi(d, "last_care_day", 0)),
            last_sworn_proposal_day=max(0, _gi(d, "last_sworn_proposal_day", 0)),
            region_modifiers=[dict(m) for m in (d.get("region_modifiers") or [])
                                if isinstance(m, dict)][:12],
            # [P39d+e] 编年史白名单透传（chronicle 滚 8 / pending cap 80）
            chronicle=[dict(c) for c in (d.get("chronicle") or [])
                       if isinstance(c, dict)][:8],
            chronicle_pending=[dict(c) for c in (d.get("chronicle_pending") or [])
                               if isinstance(c, dict)][:80],
            chronicle_watermark_tick=max(0, _gi(d, "chronicle_watermark_tick", 0)),
            # [④ 2026-08-30] 共享传闻 + [⑦] 旁听 + [⑥] 人格演化水位
            shared_rumors=[dict(r) for r in (d.get("shared_rumors") or []) if isinstance(r, dict)][:24],
            rumor_pending=[dict(r) for r in (d.get("rumor_pending") or []) if isinstance(r, dict)][:40],
            rumor_watermark_tick=max(0, _gi(d, "rumor_watermark_tick", 0)),
            last_overheard_tick=max(0, _gi(d, "last_overheard_tick", 0)),
            drift_watermark_tick=max(0, _gi(d, "drift_watermark_tick", 0)),
            last_drift_batch_tick=max(0, _gi(d, "last_drift_batch_tick", 0)),
            last_farm_day=max(0, _gi(d, "last_farm_day", 0)),
            last_life_day=max(0, _gi(d, "last_life_day", 0)),
            last_social_day=max(0, _gi(d, "last_social_day", 0)),
            last_domain_day=max(0, _gi(d, "last_domain_day", 0)),
            last_hunger_day=max(0, _gi(d, "last_hunger_day", 0)),
            last_regen_day=max(0, _gi(d, "last_regen_day", 0)),
            last_impression_day=max(0, _gi(d, "last_impression_day", 0)),
            last_ambition_day=max(0, _gi(d, "last_ambition_day", 0)),
            last_event_quest_day=max(0, _gi(d, "last_event_quest_day", 0)),
            last_event_quest_tick=max(0, _gi(d, "last_event_quest_tick", 0)),
            npc_ambush_last_tick=_gi_map(d.get("npc_ambush_last_tick")),
            story_arcs=[StoryArc.from_dict(a) for a in (d.get("story_arcs") or [])
                        if isinstance(a, dict)],
            last_arc_day=max(0, _gi(d, "last_arc_day", 0)),
            pending_arc_events=[WorldEvent.from_dict(e) for e in
                                (d.get("pending_arc_events")
                                 if isinstance(d.get("pending_arc_events"), list) else [])
                                if isinstance(e, dict)][:32],
            npc_dialogues=[dict(x) for x in (d.get("npc_dialogues")
                                             if isinstance(d.get("npc_dialogues"), list) else [])
                            if isinstance(x, dict)][:40],
            pending_domain_siege=(dict(d.get("pending_domain_siege"))
                                  if isinstance(d.get("pending_domain_siege"), dict) else {}),
            imported_card_npcs=[str(x) for x in (d.get("imported_card_npcs") or []) if str(x)][:5],
            user_id=d.get("user_id", "") or "",
            player=PlayerState.from_dict(d.get("player") or {}),
            created_at=d.get("created_at", _now()),
            updated_at=d.get("updated_at", _now()),
        )

    def touch(self):
        self.updated_at = _now()
