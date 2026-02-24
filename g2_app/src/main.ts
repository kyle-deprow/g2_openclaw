/**
 * main.ts — OpenClaw G2 App Entry Point (P1.9)
 *
 * Boots the app by:
 *   1. Waiting for the EvenAppBridge SDK to become ready
 *   2. Initialising the DisplayManager and showing a loading screen
 *   3. Creating a Gateway and connecting to the PC gateway server
 *   4. Creating a StateMachine for lifecycle tracking
 *   5. Initialising the AudioCapture pipeline (mic → PCM → gateway)
 *   6. Initialising the InputHandler (R1 ring → tap/gesture routing)
 *
 * Architecture:
 *   waitForEvenAppBridge() → bridge
 *     → display.init(bridge) → showLoading()
 *     → gateway.connect()
 *     → gateway.onMessage(routeFrame)
 *     → gateway.onEvent(routeEvent)
 */

import { waitForEvenAppBridge } from '@evenrealities/even_hub_sdk';

import { AudioCapture } from './audio';
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
let audio: AudioCapture;
let input: InputHandler;

/**
 * The most recent user query text.  Updated when a transcription frame
 * arrives or (Phase 2) when the user sends text via the R1 ring.
 * Used to keep the display header contextual during thinking/streaming.
 */
let currentQuery = '';

// ---------------------------------------------------------------------------
// Frame routing
// ---------------------------------------------------------------------------

/**
 * Route an inbound frame from the Gateway to the appropriate StateMachine
 * transition and DisplayManager layout.
 *
 * CRITICAL race-condition guard:
 *   The server sends `end` immediately followed by `status:idle`.
 *   `end` transitions us to the `displaying` state, where we hold the
 *   response on screen until the user dismisses it.  We must therefore
 *   *ignore* the trailing `status:idle` when already in `displaying`.
 */
function routeFrame(frame: InboundFrame): void {
  switch (frame.type) {
    // -- Connection established ------------------------------------------
    case 'connected':
      console.log(`[Main] Gateway connected (server v${frame.version})`);
      sm.transition('idle');
      display.showIdle();
      break;

    // -- Server status change --------------------------------------------
    case 'status': {
      // Guard: ignore status:idle while we are showing a completed response
      // or displaying an error — the user must dismiss manually.
      if (frame.status === 'idle' && (sm.current === 'displaying' || sm.current === 'error')) {
        console.log(`[Main] Ignoring status:idle — currently in ${sm.current} state`);
        return;
      }

      sm.transition(frame.status);

      switch (frame.status) {
        case 'idle':
          display.showIdle();
          break;
        case 'thinking':
          display.showThinking(currentQuery);
          break;
        case 'streaming':
          display.showStreaming(currentQuery);
          break;
        case 'recording':
          display.showRecording();
          break;
        case 'transcribing':
          display.showTranscribing();
          break;
        case 'loading':
          display.showLoading();
          break;
      }
      break;
    }

    // -- Streaming assistant response delta --------------------------------
    case 'assistant':
      display.appendDelta(frame.delta);
      break;

    // -- Response complete -------------------------------------------------
    case 'end':
      sm.transition('displaying');
      display.finaliseStream();
      break;

    // -- Error from server -------------------------------------------------
    case 'error':
      if (audio.isRecording) audio.stop();
      console.error('[Main] Server error: %s (%s)', frame.detail.slice(0, 100), frame.code);
      sm.transition('error');
      // Sanitize and limit error detail before display
      display.showError(
        typeof frame.detail === 'string' ? frame.detail.slice(0, 200) : 'Unknown error',
      );
      break;

    // -- Transcription text ------------------------------------------------
    case 'transcription':
      currentQuery = frame.text;
      console.log('[Main] Transcription received (%d chars)', frame.text.length);
      break;

    // -- Ping is handled by Gateway itself; should never arrive here -------
    case 'ping':
      break;
  }
}

// ---------------------------------------------------------------------------
// Gateway event routing
// ---------------------------------------------------------------------------

/**
 * Handle transport-level events from the Gateway (disconnect / reconnect).
 * The `connected` event is intentionally a no-op here because the server
 * sends an explicit `connected` frame with version info once the WebSocket
 * is established.
 */
function routeEvent(event: GatewayEvent): void {
  switch (event) {
    case 'connected':
      // Handled by the 'connected' inbound frame — no action needed.
      break;

    case 'disconnected':
      if (audio.isRecording) audio.stop();
      console.warn('[Main] Gateway disconnected');
      sm.transition('disconnected');
      display.showDisconnected();
      break;

    case 'reconnecting':
      // TODO: Phase 2 — show retry countdown via display.updateRetryCountdown()
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

  // 2. Initialise the display and show loading screen
  display = new DisplayManager();
  await display.init(bridge);
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

  // 5. Initialise audio capture pipeline
  audio = new AudioCapture();
  audio.init(bridge, gateway);
  console.log('[Main] AudioCapture initialised');

  // 6. Initialise R1 ring input handling
  input = new InputHandler();
  input.init({ sm, display, audio, gateway, bridge });
  console.log('[Main] InputHandler initialised');
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

boot().catch((err) => {
  console.error('[Main] Fatal boot error:', err);
  // Attempt to show the error on display if it was initialised
  if (display) {
    display.showError(
      err instanceof Error ? err.message : 'Unknown boot error',
      'Restart the app',
    );
  }
});
