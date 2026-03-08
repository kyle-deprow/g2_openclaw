/**
 * main.ts — OpenClaw G2 App Entry Point
 *
 * Boots the app by:
 *   1. Waiting for the EvenAppBridge SDK to become ready
 *   2. Initialising the DisplayManager with a ConversationHistory
 *   3. Creating a Gateway and connecting to the PC gateway server
 *   4. Creating a StateMachine for lifecycle tracking
 *   5. Initialising the InputHandler (R1 ring → tap/gesture routing)
 */

import { waitForEvenAppBridge } from '@evenrealities/even_hub_sdk';

import { ConversationHistory } from './conversation';
import { DisplayManager } from './display';
import { Gateway } from './gateway';
import type { GatewayEvent } from './gateway';
import { InputHandler } from './input';
import type { InboundFrame } from './protocol';
import { StateMachine } from './state';

// ---------------------------------------------------------------------------
// Module-level references (accessible to routing functions)
// ---------------------------------------------------------------------------
let display: DisplayManager;
let gateway: Gateway;
let sm: StateMachine;
let input: InputHandler;
let conversation: ConversationHistory;

const SESSION_ID_KEY = 'g2_last_session_id';

// ---------------------------------------------------------------------------
// Frame routing
// ---------------------------------------------------------------------------

function routeFrame(frame: InboundFrame): void {
  switch (frame.type) {
    // -- Connection established ------------------------------------------
    case 'connected': {
      console.log(`[Main] Gateway connected (server v${frame.version})`);

      // Session change detection
      if (frame.sessionId) {
        let lastSessionId: string | null = null;
        try {
          lastSessionId = localStorage.getItem(SESSION_ID_KEY);
        } catch { /* sandboxed or unavailable — treat as no prior session */ }

        if (lastSessionId && lastSessionId !== frame.sessionId) {
          console.log('[Main] Session changed: %s → %s', lastSessionId, frame.sessionId);
          conversation.clear();
          conversation.addSystem('New session started');
        }

        try {
          localStorage.setItem(SESSION_ID_KEY, frame.sessionId);
        } catch { /* localStorage full or unavailable — non-fatal */ }
      }

      sm.transition('idle');
      display.showIdle().catch(err => console.error('[Main] Display error:', err));
      break;
    }

    // -- Server status change --------------------------------------------
    case 'status': {
      // Guard: ignore status:idle while in error — user must dismiss.
      if (frame.status === 'idle' && sm.current === 'error') {
        console.log('[Main] Ignoring status:idle — currently in error state');
        return;
      }

      // Guard: ignore status:idle while confirming — user must confirm/reject.
      if (frame.status === 'idle' && sm.current === 'confirming') {
        console.log('[Main] Ignoring status:idle — waiting for user confirmation');
        return;
      }

      if (!sm.transition(frame.status)) return;  // no-op if already in target state

      switch (frame.status) {
        case 'idle':
          display.showIdle().catch(err => console.error('[Main] Display error:', err));
          break;
        case 'thinking':
          display.showThinking().catch(err => console.error('[Main] Display error:', err));
          break;
        case 'streaming':
          conversation.startAssistantStream();
          display.showStreaming().catch(err => console.error('[Main] Display error:', err));
          break;
        case 'recording':
          display.showRecording().catch(err => console.error('[Main] Display error:', err));
          break;
        case 'transcribing':
          display.showTranscribing().catch(err => console.error('[Main] Display error:', err));
          break;
        case 'loading':
          display.showLoading().catch(err => console.error('[Main] Display error:', err));
          break;
      }
      break;
    }

    // -- Streaming assistant response delta --------------------------------
    case 'assistant':
      if (sm.current !== 'streaming') {
        console.warn('[Main] Ignoring late assistant delta in state:', sm.current);
        break;
      }
      conversation.appendToLastAssistant(frame.delta);
      display.appendDelta(frame.delta).catch(err => console.error('[Main] Display error:', err));
      break;

    // -- Response complete -------------------------------------------------
    case 'end':
      if (sm.current === 'streaming') {
        sm.transition('idle');
        display.finaliseStream().catch(err => console.error('[Main] Display error:', err));
      }
      break;

    // -- Error from server -------------------------------------------------
    case 'error': {
      console.error('[Main] Server error: %s (%s)', frame.detail.slice(0, 100), frame.code);
      const errorMsg = frame.detail.slice(0, 200);
      conversation.addSystem(`Error: ${errorMsg}`);
      sm.transition('error');
      display.showError(errorMsg).catch(err => console.error('[Main] Display error:', err));
      break;
    }

    // -- Transcription text ------------------------------------------------
    case 'transcription':
      console.log('[Main] Transcription received (%d chars)', frame.text.length);
      if (sm.current !== 'transcribing' && sm.current !== 'recording' && sm.current !== 'idle') {
        console.warn('[Main] Ignoring transcription in state:', sm.current);
        break;
      }
      if (frame.text.trim()) {
        conversation.addUser(frame.text);
        input.setPendingTranscription(frame.text);
        sm.transition('confirming');
        display.showConfirming(frame.text).catch(err => console.error('[Main] Display error:', err));
      }
      break;

    // -- Ping is handled by Gateway itself; should never arrive here -------
    case 'ping':
      break;

    // -- Conversation history replay on reconnect --------------------------
    case 'history': {
      console.log(`[Main] History replay: ${frame.entries.length} entries`);
      conversation.replayHistory(frame.entries);
      if (frame.entries.length > 0) {
        display.showIdle().catch(err => console.error('[Main] Display error:', err));
      }
      break;
    }

    // -- Session reset notification --------------------------------------
    case 'session_reset': {
      const reason = frame.reason;
      console.log('[Main] Session reset (%s)', reason);
      conversation.clear();
      const label = reason === 'daily_reset' ? 'New day, new session' : 'Session reset';
      conversation.addSystem(label);
      if (sm.current !== 'idle') sm.transition('idle');
      display.showSessionReset(label).catch(err => console.error('[Main] Display error:', err));
      break;
    }
  }
}

// ---------------------------------------------------------------------------
// Gateway event routing
// ---------------------------------------------------------------------------

function routeEvent(event: GatewayEvent): void {
  switch (event) {
    case 'connected':
      gateway.requestStatus();
      break;

    case 'disconnected':
      console.warn('[Main] Gateway disconnected');
      sm.transition('disconnected');
      display.showDisconnected().catch(err => console.error('[Main] Display error:', err));
      break;

    case 'reconnecting':
      console.log('[Main] Gateway reconnecting...');
      break;
  }
}

// ---------------------------------------------------------------------------
// Boot sequence
// ---------------------------------------------------------------------------

async function boot(): Promise<void> {
  console.log('[Main] OpenClaw app booting...');

  // 1. Wait for the EvenAppBridge to become available
  console.log('[Main] Waiting for EvenAppBridge...');
  const bridge = await waitForEvenAppBridge();
  console.log('[Main] EvenAppBridge ready');

  // 2. Initialise conversation model + display
  conversation = new ConversationHistory();
  display = new DisplayManager();
  await display.init(bridge, conversation);
  console.log('[Main] DisplayManager initialised');
  await display.showLoading();

  // 3. Create the state machine (starts in 'loading')
  sm = new StateMachine();
  sm.onChange((newState, oldState) => {
    console.log(`[Main] State: ${oldState} → ${newState}`);
  });

  // 4. Create the gateway, wire callbacks, and connect
  gateway = new Gateway();
  gateway.onMessage(routeFrame);
  gateway.onEvent(routeEvent);

  console.log('[Main] Connecting to gateway...');
  gateway.connect();

  // 5. Initialise R1 ring input handling
  input = new InputHandler();
  input.init({ sm, display, gateway, bridge });
  console.log('[Main] InputHandler initialised');

  // Expose dev hook for the HIL bar (tree-shaken out of production builds)
  if (import.meta.env.DEV) {
    (window as any).__g2Dev = {
      sendText: (msg: string) => input.sendText(msg),
      startRecording: () => input.startRecording(),
      stopRecording: (hilText?: string) => input.stopRecording(hilText),
      confirmTranscription: () => input.confirmTranscription(),
      rejectTranscription: () => input.rejectTranscription(),
      cancelResponse: () => input.cancelResponse(),
      resetSession: () => input.resetSession(),
      getState: () => sm.current,
      getPendingTranscription: () => input.pendingTranscription,
    };
  }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

boot().catch((err) => {
  console.error('[Main] Fatal boot error:', err);
  if (display) {
    display.showError(
      err instanceof Error ? err.message : 'Unknown boot error',
      'Restart the app',
    );
  }
});
