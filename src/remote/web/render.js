/* DreamRole 手机端气泡渲染模块
 *
 * 跟随 PC 端 RenderRulesConfig（rules + ai/user_default_color + render_mode）。
 *
 * 三档模式：
 *   markup  -> 优先用服务端给的 rendered_html（PC 端 markup.render 算好的），
 *              无 rendered_html 时本地复刻 markup 着色。
 *   markdown/auto -> 本地 marked.js 结构渲染 + 对文本片段跑规则着色（与 PC 端
 *              setMarkdown + 二次着色对齐）。
 *
 * 着色复刻 markup.py 核心路径：合并命名分组正则按 priority 升序 -> finditer 切片
 * -> 间隙填默认色 span -> 命中按 scope 过滤（不命中者退默认色） + keep_marks 剥首尾
 * -> HTML 转义 + span 包裹。
 */
(function (global) {
  'use strict';

  const SCOPE_AI = 'ai';
  const SCOPE_USER = 'user';
  const SCOPE_ALL = 'all';

  const renderState = {
    rules: [],            // [{name,pattern,color,italic,enabled,priority,scope,keep_marks}]
    aiDefault: '#9aa5ce',
    userDefault: '#9aa5ce',
    mode: 'markup',       // markup | markdown | auto
    compiled: null,       // 合并正则 + groupToRule，复刻 markup.py _rebuild_compiled
    autoMdRe: /^(\s*)(#{1,6}\s|[-*+]\s|>\s|```|---|\|)/m, // 块级 md 检测
  };

  /* ============ 加载规则 ============ */
  // [!] 返回 boolean 表示是否成功：_checkVersion 据此决定是否推进 _lastVersion 基线，
  //     避免 loadRules 静默失败仍推 baseline -> mobile 长期用旧规则直到下次 PC 端变更。
  async function loadRules() {
    try {
      const r = await fetch('/m/api/render_rules');
      if (!r.ok) return false;
      const j = await r.json();
      renderState.rules = j.rules || [];
      renderState.aiDefault = j.ai_default_color || '#9aa5ce';
      renderState.userDefault = j.user_default_color || '#9aa5ce';
      renderState.mode = j.render_mode || 'markup';
      _rebuildCompiled();
      return true;
    } catch (e) {
      console.warn('load render rules failed:', e);
      return false;
    }
  }

  function _rebuildCompiled() {
    // 复刻 markup.py._rebuild_compiled：过滤 enabled & pattern -> 按 priority 升序
    // -> 每条包成命名分组拼接联合正则 -> groupToRule 映射。
    const enabled = renderState.rules.filter((r) => r.enabled && r.pattern);
    enabled.sort((a, b) => (a.priority ?? 100) - (b.priority ?? 100));
    if (!enabled.length) { renderState.compiled = null; return; }
    const parts = [];
    const groupToRule = {};
    for (let i = 0; i < enabled.length; i++) {
      const gname = `r${i}`;
      groupToRule[gname] = enabled[i];
      // 命名分组：JS 支持 (?P<name>...) 在 ES2018+ 不行，用 (?:...) 不行——
      // JS 正则没有命名捕获到 group 名的回查能力（match 返回 groups 对象，ES2018 起支持）。
      // 改用 (?<r0>...) 命名捕获 + match.groups 读取。所有现代浏览器都支持。
      try {
        new RegExp(enabled[i].pattern);
      } catch (e) {
        // 单条规则编译失败跳过（与 markup.py 一致）
        continue;
      }
      parts.push(`(?<${gname}>${enabled[i].pattern})`);
    }
    if (!parts.length) { renderState.compiled = null; return; }
    try {
      const merged = new RegExp(parts.join('|'), 'g');
      renderState.compiled = { merged, groupToRule };
    } catch (e) {
      // 联合编译失败（命名分组冲突等），退回逐规则渲染
      renderState.compiled = null;
    }
  }

  /* ============ 着色辅助 ============ */
  function _escHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _defaultColor(isUser) {
    return isUser ? renderState.userDefault : renderState.aiDefault;
  }

  function _span(text, color, italic) {
    const safeColor = (color || '#c0caf5');
    let style = `color:${safeColor};`;
    if (italic) style += 'font-style:italic;';
    return `<span style="${style}">${text}</span>`;
  }

  // scope 命中判定：ai 气泡命 scope=all/ai；user 气泡命 scope=all/user
  function _scopeHit(rule, isUser) {
    if (!rule) return false;
    const sc = rule.scope || SCOPE_ALL;
    if (sc === SCOPE_ALL) return true;
    return isUser ? (sc === SCOPE_USER) : (sc === SCOPE_AI);
  }

  // keep_marks 处理：False 时剥掉首尾各 1 字符（动作 *旁白* 用）
  function _innerText(text, keepMarks) {
    if (keepMarks) return text;
    if (text.length >= 2) return text.slice(1, -1);
    return text;
  }

  // 单行渲染（复刻 markup.py._line_to_html 主路径）
  function _renderLine(line, isUser) {
    if (line === '') return '<br>';
    const cmp = renderState.compiled;
    if (!cmp) return _renderLineByRules(line, isUser);
    const defaultCol = _defaultColor(isUser);
    let out = '';
    let last = 0;
    cmp.merged.lastIndex = 0;
    let m;
    while ((m = cmp.merged.exec(line)) !== null) {
      // 命中区间
      const start = m.index;
      const end = start + m[0].length;
      // 间隙填默认色
      if (start > last) {
        out += _span(_escHtml(line.slice(last, start)), defaultCol, false);
      }
      // 找命中的命名分组
      const groups = m.groups || {};
      let hitRule = null;
      let hitText = m[0];
      for (const gname in groups) {
        if (groups[gname] != null) {
          hitRule = cmp.groupToRule[gname];
          hitText = groups[gname];
          break;
        }
      }
      if (_scopeHit(hitRule, isUser)) {
        const inner = _innerText(hitText, hitRule.keep_marks);
        out += _span(_escHtml(inner), hitRule.color, hitRule.italic);
      } else {
        // scope 不命中 -> 整段退回默认色（与 markup.py:274 一致，不丢文本）
        out += _span(_escHtml(hitText), defaultCol, false);
      }
      last = end;
      // 防零宽匹配死循环
      if (m[0].length === 0) cmp.merged.lastIndex++;
    }
    // 末尾间隙
    if (last < line.length) {
      out += _span(_escHtml(line.slice(last)), defaultCol, false);
    }
    return out;
  }

  // 兜底逐规则渲染（合并正则失败时用，复刻 markup.py._render_line_by_rules）
  function _renderLineByRules(line, isUser) {
    if (line === '') return '<br>';
    const defaultCol = _defaultColor(isUser);
    const enabled = renderState.rules.filter((r) => r.enabled && r.pattern);
    enabled.sort((a, b) => (a.priority ?? 100) - (b.priority ?? 100));
    const ranges = [];
    for (const r of enabled) {
      let re;
      try { re = new RegExp(r.pattern, 'g'); } catch { continue; }
      let m;
      while ((m = re.exec(line)) !== null) {
        ranges.push({ start: m.index, end: m.index + m[0].length, rule: r, text: m[0] });
        if (m[0].length === 0) re.lastIndex++;
      }
    }
    // 重叠保留先出现的（priority 升序，小先取）
    ranges.sort((a, b) => a.start - b.start);
    const kept = [];
    let lastEnd = -1;
    for (const rg of ranges) {
      if (rg.start >= lastEnd) { kept.push(rg); lastEnd = rg.end; }
    }
    let out = '';
    let pos = 0;
    for (const rg of kept) {
      if (rg.start > pos) out += _span(_escHtml(line.slice(pos, rg.start)), defaultCol, false);
      if (_scopeHit(rg.rule, isUser)) {
        const inner = _innerText(rg.text, rg.rule.keep_marks);
        out += _span(_escHtml(inner), rg.rule.color, rg.rule.italic);
      } else {
        out += _span(_escHtml(rg.text), defaultCol, false);
      }
      pos = rg.end;
    }
    if (pos < line.length) out += _span(_escHtml(line.slice(pos)), defaultCol, false);
    return out;
  }

  function _renderMarkup(text, isUser) {
    if (!text) return '';
    // 复刻 markup.py.render：按 \n 分行，每行 _renderLine
    const lines = text.split('\n');
    return lines.map((ln) => _renderLine(ln, isUser)).join('');
  }

  /* ============ markdown 路径 ============ */
  function _looksLikeMarkdown(text) {
    if (!text) return false;
    // 复刻 markup.py._AUTO_MD_RE（只检测块级，不检行内 *斜体*）
    const lines = text.split('\n');
    for (const ln of lines) {
      if (renderState.autoMdRe.test(ln)) return true;
    }
    return false;
  }

  // markdown 路径：marked 结构渲染 -> 对生成的 HTML 文本节点跑规则着色
  // 简化：marked 输出的 HTML 已转义，再二次着色需要解析 DOM。这里采用稳妥方案——
  // 先 marked.parse 文本 -> 临时 div -> 遍历叶子文本节点 -> 包裹 span -> 序列化回 HTML。
  function _renderMarkdown(text, isUser) {
    if (!text) return '';
    if (typeof marked === 'undefined' || !marked.parse) return _renderMarkup(text, isUser);
    let html;
    try {
      marked.setOptions({ gfm: true, breaks: true, headerIds: false });
      html = marked.parse(text);
    } catch {
      return _renderMarkup(text, isUser);
    }
    // 二次着色：解析到临时 DOM，对每个文本节点（非代码块内）着色
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    _colorizeNode(tmp, isUser);
    return tmp.innerHTML;
  }

  // 遍历 DOM 节点对文本节点着色：复刻 markup.py._colorize_document_with_rules
  function _colorizeNode(node, isUser) {
    // 不着色 <code> <pre> 内代码（保持代码字面）
    const skipTags = new Set(['CODE', 'PRE', 'SCRIPT', 'STYLE']);
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, {
      acceptNode(t) {
        const parent = t.parentElement;
        if (parent && skipTags.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
        if (!t.nodeValue || !t.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        // 跳过已是 span 内的文本节点（避免重复着色嵌套）
        if (parent && parent.tagName === 'SPAN' && parent.style.color) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const textNodes = [];
    let n;
    while ((n = walker.nextNode())) textNodes.push(n);
    for (const tn of textNodes) {
      const html = _renderMarkup(tn.nodeValue, isUser);
      if (html && html !== tn.nodeValue) {
        const span = document.createElement('span');
        span.innerHTML = html;
        tn.replaceWith(...span.childNodes);
      }
    }
  }

  /* ============ 对外入口 ============ */
  // render(content, isUser, serverRenderedHtml)
  //   优先用服务端 rendered_html（markup 模式 PC 端算好的）；
  //   markdown/auto 模式无视 serverRenderedHtml 走本地 marked 路径（与 PC 端 markdown 模式对齐）。
  function render(content, isUser, serverRenderedHtml) {
    if (!content) return '';
    const mode = renderState.mode || 'markup';
    if (mode === 'markdown') return _renderMarkdown(content, isUser);
    if (mode === 'auto') {
      return _looksLikeMarkdown(content) ? _renderMarkdown(content, isUser) : _renderOrServer(content, isUser, serverRenderedHtml);
    }
    // markup
    return _renderOrServer(content, isUser, serverRenderedHtml);
  }

  function _renderOrServer(content, isUser, serverRenderedHtml) {
    if (serverRenderedHtml) return serverRenderedHtml;
    return _renderMarkup(content, isUser);
  }

  /* ============ 规则热更新轮询 ============
   * 手机端定期拉 /m/api/render_rules/version（轻量，仅 version + mode），版本号变化才
   * 重拉全量 rules 并回调 onChanged。PC 端改完规则保存时 markup 版本号自增，手机端
   * 最迟 15s 内感知 + 页面切回 visible 立即检查。
   * [!] 仅在「无流式生成进行中」时回调重渲当前会话（由 onChanged 调用方判断），避免
   *     清掉流式占位气泡；流式中规则已更新到 renderState，下次自然渲染用新规则。
   */
  let _lastVersion = null;   // null = 尚未建立基线，首次拉只存不触发
  let _pollTimer = null;
  let _visHandler = null;

  async function _checkVersion(onChanged) {
    try {
      const r = await fetch('/m/api/render_rules/version');
      if (!r.ok) return;
      const j = await r.json();
      const v = j.version;
      if (_lastVersion === null) {
        // 首次建立基线：不触发 onChanged（避免刚登录就无谓重渲）。
        // [!] 即使首次也只存版本号不重拉规则（规则在登录时 loadRules 已拉过）。
        _lastVersion = v;
        return;
      }
      if (v !== _lastVersion) {
        // 版本变化：重拉规则，成功才推进基线 + 回调；失败保留旧基线让下次轮询重试
        const ok = await loadRules();
        if (ok) {
          _lastVersion = v;
          if (typeof onChanged === 'function') {
            try { onChanged(); } catch (e) { console.warn('onChanged callback error:', e); }
          }
        }
        // ok=false 时不推进 _lastVersion，下次 _checkVersion 仍会检测到 v 变化重试
      }
    } catch (e) {
      console.warn('check render rules version failed:', e);
    }
  }

  function startPolling(onChanged) {
    // 首次建立版本基线（不触发 onChanged，避免刚登录就无谓重渲）
    _checkVersion(onChanged);
    if (_pollTimer) clearInterval(_pollTimer);
    _pollTimer = setInterval(() => _checkVersion(onChanged), 15000);
    // 页面切回 visible 立即检查（用户从别的标签切回手机端即时刷新）
    if (_visHandler) document.removeEventListener('visibilitychange', _visHandler);
    _visHandler = () => {
      if (document.visibilityState === 'visible') _checkVersion(onChanged);
    };
    document.addEventListener('visibilitychange', _visHandler);
  }

  global.DRRender = {
    loadRules,
    render,
    getMode: () => renderState.mode,
    startPolling,
  };
})(window);