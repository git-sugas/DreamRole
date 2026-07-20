"""角色卡数据模型（参考 SillyTavern 角色卡格式）。"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


@dataclass
class Character:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""          # 角色描述
    personality: str = ""          # 性格
    scenario: str = ""             # 场景
    first_message: str = ""        # 第一条消息（开场白）
    mes_example: str = ""          # 对话示例
    alternate_greetings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    creator: str = ""
    avatar: str = ""               # 头像文件名（存于 avatars 目录）
    appearance_tags: str = ""      # 固定外貌 tag（原生英文 Danbooru tag 逗号分隔，生图时注入 LLM 不参与召回）
    # ---- 绑定 ----
    api_id: str = ""               # 绑定的 API id
    # ---- 记忆 ----
    memory_mode: str = "none"      # "none" | "summary" | "embedding_hybrid"
    memory_config: dict = field(default_factory=lambda: {
        "summary_interval": 20,     # summary 模式：每隔多少条消息触发一次总结
        "summary_window": 20,       # summary 模式：每次总结时取最近多少条对话喂给 AI（与触发间隔解耦）
        "embedding_interval": 1,    # embedding_hybrid 模式：每隔多少条 assistant 消息整理一次（1=每条都整理；越大越省整理 API）
        # embedding_hybrid 模式注入条数走 MemoryPreset.hybrid_recall_top_k（在记忆整理 tab 配置），不在此处。
    })
    # ---- 元信息 ----
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "personality": self.personality,
            "scenario": self.scenario,
            "first_message": self.first_message,
            "mes_example": self.mes_example,
            "alternate_greetings": self.alternate_greetings,
            "tags": self.tags,
            "creator": self.creator,
            "avatar": self.avatar,
            "appearance_tags": self.appearance_tags,
            "api_id": self.api_id,
            "memory_mode": self.memory_mode,
            "memory_config": self.memory_config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Character:
        # [!] list/dict 字段 null 防御：JSON 显式存 null 时 d.get 返回 None，迭代会崩。
        # memory_config 深合并默认值：老数据缺字段时补默认（避免下游 KeyError）。
        default_cfg = {
            "summary_interval": 20,
            "summary_window": 20,
            "embedding_interval": 1,
        }
        raw_cfg = d.get("memory_config") or {}
        if isinstance(raw_cfg, dict):
            merged_cfg = {**default_cfg, **raw_cfg}
        else:
            merged_cfg = dict(default_cfg)
        # memory_mode 白名单校验（旧 embedding 模式已下线，降级为 none；用户可手动重选 embedding_hybrid）
        mode = d.get("memory_mode", "none")
        if mode == "embedding":
            mode = "none"
        elif mode not in ("none", "summary", "embedding_hybrid"):
            mode = "none"
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            description=d.get("description", "") or "",
            personality=d.get("personality", "") or "",
            scenario=d.get("scenario", "") or "",
            first_message=d.get("first_message", "") or "",
            mes_example=d.get("mes_example", "") or "",
            alternate_greetings=d.get("alternate_greetings") or [],
            tags=d.get("tags") or [],
            creator=d.get("creator", "") or "",
            avatar=d.get("avatar", "") or "",
            appearance_tags=d.get("appearance_tags", "") or "",
            api_id=d.get("api_id", "") or "",
            memory_mode=mode,
            memory_config=merged_cfg,
            created_at=d.get("created_at", _now()),
            updated_at=d.get("updated_at", _now()),
        )

    def touch(self):
        self.updated_at = _now()