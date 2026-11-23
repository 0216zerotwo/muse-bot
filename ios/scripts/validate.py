"""Static project checks. This does not compile Swift or contact a server.

Optional grammar dependencies can be isolated inside the ignored .validation/python:
  python -m pip install --target .validation/python --only-binary=:all: --no-cache-dir \
      tree-sitter==0.23.2 tree-sitter-swift==0.0.1
  python scripts/validate.py --require-swift

The listed grammar versions work with the Windows Python 3.9 wheel. A newer compatible
parser/grammar pair may also be used. Grammar parsing does not type-check Swift/UIKit APIs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import re
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def _ValidationPaths() -> list[Path]:
    paths = [ROOT / ".validation" / "python"]
    git_marker = ROOT.parent / ".git"
    if git_marker.is_file():
        text = git_marker.read_text(encoding="utf-8", errors="ignore").strip()
        if text.startswith("gitdir:"):
            raw = text.split(":", 1)[1].strip()
            git_dir = Path(raw) if Path(raw).is_absolute() else (ROOT.parent / raw).resolve()
            common = git_dir / "commondir"
            if common.is_file():
                rel = common.read_text(encoding="utf-8", errors="ignore").strip()
                main_git = (git_dir / rel).resolve()
                paths.append(main_git.parent / "ios" / ".validation" / "python")
    return paths


for _candidate in reversed(_ValidationPaths()):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


def _load_yaml(text: str):
    try:
        import yaml

        return yaml.load(text, Loader=yaml.BaseLoader)
    except ImportError:
        from ruamel.yaml import YAML

        loader = YAML(typ="base")
        return loader.load(text)


def check_project() -> None:
    project = _load_yaml((ROOT / "project.yml").read_text(encoding="utf-8"))
    workflow_text = (ROOT.parent / ".github/workflows/ios.yml").read_text(encoding="utf-8")
    workflow = _load_yaml(workflow_text)
    assert project["targets"]["Companion"]["settings"]["base"]["PRODUCT_BUNDLE_IDENTIFIER"] == "com.personalbot.companion"
    assert project["options"]["deploymentTarget"]["iOS"] == "17.0"
    assert project["settings"]["base"]["SWIFT_VERSION"] in ("5", "5.0", "6", "6.0")
    assert project["packages"]["MarkdownUI"]["exactVersion"] == "2.4.1"
    assert "on" in workflow and "workflow_dispatch" in workflow["on"]
    assert workflow["jobs"]["build"]["needs"] == "test"
    allowed_actions = {"actions/checkout", "actions/upload-artifact", "actions/cache"}
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == "macos-latest", name
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
                action, _ = step["uses"].split("@", 1)
                assert action in allowed_actions, f"Unexpected unapproved GitHub Action: {action}"
    assert "CODE_SIGNING_ALLOWED=NO" in workflow_text
    assert "Xcode_26" in workflow_text, "Workflow must select or prefer Xcode 26"
    app_sources = project["targets"]["Companion"]["sources"]
    assert any(item["path"] == "Companion" for item in app_sources)
    assert any(item["path"] == "Companion/Resources" and item.get("buildPhase") == "resources" for item in app_sources)
    assert project["targets"]["CompanionTests"]["sources"][0]["path"] == "CompanionTests"
    assert any(item["path"] == "CompanionTests/Fixtures" and item.get("buildPhase") == "resources"
               for item in project["targets"]["CompanionTests"]["sources"])
    assert "CompanionUITests" in project["targets"]
    assert project["targets"]["CompanionUITests"]["sources"][0]["path"] == "CompanionUITests"
    print("PASS YAML: project and workflow; identifiers, source roots, SHA-pinned actions/package, unsigned build")


def check_fixtures_and_screens() -> None:
    fixtures = sorted((ROOT / "CompanionTests/Fixtures").glob("*.json"))
    assert len(fixtures) >= 26, "Each JSON data endpoint needs a neutralized observed response"
    for fixture in fixtures:
        json.loads(fixture.read_text(encoding="utf-8"))
    files = sorted((ROOT / "Companion").rglob("*.swift"))
    sources = "\n".join(file.read_text(encoding="utf-8") for file in files)
    assert "ComingSoonView" not in sources and "即将推出" not in sources
    for screen in ["FeedScreen", "IdeasScreen", "GoalsScreen", "HelpersScreen", "LibraryScreen", "SettingsView",
                   "AppearanceSettingsPage", "ModelsSettingsPage", "UsageSettingsPage", "SkillsSettingsPage",
                   "ConnectionsSettingsPage", "PermissionsSettingsPage", "VaultSettingsPage", "DevicesSettingsPage",
                   "IdentityView", "ComputerView", "GoalBannerView"]:
        assert re.search(r"struct\s+" + screen + r"\s*:", sources), screen
    tests = "\n".join(file.read_text(encoding="utf-8") for file in sorted((ROOT / "CompanionTests").glob("*.swift")))
    count = len(re.findall(r"func test\w+\s*\(", tests))
    print(f"PASS feature coverage: six tabs, eight settings pages, identity/browser/goal banner, {len(fixtures)} JSON fixtures, {count} XCTest methods (not executed)")


def check_resources() -> None:
    with (ROOT / "Companion/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleDisplayName"] == "Muse Bot"
    assert info["NSAppTransportSecurity"]["NSAllowsArbitraryLoads"]
    for key in ["NSLocalNetworkUsageDescription", "NSCameraUsageDescription", "NSPhotoLibraryUsageDescription",
                "NSPhotoLibraryAddUsageDescription", "NSMicrophoneUsageDescription"]:
        assert info[key], key
    ET.parse(ROOT / "Companion/Resources/LaunchScreen.storyboard")
    count = 0
    for file in (ROOT / "Companion/Resources/Assets.xcassets").rglob("Contents.json"):
        asset = json.loads(file.read_text(encoding="utf-8"))
        for image in asset.get("images", []):
            if "filename" in image:
                assert (file.parent / image["filename"]).is_file(), image["filename"]
                count += 1
    uikit_root = ROOT / "Companion/Resources/UiKit"
    for rel in [
        "kit.css",
        "kit.js",
        "vendor/chart.umd.js",
        "vendor/leaflet/leaflet.css",
        "vendor/leaflet/leaflet.js",
        "vendor/leaflet/images/layers.png",
        "vendor/leaflet/images/layers-2x.png",
        "vendor/leaflet/images/marker-icon.png",
        "vendor/leaflet/images/marker-icon-2x.png",
        "vendor/leaflet/images/marker-shadow.png",
    ]:
        target = uikit_root / rel
        assert target.is_file() and target.stat().st_size > 0, f"Missing UiKit asset: {rel}"
    faces_text = (ROOT / "Companion/Views/Faces.swift").read_text(encoding="utf-8")
    names_match = re.search(r"names:\s*\[String:\s*String\]\s*=\s*\[(.*?)\]", faces_text, re.S)
    assert names_match, "Could not locate DrawnIcons.names in Faces.swift"
    icon_names = sorted(set(re.findall(r':\s*"([a-z0-9_-]+)"', names_match.group(1))))
    assert len(icon_names) >= 20, "Expected DrawnIcons icon mappings"
    icons_dir = ROOT / "Companion/Resources/Icons"
    for icon_name in icon_names:
        icon_file = icons_dir / f"{icon_name}.webp"
        assert icon_file.is_file() and icon_file.stat().st_size > 0, f"Missing drawn icon: {icon_name}.webp"
    print(f"PASS resources: plist, launch XML, {count} xcassets, {len(icon_names)} drawn icons, UiKit offline bundle")
    try:
        from PIL import Image
    except ImportError:
        print("SKIP bitmap inspection: install Pillow if needed")
        return
    with Image.open(ROOT / "Companion/Resources/Assets.xcassets/AppIcon.appiconset/icon-1024.png") as icon:
        assert icon.mode == "RGB" and icon.size == (1024, 1024)
    with Image.open(ROOT / "Companion/Resources/orange-cat-spritesheet.webp") as atlas:
        assert atlas.size == (1536, 1872)
    with Image.open(ROOT / "Companion/Resources/orange-cat-actions.webp") as atlas:
        assert atlas.size == (1536, 1248)
    print("PASS bitmaps: opaque RGB 1024 icon and 8-column, 9/6-row orange-cat atlases")


def check_static_invariants() -> None:
    app_files = sorted((ROOT / "Companion").rglob("*.swift"))
    for file in app_files:
        rel = file.relative_to(ROOT).as_posix()
        text = file.read_text(encoding="utf-8")
        assert "URLSession.shared.webSocketTask" not in text, (
            f"{rel}: WebSockets must use APIClient's ephemeral no-redirect URLSession"
        )
        assert ".completeFileProtection]" not in text, (
            f"{rel}: Use .completeFileProtectionUntilFirstUserAuthentication instead of strict .completeFileProtection"
        )
        # Disallow mutating .dateFormat on shared static formatters outside their initializer closure.
        for line_no, line in enumerate(text.splitlines(), 1):
            if ".dateFormat =" in line and "let formatter = DateFormatter()" not in line:
                window = "\n".join(text.splitlines()[max(0, line_no - 4):line_no])
                assert "DateFormatter()" in window, f"{rel}:{line_no}: Do not mutate shared DateFormatter.dateFormat"
        if "PHImageManager.default()." in text:
            assert "OnceBox(" in text, f"{rel}: PHImageManager continuations must be guarded with OnceBox"
            assert "PHImageResultIsDegradedKey" in text, f"{rel}: Filter degraded PhotoKit callbacks"

    presentation = (ROOT / "Companion/Core/Presentation.swift").read_text(encoding="utf-8")
    assert 'mcp__' in presentation, "Presentation.swift must support Hermes mcp__<server>__<tool> names"
    store = (ROOT / "Companion/Services/CompanionStore.swift").read_text(encoding="utf-8")
    for required_event in ('"goal_state"', '"approval_updated"'):
        assert required_event in store, f"CompanionStore.swift must handle {required_event} WebSocket events"
    print("PASS static invariants: ephemeral WS sessions, immutable formatters, once-guarded PhotoKit, MCP/WS contracts")


def check_swift(required: bool) -> bool:
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_swift
    except ImportError:
        if required:
            raise RuntimeError("Swift grammar modules are missing; see this script's module docstring")
        print("SKIP Swift grammar: optional tree-sitter and tree-sitter-swift are not installed")
        return True
    language = Language(tree_sitter_swift.language())
    parser = Parser(language)
    files = (
        sorted((ROOT / "Companion").rglob("*.swift"))
        + sorted((ROOT / "CompanionTests").rglob("*.swift"))
        + sorted((ROOT / "CompanionUITests").rglob("*.swift"))
    )
    issues = []
    for file in files:
        data = file.read_bytes()
        tree = parser.parse(data)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "ERROR" or node.is_missing:
                row, column = node.start_point
                label = "missing " + node.type if node.is_missing else "ERROR"
                snippet = data[node.start_byte:min(node.end_byte, node.start_byte + 100)].decode("utf-8", errors="replace")
                issues.append((str(file.relative_to(ROOT)), row + 1, column + 1, label, snippet))
            stack.extend(reversed(node.children))
    for file, row, column, label, snippet in issues:
        print(f"{file}:{row}:{column}: {label}: {snippet!r}")
    if issues:
        print(f"FAIL Swift grammar: {len(issues)} ERROR/missing nodes in {len(files)} files")
        return False
    print(f"PASS Swift grammar: {len(files)} files, zero ERROR/missing nodes (grammar only, no compiler/type checking)")
    return True


def main() -> int:
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--require-swift", action="store_true", help="fail instead of skipping if grammar modules are missing")
    arguments = options.parse_args()
    try:
        check_project()
        check_resources()
        check_fixtures_and_screens()
        check_static_invariants()
        return 0 if check_swift(arguments.require_swift) else 1
    except Exception as error:
        print(f"FAIL static validation: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
