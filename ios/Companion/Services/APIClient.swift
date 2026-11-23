import Foundation

enum ServiceError: LocalizedError, Sendable {
    case http(Int)
    case message(String)

    var errorDescription: String? {
        switch self {
        case .http(let status): return "服务器返回 HTTP \(status)"
        case .message(let value): return value
        }
    }

    var isAuthentication: Bool {
        if case .http(let status) = self { return status == 401 || status == 403 }
        return false
    }
}

/// A redirect must never forward an upload capability or device credential to another origin.
private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

@MainActor
final class APIClient {
    let baseURL: URL
    private let redirectDelegate: NoRedirectDelegate
    private let session: URLSession
    var apiToken = ""

    init(baseURL: URL) {
        self.baseURL = baseURL
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 330
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        let delegate = NoRedirectDelegate()
        redirectDelegate = delegate
        session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    }

    nonisolated private static let trailingPunctuation: Set<Character> = [
        ".", ",", ";", "!", "?", ")", ">", "'", "\"",
        "。", "，", "、", "；", "！", "？", "）", "】", "》", "」", "』", "”", "’",
    ]

    /// Extracts a URL or host token from pasted text and strips trailing sentence punctuation while preserving IPv6 `[::1]`.
    nonisolated static func extractAddressCandidate(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        var token = trimmed
        if let match = trimmed.range(of: #"(?i)(?:https?|musebot)://[^\s<>"]+"#, options: .regularExpression) {
            token = String(trimmed[match])
        }
        while let last = token.last {
            if last == "]" {
                let opens = token.filter { $0 == "[" }.count
                let closes = token.filter { $0 == "]" }.count
                if closes > opens { token.removeLast(); continue }
                break
            }
            if trailingPunctuation.contains(last) {
                token.removeLast()
            } else {
                break
            }
        }
        return token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    nonisolated static func extractPairingCode(from raw: String) -> String? {
        let candidate = extractAddressCandidate(raw)
        guard !candidate.isEmpty else { return nil }
        let components = URLComponents(string: candidate)
            ?? (candidate.contains("://") ? nil : URLComponents(string: "https://" + candidate))
        if let items = components?.queryItems {
            for name in ["pair", "code"] {
                if let value = items.first(where: { $0.name.lowercased() == name })?.value?
                    .trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
                    return value
                }
            }
        }
        if let fragment = components?.fragment, !fragment.isEmpty {
            if let fragItems = URLComponents(string: "https://pair.invalid/?" + fragment)?.queryItems {
                for name in ["pair", "code"] {
                    if let value = fragItems.first(where: { $0.name.lowercased() == name })?.value?
                        .trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
                        return value
                    }
                }
            }
        }
        return nil
    }

    /// Parses `musebot://`, `http(s)://`, or bare server/pairing inputs into `(server, pairingCode)`.
    nonisolated static func parseConnectInput(_ raw: String) -> (server: String?, pairingCode: String?) {
        let candidate = extractAddressCandidate(raw)
        guard !candidate.isEmpty else { return (nil, nil) }
        if candidate.lowercased().hasPrefix("musebot://"), let parts = URLComponents(string: candidate) {
            let query = parts.queryItems ?? []
            let serverParam = ["server", "url", "base"].lazy
                .compactMap { key in query.first(where: { $0.name.lowercased() == key })?.value?.trimmingCharacters(in: .whitespacesAndNewlines) }
                .first { !$0.isEmpty }
            let directCode = extractPairingCode(from: candidate)
                ?? serverParam.flatMap { extractPairingCode(from: $0) }
            if let serverParam {
                return (serverParam, directCode)
            }
            let host = parts.host ?? ""
            let lowerHost = host.lowercased()
            if !host.isEmpty && lowerHost != "pair" && lowerHost != "connect" && lowerHost != "open" {
                let formattedHost = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
                let portPart = parts.port.map { ":\($0)" } ?? ""
                return (formattedHost + portPart + parts.path, directCode)
            }
            return (nil, directCode)
        }
        return (candidate, extractPairingCode(from: candidate))
    }

    nonisolated private static func isLocalOrPrivateHost(_ host: String, port: Int?) -> Bool {
        if port == 8766 || port == 8799 { return true }
        let clean = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]")).lowercased()
        if clean == "localhost" || clean == "::1" || clean.hasPrefix("fe80:") || clean.hasPrefix("fc") || clean.hasPrefix("fd") {
            return true
        }
        if clean.hasSuffix(".local") || clean.hasSuffix(".lan") || clean.hasSuffix(".home.arpa") {
            return true
        }
        let octets = clean.split(separator: ".", omittingEmptySubsequences: false)
        if octets.count == 4, let a = Int(octets[0]), let b = Int(octets[1]),
           let c = Int(octets[2]), let d = Int(octets[3]),
           [a, b, c, d].allSatisfy({ (0...255).contains($0) }) {
            if a == 127 || a == 10 || (a == 192 && b == 168) || (a == 172 && (16...31).contains(b)) {
                return true
            }
            // Tailscale / CGNAT 100.64.0.0/10
            if a == 100 && (64...127).contains(b) {
                return true
            }
        }
        return false
    }

    nonisolated private static func looksLikeValidHost(_ host: String) -> Bool {
        let clean = host.trimmingCharacters(in: CharacterSet(charactersIn: "[]")).lowercased()
        guard !clean.isEmpty, !clean.contains(where: \.isWhitespace) else { return false }
        if clean == "localhost" { return true }
        if clean.contains(":") { return host.hasPrefix("[") || clean == "::1" || clean.contains(":") }
        guard clean.contains("."), !clean.hasPrefix("."), !clean.hasSuffix("."), !clean.contains("..") else { return false }
        return clean.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == ".") }
    }

    nonisolated static func normalizedBasePath(_ rawPath: String) -> String {
        guard !rawPath.isEmpty, rawPath != "/" else { return "" }
        var path = rawPath
        for suffix in ["/companion.html", "/index.html"] {
            if path.lowercased().hasSuffix(suffix) {
                path = String(path.dropLast(suffix.count))
                break
            }
        }
        while path.hasSuffix("/") { path.removeLast() }
        if path.isEmpty { return "" }
        return path.hasPrefix("/") ? path : "/" + path
    }

    nonisolated static func normalizedServer(_ input: String) throws -> URL {
        let parsed = parseConnectInput(input)
        let candidate = (parsed.server ?? extractAddressCandidate(input)).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !candidate.isEmpty else {
            throw ServiceError.message("请输入完整的服务器地址")
        }
        let withScheme: String
        if candidate.range(of: "^[A-Za-z][A-Za-z0-9+.-]*://", options: .regularExpression) != nil {
            withScheme = candidate
        } else if candidate.range(of: "^[A-Za-z][A-Za-z0-9+.-]*:(?!\\d)", options: .regularExpression) != nil,
                  !candidate.hasPrefix("[") {
            throw ServiceError.message("请输入完整的 http:// 或 https:// 服务器地址")
        } else {
            guard let probe = URLComponents(string: "http://" + candidate),
                  let host = probe.host, looksLikeValidHost(host) else {
                throw ServiceError.message("请输入完整的 http:// 或 https:// 服务器地址")
            }
            let scheme = isLocalOrPrivateHost(host, port: probe.port) ? "http" : "https"
            withScheme = scheme + "://" + candidate
        }

        guard var parts = URLComponents(string: withScheme),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              let host = parts.host, looksLikeValidHost(host),
              parts.user == nil, parts.password == nil else {
            throw ServiceError.message("请输入完整的 http:// 或 https:// 服务器地址")
        }
        let segments = parts.path.split(separator: "/")
        guard !segments.contains("..") else {
            throw ServiceError.message("服务器地址格式不对")
        }
        parts.scheme = parts.scheme?.lowercased()
        if !host.contains(":") && host != host.lowercased() {
            parts.host = host.lowercased()
        }
        parts.path = normalizedBasePath(parts.path)
        parts.query = nil
        parts.fragment = nil
        guard let url = parts.url else { throw ServiceError.message("服务器地址格式不对") }
        return url
    }

    /// Resolves root-relative (`/api/...`) and relative paths while preserving any reverse-proxy subpath on `baseURL`.
    nonisolated static func resolvePath(_ path: String, against baseURL: URL?) -> URL? {
        guard !path.isEmpty, !path.hasPrefix("//") else { return nil }
        if path.range(of: "^[A-Za-z][A-Za-z0-9+.-]*:", options: .regularExpression) != nil {
            return URL(string: path)
        }
        guard let baseURL else { return URL(string: path) }
        let basePath = normalizedBasePath(baseURL.path)
        if path.hasPrefix("/") {
            let rawPathOnly = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? path
            let effective: String
            if !basePath.isEmpty && rawPathOnly != basePath && !rawPathOnly.hasPrefix(basePath + "/") {
                effective = basePath + path
            } else {
                effective = path
            }
            return URL(string: effective, relativeTo: baseURL)?.absoluteURL
        }
        let baseString = baseURL.absoluteString.hasSuffix("/") ? baseURL.absoluteString : baseURL.absoluteString + "/"
        guard let directoryBase = URL(string: baseString) else { return nil }
        return URL(string: path, relativeTo: directoryBase)?.absoluteURL
    }

    func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        let basePath = Self.normalizedBasePath(baseURL.path)
        guard let relative = Self.resolvePath(path, against: baseURL),
              var parts = URLComponents(url: relative, resolvingAgainstBaseURL: true),
              parts.scheme == baseURL.scheme, parts.host == baseURL.host, parts.port == baseURL.port,
              parts.user == nil, parts.password == nil,
              !parts.path.split(separator: "/").contains(".."),
              basePath.isEmpty || parts.path == basePath || parts.path.hasPrefix(basePath + "/") else {
            throw ServiceError.message("服务器返回了无效的路径")
        }
        if !query.isEmpty {
            parts.queryItems = (parts.queryItems ?? []) + query
        }
        guard let result = parts.url else { throw ServiceError.message("请求地址无效") }
        return result
    }

    func get(_ path: String, query: [URLQueryItem] = []) async throws -> JSONValue {
        var request = URLRequest(url: try url(path, query: query))
        // The first ideas read can ask the model to populate an empty cache.
        if path == "/api/webui/companion/ideas" || request.url?.path.hasSuffix("/api/webui/companion/ideas") == true {
            request.timeoutInterval = 95
        }
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return try await perform(request)
    }

    func post(_ path: String, payload: [String: JSONValue] = [:]) async throws -> JSONValue {
        var request = URLRequest(url: try url(path))
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(JSONValue.object(payload))
        return try await perform(request)
    }

    func pair(code: String) async throws -> JSONValue {
        var request = URLRequest(url: try url("/api/pair"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(JSONValue.object([
            "code": .string(code), "name": .string("iPhone"),
        ]))
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ServiceError.message("服务器没有响应") }
        guard (200..<300).contains(response.statusCode) else {
            let body = try? JSONValue.parse(data)
            throw ServiceError.message(body?["error"].stringValue == "locked"
                ? "试错太多次了，过 10 分钟再试"
                : "配对码不对或已过期，在服务器上再生成一个")
        }
        return try JSONValue.parse(data)
    }

    func bootstrap(deviceToken: String?) async throws -> JSONValue {
        var request = URLRequest(url: try url("/webui/bootstrap"))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let deviceToken, !deviceToken.isEmpty {
            request.setValue("Device \(deviceToken)", forHTTPHeaderField: "Authorization")
        }
        return try await perform(request)
    }

    func socketURL(bootstrap: JSONValue) throws -> URL {
        let advertised = bootstrap["ws_url"].stringValue
        let path = bootstrap["ws_path"].stringValue
        guard !path.isEmpty else { throw ServiceError.message("服务器没有提供聊天连接") }
        var parts: URLComponents?
        if advertised.hasPrefix("ws://") || advertised.hasPrefix("wss://") {
            parts = URLComponents(string: advertised)
        } else {
            parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: true)
            parts?.scheme = baseURL.scheme == "https" ? "wss" : "ws"
            let basePath = Self.normalizedBasePath(baseURL.path)
            let normalizedWSPath = path.hasPrefix("/") ? path : "/" + path
            if !basePath.isEmpty && normalizedWSPath != basePath && !normalizedWSPath.hasPrefix(basePath + "/") {
                parts?.path = normalizedWSPath == "/" ? basePath + "/" : basePath + normalizedWSPath
            } else {
                parts?.path = normalizedWSPath
            }
            parts?.query = nil
        }
        guard var components = parts, components.host != nil,
              components.user == nil, components.password == nil else {
            throw ServiceError.message("聊天连接地址无效")
        }
        var query = components.queryItems ?? []
        query.removeAll { $0.name == "token" }
        if let token = bootstrap["token"].string, !token.isEmpty {
            query.append(URLQueryItem(name: "token", value: token))
        }
        components.queryItems = query.isEmpty ? nil : query
        guard let result = components.url else { throw ServiceError.message("聊天连接地址无效") }
        return result
    }

    func makeWebSocketTask(url: URL, maximumMessageSize: Int) -> URLSessionWebSocketTask {
        let task = session.webSocketTask(with: url)
        task.maximumMessageSize = max(maximumMessageSize, 1024)
        return task
    }

    func invalidate() {
        session.invalidateAndCancel()
    }

    func upload(_ draft: DraftUpload, capability: JSONValue) async throws -> JSONValue {
        guard let path = capability["path"].string, !path.isEmpty,
              let token = capability["token"].string, !token.isEmpty else {
            throw ServiceError.message("附件上传连接还没准备好")
        }
        guard !draft.data.isEmpty, draft.data.count <= 20 * 1024 * 1024 else {
            throw ServiceError.message("单个附件不能超过 20 MB")
        }
        guard var uploadAddress = URLComponents(url: try url(path), resolvingAgainstBaseURL: true) else {
            throw ServiceError.message("附件上传地址无效")
        }
        uploadAddress.query = nil
        uploadAddress.fragment = nil
        guard let uploadURL = uploadAddress.url else { throw ServiceError.message("附件上传地址无效") }
        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 315
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(draft.mimeType, forHTTPHeaderField: "Content-Type")
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        request.setValue(draft.name.addingPercentEncoding(withAllowedCharacters: allowed), forHTTPHeaderField: "X-Attachment-Name")
        request.httpBody = draft.data
        let body = try await perform(request)
        guard let reference = body["reference"].string, !reference.isEmpty else {
            throw ServiceError.message("附件上传没有返回引用")
        }
        return .object(["reference": .string(reference), "name": .string(draft.name)])
    }

    private func perform(_ request: URLRequest) async throws -> JSONValue {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ServiceError.message("服务器没有响应") }
        guard (200..<300).contains(response.statusCode) else { throw ServiceError.http(response.statusCode) }
        guard response.mimeType?.lowercased().contains("json") == true else {
            throw ServiceError.message("这个地址返回的是网页，请确认填写了伙伴服务器地址")
        }
        return try JSONValue.parse(data)
    }
}
