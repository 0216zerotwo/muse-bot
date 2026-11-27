import Foundation
import XCTest
@testable import Companion

final class Wave3FeatureTests: XCTestCase {
    private func fixtureData(_ name: String) throws -> Data {
        let bundle = Bundle(for: Wave3FeatureTests.self)
        let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
            ?? bundle.url(forResource: name, withExtension: "json")
        return try Data(contentsOf: XCTUnwrap(url, "Missing fixture \(name)"))
    }

    private func fixture(_ name: String) throws -> JSONValue {
        try JSONValue.parse(fixtureData(name))
    }

    func testChatSummaryCodableAndLegacyFixtureCompatibility() throws {
        // Wave 3 full payload with pinned / archived / empty / model_preset / reasoning_effort
        let wave3JSON = Data("""
        {
          "key": "websocket:side-alpha",
          "title": "架构评审",
          "preview": "对比 Swift Charts 与原生布局",
          "created_at": "2026-11-26T10:00:00Z",
          "updated_at": "2026-11-27T02:00:00Z",
          "pinned": true,
          "archived": false,
          "empty": false,
          "model_preset": "DeepSeek V4.1 Pro",
          "reasoning_effort": "high"
        }
        """.utf8)
        let decoded = try JSONDecoder().decode(ChatSummary.self, from: wave3JSON)
        XCTAssertEqual(decoded.id, "side-alpha")
        XCTAssertEqual(decoded.pinned, true)
        XCTAssertEqual(decoded.archived, false)
        XCTAssertEqual(decoded.empty, false)
        XCTAssertEqual(decoded.modelPreset, "DeepSeek V4.1 Pro")
        XCTAssertEqual(decoded.reasoningEffort, "high")
        XCTAssertTrue(decoded.isPinned)
        XCTAssertFalse(decoded.isArchived)
        XCTAssertFalse(decoded.isEmptyChat)

        let fromValue = ChatSummary(json: try JSONValue.parse(wave3JSON))
        XCTAssertEqual(fromValue, decoded)

        let roundTripped = try JSONDecoder().decode(ChatSummary.self, from: JSONEncoder().encode(decoded))
        XCTAssertEqual(roundTripped, decoded)

        // Legacy core-sessions.json fixture (no Wave 3 fields) must decode cleanly.
        struct SessionsEnvelope: Codable { let sessions: [ChatSummary] }
        let legacyEnvelope = try JSONDecoder().decode(SessionsEnvelope.self, from: fixtureData("core-sessions"))
        XCTAssertFalse(legacyEnvelope.sessions.isEmpty)
        XCTAssertTrue(legacyEnvelope.sessions.allSatisfy { $0.pinned == nil && $0.archived == nil && $0.modelPreset == nil && $0.reasoningEffort == nil })
        XCTAssertTrue(legacyEnvelope.sessions.allSatisfy { !$0.isPinned && !$0.isArchived })
    }

    @MainActor
    func testStoreApplySessionsPayloadPopulatesMainAndSideChats() throws {
        let store = CompanionStore()
        store.companionState = .object(["chat_id": .string("main-01")])
        let payload = try JSONValue.parse(Data("""
        {
          "sessions": [
            {
              "key": "websocket:main-01",
              "title": "主聊",
              "preview": "你好",
              "model_preset": "DeepSeek V4.1 Flash",
              "reasoning_effort": "medium"
            },
            {
              "key": "websocket:side-pin",
              "title": "置顶话题",
              "preview": "重要内容",
              "pinned": true,
              "archived": false,
              "empty": false,
              "model_preset": "GPT-6.1 Sol"
            },
            {
              "key": "websocket:side-arch",
              "title": "归档旧聊",
              "preview": "",
              "pinned": false,
              "archived": true,
              "empty": true
            },
            {
              "key": "websocket:helper-1",
              "title": "帮手",
              "helper": "helper-1"
            }
          ]
        }
        """.utf8))

        store.applySessionsPayload(payload)
        XCTAssertEqual(store.sessionSummary(for: "main-01")?.modelPreset, "DeepSeek V4.1 Flash")
        XCTAssertEqual(store.sessionSummary(for: "main-01")?.reasoningEffort, "medium")
        XCTAssertEqual(store.sideChats.count, 2)
        XCTAssertEqual(store.sideChats[0].id, "side-pin")
        XCTAssertTrue(store.sideChats[0].isPinned)
        XCTAssertFalse(store.sideChats[0].isArchived)
        XCTAssertEqual(store.sideChats[0].modelPreset, "GPT-6.1 Sol")
        XCTAssertEqual(store.sideChats[1].id, "side-arch")
        XCTAssertTrue(store.sideChats[1].isArchived)
        XCTAssertTrue(store.sideChats[1].isEmptyChat)
    }

    func testChatContextStatusDecodingAndWatermarks() throws {
        let rawData = Data("""
        {
          "chat_id": "main-01",
          "estimated_tokens": 96000,
          "context_window": 128000,
          "usage_ratio": 0.75,
          "tier1_threshold": 64000,
          "tier2_threshold": 89600,
          "has_summary_checkpoint": true,
          "model": "deepseek-pro",
          "model_preset": "DeepSeek V4.1 Pro",
          "reasoning_effort": "high"
        }
        """.utf8)
        let decoded = try JSONDecoder().decode(ChatContextStatus.self, from: rawData)
        XCTAssertEqual(decoded.chatId, "main-01")
        XCTAssertEqual(decoded.estimatedTokens, 96_000)
        XCTAssertEqual(decoded.contextWindow, 128_000)
        XCTAssertEqual(decoded.percent, 75)
        XCTAssertEqual(decoded.tier1Threshold, 64_000)
        XCTAssertEqual(decoded.tier2Threshold, 89_600)
        XCTAssertEqual(decoded.hasSummaryCheckpoint, true)
        XCTAssertEqual(decoded.model, "deepseek-pro")
        XCTAssertEqual(decoded.modelPreset, "DeepSeek V4.1 Pro")
        XCTAssertEqual(decoded.reasoningEffort, "high")
        XCTAssertTrue(decoded.isElevated)
        XCTAssertTrue(decoded.isHighWater)

        let fromValue = ChatContextStatus(json: try JSONValue.parse(rawData))
        XCTAssertEqual(fromValue, decoded)

        // Empty fallback payload (e.g. older server or mock) must degrade gracefully.
        let emptyStatus = ChatContextStatus(json: .object([:]))
        XCTAssertEqual(emptyStatus.estimatedTokens, 0)
        XCTAssertEqual(emptyStatus.contextWindow, 0)
        XCTAssertEqual(emptyStatus.percent, 0)
        XCTAssertFalse(emptyStatus.isHighWater)
        XCTAssertFalse(emptyStatus.isElevated)
    }

    func testUsageDailyBucketsAndCacheSavingsDecoding() throws {
        let payloadData = Data("""
        {
          "today": {"by_model": [], "by_source": {}, "cached_tokens": 0, "input_tokens": 0, "cost": null, "media": []},
          "week": {"by_model": [], "by_source": {}, "cached_tokens": 0, "input_tokens": 0, "cost": null, "media": []},
          "cache_hit_rate": 0.875,
          "cache_saved_cost": 4.32,
          "daily": [
            {
              "date": "2026-11-26",
              "input_tokens": 120000,
              "cached_tokens": 100000,
              "output_tokens": 15000,
              "cost": 1.25,
              "media_cost": 0.2,
              "requests": 18
            },
            {
              "date": "2026-11-27",
              "input_tokens": 5000,
              "cached_tokens": 6000,
              "output_tokens": 1200,
              "cost": null,
              "media_cost": 0.0,
              "requests": 2
            }
          ]
        }
        """.utf8)
        let decoded = try JSONDecoder().decode(UsageSummaryPayload.self, from: payloadData)
        XCTAssertEqual(decoded.cacheHitRate, 0.875)
        XCTAssertEqual(decoded.cacheSavedCost, 4.32)
        let days = try XCTUnwrap(decoded.daily)
        XCTAssertEqual(days.count, 2)
        XCTAssertEqual(days[0].freshInputTokens, 20_000)
        XCTAssertEqual(days[0].cachedTokens, 100_000)
        XCTAssertEqual(days[0].outputTokens, 15_000)
        XCTAssertEqual(days[0].totalTokens, 135_000)
        XCTAssertEqual(days[0].shortDateLabel, "11/26")
        // Server `daily[].cost` already includes `media_cost`; do not double-count.
        XCTAssertEqual(try XCTUnwrap(days[0].combinedCost), 1.25, accuracy: 0.0001)
        // Fresh input clamps safely to 0 even if cached exceeds input.
        XCTAssertEqual(days[1].freshInputTokens, 0)
        XCTAssertNil(days[1].combinedCost)

        let fromValue = UsageSummaryPayload(json: try JSONValue.parse(payloadData))
        XCTAssertEqual(fromValue, decoded)

        // Legacy settings-usage.json without Wave 3 fields must decode with nil optionals.
        let legacyDecoded = try JSONDecoder().decode(UsageSummaryPayload.self, from: fixtureData("settings-usage"))
        XCTAssertNil(legacyDecoded.daily)
        XCTAssertNil(legacyDecoded.cacheHitRate)
        XCTAssertNil(legacyDecoded.cacheSavedCost)
        let legacyValue = UsageSummaryPayload(json: try fixture("settings-usage"))
        XCTAssertNil(legacyValue.daily)
    }

    func testKnowledgeDocumentDetailChunkDecodingAndCreationOrigin() throws {
        let raw = try JSONValue.parse(Data("""
        {
          "document": {
            "id": "kb_99",
            "title": "架构设计",
            "filename": "arch.md",
            "collection": "工程",
            "chunk_count": 2,
            "size_bytes": 2048,
            "origin": "creation:文档/arch.md"
          },
          "chunks": [
            {"chunk_index": 0, "heading": "概述", "content": "系统采用本地优先架构。"},
            {"chunk_index": 1, "heading": "数据流", "content": "通过 WebSocket 增量同步状态。"}
          ]
        }
        """.utf8))
        let detail = KnowledgeDocumentDetail(json: raw)
        XCTAssertEqual(detail.document.id, "kb_99")
        XCTAssertEqual(detail.document.originLabel, "作品导入")
        XCTAssertEqual(detail.chunks.count, 2)
        XCTAssertEqual(detail.chunks[0].heading, "概述")
        XCTAssertEqual(detail.chunks[1].chunkIndex, 1)
    }

    func testMarkdownArtifactPreviewLanguageAndExtensions() {
        XCTAssertEqual(ArtifactPreviewItem.previewableLanguage("markdown", content: "# Hello"), "md")
        XCTAssertEqual(ArtifactPreviewItem.previewableLanguage("MD", content: "## Title"), "md")
        XCTAssertEqual(ArtifactPreviewItem.previewableLanguage("mdx", content: "Body"), "md")

        let mdItem = ArtifactPreviewItem(title: "Markdown 预览", language: "md", code: "# 标题\n正文")
        XCTAssertTrue(mdItem.isMarkdown)
        XCTAssertFalse(mdItem.isSVG)
        XCTAssertEqual(mdItem.fileExtension, "md")
        XCTAssertEqual(mdItem.creationFormat, "md")

        let htmlItem = ArtifactPreviewItem(title: "HTML 预览", language: "html", code: "<div>ok</div>")
        XCTAssertFalse(htmlItem.isMarkdown)
        XCTAssertEqual(htmlItem.creationFormat, "html")
    }

    func testPerChatDraftAndQuotePersistenceIsolation() throws {
        let suiteName = "com.personalbot.companion.tests.draft.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        ChatDraftPersistence.saveText("草稿 A", server: "http://127.0.0.1:8766", chatID: "chat-a", defaults: defaults)
        ChatDraftPersistence.saveQuote("引用片段 A", server: "http://127.0.0.1:8766", chatID: "chat-a", defaults: defaults)
        ChatDraftPersistence.saveText("草稿 B", server: "http://127.0.0.1:8766", chatID: "chat-b", defaults: defaults)

        let loadedA = ChatDraftPersistence.load(server: "http://127.0.0.1:8766", chatID: "chat-a", defaults: defaults)
        XCTAssertEqual(loadedA.text, "草稿 A")
        XCTAssertEqual(loadedA.quote, "引用片段 A")

        let loadedB = ChatDraftPersistence.load(server: "http://127.0.0.1:8766", chatID: "chat-b", defaults: defaults)
        XCTAssertEqual(loadedB.text, "草稿 B")
        XCTAssertNil(loadedB.quote)

        ChatDraftPersistence.clear(server: "http://127.0.0.1:8766", chatID: "chat-a", defaults: defaults)
        let clearedA = ChatDraftPersistence.load(server: "http://127.0.0.1:8766", chatID: "chat-a", defaults: defaults)
        XCTAssertEqual(clearedA.text, "")
        XCTAssertNil(clearedA.quote)
    }

    func testUnreadReplyTrackerCountsOnlyNewCompletedRepliesWhenScrolledUp() {
        var tracker = ChatUnreadTracker()
        let base = [
            ChatMessage(id: "m1", role: "user", content: "你好", createdAt: 1_000),
            ChatMessage(id: "m2", role: "assistant", content: "我在", createdAt: 2_000),
        ]
        tracker.reset(for: base)
        XCTAssertEqual(tracker.unreadCount, 0)

        // Streaming reply arrives while scrolled up -> not counted until completed.
        var streaming = ChatMessage(id: "m3", role: "assistant", content: "正在生成…", createdAt: 3_000)
        streaming.isStreaming = true
        tracker.ingest(messages: base + [streaming], followingBottom: false, positioned: true, paging: false)
        XCTAssertEqual(tracker.unreadCount, 0)

        // Reply finishes streaming while user is still scrolled up -> +1.
        streaming.isStreaming = false
        streaming.content = "生成完毕"
        tracker.ingest(messages: base + [streaming], followingBottom: false, positioned: true, paging: false)
        XCTAssertEqual(tracker.unreadCount, 1)

        // Paging older history must NOT inflate unread count.
        let older = ChatMessage(id: "m0", role: "assistant", content: "更早的回复", createdAt: 500)
        tracker.ingest(messages: [older] + base + [streaming], followingBottom: false, positioned: true, paging: true)
        XCTAssertEqual(tracker.unreadCount, 1)

        // Another completed reply arrives -> +2.
        let nextReply = ChatMessage(id: "m4", role: "assistant", content: "补充一条", createdAt: 4_000)
        tracker.ingest(messages: [older] + base + [streaming, nextReply], followingBottom: false, positioned: true, paging: false)
        XCTAssertEqual(tracker.unreadCount, 2)

        // Scrolling back to bottom clears unread count.
        tracker.ingest(messages: [older] + base + [streaming, nextReply], followingBottom: true, positioned: true, paging: false)
        XCTAssertEqual(tracker.unreadCount, 0)
    }
}
