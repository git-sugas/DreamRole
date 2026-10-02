"""[P38a] NPC 社交演化引擎（纯 Python + SeededRng；零 LLM 零 Qt）。

同地 NPC 之间的交情随时间涨落：结交 -> 深交 -> 挚友，或生隙 -> 不和 -> 仇敌，也可和解
疏远。涨落因子全部确定性：阵营对齐（同势力和气 / 敌对势力相斥）+ 敌我身份 + 爱好投缘 +
性格气质 + SeededRng 随机火花（同 world+tick+salt 同结果可回放）。

数据契约：
- NPC.social 是数值底账 {target_id: -100..100}，双向对称存同一值；
- NPC.relationships 是展示层（upsert 阶段名条目，cap 10）——场景上下文
  _fmt_npc_detail_line 前 3 条注入旁白 + NpcDetailDialog 直接渲染，展示层自动跟进；
- 阶段跨越才产 WorldEvent(category="npc"，右侧「NPC 动态」日志栏直接消费；普通涨落
  不产事件防刷屏），仇敌跨越升 major。

阶段阈值（social_stage）：<=-60 仇敌 / <=-25 不和 / <25 陌生（无条目）/ <60 相识 /
<80 好友 / >=80 挚友。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from src.utils.rng import SeededRng

_PAIRS_PER_TICK = 4        # 每 tick 至多演化 4 对（同地配对抽样）
_SOCIAL_CAP = 100
_REL_CAP = 10              # relationships 展示条目上限（与 LLM 播种口径一致）

# 阶段定义：(key, 下界, 上界] 语义见 social_stage；""=陌生（不写条目不产事件）
_STAGE_BOUNDS = (("sworn_enemy", -100, -60), ("feud", -59, -25), ("", -24, 24),
                 ("acquaintance", 25, 59), ("friend", 60, 79), ("confidant", 80, 100))

# [数据量铁律] 阶段显示名（6 题材全覆盖，缺键回退西幻；登记 test_genre_data_volume.py）
_GENRE_STAGE_NAMES: dict[str, dict[str, str]] = {
    "xianxia": {"acquaintance": "论道之交", "friend": "道友", "confidant": "莫逆道友",
                "feud": "心存芥蒂", "sworn_enemy": "死敌"},
    "wuxia": {"acquaintance": "萍水之交", "friend": "江湖朋友", "confidant": "生死之交",
              "feud": "有过节", "sworn_enemy": "仇家"},
    "modern": {"acquaintance": "熟人", "friend": "朋友", "confidant": "挚友",
               "feud": "闹过矛盾", "sworn_enemy": "死对头"},
    "scifi": {"acquaintance": "同行者", "friend": "搭档", "confidant": "过命交情",
              "feud": "关系紧张", "sworn_enemy": "宿敌"},
    "apocalypse": {"acquaintance": "幸存同伴", "friend": "信赖的同伴", "confidant": "生死相依",
                   "feud": "有过冲突", "sworn_enemy": "仇敌"},
    "western_fantasy": {"acquaintance": "相识", "friend": "好友", "confidant": "挚友",
                        "feud": "不和", "sworn_enemy": "仇敌"},
}

# [数据量铁律] 阶段跨越事件文案（占位 {a}{b}；每类 >=3/题材，reconcile >=2；基线测试守护）
_GENRE_EVENT_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "xianxia": {
        "befriend": ["{a}与{b}在坊市论道投机，互留了名帖。",
                     "{a}向{b}请教了一个修行疑惑，两人相谈甚欢。",
                     "{a}与{b}在灵植园偶遇，交换了几株灵草的培育心得。"],
        "deepen": ["{a}与{b}结伴秘境历练归来，交情更进一层。",
                   "{a}将珍藏的丹方抄本示与{b}，引为知己。",
                   "{b}闭关出关当日，{a}携灵茶登门相贺。"],
        "strain": ["{a}与{b}为一件法器的归属起了口角。",
                   "{a}误信坊间流言，与{b}生出嫌隙。",
                   "{a}与{b}论道时观点相左，不欢而散。"],
        "feud": ["{a}与{b}当众决裂，扬言恩断义绝。",
                 "{a}指控{b}夺其机缘，二人势同水火。",
                 "{b}毁了{a}苦心布置的护山大阵，二人结下死仇。"],
        "reconcile": ["{a}与{b}经人调解，终于冰释前嫌。",
                      "{a}主动登门致歉，与{b}恢复了往来。"],
    },
    "wuxia": {
        "befriend": ["{a}与{b}在客栈拼桌畅饮，互道久仰。",
                     "{a}指点{b}三招剑法，两人以武会友。",
                     "{a}与{b}同赏一场雪夜比武，相谈甚欢。"],
        "deepen": ["{a}与{b}联手剿了黑风寨，从此以兄弟相称。",
                   "{a}把家传心法说与{b}听，引为知己。",
                   "{b}重伤之际得{a}背行百里求医，情义深重。"],
        "strain": ["{a}与{b}比武时手下失了分寸，落下怨气。",
                   "{a}怀疑{b}走漏了行镖的消息。",
                   "{a}与{b}为门派名分争执不休。"],
        "feud": ["{a}与{b}擂台立誓恩断义绝，江湖皆闻。",
                 "{a}折了{b}的兵器，二人结下梁子。",
                 "{b}夺了{a}的镖银，两人已成仇家。"],
        "reconcile": ["{a}与{b}在武林大会上握手言和。",
                      "经帮中长老说和，{a}与{b}尽释前嫌。"],
    },
    "modern": {
        "befriend": ["{a}与{b}在通勤路上搭上了话，互加了联系方式。",
                     "{a}帮{b}修好了抛锚的车，两人聊得投缘。",
                     "{a}与{b}发现是同一家健身房的常客，约好搭伙锻炼。"],
        "deepen": ["{a}与{b}合租公寓互相照应，成了无话不谈的朋友。",
                   "{a}搬家那天{b}来帮了一整天忙，交情更深厚了。",
                   "{a}与{b}合伙组队参加了城市马拉松。"],
        "strain": ["{a}与{b}因停车位的归属吵了一架。",
                   "{a}觉得{b}在项目里抢了自己的功劳。",
                   "{a}与{b}聚餐分账时闹得不愉快。"],
        "feud": ["{a}与{b}当众翻脸，从此见面不再打招呼。",
                 "{a}把{b}从好友列表删了个干净。",
                 "{b}拖欠{a}的借款迟迟不还，两人彻底闹掰。"],
        "reconcile": ["{a}与{b}在共同朋友的婚礼上和好如初。",
                      "{a}发消息向{b}道了歉，两人重新联系起来。"],
    },
    "scifi": {
        "befriend": ["{a}与{b}在舰桥值班时聊起了母星，颇为投缘。",
                     "{a}帮{b}调通了故障的维护机器人，两人互留了通讯码。",
                     "{a}与{b}在休眠舱外下了一盘三维棋，相谈甚欢。"],
        "deepen": ["{a}与{b}并肩修好了泄漏的聚变导管，成了过命交情。",
                   "{a}把自己攒的氧气配额分给了{b}，引为知己。",
                   "{a}与{b}联名申请了同一支外勤小队。"],
        "strain": ["{a}与{b}为航线选择起了争执。",
                   "{a}怀疑{b}向舰队情报官打了小报告。",
                   "{a}与{b}轮值表换班的事没谈拢。"],
        "feud": ["{a}与{b}在全舰通讯里公开决裂。",
                 "{a}锁死了{b}的实验室权限，两人彻底反目。",
                 "{b}检举{a}违规改造义体，二人已成宿敌。"],
        "reconcile": ["{a}与{b}在殖民地方舟对接仪式上重归于好。",
                      "经舰长调解，{a}与{b}恢复了协作关系。"],
    },
    "apocalypse": {
        "befriend": ["{a}与{b}在废墟搜刮时互相搭了把手，交换了据点位置。",
                     "{a}分了{b}半壶净水，两人聊起末世前的日子。",
                     "{a}与{b}联手吓退了一群拾荒者，互相记住了名字。"],
        "deepen": ["{a}与{b}守住了同一道防线一整夜，从此性命相托。",
                   "{a}把最后的抗生素给了{b}，这份情{b}记下了。",
                   "{a}与{b}在据点里搭伙开了间小工坊。"],
        "strain": ["{a}与{b}为半箱罐头的归属争执不下。",
                   "{a}怀疑{b}私藏了搜刮物资。",
                   "{a}与{b}在撤离路线上意见相左。"],
        "feud": ["{a}与{b}当着全据点的面决裂，各带走了一批物资。",
                 "{a}指控{b}见死不救，二人势不两立。",
                 "{b}在夜哨时抛弃了{a}，这笔账{a}记死了。"],
        "reconcile": ["{a}与{b}在共同抗尸潮后放下了旧怨。",
                      "据点首领出面调停，{a}与{b}重新并肩巡逻。"],
    },
    "western_fantasy": {
        "befriend": ["{a}与{b}在冒险者公会拼了一桌，聊得投缘。",
                     "{a}替{b}挡下了一支兽人箭，两人互留了姓名。",
                     "{a}与{b}在酒馆为同一支民谣举杯。"],
        "deepen": ["{a}与{b}同闯哥布林巢穴归来，成了过命的交情。",
                   "{a}把祖传的剑穗赠予{b}，引为挚友。",
                   "{a}与{b}结伴护送商队穿越了黑松林。"],
        "strain": ["{a}与{b}为战利品的分配起了争执。",
                   "{a}怀疑{b}在地下城深处动了私心。",
                   "{a}与{b}因信仰分歧冷了脸。"],
        "feud": ["{a}与{b}在公会大厅当众决裂，誓不再同行。",
                 "{a}折断了{b}的佩剑，两人结下血仇。",
                 "{b}在战斗中弃{a}而逃，{a}扬言必讨此债。"],
        "reconcile": ["{a}与{b}在篝火边把话说开，重新结伴上路。",
                      "经老冒险者劝和，{a}与{b}尽释前嫌。"],
    },
}


def stage_names(world: Any) -> dict:
    ov = getattr(world, "config_overlay", None)
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    return _GENRE_STAGE_NAMES.get(tid) or _GENRE_STAGE_NAMES["western_fantasy"]


def event_templates(world: Any) -> dict:
    ov = getattr(world, "config_overlay", None)
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    return _GENRE_EVENT_TEMPLATES.get(tid) or _GENRE_EVENT_TEMPLATES["western_fantasy"]


def social_stage(v: int) -> str:
    """数值 -> 阶段 key（""=陌生）。"""
    v = max(-_SOCIAL_CAP, min(_SOCIAL_CAP, int(v)))
    for key, lo, hi in _STAGE_BOUNDS:
        if lo <= v <= hi:
            return key
    return ""


def _clamp(v: int) -> int:
    return max(-_SOCIAL_CAP, min(_SOCIAL_CAP, int(v)))


def _pair_drift(world: Any, a: Any, b: Any, rng: SeededRng) -> int:
    """一对 NPC 的单次交情变化（确定性因子 + 随机火花）。"""
    delta = 0
    # 阵营对齐：同势力 +2；敌对势力 -3；盟友势力 +1
    facs = {f.id: f for f in (getattr(world, "factions", None) or [])}
    fa = facs.get(str(getattr(a, "faction_id", "") or ""))
    fb = facs.get(str(getattr(b, "faction_id", "") or ""))
    if fa is not None and fb is not None:
        if fa.id == fb.id:
            delta += 2
        else:
            rel = int(fa.relations.get(fb.id, 0) or 0)
            delta += (-3 if rel < 0 else (1 if rel > 0 else 0))
    # 敌我身份：一敌一友难相容 -2；同为敌对（同伙）+1
    ah, bh = bool(getattr(a, "hostile", False)), bool(getattr(b, "hostile", False))
    if ah != bh:
        delta -= 2
    elif ah and bh:
        delta += 1
    # 爱好投缘：交集非空 +2
    if (set(getattr(a, "hobbies", None) or [])
            & set(getattr(b, "hobbies", None) or [])):
        delta += 2
    # 性格气质：性格描述前 2 字关键词相同 +1
    pa = str(getattr(a, "personality", "") or "")[:2]
    pb = str(getattr(b, "personality", "") or "")[:2]
    if pa and pa == pb:
        delta += 1
    # 随机火花
    delta += rng.roll(-4, 4)
    return delta


def _same_loc_pairs(world: Any) -> list:
    """同地存活 NPC 相邻配对（按 id 稳定排序，确定性名单）。"""
    by_loc: dict[str, list] = {}
    for n in (getattr(world, "npcs", None) or []):
        if not getattr(n, "alive", False):
            continue
        by_loc.setdefault(str(getattr(n, "location_id", "") or ""), []).append(n)
    pairs = []
    for _, ns in sorted(by_loc.items()):
        ns = sorted(ns, key=lambda x: str(getattr(x, "id", "")))
        for i in range(len(ns) - 1):
            pairs.append((ns[i], ns[i + 1]))
    return pairs


def _upsert_relation(npc: Any, tid: str, tname: str, display: str) -> None:
    """展示层 relationships upsert（cap 10，同 LLM 播种口径）。"""
    rels = getattr(npc, "relationships", None)
    if rels is None:
        rels = []
        npc.relationships = rels
    for r in rels:
        if isinstance(r, dict) and str(r.get("target_id", "") or "") == tid:
            r["relation"] = display
            return
    rels.append({"target_id": tid, "target_name": tname, "relation": display, "desc": ""})
    if len(rels) > _REL_CAP:
        del rels[:len(rels) - _REL_CAP]


def _remove_relation(npc: Any, tid: str) -> None:
    rels = getattr(npc, "relationships", None) or []
    npc.relationships = [r for r in rels
                         if not (isinstance(r, dict) and str(r.get("target_id", "") or "") == tid)]


def _social_get(npc: Any, tid: str) -> int:
    soc = getattr(npc, "social", None) or {}
    try:
        return int(soc.get(tid, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _social_set(a: Any, b: Any, v: int) -> None:
    """双向对称写同一值（底账容错：social 缺失时原地建）。"""
    if getattr(a, "social", None) is None:
        a.social = {}
    if getattr(b, "social", None) is None:
        b.social = {}
    a.social[str(b.id)] = v
    b.social[str(a.id)] = v


# 跨越事件类别（新阶段 -> (文案 key, severity)；降回陌生 = 移除条目 + trivial）
_CROSSING = {"acquaintance": ("befriend", "minor"), "friend": ("deepen", "minor"),
             "confidant": ("deepen", "minor"), "feud": ("strain", "minor"),
             "sworn_enemy": ("feud", "major")}


def tick_social(world: Any, rng: SeededRng, tick: int) -> list:
    """每 tick 一轮：同地配对抽样 -> 涨落 -> 阶段跨越产事件 + relationships 同步。

    普通涨落不产事件（防刷屏）；跨越事件 severity：仇敌 major，其余 minor，
    疏远回陌生 trivial。事件 category="npc" 直入右侧「NPC 动态」日志栏。"""
    events: list = []
    pairs = _same_loc_pairs(world)
    if not pairs:
        return events
    k = min(_PAIRS_PER_TICK, len(pairs))
    chosen = rng.sample(pairs, k) if len(pairs) > k else list(pairs)
    tmpls = event_templates(world)
    names = stage_names(world)
    for a, b in chosen:
        v0 = _social_get(a, str(b.id))
        v1 = _clamp(v0 + _pair_drift(world, a, b, rng))
        _social_set(a, b, v1)
        old, new = social_stage(v0), social_stage(v1)
        if old == new:
            continue                                   # 无跨越不产事件
        if new == "":
            # 跌回陌生：负面阶段上行（和解）用 reconcile 池，正面阶段下行（疏远）用 trivial
            _remove_relation(a, str(b.id))
            _remove_relation(b, str(a.id))
            if old in ("feud", "sworn_enemy"):
                pool = tmpls.get("reconcile") or tmpls["befriend"]
                text = (pool[rng.roll(0, len(pool) - 1)] if pool
                        else "{a}与{b}放下了旧怨。")
                events.append(_ev(world, tick, "minor", f"{a.name}与{b.name}和解",
                                  text.format(a=a.name, b=b.name)))
            else:
                events.append(_ev(world, tick, "trivial", f"{a.name}与{b.name}疏远",
                                  f"{a.name}与{b.name}久未往来，渐渐疏远了。"))
            continue
        tpl_key, sev = _CROSSING[new]
        pool = tmpls.get(tpl_key) or tmpls["befriend"]
        text = (pool[rng.roll(0, len(pool) - 1)] if pool else "{a}与{b}有了新的往来。")
        events.append(_ev(world, tick, sev, f"{a.name}与{b.name}{names.get(new, new)}",
                          text.format(a=a.name, b=b.name)))
        _upsert_relation(a, str(b.id), str(b.name), names.get(new, new))
        _upsert_relation(b, str(a.id), str(a.name), names.get(new, new))
    return events


def _ev(world: Any, tick: int, severity: str, title: str, desc: str, npcs=None):
    """[!] 返回 WorldEvent 实体（tick 消费链 e.severity/e.to_dict()，裸 dict 必崩）。"""
    from src.models.world import WorldEvent
    return WorldEvent(tick=int(tick), category="npc", severity=severity,
                      title=title, desc=desc, npcs=list(npcs or []))


# ---- [P39a] 每日社会交互（互市/仇敌冲突/好友互赠 + 生活开销；day 水位 last_social_day）----
# [定稿 2026-08-22] 互市 compute_item_price x social 折扣（挚友 0.8/好友 0.9/负面拒交易）；
# 仇敌冲突止于负伤+遗失不击杀（击杀统一归 npc_permadeath 管）；生活开销统一数额不分级。

_LIVING_COST_BASE = 2   # 每日生活费 = 2 + level//3（平民 2，高等级 5-6）
_BASIC_RARITIES = ("common", "uncommon", "rare")
# 互市/互赠入包复用 npc_life_engine._inv_add（容量与堆叠上限同一来源）。
# 保留容量别名供既有边界检查与调用者读取。
from src.services.npc_life_engine import _NPC_INV_CAP as _INV_CAP

# [数据量铁律] 每日交互文案池（{a}{b}{it} 占位；3 类 x >=3/题材，基线测试守护）
# [P42a 用户定稿 2026-08-23] 场间闲谈：NPC 之间的日常交谈不进玩家旁白（玩家不可知），
# 而是写进双方 NPC 记忆（chat_notes），由既有定期整理（interval）并入总结——
# 玩家经由与 NPC 对话的回忆召回间接得知。话题优先取坊间热议（世界大事成为谈资），
# 兜底题材话题池。
_GENRE_CHAT_TOPICS: dict[str, list[str]] = {
    "xianxia": ["近期灵材行价的涨跌", "坊市里新来的修士", "山门外妖兽的动静",
                "某位散修突破的传闻", "丹阁新出的丹方"],
    "wuxia": ["镖局最近的行情", "江湖上流传的新仇旧怨", "官府悬赏的江洋大盗",
              "城中新开的武馆", "漕运码头的是非"],
    "modern": ["近期的物价与房租", "新开的餐馆", "公司里的八卦",
               "天气反常的闲谈", "小区新搬来的邻居"],
    "scifi": ["能源配额的新政策", "跃迁航线的班次变动", "殖民地新闻",
              "黑市流通的违规义体", "舰队征募的广告"],
    "apocalypse": ["下一批搜刮队的路线", "据点口粮的配给", "城外变异兽的迁徙",
                   "商队带来的外埠消息", "夜里尸潮的动静"],
    "western_fantasy": ["公会新张贴的悬赏", "酒馆里的旅人传闻", "边境领主的征兵令",
                        "集市物价的波动", "老猎人讲的怪物故事"],
}


def chat_topic(world: Any, rng: SeededRng) -> str:
    """闲谈话题：坊间热议（minor+ 事件标题）优先，题材话题池兜底。"""
    try:
        from src.services import chronicle_engine as che
        hot = che.hot_topics(world, 3)
        if hot:
            return str(getattr(hot[rng.roll(0, len(hot) - 1)], "title", "") or "近来的见闻")
    except Exception:
        pass
    ov = getattr(world, "config_overlay", None)
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    pool = _GENRE_CHAT_TOPICS.get(tid) or _GENRE_CHAT_TOPICS["western_fantasy"]
    return pool[rng.roll(0, len(pool) - 1)]


_GENRE_DAILY_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "xianxia": {
        "trade": ["{a}以{p}块灵石向{b}购得{it}，两人在坊市拱手作别。",
                  "{a}登门拜访{b}，以{p}块灵石换走了{it}。",
                  "坊市收摊前，{a}向{b}补购了{it}，付了{p}块灵石。"],
        "conflict": ["{a}与{b}在坊市街头动了手，{b}负伤而退。",
                     "{a}与{b}狭路相逢再起争执，{b}吃了亏。",
                     "{a}当众与{b}斗法，{b}狼狈败走。"],
        "gift": ["{a}将随身多年的{it}赠予{b}，聊表心意。",
                 "{a}得了件好东西，转头便送了{b}——正是{it}。",
                 "{a}以{it}相赠，{b}郑重收下。"],
    },
    "wuxia": {
        "trade": ["{a}在镖局账房以{p}两银子向{b}购得{it}。",
                  "{a}与{b}在客栈后院完成了一桩{p}两银子的买卖：{it}易主。",
                  "{a}寻到{b}，花{p}两银子买下了{it}。"],
        "conflict": ["{a}与{b}在长街拔刀相向，{b}带伤而逃。",
                     "{a}与{b}的旧怨又添新账，{b}败下阵来。",
                     "{a}一掌击退了{b}，围观人群四散。"],
        "gift": ["{a}解下随身{it}赠与{b}，江湖儿女不拘小节。",
                 "{a}把新得的{it}送给了{b}。",
                 "{a}以{it}为礼，{b}抱拳谢过。"],
    },
    "modern": {
        "trade": ["{a}扫码付了{p}元，从{b}手里买下了{it}。",
                  "{a}在二手群找到{b}，{p}元成交了{it}。",
                  "{a}顺路去{b}那儿取了{it}，转账{p}元。"],
        "conflict": ["{a}与{b}在街角扭打成一团，{b}挂了彩。",
                     "{a}与{b}的积怨爆发，{b}被揍进了医院。",
                     "{a}和{b}当街对峙动粗，{b}败退。"],
        "gift": ["{a}把{it}送给了{b}，「用不上，给你」。",
                 "{a}寄了份包裹给{b}，里面是{it}。",
                 "{a}请{b}喝茶，临走塞给他{it}。"],
    },
    "scifi": {
        "trade": ["{a}以{p}信用点向{b}购得{it}，交易记录已上链。",
                  "{a}在补给站用{p}信用点换走了{b}的{it}。",
                  "{a}和{b}在气闸舱口完成了{it}的交易，{p}信用点到账。"],
        "conflict": ["{a}与{b}在走廊爆发冲突，{b}被制服在地。",
                     "{a}与{b}的矛盾升级成斗殴，{b}受伤送医。",
                     "{a}一记电击棍放倒了{b}，冲突引来巡逻机器人。"],
        "gift": ["{a}把{it}的持有权转给了{b}。",
                 "{a}将自己的{it}赠予{b}，以示信任。",
                 "{a}为{b}留了一件{it}在储物柜里。"],
    },
    "apocalypse": {
        "trade": ["{a}用{p}枚物资券向{b}换来了{it}。",
                  "{a}在以物易物的篝火旁，以{p}枚物资券购得{b}的{it}。",
                  "{a}敲开{b}的门，花{p}枚物资券买下了{it}。"],
        "conflict": ["{a}与{b}为争半瓶净水动了手，{b}鼻青脸肿。",
                     "{a}与{b}在废墟里扭打，{b}败退时踉跄跌倒。",
                     "{a}与{b}的旧账翻了出来，{b}挨了顿打。"],
        "gift": ["{a}把省下的{it}塞给了{b}——末世里的情分。",
                 "{a}将自己珍藏的{it}赠予{b}。",
                 "{a}巡逻归来，给{b}带了件{it}。"],
    },
    "western_fantasy": {
        "trade": ["{a}以{p}枚金币向{b}购得{it}，两人在公会大厅握手。",
                  "{a}在集市摊位前付了{p}枚金币，从{b}手里接过{it}。",
                  "{a}与{b}完成了一笔{p}枚金币的交易：{it}。"],
        "conflict": ["{a}与{b}在酒馆后巷拔剑相斗，{b}带伤败走。",
                     "{a}与{b}的宿怨再度爆发，{b}被击倒在地。",
                     "{a}一记盾击撂倒了{b}，围观者哄散。"],
        "gift": ["{a}将{it}赠予{b}，冒险者之间的信物。",
                 "{a}把新缴获的{it}送给了{b}。",
                 "{a}以{it}为礼，{b}郑重收下。"],
    },
}


def living_cost(npc: Any) -> int:
    """每日生活费（统一数额不分级，[P39 定稿]；数值 = 2 + level//3）。"""
    return _LIVING_COST_BASE + max(0, int(getattr(npc, "level", 1) or 1)) // 3


def _item(world: Any, iid: str):
    return next((i for i in (getattr(world, "items", None) or [])
                 if getattr(i, "id", "") == iid), None)


def daily_templates(world: Any) -> dict:
    ov = getattr(world, "config_overlay", None)
    tid = ov.get("attribute_template_id", "western_fantasy") if isinstance(ov, dict) \
        else "western_fantasy"
    return _GENRE_DAILY_TEMPLATES.get(tid) or _GENRE_DAILY_TEMPLATES["western_fantasy"]


def _social_price_mult(v: int):
    """互市折扣：挚友 0.8 / 好友 0.9 / 中立 1.0；负面关系拒交易（None）。"""
    if v <= -25:
        return None
    if v >= 80:
        return 0.8
    if v >= 60:
        return 0.9
    return 1.0


def _wants(world: Any, npc: Any) -> list:
    """NPC 想买的物品 id（确定性有序）：配方缺料（生产需求）+ 生活消耗品（背包无药水时）。"""
    inv = list(getattr(npc, "inventory", None) or [])
    inv_counts = Counter(inv)
    want: list = []
    for r in (getattr(world, "recipes", None) or []):
        if getattr(r, "required_building", ""):
            continue
        out = _item(world, str(getattr(r, "output_item_id", "") or ""))
        if out is None or out.rarity not in _BASIC_RARITIES:
            continue
        for iid, qty in Counter(r.inputs or []).items():
            if inv_counts[iid] < qty and iid not in want and _item(world, iid) is not None:
                want.append(iid)
    if not any((_item(world, i) is not None
                and _item(world, i).type == "consumable") for i in inv):
        for it in (getattr(world, "items", None) or []):
            if (it.type == "consumable" and it.rarity in ("common", "uncommon")
                    and inv_counts[it.id] == 0 and it.id not in want):
                want.append(it.id)
                break
    return want[:4]


def _inv_add(npc: Any, item_id: str) -> bool:
    from src.services.npc_life_engine import _inv_add as _life_inv_add
    return _life_inv_add(npc, item_id)


def _try_trade(world: Any, buyer: Any, seller: Any, v: int, rng: SeededRng,
               tick: int, tmpls: dict):
    """单笔互市：买家缺的恰在卖家背包 -> social 折扣成交，物品/wallet 真实流转。"""
    from src.services import trade_engine as tre
    mult = _social_price_mult(v)
    if mult is None:
        return None
    for iid in _wants(world, buyer):
        if iid not in (getattr(seller, "inventory", None) or []):
            continue
        it = _item(world, iid)
        if it is None:
            continue
        price = max(1, int(tre.compute_item_price(it) * mult))
        if int(getattr(buyer, "wallet", 0) or 0) < price:
            return None                                   # 钱不够本轮作罢
        seller.inventory.remove(iid)
        if not _inv_add(buyer, iid):                       # 买家背包满 -> 回滚不成交
            seller.inventory.append(iid)
            return None
        buyer.wallet = int(getattr(buyer, "wallet", 0) or 0) - price
        seller.wallet = int(getattr(seller, "wallet", 0) or 0) + price
        pool = tmpls.get("trade") or []
        text = pool[rng.roll(0, len(pool) - 1)] if pool else "{a}向{b}买下了{it}。"
        name = it.name if it else iid
        return _ev(world, tick, "minor", f"{buyer.name}与{seller.name}交易",
                   text.format(a=buyer.name, b=seller.name, it=name, p=price))
    return None


def _power_of(npc: Any) -> int:
    from src.services import talent_engine as te
    return max(1, int(getattr(npc, "level", 1) or 1)) * 10 \
        + te.effective_stat(npc, "str") + te.effective_stat(npc, "vit")


def _try_conflict(world: Any, a: Any, b: Any, rng: SeededRng, tick: int, tmpls: dict):
    """仇敌偶遇冲突（轻量战力比，[定稿] 止于负伤+遗失不击杀；每对每日至多一桩）。"""
    if not rng.chance(0.25):
        return None
    winner, loser = (a, b) if _power_of(a) >= _power_of(b) else (b, a)
    dmg = max(1, int(getattr(winner, "level", 1) or 1)) * 3 + rng.roll(0, 5)
    loser.hp = max(1, int(getattr(loser, "hp", 1) or 1) - dmg)
    loot_name = ""
    if getattr(loser, "inventory", None) and rng.chance(0.4):
        idx = rng.roll(0, len(loser.inventory) - 1)
        iid = loser.inventory.pop(idx)
        it = _item(world, iid)
        loot_name = it.name if it else iid
        if not _inv_add(winner, iid):
            loser.inventory.append(iid)          # 胜者背包满：遗失物还给败者（物品守恒，勿湮灭）
            loot_name = ""
    pool = tmpls.get("conflict") or []
    text = pool[rng.roll(0, len(pool) - 1)] if pool else "{a}与{b}起了冲突。"
    desc = text.format(a=winner.name, b=loser.name, it=loot_name or "随身之物", p=0)
    if loot_name:
        desc += f"{loser.name}的{loot_name}落入了{winner.name}手中。"
    return _ev(world, tick, "major", f"{a.name}与{b.name}火并", desc)


def _try_gift(world: Any, a: Any, b: Any, rng: SeededRng, tick: int, tmpls: dict):
    """挚友互赠（生产资料不送；双向随机定赠方）。"""
    if not rng.chance(0.15):
        return None
    giver, receiver = (a, b) if rng.chance(0.5) else (b, a)
    giftables = [i for i in (getattr(giver, "inventory", None) or [])
                 if (_item(world, i) is not None and _item(world, i).type != "material")]
    if not giftables:
        return None
    iid = giftables[rng.roll(0, len(giftables) - 1)]
    it = _item(world, iid)
    giver.inventory.remove(iid)
    if not _inv_add(receiver, iid):                        # 收礼方背包满 -> 回滚
        giver.inventory.append(iid)
        return None
    pool = tmpls.get("gift") or []
    text = pool[rng.roll(0, len(pool) - 1)] if pool else "{a}将{it}赠予{b}。"
    return _ev(world, tick, "minor", f"{giver.name}赠礼",
               text.format(a=giver.name, b=receiver.name,
                           it=(it.name if it else iid), p=0))


def daily_interactions(world: Any, rng: SeededRng, tick: int) -> list:
    """每日社会交互（day 水位 last_social_day；每对同地 NPC 至多产 1 事件防刷屏）。

    顺序：生活开销（全体存活，静默扣款不产事件）-> 同地配对（互市优先，其次仇敌冲突/
    挚友互赠按 social 值互斥分流）。"""
    events: list = []
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    if int(getattr(world, "last_social_day", 0) or 0) == day:
        return events
    world.last_social_day = day
    for n in (getattr(world, "npcs", None) or []):
        if getattr(n, "alive", False) and int(getattr(n, "wallet", 0) or 0) > 0:
            n.wallet = max(0, int(n.wallet) - living_cost(n))
    tmpls = daily_templates(world)
    for a, b in _same_loc_pairs(world):
        v = _social_get(a, str(b.id))
        ev = _try_trade(world, a, b, v, rng, tick, tmpls) \
            or _try_trade(world, b, a, v, rng, tick, tmpls)
        if ev is None and v <= -60:
            ev = _try_conflict(world, a, b, rng, tick, tmpls)
        elif ev is None and v >= 80:
            ev = _try_gift(world, a, b, rng, tick, tmpls)
        if ev is not None:
            events.append(ev)
            continue
        # [P42a] 场间闲谈：非敌对对每日 8% 概率交谈——产「闲谈：」事件由服务层分流进
        # 双方 NPC 记忆（玩家不可知，不进 event_log）
        if v > -25 and rng.chance(0.08):
            topic = chat_topic(world, rng)
            events.append(_ev(world, tick, "trivial", f"闲谈：{a.name}与{b.name}",
                              f"{a.name}与{b.name}闲谈了一番，谈及{topic}。",
                              npcs=[str(a.id), str(b.id)]))
    return events


# ---- [玩家印象 2026-09-06 用户指示] NPC 各自对玩家的主观印象（非全局声誉）----
def update_impression(npc, tag: str, trust_delta: int = 0, firsthand: bool = True) -> None:
    """更新某 NPC 对玩家的印象。firsthand=True（亲眼互动）熟悉度 +1；False（传闻听说）
    只动标签不加熟悉度（浅印象）。标签白名单见 models.world._IMPRESSION_TAG_VALUES，
    白名单外忽略。trust 钳 -100~100；tags 滚动 cap 4（最新的在前）。

    钩子：赠送/战斗胜负/任务领奖/委托交付（firsthand）；传闻扩散（False）。"""
    from src.models.world import _IMPRESSION_TAG_VALUES
    if npc is None or tag not in _IMPRESSION_TAG_VALUES:
        return
    imp = getattr(npc, "player_impression", None)
    if not isinstance(imp, dict):
        imp = {}
    imp.setdefault("tags", [])
    imp.setdefault("trust", 0)
    imp.setdefault("familiarity", 0)
    tags = [t for t in (imp.get("tags") or []) if isinstance(t, str)]
    if tag not in tags:
        tags = [tag] + tags
        imp["tags"] = tags[:4]
    imp["trust"] = max(-100, min(100, int(imp.get("trust", 0) or 0) + int(trust_delta)))
    if firsthand:
        imp["familiarity"] = max(0, int(imp.get("familiarity", 0) or 0)) + 1
    npc.player_impression = imp


def impression_line(npc) -> str:
    """[玩家印象] 详情/上下文一行文案（空印象返回空串）。深浅由熟悉度分档（0-2 听说/3+ 相识/8+ 熟识）。"""
    imp = getattr(npc, "player_impression", None)
    if not isinstance(imp, dict) or not (imp.get("tags") or []):
        return ""
    tags = "、".join(str(t) for t in imp["tags"][:3])
    fam = int(imp.get("familiarity", 0) or 0)
    depth = "听说" if fam <= 2 else ("相识" if fam < 8 else "熟识")
    trust = int(imp.get("trust", 0) or 0)
    t = "信任" if trust >= 30 else ("警惕" if trust <= -30 else "")
    return f"他眼中的你（{depth}{'·' + t if t else ''}）：{tags}"
