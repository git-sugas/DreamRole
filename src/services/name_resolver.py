"""[P44 用户指示 2026-08-23] 统一名称解析器：候选集锚定 + 双向子串/编辑距离容错。

背景：LLM 不能保证完全按名单输出名字（加头衔/去姓/漏字/别字/中点间隔装饰），
全链「按名字强行匹配」的精确等值查找会静默丢意图（战斗打不到/交易找不到货/
日计划丢档）。范式取自 world_sim_service._resolve_dest（精确名优先 + 最长真实
地名子串），本模块把它泛化到 NPC 名/物品名/资源点名/计划键，供服务层与各引擎共用。

匹配层级（从严到宽，先命中先停）：
1. 精确等值（strip 后）
2. 归一化等值（剥空白/中点/引号/括号等装饰符后比较）
2b. 字符集等值（归一化后字符多重集相等——LLM 常把修饰词乱序：
   「上品回气丹」vs「回气丹·上品」；中文短名同字集异序撞名概率可忽略）
3. 双向子串（query 含 cand 或 cand 含 query；较短侧 >= 2 字防单字误吞；
   多命中取最长候选——「铁匠鲁大锤」含「鲁大锤」命中，「妖」不含「妖王」不命中）
4. 编辑距离容错（Levenshtein；较短侧 <= 4 字容 1，>= 5 字容 2，防 LLM 别字/漏字；
   较短侧 < 2 字不做编辑距离——单字名对任意单字距离都是 1 会全乱）

设计约束（守世界模拟铁律）：
- 纯 Python 零 LLM 零 Qt，叶子模块（不 import 任何 service/model，防环）。
- 确定性：纯函数，同输入同输出；平手按候选列表原顺序取首个。
- 候选集锚定：只在调用方给的真实名单内解析，绝不凭空造名（引擎不追认虚构）。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# 装饰符剥除表：空白 + 中点/间隔号族 + 引号族 + 括号族 + 句读点。
# [!] 不剥逗号/顿号——物品名与描述性前缀的分隔交给编辑距离层兜（剥了会把
# 「回气丹，上品」与「回气丹」直接划等号，跨候选误吞）。
_STRIP_RE = re.compile(
    r"[\s\u00b7\u2022\u30fb\u2027\u00b7\u00a0"        # 空白族 + 中点/间隔号族
    r"'\u2018\u2019\"\u201c\u201d"                    # 引号族
    r"()\[\]{}（）［］｛｝【】〔〕《》〈〉「」『』"    # 括号族
    r".\u3002\u00b7\uff0e~\uff5e\-—―_"                # 句点/波浪/破折/下划线
    r"]"
)


def _normalize(s: str) -> str:
    """比较用归一化：剥装饰符，不改变原值。"""
    return _STRIP_RE.sub("", str(s or ""))


def _levenshtein(a: str, b: str) -> int:
    """标准编辑距离（含插入/删除/替换）。短串在外层，省一半内存。"""
    if len(a) > len(b):
        a, b = b, a
    if not a:
        return len(b)
    prev = list(range(len(a) + 1))
    for j, cb in enumerate(b, 1):
        cur = [j] + [0] * len(a)
        for i, ca in enumerate(a, 1):
            cur[i] = min(prev[i] + 1,        # 删除
                         cur[i - 1] + 1,     # 插入
                         prev[i - 1] + (ca != cb))  # 替换
        prev = cur
    return prev[len(a)]


def _max_edit(shorter_len: int) -> int:
    """编辑距离容错上限：短名严、长名宽（汉字名 2-5 字为主）。"""
    return 1 if shorter_len <= 4 else 2


def names_match(a: str, b: str) -> bool:
    """单对名称匹配判定（quest 目标计数等场景用）。

    层级同 resolve_name；空串不匹配任何东西（空 target 语义由调用方处理）。
    """
    sa, sb = str(a or "").strip(), str(b or "").strip()
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    na, nb = _normalize(sa), _normalize(sb)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # 字符集等值（乱序重排）
    if len(na) == len(nb) and sorted(na) == sorted(nb):
        return True
    # 双向子串（较短侧 >= 2 字）
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 2 and short in long_:
        return True
    # 编辑距离（较短侧 >= 2 字）
    if len(short) >= 2 and abs(len(na) - len(nb)) <= _max_edit(len(short)) \
            and _levenshtein(na, nb) <= _max_edit(len(short)):
        return True
    return False


def resolve_name(query: str, candidates: Iterable) -> Optional[str]:
    """在真实候选名单内解析 query，返回命中的候选原串；None = 未命中。

    层级见模块 docstring。多命中取最长候选名（_resolve_dest 口径：LLM 常加
    「门前/大人/铁匠」前后缀，长候选 = 更具体的真实名）；仍平手取列表顺序首个。
    """
    q = str(query or "").strip()
    cands = [str(c) for c in (candidates or []) if str(c or "").strip()]
    if not q or not cands:
        return None
    # 1. 精确等值
    for c in cands:
        if c == q:
            return c
    # 2. 归一化等值（首个命中即停）
    nq = _normalize(q)
    if nq:
        for c in cands:
            if _normalize(c) == nq:
                return c
        # 2b. 字符集等值（乱序重排：首个命中即停）
        nq_set = sorted(nq)
        for c in cands:
            nc = _normalize(c)
            if nc and len(nc) == len(nq) and sorted(nc) == nq_set:
                return c
    # 3. 双向子串：收集全部命中取最长候选名
    best = None
    if nq:
        for c in cands:
            nc = _normalize(c)
            if not nc:
                continue
            short, long_ = (nq, nc) if len(nq) <= len(nc) else (nc, nq)
            if len(short) >= 2 and short in long_:
                if best is None or len(nc) > len(_normalize(best)):
                    best = c
    if best is not None:
        return best
    # 4. 编辑距离：最小距离优先，平手取最长候选名，再平手取列表顺序首个
    if nq:
        best = None
        best_key = None
        for c in cands:
            nc = _normalize(c)
            if not nc:
                continue
            short_len = min(len(nq), len(nc))
            if short_len < 2:
                continue
            cap = _max_edit(short_len)
            if abs(len(nq) - len(nc)) > cap:
                continue
            dist = _levenshtein(nq, nc)
            if dist <= cap:
                key = (dist, -len(nc))
                if best_key is None or key < best_key:
                    best, best_key = c, key
        if best is not None:
            return best
    return None
