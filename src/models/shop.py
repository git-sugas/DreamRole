"""[P6] 世界模拟商店数据模型（独立于 Item/NPC，随 World JSON 一起序列化）。

需求来源：read.md §22 Phase B「活世界 + 代码交易」。商售 NPC 各有差异化库存，
本地定时器 + tick 双轨补货（LLM 重新生成商品），交易全程纯 Python 算（不经 LLM）。

设计：
- `ShopStockEntry`：单条货架记录（item_id 引用 world.items 目录 + 单价 + 库存）。
- `Shop`：一个商店（绑商售 NPC + 地点 + shop_type 题材类型 + 货架列表 + 买卖价格倍率 +
  last_restock_tick/restock_interval 补货节流）。
- 入 World.shops（一世界多商店），随 save_world 原子写。交易/tick/LLM 重生成三种路径都改它。
- 全 dataclass + to_dict/from_dict + 强制类型 + or 兜底 + 枚举白名单（守 §11），
  老 JSON 缺字段自动补默认（向后兼容）。

底层字段不变铁律：货币题材化（金币/灵石/信用点）只改显示，底层仍是 PlayerState.gold；
shop_type 是内部 key（general/weapon/...），题材显示名经 GenreText.shop_type() 转。
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


# 商店类型内部 key 白名单（与 world_sim_preset._SHOP_TYPE_KEYS 一致）。
# shop_type 决定 LLM 备货题材倾向 + GenreText 题材显示名（百宝阁/法器阁/枪械店...）。
_SHOP_TYPE_VALUES = ("general", "weapon", "armor", "alchemy", "consumable", "material", "magic", "auction")


@dataclass
class ShopStockEntry:
    """单条货架记录。

    item_id: 指向 world.items 目录里的 Item.id（目录作「蓝图池」，补货从中挑选）。
    price:   单价（>0 用此固定价；0 = 交易时按 trade_engine.compute_item_price 公式算）。
    stock:   当前库存（-1 = 无限；0 = 售罄待补）。
    max_stock: 库存上限（确定性补货向此靠拢；-1 = 无限不补）。
    tag:     条目标记（[P36b] "npc_made" = NPC 手制货；空 = 常规条目。
             [定版裁剪 2026-09-05] "festival" 节日货架随节日系统移除）。
    """
    item_id: str = ""
    price: int = 0
    stock: int = 0
    max_stock: int = 0
    tag: str = ""

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id, "price": self.price,
            "stock": self.stock, "max_stock": self.max_stock,
            "tag": self.tag,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShopStockEntry":
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
            item_id=d.get("item_id", "") or "",
            price=max(0, _i("price", 0)),
            stock=_i("stock", 0),          # -1 允许（无限），不钳
            max_stock=_i("max_stock", 0),  # -1 允许（无限）
            tag=str(d.get("tag", "") or ""),
        )


@dataclass
class Shop:
    """一个商店。

    merchant_npc_id: 绑定的商售 NPC（NPC.is_merchant=True，NPC.shop_id 反向指向本店）。
    location_id:     商店所在地点（= merchant.location_id，方便按地点过滤在场商店）。
    shop_type:       商店类型 key（_SHOP_TYPE_VALUES），决定 LLM 备货题材 + GenreText 显示名。
    stock:           货架列表 list[ShopStockEntry]。
    buy_price_mult:  玩家买入价 = base * buy_price_mult（默认 1.0；可 >1 表示黑店加价）。
    sell_price_mult: 玩家卖出价 = base * sell_price_mult（默认 0.5；玩家卖出永远打折）。
    last_restock_tick: 上次补货（确定性 or LLM）的世界回合号。
    restock_interval:  补货间隔（tick）；current_tick - last_restock >= interval 则触发补货。
    name: 商店显示名（如「百宝阁」「老张丹药铺」），LLM 生成或引擎据 shop_type 题材兜底。
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    merchant_npc_id: str = ""
    location_id: str = ""
    place_id: str = ""                # [修 2026-09-06] 绑定场所 id（_ensure_shop_places 配对；空=未绑定）
    shop_type: str = "general"        # 白名单 _SHOP_TYPE_VALUES
    stock: list[ShopStockEntry] = field(default_factory=list)
    buy_price_mult: float = 1.0
    sell_price_mult: float = 0.5
    last_restock_tick: int = 0
    restock_interval: int = 5         # tick
    name: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "merchant_npc_id": self.merchant_npc_id,
            "location_id": self.location_id,
            "place_id": self.place_id,
            "shop_type": self.shop_type,
            "stock": [e.to_dict() for e in self.stock],
            "buy_price_mult": self.buy_price_mult,
            "sell_price_mult": self.sell_price_mult,
            "last_restock_tick": self.last_restock_tick,
            "restock_interval": self.restock_interval,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Shop":
        if not d or not isinstance(d, dict):
            return cls()
        st = d.get("shop_type", "general") or "general"
        if st not in _SHOP_TYPE_VALUES:
            st = "general"

        def _i(key: str, default: int) -> int:
            v = d.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        def _f(key: str, default: float) -> float:
            v = d.get(key)
            if v is None:
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        stock_raw = d.get("stock") or []
        stock = [ShopStockEntry.from_dict(e) for e in stock_raw if isinstance(e, dict)]
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            merchant_npc_id=d.get("merchant_npc_id", "") or "",
            location_id=d.get("location_id", "") or "",
            place_id=str(d.get("place_id", "") or ""),
            shop_type=st,
            stock=stock,
            buy_price_mult=max(0.1, _f("buy_price_mult", 1.0)),
            sell_price_mult=max(0.0, min(1.0, _f("sell_price_mult", 0.5))),
            last_restock_tick=max(0, _i("last_restock_tick", 0)),
            restock_interval=max(1, _i("restock_interval", 5)),
            name=d.get("name", "") or "",
            created_at=d.get("created_at", _now()),
            updated_at=d.get("updated_at", _now()),
        )

    def touch(self):
        self.updated_at = _now()

    def find_entry(self, item_id: str) -> ShopStockEntry | None:
        """按 item_id 取货架记录（无则 None）。"""
        for e in self.stock:
            if e.item_id == item_id:
                return e
        return None
