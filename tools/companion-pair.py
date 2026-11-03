"""A pairing code for connecting a phone to the companion. Run it on the server itself:

    python tools/companion-pair.py                              # link with this machine's address
    python tools/companion-pair.py https://bot.example.com      # link with the address phones use
    python tools/companion-pair.py https://bot.example.com 8766 # and the companion's local port

Open the printed link on the phone (it pairs at once), or enter the server's address and the
code in the app. A code is good once, for 10 minutes.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Chinese output on a Windows console or pipe

public = next((a for a in sys.argv[1:] if a.startswith(("http://", "https://"))), "")
port = next((a for a in sys.argv[1:] if a.isdigit()), os.environ.get("COMPANION_PORT", "8766"))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never through a proxy
query = f"?{urllib.parse.urlencode({'base': public})}" if public else ""
request = urllib.request.Request(f"http://127.0.0.1:{port}/api/pair/code{query}", data=b"", method="POST")
try:
    body = json.load(opener.open(request, timeout=10))
except OSError as exc:
    sys.exit(f"连不上本机的伙伴服务（端口 {port}）：{exc}\n先确认网关在运行。")
print(f"配对码：{body['code']}（10 分钟内有效，只能用一次）")
print(f"一键连接：{body['link']}")
if not public:
    print("（这是本机地址。给手机用时带上手机能访问的地址，比如 python tools/companion-pair.py https://你的域名）")
