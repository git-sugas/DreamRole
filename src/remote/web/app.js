/* DreamRole 手机端 - 单页应用逻辑
 * 视图：登录 / 会话列表 / 聊天
 * SSE：用 fetch + ReadableStream 读 text/event-stream（EventSource 不支持 POST + 带 body）。
 * 流式跟随 PC 端 api.streaming（服务端编排器自动判断，前端不区分）。
 */

const API = '/m/api';
const $ = (id) => document.getElementById(id);

const state = {
  currentSession: null,
  sessionDetail: null,
  selectedSpeakerId: '',   // 群聊 manual 模式手机端选择的角色 id
  generating: false,        // 是否正在生成（用 abort 控制取消 + 关闭 SSE）
  abortCtrl: null,
  pendingSummaryReload: false,  // 本轮触发了自动总结，生成结束后全量重渲染
};

/* ============ 工具函数 ============ */
function showView(id) {
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  const el = $(id);
  el.classList.add('active');
}

function showStatus(text) {
  const o = $('status-overlay');
  o.textContent = text;
  o.classList.add('text', 'show');
  if (text) o.classList.add('show'); else o.classList.remove('show');
}
function clearStatus() { $('status-overlay').classList.remove('show'); }

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

function escHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function avatarUrl(name) {
  if (!name) return '';
  return `${API}/avatars/${encodeURIComponent(name)}`;
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) {
      return d.toTimeString().slice(0, 5);
    }
    return `${d.getMonth() + 1}/${d.getDate()}`;
  } catch { return ''; }
}

/* ============ 登录 ============ */
async function init() {
  // 自动检测 URL 携带的配对码（扫码场景：server.py 把 ?code= 注入 meta）
  const meta = document.querySelector('meta[name="dr-pair-code"]');
  const urlParams = new URLSearchParams(location.search);
  const autoCode = (meta && meta.content) || urlParams.get('code') || '';

  // 先 ping 看是否已登录
  try {
    const r = await fetch(`${API}/ping`);
    if (r.ok) {
      const j = await r.json();
      if (j.authed) {
        // 已登录：先加载渲染规则再进会话列表（rules 影响气泡着色）
        if (window.DRRender) await DRRender.loadRules();
        // 启动规则热更新轮询（PC 端改规则后自动感知重渲）
        if (window.DRRender && DRRender.startPolling) DRRender.startPolling(onRulesChanged);
        enterSessions();
        return;
      }
    }
  } catch {}

  if (autoCode) {
    $('pair-input').value = autoCode;
    doLogin();
    return;
  }
  showView('view-login');
  $('pair-input').focus();
}

$('login-btn').addEventListener('click', doLogin);
$('pair-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });

async function doLogin() {
  const code = $('pair-input').value.trim();
  const errEl = $('login-err');
  errEl.textContent = '';
  if (!/^\d{6}$/.test(code)) {
    errEl.textContent = '请输入 6 位配对码';
    return;
  }
  $('login-btn').disabled = true;
  try {
    const r = await fetch(`${API}/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    const j = await r.json();
    if (j.ok) {
      if (window.DRRender) { try { await DRRender.loadRules(); } catch {} }
      // 启动规则热更新轮询（与 init 已登录路径一致）
      if (window.DRRender && DRRender.startPolling) DRRender.startPolling(onRulesChanged);
      enterSessions();
    } else {
      errEl.textContent = j.detail || '登录失败';
      $('login-btn').disabled = false;
    }
  } catch (e) {
    errEl.textContent = '网络错误';
    $('login-btn').disabled = false;
  }
}

$('logout-btn').addEventListener('click', async () => {
  // 删除客户端 cookie（设过期）
  document.cookie = 'dr_remote_pair=; Max-Age=0; path=/';
  location.reload();
});

/* ============ 会话列表 ============ */
async function enterSessions() {
  showView('view-sessions');
  const list = $('session-list');
  list.innerHTML = '<div class="empty-state">加载中…</div>';
  try {
    const r = await fetch(`${API}/sessions`);
    if (r.status === 401) { showView('view-login'); return; }
    const j = await r.json();
    renderSessions(j.sessions || []);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">加载失败：${escHtml(String(e))}</div>`;
  }
}

function renderSessions(items) {
  const list = $('session-list');
  if (!items.length) {
    list.innerHTML = '<div class="empty-state">PC 端暂无会话<br>请先在 PC 端创建会话</div>';
    return;
  }
  list.innerHTML = items.map((s) => {
    const chars = s.characters || [];
    // 群聊也只显示第一个人物头像（多头像堆叠在列表里显得乱）
    const avatars = chars.length ? `<img src="${avatarUrl(chars[0].avatar)}" alt="">` : '';
    const typeTag = s.session_type === 'group' ? '群聊' : '单聊';
    const last = s.last_text ? `<span>${s.last_role === 'user' ? '我：' : ''}${escHtml(s.last_text)}</span>` : '<span style="color:#565f89">暂无消息</span>';
    return `
      <div class="session-item" data-id="${escHtml(s.id)}">
        <div class="avatars">${avatars}</div>
        <div class="meta">
          <div class="title">${escHtml(s.title)} <span class="badge-type">${typeTag}</span></div>
          <div class="last">${last}</div>
        </div>
        <div class="time">${formatTime(s.updated_at)}</div>
      </div>`;
  }).join('');
  list.querySelectorAll('.session-item').forEach((el) => {
    el.addEventListener('click', () => openChat(el.dataset.id));
  });
}

/* ============ 聊天视图 ============ */
async function openChat(sessionId) {
  showView('view-chat');
  state.currentSession = sessionId;
  state.selectedSpeakerId = '';
  $('msg-list').innerHTML = '<div class="empty-state">加载中…</div>';
  $('speaker-bar').classList.remove('show');
  try {
    const r = await fetch(`${API}/sessions/${encodeURIComponent(sessionId)}`);
    if (r.status === 401) { showView('view-login'); return; }
    if (!r.ok) { $('msg-list').innerHTML = `<div class="empty-state">会话加载失败 (${r.status})</div>`; return; }
    const j = await r.json();
    state.sessionDetail = j;
    renderSession(j);
  } catch (e) {
    $('msg-list').innerHTML = `<div class="empty-state">网络错误：${escHtml(String(e))}</div>`;
  }
}

// 静默重载当前会话（不重置选角状态、不显示「加载中」）：用于自动总结后全量重渲染，
// 让 summary 落到正确位置 + 旧消息折叠成 collapsed_block 占位（与手动刷新行为一致）。
async function reloadCurrentSession() {
  if (!state.currentSession) return;
  try {
    const r = await fetch(`${API}/sessions/${encodeURIComponent(state.currentSession)}`);
    if (!r.ok) return;
    const j = await r.json();
    state.sessionDetail = j;
    renderSession(j);
  } catch (e) { /* 静默失败，不影响已展示的内容 */ }
}

// 渲染规则热更新回调：PC 端改完规则后手机端轮询到版本变化时触发。
// [!] 仅在「有当前会话 + 无流式生成进行中」时重渲当前会话气泡；流式中规则已更新到
//     renderState，重渲会清掉流式占位气泡故跳过，等下次自然渲染用新规则。
// [!] 500ms 防抖：visibilitychange 与 15s 定时器可能撞车，合并为一次重渲。
let _rulesChangedTimer = null;
function onRulesChanged() {
  if (_rulesChangedTimer) clearTimeout(_rulesChangedTimer);
  _rulesChangedTimer = setTimeout(() => {
    _rulesChangedTimer = null;
    if (state.currentSession && !state.generating) {
      reloadCurrentSession();
    }
  }, 500);
}

function renderSession(detail) {
  const s = detail.session;
  $('chat-title').textContent = s.title || '会话';
  const chars = s.characters || [];
  $('chat-sub').textContent = chars.map((c) => c.name).join('、') || '';
  // 群聊 manual 模式展示选角条
  const bar = $('speaker-bar');
  const chips = $('speaker-chips');
  if (s.session_type === 'group' && s.group_mode === 'manual' && chars.length > 1) {
    state.selectedSpeakerId = s.default_speaker_id || (chars[0] && chars[0].id) || '';
    chips.innerHTML = chars.map((c) => `
      <div class="speaker-chip ${c.id === state.selectedSpeakerId ? 'active' : ''}" data-id="${escHtml(c.id)}">
        <img src="${avatarUrl(c.avatar)}" alt="">
        <div class="nm">${escHtml(c.name)}</div>
      </div>`).join('');
    chips.querySelectorAll('.speaker-chip').forEach((el) => {
      el.addEventListener('click', () => {
        state.selectedSpeakerId = el.dataset.id;
        chips.querySelectorAll('.speaker-chip').forEach((c) => c.classList.remove('active'));
        el.classList.add('active');
      });
    });
    bar.classList.add('show');
  } else {
    bar.classList.remove('show');
  }
  renderMessages(detail.messages || []);
}

function renderMessages(msgs) {
  const list = $('msg-list');
  if (!msgs.length) {
    list.innerHTML = '<div class="empty-state">暂无消息，发送第一条吧</div>';
    return;
  }
  list.innerHTML = '';
  msgs.forEach((m) => list.appendChild(buildBubble(m)));
  scrollBottom();
  // [!] 进会话必须可靠滚到底：图片 loading="lazy" 异步加载完成后会撑高列表，
  // 首次 scrollBottom 时 scrollHeight 尚不含图片高度，停在中间。补两轮延迟
  // 重滚 + 给未加载完的图片挂 load 后再滚一次（仅整列表渲染场景，此时语义即
  // 「定位到最新」，粘底可接受；流式追加不走此分支不受影响）。
  requestAnimationFrame(() => scrollBottom());
  setTimeout(scrollBottom, 300);
  list.querySelectorAll('img').forEach((img) => {
    if (!img.complete) img.addEventListener('load', scrollBottom, { once: true });
  });
}

function buildBubble(m) {
  // 折叠块占位（服务端聚合一组 collapsed 消息）：居中提示卡「已折叠 N 条消息」，
  // 与 PC 端 CollapsedBlock 视觉等价（手机端不展开原文，只提示有这批被总结过）。
  if (m.role === 'collapsed_block') {
    const div = document.createElement('div');
    div.className = 'msg collapsed-block';
    div.dataset.id = m.id;
    const reasonText = m.collapsed_reason === 'manual' ? '手动折叠' : '自动总结';
    div.innerHTML = `<div class="body"><div class="bubble">[折叠] 已折叠 ${m.collapsed_count} 条消息（${reasonText}）</div></div>`;
    return div;
  }

  const div = document.createElement('div');
  div.className = `msg ${m.role === 'user' ? 'user' : (m.role === 'summary' ? 'summary' : (m.role === 'system' ? 'system' : ''))}`;
  div.dataset.id = m.id;
  // 头像
  let avatar = '';
  if (m.role === 'user') {
    const u = state.sessionDetail && state.sessionDetail.session.user;
    avatar = (u && u.avatar) ? avatarUrl(u.avatar) : '';
  } else if (m.role === 'assistant' || m.role === 'summary') {
    // 找角色
    const s = state.sessionDetail && state.sessionDetail.session;
    const chars = (s && s.characters) || [];
    const c = chars.find((x) => x.id === m.character_id) || chars[0];
    avatar = c && c.avatar ? avatarUrl(c.avatar) : '';
  }
  // 名称
  let name = '';
  if (m.role === 'user') name = state.sessionDetail && state.sessionDetail.session.player_name || '我';
  else if (m.role === 'assistant') name = m.character_name || ((state.sessionDetail.session.characters.find(x=>x.id===m.character_id))||{}).name || 'AI';
  else if (m.role === 'summary') name = '上文总结';
  else if (m.role === 'system') name = '系统';

  // 内容
  let inner;
  if (m.is_image_only && m.image_url) {
    // 纯图片消息：图片 + 下方灰色 caption（PC 端 _build_image_only 同款布局，
    // caption = 生成时的中文 prompt，对用户是有用信息）。content 为空时只显示图。
    // [!] loading="lazy"：长会话滚到视口才加载图片，省流量；URL 由不变 image_path
    //     派生，浏览器对同 URL 命中 disk cache 不重复请求（服务端已加 Cache-Control）。
    const captionHtml = m.content ? `<div class="img-caption">${escHtml(m.content)}</div>` : '';
    inner = `<img src="${m.image_url}" alt="" loading="lazy">${captionHtml}`;
  } else if (m.role === 'summary') {
    // [!] summary 走 DRRender 着色（PC 端 summary 气泡正文是富文本含分色，
    // 手机端早期用 <em> 纯转义丢分色，现对齐 PC 端走着色）
    inner = window.DRRender
      ? DRRender.render(m.content || '', false, m.rendered_html || '')
      : `<em>${escHtml(m.content)}</em>`;
  } else if (window.DRRender) {
    // 主路径：PC 端 rendered_html 优先（markup 模式）/ mark.js 兜底（markdown/auto）
    inner = DRRender.render(m.content || '', m.role === 'user', m.rendered_html || '');
  } else {
    inner = escHtml(m.content);
  }

  // [!] 已停止角标：被中断的部分回复，与完整回复视觉区分（PC 端红色斜体 ⏹ 已停止）
  let stoppedBadge = '';
  if (m.is_stopped) {
    stoppedBadge = '<div class="stopped-badge">⏹ 已停止</div>';
  }

  div.innerHTML = `
    ${avatar ? `<img class="avatar" src="${avatar}">` : `<div style="width:36px"></div>`}
    <div class="body">
      ${m.role !== 'user' && name ? `<div class="name">${escHtml(name)}</div>` : ''}
      <div class="bubble">${inner}</div>
      ${stoppedBadge}
    </div>`;
  return div;
}

function scrollBottom() {
  const list = $('msg-list');
  list.scrollTop = list.scrollHeight;
}

/* ============ 长按消息：上下文菜单（重试/复制） ============ */
// 长按计时 600ms 触发，干预 click/scroll 用 touchmove 超阈值取消。
const LONG_PRESS_MS = 600;
const MOVE_TOLERANCE = 10;
let longPressTimer = null;
let longPressTarget = null;
let longPressStart = null;

function initLongPress() {
  const list = $('msg-list');
  list.addEventListener('touchstart', (e) => {
    if (state.generating) return;
    const msgEl = e.target.closest('.msg');
    if (!msgEl || !msgEl.dataset.id) return;
    longPressTarget = msgEl;
    longPressStart = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    msgEl.classList.add('pressing');
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      // 触发上下文菜单
      const msgId = msgEl.dataset.id;
      const msg = findMessageById(msgId);
      if (msg) showCtxMenu(msg, msgEl);
      // 取消 press 视觉态由 touchend 处理
    }, LONG_PRESS_MS);
  }, { passive: true });

  list.addEventListener('touchmove', (e) => {
    if (!longPressStart || !longPressTimer) return;
    const dx = e.touches[0].clientX - longPressStart.x;
    const dy = e.touches[0].clientY - longPressStart.y;
    if (Math.abs(dx) > MOVE_TOLERANCE || Math.abs(dy) > MOVE_TOLERANCE) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
      if (longPressTarget) longPressTarget.classList.remove('pressing');
    }
  }, { passive: true });

  list.addEventListener('touchend', () => {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    if (longPressTarget) longPressTarget.classList.remove('pressing');
  });
  list.addEventListener('touchcancel', () => {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    if (longPressTarget) longPressTarget.classList.remove('pressing');
  });

  // 桌面浏览器右键也触发（鼠标长按/event contextmenu）
  list.addEventListener('contextmenu', (e) => {
    if (state.generating) return;
    const msgEl = e.target.closest('.msg');
    if (!msgEl || !msgEl.dataset.id) return;
    e.preventDefault();
    const msg = findMessageById(msgEl.dataset.id);
    if (msg) showCtxMenu(msg, msgEl);
  });

  // 遮罩点击关闭菜单
  $('ctx-mask').addEventListener('click', hideCtxMenu);
}

function findMessageById(id) {
  // 优先从当前 sessionDetail 找（历史消息），不在内存里就 return null
  if (!state.sessionDetail) return null;
  // session messages 不一定有完整字段；用 msg-list DOM 直接拿 bubble 元素无意义。
  // 历史消息在 state.sessionDetail.messages 里；流式占位/tmp 不能重试。
  const msgs = state.sessionDetail.messages || [];
  return msgs.find((m) => m.id === id);
}

function showCtxMenu(msg, msgEl) {
  const menu = $('ctx-menu');
  const mask = $('ctx-mask');
  let html = '';
  // 重试：仅 AI 回复消息（非纯图片）
  if (msg.role === 'assistant' && !msg.is_image_only) {
    html += `<div class="ctx-item" data-act="retry">🔄 重试<span class="hint">删除该回复并重新生成</span></div>`;
  }
  // 复制文本
  if (msg.content) {
    html += `<div class="ctx-item" data-act="copy">📋 复制文本</div>`;
  }
  html += `<div class="ctx-divider"></div>`;
  html += `<div class="ctx-item danger" data-act="cancel">取消</div>`;
  menu.innerHTML = html;
  menu.style.display = 'block';
  mask.classList.add('show');
  // 项点击
  menu.querySelectorAll('.ctx-item').forEach((el) => {
    el.addEventListener('click', () => {
      const act = el.dataset.act;
      hideCtxMenu();
      if (act === 'retry') doRetry(msg);
      else if (act === 'copy') {
        navigator.clipboard && navigator.clipboard.writeText(msg.content || '').then(
          () => toast('已复制'), () => toast('复制失败')
        );
      }
    });
  });
}

function hideCtxMenu() {
  $('ctx-menu').style.display = 'none';
  $('ctx-mask').classList.remove('show');
}

/* 重试：发 SSE 走 /retry，复用流式渲染基础设施 */
async function doRetry(msg) {
  if (state.generating) { toast('正在生成中，请先停止'); return; }
  if (!state.currentSession) return;
  setGenerating(true);
  state.abortCtrl = new AbortController();
  try {
    const resp = await fetch(`${API}/sessions/${encodeURIComponent(state.currentSession)}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: msg.id }),
      signal: state.abortCtrl.signal,
    });
    if (resp.status === 401) { showView('view-login'); setGenerating(false); return; }
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { const j = await resp.json(); detail = j.detail || detail; } catch {}
      toast('重试失败：' + detail);
      setGenerating(false);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const evBlock = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        handleRetrySSEBlock(evBlock);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') toast('网络错误：' + e);
  } finally {
    finalizeBubble();
    setGenerating(false);
    state.abortCtrl = null;
    // [!] retry 期间若触发了自动总结，同样全量重渲染（同 chat 路径）。
    if (state.pendingSummaryReload) {
      state.pendingSummaryReload = false;
      await reloadCurrentSession();
    }
  }
}

function handleRetrySSEBlock(block) {
  const lines = block.split('\n');
  let dataLine = '';
  for (const ln of lines) {
    if (ln.startsWith('data:')) dataLine += ln.slice(5).trim();
  }
  if (!dataLine) return;
  let ev;
  try { ev = JSON.parse(dataLine); } catch { return; }
  switch (ev.type) {
    case 'retry_start':
      // 删除被重试 message 及其后的气泡，并创建新流式占位 assistant 气泡
      (ev.deleted_ids || []).forEach((id) => {
        const el = $('msg-list').querySelector(`.msg[data-id="${CSS.escape(id)}"]`);
        if (el) el.remove();
      });
      // [!] 同步从内存消息列表删除（与 DOM 删除对称），否则下次 findMessageById
      //    命中已删旧消息，doRetry 用旧 message_id 调 /retry 会 404。
      untrackMessageIds(ev.deleted_ids || []);
      // 创建新流式占位气泡（用被重试消息的 character 作发言者）
      const chars = (state.sessionDetail.session.characters) || [];
      const cMatch = chars.find((c) => c.id === state.sessionDetail.session.default_speaker_id) || chars[0];
      const streamMsg = {
        id: 'stream-' + Date.now(),
        role: 'assistant',
        content: '',
        character_id: cMatch && cMatch.id || '',
        character_name: cMatch && cMatch.name || '',
        image_url: '', is_image_only: false, is_summary: false,
      };
      const bubbleEl = buildBubble(streamMsg);
      $('msg-list').appendChild(bubbleEl);
      currentStreamingBubbleBody = bubbleEl.querySelector('.bubble');
      currentStreamingText = '';
      currentStreamRenderedHtml = '';
      const cursor = document.createElement('span');
      cursor.className = 'cursor';
      currentStreamingBubbleBody.appendChild(cursor);
      break;
    case 'chunk':
      currentStreamingText += ev.text;
      currentStreamRenderedHtml = ev.rendered_html || '';
      updateStreamingText();
      break;
    case 'message':
      handleServerMessage(ev.msg);
      break;
    case 'summary':
      // retry 期间也可能触发自动总结（chat_orchestrator.py:171）。与 chat 路径一致：
      // 不 appendChild（会落末尾顺序错乱），设标记等流结束后全量重渲染。
      if (ev.msg) {
        state.pendingSummaryReload = true;
      }
      break;
    case 'image':
      // 走 chat 一致的分支逻辑
      if (ev.msg && ev.msg.image_url) {
        $('msg-list').appendChild(buildBubble(ev.msg));
        trackMessage(ev.msg);  // 图片消息也入内存列表（可被复制 prompt）
        scrollBottom();
      }
      break;
    case 'status':
      if (ev.text) showStatus(ev.text); else clearStatus();
      break;
    case 'error':
      finishStreamError(ev.message || '重试错误');
      break;
    case 'done':
      clearStatus();
      break;
    case 'speaker':
      // auto 模式重试可能经导演重新选角（与原发言者不同），同步 name + avatar
      updateStreamingHeader(ev.name, ev.avatar);
      break;
  }
}

/* ============ 发送消息（SSE） ============ */
let currentStreamingBubbleBody = null;
let currentStreamingText = '';
let currentStreamRenderedHtml = '';  // 服务端流式累积后给的 rendered_html（markup 模式）

// 更新流式占位气泡的 name + avatar（auto 模式选角结果到达时同步发言者头像+名字，
// 与 PC 端 _on_speaker 高亮面板 + 占位 update_header 对齐）。
function updateStreamingHeader(name, avatar) {
  if (!currentStreamingBubbleBody) return;
  const body = currentStreamingBubbleBody.parentElement;        // .body
  const msgEl = body ? body.parentElement : null;                 // .msg
  if (name && body) {
    const nm = body.querySelector('.name');
    if (nm) nm.textContent = name;
  }
  if (avatar && msgEl) {
    const av = msgEl.querySelector('img.avatar');
    if (av) av.src = avatarUrl(avatar);
  }
}

// 维护当前会话内存消息列表（state.sessionDetail.messages）：供 findMessageById 命中
// 本轮新生成的消息（重试/复制依赖）。前后端时序约定：服务端只在 save_message 后才
// emit on_message，故此处的 msg.id 即 DB id，安全作为后续定位键。
function trackMessage(msg) {
  if (!state.sessionDetail) return;
  const arr = state.sessionDetail.messages || (state.sessionDetail.messages = []);
  // 去重：相同 id 不重复 push（同一引用多次 emit 的兜底）
  if (!arr.some((m) => m.id === msg.id)) arr.push(msg);
}

// 从内存消息列表移除一批 id（重试 retry_start 删除旧消息时同步清理，避免下次
// findMessageById 命中已删的旧 id）。
function untrackMessageIds(ids) {
  if (!state.sessionDetail || !state.sessionDetail.messages || !ids || !ids.length) return;
  const idSet = new Set(ids);
  state.sessionDetail.messages = state.sessionDetail.messages.filter((m) => !idSet.has(m.id));
}

// 发送/停止合一：按钮根据 state.generating 决定行为（避免双 handler 冲突）
$('send-btn').addEventListener('click', () => {
  if (state.generating) stopGenerate();
  else sendMessage();
});
$('msg-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendMessage();
  }
});
// 自适应输入高度
$('msg-input').addEventListener('input', (e) => {
  const ta = e.target;
  ta.style.height = '44px';
  ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
});

async function sendMessage() {
  const ta = $('msg-input');
  const content = ta.value.trim();
  if (!content || state.generating) return;
  if (!state.currentSession) return;
  ta.value = '';
  ta.style.height = '44px';
  // 立即追加一条 user 气泡
  const list = $('msg-list');
  if (list.querySelector('.empty-state')) list.innerHTML = '';
  list.appendChild(buildBubble({
    id: 'tmp-' + Date.now(),
    role: 'user',
    content: content,
    character_name: '',
    image_url: '',
    is_image_only: false,
    is_summary: false,
  }));
  scrollBottom();

  // 创建占位 assistant 气泡（流式追加）
  const streamingMsg = {
    id: 'stream-' + Date.now(),
    role: 'assistant',
    content: '',
    character_id: state.selectedSpeakerId || (state.sessionDetail.session.characters[0]||{}).id || '',
    character_name: (state.sessionDetail.session.characters.find(x=>x.id===(state.selectedSpeakerId || (state.sessionDetail.session.characters[0]||{}).id)) || {}).name || '',
    image_url: '', is_image_only: false, is_summary: false,
  };
  const bubbleEl = buildBubble(streamingMsg);
  list.appendChild(bubbleEl);
  currentStreamingBubbleBody = bubbleEl.querySelector('.bubble');
  currentStreamingText = '';
  currentStreamRenderedHtml = '';
  // 闪烁光标
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  currentStreamingBubbleBody.appendChild(cursor);

  setGenerating(true);
  state.abortCtrl = new AbortController();
  try {
    const resp = await fetch(`${API}/sessions/${encodeURIComponent(state.currentSession)}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        character_id: state.selectedSpeakerId,
      }),
      signal: state.abortCtrl.signal,
    });
    if (resp.status === 401) { showView('view-login'); setGenerating(false); return; }
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { const j = await resp.json(); detail = j.detail || detail; } catch {}
      finishStreamError(detail);
      return;
    }
    // 读 SSE stream
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      // 按事件分隔解析（data: ...\n\n）
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const evBlock = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        handleSSEBlock(evBlock);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      finishStreamError('网络错误：' + e);
    } else {
      // 用户主动停止，不报错；服务端会推 done
    }
  } finally {
    finalizeBubble();
    setGenerating(false);
    state.abortCtrl = null;
    // [!] 本轮若触发了自动总结，生成结束后全量重渲染（summary 落到正确位置 +
    // 旧消息折叠成 collapsed_block 占位）。不在 summary 事件到达时立即重渲染是
    // 为保护流式占位气泡；此时流已读完、占位已被正式消息替换，重渲染安全。
    if (state.pendingSummaryReload) {
      state.pendingSummaryReload = false;
      await reloadCurrentSession();
    }
  }
}

function handleSSEBlock(block) {
  const lines = block.split('\n');
  let dataLine = '';
  for (const ln of lines) {
    if (ln.startsWith('data:')) dataLine += ln.slice(5).trim();
  }
  if (!dataLine) return;
  let ev;
  try { ev = JSON.parse(dataLine); } catch { return; }
  switch (ev.type) {
    case 'chunk':
      currentStreamingText += ev.text;
      // 服务端流式累积后调 markup.render 一并给了 rendered_html（markup 模式）；
      // markdown/auto 模式服务端 HTML 仍是着色版纯文本，前端用 DRRender 兜底结构化。
      currentStreamRenderedHtml = ev.rendered_html || '';
      updateStreamingText();
      break;
    case 'status':
      if (ev.text) showStatus(ev.text); else clearStatus();
      break;
    case 'message':
      // 服务端保存的正式消息（user/assistant），替换/追加
      handleServerMessage(ev.msg);
      break;
    case 'image':
      // 图片事件：服务端推独立纯图片消息（已落库），前端作为独立纯图片气泡追加。
      // [!] 时序：on_message(assistant) 已在 on_image 之前把流式占位文本气泡替换成
      //     正式 assistant 气泡（编排器 _process_image_tags 在落库 assistant 之后调，
      //     见 chat_orchestrator.py:300-310），故此处 stream 选择器通常找不到 -> 直接
      //     appendChild 末尾即可，顺序自然正确（assistant 文本在上，图片气泡紧随其后）。
      console.debug('[image event]', ev);
      if (ev.msg && ev.msg.image_url) {
        $('msg-list').appendChild(buildBubble(ev.msg));
        trackMessage(ev.msg);  // 图片消息也入内存列表（可被复制 prompt）
        scrollBottom();
      } else if (ev.image_url) {
        // 兜底：服务端没带 msg，单独用 image_url 构造一个图片气泡（用临时 id，不入
        // 内存列表——无 DB id，下次刷新会被真实 DB 消息替换）。
        $('msg-list').appendChild(buildBubble({
          id: 'img-' + Date.now(),
          role: 'assistant',
          is_image_only: true,
          image_url: ev.image_url,
          content: ev.prompt || '',
          rendered_html: '',
          image_path: '',
          is_summary: false,
        }));
        scrollBottom();
      }
      break;
    case 'speaker':
      // 群聊选角结果（auto 模式）：同步 name + avatar（auto 选角可能与 selectedSpeaker
      // 不同，仅更新 name 会让头像仍是初始占位角色）。
      updateStreamingHeader(ev.name, ev.avatar);
      break;
    case 'summary':
      // 自动总结：服务端在 LLM 回复之前触发（编排器 generate_response 入口先总结）。
      // [!] 不在此处 appendChild summary 气泡：summary 的逻辑位置在被折叠的旧消息之后、
      // 本轮消息之前（DB 按时间戳排序），而流式 append 模型下 appendChild 永远落末尾，
      // 会把 summary 摆到刚生成的 assistant 之后（顺序错乱）。改为设标记，等本轮生成
      // 结束（SSE 流读完）后全量重拉 GET /sessions/{id} 重渲染，与刷新行为一致：
      // summary 自动落到折叠块后、旧消息被折叠成 collapsed_block 占位。
      // [!] 不能在 summary 事件到达时立即重渲染：此时 tmp-user / stream-assistant 占位
      // 气泡已在 DOM，立即重渲染会清掉它们，后续 chunk / message 事件找不到占位致流式崩。
      if (ev.msg) {
        state.pendingSummaryReload = true;
      }
      break;
    case 'usage':
      // 可用于状态展示，当前省略
      break;
    case 'error':
      finishStreamError(ev.message || '生成错误');
      break;
    case 'done':
      clearStatus();
      break;
  }
}

function updateStreamingText() {
  if (!currentStreamingBubbleBody) return;
  // 服务端给了 rendered_html（流式累积后的着色 HTML）-> 直接用；
  // 否则前端用 DRRender 兜底渲染（markdown 路径）；都没有最后退 escHtml。
  let html;
  if (window.DRRender) {
    html = DRRender.render(currentStreamingText, false, currentStreamRenderedHtml || '');
  } else {
    html = escHtml(currentStreamingText);
  }
  // 保留尾部光标元件
  const cursor = currentStreamingBubbleBody.querySelector('.cursor');
  currentStreamingBubbleBody.innerHTML = html;
  if (cursor) currentStreamingBubbleBody.appendChild(cursor);
  scrollBottom();
}

function handleServerMessage(msg) {
  // 服务端保存了正式 user 消息（延迟存储，LLM 回复后才 emit）：移除临时 user 气泡，
  // 把正式 user 气泡插到流式占位 assistant 气泡前面（保持 user 在 assistant 之上的视觉顺序）。
  // [!] 不能 appendChild 到末尾：此时流式占位 assistant 通常已在末尾，appendChild 会让 user
  //     落到 assistant 之后，视觉上变成 assistant 在 user 上方（顺序错乱）。
  if (msg.role === 'user') {
    const tmp = $('msg-list').querySelector('.msg.user[data-id^="tmp-"]');
    if (tmp) tmp.remove();
    const stream = $('msg-list').querySelector('.msg[data-id^="stream-"]');
    const newEl = buildBubble(msg);
    if (stream && stream.parentNode) {
      stream.parentNode.insertBefore(newEl, stream);
    } else {
      $('msg-list').appendChild(newEl);
    }
    trackMessage(msg);  // [!] 同步进内存列表，供后续 findMessageById（重试/复制）
    scrollBottom();
    return;
  }
  // assistant 正式消息：替换流式占位气泡
  if (msg.role === 'assistant') {
    const stream = $('msg-list').querySelector('.msg[data-id^="stream-"]');
    if (stream) {
      // 把流式内容更新为正式 content（用服务端已渲染的 rendered_html）
      currentStreamingText = msg.content || currentStreamingText;
      const newEl = buildBubble({ ...msg, content: currentStreamingText });
      stream.replaceWith(newEl);
      currentStreamingBubbleBody = null;
      scrollBottom();
    } else {
      $('msg-list').appendChild(buildBubble(msg));
      scrollBottom();
    }
    trackMessage(msg);  // [!] 同步进内存列表，供后续 findMessageById（重试/复制）
  }
  // summary 消息暂不渲染（与 PC 端一致 collapsed）
}

function finishStreamError(msg) {
  if (currentStreamingBubbleBody) {
    const cursor = currentStreamingBubbleBody.querySelector('.cursor');
    if (cursor) cursor.remove();
    if (!currentStreamingText) {
      currentStreamingBubbleBody.innerHTML = `<span style="color:#f7768e">${escHtml(msg)}</span>`;
    } else {
      currentStreamingBubbleBody.appendChild(
        Object.assign(document.createElement('div'), {
          style: 'color:#f7768e;font-size:13px;margin-top:6px',
          textContent: '（' + msg + '）',
        })
      );
    }
  }
  toast(msg);
  clearStatus();
}

function finalizeBubble() {
  if (currentStreamingBubbleBody) {
    const cursor = currentStreamingBubbleBody.querySelector('.cursor');
    if (cursor) cursor.remove();
    currentStreamingBubbleBody = null;
  }
}

function setGenerating(on) {
  state.generating = on;
  const btn = $('send-btn');
  if (on) {
    btn.textContent = '停止';
    btn.classList.remove('primary');
    btn.classList.add('stop');
  } else {
    btn.textContent = '发送';
    btn.classList.remove('stop');
    btn.classList.add('primary');
  }
}

// 停止生成（点「停止」按钮触发）：通知服务端取消 + 中断前端 fetch
async function stopGenerate() {
  try {
    await fetch(`${API}/sessions/${encodeURIComponent(state.currentSession)}/stop`, {
      method: 'POST',
    });
  } catch {}
  if (state.abortCtrl) state.abortCtrl.abort();
}

// 返回按钮
$('back-btn').addEventListener('click', () => {
  if (state.generating) {
    if (state.abortCtrl) state.abortCtrl.abort();
    try { fetch(`${API}/sessions/${encodeURIComponent(state.currentSession)}/stop`, { method: 'POST' }); } catch {}
    setGenerating(false);
  }
  enterSessions();
});

// 启动
initLongPress();
init();