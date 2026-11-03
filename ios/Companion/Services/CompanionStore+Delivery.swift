import CryptoKit
import Foundation

struct DraftUpload: Identifiable, Codable, Equatable, Sendable {
    var id = UUID().uuidString
    var data: Data
    var mimeType: String
    var name: String

    init(data: Data, mimeType: String, name: String) {
        self.data = data
        self.mimeType = mimeType
        self.name = name
    }

    var preview: MediaAttachment {
        MediaAttachment(url: "data:\(mimeType);base64,\(data.base64EncodedString())", name: name,
                        kind: mimeType.hasPrefix("image/") ? "image" : mimeType.hasPrefix("video/") ? "video" : "file")
    }
}

struct SideChat: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let preview: String
}

struct QueuedTurn: Codable {
    let id: String
    let chatID: String
    let text: String
    let attachments: [DraftUpload]
    let createdAt: Double
    /// queued: never written; sent: delivery uncertain until ACK or canonical history.
    var state: String
}

@MainActor
extension CompanionStore {
    @discardableResult
    func send(text: String, attachments: [DraftUpload] = []) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !currentChatID.isEmpty, !trimmed.isEmpty || !attachments.isEmpty else { return false }
        guard attachments.allSatisfy({ !$0.data.isEmpty && $0.data.count <= 20 * 1024 * 1024 }) else {
            errorMessage = "单个附件不能超过 20 MB"; return false
        }
        let estimate = JSONValue.object([
            "type": .string("message"), "chat_id": .string(currentChatID), "content": .string(trimmed),
            "turn_id": .string(UUID().uuidString), "webui": .bool(true),
            "media": .array(attachments.map { .object(["reference": .string(String(repeating: "x", count: 128)), "name": .string($0.name)]) }),
        ])
        guard let estimatedBytes = try? JSONEncoder().encode(estimate), estimatedBytes.count <= frameLimit else {
            errorMessage = "消息太长，请分成几条发送"; return false
        }
        let queued = QueuedTurn(id: UUID().uuidString, chatID: currentChatID, text: trimmed,
                                attachments: attachments, createdAt: Date().timeIntervalSince1970 * 1000, state: "queued")
        outbox.append(queued)
        var projection = projections[currentChatID] ?? StreamProjection()
        projection.messages.append(optimistic(queued))
        projections[currentChatID] = projection
        persistOutbox()
        publishCurrent()
        beginFlush()
        return true
    }

    func optimistic(_ queued: QueuedTurn) -> ChatMessage {
        var message = ChatMessage(id: "local-" + queued.id, role: "user", content: queued.text, createdAt: queued.createdAt)
        message.turnId = queued.id
        message.turnPhase = "user"
        message.deliveryStatus = queued.state == "queued" ? "sending" : "failed"
        message.media = queued.attachments.map(\.preview)
        return message
    }

    func beginFlush() {
        guard connected, sessionInitialized, flushTask == nil else { return }
        let attempt = generation
        flushTask = Task { [weak self] in
            guard let self else { return }
            await self.flushOutbox()
            if self.generation == attempt { self.flushTask = nil }
        }
    }

    func flushOutbox() async {
        let attempt = generation
        while let queued = outbox.first(where: { $0.state == "queued" }) {
            guard connected, attempt == generation, let api, !Task.isCancelled else { return }
            do {
                uploading = !queued.attachments.isEmpty
                var references: [JSONValue] = []
                for attachment in queued.attachments {
                    let reference = try await api.upload(attachment, capability: uploadCapability)
                    guard attempt == generation, connected else { return }
                    references.append(reference)
                }
                uploading = false
                var fields: [String: JSONValue] = [
                    "type": .string("message"), "chat_id": .string(queued.chatID),
                    "content": .string(queued.text), "turn_id": .string(queued.id), "webui": .bool(true),
                ]
                if !references.isEmpty { fields["media"] = .array(references) }
                let frame = JSONValue.object(fields)
                guard try JSONEncoder().encode(frame).count <= frameLimit else {
                    throw ServiceError.message("消息太长，请分成几条发送")
                }
                guard let index = outbox.firstIndex(where: { $0.id == queued.id }) else { continue }
                // Persist uncertainty before writing: an app termination cannot cause a duplicate send.
                outbox[index].state = "sent"
                persistOutbox()
                try await write(frame)
                runningChats.insert(queued.chatID)
                publishCurrent()
            } catch {
                uploading = false
                if attempt == generation {
                    show(error)
                    if let service = error as? ServiceError, service.isAuthentication { connectionDropped() }
                }
                return
            }
        }
    }

    func stop() async {
        guard connected, !currentChatID.isEmpty else { return }
        do {
            try await write(.object(["type": .string("message"), "chat_id": .string(currentChatID),
                                     "content": .string("/stop"), "turn_id": .string("webui-system:" + UUID().uuidString), "webui": .bool(true)]))
        } catch { show(error) }
    }

    /// 停止任务 (from the browser view): every conversation that is working now stops where it is —
    /// the browser task may belong to a helper's chat rather than the one open.
    func stopRunning() async {
        guard connected else { return }
        let chats = runningChats.isEmpty ? [currentChatID].filter { !$0.isEmpty } : Array(runningChats)
        for chat in chats {
            do {
                try await write(.object(["type": .string("message"), "chat_id": .string(chat), "content": .string("/stop"),
                                         "turn_id": .string("webui-system:" + UUID().uuidString), "webui": .bool(true)]))
            } catch { show(error) }
        }
    }

    func createChat() async {
        do {
            let id = try await newChatID()
            await refreshSessions()
            await changeChat(id)
        } catch { show(error) }
    }

    func newChatID() async throws -> String {
        guard connected, pendingNewChat == nil else { throw ServiceError.message("请等连接完成后再新建旁聊") }
        return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<String, Error>) in
            pendingNewChat = continuation
            newChatTimeout = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: 20_000_000_000) } catch { return }
                guard let self, let pending = self.pendingNewChat else { return }
                self.pendingNewChat = nil
                pending.resume(throwing: ServiceError.message("新建旁聊超时，请重试"))
            }
            Task { [weak self] in
                do { try await self?.write(.object(["type": .string("new_chat")])) }
                catch {
                    guard let self, let pending = self.pendingNewChat else { return }
                    self.pendingNewChat = nil
                    self.newChatTimeout?.cancel()
                    pending.resume(throwing: error)
                }
            }
        }
    }

    func renameChat(id: String, title: String) async {
        do {
            _ = try await requestMutation("companion.chats.change", payload: ["id": .string(id), "action": .string("rename"), "title": .string(title)])
            await refreshSessions()
        } catch { show(error) }
    }

    func deleteChat(id: String) async {
        guard id != mainChatID else { return }
        do {
            _ = try await requestMutation("companion.chats.change", payload: ["id": .string(id), "action": .string("delete")])
            outbox.removeAll { $0.chatID == id }
            persistOutbox()
            knownChats.remove(id)
            projections.removeValue(forKey: id)
            historyEvents.removeValue(forKey: id)
            savedEvents.removeValue(forKey: id)
            LocalStore.remove(LocalStore.url("chats", server: serverAddress, name: id))
            if currentChatID == id { await changeChat(mainChatID) }
            await refreshSessions()
        } catch { show(error) }
    }

    func deleteMessage(_ message: ChatMessage) async {
        guard let turnID = message.turnId, !turnID.isEmpty else { return }
        let id = currentChatID
        if outbox.contains(where: { $0.id == turnID && $0.state == "queued" }) {
            outbox.removeAll { $0.id == turnID }
            projections[id]?.messages.removeAll { $0.turnId == turnID }
            persistOutbox()
            publishCurrent()
            return
        }
        do {
            _ = try await requestMutation("companion.chats.change", payload: [
                "id": .string(id), "action": .string("delete_message"), "turn_id": .string(turnID),
                "role": .string(message.role == "user" ? "user" : "reply"),
            ])
            outbox.removeAll { $0.chatID == id && $0.id == turnID && message.role == "user" }
            persistOutbox()
            await hydrate(id)
        } catch { show(error) }
    }

    /// 查找聊天记录 (the side menu's search): messages in every chat that say the words, newest first.
    func searchMessages(_ words: String) async throws -> [JSONValue] {
        guard let api, !api.apiToken.isEmpty else { throw ServiceError.message("连接后再试一次") }
        return try await api.get("/api/webui/companion/search", query: [URLQueryItem(name: "q", value: words)])["hits"].array
    }

    /// 重新生成: the newest answer again. The server takes the old one out and asks the question
    /// again under the same turn; the chat is read again, so it shows the question at work.
    func regenerate(_ message: ChatMessage) async {
        guard let turnID = message.turnId, !turnID.isEmpty, connected, !running else { return }
        let id = currentChatID
        do {
            _ = try await requestMutation("companion.chats.change", payload: [
                "id": .string(id), "action": .string("regenerate"), "turn_id": .string(turnID),
            ])
            await hydrate(id)
        } catch { show(error) }
    }

    /// Used only for a user-requested retry of an uncertain write, after checking the transcript.
    func retryMessage(_ message: ChatMessage) async {
        guard let turnID = message.turnId, connected else { return }
        let id = currentChatID
        guard await hydrate(id) else { return }
        guard let index = outbox.firstIndex(where: { $0.id == turnID && $0.chatID == id }) else { return }
        outbox[index].state = "queued"
        if var projection = projections[id], let row = projection.messages.firstIndex(where: { $0.turnId == turnID && $0.role == "user" }) {
            projection.messages[row].deliveryStatus = "sending"
            projections[id] = projection
        }
        persistOutbox()
        publishCurrent()
        beginFlush()
    }

    var outboxURL: URL? {
        guard !serverAddress.isEmpty,
              let folder = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
        let digest = SHA256.hash(data: Data(serverAddress.utf8)).map { String(format: "%02x", $0) }.joined()
        let directory = folder.appendingPathComponent("Outbox", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent(digest + ".json")
    }

    func loadOutbox() {
        guard let url = outboxURL, let data = try? Data(contentsOf: url),
              let items = try? JSONDecoder().decode([QueuedTurn].self, from: data) else { outbox = []; queuedCount = 0; return }
        outbox = items
        queuedCount = items.filter { $0.state == "queued" }.count
    }

    func restoreQueuedPreviews() {
        for item in outbox {
            var projection = projections[item.chatID] ?? StreamProjection()
            if !projection.messages.contains(where: { $0.turnId == item.id && $0.role == "user" }) {
                projection.messages.append(optimistic(item))
            }
            projections[item.chatID] = projection
        }
        publishCurrent()
    }

    func persistOutbox() {
        queuedCount = outbox.filter { $0.state == "queued" }.count
        guard let url = outboxURL, let data = try? JSONEncoder().encode(outbox) else { return }
        do {
            try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            var excluded = url
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? excluded.setResourceValues(values)
        } catch { errorMessage = "离线消息暂时无法保存，请保持 App 打开" }
    }

}
