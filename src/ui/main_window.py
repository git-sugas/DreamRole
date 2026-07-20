"""主窗口：整合侧栏、聊天区、角色面板。"""
from __future__ import annotations
import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QListWidget,
    QListWidgetItem, QLabel, QPushButton, QMenu, QMenuBar, QStatusBar,
    QMessageBox, QInputDialog, QFrame, QTextEdit, QDialog,
    QDialogButtonBox, QFileDialog, QApplication, QFormLayout,
)

from src.models import Session, Message, Character
from src.ui.chat_view import ChatView
from src.ui.chat_input import ChatInput
from src.ui.character_panel import CharacterPanel
from src.ui.chat_worker import ChatWorker


class MainWindow(QMainWindow):
    def __init__(self, services):
        super().__init__()
        self.services = services
        self.storage = services["storage"]
        self.orchestrator = services["orchestrator"]
        self.comfyui = services["comfyui"]
        self.danbooru = services.get("danbooru")

        self.current_session: Session | None = None
        self.current_messages: list[Message] = []
        self.current_characters: list[Character] = []
        self.worker: ChatWorker | None = None
        self._streaming_started = False
        self._pending_speaker = None
        self._stop_pending = False
        self._continue_target: Message | None = None  # 续写模式下的目标消息（原地更新）

        self.setWindowTitle("DreamRole")
        # 屏幕几何适配：默认以 720p 启动并在可用区内居中，避免固定 1080 在分辨率
        # 不足或底部带任务栏的屏幕上被裁；用户点最大化即全屏撑满（1080 屏上拉到 1080）。
        # 可用区小于 720p 时收缩到可用区，并把最小尺寸也降到可用区防强制超出。
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        target_w, target_h = 1600, 900
        min_w, min_h = 1280, 720
        if avail is not None:
            avail_w, avail_h = avail.width(), avail.height()
            target_w = min(target_w, avail_w)
            target_h = min(target_h, avail_h)
            min_w = min(min_w, avail_w)
            min_h = min(min_h, avail_h)
            self.move((avail.width() - target_w) // 2 + avail.left(),
                      (avail.height() - target_h) // 2 + avail.top())
        self.resize(target_w, target_h)
        self.setMinimumSize(min_w, min_h)

        self._build_menu()
        self._build_ui()
        self._build_status_bar()
        self._load_sessions()

    # ============ 菜单 ============
    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件")
        file_menu.addAction("新建会话", self._on_new_chat)
        file_menu.addAction("删除会话", self._on_delete_session)
        file_menu.addSeparator()
        file_menu.addAction("导出会话存档...", self._on_export_session)
        file_menu.addAction("导入会话存档...", self._on_import_session)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        char_menu = menubar.addMenu("角色")
        char_menu.addAction("角色卡管理", self._on_character_editor)
        char_menu.addAction("用户管理", self._on_user_editor)
        char_menu.addAction("世界书管理", self._on_world_book)
        char_menu.addAction("角色记忆", self._on_memory)

        # 文生图菜单（放在设置前）：Danbooru Tag 设置 + ComfyUI 设置
        img_menu = menubar.addMenu("文生图")
        img_menu.addAction("Danbooru Tag 设置", self._on_danbooru_settings)
        img_menu.addAction("ComfyUI 设置", self._on_comfyui)

        settings_menu = menubar.addMenu("设置")
        settings_menu.addAction("API 与预设", self._on_api_settings)
        settings_menu.addAction("气泡配色规则", self._on_render_rules)
        settings_menu.addAction("统计信息", self._on_stats)
        settings_menu.addAction("破限设置...", self._on_app_config)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("关于", self._on_about)

    # ============ UI ============
    def _build_ui(self):
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)

        # 左侧栏：会话列表
        left = QWidget()
        left.setFixedWidth(260)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)

        title = QLabel("会话列表")
        title.setObjectName("titleLabel")
        left_layout.addWidget(title)

        new_btn = QPushButton("+ 新建会话")
        new_btn.setObjectName("primaryBtn")
        new_btn.clicked.connect(self._on_new_chat)
        left_layout.addWidget(new_btn)

        self.session_list = QListWidget()
        self.session_list.setFrameShape(QFrame.NoFrame)
        self.session_list.currentItemChanged.connect(self._on_session_selected)
        # 会话右键菜单：不用为了新建会话跑进 GroupSetupDialog，就能临时改当前会话的
        # 上文总结开关/阈值/每次N条，以及切换绑定用户。
        self.session_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._on_session_context_menu)
        left_layout.addWidget(self.session_list)
        splitter.addWidget(left)

        # 中间：聊天区
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(4, 8, 4, 8)
        center_layout.setSpacing(0)

        self.title_label = QLabel("选择或新建会话")
        self.title_label.setObjectName("titleLabel")
        self.title_label.setContentsMargins(8, 0, 8, 8)
        center_layout.addWidget(self.title_label)

        self.chat_view = ChatView()
        self.chat_view.message_context_menu.connect(self._on_message_context_menu)
        self.chat_view.avatar_clicked.connect(self._on_avatar_clicked)
        center_layout.addWidget(self.chat_view, 1)

        self.chat_input = ChatInput()
        self.chat_input.send_requested.connect(self._on_send)
        self.chat_input.continue_requested.connect(self._on_continue)
        self.chat_input.mode_changed.connect(self._on_mode_changed)
        self.chat_input.stop_requested.connect(self._on_stop)
        center_layout.addWidget(self.chat_input)
        splitter.addWidget(center)

        # 右侧：角色面板
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.character_panel = CharacterPanel()
        self.character_panel.character_clicked.connect(self._on_character_clicked)
        right_layout.addWidget(self.character_panel, 1)

        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        layout.addWidget(splitter)
        self.setCentralWidget(central)

    def _build_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("就绪")
        self.status_bar.addWidget(self.status_label)
        self.token_label = QLabel("")
        self.status_bar.addPermanentWidget(self.token_label)

    # ============ 会话管理 ============
    def _load_sessions(self):
        self.session_list.clear()
        sessions = self.storage.load_all_sessions()
        for s in sessions:
            item = QListWidgetItem(s.title or "未命名会话")
            item.setData(Qt.UserRole, s.id)
            self.session_list.addItem(item)
        if sessions:
            self.session_list.setCurrentRow(0)

    def _on_session_selected(self, current, previous):
        if not current:
            return
        session_id = current.data(Qt.UserRole)
        session = self.storage.load_session(session_id)
        if not session:
            return
        self.current_session = session
        self.current_messages = self.storage.load_messages(session.id)
        self._load_characters()
        self._display_session()
        self._update_token_label()

    def _load_characters(self):
        self.current_characters = []
        for cid in self.current_session.character_ids:
            char = self.storage.load_character(cid)
            if char:
                self.current_characters.append(char)

    def _display_session(self):
        s = self.current_session
        self.title_label.setText(s.title or "未命名会话")

        # 设置角色头像映射（character_id -> avatar 文件名）
        avatar_map = {char.id: char.avatar for char in self.current_characters if char.avatar}
        self.chat_view.set_character_avatars(avatar_map)

        # 用户头像映射：如果会话绑定了 User 实体且该 User 有头像，则把头像传给
        # chat_view 供用户消息气泡显示（message_bubble 原对 user 消息强制空头像，已放开）。
        user_avatar = ""
        if getattr(s, "user_id", ""):
            u = self.storage.load_user(s.user_id)
            if u and u.avatar:
                user_avatar = u.avatar
        self.chat_view.set_user_avatar(user_avatar)

        self.chat_view.load_messages(self.current_messages)

        # 角色面板
        api_names = {}
        for char in self.current_characters:
            api = self.storage.load_api(char.api_id)
            api_names[char.id] = api.name if api else "未绑定"
        self.character_panel.set_characters(self.current_characters, api_names)

        # 输入栏
        is_group = s.session_type == "group"
        self.chat_input.set_group_mode(is_group)
        self.chat_input.set_current_mode(s.group_mode)
        self.chat_input.set_generating(False)

    def _on_new_chat(self):
        from src.ui.dialogs.group_setup import GroupSetupDialog
        chars = self.storage.load_all_characters()
        apis = self.storage.load_all_apis()
        world_books = self.storage.load_all_world_books()
        users = self.storage.load_all_users()
        if not chars:
            QMessageBox.warning(self, "提示", "请先创建角色卡")
            return
        dlg = GroupSetupDialog(chars, apis, world_books, users, self)
        if dlg.exec():
            data = dlg.get_result()
            session = Session(
                title=data["title"],
                session_type=data["session_type"],
                character_ids=data["character_ids"],
                world_book_id=data.get("world_book_id", ""),
                user_id=data.get("user_id", ""),
                player_name=data.get("player_name", "用户"),
                group_mode=data.get("group_mode", "manual"),
                director_api_id=data.get("director_api_id", ""),
                # [!] 记住开场白角色作为手动模式默认发言者（空则后续回退首个角色）
                default_speaker_id=data.get("greeting_character_id", ""),
                auto_summary_enabled=data.get("auto_summary_enabled", True),
                auto_summary_threshold=data.get("auto_summary_threshold", 30),
                auto_summary_count=data.get("auto_summary_count", 15),
            )
            self.storage.save_session(session)
            self.current_session = session
            self.current_messages = []
            # 注入开场白（单聊或群聊均可选；群聊用 greeting_character_id 定位发言角色）
            greeting = data.get("greeting", "")
            greeting_cid = data.get("greeting_character_id", "")
            if greeting and session.character_ids:
                # 优先用对话框指定的角色；缺省回退首个角色
                cid = greeting_cid or session.character_ids[0]
                char = self.storage.load_character(cid) if cid else None
                if not char and session.character_ids:
                    char = self.storage.load_character(session.character_ids[0])
                greeting_msg = Message(
                    session_id=session.id,
                    role="assistant",
                    character_id=char.id if char else "",
                    character_name=char.name if char else "",
                    content=greeting,
                )
                self.storage.save_message(greeting_msg)
                self.current_messages.append(greeting_msg)
            self._load_characters()
            self._display_session()
            self._load_sessions()
            # 选中新会话
            for i in range(self.session_list.count()):
                if self.session_list.item(i).data(Qt.UserRole) == session.id:
                    self.session_list.setCurrentRow(i)
                    break

    def _on_delete_session(self):
        if not self.current_session:
            return
        reply = QMessageBox.question(
            self, "确认", f"删除会话「{self.current_session.title}」？"
        )
        if reply == QMessageBox.Yes:
            self.storage.delete_session(self.current_session.id)
            self.current_session = None
            self.current_messages = []
            self.chat_view.clear_messages()
            self.title_label.setText("选择或新建会话")
            self._load_sessions()

    def _on_export_session(self):
        """导出当前会话为 JSON 存档（含消息、角色卡、世界书）。"""
        if not self.current_session:
            QMessageBox.information(self, "导出会话", "请先选择一个会话")
            return
        default_name = (self.current_session.title or "session") + ".json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出会话存档", default_name, "JSON 存档 (*.json)"
        )
        if not path:
            return
        import json as _json
        archive = self.storage.export_session_archive(self.current_session.id)
        if archive is None:
            QMessageBox.warning(self, "导出失败", "会话数据读取失败")
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(archive, f, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "导出失败", f"写入文件失败: {e}")
            return
        QMessageBox.information(
            self, "导出成功",
            f"已导出会话「{self.current_session.title}」，含 {len(archive['messages'])} 条消息。"
        )

    def _on_import_session(self):
        """从 JSON 存档导入一个新会话。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入会话存档", "", "JSON 存档 (*.json)"
        )
        if not path:
            return
        import json as _json
        try:
            with open(path, "r", encoding="utf-8") as f:
                archive = _json.load(f)
        except (OSError, _json.JSONDecodeError) as e:
            QMessageBox.warning(self, "导入失败", f"读取文件失败: {e}")
            return
        new_session = self.storage.import_session_archive(archive)
        if new_session is None:
            QMessageBox.warning(self, "导入失败", "不是有效的会话存档文件")
            return
        # 切换到导入的新会话
        self.current_session = new_session
        self.current_messages = self.storage.load_messages(new_session.id)
        self._load_characters()
        self._display_session()
        self._load_sessions()
        for i in range(self.session_list.count()):
            if self.session_list.item(i).data(Qt.UserRole) == new_session.id:
                self.session_list.setCurrentRow(i)
                break
        QMessageBox.information(
            self, "导入成功",
            f"已导入会话「{new_session.title}」。角色卡 / 世界书已按需自动补齐（已存在的同名资源不会被覆盖）。"
        )

    # ============ 聊天流程 ============
    def _on_send(self, text: str):
        if not self.current_session or self.worker:
            return
        s = self.current_session
        if s.session_type == "group" and s.group_mode == "auto":
            self._start_worker("send_and_auto_respond", content=text)
        else:
            self._start_worker("send_and_respond", content=text)

    def _on_continue(self):
        if not self.current_session or self.worker:
            return
        if self.current_session.session_type != "group":
            return
        self._start_worker("continue_group_chat")

    def _on_character_clicked(self, character_id: str):
        if not self.current_session or self.worker:
            return
        # 单聊模式不触发手动发言（手动发言是群聊专属功能）
        if self.current_session.session_type == "single":
            self.status_label.setText("单聊模式下请直接输入消息发送")
            return
        char = self.storage.load_character(character_id)
        if not char:
            return
        self.character_panel.highlight_character(character_id)
        self._start_worker("trigger_character", character=char)

    def _on_stop(self):
        """停止当前生成：标记取消并通知 worker，UI 待 done 后清理。"""
        if not self.worker:
            return
        self._stop_pending = True
        self.worker.cancel()
        self.chat_input.stop_btn.setEnabled(False)
        self.status_label.setText("正在停止...")

    def _on_mode_changed(self, mode: str):
        if self.current_session:
            # 单聊不允许切换发言模式（单聊无导演/手动发言概念）
            if self.current_session.session_type == "single":
                return
            self.current_session.group_mode = mode
            self.storage.save_session(self.current_session)

    def _start_worker(self, action: str, content: str = "", character=None, target_msg=None):
        if not self.current_session:
            return
        self.chat_input.set_generating(True)
        self._streaming_started = False
        self._pending_speaker = None
        self._stop_pending = False
        # 续写模式：target_msg 即续写目标，原地更新气泡
        self._continue_target = target_msg if action == "continue_response" else None
        self.worker = ChatWorker(
            self.orchestrator, action,
            session=self.current_session,
            messages=self.current_messages,
            content=content,
            character=character,
            target_msg=target_msg,
        )
        self.worker.chunk.connect(self._on_chunk)
        self.worker.message_saved.connect(self._on_message_saved)
        self.worker.usage.connect(self._on_usage)
        self.worker.error.connect(self._on_error)
        self.worker.speaker.connect(self._on_speaker)
        self.worker.image.connect(self._on_image)
        self.worker.summary.connect(self._on_summary)
        self.worker.status.connect(self._on_status)
        self.worker.done.connect(self._on_done)
        # 手改模式：BlockingQueuedConnection 让 worker 线程 emit 时阻塞，
        # 直到主线程槽弹完窗、把结果回填到 worker._manual_select_result 才继续。
        self.worker.manual_select_request.connect(
            self._on_manual_select_request, Qt.BlockingQueuedConnection
        )
        # [!] worker 线程结束后自动 deleteLater，避免 QThread 对象泄漏
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def closeEvent(self, event):
        # [!] 关闭窗口时若有 worker 在跑，先 cancel 再 wait，避免 QThread 仍在后台
        # 执行 HTTP 请求导致「QThread: Destroyed while thread is still running」。
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(3000)  # 最多等 3 秒
        # [!] 图片重生成 worker 也要等待：comfyui.generate 是阻塞调用无取消机制，
        # 关窗时若仍在跑，worker emit finished_signal 会触发已销毁接收者致 0xC0000409 崩溃
        # （与 §17 CharacterEditorDialog.closeEvent 等头像 worker 同型，对称处理）。
        img_worker = getattr(self, "_img_regen_worker", None)
        if img_worker is not None:
            try:
                if img_worker.isRunning():
                    try:
                        img_worker.finished_signal.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                    img_worker.wait(120000)  # 最多等 120 秒（与头像生成一致）
            except RuntimeError:
                # C++ 对象已删除（deleteLater 异步），忽略
                pass
            self._img_regen_worker = None
        event.accept()

    # ---- Worker 信号处理 ----
    def _on_chunk(self, text: str):
        # 续写模式：不建占位气泡，直接在已有气泡上追加显示
        if self._continue_target is not None:
            tgt = self._continue_target
            if not hasattr(self, "_continue_acc"):
                self._continue_acc = ""
            self._continue_acc += text
            # 临时把 target 的内容设为 已有 + 累积，刷新气泡显示
            preview = Message(
                id=tgt.id, session_id=tgt.session_id, role="assistant",
                character_id=tgt.character_id, character_name=tgt.character_name,
                content=tgt.content + self._continue_acc,
            )
            self.chat_view.refresh_bubble(preview)
            self.chat_view.scroll_to_bottom()
            return
        if not self._streaming_started:
            self._streaming_started = True
            # 创建占位流式气泡（优先用已选定的发言角色）
            char = getattr(self, "_pending_speaker", None)
            if not char and self.current_characters:
                char = self.current_characters[0]
            name = char.name if char else ""
            cid = char.id if char else ""
            placeholder = Message(role="assistant", character_id=cid, character_name=name, content="")
            self.chat_view.start_streaming(placeholder)
        self.chat_view.append_streaming(text)

    def _on_message_saved(self, msg):
        # 统一去重 append：编排器已会 append 同一引用，此处再 append 会重复，故去重
        def _append_once(m):
            if not any(x.id == m.id for x in self.current_messages):
                self.current_messages.append(m)

        if msg.role == "user":
            # 延迟存储：user 消息在 LLM 回复完成后才到达，此时流式占位气泡
            # 已显示在底部。[!] 不可在此清理流式状态（finish_streaming/翻标志）--
            # 占位气泡必须保留到 assistant 消息到达时由 finalize_streaming_to
            # 升级为正式气泡，否则 assistant 分支会走 add_message 重复新增，
            # 造成「占位LLM + 用户 + 重复LLM」三条气泡。
            # user 气泡插到占位之前（历史顺序 user 先、LLM 后），流式状态原样保留。
            if self._streaming_started:
                self.chat_view.add_message_before_streaming(msg)
            else:
                self.chat_view.add_message(msg)
            _append_once(msg)
        elif msg.role == "assistant" and self._continue_target is not None:
            # 续写完成：原地更新目标消息气泡（同 id），同步角标
            self._continue_target.content = msg.content
            self._continue_target.tokens = msg.tokens
            self._continue_target.is_stopped = msg.is_stopped
            self.chat_view.refresh_bubble(self._continue_target)
            # current_messages 中该消息引用由 orchestrator 直接改了字段，无需替换
        elif msg.role == "assistant":
            # 流式占位气泡「升级」为正式气泡：把登记键从占位临时 id 改为正式 id，
            # 使后续 编辑/重试/删除 能按正式 id 定位到该气泡。
            if self._streaming_started and self.chat_view.finalize_streaming_to(msg):
                self._streaming_started = False
            else:
                # 非流式或占位已清理：正常新增气泡
                if self._streaming_started:
                    self.chat_view.finish_streaming()
                    self._streaming_started = False
                self.chat_view.add_message(msg)
            _append_once(msg)
        elif msg.role == "summary":
            self.chat_view.add_summary_block(msg)
            _append_once(msg)

    def _on_usage(self, api_id, usage):
        self._update_token_label()

    def _on_error(self, err: str):
        self.status_label.setText(f"错误: {err}")
        if self._streaming_started:
            self.chat_view.finish_streaming()
            self._streaming_started = False
        if self._continue_target is not None:
            # 续写出错：恢复气泡为续写前内容
            self.chat_view.refresh_bubble(self._continue_target)
            self._continue_target = None
            if hasattr(self, "_continue_acc"):
                del self._continue_acc
        # [!] 严重错误弹窗提示（与导出/导入失败一致），避免状态栏文字一闪而过被 _on_done 覆盖。
        # 非空 err 才弹（部分路径 emit 空串作为清除信号）。
        if err:
            QMessageBox.warning(self, "错误", err)
    def _on_speaker(self, char):
        self._pending_speaker = char
        self.character_panel.highlight_character(char.id)
        self.status_label.setText(f"选中发言: {char.name}")

    def _on_image(self, path: str, prompt: str):
        # 纯图片消息持久化：AI 回复含 [img:...] 时，编排器 emit on_image，此处
        # 落库 + 加气泡。is_image_only=True 让 context_builder 跳过上下文（不入 API），
        # 但消息记录入库保证重开应用可见历史图片。
        s = self.current_session
        if not s:
            return
        # 沿用上一条 assistant 的角色（图片是 AI 回复的产物）
        char_id, char_name = "", ""
        for m in reversed(self.current_messages):
            if m.role == "assistant" and m.character_id:
                char_id, char_name = m.character_id, m.character_name
                break
        msg = Message(
            role="assistant",
            session_id=s.id,
            character_id=char_id,
            character_name=char_name,
            content=prompt,
            image_path=path,
            is_image_only=True,
        )
        self.storage.save_message(msg)
        self.chat_view.add_message(msg)
        if not any(x.id == msg.id for x in self.current_messages):
            self.current_messages.append(msg)

    def _on_summary(self, msg):
        # [!] 总结发生在 generate_response 入口（orchestrator:165），此时流式占位
        # 气泡尚未创建（_streaming_started=False），可安全重渲染整个视图。
        # summarize_and_collapse 已把被总结的旧消息标记 collapsed=True 写库，
        # 并插入 summary 消息。load_messages 内置的折叠分组逻辑会把 collapsed
        # 的消息归并为 CollapsedBlock，summary 消息作为独立气泡插入--实现
        # 「总结后旧消息实时折叠」的视觉效果，无需等下次切会话/重开。
        if not self.current_session:
            return
        messages = self.storage.load_messages(self.current_session.id)
        self.current_messages = messages
        self.chat_view.load_messages(messages)

    def _on_status(self, text: str):
        self.status_label.setText(text)

    def _on_manual_select_request(self, candidates, description):
        """手改模式：主线程弹窗供用户勾选 tag，结果回填到发起的 worker。

        由 BlockingQueuedConnection 调用，本槽返回前 worker 线程一直阻塞。
        用户确认 → 回填勾选的 name 列表；用户取消 → 回填 None（编排器跳过此图）。
        """
        from src.ui.dialogs.danbooru_select_dialog import DanbooruSelectDialog
        dlg = DanbooruSelectDialog(candidates, description, self)
        if dlg.exec() == QDialog.Accepted:
            result = dlg.selected()  # list[str]（可能为空列表=用户确认但没选任何项）
        else:
            result = None  # 取消
        # 回填到发起 worker
        worker = self.sender()
        if isinstance(worker, ChatWorker):
            worker._manual_select_result = result

    def _on_done(self):
        # 续写模式收尾
        if self._continue_target is not None:
            # 已在 _on_message_saved 刷新气泡；此处仅清理续写状态
            self.chat_view.refresh_bubble(self._continue_target)
            self._continue_target = None
            if hasattr(self, "_continue_acc"):
                del self._continue_acc
            self._streaming_started = False
            self._stop_pending = False
            self.worker = None
            self.chat_input.set_generating(False)
            self.status_label.setText("就绪")
            self._update_token_label()
            if self.current_session:
                self.current_messages = self.storage.load_messages(self.current_session.id)
            return
        # 用户停止且占位气泡未升级为正式消息时，清理空占位气泡
        if self._stop_pending and self._streaming_started:
            self.chat_view.cancel_streaming()
        else:
            self.chat_view.finish_streaming()
        self._streaming_started = False
        self._stop_pending = False
        self.worker = None
        self.chat_input.set_generating(False)
        self.status_label.setText("就绪")
        self._update_token_label()
        # 重新加载消息以同步折叠状态
        if self.current_session:
            self.current_messages = self.storage.load_messages(self.current_session.id)

    def _update_token_label(self):
        if not self.current_session:
            self.token_label.setText("")
            return
        total_p = total_c = total_cached = 0
        api_ids: set[str] = set()
        for char in self.current_characters:
            stats = self.services["stats"].get_stats(char.api_id)
            total_p += stats.total_prompt_tokens
            total_c += stats.total_completion_tokens
            total_cached += stats.total_cached_tokens
            api_ids.add(char.api_id)
        rate = f"{total_cached / total_p * 100:.0f}%" if total_p > 0 else "0%"
        # 费用：按当前会话涉及 API 的费率合计
        cost = self.services["stats"].get_total_cost(list(api_ids)) if api_ids else 0.0
        cost_text = f" | 费用: ¥{cost:.4f}" if cost > 0 else ""
        self.token_label.setText(
            f"Prompt: {total_p:,} | Completion: {total_c:,} | 缓存命中: {rate}{cost_text}"
        )

    # ============ 消息右键菜单：编辑 / 重试 / 删除 ============
    def _on_message_context_menu(self, msg: Message, pos):
        """气泡右键菜单：编辑、重试（仅AI）、删除。"""
        if self.worker is not None:
            return  # 生成中不响应
        # 纯图片消息/总结消息不提供编辑重试
        if msg.is_summary:
            return
        # 纯图片消息：专属菜单（查看提示词 / 重新生成 / 删除）
        if msg.is_image_only:
            menu = QMenu(self)
            menu.addAction("🔍 查看提示词").triggered.connect(
                lambda: self._on_view_image_prompt(msg)
            )
            menu.addAction("🔄 重新生成").triggered.connect(
                lambda: self._on_regenerate_image(msg)
            )
            menu.addSeparator()
            menu.addAction("🗑️ 删除").triggered.connect(
                lambda: self._on_delete_message(msg)
            )
            menu.exec(pos)
            return
        menu = QMenu(self)
        act_edit = menu.addAction("✏️ 编辑")
        if msg.role == "assistant":
            if msg.is_stopped:
                menu.addAction("✍️ 续写").triggered.connect(
                    lambda: self._on_continue_message(msg)
                )
            menu.addAction("🔄 重试").triggered.connect(
                lambda: self._on_retry_message(msg)
            )
        menu.addSeparator()
        act_delete = menu.addAction("🗑️ 删除")
        act_edit.triggered.connect(lambda: self._on_edit_message(msg))
        act_delete.triggered.connect(lambda: self._on_delete_message(msg))
        menu.exec(pos)

    def _on_edit_message(self, msg: Message):
        """编辑消息文本内容。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑消息")
        dl = QVBoxLayout(dlg)
        dl.addWidget(QLabel("编辑消息内容（保存后会更新并发送给后续对话）："))
        te = QTextEdit()
        te.setPlainText(msg.content)
        te.setMinimumHeight(180)
        dl.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        new_text = te.toPlainText()
        if new_text == msg.content:
            return
        # 同步：持久化(内部会更新 msg.content 与 tokens) + 刷新气泡
        # msg 即内存列表中的元素引用，无需额外替换。
        self.orchestrator.update_message_content(msg, new_text)
        self.chat_view.refresh_bubble(msg)
        self.status_label.setText("消息已更新")

    def _on_continue_message(self, msg: Message):
        """续写被中断（已停止）的 AI 回复：从断点续写而非整条重试。"""
        if not self.current_session or self.worker:
            return
        if not msg.is_stopped:
            return
        self._start_worker("continue_response", target_msg=msg)

    def _on_retry_message(self, msg: Message):
        """重试 AI 回复：删除该消息及其后所有消息，重新生成。"""
        if not self.current_session or self.worker:
            return
        # 找到该消息位置，判断其后是否还有消息需要确认删除
        idx = next((i for i, m in enumerate(self.current_messages) if m.id == msg.id), None)
        has_after = idx is not None and idx < len(self.current_messages) - 1
        tip = "重试将删除这条回复并重新生成。"
        if has_after:
            tip += "\n注意：这条回复之后的全部消息也会被删除。"
        reply = QMessageBox.question(self, "确认重试", tip)
        if reply != QMessageBox.Yes:
            return
        # 立即从视图移除该气泡及之后所有气泡（避免重试期间显示旧内容）
        if idx is not None:
            to_remove_ids = [m.id for m in self.current_messages[idx:]]
            for mid in to_remove_ids:
                self.chat_view.delete_bubble(mid)
            # [!] 与 orchestrator.regenerate_from 对称：若前一条是本轮 user 消息
            # （延迟存储下 user 先于 assistant 存），orchestrator 会删掉它并重新
            # 走 pending_trigger 生成新 user。UI 必须同步删掉旧 user 气泡，否则
            # 新 user 气泡经 on_message 加进来后，UI 上会出现「两条用户消息」
            # （旧 user 气泡残留 + 新 user 气泡）。条件须与 orchestrator 完全一致：
            # idx > 0 且前一条 role == "user"。
            if idx > 0 and self.current_messages[idx - 1].role == "user":
                self.chat_view.delete_bubble(self.current_messages[idx - 1].id)
        self._start_worker("regenerate", target_msg=msg)

    def _on_delete_message(self, msg: Message):
        """删除单条消息。"""
        if not self.current_session or self.worker:
            return
        idx = next((i for i, m in enumerate(self.current_messages) if m.id == msg.id), None)
        has_after = idx is not None and idx < len(self.current_messages) - 1
        tip = f"删除这条消息？\n「{(msg.character_name or '用户')}：{msg.content[:40]}」"
        if has_after:
            tip += "\n（仅删除这一条；其后的消息保留）"
        reply = QMessageBox.question(self, "确认删除", tip)
        if reply != QMessageBox.Yes:
            return
        self.orchestrator.delete_message_and_after(
            self.current_session, self.current_messages, msg, delete_after=False
        )
        self.chat_view.delete_bubble(msg.id)
        self.status_label.setText("消息已删除")

    # ============ 图片消息：查看提示词 / 重新生成 ============
    def _resolve_image_negative(self) -> str:
        """取图片生成的负面提示词（从 DanbooruPreset.negative_prompt）。

        图片消息本身只存了 positive（在 content 字段），negative 未持久化，
        这里从预设取当前值（足够展示；老图片生成时的 negative 可能与当前预设不同，
        但仅用于查看，不影响重新生成 -- 重新生成仍用原 positive 调 ComfyUI）。
        """
        if self.danbooru is not None:
            try:
                preset = self.storage.load_danbooru_preset()
                return preset.negative_prompt or ""
            except Exception:
                return ""
        return ""

    def _on_view_image_prompt(self, msg: Message):
        """只读弹窗显示图片消息的完整提示词（positive / negative / image_path），可复制。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("图片提示词")
        dl = QVBoxLayout(dlg)
        positive = msg.content or ""
        negative = self._resolve_image_negative()
        form = QFormLayout()
        positive_edit = QTextEdit(positive)
        positive_edit.setReadOnly(True)
        positive_edit.setMinimumHeight(100)
        negative_edit = QTextEdit(negative)
        negative_edit.setReadOnly(True)
        negative_edit.setMinimumHeight(80)
        path_label = QLabel(msg.image_path or "（无）")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("正向（positive）:", positive_edit)
        form.addRow("负向（negative）:", negative_edit)
        form.addRow("图片路径:", path_label)
        dl.addLayout(form)
        # 提示：negative 是当前预设值，可能与生成时不同
        hint = QLabel("注：负向提示词取自当前 Danbooru 预设，可能与该图片生成时的值不同。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        dl.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        dl.addWidget(bb)
        dlg.exec()

    def _on_avatar_clicked(self, msg: Message):
        """点击气泡头像：弹出对话框居中显示头像原图（圆形裁剪前）。
        AI 消息走 character.avatar，用户消息走当前会话绑定的 user.avatar。
        无 avatar（首字占位）时提示无图可预览。"""
        # 解析头像文件名：AI 消息用 character_id 查角色，用户消息用会话 user_id 查用户
        avatar_name = ""
        if msg.character_id:
            char = self.storage.load_character(msg.character_id)
            if char:
                avatar_name = char.avatar or ""
        else:
            s = self.current_session
            if s and getattr(s, "user_id", ""):
                u = self.storage.load_user(s.user_id)
                if u:
                    avatar_name = u.avatar or ""
        if not avatar_name:
            name = msg.character_name or "用户"
            QMessageBox.information(self, "无头像", f"{name} 未设置头像图片（当前为首字占位）。")
            return
        # 拼完整路径（avatar 字段存文件名，实际文件在 avatars 目录）
        from src.config import paths
        avatar_path = os.path.join(paths.avatars_dir(), avatar_name)
        if not os.path.exists(avatar_path):
            QMessageBox.warning(self, "文件缺失", f"头像文件不存在:\n{avatar_path}")
            return
        # GIF 动图用 QMovie 播放，静态图用 QPixmap
        is_gif = avatar_name.lower().endswith(".gif")
        dlg = QDialog(self)
        dlg.setWindowTitle(f"头像预览 - {msg.character_name or '用户'}")
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(8, 8, 8, 8)
        if is_gif:
            from PySide6.QtGui import QMovie
            lbl = QLabel()
            movie = QMovie(avatar_path)
            # 限制最大尺寸适配屏幕（保持原比例），超大 GIF 缩放显示
            screen = QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else None
            max_w = avail.width() - 80 if avail else 800
            max_h = avail.height() - 120 if avail else 600
            if movie.isValid():
                # 用 scaledSize 限制 QMovie 输出尺寸（保持比例）
                sz = movie.currentPixmap().size()
                if not sz.isEmpty():
                    scaled = sz.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    movie.setScaledSize(scaled)
                movie.start()
                lbl.setMovie(movie)
                # 保活 movie（dlg 关闭时随 dlg 销毁）
                dlg._avatar_movie = movie
            else:
                lbl.setText("GIF 文件损坏，无法预览")
        else:
            from PySide6.QtGui import QPixmap
            pix = QPixmap(avatar_path)
            if pix.isNull():
                QMessageBox.warning(self, "读取失败", f"无法读取头像图片:\n{avatar_path}")
                return
            # 大图缩放适配屏幕（保持比例），小图原尺寸显示
            screen = QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else None
            max_w = avail.width() - 80 if avail else 800
            max_h = avail.height() - 120 if avail else 600
            if pix.width() > max_w or pix.height() > max_h:
                pix = pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            lbl = QLabel()
            lbl.setPixmap(pix)
        lbl.setAlignment(Qt.AlignCenter)
        vl.addWidget(lbl)
        # 文件名提示（可选中复制）
        from PySide6.QtWidgets import QDialogButtonBox
        path_label = QLabel(avatar_name)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setStyleSheet("color: #565f89; font-size: 11px;")
        vl.addWidget(path_label)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        vl.addWidget(bb)
        dlg.exec()

    def _on_regenerate_image(self, msg: Message):
        """用原存的 positive 重调 ComfyUI 重新生成（只换 seed），生成后更新 image_path。"""
        if not (self.comfyui and self.comfyui.is_enabled()):
            QMessageBox.warning(self, "无法生成", "请先在 ComfyUI 设置中启用并配置工作流")
            return
        positive = msg.content or ""
        if not positive:
            QMessageBox.warning(self, "无法生成", "该图片消息未保存提示词，无法重新生成")
            return
        # 防重复启动（图片重生成 worker 独立于聊天 worker，用专门字段跟踪）
        # [!] isRunning() 可能抛 RuntimeError（deleteLater 异步删 C++ 对象但 Python 引用还在），
        # try 兜底防御，异常时视为未运行并置 None 允许新建（与 character_editor._on_gen_avatar 对称）。
        try:
            if getattr(self, "_img_regen_worker", None) and self._img_regen_worker.isRunning():
                return
        except RuntimeError:
            self._img_regen_worker = None
        negative = self._resolve_image_negative()
        self._img_regen_worker = _ImageRegenWorker(
            self.comfyui, positive, negative, msg.id,
        )
        self._img_regen_worker.finished_signal.connect(
            lambda ok, info: self._on_image_regen_done(ok, info, msg)
        )
        self._img_regen_worker.finished.connect(self._img_regen_worker.deleteLater)
        self.status_label.setText("正在重新生成图片…")
        self._img_regen_worker.start()

    def _on_image_regen_done(self, ok: bool, info: str, msg: Message):
        """图片重生成 worker 完成回调：ok=True 时 info 是新图片路径。"""
        # [!] 置 None 防 isRunning() 抛 RuntimeError（deleteLater 异步删 C++ 对象但 Python 引用还在），
        # 与 character_editor._on_avatar_gen_done 对称。
        self._img_regen_worker = None
        if not ok:
            self.status_label.setText("")
            QMessageBox.warning(self, "重新生成失败", info)
            return
        # info 是 ComfyUI 下载到 images_dir 的新图片绝对路径
        new_path = info
        if not os.path.exists(new_path):
            QMessageBox.warning(self, "重新生成失败", "生成的图片文件未找到")
            return
        # 删旧图片文件（避免孤儿文件）
        old_path = msg.image_path
        if old_path and os.path.exists(old_path) and os.path.abspath(old_path) != os.path.abspath(new_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
        # 更新消息 image_path 并落库 + 刷新气泡
        msg.image_path = new_path
        self.storage.save_message(msg)
        self.chat_view.refresh_bubble(msg)
        self.status_label.setText("图片已重新生成")

    # ============ 菜单动作 ============
    def _on_character_editor(self):
        from src.ui.dialogs.character_editor import CharacterEditorDialog
        dlg = CharacterEditorDialog(self.storage, self.comfyui, self.danbooru, self)
        dlg.exec()
        if self.current_session:
            self._load_characters()
            self._display_session()

    def _on_user_editor(self):
        from src.ui.dialogs.user_editor import UserEditorDialog
        dlg = UserEditorDialog(self.storage, self.comfyui, self.danbooru, self)
        dlg.exec()
        # 用户变更可能影响当前会话头像/上下文，刷新
        if self.current_session:
            self._load_characters()
            self._display_session()

    def _on_session_context_menu(self, pos):
        """会话列表右键菜单：上文总结设置 / 切换用户。"""
        if not self.current_session:
            return
        menu = QMenu(self)
        menu.addAction("上文总结设置…", self._edit_session_summary)
        menu.addAction("切换用户…", self._switch_session_user)
        menu.exec(self.session_list.mapToGlobal(pos))

    def _edit_session_summary(self):
        """对当前会话临时修改上文总结三参数（会话级，不入预设）。"""
        from PySide6.QtWidgets import QSpinBox, QCheckBox
        s = self.current_session
        if not s:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"上文总结设置 — {s.title or s.id[:8]}")
        dl = QVBoxLayout(dlg)
        hint = QLabel("会话级配置：与角色记忆独立。可随时开关、调整阈值。改完保存即时生效。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        dl.addWidget(hint)
        form = QFormLayout()
        en_chk = QCheckBox("启用上文自动总结")
        en_chk.setChecked(s.auto_summary_enabled)
        form.addRow(en_chk)
        th_spin = QSpinBox(); th_spin.setRange(5, 500); th_spin.setValue(s.auto_summary_threshold)
        form.addRow("触发阈值(条):", th_spin)
        ct_spin = QSpinBox(); ct_spin.setRange(2, 100); ct_spin.setValue(s.auto_summary_count)
        form.addRow("每次总结N条:", ct_spin)
        dl.addLayout(form)
        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            s.auto_summary_enabled = en_chk.isChecked()
            s.auto_summary_threshold = th_spin.value()
            s.auto_summary_count = ct_spin.value()
            s.touch()
            self.storage.save_session(s)

    def _switch_session_user(self):
        """切换当前会话绑定的用户。"""
        from PySide6.QtWidgets import QComboBox
        s = self.current_session
        if not s:
            return
        users = self.storage.load_all_users()
        dlg = QDialog(self)
        dlg.setWindowTitle(f"切换用户 — {s.title or s.id[:8]}")
        dl = QVBoxLayout(dlg)
        form = QFormLayout()
        combo = QComboBox()
        combo.addItem("（不绑定用户）", "")
        cur_idx = 0
        for i, u in enumerate(users, start=1):
            combo.addItem(u.name or "未命名用户", u.id)
            if u.id == s.user_id:
                cur_idx = i
        combo.setCurrentIndex(cur_idx)
        form.addRow("切换为:", combo)
        dl.addLayout(form)
        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            new_uid = combo.currentData() or ""
            s.user_id = new_uid
            # 联动改 player_name：切到某用户时若选了则用其名（用户仍可在新建会话时覆盖）
            if new_uid:
                u = next((x for x in users if x.id == new_uid), None)
                if u and u.name:
                    s.player_name = u.name
            s.touch()
            self.storage.save_session(s)
            self._display_session()

    def _on_world_book(self):
        from src.ui.dialogs.world_book_editor import WorldBookEditorDialog
        dlg = WorldBookEditorDialog(self.storage, self)
        dlg.exec()

    def _on_api_settings(self):
        from src.ui.dialogs.api_dialog import ApiSettingsDialog
        dlg = ApiSettingsDialog(self.storage, self)
        dlg.exec()
        if self.current_session:
            self._load_characters()
            self._display_session()
            self._update_token_label()

    def _on_render_rules(self):
        """气泡配色规则编辑：保存后热更新并刷新当前会话所有气泡。"""
        from src.ui.dialogs.render_rules_dialog import RenderRulesDialog
        dlg = RenderRulesDialog(self.storage, self)
        if dlg.exec() == QDialog.Accepted:
            # 配色规则已在对话框内 set_rules_config + 持久化，
            # 这里只需刷新当前已显示的气泡即可即时生效。
            self.chat_view.refresh_all_bubbles()
            self.status_label.setText("配色规则已更新")

    def _on_comfyui(self):
        from src.ui.dialogs.comfyui_dialog import ComfyUiDialog
        dlg = ComfyUiDialog(self.comfyui, self)
        dlg.exec()

    def _on_danbooru_settings(self):
        """Danbooru Tag 设置：库管理 + 模式 + 负面模板 + 测试。"""
        from src.ui.dialogs.danbooru_dialog import DanbooruSettingsDialog
        dlg = DanbooruSettingsDialog(self.storage, self.danbooru, self)
        dlg.exec()

    def _on_stats(self):
        from src.ui.dialogs.stats_dialog import StatsDialog
        dlg = StatsDialog(self.storage, self.services["stats"], self)
        dlg.exec()
        self._update_token_label()

    def _on_app_config(self):
        from src.ui.dialogs.app_config_dialog import AppConfigDialog
        dlg = AppConfigDialog(self.storage, self)
        dlg.exec()

    def _on_memory(self):
        from src.ui.dialogs.memory_dialog import MemoryDialog
        dlg = MemoryDialog(self.storage, self.services["memory"], self)
        dlg.exec()

    def _on_about(self):
        from src.ui.dialogs.about_dialog import AboutDialog
        dlg = AboutDialog(self)
        dlg.exec()


class _ImageRegenWorker(QThread):
    """后台用原 positive 重调 ComfyUI 重新生成图片（只换 seed）。

    独立于 ChatWorker（图片重生成不涉及聊天编排），避免卡 UI。
    finished_signal: (ok, 新图片路径或错误信息)。
    """
    finished_signal = Signal(bool, str)

    def __init__(self, comfyui, positive: str, negative: str, msg_id: str):
        super().__init__()
        self.comfyui = comfyui
        self.positive = positive
        self.negative = negative
        self.msg_id = msg_id

    def run(self):
        try:
            # 用原 positive 重调 ComfyUI（不传 dest_dir，落 images_dir，与聊天出图一致）
            new_path = self.comfyui.generate(self.positive, self.negative)
            if not new_path:
                self.finished_signal.emit(False, "图片生成失败，请检查 ComfyUI 服务与工作流")
                return
            self.finished_signal.emit(True, new_path)
        except Exception as e:
            self.finished_signal.emit(False, str(e))