"""Talk to the companion server like the phone app: new chat, one message, print events until turn_end."""
import asyncio, json, sys, urllib.request
import websockets

BASE = "127.0.0.1:8766"
# --stop: press the app's stop button once the first tool call shows up.
STOP_AFTER_TOOL = "--stop" in sys.argv
if STOP_AFTER_TOOL:
    sys.argv.remove("--stop")

async def main(text: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    boot = json.load(opener.open(f"http://{BASE}/webui/bootstrap"))
    async with websockets.connect(f"ws://{BASE}{boot['ws_path']}?token={boot['token']}", proxy=None) as ws:
        print("<", await ws.recv())
        await ws.send(json.dumps({"type": "new_chat"}))
        attached = json.loads(await ws.recv()); print("<", attached)
        chat = attached["chat_id"]
        await ws.send(json.dumps({"type": "message", "chat_id": chat, "content": text, "turn_id": "turn-1", "webui": True}))
        deltas, stopped = [], False
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), 180))
            if ev.get("event") == "delta":
                deltas.append(ev["text"]); continue
            print("<", {k: v for k, v in ev.items() if k != "chat_id"})
            if STOP_AFTER_TOOL and ev.get("kind") == "tool_hint" and not stopped:
                stopped = True
                await ws.send(json.dumps({"type": "message", "chat_id": chat, "content": "/stop", "turn_id": "turn-stop", "webui": True}))
                print("> /stop")
            if ev.get("event") == "turn_end":
                break
        print("deltas:", len(deltas), "| streamed:", "".join(deltas)[:200])
        hist = json.load(opener.open(urllib.request.Request(f"http://{BASE}/api/sessions/websocket:{chat}/webui-thread", headers={"Authorization": f"Bearer {boot['api_token']}"})))
        print("history events:", [e["event"] for e in hist["events"]])

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "你好，用一句话介绍你自己"))
