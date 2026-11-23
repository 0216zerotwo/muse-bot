"""Talk to the companion server like the phone app: new chat, one message, print events until turn_end."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

USAGE = """Usage:
  python tools/companion_ws_probe.py [--base HOST:PORT] [--stop] [message]

Options:
  --base HOST:PORT  Server host:port (default: 127.0.0.1:<COMPANION_PORT or 8766>)
  --stop            Send /stop once the first tool_hint event arrives
  -h, --help        Show this help message and exit
"""


async def run_probe(base: str, text: str, stop_after_tool: bool) -> None:
    import websockets

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{base}/webui/bootstrap", timeout=15) as resp:
        boot = json.load(resp)
    async with websockets.connect(f"ws://{base}{boot['ws_path']}?token={boot['token']}", proxy=None) as ws:
        print("<", await ws.recv())
        await ws.send(json.dumps({"type": "new_chat"}))
        attached = json.loads(await ws.recv())
        print("<", attached)
        chat = attached["chat_id"]
        await ws.send(json.dumps({"type": "message", "chat_id": chat, "content": text, "turn_id": "turn-1", "webui": True}))
        deltas: list[str] = []
        stopped = False
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), 180))
            if ev.get("event") == "delta":
                deltas.append(ev["text"])
                continue
            print("<", {k: v for k, v in ev.items() if k != "chat_id"})
            if stop_after_tool and ev.get("kind") == "tool_hint" and not stopped:
                stopped = True
                await ws.send(json.dumps({"type": "message", "chat_id": chat, "content": "/stop", "turn_id": "turn-stop", "webui": True}))
                print("> /stop")
            if ev.get("event") == "turn_end":
                break
        print("deltas:", len(deltas), "| streamed:", "".join(deltas)[:200])
        req = urllib.request.Request(
            f"http://{base}/api/sessions/websocket:{chat}/webui-thread",
            headers={"Authorization": f"Bearer {boot['api_token']}"},
        )
        with opener.open(req, timeout=15) as hist_resp:
            hist = json.load(hist_resp)
        print("history events:", [e["event"] for e in hist["events"]])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(USAGE.rstrip())
        return 0

    port = os.environ.get("COMPANION_PORT", "8766")
    base = os.environ.get("COMPANION_BASE", f"127.0.0.1:{port}")
    stop_after_tool = False
    positional: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--stop":
            stop_after_tool = True
            i += 1
        elif arg == "--base":
            if i + 1 >= len(args):
                print("--base requires HOST:PORT", file=sys.stderr)
                return 1
            base = args[i + 1]
            i += 2
        elif arg.startswith("-"):
            print(f"Unknown option: {arg}\n\n{USAGE.rstrip()}", file=sys.stderr)
            return 1
        else:
            positional.append(arg)
            i += 1

    text = positional[0] if positional else "你好，用一句话介绍你自己"
    try:
        asyncio.run(run_probe(base, text, stop_after_tool))
    except OSError as exc:
        print(f"Cannot reach companion server at {base}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
