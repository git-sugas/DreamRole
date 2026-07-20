"""Danbooru Tag 加工预设：供「中文输入 → Danbooru 英文 tag」的两段式 RAG 加工。

流程：embedding 召回候选 tag → LLM 从候选里选+排序输出英文 tag 串（仿记忆整理）。
本预设只管 LLM 加工这一段（API 绑定 + system_prompt + 生成参数）；
embedding 用「记忆整理」标签页配置的 API（复用 MemoryPreset 的 embedding 配置，
不重复造配置项）。

独立于正文 API：加工时优先用这里绑定的 api_id（建议一个便宜小模型专门跑 tag 加工，
省 token）；未绑定回退会话当前 API；仍无可用回退首个启用的 API（头像生成等无会话
上下文场景的兜底，由 danbooru_service._resolve_llm_api 三级解析实现）。

提示词里可写 `{{标签}}` 占位：手改模式下注入用户勾选的 tag 列表，
自动模式下注入 embedding 全量召回的候选列表。

另含一组「库/模式/nsfw/负面」配置（csv 路径、是否手改、nsfw 开关、召回数、负面模板），
供 Danbooru 设置对话框编辑。
"""
from __future__ import annotations
from dataclasses import dataclass


# 默认加工提示词：约束 LLM 只从候选中选 + 输出逗号分隔英文 tag 串
DEFAULT_DANBOORU_SYSTEM_PROMPT = (
    "你是一个 Danbooru 标签翻译助手。目标画图模型吃 Danbooru 英文 tag，"
    "你的任务是根据用户给的中文描述，从下方候选标签中选取最贴切的，"
    "整理成一条适合文生图（如 Anima/NovelAI/SD 动漫模型）的正向提示词。\n\n"
    "用户提示词中的 {{标签}} 处会注入本次可选的候选标签列表（每行一条，"
    "字段以竖线 | 分隔，格式：name | cn_name | post_count | category）。各字段含义：\n"
    "- name：Danbooru 英文原名（下划线连接），输出时必须用这个原名。\n"
    "- cn_name：中文译名或别名，是你理解 tag 语义的主要依据。\n"
    "- post_count：该 tag 在 Danbooru 的帖子数，数值越大越常见、越主流；"
    "同义 tag 优先选数值高的。\n"
    "- category：分类编码，0=general（通用概念：动作/姿势/特征/物品/场景），"
    "1=artist（画师），3=copyright（作品/系列，如某动漫名），"
    "4=character（具体角色名），5=meta（元信息：画质/数量/视角等）。\n\n"
    "要求：\n"
    "1. 只能从候选标签中挑选，不要生造英文 tag，不要输出中文。\n"
    "2. 输出为逗号分隔的英文 tag 串（用下划线连接的 Danbooru 原名），不要换行、"
    "不要加序号、不要任何说明文字。\n"
    "3. 顺序参考：主体（如 1girl/1boy）-> 数量 -> 动作/姿势 -> 表情 -> 发型发色 -> "
    "服装配饰 -> 场景背景 -> 构图。\n"
    "4. 剔除冗余与矛盾（如 1girl 与 multiple_girls 不共存；solo 与多人组不共存）。\n"
    "5. 优先选 post_count 更高的常见 tag；语义模糊时宁可少选不要硬凑。\n"
    "6. **不要输出任何质量词、画质词**（如 best quality / masterpiece / highres / "
    "score_9 等），固定质量提示词由调用方在外部统一追加，你只负责画面内容。\n"
    "7. 若候选中没有能覆盖中文描述关键要素的 tag，可在末尾补充少量 Danbooru 风格的"
    "英文短语（如 black_thighhighs），但总数不超过 2 个。\n"
    "8. 若用户提示词中出现「角色外貌参考」块，请从中选取与中文描述主体一致的角色"
    "外貌 tag 融进输出；多角色时按描述判断该用哪个（或哪几个），不要把所有角色"
    "外貌都堆进去。\n"
    "9. [!] **必须选主体标签**：候选中以 `1girl/1boy/2girls/2boys/.../multiple_girls/"
    "multiple_boys/solo/couple` 为主的标签表达画面里的人物构成，你必须根据中文描述"
    "判断画面有几个人、什么性别，从中选合适的主体标签放在输出最前面。例如：\n"
    "   - 只有一个女性 -> `1girl, solo`\n"
    "   - 一男一女 -> `1girl, 1boy, couple`\n"
    "   - 两个女性 -> `2girls`\n"
    "   - 一个女性为画面重心但有男性在场 -> `1girl, male_focus`\n"
    "   - 多个女性 -> `multiple_girls`\n"
    "   切勿漏选主体标签，否则模型不知道画几个人；切勿选互相矛盾的数量标签。"
)

# 默认固定正面提示词前缀（拼在加工产出的正向 tag 之前，质量与画风由它统一控制）
DEFAULT_POSITIVE_PREFIX = (
    "best quality, masterpiece, highres, absurdres, very aesthetic"
)

# 默认固定负面模板（动漫模型通用）
DEFAULT_NEGATIVE_PROMPT = (
    "worst quality, low quality, bad anatomy, bad hands, missing fingers, "
    "extra digits, fewer digits, cropped, watermark, signature, username, "
    "error, jpeg artifacts"
)


@dataclass
class DanbooruPreset:
    """Danbooru tag 加工预设（单例）。

    LLM 加工段：api_id / system_prompt / temperature / max_tokens / top_p。
    库与模式段：manual_mode / allow_nsfw / recall_top_n / negative_prompt /
               csv_path / last_csv_mtime / last_db_count。
    """
    id: str = "danbooru_preset"          # 固定单例 id
    # ---- LLM 加工段 ----
    api_id: str = ""                     # 绑定独立 API（空=回退会话当前 API）
    system_prompt: str = DEFAULT_DANBOORU_SYSTEM_PROMPT
    temperature: float = 0.3             # 加工需稳定，低温度
    max_tokens: int = 300                # tag 串不长
    top_p: float = 0.9
    # ---- 库与模式段 ----
    manual_mode: bool = False            # True=手改模式（emb召回->用户勾选->LLM加工）
    allow_nsfw: bool = False             # 全局 nsfw 开关（False=召回时过滤 nsfw 标签）
    allow_categories: tuple[int, ...] = (0, 1, 3, 4, 5)  # 召回时保留的 category 集合（默认全开）
    enable_wiki_fts: bool = True         # FTS5 是否召回 wiki 路（False=仅 cn_search，退回纯 cn 逻辑）
    recall_top_n: int = 50               # embedding 召回数
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT   # 固定负面模板，拼接在加工产出后
    positive_prefix: str = DEFAULT_POSITIVE_PREFIX   # 固定正面质量/画风词，拼在加工产出前
    csv_path: str = ""                   # 记住上次导入的 CSV 路径
    last_csv_mtime: str = ""             # 上次建库对应的 CSV 修改时间（字符串）
    last_db_count: int = 0               # 库内当前条数
    # ---- 融合权重段（recall_candidates 的 score = w_emb·emb + w_fts·fts + w_wiki·wiki + w_pc·pc_norm）----
    # 默认值 = 经验值；用户可在设置对话框调整，不强制归一化（便于做总分高低对比）。
    # w_fts 给 cn_name 精确命中（高置信度）；w_wiki 给 wiki 语义兜底命中（低置信度，默认远低于 w_fts）。
    # [!] fts 默认从 0.35 降到 0.20、pc 从 0.15 升到 0.20：本数据结构下 79.6% 的词 df=1 ->
    # IDF 二值化、bm25 退化成弱信号（区分度低），故让 emb 主力 + pc 常见度主导同义词排名。
    weight_emb: float = 0.5
    weight_fts: float = 0.20
    weight_wiki: float = 0.10
    weight_pc: float = 0.20

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "api_id": self.api_id,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "manual_mode": self.manual_mode,
            "allow_nsfw": self.allow_nsfw,
            "allow_categories": list(self.allow_categories),
            "enable_wiki_fts": self.enable_wiki_fts,
            "recall_top_n": self.recall_top_n,
            "negative_prompt": self.negative_prompt,
            "positive_prefix": self.positive_prefix,
            "csv_path": self.csv_path,
            "last_csv_mtime": self.last_csv_mtime,
            "last_db_count": self.last_db_count,
            "weight_emb": self.weight_emb,
            "weight_fts": self.weight_fts,
            "weight_wiki": self.weight_wiki,
            "weight_pc": self.weight_pc,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DanbooruPreset":
        if not d:
            return cls()
        return cls(
            id=d.get("id", "danbooru_preset"),
            api_id=d.get("api_id", ""),
            system_prompt=d.get("system_prompt", DEFAULT_DANBOORU_SYSTEM_PROMPT),
            temperature=float(d.get("temperature", 0.3)),
            max_tokens=int(d.get("max_tokens", 300)),
            top_p=float(d.get("top_p", 0.9)),
            manual_mode=bool(d.get("manual_mode", False)),
            allow_nsfw=bool(d.get("allow_nsfw", False)),
            allow_categories=tuple(
                int(x) for x in (d.get("allow_categories") or (0, 1, 3, 4, 5))
                if isinstance(x, (int, float)) or (isinstance(x, str) and x.lstrip("-").isdigit())
            ) or (0, 1, 3, 4, 5),
            enable_wiki_fts=bool(d.get("enable_wiki_fts", True)),
            recall_top_n=int(d.get("recall_top_n", 50)),
            negative_prompt=d.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT),
            positive_prefix=d.get("positive_prefix", DEFAULT_POSITIVE_PREFIX),
            csv_path=d.get("csv_path", ""),
            last_csv_mtime=d.get("last_csv_mtime", ""),
            last_db_count=int(d.get("last_db_count", 0)),
            weight_emb=float(d.get("weight_emb", 0.5)),
            weight_fts=float(d.get("weight_fts", 0.20)),
            weight_wiki=float(d.get("weight_wiki", 0.10)),
            weight_pc=float(d.get("weight_pc", 0.20)),
        )


def default_danbooru_preset() -> DanbooruPreset:
    return DanbooruPreset()


# ============ 解析加工输出 ============
def parse_tag_output(text: str) -> str:
    """解析 LLM 加工输出为干净的英文 tag 逗号串。

    容错：去代码块包裹、去首尾引号、统一分隔符为半角逗号、去空、剔除含中文/
    全角标点的说明性片段、去重保序。LLM 偶尔输出说明文字（如「结果如下：」），
    这里靠「合法 Danbooru tag 不含中文/中文标点」剔除之。
    """
    if not text:
        return ""
    s = text.strip()
    # 去代码块包裹（```...``` 或 ```lang\n...\n```）
    if s.startswith("```"):
        s = s.strip("`")
        # 去掉可能的语言标识行
        s = s.split("\n", 1)[-1] if "\n" in s else s
    # 去首尾引号
    s = s.strip().strip('"').strip("'").strip()
    # 统一全角逗号、顿号、换行、分号为半角逗号
    for ch in ("，", "、", "\n", "\r", ";", "；"):
        s = s.replace(ch, ",")
    parts = [p.strip().strip('"').strip("'").strip() for p in s.split(",")]
    # 剔除含中文或全角标点的片段（合法 tag 是英文/数字/下划线/半角括号冒号）
    import re
    parts = [p for p in parts if p and not re.search(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', p)]
    # 去重保序
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return ", ".join(out)
