import Foundation
import XCTest
import WebKit
@testable import Companion

final class EmbeddedCardTests: XCTestCase {
    /// The network rules must compile in WebKit (a `|` in a url-filter once made every card fail).
    @MainActor
    func testNetworkRulesCompile() async throws {
        let store = try XCTUnwrap(WKContentRuleListStore.default())
        let list: WKContentRuleList? = try await withCheckedThrowingContinuation { continuation in
            store.compileContentRuleList(forIdentifier: "CompanionCardPolicyTest", encodedContentRuleList: EmbeddedCardDocument.networkRules) { list, error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume(returning: list) }
            }
        }
        XCTAssertNotNil(list)
    }

    func testWidgetURLsStayOnExactSignedServerRoute() throws {
        let base = try XCTUnwrap(URL(string: "https://example.invalid:8766"))
        XCTAssertNotNil(EmbeddedCardDocument.widgetURL("/api/webui/companion/widgets/sig_123/payload-ABC", baseURL: base))
        for path in ["https://other.invalid/api/webui/companion/widgets/sig/name", "/api/webui/companion/widgets/sig/name?redirect=1", "/api/webui/companion/widgets/sig/name#fragment", "/api/webui/companion/widgets/sig/../secret", "/api/webui/companion/widgets/sig/a%2Fb", "/api/webui/companion/avatars/sig/name", "file:///api/webui/companion/widgets/sig/name"] {
            XCTAssertNil(EmbeddedCardDocument.widgetURL(path, baseURL: base), path)
        }
    }

    func testBridgeOnlyAcceptsNonemptyStringMessagesAndBoundsOutput() {
        XCTAssertNil(EmbeddedCardDocument.message(.object(["type": .string("companion:send"), "text": .number(12)])))
        XCTAssertNil(EmbeddedCardDocument.message(.object(["type": .string("other"), "text": .string("text")])))
        XCTAssertNil(EmbeddedCardDocument.message(.object(["type": .string("companion:send"), "text": .string(" \n ")])))
        XCTAssertEqual(EmbeddedCardDocument.message(.object(["type": .string("companion:send"), "text": .string("  示例 \n")])), "示例")
        XCTAssertEqual(EmbeddedCardDocument.message(.object(["type": .string("companion:send"), "text": .string(String(repeating: "x", count: 2500))]))?.count, 2000)
        XCTAssertEqual(EmbeddedCardDocument.height(.nan), 240)
        XCTAssertEqual(EmbeddedCardDocument.height(10), 160)
        XCTAssertEqual(EmbeddedCardDocument.height(9000), 600)
    }

    func testContentPolicyBlocksNetworkAndKeepsOpaqueOrigin() throws {
        let rules = try JSONValue.parse(Data(EmbeddedCardDocument.networkRules.utf8))
        XCTAssertEqual(rules.array.first?["action"]["type"].stringValue, "block")
        XCTAssertEqual(rules.array.last?["trigger"]["resource-type"], .array([.string("image")]))
        let kit = EmbeddedUiKit(css: "LOCAL_CSS", js: "LOCAL_KIT", chart: "LOCAL_CHART", leaflet: "LOCAL_MAP", leafletCSS: "LOCAL_MAP_CSS", markerSetup: "")
        let page = EmbeddedCardDocument.make(content: "<button data-send=\"你好\">操作</button>", kit: kit, tokens: [:], dark: true)
        XCTAssertTrue(page.contains("sandbox=\"allow-scripts allow-forms\""))
        XCTAssertFalse(page.contains("allow-same-origin"))
        XCTAssertTrue(page.contains("connect-src 'none'"))
        XCTAssertTrue(page.contains("form-action 'none'"))
        XCTAssertTrue(page.contains("event.source!==frame.contentWindow"))
        XCTAssertTrue(page.contains("navigator.userActivation.isActive"))
        XCTAssertTrue(page.contains("&lt;button"))
    }

    func testLocalLibrariesReplaceCDNsAndUnsafeThemeValuesAreDropped() {
        let kit = EmbeddedUiKit(css: "LOCAL_CSS", js: "LOCAL_KIT", chart: "LOCAL_CHART", leaflet: "LOCAL_MAP", leafletCSS: "LOCAL_MAP_CSS", markerSetup: "")
        let page = EmbeddedCardDocument.make(content: "<!doctype html><html><head><script src=\"https://cdn.example.invalid/chart.js\"></script></head><body><div id=\"chart\"></div><script>new Chart('chart',{}); L.map('map');</script></body></html>", kit: kit,
                                             tokens: ["--c-primary": "#123456", "--c-evil": "</style><script>bad()</script>", "--c-x;}evil{": "red"], dark: false)
        XCTAssertTrue(page.contains("LOCAL_CHART"))
        XCTAssertTrue(page.contains("LOCAL_MAP"))
        XCTAssertFalse(page.contains("cdn.example.invalid"))
        XCTAssertFalse(page.contains("bad()"))
        XCTAssertFalse(page.contains("evil{"))
        XCTAssertTrue(page.contains("#123456"))
    }

    func testBundledKitAndMapIconsArePresent() throws {
        let kit = try EmbeddedUiKit.load()
        XCTAssertTrue(kit.js.contains("companion:send"))
        XCTAssertTrue(kit.css.contains("--c-primary"))
        XCTAssertTrue(kit.chart.contains("Chart"))
        XCTAssertTrue(kit.leaflet.contains("Leaflet"))
        XCTAssertTrue(kit.leafletCSS.contains("data:image/png;base64,"))
        XCTAssertTrue(kit.markerSetup.contains("iconRetinaUrl:'data:image/png;base64,"))
    }
}
