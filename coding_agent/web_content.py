# Remote 模式的 Web 前端 HTML。
# 从 Go 版 internal/remote/web.go 原样复制。

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Coding Agent Remote</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #1a1b26;
  --bg-surface: #24283b;
  --bg-input: #1f2335;
  --text: #c0caf5;
  --text-dim: #565f89;
  --text-bright: #e0e6ff;
  --accent: #bb9af7;
  --accent-dim: #7c6da4;
  --green: #9ece6a;
  --red: #f7768e;
  --yellow: #e0af68;
  --blue: #7aa2f7;
  --border: #3b4261;
  --tool-bg: #1f2335;
  --code-bg: #16161e;
}

html, body { height: 100%; background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', Consolas, monospace; font-size: 14px; }

#app { display: flex; flex-direction: column; height: 100vh; max-width: 960px; margin: 0 auto; }

/* 顶部状态栏 */
#status-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 16px; border-bottom: 1px solid var(--border);
  font-size: 12px; color: var(--text-dim); flex-shrink: 0;
}
#status-bar .brand { color: var(--accent); font-weight: bold; font-size: 14px; }
#status-bar .info { display: flex; gap: 16px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot.connected { background: var(--green); }
.dot.disconnected { background: var(--red); }

/* 消息区域 */
#messages {
  flex: 1; overflow-y: auto; padding: 16px;
  scroll-behavior: smooth;
}
#messages::-webkit-scrollbar { width: 6px; }
#messages::-webkit-scrollbar-track { background: transparent; }
#messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.msg { margin-bottom: 16px; line-height: 1.6; }
.msg-user { }
.msg-user .label { color: var(--accent); font-weight: bold; }
.msg-user .content { color: var(--text-bright); margin-top: 4px; white-space: pre-wrap; }
.msg-assistant .content { color: var(--text); margin-top: 4px; }
.msg-assistant .content p { margin-bottom: 8px; }
.msg-assistant .content code {
  background: var(--code-bg); padding: 2px 6px; border-radius: 4px;
  font-size: 13px;
}
.msg-assistant .content pre {
  background: var(--code-bg); padding: 12px; border-radius: 6px;
  margin: 8px 0; overflow-x: auto; border: 1px solid var(--border);
}
.msg-assistant .content pre code { background: none; padding: 0; }

.msg-system { color: var(--text-dim); font-size: 13px; white-space: pre-wrap; }
.msg-error { color: var(--red); }

/* 工具调用 */
.tool-block {
  background: var(--tool-bg); border: 1px solid var(--border); border-radius: 6px;
  margin: 8px 0; overflow: hidden;
}
.tool-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; user-select: none;
  font-size: 13px;
}
.tool-header:hover { background: rgba(255,255,255,0.03); }
.tool-header .icon { font-size: 12px; color: var(--text-dim); transition: transform 0.2s; }
.tool-header .icon.expanded { transform: rotate(90deg); }
.tool-header .name { color: var(--blue); font-weight: 600; }
.tool-header .status { margin-left: auto; font-size: 12px; }
.tool-header .status.ok { color: var(--green); }
.tool-header .status.err { color: var(--red); }
.tool-header .status.loading { color: var(--yellow); }
.tool-body {
  display: none; padding: 8px 12px; border-top: 1px solid var(--border);
  font-size: 12px; max-height: 300px; overflow-y: auto; white-space: pre-wrap;
  color: var(--text-dim);
}
.tool-body.show { display: block; }

/* 思考过程（折叠） */
.thinking-block {
  background: var(--tool-bg); border: 1px solid var(--border); border-radius: 6px;
  margin: 8px 0; overflow: hidden;
}
.thinking-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer; user-select: none;
  font-size: 13px; color: var(--text-dim);
}
.thinking-header:hover { background: rgba(255,255,255,0.03); }
.thinking-header .icon { font-size: 12px; transition: transform 0.2s; }
.thinking-header .icon.expanded { transform: rotate(90deg); }
.thinking-body {
  display: none; padding: 8px 12px; border-top: 1px solid var(--border);
  font-size: 12px; max-height: 300px; overflow-y: auto; white-space: pre-wrap;
  color: var(--text-dim); font-style: italic;
}
.thinking-body.show { display: block; }

/* 权限弹窗 */
.perm-dialog {
  background: var(--bg-surface); border: 2px solid var(--yellow);
  border-radius: 8px; padding: 16px; margin: 12px 0;
}
.perm-dialog .title { color: var(--yellow); font-weight: bold; margin-bottom: 8px; }
.perm-dialog .desc { color: var(--text); margin-bottom: 12px; font-size: 13px; white-space: pre-wrap; }
.perm-dialog .actions { display: flex; gap: 8px; }
.perm-dialog button {
  padding: 6px 16px; border-radius: 4px; border: 1px solid var(--border);
  cursor: pointer; font-family: inherit; font-size: 13px;
}
.btn-allow { background: var(--green); color: var(--bg); border-color: var(--green); }
.btn-deny { background: transparent; color: var(--red); border-color: var(--red); }
.btn-always { background: transparent; color: var(--blue); border-color: var(--blue); }

/* 斜杠命令菜单 */
#slash-menu {
  display: none; position: absolute; bottom: 100%; left: 0; right: 0;
  background: var(--bg-surface); border: 1px solid var(--border);
  border-radius: 6px; margin-bottom: 4px; max-height: 240px;
  overflow-y: auto; box-shadow: 0 -4px 12px rgba(0,0,0,0.3);
}
#slash-menu.show { display: block; }
.slash-item {
  padding: 8px 12px; cursor: pointer; display: flex; gap: 8px; align-items: baseline;
}
.slash-item:hover, .slash-item.active { background: rgba(187,154,247,0.1); }
.slash-item .cmd-name { color: var(--accent); font-weight: 600; white-space: nowrap; }
.slash-item .cmd-desc { color: var(--text-dim); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 输入区域 */
#input-area {
  flex-shrink: 0; border-top: 1px solid var(--border);
  padding: 12px 16px; display: flex; gap: 8px; align-items: flex-end;
}
#input-area textarea {
  flex: 1; background: var(--bg-input); color: var(--text-bright);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 12px; font-family: inherit; font-size: 14px;
  resize: none; outline: none; min-height: 42px; max-height: 200px;
  line-height: 1.5;
}
#input-area textarea:focus { border-color: var(--accent); }
#input-area textarea::placeholder { color: var(--text-dim); }
#send-btn {
  background: var(--accent); color: var(--bg); border: none;
  border-radius: 6px; padding: 10px 16px; cursor: pointer;
  font-family: inherit; font-size: 14px; font-weight: 600;
  white-space: nowrap;
}
#send-btn:hover { opacity: 0.9; }
#send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* 流式光标闪烁 */
.cursor { animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* 完成指示 */
.done-indicator { color: var(--text-dim); font-size: 12px; margin-top: 4px; }

/* Markdown 列表 */
.msg-assistant .content ul, .msg-assistant .content ol { padding-left: 20px; margin: 8px 0; }
.msg-assistant .content li { margin: 4px 0; }
.msg-assistant .content h1, .msg-assistant .content h2, .msg-assistant .content h3 {
  color: var(--text-bright); margin: 12px 0 8px; }
.msg-assistant .content blockquote {
  border-left: 3px solid var(--accent-dim); padding-left: 12px;
  color: var(--text-dim); margin: 8px 0;
}
.msg-assistant .content table { border-collapse: collapse; margin: 8px 0; }
.msg-assistant .content th, .msg-assistant .content td {
  border: 1px solid var(--border); padding: 6px 12px; text-align: left; }
.msg-assistant .content th { background: var(--bg-surface); }

/* 权限模式下拉 */
.perm-label { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-dim); margin-left: 8px; }
#perm-select {
  background: var(--bg-input); color: var(--text); border: 1px solid var(--border);
  border-radius: 4px; padding: 2px 6px; font-size: 12px; outline: none; cursor: pointer;
}
#perm-select:focus { border-color: var(--accent); }

/* 测评控件（柔和胶囊风格） */
.eval-control { display: inline-flex; align-items: center; gap: 6px; margin-left: 10px; }
#eval-task-select {
  background: var(--bg-input); color: var(--text); border: 1px solid var(--border);
  border-radius: 999px; padding: 4px 12px; font-size: 12px; outline: none; cursor: pointer;
  max-width: 160px;
}
#eval-task-select:focus { border-color: var(--accent); }
#eval-btn {
  background: var(--accent); color: #fff; border: none; border-radius: 999px;
  padding: 4px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
  transition: opacity .2s;
}
#eval-btn:hover { opacity: .85; }
#eval-btn:disabled { opacity: .4; cursor: not-allowed; }

/* 评测面板（居中弹窗） */
#eval-panel {
  position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: 560px; max-width: 90vw; max-height: 70vh;
  background: var(--bg-surface); border: 1px solid var(--border); border-radius: 14px;
  display: flex; flex-direction: column; box-shadow: 0 12px 40px rgba(0,0,0,.45);
  z-index: 1000; overflow: hidden;
}
/* 居中弹窗遮罩 */
#eval-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,.45); z-index: 999; display: none;
}
.eval-head {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); background: var(--bg-surface);
}
.eval-head #eval-title { font-weight: 600; font-size: 13px; flex: 1; }
.eval-status { font-size: 11px; padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.eval-status.running { background: rgba(59,130,246,.15); color: #3b82f6; }
.eval-status.pass { background: rgba(16,185,129,.15); color: #10b981; }
.eval-status.fail { background: rgba(239,68,68,.15); color: #ef4444; }
#eval-close { background: none; border: none; color: var(--text-dim); font-size: 14px; cursor: pointer; }
#eval-close:hover { color: var(--text); }
#eval-body { padding: 12px 14px; overflow-y: auto; font-size: 13px; line-height: 1.6; }
.eval-task-meta { color: var(--text-dim); font-size: 12px; margin-bottom: 8px; }
.eval-step { margin-bottom: 8px; }
.eval-step .step-label { font-weight: 600; color: var(--accent); }
.eval-result { margin-top: 10px; padding: 10px 12px; border-radius: 8px; font-weight: 600; }
.eval-result.pass { background: rgba(16,185,129,.12); color: #10b981; }
.eval-result.fail { background: rgba(239,68,68,.12); color: #ef4444; }
.eval-text { white-space: pre-wrap; color: var(--text); margin: 2px 0; }
.eval-tool { color: var(--text-dim); font-size: 12px; margin: 2px 0; }
</style>
</head>
<body>
<div id="app">
  <div id="status-bar">
    <span class="brand">⚡ Coding Agent 远程控制</span>
    <div class="info">
      <span id="conn-status"><span class="dot disconnected"></span>连接中...</span>
      <span id="token-info"></span>
      <label class="perm-label" title="权限模式">🔒
        <select id="perm-select">
          <option value="default">默认模式</option>
          <option value="acceptEdits">自动接受编辑</option>
          <option value="plan">计划模式</option>
          <option value="bypassPermissions">跳过权限检查</option>
        </select>
      </label>
      <div class="eval-control" title="自进化评测：驱动 Agent 真实完成一个编码任务并打分">
        <select id="eval-task-select">
          <option value="">-- 选择评测任务 --</option>
          <option value="fibonacci">斐波那契数列</option>
          <option value="is_prime">质数判断</option>
          <option value="reverse_string">字符串反转</option>
          <option value="factorial">阶乘计算</option>
          <option value="max_of_three">三数最大值</option>
          <option value="merge_dicts">字典合并</option>
          <option value="count_words">单词计数</option>
          <option value="two_file_project">多文件项目</option>
        </select>
        <button id="eval-btn">🎯 测评</button>
      </div>
    </div>
  </div>
  <div id="messages"></div>
  <div id="eval-overlay"></div>
  <div id="eval-panel" style="display:none;">
    <div class="eval-head">
      <span id="eval-title">评测进行中...</span>
      <span id="eval-status" class="eval-status running">运行中</span>
      <button id="eval-close">✕</button>
    </div>
    <div id="eval-body"></div>
  </div>
  <div id="input-area" style="position:relative;">
    <div id="slash-menu"></div>
    <textarea id="input" placeholder="输入消息... (Enter 发送，Shift+Enter 换行)" rows="1"></textarea>
    <button id="send-btn">发送</button>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const connStatus = document.getElementById('conn-status');
const tokenInfo = document.getElementById('token-info');
const slashMenu = document.getElementById('slash-menu');
const permSelect = document.getElementById('perm-select');
const evalTaskSelect = document.getElementById('eval-task-select');
const evalBtn = document.getElementById('eval-btn');
const evalPanel = document.getElementById('eval-panel');
const evalTitle = document.getElementById('eval-title');
const evalStatus = document.getElementById('eval-status');
const evalBody = document.getElementById('eval-body');
const evalClose = document.getElementById('eval-close');
const evalOverlay = document.getElementById('eval-overlay');

let ws = null;
let streaming = false;
let allCommands = [];
let slashCursor = 0;
let slashFiltered = [];
let currentAssistantEl = null;
let currentStreamText = '';
let currentThinkingEl = null;
let currentThinkingText = '';
let autoScroll = true;
let pingTimer = null;
let connectedOnce = false;
let evalActive = false;

// Markdown 渲染配置
if (typeof marked !== 'undefined') {
  marked.setOptions({ breaks: true, gfm: true });
}

function renderMarkdown(text) {
  if (typeof marked !== 'undefined') {
    try { return marked.parse(text); } catch(e) {}
  }
  return escapeHtml(text);
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function connect() {
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  if (ws) { try { ws.onclose = null; ws.close(); } catch(e) {} ws = null; }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');

  ws.onopen = () => {
    connStatus.innerHTML = '<span class="dot connected"></span>已连接';
    // 每 10 秒发一次应用层 ping，防止连接被中间件/浏览器回收
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping', data: {} }));
      }
    }, 10000);
  };

  ws.onclose = () => {
    connStatus.innerHTML = '<span class="dot disconnected"></span>重连中...';
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    setTimeout(connect, 3000);
  };

  ws.onerror = () => {};

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    handleMessage(msg);
  };
}

// ---------- 自进化评测 ----------

function startEval() {
  const task = evalTaskSelect.value;
  if (!task) { alert('请先选择一个评测任务'); return; }
  evalPanel.style.display = 'flex';
  evalOverlay.style.display = 'block';
  evalTitle.textContent = '评测进行中...';
  evalStatus.className = 'eval-status running';
  evalStatus.textContent = '运行中';
  evalBody.innerHTML = '<div class="eval-task-meta">正在驱动 Agent 执行该任务...</div>';
  evalBtn.disabled = true;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'evaluate', data: { task } }));
  }
}

function closeEval() {
  evalPanel.style.display = 'none';
  evalOverlay.style.display = 'none';
  evalBody.innerHTML = '';
  evalBtn.disabled = false;
}

function renderEvalStream(text) {
  const last = evalBody.lastElementChild;
  if (last && last.classList.contains('eval-text')) {
    last.textContent += text;
  } else {
    const div = document.createElement('div');
    div.className = 'eval-text';
    div.textContent = text;
    evalBody.appendChild(div);
  }
  evalBody.scrollTop = evalBody.scrollHeight;
}

function renderEvalTool(name, args) {
  const div = document.createElement('div');
  div.className = 'eval-tool';
  div.textContent = '⚙️ ' + name + (args ? ' ' + JSON.stringify(args) : '');
  evalBody.appendChild(div);
  evalBody.scrollTop = evalBody.scrollHeight;
}

function renderEvalMeta(data) {
  const div = document.createElement('div');
  div.className = 'eval-task-meta';
  const domainZh = { 'coding': '编码', 'debugging': '调试', 'refactor': '重构', 'testing': '测试', 'file-io': '文件操作', 'multi-file': '多文件', 'data-processing': '数据处理' };
  div.textContent = '任务: ' + (data.label || data.task) + ' | 难度: ' + data.difficulty + ' | 领域: ' + (domainZh[data.domain] || data.domain);
  return div;
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'eval_start':
      evalActive = true;
      evalTitle.textContent = '评测: ' + (msg.data.label || msg.data.task);
      evalStatus.className = 'eval-status running';
      evalStatus.textContent = '运行中';
      evalBody.innerHTML = '';
      evalBody.appendChild(renderEvalMeta(msg.data));
      break;
    case 'eval_result':
      evalActive = false;
      evalBtn.disabled = false;
      const ok = msg.data.ok;
      evalStatus.className = 'eval-status ' + (ok ? 'pass' : 'fail');
      evalStatus.textContent = ok ? '通过' : '失败';
      const res = document.createElement('div');
      res.className = 'eval-result ' + (ok ? 'pass' : 'fail');
      res.textContent = (ok ? '✅ 通过' : '❌ 失败') + '  ' + msg.data.detail + '（耗时 ' + msg.data.elapsed + 's）';
      evalBody.appendChild(res);
      evalBody.scrollTop = evalBody.scrollHeight;
      break;
    case 'permission_mode_changed':
      if (permSelect && msg.data && msg.data.mode) permSelect.value = msg.data.mode;
      break;
    case 'connected':
      if (!connectedOnce) {
        addSystem('会话: ' + msg.data.session + ' | 工作目录: ' + msg.data.cwd);
        connectedOnce = true;
      }
      break;

    case 'commands':
      allCommands = msg.data || [];
      break;

    case 'system':
      addSystem(msg.data.message);
      break;

    case 'clear':
      messagesEl.innerHTML = '';
      addSystem('对话已清空。');
      break;

    case 'command_done':
      streaming = false;
      updateUI();
      break;

    case 'replay_user':
      addUser(msg.data.content);
      break;

    case 'replay_assistant': {
      const el = addAssistant('');
      updateAssistant(el, msg.data.content, false);
      break;
    }

    case 'stream_text':
      if (evalActive) { renderEvalStream(msg.data.text); break; }
      if (currentThinkingEl) {
        currentThinkingEl.querySelector('.thinking-header span:last-child').textContent = '💭 思考';
        currentThinkingEl = null;
        currentThinkingText = '';
      }
      if (!currentAssistantEl) {
        currentAssistantEl = addAssistant('');
        currentStreamText = '';
      }
      currentStreamText += msg.data.text;
      updateAssistant(currentAssistantEl, currentStreamText, true);
      break;

    case 'stream_end':
      if (currentAssistantEl) {
        updateAssistant(currentAssistantEl, currentStreamText, false);
      }
      currentAssistantEl = null;
      currentStreamText = '';
      break;

    case 'thinking_text':
      if (!currentThinkingEl) {
        currentThinkingEl = addThinking();
        currentThinkingText = '';
      }
      currentThinkingText += msg.data.text;
      currentThinkingEl.querySelector('.thinking-body').textContent = currentThinkingText;
      break;

    case 'tool_use':
      if (evalActive) { renderEvalTool(msg.data.toolName, msg.data.args); break; }
      if (currentThinkingEl) {
        currentThinkingEl.querySelector('.thinking-header span:last-child').textContent = '💭 思考';
        currentThinkingEl = null;
        currentThinkingText = '';
      }
      addToolUse(msg.data);
      break;

    case 'tool_result':
      if (evalActive) { renderEvalTool('↳ ' + msg.data.toolName, msg.data.isError ? '⚠ error' : '✓ done'); break; }
      updateToolResult(msg.data);
      break;

    case 'permission_request':
      addPermissionDialog(msg.data);
      break;

    case 'ask_user':
      addAskUserDialog(msg.data);
      break;

    case 'turn_complete':
      break;

    case 'loop_complete':
      if (currentAssistantEl) {
        updateAssistant(currentAssistantEl, currentStreamText, false);
        currentAssistantEl = null;
        currentStreamText = '';
      }
      currentThinkingEl = null;
      currentThinkingText = '';
      const el = document.createElement('div');
      el.className = 'done-indicator';
      el.textContent = '✻ Done in ' + msg.data.elapsed.toFixed(1) + 's';
      messagesEl.appendChild(el);
      streaming = false;
      updateUI();
      scrollToBottom();
      break;

    case 'usage':
      tokenInfo.textContent = 'In: ' + formatTokens(msg.data.inputTokens) + ' | Out: ' + formatTokens(msg.data.outputTokens) + ' tokens';
      break;

    case 'error':
      addError(msg.data.message);
      streaming = false;
      updateUI();
      break;

    case 'compact':
      addSystem('⟳ ' + msg.data.message);
      break;

    case 'retry':
      addSystem('↻ 重试中: ' + msg.data.reason);
      break;
  }
}

function formatTokens(n) {
  if (n > 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n > 1000) return (n/1000).toFixed(1) + 'K';
  return '' + n;
}

function addUser(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-user';
  div.innerHTML = '<span class="label">❯ </span><span class="content">' + escapeHtml(text) + '</span>';
  messagesEl.appendChild(div);
  scrollToBottom();
}

function addAssistant(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-assistant';
  div.innerHTML = '<div class="content"></div>';
  messagesEl.appendChild(div);
  scrollToBottom();
  return div;
}

function updateAssistant(el, text, isStreaming) {
  const contentEl = el.querySelector('.content');
  // 检测 <think>...</think> 标签，分离思考内容和正文
  const thinkMatch = text.match(/^<think>([\s\S]*?)<\/think>\s*([\s\S]*)$/);
  if (thinkMatch) {
    const thinkText = thinkMatch[1].trim();
    const bodyText = thinkMatch[2].trim();
    let html = '';
    if (thinkText) {
      html += '<div class="thinking-block">' +
        '<div class="thinking-header" onclick="toggleThinking(this)">' +
          '<span class="icon">▶</span>' +
          '<span>💭 Thought</span>' +
        '</div>' +
        '<div class="thinking-body">' + escapeHtml(thinkText) + '</div>' +
      '</div>';
    }
    if (bodyText) {
      html += renderMarkdown(bodyText);
    }
    if (isStreaming) html += '<span class="cursor">▎</span>';
    contentEl.innerHTML = html;
  } else if (text.startsWith('<think>') && isStreaming) {
    // 思考还没结束，显示为折叠的 thinking 块 + 光标
    const thinkBody = text.replace(/^<think>\s*/, '');
    let html = '<div class="thinking-block">' +
      '<div class="thinking-header" onclick="toggleThinking(this)">' +
        '<span class="icon">▶</span>' +
        '<span>💭 思考中...</span>' +
      '</div>' +
      '<div class="thinking-body">' + escapeHtml(thinkBody) + '</div>' +
    '</div>';
    html += '<span class="cursor">▎</span>';
    contentEl.innerHTML = html;
  } else {
    let html = renderMarkdown(text);
    if (isStreaming) html += '<span class="cursor">▎</span>';
    contentEl.innerHTML = html;
  }
  scrollToBottom();
}

function addThinking() {
  const div = document.createElement('div');
  div.className = 'thinking-block';
  div.innerHTML =
    '<div class="thinking-header" onclick="toggleThinking(this)">' +
      '<span class="icon">▶</span>' +
      '<span>💭 思考中...</span>' +
    '</div>' +
    '<div class="thinking-body"></div>';
  messagesEl.appendChild(div);
  scrollToBottom();
  return div;
}

function toggleThinking(header) {
  header.querySelector('.icon').classList.toggle('expanded');
  header.nextElementSibling.classList.toggle('show');
}

function addSystem(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-system';
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function addError(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-error';
  div.textContent = '✖ ' + text;
  messagesEl.appendChild(div);
  scrollToBottom();
}

// 工具调用
const toolElements = {};

function addToolUse(data) {
  const div = document.createElement('div');
  div.className = 'tool-block';

  const argsStr = data.args ? JSON.stringify(data.args, null, 2) : '';
  let argsPreview = '';
  if (data.args) {
    if (data.args.command) argsPreview = data.args.command;
    else if (data.args.file_path) argsPreview = data.args.file_path;
    else if (data.args.pattern) argsPreview = data.args.pattern;
    else if (data.args.path) argsPreview = data.args.path;
  }

  div.innerHTML =
    '<div class="tool-header" onclick="toggleTool(this)">' +
      '<span class="icon">▶</span>' +
      '<span class="name">' + escapeHtml(data.toolName) + '</span>' +
      (argsPreview ? '<span style="color:var(--text-dim);font-size:12px;margin-left:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:500px;">' + escapeHtml(argsPreview) + '</span>' : '') +
      '<span class="status loading">⏳ 运行中...</span>' +
    '</div>' +
    '<div class="tool-body">' +
      (argsStr ? '<div style="color:var(--blue);margin-bottom:8px;">参数:\n' + escapeHtml(argsStr) + '</div>' : '') +
      '<div class="tool-output"></div>' +
    '</div>';

  messagesEl.appendChild(div);
  toolElements[data.toolName + '_' + (data.toolId || '')] = div;
  scrollToBottom();
}

function updateToolResult(data) {
  const key = data.toolName + '_' + (data.toolId || '');
  const div = toolElements[key];
  if (div) {
    const statusEl = div.querySelector('.status');
    if (data.isError) {
      statusEl.className = 'status err';
      statusEl.textContent = '✗ ' + data.elapsed.toFixed(1) + 's';
    } else {
      statusEl.className = 'status ok';
      statusEl.textContent = '✓ ' + data.elapsed.toFixed(1) + 's';
    }
    const outputEl = div.querySelector('.tool-output');
    if (data.output) {
      const truncated = data.output.length > 5000 ? data.output.substring(0, 5000) + '\n... (已截断)' : data.output;
      outputEl.textContent = truncated;
    }
    delete toolElements[key];
  } else {
    // 如果没有匹配到，直接创建一个完成的 tool block
    const block = document.createElement('div');
    block.className = 'tool-block';
    const status = data.isError ? '✗' : '✓';
    const statusClass = data.isError ? 'err' : 'ok';
    block.innerHTML =
      '<div class="tool-header" onclick="toggleTool(this)">' +
        '<span class="icon">▶</span>' +
        '<span class="name">' + escapeHtml(data.toolName) + '</span>' +
        '<span class="status ' + statusClass + '">' + status + ' ' + data.elapsed.toFixed(1) + 's</span>' +
      '</div>' +
      '<div class="tool-body">' + escapeHtml(data.output || '') + '</div>';
    messagesEl.appendChild(block);
  }
  scrollToBottom();
}

function toggleTool(header) {
  const icon = header.querySelector('.icon');
  const body = header.nextElementSibling;
  icon.classList.toggle('expanded');
  body.classList.toggle('show');
}

// 权限弹窗
function addPermissionDialog(data) {
  const div = document.createElement('div');
  div.className = 'perm-dialog';
  div.id = 'perm-' + data.id;
  div.innerHTML =
    '<div class="title">🔒 需要权限: ' + escapeHtml(data.toolName) + '</div>' +
    '<div class="desc">' + escapeHtml(data.description) + '</div>' +
    '<div class="actions">' +
      '<button class="btn-allow" onclick="respondPerm(\'' + data.id + '\', \'allow\')">允许</button>' +
      '<button class="btn-always" onclick="respondPerm(\'' + data.id + '\', \'allowAlways\')">始终允许</button>' +
      '<button class="btn-deny" onclick="respondPerm(\'' + data.id + '\', \'deny\')">拒绝</button>' +
    '</div>';
  messagesEl.appendChild(div);
  scrollToBottom();
}

function respondPerm(id, response) {
  ws.send(JSON.stringify({ type: 'permission_response', data: { id, response } }));
  const el = document.getElementById('perm-' + id);
  if (el) {
    const label = { allow: '允许', allowAlways: '始终允许', deny: '拒绝' }[response] || response;
    el.innerHTML = '<div style="color:var(--text-dim)">🔒 权限: ' + label + '</div>';
  }
}

// Ask User 弹窗
function addAskUserDialog(data) {
  const div = document.createElement('div');
  div.className = 'perm-dialog';
  div.id = 'ask-' + data.id;

  let html = '<div class="title">❓ 问题</div>';
  const questions = data.questions || [];
  questions.forEach((q, qi) => {
    html += '<div style="margin-bottom:12px;">';
    html += '<div style="margin-bottom:6px;color:var(--text-bright);">' + escapeHtml(q.question || q.Text || '') + '</div>';
    const options = q.options || q.Options || [];
    options.forEach((opt, oi) => {
      const label = opt.label || opt.Label || '';
      const desc = opt.description || opt.Description || '';
      html += '<label style="display:block;margin:4px 0;cursor:pointer;">' +
        '<input type="radio" name="ask_' + data.id + '_' + qi + '" value="' + escapeHtml(label) + '"> ' +
        '<span style="color:var(--blue)">' + escapeHtml(label) + '</span>' +
        (desc ? ' <span style="color:var(--text-dim);font-size:12px;">— ' + escapeHtml(desc) + '</span>' : '') +
        '</label>';
    });
    // Other 选项
    html += '<label style="display:block;margin:4px 0;cursor:pointer;">' +
      '<input type="radio" name="ask_' + data.id + '_' + qi + '" value="__other__"> ' +
      '<span style="color:var(--text-dim)">其他: </span>' +
      '<input type="text" id="ask_other_' + data.id + '_' + qi + '" style="background:var(--bg-input);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-family:inherit;font-size:13px;width:300px;">' +
      '</label>';
    html += '</div>';
  });
  html += '<div class="actions"><button class="btn-allow" onclick="respondAsk(\'' + data.id + '\',' + questions.length + ')">提交</button></div>';

  div.innerHTML = html;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function respondAsk(id, qCount) {
  const answers = {};
  for (let i = 0; i < qCount; i++) {
    const radios = document.querySelectorAll('input[name="ask_' + id + '_' + i + '"]');
    let val = '';
    radios.forEach(r => {
      if (r.checked) {
        if (r.value === '__other__') {
          val = document.getElementById('ask_other_' + id + '_' + i).value;
        } else {
          val = r.value;
        }
      }
    });
    answers['question_' + i] = val;
  }
  ws.send(JSON.stringify({ type: 'ask_user_response', data: { id, answers } }));
  const el = document.getElementById('ask-' + id);
  if (el) {
    el.innerHTML = '<div style="color:var(--text-dim)">✓ Answered</div>';
  }
}

// 滚动控制
messagesEl.addEventListener('scroll', () => {
  const { scrollTop, scrollHeight, clientHeight } = messagesEl;
  autoScroll = scrollHeight - scrollTop - clientHeight < 60;
});

function scrollToBottom() {
  if (autoScroll) {
    requestAnimationFrame(() => {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    });
  }
}

// 输入处理
function sendMessage() {
  if (slashMenu.classList.contains('show')) { hideSlashMenu(); }
  const text = inputEl.value.trim();
  if (!text || streaming) return;
  addUser(text);
  ws.send(JSON.stringify({ type: 'user_message', data: { content: text } }));
  inputEl.value = '';
  inputEl.style.height = 'auto';
  streaming = true;
  updateUI();
}

function updateUI() {
  sendBtn.disabled = streaming;
  inputEl.disabled = streaming;
  if (!streaming) inputEl.focus();
}

// ── 斜杠命令菜单 ──

function showSlashMenu(filter) {
  const prefix = filter.toLowerCase();
  slashFiltered = allCommands.filter(c => c.name.startsWith(prefix));
  if (slashFiltered.length === 0) { hideSlashMenu(); return; }
  slashCursor = 0;
  renderSlashMenu();
  slashMenu.classList.add('show');
}

function hideSlashMenu() {
  slashMenu.classList.remove('show');
  slashFiltered = [];
  slashCursor = 0;
}

function renderSlashMenu() {
  slashMenu.innerHTML = slashFiltered.map((c, i) =>
    '<div class="slash-item' + (i === slashCursor ? ' active' : '') + '" data-idx="' + i + '">' +
      '<span class="cmd-name">/' + escapeHtml(c.name) + '</span>' +
      '<span class="cmd-desc">' + escapeHtml(c.description) + '</span>' +
    '</div>'
  ).join('');
  // 点击选中
  slashMenu.querySelectorAll('.slash-item').forEach(el => {
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const idx = parseInt(el.dataset.idx);
      selectSlashItem(idx);
    });
  });
  // 滚动到当前高亮项
  const activeEl = slashMenu.querySelector('.active');
  if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

function selectSlashItem(idx) {
  const cmd = slashFiltered[idx];
  if (!cmd) return;
  inputEl.value = '/' + cmd.name + ' ';
  hideSlashMenu();
  inputEl.focus();
}

inputEl.addEventListener('keydown', (e) => {
  if (slashMenu.classList.contains('show')) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      slashCursor = Math.min(slashCursor + 1, slashFiltered.length - 1);
      renderSlashMenu();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      slashCursor = Math.max(slashCursor - 1, 0);
      renderSlashMenu();
      return;
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault();
      selectSlashItem(slashCursor);
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      hideSlashMenu();
      return;
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';

  const val = inputEl.value;
  // 检测是否在输入斜杠命令
  if (val.startsWith('/') && !val.includes('\n')) {
    const prefix = val.substring(1).split(' ')[0];
    if (!val.includes(' ')) {
      showSlashMenu(prefix);
    } else {
      hideSlashMenu();
    }
  } else {
    hideSlashMenu();
  }
});

sendBtn.addEventListener('click', sendMessage);

// 权限模式切换
permSelect.addEventListener('change', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'permission_mode', data: { mode: permSelect.value } }));
  }
});

// 测评按钮
evalBtn.addEventListener('click', startEval);
evalClose.addEventListener('click', closeEval);

// 启动
connect();
inputEl.focus();
</script>
</body>
</html>"""
