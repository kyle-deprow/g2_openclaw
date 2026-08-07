/**
 * api.ts — Centralized application API.
 *
 * Provides a typed interface for all app control operations.
 * Used by phone UI, HTTP automation endpoints, and external tools.
 */

import type { ConversationHistory } from './conversation';
import type { Gateway } from './gateway';
import type { InputHandler } from './input';
import type { StateMachine } from './state';
import type {
  AppStatus,
  AutoresearchStatusFrame,
} from './protocol';

export interface AppApi {
  // State
  getState(): AppStatus;
  getGatewayConnected(): boolean;
  getSessionId(): string | null;
  getAutoresearchStatus(): AutoresearchStatusFrame | null;

  // Conversation
  getConversation(): Array<{ role: 'user' | 'assistant' | 'system'; text: string; timestamp: number }>;
  getDisplayText(): string;

  // Actions
  sendText(message: string): boolean;
  ttsRecord(text: string): boolean;
  startRecording(): boolean;
  stopRecording(hilText?: string): boolean;
  confirmTranscription(): boolean;
  rejectTranscription(): boolean;
  cancelResponse(): boolean;
  forceStop(): boolean;
  killOpenClawSession(): void;
  getPendingTranscription(): string | null;

  resetSession(): boolean;

  getActiveTab(): ActiveTab;
  setActiveTab(tab: ActiveTab): void;

  // Simulation
  tap(): boolean;
  doubleTap(): boolean;
}

export type ActiveTab = 'openclaw' | 'telemetry';

export const SESSION_ID_KEY = 'g2_last_session_id';

export function createAppApi(deps: {
  sm: StateMachine;
  input: InputHandler;
  conversation: ConversationHistory;
  gateway: Gateway;
  getAutoresearchStatus: () => AutoresearchStatusFrame | null;
  getActiveTab: () => ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
}): AppApi {
  const {
    sm,
    input,
    conversation,
    gateway,
    getAutoresearchStatus,
    getActiveTab,
    setActiveTab: setTab,
  } = deps;

  return {
    getState: () => sm.current,
    getGatewayConnected: () => gateway.isConnected,
    getAutoresearchStatus: () => getAutoresearchStatus(),
    getSessionId: () => {
      try { return localStorage.getItem(SESSION_ID_KEY); } catch { return null; }
    },
    getConversation: () => conversation.getEntries(),
    getDisplayText: () => conversation.formatReverse(2000),
    sendText: (msg) => input.sendText(msg),
    ttsRecord: (text) => input.ttsRecord(text),
    startRecording: () => input.startRecording(),
    stopRecording: (hilText) => input.stopRecording(hilText),
    confirmTranscription: () => input.confirmTranscription(),
    rejectTranscription: () => input.rejectTranscription(),
    cancelResponse: () => input.cancelResponse(),
    forceStop: () => input.forceStop(),
    killOpenClawSession: () => {
      gateway.sendJson({ type: 'force_stop' });
    },
    getPendingTranscription: () => input.pendingTranscription,
    resetSession: () => input.resetSession(),

    getActiveTab: () => getActiveTab(),
    setActiveTab: (tab) => {
      if (tab !== 'openclaw' && tab !== 'telemetry') {
        throw new Error(`Unknown active tab: ${String(tab)}`);
      }
      setTab(tab);
    },

    tap: () => input.simulateTap(),
    doubleTap: () => input.simulateDoubleTap(),
  };
}
