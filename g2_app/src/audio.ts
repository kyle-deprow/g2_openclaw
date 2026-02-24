import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import type { Gateway } from './gateway';

/**
 * AudioCapture — manages microphone recording and PCM relay.
 *
 * Opens/closes the mic via bridge.audioControl(). Each onMicData chunk
 * is forwarded immediately as a binary WebSocket frame — zero buffering
 * on the phone side (all buffering happens in the Gateway's AudioBuffer).
 *
 * The SDK delivers audioPcm as Uint8Array. We forward the underlying
 * ArrayBuffer directly via gateway.send().
 */
export class AudioCapture {
  private bridge: EvenAppBridge | null = null;
  private gateway: Gateway | null = null;
  private _recording = false;

  /** Store references. Call once after bridge and gateway are ready. */
  init(bridge: EvenAppBridge, gateway: Gateway): void {
    if (this.bridge) {
      console.warn('[Audio] Already initialised — ignoring duplicate init()');
      return;
    }
    this.bridge = bridge;
    this.gateway = gateway;

    // Register mic data handler once
    // NOTE: onMicData is a convenience wrapper around onEvenHubEvent for
    // audio frames. The SDK may expose it at runtime even if absent from
    // the published .d.ts (version ≥ 0.0.7). We cast through `any` to
    // suppress the type-checker while keeping the runtime call correct.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (bridge as any).onMicData((data: { audioPcm: Uint8Array }) => {
      if (this._recording && this.gateway) {
        // Forward raw PCM bytes as binary WebSocket frame.
        // Uint8Array may be a view into a larger ArrayBuffer, so we must
        // respect byteOffset and byteLength when slicing.
        const { buffer, byteOffset, byteLength } = data.audioPcm;
        this.gateway.send(buffer.slice(byteOffset, byteOffset + byteLength) as ArrayBuffer);
      }
    });
  }

  /**
   * Start recording: send start_audio frame, then enable mic.
   * MUST be called after createStartUpPageContainer.
   */
  start(): void {
    if (!this.bridge || !this.gateway) {
      throw new Error('[Audio] Not initialised — call init() first');
    }
    if (this._recording) {
      console.warn('[Audio] Already recording');
      return;
    }

    // Send start_audio frame with G2's PCM format
    this.gateway.sendJson({
      type: 'start_audio',
      sampleRate: 16000,
      channels: 1,
      sampleWidth: 2,
    });

    // Enable microphone — set flag first so state is consistent even if
    // audioControl throws (e.g. in the simulator environment).
    this._recording = true;
    try {
      this.bridge.audioControl(true);
    } catch (err) {
      console.error('[Audio] bridge.audioControl(true) failed — mic may not be active:', err);
    }
    console.log('[Audio] Recording started');
  }

  /** Stop recording: disable mic, then send stop_audio frame. */
  stop(): void {
    if (!this.bridge || !this.gateway) return;
    if (!this._recording) return;

    this._recording = false;
    try {
      this.bridge.audioControl(false);
    } catch (err) {
      console.error('[Audio] bridge.audioControl(false) failed:', err);
    }

    this.gateway.sendJson({ type: 'stop_audio' });
    console.log('[Audio] Recording stopped');
  }

  get isRecording(): boolean {
    return this._recording;
  }
}
