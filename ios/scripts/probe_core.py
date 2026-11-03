"""GET-only core contract fixtures, sanitized before writing or displaying.

Runs the project's authorized probe. Never saves raw responses or reads credential
files. Bootstrap is deliberately excluded: its response contains live credentials.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ios/CompanionTests/Fixtures"
ENUMS = {"event", "kind", "role", "type", "phase", "turn_phase", "projection",
         "theme_mode", "pet_size", "tone", "status", "decision", "scope", "scopes", "runtime_surface"}
IDS = {"id", "chat_id", "turn_id", "stream_id", "helper", "message_id", "call_id", "key",
       "sessionKey", "avatar", "members", "completed_turn_ids"}


def request(path):
    result = subprocess.run([str(ROOT / "hermes-env/Scripts/python.exe"), "-X", "utf8",
                             str(ROOT / "tools/companion_api_probe.py"), "get", path],
                            cwd=ROOT, check=True, capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


def clean(value, key=""):
    if isinstance(value, dict):
        return {k: clean(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(item, key) for item in value]
    if not isinstance(value, str) or not value:
        return value
    if key.lower() in {"token", "api_token", "password", "api_key", "secret", "authorization"}:
        return "REDACTED"
    if key in ENUMS:
        return value
    if key in IDS:
        if value.startswith("websocket:"):
            return "websocket:" + clean(value.split(":", 1)[1], "id")
        if value.startswith("image:"):
            return "image:" + clean(value.split(":", 1)[1], "id")
        return "example-" + hashlib.sha256(value.encode()).hexdigest()[:8]
    if value.startswith("data:image/"):
        return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nV8AAAAASUVORK5CYII="
    if key.endswith("url") or key in {"idle", "thinking", "working", "waiting"}:
        return "/api/media/example/asset.webp"
    if key in {"created_at", "updated_at"}:
        return "2026-10-29T12:00:00Z"
    if key == "timezone":
        return "Asia/Taipei"
    if key == "revision":
        return value
    return "示例内容"


def save(name, path, body=None):
    data = request(path) if body is None else body
    sanitized = clean(data)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (name + ".json")).write_text(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("GET", path.split("?")[0], "OK; sanitized fixture", name)
    return data


def main():
    state = save("core-state", "/api/webui/companion")
    save("core-sessions", "/api/sessions")
    save("core-avatars", "/api/webui/companion/avatars")
    save("core-helpers", "/api/webui/companion/helpers")
    save("core-approvals", "/api/webui/approvals?session_key=" + quote("websocket:" + state["chat_id"], safe=""))
    key = quote("websocket:" + state["chat_id"], safe="")
    thread = request("/api/sessions/" + key + "/webui-thread?limit=80&direction=latest")
    # Keep one example per event field layout, all original field names/types.
    examples = {}
    for row in thread.get("events", []):
        layout = (row.get("event"), tuple(sorted(row.keys())))
        examples.setdefault(layout, row)
    shortened = {**thread, "events": list(examples.values())}
    save("core-thread", "/api/sessions/{key}/webui-thread", shortened)
    print("Thread fixture keeps", len(examples), "observed event layouts.")


if __name__ == "__main__":
    main()
