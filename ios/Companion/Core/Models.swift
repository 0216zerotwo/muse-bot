import Foundation

/// Wire objects stay lossless as the companion adds optional fields.
enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([JSONValue].self) { self = .array(value) }
        else { self = .object(try container.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    subscript(_ key: String) -> JSONValue { object[key] ?? .null }
    var object: [String: JSONValue] { if case .object(let value) = self { return value }; return [:] }
    var array: [JSONValue] { if case .array(let value) = self { return value }; return [] }
    var string: String? { if case .string(let value) = self { return value }; return nil }
    /// Display fields occasionally arrive as numbers; structured/card validation can
    /// still use `string` when the wire contract specifically requires a string.
    var stringValue: String {
        if case .string(let value) = self { return value }
        return nonemptyString ?? ""
    }
    var boolValue: Bool {
        switch self {
        case .bool(let value): return value
        case .number(let value): return value != 0
        case .string(let value): return ["true", "1", "yes"].contains(value.lowercased())
        default: return false
        }
    }
    /// Skip malformed list entries while retaining valid records on the screen.
    var records: [JSONValue] { array.filter { if case .object = $0 { return true }; return false } }
    var doubleValue: Double? {
        if case .number(let value) = self { return value.isFinite ? value : nil }
        if let value = string, let number = Double(value), number.isFinite { return number }
        return nil
    }
    var intValue: Int? {
        guard let value = doubleValue, value >= Double(Int.min), value < Double(Int.max) else { return nil }
        return Int(value)
    }
    var nonemptyString: String? {
        let value: String
        switch self {
        case .string(let text): value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        case .number(let number): value = number.rounded() == number ? String(format: "%.0f", number) : String(number)
        default: return nil
        }
        return value.isEmpty ? nil : value
    }
    static func parse(_ data: Data) throws -> JSONValue { try JSONDecoder().decode(JSONValue.self, from: data) }
}

struct MediaAttachment: Identifiable, Codable, Equatable, Sendable {
    var kind: String
    var url: String
    var name: String?
    var id: String { url + "|" + (name ?? "") }

    init(url: String, name: String? = nil, kind: String? = nil) {
        self.url = url
        self.name = name
        let candidate = name.flatMap { $0.isEmpty ? nil : $0 } ?? url
        let clean = candidate.components(separatedBy: CharacterSet(charactersIn: "?#")).first ?? ""
        let ext = (clean as NSString).pathExtension.lowercased()
        if ext == "svg" || url.lowercased().hasPrefix("data:image/svg+xml") { self.kind = "file" }
        else if url.hasPrefix("data:image/") || ["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico", "tif", "tiff", "heic", "heif"].contains(ext) { self.kind = "image" }
        else if url.hasPrefix("data:video/") || ["mp4", "webm", "mov", "m4v", "avi", "mkv", "3gp"].contains(ext) { self.kind = "video" }
        else { self.kind = ["image", "video", "file"].contains(kind ?? "") ? (kind ?? "file") : "file" }
    }

    init(json: JSONValue) {
        self.init(url: json.string ?? json["url"].stringValue, name: json["name"].nonemptyString, kind: json["kind"].string)
    }

    init(from decoder: Decoder) throws { self.init(json: try JSONValue(from: decoder)) }
}

/// Structured web or knowledge-base citation attached to an assistant reply (`sources`).
struct CitationSource: Identifiable, Codable, Equatable, Sendable {
    var id: Int
    var title: String
    var url: String
    var domain: String
    var snippet: String

    var isKnowledge: Bool { url.hasPrefix("#knowledge:") }

    var knowledgeDocID: String? {
        guard isKnowledge else { return nil }
        let raw = String(url.dropFirst("#knowledge:".count)).trimmingCharacters(in: .whitespacesAndNewlines)
        return raw.isEmpty ? nil : raw
    }

    var webURL: URL? {
        guard !isKnowledge, let parsed = URL(string: url),
              let scheme = parsed.scheme?.lowercased(), ["http", "https"].contains(scheme) else { return nil }
        return parsed
    }

    var displayDomain: String {
        if !domain.isEmpty { return domain }
        if isKnowledge { return "知识库" }
        if let host = URL(string: url)?.host, !host.isEmpty {
            return host.hasPrefix("www.") && host.count > 4 ? String(host.dropFirst(4)) : host
        }
        return title.isEmpty ? "来源" : title
    }

    init(id: Int, title: String, url: String, domain: String = "", snippet: String = "") {
        self.id = id
        self.title = title
        self.url = url
        self.domain = domain
        self.snippet = snippet
    }

    init(json: JSONValue, fallbackID: Int = 1) {
        let resolvedID = json["id"].intValue ?? fallbackID
        let rawURL = json["url"].nonemptyString ?? ""
        let rawDomain = json["domain"].nonemptyString ?? ""
        let rawTitle = json["title"].nonemptyString ?? (rawDomain.isEmpty ? rawURL : rawDomain)
        let rawSnippet = json["snippet"].nonemptyString ?? json["description"].nonemptyString ?? ""
        self.init(id: resolvedID, title: rawTitle, url: rawURL, domain: rawDomain, snippet: rawSnippet)
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }

    static func parseList(_ value: JSONValue) -> [CitationSource]? {
        let records = value.records
        guard !records.isEmpty else { return nil }
        var seen: Set<String> = []
        var items: [CitationSource] = []
        for (offset, row) in records.enumerated() {
            let source = CitationSource(json: row, fallbackID: offset + 1)
            guard !source.url.isEmpty || !source.title.isEmpty else { continue }
            let dedupKey = source.url.isEmpty ? "\(source.id)|\(source.title)" : source.url
            if seen.insert(dedupKey).inserted {
                items.append(source)
            }
        }
        return items.isEmpty ? nil : items
    }
}

/// One long-term memory entry returned by `/api/webui/companion/memory` (supports optional `pinned`).
struct MemoryItem: Identifiable, Codable, Equatable, Sendable {
    var id: String
    var file: String
    var line: String
    var text: String
    var pinned: Bool

    init(id: String, file: String, line: String, text: String, pinned: Bool = false) {
        self.id = id
        self.file = file
        self.line = line
        self.text = text
        self.pinned = pinned
    }

    init(json: JSONValue, defaultFile: String = "USER.md") {
        let file = json["file"].nonemptyString ?? defaultFile
        let line = json["line"].string ?? json["text"].stringValue
        let text = json["text"].nonemptyString ?? line.trimmingCharacters(in: .whitespacesAndNewlines)
        let id = json["id"].nonemptyString ?? "\(file)|\(line)"
        let pinned = json["pinned"].boolValue
        self.init(id: id, file: file, line: line, text: text, pinned: pinned)
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }
}

/// One grouped memory section (`USER.md` / `MEMORY.md`).
struct MemorySection: Identifiable, Codable, Equatable, Sendable {
    var file: String
    var title: String
    var items: [MemoryItem]
    var id: String { file }

    init(file: String, title: String, items: [MemoryItem]) {
        self.file = file
        self.title = title
        self.items = items
    }

    init(json: JSONValue) {
        let file = json["file"].nonemptyString ?? "USER.md"
        let title = json["title"].nonemptyString ?? file
        let parsed = json["items"].records.map { MemoryItem(json: $0, defaultFile: file) }
            .filter { !$0.text.isEmpty }
        self.init(file: file, title: title, items: parsed)
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }

    static func parseSections(_ payload: JSONValue) -> [MemorySection] {
        payload["sections"].records.map(MemorySection.init(json:)).filter { !$0.items.isEmpty }
    }
}

/// Contextual follow-up prompt suggestions returned by `GET /api/sessions/{key}/followups`.
struct FollowupsResponse: Codable, Equatable, Sendable {
    var chatId: String
    var turnId: String
    var suggestions: [String]

    init(chatId: String = "", turnId: String = "", suggestions: [String] = []) {
        self.chatId = chatId
        self.turnId = turnId
        self.suggestions = suggestions
    }

    init(json: JSONValue) {
        let chatId = json["chat_id"].nonemptyString ?? json["chatId"].nonemptyString ?? ""
        let turnId = json["turn_id"].nonemptyString ?? json["turnId"].nonemptyString ?? ""
        var seen: Set<String> = []
        let items: [String] = json["suggestions"].array.compactMap { entry in
            guard let text = entry.nonemptyString else { return nil }
            let key = text.lowercased()
            guard seen.insert(key).inserted else { return nil }
            return text
        }
        self.init(chatId: chatId, turnId: turnId, suggestions: Array(items.prefix(3)))
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }
}

struct ChatMessage: Identifiable, Codable, Equatable, Sendable {
    var id: String
    var role: String
    var content: String
    var createdAt: Double
    var kind: String?
    var isStreaming = false
    var reasoning: String?
    var reasoningStreaming = false
    var media: [MediaAttachment] = []
    var source: JSONValue?
    var sources: [CitationSource]? = nil
    var toolEvents: [JSONValue] = []
    var turnId: String?
    var turnPhase: String?
    var turnSeq: Int?
    var deliveryStatus: String?
    var activityKind: String?
    var sessionMessageID: String?

    init(id: String = UUID().uuidString, role: String, content: String, createdAt: Double = Date().timeIntervalSince1970 * 1_000) {
        self.id = id
        self.role = role
        self.content = content
        self.createdAt = createdAt
    }

    init(json: JSONValue) {
        self.init(id: json["id"].nonemptyString ?? UUID().uuidString,
                  role: json["role"].nonemptyString ?? "assistant",
                  content: json["content"].string ?? json["text"].stringValue,
                  createdAt: json["createdAt"].doubleValue ?? json["created_at_ms"].doubleValue ?? 0)
        kind = json["kind"].string
        isStreaming = json["isStreaming"].boolValue
        reasoning = json["reasoning"].string
        reasoningStreaming = json["reasoningStreaming"].boolValue
        media = (json["media"].array + json["images"].array).map(MediaAttachment.init(json:)).filter { !$0.url.isEmpty }
        source = json["source"] == .null ? nil : json["source"]
        sources = CitationSource.parseList(json["sources"])
        toolEvents = json["toolEvents"].array
        turnId = json["turnId"].string ?? json["turn_id"].string
        turnPhase = json["turnPhase"].string
        turnSeq = json["turnSeq"].intValue
        deliveryStatus = json["deliveryStatus"].string
        activityKind = json["activityKind"].string
        sessionMessageID = json["sessionMessageID"].nonemptyString ?? json["sessionMessage"]["message_id"].nonemptyString
    }

    init(from decoder: Decoder) throws { self.init(json: try JSONValue(from: decoder)) }
}

/// Live `/goal` state pushed over the chat WebSocket (`goal_state` frame), matching `GoalBanner.tsx`.
struct GoalState: Codable, Equatable, Sendable {
    var active: Bool
    var status: String
    var objective: String
    var uiSummary: String?
    var recap: String?

    var isBlocked: Bool { status == "blocked" }

    init(active: Bool = false, status: String = "idle", objective: String = "", uiSummary: String? = nil, recap: String? = nil) {
        self.active = active
        self.status = status
        self.objective = objective
        self.uiSummary = uiSummary
        self.recap = recap
    }

    init(json: JSONValue) {
        let active = json["active"].boolValue
        let rawStatus = json["status"].nonemptyString ?? (active ? "active" : "idle")
        self.init(
            active: active,
            status: rawStatus,
            objective: json["objective"].nonemptyString ?? "",
            uiSummary: json["ui_summary"].nonemptyString,
            recap: json["recap"].nonemptyString
        )
    }
}

/// Session summary decoded from `GET /api/sessions` (all Wave 3 metadata fields are Optional for fixture compatibility).
struct ChatSummary: Identifiable, Codable, Equatable, Sendable {
    var key: String
    var title: String?
    var preview: String?
    var createdAt: String?
    var updatedAt: String?
    var helper: String?
    var group: Bool?
    var pinned: Bool?
    var archived: Bool?
    var empty: Bool?
    var modelPreset: String?
    var reasoningEffort: String?

    enum CodingKeys: String, CodingKey {
        case key, title, preview, helper, group, pinned, archived, empty
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case modelPreset = "model_preset"
        case reasoningEffort = "reasoning_effort"
    }

    var id: String {
        key.hasPrefix("websocket:") ? String(key.dropFirst("websocket:".count)) : key
    }

    var isPinned: Bool { pinned ?? false }
    var isArchived: Bool { archived ?? false }
    var isEmptyChat: Bool {
        if let empty { return empty }
        return (preview ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var displayTitle: String {
        let trimmed = (title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "新的旁聊" : trimmed
    }

    init(
        key: String,
        title: String? = nil,
        preview: String? = nil,
        createdAt: String? = nil,
        updatedAt: String? = nil,
        helper: String? = nil,
        group: Bool? = nil,
        pinned: Bool? = nil,
        archived: Bool? = nil,
        empty: Bool? = nil,
        modelPreset: String? = nil,
        reasoningEffort: String? = nil
    ) {
        self.key = key
        self.title = title
        self.preview = preview
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.helper = helper
        self.group = group
        self.pinned = pinned
        self.archived = archived
        self.empty = empty
        self.modelPreset = modelPreset
        self.reasoningEffort = reasoningEffort
    }

    init(json: JSONValue) {
        let pinnedFlag: Bool? = json["pinned"] == .null ? nil : json["pinned"].boolValue
        let archivedFlag: Bool? = json["archived"] == .null ? nil : json["archived"].boolValue
        let emptyFlag: Bool? = json["empty"] == .null ? nil : json["empty"].boolValue
        let groupFlag: Bool? = json["group"] == .null ? nil : json["group"].boolValue
        self.init(
            key: json["key"].stringValue,
            title: json["title"].string,
            preview: json["preview"].string,
            createdAt: json["created_at"].nonemptyString,
            updatedAt: json["updated_at"].nonemptyString,
            helper: json["helper"].nonemptyString,
            group: groupFlag,
            pinned: pinnedFlag,
            archived: archivedFlag,
            empty: emptyFlag,
            modelPreset: json["model_preset"].nonemptyString,
            reasoningEffort: json["reasoning_effort"].nonemptyString
        )
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        key = try container.decodeIfPresent(String.self, forKey: .key) ?? ""
        title = try container.decodeIfPresent(String.self, forKey: .title)
        preview = try container.decodeIfPresent(String.self, forKey: .preview)
        createdAt = try container.decodeIfPresent(String.self, forKey: .createdAt)
        updatedAt = try container.decodeIfPresent(String.self, forKey: .updatedAt)
        helper = try container.decodeIfPresent(String.self, forKey: .helper)
        group = try container.decodeIfPresent(Bool.self, forKey: .group)
        pinned = try container.decodeIfPresent(Bool.self, forKey: .pinned)
        archived = try container.decodeIfPresent(Bool.self, forKey: .archived)
        empty = try container.decodeIfPresent(Bool.self, forKey: .empty)
        modelPreset = try container.decodeIfPresent(String.self, forKey: .modelPreset)
        reasoningEffort = try container.decodeIfPresent(String.self, forKey: .reasoningEffort)
    }
}

/// Active context window occupancy returned by `GET /api/sessions/{key}/context`.
struct ChatContextStatus: Codable, Equatable, Sendable {
    var chatId: String?
    var estimatedTokens: Int
    var contextWindow: Int
    var usageRatio: Double
    var tier1Threshold: Int?
    var tier2Threshold: Int?
    var hasSummaryCheckpoint: Bool?
    var model: String?
    var modelPreset: String?
    var reasoningEffort: String?

    enum CodingKeys: String, CodingKey {
        case chatId = "chat_id"
        case estimatedTokens = "estimated_tokens"
        case contextWindow = "context_window"
        case usageRatio = "usage_ratio"
        case tier1Threshold = "tier1_threshold"
        case tier2Threshold = "tier2_threshold"
        case hasSummaryCheckpoint = "has_summary_checkpoint"
        case model
        case modelPreset = "model_preset"
        case reasoningEffort = "reasoning_effort"
    }

    var clampedRatio: Double {
        let raw: Double
        if usageRatio.isFinite && usageRatio > 0 {
            raw = usageRatio
        } else if contextWindow > 0 && estimatedTokens > 0 {
            raw = Double(estimatedTokens) / Double(contextWindow)
        } else {
            raw = 0
        }
        return min(1, max(0, raw))
    }

    var percent: Int {
        Int((clampedRatio * 100).rounded())
    }

    var isHighWater: Bool {
        if let tier2 = tier2Threshold, tier2 > 0, estimatedTokens >= tier2 { return true }
        return clampedRatio >= 0.7
    }

    var isElevated: Bool {
        if let tier1 = tier1Threshold, tier1 > 0, estimatedTokens >= tier1 { return true }
        return clampedRatio >= 0.5
    }

    init(
        chatId: String? = nil,
        estimatedTokens: Int = 0,
        contextWindow: Int = 0,
        usageRatio: Double = 0,
        tier1Threshold: Int? = nil,
        tier2Threshold: Int? = nil,
        hasSummaryCheckpoint: Bool? = nil,
        model: String? = nil,
        modelPreset: String? = nil,
        reasoningEffort: String? = nil
    ) {
        self.chatId = chatId
        self.estimatedTokens = estimatedTokens
        self.contextWindow = contextWindow
        self.usageRatio = usageRatio
        self.tier1Threshold = tier1Threshold
        self.tier2Threshold = tier2Threshold
        self.hasSummaryCheckpoint = hasSummaryCheckpoint
        self.model = model
        self.modelPreset = modelPreset
        self.reasoningEffort = reasoningEffort
    }

    init(json: JSONValue) {
        let estimated = json["estimated_tokens"].intValue ?? 0
        let window = json["context_window"].intValue ?? 0
        let ratio = json["usage_ratio"].doubleValue ?? (window > 0 ? Double(estimated) / Double(window) : 0)
        let checkpoint: Bool? = json["has_summary_checkpoint"] == .null ? nil : json["has_summary_checkpoint"].boolValue
        self.init(
            chatId: json["chat_id"].nonemptyString,
            estimatedTokens: max(0, estimated),
            contextWindow: max(0, window),
            usageRatio: ratio.isFinite ? max(0, ratio) : 0,
            tier1Threshold: json["tier1_threshold"].intValue,
            tier2Threshold: json["tier2_threshold"].intValue,
            hasSummaryCheckpoint: checkpoint,
            model: json["model"].nonemptyString,
            modelPreset: json["model_preset"].nonemptyString,
            reasoningEffort: json["reasoning_effort"].nonemptyString
        )
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        chatId = try container.decodeIfPresent(String.self, forKey: .chatId)
        estimatedTokens = max(0, try container.decodeIfPresent(Int.self, forKey: .estimatedTokens) ?? 0)
        contextWindow = max(0, try container.decodeIfPresent(Int.self, forKey: .contextWindow) ?? 0)
        let decodedRatio = try container.decodeIfPresent(Double.self, forKey: .usageRatio)
        if let decodedRatio, decodedRatio.isFinite {
            usageRatio = max(0, decodedRatio)
        } else if contextWindow > 0 {
            usageRatio = Double(estimatedTokens) / Double(contextWindow)
        } else {
            usageRatio = 0
        }
        tier1Threshold = try container.decodeIfPresent(Int.self, forKey: .tier1Threshold)
        tier2Threshold = try container.decodeIfPresent(Int.self, forKey: .tier2Threshold)
        hasSummaryCheckpoint = try container.decodeIfPresent(Bool.self, forKey: .hasSummaryCheckpoint)
        model = try container.decodeIfPresent(String.self, forKey: .model)
        modelPreset = try container.decodeIfPresent(String.self, forKey: .modelPreset)
        reasoningEffort = try container.decodeIfPresent(String.self, forKey: .reasoningEffort)
    }
}

/// Persists per-chat composer text and quoted snippet together so switching chats never loses drafts.
enum ChatDraftPersistence {
    struct Draft: Equatable, Sendable {
        var text: String
        var quote: String?
    }

    static func textKey(server: String, chatID: String) -> String {
        "draft.\(server).\(chatID)"
    }

    static func quoteKey(server: String, chatID: String) -> String {
        "draft.quote.\(server).\(chatID)"
    }

    static func load(server: String, chatID: String, defaults: UserDefaults = .standard) -> Draft {
        guard !chatID.isEmpty else { return Draft(text: "", quote: nil) }
        let text = defaults.string(forKey: textKey(server: server, chatID: chatID)) ?? ""
        let rawQuote = defaults.string(forKey: quoteKey(server: server, chatID: chatID))
        let quote = rawQuote?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false ? rawQuote : nil
        return Draft(text: text, quote: quote)
    }

    static func saveText(_ text: String, server: String, chatID: String, defaults: UserDefaults = .standard) {
        guard !chatID.isEmpty else { return }
        let key = textKey(server: server, chatID: chatID)
        if text.isEmpty {
            defaults.removeObject(forKey: key)
        } else {
            defaults.set(text, forKey: key)
        }
    }

    static func saveQuote(_ quote: String?, server: String, chatID: String, defaults: UserDefaults = .standard) {
        guard !chatID.isEmpty else { return }
        let key = quoteKey(server: server, chatID: chatID)
        if let quote, !quote.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            defaults.set(quote, forKey: key)
        } else {
            defaults.removeObject(forKey: key)
        }
    }

    static func clear(server: String, chatID: String, defaults: UserDefaults = .standard) {
        guard !chatID.isEmpty else { return }
        defaults.removeObject(forKey: textKey(server: server, chatID: chatID))
        defaults.removeObject(forKey: quoteKey(server: server, chatID: chatID))
    }
}

/// Tracks newly completed assistant replies arriving while the user is scrolled up reading history.
struct ChatUnreadTracker: Equatable, Sendable {
    private(set) var unreadCount = 0
    private var seenCompletedIDs: Set<String> = []
    private var highWaterTimestamp: Double = 0

    mutating func reset(for messages: [ChatMessage]) {
        unreadCount = 0
        seenCompletedIDs = Set(completedReplies(in: messages).map(\.id))
        highWaterTimestamp = messages.map(\.createdAt).max() ?? 0
    }

    mutating func ingest(messages: [ChatMessage], followingBottom: Bool, positioned: Bool, paging: Bool) {
        let completed = completedReplies(in: messages)
        if !positioned || followingBottom {
            unreadCount = 0
            seenCompletedIDs = Set(completed.map(\.id))
            highWaterTimestamp = max(highWaterTimestamp, messages.map(\.createdAt).max() ?? 0)
            return
        }
        guard !paging else {
            seenCompletedIDs.formUnion(completed.map(\.id))
            return
        }
        var added = 0
        for msg in completed where !seenCompletedIDs.contains(msg.id) {
            seenCompletedIDs.insert(msg.id)
            if msg.createdAt >= highWaterTimestamp {
                added += 1
                highWaterTimestamp = max(highWaterTimestamp, msg.createdAt)
            }
        }
        unreadCount += added
    }

    private func completedReplies(in messages: [ChatMessage]) -> [ChatMessage] {
        messages.filter { msg in
            msg.role != "user" && !msg.isStreaming && Presentation.isVisibleReply(msg)
        }
    }
}
