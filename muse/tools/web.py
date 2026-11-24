"""Web tools: `web_search` (`data.web` contract) and SSRF-guarded `web_extract`."""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Optional

import httpx

TOOLSET = "web"
MAX_REDIRECTS = 5
MAX_EXTRACT_BYTES = 2 * 1024 * 1024
DEFAULT_EXTRACT_CHARS = 20_000

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "100.100.100.200",
})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_SKIP_HTML_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "head", "iframe"})
_BLOCK_HTML_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "blockquote", "pre", "tr", "ul", "ol", "dl", "dt", "dd", "hr", "br",
})
_HEADING_TAGS = {f"h{i}": "#" * i for i in range(1, 7)}
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _is_forbidden_ip(raw_ip: str) -> bool:
    addr = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    candidates = [addr]
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        candidates.append(addr.ipv4_mapped)
    for item in candidates:
        if (
            item.is_private
            or item.is_loopback
            or item.is_link_local
            or item.is_multicast
            or item.is_unspecified
            or item.is_reserved
        ):
            return True
        if isinstance(item, ipaddress.IPv4Address) and item in _CGNAT_NET:
            return True
    return False


def is_safe_public_url(url: str) -> bool:
    """Validate that `url` uses http/https and resolves strictly to public unicast IPs."""
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        return False
    try:
        return not _is_forbidden_ip(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return False
        try:
            if _is_forbidden_ip(str(sockaddr[0])):
                return False
        except ValueError:
            return False
    return True


class _HtmlToMarkdownParser(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._in_pre = False
        self._link_stack: list[tuple[str, int]] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        low = tag.lower()
        if low == "title":
            self._in_title = True
            return
        if low in _SKIP_HTML_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if low in _HEADING_TAGS:
            self._parts.append(f"\n\n{_HEADING_TAGS[low]} ")
        elif low == "li":
            self._parts.append("\n- ")
        elif low in ("td", "th"):
            self._parts.append(" | ")
        elif low == "pre":
            self._in_pre = True
            self._parts.append("\n```\n")
        elif low == "code" and not self._in_pre:
            self._parts.append("`")
        elif low in _BLOCK_HTML_TAGS:
            self._parts.append("\n")
        elif low == "a":
            href = dict(attrs).get("href") or ""
            resolved = urllib.parse.urljoin(self.base_url, href.strip()) if href.strip() else ""
            self._link_stack.append((resolved, len(self._parts)))
            self._parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low == "title":
            self._in_title = False
            return
        if low in _SKIP_HTML_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return
        if low in _HEADING_TAGS:
            self._parts.append("\n")
        elif low == "pre":
            self._in_pre = False
            self._parts.append("\n```\n")
        elif low == "code" and not self._in_pre:
            self._parts.append("`")
        elif low in _BLOCK_HTML_TAGS:
            self._parts.append("\n")
        elif low == "a" and self._link_stack:
            href, open_idx = self._link_stack.pop()
            anchor_txt = "".join(self._parts[open_idx + 1 :]).strip()
            if href and anchor_txt and href.startswith(("http://", "https://")):
                self._parts.append(f"]({href})")
            else:
                self._parts[open_idx] = ""

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth > 0 or not data:
            return
        if self._in_pre:
            self._parts.append(data)
        else:
            collapsed = re.sub(r"[ \t\r\f\v]+", " ", data)
            self._parts.append(collapsed)

    def get_result(self) -> tuple[str, str]:
        title = " ".join("".join(self.title_parts).split()).strip()
        raw = html.unescape("".join(self._parts))
        lines = [line.strip() for line in raw.splitlines()]
        cleaned = _MULTI_BLANK_RE.sub("\n\n", "\n".join(lines)).strip()
        return title, cleaned


def html_to_markdown(raw_html: str, *, base_url: str = "") -> tuple[str, str]:
    parser = _HtmlToMarkdownParser(base_url=base_url)
    parser.feed(raw_html)
    parser.close()
    return parser.get_result()


def _search_tavily(query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        resp = client.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results},
        )
        resp.raise_for_status()
        payload = resp.json()
    return [
        {
            "url": str(item.get("url") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "description": str(item.get("content") or item.get("snippet") or "").strip(),
        }
        for item in (payload.get("results") or [])
        if isinstance(item, dict) and item.get("url")
    ]


def _search_brave(query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        resp = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
        resp.raise_for_status()
        payload = resp.json()
    web_rows = ((payload.get("web") or {}).get("results")) or []
    return [
        {
            "url": str(item.get("url") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "description": str(item.get("description") or "").strip(),
        }
        for item in web_rows
        if isinstance(item, dict) and item.get("url")
    ]


def _search_ddgs(
    query: str,
    max_results: int,
    *,
    region: str = "wt-wt",
    timelimit: Optional[str] = None,
) -> list[dict[str, str]]:
    from ddgs import DDGS

    with DDGS() as ddgs:
        raw_items = list(
            ddgs.text(
                query,
                region=region,
                timelimit=timelimit,
                max_results=max_results,
            )
            or []
        )
    out: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("href") or item.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "url": url,
            "title": str(item.get("title") or "").strip(),
            "description": str(item.get("body") or item.get("description") or "").strip(),
        })
    return out


def web_search_handler(args: dict[str, Any], **_: Any) -> str:
    """Search the web and strictly return `{"success": True, "data": {"web": [...]}}`."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"success": False, "error": "Parameter 'query' is required.", "data": {"web": []}},
            ensure_ascii=False,
        )
    limit = max(1, min(25, int(args.get("max_results") or args.get("count") or 8)))
    region = str(args.get("region") or "wt-wt").strip() or "wt-wt"
    timelimit = str(args["timelimit"]).strip() if args.get("timelimit") else None
    requested = str(args.get("backend") or os.environ.get("MUSE_WEB_SEARCH_BACKEND") or "auto").lower()

    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()

    try:
        if requested == "tavily" or (requested == "auto" and tavily_key):
            rows = _search_tavily(query, limit, tavily_key)
            chosen = "tavily"
        elif requested == "brave" or (requested == "auto" and brave_key):
            rows = _search_brave(query, limit, brave_key)
            chosen = "brave"
        else:
            rows = _search_ddgs(query, limit, region=region, timelimit=timelimit)
            chosen = "ddgs"
        return json.dumps(
            {
                "success": True,
                "backend": chosen,
                "query": query,
                "data": {"web": rows[:limit]},
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "query": query,
                "data": {"web": []},
            },
            ensure_ascii=False,
        )


def _fetch_and_extract_one(
    url: str,
    *,
    max_chars: int = DEFAULT_EXTRACT_CHARS,
    max_bytes: int = MAX_EXTRACT_BYTES,
    url_validator: Any = is_safe_public_url,
) -> dict[str, Any]:
    current = (url or "").strip()
    if not url_validator(current):
        return {"url": current, "content": "", "error": "Blocked unsafe or non-public URL"}

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MuseBot/2.0; +https://musebot.local)",
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
    }
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return {"url": current, "content": "", "error": "Redirect missing Location"}
                        nxt = urllib.parse.urljoin(str(resp.request.url), loc)
                        if not url_validator(nxt):
                            return {
                                "url": current,
                                "content": "",
                                "error": "Redirect target blocked by SSRF policy",
                            }
                        current = nxt
                        continue
                    resp.raise_for_status()
                    ctype = (resp.headers.get("content-type") or "").lower()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            chunks.append(chunk[: max(0, max_bytes - (total - len(chunk)))])
                            break
                        chunks.append(chunk)
                    raw_body = b"".join(chunks)
                    charset = resp.encoding or "utf-8"
                    try:
                        decoded = raw_body.decode(charset, errors="replace")
                    except LookupError:
                        decoded = raw_body.decode("utf-8", errors="replace")

                    if "html" in ctype or "<html" in decoded[:512].lower():
                        title, content = html_to_markdown(decoded, base_url=current)
                    else:
                        title = ""
                        content = decoded.strip()
                    if len(content) > max_chars:
                        content = content[:max_chars] + "\n... [content truncated]"
                    return {"url": current, "title": title, "content": content}
        return {"url": current, "content": "", "error": "Too many redirects"}
    except Exception as exc:
        return {"url": current, "content": "", "error": f"{type(exc).__name__}: {exc}"}


def web_extract_handler(args: dict[str, Any], **_: Any) -> str:
    """Fetch and extract clean Markdown/text from one or more public URLs with SSRF protection."""
    raw_urls: list[str] = []
    if isinstance(args.get("urls"), list):
        raw_urls.extend(str(u).strip() for u in args["urls"] if str(u).strip())
    if args.get("url"):
        single = str(args["url"]).strip()
        if single and single not in raw_urls:
            raw_urls.append(single)
    if not raw_urls:
        return json.dumps(
            {"success": False, "error": "Provide 'url' or 'urls'.", "results": []},
            ensure_ascii=False,
        )

    max_chars = max(500, min(100_000, int(args.get("max_chars") or DEFAULT_EXTRACT_CHARS)))
    results = [_fetch_and_extract_one(u, max_chars=max_chars) for u in raw_urls[:10]]
    any_ok = any(bool(r.get("content")) and not r.get("error") for r in results)
    payload: dict[str, Any] = {"success": any_ok, "results": results}
    if len(results) == 1:
        payload["url"] = results[0].get("url", "")
        payload["title"] = results[0].get("title", "")
        payload["content"] = results[0].get("content", "")
        if results[0].get("error"):
            payload["error"] = results[0]["error"]
    return json.dumps(payload, ensure_ascii=False)


WEB_SEARCH_SCHEMA: dict[str, Any] = {
    "name": "web_search",
    "description": "Search the public web for up-to-date pages, documentation, news, and facts.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query keywords."},
            "max_results": {
                "type": "integer",
                "description": "Maximum search hits to return (default 8).",
                "default": 8,
            },
            "timelimit": {
                "type": "string",
                "enum": ["d", "w", "m", "y"],
                "description": "Optional recency filter (day, week, month, year).",
            },
        },
        "required": ["query"],
    },
}

WEB_EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "web_extract",
    "description": "Fetch one or more public HTTP(S) URLs and extract clean Markdown/text.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Single public URL to extract."},
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Batch of public URLs to extract (up to 10).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters per page (default 20000).",
                "default": 20000,
            },
        },
    },
}


def register_web_tools(registry: Any) -> None:
    registry.register("web_search", toolset=TOOLSET, schema=WEB_SEARCH_SCHEMA, handler=web_search_handler)
    registry.register("web_extract", toolset=TOOLSET, schema=WEB_EXTRACT_SCHEMA, handler=web_extract_handler)


__all__ = [
    "TOOLSET",
    "WEB_EXTRACT_SCHEMA",
    "WEB_SEARCH_SCHEMA",
    "html_to_markdown",
    "is_safe_public_url",
    "register_web_tools",
    "web_extract_handler",
    "web_search_handler",
]
