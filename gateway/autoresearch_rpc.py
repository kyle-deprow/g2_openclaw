"""Authenticated OpenClaw RPC boundaries for autoresearch supervision."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_AGENT_ID as AUTORESEARCH_OWNER_AGENT_ID,
)
from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_SESSION_KEY as AUTORESEARCH_OWNER_SESSION_KEY,
)
from gateway.autoresearch_shared import (
    DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS as DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS,
)
from gateway.autoresearch_shared import (
    DEFAULT_TASK_RPC_TIMEOUT_SECONDS as DEFAULT_TASK_RPC_TIMEOUT_SECONDS,
)
from gateway.autoresearch_shared import (
    READ_ONLY_TASK_LIST_ATTEMPTS as READ_ONLY_TASK_LIST_ATTEMPTS,
)
from gateway.autoresearch_shared import (
    READ_ONLY_TASK_LIST_RETRY_SECONDS as READ_ONLY_TASK_LIST_RETRY_SECONDS,
)
from gateway.autoresearch_shared import (
    REQUIRED_OPENCLAW_VERSION_TEXT as REQUIRED_OPENCLAW_VERSION_TEXT,
)
from gateway.autoresearch_shared import (
    OpenClawUnavailableError as OpenClawUnavailableError,
)
from gateway.autoresearch_shared import (
    ShutdownInterrupted as ShutdownInterrupted,
)
from gateway.autoresearch_shared import (
    SupervisorError as SupervisorError,
)
from gateway.openclaw_client import (
    OpenClawError as OpenClawError,
)
from gateway.openclaw_client import (
    OpenClawTransportError as OpenClawTransportError,
)

logger: logging.Logger = logging.getLogger(__name__)


ShutdownRequested = Callable[[], bool]


class _OpenClawClient(Protocol):
    async def request_once(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float,
        required_server_version: str,
    ) -> Mapping[str, object]: ...


def OpenClawClient(host: str, port: int, token: str) -> _OpenClawClient:
    # The function form preserves the gateway.autoresearch_supervisor.OpenClawClient
    # patch surface and is intentionally not the OpenClawClient class.
    from gateway import autoresearch_supervisor

    return autoresearch_supervisor.OpenClawClient(host, port, token)


def load_dotenv(dotenv_path: Path) -> bool:
    # The function form preserves the gateway.autoresearch_supervisor.load_dotenv
    # patch surface and is intentionally not the imported dotenv function.
    from gateway import autoresearch_supervisor

    return autoresearch_supervisor.load_dotenv(dotenv_path)


def _shutdown_not_requested() -> bool:
    return False


class TaskGateway(Protocol):
    """Synchronous boundary for native, authenticated gateway control RPCs."""

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class NativeGatewayRPC:
    """One-shot native WebSocket gateway RPC client; it never launches the CLI."""

    host: str
    port: int
    token: str

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        if shutdown_requested():
            raise ShutdownInterrupted("OpenClaw gateway RPC interrupted during shutdown")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SupervisorError("Native gateway RPC requests require a synchronous caller")
        try:
            return asyncio.run(self._request(method, params, shutdown_requested))
        except OpenClawTransportError as exc:
            detail = f"OpenClaw gateway RPC failed ({method}): {exc}"
            if shutdown_requested():
                raise ShutdownInterrupted(detail) from exc
            raise OpenClawUnavailableError(detail) from exc
        except OpenClawError as exc:
            detail = f"OpenClaw gateway RPC failed ({method}): {exc}"
            if shutdown_requested():
                raise ShutdownInterrupted(detail) from exc
            raise SupervisorError(detail) from exc

    async def _request(
        self,
        method: str,
        params: Mapping[str, object],
        shutdown_requested: ShutdownRequested,
    ) -> Mapping[str, object]:
        client = OpenClawClient(self.host, self.port, self.token)
        request = asyncio.create_task(
            client.request_once(
                method,
                params,
                timeout_seconds=DEFAULT_TASK_RPC_TIMEOUT_SECONDS,
                required_server_version=REQUIRED_OPENCLAW_VERSION_TEXT,
            )
        )
        while not request.done():
            if shutdown_requested():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request
                raise ShutdownInterrupted("OpenClaw gateway RPC interrupted during shutdown")
            await asyncio.sleep(DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS)
        return await request


def _default_task_gateway() -> NativeGatewayRPC:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if not token:
        raise SupervisorError("OPENCLAW_GATEWAY_TOKEN is required for native task observation")
    raw_port = os.environ.get("OPENCLAW_PORT", "18789")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SupervisorError(
            "OPENCLAW_PORT must be an integer for native task observation"
        ) from exc
    if not 1 <= port <= 65535:
        raise SupervisorError("OPENCLAW_PORT must be between 1 and 65535")
    host = os.environ.get("OPENCLAW_HOST", "127.0.0.1").strip()
    if not host:
        raise SupervisorError("OPENCLAW_HOST must be non-empty for native task observation")
    return NativeGatewayRPC(host, port, token)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorError(f"duplicate OpenClaw session key: {key}")
        result[key] = value
    return result


def make_idempotency_key(*, purpose: str, material: str) -> str:
    """Produce a stable, bounded key for one logical owner-session request."""
    digest = hashlib.sha256(
        f"{purpose}\n{AUTORESEARCH_OWNER_SESSION_KEY}\n{material}".encode()
    ).hexdigest()
    return f"autoresearch-{purpose}-{digest}"


@dataclass(frozen=True, slots=True)
class WakeDeliveryProof:
    status: str
    run_id: str | None = None
    cached_terminal: bool = False


class OpenClawRPC:
    """Native authenticated gateway RPC boundary shared by owner control surfaces."""

    def __init__(
        self,
        gateway: TaskGateway | None = None,
    ) -> None:
        self._gateway = gateway or _default_task_gateway()

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        return self._gateway.request(method, params, shutdown_requested=shutdown_requested)

    def list_running_tasks(
        self,
        *,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        """Read OpenClaw running tasks with strict bounded retry for RPC failures."""
        last_error: OpenClawUnavailableError | None = None
        for attempt in range(1, READ_ONLY_TASK_LIST_ATTEMPTS + 1):
            try:
                return self._request(
                    "tasks.list",
                    {"status": "running", "limit": 500},
                    shutdown_requested=shutdown_requested,
                )
            except ShutdownInterrupted:
                if last_error is not None:
                    raise OpenClawUnavailableError(
                        "OpenClaw running task list failed before shutdown during retry: "
                        f"{last_error}"
                    ) from last_error
                raise
            except OpenClawUnavailableError as exc:
                if shutdown_requested():
                    raise ShutdownInterrupted(str(exc)) from exc
                last_error = exc
                if attempt >= READ_ONLY_TASK_LIST_ATTEMPTS:
                    break
                logger.warning(
                    "OpenClaw read-only task list failed; retrying (%s/%s): %s",
                    attempt,
                    READ_ONLY_TASK_LIST_ATTEMPTS,
                    exc,
                )
                time.sleep(READ_ONLY_TASK_LIST_RETRY_SECONDS)
                if shutdown_requested():
                    raise OpenClawUnavailableError(
                        "OpenClaw running task list failed before shutdown during retry: "
                        f"{last_error}"
                    ) from last_error
        raise OpenClawUnavailableError(
            "OpenClaw running task list failed after "
            f"{READ_ONLY_TASK_LIST_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def wake(
        self,
        *,
        message: str,
        idempotency_key: str,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> WakeDeliveryProof:
        params = json.dumps(
            {
                "message": message,
                "sessionKey": AUTORESEARCH_OWNER_SESSION_KEY,
                "idempotencyKey": idempotency_key,
            },
            separators=(",", ":"),
        )
        payload = self._request(
            "agent",
            json.loads(params, object_pairs_hook=_strict_json_object),
            shutdown_requested=shutdown_requested,
        )
        status = payload.get("status")
        if not isinstance(status, str):
            raise SupervisorError("OpenClaw wake response is missing a string status")
        session_key = payload.get("sessionKey")
        if status in {"accepted", "in_flight"} and session_key != AUTORESEARCH_OWNER_SESSION_KEY:
            raise SupervisorError(
                "OpenClaw wake response did not target the dedicated owner session"
            )
        if session_key is not None and session_key != AUTORESEARCH_OWNER_SESSION_KEY:
            raise SupervisorError(
                "OpenClaw wake response did not target the dedicated owner session"
            )
        run_id = payload.get("runId")
        if not isinstance(run_id, str) or not run_id.strip():
            raise SupervisorError("OpenClaw wake response is missing a non-empty runId")
        if status in {"accepted", "in_flight"}:
            return WakeDeliveryProof(status=status, run_id=run_id)
        if (
            set(payload) == {"runId", "status", "summary", "result"}
            and status == "ok"
            and payload.get("summary") == "completed"
            and isinstance(payload.get("result"), Mapping)
            and bool(payload["result"])
        ):
            return WakeDeliveryProof(
                status=status,
                run_id=run_id,
                cached_terminal=True,
            )
        raise SupervisorError(
            "OpenClaw wake response did not prove delivery to the dedicated owner session"
        )

    def delete_owner_session(self) -> bool:
        params = json.dumps(
            {
                "key": AUTORESEARCH_OWNER_SESSION_KEY,
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
                "deleteTranscript": False,
            },
            separators=(",", ":"),
        )
        payload = self._request(
            "sessions.delete", json.loads(params, object_pairs_hook=_strict_json_object)
        )
        if payload.get("ok") is not True or payload.get("key") != AUTORESEARCH_OWNER_SESSION_KEY:
            raise SupervisorError(
                "OpenClaw owner-session deletion response is malformed or mismatched"
            )
        if payload.get("deleted") is True:
            return True
        if payload.get("deleted") is False and payload.get("absent") is True:
            return False
        raise SupervisorError(
            "OpenClaw owner-session deletion response did not confirm deletion or absence"
        )

    def abort_owner_run(self, *, run_id: str) -> None:
        params = json.dumps(
            {
                "key": AUTORESEARCH_OWNER_SESSION_KEY,
                "runId": run_id,
                "agentId": AUTORESEARCH_OWNER_AGENT_ID,
            },
            separators=(",", ":"),
        )
        payload = self._request(
            "sessions.abort", json.loads(params, object_pairs_hook=_strict_json_object)
        )
        status = payload.get("status")
        aborted_run_id = payload.get("abortedRunId")
        if payload.get("ok") is not True or (
            (status == "aborted" and aborted_run_id != run_id)
            or (status == "no-active-run" and aborted_run_id is not None)
            or status not in {"aborted", "no-active-run"}
        ):
            raise SupervisorError(
                f"OpenClaw owner-run abort response is malformed or mismatched: {run_id}"
            )

    def cancel_task(self, *, task_id: str) -> None:
        params = json.dumps(
            {"taskId": task_id, "reason": "Quantipy autoresearch stopped by operator."},
            separators=(",", ":"),
        )
        payload = self._request(
            "tasks.cancel", json.loads(params, object_pairs_hook=_strict_json_object)
        )
        if payload.get("found") is not True or payload.get("cancelled") is not True:
            raise SupervisorError(
                f"OpenClaw task cancellation response is malformed or mismatched: {task_id}"
            )
        returned_task = payload.get("task")
        if returned_task is None:
            return
        if not isinstance(returned_task, Mapping):
            raise SupervisorError(
                f"OpenClaw task cancellation response has an invalid task: {task_id}"
            )
        returned_task_id = returned_task.get("taskId")
        returned_id = returned_task.get("id")
        if returned_task_id != task_id or (
            returned_id is not None and returned_id != returned_task_id
        ):
            raise SupervisorError(
                f"OpenClaw task cancellation response is malformed or mismatched: {task_id}"
            )

    def get_task(
        self,
        *,
        task_id: str,
        shutdown_requested: ShutdownRequested = _shutdown_not_requested,
    ) -> Mapping[str, object]:
        payload = self._request(
            "tasks.get",
            {"taskId": task_id},
            shutdown_requested=shutdown_requested,
        )
        task = payload.get("task")
        if not isinstance(task, Mapping):
            raise SupervisorError(f"OpenClaw tasks.get response has no object task: {task_id}")
        return task
