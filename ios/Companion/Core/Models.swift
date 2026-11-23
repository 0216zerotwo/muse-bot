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
        if url.hasPrefix("data:image/") || ["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico", "svg", "tif", "tiff", "heic", "heif"].contains(ext) { self.kind = "image" }
        else if url.hasPrefix("data:video/") || ["mp4", "webm", "mov", "m4v", "avi", "mkv", "3gp"].contains(ext) { self.kind = "video" }
        else { self.kind = ["image", "video", "file"].contains(kind ?? "") ? (kind ?? "file") : "file" }
    }

    init(json: JSONValue) {
        self.init(url: json.string ?? json["url"].stringValue, name: json["name"].nonemptyString, kind: json["kind"].string)
    }

    init(from decoder: Decoder) throws { self.init(json: try JSONValue(from: decoder)) }
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
