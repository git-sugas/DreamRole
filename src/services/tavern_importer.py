"""SillyTavern 角色卡导入解析（chara_card_v2 / v3 JSON 与 PNG 卡）。

职责单一：把酒馆角色卡解析映射成 DreamRole 的 (Character, WorldBook|None)，
不判断同名冲突（交给 storage.import_tavern_character 处理）。

两条入口：
- parse_tavern_card(data: dict)：JSON 入口，不碰文件系统，不写头像
  （JSON 卡 data.avatar 是 base64 但通常无独立图源，导入后可手动设头像）。
- parse_tavern_card_from_png(path)：PNG 入口，从 tEXt chunk（keyword `ccv3`/`chara`，
  base64 of UTF-8 JSON）解析角色卡，并把 PNG 图片本身当头像写入 avatars 目录。

酒馆卡顶层字段与 data.* 同义（data.* 是规范字段），统一优先读 data.* 回退顶层。
不导入字段：creator_notes、system_prompt、post_history_instructions、
talkativeness、fav、spec 等无对应字段。
"""
from __future__ import annotations
import os
import json
import base64
import struct
import zlib
import uuid
import shutil
from typing import Optional

from src.config import paths
from src.models import Character, WorldBook, WorldBookEntry


# 酒馆 position 整数 -> DreamRole 字符串枚举映射
# 酒馆规范：0=before_char, 1=after_char, 2=before_AN, 3=after_AN, 4=at_top, 5=at_bottom
# [!] 实际导出的卡 position 既可能是整数也可能是字符串枚举名（如示例文件用 "after_char"），
# 两种都兼容，未知值回退 before_char。
_TAVERN_POSITION_MAP = {
    0: "before_char",
    1: "after_char",
    2: "before_an",
    3: "after_an",
    4: "at_top",
    5: "at_bottom",
}


def _str(d: dict, key: str, fallback_key: str = "") -> str:
    """从 dict 取字符串字段：优先 key，回退 fallback_key，再回退 ""。
    [!] or "" 防 null（与 model from_dict 一致，§12）。
    """
    val = d.get(key)
    if val is None and fallback_key:
        val = d.get(fallback_key)
    return val or ""


def _str_list(d: dict, key: str, fallback_key: str = "") -> list[str]:
    """从 dict 取字符串列表字段，过滤非字符串项防脏数据。"""
    val = d.get(key)
    if (val is None or not isinstance(val, list)) and fallback_key:
        val = d.get(fallback_key)
    if not isinstance(val, list):
        return []
    return [str(x) for x in val if isinstance(x, str)]


def _map_position(raw) -> str:
    """酒馆 position（int 0-5 或字符串枚举名）-> DreamRole 枚举，未知值回退 before_char。

    酒馆规范是整数，但部分导出卡用字符串枚举名（如示例文件用 "after_char"），
    两种都兼容。
    """
    # 字符串枚举名：直接校验是否在 DreamRole 白名单内
    if isinstance(raw, str):
        if raw in ("before_char", "after_char", "before_an", "after_an", "at_top", "at_bottom"):
            return raw
        return "before_char"
    # 整数
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return "before_char"
    return _TAVERN_POSITION_MAP.get(idx, "before_char")


def parse_tavern_card(data: dict) -> tuple[Character, Optional[WorldBook]]:
    """解析酒馆角色卡 dict，返回 (Character, WorldBook或None)。

    顶层非 dict / 缺 name -> 抛 ValueError（UI 层捕获弹错）。
    Character / WorldBook 均用新生成 uuid（是否复用同名由 storage 决定）。
    """
    if not isinstance(data, dict):
        raise ValueError("角色卡根节点不是对象")

    # 酒馆卡规范字段在 data.* 下，顶层是兼容镜像（示例文件两者都有）
    inner = data.get("data")
    src = inner if isinstance(inner, dict) else data

    name = _str(src, "name", "name") or _str(data, "name")
    if not name:
        raise ValueError("角色卡缺少 name 字段")

    char = Character(
        name=name,
        description=_str(src, "description", "description"),
        personality=_str(src, "personality", "personality"),
        scenario=_str(src, "scenario", "scenario"),
        first_message=_str(src, "first_mes", "first_mes"),
        mes_example=_str(src, "mes_example", "mes_example"),
        alternate_greetings=_str_list(src, "alternate_greetings", "alternate_greetings"),
        tags=_str_list(src, "tags", "tags"),
        creator=_str(src, "creator", "creator"),
    )

    # 世界书：character_book 可能在 data.* 或顶层，统一取
    book_raw = src.get("character_book") or data.get("character_book")
    world_book = _parse_character_book(book_raw, fallback_name=name)
    return char, world_book


def _parse_character_book(book_raw, fallback_name: str) -> Optional[WorldBook]:
    """解析酒馆 character_book -> DreamRole WorldBook。无 entries 返回 None。"""
    if not isinstance(book_raw, dict):
        return None
    raw_entries = book_raw.get("entries")
    if not isinstance(raw_entries, list):
        # 酒馆旧格式 entries 可能是 dict（id->entry），统一收成 list
        if isinstance(raw_entries, dict):
            raw_entries = list(raw_entries.values())
        else:
            raw_entries = []
    # 过滤非 dict 项
    raw_entries = [e for e in raw_entries if isinstance(e, dict)]
    if not raw_entries:
        return None

    wb_name = book_raw.get("name") or fallback_name
    entries: list[WorldBookEntry] = []
    for e in raw_entries:
        entries.append(WorldBookEntry(
            keys=_str_list(e, "keys"),
            secondary_keys=_str_list(e, "secondary_keys"),
            content=_str(e, "content"),
            enabled=bool(e.get("enabled", True)),
            insertion_order=int(e.get("insertion_order", 100) or 100),
            position=_map_position(e.get("position", 0)),
            case_sensitive=False,  # 酒馆 use_regex 语义不同，默认 False
            selective=bool(e.get("selective", False)),
            constant=bool(e.get("constant", False)),
        ))
    return WorldBook(name=wb_name, entries=entries)


# PNG 签名（8 字节）：89 50 4E 47 0D 0A 1A 0A
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _parse_png_text_chunks(path: str) -> dict[str, bytes]:
    """解析 PNG 文件的所有 text chunk，返回 {keyword: text_bytes}。

    支持 tEXt / iTXt / zTXt 三种 text chunk（keyword 取 latin1 解码，text 保留原始字节）。
    非标准库：仅用 struct + zlib，不依赖 Pillow。
    非 PNG / 签名错误抛 ValueError。
    """
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != _PNG_SIGNATURE:
            raise ValueError("不是有效的 PNG 文件（签名错误）")

        chunks: dict[str, bytes] = {}
        while True:
            header = f.read(8)
            if len(header) < 8:
                break  # EOF
            length = struct.unpack(">I", header[:4])[0]
            ctype = header[4:8].decode("latin1")
            data = f.read(length)
            f.read(4)  # CRC（不校验，跳过）

            if ctype == "tEXt":
                # keyword \x00 text  （keyword latin1，text 是 UTF-8 字节）
                nul = data.find(b"\x00")
                if nul < 0:
                    continue
                keyword = data[:nul].decode("latin1")
                chunks[keyword] = data[nul + 1:]
            elif ctype == "zTXt":
                # keyword \x00 compression_method(1) compressed_text
                nul = data.find(b"\x00")
                if nul < 0:
                    continue
                keyword = data[:nul].decode("latin1")
                compressed = data[nul + 2:]  # 跳过 \x00 和 compression_method
                try:
                    chunks[keyword] = zlib.decompress(compressed)
                except zlib.error:
                    continue
            elif ctype == "iTXt":
                # keyword \x00 comp_flag(1) comp_method(1) lang \x00 translated \x00 text
                nul = data.find(b"\x00")
                if nul < 0:
                    continue
                keyword = data[:nul].decode("latin1")
                rest = data[nul + 1:]
                if len(rest) < 2:
                    continue
                comp_flag = rest[0]
                # 跳过 comp_method(1)，再吃两个 \x00 分隔的 lang / translated
                rest = rest[2:]
                p1 = rest.find(b"\x00")
                if p1 < 0:
                    continue
                rest = rest[p1 + 1:]
                p2 = rest.find(b"\x00")
                if p2 < 0:
                    continue
                text = rest[p2 + 1:]
                if comp_flag == 1:
                    try:
                        text = zlib.decompress(text)
                    except zlib.error:
                        continue
                chunks[keyword] = text
            elif ctype == "IEND":
                break
        return chunks


def parse_tavern_card_from_png(path: str) -> tuple[Character, Optional[WorldBook]]:
    """解析 SillyTavern PNG 角色卡，返回 (Character, WorldBook或None)。

    PNG 卡把 JSON 以 base64(UTF-8) 嵌在 tEXt chunk 里，keyword 优先 `ccv3`（v3）
    回退 `chara`（v2）。PNG 图片本身当角色头像，复制到 avatars 目录并写回 char.avatar。
    解析/解码失败抛 ValueError（UI 层捕获弹错）。
    """
    chunks = _parse_png_text_chunks(path)
    text = chunks.get("ccv3") or chunks.get("chara")
    if not text:
        raise ValueError("PNG 角色卡缺少 chara/ccv3 文本块")

    try:
        raw = base64.b64decode(text)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"PNG 角色卡 JSON 解码失败: {e}") from e

    char, world_book = parse_tavern_card(data)

    # 把 PNG 当头像写入 avatars 目录。
    # [!] 预处理成正方形（透明 padding 居中）：酒馆卡 PNG 常是非正方形竖图（如 512x768），
    # make_avatar_pixmap 用 KeepAspectRatioByExpanding 缩放后会裁掉左右两侧画面，
    # 圆形遮罩内只剩中间竖条、四周被图片背景色填满，视觉上不像圆。
    # 居中填充正方形后，圆内能露出完整人物、四周透明，圆形显示正常。
    # GIF 动图原样复制（保留动画帧），扩展名用 .gif 让下游 is_gif_avatar 走 QMovie。
    try:
        filename = _save_square_avatar(path, paths.avatars_dir())
        char.avatar = filename
    except (OSError, ValueError):
        # 头像写失败不阻断导入（角色卡字段已解析，仅头像缺失）
        pass
    return char, world_book


def _save_square_avatar(src_path: str, dest_dir: str) -> str:
    """把任意图片读入，居中填充成正方形后保存到 dest_dir，返回最终文件名。

    透明 padding（RGBA 透明）居中：原图小于正方形边长时四周透明，竖图/横图
    都能完整保留画面主体。GIF 动图原样复制（保留动画帧），扩展名用 .gif
    让下游 is_gif_avatar 按扩展名走 QMovie 动图路径；其他格式存 PNG 扩展名 .png。
    PIL 加载失败（非图片或损坏）抛 ValueError（UnidentifiedImageError 归一到 ValueError，
    调用方只需 catch OSError/ValueError）。
    """
    from PIL import Image

    try:
        img = Image.open(src_path)
    except Exception as e:
        # UnidentifiedImageError 继承 OSError，但统一转 ValueError 让契约自洽
        # （调用方文档承诺 ValueError，不必关心 PIL 内部异常类型）
        raise ValueError(f"无法识别的图片文件: {e}") from e

    with img:
        fmt = img.format
        if fmt == "GIF":
            # GIF 动图保持原样复制（动图不能强转静态 PNG，会丢动画帧）
            filename = f"{uuid.uuid4().hex[:8]}.gif"
            shutil.copy2(src_path, os.path.join(dest_dir, filename))
            return filename

        # 统一带 alpha 通道（with 内操作，img 句柄随退出自动释放）
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        w, h = img.size

        if w == h:
            # 已是正方形：直接保存（保持原像素，避免无谓重编码）
            filename = f"{uuid.uuid4().hex[:8]}.png"
            img.save(os.path.join(dest_dir, filename), "PNG")
            return filename

        # 居中填充成正方形：取 max(w,h) 为边长，透明底，原图居中贴上
        side = max(w, h)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        offset = ((side - w) // 2, (side - h) // 2)
        canvas.paste(img, offset, img)  # 第三参数 mask 用 img 自身 alpha 透明合成
        filename = f"{uuid.uuid4().hex[:8]}.png"
        canvas.save(os.path.join(dest_dir, filename), "PNG")
        return filename
