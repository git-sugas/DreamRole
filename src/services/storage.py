"""持久化存储层：JSON 文件存储配置对象，SQLite 存储消息与统计。"""
from __future__ import annotations
from src.utils.debug import debug_log
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Optional

from src.config import paths
from src.models import (
    Character, Message, Session, ApiConfig, Preset, WorldBook, ApiStats,
    RenderRulesConfig, MemoryPreset, User, SummaryPreset,
    DanbooruPreset, AppConfig, default_app_config,
)


def _fts_cn_tokens(s: str, join_sep: str, verbose: bool = False,
                   for_search: bool = False,
                   drop_single_cjk: bool = False) -> str:
    """Danbooru FTS5 词级 token 化（纯 jieba 词级，不拆字；cn_search 与 wiki_search 共用）。

    - 切词模式由 for_search 控制：
      * for_search=False（默认，入库端）：jieba.cut 精确模式，每个词单独一个 token
        （精确词边界）。词典里的长词按整词切分（如 alias.dict 中的「黑发女孩」切为整词）。
      * for_search=True（查询端）：jieba.cut_for_search 搜索引擎模式，对长词递归切出
        子词 + 整词（如「黑发女孩」-> 黑发 / 女孩 / 黑发女孩）。子词扩大召回范围
        （命中库里独立的「黑发」「女孩」tag 行），整词保留精确匹配。入库端用精确模式
        切词建索引，查询端用搜索模式切出子词后，子词在库里本就有对应 tag 行可命中，
        两端切词方式不同但 token 仍能对齐命中，无需重建索引。
    - **不拆字**：包括 OOV（词典外新词）也整词保留为一个 token，不把词内 CJK 字单独成
      token。词典内词已有精确边界；OOV 词拆字只会引入单字噪声----查"英雄联盟"时把
      "英/雄/联/盟"也并进 OR，导致含这些字但语义无关的 tag 被 bm25 误召回。冷门 OOV
      字面召回的损失由 embedding 语义路兜底，alias.dict 已收录 ~16万 cn_name，真正 OOV
      极少，拆字坏处大于好处。for_search=True 时 cut_for_search 对 OOV 长词拆出的单字，
      由查询端单字白名单过滤兜底（虚词丢弃、有意义单字保留）。
    - **过滤纯符号 token**：jieba 切出的标点/符号 token（如 （ ） ， 《 》 。 ）被丢弃，
      不进入索引/MATCH。判据=token 内不含任何字母或数字（CJK 汉字 isalnum()=True 视为
      有效）时丢弃。功能上 FTS5 unicode61 本就忽略标点，过滤只是让 cn_search/wiki_search
      存储更干净、日志可读，不改变命中行为。
    - **drop_single_cjk=True 时丢弃 CJK 单字 token**：wiki 是自然语言句子，jieba 精确模式
      会把虚词/副词（的/中/或/与/和/是/为/在...）切成单字，占 wiki_search token 约 26%，
      严重稀释 bm25 信噪比。wiki 入库用此参数丢弃全部 CJK 单字（wiki 是低权重兜底路，
      单字语义太弱，过滤利大于弊；查询端白名单本就丢查询侧虚词单字，入库侧留着也匹配不上）。
      cn 入库不传此参数（默认 False）——cn_name 是结构化别名，单字可能是有意义角色名/属性
      （如「丛」「一」「弓」），需保留供查询端白名单召回。
    - ASCII 连续字母数字/下划线聚成一个 token（如 Danbooru 英文名）。

    入库时 join_sep=' '（建多 token 索引），查询时 join_sep=' OR '（MATCH 表达式）。
    cn_search 与 wiki_search 两路共用此函数。jieba 默认词典对 nsfw 覆盖不全，
    DanbooruService 启动时会 load_userdict 加载 nsfw 词典文件，提升 nsfw 切分精度。

    verbose=True 时打印 jieba 切词与 token 日志（查询路径用，每次只一条查询；
    入库路径 5 万条会刷屏，故入库时 verbose=False 不打印）。
    """
    if not s:
        return ""
    import jieba
    tokens: list[str] = []
    seen: set[str] = set()
    # for_search=True 用搜索引擎模式（子词+整词，扩大查询召回）；否则精确模式（入库对齐整词边界）。
    jieba_words = list(jieba.cut_for_search(s) if for_search else jieba.cut(s))
    dropped_syms: list[str] = []   # 被过滤的纯符号 token（仅日志用）
    dropped_single: list[str] = []  # 被丢弃的 CJK 单字 token（仅日志用，drop_single_cjk=True 时）
    for word in jieba_words:
        word = word.strip()
        if not word:
            continue
        # 过滤纯符号 token：不含任何字母/数字（CJK 汉字 isalnum()=True）则丢弃。
        # 如 （ ） ， 《 》 。 等标点被 jieba 当独立 token 切出，进索引无意义还占空间。
        if not any(ch.isalnum() for ch in word):
            dropped_syms.append(word)
            continue
        # drop_single_cjk：丢弃 CJK 单字 token（wiki 入库用，过滤虚词噪声）。
        if drop_single_cjk and len(word) == 1 and "\u4e00" <= word <= "\u9fff":
            dropped_single.append(word)
            continue
        if word not in seen:
            seen.add(word)
            tokens.append(word)
        # 纯词级：不拆字。词典内词（dt.FREQ>0）与 OOV 词（freq=0）一视同仁整词保留，
        # 避免 OOV 拆字引入单字噪声污染 bm25 排序。
    result = join_sep.join(tokens)
    if verbose:
        # 关键日志：jieba 原始切词 + token 结果，方便验证分词是否正确
        debug_log(lambda: f"[FTS5.tokens] 输入: {s!r}")
        debug_log(lambda: f"[FTS5.tokens] jieba 切词: {jieba_words}")
        debug_log(lambda: f"[FTS5.tokens] token (join={join_sep!r}): {result}")
        if dropped_syms:
            debug_log(lambda: f"[FTS5.tokens] 过滤纯符号 token: {dropped_syms}")
        if dropped_single:
            debug_log(lambda: f"[FTS5.tokens] 丢弃 CJK 单字(drop_single_cjk): {dropped_single}")
    return result


class Storage:
    """单例存储管理器。"""

    _instance: Optional["Storage"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._db_lock = threading.Lock()
        # 单例下复用单一持久连接（check_same_thread=False 允许在 QThread 中使用）。
        # 所有访问由 self._db_lock 串行化，避免并发问题。
        # 旧实现每次 _get_conn 都新建连接且不会关闭（with sqlite3.Connection 只管事务
        # 不管关闭），长时间运行会泄漏大量连接。
        self._conn: Optional[sqlite3.Connection] = None
        # 由 app.py 注入：删除角色时级联清理 ChromaDB collection + 计数文件 + summary 文件。
        # 未注入时 delete_character 仅清 SQLite 三表（行为不劣化于旧版）。
        self._memory_service = None
        self._init_db()

    def set_memory_service(self, memory_service) -> None:
        """注入 MemoryService，供 delete_character 级联清理 ChromaDB + 计数文件。"""
        self._memory_service = memory_service

    # ============ SQLite ============
    def _get_conn(self) -> sqlite3.Connection:
        """返回复用的单一持久连接（线程安全由 _db_lock 保证）。"""
        if self._conn is None:
            self._conn = sqlite3.connect(paths.db_path(), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        with self._db_lock, self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    character_id TEXT,
                    character_name TEXT,
                    content TEXT,
                    timestamp TEXT,
                    collapsed INTEGER DEFAULT 0,
                    collapsed_reason TEXT,
                    tokens INTEGER DEFAULT 0,
                    image_path TEXT,
                    is_image_only INTEGER DEFAULT 0,
                    summary_of TEXT,
                    is_stopped INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

                CREATE TABLE IF NOT EXISTS stats (
                    api_id TEXT PRIMARY KEY,
                    total_prompt_tokens INTEGER DEFAULT 0,
                    total_completion_tokens INTEGER DEFAULT 0,
                    total_cached_tokens INTEGER DEFAULT 0,
                    request_count INTEGER DEFAULT 0,
                    last_reset TEXT
                );

                -- ============ 角色 Embedding Hybrid 记忆（embedding_hybrid 模式专用）============
                -- 三张表：一张主存储普通表 + 两张 FTS5 虚拟表（triggers 路 / detail 路物理分离）。
                -- 拆双 FTS5 表原因与 Danbooru 一致：bm25 文档长度归一化用「所有索引列长度之和」，
                -- triggers（3-4 词）与 detail（十几~几十词）同表会让 detail 长文本稀释 triggers 命中的
                -- bm25（重蹈 Danbooru「阿狸皮肤反超」坑）。物理拆表后各路 bm25 独立归一化，互不污染。
                -- 主存储普通表：召回拿 seq 后反查 detail 原文渲染。每角色独立（按 character_id 区分），
                -- 纯追加不入库（不删除旧条目，靠 seq 单调递增 + 提示词告诉 LLM 大 seq 为准）。
                CREATE TABLE IF NOT EXISTS char_memory_entry (
                    character_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    triggers TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_msg_index INTEGER DEFAULT 0,
                    PRIMARY KEY (character_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_char_mem_cid_seq
                    ON char_memory_entry(character_id, seq);

                -- triggers 路 FTS5：仅 triggers_search 索引列参与 MATCH/bm25，短而精（3-4 词）。
                -- triggers_search = jieba 精确分词 + drop_single_cjk（库内绝无单字 token）。
                CREATE VIRTUAL TABLE IF NOT EXISTS char_mem_fts_triggers USING fts5(
                    character_id UNINDEXED,
                    seq UNINDEXED,
                    triggers UNINDEXED,
                    triggers_search,
                    tokenize = 'unicode61'
                );
                -- detail 路 FTS5：仅 detail_search 索引列参与 MATCH/bm25，长文本但只和 detail 路自己比。
                -- detail_search = jieba 精确分词 + drop_single_cjk（库内绝无单字 token）。
                CREATE VIRTUAL TABLE IF NOT EXISTS char_mem_fts_detail USING fts5(
                    character_id UNINDEXED,
                    seq UNINDEXED,
                    detail UNINDEXED,
                    detail_search,
                    tokenize = 'unicode61'
                );

                -- Danbooru tag FTS5 全文索引：一 tag 一行（5万）。拆双表：
                -- 主表 danbooru_tag_fts 仅 cn_search（主召回，高置信度 fts_sim）；
                -- wiki 表 danbooru_tag_fts_wiki 仅 wiki_search（语义兜底，低置信度 wiki_sim）。
                -- [!] 拆表原因：FTS5 bm25 的文档长度归一化用「所有索引列长度之和」，
                -- 与 MATCH 列前缀无关。旧版 cn_search+wiki_search 同表时，wiki 长文本（动辄
                -- 数十词）会把 cn 命中的 bm25 稀释（如 ahri_(league) wiki=28 词 -> 主 tag
                -- 排最后）。拆表后各路 bm25 独立，互不污染。列权重参数 bm25(t,1.0,0.0) 实测
                -- 只改 tf 得分不改文档长度归一化，无法绕过，故只能物理拆表。
                -- 两表均冗余存 post_count/category/nsfw（UNINDEXED）供召回时直接取，避免 join。
                -- cn_search/wiki_search 存 jieba 词级切分后空格分隔的形式：每个 jieba 词一个
                -- token（纯词级不拆字，OOV 整词保留）。unicode61 按非字母数字边界切分，空格
                -- 天然分隔 token。两路共用同一 _fts_cn_tokens，入库/查询 token 对齐。
                CREATE VIRTUAL TABLE IF NOT EXISTS danbooru_tag_fts USING fts5(
                    name UNINDEXED,
                    cn_name_raw UNINDEXED,
                    cn_search,
                    post_count UNINDEXED,
                    category UNINDEXED,
                    nsfw UNINDEXED,
                    tokenize = 'unicode61'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS danbooru_tag_fts_wiki USING fts5(
                    name UNINDEXED,
                    wiki_search,
                    wiki UNINDEXED,
                    post_count UNINDEXED,
                    category UNINDEXED,
                    nsfw UNINDEXED,
                    tokenize = 'unicode61'
                );
            """)
            # ---- 历史兼容：为旧库补列 ----
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
            if "is_stopped" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN is_stopped INTEGER DEFAULT 0")
            # ---- 历史兼容：FTS5 旧 schema DROP 重建 ----
            # 版本演进：早期 name/cn_name/wiki/... -> 加 cn_search/wiki_search 单表 ->
            # 现拆双表（主表仅 cn_search、wiki 表 danbooru_tag_fts_wiki）。
            # 检测旧 schema 标志：danbooru_tag_fts 存在且含 wiki_search 列（旧单表）。
            # FTS5 虚拟表不支持 ALTER，只能 DROP 重建。旧库数据会在下次 build_index 时重灌。
            fts_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='danbooru_tag_fts'"
            ).fetchone() is not None
            if fts_exists:
                fts_cols = {r["name"] for r in conn.execute(
                    "PRAGMA table_info(danbooru_tag_fts)"
                )}
                # 旧单表含 wiki_search/wiki 列 -> DROP（新两表由上方 CREATE IF NOT EXISTS 新建）
                if "wiki_search" in fts_cols or "wiki" in fts_cols:
                    conn.execute("DROP TABLE IF EXISTS danbooru_tag_fts")
                    conn.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS danbooru_tag_fts USING fts5(
                            name UNINDEXED,
                            cn_name_raw UNINDEXED,
                            cn_search,
                            post_count UNINDEXED,
                            category UNINDEXED,
                            nsfw UNINDEXED,
                            tokenize = 'unicode61'
                        )
                    """)
                    # 残留的旧 wiki 表（若上一版已建过同名）也一并清掉重建成新 schema
                    conn.execute("DROP TABLE IF EXISTS danbooru_tag_fts_wiki")
                    conn.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS danbooru_tag_fts_wiki USING fts5(
                            name UNINDEXED,
                            wiki_search,
                            wiki UNINDEXED,
                            post_count UNINDEXED,
                            category UNINDEXED,
                            nsfw UNINDEXED,
                            tokenize = 'unicode61'
                        )
                    """)

    # ============ 通用 JSON 读写 ============
    # 模块级 JSON 写锁：护所有 JSON 文件写（角色卡/会话/预设/世界书/render_rules 等），
    # 防 ChatWorker 线程与 UI 主线程并发写交错损坏文件。SQLite 写另由 _db_lock 保护。
    _json_write_lock = threading.Lock()

    @classmethod
    def _save_json_atomic(cls, filepath: str, data, indent: int = 2):
        """原子写 JSON：tmp 文件 + os.replace 原子替换 + 写锁。

        防崩溃半写损坏（直接 open("w") 中途崩溃留截断文件，下次 json.load 抛错）
        与并发写交错（两个线程同时 open("w") 会让文件内容交错）。
        """
        tmp = filepath + ".tmp"
        with cls._json_write_lock:
            try:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=indent)
                os.replace(tmp, filepath)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    @staticmethod
    def _save_json(directory: str, obj_id: str, data: dict):
        filepath = os.path.join(directory, f"{obj_id}.json")
        Storage._save_json_atomic(filepath, data)

    @staticmethod
    def _load_json(directory: str, obj_id: str) -> Optional[dict]:
        filepath = f"{directory}/{obj_id}.json"
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    @staticmethod
    def _load_all_json(directory: str) -> list[dict]:
        import os
        results = []
        if not os.path.isdir(directory):
            return results
        for fname in os.listdir(directory):
            if fname.endswith(".json"):
                try:
                    with open(f"{directory}/{fname}", "r", encoding="utf-8") as f:
                        results.append(json.load(f))
                except (json.JSONDecodeError, OSError):
                    pass
        return results

    @staticmethod
    def _delete_json(directory: str, obj_id: str):
        import os
        filepath = f"{directory}/{obj_id}.json"
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass

    # ============ 角色卡 ============
    def save_character(self, char: Character):
        self._save_json(paths.characters_dir(), char.id, char.to_dict())

    def load_character(self, char_id: str) -> Optional[Character]:
        d = self._load_json(paths.characters_dir(), char_id)
        return Character.from_dict(d) if d else None

    def load_all_characters(self) -> list[Character]:
        return [Character.from_dict(d) for d in self._load_all_json(paths.characters_dir())]

    def delete_character(self, char_id: str):
        """删除角色卡 JSON，并级联清理该角色的全部记忆数据。

        [!] 删除角色必须同步清理记忆，否则：
        - SQLite char_memory_entry/FTS5 三表残留孤儿数据；
        - ChromaDB collection 残留（存档导入同 id 角色会"复活"旧记忆挂到新角色上）；
        - {cid}_embed_count.json / summary 记忆文件残留。
        ChromaDB + 计数文件 + summary 文件由注入的 memory_service.clear_memory_by_id 清理
        （app.py 初始化时注入；未注入时仅清 SQLite 三表，行为不劣化于旧版）。
        """
        # 先清记忆（SQLite 三表 + ChromaDB + 计数文件 + summary 文件）
        if self._memory_service is not None:
            try:
                self._memory_service.clear_memory_by_id(char_id)
            except Exception:
                pass
        else:
            # 未注入 memory_service 时至少清 SQLite 三表（hybrid 模式数据）
            try:
                self.clear_char_memory(char_id)
            except Exception:
                pass
        self._delete_json(paths.characters_dir(), char_id)

    # ============ 用户卡 ============
    def save_user(self, user: User):
        self._save_json(paths.users_dir(), user.id, user.to_dict())

    def load_user(self, user_id: str) -> Optional[User]:
        d = self._load_json(paths.users_dir(), user_id)
        return User.from_dict(d) if d else None

    def load_all_users(self) -> list[User]:
        # 按 name（回退 id）排序保证 UI 下拉顺序稳定
        users = [User.from_dict(d) for d in self._load_all_json(paths.users_dir())]
        return sorted(users, key=lambda u: u.name or u.id)

    def delete_user(self, user_id: str):
        self._delete_json(paths.users_dir(), user_id)

    # ============ API 配置 ============
    def save_api(self, api: ApiConfig):
        self._save_json(paths.apis_dir(), api.id, api.to_dict())

    def load_api(self, api_id: str) -> Optional[ApiConfig]:
        d = self._load_json(paths.apis_dir(), api_id)
        return ApiConfig.from_dict(d) if d else None

    def load_all_apis(self) -> list[ApiConfig]:
        return [ApiConfig.from_dict(d) for d in self._load_all_json(paths.apis_dir())]

    def delete_api(self, api_id: str):
        self._delete_json(paths.apis_dir(), api_id)

    # ============ 预设 ============
    def save_preset(self, preset: Preset):
        self._save_json(paths.presets_dir(), preset.id, preset.to_dict())

    def load_preset(self, preset_id: str) -> Optional[Preset]:
        d = self._load_json(paths.presets_dir(), preset_id)
        return Preset.from_dict(d) if d else None

    def load_all_presets(self) -> list[Preset]:
        return [Preset.from_dict(d) for d in self._load_all_json(paths.presets_dir())]

    def delete_preset(self, preset_id: str):
        self._delete_json(paths.presets_dir(), preset_id)

    # ============ 世界书 ============
    def save_world_book(self, wb: WorldBook):
        self._save_json(paths.world_books_dir(), wb.id, wb.to_dict())

    def load_world_book(self, wb_id: str) -> Optional[WorldBook]:
        d = self._load_json(paths.world_books_dir(), wb_id)
        return WorldBook.from_dict(d) if d else None

    def load_all_world_books(self) -> list[WorldBook]:
        return [WorldBook.from_dict(d) for d in self._load_all_json(paths.world_books_dir())]

    def delete_world_book(self, wb_id: str):
        self._delete_json(paths.world_books_dir(), wb_id)

    # ============ 会话 ============
    def save_session(self, session: Session):
        self._save_json(paths.chats_dir(), session.id, session.to_dict())

    def load_session(self, session_id: str) -> Optional[Session]:
        d = self._load_json(paths.chats_dir(), session_id)
        return Session.from_dict(d) if d else None

    def load_all_sessions(self) -> list[Session]:
        return [Session.from_dict(d) for d in self._load_all_json(paths.chats_dir())]

    def delete_session(self, session_id: str):
        self._delete_json(paths.chats_dir(), session_id)
        # 同时删除该会话的所有消息
        with self._db_lock, self._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    # ============ 消息（SQLite）============
    def save_message(self, msg: Message):
        with self._db_lock, self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO messages
                (id, session_id, role, character_id, character_name, content, timestamp,
                 collapsed, collapsed_reason, tokens, image_path, is_image_only, summary_of, is_stopped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                msg.id, msg.session_id, msg.role, msg.character_id, msg.character_name,
                msg.content, msg.timestamp, int(msg.collapsed), msg.collapsed_reason,
                msg.tokens, msg.image_path, int(msg.is_image_only),
                json.dumps(msg.summary_of, ensure_ascii=False), int(msg.is_stopped),
            ))
            conn.commit()

    def update_message(self, msg: Message):
        self.save_message(msg)

    def load_messages(self, session_id: str) -> list[Message]:
        with self._db_lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,),
            ).fetchall()
        messages = []
        for row in rows:
            d = dict(row)
            d["collapsed"] = bool(d["collapsed"])
            d["is_image_only"] = bool(d["is_image_only"])
            d["is_stopped"] = bool(d.get("is_stopped", 0))
            try:
                d["summary_of"] = json.loads(d["summary_of"]) if d["summary_of"] else []
            except (json.JSONDecodeError, TypeError):
                d["summary_of"] = []
            messages.append(Message.from_dict(d))
        return messages

    def delete_message(self, msg_id: str):
        # 删消息前先取 image_path，删除后同步清理磁盘图片文件，避免孤儿堆积
        with self._db_lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT image_path FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
            if row and row["image_path"]:
                try:
                    os.remove(row["image_path"])
                except OSError:
                    pass
            conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            conn.commit()

    # ============ 应用全局配置（单例，data/app_config.json）============
    def load_app_config(self) -> AppConfig:
        """加载应用全局配置；文件不存在返回默认（不自动落盘）。"""
        path = paths.config_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return AppConfig.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return default_app_config()

    def save_app_config(self, config: AppConfig):
        """保存应用全局配置到 data/app_config.json。"""
        self._save_json_atomic(paths.config_path(), config.to_dict())

    # ============ 气泡配色规则（独立 JSON 文件）============
    def load_render_rules(self) -> RenderRulesConfig:
        """加载配色规则配置；文件不存在返回默认配置（不落盘）。"""
        path = paths.render_rules_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return RenderRulesConfig.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            from src.models import default_config
            return default_config()

    def save_render_rules(self, cfg: RenderRulesConfig):
        """保存配色规则配置到 data/render_rules.json。"""
        self._save_json_atomic(paths.render_rules_path(), cfg.to_dict())

    # ============ 记忆整理预设（独立 JSON 文件，单例）============
    def load_memory_preset(self) -> MemoryPreset:
        """加载记忆整理预设；文件不存在返回默认（不自动落盘）。"""
        path = paths.memory_preset_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return MemoryPreset.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            from src.models import default_memory_preset
            return default_memory_preset()

    def save_memory_preset(self, preset: MemoryPreset):
        """保存记忆整理预设到 data/memory_preset.json。"""
        self._save_json_atomic(paths.memory_preset_path(), preset.to_dict())

    # ============ 上文总结预设 ============
    def load_summary_preset(self) -> SummaryPreset:
        """加载上文总结预设；文件不存在返回默认（不自动落盘）。"""
        path = paths.summary_preset_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SummaryPreset.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            from src.models import default_summary_preset
            return default_summary_preset()

    def save_summary_preset(self, preset: SummaryPreset):
        """保存上文总结预设到 data/summary_preset.json。"""
        self._save_json_atomic(paths.summary_preset_path(), preset.to_dict())

    # ============ Danbooru tag 加工预设 ============
    def load_danbooru_preset(self) -> DanbooruPreset:
        """加载 Danbooru 加工预设；文件不存在返回默认（不自动落盘）。"""
        path = paths.danbooru_preset_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return DanbooruPreset.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            from src.models import default_danbooru_preset
            return default_danbooru_preset()

    def save_danbooru_preset(self, preset: DanbooruPreset):
        """保存 Danbooru 加工预设到 data/danbooru_preset.json。"""
        self._save_json_atomic(paths.danbooru_preset_path(), preset.to_dict())

    # ============ Danbooru tag FTS5 索引（拆双表：主表 cn_search 主召回 bm25，wiki 表 wiki_search 兜底 bm25）============
    def clear_danbooru_fts(self) -> None:
        """清空 FTS5 索引（重建库前调用）。主表 + wiki 表同步清空。"""
        with self._db_lock, self._get_conn() as conn:
            conn.execute("DELETE FROM danbooru_tag_fts")
            conn.execute("DELETE FROM danbooru_tag_fts_wiki")
            conn.commit()

    def bulk_insert_danbooru_fts(self, rows: list[dict]) -> None:
        """批量插入 FTS5 双表。每行需含 name/cn_name/wiki/post_count/category/nsfw。

        拆双表：主表 danbooru_tag_fts 插 cn_search（cn_name 拆词，jieba 纯词级不拆字）
        + cn_name_raw/post_count/category/nsfw；wiki 表 danbooru_tag_fts_wiki 插
        wiki_search（wiki 拆词，同样 jieba 纯词级）+ wiki 原始串/post_count/category/nsfw。
        两表均冗余存 post_count/category/nsfw（UNINDEXED）供召回时直接取，避免 join。
        入库端不过滤单字（保留全量），单字白名单过滤只在查询端做。
        """
        if not rows:
            return
        sql_cn = ("INSERT INTO danbooru_tag_fts "
                  "(name, cn_name_raw, cn_search, post_count, category, nsfw) "
                  "VALUES (?, ?, ?, ?, ?, ?)")
        sql_wiki = ("INSERT INTO danbooru_tag_fts_wiki "
                    "(name, wiki_search, wiki, post_count, category, nsfw) "
                    "VALUES (?, ?, ?, ?, ?, ?)")
        # 入库分词不逐条打日志（5万条会刷屏），只打汇总 + 第一条样本验证 token 化
        sample = rows[0]
        sample_cn_tokens = _fts_cn_tokens(sample["cn_name"], " ", verbose=False)
        sample_wiki_tokens = _fts_cn_tokens(sample.get("wiki", ""), " ", verbose=False,
                                            drop_single_cjk=True)
        debug_log(lambda: f"[FTS5.insert] SQL(主表): {sql_cn}")
        debug_log(lambda: f"[FTS5.insert] SQL(wiki表): {sql_wiki}")
        debug_log(lambda: f"[FTS5.insert] 批量插入 {len(rows)} 行")
        debug_log(lambda: f"[FTS5.insert] 样本[0]: name={sample['name']!r} cn_name_raw={sample['cn_name']!r} -> cn_search={sample_cn_tokens!r}")
        debug_log(lambda: f"[FTS5.insert] 样本[0]: wiki={sample.get('wiki','')!r} -> wiki_search={sample_wiki_tokens!r}")
        # [!] 分批 commit 释放 _db_lock 窗口：5 万行一次性 executemany+commit 会长时间
        # 占 _db_lock，期间 ChatWorker 的消息读写阻塞。每 5000 行一批提交，让出锁窗口
        # 给消息写。切词在锁内但 Python 先构造列表再传 executemany，切词开销不因分批放大。
        BATCH = 5000
        cn_params = [
            (r["name"], r["cn_name"], _fts_cn_tokens(r["cn_name"], " ", verbose=False),
             r["post_count"], r["category"], r["nsfw"])
            for r in rows
        ]
        wiki_params = [
            (r["name"], _fts_cn_tokens(r.get("wiki", ""), " ", verbose=False,
                                       drop_single_cjk=True),
             r["wiki"], r["post_count"], r["category"], r["nsfw"])
            for r in rows
        ]
        with self._db_lock, self._get_conn() as conn:
            for i in range(0, len(rows), BATCH):
                conn.executemany(sql_cn, cn_params[i:i + BATCH])
                conn.executemany(sql_wiki, wiki_params[i:i + BATCH])
                conn.commit()  # 每批提交，释放锁窗口给消息写
        debug_log(lambda: f"[FTS5.insert] 完成，FTS5 主表当前 {self.count_danbooru_fts()} 行")

    def count_danbooru_fts(self) -> int:
        """FTS5 表内当前行数（异常/无表返回 0）。"""
        try:
            with self._db_lock, self._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM danbooru_tag_fts"
                ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    # ============ 角色 Embedding Hybrid 记忆存储（主表 + 两张 FTS5 表，纯追加）============
    def max_char_memory_seq(self, character_id: str) -> int:
        """该角色当前最大 seq（无记录返回 0，新条目 seq = 此值 + 1）。"""
        with self._db_lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM char_memory_entry WHERE character_id = ?",
                (character_id,),
            ).fetchone()
        return int(row["m"]) if row else 0

    def insert_char_memory(
            self, character_id: str, seq: int, triggers: str, detail: str,
            created_msg_index: int = 0,
    ) -> None:
        """插入一条记忆到三处存储：主表 + triggers FTS5 + detail FTS5（一次事务）。

        triggers_search / detail_search 均用 jieba 精确分词 + drop_single_cjk，
        保证库内绝无单字 token（查询端也丢单字，库里库外完全对齐）。
        """
        trig_search = _fts_cn_tokens(triggers, " ", verbose=False, drop_single_cjk=True)
        det_search = _fts_cn_tokens(detail, " ", verbose=False, drop_single_cjk=True)
        debug_log(lambda: f"[CharMem.insert] cid={character_id[:8]} seq={seq} "
                  f"triggers={triggers!r} -> trig_search={trig_search!r}")
        debug_log(lambda: f"[CharMem.insert]   detail={detail!r} -> det_search={det_search!r}")
        with self._db_lock, self._get_conn() as conn:
            conn.execute(
                "INSERT INTO char_memory_entry (character_id, seq, triggers, detail, created_msg_index) "
                "VALUES (?, ?, ?, ?, ?)",
                (character_id, seq, triggers, detail, created_msg_index),
            )
            conn.execute(
                "INSERT INTO char_mem_fts_triggers (character_id, seq, triggers, triggers_search) "
                "VALUES (?, ?, ?, ?)",
                (character_id, seq, triggers, trig_search),
            )
            conn.execute(
                "INSERT INTO char_mem_fts_detail (character_id, seq, detail, detail_search) "
                "VALUES (?, ?, ?, ?)",
                (character_id, seq, detail, det_search),
            )
            conn.commit()

    def query_char_mem_fts_triggers(
            self, text: str, character_id: str, top_n: int,
    ) -> list[dict]:
        """triggers 路 FTS5 召回：MATCH triggers_search + bm25。

        返回 [{seq, triggers, s, fts_src='trig'}, ...]，s 为 bm25 负值（越负越相关）。
        查询端 jieba.cut_for_search + drop_single_cjk（库里库外无单字，对齐）。
        无分词结果返回空。
        """
        match_expr = _fts_cn_tokens(text, " OR ", verbose=False, for_search=True,
                                    drop_single_cjk=True)
        if not match_expr:
            return []
        trig_match = " OR ".join(f"triggers_search:{t}" for t in match_expr.split(" OR ") if t)
        sql = ("SELECT seq, triggers, bm25(char_mem_fts_triggers) AS s "
               "FROM char_mem_fts_triggers "
               "WHERE char_mem_fts_triggers MATCH ? AND character_id = ? "
               "ORDER BY s ASC LIMIT ?")
        debug_log(lambda: f"[CharMem.fts_trig] cid={character_id[:8]} match={trig_match!r} top_n={top_n}")
        try:
            with self._db_lock, self._get_conn() as conn:
                rows = [dict(r) for r in conn.execute(
                    sql, (trig_match, character_id, top_n * 3),
                ).fetchall()]
        except sqlite3.OperationalError as e:
            debug_log(lambda: f"[CharMem.fts_trig] 查询失败: {e}")
            return []
        for r in rows:
            r["fts_src"] = "trig"
        return rows

    def query_char_mem_fts_detail(
            self, text: str, character_id: str, top_n: int,
    ) -> list[dict]:
        """detail 路 FTS5 召回：MATCH detail_search + bm25（独立表，不稀释 triggers 路）。

        返回 [{seq, detail, s, fts_src='detail'}, ...]，s 为 bm25 负值。
        查询端 jieba.cut_for_search + drop_single_cjk（库里库外无单字，对齐）。
        """
        match_expr = _fts_cn_tokens(text, " OR ", verbose=False, for_search=True,
                                    drop_single_cjk=True)
        if not match_expr:
            return []
        det_match = " OR ".join(f"detail_search:{t}" for t in match_expr.split(" OR ") if t)
        sql = ("SELECT seq, detail, bm25(char_mem_fts_detail) AS s "
               "FROM char_mem_fts_detail "
               "WHERE char_mem_fts_detail MATCH ? AND character_id = ? "
               "ORDER BY s ASC LIMIT ?")
        debug_log(lambda: f"[CharMem.fts_det] cid={character_id[:8]} match={det_match!r} top_n={top_n}")
        try:
            with self._db_lock, self._get_conn() as conn:
                rows = [dict(r) for r in conn.execute(
                    sql, (det_match, character_id, top_n * 3),
                ).fetchall()]
        except sqlite3.OperationalError as e:
            debug_log(lambda: f"[CharMem.fts_det] 查询失败: {e}")
            return []
        for r in rows:
            r["fts_src"] = "detail"
        return rows

    def fetch_char_memory_details(
            self, character_id: str, seqs: list[int],
    ) -> dict[int, str]:
        """按 seq 批量反查 detail 原文（召回拿 seq 后渲染用）。

        返回 {seq: detail}，未命中的 seq 不在 dict 中。
        """
        if not seqs:
            return {}
        placeholders = ",".join("?" for _ in seqs)
        sql = (f"SELECT seq, detail FROM char_memory_entry "
               f"WHERE character_id = ? AND seq IN ({placeholders})")
        try:
            with self._db_lock, self._get_conn() as conn:
                rows = conn.execute(sql, [character_id] + list(seqs)).fetchall()
            return {int(r["seq"]): r["detail"] for r in rows if r["detail"]}
        except sqlite3.OperationalError as e:
            debug_log(lambda: f"[CharMem.fetch_det] 反查 detail 失败: {e}")
            return {}

    def fetch_all_char_memory_entries(self, character_id: str) -> list[dict]:
        """读取该角色全部记忆条目（按 seq 升序），供记忆页两栏展示。

        返回 [{seq, triggers, detail, created_msg_index}, ...]。
        """
        sql = ("SELECT seq, triggers, detail, created_msg_index "
               "FROM char_memory_entry WHERE character_id = ? ORDER BY seq ASC")
        try:
            with self._db_lock, self._get_conn() as conn:
                rows = [dict(r) for r in conn.execute(sql, (character_id,)).fetchall()]
        except sqlite3.OperationalError as e:
            debug_log(lambda: f"[CharMem.fetch_all] 读取失败: {e}")
            return []
        return rows

    def count_char_memory_entries(self, character_id: str) -> int:
        """该角色记忆条目数（异常/无表返回 0）。"""
        try:
            with self._db_lock, self._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM char_memory_entry WHERE character_id = ?",
                    (character_id,),
                ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def clear_char_memory(self, character_id: str) -> None:
        """清空该角色全部记忆（主表 + 两张 FTS5 表）。ChromaDB 由调用方清。"""
        with self._db_lock, self._get_conn() as conn:
            conn.execute("DELETE FROM char_memory_entry WHERE character_id = ?", (character_id,))
            conn.execute("DELETE FROM char_mem_fts_triggers WHERE character_id = ?", (character_id,))
            conn.execute("DELETE FROM char_mem_fts_detail WHERE character_id = ?", (character_id,))
            conn.commit()

    def fetch_danbooru_cn_names(self, names: list[str]) -> dict[str, str]:
        """批量反查 tag name -> 完整 cn_name_raw（FTS5 表）。

        用途：embedding 路径单独命中的 tag（FTS5 未命中）在 ChromaDB metadata 里只存
        单条 alias（如\"英雄联盟\"），需从 FTS5 表反查完整 cn_name_raw（如
        \"阿狸,九尾妖狐,英雄联盟\"）覆盖回 TagCandidate.cn_name，避免召回结果里
        同 copyright 下的多个角色 cn_name 全部退化成同一别名无法区分。

        未命中的 name 不出现在返回 dict 中（调用方据 dict.get(name, fallback) 取）。
        """
        if not names:
            return {}
        # FTS5 虚拟表不支持 IN 批量？实际 SQLite 支持对普通列 IN，但 FTS5 虚拟表的
        # UNINDEXED 列经测试可用 IN。这里用占位符展开兼容性更好。
        placeholders = ",".join("?" for _ in names)
        sql = (f"SELECT name, cn_name_raw FROM danbooru_tag_fts "
               f"WHERE name IN ({placeholders})")
        try:
            with self._db_lock, self._get_conn() as conn:
                rows = conn.execute(sql, names).fetchall()
            return {r["name"]: r["cn_name_raw"] for r in rows if r["name"]}
        except sqlite3.OperationalError as e:
            debug_log(lambda: f"[FTS5.fetch_cn] 反查 cn_name 失败: {e}")
            return {}

    def fetch_danbooru_subject_tags(self, names: list[str]) -> list[dict]:
        """批量反查主体标签全量明细（供 recall_candidates 注入常驻主体候选池）。

        返回 [{name, cn_name_raw, post_count, category, nsfw}, ...]，按 names 传入顺序保序，
        库里缺失的项静默跳过（防御建库不全）。与 fetch_danbooru_cn_names 的区别：这里取全量
        明细字段（pc/cat/nsfw 都要），用于构造完整 TagCandidate。
        """
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        sql = (f"SELECT name, cn_name_raw, post_count, category, nsfw "
               f"FROM danbooru_tag_fts WHERE name IN ({placeholders})")
        try:
            with self._db_lock, self._get_conn() as conn:
                rows = {r["name"]: r for r in conn.execute(sql, names).fetchall() if r["name"]}
        except sqlite3.OperationalError as e:
            debug_log(lambda: f"[FTS5.fetch_subject] 反查主体标签失败: {e}")
            return []
        # 按传入 names 顺序保序输出（缺失项跳过）
        return [
            {"name": n,
             "cn_name_raw": rows[n]["cn_name_raw"],
             "post_count": rows[n]["post_count"],
             "category": rows[n]["category"],
             "nsfw": rows[n]["nsfw"]}
            for n in names if n in rows
        ]

    # ============ 单字白名单（查询单字过滤，反向：不在表里丢弃）============
    def load_char_whitelist(self) -> set[str]:
        """读取单字白名单（data/danbooru_dict/char_whitelist.dict，每行一个 CJK 单字）。

        用途：查询端 jieba 切出的「长度为 1 的 CJK token」不在白名单则丢弃（虚词「的/了/
        是/在/中」等单字进 OR MATCH 会把含这些字的 tag 全召回、污染 bm25）。多字 token 与
        ASCII token 不受影响。白名单建库时从单字 alias 自动生成（DanbooruService.
        _write_char_whitelist），用户可在设置页查看/编辑。文件不存在返回空 set（冷启动
        保守丢弃所有 CJK 单字，避免虚词噪声；建库后自动生成）。
        """
        path = os.path.join(paths.danbooru_dict_dir(), "char_whitelist.dict")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {line.strip() for line in f if line.strip()}
        except FileNotFoundError:
            return set()
        except Exception as e:
            debug_log(lambda: f"[FTS5.whitelist] 读取单字白名单失败: {e}")
            return set()

    def save_char_whitelist(self, chars: set[str]) -> None:
        """保存单字白名单（设置页编辑用，覆盖式写回）。原子写防半写损坏。"""
        dict_dir = paths.danbooru_dict_dir()
        path = os.path.join(dict_dir, "char_whitelist.dict")
        tmp = path + ".tmp"
        with self._json_write_lock:
            try:
                os.makedirs(dict_dir, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    for ch in sorted(chars):
                        f.write(ch + "\n")
                os.replace(tmp, path)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    def query_danbooru_fts(self, text: str, top_n: int,
                           allow_nsfw: bool = True,
                           enable_wiki: bool = True) -> list[dict]:
        """FTS5 + BM25 字面召回（拆双表，两路独立 bm25）。返回
        [{name, cn_name_raw, post_count, category, nsfw, s, fts_src}, ...]：
          - s 为 SQLite bm25() 返回的负值（越负越相关）；cn 行与 wiki 行各自带真实 bm25，
            由调用方按 fts_src 分组分别 min-max 归一化（cn 归一化得 fts_sim，wiki 归一化得 wiki_sim）。
          - fts_src="cn"（cn_search 命中，主召回）或 "wiki"（仅 wiki_search 命中，语义兜底）。
            cn_search 已命中的 tag 不再算 wiki 路（按 name 去重，cn 优先），避免双重计分。
          - wiki 行无 cn_name_raw（wiki 表不存该列），留空由调用方 cn_name 补全逻辑兜底。

        enable_wiki=False 时跳过 wiki 路（仅 cn_search），退回纯 cn 逻辑（无 wiki 召回/打分）。

        拆双表（主表 danbooru_tag_fts 仅 cn_search、wiki 表 danbooru_tag_fts_wiki 仅 wiki_search）：
        FTS5 bm25 文档长度归一化用「所有索引列长度之和」，与 MATCH 列前缀无关。旧版同表时
        wiki 长文本会稀释 cn 命中的 bm25（如 ahri_(league) wiki=28 词 -> 主 tag 排最后）。
        拆表后各路 bm25 独立计算，文档长度只含本表列，互不污染。列权重参数 bm25(t,1.0,0.0)
        实测只改 tf 得分不改文档长度归一化，无法绕过，故只能物理拆表。
          路1 主表：cn_search MATCH + bm25，主召回，s 为 cn 相关度 bm25 负值。
          路2 wiki 表：wiki_search MATCH + bm25，wiki 含查询词即召回；
              与路1 按 name 去重，新增行保留 wiki bm25 追加尾部。
        allow_nsfw=False 时 FTS5 UNINDEXED 列无法在 SQL 层 WHERE，改为多取 3 倍再应用层过滤。

        MATCH 表达式 = jieba.cut_for_search 切出的词 token（子词+整词）用 OR 连接（任一命中即召回）。
        用搜索模式而非精确模式：查询「黑发女孩」会切出 黑发/女孩/黑发女孩 三个 token，子词命中
        库里独立的「黑发」「女孩」tag 行，整词命中「黑发女孩」tag 行。入库端仍用精确模式（jieba.cut）
        切整词建索引，两端切词方式不同但 token 能对齐命中（子词在库里本有对应 tag 行），无需重建索引。
        [!] 单字白名单过滤：切出 tokens 后，对「长度为 1 的 CJK token」查 load_char_whitelist()，
        不在白名单的丢弃（虚词「的/中」等单字进 OR 会污染召回；cut_for_search 对 OOV 长词拆出的
        单字也由此过滤兜底）。多字 token 与 ASCII token 不受影响。
        过滤只在查询端做，入库端不过滤（保留全量，避免删掉恰好单字的有意义 tag）。
        """
        match_expr = _fts_cn_tokens(text, " OR ", verbose=True, for_search=True)
        if not match_expr:
            debug_log("[FTS5.query] 分词后为空，跳过查询")
            return []
        # cn/wiki 两路共用同一份纯词级 token 列表（与入库端对齐，不再二次分词）
        all_tokens = [t for t in match_expr.split(" OR ") if t]
        # 单字白名单过滤：丢弃不在白名单的 CJK 单字 token（虚词噪声）
        whitelist = self.load_char_whitelist()
        kept_tokens: list[str] = []
        dropped_single: list[str] = []
        for t in all_tokens:
            is_cjk_single = (
                len(t) == 1 and "\u4e00" <= t <= "\u9fff"
            )
            if is_cjk_single and t not in whitelist:
                dropped_single.append(t)
                continue
            kept_tokens.append(t)
        if dropped_single:
            debug_log(lambda: f"[FTS5.query] 单字白名单过滤: 丢弃 {dropped_single}（不在白名单的 CJK 单字）")
        if not kept_tokens:
            debug_log("[FTS5.query] 单字过滤后 token 为空，跳过查询")
            return []
        cn_match = " OR ".join(f"cn_search:{t}" for t in kept_tokens)
        wiki_match = " OR ".join(f"wiki_search:{t}" for t in kept_tokens)
        limit = top_n * 3 if not allow_nsfw else top_n

        sql_cn = ("SELECT name, cn_name_raw, post_count, category, nsfw, "
                  "       bm25(danbooru_tag_fts) AS s "
                  "FROM danbooru_tag_fts "
                  "WHERE danbooru_tag_fts MATCH ? "
                  "ORDER BY s ASC LIMIT ?")
        debug_log(lambda: f"[FTS5.query] 路1 SQL(主表): {sql_cn}")
        debug_log(lambda: f"[FTS5.query] 路1 参数: match={cn_match!r}, limit={limit}, enable_wiki={enable_wiki}")
        with self._db_lock, self._get_conn() as conn:
            cn_rows = [dict(r) for r in conn.execute(sql_cn, (cn_match, limit)).fetchall()]
            wiki_rows: list[dict] = []
            if enable_wiki:
                if not wiki_match:
                    debug_log("[FTS5.query] wiki 路分词后为空，跳过路2")
                else:
                    # 路2 查 wiki 表：bm25 只反映 wiki_search 列命中相关度（独立表，无 cn 列污染）。
                    sql_wiki = ("SELECT name, post_count, category, nsfw, "
                                "       bm25(danbooru_tag_fts_wiki) AS s "
                                "FROM danbooru_tag_fts_wiki "
                                "WHERE danbooru_tag_fts_wiki MATCH ? "
                                "ORDER BY s ASC LIMIT ?")
                    debug_log(lambda: f"[FTS5.query] 路2 SQL(wiki表): {sql_wiki}")
                    debug_log(lambda: f"[FTS5.query] 路2 参数: match={wiki_match!r}, limit={limit}")
                    wiki_rows = [dict(r) for r in conn.execute(sql_wiki, (wiki_match, limit)).fetchall()]
            else:
                debug_log("[FTS5.query] wiki 路已关闭（enable_wiki=False），仅 cn_search")

        # 路1 标 cn；路2 按 name 去重（cn 优先），新增行标 wiki、保留 wiki bm25。
        for r in cn_rows:
            r["fts_src"] = "cn"
        cn_names = {r["name"] for r in cn_rows if r["name"]}
        wiki_only: list[dict] = []
        for r in wiki_rows:
            name = r.get("name")
            if not name or name in cn_names:
                continue
            r["fts_src"] = "wiki"   # 仅 wiki 命中：低置信度兜底，保留 wiki bm25 供归一化
            r["cn_name_raw"] = ""   # wiki 表不存 cn_name_raw，留空由调用方补全逻辑兜底
            wiki_only.append(r)
        out = cn_rows + wiki_only
        debug_log(lambda: f"[FTS5.query] 路1 cn 命中 {len(cn_rows)} 行，路2 wiki-only 新增 {len(wiki_only)} 行，合并 {len(out)} 行（过滤前）")
        # 打印前 5 条便于验证
        for i, r in enumerate(out[:5]):
            debug_log(lambda: f"[FTS5.query]   [{i}] {r['name']} | cn={r.get('cn_name_raw','')} | bm25={r['s']:.4f} | src={r['fts_src']}")
        if not allow_nsfw:
            out = [r for r in out if not int(r.get("nsfw", 0))]
            debug_log(lambda: f"[FTS5.query] nsfw 过滤后剩 {len(out)} 行")
        return out[:top_n]

    # ============ 统计 ============
    def get_stats(self, api_id: str) -> ApiStats:
        with self._db_lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM stats WHERE api_id = ?", (api_id,)
            ).fetchone()
        if row:
            return ApiStats.from_dict(dict(row))
        return ApiStats(api_id=api_id)

    def save_stats(self, stats: ApiStats):
        with self._db_lock, self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO stats
                (api_id, total_prompt_tokens, total_completion_tokens, total_cached_tokens,
                 request_count, last_reset)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                stats.api_id, stats.total_prompt_tokens, stats.total_completion_tokens,
                stats.total_cached_tokens, stats.request_count, stats.last_reset,
            ))
            conn.commit()

    def increment_stats(
        self, api_id: str, prompt: int, completion: int, cached: int,
    ):
        """原子自增统计：单次 SQL 完成「不存在则插入初始行 / 存在则累加」。

        解决旧 record_usage「读-改-写」跨两次取锁的竞态（并发调用会丢更新）。
        ON CONFLICT(api_id) DO UPDATE 依赖 stats 表的 api_id 主键（见 _init_db schema）。
        """
        now = datetime.now().isoformat()
        with self._db_lock, self._get_conn() as conn:
            conn.execute("""
                INSERT INTO stats
                (api_id, total_prompt_tokens, total_completion_tokens,
                 total_cached_tokens, request_count, last_reset)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(api_id) DO UPDATE SET
                    total_prompt_tokens = total_prompt_tokens + excluded.total_prompt_tokens,
                    total_completion_tokens = total_completion_tokens + excluded.total_completion_tokens,
                    total_cached_tokens = total_cached_tokens + excluded.total_cached_tokens,
                    request_count = request_count + 1
            """, (api_id, prompt, completion, cached, now))
            conn.commit()

    def reset_stats(self, api_id: str):
        stats = ApiStats(api_id=api_id)
        stats.reset()
        self.save_stats(stats)

    def get_all_stats(self) -> list[ApiStats]:
        with self._db_lock, self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM stats").fetchall()
        return [ApiStats.from_dict(dict(row)) for row in rows]

    # ============ 会话导出 / 导入 ============
    @staticmethod
    def _archive_now() -> str:
        return datetime.now().isoformat()

    def export_session_archive(self, session_id: str) -> Optional[dict]:
        """
        将会话（含消息、引用的角色卡、世界书）打包为可移植的 dict（可 json.dump）。
        返回 None 表示会话不存在。
        """
        session = self.load_session(session_id)
        if not session:
            return None
        messages = self.load_messages(session_id)
        # 引用的角色卡（随存档携带，导入端无需已有同名角色）
        characters: list[dict] = []
        for cid in session.character_ids:
            d = self._load_json(paths.characters_dir(), cid)
            if d:
                characters.append(d)
        # 世界书（可选）
        world_book: Optional[dict] = None
        if session.world_book_id:
            world_book = self._load_json(paths.world_books_dir(), session.world_book_id)
        return {
            "format": "ai-roleplay-session-archive",
            "version": 1,
            "exported_at": self._archive_now(),
            "session": session.to_dict(),
            "messages": [m.to_dict() for m in messages],
            "characters": characters,
            "world_book": world_book,
        }

    def import_session_archive(self, archive: dict) -> Optional[Session]:
        """
        从存档导入一个**新**会话（不复用源会话 id，避免覆盖已有会话）。
        角色卡合并策略（智能合并同名）：
        - 本地已有同 id → 复用该 id，不覆盖本地修改；
        - 本地无同 id 但有同名 → 复用本地同名角色 id（建立映射），避免重复导入；
        - 均无 → 导入存档中的角色卡（保留原 id）。
        世界书同理（同 id 复用 / 同名复用 / 否则导入）。
        会话 character_ids 与消息 character_id 全部按映射改写为本地实际角色 id。
        """
        if not isinstance(archive, dict) or archive.get("format") != "ai-roleplay-session-archive":
            return None
        sess_dict = archive.get("session")
        msgs_dicts = archive.get("messages", []) or []
        char_dicts = archive.get("characters", []) or []
        wb_dict = archive.get("world_book")
        if not sess_dict:
            return None

        # 本地已有角色/世界书索引（名 → id），用于同名合并
        local_char_name_to_id: dict[str, str] = {}
        for d in self._load_all_json(paths.characters_dir()):
            nm = d.get("name")
            if nm:
                local_char_name_to_id[nm] = d.get("id", "")
        local_wb_name_to_id: dict[str, str] = {}
        for d in self._load_all_json(paths.world_books_dir()):
            nm = d.get("name")
            if nm:
                local_wb_name_to_id[nm] = d.get("id", "")

        # 角色卡合并：char_id_map[存档角色id] = 本地实际角色id
        char_id_map: dict[str, str] = {}
        for cd in char_dicts:
            cid = cd.get("id", "")
            cname = cd.get("name", "")
            if cid and self._load_json(paths.characters_dir(), cid) is not None:
                # 同 id 已存在 → 复用
                char_id_map[cid] = cid
            elif cname and cname in local_char_name_to_id:
                # 同名已存在 → 复用本地同名角色
                char_id_map[cid] = local_char_name_to_id[cname]
            elif cid:
                # 均无 → 导入存档角色卡
                self._save_json(paths.characters_dir(), cid, cd)
                char_id_map[cid] = cid

        # 世界书合并：wb_id_map[存档世界书id] = 本地实际世界书id
        wb_id_map: dict[str, str] = {}
        if isinstance(wb_dict, dict) and wb_dict.get("id"):
            wbid = wb_dict["id"]
            wbname = wb_dict.get("name", "")
            if self._load_json(paths.world_books_dir(), wbid) is not None:
                wb_id_map[wbid] = wbid
            elif wbname and wbname in local_wb_name_to_id:
                wb_id_map[wbid] = local_wb_name_to_id[wbname]
            else:
                self._save_json(paths.world_books_dir(), wbid, wb_dict)
                wb_id_map[wbid] = wbid

        # 生成新会话 id 与新消息 id 的映射（summary_of 引用需同步改写）
        new_session_id = str(uuid.uuid4())
        msg_id_map: dict[str, str] = {}
        for m in msgs_dicts:
            old_id = m.get("id")
            if old_id:
                msg_id_map[old_id] = str(uuid.uuid4())

        # 保存会话（替换 id，改写 character_ids / world_book_id 引用，touch 更新时间）
        new_session = Session.from_dict(sess_dict)
        new_session.id = new_session_id
        new_session.character_ids = [char_id_map.get(cid, cid) for cid in new_session.character_ids]
        if new_session.world_book_id:
            new_session.world_book_id = wb_id_map.get(new_session.world_book_id, new_session.world_book_id)
        # [!] default_speaker_id 同样要按 char_id_map 重映射（存档里存的是原角色 id，
        # 导入后角色 id 可能因同名合并而变化）；映射后若不在本地 character_ids 里，
        # send_and_respond 的兜底会回退首个角色，不会崩。
        if new_session.default_speaker_id:
            new_session.default_speaker_id = char_id_map.get(
                new_session.default_speaker_id, new_session.default_speaker_id
            )
        new_session.touch()
        self.save_session(new_session)

        # 保存消息（新 id / 新 session_id，改写 character_id 与 summary_of 引用）
        for m_dict in msgs_dicts:
            msg = Message.from_dict(m_dict)
            old_mid = m_dict.get("id", "")
            msg.id = msg_id_map.get(old_mid, str(uuid.uuid4()))
            msg.session_id = new_session_id
            if msg.character_id:
                msg.character_id = char_id_map.get(msg.character_id, msg.character_id)
            msg.summary_of = [msg_id_map.get(sid, sid) for sid in msg.summary_of]
            self.save_message(msg)

        return new_session