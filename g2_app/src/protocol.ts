// === Status types ===
export type GatewayStatus = 'loading' | 'idle' | 'recording' | 'transcribing' | 'thinking' | 'streaming';
export type AppStatus = GatewayStatus | 'displaying' | 'error' | 'disconnected';

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
}

export interface PingFrame {
  type: 'ping';
}

export type InboundFrame =
  | StatusFrame
  | TranscriptionFrame
  | AssistantDelta
  | EndFrame
  | ErrorFrame
  | ConnectedFrame
  | PingFrame;

// === Outbound frames (App → Gateway) ===
export interface StartAudioFrame {
  type: 'start_audio';
  sampleRate: number;
  channels: number;
  sampleWidth: number;
}

export interface StopAudioFrame {
  type: 'stop_audio';
}

export interface TextFrame {
  type: 'text';
  message: string;
}

export interface PongFrame {
  type: 'pong';
}

export type OutboundFrame = StartAudioFrame | StopAudioFrame | TextFrame | PongFrame;

// === Frame parsing ===
const INBOUND_TYPES = new Set(['status', 'transcription', 'assistant', 'end', 'error', 'connected', 'ping']);

/** Required fields per inbound frame type (mirrors Python gateway validation). */
const REQUIRED_FIELDS: Record<string, string[]> = {
  status: ['status'],
  transcription: ['text'],
  assistant: ['delta'],
  end: [],
  error: ['detail', 'code'],
  connected: ['version'],
  ping: [],
};

/** Valid status values (matches GatewayStatus union). */
const VALID_STATUSES = new Set(['loading', 'idle', 'recording', 'transcribing', 'thinking', 'streaming']);

/** Valid error codes (matches ErrorCode union). */
const VALID_ERROR_CODES = new Set([
  'AUTH_FAILED', 'TRANSCRIPTION_FAILED', 'BUFFER_OVERFLOW', 'OPENCLAW_ERROR',
  'INVALID_FRAME', 'INVALID_STATE', 'TIMEOUT', 'INTERNAL_ERROR',
]);

/** Expected types for required fields (runtime validation). */
const FIELD_TYPES: Record<string, Record<string, string>> = {
  status: { status: 'string' },
  transcription: { text: 'string' },
  assistant: { delta: 'string' },
  error: { detail: 'string', code: 'string' },
  connected: { version: 'string' },
};

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

  // Validate field types
  const typeChecks = FIELD_TYPES[frame.type as string];
  if (typeChecks) {
    for (const [field, expectedType] of Object.entries(typeChecks)) {
      if (typeof frame[field] !== expectedType) {
        throw new Error(`Field "${field}" must be ${expectedType}, got ${typeof frame[field]}`);
      }
    }
  }

  // Build a clean object with only known fields to prevent prototype pollution
  const clean: Record<string, unknown> = { type: frame.type };
  const knownFields = required ?? [];
  for (const f of knownFields) { clean[f] = frame[f]; }

  // M-5: Validate status value against known union
  if (clean.type === 'status' && !VALID_STATUSES.has(clean.status as string)) {
    throw new Error(`Invalid status value: "${clean.status}"`);
  }

  // M-6: Validate error code against known union
  if (clean.type === 'error' && !VALID_ERROR_CODES.has(clean.code as string)) {
    throw new Error(`Invalid error code: "${clean.code}"`);
  }

  return clean as unknown as InboundFrame;
}
