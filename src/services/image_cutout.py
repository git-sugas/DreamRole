"""[2026-09-25 用户指示] 纯色背景抠图（世界模拟人像/怪物图生图后处理）。

用户诉求：「人物和怪物的头像生图时加一个纯色背景，然后用代码扣成透明的，
这样不管是战斗还是展示都比较好看。」

本模块只做一件事：把「主体 + 纯色背景」的 PNG 里的背景抠成透明（原地覆盖）。
配合 world_sim_service 的生图侧（提示词追加纯色背景 tag）成对使用，但模块本身
不依赖任何世界模拟代码，可单测（只依赖 Pillow + numpy）。

算法（确定性、纯本地，无需 GPU/网络）：
1. 参考背景色 = 四边 `_EDGE_BAND` 圈像素的中位数（抗单角杂色/水印，
   也不要求实际颜色等于提示词里要的颜色——LLM 出了灰底照样能抠）。
2. 候选掩码 = 与参考色的欧氏距离 <= tolerance 的像素。
3. 连通性：从图片四边的候选像素出发做扫描线 flood fill，**只抠与画面边缘连通的区域**。
   这是与「全局色替换」的关键区别——人物身上的同色区域（白衣服配白底）不与边缘
   连通，不会被误抠。
4. 边缘去白边：只把颜色接近背景的主体外沿向内收 1 像素再羽化，并按背景色反算半透明边缘的前景色；
   避免白底像素在暗色战场上变成亮色轮廓。
5. 安全回退（任一不满足即放弃、保留原图，调用方无感）：
   - 抠掉比例 < `_MIN_CUT_RATIO`：说明边缘不是纯色背景（flood fill 只吃到零星杂色），
     抠了等于没抠还可能啃掉主体边缘；
   - 抠掉比例 > `_MAX_CUT_RATIO`：说明主体与背景同色（整图被吃掉），绝不能应用；
   - 图片已有透明像素（`_ALREADY_ALPHA_RATIO` 以上）：视为已抠过，幂等跳过（可反复调用）。

[!] 幂等性是刻意的：世界模拟的缓存类图（combat_/pet_{key}.png）命中缓存不再重生成，
补图/升级入口会对已存在的文件重复调本函数，重复调用必须无害。
"""
from __future__ import annotations

import os
import uuid

import numpy as np
from PIL import Image, ImageFilter

from src.utils.debug import debug_log

# 边缘取样带宽（参考色统计用四边各这么宽的条带）
_EDGE_BAND = 2
# 默认容差（0-255 欧氏距离）。取值依据（实测见 tests/test_image_cutout.py）：
# 「纯色 + 采样噪声」和「8 级线性渐变」远在容差内；真实出图的 simple background 常带
# 轻度暗角/径向渐变（边缘到中心差 ~60 级），56 可整片抠净而浅色主体（与纯色背景差
# 60+ 级）不被误吃。更陡的渐变（>90 级差）属「背景不纯」，由比例安全阀拦回原图。
DEFAULT_TOLERANCE = 56
# 已有透明像素比例超过该值视为「已抠过」（幂等跳过）
_ALREADY_ALPHA_RATIO = 0.01
# 安全阀：抠掉比例下限/上限（超出即放弃，保留原图）
_MIN_CUT_RATIO = 0.02
_MAX_CUT_RATIO = 0.93
# 羽化模糊半径与软阈值 LUT（< LOW 全透明 / > HIGH 全不透明）。
# [!] 阈值实测标定：GaussianBlur(0.7) 在竖直边界上的 alpha 剖面约 [0,5,49,206,250,255]，
#   软带取 (30,220) 才能让边界那 1-2px 落到中间值（真抗锯齿）；早期取 (96,176) 时软带
#   打不到剖面值，等于空转（直边仍是硬跳变）。
_FEATHER_RADIUS = 0.7
_FEATHER_LOW = 30
_FEATHER_HIGH = 220


def _edge_reference(rgb: np.ndarray) -> np.ndarray:
    """参考背景色 = 四边条带像素的逐通道中位数。"""
    edge = np.concatenate([
        rgb[:_EDGE_BAND].reshape(-1, 3),
        rgb[-_EDGE_BAND:].reshape(-1, 3),
        rgb[:, :_EDGE_BAND].reshape(-1, 3),
        rgb[:, -_EDGE_BAND:].reshape(-1, 3),
    ])
    return np.median(edge, axis=0).astype(np.float32)


def _flood_from_edges(cand: np.ndarray) -> np.ndarray:
    """扫描线 flood fill：从四边候选像素出发，返回与其连通的候选区掩码。

    只吃与画面边缘连通的部分（人物内部的同色区域不吃）。
    `cand.tolist()` 转 Python 标量后逐段填充——纯 Python 索引 numpy 标量在
    百万像素图上会慢一个量级，转 list 是必要的性能手段。
    """
    h, w = cand.shape
    cand_py = cand.tolist()
    mask_py = [bytearray(w) for _ in range(h)]
    stack: list[tuple[int, int]] = []
    for x in range(w):
        if cand_py[0][x]:
            stack.append((0, x))
        if cand_py[h - 1][x]:
            stack.append((h - 1, x))
    for y in range(h):
        if cand_py[y][0]:
            stack.append((y, 0))
        if cand_py[y][w - 1]:
            stack.append((y, w - 1))
    while stack:
        y, x = stack.pop()
        row = cand_py[y]
        mrow = mask_py[y]
        if mrow[x] or not row[x]:
            continue
        x1 = x
        while x1 > 0 and row[x1 - 1] and not mrow[x1 - 1]:
            x1 -= 1
        x2 = x
        while x2 < w - 1 and row[x2 + 1] and not mrow[x2 + 1]:
            x2 += 1
        mrow[x1:x2 + 1] = b"\x01" * (x2 - x1 + 1)
        for ny in (y - 1, y + 1):
            if ny < 0 or ny >= h:
                continue
            nrow = cand_py[ny]
            nmrow = mask_py[ny]
            nx = x1
            while nx <= x2:
                if nrow[nx] and not nmrow[nx]:
                    stack.append((ny, nx))
                    while nx <= x2 and nrow[nx]:
                        nx += 1
                else:
                    nx += 1
    return np.frombuffer(b"".join(bytes(r) for r in mask_py), dtype=np.uint8) \
        .reshape(h, w).astype(bool)


def _feather_alpha(alpha: Image.Image) -> Image.Image:
    """边缘羽化：小半径模糊 + 软阈值 LUT（背景仍 0 / 主体内部仍 255）。

    只影响边界那 1-2px（得到中间 alpha 值 -> 抗锯齿）；远离边界的背景与主体内部
    不受影响（< LOW 压回 0、> HIGH 提到 255），不会把主体啃成半透明。
    """
    alpha = alpha.filter(ImageFilter.GaussianBlur(_FEATHER_RADIUS))
    return alpha.point(
        lambda v: 0 if v <= _FEATHER_LOW
        else (255 if v >= _FEATHER_HIGH
              else (v - _FEATHER_LOW) * 255 // (_FEATHER_HIGH - _FEATHER_LOW)))


def _unmatte_edges(rgb: np.ndarray, alpha: np.ndarray, background: np.ndarray) -> np.ndarray:
    """把边缘像素中混入的纯色底反算掉；透明区 RGB 清零以免缩放时渗白。"""
    a = alpha.astype(np.float32) / 255.0
    out = rgb.copy()
    edge = (alpha > 0) & (alpha < 255)
    if edge.any():
        weight = np.maximum(a[edge, None], 0.08)
        out[edge] = np.clip((rgb[edge] - (1.0 - a[edge, None]) * background) / weight,
                            0, 255)
    out[alpha == 0] = 0
    return out.astype(np.uint8)


def cutout_solid_background(image_path: str, tolerance: int = DEFAULT_TOLERANCE,
                            feather: bool = True) -> bool:
    """把图片的纯色背景抠成透明并原地覆盖（PNG）。返回是否「现在具备透明背景」。

    - True：本次抠图已应用，或图片本来就有透明像素（幂等跳过）；
    - False：不满足抠图条件（已有图无纯色背景 / 安全阀拦截 / 读写失败），文件未改动。

    [!] 调用方（世界模拟生图链路）不需要区分「新抠的」和「早就抠过的」——
      两者都意味着「图可以按透明底展示」。失败一律静默保留原图（退化为抠图前行为）。
    """
    if not image_path or not os.path.exists(image_path):
        return False
    tmp = ""
    try:
        with Image.open(image_path) as src:
            src.load()
            img = src.convert("RGBA")
        # 幂等：已有透明像素 -> 视为已抠过，不重复处理（防二次抠图损伤边缘）。
        # [!] 判定必须用 convert("RGBA") 之后的 alpha：P / RGB + tRNS（调色板透明索引）
        #   时 getbands() 里没有 "A"，只看模式会漏判 -> 已抠图被白跑一遍并重写文件。
        existing = np.asarray(img.getchannel("A"))
        if float((existing < 250).mean()) > _ALREADY_ALPHA_RATIO:
            return True
        w, h = img.size
        if w < 8 or h < 8:
            return False
        rgb = np.asarray(img)[:, :, :3].astype(np.float32)
        ref = _edge_reference(rgb)
        # [!] 用平方距离比较：省掉一次全图 sqrt（大图每步都是几十 MB 的临时量）
        tol2 = float(tolerance) ** 2
        dist2 = ((rgb - ref) ** 2).sum(axis=2)
        mask = _flood_from_edges(dist2 <= tol2)
        cut_ratio = float(mask.mean())
        if cut_ratio < _MIN_CUT_RATIO or cut_ratio > _MAX_CUT_RATIO:
            debug_log(lambda: f"[Cutout] 放弃（抠除比例 {cut_ratio:.3f} 越界）: "
                             f"{os.path.basename(image_path)}")
            return False
        # [!] 不传 mode：Image.fromarray 的 mode 参数已废弃，uint8 二维数组自动按 "L"
        alpha = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8))
        if feather:
            # 只削掉与背景相近的外沿；深色细剑/发梢常仅 1px，整张掩码收边会把它们抹掉。
            contracted = np.asarray(alpha.filter(ImageFilter.MinFilter(3)))
            near_bg = dist2 <= (max(1.0, float(tolerance)) * 1.7) ** 2
            raw = np.asarray(alpha)
            alpha = Image.fromarray(np.where(near_bg, contracted, raw).astype(np.uint8))
            alpha = _feather_alpha(alpha)
        alpha_arr = np.asarray(alpha)
        clean_rgb = _unmatte_edges(rgb, alpha_arr, ref) if feather else rgb.astype(np.uint8)
        img = Image.fromarray(np.dstack([clean_rgb, alpha_arr]).astype(np.uint8))
        # 原子写（守 §11「tmp + os.replace」口径）：抠图失败绝不半写坏原图。
        # [!] tmp 名带随机段：同一张图可能被两条链路同时抠（补图 worker / 地图拓展 /
        #   NPC 头像重生成），固定 tmp 名会互相覆盖出半成品。
        tmp = f"{image_path}.{uuid.uuid4().hex[:8]}.cutout.tmp"
        img.save(tmp, "PNG")
        os.replace(tmp, image_path)
    except Exception as e:
        # [!] lambda 默认参数捕获异常对象（except 变量在块结束后被删除，闭包裸引用会 NameError）
        debug_log(lambda err=e: f"[Cutout] 抠图异常（保留原图）: {err}")
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False
    # [!] 成功日志与 return 都在写盘之后、try 之外：debug_log 自身若抛异常（如 Windows
    #   控制台编码），绝不能把「已经抠好的图」报成失败、更不能让异常逃出本函数打断
    #   调用方的生图批次（"抠图失败不影响出图"是契约）。
    try:
        debug_log(lambda: f"[Cutout] 抠图完成（背景占比 {cut_ratio:.3f}）: "
                         f"{os.path.basename(image_path)}")
    except Exception:
        pass
    return True
