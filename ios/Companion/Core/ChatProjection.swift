import Foundation

/// Ports the web client's answer cursor. A stream boundary closes one segment;
/// only a resuming + merge_next boundary keeps that bubble open for later text.
struct StreamProjection: Sendable {
    var messages: [ChatMessage]
    private var activeAssistantID: String?
    private var closedAssistantIDs: Set<String> = []
    private var closedTurnIDs: Set<String> = []
    private var turnAliases: [String: String] = [:]
    private var mergeReasoning = false
    private var suppressUntilTurnEnd = false

    init(messages: [ChatMessage] = []) { self.messages = messages }

    mutating func resetCursor() {
        activeAssistantID = nil
        closedAssistantIDs.removeAll()
        mergeReasoning = false
        suppressUntilTurnEnd = false
    }

    mutating func apply(_ event: JSONValue, now: Double = Date().timeIntervalSince1970 * 1_000,
                        persisted: Bool = false, sideChannel: Bool = false) {
        let name = event["event"].stringValue
        let text = event["text"].stringValue
        let createdAt = event["created_at_ms"].doubleValue ?? now
        let id = event["projection_id"].nonemptyString ?? UUID().uuidString
        let rawTurn = event["turn_id"].nonemptyString
        var turn = rawTurn
        if let rawTurn, closedTurnIDs.contains(rawTurn) {
            if turnAliases[rawTurn] == nil {
                turnAliases[rawTurn] = rawTurn + ":replay:" + (event["projection_id"].nonemptyString ?? String(createdAt))
            }
            turn = turnAliases[rawTurn]
        }
        let source = event["source"] == .null ? nil : event["source"]

        switch name {
        case "user_message":
            let sessionID = event["provenance"]["session_message"]["message_id"].nonemptyString
            if let sessionID, messages.contains(where: { $0.sessionMessageID == sessionID }) { return }
            if sessionID == nil, let rawTurn, messages.contains(where: { $0.role == "user" && $0.turnId == rawTurn }) { return }
            if let activeAssistantID, let index = messages.firstIndex(where: { $0.id == activeAssistantID }) {
                messages[index].isStreaming = false
            }
            closeReasoning()
            resetCursor()
            var row = ChatMessage(id: id, role: "user", content: text, createdAt: createdAt)
            Self.stamp(&row, event: event, turn: turn, phase: "user")
            row.media = event["media_urls"].array.map(MediaAttachment.init(json:))
            row.sessionMessageID = sessionID
            row.deliveryStatus = persisted ? nil : "accepted"
            messages.append(row)

        case "delta":
            guard !suppressUntilTurnEnd, !text.isEmpty else { return }
            let index = activeIndex(turn: turn) ?? placeholderIndex(turn: turn) ?? streamingIndex(turn: turn)
                ?? appendAssistant(id: id, createdAt: createdAt)
            messages[index].content += text
            messages[index].isStreaming = true
            Self.stamp(&messages[index], event: event, turn: turn, phase: "answer")
            if let source { messages[index].source = source }
            activeAssistantID = messages[index].id
            closedAssistantIDs.remove(messages[index].id)
            mergeReasoning = false

        case "stream_end":
            guard !suppressUntilTurnEnd else { return }
            var target = activeIndex(turn: turn) ?? streamingIndex(turn: turn)
            if let replacement = event["text"].string {
                if target == nil { target = appendAssistant(id: id, createdAt: createdAt) }
                if let target {
                    messages[target].content = replacement
                    messages[target].isStreaming = true
                }
            }
            if let target {
                Self.stamp(&messages[target], event: event, turn: turn, phase: "answer")
                if let source { messages[target].source = source }
                activeAssistantID = messages[target].id
            }
            let mergeNext = event["resuming"].boolValue && event["merge_next"].boolValue
            mergeReasoning = mergeNext && activeAssistantID != nil
            if !mergeNext { closeAnswer() }

        case "reasoning_delta", "reasoning_end":
            guard !suppressUntilTurnEnd else { return }
            if !text.isEmpty { appendReasoning(text, event: event, turn: turn, id: id, createdAt: createdAt) }
            if name == "reasoning_end" { closeReasoning() }

        case "message":
            let kind = event["kind"].stringValue
            if suppressUntilTurnEnd && ["reasoning", "tool_hint", "progress"].contains(kind) { return }
            if kind == "reasoning" {
                if !text.isEmpty { appendReasoning(text, event: event, turn: turn, id: id, createdAt: createdAt) }
                closeReasoning()
                return
            }
            if kind == "tool_hint" || kind == "progress" {
                closeAnswer()
                projectToolEvents(event, text: text, turn: turn, id: id, createdAt: createdAt)
                return
            }
            let mediaValue = event["media_urls"] == .null ? event["media"] : event["media_urls"]
            let media = mediaValue.array.map(MediaAttachment.init(json:))
            // Scheduled jobs, helpers and notices can arrive during another turn.
            // Their source marks independent delivery, so they never consume its draft.
            let independent = sideChannel || source != nil
            var row = ChatMessage(id: id, role: "assistant", content: text, createdAt: createdAt)
            Self.stamp(&row, event: event, turn: turn, phase: "answer")
            row.source = source
            row.media = media
            if !independent {
                let active = activeAssistantID
                closeAnswer()
                if let active { messages.removeAll(where: { $0.id == active }) }
            }
            // A reasoning-only placeholder adopts its final answer instead of duplicating a row.
            if let index = messages.indices.last, messages[index].role == "assistant",
               messages[index].kind != "trace", messages[index].content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               messages[index].reasoning != nil, !messages[index].reasoningStreaming, matches(messages[index], turn: turn) {
                row.id = messages[index].id
                row.reasoning = messages[index].reasoning
                messages[index] = row
            } else { messages.append(row) }
            if !independent && !media.isEmpty { suppressUntilTurnEnd = true }

        case "file_edit":
            closeAnswer()
            // File edits are header activity only in the companion chat.
            let events = event["edits"].array.map { edit -> JSONValue in
                var fields = edit.object
                fields["name"] = edit["tool"]
                fields["phase"] = .string(edit["phase"].string ?? (edit["status"].stringValue == "editing" ? "start" : "end"))
                return .object(fields)
            }
            projectToolEvents(.object(["tool_events": .array(events)]), text: "", turn: turn, id: id, createdAt: createdAt)

        case "turn_end":
            suppressUntilTurnEnd = false
            for index in messages.indices {
                messages[index].isStreaming = false
                messages[index].reasoningStreaming = false
            }
            activeAssistantID = nil
            closedAssistantIDs.removeAll()
            mergeReasoning = false
            if let rawTurn {
                if turnAliases.removeValue(forKey: rawTurn) == nil { closedTurnIDs.insert(rawTurn) }
            }

        case "context_compaction":
            closeAnswer()
        default:
            break
        }
    }

    private func matches(_ message: ChatMessage, turn: String?) -> Bool {
        turn == nil || message.turnId == nil || message.turnId == turn
    }

    private mutating func activeIndex(turn: String?) -> Int? {
        guard let activeAssistantID,
              let index = messages.firstIndex(where: { $0.id == activeAssistantID }),
              messages[index].role == "assistant", messages[index].kind != "trace",
              messages[index].isStreaming, matches(messages[index], turn: turn) else {
            self.activeAssistantID = nil
            mergeReasoning = false
            return nil
        }
        return index
    }

    private func placeholderIndex(turn: String?) -> Int? {
        guard let index = messages.indices.last else { return nil }
        let row = messages[index]
        return row.role == "assistant" && row.kind != "trace" && row.content.isEmpty && row.isStreaming && matches(row, turn: turn) ? index : nil
    }

    private func streamingIndex(turn: String?) -> Int? {
        for index in messages.indices.reversed() {
            let row = messages[index]
            if row.kind == "trace" { continue }
            if row.role == "assistant" && row.isStreaming && !closedAssistantIDs.contains(row.id) && matches(row, turn: turn) { return index }
            if row.role == "user" { break }
        }
        return nil
    }

    private mutating func appendAssistant(id: String, createdAt: Double) -> Int {
        var row = ChatMessage(id: id, role: "assistant", content: "", createdAt: createdAt)
        row.isStreaming = true
        messages.append(row)
        return messages.count - 1
    }

    private mutating func closeAnswer() {
        if let activeAssistantID { closedAssistantIDs.insert(activeAssistantID) }
        activeAssistantID = nil
        mergeReasoning = false
    }

    private mutating func closeReasoning() {
        if let index = messages.lastIndex(where: { $0.reasoningStreaming }) { messages[index].reasoningStreaming = false }
    }

    private static func stamp(_ row: inout ChatMessage, event: JSONValue, turn: String?, phase: String) {
        if let turn { row.turnId = turn }
        row.turnPhase = event["turn_phase"].string ?? phase
        row.turnSeq = event["turn_seq"].intValue ?? row.turnSeq
        row.activityKind = event["activity_kind"].string ?? event["activityKind"].string ?? row.activityKind
    }

    private mutating func appendReasoning(_ text: String, event: JSONValue, turn: String?, id: String, createdAt: Double) {
        if mergeReasoning, let index = activeIndex(turn: turn) {
            let separator = messages[index].reasoning != nil && !messages[index].reasoningStreaming ? "\n\n" : ""
            messages[index].reasoning = (messages[index].reasoning ?? "") + separator + text
            messages[index].reasoningStreaming = true
            return
        }
        closeAnswer()
        var target: Int?
        for index in messages.indices.reversed() {
            let row = messages[index]
            if row.role == "user" || row.kind == "trace" { break }
            if row.role != "assistant" { continue }
            if matches(row, turn: turn) && row.content.isEmpty && (row.reasoningStreaming || (row.isStreaming && row.reasoning == nil)) { target = index }
            break
        }
        let index = target ?? appendAssistant(id: id, createdAt: createdAt)
        messages[index].reasoning = (messages[index].reasoning ?? "") + text
        messages[index].reasoningStreaming = true
        Self.stamp(&messages[index], event: event, turn: turn, phase: "reasoning")
    }

    private mutating func projectToolEvents(_ event: JSONValue, text: String, turn: String?, id: String, createdAt: Double) {
        let events = event["tool_events"].array
        guard !events.isEmpty || !text.isEmpty else { return }
        var index: Int
        if let last = messages.indices.last, messages[last].kind == "trace", matches(messages[last], turn: turn) { index = last }
        else {
            var row = ChatMessage(id: id, role: "tool", content: "", createdAt: createdAt)
            row.kind = "trace"
            row.turnId = turn
            messages.append(row)
            index = messages.count - 1
        }
        messages[index].content = text
        for incoming in events {
            let callID = incoming["call_id"].stringValue
            if !callID.isEmpty, let old = messages[index].toolEvents.firstIndex(where: { $0["call_id"].stringValue == callID }) {
                var merged = messages[index].toolEvents[old].object
                merged.merge(incoming.object) { _, newest in newest }
                messages[index].toolEvents[old] = .object(merged)
            } else { messages[index].toolEvents.append(incoming) }
        }
    }

    static func replay(_ events: [JSONValue]) -> [ChatMessage] {
        var projection = StreamProjection()
        for (index, event) in events.enumerated() {
            var fields = event.object
            if event["projection_id"].string == nil { fields["projection_id"] = .string("history-\(index)") }
            projection.apply(.object(fields), now: event["created_at_ms"].doubleValue ?? Double(index), persisted: true)
        }
        for index in projection.messages.indices {
            projection.messages[index].isStreaming = false
            projection.messages[index].reasoningStreaming = false
        }
        return projection.messages
    }
}
