"""检测: what a company's platform says right now about its models — the current model IDs and
their prices — read from its own pricing pages (or, when none is known or it can't be read, the
top results of a web search), then put in order by the chat model.

Prices come in different shapes, so each is kept as a unit and a few rows:

- ``mtok``: per million tokens — one row per tier, each with ``input`` / ``output`` (and ``cached``,
  the cache-hit input price, when the page gives it); ``spec`` names the tier (``≤512K``).
- ``second``: per second of video — one row per resolution (``768P``: 0.5).
- ``video``: per clip — one row per resolution and length (``768P·6秒``: 2.0).
- ``image``: per picture — one row, or one per size.

Always in 元 (dollar prices are converted). The result is kept per company and kind
(``model-check.json``), shown in 添加模型, and the prices of models already added are updated.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .fs_atomic import atomic_write_json
from .model_catalog import USD_TO_CNY, vendor
from .paths import get_webui_dir

logger = logging.getLogger(__name__)

UNITS = ("mtok", "second", "video", "image")
UNIT_LABEL = {"mtok": "每百万 token", "second": "元/秒", "video": "元/条", "image": "元/张"}
_KIND_WORDS = {"text": "文本/对话（语言）模型", "image": "图像生成（画图）模型", "video": "视频生成模型"}
_PAGE_CHARS = 30_000
_TIMEOUT_S = 120
_LOCK = threading.Lock()

_PROMPT = """你在读一家 AI 模型公司的官方定价/模型页面，整理出其中的{kind_word}。
只输出一个 JSON 对象，不要别的文字：
{{"models": [{{"id": "模型 ID，和页面上调用时写的一字不差", "name": "简短的显示名", "note": "一句中文特点（可空）",
  "price": {{"unit": "{units}", "currency": "CNY 或 USD", "rows": [...]}}}}]}}
rows 的写法：
- unit=mtok（按百万 token）：[{{"spec": "档位，没有分档就空字符串", "input": 数字, "output": 数字, "cached": 数字或 null}}]
- unit=second（视频按秒）：[{{"spec": "分辨率，如 768P", "price": 数字}}]
- unit=video（视频按条）：[{{"spec": "分辨率·时长，如 768P·6秒", "price": 数字}}]
- unit=image（图片按张）：[{{"spec": "尺寸或空字符串", "price": 数字}}]
规则：
- 只要{kind_word}；别的种类的模型不要。
- 「历史模型」「旧版」只要还写着价格、还能调用，就要列出来（放在后面）；只有明确写了已下线、停止服务的才不要。
- 只要直接生成内容的模型；对已有结果做再生成、超分、改写的工具型模型不要。
- 价格就是页面上写的数字，不要换算、不要猜；页面没写价格就把 price 设为 null。
- 同一个模型的不同档位放进同一个模型的 rows 里，不要拆成几个模型；第一行放最常见的情况（标准档、高峰/白天时段、较短上下文那档），费用按第一行估算。
- 有「输入图片/输入视频」之类的附加价格，只要输出的价格。
- 最多 30 个模型，新的在前。"""


def _cache_path() -> Path:
    return get_webui_dir() / "model-check.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def checked(vendor_id: str, kind: str) -> Optional[dict[str, Any]]:
    """The last check of a company's models of one kind, or None."""
    entry = (_read().get(vendor_id) or {}).get(kind)
    return entry if isinstance(entry, dict) else None


def _save(vendor_id: str, kind: str, entry: dict[str, Any]) -> None:
    with _LOCK:
        data = _read()
        data.setdefault(vendor_id, {})[kind] = entry
        atomic_write_json(_cache_path(), data)


# ---------- reading the pages ----------


def _extract(urls: list[str]) -> list[tuple[str, str]]:
    from muse.tools.web import web_extract_handler

    try:
        raw = json.loads(web_extract_handler({"urls": urls, "max_chars": _PAGE_CHARS}))
    except Exception as exc:
        logger.info("companion: reading pricing pages failed (%s)", type(exc).__name__)
        return []
    return [(str(r.get("url") or ""), str(r.get("content") or "")) for r in raw.get("results") or []
            if isinstance(r, dict) and not r.get("error") and len(str(r.get("content") or "")) > 200]


def _search(entry: dict[str, Any], kind: str) -> list[str]:
    from muse.tools.web import web_search_handler

    query = f"{entry['name']} API {_KIND_WORDS[kind]} 价格 模型 ID 官方"
    try:
        raw = json.loads(web_search_handler({"query": query, "max_results": 5}))
    except Exception as exc:
        logger.info("companion: searching for pricing failed (%s)", type(exc).__name__)
        return []
    rows = ((raw.get("data") or {}).get("web") or []) if isinstance(raw, dict) else []
    domain = str(entry.get("domain") or "")
    urls = [str(r.get("url")) for r in rows if isinstance(r, dict) and r.get("url")]
    # The company's own pages first.
    return sorted(urls, key=lambda u: domain.split(".")[0] not in u)[:3]


def _as_markdown(url: str) -> Optional[str]:
    """A docs site's own markdown of a page (``<page>.md``, as Mintlify and others serve it): every
    tab of a tabbed price table, which a page reader sees only the first of."""
    import httpx

    try:
        response = httpx.get(url.rstrip("/") + ".md", timeout=15, follow_redirects=True)
    except Exception:
        return None
    kind = response.headers.get("content-type", "")
    if response.status_code != 200 or "html" in kind or len(response.text) < 1000:
        return None
    return response.text


def _pages(entry: dict[str, Any], kind: str) -> list[tuple[str, str]]:
    known = list((entry.get("pricing") or {}).get(kind) or (entry.get("pricing") or {}).get("all") or [])
    # Only the company's own pages listed in the catalog are fetched directly; anything found by a
    # search goes through Hermes's page reader (and its checks).
    pages = [(url, text) for url in known if (text := _as_markdown(url))]
    rest = [url for url in known if url not in {u for u, _ in pages}]
    pages += _extract(rest) if rest else []
    if sum(len(text) for _, text in pages) < 1500:
        pages += _extract(_search(entry, kind))
    return pages


# ---------- putting it in order ----------


def _ask(pages: list[tuple[str, str]], kind: str) -> list[Any]:
    from muse.llm.auxiliary import call_llm

    from .models import read_models, runtime_provider

    data = read_models()
    name = data.get("default")
    target = ({"provider": runtime_provider(name, data["presets"][name]), "model": data["presets"][name]["model"]}
              if name in data["presets"] else {})
    units = "mtok" if kind == "text" else "second | video" if kind == "video" else "image"
    text = "\n\n".join(f"### 页面 {url}\n{content[:_PAGE_CHARS]}" for url, content in pages)[:60_000]
    response = call_llm(
        messages=[{"role": "system", "content": _PROMPT.format(kind_word=_KIND_WORDS[kind], units=units)},
                  {"role": "user", "content": text}],
        max_tokens=4000, temperature=0, timeout=_TIMEOUT_S, reasoning_config={"enabled": False}, **target,
    )
    raw = response.choices[0].message.content or ""
    match = re.search(r"\{[\s\S]*\}", raw)
    try:
        found = json.loads(match.group(0)) if match else {}
    except ValueError:
        found = {}
    return found.get("models") if isinstance(found, dict) and isinstance(found.get("models"), list) else []


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def clean_price(raw: Any) -> Optional[dict[str, Any]]:
    """A price as the checker or the app gives it, checked and in 元; None when it says nothing."""
    if not isinstance(raw, dict) or raw.get("unit") not in UNITS:
        return None
    rate = USD_TO_CNY if str(raw.get("currency") or "").upper() == "USD" else 1.0
    money = lambda v: None if _number(v) is None else round(_number(v) * rate, 4)  # noqa: E731
    rows = []
    for row in raw.get("rows") or []:
        if not isinstance(row, dict):
            continue
        spec = " ".join(str(row.get("spec") or "").split())[:24]
        if raw["unit"] == "mtok":
            if money(row.get("input")) is None or money(row.get("output")) is None:
                continue
            rows.append({"spec": spec, "input": money(row["input"]), "output": money(row["output"]),
                         "cached": money(row.get("cached"))})
        elif money(row.get("price")) is not None:
            rows.append({"spec": spec, "price": money(row["price"])})
    return {"unit": raw["unit"], "rows": rows[:8]} if rows else None


def _clean(models: list[Any], kind: str) -> list[dict[str, Any]]:
    out, seen = [], set()
    for raw in models:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or "").strip()
        if not model_id or len(model_id) > 120 or " " in model_id or model_id in seen:
            continue
        seen.add(model_id)
        price = clean_price(raw.get("price"))
        if price and (price["unit"] == "mtok") != (kind == "text"):
            price = None  # a price in the wrong shape for this kind is a misread
        out.append({"id": model_id, "name": " ".join(str(raw.get("name") or model_id).split())[:40],
                    "note": " ".join(str(raw.get("note") or "").split())[:40], "price": price})
    return out[:30]


def check(vendor_id: str, kind: str) -> dict[str, Any]:
    """Reads the company's platform now (half a minute to two): its current models of this kind
    and their prices. Saved, and the prices of the models already added from it updated."""
    entry = vendor(vendor_id)
    if entry is None or entry.get("login"):
        raise ValueError("这个服务商没法检测")
    if kind not in _KIND_WORDS:
        raise ValueError("kind 只能是 text、image 或 video")
    pages = _pages(entry, kind)
    if not pages:
        raise ValueError("没读到它的定价页面，稍后再试")
    try:
        models = _clean(_ask(pages, kind), kind)
    except Exception as exc:
        logger.info("companion: reading %s's models failed (%s)", vendor_id, exc)
        raise ValueError("整理模型和价格时出错了，稍后再试") from None
    if not models:
        raise ValueError("页面上没找到这类模型")
    result = {"checked_at": time.time(), "sources": [url for url, _ in pages][:4], "models": models}
    _save(vendor_id, kind, result)
    from .models import update_checked_prices

    update_checked_prices(vendor_id, kind, {m["id"]: m["price"] for m in models if m["price"]})
    return result


# ---------- showing a price ----------


def text_prices(price: Optional[dict[str, Any]]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Input, output and cache-hit 元 per million tokens of a text model (its first tier)."""
    if not price or price.get("unit") != "mtok" or not price.get("rows"):
        return None, None, None
    row = price["rows"][0]
    return row["input"], row["output"], row.get("cached")


def cost_of(price: Optional[dict[str, Any]], *, count: int = 1, seconds: Optional[float] = None,
            resolution: str = "") -> Optional[float]:
    """What a picture or a clip cost, from its model's price: the row for its resolution (and
    length), else the cheapest."""
    if not price or price.get("unit") not in ("second", "video", "image") or not price.get("rows"):
        return None
    rows = price["rows"]
    want = resolution.upper().replace("P", "")
    # The common 720p is what some companies sell as 768P.
    wants = {want, "768"} if want == "720" else {want}

    def matches(row: dict[str, Any]) -> bool:
        return bool(want) and any(w in row["spec"].upper().replace("P", "") for w in wants)

    fitting = [r for r in rows if matches(r)] or rows
    if price["unit"] == "video" and seconds:
        def length(row: dict[str, Any]) -> float:
            found = re.search(r"(\d+)\s*(?:秒|s)", row["spec"])
            return abs(float(found.group(1)) - seconds) if found else 99.0

        fitting = sorted(fitting, key=length)
    row = min(fitting, key=lambda r: r["price"]) if price["unit"] != "video" or not seconds else fitting[0]
    if price["unit"] == "second":
        return round(row["price"] * float(seconds or 0) * count, 4)
    return round(row["price"] * count, 4)
