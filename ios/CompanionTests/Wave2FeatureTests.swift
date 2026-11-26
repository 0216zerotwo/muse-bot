import Foundation
import XCTest
@testable import Companion

final class Wave2FeatureTests: XCTestCase {
    private func fixture(_ name: String) throws -> JSONValue {
        let bundle = Bundle(for: Wave2FeatureTests.self)
        let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
            ?? bundle.url(forResource: name, withExtension: "json")
        return try JSONValue.parse(Data(contentsOf: XCTUnwrap(url, "Missing fixture \(name)")))
    }

    func testCitationSourcesDecodingAndStreamProjection() throws {
        let thread = try fixture("core-citations-thread")
        let messages = StreamProjection.replay(thread["events"].records)
        let reply = try XCTUnwrap(messages.last(where: { $0.role == "assistant" }))
        let sources = try XCTUnwrap(reply.sources)
        XCTAssertEqual(sources.count, 2)

        XCTAssertEqual(sources[0].id, 1)
        XCTAssertEqual(sources[0].displayDomain, "sqlite.org")
        XCTAssertFalse(sources[0].isKnowledge)
        XCTAssertEqual(sources[0].webURL?.absoluteString, "https://www.sqlite.org/fts5.html")

        XCTAssertEqual(sources[1].id, 2)
        XCTAssertTrue(sources[1].isKnowledge)
        XCTAssertEqual(sources[1].knowledgeDocID, "kb_doc_001")
        XCTAssertEqual(sources[1].displayDomain, "知识库")
        XCTAssertNil(sources[1].webURL)

        let encoded = try JSONEncoder().encode(reply)
        let decoded = try JSONDecoder().decode(ChatMessage.self, from: encoded)
        XCTAssertEqual(decoded.sources, sources)

        // Streaming sources delivered on turn_end should merge cleanly onto the final assistant bubble.
        var live = StreamProjection()
        live.apply(.object(["event": .string("delta"), "text": .string("分析完成"), "turn_id": .string("t-live")]))
        live.apply(.object([
            "event": .string("turn_end"),
            "turn_id": .string("t-live"),
            "sources": .array([
                .object(["id": .number(1), "title": .string("Example"), "url": .string("https://www.example.com/a"), "snippet": .string("摘要")]),
            ]),
        ]))
        XCTAssertEqual(live.messages.last?.sources?.count, 1)
        XCTAssertEqual(live.messages.last?.sources?.first?.displayDomain, "example.com")
    }

    func testMemoryPinnedDecodingAndBackwardCompatibility() throws {
        let pinnedPayload = try fixture("identity-memory-pinned")
        let sections = MemorySection.parseSections(pinnedPayload)
        XCTAssertEqual(sections.count, 2)
        XCTAssertEqual(sections[0].file, "USER.md")
        XCTAssertEqual(sections[0].items.count, 2)
        XCTAssertTrue(sections[0].items[0].pinned)
        XCTAssertFalse(sections[0].items[1].pinned)
        // Missing `pinned` field defaults safely to false.
        XCTAssertFalse(sections[1].items[0].pinned)

        // Existing legacy fixture without `pinned` must remain 100% decodable.
        let legacyPayload = try fixture("identity-memory")
        let legacySections = MemorySection.parseSections(legacyPayload)
        XCTAssertFalse(legacySections.isEmpty)
        XCTAssertTrue(legacySections.flatMap(\.items).allSatisfy { !$0.pinned })
    }

    func testKnowledgeVaultPayloadAndSearchHitsDecoding() throws {
        let raw = try fixture("tabs-knowledge")
        let payload = KnowledgePayload(json: raw)
        XCTAssertEqual(payload.documents.count, 2)
        XCTAssertEqual(payload.collections, ["默认", "工作资料"])
        XCTAssertEqual(payload.collectionCounts["工作资料"], 1)
        XCTAssertEqual(payload.stats.docCount, 2)
        XCTAssertEqual(payload.stats.chunkCount, 27)
        XCTAssertEqual(payload.stats.totalBytes, 440_670)
        XCTAssertEqual(payload.documents[0].originLabel, "上传")
        XCTAssertEqual(payload.documents[1].originLabel, "作品导入")

        let hits = try XCTUnwrap(payload.hits)
        XCTAssertEqual(hits.count, 1)
        XCTAssertEqual(hits[0].docId, "kb_doc_001")
        XCTAssertEqual(hits[0].chunkIndex, 3)
        XCTAssertEqual(hits[0].matches, [[3, 14], [34, 38]])

        let emptyPayload = KnowledgePayload(json: .object([:]))
        XCTAssertTrue(emptyPayload.documents.isEmpty)
        XCTAssertNil(emptyPayload.hits)
        XCTAssertEqual(emptyPayload.stats.docCount, 0)
    }

    func testFollowupsResponseDecodingAndDeduplication() throws {
        let raw = try fixture("core-followups")
        let parsed = FollowupsResponse(json: raw)
        XCTAssertEqual(parsed.chatId, "example-chat-01")
        XCTAssertEqual(parsed.turnId, "turn-009")
        XCTAssertEqual(parsed.suggestions.count, 3)

        let noisy = FollowupsResponse(json: .object([
            "chat_id": .string("c1"),
            "turn_id": .string("t1"),
            "suggestions": .array([.string(""), .string("继续展开"), .string("继续展开"), .string("给个例子"), .string("下一步"), .string("第四条截断")]),
        ]))
        XCTAssertEqual(noisy.suggestions, ["继续展开", "给个例子", "下一步"])
    }

    func testSVGAndHTMLFileKindsAndSafeSandboxedRendering() {
        XCTAssertEqual(FileKind.of("diagram.svg"), .svg)
        XCTAssertTrue(FileKind.svg.isPage)
        XCTAssertTrue(FileKinds.isSVG(name: "chart.SVG"))
        XCTAssertTrue(FileKinds.isHTML(name: "demo.html"))
        XCTAssertTrue(FileKinds.isCodeOrText(name: "server.py"))
        XCTAssertTrue(FileKinds.isCodeOrText(name: "config.yaml"))
        XCTAssertTrue(FileKinds.canImportToKnowledge(name: "notes.md"))
        XCTAssertTrue(FileKinds.canImportToKnowledge(name: "report.pdf"))
        XCTAssertTrue(FileKinds.canImportToKnowledge(name: "app.swift"))
        XCTAssertFalse(FileKinds.canImportToKnowledge(name: "archive.zip"))
        XCTAssertFalse(FileKinds.canImportToKnowledge(name: "clip.mp4"))

        // SVG attachments must not fall into raster image decoding.
        let svgAttachment = MediaAttachment(url: "/api/webui/companion/creations/file/sig/diagram.svg", name: "diagram.svg", kind: "image")
        XCTAssertEqual(svgAttachment.kind, "file")

        // SVG wrapper must strip inline scripts and lock down CSP & iframe sandbox.
        let wrappedSVG = FileKinds.svgPage("<svg viewBox='0 0 10 10'><script>alert(1)</script><circle cx='5' cy='5' r='4'/></svg>")
        XCTAssertTrue(wrappedSVG.contains("sandbox=\"\""))
        XCTAssertTrue(wrappedSVG.contains("script-src 'none'"))
        XCTAssertFalse(wrappedSVG.localizedCaseInsensitiveContains("alert(1)"))

        XCTAssertEqual(ArtifactPreviewItem.previewableLanguage("html", content: "<div>hi</div>"), "html")
        XCTAssertEqual(ArtifactPreviewItem.previewableLanguage("SVG", content: "<svg></svg>"), "svg")
        XCTAssertEqual(ArtifactPreviewItem.previewableLanguage("xml", content: "<svg></svg>"), "svg")
        XCTAssertNil(ArtifactPreviewItem.previewableLanguage("python", content: "print(1)"))
    }

    func testSearchHighlightMultiSpanAndFallback() {
        let text = "SQLite FTS5 trigram 支持中文和 FTS5 检索"
        let explicit = SearchHighlight.resolvedSpans(in: text, matches: [[7, 11], [26, 30], [99, 120]], fallbackQuery: "")
        XCTAssertEqual(explicit, [
            SearchHighlight.Span(start: 7, end: 11),
            SearchHighlight.Span(start: 26, end: 30),
        ])

        let fallback = SearchHighlight.resolvedSpans(in: text, matches: nil, fallbackQuery: "fts5")
        XCTAssertEqual(fallback.count, 2)
        XCTAssertEqual(fallback[0], SearchHighlight.Span(start: 7, end: 11))
        XCTAssertEqual(fallback[1], SearchHighlight.Span(start: 26, end: 30))
    }

    @MainActor
    func testCapabilitiesTTSAndEnsureTurnVisibleExpandsWindow() async {
        let store = CompanionStore()
        XCTAssertFalse(store.ttsAvailable)
        store.updateCapabilities(from: .object(["capabilities": .object(["stt": .bool(true), "tts": .bool(true)])]))
        XCTAssertTrue(store.ttsAvailable)
        store.updateCapabilities(from: .object(["capabilities": .object(["tts": .bool(false)])]))
        XCTAssertFalse(store.ttsAvailable)

        // Build 90 messages (exceeding default 60-message window) and jump to turn #5.
        var projection = StreamProjection()
        for idx in 0..<90 {
            var msg = ChatMessage(id: "m-\(idx)", role: idx % 2 == 0 ? "user" : "assistant", content: "第 \(idx) 条消息", createdAt: Double(idx * 1_000))
            msg.turnId = "turn-\(idx)"
            projection.messages.append(msg)
        }
        store.currentChatID = "chat-long"
        store.projections["chat-long"] = projection
        store.publishCurrent()
        XCTAssertEqual(store.messages.count, 60)
        XCTAssertFalse(store.messages.contains { $0.turnId == "turn-5" })

        let found = await store.ensureTurnVisible("turn-5", in: "chat-long")
        XCTAssertEqual(found?.id, "m-5")
        XCTAssertGreaterThanOrEqual(store.messages.count, 85)
        XCTAssertTrue(store.messages.contains { $0.turnId == "turn-5" })
    }
}
