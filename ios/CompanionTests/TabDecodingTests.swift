import XCTest
@testable import Companion

final class TabDecodingTests: XCTestCase {
    private func fixture(_ name: String) throws -> JSONValue {
        let bundle = Bundle(for: TabDecodingTests.self)
        let url = try XCTUnwrap(bundle.url(forResource: name, withExtension: "json", subdirectory: "Fixtures") ?? bundle.url(forResource: name, withExtension: "json"))
        return try JSONValue.parse(Data(contentsOf: url))
    }

    func testRealFeedResponse() throws {
        let data = try fixture("tabs-feed")
        XCTAssertNotNil(data["instructions"].nonemptyString)
        XCTAssertNotNil(data["time"].nonemptyString)
        let posts = TabRecords.items(data, key: "posts")
        XCTAssertFalse(posts.isEmpty)
        XCTAssertNotNil(posts.first?["ts"].doubleValue)
        XCTAssertNotNil(posts.first?["body"].nonemptyString)
        XCTAssertTrue(posts.contains { $0["image"] == .null })
    }

    func testRealIdeasResponse() throws {
        let data = try fixture("tabs-ideas")
        XCTAssertNotNil(data["generated_at"].doubleValue)
        let ideas = TabRecords.items(data, key: "ideas", identity: "title")
        XCTAssertFalse(ideas.isEmpty)
        XCTAssertNotNil(ideas.first?["reason"].nonemptyString)
        XCTAssertNotNil(ideas.first?["prompt"].nonemptyString)
    }

    func testRealGoalsResponseHasEmptyGoalsArray() throws {
        let data = try fixture("tabs-goals")
        XCTAssertEqual(data["goals"], .array([]))
        XCTAssertTrue(TabRecords.items(data, key: "goals").isEmpty)
    }

    func testRealRoutinesResponseAllowsNullBriefTime() throws {
        let data = try fixture("tabs-routines")
        XCTAssertEqual(data["brief_time"], .null)
        XCTAssertEqual(data["checkins"], .bool(false))
    }

    func testRealHelpersResponseContainsGroupsAndOptionalWork() throws {
        let data = try fixture("tabs-helpers")
        let helpers = TabRecords.items(data, key: "helpers")
        XCTAssertFalse(helpers.isEmpty)
        XCTAssertNotNil(helpers.first?["chat_id"].nonemptyString)
        XCTAssertTrue(helpers.contains { $0["last"] == .null })
        XCTAssertTrue(helpers.contains { !$0["work"].array.isEmpty })
        XCTAssertFalse(TabRecords.items(data, key: "groups").isEmpty)
        XCTAssertFalse(TabRecords.items(data, key: "templates", identity: "name").isEmpty)
        XCTAssertGreaterThan(data["max_group"].intValue ?? 0, 1)
    }

    func testRealCreationsResponseIncludesEachMediaType() throws {
        let data = try fixture("tabs-creations")
        let items = TabRecords.items(data, key: "items")
        XCTAssertFalse(items.isEmpty)
        XCTAssertTrue(items.contains { $0["kind"].stringValue == "image" })
        XCTAssertTrue(items.contains { $0["kind"].stringValue == "video" })
        XCTAssertTrue(items.contains { $0["kind"].stringValue == "document" })
        XCTAssertNotNil(items.first?["size"].intValue)
        XCTAssertNotNil(items.first?["url"].nonemptyString)
        XCTAssertNotNil(items.first.flatMap { TabRecords.date($0["created_at"]) })
    }

    func testRealTriggersResponseHasEmptyArray() throws {
        let data = try fixture("tabs-triggers")
        XCTAssertEqual(data["triggers"], .array([]))
    }

    func testRealAutomationsResponseKeepsNestedScheduleAndState() throws {
        let data = try fixture("tabs-automations")
        let jobs = TabRecords.items(data, key: "jobs")
        XCTAssertFalse(jobs.isEmpty)
        XCTAssertEqual(jobs.first?["schedule"]["kind"].stringValue, "cron")
        XCTAssertNotNil(jobs.first?["payload"]["message"].nonemptyString)
        XCTAssertNotNil(jobs.first?["state"]["next_run_at_ms"].doubleValue)
    }

    func testMalformedRowsAndNumericIDsDoNotBreakList() throws {
        let data = try JSONValue.parse(Data(#"{"items":[null,42,{}, {"id":12,"title":null,"extra":"allowed"},{"id":"good","title":55},{"id":"good","title":"duplicate"}]}"#.utf8))
        let items = TabRecords.items(data, key: "items")
        XCTAssertEqual(items.map(\.id), ["12", "good"])
        XCTAssertEqual(items[0].title, "未命名")
        XCTAssertEqual(items[1].title, "55")
    }

    func testGoalAndTriggerProjectionAllowsOptionalFieldsAndMixedTypes() throws {
        let data = try JSONValue.parse(Data(#"{"goals":[{"id":7,"title":"示例目标","done":false,"paused":false,"due":null,"checkin":null,"steps":[{"text":"第一步","status":"pending"}],"log":null}],"triggers":[{"id":"t","name":"示例触发","paused":true,"instruction":null}]}"#.utf8))
        XCTAssertEqual(TabRecords.items(data, key: "goals").first?.id, "7")
        XCTAssertEqual(TabRecords.items(data, key: "goals").first?["steps"].array.count, 1)
        XCTAssertEqual(TabRecords.items(data, key: "triggers").first?["paused"].boolValue, true)
    }
}
