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
LOCAL_PACKAGES = ROOT / ".validation" / "python"
if LOCAL_PACKAGES.is_dir():
    sys.path.insert(0, str(LOCAL_PACKAGES))


def check_project() -> None:
    import yaml

    project = yaml.load((ROOT / "project.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    workflow = yaml.load((ROOT.parent / ".github/workflows/ios.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert project["targets"]["Companion"]["settings"]["base"]["PRODUCT_BUNDLE_IDENTIFIER"] == "com.personalbot.companion"
    assert project["options"]["deploymentTarget"]["iOS"] == "17.0"
    assert project["settings"]["base"]["SWIFT_VERSION"] in ("5", "5.0", "6", "6.0")
    assert project["packages"]["MarkdownUI"]["exactVersion"] == "2.4.1"
    assert "on" in workflow and "workflow_dispatch" in workflow["on"]
    assert workflow["jobs"]["build"]["needs"] == "test"
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == "macos-latest", name
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    assert "CODE_SIGNING_ALLOWED=NO" in (ROOT.parent / ".github/workflows/ios.yml").read_text(encoding="utf-8")
    app_sources = project["targets"]["Companion"]["sources"]
    assert any(item["path"] == "Companion" for item in app_sources)
    assert any(item["path"] == "Companion/Resources" and item.get("buildPhase") == "resources" for item in app_sources)
    assert project["targets"]["CompanionTests"]["sources"][0]["path"] == "CompanionTests"
    assert any(item["path"] == "CompanionTests/Fixtures" and item.get("buildPhase") == "resources"
               for item in project["targets"]["CompanionTests"]["sources"])
    assert workflow["env"]["DEVELOPER_DIR"] == "/Applications/Xcode_26.3.app/Contents/Developer"
    expected_actions = {"actions/checkout": "1af3b93b6815bc44a9784bd300feb67ff0d1eeb3",
                        "actions/upload-artifact": "b7c566a772e6b6bfb58ed0dc250532a479d7789f"}
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                action, sha = step["uses"].split("@")
                assert expected_actions[action] == sha, "Use the audited Node 24 action pins"
    print("PASS YAML: project and workflow; identifiers, source roots, pinned actions/package, unsigned build")


def check_fixtures_and_screens() -> None:
    fixtures = sorted((ROOT / "CompanionTests/Fixtures").glob("*.json"))
    assert len(fixtures) >= 26, "Each JSON data endpoint needs a neutralized observed response"
    for fixture in fixtures:
        json.loads(fixture.read_text(encoding="utf-8"))
    files = list((ROOT / "Companion").rglob("*.swift"))
    sources = "\n".join(file.read_text(encoding="utf-8") for file in files)
    assert "ComingSoonView" not in sources and "即将推出" not in sources
    for screen in ["FeedScreen", "IdeasScreen", "GoalsScreen", "HelpersScreen", "LibraryScreen", "SettingsView",
                   "AppearanceSettingsPage", "ModelsSettingsPage", "UsageSettingsPage", "SkillsSettingsPage",
                   "ConnectionsSettingsPage", "PermissionsSettingsPage", "VaultSettingsPage", "DevicesSettingsPage",
                   "IdentityView", "ComputerView"]:
        assert re.search(r"struct\s+" + screen + r"\s*:", sources), screen
    tests = "\n".join(file.read_text(encoding="utf-8") for file in (ROOT / "CompanionTests").glob("*.swift"))
    count = len(re.findall(r"func test\w+\s*\(", tests))
    print(f"PASS feature coverage: six tabs, eight settings pages, identity/browser, {len(fixtures)} JSON fixtures, {count} XCTest methods (not executed)")


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
    print(f"PASS resources: plist, launch XML, {count} asset image references")
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
    files = sorted((ROOT / "Companion").rglob("*.swift")) + sorted((ROOT / "CompanionTests").rglob("*.swift"))
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
        return 0 if check_swift(arguments.require_swift) else 1
    except Exception as error:
        print(f"FAIL static validation: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
