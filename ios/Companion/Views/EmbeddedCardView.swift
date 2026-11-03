import SwiftUI
import UIKit
@preconcurrency import WebKit

/// Generated content gets an opaque iframe origin, no credentials, and only a
/// narrow height/send bridge. It never navigates the app or loads its own code.
enum EmbeddedCardDocument {
    static let policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob: https://*.is.autonavi.com; font-src data:; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
    // WebKit's url-filter has no `|` alternation (a rule list using it fails to compile): one rule per scheme.
    static let networkRules = #"[{"trigger":{"url-filter":".*"},"action":{"type":"block"}},{"trigger":{"url-filter":"^data:"},"action":{"type":"ignore-previous-rules"}},{"trigger":{"url-filter":"^blob:"},"action":{"type":"ignore-previous-rules"}},{"trigger":{"url-filter":"^about:"},"action":{"type":"ignore-previous-rules"}},{"trigger":{"url-filter":"^https://webrd0[1-4]\\.is\\.autonavi\\.com/appmaptile\\?","resource-type":["image"]},"action":{"type":"ignore-previous-rules"}}]"#

    static func widgetURL(_ path: String, baseURL: URL?) -> URL? {
        guard let baseURL, ["http", "https"].contains(baseURL.scheme?.lowercased() ?? ""),
              let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: true),
              parts.scheme?.lowercased() == baseURL.scheme?.lowercased(),
              parts.host?.lowercased() == baseURL.host?.lowercased(), parts.port == baseURL.port,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.percentEncodedPath.range(of: #"^/api/webui/companion/widgets/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else { return nil }
        return url
    }

    static func height(_ value: Double?) -> Double { min(600, max(160, value.flatMap { $0.isFinite ? $0 : nil } ?? 240)) }
    static func message(_ value: JSONValue) -> String? {
        guard value["type"].string == "companion:send", let raw = value["text"].string else { return nil }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : String(text.prefix(2000))
    }
    private static func escaped(_ value: String) -> String {
        value.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
    }
    private static func script(_ value: String) -> String { "<script>" + value.replacingOccurrences(of: "</script", with: "<\\/script", options: .caseInsensitive) + "</script>" }
    private static func style(_ value: String) -> String { "<style>" + value.replacingOccurrences(of: "</style", with: "<\\/style", options: .caseInsensitive) + "</style>" }
    static func make(content: String, kit: EmbeddedUiKit, tokens: [String: String], dark: Bool) -> String {
        var html = content
        // Widgets can be full documents and may reference CDN copies of the kit.
        // Their executable libraries come from this bundle instead.
        for pattern in [#"(?is)<!doctype[^>]*>"#, #"(?is)</?(?:html|head|body)\b[^>]*>"#, #"(?is)<base\b[^>]*>"#, #"(?is)<meta\b[^>]*http-equiv\s*=\s*['\"][^'\"]*['\"][^>]*>"#, #"(?is)<script\b[^>]*\bsrc\s*=[^>]*>.*?</script\s*>"#, #"(?is)<link\b[^>]*>"#] {
            html = html.replacingOccurrences(of: pattern, with: "", options: .regularExpression)
        }
        let safeTokens = tokens.filter { key, value in key.range(of: #"^--c-[a-z0-9-]+$"#, options: .regularExpression) != nil && value.rangeOfCharacter(from: CharacterSet(charactersIn: "<>{};\"'")) == nil }
        let theme = safeTokens.keys.sorted().map { "\($0):\(safeTokens[$0] ?? "")" }.joined(separator: ";")
        let needsChart = content.range(of: #"ui\.chart|new\s+Chart\b|chart(?:\.umd|\.js|js)"#, options: [.regularExpression, .caseInsensitive]) != nil
        let needsMap = content.range(of: #"ui\.map|\bL\.map\b|leaflet"#, options: [.regularExpression, .caseInsensitive]) != nil
        let inner = [
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"referrer\" content=\"no-referrer\">",
            "<meta name=\"color-scheme\" content=\"\(dark ? "dark" : "light")\"><meta http-equiv=\"Content-Security-Policy\" content=\"\(policy)\">",
            style(kit.css), needsMap ? style(kit.leafletCSS) : "", style(":root{\(theme)}"),
            needsChart ? script(kit.chart) : "", needsMap ? script(kit.leaflet + kit.markerSetup) : "", script(kit.js),
            "</head><body>\(html)</body></html>"
        ].joined(separator: "\n")
        let themeData = (try? JSONEncoder().encode(JSONValue.object(safeTokens.mapValues(JSONValue.string)))) ?? Data("{}".utf8)
        let themeJSON = String(data: themeData, encoding: .utf8) ?? "{}"
        let bridge = """
        (function(){
          var frame=document.getElementById('content');
          frame.addEventListener('load',function(){frame.contentWindow.postMessage({type:'companion:theme',tokens:\(themeJSON)},'*');});
          window.addEventListener('message',function(event){
            if(event.source!==frame.contentWindow||!event.data)return;
            var data=event.data;
            if(data.type==='companion:height'&&typeof data.height==='number'&&isFinite(data.height)){
              window.webkit.messageHandlers.companionCard.postMessage({type:data.type,height:data.height});
            }else if(data.type==='companion:send'&&typeof data.text==='string'&&navigator.userActivation&&navigator.userActivation.isActive){
              window.webkit.messageHandlers.companionCard.postMessage({type:data.type,text:data.text.slice(0,2000)});
            }
          });
        })();
        """
        // The outer page alone can call the native bridge. The sandboxed child
        // cannot access its parent or obtain a server/file origin.
        let outerPolicy = policy.replacingOccurrences(of: "frame-src 'none'", with: "frame-src about:")
        return "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"referrer\" content=\"no-referrer\"><meta http-equiv=\"Content-Security-Policy\" content=\"\(outerPolicy)\"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}iframe{width:100%;height:100%;border:0;display:block}</style></head><body><iframe id=\"content\" title=\"互动内容\" sandbox=\"allow-scripts allow-forms\" referrerpolicy=\"no-referrer\" srcdoc=\"\(escaped(inner))\"></iframe>\(script(bridge))</body></html>"
    }
}

struct EmbeddedUiKit {
    let css: String
    let js: String
    let chart: String
    let leaflet: String
    let leafletCSS: String
    let markerSetup: String
    static func load(bundle: Bundle = .main) throws -> EmbeddedUiKit {
        func url(_ name: String, _ ext: String, _ subdirectory: String) throws -> URL {
            guard let found = bundle.url(forResource: name, withExtension: ext, subdirectory: subdirectory)
                    ?? bundle.url(forResource: name, withExtension: ext) else { throw ServiceError.message("互动内容组件缺失，请重新安装 App。") }
            return found
        }
        func text(_ name: String, _ ext: String, _ subdirectory: String) throws -> String { try String(contentsOf: url(name, ext, subdirectory), encoding: .utf8) }
        func png(_ name: String) throws -> String { "data:image/png;base64," + (try Data(contentsOf: url(name, "png", "UiKit/vendor/leaflet/images"))).base64EncodedString() }
        var mapCSS = try text("leaflet", "css", "UiKit/vendor/leaflet")
        for name in ["layers", "layers-2x", "marker-icon"] { mapCSS = mapCSS.replacingOccurrences(of: "images/\(name).png", with: try png(name)) }
        let icon = try png("marker-icon"), retina = try png("marker-icon-2x"), shadow = try png("marker-shadow")
        let markerSetup = ";L.Icon.Default.imagePath='';L.Icon.Default.mergeOptions({iconUrl:'\(icon)',iconRetinaUrl:'\(retina)',shadowUrl:'\(shadow)'});"
        return EmbeddedUiKit(css: try text("kit", "css", "UiKit"), js: try text("kit", "js", "UiKit"),
                             chart: try text("chart.umd", "js", "UiKit/vendor"), leaflet: try text("leaflet", "js", "UiKit/vendor/leaflet"), leafletCSS: mapCSS, markerSetup: markerSetup)
    }
}

private final class EmbeddedWidgetRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) { completionHandler(nil) }
}

/// Interactive cards met before, so scrolling past them is cheap: the kit read once, each card's page
/// built once, its last height (no jump when it comes back), and a picture of it — shown instead of
/// starting a web view again; a tap brings the live card back.
@MainActor
enum CardMemory {
    static var kit: EmbeddedUiKit?
    static var heights: [String: CGFloat] = [:]
    static let documents = NSCache<NSString, NSString>()
    static let pictures: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>()
        cache.totalCostLimit = 48 * 1024 * 1024
        return cache
    }()

    static func loadKit() throws -> EmbeddedUiKit {
        if let kit { return kit }
        let loaded = try EmbeddedUiKit.load()
        kit = loaded
        return loaded
    }
}

@MainActor
struct EmbeddedCardView: View {
    let card: ReplyCard
    let baseURL: URL?
    var send: ((String) -> Bool)?
    @Environment(\.colorScheme) private var scheme
    @Environment(\.displayScale) private var displayScale
    @State private var source: String?
    @State private var document: String?
    @State private var error: String?
    @State private var height: CGFloat = 240
    @State private var retry = 0
    /// A web view is running for this card (else its picture shows, when there is one).
    @State private var live = false
    /// Its picture kept on this iPhone from an earlier launch.
    @State private var keptPicture: UIImage?

    /// The card's identity: its content (the same card in another reply is the same card). A content
    /// digest, not `hashValue`, so it is the same on the next launch and the kept picture is found.
    private let key: String

    init(card: ReplyCard, baseURL: URL?, send: ((String) -> Bool)? = nil) {
        self.card = card
        self.baseURL = baseURL
        self.send = send
        key = "\(card.type)|\(card.data["url"].stringValue)|\(LocalStore.digest(card.data["html"].stringValue))"
    }

    private var pictureKey: NSString { "\(key)|\(scheme == .dark ? "dark" : "light")" as NSString }
    private var pictureFile: URL? { LocalStore.url("cards", server: baseURL?.absoluteString ?? "", name: pictureKey as String) }

    var body: some View {
        VStack(spacing: 10) {
            if let error { ResourceErrorView(message: error) { self.error = nil; source = nil; document = nil; retry += 1 } }
            else if !live, let picture = CardMemory.pictures.object(forKey: pictureKey) ?? keptPicture {
                // Seen before: its picture, until tapped (scrolling past starts no web view).
                Image(uiImage: picture).resizable().scaledToFit()
                    .frame(maxWidth: .infinity).frame(height: height, alignment: .top)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .overlay(alignment: .bottomTrailing) {
                        Label("点一下互动", systemImage: "hand.tap").font(.system(size: 11)).padding(.horizontal, 8).padding(.vertical, 4)
                            .background(.ultraThinMaterial, in: Capsule()).padding(8)
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { live = true; Haptics.selection() }
                    .accessibilityLabel((card.title ?? "互动内容") + "，点一下开始互动").accessibilityAddTraits(.isButton)
            } else if let document {
                EmbeddedWebSurface(document: document, fixedHeight: card.data["height"].doubleValue != nil, height: $height, error: $error, send: send,
                                   onSnapshot: { picture in
                                       CardMemory.pictures.setObject(picture, forKey: pictureKey, cost: Int(picture.size.width * picture.size.height * picture.scale * picture.scale * 4))
                                       LocalStore.keep(picture, to: pictureFile)
                                   })
                    .frame(height: height).clipShape(RoundedRectangle(cornerRadius: 14))
                    .accessibilityLabel(card.title ?? "互动内容")
                    .onAppear { live = true }
            } else { ProgressView("正在加载互动内容…").frame(maxWidth: .infinity, minHeight: height) }
        }
        .onChange(of: height) { _, value in CardMemory.heights[key] = value }
        .task(id: pictureKey as String) { await restorePicture() }
        .task(id: "\(retry)|\(key)") { await load() }
        .task(id: "\(scheme == .dark)|\(source?.count ?? -1)") { buildDocument() }
    }

    /// After a relaunch: the card's picture from this iPhone (its height is the picture's), so the chat
    /// opens without starting a web view per card.
    private func restorePicture() async {
        keptPicture = nil  // (light and dark have a picture each)
        guard !live, CardMemory.pictures.object(forKey: pictureKey) == nil,
              let picture = await LocalStore.picture(pictureFile, scale: displayScale), !Task.isCancelled else { return }
        CardMemory.pictures.setObject(picture, forKey: pictureKey, cost: Int(picture.size.width * picture.size.height * picture.scale * picture.scale * 4))
        if CardMemory.heights[key] == nil {
            CardMemory.heights[key] = picture.size.height
            height = picture.size.height
        }
        keptPicture = picture
    }

    /// The card's page, built once per card and colour scheme (it inlines the chart library: large).
    private func buildDocument() {
        guard let source, let kit = CardMemory.kit else { return }
        let cacheKey = pictureKey
        if let kept = CardMemory.documents.object(forKey: cacheKey) { document = kept as String; return }
        let made = EmbeddedCardDocument.make(content: source, kit: kit, tokens: tokens, dark: scheme == .dark)
        CardMemory.documents.setObject(made as NSString, forKey: cacheKey)
        document = made
    }

    private var tokens: [String: String] {
        func hex(_ color: Color) -> String {
            let traits = UITraitCollection(userInterfaceStyle: scheme == .dark ? .dark : .light)
            let color = UIColor(color).resolvedColor(with: traits)
            var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            color.getRed(&r, green: &g, blue: &b, alpha: &a)
            return String(format: "#%02x%02x%02x", Int(r * 255), Int(g * 255), Int(b * 255))
        }
        return ["--c-bg": hex(CompanionPalette.background), "--c-surface": hex(CompanionPalette.surface), "--c-text": hex(.primary),
                "--c-text-muted": hex(.secondary), "--c-primary": hex(CompanionPalette.primary), "--c-primary-text": hex(CompanionPalette.primaryText),
                "--c-accent": hex(CompanionPalette.primary), "--c-accent-soft": hex(CompanionPalette.elevated), "--c-fill": hex(CompanionPalette.elevated),
                "--c-border": scheme == .dark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.085)", "--c-good": scheme == .dark ? "#7fbf8a" : "#4f8a5b", "--c-bad": scheme == .dark ? "#ff8a7a" : "#b42318"]
    }
    private func load() async {
        error = nil; source = nil
        height = CardMemory.heights[key] ?? CGFloat(EmbeddedCardDocument.height(card.data["height"].doubleValue))
        do {
            _ = try CardMemory.loadKit()
            if card.type == "ui" { source = card.data["html"].stringValue; return }
            guard let url = EmbeddedCardDocument.widgetURL(card.data["url"].stringValue, baseURL: baseURL) else { throw ServiceError.message("互动内容地址无效。") }
            // Signed widget paths authorize themselves, as in companion/server.py.
            // No device/API credential, cookies, or redirected request is sent.
            let configuration = URLSessionConfiguration.ephemeral
            configuration.httpCookieStorage = nil; configuration.urlCache = nil
            configuration.timeoutIntervalForRequest = 20; configuration.timeoutIntervalForResource = 30
            let session = URLSession(configuration: configuration, delegate: EmbeddedWidgetRedirects(), delegateQueue: nil)
            defer { session.invalidateAndCancel() }
            var request = URLRequest(url: url); request.cachePolicy = .reloadIgnoringLocalCacheData
            request.setValue("text/html", forHTTPHeaderField: "Accept")
            let (data, response) = try await session.data(for: request)
            guard let response = response as? HTTPURLResponse, response.statusCode == 200,
                  response.url == url, response.mimeType == "text/html", data.count <= 2 * 1024 * 1024,
                  let text = String(data: data, encoding: .utf8) else { throw ServiceError.message("互动内容没能加载，稍后重试。") }
            try Task.checkCancellation(); source = text
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}

@MainActor
private struct EmbeddedWebSurface: UIViewRepresentable {
    let document: String
    let fixedHeight: Bool
    @Binding var height: CGFloat
    @Binding var error: String?
    var send: ((String) -> Bool)?
    /// A picture of the card once it has drawn and settled (charts animate in first).
    var onSnapshot: (UIImage) -> Void = { _ in }
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.add(context.coordinator, name: "companionCard")
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.isOpaque = false; webView.backgroundColor = .clear; webView.scrollView.backgroundColor = .clear
        webView.scrollView.isScrollEnabled = false; webView.allowsLinkPreview = false
        webView.navigationDelegate = context.coordinator
        context.coordinator.webView = webView
        context.coordinator.prepare()
        return webView
    }
    func updateUIView(_ uiView: WKWebView, context: Context) { context.coordinator.parent = self; context.coordinator.load() }
    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        uiView.stopLoading(); uiView.navigationDelegate = nil
        uiView.configuration.userContentController.removeScriptMessageHandler(forName: "companionCard")
        coordinator.webView = nil
    }
    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        var parent: EmbeddedWebSurface
        weak var webView: WKWebView?
        private var policyReady = false
        private var loaded = ""
        private var submitted = false
        private var snapshotTask: Task<Void, Never>?
        init(_ parent: EmbeddedWebSurface) { self.parent = parent }
        /// Its picture, taken once it has been still for a moment (a later change takes it again).
        func scheduleSnapshot() {
            snapshotTask?.cancel()
            snapshotTask = Task { @MainActor [weak self] in
                try? await Task.sleep(for: .milliseconds(1_500))
                guard let self, !Task.isCancelled, let webView = self.webView, webView.bounds.width > 0 else { return }
                webView.takeSnapshot(with: nil) { [weak self] image, _ in
                    guard let image else { return }
                    Task { @MainActor in self?.parent.onSnapshot(image) }
                }
            }
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { scheduleSnapshot() }
        func prepare() {
            WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "CompanionCardPolicyV2", encodedContentRuleList: EmbeddedCardDocument.networkRules) { [weak self] list, _ in
                Task { @MainActor [weak self] in
                    guard let self, let webView = self.webView else { return }
                    // The rule list is a second wall: the page's own policy (connect-src 'none', no remote
                    // scripts) already keeps it offline, so the card still shows if the list can't be made.
                    if let list { webView.configuration.userContentController.add(list) }
                    self.policyReady = true; self.load()
                }
            }
        }
        func load() {
            guard policyReady, loaded != parent.document else { return }
            loaded = parent.document; submitted = false
            webView?.loadHTMLString(loaded, baseURL: nil)
        }
        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.frameInfo.isMainFrame, message.webView === webView,
                  let data = try? JSONSerialization.data(withJSONObject: message.body), let value = try? JSONValue.parse(data) else { return }
            if let text = EmbeddedCardDocument.message(value), !submitted, let send = parent.send, send(text) { submitted = true; Haptics.send() }
            else if value["type"].string == "companion:height", !parent.fixedHeight {
                let next = CGFloat(EmbeddedCardDocument.height(value["height"].doubleValue))
                if abs(parent.height - next) > 2 { parent.height = next; scheduleSnapshot() }
            }
        }
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let address = navigationAction.request.url?.absoluteString ?? ""
            decisionHandler(navigationAction.navigationType == .other && ["about:blank", "about:srcdoc"].contains(address) ? .allow : .cancel)
        }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }
        private func failed(_ error: Error) {
            if (error as NSError).code != NSURLErrorCancelled { parent.error = "互动内容没能显示，请重试。" }
        }
        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { parent.error = "互动内容已暂停，点重试重新打开。" }
    }
}
