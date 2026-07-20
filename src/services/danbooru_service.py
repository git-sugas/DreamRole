"""Danbooru tag 加工服务：中文描述 → embedding 召回 + FTS5 字面召回 → 融合排序 → LLM 加工 → 英文 tag 串。

三段式 RAG（召回融合版）：
  1. 双路召回：
     - embedding 语义召回（ChromaDB，每 cn_name alias 一向量，复用 MemoryPreset 的 embedding 配置）
     - FTS5 字面召回（SQLite `danbooru_tag_fts`，一 tag 一行，bm25 + MATCH cn_name）
  2. 融合排序：`score = 0.5·emb_sim + 0.35·fts_sim + 0.15·pc_norm`，两路任一失败互为兜底
  3. LLM 加工（独立 DanbooruPreset 的 api_id + system_prompt，仿记忆整理）

库结构：
  - ChromaDB collection `danbooru_tags`，persist 到 data/danbooru_db/，独立于记忆 data/chroma/。
    打标方案：每 tag 一向量，embedding 文本 = `[cn_name] {cn_name} [Wiki] {wiki}`。
    [cn_name] 段是同义词集合（cn_name 整串），[Wiki] 段是 wiki 语义描述（wiki 进 embedding，
    bge-m3 长文本能力充分利用，旧拆 alias 方案 wiki 完全没进 emb）。
    metadata = 全量明细字段（name/cn_name_raw/post_count/category/nsfw），召回后直接取 cn_name_raw，
    不再反查 FTS5。wiki 不入 metadata（已在 embedding 文本里，反查不需要）。
  - SQLite `danbooru_tag_fts`（FTS5）：一 tag 一行（5万），列 name/cn_name_raw/cn_search/
    wiki_search/wiki(UNINDEXED)/post_count/category/nsfw。
    cn_search 走 MATCH + bm25 召回（主召回，高置信度 fts_sim）；
    wiki_search 走 MATCH + bm25 召回（语义兜底，低置信度 wiki_sim）--两路查询合并，
    cn 已命中的 tag 按 name 去重（cn 优先），wiki_sim 仅给 wiki-only 命中行；
    wiki UNINDEXED 旁存原始串供回查。

  ⚠️ wiki 经 [Wiki] 标签进 embedding 文本参与 emb 路召回（打标方案核心改进）；
  FTS5 经 wiki_search 列独立 bm25 打分（低权重 w_wiki，兜底 cn 未命中的 tag；
  实测 bm25 列权重参数无法隔离 wiki 打分，故用两路查询合并而非单查询+列权重：
  路1 cn_search+bm25，路2 wiki_search+bm25，按 name 去重后 wiki-only 行追加在路1 尾部）。
  wiki 不注入 LLM（TagCandidate.wiki 仍恒空），仅 emb 文本 + FTS5 wiki_search 两路参与召回打分。

入口：
  - build_index(csv_path, on_progress)  # 进度回调供 UI
  - recall_candidates(text, top_n, allow_nsfw) -> list[TagCandidate]  # emb+fts(cn/wiki)多路融合
  - process_to_tags(text, candidates_or_selected, preset, session_api) -> str
  - process_image_description(text, ...) -> (positive, negative)   # 编排器入口
"""
from __future__ import annotations
from src.utils.debug import debug_log
import csv
import math
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from src.config import paths
from src.models import (
    ApiConfig, Preset, MemoryPreset, DanbooruPreset, parse_tag_output,
)
from src.services.embedding_client import EmbeddingClient
from src.services.llm_client import LlmClient
from src.services.storage import Storage
from src.utils.helpers import contains_chinese

COLLECTION_NAME = "danbooru_tags"
# 建库批量 embedding 每批上限。OpenAI 兼容 /embeddings 端点普遍支持单请求上千条 input，
# 用 500 一批可把建库显著提速；embed_batch 本身无硬上限。
EMBED_BATCH_SIZE = 500   # EmbeddingClient.embed_batch 每批上限
# 大批量 embedding 单请求耗时较高（1000 条向量约数十秒~两分钟），建库用更长超时，
# 避免撞 EmbeddingClient 默认 60s 超时后整批降级逐条（1000 条逐条更慢）。
INDEX_EMBED_TIMEOUT = 300.0

# cn_name 多别名拆分：把所有分隔符（标点/括号/斜杠/间隔号/连字符/空白）当分隔符拆碎，
# 符号（如 （））正常查询语句里不会出现，拆碎后词典/向量/FTS5 都用纯净词。
# 分隔符集合（全角字符直接用字面量，勿转义）：
#   - 逗号顿号：，,、
#   - 斜杠：/／
#   - 半角括号：()
#   - 全角括号：（）〔〕【】《》「」『』〈〉
#   - 间隔号：·•・
#   - 连字符：-－-–
#   - 感叹/问号：！？
#   - 空白：空格制表符等
# [!] 粘连组预处理（_merge_glued_groups）：间隔号/连字符连接的译名片段直接拆开会产生无意义
#     CJK 单字（如「丁·贾林」的丁、「境·界」的境/界、「李-恩菲尔德」的李、「欧比-旺」的旺），
#     故拆分前对粘连组先做合并/剔除，只针对「CJK片段 间隔号/连字符 CJK片段」组，
#     不影响括号/斜杠等拆出的单字（如角色本名「丛」「一」保留）。
# 例：「莫德雷德（Fate/Apocrypha）」-> 莫德雷德 / Fate / Apocrypha
#     「阿尔托莉雅·潘德拉贡」-> 阿尔托莉雅 / 潘德拉贡（全多字词，各自保留）
ALIAS_SPLIT = re.compile(
    r"[，,、/／\(\)（）〔〕【】《》「」『』〈〉·•・・・！？\s\-－–]+"
)
# 过滤纯 ASCII 短碎片：拆分会产生 !/&/.../+/0 等符号碎片，进 jieba 词典会污染分词。
# 保留规则：含 CJK 的 token 全留；纯 ASCII 需长度≥2 且含字母
# （Fate/Extra/DA/EX 保留，!/&,0 丢弃）。
_ASCII_KEEP = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9]{2,}")

# 粘连分隔符：间隔号（中文间隔号 U+00B7 / 项目符号 U+2022 / 日文间隔号 U+30FB）
# + 连字符（- － –）。这些字符在 cn_name 里连接译名片段（如「丁·贾林」「境·界」
# 「李-恩菲尔德」「欧比-旺」），若直接当分隔符拆开会产生无意义 CJK 单字（丁/境/界/李/旺），
# 故拆分前先做合并/剔除预处理。
# 注意：- 放字符串末尾，进正则字符类 [...] 时是字面连字符（非范围符），无需转义。
_GLUE = "\u00b7\u2022\u30fb－–-"
# 粘连组：CJK 片段(粘连分隔符 CJK 片段){1,}（至少两段 CJK 用粘连符连接才算一组）。
# 只匹配纯 CJK 片段间的粘连符，不匹配 CJK 与 ASCII 间的（如「舞-HiME」不在此处理，
# 因 HiME 是 ASCII；而「舞-乙姬」会处理）。
_GLUE_GROUP = re.compile(
    rf"[\u4e00-\u9fff]+(?:[{re.escape(_GLUE)}][\u4e00-\u9fff]+)+"
)


def _merge_glued_groups(s: str) -> str:
    """预处理 cn_name 中的粘连组（间隔号/连字符连接的 CJK 译名片段，在 ALIAS_SPLIT 拆分前调用）。

    间隔号/连字符连接的 CJK 译名片段直接拆开会产生无意义单字（如「丁·贾林」的丁、「境·界」
    的境/界、「李-恩菲尔德」的李、「欧比-旺」的旺），故按组内片段长度做不同处理：
      - 全单字（如「境·界」「黑·白·红」）：合并成一词（境界 / 黑白红）。
      - 单字 + 多字混合（如「丁·贾林」「李-恩菲尔德」「嘉妮特·蒂·亚历山德罗斯」）：
        剔除单字段，保留多字段（贾林 / 恩菲尔德 / 嘉妮特·亚历山德罗斯）。
      - 全多字（如「阿尔托莉雅·潘德拉贡」）：保留各段，粘连符换成空格让其各自成 token。
    处理结果里的空格会被后续 ALIAS_SPLIT 当分隔符正常拆分。只处理「CJK 片段 粘连符
    CJK 片段」的连续组，不影响括号/斜杠等拆出的单字（如角色本名「丛」「一」保留），
    也不处理 CJK 与 ASCII 间的粘连符（如「舞-HiME」的连字符不在此处理）。
    """
    def _repl(m: re.Match) -> str:
        frags = [f for f in re.split(
            rf"[{re.escape(_GLUE)}]+", m.group(0)) if f]
        if len(frags) < 2:
            return m.group(0)
        singles = [f for f in frags if len(f) == 1]
        multis = [f for f in frags if len(f) >= 2]
        if not singles:
            # 全多字：各段独立成 token（粘连符换空格）
            return " ".join(frags)
        if not multis:
            # 全单字：合并成一词
            return "".join(frags)
        # 单字+多字混合：剔除单字，多字间用空格各自成 token
        return " ".join(multis)
    return _GLUE_GROUP.sub(_repl, s)


def split_cn_aliases(cn: str) -> list[str]:
    """把 cn_name 拆成纯净 alias 列表（粘连组预处理 + 分隔符全拆 + 过滤 ASCII 短碎片 + 保序去重）。

    用于建库（ChromaDB embedding 文本 + alias.dict 词典生成），保证三处数据一致：
    - ChromaDB：每个纯净 alias 一向量（去重后无重复向量，省 embedding 调用+空间）
    - alias.dict：纯净词进 jieba 词典
    - FTS5：由 _fts_cn_tokens 单独切词（不经此函数）
    返回空列表时调用方应回退到英文名 name。

    粘连组预处理（_merge_glued_groups）在主拆分前先处理「CJK片段 粘连符 CJK片段」组，
    避免译名里的间隔号/连字符拆出无意义单字（如「丁·贾林」-> 贾林 而非 丁/贾林）。

    保序去重：cn_name 里同一 alias 可能因分隔符重复出现（如
    「阿狸（英雄联盟）,英雄联盟」拆出两个「英雄联盟」），每个 alias 只入库一次，
    避免同 embedding 文本的同 name 重复向量（ChromaDB query 本就按 name 取 max sim，
    重复向量不提升召回质量只浪费 embedding 调用与存储）。
    """
    if not cn:
        return []
    # 先预处理粘连组（间隔号/连字符，合并/剔单字），再按分隔符拆碎。
    cn = _merge_glued_groups(cn)
    parts = [p.strip() for p in ALIAS_SPLIT.split(cn) if p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        # 含 CJK 的 token 全部保留（中文别名是召回主力）
        keep = any("\u4e00" <= ch <= "\u9fff" for ch in p) or bool(_ASCII_KEEP.fullmatch(p))
        if not keep or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


# post_count 归一化上限（用于常见度权重）。Danbooru 最大 tag 1girl 约 8e6，取 1e7 留余量。
_MAX_PC = 10_000_000
_MAX_PC_LOG = math.log10(_MAX_PC + 10)   # 模块级常量，避免每次召回重算

# 默认 nsfw 词典：覆盖 jieba 默认词典未收录或切错的常见 nsfw / 性相关词汇。
# 格式：每行 `词 频率 词性`（jieba userdict 格式，频率越大优先级越高；词性可省略）。
# 首次启动 DanbooruService 时写入 data/danbooru_dict/nsfw.dict 并 load_userdict。
# 用户可手动编辑此文件增删词，下次启动生效（不覆盖已存在文件）。
# 频率 99999 最高，覆盖 alias 词典（最高 99998），保证 nsfw 词优先切分。
# 词性 n=名词。覆盖四类：身体部位 / 性行为动作 / 性相关服装道具 / 裸露尺度描述。
# 解决 jieba 默认词典对这些词切分不准（切碎或切错边界）导致 FTS5 cn_search 召回命中差的问题。
_DEFAULT_NSFW_DICT = """\
"""


# [!] 主体数量/性别标签常驻候选池：这些 tag 表达画面里「有几个人、什么性别」是文生图必备的主体锚点，
# 但中文描述里通常不含「1个女孩/1个男孩/双人」等字面词，召回（emb+FTS5）命中不到 -> LLM 加工时
# 候选池里没有 -> 无法正确输出 1girl/1boy/couple 等主体标签（旧 bug：只能硬凑 male_focus 或漏选）。
# 故在 recall_candidates 里无条件从库里查出这组标签明细，构造 TagCandidate（src='subject'）注入候选池，
# 让 LLM 能根据中文描述判断该选哪个（如「苏婉清+小厮」= 1girl + 1boy + couple）。
# 全部为 cat=0(通用)、nsfw=0；库里缺失的项静默跳过（防御建库不全）。
SUBJECT_TAGS: tuple[str, ...] = (
    # 单人主体
    "1girl", "1boy",
    "solo", "solo_focus",
    # 多人按性别+数量
    "2girls", "2boys", "3girls", "3boys", "4girls", "4boys", "5girls", "5boys",
    "6+girls", "6+boys",
    # 多人泛指
    "multiple_girls", "multiple_boys",
    # 关系/焦点
    "couple", "male_focus",
)


@dataclass
class TagCandidate:
    """召回候选 tag。"""
    name: str             # Danbooru 原名（下划线连接，输出用）
    cn_name: str          # 中文名/别名列表
    wiki: str             # 词条说明
    post_count: int
    category: int         # 0=general 1=artist 3=copyright 4=character 5+=meta（2 已废弃）
    nsfw: int             # 0/1
    score: float          # 重排后综合得分（越高越优）
    src: str = ""         # 召回来源标注（命中路用 + 连接，如 "emb" / "fts" / "wiki" / "emb+fts+wiki"）


class DanbooruService:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._chroma_client = None
        self._jieba_dict_loaded = False   # nsfw + alias 词典是否已 load_userdict（一次性）

    def _jb_prefix(self) -> str:
        """读取破限前缀：开关关或 prefix 空返回空串。"""
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    # ============ jieba 词典（nsfw 内置 + alias 建库生成）============
    def _ensure_jieba_dict(self) -> None:
        """首次使用 jieba 前加载自定义词典（一次性，延迟到首次建库/召回）。

        加载顺序（影响优先级，后加载的词典词频会被合并，同词高频率覆盖低频率）：
        1. alias.dict（建库时从 CSV cn_name 生成，~16万词，频率=post_count）
           让 jieba 优先按完整中文 alias 切词，保证整词精确边界（FTS5 纯词级不拆字）。
        2. nsfw.dict（内置 ~70 词，频率 99999 最高，覆盖 alias 词典）
           保证 nsfw 专用词优先级最高，不被 alias 词典的普通词干扰。

        首次 import jieba + 加载 16万词词典有约 1-2s 开销，故延迟到真正用 FTS5 时才触发，
        不阻塞应用启动。
        """
        if self._jieba_dict_loaded:
            return
        try:
            import jieba
            dict_dir = paths.danbooru_dict_dir()

            # 1. 加载 alias 词典（建库时生成，~16万词）
            alias_path = os.path.join(dict_dir, "alias.dict")
            alias_loaded = 0
            if os.path.exists(alias_path):
                jieba.load_userdict(alias_path)
                alias_loaded = os.path.getsize(alias_path)
                debug_log(lambda: f"[Danbooru.jieba] 已加载 alias 词典: {alias_path}（{alias_loaded} 字节）")
            else:
                debug_log(lambda: f"[Danbooru.jieba] alias 词典不存在（未建库或建库失败），跳过；将仅用 nsfw 词典 + jieba 默认")

            # 2. 加载 nsfw 词典（内置默认，首次自动生成；后加载以覆盖更高优先级）
            nsfw_path = os.path.join(dict_dir, "nsfw.dict")
            if not os.path.exists(nsfw_path):
                with open(nsfw_path, "w", encoding="utf-8") as f:
                    f.write(_DEFAULT_NSFW_DICT)
            jieba.load_userdict(nsfw_path)
            debug_log(lambda: f"[Danbooru.jieba] 已加载 nsfw 词典: {nsfw_path}")
            # [!] 标志位在 try 成功走完后才置 True：若中途异常（import 失败 / alias.dict
            # 加载出错 / 用户强制关程序中断）则保持 False，下次调用会重试，保证 nsfw.dict
            # 迟早能生成。曾放在 try 之前导致一次中断后永不重试、nsfw.dict 永不生成。
            self._jieba_dict_loaded = True
        except Exception as e:
            debug_log(lambda: f"[Danbooru.jieba] 加载词典失败（FTS5 退回默认分词）: {e}")

    def _write_alias_dict(self, alias_freq: dict[str, int]) -> None:
        """把建库收集的 alias 频率表写成 jieba userdict 格式文件。

        格式：每行 `alias post_count`（jieba userdict 词频越高优先级越高）。
        用 post_count 当频率：高频 tag 的 alias 优先切分（1girl 的"女孩"优先于冷门角色的）。
        只收含 CJK 的 alias（纯英文/数字 token jieba 默认能切，且短英文当词会干扰分词）。
        每次建库覆盖式重写（与库内容同步）。
        """
        if not alias_freq:
            debug_log("[Danbooru.alias_dict] 无 alias 数据，跳过词典生成")
            return
        try:
            dict_dir = paths.danbooru_dict_dir()
            dict_path = os.path.join(dict_dir, "alias.dict")
            # 按 post_count 降序写（便于人工查看 top 词）
            sorted_items = sorted(alias_freq.items(), key=lambda x: -x[1])
            with open(dict_path, "w", encoding="utf-8") as f:
                for alias, pc in sorted_items:
                    # post_count 可能很大（百万级），jieba 词频建议在合理范围，取 min(pc, 99998)
                    # 上限留 99999/99998 给 nsfw 词典覆盖
                    freq = min(int(pc), 99998) if pc > 0 else 1
                    f.write(f"{alias} {freq}\n")
            debug_log(lambda: f"[Danbooru.alias_dict] 已生成 alias 词典: {len(alias_freq)} 词 -> {dict_path}")
            # 打印 top10 便于验证
            top10 = sorted_items[:10]
            debug_log(lambda: f"[Danbooru.alias_dict] top10 高频 alias: {[(a, pc) for a, pc in top10]}")
        except Exception as e:
            debug_log(lambda: f"[Danbooru.alias_dict] 生成词典失败: {e}")

    def _write_char_whitelist(self, single_chars: set[str]) -> None:
        """把建库收集的「单字 alias」写成单字白名单文件（供查询端单字过滤）。

        背景：查询端 jieba 切出的「长度为 1 的 CJK token」不在白名单则丢弃（虚词「的/中」
        等单字进 OR MATCH 会把含这些字的 tag 全召回、污染 bm25）。但有些单字是有意义的 tag
        或别名（如「阿」「鬼」），需要保留。白名单从建库时 cn_name 拆出的单字 alias 收集
        （split_cn_aliases 已过滤纯 ASCII 短碎片，只留含 CJK 的 token；这里再筛长度=1），
        保证「库里存在的单字 alias」查询时不会被误删。

        每次建库覆盖式重写（与库内容同步）。格式：每行一个 CJK 单字。
        """
        if not single_chars:
            debug_log("[Danbooru.char_whitelist] 无单字 alias 数据，跳过白名单生成")
            return
        try:
            dict_dir = paths.danbooru_dict_dir()
            dict_path = os.path.join(dict_dir, "char_whitelist.dict")
            with open(dict_path, "w", encoding="utf-8") as f:
                for ch in sorted(single_chars):
                    f.write(ch + "\n")
            debug_log(lambda: f"[Danbooru.char_whitelist] 已生成单字白名单: {len(single_chars)} 字 -> {dict_path}")
        except Exception as e:
            debug_log(lambda: f"[Danbooru.char_whitelist] 生成白名单失败: {e}")

    # ============ ChromaDB ============
    def _get_chroma(self):
        if self._chroma_client is None:
            import chromadb
            self._chroma_client = chromadb.PersistentClient(path=paths.danbooru_db_dir())
        return self._chroma_client

    def _get_collection(self):
        client = self._get_chroma()
        return client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def _cleanup_chroma_persist(self) -> None:
        """重建库前清理 ChromaDB 持久化残留，避免每次重建堆积 UUID 子目录 + sqlite 膨胀。

        背景：ChromaDB PersistentClient 的 delete_collection() 只删 chroma.sqlite3 里的
        collection 逻辑记录，不删底层 segment 持久化目录（每个 collection 一个 UUID 子目录，
        存向量数据）。多次重建后 data/danbooru_db/ 下会堆积一堆 UUID 目录，且 chroma.sqlite3
        因 delete 标记删除后不 VACUUM 只增不减（实测几次重建涨到 126MB+）。chromadb>=0.5 的
        PersistentClient 没有 reset() 方法（仅 EphemeralClient 有，已弃用），只能手动清文件。

        做法（delete_collection 之后、新建 collection 之前调用）：
        1. 丢弃旧 _chroma_client 引用（释放对 sqlite 的句柄，否则删文件/锁库）；
        2. 扫描 danbooru_db_dir() 下符合 UUID 格式的子目录全部删掉（segment 残留），
           保留 chroma.sqlite3 等非目录文件；
        3. 对 chroma.sqlite3 做 VACUUM 压缩空间（标记删除的页回收到文件系统）。
        清理后 _get_chroma() 会懒重建 client，_get_collection() 建新 collection。

        幂等：目录不存在或无 UUID 子目录时无副作用；VACUUM 失败不影响建库（只留空间未回收）。
        """
        # 1. 丢弃旧 client 引用，释放 sqlite 句柄（避免文件锁/状态不一致）
        self._chroma_client = None

        db_dir = paths.danbooru_db_dir()
        if not os.path.isdir(db_dir):
            return
        # 2. 清理 UUID 格式的 segment 子目录残留（xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）
        uuid_pattern = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        removed_dirs = 0
        for name in os.listdir(db_dir):
            sub = os.path.join(db_dir, name)
            if os.path.isdir(sub) and uuid_pattern.match(name):
                try:
                    shutil.rmtree(sub)
                    removed_dirs += 1
                except Exception as e:
                    debug_log(lambda: f"[Danbooru.cleanup] 删除残留目录 {name} 失败: {e}")
                    # [!] 重试一次：Windows 下 sqlite 句柄释放有延迟，GC 后可能仍短暂占用。
                    # sleep 0.5s 等句柄释放后再删，避免重建时残留目录堆积。
                    try:
                        import time as _t
                        _t.sleep(0.5)
                        shutil.rmtree(sub)
                        removed_dirs += 1
                        debug_log(lambda: f"[Danbooru.cleanup] 重试删除 {name} 成功")
                    except Exception as e2:
                        debug_log(lambda: f"[Danbooru.cleanup] 重试删除 {name} 仍失败: {e2}")
        if removed_dirs:
            debug_log(lambda: f"[Danbooru.cleanup] 已清理 {removed_dirs} 个 ChromaDB segment 残留目录")

        # 3. VACUUM 压缩 chroma.sqlite3（delete_collection 标记删除的页回收）
        sqlite_path = os.path.join(db_dir, "chroma.sqlite3")
        if os.path.exists(sqlite_path):
            try:
                before = os.path.getsize(sqlite_path)
                # 独立短连接做 VACUUM（事务型，autocommit），不复用 Storage 的持久连接
                conn = sqlite3.connect(sqlite_path)
                conn.execute("VACUUM")
                conn.close()
                after = os.path.getsize(sqlite_path)
                if after < before:
                    saved = (before - after) / (1024 * 1024)
                    debug_log(lambda: f"[Danbooru.cleanup] chroma.sqlite3 VACUUM: "
                          f"{before/1024/1024:.1f}MB -> {after/1024/1024:.1f}MB（回收 {saved:.1f}MB）")
            except Exception as e:
                debug_log(lambda: f"[Danbooru.cleanup] chroma.sqlite3 VACUUM 失败（不影响建库）: {e}")

    def db_count(self) -> int:
        """当前库内 tag 条数（一 tag 一行，FTS5 表计数；异常/无库返回 0）。

        注意：ChromaDB 内是 alias 向量（约 tag 数 ×4），不是 tag 真实条数，
        故优先用 FTS5 行数；FTS5 不可用时回退 ChromaDB.count（仍 ≥0，判空逻辑兼容）。
        """
        n = self.storage.count_danbooru_fts()
        if n > 0:
            return n
        try:
            return self._get_collection().count()
        except Exception:
            return 0

    # ============ embedding 配置（复用 MemoryPreset）============
    def _resolve_embedding_api(
        self, session_api: Optional[ApiConfig]
    ) -> Optional[ApiConfig]:
        """embedding 用 MemoryPreset 绑定的 API；未绑或不可用回退 session_api。

        与记忆模块同口径：mem_preset.api_id 非空 → load_api → 校验 enabled → 用之。
        """
        try:
            mem = self.storage.load_memory_preset()
        except Exception:
            mem = None
        if mem and mem.api_id:
            api = self.storage.load_api(mem.api_id)
            if api and api.enabled:
                # 还需校验有 embedding_model
                if (api.embedding_model or "").strip():
                    return api
        # 回退会话 API（需自身有 embedding_model）
        if session_api and (session_api.embedding_model or "").strip():
            return session_api
        return None

    # ============ 建库 ============
    # CSV 候选编码：utf-8-sig 兼容带 BOM 的 UTF-8；gbk/gb18030 兼容 Windows 中文导出
    _CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")

    def _read_csv(self, csv_path: str) -> list[dict]:
        """读 CSV 为 dict 列表。期望列：name,cn_name,wiki,post_count,category,nsfw。

        依次尝试多种编码以兼容 UTF-8(含 BOM) 与 Windows 导出的 GBK/GB18030。
        """
        last_err: Optional[Exception] = None
        for enc in self._CSV_ENCODINGS:
            try:
                with open(csv_path, "r", encoding=enc, newline="") as f:
                    reader = csv.DictReader(f)
                    rows = [r for r in reader]
                    if rows:
                        return rows
                    # 空结果可能只是该编码误读后 BOM 干扰列名，换下一种再试
                    # 但若文件本身只有表头/空行，结果都为空，保留最后一次结果
                    last_rows = rows
            except UnicodeDecodeError as e:
                last_err = e
                continue
        # 走到这里说明所有编码都不理想：返回最后读到的（可能空）或抛最后一次解码错误
        if "last_rows" in locals():
            return last_rows
        if last_err is not None:
            raise last_err
        return []

    def build_index(
        self,
        csv_path: str,
        session_api: Optional[ApiConfig] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> tuple[bool, str, int]:
        """从 CSV 重建标签库。返回 (success, message, imported_tag_count)。

        覆盖式重建：清旧 ChromaDB collection + 清旧 FTS5 表，再建新。
        - ChromaDB：每 tag 一向量打标（embedding 文本 = [cn_name] cn_name [Wiki] wiki），
          metadata 存全量明细（name/cn_name_raw/post_count/category/nsfw），召回后直接取 cn_name_raw。
        - SQLite FTS5：一 tag 一行（5万），cn_name 入 cn_search 走 MATCH/bm25 召回（主召回），
          wiki 入 wiki_search 走 MATCH + bm25 召回（低权重 wiki_sim 打分）+ wiki 列旁存原始串供回查。
        embedding 用 MemoryPreset 绑定 API（回退 session_api），没有可用 API 直接报错。
        """
        self._ensure_jieba_dict()
        emb_api = self._resolve_embedding_api(session_api)
        if emb_api is None:
            debug_log("[Danbooru.build_index] 无可用 embedding API，建库中止")
            return (False,
                    "未找到可用的 embedding API。请在「API 与预设 -> 记忆整理」标签页"
                    "绑定一个配置了 embedding 模型的 API，或给当前会话 API 配 embedding 模型。",
                    0)
        debug_log(lambda: f"[Danbooru.build_index] 开始建库: csv={csv_path!r}")
        debug_log(lambda: f"[Danbooru.build_index] embedding API={emb_api.name!r} model={emb_api.embedding_model!r}")
        try:
            rows = self._read_csv(csv_path)
        except FileNotFoundError:
            return False, f"CSV 文件不存在：{csv_path}", 0
        except Exception as e:
            return False, f"读取 CSV 失败：{e}", 0
        if not rows:
            return False, "CSV 文件为空或无数据行", 0
        debug_log(lambda: f"[Danbooru.build_index] CSV 读取成功: {len(rows)} 行")

        total = len(rows)
        if on_progress:
            on_progress(0, total)

        # 解析每行：
        #   records: [(emb_text, meta), ...]  每 tag 一条 ChromaDB 行（打标方案：
        #            emb_text = [cn_name] cn_name [Wiki] wiki，metadata 存全量明细含 cn_name_raw）
        #   fts_rows: [{name, cn_name, wiki, post_count, category, nsfw}, ...]  一 tag 一行
        #   alias_freq: {alias: max_post_count}  用于生成 jieba 词典，让 jieba 优先按完整 alias 切词
        records: list[tuple[str, dict]] = []
        fts_rows: list[dict] = []
        alias_freq: dict[str, int] = {}   # alias -> max post_count（同 alias 多 tag 取 max）
        single_chars: set[str] = set()    # 单字 CJK alias 集合（生成查询单字白名单）
        total_tags = 0
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            cn = (r.get("cn_name") or "").strip()
            wiki = (r.get("wiki") or "").strip()   # 入 wiki_search 索引 + 打标进 [Wiki] 段
            try:
                pc = int(r.get("post_count") or 0)
            except (ValueError, TypeError):
                pc = 0
            try:
                cat = int(r.get("category") or 0)
            except (ValueError, TypeError):
                cat = 0
            try:
                nsfw = int(r.get("nsfw") or 0)
            except (ValueError, TypeError):
                nsfw = 0

            # 打标方案：每 tag 一向量，embedding 文本 = [cn_name] cn_name [Wiki] wiki。
            # [cn_name] 段是同义词集合（cn_name 整串，逗号分隔），[Wiki] 段是 wiki 语义描述。
            # bge-m3 长文本能力充分利用，wiki 语义锚定让 emb 路召回更准（旧拆 alias 方案 wiki 完全没进 emb）。
            # 标签名贴合 CSV 列原义；wiki 为空时 [Wiki] 段留空（库内 wiki 100% 非空，仅 6 条空兜底）。
            emb_text = f"[cn_name] {cn} [Wiki] {wiki}"
            # ChromaDB metadata 存全量明细：召回后直接从 metadata 取 cn_name_raw，不再反查 FTS5。
            meta = {
                "name": name,
                "cn_name_raw": cn,   # 完整 cn_name 串（逗号分隔），召回端直接取
                "post_count": pc,
                "category": cat,
                "nsfw": nsfw,
            }
            records.append((emb_text, meta))
            total_tags += 1

            # split_cn_aliases 仍调用一次：只为生成 jieba alias 词典 + 单字白名单，
            # 不再用于 ChromaDB 入库（打标后每 tag 一向量，不拆 alias）。
            # 词典让 jieba 优先按完整 alias 切词，保证 FTS5 纯词级精确边界。
            for alias in split_cn_aliases(cn):
                if alias and any("\u4e00" <= ch <= "\u9fff" for ch in alias):
                    cur = alias_freq.get(alias, 0)
                    if pc > cur:
                        alias_freq[alias] = pc
                    # 单字白名单收集：cn_name 里本来就存在的单字 alias（如「阿」「鬼」）。
                    # 查询端 jieba 切出的单字 CJK token 不在白名单则丢弃，
                    # 故建库时把 cn_name 里本来就存在的单字 alias 收进白名单，避免有意义单字被误删。
                    if len(alias) == 1:
                        single_chars.add(alias)

            # FTS5 一 tag 一行：cn_name 整串入 cn_search 索引（bm25 召回），
            # wiki 整串入 wiki_search 索引（MATCH+bm25 召回，低权重 wiki_sim 打分）+ wiki 列旁存原始串。
            # cn_search/wiki_search 派生在 bulk_insert_danbooru_fts 内用 _fts_cn_tokens 切词。
            fts_rows.append({
                "name": name, "cn_name": cn, "wiki": wiki,
                "post_count": pc, "category": cat, "nsfw": nsfw,
            })

        if not records:
            debug_log("[Danbooru.build_index] CSV 中无有效 tag 行（缺 name 列）")
            return False, "CSV 中没有有效 tag 行（缺 name 列）", 0
        debug_log(lambda: f"[Danbooru.build_index] 解析完成: {len(fts_rows)} 个 tag -> {total_tags} 个向量（每 tag 一向量，打标方案）")

        # 覆盖式重建：ChromaDB + FTS5 同步清空
        client = self._get_chroma()
        try:
            client.delete_collection(COLLECTION_NAME)
            debug_log(lambda: f"[Danbooru.build_index] 已删除旧 ChromaDB collection: {COLLECTION_NAME}")
        except Exception:
            pass
        # [!] 显式释放 client 局部变量 + GC，让 chromadb PersistentClient 内部 sqlite 句柄释放。
        # 否则 Windows 下 _cleanup_chroma_persist 删 UUID 目录 / VACUUM 会因文件被占用失败
        # （旧实现只丢 self._chroma_client 引用，局部变量 client 仍持有句柄，sqlite 连接不释放）。
        del client
        import gc
        gc.collect()
        # delete_collection 只删 sqlite 逻辑记录，不删底层 segment 目录也不 VACUUM，
        # 多次重建会堆积 UUID 子目录 + sqlite 膨胀。这里彻底清理持久化残留。
        self._cleanup_chroma_persist()
        collection = self._get_collection()
        try:
            self.storage.clear_danbooru_fts()
            debug_log("[Danbooru.build_index] 已清空旧 FTS5 表")
        except Exception as e:
            debug_log(lambda: f"[Danbooru.build_index] 清空 FTS5 失败: {e}")

        # 生成 jieba alias 词典（alias -> post_count 当频率），让 jieba 优先按完整 alias 切词，
        # 保证整词精确边界（FTS5 纯词级不拆字，OOV 整词保留）。每次建库都重新生成（覆盖式）。
        self._write_alias_dict(alias_freq)
        # ⚠️ 关键：写完新 alias.dict 后必须立即重新加载，否则 jieba 内存里还是旧词典，
        # 紧接着的 bulk_insert_danbooru_fts 会用旧词典分词入库，而下次召回用新词典分词查询，
        # 两端 token 不对齐导致 MATCH 命中 0 行（如入库切"英雄 联盟"、查询切"英雄联盟"整词）。
        # _ensure_jieba_dict 是一次性的（_jieba_dict_loaded 标志），重置标志后立即重载。
        self._jieba_dict_loaded = False
        self._ensure_jieba_dict()
        debug_log("[Danbooru.build_index] 新 alias.dict 已生成并重新加载到 jieba 内存，确保入库/查询分词一致")

        # 生成单字白名单（cn_name 里本来就存在的单字 CJK alias）。注意：白名单收集的是
        # split_cn_aliases 拆出的长度=1 的 alias（cn_name 原始内容），与 jieba 切词无关，
        # 故不受 alias.dict 重载影响，重载前后结果一致，这里直接写。
        self._write_char_whitelist(single_chars)

        # 先批量插入 FTS5（毫秒级，不占进度条）--此时 jieba 内存已是新词典，入库分词与后续查询对齐
        try:
            self.storage.bulk_insert_danbooru_fts(fts_rows)
        except Exception as e:
            debug_log(lambda: f"[Danbooru.build_index] FTS5 批量插入失败: {e}")

        # ChromaDB 批量 embedding 入库（耗时大头），进度条按 tag 处理数推进
        emb_client = EmbeddingClient(emb_api, timeout=INDEX_EMBED_TIMEOUT)
        embedded = 0   # 成功入向量库的 tag 数
        degraded_batches = 0   # 降级逐条的批次数
        n = len(records)
        debug_log(lambda: f"[Danbooru.build_index] 开始 ChromaDB 批量 embedding: 共 {n} 个 tag，每批 {EMBED_BATCH_SIZE}")
        for start in range(0, n, EMBED_BATCH_SIZE):
            batch = records[start:start + EMBED_BATCH_SIZE]
            texts = [t for t, _ in batch]
            embs = emb_client.embed_batch(texts)
            if not embs or len(embs) != len(batch):
                # 批量失败：降级逐条
                degraded_batches += 1
                if degraded_batches <= 3:   # 只打前 3 次降级，避免刷屏
                    debug_log(lambda: f"[Danbooru.build_index] 批次 {start}-{start+len(batch)} 批量 embedding 失败，降级逐条")
                valid: list[tuple[str, dict, list[float]]] = []
                for t, m in batch:
                    e = emb_client.embed(t)
                    if e is not None:
                        valid.append((t, m, e))
            else:
                valid = [(batch[i][0], batch[i][1], embs[i]) for i in range(len(batch))]
            if not valid:
                if on_progress:
                    on_progress(start + len(batch), n)
                continue
            import time
            ts = time.time()
            # id 唯一性：用 start（tag 全局序号在 records 中的偏移）+ 批内序号 + 时间戳
            collection.add(
                ids=[f"tag_{start + i}_{ts}" for i in range(len(valid))],
                embeddings=[e for _, _, e in valid],
                documents=[t for t, _, _ in valid],
                metadatas=[m for _, m, _ in valid],
            )
            embedded += len(valid)
            if on_progress:
                on_progress(start + len(batch), n)

        if degraded_batches > 0:
            debug_log(lambda: f"[Danbooru.build_index] 共 {degraded_batches} 个批次降级逐条 embedding")
        tag_count = len(fts_rows)
        msg = f"成功导入 {tag_count} 个 tag（{embedded} 个向量）"
        debug_log(lambda: f"[Danbooru.build_index] 建库完成: {msg}")
        return True, msg, tag_count

    # ============ 检索（多路融合：embedding + FTS5 cn_search/wiki_search BM25）============
    def recall_candidates(
        self,
        text: str,
        top_n: int,
        allow_nsfw: bool,
        session_api: Optional[ApiConfig] = None,
        weights: Optional[tuple[float, float, float, float]] = None,
        enable_wiki: bool = True,
        allow_categories: Optional[Iterable[int]] = None,
    ) -> list[TagCandidate]:
        """多路召回后融合排序，返回 top_n 个 TagCandidate。

        路径 A - embedding 语义召回：每 tag 一向量打标入 ChromaDB（[cn_name] xxx [Wiki] yyy），
                按 name 去重取最高 sim（打标后每 tag 一向量，去重为防御性）。
                emb 路命中的 tag 直接从 metadata 取 cn_name_raw，不再反查 FTS5。
        路径 B - FTS5 字面召回（两路合并，各路独立 bm25 归一化）：
                路1 cn_search MATCH + bm25 -> fts_sim（主召回，高置信度）；
                路2 wiki_search MATCH + bm25 -> wiki_sim（语义兜底，低置信度）；
                cn 已命中的 tag 按 name 去重（cn 优先），wiki_sim 仅给 wiki-only 命中的 tag。
        融合：`score = w_emb·emb_sim + w_fts·fts_sim + w_wiki·wiki_sim + w_pc·pc_norm`
        （各路任一缺失该权重项为 0；两路任一失败互为兜底）。
        weights=(w_emb, w_fts, w_wiki, w_pc)，默认 None 用经验值 (0.5, 0.20, 0.10, 0.20)；
        不强制归一化（用户可能想压低总分做对比）。由调用方从 DanbooruPreset 传入。
        [!] fts 权重偏低原因：本数据结构下 79.6% 的词 df=1 -> IDF 二值化，bm25 退化成弱信号
        （区分度低），故 fts 从 0.35 降到 0.20、pc 从 0.15 升到 0.20，让 emb 主力 + pc 常见度主导排名。
        enable_wiki=False 时跳过 wiki_search 路（仅 cn_search），退回纯 cn 逻辑（无 wiki 召回/打分）。
        wiki 经 [Wiki] 标签进 emb 文本参与 emb 路召回（打标方案），不注入 LLM；
        FTS5 wiki_search 路独立 bm25 打分（低权重，与 emb 路的 wiki 语义双重但互补）。
        allow_categories：召回后按 category 硬过滤，未列入的 tag 直接丢弃（None 或空 = 不过滤，
        向后兼容老调用方/老 preset）。默认全开 = (0,1,3,4,5)，由 DanbooruPreset 透传，
        让用户在设置对话框勾选全局生效（聊天出图/头像生成/测试区同源）。
        [!] 主体标签常驻注入：SUBJECT_TAGS（1girl/1boy/2girls/couple/solo 等）在 top_n 截断后
        无条件从库里查明细追加进候选池（src='subject'，不占 top_n 名额），解决中文描述不含数量词
        致召回命中不到主体标签、LLM 无法输出 1girl/1boy 的问题。已召回命中的去重跳过。
        """
        self._ensure_jieba_dict()
        if weights is None:
            weights = (0.5, 0.20, 0.10, 0.20)
        w_emb, w_fts, w_wiki, w_pc = weights
        debug_log(lambda: f"[Danbooru.recall] 输入: text={text!r}, top_n={top_n}, allow_nsfw={allow_nsfw}, weights=(emb={w_emb}, fts={w_fts}, wiki={w_wiki}, pc={w_pc})")
        # ===== 路径 A：embedding 召回（按 name 去重取最高 sim）=====
        emb_names: dict[str, float] = {}      # name -> max sim ∈ [0,1]
        emb_meta: dict[str, dict] = {}        # name -> metadata（含 cn 别名 alias）
        try:
            collection = self._get_collection()
            count = collection.count()
        except Exception:
            count = 0
        debug_log(lambda: f"[Danbooru.recall] 路径A embedding: ChromaDB 向量数={count}")
        if count > 0:
            emb_api = self._resolve_embedding_api(session_api)
            if emb_api is None:
                debug_log("[Danbooru.recall] 路径A: 无可用 embedding API，跳过（仅走 FTS5）")
            else:
                debug_log(lambda: f"[Danbooru.recall] 路径A: 用 embedding API={emb_api.name!r} model={emb_api.embedding_model!r}")
                try:
                    emb = EmbeddingClient(emb_api).embed(text)
                except Exception as e:
                    debug_log(lambda: f"[Danbooru.recall] 路径A embedding 调用失败: {e}")
                    emb = None
                if emb is None:
                    debug_log("[Danbooru.recall] 路径A: embedding 返回 None，跳过")
                else:
                    # [!] fetch_n 统一取 top_n*3，与 FTS5 路（776 行 query_danbooru_fts 也取 top_n*3）对称，
                    # 让 emb 路候选数不劣于 FTS5 路。nsfw 过滤由 756-758 行在应用层做（读后过滤），
                    # 不在 query 阶段缩减 fetch_n——否则 allow_nsfw=True 时 emb 路只取 top_n，
                    # 融合排序时 emb 路强相关但排在 top_n 之外的 tag 直接丢失，召回质量受损。
                    fetch_n = min(top_n * 3, count)
                    debug_log(lambda: f"[Danbooru.recall] 路径A: ChromaDB query n_results={fetch_n} (allow_nsfw={allow_nsfw}, 读后过滤)")
                    try:
                        results = collection.query(
                            query_embeddings=[emb],
                            n_results=fetch_n,
                            include=["metadatas", "distances"],   # 不需要 documents
                        )
                    except Exception as e:
                        debug_log(lambda: f"[Danbooru.recall] 路径A ChromaDB query 失败: {e}")
                        results = None
                    if results:
                        metas = results.get("metadatas", [[]])[0]
                        dists = results.get("distances", [[]])[0]
                        for i, m in enumerate(metas):
                            if not m:
                                continue
                            nsfw = int(m.get("nsfw", 0) or 0)
                            if nsfw and not allow_nsfw:
                                continue
                            dist = float(dists[i]) if i < len(dists) else 1.0
                            sim = max(0.0, 1.0 - dist)   # cosine distance -> sim
                            name = str(m.get("name", ""))
                            if not name:
                                continue
                            if name not in emb_names or sim > emb_names[name]:
                                emb_names[name] = sim
                                emb_meta[name] = dict(m)
                    debug_log(lambda: f"[Danbooru.recall] 路径A: 去重后 {len(emb_names)} 个候选")
                    for i, (n, s) in enumerate(sorted(emb_names.items(), key=lambda x: -x[1])[:5]):
                        debug_log(lambda: f"[Danbooru.recall]   路径A top[{i}] {n} sim={s:.4f}")

        # ===== 路径 B：FTS5 字面 + bm25 召回（cn / wiki 两路，各路独立归一化）=====
        fts_names: dict[str, float] = {}      # name -> 归一化 cn fts_sim ∈ [0,1]（cn_search 命中）
        wiki_names: dict[str, float] = {}     # name -> 归一化 wiki_sim ∈ [0,1]（仅 wiki_search 命中）
        fts_meta: dict[str, dict] = {}        # name -> {cn_name_raw, post_count, category, nsfw, fts_src}
        try:
            rows = self.storage.query_danbooru_fts(text, top_n * 3, allow_nsfw, enable_wiki=enable_wiki)
        except Exception as e:
            debug_log(lambda: f"[Danbooru.recall] 路径B FTS5 查询失败: {e}")
            rows = []
        debug_log(lambda: f"[Danbooru.recall] 路径B FTS5: 返回 {len(rows)} 行")
        if rows:
            # 按 fts_src 分组：cn 行与 wiki 行各自 min-max 归一化 bm25（绝对值，越负越相关）。
            cn_rows = [r for r in rows if r.get("fts_src") == "cn"]
            wiki_rows = [r for r in rows if r.get("fts_src") == "wiki"]
            max_cn = max((abs(r["s"]) for r in cn_rows), default=0.0) or 1.0
            max_wiki = max((abs(r["s"]) for r in wiki_rows), default=0.0) or 1.0
            for r in rows:
                name = r["name"]
                if not name:
                    continue
                if r.get("fts_src") == "wiki":
                    wiki_names[name] = abs(r["s"]) / max_wiki
                else:
                    fts_names[name] = abs(r["s"]) / max_cn
                fts_meta[name] = r
            for i, (n, s) in enumerate(sorted(fts_names.items(), key=lambda x: -x[1])[:5]):
                debug_log(lambda: f"[Danbooru.recall]   路径B-cn top[{i}] {n} fts_sim={s:.4f}")
            for i, (n, s) in enumerate(sorted(wiki_names.items(), key=lambda x: -x[1])[:5]):
                debug_log(lambda: f"[Danbooru.recall]   路径B-wiki top[{i}] {n} wiki_sim={s:.4f}")

        # ===== 融合：union of names，按 score 排序 =====
        all_names = set(emb_names) | set(fts_names) | set(wiki_names)
        only_emb = set(emb_names) - set(fts_names) - set(wiki_names)
        only_fts = set(fts_names) - set(emb_names) - set(wiki_names)
        only_wiki = set(wiki_names) - set(emb_names) - set(fts_names)
        debug_log(lambda: f"[Danbooru.recall] 融合: 总 {len(all_names)} = 仅emb {len(only_emb)} + 仅cn {len(only_fts)} + 仅wiki {len(only_wiki)} + 多路命中 {len(all_names) - len(only_emb) - len(only_fts) - len(only_wiki)}")
        if not all_names:
            debug_log("[Danbooru.recall] 两路均无召回，返回空")
            return []

        # ===== TagCandidate 构造 =====
        # [!] 打标方案后 ChromaDB metadata 已存 cn_name_raw 全量明细，emb 路命中的 tag
        # 直接从 emb_meta 取 cn_name_raw，不再需要反查 FTS5 表（旧拆 alias 方案 emb 路
        # 单命中 tag metadata 只有单条 alias，需 fetch_danbooru_cn_names 反查完整 cn_name_raw）。
        cand: list[TagCandidate] = []
        for name in all_names:
            s_emb = emb_names.get(name, 0.0)
            s_fts = fts_names.get(name, 0.0)
            s_wiki = wiki_names.get(name, 0.0)
            # post_count：优先从 emb_meta（刚反序列化）取，缺失则从 fts_meta 取
            m_e = emb_meta.get(name, {})
            m_f = fts_meta.get(name, {})
            try:
                pc = int(m_e.get("post_count", m_f.get("post_count", 0)) or 0)
            except (ValueError, TypeError):
                pc = 0
            try:
                cat = int(m_e.get("category", m_f.get("category", 0)) or 0)
            except (ValueError, TypeError):
                cat = 0
            try:
                nsfw = int(m_e.get("nsfw", m_f.get("nsfw", 0)) or 0)
            except (ValueError, TypeError):
                nsfw = 0
            # cn_name 取值优先级：emb 路命中从 metadata 的 cn_name_raw 取（打标后已存全量）>
            # FTS5 路命中的 cn_name_raw > 空串兜底。
            cn_name = str(
                m_e.get("cn_name_raw") or m_f.get("cn_name_raw") or ""
            )
            s_post = math.log10(pc + 10) / _MAX_PC_LOG
            score = w_emb * s_emb + w_fts * s_fts + w_wiki * s_wiki + w_pc * s_post
            # 召回来源标注：命中路用 + 连接（如 "emb" / "fts" / "wiki" / "emb+fts+wiki"）。
            # 展示用：测试区 + 手改弹窗每行末尾显示，让用户看清这条 tag 是哪路召回的。
            src_parts = []
            if name in emb_names:
                src_parts.append("emb")
            if name in fts_names:
                src_parts.append("fts")
            if name in wiki_names:
                src_parts.append("wiki")
            src = "+".join(src_parts) if src_parts else "none"
            cand.append(TagCandidate(
                name=name,
                cn_name=cn_name,
                wiki="",   # wiki 经 wiki_search 参与 FTS5 召回但不注入 LLM，故 TagCandidate.wiki 恒空
                post_count=pc,
                category=cat,
                nsfw=nsfw,
                score=score,
                src=src,
            ))
        cand.sort(key=lambda c: c.score, reverse=True)
        # [!] category 硬过滤：按 allow_categories 全局过滤候选（None/空=不过滤，向后兼容）。
        # 在排序后、截 top_n 前过滤，让被过滤的 tag 不占用 top_n 名额（避免过滤后候选变少）。
        # 由 DanbooruPreset.allow_categories 透传，让测试区 / 聊天出图 / 头像生成同源生效。
        if allow_categories:
            allow_set = {int(c) for c in allow_categories}
            before = len(cand)
            cand = [c for c in cand if int(c.category) in allow_set]
            debug_log(lambda: f"[Danbooru.recall] category 过滤: 允许 {sorted(allow_set)}，过滤 {before} -> {len(cand)} 个候选")
        debug_log(lambda: f"[Danbooru.recall] 融合排序后 top {min(top_n, len(cand))} 结果:")
        for i, c in enumerate(cand[:top_n]):
            debug_log(lambda: f"[Danbooru.recall]   [{i}] {c.name:20s} score={c.score:.4f} "
                  f"(emb={emb_names.get(c.name,0):.3f} fts={fts_names.get(c.name,0):.3f} wiki={wiki_names.get(c.name,0):.3f} pc={c.post_count}) 来源={c.src}")
        result = cand[:top_n]

        # [!] 主体标签常驻注入（不占 top_n 名额）：中文描述通常不含「1个女孩/双人」等字面词，
        # 召回（emb+FTS5）命中不到主体数量/性别标签 -> LLM 加工时候选池缺失 -> 无法正确输出
        # 1girl/1boy/couple 等。此处无条件从库里查 SUBJECT_TAGS 明细追加进候选池（src='subject'），
        # 让 LLM 能据描述判断选哪个。已召回命中的去重跳过；allow_categories 过滤同样生效。
        existing = {c.name for c in result}
        subject_rows = self.storage.fetch_danbooru_subject_tags(list(SUBJECT_TAGS))
        allow_set = {int(c) for c in allow_categories} if allow_categories else None
        added = []
        for r in subject_rows:
            if r["name"] in existing:
                continue
            if allow_set is not None and int(r["category"]) not in allow_set:
                continue
            result.append(TagCandidate(
                name=r["name"],
                cn_name=str(r["cn_name_raw"] or ""),
                wiki="",
                post_count=int(r["post_count"] or 0),
                category=int(r["category"] or 0),
                nsfw=int(r["nsfw"] or 0),
                score=0.0,   # 主体标签不参与融合排序，score=0 仅占位
                src="subject",
            ))
            added.append(r["name"])
        if added:
            debug_log(lambda: f"[Danbooru.recall] 主体标签注入 {len(added)} 个(不占top_n): {added}")
        return result

    # ============ LLM 加工 ============
    def _resolve_llm_api(
        self, preset: DanbooruPreset, session_api: Optional[ApiConfig]
    ) -> Optional[ApiConfig]:
        """tag 加工 LLM 用 DanbooruPreset.api_id；未绑或不可用回退 session_api；
        仍无可用 API 回退首个 enabled 的 API（头像生成等无 session 上下文场景兜底）。"""
        if preset.api_id:
            api = self.storage.load_api(preset.api_id)
            if api and api.enabled:
                return api
        if session_api and session_api.enabled:
            return session_api
        # 兜底：头像生成 / 续写等无明确 session_api 时，用任意一个启用的 API
        for api in self.storage.load_all_apis():
            if api.enabled:
                return api
        return None

    def process_to_tags(
        self,
        text: str,
        candidates: list[TagCandidate],
        user_selected: Optional[list[str]],
        preset: DanbooruPreset,
        session_api: Optional[ApiConfig] = None,
        character_appearances: Optional[list[tuple[str, str]]] = None,
        cancel_check=None,
    ) -> str:
        """LLM 加工：把召回候选（或用户勾选）通过 {{标签}} 注入提示词，输出英文 tag 串。

        - user_selected 非空（手改模式）：注入用户勾选的 name 列表
        - user_selected 为 None（自动模式）：注入全量召回候选
        - character_appearances：会话内角色的固定外貌 tag（角色名, tag 串）列表，
          注入 user prompt 作为「角色外貌参考」让 LLM 按中文描述选用，不参与召回。
          为空或全部 tag 为空则不注入（向后兼容）。失败/取消返回空串。
        - cancel_check：透传给 chat_cancelable，停止生成时可中断加工 LLM 调用（§6 契约）。
        """
        api = self._resolve_llm_api(preset, session_api)
        if api is None:
            debug_log("[Danbooru.process] 无可用 LLM API，加工中止")
            return ""
        mode = "手改(用户勾选)" if user_selected is not None else "自动(全量召回)"
        debug_log(lambda: f"[Danbooru.process] 开始加工: text={text!r} 模式={mode} LLM API={api.name!r} model={api.model!r}")

        # 构造 {{标签}} 内容
        if user_selected is not None:
            # 手改模式：用户已勾选的 name（外加可能新增的）
            tag_lines = [name for name in user_selected if name]
        else:
            tag_lines = [
                f"{c.name} | {c.cn_name} | {c.post_count} | {c.category}"
                for c in candidates
            ]
        tags_block = "\n".join(tag_lines) if tag_lines else "（无候选标签）"
        debug_log(lambda: f"[Danbooru.process] {{标签}}注入 {len(tag_lines)} 行候选，前3行: {tag_lines[:3]}")

        # {{标签}} 占位替换进 system_prompt
        system_prompt = (preset.system_prompt or "").replace("{{标签}}", tags_block)
        # 用户提示词：原始中文描述 + 可选的角色外貌参考块
        user_prompt = f"中文描述：{text}"
        # 角色外貌参考：仅当传入且至少一项 tag 非空时追加（向后兼容，全空等价于不传）
        if character_appearances:
            appear_lines = [
                f"- {name}: {tags.strip()}"
                for name, tags in character_appearances
                if name and tags and tags.strip()
            ]
            if appear_lines:
                user_prompt += (
                    "\n\n角色外貌参考（按需选用，把这些外貌 tag 融进结果，"
                    "与中文描述主体一致优先）：\n" + "\n".join(appear_lines)
                )
                debug_log(lambda: f"[Danbooru.process] 注入角色外貌参考 {len(appear_lines)} 项: {appear_lines}")
        user_prompt += "\n\n请从这个描述中挑选并整理出英文 Danbooru tag 串。"

        tmp_preset = Preset(
            name="danbooru_tag",
            system_prompt=system_prompt,
            temperature=preset.temperature,
            max_tokens=preset.max_tokens,
            top_p=preset.top_p,
        )
        try:
            llm = LlmClient(api, tmp_preset, jailbreak_prefix=self._jb_prefix())
            # [!] 用 chat_cancelable 透传 cancel_check，停止生成时可中断加工（§6 契约）。
            result = llm.chat_cancelable(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                cancel_check=cancel_check,
            )
        except Exception as e:
            debug_log(lambda: f"[Danbooru.process] 调用失败: {e}")
            return ""
        if result.cancelled:
            debug_log("[Danbooru.process] 加工被用户取消")
            return ""
        if result.error or not result.content:
            debug_log(lambda: f"[Danbooru.process] 无内容返回: error={result.error!r} content={result.content!r}")
            return ""
        debug_log(lambda: f"[Danbooru.process] LLM 原始输出: {result.content!r}")
        out = parse_tag_output(result.content)
        debug_log(lambda: f"[Danbooru.process] parse_tag_output 清洗后: {out!r}")
        # 出参给目标画图模型时 `_` -> 空格（用户目标客户端不识下划线分隔的 Danbooru 原名）。
        # 注意：仅转最终输出串，召回/匹配全链路继续用 `_` 原名（ChromaDB metadata.name、
        # FTS5.name、{{标签}} 注入仍用原名），避免影响检索精度。
        final = out.replace("_", " ") if out else ""
        debug_log(lambda: f"[Danbooru.process] 末步 _->空格 后最终输出: {final!r}")
        return final

    # ============ 编排入口 ============
    def process_image_description(
        self,
        text: str,
        session_api: Optional[ApiConfig] = None,
        user_selected: Optional[list[str]] = None,
        character_appearances: Optional[list[tuple[str, str]]] = None,
        cancel_check=None,
    ) -> tuple[str, str]:
        """编排器入口：中文描述 -> (正向 tag 串, 负面模板)。

        - 纯英文透传：返回 (text, "")，不注入角色外貌 tag（透传语义是用户已写好英文 tag，不应被 LLM 改写）
        - 含中文：
            * 手改模式：调用方通过 user_selected 传入用户勾选结果（编排器用 callback 拿）
            * 自动模式：user_selected=None，内部召回全量送 LLM
            * character_appearances：会话内角色固定外貌 tag，注入 LLM user prompt 让其按描述选用
        - cancel_check：透传给 process_to_tags -> chat_cancelable，停止生成时可中断加工（§6 契约）。
          recall_candidates 只查 ChromaDB/FTS5（毫秒级），不透传。
        返回 (positive, negative)。加工失败/取消 positive 为空串（编排器据此跳过生图）。
        """
        preset = self.storage.load_danbooru_preset()
        negative = preset.negative_prompt or ""
        if not contains_chinese(text):
            return text, ""  # 纯英文透传，不动负面（保持 ComfyUI 原行为）

        if user_selected is None:
            # 自动模式：内部召回，透传 preset 融合权重
            candidates = self.recall_candidates(
                text, preset.recall_top_n, preset.allow_nsfw, session_api,
                weights=(preset.weight_emb, preset.weight_fts, preset.weight_wiki, preset.weight_pc),
                enable_wiki=preset.enable_wiki_fts,
                allow_categories=preset.allow_categories,
            )
        else:
            # 手改模式：编排器已通过 callback 拿到用户勾选，这里不重复召回
            candidates = []
        positive = self.process_to_tags(
            text, candidates, user_selected, preset, session_api,
            character_appearances=character_appearances,
            cancel_check=cancel_check,
        )
        if positive:
            # 固定正面质量/画风词拼在加工产出前（LLM 已被约束只出画面内容 tag，
            # 质量词统一由 positive_prefix 控制，避免不同 LLM 产出风格不一）。
            prefix = (preset.positive_prefix or "").strip()
            if prefix:
                positive = f"{prefix}, {positive}"
        return positive, negative
