"""聊天消息数据模型。"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


@dataclass
class Message:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    role: str = "user"             # "user" | "assistant" | "system" | "summary"
    character_id: str = ""         # 发言角色 id（user 消息为空）
    character_name: str = ""       # 发言者显示名
    content: str = ""              # 消息文本内容
    timestamp: str = field(default_factory=_now)
    # ---- 折叠 ----
    collapsed: bool = False        # 是否折叠（不发送给 API）
    collapsed_reason: str = ""     # "auto_summary" | "manual" | ""
    # ---- token ----
    tokens: int = 0                # 该消息 token 数
    # ---- 图片 ----
    image_path: str = ""           # 图片文件路径（非空则为图片消息）
    is_image_only: bool = False    # 纯图片消息（不入上下文，仅展示）
    # ---- 中断标记 ----
    is_stopped: bool = False        # 生成被用户停止时保存的部分回复标记
    # ---- 总结关联 ----
    summary_of: list[str] = field(default_factory=list)  # 该 summary 消息总结了哪些消息 id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "content": self.content,
            "timestamp": self.timestamp,
            "collapsed": self.collapsed,
            "collapsed_reason": self.collapsed_reason,
            "tokens": self.tokens,
            "image_path": self.image_path,
            "is_image_only": self.is_image_only,
            "summary_of": self.summary_of,
            "is_stopped": self.is_stopped,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Message:
        # role 白名单校验
        role = d.get("role", "user")
        if role not in ("user", "assistant", "system", "summary"):
            role = "user"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            session_id=d.get("session_id", "") or "",
            role=role,
            character_id=d.get("character_id", "") or "",
            character_name=d.get("character_name", "") or "",
            content=d.get("content", "") or "",
            timestamp=d.get("timestamp", _now()),
            collapsed=bool(d.get("collapsed", False)),
            collapsed_reason=d.get("collapsed_reason", "") or "",
            tokens=int(d.get("tokens", 0) or 0),
            image_path=d.get("image_path", "") or "",
            is_image_only=bool(d.get("is_image_only", False)),
            summary_of=d.get("summary_of") or [],
            is_stopped=bool(d.get("is_stopped", False)),
        )

    @property
    def is_summary(self) -> bool:
        return self.role == "summary"

    @property
    def is_image(self) -> bool:
        return bool(self.image_path) or self.is_image_only