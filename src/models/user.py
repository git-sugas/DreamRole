"""用户数据模型（轻量实体：姓名 + 头像 + 描述）。

与 Character 平行：会话可选绑定一个 User（session.user_id），其 name 用作 {{user}}
替换、注入 BLOCK_USER；avatar 显示在用户消息气泡右侧；description 注入 BLOCK_USER
作「用户设定」。头像复用 paths.avatars_dir()（与角色共享）。
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


@dataclass
class User:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    avatar: str = ""               # 头像文件名（存于 avatars 目录，与角色共享）
    description: str = ""          # 用户描述 / 人设
    appearance_tags: str = ""      # 固定外貌 tag（原生英文 Danbooru tag 逗号分隔，生图时注入 LLM 不参与召回）
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "avatar": self.avatar,
            "description": self.description,
            "appearance_tags": self.appearance_tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",                 # [!] or "" 防手改 JSON 存 null 致下游 None（§12）
            avatar=d.get("avatar", "") or "",
            description=d.get("description", "") or "",
            appearance_tags=d.get("appearance_tags", "") or "",   # [!] or "" 防手改 JSON 存 null（与角色卡一致）
            created_at=d.get("created_at", _now()),
            updated_at=d.get("updated_at", _now()),
        )

    def touch(self):
        self.updated_at = _now()