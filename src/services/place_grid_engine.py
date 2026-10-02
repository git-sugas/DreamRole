"""[P50 地点网格 2026-09-25 用户拍板] 地点网格化地图：场所系统的几何注释层。

架构立场（三代理 DeepSeek/Qwen/GLM 同题共识）：网格是 P27 场所系统的**可视化层**——
位置权威仍是 (location_id, place_id)，移动/场所切换/place 重置链一律不动；
网格只回答「这个场所在地图上摆在哪、长什么样」，不新增位置语义。

- 布局模板：`_GRID_TEMPLATES` 按地点类型（city/town/village/wilderness）各 2 套写死构图
  （Qwen 方案的「几何与题材解耦」：几何跨题材复用，题材感来自格上场所名——
  places 本身已题材化，不另建 48 套几何）。
- 选套：SeededRng 锚 loc.id 确定性选套（同地点永远同一张图，跨次启动不漂移）。
- 格子类型：B=既有场所（进店/前往）、H=宅位（玩家安置）、G=田块（一格一作物）、
  .=空地（纯走位，无交互——摆设最少化）。
- 幂等：grid_for_location(world, loc) 纯函数——每次渲染从 (places, home, garden) 推导，
  零落盘零迁移（GLM 方案：布局由纯函数推导，不加 Location 字段）。
- [!] 玩家可见收益（白话）：进一个镇子，能看到铁匠铺在西北角、药铺在东边、
  自己的宅子在村口第几格——地点从「一列文字」变成「一张能看的图」。
"""
from __future__ import annotations

from typing import Any, Optional

from src.utils.rng import SeededRng

# 网格模板：地点类型 -> [行列表]（字符语义：B=场所 H=宅位 G=田块 .=空地）。
# 几何与题材解耦（Qwen 方案）：题材感来自格上场所名（places 已题材化），
# 故模板只按 settlement_size 区分、跨题材复用——数据量省 6 倍且不违 §23。
_GRID_TEMPLATES: dict[str, list] = {
    "city": [
        [  # 城·甲：坊市围广场，田在城南
            "..BBB...",
            ".B...B..",
            "..B.B...",
            "HH...HH.",
            "..GG.GG.",
            "........",
        ],
        [  # 城·乙：双列街市
            ".B.B.B..",
            ".B.B.B..",
            "...B....",
            ".HH.HH..",
            ".GG.GG..",
            "........",
        ],
    ],
    "town": [
        [  # 镇·甲
            "...BB...",
            ".B...B..",
            "HH...HH.",
            "..GG....",
            "........",
        ],
        [  # 镇·乙
            ".BB.....",
            "..B.B...",
            "H...H...",
            "..GG....",
            "........",
        ],
    ],
    "village": [
        [  # 村·甲
            "..B.....",
            ".B..B...",
            "H..H....",
            ".GG.....",
            "........",
        ],
        [  # 村·乙
            ".B..B...",
            "...B....",
            ".H..H...",
            "..GG....",
            "........",
        ],
    ],
    "wilderness": [
        [  # 野·甲：猎户小屋 + 零星田
            "........",
            ".B......",
            "...H....",
            "..GG....",
            "........",
        ],
        [  # 野·乙
            "........",
            "....B...",
            "..H.....",
            ".GG.....",
            "........",
        ],
    ],
}


def _template_for(loc: Any) -> list:
    """按地点类型取模板池，SeededRng 锚 loc.id 确定性选套（同地点恒同图）。"""
    size = str(getattr(loc, "settlement_size", "") or "") or "wilderness"
    kind = "wilderness" if getattr(loc, "kind", "") == "wilderness" else size
    pool = _GRID_TEMPLATES.get(kind) or _GRID_TEMPLATES["wilderness"]
    idx = SeededRng.seed_from(str(getattr(loc, "id", "") or "x"), 0,
                              f"grid_layout_{loc.id}").roll(1, len(pool)) - 1
    return pool[max(0, min(len(pool) - 1, idx))]


def grid_for_location(world: Any, loc: Any) -> dict:
    """推导地点网格（纯函数、幂等、零落盘）：场所/宅/田落到模板格上。

    返回 {"cols", "rows", "cells": [{x,y,ch,kind,label,ref}]}。
    - B 格：loc.places 按序填充（场所名即格上标签；places 多于 B 格时溢出场所在
      场所条里仍可达，只是不上图——模板 B 格数按各类型场所上限设计）。
    - H 格：玩家在此地的宅占一格（home.grid_cell 匹配），其余为「可安置」空宅位。
    - G 格：home.garden 的 plot 按序填充（**一格一作物**——数据层本就一地块一条记录，
      此处把「一格一作物」显性化）；超出田块容量的 G 格标「未开垦」。
    """
    places = list(getattr(loc, "places", None) or [])
    home = next((h for h in (getattr(world, "homes", None) or [])
                 if getattr(h, "location_id", "") == getattr(loc, "id", "")), None)
    plots = list(getattr(home, "garden", None) or []) if home is not None else []
    rows = _template_for(loc)
    cells: list = []
    b_i = 0
    g_i = 0
    home_grid = str(getattr(home, "grid_cell", "") or "")
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            cell = {"x": x, "y": y, "ch": ch, "kind": "empty", "label": "", "ref": ""}
            if ch == "B":
                if b_i < len(places):
                    p = places[b_i]
                    cell.update({"kind": "place",
                                 "label": str(getattr(p, "name", "") or "?"),
                                 "ref": str(getattr(p, "id", "") or "")})
                else:
                    cell.update({"kind": "vacant", "label": "空置"})
                b_i += 1
            elif ch == "H":
                if home is not None and home_grid == f"{x},{y}":
                    cell.update({"kind": "home", "label": str(getattr(home, "name", "") or "宅"),
                                 "ref": str(getattr(home, "id", "") or "")})
                else:
                    cell.update({"kind": "lot", "label": "可安置"})
            elif ch == "G":
                if g_i < len(plots):
                    plot = plots[g_i] if isinstance(plots[g_i], dict) else {}
                    crop_name = str(plot.get("crop_name", "") or "")
                    cell.update({"kind": "plot", "ref": str(g_i),
                                 "label": (f"{crop_name}（{plot.get('stage', 0)}阶）"
                                           if crop_name else "已开垦")})
                else:
                    cell.update({"kind": "locked_plot", "label": "未开垦"})
                g_i += 1
            cells.append(cell)
    return {"cols": len(rows[0]) if rows else 0, "rows": len(rows), "cells": cells}


def settle_cell(world: Any, home: Any, loc: Any, gx: int, gy: int) -> "tuple[bool, str]":
    """把已购的宅安置到地点网格的指定宅位（购宅后或迁址时调用）。

    校验：目标格必须是 H（宅位）、未被自己的其他占用记录写错（一宅一格）。
    安置只写 Home.grid_cell（零迁移：Location 不加字段）。
    """
    if home is None:
        return False, "未拥有宅邸"
    grid = grid_for_location(world, loc)
    cell = next((c for c in grid["cells"]
                 if c["x"] == int(gx) and c["y"] == int(gy)), None)
    if cell is None or cell["kind"] != "lot":
        return False, "那里不能安置宅邸（只能选空宅位）"
    home.grid_cell = f"{int(gx)},{int(gy)}"
    return True, ""
