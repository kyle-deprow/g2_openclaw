"""Minimal mock OpenClaw WebSocket server for offline development and CI.

Start standalone:
    python tests/mocks/mock_openclaw.py [--port PORT]

Wire protocol:
    req/res for ``connect`` (auth) and ``agent`` (run),
    then streamed ``event`` messages with assistant deltas and lifecycle end.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys

import websockets
from websockets import ServerConnection

DEFAULT_PORT = 18789
DEFAULT_RESPONSE = "This is a mock response from OpenClaw."


def _split_response(text: str, chunks: int = 5) -> list[str]:
    """Split *text* into roughly equal chunks for streaming deltas."""
    words = text.split(" ")
    result: list[str] = []
    per = max(1, len(words) // chunks)
    for i in range(0, len(words), per):
        chunk = " ".join(words[i : i + per])
        if i + per < len(words):
            chunk += " "
        result.append(chunk)
    return result or [text]


async def handler(ws: ServerConnection) -> None:
    """Handle a single client connection following the OpenClaw wire protocol."""
    response_text = os.environ.get("MOCK_RESPONSE", DEFAULT_RESPONSE)
    deltas = _split_response(response_text)

    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")
        msg_id = msg.get("id")
        method = msg.get("method")

        if msg_type != "req":
            continue

        if method == "connect":
            await ws.send(json.dumps({"type": "res", "id": msg_id, "ok": True, "payload": {}}))

        elif method == "agent":
            # Accept the run
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": msg_id,
                        "ok": True,
                        "payload": {
                            "runId": "mock-run-1",
                            "acceptedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                )
            )

            # Stream assistant deltas
            for delta in deltas:
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {"stream": "assistant", "delta": delta},
                        }
                    )
                )
                await asyncio.sleep(0.05)

            # Lifecycle end
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "agent",
                        "payload": {"stream": "lifecycle", "phase": "end"},
                    }
                )
            )


async def main(port: int = DEFAULT_PORT) -> None:
    """Run the mock server until interrupted."""
    stop = asyncio.get_running_loop().create_future()

    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set_result, None)

    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"Mock OpenClaw server listening on ws://0.0.0.0:{port}")
        await stop


if __name__ == "__main__":
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])
    asyncio.run(main(port))
