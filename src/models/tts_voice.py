"""TTS 音色设定数据模型。

一个音色设定 = 一个 JSON 文件（多文件存储，仿 ApiConfig 模式），
存于 data/tts_voices/{id}.json。

音色设定包含 Fish Audio 的 reference_id（voice model id）+ 通用生成参数
（format/latency/speed）+ 非通用自定义参数（extra_params，键值对列表，
兼容不同 TTS 服务的差异化字段，如 prosody.volume/chunk_length 等）。

Fish Audio API key 与 base_url 不存于此（所有音色共用，统一存于 TtsPreset）。
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat()


# format 白名单（Fish Audio S2 支持的输出格式）
_VALID_FORMATS = {"mp3", "wav", "pcm", "opus"}

# latency 白名单
_VALID_LATENCY = {"balanced", "normal"}


@dataclass
class TtsVoice:
    """单个 TTS 音色设定。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""                       # 备注名（如「温柔」「心声」「旁白」）
    reference_id: str = ""               # Fish Audio voice model id
    format: str = "mp3"                  # 输出格式：mp3/wav/pcm/opus
    latency: str = "balanced"            # 延迟模式：balanced/normal
    speed: float = 1.0                    # 语速 0.5-2.0
    # 非通用自定义参数：键值对列表，兼容不同 TTS 服务的差异化字段。
    # 例如 Fish Audio 的 prosody.volume（0）、chunk_length（150）等。
    # 值统一存 str，调用时按需转 int/float/str 透传到 TTS 请求 body。
    extra_params: list[dict] = field(default_factory=list)
    # [{"key": "prosody.volume", "value": "0"}, {"key": "chunk_length", "value": "150"}]
    enabled: bool = True
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self):
        """更新时间戳（与 Character/WorldBook.touch 一致，保存前调用）。"""
        self.updated_at = _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "reference_id": self.reference_id,
            "format": self.format,
            "latency": self.latency,
            "speed": self.speed,
            "extra_params": [dict(p) for p in self.extra_params],
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TtsVoice":
        if not d:
            return cls()
        # format/latency 白名单校验（防脏数据）
        fmt = d.get("format", "mp3")
        if fmt not in _VALID_FORMATS:
            fmt = "mp3"
        latency = d.get("latency", "balanced")
        if latency not in _VALID_LATENCY:
            latency = "balanced"
        # extra_params 容错：null/非 list 回退空列表
        raw_params = d.get("extra_params") or []
        extra_params = []
        for p in raw_params:
            if isinstance(p, dict) and "key" in p:
                extra_params.append({"key": str(p["key"]), "value": str(p.get("value", ""))})
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            reference_id=d.get("reference_id", "") or "",
            format=fmt,
            latency=latency,
            speed=float(d.get("speed", 1.0) or 1.0),
            extra_params=extra_params,
            enabled=bool(d.get("enabled", True)),
            created_at=d.get("created_at", _now()) or _now(),
            updated_at=d.get("updated_at", _now()) or _now(),
        )


def default_tts_voice() -> TtsVoice:
    """默认音色设定（新建音色时的初始值）。"""
    return TtsVoice()
