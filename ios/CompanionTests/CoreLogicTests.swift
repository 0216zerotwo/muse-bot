import XCTest
@testable import Companion

final class CoreLogicTests: XCTestCase {
    private func json(_ source: String) -> JSONValue { try! JSONValue.parse(Data(source.utf8)) }

    private func event(_ name: String, _ text: String? = nil, _ extra: [String: JSONValue] = [:]) -> JSONValue {
        var fields: [String: JSONValue] = ["event": .string(name), "chat_id": .string("c"), "turn_id": .string("t1")]
        if let text { fields["text"] = .string(text) }
        fields.merge(extra) { _, value in value }
        return .object(fields)
    }

    private func answers(_ events: [JSONValue]) -> [String] {
        StreamProjection.replay(events).filter { $0.role == "assistant" }.map(\.content)
    }

    func testStreamRewriteResumesInsideSameBubble() {
        // Mirrors companion-stream.test.ts: stream_end.text replaces, then delta appends.
        let result = answers([
            event("user_message", "找 modelId"),
            event("delta", "我找到了：modelId 是 fe50"),
            event("stream_end", "我找到了：它的 modelId 是 fe50cd", ["resuming": .bool(true), "merge_next": .bool(true)]),
            event("delta", "f3040449"), event("stream_end"), event("turn_end")
        ])
        XCTAssertEqual(result, ["我找到了：它的 modelId 是 fe50cdf3040449"])
    }

    func testFinalTextReplacesDraftAndMayArriveWithoutDelta() {
        XCTAssertEqual(answers([event("delta", "我先看看 Fish Audio 上"), event("stream_end", "找到了：fe50cdf3040449")]), ["找到了：fe50cdf3040449"])
        XCTAssertEqual(answers([event("stream_end", "整个答案"), event("turn_end")]), ["整个答案"])
    }

    func testPlainStreamBoundaryCreatesAnotherSegment() {
        var projection = StreamProjection()
        projection.apply(event("delta", "第一段"), now: 10)
        projection.apply(event("stream_end"), now: 11)
        projection.apply(event("delta", "第二段"), now: 12)
        XCTAssertEqual(projection.messages.map(\.content), ["第一段", "第二段"])
        XCTAssertTrue(projection.messages.allSatisfy(\.isStreaming))
        projection.apply(event("turn_end"), now: 13)
        XCTAssertTrue(projection.messages.allSatisfy { !$0.isStreaming })
    }

    func testOnlyBothResumeFlagsKeepBubbleOpen() {
        XCTAssertEqual(answers([
            event("delta", "A"), event("stream_end", nil, ["resuming": .bool(true)]),
            event("delta", "B"), event("turn_end")
        ]), ["A", "B"])
    }

    func testReasoningPlaceholderIsAdoptedAndToolBreaksAnswer() {
        var projection = StreamProjection()
        projection.apply(event("reasoning_delta", "想一想"))
        projection.apply(event("reasoning_end"))
        projection.apply(event("delta", "看看天气"))
        XCTAssertEqual(projection.messages.count, 1)
        projection.apply(event("message", "搜索", ["kind": .string("tool_hint"), "tool_events": .array([json(#"{"name":"web_search","call_id":"s1","phase":"start"}"#)])]))
        projection.apply(event("delta", "晴"))
        XCTAssertEqual(projection.messages.filter { $0.role == "assistant" }.map(\.content), ["看看天气", "晴"])
        XCTAssertEqual(projection.messages.filter { $0.kind == "trace" }.count, 1)
    }

    func testMessageSnapshotReplacesOpenDraftAndMediaSuppressesTrailingDelta() {
        let image = json(#"{"url":"/api/media/photo.jpg?sig=x","name":"photo.jpg"}"#)
        var projection = StreamProjection()
        projection.apply(event("delta", "草稿"))
        projection.apply(event("message", "好了", ["media_urls": .array([image])]))
        projection.apply(event("delta", "重复"))
        XCTAssertEqual(projection.messages.count, 1)
        XCTAssertEqual(projection.messages[0].content, "好了")
        XCTAssertEqual(projection.messages[0].media.first?.kind, "image")
        projection.apply(event("turn_end"))
        projection.apply(event("delta", "新的回复", ["turn_id": .string("t2")]))
        XCTAssertEqual(projection.messages.filter { $0.role == "assistant" }.map(\.content), ["好了", "新的回复"])
    }

    func testSideChannelDoesNotReplaceCurrentAnswer() {
        var projection = StreamProjection()
        projection.apply(event("delta", "正在回复"))
        projection.apply(event("message", "提醒喝水", ["source": json(#"{"kind":"cron","label":"喝水"}"#)]), sideChannel: true)
        projection.apply(event("delta", "，继续"))
        XCTAssertEqual(projection.messages.map(\.content), ["正在回复，继续", "提醒喝水"])
    }

    func testScheduledMediaAutomaticallyLeavesDraftOpenWithoutSuppressingNextDelta() {
        var projection = StreamProjection()
        projection.apply(event("delta", "答案"))
        let reminder = event("message", "提醒", ["source": json(#"{"kind":"cron","label":"喝水"}"#), "media_urls": .array([json(#"{"url":"/photo.jpg"}"#)])])
        projection.apply(reminder)
        projection.apply(event("delta", "继续"))
        XCTAssertEqual(projection.messages.map(\.content), ["答案继续", "提醒"])
        XCTAssertEqual(projection.messages[1].media.count, 1)
        projection.apply(event("message", "记住了", ["source": json(#"{"kind":"notice"}"#)]))
        projection.apply(event("delta", "。"))
        XCTAssertEqual(projection.messages.first?.content, "答案继续。")
    }

    func testEchoDeduplicationAndReplayIDs() {
        let messages = StreamProjection.replay([event("user_message", "你好"), event("user_message", "你好"), event("delta", "嗨"), event("turn_end")])
        XCTAssertEqual(messages.map(\.id), ["history-0", "history-2"])
        XCTAssertFalse(messages.contains(where: \.isStreaming))
        XCTAssertEqual(messages.first?.deliveryStatus, nil)
    }

    func testClosedTurnIDDoesNotMergeIndependentReplay() {
        let messages = StreamProjection.replay([event("delta", "一"), event("turn_end"), event("delta", "二"), event("turn_end")])
        XCTAssertEqual(messages.map(\.content), ["一", "二"])
        XCTAssertNotEqual(messages[0].turnId, messages[1].turnId)
    }

    func testReplyParagraphsListsCodeAndLongDraft() {
        XCTAssertEqual(ReplyContent.chunkReply("你好！\n\n今天想做点什么？"), ["你好！", "今天想做点什么？"])
        XCTAssertEqual(ReplyContent.chunkReply("步骤如下：\n\n1. 打开设置\n\n2. 选择模型\n\n```bash\necho a\n\necho b\n```"),
                       ["步骤如下：", "1. 打开设置\n\n2. 选择模型", "```bash\necho a\n\necho b\n```"])
        XCTAssertEqual(ReplyContent.chunkReply("## 标题\n\n正文"), ["## 标题\n\n正文"])
        XCTAssertEqual(ReplyContent.chunkReply(["这是草稿：", "你好，", "我想报名。", "谢谢，", "Jordan", "可以发送吗？"].joined(separator: "\n\n")),
                       ["这是草稿：", "你好，\n\n我想报名。\n\n谢谢，\n\nJordan", "可以发送吗？"])
    }

    func testGeneratedMediaLinksAndChinesePunctuation() {
        let video = "https://cdn.example.com/out/abc.mp4?Expires=1700000000&Signature=x%2By"
        let image = "https://cdn.example.com/img/cat.jpeg"
        let text = "视频做好了：\n\n\(video)\n\n[看图](\(image))。\n\n还要再改吗？"
        XCTAssertEqual(ReplyContent.chunkReply(text), ["视频做好了：", "还要再改吗？"])
        XCTAssertEqual(ReplyContent.linkedMedia(text).map(\.kind), ["video", "image"])
        XCTAssertEqual(ReplyContent.textWithoutCards(video), video)
        XCTAssertEqual(ReplyContent.linkedMedia("我放这里了\(video)，有效期一天。参考 https://example.com/page 和 ![预览](\(image))").map(\.url), [video])
    }

    func testTextCardsUnknownJSONAndPendingStayInOrder() {
        let text = "先看天气：\n\n```card\n{\"type\":\"weather\",\"place\":\"台北\",\"temp\":\"23°\"}\n```\n\n```card\n{\"type\":\"nope\"}\n```\n\n要出门吗？\n\n```card\n{\"type\":\"buttons\",\"items\":[\"要\",\"不了\"]}\n```"
        let blocks = ReplyContent.blocks(text)
        XCTAssertEqual(blocks.count, 4)
        XCTAssertEqual(ReplyContent.textWithoutCards(text), "先看天气：\n\n要出门吗？")
        XCTAssertEqual(ReplyContent.blocks("好的：\n\n```card\n{\"type\":\"choi", streaming: true), [.bubble("好的："), .pending(nil)])
        XCTAssertEqual(ReplyContent.blocks("好的：\n\n```card\n{\"type\":\"choi"), [.bubble("好的：")])
        XCTAssertEqual(ReplyContent.blocks("```json\n{\"a\":1}\n```"), [.bubble("```json\n{\"a\":1}\n```")])
        XCTAssertEqual(ReplyContent.blocks("```ui\n<div class=", streaming: true), [.pending("ui")])
    }

    func testEveryNativeCardAndParserBounds() {
        let examples = json(#"[{"type":"buttons","items":["好"]},{"type":"choices","multiple":true,"options":["日料","火锅"]},{"type":"pictures","items":[{"url":"/signed/1","label":"1"}]},{"type":"weather","place":"台北","temp":23},{"type":"timeline","items":[{"title":"抵达"}]},{"type":"places","items":[{"name":"公园"}]},{"type":"compare","options":["A","B"],"rows":[{"label":"价格","values":[1,2]}]},{"type":"checklist","items":["伞"]},{"type":"table","columns":["项目"],"rows":[["职责"]]},{"type":"form","fields":[{"label":"姓名","kind":"text"}]}]"#).array
        XCTAssertEqual(examples.compactMap(ReplyContent.parseCard).count, 10)
        XCTAssertNil(ReplyContent.parseCard(json(#"{"type":"pictures","items":[{"url":"javascript:alert(1)","label":"1"}]}"#)))
        XCTAssertNil(ReplyContent.parseCard(json(#"{"type":"widget","title":"x","url":"https://evil.example"}"#)))
        XCTAssertNotNil(ReplyContent.parseCard(json(#"{"type":"widget","title":"图","url":"/api/webui/companion/widgets/a/b"}"#)))
        XCTAssertNil(ReplyContent.parseCard(json(#"{"type":"form","fields":[{"label":"口味","kind":"chips"}]}"#)))
        XCTAssertNil(ReplyContent.parseCard(json(#"{"type":"connect","service":"https://evil.example"}"#)))
        let many = JSONValue.object(["type": .string("buttons"), "items": .array((0..<20).map { .string(String($0)) })])
        XCTAssertEqual(ReplyContent.parseCard(many)?.options.count, 6)
        XCTAssertNil(ReplyContent.parseCard(json(#"{"type":"buttons","items":[23]}"#)))
    }

    func testFormSubmissionUsesLabelsUnitsAndMultiSeparator() throws {
        let card = try XCTUnwrap(ReplyContent.parseCard(json(#"{"type":"form","title":"订餐厅","fields":[{"name":"people","label":"人数","kind":"chips","options":["2","4"],"required":true},{"name":"budget","label":"人均","kind":"slider","min":100,"max":1000,"unit":"元","value":300}]}"#)))
        XCTAssertEqual(ReplyContent.formMessage(card: card, values: ["people": .string("4"), "budget": .number(300)]), "订餐厅\n人数：4\n人均：300元")
        let multi = try XCTUnwrap(ReplyContent.parseCard(json(#"{"type":"form","fields":[{"name":"f","label":"口味","kind":"multi","options":["日料","火锅"]}]}"#)))
        XCTAssertEqual(ReplyContent.formMessage(card: multi, values: ["f": .array([.string("日料"), .string("火锅")])]), "我填好了\n口味：日料、火锅")
    }

    func testQuotesRoundTripBlankLinesAndCommandsSkipQuote() {
        let formatted = UserQuote.format(content: "  回复  ", quotedContext: "原文\r\n\r\n第二段")
        XCTAssertEqual(formatted, "> [!QUOTE]\n> 原文\n>\n> 第二段\n\n回复")
        let parsed = UserQuote.parse(formatted)
        XCTAssertEqual(parsed.quote, "原文\n\n第二段")
        XCTAssertEqual(parsed.content, "回复")
        XCTAssertEqual(UserQuote.format(content: "/stop", quotedContext: "原文"), "/stop")
        XCTAssertNil(UserQuote.parse("> [!QUOTE]\n>\n\n正文").quote)
        XCTAssertNil(UserQuote.parse("> [!QUOTE]\nnot quoted\n\n正文").quote)
    }

    func testActivityLabelsAndLatestRunningTool() {
        XCTAssertEqual(Presentation.toolActivity("generate_image"), Activity(kind: "image", label: "正在画图"))
        XCTAssertEqual(Presentation.toolActivity("mcp_browser_browser_click").label, "正在点网页")
        XCTAssertEqual(Presentation.toolActivity("mcp_icloud-calendar_delete_event").label, "正在准备删除日程")
        XCTAssertEqual(Presentation.toolActivity("mcp_mail_send").label, "正在使用 mail")
        let user = ChatMessage(role: "user", content: "查天气")
        var trace = ChatMessage(role: "tool", content: "")
        trace.kind = "trace"
        trace.toolEvents = [json(#"{"name":"web_search","phase":"end"}"#), json(#"{"name":"mcp_mail_send","phase":"start"}"#)]
        XCTAssertNil(Presentation.currentActivity(messages: [user], streaming: false))
        XCTAssertEqual(Presentation.currentActivity(messages: [user, trace], streaming: true)?.label, "正在使用 mail")
        var reply = ChatMessage(role: "assistant", content: "今天")
        reply.isStreaming = true
        XCTAssertEqual(Presentation.currentActivity(messages: [user, reply], streaming: true)?.kind, "typing")
        XCTAssertEqual(Presentation.currentActivity(messages: [user], streaming: true)?.kind, "thinking")
    }

    func testAnswerableCardsIgnoreScheduledReplyButCloseAfterUser() {
        let question = ChatMessage(id: "a", role: "assistant", content: "哪天？")
        var scheduled = ChatMessage(id: "s", role: "assistant", content: "喝水")
        scheduled.source = json(#"{"kind":"cron","label":"喝水"}"#)
        XCTAssertEqual(Presentation.answerableReplyID(messages: [question, scheduled]), "a")
        XCTAssertNil(Presentation.answerableReplyID(messages: [question, scheduled, ChatMessage(role: "user", content: "周六")]))
        var notice = scheduled
        notice.source = json(#"{"kind":"notice"}"#)
        XCTAssertTrue(Presentation.isVisible(notice))
        XCTAssertEqual(Presentation.answerableReplyID(messages: [question, notice]), "a")
        var narration = question
        narration.activityKind = "model"
        XCTAssertFalse(Presentation.isVisible(narration))
    }

    func testAvatarStateSelectsOwnClipAndFallbackStill() {
        let source = json(#"{"image_url":"idle.png","working_url":"work.png","happy_url":"happy.png","motions":{"idle":"idle.webp","thinking":"think.webp","working":"work.webp","waiting":"wait.webp"}}"#)
        XCTAssertEqual(AvatarChoice.picture(source: source, animation: "idle"), AvatarPicture(clip: "idle.webp", still: "idle.png"))
        XCTAssertEqual(AvatarChoice.picture(source: source, animation: "review"), AvatarPicture(clip: "think.webp", still: "work.png"))
        XCTAssertEqual(AvatarChoice.picture(source: source, animation: "drawing"), AvatarPicture(clip: "work.webp", still: "work.png"))
        XCTAssertEqual(AvatarChoice.picture(source: source, animation: "waiting"), AvatarPicture(clip: "wait.webp", still: "idle.png"))
        XCTAssertEqual(AvatarChoice.picture(source: source, animation: "waving"), AvatarPicture(clip: nil, still: "happy.png"))
        let noThinking = json(#"{"image_url":"still.png","motions":{"working":"work.webp"}}"#)
        XCTAssertEqual(AvatarChoice.picture(source: noThinking, animation: "review").clip, "work.webp")
        XCTAssertNil(AvatarChoice.picture(source: noThinking, animation: "waiting").clip)
    }

    func testJSONRoundTripAndSignedMediaKind() throws {
        let original = json(#"{"s":"台北","b":true,"n":23,"a":[null,{}]}"#)
        XCTAssertEqual(try JSONValue.parse(JSONEncoder().encode(original)), original)
        XCTAssertEqual(MediaAttachment(url: "/api/media/file?sig=x", name: "movie.mov").kind, "video")
        XCTAssertEqual(MediaAttachment(url: "/api/media/photo.webp?sig=x").kind, "image")
        XCTAssertEqual(MediaAttachment(url: "/api/media/file?sig=x", name: "report.pdf").kind, "file")
        XCTAssertEqual(MediaAttachment(url: "/api/media/opaque.bin?sig=x", kind: "image").kind, "image")
    }
}
