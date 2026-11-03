import Foundation

/// A notification the phone sets for itself: this app gets no push, so a reminder (喝水, 每日早报)
/// would otherwise only show once the app is opened. The server says which jobs speak to the user on
/// time (``notice``) and when they run over the next two days (``upcoming_ms``).
struct ReminderTime: Equatable {
    let id: String
    let at: Date
    let body: String
}

enum ReminderPlan {
    /// iOS keeps 64 pending notifications per app; a few are left for anything else.
    static let limit = 60

    static func times(_ jobs: [JSONValue], now: Date) -> [ReminderTime] {
        var result: [ReminderTime] = []
        for job in jobs where job["enabled"].boolValue {
            guard let body = job["notice"].nonemptyString else { continue }
            for value in job["upcoming_ms"].array {
                guard let ms = value.doubleValue else { continue }
                let at = Date(timeIntervalSince1970: ms / 1000)
                guard at.timeIntervalSince(now) > 5 else { continue }  // not one already going off
                result.append(ReminderTime(id: job["id"].stringValue + "-" + String(Int64(ms)), at: at, body: body))
            }
        }
        return Array(result.sorted { $0.at < $1.at }.prefix(limit))
    }
}
