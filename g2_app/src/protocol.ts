// === Status types ===
export type GatewayStatus = 'loading' | 'idle' | 'recording' | 'transcribing' | 'thinking' | 'streaming';
export type AppStatus = GatewayStatus | 'error' | 'disconnected' | 'confirming';

export type ErrorCode =
  | 'AUTH_FAILED'
  | 'TRANSCRIPTION_FAILED'
  | 'BUFFER_OVERFLOW'
  | 'OPENCLAW_ERROR'
  | 'INVALID_FRAME'
  | 'INVALID_STATE'
  | 'TIMEOUT'
  | 'INTERNAL_ERROR';

// === Inbound frames (Gateway → App) ===
export interface StatusFrame {
  type: 'status';
  status: GatewayStatus;
  question?: string;
  elapsedMs?: number;
  phase?: string;
}

export interface TranscriptionFrame {
  type: 'transcription';
  text: string;
}

export interface AssistantDelta {
  type: 'assistant';
  delta: string;
}

export interface EndFrame {
  type: 'end';
}

export interface ErrorFrame {
  type: 'error';
  detail: string;
  code: ErrorCode;
}

export interface ConnectedFrame {
  type: 'connected';
  version: string;
  sessionId?: string;
  sessionKey?: string;
  sessionStartedAt?: string;
  taskSummary?: string;
}

export interface PingFrame {
  type: 'ping';
}

export interface HistoryEntry {
  role: 'user' | 'assistant';
  text: string;
  ts: number;
}

export interface HistoryFrame {
  type: 'history';
  entries: HistoryEntry[];
  /** Additive owner projection; an absent value is the initial replay. */
  historyKind?: 'research_owner_delta';
}

export interface SessionResetFrame {
  type: 'session_reset';
  reason: 'user_request' | 'daily_reset' | 'force_stop';
}

export interface AutoresearchStatusFrame {
  type: 'autoresearch_status';
  hypothesisId: string | null;
  hypothesisState: string | null;
  attemptId: string | null;
  attemptState: string | null;
  stage: string;
  lastAstraDecision: string | null;
  campaignStatus: string | null;
  boundaryFailure: string | null;
  lastEventAt: string | null;
  ownerState: 'active' | 'inactive' | 'unknown';
  updatedAt: string | null;
  available: boolean;
  unavailableReason: string | null;
}

export type InboundFrame =
  | StatusFrame
  | TranscriptionFrame
  | AssistantDelta
  | EndFrame
  | ErrorFrame
  | ConnectedFrame
  | PingFrame
  | HistoryFrame
  | SessionResetFrame
  | AutoresearchStatusFrame;

// === Outbound frames (App → Gateway) ===
export interface TextFrame {
  type: 'text';
  message: string;
}

export interface PongFrame {
  type: 'pong';
}

export interface StartAudioFrame {
  type: 'start_audio';
  sampleRate: number;
  channels: number;
  sampleWidth: number;
}

export interface StopAudioFrame {
  type: 'stop_audio';
  hilText?: string;
}

export interface StatusRequestFrame {
  type: 'status_request';
}

export interface ResetSessionFrame {
  type: 'reset_session';
}

export interface ForceStopFrame {
  type: 'force_stop';
}

export type OutboundFrame = TextFrame | PongFrame | StartAudioFrame | StopAudioFrame | StatusRequestFrame | ResetSessionFrame | ForceStopFrame;

// === Frame parsing ===
const INBOUND_TYPES = new Set(['status', 'transcription', 'assistant', 'end', 'error', 'connected', 'ping', 'history', 'session_reset', 'autoresearch_status']);

/** Required fields per inbound frame type (mirrors Python gateway validation). */
const REQUIRED_FIELDS: Record<string, string[]> = {
  status: ['status'],
  transcription: ['text'],
  assistant: ['delta'],
  end: [],
  error: ['detail', 'code'],
  connected: ['version'],
  ping: [],
  history: ['entries'],
  session_reset: ['reason'],
  autoresearch_status: [
    'hypothesisId', 'hypothesisState', 'attemptId', 'attemptState', 'stage',
    'lastAstraDecision', 'campaignStatus', 'boundaryFailure', 'lastEventAt', 'ownerState',
    'updatedAt', 'available', 'unavailableReason',
  ],
};

/** Valid status values (matches GatewayStatus union). */
const VALID_STATUSES = new Set(['loading', 'idle', 'recording', 'transcribing', 'thinking', 'streaming']);

/** Valid error codes (matches ErrorCode union). */
const VALID_ERROR_CODES = new Set([
  'AUTH_FAILED', 'TRANSCRIPTION_FAILED', 'BUFFER_OVERFLOW', 'OPENCLAW_ERROR',
  'INVALID_FRAME', 'INVALID_STATE', 'TIMEOUT', 'INTERNAL_ERROR',
]);

/** Valid session_reset reason values. */
const VALID_REASONS = new Set(['user_request', 'daily_reset', 'force_stop']);

/** Expected types for required and optional fields (runtime validation).
 *  Any declared field with a wrong type is rejected. */
const FIELD_TYPES: Record<string, Record<string, string>> = {
  status: { status: 'string', question: 'string', elapsedMs: 'number', phase: 'string' },
  transcription: { text: 'string' },
  assistant: { delta: 'string' },
  error: { detail: 'string', code: 'string' },
  connected: { version: 'string', sessionId: 'string', sessionKey: 'string', sessionStartedAt: 'string', taskSummary: 'string' },
  history: { entries: 'object', historyKind: 'string' },
  session_reset: { reason: 'string' },
  autoresearch_status: {
    hypothesisId: 'string', hypothesisState: 'string', attemptId: 'string', attemptState: 'string',
    stage: 'string', lastAstraDecision: 'string', campaignStatus: 'string', boundaryFailure: 'string',
    lastEventAt: 'string', ownerState: 'string', updatedAt: 'string', available: 'boolean', unavailableReason: 'string',
  },
};

const NULLABLE_AUTORESEARCH_FIELDS = new Set([
  'hypothesisId', 'hypothesisState', 'attemptId', 'attemptState',
  'lastAstraDecision', 'campaignStatus', 'boundaryFailure', 'lastEventAt', 'updatedAt', 'unavailableReason',
]);

export function parseFrame(data: string): InboundFrame {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    throw new Error(`Invalid JSON: ${data.slice(0, 100)}`);
  }
  if (typeof parsed !== 'object' || parsed === null || !('type' in parsed)) {
    throw new Error('Frame missing "type" field');
  }
  const frame = parsed as Record<string, unknown>;
  if (!INBOUND_TYPES.has(frame.type as string)) {
    throw new Error(`Unknown frame type: ${frame.type}`);
  }

  const required = REQUIRED_FIELDS[frame.type as string];
  if (required) {
    for (const field of required) {
      if (!(field in frame)) {
        throw new Error(`Frame type "${frame.type}" missing required field "${field}"`);
      }
    }
  }

  // Validate known field types. Unknown fields are omitted from the returned object below.
  const typeChecks = FIELD_TYPES[frame.type as string];
  if (typeChecks) {
    for (const [field, expectedType] of Object.entries(typeChecks)) {
      if (field in frame && frame[field] === null && frame.type === 'autoresearch_status' && NULLABLE_AUTORESEARCH_FIELDS.has(field)) {
        continue;
      }
      if (field in frame && typeof frame[field] !== expectedType) {
        throw new Error(`Field "${field}" must be ${expectedType}, got ${typeof frame[field]}`);
      }
    }
  }

  // Build a clean object with only known fields to prevent prototype pollution
  const clean: Record<string, unknown> = { type: frame.type };
  const knownFields = required ?? [];
  for (const f of knownFields) { clean[f] = frame[f]; }

  // Copy optional connected frame fields
  if (clean.type === 'connected') {
    if (typeof frame.sessionId === 'string') clean.sessionId = frame.sessionId;
    if (typeof frame.sessionKey === 'string') clean.sessionKey = frame.sessionKey;
    if (typeof frame.sessionStartedAt === 'string') clean.sessionStartedAt = frame.sessionStartedAt;
    if (typeof frame.taskSummary === 'string') clean.taskSummary = frame.taskSummary;
  }

  // Copy optional status metadata fields
  if (clean.type === 'status') {
    if (typeof frame.question === 'string') clean.question = frame.question;
    if (typeof frame.elapsedMs === 'number') clean.elapsedMs = frame.elapsedMs;
    if (typeof frame.phase === 'string') clean.phase = frame.phase;
  }

  // Copy history entries array (filter out malformed entries)
  if (clean.type === 'history') {
    if (!Array.isArray(frame.entries)) {
      throw new Error('history.entries must be an array');
    }
    clean.entries = (frame.entries as unknown[]).map((entry, index) => {
      if (typeof entry !== 'object' || entry === null) {
        throw new Error(`history.entries[${index}] must be an object`);
      }
      const e = entry as Record<string, unknown>;
      if (e.role !== 'user' && e.role !== 'assistant') {
        throw new Error(`history.entries[${index}].role must be user or assistant`);
      }
      if (typeof e.text !== 'string') {
        throw new Error(`history.entries[${index}].text must be string`);
      }
      if (typeof e.ts !== 'number') {
        throw new Error(`history.entries[${index}].ts must be number`);
      }
      return { role: e.role, text: e.text, ts: e.ts };
    });
    if (frame.historyKind !== undefined) {
      if (frame.historyKind !== 'research_owner_delta') {
        throw new Error('history.historyKind must be research_owner_delta');
      }
      if ((frame.entries as unknown[]).some((entry) => (
        typeof entry !== 'object' || entry === null
        || (entry as Record<string, unknown>).role !== 'assistant'
      ))) {
        throw new Error('research owner history entries must be assistant messages');
      }
      clean.historyKind = frame.historyKind;
    }
  }

  // M-5: Validate status value against known union
  if (clean.type === 'status' && !VALID_STATUSES.has(clean.status as string)) {
    throw new Error(`Invalid status value: "${clean.status}"`);
  }

  // M-6: Validate error code against known union
  if (clean.type === 'error' && !VALID_ERROR_CODES.has(clean.code as string)) {
    throw new Error(`Invalid error code: "${clean.code}"`);
  }

  // Validate session_reset reason against known union
  if (clean.type === 'session_reset' && !VALID_REASONS.has(clean.reason as string)) {
    throw new Error(`Invalid session_reset reason: "${clean.reason}"`);
  }

  return clean as unknown as InboundFrame;
}
