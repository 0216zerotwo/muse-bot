"""Capture only GET probe responses, sanitizing them before anything is written."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "ios" / "CompanionTests" / "Fixtures"
PATHS = {
    "settings-models": "/api/webui/companion/models",
    "settings-model-catalog": "/api/webui/companion/model-catalog",
    "settings-usage": "/api/webui/companion/usage",
    "settings-skills": "/api/webui/companion/skills",
    "settings-permissions": "/api/webui/companion/permissions",
    "settings-vault": "/api/webui/companion/vault",
    "settings-devices": "/api/webui/devices",
    "settings-services": "/api/settings/mcp-presets",
}
TEXT = {"description", "note", "label", "identifier", "connection_summary", "error"}
IDS = {}


def clean(value, key=""):
    if isinstance(value, dict):
        return {k: clean(v, k) for k, v in value.items()}
    if isinstance(value, list):
        sample = value[:2] if any(isinstance(item, dict) for item in value) else value
        return [clean(item) for item in sample]
    if isinstance(value, str):
        if key in TEXT and value:
            return "Neutral example"
        if key in {"origin", "url", "link"} and value:
            return "https://example.invalid/"
        if key in {"id", "name"} and value and key == "id":
            return IDS.setdefault(value, f"example-id-{len(IDS) + 1}")
        return value[:200]
    return value


if __name__ == "__main__":
    DEST.mkdir(parents=True, exist_ok=True)
    for name, path in PATHS.items():
        result = subprocess.run(
            [str(ROOT / "hermes-env/Scripts/python.exe"), "-X", "utf8",
             str(ROOT / "tools/companion_api_probe.py"), "get", path],
            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=60, check=True,
        )
        parsed = json.loads(result.stdout)
        (DEST / f"{name}.json").write_text(json.dumps(clean(parsed), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"GET {path}: saved sanitized fixture; root fields: {', '.join(parsed)}")
