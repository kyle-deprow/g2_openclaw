from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
G2_APP = REPO_ROOT / "g2_app"
APP_JSON = G2_APP / "app.json"
EVENHUB_BIN = G2_APP / "node_modules/.bin/evenhub"


def test_app_json_uses_evenhub_0_1_13_permission_object_schema() -> None:
    manifest = json.loads(APP_JSON.read_text(encoding="utf-8"))

    assert manifest["edition"] == "202601"
    assert manifest["min_app_version"] == "2.0.0"
    assert manifest["min_sdk_version"] == "0.0.11"
    assert manifest["supported_languages"] == ["en"]
    assert manifest["permissions"] == [
        {
            "name": "network",
            "desc": "Connect to the local G2 OpenClaw gateway.",
            "whitelist": [],
        },
        {
            "name": "g2-microphone",
            "desc": "Capture voice commands from the G2 glasses microphone.",
        },
    ]
    assert all(isinstance(permission, dict) for permission in manifest["permissions"])


def test_installed_evenhub_cli_can_pack_manifest(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    output = tmp_path / "g2-openclaw.ehpk"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>OpenClaw</title>", encoding="utf-8")

    subprocess.run(
        [str(EVENHUB_BIN), "pack", str(APP_JSON), str(dist), "--output", str(output)],
        check=True,
        cwd=G2_APP,
        capture_output=True,
        text=True,
    )

    assert output.stat().st_size > 0
