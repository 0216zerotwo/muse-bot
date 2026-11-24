"""File tools: `read_file` (text/PDF/Office), atomic `write_file`, fuzzy `patch`, `search_files`."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Any, Optional

TOOLSET = "file"
MAX_READ_BYTES = 10 * 1024 * 1024
MAX_LINE_DISPLAY_CHARS = 2000
_SKIP_SEARCH_DIRS = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
})
_OFFICE_EXTS = frozenset({".docx", ".xlsx", ".pptx", ".odt"})
_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _resolve_path(raw_path: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("File path is required")
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


def _decode_pdf_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == 0x5C and i + 1 < n:
            nxt = raw[i + 1]
            esc_map = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("("): 40, ord(")"): 41, 0x5C: 92}
            if nxt in esc_map:
                out.append(esc_map[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    if out.startswith(b"\xfe\xff"):
        return bytes(out[2:]).decode("utf-16-be", errors="replace")
    return bytes(out).decode("latin-1", errors="replace")


def _extract_pdf_stdlib(path: Path) -> list[str]:
    raw_pdf = path.read_bytes()
    stream_re = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
    text_op_re = re.compile(rb"\((?:\\.|[^\\)])*\)\s*Tj|\[(.*?)\]\s*TJ", re.DOTALL)
    lit_re = re.compile(rb"\((?:\\.|[^\\)])*\)")
    lines: list[str] = []
    page_num = 0

    for m in stream_re.finditer(raw_pdf):
        header, body = m.group(1), m.group(2)
        if b"/FlateDecode" in header:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        if b"BT" not in body or b"ET" not in body:
            continue
        page_num += 1
        chunks: list[str] = []
        for block in re.findall(rb"BT(.*?)ET", body, flags=re.DOTALL):
            for op_match in text_op_re.finditer(block):
                full_op = op_match.group(0)
                for lit in lit_re.findall(full_op):
                    chunks.append(_decode_pdf_literal(lit[1:-1]))
        if chunks:
            lines.append(f"--- [Page {page_num}] ---")
            joined = "".join(chunks).strip()
            lines.extend(joined.splitlines() or [joined])
    return lines or ["[No extractable text found in PDF; install pypdf for complex encodings]"]


def _extract_pdf_lines(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(path))
        lines: list[str] = []
        for idx, page in enumerate(reader.pages, start=1):
            lines.append(f"--- [Page {idx}/{len(reader.pages)}] ---")
            txt = (page.extract_text() or "").strip()
            if txt:
                lines.extend(txt.splitlines())
            else:
                lines.append("[No extractable text on page]")
        return lines
    except ImportError:
        pass

    pdftotext_bin = shutil.which("pdftotext")
    if pdftotext_bin:
        with contextlib.suppress(Exception):
            proc = subprocess.run(
                [pdftotext_bin, str(path), "-"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.splitlines()

    return _extract_pdf_stdlib(path)


def _xml_text_fragments(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    out: list[str] = []
    for elem in root.iter():
        if elem.text and elem.text.strip():
            out.append(elem.text.strip())
    return out


def _extract_office_lines(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    lines: list[str] = []
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if suffix == ".docx" and "word/document.xml" in names:
            root = ET.fromstring(zf.read("word/document.xml"))
            for elem in root.iter():
                if elem.tag.endswith("}p"):
                    runs = [
                        child.text
                        for child in elem.iter()
                        if child.tag.endswith("}t") and child.text
                    ]
                    para = "".join(runs).strip()
                    if para:
                        lines.append(para)
        elif suffix == ".xlsx":
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter():
                    if si.tag.endswith("}si"):
                        parts = [t.text for t in si.iter() if t.tag.endswith("}t") and t.text]
                        shared.append("".join(parts))
            sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
            for sname in sheets:
                lines.append(f"--- [{Path(sname).stem}] ---")
                wroot = ET.fromstring(zf.read(sname))
                for row in wroot.iter():
                    if not row.tag.endswith("}row"):
                        continue
                    vals: list[str] = []
                    for cell in row:
                        if not cell.tag.endswith("}c"):
                            continue
                        ctype = cell.attrib.get("t", "")
                        raw_v = ""
                        for ch in cell:
                            if ch.tag.endswith("}v") and ch.text:
                                raw_v = ch.text
                            elif ch.tag.endswith("}is"):
                                raw_v = "".join(
                                    t.text for t in ch.iter() if t.tag.endswith("}t") and t.text
                                )
                        if ctype == "s" and raw_v.isdigit() and int(raw_v) < len(shared):
                            vals.append(shared[int(raw_v)])
                        elif raw_v:
                            vals.append(raw_v)
                    if vals:
                        lines.append("\t".join(vals))
        elif suffix == ".pptx":
            slides = sorted(n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
            for idx, sname in enumerate(slides, start=1):
                lines.append(f"--- [Slide {idx}] ---")
                lines.extend(_xml_text_fragments(zf.read(sname)))
        elif suffix == ".odt" and "content.xml" in names:
            lines.extend(_xml_text_fragments(zf.read("content.xml")))
    return lines or ["[Empty document]"]


def _format_paginated_lines(
    lines: list[str],
    *,
    offset: int,
    limit: int,
    path: Path,
) -> dict[str, Any]:
    total = len(lines)
    start_idx = max(0, int(offset) - 1)
    max_lines = max(1, min(2000, int(limit)))
    end_idx = min(total, start_idx + max_lines)
    numbered: list[str] = []
    for idx in range(start_idx, end_idx):
        line = lines[idx]
        if len(line) > MAX_LINE_DISPLAY_CHARS:
            line = line[:MAX_LINE_DISPLAY_CHARS] + " ... [line truncated]"
        numbered.append(f"{idx + 1:>6}\t{line}")
    return {
        "success": True,
        "path": str(path),
        "total_lines": total,
        "start_line": (start_idx + 1) if total > 0 else 0,
        "end_line": end_idx,
        "truncated": end_idx < total,
        "content": "\n".join(numbered),
    }


def read_file_handler(args: dict[str, Any], **_: Any) -> str:
    """Read a text, PDF, or Office file with 1-based line pagination."""
    raw_path = str(args.get("path") or args.get("file_path") or "").strip()
    offset = max(1, int(args.get("offset") or args.get("start_line") or 1))
    limit = max(1, min(2000, int(args.get("limit") or 200)))
    try:
        path = _resolve_path(raw_path)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    if not path.exists():
        return json.dumps(
            {"success": False, "error": f"File not found: {path}"},
            ensure_ascii=False,
        )
    if path.is_dir():
        entries = sorted(
            f"{p.name}/" if p.is_dir() else p.name
            for p in path.iterdir()
        )
        return json.dumps(
            {
                "success": True,
                "path": str(path),
                "is_directory": True,
                "entries": entries[:500],
            },
            ensure_ascii=False,
        )

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            lines = _extract_pdf_lines(path)
            return json.dumps(
                _format_paginated_lines(lines, offset=offset, limit=limit, path=path),
                ensure_ascii=False,
            )
        if suffix in _OFFICE_EXTS:
            lines = _extract_office_lines(path)
            return json.dumps(
                _format_paginated_lines(lines, offset=offset, limit=limit, path=path),
                ensure_ascii=False,
            )
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return json.dumps(
                {
                    "success": False,
                    "error": f"File exceeds {MAX_READ_BYTES} bytes ({size} bytes): {path}",
                },
                ensure_ascii=False,
            )
        raw_bytes = path.read_bytes()
        if b"\x00" in raw_bytes[:8192]:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Binary file cannot be read as text ({size} bytes): {path}",
                },
                ensure_ascii=False,
            )
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw_bytes.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return json.dumps(
            _format_paginated_lines(lines, offset=offset, limit=limit, path=path),
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


def atomic_write_bytes(path: Path, data: bytes, *, create_dirs: bool = True) -> None:
    """Write `data` to `path` atomically via sibling temporary file + `os.replace`."""
    parent = path.parent
    if create_dirs:
        parent.mkdir(parents=True, exist_ok=True)
    existing_mode: Optional[int] = None
    if os.name == "posix" and path.exists():
        with contextlib.suppress(OSError):
            existing_mode = path.stat().st_mode & 0o777

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if existing_mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def write_file_handler(args: dict[str, Any], **_: Any) -> str:
    """Atomically write UTF-8 text to a target file."""
    raw_path = str(args.get("path") or args.get("file_path") or "").strip()
    if "content" not in args:
        return json.dumps(
            {"success": False, "error": "Parameter 'content' is required."},
            ensure_ascii=False,
        )
    content = str(args.get("content") or "")
    create_dirs = bool(args.get("create_dirs", True))
    try:
        path = _resolve_path(raw_path)
        data = content.encode("utf-8")
        atomic_write_bytes(path, data, create_dirs=create_dirs)
        return json.dumps(
            {
                "success": True,
                "path": str(path),
                "bytes_written": len(data),
                "lines_written": len(content.splitlines()),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


def _fuzzy_line_norm(line: str) -> str:
    return " ".join(line.strip().split())


def _apply_search_replace(
    text: str,
    old_str: str,
    new_str: str,
    *,
    replace_all: bool = False,
) -> tuple[str, int, str]:
    """Apply exact, trailing-space-normalized, or whitespace-fuzzy replacement."""
    if not old_str:
        raise ValueError("old_string must be non-empty")

    exact_count = text.count(old_str)
    if exact_count > 0:
        if exact_count > 1 and not replace_all:
            raise ValueError(
                f"old_string matched {exact_count} locations; provide more context or set replace_all=True"
            )
        updated = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        return updated, (exact_count if replace_all else 1), "exact"

    src_lines = text.splitlines()
    old_lines = old_str.splitlines()
    new_lines = new_str.splitlines()
    if not old_lines or len(old_lines) > len(src_lines):
        raise ValueError("old_string not found in file")

    window = len(old_lines)
    # Pass 1: rstrip match; Pass 2: collapsed whitespace fuzzy match.
    for mode, norm_fn in (
        ("rstrip_fuzzy", lambda s: s.rstrip()),
        ("whitespace_fuzzy", _fuzzy_line_norm),
    ):
        target_sig = [norm_fn(line) for line in old_lines]
        if not any(target_sig):
            continue
        hits: list[int] = []
        idx = 0
        while idx <= len(src_lines) - window:
            cand_sig = [norm_fn(src_lines[idx + j]) for j in range(window)]
            if cand_sig == target_sig:
                hits.append(idx)
                idx += window if replace_all else 1
            else:
                idx += 1
        if hits:
            if len(hits) > 1 and not replace_all:
                raise ValueError(
                    f"Fuzzy match is ambiguous ({len(hits)} matches); include surrounding lines"
                )
            rebuilt = list(src_lines)
            for start in reversed(hits if replace_all else hits[:1]):
                rebuilt[start : start + window] = new_lines
            trailing_nl = "\n" if text.endswith("\n") else ""
            return "\n".join(rebuilt) + trailing_nl, (len(hits) if replace_all else 1), mode

    raise ValueError("old_string not found in file (exact and fuzzy matching failed)")


def _find_subseq(
    haystack: list[str],
    needle: list[str],
    hint_idx: int,
    norm_fn: Any = None,
) -> int:
    if not needle:
        return max(0, min(len(haystack), hint_idx))
    nlen = len(needle)
    target = [norm_fn(x) for x in needle] if norm_fn else needle
    candidates: list[int] = []
    for i in range(0, len(haystack) - nlen + 1):
        chunk = [norm_fn(haystack[i + j]) for j in range(nlen)] if norm_fn else haystack[i : i + nlen]
        if chunk == target:
            candidates.append(i)
    if not candidates:
        return -1
    candidates.sort(key=lambda pos: abs(pos - hint_idx))
    return candidates[0]


def _apply_unified_diff(text: str, diff_text: str) -> tuple[str, int]:
    src_lines = text.splitlines()
    raw_lines = diff_text.splitlines()
    hunks: list[tuple[int, list[str]]] = []
    cur_start = 1
    cur_body: Optional[list[str]] = None

    for line in raw_lines:
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if cur_body is not None:
                hunks.append((cur_start, cur_body))
            cur_start = max(1, int(m.group(1)))
            cur_body = []
            continue
        if cur_body is not None:
            if line.startswith("\\ No newline"):
                continue
            if line.startswith((" ", "+", "-")):
                cur_body.append(line)
            elif line == "":
                cur_body.append(" ")
    if cur_body is not None:
        hunks.append((cur_start, cur_body))
    if not hunks:
        raise ValueError("No valid @@ unified diff hunks found")

    offset = 0
    applied = 0
    for orig_start, body in hunks:
        before = [ln[1:] for ln in body if ln.startswith((" ", "-"))]
        after = [ln[1:] for ln in body if ln.startswith((" ", "+"))]
        hint = max(0, orig_start - 1 + offset)
        pos = _find_subseq(src_lines, before, hint)
        if pos < 0:
            pos = _find_subseq(src_lines, before, hint, norm_fn=lambda s: s.rstrip())
        if pos < 0:
            pos = _find_subseq(src_lines, before, hint, norm_fn=_fuzzy_line_norm)
        if pos < 0:
            raise ValueError(f"Failed to match unified diff hunk near line {orig_start}")
        src_lines[pos : pos + len(before)] = after
        offset += len(after) - len(before)
        applied += 1

    trailing_nl = "\n" if (text.endswith("\n") or not text) else ""
    return "\n".join(src_lines) + trailing_nl, applied


def patch_handler(args: dict[str, Any], **_: Any) -> str:
    """Patch an existing file via exact/fuzzy `old_string`->`new_string` or `unified_diff`."""
    raw_path = str(args.get("path") or args.get("file_path") or "").strip()
    try:
        path = _resolve_path(raw_path)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    if not path.is_file():
        return json.dumps(
            {"success": False, "error": f"Target file not found: {path}"},
            ensure_ascii=False,
        )

    try:
        raw_bytes = path.read_bytes()
        uses_crlf = b"\r\n" in raw_bytes
        norm_text = raw_bytes.decode("utf-8-sig").replace("\r\n", "\n")

        diff_text = args.get("unified_diff") or args.get("diff") or args.get("patch")
        if isinstance(diff_text, str) and "@@" in diff_text:
            updated_lf, count = _apply_unified_diff(norm_text, diff_text.replace("\r\n", "\n"))
            strategy = "unified_diff"
        else:
            old_s = str(args.get("old_string") if "old_string" in args else (args.get("target") or ""))
            if "new_string" not in args and "replacement" not in args:
                return json.dumps(
                    {
                        "success": False,
                        "error": "Provide either ('old_string' and 'new_string') or 'unified_diff'.",
                    },
                    ensure_ascii=False,
                )
            new_s = str(args.get("new_string") if "new_string" in args else (args.get("replacement") or ""))
            updated_lf, count, strategy = _apply_search_replace(
                norm_text,
                old_s.replace("\r\n", "\n"),
                new_s.replace("\r\n", "\n"),
                replace_all=bool(args.get("replace_all", False)),
            )

        final_text = updated_lf.replace("\n", "\r\n") if uses_crlf else updated_lf
        atomic_write_bytes(path, final_text.encode("utf-8"), create_dirs=False)
        return json.dumps(
            {
                "success": True,
                "path": str(path),
                "replacements": count,
                "strategy": strategy,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


def _search_with_rg(
    rg_bin: str,
    pattern: str,
    target: Path,
    *,
    file_glob: Optional[str],
    case_sensitive: Optional[bool],
    fixed_strings: bool,
    max_results: int,
) -> Optional[list[dict[str, Any]]]:
    cmd = [
        rg_bin,
        "--line-number",
        "--no-heading",
        "--color=never",
        "--with-filename",
        f"--max-count={max_results}",
    ]
    if fixed_strings:
        cmd.append("--fixed-strings")
    if case_sensitive is True:
        cmd.append("--case-sensitive")
    elif case_sensitive is False:
        cmd.append("--ignore-case")
    else:
        cmd.append("--smart-case")
    if file_glob:
        cmd.extend(["--glob", file_glob])
    cmd.extend(["--", pattern, str(target)])

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode not in (0, 1):
        return None
    matches: list[dict[str, Any]] = []
    line_re = re.compile(r"^(.*?):(\d+):(.*)$")
    for raw in completed.stdout.splitlines():
        m = line_re.match(raw)
        if not m:
            continue
        matches.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "text": m.group(3)[:500],
        })
        if len(matches) >= max_results:
            break
    return matches


def _search_pure_python(
    pattern: str,
    target: Path,
    *,
    file_glob: Optional[str],
    case_sensitive: Optional[bool],
    fixed_strings: bool,
    max_results: int,
) -> list[dict[str, Any]]:
    ignore_case = (case_sensitive is False) or (case_sensitive is None and pattern.islower())
    flags = re.IGNORECASE if ignore_case else 0
    compiled = re.compile(re.escape(pattern) if fixed_strings else pattern, flags)

    def _iter_files(base: Path) -> list[Path]:
        if base.is_file():
            return [base]
        collected: list[Path] = []
        for root_str, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_SEARCH_DIRS and not d.startswith(".")]
            root_path = Path(root_str)
            for fname in filenames:
                if file_glob and not fnmatch.fnmatch(fname, file_glob):
                    continue
                collected.append(root_path / fname)
        return sorted(collected)

    matches: list[dict[str, Any]] = []
    for fpath in _iter_files(target):
        with contextlib.suppress(OSError):
            if fpath.stat().st_size > 2 * 1024 * 1024:
                continue
            raw = fpath.read_bytes()
            if b"\x00" in raw[:4096]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    matches.append({
                        "file": str(fpath),
                        "line": lineno,
                        "text": line[:500],
                    })
                    if len(matches) >= max_results:
                        return matches
    return matches


def search_files_handler(args: dict[str, Any], **_: Any) -> str:
    """Search files using `rg` when available with automatic pure-Python regex/literal fallback."""
    pattern = str(args.get("pattern") or args.get("query") or "").strip()
    if not pattern:
        return json.dumps(
            {"success": False, "error": "Parameter 'pattern' is required."},
            ensure_ascii=False,
        )
    raw_target = str(args.get("path") or ".").strip()
    file_glob = str(args["file_glob"]).strip() if args.get("file_glob") else None
    fixed_strings = bool(args.get("fixed_strings", False))
    case_arg = args.get("case_sensitive")
    case_sensitive = bool(case_arg) if isinstance(case_arg, bool) else None
    max_results = max(1, min(500, int(args.get("max_results") or 100)))

    try:
        target = _resolve_path(raw_target)
        if not target.exists():
            return json.dumps(
                {"success": False, "error": f"Search path does not exist: {target}"},
                ensure_ascii=False,
            )
        rg_bin = shutil.which("rg")
        engine = "python"
        matches: Optional[list[dict[str, Any]]] = None
        if rg_bin:
            matches = _search_with_rg(
                rg_bin,
                pattern,
                target,
                file_glob=file_glob,
                case_sensitive=case_sensitive,
                fixed_strings=fixed_strings,
                max_results=max_results,
            )
            if matches is not None:
                engine = "rg"
        if matches is None:
            matches = _search_pure_python(
                pattern,
                target,
                file_glob=file_glob,
                case_sensitive=case_sensitive,
                fixed_strings=fixed_strings,
                max_results=max_results,
            )
        return json.dumps(
            {
                "success": True,
                "engine": engine,
                "count": len(matches),
                "truncated": len(matches) >= max_results,
                "matches": matches,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


READ_FILE_SCHEMA: dict[str, Any] = {
    "name": "read_file",
    "description": "Read a text, PDF (.pdf), or Office (.docx/.xlsx/.pptx) file with line pagination.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file or directory."},
            "offset": {
                "type": "integer",
                "description": "1-based starting line number (default 1).",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to return (default 200).",
                "default": 200,
            },
        },
        "required": ["path"],
    },
}

WRITE_FILE_SCHEMA: dict[str, Any] = {
    "name": "write_file",
    "description": "Atomically create or overwrite a UTF-8 file.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target file path."},
            "content": {"type": "string", "description": "Full text content to write."},
            "create_dirs": {
                "type": "boolean",
                "description": "Create parent directories if missing (default True).",
                "default": True,
            },
        },
        "required": ["path", "content"],
    },
}

PATCH_SCHEMA: dict[str, Any] = {
    "name": "patch",
    "description": (
        "Edit an existing file atomically using exact/whitespace-fuzzy search-and-replace "
        "(`old_string` -> `new_string`) or `unified_diff` hunks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the existing file."},
            "old_string": {"type": "string", "description": "Existing text snippet to replace."},
            "new_string": {"type": "string", "description": "Replacement text snippet."},
            "unified_diff": {"type": "string", "description": "Unified diff containing @@ hunks."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string (default False).",
                "default": False,
            },
        },
        "required": ["path"],
    },
}

SEARCH_FILES_SCHEMA: dict[str, Any] = {
    "name": "search_files",
    "description": "Search file contents by regex or literal string (`rg` fast path + Python fallback).",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression or literal string."},
            "path": {"type": "string", "description": "Directory or file path to search (default '.')."},
            "file_glob": {"type": "string", "description": "Optional filename glob filter, e.g. '*.py'."},
            "fixed_strings": {
                "type": "boolean",
                "description": "Treat pattern as literal text rather than regex.",
                "default": False,
            },
            "case_sensitive": {"type": "boolean", "description": "Force case sensitivity."},
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 100).",
                "default": 100,
            },
        },
        "required": ["pattern"],
    },
}


def register_file_tools(registry: Any) -> None:
    registry.register("read_file", toolset=TOOLSET, schema=READ_FILE_SCHEMA, handler=read_file_handler)
    registry.register("write_file", toolset=TOOLSET, schema=WRITE_FILE_SCHEMA, handler=write_file_handler)
    registry.register("patch", toolset=TOOLSET, schema=PATCH_SCHEMA, handler=patch_handler)
    registry.register(
        "search_files",
        toolset=TOOLSET,
        schema=SEARCH_FILES_SCHEMA,
        handler=search_files_handler,
    )


__all__ = [
    "PATCH_SCHEMA",
    "READ_FILE_SCHEMA",
    "SEARCH_FILES_SCHEMA",
    "TOOLSET",
    "WRITE_FILE_SCHEMA",
    "atomic_write_bytes",
    "patch_handler",
    "read_file_handler",
    "register_file_tools",
    "search_files_handler",
    "write_file_handler",
]
