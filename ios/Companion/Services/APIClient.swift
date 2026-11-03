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

    static func normalizedServer(_ input: String) throws -> URL {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: text),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              parts.host?.isEmpty == false, parts.user == nil, parts.password == nil else {
            throw ServiceError.message("请输入完整的 http:// 或 https:// 服务器地址")
        }
        parts.scheme = parts.scheme?.lowercased()
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        guard let url = parts.url else { throw ServiceError.message("服务器地址格式不对") }
        return url
    }

    func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard let relative = URL(string: path, relativeTo: baseURL)?.absoluteURL,
              var parts = URLComponents(url: relative, resolvingAgainstBaseURL: true),
              parts.scheme == baseURL.scheme, parts.host == baseURL.host, parts.port == baseURL.port,
              parts.user == nil, parts.password == nil else {
            throw ServiceError.message("服务器返回了无效的路径")
        }
        if !query.isEmpty { parts.queryItems = query }
        guard let result = parts.url else { throw ServiceError.message("请求地址无效") }
        return result
    }

    func get(_ path: String, query: [URLQueryItem] = []) async throws -> JSONValue {
        var request = URLRequest(url: try url(path, query: query))
        // The first ideas read can ask the model to populate an empty cache.
        if request.url?.path == "/api/webui/companion/ideas" { request.timeoutInterval = 95 }
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
            parts?.path = path.hasPrefix("/") ? path : "/" + path
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
