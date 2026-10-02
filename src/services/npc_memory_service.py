"""[P6c] 世界模拟 NPC 个人记忆服务（独立于角色记忆 memory_service）。

需求来源：read.md §22 Phase C「NPC 应有自己的介绍页...还有自己个人的记忆，可以参考现在的
角色记忆系统，通过 emb 召回注入记忆的上限，或者用总结也可以，但是需要 llm 自己懂得分类合并」。

设计（仿 memory_service，但存储走「每世界一目录 JSON + ChromaDB」，不新增 SQLite/FTS5 表）：
- 两模式：summary（LLM 把该 NPC 近期经历整合成一段记忆，覆盖存）+ embedding_hybrid
  （LLM 产 [triggers: ...] 明细行，纯追加 + ChromaDB emb 召回 top-K）。
- 触发：每与该 NPC 交互 N 次（npc_memory_interval）整理一次（取窗 = interval）。
- 召回：注入场景上下文【在场 NPC】段每人 top-K 记忆（emb 路或最近 N 条回退）。
- LLM 自分类合并：提示词要求「不重复已有事实，新事实分类，冲突以大 seq 为准」。
- 存储路径：data/worlds/{world_id}_npc_memory/{npc_id}.json（记忆内容）+
  ChromaDB collection npc_{npc_id}_hybrid（embedding_hybrid 模式的 emb 向量）。
- 级联清理：clear_world_memory(world) 删 JSON + ChromaDB（storage.delete_world 删 JSON 目录的补充）。

守 §21 完全独立铁律：独立 service，collection 命名 npc_* 与 char_* 物理隔离，不动 memory_service。
- 私聊关窗整理（consolidate_chat 2026-09-08）：复用 check_and_update 同一整理链路
  （旧版独立「5 分类」提示词 + 覆盖写 summary 已删——真机存档实测丢关键事实）。
底层范式：LLM 出记忆语义，召回/注入纯 Python。零 emoji。
"""
from __future__ import annotations
import json
import os
import re

from typing import Callable, Optional

from src.config import paths
from src.models import (
    Preset, WorldSimPreset, World, NPC, SceneLog,
    DEFAULT_NPC_MEMORY_SUMMARY_PROMPT, DEFAULT_NPC_MEMORY_HYBRID_PROMPT,
    DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT, DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT,
)
from src.services.llm_client import LlmClient
from src.services.world_sim_service import (
    scrub_npc_cmd_head, _fmt_user_identity, _world_anchor,
)
from src.services.storage import Storage


_HYBRID_LINE = re.compile(r"\[triggers[:：]\s*([^]]+)\]\s*(.+)")
# [记忆驱动切段 2026-09-08] summary 段落按句末标点切句（。！？；;）——旧版按「；」
# 切分，段落记忆只有偶然写分号才切段，否则整段退化为前 60 字半句（真机存档
# 5 份记忆 4 份无分号）；按句切不再依赖 LLM 写作习惯（旧 5 分类存档条目以句号
# 收尾，同样按句切开）。
_SENT_RE = re.compile(r"(?<=[。！？!?；;])")


def _collection_name(npc_id: str) -> str:
    """ChromaDB collection 名：npc_{full_uuid}_hybrid（与 char_* 物理隔离）。"""
    return f"npc_{npc_id}_hybrid"


class NpcMemoryService:
    """NPC 个人记忆（summary / embedding_hybrid）。"""

    def __init__(self, storage: Storage):
        self.storage = storage
        self._chroma_client = None

    # ============ 存储路径 ============
    def _mem_path(self, world_id: str, npc_id: str) -> str:
        return os.path.join(paths.npc_memory_dir(world_id), f"{npc_id}.json")

    def _load(self, world_id: str, npc_id: str) -> dict:
        try:
            with open(self._mem_path(world_id, npc_id), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, world_id: str, npc_id: str, data: dict):
        # [!] 统一走 Storage._save_json_atomic（建目录 + tmp + os.replace + 全局写锁）：
        # 自造原子写无锁，scene worker 整理与 UI 侧 recall 并发写同一 .tmp 会交错损坏。
        from src.services.storage import Storage
        Storage._save_json_atomic(self._mem_path(world_id, npc_id), data)

    def record_chat_note(self, world, npc_id: str, note: str) -> None:
        """[P42a] 场间闲谈笔记直写记忆文件（chat_notes 队列，无 LLM）。

        下次 check_and_update 整理时并入总结输入并清空队列（玩家不可知的 NPC-NPC
        交谈经定期总结正式成为记忆；召回走既有 recall）。"""
        if not world or not npc_id or not str(note or "").strip():
            return
        data = self._load(world.id, npc_id)
        notes = data.get("chat_notes")
        if not isinstance(notes, list):
            notes = []
        notes.append({"day": max(1, int(getattr(world, "day_count", 1) or 1)),
                      "text": str(note).strip()[:120]})
        data["chat_notes"] = notes[-20:]          # 滚动 20 条防膨胀
        self._save(world.id, npc_id, data)

    def _drain_chat_notes(self, world_id: str, npc_id: str, data: Optional[dict] = None) -> list:
        """取走并清空 chat_notes（整理时并入输入用）。

        data 传入时在其上原地 pop 不落盘（调用方随后整体 _save 持久化）；
        [!] 必须与调用方持有同一份 data——另行 _load 清空再被外层旧快照整体回写
        会把队列原样复活（同一批笔记永久污染每轮整理输入，P42a 契约失效）。"""
        if data is None:
            data = self._load(world_id, npc_id)
            save = True
        else:
            save = False
        notes = data.pop("chat_notes", None)
        if not isinstance(notes, list) or not notes:
            return []
        if save:
            data["chat_notes"] = []
            self._save(world_id, npc_id, data)
        return [str(n.get("text", "") or "") for n in notes if isinstance(n, dict)]

    # ============ ChromaDB ============
    def _get_chroma(self):
        if self._chroma_client is None:
            import chromadb
            self._chroma_client = chromadb.PersistentClient(path=paths.chroma_dir())
        return self._chroma_client

    def _get_collection(self, npc_id: str):
        client = self._get_chroma()
        return client.get_or_create_collection(
            name=_collection_name(npc_id), metadata={"hnsw:space": "cosine"})

    # ============ API 解析 ============
    def _resolve_llm_api(self, preset: WorldSimPreset):
        """LLM API 三级兜底：npc_memory_api_id -> calculator_api_id -> 首个 enabled。"""
        api_id = getattr(preset, "npc_memory_api_id", "") or preset.calculator_api_id
        if api_id:
            api = self.storage.load_api(api_id)
            if api and api.enabled:
                return api
        for api in self.storage.load_all_apis():
            if api.enabled:
                return api
        return None

    def _resolve_emb_api(self):
        """取一个配了 embedding_model 的 API（emb 召回用）。无则返回 None（hybrid 降级最近 N）。"""
        for api in self.storage.load_all_apis():
            if api.enabled and getattr(api, "embedding_model", ""):
                return api
        return None

    def _jb_prefix(self) -> str:
        cfg = self.storage.load_app_config()
        if cfg.jailbreak_enabled and cfg.jailbreak_prefix:
            return cfg.jailbreak_prefix
        return ""

    # ============ 结构化记忆输入构造（[P23] 阶段一：防 OOC/漏剧情/漏细节） ============
    @staticmethod
    def build_structured_recent_text(intent: dict, player_action: Optional[str],
                                     narration: str, talk_npc_name: str) -> str:
        """[P23 阶段一] 把记忆输入从「旁白全文」重构为信息丰富的多段输入。

        设计哲学：不预抽取、不裁剪——把所有原始素材喂给记忆 LLM，由提示词规则指导它
        自己做视角过滤与取舍（语义判断是 LLM 的活，纯 Python 按句/引号切会漏会错）。

        防四大缺陷：
        - 玩家言缺位 -> 【玩家本回合行动】直接取 player_action（原始输入/选项 hint）
        - 全知污染 -> 喂旁白全文但标注「第二人称玩家视角，含该 NPC 感知不到的信息」，
          由提示词规则要求 LLM 只取该 NPC 在场时能感知/参与的部分
        - 台词当事实 -> 【结算结构化后果】取自 intent.effects（引擎已产出的客观后果），
          提示词规则要求区分 NPC 台词的陈述/客套
        - 档案薄 -> NPC 档案补厚在 _consolidate_summary/hybrid 的 user 消息里做（本方法不管）

        返回拼好的多段文本，供 check_and_update 的 recent_text 用。
        """
        parts = []
        # 【玩家本回合行动】—— 直接取玩家输入，补回叙事规则2导致的玩家言辞缺位
        pa = (player_action or "").strip()
        if pa:
            parts.append(f"【玩家本回合行动】\n{pa}\n"
                         f"（这是玩家本回合的输入。旁白中可能未逐字呈现玩家原话，"
                         f"以此为准理解玩家意图；玩家具体说了什么若旁白未还原，按此意图推断，不得编造细节。）")
        else:
            parts.append("【玩家本回合行动】\n（未记录玩家行动文本。）")
        # 【结算结构化后果】—— 引擎纯 Python 产出的客观事实，非文学化描写
        effects = intent.get("effects") if intent else None
        if isinstance(effects, list) and effects:
            eff_lines = [f"- {str(e).strip()}" for e in effects if str(e).strip()]
            if eff_lines:
                parts.append("【结算结构化后果】（引擎结算的客观事实，双方在场都可见）\n" + "\n".join(eff_lines))
        # 【本回合旁白全文】—— 第二人称玩家视角叙事，信息最全但含全知视角内容
        clean_narr = scrub_npc_cmd_head(narration or "").strip()
        if clean_narr:
            parts.append("【本回合旁白全文】\n"
                         f"（这是玩家视角的第二人称叙事。{talk_npc_name} 是本回合 talk_to 的对象，"
                         "旁白中的无主语引号台词大概率是其所说。但旁白是全知视角，"
                         "可能含该 NPC 感知不到的信息：玩家的内心活动、其他在场人物的私下举动、"
                         "视线外的事件、环境氛围。整理记忆时由你判断哪些是该 NPC 在场时能直接感知/"
                         "参与的，只记这些；该 NPC 的台词要区分陈述事实与客套/戏谑/夸张/谎言，"
                         "后者不作为事实记但可作为说话风格/态度印象。）\n" + clean_narr)
        return "\n\n".join(parts)

    # ============ 触发 + 整理 ============
    def check_and_update(self, npc: NPC, world: World, scene: SceneLog,
                         preset: WorldSimPreset, cancel_check: Optional[Callable[[], bool]] = None,
                         recent_text: Optional[str] = None, force: bool = False) -> bool:
        """每与该 NPC 交互一次调一次；攒满 interval 整理一轮。返回是否执行了整理。

        取窗 = 最近 interval 条场景记录（含玩家行动 + 旁白）。整理成功才推进计数。
        recent_text 非空时经 scrub_npc_cmd_head 清洗后用作整理输入（场景 worker 把刚
        生成的旁白拼进去，绕开「主线程才落场景日志」的时序）。
        [P23] force=True 时跳过 interval 计数直接整理（settle LLM 判定 memory_worthy=true
        的关键事件立即整理，不被 interval 节流；仍受 mode/off 开关约束）。
        """
        mode = self._mode(preset)
        if mode == "off" or not npc or not world:
            return False
        interval = max(1, int(getattr(preset, "npc_memory_interval", 5) or 5))
        data = self._load(world.id, npc.id)
        count = int(data.get("count", 0) or 0) + 1
        if not force and count < interval:
            data["count"] = count
            data["npc_id"] = npc.id
            data["world_id"] = world.id
            self._save(world.id, npc.id, data)
            return False
        # 取窗：recent_text 优先；否则最近 interval 条场景记录。
        # 两条路径统一过 scrub_npc_cmd_head（幂等）：防旧版漏剥的头部 [NPC] 命令行入记忆库。
        if recent_text is not None:
            text = scrub_npc_cmd_head(recent_text)
        else:
            recent = scene.recent(interval) if hasattr(scene, "recent") else (scene.log[-interval:] if scene.log else [])
            text = "\n".join(scrub_npc_cmd_head(e.content) for e in recent if getattr(e, "content", ""))
        # [P42a] 场间闲谈笔记并入本轮整理输入（取走并清空；玩家不可知的 NPC-NPC
        # 交谈经定期总结正式成为记忆）。传同一份 data 原地取走——末尾统一 _save 落盘
        # 即「清空」生效，勿另行加载（会被旧快照回写复活）。
        chat_notes = self._drain_chat_notes(world.id, npc.id, data)
        if chat_notes:
            text = "\n".join(chat_notes) + "\n" + text
        if not text.strip():
            data["count"] = 0
            self._save(world.id, npc.id, data)
            return False
        ok = False
        if mode == "summary":
            ok = self._consolidate_summary(npc, world, text, data, preset, cancel_check)
        else:  # embedding_hybrid
            ok = self._consolidate_hybrid(npc, world, text, data, preset, cancel_check)
        # 成功才推进计数（失败/取消保留以便重试）；闲谈笔记失败/取消时回填队列重试（不丢不漏）
        data["count"] = 0 if ok else count
        if not ok and chat_notes:
            data["chat_notes"] = [{"text": n} for n in chat_notes]
        data["npc_id"] = npc.id
        data["world_id"] = world.id
        self._save(world.id, npc.id, data)
        return ok

    def consolidate_chat(self, npc: NPC, world: World, chat_text: str,
                         preset: WorldSimPreset,
                         cancel_check: Optional[Callable[[], bool]] = None) -> bool:
        """[记忆化私聊重构 2026-09-08] 私聊关窗整理入口：复用 check_and_update 的
        _consolidate_summary / _consolidate_hybrid 整条链路——同一套 preset 提示词
        （summary=段落 / hybrid=[triggers] 条目）、「现有记忆全文 + 素材 -> 合并整理
        （保留仍有效旧记忆）」语义、npc_memory API 兜底链、玩家身份行。

        旧版（2026-08-29）私聊总结走独立「5 分类」提示词 + merge_chat_summary 覆盖写
        summary（既有记忆输入还是 recent_lines 的 60 字碎块）——真机存档实测私聊过的
        NPC 反而丢了全部关键场景事实，且分类结构无任何引擎消费，已删。

        与 check_and_update 差异：不走 interval 计数（关窗即整理）、不并入 chat_notes
        （留给下一轮 interval 整理，不丢不漏）。整理成功才落盘；失败返回 False。
        """
        mode = self._mode(preset)
        if mode == "off" or not npc or not world or not (chat_text or "").strip():
            return False
        # 统一过 scrub_npc_cmd_head（幂等，与 check_and_update 口径一致——防泄漏
        # 命令行入库；私聊正文正常无命令行，此为防御）
        text = scrub_npc_cmd_head(chat_text)
        data = self._load(world.id, npc.id)
        if mode == "summary":
            ok = self._consolidate_summary(npc, world, text, data, preset, cancel_check)
        else:  # embedding_hybrid
            ok = self._consolidate_hybrid(npc, world, text, data, preset, cancel_check)
        if ok:
            # [!] 落盘前重载磁盘合并（跨 LLM 调用持旧快照直接回写会复活并发 drain
            # 的 chat_notes / 回滚 count——scene worker 的 check_and_update 与关窗
            # 整理并行窗口，P42a 同款隐患）：只回写本链路产出的字段
            # （summary / entries），chat_notes/count 以磁盘最新为准。
            fresh = self._load(world.id, npc.id)
            fresh["summary"] = data.get("summary", "")
            if mode == "embedding_hybrid":
                disk_seqs = {int(e.get("seq", 0)) for e in (fresh.get("entries") or [])
                             if isinstance(e, dict)}
                merged = [e for e in (fresh.get("entries") or []) if isinstance(e, dict)]
                for e in (data.get("entries") or []):
                    if isinstance(e, dict) and int(e.get("seq", 0)) not in disk_seqs:
                        merged.append(e)
                fresh["entries"] = merged[-500:]
            fresh["npc_id"] = npc.id
            fresh["world_id"] = world.id
            self._save(world.id, npc.id, fresh)
        return ok

    def _mode(self, preset: WorldSimPreset) -> str:
        if not getattr(preset, "npc_memory_enabled", True):
            return "off"
        m = getattr(preset, "npc_memory_mode", "summary") or "summary"
        return m if m in ("summary", "embedding_hybrid") else "summary"

    @staticmethod
    def _fmt_npc_profile(npc, world) -> str:
        """[P23 阶段一] NPC 档案厚注入（与滴答 _build_key_npc_user_message 口径对齐）。

        旧版只注入「身份+性格」两项 -> 记忆 LLM 缺「对该 NPC 什么算重要」的判断锚点，
        把无关细节也记了。补目标/关系/小传/交情，让记忆梳理有取舍依据。
        """
        seg = [f"NPC：{npc.name}（身份：{npc.role or '未定'}；性格：{npc.personality or '未定'}"]
        # [A+B 2026-09-06] 此刻念头/今日目标进档案（说话/记忆贴合当下心境）
        _th = (getattr(npc, "current_thought", "") or "").strip()
        if _th:
            seg.append(f"；念头：{_th[:60]}")
        goal = (getattr(npc, "goal", "") or "").strip()
        if goal:
            seg.append(f"；目标：{goal}")
        # 关系（前 3，与滴答/world_sim_service 同口径）
        rels = getattr(npc, "relationships", None) or []
        if isinstance(rels, list) and rels:
            rel_parts = []
            for r in rels:
                if isinstance(r, dict):
                    tn = str(r.get("target_name", "") or "").strip()
                    rl = str(r.get("relation", "") or "").strip()
                    if tn and rl:
                        rel_parts.append(f"{tn}({rl})")
            if rel_parts:
                seg.append("；关系：" + "、".join(rel_parts[:3]))
        notes = (getattr(npc, "notes", "") or "").strip()
        if notes:
            seg.append(f"；小传：{notes[:60]}")
        # 交情（需 world 判好友/同伴）
        try:
            from src.services import npc_relation_engine as nre
            aff = int(getattr(npc, "affinity", 0) or 0)
            friend_tag = "，是玩家的好友" if nre.is_friend(world, npc) else ""
            comp_tag = "，正与玩家同行" if nre.is_companion(world, npc) else ""
            seg.append(f"；与玩家交情：{nre.affinity_level(aff)}({aff}){friend_tag}{comp_tag}")
        except Exception:
            aff = int(getattr(npc, "affinity", 0) or 0)
            seg.append(f"；与玩家交情：{aff}")
        seg.append("）")
        return "".join(seg)

    def _fmt_player_identity(self, world) -> str:
        """[2026-08-23 用户卡绑定] 玩家身份行（世界绑定用户卡的姓名/人设）。
        未绑定/已删/无 storage 返回空串（记忆 LLM 退回旧泛称口径）。让记忆里的玩家
        有名字有形象，不再「那个男人/那个白衣年轻人」一片模糊。"""
        uid = str(getattr(world, "user_id", "") or "")
        if not uid or self.storage is None:
            return ""
        try:
            return _fmt_user_identity(self.storage.load_user(uid))
        except Exception:
            return ""

    def _player_identity_line(self, world) -> str:
        """[2026-08-23] 注入记忆整理 user 消息的玩家身份行（含指代规则）；未绑返回空串。"""
        pid = self._fmt_player_identity(world)
        if not pid:
            return ""
        return (f"玩家身份：{pid}（素材中的「你」即该玩家。记忆提及玩家一律用此姓名/形象，"
                "不要用「那个男人/那位年轻人/白衣人」之类泛称。）\n")

    def _consolidate_summary(self, npc, world, text, data, preset, cancel_check) -> bool:
        api = self._resolve_llm_api(preset)
        if not api:
            return False
        sys_prompt = preset.npc_memory_summary_prompt or DEFAULT_NPC_MEMORY_SUMMARY_PROMPT
        tmp = Preset(name="npc_mem_summary", system_prompt=sys_prompt.replace("{{char_name}}", npc.name),
                     temperature=0.3, max_tokens=10000, top_p=0.9)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())
        cur = data.get("summary", "")
        profile = self._fmt_npc_profile(npc, world)
        pid_line = self._player_identity_line(world)
        user = (f"{_world_anchor(world)}\n{profile}\n{pid_line}现有记忆：\n{cur or '（无）'}\n\n{text}\n\n请输出整理后的记忆段落。")
        messages = [{"role": "system", "content": tmp.system_prompt}, {"role": "user", "content": user}]
        if cancel_check and cancel_check():
            return False
        result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
        if result.cancelled or result.error or not (result.content or "").strip():
            return False
        data["summary"] = result.content.strip()
        return True

    def _consolidate_hybrid(self, npc, world, text, data, preset, cancel_check) -> bool:
        api = self._resolve_llm_api(preset)
        if not api:
            return False
        sys_prompt = preset.npc_memory_hybrid_prompt or DEFAULT_NPC_MEMORY_HYBRID_PROMPT
        tmp = Preset(name="npc_mem_hybrid", system_prompt=sys_prompt,
                     temperature=0.3, max_tokens=10000, top_p=0.9)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())
        entries = data.get("entries", []) if isinstance(data.get("entries"), list) else []
        existing = "\n".join(f"[seq{e.get('seq', 0)}] {e.get('detail', '')}" for e in entries[-20:]) or "（无）"
        profile = self._fmt_npc_profile(npc, world)
        pid_line = self._player_identity_line(world)
        user = (f"{_world_anchor(world)}\n{profile}\n{pid_line}已有记忆条目：\n{existing}\n\n{text}\n\n请输出新增记忆条目（每行一条，[triggers: ...] 明细）。")
        messages = [{"role": "system", "content": tmp.system_prompt}, {"role": "user", "content": user}]
        if cancel_check and cancel_check():
            return False
        result = llm.chat_cancelable(messages, cancel_check=cancel_check, block_labels=["系统", "用户"])
        if result.cancelled or result.error or not (result.content or "").strip():
            return False
        new_lines = self._parse_hybrid(result.content)
        if not new_lines:
            return False
        # [P29] 追加+embedding+trim 抽成 _append_hybrid_entries（与 consolidate_fold 共用）
        self._append_hybrid_entries(npc, data, new_lines)
        return True

    @staticmethod
    def _parse_hybrid(text: str) -> list[tuple[list[str], str]]:
        out = []
        for line in (text or "").splitlines():
            m = _HYBRID_LINE.search(line.strip())
            if not m:
                continue
            trig = [w.strip() for w in m.group(1).replace("，", ",").split(",") if w.strip()][:4]
            detail = m.group(2).strip()
            if detail and trig:
                out.append((trig, detail))
        return out

    def _append_hybrid_entries(self, npc, data, new_lines: list) -> None:
        """[P29] hybrid 模式追加条目 + embedding + trim 500（_consolidate_hybrid 与
        consolidate_fold 共用尾部，避免逻辑分裂）。原地改 data["entries"]。"""
        entries = data.get("entries", []) if isinstance(data.get("entries", []), list) else []
        seq = max([int(e.get("seq", 0)) for e in entries], default=0)
        emb_api = self._resolve_emb_api()
        emb_client = None
        if emb_api is not None:
            from src.services.embedding_client import EmbeddingClient
            emb_client = EmbeddingClient(emb_api)
        try:
            col = self._get_collection(npc.id) if emb_client is not None else None
        except Exception:
            col = None
        for trig, detail in new_lines:
            seq += 1
            entries.append({"seq": seq, "triggers": trig, "detail": detail})
            if col is not None:
                try:
                    vec = emb_client.embed(detail)
                    if vec:
                        import time
                        col.upsert(ids=[f"npcmem_{seq}_{time.time()}"],
                                   embeddings=[vec], documents=[detail],
                                   metadatas=[{"npc_id": npc.id, "seq": seq, "triggers": trig}])
                except Exception:
                    pass
        if len(entries) > 500:
            entries = entries[-500:]
        data["entries"] = entries

    @staticmethod
    def _parse_batch(content: str, candidate_names: list) -> list:
        """[P29] 解析批量整理输出：按 `=== NPC名 ===` 切块，匹配候选名。
        返回 [(npc_name, block_text), ...]，未匹配候选名的块丢弃（容错）。"""
        if not content:
            return []
        name_set = set(candidate_names)
        blocks = []
        cur_name = None
        cur_lines = []
        for line in content.splitlines():
            m = re.match(r"^={2,}\s*(.+?)\s*={2,}\s*$", line.strip())
            if m:
                # 收尾上一块
                if cur_name is not None and cur_name in name_set:
                    blocks.append((cur_name, "\n".join(cur_lines).strip()))
                cur_name = m.group(1).strip()
                cur_lines = []
            else:
                cur_lines.append(line)
        # 收尾最后一块
        if cur_name is not None and cur_name in name_set:
            blocks.append((cur_name, "\n".join(cur_lines).strip()))
        return blocks

    def consolidate_fold(self, world: World, scene: SceneLog, preset: WorldSimPreset,
                         cancel_check: Optional[Callable[[], bool]] = None,
                         folded_entries: list = None, npc_summary: str = "") -> dict:
        """[P29] 折叠时批量整理在场 NPC 记忆（1 次 LLM 调用出 N 个 NPC 的记忆增量）。

        互补于每回合 talk_to 单 NPC 即时整理：折叠时用待删旧条目的多回合窗口 + 各 NPC
        既有记忆 + 档案，一次调用出 N 个 NPC 的记忆增量，拆分后逐 NPC 写入。这是对"即将
        被 compress_scene_history 删除的旁白"的最后抢救——非 talk_to NPC 补覆盖面，
        talk_to NPC 用跨回合长上下文重整已有记忆（方案 A：质量升级非纯冗余）。

        folded_entries = 待折叠的旧 SceneEntry 列表（已从 scene.log 删除，引用独立不失效）。
        npc_summary = 折叠刚产出的 scene.summary（作上下文锚）。
        返回 {"ok": bool, "consolidated": [npc_name,...], "usage": LlmUsage|None}。
        best-effort：失败/取消/无候选/无 API 返回 {"ok": False, "consolidated": [], "usage": None}，
        不抛异常（折叠 summary 已成功，记忆整理是额外收益不回滚）。
        """
        mode = self._mode(preset)
        if mode == "off" or not world:
            return {"ok": False, "consolidated": [], "usage": None}
        # 候选：在场 + 存活 + 非敌对（cap 8 省 token；超出下轮折叠再覆盖）
        ploc = getattr(world.player, "location_id", "") if world.player else ""
        candidates = [n for n in (world.npcs or [])
                      if getattr(n, "alive", True) and not getattr(n, "hostile", False)
                      and getattr(n, "location_id", "") == ploc][:8]
        if not candidates or not folded_entries:
            return {"ok": False, "consolidated": [], "usage": None}
        api = self._resolve_llm_api(preset)
        if not api:
            return {"ok": False, "consolidated": [], "usage": None}

        # 选 prompt（mode 二选一；_mode 已保证 summary/embedding_hybrid）
        if mode == "summary":
            sys_prompt = preset.npc_memory_batch_summary_prompt or DEFAULT_NPC_MEMORY_BATCH_SUMMARY_PROMPT
        else:
            sys_prompt = preset.npc_memory_batch_hybrid_prompt or DEFAULT_NPC_MEMORY_BATCH_HYBRID_PROMPT
        tmp = Preset(name="npc_mem_batch", system_prompt=sys_prompt,
                     temperature=0.3, max_tokens=10000, top_p=0.9)
        llm = LlmClient(api, tmp, jailbreak_prefix=self._jb_prefix())

        # 构造 user 消息：候选 NPC 档案 + 既有记忆 + 折叠条目窗口 + 前情摘要
        cand_names = [n.name for n in candidates]
        npc_segs = []
        for n in candidates:
            profile = self._fmt_npc_profile(n, world)
            data = self._load(world.id, n.id)
            if mode == "summary":
                mem = str(data.get("summary", "") or "") or "（无）"
            else:
                entries = data.get("entries", []) if isinstance(data.get("entries", []), list) else []
                mem = "\n".join(f"[seq{e.get('seq', 0)}] {e.get('detail', '')}"
                                for e in entries[-20:]) or "（无）"
            npc_segs.append(f"--- NPC：{n.name} ---\n{profile}\n现有记忆：\n{mem}")
        # 折叠条目窗口（带 talk_to 标注）
        entry_lines = []
        for e in folded_entries:
            tag = {"player": "玩家", "narrator": "旁白", "system": "系统"}.get(e.role, e.role)
            talk = (e.meta or {}).get("talk_to", "") if hasattr(e, "meta") else ""
            talk_tag = f" 对话:{talk}" if talk else ""
            content = scrub_npc_cmd_head(e.content or "").strip()
            if not content:
                continue
            entry_lines.append(f"[{tag}|回合{e.tick}{talk_tag}] {content[:400]}")
        pid_line = self._player_identity_line(world)
        user_msg = (
            f"{_world_anchor(world)}\n\n"
            f"【候选 NPC 列表】{'、'.join(cand_names)}\n\n"
            + (f"【玩家身份】\n{pid_line}\n" if pid_line else "")
            + "\n\n".join(npc_segs)
            + "\n\n【前情摘要】\n" + (npc_summary or "（无）")
            + "\n\n【待整理的场景记录】（按时间从旧到新，含当时对话对象标注）\n"
            + "\n".join(entry_lines)
            + "\n\n请按格式为每个候选 NPC 输出记忆。"
        )
        if cancel_check and cancel_check():
            return {"ok": False, "consolidated": [], "usage": None}
        result = llm.chat_cancelable(
            [{"role": "system", "content": tmp.system_prompt},
             {"role": "user", "content": user_msg}],
            cancel_check=cancel_check, block_labels=["系统", "用户"],
        )
        if result.cancelled or result.error or not (result.content or "").strip():
            return {"ok": False, "consolidated": [], "usage": result.usage}
        # 解析 + 逐 NPC 写入（部分写入：单 NPC 失败不影响其他）
        blocks = self._parse_batch(result.content, cand_names)
        name_to_npc = {n.name: n for n in candidates}
        consolidated = []
        for npc_name, block_text in blocks:
            npc = name_to_npc.get(npc_name)
            if npc is None or not block_text:
                continue
            data = self._load(world.id, npc.id)
            wrote = False
            if mode == "summary":
                if block_text and block_text != "无新增":
                    data["summary"] = block_text
                    wrote = True
            else:  # embedding_hybrid
                new_lines = self._parse_hybrid(block_text)
                if new_lines:
                    self._append_hybrid_entries(npc, data, new_lines)
                    wrote = True
            if wrote:
                data["npc_id"] = npc.id
                data["world_id"] = world.id
                self._save(world.id, npc.id, data)
                consolidated.append(npc_name)
        return {"ok": True, "consolidated": consolidated, "usage": result.usage}

    # ============ 召回 ============
    def recent_lines(self, world, npc, limit: int = 3) -> list:
        """[记忆驱动 2026-08-29] 无 LLM 直读最近记忆（决策注入专用）：

        hybrid 模式取最后 limit 条 detail（各截 80 字）；summary 模式按句末标点
        （。！？；;）切句取末 limit 句（各截 80 字）。无记忆返回 []。纯读不写，
        不触发整理。
        [!] 旧版按「；」切分且每段硬截 60 字——段落记忆只有偶然写分号才切段，
        否则整段退化为前 60 字半句（真机存档 5 份 4 份无分号）；改按句切后
        不依赖 LLM 写作习惯（2026-09-08）。
        """
        data = self._load(getattr(world, "id", ""), getattr(npc, "id", ""))
        # 口径注记：不查 _mode（off 模式残留旧文件时仍会注入）——off 是用户显式
        # 关闭召回，但既有记忆内容真实存在过，注入决策无害且免传 preset。
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        if entries:
            out = []
            for e in reversed(entries[-max(1, int(limit or 3)):]):
                d = str((e or {}).get("detail", "") or "").strip()
                if d:
                    out.append(d[:80])
            return out
        summ = str(data.get("summary", "") or "").strip()
        if not summ:
            return []
        segs = [s.strip() for s in _SENT_RE.split(summ) if s.strip()]
        out = []
        for s in segs[-max(1, int(limit or 3)):]:
            # 空白压平（旧 5 分类存档条目含换行）+ 截 80 + 去尾标点防与注入连接符重复
            t = " ".join(s.split())[:80].rstrip("。！？!?；;，,、")
            if t:
                out.append(t)
        return out

    def recall(self, npc: NPC, world: World, query: str, preset: WorldSimPreset,
               top_k: Optional[int] = None) -> str:
        """召回该 NPC 的记忆文本（注入场景上下文用）。off/无记忆返回空串。"""
        mode = self._mode(preset)
        if mode == "off" or not npc or not world:
            return ""
        data = self._load(world.id, npc.id)
        k = int(top_k or getattr(preset, "npc_memory_top_k", 5) or 5)
        if mode == "summary":
            return scrub_npc_cmd_head(str(data.get("summary", "") or ""))
        # embedding_hybrid：emb 召回 top-K，失败回退最近 N
        entries = data.get("entries", []) if isinstance(data.get("entries"), list) else []
        if not entries:
            return ""
        emb_api = self._resolve_emb_api()
        if emb_api is not None:
            try:
                from src.services.embedding_client import EmbeddingClient
                vec = EmbeddingClient(emb_api).embed(query or npc.name)
                if vec:
                    col = self._get_collection(npc.id)
                    res = col.query(query_embeddings=[vec], n_results=min(k, len(entries)))
                    docs = (res.get("documents") or [[]])[0]
                    if docs:
                        return self._with_chat_excerpt(
                            scrub_npc_cmd_head("\n".join(docs)), world, npc)
            except Exception:
                pass
        # 回退：最近 K 条（按 seq 倒序取后取前 K，按时序输出）
        recent = sorted(entries, key=lambda e: int(e.get("seq", 0)))[-k:]
        return self._with_chat_excerpt(
            scrub_npc_cmd_head("\n".join(e.get("detail", "") for e in recent if e.get("detail"))),
            world, npc)

    def _with_chat_excerpt(self, base: str, world, npc) -> str:
        """[A3 2026-08-28] 私聊摘录并入召回文本：NPC 在回合上下文/私聊里「记得」
        最近与玩家聊过什么（此前私聊只活在私聊界面，语义零渗透进回合）。

        取最近 4 条（每条截 80 字）；无记录原样返回。best-effort。"""
        try:
            chat_path = os.path.join(paths.npc_chat_dir(world.id), f"{npc.id}.json")
            if not os.path.exists(chat_path):
                return base
            msgs = json.load(open(chat_path, encoding="utf-8"))
            if not isinstance(msgs, list):
                return base
            lines = []
            for m in msgs[-4:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = str(m.get("content", "") or "").strip()
                if not content or role not in ("user", "assistant"):
                    continue
                who = "玩家" if role == "user" else (npc.name or "对方")
                lines.append(f"{who}：{content[:80]}")
            if not lines:
                return base
            excerpt = "\n【私聊摘录】（最近与玩家的私聊，据实记得）\n" + "\n".join(lines)
            return (base + "\n" + excerpt) if base else excerpt
        except Exception:
            return base

    # ============ 查看（档案页用）============
    def get_memory_info(self, npc_id: str, world_id: str) -> dict:
        data = self._load(world_id, npc_id)
        return {
            "summary": data.get("summary", "") or "",
            "entries": list(data.get("entries", []) or []),
            "count": int(data.get("count", 0) or 0),
        }

    # ============ 清理 ============
    def clear_npc_memory(self, world_id: str, npc_id: str):
        try:
            os.remove(self._mem_path(world_id, npc_id))
        except OSError:
            pass
        try:
            self._get_chroma().delete_collection(_collection_name(npc_id))
        except Exception:
            pass

    def clear_world_memory(self, world: World):
        """删世界所有 NPC 记忆（JSON + ChromaDB）。delete_world 编排路径调用。"""
        if not world:
            return
        for npc in world.npcs:
            self.clear_npc_memory(world.id, npc.id)
