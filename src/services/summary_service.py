"""
总结服务：自动总结旧消息并自动折叠被总结的楼层。

流程：
  1. 检测未折叠消息数是否超过阈值
  2. 取最早的 N 条活跃消息
  3. 调用 AI 生成摘要（独立 API + SummaryPreset 提示词，回退会话 director_api_id 或角色绑定 API）
  4. 创建 summary 类型消息（时间戳紧跟最后一条被总结消息）
  5. 被总结的消息自动标记 collapsed=True, reason="auto_summary"
  6. 上下文构建时发送 summary 消息替代折叠消息 -> 节省 token + 新前缀基点
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from src.utils.debug import debug_log
from src.models import Message, Session, ApiConfig, Preset, SummaryPreset
from src.models.summary_preset import DEFAULT_SUMMARY_SYSTEM_PROMPT, DEFAULT_SUMMARY_SYSTEM_PROMPT_GROUP
from src.services.llm_client import LlmClient
from src.services.storage import Storage


# 旧硬编码提示词迁为 SummaryPreset.summary_prompt 默认值；保留旧名作 alias 兼容旧 import
SUMMARY_SYSTEM_PROMPT = DEFAULT_SUMMARY_SYSTEM_PROMPT


class SummaryService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def _jb_prefix(self) -> str:
        """读取破限前缀：开关关或 prefix 空返回空串。"""
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    def get_active_messages(self, messages: list[Message]) -> list[Message]:
        """获取未折叠、非总结、非纯图片的消息（按时间顺序）。"""
        return [
            m for m in messages
            if not m.collapsed and not m.is_summary and not m.is_image_only
        ]

    def needs_summary(self, session: Session, messages: list[Message]) -> bool:
        """检测是否需要自动总结。"""
        if not session.auto_summary_enabled:
            return False
        active = self.get_active_messages(messages)
        return len(active) >= session.auto_summary_threshold

    def summarize_and_collapse(
            self,
            session: Session,
            messages: list[Message],
            api_config: ApiConfig,
            preset: Preset,
            cancel_check=None,
    ) -> Optional[Message]:
        """
        执行自动总结并折叠被总结的楼层。
        返回创建的 summary 消息，失败/取消返回 None。

        api_config/preset 为编排器传来的回退 API 与预设（会话 director_api_id 或
        角色绑定 API）。这里会优先用 SummaryPreset 绑定的独立 API（若已绑定且启用），
        以及用 SummaryPreset 的提示词与生成参数替代旧硬编码提示词。

        cancel_check 透传给 llm.chat_cancelable，支持停止生成时中断总结调用。
        """
        active = self.get_active_messages(messages)
        if len(active) < session.auto_summary_count:
            return None

        # 取最早的 N 条活跃消息
        to_summarize = active[: session.auto_summary_count]

        # 构建对话文本
        conversation_parts = []
        for m in to_summarize:
            speaker = m.character_name or session.player_name
            conversation_parts.append(f"{speaker}：{m.content}")
        conversation = "\n".join(conversation_parts)

        # 独立上文总结配置：优先用 SummaryPreset 绑定的独立 API（建议便宜小模型），
        # 未绑定回退编排器传来的 api_config（会话 director_api_id 或角色绑定 API）。
        # 提示词按 session_type 选单/群聊版，生成参数用 SummaryPreset，缺字段回退默认。
        summary_preset_cfg = self.storage.load_summary_preset()
        s_api = api_config
        if summary_preset_cfg.api_id:
            preset_api = self.storage.load_api(summary_preset_cfg.api_id)
            if preset_api and preset_api.enabled:
                s_api = preset_api
        # 按 session_type 选单聊/群聊总结提示词
        is_group = getattr(session, "session_type", "single") == "group"
        if is_group:
            sys_prompt = summary_preset_cfg.system_prompt_group or DEFAULT_SUMMARY_SYSTEM_PROMPT_GROUP
        else:
            sys_prompt = summary_preset_cfg.system_prompt or DEFAULT_SUMMARY_SYSTEM_PROMPT

        # 复用角色绑定/导演回退预设取其参数默认；summary 生成参数由 SummaryPreset 主导
        gen_preset = Preset(
            name="summary",
            system_prompt=sys_prompt,
            temperature=summary_preset_cfg.temperature,
            max_tokens=summary_preset_cfg.max_tokens,
            top_p=summary_preset_cfg.top_p,
        )
        llm = LlmClient(s_api, gen_preset, jailbreak_prefix=self._jb_prefix())
        debug_log(lambda: f"[Summary] session={session.title or session.id[:8]} API={s_api.name}({s_api.model})")
        debug_log(lambda: f"[Summary] 入参 对话({len(to_summarize)} 条):\n{conversation[:800]}")
        # 用 chat_cancelable 透传 cancel_check，支持停止生成时中断总结
        result = llm.chat_cancelable([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": conversation},
        ], cancel_check=cancel_check)
        debug_log(lambda: f"[Summary] 出参:\n{result.content or '（空/失败）'}")

        if result.cancelled or result.error or not result.content:
            return None

        # 创建 summary 消息，时间戳紧跟最后一条被总结消息
        last_ts = to_summarize[-1].timestamp
        try:
            last_dt = datetime.fromisoformat(last_ts)
            summary_ts = (last_dt + timedelta(milliseconds=1)).isoformat()
        except (ValueError, TypeError):
            summary_ts = last_ts

        summary_msg = Message(
            session_id=session.id,
            role="summary",
            character_name="系统总结",
            content=result.content.strip(),
            timestamp=summary_ts,
            tokens=result.usage.completion_tokens,
            summary_of=[m.id for m in to_summarize],
        )

        # 标记被总结的消息为折叠
        for m in to_summarize:
            m.collapsed = True
            m.collapsed_reason = "auto_summary"
            self.storage.update_message(m)

        # 保存 summary 消息
        self.storage.save_message(summary_msg)

        return summary_msg

    def manual_collapse(
            self, messages: list[Message], start_id: str, end_id: str
    ) -> list[Message]:
        """手动折叠指定范围内的消息。"""
        ids = [m.id for m in messages]
        try:
            start_idx = ids.index(start_id)
            end_idx = ids.index(end_id)
        except ValueError:
            return messages
        if start_idx > end_idx:
            start_idx, end_idx = end_idx, start_idx
        for m in messages[start_idx: end_idx + 1]:
            if not m.is_summary and not m.is_image_only:
                m.collapsed = True
                m.collapsed_reason = "manual"
                self.storage.update_message(m)
        return messages

    def uncollapse(self, messages: list[Message], msg_id: str):
        """取消折叠单条消息。"""
        for m in messages:
            if m.id == msg_id:
                m.collapsed = False
                m.collapsed_reason = ""
                self.storage.update_message(m)
                break