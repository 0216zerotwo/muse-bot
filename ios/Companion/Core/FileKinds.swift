import Foundation

/// What a file the companion sent is, for its card in the chat (as Muse shows a document: its first
/// page, then its name and kind): the mark on its icon, the words under its name, how the page is drawn.
enum FileKind: String, Equatable, Sendable {
    case pdf, markdown, text, code, table, web, word, sheet, slides, audio, archive, other

    static func of(_ name: String) -> FileKind {
        switch FileKinds.ext(name) {
        case "pdf": return .pdf
        case "md", "markdown", "mdown", "mkd": return .markdown
        case "txt", "text", "log": return .text
        case "csv", "tsv": return .table
        case "html", "htm", "xhtml": return .web
        case "doc", "docx", "rtf", "pages", "odt": return .word
        case "xls", "xlsx", "numbers", "ods": return .sheet
        case "ppt", "pptx", "key", "odp": return .slides
        case "mp3", "m4a", "aac", "wav", "aif", "aiff", "caf", "flac", "ogg", "opus", "amr": return .audio
        case "zip", "rar", "7z", "tar", "gz", "tgz": return .archive
        case "json", "jsonl", "py", "js", "mjs", "ts", "tsx", "jsx", "swift", "sh", "bash", "zsh", "ps1", "bat",
             "yaml", "yml", "toml", "ini", "cfg", "conf", "xml", "css", "scss", "sql", "c", "h", "cc", "cpp", "hpp",
             "java", "kt", "go", "rs", "rb", "php", "lua", "r", "cs", "vue": return .code
        default: return .other
        }
    }

    /// The words under its name.
    var words: String {
        switch self {
        case .pdf: return "PDF"
        case .markdown, .word: return "文档"
        case .text: return "文本"
        case .code: return "代码"
        case .table, .sheet: return "表格"
        case .web: return "网页"
        case .slides: return "演示文稿"
        case .audio: return "音频"
        case .archive: return "压缩包"
        case .other: return "文件"
        }
    }

    /// Drawn from its own text on this iPhone.
    var isText: Bool { [.markdown, .text, .code, .table].contains(self) }
    /// Its first page is a picture made on this iPhone (PDF, Office and iWork files, web pages).
    var isPage: Bool { [.pdf, .word, .sheet, .slides, .web].contains(self) }
}

/// A file being written in the turn at work: shown as it will look, before it is sent.
struct FileDraft: Equatable, Sendable {
    let id: String
    let name: String
    let content: String
    /// Written (the call has finished); it comes as a file next.
    let done: Bool
}

enum FileKinds {
    static func ext(_ name: String) -> String {
        let clean = name.components(separatedBy: CharacterSet(charactersIn: "?#")).first ?? name
        return (clean as NSString).pathExtension.lowercased()
    }

    /// The mark on its icon: "PDF", "MD", "XLSX" (at most four letters).
    static func badge(_ name: String) -> String {
        let ext = ext(name)
        return ext.isEmpty ? "FILE" : String(ext.uppercased().prefix(4))
    }

    /// Its name without the ending ("阳明山周日徒步计划.md" → "阳明山周日徒步计划").
    static func title(_ name: String) -> String {
        let base = ((name as NSString).lastPathComponent as NSString).deletingPathExtension
        return base.isEmpty ? name : base
    }

    /// "PDF · 4 页 · 312 KB": what it is, its pages, its size (each when known).
    static func subtitle(_ kind: FileKind, pages: Int?, bytes: Int?) -> String {
        var parts = [kind.words]
        if let pages, pages > 0 { parts.append("\(pages) 页") }
        if let bytes, bytes > 0 { parts.append(ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)) }
        return parts.joined(separator: " · ")
    }

    /// A file name that can be saved as is (no folders, nothing hidden).
    static func safeName(_ name: String) -> String {
        let last = (name.replacingOccurrences(of: "\\", with: "/") as NSString).lastPathComponent
        let clean = last.components(separatedBy: CharacterSet(charactersIn: "/:\0")).joined(separator: "_")
            .trimmingCharacters(in: CharacterSet(charactersIn: ". "))
        return clean.isEmpty ? "文件" : clean
    }

    /// The start of a file's text: UTF-8, else GB18030 (what Chinese Windows writes), else as much as
    /// reads. Cut at `limit` characters, at a line's end when one is near.
    static func text(_ data: Data, limit: Int = 6_000) -> String {
        let head = data.prefix(limit * 4)
        let gb18030 = String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(CFStringEncoding(CFStringEncodings.GB_18030_2000.rawValue)))
        var text = String(data: head, encoding: .utf8)
            ?? (head.count < data.count ? String(data: head.dropLast(trailingPartial(head)), encoding: .utf8) : nil)
            ?? String(data: head, encoding: gb18030)
            ?? String(decoding: head, as: UTF8.self)
        if text.hasPrefix("\u{FEFF}") { text.removeFirst() }
        text = text.replacingOccurrences(of: "\r\n", with: "\n")
        guard text.count > limit else { return text }
        let cut = text.prefix(limit)
        if let line = cut.lastIndex(of: "\n"), cut.distance(from: cut.startIndex, to: line) > limit * 3 / 4 { return String(cut[..<line]) }
        return String(cut)
    }

    /// Bytes of a UTF-8 character cut off at the end of `data` (a file read only in part).
    private static func trailingPartial(_ data: Data) -> Int {
        var count = 0
        for byte in data.reversed().prefix(3) {
            count += 1
            if byte & 0xC0 != 0x80 { return byte & 0x80 == 0 ? 0 : count }  // ASCII ends cleanly; a lead byte starts the cut one
        }
        return 0
    }

    /// Markdown for the page: pictures left out (they would load from the web), the start only.
    static func markdownPreview(_ text: String) -> String {
        text.replacingOccurrences(of: #"!\[[^\]]*\]\([^)]*\)"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"(?m)^\s*<img\b[^>]*>\s*$"#, with: "", options: .regularExpression)
    }

    /// The first rows of a CSV or TSV (quoted cells understood), each with `columns` cells at most and
    /// all the same length.
    static func rows(_ text: String, separator: Character = ",", rows limit: Int = 10, columns: Int = 5) -> [[String]] {
        var rows: [[String]] = []
        var row: [String] = []
        var cell = ""
        var quoted = false
        var iterator = text.makeIterator()
        var pending: Character? = nil
        func endRow() {
            row.append(cell); cell = ""
            if !(row.count == 1 && row[0].isEmpty) { rows.append(row.map { $0.trimmingCharacters(in: .whitespaces) }) }
            row = []
        }
        while rows.count < limit, let next = pending ?? iterator.next() {
            pending = nil
            if quoted {
                if next == "\"" {
                    let after = iterator.next()
                    if after == "\"" { cell.append("\"") } else { quoted = false; pending = after }
                } else { cell.append(next) }
            } else if next == "\"" && cell.isEmpty { quoted = true }
            else if next == separator { row.append(cell); cell = "" }
            else if next == "\n" || next == "\r\n" { endRow() }
            else if next != "\r" { cell.append(next) }
        }
        if rows.count < limit && (!cell.isEmpty || !row.isEmpty) { endRow() }
        let width = min(columns, rows.map(\.count).max() ?? 0)
        return rows.map { Array(($0 + Array(repeating: "", count: max(0, width - $0.count))).prefix(width)) }
    }

    /// The newest document written in the turn at work, until the file itself arrives. Only what is
    /// written for the user (documents, notes, tables, pages), not the scripts it writes for itself.
    static func draft(_ messages: [ChatMessage]) -> FileDraft? {
        guard let turn = messages.last(where: { $0.turnId?.isEmpty == false })?.turnId else { return nil }
        let items = messages.filter { $0.turnId == turn }
        var newest: FileDraft?
        for message in items {
            for (index, event) in message.toolEvents.enumerated() where event["name"].stringValue == "write_file" {
                let path = event["arguments"]["path"].stringValue.replacingOccurrences(of: "\\", with: "/")
                let content = event["arguments"]["content"].stringValue
                let name = (path as NSString).lastPathComponent
                let kind = FileKind.of(name)
                guard !content.isEmpty, [.markdown, .text, .table, .web].contains(kind), event["error"].nonemptyString == nil,
                      !path.split(separator: "/").contains(where: { $0.hasPrefix(".") }), name != "SKILL.md" else { continue }
                newest = FileDraft(id: event["call_id"].nonemptyString ?? "\(message.id)-\(index)", name: name, content: content,
                                  done: event["phase"].stringValue == "end")
            }
        }
        guard let draft = newest, !items.contains(where: { message in message.media.contains { $0.name == draft.name } }) else { return nil }
        return draft
    }

    /// A web page file, ready for a preview web view: inside a sandboxed frame with no origin, under the
    /// same rules as the chat's cards (no network, no navigation, nothing reaching the app).
    static func webPage(_ html: String) -> String {
        let policy = "<meta http-equiv=\"Content-Security-Policy\" content=\"\(EmbeddedCardDocument.policy)\">"
        var inner = html
        if let head = inner.range(of: #"(?is)<head\b[^>]*>"#, options: .regularExpression)
            ?? inner.range(of: #"(?is)<html\b[^>]*>"#, options: .regularExpression)
            ?? inner.range(of: #"(?is)^\s*<!doctype[^>]*>"#, options: .regularExpression) {
            inner.insert(contentsOf: policy, at: head.upperBound)
        } else { inner = policy + inner }
        let escaped = inner.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        let outer = EmbeddedCardDocument.policy.replacingOccurrences(of: "frame-src 'none'", with: "frame-src about:")
        return "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"referrer\" content=\"no-referrer\"><meta http-equiv=\"Content-Security-Policy\" content=\"\(outer)\"><meta name=\"color-scheme\" content=\"light\"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#fff}iframe{width:100%;height:100%;border:0;display:block}</style></head><body><iframe title=\"网页预览\" sandbox=\"allow-scripts\" referrerpolicy=\"no-referrer\" srcdoc=\"\(escaped)\"></iframe></body></html>"
    }
}
