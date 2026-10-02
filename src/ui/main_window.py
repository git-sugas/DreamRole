"""主窗口：整合侧栏、聊天区、角色面板。"""
from __future__ import annotations
import os

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QListWidget,
    QListWidgetItem, QLabel, QPushButton, QMenu, QStatusBar,
    QMessageBox, QFrame, QTextEdit, QDialog,
    QDialogButtonBox, QFileDialog, QApplication, QFormLayout,
    QStackedWidget, QTabBar,
)

from src.models import Session, Message, Character
from src.ui.chat_view import ChatView
from src.ui.chat_input import ChatInput
from src.ui.character_panel import CharacterPanel
from src.ui.chat_worker import ChatWorker
from src.ui.tabs.browse_tabs import CharacterBrowseTab, WorldBookBrowseTab
from src.ui.widgets.tts_player import TtsPlayer
from src.services.tts_service import TtsService


class MainWindow(QMainWindow):
    def __init__(self, services):
        super().__init__()
        self.services = services
        self.storage = services["storage"]
        self.orchestrator = services["orchestrator"]
        self.comfyui = services["comfyui"]
        self.danbooru = services.get("danbooru")
        self.world_sim = services.get("world_sim")

        self.current_session: Session | None = None
        self.current_messages: list[Message] = []
        self.current_characters: list[Character] = []
        self.worker: ChatWorker | None = None
        self._streaming_started = False
        self._pending_speaker = None
        self._stop_pending = False
        self._gen_error = False  # 本次生成是否出错（_on_error 置 True；_on_done 据此跳过自动 TTS）
        self._continue_target: Message | None = None  # 续写模式下的目标消息（原地更新）
        # TTS 相关
        self._tts_worker = None
        self.tts_service = TtsService(self.storage)
        self.tts_player = TtsPlayer(self)

        self.setWindowTitle("DreamRole")
        # 屏幕几何适配：默认按 1920x1080 设计启动并在可用区内居中；可用区不足（低分辨率屏
        # 或底部任务栏占高）时收缩到可用区，最小尺寸同步收紧防强制超出，用户可手动最大化。
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        target_w, target_h = 1920, 1080
        min_w, min_h = 1440, 900
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

        # [!] 启动时把持久化的渲染模式同步进 markup 模块状态：必须在 _load_sessions 之前，
        # 否则 _load_sessions -> _display_session -> chat_view.load_messages 渲染气泡时
        # markup 模块级 _render_mode 还是默认 "markup"，启动自动加载的会话气泡会用错模式
        # （用户设了 markdown/auto 也不生效，且设完 mode 后不会刷新已显示的气泡）。
        from src.utils import markup
        markup.set_render_mode(self.storage.load_app_config().render_mode)
        self._build_menu()
        self._build_ui()
        self._build_status_bar()
        self._load_sessions()
        # 默认进单聊页（tab 0）：QTabBar 初始即 0 不触发 currentChanged，需手动填充一次
        self._on_tab_changed(0)

    # ============ 菜单 ============
    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件")
        file_menu.addAction("新建会话", self._on_new_chat)
        file_menu.addAction("删除会话", self._on_delete_session)
        file_menu.addSeparator()
        file_menu.addAction("导出会话存档...", self._on_export_session)
        file_menu.addAction("导入会话存档...", self._on_import_session)
        file_menu.addAction("导入酒馆角色卡...", self._on_import_tavern_card)
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
        settings_menu.addSeparator()
        settings_menu.addAction("世界模拟设置...", self._on_world_sim_settings)
        settings_menu.addAction("TTS 语音设置", self._on_tts_settings)
        settings_menu.addAction("手机端服务...", self._on_remote_service)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("关于", self._on_about)

    # ============ UI ============
    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部居中三标签：单聊 / 群聊 / 会话
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 6, 0, 6)
        tab_row.addStretch(1)
        self.tab_bar = QTabBar()
        self.tab_bar.setObjectName("topTabBar")
        self.tab_bar.setExpanding(False)
        self.tab_bar.addTab("单聊")
        self.tab_bar.addTab("群聊")
        self.tab_bar.addTab("会话")
        self.tab_bar.addTab("世界模拟")
        tab_row.addWidget(self.tab_bar)
        tab_row.addStretch(1)
        outer.addLayout(tab_row)

        # 内容栈：[0]单聊角色卡墙 / [1]群聊世界书卡墙 / [2]会话页(现有三栏)
        self.stack = QStackedWidget()

        # [0] 单聊：角色卡墙（点卡 -> 单聊锁定建会话）
        self.single_tab = CharacterBrowseTab()
        self.single_tab.character_selected.connect(self._on_single_card_clicked)
        self.single_tab.manage_requested.connect(self._on_character_editor)
        self.stack.addWidget(self.single_tab)

        # [1] 群聊：世界书卡墙（点卡 -> 群聊锁定建会话）
        self.group_tab = WorldBookBrowseTab()
        self.group_tab.world_book_selected.connect(self._on_group_card_clicked)
        self.group_tab.manage_requested.connect(self._on_world_book)
        self.stack.addWidget(self.group_tab)

        # [2] 会话页：现有三栏（左会话列表 / 中聊天 / 右角色面板），原样搬进栈
        session_view = QWidget()
        sv_layout = QHBoxLayout(session_view)
        sv_layout.setContentsMargins(0, 0, 0, 0)
        sv_layout.setSpacing(0)
        splitter = QSplitter(Qt.Horizontal)

        # 左侧栏：会话列表
        left = QWidget()
        left.setFixedWidth(260)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)

        # [深改 2026-10-01] 会话页暗金化：左栏金字区头（纯视觉，聊天主链逻辑零改动）
        from src.ui.widgets.game_widgets import card_title
        title = card_title("会话列表")
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

        # [深改 2026-10-01] 会话标题大金字 + 金笔分隔（随会话切换动态换文本，样式恒定）
        from src.ui.widgets.game_widgets import banner_rule
        self.title_label = QLabel("选择或新建会话")
        self.title_label.setObjectName("bannerName")
        self.title_label.setContentsMargins(8, 0, 8, 4)
        center_layout.addWidget(self.title_label)
        center_layout.addWidget(banner_rule())
        center_layout.addSpacing(4)

        self.chat_view = ChatView()
        self.chat_view.message_context_menu.connect(self._on_message_context_menu)
        self.chat_view.avatar_clicked.connect(self._on_avatar_clicked)
        self.chat_view.image_clicked.connect(self._on_image_clicked)
        self.chat_view.segment_play_clicked.connect(self._on_play_segment)
        center_layout.addWidget(self.chat_view, 1)

        self.chat_input = ChatInput()
        self.chat_input.send_requested.connect(self._on_send)
        self.chat_input.continue_requested.connect(self._on_continue)
        self.chat_input.mode_changed.connect(self._on_mode_changed)
        self.chat_input.speaker_changed.connect(self._on_speaker_changed)
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
        sv_layout.addWidget(splitter)
        self.stack.addWidget(session_view)

        # [3] 世界模拟：世界卡墙 + 详情 + 场景交互（独立系统，复用 CardGrid/WorldDetailView/WorldSceneView）
        from src.ui.tabs.world_sim_tab import WorldSimBrowseTab
        self.world_sim_tab = WorldSimBrowseTab(self.world_sim, self.storage)
        self.world_sim_tab.new_world_requested.connect(self._on_new_world)
        self.world_sim_tab.settings_requested.connect(self._on_world_sim_settings)
        self.world_sim_tab.world_selected.connect(self._on_world_opened)
        # [修 2026-09-05] 新世界后台补图完成 -> 状态栏非阻塞提示
        self.world_sim_tab.image_backfill_done.connect(self._on_world_images_done)
        self.stack.addWidget(self.world_sim_tab)

        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # 标签切换：切栈 + 懒刷新目标浏览页（编辑角色/世界书后切回即时新鲜）
        self.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.stack.setCurrentIndex(0)  # 默认单聊

    def _on_tab_changed(self, idx: int):
        """切栈；单聊/群聊页懒刷新（从 storage 重新拉，保证编辑后新鲜）。"""
        self.stack.setCurrentIndex(idx)
        if idx == 0:
            self.single_tab.populate(self.storage.load_all_characters())
        elif idx == 1:
            self.group_tab.populate(self.storage.load_all_world_books())
        # idx == 2 会话页：session_list 已在 _load_sessions 维护，无需刷新；
        # [!] 但切到会话页时聊天区才从隐藏变可见，之前 load_messages 的 scroll_to_bottom
        # 是在页面零尺寸时算的 maximum（=0 没滚成），这里补一次让当前会话跳到最新。
        elif idx == 2:
            self.chat_view.scroll_to_bottom()
        # idx == 3 世界模拟页：懒刷新世界卡墙
        # [!] 不再强制 show_browse（P2 场景页加入后，切回应保留 browse/detail/scene 状态）；
        #     populate 只更新卡墙数据，不影响当前 stack page。
        elif idx == 3:
            self.world_sim_tab.populate(self.storage.load_all_worlds())

    def _build_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("就绪")
        self.status_bar.addWidget(self.status_label)
        self.token_label = QLabel("")
        # [深改 2026-10-01] token 统计行暖金调（旧默认冷灰与暗金系不搭）
        self.token_label.setStyleSheet("color:#8a7a54; font-size:11px;")
        self.status_bar.addPermanentWidget(self.token_label)
        # 远程服务状态：永久区控件（右侧），开启时显示「手机端: ip:port」，
        # 关闭时清空。用 addPermanentWidget 不被临时消息覆盖（与 token_label 风格一致）。
        self.remote_label = QLabel("")
        self.remote_label.setStyleSheet("color: #7aa2f7;")
        self.status_bar.addPermanentWidget(self.remote_label)
        self._refresh_remote_label()

    def _refresh_remote_label(self):
        """据远程服务实际运行态更新状态栏提示（即时启停后实时反映）。

        [!] 读 server.get_status() 而非磁盘 AppConfig.remote_enabled：服务可能因端口
            占用等异步失败处于 error 态，或在对话框内即时启停，磁盘字段与运行态已脱节。
        [!] 仅在对话框关闭等已有刷新点调用，不引入周期 QTimer（对话框内自有 1s 轮询
            做主动监控，状态栏只需在交互节点同步即可）。
        """
        try:
            from src.remote.server import get_status, get_local_ip
            st = get_status()
        except Exception:
            self.remote_label.setText("")
            return
        state = st.get("state", "stopped")
        if state == "running":
            try:
                ip = get_local_ip()
            except Exception:
                ip = "0.0.0.0"
            self.remote_label.setText(f"「手机端: {ip}:{st.get('port', 0)} 运行中」")
        elif state == "starting":
            self.remote_label.setText("「手机端: 启动中…」")
        elif state == "stopping":
            self.remote_label.setText("「手机端: 停止中…」")
        elif state == "error":
            self.remote_label.setText("「手机端: 启动失败」")
        else:  # stopped
            self.remote_label.setText("")

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
        # [!] 切会话前停止 TTS 播放与在跑的 TTS worker，避免旧会话音频串到新会话
        self.tts_player.stop()
        if self._tts_worker is not None:
            try:
                if self._tts_worker.isRunning():
                    self._tts_worker.wait(3000)
            except RuntimeError:
                pass
            self._tts_worker = None
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
        # 发言角色选择器：填当前会话角色，选中当前默认发言者（空回退首个角色）
        self.chat_input.set_speakers(self.current_characters)
        default_speaker = s.default_speaker_id or (s.character_ids[0] if s.character_ids else "")
        self.chat_input.set_current_speaker(default_speaker)
        self.chat_input.set_generating(False)

    def _on_new_chat(self):
        """会话页「+新建会话」/菜单入口：全自由版 GroupSetupDialog（默认 lock_type=""）。"""
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
            session = self._create_session_from_result(data)
            self._go_to_session_tab()
            self._select_session_row(session.id)

    def _on_single_card_clicked(self, char_id: str):
        """单聊页点角色卡 -> 单聊锁定版建会话（角色预选+隐藏角色 grid、类型锁定 single）。"""
        from src.ui.dialogs.group_setup import GroupSetupDialog
        chars = self.storage.load_all_characters()
        if not any(c.id == char_id for c in chars):
            QMessageBox.warning(self, "提示", "角色不存在，请刷新")
            return
        apis = self.storage.load_all_apis()
        world_books = self.storage.load_all_world_books()
        users = self.storage.load_all_users()
        dlg = GroupSetupDialog(chars, apis, world_books, users, self,
                               lock_type="single", preset_character_id=char_id)
        if dlg.exec():
            data = dlg.get_result()
            session = self._create_session_from_result(data)
            self._go_to_session_tab()
            self._select_session_row(session.id)

    def _on_group_card_clicked(self, wb_id: str):
        """群聊页点世界书卡 -> 群聊锁定版建会话（世界书锁定、类型锁定 group、可选角色）。"""
        from src.ui.dialogs.group_setup import GroupSetupDialog
        chars = self.storage.load_all_characters()
        if not chars:
            QMessageBox.warning(self, "提示", "请先创建角色卡")
            return
        apis = self.storage.load_all_apis()
        world_books = self.storage.load_all_world_books()
        users = self.storage.load_all_users()
        if not any(wb.id == wb_id for wb in world_books):
            QMessageBox.warning(self, "提示", "世界书不存在，请刷新")
            return
        dlg = GroupSetupDialog(chars, apis, world_books, users, self,
                               lock_type="group", preset_world_book_id=wb_id)
        if dlg.exec():
            data = dlg.get_result()
            session = self._create_session_from_result(data)
            self._go_to_session_tab()
            self._select_session_row(session.id)

    def _create_session_from_result(self, data: dict) -> Session:
        """据 GroupSetupDialog.get_result() 建 Session + 注入开场白 + 重载 UI。三入口共用。"""
        session = Session(
            title=data["title"],
            session_type=data["session_type"],
            character_ids=data["character_ids"],
            world_book_id=data.get("world_book_id", ""),
            user_id=data.get("user_id", ""),
            player_name=data.get("player_name", "用户"),
            group_mode=data.get("group_mode", "manual"),
            director_api_id=data.get("director_api_id", ""),
            group_memory_interval=int(data.get("group_memory_interval", 0) or 0),
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
            # [!] 开场白落库前替换 {{char}}/{{user}} 全局变量：开场白来自角色卡
            # first_message/alternate_greetings 原文，若不替换 {{user}} 会以字面量残留。
            # 后续进上下文走 HISTORY 块是 append-only 原样透传（§1 缓存友好契约，
            # context_builder 只对系统提示/角色信息块做 _fill_vars，不逐条替换历史），
            # 故必须在落库这一步把变量替换好。这里只替换两个全局变量，与 _fill_vars
            # 的「始终替换」语义一致（开场白场景用不到 {{user_description}}/角色字段变量）。
            greeting = (
                greeting
                .replace("{{char}}", char.name if char else "")
                .replace("{{user}}", session.player_name)
            )
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
        return session

    def _go_to_session_tab(self):
        """建会话后切到会话页（tab 索引 2）。"""
        self.tab_bar.setCurrentIndex(2)

    def _select_session_row(self, session_id: str):
        """在会话列表里选中指定会话。"""
        for i in range(self.session_list.count()):
            if self.session_list.item(i).data(Qt.UserRole) == session_id:
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

    def _on_import_tavern_card(self):
        """从 SillyTavern 角色卡导入角色卡（含第一条消息/简介/世界书等）。

        支持 JSON（chara_card_v2/v3）与 PNG（JSON 以 base64 嵌在 tEXt chunk）：
        角色字段映射到 Character，character_book.entries 映射到 WorldBook。
        PNG 卡的图片本身当角色头像。本地已有同名角色卡/世界书时复用，否则新建。
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "导入酒馆角色卡", "", "角色卡 (*.json *.png)"
        )
        if not path:
            return

        import json as _json
        is_png = path.lower().endswith(".png")
        avatar_set = False
        try:
            if is_png:
                from src.services.tavern_importer import parse_tavern_card_from_png
                char, world_book = parse_tavern_card_from_png(path)
                avatar_set = bool(char.avatar)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                from src.services.tavern_importer import parse_tavern_card
                char, world_book = parse_tavern_card(data)
        except (OSError, _json.JSONDecodeError) as e:
            QMessageBox.warning(self, "导入失败", f"读取文件失败: {e}")
            return
        except ValueError as e:
            QMessageBox.warning(self, "导入失败", f"不是有效的酒馆角色卡: {e}")
            return

        # 记录是否同名复用（导入前查本地同名）
        reused = bool(char.name) and any(
            c.name == char.name for c in self.storage.load_all_characters()
        )
        landed = self.storage.import_tavern_character(char, world_book)
        # 刷新右侧角色面板
        self._load_characters()

        msg = f"已导入角色卡「{landed.name}」。"
        if avatar_set:
            msg += "头像已设为图片本身。"
        if reused:
            msg += "（本地已有同名角色，已复用，未覆盖本地修改）"
        if world_book is not None:
            wb_name = world_book.name or landed.name
            msg += f"\n世界书「{wb_name}」已按需创建/复用（{len(world_book.entries)} 条条目）。"
        QMessageBox.information(self, "导入成功", msg)

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

    def _on_speaker_changed(self, character_id: str):
        """发言角色选择器选角：设默认发言者 + 高亮面板，不触发发送。
        用户接着输入文字点发送，走 send_and_respond 用新 default_speaker_id。
        [!] 不调 trigger_character（不构造「轮到X发言」trigger、不发送），
            与点头像直接发言（trigger_character）区别：点头像是立即发言，
            选择器是「先设好谁回再发」。
        """
        if not self.current_session or not character_id:
            return
        # 生成中不响应选角（防竞态）；单聊/auto 模式选择器本应禁用，兜底 return
        if self.worker or self.current_session.session_type != "group":
            return
        if self.current_session.group_mode == "auto":
            return
        char = self.storage.load_character(character_id)
        if not char:
            return
        # 高亮面板该角色（与点头像路径一致）
        self.character_panel.highlight_character(character_id)
        # 持久化为默认发言者（复用 _remember_speaker 写 default_speaker_id + save_session）
        self.orchestrator._remember_speaker(self.current_session, char)

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
            # [!] 发言角色选择器跟随模式：手动启用、自动禁用（auto 由导演 LLM 选角）
            self.chat_input.set_speaker_enabled(mode == "manual")

    def _start_worker(self, action: str, content: str = "", character=None, target_msg=None):
        if not self.current_session:
            return
        self.chat_input.set_generating(True)
        self._streaming_started = False
        self._pending_speaker = None
        self._stop_pending = False
        self._gen_error = False  # 每次生成前清错误标志（_on_error 会置 True）
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
        # 执行 HTTP 请求导致「QThread: Destroyed while still running」。
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(3000)  # 最多等 3 秒
        # [!] TTS 播放器停止（避免关窗后音频继续播）
        self.tts_player.stop()
        # [!] TTS worker 也要等待：TTS 合成是阻塞 HTTP 调用，关窗时若仍在跑，
        # worker emit 信号会触发已销毁接收者致崩溃（与 _img_regen_worker 同型）。
        tts_worker = getattr(self, "_tts_worker", None)
        if tts_worker is not None:
            try:
                if tts_worker.isRunning():
                    try:
                        tts_worker.finished_signal.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                    tts_worker.wait(10000)  # 最多等 10 秒
            except RuntimeError:
                pass
            self._tts_worker = None
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
        # [!] P2 场景交互 worker：场景页可能正在跑 settle/narrate 长任务，关窗前清理
        # （仿 WorldGenDialog closeEvent：disconnect + cancel + wait，守 §15）。
        if getattr(self.world_sim_tab, "_scene_view", None) is not None:
            if self.world_sim_tab.cleanup_scene() is False:
                event.ignore()
                scene_view = self.world_sim_tab._scene_view
                if not getattr(self, "_scene_close_pending", False):
                    self._scene_close_pending = True
                    def _retry_scene_close():
                        if not self._scene_close_pending:
                            return
                        self._scene_close_pending = False
                        self.close()
                    scene_view._worker.finished.connect(_retry_scene_close)
                    if scene_view._worker.isFinished():
                        QTimer.singleShot(0, _retry_scene_close)
                return
        # [修 2026-09-10] 世界补图/地点背景 worker 同守 §15：原实现无人清理
        # （parent=None + 只被 tab 持引用），关窗时若仍在跑会随对象销毁被 GC 致崩溃。
        if getattr(self, "world_sim_tab", None) is not None:
            try:
                self.world_sim_tab.cleanup_workers()
            except (RuntimeError, AttributeError):
                pass
        # [!] 世界生成 worker 由 WorldGenDialog 内部管理（模态 exec，主窗在生成期不可关），
        # 对话框自身 closeEvent/reject 已 cancel+wait，主窗无需重复处理。
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
        self._gen_error = True  # 标记本次生成出错，_on_done 据此跳过自动 TTS
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
        # [!] 防御性收尾：无论下方哪步抛异常，都必须恢复按钮态，否则 UI 永久卡在
        # 「停止」态（worker 数据已落库但 UI 不恢复，用户只能重启）。用 try/finally
        # 包裹，finally 里强制 set_generating(False) + 清 worker/streaming 标志。
        try:
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
                self.status_label.setText("就绪")
                self._update_token_label()
                if self.current_session:
                    self.current_messages = self.storage.load_messages(self.current_session.id)
                # 自动 TTS 钩子（续写模式）：LLM 出错则跳过，不朗读半截/无意义内容
                if not self._gen_error:
                    self._maybe_auto_tts()
                return
            # 用户停止且占位气泡未升级为正式消息时，清理空占位气泡
            if self._stop_pending and self._streaming_started:
                self.chat_view.cancel_streaming()
            else:
                self.chat_view.finish_streaming()
            self._streaming_started = False
            self._stop_pending = False
            self.worker = None
            self.status_label.setText("就绪")
            self._update_token_label()
            # 重新加载消息以同步折叠状态
            if self.current_session:
                self.current_messages = self.storage.load_messages(self.current_session.id)
            # 自动 TTS 钩子（正常生成完成）：LLM 出错则跳过，不朗读半截/无意义内容
            if not self._gen_error:
                self._maybe_auto_tts()
        except Exception as e:
            # [!] 收尾阶段异常不可吞（用户需知道为何状态没恢复），但按钮态必须恢复。
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "收尾异常", f"生成已完成但收尾出错，UI 状态已强制恢复：\n{e}")
        finally:
            # [!] 强制恢复：无论上面是否异常，按钮/输入框/worker 标志都必须复位。
            self._streaming_started = False
            self._stop_pending = False
            self.worker = None
            self.chat_input.set_generating(False)

    # ============ TTS 语音 ============
    def _on_tts_settings(self):
        """打开 TTS 语音设置对话框。"""
        from src.ui.dialogs.tts_dialog import TtsSettingsDialog
        dlg = TtsSettingsDialog(self.storage, self)
        dlg.exec()
        # 对话框关闭后重载 TTS 服务（配置可能已改）
        self.tts_service = TtsService(self.storage)

    # ============ 世界模拟（独立 SLG 系统，§21）============
    def _on_world_sim_settings(self):
        """打开世界模拟设置对话框。"""
        from src.ui.dialogs.world_sim_settings_dialog import WorldSimSettingsDialog
        dlg = WorldSimSettingsDialog(self.storage, self)
        dlg.exec()

    def _on_new_world(self):
        """打开世界生成对话框 -> 生成成功后预览 -> 保存落库。"""
        from src.ui.dialogs.world_gen_dialog import WorldGenDialog
        from src.ui.dialogs.world_preview_dialog import WorldPreviewDialog
        if not self.world_sim:
            QMessageBox.warning(self, "提示", "世界模拟服务未初始化。")
            return
        preset = self.storage.load_world_sim_preset()
        gen_dlg = WorldGenDialog(self.world_sim, preset, self)
        # worker 由对话框内部管理；这里只在对话框 accept 后取结果
        if gen_dlg.exec():
            world = gen_dlg.get_world()
            if world is None:
                return
            # 预览/微调
            prev = WorldPreviewDialog(world, self)
            if prev.exec():
                saved = prev.get_world()
                # [P11b] 开局属性分配（预览通过后、天赋选择前）：5 维基线 5 + 10 点自选（QQ 码 +50）
                from src.ui.dialogs.attribute_alloc_dialog import AttributeAllocDialog
                attr_dlg = AttributeAllocDialog(saved, self)
                if not attr_dlg.exec():
                    return  # 取消属性分配则不保存
                # [P9] 开局天赋选择（预览通过后、保存前）：锁死1 + 天赋点自选 + QQ 彩蛋码
                from src.ui.dialogs.talent_select_dialog import TalentSelectDialog
                talent_dlg = TalentSelectDialog(saved, self)
                if not talent_dlg.exec():
                    return  # 取消天赋选择则不保存
                self.storage.save_world(saved)
                # [修 2026-09-05 用户指示] 生图不再阻塞生成管线：保存即可玩，图片
                # 后台批量补（完成后状态栏提示 + 详情页自动刷新）
                if gen_dlg.gen_images_enabled():
                    self.world_sim_tab.start_auto_backfill(saved)
                QMessageBox.information(
                    self, "已保存",
                    f"世界「{saved.name}」已保存，可以开始游玩！"
                    + ("\n世界图片正在后台生成，完成后自动显示。" if gen_dlg.gen_images_enabled() else ""))
                # 刷新世界模拟卡墙并切过去
                self.world_sim_tab.populate(self.storage.load_all_worlds())
                self.world_sim_tab.show_detail(saved)
                self.tab_bar.setCurrentIndex(3)

    def _on_world_images_done(self, world_id: str, done: int, failed: int):
        """[修 2026-09-05] 新世界后台补图完成：状态栏轻提示（不打断游玩）。"""
        world = self.storage.load_world(world_id)
        name = getattr(world, "name", "世界") if world is not None else "世界"
        msg = f"「{name}」图片已生成 {done} 张"
        if failed:
            msg += f"（{failed} 张失败，可在详情页「补缺失图」重试）"
        self.statusBar().showMessage(msg, 8000)

    def _on_world_opened(self, world_id: str):
        """点世界卡 -> 进详情页。"""
        world = self.storage.load_world(world_id)
        if world is None:
            QMessageBox.warning(self, "提示", "世界加载失败，可能已被删除。")
            self.world_sim_tab.populate(self.storage.load_all_worlds())
            return
        self.world_sim_tab.show_detail(world)

    def _get_session_api(self):
        """解析当前会话的发言 API（供情绪 LLM 兜底用）。

        单聊用角色 api_id，群聊用 session.director_api_id；都不行传 None 让 TtsService 兜底。
        """
        if not self.current_session:
            return None
        if self.current_session.session_type == "group":
            if self.current_session.director_api_id:
                return self.storage.load_api(self.current_session.director_api_id)
        if self.current_characters:
            return self.storage.load_api(self.current_characters[0].api_id)
        return None

    def _maybe_auto_tts(self):
        """自动 TTS：对最后一条文字 assistant 消息、且自动配音开启时触发。

        [!] 从末尾往前找最后一条「非 image_only 的 assistant 消息」--AI 回复含
        [img:...] 时，图片消息（is_image_only=True）会排在文字 assistant 之后落库，
        占据 current_messages[-1]。若只看绝对末条会命中图片消息而跳过 TTS，必须
        跳过图片消息往前找文字 assistant（图片消息不该配音，也不进 TTS 流程）。
        summary 消息同理跳过。
        """
        try:
            preset = self.storage.load_tts_preset()
            if not preset.auto_tts_enabled or not preset.tts_api_key:
                return
            if not self.current_messages:
                return
            # 从末尾往前找最后一条可配音的 assistant（跳过图片/总结）
            target = None
            for m in reversed(self.current_messages):
                if m.role != "assistant":
                    continue
                if m.is_image_only or m.is_summary:
                    continue
                target = m
                break
            if target is None:
                return
            if target.has_tts:
                return  # 已生成过不重复
            # [!] auto_play=False：自动触发不强制播放，由 _on_tts_done 读 preset.auto_play_enabled
            # 决定（关则仅生成音频落到气泡，用户点段播放）
            self._start_tts_worker(target, auto_play=False)
        except Exception as e:
            print(f"[TTS] 自动配音触发失败: {e}")

    def _start_tts_worker(self, msg: Message, auto_play: bool = True):
        """启动 TTS worker 后台合成音频。

        auto_play 语义：
        - True：手动「生成并朗读」显式触发，合成后强制播放全段（无视 preset.auto_play_enabled）。
        - False：自动触发（_maybe_auto_tts），合成后由 _on_tts_done 读 preset.auto_play_enabled
          决定是否播放（关则仅生成音频落到气泡，用户点段播放）。
        """
        # 防重复：若已有 TTS worker 在跑，先等它结束
        if self._tts_worker is not None:
            try:
                if self._tts_worker.isRunning():
                    return
            except RuntimeError:
                pass
            self._tts_worker = None
        self._tts_auto_play = auto_play
        self._tts_worker = _TtsWorker(self.tts_service, msg, self._get_session_api())
        self._tts_worker.finished_signal.connect(self._on_tts_done)
        self._tts_worker.finished.connect(self._tts_worker.deleteLater)
        self._tts_worker.start()

    def _on_tts_done(self, ok, msg_id, segments, error):
        """TTS 合成完成回调。

        segments 是 [{text, path}, ...] 段映射（process_message_tts 返回）。
        据此更新 msg.audio_paths / tts_segments / has_tts 并落库，刷新气泡段列表，
        再按 auto_play 决定是否自动播放：
        - 手动「生成并朗读」（_tts_auto_play=True）：强制播放全段。
        - 自动触发（_maybe_auto_tts）：读 preset.auto_play_enabled，关则不播
          （仅生成音频落到气泡，用户可点段播放）。
        """
        self._tts_worker = None
        if not ok:
            self.statusBar().showMessage(f"TTS 失败: {error}", 4000)
            return
        if not segments:
            return  # 无命中段，静默
        # 更新消息的 audio_paths + tts_segments + has_tts，落库
        msg = next((m for m in self.current_messages if m.id == msg_id), None)
        if not msg:
            # 消息已被删除/切会话（current_messages 已换）：不落库也不播放，
            # 避免孤儿音频与跨会话串音
            return
        msg.audio_paths = [s["path"] for s in segments if s.get("path")]
        msg.tts_segments = segments
        msg.has_tts = True
        self.storage.save_message(msg)
        # 原地刷新气泡段列表（TTS 生成后气泡可点击播放各段）
        self.chat_view.refresh_tts_segments(msg)
        # 播放：手动触发强制播（_tts_auto_play=True）；自动触发读 preset.auto_play_enabled
        should_play = getattr(self, "_tts_auto_play", True)
        if not should_play:
            # 自动触发且未显式要求播放时，按 preset.auto_play_enabled 决定
            try:
                preset = self.storage.load_tts_preset()
                should_play = preset.auto_play_enabled
            except Exception:
                should_play = True  # 读配置失败兜底播放（不阻断主流程）
        if should_play and msg.audio_paths:
            self.tts_player.play_sequence(msg.audio_paths)

    def _on_play_tts(self, msg: Message):
        """播放已有 TTS 音频。"""
        if msg.audio_paths:
            self.tts_player.play_sequence(msg.audio_paths)

    def _on_generate_and_play_tts(self, msg: Message):
        """生成 TTS 并播放（手动模式）。"""
        # 若已有旧音频，先清理再重新生成
        if msg.audio_paths:
            self.tts_service.cleanup_message_audio(msg)
            self.storage.save_message(msg)
        self._start_tts_worker(msg, auto_play=True)

    def _on_play_segment(self, msg_id: str, path: str):
        """点击气泡某 TTS 段播放该段音频（单段播放）。

        [!] 不校验 msg 是否在 current_messages（段点击来自已渲染气泡，消息必然存在）；
        直接用传入的 path 调 play_sequence 单段播放。
        """
        if path:
            self.tts_player.play_sequence([path])

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
        # [!] 按 id 从 current_messages 取最新 msg 对象：气泡持有的 self.message 可能是
        # load_messages 重新加载前的旧引用（_on_done 会整体替换 current_messages 为新对象），
        # 旧引用的 has_tts/audio_paths 不会被 _on_tts_done 更新，会导致已生成 TTS 的消息
        # 仍显示「生成并朗读」。此处用最新对象判断 TTS 状态，避免引用断裂。
        latest = next((m for m in self.current_messages if m.id == msg.id), None)
        if latest is not None:
            msg = latest
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
        # TTS 朗读（assistant 消息）
        if msg.role == "assistant":
            if msg.has_tts and msg.audio_paths:
                menu.addAction("🔊 朗读").triggered.connect(
                    lambda: self._on_play_tts(msg)
                )
                menu.addAction("⏹️ 停止朗读").triggered.connect(self.tts_player.stop)
            else:
                menu.addAction("🔊 生成并朗读").triggered.connect(
                    lambda: self._on_generate_and_play_tts(msg)
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
        # [!] 编辑后旧 TTS 音频失效（内容已变，旧段音频对应的是旧文本）：清理音频文件
        # + tts_segments + has_tts，刷新气泡移除段列表，避免点段播放到旧文本的音频。
        if msg.has_tts:
            self.tts_service.cleanup_message_audio(msg)
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

    def _on_image_clicked(self, msg: Message):
        """点击气泡内图片：弹出对话框放大查看原图。

        与头像预览（_on_avatar_clicked）对称，但图片来源是消息的 image_path
        （ComfyUI 生成，静态 PNG 无 GIF 需处理），无需经角色卡查 avatar 文件名。
        无 image_path / 文件缺失 / 读取失败时弹提示。
        """
        image_path = msg.image_path or ""
        if not image_path:
            QMessageBox.information(self, "无图片", "该消息没有关联的图片。")
            return
        if not os.path.exists(image_path):
            QMessageBox.warning(self, "文件缺失", f"图片文件不存在:\n{image_path}")
            return
        from PySide6.QtGui import QPixmap
        pix = QPixmap(image_path)
        if pix.isNull():
            QMessageBox.warning(self, "读取失败", f"无法读取图片:\n{image_path}")
            return
        # 大图缩放适配屏幕（保持比例），小图原尺寸显示。
        # [!] 预留比头像预览（-120）更大（-200）：本弹窗下方还有 caption(提示词)
        # + 路径 + Close 按钮，长 prompt 换行后占高较多，预留不足会把 Close 挤出屏幕。
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        max_w = avail.width() - 80 if avail else 800
        max_h = avail.height() - 200 if avail else 540
        if pix.width() > max_w or pix.height() > max_h:
            pix = pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"图片预览 - {msg.character_name or '用户'}")
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(8, 8, 8, 8)
        lbl = QLabel()
        lbl.setPixmap(pix)
        lbl.setAlignment(Qt.AlignCenter)
        vl.addWidget(lbl)
        # 生成提示词 caption（可选中复制）。[!] setTextFormat(PlainText)：prompt 可能
        # 含 <lora:...> 等尖括号片段，AutoText 会当 HTML 误解析吞标签，强制纯文本。
        if msg.content:
            cap = QLabel(msg.content)
            cap.setWordWrap(True)
            cap.setTextFormat(Qt.PlainText)
            cap.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cap.setStyleSheet("color: #565f89; font-size: 11px;")
            vl.addWidget(cap)
        # 文件路径（可选中复制）
        path_label = QLabel(image_path)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setStyleSheet("color: #565f89; font-size: 11px;")
        vl.addWidget(path_label)
        from PySide6.QtWidgets import QDialogButtonBox
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
        """会话列表右键菜单：上文总结设置 / 群聊记忆设置(仅群聊) / 切换用户。"""
        if not self.current_session:
            return
        s = self.current_session
        menu = QMenu(self)
        menu.addAction("上文总结设置…", self._edit_session_summary)
        # [!] 群聊记忆设置仅群聊会话显示（单聊用角色个人 memory_config，无会话级配置）
        if getattr(s, "session_type", "single") == "group":
            menu.addAction("群聊记忆设置…", self._edit_group_memory)
        menu.addAction("切换用户…", self._switch_session_user)
        menu.exec(self.session_list.mapToGlobal(pos))

    def _edit_group_memory(self):
        """对当前群聊会话修改会话级记忆整理触发条数（group_memory_interval）。

        0=禁用走各角色卡个人配置；>0=每 N 条全量消息触发，窗口内发言过的角色都整理这同一段对话。
        """
        from PySide6.QtWidgets import QSpinBox, QDialogButtonBox
        s = self.current_session
        if not s or getattr(s, "session_type", "single") != "group":
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"群聊记忆设置 - {s.title or s.id[:8]}")
        dl = QVBoxLayout(dlg)
        hint = QLabel(
            "会话级配置（仅群聊）：按全量消息条数(用户+所有角色)触发记忆整理。\n"
            "0=禁用，走各角色卡个人配置(默认)；\n"
            ">0=每N条全量消息触发一次，窗口内发言过的角色都整理这同一段对话(全群共用一个边界)。\n"
            "记忆模式仍用各角色卡自己的设置(AI总结/Embedding混合)。改完保存即时生效。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        dl.addWidget(hint)
        form = QFormLayout()
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(getattr(s, "group_memory_interval", 0) or 0)
        spin.setSpecialValueText("禁用(用角色个人配置)")
        spin.setToolTip(
            "每 N 条全量未折叠消息触发一次记忆整理。\n"
            "触发后，这段 N 条消息里发言过的角色都各自整理(用各自旧记忆+这同一段新对话)。\n"
            "全群共用一个边界：整理过的段下次不会重复整理。"
        )
        form.addRow("触发条数(条):", spin)
        dl.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            s.group_memory_interval = spin.value()
            s.touch()
            self.storage.save_session(s)
            QMessageBox.information(self, "已保存", f"群聊「{s.title or s.id[:8]}」记忆设置已保存。")

    def _edit_session_summary(self):
        """对当前会话临时修改上文总结三参数（会话级，不入预设）。"""
        from PySide6.QtWidgets import QSpinBox, QCheckBox
        s = self.current_session
        if not s:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"上文总结设置 — {s.title or s.id[:8]}")
        dl = QVBoxLayout(dlg)
        hint = QLabel(
            "会话级配置：与角色记忆独立。可随时开关、调整阈值。改完保存即时生效。"
            "注意：阈值按全部未折叠消息（含用户与 AI 发言）计数，角色记忆则按 AI 回复条数计数，口径不同。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        dl.addWidget(hint)
        form = QFormLayout()
        en_chk = QCheckBox("启用上文自动总结")
        en_chk.setChecked(s.auto_summary_enabled)
        form.addRow(en_chk)
        th_spin = QSpinBox(); th_spin.setRange(5, 500); th_spin.setValue(s.auto_summary_threshold)
        th_spin.setToolTip(
            "未折叠的活跃消息（含用户和 AI 双方的发言）累计超过此数即触发一次自动总结。"
            "按全部消息计数，不是只数 AI 回复（与角色记忆的「每 N 条 AI 回复触发」口径不同）。"
        )
        form.addRow("触发阈值(条):", th_spin)
        ct_spin = QSpinBox(); ct_spin.setRange(2, 100); ct_spin.setValue(s.auto_summary_count)
        ct_spin.setToolTip("每次总结取最早的 N 条活跃消息（含用户与 AI 发言）喂给总结模型，其余消息保留。")
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

    def _on_remote_service(self):
        """手机端服务（远程联动）设置：开关/端口/配对码/IP/二维码 + 即时启停。

        [!] 即时启停：对话框内「启动/停止/重启」按钮 + 保存按钮按差异启停，无需重启
            exe；对话框关闭后刷新状态栏 remote_label 反映真实运行态（含失败态）。
        """
        from src.ui.dialogs.remote_service_dialog import RemoteServiceDialog
        dlg = RemoteServiceDialog(self.storage, self.services, self)
        dlg.exec()
        # 对话框关闭后刷新状态栏提示（即使取消也刷新，反映真实运行态）
        self._refresh_remote_label()

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


class _TtsWorker(QThread):
    """后台执行 TTS 语音合成（避免阻塞 UI）。

    独立于 ChatWorker（TTS 不涉及聊天编排），仿 _ImageRegenWorker。
    finished_signal: (ok, msg_id, segments列表[{text,path}], 错误信息)。
    """
    finished_signal = Signal(bool, str, list, str)

    def __init__(self, tts_service, msg: Message, session_api=None):
        super().__init__()
        self.tts_service = tts_service
        self.msg = msg
        self.session_api = session_api

    def run(self):
        try:
            segments = self.tts_service.process_message_tts(
                self.msg, session_api=self.session_api
            )
            self.finished_signal.emit(True, self.msg.id, segments, "")
        except Exception as e:
            self.finished_signal.emit(False, self.msg.id, [], str(e))
