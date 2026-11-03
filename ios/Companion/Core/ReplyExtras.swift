import Foundation

/// A reply as it is read out (朗读): its words only — no cards, links' addresses, Markdown marks or
/// table rules, which a voice would spell out.
enum SpokenText {
    static func of(_ reply: String) -> String {
        var text = ReplyContent.textWithoutCards(reply)
        let rules: [(String, String)] = [
            (#"(?s)```.*?```"#, "（代码略）"),                     // code is not read out
            (#"!\[[^\]]*\]\([^)]*\)"#, ""),                       // pictures
            (#"\[([^\]]+)\]\([^)]*\)"#, "$1"),                    // links: their words
            (#"https?://\S+"#, ""),                               // bare addresses
            (#"(?m)^\s*\|?\s*:?-{3,}.*$"#, ""),                   // table rules
            (#"\|"#, "，"),                                       // table cells
            (#"(?m)^[ \t]*，[ \t]*|[ \t]*，[ \t]*$"#, ""),             // a row's outer edges
            (#"(?m)^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+"#, ""),    // headings, quotes, list marks
            (#"(\*\*|__|~~|`|\*)"#, ""),                          // emphasis and code marks
            (#"[ \t]+"#, " "),
            (#"\n{2,}"#, "\n"),
        ]
        for (pattern, replacement) in rules {
            text = text.replacingOccurrences(of: pattern, with: replacement, options: .regularExpression)
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Chinese is read with a Chinese voice; a reply with none in it, with an English one.
    static func isChinese(_ text: String) -> Bool {
        text.unicodeScalars.contains { (0x4E00...0x9FFF).contains($0.value) }
    }
}

/// What it saved about the user in a turn (its memory tool, target ``user``), shown under the reply
/// as ChatGPT shows "Memory updated": 已记住：不吃香菜.
enum MemoryNotes {
    static func saved(_ event: JSONValue) -> [String] {
        guard event["name"].stringValue == "memory", event["phase"].stringValue == "end",
              event["error"].nonemptyString == nil, event["arguments"]["target"].stringValue == "user" else { return [] }
        let result = event["result"].stringValue
        // Refused (full, or nothing to patch): nothing was saved.
        if result.contains("\"success\": false") || result.contains("\"success\":false") { return [] }
        let arguments = event["arguments"]
        let operations = arguments["operations"].array.isEmpty ? [arguments] : arguments["operations"].array
        return operations.compactMap { (operation: JSONValue) -> String? in
            guard ["add", "patch"].contains(operation["action"].stringValue) else { return nil }
            let content = (operation["content"].nonemptyString ?? operation["new_text"].nonemptyString ?? "")
                .replacingOccurrences(of: #"^【[^】]*】\s*"#, with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !content.isEmpty else { return nil }
            return content.count > 40 ? String(content.prefix(40)) + "…" : content
        }
    }

    /// Each turn's saved notes, by turn.
    static func byTurn(_ messages: [ChatMessage]) -> [String: [String]] {
        var notes: [String: [String]] = [:]
        for message in messages {
            guard let turn = message.turnId, !message.toolEvents.isEmpty else { continue }
            for event in message.toolEvents {
                for note in saved(event) where !(notes[turn] ?? []).contains(note) { notes[turn, default: []].append(note) }
            }
        }
        return notes
    }
}
