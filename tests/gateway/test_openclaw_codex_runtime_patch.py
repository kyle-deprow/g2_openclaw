"""Regression tests for the fail-closed OpenClaw Codex runtime verifier."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_SCRIPT = REPO_ROOT / "scripts/ensure-openclaw-codex-runtime.mjs"
VULNERABLE_BRANCH = (
    "if(isIntentionalNativeAutoCompactionSkip(result))return{compacted:false,"
    "fallbackToContextEngine:true,failureReason:CODEX_APP_SERVER_OWNS_AUTO_COMPACTION_REASON};"
)
PATCH_MARKER = "g2_openclaw:codex-auto-compaction-no-fallback:v1"


def _fixture(
    tmp_path: Path, *, version: str = "2026.7.1-2", source: str = VULNERABLE_BRANCH
) -> tuple[Path, Path]:
    package_root = tmp_path / "node_modules/openclaw"
    dist = package_root / "dist"
    dist.mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps({"name": "openclaw", "version": version}), encoding="utf-8"
    )
    (dist / "cli-compaction-fixture.js").write_text(source, encoding="utf-8")
    binary = tmp_path / "openclaw"
    binary.write_text("#!/bin/sh\nprintf 'OpenClaw 2026.7.1-2\\n'\n", encoding="utf-8")
    binary.chmod(0o755)
    return package_root, binary


def _run(package_root: Path | None, binary: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["OPENCLAW_BIN"] = str(binary)
    if package_root is None:
        env.pop("OPENCLAW_PACKAGE_ROOT", None)
    else:
        env["OPENCLAW_PACKAGE_ROOT"] = str(package_root)
    return subprocess.run(
        ["node", str(PATCH_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_patcher_removes_codex_api_key_fallback_and_is_idempotent(tmp_path: Path) -> None:
    package_root, binary = _fixture(tmp_path)
    bundle = package_root / "dist/cli-compaction-fixture.js"

    first = _run(package_root, binary)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["status"] == "patched"
    patched = bundle.read_text(encoding="utf-8")
    assert "fallbackToContextEngine:true" not in patched
    assert PATCH_MARKER in patched

    second = _run(package_root, binary)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "verified"
    assert bundle.read_text(encoding="utf-8") == patched


def test_patcher_follows_explicit_shell_wrapper_to_pnpm_launcher(tmp_path: Path) -> None:
    package_root, _ = _fixture(tmp_path)
    launcher = tmp_path / "openclaw-launcher"
    launcher.write_text(
        f"""#!/bin/sh
# {package_root}/openclaw.mjs
printf 'OpenClaw 2026.7.1-2\\n'
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper = tmp_path / "openclaw-wrapper"
    wrapper.write_text(f'#!/bin/sh\nexec {launcher} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)

    result = _run(None, wrapper)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "patched"
    assert PATCH_MARKER in (package_root / "dist/cli-compaction-fixture.js").read_text(
        encoding="utf-8"
    )


def test_patcher_rejects_unknown_package_version_without_writing(tmp_path: Path) -> None:
    package_root, binary = _fixture(tmp_path, version="2026.7.2")
    bundle = package_root / "dist/cli-compaction-fixture.js"
    original = bundle.read_text(encoding="utf-8")

    result = _run(package_root, binary)

    assert result.returncode == 1
    assert "does not match CLI 2026.7.1-2" in result.stderr
    assert bundle.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "source",
    ["if (differentBranch) return true;", f"{VULNERABLE_BRANCH}{VULNERABLE_BRANCH}"],
)
def test_patcher_rejects_unknown_or_ambiguous_source_without_writing(
    tmp_path: Path, source: str
) -> None:
    package_root, binary = _fixture(tmp_path, source=source)
    bundle = package_root / "dist/cli-compaction-fixture.js"
    original = bundle.read_text(encoding="utf-8")

    result = _run(package_root, binary)

    assert result.returncode == 1
    assert "refusing to modify" in result.stderr
    assert bundle.read_text(encoding="utf-8") == original
