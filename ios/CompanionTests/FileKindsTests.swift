import XCTest
@testable import Companion

/// Files in the chat: what kind each is, the text and rows its page shows, and the document being
/// written while it works.
final class FileKindsTests: XCTestCase {
    func testKindsBadgesAndNames() {
        XCTAssertEqual(FileKind.of("报告.PDF"), .pdf)
        XCTAssertEqual(FileKind.of("阳明山周日徒步计划.md"), .markdown)
        XCTAssertEqual(FileKind.of("预算.xlsx"), .sheet)
        XCTAssertEqual(FileKind.of("鹈狐骑车.html"), .web)
        XCTAssertEqual(FileKind.of("清单.csv"), .table)
        XCTAssertEqual(FileKind.of("包.zip"), .archive)
        XCTAssertEqual(FileKind.of("没有后缀"), .other)
        XCTAssertTrue(FileKind.markdown.isText)
        XCTAssertTrue(FileKind.pdf.isPage)
        XCTAssertFalse(FileKind.archive.isText || FileKind.archive.isPage)
        XCTAssertEqual(FileKinds.badge("预算.xlsx"), "XLSX")
        XCTAssertEqual(FileKinds.badge("没有后缀"), "FILE")
        XCTAssertEqual(FileKinds.title("阳明山周日徒步计划.md"), "阳明山周日徒步计划")
        XCTAssertEqual(FileKinds.subtitle(.pdf, pages: 4, bytes: nil), "PDF · 4 页")
        XCTAssertTrue(FileKinds.subtitle(.markdown, pages: nil, bytes: 2_600).hasPrefix("文档 · "))
        XCTAssertEqual(FileKinds.safeName("../a/b:c.md"), "b_c.md")
        XCTAssertEqual(FileKinds.safeName(".."), "文件")
    }

    func testTextReadsUTF8ChineseWindowsAndAPartialEnd() throws {
        XCTAssertEqual(FileKinds.text(Data("\u{FEFF}第一行\r\n第二行".utf8)), "第一行\n第二行")
        // A file read only in part may end inside a character: what is whole still reads.
        XCTAssertEqual(FileKinds.text(Data("这是一段很长的文字".utf8), limit: 2), "这是")
        let gb18030 = String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(CFStringEncoding(CFStringEncodings.GB_18030_2000.rawValue)))
        let windows = try XCTUnwrap("周日去爬山".data(using: gb18030))
        XCTAssertEqual(FileKinds.text(windows), "周日去爬山")
        XCTAssertEqual(FileKinds.markdownPreview("# 行程\n![山](https://x.cn/a.png)\n早上 7 点出发"), "# 行程\n\n早上 7 点出发")
    }

    func testTableRowsUnderstandQuotes() {
        let rows = FileKinds.rows("名字,价格\r\n\"苹果, 红\",5\n\"他说\"\"好\"\"\",3\n只有一格\n")
        XCTAssertEqual(rows, [["名字", "价格"], ["苹果, 红", "5"], ["他说\"好\"", "3"], ["只有一格", ""]])
        XCTAssertEqual(FileKinds.rows("a\tb\tc", separator: "\t", columns: 2), [["a", "b"]])
        XCTAssertEqual(FileKinds.rows("1\n2\n3\n4", rows: 2), [["1"], ["2"]])
    }

    private func write(_ id: String, _ path: String, _ content: String, phase: String = "end") -> JSONValue {
        .object(["call_id": .string(id), "name": .string("write_file"), "phase": .string(phase),
                 "arguments": .object(["path": .string(path), "content": .string(content)])])
    }

    func testTheDocumentBeingWrittenShowsUntilItIsSent() {
        var ask = ChatMessage(role: "user", content: "做个周末计划", createdAt: 1_000)
        ask.turnId = "t1"
        var script = ChatMessage(role: "assistant", content: "", createdAt: 2_000)
        script.turnId = "t1"
        script.toolEvents = [write("s", "/tmp/fetch.py", "print(1)"), write("h", "/home/me/.hermes/notes.md", "私人笔记")]
        var plan = ChatMessage(role: "assistant", content: "", createdAt: 3_000)
        plan.turnId = "t1"
        plan.toolEvents = [write("p", "E:\\creations\\documents\\周末计划.md", "# 周末\n- 爬山", phase: "start")]
        XCTAssertNil(FileKinds.draft([ask, script]))  // its own scripts and hidden notes are not shown
        let writing = FileKinds.draft([ask, script, plan])
        XCTAssertEqual(writing?.name, "周末计划.md")
        XCTAssertEqual(writing?.content, "# 周末\n- 爬山")
        XCTAssertEqual(writing?.done, false)
        var written = plan
        written.toolEvents = [write("p", "E:\\creations\\documents\\周末计划.md", "# 周末\n- 爬山")]
        XCTAssertEqual(FileKinds.draft([ask, script, plan, written])?.done, true)
        var sent = ChatMessage(role: "assistant", content: "写好了", createdAt: 4_000)
        sent.turnId = "t1"
        sent.media = [MediaAttachment(url: "/api/media/a/b", name: "周末计划.md")]
        XCTAssertNil(FileKinds.draft([ask, script, plan, written, sent]))  // the file's own card shows it now
        var next = ChatMessage(role: "user", content: "谢谢", createdAt: 5_000)
        next.turnId = "t2"
        XCTAssertNil(FileKinds.draft([ask, plan, next]))  // only the turn at work
    }

    func testAWebPageFileIsSandboxedAndOffline() {
        let page = FileKinds.webPage("<!doctype html><html><head><title>骑车</title></head><body><script>draw()</script></body></html>")
        XCTAssertTrue(page.contains("sandbox=\"allow-scripts\""))
        XCTAssertFalse(page.contains("allow-same-origin"))
        XCTAssertFalse(page.contains("<script>"))  // the file is only ever the frame's content
        XCTAssertTrue(page.contains("&lt;head&gt;&lt;meta http-equiv=&quot;Content-Security-Policy&quot;"))
        XCTAssertTrue(page.contains("connect-src 'none'"))
        XCTAssertTrue(FileKinds.webPage("<p>只有片段</p>").contains("srcdoc=\"&lt;meta http-equiv"))

        let scriptTrap = FileKinds.webPage("<script>const x = '<head>'; fetch('https://evil.example');</script><p>ok</p>")
        let cspRange = scriptTrap.range(of: "Content-Security-Policy")
        let scriptRange = scriptTrap.range(of: "&lt;script&gt;")
        XCTAssertNotNil(cspRange)
        XCTAssertNotNil(scriptRange)
        if let cspRange, let scriptRange {
            XCTAssertLessThan(cspRange.lowerBound, scriptRange.lowerBound)
        }

        let commentTrap = FileKinds.webPage("<!-- <head> --><p>ok</p>")
        let commentCSPRange = commentTrap.range(of: "Content-Security-Policy")
        let commentStartRange = commentTrap.range(of: "&lt;!--")
        XCTAssertNotNil(commentCSPRange)
        XCTAssertNotNil(commentStartRange)
        if let commentCSPRange, let commentStartRange {
            XCTAssertLessThan(commentCSPRange.lowerBound, commentStartRange.lowerBound)
        }
    }
}
