import Foundation

private final class FormatterBox: @unchecked Sendable {
    private let lock = NSLock()
    private let precise: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    private let plain = ISO8601DateFormatter()
    private let monthDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日"
        return formatter
    }()
    private let yearMonthDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "yyyy年M月d日"
        return formatter
    }()

    func parseISO8601(_ text: String) -> Date? {
        lock.lock()
        defer { lock.unlock() }
        return precise.date(from: text) ?? plain.date(from: text)
    }

    func formatDay(_ date: Date, sameYear: Bool) -> String {
        lock.lock()
        defer { lock.unlock() }
        return (sameYear ? monthDay : yearMonthDay).string(from: date)
    }
}

/// Tolerant list projection shared by feature screens. Nulls and malformed rows never poison a list.
struct TabRecord: Identifiable, Equatable {
    let id: String
    let value: JSONValue
    subscript(_ key: String) -> JSONValue { value[key] }
    var title: String { value["title"].nonemptyString ?? value["name"].nonemptyString ?? "未命名" }
}

enum TabRecords {
    static func items(_ payload: JSONValue, key: String, identity: String = "id") -> [TabRecord] {
        var seen = Set<String>()
        return payload[key].array.compactMap { row in
            guard !row.object.isEmpty, let id = row[identity].nonemptyString, seen.insert(id).inserted else { return nil }
            return TabRecord(id: id, value: row)
        }
    }

    // Formatters are slow to make and these run for every row on every redraw: made once, lock-guarded.
    private static let formatters = FormatterBox()

    static func date(_ value: JSONValue) -> Date? {
        if let ms = value.doubleValue { return Date(timeIntervalSince1970: ms / 1_000) }
        guard let text = value.nonemptyString else { return nil }
        return formatters.parseISO8601(text)
    }

    static func day(_ value: JSONValue) -> String {
        guard let date = date(value) else { return "其他日期" }
        if Calendar.current.isDateInToday(date) { return "今天" }
        if Calendar.current.isDateInYesterday(date) { return "昨天" }
        let sameYear = Calendar.current.component(.year, from: date) == Calendar.current.component(.year, from: Date())
        return formatters.formatDay(date, sameYear: sameYear)
    }

    static func ago(_ value: JSONValue) -> String {
        guard let date = date(value) else { return "" }
        let minutes = max(0, Int(Date().timeIntervalSince(date) / 60))
        if minutes < 1 { return "刚刚" }
        if minutes < 60 { return "\(minutes)分钟" }
        if minutes < 1_440 { return "\(minutes / 60)小时" }
        return "\(minutes / 1_440)天"
    }
}
