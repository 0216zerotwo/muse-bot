import XCTest
@testable import Companion

final class UXParityTests: XCTestCase {
    func testNormalizedServerPreservesSubpathAndDefaultsSchemes() throws {
        XCTAssertEqual(try APIClient.normalizedServer("bot.example.com").absoluteString, "https://bot.example.com")
        XCTAssertEqual(
            try APIClient.normalizedServer("bot.example.com/muse/companion.html#pair=ABCD-2345").absoluteString,
            "https://bot.example.com/muse"
        )
        XCTAssertEqual(
            try APIClient.normalizedServer("HTTPS://Bot.Example.com/sub/path/").absoluteString,
            "https://bot.example.com/sub/path"
        )
        XCTAssertEqual(try APIClient.normalizedServer("192.168.1.20:8766").absoluteString, "http://192.168.1.20:8766")
        XCTAssertEqual(try APIClient.normalizedServer("100.88.12.34:8766").absoluteString, "http://100.88.12.34:8766")
        XCTAssertEqual(try APIClient.normalizedServer("localhost:8766").absoluteString, "http://localhost:8766")
        XCTAssertEqual(try APIClient.normalizedServer("[::1]:8766").absoluteString, "http://[::1]:8766")
        XCTAssertEqual(try APIClient.normalizedServer("macmini.local:8766").absoluteString, "http://macmini.local:8766")
        XCTAssertEqual(
            try APIClient.normalizedServer("在手机打开 https://bot.example.com/bot/companion.html#pair=WXYZ-6789。").absoluteString,
            "https://bot.example.com/bot"
        )

        for invalid in [
            "",
            "   ",
            "ftp://example.com",
            "javascript:alert(1)",
            "https://user:pass@example.com",
            "https://example.com/a/../b",
            "not a valid server",
            "plainword",
        ] {
            XCTAssertThrowsError(try APIClient.normalizedServer(invalid), "Expected failure for \(invalid)")
        }
    }

    @MainActor
    func testSubpathAwareURLResolutionAndWebSocketPaths() throws {
        let base = try APIClient.normalizedServer("https://bot.example.com/muse/")
        let client = APIClient(baseURL: base)

        XCTAssertEqual(try client.url("/webui/bootstrap").absoluteString, "https://bot.example.com/muse/webui/bootstrap")
        XCTAssertEqual(
            try client.url("/api/sessions", query: [URLQueryItem(name: "limit", value: "20")]).absoluteString,
            "https://bot.example.com/muse/api/sessions?limit=20"
        )
        XCTAssertEqual(try client.url("/muse/api/attachments").absoluteString, "https://bot.example.com/muse/api/attachments")

        XCTAssertThrowsError(try client.url("https://evil.example/api"))
        XCTAssertThrowsError(try client.url("//evil.example/api"))
        XCTAssertThrowsError(try client.url("/../admin"))

        let wsURL = try client.socketURL(bootstrap: .object([
            "ws_path": .string("/ws"),
            "token": .string("tok-1"),
        ]))
        XCTAssertEqual(wsURL.absoluteString, "wss://bot.example.com/muse/ws?token=tok-1")

        XCTAssertEqual(
            resolvedURL("/api/media/abc/def", baseURL: base)?.absoluteString,
            "https://bot.example.com/muse/api/media/abc/def"
        )
        XCTAssertEqual(
            resolvedURL("https://cdn.example.com/pic.png", baseURL: base)?.absoluteString,
            "https://cdn.example.com/pic.png"
        )
        XCTAssertNil(resolvedURL("//evil.example/pic.png", baseURL: base))
    }

    func testPairingCodeAndMusebotDeepLinkParsing() throws {
        XCTAssertEqual(CompanionStore.pairingCode(in: "http://127.0.0.1:8799/#pair=ABCD-2345"), "ABCD-2345")
        XCTAssertEqual(CompanionStore.pairingCode(in: "https://bot.example.com/companion.html#foo=1&pair=WXYZ-6789"), "WXYZ-6789")
        XCTAssertEqual(CompanionStore.pairingCode(in: "https://bot.example.com/?pair=CODE-1111"), "CODE-1111")
        XCTAssertEqual(CompanionStore.pairingCode(in: "链接：https://bot.example.com/companion.html#pair=ABCD-2345。"), "ABCD-2345")
        XCTAssertNil(CompanionStore.pairingCode(in: "https://bot.example.com/#pair="))

        let deepQuery = APIClient.parseConnectInput("musebot://pair?server=https%3A%2F%2Fbot.example.com%2Fmuse&code=PAIR-9999")
        XCTAssertEqual(deepQuery.server, "https://bot.example.com/muse")
        XCTAssertEqual(deepQuery.pairingCode, "PAIR-9999")

        let deepHost = APIClient.parseConnectInput("musebot://bot.example.com/muse/companion.html#pair=PAIR-8888")
        XCTAssertEqual(try APIClient.normalizedServer("musebot://bot.example.com/muse/companion.html#pair=PAIR-8888").absoluteString, "https://bot.example.com/muse")
        XCTAssertEqual(deepHost.pairingCode, "PAIR-8888")

        let codeOnly = APIClient.parseConnectInput("musebot://pair?code=ONLY-CODE")
        XCTAssertNil(codeOnly.server)
        XCTAssertEqual(codeOnly.pairingCode, "ONLY-CODE")
    }

    func testVaultValidationMatchesBackendRules() {
        XCTAssertTrue(VaultValidation.isAllowedEnvName("GITHUB_TOKEN"))
        XCTAssertTrue(VaultValidation.isAllowedEnvName("OPENAI_API_KEY"))
        XCTAssertTrue(VaultValidation.isAllowedEnvName("AMAP_KEY_2"))
        XCTAssertNil(VaultValidation.envNameError(""))
        XCTAssertNil(VaultValidation.envNameError("GITHUB_TOKEN"))

        for rejected in [
            "PATH", "PYTHONPATH", "HOME", "HTTPS_PROXY", "NODE_OPTIONS",
            "HERMES_SECRET", "COMPANION_TOKEN", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
            "SSH_AUTH_SOCK", "PYTHON_KEY", "ab", "1ABC", "github_token",
        ] {
            XCTAssertFalse(VaultValidation.isAllowedEnvName(rejected), "Expected \(rejected) to be rejected")
            XCTAssertNotNil(VaultValidation.envNameError(rejected))
        }

        XCTAssertTrue(VaultValidation.isValidOrigin("github.com"))
        XCTAssertTrue(VaultValidation.isValidOrigin("https://login.example.com/auth"))
        XCTAssertTrue(VaultValidation.isValidOrigin("http://localhost:3000"))
        XCTAssertFalse(VaultValidation.isValidOrigin(""))
        XCTAssertFalse(VaultValidation.isValidOrigin("ftp://example.com"))
        XCTAssertFalse(VaultValidation.isValidOrigin("https://user:pass@example.com"))
        XCTAssertFalse(VaultValidation.isValidOrigin("plainword"))
    }

    func testComposerSlashAndHelperMentionAutocomplete() {
        XCTAssertEqual(ComposerAutocomplete.slashQuery(in: "/"), "")
        XCTAssertEqual(ComposerAutocomplete.slashQuery(in: "/go"), "go")
        XCTAssertNil(ComposerAutocomplete.slashQuery(in: "/goal 整理周报"))
        XCTAssertNil(ComposerAutocomplete.slashQuery(in: "你好 /goal"))

        let learned: [JSONValue] = [
            .object(["name": .string("weekly-report"), "description": .string("整理本周工作周报")]),
        ]
        let allSlash = ComposerAutocomplete.matchingSlashItems(query: "", learnedSkills: learned)
        XCTAssertTrue(allSlash.contains { $0.key == "goal" && $0.text == "/goal " })
        XCTAssertTrue(allSlash.contains { $0.key == "weekly-report" && $0.text == "用「weekly-report」技能：" })
        XCTAssertEqual(ComposerAutocomplete.matchingSlashItems(query: "周报", learnedSkills: learned).map(\.key), ["weekly-report"])

        XCTAssertEqual(ComposerAutocomplete.mentionQuery(in: "@"), "")
        XCTAssertEqual(ComposerAutocomplete.mentionQuery(in: "交给 @记"), "记")
        XCTAssertNil(ComposerAutocomplete.mentionQuery(in: "test@example.com"))
        XCTAssertEqual(ComposerAutocomplete.replacingMention(in: "把这笔交给 @记", with: "记账"), "把这笔交给 @记账 ")

        let helpers: [JSONValue] = [
            .object(["id": .string("h1"), "name": .string("记账"), "title": .string("记下每笔收支")]),
            .object(["id": .string("h2"), "name": .string("翻译"), "title": .string("中英互译")]),
        ]
        XCTAssertEqual(ComposerAutocomplete.matchingHelpers(query: "收支", helpers: helpers).count, 1)
        XCTAssertEqual(ComposerAutocomplete.matchingHelpers(query: "", helpers: helpers).count, 2)
    }

    func testHelperShareSchedulesAndUsageClamping() throws {
        let helper: JSONValue = .object([
            "name": .string("记账管家"),
            "emoji": .string("💰"),
            "title": .string("按月汇总收支"),
            "instructions": .string("每笔记下金额与类别"),
        ])
        let shared = HelperShare.text(helper)
        let parsed = try XCTUnwrap(HelperShare.parse("这是分享给你的设定：\n" + shared))
        XCTAssertEqual(parsed["name"]?.stringValue, "记账管家")
        XCTAssertEqual(parsed["emoji"]?.stringValue, "💰")

        XCTAssertEqual(Schedules.cron("30 7 * * 1-5"), "工作日 07:30")
        XCTAssertEqual(
            Schedules.automationRunPath(jobID: "job/1?a=b", runID: "run#2 c"),
            "/api/webui/automations/job%2F1%3Fa%3Db/runs/run%232%20c"
        )

        XCTAssertEqual(UsageSettingsPage.clampedProgress(value: 150, total: 100), 1.0)
        XCTAssertEqual(UsageSettingsPage.clampedProgress(value: -20, total: 100), 0.0)
        XCTAssertEqual(UsageSettingsPage.clampedProgress(value: .nan, total: 100), 0.0)
        XCTAssertEqual(UsageSettingsPage.clampedPercent(part: 120, whole: 100), 100)
        XCTAssertEqual(UsageSettingsPage.clampedPercent(part: -5, whole: 100), 0)
    }

    @MainActor
    func testIsolatedHelperChatAndCapabilitiesState() async {
        let store = CompanionStore()
        store.currentChatID = "main-chat"
        XCTAssertTrue(store.sttAvailable)

        store.updateCapabilities(from: .object(["capabilities": .object(["stt": .bool(false)])]))
        XCTAssertFalse(store.sttAvailable)
        store.updateCapabilities(from: .object([:]))
        XCTAssertFalse(store.sttAvailable)
        store.updateCapabilities(from: .object(["capabilities": .object(["stt": .bool(true)])]))
        XCTAssertTrue(store.sttAvailable)

        await store.openHelperChat("helper-chat-1")
        XCTAssertEqual(store.currentChatID, "main-chat")
        XCTAssertEqual(store.helperChatID, "helper-chat-1")

        store.closeHelperChat()
        XCTAssertEqual(store.currentChatID, "main-chat")
        XCTAssertEqual(store.helperChatID, "")
    }
}
