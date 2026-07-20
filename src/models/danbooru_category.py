"""Danbooru tag category 整数 -> 中文标签映射。

Danbouru 官方 category 编号（与 CSV/ChromaDB metadata 一致）：
  0=general(通用) 1=artist(画师) 2=(已废弃) 3=copyright(版权) 4=character(角色) 5+=meta(元)
注意：编号 2 在 Danbooru 历史上已废弃（原属 copyright 子类），实际数据中无 2。
⚠️ read.md 旧注释误写为 0=general 1=artist 2=copyright 3=character 4=meta，已据真实
CSV 数据（category=4 的 tag 是初音未来/博丽灵梦等角色）纠正。

本模块提供统一映射，供测试区候选列表与手改勾选窗分桶过滤、显示中文标签复用。
"""
from __future__ import annotations

# category 整数 -> 中文标签（Danbouru 官方编号，2 已废弃故不列入复选框）
DANBOORU_CATEGORIES: dict[int, str] = {
    0: "通用",
    1: "画师",
    3: "版权",
    4: "角色",
    5: "元",
}

# 有序 (int, label) 列表，供 UI 按固定顺序生成复选框（跳过废弃的 2）
DANBOORU_CATEGORY_LIST: list[tuple[int, str]] = [
    (0, "通用"),
    (1, "画师"),
    (3, "版权"),
    (4, "角色"),
    (5, "元"),
]


def category_label(cat: int) -> str:
    """category 整数 -> 中文标签；未知编号（含废弃的 2）回退为 '其他'。"""
    return DANBOORU_CATEGORIES.get(int(cat), "其他")
