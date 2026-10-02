"""远程服务（手机端联动）核心服务端。

PC 端在后台 daemon 线程跑 fastapi/uvicorn，监听 0.0.0.0:port；
手机浏览器访问 PC 局域网 IP:端口，经配对码认证后查看会话/聊天。

[!] 零 PySide6 依赖：只 import fastapi/uvicorn/标准库/项目 service 层。
[!] 复用 orchestrator 单例：通过 app.state 注入，绝不新建一套服务。
[!] 流式跟随 api.streaming：编排器已内置 if api.streaming 分支
    （chat_orchestrator.py:228-271），服务端 on_chunk 推队列，SSE 推浏览器。
[!] 线程模型：uvicorn 跑 daemon 线程；fastapi 路由 async def，编排器同步
    调用丢 asyncio 线程池（run_in_executor），回调直接 queue.put（worker 线程）；
    SSE async generator 用 loop.run_in_executor 桥接 queue.get 到事件循环。
[!] 取消生成：每个进行中的请求关联 threading.Event，cancel_check=lambda: ev.is_set()，
    POST /m/api/sessions/{id}/stop 把对应 Event 置 True，编排器透传中断 LLM 拉取。
"""
from __future__ import annotations
import os
import json
import asyncio
import threading
import queue
import secrets
import socket
from datetime import datetime
from typing import Optional

# ---- ASGI 栈（PyInstaller 子模块已列入 hiddenimports）----
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import (
    StreamingResponse, FileResponse, HTMLResponse,
    JSONResponse, RedirectResponse, Response,
)
from starlette.middleware.base import BaseHTTPMiddleware

import uvicorn

# ---- 项目 service 层（零 PySide6 依赖）----
from src.app import get_resource_path
from src.config import paths
from src.services.chat_orchestrator import ChatCallbacks
from src.utils import markup


# ============================================================================
# 渲染辅助：复用 PC 端 markup 模块产 HTML 直返手机端（避免 JS 复刻规则双份维护）
# ============================================================================
def _render_msg_html(content: str, is_user: bool) -> str:
    """用 PC 端 markup.render 把消息渲染成 HTML 片段返回手机端。

    [!] 仅 markup 模式产出可用 HTML（带 <span style=color> 的配色片段）；
        markdown/auto 模式 markup.render 只着色不结构化，仍可作「着色版纯文本」用
        （结构由手机端 render.js 用 marked.js 兜底，或直接 innerHTML 注入也只丢结构）。
    [!] markup.render 是纯函数（无 Qt 依赖），线程安全可直接在 worker 线程调用。
    """
    if not content:
        return ""
    try:
        return markup.render(content, is_user)
    except Exception:
        # 渲染异常退回 HTML 转义纯文本，绝不抛错导致整个请求 500
        import html as _html
        return _html.escape(content, quote=False).replace("\n", "<br>")


# ============================================================================
# 配对码 / Cookie 常量
# ============================================================================
PAIR_COOKIE_NAME = "dr_remote_pair"
PAIR_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 天


# ============================================================================
# 模块级单例：uvicorn Server + 进行中请求的取消事件表
# [!] _server/_thread/_last_error/_stopping/_cur_port 同由 _server_lock 保护，
#     即时启停（RemoteServiceDialog 启动/停止/重启按钮 + main.py 开机自启）读写这些状态。
# ============================================================================
_server: Optional[uvicorn.Server] = None
_thread: Optional[threading.Thread] = None
_last_error: Optional[str] = None        # daemon 线程内 server.run() 抛异常时填
_stopping: bool = False                  # 主动停止中（区分正常退出 vs 异常退出）
_cur_port: int = 0                       # 最近一次启动端口（UI 状态展示用）
_server_lock = threading.Lock()

# key: session_id, value: threading.Event；停止生成时 set()，编排器透传中断
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def is_running() -> bool:
    """远程服务是否正在运行。"""
    with _server_lock:
        return _server is not None and _server.started


def get_status() -> dict:
    """远程服务当前状态（供 UI 即时启停按钮 + 状态栏轮询）。

    返回 {state, running, port, error}：
      state: "stopped"|"starting"|"running"|"stopping"|"error"
      running: 等价 state=="running"
      port: 最近一次启动端口（0 表示从未启动）
      error: 启动失败的错误信息（state=="error" 时非空）
    """
    with _server_lock:
        if _stopping:
            return {"state": "stopping", "running": False, "port": _cur_port, "error": ""}
        if _server is None:
            if _last_error:
                return {"state": "error", "running": False, "port": _cur_port, "error": _last_error}
            return {"state": "stopped", "running": False, "port": _cur_port, "error": ""}
        started = bool(getattr(_server, "started", False))
        if started:
            return {"state": "running", "running": True, "port": _cur_port, "error": ""}
        if _last_error:
            return {"state": "error", "running": False, "port": _cur_port, "error": _last_error}
        return {"state": "starting", "running": False, "port": _cur_port, "error": ""}


def _check_port_free(port: int) -> None:
    """预检端口是否可绑定（丢线程前同步检查，占用则 raise RuntimeError）。

    [!] bind 成功立即 close 再交 uvicorn 接管，存在 TOCTOU 概率（close 后被别的进程
        抢占），但 daemon 线程内 server.run() 仍有 _run_server_wrapper try 兜底回收
        错误到 _last_error。预检主要意义是让调用方（main.py/UI）try 能即时捕获端口
        占用，而非等到 daemon 线程内异步失败（那时 start_server 早已 return True）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        raise RuntimeError(f"端口 {port} 被占用或无法绑定: {e}")
    finally:
        s.close()


def _run_server_wrapper(server: "uvicorn.Server") -> None:
    """daemon 线程入口：跑 server.run()，异常/意外退出回收到 _last_error + 清 _server。

    [!] 必须 `except BaseException`：uvicorn 启动失败（端口 bind 失败 / lifespan 错误）
        走 `sys.exit(STARTUP_FAILURE)` 抛 SystemExit（BaseException 子类，不被 except
        Exception 捕获）。若不捕，daemon 线程带未捕获 SystemExit 退出，_server 不清、
        _last_error 不写 -> get_status 永久卡 "starting"，UI 看不到失败。这是本次「异步
        失败可被 UI 感知」承诺的关键兜底。
    [!] 「是否 stop 触发」必须在 cleanup 当下读 _stopping，而非函数开头预读：stop_server
        是在 server.run() 进行中才设 _stopping=True（用户点停止时 server 已在跑），函数
        开头读必然是 False（start_server 刚把 _stopping=False）。预读会导致正常 stop 被
        误判为「意外退出」写 _last_error。
    [!] daemon 退出时无条件清 _server/_thread/_stopping：解决 stop_server join 超时分支
        不清状态的死锁——daemon 真正退出（哪怕 join 已超时）也会把状态从「停止中」拉回
        「已停止」，UI 自动恢复可点击。
    [!] stop 触发的 SystemExit 是 uvicorn 关闭流程的正常产物（user exit / signal），
        不算错误不写 _last_error；非 stop 触发的退出（lifespan 错误/意外）才记 error。
    """
    global _server, _thread, _last_error, _stopping
    try:
        server.run()
    except BaseException as e:
        # SystemExit / Exception 一并回收（见 docstring）
        with _server_lock:
            # cleanup 当下读 _stopping（而非预读），正确区分 stop 触发 vs 意外退出
            was_stopping = _stopping
            if _server is server:
                _server = None
            if _thread is threading.current_thread():
                _thread = None
            _stopping = False  # daemon 已退，复位让 UI 恢复可点击
            # 非 stop 触发的异常才记 error（stop 触发的 SystemExit 是正常关闭产物）
            if not was_stopping:
                _last_error = f"{type(e).__name__}: {e}"
        return
    # 正常返回（无异常）
    with _server_lock:
        was_stopping = _stopping
        if _server is server:
            _server = None
        if _thread is threading.current_thread():
            _thread = None
        _stopping = False
        if not was_stopping:
            # 非用户主动停止却正常退出 = 意外退出（如 lifespan 主动 shutdown）
            _last_error = "服务意外退出（未触发停止但 server.run 已返回）"


def get_local_ip() -> str:
    """获取本机局域网 IPv4（用于手机端扫码/输地址）。

    多网卡时筛非回环 IPv4；拿不到回退 127.0.0.1。
    """
    try:
        # 用 UDP connect 探测实际出口 IP（不真发包，仅让内核选路由）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    # 回退：gethostbyname
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


# ============================================================================
# 配对码校验
# ============================================================================
def _pair_code_from_state(request: Request) -> str:
    """从 app.state 取当前配对码（运行时不变，启动时从 AppConfig 注入）。"""
    return getattr(request.app.state, "pair_code", "") or ""


def _check_pair_cookie(request: Request) -> bool:
    """请求是否已通过配对码认证（Cookie 命中当前配对码）。"""
    code = _pair_code_from_state(request)
    if not code:
        return False
    return request.cookies.get(PAIR_COOKIE_NAME) == code


class PairAuthMiddleware(BaseHTTPMiddleware):
    """配对码认证中间件：未认证的 /m/api/* 重定向到登录页。

    - /m/ 与 /m （页面）、/m/login、/m/api/login、静态资源放行
    - /m/api/* 未通过认证返回 401（fetch 端据此跳登录页）
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 放行：页面、登录页、登录接口、静态资源、favicon
        if (
            path in ("/", "/m", "/m/", "/m/login", "/favicon.ico")
            or path == "/m/api/login"
            or path.startswith("/m/static/")
        ):
            return await call_next(request)
        # /m/api/* 需认证
        if path.startswith("/m/api/"):
            if not _check_pair_cookie(request):
                return JSONResponse(
                    {"detail": "未认证，请先输入配对码"}, status_code=401
                )
        return await call_next(request)


# ============================================================================
# FastAPI app 工厂
# ============================================================================
def _build_app(services: dict, app_config) -> FastAPI:
    """构造 fastapi app 并注入 service 单例到 app.state。

    services: init_services() 返回的 dict（含 storage/orchestrator 等）。
    app_config: AppConfig 实例（含 remote_pair_code，启动时已确保非空）。
    """
    app = FastAPI(title="DreamRole Remote", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    app.state.storage = services["storage"]
    app.state.orchestrator = services["orchestrator"]
    app.state.pair_code = app_config.remote_pair_code
    app.add_middleware(PairAuthMiddleware)

    _register_routes(app)
    return app


def _sse_event(payload: dict) -> bytes:
    """生成一个 SSE 帧：data: <json>\\n\\n。"""
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _stream_from_queue(q, fut, loop, session_id, cancel_event):
    """共享 SSE 事件分发生成器：从 q 读事件推 SSE 帧，retry_start 事件带
    deleted_ids 让前端先删旧气泡。fut 结束确保 await 防 async 任务悬挂。

    [!] 客户端断连（手机端 abort/返回/关页面）会被 starlette 取消 async generator，
        抛 CancelledError（BaseException 子类，不被 except Exception 捕获）。finally 里
        主动 set cancel_event 让 worker 线程中断（用户已不看就别浪费 token），再清理 dict
        防残留。覆盖正常 done / 异常 / 断连三种退出路径。
    [!] cancel_event 必须按引用传（非按 session_id 取）做身份校验后才 pop：若同会话在
        本流断连后已发起新请求 B，_cancel_events[session_id] 已被 evB 覆盖，此时按 session_id
        pop 会误删 evB 并 set() 误中断新流。身份校验（is ev）只清属于自己的那条。
        （read.md:239 跨会话清理 TODO 的局部修补——彻底方案是用 request_id 而非 session_id。）
    """
    async def gen():
        try:
            while True:
                try:
                    item = await loop.run_in_executor(None, q.get, True, 1.0)
                except queue.Empty:
                    if fut.done():
                        break
                    continue
                kind, data = item
                if kind == "chunk":
                    yield _sse_event({"type": "chunk", **data})
                elif kind == "usage":
                    yield _sse_event({"type": "usage", **data})
                elif kind == "message":
                    # 图片消息补 image_url（与 chat 路径一致，regen 期间也可能出图）
                    if data.get("image_path") and data.get("is_image_only"):
                        import base64
                        data["image_url"] = "/m/api/images/" + base64.b64encode(
                            data["image_path"].encode("utf-8")
                        ).decode("ascii")
                    yield _sse_event({"type": "message", "msg": data})
                elif kind == "image":
                    yield _sse_event({"type": "image", **data})
                elif kind == "speaker":
                    yield _sse_event({"type": "speaker", **data})
                elif kind == "summary":
                    yield _sse_event({"type": "summary", "msg": data})
                elif kind == "status":
                    yield _sse_event({"type": "status", "text": data})
                elif kind == "error":
                    yield _sse_event({"type": "error", "message": data})
                elif kind == "retry_start":
                    # retry 启动：通知前端立即删除一批旧气泡
                    yield _sse_event({"type": "retry_start", **data})
                elif kind == "done":
                    yield _sse_event({"type": "done"})
                    break
            try:
                await fut
            except Exception:
                pass
        finally:
            # [!] 身份校验后 pop：仅当 _cancel_events[session_id] 仍是本流自己的 event
            #    才删，避免同会话新请求覆盖后误删新 event（见 docstring 竞态说明）。
            with _cancel_lock:
                cur = _cancel_events.get(session_id)
                if cur is cancel_event:
                    _cancel_events.pop(session_id, None)
            # 客户端断连时主动 set 让 worker 线程中断；正常 done 时 worker 已退、set 无害。
            if not cancel_event.is_set():
                cancel_event.set()
    return gen()


def _sse_response(event_stream_gen):
    return StreamingResponse(
        event_stream_gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _register_routes(app: FastAPI) -> None:
    """注册全部路由。路由内复用 app.state.storage / orchestrator。"""

    storage = app.state.storage
    orchestrator = app.state.orchestrator

    # ------------------------------------------------------------------
    # 图片消息角色归属：取「会话内最近一条 assistant」的 character_id/name（与 PC 端
    # main_window._on_image 语义一致）。PC 端反向遍历自己的 current_messages（其中本轮
    # assistant 已被 _on_message_saved 追加）；服务端编排器传进来的 messages 列表对
    # assistant 只 save 不 append（generate_response:310 未 _append_message），故此处必须
    # 从 DB 拉，否则会命中上一轮 assistant 致群聊图片角色错置、且已脏数据落库。
    # ------------------------------------------------------------------
    def _pick_image_speaker(session_id: str) -> tuple[str, str]:
        try:
            db_msgs = storage.load_messages(session_id)
        except Exception:
            return "", ""
        for m in reversed(db_msgs):
            if m.role == "assistant" and m.character_id:
                return m.character_id, m.character_name
        return "", ""

    # ------------------------------------------------------------------
    # 静态页面：手机端单页应用
    # ------------------------------------------------------------------
    def _web_dir() -> str:
        # 开发模式:项目根/src/remote/web；打包后:_MEIPASS/src/remote/web
        return get_resource_path(os.path.join("src", "remote", "web"))

    @app.get("/", include_in_schema=False)
    async def root_index(request: Request):
        # 根路径重定向到 /m/，便于直接输 IP:端口
        return RedirectResponse(url="/m/")

    @app.get("/m", include_in_schema=False)
    @app.get("/m/", include_in_schema=False)
    async def mobile_index():
        idx = os.path.join(_web_dir(), "index.html")
        if not os.path.exists(idx):
            return HTMLResponse("<h1>DreamRole 手机端</h1><p>静态资源缺失</p>", status_code=500)
        with open(idx, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    @app.get("/m/login", include_in_schema=False)
    async def mobile_login(code: Optional[str] = Query(None)):
        # 扫码场景 URL 带 ?code=xxxxxx，重定向到 /m/ 并把码写入前端可读位置
        idx = os.path.join(_web_dir(), "index.html")
        if not os.path.exists(idx):
            return HTMLResponse("<h1>DreamRole 手机端</h1><p>静态资源缺失</p>", status_code=500)
        with open(idx, "r", encoding="utf-8") as f:
            html = f.read()
        if code:
            # 前端 JS 从 meta 标签或 query 读配对码自动提交登录
            html = html.replace("</head>", f'<meta name="dr-pair-code" content="{code}"></head>')
        return HTMLResponse(html)

    @app.get("/m/static/{name}", include_in_schema=False)
    async def static_asset(name: str):
        # 受限静态资源：仅允许 web 目录下的已知文件名（防路径遍历）
        allowed = {"app.js", "style.css", "render.js", "marked.min.js", "favicon.ico"}
        if name not in allowed:
            raise HTTPException(status_code=404)
        p = os.path.join(_web_dir(), name)
        if not os.path.exists(p):
            raise HTTPException(status_code=404)
        return FileResponse(p)

    # ------------------------------------------------------------------
    # 登录接口
    # ------------------------------------------------------------------
    @app.post("/m/api/login")
    async def api_login(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        code = str(body.get("code", "") or "").strip()
        if not code:
            return JSONResponse({"ok": False, "detail": "请输入配对码"}, status_code=400)
        expected = _pair_code_from_state(request)
        if not expected or not secrets.compare_digest(code, expected):
            return JSONResponse({"ok": False, "detail": "配对码错误"}, status_code=403)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key=PAIR_COOKIE_NAME,
            value=code,
            max_age=PAIR_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp

    @app.get("/m/api/ping")
    async def api_ping(request: Request):
        # 已认证则返回 ok（前端用来校验会话是否仍有效 + 已登录）
        return {"ok": True, "authed": _check_pair_cookie(request)}

    # ------------------------------------------------------------------
    # 渲染规则：手机端初始化时拉一次，缓存用于判断模式 + markdown 兜底着色复用
    # ------------------------------------------------------------------
    @app.get("/m/api/render_rules")
    async def api_render_rules(request: Request):
        cfg = markup.get_rules_config()
        mode = markup.get_render_mode()
        # 规则只回必要字段（不对手机端暴露 id 等内部信息），含 scope 供前端按 is_user 过滤
        rules = []
        for r in cfg.rules:
            rules.append({
                "name": r.name,
                "pattern": r.pattern,
                "color": r.color,
                "italic": r.italic,
                "enabled": bool(r.enabled),
                "priority": int(r.priority),
                "scope": r.scope,
                "keep_marks": bool(r.keep_marks),
            })
        return {
            "rules": rules,
            "ai_default_color": cfg.ai_default_color,
            "user_default_color": cfg.user_default_color,
            "render_mode": mode,
        }

    @app.get("/m/api/render_rules/version")
    async def api_render_rules_version(request: Request):
        # 轻量版本号端点：手机端轮询判断规则/模式是否变更（避免每次拉全量 rules 比对）。
        # 版本号在 markup.set_rules_config/set_render_mode/reload_rules 时自增。
        return {
            "version": markup.get_render_rules_version(),
            "mode": markup.get_render_mode(),
        }

    # ------------------------------------------------------------------
    # 会话列表
    # ------------------------------------------------------------------
    @app.get("/m/api/sessions")
    async def api_sessions():
        sessions = storage.load_all_sessions()
        # 按 updated_at 倒序（最新在最上面），与 PC 端 UI 风格一致
        sessions = sorted(
            sessions,
            key=lambda s: getattr(s, "updated_at", "") or getattr(s, "created_at", ""),
            reverse=True,
        )
        items = []
        for s in sessions:
            # 最后一条消息预览（assistant 优先，无则任意）
            msgs = storage.load_messages(s.id)
            last_text = ""
            last_role = ""
            if msgs:
                lm = msgs[-1]
                last_role = lm.role
                last_text = (lm.content or "").strip()
                if len(last_text) > 60:
                    last_text = last_text[:60] + "..."
            # 角色摘要
            characters = []
            for cid in s.character_ids:
                c = storage.load_character(cid)
                if c:
                    characters.append({
                        "id": c.id,
                        "name": c.name,
                        "avatar": c.avatar or "",
                    })
            items.append({
                "id": s.id,
                "title": s.title or (characters[0]["name"] if characters else "未命名会话"),
                "session_type": s.session_type,
                "group_mode": s.group_mode,
                "player_name": s.player_name,
                "characters": characters,
                "default_speaker_id": s.default_speaker_id,
                "streaming": s.streaming,
                "updated_at": s.updated_at,
                "last_text": last_text,
                "last_role": last_role,
            })
        return {"sessions": items}

    # ------------------------------------------------------------------
    # 会话详情 + 历史消息
    # ------------------------------------------------------------------
    @app.get("/m/api/sessions/{session_id}")
    async def api_session_detail(session_id: str):
        s = storage.load_session(session_id)
        if not s:
            raise HTTPException(status_code=404, detail="会话不存在")
        msgs = storage.load_messages(session_id)
        # 折叠消息聚合成「折叠块占位」推给前端（与 PC 端 CollapsedBlock 视觉等价），
        # 让手机端用户知道有 N 条早期消息被总结/手动折叠（不再凭空消失一大段）。
        # [!] 折叠占位内不含被折叠消息原文（手机端不做展开懒渲染，与 PC 端「可点开看原文」
        #     有差异但可接受——手机端窄屏 + 跨网络懒渲染原文成本/价值不划算）。
        # [!] 统计折叠块索引数量只统计非图片非 summary 的 user/assistant 消息（与 PC 端
        #     CollapsedBlock._ensure_built 计数口径一致）。
        visible = []
        collapsed_run = []  # 当前折叠段累计的对象
        def _flush_collapsed():
            if not collapsed_run:
                return
            count = sum(
                1 for m in collapsed_run
                if not m.is_image_only and m.role not in ("summary",)
            )
            if count > 0:
                # 折叠块的 reason 取第一个非空 collapsed_reason
                reason = ""
                for m in collapsed_run:
                    if m.collapsed_reason:
                        reason = m.collapsed_reason
                        break
                visible.append({
                    "id": f"collapsed_{len(visible)}",
                    "role": "collapsed_block",
                    "collapsed_count": count,
                    "collapsed_reason": reason,
                    "content": "",
                    "rendered_html": "",
                    "image_path": "", "image_url": "",
                    "is_image_only": False, "is_summary": False, "is_stopped": False,
                    "character_id": "", "character_name": "",
                })
            collapsed_run.clear()

        for m in msgs:
            if m.collapsed:
                collapsed_run.append(m)
                continue
            # 进入非折叠消息前先把当前折叠段刷成占位
            _flush_collapsed()
            d = m.to_dict()
            # 渲染 HTML 片段（markup 模式给完整配色 HTML；markdown/auto 模式留空让前端
            # 用 marked.js 兜底结构化再二次着色）。图片消息不走着色，前端直接 img 渲染。
            # [!] summary 消息也走着色（PC 端 summary 气泡正文是富文本，与普通消息一样
            #     走 _build_summary 的富文本渲染；手机端复用 DRRender 走着色）。
            if m.content and not m.is_image_only:
                is_user = (m.role == "user")
                d["rendered_html"] = _render_msg_html(m.content, is_user)
            else:
                d["rendered_html"] = ""
            # 图片消息：image_path 是绝对路径，转成 API URL 给前端加载
            if m.is_image and m.image_path:
                # 用 base64 编码绝对路径避免 URL 特殊字符问题
                import base64
                d["image_url"] = "/m/api/images/" + base64.b64encode(
                    m.image_path.encode("utf-8")
                ).decode("ascii")
            visible.append(d)
        # 末尾折叠段（理论上不出现，保险刷一次）
        _flush_collapsed()
        characters = []
        for cid in s.character_ids:
            c = storage.load_character(cid)
            if c:
                characters.append({
                    "id": c.id,
                    "name": c.name,
                    "avatar": c.avatar or "",
                    "description": (c.description or "")[:200],
                })
        user_info = None
        if s.user_id:
            u = storage.load_user(s.user_id)
            if u:
                user_info = {"id": u.id, "name": u.name, "avatar": u.avatar or ""}
        return {
            "session": {
                "id": s.id,
                "title": s.title or (characters[0]["name"] if characters else "未命名会话"),
                "session_type": s.session_type,
                "group_mode": s.group_mode,
                "player_name": s.player_name,
                "characters": characters,
                "default_speaker_id": s.default_speaker_id,
                "streaming": s.streaming,
                "user": user_info,
            },
            "messages": visible,
        }

    # ------------------------------------------------------------------
    # 聊天（SSE，流式/非流式跟随 api.streaming）
    # ------------------------------------------------------------------
    # _sse_event 已提到模块级（_stream_from_queue 共享）

    def _run_chat(
        session, messages, content, character, mode, cancel_event: threading.Event,
        q: "queue.Queue",
    ) -> None:
        """在 worker 线程跑编排器，回调把事件推队列；返回前推 ('done', None) 收尾。"""
        # 流式累积完整文本：on_chunk 推增量，但服务端累积全量后调 markup.render
        # 产 rendered_html 一并推给前端，前端直接 innerHTML 替换（与 PC 端流式整段重渲一致）。
        accumulated = {"text": ""}
        try:
            def on_chunk(text):
                accumulated["text"] += text
                html = _render_msg_html(accumulated["text"], is_user=False)
                q.put(("chunk", {"text": text, "rendered_html": html}))
            def on_usage(api_id, usage):
                q.put(("usage", {
                    "api_id": api_id,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                }))
            def on_message(msg):
                # 同步维护 messages 列表：编排器只对 user 调 _append_message，对 assistant
                # 仅 save_message（不 append）。此处补 append 保证后续 on_image 取当前轮次
                # assistant（与 PC 端 _on_message_saved -> _append_once 对齐）。
                if not any(x.id == msg.id for x in messages):
                    messages.append(msg)
                d = msg.to_dict()
                # 落库的正式消息补 rendered_html（前台/继续/续写均会触发）
                if d.get("content") and not d.get("is_image_only"):
                    is_user = (d.get("role") == "user")
                    d["rendered_html"] = _render_msg_html(d["content"], is_user)
                else:
                    d["rendered_html"] = ""
                q.put(("message", d))
            def on_error(err):
                q.put(("error", str(err)))
            def on_speaker(char):
                if char:
                    q.put(("speaker", {"id": char.id, "name": char.name, "avatar": char.avatar or ""}))
            def on_image(path, prompt):
                # 复刻 PC 端 main_window._on_image：纯图片消息持久化（is_image_only=True
                # 让 context_builder 跳过上下文不入 API，但落库保证重开可见历史图片）。
                # 服务端必须独立存库（编排器只 emit on_image 不落库），否则手机聊出的图片
                # 下次打开 PC 端就消失了。
                import base64
                url = "/m/api/images/" + base64.b64encode(path.encode("utf-8")).decode("ascii")
                # [!] 取「会话内最近一条 assistant」的 character_id/name（与 PC 端反向遍历
                #    current_messages 一致）。此处用 _pick_image_speaker 自 DB 拉最新列表——
                #    不能遍历传入 messages：编排器对 assistant 只 save 不 _append_message
                #    (chat_orchestrator.py:310)，列表里没有本轮 assistant，会误命上一轮。
                char_id, char_name = _pick_image_speaker(session.id)
                from src.models import Message
                img_msg = Message(
                    role="assistant",
                    session_id=session.id,
                    character_id=char_id,
                    character_name=char_name,
                    content=prompt,
                    image_path=path,
                    is_image_only=True,
                )
                try:
                    storage.save_message(img_msg)
                    if not any(x.id == img_msg.id for x in messages):
                        messages.append(img_msg)
                except Exception:
                    pass
                # 推 image 事件给前端：带 msg 完整字段（含 image_url + rendered_html=""），
                # 前端据此作为独立纯图片气泡渲染（不只 appendChild 到流式占位气泡）。
                d = img_msg.to_dict()
                d["rendered_html"] = ""
                d["image_url"] = url
                q.put(("image", {"image_url": url, "prompt": prompt, "msg": d}))
            def on_summary(msg):
                # summary 也走 DRRender 着色（PC 端 summary 气泡正文是富文本含分色）
                d = msg.to_dict()
                if d.get("content"):
                    d["rendered_html"] = _render_msg_html(d["content"], is_user=False)
                else:
                    d["rendered_html"] = ""
                q.put(("summary", d))
            def on_status(text):
                q.put(("status", text))
            def on_done():
                pass  # 编排器末尾会调 on_done，但 run() finally 也会兜底推 done

            cb = ChatCallbacks(
                on_chunk=on_chunk, on_usage=on_usage, on_message=on_message,
                on_error=on_error, on_speaker=on_speaker, on_image=on_image,
                on_summary=on_summary, on_status=on_status,
                on_done=None, on_danbooru_manual_select=None,
            )
            cancel_check = cancel_event.is_set

            if mode == "send_and_respond":
                orchestrator.send_and_respond(
                    session, messages, content, cb, cancel_check=cancel_check
                )
            elif mode == "send_and_auto_respond":
                orchestrator.send_and_auto_respond(
                    session, messages, content, cb, cancel_check=cancel_check
                )
            elif mode == "trigger_character":
                if character is None:
                    q.put(("error", "群聊手动模式下未选择发言角色"))
                    return
                orchestrator.trigger_character(
                    session, messages, character, cb, cancel_check=cancel_check
                )
            else:
                q.put(("error", f"未知聊天模式: {mode}"))
        except Exception as e:
            q.put(("error", f"内部错误: {e}"))
        finally:
            q.put(("done", None))

    def _run_regen(
        session, messages, target_msg, cancel_event: threading.Event,
        q: "queue.Queue",
    ) -> None:
        """在 worker 线程跑编排器.regenerate_from：删除该 assistant 及其后所有消息
        （若前一条是本轮 user 也一并删），重新生成。流式走 chat_stream（or 非流式
        chat_cancelable）由编排器自动决定跟随 api.streaming，on_chunk 推队列。
        """
        # retry 期间独立累积流式文本（重生成时新 assistant 的累积内容）
        regen_accum = {"text": ""}
        # 先算要通知前端删除的气泡 ids：assistant_msg 及其后所有消息，
        # 以及（若前一条是 user）前一条 user 消息。与编排器删除范围一致，让前端立即
        # 从视图删除避免视觉残留旧内容。
        try:
            ids_to_drop = []
            idx = next(
                (i for i, m in enumerate(messages) if m.id == target_msg.id), None
            )
            if idx is not None:
                ids_to_drop = [m.id for m in messages[idx:]]
                if idx > 0 and messages[idx - 1].role == "user":
                    ids_to_drop.insert(0, messages[idx - 1].id)
            q.put(("retry_start", {"deleted_ids": ids_to_drop}))

            def _on_chunk(text):
                regen_accum["text"] += text
                html = _render_msg_html(regen_accum["text"], is_user=False)
                q.put(("chunk", {"text": text, "rendered_html": html}))
            def _on_usage(api_id, usage):
                q.put(("usage", {
                    "api_id": api_id,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                }))
            def _on_message(msg):
                # 同步维护 messages 列表（与 _run_chat.on_message 一致，见上）
                if not any(x.id == msg.id for x in messages):
                    messages.append(msg)
                d = msg.to_dict()
                if d.get("content") and not d.get("is_image_only"):
                    is_user = (d.get("role") == "user")
                    d["rendered_html"] = _render_msg_html(d["content"], is_user)
                else:
                    d["rendered_html"] = ""
                q.put(("message", d))
            def _on_error(err):
                q.put(("error", str(err)))
            def _on_speaker(char):
                if char:
                    q.put(("speaker", {"id": char.id, "name": char.name, "avatar": char.avatar or ""}))
            def _on_image(path, prompt):
                import base64
                url = "/m/api/images/" + base64.b64encode(path.encode("utf-8")).decode("ascii")
                # [!] 同 _run_chat.on_image：用 _pick_image_speaker 自 DB 取最近 assistant
                #    character_id/name（不能遍历 messages，编排器对 assistant 只 save 不 append）
                char_id, char_name = _pick_image_speaker(session.id)
                from src.models import Message
                img_msg = Message(
                    role="assistant", session_id=session.id,
                    character_id=char_id, character_name=char_name,
                    content=prompt, image_path=path, is_image_only=True,
                )
                try:
                    storage.save_message(img_msg)
                    if not any(x.id == img_msg.id for x in messages):
                        messages.append(img_msg)
                except Exception:
                    pass
                d = img_msg.to_dict(); d["rendered_html"] = ""; d["image_url"] = url
                q.put(("image", {"image_url": url, "prompt": prompt, "msg": d}))
            def _on_summary(msg):
                # summary 也走 DRRender 着色（与 _run_chat.on_summary 一致）
                d = msg.to_dict()
                if d.get("content"):
                    d["rendered_html"] = _render_msg_html(d["content"], is_user=False)
                else:
                    d["rendered_html"] = ""
                q.put(("summary", d))
            def _on_status(text):
                q.put(("status", text))

            cb = ChatCallbacks(
                on_chunk=_on_chunk, on_usage=_on_usage, on_message=_on_message,
                on_error=_on_error, on_speaker=_on_speaker, on_image=_on_image,
                on_summary=_on_summary, on_status=_on_status,
                on_done=None, on_danbooru_manual_select=None,
            )
            cancel_check = cancel_event.is_set
            orchestrator.regenerate_from(
                session, messages, target_msg, cb, cancel_check=cancel_check
            )
        except Exception as e:
            q.put(("error", f"内部错误: {e}"))
        finally:
            q.put(("done", None))

    @app.post("/m/api/sessions/{session_id}/chat")
    async def api_chat(session_id: str, request: Request):
        s = storage.load_session(session_id)
        if not s:
            raise HTTPException(status_code=404, detail="会话不存在")
        try:
            body = await request.json()
        except Exception:
            body = {}
        content = str(body.get("content", "") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="消息内容不能为空")
        # manual 群聊时手机端选择的角色 id（可选）
        character_id_req = str(body.get("character_id", "") or "").strip()

        # 选择编排器方法 + 角色
        if s.session_type == "group" and s.group_mode == "auto":
            mode = "send_and_auto_respond"
            character = None
        elif s.session_type == "group":  # manual
            mode = "trigger_character"
            cid = character_id_req or s.default_speaker_id or (s.character_ids[0] if s.character_ids else "")
            character = storage.load_character(cid) if cid else None
            # [!] 手机端选角落库（与 PC 端 _on_speaker_changed -> _remember_speaker 对齐，
            #    §8 契约：手动模式选择即记住最近发言者，下次进会话/PC 端仍记住）。
            #    character_id_req 非空才写（沿用 PC 端只在用户主动改选角时触发的语义）。
            if character_id_req and character and s.default_speaker_id != character.id:
                s.default_speaker_id = character.id
                s.touch()
                storage.save_session(s)
        else:  # single
            mode = "send_and_respond"
            character = None

        messages = storage.load_messages(session_id)
        cancel_event = threading.Event()
        with _cancel_lock:
            _cancel_events[session_id] = cancel_event

        q: "queue.Queue" = queue.Queue()

        async def event_stream():
            loop = asyncio.get_running_loop()
            # 异步跑阻塞 _run_chat
            fut = loop.run_in_executor(
                None, _run_chat, s, messages, content, character, mode, cancel_event, q
            )
            async for chunk in _stream_from_queue(q, fut, loop, session_id, cancel_event):
                yield chunk

        return _sse_response(event_stream())

    # ------------------------------------------------------------------
    # 重试 AI 回复（长按消息触发）：删该 msg 及其后所有 + 前一条 user，重生成
    # ------------------------------------------------------------------
    @app.post("/m/api/sessions/{session_id}/retry")
    async def api_retry(session_id: str, request: Request):
        s = storage.load_session(session_id)
        if not s:
            raise HTTPException(status_code=404, detail="会话不存在")
        try:
            body = await request.json()
        except Exception:
            body = {}
        msg_id = str(body.get("message_id", "") or "").strip()
        if not msg_id:
            raise HTTPException(status_code=400, detail="缺少 message_id")
        messages = storage.load_messages(session_id)
        # 从 messages 列表找出目标 assistant message 对象（regenerate_from 要求 Message 实例）
        target = next((m for m in messages if m.id == msg_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        if target.role != "assistant":
            raise HTTPException(status_code=400, detail="只能重试 AI 回复")

        cancel_event = threading.Event()
        with _cancel_lock:
            _cancel_events[session_id] = cancel_event
        q: "queue.Queue" = queue.Queue()

        async def event_stream():
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(
                None, _run_regen, s, messages, target, cancel_event, q
            )
            async for chunk in _stream_from_queue(q, fut, loop, session_id, cancel_event):
                yield chunk

        return _sse_response(event_stream())

    # ------------------------------------------------------------------
    # 停止生成
    # ------------------------------------------------------------------
    @app.post("/m/api/sessions/{session_id}/stop")
    async def api_stop(session_id: str):
        with _cancel_lock:
            ev = _cancel_events.get(session_id)
            if ev is not None:
                ev.set()
                return {"ok": True, "stopped": True}
        return {"ok": True, "stopped": False}

    # ------------------------------------------------------------------
    # 头像（角色 + 用户共用 avatars 目录，文件名）
    # ------------------------------------------------------------------
    @app.get("/m/api/avatars/{filename}")
    async def api_avatar(filename: str):
        # 防路径遍历：仅 basename
        safe = os.path.basename(filename)
        if not safe or safe != filename:
            raise HTTPException(status_code=400, detail="非法文件名")
        p = os.path.join(paths.avatars_dir(), safe)
        if not os.path.exists(p):
            raise HTTPException(status_code=404, detail="头像不存在")
        return FileResponse(p)

    # ------------------------------------------------------------------
    # 消息图片（image_path 是绝对路径，前端用 base64 编码后传回）
    # [!] 缓存策略：图片一旦生成不变（image_path 含生成唯一性文件名），URL 由不变
    #     image_path base64 派生 -> 同一张图永远同 URL。给 Cache-Control: max-age=86400
    #     让浏览器在 1 天内对同 URL 直接命中 disk cache 不发请求；过期后用 ETag 协商 304。
    #     解决手机端 reloadCurrentSession/openChat/retry 重建气泡时重复拉取同一张图的问题。
    # ------------------------------------------------------------------
    @app.get("/m/api/images/{encoded}")
    async def api_image(encoded: str, request: Request):
        import base64
        try:
            full = base64.b64decode(encoded).decode("utf-8")
        except Exception:
            raise HTTPException(status_code=400, detail="非法图片路径")
        # 安全校验：必须位于 images_dir 内（防任意文件读取）
        try:
            img_dir = os.path.abspath(paths.images_dir())
            real = os.path.abspath(full)
            if not real.startswith(img_dir + os.sep) and real != img_dir:
                raise HTTPException(status_code=403, detail="图片路径越界")
        except Exception:
            raise HTTPException(status_code=403, detail="图片路径校验失败")
        if not os.path.exists(full):
            raise HTTPException(status_code=404, detail="图片不存在")
        # 基于 st_mtime + st_size 算 ETag（与 Starlette FileResponse 算法对齐，但显式控制
        # 以保证 304 协商稳定命中）。图片不变则 ETag 不变，304 命中省带宽。
        try:
            st = os.stat(full)
            etag = f'"{int(st.st_mtime)}-{st.st_size}"'
        except OSError:
            etag = None
        if etag:
            inm = request.headers.get("if-none-match")
            if inm and inm == etag:
                # 协商缓存命中：不返 body，省带宽
                return Response(status_code=304, headers={
                    "ETag": etag,
                    "Cache-Control": "public, max-age=86400",
                })
            headers = {"Cache-Control": "public, max-age=86400", "ETag": etag}
        else:
            headers = {"Cache-Control": "public, max-age=86400"}
        return FileResponse(full, headers=headers)


# ============================================================================
# 启停
# [!] 本版本支持即时启停：RemoteServiceDialog 的「启动/停止/重启」按钮与 main.py
#     开机自启均直接调用 start_server/stop_server，无需重启 exe。
# ============================================================================
def start_server(services: dict, app_config) -> bool:
    """启动远程服务到后台 daemon 线程。返回是否成功（同步预检成功即 True）。

    [!] 调用方（main.py / RemoteServiceDialog）须 try/except 包裹：本函数同步抛
        RuntimeError 表端口占用/无法绑定，调用方据此弹窗反馈；端口预检通过后
        daemon 线程内 server.run() 的异步错误回收到 _last_error，由 get_status
        暴露给 UI（避免旧版 start_server 返回 True 后端口 bind 失败用户无感知）。
    [!] 若已运行直接返回 True。
    [!] 若 app_config.remote_pair_code 为空，自动生成 6 位并落盘。
    """
    global _server, _thread, _last_error, _stopping, _cur_port
    with _server_lock:
        if _server is not None and getattr(_server, "started", False):
            return True
        # 先清掉上次的错误状态（避免下拉切换端口后旧错误残留）
        _last_error = None
        _stopping = False
        # 确保配对码非空（首次开启自动生成 6 位数字并落盘，供 RemoteServiceDialog 展示）
        if not app_config.remote_pair_code:
            app_config.remote_pair_code = "".join(
                secrets.choice("0123456789") for _ in range(6)
            )
            app_config.remote_pair_code_updated_at = datetime.now().isoformat()
            try:
                services["storage"].save_app_config(app_config)
            except Exception:
                pass
        cfg_port = app_config.remote_port
        try:
            # 同步预检端口占用（丢线程前，让调用方能即时捕获）。位于 try 内，预检失败时
            # 走 except 清 _server 并把 RuntimeError 写 _last_error 让 UI 可见。
            _check_port_free(cfg_port)
            app = _build_app(services, app_config)
            uconfig = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=cfg_port,
                log_level="warning",
                lifespan="on",
                # 关闭访问日志（手机端高频拉消息会刷屏），warning 级只记错误
                access_log=False,
                # 用标准 asyncio 循环（uvicorn[standard] 自带 httptools 但 ASGI 不强依赖）
                loop="auto",
            )
            server = uvicorn.Server(uconfig)
            # 防止 Server.run 在 Windows 上把当前线程当成主线程来做信号处理
            server.install_signal_handlers = lambda: None
            _server = server
            _cur_port = cfg_port
            t = threading.Thread(
                target=_run_server_wrapper, args=(server,),
                daemon=True, name="DreamRoleRemote",
            )
            _thread = t
            t.start()
            return True
        except Exception as e:
            # 同步阶段失败（端口预检 / build_app / 起线程）：清模块级状态防 _server 非
            # None 但实际未启动；写 _last_error 让 get_status 反馈给 UI（不靠调用方
            # 弹窗才能看到）。
            _server = None
            _thread = None
            _last_error = f"{type(e).__name__}: {e}"
            raise


def stop_server(timeout: float = 5.0) -> bool:
    """停止远程服务，等待 daemon 线程退出或 timeout 到。返回是否真正停止。

    [!] 即时启停专用：RemoteServiceDialog「停止/重启」按钮调用。
    [!] join + force_exit 解决旧版 stop_server 两个问题：
        (1) 旧版仅设 should_exit 不 join，调用方立即返回但端口仍占用，立即重启会因
            端口未释放而 bind 失败 -> RuntimeError 端口被占用；
        (2) 旧版立即 _server=None 致 stop 后再快速查 is_running 得到 False，但实际
        daemon 线程还在跑，状态与运行时脱节 -> 端口仍占用但 UI 显示已停止。
    [!] _server/_thread 在 join 完成后才置 None，期间 _stopping=True 让 get_status
        返回 "stopping"，UI 可正确显示「停止中...」禁用按钮。
    """
    global _server, _thread, _last_error, _stopping
    # 锁内取句柄并标记 stopping（不立即清 _server，避免 get_status 在 join 期间误判为
    # stopped；join 完成后再清）
    with _server_lock:
        srv = _server
        th = _thread
        if srv is None or th is None:
            _stopping = False
            return True
        _stopping = True
        # 设退出标志（同时 force_exit 让 uvicorn 更快退出，不等 keep-alive 连接）
        try:
            srv.should_exit = True
            srv.force_exit = True
        except Exception:
            pass
    # 释锁等线程退出（join 期间不持锁，避免 daemon 线程内 _run_server_wrapper 取锁死锁）
    if th is not None and th.is_alive():
        th.join(timeout)
    stopped = not (th.is_alive() if th else True)
    with _server_lock:
        if stopped:
            # 真正退出：清状态，get_status 回到 stopped
            _stopping = False
            _server = None
            _thread = None
            # 停止是用户主动行为，不计为错误，清掉旧错误状态
            _last_error = None
        else:
            # [!] join 超时但线程仍活：不清 _server/_thread，保留 _stopping=True 让
            # get_status 返回 "stopping"，UI 继续显示「停止中…」并禁用按钮；调用方据
            # 返回的 False 可重试。避免旧版「超时仍清状态 -> UI 谎报停止 + orphan 线程
            # 泄漏且无法再 join」的双重问题。
            pass
    return stopped


def restart_server(services: dict, app_config) -> bool:
    """重启远程服务（停止旧实例 -> 等待端口释放 -> 起新实例）。

    [!] 即时启停专用：RemoteServiceDialog「重启」按钮 / 端口变更后保存调用。
        单纯 stop 再 start 有竞态：stop join 返回后端口可能仍由内核保持 TIME_WAIT
        一小段时间；start_server 内 _check_port_free 用 SO_REUSEADDR 已能容忍
        TIME_WAIT 占用，故 stop+start 组合在常见情况下安全。
    """
    # 已停止 -> 直接起；运行中 -> 先停再起
    if is_running():
        stop_server()
    return start_server(services, app_config)