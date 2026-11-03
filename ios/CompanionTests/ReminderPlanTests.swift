import XCTest
@testable import Companion

/// Reminders go off on the phone itself (the app gets no push): when, and what it says.
final class ReminderPlanTests: XCTestCase {
    func testUpcomingRemindersBecomeTheirOwnNotifications() throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let ms = now.timeIntervalSince1970 * 1000
        let jobs = try JSONValue.parse(Data("""
        {"jobs": [
          {"id": "water", "enabled": true, "notice": "喝水提醒", "upcoming_ms": [\(ms + 7_200_000), \(ms + 1_000), \(ms - 60_000)]},
          {"id": "brief", "enabled": true, "notice": "每日早报", "upcoming_ms": [\(ms + 3_600_000)]},
          {"id": "feed", "enabled": true, "notice": null, "upcoming_ms": [\(ms + 600_000)]},
          {"id": "paused", "enabled": false, "notice": "吃维生素", "upcoming_ms": [\(ms + 600_000)]}
        ]}
        """.utf8))["jobs"].array
        let times = ReminderPlan.times(jobs, now: now)
        XCTAssertEqual(times.map(\.body), ["每日早报", "喝水提醒"])  // soonest first; past, silent and paused left out
        XCTAssertEqual(times.first?.at, now.addingTimeInterval(3_600))
        XCTAssertEqual(times.first?.id, "brief-\(Int64(ms + 3_600_000))")
    }

    func testNoMoreThanThePhoneKeeps() throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let many = (1...100).map { "\(now.timeIntervalSince1970 * 1000 + Double($0) * 60_000)" }.joined(separator: ",")
        let jobs = try JSONValue.parse(Data(#"{"jobs": [{"id": "j", "enabled": true, "notice": "提醒", "upcoming_ms": [\#(many)]}]}"#.utf8))["jobs"].array
        XCTAssertEqual(ReminderPlan.times(jobs, now: now).count, ReminderPlan.limit)
    }
}
