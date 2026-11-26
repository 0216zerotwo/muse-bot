"""Web tools: `web_search` (`data.web` contract), concurrent SSRF-guarded `web_extract`, and `deep_research`."""

from __future__ import annotations

import contextvars
import html
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Optional

import httpx

from muse.interceptors import scrub_url_secrets
from muse.tools.citations import record_citations

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


def _run_search_query(
    query: str,
    limit: int,
    *,
    requested: str = "auto",
    region: str = "wt-wt",
    timelimit: Optional[str] = None,
) -> tuple[str, list[dict[str, str]]]:
    from muse.config_store import get_env_value

    tavily_key = (get_env_value("TAVILY_API_KEY") or "").strip()
    brave_key = (get_env_value("BRAVE_API_KEY") or "").strip()
    if requested == "tavily" or (requested == "auto" and tavily_key):
        return "tavily", _search_tavily(query, limit, tavily_key)
    if requested == "brave" or (requested == "auto" and brave_key):
        return "brave", _search_brave(query, limit, brave_key)
    return "ddgs", _search_ddgs(query, limit, region=region, timelimit=timelimit)


def web_search_handler(args: dict[str, Any], **_: Any) -> str:
    """Search the web and strictly return `{"success": True, "data": {"web": [...]}}`."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"success": False, "error": "Parameter 'query' is required.", "data": {"web": []}},
            ensure_ascii=False,
        )
    from muse.config_store import get_env_value

    limit = max(1, min(25, int(args.get("max_results") or args.get("count") or 8)))
    region = str(args.get("region") or "wt-wt").strip() or "wt-wt"
    timelimit = str(args["timelimit"]).strip() if args.get("timelimit") else None
    requested = str(args.get("backend") or get_env_value("MUSE_WEB_SEARCH_BACKEND") or "auto").lower()

    try:
        chosen, rows = _run_search_query(query, limit, requested=requested, region=region, timelimit=timelimit)
        top_rows = rows[:limit]
        if top_rows:
            record_citations(
                [
                    {
                        "title": item.get("title") or "",
                        "url": item.get("url") or "",
                        "snippet": item.get("description") or "",
                    }
                    for item in top_rows
                ]
            )
        return json.dumps(
            {
                "success": True,
                "backend": chosen,
                "query": query,
                "data": {"web": top_rows},
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


def _extract_snippet(content: str, max_len: int = 240) -> str:
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s+", "", str(content or ""))
    cleaned = " ".join(cleaned.split()).strip()
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def _normalize_dedup_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(scrub_url_secrets(str(url or "").strip()))
    except ValueError:
        return ""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"[{host}]{port}" if ":" in host else f"{host}{port}"
    return urllib.parse.urlunsplit((scheme, netloc, (parsed.path or "").rstrip("/"), parsed.query or "", ""))


def _url_domain(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") and len(host) > 4 else host


def _extract_urls_parallel(
    urls: list[str],
    *,
    max_chars: int = DEFAULT_EXTRACT_CHARS,
    url_validator: Any = is_safe_public_url,
) -> list[dict[str, Any]]:
    if not urls:
        return []
    workers = min(5, len(urls))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                contextvars.copy_context().run,
                _fetch_and_extract_one,
                u,
                max_chars=max_chars,
                url_validator=url_validator,
            )
            for u in urls
        ]
        return [fut.result() for fut in futures]


def web_extract_handler(args: dict[str, Any], **_: Any) -> str:
    """Fetch and extract clean Markdown/text from one or more public URLs concurrently with SSRF protection."""
    raw_urls: list[str] = []
    if isinstance(args.get("urls"), list):
        for item in args["urls"]:
            s = str(item).strip()
            if s and s not in raw_urls:
                raw_urls.append(s)
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
    results = _extract_urls_parallel(raw_urls[:10], max_chars=max_chars)

    cited_items = [
        {
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "snippet": _extract_snippet(str(r.get("content") or "")),
        }
        for r in results
        if r.get("content") and not r.get("error")
    ]
    if cited_items:
        record_citations(cited_items)

    any_ok = any(bool(r.get("content")) and not r.get("error") for r in results)
    payload: dict[str, Any] = {"success": any_ok, "results": results}
    if len(results) == 1:
        payload["url"] = results[0].get("url", "")
        payload["title"] = results[0].get("title", "")
        payload["content"] = results[0].get("content", "")
        if results[0].get("error"):
            payload["error"] = results[0]["error"]
    return json.dumps(payload, ensure_ascii=False)


def deep_research_handler(args: dict[str, Any], **_: Any) -> str:
    """Run parallel multi-query web search + SSRF-safe extraction and return a numbered `[1]..[N]` digest."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"success": False, "error": "Parameter 'query' is required.", "sources": [], "digest": ""},
            ensure_ascii=False,
        )

    queries: list[str] = [query]
    seen_q = {query.casefold()}
    raw_subs = args.get("sub_queries")
    if isinstance(raw_subs, list):
        for item in raw_subs:
            sub = str(item or "").strip()
            if sub and sub.casefold() not in seen_q:
                seen_q.add(sub.casefold())
                queries.append(sub)
            if len(queries) >= 4:
                break

    raw_max = args.get("max_sources")
    try:
        max_sources = 5 if raw_max is None or raw_max == "" else max(1, min(8, int(raw_max)))
    except (TypeError, ValueError):
        max_sources = 5

    from muse.config_store import get_env_value

    requested = str(args.get("backend") or get_env_value("MUSE_WEB_SEARCH_BACKEND") or "auto").lower()
    per_query_limit = max(5, max_sources + 2)

    def _search_one(q: str) -> tuple[str, list[dict[str, str]], Optional[str]]:
        try:
            _, rows = _run_search_query(q, per_query_limit, requested=requested)
            return q, rows[:per_query_limit], None
        except Exception as exc:
            return q, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        search_futs = [pool.submit(contextvars.copy_context().run, _search_one, q) for q in queries]
        search_outputs = [f.result() for f in search_futs]

    merged: dict[str, dict[str, Any]] = {}
    search_errors: list[str] = []
    skipped_unsafe = 0
    order_seq = 0
    for q_text, rows, err in search_outputs:
        if err:
            search_errors.append(f"{q_text}: {err}")
            continue
        for row in rows:
            raw_url = str(row.get("url") or "").strip()
            norm_key = _normalize_dedup_url(raw_url)
            if not norm_key:
                continue
            if norm_key in merged:
                merged[norm_key]["hits"] += 1
                if not merged[norm_key]["title"] and row.get("title"):
                    merged[norm_key]["title"] = str(row["title"]).strip()
                if not merged[norm_key]["description"] and row.get("description"):
                    merged[norm_key]["description"] = str(row["description"]).strip()
                continue
            if not is_safe_public_url(raw_url):
                skipped_unsafe += 1
                continue
            merged[norm_key] = {
                "url": raw_url,
                "title": str(row.get("title") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "hits": 1,
                "rank": order_seq,
            }
            order_seq += 1

    ranked = sorted(merged.values(), key=lambda item: (-item["hits"], item["rank"]))
    selected = ranked[:max_sources]
    if not selected:
        return json.dumps(
            {
                "success": False,
                "query": query,
                "queries": queries,
                "count": 0,
                "sources": [],
                "digest": "",
                "skipped_unsafe": skipped_unsafe,
                "errors": search_errors,
            },
            ensure_ascii=False,
        )

    per_source_chars = min(4000, max(1200, 16_000 // max_sources))
    extracted_list = _extract_urls_parallel(
        [item["url"] for item in selected],
        max_chars=per_source_chars,
    )

    draft_sources: list[dict[str, Any]] = []
    for cand, ext in zip(selected, extracted_list):
        err = str(ext.get("error") or "")
        if err and ("SSRF" in err or "Blocked" in err):
            skipped_unsafe += 1
            continue
        content = str(ext.get("content") or "").strip()
        final_url = scrub_url_secrets(str(ext.get("url") or cand["url"]).strip())
        title = str(ext.get("title") or cand["title"] or _url_domain(final_url) or final_url).strip()
        snippet = str(cand["description"] or (_extract_snippet(content) if content else "")).strip()
        if not content and not snippet:
            continue
        draft_sources.append(
            {
                "title": title,
                "url": final_url,
                "domain": _url_domain(final_url),
                "snippet": snippet,
                "excerpt": content if content else snippet,
                "extracted": bool(content) and not err,
            }
        )

    recorded = record_citations(
        [
            {"title": s["title"], "url": s["url"], "domain": s["domain"], "snippet": s["snippet"]}
            for s in draft_sources
        ]
    )
    recorded_by_key = {_normalize_dedup_url(str(c.get("url") or "")): c for c in recorded}
    next_fallback_id = max((int(c.get("id") or 0) for c in recorded), default=0)

    sources: list[dict[str, Any]] = []
    for src in draft_sources:
        cited = recorded_by_key.get(_normalize_dedup_url(src["url"]))
        if cited is not None:
            sid = int(cited["id"])
        else:
            next_fallback_id += 1
            sid = next_fallback_id
        sources.append(
            {
                "id": sid,
                "title": str(cited["title"]) if cited else src["title"],
                "url": str(cited["url"]) if cited else src["url"],
                "domain": str(cited["domain"]) if cited else src["domain"],
                "snippet": str(cited["snippet"]) if cited else src["snippet"],
                "excerpt": src["excerpt"],
                "extracted": src["extracted"],
            }
        )

    digest_parts = [
        f"Deep Research: {query}",
        f"Queries ({len(queries)}): " + " | ".join(queries),
        f"Sources ({len(sources)}):",
    ]
    for src in sources:
        block = [
            f"\n[{src['id']}] {src['title']} ({src['domain']})",
            f"URL: {src['url']}",
        ]
        if src["snippet"]:
            block.append(f"Summary: {src['snippet']}")
        if src["excerpt"] and src["excerpt"] != src["snippet"]:
            block.append(f"Excerpt:\n{src['excerpt']}")
        digest_parts.append("\n".join(block))

    return json.dumps(
        {
            "success": bool(sources),
            "query": query,
            "queries": queries,
            "count": len(sources),
            "sources": sources,
            "digest": "\n".join(digest_parts).strip(),
            "skipped_unsafe": skipped_unsafe,
            **({"errors": search_errors} if search_errors else {}),
        },
        ensure_ascii=False,
    )


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
    "description": "Fetch one or more public HTTP(S) URLs concurrently and extract clean Markdown/text.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Single public URL to extract."},
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Batch of public URLs to extract concurrently (up to 10).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters per page (default 20000).",
                "default": 20000,
            },
        },
    },
}

DEEP_RESEARCH_SCHEMA: dict[str, Any] = {
    "name": "deep_research",
    "description": (
        "Run parallel multi-query web search, deduplicate safe public URLs, concurrently extract "
        "full page content, record structured citations, and return a `[1]..[N]` numbered research digest."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Primary research question or topic."},
            "sub_queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 3 focused sub-queries to search in parallel alongside the main query.",
            },
            "max_sources": {
                "type": "integer",
                "description": "Maximum number of distinct public web sources to extract (1 to 8, default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


def register_web_tools(registry: Any) -> None:
    registry.register("web_search", toolset=TOOLSET, schema=WEB_SEARCH_SCHEMA, handler=web_search_handler)
    registry.register("web_extract", toolset=TOOLSET, schema=WEB_EXTRACT_SCHEMA, handler=web_extract_handler)
    registry.register("deep_research", toolset=TOOLSET, schema=DEEP_RESEARCH_SCHEMA, handler=deep_research_handler)


__all__ = [
    "DEEP_RESEARCH_SCHEMA",
    "TOOLSET",
    "WEB_EXTRACT_SCHEMA",
    "WEB_SEARCH_SCHEMA",
    "deep_research_handler",
    "html_to_markdown",
    "is_safe_public_url",
    "register_web_tools",
    "web_extract_handler",
    "web_search_handler",
]
