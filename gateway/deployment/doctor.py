"""Validate the managed Codex doctor report during deployment."""

# ruff: noqa: E501, SIM114

import argparse
import sys
from collections.abc import Sequence
from typing import TypeGuard


def validate() -> None:
    """Validate the managed Codex doctor report."""
    import json
    import re
    import sys
    from pathlib import Path

    stdout_path = Path(sys.argv[1])
    stderr_path = Path(sys.argv[2])
    codex_home = Path(sys.argv[3])
    config_path = Path(sys.argv[4])
    required_version = sys.argv[5]
    app_server_package_root = Path(sys.argv[6])
    doctor_status = int(sys.argv[7])

    try:
        report = json.loads(stdout_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"doctor did not emit valid JSON for {config_path}: {exc}") from exc

    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise SystemExit(f"doctor JSON for {config_path} is missing checks")

    required_checks = {
        "config.load",
        "mcp.config",
        "sandbox.helpers",
        "runtime.provenance",
    }
    missing = sorted(required_checks - set(checks))
    if missing:
        raise SystemExit(
            f"doctor JSON for {config_path} is missing owned checks: {', '.join(missing)}"
        )

    failures: list[str] = []
    for check_id in sorted(required_checks):
        check = checks[check_id]
        if not isinstance(check, dict):
            failures.append(f"{check_id}=invalid")
        elif check.get("status") != "ok":
            failures.append(f"{check_id}={check.get('status', '<missing>')}")

    if report.get("codexVersion") != required_version:
        failures.append(
            f"codexVersion={report.get('codexVersion', '<missing>')} expected {required_version}"
        )

    runtime = checks["runtime.provenance"]
    runtime_details = runtime.get("details") if isinstance(runtime, dict) else None
    if not isinstance(runtime_details, dict) or runtime_details.get("version") != required_version:
        actual = (
            runtime_details.get("version", "<missing>")
            if isinstance(runtime_details, dict)
            else "<missing>"
        )
        failures.append(f"runtime.provenance.details.version={actual} expected {required_version}")

    ignored: list[str] = []
    unexpected: list[str] = []

    def has_shape(
        check_id: str,
        check: dict[str, object],
        *,
        category: str,
        status: str,
        summary: str,
        details: dict[str, object],
    ) -> bool:
        if check.get("id") != check_id:
            return False
        if check.get("category") != category:
            return False
        if check.get("status") != status:
            return False
        if check.get("summary") != summary:
            return False
        actual_details = check.get("details")
        if not isinstance(actual_details, dict):
            return False
        return actual_details == details

    def is_abs_path(value: object) -> TypeGuard[str]:
        return isinstance(value, str) and value.startswith("/")

    def is_semver(value: object) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", value) is not None
        )

    def is_known_curl_timeout(value: object) -> bool:
        return value == "curl: (28) Resolving timed out after 5000 milliseconds"

    def detail_string(details: dict[str, object], key: str) -> str | None:
        value = details.get(key)
        return value if isinstance(value, str) else None

    def path_codex_entries_are_structural(details: dict[str, object]) -> bool:
        raw_count = detail_string(details, "PATH codex entries")
        if raw_count is None or not raw_count.isdecimal():
            return False
        count = int(raw_count)
        path_keys: list[tuple[int, str]] = []
        for key, value in details.items():
            match = re.fullmatch(r"PATH codex #([0-9]+)", key)
            if match is None:
                continue
            if not is_abs_path(value) or Path(value).name != "codex":
                return False
            path_keys.append((int(match.group(1)), key))
        if len(path_keys) != count:
            return False
        return sorted(number for number, _ in path_keys) == list(range(1, count + 1))

    def install_context_native_root(details: dict[str, object]) -> str | None:
        install_context = detail_string(details, "install context")
        if install_context is None:
            return None
        match = re.fullmatch(
            r"npm \(package (/.+), bin \1/bin, resources \1/codex-resources, path \1/codex-path\)",
            install_context,
        )
        if match is None:
            return None
        native_root = match.group(1)
        if "/@openai/codex-" not in native_root or "/vendor/" not in native_root:
            return None
        return native_root

    def details_have_exact_keys(details: dict[str, object], expected: set[str]) -> bool:
        return set(details) == expected

    def is_expected_openclaw_managed_auth_failure(check_id: str, check: dict[str, object]) -> bool:
        return (
            has_shape(
                check_id,
                check,
                category="auth",
                status="fail",
                summary="no Codex credentials were found",
                details={
                    "auth file": str(codex_home / "auth.json"),
                    "auth storage mode": "File",
                },
            )
            and not (codex_home / "auth.json").exists()
        )

    def is_expected_embedded_installation_failure(check_id: str, check: dict[str, object]) -> bool:
        if check.get("id") != check_id:
            return False
        if check.get("category") != "install":
            return False
        if check.get("status") != "fail":
            return False
        if check.get("summary") != "npm install -g @openai/codex would update a different install":
            return False
        details = check.get("details")
        if not isinstance(details, dict) or not path_codex_entries_are_structural(details):
            return False
        path_keys = {key for key in details if re.fullmatch(r"PATH codex #[0-9]+", key)}
        expected_keys = {
            "PATH codex entries",
            "current executable",
            "install context",
            "managed by bun",
            "managed by npm",
            "managed by pnpm",
            "managed package root",
            "npm package root",
            "running package root",
            *path_keys,
        }
        native_root = install_context_native_root(details)
        return (
            details_have_exact_keys(details, expected_keys)
            and native_root is not None
            and details.get("current executable") == f"{native_root}/bin/codex"
            and details.get("managed by bun") == "false"
            and details.get("managed by npm") == "true"
            and details.get("managed by pnpm") == "false"
            and details.get("managed package root") == str(app_server_package_root)
            and details.get("running package root") == str(app_server_package_root)
            and is_abs_path(details.get("npm package root"))
            and details.get("npm package root") != str(app_server_package_root)
        )

    def is_expected_update_probe_failure(check_id: str, check: dict[str, object]) -> bool:
        if check.get("id") != check_id:
            return False
        details = check.get("details")
        if not isinstance(details, dict):
            return False
        mismatch = (
            check.get("category") == "updates"
            and check.get("status") == "fail"
            and check.get("summary") == "update would target a different npm install"
            and details_have_exact_keys(
                details,
                {
                    "check for update on startup",
                    "latest version",
                    "latest version status",
                    "npm package root",
                    "running package root",
                    "update action",
                    "version cache",
                },
            )
            and details.get("check for update on startup") == "true"
            and is_semver(details.get("latest version"))
            and details.get("latest version status") == "newer version is available"
            and is_abs_path(details.get("npm package root"))
            and details.get("npm package root") != str(app_server_package_root)
            and details.get("running package root") == str(app_server_package_root)
            and details.get("update action") == "npm install -g @openai/codex"
            and details.get("version cache") == [str(codex_home / "version.json"), "missing"]
        )
        probe_timeout = (
            check.get("category") == "updates"
            and check.get("status") == "fail"
            and check.get("summary") == "update would target a different npm install"
            and details_have_exact_keys(
                details,
                {
                    "check for update on startup",
                    "latest version probe",
                    "npm package root",
                    "running package root",
                    "update action",
                    "version cache",
                },
            )
            and details.get("check for update on startup") == "true"
            and is_known_curl_timeout(details.get("latest version probe"))
            and is_abs_path(details.get("npm package root"))
            and details.get("npm package root") != str(app_server_package_root)
            and details.get("running package root") == str(app_server_package_root)
            and details.get("update action") == "npm install -g @openai/codex"
            and details.get("version cache") == [str(codex_home / "version.json"), "missing"]
        )
        timeout = has_shape(
            check_id,
            check,
            category="updates",
            status="warning",
            summary="update check timed out",
            details={"running package root": str(app_server_package_root)},
        )
        return mismatch or probe_timeout or timeout

    def is_expected_missing_auth_websocket_warning(check_id: str, check: dict[str, object]) -> bool:
        if check.get("id") != check_id:
            return False
        if check.get("category") != "websocket":
            return False
        if check.get("status") != "warning":
            return False
        if check.get("summary") != "Responses WebSocket failed; HTTPS fallback may still work":
            return False
        details = check.get("details")
        if not isinstance(details, dict):
            return False
        return (
            details_have_exact_keys(
                details,
                {
                    "DNS",
                    "auth mode",
                    "connect timeout",
                    "endpoint",
                    "handshake transport error",
                    "model provider",
                    "provider name",
                    "proxy env vars",
                    "supports websockets",
                    "wire API",
                },
            )
            and isinstance(details.get("DNS"), str)
            and re.fullmatch(r"[0-9]+ IPv4, [0-9]+ IPv6, first (IPv4|IPv6|none)", details["DNS"])
            is not None
            and details.get("auth mode") == "none"
            and isinstance(details.get("connect timeout"), str)
            and re.fullmatch(r"[0-9]+ ms", details["connect timeout"]) is not None
            and details.get("endpoint") == "wss://api.openai.com/v1/<redacted>"
            and isinstance(details.get("handshake transport error"), str)
            and details["handshake transport error"].startswith("http 401 Unauthorized:")
            and details.get("model provider") == "openai"
            and details.get("provider name") == "OpenAI"
            and details.get("proxy env vars") == "none"
            and details.get("supports websockets") == "true"
            and details.get("wire API") == "responses"
            and not (codex_home / "auth.json").exists()
        )

    for check_id, raw_check in checks.items():
        if not isinstance(raw_check, dict):
            unexpected.append(f"{check_id}=invalid")
            continue
        status = raw_check.get("status")
        if check_id == "auth.credentials":
            if status == "fail" and is_expected_openclaw_managed_auth_failure(check_id, raw_check):
                ignored.append(check_id)
            else:
                unexpected.append(f"{check_id}={status}")
            continue
        if status == "ok":
            continue
        if status not in {"fail", "warning"}:
            unexpected.append(f"{check_id}={status}")
            continue
        if check_id == "installation" and is_expected_embedded_installation_failure(
            check_id, raw_check
        ):
            ignored.append(check_id)
        elif check_id == "updates.status" and is_expected_update_probe_failure(check_id, raw_check):
            ignored.append(check_id)
        elif (
            check_id == "network.websocket_reachability"
            and is_expected_missing_auth_websocket_warning(check_id, raw_check)
        ):
            ignored.append(check_id)
        else:
            unexpected.append(f"{check_id}={status}")

    if failures:
        raise SystemExit(
            f"owned Codex doctor checks failed for {config_path}: {', '.join(failures)}"
        )
    if unexpected:
        raise SystemExit(
            f"unexpected fatal Codex doctor checks for {config_path}: {', '.join(sorted(unexpected))}"
        )

    if doctor_status == 0:
        if ignored:
            raise SystemExit(
                f"doctor exited 0 for {config_path} with non-ok checks: {', '.join(sorted(ignored))}"
            )
    elif doctor_status == 1:
        if ignored:
            print(
                "Codex doctor non-owned failures ignored for "
                f"{config_path}: {', '.join(sorted(ignored))}. "
                "OpenClaw owns OAuth in openclaw-agent.sqlite and embeds the pinned Codex package."
            )
        else:
            raise SystemExit(
                f"doctor exited 1 for {config_path} without an allowed non-owned failure"
            )
    else:
        raise SystemExit(f"doctor exited {doctor_status} for {config_path}; expected 0 or 1")

    stderr = stderr_path.read_text(encoding="utf-8").strip()
    if stderr:
        print(f"Codex doctor stderr for {config_path}: {stderr}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the Codex doctor validation command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("doctor_stdout")
    validate_parser.add_argument("doctor_stderr")
    validate_parser.add_argument("codex_home")
    validate_parser.add_argument("config_path")
    validate_parser.add_argument("required_version")
    validate_parser.add_argument("app_server_package_root")
    validate_parser.add_argument("doctor_status")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch Codex doctor validation."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        sys.argv = [
            sys.argv[0],
            args.doctor_stdout,
            args.doctor_stderr,
            args.codex_home,
            args.config_path,
            args.required_version,
            args.app_server_package_root,
            args.doctor_status,
        ]
        validate()
        return 0
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
