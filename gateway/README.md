# G2 OpenClaw Gateway

WebSocket gateway that bridges the G2 glasses (via a phone companion app) to the OpenClaw AI agent.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager

## Installation

From the repository root:

```bash
uv sync
```

## Configuration

The gateway reads these environment variables:

| Variable         | Default     | Description                              |
| ---------------- | ----------- | ---------------------------------------- |
| `GATEWAY_HOST`   | `0.0.0.0`  | Bind address                             |
| `GATEWAY_PORT`   | `8765`      | Listen port                              |
| `GATEWAY_TOKEN`  | *(required)* | Shared secret sent in the first WebSocket `auth` frame. |
| `OPENCLAW_GATEWAY_TOKEN` | *(required)* | Token copied from `~/.openclaw/openclaw.json` → `gateway.auth.token`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP endpoint. `none` or empty disables telemetry |

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

Connect a WebSocket client to `ws://localhost:8765`, then send `{"type":"auth","token":"<GATEWAY_TOKEN>"}` as the first frame.

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

## Observability (OpenTelemetry)

The gateway exports traces, metrics, and logs via OTLP. See the [root README](../README.md#observability) for the full observability stack setup.

Key modules:
- `otel_setup.py` — Initialization, logging configuration, graceful degradation
- `metrics.py` — Custom application metrics (5 instruments)

When OTel is disabled, the gateway uses console + file logging (`logs/gateway.log`).

## Operational Notes

- `OPENCLAW_GATEWAY_TOKEN` is required; startup does not fall back to mock responses.
- Audio transcription is active when `Transcriber` loads successfully; startup fails if the Whisper model cannot load.
- Single connection model: only one WebSocket client is active at a time; a new connection replaces the previous one.
- No TLS is provided by the gateway; use a reverse proxy or private network for production encryption.
