import Foundation

/// A scheduled job's timing in words, as the web's describeSchedule / cronLabel / triggerWhen:
/// "每天 08:00", "工作日 07:30", "每天 9/12/15 点", "每 30 分钟", "一次 · 明天 08:00".
enum Schedules {
    private static let days = ["0": "周日", "1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六", "7": "周日"]

    static func describe(_ job: JSONValue) -> String {
        let schedule = job["schedule"]
        switch schedule["kind"].stringValue {
        case "at":
            if let at = schedule["at_ms"].doubleValue { return "一次 · \(dayLabel(at))" }
        case "every":
            if let every = schedule["every_ms"].doubleValue, every > 0 {
                let minutes = Int((every / 60_000).rounded())
                return minutes >= 60 && minutes % 60 == 0 ? "每 \(minutes / 60) 小时" : "每 \(minutes) 分钟"
            }
        case "cron":
            if let expr = schedule["expr"].nonemptyString { return cron(expr) }
        case "local":
            return "由外部触发"
        default: break
        }
        return "定时"
    }

    static func cron(_ expr: String) -> String {
        let parts = expr.split(whereSeparator: \.isWhitespace).map(String.init)
        let raw = "定时 · \(expr)"
        guard parts.count == 5 else { return raw }
        let (minute, hour, dom, month, dow) = (parts[0], parts[1], parts[2], parts[3], parts[4])
        func digits(_ text: String) -> Bool { !text.isEmpty && text.allSatisfy(\.isNumber) }
        guard month == "*", digits(minute) else { return raw }
        let at: String
        if digits(hour) {
            at = String(format: "%02d:%02d", Int(hour) ?? 0, Int(minute) ?? 0)
        } else if hour.contains(","), hour.split(separator: ",").allSatisfy({ digits(String($0)) }) {
            at = hour.split(separator: ",").joined(separator: "/") + " 点" + (minute == "0" ? "" : " \(minute) 分")
        } else { return raw }
        if dom == "*" && dow == "*" { return "每天 \(at)" }
        if dom == "*" && dow == "1-5" { return "工作日 \(at)" }
        if dom == "*", let day = days[dow] { return "每\(day) \(at)" }
        if digits(dom) && dow == "*" { return "每月 \(dom) 日 \(at)" }
        return raw
    }

    /// When a trigger looks: "每 1 小时看一次新邮件", "收到通知时".
    static func triggerWhen(_ trigger: JSONValue) -> String {
        let kind = trigger["kind"].stringValue
        if kind == "webhook" { return trigger["fired_at"].doubleValue.map { "上次触发 \(dayLabel($0))" } ?? "收到通知时" }
        let every = trigger["every"].intValue ?? 60
        let span = every % 60 == 0 ? "\(every / 60) 小时" : "\(every) 分钟"
        switch kind {
        case "email": return "每 \(span)看一次新邮件"
        case "calendar": return "每 \(span)看一次日程"
        default: return "每 \(span)看一次网页"
        }
    }

    static let triggerEmoji = ["email": "📧", "calendar": "📅", "page": "🌐", "webhook": "🔗"]

    static func automationRunPath(jobID: String, runID: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        func encodeSegment(_ raw: String) -> String {
            if raw == "." { return "%2E" }
            if raw == ".." { return "%2E%2E" }
            return raw.addingPercentEncoding(withAllowedCharacters: allowed) ?? raw
        }
        return "/api/webui/automations/\(encodeSegment(jobID))/runs/\(encodeSegment(runID))"
    }
}
