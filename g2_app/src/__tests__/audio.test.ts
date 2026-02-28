import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AudioCapture } from '../audio';
import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import type { Gateway } from '../gateway';

// ---------------------------------------------------------------------------
// Mock factories
// ---------------------------------------------------------------------------

function createMockBridge() {
  return {
    audioControl: vi.fn(),
    onMicData: vi.fn(),
  } as unknown as EvenAppBridge;
}

function createMockGateway() {
  return {
    send: vi.fn(),
    sendJson: vi.fn(),
  } as unknown as Gateway;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AudioCapture', () => {
  let audio: AudioCapture;
  let bridge: EvenAppBridge;
  let gateway: Gateway;

  beforeEach(() => {
    audio = new AudioCapture();
    bridge = createMockBridge();
    gateway = createMockGateway();
  });

  it('init registers mic handler', () => {
    audio.init(bridge, gateway);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const onMicData = (bridge as any).onMicData;
    expect(onMicData).toHaveBeenCalledOnce();
    expect(onMicData).toHaveBeenCalledWith(expect.any(Function));
  });

  it('start sends start_audio frame', () => {
    audio.init(bridge, gateway);
    audio.start();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((gateway as any).sendJson).toHaveBeenCalledWith({
      type: 'start_audio',
      sampleRate: 16000,
      channels: 1,
      sampleWidth: 2,
    });
  });

  it('start enables mic', () => {
    audio.init(bridge, gateway);
    audio.start();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((bridge as any).audioControl).toHaveBeenCalledWith(true);
  });

  it('stop disables mic and sends stop_audio', () => {
    audio.init(bridge, gateway);
    audio.start();
    audio.stop();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((bridge as any).audioControl).toHaveBeenCalledWith(false);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((gateway as any).sendJson).toHaveBeenCalledWith({ type: 'stop_audio' });
  });

  it('mic data forwarded when recording', () => {
    audio.init(bridge, gateway);
    audio.start();

    // Retrieve the callback registered with onMicData
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const micCallback = (bridge as any).onMicData.mock.calls[0][0];
    const pcmData = new Uint8Array([1, 2, 3, 4]);
    micCallback({ audioPcm: pcmData });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((gateway as any).send).toHaveBeenCalledWith(pcmData.buffer);
  });

  it('mic data ignored when not recording', () => {
    audio.init(bridge, gateway);
    // NOT started — _recording is false

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const micCallback = (bridge as any).onMicData.mock.calls[0][0];
    const pcmData = new Uint8Array([1, 2, 3, 4]);
    micCallback({ audioPcm: pcmData });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((gateway as any).send).not.toHaveBeenCalled();
  });

  it('start when already recording warns only', () => {
    audio.init(bridge, gateway);
    audio.start();

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    audio.start(); // second call — should warn, not double-start

    expect(warnSpy).toHaveBeenCalledWith('[Audio] Already recording');
    // audioControl should have been called exactly once (only the first start)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((bridge as any).audioControl).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
  });

  it('start without init throws', () => {
    expect(() => audio.start()).toThrow('[Audio] Not initialised — call init() first');
  });

  it('mic data respects Uint8Array byteOffset when forwarding', () => {
    audio.init(bridge, gateway);
    audio.start();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const micCallback = (bridge as any).onMicData.mock.calls[0][0];

    // Create a Uint8Array view into a larger backing buffer at a non-zero offset
    const backing = new ArrayBuffer(10);
    const view = new Uint8Array(backing, 3, 4); // byteOffset=3, byteLength=4
    view.set([10, 20, 30, 40]);

    micCallback({ audioPcm: view });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sentBuffer = (gateway as any).send.mock.calls[0][0] as ArrayBuffer;
    expect(sentBuffer.byteLength).toBe(4);
    expect(new Uint8Array(sentBuffer)).toEqual(new Uint8Array([10, 20, 30, 40]));
  });

  it('double init is ignored with warning', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    audio.init(bridge, gateway);
    audio.init(bridge, gateway);

    expect(warnSpy).toHaveBeenCalledWith('[Audio] Already initialised — ignoring duplicate init()');
    // onMicData should only have been registered once
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((bridge as any).onMicData).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
  });

  // -------------------------------------------------------------------------
  // onEvenHubEvent fallback (simulator mode — no onMicData)
  // -------------------------------------------------------------------------

  describe('onEvenHubEvent fallback', () => {
    function createSimulatorBridge() {
      return {
        audioControl: vi.fn(),
        // no onMicData — simulates the simulator environment
        onEvenHubEvent: vi.fn(),
      } as unknown as EvenAppBridge;
    }

    it('registers onEvenHubEvent when onMicData is absent', () => {
      const simBridge = createSimulatorBridge();
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});

      audio.init(simBridge, gateway);

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((simBridge as any).onEvenHubEvent).toHaveBeenCalledOnce();
      expect(logSpy).toHaveBeenCalledWith(
        '[Audio] onMicData not available — using onEvenHubEvent fallback for audio frames',
      );
      logSpy.mockRestore();
    });

    it('forwards audio frames via onEvenHubEvent when recording', () => {
      const simBridge = createSimulatorBridge();
      audio.init(simBridge, gateway);
      audio.start();

      // Retrieve the onEvenHubEvent callback
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const eventCallback = (simBridge as any).onEvenHubEvent.mock.calls[0][0];

      const pcmData = new Uint8Array([10, 20, 30, 40]);
      eventCallback({ audioEvent: { audioPcm: pcmData } });

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((gateway as any).send).toHaveBeenCalledWith(pcmData.buffer);
    });

    it('ignores audio frames via onEvenHubEvent when not recording', () => {
      const simBridge = createSimulatorBridge();
      audio.init(simBridge, gateway);
      // NOT started

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const eventCallback = (simBridge as any).onEvenHubEvent.mock.calls[0][0];

      const pcmData = new Uint8Array([10, 20, 30, 40]);
      eventCallback({ audioEvent: { audioPcm: pcmData } });

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((gateway as any).send).not.toHaveBeenCalled();
    });

    it('ignores non-audio events in onEvenHubEvent fallback', () => {
      const simBridge = createSimulatorBridge();
      audio.init(simBridge, gateway);
      audio.start();

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const eventCallback = (simBridge as any).onEvenHubEvent.mock.calls[0][0];

      // Fire a non-audio event — should be silently ignored
      eventCallback({ textEvent: { eventType: 0, textContent: 'hello' } });

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((gateway as any).send).not.toHaveBeenCalled();
    });

    it('respects Uint8Array byteOffset in onEvenHubEvent fallback', () => {
      const simBridge = createSimulatorBridge();
      audio.init(simBridge, gateway);
      audio.start();

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const eventCallback = (simBridge as any).onEvenHubEvent.mock.calls[0][0];

      // Create a view into a larger buffer at a non-zero offset
      const backing = new ArrayBuffer(10);
      const view = new Uint8Array(backing, 3, 4);
      view.set([10, 20, 30, 40]);

      eventCallback({ audioEvent: { audioPcm: view } });

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const sentBuffer = (gateway as any).send.mock.calls[0][0] as ArrayBuffer;
      expect(sentBuffer.byteLength).toBe(4);
      expect(new Uint8Array(sentBuffer)).toEqual(new Uint8Array([10, 20, 30, 40]));
    });
  });
});
