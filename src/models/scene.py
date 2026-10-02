"""场景日志模型（世界模拟 P2 场景交互循环）。

独立于 World JSON，单独存 data/worlds/{id}_scene.json（守 §21「无需改 schema」+
保持 World JSON 精简：世界定义静态，场景会话动态）。

SceneLog 聚合 SceneEntry 列表：
  - role=player   玩家行动（选项 hint 或自由输入文本）
  - role=narrator 叙事旁白（叙事 LLM 流式输出）
  - role=system   系统提示（如「已移动到 X」「行动不可行」），P2 少量使用

P2 唯一结构化状态变更在 World 上（player.location_id / tick_count），场景日志只记
叙述流；P3/P4 的背包/任务进度/战斗结果可继续往 meta 里塞或扩字段。
所有子实体均 dataclass + to_dict/from_dict + 枚举白名单 + 强制类型 + or 兜底（守 §11）。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
import uuid


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# 枚举白名单（from_dict 校验防脏数据）
_SCENE_ROLE_VALUES = ("player", "narrator", "system")


@dataclass
class SceneEntry:
    """单条场景日志。"""
    id: str = field(default_factory=_new_id)   # [P23] 唯一标识（右键删除回指用）
    role: str = "narrator"          # player | narrator | system
    content: str = ""               # 文本内容（player=行动文本，narrator=旁白，system=提示）
    timestamp: str = field(default_factory=_now)
    tick: int = 0                   # 所属世界回合（= 写入时 world.tick_count）
    meta: dict = field(default_factory=dict)   # 扩展（如 intent_type/options 等结构化尾巴）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "tick": self.tick,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SceneEntry":
        if not d or not isinstance(d, dict):
            return cls()
        r = d.get("role", "narrator") or "narrator"
        if r not in _SCENE_ROLE_VALUES:
            r = "narrator"
        meta = d.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        eid = str(d.get("id", "") or "").strip()
        return cls(
            id=eid if eid else _new_id(),   # 旧存档无 id 自动补，向后兼容
            role=r,
            content=d.get("content", "") or "",
            timestamp=d.get("timestamp", _now()),
            tick=int(d.get("tick", 0) or 0),
            meta=dict(meta),
        )


@dataclass
class SceneLog:
    """场景会话日志（一世界一份，独立 JSON）。"""
    world_id: str = ""
    tick: int = 0                   # 场景内回合计数（与 world.tick_count 同步推进）
    log: list[SceneEntry] = field(default_factory=list)
    # [P15a] 前情摘要：最老日志条目滚动压缩成的连贯段落（compress_scene_history 维护）。
    # 空串 = 尚无摘要（短局未触发阈值）。摘要与最近若干条全文一起进场景上下文。
    summary: str = ""
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "world_id": self.world_id,
            "tick": self.tick,
            "log": [e.to_dict() for e in self.log],
            "summary": self.summary,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SceneLog":
        if not d or not isinstance(d, dict):
            return cls()
        return cls(
            world_id=d.get("world_id", "") or "",
            tick=int(d.get("tick", 0) or 0),
            log=[SceneEntry.from_dict(e) for e in (d.get("log") or []) if isinstance(e, dict)],
            summary=d.get("summary", "") or "",
            updated_at=d.get("updated_at", _now()),
        )

    def touch(self):
        self.updated_at = _now()

    def append(self, role: str, content: str, tick: int = 0, meta: dict | None = None) -> SceneEntry:
        """便捷追加一条日志并返回。"""
        e = SceneEntry(role=role, content=content, tick=tick, meta=meta or {})
        self.log.append(e)
        return e

    def recent(self, n: int = 8) -> list[SceneEntry]:
        """取最近 n 条（送 LLM 上下文用）。"""
        if n <= 0 or not self.log:
            return []
        return self.log[-n:]

    def delete_entry(self, entry_id: str) -> bool:
        """[P23] 按 id 删除单条日志（右键删除旁白用）。命中则移除 + touch，返回是否命中。"""
        if not entry_id:
            return False
        for i, e in enumerate(self.log):
            if e.id == entry_id:
                del self.log[i]
                self.touch()
                return True
        return False
