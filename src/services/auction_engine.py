"""[P34g] 拍卖会事件引擎（纯 Python + SeededRng + 1 次 LLM 出拍品骨架由 world_sim_service 调）。

城市定期举办拍卖会：tick 周期（每 auction_interval_days 天）在随机城市型聚落生成拍卖会 ->
event_log 预告 -> start_auction 生成拍品（identified=False 未鉴定 + 高 level 可超 shop_max_item_level）->
玩家到场竞价（押金模式）-> end_auction 拍出入包/未拍出 sink。

[NPC 竞拍 2026-09-11] 拍卖期间 NPC 同场竞价：全图存活 NPC 都看得见拍品、按自己的 wallet 出价
（可动用 = wallet，买不起不进场），每件每天最多被加价 1 次（+1~2 档）、每个 NPC 每天最多抢
1 件（npc_bidding_round，每日 1 次由 _tick_auctions 驱动）。出价者类别记在 AuctionLot.bidder_kind，
决定押金退还与落槌交割走玩家 gold 还是 NPC wallet。end_auction 追加一条「拍卖落槌」播报。

数值范式铁律：LLM 出拍品骨架（name/type/rarity/level 意向，语义），引擎算起拍价/竞价档位/押金结算/
物品生成（结构）。SeededRng 确定性（同 world+day+salt 同结果）。

[!] 完全独立铁律（§21）：不动单聊/群聊/会话/远程模块；仅 import 复用 World/Location/Item/AuctionEvent/
AuctionLot 模型 + SeededRng + trade_engine.compute_item_price + combat_engine.try_add_to_inventory。
"""
from __future__ import annotations

from src.models.world import World, Item, AuctionEvent, AuctionLot, WorldEvent
from src.utils.rng import SeededRng


# [P34g] 拍卖会拍品题材化兜底池：6 题材各 >=6 拍品（name + type + rarity 权重档）。
# 兜底生成拍品时用（LLM 失败）；每条 {name, type, rarity}，rarity 决定品级档。
# 缺键回退西幻守 §23；登记 test_genre_data_volume.py 基线。
_GENRE_AUCTION_LOT_NAMES: dict[str, list[dict]] = {
    "western_fantasy": [
        {"name": "古王之剑", "type": "weapon", "rarity": "legendary"},
        {"name": "精灵族银甲", "type": "armor", "rarity": "epic"},
        {"name": "龙鳞护符", "type": "accessory", "rarity": "epic"},
        {"name": "失传炼金配方", "type": "consumable", "rarity": "rare"},
        {"name": "矮人精金锭", "type": "material", "rarity": "rare"},
        {"name": "上古战场遗物", "type": "accessory", "rarity": "legendary"},
        {"name": "贵族藏画", "type": "material", "rarity": "uncommon"},
        {"name": "游侠长弓", "type": "weapon", "rarity": "rare"},
        {"name": "龙骨法杖", "type": "weapon", "rarity": "legendary"},
        {"name": "精灵长弓", "type": "weapon", "rarity": "epic"},
    ],
    "xianxia": [
        {"name": "上古残剑", "type": "weapon", "rarity": "legendary"},
        {"name": "陨铁战甲", "type": "armor", "rarity": "epic"},
        {"name": "龙纹玉佩", "type": "accessory", "rarity": "epic"},
        {"name": "九转还魂丹", "type": "consumable", "rarity": "legendary"},
        {"name": "万年灵髓", "type": "material", "rarity": "rare"},
        {"name": "古修士洞府图", "type": "material", "rarity": "rare"},
        {"name": "青玉飞剑", "type": "weapon", "rarity": "rare"},
        {"name": "妖兽内丹", "type": "material", "rarity": "epic"},
        {"name": "诛仙剑胚", "type": "weapon", "rarity": "legendary"},
        {"name": "护山大阵图", "type": "material", "rarity": "epic"},
    ],
    "wuxia": [
        {"name": "倚天残锋", "type": "weapon", "rarity": "legendary"},
        {"name": "软猬甲", "type": "armor", "rarity": "epic"},
        {"name": "辟邪玉坠", "type": "accessory", "rarity": "epic"},
        {"name": "大还丹", "type": "consumable", "rarity": "legendary"},
        {"name": "千年雪莲", "type": "material", "rarity": "rare"},
        {"name": "前朝名画", "type": "material", "rarity": "rare"},
        {"name": "青锋剑", "type": "weapon", "rarity": "rare"},
        {"name": "古武秘籍残页", "type": "consumable", "rarity": "epic"},
        {"name": "绝世剑谱", "type": "consumable", "rarity": "legendary"},
        {"name": "千年灵芝", "type": "material", "rarity": "epic"},
    ],
    "modern": [
        {"name": "传世名画", "type": "material", "rarity": "legendary"},
        {"name": "古董怀表", "type": "accessory", "rarity": "epic"},
        {"name": "限量版名表", "type": "accessory", "rarity": "epic"},
        {"name": "老字号秘方", "type": "consumable", "rarity": "rare"},
        {"name": "稀有翡翠", "type": "material", "rarity": "rare"},
        {"name": "传家宝剑", "type": "weapon", "rarity": "rare"},
        {"name": "战地勋章", "type": "accessory", "rarity": "uncommon"},
        {"name": "名厂红酒", "type": "consumable", "rarity": "uncommon"},
        {"name": "皇室王冠", "type": "accessory", "rarity": "legendary"},
        {"name": "绝版跑车", "type": "material", "rarity": "epic"},
    ],
    "scifi": [
        {"name": "外星遗物", "type": "accessory", "rarity": "legendary"},
        {"name": "曲率引擎核心", "type": "material", "rarity": "legendary"},
        {"name": "能量合金甲", "type": "armor", "rarity": "epic"},
        {"name": "纳米修复剂", "type": "consumable", "rarity": "rare"},
        {"name": "稀有矿石样本", "type": "material", "rarity": "rare"},
        {"name": "脉冲手枪", "type": "weapon", "rarity": "epic"},
        {"name": "古地球文物", "type": "material", "rarity": "uncommon"},
        {"name": "数据晶片", "type": "consumable", "rarity": "uncommon"},
        {"name": "星图坐标", "type": "material", "rarity": "legendary"},
        {"name": "量子计算机", "type": "material", "rarity": "epic"},
    ],
    "apocalypse": [
        {"name": "战前名表", "type": "accessory", "rarity": "legendary"},
        {"name": "军用外骨骼", "type": "armor", "rarity": "epic"},
        {"name": "高浓缩能量块", "type": "material", "rarity": "legendary"},
        {"name": "疫苗原液", "type": "consumable", "rarity": "epic"},
        {"name": "精制枪械", "type": "weapon", "rarity": "rare"},
        {"name": "变异兽晶核", "type": "material", "rarity": "rare"},
        {"name": "战前名酒", "type": "consumable", "rarity": "uncommon"},
        {"name": "防辐射药剂", "type": "consumable", "rarity": "rare"},
        {"name": "军用卫星终端", "type": "material", "rarity": "legendary"},
        {"name": "战前药库钥匙", "type": "consumable", "rarity": "epic"},
    ],
}


def _auction_lot_pool(genre_id: str) -> list[dict]:
    """[P34g] 取题材拍卖品兜底池（缺题材回退西幻）。返回 list 不被外部修改。"""
    return _GENRE_AUCTION_LOT_NAMES.get(genre_id) or _GENRE_AUCTION_LOT_NAMES["western_fantasy"]


def _genre_id(world: World) -> str:
    """读世界题材 id（config_overlay.attribute_template_id，兜底 western_fantasy）。"""
    ov = getattr(world, "config_overlay", None) or {}
    return str(ov.get("attribute_template_id", "western_fantasy") or "western_fantasy")


def _city_locations(world: World) -> list:
    """城市型聚落（优先 city，无 city 回退 town；返回 Location 列表）。"""
    cities = [l for l in getattr(world, "locations", []) or []
              if getattr(l, "kind", "") == "settlement"
              and getattr(l, "settlement_size", "") == "city"]
    if cities:
        return cities
    return [l for l in getattr(world, "locations", []) or []
            if getattr(l, "kind", "") == "settlement"
            and getattr(l, "settlement_size", "") == "town"]


def active_auction_at(world: World, loc_id: str):
    """[P34g] 取某地点正在进行的拍卖会（无则 None）。UI 入口 gate 用。"""
    for a in getattr(world, "auctions", None) or []:
        if isinstance(a, AuctionEvent) and a.status == "active" \
                and a.city_location_id == loc_id:
            return a
    return None


def maybe_spawn_auction(world: World, rng: SeededRng, interval_days: int,
                        duration_days: int) -> list:
    """[P34g] 周期生成新拍卖会：last_auction_spawn_day + interval_days 到期 + 选随机城市。

    - 选随机 city（无 city 回退 town；无任何聚落跳过，返回 []）。
    - 建 AuctionEvent status=upcoming + start_day=day_count+1 + end_day=start_day+duration_days。
    - 更新 world.last_auction_spawn_day = day_count（无论是否选到城——避免每 tick 重复 roll）。
    - 返回 [WorldEvent 预告]。
    """
    events = []
    day = int(getattr(world, "day_count", 1) or 1)
    interval = max(1, int(interval_days or 0))
    if day < int(getattr(world, "last_auction_spawn_day", 0) or 0) + interval:
        return events  # 未到期
    world.last_auction_spawn_day = day
    cities = _city_locations(world)
    if not cities:
        return events
    city = rng.pick(cities)
    if city is None:
        return events
    dur = max(1, int(duration_days or 0))
    start_day = day + 1
    auction = AuctionEvent(
        city_location_id=city.id,
        start_day=start_day,
        end_day=start_day + dur,
        status="upcoming",
    )
    world.auctions.append(auction)
    events.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="economy",
        severity="minor", title="拍卖会预告",
        desc=f"传闻：「{city.name}」将于近日举办拍卖会，珍奇拍品待价而沽。",
        locations=[city.id],
    ))
    return events


def start_auction(world: World, auction: AuctionEvent, lot_count: int,
                  llm_skeletons=None, item_max_level: int = 6,
                  preset=None) -> None:
    """[P34g] 拍卖会开场：生成拍品 Item（identified=False 未鉴定）+ 挂 AuctionLot。

    - 拍品骨架来源：llm_skeletons（LLM 出的 [{name,type,rarity,level,desc}]）优先，缺/非法走
      题材兜底池确定性生成（SeededRng 锚 world.id + auction.id）。
    - 每件拍品：派生 id `{auction_id}__auc{i}`（与 __unid/__craft/__rarity/__idn/__rr 隔离不撞）
      + identified=False + level 钳 item_max_level（可超 shop_max_item_level）。
    - start_price=compute_item_price（level/rarity 公式）；bid_step=start_price//5 阶梯（>=1）。
    - 拍品 Item 入 world.items（临时生成，只被拍卖持有，end_auction 未拍出时移除 sink）。
    """
    from src.services import trade_engine as tre
    auction.status = "active"
    count = max(1, int(lot_count or 0))
    gid = _genre_id(world)
    pool = _auction_lot_pool(gid)
    rng = SeededRng.seed_from(getattr(world, "id", ""),
                              int(getattr(world, "day_count", 0) or 0), f"auc_{auction.id}")
    # 规范化 LLM 骨架（白名单 type/rarity + name 非空）
    skeletons = []
    if isinstance(llm_skeletons, list):
        for sk in llm_skeletons:
            if not isinstance(sk, dict):
                continue
            nm = str(sk.get("name") or "").strip()
            if not nm:
                continue
            t = str(sk.get("type") or "material").strip()
            if t not in ("weapon", "armor", "consumable", "material", "accessory"):
                t = "material"
            r = str(sk.get("rarity") or "common").strip()
            if r not in ("common", "uncommon", "rare", "epic", "legendary", "mythic"):
                r = "common"
            lv = int(sk.get("level") or 0) if isinstance(sk.get("level"), (int, float)) else 0
            lv = max(0, min(int(item_max_level), lv))  # [!] 0=不启用 level 体系（凡品世界），勿 or 10 吞 0
            skeletons.append({"name": nm, "type": t, "rarity": r,
                              "level": lv, "desc": str(sk.get("desc") or "")})
    # 拍品生成
    for i in range(count):
        if i < len(skeletons):
            sk = skeletons[i]
            name = sk["name"]
            t, r, lv, desc = sk["type"], sk["rarity"], sk["level"], sk["desc"]
        else:
            # 兜底池确定性选（倾向 epic/legendary 压轴）
            entry = rng.pick(pool)
            name = entry["name"]
            t, r = entry["type"], entry["rarity"]
            lv = max(0, min(int(item_max_level), _rarity_level(r) + rng.roll(0, 2)))  # [!] 0=凡品世界勿吞
            desc = "拍卖会珍品，来历不凡。"
        # 去重名（同名加序号）
        existing_names = {it.name for it in world.items}
        base = name
        if base in existing_names:
            n = 2
            while f"{base}·{n}" in existing_names:
                n += 1
            name = f"{base}·{n}"
        item_id = f"{auction.id}__auc{i}"
        it = Item(id=item_id, name=name, type=t, rarity=r, desc=desc,
                  level=lv, identified=False)
        # [修 2026-10-01 真机审计] 拍品数值兜底：此前 LLM 骨架与兜底池两条路径都不带
        # 攻/防/加成（武侠档实测 epic 软猬甲防 0——买了装备等于没装）。走世界生成同
        # 一口径 _fill_item_defaults（幂等，0 值才补）；须在 compute_item_price 前跑。
        # preset 透传 item_max_level（凡品世界=0 时 level 兜底不得把 0 抬成品阶档；
        # 调用方不传 preset 时按本函数的 item_max_level 参数造等价 shim）。
        # 懒导入防环（world_sim_service 模块级导入本模块）。
        from types import SimpleNamespace as _NS
        from src.services.world_sim_service import _fill_item_defaults as _fill_items
        _fill_items(world, [it],
                    preset if preset is not None else _NS(item_max_level=item_max_level))
        world.items.append(it)
        # 起拍价 + 竞价档位
        start_price = max(1, tre.compute_item_price(it))
        lot = AuctionLot(item_id=item_id, start_price=start_price,
                         current_bid=start_price, bid_step=max(1, start_price // 5),
                         min_level=lv, sold=False, current_bidder="", bidder_kind="player")
        auction.lots.append(lot)


def _rarity_level(rarity: str) -> int:
    return {"common": 1, "uncommon": 2, "rare": 3, "epic": 4, "legendary": 5, "mythic": 6}.get(rarity, 1)


# [NPC 竞拍 2026-09-11] 出价者类别 -> 钱字段名（玩家 gold / NPC wallet 不同名，别混用）。
_MONEY_ATTR = {"player": "gold", "npc": "wallet"}
# 落槌播报明细条数上限（超出补「等 N 件」；单条事件不撞 max_events_per_tick，但右侧动态栏
# desc 截断 60 字，再多也显示不出来）。
_WIN_LOG_DETAIL = 3


def _money(entity, kind: str) -> int:
    """[NPC 竞拍 2026-09-11] 读出价者的钱（玩家 gold / NPC wallet）。"""
    return max(0, int(getattr(entity, _MONEY_ATTR.get(kind, "wallet"), 0) or 0))


def _set_money(entity, kind: str, value: int) -> None:
    """[NPC 竞拍 2026-09-11] 写出价者的钱（钳非负）。"""
    try:
        setattr(entity, _MONEY_ATTR.get(kind, "wallet"), max(0, int(value)))
    except Exception:  # noqa: BLE001 - 脏实体（无该字段/只读）不该炸掉整场结算
        pass


def _find_npc(world: World, npc_id: str):
    """按 id 找存活 NPC（找不到/已死都返回 None，调用方按 None 处理）。

    [NPC 竞拍 2026-09-12] 死亡态是 alive=False 且仍留在 world.npcs（NPC.alive 注释明言
    「不删实体」，cleanup_and_respawn 复活同一对象）——不查 alive 会把尸体当活人：拍品
    交割进尸包、押金退进尸体钱包、播报死人中标。
    """
    nid = str(npc_id or "")
    if not nid:
        return None
    for n in (getattr(world, "npcs", None) or []):
        if str(getattr(n, "id", "") or "") == nid and getattr(n, "alive", False):
            return n
    return None


def _bidder_id(entity, kind: str) -> str:
    """出价者 -> 落 lot.current_bidder 的 id（玩家无 id 时落 "player"，与旧版一致）。"""
    eid = getattr(entity, "id", None)
    if kind == "npc":
        return str(eid or "")
    return str(eid) if eid else "player"


def resolve_bidder(world: World, lot: AuctionLot):
    """[NPC 竞拍 2026-09-11] 解出当前最高出价者 -> (entity, kind)；无人出价 -> (None, "")。

    [!] kind 决定押金退还与落槌交割走 gold 还是 wallet。NPC 已死/不在 world.npcs 时返回
    (None, "npc")——押金无法退回（sink），物品流拍，绝不许落到玩家头上。
    """
    if not str(getattr(lot, "current_bidder", "") or ""):
        return None, ""
    kind = str(getattr(lot, "bidder_kind", "player") or "player")
    if kind not in _MONEY_ATTR:
        kind = "player"
    if kind == "player":
        return getattr(world, "player", None), "player"
    return _find_npc(world, getattr(lot, "current_bidder", "")), "npc"


def leader_name(world: World, lot: AuctionLot) -> tuple:
    """[NPC 竞拍 2026-09-11] 当前领先者 -> (kind, 显示名)；无人出价 -> ("", "")。UI 三态用。"""
    entity, kind = resolve_bidder(world, lot)    # [!] 返回序是 (entity, kind)
    if not kind:
        return "", ""
    if kind == "player":
        return "player", "你"
    return "npc", str(getattr(entity, "name", "") or "某位买家")


def _npc_inv_add(npc, item_id: str) -> bool:
    """[NPC 竞拍 2026-09-11] NPC 口径入包（去重 + npc_life_engine._NPC_INV_CAP）——单一出口走
    npc_life_engine._inv_add，勿在此另抄一份规则（两侧分叉后会静默不一致）。

    [!] 绝不能改用 combat_engine.try_add_to_inventory（玩家口径 20+vit*2）——会把 NPC 包撑破
    cap（[P45 2026-09-12] 8 -> 16），此后该 NPC 的采集/进货/互市/受赠全部因 _inv_add
    恒 False 停摆。容量常量勿在此硬编码，一律引用 nle 侧单一来源。
    """
    from src.services import npc_life_engine as nle
    try:
        return bool(nle._inv_add(npc, item_id))
    except Exception:  # noqa: BLE001 - 入包规则不可用时退回「不入包」（调用方会退款+流拍）
        return False


def place_bid(world: World, auction: AuctionEvent, lot: AuctionLot,
              amount: int, bidder=None, bidder_kind: str = "player") -> tuple[bool, str]:
    """[P34g] 竞价（押金模式）：出价即扣，被超出退还上一竞拍者。

    - amount >= current_bid + bid_step 才有效；出价者的钱 >= amount。
    - 扣出价者押金；若已有领先者，按其 bidder_kind 退还上一笔押金（玩家退 gold / NPC 退 wallet）。
    - 更新 lot.current_bid / lot.current_bidder / lot.bidder_kind。
    - bidder=None 视为玩家（world.player）——UI 现有调用不变。

    [NPC 竞拍 2026-09-11] 上一竞拍者可能是 NPC：押金必须退还给 NPC 自己，绝不能进玩家口袋。
    """
    amt = int(amount)
    if amt <= 0:
        return False, "出价须为正整数。"
    min_bid = int(lot.current_bid) + max(1, int(lot.bid_step))  # 与 npc_bidding_round 同口径
    if amt < min_bid:
        return False, f"出价须不低于 {min_bid}（当前 {lot.current_bid} + 档 {lot.bid_step}）。"
    kind = bidder_kind if bidder_kind in _MONEY_ATTR else "player"
    if kind == "npc":
        entity = bidder      # [!] NPC 出价必须显式传 bidder，否则会拿玩家对象去读 wallet
    else:
        entity = bidder if bidder is not None else getattr(world, "player", None)
    if entity is None:
        return False, "无出价者。"
    prev_entity, prev_kind = resolve_bidder(world, lot)
    # [!] 余额校验须计入「退回自己上一笔押金」——已押 1000 现金剩 200 时出 1100 应被允许
    self_outbid = prev_entity is not None and prev_entity is entity
    refund = int(lot.current_bid) if (self_outbid and int(lot.current_bid) > 0) else 0
    if _money(entity, kind) + refund < amt:
        from src.models.world_sim_preset import GenreText as _GT
        _cur = _GT(getattr(world, "config_overlay", None) or {}).currency
        return False, f"{_cur}不足（需 {amt}）。"
    # 退还上一竞拍者押金（[NPC 竞拍 2026-09-11] 按 kind 路由；自我加价时不重复退）
    if prev_entity is not None and int(lot.current_bid) > 0 and not self_outbid:
        _set_money(prev_entity, prev_kind,
                   _money(prev_entity, prev_kind) + int(lot.current_bid))
    _set_money(entity, kind, _money(entity, kind) + refund - amt)
    lot.current_bidder = _bidder_id(entity, kind)
    lot.bidder_kind = kind
    lot.current_bid = amt
    from src.models.world_sim_preset import GenreText as _GT
    return True, f"出价 {amt} {_GT(getattr(world, 'config_overlay', None) or {}).currency}，当前领先。"


def npc_bidding_round(world: World, auction: AuctionEvent, rng: SeededRng) -> int:
    """[NPC 竞拍 2026-09-11] 拍卖会每日一轮 NPC 竞价（纯引擎、零 LLM、确定性）。

    - 全图存活 NPC 都看得见拍品，按自己的 wallet 出价（可动用 = wallet，买不起就不进场）。
    - 每件未成交拍品每天最多被加价 1 次（+1~2 档）；每个 NPC 每天最多抢 1 件。
    - 不自我加价（已是该件领先者的 NPC 跳过）。
    - [!] 竞价过程不产生事件——每天 N 件会撞 max_events_per_tick（默认 3）；只在
      end_auction 落槌时统一播报。

    返回本轮加价次数。
    """
    if str(getattr(auction, "status", "") or "") != "active":
        return 0
    pool = [n for n in (getattr(world, "npcs", None) or [])
            if getattr(n, "alive", False) and int(getattr(n, "wallet", 0) or 0) > 0]
    if not pool:
        return 0
    bid_today = set()
    raised = 0
    for lot in list(getattr(auction, "lots", None) or []):
        if not isinstance(lot, AuctionLot) or lot.sold:
            continue
        step = max(1, int(lot.bid_step))
        min_bid = int(lot.current_bid) + step
        leader_is_npc = (str(getattr(lot, "bidder_kind", "player") or "player") == "npc")
        leader_id = str(getattr(lot, "current_bidder", "") or "")
        cands = []
        for n in pool:
            nid = str(getattr(n, "id", "") or "")
            if not nid or nid in bid_today:
                continue
            if leader_is_npc and nid == leader_id:
                continue  # 自己已领先，不自我加价
            if int(getattr(n, "wallet", 0) or 0) < min_bid:
                continue
            cands.append(n)
        if not cands:
            continue
        bidder = rng.pick(cands)
        if bidder is None:
            continue
        amt = int(lot.current_bid) + step * int(rng.roll(1, 2))
        avail = int(getattr(bidder, "wallet", 0) or 0)
        if amt > avail:
            amt = avail
        if amt < min_bid:
            continue
        ok, _msg = place_bid(world, auction, lot, amt, bidder=bidder, bidder_kind="npc")
        if not ok:
            continue
        bid_today.add(str(getattr(bidder, "id", "") or ""))
        raised += 1
    return raised


def end_auction(world: World, auction: AuctionEvent) -> list:
    """[P34g] 拍卖会结束：拍出交割 / 未拍出 sink / 结算。

    - 有 current_bidder 的 lot 视为拍出，按 bidder_kind 交割（[NPC 竞拍 2026-09-11]）：
      玩家走 try_add_to_inventory（背包满退 gold），NPC 走 _npc_inv_add（`npc_life_engine._NPC_INV_CAP`，
      包满退 wallet）；得主是已死/不存在的 NPC -> 流拍，物品 sink（押金同样退不回去）。
    - 未拍出的 lot 物品从 world.items 移除（filter-reassign sink；拍品临时生成只被拍卖持有）。
    - status=ended + event_log 结算（落幕 + 落槌播报一条）。
    返回 [WorldEvent 结算]。
    """
    from src.services.combat_engine import try_add_to_inventory
    events = []
    wins = []          # [(得主显示名, 成交价, 物品名, npc_id 或 "")]
    unsold_ids = []
    for lot in auction.lots:
        if not isinstance(lot, AuctionLot):
            continue
        if not (lot.current_bidder and lot.sold is False):
            unsold_ids.append(lot.item_id)
            continue
        winner, kind = resolve_bidder(world, lot)
        delivered = False
        if winner is not None and kind == "npc":
            if _npc_inv_add(winner, lot.item_id):
                delivered = True
            else:
                # NPC 背包满：退成交价（物品 sink）
                _set_money(winner, "npc", _money(winner, "npc") + int(lot.current_bid))
        elif winner is not None and kind == "player":
            if try_add_to_inventory(winner, lot.item_id):
                delivered = True
            else:
                # 背包满：退成交价（物品 sink）
                _set_money(winner, "player", _money(winner, "player") + int(lot.current_bid))
        if delivered:
            lot.sold = True
            who = "你" if kind == "player" else str(getattr(winner, "name", "") or "某位买家")
            wins.append((who, int(lot.current_bid), _item_name(world, lot.item_id),
                         "" if kind == "player" else str(getattr(winner, "id", "") or "")))
        else:
            lot.sold = False
            unsold_ids.append(lot.item_id)
    # 未拍出物品 sink（临时生成只被拍卖持有，移除安全）
    if unsold_ids:
        world.items = [i for i in world.items if getattr(i, "id", "") not in set(unsold_ids)]
    auction.status = "ended"
    city = next((l for l in world.locations if l.id == auction.city_location_id), None)
    city_name = city.name if city else "某城"
    won = len([lot for lot in auction.lots if isinstance(lot, AuctionLot) and lot.sold])
    events.append(WorldEvent(
        tick=int(getattr(world, "tick_count", 0) or 0), category="economy",
        severity="minor", title="拍卖会落幕",
        desc=f"「{city_name}」拍卖会结束，成交 {won} 件拍品。",
        locations=[auction.city_location_id],
    ))
    # [NPC 竞拍 2026-09-11] 落槌播报：一场 1 条汇总（明细最多 3 条 + 「等 N 件」）。
    # [!] category="npc" 才会进右侧「NPC 动态」栏（_npc_log_entries 只收 npc/faction_war）；
    # 走 economy 那里不显示、连红点都不亮。
    if wins:
        from src.models.world_sim_preset import GenreText as _GT
        _cur = _GT(getattr(world, "config_overlay", None) or {}).currency
        parts = [f"「{w[0]}」在「{city_name}」以 {w[1]} {_cur}拍得「{w[2]}」"
                 for w in wins[:_WIN_LOG_DETAIL]]
        desc = "；".join(parts)
        if len(wins) > _WIN_LOG_DETAIL:
            desc += f"；等 {len(wins) - _WIN_LOG_DETAIL} 件"
        events.append(WorldEvent(
            tick=int(getattr(world, "tick_count", 0) or 0), category="npc",
            severity="minor", title="拍卖落槌", desc=desc + "。",
            npcs=[w[3] for w in wins if w[3]],
            locations=[auction.city_location_id],
        ))
    return events


def _item_name(world: World, item_id: str) -> str:
    """拍品显示名（找不到物品时兜底「珍品」）。"""
    for it in (getattr(world, "items", None) or []):
        if getattr(it, "id", "") == item_id:
            return str(getattr(it, "name", "") or "珍品")
    return "珍品"
