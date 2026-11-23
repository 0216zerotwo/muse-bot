"""Static validation for the Muse Bot Android shell (`android/`).

Checks AndroidManifest.xml, resource XML/bitmap integrity, setup.html structure
and JS bridge calls, Java source syntax/balance/imports, security invariants,
line-ending hygiene, and `@JavascriptInterface` parity with `web/src/companion/android-shell.ts`.
"""
from __future__ import annotations

import html.parser
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app/src/main"
JAVA_DIR = MAIN / "java/com/personalbot/companion"
RES_DIR = MAIN / "res"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def _attr(elem: ET.Element, name: str) -> str | None:
    return elem.attrib.get(ANDROID_NS + name)


def _strip_java_comments_and_strings(source: str) -> str:
    """Replaces string/char literals and comments with spaces while preserving line count."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
        elif ch in ('"', "'"):
            quote = ch
            out.append(quote)
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                else:
                    if source[i] == "\n":
                        raise SyntaxError("Unterminated string/char literal before newline")
                    out.append(" ")
                    i += 1
            if i >= n:
                raise SyntaxError("Unterminated string/char literal at EOF")
            out.append(quote)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_manifest() -> None:
    manifest_path = MAIN / "AndroidManifest.xml"
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    assert root.tag == "manifest"
    assert root.attrib.get("package") == "com.personalbot.companion"
    assert _attr(root, "versionCode") and int(_attr(root, "versionCode") or "0") >= 1
    assert _attr(root, "versionName")

    permissions = {_attr(p, "name") for p in root.findall("uses-permission")}
    for required in [
        "android.permission.INTERNET",
        "android.permission.RECORD_AUDIO",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.RECEIVE_BOOT_COMPLETED",
        "android.permission.ACCESS_NETWORK_STATE",
        "android.permission.SCHEDULE_EXACT_ALARM",
    ]:
        assert required in permissions, f"Missing permission: {required}"

    app = root.find("application")
    assert app is not None, "Missing <application>"
    assert _attr(app, "allowBackup") == "false", "allowBackup must stay false to protect device tokens"
    assert _attr(app, "usesCleartextTraffic") == "true"

    component_tags = ("activity", "service", "receiver", "provider")
    components: dict[str, ET.Element] = {}
    for tag in component_tags:
        for elem in app.findall(tag):
            name = _attr(elem, "name")
            assert name and name.startswith("."), f"Unexpected component name: {name}"
            cls = name[1:]
            java_file = JAVA_DIR / f"{cls}.java"
            assert java_file.is_file(), f"Manifest component {name} missing {java_file}"
            components[cls] = elem

    main_activity = components.get("MainActivity")
    assert main_activity is not None and _attr(main_activity, "exported") == "true"
    actions = {
        _attr(action, "name")
        for intent_filter in main_activity.findall("intent-filter")
        for action in intent_filter.findall("action")
    }
    for expected_action in (
        "android.intent.action.MAIN",
        "android.intent.action.SEND",
        "android.intent.action.SEND_MULTIPLE",
    ):
        assert expected_action in actions, f"MainActivity missing intent action {expected_action}"

    provider = components.get("FilesProvider")
    assert provider is not None
    assert _attr(provider, "exported") == "false"
    assert _attr(provider, "grantUriPermissions") == "true"
    assert _attr(provider, "authorities") == "com.personalbot.companion.files"

    reminder_receiver = components.get("ReminderReceiver")
    assert reminder_receiver is not None and _attr(reminder_receiver, "exported") == "false"

    refresh_job = components.get("RefreshJob")
    assert refresh_job is not None
    assert _attr(refresh_job, "exported") == "false"
    assert _attr(refresh_job, "permission") == "android.permission.BIND_JOB_SERVICE"
    print(f"PASS Manifest: package, permissions, {len(components)} verified components, share intent filters")


def check_resources() -> None:
    xml_files = sorted(RES_DIR.rglob("*.xml"))
    assert xml_files, "No Android XML resources found"
    defined: dict[str, set[str]] = {
        "string": set(),
        "style": set(),
        "drawable": set(),
        "mipmap": set(),
    }
    referenced: set[tuple[str, str]] = set()

    for file in RES_DIR.rglob("*"):
        if not file.is_file():
            continue
        parent = file.parent.name.split("-", 1)[0]
        if parent in ("drawable", "mipmap"):
            defined[parent].add(file.stem)

    ref_pattern = re.compile(r"@(string|style|drawable|mipmap)/([A-Za-z0-9_.]+)")

    for xml_file in [MAIN / "AndroidManifest.xml", *xml_files]:
        text = xml_file.read_text(encoding="utf-8")
        tree = ET.fromstring(text)
        if xml_file.parent.name.startswith("values"):
            for child in tree:
                if child.tag in ("string", "style") and "name" in child.attrib:
                    defined[child.tag].add(child.attrib["name"])
                if child.tag == "style" and "parent" in child.attrib:
                    parent_style = child.attrib["parent"]
                    if parent_style.startswith("@style/"):
                        referenced.add(("style", parent_style.split("/", 1)[1]))
        for match in ref_pattern.finditer(text):
            referenced.add((match.group(1), match.group(2)))

    for java_file in sorted(JAVA_DIR.glob("*.java")):
        text = java_file.read_text(encoding="utf-8")
        for match in re.finditer(r"(?<!android\.)\bR\.(string|style|drawable|mipmap)\.([A-Za-z0-9_]+)\b", text):
            referenced.add((match.group(1), match.group(2)))

    for kind, name in sorted(referenced):
        assert name in defined[kind], f"Unresolved resource reference @{kind}/{name}"

    png_files = sorted(RES_DIR.rglob("*.png"))
    assert len(png_files) >= 10, "Expected launcher mipmap PNGs across densities"
    for png in png_files:
        header = png.read_bytes()[:8]
        assert header == b"\x89PNG\r\n\x1a\n", f"Invalid PNG signature in {png}"

    print(
        f"PASS resources: {len(xml_files)} XML files, {len(referenced)} resolved references, "
        f"{len(png_files)} valid PNG icons"
    )


class _SetupHTMLChecker(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.input_types: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        elem_id = attr_map.get("id")
        if elem_id:
            self.ids.add(elem_id)
        if tag == "input" and elem_id:
            self.input_types[elem_id] = attr_map.get("type") or "text"


def check_setup_html_and_java() -> None:
    java_files = sorted(JAVA_DIR.glob("*.java"))
    assert len(java_files) >= 8, "Expected all Android Java classes"

    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = {")": "(", "]": "[", "}": "{"}

    for java_file in java_files:
        raw = java_file.read_text(encoding="utf-8")
        assert "\r" not in raw, f"{java_file.name}: must use LF line endings"
        stripped = _strip_java_comments_and_strings(raw)
        assert re.search(r"^\s*package\s+com\.personalbot\.companion\s*;", stripped, re.M), java_file.name
        assert re.search(
            rf"\b(class|interface|enum)\s+{re.escape(java_file.stem)}\b", stripped
        ), f"{java_file.name}: missing top-level type {java_file.stem}"

        stack: list[tuple[str, int]] = []
        line_no = 1
        for ch in stripped:
            if ch == "\n":
                line_no += 1
            elif ch in pairs:
                stack.append((ch, line_no))
            elif ch in closing:
                assert stack and stack[-1][0] == closing[ch], (
                    f"{java_file.name}:{line_no}: mismatched '{ch}'"
                )
                stack.pop()
        assert not stack, f"{java_file.name}:{stack[-1][1]}: unclosed '{stack[-1][0]}'"
        # Detect accidental duplicate exact method signatures inside the same file
        seen_sigs: set[str] = set()
        for sig_match in re.finditer(
            r"^\s*(?:public|private|protected|static|final|synchronized|\s)*[\w<>\[\].]+\s+(\w+\s*\([^)]*\))\s*(?:throws\s+[\w.,\s]+)?\{",
            stripped,
            re.M,
        ):
            sig = re.sub(r"\s+", " ", sig_match.group(1)).strip()
            if sig.startswith(("if ", "for ", "while ", "switch ", "catch ", "synchronized ")):
                continue
            assert sig not in seen_sigs or java_file.name == "MainActivity.java", (
                f"{java_file.name}: duplicate method signature {sig}"
            )
            seen_sigs.add(sig)

    main_src = (JAVA_DIR / "MainActivity.java").read_text(encoding="utf-8")
    bridge_methods: dict[str, str] = {}
    for match in re.finditer(
        r"@JavascriptInterface\s+public\s+[\w<>\[\].]+\s+(\w+)\s*\([^)]*\)\s*\{",
        main_src,
    ):
        name = match.group(1)
        start = match.end() - 1
        depth = 0
        end = start
        while end < len(main_src):
            if main_src[end] == "{":
                depth += 1
            elif main_src[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        body = main_src[start : end + 1]
        bridge_methods[name] = body
        assert "isCompanionOrigin()" in body or "isSetupOrigin()" in body, (
            f"MainActivity.Bridge.{name} must enforce origin check (isCompanionOrigin/isSetupOrigin)"
        )

    # Check setup.html structure & bridge calls
    setup_path = MAIN / "assets/setup.html"
    setup_html = setup_path.read_text(encoding="utf-8")
    assert "\r" not in setup_html, "setup.html must use LF line endings"
    assert setup_html.lstrip().lower().startswith("<!doctype html>"), "setup.html missing <!doctype html>"
    parser = _SetupHTMLChecker()
    parser.feed(setup_html)
    for required_id in ("form", "server", "error", "reconnect-panel", "retry-btn", "switch-btn"):
        assert required_id in parser.ids, f"setup.html missing #{required_id}"
    assert parser.input_types.get("server") == "text", (
        "setup.html #server input must use type='text' so bare domains and pairing snippets submit"
    )
    for js_call in set(re.findall(r"\bbridge\.(\w+)\b", setup_html)):
        assert js_call in bridge_methods, f"setup.html calls unknown Bridge method: bridge.{js_call}"

    # Cross-check against web/src/companion/android-shell.ts when present
    web_shell = ROOT.parent / "web/src/companion/android-shell.ts"
    if web_shell.is_file():
        ts = web_shell.read_text(encoding="utf-8")
        iface = re.search(r"interface\s+AndroidShell\s*\{(.*?)\}", ts, re.S)
        assert iface, "Could not parse AndroidShell interface"
        expected_web_methods = set(re.findall(r"^\s*(\w+)\??\s*\(", iface.group(1), re.M))
        missing = expected_web_methods - set(bridge_methods)
        assert not missing, f"MainActivity.Bridge missing AndroidShell methods: {sorted(missing)}"

    # Static safety & contract invariants
    assert "StaticServiceWorkerClient" in main_src and "WeakReference<MainActivity>" in main_src, (
        "ServiceWorkerController client must use a static WeakReference wrapper"
    )
    for file_prefix in (
        '"/api/media/"',
        '"/api/webui/companion/creations/file/"',
        '"/api/webui/companion/avatars/file/"',
    ):
        assert file_prefix in main_src, f"MainActivity.FILE_PATHS missing {file_prefix}"

    media_src = (JAVA_DIR / "MediaLibrary.java").read_text(encoding="utf-8")
    assert r"filename\\*" in media_src and "filenameFromSignedUrl" in media_src, (
        "MediaLibrary.java must decode RFC 5987 filename* and signed URL payloads"
    )

    reminders_src = (JAVA_DIR / "Reminders.java").read_text(encoding="utf-8")
    assert "catch (SecurityException" in reminders_src and "synchronized (ALARM_LOCK)" in reminders_src, (
        "Reminders.java must synchronize alarms and fall back on SecurityException"
    )

    print(
        f"PASS Java & bridge: {len(java_files)} classes balanced, "
        f"{len(bridge_methods)} origin-checked @JavascriptInterface methods, setup.html & web parity verified"
    )


def check_scripts() -> None:
    build_sh = (ROOT / "build.sh").read_bytes()
    assert b"\r" not in build_sh, "android/build.sh must use LF line endings"
    build_text = build_sh.decode("utf-8")
    assert "chmod 600" in build_text, "android/build.sh must restrict keystore file permissions"
    assert "head -c 32" not in build_text, "Avoid SIGPIPE-prone head -c in build.sh under pipefail"

    raw_ps1 = (ROOT / "install.ps1").read_bytes()
    assert raw_ps1.startswith(b"\xef\xbb\xbf"), (
        "android/install.ps1 must keep UTF-8 BOM so Windows PowerShell 5.1 parses Chinese strings cleanly"
    )
    install_ps1 = raw_ps1.decode("utf-8-sig")
    assert "-s $target" in install_ps1, "android/install.ps1 must target explicit adb serial (-s)"
    print("PASS scripts: build.sh (LF, chmod 600, SIGPIPE-safe) and install.ps1 (UTF-8 BOM, multi-device adb -s)")


def main() -> int:
    try:
        check_manifest()
        check_resources()
        check_setup_html_and_java()
        check_scripts()
        return 0
    except Exception as exc:
        print(f"FAIL Android static validation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
