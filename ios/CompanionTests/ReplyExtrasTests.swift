import XCTest
@testable import Companion

/// 朗读 reads a reply's words only; 已记住 shows what it saved about the user in a turn.
final class ReplyExtrasTests: XCTestCase {
    func testAReplyIsReadAsWordsOnly() {
        let reply = """
        ## 结论
        **谷歌**这周跌了 12.6%，详见[财报解读](https://example.com/a)。
        - 10/29 财报后跳空
        - 其余几天小跌

        | 日期 | 涨跌 |
        |---|---|
        | 10/29 | -11.7% |

        ```python
        print(1)
        ```
        原文 https://example.com/b
        """
        let spoken = SpokenText.of(reply)
        XCTAssertTrue(spoken.hasPrefix("结论\n谷歌这周跌了 12.6%，详见财报解读。"))
        XCTAssertFalse(spoken.contains("http"))
        XCTAssertFalse(spoken.contains("**") || spoken.contains("#") || spoken.contains("|") || spoken.contains("---"))
        XCTAssertTrue(spoken.contains("10/29 财报后跳空"))
        XCTAssertTrue(spoken.contains("（代码略）"))
        XCTAssertFalse(spoken.contains("print"))
        XCTAssertTrue(SpokenText.isChinese(spoken))
        XCTAssertFalse(SpokenText.isChinese("Good morning"))
    }

    private func memory(_ arguments: [String: JSONValue], phase: String = "end", result: String = #"{"success": true}"#) -> JSONValue {
        .object(["name": .string("memory"), "phase": .string(phase), "arguments": .object(arguments), "result": .string(result)])
    }

    func testWhatItSavedAboutTheUserShowsUnderTheReply() {
        let add = memory(["target": .string("user"), "action": .string("add"), "content": .string("【饮食】不吃香菜；不太能吃辣。")])
        XCTAssertEqual(MemoryNotes.saved(add), ["不吃香菜；不太能吃辣。"])
        let batch = memory(["target": .string("user"), "operations": .array([
            .object(["action": .string("patch"), "old_text": .string("住上海"), "content": .string("住在上海徐汇")]),
            .object(["action": .string("remove"), "old_text": .string("在追美股")]),
        ])])
        XCTAssertEqual(MemoryNotes.saved(batch), ["住在上海徐汇"])
        // Its own notes, a call still running, and a refused write are not shown.
        XCTAssertEqual(MemoryNotes.saved(memory(["target": .string("memory"), "action": .string("add"), "content": .string("抖音要扫码")])), [])
        XCTAssertEqual(MemoryNotes.saved(memory(["target": .string("user"), "action": .string("add"), "content": .string("x")], phase: "start")), [])
        XCTAssertEqual(MemoryNotes.saved(memory(["target": .string("user"), "action": .string("add"), "content": .string("x")],
                                                result: #"{"success": false, "error": "full"}"#)), [])

        var turn = ChatMessage(role: "assistant", content: "", createdAt: 1)
        turn.turnId = "t1"
        turn.toolEvents = [add, add]
        XCTAssertEqual(MemoryNotes.byTurn([turn]), ["t1": ["不吃香菜；不太能吃辣。"]])
    }
}
