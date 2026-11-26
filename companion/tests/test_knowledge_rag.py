"""Tests for the SQLite FTS5 local personal knowledge base engine and native RAG tools."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from _loader import load, temp_dir  # noqa: E402
from muse.paths import muse_home_override
from muse.tools import CORE_TOOLSETS, create_default_tool_registry
from muse.tools.knowledge import knowledge_list_handler, knowledge_search_handler


def _make_minimal_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "doc.docx"
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("word/document.xml", doc_xml.encode("utf-8"))
        return out.read_bytes()


def _make_minimal_xlsx(cell_text: str) -> bytes:
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<si><t>{cell_text}</t></si></sst>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>'
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sheet.xlsx"
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("xl/sharedStrings.xml", shared.encode("utf-8"))
            zf.writestr("xl/worksheets/sheet1.xml", sheet.encode("utf-8"))
        return out.read_bytes()


def _make_minimal_pptx(slides: list[str]) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "deck.pptx"
        with zipfile.ZipFile(out, "w") as zf:
            for idx, txt in enumerate(slides, start=1):
                xml = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{txt}</a:t></a:r></a:p>"
                    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
                )
                zf.writestr(f"ppt/slides/slide{idx}.xml", xml.encode("utf-8"))
        return out.read_bytes()


def _make_single_page_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream_bytes = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


class KnowledgeEngineAndRagTests(unittest.TestCase):
    def setUp(self):
        from companion.modules import knowledge

        self.kb = knowledge

    def test_muse_home_isolation_and_schema_wal(self):
        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            empty = self.kb.list_knowledge()
            self.assertEqual(empty["documents"], [])
            self.assertEqual(empty["collections"], [])
            self.assertEqual(empty["stats"], {"doc_count": 0, "chunk_count": 0, "total_bytes": 0})
            self.assertFalse((Path(tmp) / "knowledge.db").exists())

            doc = self.kb.ingest_bytes("notes.md", "# 标题\n\n测试内容落盘".encode("utf-8"))
            db_path = Path(tmp) / "knowledge.db"
            self.assertTrue(db_path.is_file())
            self.assertEqual(doc["collection"], "默认")
            self.assertGreater(doc["chunk_count"], 0)

            conn = sqlite3.connect(str(db_path))
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(str(mode).lower(), "wal")
                tables = {
                    r[0]
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                self.assertTrue({"kb_documents", "kb_chunks", "kb_chunks_fts"}.issubset(tables))
            finally:
                conn.close()

    def test_heading_aware_chunking_overlap_and_max_chunks(self):
        md = (
            "# 系统总览\n\n"
            "这是总览介绍段落。\n\n"
            "```python\n"
            "# 代码块里的注释不能当成一级标题\n"
            "def keep_inside():\n"
            "    return 42\n"
            "```\n\n"
            "## 核心模块\n\n"
            "### 混合检索\n\n"
            "采用 FTS5 trigram 与短词 LIKE 联合打分。\n"
        )
        chunks = self.kb.chunk_document_text(md, doc_kind="markdown")
        self.assertEqual(chunks[0]["heading"], "系统总览")
        self.assertIn("keep_inside", chunks[0]["content"])
        self.assertEqual(chunks[1]["heading"], "系统总览 / 核心模块 / 混合检索")

        # Long single section should chunk into ~800-1000 chars with ~120-150 char tail overlap.
        paras = [f"第{i:02d}段：关于个人知识库与本地混合检索的详细设计说明，包含关键术语检索与并发安全机制。" * 4 for i in range(18)]
        long_chunks = self.kb.chunk_document_text("# 长章节\n\n" + "\n\n".join(paras), doc_kind="markdown")
        self.assertGreaterEqual(len(long_chunks), 3)
        for ch in long_chunks[:-1]:
            self.assertGreaterEqual(len(ch["content"]), 750)
            self.assertLessEqual(len(ch["content"]), 1000)
        # Adjacent chunks share ~120-150 chars tail-to-head overlap.
        head_next = long_chunks[1]["content"][:160]
        tail_prev = long_chunks[0]["content"][-120:]
        self.assertIn(tail_prev[:80], head_next)

        # Page markers ([Page N] / 第 N 页) maintain heading breadcrumbs.
        paged = "[Page 1]\n第一页摘要内容。\n\n第 2 页\n第二页核心结论。"
        paged_chunks = self.kb.chunk_document_text(paged, doc_kind="pdf")
        self.assertEqual(paged_chunks[0]["heading"], "[Page 1]")
        self.assertEqual(paged_chunks[1]["heading"], "第 2 页")

        # Max chunks cap at 500.
        huge_md = "\n\n".join(f"## 节{i}\n\n段落内容 {i} " + ("文本" * 20) for i in range(560))
        capped = self.kb.chunk_document_text(huge_md, doc_kind="markdown")
        self.assertEqual(len(capped), 500)

    def test_parses_pdf_office_html_code_and_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            pdf_doc = self.kb.ingest_bytes("spec.pdf", _make_single_page_pdf("MuseBot PDF Knowledge Vault"))
            docx_doc = self.kb.ingest_bytes("memo.docx", _make_minimal_docx(["第一段会议纪要", "包含项目排期"]))
            xlsx_doc = self.kb.ingest_bytes("budget.xlsx", _make_minimal_xlsx("Q4研发预算总表"))
            pptx_doc = self.kb.ingest_bytes("slides.pptx", _make_minimal_pptx(["架构演进路线图", "第二页细节"]))
            html_doc = self.kb.ingest_bytes(
                "guide.html",
                b"<html><head><title>Web Guide</title></head><body><h1>Install</h1><p>Run setup script.</p></body></html>",
            )
            py_doc = self.kb.ingest_bytes(
                "engine.py",
                b"# module comment should not split heading\ndef hybrid_rank_score(x):\n    return x * 2\n",
            )
            self.assertEqual(html_doc["title"], "Web Guide")
            self.assertGreater(pdf_doc["chunk_count"], 0)
            self.assertGreater(docx_doc["chunk_count"], 0)
            self.assertGreater(xlsx_doc["chunk_count"], 0)
            self.assertGreater(pptx_doc["chunk_count"], 0)
            self.assertGreater(py_doc["chunk_count"], 0)

            self.assertTrue(self.kb.search_knowledge("Knowledge Vault"))
            self.assertTrue(self.kb.search_knowledge("会议纪要"))
            self.assertTrue(self.kb.search_knowledge("预算总表"))
            self.assertTrue(self.kb.search_knowledge("路线图"))
            self.assertTrue(self.kb.search_knowledge("hybrid_rank_score"))

            # Reject empty, oversized, binary, and unknown formats with friendly Chinese messages.
            for bad_name, bad_bytes in (
                ("empty.txt", b""),
                ("blank.md", b"   \n\t  "),
                ("bad.bin", b"\x00\x01\x02\x03"),
                ("null.txt", b"hello\x00world"),
                ("broken.docx", b"not-a-zip"),
                ("img.png", b"\x89PNG\r\n\x1a\n"),
            ):
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(ValueError):
                        self.kb.ingest_bytes(bad_name, bad_bytes)

            with mock.patch.object(self.kb, "MAX_DOCUMENT_BYTES", 32):
                with self.assertRaises(ValueError) as ctx:
                    self.kb.ingest_bytes("big.txt", b"x" * 64)
                self.assertIn("超过", str(ctx.exception))

    def test_hybrid_search_short_cjk_long_sentence_highlights_and_unicode61_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            self.kb.ingest_bytes(
                "travel.md",
                "# 秋日旅行\n\n我下周准备去京都看红叶，顺便喝手冲咖啡，再去岚山散步。".encode("utf-8"),
                title="京都赏枫指南",
                collection="生活",
            )
            self.kb.ingest_bytes(
                "code.go",
                "package main\n// Go concurrency notes with ToolRegistry and async_download_to_file\nfunc main() {}\n".encode("utf-8"),
                title="Go 并发笔记",
                collection="技术",
            )
            self.kb.ingest_bytes(
                "exact.md",
                "# 精确命中\n\n京都红叶和手冲咖啡路线规划全文。".encode("utf-8"),
                title="精确长句测试",
                collection="生活",
            )

            # 1-2 char CJK and short ASCII queries
            hit_2cjk = self.kb.search_knowledge("红叶")
            self.assertGreaterEqual(len(hit_2cjk), 1)
            hit_1cjk = self.kb.search_knowledge("猫")
            self.assertEqual(hit_1cjk, [])
            hit_go = self.kb.search_knowledge("Go")
            self.assertEqual(hit_go[0]["doc_id"], self.kb.list_knowledge(collection="技术")["documents"][0]["id"])

            # >= 3 chars identifier (FTS5 trigram) + mixed FTS/LIKE query
            hit_ident = self.kb.search_knowledge("async_download_to_file")
            self.assertEqual(len(hit_ident), 1)
            self.assertEqual(hit_ident[0]["collection"], "技术")

            self.kb.ingest_bytes(
                "combo.md",
                "使用 ToolRegistry 在京都赏红叶时查资料。".encode("utf-8"),
                title="混合词项排序测试",
                collection="综合",
            )
            mixed_hits = self.kb.search_knowledge("ToolRegistry 红叶")
            self.assertEqual(mixed_hits[0]["title"], "混合词项排序测试")

            # Isolated _loader package compatibility
            _, (loader_kb,) = load("knowledge")
            with tempfile.TemporaryDirectory() as tmp_iso, muse_home_override(tmp_iso):
                iso_doc = loader_kb.ingest_bytes("iso.md", "# 隔离测试\n\n独立沙箱目录".encode("utf-8"), title="隔离测试")
                self.assertEqual(iso_doc["title"], "隔离测试")
                self.assertEqual(len(loader_kb.search_knowledge("沙箱")), 1)

            # Long mixed natural Chinese sentence should recall both exact and semantically matching chunks, with exact ranked #1
            hit_long = self.kb.search_knowledge("京都红叶和手冲咖啡")
            self.assertGreaterEqual(len(hit_long), 2)
            self.assertEqual(hit_long[0]["title"], "精确长句测试")
            self.assertIn("京都赏枫指南", [h["title"] for h in hit_long])

            # Matches highlight offset invariants
            for h in hit_long:
                self.assertGreater(len(h["matches"]), 0)
                for start, end in h["matches"]:
                    self.assertGreaterEqual(start, 0)
                    self.assertLess(start, end)
                    self.assertLessEqual(end, len(h["snippet"]))

            # Collection filter works on both search_knowledge and list_knowledge
            filtered = self.kb.list_knowledge(collection="技术", query="红叶")
            self.assertEqual(filtered["hits"], [])
            self.assertEqual(filtered["stats"]["doc_count"], 1)
            self.assertEqual(set(filtered["collections"]), {"生活", "技术", "综合"})

        # Verify unicode61 fallback when trigram is unavailable
        with tempfile.TemporaryDirectory() as tmp2, muse_home_override(tmp2):
            real_ensure = self.kb._ensure_fts_table

            def _force_unicode61(conn: sqlite3.Connection) -> str:
                existing = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'kb_chunks_fts'"
                ).fetchone()
                if existing is not None:
                    return real_ensure(conn)
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts "
                    "USING fts5(title, heading, content, tokenize='unicode61')"
                )
                return "unicode61"

            with mock.patch.object(self.kb, "_ensure_fts_table", side_effect=_force_unicode61):
                self.kb.ingest_bytes("u.md", "# 降级测试\n\n手冲咖啡与 ToolRegistry 测试".encode("utf-8"))
                self.assertEqual(len(self.kb.search_knowledge("手冲咖啡")), 1)
                self.assertEqual(len(self.kb.search_knowledge("ToolRegistry")), 1)

    def test_deduplication_update_title_fts_sync_and_cascade_delete(self):
        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            payload = "# 初版文档\n\n核心内容段落。".encode("utf-8")
            d1 = self.kb.ingest_bytes("doc.md", payload, title="原始标题", collection="默认")
            self.assertFalse(d1["deduplicated"])

            # Re-uploading identical content in same collection is idempotent and updates title + FTS
            d2 = self.kb.ingest_bytes("doc-copy.md", payload, title="更新后标题", collection="默认")
            self.assertTrue(d2["deduplicated"])
            self.assertEqual(d1["id"], d2["id"])
            self.assertEqual(d2["title"], "更新后标题")
            self.assertEqual(self.kb.list_knowledge()["stats"]["doc_count"], 1)

            # Same content in a different collection creates a distinct document
            d3 = self.kb.ingest_bytes("doc.md", payload, title="工作副本", collection="工作")
            self.assertFalse(d3["deduplicated"])
            self.assertNotEqual(d1["id"], d3["id"])

            # update_document syncs kb_chunks_fts title
            updated = self.kb.update_document(d1["id"], title="最终架构名称", collection="归档")
            self.assertEqual(updated["title"], "最终架构名称")
            self.assertEqual(updated["collection"], "归档")
            self.assertEqual(len(self.kb.search_knowledge("最终架构名称")), 1)
            self.assertEqual(self.kb.search_knowledge("更新后标题", collection="归档"), [])

            # delete_document cascades across kb_chunks and kb_chunks_fts
            self.kb.delete_document(d1["id"])
            self.assertEqual(self.kb.search_knowledge("最终架构名称"), [])
            conn = sqlite3.connect(str(Path(tmp) / "knowledge.db"))
            try:
                chunks_left = conn.execute("SELECT COUNT(*) FROM kb_chunks WHERE doc_id = ?", (d1["id"],)).fetchone()[0]
                fts_left = conn.execute(
                    "SELECT COUNT(*) FROM kb_chunks_fts WHERE kb_chunks_fts MATCH '\"最终架构名称\"'"
                ).fetchone()[0]
                self.assertEqual(chunks_left, 0)
                self.assertEqual(fts_left, 0)
            finally:
                conn.close()

    def test_import_from_creation_secrets_guard_and_url_scrubbing(self):
        from companion.modules import creations

        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            # Secret paths rejected; .env.example allowed
            with self.assertRaises(ValueError):
                self.kb.ingest_bytes(".env", b"OPENAI_API_KEY=sk-123456")
            with self.assertRaises(ValueError):
                self.kb.ingest_bytes("auth.json", b'{"token":"secret"}')
            env_file = Path(tmp) / ".env"
            env_file.write_text("SECRET=1", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.kb.ingest_file(env_file)

            example_ok = self.kb.ingest_bytes("deploy/.env.example", b"PORT=8766\n")
            self.assertEqual(example_ok["filename"], ".env.example")

            # URL query secrets scrubbed before storage & indexing
            self.kb.ingest_bytes(
                "links.md",
                "调用地址 https://api.example.com/v1/run?api_key=TopSecretKey999&mode=fast 完毕。".encode("utf-8"),
                title="接口说明",
            )
            hits = self.kb.search_knowledge("调用地址")
            self.assertEqual(len(hits), 1)
            self.assertNotIn("TopSecretKey999", hits[0]["content"])
            self.assertIn("[REDACTED]", hits[0]["content"])

            # Import from 创意 creations_root()
            src_doc = Path(tmp) / "report.md"
            src_doc.write_text("# 季度总结\n\n营收同比增长 35%。", encoding="utf-8")
            creation_id = creations.import_file(src_doc, key="rep-1", title="季度总结", origin="chat")
            self.assertIsNotNone(creation_id)
            imported = self.kb.import_from_creation(creation_id, collection="作品集")
            self.assertEqual(imported["origin"], "creation")
            self.assertEqual(imported["title"], "季度总结")
            self.assertEqual(imported["collection"], "作品集")
            self.assertEqual(len(self.kb.search_knowledge("同比增长", collection="作品集")), 1)

            # Traversal and missing creation rejected
            with self.assertRaises(ValueError):
                self.kb.import_from_creation("../muse.db")
            with self.assertRaises(ValueError):
                self.kb.import_from_creation("文档/not-exist.md")

    def test_native_tools_registration_output_and_citation_hook(self):
        self.assertIn("knowledge", CORE_TOOLSETS)
        reg = create_default_tool_registry()
        self.assertIn("knowledge", reg.toolsets())
        tool_names = {d["function"]["name"] for d in reg.get_definitions()}
        self.assertIn("knowledge_search", tool_names)
        self.assertIn("knowledge_list", tool_names)

        with tempfile.TemporaryDirectory() as tmp, muse_home_override(tmp):
            doc = self.kb.ingest_bytes(
                "handbook.md",
                "# 入职手册\n\n## 报销规范\n\n差旅发票需要在每月 25 号前提交审批。".encode("utf-8"),
                title="员工手册",
                collection="行政",
            )

            # Dispatch knowledge_list and knowledge_search via ToolRegistry
            listed = json.loads(asyncio.run(reg.dispatch("knowledge_list", {})))
            self.assertTrue(listed["success"])
            self.assertEqual(listed["stats"]["doc_count"], 1)

            # Simulate muse.tools.citations presence (Phase 2 integration check)
            recorded: list[list[dict[str, str]]] = []
            fake_citations = types.ModuleType("muse.tools.citations")
            fake_citations.record_citations = lambda items: recorded.append(list(items))
            with mock.patch.dict(sys.modules, {"muse.tools.citations": fake_citations}):
                raw_hit = asyncio.run(reg.dispatch("knowledge_search", {"query": "差旅发票", "limit": 3}))
            parsed = json.loads(raw_hit)
            self.assertTrue(parsed["success"])
            self.assertEqual(parsed["count"], 1)
            self.assertIn("[K1] 《员工手册》（行政 / 入职手册 / 报销规范）", parsed["formatted"])
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0][0]["url"], f"#knowledge:{doc['id']}")
            self.assertEqual(recorded[0][0]["domain"], "知识库 · 行政")
            self.assertEqual(recorded[0][0]["title"], "员工手册 · 入职手册 / 报销规范")

            # Empty query returns structured error; no-hit query succeeds cleanly
            err = json.loads(knowledge_search_handler({"query": ""}))
            self.assertFalse(err["success"])
            empty_list = json.loads(knowledge_list_handler({"collection": "不存在"}))
            self.assertTrue(empty_list["success"])
            self.assertEqual(empty_list["stats"]["doc_count"], 0)


if __name__ == "__main__":
    unittest.main()
