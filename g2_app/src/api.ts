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
import type { AppStatus } from './protocol';

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
  getPendingTranscription(): string | null;

  // Sessions
  getSessionList(): Array<{ sessionKey: string; sessionId: string; updatedAt: string; preview: string; messageCount: number; label: string; isActive: boolean }> | null;
  openSessionMenu(): boolean;
  closeSessionMenu(): boolean;
  selectSession(index: number): boolean;
  resetSession(): boolean;

  // Simulation
  tap(): boolean;
  doubleTap(): boolean;
}

export const SESSION_ID_KEY = 'g2_last_session_id';

export function createAppApi(deps: {
  sm: StateMachine;
  input: InputHandler;
  conversation: ConversationHistory;
  gateway: Gateway;
}): AppApi {
  const { sm, input, conversation, gateway } = deps;

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
    getPendingTranscription: () => input.pendingTranscription,
    getSessionList: () => input.sessionList,
    openSessionMenu: () => input.openSessionMenu(),
    closeSessionMenu: () => input.closeSessionMenu(),
    selectSession: (index) => input.simulateMenuSelect(index),
    resetSession: () => input.resetSession(),
    tap: () => input.simulateTap(),
    doubleTap: () => input.simulateDoubleTap(),
  };
}
