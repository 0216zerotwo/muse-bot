"""Call the companion server like the app: GET a route, or send one mutation over the socket.

    python tools/companion_api_probe.py get /api/webui/companion/models
    python tools/companion_api_probe.py do companion.models.check '{"names": ["glm"]}'
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

USAGE = """Usage:
  python tools/companion_api_probe.py [--base HOST:PORT] get <path>
  python tools/companion_api_probe.py [--base HOST:PORT] do <action> [json-payload]

Environment:
  COMPANION_BASE   Server host:port (default: 127.0.0.1:<COMPANION_PORT or 8766>)
  COMPANION_PORT   Local companion port when COMPANION_BASE is unset (default: 8766)
"""

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def boot(base: str) -> dict:
    with opener.open(f"http://{base}/webui/bootstrap", timeout=15) as resp:
        return json.load(resp)


def get(base: str, path: str) -> None:
    token = boot(base)["api_token"]
    route = path if path.startswith("/") else "/" + path
    request = urllib.request.Request(f"http://{base}{route}", headers={"Authorization": f"Bearer {token}"})
    with opener.open(request, timeout=30) as resp:
        print(json.dumps(json.load(resp), ensure_ascii=False, indent=1))


async def do(base: str, action: str, payload: dict) -> None:
    import websockets

    b = boot(base)
    async with websockets.connect(f"ws://{base}{b['ws_path']}?token={b['token']}", proxy=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "webui_request", "request_id": "r1", "action": action, "payload": payload}))
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 180))
            if event.get("event") == "webui_response":
                print(json.dumps(event, ensure_ascii=False, indent=1))
                return


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or "-h" in args or "--help" in args:
        out = sys.stdout if ("-h" in args or "--help" in args) else sys.stderr
        print(USAGE.rstrip(), file=out)
        return 0 if ("-h" in args or "--help" in args) else 1

    port = os.environ.get("COMPANION_PORT", "8766")
    base = os.environ.get("COMPANION_BASE", f"127.0.0.1:{port}")
    if args[0] == "--base":
        if len(args) < 2:
            print("--base requires HOST:PORT", file=sys.stderr)
            return 1
        base = args[1]
        args = args[2:]

    if len(args) < 2 or args[0] not in ("get", "do"):
        print(USAGE.rstrip(), file=sys.stderr)
        return 1

    try:
        if args[0] == "get":
            get(base, args[1])
        else:
            payload = json.loads(args[2]) if len(args) > 2 else {}
            asyncio.run(do(base, args[1], payload))
    except OSError as exc:
        print(f"Cannot reach companion server at {base}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid JSON payload or response: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
