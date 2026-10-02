"""聊天工作线程：在 QThread 中运行编排器，通过信号更新 UI。"""
from __future__ import annotations
from PySide6.QtCore import QThread, Signal

from src.services.chat_orchestrator import ChatCallbacks


class ChatWorker(QThread):
    """后台执行聊天操作的工作线程。"""

    chunk = Signal(str)                 # 流式文本片段
    message_saved = Signal(object)      # Message 对象
    usage = Signal(str, object)         # (api_id, LlmUsage)
    error = Signal(str)                 # 错误信息
    speaker = Signal(object)            # 被选中的 Character
    image = Signal(str, str)            # (图片路径, 提示词)
    summary = Signal(object)            # 总结 Message
    status = Signal(str)                # 状态文本
    done = Signal()                     # 本轮完成
    # 手改模式：worker 线程发起，主线程弹窗阻塞返回结果。
    # 参数 (candidates, description)；主线程槽需用 BlockingQueuedConnection 连接，
    # 弹窗结果写入 sender() worker 的 _manual_select_result 后 emit 才返回。
    manual_select_request = Signal(object, str)

    def __init__(self, orchestrator, action: str, session=None, messages=None,
                 content: str = "", character=None, target_msg=None, parent=None):
        super().__init__(parent)
        self.orchestrator = orchestrator
        self.action = action
        self.session = session
        self.messages = messages
        self.content = content
        self.character = character
        self.target_msg = target_msg  # 编辑/重试/删除的目标消息
        self._cancelled = False
        self._manual_select_result: object = None  # 主线程弹窗回填结果

    def cancel(self):
        self._cancelled = True

    def run(self):
        cb = ChatCallbacks(
            on_chunk=self._on_chunk,
            on_usage=self._on_usage,
            on_message=self._on_message,
            on_error=self._on_error,
            on_speaker=self._on_speaker,
            on_image=self._on_image,
            on_summary=self._on_summary,
            on_status=self._on_status,
            on_done=None,  # done 在方法返回后手动发
            on_danbooru_manual_select=self._on_manual_select,
        )
        try:
            cancel_check = lambda: self._cancelled
            if self.action == "send_and_respond":
                self.orchestrator.send_and_respond(
                    self.session, self.messages, self.content, cb, cancel_check=cancel_check
                )
            elif self.action == "send_and_auto_respond":
                self.orchestrator.send_and_auto_respond(
                    self.session, self.messages, self.content, cb, cancel_check=cancel_check
                )
            elif self.action == "continue_group_chat":
                self.orchestrator.continue_group_chat(
                    self.session, self.messages, cb, cancel_check=cancel_check
                )
            elif self.action == "trigger_character":
                self.orchestrator.trigger_character(
                    self.session, self.messages, self.character, cb, cancel_check=cancel_check
                )
            elif self.action == "regenerate":
                self.orchestrator.regenerate_from(
                    self.session, self.messages, self.target_msg, cb, cancel_check=cancel_check
                )
            elif self.action == "continue_response":
                self.orchestrator.continue_response(
                    self.session, self.messages, self.target_msg, cb, cancel_check=cancel_check
                )
            # 注：编辑(edit_message)走 UI 同步路径(main_window._on_edit_message 直接调用
            # orchestrator.update_message_content)，不经 worker，故此处不设分支。
        except Exception as e:
            self.error.emit(f"内部错误: {e}")
        finally:
            self.done.emit()

    def _on_chunk(self, text):
        if not self._cancelled:
            self.chunk.emit(text)

    def _on_usage(self, api_id, usage):
        self.usage.emit(api_id, usage)

    def _on_message(self, msg):
        self.message_saved.emit(msg)

    def _on_error(self, err):
        self.error.emit(err)

    def _on_speaker(self, char):
        self.speaker.emit(char)

    def _on_image(self, path, prompt):
        self.image.emit(path, prompt)

    def _on_summary(self, msg):
        self.summary.emit(msg)

    def _on_status(self, text):
        self.status.emit(text)

    def _on_manual_select(self, candidates, description):
        """手改模式回调：emit 信号到主线程弹窗，阻塞等待结果。

        依赖 MainWindow 用 Qt.BlockingQueuedConnection 连接 manual_select_request，
        emit 在该连接上会阻塞直到主线程槽返回，结果由槽回填到
        self._manual_select_result。返回 None 表示用户取消（跳过此图）。
        """
        if self._cancelled:
            return None
        self._manual_select_result = None
        # emit 在 BlockingQueuedConnection 上会阻塞至主线程槽返回
        self.manual_select_request.emit(candidates, description)
        return self._manual_select_result