import { defineConfig, type Plugin } from 'vite';
import { apiPlugin } from './dev-api';

function inputBar(): Plugin {
  return {
    name: 'input-bar',
    apply: 'serve',
    transformIndexHtml(html) {
      const barHtml = `
<!-- Input bar: Send (direct) + TTS (simulated glasses flow) -->
<div id="input-bar" style="
  position: fixed; bottom: 0; left: 0; right: 0;
  display: flex; gap: 8px; padding: 12px 16px;
  background: #1a1a2e; border-top: 1px solid #333;
  z-index: 9999; font-family: system-ui, sans-serif;
">
  <input id="input-text" type="text" placeholder="Type your message..."
    autocomplete="off" style="
    flex: 1; padding: 10px 14px; font-size: 15px;
    border: 1px solid #555; border-radius: 6px;
    background: #0f0f1a; color: #e0e0e0; outline: none;
  " />
  <button id="btn-send" title="Send directly to OpenClaw" style="
    padding: 10px 18px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #3fb950; color: #fff; cursor: pointer;
  ">Send</button>
  <button id="btn-tts" title="TTS → Whisper → Confirm (simulates glasses)" style="
    padding: 10px 14px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #6e40c9; color: #fff; cursor: pointer;
  ">TTS</button>
  <button id="btn-confirm" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #27ae60; color: #fff; cursor: pointer;
    display: none;
  ">Confirm</button>
  <button id="btn-reject" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #95a5a6; color: #fff; cursor: pointer;
    display: none;
  ">Reject</button>
  <button id="btn-cancel" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #e67e22; color: #fff; cursor: pointer;
    display: none;
  ">Cancel</button>
  <span id="input-state" style="
    padding: 10px 8px; font-size: 12px; color: #888;
    min-width: 80px; text-align: center;
  ">--</span>
</div>
<script>
  // Input bar — calls into the app via window.__g2Api
  function updateInputBar() {
    var api = window.__g2Api;
    var state = api ? api.getState() : 'loading';
    var stateEl = document.getElementById('input-state');
    if (stateEl) stateEl.textContent = state;

    var sendBtn = document.getElementById('btn-send');
    var ttsBtn = document.getElementById('btn-tts');
    var confirmBtn = document.getElementById('btn-confirm');
    var rejectBtn = document.getElementById('btn-reject');
    var cancelBtn = document.getElementById('btn-cancel');
    var textInput = document.getElementById('input-text');

    if (!sendBtn || !ttsBtn) return;

    var canSend = state === 'idle' || state === 'confirming';
    sendBtn.style.display = canSend ? '' : 'none';
    ttsBtn.style.display = state === 'idle' ? '' : 'none';
    if (confirmBtn) confirmBtn.style.display = state === 'confirming' ? '' : 'none';
    if (rejectBtn) rejectBtn.style.display = state === 'confirming' ? '' : 'none';
    if (cancelBtn) cancelBtn.style.display = (state === 'thinking' || state === 'streaming') ? '' : 'none';
    if (textInput) textInput.disabled = !canSend;
  }

  setInterval(updateInputBar, 200);

  document.getElementById('btn-send').addEventListener('click', function() {
    var api = window.__g2Api;
    if (!api) return;
    var input = document.getElementById('input-text');
    var text = input ? input.value.trim() : '';
    if (!text) return;
    if (api.sendText(text)) {
      input.value = '';
    }
  });

  document.getElementById('btn-tts').addEventListener('click', function() {
    var api = window.__g2Api;
    if (!api) return;
    var input = document.getElementById('input-text');
    var text = input ? input.value.trim() : '';
    if (!text) return;
    if (api.ttsRecord(text)) {
      input.value = '';
    }
  });

  document.getElementById('btn-confirm').addEventListener('click', function() {
    if (window.__g2Api) window.__g2Api.confirmTranscription();
  });

  document.getElementById('btn-reject').addEventListener('click', function() {
    if (window.__g2Api) window.__g2Api.rejectTranscription();
  });

  document.getElementById('btn-cancel').addEventListener('click', function() {
    if (window.__g2Api) window.__g2Api.cancelResponse();
  });

  document.getElementById('input-text').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      document.getElementById('btn-send').click();
    }
  });
</script>`;
      return html.replace('</body>', barHtml + '\n</body>');
    },
  };
}

function telemetryPanel(): Plugin {
  return {
    name: 'telemetry-panel',
    apply: 'serve',
    transformIndexHtml(html) {
      // Relax CSP to allow inline scripts (telemetry, input bar, API)
      html = html.replace(
        /script-src\s+'self'/,
        "script-src 'self' 'unsafe-inline'",
      );

      const panelBlock = `
<!-- Telemetry panel -->
<style>
  html, body { background: #0d1117 !important; margin: 0 !important; padding: 0 !important; height: 100% !important; }
  #dev-telemetry {
    position: fixed; top: 0; right: 0; width: 30%; bottom: 54px;
    display: flex; flex-direction: column;
    background: #0d1117; color: #c9d1d9;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 13px; z-index: 9000; overflow: hidden;
    border-left: 1px solid #30363d;
  }
  #dt-status {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 12px; min-height: 32px; box-sizing: border-box;
    background: #161b22; border-bottom: 1px solid #30363d; flex-shrink: 0;
  }
  .dt-badge {
    padding: 2px 8px; border-radius: 3px; font-weight: 700;
    font-size: 11px; text-transform: uppercase;
  }
  .dt-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .dt-meta { font-size: 11px; color: #8b949e; }
  .dt-spacer { flex: 1; }
  .dt-btn {
    padding: 2px 8px; font-size: 10px; background: #21262d; color: #8b949e;
    border: 1px solid #30363d; border-radius: 3px; cursor: pointer; font-family: inherit;
  }
  .dt-btn:hover { background: #30363d; color: #c9d1d9; }
  #dt-log {
    flex: 1; overflow-y: auto; overflow-x: hidden; padding: 2px 0;
  }
  .dt-entry {
    padding: 1px 10px; line-height: 1.5; word-wrap: break-word; overflow-wrap: break-word;
  }
  .dt-ts { color: #484f58; font-size: 11px; }
  .dt-info { color: #58a6ff; }
  .dt-warn { color: #d29922; }
  .dt-error { color: #f85149; }
  #dt-frames-hdr {
    padding: 5px 10px; cursor: pointer; background: #161b22;
    border-top: 1px solid #30363d; border-bottom: 1px solid #30363d;
    user-select: none; flex-shrink: 0; font-size: 12px; color: #8b949e;
  }
  #dt-frames-hdr:hover { background: #1c2128; }
  #dt-frames-box {
    max-height: 0; overflow-y: auto; overflow-x: hidden;
    background: #0d1117; flex-shrink: 0;
    transition: max-height 0.15s ease;
  }
  #dt-frames-box.open { max-height: 180px; }
  .dt-frame {
    padding: 1px 10px; line-height: 1.5; font-size: 12px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .dt-frame-out { color: #bc8cff; }
  .dt-frame-in { color: #3fb950; }
  .dt-frame b { font-weight: 700; }
</style>
<div id="dev-telemetry">
  <div id="dt-status">
    <span class="dt-badge" id="dt-badge" style="background:#484f58;color:#fff">LOADING</span>
    <span class="dt-dot" id="dt-dot" style="background:#484f58"></span>
    <span class="dt-meta" id="dt-conn">--</span>
    <span class="dt-spacer"></span>
    <span class="dt-meta" id="dt-up">0s</span>
    <button class="dt-btn" id="dt-clear">Clear</button>
  </div>
  <div id="dt-log"></div>
  <div id="dt-frames-hdr" onclick="this.classList.toggle('open');document.getElementById('dt-frames-box').classList.toggle('open')">
    &#9654; Frames (<span id="dt-fc">0</span>)
  </div>
  <div id="dt-frames-box"></div>
</div>
<script>
// Telemetry store — app code pushes to this via window.__g2Telemetry
(function() {
  var startTime = Date.now();
  var MAX_LOG = 500, MAX_FRAME = 50;
  var logEl = document.getElementById('dt-log');
  var framesEl = document.getElementById('dt-frames-box');
  var fcEl = document.getElementById('dt-fc');
  var badgeEl = document.getElementById('dt-badge');
  var dotEl = document.getElementById('dt-dot');
  var connEl = document.getElementById('dt-conn');
  var upEl = document.getElementById('dt-up');
  var logCount = 0;

  var stColors = {
    idle:'#3fb950', recording:'#f85149', thinking:'#d29922',
    streaming:'#58a6ff', error:'#f85149', disconnected:'#484f58',
    menu:'#bc8cff', loading:'#484f58', confirming:'#d29922',
    transcribing:'#d29922'
  };
  var darkText = {thinking:1,confirming:1,transcribing:1};

  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function ts(t) {
    var d = new Date(t || Date.now());
    return d.toTimeString().slice(0,8)+'.'+String(d.getMilliseconds()).padStart(3,'0');
  }

  function atBottom() { return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40; }

  function addLog(level, msg) {
    var wasBot = atBottom();
    var d = document.createElement('div');
    d.className = 'dt-entry';
    var cls = level === 'warn' ? 'dt-warn' : level === 'error' ? 'dt-error' : 'dt-info';
    var icon = level === 'warn' ? '!' : level === 'error' ? 'X' : '>';
    d.innerHTML = '<span class="dt-ts">' + ts() + '</span> <span class="'+cls+'">' + icon + ' ' + esc(msg) + '</span>';
    logEl.appendChild(d);
    logCount++;
    while (logCount > MAX_LOG) { logEl.removeChild(logEl.firstChild); logCount--; }
    if (wasBot) logEl.scrollTop = logEl.scrollHeight;
  }

  function addFrame(dir, data) {
    var d = document.createElement('div');
    d.className = 'dt-frame ' + (dir === 'out' ? 'dt-frame-out' : 'dt-frame-in');
    var arrow = dir === 'out' ? '\u2192' : '\u2190';
    var ftype = '';
    try { ftype = JSON.parse(data).type || ''; } catch(e) {}
    var show = String(data).length > 100 ? String(data).slice(0,100)+'\u2026' : String(data);
    d.innerHTML = '<span class="dt-ts">' + ts() + '</span> ' + arrow + ' '
      + (ftype ? '<b>'+esc(ftype)+'</b> ' : '') + esc(show);
    framesEl.appendChild(d);
    while (framesEl.children.length > MAX_FRAME) framesEl.removeChild(framesEl.firstChild);
    fcEl.textContent = String(framesEl.children.length);
  }

  window.__g2Telemetry = { addLog: addLog, addFrame: addFrame };

  var _log = console.log, _warn = console.warn, _err = console.error;
  function fmt(a) { return typeof a === 'string' ? a : JSON.stringify(a); }
  console.log = function() { _log.apply(console, arguments); try { addLog('info', [].slice.call(arguments).map(fmt).join(' ')); } catch(e){} };
  console.warn = function() { _warn.apply(console, arguments); try { addLog('warn', [].slice.call(arguments).map(fmt).join(' ')); } catch(e){} };
  console.error = function() { _err.apply(console, arguments); try { addLog('error', [].slice.call(arguments).map(fmt).join(' ')); } catch(e){} };

  document.getElementById('dt-clear').onclick = function() { logEl.innerHTML = ''; logCount = 0; };

  setInterval(function() {
    try {
      var api = window.__g2Api;
      var st = api ? api.getState() : 'loading';
      badgeEl.textContent = st.toUpperCase();
      badgeEl.style.background = stColors[st] || '#484f58';
      badgeEl.style.color = darkText[st] ? '#000' : '#fff';
      var conn = api && api.getGatewayConnected ? api.getGatewayConnected() : false;
      dotEl.style.background = conn ? '#3fb950' : '#f85149';
      connEl.textContent = conn ? 'Connected' : 'Disconnected';
      var s = Math.floor((Date.now() - startTime) / 1000);
      var m = Math.floor(s / 60);
      upEl.textContent = m > 0 ? m+'m '+(s%60)+'s' : s+'s';
    } catch(e) {}
  }, 300);

  addLog('info', 'Telemetry panel ready');
})();
<\/script>`;

      return html.replace('</body>', panelBlock + '\n</body>');
    },
  };
}

function sessionPanel(): Plugin {
  return {
    name: 'session-panel',
    apply: 'serve',
    transformIndexHtml(html) {
      const panelHtml = `
<!-- Session / Conversation panel -->
<style>
  #session-panel {
    position: fixed; top: 0; left: 0; width: 70%; bottom: 54px;
    display: flex; flex-direction: column;
    background: #0d1117; color: #c9d1d9;
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 14px; z-index: 9000; overflow: hidden;
    border-right: 1px solid #30363d;
  }
  #sp-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 16px; min-height: 40px; box-sizing: border-box;
    background: #161b22; border-bottom: 1px solid #30363d; flex-shrink: 0;
  }
  #sp-header h2 { margin: 0; font-size: 16px; font-weight: 600; color: #e6edf3; }
  .sp-header-spacer { flex: 1; }
  .sp-btn {
    padding: 5px 12px; font-size: 12px; font-weight: 500;
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 6px; cursor: pointer; font-family: inherit;
    transition: background 0.15s;
  }
  .sp-btn:hover { background: #30363d; }
  .sp-btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
  .sp-btn-primary:hover { background: #2ea043; }

  /* Session list view */
  #sp-sessions {
    flex: 1; overflow-y: auto; padding: 12px 16px;
  }
  .sp-card {
    padding: 12px 16px; margin-bottom: 8px;
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    cursor: pointer; transition: background 0.15s, border-color 0.15s;
  }
  .sp-card:hover { background: #1c2128; border-color: #484f58; }
  .sp-card.sp-active { border-left: 3px solid #3fb950; }
  .sp-card-label {
    font-size: 14px; font-weight: 600; color: #e6edf3;
    margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .sp-card-preview {
    font-size: 13px; color: #8b949e; margin-bottom: 6px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .sp-card-meta {
    display: flex; gap: 12px; font-size: 11px; color: #484f58;
  }
  .sp-empty {
    text-align: center; color: #484f58; padding: 40px 20px;
    font-size: 14px;
  }

  /* Conversation view */
  #sp-conversation {
    flex: 1; overflow-y: auto; padding: 12px 16px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .sp-msg {
    max-width: 80%; padding: 10px 14px; border-radius: 12px;
    font-size: 14px; line-height: 1.5; word-wrap: break-word;
  }
  .sp-msg-user {
    align-self: flex-end; background: #1a3a2a; color: #aff5b4;
    border-bottom-right-radius: 4px;
  }
  .sp-msg-assistant {
    align-self: flex-start; background: #1a2a3a; color: #a5d6ff;
    border-bottom-left-radius: 4px;
  }
  .sp-msg-system {
    align-self: center; background: #2d2d2d; color: #8b949e;
    font-size: 12px; font-style: italic; text-align: center;
  }
  .sp-msg-role {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    margin-bottom: 2px; opacity: 0.7;
  }
</style>
<div id="session-panel">
  <div id="sp-header">
    <h2 id="sp-title">Sessions</h2>
    <span class="sp-header-spacer"></span>
    <button class="sp-btn" id="sp-back" style="display:none">&#8592; Sessions</button>
  </div>
  <div id="sp-sessions"></div>
  <div id="sp-conversation" style="display:none"></div>
</div>
<script>
(function() {
  var sessionsEl = document.getElementById('sp-sessions');
  var convEl = document.getElementById('sp-conversation');
  var titleEl = document.getElementById('sp-title');
  var backBtn = document.getElementById('sp-back');
  var lastState = '';
  var lastConvLen = -1;
  var lastSessionJson = '';
  var autoScroll = true;

  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  function relTime(iso) {
    if (!iso) return '';
    var diff = Date.now() - new Date(iso).getTime();
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
    if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
    return Math.floor(diff/86400000) + 'd ago';
  }

  function renderSessions(sessions) {
    if (!sessions || sessions.length === 0) {
      sessionsEl.innerHTML = '<div class="sp-empty">No sessions yet.<br>Waiting for gateway...</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      var cls = 'sp-card' + (s.isActive ? ' sp-active' : '');
      html += '<div class="' + cls + '" data-idx="' + i + '">'
        + '<div class="sp-card-label">' + esc(s.label || s.sessionKey) + '</div>'
        + '<div class="sp-card-preview">' + esc(s.preview || 'No messages') + '</div>'
        + '<div class="sp-card-meta">'
        + '<span>' + s.messageCount + ' msgs</span>'
        + '<span>' + relTime(s.updatedAt) + '</span>'
        + (s.isActive ? '<span style="color:#3fb950">\\u25cf Active</span>' : '')
        + '</div></div>';
    }
    sessionsEl.innerHTML = html;

    // Attach click handlers
    var cards = sessionsEl.querySelectorAll('.sp-card');
    for (var j = 0; j < cards.length; j++) {
      cards[j].addEventListener('click', function() {
        var idx = parseInt(this.getAttribute('data-idx'), 10);
        if (window.__g2Api) {
          // index 0 in the glass menu is "New Session", actual sessions start at 1
          window.__g2Api.selectSession(idx + 1);
        }
      });
    }
  }

  function renderConversation(entries) {
    if (!entries || entries.length === 0) {
      convEl.innerHTML = '<div class="sp-empty">No messages yet. Send one below!</div>';
      return;
    }
    var wasBottom = convEl.scrollHeight - convEl.scrollTop - convEl.clientHeight < 40;
    var html = '';
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      var cls = 'sp-msg sp-msg-' + e.role;
      html += '<div class="' + cls + '">'
        + '<div class="sp-msg-role">' + esc(e.role) + '</div>'
        + esc(e.text)
        + '</div>';
    }
    convEl.innerHTML = html;
    if (wasBottom || autoScroll) {
      convEl.scrollTop = convEl.scrollHeight;
      autoScroll = false;
    }
  }

  function showSessionView() {
    sessionsEl.style.display = '';
    convEl.style.display = 'none';
    backBtn.style.display = 'none';
    titleEl.textContent = 'Sessions';
  }

  function showConversationView() {
    sessionsEl.style.display = 'none';
    convEl.style.display = '';
    backBtn.style.display = '';
    titleEl.textContent = 'Conversation';
    autoScroll = true;
  }

  backBtn.addEventListener('click', function() {
    if (window.__g2Api) window.__g2Api.openSessionMenu();
  });

  setInterval(function() {
    var api = window.__g2Api;
    if (!api) return;
    var state = api.getState();

    if (state === 'menu' || state === 'loading') {
      if (lastState !== 'menu' && lastState !== 'loading') {
        showSessionView();
        lastConvLen = -1;
      }
      // Poll session list
      var sessions = api.getSessionList ? api.getSessionList() : null;
      var json = JSON.stringify(sessions);
      if (json !== lastSessionJson) {
        lastSessionJson = json;
        renderSessions(sessions);
      }
    } else {
      if (lastState === 'menu' || lastState === 'loading' || lastState === '') {
        showConversationView();
        lastSessionJson = '';
      }
      // Poll conversation
      var conv = api.getConversation();
      if (conv && conv.length !== lastConvLen) {
        lastConvLen = conv.length;
        renderConversation(conv);
      }
    }
    lastState = state;
  }, 500);

  showSessionView();
})();
<\/script>`;
      return html.replace('</body>', panelHtml + '\n</body>');
    },
  };
}

export default defineConfig({
  root: '.',
  server: {
    host: 'localhost',
    port: 5173,
  },
  plugins: [apiPlugin(), inputBar(), telemetryPanel(), sessionPanel()],
  build: {
    sourcemap: false,
    outDir: 'dist',
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
