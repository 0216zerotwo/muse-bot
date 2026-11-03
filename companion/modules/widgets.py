"""Small pages in replies (the free-form fallback of the app's interactive cards).

The companion writes an HTML file and puts a card in its reply::

    ```card
    {"type": "widget", "title": "这个月的开销", "file": "/abs/path/page.html"}
    ```

Before the reply reaches the app, :func:`rewrite_cards` copies the file into
``<companion dir>/widgets/served`` and swaps ``file`` for a signed ``url``; the
app shows it in a sandboxed frame. :func:`page` serves it with a bridge script
in front (theme colors, auto height, ``companion.send(text)`` /
``data-send="…"`` to answer as the user) and a content policy that lets it load
chart libraries from public CDNs but never send data anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from .paths import get_webui_dir

PREFIX = "/api/webui/companion/widgets"
# Models sometimes label a card block ``json``; the app treats a json block that is a card as one.
# ``ui`` blocks are whole generated pages: held back while written, never rewritten.
_FENCE = re.compile(r"```(?:card|json|ui)[ \t]*\n([\s\S]*?)\n```")
_OPEN_FENCE = re.compile(r"```(?:card|json|ui)[ \t]*\n")
_MAX_BYTES = 2 * 1024 * 1024
_CDNS = "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com https://esm.sh"
POLICY = (
    "default-src 'none'; "
    f"script-src 'unsafe-inline' {_CDNS}; "
    f"style-src 'unsafe-inline' {_CDNS} https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com data:; "
    "img-src data: blob:; media-src data: blob:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)

BRIDGE = """<script>
(function () {
  var post = function (message) { parent.postMessage(message, "*"); };
  window.companion = {
    send: function (text) { if (text) post({ type: "companion:send", text: String(text) }); }
  };
  document.addEventListener("click", function (event) {
    var target = event.target.closest && event.target.closest("[data-send]");
    if (target) { event.preventDefault(); window.companion.send(target.getAttribute("data-send") || target.textContent.trim()); }
  });
  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type !== "companion:theme" || !data.tokens) return;
    for (var name in data.tokens) document.documentElement.style.setProperty(name, data.tokens[name]);
  });
  var report = function () { post({ type: "companion:height", height: Math.ceil(document.documentElement.scrollHeight) }); };
  window.addEventListener("load", function () {
    report();
    if (window.ResizeObserver) new ResizeObserver(report).observe(document.documentElement);
  });
})();
</script>
<style>
:root { --c-bg: #f8f3ef; --c-surface: #fffefd; --c-text: #1e1814; --c-text-muted: #6e6058; --c-border: rgb(30 24 20 / 0.085);
  --c-primary: #bd5a02; --c-primary-text: #fff; --c-accent: #e67f3b; --c-accent-soft: #fee1d1; --c-fill: #efe7e0; }
html, body { margin: 0; background: transparent; color: var(--c-text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", "HarmonyOS Sans SC", "MiSans", system-ui, sans-serif; }
body { padding: 12px 14px 14px; }
button { font: inherit; border: 0; border-radius: 999px; padding: 8px 14px; background: var(--c-fill); color: var(--c-text); }
button.primary, [data-primary] { background: var(--c-primary); color: var(--c-primary-text); }
</style>
"""


def _served_root() -> Path:
    return get_webui_dir() / "widgets" / "served"


def stage(source: Path) -> Optional[str]:
    """Copy the page into the served folder; the same file always gets the same name (and link)."""
    try:
        stat = source.stat()
    except OSError:
        return None
    if not source.is_file() or source.suffix.lower() not in (".html", ".htm") or stat.st_size > _MAX_BYTES:
        return None
    key = hashlib.sha1(f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()[:20]
    target = _served_root() / f"{key}.html"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return target.name


def rewrite_cards(text: str, sign: Callable[[str], str]) -> str:
    """Swap each finished widget card's ``file`` for a signed ``url`` (others are left as written)."""

    def replace(match: re.Match) -> str:
        try:
            card = json.loads(match.group(1))
        except ValueError:
            return match.group(0)
        if not isinstance(card, dict) or card.get("type") != "widget" or not card.get("file"):
            return match.group(0)
        name = stage(Path(str(card.pop("file"))).expanduser())
        if name is None:
            return ""  # the page isn't there: show nothing rather than a broken card
        card["url"] = sign(name)
        return "```card\n" + json.dumps(card, ensure_ascii=False) + "\n```"

    return _FENCE.sub(replace, text)


def hold_back_open_card(text: str) -> str:
    """While a reply streams, a card still being written shows only its opening line (the app draws
    a placeholder); its body appears once complete, so the stream never has to be rewritten."""
    closed_until = 0
    for match in _FENCE.finditer(text):
        closed_until = match.end()
    opening = _OPEN_FENCE.search(text, closed_until)
    return text[: opening.end()] if opening else text


def page(name: str) -> Optional[bytes]:
    """The served page with the bridge in front, or None."""
    path = _served_root() / name
    if "/" in name or "\\" in name or name.startswith(".") or not path.is_file():
        return None
    html = path.read_text(encoding="utf-8", errors="replace")
    # Right after <head>, else after the doctype (before it would put the page in quirks mode).
    anchor = re.search(r"<head(?:\s[^>]*)?>", html, re.I) or re.match(r"\s*<!doctype[^>]*>", html, re.I)
    at = anchor.end() if anchor else 0
    html = html[:at] + BRIDGE + html[at:]
    return html.encode("utf-8")


def strip_cards(text: str) -> str:
    """Reply text without card blocks (notification previews, file titles)."""
    return _FENCE.sub("", text).strip()
