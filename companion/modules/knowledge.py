"""Local Personal Knowledge Base / Document Vault powered by SQLite FTS5 (trigram + CJK LIKE hybrid)."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import mimetypes
import random
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from muse.interceptors import scrub_url_secrets
from muse.paths import get_muse_home

from . import creations, secrets_guard

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "默认"
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
TARGET_CHUNK_CHARS = 900
MAX_CHUNK_CHARS = 1000
OVERLAP_CHARS = 140
MAX_CHUNKS_PER_DOC = 500
DEFAULT_SEARCH_LIMIT = 6
MAX_SEARCH_LIMIT = 50
SNIPPET_CHARS = 240

_PDF_EXTS = frozenset({".pdf"})
_OFFICE_EXTS = frozenset({".docx", ".xlsx", ".pptx", ".odt"})
_HTML_EXTS = frozenset({".html", ".htm", ".xhtml"})
_MD_EXTS = frozenset({".md", ".markdown", ".mdx"})
_TEXT_EXTS = frozenset({
    ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".svg", ".rst", ".log", ".ini", ".toml", ".conf", ".cfg",
    ".example", ".sample", ".template",
})
_CODE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".swift",
    ".java", ".kt", ".kts", ".go", ".rs", ".sh", ".bash", ".zsh",
    ".css", ".scss", ".sql", ".c", ".h", ".cpp", ".hpp", ".rb", ".php",
})
_SUPPORTED_EXTS = _PDF_EXTS | _OFFICE_EXTS | _HTML_EXTS | _MD_EXTS | _TEXT_EXTS | _CODE_EXTS
_EXTENSIONLESS_TEXT_NAMES = frozenset({"readme", "license", "makefile", "dockerfile", "changelog"})

_MIME_BY_EXT: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdx": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".rst": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".py": "text/x-python",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".js": "text/javascript",
    ".jsx": "text/jsx",
    ".swift": "text/x-swift",
    ".java": "text/x-java-source",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".sh": "text/x-shellscript",
}

_SENTINEL_EXTRACT_LINES = frozenset({
    "[No extractable text found in PDF; install pypdf for complex encodings]",
    "[No extractable text on page]",
    "[Empty document]",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT '默认',
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'upload',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_documents_collection ON kb_documents(collection, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_documents_collection_sha ON kb_documents(collection, content_sha256);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id, chunk_index ASC);
"""

_FTS_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ad AFTER DELETE ON kb_chunks
BEGIN
    DELETE FROM kb_chunks_fts WHERE rowid = old.id;
END;
"""

_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x20000, 0x2A6DF),
    (0x3040, 0x309F),
    (0x30A0, 0x30FF),
    (0xAC00, 0xD7AF),
)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]+")
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9_.\-+#]+")
_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')
_PUNCT_SPLIT_RE = re.compile(r"[，。！？；：、（）()【】\[\]「」『』《》〈〉\"'`/\\|]+")
_FTS_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})
_CJK_STOP_CHARS = frozenset("的了是在我有你他她它这那和与及或把被给向往从到吗呢吧啊着过就都也还很最让使")

_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_PAGE_MARKER_RE = re.compile(
    r"^(?:---\s*\[((?:Page|Slide)\s+\d+(?:/\d+)?|sheet[^\]]*)\]\s*---"
    r"|\[((?:Page|Slide)\s+\d+(?:/\d+)?)\]"
    r"|(?:---+\s*)?(第\s*\d+\s*页)(?:\s*---+)?"
    r"|(?:Page\s+\d+(?:/\d+)?))$",
    re.IGNORECASE,
)

_DB_LOCKS_GUARD = threading.RLock()
_DB_LOCKS: dict[str, threading.RLock] = {}


def knowledge_db_path(home: Path | str | None = None) -> Path:
    """Resolve `<MUSE_HOME>/knowledge.db` dynamically so test `MUSE_HOME` overrides stay isolated."""
    base = Path(home).expanduser() if home is not None else get_muse_home()
    return base / "knowledge.db"


def _lock_for(db_path: Path) -> threading.RLock:
    key = str(db_path.resolve(strict=False))
    with _DB_LOCKS_GUARD:
        return _DB_LOCKS.setdefault(key, threading.RLock())


def _scrub_utf8(text: str) -> str:
    try:
        text.encode("utf-8")
        cleaned = text
    except UnicodeEncodeError:
        cleaned = text.encode("utf-8", "replace").decode("utf-8")
    return scrub_url_secrets(cleaned)


def _has_cjk(text: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in text)


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_collection(collection: str | None) -> str:
    clean = " ".join(str(collection or "").split())[:40].strip()
    return clean or DEFAULT_COLLECTION


def _normalize_filename(filename: str) -> str:
    raw = str(filename or "").strip()
    if not raw:
        raise ValueError("缺少文件名")
    if secrets_guard.is_secret_path(raw):
        raise ValueError("受保护的密钥或配置文件不能导入知识库")
    name = Path(raw.replace("\\", "/")).name.strip()
    if not name or name in (".", ".."):
        raise ValueError("文件名不合法")
    if secrets_guard.is_secret_path(name):
        raise ValueError("受保护的密钥或配置文件不能导入知识库")
    return name


def _guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or "text/plain"


def _ensure_fts_table(conn: sqlite3.Connection) -> str:
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'kb_chunks_fts'"
    ).fetchone()
    if existing is not None:
        return "unicode61" if "unicode61" in (existing["sql"] or "").lower() else "trigram"
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts "
            "USING fts5(title, heading, content, tokenize='trigram')"
        )
        return "trigram"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "trigram" not in msg and "tokenizer" not in msg:
            raise
        logger.warning("SQLite lacks FTS5 trigram tokenizer (%s); falling back to unicode61", type(exc).__name__)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts "
            "USING fts5(title, heading, content, tokenize='unicode61')"
        )
        return "unicode61"


@contextmanager
def _tx_on(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    deadline = time.monotonic() + 8.0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(random.uniform(0.01, 0.05))
    try:
        yield conn
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def _open_db(home: Path | str | None = None) -> Iterator[tuple[sqlite3.Connection, str]]:
    db_path = knowledge_db_path(home)
    lock = _lock_for(db_path)
    with lock:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(db_path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        break
                    time.sleep(random.uniform(0.01, 0.05))
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with _tx_on(conn):
                for stmt in _SCHEMA_SQL.strip().split(";"):
                    if stmt.strip():
                        conn.execute(stmt.strip())
                tokenizer = _ensure_fts_table(conn)
                conn.execute(_FTS_DELETE_TRIGGER_SQL.strip())
            yield conn, tokenizer
        finally:
            with contextlib.suppress(Exception):
                conn.close()


def _decode_text_bytes(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise ValueError("检测到二进制内容，无法作为文本导入知识库")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
        if text:
            bad = sum(1 for ch in text[:4096] if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\r\t"))
            if bad / min(len(text), 4096) > 0.12:
                raise ValueError("文件包含大量无法识别的二进制字符，暂不支持导入")
    if text:
        ctrl = sum(1 for ch in text[:4096] if ord(ch) < 32 and ch not in "\n\r\t")
        if ctrl / min(len(text), 4096) > 0.12:
            raise ValueError("检测到二进制或控制字符内容，无法作为文本文档导入")
    return text


def _filter_extracted_lines(lines: list[str]) -> str:
    kept: list[str] = []
    has_real_content = False
    for line in lines:
        stripped = (line or "").strip()
        if stripped in _SENTINEL_EXTRACT_LINES:
            continue
        kept.append(line.rstrip())
        if stripped and not _PAGE_MARKER_RE.match(stripped):
            has_real_content = True
    return "\n".join(kept).strip() if has_real_content else ""


def _extract_text(filename: str, data: bytes) -> tuple[str, str, str]:
    """Parse uploaded document bytes into `(clean_text, doc_kind, suggested_title)`."""
    if not data:
        raise ValueError("文件内容为空，无法导入知识库")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("文件大小超过 20 MB 上限")

    ext = Path(filename).suffix.lower()
    stem_low = filename.lower()
    if ext not in _SUPPORTED_EXTS and not (ext == "" and stem_low in _EXTENSIONLESS_TEXT_NAMES):
        raise ValueError(f"不支持的文件格式（{ext or '未知格式'}），请上传文档、表格、演示文稿、Markdown 或代码文件")

    if ext in _PDF_EXTS:
        # 延迟导入 muse.tools，避免与 muse.tools.knowledge 形成启动循环依赖
        from muse.tools import files as file_tools

        with tempfile.TemporaryDirectory(prefix="muse-kb-pdf-") as tmp:
            tmp_path = Path(tmp) / f"doc{ext}"
            tmp_path.write_bytes(data)
            try:
                lines = file_tools._extract_pdf_lines(tmp_path)
            except Exception as exc:
                raise ValueError(f"无法解析该 PDF 文件：{type(exc).__name__}") from exc
        text = _filter_extracted_lines(lines)
        if not text:
            raise ValueError("PDF 中没有可提取的文字内容")
        return text, "pdf", ""

    if ext in _OFFICE_EXTS:
        from muse.tools import files as file_tools

        with tempfile.TemporaryDirectory(prefix="muse-kb-office-") as tmp:
            tmp_path = Path(tmp) / f"doc{ext}"
            tmp_path.write_bytes(data)
            try:
                lines = file_tools._extract_office_lines(tmp_path)
            except (zipfile.BadZipFile, ET.ParseError, OSError, ValueError) as exc:
                raise ValueError(f"无法解析该 Office 文档，请确认文件未损坏（{type(exc).__name__}）") from exc
        text = _filter_extracted_lines(lines)
        if not text:
            raise ValueError("文档内容为空，无法导入知识库")
        return text, "office", ""

    decoded = _decode_text_bytes(data)
    if ext in _HTML_EXTS:
        from muse.tools.web import html_to_markdown

        html_title, md_text = html_to_markdown(decoded)
        if not md_text.strip():
            raise ValueError("网页文档正文为空，无法导入知识库")
        return md_text.strip(), "markdown", html_title.strip()

    if not decoded.strip():
        raise ValueError("文档内容为空，无法导入知识库")

    if ext in _MD_EXTS:
        return decoded.strip(), "markdown", ""
    if ext in _CODE_EXTS or ext in {".json", ".jsonl", ".yaml", ".yml", ".xml", ".svg", ".ini", ".toml", ".conf", ".cfg"}:
        return decoded.strip(), "code", ""
    return decoded.strip(), "text", ""


def _tail_overlap(text: str, *, target: int = OVERLAP_CHARS) -> str:
    if len(text) <= target:
        return text
    min_keep = min(len(text), 120)
    max_keep = min(len(text), 150)
    raw = text[-target:]
    # Snap cleanly near a newline or word boundary inside [120, 150] chars when available.
    search_slice = text[-max_keep:]
    best_offset = len(search_slice) - target
    for sep in ("\n", "。", "！", "？", ". ", " "):
        idx = search_slice.find(sep, max(0, len(search_slice) - max_keep), len(search_slice) - min_keep + 1)
        if idx >= 0:
            candidate = search_slice[idx + len(sep) :].lstrip()
            if 120 <= len(candidate) <= 150:
                return candidate
    _ = best_offset
    return raw.lstrip() or raw


def _split_oversized_paragraph(para: str, *, max_len: int = MAX_CHUNK_CHARS) -> list[str]:
    text = para.strip()
    if len(text) <= max_len:
        return [text] if text else []

    pieces: list[str] = []
    pos = 0
    n = len(text)
    step_target = min(TARGET_CHUNK_CHARS, max_len)
    while pos < n:
        end = min(n, pos + step_target)
        if end < n:
            window = text[pos:end]
            split_at = -1
            for sep in ("\n", "。", "！", "？", ". ", "! ", "? ", "；", ";", " "):
                found = window.rfind(sep, max(0, len(window) // 2))
                if found >= 0:
                    split_at = pos + found + len(sep)
                    break
            if split_at > pos:
                end = split_at
        piece = text[pos:end].strip()
        if piece:
            pieces.append(piece)
        if end >= n:
            break
        next_pos = max(pos + 1, end - OVERLAP_CHARS)
        pos = next_pos
    return pieces


def _split_paragraphs(body: str) -> list[str]:
    paras: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur.append(line)
            continue
        if not in_fence and not line.strip():
            if cur:
                joined = "\n".join(cur).strip()
                if joined:
                    paras.append(joined)
                cur = []
            continue
        cur.append(line)
    if cur:
        joined = "\n".join(cur).strip()
        if joined:
            paras.append(joined)
    return paras


def chunk_document_text(text: str, *, doc_kind: str = "markdown") -> list[dict[str, Any]]:
    """Split text into heading-aware chunks (~800-1000 chars, ~120-150 char tail overlap, max 500)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    allow_md_headings = doc_kind != "code"
    page_label = ""
    heading_stack = ["", "", "", ""]
    sections: list[tuple[str, str]] = []
    cur_lines: list[str] = []
    in_fence = False

    def _current_heading() -> str:
        parts = [p for p in (page_label, *[h for h in heading_stack if h]) if p]
        return " / ".join(parts)

    def _flush_section() -> None:
        body = "\n".join(cur_lines).strip()
        cur_lines.clear()
        if body:
            sections.append((_current_heading(), body))

    for line in normalized.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur_lines.append(line)
            continue
        if not in_fence:
            stripped = line.strip()
            pm = _PAGE_MARKER_RE.match(stripped)
            if pm:
                _flush_section()
                raw_label = next((g for g in pm.groups() if g), "").strip()
                if raw_label.lower().startswith(("page ", "slide ", "sheet")):
                    page_label = f"[{raw_label}]"
                else:
                    page_label = " ".join(raw_label.split())
                continue
            if allow_md_headings:
                hm = _MD_HEADING_RE.match(stripped)
                if hm:
                    _flush_section()
                    level = len(hm.group(1))
                    heading_text = " ".join(hm.group(2).split()).strip()
                    if heading_text:
                        heading_stack[level - 1] = heading_text
                        for idx in range(level, 4):
                            heading_stack[idx] = ""
                    continue
        cur_lines.append(line)
    _flush_section()

    if not sections:
        sections = [(_current_heading(), normalized)]

    chunks: list[dict[str, Any]] = []
    max_piece = MAX_CHUNK_CHARS - 152  # Ensures <= 150-char overlap + "\\n\\n" + piece always stays <= 1000 chars.
    for heading, body in sections:
        paragraphs: list[str] = []
        for para in _split_paragraphs(body):
            paragraphs.extend(_split_oversized_paragraph(para, max_len=max_piece))
        if not paragraphs:
            continue

        buf: list[str] = []
        buf_len = 0
        for para in paragraphs:
            sep_len = 2 if buf else 0
            if buf and buf_len + sep_len + len(para) > TARGET_CHUNK_CHARS:
                emitted = "\n\n".join(buf)
                chunks.append({"heading": heading, "content": emitted})
                if len(chunks) >= MAX_CHUNKS_PER_DOC:
                    break
                tail = _tail_overlap(emitted)
                if tail and len(tail) + 2 + len(para) <= MAX_CHUNK_CHARS:
                    buf = [tail, para]
                    buf_len = len(tail) + 2 + len(para)
                else:
                    buf = [para]
                    buf_len = len(para)
            else:
                buf.append(para)
                buf_len += sep_len + len(para)
        if len(chunks) >= MAX_CHUNKS_PER_DOC:
            break
        if buf:
            chunks.append({"heading": heading, "content": "\n\n".join(buf)})
            if len(chunks) >= MAX_CHUNKS_PER_DOC:
                break

    for idx, ch in enumerate(chunks):
        ch["chunk_index"] = idx
    return chunks


def _doc_row_to_dict(row: sqlite3.Row, *, deduplicated: bool | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "filename": str(row["filename"]),
        "collection": str(row["collection"]),
        "mime": str(row["mime"]),
        "size_bytes": int(row["size_bytes"]),
        "char_count": int(row["char_count"]),
        "chunk_count": int(row["chunk_count"]),
        "content_sha256": str(row["content_sha256"]),
        "origin": str(row["origin"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    if deduplicated is not None:
        out["deduplicated"] = deduplicated
    return out


def ingest_bytes(
    filename: str,
    data: bytes,
    *,
    title: str | None = None,
    collection: str = DEFAULT_COLLECTION,
    origin: str = "upload",
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Parse, scrub, chunk, and store a document into `<MUSE_HOME>/knowledge.db` (idempotent per collection+sha256)."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("文件内容格式不正确")
    raw_bytes = bytes(data)
    clean_filename = _normalize_filename(filename)
    clean_collection = _normalize_collection(collection)
    clean_origin = " ".join(str(origin or "upload").split())[:32] or "upload"

    extracted, doc_kind, html_title = _extract_text(clean_filename, raw_bytes)
    clean_text = _scrub_utf8(extracted).strip()
    if not clean_text:
        raise ValueError("文档内容为空，无法导入知识库")

    explicit_title = _scrub_utf8(" ".join(str(title).split())[:120]).strip() if title is not None and str(title).strip() else None
    resolved_title = explicit_title or _scrub_utf8(html_title[:120]).strip() or Path(clean_filename).stem or "未命名文档"
    mime = _guess_mime(clean_filename)
    content_sha256 = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
    now = time.time()

    with _open_db(home) as (conn, _):
        with _tx_on(conn):
            existing = conn.execute(
                "SELECT * FROM kb_documents WHERE collection = ? AND content_sha256 = ?",
                (clean_collection, content_sha256),
            ).fetchone()
            if existing is not None:
                doc_id = str(existing["id"])
                new_title = explicit_title if explicit_title else str(existing["title"])
                conn.execute(
                    "UPDATE kb_documents SET title = ?, updated_at = ? WHERE id = ?",
                    (new_title, now, doc_id),
                )
                if explicit_title and explicit_title != existing["title"]:
                    conn.execute(
                        "UPDATE kb_chunks_fts SET title = ? WHERE rowid IN (SELECT id FROM kb_chunks WHERE doc_id = ?)",
                        (new_title, doc_id),
                    )
                updated_row = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
                assert updated_row is not None
                return _doc_row_to_dict(updated_row, deduplicated=True)

            chunks = chunk_document_text(clean_text, doc_kind=doc_kind)
            if not chunks:
                raise ValueError("文档未能切分出有效段落，无法导入知识库")

            doc_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO kb_documents "
                "(id, title, filename, collection, mime, size_bytes, char_count, chunk_count, content_sha256, origin, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    resolved_title,
                    clean_filename,
                    clean_collection,
                    mime,
                    len(raw_bytes),
                    len(clean_text),
                    len(chunks),
                    content_sha256,
                    clean_origin,
                    now,
                    now,
                ),
            )
            for ch in chunks:
                cur = conn.execute(
                    "INSERT INTO kb_chunks (doc_id, chunk_index, heading, content) VALUES (?, ?, ?, ?)",
                    (doc_id, int(ch["chunk_index"]), str(ch["heading"]), str(ch["content"])),
                )
                chunk_id = int(cur.lastrowid or 0)
                conn.execute(
                    "INSERT INTO kb_chunks_fts (rowid, title, heading, content) VALUES (?, ?, ?, ?)",
                    (chunk_id, resolved_title, str(ch["heading"]), str(ch["content"])),
                )

            created_row = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
            assert created_row is not None
            return _doc_row_to_dict(created_row, deduplicated=False)


def ingest_file(
    path: Path | str,
    *,
    title: str | None = None,
    collection: str = DEFAULT_COLLECTION,
    origin: str = "local",
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Validate `path` with `secrets_guard` and ingest its contents into the knowledge base."""
    if secrets_guard.is_secret_path(path):
        raise ValueError("受保护的密钥或配置文件不能导入知识库")
    resolved = Path(path).expanduser().resolve()
    if secrets_guard.is_secret_path(resolved):
        raise ValueError("受保护的密钥或配置文件不能导入知识库")
    if not resolved.is_file():
        raise ValueError(f"找不到文件：{resolved.name}")
    if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("文件大小超过 20 MB 上限")
    return ingest_bytes(
        resolved.name,
        resolved.read_bytes(),
        title=title,
        collection=collection,
        origin=origin,
        home=home,
    )


def import_from_creation(
    creation_id: str,
    *,
    collection: str = DEFAULT_COLLECTION,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Import a safe document from `创意` (`creations.creations_root()`) into the knowledge base."""
    cid = str(creation_id or "").strip().replace("\\", "/")
    if not cid or cid.startswith("/") or any(part in ("", ".", "..") or part.startswith(".") for part in Path(cid).parts):
        raise ValueError("找不到这个作品")
    if secrets_guard.is_secret_path(cid):
        raise ValueError("受保护的敏感文件不能导入知识库")

    root = creations.creations_root()
    root_resolved = root.resolve()
    resolved = (root / cid).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError("找不到这个作品") from None
    if secrets_guard.is_secret_path(resolved) or not resolved.is_file():
        raise ValueError("找不到这个作品")
    if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("文件大小超过 20 MB 上限")

    meta = creations._read_index(root).get("entries", {}).get(cid, {})
    title = meta.get("title") or creations._display_title(resolved.stem)
    return ingest_bytes(
        resolved.name,
        resolved.read_bytes(),
        title=title,
        collection=collection,
        origin="creation",
        home=home,
    )


def _extract_query_terms(query: str) -> tuple[list[str], list[str]]:
    """Return `(primary_terms, sub_terms)` supporting mixed English, code symbols, 1-2 char CJK, and long sentences."""
    cleaned = " ".join((query or "").strip().split())[:1024]
    if not cleaned:
        return [], []

    primary: list[str] = []
    seen_primary: set[str] = set()

    def _add_primary(tok: str) -> None:
        t = tok.strip()
        if not t:
            return
        low = t.casefold()
        if low not in seen_primary:
            seen_primary.add(low)
            primary.append(t)

    for match in _TOKEN_RE.finditer(cleaned):
        phrase, bare = match.group(1), match.group(2)
        if phrase is not None:
            _add_primary(phrase)
            continue
        raw_tok = (bare or "").strip()
        if not raw_tok or raw_tok.upper() in _FTS_OPERATORS:
            continue
        if raw_tok.endswith("*") and len(raw_tok) > 1:
            raw_tok = raw_tok.rstrip("*")
        for piece in _PUNCT_SPLIT_RE.split(raw_tok):
            piece = piece.strip()
            if not piece or piece.upper() in _FTS_OPERATORS:
                continue
            _add_primary(piece)
            if _has_cjk(piece) and _ASCII_RUN_RE.search(piece):
                for m_cjk in _CJK_RUN_RE.finditer(piece):
                    _add_primary(m_cjk.group(0))
                for m_asc in _ASCII_RUN_RE.finditer(piece):
                    _add_primary(m_asc.group(0))

    sub_terms: list[str] = []
    seen_sub = set(seen_primary)

    def _add_sub(tok: str) -> None:
        low = tok.casefold()
        if tok and low not in seen_sub:
            seen_sub.add(low)
            sub_terms.append(tok)

    for term in primary:
        if not _has_cjk(term):
            continue
        for m_cjk in _CJK_RUN_RE.finditer(term):
            run = m_cjk.group(0)
            if len(run) < 4:
                continue
            for n_gram in (3, 2):
                for i in range(0, len(run) - n_gram + 1):
                    gram = run[i : i + n_gram]
                    if all(ch in _CJK_STOP_CHARS for ch in gram):
                        continue
                    if n_gram == 2 and (gram[0] in _CJK_STOP_CHARS or gram[-1] in _CJK_STOP_CHARS):
                        continue
                    _add_sub(gram)
                    if len(sub_terms) >= 24:
                        return primary, sub_terms
    return primary, sub_terms


def _build_snippet_and_matches(content: str, needles: list[str]) -> tuple[str, list[list[int]]]:
    clean = " ".join((content or "").split())
    if not clean:
        return "", []

    ordered_needles: list[str] = []
    seen: set[str] = set()
    for term in sorted((t for t in needles if t), key=len, reverse=True):
        low = term.casefold()
        if low not in seen:
            seen.add(low)
            ordered_needles.append(term)

    folded = clean.casefold()
    first_pos = -1
    for term in ordered_needles:
        pos = folded.find(term.casefold())
        if pos >= 0 and (first_pos < 0 or pos < first_pos):
            first_pos = pos

    if len(clean) <= SNIPPET_CHARS:
        snippet = clean
    else:
        start = max(0, first_pos - 72) if first_pos >= 0 else 0
        end = min(len(clean), start + SNIPPET_CHARS)
        if end == len(clean):
            start = max(0, end - SNIPPET_CHARS)
        snippet = ("…" if start > 0 else "") + clean[start:end] + ("…" if end < len(clean) else "")

    if not ordered_needles:
        return snippet, []

    pattern = re.compile("|".join(re.escape(t) for t in ordered_needles), re.IGNORECASE)
    matches = [[m.start(), m.end()] for m in pattern.finditer(snippet)]
    return snippet, matches


def _fts_usable(term: str, tokenizer: str) -> bool:
    return len(term) >= 3 if tokenizer == "trigram" else (bool(term) and not _has_cjk(term))


def search_knowledge(
    query: str,
    *,
    collection: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    home: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid FTS5 bm25 + CJK/short-term LIKE retrieval over knowledge chunks."""
    cleaned_query = " ".join((query or "").strip().split())[:1024]
    if not cleaned_query:
        return []
    cap = max(1, min(MAX_SEARCH_LIMIT, int(limit or DEFAULT_SEARCH_LIMIT)))
    db_path = knowledge_db_path(home)
    if not db_path.is_file():
        return []

    primary_terms, sub_terms = _extract_query_terms(cleaned_query)
    if not primary_terms:
        return []

    col_filter = _normalize_collection(collection) if collection is not None and str(collection).strip() else None
    all_terms = [*primary_terms, *sub_terms]

    with _open_db(home) as (conn, tokenizer):
        fts_terms = [t for t in all_terms if _fts_usable(t, tokenizer)][:16]
        like_terms = [t for t in all_terms if not _fts_usable(t, tokenizer)][:16]

        candidates: dict[int, dict[str, Any]] = {}
        fetch_cap = max(60, cap * 10)
        base_cols = (
            "c.id AS chunk_id, c.doc_id, d.title, d.filename, d.collection, "
            "c.chunk_index, c.heading, c.content, d.updated_at"
        )

        # Fast AND pass for multi-term queries (ensuring chunks matching all primary terms enter the pool).
        if len(primary_terms) >= 2:
            p_fts = [t for t in primary_terms if _fts_usable(t, tokenizer)]
            p_like = [t for t in primary_terms if not _fts_usable(t, tokenizer)]
            and_where: list[str] = []
            and_params: list[Any] = []
            if p_fts:
                and_where.append("kb_chunks_fts MATCH ?")
                and_params.append(" ".join('"' + t.replace('"', '""') + '"' for t in p_fts))
            for lt in p_like:
                pat = f"%{_escape_like(lt)}%"
                and_where.append("(d.title LIKE ? ESCAPE '\\' OR c.heading LIKE ? ESCAPE '\\' OR c.content LIKE ? ESCAPE '\\')")
                and_params.extend([pat, pat, pat])
            if col_filter is not None:
                and_where.append("d.collection = ?")
                and_params.append(col_filter)
            if p_fts:
                and_sql = (
                    f"SELECT {base_cols}, bm25(kb_chunks_fts, 2.0, 1.5, 1.0) AS rank_bm25 "
                    "FROM kb_chunks_fts "
                    "JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid "
                    "JOIN kb_documents d ON d.id = c.doc_id "
                    f"WHERE {' AND '.join(and_where)} "
                    "ORDER BY rank_bm25 ASC, d.updated_at DESC, c.chunk_index ASC LIMIT ?"
                )
                with contextlib.suppress(sqlite3.OperationalError):
                    for row in conn.execute(and_sql, [*and_params, fetch_cap]).fetchall():
                        cid = int(row["chunk_id"])
                        candidates[cid] = {**dict(row), "rank_bm25": float(row["rank_bm25"] or 0.0)}
            elif and_where:
                and_sql = (
                    f"SELECT {base_cols} FROM kb_chunks c "
                    "JOIN kb_documents d ON d.id = c.doc_id "
                    f"WHERE {' AND '.join(and_where)} "
                    "ORDER BY d.updated_at DESC, c.chunk_index ASC LIMIT ?"
                )
                for row in conn.execute(and_sql, [*and_params, fetch_cap]).fetchall():
                    cid = int(row["chunk_id"])
                    candidates[cid] = {**dict(row), "rank_bm25": None}

        if fts_terms:
            match_expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
            where_sql = "kb_chunks_fts MATCH ?"
            params: list[Any] = [match_expr]
            if col_filter is not None:
                where_sql += " AND d.collection = ?"
                params.append(col_filter)
            sql = (
                f"SELECT {base_cols}, bm25(kb_chunks_fts, 2.0, 1.5, 1.0) AS rank_bm25 "
                "FROM kb_chunks_fts "
                "JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid "
                "JOIN kb_documents d ON d.id = c.doc_id "
                f"WHERE {where_sql} "
                "ORDER BY rank_bm25 ASC, d.updated_at DESC, c.chunk_index ASC LIMIT ?"
            )
            with contextlib.suppress(sqlite3.OperationalError):
                for row in conn.execute(sql, [*params, fetch_cap]).fetchall():
                    cid = int(row["chunk_id"])
                    if cid not in candidates:
                        candidates[cid] = {**dict(row), "rank_bm25": float(row["rank_bm25"] or 0.0)}

        # Ensure short terms (1-2 char CJK/ASCII), unicode61 CJK terms, or fallback misses are recalled via LIKE.
        need_like = list(like_terms)
        if not candidates:
            for t in primary_terms:
                if t not in need_like:
                    need_like.append(t)
        if need_like:
            or_clauses = []
            like_params: list[Any] = []
            for lt in need_like[:20]:
                pat = f"%{_escape_like(lt)}%"
                or_clauses.append("(d.title LIKE ? ESCAPE '\\' OR c.heading LIKE ? ESCAPE '\\' OR c.content LIKE ? ESCAPE '\\')")
                like_params.extend([pat, pat, pat])
            where_parts = [f"({' OR '.join(or_clauses)})"]
            if col_filter is not None:
                where_parts.append("d.collection = ?")
                like_params.append(col_filter)
            sql_like = (
                f"SELECT {base_cols} FROM kb_chunks c "
                "JOIN kb_documents d ON d.id = c.doc_id "
                f"WHERE {' AND '.join(where_parts)} "
                "ORDER BY d.updated_at DESC, c.chunk_index ASC LIMIT ?"
            )
            for row in conn.execute(sql_like, [*like_params, fetch_cap]).fetchall():
                cid = int(row["chunk_id"])
                if cid not in candidates:
                    candidates[cid] = {**dict(row), "rank_bm25": None}

    if not candidates:
        return []

    folded_query = cleaned_query.casefold()
    scored: list[tuple[float, float, int, dict[str, Any], list[str]]] = []

    for cand in candidates.values():
        title_l = str(cand["title"]).casefold()
        heading_l = str(cand["heading"]).casefold()
        content_l = str(cand["content"]).casefold()

        score = 0.0
        matched_needles: list[str] = []

        if folded_query in title_l:
            score += 6.0
            matched_needles.append(cleaned_query)
        if folded_query in heading_l:
            score += 4.0
            matched_needles.append(cleaned_query)
        if folded_query in content_l:
            score += 5.0
            matched_needles.append(cleaned_query)

        primary_hits = 0
        for term in primary_terms:
            tl = term.casefold()
            hit_here = False
            if tl in title_l:
                score += 3.5
                hit_here = True
            if tl in heading_l:
                score += 2.2
                hit_here = True
            cnt = content_l.count(tl)
            if cnt > 0:
                score += 1.5 + 0.25 * min(6, cnt)
                hit_here = True
            if hit_here:
                primary_hits += 1
                matched_needles.append(term)

        if primary_hits > 0:
            score += 4.0 * (primary_hits / len(primary_terms))
            if primary_hits == len(primary_terms):
                score += 2.5

        sub_hits = 0
        for st in sub_terms:
            sl = st.casefold()
            hit_sub = False
            if sl in title_l:
                score += 0.9
                hit_sub = True
            if sl in heading_l:
                score += 0.6
                hit_sub = True
            if sl in content_l:
                score += 0.45
                hit_sub = True
            if hit_sub:
                sub_hits += 1
                matched_needles.append(st)

        if sub_terms and sub_hits > 0:
            score += 1.8 * (sub_hits / len(sub_terms))

        if score <= 0.0:
            continue

        bm25_raw = cand.get("rank_bm25")
        if bm25_raw is not None:
            bm25_pos = max(0.0, -float(bm25_raw))
            score += (bm25_pos / (1.0 + 0.15 * bm25_pos)) * 2.0

        scored.append((score, float(cand["updated_at"]), int(cand["chunk_index"]), cand, matched_needles))

    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

    results: list[dict[str, Any]] = []
    for score, _, _, cand, matched_needles in scored[:cap]:
        snippet, matches = _build_snippet_and_matches(str(cand["content"]), matched_needles)
        results.append({
            "doc_id": str(cand["doc_id"]),
            "title": str(cand["title"]),
            "filename": str(cand["filename"]),
            "collection": str(cand["collection"]),
            "chunk_index": int(cand["chunk_index"]),
            "heading": str(cand["heading"]),
            "snippet": snippet,
            "content": str(cand["content"]),
            "score": round(score, 4),
            "matches": matches,
        })
    return results


def list_knowledge(
    *,
    collection: str | None = None,
    query: str | None = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """List knowledge documents, collections, aggregate stats, and optional search hits."""
    clean_q = " ".join((query or "").strip().split())
    col_filter = _normalize_collection(collection) if collection is not None and str(collection).strip() else None
    db_path = knowledge_db_path(home)
    if not db_path.is_file():
        out: dict[str, Any] = {
            "documents": [],
            "collections": [],
            "collection_counts": {},
            "stats": {"doc_count": 0, "chunk_count": 0, "total_bytes": 0},
        }
        if clean_q:
            out["hits"] = []
        return out

    with _open_db(home) as (conn, _):
        all_col_rows = conn.execute(
            "SELECT collection, COUNT(*) AS cnt FROM kb_documents GROUP BY collection"
        ).fetchall()
        collection_counts: dict[str, int] = {str(r["collection"]): int(r["cnt"]) for r in all_col_rows}
        collections = sorted(
            collection_counts.keys(),
            key=lambda name: (0 if name == DEFAULT_COLLECTION else 1, name),
        )

        if col_filter is not None:
            doc_rows = conn.execute(
                "SELECT * FROM kb_documents WHERE collection = ? ORDER BY updated_at DESC, created_at DESC",
                (col_filter,),
            ).fetchall()
        else:
            doc_rows = conn.execute(
                "SELECT * FROM kb_documents ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()

    documents = [_doc_row_to_dict(r) for r in doc_rows]
    stats = {
        "doc_count": len(documents),
        "chunk_count": sum(d["chunk_count"] for d in documents),
        "total_bytes": sum(d["size_bytes"] for d in documents),
    }
    payload: dict[str, Any] = {
        "documents": documents,
        "collections": collections,
        "collection_counts": collection_counts,
        "stats": stats,
    }
    if clean_q:
        payload["hits"] = search_knowledge(clean_q, collection=col_filter, home=home)
    return payload


def update_document(
    doc_id: str,
    *,
    title: str | None = None,
    collection: str | None = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Update document title and/or collection, keeping `kb_chunks_fts.title` in sync."""
    clean_id = str(doc_id or "").strip()
    if not clean_id:
        raise ValueError("缺少文档 ID")
    db_path = knowledge_db_path(home)
    if not db_path.is_file():
        raise ValueError("找不到这篇知识库文档")

    new_title: str | None = None
    if title is not None:
        new_title = _scrub_utf8(" ".join(str(title).split())[:120]).strip()
        if not new_title:
            raise ValueError("文档标题不能为空")

    new_collection: str | None = None
    if collection is not None:
        raw_col = " ".join(str(collection).split())[:40].strip()
        if not raw_col:
            raise ValueError("分组名称不能为空")
        new_collection = raw_col

    now = time.time()
    with _open_db(home) as (conn, _):
        with _tx_on(conn):
            existing = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (clean_id,)).fetchone()
            if existing is None:
                raise ValueError("找不到这篇知识库文档")

            eff_title = new_title if new_title is not None else str(existing["title"])
            eff_col = new_collection if new_collection is not None else str(existing["collection"])

            if eff_col != existing["collection"]:
                dup = conn.execute(
                    "SELECT id FROM kb_documents WHERE collection = ? AND content_sha256 = ? AND id != ?",
                    (eff_col, existing["content_sha256"], clean_id),
                ).fetchone()
                if dup is not None:
                    raise ValueError(f"分组「{eff_col}」中已存在相同内容的文档")

            conn.execute(
                "UPDATE kb_documents SET title = ?, collection = ?, updated_at = ? WHERE id = ?",
                (eff_title, eff_col, now, clean_id),
            )
            if eff_title != existing["title"]:
                conn.execute(
                    "UPDATE kb_chunks_fts SET title = ? WHERE rowid IN (SELECT id FROM kb_chunks WHERE doc_id = ?)",
                    (eff_title, clean_id),
                )
            updated = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (clean_id,)).fetchone()
            assert updated is not None
            return _doc_row_to_dict(updated)


def delete_document(doc_id: str, *, home: Path | str | None = None) -> None:
    """Delete a document and cascade-clean `kb_chunks` and `kb_chunks_fts`."""
    clean_id = str(doc_id or "").strip()
    if not clean_id:
        raise ValueError("缺少文档 ID")
    db_path = knowledge_db_path(home)
    if not db_path.is_file():
        raise ValueError("找不到这篇知识库文档")

    with _open_db(home) as (conn, _):
        with _tx_on(conn):
            cur = conn.execute("DELETE FROM kb_documents WHERE id = ?", (clean_id,))
            if cur.rowcount == 0:
                raise ValueError("找不到这篇知识库文档")


__all__ = [
    "DEFAULT_COLLECTION",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_CHUNKS_PER_DOC",
    "MAX_DOCUMENT_BYTES",
    "MAX_SEARCH_LIMIT",
    "chunk_document_text",
    "delete_document",
    "import_from_creation",
    "ingest_bytes",
    "ingest_file",
    "knowledge_db_path",
    "list_knowledge",
    "search_knowledge",
    "update_document",
]
