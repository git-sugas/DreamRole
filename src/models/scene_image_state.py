"""场景事件生图状态（世界模拟 P2 场景交互循环，关键叙事回合自动出图）。

独立于 World JSON 与 SceneLog（守 §21「场景日志独立持久化」+ World JSON 精简）：
- 存 data/worlds/{id}_scene_images.json（仿 _scene.json 模式）
- 不污染 SceneLog（场景日志只记对话流，§21a §21b 强调）
- 不污染 World JSON（World 是世界静态定义）

[!] 状态语义：
- last_image_tick：本场景最近一次出图时的 world.tick_count（频次限频用）
- generated_count：本场景累计已生图数（上限限频用）
- images_by_entry：旁白条目 ID 到图片文件名的映射（重进场景恢复插图，不送入 LLM 上下文）
- 跨场景（重新进入同一世界）状态保留：用户不主动清就一直累加

删除世界时由 storage.delete_world 级联清理（仿 delete_scene 防孤儿）。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
import os


def _now() -> str:
    return datetime.now().isoformat()


def _nonnegative_count(value: object) -> int:
    try:
        return min(1_000_000_000, max(0, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass
class SceneImageState:
    """场景事件生图状态。"""
    world_id: str = ""
    last_image_tick: int = 0        # 最近出图时的 world.tick_count（0 = 还没出过）
    generated_count: int = 0        # 本场景累计已生图数
    images_by_entry: dict[str, list[str]] = field(default_factory=dict)  # 旁白条目 ID -> 插图文件名
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "world_id": self.world_id,
            "last_image_tick": self.last_image_tick,
            "generated_count": self.generated_count,
            "images_by_entry": {k: list(v) for k, v in self.images_by_entry.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def touch(self):
        """更新 updated_at（仿 SceneLog.touch / World.touch / Character.touch 守 §11 契约）。"""
        self.updated_at = _now()

    @staticmethod
    def _valid_filename(value: object) -> bool:
        return (isinstance(value, str) and bool(value)
                and os.path.basename(value) == value and "\\" not in value
                and value.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))

    def attach(self, entry_id: str, filenames: list[str]) -> bool:
        """给已落盘的旁白绑定插图；重复信号不会重复插入。"""
        if not entry_id:
            return False
        valid = [name for name in filenames if self._valid_filename(name)]
        if not valid:
            return False
        bucket = self.images_by_entry.setdefault(entry_id, [])
        changed = False
        for name in valid:
            if name not in bucket:
                bucket.append(name)
                changed = True
        if changed:
            self.touch()
        return changed

    def forget(self, entry_id: str) -> bool:
        if entry_id in self.images_by_entry:
            del self.images_by_entry[entry_id]
            self.touch()
            return True
        return False

    @classmethod
    def from_dict(cls, d: dict) -> "SceneImageState":
        if not d or not isinstance(d, dict):
            return cls()
        raw_images = d.get("images_by_entry") or {}
        images = {}
        if isinstance(raw_images, dict):
            for key, values in raw_images.items():
                if isinstance(key, str) and key and isinstance(values, list):
                    names = [v for v in values if cls._valid_filename(v)]
                    if names:
                        images[key] = list(dict.fromkeys(names))
        return cls(
            world_id=d.get("world_id", "") or "",
            # 整数钳制 [0, 1e9]：防脏数据/手改 JSON 致崩
            last_image_tick=_nonnegative_count(d.get("last_image_tick")),
            generated_count=_nonnegative_count(d.get("generated_count")),
            images_by_entry=images,
            created_at=d.get("created_at", "") or _now(),
            updated_at=d.get("updated_at", "") or _now(),
        )
