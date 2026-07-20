"""
聊天编排器：协调上下文构建、记忆、总结、LLM 调用、图片生成、统计。

通过回调函数与 UI 交互（UI 在 QThread 中调用编排器方法）。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional

from src.models import Character, Message, Session, ApiConfig, Preset, WorldBook
from src.services.llm_client import LlmClient, LlmUsage
from src.services.context_builder import ContextBuilder
from src.services.memory_service import MemoryService
from src.services.summary_service import SummaryService
from src.services.stats_service import StatsService
from src.services.storage import Storage
from src.utils.helpers import parse_image_tags, remove_image_tags, contains_chinese


@dataclass
class ChatCallbacks:
    """UI 回调接口。"""
    on_chunk: Optional[Callable[[str], None]] = None
    on_usage: Optional[Callable[[str, LlmUsage], None]] = None
    on_message: Optional[Callable[[Message], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_speaker: Optional[Callable[[Character], None]] = None
    on_image: Optional[Callable[[str, str], None]] = None
    on_summary: Optional[Callable[[Message], None]] = None
    on_status: Optional[Callable[[str], None]] = None
    on_done: Optional[Callable[[], None]] = None
    # 手改模式下由 UI 弹窗返回用户勾选的 tag name 列表；返回 None=取消跳过此图。
    # 入参：(候选 TagCandidate 列表, 原始中文描述)（worker 线程调用，UI 需切到主线程弹窗阻塞返回）。
    on_danbooru_manual_select: Optional[Callable[[list, str], Optional[list]]] = None


class ChatOrchestrator:
    """聊天流程编排器。"""

    def __init__(
            self,
            storage: Storage,
            context_builder: ContextBuilder,
            memory_service: MemoryService,
            summary_service: SummaryService,
            stats_service: StatsService,
            comfyui_service=None,
            danbooru_service=None,
    ):
        self.storage = storage
        self.ctx_builder = context_builder
        self.memory = memory_service
        self.summary = summary_service
        self.stats = stats_service
        self.comfyui = comfyui_service
        self.danbooru = danbooru_service

    def _jb_prefix(self) -> str:
        """读取破限前缀：开关关或 prefix 空返回空串（LlmClient 收到空串不注入）。"""
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    def _load_api_and_preset(self, api_id):
        api = self.storage.load_api(api_id)
        if not api or not api.enabled:
            return None, None
        preset = self.storage.load_preset(api.preset_id) if api.preset_id else None
        if not preset:
            preset = Preset(name="default")
        return api, preset

    def _load_world_book(self, session):
        if session.world_book_id:
            return self.storage.load_world_book(session.world_book_id)
        return None

    def _load_group_members(self, session):
        members = []
        for cid in session.character_ids:
            char = self.storage.load_character(cid)
            if char:
                members.append(char)
        return members

    def _build_character_appearances(self, character, group_members):
        """构造会话内角色固定外貌 tag 列表 [(name, tags), ...]，过滤空 tag。

        - 群聊（group_members 非空）：全量成员的外貌 tag 都传，LLM 按中文描述选用
        - 单聊：只传当前角色
        - 过滤 appearance_tags 为空的角色；全空返回 None（向后兼容，不注入）
        """
        chars = group_members if group_members else ([character] if character else [])
        appear = []
        for c in chars:
            if c and c.name and c.appearance_tags and c.appearance_tags.strip():
                appear.append((c.name, c.appearance_tags.strip()))
        return appear if appear else None

    def _emit(self, cb, attr, *args):
        fn = getattr(cb, attr, None)
        if fn:
            fn(*args)

    def send_user_message(self, session, content, character_name=""):
        msg = Message(
            session_id=session.id,
            role="user",
            character_name=character_name or session.player_name,
            content=content,
        )
        self.storage.save_message(msg)
        session.touch()
        self.storage.save_session(session)
        return msg

    @staticmethod
    def _append_message(messages: list[Message], msg: Message):
        """安全地把消息加入内存列表，避免与 UI 层回调重复 append 导致同一条消息出现两次。

        UI 层（MainWindow._on_message_saved）会通过 on_message 回调把 user/assistant
        消息 append 到 current_messages；编排器若也 append 同一个引用，就会重复。
        这里统一去重：仅当该消息 id 不在列表中时才追加。
        """
        if msg and not any(m.id == msg.id for m in messages):
            messages.append(msg)

    def _save_pending_user(self, session, messages, content, name, callbacks):
        """立即存储本轮 user 消息并通知 UI（用于选角失败/LLM 报错时不丢用户输入）。

        与 generate_response 的延迟存储不同：此处是「用户输入已发生但无 assistant 回复」
        的兜底，避免用户消息丢失（与取消时存 user 语义一致）。
        """
        user_msg = Message(
            session_id=session.id, role="user",
            character_name=name or session.player_name,
            content=content,
        )
        self.storage.save_message(user_msg)
        self._append_message(messages, user_msg)
        self._emit(callbacks, "on_message", user_msg)

    def generate_response(self, session, messages, character, callbacks,
                          cancel_check=None,
                          pending_trigger="", pending_user_msg=None):
        """生成角色回复。

        本轮触发消息采用「延迟存储」：pending_trigger 传给 context_builder 的
        LAST_USER 块作为末尾 user 触发消息，此时尚未存入 messages/DB（故 HISTORY
        不含本轮）。LLM 回复后，若 pending_user_msg 不为 None，则先存 user 消息再存
        assistant 消息（保证历史时序正确）。

        - pending_trigger: 末尾 user 触发消息内容（用户真实输入或构造的"轮到X发言"）。
          续写场景传空串 -> LAST_USER 块返回空，末尾由 orchestrator 追加 prefill。
        - pending_user_msg: (content, name) 元组，回复后存为 user 消息；None 表示
          本轮无新 user 消息需存储（如续写）。
        """
        api, preset = self._load_api_and_preset(character.api_id)
        if not api:
            self._emit(callbacks, "on_error", f"角色 {character.name} 绑定的 API 不可用")
            return None

        # 自动总结（此时 messages 不含本轮 trigger，总结基于纯历史，正确）
        if self.summary.needs_summary(session, messages):
            self._emit(callbacks, "on_status", "正在总结上文...")
            s_api, s_preset = self._load_api_and_preset(
                session.director_api_id or character.api_id
            )
            if s_api and s_preset:
                smsg = self.summary.summarize_and_collapse(
                    session, messages, s_api, s_preset, cancel_check=cancel_check,
                )
                if smsg:
                    self._emit(callbacks, "on_summary", smsg)
                    messages = self.storage.load_messages(session.id)
            self._emit(callbacks, "on_status", "")

        world_book = self._load_world_book(session)
        group_members = self._load_group_members(session) if session.session_type == "group" else None

        # 记忆检索：query_text 含本轮 trigger，让检索能基于本轮内容
        query_text = ""
        if messages:
            recent = [m for m in messages if not m.collapsed and not m.is_image_only][-3:]
            query_text = " ".join(m.content for m in recent)
        if pending_trigger:
            query_text = (query_text + " " + pending_trigger).strip()
        if character.memory_mode == "embedding_hybrid":
            # hybrid 模式：两次召回合并（上一条 assistant + 本轮 user）
            # 第一次召回 query = 上一条 assistant 消息（首次对话用 first_message）
            assistant_query = ""
            if messages:
                last_asst = [m for m in messages if not m.collapsed and not m.is_image_only
                             and m.role == "assistant"][-1:]
                if last_asst:
                    assistant_query = last_asst[0].content
            if not assistant_query:
                assistant_query = character.first_message or ""
            memory_text = self.memory.get_hybrid_memory_text(
                character, api, assistant_query, pending_trigger or "",
                session_type=getattr(session, "session_type", "single"),
            )
        else:
            memory_text = self.memory.get_memory_text(character, query_text, api)

        # 上下文构建（pending_trigger -> LAST_USER 块；session_type 选单/群聊提示词）
        user = self.storage.load_user(session.user_id) if getattr(session, "user_id", "") else None
        api_messages, api_labels = self.ctx_builder.build_messages_with_labels(
            preset, character, session, messages,
            world_book=world_book, memory_text=memory_text,
            pending_trigger=pending_trigger,
            group_members=group_members, user=user,
        )

        # LLM 调用
        self._emit(callbacks, "on_status", f"{character.name} 正在思考...")
        llm = LlmClient(api, preset, jailbreak_prefix=self._jb_prefix())
        full_content = ""
        usage = LlmUsage()

        # 流式开关以 API 配置为准（适配不同模型/网关）
        cancelled = False
        if getattr(api, "streaming", True):
            for event_type, data in llm.chat_stream(
                    api_messages, cancel_check=cancel_check, block_labels=api_labels,
            ):
                if event_type == "text":
                    full_content += data
                    self._emit(callbacks, "on_chunk", data)
                elif event_type == "usage":
                    usage = data
                elif event_type == "error":
                    # [!] LLM 报错时用户输入已发生，先存 user 不丢失（与取消时存 user 一致）
                    if pending_user_msg is not None:
                        u_content, u_name = pending_user_msg
                        self._save_pending_user(session, messages, u_content, u_name, callbacks)
                    self._emit(callbacks, "on_error", data)
                    self._emit(callbacks, "on_status", "")
                    return None
                elif event_type == "cancelled":
                    cancelled = True
                    break
        else:
            # 非流式分支用 chat_cancelable 透传 cancel_check（至少在调用前后可检查取消，
            # 流式时可逐行中断）。原 llm.chat() 不收 cancel_check，停止生成在非流式下失效。
            result = llm.chat_cancelable(
                api_messages, cancel_check=cancel_check, block_labels=api_labels,
            )
            if result.cancelled:
                cancelled = True
            elif result.error:
                # [!] LLM 报错时用户输入已发生，先存 user 不丢失
                if pending_user_msg is not None:
                    u_content, u_name = pending_user_msg
                    self._save_pending_user(session, messages, u_content, u_name, callbacks)
                self._emit(callbacks, "on_error", result.error)
                self._emit(callbacks, "on_status", "")
                return None
            else:
                full_content = result.content
                usage = result.usage
                # 有内容才通知 UI（触发占位气泡建/升级）；空回复时由 on_message 走新增分支
                if full_content:
                    self._emit(callbacks, "on_chunk", full_content)

        # 统计（取消时仍记录已发生的 usage）
        self.stats.record_usage(api.id, usage)
        self._emit(callbacks, "on_usage", api.id, usage)

        # 延迟存储：先存本轮 user 消息（若有），再存 assistant 消息。
        # 取消时也要先存 user（用户输入已发生不能丢），再存 assistant 部分文本。
        if pending_user_msg is not None:
            u_content, u_name = pending_user_msg
            user_msg = Message(
                session_id=session.id, role="user",
                character_name=u_name or session.player_name,
                content=u_content,
            )
            self.storage.save_message(user_msg)
            self._append_message(messages, user_msg)
            self._emit(callbacks, "on_message", user_msg)

        # 保存 assistant 消息（取消时若有已收到的部分文本，仍保存以便保留可见内容）
        saved_content = full_content
        if saved_content or not cancelled:
            assistant_msg = Message(
                session_id=session.id, role="assistant",
                character_id=character.id, character_name=character.name,
                content=saved_content, tokens=usage.completion_tokens,
                is_stopped=cancelled and bool(saved_content),
            )
            self.storage.save_message(assistant_msg)
            self._emit(callbacks, "on_message", assistant_msg)
        else:
            assistant_msg = None

        # 取消时跳过图片/记忆等后续处理，尽快返回
        if cancelled:
            self._emit(callbacks, "on_status", "")
            return assistant_msg

        # 图片
        self._process_image_tags(
            full_content, callbacks, api,
            character_appearances=self._build_character_appearances(character, group_members),
            cancel_check=cancel_check,
        )

        # 记忆更新（透传 session_id 用于会话级增量边界，cancel_check 用于中断整理）
        if character.memory_mode == "embedding_hybrid":
            self.memory.check_and_update_hybrid(
                character, assistant_msg, api, recent_messages=messages,
                session_type=session.session_type, session_id=session.id,
                cancel_check=cancel_check,
            )
        elif character.memory_mode == "summary":
            self.memory.check_and_update_summary(
                character, session, messages, api, cancel_check=cancel_check,
            )

        self._emit(callbacks, "on_status", "")
        return assistant_msg

    def _process_image_tags(self, content, callbacks, api=None,
                            character_appearances=None, cancel_check=None):
        """解析 [img:...] 标签并生成图片。

        纯英文 tag 透传 ComfyUI；含中文时若 Danbooru 服务可用，先走
        「emb召回 + LLM加工」转成英文 Danbooru tag 串再生成。
        手改模式：通过 callbacks.on_danbooru_manual_select 拿用户勾选结果
        （worker 线程发起，UI 切主线程弹窗阻塞返回）。
        character_appearances：会话内角色固定外貌 tag（角色名, tag 串）列表，
          注入 Danbooru LLM 加工 user prompt 让其按中文描述选用，不参与召回。
          仅含中文走 RAG 时生效；纯英文透传不注入。
        cancel_check：透传给 process_image_description 的 LLM 加工（chat_cancelable），
          停止生成时可中断加工；ComfyUI generate 阻塞不可取消（契约已说明）。
        """
        if not self.comfyui or not callbacks.on_image:
            return
        tags = parse_image_tags(content)
        for pos_prompt, neg_prompt in tags:
            # [!] 停止生成时中断图片标签加工（§6 契约）：每个标签加工前检查一次。
            if cancel_check and cancel_check():
                return
            final_pos, final_neg = pos_prompt, neg_prompt
            if (self.danbooru is not None and contains_chinese(pos_prompt)
                    and self.danbooru.db_count() > 0):
                # 手改模式：先召回，让用户勾选
                user_selected: Optional[list] = None
                preset = self.storage.load_danbooru_preset()
                if preset.manual_mode and callbacks.on_danbooru_manual_select:
                    candidates = self.danbooru.recall_candidates(
                        pos_prompt, preset.recall_top_n,
                        preset.allow_nsfw, session_api=api,
                        weights=(preset.weight_emb, preset.weight_fts, preset.weight_wiki, preset.weight_pc),
                        enable_wiki=preset.enable_wiki_fts,
                        allow_categories=preset.allow_categories,
                    )
                    self._emit(callbacks, "on_status", "等待挑选标签…")
                    user_selected = callbacks.on_danbooru_manual_select(candidates, pos_prompt)
                    self._emit(callbacks, "on_status", "")
                    if user_selected is None:
                        # 用户取消 -> 跳过此图
                        continue
                self._emit(callbacks, "on_status", f"正在转换标签: {pos_prompt[:30]}...")
                positive, negative = self.danbooru.process_image_description(
                    pos_prompt, session_api=api, user_selected=user_selected,
                    character_appearances=character_appearances,
                    cancel_check=cancel_check,
                )
                self._emit(callbacks, "on_status", "")
                if not positive:
                    # [!] 加工失败/取消 -> 提示用户并跳过此图（避免用原文中文塞给模型）
                    self._emit(callbacks, "on_error", f"图片标签加工失败，已跳过: {pos_prompt[:30]}")
                    continue
                final_pos, final_neg = positive, (negative or neg_prompt)
            # ComfyUI generate 不可取消（阻塞调用），加工中断后仍可能进入此处（cancel_check 在循环开头检查）
            self._emit(callbacks, "on_status", f"正在生成图片: {final_pos[:30]}...")
            image_path = self.comfyui.generate(final_pos, final_neg)
            if image_path:
                self._emit(callbacks, "on_image", image_path, final_pos)
            else:
                # [!] ComfyUI 生成失败（服务不可达/工作流错误）提示用户
                self._emit(callbacks, "on_error", f"图片生成失败: {final_pos[:30]}")
            self._emit(callbacks, "on_status", "")

    def auto_pick_speaker(self, session, messages, callbacks, cancel_check=None,
                          pending_trigger=""):
        if not session.director_api_id:
            self._emit(callbacks, "on_error", "未设置导演 API")
            return None
        api, preset = self._load_api_and_preset(session.director_api_id)
        if not api:
            self._emit(callbacks, "on_error", "导演 API 不可用")
            return None
        members = self._load_group_members(session)
        if not members:
            return None

        # 前置取消检查：停止生成可能在选角前就已请求
        if cancel_check and cancel_check():
            self._emit(callbacks, "on_status", "")
            return None

        director_messages, director_labels = self.ctx_builder.build_director_messages_with_labels(
            preset, session, messages, members, pending_trigger=pending_trigger,
        )
        self._emit(callbacks, "on_status", "正在选择发言角色...")
        llm = LlmClient(api, preset, jailbreak_prefix=self._jb_prefix())
        # ⚠️ 必须用可取消调用并透传 cancel_check，否则导演选角是一次阻塞 HTTP，
        # 用户在选角阶段点停止会卡死（worker 阻塞在 httpx，done 不发）。
        result = llm.chat_cancelable(
            director_messages, cancel_check=cancel_check, block_labels=director_labels,
        )
        if result.cancelled:
            self._emit(callbacks, "on_status", "")
            return None
        if result.error:
            self._emit(callbacks, "on_error", f"导演 API 错误: {result.error}")
            return None

        self.stats.record_usage(api.id, result.usage)
        self._emit(callbacks, "on_usage", api.id, result.usage)

        picked = result.content.strip().strip('"').strip("'").strip()
        # [!] 精确匹配优先 + 长名优先子串匹配：旧版双向子串 `char.name in picked or picked in char.name`
        # 会让短名（如「小明」）误匹配导演输出「小明华」（另一角色），按名字长度降序避免短名优先。
        for char in members:  # 精确匹配优先
            if char.name == picked:
                self._emit(callbacks, "on_speaker", char)
                self._emit(callbacks, "on_status", "")
                self._remember_speaker(session, char)
                return char
        for char in sorted(members, key=lambda c: -len(c.name)):  # 长名优先子串
            if char.name and (char.name in picked or picked in char.name):
                self._emit(callbacks, "on_speaker", char)
                self._emit(callbacks, "on_status", "")
                self._remember_speaker(session, char)
                return char
        # 选角失败：不再静默选 members[0]，提示用户
        self._emit(callbacks, "on_error", f"导演选角未匹配到角色：{picked[:30]}")
        return None

    def _remember_speaker(self, session, character):
        # [!] 记住最近一次实际选中的发言者：写 session.default_speaker_id，
        # 使后续「直接发消息」(send_and_respond) 默认由该角色回复。
        # 统一覆盖三条选角路径：auto 模式导演选角(auto_pick_speaker)、
        # auto 模式点头像临时干预(trigger_character)、manual 模式点头像。
        # 失败/取消不进此方法（选角未成功不更新，保留上次发言者）。
        if session.default_speaker_id != character.id:
            session.default_speaker_id = character.id
            session.touch()
            self.storage.save_session(session)

    def send_and_auto_respond(self, session, messages, content, callbacks, cancel_check=None):
        # 本轮 user 消息延迟存储：先作为 pending_trigger 让导演选角能看到，回复后再存。
        pending_trigger = content
        pending_user_name = session.player_name
        speaker = self.auto_pick_speaker(
            session, messages, callbacks,
            cancel_check=cancel_check, pending_trigger=pending_trigger,
        )
        if speaker:
            self.generate_response(
                session, messages, speaker, callbacks,
                cancel_check=cancel_check,
                pending_trigger=pending_trigger,
                pending_user_msg=(pending_trigger, pending_user_name),
            )
        else:
            # [!] 选角失败/取消时用户输入已发生，存为 user 消息不丢失（与取消时存 user 一致）
            # 取消（speaker is None 且无 on_error）时不存，保留原「取消即丢弃」语义
            if not cancel_check or not cancel_check():
                self._save_pending_user(
                    session, messages, pending_trigger, pending_user_name, callbacks,
                )
        self._emit(callbacks, "on_done")

    def continue_group_chat(self, session, messages, callbacks, cancel_check=None):
        # 续聊无新 user 输入，构造轻量触发消息"轮到X发言"作为 LAST_USER + 存为 user 消息。
        speaker = self.auto_pick_speaker(session, messages, callbacks, cancel_check=cancel_check)
        if speaker:
            trigger = f"轮到 {speaker.name} 发言"
            self.generate_response(
                session, messages, speaker, callbacks,
                cancel_check=cancel_check,
                pending_trigger=trigger,
                pending_user_msg=(trigger, session.player_name),
            )
        self._emit(callbacks, "on_done")

    def trigger_character(self, session, messages, character, callbacks, cancel_check=None):
        # 手动触发：构造轻量触发消息"轮到X发言"作为 LAST_USER + 存为 user 消息。
        trigger = f"轮到 {character.name} 发言"
        # [!] 记住本次选中的发言者：更新 session.default_speaker_id，使后续
        # 「直接发消息」默认由该角色回复。auto 模式点头像（用户在导演选角之外
        # 手动指定一次发言）同样更新--用户意图覆盖导演选角，后续直接发消息应
        # 跟着这个角色（修复「auto 选 B 后点 A，再直接发消息却由 B 回应」的 bug）。
        self._remember_speaker(session, character)
        self.generate_response(
            session, messages, character, callbacks,
            cancel_check=cancel_check,
            pending_trigger=trigger,
            pending_user_msg=(trigger, session.player_name),
        )
        self._emit(callbacks, "on_done")

    def continue_response(
            self, session, messages, target_msg: Message, callbacks, cancel_check=None,
    ):
        """
        续写被中断（is_stopped）的 assistant 消息：以已有文本作为最后一条
        assistant 消息的 prefill，让 LLM 接着输出，续写内容追加到原消息并清除
        is_stopped 标记（原地更新，不新增消息）。

        - target_msg 必须为 assistant 且 is_stopped=True。
        - 无新增 user 消息；历史中保留 target_msg 的已有文本。
        """
        if target_msg.role != "assistant":
            self._emit(callbacks, "on_error", "只能续写 AI 回复消息")
            self._emit(callbacks, "on_done")
            return
        if not target_msg.is_stopped:
            self._emit(callbacks, "on_error", "该消息未被中断，无需续写")
            self._emit(callbacks, "on_done")
            return
        char = None
        if target_msg.character_id:
            char = self.storage.load_character(target_msg.character_id)
        if not char and session.character_ids:
            char = self.storage.load_character(session.character_ids[0])
        if not char:
            self._emit(callbacks, "on_error", "未找到可用角色")
            self._emit(callbacks, "on_done")
            return

        api, preset = self._load_api_and_preset(char.api_id)
        if not api:
            self._emit(callbacks, "on_error", f"角色 {char.name} 绑定的 API 不可用")
            self._emit(callbacks, "on_done")
            return

        world_book = self._load_world_book(session)
        group_members = self._load_group_members(session) if session.session_type == "group" else None

        query_text = ""
        if messages:
            recent = [m for m in messages if not m.collapsed and not m.is_image_only][-3:]
            query_text = " ".join(m.content for m in recent)
        # 续写场景无 pending_trigger（接已有 assistant 文本继续），hybrid 走单次召回兜底，
        # 用 query_text（含最近对话）作 query，避免无 user 输入导致两次召回退化的复杂处理。
        memory_text = self.memory.get_memory_text(char, query_text, api)

        # 续写豁免 LAST_USER：pending_trigger 传空 -> LAST_USER 块返回空，
        # 末尾由 prefill 的 assistant 消息充当触发（续写语义本就特殊，多数模型支持 assistant prefill）。
        # 续写同样需要注入用户信息（与正文一致）
        user = self.storage.load_user(session.user_id) if getattr(session, "user_id", "") else None
        api_messages, api_labels = self.ctx_builder.build_messages_with_labels(
            preset, char, session, messages,
            world_book=world_book, memory_text=memory_text,
            pending_trigger="", group_members=group_members, user=user,
        )
        # prefill：把已有文本作为最后一条 assistant 消息追加，符合多数 OpenAI 兼容网关的续写语义
        prefilled = target_msg.content
        api_messages.append({"role": "assistant", "content": prefilled})
        # prefill 不属于任何上下文模块，单独补标签保持与 messages 等长（否则日志退化）
        api_labels.append("续写 prefill")

        self._emit(callbacks, "on_status", f"{char.name} 正在续写...")
        llm = LlmClient(api, preset, jailbreak_prefix=self._jb_prefix())
        full_content = ""
        usage = LlmUsage()

        cancelled = False
        if getattr(api, "streaming", True):
            for event_type, data in llm.chat_stream(
                    api_messages, cancel_check=cancel_check, block_labels=api_labels,
            ):
                if event_type == "text":
                    full_content += data
                    self._emit(callbacks, "on_chunk", data)
                elif event_type == "usage":
                    usage = data
                elif event_type == "error":
                    self._emit(callbacks, "on_error", data)
                    self._emit(callbacks, "on_status", "")
                    return
                elif event_type == "cancelled":
                    cancelled = True
                    break
        else:
            # 非流式分支用 chat_cancelable 透传 cancel_check（与 generate_response 一致）
            result = llm.chat_cancelable(
                api_messages, cancel_check=cancel_check, block_labels=api_labels,
            )
            if result.cancelled:
                cancelled = True
            elif result.error:
                self._emit(callbacks, "on_error", result.error)
                self._emit(callbacks, "on_status", "")
                return
            else:
                full_content = result.content
                usage = result.usage
                if full_content:
                    self._emit(callbacks, "on_chunk", full_content)

        # [!] 统计：取消时仍记录已发生的 usage（与 generate_response:267-269 一致）。
        # 放在 cancelled return 之前，确保中断路径也计入费用统计。
        self.stats.record_usage(api.id, usage)
        self._emit(callbacks, "on_usage", api.id, usage)

        # 合并：在已有文本后追加续写内容（去重处理 LLM 可能重复了已有开头）
        merged = self._merge_continuation(prefilled, full_content)
        # 若再次被中断，仍保留部分续写文本并维持 is_stopped
        still_stopped = cancelled and bool(full_content)
        was_stopped = not bool(full_content) and cancelled
        target_msg.content = merged
        target_msg.tokens = usage.completion_tokens + self._estimate_tokens(prefilled)
        target_msg.is_stopped = still_stopped or was_stopped
        self.storage.save_message(target_msg)
        self._emit(callbacks, "on_message", target_msg)

        if cancelled:
            self._emit(callbacks, "on_status", "")
            return

        # 图片（处理整段合并后的内容）
        self._process_image_tags(
            merged, callbacks, api,
            character_appearances=self._build_character_appearances(char, group_members),
            cancel_check=cancel_check,
        )

        # 记忆更新（透传 session_id 与 cancel_check，与 generate_response 一致）
        if char.memory_mode == "embedding_hybrid":
            self.memory.check_and_update_hybrid(
                char, target_msg, api, recent_messages=messages,
                session_type=session.session_type, session_id=session.id,
                cancel_check=cancel_check,
            )
        elif char.memory_mode == "summary":
            self.memory.check_and_update_summary(
                char, session, messages, api, cancel_check=cancel_check,
            )

        self._emit(callbacks, "on_status", "")
        self._emit(callbacks, "on_done")

    @staticmethod
    def _merge_continuation(existing: str, continuation: str) -> str:
        """合并已有文本与续写内容；若续写以已有文本的子串开头则去重。"""
        if not continuation:
            return existing
        # 简单去重：若续写开头与已有文本末尾有重叠（LLM 复述了 prefill），裁掉重叠部分
        overlap = 0
        max_check = min(len(existing), len(continuation), 200)
        for n in range(max_check, 0, -1):
            if existing.endswith(continuation[:n]):
                overlap = n
                break
        return existing + continuation[overlap:]

    def send_and_respond(self, session, messages, content, callbacks, cancel_check=None):
        # 本轮 user 消息延迟存储，先作为 pending_trigger，回复后再存。
        pending_trigger = content
        pending_user_name = session.player_name
        if session.character_ids:
            # [!] 默认发言者：优先用 session.default_speaker_id（手动模式记住的
            # 上次发言者/开场白角色），为空回退 character_ids[0]（向后兼容老会话）。
            # 单聊只有一个角色，default_speaker_id 通常为空，自然回退首个角色。
            speaker_id = session.default_speaker_id or session.character_ids[0]
            char = self.storage.load_character(speaker_id)
            # 兜底：default_speaker_id 失效（角色被删）时回退首个角色
            if not char:
                char = self.storage.load_character(session.character_ids[0])
            if char:
                self.generate_response(
                    session, messages, char, callbacks,
                    cancel_check=cancel_check,
                    pending_trigger=pending_trigger,
                    pending_user_msg=(pending_trigger, pending_user_name),
                )
        self._emit(callbacks, "on_done")

    # ============ 消息编辑 / 重试 / 删除（供 UI 右键菜单调用）============
    def update_message_content(self, msg: Message, new_content: str):
        """编辑消息文本内容（同步持久化）。"""
        msg.content = new_content
        msg.tokens = self._estimate_tokens(new_content)
        msg.is_stopped = False  # 编辑后视为人工修正，清除中断标记
        self.storage.save_message(msg)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        轻量 token 估算（仅编辑消息时用，避免触发可能慢的 tiktoken 编码器加载）。
        采用粗略回退：中文按 1 token，英文约 4 字符/token。
        """
        if not text:
            return 0
        cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        en = len(text) - cn
        return cn + en // 4

    def regenerate_from(self, session, messages, assistant_msg: Message, callbacks, cancel_check=None):
        """
        重试：删除指定 assistant 消息及其前一条 user 消息（本轮触发对），重新生成。

        与酒馆「重试上一条」语义一致，但适配延迟存储机制：
        正常聊天时 user+assistant 是成对延迟存储的（generate_response 里先存 user
        再存 assistant），重试应把这一对都删掉，让用户输入重新作为 pending_trigger
        走 LAST_USER，回复后重新成对存储。这样重试后结构与正常聊天完全一致。

        - assistant_msg 必须为 assistant 角色。
        - 若该消息有 character_id，用该角色重试；否则用会话首个角色。
        - 删除 assistant_msg 及其之后所有消息（含其本条），并删除其前一条 user 消息
          （本轮触发消息）。
        - 重新生成时 pending_trigger=被删 user 的内容，pending_user_msg=(内容, 名字)
          回复后重新存储。
        - 边界：若前一条不是 user（如连续重试、或历史末尾本就是 assistant），则不删
          前一条，退化为「只删 assistant + 不构造 trigger」复用 HISTORY 末尾。

        注：LLM 没返回（无 assistant 消息持久化）时 UI 无气泡可右键，进不了此路径，
        用户直接重新输入即可（等价于「重新输入一次用户信息」）。
        """
        if assistant_msg.role != "assistant":
            self._emit(callbacks, "on_error", "只能重试 AI 回复消息")
            self._emit(callbacks, "on_done")
            return

        # 定位该消息及其之后的所有消息
        idx = next(
            (i for i, m in enumerate(messages) if m.id == assistant_msg.id),
            None,
        )
        if idx is None:
            self._emit(callbacks, "on_error", "未找到要重试的消息")
            self._emit(callbacks, "on_done")
            return

        # 检查前一条是否是 user 消息（本轮触发对）。延迟存储下 user 先于 assistant 存，
        # 故正常聊天路径里被重试的 assistant 前一条必是本轮 user。
        prev_user_msg = None
        if idx > 0 and messages[idx - 1].role == "user":
            prev_user_msg = messages[idx - 1]

        # 删除该消息及之后所有消息
        to_delete = messages[idx:]
        for m in to_delete:
            self.storage.delete_message(m.id)
        del messages[idx:]

        # 删除前一条 user 消息（本轮触发对）
        pending_trigger = ""
        pending_user_name = session.player_name
        if prev_user_msg is not None:
            pending_trigger = prev_user_msg.content
            pending_user_name = prev_user_msg.character_name or session.player_name
            self.storage.delete_message(prev_user_msg.id)
            # messages 列表里 prev_user_msg 在 idx-1，删除它
            del messages[idx - 1]

        # 确定重新生成的角色
        char = None
        if assistant_msg.character_id:
            char = self.storage.load_character(assistant_msg.character_id)
        if not char and session.character_ids:
            char = self.storage.load_character(session.character_ids[0])
        if not char:
            self._emit(callbacks, "on_error", "未找到可用角色")
            self._emit(callbacks, "on_done")
            return

        self._emit(callbacks, "on_status", f"正在重试 {char.name} 的回复...")
        if pending_trigger:
            # 正常路径：删了 user+assistant 对，重新走 pending_trigger（与正常聊天一致）
            self.generate_response(
                session, messages, char, callbacks,
                cancel_check=cancel_check,
                pending_trigger=pending_trigger,
                pending_user_msg=(pending_trigger, pending_user_name),
            )
        else:
            # 边界：前一条不是 user（如连续重试、历史末尾本就是 assistant），不构造 trigger，
            # 复用 HISTORY 末尾消息充当触发（pending_user_msg=None 不新增 user）
            self.generate_response(
                session, messages, char, callbacks,
                cancel_check=cancel_check,
                pending_trigger="",
                pending_user_msg=None,
            )
        self._emit(callbacks, "on_done")

    def delete_message_and_after(self, session, messages, msg: Message, delete_after: bool = False):
        """
        删除消息。delete_after=True 时删除该消息及其后所有消息（向下分支删除）。
        普通删除只删该条本身。
        """
        idx = next((i for i, m in enumerate(messages) if m.id == msg.id), None)
        if idx is None:
            return
        if delete_after:
            for m in messages[idx:]:
                self.storage.delete_message(m.id)
            del messages[idx:]
        else:
            self.storage.delete_message(msg.id)
            messages.pop(idx)