"""TTS 预设：全局 TTS 连接配置 + 情绪控制 LLM 预设。

单例文件 data/tts_preset.json（仿 SummaryPreset 模式）。

两类配置共存于一个文件：
1. TTS 连接基础设置：tts_api_key / tts_base_url / auto_tts_enabled（全局自动 TTS 开关）。
   所有音色共用这一个 Fish Audio API key（安全+复用，不存音色里）。
2. 情绪控制 LLM 预设：emotion_enabled（开关）/ api_id（独立 LLM）/ system_prompt /
   temperature/max_tokens/top_p。开启后对正则命中的文本段调 LLM 加工（插入 S2 方括号
   情绪标记），再用返回文本调 TTS；关闭则直接用原文调 TTS。

只兼容 Fish Audio S2 模型（方括号 [happy] 情绪语法）。
"""
from __future__ import annotations
from dataclasses import dataclass


# 默认情绪标注提示词：要求 LLM 对输入的文本片段（带完整气泡上下文），按语境插入 S2
# 方括号情绪标记，保留原文不增删内容，只加情绪标记，输出纯文本。附 Fish Audio 支持的
# 常用情绪标签（已精简去重：去掉语气/音效/特殊效果标记，近义情绪合并选常用词）。
# 含多片段 / 分隔契约（批量加工，见 tts_service._emotion_enhance_batch）。
DEFAULT_TTS_EMOTION_SYSTEM_PROMPT = (
    "你是一个语音情绪标注助手。我会给你一段角色扮演对话中的文本片段，"
    "同时提供该片段所属的完整气泡内容作为上下文，请你根据上下文语境，"
    "为这段文本片段添加合适的情绪标记，让语音合成时情绪更自然。\n\n"
    "规则：\n"
    "1. 使用 Fish Audio S2 模型的方括号语法。情绪标记放在对应分句开头，"
    "一句话里情绪变化时要分别标注，不要整句只贴一个情绪词。\n"
    "2. 可组合多个标记，如 [sad][whispering] 我好想你\n"
    "3. 可用强度修饰，如 [very excited] 这太棒了！[slightly sad] 有点失望\n"
    "4. 保留原文内容，不增删任何文字，只添加情绪标记\n"
    "5. 一次输入多个片段时用 / 分隔，输出也必须用 / 分隔成相同数量的片段，"
    "不要合并、拆分或增删片段数量\n"
    "6. 只输出带标记的文本，不要输出任何说明、解释或前后缀\n\n"
    "标注示例（一句多情绪，按语气转折分段标注）：\n"
    "[calm] 哟，这就是大姐姐新挑的小厮？ [sarcastic] 倒是生得清秀，不像个干粗活的料。 "
    "[confident] 你要是机灵，跟我去做事，月钱比书房多三成。 [disdainful] 别告诉大姐姐是我来挖人的，啊？\n\n"
    "[angry] 你怎么才来！ [worried] 我还以为你出了什么事…… [embarrassed] 没、没什么，就是铺子那边对不上账，急而已。\n\n"
    "常用情绪标签参考（从以下清单中选择最合适的，也可用自然语言描述如 [mysterious]）：\n\n"
    "happy/sad/angry/excited/calm/nervous/confident/surprised/scared/worried/"
    "frustrated/depressed/empathetic/embarrassed/disgusted/moved/proud/relaxed/"
    "grateful/curious/sarcastic/disdainful/disappointed/guilty/jealous/hopeful/"
    "determined/resigned/nostalgic/lonely/bored/confused/anxious\n\n"
    "强度修饰词：slightly / very / extremely（如 [slightly sad] [very excited] [extremely angry]）"
)


@dataclass
class TtsPreset:
    """TTS 全局配置 + 情绪控制 LLM 预设（单例）。"""
    id: str = "tts_preset"               # 固定单例 id
    # ---- TTS 连接基础设置（所有音色共用）----
    tts_api_key: str = ""                # Fish Audio API key（所有音色共用）
    tts_base_url: str = "https://api.fish.audio"  # Fish Audio API base url
    tts_proxy: str = ""                  # 代理地址（如 http://127.0.0.1:7890，空=不走代理）
    tts_model: str = "s2.1-pro-free"    # TTS 模型（s2.1-pro/s2.1-pro-free/s2-pro/s1）
    auto_tts_enabled: bool = False      # 全局自动 TTS 开关（开=每条 LLM 回复自动生成 TTS 音频）
    auto_play_enabled: bool = True      # 生成 TTS 后是否自动播放（关=仅生成音频，点气泡句子播放）
    # ---- 情绪控制 LLM 设置 ----
    emotion_enabled: bool = False        # 情绪 LLM 开关：关=直接原文 TTS，开=LLM 加工后 TTS
    api_id: str = ""                    # 绑定独立 LLM API（情绪标注用；空=回退会话 API/首个 enabled）
    system_prompt: str = DEFAULT_TTS_EMOTION_SYSTEM_PROMPT  # 情绪标注提示词
    temperature: float = 0.7
    max_tokens: int = 1024
    top_p: float = 0.7

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tts_api_key": self.tts_api_key,
            "tts_base_url": self.tts_base_url,
            "tts_proxy": self.tts_proxy,
            "tts_model": self.tts_model,
            "auto_tts_enabled": self.auto_tts_enabled,
            "auto_play_enabled": self.auto_play_enabled,
            "emotion_enabled": self.emotion_enabled,
            "api_id": self.api_id,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TtsPreset":
        if not d:
            return cls()
        return cls(
            id=d.get("id", "tts_preset"),
            tts_api_key=d.get("tts_api_key", "") or "",
            tts_base_url=d.get("tts_base_url", "https://api.fish.audio") or "https://api.fish.audio",
            tts_proxy=d.get("tts_proxy", "") or "",
            tts_model=d.get("tts_model", "") or "s2.1-pro-free",
            auto_tts_enabled=bool(d.get("auto_tts_enabled", False)),
            # 老配置无此字段默认 True（向后兼容：原 auto_tts_enabled 同时控生成+播放，
            # 拆分后 auto_play_enabled 默认开保持「生成即播」的旧体验）
            auto_play_enabled=bool(d.get("auto_play_enabled", True)),
            emotion_enabled=bool(d.get("emotion_enabled", False)),
            api_id=d.get("api_id", "") or "",
            # 空 system_prompt 回退默认（与 SummaryPreset/DanbooruPreset 一致）
            system_prompt=d.get("system_prompt", "") or DEFAULT_TTS_EMOTION_SYSTEM_PROMPT,
            temperature=float(d.get("temperature", 0.7) or 0.7),
            max_tokens=int(d.get("max_tokens", 1024) or 1024),
            top_p=float(d.get("top_p", 0.7) or 0.7),
        )


def default_tts_preset() -> TtsPreset:
    return TtsPreset()
