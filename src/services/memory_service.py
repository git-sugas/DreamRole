"""
角色记忆服务：两种模式，均优先用 MemoryPreset.api_id 独立绑定 API（缺失回退角色绑定 API）。
  - summary: 旧记忆 + 上次总结到现在的新增对话 -> 调用总结接口（MemoryPreset.summary_prompt）
    -> 覆盖存成一段文本记忆。触发与取窗统一用 summary_interval（单参数，聊多少梳理多少）。
    增量追踪用 {cid}_summary.json 的 updated_at_msg_count。
  - embedding_hybrid: 两路召回（emb_detail + triggers）+ 两次召回合并 + 纯追加。
    整理用 MemoryPreset.hybrid_system_prompt，输出 `[triggers: ...] 明细` 纯追加入库
    （ChromaDB _hybrid collection + SQLite 两表）。触发与取窗统一用 embedding_interval（单参数）。
    增量追踪用 {cid}_embed_count.json。
  两模式共享 MemoryPreset 的 api_id 与生成参数；提示词各自一份（输出约定不同）。
  [!] 群聊会话级记忆（session.group_memory_interval > 0）：全量消息触发 + 全群共用一个边界
  （{session_id}_group_mem.json）+ 同段对话喂多角色，各角色走自己 memory_mode。单聊/未启用时
  走角色个人配置路径（按该角色 assistant 计数触发）。

记忆按角色全局存储（跨会话），存于 data/memory/ 目录与 data/chroma/ 向量库。
[!] 向量路整句去标签（bge-m3/qwen3-emb 不吃 [Trigger]/[Detail] 标签，标签是噪声词）：
    emb 入库文本 = detail 实际内容（不再拼 [Trigger] triggers [Detail] detail）。
    原因：triggers 是从 detail 提炼的 4 维度词，detail 语义完全覆盖 triggers，
    triggers 进向量是冗余且短词拼进整句稀释 detail 语义密度。
[!] 长文本召回从 FTS5 迁向量：detail 不再走 detail_search bm25（表已删除），改走 emb_detail 整句语义召回。
    短词仍走 bm25 拆词：triggers_search（4 个触发词，精确匹配）保留 FTS5。
"""
from __future__ import annotations
from src.utils.debug import debug_log
import json
import os
import re
from typing import Optional

from src.config import paths
from src.models import (
    Character, Message, Session, ApiConfig, Preset, MemoryPreset,
)
from src.models.memory_preset import (
    DEFAULT_MEMORY_SUMMARY_PROMPT, DEFAULT_MEMORY_SUMMARY_PROMPT_GROUP,
    DEFAULT_MEMORY_HYBRID_PROMPT, DEFAULT_MEMORY_HYBRID_PROMPT_GROUP,
)
from src.services.embedding_client import EmbeddingClient
from src.services.llm_client import LlmClient
from src.services.storage import Storage


# hybrid 整理输出解析：每行 `[triggers: 词1,词2,词3] 明细内容` -> (triggers, detail)
_HYBRID_LINE = re.compile(r"^\[triggers:\s*(.*?)\]\s*(.*)$")


def parse_hybrid_entries(text: str) -> list[tuple[str, str]]:
    """解析 hybrid 整理 API 输出为 [(triggers, detail), ...]。

    格式约定：每行一条，行首 `[triggers: 词1,词2,词3]` 后跟明细内容。
    - 不符合格式的行跳过（容错：LLM 可能输出说明文字/空行）。
    - triggers 保留原始逗号分隔串（入库时再分词），detail 保留原文。
    """
    entries: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _HYBRID_LINE.match(line)
        if not m:
            continue
        triggers = m.group(1).strip()
        detail = m.group(2).strip()
        if triggers and detail:
            entries.append((triggers, detail))
    return entries


class MemoryService:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._chroma_client = None

    def _jb_prefix(self) -> str:
        """读取破限前缀：开关关或 prefix 空返回空串。"""
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    # ============ 路径 ============
    @staticmethod
    def _memory_dir() -> str:
        return paths.get_subdir("memory")

    @staticmethod
    def _summary_path(character_id: str) -> str:
        return os.path.join(MemoryService._memory_dir(), f"{character_id}_summary.json")

    @staticmethod
    def _collection_name(character_id: str) -> str:
        """ChromaDB collection 名（embedding_hybrid 模式专用）：char_{full_uuid}_hybrid。

        [!] 用完整 uuid（去掉旧版 [:12] 截断，防 UUID 前 12 hex 碰撞串角色）。
        清理老 collection（无后缀 char_{uuid[:12]} / 已下线 embedding 模式的 char_{id}_emb）
        在 clear_memory_by_id / _get_chroma 中兼容处理。
        """
        return f"char_{character_id}_hybrid"

    def _embedding_interval(self, character: Character) -> int:
        """embedding_hybrid 模式的整理间隔（每 N 条该角色 assistant 消息整理一次）。默认 1。

        字段名沿用 embedding_interval（历史命名，hybrid 复用，不改名避免老数据迁移）。
        [!] 取窗同 interval（聊多少梳理多少，去掉旧 interval*4 乘数，与 summary 模式口径统一）。
        """
        cfg = character.memory_config or {}
        return max(1, int(cfg.get("embedding_interval", cfg.get("summary_interval", 1)) or 1))

    # [!] _summary_window 已删除：summary 模式取窗统一用 summary_interval（单参数驱动触发+取窗，
    #     与 embedding_hybrid 口径一致）。老角色卡 summary_window 字段忽略不读。

    # ============ ChromaDB ============
    _legacy_cleaned = False  # 类级标志：老 collection 清理只跑一次

    def _get_chroma(self):
        if self._chroma_client is None:
            import chromadb
            self._chroma_client = chromadb.PersistentClient(path=paths.chroma_dir())
            # 一次性迁移：清理非 _hybrid 后缀的老 collection。
            # - 旧版 char_{uuid[:12]}（无后缀）：老数据 schema 不兼容，清理避免占空间。
            # - char_{uuid}_emb（已下线 embedding 模式产生）：embedding 模式已移除，成孤儿。
            # 仅 char_{uuid}_hybrid 是当前 embedding_hybrid 模式在用的，保留。
            # [!] 老带标签 emb 文本的 hybrid collection（去标签改造前的老库）不在此自动清理，
            # 需用户到「记忆管理」页面手动删某角色记忆（clear_memory_by_id 全清）。开发版本不做迁移。
            if not MemoryService._legacy_cleaned:
                MemoryService._legacy_cleaned = True
                try:
                    for col in self._chroma_client.list_collections():
                        # chromadb 1.5.x list_collections 返回 Collection 对象，取 .name
                        name = col.name if hasattr(col, "name") else str(col)
                        # 非当前 _hybrid 命名的 char_* collection 全清
                        if name.startswith("char_") and not name.endswith("_hybrid"):
                            try:
                                self._chroma_client.delete_collection(name)
                            except Exception:
                                pass
                except Exception:
                    pass
        return self._chroma_client

    def _get_collection(self, character_id: str):
        """获取/创建该角色 embedding_hybrid 模式的 ChromaDB collection（char_{id}_hybrid）。"""
        client = self._get_chroma()
        return client.get_or_create_collection(
            name=self._collection_name(character_id),
            metadata={"hnsw:space": "cosine"},
        )

    # ============ 获取记忆文本（注入上下文）============
    def get_memory_text(
            self,
            character: Character,
            query_text: str = "",
            api_config: Optional[ApiConfig] = None,
    ) -> str:
        """获取要注入上下文的记忆文本。"""
        if character.memory_mode == "summary":
            return self._read_summary_memory(character.id)
        elif character.memory_mode == "embedding_hybrid":
            # hybrid 模式由编排器单独调 get_hybrid_memory_text（两次召回合并），
            # 这里 query_text 单参无法表达两次召回，留作兜底单次召回入口。
            if api_config and query_text:
                return self.get_hybrid_memory_text_single(character, api_config, query_text)
            return ""
        return ""

    # ============ Embedding Hybrid 模式：两次召回合并入口（编排器调用）============
    def get_hybrid_memory_text(
            self,
            character: Character,
            api_config: ApiConfig,
            assistant_query: str,
            user_query: str,
            session_type: str = "single",
    ) -> str:
        """hybrid 模式注入用记忆文本：两次召回（上一条 assistant / 本轮 user）合并取 top-K。

        - assistant_query：上一条 assistant 消息内容（首次对话用 character.first_message）
        - user_query：本轮 user 输入（pending_trigger）
        两次各自跑三路融合得带 score 候选，合并：
          merged_score[seq] = user_w * score_user + (1-user_w) * score_assistant
        （某次召回无该 seq 则该项 score 按 0）。取 top-K seq 反查 detail 原文渲染。
        两次召回各调一次 embedding API（共2次，除非 emb 路短路）。
        """
        preset = self.storage.load_memory_preset()
        top_k = max(1, int(getattr(preset, "hybrid_recall_top_k", 15)))
        user_w = float(getattr(preset, "hybrid_user_recall_weight", 0.6))
        weights = tuple(getattr(preset, "hybrid_recall_weights", (0.6, 0.2, 0.2)))

        # 第一次召回：上一条 assistant（或 first_message）
        # 用 _recall_hybrid_detail（而非 _recall_hybrid_scored）拿 detail_text，
        # emb 路命中的 seq 可直接从 metadata 取 detail，省一次 SQLite 反查
        det_assistant: dict[int, dict] = {}
        if assistant_query.strip():
            det_assistant = self._recall_hybrid_detail(
                character, api_config, assistant_query, weights, top_k,
            )
            debug_log(lambda: f"[Memory.hybrid.recall] assistant 召回 {len(det_assistant)} 条")
        # 第二次召回：本轮 user
        det_user: dict[int, dict] = {}
        if user_query.strip():
            det_user = self._recall_hybrid_detail(
                character, api_config, user_query, weights, top_k,
            )
            debug_log(lambda: f"[Memory.hybrid.recall] user 召回 {len(det_user)} 条")

        # 合并：union of seq，加权求和
        all_seqs = set(det_assistant) | set(det_user)
        if not all_seqs:
            debug_log("[Memory.hybrid.recall] 两次召回均空，返回空记忆")
            return ""
        merged: dict[int, float] = {}
        for seq in all_seqs:
            s_a = det_assistant.get(seq, {}).get("score", 0.0)
            s_u = det_user.get(seq, {}).get("score", 0.0)
            # 若只有一次召回有结果（另一次 query 为空），直接用那次分数，不做加权
            if not det_assistant:
                merged[seq] = s_u
            elif not det_user:
                merged[seq] = s_a
            else:
                merged[seq] = user_w * s_u + (1.0 - user_w) * s_a
        # 先按分数降序取 top-K 候选，再按 seq 升序排序展示（便于 LLM 按时间顺序理解新旧关系）
        ranked = sorted(merged.items(), key=lambda x: -x[1])[:top_k]
        debug_log(lambda: f"[Memory.hybrid.recall] 合并后 top {len(ranked)}: "
                  + ", ".join(f"{s}={v:.4f}" for s, v in ranked))
        seqs = sorted(s for s, _ in ranked)   # 展示按 seq 从小到大
        # [!] emb 路命中的 seq 从 metadata 取 detail（打标方案，省反查）；
        # 非 emb 路命中的 seq 仍反查 SQLite 兜底（通常很少甚至为空）
        detail_map: dict[int, str] = {}
        non_emb_seqs: list[int] = []
        for seq in seqs:
            # 两次召回任一次 emb 路命中即有 detail_text
            det = det_assistant.get(seq, {}).get("detail_text") or det_user.get(seq, {}).get("detail_text")
            if det:
                detail_map[seq] = det
            else:
                non_emb_seqs.append(seq)
        if non_emb_seqs:
            detail_map.update(self.storage.fetch_char_memory_details(character.id, non_emb_seqs))
        # 按 seq 升序渲染（便于 LLM 按时间顺序理解新旧关系），每条带 seq 前缀
        lines = []
        for seq in seqs:
            det = detail_map.get(seq)
            if det:
                lines.append(f"[{seq}] {det}")
        return "\n".join(lines)

    def get_hybrid_memory_text_single(
            self, character: Character, api_config: ApiConfig, query_text: str,
    ) -> str:
        """hybrid 模式单次召回兜底入口（get_memory_text 走的路径，无两次召回）。

        用于编排器未走两次召回的兜底场景（如测试/其他调用路径）。取 top-K 渲染。
        """
        preset = self.storage.load_memory_preset()
        top_k = max(1, int(getattr(preset, "hybrid_recall_top_k", 15)))
        weights = tuple(getattr(preset, "hybrid_recall_weights", (0.6, 0.2, 0.2)))
        # 用 _recall_hybrid_detail 拿 detail_text，emb 路命中的 seq 直接从 metadata 取（省反查）
        det_map = self._recall_hybrid_detail(character, api_config, query_text, weights, top_k)
        if not det_map:
            return ""
        # 先按分数取 top-K，再按 seq 升序展示（与 get_hybrid_memory_text 口径一致）
        ranked = sorted(det_map.items(), key=lambda x: -x[1]["score"])[:top_k]
        seqs = sorted(s for s, _ in ranked)
        # emb 路命中的从 detail_text 取，非 emb 路命中的反查兜底
        detail_map: dict[int, str] = {}
        non_emb_seqs: list[int] = []
        for seq in seqs:
            det = det_map.get(seq, {}).get("detail_text")
            if det:
                detail_map[seq] = det
            else:
                non_emb_seqs.append(seq)
        if non_emb_seqs:
            detail_map.update(self.storage.fetch_char_memory_details(character.id, non_emb_seqs))
        lines = []
        for seq in seqs:
            det = detail_map.get(seq)
            if det:
                lines.append(f"[{seq}] {det}")
        return "\n".join(lines)

    def _recall_hybrid_scored(
            self,
            character: Character,
            api_config: ApiConfig,
            query_text: str,
            weights: tuple,
            top_n: int,
    ) -> dict[int, float]:
        """hybrid 单次三路融合召回，返回 {seq: score}（已融合，未截断到 top_n）。

        薄包装：调 _recall_hybrid_detail 取明细后只返回 score 字段。
        [!] 打标方案后正式注入路径改用 _recall_hybrid_detail 以拿 detail_text（emb 路命中
        的 seq 从 metadata 直接取 detail，省反查）。此方法保留供未来只需 score 的场景。
        """
        detail = self._recall_hybrid_detail(character, api_config, query_text, weights, top_n)
        return {seq: info["score"] for seq, info in detail.items()}

    def _recall_hybrid_detail(
            self,
            character: Character,
            api_config: ApiConfig,
            query_text: str,
            weights: tuple,
            top_n: int,
    ) -> dict[int, dict]:
        """hybrid 单次两路融合召回，返回 {seq: {s_emb, s_trig, s_seq, score, src}}。

        [!] 向量路整句去标签：emb 文本 = detail 实际内容（不再拼 [Trigger] triggers [Detail] detail）。
        两路：emb_detail（ChromaDB 语义，整句 detail）+ triggers（FTS5 bm25，4 个触发词）。
        [!] detail FTS5 路已删除（detail 长文本召回从 bm25 迁向量 emb_detail，整句语义优于拆词）。
        score = w_emb·emb_sim + w_trig·trig_sim + w_seq·seq_norm
        各路按 seq 去重；trig bm25 绝对值 min-max 归一化；
        seq 在候选集内 min-max 归一化（新记忆略优先）；候选集仅 1 个 seq 时 seq_norm=1.0。
        emb 路无 embedding_model 时短路（仅 trig+seq 两路，s_emb 恒 0）。
        src 标注该 seq 命中了哪几路（如 "emb+trig"），供测试区展示。
        """
        w_emb, w_trig, w_seq = weights
        cid = character.id

        # ===== 路 A：embedding 语义召回（按 seq 去重取最高 sim）=====
        emb_scores: dict[int, float] = {}
        emb_meta_map: dict[int, dict] = {}   # seq -> {detail, triggers}（emb 路命中时从 metadata 直接取，省反查）
        emb_api_ok = bool(getattr(api_config, "embedding_model", ""))
        if emb_api_ok:
            try:
                collection = self._get_collection(cid)
                count = collection.count()
            except Exception:
                count = 0
            if count > 0:
                try:
                    emb = EmbeddingClient(api_config).embed(query_text)
                except Exception as e:
                    debug_log(lambda: f"[Memory.hybrid.recall] emb 调用失败: {e}")
                    emb = None
                if emb is not None:
                    fetch_n = min(top_n * 3, count)
                    try:
                        results = collection.query(
                            query_embeddings=[emb],
                            n_results=fetch_n,
                            include=["metadatas", "distances"],
                        )
                    except Exception as e:
                        debug_log(lambda: f"[Memory.hybrid.recall] emb query 失败: {e}")
                        results = None
                    if results:
                        metas = results.get("metadatas", [[]])[0]
                        dists = results.get("distances", [[]])[0]
                        for i, m in enumerate(metas):
                            if not m:
                                continue
                            dist = float(dists[i]) if i < len(dists) else 1.0
                            sim = max(0.0, 1.0 - dist)
                            try:
                                seq = int(m.get("seq", 0))
                            except (ValueError, TypeError):
                                continue
                            if seq <= 0:
                                continue
                            if seq not in emb_scores or sim > emb_scores[seq]:
                                emb_scores[seq] = sim
                                # metadata 已存全量明细，召回时直接收集 detail/triggers，
                                # 后续渲染不用再反查 SQLite 主表（非 emb 路命中 seq 仍需反查兜底）
                                emb_meta_map[seq] = {
                                    "detail": m.get("detail", ""),
                                    "triggers": m.get("triggers", ""),
                                }
                    debug_log(lambda: f"[Memory.hybrid.recall] 路 A emb: {len(emb_scores)} seq")
        else:
            debug_log("[Memory.hybrid.recall] 路 A emb: 无 embedding_model，短路")

        # ===== 路 B：triggers 路 FTS5 召回（独立表 bm25）=====
        trig_rows = self.storage.query_char_mem_fts_triggers(query_text, cid, top_n)
        trig_scores: dict[int, float] = {}
        max_trig = max((abs(r["s"]) for r in trig_rows), default=0.0) or 1.0
        for r in trig_rows:
            try:
                seq = int(r["seq"])
            except (ValueError, TypeError):
                continue
            trig_scores[seq] = abs(r["s"]) / max_trig
        debug_log(lambda: f"[Memory.hybrid.recall] 路 B trig: {len(trig_scores)} seq")

        # [!] detail FTS5 路（路 C）已删除：detail 长文本召回改走 emb_detail 向量整句语义召回。

        # ===== 融合：union of seq，返回明细 =====
        all_seqs = set(emb_scores) | set(trig_scores)
        if not all_seqs:
            return {}
        # [!] emb 路 sim 也做候选集内 min-max 归一化（与 trig 口径一致），
        # 避免两路同权相加时量级不可比（emb 绝对值 vs trig 的 abs/max）。
        if emb_scores:
            max_emb = max(emb_scores.values()) or 1.0
            min_emb = min(emb_scores.values())
            emb_range = (max_emb - min_emb) or 1.0
            emb_scores = {k: (v - min_emb) / emb_range for k, v in emb_scores.items()}
        # [!] emb 路短路（无 embedding_model 或召回为空）时，剩余两路权重重归一化，
        # 避免退化为「半功率」召回（score 上限骤降导致 top-K 截断阈值偏低）。
        if not emb_scores and (w_trig + w_seq) > 0:
            total = w_trig + w_seq
            w_emb, w_trig, w_seq = 0.0, w_trig / total, w_seq / total
        seq_min = min(all_seqs)
        seq_max = max(all_seqs)
        seq_range = (seq_max - seq_min) or 1   # 候选集仅 1 个 seq 时回退 1 防除零
        detail: dict[int, dict] = {}
        for seq in all_seqs:
            s_emb = emb_scores.get(seq, 0.0)
            s_trig = trig_scores.get(seq, 0.0)
            s_seq = (seq - seq_min) / seq_range
            score = w_emb * s_emb + w_trig * s_trig + w_seq * s_seq
            # src 标注命中路
            parts = []
            if seq in emb_scores:
                parts.append("emb")
            if seq in trig_scores:
                parts.append("trig")
            detail[seq] = {
                "s_emb": s_emb, "s_trig": s_trig,
                "s_seq": s_seq, "score": score, "src": "+".join(parts) or "none",
                # emb 路命中的 seq 从 metadata 直接取 detail/triggers 原文（省反查）；
                # 非 emb 路命中的 seq 这两字段为空，由调用方反查 SQLite 兜底。
                "detail_text": emb_meta_map.get(seq, {}).get("detail", ""),
                "triggers_text": emb_meta_map.get(seq, {}).get("triggers", ""),
            }
        debug_log(lambda: f"[Memory.hybrid.recall] 融合 {len(detail)} seq，top5: "
                  + ", ".join(f"{s}={v['score']:.4f}" for s, v in sorted(detail.items(), key=lambda x: -x[1]['score'])[:5]))
        return detail

    def recall_hybrid_with_detail(
            self,
            character: Character,
            api_config: ApiConfig,
            assistant_query: str,
            user_query: str,
            session_type: str = "single",
    ) -> list[dict]:
        """两次召回合并，返回带明细的结果（供测试区用，不影响正式注入）。

        返回 [{seq, triggers, detail, s_emb, s_trig, s_seq,
               merged_score, src_assistant, src_user}], 按 merged_score 降序。
        s_emb/s_trig/s_seq 取两次召回中该 seq 的最大子分（展示用，看哪路强）；
        src_assistant/src_user 分别标两次召回各自的命中路；merged_score 是加权合并分。
        """
        preset = self.storage.load_memory_preset()
        top_k = max(1, int(getattr(preset, "hybrid_recall_top_k", 15)))
        user_w = float(getattr(preset, "hybrid_user_recall_weight", 0.6))
        weights = tuple(getattr(preset, "hybrid_recall_weights", (0.6, 0.2, 0.2)))

        # 两次召回明细
        det_a: dict[int, dict] = {}
        if assistant_query.strip():
            det_a = self._recall_hybrid_detail(character, api_config, assistant_query, weights, top_k)
        det_u: dict[int, dict] = {}
        if user_query.strip():
            det_u = self._recall_hybrid_detail(character, api_config, user_query, weights, top_k)

        all_seqs = set(det_a) | set(det_u)
        if not all_seqs:
            return []

        # 合并：merged_score 加权；子分取两次中最大（展示哪路强）
        merged: dict[int, float] = {}
        for seq in all_seqs:
            ia = det_a.get(seq)
            iu = det_u.get(seq)
            score_a = ia["score"] if ia else 0.0
            score_u = iu["score"] if iu else 0.0
            if not det_a:
                merged[seq] = score_u
            elif not det_u:
                merged[seq] = score_a
            else:
                merged[seq] = user_w * score_u + (1.0 - user_w) * score_a

        # 取 top-K 后查 triggers/detail 原文。
        # [!] emb 路命中的 seq 从 metadata 的 detail_text/triggers_text 直接取（打标方案省反查）；
        # 非 emb 路命中的 seq（仅 trig/detail FTS5 路命中）仍反查 SQLite 兜底。
        ranked = sorted(merged.items(), key=lambda x: -x[1])[:top_k]
        seqs = [s for s, _ in ranked]
        detail_map: dict[int, str] = {}
        trig_map: dict[int, str] = {}
        non_emb_seqs: list[int] = []
        for seq in seqs:
            ia = det_a.get(seq, {})
            iu = det_u.get(seq, {})
            det = ia.get("detail_text") or iu.get("detail_text")
            trig = ia.get("triggers_text") or iu.get("triggers_text")
            if det:
                detail_map[seq] = det
            if trig:
                trig_map[seq] = trig
            # detail 或 triggers 任一缺失（非 emb 路命中）都需反查兜底
            if not det or not trig:
                non_emb_seqs.append(seq)
        if non_emb_seqs:
            # fetch_char_memory_details 返回 detail；triggers 需 fetch_all_char_memory_entries
            detail_map.update(self.storage.fetch_char_memory_details(character.id, non_emb_seqs))
            entries = self.storage.fetch_all_char_memory_entries(character.id)
            for e in entries:
                s = int(e["seq"])
                if s in set(non_emb_seqs) and s not in trig_map:
                    trig_map[s] = e["triggers"]

        result = []
        for seq, mscore in ranked:
            ia = det_a.get(seq, {})
            iu = det_u.get(seq, {})
            result.append({
                "seq": seq,
                "triggers": trig_map.get(seq, ""),
                "detail": detail_map.get(seq, ""),
                "s_emb": max(ia.get("s_emb", 0.0), iu.get("s_emb", 0.0)),
                "s_trig": max(ia.get("s_trig", 0.0), iu.get("s_trig", 0.0)),
                "s_seq": max(ia.get("s_seq", 0.0), iu.get("s_seq", 0.0)),
                "merged_score": mscore,
                "src_assistant": ia.get("src", ""),
                "src_user": iu.get("src", ""),
            })
        return result

    # ============ 两栏展示用公共读取 ============
    def get_summary_text(self, character_id: str) -> str:
        """读取 summary 模式的当前记忆文本（供记忆页两栏展示用，与 mode 无关）。"""
        return self._read_summary_memory(character_id)

    def get_hybrid_entries_text(self, character_id: str) -> str:
        """读取 hybrid 模式全部记忆条目，按 seq 升序渲染成文本（供记忆页展示用，与 mode 无关）。

        格式：每行 `[seq] 触发词:... | 明细`，便于查看条目内容与新旧顺序。
        """
        entries = self.storage.fetch_all_char_memory_entries(character_id)
        if not entries:
            return ""
        return "\n".join(
            f"[{e['seq']}] 触发词:{e['triggers']} | {e['detail']}"
            for e in entries
        )

    def get_hybrid_entry_count(self, character_id: str) -> int:
        """读取 hybrid 模式记忆条目数（与 mode 无关，异常返回 0）。"""
        return self.storage.count_char_memory_entries(character_id)

    def get_summary_msg_count(self, character_id: str) -> int:
        """读取 summary 记忆对应的「已总结到第 N 条」计数（与 mode 无关）。"""
        return self._read_summary_count(character_id)

    # ============ Summary 模式 ============
    def _read_summary_memory(self, character_id: str) -> str:
        path = self._summary_path(character_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("memory", "")
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    def _write_summary_memory_session(
        self, character_id: str, session_id: str, memory: str, msg_count: int,
    ):
        """写 summary 记忆内容（全局）+ 更新该会话的计数（会话级）。

        [!] 读改写整段包在 _json_write_lock 内（§12 契约）：记忆整理（ChatWorker）与
        clear_memory（主线程）并发写同一角色 summary 文件时，读旧 counts 若不在锁内
        会被另一线程的写覆盖，导致其他会话的计数丢失。
        """
        path = self._summary_path(character_id)
        with Storage._json_write_lock:
            old = {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f) or {}
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            counts = old.get("updated_at_msg_count") if isinstance(old.get("updated_at_msg_count"), dict) else {}
            counts[session_id] = msg_count
            data = {"memory": memory, "updated_at_msg_count": counts}
            Storage._save_json_atomic(path, data)

    def _read_summary_count(self, character_id: str, session_id: str = "") -> int:
        """读取该角色在指定会话的 summary 已整理计数。
        session_id 为空（老调用方）时返回任意一个会话的计数（向后兼容，不再使用）。"""
        path = self._summary_path(character_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return 0
        counts = data.get("updated_at_msg_count")
        if isinstance(counts, dict):
            if session_id:
                return int(counts.get(session_id, 0))
            # 老调用方无 session_id：取最大值兜底（不应再出现）
            return max((int(v) for v in counts.values()), default=0)
        # 老格式（int）：原值，但已错位，调用方应尽快迁移
        return int(counts) if counts is not None else 0

    def check_and_update_summary(
            self,
            character: Character,
            session: Session,
            messages: list[Message],
            api_config: ApiConfig,
            cancel_check=None,
    ) -> bool:
        """
        检查是否需要更新总结记忆，需要则更新。
        返回是否执行了更新。

        [!] 增量边界按会话级：last_count 按 (character_id, session_id) 持久化，
        与 char_msgs（本会话该角色 assistant 计数）同口径，跨会话不错位。
        记忆内容仍按角色全局存储（read.md §9）。
        cancel_check 透传给 llm.chat_cancelable，支持停止生成时中断总结。
        """
        if character.memory_mode != "summary":
            return False

        interval = character.memory_config.get("summary_interval", 20)
        sid = getattr(session, "id", "") or ""
        last_count = self._read_summary_count(character.id, sid)

        # 只统计该角色的消息。注意：不排除已折叠消息 -- 上文总结会折叠消息，
        # 若排除折叠消息，被折叠的消息既不进历史也不被记忆计数，会导致记忆增量
        # 边界错乱、漏整理（折叠一批后序号基准漂移，永远数不到 interval）。
        # 已折叠消息原文仍在 DB（summary 只标 collapsed=True 不删除），纳入计数
        # 保证序号稳定；取数窗口可能含已折叠原文，但 summary（对话压缩）与 memory
        # （角色沉淀）目的不同，重叠可接受甚至有益于早期重要事实沉淀。
        char_msgs = [
            m for m in messages
            if m.character_id == character.id and m.role == "assistant"
               and not m.is_image_only
        ]
        if len(char_msgs) - last_count < interval:
            return False

        # 取上次更新后的新消息
        new_msgs = char_msgs[last_count:]
        if not new_msgs:
            return False
        # [!] 取窗统一用 interval（单参数驱动触发+取窗，与 embedding_hybrid 口径一致；
        #     聊多少梳理多少，不再有独立 summary_window）。
        conversation = "\n".join(
            f"{m.character_name}：{m.content}" for m in new_msgs[-interval:]
        )
        # 走核心：旧记忆 + conversation -> LLM -> 覆盖存
        ok, memory_text = self._consolidate_summary_with_segment(
            character, conversation, session, api_config, cancel_check=cancel_check,
        )
        if not ok:
            return False
        # 成功推进该角色在本会话的计数（与 hybrid 口径一致：成功才推进）。
        # [!] 用核心方法返回的 memory_text 直接传，避免无锁回读正文（消除竞态窗口）。
        self._write_summary_memory_session(character.id, sid, memory_text, len(char_msgs))
        return True

    def _consolidate_summary_with_segment(
            self,
            character: Character,
            conversation: str,
            session: Session,
            api_config: ApiConfig,
            cancel_check=None,
    ) -> tuple[bool, str]:
        """summary 整理核心：当前记忆 + conversation -> LLM -> 覆盖存 {cid}_summary.json。

        单聊（check_and_update_summary 取窗后）与群聊（check_and_update_group_memory 传入全量段）
        共用此核心。返回 (是否成功, 整理后记忆文本)。失败/取消/空返回 (False, "")。
        """
        current_memory = self._read_summary_memory(character.id)
        # 独立总结接口：与 embedding 整理共享 MemoryPreset（同一子页面配置，
        # 两模式共用 api_id 与温度/max_tokens/top_p；提示词各自一份：
        # summary 用 summary_prompt，embedding 用 system_prompt）。
        # 缺失 api_id 回退角色绑定 API。
        mem_preset = self.storage.load_memory_preset()
        summary_api = api_config
        if mem_preset.api_id:
            preset_api = self.storage.load_api(mem_preset.api_id)
            if preset_api and preset_api.enabled:
                summary_api = preset_api
        # 提示词从 MemoryPreset.summary_prompt 读取（可在「API 与预设 -> 记忆整理」页编辑）；
        # 缺失/为空回退内置 DEFAULT_MEMORY_SUMMARY_PROMPT，保证行为不劣化。
        # 按 session_type 选单/群聊总结提示词；为空回退对应默认版
        if getattr(session, "session_type", "single") == "group":
            summary_prompt = mem_preset.summary_prompt_group or DEFAULT_MEMORY_SUMMARY_PROMPT_GROUP
        else:
            summary_prompt = mem_preset.summary_prompt or DEFAULT_MEMORY_SUMMARY_PROMPT
        # [!] {{char_name}} 占位符替换为当前角色名（与 hybrid 整理同理，消除泛义歧义）
        summary_prompt = summary_prompt.replace("{{char_name}}", character.name)

        user_prompt = (
            f"角色名：{character.name}\n\n"
            f"当前记忆：\n{current_memory or '（暂无记忆）'}\n\n"
            f"新对话内容：\n{conversation or '（无）'}\n\n"
            "请输出整合后的最新角色记忆。"
        )
        # 复用 MemoryPreset 的生成参数（温度/max_tokens/top_p），提示词用 summary_prompt
        gen_preset = Preset(
            name="memory_summary",
            system_prompt=summary_prompt,
            temperature=mem_preset.temperature,
            max_tokens=mem_preset.max_tokens,
            top_p=mem_preset.top_p,
        )
        llm = LlmClient(summary_api, gen_preset, jailbreak_prefix=self._jb_prefix())
        debug_log(lambda: f"[Memory.summary] 角色={character.name} API={summary_api.name}({summary_api.model})")
        debug_log(lambda: f"[Memory.summary] 入参 当前记忆:\n{current_memory or '（暂无）'}")
        debug_log(lambda: f"[Memory.summary] 入参 新对话:\n{conversation or '（无）'}")
        # 用 chat_cancelable 透传 cancel_check，支持停止生成时中断总结调用
        result = llm.chat_cancelable([
            {"role": "system", "content": summary_prompt},
            {"role": "user", "content": user_prompt},
        ], cancel_check=cancel_check)
        debug_log(lambda: f"[Memory.summary] 出参:\n{result.content or '（空/失败）'}")

        # 取消或失败时不写记忆（调用方决定是否推进计数）
        if result.cancelled or result.error or not result.content:
            return False, ""
        memory_text = result.content.strip()
        # 覆盖存（全局记忆，按角色存储，不动会话级计数--计数由调用方按成功与否推进）
        self._write_summary_memory_global(character.id, memory_text)
        return True, memory_text

    @staticmethod
    def _write_summary_memory_global(character_id: str, memory: str):
        """仅写 summary 记忆正文（全局，按角色存储），不触碰会话级计数。

        与 _write_summary_memory_session（写正文+更新该会话计数）拆分：
        群聊批量整理时多个角色共用一个全量边界，各角色的会话级计数语义不同
        （群聊边界在 session 级 _group_mem.json，不在此处的 {cid}_summary.json），
        故整理核心只写正文，计数推进由调用方决定。
        """
        path = MemoryService._summary_path(character_id)
        with Storage._json_write_lock:
            old = {}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f) or {}
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            counts = old.get("updated_at_msg_count") if isinstance(old.get("updated_at_msg_count"), dict) else {}
            data = {"memory": memory, "updated_at_msg_count": counts}
            Storage._save_json_atomic(path, data)

    @staticmethod
    def _count_char_assistant_msgs(messages: list[Message], character_id: str) -> int:
        """统计 recent_messages 中该角色的 assistant 消息条数（不排除折叠）。

        口径与 summary 模式 char_msgs 计数一致，用于增量追踪。
        不排除折叠消息以避免上文总结折叠后序号基准漂移导致漏整理。
        """
        return sum(
            1 for m in messages
            if m.character_id == character_id and m.role == "assistant"
               and not m.is_image_only
        )

    # ============ Embedding Hybrid 模式（纯追加 + 三路召回）============
    def check_and_update_hybrid(
            self,
            character: Character,
            message: Message,
            api_config: ApiConfig,
            recent_messages: list[Message] | None = None,
            session_type: str = "single",
            session_id: str = "",
            cancel_check=None,
    ):
        """hybrid 模式记忆整理入库（纯追加，不清旧条目）。

        与旧 embedding 模式「整理式覆盖」不同：hybrid 每 N 条该角色 assistant 消息触发一次整理，
        调 LLM 产出新增条目（看旧记忆去重，冲突事实产出新条目），纯追加入三处存储
        （char_memory_entry + triggers FTS5 表 + ChromaDB），每条新 seq 单调递增。
        不删除旧条目，靠 seq 递增 + 提示词告诉 LLM 大 seq 为准。

        触发与增量追踪：embedding_interval 控制频率（默认1），last_msg_index 增量取
        「上次整理到现在的新对话」原文。整理失败不影响已有条目。

        [!] 增量边界按会话级；整理失败/取消不推进 last_msg_index。
        """
        if character.memory_mode != "embedding_hybrid":
            return
        if message.is_image_only or message.role != "assistant":
            return

        interval = self._embedding_interval(character)
        # [!] 触发判断用「消息序号差值」而非「调用次数」：current_index 实时统计该角色
        # assistant 消息数，last_index 是上次整理到的序号。差值 >= interval 才触发。
        # 旧实现用「调用次数 count % interval」：重试/删除会让 count 涨但消息数不涨
        # （重试删1条加1条，current_index 不变），导致 count 到点但实际只有1条消息，
        # 重复整理同一条消息。改用差值后，重试5次实际1条消息 -> 差值1 < interval 不触发，
        # 与 summary 模式口径一致。count 字段保留仅用于日志/兼容老数据，不再驱动触发。
        msgs = list(recent_messages or [message])
        current_index = self._count_char_assistant_msgs(msgs, character.id)
        last_index = self._read_last_msg_index(character.id, session_id)

        if current_index - last_index < interval:
            # 未到整理点，跳过 API 调用；last_index 保持旧值（不推进，下次重试同段）
            return

        # 到整理点：执行纯追加整理；失败/取消时不推进 last_index
        ok = self._consolidate_hybrid(
            character, message, api_config, msgs, last_index, current_index, session_type,
            cancel_check=cancel_check,
        )
        # [!] 成功才推进 last_index（失败/取消保留旧值，下次重试同段，不丢不漏）。
        # count 字段仍写（兼容老代码读取 + 日志），但不再驱动触发判断。
        new_count = self._read_consolidate_count(character.id, session_id) + 1
        self._write_consolidate_count(
            character.id, session_id, new_count,
            last_msg_index=(current_index if ok else last_index),
        )

    def _consolidate_hybrid(
            self,
            character: Character,
            trigger_msg: Message,
            api_config: ApiConfig,
            messages: list[Message],
            last_index: int,
            current_index: int,
            session_type: str = "single",
            cancel_check=None,
    ) -> bool:
        """执行一次 hybrid 记忆整理（单聊/角色个人配置路径）：旧记忆 + 增量对话 -> LLM -> 纯追加。

        增量对话由 (last_index, current_index) 按该角色 assistant 序号定位（含中间全员发言），
        再交 _consolidate_hybrid_with_segment 走「旧记忆 + new_conv -> LLM -> 纯追加」核心。
        """
        try:
            new_conv = self._build_incremental_conversation(
                messages, character.id, last_index, current_index,
                self._embedding_interval(character),
            )
            return self._consolidate_hybrid_with_segment(
                character, new_conv, current_index, api_config, session_type,
                cancel_check=cancel_check,
            )
        except Exception as e:
            debug_log(lambda: f"[Memory.hybrid.consolidate] 整理失败（角色 {character.name}）: {e}")
            return False

    def _consolidate_hybrid_with_segment(
            self,
            character: Character,
            new_conversation: str,
            created_msg_index: int,
            api_config: ApiConfig,
            session_type: str = "single",
            cancel_check=None,
    ) -> bool:
        """hybrid 整理核心：旧记忆(triggers+detail原文) + new_conversation -> LLM 产出新增条目 -> 纯追加入库。

        单聊（_consolidate_hybrid 增量定位后）与群聊（check_and_update_group_memory 传入全量段）
        共用此核心。返回 True 表示整理成功，False 表示失败/取消/空。
        """
        try:
            # 1. 读取已有全部条目（喂 LLM 去重用，按 seq 升序）
            old_entries = self.storage.fetch_all_char_memory_entries(character.id)
            old_memory_text = "\n".join(
                f"[seq:{e['seq']}] 触发词:{e['triggers']} | {e['detail']}"
                for e in old_entries
            )
            debug_log(lambda: f"[Memory.hybrid.consolidate] 角色={character.name} 已有条目 {len(old_entries)}")

            # 2. 调整理 API（优先 MemoryPreset 绑定 API，回退角色绑定 API）
            preset = self.storage.load_memory_preset()
            cons_api = api_config
            if preset.api_id:
                preset_api = self.storage.load_api(preset.api_id)
                if preset_api and preset_api.enabled:
                    cons_api = preset_api
            debug_log(lambda: f"[Memory.hybrid.consolidate] API={cons_api.name}({cons_api.model})")
            debug_log(lambda: f"[Memory.hybrid.consolidate] 入参 旧记忆:\n{old_memory_text or '（暂无）'}")
            debug_log(lambda: f"[Memory.hybrid.consolidate] 入参 新对话:\n{new_conversation or '（无）'}")
            consolidated, cancelled = self._call_hybrid_consolidate_api(
                character, old_memory_text, new_conversation, cons_api, preset, session_type,
                cancel_check=cancel_check,
            )
            if cancelled:
                debug_log(f"[Memory.hybrid.consolidate] 角色 {character.name} 整理被取消")
                return False
            debug_log(lambda: f"[Memory.hybrid.consolidate] 出参:\n{consolidated or '（空/失败）'}")
            if not consolidated:
                return False

            # 3. 解析为 (triggers, detail) 列表 -> 纯追加入库
            entries = parse_hybrid_entries(consolidated)
            if not entries:
                debug_log("[Memory.hybrid.consolidate] 解析出条目为空，跳过入库")
                return False
            debug_log(lambda: f"[Memory.hybrid.consolidate] 解析条目 {len(entries)} 条，纯追加入库")
            self._append_hybrid_entries(character.id, entries, created_msg_index, cons_api)
            return True
        except Exception as e:
            debug_log(lambda: f"[Memory.hybrid.consolidate] 整理失败（角色 {character.name}）: {e}")
            return False

    def _call_hybrid_consolidate_api(
            self,
            character: Character,
            old_memory: str,
            new_conversation: str,
            api_config: ApiConfig,
            preset: MemoryPreset,
            session_type: str = "single",
            cancel_check=None,
    ) -> tuple[str, bool]:
        """调用 hybrid 记忆整理 API，返回 (整理后文本, 是否取消)。
        失败/空返回 ("", False)，取消返回 ("", True)。"""
        # 按 session_type 选单/群聊整理提示词；为空回退对应默认版
        if session_type == "group":
            sys_prompt = preset.hybrid_system_prompt_group or DEFAULT_MEMORY_HYBRID_PROMPT_GROUP
        else:
            sys_prompt = preset.hybrid_system_prompt or DEFAULT_MEMORY_HYBRID_PROMPT
        # [!] {{char_name}} 占位符替换为当前角色名：提示词用【{{char_name}}】明确点名
        # 要整理谁的记忆，消除「该角色/本角色」的泛义歧义（单聊里若出现其他角色，
        # LLM 能据此判断只整理当前角色的事）。
        sys_prompt = sys_prompt.replace("{{char_name}}", character.name)
        user_prompt = (
            f"角色名：{character.name}\n\n"
            f"当前已有记忆（供你参考避免重复，序号越大越新，冲突时以大序号为准）：\n"
            f"{old_memory or '（暂无记忆）'}\n\n"
            f"新对话内容：\n{new_conversation or '（无）'}\n\n"
            "请输出新增的记忆条目（不要重复已有事实，只产出真正新增或有变化的事实）。"
        )
        mem_preset = Preset(
            name="memory_hybrid_consolidate",
            system_prompt=sys_prompt,
            temperature=preset.temperature,
            max_tokens=preset.max_tokens,
            top_p=preset.top_p,
        )
        llm = LlmClient(api_config, mem_preset, jailbreak_prefix=self._jb_prefix())
        # 用 chat_cancelable 透传 cancel_check，支持停止生成时中断整理
        result = llm.chat_cancelable([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ], cancel_check=cancel_check)
        if result.cancelled:
            return "", True
        if result.error or not result.content:
            return "", False
        return result.content.strip(), False

    def _append_hybrid_entries(
            self,
            character_id: str,
            entries: list[tuple[str, str]],
            created_msg_index: int,
            api_config: ApiConfig,
    ):
        """把解析出的 (triggers, detail) 条目纯追加入三处存储。

        每条 seq = 当前 MAX(seq)+1 递增；emb 路仅当 api 有 embedding_model 时入库
        （无 embedding_model 时跳过 emb 路，仍写主表 + triggers FTS5 表，召回时 emb 路短路）。
        """
        if not entries:
            return
        # 过滤无效条目（triggers 或 detail 为空）
        valid = [(t.strip(), d.strip()) for t, d in entries if t.strip() and d.strip()]
        if not valid:
            return
        base_seq = self.storage.max_char_memory_seq(character_id)
        seq = base_seq
        emb_client = None
        collection = None
        if getattr(api_config, "embedding_model", ""):
            try:
                collection = self._get_collection(character_id)
                emb_client = EmbeddingClient(api_config)
            except Exception as e:
                debug_log(lambda: f"[Memory.hybrid.append] emb 路初始化失败，仅入库 FTS5: {e}")
                collection = None
        for triggers, detail in valid:
            seq += 1
            # 写主表 + triggers FTS5 表（detail FTS5 表已删除，detail 改走向量）
            self.storage.insert_char_memory(character_id, seq, triggers, detail, created_msg_index)
            # emb 路：整句去标签（bge-m3/qwen3-emb 不吃 [Trigger]/[Detail] 标签），emb 文本 = detail 实际内容。
            # triggers 不进向量：triggers 是从 detail 提炼的 4 维度词，detail 语义完全覆盖 triggers，
            # 拼进整句是冗余且短词稀释 detail 语义密度。metadata 仍存 seq/character_id/triggers/detail。
            if collection is not None and emb_client is not None:
                emb_text = detail
                emb = emb_client.embed(emb_text)
                if emb is not None:
                    import time
                    ts = time.time()
                    collection.add(
                        ids=[f"hmem_{seq}_{ts}"],
                        embeddings=[emb],
                        documents=[emb_text],
                        metadatas=[{
                            "seq": seq, "character_id": character_id,
                            "triggers": triggers, "detail": detail,
                        }],
                    )
        debug_log(lambda: f"[Memory.hybrid.append] 入库 {len(valid)} 条，seq {base_seq+1}-{seq}")

    @staticmethod
    def _build_incremental_conversation(
            messages: list[Message],
            character_id: str,
            last_index: int,
            current_index: int,
            interval: int,
    ) -> str:
        """构建「上次整理到现在新增的对话」原文（含用户与各角色发言）。

        在 messages 中按该角色 assistant 未折叠消息计序，取第 last_index+1 条该角色
        assistant 到第 current_index 条之间的所有活跃对话（含中间用户/其他角色发言）。
        last_index=0 时取从首条到 current_index 全段（首次即全量，行为不劣化）。
        给一个上限窗口 interval*4，防止单次整理喂入过长对话。
        """
        # 先定位第 (last_index+1) 条该角色 assistant 在 messages 里的索引位置
        nth = last_index + 1  # 起始是第几条该角色 assistant（1-based）
        start_pos = None
        seen = 0
        for i, m in enumerate(messages):
            if (m.character_id == character_id and m.role == "assistant"
                    and not m.is_image_only):
                seen += 1
                if seen == nth:
                    start_pos = i
                    break
        if start_pos is None:
            # 边界：上次序号已超过当前累计，无明显增量可取，回退空
            return ""
        # 结束位置：第 current_index 条该角色 assistant 之后（含本轮）
        end_pos = None
        seen = 0
        for i, m in enumerate(messages):
            if (m.character_id == character_id and m.role == "assistant"
                    and not m.is_image_only):
                seen += 1
                if seen == current_index:
                    end_pos = i + 1  # 含本条
                    break
        if end_pos is None:
            end_pos = len(messages)
        # [!] 上限窗口 = interval（聊多少梳理多少，与 summary 模式口径统一；
        # 去掉旧 interval*4 乘数：触发间隔即取窗上限，不再多取）。
        cap = max(1, interval)
        if end_pos - start_pos > cap:
            start_pos = end_pos - cap
        parts = []
        for m in messages[start_pos:end_pos]:
            if m.is_image_only or m.is_summary:
                continue
            speaker = m.character_name or "用户"
            parts.append(f"{speaker}：{m.content}")
        return "\n".join(parts)

    # 整理计数与增量边界持久化（用 summary 同目录下的单独文件，复用路径约定）
    # [!] 会话级增量边界：文件存 dict {session_id: {"count": N, "last_msg_index": M}}。
    # 记忆按角色全局存储（read.md §9），但增量边界按会话级追踪：
    # 同一角色在不同会话聊不同场景，会话 B 是新场景，从第 1 条 assistant 开始整理，
    # 不应受会话 A 留下的 last_msg_index 影响（原方案按 character_id 全局持久化
    # last_msg_index，会话 B 的 current_index（本会话计数）与会话 A 留下的全局
    # last_index 错位，导致 [last+1, current] 取不到新对话）。
    # 老文件非 dict 格式（扁平 {count, last_msg_index}）视为错位数据清零。
    def _consolidate_count_path(self, character_id: str) -> str:
        return os.path.join(self._memory_dir(), f"{character_id}_embed_count.json")

    def _read_count_file(self, character_id: str) -> dict:
        """读取整个计数文件（dict: session_id -> {count, last_msg_index}）。
        老格式/损坏返回空 dict。"""
        path = self._consolidate_count_path(character_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and all(
                isinstance(v, dict) for v in data.values()
            ):
                return data
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        return {}

    def _write_count_file(self, character_id: str, data: dict):
        """原子写入计数文件（走 Storage._save_json_atomic，统一写锁，§12 契约）。

        [!] 本方法只写不读，调用方若需读改写（如 _write_consolidate_count）必须自行
        在 _json_write_lock 内完成「读旧 + 改 + 调本方法」，否则读改写竞态丢更新。
        """
        path = self._consolidate_count_path(character_id)
        Storage._save_json_atomic(path, data)

    def _read_consolidate_count(self, character_id: str, session_id: str) -> int:
        """读取该角色在指定会话的已整理次数（用持久化而非 collection.count，避免清库后计错）。"""
        data = self._read_count_file(character_id)
        return int(data.get(session_id, {}).get("count", 0))

    def _read_last_msg_index(self, character_id: str, session_id: str) -> int:
        """上次整理到该角色在本会话内已累计的 assistant 未折叠消息序号（0=尚未整理过）。
        增量追踪：本次整理只用 [last_msg_index, current_index) 段对话。
        老文件/新会话视为 0（首次整理回退为全量取数，行为不劣化）。"""
        data = self._read_count_file(character_id)
        return int(data.get(session_id, {}).get("last_msg_index", 0))

    def _write_consolidate_count(
        self, character_id: str, session_id: str,
        count: int, last_msg_index: int = 0,
    ):
        """写入该角色在指定会话的已整理次数 + last_msg_index 增量边界（会话级）。
        其他会话的计数保持不变。

        [!] 读改写整段包在 _json_write_lock 内（§12 契约）：ChatWorker 整理记忆写计数
        与主线程 clear_memory 写计数并发时，读旧值若不在锁内会被另一线程的写覆盖，
        导致其他会话的计数丢失。_save_json_atomic 内部也有写锁，但只锁写不锁读，
        故此处需方法级加锁覆盖「读旧 + 改 + 写」。
        """
        path = self._consolidate_count_path(character_id)
        with Storage._json_write_lock:
            data = self._read_count_file(character_id)
            data[session_id] = {"count": count, "last_msg_index": last_msg_index}
            Storage._save_json_atomic(path, data)

    # ============ 群聊会话级记忆（全量触发 + 全群共用边界 + 同段对话喂多角色）============
    # [!] 与单聊/角色个人配置路径独立：群聊 session.group_memory_interval > 0 时启用，
    # 触发口径=全量未折叠消息（用户+所有角色），到点后对窗口内发言过的角色逐一整理，
    # 每个角色喂「同一段 [last+1, current] 全量对话」+ 各自旧记忆，各走自己 memory_mode。
    # 边界计数器存 session 级单文件（全群共用一个），与角色级 {cid}_embed_count.json 解耦。
    def _group_mem_path(self, session_id: str) -> str:
        """session 级群聊记忆边界文件：data/memory/{session_id}_group_mem.json。"""
        return os.path.join(self._memory_dir(), f"{session_id}_group_mem.json")

    def _read_group_mem_state(self, session_id: str) -> dict:
        """读全群共用的整理状态。
        - last_msg_index: 上次整理到第几条全量消息（全局水位，不管成败都推进）
        - count: 已攒满触发的轮数
        - failed_chars: 失败角色名单 {char_id: {"start": N, "end": M}}（重试时取 [start, end] 段）
        """
        path = self._group_mem_path(session_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                failed = data.get("failed_chars") or {}
                if not isinstance(failed, dict):
                    failed = {}
                return {
                    "last_msg_index": int(data.get("last_msg_index", 0) or 0),
                    "count": int(data.get("count", 0) or 0),
                    "failed_chars": failed,
                }
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            pass
        return {"last_msg_index": 0, "count": 0, "failed_chars": {}}

    def _write_group_mem_state(self, session_id: str, last_msg_index: int, count: int, failed_chars: dict):
        """写全群边界状态（读改写在 _json_write_lock 内，与角色计数文件同口径，§12 契约）。"""
        path = self._group_mem_path(session_id)
        with Storage._json_write_lock:
            Storage._save_json_atomic(path, {
                "last_msg_index": last_msg_index, "count": count,
                "failed_chars": failed_chars,
            })

    def clear_group_mem_state(self, session_id: str):
        """删会话时级联清理群聊会话级记忆边界文件（{session_id}_group_mem.json）。

        与 clear_session_memory_state（按 cid 删角色级条目）不同：群聊边界是 session 级单文件，
        删会话时单独删一次。best-effort（孤儿无功能影响，session_id 是 UUID 不会被复用）。
        """
        try:
            os.remove(self._group_mem_path(session_id))
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _group_char_key(session_id: str) -> str:
        """群聊每角色取数边界在 {cid}_embed_count.json 里的 key（与角色个人配置路径区分）。

        用 "group:{session_id}" 前缀区分：角色个人配置路径用 session_id 作 key（无前缀），
        群聊会话级路径用 "group:{session_id}" 作 key，两者共存于同一文件不串。
        """
        return f"group:{session_id}"

    def _read_group_char_last_index(self, character_id: str, session_id: str) -> int:
        """读该角色在群聊会话里的取数边界（自己整理到第几条全量消息）。0=尚未整理过。"""
        data = self._read_count_file(character_id)
        key = self._group_char_key(session_id)
        return int(data.get(key, {}).get("last_msg_index", 0) or 0)

    def _write_group_char_last_index(self, character_id: str, session_id: str, last_msg_index: int):
        """写该角色在群聊会话里的取数边界（读改写在 _json_write_lock 内，与 _write_consolidate_count 同口径）。

        [!] 与角色个人配置路径（无前缀 session_id key）共存于同一 {cid}_embed_count.json 文件，
        互不干扰。读改写整段包在锁内防并发丢更新。
        """
        path = self._consolidate_count_path(character_id)
        key = self._group_char_key(session_id)
        with Storage._json_write_lock:
            data = self._read_count_file(character_id)
            data[key] = {"count": 0, "last_msg_index": last_msg_index}
            Storage._save_json_atomic(path, data)

    def check_and_update_group_memory(
            self,
            session: Session,
            members: list[Character],
            messages: list[Message],
            api_config: ApiConfig,
            cancel_check=None,
    ) -> None:
        """群聊会话级记忆整理（仅 session.group_memory_interval > 0 时启用）。

        两种触发模式：
        1. 攒满触发（正常节奏）：current - group_last >= interval。
           段 = [group_last, current]（本轮新增的全量消息）。找段内「参与」的角色
           （有 assistant 发言），各取该段+各自旧记忆整理。
           成功 char_last 推进到 current；失败记入 failed_chars {start:group_last, end:current}。
           全局水位不管成败都推进到 current。
        2. 失败重试触发：failed_chars 非空 且 current > group_last（消息增长了）。
           只整失败名单里的角色，取 [start, end] 段（上次失败的那段，不含新消息）。
           成功 char_last 推进到 end，从名单移除。全局水位不推进。

        [!] 角色是否「参与」段：该角色有 assistant 发言，或段内消息 content 含该角色名
        （LLM 描述其他角色时也算，符合"在场感知"）。未参与的角色不整理（哪怕 char_last < current）。

        边界：全局水位 + 失败名单存 session 级 JSON（{session_id}_group_mem.json）；
        每角色 char_last 存角色级 JSON（{cid}_embed_count.json，key=group:{session_id}）。
        群聊时编排器调用此入口，不再逐角色调 check_and_update_hybrid/summary。
        """
        interval = getattr(session, "group_memory_interval", 0) or 0
        if interval <= 0:
            return  # 会话级禁用，编排器回退走角色个人配置路径
        # 全量消息计数（含 user + 所有角色 assistant，不排除折叠消息，与单聊口径一致）。
        current_index = len(messages)
        state = self._read_group_mem_state(session.id)
        group_last = state["last_msg_index"]
        failed_chars: dict = state["failed_chars"]
        candidates = [c for c in members if c.memory_mode != "none"]

        # ===== 判断触发模式 =====
        reached_interval = current_index - group_last >= interval
        has_failed = bool(failed_chars) and current_index > group_last
        if not reached_interval and not has_failed:
            return  # 既没攒满也没失败角色要重试，不触发

        # ===== 模式2：失败重试（优先，只整失败名单角色）=====
        if has_failed and not reached_interval:
            self._retry_failed_chars(session, members, messages, api_config, state, cancel_check)
            return
        # ===== 模式1：攒满触发（同时若有失败角色也一并重试）=====
        # 先重试失败角色（取上次失败段），再整理本段参与角色
        if failed_chars:
            self._retry_failed_chars(session, members, messages, api_config, state, cancel_check)
            # 重试后重新读状态（失败名单可能已变）
            state = self._read_group_mem_state(session.id)
            failed_chars = state["failed_chars"]
        # 整理本段 [group_last, current] 参与的角色
        seg_start = group_last
        seg_end = current_index
        segment = messages[seg_start:seg_end]
        if not segment:
            return
        # 渲染段文本（跳过图片/summary 占位；折叠原文保留，与单聊口径一致）
        segment_text = self._render_segment_text(segment, session)
        # 找段内参与的角色（发言或被提及）
        targets = self._find_participating_chars(segment, candidates)
        debug_log(lambda: f"[Memory.group] 会话={session.id} 攒满 全量={current_index} 水位={group_last} "
                  f"段=[{seg_start},{seg_end}] 参与角色={[c.name for c in targets]}")
        for char in targets:
            if cancel_check and cancel_check():
                debug_log("[Memory.group] 整理被取消")
                return
            try:
                ok = self._consolidate_char(char, segment_text, seg_end, api_config,
                                            session, cancel_check)
                if ok:
                    self._write_group_char_last_index(char.id, session.id, seg_end)
                    debug_log(lambda: f"[Memory.group] 角色 {char.name} 成功，char_last={seg_end}")
                else:
                    # 失败：记入失败名单（记录失败段 start/end，重试时只取这段）
                    failed_chars[char.id] = {"start": seg_start, "end": seg_end}
                    debug_log(lambda: f"[Memory.group] 角色 {char.name} 整理失败，记入失败名单 [{seg_start},{seg_end}]")
            except Exception as e:
                failed_chars[char.id] = {"start": seg_start, "end": seg_end}
                debug_log(lambda: f"[Memory.group] 角色 {char.name} 整理异常: {e}")
        # 推进全局水位（不管成败），更新失败名单
        self._write_group_mem_state(session.id, current_index, state["count"] + 1, failed_chars)
        debug_log(lambda: f"[Memory.group] 全局水位推进到 {current_index}，剩余失败角色={list(failed_chars.keys())}")

    def _retry_failed_chars(
            self, session: Session, members: list[Character], messages: list[Message],
            api_config: ApiConfig, state: dict, cancel_check=None,
    ) -> None:
        """失败重试：只整失败名单里的角色，各取上次失败的 [start, end] 段。"""
        failed_chars: dict = state["failed_chars"]
        if not failed_chars:
            return
        char_map = {c.id: c for c in members}
        # 复制 key 避免迭代时修改
        for cid, rng in list(failed_chars.items()):
            char = char_map.get(cid)
            if not char or char.memory_mode == "none":
                failed_chars.pop(cid, None)  # 角色已删/改none，清出名单
                continue
            if cancel_check and cancel_check():
                debug_log("[Memory.group] 失败重试被取消")
                return
            seg_start = int(rng.get("start", 0))
            seg_end = int(rng.get("end", 0))
            segment = messages[seg_start:seg_end]
            segment_text = self._render_segment_text(segment, session)
            if not segment_text.strip():
                # 段内无内容，直接标记成功移出名单
                self._write_group_char_last_index(cid, session.id, seg_end)
                failed_chars.pop(cid, None)
                continue
            try:
                ok = self._consolidate_char(char, segment_text, seg_end, api_config,
                                            session, cancel_check)
                if ok:
                    self._write_group_char_last_index(cid, session.id, seg_end)
                    failed_chars.pop(cid, None)
                    debug_log(lambda: f"[Memory.group] 角色 {char.name} 重试成功，char_last={seg_end}")
                else:
                    debug_log(lambda: f"[Memory.group] 角色 {char.name} 重试仍失败，保留名单 [{seg_start},{seg_end}]")
            except Exception as e:
                debug_log(lambda: f"[Memory.group] 角色 {char.name} 重试异常: {e}")
        # 更新状态（失败名单可能已变，全局水位不推进）
        self._write_group_mem_state(session.id, state["last_msg_index"], state["count"], failed_chars)

    @staticmethod
    def _render_segment_text(segment: list[Message], session: Session) -> str:
        """渲染段消息为「角色名：内容」文本（跳过图片/summary 占位）。"""
        return "\n".join(
            f"{m.character_name or session.player_name or '用户'}：{m.content}"
            for m in segment
            if not m.is_image_only and not m.is_summary
        )

    @staticmethod
    def _find_participating_chars(segment: list[Message], candidates: list[Character]) -> list[Character]:
        """找段内「参与」的角色：有 assistant 发言。

        [!] 只整发言过的角色（不判断被提及）：避免角色名子串误匹配（如角色名"明"命中"明天"）。
        群聊记忆提示词已用 {{char_name}} 点名「只记录该角色在场时能感知的事」，LLM 自己会判断
        要不要记录其他角色的言行。后续如需更精确可加 @提及 判断。
        """
        result = []
        for char in candidates:
            for m in segment:
                if m.is_image_only or m.is_summary:
                    continue
                # 该角色有 assistant 发言
                if m.role == "assistant" and m.character_id == char.id:
                    result.append(char)
                    break
        return result

    def _consolidate_char(
            self, char: Character, segment_text: str, msg_index: int,
            api_config: ApiConfig, session: Session, cancel_check=None,
    ) -> bool:
        """调用角色的整理核心（按 memory_mode 分发），返回是否成功。"""
        if char.memory_mode == "embedding_hybrid":
            return self._consolidate_hybrid_with_segment(
                char, segment_text, msg_index, api_config,
                session.session_type, cancel_check=cancel_check,
            )
        elif char.memory_mode == "summary":
            ok, _ = self._consolidate_summary_with_segment(
                char, segment_text, session, api_config,
                cancel_check=cancel_check,
            )
            return ok
        return True  # none 模式（不应进 candidates，兜底）

    def get_memory_info(self, character: Character) -> dict:
        """获取记忆状态信息（用于 UI 展示）。"""
        if character.memory_mode == "summary":
            return {
                "mode": "summary",
                "memory": self._read_summary_memory(character.id),
                "msg_count": self._read_summary_count(character.id),
            }
        elif character.memory_mode == "embedding_hybrid":
            return {
                "mode": "embedding_hybrid",
                "count": self.storage.count_char_memory_entries(character.id),
            }
        return {"mode": "none"}

    def clear_memory_by_id(self, character_id: str):
        """按 character_id 全清该角色所有模式的记忆数据（不依赖 Character 对象）。

        用于删除角色时级联清理，覆盖当前与历史的全部存储：
        - summary：{cid}_summary.json
        - embedding_hybrid：SQLite 三表 + ChromaDB collection（char_{cid}_hybrid）+ {cid}_embed_count.json
        - 历史孤儿：已下线 embedding 模式的 char_{cid}_emb collection + 老命名 char_{cid[:12]}
        清理时三种 collection 名都尝试删，无则忽略。
        """
        # summary 记忆文件
        try:
            os.remove(self._summary_path(character_id))
        except FileNotFoundError:
            pass
        except OSError:
            pass
        # hybrid 模式 SQLite 三表
        try:
            self.storage.clear_char_memory(character_id)
        except Exception:
            pass
        # ChromaDB collection：当前 _hybrid + 已下线 _emb 孤儿 + 老命名 char_{cid[:12]} 都尝试删
        try:
            client = self._get_chroma()
            for name in (
                self._collection_name(character_id),                    # 当前 hybrid
                f"char_{character_id}_emb",                             # 已下线 embedding 模式孤儿
                f"char_{character_id[:12]}",                            # 老命名兼容
            ):
                try:
                    client.delete_collection(name)
                except Exception:
                    pass
        except Exception:
            pass
        # 整理计数文件（hybrid 用，会话级 dict 结构）
        try:
            os.remove(self._consolidate_count_path(character_id))
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def clear_session_memory_state(self, character_id: str, session_id: str):
        """删会话时级联清理该角色在本会话的增量边界/计数状态。

        与 clear_memory_by_id（删角色时全清整个角色的记忆文件）不同：角色仍在其他
        会话使用，只删本会话条目，保留 memory 正文与其他会话计数。清理三处会话级 dict：
        - {cid}_summary.json 的 updated_at_msg_count[session_id]（保留 memory 正文）
        - {cid}_embed_count.json 的 [session_id]（角色个人配置路径，保留其他会话计数）
        - {cid}_embed_count.json 的 [group:{session_id}]（群聊会话级路径，保留其他会话计数）

        [!] 读改写整段包在 _json_write_lock 内（§12 契约，与 _write_consolidate_count/
        _write_summary_memory_session 同口径）：删会话与 ChatWorker 整理记忆并发写同一
        角色计数文件时，读旧值若不在锁内会被另一线程写覆盖致其他会话计数丢失。
        整体 best-effort：任何异常都不阻断会话删除（计数残留无功能影响，session_id 是
        UUID 不会被复用，孤儿条目永不再被读到）。
        """
        # summary 计数条目（保留 memory 正文与其他会话计数）
        try:
            path = self._summary_path(character_id)
            with Storage._json_write_lock:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except (FileNotFoundError, json.JSONDecodeError):
                    data = {}
                counts = data.get("updated_at_msg_count")
                if isinstance(counts, dict) and session_id in counts:
                    del counts[session_id]
                    Storage._save_json_atomic(path, data)
        except Exception:
            pass
        # hybrid 整理计数条目（保留其他会话计数）
        try:
            with Storage._json_write_lock:
                data = self._read_count_file(character_id)
                changed = False
                # 角色个人配置路径条目（无前缀 session_id）
                if session_id in data:
                    del data[session_id]
                    changed = True
                # 群聊会话级路径条目（group:{session_id} 前缀）
                group_key = self._group_char_key(session_id)
                if group_key in data:
                    del data[group_key]
                    changed = True
                if changed:
                    Storage._save_json_atomic(self._consolidate_count_path(character_id), data)
        except Exception:
            pass

    def clear_memory(self, character: Character):
        """清空角色当前模式的记忆（按 character.memory_mode 选择性清理）。

        [!] 若需全清（如删除角色），用 clear_memory_by_id 覆盖所有模式。
        """
        self.clear_memory_by_id(character.id)