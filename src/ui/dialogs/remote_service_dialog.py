"""远程服务（手机端联动）设置对话框：开关 + 端口 + 配对码 + 局域网 IP + 二维码。

启用后即时在后台 daemon 线程跑 fastapi/uvicorn 监听 0.0.0.0:port，
手机浏览器访问 PC 局域网 IP:端口（或扫下方二维码）经配对码认证后可查看会话并聊天。

[!] 服务开关 + 端口 + 配对码改完即时启停（无需重启 exe）：保存按钮按差异自动
    启动/停止/重启 uvicorn；顶部「启动/停止/重启」按钮可单独操作。
[!] 保存遵循 AppConfig 单例写入规范：先 load_app_config 再改 remote_* 字段，避免
    覆盖破限/render_mode 等其它对话框管的字段（与破限设置对话框 _on_save 风格一致）。
[!] 配对码可重置（生成新 6 位码）。首次开启远程服务时若配对码为空，server.py 会
    自动生成并落盘，此对话框只展示当前码（不强制填，空则启动时生成）。
"""
from __future__ import annotations
import secrets
from datetime import datetime

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QCheckBox, QLabel,
    QPushButton, QHBoxLayout, QMessageBox, QSpinBox,
    QLineEdit, QFrame,
)
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import Qt, QTimer, QThread, Signal


# 测试用 QR 库，避免 dialog import 时强依赖（用于生成本地二维码展示）
try:
    import qrcode
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False


class _RemoteOpWorker(QThread):
    """后台跑远程服务启停操作，避免主线程同步 join 卡 UI。

    [!] stop_server 内 th.join(timeout=5.0) 同步等待 daemon 线程退出，主线程直调会
        最长阻塞 UI 5 秒（事件循环冻结、状态栏定时器不触发、用户看到假死）。本 worker
        把启停调用丢到子线程，finished 信号回主线程刷状态 + 弹反馈。
    [!] start_server 的 _check_port_free 同步 bind 是 ms 级（不阻塞），但因统一起见也
        走 worker，避免预检偶发慢时 卡 UI；且 start 失败要弹窗也需异步反馈。
    [!] 生命周期：parent=None 自管（不挂 dialog）。RemoteServiceDialog 非单例（每次 new，
        exec 返回后 GC），dialog.accept() 后 Python refcount 归零会级联删除 parent=self
        的子 QThread，若 worker 仍 running 触发 Qt abort「Destroyed while still running」。
        故 worker 不做 dialog 子对象，由 dialog._active_workers set 持 Python 引用防 GC，
        finished 信号移除引用 + deleteLater。
    """

    op_done = Signal(str, bool, str)  # (op, success, message)

    def __init__(self, op: str, services=None, storage=None, save_action=None):
        # [!] 不传 parent：避免 dialog GC 级联删 worker（见类 docstring）
        super().__init__(None)
        self._op = op            # "start" | "stop" | "restart" | "save_apply"
        self._services = services
        self._storage = storage
        self._save_action = save_action  # 保存路径用：实际启停动作 "stop"/"start"/"restart"/"none"

    def run(self):
        op = self._op
        try:
            if op == "save_apply":
                # 保存路径：cfg 已落盘，按预计算的 save_action 启停
                action = self._save_action or "none"
                if action == "none":
                    self.op_done.emit(op, True, "手机端服务设置已保存。")
                elif action == "stop":
                    from src.remote import stop_server
                    stopped = stop_server()
                    if stopped:
                        self.op_done.emit(op, True, "手机端服务设置已保存，服务已停止。")
                    else:
                        self.op_done.emit(op, False, "设置已保存，但停止超时，服务仍在关闭中。")
                elif action == "start":
                    from src.remote import start_server
                    start_server(self._services, self._storage.load_app_config())
                    self.op_done.emit(op, True, "手机端服务设置已保存，服务已启动。")
                elif action == "restart":
                    from src.remote import restart_server
                    restart_server(self._services, self._storage.load_app_config())
                    self.op_done.emit(op, True, "手机端服务设置已保存，服务已重启以应用新端口/配对码。")
                else:
                    self.op_done.emit(op, False, f"未知 save_action: {action}")
            elif op == "stop":
                from src.remote import stop_server
                stopped = stop_server()
                if stopped:
                    self.op_done.emit(op, True, "服务已停止。")
                else:
                    self.op_done.emit(op, False, "停止超时，服务仍在关闭中，请稍候再试。")
            elif op == "start":
                if not self._services:
                    self.op_done.emit(op, False, "内部错误：缺少 services。")
                    return
                cfg = self._storage.load_app_config()
                if not cfg.remote_enabled:
                    self.op_done.emit(op, False, "请先勾选「启用手机端服务」并点「保存」，再启动。")
                    return
                from src.remote import start_server
                start_server(self._services, cfg)
                self.op_done.emit(op, True, "服务已启动。")
            elif op == "restart":
                if not self._services:
                    self.op_done.emit(op, False, "内部错误：缺少 services。")
                    return
                cfg = self._storage.load_app_config()
                from src.remote import restart_server
                restart_server(self._services, cfg)
                self.op_done.emit(op, True, "服务已重启。")
            else:
                self.op_done.emit(op, False, f"未知操作: {op}")
        except Exception as e:
            self.op_done.emit(op, False, f"{type(e).__name__}: {e}")


_HINT = (
    "远程服务说明：\n"
    "1. 勾选「启用」并保存，即在后台启动远程服务（端口 listen 0.0.0.0），无需重启 exe；\n"
    "2. 手机与电脑处于同一局域网，用手机浏览器访问下方「局域网地址」，或扫描右侧二维码；\n"
    "3. 首次访问输入 6 位配对码（或扫已带 ?code=xxxxxx 的二维码直接登录）即可，浏览器记住 30 天；\n"
    "4. 手机端只能查看已有会话并聊天，不能创建/修改会话或角色，所有数据仍在 PC 端；\n"
    "5. 流式输出跟随各会话绑定 API 的 streaming 设置（PC 端怎么配，手机端就怎么收）。\n\n"
    "[!] 服务默认关闭。不勾选启用，不起服务、不占端口。\n"
    "[!] 仅局域网用。外网访问需自行做内网穿透（frp/zerotier 等），存在安全风险请谨慎。\n"
    "[!] Windows 首次启动可能弹防火墙提示，需放行 Python/exe 监听该端口。"
)


class RemoteServiceDialog(QDialog):
    """远程服务（手机端联动）设置对话框（全局单例 AppConfig 的 remote_* 字段）。

    [!] 即时启停版本：构造需传 services（含 storage/orchestrator 等），用于
        start_server/restart_server 调用（stop_server 不需 services）。
    """

    def __init__(self, storage, services, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.services = services
        self.setWindowTitle("手机端服务")
        self.resize(760, 660)
        self.setMinimumSize(640, 560)
        # [!] _active_workers: 持 Python 引用防 worker GC（worker parent=None 自管，
        #    不挂 dialog；dialog 非单例 accept 后 GC 不会级联删 worker）。finished 移除。
        self._active_workers: set = set()
        self._build_ui()
        self._load()
        # 1s 轮询运行状态：刷新顶部状态标签 + 启动/停止/重启按钮启用态。
        # [!] 连 finished 信号 stop 定时器：accept/reject/done 走 hide 不触发 closeEvent，
        #     只有 finished 信号能可靠覆盖三种关闭路径（X 按钮/回车确认/代码 accept/reject）。
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(1000)
        self.finished.connect(self._on_dialog_finished)
        self._refresh_status()

    def _on_dialog_finished(self, _result):
        """对话框结束（accept/reject/close 任一路径）时停定时器 + 等 worker 退。

        [!] wait(6000) 覆盖 stop_server 内 th.join(5.0) + overhead：worker 跑 stop 时
            最长 5s+，wait 6s 确保正常退出；超时则 worker 仍在后台跑（parent=None 不被
            dialog GC 删），下次 finished/finished 信号链自然清理，无崩溃风险。
        """
        self._status_timer.stop()
        # 等所有活跃 worker 退出（最多 6s 覆盖 stop_server 的 5s join）
        for w in list(self._active_workers):
            if w.isRunning():
                w.wait(6000)

    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        from src.ui.widgets.game_widgets import page_header
        layout.addWidget(page_header("手机端服务"))

        # 运行状态区（顶部）：状态标签 + 启动/停止/重启按钮
        status_box = QFrame()
        status_box.setFrameShape(QFrame.StyledPanel)
        status_box.setStyleSheet("QFrame#statusBox { background: #1a1b26; border: 1px solid #2A2E45; border-radius: 4px; }")
        status_box.setObjectName("statusBox")
        sl = QVBoxLayout(status_box)
        sl.setContentsMargins(10, 8, 10, 8)
        self.status_label = QLabel("检测中…")
        self.status_label.setStyleSheet("font-weight: bold; color: #565f89;")
        self.status_label.setWordWrap(True)
        sl.addWidget(self.status_label)
        btns = QHBoxLayout()
        self.start_btn = QPushButton("启动")
        self.stop_btn = QPushButton("停止")
        self.restart_btn = QPushButton("重启")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.restart_btn.clicked.connect(self._on_restart)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.restart_btn)
        btns.addStretch()
        sl.addLayout(btns)
        layout.addWidget(status_box)

        # 启用开关
        self.enabled_chk = QCheckBox(
            "启用手机端服务（保存后即时在后台启动，监听局域网供手机浏览器访问）"
        )
        layout.addWidget(self.enabled_chk)

        # 表单：端口 + 配对码
        form = QFormLayout()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8000)
        self.port_spin.setToolTip("监听端口，1-65535，常用 8000/8080")
        form.addRow("监听端口：", self.port_spin)

        # 局域网 IP（只读展示）
        self.ip_label = QLabel("（点击「刷新」检测）")
        self.ip_label.setStyleSheet("color: #7aa2f7;")
        self.ip_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ip_row = QHBoxLayout()
        ip_row.addWidget(self.ip_label, 1)
        refresh_ip_btn = QPushButton("刷新 IP")
        refresh_ip_btn.clicked.connect(self._refresh_ip)
        ip_row.addWidget(refresh_ip_btn)
        form.addRow("局域网地址：", ip_row)

        # 配对码（可编辑/重置）
        pair_row = QHBoxLayout()
        self.pair_edit = QLineEdit()
        self.pair_edit.setMaxLength(6)
        self.pair_edit.setPlaceholderText("6 位数字，留空则启动时自动生成")
        pair_row.addWidget(self.pair_edit, 1)
        gen_btn = QPushButton("生成新码")
        gen_btn.clicked.connect(self._gen_pair_code)
        pair_row.addWidget(gen_btn)
        form.addRow("配对码：", pair_row)

        # 配对码生成时间（只读）
        self.pair_updated_label = QLabel("—")
        self.pair_updated_label.setStyleSheet("color: #565f89;")
        form.addRow("配对码更新于：", self.pair_updated_label)

        layout.addLayout(form)

        # 二维码与地址展示区
        qr_area = QHBoxLayout()
        self.qr_label = QLabel("（点击「刷新」生成二维码）")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(220, 220)
        self.qr_label.setStyleSheet(
            "border: 1px solid #2A2E45; background: #1a1b26; padding: 8px;"
        )
        self.qr_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        qr_area.addWidget(self.qr_label, 1)
        qr_text = QVBoxLayout()
        self.url_label = QLabel("（未启用）")
        self.url_label.setStyleSheet("color: #7aa2f7; font-size: 12px;")
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setWordWrap(True)
        qr_text.addWidget(self.url_label)
        gen_qr_btn = QPushButton("生成/刷新二维码")
        gen_qr_btn.clicked.connect(self._refresh_qr)
        qr_text.addWidget(gen_qr_btn)
        qr_text.addStretch()
        qr_area.addLayout(qr_text, 1)
        layout.addLayout(qr_area)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #2A2E45;")
        layout.addWidget(line)

        # 提示文案
        hint = QLabel(_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #565f89; font-size: 11px;")
        layout.addWidget(hint)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    def _load(self):
        """从 AppConfig 加载远程服务相关字段到表单。"""
        cfg = self.storage.load_app_config()
        self.enabled_chk.setChecked(cfg.remote_enabled)
        self.port_spin.setValue(cfg.remote_port or 8000)
        self.pair_edit.setText(cfg.remote_pair_code or "")
        if cfg.remote_pair_code_updated_at:
            self.pair_updated_label.setText(str(cfg.remote_pair_code_updated_at))
        else:
            self.pair_updated_label.setText("—")
        self._refresh_ip()
        self._refresh_qr()

    def _refresh_ip(self):
        """检测本机局域网 IP 并展示地址。"""
        try:
            from src.remote.server import get_local_ip
            ip = get_local_ip()
        except Exception:
            ip = "127.0.0.1"
        port = self.port_spin.value()
        self._cur_ip = ip
        self._cur_port = port
        url = f"http://{ip}:{port}/m/?code={self.pair_edit.text().strip()}"
        self.ip_label.setText(f"{url}  （手机在同一 WiFi 下访问）")
        self.url_label.setText(url)

    def _gen_pair_code(self):
        """生成新 6 位配对码填入输入框（不立即保存，点「保存」时落盘）。"""
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        self.pair_edit.setText(code)
        self._refresh_qr()

    def _refresh_qr(self):
        """根据当前 IP/端口/配对码刷新二维码图片。"""
        if not self.enabled_chk.isChecked():
            self.qr_label.setText("（未启用手机端服务）")
            self.qr_label.setStyleSheet(
                "border: 1px solid #2A2E45; background: #1a1b26; padding: 8px; color: #565f89;"
            )
            return
        ip = getattr(self, "_cur_ip", "127.0.0.1")
        port = self.port_spin.value()
        code = self.pair_edit.text().strip()
        url = f"http://{ip}:{port}/m/"
        if code:
            url += f"?code={code}"
        if not _HAS_QRCODE:
            self.qr_label.setText("（缺 qrcode 库，请 pip install qrcode[pil]）")
            return
        try:
            qr = qrcode.QRCode(version=1, box_size=6, border=2,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            # 转 QPixmap 显示（白色背景 + 黑色码点）
            w, h = img.size
            data = img.tobytes("raw", "RGB")
            qimg = QImage(data, w, h, 3 * w, QImage.Format_RGB888)
            pm = QPixmap.fromImage(qimg).scaled(
                200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.qr_label.setText("")
            self.qr_label.setPixmap(pm)
            self.qr_label.setStyleSheet(
                "border: 1px solid #2A2E45; background: #ffffff; padding: 8px;"
            )
        except Exception as e:
            self.qr_label.setText(f"（二维码生成失败：{e}）")

    # ------------------------------------------------------------------
    def _refresh_status(self):
        """从 server.get_status() 读运行态，刷新顶部状态标签 + 按钮启用态。"""
        try:
            from src.remote.server import get_status
            st = get_status()
        except Exception:
            st = {"state": "stopped", "running": False, "port": 0, "error": ""}
        state = st.get("state", "stopped")
        port = st.get("port", 0)
        err = st.get("error", "")
        # 状态文案 + 颜色（与 PC 端深色主题色调对齐：绿=#9ece6a 黄=#e0af68 红=#f7768e 灰=#565f89）
        if state == "running":
            txt = f"● 运行中（端口 {port}）"
            col = "#9ece6a"
        elif state == "starting":
            txt = "○ 启动中…"
            col = "#e0af68"
        elif state == "stopping":
            txt = "○ 停止中…"
            col = "#e0af68"
        elif state == "error":
            txt = f"✕ 启动失败：{err}" if err else "✕ 启动失败"
            col = "#f7768e"
        else:  # stopped
            txt = "○ 已停止"
            col = "#565f89"
        self.status_label.setText(txt)
        self.status_label.setStyleSheet(f"font-weight: bold; color: {col};")
        # 按钮启用态：stopped/error 可启动；running/starting 可停止；running 可重启
        self.start_btn.setEnabled(state in ("stopped", "error"))
        self.stop_btn.setEnabled(state in ("running", "starting"))
        self.restart_btn.setEnabled(state == "running")

    def _start_op_worker(self, op: str, save_action: str = "none"):
        """启动一个 _RemoteOpWorker 跑启停操作（防主线程同步 join 卡 UI）。

        [!] 同一时刻只允许一个 worker：若已有 worker 在跑，忽略本次（按钮启用态已据
            状态禁用，这里是兜底防竞态）。
        """
        if any(w.isRunning() for w in self._active_workers):
            return
        # 操作期间禁用三按钮防重复点击，状态标签显示「操作中…」
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.restart_btn.setEnabled(False)
        self.status_label.setText("○ 操作中…")
        self.status_label.setStyleSheet("font-weight: bold; color: #e0af68;")
        # 保存按钮也禁用防重复（_on_save 路径）
        save_btn = self.findChild(QPushButton, "primaryBtn")
        if save_btn:
            save_btn.setEnabled(False)
        # [!] worker parent=None 自管，由 _active_workers 持 Python 引用防 GC（见类 docstring）
        w = _RemoteOpWorker(
            op, services=self.services, storage=self.storage,
            save_action=save_action,
        )
        w.op_done.connect(self._on_op_done)
        # finished -> 清 registry 引用 + deleteLater；并恢复按钮态
        w.finished.connect(lambda: self._cleanup_worker(w))
        self._active_workers.add(w)
        w.start()

    def _cleanup_worker(self, worker):
        self._active_workers.discard(worker)
        worker.deleteLater()
        # 恢复保存按钮（操作期间被禁用）
        save_btn = self.findChild(QPushButton, "primaryBtn")
        if save_btn:
            save_btn.setEnabled(True)
        # 仅当无其它活跃 worker 时才刷状态恢复按钮态
        if not any(w.isRunning() for w in self._active_workers):
            self._refresh_status()

    def _on_op_done(self, op: str, success: bool, message: str):
        """worker 完成回调：刷新状态 + 弹反馈 + 启动/重启/保存成功后重读磁盘刷新表单。

        [!] save_apply 成功路径额外 accept 关闭对话框（保存流程终结）。
        [!] 对话框已隐藏（用户提前关闭）则不弹 modal 防延迟弹窗突袭；save_apply 也不再
            accept（对话框已 finished，二次 accept 无意义且会触发二次 finished 信号）。
        """
        if not self.isVisible():
            # 对话框已关闭：worker 仍在后台跑完，不弹 modal 防突袭。状态由 _cleanup_worker
            # 或下次打开对话框的 _refresh_status 刷新。
            return
        if success:
            # 启动/重启/保存的 start/restart 可能触发 server.py 自动生成配对码，重读磁盘
            # 刷新表单的 pair_edit + 二维码，让用户立即看到新配对码。
            if op in ("start", "restart", "save_apply"):
                try:
                    self._load()
                except Exception:
                    pass  # _load 抛错不阻断 accept 流程
            self._refresh_status()
            QMessageBox.information(self, "操作完成", message)
            if op == "save_apply":
                self.accept()
        else:
            self._refresh_status()
            QMessageBox.warning(self, "操作失败", message)
            # save_apply 失败保留对话框打开让用户看状态区错误（不 accept）

    def _on_start(self):
        """「启动」按钮：异步启动服务（用磁盘配置）。"""
        self._start_op_worker("start")

    def _on_stop(self):
        """「停止」按钮：异步停止服务。"""
        self._start_op_worker("stop")

    def _on_restart(self):
        """「重启」按钮：异步重启服务（应用端口/配对码变更）。"""
        self._start_op_worker("restart")

    # ------------------------------------------------------------------
    def _on_save(self):
        """保存到 data/app_config.json 并按差异异步即时启停服务（无需重启 exe）。

        [!] 遵循 AppConfig 单例写入规范：先 load_app_config 读完整配置，
            只改 remote_* 字段，保留 jailbreak/render_mode 等其它字段。
        [!] 启停策略（按 old vs new 差异）：
            - 关闭开关 + 运行中 -> 停止
            - 开启开关 + 未运行 -> 启动
            - 开启开关 + 运行中 + (端口或配对码变更) -> 重启（端口 bind / 配对码注入
              app.state 均在 _build_app 时完成，运行中改这两项需重启才生效）
            - 开启开关 + 运行中 + 无变更 -> 无操作
        [!] 启停走 _RemoteOpWorker 异步（防 stop_server 同步 join 5s 卡 UI）；启停失败
            时 worker 通过 _on_op_done 弹窗 + 保留对话框打开（不 accept），让用户看状态。
        [!] import src.remote.server 放 try 内：fastapi/uvicorn 缺失时也能友好弹窗而非
            抛未捕获异常（配置已落盘，下次重开仍可见）。
        """
        old_cfg = self.storage.load_app_config()
        old_enabled = bool(old_cfg.remote_enabled)
        old_port = int(old_cfg.remote_port or 8000)
        old_code = str(old_cfg.remote_pair_code or "")

        cfg = old_cfg
        cfg.remote_enabled = self.enabled_chk.isChecked()
        port = int(self.port_spin.value())
        if port < 1 or port > 65535:
            port = 8000
        cfg.remote_port = port
        # 配对码：仅允许 1-6 位数字（或留空启动时自动生成）；非法字符过滤
        code_raw = self.pair_edit.text().strip()
        if code_raw and not code_raw.isdigit():
            QMessageBox.warning(self, "配对码格式错误", "配对码需为 6 位数字，或留空自动生成。")
            return
        code_changed = code_raw != old_code
        cfg.remote_pair_code = code_raw
        if code_changed:
            cfg.remote_pair_code_updated_at = datetime.now().isoformat()
        self.storage.save_app_config(cfg)

        # 计算启停动作（is_running 是同步轻量读模块状态，不阻塞 UI）
        new_enabled = cfg.remote_enabled
        port_changed = (port != old_port)
        try:
            from src.remote.server import is_running
            running = is_running()
        except Exception as e:
            # import 失败（fastapi/uvicorn 缺失）：配置已落盘，弹友好提示
            QMessageBox.warning(
                self, "启停失败",
                f"设置已保存，但远程模块不可用：\n{e}\n\n请在对话框顶部查看运行状态。"
            )
            self._refresh_status()
            return

        if not new_enabled:
            save_action = "stop" if running else "none"
        else:
            if not running:
                save_action = "start"
            elif port_changed or code_changed:
                save_action = "restart"
            else:
                save_action = "none"
        # 异步启停（worker 完成后 _on_op_done 弹反馈 + 成功则 accept）
        self._start_op_worker("save_apply", save_action=save_action)