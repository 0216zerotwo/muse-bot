"""A stand-in companion server for screenshots on the cloud Mac: the app pairs with it, connects
and reads every screen's data from the test fixtures (their placeholder text replaced with readable
sample content). Nothing here talks to the real server.

    pip install websockets pillow
    python preview/mock_server.py 8799      # HTTP on 8799, the chat socket on 8800
"""
import asyncio
import base64
import hashlib
import io
import struct
import time
import itertools
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "CompanionTests" / "Fixtures"
CODE = "ABCD-2345"

ROUTES = {
    "/api/webui/companion": "core-state",
    "/api/webui/companion/avatars": "core-avatars",
    "/api/webui/companion/helpers": "core-helpers",
    "/api/sessions": "core-sessions",
    "/api/webui/approvals": "core-approvals",
    "/api/webui/companion/computer": "identity-computer",
    "/api/webui/companion/memory-doc": "identity-memory-doc",
    "/api/webui/companion/memory": "identity-memory",
    "/api/webui/companion/soul": "identity-soul",
    "/api/webui/devices": "settings-devices",
    "/api/webui/companion/model-catalog": "settings-model-catalog",
    "/api/webui/companion/models": "settings-models",
    "/api/webui/companion/permissions": "settings-permissions",
    "/api/settings/mcp-presets": "settings-services",
    "/api/webui/companion/skills": "settings-skills",
    "/api/webui/companion/usage": "settings-usage",
    "/api/webui/companion/vault": "settings-vault",
    "/api/webui/automations": "tabs-automations",
    "/api/webui/companion/creations": "tabs-creations",
    "/api/webui/companion/feed": "tabs-feed",
    "/api/webui/companion/goals": "tabs-goals",
    "/api/webui/companion/ideas": "tabs-ideas",
    "/api/webui/companion/routines": "tabs-routines",
    "/api/webui/companion/triggers": "tabs-triggers",
}

# Readable stand-ins for the fixtures' "示例内容", by field name.
WORDS = {
    "name": ["小橘", "翻译", "行程", "比价", "写手", "记账", "研究员", "管家"],
    "title": ["整理 MacBook 对比", "周末去杭州", "每天早上的天气", "读书笔记", "健身计划"],
    "preview": ["好的，我把三款的续航和价格整理成表了。", "明早 7 点提醒你带伞。", "这周花了 486 元，比上周少一点。"],
    "text": ["帮我看看这周末杭州天气怎么样，适合出去玩吗？",
             "周六多云转晴，18–24°C，很适合出门；周日下午有小雨，记得带伞。我把两天的安排先排了一下：",
             "那顺便帮我订一下周六上午去杭州的高铁",
             "好的，我查了一下，上午有这几趟还有票，你挑一趟：",
             "画一张我们一起去西湖的图"],
    "starters": ["帮我翻译这段话", "规划一个周末行程", "比一比这两个"],
    "instructions": ["说话简洁，先给结论。"],
    "emoji": ["🧭", "📰", "✍️", "💰", "🔍", "🏠"],
    "preset": ["日常"],
    "model": ["DeepSeek V4.1 Flash"],
    "provider": ["DeepSeek"],
    "label": ["搜索网页"],
}
ASKS = itertools.cycle(["帮我看看这周末杭州天气怎么样，适合出去玩吗？", "那周六上午出发的话，几点走比较好？",
                        "顺便推荐两家西湖边的餐厅"])
ANSWERS = itertools.cycle(["周六多云转晴，**18–24°C**，很适合出门；周日下午有小雨，记得带伞。",
                           "建议 8 点前出发，避开早高峰：\n\n- 坐 8:05 的高铁，9:12 到杭州东\n- 地铁 1 号线到龙翔桥，步行 10 分钟到湖边",
                           "可以试试 **知味观**（老字号，小笼和片儿川）和 **绿茶餐厅**（排队多，建议提前取号）。"])
_cycles: dict[str, itertools.cycle] = {}


def filled(value, key=None):
    if isinstance(value, dict):
        return {k: filled(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [filled(v, key) for v in value]
    if value == "示例内容":
        pool = WORDS.get(key or "", WORDS["preview"])
        return next(_cycles.setdefault(key or "", itertools.cycle(pool)))
    return value


def fixture(name):
    return filled(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


STATE = fixture("core-state")
STATE.update(name="小橘", avatar="")  # the built-in orange cat
CHAT = STATE["chat_id"]

CARD = """周六这几趟还有票：

```card
{"type": "choices", "title": "选一趟", "options": ["G7311 08:05 → 09:12", "G7501 09:00 → 10:01", "G7355 10:20 → 11:27"]}
```

```card
{"type": "weather", "place": "杭州", "temp": "22°", "summary": "多云转晴", "details": [{"label": "湿度", "value": "62%"}, {"label": "风", "value": "东北风 2 级"}], "hours": [{"time": "09:00", "temp": "19°"}, {"time": "12:00", "temp": "23°"}, {"time": "15:00", "temp": "24°"}, {"time": "18:00", "temp": "21°"}]}
```"""


CHART = """近一周几只科技股都在跌，英伟达跌得最多：

```ui
<div style="padding:12px 12px 4px"><canvas id="drop" height="220"></canvas></div>
<script>
new Chart(document.getElementById('drop'), {type: 'bar',
  data: {labels: ['英伟达', '特斯拉', '苹果', '微软', '谷歌'],
         datasets: [{label: '近一周涨跌 %', data: [-6.2, -4.8, -2.1, -1.5, -0.9]}]},
  options: {plugins: {legend: {display: false}}}});
</script>
```"""


PLAN = """# 阳明山周日徒步计划

**日期**：周日 · 早上 7:30 出发
**路线**：冷水坑 → 七星山主峰 → 小油坑（约 6 公里）

## 行程
1. 07:30 剑潭站集合，搭 108 路
2. 09:00 冷水坑出发，先看牛奶湖
3. 11:00 登顶七星山主峰，午餐
4. 14:00 小油坑下山，看喷气孔

## 带上
- 水 1.5 升、能量棒
- 防风外套（山顶风大）
- 雨伞，下午可能有阵雨
"""


def demo_pdf() -> bytes:
    """A one-page PDF (a heading, lines of text and a table's rules), written by hand: no library."""
    lines = ["BT /F1 22 Tf 56 770 Td (Weekend Hike) Tj ET",
             "BT /F1 11 Tf 56 748 Td (Sunday, 7:30 start  -  6 km  -  about 5 hours) Tj ET",
             "0.85 0.24 0.22 RG 2 w 56 736 m 539 736 l S"]
    for i, (time_, what) in enumerate([("07:30", "Meet at the station, bus 108"), ("09:00", "Start at Lengshuikeng"),
                                       ("11:00", "Summit, lunch"), ("14:00", "Down by Xiaoyoukeng")]):
        y = 700 - i * 28
        lines.append(f"BT /F1 12 Tf 56 {y} Td ({time_}) Tj ET")
        lines.append(f"BT /F1 12 Tf 130 {y} Td ({what}) Tj ET")
        lines.append(f"0.8 G 0.5 w 56 {y - 9} m 539 {y - 9} l S")
    lines.append("BT /F1 12 Tf 56 560 Td (Bring: 1.5 L water, wind jacket, umbrella) Tj ET")
    stream = "\n".join(lines).encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


DOCUMENTS = {"/api/media/demo/plan.md": (PLAN.encode(), "text/markdown; charset=utf-8"),
             "/api/media/demo/plan.pdf": (demo_pdf(), "application/pdf")}


def thread():
    data = fixture("core-thread")
    for event in data.get("events", []):  # each side says its own kind of thing
        if event.get("text"):
            event["text"] = next(ASKS) if event["event"] == "user_message" else next(ANSWERS)
    ms = 1793300000000
    extra = [
        {"event": "user_message", "chat_id": CHAT, "text": "帮我查周六上午去杭州的高铁", "turn_id": "demo-1",
         "starts_turn": True, "created_at_ms": ms},
        # What it did on the way (做过的事 lists the turn as a task with these steps).
        {"event": "message", "chat_id": CHAT, "text": "", "kind": "tool_hint", "turn_phase": "activity", "turn_id": "demo-1",
         "tool_events": [{"version": 1, "phase": "end", "call_id": "demo-1-search", "name": "web_search",
                          "arguments": {"query": "周六上午 上海到杭州 高铁"}, "result": "10 个结果", "error": None}],
         "created_at_ms": ms + 1500},
        {"event": "message", "chat_id": CHAT, "text": "", "kind": "tool_hint", "turn_phase": "activity", "turn_id": "demo-1",
         "tool_events": [{"version": 1, "phase": "end", "call_id": "demo-1-page", "name": "web_fetch",
                          "arguments": {"url": "https://www.12306.cn/index/"}, "result": "车次列表", "error": None}],
         "created_at_ms": ms + 2600},
        {"event": "message", "chat_id": CHAT, "text": CARD, "turn_id": "demo-1", "created_at_ms": ms + 4000},
        {"event": "turn_end", "chat_id": CHAT, "turn_id": "demo-1", "created_at_ms": ms + 4100},
        {"event": "user_message", "chat_id": CHAT, "text": "画一张我们一起去西湖的图", "turn_id": "demo-2",
         "starts_turn": True, "created_at_ms": ms + 60000},
        {"event": "message", "chat_id": CHAT, "text": "画好啦，傍晚的断桥边：", "turn_id": "demo-2",
         "media": [{"url": "/api/media/demo/westlake.png", "name": "westlake.png", "kind": "image"}],
         "created_at_ms": ms + 90000},
        {"event": "turn_end", "chat_id": CHAT, "turn_id": "demo-2", "created_at_ms": ms + 90100},
        # An interactive chart card (a ```ui block), drawn in the card's sandboxed web view.
        {"event": "user_message", "chat_id": CHAT, "text": "可视化展示最近美股科技股的跌幅", "turn_id": "demo-3",
         "starts_turn": True, "created_at_ms": ms + 120000},
        {"event": "message", "chat_id": CHAT, "text": CHART, "turn_id": "demo-3", "created_at_ms": ms + 150000},
        {"event": "turn_end", "chat_id": CHAT, "turn_id": "demo-3", "created_at_ms": ms + 150100},
        # Documents it wrote, each shown by its first page (the writing call is one of its steps).
        {"event": "user_message", "chat_id": CHAT, "text": "把周末徒步计划整理成文档发我", "turn_id": "demo-4",
         "starts_turn": True, "created_at_ms": ms + 180000},
        {"event": "message", "chat_id": CHAT, "text": "", "kind": "tool_hint", "turn_phase": "activity", "turn_id": "demo-4",
         "tool_events": [{"version": 1, "phase": "end", "call_id": "demo-4-write", "name": "write_file",
                          "arguments": {"path": "/srv/creations/documents/周末徒步计划.md", "content": PLAN},
                          "result": "{\"bytes_written\": 420}", "error": None},
                         # What it learned about the user on the way (已记住 under the reply).
                         {"version": 1, "phase": "end", "call_id": "demo-4-memory", "name": "memory",
                          "arguments": {"target": "user", "action": "add", "content": "【近况与计划】这个周日去阳明山徒步"},
                          "result": "{\"success\": true}", "error": None}],
         "created_at_ms": ms + 185000},
        {"event": "message", "chat_id": CHAT, "text": "写好了，行程和装备都在里面；打印版也一起给你：", "turn_id": "demo-4",
         "media": [{"url": "/api/media/demo/plan.md", "name": "周末徒步计划.md", "kind": "file"},
                   {"url": "/api/media/demo/plan.pdf", "name": "Weekend Hike.pdf", "kind": "file"}],
         "created_at_ms": ms + 190000},
        {"event": "turn_end", "chat_id": CHAT, "turn_id": "demo-4", "created_at_ms": ms + 190100},
    ]
    data["events"] = data.get("events", []) + extra
    data["completed_turn_ids"] = list(data.get("completed_turn_ids", [])) + ["demo-1", "demo-2", "demo-3", "demo-4"]
    data["active_turn_id"] = None  # as the server says it: nothing running
    data["has_pending_tool_calls"] = False
    return data


SERVICES = [("email", "通用邮箱", "收发 QQ、163、Gmail 等任意 IMAP/SMTP 邮箱"),
            ("icloud-calendar", "iCloud 日历", "查看、添加和修改 iPhone 上的 iCloud 日程"),
            ("kuaidi100", "快递查询", "用快递单号查物流进度、估算寄件运费"),
            ("amap", "高德地图", "查天气、规划路线（驾车/步行/骑行/公交）、搜周边"),
            ("train-12306", "12306 火车票", "查火车票余票、车次、经停站和中转方案"),
            ("browser", "浏览器", "让它打开网页替你办事：查价格、填表、截图"),
            ("minimax", "MiniMax 视频与图片", "用 MiniMax（海螺）生成视频、图片和语音"),
            ("home-assistant", "Home Assistant 智能家居", "查看和控制家里的灯、空调、窗帘、插座")]


LOGOS = {name: f"https://www.google.com/s2/favicons?domain={domain}&sz=64" for name, domain in [
    ("email", "mail.qq.com"), ("icloud-calendar", "icloud.com"), ("kuaidi100", "kuaidi100.com"), ("amap", "amap.com"),
    ("train-12306", "12306.cn"), ("minimax", "minimaxi.com"), ("home-assistant", "home-assistant.io")]}


def services():
    data = fixture("settings-services")
    template = dict(data["presets"][0])
    rows = []
    for index, (name, title, about) in enumerate(SERVICES):
        row = dict(template, name=name, display_name=title, description=about, logo_url=LOGOS.get(name))
        connected = index == 5  # the browser is connected, as on most servers
        row.update(installed=connected, configured=connected, enabled=connected,
                   status="connected" if connected else "available")
        rows.append(row)
    data["presets"] = rows + data["presets"]
    data["installed_count"] = 1
    return data


VENDORS = [("deepseek", "DeepSeek", "deepseek.com", "#4D6BFE", "cn", None), ("chatgpt", "ChatGPT 订阅", "chatgpt.com", "#0D0D0D", "sub", "chatgpt"),
           ("volcengine", "火山方舟", "volcengine.com", "#3370FF", "cn", None), ("minimax", "MiniMax", "minimaxi.com", "#E73562", "cn", None),
           ("openrouter", "OpenRouter", "openrouter.ai", "#6467F2", "hub", None), ("custom", "自定义", "", None, "hub", None)]


def catalog():
    """The fixture's catalog with the vendor ids the models really use (so rows show their logos)."""
    data = fixture("settings-model-catalog")
    template = dict(data["vendors"][0])
    data["vendors"] = [dict(template, id=vid, name=name, color=color, group=group, login=login, local_login=bool(login),
                            has_key=vid != "openrouter", kinds=["text", "image", "video"],
                            console=f"https://{domain}/" if domain and not login else "", note="" if login else "按量付费，填上 API key 就能用。",
                            logo_url=f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else None)
                       for vid, name, domain, color, group, login in VENDORS]
    return data


# A company's models for 添加模型 (companion.models.vendor / .check), with prices as the server sends them.
VENDOR_MODELS = {
    "models": [
        {"id": "deepseek-flash", "name": "DeepSeek V4.1 Flash", "note": "日常聊天，便宜又快", "vision": True, "reasoning": True,
         "context": 1048576, "recommended": True, "price": {"unit": "mtok", "rows": [{"spec": "", "input": 1, "output": 2, "cached": 0.1}]}},
        {"id": "deepseek-pro", "name": "DeepSeek V4.1 Pro", "note": "更强的推理", "vision": True, "reasoning": True,
         "context": 1048576, "recommended": True, "price": {"unit": "mtok", "rows": [{"spec": "≤128K", "input": 4, "output": 16}, {"spec": "", "input": 8, "output": 24}]}},
        {"id": "deepseek-v3", "name": "DeepSeek V3", "note": "", "vision": False, "reasoning": False,
         "context": 131072, "recommended": False, "price": {"unit": "mtok", "rows": [{"spec": "", "input": 2, "output": 8}]}},
    ],
    "checked": {"at": 1793300000, "sources": []},
    "can_check": True,
}
MUTATIONS = {"companion.models.vendor": VENDOR_MODELS, "companion.models.check": VENDOR_MODELS}


def feed():
    """The fixture's posts, the first with a source link (opened in the app's own web page)."""
    data = fixture("tabs-feed")
    if data.get("posts"):
        data["posts"][0]["title"] = "苹果发布新款 MacBook Pro"
        data["posts"][0]["body"] = "M6 芯片，续航再长两小时，起售价不变。\n\n[阅读原文](https://example.com/)"
    return data


def picture(seed: str) -> bytes:
    h = abs(hash(seed))
    a = ((h >> 0) % 200 + 30, (h >> 8) % 200 + 30, (h >> 16) % 200 + 30)
    b = ((h >> 24) % 200 + 30, (h >> 32) % 200 + 30, (h >> 40) % 200 + 30)
    im = Image.new("RGB", (600, 600))
    draw = ImageDraw.Draw(im)
    for y in range(600):
        t = y / 599
        draw.line([(0, y), (600, y)], fill=tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3)))
    draw.ellipse((200, 180, 400, 380), fill=(255, 255, 255))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799


def _ws_send(stream, message):
    data = json.dumps(message, ensure_ascii=False).encode()
    head = bytes([0x81])
    if len(data) < 126:
        head += bytes([len(data)])
    elif len(data) < 65536:
        head += bytes([126]) + struct.pack(">H", len(data))
    else:
        head += bytes([127]) + struct.pack(">Q", len(data))
    stream.write(head + data)
    stream.flush()


def _ws_read(stream):
    """One client frame's text (masked, as clients send), or None when the socket closed."""
    head = stream.read(2)
    if len(head) < 2 or head[0] & 0x0F == 0x8:
        return None
    size = head[1] & 0x7F
    if size == 126:
        size = struct.unpack(">H", stream.read(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", stream.read(8))[0]
    mask = stream.read(4)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(stream.read(size)))
    return payload.decode("utf-8", "replace")


def computer_socket(handler):
    """The server browser as the app sees it: a page and its frames; take / done hand it over, copy
    returns a selection. Enough for the screenshots of the browser view."""
    key = handler.headers.get("Sec-WebSocket-Key", "")
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    out, lock, alive = handler.wfile, threading.Lock(), {"on": True, "hold": None}
    frame = base64.b64encode(picture("browser-page")).decode()

    def say(message):
        with lock:
            _ws_send(out, message)

    say({"type": "page", "title": "Example Domain", "url": "https://example.com/", "hold": None, "handoff": {}})

    def frames():
        while alive["on"]:
            try:
                say({"type": "frame", "data": frame, "w": 1280, "h": 800})
            except OSError:
                return
            time.sleep(1)

    threading.Thread(target=frames, daemon=True).start()
    try:
        while True:
            text = _ws_read(handler.rfile)
            if text is None:
                break
            message = json.loads(text)
            kind = message.get("type")
            if kind in ("take", "start"):
                alive["hold"] = "user"
                say({"type": "hold", "hold": "user"})
            elif kind in ("done", "close"):
                alive["hold"] = None
                say({"type": "hold", "hold": None})
            elif kind == "copy":
                say({"type": "copied", "text": "苹果发布新款 MacBook Pro"})
    except (OSError, ValueError):
        pass
    alive["on"] = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # the browser socket's upgrade must answer in 1.1
    def send(self, status, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws/computer" and self.headers.get("Upgrade", "").lower() == "websocket":
            return computer_socket(self)
        if path in DOCUMENTS:
            return self.send(200, *DOCUMENTS[path])
        if path.startswith(("/api/media/", "/api/webui/companion/creations/file/")):
            return self.send(200, picture(path), "image/png")
        if path == "/webui/bootstrap":
            return self.send(200, {"token": "demo", "api_token": "demo", "ws_path": "/",
                                   "ws_url": f"ws://127.0.0.1:{PORT + 1}/", "expires_in": 3600,
                                   "device_id": "demo-iphone",
                                   "limits": {"transport": {"max_frame_bytes": 2097152}}})
        if path.endswith("/webui-thread"):
            # The main chat has the sample conversation; a new side chat starts empty.
            return self.send(200, thread() if CHAT in path else {"events": [], "completed_turn_ids": [], "active_turn_id": None,
                                                                  "has_pending_tool_calls": False})
        if path == "/api/settings/mcp-presets":
            return self.send(200, services())
        if path == "/api/webui/companion/search":
            return self.send(200, {"query": "西湖", "hits": [
                {"chat_id": CHAT, "chat_title": "主要聊天", "role": "assistant", "turn_id": "demo-2",
                 "text": "画好啦，傍晚的断桥边，我们一起看西湖。", "at_ms": 1793300090000},
                {"chat_id": CHAT, "chat_title": "主要聊天", "role": "user", "turn_id": "demo-2",
                 "text": "画一张我们一起去西湖的图", "at_ms": 1793300060000}]})
        if path == "/api/webui/companion/feed":
            return self.send(200, feed())
        if path == "/api/webui/companion/model-catalog":
            return self.send(200, catalog())
        if path == "/api/webui/companion":
            return self.send(200, STATE)
        name = ROUTES.get(path)
        self.send(200, fixture(name) if name else {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.path == "/api/pair":
            code = str(json.loads(body or b"{}").get("code", "")).upper()
            if code != CODE:
                return self.send(400, {"error": "bad_code"})
            return self.send(200, {"token": "demo-device", "device_id": "demo-iphone"})
        self.send(200, {"code": "WXYZ-6789", "link": "http://127.0.0.1/#pair=WXYZ-6789",
                        "reference": "demo-upload"})


async def chat(ws):
    await ws.send(json.dumps({"event": "ready", "upload": {"path": "/api/attachments", "token": "demo"}}))
    async for raw in ws:
        frame = json.loads(raw)
        kind = frame.get("type")
        out = []
        if kind == "attach":
            out.append({"event": "attached", "chat_id": frame.get("chat_id")})
        elif kind == "new_chat":
            out.append({"event": "attached", "chat_id": f"demo-{uuid.uuid4().hex[:8]}"})
        elif kind == "webui_request":
            if frame.get("action") == "companion.update":  # keep what was saved, as the server does
                STATE.update((frame.get("payload") or {}).get("changes") or {})
            result = STATE if frame.get("action") == "companion.update" else MUTATIONS.get(frame.get("action"), {})
            out.append({"event": "webui_response", "request_id": frame.get("request_id"), "ok": True, "result": result})
        elif kind == "message":
            chat_id, turn = frame.get("chat_id"), frame.get("turn_id")
            await ws.send(json.dumps({"event": "message_accepted", "chat_id": chat_id, "turn_id": turn}))
            await asyncio.sleep(0.5)
            out += [{"event": "message", "chat_id": chat_id, "turn_id": turn, "text": "收到！（这是截图用的模拟回复）"},
                    {"event": "turn_end", "chat_id": chat_id, "turn_id": turn}]
        for event in out:
            await ws.send(json.dumps(event, ensure_ascii=False))


async def main():
    http = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    async with serve(chat, "127.0.0.1", PORT + 1, max_size=4 * 1024 * 1024) as server:
        print(f"mock companion on http://127.0.0.1:{PORT} (code {CODE})", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
