"""Call the companion server like the app: GET a route, or send one mutation over the socket.

    python companion_api_probe.py get /api/webui/companion/models
    python companion_api_probe.py do companion.models.check '{"names": ["glm"]}'
"""
import asyncio
import json
import sys
import urllib.request

import websockets

BASE = "127.0.0.1:8766"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def boot() -> dict:
    return json.load(opener.open(f"http://{BASE}/webui/bootstrap"))


def get(path: str) -> None:
    token = boot()["api_token"]
    request = urllib.request.Request(f"http://{BASE}{path}", headers={"Authorization": f"Bearer {token}"})
    print(json.dumps(json.load(opener.open(request)), ensure_ascii=False, indent=1))


async def do(action: str, payload: dict) -> None:
    b = boot()
    async with websockets.connect(f"ws://{BASE}{b['ws_path']}?token={b['token']}", proxy=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "webui_request", "request_id": "r1", "action": action, "payload": payload}))
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 180))
            if event.get("event") == "webui_response":
                print(json.dumps(event, ensure_ascii=False, indent=1))
                return


if sys.argv[1] == "get":
    get(sys.argv[2])
else:
    asyncio.run(do(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}))
