"""世界书数据模型（参考 SillyTavern Lorebook）。"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field


@dataclass
class WorldBookEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    keys: list[str] = field(default_factory=list)        # 触发关键词
    content: str = ""                                     # 注入内容
    enabled: bool = True
    insertion_order: int = 100                            # 注入顺序（小的在前）
    position: str = "before_char"  # before_char | after_char | before_an | after_an | at_top | at_bottom
    case_sensitive: bool = False
    selective: bool = False                               # True 时需 secondary_keys 也匹配
    secondary_keys: list[str] = field(default_factory=list)
    constant: bool = False                               # True 时始终注入（不依赖关键词）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "keys": self.keys,
            "content": self.content,
            "enabled": self.enabled,
            "insertion_order": self.insertion_order,
            "position": self.position,
            "case_sensitive": self.case_sensitive,
            "selective": self.selective,
            "secondary_keys": self.secondary_keys,
            "constant": self.constant,
        }

    @classmethod
    def from_dict(cls, d: dict) -> WorldBookEntry:
        # position 白名单校验
        position = d.get("position", "before_char")
        if position not in ("before_char", "after_char", "before_an", "after_an", "at_top", "at_bottom"):
            position = "before_char"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            keys=d.get("keys") or [],
            content=d.get("content", "") or "",
            enabled=bool(d.get("enabled", True)),
            insertion_order=int(d.get("insertion_order", 100) or 100),
            position=position,
            case_sensitive=bool(d.get("case_sensitive", False)),
            selective=bool(d.get("selective", False)),
            secondary_keys=d.get("secondary_keys") or [],
            constant=bool(d.get("constant", False)),
        )


@dataclass
class WorldBook:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    entries: list[WorldBookEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> WorldBook:
        raw_entries = d.get("entries") or []
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            entries=[WorldBookEntry.from_dict(e) for e in raw_entries if isinstance(e, dict)],
        )