import type { AppStatus } from './protocol';

// Valid transitions map: from → allowed targets
const TRANSITIONS: Record<AppStatus, AppStatus[]> = {
  loading:      ['idle', 'error', 'disconnected'],
  idle:         ['recording', 'thinking', 'error', 'disconnected', 'loading'],
  recording:    ['transcribing', 'idle', 'error', 'disconnected'],
  transcribing: ['thinking', 'idle', 'error', 'disconnected'],
  thinking:     ['streaming', 'idle', 'error', 'disconnected'],
  streaming:    ['displaying', 'idle', 'error', 'disconnected'],
  displaying:   ['idle', 'recording', 'error', 'disconnected'],
  error:        ['idle', 'disconnected'],
  disconnected: ['loading', 'idle', 'error'],
};

export type StateChangeCallback = (newState: AppStatus, oldState: AppStatus) => void;

export class StateMachine {
  private _current: AppStatus = 'loading';
  private callbacks: StateChangeCallback[] = [];

  get current(): AppStatus {
    return this._current;
  }

  transition(newState: AppStatus): boolean {
    if (newState === this._current) return false;

    const allowed = TRANSITIONS[this._current];
    if (!allowed?.includes(newState)) {
      console.warn(`[State] Invalid transition: ${this._current} → ${newState}`);
      return false;
    }

    const old = this._current;
    this._current = newState;
    for (const cb of this.callbacks) {
      try { cb(newState, old); } catch (e) { console.error('[State] Callback error:', e); }
    }
    return true;
  }

  onChange(cb: StateChangeCallback): void {
    this.callbacks.push(cb);
  }

  /** Reset to initial state (for testing) */
  reset(): void {
    this._current = 'loading';
    this.callbacks = [];
  }
}
