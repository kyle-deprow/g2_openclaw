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
import type { AppStatus, CopilotSessionEntry, CopilotHistoryEntry } from './protocol';

export interface AppApi {
  // State
  getState(): AppStatus;
  getGatewayConnected(): boolean;
  getSessionId(): string | null;

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

  // Sessions
  getSessionList(): Array<{ sessionKey: string; sessionId: string; updatedAt: string; preview: string; messageCount: number; label: string; isActive: boolean }> | null;
  openSessionMenu(): boolean;
  closeSessionMenu(): boolean;
  selectSession(index: number): boolean;
  resetSession(): boolean;

  // Copilot
  getCopilotSessions(): CopilotSessionEntry[] | null;
  requestCopilotSessions(): void;
  watchCopilotSession(sessionId: string): void;
  unwatchCopilotSession(): void;
  killCopilotSession(sessionId: string): void;
  getCopilotConversation(): Array<{ role: string; text: string; ts: number }>;
  getActiveTab(): ActiveTab;
  setActiveTab(tab: ActiveTab): void;

  // Simulation
  tap(): boolean;
  doubleTap(): boolean;
}

export type ActiveTab = 'openclaw' | 'copilot' | 'telemetry';

export const SESSION_ID_KEY = 'g2_last_session_id';

export function createAppApi(deps: {
  sm: StateMachine;
  input: InputHandler;
  conversation: ConversationHistory;
  gateway: Gateway;
  getCopilotSessions: () => CopilotSessionEntry[] | null;
  getCopilotConversation: () => CopilotHistoryEntry[];
  getActiveTab: () => ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
}): AppApi {
  const { sm, input, conversation, gateway, getCopilotSessions, getCopilotConversation, getActiveTab, setActiveTab: setTab } = deps;

  return {
    getState: () => sm.current,
    getGatewayConnected: () => gateway.isConnected,
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
      gateway.requestSessionList();
    },
    getPendingTranscription: () => input.pendingTranscription,
    getSessionList: () => input.sessionList,
    openSessionMenu: () => input.openSessionMenu(),
    closeSessionMenu: () => input.closeSessionMenu(),
    selectSession: (index) => input.simulateMenuSelect(index),
    resetSession: () => input.resetSession(),

    getCopilotSessions: () => getCopilotSessions(),
    requestCopilotSessions: () => gateway.sendJson({ type: 'copilot_session_list_request' }),
    watchCopilotSession: (sessionId) => gateway.sendJson({ type: 'copilot_watch', sessionId }),
    unwatchCopilotSession: () => gateway.sendJson({ type: 'copilot_unwatch' }),
    killCopilotSession: (sessionId) => gateway.sendJson({ type: 'copilot_kill', sessionId }),
    getCopilotConversation: () => getCopilotConversation(),
    getActiveTab: () => getActiveTab(),
    setActiveTab: (tab) => setTab(tab),

    tap: () => input.simulateTap(),
    doubleTap: () => input.simulateDoubleTap(),
  };
}
