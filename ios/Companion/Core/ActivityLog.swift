import Foundation

/// 做过的事 on the companion's page, listed as Muse lists tasks: each turn that did something (used
/// a tool) is one task — what was asked, what came of it, its steps — and so is each scheduled run.
struct ActivityStep: Identifiable, Equatable {
    let id: String
    let title: String
    let detail: String
    let failed: Bool
    /// Started and not finished yet.
    var running = false
}

struct ActivityTask: Identifiable, Equatable {
    enum Status: Equatable { case done, running, failed }
    let id: String
    let title: String
    let summary: String
    /// Milliseconds.
    let at: Double
    let status: Status
    let steps: [ActivityStep]
    /// The answer (Markdown, cards left out).
    let reply: String
    /// When its last message came (ms): how long it took.
    var end: Double?
    /// A scheduled run: its job and run (the report is read when it opens).
    var jobID: String?
    var runID: String?
    var schedule: String?
}

enum ActivityLog {
    /// ``detailed: false`` leaves out the title, summary and answer (the chat's step chips need only
    /// the steps and times, on every redraw).
    static func tasks(messages: [ChatMessage], running: Bool, jobs: [JSONValue], limit: Int = 60, detailed: Bool = true) -> [ActivityTask] {
        var order: [String] = []
        var turns: [String: [ChatMessage]] = [:]
        for message in messages {
            guard let turn = message.turnId, !turn.isEmpty else { continue }
            if turns[turn] == nil { order.append(turn) }
            turns[turn, default: []].append(message)
        }
        var result: [ActivityTask] = []
        for (index, turn) in order.enumerated() {
            let items = turns[turn] ?? []
            let steps = self.steps(items)
            guard !steps.isEmpty else { continue }  // only talking: not a task
            let request = items.first { $0.role == "user" }?.content ?? ""
            let answer = items.last { $0.role == "assistant" && Presentation.isVisibleReply($0) && $0.source == nil }
            let reply = detailed ? answer.map { ReplyContent.textWithoutCards($0.content) } ?? "" : ""
            let live = running && index == order.count - 1
            let status: ActivityTask.Status = live ? .running : (answer == nil && steps.contains(where: \.failed) ? .failed : .done)
            result.append(ActivityTask(id: turn, title: detailed ? title(request, fallback: steps[0].title) : "",
                                       summary: detailed ? summary(reply, steps: steps.count) : "",
                                       at: items.first?.createdAt ?? 0, status: status, steps: steps, reply: reply,
                                       end: items.last?.createdAt))
        }
        for job in jobs {
            for run in job["runs"].array {
                let at = run["at_ms"].doubleValue ?? 0
                let failed = run["status"].stringValue == "error"
                result.append(ActivityTask(id: job["id"].stringValue + "/" + run["id"].stringValue, title: job["name"].stringValue,
                                           summary: failed ? "定时任务没有完成" : "定时任务 · " + Schedules.describe(job), at: at,
                                           status: failed ? .failed : .done, steps: [], reply: "",
                                           jobID: job["id"].stringValue, runID: run["id"].stringValue, schedule: Schedules.describe(job)))
            }
        }
        return Array(result.sorted { $0.at > $1.at }.prefix(limit))
    }

    /// One step per tool call (its end replaces its start).
    static func steps(_ messages: [ChatMessage]) -> [ActivityStep] {
        var order: [String] = []
        var latest: [String: JSONValue] = [:]
        for message in messages {
            for (index, event) in message.toolEvents.enumerated() {
                let id = event["call_id"].nonemptyString ?? "\(message.id)-\(index)"
                if latest[id] == nil { order.append(id) }
                latest[id] = event
            }
        }
        return order.compactMap { id in
            guard let event = latest[id] else { return nil }
            let name = event["name"].nonemptyString ?? event["function"]["name"].stringValue
            let label = Presentation.toolActivity(name).label.replacingOccurrences(of: "正在", with: "")
            return ActivityStep(id: id, title: label.isEmpty ? name : label, detail: detail(event["arguments"]),
                                failed: event["error"].nonemptyString != nil, running: event["phase"].stringValue == "start")
        }
    }

    /// The turn at work now (the newest one): its steps so far, and when it began (ms).
    static func current(_ messages: [ChatMessage]) -> (steps: [ActivityStep], since: Double?) {
        guard let turn = messages.last(where: { $0.turnId?.isEmpty == false })?.turnId else { return ([], messages.last?.createdAt) }
        let items = messages.filter { $0.turnId == turn }
        return (steps(items), items.first?.createdAt)
    }

    /// What a step worked on: the search, the page, the command…
    static func detail(_ arguments: JSONValue) -> String {
        for key in ["query", "q", "url", "command", "path", "file_path", "name", "task", "prompt", "text", "element"] {
            if let value = arguments[key].nonemptyString { return line(value, limit: 80) }
        }
        return ""
    }

    static func title(_ request: String, fallback: String) -> String {
        let text = line(request.replacingOccurrences(of: #"^[#>*\-\s]+"#, with: "", options: .regularExpression), limit: 26)
        return text.isEmpty ? fallback : text
    }

    static func summary(_ reply: String, steps: Int) -> String {
        let plain = reply
            .replacingOccurrences(of: #"!?\[([^\]]*)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: #"[*_`#>|]"#, with: "", options: .regularExpression)
        let first = plain.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }.first { !$0.isEmpty } ?? ""
        return first.isEmpty ? "做了 \(steps) 步" : line(first, limit: 70)
    }

    static func line(_ text: String, limit: Int) -> String {
        let one = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? ""
        let collapsed = one.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return collapsed.count > limit ? String(collapsed.prefix(limit)) + "…" : collapsed
    }
}

/// How long something took, in words ("1 分 12 秒") or as a running clock ("1:12").
enum Elapsed {
    static func words(_ ms: Double) -> String {
        let seconds = max(1, Int(ms / 1000))
        if seconds < 60 { return "\(seconds) 秒" }
        if seconds < 3600 { return "\(seconds / 60) 分" + (seconds % 60 > 0 ? " \(seconds % 60) 秒" : "") }
        return "\(seconds / 3600) 小时" + (seconds % 3600 / 60 > 0 ? " \(seconds % 3600 / 60) 分" : "")
    }

    static func clock(_ ms: Double) -> String {
        let seconds = max(0, Int(ms / 1000))
        if seconds >= 3600 { return String(format: "%d:%02d:%02d", seconds / 3600, seconds % 3600 / 60, seconds % 60) }
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
}

/// How an approval reads (its kind, title, what it is about, how it ended), for the card that asks
/// and for 审核记录.
enum ApprovalWords {
    private static let actionAliases: [String: String] = [
        "send_mail": "send_email", "reply_email": "send_email", "send_message": "send_email",
        "delete_email": "delete_emails", "trash_email": "delete_emails",
        "remove_event": "delete_event"
    ]

    static func action(_ approval: JSONValue) -> String {
        if let value = approval["action"].nonemptyString { return actionAliases[value] ?? value }
        let tool = approval["tool_name"].stringValue
        if let parsed = Presentation.parseMCPTool(tool) {
            let candidate = actionAliases[parsed.tool] ?? parsed.tool
            if titles[candidate] != nil { return candidate }
        }
        let suffixes = ["send_email", "send_mail", "reply_email", "forward_email",
                        "delete_emails", "delete_email", "trash_email", "email_command",
                        "delete_event", "remove_event"]
        if let matched = suffixes.first(where: { tool.hasSuffix("_" + $0) }) {
            return actionAliases[matched] ?? matched
        }
        return "email_command"
    }

    /// Normalizes `recipients` or `to` whether passed as `string[]`, comma-separated `string`, or scalar.
    static func recipients(_ args: JSONValue) -> [String] {
        let raw = args["recipients"] != .null ? args["recipients"] : args["to"]
        let pieces: [String]
        if !raw.array.isEmpty {
            pieces = raw.array.compactMap(\.nonemptyString)
        } else if let single = raw.nonemptyString {
            pieces = [single]
        } else {
            pieces = []
        }
        var seen: Set<String> = []
        var result: [String] = []
        for item in pieces {
            for part in item.components(separatedBy: CharacterSet(charactersIn: ",;，；、")) {
                let clean = part.trimmingCharacters(in: .whitespacesAndNewlines)
                if !clean.isEmpty, seen.insert(clean.lowercased()).inserted {
                    result.append(clean)
                }
            }
        }
        return result
    }

    /// Normalizes `email_ids` or `email_id` across string/numeric arrays and comma-separated strings.
    static func emailIDs(_ args: JSONValue) -> [String] {
        let raw = args["email_ids"] != .null ? args["email_ids"] : args["email_id"]
        if !raw.array.isEmpty {
            return raw.array.compactMap(\.nonemptyString)
        }
        guard let text = raw.nonemptyString else { return [] }
        return text.components(separatedBy: CharacterSet(charactersIn: ",;")).map {
            $0.trimmingCharacters(in: .whitespacesAndNewlines)
        }.filter { !$0.isEmpty }
    }

    static let titles = [
        "send_email": "邮件", "forward_email": "转发邮件", "delete_emails": "删除邮件", "email_command": "邮件操作", "delete_event": "删除日程",
        "browser_submit": "在网页上点按钮", "browser_upload": "上传文件到网页", "run_command": "运行命令", "goal_plan": "调整计划",
        "send_private": "把信息发到网站", "browse": "打开网页", "payment": "付款或下单", "connector_read": "读取连接的服务",
        "connector_write": "在连接的服务里修改", "schedule": "设定时任务",
    ]

    static func title(_ approval: JSONValue) -> String {
        // A press that may buy something reads as what it is, whatever kind of click it was held as.
        if approval["context"]["browser"]["paying"].boolValue { return titles["payment"] ?? "付款或下单" }
        return titles[action(approval)] ?? "操作确认"
    }

    /// A step in the browser in a sentence: where, what it presses, and what that does.
    static func browserSummary(_ approval: JSONValue) -> String? { approval["context"]["browser"]["summary"].nonemptyString }

    /// What it was about, in a line: who the mail goes to, the command's reason, the site.
    static func subject(_ approval: JSONValue) -> String {
        let action = action(approval)
        let args = approval["arguments"]
        if ["send_email", "forward_email"].contains(action) {
            let list = recipients(args)
            let subjectText = args["subject"].nonemptyString.map { "「\($0)」" } ?? ""
            if !list.isEmpty {
                return "发给 " + list.joined(separator: "、") + subjectText
            }
            return subjectText
        }
        if action == "delete_emails" {
            let ids = emailIDs(args)
            if !ids.isEmpty { return "共 \(ids.count) 封邮件" }
        }
        if action == "run_command" { return args["reason"].nonemptyString ?? args["command"].stringValue }
        if action == "send_private" { return "发到 " + approval["context"]["egress"]["host"].stringValue }
        if let summary = browserSummary(approval) { return summary.components(separatedBy: "。").first ?? summary }
        if let element = args["element"].nonemptyString { return "「\(element)」" }
        return args["url"].nonemptyString ?? ""
    }

    static func icon(_ approval: JSONValue) -> String {
        switch action(approval) {
        case "send_email", "forward_email", "delete_emails", "email_command": return "envelope"
        case "browse", "send_private", "browser_submit", "browser_upload": return "globe"
        case "run_command": return "terminal"
        case "payment": return "creditcard"
        case "schedule", "delete_event": return "calendar"
        default: return "wrench.adjustable"
        }
    }

    /// 已允许一次 / 始终允许 / 已被拒…, and whether it was a yes.
    static func outcome(_ approval: JSONValue) -> (label: String, allowed: Bool?) {
        switch approval["status"].stringValue {
        case "pending": return ("等你确认", nil)
        case "rejected": return ("已被拒", false)
        case "failed": return ("已允许 · 没有成功", true)
        default:
            switch approval["scope"].stringValue {
            case "24h": return ("24 小时内允许", true)
            case "always": return ("始终允许", true)
            default: return ("已允许一次", true)
            }
        }
    }

    static func argumentLabel(_ key: String) -> String {
        ["recipients": "收件人", "to": "收件人", "cc": "抄送", "bcc": "密送", "subject": "主题", "body": "正文", "command": "命令", "reason": "原因",
         "paths": "文件", "attachments": "附件", "element": "点击", "email_id": "原邮件", "email_ids": "邮件", "event_id": "日程", "url": "网站", "protocol": "协议"][key] ?? key
    }

    static func argumentText(_ value: JSONValue) -> String {
        if !value.array.isEmpty { return value.array.map(argumentText).joined(separator: "、") }
        if !value.object.isEmpty { return value.object.keys.sorted().map { $0 + "：" + argumentText(value[$0]) }.joined(separator: "\n") }
        if case .bool(let flag) = value { return flag ? "是" : "否" }
        return value.nonemptyString ?? ""
    }
}
