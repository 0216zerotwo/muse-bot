import Foundation
import XCTest
@testable import Companion

final class IdentityDecodingTests: XCTestCase {
    private func fixture(_ name: String) throws -> JSONValue {
        let bundle = Bundle(for: Self.self)
        let url = bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
            ?? bundle.url(forResource: name, withExtension: "json")
        return try JSONValue.parse(Data(contentsOf: XCTUnwrap(url, "Missing fixture \(name)")))
    }

    func testObservedIdleComputerKeepsNullablePageAndHandoff() throws {
        let status = try fixture("identity-computer")
        XCTAssertFalse(status["running"].boolValue)
        for key in ["since", "page", "hold", "handoff", "last_action"] { XCTAssertEqual(status[key], .null) }
        XCTAssertEqual(status["page"]["title"].stringValue, "")
    }

    func testObservedSoulDocumentIsTextWithoutEnvelope() throws {
        let value = try fixture("identity-soul")
        XCTAssertNotNil(value["text"].string)
        XCTAssertEqual(value["result"], .null)
        XCTAssertNotNil(value["updated_at"].doubleValue)  // the card's date
        XCTAssertEqual(value.object.count, 2)
    }

    func testObservedMemoryDocumentIsTextWithoutEnvelope() throws {
        let value = try fixture("identity-memory-doc")
        XCTAssertNotNil(value["text"].string)
        XCTAssertEqual(value["result"], .null)
        XCTAssertNotNil(value["updated_at"].doubleValue)
        XCTAssertEqual(value.object.count, 2)
    }

    func testObservedMemoryChangesKeepSecondsAndBeforeAfterShape() throws {
        let value = try fixture("identity-memory")
        let section = try XCTUnwrap(value["sections"].records.first)
        XCTAssertEqual(section["file"].stringValue, "USER.md")
        XCTAssertFalse(section["items"].records.isEmpty)
        let change = try XCTUnwrap(value["changes"].records.first)
        XCTAssertEqual(change["kind"].stringValue, "learn-add")
        XCTAssertNotNil(change["after"].string)
        XCTAssertEqual(change["before"].array, [])
        let learnedAt = try XCTUnwrap(value["learned_at"].doubleValue)
        XCTAssertLessThan(learnedAt, 100_000_000_000, "Memory timestamps are seconds, unlike chat event milliseconds")
        XCTAssertFalse(change["undone"].boolValue, "Older change records omit undone")
    }
}
