"""场景交互 worker（QThread）：驱动单回合 settle -> apply -> narrate（流式）。

仿 WorldGenWorker（QThread + finished_signal + _cancelled + cancel_check）+
ChatWorker（chunk 信号透传流式文本）。

[!] 守 §5 取消透传：settle/narrate 全走 cancel_check；停止保留已收旁白。
[!] 守 §15 worker 生命周期：场景页 back/close 须 disconnect + cancel + wait。

worker 在子线程做 LLM 调用 + 引擎结算（纯 Python 数据操作，world/scene 无 Qt 依赖）；
storage.save_scene/save_world 与 UI 更新在主线程 finished 槽做（DB 锁 + Qt 对象主线程约束）。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from src.models import WorldSimPreset, World, SceneLog
from src.utils.helpers import remove_image_tags
from src.utils.debug import debug_log

# [修 2026-09-05] 记忆整理门控：这些意图类型的 talk_to 不是对话对象（gather 填的是
# 资源点/尸体名、use_item/move/observe/wait 无对象），不触发 NPC 记忆整理。
_MEMORY_SKIP_INTENTS = {"gather", "use_item", "move", "observe", "wait"}
# [!] gift 不在跳过表：送礼是重要 NPC 互动，记忆整理照常（引擎已真结算，记忆与背包同源）


def _memory_target_npc(world: World, intent, svc) -> object:
    """[修 2026-09-05] 本回合记忆整理的目标 NPC（无则 None）。

    门控：行动已结算成功、intent_type 须是 NPC 交互类、talk_to 非空、
    按名可解析且 NPC 存活并与玩家同场所。原实现只查 talk_to 名字——真人测试「搜刮
    赤炎狂徒」：settle 给 gather 填了 talk_to=尸体名，引擎照走记忆整理，给死人烧了
    一整通 LLM（模型答「死者记忆至此中断」）白耗 token 还写垃圾记忆条目。
    hostile 不拦（敌对 NPC 记仇是正当记忆）。
    """
    if not intent or not intent.get("resolved", True):
        return None
    if str(intent.get("intent_type", "") or "") in _MEMORY_SKIP_INTENTS:
        return None
    talk_name = str(intent.get("talk_to") or "").strip()
    if not talk_name:
        return None
    # [修 2026-09-10] 名字走 name_resolver 容错解析（守 [P44]）：LLM 产出名常带敬称/别字，
    # 精确等值会静默漏命中 -> 本回合记忆整理被跳过。锚定真实名单，编造名不追认。
    from src.services import name_resolver as nrs
    hit = nrs.resolve_name(talk_name, [n.name for n in world.npcs])
    npc = next((n for n in world.npcs if n.name == hit), None) if hit else None
    if (npc is None or not getattr(npc, "alive", True)
            or not svc._same_place_as_player(world, npc)):
        return None
    return npc


def _apply_engine_veto(intent: dict, summary: dict, llm_resolved: bool,
                       llm_hint: str = "") -> None:
    """[修 2026-09-05] 引擎否决补丁：LLM 判 resolved=true 但引擎结算翻 false 时，
    LLM 的 reason/effects/narration_hint 都是按「行动能成」写的——原样透传旁白会
    产生「行动受阻但写着可行/捡到战利品」的自相矛盾上下文（真人测试：搜刮尸体，
    引擎一分钱没给、旁白照 LLM 编的 effects 写「纳入背包」，玩家以为捡到了东西）。
    此处以引擎 reason 覆写（受阻块读 intent["reason"]）、作废按成功写的 effects；
    hint 用 apply_intent 前的快照 llm_hint 判归属：与快照相同=引擎没写过（作废）、
    以快照为前缀=引擎尾部追加过内容（野外怪拦路等，剥前缀只留引擎段）、其余保留
    （llm_hint 为空时现有 hint 全部来自引擎）。LLM 自判受阻（其 effects 本就按受阻
    场景写）与引擎放行的回合不在此列。
    """
    if llm_resolved and not intent.get("resolved", True):
        if summary.get("reason"):
            intent["reason"] = summary["reason"]
        intent["effects"] = []
        cur = str(intent.get("narration_hint") or "")
        if cur == llm_hint:
            intent["narration_hint"] = ""
        elif llm_hint and cur.startswith(llm_hint):
            intent["narration_hint"] = cur[len(llm_hint):].lstrip("；;，, |")


class SceneWorker(QThread):
    """单回合场景交互后台任务。

    信号：
      stage(str)                - 当前阶段（"结算中…" / "生成旁白…"）
      chunk(str)                - 叙事流式文本片段
      usage(str, object)        - (api_id, LlmUsage) 计费统计
      error(str)                - 错误信息
      finished_signal(bool, dict) - True+payload / False+err_dict
        payload = {
          "is_intro": bool,          # 是否首回合开场
          "narration": str,          # 旁白全文（取消时为已收部分）
          "options": list,           # 恒 []（[用户指示 2026-09-05] 每回合生成选项已移除）
          "intent": dict|None,       # 结算意图（intro 时为 None）
          "settle_summary": dict,    # apply_intent 返回的结算摘要
          "tick_report": dict,       # P4 tick_world 返回的世界滴答报告（intro 时为空 dict）
          "cancelled": bool,
          "speech_updated": bool,    # [P20] 本回合是否补生成过 NPC 说话腔调（主线程据此落盘）
        }
        err_dict = {"msg": str, "speech_updated": bool}   # 错误信息 + 腔调是否已补（需落盘防重触发）

    构造：
      svc, world, scene, preset 必传；player_action=None 表示首回合开场。
    """

    stage = Signal(str)
    chunk = Signal(str)
    usage = Signal(str, object)
    error = Signal(str)
    # P2 场景事件生图：narration 完成后提取 [img:...] 出图，逐张 emit image_filename
    # （独立于 ChatOrchestrator.on_image，仿 §21c 独立铁律）
    image_generated = Signal(str)
    finished_signal = Signal(bool, object)

    def __init__(self, world_sim_service, world: World, scene: SceneLog,
                 preset: WorldSimPreset, player_action=None, preset_intent=None,
                 turn_snapshot=None, parent=None):
        super().__init__(parent)
        self.svc = world_sim_service
        self.world = world
        self.scene = scene
        self.preset = preset
        self.player_action = player_action      # None = 首回合 intro
        # [P7g] 预设意图：非空时跳过 settle LLM，直接 apply_intent（UI 快捷行动如采集用，
        # 让 intent_type=gather 可靠走引擎分支，不依赖 settle LLM 决策）。
        self.preset_intent = preset_intent
        # [断网韧性 P45(3)] 回合事务性快照：(world.to_dict(), scene.to_dict())，由主线程
        # 在启动 worker 前（玩家条目已入 scene 后）拍摄。回合内 LLM 全链（settle+narrate）
        # 任一失败 -> _restore_snapshot 原地恢复（apply_intent 的引擎修改一并撤销）+
        # rolled_back=True 上报，主线程不落盘、stash 失败回合供右键「重试本回合」。
        # 取消路径不回滚（守 §5：中断保留已收旁白与结算）。
        self.turn_snapshot = turn_snapshot
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _restore_snapshot(self) -> bool:
        """[断网韧性] 按快照原地恢复 world/scene（保持对象引用不变——所有持有方
        （场景页/服务层）无需重绑）。World/SceneLog 是纯数据 dataclass，
        __dict__.update(from_dict 重建对象) 等价整体替换字段。
        返回是否恢复成功（无快照/恢复异常返回 False——调用方据此上报
        rolled_back=False，主线程退化为 _tick_before 漂移落盘兜底）。"""
        if self.turn_snapshot is None:
            return False
        try:
            from src.models import SceneLog as _SL
            w2 = World.from_dict(self.turn_snapshot[0])
            s2 = _SL.from_dict(self.turn_snapshot[1])
            self.world.__dict__.update(w2.__dict__)
            self.scene.__dict__.update(s2.__dict__)
            return True
        except Exception as e:  # noqa: BLE001 - 回滚失败退化为旧兜底路径
            debug_log(lambda: f"[SceneWorker] 快照回滚失败（走漂移落盘兜底）: {e}")
            return False

    def _fail_transactional(self, msg: str, speech_updated: bool):
        """[断网韧性] 失败统一出口：尽力回滚 + 上报 rolled_back 实况。
        回滚成功 -> speech 更新一并撤销（报 False）；退化路径保留 speech_updated
        信号（主线程据此落盘，守 P20 腔调幂等契约）。"""
        restored = self._restore_snapshot()
        self.finished_signal.emit(False, {
            "msg": msg,
            "speech_updated": (False if restored else speech_updated),
            "rolled_back": restored,
        })

    def run(self):
        # [P20] speech_updated 在 try 外初始化，确保 except 块也能安全引用（防异常发生在
        # 赋值前 NameError）；ensure 在 try 内赋值，主线程据此落盘防腔调重触发。
        speech_updated = False
        is_intro = False    # try 外初始化（except 分支需判断：intro 失败不回滚）
        try:
            # [P20] 老世界 NPC 说话腔调补生成（一次批量 LLM，幂等，QThread 内不阻塞 UI）。
            # 只在有缺失腔调时触发，补完即永不触发；失败不阻断回合（下回合再试）。
            # [!] 补完的腔调必须落盘，否则重开世界后 from_dict 读到空又触发补生成，
            # 违反「补完永不触发」幂等契约（纯旁白/聊天回合无 moved/damage/gather 等
            # 事件时不触发 save_world，腔调只存内存）。返回值经 finished_signal 传主线程，
            # _on_finished 据此强制 save_world。
            if not self._cancelled:
                try:
                    speech_updated = self.svc.ensure_npc_speech_styles(
                        self.world, self.preset, cancel_check=lambda: self._cancelled)
                except Exception:
                    pass  # 补生成失败不阻断回合

            is_intro = self.player_action is None and not self.scene.log

            if is_intro:
                # ---- 首回合：只生成开场旁白 ----
                # [修 2026-09-13 用户指示] 首回合不再调用 settle_action「结算初始选项」：
                # rev 44（2026-09-05）移除选项功能后，该调用产出的 intent 无人消费
                # （首回合无玩家条目，talk_to 归因无处回填），纯烧一次结算 LLM。
                self.stage.emit("生成开场旁白…")
                narration, nerr, nusage = self.svc.narrate_intro(
                    self.world, self.scene, self.preset,
                    on_chunk=lambda t: self.chunk.emit(t),
                    cancel_check=lambda: self._cancelled,
                )
                if nusage is not None:
                    api = self.svc._resolve_api(self.preset.narrative_api_id or self.preset.calculator_api_id)
                    if api:
                        self.usage.emit(api.id, nusage)
                if self._cancelled:
                    self.finished_signal.emit(True, {
                        "is_intro": True, "narration": narration, "options": [],
                        "intent": None, "settle_summary": {}, "tick_report": {}, "cancelled": True,
                        "speech_updated": speech_updated,
                    })
                    return
                if nerr and nerr != "已取消":
                    self.error.emit(nerr)
                    # 旁白失败仍进入世界，不阻断（玩家自由输入行动即可）
                    if not narration:
                        narration = "（开场旁白生成失败，但你可以开始探索。）"

                # 场景事件生图（独立于 ChatOrchestrator，§21c 独立铁律）
                # 取消时不生图（节约资源），已 emit 的 chunk 不受影响
                if not self._cancelled and narration and self.preset.narrative_image_event:
                    self.stage.emit("生成场景插图…")
                    image_paths = self.svc.process_scene_images(
                        narration, self.world, self.preset,
                        cancel_check=lambda: self._cancelled,
                    )
                    for img_path in image_paths:
                        self.image_generated.emit(img_path)
                # 始终剔除 [img:...] 标签（守 §12 契约）
                if narration:
                    narration = remove_image_tags(narration).strip()
                    if not narration:
                        narration = "（开场旁白为空。）"

                self.finished_signal.emit(True, {
                    "is_intro": True, "narration": narration, "options": [],
                    "intent": None, "settle_summary": {}, "tick_report": {}, "cancelled": False,
                    "speech_updated": speech_updated,
                })
                return

            # ---- 普通回合：settle -> apply -> narrate ----
            if self.preset_intent is not None:
                # [P7g] 预设意图（UI 快捷行动如采集）：跳过 settle LLM，直接用预设 intent
                intent = dict(self.preset_intent)
                intent.setdefault("resolved", True)
                intent.setdefault("intent_type", "custom")
                self.stage.emit("结算行动…")
            else:
                self.stage.emit("结算行动…")
                intent, serr = self.svc.settle_action(
                    self.world, self.scene, self.player_action, self.preset,
                    cancel_check=lambda: self._cancelled,
                )
                if self._cancelled:
                    # 取消也走 ok 路径（与 intro 取消一致），保留已收旁白，不弹错误框
                    self.finished_signal.emit(True, {
                        "is_intro": False, "narration": "", "options": [],
                        "intent": None, "settle_summary": {}, "tick_report": {}, "cancelled": True,
                        "speech_updated": speech_updated,
                    })
                    return
                if intent is None:
                    # [断网韧性] settle 失败：回滚快照（撤销本轮 speech 补生成等任何先行
                    # 修改），rolled_back 上报（主线程不落盘 + stash 重试）
                    self._fail_transactional(serr or "行动结算失败", speech_updated)
                    return

            # 引擎结算（纯 Python，改 world/scene）
            # [修 2026-09-05] 引擎否决补丁须在 apply_intent 之后、narrate 之前
            # （LLM 按成功写的 reason/effects/hint 不得进旁白上下文）
            _llm_resolved = bool(intent.get("resolved", True))
            _llm_hint = str(intent.get("narration_hint") or "")
            settle_summary = self.svc.apply_intent(self.world, self.scene, intent, self.preset)
            _apply_engine_veto(intent, settle_summary, _llm_resolved, _llm_hint)

            # 叙事旁白（流式）
            # [!] [P10c] 世界滴答在 narrate 之后（旧序 tick->narrate 有时序缺陷）：
            # settle 意图与 narrate 上下文必须基于同一世界快照——旧序下 tick 在两者之间
            # 挪动 NPC/产生事件，narrate 的【在场 NPC】与结算现场脱节，旁白会出现
            # 不在当前地点的人物（离屏要角动态被织入现场描写）。tick 移到旁白后，
            # 世界演化下一回合的上下文才体现（晚一拍但一致），旁白流式也更早出字
            # （不用先等 tick 的 LLM 调用）；取消回合不跑 tick（世界不推进，守 §5 语义）。
            self.stage.emit("生成旁白…")
            narration, nerr, nusage = self.svc.narrate_outcome(
                self.world, self.scene, self.player_action, intent, self.preset,
                on_chunk=lambda t: self.chunk.emit(t),
                cancel_check=lambda: self._cancelled,
            )
            if nusage is not None:
                api = self.svc._resolve_api(self.preset.narrative_api_id or self.preset.calculator_api_id)
                if api:
                    self.usage.emit(api.id, nusage)
            if self._cancelled:
                # 取消保留已收旁白（守 §5），走 ok 路径不弹错误框；不跑 tick（回合未完成不推进世界）
                self.finished_signal.emit(True, {
                    "is_intro": False, "narration": narration, "options": [],
                    "intent": intent, "settle_summary": settle_summary,
                    "tick_report": {}, "cancelled": True,
                    "speech_updated": speech_updated,
                })
                return

            # [断网韧性 P45(3)] 叙事失败 = 回合未完成：回滚快照（settle 的 apply_intent
            # 引擎修改一并撤销）+ 不跑 tick 不落盘，主线程 stash 失败回合供右键重试。
            # 旧序（错误处理在 tick 后）会把「结算已生效但旁白是占位文本」的半截回合
            # 连同 tick 一起落盘——r4 断网 3 连败即此形态。部分流式正文一并丢弃
            # （不完整散文不该留在日志里，重试会重新生成完整旁白）。
            if nerr and nerr != "已取消":
                self.error.emit(nerr)
                self._fail_transactional(f"旁白生成失败：{nerr}", speech_updated)
                return

            # P4 世界滴答：旁白完成后跑离屏世界演化（经济/势力战/离屏NPC/校准）。
            # 产生的 event_log 下一回合进入场景上下文（narrate 已结束，本回合旁白基于结算现场）。
            # 规则部分即时不可取消，LLM 部分走 cancel_check。
            self.stage.emit("世界滴答…")
            tick_report = self.svc.tick_world(self.world, self.scene, self.preset,
                                              cancel_check=lambda: self._cancelled)

            # [!] 场景事件生图（独立于 ChatOrchestrator，§21c 独立铁律）
            # 取消时不生图（节约资源）；finished_signal 仍照常发（生图是后置增强，不影响回合状态）。
            # process_scene_images 在生图前内部已用 parse_image_tags 提取标签，但 narration
            # 原文仍含 [img:...] 字面量，落场景日志前剔除（与单聊/群聊的 remove_image_tags
            # 行为一致，守 §12 契约）。
            if not self._cancelled and narration and self.preset.narrative_image_event:
                self.stage.emit("生成场景插图…")
                image_paths = self.svc.process_scene_images(
                    narration, self.world, self.preset,
                    cancel_check=lambda: self._cancelled,
                )
                for img_path in image_paths:
                    self.image_generated.emit(img_path)
            if narration:
                # 始终剔除 [img:...] 标签（即使场景生图关闭，叙事 LLM 仍可能输出 [img:...]）
                narration = remove_image_tags(narration).strip()
                if not narration:
                    narration = "（旁白为空。）"

            # [P6c] NPC 个人记忆整理：本回合若与某 NPC 交谈/互动（intent.talk_to），
            # 把「结构化记忆输入」交给 NpcMemoryService 整理（攒满 interval 触发，LLM 自分类合并）。
            # 走 cancel_check 可取消；失败/取消不阻断回合（best-effort）。
            # [P23] settle LLM 判定 memory_worthy=true 的关键事件 force 触发（绕 interval）。
            # [P23 阶段一] 记忆输入重构：不再单喂旁白全文（第二人称全知视角，含该 NPC 感知
            # 不到的信息 + 玩家言辞因叙事规则2缺位），改用 build_structured_recent_text 产出
            # 三段结构化（玩家言行/NPC言行/客观事件）+ 旁白全文降级附注，防 OOC/漏剧情/漏细节。
            try:
                if (not self._cancelled and getattr(self.preset, "npc_memory_enabled", True)
                        and intent):
                    # 只为已完成且当面发生的互动整理记忆；死人、远处人物和受阻行动跳过。
                    npc = _memory_target_npc(self.world, intent, self.svc)
                    if npc is not None:
                        recent_text = self.svc.npc_memory().build_structured_recent_text(
                            intent, self.player_action, narration, npc.name)
                        self.stage.emit("整理 NPC 记忆…")
                        self.svc.npc_memory().check_and_update(
                            npc, self.world, self.scene, self.preset,
                            cancel_check=lambda: self._cancelled, recent_text=recent_text)
                        # [P42b 用户指示 2026-08-23] 移除 memory_worthy LLM 决定整理时机
                        # ——整理节奏纯 interval 确定（何时总结不该由 LLM 拍脑袋）
            except Exception:
                pass  # 记忆整理失败不影响回合

            # [P15a1] 场景日志滚动总结：log 超阈值时把最老条目压成前情摘要（子线程 LLM
            # 小调用）。失败/取消保留条目下轮重试；best-effort 不影响回合结果。
            # 阈值预检只为避免「整理前情摘要…」阶段提示闪现（权威判定在 service；
            # max(_th,12) 对齐 service 的 n=len-12>0 约束，阈值 <13 时不闪无效提示）。
            try:
                _th = int(getattr(self.preset, "scene_summary_threshold", 20) or 0)
                if (not self._cancelled and _th > 0
                        and len(self.scene.log) > max(_th, 12)):
                    self.stage.emit("整理前情摘要…")
                    cok, cusage = self.svc.compress_scene_history(
                        self.world, self.scene, self.preset,
                        cancel_check=lambda: self._cancelled)
                    if cusage is not None:
                        api = self.svc._resolve_api(self.preset.calculator_api_id)
                        if api:
                            self.usage.emit(api.id, cusage)
            except Exception:
                pass

            self.finished_signal.emit(True, {
                "is_intro": False, "narration": narration, "options": [],
                "intent": intent, "settle_summary": settle_summary,
                "tick_report": tick_report, "cancelled": self._cancelled,
                "speech_updated": speech_updated,
            })
        except Exception as e:
            # [断网韧性] 内部异常同样回滚（world 可能改到一半；尽力恢复快照态）。
            # [!] intro 回合不回滚：intro 的快照是空场景，回滚会让玩家失去重开入口
            if is_intro:
                self.finished_signal.emit(False, {
                    "msg": f"内部错误：{e}", "speech_updated": speech_updated,
                    "rolled_back": False,
                })
            else:
                self._fail_transactional(f"内部错误：{e}", speech_updated)
