"""聊天会话数据模型。"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    session_type: str = "single"   # "single" | "group"
    character_ids: list[str] = field(default_factory=list)
    world_book_id: str = ""
    player_name: str = "用户"
    user_id: str = ""          # 绑定的 User 实体 id（绑定时取该 User 的 name/avatar/description）
    # ---- 群聊设置 ----
    group_mode: str = "manual"     # "auto"（API选下一个发言者）| "manual"（点击头像）
    director_api_id: str = ""      # 导演 API（auto 模式用，决定下一个发言者）
    # [!] 「直接发消息」的默认发言者 id（追踪最近一次实际选中的发言者）：
    # 创建会话时记录开场白选的角色；任何模式选角成功后经 _remember_speaker
    # 更新（auto 导演选角 / auto 点头像临时干预 / manual 点头像）。
    # 为空时回退 character_ids[0]（向后兼容老会话）。失败/取消不更新。
    default_speaker_id: str = ""
    # ---- 自动总结 ----
    auto_summary_enabled: bool = True
    auto_summary_threshold: int = 30   # 未折叠消息超过此数触发总结
    auto_summary_count: int = 15       # 每次总结最早多少条
    # ---- 流式 ----
    streaming: bool = True
    # ---- 元信息 ----
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "session_type": self.session_type,
            "character_ids": self.character_ids,
            "world_book_id": self.world_book_id,
            "player_name": self.player_name,
            "user_id": self.user_id,
            "group_mode": self.group_mode,
            "director_api_id": self.director_api_id,
            "default_speaker_id": self.default_speaker_id,
            "auto_summary_enabled": self.auto_summary_enabled,
            "auto_summary_threshold": self.auto_summary_threshold,
            "auto_summary_count": self.auto_summary_count,
            "streaming": self.streaming,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        # 枚举字段白名单校验
        session_type = d.get("session_type", "single")
        if session_type not in ("single", "group"):
            session_type = "single"
        group_mode = d.get("group_mode", "manual")
        if group_mode not in ("manual", "auto"):
            group_mode = "manual"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d.get("title", "") or "",
            session_type=session_type,
            character_ids=d.get("character_ids") or [],
            world_book_id=d.get("world_book_id", "") or "",
            player_name=d.get("player_name", "用户") or "用户",
            user_id=d.get("user_id", "") or "",
            group_mode=group_mode,
            director_api_id=d.get("director_api_id", "") or "",
            default_speaker_id=d.get("default_speaker_id", "") or "",
            auto_summary_enabled=bool(d.get("auto_summary_enabled", True)),
            auto_summary_threshold=int(d.get("auto_summary_threshold", 30) or 30),
            auto_summary_count=int(d.get("auto_summary_count", 15) or 15),
            streaming=bool(d.get("streaming", True)),
            created_at=d.get("created_at", _now()),
            updated_at=d.get("updated_at", _now()),
        )

    def touch(self):
        self.updated_at = _now()