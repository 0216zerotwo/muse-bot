import Foundation

struct CardButton: Equatable, Identifiable, Sendable {
    var label: String
    var send: String?
    var primary: Bool
    var id: String { label + "|" + (send ?? "") }
    var message: String { send ?? label }
}

struct FormField: Equatable, Identifiable, Sendable {
    var name: String
    var label: String
    var kind: String
    var options: [String]
    var min: Double?
    var max: Double?
    var step: Double?
    var unit: String?
    var placeholder: String?
    var value: JSONValue
    var required: Bool
    var id: String { name }
}

struct ReplyCard: Equatable, Sendable {
    var type: String
    var data: JSONValue
    var title: String? { data["title"].nonemptyString }
    var items: [JSONValue] { data["items"].array }
    var options: [CardButton] {
        (type == "buttons" ? items : data["options"].array).compactMap { value in
            guard let label = value["label"].nonemptyString ?? value.nonemptyString else { return nil }
            return CardButton(label: label, send: value["send"].nonemptyString, primary: value["primary"].boolValue)
        }
    }
    var fields: [FormField] {
        data["fields"].array.enumerated().compactMap { index, value in
            guard let label = value["label"].nonemptyString else { return nil }
            return FormField(name: value["name"].nonemptyString ?? "field\(index)", label: label,
                             kind: value["kind"].nonemptyString ?? "text",
                             options: value["options"].array.compactMap(\.nonemptyString),
                             min: value["min"].doubleValue, max: value["max"].doubleValue, step: value["step"].doubleValue,
                             unit: value["unit"].nonemptyString, placeholder: value["placeholder"].nonemptyString,
                             value: value["value"], required: value["required"].boolValue)
        }
    }
}

enum ReplyBlock: Equatable, Sendable {
    case bubble(String)
    case card(ReplyCard)
    case pending(String?)
}

/// A parsed message, kept by `ReplyContent.blocks` (NSCache needs a class).
private final class ParsedBlocks {
    let blocks: [ReplyBlock]
    init(_ blocks: [ReplyBlock]) { self.blocks = blocks }
}

enum ReplyContent {
    // Views redraw many times a second while a reply streams: patterns are compiled once and a
    // finished message is parsed once. NSCache is thread-safe and gives memory back under pressure.
    private static let patterns = NSCache<NSString, NSRegularExpression>()
    private static let parsed: NSCache<NSString, ParsedBlocks> = {
        let cache = NSCache<NSString, ParsedBlocks>()
        cache.countLimit = 400
        return cache
    }()

    private static func matches(_ pattern: String, _ text: String, options: NSRegularExpression.Options = []) -> [NSTextCheckingResult] {
        let key = "\(options.rawValue)|\(pattern)" as NSString
        let expression: NSRegularExpression
        if let kept = patterns.object(forKey: key) { expression = kept }
        else {
            guard let made = try? NSRegularExpression(pattern: pattern, options: options) else { return [] }
            patterns.setObject(made, forKey: key)
            expression = made
        }
        return expression.matches(in: text, range: NSRange(text.startIndex..., in: text))
    }
    private static func test(_ pattern: String, _ text: String) -> Bool { !matches(pattern, text).isEmpty }
    private static func substring(_ text: String, _ range: NSRange) -> String {
        guard let range = Range(range, in: text) else { return "" }
        return String(text[range])
    }
    private static func trim(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// Lists, fenced code and headings stay together; long replies retain intro/body/closing.
    static func chunkReply(_ content: String) -> [String] {
        var paragraphs: [String] = []
        var current: [String] = []
        var inFence = false
        for line in content.components(separatedBy: "\n") {
            if test(#"^\s*(```|~~~)"#, line) { inFence.toggle() }
            if !inFence && trim(line).isEmpty {
                if !current.isEmpty { paragraphs.append(current.joined(separator: "\n")) }
                current = []
            } else { current.append(line) }
        }
        if !current.isEmpty { paragraphs.append(current.joined(separator: "\n")) }
        var merged: [String] = []
        for paragraph in paragraphs {
            if isMediaOnly(paragraph) || isBareFence(paragraph) { continue }
            let previous = merged.last
            let list = #"^\s*(?:[-*+]|\d+[.)])\s"#
            let continuesList = previous.map { test(list, paragraph) && test(list, $0.components(separatedBy: "\n").last ?? "") } ?? false
            let followsHeading = previous.map { test(#"^\s*#{1,6}\s"#, $0) && !$0.contains("\n") } ?? false
            if let previous, continuesList || followsHeading { merged[merged.count - 1] = previous + "\n\n" + paragraph }
            else { merged.append(paragraph) }
        }
        if merged.count <= 4 { return merged }
        return [merged[0], merged.dropFirst().dropLast().joined(separator: "\n\n"), merged[merged.count - 1]]
    }

    /// Fence marks with nothing inside (a model sometimes closes a card twice): no empty code bubble.
    private static func isBareFence(_ paragraph: String) -> Bool {
        paragraph.components(separatedBy: "\n").allSatisfy { line in
            let text = trim(line)
            return text.isEmpty || test(#"^(```|~~~)[\w-]*$"#, text)
        }
    }

    static func blocks(_ content: String, streaming: Bool = false) -> [ReplyBlock] {
        let key = ((streaming ? "s|" : "d|") + content) as NSString
        if let kept = parsed.object(forKey: key) { return kept.blocks }
        let result = parseBlocks(content, streaming: streaming)
        parsed.setObject(ParsedBlocks(result), forKey: key)
        return result
    }

    private static func parseBlocks(_ content: String, streaming: Bool) -> [ReplyBlock] {
        let ns = content as NSString
        var blocks: [ReplyBlock] = []
        var last = 0
        let fences = matches(#"```(card|json|ui)[ \t]*\n([\s\S]*?)(?:\n```|$)"#, content)
        func appendText(_ text: String) { blocks += chunkReply(trim(text)).map(ReplyBlock.bubble) }
        for match in fences {
            appendText(ns.substring(with: NSRange(location: last, length: match.range.location - last)))
            last = NSMaxRange(match.range)
            let raw = ns.substring(with: match.range)
            let kind = substring(content, match.range(at: 1))
            let body = substring(content, match.range(at: 2))
            guard raw.hasSuffix("\n```") else {
                if streaming { blocks.append(.pending(kind == "ui" ? "ui" : nil)) }
                continue
            }
            if kind == "ui" {
                if !trim(body).isEmpty { blocks.append(.card(ReplyCard(type: "ui", data: .object(["type": .string("ui"), "html": .string(body)])))) }
                continue
            }
            if let data = body.data(using: .utf8), let value = try? JSONValue.parse(data), let card = parseCard(value) {
                blocks.append(.card(card))
            } else if kind == "json" { appendText(raw) }
        }
        if last < ns.length { appendText(ns.substring(from: last)) }
        return blocks
    }

    static func textWithoutCards(_ content: String) -> String {
        let ns = content as NSString
        var pieces: [String] = []
        var last = 0
        for match in matches(#"```(card|json|ui)[ \t]*\n([\s\S]*?)(?:\n```|$)"#, content) {
            let before = trim(ns.substring(with: NSRange(location: last, length: match.range.location - last)))
            if !before.isEmpty { pieces.append(before) }
            last = NSMaxRange(match.range)
            let raw = ns.substring(with: match.range)
            if substring(content, match.range(at: 1)) == "json", raw.hasSuffix("\n```") {
                let body = substring(content, match.range(at: 2))
                let value = body.data(using: .utf8).flatMap { try? JSONValue.parse($0) }
                if value.flatMap(parseCard) == nil { pieces.append(raw) }
            }
        }
        let rest = trim(ns.substring(from: last))
        if !rest.isEmpty && !isBareFence(rest) { pieces.append(rest) }
        return pieces.joined(separator: "\n\n")
    }

    private static func button(_ value: JSONValue) -> JSONValue? {
        let direct = value.string?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let label = (direct?.isEmpty == false ? direct : nil) ?? value["label"].nonemptyString else { return nil }
        var result: [String: JSONValue] = ["label": .string(label), "primary": .bool(value["primary"].boolValue)]
        if let send = value["send"].nonemptyString { result["send"] = .string(send) }
        return .object(result)
    }

    private static func displayStrings(_ value: JSONValue, keys: [String]) -> JSONValue {
        var fields = value.object
        for key in keys {
            if let text = value[key].nonemptyString { fields[key] = .string(text) }
            else { fields.removeValue(forKey: key) }
        }
        return .object(fields)
    }

    /// Validation and limits match replyCards.ts; invalid card fences disappear.
    static func parseCard(_ raw: JSONValue) -> ReplyCard? {
        let type = raw["type"].stringValue
        var data = raw.object
        switch type {
        case "buttons", "choices":
            let key = type == "buttons" ? "items" : "options"
            let values = raw[key].array.compactMap(button)
            guard !values.isEmpty else { return nil }
            data[key] = .array(Array(values.prefix(type == "buttons" ? 6 : 12)))
        case "pictures":
            let values = raw["items"].array.filter {
                guard let url = $0["url"].nonemptyString, $0["label"].nonemptyString != nil else { return false }
                return url.hasPrefix("/") || url.hasPrefix("https://")
            }
            guard !values.isEmpty else { return nil }
            data["items"] = .array(Array(values.prefix(6)))
        case "weather":
            guard raw["place"].nonemptyString != nil, raw["temp"].nonemptyString != nil else { return nil }
            data["details"] = .array(Array(raw["details"].array.filter { $0["label"].nonemptyString != nil && $0["value"].nonemptyString != nil }.prefix(6)))
            data["hours"] = .array(Array(raw["hours"].array.filter { $0["time"].nonemptyString != nil && $0["temp"].nonemptyString != nil }.prefix(8)))
        case "timeline", "places", "trains":
            let key = type == "timeline" ? "title" : (type == "places" ? "name" : "code")
            let items = raw["items"].array.filter { value in
                value[key].nonemptyString != nil && (type != "trains" || (value["depart"].nonemptyString != nil && value["arrive"].nonemptyString != nil))
            }
            guard !items.isEmpty else { return nil }
            data["items"] = .array(Array(items.prefix(type == "timeline" ? items.count : (type == "places" ? 8 : 10))))
        case "compare":
            let options = raw["options"].array.compactMap(\.nonemptyString)
            let rows = raw["rows"].array.filter { $0["label"].nonemptyString != nil }
            guard options.count >= 2, !rows.isEmpty else { return nil }
            data["options"] = .array(options.prefix(4).map(JSONValue.string))
            data["rows"] = .array(rows)
            if let best = raw["best"].doubleValue, best >= 0, best < Double(options.count) { data["best"] = .number(best) }
            else { data.removeValue(forKey: "best") }
        case "checklist":
            let items: [JSONValue] = raw["items"].array.compactMap { value in
                guard let text = value.string ?? value["text"].nonemptyString, !text.isEmpty else { return nil }
                return .object(["text": .string(text), "done": .bool(value["done"].boolValue)])
            }
            guard !items.isEmpty else { return nil }
            data["items"] = .array(items)
        case "table":
            guard !raw["columns"].array.isEmpty, !raw["rows"].array.isEmpty else { return nil }
            data["columns"] = .array(raw["columns"].array.map { .string($0.nonemptyString ?? "") })
            data["rows"] = .array(raw["rows"].array.prefix(30).map { .array($0.array.map { .string($0.nonemptyString ?? "") }) })
        case "form":
            let fields: [JSONValue] = raw["fields"].array.enumerated().compactMap { index, value in
                guard value["label"].nonemptyString != nil else { return nil }
                let kinds = ["text", "number", "select", "chips", "multi", "slider", "date", "time"]
                let kind = kinds.contains(value["kind"].stringValue) ? value["kind"].stringValue : "text"
                let options = value["options"].array.compactMap(\.nonemptyString)
                if ["select", "chips", "multi"].contains(kind) && options.isEmpty { return nil }
                var field = value.object
                field["name"] = .string(value["name"].nonemptyString ?? "field\(index)")
                field["kind"] = .string(kind)
                field["options"] = .array(options.map(JSONValue.string))
                return .object(field)
            }
            guard !fields.isEmpty else { return nil }
            data["fields"] = .array(Array(fields.prefix(8)))
        case "widget":
            guard raw["title"].nonemptyString != nil, raw["url"].stringValue.hasPrefix("/api/webui/companion/widgets/") else { return nil }
            if let height = raw["height"].doubleValue, height != 0 { data["height"] = .number(min(max(height, 160), 720)) }
        case "connect":
            guard test(#"^[a-z0-9-]{1,40}$"#, raw["service"].stringValue) else { return nil }
        case "credential":
            let kind = raw["kind"].stringValue
            guard ["login", "api_key"].contains(kind), raw["service"].nonemptyString != nil else { return nil }
            if kind == "api_key" && !test(#"^[A-Z][A-Z0-9_]{2,63}$"#, raw["env"].stringValue) { return nil }
        default:
            return nil
        }
        // The web parser's str() accepts both strings and numbers. Native views
        // can consistently consume the resulting display values as strings.
        data = displayStrings(.object(data), keys: ["title", "submit", "place", "when", "temp", "summary", "icon", "high", "low", "from", "to", "date", "url", "service", "reason", "origin", "env"]).object
        for key in ["items", "details", "hours", "rows", "fields"] {
            guard let value = data[key], case .array(let entries) = value else { continue }
            data[key] = .array(entries.map { entry in
                guard case .object = entry else { return entry }
                var normalized = displayStrings(entry, keys: ["label", "send", "url", "time", "title", "detail", "name", "address", "note", "rating", "code", "depart", "arrive", "duration", "price", "text", "temp", "icon", "unit", "placeholder"]).object
                if key == "details" { normalized["value"] = .string(entry["value"].nonemptyString ?? "") }
                if key == "rows", type == "compare" {
                    normalized["values"] = .array(entry["values"].array.map { .string($0.nonemptyString ?? "—") })
                }
                if type == "trains", key == "items" {
                    normalized["seats"] = .array(entry["seats"].array.filter { $0["name"].nonemptyString != nil }.map { displayStrings($0, keys: ["name", "left"]) })
                }
                return .object(normalized)
            })
        }
        return ReplyCard(type: type, data: .object(data))
    }

    static func formMessage(card: ReplyCard, values: [String: JSONValue]) -> String {
        let lines = card.fields.compactMap { field -> String? in
            let value = values[field.name] ?? .null
            let text: String
            if case .array(let list) = value { text = list.compactMap(\.nonemptyString).joined(separator: "、") }
            else { text = value.nonemptyString ?? "" }
            guard !text.isEmpty else { return nil }
            return field.label + "：" + text + (field.kind != "multi" ? field.unit ?? "" : "")
        }
        return ([card.title ?? "我填好了"] + lines).joined(separator: "\n")
    }

    private static func mediaKind(_ url: String) -> String? {
        guard let parsed = URL(string: url), ["http", "https"].contains(parsed.scheme?.lowercased() ?? "") else { return nil }
        let ext = parsed.pathExtension.lowercased()
        if ["mp4", "m4v", "mov", "webm"].contains(ext) { return "video" }
        if ["png", "jpg", "jpeg", "gif", "webp", "avif"].contains(ext) { return "image" }
        return nil
    }

    private static func mediaURLs(_ content: String) -> [String] {
        let stripped = content.replacingOccurrences(of: #"!\[[^\]]*\]\([^)]*\)"#, with: "", options: .regularExpression)
        return matches(#"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+"#, stripped).map {
            substring(stripped, $0.range).replacingOccurrences(of: #"[.,;:!?]+$"#, with: "", options: .regularExpression)
        }.filter { mediaKind($0) != nil }
    }

    static func linkedMedia(_ content: String) -> [MediaAttachment] {
        var seen: Set<String> = []
        return mediaURLs(content).filter { seen.insert($0).inserted }.map { MediaAttachment(url: $0, kind: mediaKind($0)) }
    }

    private static func isMediaOnly(_ paragraph: String) -> Bool {
        let urls = mediaURLs(paragraph)
        guard !urls.isEmpty else { return false }
        var rest = paragraph
        for match in matches(#"\[[^\]]*\]\(([^)\s]+)\)"#, paragraph).reversed() {
            if mediaKind(substring(paragraph, match.range(at: 1))) != nil, let range = Range(match.range, in: rest) { rest.removeSubrange(range) }
        }
        for url in urls { rest = rest.replacingOccurrences(of: url, with: "") }
        return rest.replacingOccurrences(of: #"[\s\-*+>:：.,。、<>]"#, with: "", options: .regularExpression).isEmpty
    }
}

enum UserQuote {
    static let marker = "> [!QUOTE]"
    private static func normalize(_ content: String) -> String { content.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n") }

    static func format(content: String, quotedContext: String?) -> String {
        let body = content.trimmingCharacters(in: .whitespacesAndNewlines)
        let quote = normalize(quotedContext ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !quote.isEmpty, !body.hasPrefix("/") else { return body }
        let block = quote.components(separatedBy: "\n").map { $0.isEmpty ? ">" : "> " + $0 }.joined(separator: "\n")
        return marker + "\n" + block + (body.isEmpty ? "" : "\n\n" + body)
    }

    static func parse(_ content: String) -> (quote: String?, content: String) {
        let normalized = normalize(content)
        guard normalized.hasPrefix(marker + "\n") else { return (nil, content) }
        let remainder = String(normalized.dropFirst(marker.count + 1))
        let parts = remainder.components(separatedBy: "\n\n")
        let lines = (parts.first ?? "").components(separatedBy: "\n")
        guard !lines.isEmpty, lines.allSatisfy({ $0 == ">" || $0.hasPrefix("> ") }) else { return (nil, content) }
        let quote = lines.map { $0 == ">" ? "" : String($0.dropFirst(2)) }.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !quote.isEmpty else { return (nil, content) }
        return (quote, parts.dropFirst().joined(separator: "\n\n"))
    }
}
