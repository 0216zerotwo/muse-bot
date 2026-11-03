import Foundation
import XCTest
@testable import Companion

/// Fixtures were collected with the authorized GET-only probe. User text, IDs,
/// signed paths and image data are neutralized; the observed wire field layouts
/// and JSON types are retained. No live credentials or transcript text is stored.
final class CoreDecodingTests: XCTestCase {
    private func fixture(_ name: String) throws -> JSONValue {
        let bundle = Bundle(for: CoreDecodingTests.self)
        let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
            ?? bundle.url(forResource: name, withExtension: "json")
        return try JSONValue.parse(Data(contentsOf: XCTUnwrap(url, "Missing fixture \(name)")))
    }

    func testRealCompanionStateAllowsNullIdentityFields() throws {
        let state = try fixture("core-state")
        XCTAssertEqual(state["schema_version"].intValue, 1)
        XCTAssertEqual(state["theme_mode"].stringValue, "dark")
        XCTAssertEqual(state["pet_size"].stringValue, "large")
        XCTAssertEqual(state["user_name"], .null)
        XCTAssertNotNil(state["feed_seen_at"].doubleValue)
        XCTAssertNotNil(state["chat_id"].nonemptyString)
    }

    func testRealSessionsUseKeysAndOptionalHelperAndGroupMarkers() throws {
        let sessions = try fixture("core-sessions")["sessions"].records
        XCTAssertFalse(sessions.isEmpty)
        XCTAssertTrue(sessions.allSatisfy { $0["key"].stringValue.hasPrefix("websocket:") })
        XCTAssertTrue(sessions.contains { $0["helper"].nonemptyString != nil })
        XCTAssertTrue(sessions.contains { $0["group"].boolValue })
        XCTAssertTrue(sessions.contains { $0["title"].stringValue.isEmpty })
    }

    func testRealAvatarLibraryUsesItemsAndOptionalMotionURLs() throws {
        let avatar = try XCTUnwrap(fixture("core-avatars")["items"].records.first)
        XCTAssertNotNil(avatar["id"].nonemptyString)
        XCTAssertEqual(avatar["sheet_url"], .null)
        XCTAssertFalse(AvatarChoice.picture(source: avatar, animation: "idle").still.isEmpty)
        XCTAssertNotNil(AvatarChoice.picture(source: avatar, animation: "waiting").clip)
        XCTAssertFalse(avatar["motion_pending"].boolValue)
    }

    func testRealHelpersKeepTemplatesGroupsAndNullableLastMessage() throws {
        let resource = try fixture("core-helpers")
        let helper = try XCTUnwrap(resource["helpers"].records.first)
        XCTAssertEqual(helper["last"], .null)
        XCTAssertNotNil(helper["chat_id"].nonemptyString)
        XCTAssertFalse(resource["templates"].records.isEmpty)
        let group = try XCTUnwrap(resource["groups"].records.first)
        XCTAssertFalse(group["members"].array.isEmpty)
        XCTAssertNotNil(group["chat_id"].nonemptyString)
    }

    func testRealSessionScopedApprovalsKeepPerRecordScopes() throws {
        let approvals = try fixture("core-approvals")["approvals"].records
        XCTAssertFalse(approvals.isEmpty)
        for approval in approvals {
            XCTAssertNotNil(approval["id"].nonemptyString)
            XCTAssertFalse(approval["arguments"].object.isEmpty)
            XCTAssertEqual(approval["scope"].stringValue, "once")
            XCTAssertFalse(approval["scopes"].array.isEmpty)
            XCTAssertNotNil(approval["created_at_ms"].doubleValue)
        }
    }

    func testRealCanonicalThreadReplaysObservedEventShapes() throws {
        let thread = try fixture("core-thread")
        XCTAssertEqual(thread["schemaVersion"].intValue, 1)
        XCTAssertEqual(thread["projection"].stringValue, "events")
        XCTAssertTrue(thread["sessionKey"].stringValue.hasPrefix("websocket:"))
        let events = thread["events"].records
        XCTAssertTrue(events.contains { $0["event"].stringValue == "stream_end" })
        XCTAssertTrue(events.contains { $0["kind"].stringValue == "tool_hint" })
        let messages = StreamProjection.replay(events)
        XCTAssertTrue(messages.contains { $0.role == "user" })
        XCTAssertTrue(messages.contains { !$0.media.isEmpty })
        XCTAssertTrue(messages.allSatisfy { !$0.isStreaming })
    }

    func testLenientDisplayFieldsAndMalformedRowsDoNotFailWholeList() throws {
        let value = try JSONValue.parse(Data(#"{"name":72,"enabled":"true","count":"4","items":[null,8,"bad",{"id":9,"title":null},{}],"new_field":{"anything":true}}"#.utf8))
        XCTAssertEqual(value["name"].stringValue, "72")
        XCTAssertTrue(value["enabled"].boolValue)
        XCTAssertEqual(value["count"].intValue, 4)
        XCTAssertEqual(value["items"].records.count, 2)
        XCTAssertEqual(value["items"].records.first?["id"].nonemptyString, "9")
        XCTAssertEqual(value["missing"].stringValue, "")
        XCTAssertEqual(value["missing"].records.count, 0)
        XCTAssertNil(value["name"].string) // strict validation stays available to card parsers
    }

    func testMessageAndMediaCodableAllowMissingOptionalAndNumericFields() throws {
        let row = try JSONDecoder().decode(ChatMessage.self, from: Data(#"{"id":17,"content":null,"text":"示例","role":null,"media":[{"url":"/asset","kind":"image","name":27}],"new_field":true}"#.utf8))
        XCTAssertEqual(row.id, "17")
        XCTAssertEqual(row.role, "assistant")
        XCTAssertEqual(row.content, "示例")
        XCTAssertEqual(row.media.first?.kind, "image")
        XCTAssertEqual(row.media.first?.name, "27")
        let restored = try JSONDecoder().decode(ChatMessage.self, from: JSONEncoder().encode(row))
        XCTAssertEqual(restored, row)
    }

    @MainActor
    func testBootstrapSocketAddressKeepsExactPathAndEscapesToken() throws {
        // Deliberately synthetic bootstrap: the real endpoint contains live credentials,
        // so only its public schema is read from companion/server.py.
        let client = APIClient(baseURL: try XCTUnwrap(URL(string: "https://example.invalid")))
        let boot: JSONValue = .object(["ws_path": .string("/ws"), "token": .string("fixture + token")])
        let address = try client.socketURL(bootstrap: boot)
        let parts = try XCTUnwrap(URLComponents(url: address, resolvingAgainstBaseURL: false))
        XCTAssertEqual(parts.scheme, "wss")
        XCTAssertEqual(parts.path, "/ws")
        XCTAssertEqual(parts.queryItems?.first(where: { $0.name == "token" })?.value, "fixture + token")
    }
}
