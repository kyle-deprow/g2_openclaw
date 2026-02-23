# G2 OpenClaw Gateway

WebSocket gateway that bridges the G2 glasses (via a phone companion app) to the OpenClaw AI agent. Phase 1 provides a working vertical slice with mock responses.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager

## Installation

From the repository root:

```bash
uv sync --extra dev
```

## Configuration

The gateway reads three environment variables (all optional):

| Variable         | Default     | Description                              |
| ---------------- | ----------- | ---------------------------------------- |
| `GATEWAY_HOST`   | `0.0.0.0`  | Bind address                             |
| `GATEWAY_PORT`   | `8765`      | Listen port                              |
| `GATEWAY_TOKEN`  | *(none)*    | Shared secret for `?token=` auth. If unset, auth is disabled. |

You can place these in a `.env` file at the repo root; `python-dotenv` will load it automatically.

## Running

From the repository root:

```bash
PYTHONPATH=gateway uv run python -m gateway
```

The gateway will log to stdout:

```
2026-02-22 12:00:00 INFO gateway.server: Gateway listening on 0.0.0.0:8765
```

Connect a WebSocket client to `ws://localhost:8765` (append `?token=<TOKEN>` if `GATEWAY_TOKEN` is set).

## Testing

**Unit tests** (gateway logic, protocol parsing, config):

```bash
uv run pytest tests/gateway/ -v
```

**Integration tests** (full vertical slice over real WebSocket):

```bash
uv run pytest tests/integration/ -v
```

**All tests:**

```bash
uv run pytest -v
```

## Protocol Overview

The gateway uses a JSON-over-WebSocket protocol. Each message is a single JSON object with a `type` field.

### Phone → Gateway

| Frame Type    | Key Fields                          | Description                |
| ------------- | ----------------------------------- | -------------------------- |
| `text`        | `message: str`                      | Send a text query          |
| `start_audio` | `sampleRate`, `channels`, `sampleWidth` | Begin audio stream *(Phase 2)* |
| `stop_audio`  | —                                   | End audio stream *(Phase 2)* |
| `pong`        | —                                   | Keepalive response         |

### Gateway → Phone

| Frame Type      | Key Fields        | Description                          |
| --------------- | ----------------- | ------------------------------------ |
| `connected`     | `version: str`    | Handshake — sent immediately on connect |
| `status`        | `status: str`     | State change (idle, thinking, streaming, …) |
| `assistant`     | `delta: str`      | Streamed response chunk              |
| `end`           | —                 | End of response                      |
| `transcription` | `text: str`       | Speech-to-text result *(Phase 2)*    |
| `error`         | `detail`, `code`  | Error notification                   |
| `ping`          | —                 | Keepalive probe                      |

For the full protocol spec, see [docs/02-pc-gateway-design.md](../../docs/02-pc-gateway-design.md).

## Phase 1 Limitations

- **Mock responses only** — the `MockResponseHandler` returns three hardcoded text deltas. No AI backend is connected yet.
- **No audio support** — `start_audio` / `stop_audio` frames are logged but not processed.
- **Single connection** — only one WebSocket client is active at a time; a new connection replaces the previous one.
- **No TLS** — use a reverse proxy for production encryption.
