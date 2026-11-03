import XCTest
@testable import Companion

/// 做过的事 and 审核记录 on the companion's page: tasks from what it did, and how approvals read.
final class ActivityLogTests: XCTestCase {
    private func message(_ role: String, _ text: String, turn: String, at: Double, tools: [JSONValue] = []) -> ChatMessage {
        var message = ChatMessage(role: role, content: text, createdAt: at)
        message.turnId = turn
        message.toolEvents = tools
        return message
    }

    private func tool(_ id: String, _ name: String, _ arguments: [String: JSONValue], phase: String = "end", error: String? = nil) -> JSONValue {
        .object(["call_id": .string(id), "name": .string(name), "phase": .string(phase), "arguments": .object(arguments),
                 "error": error.map(JSONValue.string) ?? .null])
    }

    func testATurnThatUsedToolsIsATaskAndTalkIsNot() {
        let messages = [
            message("user", "早", turn: "t1", at: 1_000),
            message("assistant", "早上好", turn: "t1", at: 2_000),
            message("user", "帮我查周六上午去杭州的高铁\n要靠窗", turn: "t2", at: 10_000),
            message("assistant", "", turn: "t2", at: 11_000, tools: [tool("a", "web_search", ["query": .string("上海 杭州 高铁")], phase: "start")]),
            message("assistant", "", turn: "t2", at: 12_000, tools: [tool("a", "web_search", ["query": .string("上海 杭州 高铁")]),
                                                                    tool("b", "web_fetch", ["url": .string("https://www.12306.cn/")], error: "超时")]),
            message("assistant", "**找到 3 班**，最早 7:05 出发。\n\n详情如下", turn: "t2", at: 13_000),
        ]
        let tasks = ActivityLog.tasks(messages: messages, running: false, jobs: [])
        XCTAssertEqual(tasks.map(\.id), ["t2"])
        let task = tasks[0]
        XCTAssertEqual(task.title, "帮我查周六上午去杭州的高铁")
        XCTAssertEqual(task.summary, "找到 3 班，最早 7:05 出发。")
        XCTAssertEqual(task.status, .done)
        XCTAssertEqual(task.steps.count, 2)  // a call's start and end are one step
        XCTAssertEqual(task.steps[0].detail, "上海 杭州 高铁")
        XCTAssertTrue(task.steps[1].failed)
        XCTAssertEqual(task.at, 10_000)
    }

    func testTheLastTurnIsRunningWhileTheChatWorks() {
        let messages = [message("user", "整理邮件", turn: "t1", at: 1_000),
                        message("assistant", "", turn: "t1", at: 2_000, tools: [tool("a", "list_emails", [:], phase: "start")])]
        XCTAssertEqual(ActivityLog.tasks(messages: messages, running: true, jobs: []).first?.status, .running)
        XCTAssertEqual(ActivityLog.tasks(messages: messages, running: false, jobs: []).first?.summary, "做了 1 步")
    }

    func testScheduledRunsAreTasksNewestFirst() throws {
        let job = try JSONValue.parse(Data(#"{"id":"j","name":"动态更新","schedule":{"kind":"cron","expr":"0 8 * * *"},"runs":[{"id":"r2","at_ms":5000,"status":"error"},{"id":"r1","at_ms":3000,"status":"ok"}]}"#.utf8))
        let messages = [message("user", "查天气", turn: "t", at: 4_000),
                        message("assistant", "晴", turn: "t", at: 4_500, tools: [tool("w", "web_search", ["query": .string("天气")])])]
        let tasks = ActivityLog.tasks(messages: messages, running: false, jobs: [job])
        XCTAssertEqual(tasks.map(\.id), ["j/r2", "t", "j/r1"])
        XCTAssertEqual(tasks[0].status, .failed)
        XCTAssertEqual(tasks[0].runID, "r2")
        XCTAssertEqual(tasks[2].summary, "定时任务 · 每天 08:00")
    }

    func testApprovalsReadAsAllowedOrRefused() throws {
        func approval(_ json: String) throws -> JSONValue { try JSONValue.parse(Data(json.utf8)) }
        let mail = try approval(#"{"tool_name":"mcp_email_send_email","status":"rejected","arguments":{"recipients":["a@b.c"],"subject":"周六"}}"#)
        XCTAssertEqual(ApprovalWords.title(mail), "邮件")
        XCTAssertEqual(ApprovalWords.subject(mail), "发给 a@b.c「周六」")
        XCTAssertEqual(ApprovalWords.outcome(mail).label, "已被拒")
        XCTAssertEqual(ApprovalWords.outcome(mail).allowed, false)
        let site = try approval(#"{"action":"send_private","status":"executed","scope":"24h","arguments":{},"context":{"egress":{"host":"jd.com"}}}"#)
        XCTAssertEqual(ApprovalWords.subject(site), "发到 jd.com")
        XCTAssertEqual(ApprovalWords.outcome(site).label, "24 小时内允许")
        XCTAssertEqual(ApprovalWords.icon(site), "globe")
        let press = try approval(#"{"action":"browser_submit","status":"pending","arguments":{"code":"click_at_xy(1, 2)"},"context":{"browser":{"host":"apple.com.cn","paying":true,"summary":"要在 apple.com.cn 上点「立即购买」。点了可能就下单或付款了。"}}}"#)
        XCTAssertEqual(ApprovalWords.title(press), "付款或下单")
        XCTAssertEqual(ApprovalWords.subject(press), "要在 apple.com.cn 上点「立即购买」")
    }

    func testTheWorkingTurnShowsItsStepsAndStart() {
        let messages = [message("user", "早", turn: "t0", at: 500),
                        message("user", "查快递", turn: "t1", at: 1_000),
                        message("assistant", "", turn: "t1", at: 2_000, tools: [tool("a", "web_search", ["query": .string("快递")]),
                                                                                tool("b", "web_fetch", ["url": .string("https://x.cn")], phase: "start")])]
        let current = ActivityLog.current(messages)
        XCTAssertEqual(current.since, 1_000)
        XCTAssertEqual(current.steps.map(\.running), [false, true])
        let light = ActivityLog.tasks(messages: messages, running: false, jobs: [], detailed: false)
        XCTAssertEqual(light.first?.steps.count, 2)
        XCTAssertEqual(light.first?.end, 2_000)
        XCTAssertEqual(light.first?.title, "")  // worked out only when the task is opened
    }

    func testDurationsReadNaturally() {
        XCTAssertEqual(Elapsed.words(8_400), "8 秒")
        XCTAssertEqual(Elapsed.words(72_000), "1 分 12 秒")
        XCTAssertEqual(Elapsed.words(180_000), "3 分")
        XCTAssertEqual(Elapsed.words(3_780_000), "1 小时 3 分")
        XCTAssertEqual(Elapsed.clock(72_000), "1:12")
        XCTAssertEqual(Elapsed.clock(3_723_000), "1:02:03")
    }

    func testAStrayFenceAfterACardLeavesNoEmptyBubble() {
        let reply = "记忆现在只有 5 条。\n\n```ui\n<div>体检</div>\n```\n```"
        let blocks = ReplyContent.blocks(reply)
        XCTAssertEqual(blocks.count, 2)  // the text and the card, nothing after
        if case .card = blocks[1] {} else { XCTFail("the card should come last") }
        XCTAssertEqual(ReplyContent.textWithoutCards(reply), "记忆现在只有 5 条。")
    }
}
