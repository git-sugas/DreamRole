"""
角色扮演文本富文本渲染器（规则引擎）。

把聊天文本解析为带颜色的 HTML，区分对话台词/动作旁白/心声/符号等。
规则可由用户在「设置 → 气泡配色规则」中自定义（正则匹配），存于
data/render_rules.json，全局生效。

调用 render(text, is_user) -> html。仅用于 UI 展示，不修改发送给 API 的原文。
"""
from __future__ import annotations
import html as _html
import json
import re
import threading

from src.config import paths
from src.models.render_rules import (
    RenderRulesConfig, RenderRule,
    SCOPE_AI, SCOPE_USER, SCOPE_ALL, default_config,
    _validate_color,
)


# ============ 规则加载（线程安全单例）============
_rules_lock = threading.Lock()
_rules_config: RenderRulesConfig = default_config()
# 编译缓存：[(rule, compiled_pattern)]，仅含 enabled 且编译成功的规则
_compiled: list[tuple[RenderRule, re.Pattern]] = []
# 合并大正则：(?P<r0>...)|(?P<r1>...)|...
_merged_pattern: re.Pattern | None = None
# 分组名 -> rule 的映射，按合并顺序
_group_to_rule: dict[str, RenderRule] = {}


def _load_config_from_disk() -> RenderRulesConfig:
    """从 data/render_rules.json 加载配置；不存在则用默认并落盘。"""
    path = paths.render_rules_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return RenderRulesConfig.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = default_config()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return cfg


def _rebuild_compiled():
    """根据 _rules_config 重建编译缓存与合并正则。"""
    global _compiled, _merged_pattern, _group_to_rule
    compiled: list[tuple[RenderRule, re.Pattern]] = []
    for rule in _rules_config.rules:
        if not rule.enabled or not rule.pattern:
            continue
        try:
            compiled.append((rule, re.compile(rule.pattern)))
        except re.error as e:
            print(f"[markup] 规则「{rule.name}」正则编译失败，已跳过: {e}")
    # 按 priority 升序（数字小先匹配）
    compiled.sort(key=lambda x: x[0].priority)
    _compiled = compiled

    # 构造合并命名分组合并正则
    parts: list[str] = []
    group_to_rule: dict[str, RenderRule] = {}
    for i, (rule, _) in enumerate(compiled):
        gname = f"r{i}"
        parts.append(f"(?P<{gname}>{rule.pattern})")
        group_to_rule[gname] = rule
    if parts:
        # [!] 合并正则编译包 try：用户写命名分组（(?P<r5>...) 或两条同名命名分组）
        # 会让合并编译抛 re.error（单条编译能过）。失败时退回 _merged_pattern=None，
        # 渲染走逐规则 finditer 兜底（_line_to_html 已处理 None 分支）。
        try:
            _merged_pattern = re.compile("|".join(parts))
        except re.error as e:
            print(f"[markup] 合并正则编译失败，退回逐规则查找: {e}")
            _merged_pattern = None
    else:
        _merged_pattern = None
    _group_to_rule = group_to_rule


def reload_rules():
    """重新从磁盘加载规则并重建缓存。保存后调用以热更新。"""
    global _rules_config
    with _rules_lock:
        _rules_config = _load_config_from_disk()
        _rebuild_compiled()


def get_rules_config() -> RenderRulesConfig:
    """获取当前规则配置（用于 UI 读取编辑）。"""
    with _rules_lock:
        return _rules_config


def set_rules_config(cfg: RenderRulesConfig):
    """设置内存中的规则配置并重建缓存（不落盘，调用方负责持久化）。"""
    global _rules_config
    with _rules_lock:
        _rules_config = cfg
        _rebuild_compiled()


# 模块首次导入时加载规则
try:
    _rules_config = _load_config_from_disk()
    _rebuild_compiled()
except Exception as e:  # 启动期不应因渲染规则崩溃
    print(f"[markup] 规则加载失败，回退默认: {e}")
    _rules_config = default_config()
    _rebuild_compiled()


# ============ 渲染 ============
def _escape(text: str) -> str:
    """HTML 转义。"""
    return _html.escape(text, quote=False)


def _span(color: str, text: str, italic: bool = False) -> str:
    style = f"color:{_validate_color(color)};"
    if italic:
        style += "font-style:italic;"
    return f'<span style="{style}">{text}</span>'


def _is_symbol_only(line: str) -> bool:
    """整行是否仅由装饰符号/空白组成（保留原逻辑，供纯符号行整行着色）。"""
    stripped = line.strip()
    if not stripped:
        return False
    symbol_chars = set("♡❤♥★☆✧✦♦♢♤♠♣♧☕🎵🎶・…")
    return all(c in symbol_chars for c in stripped)


def _default_color(is_user: bool) -> str:
    return _rules_config.user_default_color if is_user else _rules_config.ai_default_color


def _render_line_by_rules(
    line: str,
    is_user: bool,
    default_color: str,
    compiled: list[tuple[RenderRule, re.Pattern]],
) -> str:
    """合并正则不可用时，逐规则 finditer 兜底渲染（§12 契约：合并正则只是性能优化，失败不影响渲染）。

    语义与 _line_to_html 的 merged 分支完全一致：
    - 收集所有规则的命中区间 [(start, end, rule), ...]
    - 按 start 排序，重叠区间取先出现的（compiled 已按 priority 升序，priority 小先匹配）
    - 命中片段按规则色（scope 不命中则默认色），间隙按默认色
    - keep_marks=False 时剥首尾标记字符
    """
    hits: list[tuple[int, int, RenderRule]] = []
    for rule, pat in compiled:
        for m in pat.finditer(line):
            hits.append((m.start(), m.end(), rule))
    if not hits:
        return _span(default_color, _escape(line))
    hits.sort(key=lambda x: (x[0], x[1]))
    # 合并重叠：保留先出现的（priority 小），后续被覆盖的跳过
    merged_hits: list[tuple[int, int, RenderRule]] = []
    last_end = -1
    for s, e, r in hits:
        if s < last_end:
            continue   # 与已保留区间重叠，丢弃
        merged_hits.append((s, e, r))
        last_end = max(last_end, e)
    parts: list[str] = []
    pos = 0
    for s, e, rule in merged_hits:
        if s > pos:
            parts.append(_span(default_color, _escape(line[pos:s])))
        text = line[s:e]
        if rule.scope not in (SCOPE_ALL, SCOPE_USER if is_user else SCOPE_AI):
            parts.append(_span(default_color, _escape(text)))
        else:
            inner = text if rule.keep_marks else text[1:-1] if len(text) >= 2 else text
            parts.append(_span(rule.color, _escape(inner), rule.italic))
        pos = e
    if pos < len(line):
        parts.append(_span(default_color, _escape(line[pos:])))
    return "".join(parts)


def _line_to_html(line: str, is_user: bool) -> str:
    """单行文本转 HTML（行内解析）。"""
    if not line.strip():
        return "<br>"

    # 整行是装饰符号 -> 找一条 scope 命中的符号规则整行着色，否则默认色
    if _is_symbol_only(line):
        for r, pat in _compiled:
            if r.scope in (SCOPE_ALL, SCOPE_USER if is_user else SCOPE_AI) and pat.fullmatch(line):
                return _span(r.color, _escape(line), r.italic)
        return _span(_default_color(is_user), _escape(line))

    default_color = _default_color(is_user)
    if _merged_pattern is None:
        # [!] 合并正则编译失败（用户写了命名分组等让合并编译抛错但单条能编译）时，
        # 退回逐规则 finditer 兜底，保证行内着色不静默失效（§12 契约）。
        return _render_line_by_rules(line, is_user, default_color, _compiled)

    parts: list[str] = []
    pos = 0
    for m in _merged_pattern.finditer(line):
        if m.start() > pos:
            parts.append(_span(default_color, _escape(line[pos:m.start()])))
        # 找到命中的命名分组
        hit_rule: RenderRule | None = None
        text = m.group(0)
        for gname, rule in _group_to_rule.items():
            if m.group(gname) is not None:
                hit_rule = rule
                break
        if hit_rule is None:
            # 理论上不会发生，兜底
            parts.append(_span(default_color, _escape(text)))
            pos = m.end()
            continue
        # 作用域过滤：scope 不命中的，该片段按默认色处理
        if hit_rule.scope not in (SCOPE_ALL, SCOPE_USER if is_user else SCOPE_AI):
            parts.append(_span(default_color, _escape(text)))
        else:
            inner = text if hit_rule.keep_marks else text[1:-1] if len(text) >= 2 else text
            parts.append(_span(hit_rule.color, _escape(inner), hit_rule.italic))
        pos = m.end()
    if pos < len(line):
        parts.append(_span(default_color, _escape(line[pos:])))
    return "".join(parts)


def render(text: str, is_user: bool = False) -> str:
    """
    把角色扮演文本渲染为富文本 HTML（用于 QLabel RichText 显示）。

    按行解析，行内再按用户配色规则解析。换行以 <br> 保留。
    流式追加时也可安全调用：未闭合的引号/星号会被当作普通文本，不影响显示。

    ⚠️ 契约：本函数**纯展示**，绝不修改原数据——返回全新的 HTML 字符串，
    调用方只应把它用于 `label.setText(...)`，禁止写回 `message.content`。
    发送给 API 的内容始终是未渲染的原文。所有文本先经 _escape 转义，
    AI 输出的原始 HTML 标签会被当字面文本显示（防注入/防布局破坏）。
    """
    if not text:
        return ""
    lines = text.split("\n")
    html_parts = [_line_to_html(ln, is_user) for ln in lines]
    return "".join(html_parts)


# ============ 正则测试辅助（供编辑窗体调用）============
def test_pattern(pattern: str, text: str) -> list[tuple[int, int, str]]:
    """
    测试一条正则对文本的命中情况，返回 [(start, end, matched_text), ...]。
    正则非法返回空列表（调用方据此提示）。
    """
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []
    return [(m.start(), m.end(), m.group(0)) for m in compiled.finditer(text)]


def render_with_config(text: str, is_user: bool, cfg: RenderRulesConfig) -> str:
    """
    用指定规则配置（而非全局内存配置）渲染文本，供编辑窗体实时预览。
    不改变全局状态；仅在 UI 主线程预览时调用。
    """
    compiled: list[tuple[RenderRule, re.Pattern]] = []
    for rule in cfg.rules:
        if not rule.enabled or not rule.pattern:
            continue
        try:
            compiled.append((rule, re.compile(rule.pattern)))
        except re.error:
            continue
    compiled.sort(key=lambda x: x[0].priority)
    parts_re: list[str] = []
    group_to_rule: dict[str, RenderRule] = {}
    for i, (rule, _) in enumerate(compiled):
        gname = f"pr{i}"
        parts_re.append(f"(?P<{gname}>{rule.pattern})")
        group_to_rule[gname] = rule
    # [!] 合并正则编译包 try（与 _rebuild_compiled 一致），失败退回 None 走逐规则 finditer
    if parts_re:
        try:
            merged = re.compile("|".join(parts_re))
        except re.error as e:
            print(f"[markup.render_with_config] 合并正则编译失败，退回逐规则查找: {e}")
            merged = None
    else:
        merged = None
    default_color = _validate_color(cfg.user_default_color if is_user else cfg.ai_default_color)

    def _esc(t: str) -> str:
        return _html.escape(t, quote=False)

    def _spn(color: str, t: str, italic: bool = False) -> str:
        style = f"color:{_validate_color(color)};"
        if italic:
            style += "font-style:italic;"
        return f'<span style="{style}">{t}</span>'

    def _line(ln: str) -> str:
        if not ln.strip():
            return "<br>"
        if _is_symbol_only(ln):
            # pattern 匹配优先（与 _line_to_html 一致，避免错着成 priority 最小规则色）
            for r, pat in compiled:
                if r.scope in (SCOPE_ALL, SCOPE_USER if is_user else SCOPE_AI) and pat.fullmatch(ln):
                    return _spn(r.color, _esc(ln), r.italic)
            return _spn(default_color, _esc(ln))
        if merged is None:
            # [!] 合并正则编译失败时退回逐规则 finditer 兜底（与 _line_to_html 一致，§12 契约）。
            # 复用模块级 _render_line_by_rules（其 _span/_escape 与本函数 _spn/_esc 实现一致）。
            return _render_line_by_rules(ln, is_user, default_color, compiled)
        out: list[str] = []
        pos = 0
        for m in merged.finditer(ln):
            if m.start() > pos:
                out.append(_spn(default_color, _esc(ln[pos:m.start()])))
            hit: RenderRule | None = None
            t = m.group(0)
            for gname, rule in group_to_rule.items():
                if m.group(gname) is not None:
                    hit = rule
                    break
            if hit is None:
                out.append(_spn(default_color, _esc(t)))
            elif hit.scope not in (SCOPE_ALL, SCOPE_USER if is_user else SCOPE_AI):
                out.append(_spn(default_color, _esc(t)))
            else:
                inner = t if hit.keep_marks else t[1:-1] if len(t) >= 2 else t
                out.append(_spn(hit.color, _esc(inner), hit.italic))
            pos = m.end()
        if pos < len(ln):
            out.append(_spn(default_color, _esc(ln[pos:])))
        return "".join(out)

    if not text:
        return ""
    return "".join(_line(ln) for ln in text.split("\n"))
