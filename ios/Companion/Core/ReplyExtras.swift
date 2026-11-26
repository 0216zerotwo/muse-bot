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
            (#"\[\d{1,2}\]"#, ""),                                // citation references
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

/// Composer `/` presets + learned skills and `@帮手` autocomplete helpers, matching `web/src/companion/Composer.tsx`.
enum ComposerAutocomplete {
    struct SlashItem: Equatable, Identifiable, Sendable {
        let id: String
        let key: String
        let label: String
        let detail: String
        let text: String
        let isSkill: Bool
    }

    static let presets: [SlashItem] = [
        SlashItem(id: "preset:goal", key: "goal", label: "长任务", detail: "一直做到完成，中途不用你盯着", text: "/goal ", isSkill: false),
        SlashItem(id: "preset:compact", key: "compact", label: "整理上下文", detail: "聊得太长时压缩一下，更快也更省", text: "/compact", isSkill: false),
        SlashItem(id: "preset:draw", key: "draw", label: "画一张图", detail: "用生图模型画出来", text: "画一张图：", isSkill: false),
        SlashItem(id: "preset:search", key: "search", label: "联网查", detail: "搜最新的信息，给出处", text: "联网查一下：", isSkill: false),
        SlashItem(id: "preset:routine", key: "routine", label: "设个例行", detail: "定时帮你做一件事", text: "帮我设一个定时任务：", isSkill: false),
    ]

    static let homeStarters: [String] = [
        "帮我理一下今天要做的事",
        "联网查一下最近的新闻",
        "帮我设一个定时提醒",
    ]

    static func slashQuery(in text: String) -> String? {
        guard text.hasPrefix("/") else { return nil }
        let rest = text.dropFirst()
        guard rest.count <= 24, !rest.contains(where: \.isWhitespace) else { return nil }
        return String(rest)
    }

    static func matchingSlashItems(query: String, learnedSkills: [JSONValue] = []) -> [SlashItem] {
        let skillItems: [SlashItem] = learnedSkills.compactMap { skill in
            guard let name = skill["name"].nonemptyString else { return nil }
            let cleanLabel = name.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ")
            let detail = skill["description"].nonemptyString ?? "已学会的技能"
            return SlashItem(id: "skill:" + name, key: name, label: cleanLabel, detail: detail, text: "用「\(name)」技能：", isSkill: true)
        }
        let all = presets + skillItems
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return Array(all.prefix(8)) }
        return Array(all.filter { item in
            item.key.localizedCaseInsensitiveContains(trimmed)
                || item.label.localizedCaseInsensitiveContains(trimmed)
                || item.detail.localizedCaseInsensitiveContains(trimmed)
        }.prefix(8))
    }

    static func mentionQuery(in text: String) -> String? {
        guard let atIndex = text.lastIndex(of: "@") else { return nil }
        if atIndex > text.startIndex {
            let previous = text[text.index(before: atIndex)]
            guard previous.isWhitespace else { return nil }
        }
        let suffix = text[text.index(after: atIndex)...]
        guard suffix.count <= 12, !suffix.contains(where: { $0.isWhitespace || $0 == "@" }) else { return nil }
        return String(suffix)
    }

    static func matchingHelpers(query: String, helpers: [JSONValue]) -> [JSONValue] {
        let valid = helpers.filter { $0["name"].nonemptyString != nil }
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return Array(valid.prefix(6)) }
        return Array(valid.filter { helper in
            helper["name"].stringValue.localizedCaseInsensitiveContains(trimmed)
                || helper["title"].stringValue.localizedCaseInsensitiveContains(trimmed)
        }.prefix(6))
    }

    static func replacingMention(in text: String, with helperName: String) -> String {
        guard mentionQuery(in: text) != nil, let atIndex = text.lastIndex(of: "@") else { return text }
        return String(text[..<atIndex]) + "@\(helperName) "
    }
}

/// Multi-span search highlighting supporting backend `matches: [[start, end]]` character intervals.
enum SearchHighlight {
    struct Span: Equatable, Sendable {
        let start: Int
        let end: Int
    }

    /// Computes non-overlapping, clamped character spans in `text` from backend `matches` or fallback query.
    static func resolvedSpans(in text: String, matches: [[Int]]?, fallbackQuery: String) -> [Span] {
        let length = text.count
        guard length > 0 else { return [] }
        if let matches, !matches.isEmpty {
            var spans: [Span] = []
            for pair in matches {
                guard pair.count >= 2 else { continue }
                let s = max(0, min(length, pair[0]))
                let e = max(0, min(length, pair[1]))
                if e > s { spans.append(Span(start: s, end: e)) }
            }
            if !spans.isEmpty { return normalize(spans) }
        }
        let query = fallbackQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return [] }
        let tokens = query.split(whereSeparator: \.isWhitespace).map(String.init)
        var candidates: [String] = [query]
        for token in tokens where !candidates.contains(where: { $0.caseInsensitiveCompare(token) == .orderedSame }) {
            candidates.append(token)
        }
        var found: [Span] = []
        for needle in candidates {
            var searchStart = text.startIndex
            while searchStart < text.endIndex,
                  let range = text.range(of: needle, options: .caseInsensitive, range: searchStart..<text.endIndex) {
                let s = text.distance(from: text.startIndex, to: range.lowerBound)
                let e = text.distance(from: text.startIndex, to: range.upperBound)
                if e > s { found.append(Span(start: s, end: e)) }
                searchStart = range.upperBound
            }
        }
        return normalize(found)
    }

    private static func normalize(_ spans: [Span]) -> [Span] {
        let sorted = spans.sorted { lhs, rhs in
            if lhs.start != rhs.start { return lhs.start < rhs.start }
            return lhs.end > rhs.end
        }
        var merged: [Span] = []
        for span in sorted {
            if let last = merged.last, span.start <= last.end {
                merged[merged.count - 1] = Span(start: last.start, end: max(last.end, span.end))
            } else {
                merged.append(span)
            }
        }
        return merged
    }
}
