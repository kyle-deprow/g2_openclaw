import { defineConfig, type Plugin } from 'vite';

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

export default defineConfig({
  root: '.',
  server: {
    host: 'localhost',
    port: 5173,
  },
  plugins: [hilDevBar()],
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
