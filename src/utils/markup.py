"""
角色扮演文本富文本渲染器（规则引擎）。

把聊天文本解析为带颜色的富文本，区分对话台词/动作旁白/心声/符号等。
规则可由用户在「设置 -> 气泡配色规则」中自定义（正则匹配），存于
data/render_rules.json，全局生效。

两种渲染路径：
  - render(text, is_user) -> html          产出带 <span style=color> 的 HTML（markup 模式，供 QLabel RichText）
  - render_to_document(text, is_user, doc)  直接渲染进 QTextDocument
      - markup 模式：setHtml(配色 span HTML)（老行为）
      - markdown 模式：setMarkdown 结构化（标题/列表/表格/代码块/引用/分割线）+ QTextCursor 二次着色
      - auto 模式：检测到 Markdown 块级语法走 markdown，否则走 markup

仅用于 UI 展示，不修改发送给 API 的原文。
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


# ============ 渲染模式 ============
# markup  -> 仅配色规则，AI 的文本当字面文本（§15 老行为，默认）
# markdown -> setMarkdown 结构化（标题/列表/表格/代码块/引用/分割线）+ 配色规则二次着色
# auto    -> 文本含 Markdown 块级语法走 markdown，否则走 markup
RENDER_MODE_MARKUP = "markup"
RENDER_MODE_MARKDOWN = "markdown"
RENDER_MODE_AUTO = "auto"
_VALID_RENDER_MODES = {RENDER_MODE_MARKUP, RENDER_MODE_MARKDOWN, RENDER_MODE_AUTO}

# auto 模式检测：含 Markdown 块级语法即触发 markdown 模式。
# [!] 只检测块级标记，不检测行内 *斜体*/**加粗**：角色扮演文本里 *旁白* 是高频配色标记，
# 若把行内 * 算作 md 触发条件会误把纯旁白文本也走 markdown（星号被当斜体吃掉，配色标记丢失）。
# 块级标记：行首 # 标题 / 行首 - * + 列表 / 行首 > 引用 / 围栏 ``` / 表格 | / 分割线 ---
_AUTO_MD_RE = re.compile(
    r"(?m)"                       # 多行模式
    r"^\s{0,3}#{1,6}\s"           # 行首标题 # ~ ######
    r"|^\s{0,3}[-*+]\s"           # 行首无序列表 - * +
    r"|^\s{0,3}>\s?"              # 行首引用 >
    r"|^\s{0,3}```"               # 行首围栏代码块
    r"|^\s{0,3}---+\s*$"          # 分割线 ---
    r"|\|.*\|.*\n\s*\|[\s\-:|]+"  # 表格（含分隔行 |---|）
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
# 规则/模式版本号：每次 set_rules_config/set_render_mode/reload_rules 自增，供手机端
# 轮询判断是否需要重拉规则（避免每次拉全量 rules 比对）。手机端轮询 /m/api/render_rules/version。
_render_rules_version: int = 0


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
    global _rules_config, _render_rules_version
    with _rules_lock:
        _rules_config = _load_config_from_disk()
        _rebuild_compiled()
        _render_rules_version += 1


def get_rules_config() -> RenderRulesConfig:
    """获取当前规则配置（用于 UI 读取编辑）。"""
    with _rules_lock:
        return _rules_config


def set_rules_config(cfg: RenderRulesConfig):
    """设置内存中的规则配置并重建缓存（不落盘，调用方负责持久化）。"""
    global _rules_config, _render_rules_version
    with _rules_lock:
        _rules_config = cfg
        _rebuild_compiled()
        _render_rules_version += 1


def get_render_rules_version() -> int:
    """当前规则/模式版本号（手机端轮询用，每次规则或模式变更自增）。"""
    with _rules_lock:
        return _render_rules_version


# ============ 渲染模式状态 ============
_render_mode: str = RENDER_MODE_MARKUP


def set_render_mode(mode: str):
    """设置全局渲染模式（markup/html/auto）。非法值回退 markup。"""
    global _render_mode, _render_rules_version
    if mode not in _VALID_RENDER_MODES:
        mode = RENDER_MODE_MARKUP
    # [!] check-then-set + bump 同在 _rules_lock 内，与 get_render_rules_version 读锁
    # 对称，消除并发双计版本号与 TOCTOU 写覆盖。
    with _rules_lock:
        if mode != _render_mode:
            _render_mode = mode
            _render_rules_version += 1


def get_render_mode() -> str:
    return _render_mode


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


# ============ Markdown 模式：渲染进 QTextDocument ============
def _looks_like_markdown(text: str) -> bool:
    """auto 模式检测：文本是否含 Markdown 块级语法。

    [!] 只检测块级标记，不检测行内 *斜体*/**加粗**：角色扮演文本里 *旁白* 是
    高频配色标记，若把行内 * 算作 md 触发条件会误把纯旁白文本也走 markdown
    （星号被当斜体吃掉，配色标记丢失）。块级标记才触发：标题/列表/引用/代码块/表格/分割线。
    """
    return bool(_AUTO_MD_RE.search(text))


def _normalize_fences(text: str) -> str:
    """markdown 渲染前规范化围栏代码块：开围栏行前若非空行则补一个空行。

    MD4C（Qt setMarkdown 用的解析器）要求围栏代码块前有空行才识别成 <pre><code>，
    酒馆卡作者习惯把 ``` 紧贴正文行（如 `『...』\\n```json`），导致围栏被当字面文本
    不解析。本函数仅给开围栏行（行 strip 后以 ``` 开头）前补空行，让 MD4C 识别。

    [!] 纯展示规范化：只作用于传给 setMarkdown 的临时文本，不写回 message.content
    （§13 纯展示契约）。闭围栏后无需补空行（开围栏被识别后整个代码块就解析了）。
    [!] 只在「上一行非空」时补一个空行，不引入连续多余空行。
    [!] 行内反引号（非行首 ```）不受影响：只检测行 strip 后以 ``` 开头的行。
    """
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    for ln in lines:
        if ln.strip().startswith("```"):
            if out and out[-1].strip() != "":
                out.append("")  # 上一行非空，补一个空行分隔
        out.append(ln)
    return "\n".join(out)


# 标准 HTML 内联/块标签白名单：这些不吞（交给 Qt setMarkdown 处理）
_STD_HTML_TAGS = {
    "font", "b", "i", "u", "strong", "em", "a", "span", "div", "p", "br",
    "img", "sub", "sup", "small", "big", "tt", "code", "pre", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "tr", "td", "th",
    "hr", "center", "s", "strike", "del", "ins", "mark", "abbr", "cite", "q",
}
# [!] 只吞「成对出现」的自定义块标签：酒馆卡的 <StatusBlock>...</StatusBlock> /
# <world>...</world> 都是成对开闭；RP 角括号动作 <sighs>/<waves> 通常无对应闭标签，
# 不会被误吞。先扫一遍找出所有成对标签名，再删这些标签的整行。
_TAG_LINE_RE = re.compile(r"^\s*</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>\s*$")
_OPEN_TAG_RE = re.compile(r"^\s*<([A-Za-z][A-Za-z0-9]*)\b[^>]*>\s*$")
_CLOSE_TAG_RE = re.compile(r"^\s*</([A-Za-z][A-Za-z0-9]*)\s*>\s*$")


def _strip_custom_blocks(text: str) -> str:
    """markdown 渲染前吞掉自定义语义块标签行（如 StatusBlock/world/settings 等）。

    酒馆卡作者用 `<StatusBlock>...</StatusBlock>`/`<world>...</world>` 等自定义块标签
    分组内容，LLM 也会输出。Qt setMarkdown 把它们当未知 HTML 吃掉，但会破坏内部结构
    （```json 代码块等被压乱）。本函数把「整行独占的非标准 HTML 标签行」直接删掉，
    标签前后各保留一个空行作段落分隔，内容当普通段落渲染。

    [!] 只吞「成对出现」的自定义块标签：先扫描全文找出既有开标签 `<Tag>` 又有闭标签
    `</Tag>` 的标签名（标签名不在 _STD_HTML_TAGS 白名单），再删这些标签的整行。
    RP 角括号动作 `<sighs>`/`<waves>` 通常无对应闭标签，不会被误吞。
    [!] 只处理「行首出现、整行独占」的标签（避免误伤正文里的 `<` 字符）。
    [!] 纯展示规范化：不写回 message.content（§13 纯展示契约）。
    """
    if not text:
        return text
    lines = text.split("\n")
    # 1. 扫描全文，收集成对出现的自定义标签名（开闭都有、且非标准 HTML 标签）
    open_tags: set[str] = set()
    close_tags: set[str] = set()
    for ln in lines:
        mo = _OPEN_TAG_RE.match(ln)
        if mo and mo.group(1).lower() not in _STD_HTML_TAGS:
            open_tags.add(mo.group(1))
        mc = _CLOSE_TAG_RE.match(ln)
        if mc and mc.group(1).lower() not in _STD_HTML_TAGS:
            close_tags.add(mc.group(1))
    paired = open_tags & close_tags  # 既有开又有闭才算成对块标签
    if not paired:
        return text
    # 2. 删这些标签的整行，吞掉后前面补空行分隔
    out: list[str] = []
    for ln in lines:
        m = _TAG_LINE_RE.match(ln)
        if m and m.group(1) in paired:
            # 成对自定义块标签行：吞掉，前面补空行分隔
            if out and out[-1].strip() != "":
                out.append("")
            continue
        out.append(ln)
    result = "\n".join(out)
    # 清理吞标签后可能留下的连续多余空行（3+ -> 2）
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


# markdown 模式段落间距：模拟原文空行的视觉停顿（markdown 把空行当段落分隔吞掉）
# 每个非代码块段落下方留一个行高的间距，让段落间有视觉空行感。
_PARA_SPACING_PX = 14

# markdown 模式代码块背景色：围栏代码块（```json 等）铺一层深底色，模拟酒馆代码框观感。
# Qt setMarkdown 解析围栏代码块为 <pre><code> 但 QTextDocument 默认样式表为空、不附带背景，
# 代码文本直接透出气泡底色，没有"代码框"观感。在 block 格式上 setBackground 给每行代码
# block 铺同色深底，多行连续拼成整块深底代码框（行间无 margin 故底色连续）。
_CODE_BG_COLOR = "#0d0d12"


def _apply_paragraph_spacing(doc, text: str) -> None:
    """markdown 渲染后给非代码块段落设 bottomMargin，给代码块设深色背景。

    markdown 语义把连续空行当段落分隔吞掉，角色扮演文本的空行是重要节奏停顿（场景切换、
    留白），全挤一起阅读体验差。本函数给非代码块 block 设 bottomMargin 模拟空行间距。
    [!] 代码块（BlockNonBreakableLines）不加间距：代码块内部多行会拆成多个 code block，
    加间距会让代码行之间产生空隙破坏紧凑性。代码块与前后段落的间距靠其前后段落自身的
    bottomMargin 撑开。
    [!] 代码块铺深色背景：Qt setMarkdown 默认不给 <pre><code> 加背景，需手动在 block
    格式上补。代码块文本色由 _colorize_document_with_rules 第 1 步设为 default_color
    （#c0caf5 浅蓝白），在 #0d0d12 深底上对比度足够可读。
    [!] 纯展示：只改 document 的 block 格式，不写回 message.content。
    """
    from PySide6.QtGui import QTextCursor, QTextBlockFormat, QColor
    b = doc.firstBlock()
    while b.isValid():
        fmt = b.blockFormat()
        if fmt.boolProperty(QTextBlockFormat.BlockNonBreakableLines):
            # 代码块：铺深色背景（模拟酒馆代码框），不加 margin 保持行间紧凑
            new_fmt = QTextBlockFormat(fmt)
            new_fmt.setBackground(QColor(_CODE_BG_COLOR))
            cur = QTextCursor(b)
            cur.setBlockFormat(new_fmt)
        else:
            new_fmt = QTextBlockFormat(fmt)
            new_fmt.setBottomMargin(_PARA_SPACING_PX)
            cur = QTextCursor(b)
            cur.setBlockFormat(new_fmt)
        b = b.next()


# <font color='#XXX'> -> <span style="color:#XXX">，</font> -> </span>
# [!] Qt setMarkdown 对 <font> 标签处理不稳定：独占行时当 HTML 块吞掉后续内容，
# 内联时丢颜色。<span style="color:..."> 更稳定，Qt 当行内 HTML 正确解析颜色不吞内容。
_FONT_OPEN_RE = re.compile(r"""<font\s+color\s*=\s*['"]([^'"]*)['"][^>]*>""", re.IGNORECASE)


def _convert_font_to_span(text: str) -> str:
    """把 <font color='...'>...</font> 转成 <span style="color:...">...</span>，
    并把标签与内容合并到同一行（标签后/前不留换行）。

    Qt setMarkdown 对 <font> 标签处理不稳定（独占行当 HTML 块吞后续内容、内联丢颜色）；
    <span style="color:..."> 更稳定，但**标签独占一行或紧跟换行时 Qt 仍会当 HTML 块处理**
    导致段落合并/内容丢失。故转换后必须把开标签与后续内容合并到同一行（`<span...>\n内容`
    -> `<span...>内容`），闭标签与前一内容合并（`内容\n</span>` -> `内容</span>`）。
    [!] 纯展示规范化：不写回 message.content（§13 纯展示契约）。
    """
    if not text or "<font" not in text.lower():
        return text
    # <font color='X'> -> <span style="color:X">
    text = _FONT_OPEN_RE.sub(r'<span style="color:\1">', text)
    # </font> -> </span>
    text = re.sub(r"</font\s*>", "</span>", text, flags=re.IGNORECASE)
    # 开标签后紧跟换行 -> 合并到同一行（去掉标签后的换行）
    text = re.sub(r'(<span[^>]*>)\s*\n', r'\1', text)
    # 闭标签前紧跟换行 -> 合并到同一行（去掉闭标签前的换行）
    text = re.sub(r'\n\s*(</span>)', r'\1', text)
    return text



def _colorize_document_with_rules(doc, is_user: bool, compiled: list[tuple[RenderRule, re.Pattern]],
                                  default_color: str):
    """对已 setMarkdown 的 document 用配色规则二次着色（QTextCursor.mergeCharFormat）。

    [!] 配色规则只作用于纯文本内容：Markdown 标记（#/|/>/```）在 setMarkdown 后已被
    解析为结构，block.text() 是去标记后的纯文本，正则匹配的是纯文本片段（如「台词」），
    不会碰结构标记。这与 §15 配色规则语义一致（着色正文，不碰标记）。
    [!] 只对黑色/无色字符设默认色，保留 Qt 从 <font color> 解析的行内彩色：setMarkdown
    把 <font color='#XXX'> 解析成字符 foreground，全局 mergeCharFormat 会覆盖它。改为
    用选区法逐字符检测 foreground，只对黑色（#000000 或无 brush）的连续段设默认色，
    彩色字符跳过保留。<font color> 内容若命中配色规则仍会被规则色覆盖（规则优先，预期）。
    """
    # 延迟导入：避免 markup 模块在非 GUI 场景（如纯逻辑测试）加载时强依赖 QtGui
    from PySide6.QtGui import QTextCursor, QTextCharFormat, QColor

    # 1. 只对黑色/无色字符设默认色，保留 Qt 解析的 <font color> 行内色。
    # [!] 用 QTextFragment 迭代（O(fragments) 而非 O(chars)，长文本不卡顿）：每个 fragment
    # 是同格式连续段，自带 charFormat()，天然适合「找连续黑色段」。比逐字符 setPosition 高效。
    # [!] 用 mergeCharFormat 而非 setCharFormat（保留 bold/italic 等结构格式）。
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(default_color))
    b = doc.firstBlock()
    while b.isValid():
        it = b.begin()
        while not it.atEnd():
            frag = it.fragment()
            it += 1
            if not frag.isValid():
                continue
            fg = frag.charFormat().foreground()
            is_black = (not fg.style()) or fg.color().name().lower() in ("#000000", "#000")
            if is_black:
                c = QTextCursor(doc)
                c.setPosition(frag.position())
                c.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
                c.mergeCharFormat(fmt)
        b = b.next()

    # 2. 逐 block 遍历，对命中配色规则的片段 merge 规则色
    # 复用与 _render_line_by_rules 一致的重叠合并逻辑（priority 升序，先出现优先）
    b = doc.firstBlock()
    while b.isValid():
        text = b.text()
        block_pos = b.position()
        if not text:
            b = b.next()
            continue
        # 收集本 block 所有规则命中
        hits: list[tuple[int, int, RenderRule]] = []
        for rule, pat in compiled:
            # 作用域过滤：scope 不命中的规则跳过（与 _line_to_html 一致）
            if rule.scope not in (SCOPE_ALL, SCOPE_USER if is_user else SCOPE_AI):
                continue
            for m in pat.finditer(text):
                hits.append((m.start(), m.end(), rule))
        if not hits:
            b = b.next()
            continue
        # priority 升序（compiled 已排序，但 finditer 跨规则混合后需重排）
        hits.sort(key=lambda x: (x[0], x[1]))
        # 合并重叠：保留先出现的
        merged: list[tuple[int, int, RenderRule]] = []
        last_end = -1
        for s, e, r in hits:
            if s < last_end:
                continue
            merged.append((s, e, r))
            last_end = max(last_end, e)
        # 逐命中片段 merge 字符格式
        for s, e, rule in merged:
            inner_start, inner_end = s, e
            if not rule.keep_marks and e - s >= 2:
                # keep_marks=False 时去掉首尾各一字符（与 _line_to_html 一致）
                inner_start, inner_end = s + 1, e - 1
            if inner_end <= inner_start:
                continue
            c = QTextCursor(doc)
            c.setPosition(block_pos + inner_start)
            c.setPosition(block_pos + inner_end, QTextCursor.KeepAnchor)
            f = QTextCharFormat()
            f.setForeground(QColor(_validate_color(rule.color)))
            if rule.italic:
                f.setFontItalic(True)
            c.mergeCharFormat(f)
        b = b.next()


def render_to_document(text: str, is_user: bool, doc):
    """渲染文本进 QTextDocument（QTextBrowser.document()）。

    按全局 _render_mode 选择渲染路径：
      - markup   -> setHtml(配色 span HTML)（§15 老行为，所有文本转义，标签当字面文本）
      - markdown -> setMarkdown 结构化 + 配色规则二次着色
      - auto     -> 检测到 Markdown 块级语法走 markdown，否则走 markup

    [!] 纯展示契约不变：绝不修改原 text/调用方 message.content，发 API 始终原文。
    [!] setMarkdown 不支持增量，流式整段重渲（与现状 update_content 整段重渲一致）。
    """
    if not text:
        doc.clear()
        return
    mode = _render_mode
    if mode == RENDER_MODE_AUTO:
        mode = RENDER_MODE_MARKDOWN if _looks_like_markdown(text) else RENDER_MODE_MARKUP

    if mode == RENDER_MODE_MARKDOWN:
        from PySide6.QtGui import QTextDocument
        # [!] 渲染前预处理：吞自定义块标签 -> 补围栏空行 -> font 转 span
        text = _strip_custom_blocks(text)
        text = _normalize_fences(text)
        text = _convert_font_to_span(text)
        doc.setMarkdown(text, QTextDocument.MarkdownDialectGitHub)
        _colorize_document_with_rules(doc, is_user, _compiled, _default_color(is_user))
        # [!] markdown 把空行当段落分隔吞掉，角色扮演文本的空行是重要节奏停顿，
        # 给每个非代码块段落设 bottomMargin 模拟原文空行的视觉间距。
        _apply_paragraph_spacing(doc, text)
    else:
        # markup 模式：走老逻辑产出 HTML 再 setHtml
        doc.setHtml(render(text, is_user))


def render_with_config_to_document(text: str, is_user: bool, cfg: RenderRulesConfig,
                                   mode: str, doc):
    """用指定规则配置 + 渲染模式渲染进 document，供编辑窗体实时预览。

    不改变全局状态；仅在 UI 主线程预览时调用。与 render_with_config 对应的 document 版。
    """
    if not text:
        doc.clear()
        return
    # 编译 cfg 的规则（与 render_with_config 一致，不污染全局 _compiled）
    compiled: list[tuple[RenderRule, re.Pattern]] = []
    for rule in cfg.rules:
        if not rule.enabled or not rule.pattern:
            continue
        try:
            compiled.append((rule, re.compile(rule.pattern)))
        except re.error:
            continue
    compiled.sort(key=lambda x: x[0].priority)
    default_color = _validate_color(cfg.user_default_color if is_user else cfg.ai_default_color)

    if mode == RENDER_MODE_AUTO:
        mode = RENDER_MODE_MARKDOWN if _looks_like_markdown(text) else RENDER_MODE_MARKUP

    if mode == RENDER_MODE_MARKDOWN:
        from PySide6.QtGui import QTextDocument
        # [!] 渲染前预处理（与 render_to_document 一致，预览与实际渲染对齐）
        text = _strip_custom_blocks(text)
        text = _normalize_fences(text)
        doc.setMarkdown(text, QTextDocument.MarkdownDialectGitHub)
        _colorize_document_with_rules(doc, is_user, compiled, default_color)
        _apply_paragraph_spacing(doc, text)
    else:
        doc.setHtml(render_with_config(text, is_user, cfg))
