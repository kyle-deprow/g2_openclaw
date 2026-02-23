import { describe, it, expect, vi } from 'vitest';
import { StateMachine } from '../state';
import type { AppStatus } from '../protocol';

describe('StateMachine', () => {
  it('starts in loading state', () => {
    const sm = new StateMachine();
    expect(sm.current).toBe('loading');
  });

  it('allows valid transitions', () => {
    const sm = new StateMachine();
    expect(sm.transition('idle')).toBe(true);
    expect(sm.current).toBe('idle');
  });

  it('rejects invalid transitions', () => {
    const sm = new StateMachine();
    expect(sm.transition('streaming')).toBe(false);
    expect(sm.current).toBe('loading');
  });

  it('fires callbacks on valid transitions', () => {
    const sm = new StateMachine();
    const cb = vi.fn();
    sm.onChange(cb);
    sm.transition('idle');
    expect(cb).toHaveBeenCalledWith('idle', 'loading');
  });

  it('does not fire callbacks on invalid transitions', () => {
    const sm = new StateMachine();
    const cb = vi.fn();
    sm.onChange(cb);
    sm.transition('streaming'); // invalid from loading
    expect(cb).not.toHaveBeenCalled();
  });

  it('does not fire callbacks on same-state transition', () => {
    const sm = new StateMachine();
    sm.transition('idle');
    const cb = vi.fn();
    sm.onChange(cb);
    expect(sm.transition('idle')).toBe(false);
    expect(cb).not.toHaveBeenCalled();
  });

  it('follows full happy-path flow', () => {
    const sm = new StateMachine();
    const states: AppStatus[] = ['idle', 'recording', 'transcribing', 'thinking', 'streaming', 'displaying', 'idle'];
    for (const s of states) {
      expect(sm.transition(s)).toBe(true);
      expect(sm.current).toBe(s);
    }
  });

  it('allows error from any active state', () => {
    const activeStates: AppStatus[] = ['idle', 'recording', 'transcribing', 'thinking', 'streaming', 'displaying'];
    for (const state of activeStates) {
      const sm = new StateMachine();
      sm.transition('idle');
      if (state !== 'idle') {
        // Navigate to the target state via valid path
        if (state === 'recording') sm.transition('recording');
        if (state === 'transcribing') { sm.transition('recording'); sm.transition('transcribing'); }
        if (state === 'thinking') { sm.transition('recording'); sm.transition('transcribing'); sm.transition('thinking'); }
        if (state === 'streaming') { sm.transition('recording'); sm.transition('transcribing'); sm.transition('thinking'); sm.transition('streaming'); }
        if (state === 'displaying') { sm.transition('recording'); sm.transition('transcribing'); sm.transition('thinking'); sm.transition('streaming'); sm.transition('displaying'); }
      }
      expect(sm.current).toBe(state);
      expect(sm.transition('error')).toBe(true);
      expect(sm.current).toBe('error');
    }
  });

  it('transitions from error to idle', () => {
    const sm = new StateMachine();
    sm.transition('idle');
    sm.transition('error');
    expect(sm.transition('idle')).toBe(true);
  });

  it('transitions from disconnected to idle or loading', () => {
    const sm = new StateMachine();
    sm.transition('disconnected');
    expect(sm.transition('loading')).toBe(true);
  });

  it('displaying → idle on dismiss', () => {
    const sm = new StateMachine();
    sm.transition('idle');
    sm.transition('recording');
    sm.transition('transcribing');
    sm.transition('thinking');
    sm.transition('streaming');
    sm.transition('displaying');
    expect(sm.transition('idle')).toBe(true);
  });
});
