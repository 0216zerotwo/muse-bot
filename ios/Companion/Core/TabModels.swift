import Foundation

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

    // Formatters are slow to make and these run for every row on every redraw: made once.
    private static let precise: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    private static let plain = ISO8601DateFormatter()
    private static let monthDayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日"
        return formatter
    }()
    private static let yearMonthDayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "yyyy年M月d日"
        return formatter
    }()

    static func date(_ value: JSONValue) -> Date? {
        if let ms = value.doubleValue { return Date(timeIntervalSince1970: ms / 1_000) }
        guard let text = value.nonemptyString else { return nil }
        return precise.date(from: text) ?? plain.date(from: text)
    }

    static func day(_ value: JSONValue) -> String {
        guard let date = date(value) else { return "其他日期" }
        if Calendar.current.isDateInToday(date) { return "今天" }
        if Calendar.current.isDateInYesterday(date) { return "昨天" }
        let sameYear = Calendar.current.component(.year, from: date) == Calendar.current.component(.year, from: Date())
        return (sameYear ? monthDayFormatter : yearMonthDayFormatter).string(from: date)
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
