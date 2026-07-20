"""预设数据模型（系统提示模板 + 生成参数 + 上下文模块顺序）。"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field


# 旧版默认系统提示（合并了角色描述等，用于老数据迁移检测）
LEGACY_SYSTEM_PROMPT = (
    "你是 {{char}}，请完全沉浸在角色中与 {{user}} 进行角色扮演。\n\n"
    "角色描述：{{description}}\n\n"
    "性格特征：{{personality}}\n\n"
    "当前场景：{{scenario}}\n\n"
    "要求：\n"
    "1. 始终以 {{char}} 的身份回复，保持角色性格一致。\n"
    "2. 使用自然、生动的语言，描写动作和心理活动。\n"
    "3. 不要代替 {{user}} 行动或说话。\n"
)

# 默认系统提示模板（单聊版，含上下文结构说明）。仅支持变量 {{char}} {{user}}。
DEFAULT_SYSTEM_PROMPT = (
    "=== 图片标签要求（最高优先级，每次回复必须遵守）===\n"
    "回复正文写完后，必须在末尾追加两个 `[img:视角词，场景描述]` 标签，分别从两个视角"
    "描述本轮最后一条消息涉及的人物及场景，供文生图用。这是硬性格式要求，不可省略，"
    "不可只输出一个。\n"
    "- 第一个标签以「第一人称视角」开头（从 {{user}} 的眼睛看 {{char}} 与场景，"
    "如 `[img:第一人称视角，{{char}}站在书案前回眸看向镜头，素白衣裙，雪白长发垂至腰际，"
    "蓝眸含笑，面颊微红，手持毛笔，身后是雕花窗与午后阳光]`）。\n"
    "- 第二个标签以「第三人称视角」开头（旁观者全景，如 `[img:第三人称视角，书房内{{char}}"
    "立于书案旁回眸看向身后的{{user}}，白衣白发，两人之间隔着三步，案上摊着宣纸与砚台，"
    "阳光透过窗棂洒在地上]`）。\n"
    "标签内描述风格（仅约束 [img:...] 内部，不影响正文文风）：\n"
    "(1) 只写画面能直接看到的：外貌（发色/发长/瞳色/肤色/服装）、动作姿势、表情神态、"
    "持有物品、场景可见元素（家具/建筑/光线/天气）。\n"
    "(2) 禁抽象情绪（「似笑非笑的光」改「嘴角微扬」）、禁嗅觉/听觉/触觉（「墨香」「声音压低」删）、"
    "禁文学修辞（「纤弱身影拉得很长」改「身影修长」）。\n"
    "(3) 角色名可保留，外貌细节尽量具体便于 tag 召回。\n"
    "标签只输出 `[img:...]` 本身，不加说明文字。\n"
    "=== 图片标签要求结束 ===\n\n"
    "你是 {{char}}，请完全沉浸在角色中与 {{user}} 进行角色扮演。\n\n"
    "以下是你的对话上下文结构：\n"
    "- 角色信息：你的身份、性格与当前场景\n"
    "- 用户信息：与你对话的用户\n"
    "- 世界书：场景相关的补充设定\n"
    "- 历史对话：过往已经发生的对话记录\n"
    "- <summary>：对更早对话的总结回顾\n"
    "- <memory>：你长期记得的事（不是刚发生的，是你的长期记忆）\n"
    "- 最后一条消息：本轮需要你回复的消息\n\n"
    "要求：\n"
    "1. 始终以 {{char}} 的身份回复，保持角色性格一致。\n"
    "2. 使用自然、生动的语言，描写动作和心理活动。\n"
    "3. 不要代替 {{user}} 行动或说话。\n"
    "4. 每次回复控制在 800-1500 字，内容要充实饱满，包含动作、神态、心理与对话，避免短促敷衍。\n"
    "5. 回复末尾必须按上方「图片标签要求」输出两个 [img:...] 标签（不可省略）。"
)

# 默认系统提示模板（群聊版，含上下文结构说明）。支持变量 {{char}} {{user}} {{group_member_names}}。
# [!] {{group_member_names}} = 参与群聊的角色卡角色名（逗号分隔），提示词用它明确「不要扮演
# 这些角色」，而非泛泛禁止扮演别的角色--允许 LLM 拓展临时 NPC、第三人称旁白推进剧情。
DEFAULT_SYSTEM_PROMPT_GROUP = (
    "=== 图片标签要求（最高优先级，每次回复必须遵守）===\n"
    "回复正文写完后，必须在末尾追加两个 `[img:视角词，场景描述]` 标签，分别从两个视角"
    "描述本轮最后一条消息涉及的人物及场景，供文生图用。这是硬性格式要求，不可省略，"
    "不可只输出一个。\n"
    "- 第一个标签以「第一人称视角」开头（从最后一条消息的接收者眼睛看 {{char}} 与场景，"
    "如 `[img:第一人称视角，{{char}}站在书案前回眸看向镜头，素白衣裙，雪白长发垂至腰际，"
    "蓝眸含笑，面颊微红，手持毛笔，身后是雕花窗与午后阳光]`）。\n"
    "- 第二个标签以「第三人称视角」开头（旁观者全景，如 `[img:第三人称视角，书房内{{char}}"
    "立于书案旁回眸看向身后的人，白衣白发，两人之间隔着三步，案上摊着宣纸与砚台，"
    "阳光透过窗棂洒在地上]`）。\n"
    "标签内描述风格（仅约束 [img:...] 内部，不影响正文文风）：\n"
    "(1) 只写画面能直接看到的：外貌（发色/发长/瞳色/肤色/服装）、动作姿势、表情神态、"
    "持有物品、场景可见元素（家具/建筑/光线/天气）。\n"
    "(2) 禁抽象情绪（「似笑非笑的光」改「嘴角微扬」）、禁嗅觉/听觉/触觉（「墨香」「声音压低」删）、"
    "禁文学修辞（「纤弱身影拉得很长」改「身影修长」）。\n"
    "(3) 角色名可保留，外貌细节尽量具体便于 tag 召回。\n"
    "标签只输出 `[img:...]` 本身，不加说明文字。\n"
    "=== 图片标签要求结束 ===\n\n"
    "你是 {{char}}，正在与 {{user}} 及其他角色进行群聊角色扮演。\n\n"
    "以下是你的对话上下文结构：\n"
    "- 角色信息：你的身份、性格与当前场景，以及群聊中的其他角色\n"
    "- 用户信息：参与群聊的用户\n"
    "- 世界书：场景相关的补充设定\n"
    "- 历史对话：过往已经发生的对话记录（含其他角色的发言）\n"
    "- <summary>：对更早对话的总结回顾\n"
    "- <memory>：你个人长期记得的事（不是刚发生的，是你的长期记忆；其他角色有自己的记忆，你只看到自己的）\n"
    "- 最后一条消息：本轮需要你回复的消息\n\n"
    "要求：\n"
    "1. 始终以 {{char}} 的身份回复，保持角色性格一致。\n"
    "2. 使用自然、生动的语言，描写动作和心理活动。\n"
    "3. 不要代替 {{user}} 行动或说话。\n"
    "4. 不要扮演参与群聊的角色卡角色（{{group_member_names}}）--这些角色由他们自己发言，"
    "你不要替他们说话或行动。但你可以引入并扮演其他临时角色（如路人、NPC）推进剧情，"
    "或用第三人称旁白描写场景与事件。\n"
    "5. 只在你该发言时回复，不要替上述角色卡角色代言。\n"
    "6. 每次回复控制在 800-1500 字，内容要充实饱满，包含动作、神态、心理与对话，避免短促敷衍。\n"
    "7. 回复末尾必须按上方「图片标签要求」输出两个 [img:...] 标签（不可省略）。"
)

# 默认角色信息模板（支持变量 {{description}} {{personality}} {{scenario}} {{char}} {{user}}）
DEFAULT_CHARACTER_INFO_TEMPLATE = (
    "角色描述：{{description}}\n\n"
    "性格特征：{{personality}}\n\n"
    "当前场景：{{scenario}}"
)

# 导演模式提示（用于群聊自动选择下一个发言者）
DEFAULT_DIRECTOR_PROMPT = (
    "你是一个群聊导演。根据当前对话内容和上下文，选择最适合下一个发言的角色。\n\n"
    "可选角色：{characters}\n\n"
    "请只输出角色的名字，不要输出任何其他内容。"
)

# ============ 上下文模块类型 ============
# 内置块类型（不可删除，仅可禁用/排序）
BLOCK_SYSTEM_PROMPT = "system_prompt"   # 系统提示
BLOCK_CHARACTER_INFO = "character_info"  # 角色信息
BLOCK_USER = "user_info"                 # 用户信息（注入 {{user}} 名字 + 用户设定）
BLOCK_SUMMARY = "summary"                # 上文总结
BLOCK_HISTORY = "history"                # 历史消息（不含本轮触发消息，本轮触发由 LAST_USER 承载）
BLOCK_WORLD_BOOK = "world_book"          # 世界书
BLOCK_MEMORY = "memory"                  # 角色记忆
BLOCK_LAST_USER = "last_user"            # 最后用户消息（本轮触发消息，强制置末）
BLOCK_CUSTOM = "custom"                  # 自定义文本块

# 内置块类型集合（不可删除）
BUILTIN_BLOCK_TYPES = {
    BLOCK_SYSTEM_PROMPT, BLOCK_CHARACTER_INFO, BLOCK_USER, BLOCK_SUMMARY,
    BLOCK_HISTORY, BLOCK_WORLD_BOOK, BLOCK_MEMORY, BLOCK_LAST_USER,
}

# 内置块默认显示名
BLOCK_LABELS = {
    BLOCK_SYSTEM_PROMPT: "系统提示",
    BLOCK_CHARACTER_INFO: "角色信息",
    BLOCK_USER: "用户信息",
    BLOCK_SUMMARY: "上文总结",
    BLOCK_HISTORY: "历史消息",
    BLOCK_WORLD_BOOK: "世界书",
    BLOCK_MEMORY: "角色记忆",
    BLOCK_LAST_USER: "最后用户消息",
}

# ============ 自定义块角色 ============
# 自定义块注入 messages 时可选用的三种角色。system=系统指令（默认，向后兼容），
# user=用户发言，assistant=AI 发言（可用作 prefill/预设发言）。内置块 role 各自
# 硬编码合理值，不走此机制。
CUSTOM_BLOCK_ROLES: tuple[str, ...] = ("system", "user", "assistant")
DEFAULT_CUSTOM_BLOCK_ROLE = "system"
# 角色中文显示名（UI 下拉与列表标记用），键与 CUSTOM_BLOCK_ROLES 一一对应
CUSTOM_BLOCK_ROLE_LABELS: dict[str, str] = {
    "system": "系统",
    "user": "用户",
    "assistant": "AI",
}


def _normalize_custom_role(value) -> str:
    """校验自定义块 role 字段，非法值/缺省回退 system（向后兼容老数据）。"""
    if value in CUSTOM_BLOCK_ROLES:
        return value
    return DEFAULT_CUSTOM_BLOCK_ROLE


def _default_context_blocks() -> list[dict]:
    """默认上下文模块顺序。

    设计：稳定块在前（含常驻世界书）-> append-only 历史 -> 半易变块（总结/记忆）->
    本轮触发消息（强制置末）。
    世界书放历史前：常驻条目内容固定，成为稳定前缀区一部分，历史 append-only
    增长时缓存命中区更大；触发式条目每轮可能变，但放历史前不劣于放历史后
    （历史折叠变化时不会连带重算世界书）。语义上「世界设定先于故事」也合理。
    """
    return [
        {"type": BLOCK_SYSTEM_PROMPT, "enabled": True},
        {"type": BLOCK_CHARACTER_INFO, "enabled": True},
        # 用户信息紧跟角色信息：稳定前缀区，命中缓存
        {"type": BLOCK_USER, "enabled": True},
        # 世界书放历史前：常驻条目稳定命中缓存，世界设定先于故事的语义
        {"type": BLOCK_WORLD_BOOK, "enabled": True},
        {"type": BLOCK_HISTORY, "enabled": True},
        # 总结/记忆放在历史后：不打断历史的前缀缓存（历史 append-only 命中率最高）
        {"type": BLOCK_SUMMARY, "enabled": True},
        {"type": BLOCK_MEMORY, "enabled": True},
        # 本轮触发消息强制置末（build_messages 会兜底再次强制末尾）
        {"type": BLOCK_LAST_USER, "enabled": True},
    ]


@dataclass
class Preset:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT                # 单聊系统提示
    system_prompt_group: str = DEFAULT_SYSTEM_PROMPT_GROUP    # 群聊系统提示（发言用，与 director_prompt 选角分离）
    character_info_template: str = DEFAULT_CHARACTER_INFO_TEMPLATE
    temperature: float = 0.8
    max_tokens: int = 1024
    top_p: float = 0.95
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    director_prompt: str = DEFAULT_DIRECTOR_PROMPT  # 导演模式系统提示
    # 上下文模块顺序：每项 {"type":..., "enabled":bool, "label":str(自定义块用),
    # "content":str(自定义块用), "role":str(自定义块用, system/user/assistant, 默认 system)}
    context_blocks: list[dict] = field(default_factory=_default_context_blocks)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "system_prompt": self.system_prompt,
            "system_prompt_group": self.system_prompt_group,
            "character_info_template": self.character_info_template,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "director_prompt": self.director_prompt,
            "context_blocks": [dict(b) for b in self.context_blocks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Preset:
        # 老数据迁移：若 system_prompt 仍是旧版默认（含角色描述三段），升级为新单聊版。
        sys_prompt = d.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        if sys_prompt == LEGACY_SYSTEM_PROMPT:
            sys_prompt = DEFAULT_SYSTEM_PROMPT

        # context_blocks 兼容：老数据无此字段 -> 用默认；缺失新内置块则补齐。
        blocks = d.get("context_blocks")
        if not isinstance(blocks, list) or not blocks:
            blocks = _default_context_blocks()
        else:
            blocks = cls._normalize_blocks(blocks)

        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", "") or "",
            system_prompt=sys_prompt,
            # 老数据无 system_prompt_group 字段回退默认群聊版
            system_prompt_group=d.get("system_prompt_group", DEFAULT_SYSTEM_PROMPT_GROUP) or DEFAULT_SYSTEM_PROMPT_GROUP,
            character_info_template=d.get("character_info_template", DEFAULT_CHARACTER_INFO_TEMPLATE) or DEFAULT_CHARACTER_INFO_TEMPLATE,
            temperature=float(d.get("temperature", 0.8) or 0.8),
            max_tokens=int(d.get("max_tokens", 1024) or 1024),
            top_p=float(d.get("top_p", 0.95) or 0.95),
            frequency_penalty=float(d.get("frequency_penalty", 0.0) or 0.0),
            presence_penalty=float(d.get("presence_penalty", 0.0) or 0.0),
            director_prompt=d.get("director_prompt", DEFAULT_DIRECTOR_PROMPT) or DEFAULT_DIRECTOR_PROMPT,
            context_blocks=blocks,
        )

    @staticmethod
    def _normalize_blocks(blocks: list[dict]) -> list[dict]:
        """规整化 context_blocks：补 enabled 字段、补齐缺失的内置块、强制 LAST_USER 置末。

        - 已废弃的 BLOCK_INSTRUCTION 块在规整时丢弃（避免老数据残留无效块）。
        - LAST_USER 块强制置末：若有多个只保留一个，不在末尾则挪到末尾。
        """
        normalized = []
        seen_types = set()
        last_user_block = None
        for b in blocks:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if not btype:
                continue
            # 丢弃已废弃的 INSTRUCTION 块
            if btype == "instruction":
                continue
            # 内置块只保留一份（去重）
            if btype in BUILTIN_BLOCK_TYPES:
                if btype in seen_types:
                    continue
                seen_types.add(btype)
                block_item = {"type": btype, "enabled": bool(b.get("enabled", True))}
                if btype == BLOCK_LAST_USER:
                    last_user_block = block_item
                    continue  # 不直接加入，最后强制置末
                normalized.append(block_item)
            else:
                # 自定义块
                btype = BLOCK_CUSTOM
                normalized.append({
                    "type": BLOCK_CUSTOM,
                    "enabled": bool(b.get("enabled", True)),
                    "label": b.get("label", "自定义模块"),
                    "content": b.get("content", ""),
                    "role": _normalize_custom_role(b.get("role")),
                })
        # 补齐缺失的内置块（按默认顺序追加到末尾）
        for btype_item in _default_context_blocks():
            t = btype_item["type"]
            if t not in seen_types:
                seen_types.add(t)
                block_item = {"type": t, "enabled": True}
                if t == BLOCK_LAST_USER:
                    last_user_block = block_item
                else:
                    normalized.append(block_item)
        # LAST_USER 强制置末
        if last_user_block is not None:
            normalized.append(last_user_block)
        return normalized
