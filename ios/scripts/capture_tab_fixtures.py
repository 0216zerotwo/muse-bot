"""GET-only capture through the project's redacting probe; write neutral XCTest fixtures.

Raw responses stay in subprocess memory, never on disk. This helper does not import server
configuration or read credentials. Run from the workspace root with the project's Python.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
ENDPOINTS = {
    "tabs-feed": "/api/webui/companion/feed",
    "tabs-ideas": "/api/webui/companion/ideas",
    "tabs-goals": "/api/webui/companion/goals",
    "tabs-routines": "/api/webui/companion/routines",
    "tabs-helpers": "/api/webui/companion/helpers",
    "tabs-creations": "/api/webui/companion/creations",
    "tabs-triggers": "/api/webui/companion/triggers",
    "tabs-automations": "/api/webui/automations",
}
ENUM_FIELDS = {"kind", "status", "last_status", "last_error", "origin", "area", "by", "action", "role", "preset", "expr", "tz", "time", "created_at", "emoji"}


def neutral(value, key="", ids=None):
    if ids is None:
        ids = {}
    if isinstance(value, dict):
        return {k: neutral(v, k, ids) for k, v in value.items()}
    if isinstance(value, list):
        # Keep representative structures, including populated work/habits and each media kind.
        if value and all(isinstance(item, dict) for item in value):
            chosen = []
            shapes = set()
            for item in value:
                shape = (tuple(sorted(item)), item.get("kind"), bool(item.get("work")), bool(item.get("habits")), type(item.get("image")).__name__, type(item.get("prompt")).__name__)
                if shape not in shapes:
                    chosen.append(item)
                    shapes.add(shape)
            value = chosen
        return [neutral(item, key, ids) for item in value]
    if isinstance(value, str):
        if not value:
            return value
        if key in {"id", "chat_id", "turn_id", "members"}:
            if value not in ids:
                ids[value] = "example-" + str(len(ids) + 1)
            return ids[value]
        if key in ENUM_FIELDS:
            return value
        if key in {"url", "image", "hook_url"}:
            return "/api/webui/companion/creations/file/example-signature/example.png" if value.startswith("/") else "https://example.com/image.png"
        if key == "avatar":
            return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6lOwAAAAASUVORK5CYII="
        if key in {"name", "title"}:
            return "示例名称"
        if key == "model":
            return "example-model"
        if key == "instructions":
            return "保持内容简洁，展示示例。"
        return "中性示例内容"
    return value


def main():
    output = ROOT / "CompanionTests" / "Fixtures"
    output.mkdir(parents=True, exist_ok=True)
    for name, endpoint in ENDPOINTS.items():
        response = subprocess.run(
            [str(WORKSPACE / "hermes-env/Scripts/python.exe"), "-X", "utf8", str(WORKSPACE / "tools/companion_api_probe.py"), "get", endpoint],
            cwd=WORKSPACE, capture_output=True, encoding="utf-8", check=True,
        )
        payload = json.loads(response.stdout)
        safe = neutral(payload)
        (output / (name + ".json")).write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("PASS GET", endpoint, "fields:", ", ".join(payload))


if __name__ == "__main__":
    main()
