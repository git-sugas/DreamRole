"""上文总结预设：供「上文自动总结」独立绑定 API + 可编辑提示词 + 生成参数。

独立于正文 API：触发自动总结时优先用这里绑定的 api_id（建议一个便宜小模型专门跑
总结，省 token），未绑定则回退会话 director_api_id 或角色绑定 API。

提示词默认沿用历史硬编码的 SUMMARY_SYSTEM_PROMPT，迁为可编辑；老数据无此文件时
返回默认值，行为与旧硬编码版本零变化。
"""
from __future__ import annotations
from dataclasses import dataclass


# 默认上文总结提示词（单聊版）。输入格式为多行「角色名：内容」，输出纯摘要正文
# （代码会包 <summary> 标签注入上下文，故 LLM 不可自加标签/标题/前缀）。
DEFAULT_SUMMARY_SYSTEM_PROMPT = (
    "你是一个对话总结助手。请将以下角色扮演对话总结为简洁的摘要，保留：\n"
    "1. 关键事件和剧情发展\n"
    "2. 角色之间的关系和互动\n"
    "3. 重要的场景和环境信息\n"
    "4. 角色的情绪和心理状态变化\n\n"
    "输入格式为多行「角色名：发言内容」，请注意区分各发言者。\n"
    "请用第三人称叙述，控制在300字以内。\n"
    "只输出摘要正文，不要输出任何标签、标题、前缀或说明性文字。"
)

# 默认上文总结提示词（群聊版）。群聊总结需保留发言者归属，避免多人对话压成一段
# 后丢失"谁说了什么"。
DEFAULT_SUMMARY_SYSTEM_PROMPT_GROUP = (
    "你是一个群聊对话总结助手。请将以下群聊角色扮演对话总结为简洁的摘要，保留：\n"
    "1. 关键事件和剧情发展\n"
    "2. 各角色之间的关系和互动（注明是哪个角色）\n"
    "3. 重要的场景和环境信息\n"
    "4. 各角色的情绪和心理状态变化\n\n"
    "输入格式为多行「角色名：发言内容」，群聊中有多个角色发言，请务必在摘要中\n"
    "保留发言者归属（如「艾莉说明了魔法来源」「旅人询问了路线」），不要混作一团。\n"
    "请用第三人称叙述，控制在400字以内。\n"
    "只输出摘要正文，不要输出任何标签、标题、前缀或说明性文字。"
)


@dataclass
class SummaryPreset:
    """上文总结预设（单例，与 MemoryPreset 同样独立于正文 API 配置）。

    系统提示分单聊/群聊两版，按触发总结时的 session_type 选用。
    """
    id: str = "summary_preset"   # 固定单例 id
    api_id: str = ""            # 绑定独立 API（用于上文总结调用；空=回退会话 director_api_id 或角色绑定 API）
    system_prompt: str = DEFAULT_SUMMARY_SYSTEM_PROMPT                # 单聊总结提示词
    system_prompt_group: str = DEFAULT_SUMMARY_SYSTEM_PROMPT_GROUP    # 群聊总结提示词
    temperature: float = 0.3
    max_tokens: int = 512
    top_p: float = 0.9

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "api_id": self.api_id,
            "system_prompt": self.system_prompt,
            "system_prompt_group": self.system_prompt_group,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SummaryPreset":
        if not d:
            return cls()
        return cls(
            id=d.get("id", "summary_preset"),
            api_id=d.get("api_id", ""),
            system_prompt=d.get("system_prompt", DEFAULT_SUMMARY_SYSTEM_PROMPT),
            # 老数据无 system_prompt_group 字段回退默认群聊版
            system_prompt_group=d.get("system_prompt_group", DEFAULT_SUMMARY_SYSTEM_PROMPT_GROUP),
            temperature=float(d.get("temperature", 0.3)),
            max_tokens=int(d.get("max_tokens", 512)),
            top_p=float(d.get("top_p", 0.9)),
        )


def default_summary_preset() -> SummaryPreset:
    return SummaryPreset()