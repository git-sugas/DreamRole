"""[P26a] 结义/婚恋关系引擎（纯 Python，无 LLM/Qt 依赖，可单测）。

架构分层（守 §21 / 数值范式铁律）：引擎管阈值与状态机（friend/sworn/sweetheart/spouse
单调递进，只进不退）+ 伤害加成 + 信物生成 + 离屏动态加权；LLM 管仪式叙事（经现有
场景回合 narration_hint 描写结义/婚礼，不改 settle 提示词）。

状态机（数据层已 scaffold：PlayerState.relations / NPC.shared_combats /
Item.is_courtship_gift / preset.relations_enabled+sworn_damage_bonus，本模块只读写）：
- affinity 60+ & friend_npc_ids -> friend（已有加好友按钮，本模块不动）
- affinity 80+ & shared_combats>=3 -> sworn（结义按钮，can_sworn 校验）
- sworn + 送出 is_courtship_gift 信物 + affinity>=90 -> sweetheart（apply_player_gift 检测）
- sweetheart + 节日当天 + 拥有住宅 -> spouse（求婚按钮，can_propose 校验）

题材化：6 题材关系称谓池（义兄/道侣/夫君 等）+ 信物名池（在 encounter_engine）。
无限世界：婚后是持续互动不是结局（配偶持续同行/离屏动态加权）；关系只进不退。
"""
from __future__ import annotations

from typing import Any

from src.models.world import WorldEvent


# ---- 阶段顺序 + 阈值常量（纯引擎）----
STAGE_FRIEND = "friend"
STAGE_SWORN = "sworn"
STAGE_SWEETHEART = "sweetheart"
STAGE_SPOUSE = "spouse"
_STAGE_ORDER = (STAGE_FRIEND, STAGE_SWORN, STAGE_SWEETHEART, STAGE_SPOUSE)

SWORN_AFFINITY = 80            # 结义所需交情
SWORN_COMBAT_COUNT = 3         # 结义所需共同战斗场次
SWEETHEART_AFFINITY = 90       # 恋人跃迁所需交情
SPOUSE_AFFINITY = 90           # 求婚所需交情

# sweetheart/spouse 离屏动态加权（叠加在 tick_friends 基础概率上）
SWEETHEART_MSG_BONUS = 0.05
SWEETHEART_GIFT_BONUS = 0.03
SPOUSE_MSG_BONUS = 0.08
SPOUSE_GIFT_BONUS = 0.05

# ---- 题材化关系称谓池（6 题材各 6 键，缺键回退西幻）----
# 每组 {sworn_male, sworn_female, sweetheart_male, sweetheart_female, spouse_male, spouse_female}
# NPC 无显式性别字段——按 role/name 关键词推断（_infer_gender）；无法推断用 sworn_neutral 等。
_GENRE_RELATION_TERMS: dict[str, dict] = {
    "xianxia": {
        "sworn_male": "师兄", "sworn_female": "师妹", "sworn_neutral": "道友",
        "sweetheart_male": "道侣", "sweetheart_female": "道侣", "sweetheart_neutral": "道侣",
        "spouse_male": "双修道侣", "spouse_female": "双修道侣", "spouse_neutral": "双修道侣",
    },
    "wuxia": {
        "sworn_male": "义兄", "sworn_female": "义妹", "sworn_neutral": "结义兄弟",
        "sweetheart_male": "心上人", "sweetheart_female": "心上人", "sweetheart_neutral": "心上人",
        "spouse_male": "夫君", "spouse_female": "娘子", "spouse_neutral": "结发",
    },
    "modern": {
        "sworn_male": "义兄", "sworn_female": "义妹", "sworn_neutral": "结拜",
        "sweetheart_male": "男朋友", "sweetheart_female": "女朋友", "sweetheart_neutral": "恋人",
        "spouse_male": "老公", "spouse_female": "老婆", "spouse_neutral": "配偶",
    },
    "scifi": {
        "sworn_male": "义兄", "sworn_female": "义妹", "sworn_neutral": "契约伙伴",
        "sweetheart_male": "伴侣", "sweetheart_female": "伴侣", "sweetheart_neutral": "伴侣",
        "spouse_male": "终身伴侣", "spouse_female": "终身伴侣", "spouse_neutral": "终身伴侣",
    },
    "apocalypse": {
        "sworn_male": "义兄", "sworn_female": "义妹", "sworn_neutral": "生死之交",
        "sweetheart_male": "心上人", "sweetheart_female": "心上人", "sweetheart_neutral": "心上人",
        "spouse_male": "夫君", "spouse_female": "娘子", "spouse_neutral": "结发",
    },
    "western_fantasy": {
        "sworn_male": "义兄", "sworn_female": "义妹", "sworn_neutral": "结义兄弟",
        "sweetheart_male": "恋人", "sweetheart_female": "恋人", "sweetheart_neutral": "恋人",
        "spouse_male": "夫君", "spouse_female": "娘子", "spouse_neutral": "配偶",
    },
}

# 性别推断关键词（NPC 无 gender 字段，按 role/name 粗估；命中 male/female 任一即可，无法推断 neutral）
_MALE_KEYWORDS = ("兄", "弟", "父", "翁", "公", "汉", "郎", "男", "哥", "爷", "叔", "伯", "将", "王", "主")
_FEMALE_KEYWORDS = ("姐", "妹", "母", "婆", "娘", "女", "姑", "嫂", "姨", "姬", "妃", "后", "仙子", "丫头")


def _terms(world: Any) -> dict:
    ov = getattr(world, "config_overlay", None) or {}
    gid = str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy") if isinstance(ov, dict) else "western_fantasy"
    return _GENRE_RELATION_TERMS.get(gid) or _GENRE_RELATION_TERMS["western_fantasy"]


def _infer_gender(npc: Any) -> str:
    """粗估 NPC 性别（male/female/neutral）——按 role/name 关键词；无法推断 neutral。"""
    text = (str(getattr(npc, "role", "") or "") + str(getattr(npc, "name", "") or ""))
    if any(k in text for k in _FEMALE_KEYWORDS):
        return "female"
    if any(k in text for k in _MALE_KEYWORDS):
        return "male"
    return "neutral"


# ============ 状态机读取 ============
def stage_of(world: Any, npc_id: str) -> str:
    """读玩家与某 NPC 的关系阶段（空串=无关系）。"""
    rel = getattr(getattr(world, "player", None), "relations", None) or {}
    if not isinstance(rel, dict):
        return ""
    entry = rel.get(str(npc_id))
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("stage", "") or "")


def since_day_of(world: Any, npc_id: str) -> int:
    rel = getattr(getattr(world, "player", None), "relations", None) or {}
    if not isinstance(rel, dict):
        return 0
    entry = rel.get(str(npc_id))
    if not isinstance(entry, dict):
        return 0
    try:
        return max(1, int(entry.get("since_day", 1) or 1))
    except (TypeError, ValueError):
        return 1


def relation_term(world: Any, npc: Any, stage: str) -> str:
    """题材化关系称谓（""=无关系）。按 npc 性别选 male/female/neutral 键。"""
    if not stage or stage not in _STAGE_ORDER:
        return ""
    gender = _infer_gender(npc)
    terms = _terms(world)
    key = f"{stage}_{gender}"
    return str(terms.get(key) or terms.get(f"{stage}_neutral") or "")


def has_relation(world: Any, npc_id: str) -> bool:
    return bool(stage_of(world, npc_id))


# ============ 阶段跃迁门控 ============
def _enabled(world: Any, preset: Any) -> bool:
    if preset is None:
        return True
    return bool(getattr(preset, "relations_enabled", True))


def can_sworn(world: Any, npc: Any, preset: Any) -> tuple[bool, str]:
    """结义条件：relations_enabled + 非敌对/存活 + 阶段<sworn + 交情>=80 + 共同战斗>=3。"""
    if not _enabled(world, preset):
        return False, "关系系统未启用"
    if npc is None or getattr(npc, "hostile", False) or not getattr(npc, "alive", True):
        return False, "对方不可结义"
    cur = stage_of(world, npc.id)
    if cur in (STAGE_SWORN, STAGE_SWEETHEART, STAGE_SPOUSE):
        return False, "已结义"
    aff = int(getattr(npc, "affinity", 0) or 0)
    if aff < SWORN_AFFINITY:
        return False, f"交情不足（需{SWORN_AFFINITY}，当前{aff}）"
    sc = int(getattr(npc, "shared_combats", 0) or 0)
    if sc < SWORN_COMBAT_COUNT:
        return False, f"共同战斗不足（需{SWORN_COMBAT_COUNT}场，当前{sc}）"
    return True, ""


def can_propose(world: Any, npc: Any, preset: Any) -> tuple[bool, str]:
    """求婚条件：relations_enabled + 存活 + 阶段==sweetheart + 交情>=90 + 拥有住宅。

    [纠偏 2026-09-10] 旧注释多写了「节日当天」——节日系统已于 2026-09-05 整体裁剪，
    代码从未有该门控，注释与实现分叉（守「文档说 A 代码做 B」的漂移）。"""
    if not _enabled(world, preset):
        return False, "关系系统未启用"
    if npc is None or not getattr(npc, "alive", True):
        return False, "对方不可求婚"
    if stage_of(world, npc.id) != STAGE_SWEETHEART:
        return False, "须先结为恋人"
    aff = int(getattr(npc, "affinity", 0) or 0)
    if aff < SPOUSE_AFFINITY:
        return False, f"交情不足（需{SPOUSE_AFFINITY}，当前{aff}）"
    if not (getattr(getattr(world, "player", None), "home_ids", None) or []):
        return False, "须拥有住宅"
    return True, ""


def _set_stage(world: Any, npc_id: str, stage: str) -> None:
    rel = getattr(getattr(world, "player", None), "relations", None)
    if not isinstance(rel, dict):
        rel = {}
        world.player.relations = rel
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    rel[str(npc_id)] = {"stage": stage, "since_day": day}


def make_sworn(world: Any, npc: Any) -> str:
    """结义跃迁：写 relations[sworn] + 返回 narration_hint（题材化）。调用方应先 can_sworn。"""
    _set_stage(world, npc.id, STAGE_SWORN)
    term = relation_term(world, npc, STAGE_SWORN)
    return (f"与{npc.name}歃血为盟，结为{term or '义兄弟'}，从此同生共死、祸福相依")


def make_sweetheart(world: Any, npc: Any) -> str:
    """恋人跃迁：写 relations[sweetheart] + 返回 narration_hint。由 apply_player_gift 信物分支调。"""
    _set_stage(world, npc.id, STAGE_SWEETHEART)
    term = relation_term(world, npc, STAGE_SWEETHEART)
    return (f"送出定情信物，{npc.name}含羞收下——二人结为{term or '恋人'}，情定此生")


def make_spouse(world: Any, npc: Any) -> str:
    """配偶跃迁：写 relations[spouse] + major event 入历史 + 自动 invite_companion + 返回 hint。

    调用方应先 can_propose。配偶豁免 companion 位限制（can_invite_companion 内已改），
    故满员也能邀请配偶同行。
    """
    _set_stage(world, npc.id, STAGE_SPOUSE)
    term = relation_term(world, npc, STAGE_SPOUSE)
    # major event 入世界历史
    loc_id = str(getattr(getattr(world, "player", None), "location_id", "") or "")
    world.event_log.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0),
        category="event", severity="major",
        title=f"婚礼：玩家与{npc.name}",
        desc=f"玩家与{npc.name}举行婚礼，结为{term or '夫妻'}，宾客盈门、传为佳话。",
        npcs=[npc.id],
        locations=[loc_id] if loc_id else [],
    ))
    # 自动邀请同行（豁免位限制；若已在同行名单则跳过）
    from src.services import npc_reaction_engine as nre
    if npc.id not in (getattr(world.player, "companion_npc_ids", None) or []):
        nre.invite_companion(world, npc)
    # [审核修复 2026-09-13] 节日系统已于 2026-09-05 整体裁剪（calendar_engine 同款声明），
    # 「在节日良辰」是不存在的设定，会作为 hint 串进婚礼旁白（轻度 OOC）。
    return (f"与{npc.name}举行婚礼，结为{term or '夫妻'}，从此相伴相随")


# ============ 战斗加成（CombatSession.sworn_npc_ids 预注入后供调用方读）============
def sworn_npc_ids_of(world: Any, ally_npc_ids: list) -> set:
    """从 ally npc_id 列表筛出 sworn 阶段同伴 id（start_combat 预注入用）。"""
    out = set()
    for nid in (ally_npc_ids or []):
        nid = str(nid or "")
        if nid and stage_of(world, nid) == STAGE_SWORN:
            out.add(nid)
    return out


def sworn_damage_mult(world: Any, session_sworn_ids: set, preset: Any) -> float:
    """有 sworn 同伴在场 -> preset.sworn_damage_bonus，否则 1.0。"""
    if not session_sworn_ids:
        return 1.0
    if preset is None:
        return 1.10
    try:
        raw = getattr(preset, "sworn_damage_bonus", 1.10)
        m = 1.10 if raw is None else float(raw)  # [!] 0.0 合法（关闭加成），勿 or 复活；max(1.0,·) 归一
    except (TypeError, ValueError):
        m = 1.10
    return max(1.0, m)


# ============ 离屏动态加权（tick_world 子阶段）============
def friend_event_bonuses(world: Any, npc: Any) -> tuple[float, float]:
    """返回 (msg_bonus, gift_bonus) 叠加在 tick_friends 基础概率上（sweetheart/spouse 加权）。

    无关系/friend/sworn -> (0,0)；sweetheart -> (0.05, 0.03)；spouse -> (0.08, 0.05)。
    """
    stage = stage_of(world, npc.id)
    if stage == STAGE_SWEETHEART:
        return (SWEETHEART_MSG_BONUS, SWEETHEART_GIFT_BONUS)
    if stage == STAGE_SPOUSE:
        return (SPOUSE_MSG_BONUS, SPOUSE_GIFT_BONUS)
    return (0.0, 0.0)


def tick_relations(world: Any) -> list:
    """关系离屏动态加权子阶段（纯 Python，无 LLM）。

    本子阶段不直接产事件——加权由 tick_friends 读取 friend_event_bonuses 应用（解耦：
    避免重复 roll 同一 friend）。本函数返回空列表占位（_phase 隔离兼容），保留扩展位
    （后续可在此产 sweetheart/spouse 专属事件如「思念」传闻）。纯 best-effort。
    """
    return []
