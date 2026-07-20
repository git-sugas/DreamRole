"""生成 DreamRole 应用图标 app.ico。

设计：圆角方块深色底（Tokyo Night #1a1b26）+ 白色对话气泡 + 蓝色 AI 星标，
风格与软件主题一致，在浅色/深色任务栏都清晰。

生成多尺寸 ico（16/24/32/48/64/128/256），同时输出 256x256 的预览 PNG 便于核对。
用法: python tools/make_icon.py
"""
from __future__ import annotations
import os
from PIL import Image, ImageDraw


# 主题色（与 src/ui/theme.qss 一致）
BG = (26, 27, 38)          # #1a1b26 深蓝紫底
BG_SOFT = (42, 43, 61)      # #2a2b3d 稍亮，做方块边沿渐变感
BUBBLE = (192, 202, 245)    # #c0caf5 柔白（气泡主体，比纯白柔和）
BUBBLE_DARK = (26, 27, 38)  # 气泡内文字/尾巴用底色
ACCENT = (122, 162, 247)    # #7aa2f7 强调蓝（AI 星标）
ACCENT2 = (224, 175, 104)   # #e0af68 金（星标点缀）


def _rounded_rect_path(size: int, radius: int):
    """返回圆角方形的路径（用于 mask 绘制）。"""
    img = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return img


def render(size: int) -> Image.Image:
    """渲染指定尺寸的图标。所有坐标按 size 等比缩放。"""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 1) 圆角方块底（Windows 11 风格圆角较大）
    radius = max(2, int(s * 0.22))
    mask = _rounded_rect_path(s, radius)
    # 先铺稍亮色再叠主色，形成细微边沿高光感（大尺寸才看得出，小尺寸无妨）
    bg_layer = Image.new("RGBA", (s, s), BG_SOFT + (255,))
    img.paste(bg_layer, (0, 0), mask)
    # 内层主色：尺寸 s-2，paste 到 (1,1)，mask 与 source 同尺寸
    inner_size = s - 2
    inner = Image.new("RGBA", (inner_size, inner_size), BG + (255,))
    inner_mask = _rounded_rect_path(inner_size, max(1, radius - 1))
    img.paste(inner, (1, 1), inner_mask)

    d = ImageDraw.Draw(img)

    # 2) 对话气泡：圆角矩形 + 左下小尾巴
    #    气泡占图标中央偏上，留底部给星标
    bw = int(s * 0.60)   # 气泡宽
    bh = int(s * 0.44)   # 气泡高
    bx = (s - bw) // 2   # 水平居中
    by = int(s * 0.16)   # 顶部留白
    br = max(2, int(s * 0.10))  # 气泡圆角
    d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=br, fill=BUBBLE + (255,))

    # 气泡尾巴（左下指向，表示 AI 回复）
    tail = [
        (bx + int(s * 0.06), by + bh - 1),
        (bx + int(s * 0.06), by + bh + int(s * 0.10)),
        (bx + int(s * 0.20), by + bh - 1),
    ]
    d.polygon(tail, fill=BUBBLE + (255,))

    # 气泡内三个圆点（聊天气泡经典符号，省去文字渲染跨平台字体问题）
    dot_r = max(1, int(s * 0.045))
    dot_y = by + bh // 2
    gap = int(s * 0.11)
    cx = s // 2
    for off in (-gap, 0, gap):
        d.ellipse((cx + off - dot_r, dot_y - dot_r,
                   cx + off + dot_r, dot_y + dot_r),
                  fill=BUBBLE_DARK + (255,))

    # 3) AI 星标（四角星，气泡右下方点缀，呼应「AI」概念）
    #    用四角星而非五角星：更现代、更像 AI/sparkle 符号
    star_cx = int(s * 0.72)
    star_cy = int(s * 0.74)
    star_r = int(s * 0.16)  # 外径
    star_r2 = int(s * 0.055)  # 内径（四角星腰部）
    # 四角星顶点：上/右/下/左 为外径，斜对角为内径
    star_pts = [
        (star_cx, star_cy - star_r),           # 上
        (star_cx + star_r2, star_cy - star_r2),  # 右上腰
        (star_cx + star_r, star_cy),            # 右
        (star_cx + star_r2, star_cy + star_r2),  # 右下腰
        (star_cx, star_cy + star_r),            # 下
        (star_cx - star_r2, star_cy + star_r2),  # 左下腰
        (star_cx - star_r, star_cy),            # 左
        (star_cx - star_r2, star_cy - star_r2),  # 左上腰
    ]
    # 星标底色用蓝色，加金色描边小尺寸也能分辨
    d.polygon(star_pts, fill=ACCENT + (255,))
    # 星标中心高光点（仅大尺寸画，小尺寸画了反而糊）
    if s >= 48:
        hr = max(1, int(s * 0.025))
        d.ellipse((star_cx - hr, star_cy - hr, star_cx + hr, star_cy + hr),
                  fill=ACCENT2 + (255,))

    img.load()
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    assets_dir = os.path.join(project_root, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # 多尺寸 ico（Windows 任务栏/Alt-Tab/文件管理器各取所需）
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render(s) for s in sizes]

    ico_path = os.path.join(assets_dir, "app.ico")
    # Pillow 写 ico：传入最大尺寸图像 + sizes 列表，自动嵌入各尺寸
    images[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )
    # 同时存 256 预览 PNG 便于核对效果
    png_path = os.path.join(assets_dir, "app_preview.png")
    images[-1].save(png_path, format="PNG")

    print(f"OK -> {ico_path}")
    print(f"OK -> {png_path}")
    print(f"sizes: {sizes}")


if __name__ == "__main__":
    main()
