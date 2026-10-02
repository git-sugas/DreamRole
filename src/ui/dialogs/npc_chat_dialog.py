"""[P10] 好友 NPC 私聊对话框 + 后台 worker。

私聊链路：NpcChatDialog（QDialog，流式 UI）-> NpcChatWorker(QThread) ->
svc.npc_private_chat（叙事 LLM，人设/交情/NPC 记忆注入）。历史持久化走
storage.load_npc_chat / save_npc_chat（data/worlds/{id}_npc_chat/{npc_id}.json）。

守 §15 worker 生命周期：closeEvent/reject disconnect + cancel + wait；
守 §5 取消透传（cancel_check）。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal, Qt, QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QWidget,
)

from src.config import paths
from src.ui.widgets.card_grid import make_card_pixmap
from src.services import npc_reaction_engine as nre
from src.utils.debug import debug_log


class NpcChatWorker(QThread):
    """[P10] NPC 私聊后台任务（叙事 LLM 流式）。"""

    chunk = Signal(str)
    usage = Signal(str, object)
    error = Signal(str)
    finished_signal = Signal(bool, object)   # True+{"text": str} / False+error_str

    def __init__(self, svc, world, npc, history, preset, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.npc = npc
        self.history = history
        self.preset = preset
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            text, err, usage = self.svc.npc_private_chat(
                self.world, self.npc, self.history, self.preset,
                on_chunk=lambda t: self.chunk.emit(t),
                cancel_check=lambda: self._cancelled,
            )
            if usage is not None:
                api = self.svc._resolve_api(
                    (self.preset.narrative_api_id or self.preset.calculator_api_id) if self.preset else "")
                if api:
                    self.usage.emit(api.id, usage)
            if err and err != "已取消":
                self.error.emit(err)
            self.finished_signal.emit(True, {"text": text, "cancelled": err == "已取消"})
        except Exception as e:
            self.finished_signal.emit(False, f"私聊失败：{e}")


class _ChatSummaryWorker(QThread):
    """[记忆化私聊 2026-08-29] 关窗总结后台任务：本会话消息 + 既有记忆 -> LLM 合并
    入 NPC 记忆文件（best-effort，失败丢弃不阻断关窗）。"""

    finished_signal = Signal(bool, str)   # (ok, msg)

    def __init__(self, svc, world, npc, session, preset, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.world = world
        self.npc = npc
        self.session = session
        self.preset = preset

    def run(self):
        try:
            ok, msg = self.svc.summarize_chat_session(
                self.world, self.npc, self.session, self.preset)
            self.finished_signal.emit(bool(ok), str(msg))
        except Exception as e:  # noqa: BLE001
            from src.utils.debug import debug_log
            debug_log(lambda: f"[WorldSim] 私聊总结异常: {e}")
            self.finished_signal.emit(False, str(e))


class NpcChatDialog(QDialog):
    """[P10] 与好友 NPC 私聊（流式回复 + 历史持久化 + 交情 +1/次）。"""

    # [私聊单例 2026-09-07 用户报私聊历史丢失] 类级单例表 {(world_id, npc_id): dlg}：
    # 好友页/场景页/NPC 详情任一入口打开同 NPC 私聊都走同一个窗——双窗口各持一份
    # _session，新窗 session 为空，会把旧窗已发送的 user 消息挤掉（LLM 入参缺上文
    # = “对话被清空”观感）。open_for 负责判活/前置/新建三态。
    _open_dialogs: dict = {}

    @classmethod
    def open_for(cls, world, npc, world_sim_service, storage, preset, parent=None):
        """打开同 NPC 私聊窗（三态）：已存在且活着 -> 前置激活返回旧窗（不新建）；
        已关闭/已销毁 -> 新建；新建后记表，窗体 finished 时摘表。"""
        import shiboken6 as _sbk
        key = (str(getattr(world, "id", "") or ""),
               str(getattr(npc, "id", "") or ""))
        old = cls._open_dialogs.get(key)
        if old is not None and _sbk.isValid(old):
            try:
                old.show()
                old.raise_()
                old.activateWindow()
            except RuntimeError:
                pass
            else:
                return old, False       # 复用旧窗
        dlg = cls(world, npc, world_sim_service, storage, preset, parent=parent)
        cls._open_dialogs[key] = dlg
        try:
            dlg.finished.connect(
                lambda _r, _k=key: cls._open_dialogs.pop(_k, None))
        except (RuntimeError, TypeError):
            pass
        return dlg, True                # 新建

    def __init__(self, world, npc, world_sim_service, storage, preset, parent=None):
        super().__init__(parent)
        self.world = world
        self.npc = npc
        self.svc = world_sim_service
        self.storage = storage
        self.preset = preset
        self._worker: NpcChatWorker | None = None
        self._busy = False
        self._stream_started = False
        # [记忆化私聊 2026-08-29] 会话内消息数组（开窗空/仅载未读，关窗总结入记忆）
        self._session: list = []
        self._summary_worker = None   # [记忆化私聊] 关窗总结 worker 引用
        self.setWindowTitle(f"私聊：{npc.name}")
        self.resize(480, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)

        # 头部：金顶线卡（头像金环 + 大名 + 身份/交情徽章行）
        # [深改 2026-10-01] 旧头部浮在裸背景上与聊天区脱节；卡化 + 徽章化统一暗金语言
        from src.ui.widgets.game_widgets import GameCard, game_badge
        head_card = GameCard(gold=True)
        head = QHBoxLayout()
        head.setSpacing(10)
        av = QLabel()
        av.setFixedSize(48, 48)
        av.setPixmap(make_card_pixmap(npc.name, npc.avatar, paths.world_images_dir(), 48))
        av.setStyleSheet("border-radius:24px; border:2px solid #6b5a35;")
        head.addWidget(av)
        info = QVBoxLayout()
        info.setSpacing(4)
        nm = QLabel(f"【{npc.name}】")
        nm.setObjectName("bannerName")
        info.addWidget(nm)
        aff = int(getattr(npc, "affinity", 0))
        badge_row = QWidget()
        bh = QHBoxLayout(badge_row)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(6)
        bh.addWidget(game_badge(npc.role or "未知", "info"))
        bh.addWidget(game_badge(
            f"交情 {nre.affinity_level(aff)}（{aff}/100）", "gold"))
        bh.addStretch()
        info.addWidget(badge_row)
        head.addLayout(info, 1)
        head_card.content.addLayout(head)
        outer.addWidget(head_card)

        # 聊天记录（[深改] 暖深底 + 羊皮纸色 + 暗金细边，同战斗日志语言）
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet("QTextEdit{background:#15130e; color:#cfc6ae; "
                                    "border:1px solid #3f3722; border-radius:6px;}")
        outer.addWidget(self.log_edit, 1)

        # 输入行
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText(f"对 {npc.name} 说点什么…（回车发送）")
        self.input.returnPressed.connect(self._on_send)
        # [回车清空修复 2026-09-08] QLineEdit 对 Return 是 emit+ignore（事件继续
        # 传给 QDialog -> 点击默认按钮）。用户点过一次「清空记录」后它保持 autoDefault
        # 默认身份，之后每次回车 = 发送 + 点清空双触发（真机复现实锤：_on_send 与
        # _on_clear 同回车先后触发，session 被洗）。filter 消费回车事件掐断传播，
        # 全按钮关 autoDefault 双保险（同 chat_input 的 IME 前例）。
        self.input.installEventFilter(self)
        row.addWidget(self.input, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setAutoDefault(False)
        self.send_btn.setDefault(False)
        self.send_btn.setObjectName("primaryBtn")
        self.send_btn.clicked.connect(self._on_send)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setAutoDefault(False)
        self.stop_btn.setDefault(False)
        self.stop_btn.setObjectName("dangerBtn")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.hide()
        row.addWidget(self.send_btn)
        row.addWidget(self.stop_btn)
        outer.addLayout(row)

        # 底部：清空记录 + 关闭
        bottom = QHBoxLayout()
        clear_btn = QPushButton("清空记录")
        clear_btn.setAutoDefault(False)
        clear_btn.setDefault(False)
        clear_btn.clicked.connect(self._on_clear)
        bottom.addWidget(clear_btn)
        bottom.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        outer.addLayout(bottom)

        self._load_history()

    # ---------- 回车过滤（同 chat_input 的 IME 前例）----------
    def eventFilter(self, obj, event):
        """[回车清空修复 2026-09-08] 输入框回车：消费事件（防传播给 QDialog
        触发默认按钮点击——曾致「回车=发送+点清空记录」双触发洗掉聊天记录）。

        IME 组合态（拼音选词中）让输入法处理 Enter（确认候选词），不发送。
        """
        if obj is self.input and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                from PySide6.QtGui import QGuiApplication
                im = QGuiApplication.inputMethod()
                if im is not None and im.isVisible():
                    return False          # 组合态：交给输入法确认候选词
                self._on_send()
                return True               # 消费：不再传播给 dialog/默认按钮
        return super().eventFilter(obj, event)

    # ---------- 历史 ----------
    def _load_history(self):
        """[记忆化私聊 2026-08-29] 开窗只显示未读主动消息（A3 好友主动发来的），
        读完即清历史文件——每次开窗空窗体，NPC 记住的内容全部走关窗记忆总结。"""
        self.log_edit.clear()
        history = self.storage.load_npc_chat(self.world.id, self.npc.id)
        unread = [m for m in (history or [])
                  if isinstance(m, dict) and m.get("role") == "assistant"
                  and not m.get("read", False)]
        if unread:
            for m in unread:
                self.log_edit.append(f"{self.npc.name}：{m.get('content', '')}")
                self._session.append({"role": "assistant",
                                      "content": str(m.get("content", "") or "")})
                self.storage.save_npc_chat(self.world.id, self.npc.id, [])
        else:
            self.log_edit.append(
                f"（你与 {self.npc.name} 开始私聊。语气将贴合对方的性格与你们的交情。）")

    def _on_clear(self):
        self._session = []
        self.log_edit.clear()

    # ---------- 发送/流式 ----------
    def _set_busy(self, busy: bool):
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)
        if busy:
            self.stop_btn.show()
            self.send_btn.hide()
        else:
            self.stop_btn.hide()
            self.send_btn.show()

    def _on_send(self):
        if self._busy:
            return
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self._session.append({"role": "user", "content": text})
        self.log_edit.append(f"你：{text}")
        self._stream_started = False
        self._set_busy(True)

        self._worker = NpcChatWorker(self.svc, self.world, self.npc,
                                     list(self._session), self.preset, parent=None)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.error.connect(self._on_error)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_chunk(self, text: str):
        # [野 worker 守卫 2026-09-07] 非当前 worker 的信号直接丢弃（旧 worker 在
        # _cleanup_worker 里 disconnect 失败/竞态时，其残留 chunk 不得写入本窗）。
        if self.sender() is not None and self.sender() is not self._worker:
            return
        if not text:
            return
        if not self._stream_started:
            self._stream_started = True
            self.log_edit.append(f"{self.npc.name}：")
        self.log_edit.moveCursor(QTextCursor.End)
        self.log_edit.insertPlainText(text)
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_error(self, text: str):
        if self.sender() is not None and self.sender() is not self._worker:
            return
        self.log_edit.append(f"（{text}）")

    def _on_finished(self, ok: bool, payload):
        if self.sender() is not None and self.sender() is not self._worker:
            return
        self._set_busy(False)
        if not ok:
            self.log_edit.append(f"（{payload}）")
            return
        data = payload or {}
        text = str(data.get("text", "") or "").strip()
        if not text:
            return
        # [记忆化私聊] assistant 消息入会话数组（不落历史文件——关窗统一总结入记忆）
        self._session.append({"role": "assistant", "content": text})
        nre.touch_friend_interact(self.world, self.npc)   # [P24c] 私聊算深互动，刷新衰减水位线
        try:
            self.storage.save_world(self.world)
        except Exception:
            pass
        if not self._stream_started:
            # 非流式兜底（一次性到达时 chunk 已喂过则不重复）
            self.log_edit.append(f"{self.npc.name}：{text}")

    def _on_worker_done(self):
        w = self._worker
        self._worker = None
        if w is not None:
            w.deleteLater()

    def _on_stop(self):
        if self._worker is not None:
            self._worker.cancel()

    # ---------- 生命周期（守 §15）----------
    def _cleanup_worker(self):
        w = self._worker
        if w is None:
            return
        try:
            w.chunk.disconnect()
            w.error.disconnect()
            w.finished_signal.disconnect()
            w.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        w.wait(5000)
        self._worker = None
        # [!] wait 超时后线程可能仍在收尾，直接 deleteLater 会析构运行中 QThread
        # （Destroyed while running 崩溃）——挂到 finished 让线程自然结束后再销毁。
        if w.isFinished():
            w.deleteLater()
        else:
            try:
                w.finished.connect(w.deleteLater)
            except (RuntimeError, TypeError):
                w.deleteLater()

    def _summarize_session(self):
        """[记忆化私聊 2026-08-29] 关窗总结：会话有实质对话 -> 后台 LLM 合并入
        NPC 记忆（best-effort，失败不阻断关窗——下次开窗空窗体，但 NPC 可能没记住）。"""
        msgs = [m for m in (self._session or [])
                if isinstance(m, dict) and str(m.get("content", "") or "").strip()]
        if len(msgs) < 2 or self.svc is None:
            return
        worker = _ChatSummaryWorker(self.svc, self.world, self.npc,
                                    msgs, self.preset, parent=None)
        # [!] 持引用防 GC（dialog 销毁后 worker 仍在跑会 0xC0000409 崩溃）
        self._summary_worker = worker
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def reject(self):
        self._cleanup_worker()
        self._summarize_session()
        super().reject()

    def accept(self):
        # [!] 「关闭」按钮走 accept，不触发 closeEvent/reject——若不在此清理，
        # 流式生成中关闭会在 QThread 收尾时被析构（Destroyed while running 崩溃）
        self._cleanup_worker()
        self._summarize_session()
        super().accept()

    def closeEvent(self, event):
        self._cleanup_worker()
        self._summarize_session()
        super().closeEvent(event)
