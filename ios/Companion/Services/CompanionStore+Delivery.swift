import CryptoKit
import Foundation

private actor OutboxPersistence {
    static let shared = OutboxPersistence()

    func save(turns: [QueuedTurn], to url: URL, removing staleBlobs: [URL]) -> Bool {
        guard let data = try? JSONEncoder().encode(turns) else { return false }
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            var excluded = url
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? excluded.setResourceValues(values)
            for blob in staleBlobs {
                try? FileManager.default.removeItem(at: blob)
            }
            return true
        } catch {
            return false
        }
    }

    func removeAll(outboxURL: URL?, blobURLs: [URL]) {
        if let outboxURL {
            try? FileManager.default.removeItem(at: outboxURL)
        }
        for blob in blobURLs {
            try? FileManager.default.removeItem(at: blob)
        }
    }
}

struct DraftUpload: Identifiable, Codable, Equatable, Sendable {
    var id = UUID().uuidString
    var data: Data
    var mimeType: String
    var name: String
    var blobName: String?

    private enum CodingKeys: String, CodingKey {
        case id, data, mimeType, name, blobName
    }

    init(id: String = UUID().uuidString, data: Data, mimeType: String, name: String, blobName: String? = nil) {
        self.id = id
        self.data = data
        self.mimeType = mimeType
        self.name = name
        self.blobName = blobName
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        mimeType = try container.decode(String.self, forKey: .mimeType)
        name = try container.decode(String.self, forKey: .name)
        blobName = try container.decodeIfPresent(String.self, forKey: .blobName)
        data = try container.decodeIfPresent(Data.self, forKey: .data) ?? Data()
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(mimeType, forKey: .mimeType)
        try container.encode(name, forKey: .name)
        if let blobName, !blobName.isEmpty {
            try container.encode(blobName, forKey: .blobName)
        } else {
            try container.encode(data, forKey: .data)
        }
    }

    var blobURL: URL? {
        guard let blobName, !blobName.isEmpty else { return nil }
        return Self.blobURL(named: blobName)
    }

    var byteCount: Int {
        if !data.isEmpty { return data.count }
        guard let url = blobURL,
              let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize else { return 0 }
        return size
    }

    var preview: MediaAttachment {
        let mediaKind = mimeType.hasPrefix("image/") ? "image" : mimeType.hasPrefix("video/") ? "video" : "file"
        if let url = blobURL, FileManager.default.fileExists(atPath: url.path) {
            return MediaAttachment(url: url.absoluteString, name: name, kind: mediaKind)
        }
        return MediaAttachment(url: "data:\(mimeType);base64,\(data.base64EncodedString())", name: name, kind: mediaKind)
    }

    /// Writes attachment bytes off the main actor so composer previews and outbox persistence stay tiny.
    static func staged(data: Data, mimeType: String, name: String) async -> DraftUpload {
        let id = UUID().uuidString
        let fileName = safeBlobFileName(id: id, originalName: name)
        let wrote = await Task.detached(priority: .userInitiated) {
            Self.writeBlobFile(data, named: fileName)
        }.value
        if wrote {
            return DraftUpload(id: id, data: Data(), mimeType: mimeType, name: name, blobName: fileName)
        }
        return DraftUpload(id: id, data: data, mimeType: mimeType, name: name)
    }

    /// Ensures direct callers of `DraftUpload(data:mimeType:name:)` also spill large payloads to disk.
    func ensuringStaged() -> DraftUpload {
        if let blobName, !blobName.isEmpty { return self }
        guard !data.isEmpty else { return self }
        let fileName = Self.safeBlobFileName(id: id, originalName: name)
        if Self.writeBlobFile(data, named: fileName) {
            return DraftUpload(id: id, data: Data(), mimeType: mimeType, name: name, blobName: fileName)
        }
        return self
    }

    func loadedForUpload() async throws -> DraftUpload {
        if !data.isEmpty { return self }
        guard let url = blobURL else {
            throw ServiceError.message("附件内容已失效，请重新添加")
        }
        let bytes = try await Task.detached(priority: .userInitiated) { () throws -> Data in
            guard let loaded = try? Data(contentsOf: url), !loaded.isEmpty else {
                throw ServiceError.message("附件内容已失效，请重新添加")
            }
            return loaded
        }.value
        return DraftUpload(id: id, data: bytes, mimeType: mimeType, name: name, blobName: blobName)
    }

    func removeStagedBlob() {
        guard let url = blobURL else { return }
        Task.detached(priority: .utility) {
            try? FileManager.default.removeItem(at: url)
        }
    }

    static var outboxDirectory: URL? {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("Outbox", isDirectory: true)
    }

    static var blobsDirectory: URL? {
        outboxDirectory?.appendingPathComponent("Blobs", isDirectory: true)
    }

    static func blobURL(named fileName: String) -> URL? {
        let clean = (fileName as NSString).lastPathComponent
        guard !clean.isEmpty, clean == fileName else { return nil }
        return blobsDirectory?.appendingPathComponent(clean)
    }

    private static func safeBlobFileName(id: String, originalName: String) -> String {
        let cleanID = String(id.filter { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-") }.prefix(64))
        let base = cleanID.isEmpty ? UUID().uuidString : cleanID
        let rawExt = (originalName as NSString).pathExtension.lowercased()
        let cleanExt = String(rawExt.filter { $0.isASCII && ($0.isLetter || $0.isNumber) }.prefix(10))
        return cleanExt.isEmpty ? base : "\(base).\(cleanExt)"
    }

    private static func writeBlobFile(_ data: Data, named fileName: String) -> Bool {
        guard let url = blobURL(named: fileName) else { return false }
        let directory = url.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            var dirExcluded = directory
            var dirValues = URLResourceValues()
            dirValues.isExcludedFromBackup = true
            try? dirExcluded.setResourceValues(dirValues)
            try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            var excluded = url
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? excluded.setResourceValues(values)
            return true
        } catch {
            return false
        }
    }

    /// Prunes unreferenced staged blobs older than 1 hour at launch (for example abandoned composer drafts).
    static func trimOrphanedBlobs(maxAgeSeconds: TimeInterval = 3_600) {
        guard let outboxDir = outboxDirectory, let blobsDir = blobsDirectory else { return }
        var referenced: Set<String> = []
        if let jsonFiles = try? FileManager.default.contentsOfDirectory(at: outboxDir, includingPropertiesForKeys: nil) {
            for file in jsonFiles where file.pathExtension == "json" {
                guard let data = try? Data(contentsOf: file),
                      let turns = try? JSONDecoder().decode([QueuedTurn].self, from: data) else { continue }
                for turn in turns {
                    for attachment in turn.attachments {
                        if let blobName = attachment.blobName { referenced.insert(blobName) }
                    }
                }
            }
        }
        guard let blobs = try? FileManager.default.contentsOfDirectory(
            at: blobsDir,
            includingPropertiesForKeys: [.contentModificationDateKey]
        ) else { return }
        let cutoff = Date().addingTimeInterval(-maxAgeSeconds)
        for blob in blobs where !referenced.contains(blob.lastPathComponent) {
            let modified = (try? blob.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
            if modified < cutoff {
                try? FileManager.default.removeItem(at: blob)
            }
        }
    }
}

struct SideChat: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let preview: String
}

struct QueuedTurn: Codable, Sendable {
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
    func send(text: String, attachments: [DraftUpload] = [], chatID: String? = nil) -> Bool {
        let targetChatID = chatID ?? currentChatID
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !targetChatID.isEmpty, !trimmed.isEmpty || !attachments.isEmpty else { return false }
        guard attachments.count <= 6 else {
            errorMessage = "一次最多添加 6 个附件"
            return false
        }
        guard attachments.allSatisfy({ let count = $0.byteCount; return count > 0 && count <= 20 * 1024 * 1024 }) else {
            errorMessage = "单个附件不能超过 20 MB"
            return false
        }
        let estimate = JSONValue.object([
            "type": .string("message"), "chat_id": .string(targetChatID), "content": .string(trimmed),
            "turn_id": .string(UUID().uuidString), "webui": .bool(true),
            "media": .array(attachments.map { .object(["reference": .string(String(repeating: "x", count: 128)), "name": .string($0.name)]) }),
        ])
        guard let estimatedBytes = try? JSONEncoder().encode(estimate), estimatedBytes.count <= frameLimit else {
            errorMessage = "消息太长，请分成几条发送"
            return false
        }
        let stagedAttachments = attachments.map { $0.ensuringStaged() }
        let queued = QueuedTurn(id: UUID().uuidString, chatID: targetChatID, text: trimmed,
                                attachments: stagedAttachments, createdAt: Date().timeIntervalSince1970 * 1000, state: "queued")
        outbox.append(queued)
        var projection = projections[targetChatID] ?? StreamProjection()
        projection.messages.append(optimistic(queued))
        projections[targetChatID] = projection
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
        flushRetryTask?.cancel()
        flushRetryTask = nil
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
            var markedSent = false
            do {
                uploading = !queued.attachments.isEmpty
                var references: [JSONValue] = []
                for attachment in queued.attachments {
                    let uploadDraft = try await attachment.loadedForUpload()
                    let reference = try await api.upload(uploadDraft, capability: uploadCapability)
                    guard attempt == generation, connected else { uploading = false; return }
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
                markedSent = true
                await persistOutboxNow()
                try await write(frame)
                flushRetryAttempt = 0
                runningChats.insert(queued.chatID)
                publishCurrent()
            } catch {
                uploading = false
                guard attempt == generation else { return }
                if markedSent {
                    markOptimisticDelivery(turnID: queued.id, chatID: queued.chatID, status: "failed")
                    publishCurrent()
                    show(error)
                } else if let service = error as? ServiceError, service.isAuthentication {
                    show(error)
                    connectionDropped()
                } else if isPermanentOutboxFailure(error) {
                    if let index = outbox.firstIndex(where: { $0.id == queued.id }) {
                        outbox[index].state = "sent"
                        persistOutbox()
                    }
                    markOptimisticDelivery(turnID: queued.id, chatID: queued.chatID, status: "failed")
                    publishCurrent()
                    show(error)
                } else {
                    show(error)
                    scheduleFlushRetry(generation: attempt)
                }
                return
            }
        }
    }

    private func markOptimisticDelivery(turnID: String, chatID: String, status: String) {
        guard var projection = projections[chatID] else { return }
        var changed = false
        for index in projection.messages.indices where projection.messages[index].turnId == turnID && projection.messages[index].role == "user" {
            if projection.messages[index].deliveryStatus != status {
                projection.messages[index].deliveryStatus = status
                changed = true
            }
        }
        if changed { projections[chatID] = projection }
    }

    private func isPermanentOutboxFailure(_ error: Error) -> Bool {
        guard let service = error as? ServiceError else { return false }
        switch service {
        case .http(let status):
            return [400, 413, 415, 422].contains(status)
        case .message(let message):
            return message == "单个附件不能超过 20 MB"
                || message == "消息太长，请分成几条发送"
                || message == "附件内容已失效，请重新添加"
        }
    }

    private func scheduleFlushRetry(generation attempt: UUID) {
        guard connected, sessionInitialized, outbox.contains(where: { $0.state == "queued" }) else { return }
        flushRetryTask?.cancel()
        let delay = min(30.0, 2.0 * pow(2.0, Double(min(flushRetryAttempt, 4))))
        flushRetryAttempt += 1
        flushRetryTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            catch { return }
            guard let self, self.generation == attempt, self.connected, self.sessionInitialized else { return }
            self.flushRetryTask = nil
            self.beginFlush()
        }
    }

    func stop(chatID: String? = nil) async {
        let targetChatID = chatID ?? currentChatID
        guard connected, !targetChatID.isEmpty else { return }
        do {
            try await write(.object(["type": .string("message"), "chat_id": .string(targetChatID),
                                     "content": .string("/stop"), "turn_id": .string("webui-system:" + UUID().uuidString), "webui": .bool(true)]))
        } catch { show(error) }
    }

    /// Stops an active `/goal` loop, interrupting any in-flight turn first (matching `CompanionScreen.tsx`).
    func stopGoal() async {
        let id = currentChatID
        guard !id.isEmpty else { return }
        if runningChats.contains(id) {
            cancelGoalAfterStop.insert(id)
            await stop(chatID: id)
        } else {
            cancelGoalAfterStop.remove(id)
            _ = send(text: "/goal stop", chatID: id)
        }
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
            goalStates.removeValue(forKey: id)
            cancelGoalAfterStop.remove(id)
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
        markOptimisticDelivery(turnID: turnID, chatID: id, status: "sending")
        persistOutbox()
        publishCurrent()
        beginFlush()
    }

    var outboxURL: URL? {
        guard !serverAddress.isEmpty, let directory = DraftUpload.outboxDirectory else { return nil }
        let digest = SHA256.hash(data: Data(serverAddress.utf8)).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(digest + ".json")
    }

    func loadOutbox() {
        guard let url = outboxURL, let data = try? Data(contentsOf: url),
              let items = try? JSONDecoder().decode([QueuedTurn].self, from: data) else {
            outbox = []
            trackedOutboxBlobs = []
            queuedCount = 0
            return
        }
        let migrated = items.map { turn in
            QueuedTurn(
                id: turn.id,
                chatID: turn.chatID,
                text: turn.text,
                attachments: turn.attachments.map { $0.ensuringStaged() },
                createdAt: turn.createdAt,
                state: turn.state
            )
        }
        outbox = migrated
        trackedOutboxBlobs = Set(migrated.flatMap(\.attachments).compactMap(\.blobName))
        queuedCount = migrated.filter { $0.state == "queued" }.count
        if migrated.flatMap(\.attachments).map(\.blobName) != items.flatMap(\.attachments).map(\.blobName) {
            persistOutbox()
        }
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
        Task { [weak self] in
            await self?.persistOutboxNow()
        }
    }

    func persistOutboxNow() async {
        let queued = outbox.filter { $0.state == "queued" }.count
        if queuedCount != queued { queuedCount = queued }
        let snapshot = outbox
        let currentBlobs = Set(snapshot.flatMap(\.attachments).compactMap(\.blobName))
        let removedBlobs = trackedOutboxBlobs.subtracting(currentBlobs)
        trackedOutboxBlobs = currentBlobs
        let staleURLs = removedBlobs.compactMap(DraftUpload.blobURL(named:))
        guard let url = outboxURL else { return }
        let ok = await OutboxPersistence.shared.save(turns: snapshot, to: url, removing: staleURLs)
        if !ok {
            errorMessage = "离线消息暂时无法保存，请保持 App 打开"
        }
    }

    func clearServerOutbox() {
        let urls = trackedOutboxBlobs.compactMap(DraftUpload.blobURL(named:))
            + outbox.flatMap(\.attachments).compactMap(\.blobURL)
        trackedOutboxBlobs = []
        outbox = []
        queuedCount = 0
        let file = outboxURL
        Task {
            await OutboxPersistence.shared.removeAll(outboxURL: file, blobURLs: urls)
        }
    }
}
