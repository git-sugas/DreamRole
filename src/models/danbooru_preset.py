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


# 默认加工提示词：候选标签是「召回锚点」不是「唯一来源」，覆盖不到的画面要素用 Danbooru 风格英文 tag 补。
# 输出格式「tag 串 + 自然语言」混合（目标画图模型支持），用单独成行的 `/` 作分隔符：
#   第一行 = 逗号分隔英文 tag 串；第二行 = `/`；第三行起 = 自然语言 1-3 句。
# [!] Tag 排列顺序按目标模型官方规格（Count -> Name -> Framing/POV -> Expression -> Physical
#     -> Clothing -> Action -> Scene -> Objects），Framing/POV 前置、Action 后置。
# [!] 自然语言规则约束 `/` 后的段：A1/A2/B1 简单词、具体可视化、禁抽象/修辞、每句<30词、
#     主动语态、多角色分开描述、Subject+Posture+Action+Location 结构、动作强度用主动动词描述流体/冲击。
# [!] allow_nsfw=True 时由 process_to_tags 在末尾追加 DEFAULT_DANBOORU_NSFW_PROMPT 子段（生物特征/
#     流体冲击/动态姿势/动态效果 + 自然语言增强），本提示词不内置 NSFW 规则（避免污染非 NSFW 场景）。
DEFAULT_DANBOORU_SYSTEM_PROMPT = (
    "你是一个 Danbooru 标签翻译助手。目标画图模型吃 Danbooru 英文 tag，并支持「tag 串 + 自然语言」混合输入，"
    "你的任务是根据用户给的中文描述，整理成一条适合文生图（如 Anima/NovelAI/SD 动漫模型）的正向提示词。\n\n"
    "用户提示词中的 {{标签}} 处会注入本次召回的候选标签列表（每行一条，"
    "字段以竖线 | 分隔，格式：name | cn_name | post_count | category）。"
    "**召回候选只是翻译锚点和常用 tag 提示，不是唯一来源**--召回受 embedding/FTS5 限制，"
    "常常覆盖不到中文描述里的画面要素（如站立 standing、家具 furniture、青瓷 celadon 等），"
    "这些缺失的要素你必须自行用 Danbooru 风格英文 tag 补全，不能因为候选里没有就漏标，"
    "否则画面关键要素丢失、出图残缺。各字段含义：\n"
    "- name：Danbooru 英文原名（下划线连接），输出时必须用这个原名。\n"
    "- cn_name：中文译名或别名，是你理解 tag 语义的主要依据。\n"
    "- post_count：该 tag 在 Danbooru 的帖子数，数值越大越常见、越主流；"
    "同义 tag 优先选数值高的。\n"
    "- category：分类编码，0=general（通用概念：动作/姿势/特征/物品/场景），"
    "1=artist（画师），3=copyright（作品/系列，如某动漫名），"
    "4=character（具体角色名），5=meta（元信息：画质/数量/视角等）。\n\n"
    "要求：\n"
    "1. **候选里有的优先从候选选**（用 cn_name 对齐中文语义）；**候选覆盖不到的画面要素，"
    "自行补 Danbooru 风格英文 tag**（下划线连接的标准 tag，如 standing / wooden_furniture / "
    "celadon / side_window 等），确保中文描述里的关键要素（人物数量/动作/姿势/服装/场景/陈设/光影）"
    "尽可能都有对应 tag 覆盖。不要因为候选里没有就漏标。\n"
    "2. **输出格式**：先输出逗号分隔的英文 tag 串（写成一行，不要换行），然后另起一行用一个单独的 `/` "
    "作为分隔符，再另起一行输出 1-3 句自然语言句子补充描述画面（自然语言只是 tag 的补充）。格式示例：\n"
    "   1girl, solo, upper_body, smile, blue_hair, long_hair, red_eyes, school_uniform, holding_book, classroom, window\n"
    "   /\n"
    "   A girl with blue hair sits sideways at a desk, flipping a book, by the window in a classroom.\n"
    "   不要加序号、不要任何说明文字、不要输出 [img:...] 等任何非 tag 文本。\n"
    "   [!] `/` 必须单独成一行（严格遵守示例格式），自然语言不要接在 tag 串同一行后面。\n"
    "   若描述过于简单无法写出有意义的自然语言，可只输出 tag 串（不输出 `/` 和自然语言）。\n"
    "3. **Tag 排列顺序**（严格遵守，便于模型理解画面层次）：\n"
"   主体数量（1girl/2girls 等）-> 角色名（如来自某作品）-> 构图/视角（upper body/close-up 等）"
" -> 表情（smile/crying 等）-> 身体特征（blue hair/red eyes 等）"
" -> 服装配饰（school uniform 等）-> 动作姿势（holding sword/sitting 等）"
" -> 场景环境（forest/night sky 等）-> 物体细节（rain/lantern 等）。\n"
"4. **自然语言规则**（仅约束 `/` 后的自然语言段，不影响 tag 串）：\n"
"   - [!] 目标画图模型是 2B 级小模型：只用简单常见词（A1/A2/B1 级词汇，如 run/sit/big/red/look），"
"禁用高级复杂词和复杂语法（从句嵌套/倒装/被动长句都不要），只用简单主动语态短句。\n"
"   - 每句不超过 20 词；总共 1-3 句（自然语言只是补充，tag 才是主体）。\n"
"   - 只写具体可视化的内容（字面意义），禁抽象/主观/修辞词（如 gorgeous/vibrant/epic/dreamy），"
"禁比喻/讽刺/文化引用。必须是可以直接画出来的。\n"
"   - 句式顺序自由，唯一标准是让画面信息完整清楚。\n"
"   - 多角色场景：分别描述每个角色的姿势、动作和相对位置。\n"
    "5. 剔除冗余与矛盾（如 1girl 与 2girls 不共存；solo 与 couple 不共存）。\n"
    "6. 优先选 post_count 更高的常见 tag；语义模糊时宁可少选不要硬凑。\n"
    "7. **不要输出任何质量词、画质词**（如 best quality / masterpiece / highres / "
    "score_9 等），固定质量提示词由调用方在外部统一追加，你只负责画面内容。\n"
    "8. 若用户提示词中出现「角色外貌参考」块，按角色处理：多角色时按描述判断该用"
    "哪个（或哪几个）角色的外貌，不要把所有角色外貌都堆进去。[!] **固定特征类 tag"
    "（发型/发色/瞳色/肤色/脸型/体型/妆容等）必须原样写入结果，不可省略、不可替换、"
    "不可改写为近义词**（如参考给 single_side_bun 就输出 single_side_bun，不得擅自改成"
    " hair_bun 或 side_bun）；服装配饰类（衣物/鞋袜/首饰/道具等）按场景描述酌情选用，"
    "场景未提及时保留角色默认着装。\n"
    "9. [!] **必须选主体标签**：候选中以 `1girl/1boy/2girls/2boys/solo/couple/male_focus`"
    "为主的标签表达画面里的人物构成，你必须根据中文描述"
    "判断画面有几个人、什么性别，选合适的主体标签放在输出最前面。例如：\n"
    "   - 只有一个女性 -> `1girl, solo`\n"
    "   - 一男一女 -> `1girl, 1boy, couple`\n"
    "   - 两个女性 -> `2girls`\n"
    "   - 一个女性为画面重心但有男性在场 -> `1girl, male_focus`\n"
    "   [!] 画面最多 2 人，绝不画第 3 人（与下方视角前缀段约束一致）；描述提到 3 人及以上时仍只选 1 人款或 2 人款主体标签，多余人物不出现在画面里。\n"
    "   切勿漏选主体标签，否则模型不知道画几个人；切勿选互相矛盾的数量标签。\n"
    "   **视角前缀对应人数**（中文描述以「第一人称视角」/「第三人称视角」/"
"「第三人称旁观视角」开头，据此选主体标签而非自由判断人数）：\n"
"   - 「第一人称视角」：从观看者眼睛看当前角色，画面只有当前角色 1 人"
"（观看者是镜头本身不入画）。主体标签按当前角色性别选单人款"
"（如 `1girl` 或 `1boy`等），勿选 couple/2girls 等多人款。\n"
"   - 「第三人称视角」：旁观者看观看者与当前角色双人，画面只有这 2 人"
"（观看者入画）。主体标签固定为双人款（如一男一女 `1girl, 1boy, couple`；"
"两女 `2girls`等），勿选 solo/single 等单人款。两人性别按角色外貌参考块判断。\n"
"   - 「第三人称旁观视角」：观看者旁观/偷看当前角色与另一角色互动，"
"画面只有被看的 2 人（观看者是镜头不入画，勿把观看者算进人数）。"
"主体标签按被看两人性别选双人款（勿选 solo/single 单人款）；"
"两人性别按中文描述与角色外貌参考块判断，**注意被看两人可能都不"
"是观看者**，不要默认把观看者性别算进去。\n"
"   - 描述无视角前缀时，按中文描述实际提到的人数判断（原规则 9 用法）。\n"
"10. [!] **角色只有两只手，避免肢体冲突**（配合规则 5 的冲突剔除）：每个人物恰好两只手，"
"手部动作/姿势/持物类 tag 会让生图模型给每个 tag 都画出手，叠加过多或手位互相矛盾"
"会出现三只手/四只手等肢体畸变。选词须遵守：\n"
"   - 双手整体姿势（arms_crossed / hands_on_hips / arms_behind_back / both_hands_up 等）"
"互斥，单角色同一画面只选一个，不可叠加。\n"
"   - 持物 tag（holding_X / carrying_X 等）每个占一只手，单角色最多两个（两只手各拿一件）。\n"
"   - 占用双手的姿势 tag 不可与持物 tag 共存（双手已被姿势占用，无空闲手再持物）。\n"
"   - 局部手部动作（covering_mouth / touching_face / adjusting_hair / pointing 等）"
"也占手位，同样计入双手预算，不可与上述姿势/持物 tag 叠加到超过两手。\n"
"   自检：把选出的 tag 按角色清点每只手在做什么，任何一只手被同时要求做两件不同的事、"
"或单角色手数超过两只，就删掉冲突项里较次要的那个（优先保留与画面主旨最相关的动作）。"
)

# 场景模式提示词（[2026-08-21 用户指示] 生成场景用全自然语言，不限制结构）。
# [!] process_to_tags(scene_mode=True) 用它替代 system_prompt：输出纯英文自然语言画面描述，
#     无 tag 串格式、无 `/` 分隔符、[2026-08-27] 硬约束无人物（空镜头纯场景）、无手部/肢体约束
#     （场景/物品图标等无人图；人物图走 tag 模式）。召回候选仍注入 {{标签}} 作术语锚点
#     （如地名/器物/天气的英文标准说法），但输出形态是流畅句子不是 tag。
DEFAULT_DANBOORU_SCENE_PROMPT = (
    "你是一个文生图提示词作家。目标画图模型支持自然语言输入，你的任务是把用户给的"
    "中文场景描述，写成一段适合文生图的英文自然语言画面描述。\n\n"
    "用户提示词中的 {{标签}} 处会注入本次召回的候选标签列表（每行一条，"
    "字段以竖线 | 分隔，格式：name | cn_name | post_count | category）。"
    "召回候选只是术语锚点（地名/器物/天气/材质等的英文标准说法），"
    "覆盖不到的画面要素你自行用准确的英文表达补全，不能因为候选里没有就漏写。\n\n"
    "要求：\n"
    "1. **输出为一段流畅的英文自然语言**，不输出 tag 逗号串、不输出 `/` 分隔符、"
    "不输出任何标题/序号/说明文字/markdown 代码块。\n"
    "2. [!] 目标画图模型是 2B 级小模型：只用简单常见词（A1/A2/B1 级词汇），"
    "禁用高级复杂词和复杂语法（从句嵌套/倒装/被动长句都不要），只用简单主动语态短句；"
    "每句不超过 15 词，总共不超过 4 句。\n"
    "3. **结构完全自由**（行文顺序不设模板），在句数词级限制内把画面信息写完整：\n"
    "   场景类型与整体氛围、空间布局与构图视角、光照与天气、主要景物的形态位置、"
    "   色彩与材质质感、以及描述里提到的一切可视细节。\n"
    "4. 只写具体可视化的内容（字面意义），禁抽象/主观/修辞词（如 gorgeous/vibrant/epic），"
    "禁比喻/讽刺/文化引用。必须是可以直接画出来的。\n"
    "5. **不要输出任何质量词、画质词**（如 best quality / masterpiece / highres），"
    "固定质量提示词由调用方在外部统一追加，你只负责画面内容。\n"
    "6. **画面中绝不出现人物**：无人空镜头的纯场景（环境/器物/建筑/自然景观/氛围）。"
    "即使用户描述里提到人物，也忽略人物、不画人（不画人物剪影/肢体/手的特写），"
    "画面重心始终是场景本身。\n"
    "7. 若用户提示词中出现「角色外貌参考」块，忽略它、不据此画人（场景图强制无人）。"
)

# NSFW 场景增强子段（固定常量，不暴露为可编辑字段）。
# [!] 由 process_to_tags 在 preset.allow_nsfw=True 时拼入 system_prompt 末尾；
#     allow_nsfw=False 时不注入（行为同旧版，非 NSFW 场景不被污染）。
# [!] 包含官方 NSFW 规格：生物特征（按性别选）+ 流体冲击 tag + 动态姿势 tag +
#     高强度动态效果 tag + 自然语言增强（与 §14 主提示词的自然语言规则衔接，
#     `/` 后的自然语言段在 NSFW 场景必须包含流体/冲击描写）。
DEFAULT_DANBOORU_NSFW_PROMPT = (
    "=== NSFW 场景增强规则（本段仅在 NSFW 开启时生效）===\n"
    "若描述为 NSFW/裸露/性行为场景，必须为所有角色补充显式的视觉性特征 tag，"
    "并强化物理冲击与流体动态，以最大化画面冲击力。\n\n"
    "1. **必须补充的生物特征 tag**（按角色性别选，所有出场角色都要有）：\n"
    "   - 女性：female pubic hair, 具体胸部尺寸（如 huge breasts / large breasts / small breasts），"
    "臀部尺寸（huge ass / large ass），大腿粗细（thick thighs），nipples, vulva, genitals。\n"
    "   - 男性：penis, male pubic hair, muscular（若描述为肌肉体型）。\n\n"
    "2. **必须补充的流体与冲击 tag**（表现强度）：\n"
    "   impact (effect), flying sweat, splashing liquid, saliva trail, glistening skin, "
    "skin indentation, red skin, blush。\n\n"
    "3. **动态姿势 tag**（明确动作体位）：\n"
    "   按描述选 fucked from behind / cowgirl position / bent over / from side / against wall 等，"
    "明确身体相对位置与体位。\n\n"
    "4. **高强度动态效果 tag**（剧烈场景必须加）：\n"
    "   motion blur, shaking, vibrating, flying debris, splashing liquid。\n\n"
    "5. **自然语言增强**（`/` 后的自然语言段必须包含）：\n"
    "   用主动动词描述流体（sweat/saliva/liquid）\"flying\"/\"splashing\"/\"dripping\"，"
    "描述皮肤 \"indented\"/\"pressed\" 显示握力，描述身体 \"shaking\"/\"vibrating\" 显示冲击。\n"
    "   示例：Sweat and liquid splash in the air. The boy's hands press deep into the girl's thick thighs, "
    "causing skin indentation. Both bodies are glistening with sweat and shaking from the impact.\n"
    "=== NSFW 场景增强规则结束 ==="
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
    scene_system_prompt: str = DEFAULT_DANBOORU_SCENE_PROMPT  # 场景模式（全自然语言，2026-08-21）
    temperature: float = 0.3             # 加工需稳定，低温度
    max_tokens: int = 300                # tag 串不长
    top_p: float = 0.9
    # ---- 库与模式段 ----
    manual_mode: bool = False            # True=手改模式（emb召回->用户勾选->LLM加工）
    allow_nsfw: bool = False             # 全局 nsfw 开关（False=召回时过滤 nsfw 标签）
    allow_categories: tuple[int, ...] = (0, 1, 3, 4, 5)  # 召回时保留的 category 集合（默认全开）
    enable_wiki_fts: bool = True         # 控制 wiki 向量召回路是否参与（True=emb_wiki 路开，False=仅 emb_cn + cn_search）
    recall_top_n: int = 50               # embedding 召回数
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT   # 固定负面模板，拼接在加工产出后
    positive_prefix: str = DEFAULT_POSITIVE_PREFIX   # 固定正面质量/画风词，拼在加工产出前
    csv_path: str = ""                   # 记住上次导入的 CSV 路径
    last_csv_mtime: str = ""             # 上次建库对应的 CSV 修改时间（字符串）
    last_db_count: int = 0               # 库内当前条数
    # ---- 融合权重段（recall_candidates 的 score = w_emb·emb_cn_sim + w_wiki·emb_wiki_sim + w_fts·fts_sim + w_pc·pc_norm）----
    # 默认值 = 经验值；用户可在设置对话框调整，不强制归一化（便于做总分高低对比）。
    # [!] 向量路一律整句去标签（bge-m3/qwen3-emb 不吃 [cn_name]/[Wiki] 标签，标签是噪声词）：
    #     emb_cn 路（ChromaDB danbooru_tags_cn，emb 文本=cn_name 实际内容）、
    #     emb_wiki 路（ChromaDB danbooru_tags_wiki，emb 文本=wiki 实际内容，enable_wiki_fts=False 时跳过）。
    # [!] 长文本召回从 FTS5 迁向量：wiki 不再走 wiki_search bm25（已删表），改走 emb_wiki 整句语义召回。
    #     短词/结构化词仍走 bm25 拆词：cn_search（中文别名集合）保留 FTS5。
    # w_fts 给 cn_name 精确命中（bm25 拆词，高置信度）；w_emb/w_wiki 给整句语义召回。
    weight_emb: float = 0.30             # emb_cn 路（cn_name 整句向量）
    weight_fts: float = 0.20             # fts_cn 路（cn_search bm25 拆词）
    weight_wiki: float = 0.30            # emb_wiki 路（wiki 整句向量，enable_wiki_fts=False 时此项为 0）
    weight_pc: float = 0.20              # post_count 常见度归一化

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "api_id": self.api_id,
            "system_prompt": self.system_prompt,
            "scene_system_prompt": self.scene_system_prompt,
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
            system_prompt=d.get("system_prompt", DEFAULT_DANBOORU_SYSTEM_PROMPT) or DEFAULT_DANBOORU_SYSTEM_PROMPT,
            scene_system_prompt=d.get("scene_system_prompt", DEFAULT_DANBOORU_SCENE_PROMPT) or DEFAULT_DANBOORU_SCENE_PROMPT,
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
            weight_emb=float(d.get("weight_emb", 0.30)),
            weight_fts=float(d.get("weight_fts", 0.20)),
            weight_wiki=float(d.get("weight_wiki", 0.30)),
            weight_pc=float(d.get("weight_pc", 0.20)),
        )


def default_danbooru_preset() -> DanbooruPreset:
    return DanbooruPreset()


# ============ 解析加工输出 ============
def _clean_tag_segment(seg: str) -> str:
    """清洗 tag 段：统一分隔符为半角逗号、去空、剔除含中文/全角标点的说明性片段、
    剔除 [img:...] 片段、去重保序。返回逗号分隔的干净 tag 串。

    合法 Danbooru tag 是英文/数字/下划线/半角括号冒号；LLM 偶尔输出说明文字
    （如「结果如下：」），靠「合法 tag 不含中文/中文标点」剔除之。
    """
    import re
    if not seg:
        return ""
    # 统一全角逗号、顿号、换行、分号为半角逗号（tag 段内部不应有换行，但容错处理）
    s = seg
    for ch in ("，", "、", "\n", "\r", ";", "；"):
        s = s.replace(ch, ",")
    parts = [p.strip().strip('"').strip("'").strip() for p in s.split(",")]
    # 剔除含中文或全角标点的片段
    parts = [p for p in parts if p and not re.search(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', p)]
    # [!] 兜底剔除 [img:...] 片段：合法 Danbooru tag 不含 [img: 字面量。根因：
    # 破限词默认末尾含「必须输出两个 [img:...] 标签」的格式要求（给正文 LLM 触发
    # 出图用），但破限词会被注入到所有 LLM 调用（含 Danbooru 加工 LLM）。加工 LLM
    # 被注入后会误输出 [img:英文描述]，上面的中文正则剔不掉纯英文 [img:xxx]，
    # 会原样透传给 ComfyUI positive 串污染生图（生图异常/生图后 UI 卡在生成态）。
    # 实际修复：用户在「破限设置」删掉破限词里的 img 格式要求段（正文出图改由
    # DEFAULT_SYSTEM_PROMPT 最开头的「图片标签要求」块保证，不依赖破限词）。
    # 此处再加一道兜底过滤作为防御：万一以后破限词或 LLM 幻觉又带出 [img:...]，
    # 能保证加工输出始终是纯 tag 串。合法 tag 不含 [img:，正常输出不受影响。
    parts = [p for p in parts if "[img:" not in p.lower()]
    # [!] 剔除单独的 `/` 片段：合法 Danbooru tag 用下划线连接不含斜杠。根因：
    # parse_tag_output 兜底分支（tag 段清洗后为空时整串重洗）会把 `/` 分隔符带进
    # _clean_tag_segment，若不过滤会原样透传给最终输出（如 T9 场景：LLM 只输出
    # `/` 和自然语言没出 tag，兜底返回 `/, A girl sits.` 是错的）。过滤后单独 `/`
    # 被剔除，合法 tag 不受影响。
    parts = [p for p in parts if p != "/"]
    # 去重保序
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return ", ".join(out)


def parse_tag_output(text: str) -> str:
    """解析 LLM 加工输出为「tag 串 + 可选自然语言」整串。

    支持两种输出格式（向后兼容）：
    - 纯 tag 逗号串（旧格式）：整串当 tag 段清洗，返回逗号分隔 tag 串。
    - tag 串 + `/` + 自然语言（新格式，目标画图模型支持 tag串+自然语言混合输入）：
      按 `/` 分隔符切出 tag 段与自然语言段，tag 段按 _clean_tag_segment 清洗，
      自然语言段整段保留（仅 strip 首尾空白），返回 `"{cleaned_tags}, {nl_part}"`。

    分隔符识别优先级（找第一个即切）：
    1. 单独成行的 `/`（strip 后 == "/"，最规范，匹配提示词示例格式）。
    2. 行内 ` / `（空格斜杠空格，容错 LLM 把分隔符写在同一行）。
    3. 都找不到 = 整串当 tag 段（纯 tag 串场景，向后兼容）。

    容错：去代码块包裹、去首尾引号。返回值仍为 str（保持 API 契约），
    下游 ComfyUI 把 positive 当字符串占位符替换不解析结构，tag串+自然语言整串
    直接喂模型即可。
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

    tag_seg = s
    nl_seg = ""
    # 优先级 1：单独成行的 `/`（strip 后 == "/"）
    lines = s.split("\n")
    sep_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == "/":
            sep_idx = i
            break
    if sep_idx >= 0:
        tag_seg = "\n".join(lines[:sep_idx])
        nl_seg = "\n".join(lines[sep_idx + 1:]).strip()
    else:
        # 优先级 2：行内 ` / `（空格斜杠空格）
        idx = s.find(" / ")
        if idx >= 0:
            tag_seg = s[:idx]
            nl_seg = s[idx + 3:].strip()
        else:
            # 优先级 3（[2026-08-21] LLM 偶发把自然语言接在 tag 串同一行，逗号分隔）：
            # 无分隔符时按「句号 + 空格段」反推散文起点——找最后一个以句号结尾的逗号段，
            # 从它向前回溯连续含空格的段，那段连续区即自然语言（Danbooru tag 用下划线
            # 连接不含空格；含空格且邻接句号的段是散文）。找不到句号则整串按 tag 处理
            # （纯 spaced-tag 输出不误切，保持旧行为）。
            chunks = [c.strip() for c in s.split(",")]
            period_idx = -1
            for i, c in enumerate(chunks):
                if c.endswith("."):
                    period_idx = i
            if period_idx >= 0:
                start = period_idx
                while start - 1 >= 0 and " " in chunks[start - 1] and not chunks[start - 1].endswith("."):
                    # 仅回溯「明显像散文开头」的段：>=3 个空格分隔词（单双词的 spaced-tag 不吞）
                    if len(chunks[start - 1].split()) >= 3:
                        start -= 1
                    else:
                        break
                tag_seg = ", ".join(chunks[:start])
                nl_seg = ", ".join(chunks[start:])

    cleaned_tags = _clean_tag_segment(tag_seg)
    if not cleaned_tags:
        # tag 段清洗后为空（极端容错：LLM 只输出了自然语言没出 tag），退回整串清洗兜底
        return _clean_tag_segment(s)
    if nl_seg:
        return f"{cleaned_tags}, {nl_seg}"
    return cleaned_tags
