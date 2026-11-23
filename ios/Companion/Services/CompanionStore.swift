import Combine
import CryptoKit
import Foundation
import Network

private final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }
}

@MainActor
final class CompanionStore: ObservableObject {
    @Published var serverAddress: String
    @Published var needsServer = false
    @Published var needsPairing = false
    @Published var connecting = false
    @Published var connected = false
    @Published var errorMessage: String?
    @Published var companionState: JSONValue = .object([:])
    @Published var avatars: [JSONValue] = []
    @Published var helpers: [JSONValue] = []
    @Published var messages: [ChatMessage] = []
    @Published var sideChats: [SideChat] = []
    @Published var currentChatID = ""
    @Published var loadingHistory = false
    @Published var hasOlder = false
    @Published var approvals: [JSONValue] = []
    @Published var running = false
    @Published var queuedCount = 0
    @Published var uploading = false
    @Published private(set) var currentDeviceID = ""
    @Published private(set) var currentGoal: GoalState?
    @Published var suggestedDraft: String?

    var baseURL: URL? { api?.baseURL ?? (try? APIClient.normalizedServer(serverAddress)) }
    var companionName: String { companionState["name"].nonemptyString ?? "伙伴" }
    var pendingApprovals: [JSONValue] { approvals.filter { $0["status"].stringValue == "pending" } }
    var mainChatID: String { companionState["chat_id"].stringValue }
    func composeDraft(_ text: String) {
        // Persist before publishing so a simultaneous chat switch cannot restore
        // an older local draft over a newly requested discussion or idea.
        if !currentChatID.isEmpty { UserDefaults.standard.set(text, forKey: "draft.\(serverAddress).\(currentChatID)") }
        suggestedDraft = text
    }
    var activityLabel: String {
        if !pendingApprovals.isEmpty { return "等你确认" }
        return Presentation.currentActivity(messages: messages, streaming: running)?.label ?? (connected ? "在这里陪你" : "等待连接")
    }
    var animation: String {
        if !connected { return "sleeping" }
        if !pendingApprovals.isEmpty { return "waiting" }
        if !running { return "idle" }
        let kind = Presentation.currentActivity(messages: messages, streaming: true)?.kind ?? "thinking"
        switch kind {
        case "thinking", "recall": return "review"
        case "typing", "schedule", "send": return "writing"
        case "search": return "searching"
        case "read": return "reading"
        case "files", "run": return "typing"
        case "image": return "drawing"
        default: return "running"
        }
    }

    var api: APIClient?
    var socket: URLSessionWebSocketTask?
    var receiveTask: Task<Void, Never>?
    var reconnectTask: Task<Void, Never>?
    var refreshTask: Task<Void, Never>?
    var heartbeatTask: Task<Void, Never>?
    var flushTask: Task<Void, Never>?
    var flushRetryTask: Task<Void, Never>?
    let monitor = NWPathMonitor()
    var pathSignature: String?
    var generation = UUID()
    var reconnectAttempt = 0
    var flushRetryAttempt = 0
    var tokenRefreshDeadline = Date.distantPast
    var uploadCapability: JSONValue = .null
    var frameLimit = 2 * 1024 * 1024
    var knownChats: Set<String> = []
    var runningChats: Set<String> = []
    var goalStates: [String: GoalState] = [:]
    var cancelGoalAfterStop: Set<String> = []
    var projections: [String: StreamProjection] = [:]
    var historyEvents: [String: [JSONValue]] = [:]
    /// Each chat's events as the server last sent them (also kept on this iPhone): what an
    /// incremental history request builds on.
    var savedEvents: [String: [JSONValue]] = [:]
    /// Goes up when the server says 动态 changed (a new post): the feed and its badge read it again.
    @Published var feedRevision = 0
    /// 查找聊天记录 → a message: the turn the chat scrolls to once it has opened.
    @Published var revealTurn: String?
    var duringHistory: [String: [JSONValue]] = [:]
    var visibleCounts: [String: Int] = [:]
    var beforeCursors: [String: String] = [:]
    var serverHasOlder: Set<String> = []
    var reconcileAtTurnEnd: Set<String> = []
    var needsHistoryRefresh: Set<String> = []
    var loadingChats: Set<String> = []
    var outbox: [QueuedTurn] = []
    var trackedOutboxBlobs: Set<String> = []
    var pendingMutations: [String: PendingMutation] = [:]
    var pendingTranscriptions: [String: PendingTranscription] = [:]
    var pendingNewChat: CheckedContinuation<String, Error>?
    var newChatTimeout: Task<Void, Never>?
    var sessionInitialized = false
    var lastStateRefresh = Date.distantPast
    var lastApprovalRefresh = Date.distantPast

    struct PendingMutation {
        let frame: JSONValue
        let continuation: CheckedContinuation<JSONValue, Error>
        let timeout: Task<Void, Never>
    }

    struct PendingTranscription {
        let continuation: CheckedContinuation<String, Error>
        let timeout: Task<Void, Never>
    }

    init() {
        serverAddress = UserDefaults.standard.string(forKey: "companion.server") ?? ""
        needsServer = serverAddress.isEmpty
        if !serverAddress.isEmpty {
            if let cached = UserDefaults.standard.data(forKey: cacheKey),
               let value = try? JSONValue.parse(cached) { companionState = value }
            loadOutbox()
            currentChatID = mainChatID
            restoreQueuedPreviews()
        }
        monitor.pathUpdateHandler = { [weak self] path in
            let signature = "\(path.status)-\(path.isExpensive)-\(path.usesInterfaceType(.wifi))-\(path.usesInterfaceType(.cellular))"
            let available = path.status == .satisfied
            Task { @MainActor [weak self] in
                guard let self else { return }
                let changed = self.pathSignature != nil && self.pathSignature != signature
                self.pathSignature = signature
                if changed && available && !self.needsServer && !self.needsPairing {
                    await self.foreground()
                } else if !available && !self.needsServer && !self.needsPairing {
                    self.connectionDropped()
                }
            }
        }
        monitor.start(queue: DispatchQueue(label: "com.personalbot.companion.network"))
    }

    deinit {
        monitor.cancel()
    }

    /// Called by the app when returning from background; live sockets stay open so active uploads
    /// and streaming turns continue undisturbed, while stale or expired sessions reconnect cleanly.
    func foreground() async {
        guard !needsServer, !needsPairing, !connecting, !serverAddress.isEmpty else { return }
        if connected, let currentSocket = socket, uploading || Date() < tokenRefreshDeadline {
            if await pingSocket(currentSocket, timeoutSeconds: 4) {
                guard connected, socket === currentSocket else { return }
                let refreshed = await refreshCompanion()
                guard connected, socket === currentSocket else { return }
                if refreshed || uploading {
                    await refreshSessions()
                    await refreshApprovals()
                    if !currentChatID.isEmpty, !runningChats.contains(currentChatID) {
                        await hydrate(currentChatID)
                    }
                    beginFlush()
                    return
                }
            }
        }
        await connect(server: serverAddress)
    }

    func connect(server: String) async {
        do {
            let normalized = try APIClient.normalizedServer(server).absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            if normalized == serverAddress && connecting { return }
            if normalized != serverAddress {
                closeConnection(rejectRequests: true)
                serverAddress = normalized
                UserDefaults.standard.set(normalized, forKey: "companion.server")
                resetServerState()
                loadOutbox()
                if let cached = UserDefaults.standard.data(forKey: cacheKey),
                   let value = try? JSONValue.parse(cached) { companionState = value }
                currentChatID = mainChatID
                restoreQueuedPreviews()
            }
            needsServer = false
            needsPairing = false
            errorMessage = nil
            if let code = Self.pairingCode(in: server) {
                needsPairing = true
                await pair(code: code)
                return
            }
            await openConnection()
        } catch { show(error) }
    }

    func pair(code: String) async {
        guard !connecting else { return }
        guard let base = try? APIClient.normalizedServer(serverAddress) else { needsServer = true; return }
        connecting = true
        errorMessage = nil
        do {
            let client = api ?? APIClient(baseURL: base)
            api = client
            let result = try await client.pair(code: code)
            guard let credential = result["token"].string, !credential.isEmpty else {
                throw ServiceError.message("配对没有返回设备凭证")
            }
            try DeviceKeychain.save(credential, server: serverAddress)
            needsPairing = false
            await openConnection()
        } catch {
            connecting = false
            show(error)
        }
    }

    func changeServer() {
        closeConnection(rejectRequests: true)
        needsServer = true
        needsPairing = false
        connecting = false
        errorMessage = nil
    }

    func openConnection() async {
        closeConnection(rejectRequests: false)
        guard let base = try? APIClient.normalizedServer(serverAddress) else { needsServer = true; return }
        connecting = true
        let attempt = generation
        do {
            let client: APIClient
            if let existing = api, existing.baseURL == base { client = existing }
            else {
                api?.invalidate()
                client = APIClient(baseURL: base)
                api = client
            }
            let credential = try DeviceKeychain.read(server: serverAddress)
            let boot = try await client.bootstrap(deviceToken: credential)
            guard attempt == generation else { return }
            client.apiToken = boot["api_token"].stringValue
            currentDeviceID = boot["device_id"].stringValue
            frameLimit = boot["limits"]["transport"]["max_frame_bytes"].intValue ?? 2 * 1024 * 1024
            let url = try client.socketURL(bootstrap: boot)
            let task = client.makeWebSocketTask(url: url, maximumMessageSize: frameLimit)
            socket = task
            sessionInitialized = false
            uploadCapability = .null
            task.resume()
            receiveTask = Task { [weak self, weak task] in
                guard let self, let task else { return }
                do {
                    while !Task.isCancelled {
                        let frame = try await task.receive()
                        guard attempt == self.generation else { return }
                        let data: Data
                        switch frame {
                        case .string(let text): data = Data(text.utf8)
                        case .data(let bytes): data = bytes
                        @unknown default: continue
                        }
                        guard let event = try? JSONValue.parse(data) else { continue }
                        await self.handle(event, generation: attempt)
                    }
                } catch {
                    guard attempt == self.generation, !Task.isCancelled else { return }
                    self.connectionDropped()
                }
            }
            scheduleRefresh(expiresIn: boot["expires_in"].doubleValue ?? 3600, generation: attempt)
        } catch {
            guard attempt == generation else { return }
            connecting = false
            connected = false
            if let service = error as? ServiceError, service.isAuthentication {
                DeviceKeychain.remove(server: serverAddress)
                // Removed from the server (or its pairing reset): what this iPhone kept of it goes too.
                LocalStore.removeServer(serverAddress)
                clearServerOutbox()
                savedEvents = [:]
                needsPairing = true
                errorMessage = "输入服务器生成的一次性配对码，即可连接这台 iPhone"
                closeConnection(rejectRequests: true)
            } else {
                show(error)
                scheduleReconnect()
            }
        }
    }

    func scheduleRefresh(expiresIn: Double, generation attempt: UUID) {
        refreshTask?.cancel()
        let seconds = max(5, expiresIn - min(60, expiresIn / 2))
        tokenRefreshDeadline = Date().addingTimeInterval(seconds)
        refreshTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }
            catch { return }
            guard let self, self.generation == attempt else { return }
            self.refreshTask = nil
            await self.openConnection()
        }
    }

    func connectionDropped() {
        closeConnection(rejectRequests: false)
        connecting = false
        scheduleReconnect()
    }

    func scheduleReconnect() {
        guard !needsPairing, !needsServer else { return }
        reconnectTask?.cancel()
        let seconds = min(30.0, 0.5 * pow(2, Double(min(reconnectAttempt, 6))))
        reconnectAttempt += 1
        reconnectTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }
            catch { return }
            guard let self else { return }
            self.reconnectTask = nil
            await self.openConnection()
        }
    }

    func closeConnection(rejectRequests: Bool) {
        reconcileAtTurnEnd.formUnion(runningChats)
        needsHistoryRefresh.formUnion(knownChats)
        generation = UUID()
        receiveTask?.cancel()
        reconnectTask?.cancel()
        refreshTask?.cancel()
        heartbeatTask?.cancel()
        flushTask?.cancel()
        flushTask = nil
        flushRetryTask?.cancel()
        flushRetryTask = nil
        flushRetryAttempt = 0
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        connected = false
        uploadCapability = .null
        sessionInitialized = false
        uploading = false
        loadingChats.removeAll()
        duringHistory.removeAll()
        loadingHistory = false
        let transcriptions = Array(pendingTranscriptions.values)
        pendingTranscriptions.removeAll()
        for item in transcriptions {
            item.timeout.cancel()
            item.continuation.resume(throwing: ServiceError.message("语音连接中断，请重试"))
        }
        if let pendingNewChat {
            self.pendingNewChat = nil
            pendingNewChat.resume(throwing: ServiceError.message("连接中断，请重试新建旁聊"))
        }
        newChatTimeout?.cancel()
        if rejectRequests {
            let requests = Array(pendingMutations.values)
            pendingMutations.removeAll()
            for item in requests {
                item.timeout.cancel()
                item.continuation.resume(throwing: ServiceError.message("连接已更换，请重试"))
            }
        }
    }

    func handle(_ event: JSONValue, generation attempt: UUID) async {
        let kind = event["event"].stringValue
        if kind == "ready" {
            connected = true
            connecting = false
            reconnectAttempt = 0
            errorMessage = nil
            uploadCapability = event["upload"]
            for id in knownChats { try? await write(.object(["type": .string("attach"), "chat_id": .string(id)])) }
            for request in pendingMutations.values { try? await write(request.frame) }
            Task { [weak self] in
                guard let self else { return }
                await self.initializeSession(generation: attempt)
            }
            startHeartbeat(generation: attempt)
            return
        }
        if kind == "webui_response" {
            let id = event["request_id"].stringValue
            guard let pending = pendingMutations.removeValue(forKey: id) else { return }
            pending.timeout.cancel()
            if event["ok"].boolValue { pending.continuation.resume(returning: event["result"]) }
            else {
                let status = event["error"]["status"].intValue ?? 500
                let message = event["error"]["message"].nonemptyString
                pending.continuation.resume(throwing: message.map(ServiceError.message) ?? ServiceError.http(status))
            }
            return
        }
        if kind == "transcription_result" || kind == "transcription_error" {
            let requestID = event["request_id"].stringValue
            guard let pending = pendingTranscriptions.removeValue(forKey: requestID) else { return }
            pending.timeout.cancel()
            if kind == "transcription_result" { pending.continuation.resume(returning: event["text"].stringValue) }
            else {
                let detail = event["detail"].stringValue == "not_configured"
                    ? "服务器还没有配置语音识别" : "没有识别出这段语音，请重试"
                pending.continuation.resume(throwing: ServiceError.message(detail))
            }
            return
        }
        if kind == "feed_updated" {
            feedRevision &+= 1
            return
        }
        if kind == "approval_updated" {
            Task { [weak self] in await self?.refreshApprovals() }
            return
        }
        let id = event["chat_id"].stringValue
        if kind == "attached", let continuation = pendingNewChat, !id.isEmpty, !knownChats.contains(id) {
            pendingNewChat = nil
            newChatTimeout?.cancel()
            knownChats.insert(id)
            continuation.resume(returning: id)
            return
        }
        if kind == "session_updated" {
            await refreshSessions()
            if id == currentChatID && !running { await hydrate(id) }
            return
        }
        guard !id.isEmpty else { return }
        if kind == "goal_state" {
            let state = GoalState(json: event["goal_state"])
            if state.active {
                goalStates[id] = state
            } else {
                goalStates.removeValue(forKey: id)
                cancelGoalAfterStop.remove(id)
            }
            if id == currentChatID { publishCurrent() }
            return
        }
        if kind == "message_accepted" || kind == "user_message" {
            let turnID = event["turn_id"].stringValue
            if !turnID.isEmpty {
                outbox.removeAll { $0.id == turnID && $0.chatID == id }
                persistOutbox()
                if kind == "message_accepted", var projection = projections[id] {
                    for index in projection.messages.indices where projection.messages[index].turnId == turnID && projection.messages[index].role == "user" {
                        projection.messages[index].deliveryStatus = "accepted"
                    }
                    projections[id] = projection
                }
            }
        }
        if ["message_accepted", "delta", "reasoning_delta"].contains(kind) {
            runningChats.insert(id)
        }
        if kind == "message", ["tool_hint", "progress"].contains(event["kind"].stringValue),
           event["turn_id"].nonemptyString != nil {
            runningChats.insert(id)
        }
        if kind == "user_message" && event["starts_turn"].boolValue { runningChats.insert(id) }
        if kind == "turn_end" || (kind == "goal_status" && event["status"].stringValue == "idle") {
            runningChats.remove(id)
        } else if kind == "goal_status" && event["status"].stringValue != "idle" {
            runningChats.insert(id)
        }
        if kind == "error" {
            let turnID = event["turn_id"].stringValue
            outbox.removeAll { $0.id == turnID && $0.chatID == id }
            persistOutbox()
            runningChats.remove(id)
            if id == currentChatID { errorMessage = "消息未被服务器接受，请检查内容后重试" }
        }
        var projection = projections[id] ?? StreamProjection()
        if kind == "user_message", let turnID = event["turn_id"].string {
            projection.messages.removeAll { $0.id.hasPrefix("local-") && $0.role == "user" && $0.turnId == turnID }
        }
        projection.apply(event)
        projections[id] = projection
        let replayKinds = ["user_message", "message", "delta", "stream_end", "turn_end", "reasoning_delta", "reasoning_end", "context_compaction", "file_edit"]
        // Only the chat's own events wait for a history load: "attached" and "goal_state" answer the
        // attach sent just before it, and counted as news they kept a finished chat "working".
        if replayKinds.contains(kind) {
            if duringHistory[id] != nil { duringHistory[id, default: []].append(event) }
            else { historyEvents[id, default: []].append(event) }
        }
        if id == currentChatID { publishCurrent() }
        if kind == "turn_end" {
            let shouldCancelGoal = cancelGoalAfterStop.remove(id) != nil
            let reconcile = reconcileAtTurnEnd.remove(id) != nil
            if shouldCancelGoal {
                _ = send(text: "/goal stop", chatID: id)
            }
            Task { [weak self] in
                if reconcile { await self?.hydrate(id) }
                await self?.refreshSessions()
                await self?.refreshCompanion()
                await self?.refreshLibrary()
                await self?.refreshApprovals()
            }
        } else if kind == "message" && id == currentChatID && Date().timeIntervalSince(lastApprovalRefresh) > 2 {
            Task { [weak self] in await self?.refreshApprovals() }
        }
    }

    func initializeSession(generation attempt: UUID) async {
        let loadedIdentity = await refreshCompanion()
        guard attempt == generation, connected else { return }
        guard loadedIdentity || !mainChatID.isEmpty else {
            // A failed identity read must never create a replacement main chat.
            connectionDropped()
            return
        }
        if mainChatID.isEmpty {
            do {
                let id = try await newChatID()
                companionState = try await requestMutation("companion.update", payload: ["changes": .object(["chat_id": .string(id)])])
                cacheState()
            } catch { show(error); return }
        }
        guard attempt == generation else { return }
        if currentChatID.isEmpty { currentChatID = mainChatID }
        Task { [weak self] in
            await self?.refreshSessions()
            await self?.refreshLibrary()
        }
        await changeChat(currentChatID, forceHistory: true)
        for id in Set(outbox.map(\.chatID)) where id != currentChatID {
            knownChats.insert(id)
            try? await write(.object(["type": .string("attach"), "chat_id": .string(id)]))
            await hydrate(id)
        }
        guard attempt == generation else { return }
        sessionInitialized = true
        beginFlush()
    }

    func approve(id: String, decision: String, scope: String = "once") async {
        do {
            let result = try await requestMutation("approval.decide", payload: ["id": .string(id), "decision": .string(decision), "scope": .string(scope)], timeout: 60)
            if let index = approvals.firstIndex(where: { $0["id"].stringValue == id }) { approvals[index] = result }
            await refreshApprovals()
        } catch { show(error) }
    }

    func updateAppearance(theme: String, petSize: String) async {
        do {
            companionState = try await requestMutation("companion.update", payload: ["changes": .object([
                "theme_mode": .string(theme), "pet_size": .string(petSize), "timezone": .string(TimeZone.current.identifier),
            ])])
            cacheState()
        } catch { show(error) }
    }

    /// Native pages share the authenticated client and preserve per-page error states.
    func getResource(_ path: String) async throws -> JSONValue {
        guard let api, !api.apiToken.isEmpty else { throw ServiceError.message("连接后再试一次") }
        return try await api.get(path)
    }

    func postResource(_ path: String, payload: [String: JSONValue] = [:]) async throws -> JSONValue {
        guard let api, connected else { throw ServiceError.message("连接后再试一次") }
        return try await api.post(path, payload: payload)
    }

    func computerSocketURL() throws -> URL {
        guard let api, connected else { throw ServiceError.message("连接后再试一次") }
        return try api.socketURL(bootstrap: .object(["ws_path": .string("/ws/computer"), "token": .string(api.apiToken)]))
    }

    func makeComputerSocketTask() throws -> URLSessionWebSocketTask {
        guard let api else { throw ServiceError.message("连接后再试一次") }
        return api.makeWebSocketTask(url: try computerSocketURL(), maximumMessageSize: 8 * 1024 * 1024)
    }

    func changeResource(_ action: String, payload: [String: JSONValue] = [:]) async throws -> JSONValue {
        let slow = ["companion.ideas.refresh", "companion.models.check", "companion.memory.learn",
                    "companion.memory.tidy", "settings.mcp.custom", "companion.skills.change"]
        let drawingAvatar = action.contains("avatar") || (action == "companion.helpers.change" && payload["action"]?.stringValue == "avatar")
        let timeout: Double = drawingAvatar ? 240 : slow.contains(action) ? 180 : 60
        let result = try await requestMutation(action, payload: payload, timeout: timeout)
        if action == "companion.update" { companionState = result; cacheState() }
        if action == "companion.helpers.change" || action == "companion.groups.change" { await refreshLibrary() }
        return result
    }

    func transcribeAudio(dataURL: String, durationMS: Double) async throws -> String {
        guard connected else { throw ServiceError.message("连接后再试一次") }
        let id = UUID().uuidString
        let frame: JSONValue = .object(["type": .string("transcribe_audio"), "request_id": .string(id),
                                        "data_url": .string(dataURL), "duration_ms": .number(max(0, durationMS))])
        return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<String, Error>) in
            let timer = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: 120_000_000_000) } catch { return }
                guard let pending = self?.pendingTranscriptions.removeValue(forKey: id) else { return }
                pending.continuation.resume(throwing: ServiceError.message("语音识别超时，请重试"))
            }
            pendingTranscriptions[id] = PendingTranscription(continuation: continuation, timeout: timer)
            Task { [weak self] in
                do { try await self?.write(frame) }
                catch {
                    guard let pending = self?.pendingTranscriptions.removeValue(forKey: id) else { return }
                    pending.timeout.cancel()
                    pending.continuation.resume(throwing: error)
                }
            }
        }
    }

    func requestMutation(_ action: String, payload: [String: JSONValue], timeout: Double = 20) async throws -> JSONValue {
        guard connected else { throw ServiceError.message("连接后再试一次") }
        let id = UUID().uuidString
        let frame = JSONValue.object(["type": .string("webui_request"), "request_id": .string(id), "action": .string(action), "payload": .object(payload)])
        let result = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<JSONValue, Error>) in
            let timer = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000)) } catch { return }
                guard let pending = self?.pendingMutations.removeValue(forKey: id) else { return }
                pending.continuation.resume(throwing: ServiceError.message("服务器操作超时，请重试"))
            }
            pendingMutations[id] = PendingMutation(frame: frame, continuation: continuation, timeout: timer)
            Task { [weak self] in
                do { try await self?.write(frame) }
                catch {
                    guard let pending = self?.pendingMutations.removeValue(forKey: id) else { return }
                    pending.timeout.cancel()
                    pending.continuation.resume(throwing: error)
                }
            }
        }
        // A scheduled task paused, resumed or deleted here: the phone's notifications follow.
        if action.hasPrefix("automation.") { Task { await planReminders() } }
        return result
    }

    func write(_ frame: JSONValue) async throws {
        guard connected, let socket else { throw ServiceError.message("连接已断开") }
        let data = try JSONEncoder().encode(frame)
        guard data.count <= frameLimit else { throw ServiceError.message("消息太长，请分成几条发送") }
        guard let text = String(data: data, encoding: .utf8) else { throw ServiceError.message("消息编码失败") }
        try await socket.send(.string(text))
    }

    @discardableResult
    func refreshCompanion() async -> Bool {
        guard let api else { return false }
        let attempt = generation
        do {
            let value = try await api.get("/api/webui/companion")
            guard attempt == generation else { return false }
            lastStateRefresh = Date()
            if companionState != value { companionState = value; cacheState() }
            return true
        } catch { if companionState.object.isEmpty { show(error) } }
        return false
    }

    func refreshSessions() async {
        guard let api else { return }
        let attempt = generation
        do {
            let result = try await api.get("/api/sessions")
            guard attempt == generation else { return }
            let chats: [SideChat] = result["sessions"].array.compactMap { row in
                let key = row["key"].stringValue
                guard key.hasPrefix("websocket:"), row["helper"].stringValue.isEmpty, !row["group"].boolValue else { return nil }
                let id = String(key.dropFirst("websocket:".count))
                guard !id.isEmpty, id != mainChatID else { return nil }
                return SideChat(id: id, title: row["title"].nonemptyString ?? "新的旁聊", preview: row["preview"].stringValue)
            }
            if sideChats != chats { sideChats = chats }
        } catch { /* A failed sidebar refresh must not interrupt a live conversation. */ }
    }

    func refreshLibrary() async {
        guard let api else { return }
        let attempt = generation
        if let value = try? await api.get("/api/webui/companion/avatars"), attempt == generation {
            let items = value["items"].records
            if avatars != items { avatars = items }
        }
        if let value = try? await api.get("/api/webui/companion/helpers"), attempt == generation {
            let items = value["helpers"].records
            if helpers != items { helpers = items }
        }
    }

    func refreshApprovals() async {
        guard let api, !currentChatID.isEmpty else { return }
        let id = currentChatID
        let attempt = generation
        lastApprovalRefresh = Date()
        do {
            let value = try await api.get("/api/webui/approvals", query: [URLQueryItem(name: "session_key", value: "websocket:" + id)])
            guard currentChatID == id, generation == attempt else { return }
            let items = value["approvals"].records
            if approvals != items { approvals = items }
        } catch { /* Keep the last cards during a transient refresh failure. */ }
    }

    func startHeartbeat(generation attempt: UUID) {
        heartbeatTask?.cancel()
        heartbeatTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 20_000_000_000) } catch { return }
                guard let self, self.generation == attempt, self.connected, let socket = self.socket else { return }
                do {
                    try await Self.sendPingOnce(socket, timeoutSeconds: 10)
                    guard self.generation == attempt else { return }
                    await self.refreshApprovals()
                    await self.refreshLibrary()
                    if Date().timeIntervalSince(self.lastStateRefresh) > 20 { await self.refreshCompanion() }
                } catch {
                    guard self.generation == attempt else { return }
                    self.connectionDropped()
                    return
                }
            }
        }
    }

    func pingSocket(_ socket: URLSessionWebSocketTask, timeoutSeconds: Double) async -> Bool {
        do {
            try await Self.sendPingOnce(socket, timeoutSeconds: timeoutSeconds)
            return true
        } catch {
            return false
        }
    }

    nonisolated static func sendPingOnce(_ socket: URLSessionWebSocketTask, timeoutSeconds: Double) async throws {
        let gate = LockedFlag()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let timeoutTask = Task.detached {
                do { try await Task.sleep(nanoseconds: UInt64(max(0.5, timeoutSeconds) * 1_000_000_000)) }
                catch { return }
                if gate.claim() {
                    continuation.resume(throwing: ServiceError.message("连接心跳超时"))
                }
            }
            socket.sendPing { error in
                timeoutTask.cancel()
                guard gate.claim() else { return }
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            }
        }
    }

    func publishCurrent() {
        // Every assignment to a published value redraws every screen, equal or not: only real changes.
        let all = projections[currentChatID]?.messages ?? []
        let count = visibleCounts[currentChatID] ?? 60
        let visible = Array(all.suffix(count))
        if messages != visible { messages = visible }
        let older = all.count > count || serverHasOlder.contains(currentChatID)
        if hasOlder != older { hasOlder = older }
        let busy = runningChats.contains(currentChatID)
        if running != busy { running = busy }
        let activeGoal = goalStates[currentChatID].flatMap { $0.active ? $0 : nil }
        if currentGoal != activeGoal { currentGoal = activeGoal }
        let queued = outbox.filter { $0.state == "queued" }.count
        if queuedCount != queued { queuedCount = queued }
    }

    var cacheKey: String { "companion.state." + serverAddress }
    func cacheState() {
        if let data = try? JSONEncoder().encode(companionState) { UserDefaults.standard.set(data, forKey: cacheKey) }
    }

    func resetServerState() {
        api?.invalidate()
        api = nil
        tokenRefreshDeadline = .distantPast
        currentChatID = ""
        companionState = .object([:])
        messages = []
        sideChats = []
        approvals = []
        avatars = []
        helpers = []
        currentDeviceID = ""
        currentGoal = nil
        suggestedDraft = nil
        knownChats = []
        runningChats = []
        goalStates = [:]
        cancelGoalAfterStop = []
        projections = [:]
        historyEvents = [:]
        savedEvents = [:]
        duringHistory = [:]
        loadingChats = []
        visibleCounts = [:]
        beforeCursors = [:]
        serverHasOlder = []
        reconcileAtTurnEnd = []
        needsHistoryRefresh = []
        outbox = []
        trackedOutboxBlobs = []
        running = false
        hasOlder = false
        loadingHistory = false
    }

    func show(_ error: Error) {
        if let service = error as? ServiceError { errorMessage = service.localizedDescription }
        else if error is CancellationError { return }
        else { errorMessage = "连不上服务器，请检查网络和服务器地址" }
    }

    static func pairingCode(in input: String) -> String? {
        guard let fragment = URLComponents(string: input.trimmingCharacters(in: .whitespacesAndNewlines))?.fragment else { return nil }
        let query = URLComponents(string: "https://pair.invalid/?" + fragment)
        return query?.queryItems?.first(where: { $0.name == "pair" })?.value?.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
