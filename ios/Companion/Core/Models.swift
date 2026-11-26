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
