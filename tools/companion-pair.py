"""A pairing code for connecting a phone to the companion. Run it on the server itself:

    python tools/companion-pair.py                              # link with this machine's address
    python tools/companion-pair.py bot.example.com              # bare public domain -> https://
    python tools/companion-pair.py 192.168.1.20:8766            # LAN/loopback address -> http://
    python tools/companion-pair.py https://bot.example.com 8766 # and the companion's local port

Open the printed link on the phone (it pairs at once), or enter the server's address and the
code in the app. A code is good once, for 10 minutes.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # Chinese output on a Windows console or pipe

USAGE = """用法：python tools/companion-pair.py [外部访问地址或域名] [本机端口号]

示例：
  python tools/companion-pair.py
  python tools/companion-pair.py bot.example.com
  python tools/companion-pair.py http://192.168.1.20:8766
  python tools/companion-pair.py https://bot.example.com 8766
"""


def _is_local_or_lan_host(host: str) -> bool:
    lowered = host.strip().lower().strip("[]")
    if not lowered:
        return True
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".lan", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    # Loopback, RFC1918 private LAN, link-local, or Tailscale/CGNAT (100.64.0.0/10 -> ip.is_private in 3.11+ or explicit range)
    cgnat = ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local or cgnat)


def normalize_public_url(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"地址格式不正确（仅支持 http:// 或 https://）：{raw}")
        clean_path = parsed.path.rstrip("/")
        if clean_path.endswith("/companion.html"):
            clean_path = clean_path[: -len("/companion.html")]
        return urllib.parse.urlunsplit((scheme, parsed.netloc, clean_path, "", "")).rstrip("/")

    parsed = urllib.parse.urlsplit("//" + text)
    if not parsed.netloc or not parsed.hostname:
        raise ValueError(f"地址格式不正确：{raw}")
    scheme = "http" if _is_local_or_lan_host(parsed.hostname) else "https"
    clean_path = parsed.path.rstrip("/")
    if clean_path.endswith("/companion.html"):
        clean_path = clean_path[: -len("/companion.html")]
    return urllib.parse.urlunsplit((scheme, parsed.netloc, clean_path, "", "")).rstrip("/")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(USAGE.rstrip())
        return 0

    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print(f"未知选项：{unknown[0]}\n\n{USAGE.rstrip()}", file=sys.stderr)
        return 1

    public = ""
    port = os.environ.get("COMPANION_PORT", "8766")
    for item in args:
        if item.isdigit():
            port = item
        elif not public:
            try:
                public = normalize_public_url(item)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        else:
            print(f"多余的参数：{item}\n\n{USAGE.rstrip()}", file=sys.stderr)
            return 1

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never through a proxy
    query = f"?{urllib.parse.urlencode({'base': public})}" if public else ""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/pair/code{query}", data=b"", method="POST")
    try:
        with opener.open(request, timeout=10) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(
                f"本机伙伴服务（端口 {port}）正在运行，但拒绝了配对码请求（HTTP {exc.code}）。\n"
                "请确认这台机器没有通过本机反向代理注入转发头，或在已连接设备的「设置 → 设备 → 生成配对码」中生成。",
                file=sys.stderr,
            )
        else:
            detail = ""
            try:
                detail = exc.read(512).decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            print(f"伙伴服务返回错误（HTTP {exc.code}）{('：' + detail) if detail else ''}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"连不上本机的伙伴服务（端口 {port}）：{exc}\n先确认网关在运行。", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"伙伴服务返回了无法解析的响应：{exc}", file=sys.stderr)
        return 1

    print(f"配对码：{body['code']}（10 分钟内有效，只能用一次）")
    print(f"一键连接：{body['link']}")
    if not public:
        print("（这是本机地址。给手机用时带上手机能访问的地址，比如 python tools/companion-pair.py bot.example.com）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
