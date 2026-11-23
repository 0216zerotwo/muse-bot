import Foundation

struct Activity: Equatable, Sendable {
    var kind: String
    var label: String
}

enum Presentation {
    private static let tools: [String: Activity] = {
        var result: [String: Activity] = [:]
        func add(_ names: [String], _ kind: String, _ label: String) {
            for name in names { result[name] = Activity(kind: kind, label: label) }
        }
        add(["web_search"], "search", "正在搜索网页")
        add(["web_fetch", "web_extract"], "read", "正在阅读网页")
        add(["read_file", "list_dir", "find_files", "grep", "rg", "search_files"], "files", "正在翻看文件")
        add(["write_file", "edit_file", "apply_patch", "patch"], "files", "正在整理文件")
        add(["exec", "exec_session", "terminal"], "run", "正在运行命令")
        add(["list_exec_sessions", "process_manage"], "run", "正在查看运行中的命令")
        add(["execute_code"], "run", "正在运行代码")
        add(["run_cli_app"], "run", "正在使用命令行应用")
        add(["generate_image", "image_generate"], "image", "正在画图")
        add(["vision_analyze"], "read", "正在看图")
        add(["cron", "cronjob_manage"], "schedule", "正在安排提醒")
        add(["message"], "send", "正在发送消息")
        add(["send_session_message"], "send", "正在给另一个对话发消息")
        add(["search_sessions", "read_session", "list_sessions", "session_search"], "recall", "正在回忆之前的对话")
        add(["spawn", "subagent", "delegate_task"], "delegate", "正在安排人手")
        add(["create_goal"], "delegate", "正在制定计划")
        add(["update_goal"], "delegate", "正在更新计划")
        add(["my"], "working", "正在查看自己的设置")
        add(["memory"], "recall", "正在记下来")
        add(["todo_list"], "working", "正在列计划")
        add(["skill_view", "skills_list"], "working", "正在翻自己的笔记")
        add(["skill_manage"], "working", "正在整理经验")
        add(["text_to_speech"], "app", "正在录语音")
        add(["clarify"], "thinking", "正在想要问你什么")
        add(["tool_search", "tool_describe"], "working", "正在找合适的办法")
        add(["trigger_add"], "schedule", "正在设置自动触发")
        add(["trigger_list"], "schedule", "正在查看自动触发")
        add(["trigger_remove"], "schedule", "正在删除自动触发")
        add(["goal_list"], "working", "正在查看目标")
        add(["goal_add"], "delegate", "正在记下目标")
        add(["goal_update"], "delegate", "正在更新目标进度")
        add(["goal_propose_plan"], "delegate", "正在调整计划")
        add(["browser_hand_over"], "app", "正在请你接手浏览器")
        add(["helper_ask"], "delegate", "正在交给帮手")
        add(["browser_request"], "delegate", "正在请主助理用浏览器")
        add(["credential_request"], "app", "正在请你填写凭证")
        add(["avatar_create"], "image", "正在画新形象")
        add(["avatar_pick"], "image", "正在换上选好的形象")
        add(["avatar_reset"], "image", "正在换回默认形象")
        add(["avatar_animate"], "image", "正在让形象动起来")
        add(["connect_request"], "app", "正在请你连接服务")
        add(["helper_create"], "delegate", "正在新建帮手")
        add(["helper_note"], "working", "正在记工作笔记")
        add(["feed_post"], "working", "正在写动态")
        return result
    }()

    private static let browser: [String: String] = [
        "browser_navigate": "正在打开网页", "browser_navigate_back": "正在返回上一页",
        "browser_snapshot": "正在看网页", "browser_take_screenshot": "正在截图",
        "browser_click": "正在点网页", "browser_type": "正在填写网页",
        "browser_fill_form": "正在填写网页", "browser_select_option": "正在填写网页",
        "browser_press_key": "正在操作网页", "browser_wait_for": "正在等网页加载",
        "browser_tabs": "正在切换网页", "browser_back": "正在返回上一页",
        "browser_press": "正在操作网页", "browser_scroll": "正在翻网页",
        "browser_vision": "正在看网页", "browser_get_images": "正在看网页上的图"
    ]

    private static let gatedEmailActions: Set<String> = [
        "send_email", "send_mail", "reply_email", "forward_email",
        "delete_emails", "delete_email", "trash_email", "email_command", "send_message"
    ]
    private static let gatedCalendarActions: Set<String> = ["delete_event", "remove_event"]

    /// Splits both Hermes `mcp__<server>__<tool>` and legacy `mcp_<server>_<tool>` tool names.
    static func parseMCPTool(_ name: String) -> (server: String, tool: String)? {
        if name.hasPrefix("mcp__") {
            let rest = String(name.dropFirst(5))
            guard let sep = rest.range(of: "__") else { return nil }
            let server = String(rest[..<sep.lowerBound])
            let tool = String(rest[sep.upperBound...])
            return (!server.isEmpty && !tool.isEmpty) ? (server, tool) : nil
        }
        guard name.hasPrefix("mcp_") else { return nil }
        let rest = String(name.dropFirst(4))
        for suffix in browser.keys where rest.hasSuffix("_" + suffix) {
            let server = String(rest.dropLast(suffix.count + 1))
            if !server.isEmpty { return (server, suffix) }
        }
        for suffix in gatedEmailActions.union(gatedCalendarActions) where rest.hasSuffix("_" + suffix) {
            let server = String(rest.dropLast(suffix.count + 1))
            if !server.isEmpty { return (server, suffix) }
        }
        guard let sep = rest.firstIndex(of: "_") else { return (rest, "") }
        return (String(rest[..<sep]), String(rest[rest.index(after: sep)...]))
    }

    /// Strips a service's MCP prefix (`mcp__<server>__` with `-`/`_` normalized, or `mcp_<server>_`).
    static func stripMCPPrefix(_ toolName: String, server: String) -> String {
        let normalizedServer = server.replacingOccurrences(of: "-", with: "_")
        for candidate in ["mcp__\(normalizedServer)__", "mcp__\(server)__", "mcp_\(server)_", "mcp_\(normalizedServer)_"] {
            if toolName.hasPrefix(candidate) { return String(toolName.dropFirst(candidate.count)) }
        }
        if let parsed = parseMCPTool(toolName),
           parsed.server.replacingOccurrences(of: "-", with: "_") == normalizedServer,
           !parsed.tool.isEmpty {
            return parsed.tool
        }
        return toolName
    }

    static func toolActivity(_ name: String) -> Activity {
        if let known = tools[name] { return known }
        if name.hasPrefix("browser_") {
            return Activity(kind: "read", label: browser[name] ?? "正在操作浏览器")
        }
        if let mcp = parseMCPTool(name) {
            if mcp.tool.hasPrefix("browser_") {
                return Activity(kind: "read", label: browser[mcp.tool] ?? "正在操作浏览器")
            }
            if gatedCalendarActions.contains(mcp.tool) {
                return Activity(kind: "send", label: "正在准备删除日程")
            }
            if gatedEmailActions.contains(mcp.tool) {
                return Activity(kind: "send", label: "正在准备邮件")
            }
            if mcp.server == "email" {
                return Activity(kind: "read", label: "正在查看邮箱")
            }
            return Activity(kind: "app", label: mcp.server.isEmpty ? "正在使用应用" : "正在使用 " + mcp.server)
        }
        return Activity(kind: "working", label: "正在处理")
    }

    static func currentActivity(messages: [ChatMessage], streaming: Bool) -> Activity? {
        guard streaming else { return nil }
        for message in messages.reversed() {
            if message.role == "user" { break }
            if let running = message.toolEvents.last(where: { $0["phase"].stringValue == "start" }) {
                return toolActivity(running["name"].string ?? running["function"]["name"].stringValue)
            }
            if message.role != "assistant" || message.kind == "trace" { continue }
            if message.isStreaming && !message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return Activity(kind: "typing", label: "正在输入")
            }
            if message.reasoningStreaming { return Activity(kind: "thinking", label: "正在思考") }
        }
        return Activity(kind: "thinking", label: "正在思考")
    }

    static func isVisibleReply(_ message: ChatMessage) -> Bool {
        guard message.role == "assistant", message.kind != "trace", message.activityKind != "model" else { return false }
        let text = message.content.trimmingCharacters(in: .whitespacesAndNewlines)
        return !text.hasPrefix("⏳") && (!text.isEmpty || !message.media.isEmpty)
    }

    static func isVisible(_ message: ChatMessage) -> Bool {
        message.role == "user" || message.source?["kind"].stringValue == "notice" || isVisibleReply(message)
    }

    static func isProactive(_ message: ChatMessage) -> Bool {
        message.role == "assistant" && message.source != nil && message.source?["label"].stringValue != "审批回执"
    }

    static func awaitingReply(messages: [ChatMessage], streaming: Bool) -> Bool {
        guard streaming else { return false }
        for message in messages.reversed() {
            if message.role == "user" { return true }
            if isVisibleReply(message) { return !message.isStreaming }
        }
        return true
    }

    /// A scheduled reminder after a question leaves that question's cards open.
    static func answerableReplyID(messages: [ChatMessage]) -> String? {
        var candidate: String?
        for message in messages {
            if message.source?["kind"].stringValue == "notice" { continue }
            if message.role == "user" { candidate = nil }
            else if isVisibleReply(message) {
                let kind = message.source?["kind"].stringValue ?? ""
                if kind != "cron" && kind != "checkin" { candidate = message.id }
            }
        }
        return candidate
    }
}

struct AvatarPicture: Equatable, Sendable {
    var clip: String?
    var still: String
}

enum AvatarChoice {
    static let busy: Set<String> = ["running", "running-left", "running-right", "review", "typing", "reading", "drawing", "writing", "searching"]

    static func picture(source: JSONValue, animation: String) -> AvatarPicture {
        let motions = source["motions"]
        let clip: String?
        if animation == "waiting" { clip = motions["waiting"].nonemptyString }
        else if animation == "review" { clip = motions["thinking"].nonemptyString ?? motions["working"].nonemptyString }
        else if busy.contains(animation) { clip = motions["working"].nonemptyString }
        else if animation == "idle" { clip = motions["idle"].nonemptyString }
        else { clip = nil }
        let base = source["image_url"].nonemptyString ?? source["url"].stringValue
        let still: String
        if busy.contains(animation), let working = source["working_url"].nonemptyString { still = working }
        else if ["jumping", "waving"].contains(animation), let happy = source["happy_url"].nonemptyString { still = happy }
        else { still = base }
        return AvatarPicture(clip: clip, still: still)
    }
}
