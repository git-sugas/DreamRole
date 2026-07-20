"""气泡配色规则数据模型。

用户可自定义正则匹配规则，为命中片段着色，让对话/旁白/心声/符号阅读时层次分明。
规则全局生效，存于 data/render_rules.json。

默认规则集把原 markup.py 的 5 种硬编码模式（心声/双引号/中文引号/书名号/动作/符号）
平移为 6 条正则规则，保证老用户升级后渲染效果零变化。
"""
from __future__ import annotations
import re
import uuid
from dataclasses import dataclass, field


# ============ 作用域 ============
SCOPE_AI = "ai"        # 仅 AI 气泡生效
SCOPE_USER = "user"    # 仅用户气泡生效
SCOPE_ALL = "all"      # 全部气泡生效

SCOPE_LABELS = {
    SCOPE_AI: "AI气泡",
    SCOPE_USER: "用户气泡",
    SCOPE_ALL: "全部",
}
_VALID_SCOPES = {SCOPE_AI, SCOPE_USER, SCOPE_ALL}

# color 校验：hex 或颜色名白名单（防 CSS 注入，与 markup._validate_color 一致）
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_COLOR_NAME_WHITELIST = {
    "red", "green", "blue", "yellow", "orange", "purple", "pink", "brown",
    "black", "white", "gray", "grey", "cyan", "magenta", "lime", "teal",
    "navy", "maroon", "olive", "silver", "aqua", "fuchsia",
}


def _validate_color(color, default: str = "#c0caf5") -> str:
    """校验 color 值，非法回退 default。"""
    if isinstance(color, str):
        c = color.strip()
        if _COLOR_RE.match(c) or c.lower() in _COLOR_NAME_WHITELIST:
            return c
    return default


@dataclass
class RenderRule:
    """单条配色规则。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""              # 规则名（如"对话台词"）
    pattern: str = ""           # 正则字符串（如 「[^」]*」）
    color: str = "#c0caf5"      # 命中片段颜色 #hex
    italic: bool = False        # 是否斜体
    enabled: bool = True
    priority: int = 100          # 优先级，数字小的先匹配（finditer 跨规则合并时按此排序）
    scope: str = SCOPE_ALL       # ai | user | all
    # 是否保留匹配标记（如引号/括号）：True=保留外层标记原文，False=去掉首尾各一个字符
    keep_marks: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "color": self.color,
            "italic": self.italic,
            "enabled": self.enabled,
            "priority": self.priority,
            "scope": self.scope,
            "keep_marks": self.keep_marks,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RenderRule":
        scope = d.get("scope", SCOPE_ALL)
        if scope not in _VALID_SCOPES:
            scope = SCOPE_ALL
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", ""),
            pattern=d.get("pattern", ""),
            color=_validate_color(d.get("color", "#c0caf5")),
            italic=bool(d.get("italic", False)),
            enabled=bool(d.get("enabled", True)),
            priority=int(d.get("priority", 100)),
            scope=scope,
            keep_marks=bool(d.get("keep_marks", True)),
        )


@dataclass
class RenderRulesConfig:
    """全局配色规则配置。"""
    rules: list[RenderRule] = field(default_factory=list)
    # 默认色（未命中任何规则的文本）：AI / 用户各一
    ai_default_color: str = "#9aa5ce"      # AI 气泡未命中文本 → 旁白色
    user_default_color: str = "#9aa5ce"    # 用户气泡未命中文本 → 旁白色

    def to_dict(self) -> dict:
        return {
            "rules": [r.to_dict() for r in self.rules],
            "ai_default_color": self.ai_default_color,
            "user_default_color": self.user_default_color,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RenderRulesConfig":
        if not d:
            return cls(rules=default_rules())
        # [!] rules 字段 null 防御：d.get("rules", []) 在 JSON 显式存 null 时返回 None，
        # 迭代会崩。用 `or []` 兜底。
        raw_rules = d.get("rules") or []
        return cls(
            rules=[RenderRule.from_dict(r) for r in raw_rules if isinstance(r, dict)] or default_rules(),
            ai_default_color=_validate_color(d.get("ai_default_color", "#9aa5ce"), "#9aa5ce"),
            user_default_color=_validate_color(d.get("user_default_color", "#9aa5ce"), "#9aa5ce"),
        )


# ============ 默认规则集 ============
# 与原 markup.py 硬编码行为对齐（COLOR_* 常量值）：
#   对话台词 ai=#c0caf5 / user=#89b4fa；旁白=#9aa5ce；心声=#565f89；符号=#f7768e
COLOR_AI_DIALOG = "#c0caf5"
COLOR_USER_DIALOG = "#89b4fa"
COLOR_NARRATION = "#9aa5ce"
COLOR_THOUGHT = "#565f89"
COLOR_SYMBOL = "#f7768e"


def default_rules() -> list[RenderRule]:
    """生成默认规则集（与原 markup.py 渲染效果一致）。"""
    return [
        RenderRule(
            name="心声（括号）",
            pattern=r"（[^）]*）|\([^)]*\)",
            color=COLOR_THOUGHT, italic=True, enabled=True,
            priority=10, scope=SCOPE_ALL, keep_marks=True,
        ),
        RenderRule(
            name='对话 "双引号"',
            pattern=r'"[^"]*"',
            color=COLOR_AI_DIALOG, italic=False, enabled=True,
            priority=20, scope=SCOPE_AI, keep_marks=True,
        ),
        RenderRule(
            name='对话 "双引号"(用户)',
            pattern=r'"[^"]*"',
            color=COLOR_USER_DIALOG, italic=False, enabled=True,
            priority=20, scope=SCOPE_USER, keep_marks=True,
        ),
        RenderRule(
            name="对话「中引号」",
            pattern=r"「[^」「（）]*」",
            color=COLOR_AI_DIALOG, italic=False, enabled=True,
            priority=30, scope=SCOPE_AI, keep_marks=True,
        ),
        RenderRule(
            name="对话「中引号」(用户)",
            pattern=r"「[^」「（）]*」",
            color=COLOR_USER_DIALOG, italic=False, enabled=True,
            priority=30, scope=SCOPE_USER, keep_marks=True,
        ),
        RenderRule(
            name="对话『书名号』",
            pattern=r"『[^』]*』",
            color=COLOR_AI_DIALOG, italic=False, enabled=True,
            priority=40, scope=SCOPE_AI, keep_marks=True,
        ),
        RenderRule(
            name="对话『书名号』(用户)",
            pattern=r"『[^』]*』",
            color=COLOR_USER_DIALOG, italic=False, enabled=True,
            priority=40, scope=SCOPE_USER, keep_marks=True,
        ),
        RenderRule(
            name="动作 *旁白*",
            pattern=r"\*[^*]*\*",
            color=COLOR_NARRATION, italic=True, enabled=True,
            priority=50, scope=SCOPE_ALL, keep_marks=False,
        ),
        RenderRule(
            name="装饰符号 ♡❤★",
            pattern=r"[♡❤★☆♥♠♣♦✦✧]+",
            color=COLOR_SYMBOL, italic=False, enabled=True,
            priority=60, scope=SCOPE_ALL, keep_marks=True,
        ),
    ]


def default_config() -> RenderRulesConfig:
    """默认配置（默认规则集 + 默认色）。"""
    return RenderRulesConfig(
        rules=default_rules(),
        ai_default_color=COLOR_NARRATION,
        user_default_color=COLOR_NARRATION,
    )
