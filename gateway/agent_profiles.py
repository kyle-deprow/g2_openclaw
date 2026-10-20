"""Agent provider profiles: one setting selects the model provider for every agent."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn

PROFILES_CONTRACT = "g2-agent-profiles-v1"
DEPLOYED_CONTRACT = "g2-agent-profile-deployed-v1"
ROLE_NAMES: tuple[str, ...] = ("main", "owner", "implementer", "runner", "reviewer")
RUNTIMES = ("codex", "claude-cli")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROFILE_ENV = "G2_AGENT_PROFILE"
_CONFIG_DIR = Path(__file__).resolve().parent / "openclaw_config"
DEFAULT_PROFILES_PATH: Path = _CONFIG_DIR / "agent-profiles.json"
DEFAULT_ENV_FILE: Path = _CONFIG_DIR / ".env"
DEPLOYED_PROFILE_FILENAME = "g2-agent-profile.json"
# Dedicated Claude Code configuration directory for the OpenClaw gateway (CLAUDE_CONFIG_DIR):
# keeps the operator's personal Claude Code settings, hooks and plugins out of agent runs.
CLAUDE_CONFIG_DIRNAME = "claude-code"
_RUNTIME_PROVIDER = {"codex": "openai", "claude-cli": "anthropic"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FIELDS = ("model", "model_id", "provider", "effort", "service_tier")


class AgentProfileError(ValueError):
    """Raised for any invalid profile document, name, or deployed file."""


@dataclass(frozen=True, slots=True)
class RoleSpec:
    model_ref: str
    effort: str | None
    service_tier: str | None

    @property
    def provider(self) -> str:
        return self.model_ref.split("/", 1)[0]

    @property
    def model_id(self) -> str:
        return self.model_ref.split("/", 1)[1]


@dataclass(frozen=True, slots=True, eq=True)
class AgentProfile:
    name: str
    runtime: str
    provider: str
    roles: Mapping[str, RoleSpec]

    def role(self, name: str) -> RoleSpec:
        try:
            return self.roles[name]
        except KeyError:
            raise AgentProfileError(
                f"unknown role {name!r}; expected one of {ROLE_NAMES}"
            ) from None

    def __hash__(self) -> int:
        return hash(self.to_json())

    def to_dict(self) -> dict[str, object]:
        roles: dict[str, object] = {}
        for role_name in ROLE_NAMES:
            spec = self.roles[role_name]
            entry: dict[str, object] = {"model": spec.model_ref}
            if spec.effort is not None:
                entry["effort"] = spec.effort
            if spec.service_tier is not None:
                entry["service_tier"] = spec.service_tier
            roles[role_name] = entry
        return {
            "name": self.name,
            "runtime": self.runtime,
            "provider": self.provider,
            "roles": roles,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _expect_object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AgentProfileError(f"{where} must be an object")
    return {str(k): v for k, v in value.items()}


def _check_keys(
    obj: Mapping[str, object], required: set[str], optional: set[str], where: str
) -> None:
    missing = required - obj.keys()
    if missing:
        raise AgentProfileError(f"{where} is missing keys: {sorted(missing)}")
    unknown = obj.keys() - required - optional
    if unknown:
        raise AgentProfileError(f"{where} has unknown keys: {sorted(unknown)}")


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise AgentProfileError(f"{where} must be a non-empty string")
    return value


_MODEL_REF = re.compile(r"[^\s/]+/\S+")


def _parse_profile(name: str, raw: object) -> AgentProfile:
    where = f"profile {name!r}"
    if not _NAME.fullmatch(name):
        raise AgentProfileError(f"{where} name must match {_NAME.pattern}")
    obj = _expect_object(raw, where)
    _check_keys(obj, {"runtime", "provider", "roles"}, set(), where)
    runtime = _text(obj["runtime"], f"{where} runtime")
    if runtime not in RUNTIMES:
        raise AgentProfileError(f"{where} runtime {runtime!r} is not one of {RUNTIMES}")
    provider = _text(obj["provider"], f"{where} provider")
    if _RUNTIME_PROVIDER[runtime] != provider:
        raise AgentProfileError(
            f"{where} provider {provider!r} is not valid for runtime {runtime!r}"
        )
    roles_raw = _expect_object(obj["roles"], f"{where} roles")
    if set(roles_raw) != set(ROLE_NAMES):
        raise AgentProfileError(
            f"{where} must define exactly the roles {list(ROLE_NAMES)}, got {sorted(roles_raw)}"
        )
    roles: dict[str, RoleSpec] = {}
    for role_name in ROLE_NAMES:
        rwhere = f"{where} role {role_name!r}"
        role = _expect_object(roles_raw[role_name], rwhere)
        _check_keys(role, {"model"}, {"effort", "service_tier"}, rwhere)
        ref = _text(role["model"], f"{rwhere} model")
        if not _MODEL_REF.fullmatch(ref):
            raise AgentProfileError(f"{rwhere} model {ref!r} must be 'provider/model'")
        if ref.split("/", 1)[0] != provider:
            raise AgentProfileError(
                f"{rwhere} model provider differs from profile provider {provider!r}"
            )
        effort: str | None = None
        if "effort" in role:
            effort = _text(role["effort"], f"{rwhere} effort")
            if effort not in EFFORTS:
                raise AgentProfileError(f"{rwhere} effort {effort!r} is not one of {EFFORTS}")
        tier: str | None = None
        if "service_tier" in role:
            if runtime != "codex":
                raise AgentProfileError(f"{rwhere} service_tier is only allowed on codex profiles")
            tier = _text(role["service_tier"], f"{rwhere} service_tier")
            if not _NAME.fullmatch(tier):
                raise AgentProfileError(f"{rwhere} service_tier must match {_NAME.pattern}")
        roles[role_name] = RoleSpec(ref, effort, tier)
    return AgentProfile(name, runtime, provider, MappingProxyType(roles))


def _read_json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentProfileError(f"cannot read {what} {path}: {exc}") from exc


def _load_document(path: Path | None) -> tuple[str, dict[str, AgentProfile]]:
    source = path or DEFAULT_PROFILES_PATH
    doc = _expect_object(_read_json(source, "profiles file"), "profiles document")
    if doc.get("contract") != PROFILES_CONTRACT:
        raise AgentProfileError(f"profiles contract must be {PROFILES_CONTRACT!r}")
    _check_keys(doc, {"contract", "default_profile", "profiles"}, set(), "profiles document")
    default = _text(doc["default_profile"], "default_profile")
    raw_profiles = _expect_object(doc["profiles"], "profiles")
    if not raw_profiles:
        raise AgentProfileError("profiles must define at least one profile")
    profiles = {name: _parse_profile(name, raw) for name, raw in raw_profiles.items()}
    if default not in profiles:
        raise AgentProfileError(f"default_profile {default!r} is not a defined profile")
    return default, profiles


def load_profiles(path: Path | None = None) -> dict[str, AgentProfile]:
    return _load_document(path)[1]


def _env_file_value(env_file: Path) -> str:
    """Parse PROFILE_ENV like scripts/push-openclaw-config.sh load_owner_env_data_only."""
    try:
        text = env_file.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        raise AgentProfileError(f"cannot read env file {env_file}: {exc}") from exc
    value: str | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        where = f"{env_file}:{number}"
        if "\r" in line:
            raise AgentProfileError(f"{where}: carriage returns are not allowed")
        trimmed = line.lstrip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if re.match(r"export\s", trimmed):
            trimmed = trimmed[len("export") :].lstrip()
        if "=" not in trimmed:
            continue
        key, _, raw = trimmed.partition("=")
        if key.strip() != PROFILE_ENV:
            continue
        if value is not None:
            raise AgentProfileError(f"{where}: duplicate key {PROFILE_ENV}")
        raw = raw.lstrip()
        if raw[:1] in ("'", '"'):
            if len(raw) < 2 or raw[-1] != raw[0]:
                raise AgentProfileError(f"{where}: unterminated quoted value for {PROFILE_ENV}")
            value = raw[1:-1]
        else:
            value = raw.rstrip()
        if any(ch in value for ch in "\n\r\t"):
            raise AgentProfileError(f"{where}: control characters are not allowed")
    return value or ""


def resolve_profile_name(
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    *,
    _profiles_path: Path | None = None,
) -> str:
    source = os.environ if env is None else env
    name = source.get(PROFILE_ENV, "").strip()
    if name:
        return name
    name = _env_file_value(DEFAULT_ENV_FILE if env_file is None else env_file)
    if name:
        return name
    return _load_document(_profiles_path)[0]


def load_profile(
    name: str | None = None,
    *,
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> AgentProfile:
    profiles = load_profiles(path)
    chosen = name or resolve_profile_name(env, env_file, _profiles_path=path)
    if chosen not in profiles:
        raise AgentProfileError(f"unknown profile {chosen!r}; defined: {sorted(profiles)}")
    return profiles[chosen]


def deployed_profile_path(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        env_dir = os.environ.get("OPENCLAW_STATE_DIR", "")
        state_dir = Path(env_dir) if env_dir else Path("~/.openclaw")
    resolved = state_dir.expanduser()
    if not resolved.is_absolute():
        raise AgentProfileError(f"state dir {str(state_dir)!r} must be an absolute path")
    return resolved / DEPLOYED_PROFILE_FILENAME


def claude_config_dir(state_dir: Path | None = None) -> Path:
    """The gateway's dedicated CLAUDE_CONFIG_DIR, a sibling of the deployed profile file."""
    return deployed_profile_path(state_dir).parent / CLAUDE_CONFIG_DIRNAME


def write_deployed_profile(profile: AgentProfile, state_dir: Path | None = None) -> Path:
    target = deployed_profile_path(state_dir)
    payload = {
        "contract": DEPLOYED_CONTRACT,
        "profile": profile.name,
        "profile_sha256": profile.sha256,
        "definition": profile.to_dict(),
    }
    data = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    tmp_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
        tmp_name = None
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise AgentProfileError(f"cannot write deployed profile {target}: {exc}") from exc
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    return target


def read_deployed_profile(state_dir: Path | None = None) -> AgentProfile | None:
    target = deployed_profile_path(state_dir)
    try:
        raw_text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if target.is_symlink():
            raise AgentProfileError(f"deployed profile {target} is a dangling symlink") from None
        return None
    except (OSError, ValueError) as exc:
        raise AgentProfileError(f"cannot read deployed profile {target}: {exc}") from exc
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise AgentProfileError(f"deployed profile {target} is not valid JSON: {exc}") from exc
    doc = _expect_object(parsed, "deployed profile")
    _check_keys(doc, {"contract", "profile", "profile_sha256", "definition"}, set(), "deployed")
    if doc["contract"] != DEPLOYED_CONTRACT:
        raise AgentProfileError(f"deployed contract must be {DEPLOYED_CONTRACT!r}")
    name = _text(doc["profile"], "deployed profile name")
    definition = _expect_object(doc["definition"], "deployed definition")
    if definition.get("name") != name:
        raise AgentProfileError("deployed definition name does not match profile")
    body = {k: v for k, v in definition.items() if k != "name"}
    profile = _parse_profile(name, body)
    if doc["profile_sha256"] != profile.sha256:
        raise AgentProfileError("deployed profile_sha256 does not match its definition")
    return profile


def active_runtime_profile(state_dir: Path | None = None) -> AgentProfile:
    deployed = read_deployed_profile(state_dir)
    if deployed is not None:
        return deployed
    profiles = load_profiles()
    if "codex" not in profiles:
        raise AgentProfileError("repo profiles file does not define the codex profile")
    return profiles["codex"]


def _format_text(profile: AgentProfile) -> str:
    lines = []
    for role_name in ROLE_NAMES:
        spec = profile.roles[role_name]
        lines.append(
            f"{role_name} {spec.model_ref} {spec.effort or '-'} {spec.service_tier or '-'}"
        )
    return "\n".join(lines)


def _get_field(spec: RoleSpec, field: str) -> str:
    values = {
        "model": spec.model_ref,
        "model_id": spec.model_id,
        "provider": spec.provider,
        "effort": spec.effort,
        "service_tier": spec.service_tier,
    }
    return values[field] or ""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(64)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m gateway.agent_profiles")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    show = sub.add_parser("show")
    show.add_argument("--profile")
    show.add_argument("--json", action="store_true")
    show.add_argument("--env-file", type=Path)
    get = sub.add_parser("get")
    get.add_argument("role")
    get.add_argument("field", choices=_FIELDS)
    get.add_argument("--profile")
    get.add_argument("--env-file", type=Path)
    res = sub.add_parser("resolve")
    res.add_argument("--env-file", type=Path)
    wd = sub.add_parser("write-deployed")
    wd.add_argument("--profile")
    wd.add_argument("--state-dir", type=Path)
    act = sub.add_parser("active")
    act.add_argument("--state-dir", type=Path)
    act.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            print(resolve_profile_name(env_file=args.env_file))
        elif args.command == "show":
            profile = load_profile(args.profile, env_file=args.env_file)
            print(profile.to_json() if args.json else _format_text(profile))
        elif args.command == "get":
            profile = load_profile(args.profile, env_file=args.env_file)
            print(_get_field(profile.role(args.role), args.field))
        elif args.command == "write-deployed":
            print(write_deployed_profile(load_profile(args.profile), args.state_dir))
        else:
            profile = active_runtime_profile(args.state_dir)
            print(profile.to_json() if args.json else _format_text(profile))
    except AgentProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
