"""
上下文构建器 -- 按可排序的「上下文模块」顺序拼接 messages。

模块顺序由预设的 context_blocks 定义，用户可在设置中自由拖拽排序、
启用/禁用、新增自定义文本模块。默认顺序按缓存友好性设计：

  [系统提示]            ← 稳定，命中缓存
  [角色信息]            ← 稳定，命中缓存
  [用户信息]            ← 稳定，命中缓存
  <world_book>          ← 常驻条目稳定命中缓存（世界设定先于故事）
  [历史对话]            ← append-only，命中缓存（不含本轮触发消息）
  <summary>             ← 半易变（每 N 轮重新总结）
  <memory>              ← 半易变（每轮检索/整理）
  [最后用户消息]        ← 本轮触发，强制置末

各模块产出 0 或多条消息，按 context_blocks 顺序拼接，跳过被禁用的模块。
LAST_USER 强制置末（无论用户在 UI 里拖到哪），保证发给 API 的最后一条
恒为 user 触发消息（续写场景例外，由 orchestrator 处理）。

本轮触发消息采用「延迟存储」：用户输入/构造的触发消息先作为 pending_trigger
传入 build_messages，不进 messages（故 HISTORY 自然不含它），LLM 回复后才
和 assistant 消息一起存 DB。
"""
from __future__ import annotations
from typing import Optional

from src.models import (
    Character, Message, Session, Preset, WorldBook, WorldBookEntry,
    User,
)
from src.models.preset import (
    BLOCK_SYSTEM_PROMPT, BLOCK_CHARACTER_INFO, BLOCK_SUMMARY, BLOCK_HISTORY,
    BLOCK_WORLD_BOOK, BLOCK_MEMORY, BLOCK_CUSTOM,
    BLOCK_USER, BLOCK_LAST_USER, BLOCK_LABELS,
)
from src.services.storage import Storage


# ============ 模块占位变量（可在系统提示/自定义块里写，构建时替换为对应块的实际输出）============
# 仅「内容型」内部块可做成占位：用户在系统提示里写 <角色信息>{{character_info}}</角色信息>
# 这类 XML 风格标签，构建时把 {{...}} 替换成该块渲染出的实际文本，AI 一眼看出哪段是什么。
# 命中占位后，对应块在 context_blocks 顺序拼接时被跳过（避免重复注入）。
# 注：history/instruction 是位置敏感块，不做成占位 -- 它们必须按 blocks 顺序拼接，
# 做成占位反而打乱顺序。
PLACEHOLDER_BLOCKS: dict[str, str] = {
    "{{character_info}}": BLOCK_CHARACTER_INFO,
    "{{user_info}}": BLOCK_USER,
    "{{summary}}": BLOCK_SUMMARY,
    "{{memory}}": BLOCK_MEMORY,
    "{{world_book}}": BLOCK_WORLD_BOOK,
}


class ContextBuilder:
    """构建发送给 API 的 messages 列表，模块顺序可配置。"""

    # 世界书匹配时扫描最近多少条消息
    WB_SCAN_DEPTH = 10

    def __init__(self, storage: Storage):
        self.storage = storage

    # ============ 文本变量替换 ============
    @staticmethod
    def _fill_vars(
            text: str,
            character: Character,
            player_name: str,
            include_char_fields: bool = False,
            group_info: str = "",
            user_description: str = "",
            group_member_names: str = "",
    ) -> str:
        """统一变量替换。

        - 始终替换：{{char}} {{user}} {{user_description}}（用户描述可在任何位置被引用）
        - {{group_member_names}}：群聊参与角色卡的角色名（逗号分隔），群聊系统提示词用它
          明确「不要扮演这些角色」，而非泛泛禁止扮演别的角色（允许拓展临时 NPC/旁白）。
          单聊时为空串。
        - include_char_fields=True 时额外解析：{{description}} {{personality}}
          {{scenario}} {{mes_example}}（仅在角色信息/系统提示等需要展开角色字段时启用）
        """
        result = (
            text
            .replace("{{char}}", character.name)
            .replace("{{user}}", player_name)
            .replace("{{user_description}}", user_description)
            .replace("{{group_member_names}}", group_member_names)
        )
        if include_char_fields:
            result = (
                result
                .replace("{{description}}", character.description)
                .replace("{{personality}}", character.personality)
                .replace("{{scenario}}", character.scenario + group_info)
                .replace("{{mes_example}}", character.mes_example)
            )
        return result

    # ============ 角色信息渲染（系统提示 + 角色信息块共享）============
    def _group_info_text(self, character: Character, group_members) -> str:
        """群聊时附加其他成员信息。"""
        if not group_members:
            return ""
        others = [c for c in group_members if c.id != character.id]
        if not others:
            return ""
        info = "\n\n【群聊中的其他角色】\n"
        for c in others:
            info += f"- {c.name}：{c.personality[:80]}\n"
        return info

    # ============ 世界书匹配 ============
    def match_world_book(
            self, world_book: WorldBook, recent_text: str
    ) -> list[WorldBookEntry]:
        """根据关键词匹配世界书条目。"""
        matched = []
        for entry in world_book.entries:
            if not entry.enabled:
                continue
            if entry.constant:
                matched.append(entry)
                continue
            if not entry.keys:
                continue

            text = recent_text if entry.case_sensitive else recent_text.lower()
            keys = entry.keys if entry.case_sensitive else [k.lower() for k in entry.keys]

            primary_hit = any(k in text for k in keys)

            if entry.selective:
                # 需要主关键词和次关键词都命中
                sec_keys = (
                    entry.secondary_keys
                    if entry.case_sensitive
                    else [k.lower() for k in entry.secondary_keys]
                )
                sec_hit = any(k in text for k in sec_keys) if sec_keys else True
                if primary_hit and sec_hit:
                    matched.append(entry)
            else:
                if primary_hit:
                    matched.append(entry)
        return matched

    def _recent_text(self, messages: list[Message]) -> str:
        """收集最近文本用于世界书匹配。"""
        recent_msgs = messages[-self.WB_SCAN_DEPTH:] if messages else []
        return " ".join(
            m.content for m in recent_msgs
            if not m.is_image and not m.collapsed
        )

    # ============ 单块渲染 ============
    def _render_block(
            self,
            block: dict,
            ctx: dict,
    ) -> list[dict]:
        """
        渲染单个上下文模块为消息列表（可能为空）。

        ctx 包含: preset, character, session, messages, world_book,
                  memory_text, pending_trigger, session_type, group_members, recent_text
        """
        btype = block.get("type")
        if not block.get("enabled", True):
            return []

        preset: Preset = ctx["preset"]
        character: Character = ctx["character"]
        session: Session = ctx["session"]
        messages: list[Message] = ctx["messages"]
        world_book: Optional[WorldBook] = ctx.get("world_book")
        memory_text: str = ctx.get("memory_text", "")
        group_members = ctx.get("group_members")
        recent_text: str = ctx.get("recent_text", "")
        player_name = session.player_name

        if btype == BLOCK_SYSTEM_PROMPT:
            # 按 session_type 选单聊/群聊系统提示词
            sys_prompt = (preset.system_prompt_group
                          if ctx.get("session_type") == "group"
                          else preset.system_prompt)
            # 群聊时算「除自己外」的参与角色卡角色名（逗号分隔），供 {{group_member_names}} 变量用：
            # 让提示词明确「不要扮演这些角色」（而非泛泛禁止扮演别的角色），
            # 允许 LLM 拓展临时 NPC/第三人称旁白推进剧情。
            # [!] 排除当前角色 character 自己：A 发言时提示词是「你是 A，不要扮演 B、C」，
            # 而非「你是 A，不要扮演 A、B、C」（自己不能扮演自己，矛盾）。
            group_member_names = ""
            if ctx.get("session_type") == "group" and group_members:
                group_member_names = ", ".join(
                    c.name for c in group_members
                    if c.name and c.id != character.id
                )
            text = self._fill_vars(
                sys_prompt, character, player_name,
                user_description=ctx.get("user_description", ""),
                group_member_names=group_member_names,
            )
            return [{"role": "system", "content": text}] if text.strip() else []

        if btype == BLOCK_CHARACTER_INFO:
            group_info = self._group_info_text(character, group_members)
            text = self._fill_vars(
                preset.character_info_template, character, player_name,
                include_char_fields=True, group_info=group_info,
                user_description=ctx.get("user_description", ""),
            )
            return [{"role": "system", "content": text}] if text.strip() else []

        if btype == BLOCK_USER:
            user_desc = ctx.get("user_description", "")
            # 仅当绑定了 User（user_name 非空字符串即代表有真实用户实体）或用户写了描述时输出。
            # 否则跳过块（保持向后兼容：老会话无 user_id 且无 {{user_description}} 引用时
            # 不强行注入「用户名：用户」这种无意义行）。
            user_name = ctx.get("user_name", "")
            has_user = bool(user_name) or bool(user_desc)
            if not has_user:
                return []
            text = f"用户名：{user_name or player_name}"
            if user_desc:
                text += f"\n用户设定：{user_desc}"
            return [{"role": "system", "content": text}]

        if btype == BLOCK_SUMMARY:
            result = []
            for msg in messages:
                if msg.is_image_only:
                    continue
                if msg.is_summary:
                    result.append({
                        "role": "system",
                        "content": f"<summary>\n{msg.content}\n</summary>",
                    })
            return result

        if btype == BLOCK_HISTORY:
            result = []
            for msg in messages:
                if msg.is_image_only:
                    continue  # 纯图片不入上下文
                if msg.collapsed and not msg.is_summary:
                    continue  # 折叠的非总结消息不发送
                # summary 消息由专门的 summary 块负责，这里跳过
                if msg.is_summary:
                    continue
                if msg.role == "user":
                    result.append({
                        "role": "user",
                        "content": msg.content,
                        "name": msg.character_name or player_name,
                    })
                elif msg.role == "assistant":
                    result.append({
                        "role": "assistant",
                        "content": msg.content,
                        "name": msg.character_name or character.name,
                    })
                elif msg.role == "system":
                    result.append({"role": "system", "content": msg.content})
            return result

        if btype == BLOCK_WORLD_BOOK:
            if not world_book:
                return []
            matched = self.match_world_book(world_book, recent_text)
            if not matched:
                return []
            # 统一合并所有命中条目（按 insertion_order 排序），使其可独立排序/拖动。
            # 用 <world_book> XML 标签包裹（与 <summary>/<memory> 一致），给 LLM 明确的
            # 语义边界信号：这段是场景设定/世界信息，不是角色发言或系统插话。多条目用
            # 空行分隔后整体包进一个 system 消息（保持单消息、缓存友好，不拆成多条）。
            matched.sort(key=lambda e: e.insertion_order)
            inner = "\n\n".join(e.content for e in matched if e.content and e.content.strip())
            if not inner.strip():
                return []
            return [{"role": "system", "content": f"<world_book>\n{inner}\n</world_book>"}]

        if btype == BLOCK_MEMORY:
            if not memory_text:
                return []
            return [{"role": "system", "content": f"<memory>\n{memory_text}\n</memory>"}]

        if btype == BLOCK_LAST_USER:
            # 本轮触发消息：来自 pending_trigger（用户真实输入或构造的"轮到X发言"）。
            # 该消息此时尚未存入 DB（延迟存储），HISTORY 不含它，仅此块承载。
            trigger = ctx.get("pending_trigger", "")
            if not trigger:
                return []
            return [{"role": "user", "content": trigger}]

        if btype == BLOCK_CUSTOM:
            content = block.get("content", "")
            if not content.strip():
                return []
            text = self._fill_vars(
                content, character, player_name,
                user_description=ctx.get("user_description", ""),
                include_char_fields=True,   # [!] 自定义块也解析角色字段变量（{{description}} 等），与 CHARACTER_INFO 一致（§1 契约）
            )
            if not text.strip():
                return []
            # 自定义块角色可配（system/user/assistant），缺省/非法回退 system。
            # system=系统指令，user=用户发言，assistant=AI 发言（可用作 prefill）。
            role = block.get("role", "system")
            if role not in ("system", "user", "assistant"):
                role = "system"
            return [{"role": role, "content": text}]

        return []

    # ============ 模块可读标签 ============
    @staticmethod
    def _block_label(block: dict) -> str:
        """把一个 context_blocks 项转成可读标签（用于入参日志按模块打印）。

        内置块用 BLOCK_LABELS 中文名；自定义块用其 label 字段（缺省「自定义模块」）。
        被禁用块不会走到这里（调用方已过滤）。
        """
        btype = block.get("type")
        if btype == BLOCK_CUSTOM:
            return block.get("label") or "自定义模块"
        return BLOCK_LABELS.get(btype, btype or "未知模块")

    # ============ 构建完整 messages ============
    def build_messages(
            self,
            preset: Preset,
            character: Character,
            session: Session,
            messages: list[Message],
            world_book: Optional[WorldBook] = None,
            memory_text: str = "",
            pending_trigger: str = "",
            group_members: Optional[list[Character]] = None,
            user: Optional[User] = None,
    ) -> list[dict]:
        """
        构建发送给 API 的 messages 列表。

        按 preset.context_blocks 定义的顺序拼接各模块；跳过被禁用的模块。
        折叠的非总结消息和纯图片消息不发送。

        本轮触发消息（pending_trigger）由 BLOCK_LAST_USER 承载，不存于 messages
        故 HISTORY 自然不含它。LAST_USER 强制置末（无论 context_blocks 里排哪），
        保证发给 API 的最后一条恒为 user 触发消息（续写场景例外，由 orchestrator 处理）。

        模块占位替换：系统提示模板可写 {{character_info}}/{{user_info}}/{{summary}}
        /{{memory}}/{{world_book}} 等占位，命中后把对应块的输出替换进系统提示文本，
        被引用块随后在 context_blocks 顺序拼接时跳过（避免重复注入）。
        不写占位则所有块按原顺序追加，行为零变化（向后兼容）。

        本方法是 build_messages_with_labels 的薄包装（仅取 messages，丢弃模块标签），
        供不需要模块信息的调用方使用。
        """
        msgs, _labels = self.build_messages_with_labels(
            preset, character, session, messages,
            world_book=world_book, memory_text=memory_text,
            pending_trigger=pending_trigger,
            group_members=group_members, user=user,
        )
        return msgs

    def build_messages_with_labels(
            self,
            preset: Preset,
            character: Character,
            session: Session,
            messages: list[Message],
            world_book: Optional[WorldBook] = None,
            memory_text: str = "",
            pending_trigger: str = "",
            group_members: Optional[list[Character]] = None,
            user: Optional[User] = None,
    ) -> tuple[list[dict], list[str]]:
        """
        构建 (messages, labels)：labels[i] 是 messages[i] 所属上下文模块的可读名，
        二者等长一一对应。供 LLM 客户端入参日志按模块打印「这条 message 来自哪个块」。

        一个模块可能产出多条消息（如 HISTORY 把每条历史都展开成一条 message），
        这些消息共享同一标签；被禁用 / 内容为空 / 被系统提示占位消费掉的模块不产出
        任何消息，对应标签也不出现。续写场景 orchestrator 在返回结果末尾追加的
        assistant prefill 不属于任何上下文模块，由调用方自行补标签（如「续写 prefill」）。
        """
        recent_text = self._recent_text(messages)
        ctx = {
            "preset": preset,
            "character": character,
            "session": session,
            "messages": messages,
            "world_book": world_book,
            "memory_text": memory_text,
            "pending_trigger": pending_trigger,
            "session_type": session.session_type,
            "group_members": group_members,
            "recent_text": recent_text,
            # 用户实体：name/avatar/description 来自绑定的 User；无则留空，
            # BLOCK_USER 仅在有 user_name/description 时才输出。{{user}} 仍由
            # _fill_vars 用 session.player_name 替换（保持历史行为）。
            "user_name": (user.name if user else ""),
            "user_description": (user.description if user else ""),
        }

        # 先按顺序渲染每个启用的内部块，输出暂存到 per_block（仅在占位替换时用）
        rendered_per_block: dict[str, list[dict]] = {}
        for block in preset.context_blocks:
            if not block.get("enabled", True):
                continue
            btype = block.get("type")
            outs = self._render_block(block, ctx)
            if btype in rendered_per_block:
                rendered_per_block[btype].extend(outs)
            else:
                rendered_per_block[btype] = list(outs)

        # 检测系统提示是否含占位变量；含则把对应块输出替换进系统提示文本，
        # 并把这些被引用块从「按顺序追加」中标记跳过（避免重复注入）。
        consumed_btypes: set[str] = set()
        sys_outputs = rendered_per_block.get(BLOCK_SYSTEM_PROMPT, [])
        if sys_outputs:
            sys_text = sys_outputs[0]["content"]
            for placeholder, btype in PLACEHOLDER_BLOCKS.items():
                if placeholder in sys_text:
                    block_msgs = rendered_per_block.get(btype, [])
                    inner = "\n\n".join(m["content"] for m in block_msgs if m.get("content"))
                    sys_text = sys_text.replace(placeholder, inner)
                    consumed_btypes.add(btype)
            sys_outputs[0]["content"] = sys_text

        # 按原 context_blocks 顺序拼接：被占位消费的块跳过，LAST_USER 跳过（末尾单独追加）
        # 同时记录每条 message 所属模块的可读标签，供入参日志按模块打印。
        result: list[dict] = []
        labels: list[str] = []
        for block in preset.context_blocks:
            if not block.get("enabled", True):
                continue
            btype = block.get("type")
            if btype in consumed_btypes:
                continue
            if btype == BLOCK_LAST_USER:
                continue  # 强制置末兜底：跳过此处，末尾单独追加
            outs = rendered_per_block.get(btype, [])
            if outs:
                label = self._block_label(block)
                result.extend(outs)
                labels.extend([label] * len(outs))
        # LAST_USER 强制置末
        last_user_outs = rendered_per_block.get(BLOCK_LAST_USER, [])
        if last_user_outs:
            # LAST_USER 块在 context_blocks 里必有且唯一（_normalize_blocks 保证），
            # 这里直接用内置标签名，避免再回查 block dict。
            result.extend(last_user_outs)
            labels.extend([BLOCK_LABELS[BLOCK_LAST_USER]] * len(last_user_outs))
        return result, labels

    # ============ 系统提示（兼容接口，导演/单条 system 用）============
    def build_system_prompt(
            self,
            preset: Preset,
            character: Character,
            player_name: str,
            world_book: Optional[WorldBook] = None,
            recent_text: str = "",
            group_members: Optional[list[Character]] = None,
    ) -> str:
        """
        构建单条 system 提示文本（系统提示 + 角色信息合并）。
        供导演模式等需要单条 system 的旧路径使用。日常聊天走 build_messages。
        """
        group_info = self._group_info_text(character, group_members)
        prompt = self._fill_vars(
            preset.system_prompt, character, player_name
        )
        info = self._fill_vars(
            preset.character_info_template, character, player_name,
            include_char_fields=True, group_info=group_info,
        )
        combined = prompt + "\n\n" + info if info.strip() else prompt

        # 世界书注入（合并到 system 末尾，用 <world_book> 标签包裹与块路径一致）
        if world_book and recent_text:
            matched = self.match_world_book(world_book, recent_text)
            if matched:
                matched.sort(key=lambda e: e.insertion_order)
                inner = "\n\n".join(
                    e.content for e in matched if e.content and e.content.strip()
                )
                if inner.strip():
                    combined = combined + f"\n\n<world_book>\n{inner}\n</world_book>"
        return combined

    # ============ 导演模式上下文 ============
    def build_director_messages(
            self,
            preset: Preset,
            session: Session,
            messages: list[Message],
            characters: list[Character],
            pending_trigger: str = "",
    ) -> list[dict]:
        """构建导演 API 的上下文（用于群聊自动选择下一个发言者）。

        pending_trigger：本轮用户输入（此时尚未存入 DB，messages 不含它）。
        若提供则临时追加到导演上下文末尾，让导演能看到本轮用户输入再选角。

        本方法是 build_director_messages_with_labels 的薄包装（仅取 messages）。
        """
        msgs, _labels = self.build_director_messages_with_labels(
            preset, session, messages, characters, pending_trigger=pending_trigger,
        )
        return msgs

    def build_director_messages_with_labels(
            self,
            preset: Preset,
            session: Session,
            messages: list[Message],
            characters: list[Character],
            pending_trigger: str = "",
    ) -> tuple[list[dict], list[str]]:
        """构建导演 API 的上下文，返回 (messages, labels) 供入参日志按模块打印。

        导演上下文结构与日常聊天不同：一条导演系统提示 + 历史消息（折叠/图片跳过）
        + 可选的本轮用户输入 + 一条「请选角」user 指令。labels 与 messages 等长，
        用固定中文名标注每段来源（导演系统提示 / 历史消息 / 本轮用户输入 / 选角指令）。
        """
        char_list = "、".join(c.name for c in characters)
        director_prompt = preset.director_prompt.replace("{characters}", char_list)

        result: list[dict] = [{"role": "system", "content": director_prompt}]
        labels: list[str] = ["导演系统提示"]

        # 发送最近的活跃消息（跳过折叠和图片）
        for msg in messages:
            if msg.is_image_only or (msg.collapsed and not msg.is_summary):
                continue
            if msg.is_summary:
                result.append({"role": "system", "content": f"<summary>\n{msg.content}\n</summary>"})
                labels.append("历史消息")
            elif msg.role == "user":
                result.append({"role": "user", "content": msg.content})
                labels.append("历史消息")
            elif msg.role == "assistant":
                result.append({
                    "role": "assistant",
                    "content": f"{msg.character_name}：{msg.content}",
                })
                labels.append("历史消息")

        # 临时追加本轮用户输入（不入 DB，仅让导演看到本轮内容做选角判断）
        if pending_trigger:
            result.append({"role": "user", "content": pending_trigger})
            labels.append("本轮用户输入")

        result.append({
            "role": "user",
            "content": f"请从以下角色中选择最适合下一个发言的：{char_list}\n只输出角色名字。",
        })
        labels.append("选角指令")
        return result, labels
