import { defineConfig, type Plugin } from 'vite';
import { devApiPlugin } from './dev-api';

function hilDevBar(): Plugin {
  return {
    name: 'hil-dev-bar',
    apply: 'serve', // dev server only — never in production builds
    transformIndexHtml(html) {
      const hilHtml = `
<!-- DEV-ONLY: HIL keyboard input bar (injected by vite plugin, not part of the app) -->
<div id="hil-input" style="
  position: fixed; bottom: 0; left: 0; right: 0;
  display: flex; gap: 8px; padding: 12px 16px;
  background: #1a1a2e; border-top: 1px solid #333;
  z-index: 9999; font-family: system-ui, sans-serif;
">
  <input id="hil-text" type="text" placeholder="Type your message..."
    autocomplete="off" style="
    flex: 1; padding: 10px 14px; font-size: 15px;
    border: 1px solid #555; border-radius: 6px;
    background: #0f0f1a; color: #e0e0e0; outline: none;
  " />
  <button id="hil-record" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #e74c3c; color: #fff; cursor: pointer;
  ">Record</button>
  <button id="hil-stop" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #f39c12; color: #fff; cursor: pointer;
    display: none;
  ">Stop</button>
  <button id="hil-confirm" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #27ae60; color: #fff; cursor: pointer;
    display: none;
  ">Confirm</button>
  <button id="hil-reject" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #95a5a6; color: #fff; cursor: pointer;
    display: none;
  ">Reject</button>
  <button id="hil-cancel" style="
    padding: 10px 16px; font-size: 14px; font-weight: 600;
    border: none; border-radius: 6px;
    background: #e67e22; color: #fff; cursor: pointer;
    display: none;
  ">Cancel</button>
  <span id="hil-state" style="
    padding: 10px 8px; font-size: 12px; color: #888;
    min-width: 80px; text-align: center;
  ">--</span>
</div>
<script>
  // Wire the HIL bar — calls into the app via window.__g2Dev hook
  function updateHilButtons() {
    const dev = window.__g2Dev;
    const state = dev ? dev.getState() : 'loading';
    const stateEl = document.getElementById('hil-state');
    if (stateEl) stateEl.textContent = state;

    const recordBtn = document.getElementById('hil-record');
    const stopBtn = document.getElementById('hil-stop');
    const confirmBtn = document.getElementById('hil-confirm');
    const rejectBtn = document.getElementById('hil-reject');
    const cancelBtn = document.getElementById('hil-cancel');
    const textInput = document.getElementById('hil-text');

    if (!recordBtn || !stopBtn || !confirmBtn || !rejectBtn) return;

    recordBtn.style.display = state === 'idle' ? '' : 'none';
    stopBtn.style.display = state === 'recording' ? '' : 'none';
    confirmBtn.style.display = state === 'confirming' ? '' : 'none';
    rejectBtn.style.display = state === 'confirming' ? '' : 'none';
    if (cancelBtn) cancelBtn.style.display = (state === 'thinking' || state === 'streaming') ? '' : 'none';
    if (textInput) textInput.disabled = state !== 'idle' && state !== 'recording';
  }

  // Poll state every 200ms
  setInterval(updateHilButtons, 200);

  document.getElementById('hil-record').addEventListener('click', () => {
    if (window.__g2Dev) window.__g2Dev.startRecording();
  });

  document.getElementById('hil-stop').addEventListener('click', () => {
    const input = document.getElementById('hil-text');
    const hilText = input ? input.value.trim() : '';
    if (window.__g2Dev) window.__g2Dev.stopRecording(hilText || undefined);
  });

  document.getElementById('hil-confirm').addEventListener('click', () => {
    if (window.__g2Dev) window.__g2Dev.confirmTranscription();
  });

  document.getElementById('hil-reject').addEventListener('click', () => {
    if (window.__g2Dev) window.__g2Dev.rejectTranscription();
  });

  document.getElementById('hil-cancel').addEventListener('click', () => {
    if (window.__g2Dev) window.__g2Dev.cancelResponse();
  });

  document.getElementById('hil-text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const state = window.__g2Dev ? window.__g2Dev.getState() : '';
      if (state === 'idle') {
        document.getElementById('hil-record').click();
      } else if (state === 'recording') {
        document.getElementById('hil-stop').click();
      }
    }
  });
</script>`;
      return html.replace('</body>', hilHtml + '\n</body>');
    },
  };
}

function devTelemetryPanel(): Plugin {
  return {
    name: 'dev-telemetry-panel',
    apply: 'serve',
    transformIndexHtml(html) {
      // Relax CSP for dev mode to allow inline scripts (telemetry, HIL bar, dev API)
      html = html.replace(
        /script-src\s+'self'/,
        "script-src 'self' 'unsafe-inline'",
      );

      // Single self-contained block: telemetry store + UI + polling
      // No WebSocket constructor patching — app code pushes frames directly
      const panelBlock = `
<!-- DEV-ONLY: Debug telemetry panel -->
<style>
  html, body { background: #0d1117 !important; margin: 0 !important; padding: 0 !important; height: 100% !important; }
  #dev-telemetry {
    position: fixed; top: 0; left: 0; right: 0; bottom: 54px;
    display: flex; flex-direction: column;
    background: #0d1117; color: #c9d1d9;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 13px; z-index: 9000; overflow: hidden;
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

  // Expose store — app code calls these directly
  window.__g2Telemetry = { addLog: addLog, addFrame: addFrame };

  // Patch console to capture logs
  var _log = console.log, _warn = console.warn, _err = console.error;
  function fmt(a) { return typeof a === 'string' ? a : JSON.stringify(a); }
  console.log = function() { _log.apply(console, arguments); try { addLog('info', [].slice.call(arguments).map(fmt).join(' ')); } catch(e){} };
  console.warn = function() { _warn.apply(console, arguments); try { addLog('warn', [].slice.call(arguments).map(fmt).join(' ')); } catch(e){} };
  console.error = function() { _err.apply(console, arguments); try { addLog('error', [].slice.call(arguments).map(fmt).join(' ')); } catch(e){} };

  // Clear button
  document.getElementById('dt-clear').onclick = function() { logEl.innerHTML = ''; logCount = 0; };

  // Poll state from app
  setInterval(function() {
    try {
      var dev = window.__g2Dev;
      var st = dev ? dev.getState() : 'loading';
      badgeEl.textContent = st.toUpperCase();
      badgeEl.style.background = stColors[st] || '#484f58';
      badgeEl.style.color = darkText[st] ? '#000' : '#fff';
      var conn = dev && dev.getGatewayConnected ? dev.getGatewayConnected() : false;
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

export default defineConfig({
  root: '.',
  server: {
    host: 'localhost',
    port: 5173,
  },
  plugins: [devApiPlugin(), hilDevBar(), devTelemetryPanel()],
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
