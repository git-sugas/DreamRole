"""D02 秘境一次性工具：题材目录、库存筛选与杂货/材料店的可获取渠道。"""
from __future__ import annotations

from src.models.world import Item
from src.models.shop import ShopStockEntry

MECHANISM_TOOL = "dungeon_mechanism"
SEARCH_TOOL = "dungeon_search"
TOOL_KINDS = (MECHANISM_TOOL, SEARCH_TOOL)

# 每题材每用途三种，实物是耗材包/符纸/试剂，使用一件耗掉一份。
# (名称, 具体用途)；前三种用于机关/陷阱，后三种用于宝藏暗格。
_GENRE_TOOLS = {
    "xianxia": (
        ("破阵符", "焚符扰乱阵眼"), ("解禁砂", "散砂中和禁制"), ("定机灵蜡", "填蜡固定机关枢纽"),
        ("显痕灵粉", "撒粉显出暗格接缝"), ("探宝符", "燃符探查箱壁夹层"), ("照隙灵液", "涂液照亮隐藏纹路")),
    "wuxia": (
        ("断簧楔包", "折楔锁住机关弹簧"), ("卸锁药膏", "涂膏松开锈死锁舌"), ("封针棉包", "填棉封住暗器射孔"),
        ("显缝石粉", "撒粉查找箱壁缝隙"), ("探隙薄纸", "贴纸辨别暗格空腔"), ("照纹油膏", "涂膏显出隐藏刻痕")),
    "modern": (
        ("锁芯溶解剂", "喷剂松解卡死锁芯"), ("断路熔断片", "烧断机关供电支路"), ("安全填塞胶", "注胶固定活动构件"),
        ("接缝显影剂", "喷剂显出夹层接缝"), ("空腔试纸", "贴纸检测箱体空腔"), ("微痕采样膜", "揭膜查验隐藏开口")),
    "western_fantasy": (
        ("解锁酸囊", "挤破酸囊腐蚀锁舌"), ("封簧蜡块", "熔蜡固定机关弹簧"), ("破咒符纸", "焚纸扰乱机关符印"),
        ("显迹银粉", "撒粉显出暗格痕迹"), ("寻隙符纸", "焚符探查箱壁夹层"), ("透纹药水", "涂药显出隐藏纹路")),
    "scifi": (
        ("熔断探针", "烧断机关控制线路"), ("一次性解锁芯片", "载入短效锁控破解程序"), ("执行器封堵胶", "注胶卡住机关执行器"),
        ("结构显影喷雾", "喷雾显出隐蔽隔层"), ("空腔感应贴", "贴片检测箱壁空腔"), ("隐口扫描膜", "贴膜扫描隐藏开口")),
    "apocalypse": (
        ("断线熔丝包", "烧断残存控制回路"), ("锁舌腐蚀液", "滴液腐蚀锈锁锁舌"), ("弹簧封堵泥", "填泥固定危险弹簧"),
        ("荧光寻缝粉", "撒粉追踪暗格接缝"), ("空腔探测贴", "贴片辨别箱壁空腔"), ("旧痕显影液", "涂液显出被磨去的开口标记")),
}


def init_catalog(world) -> list[Item]:
    """世界生成时确定性建目录；重跑不重复建蓝图，不凭空给玩家工具。"""
    gid = (world.config_overlay or {}).get("attribute_template_id", "western_fantasy")
    if gid not in _GENRE_TOOLS:
        gid = "western_fantasy"
    by_id = {it.id: it for it in world.items}
    used_names = {it.name for it in world.items}
    out = []
    for index, (name, usage) in enumerate(_GENRE_TOOLS[gid]):
        iid = f"dtool_{gid}_{index}"
        it = by_id.get(iid)
        if it is None:
            if name in used_names:
                base_name = f"{name}（秘境耗材）"
                name = base_name
                serial = 2
                while name in used_names:
                    name = f"{base_name}{serial}"
                    serial += 1
            kind = MECHANISM_TOOL if index < 3 else SEARCH_TOOL
            purpose = "机关破解/陷阱解除" if index < 3 else "宝藏暗格检查"
            it = Item(id=iid, name=name, type="material", rarity="common", level=1,
                      identified=True, base_price=40, tool_for=kind,
                      desc=f"{usage}。用于秘境{purpose}，消耗一件，检定基础成功率 +5 个百分点（再计天赋与上限）。")
            world.items.append(it)
            used_names.add(name)
        out.append(it)
    return out


def available_tools(world, kind: str) -> list[Item]:
    inv = set(world.player.inventory or [])
    return sorted((it for it in world.items if it.id in inv and it.tool_for == kind),
                  key=lambda it: it.id)


def stock_tools(world, shop, *, max_level: int = 3) -> int:
    """普通材料/杂货店备货收尾：每用途至少一个实际库存条目，不额外补已售罄库存。"""
    if shop.shop_type not in ("material", "general"):
        return 0
    stock_ids = {entry.item_id for entry in shop.stock}
    count = 0
    for kind in TOOL_KINDS:
        pool = sorted((it for it in world.items if it.tool_for == kind
                       and it.rarity == "common" and (max_level <= 0 or it.level <= max_level)),
                      key=lambda it: it.id)
        if pool and not any(it.id in stock_ids for it in pool):
            shop.stock.append(ShopStockEntry(item_id=pool[0].id, price=0, stock=3, max_stock=3))
            stock_ids.add(pool[0].id)
            count += 1
    return count
