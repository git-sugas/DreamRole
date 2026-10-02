"""[P57 NPC 上门 2026-09-26] 信使引擎：NPC 主动找上玩家（信件/买凶/约战/求助）+ 过期不候。

三 PM 共识落地（read.md 行动清单 [M]）：把「被世界惦记」变成确定体验——
每日 roll + 旱涝保底（>=3 天没被惦记必出一条），治中期目标真空与长线锚。

架构分层（守 §21 铁律）：
- 引擎出结构与后果（谁上门/哪类/几时过期/过期与拒绝的数值后果）——纯 Python + SeededRng
  确定性（盐 = world.id + tick + "outreach"），零 LLM。文案走题材模板池（守 §23 数据量铁律）。
- 消费既有系统：NPC 数据（交情/hostile/combat_role/hp/wallet）、好感印象（soc.update_impression）、
  任务系统（买凶 -> kill 任务，领赏须回发布人当面——复用既有领奖门控）。
- 产出喂给谁：场景页「信使」按钮（交互入口）、右侧 NPC 动态栏（WorldEvent 播报）、
  议程面板（agenda_items 聚合 pending）、战斗系统（约战）。
- [§24 底线] 约战对象等级 <= 玩家等级 +3（有胜算空间，建议层不送死）；买凶目标只挑
  敌对 NPC（kill 进度钩子按 NPC 名匹配，野怪不入 world.npcs 不稳定）。

与 tick_friends/daily_help_call 的关系：带话/寄礼/好友求援是「好友日常」低强度通道
（零交互、无过期）；本引擎是「可交互事件」通道（接受/拒绝/过期各有后果）——互补不重叠，
同一 NPC 可两路都跑（事件文案口径不同不冲突）。
"""
from __future__ import annotations

from src.models.world import NPCOutreach, Quest, WorldEvent
from src.models.world_sim_preset import GenreText
from src.utils.rng import SeededRng

# ---- 节奏常量（纯引擎定档；无 preset 旋钮，仿 daily_help_call 口径）----
_PITY_DAY = 3            # 从第 3 天起，「旱涝保底」生效（开局 2-3 天内必被惦记一次）
_DROUGHT_DAYS = 3        # 自上次上门后隔 N 天且当前无 pending -> 保底一条
_MAX_PENDING = 6         # 同时挂着的上门上限（防刷屏；满了不产新的）
_KEEP_FINISHED = 8       # 已处理记录保留条数（滚删最旧）
_LETTER_CHANCE = 0.30
_BOUNTY_CHANCE = 0.10
_DUEL_CHANCE = 0.08
_PLEA_CHANCE = 0.15
_EXPIRES = {"letter": 3, "bounty": 2, "duel": 2, "plea": 2}   # 过期不候宽限（天）
_DUEL_LEVEL_MARGIN = 3   # 约战对手等级 <= 玩家等级 + 3（§24 建议层底线）
_PLEA_HP_PCT = 60        # hp < 60% 的熟人才开口求药
_PLEA_WALLET_MAX = 30    # wallet < 30 的熟人才开口借钱
_PLEA_AFFINITY_MIN = 25  # 求助交情门槛（低于此不开口，防陌生人碰瓷）
_LETTER_AFFINITY_MIN = 40

_KIND_LABELS = {"letter": "来信", "bounty": "悬赏", "duel": "战书", "plea": "求助"}

# ---- 题材模板池（6 题材 x 4 类 x 2 变体；占位符 {npc}{loc}{target}{gold}{item}，
#      引擎填值——{gold} 经 GenreText.currency 题材化，池内零币种硬编码，守 §23）----
_GENRE_OUTREACH_TEMPLATES: dict[str, dict[str, list[dict[str, str]]]] = {
    "western_fantasy": {
        "letter": [
            {"title": "{npc}的信使", "text": "{npc}遣人送来一封信：近来边境不太平，盼你有空来{loc}一叙，我有话想当面说。"},
            {"title": "{npc}的书信", "text": "{npc}托商队捎来书信，字迹匆匆：旧事有了新的转机，望你来{loc}一聚，细节不便写在纸上。"},
        ],
        "bounty": [
            {"title": "{npc}的悬赏", "text": "「{npc}」贴出悬赏：{target}作恶多端，取其性命者赏 {gold}。胆识与刀剑，总得有一样。"},
            {"title": "悬赏令·{target}", "text": "{npc}出 {gold} 悬赏{target}的性命。此獠一日不除，{loc}一日不宁——愿意接，就来找我。"},
        ],
        "duel": [
            {"title": "{npc}的战书", "text": "{npc}在{loc}放话，指名要与你对阵。剑士看重脸面，这一战避无可避——接或不接，给个话。"},
            {"title": "战书", "text": "战书已至：{npc}听闻你的名头，约定在{loc}切磋一场。赢了扬名，输了长记性。"},
        ],
        "plea": [
            {"title": "{npc}的求助", "text": "{npc}托人带话：眼下急缺一份「{item}」救急，若你手头宽裕，望施以援手——这份情记下了。"},
            {"title": "{npc}的请求", "text": "{npc}家中遭了难事，急缺「{item}」。肯帮忙的，必有报答。"},
        ],
    },
    "xianxia": {
        "letter": [
            {"title": "{npc}的传讯玉简", "text": "{npc}传来一道玉简：近日观星见变，与你有旧约未了，请来{loc}一晤。"},
            {"title": "{npc}的仙鹤传书", "text": "{npc}遣仙鹤送书：宗门近来风云暗涌，故人相见，或有要事相商——盼你到{loc}一叙。"},
        ],
        "bounty": [
            {"title": "悬赏令·{target}", "text": "{npc}张榜悬赏 {gold}：妖修{target}祸乱乡里，伏诛者可来领赏。"},
            {"title": "{npc}的血仇榜", "text": "悬赏：{target}与{npc}有血仇，取其性命者得 {gold}。仙凡有别，因果自负。"},
        ],
        "duel": [
            {"title": "{npc}的战帖", "text": "{npc}递来战帖：听闻你道法不俗，愿与你在{loc}论道切磋，点到即止——可敢应战？"},
            {"title": "战帖·论道", "text": "战帖：{npc}闭关初成，正需一场斗法验证所学，指名寻你，约在{loc}见面。"},
        ],
        "plea": [
            {"title": "{npc}的求援", "text": "{npc}传讯求助：炉鼎出了岔子，急缺「{item}」压住伤势，望道友援手，来日必报。"},
            {"title": "{npc}的因果", "text": "{npc}旧伤复发，急需「{item}」调理。肯出手相帮的，这份因果记在心头。"},
        ],
    },
    "wuxia": {
        "letter": [
            {"title": "{npc}的口信", "text": "{npc}托镖局带口信：风声紧了，故人之约不可忘，请来{loc}详谈。"},
            {"title": "一封无署名的信", "text": "一封信落在你手上，落款画着{npc}的私记：老地方有变，速来{loc}。"},
        ],
        "bounty": [
            {"title": "江湖悬红·{target}", "text": "江湖悬红：{npc}出 {gold} 索{target}性命。恩怨分明的买卖，接不接随你。"},
            {"title": "{npc}的赏格", "text": "{npc}放话：取{target}首级者，赏 {gold}。仇家临门，唯快不破。"},
        ],
        "duel": [
            {"title": "{npc}的约战帖", "text": "{npc}下帖约战：久闻大名，未免遗憾，约在{loc}领教高招。兵刃无眼，望自珍重。"},
            {"title": "战书", "text": "战书：{npc}自出道以来未逢敌手，闻你之名，特来{loc}讨教一二。"},
        ],
        "plea": [
            {"title": "{npc}的求药信", "text": "{npc}托人求药：伤势反复，急需「{item}」，江湖救急——大恩不言谢。"},
            {"title": "{npc}的救命钱", "text": "{npc}遭人暗算困于{loc}，缺一份「{item}」续命。拔刀相助者，生死之交。"},
        ],
    },
    "modern": {
        "letter": [
            {"title": "{npc}的消息", "text": "{npc}发来一条长消息：好久不见，最近有件事想找你商量，方便的话来{loc}坐坐。"},
            {"title": "{npc}的留言", "text": "{npc}留了言：「看到尽快回我，有个忙只有你能帮。」落款是熟悉的签名。"},
        ],
        "bounty": [
            {"title": "{npc}的委托", "text": "{npc}愿意出 {gold} 感谢能让{target}再也不来捣乱的人。这钱烫手，但干净。"},
            {"title": "一桩了断", "text": "有人托{npc}带话：{target}欠的债总要还——办成了，{gold}一分不少。"},
        ],
        "duel": [
            {"title": "{npc}的约架", "text": "{npc}约你在{loc}「聊聊」：有些话动手比动口痛快，敢来就别爽约。"},
            {"title": "{npc}放话了", "text": "{npc}放话要在{loc}会会你。人都看着，这一场躲不掉。"},
        ],
        "plea": [
            {"title": "{npc}的急电", "text": "{npc}急电：家里出了急事，急需「{item}」，跑遍全城都缺货——帮帮我。"},
            {"title": "{npc}的难处", "text": "{npc}开口了：手头实在周转不开，缺一份「{item}」，这份情下个月一定还上。"},
        ],
    },
    "scifi": {
        "letter": [
            {"title": "{npc}的加密频道", "text": "加密频道传来{npc}的讯号：监测到异常数据流，与你我都有关，到{loc}碰头详谈。"},
            {"title": "{npc}的信标", "text": "{npc}的信标在你终端亮起：旧协议还有一章没走完，{loc}见。"},
        ],
        "bounty": [
            {"title": "悬赏合同·{target}", "text": "{npc}挂出悬赏 {gold}：非法改造体{target}威胁航道安全，终结者得款。"},
            {"title": "{npc}的委托单", "text": "委托：{npc}出 {gold} 求{target}的下落与终结方式。合同期内，生死自负。"},
        ],
        "duel": [
            {"title": "决斗协议", "text": "{npc}发来决斗协议：模拟场见真章，坐标{loc}——性能与胆识，总要有一样服人。"},
            {"title": "{npc}的校准请求", "text": "邀战请求：{npc}的新机体需要实战数据，指名与你校准，地点{loc}。"},
        ],
        "plea": [
            {"title": "{npc}的求救信号", "text": "{npc}的求救信号：维生系统告急，缺一份「{item}」，坐标已附——拜托了。"},
            {"title": "{npc}的抢修请求", "text": "{npc}的讯息带着杂音：舱段失压抢修，急需「{item}」，事后必谢。"},
        ],
    },
    "apocalypse": {
        "letter": [
            {"title": "{npc}的信号弹", "text": "{npc}用旧频率发了条讯息：据点方向有动静，老朋友，来{loc}一趟，当面说。"},
            {"title": "门缝里的纸条", "text": "纸条从门缝塞进来，是{npc}的字：别走大路，我有事托你——{loc}见。"},
        ],
        "bounty": [
            {"title": "废土悬赏·{target}", "text": "{npc}开出 {gold} 的赏格：{target}抢了我们最后的净水，血债血偿。"},
            {"title": "{npc}的赏金", "text": "废土悬赏：{target}的行踪值 {gold}——{npc}只认结果。"},
        ],
        "duel": [
            {"title": "{npc}的擂台", "text": "{npc}在{loc}立了擂：废土只信拳头，赢的人说话。你，敢来吗？"},
            {"title": "分个高下", "text": "{npc}放话要跟你分个高下，地点{loc}。末日里，名头就是粮食。"},
        ],
        "plea": [
            {"title": "{npc}的求救", "text": "{npc}的求救：营地里有人高烧不退，急缺「{item}」，能救一个是一个。"},
            {"title": "{npc}的口粮", "text": "{npc}断顿了，急缺「{item}」。废土上肯分口粮的，都是过命的交情。"},
        ],
    },
}


def push_system_letter(world, npc_id: str, title: str, text: str,
                       payload: dict | None = None, expires_days: int = 3) -> bool:
    """[导演偏置 2026-09-26] 系统直推一封信（P57 信使通道复用：剧情线高潮求援上门等）。

    绕过 maybe_generate 的每日 roll（不走水印、不消费共享 rng）；respect pending
    上限 _MAX_PENDING 防刷屏。npc 不存在/死亡返回 False（调用方静默降级）。"""
    npc = next((n for n in (getattr(world, "npcs", None) or []) if n.id == npc_id), None)
    if npc is None or not getattr(npc, "alive", True):
        return False
    if len([o for o in (getattr(world, "outreaches", None) or []) if o.state == "pending"]) \
            >= _MAX_PENDING:
        return False
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    world.outreaches.append(NPCOutreach(kind="letter", npc_id=str(npc_id),
                                        title=str(title)[:64], text=str(text)[:300],
                                        created_day=day,
                                        expires_day=day + max(1, int(expires_days)),
                                        payload=dict(payload or {})))
    _trim(world)
    return True


def _ev(world, tick, title: str, desc: str, npc_id: str = "", severity: str = "minor") -> WorldEvent:
    return WorldEvent(tick=int(tick or 0), category="npc", severity=severity,
                      title=title, desc=desc, npcs=[npc_id] if npc_id else [])


def _alive_npcs(world) -> list:
    return [n for n in (getattr(world, "npcs", None) or []) if getattr(n, "alive", True)]


def _npc_by_id(world, npc_id: str):
    return next((n for n in (getattr(world, "npcs", None) or []) if n.id == npc_id), None)


def _player_loc_name(world) -> str:
    pid = getattr(getattr(world, "player", None), "location_id", "")
    loc = next((l for l in (getattr(world, "locations", None) or []) if l.id == pid), None)
    return getattr(loc, "name", "") or "此地"


def _pool_for(world, kind: str) -> list[dict[str, str]]:
    ov = getattr(world, "config_overlay", None) or {}
    gid = str(ov.get("attribute_template_id", "") or "") if isinstance(ov, dict) else ""
    pools = _GENRE_OUTREACH_TEMPLATES.get(gid) or _GENRE_OUTREACH_TEMPLATES["western_fantasy"]
    return pools.get(kind) or _GENRE_OUTREACH_TEMPLATES["western_fantasy"][kind]


def _fill(tmpl: dict, npc_name: str, loc_name: str, **kw) -> tuple[str, str]:
    title = str(tmpl.get("title", "")).replace("{npc}", npc_name).replace("{loc}", loc_name)
    text = str(tmpl.get("text", "")).replace("{npc}", npc_name).replace("{loc}", loc_name)
    # 模板占位符 {target}/{item} 对应 payload 的 target_name/item_name（别名映射）
    if "target_name" in kw:
        kw.setdefault("target", kw["target_name"])
    if "item_name" in kw:
        kw.setdefault("item", kw["item_name"])
    for k, v in kw.items():
        title = title.replace("{" + k + "}", str(v))
        text = text.replace("{" + k + "}", str(v))
    return title, text


# ---- 候选挑选（模块级纯函数，供测试直查；全部确定性或走传入 rng）----

def letter_candidate(world, npcs: list, rng: SeededRng):
    """来信人：好友或交情>={_LETTER_AFFINITY_MIN} 的存活 NPC，取交情最高的 3 人再抽。"""
    friend_ids = set(getattr(getattr(world, "player", None), "friend_npc_ids", None) or [])
    pool = [n for n in npcs if n.id in friend_ids
            or int(getattr(n, "affinity", 0) or 0) >= _LETTER_AFFINITY_MIN]
    if not pool:
        return None
    pool.sort(key=lambda n: int(getattr(n, "affinity", 0) or 0), reverse=True)
    return rng.pick(pool[:3])


def bounty_target(world, npcs: list, rng: SeededRng):
    """买凶：sender = 存活非敌对 NPC（随抽），target = 存活敌对 NPC（kill 钩子按名匹配，
    故只挑 world.npcs 内的敌对单位，野怪不接单）。无敌对目标 -> None。"""
    hostiles = [n for n in npcs if getattr(n, "hostile", False)]
    senders = [n for n in npcs if not getattr(n, "hostile", False)]
    if not hostiles or not senders:
        return None
    for _ in range(6):                      # 避免 sender == target，最多试 6 次防死循环
        sender = rng.pick(senders)
        target = rng.pick(hostiles)
        if sender.id != target.id:
            return sender, target
    return None


def duel_candidate(world, npcs: list, rng: SeededRng):
    """约战对象：战斗身份（combat_role 非空非 none）、存活、非敌对、非当前同伴，
    且等级 <= 玩家等级 + {_DUEL_LEVEL_MARGIN}（§24 底线：建议层不送死）。"""
    plevel = int(getattr(getattr(world, "player", None), "level", 1) or 1)
    comp = set(getattr(getattr(world, "player", None), "companion_npc_ids", None) or [])
    pool = [n for n in npcs
            if str(getattr(n, "combat_role", "") or "") not in ("", "none")
            and not getattr(n, "hostile", False)
            and n.id not in comp
            and int(getattr(n, "level", 1) or 1) <= plevel + _DUEL_LEVEL_MARGIN]
    if not pool:
        return None
    return rng.pick(pool)


def _find_heal_item(world):
    """找一件世面上存在的治疗消耗品（heal_pct>0 优先，common 起步最便宜的最先）。"""
    items = [i for i in (getattr(world, "items", None) or [])
             if str(getattr(i, "category", "") or "") == "consumable"
             and isinstance(getattr(i, "consume_effect", None), dict)
             and int((i.consume_effect or {}).get("heal_pct", 0) or 0) > 0]
    if not items:
        return None
    items.sort(key=lambda i: (i.consume_effect.get("heal_pct", 0), getattr(i, "level", 0)))
    return items[0]


def plea_candidate(world, npcs: list, rng: SeededRng):
    """求助人：交情>={_PLEA_AFFINITY_MIN} 的存活熟人，先看缺药的（hp<{_PLEA_HP_PCT}%），
    再看缺钱的（wallet<{_PLEA_WALLET_MAX}）。返回 (npc, payload)。"""
    friends = [n for n in npcs if int(getattr(n, "affinity", 0) or 0) >= _PLEA_AFFINITY_MIN]
    if not friends:
        return None
    low_hp = [n for n in friends
              if int(getattr(n, "hp", 0) or 0) * 100
              < _PLEA_HP_PCT * max(1, int(getattr(n, "hp_max", 1) or 1))]
    if low_hp:
        heal = _find_heal_item(world)
        if heal is not None:
            npc = rng.pick(low_hp)
            return npc, {"item_id": heal.id, "item_name": getattr(heal, "name", "") or "伤药",
                         "qty": 1}
    poor = [n for n in friends
            if int(getattr(n, "wallet", 0) or 0) < _PLEA_WALLET_MAX]
    if poor:
        npc = rng.pick(poor)
        amt = int(rng.roll(30, 80))
        gt = GenreText(getattr(world, "config_overlay", None) or {}).currency
        return npc, {"gold": amt, "_gold_display": f"{amt} {gt}"}
    return None


# ---- 生成（每日水位 + 旱涝保底）----

def maybe_generate(world, rng: SeededRng, tick: int) -> list:
    """每日一次（last_outreach_day 水位）：roll 一条上门 + 旱涝保底。

    [!] 水位先记后产出是**有意**的：确定性 roll 的「今日没中」是合法结果不是故障
    （区别于 event_quests 的 LLM 预算教训——那边失败必须留重试，这边重 roll 才有随机感）。
    保底兜住连续不中的情况，玩家永远在 3 天内被世界惦记一次。
    [!] 候选函数**只调一次**：rng 是有状态的，判定与生成各调一次会消耗两个随机数，
    第二次可能抽空让保底落空（rng 双消费 bug，重构时勿回退）。
    """
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    if int(getattr(world, "last_outreach_day", 0) or 0) >= day:
        return []
    world.last_outreach_day = day
    if len([o for o in (getattr(world, "outreaches", None) or []) if o.state == "pending"]) >= _MAX_PENDING:
        return []

    npcs = _alive_npcs(world)
    pending_kinds = {o.kind for o in world.outreaches if o.state == "pending"}
    last_created = max((int(o.created_day) for o in world.outreaches), default=0)
    drought = (day - last_created >= _DROUGHT_DAYS) and day >= _PITY_DAY

    kind = ""
    npc = None
    payload_extra: dict = {}
    if drought:
        # 保底：按「来信->求助->买凶->约战」挑第一个有候选且未挂着的类别（确定性顺序）
        for k in ("letter", "plea", "bounty", "duel"):
            if k in pending_kinds:
                continue
            if k == "letter":
                c = letter_candidate(world, npcs, rng)
            elif k == "plea":
                c = plea_candidate(world, npcs, rng)
            elif k == "bounty":
                c = bounty_target(world, npcs, rng)
            else:
                c = duel_candidate(world, npcs, rng)
            if c is None:
                continue
            kind = k
            if k == "bounty":
                npc, target = c
                gold = max(40, int(getattr(target, "level", 1) or 1) * 15)
                gt = GenreText(getattr(world, "config_overlay", None) or {}).currency
                payload_extra = {"target_npc_id": target.id,
                                 "target_name": getattr(target, "name", "") or "仇敌",
                                 "gold": gold, "_gold_display": f"{gold} {gt}"}
            elif k == "plea":
                npc, payload_extra = c
            else:
                npc = c
            break
    else:
        # 日常 roll：互斥概率分流（同类别已有 pending 则跳过，防同类刷屏）
        r = rng.random()
        if r < _LETTER_CHANCE:
            kind = "letter"
        elif r < _LETTER_CHANCE + _BOUNTY_CHANCE:
            kind = "bounty"
        elif r < _LETTER_CHANCE + _BOUNTY_CHANCE + _DUEL_CHANCE:
            kind = "duel"
        elif r < _LETTER_CHANCE + _BOUNTY_CHANCE + _DUEL_CHANCE + _PLEA_CHANCE:
            kind = "plea"
        if not kind or kind in pending_kinds:
            return []
        if kind == "letter":
            npc = letter_candidate(world, npcs, rng)
        elif kind == "bounty":
            pair = bounty_target(world, npcs, rng)
            if pair is not None:
                npc, target = pair
                gold = max(40, int(getattr(target, "level", 1) or 1) * 15)
                gt = GenreText(getattr(world, "config_overlay", None) or {}).currency
                payload_extra = {"target_npc_id": target.id,
                                 "target_name": getattr(target, "name", "") or "仇敌",
                                 "gold": gold, "_gold_display": f"{gold} {gt}"}
        elif kind == "duel":
            npc = duel_candidate(world, npcs, rng)
        elif kind == "plea":
            pair = plea_candidate(world, npcs, rng)
            if pair is not None:
                npc, payload_extra = pair

    if not kind or npc is None:
        return []
    return [_spawn(world, rng, kind, npc, tick, payload_extra)]


def _spawn(world, rng: SeededRng, kind: str, npc, tick: int, payload_extra: dict) -> WorldEvent:
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    loc_name = _player_loc_name(world)
    tmpl = rng.pick(_pool_for(world, kind))
    kw = {k: v for k, v in (payload_extra or {}).items() if not k.startswith("_")}
    if "gold" in kw and "_gold_display" in (payload_extra or {}):
        kw["gold"] = payload_extra["_gold_display"]
    if "item" not in kw and "item_name" in kw:
        kw["item"] = kw["item_name"]
    title, text = _fill(tmpl, getattr(npc, "name", "") or "故人", loc_name, **kw)
    o = NPCOutreach(kind=kind, npc_id=npc.id, title=title, text=text,
                    created_day=day, expires_day=day + int(_EXPIRES.get(kind, 2)),
                    payload={k: v for k, v in (payload_extra or {}).items()
                             if not k.startswith("_")})
    world.outreaches.append(o)
    _trim(world)
    return _ev(world, tick, title,
               f"收到来自{getattr(npc, 'name', '') or '故人'}的{_KIND_LABELS.get(kind, '消息')}"
               "——点「信使」查看（过期不候）。",
               npc_id=npc.id)


def _trim(world) -> None:
    """pending 全保 + finished 留最近 _KEEP_FINISHED 条（列表序即时间序）。"""
    pending = [o for o in world.outreaches if o.state == "pending"]
    finished = [o for o in world.outreaches if o.state != "pending"]
    world.outreaches = pending + finished[-_KEEP_FINISHED:]


# ---- 过期不候 ----

def expire_pending(world, tick: int) -> list:
    """跨过 expires_day 的 pending 自动结算（幂等：state 置位后不再二次触发）。"""
    day = max(1, int(getattr(world, "day_count", 1) or 1))
    events: list = []
    for o in (getattr(world, "outreaches", None) or []):
        if o.state != "pending" or day <= int(o.expires_day):
            continue
        o.state = "expired"
        npc = _npc_by_id(world, o.npc_id)
        if npc is not None:
            if o.kind == "letter":
                npc.affinity = max(0, int(getattr(npc, "affinity", 0) or 0) - 1)
            elif o.kind == "duel":
                imp = getattr(npc, "player_impression", None)
                if not isinstance(imp, dict):
                    imp = {}
                    npc.player_impression = imp
                imp["trust"] = max(-100, min(100, int(imp.get("trust", 0) or 0) - 5))
            else:
                npc.affinity = max(0, int(getattr(npc, "affinity", 0) or 0) - 2)
        label = _KIND_LABELS.get(o.kind, "消息")
        sender = getattr(npc, "name", "") if npc is not None else "对方"
        events.append(_ev(world, tick, f"「{label}」过期不候",
                          f"{sender}的{label}没有回音，此事就算了。", npc_id=o.npc_id))
    return events


# ---- 玩家响应 ----

def accept(world, outreach_id: str, tick: int = 0) -> dict:
    """接受/收下一条上门。返回 {action, msg}；missing_* 表示条件不满足（保持 pending）。"""
    o = next((x for x in (getattr(world, "outreaches", None) or []) if x.id == outreach_id), None)
    if o is None or o.state != "pending":
        return {"action": "invalid", "msg": "这条消息已处理或不存在。"}
    npc = _npc_by_id(world, o.npc_id)
    if npc is None or not getattr(npc, "alive", True):
        o.state = "expired"
        return {"action": "gone", "msg": "对方已经不在了，此事作罢。"}
    gt = GenreText(getattr(world, "config_overlay", None) or {}).currency
    player = getattr(world, "player", None)

    if o.kind == "letter":
        o.state = "done"
        npc.affinity = min(100, int(getattr(npc, "affinity", 0) or 0) + 1)
        return {"action": "letter", "msg": f"你收下了{npc.name}的信。{npc.name}会记得你看了这封信。"}

    if o.kind == "bounty":
        tid = str((o.payload or {}).get("target_npc_id", "") or "")
        target = _npc_by_id(world, tid)
        gold = max(40, int((o.payload or {}).get("gold", 0) or 0))
        tname = str((o.payload or {}).get("target_name", "") or "目标")
        if target is None or not getattr(target, "alive", True):
            # 目标已死（死于旁路）：赏金照付，事直接了结（守恒 mint，与任务领奖同源）
            o.state = "done"
            player.gold = int(getattr(player, "gold", 0) or 0) + gold
            return {"action": "done", "msg": f"「{tname}」早已伏诛——{npc.name}如约付了 {gold} {gt}。"}
        o.state = "accepted"
        q = Quest(title=f"悬赏·{tname}",
                  objective=f"击败{tname}（{npc.name} 所托）",
                  giver_npc_id=npc.id, reward_text=f"{gold} {gt}",
                  objectives=[{"type": "kill", "target": tname, "count": 1, "current": 0,
                               "desc": f"击败{tname}"}],
                  rewards={"gold": gold}, status="active",
                  started_at_tick=int(tick or 0), chain="side")
        world.quests.append(q)
        return {"action": "quest",
                "msg": f"悬赏已接——击败「{tname}」后，回{npc.name}那里领 {gold} {gt}（任务日志可见）。"}

    if o.kind == "duel":
        o.state = "accepted"
        return {"action": "duel", "npc_id": npc.id,
                "msg": f"你应下了战书——{npc.name}这就登门。"}

    if o.kind == "plea":
        item_id = str((o.payload or {}).get("item_id", "") or "")
        if item_id:
            qty = max(1, int((o.payload or {}).get("qty", 1) or 1))
            have = sum(1 for x in (player.inventory or []) if x == item_id)
            if have < qty:
                return {"action": "missing_item",
                        "msg": "你背包里没有对方要的东西（可去商店买或野外采集后再来处理）。"}
            for _ in range(qty):
                player.inventory.remove(item_id)
            npc.inventory = list(getattr(npc, "inventory", None) or []) + [item_id]
        else:
            amt = int((o.payload or {}).get("gold", 0) or 0)
            if int(getattr(player, "gold", 0) or 0) < amt:
                return {"action": "missing_gold",
                        "msg": f"你身上的钱不够（差 {amt} {gt}）。"}
            player.gold = int(getattr(player, "gold", 0) or 0) - amt
            npc.wallet = int(getattr(npc, "wallet", 0) or 0) + amt
        npc.affinity = min(100, int(getattr(npc, "affinity", 0) or 0) + 8)
        from src.services import social_engine as _soc
        _soc.update_impression(npc, "慷慨", 3)
        o.state = "done"
        return {"action": "plea", "msg": f"你帮了{npc.name}一把。{npc.name}记下了这份情。"}

    return {"action": "invalid", "msg": "未知类型的消息。"}


def decline(world, outreach_id: str) -> str:
    """拒绝一条上门（信件无需拒绝按钮；其余交情 -2）。"""
    o = next((x for x in (getattr(world, "outreaches", None) or []) if x.id == outreach_id), None)
    if o is None or o.state != "pending":
        return "这条消息已处理或不存在。"
    o.state = "declined"
    npc = _npc_by_id(world, o.npc_id)
    if npc is not None and o.kind != "letter":
        npc.affinity = max(0, int(getattr(npc, "affinity", 0) or 0) - 2)
    return f"你婉拒了{_KIND_LABELS.get(o.kind, '这份邀请')}。"


def settle_duel(world, npc, tick: int = 0) -> dict:
    """[P57] 切磋结算（纯引擎，点到即止非致死）：战力比定胜负。

    [审查修复 2026-09-26] 约战**不走正式战斗系统**：finish_combat won 分支必置
    alive=False（友方切磋会出人命），一击制 _resolve_combat 又拒非敌对目标——两条
    既有通路都不适配「比武」。本函数复用 nle.hunt 的战力口径（等级 x10 + 力 + 耐 +
    装备攻 x0.8 / 防 x0.4，装备走 ce.equipped_attack_defense 单一来源）纯结算胜负。
    返回 {won, hint}；hint 交叙事回合（preset_intent custom）写场面，后果：
    胜=对方交情 +5 + 印象「无畏」；负=交情 +2（输家服气）。零掉落/零夺金/零死亡。
    """
    from src.services import combat_engine as _ce
    from src.services import talent_engine as _te
    from src.services import social_engine as _soc
    rng = SeededRng.seed_from(str(getattr(world, "id", "")),
                              int(getattr(world, "tick_count", 0) or 0),
                              f"duel_{getattr(npc, 'id', '')}")
    player = getattr(world, "player", None)
    p_atk, p_def = _ce.equipped_attack_defense(world, player)
    n_atk, n_def = _ce.equipped_attack_defense(world, npc)
    p_power = (max(1, int(getattr(player, "level", 1) or 1)) * 10
               + _te.effective_stat(player, "str")
               + _te.effective_stat(player, "vit") + p_atk * 0.8 + p_def * 0.4)
    n_power = (max(1, int(getattr(npc, "level", 1) or 1)) * 10
               + _te.effective_stat(npc, "str")
               + _te.effective_stat(npc, "vit") + n_atk * 0.8 + n_def * 0.4)
    chance = max(0.1, min(0.9, 0.5 + 0.5 * (p_power - n_power) / max(1.0, n_power)))
    won = rng.chance(chance)
    if won:
        npc.affinity = min(100, int(getattr(npc, "affinity", 0) or 0) + 5)
        _soc.update_impression(npc, "无畏", 3)
        hint = (f"玩家接受{npc.name}的战书，两人拆了几十招，{npc.name}渐落下风，"
                "收势抱拳认输——点到即止，观者喝彩。")
    else:
        npc.affinity = min(100, int(getattr(npc, "affinity", 0) or 0) + 2)
        hint = (f"玩家接受{npc.name}的战书，几招下来高下立判，{npc.name}点到即止收了手"
                "——胜负分明，无人挂彩。")
    return {"won": won, "hint": hint}
