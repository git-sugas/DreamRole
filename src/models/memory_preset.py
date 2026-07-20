"""记忆整理预设：供 summary 与 embedding_hybrid 两种记忆模式共用一份独立于正文 API 的配置。

本预设负责记忆的整理/总结调用：
  - summary 模式：用 summary_prompt，输出一段连贯整合文本，存成 JSON。
  - embedding_hybrid 模式：用 hybrid_system_prompt，输出 `[triggers: ...] 明细` 条目，纯追加入库。
两模式共享本预设的 api_id（独立 API 绑定）与 temperature/max_tokens/top_p；
提示词各自一份（输出约定不同，故不共享），均在此可编辑。

不绑死具体 LLM，复用角色绑定的 API 实例（仅取其 base_url/api_key/model）。
"""
from __future__ import annotations
from dataclasses import dataclass, field


# 默认总结提示词（summary 模式用）：summary 输出一段连贯记忆文本
# （或简洁条目，每行一条），不按类型分组、不加 [类型] 标记。
# 输出会直接作为 memory_text 包进 <memory> 标签注入上下文。
DEFAULT_MEMORY_SUMMARY_PROMPT = (
    "你是一个角色记忆总结助手。请根据角色【{{char_name}}】的「当前记忆」和「新对话内容」，"
    "整合出【{{char_name}}】视角的最新记忆，覆盖式输出。\n\n"
    "要求：\n"
    "1. [!] **只记录【{{char_name}}】自己的事**：【{{char_name}}】自己说过的话、做过的事、"
    "发生在【{{char_name}}】身上的事（如别人对【{{char_name}}】说的话/做的事、"
    "【{{char_name}}】的状态变化、与他人的关系变化）。**不要把其他角色自己的言行、背景、"
    "经历整理进【{{char_name}}】的记忆**--其他角色说给用户的话、其他角色之间的互动、"
    "其他角色自己的故事，都不属于【{{char_name}}】该记住的内容。只有当其他角色直接对"
    "【{{char_name}}】说话/做事时，才作为「【{{char_name}}】经历的事」记录。\n"
    "2. 新对话内容格式为多行「角色名：发言内容」，请注意区分发言者，只提取与"
    "【{{char_name}}】相关的信息。\n"
    "3. 保留仍有效的重要旧记忆，删去过时或被新对话修正的内容，整合新信息并去除冗余。\n"
    "4. 输出为一段连贯的记忆文本（或简洁条目，每行一条），不要按类型分组、不要加 [类型] 标记。\n"
    "5. 不要输出任何标签、标题、前缀或说明性文字，只输出记忆内容。"
)

# 默认总结提示词（summary 模式用，群聊版）：群聊中其他角色也在场，需明确该角色
# 只能感知"在场时发生的事"。
DEFAULT_MEMORY_SUMMARY_PROMPT_GROUP = (
    "你是一个角色记忆总结助手。请根据角色【{{char_name}}】的「当前记忆」和「新对话内容」，"
    "整合出【{{char_name}}】视角的最新记忆，覆盖式输出。\n\n"
    "要求：\n"
    "1. [!] **只记录【{{char_name}}】自己的事**：群聊中其他角色也在场，"
    "【{{char_name}}】只能记住自己在场时发生且与自己相关的事（自己对别人说的话/做的事、"
    "别人对【{{char_name}}】说的话/做的事、【{{char_name}}】的状态与关系变化、共同参与的"
    "事件）。**不要把其他角色自己的言行、背景、经历整理进【{{char_name}}】的记忆**；"
    "不要记录【{{char_name}}】不可能知道的信息。\n"
    "2. 新对话内容格式为多行「角色名：发言内容」，群聊有多个角色发言，请区分清楚"
    "谁说了什么，只提取与【{{char_name}}】相关的信息。\n"
    "3. 保留仍有效的重要旧记忆，删去过时或被新对话修正的内容，整合新信息并去除冗余。\n"
    "4. 记忆条目中可适当标注涉及的其他角色（如「与雷恩有过争执」），但这是【{{char_name}}】的记忆。\n"
    "5. 输出为一段连贯的记忆文本（或简洁条目，每行一条），不要按类型分组、不要加 [类型] 标记。\n"
    "6. 不要输出任何标签、标题、前缀或说明性文字，只输出记忆内容。"
)


# ============ Embedding Hybrid 模式整理提示词（纯追加 + 三路召回）============
# 与 summary 的「整理式覆盖」不同：hybrid 模式纯追加入库，不清旧条目，
# 靠 seq 单调递增 + 提示词告诉 LLM「大 seq 为准」让冲突时新事实覆盖旧事实语义。
# 整理 API 输入：旧记忆（triggers + detail 原文，带 seq）+ 新对话原文。
# 整理 API 输出：每行一条 `[triggers: 词1,词2,词3] 明细`，解析后纯追加入库（每条新 seq）。
# triggers 是 3-4 个多字语义词（不含角色名、不含单字），供 triggers 路 FTS5 召回；
# detail 是完整明细原文，供 detail 路 FTS5 召回 + 最终渲染注入 <memory>。
DEFAULT_MEMORY_HYBRID_PROMPT = (
    "你是一个角色记忆管理助手。请根据角色的「当前已有记忆」和「新对话内容」，"
    "为角色【{{char_name}}】整理出新增的记忆条目。\n\n"
    "重要规则：\n"
    "1. 已有记忆按序号(seq)排列，序号越大越新。当新旧记忆冲突时（如关系从朋友变成恋人），"
    "以大序号为准--不要重复产出已有的事实，只产出真正新增或有变化的事实。\n"
    "2. [!] **只记录【{{char_name}}】自己的事**：【{{char_name}}】自己说过的话、做过的事、"
    "发生在【{{char_name}}】身上的事（如别人对【{{char_name}}】说的话/做的事、"
    "【{{char_name}}】的状态变化、与他人的关系变化）。"
    "**不要把其他角色自己的言行、背景、经历整理进【{{char_name}}】的记忆**--"
    "其他角色说给用户的话、其他角色之间的互动、其他角色自己的故事，"
    "都不属于【{{char_name}}】该记住的内容。"
    "只有当其他角色直接对【{{char_name}}】说话/做事时，才作为「【{{char_name}}】经历的事」记录。\n"
    "3. 新对话内容格式为多行「角色名：发言内容」，其中「角色名：」标明了是谁在说话。"
    "请据此判断每句话是谁说的：【{{char_name}}】说的话可记为「【{{char_name}}】曾说过…」；"
    "其他角色说的话只在它是对【{{char_name}}】说的时候才记为"
    "「某某对【{{char_name}}】说了…」。\n\n"
    "输出格式（严格遵守，不要输出任何说明性文字）：\n"
    "- 每行一条记忆，格式为：[triggers: 词1,词2,词3,词4] 这条记忆的明细内容\n"
    "- triggers 必须恰好 4 个，用英文逗号分隔，满足以下全部条件：\n"
    "  · 每个词为 2-4 个汉字的语义单元，不包含角色名，不输出单字\n"
    "  · 4 个词必须分别覆盖：动作/事件、情绪/态度、对象/关系、场景/时间 四个不同维度\n"
    "  · 禁止使用近义词或语义重叠的词，每个词必须提供独立的检索入口\n"
    "- 明细是一句简洁完整的话，描述这条记忆的具体内容\n"
    "- 不要输出任何说明性文字，只输出记忆条目行\n\n"
    "示例输出格式：\n"
    "[triggers: 共进晚餐,轻松愉悦,姐姐,今晚] 宅宅和姐姐今晚一起吃了晚饭，聊到了明年的旅行计划\n"
    "[triggers: 商量探险,期待兴奋,姐姐,下周] 姐姐提到下周要出发去北方的山脉探险"
)

DEFAULT_MEMORY_HYBRID_PROMPT_GROUP = (
    "你是一个角色记忆管理助手。请根据角色的「当前已有记忆」和「新对话内容」，"
    "为角色【{{char_name}}】整理出新增的记忆条目。\n\n"
    "重要规则：\n"
    "1. 已有记忆按序号(seq)排列，序号越大越新。当新旧记忆冲突时（如关系从朋友变成恋人），"
    "以大序号为准--不要重复产出已有的事实，只产出真正新增或有变化的事实。\n"
    "2. [!] **只记录【{{char_name}}】自己的事**：群聊中其他角色也在场，"
    "【{{char_name}}】只能记住自己在场时发生且与自己相关的事（自己对别人说的话/做的事、"
    "别人对【{{char_name}}】说的话/做的事、【{{char_name}}】的状态与关系变化、共同参与的"
    "事件）。**不要把其他角色自己的言行、背景、经历整理进【{{char_name}}】的记忆**--"
    "其他角色之间的互动、其他角色自己的故事，不属于【{{char_name}}】该记住的内容；"
    "不要记录【{{char_name}}】不可能知道的信息。\n"
    "3. 新对话内容格式为多行「角色名：发言内容」，群聊有多个角色发言，请区分清楚"
    "谁说了什么，只提取与【{{char_name}}】相关的信息。\n\n"
    "输出格式（严格遵守，不要输出任何说明性文字）：\n"
    "- 每行一条记忆，格式为：[triggers: 词1,词2,词3,词4] 这条记忆的明细内容\n"
    "- triggers 必须恰好 4 个，用英文逗号分隔，满足以下全部条件：\n"
    "  · 每个词为 2-4 个汉字的语义单元，不包含角色名，不输出单字\n"
    "  · 4 个词必须分别覆盖：动作/事件、情绪/态度、对象/关系、场景/时间 四个不同维度\n"
    "  · 禁止使用近义词或语义重叠的词，每个词必须提供独立的检索入口\n"
    "- 明细是一句简洁完整的话，描述这条记忆的具体内容。可适当标注涉及的其他角色"
    "（如「与雷恩是旧识」），但这是【{{char_name}}】的记忆。\n"
    "- 不要输出任何说明性文字，只输出记忆条目行\n\n"
    "示例输出格式：\n"
    "[triggers: 并肩作战,信任默契,雷恩,昔日] 与雷恩是旧识，曾一起执行任务\n"
    "[triggers: 召集群聊,号召组织,用户,广场] 用户常在广场召集大家"
)


@dataclass
class MemoryPreset:
    """记忆整理预设（summary 与 hybrid 两模式共享 api_id 与生成参数，提示词各自一份，各分单/群聊两版）。"""
    id: str = "memory_preset"   # 固定单例 id
    api_id: str = ""            # 绑定独立 API（用于记忆整理/总结调用；空=回退角色绑定 API）
    summary_prompt: str = DEFAULT_MEMORY_SUMMARY_PROMPT             # summary 总结提示词（单聊）
    summary_prompt_group: str = DEFAULT_MEMORY_SUMMARY_PROMPT_GROUP # summary 总结提示词（群聊）
    temperature: float = 0.3    # 整理/总结需稳定，低温度
    max_tokens: int = 1024
    top_p: float = 0.9
    # ---- Embedding Hybrid 模式专用（emb+triggers+detail 三路召回融合 + 两次召回合并）----
    # hybrid_recall_weights: (emb, triggers, detail, seq) 四路融合权重，默认类比 Danbooru。
    # seq 权重让新记忆略优先（候选集内 min-max 归一化，与 trig/detail/emb 归一化口径一致）。
    # [!] seq 默认从 0.2 降到 0.1：0.2 在候选集 min-max 归一化下随跨度剧烈波动，
    # 弱相关新条目仅凭 seq 加成可能压过强相关旧条目，与「略优先」注释不符。
    hybrid_recall_weights: tuple = (0.5, 0.2, 0.1, 0.1)
    hybrid_recall_top_k: int = 15              # 注入上下文的记忆条数（top-N）
    hybrid_user_recall_weight: float = 0.6     # 两次召回合并时 user 输入召回的权重（assistant 则 1-此值）
    hybrid_system_prompt: str = DEFAULT_MEMORY_HYBRID_PROMPT          # hybrid 整理提示词（单聊）
    hybrid_system_prompt_group: str = DEFAULT_MEMORY_HYBRID_PROMPT_GROUP  # hybrid 整理提示词（群聊）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "api_id": self.api_id,
            "summary_prompt": self.summary_prompt,
            "summary_prompt_group": self.summary_prompt_group,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "hybrid_recall_weights": list(self.hybrid_recall_weights),
            "hybrid_recall_top_k": self.hybrid_recall_top_k,
            "hybrid_user_recall_weight": self.hybrid_user_recall_weight,
            "hybrid_system_prompt": self.hybrid_system_prompt,
            "hybrid_system_prompt_group": self.hybrid_system_prompt_group,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryPreset":
        if not d:
            return cls()
        # hybrid_recall_weights 容错：可能存成 list 或缺失
        raw_w = d.get("hybrid_recall_weights", [0.5, 0.2, 0.1, 0.1])
        if not isinstance(raw_w, (list, tuple)) or len(raw_w) != 4:
            raw_w = [0.5, 0.2, 0.1, 0.1]
        weights = tuple(float(x) for x in raw_w)
        return cls(
            id=d.get("id", "memory_preset"),
            api_id=d.get("api_id", ""),
            summary_prompt=d.get("summary_prompt", DEFAULT_MEMORY_SUMMARY_PROMPT),
            # 老数据无 *_group 字段回退默认群聊版，零迁移成本
            summary_prompt_group=d.get("summary_prompt_group", DEFAULT_MEMORY_SUMMARY_PROMPT_GROUP),
            temperature=float(d.get("temperature", 0.3)),
            max_tokens=int(d.get("max_tokens", 1024)),
            top_p=float(d.get("top_p", 0.9)),
            hybrid_recall_weights=weights,
            hybrid_recall_top_k=int(d.get("hybrid_recall_top_k", 15)),
            hybrid_user_recall_weight=float(d.get("hybrid_user_recall_weight", 0.6)),
            hybrid_system_prompt=d.get("hybrid_system_prompt", DEFAULT_MEMORY_HYBRID_PROMPT),
            hybrid_system_prompt_group=d.get("hybrid_system_prompt_group", DEFAULT_MEMORY_HYBRID_PROMPT_GROUP),
        )


def default_memory_preset() -> MemoryPreset:
    return MemoryPreset()