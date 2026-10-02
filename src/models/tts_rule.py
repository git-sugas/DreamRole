"""TTS 正则规则数据模型。

正则规则层：用户配置多条正则，每条匹配某种文本段（如心声、对话台词）并指定
用哪个 TTSVoice 配音。匹配不上的文本段不配音。

存于 data/tts_rules.json（单文件，仿 RenderRulesConfig 模式）。
pattern 存字符串，使用时编译（仿 markup._rebuild_compiled 容错编译）。
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field


@dataclass
class TtsRule:
    """单条 TTS 正则规则。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""              # 规则名（如「心声」「对话台词」）
    pattern: str = ""           # 正则字符串（如 「[^」]*」）
    voice_id: str = ""           # 命中后用哪个 TtsVoice.id 配音
    enabled: bool = True
    priority: int = 100          # 数字小先匹配（finditer 跨规则合并时按此排序）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "pattern": self.pattern,
            "voice_id": self.voice_id,
            "enabled": self.enabled,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TtsRule":
        if not d:
            return cls()
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            pattern=d.get("pattern", "") or "",
            voice_id=d.get("voice_id", "") or "",
            enabled=bool(d.get("enabled", True)),
            priority=int(d.get("priority", 100) or 100),
        )


@dataclass
class TtsRulesConfig:
    """全局 TTS 正则规则配置。"""
    rules: list[TtsRule] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TtsRulesConfig":
        if not d:
            return cls()
        raw_rules = d.get("rules") or []
        return cls(
            rules=[TtsRule.from_dict(r) for r in raw_rules if isinstance(r, dict)],
        )


def default_tts_rules() -> TtsRulesConfig:
    """默认 TTS 规则集。

    覆盖角色扮演常见的对话格式：心声（括号）、对话台词（中/英引号/书名号）、
    动作旁白（星号）。voice_id 留空，用户在设置里建好音色后手动关联。
    与 render_rules.py 的默认正则模式对齐，保证配色命中的段落 TTS 也能命中。
    """
    return TtsRulesConfig(rules=[
        TtsRule(
            name="心声（括号）",
            pattern=r"（[^）]*）|\([^)]*\)",
            voice_id="",
            enabled=True,
            priority=10,
        ),
        TtsRule(
            name='对话「中引号」',
            pattern=r"「[^」]*」",
            voice_id="",
            enabled=True,
            priority=20,
        ),
        TtsRule(
            name='对话 "双引号"',
            pattern=r'"[^"]*"',
            voice_id="",
            enabled=True,
            priority=30,
        ),
        TtsRule(
            name="对话『书名号』",
            pattern=r"『[^』]*』",
            voice_id="",
            enabled=True,
            priority=40,
        ),
        TtsRule(
            name="动作 *旁白*",
            pattern=r"\*[^*]*\*",
            voice_id="",
            enabled=True,
            priority=50,
        ),
    ])
