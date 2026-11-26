import Foundation

private final class FormatterBox: @unchecked Sendable {
    private let lock = NSLock()
    private let precise: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    private let plain = ISO8601DateFormatter()
    private let monthDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日"
        return formatter
    }()
    private let yearMonthDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "yyyy年M月d日"
        return formatter
    }()

    func parseISO8601(_ text: String) -> Date? {
        lock.lock()
        defer { lock.unlock() }
        return precise.date(from: text) ?? plain.date(from: text)
    }

    func formatDay(_ date: Date, sameYear: Bool) -> String {
        lock.lock()
        defer { lock.unlock() }
        return (sameYear ? monthDay : yearMonthDay).string(from: date)
    }
}

/// Tolerant list projection shared by feature screens. Nulls and malformed rows never poison a list.
struct TabRecord: Identifiable, Equatable {
    let id: String
    let value: JSONValue
    subscript(_ key: String) -> JSONValue { value[key] }
    var title: String { value["title"].nonemptyString ?? value["name"].nonemptyString ?? "未命名" }
}

enum TabRecords {
    static func items(_ payload: JSONValue, key: String, identity: String = "id") -> [TabRecord] {
        var seen = Set<String>()
        return payload[key].array.compactMap { row in
            guard !row.object.isEmpty, let id = row[identity].nonemptyString, seen.insert(id).inserted else { return nil }
            return TabRecord(id: id, value: row)
        }
    }

    // Formatters are slow to make and these run for every row on every redraw: made once, lock-guarded.
    private static let formatters = FormatterBox()

    static func date(_ value: JSONValue) -> Date? {
        if let ms = value.doubleValue { return Date(timeIntervalSince1970: ms / 1_000) }
        guard let text = value.nonemptyString else { return nil }
        return formatters.parseISO8601(text)
    }

    static func day(_ value: JSONValue) -> String {
        guard let date = date(value) else { return "其他日期" }
        if Calendar.current.isDateInToday(date) { return "今天" }
        if Calendar.current.isDateInYesterday(date) { return "昨天" }
        let sameYear = Calendar.current.component(.year, from: date) == Calendar.current.component(.year, from: Date())
        return formatters.formatDay(date, sameYear: sameYear)
    }

    static func ago(_ value: JSONValue) -> String {
        guard let date = date(value) else { return "" }
        let minutes = max(0, Int(Date().timeIntervalSince(date) / 60))
        if minutes < 1 { return "刚刚" }
        if minutes < 60 { return "\(minutes)分钟" }
        if minutes < 1_440 { return "\(minutes / 60)小时" }
        return "\(minutes / 1_440)天"
    }
}

/// One indexed document stored in the personal Knowledge Vault (`GET /api/webui/companion/knowledge`).
struct KnowledgeDocument: Identifiable, Codable, Equatable, Sendable {
    var id: String
    var title: String
    var filename: String
    var collection: String
    var mime: String
    var sizeBytes: Int
    var charCount: Int
    var chunkCount: Int
    var origin: String
    var createdAt: Double
    var updatedAt: Double

    var originLabel: String {
        switch origin {
        case "creation": return "作品导入"
        case "local": return "本地导入"
        default: return "上传"
        }
    }

    init(id: String, title: String, filename: String, collection: String = "默认", mime: String = "text/plain",
         sizeBytes: Int = 0, charCount: Int = 0, chunkCount: Int = 0, origin: String = "upload",
         createdAt: Double = 0, updatedAt: Double = 0) {
        self.id = id
        self.title = title
        self.filename = filename
        self.collection = collection
        self.mime = mime
        self.sizeBytes = sizeBytes
        self.charCount = charCount
        self.chunkCount = chunkCount
        self.origin = origin
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    init(json: JSONValue) {
        let filename = json["filename"].nonemptyString ?? "未命名文档"
        self.init(
            id: json["id"].nonemptyString ?? UUID().uuidString,
            title: json["title"].nonemptyString ?? filename,
            filename: filename,
            collection: json["collection"].nonemptyString ?? "默认",
            mime: json["mime"].nonemptyString ?? "text/plain",
            sizeBytes: json["size_bytes"].intValue ?? json["sizeBytes"].intValue ?? 0,
            charCount: json["char_count"].intValue ?? json["charCount"].intValue ?? 0,
            chunkCount: json["chunk_count"].intValue ?? json["chunkCount"].intValue ?? 0,
            origin: json["origin"].nonemptyString ?? "upload",
            createdAt: json["created_at"].doubleValue ?? json["createdAt"].doubleValue ?? 0,
            updatedAt: json["updated_at"].doubleValue ?? json["updatedAt"].doubleValue ?? 0
        )
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }
}

/// One FTS5/hybrid search hit from the Knowledge Vault.
struct KnowledgeSearchHit: Identifiable, Codable, Equatable, Sendable {
    var docId: String
    var title: String
    var filename: String
    var collection: String
    var chunkIndex: Int
    var heading: String
    var snippet: String
    var content: String
    var score: Double
    var matches: [[Int]]

    var id: String { "\(docId)#\(chunkIndex)" }

    init(docId: String, title: String, filename: String, collection: String = "默认", chunkIndex: Int = 0,
         heading: String = "", snippet: String = "", content: String = "", score: Double = 0, matches: [[Int]] = []) {
        self.docId = docId
        self.title = title
        self.filename = filename
        self.collection = collection
        self.chunkIndex = chunkIndex
        self.heading = heading
        self.snippet = snippet
        self.content = content
        self.score = score
        self.matches = matches
    }

    init(json: JSONValue) {
        let parsedMatches: [[Int]] = json["matches"].array.compactMap { entry in
            let pair = entry.array
            guard pair.count >= 2, let start = pair[0].intValue, let end = pair[1].intValue, end > start else { return nil }
            return [start, end]
        }
        let filename = json["filename"].nonemptyString ?? ""
        self.init(
            docId: json["doc_id"].nonemptyString ?? json["docId"].nonemptyString ?? "",
            title: json["title"].nonemptyString ?? (filename.isEmpty ? "知识片段" : filename),
            filename: filename,
            collection: json["collection"].nonemptyString ?? "默认",
            chunkIndex: json["chunk_index"].intValue ?? json["chunkIndex"].intValue ?? 0,
            heading: json["heading"].stringValue,
            snippet: json["snippet"].nonemptyString ?? json["content"].stringValue,
            content: json["content"].nonemptyString ?? json["snippet"].stringValue,
            score: json["score"].doubleValue ?? 0,
            matches: parsedMatches
        )
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }
}

struct KnowledgeStats: Codable, Equatable, Sendable {
    var docCount: Int
    var chunkCount: Int
    var totalBytes: Int

    init(docCount: Int = 0, chunkCount: Int = 0, totalBytes: Int = 0) {
        self.docCount = docCount
        self.chunkCount = chunkCount
        self.totalBytes = totalBytes
    }

    init(json: JSONValue) {
        self.init(
            docCount: json["doc_count"].intValue ?? json["docCount"].intValue ?? 0,
            chunkCount: json["chunk_count"].intValue ?? json["chunkCount"].intValue ?? 0,
            totalBytes: json["total_bytes"].intValue ?? json["totalBytes"].intValue ?? 0
        )
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }
}

struct KnowledgePayload: Codable, Equatable, Sendable {
    var documents: [KnowledgeDocument]
    var collections: [String]
    var collectionCounts: [String: Int]
    var stats: KnowledgeStats
    var hits: [KnowledgeSearchHit]?

    init(documents: [KnowledgeDocument] = [], collections: [String] = [],
         collectionCounts: [String: Int] = [:], stats: KnowledgeStats = KnowledgeStats(),
         hits: [KnowledgeSearchHit]? = nil) {
        self.documents = documents
        self.collections = collections
        self.collectionCounts = collectionCounts
        self.stats = stats
        self.hits = hits
    }

    init(json: JSONValue) {
        let docs = json["documents"].records.map(KnowledgeDocument.init(json:))
        let cols = json["collections"].array.compactMap(\.nonemptyString)
        var counts: [String: Int] = [:]
        for (key, value) in json["collection_counts"].object {
            if let count = value.intValue { counts[key] = count }
        }
        let stats = KnowledgeStats(json: json["stats"])
        let parsedHits: [KnowledgeSearchHit]?
        if case .array = json["hits"] {
            parsedHits = json["hits"].records.map(KnowledgeSearchHit.init(json:))
        } else {
            parsedHits = nil
        }
        self.init(documents: docs, collections: cols, collectionCounts: counts, stats: stats, hits: parsedHits)
    }

    init(from decoder: Decoder) throws {
        self.init(json: try JSONValue(from: decoder))
    }
}
