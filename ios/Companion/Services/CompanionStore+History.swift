import CryptoKit
import Foundation

@MainActor
extension CompanionStore {
    func changeChat(_ id: String, forceHistory: Bool = false) async {
        guard !id.isEmpty else { return }
        currentChatID = id
        approvals = []
        knownChats.insert(id)
        publishCurrent()
        guard connected else { return }
        try? await write(.object(["type": .string("attach"), "chat_id": .string(id)]))
        // A socket that has kept receiving this chat already has the complete live prefix.
        // The plugin persists only the final answer, so an in-progress HTTP snapshot is poorer.
        if forceHistory || needsHistoryRefresh.contains(id) || projections[id] == nil || !runningChats.contains(id) { await hydrate(id) }
        if currentChatID == id { await refreshApprovals() }
    }

    func loadOlder() async {
        let id = currentChatID
        guard !id.isEmpty, !loadingChats.contains(id) else { return }
        let loaded = projections[id]?.messages.count ?? 0
        let visible = visibleCounts[id] ?? 60
        if visible < loaded {
            visibleCounts[id] = visible + 60
            publishCurrent()
        } else if connected && serverHasOlder.contains(id) {
            await hydrate(id, older: true)
        }
    }

    /// Messages from a chat's events (each event gets a stable id from its content).
    func project(_ items: [JSONValue]) -> StreamProjection {
        var projection = StreamProjection()
        var occurrences: [String: Int] = [:]
        let stableEncoder = JSONEncoder()
        stableEncoder.outputFormatting = [.sortedKeys]
        for item in items {
            var fields = item.object
            if fields["projection_id"]?.nonemptyString == nil, let bytes = try? stableEncoder.encode(item) {
                let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
                let occurrence = occurrences[digest, default: 0]
                occurrences[digest] = occurrence + 1
                fields["projection_id"] = .string("ios-\(digest)-\(occurrence)")
            }
            projection.apply(.object(fields), persisted: true)
        }
        return projection
    }

    /// The chat as this iPhone kept it last time (read off the main thread).
    func keptEvents(_ id: String) async -> [JSONValue]? {
        let file = LocalStore.url("chats", server: serverAddress, name: id)
        return await Task.detached(priority: .userInitiated) {
            guard let data = LocalStore.read(file), let value = try? JSONValue.parse(data) else { return nil as [JSONValue]? }
            let events = value.array
            return events.isEmpty ? nil : events
        }.value
    }

    /// Keeps the chat's events on this iPhone (written off the main thread).
    func keep(_ events: [JSONValue], for id: String) {
        let file = LocalStore.url("chats", server: serverAddress, name: id)
        Task.detached(priority: .utility) {
            guard let data = try? JSONEncoder().encode(JSONValue.array(events)) else { return }
            LocalStore.write(data, to: file)
        }
    }

    @discardableResult
    func hydrate(_ id: String, older: Bool = false) async -> Bool {
        guard let api, !id.isEmpty, !loadingChats.contains(id) else { return false }
        let attempt = generation
        loadingChats.insert(id)
        if currentChatID == id { loadingHistory = true }
        duringHistory[id] = []
        defer {
            if attempt == generation {
                loadingChats.remove(id)
                duringHistory.removeValue(forKey: id)
                if currentChatID == id { loadingHistory = false; publishCurrent() }
            }
        }
        // The copy kept on this iPhone shows at once (opening the app, switching chats); the server is
        // then asked only for what came after it.
        if !older, savedEvents[id] == nil, let kept = await keptEvents(id) {
            guard attempt == generation else { return false }
            savedEvents[id] = kept
            if projections[id] == nil {
                historyEvents[id] = kept
                var projection = project(kept)
                for queued in outbox where queued.chatID == id { projection.messages.append(optimistic(queued)) }
                projections[id] = projection
                if currentChatID == id { publishCurrent() }
            }
        }
        var query = [URLQueryItem(name: "limit", value: "80"), URLQueryItem(name: "direction", value: "latest")]
        if older, let before = beforeCursors[id] { query.append(URLQueryItem(name: "before", value: before)) }
        let base = older ? [] : (savedEvents[id] ?? [])
        if let last = base.last?["created_at_ms"].doubleValue {
            query.append(URLQueryItem(name: "known", value: String(base.count)))
            query.append(URLQueryItem(name: "last", value: String(Int64(last))))
        }
        do {
            let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
            let key = ("websocket:" + id).addingPercentEncoding(withAllowedCharacters: allowed) ?? id
            let payload = try await api.get("/api/sessions/\(key)/webui-thread", query: query)
            guard attempt == generation else { return false }
            let events = payload["incremental"].boolValue ? base + payload["events"].array : payload["events"].array
            if !older {
                savedEvents[id] = events
                keep(events, for: id)
            }
            let completed = Set(payload["completed_turn_ids"].array.compactMap(\.string))
            let buffered = (duringHistory[id] ?? []).filter { event in
                if let turn = event["turn_id"].string, completed.contains(turn) { return false }
                return !events.contains(event)
            }
            let combined = older ? events + (historyEvents[id] ?? []) + buffered : events + buffered
            historyEvents[id] = combined
            var projection = project(combined)
            let canonicalTurns = Set(events.filter { $0["event"].stringValue == "user_message" }.compactMap { $0["turn_id"].string })
            outbox.removeAll { $0.chatID == id && canonicalTurns.contains($0.id) }
            let projectedTurns = Set(projection.messages.filter { $0.role == "user" }.compactMap(\.turnId))
            let pendingLocal = (projections[id]?.messages ?? []).filter { message in
                message.id.hasPrefix("local-") && message.role == "user" && !projectedTurns.contains(message.turnId ?? "")
                && !outbox.contains(where: { $0.id == message.turnId })
            }
            projection.messages.append(contentsOf: pendingLocal)
            for queued in outbox where queued.chatID == id {
                projection.messages.append(optimistic(queued))
            }
            projections[id] = projection
            persistOutbox()
            if older { visibleCounts[id, default: 60] += 60 }
            if let cursor = payload["page"]["before_cursor"].string { beforeCursors[id] = cursor }
            if payload["page"]["has_more_before"].boolValue { serverHasOlder.insert(id) }
            else { serverHasOlder.remove(id) }
            if !older {
                // Turns that ended while this loaded count as ended.
                let finished = completed.union(buffered.filter { $0["event"].stringValue == "turn_end" }.compactMap { $0["turn_id"].nonemptyString })
                let working: Bool
                if payload.object["active_turn_id"] != nil {
                    // The server says what is running (a turn cut off by a restart never ends in the log).
                    let active = payload["active_turn_id"].stringValue
                    working = !active.isEmpty && !finished.contains(active)
                } else {
                    let newestTurn = (events + buffered).last(where: { $0["turn_id"].nonemptyString != nil })?["turn_id"].string
                    working = payload["has_pending_tool_calls"].boolValue || newestTurn.map { !finished.contains($0) } == true
                }
                // A turn that started while this loaded is running too.
                let started = buffered.contains { $0["event"].stringValue == "user_message" && $0["starts_turn"].boolValue
                    && !finished.contains($0["turn_id"].stringValue) }
                if working || started {
                    runningChats.insert(id)
                    reconcileAtTurnEnd.insert(id)
                } else {
                    runningChats.remove(id)
                    reconcileAtTurnEnd.remove(id)
                }
            }
            needsHistoryRefresh.remove(id)
            return true
        } catch let error as ServiceError {
            guard attempt == generation else { return false }
            if case .http(404) = error { projections[id] = projections[id] ?? StreamProjection() }
            else if id == currentChatID { show(error) }
        } catch { if attempt == generation && id == currentChatID { show(error) } }
        return false
    }

}
