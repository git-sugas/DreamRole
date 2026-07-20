"""
角色记忆服务：两种模式，均优先用 MemoryPreset.api_id 独立绑定 API（缺失回退角色绑定 API）。
  - summary: 旧记忆 + 上次总结到现在的新增对话 -> 调用总结接口（MemoryPreset.summary_prompt）
    -> 覆盖存成一段文本记忆。触发用 summary_interval、取窗用 summary_window（解耦）。
    增量追踪用 {cid}_summary.json 的 updated_at_msg_count。
  - embedding_hybrid: 三路召回（emb+triggers+detail）+ 两次召回合并 + 纯追加。
    整理用 MemoryPreset.hybrid_system_prompt，输出 `[triggers: ...] 明细` 纯追加入库
    （ChromaDB _hybrid collection + SQLite 三表）。增量追踪用 {cid}_embed_count.json。
  两模式共享 MemoryPreset 的 api_id 与生成参数；提示词各自一份（输出约定不同）。

记忆按角色全局存储（跨会话），存于 data/memory/ 目录与 data/chroma/ 向量库。
"""
from __future__ import annotations
from src.utils.debug import debug_log
import json
import math
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
        """
        cfg = character.memory_config or {}
        return max(1, int(cfg.get("embedding_interval", cfg.get("summary_interval", 1)) or 1))

    @staticmethod
    def _summary_window(character: Character) -> int:
        """summary 模式的总结窗口（每次总结取最近多少条对话）。
        缺省/缺失/为 0 时回退到 summary_interval，老角色卡行为零变化。"""
        cfg = character.memory_config or {}
        w = cfg.get("summary_window", 0)
        if not w:
            w = cfg.get("summary_interval", 20)
        return max(1, int(w or 1))

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
            if not MemoryService._legacy_cleaned:
                MemoryService._legacy_cleaned = True
                try:
                    for name in self._chroma_client.list_collections():
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
        weights = tuple(getattr(preset, "hybrid_recall_weights", (0.5, 0.2, 0.1, 0.2)))

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
        weights = tuple(getattr(preset, "hybrid_recall_weights", (0.5, 0.2, 0.1, 0.2)))
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
        """hybrid 单次三路融合召回，返回 {seq: {s_emb, s_trig, s_detail, s_seq, score, src}}。

        三路：emb（ChromaDB 语义）+ triggers（FTS5 bm25）+ detail（FTS5 bm25 独立表）。
        score = w_emb·emb_sim + w_trig·trig_sim + w_detail·detail_sim + w_seq·seq_norm
        各路按 seq 去重；trig/detail 各自 bm25 绝对值 min-max 归一化；
        seq 在候选集内 min-max 归一化（新记忆略优先）；候选集仅 1 个 seq 时 seq_norm=1.0。
        emb 路无 embedding_model 时短路（仅 trig+detail 两路，s_emb 恒 0）。
        src 标注该 seq 命中了哪几路（如 "emb+trig+detail"），供测试区展示。
        """
        w_emb, w_trig, w_detail, w_seq = weights
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
                                # 打标方案：metadata 已存全量明细，召回时直接收集 detail/triggers，
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

        # ===== 路 C：detail 路 FTS5 召回（独立表 bm25，不稀释 trig）=====
        det_rows = self.storage.query_char_mem_fts_detail(query_text, cid, top_n)
        det_scores: dict[int, float] = {}
        max_det = max((abs(r["s"]) for r in det_rows), default=0.0) or 1.0
        for r in det_rows:
            try:
                seq = int(r["seq"])
            except (ValueError, TypeError):
                continue
            det_scores[seq] = abs(r["s"]) / max_det
        debug_log(lambda: f"[Memory.hybrid.recall] 路 C detail: {len(det_scores)} seq")

        # ===== 融合：union of seq，返回明细 =====
        all_seqs = set(emb_scores) | set(trig_scores) | set(det_scores)
        if not all_seqs:
            return {}
        # [!] emb 路 sim 也做候选集内 min-max 归一化（与 trig/detail 口径一致），
        # 避免三路同权相加时量级不可比（emb 绝对值 vs trig/detail 的 abs/max）。
        if emb_scores:
            max_emb = max(emb_scores.values()) or 1.0
            min_emb = min(emb_scores.values())
            emb_range = (max_emb - min_emb) or 1.0
            emb_scores = {k: (v - min_emb) / emb_range for k, v in emb_scores.items()}
        # [!] emb 路短路（无 embedding_model 或召回为空）时，剩余三路权重重归一化，
        # 避免退化为「半功率」召回（score 上限骤降导致 top-K 截断阈值偏低）。
        if not emb_scores and (w_trig + w_detail + w_seq) > 0:
            total = w_trig + w_detail + w_seq
            w_emb, w_trig, w_detail, w_seq = 0.0, w_trig / total, w_detail / total, w_seq / total
        seq_min = min(all_seqs)
        seq_max = max(all_seqs)
        seq_range = (seq_max - seq_min) or 1   # 候选集仅 1 个 seq 时回退 1 防除零
        detail: dict[int, dict] = {}
        for seq in all_seqs:
            s_emb = emb_scores.get(seq, 0.0)
            s_trig = trig_scores.get(seq, 0.0)
            s_det = det_scores.get(seq, 0.0)
            s_seq = (seq - seq_min) / seq_range
            score = w_emb * s_emb + w_trig * s_trig + w_detail * s_det + w_seq * s_seq
            # src 标注命中路
            parts = []
            if seq in emb_scores:
                parts.append("emb")
            if seq in trig_scores:
                parts.append("trig")
            if seq in det_scores:
                parts.append("detail")
            detail[seq] = {
                "s_emb": s_emb, "s_trig": s_trig, "s_detail": s_det,
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

        返回 [{seq, triggers, detail, s_emb, s_trig, s_detail, s_seq,
               merged_score, src_assistant, src_user}], 按 merged_score 降序。
        s_emb/s_trig/s_detail/s_seq 取两次召回中该 seq 的最大子分（展示用，看哪路强）；
        src_assistant/src_user 分别标两次召回各自的命中路；merged_score 是加权合并分。
        """
        preset = self.storage.load_memory_preset()
        top_k = max(1, int(getattr(preset, "hybrid_recall_top_k", 15)))
        user_w = float(getattr(preset, "hybrid_user_recall_weight", 0.6))
        weights = tuple(getattr(preset, "hybrid_recall_weights", (0.5, 0.2, 0.1, 0.2)))

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
                "s_detail": max(ia.get("s_detail", 0.0), iu.get("s_detail", 0.0)),
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

        current_memory = self._read_summary_memory(character.id)
        window = self._summary_window(character)
        conversation = "\n".join(
            f"{m.character_name}：{m.content}" for m in new_msgs[-window:]
        )

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

        # 取消或失败时不推进计数（last_count 保持旧值，下次重试该段对话）
        if result.cancelled or result.error or not result.content:
            return False

        self._write_summary_memory_session(character.id, sid, result.content.strip(), len(char_msgs))
        return True

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
        （char_memory_entry + 两张 FTS5 表 + ChromaDB），每条新 seq 单调递增。
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
        # 复用旧 embedding 模式的整理计数与 last_msg_index 持久化（同目录同文件，会话级 dict）
        count = self._read_consolidate_count(character.id, session_id)
        new_count = count + 1
        msgs = list(recent_messages or [message])
        current_index = self._count_char_assistant_msgs(msgs, character.id)
        last_index = self._read_last_msg_index(character.id, session_id)

        if new_count % interval != 0:
            # 未到整理点，跳过 API 调用；仍推进计数与 last_msg_index
            self._write_consolidate_count(character.id, session_id, new_count, last_msg_index=current_index)
            return

        # 到整理点：执行纯追加整理；失败/取消时不推进 last_msg_index
        ok = self._consolidate_hybrid(
            character, message, api_config, msgs, last_index, current_index, session_type,
            cancel_check=cancel_check,
        )
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
        """执行一次 hybrid 记忆整理：旧记忆(triggers+detail原文) + 新对话 -> LLM 产出新增条目 -> 纯追加入库。

        流程：
        1. 旧记忆喂法：读 char_memory_entry 全部条目渲染成 `[seq] 触发词:... | 明细` 喂 LLM（全量，不限上限）。
        2. 提示词用 hybrid_system_prompt / hybrid_system_prompt_group（输出 [triggers:..] 明细 格式）。
        3. 入库是纯追加：解析 LLM 输出为 (triggers, detail) 列表，逐条 seq 自增写三处存储，不清空旧条目。
        4. emb 路入库：triggers+detail 拼接 embed 入 ChromaDB，metadata 带 seq。

        返回 True 表示整理成功（推进 last_msg_index），False 表示失败/取消/空（保留 last_index 重试）。
        cancel_check 透传给 LLM 调用，支持停止生成时中断。
        """
        try:
            # 1. 读取已有全部条目（喂 LLM 去重用，按 seq 升序）
            old_entries = self.storage.fetch_all_char_memory_entries(character.id)
            old_memory_text = "\n".join(
                f"[seq:{e['seq']}] 触发词:{e['triggers']} | {e['detail']}"
                for e in old_entries
            )
            debug_log(lambda: f"[Memory.hybrid.consolidate] 角色={character.name} 已有条目 {len(old_entries)}")

            # 2. 取上次整理到现在的增量新对话原文（含用户与其他角色发言）
            new_conv = self._build_incremental_conversation(
                messages, character.id, last_index, current_index,
                self._embedding_interval(character),
            )

            # 3. 调整理 API（优先 MemoryPreset 绑定 API，回退角色绑定 API）
            preset = self.storage.load_memory_preset()
            cons_api = api_config
            if preset.api_id:
                preset_api = self.storage.load_api(preset.api_id)
                if preset_api and preset_api.enabled:
                    cons_api = preset_api
            debug_log(lambda: f"[Memory.hybrid.consolidate] API={cons_api.name}({cons_api.model})")
            debug_log(lambda: f"[Memory.hybrid.consolidate] 入参 旧记忆:\n{old_memory_text or '（暂无）'}")
            debug_log(lambda: f"[Memory.hybrid.consolidate] 入参 新对话(增量 last={last_index} cur={current_index}):\n{new_conv or '（无）'}")
            consolidated, cancelled = self._call_hybrid_consolidate_api(
                character, old_memory_text, new_conv, cons_api, preset, session_type,
                cancel_check=cancel_check,
            )
            if cancelled:
                debug_log(f"[Memory.hybrid.consolidate] 角色 {character.name} 整理被取消")
                return False
            debug_log(lambda: f"[Memory.hybrid.consolidate] 出参:\n{consolidated or '（空/失败）'}")
            if not consolidated:
                return False

            # 4. 解析为 (triggers, detail) 列表 -> 纯追加入库
            entries = parse_hybrid_entries(consolidated)
            if not entries:
                debug_log("[Memory.hybrid.consolidate] 解析出条目为空，跳过入库")
                return False
            debug_log(lambda: f"[Memory.hybrid.consolidate] 解析条目 {len(entries)} 条，纯追加入库")
            self._append_hybrid_entries(character.id, entries, current_index, cons_api)
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
        （无 embedding_model 时跳过 emb 路，仍写主表 + 两张 FTS5 表，召回时 emb 路短路）。
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
            # 写主表 + 两张 FTS5 表
            self.storage.insert_char_memory(character_id, seq, triggers, detail, created_msg_index)
            # emb 路：[Trigger]+[Detail] 打标后 embed 入 ChromaDB，metadata 带 seq+全量明细
            if collection is not None and emb_client is not None:
                emb_text = f"[Trigger] {triggers} [Detail] {detail}"
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
        # 上限窗口防过长（4×interval 条，覆盖约两轮 user+assistant 估算）
        cap = max(1, interval * 4)
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

    def clear_memory(self, character: Character):
        """清空角色当前模式的记忆（按 character.memory_mode 选择性清理）。

        [!] 若需全清（如删除角色），用 clear_memory_by_id 覆盖所有模式。
        """
        self.clear_memory_by_id(character.id)