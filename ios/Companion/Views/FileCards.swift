import SwiftUI
import UIKit
import AVFoundation
import PDFKit
import QuickLookThumbnailing
@preconcurrency import WebKit
import MarkdownUI

/// How a file's first page is drawn in its card.
enum FileLook {
    case text(String)
    case table([[String]])
    case picture(UIImage)
    /// A web page drawn live (then, in the chat, kept as its picture).
    case web(String)
}

/// Pages and posters already drawn, so scrolling back past a file draws nothing again.
@MainActor
enum FilePreviewMemory {
    static let pictures: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>()
        cache.totalCostLimit = 48 * 1_024 * 1_024
        return cache
    }()
    static var seconds: [String: Double] = [:]

    static func keep(_ picture: UIImage, key: String) {
        pictures.setObject(picture, forKey: key as NSString, cost: Int(picture.size.width * picture.size.height * picture.scale * picture.scale * 4))
    }
}

/// First pages, drawn off the main thread.
enum FilePages {
    struct Made: @unchecked Sendable { let picture: UIImage?; var pages = 0 }
    /// As wide as a card gets (points).
    static let width: CGFloat = 400

    /// A PDF's page count, and its first page (when `draw`).
    static func pdf(_ data: Data, scale: CGFloat, draw: Bool) -> Made {
        guard let document = PDFDocument(data: data) else { return Made(picture: nil) }
        guard draw, let page = document.page(at: 0) else { return Made(picture: nil, pages: document.pageCount) }
        let bounds = page.bounds(for: .mediaBox)
        let turned = page.rotation % 180 != 0
        let ratio = (turned ? bounds.width : bounds.height) / max(turned ? bounds.height : bounds.width, 1)
        let picture = page.thumbnail(of: CGSize(width: width * scale, height: width * scale * ratio), for: .mediaBox)
        return Made(picture: picture, pages: document.pageCount)
    }

    /// Word, Excel, PowerPoint, Pages, Numbers, Keynote, RTF: the first page as Quick Look draws it.
    static func quickLook(_ data: Data, name: String, scale: CGFloat) async -> Made {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("CompanionPreview", isDirectory: true)
        let file = folder.appendingPathComponent(UUID().uuidString + "." + FileKinds.ext(name))
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try data.write(to: file)
        } catch { return Made(picture: nil) }
        defer { try? FileManager.default.removeItem(at: file) }
        let request = QLThumbnailGenerator.Request(fileAt: file, size: CGSize(width: width, height: width * 1.4), scale: scale, representationTypes: .thumbnail)
        let made = try? await QLThumbnailGenerator.shared.generateBestRepresentation(for: request)
        return Made(picture: made?.uiImage)
    }
}

/// A shared file's local copy, under its own name.
struct SharedFile: Identifiable {
    let id = UUID()
    let url: URL
}

/// A file in the chat, as Muse shows one: its first page (a tap opens it whole), then its icon,
/// name and kind, and ··· to open, share or copy it. A kind with no page (a zip) is the row alone.
@MainActor
struct FileCard: View {
    let item: MediaAttachment
    let baseURL: URL?
    var open: () -> Void
    @Environment(\.displayScale) private var displayScale
    @State private var look: FileLook?
    @State private var pages: Int?
    @State private var bytes: Int?
    @State private var loaded = false
    @State private var sharing: SharedFile?
    @State private var problem: String?

    private var name: String { item.name ?? "文件" }
    private var kind: FileKind { FileKind.of(name) }
    private var remote: URL? { resolvedURL(item.url, baseURL: baseURL) }
    /// Its page kept on this iPhone: the chat opens with it drawn.
    private var pictureFile: URL? { LocalStore.url("previews", server: remote?.absoluteString ?? "", name: item.url) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if (kind.isText || kind.isPage) && (look != nil || !loaded) {
                FilePaper(look: look, kind: kind) { keep($0) }
                .padding([.horizontal, .top], 8)
                .onTapGesture(perform: open)
                .accessibilityElement()
                .accessibilityLabel("预览「\(FileKinds.title(name))」，点一下打开")
                .accessibilityAddTraits(.isButton)
            }
            HStack(spacing: 0) {
                Button(action: open) {
                    HStack(spacing: 12) {
                        FileBadge(name: name, kind: kind)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(FileKinds.title(name)).font(.system(size: 16, weight: .semibold)).lineLimit(2).multilineTextAlignment(.leading)
                            Text(FileKinds.subtitle(kind, pages: pages, bytes: bytes)).font(.system(size: 13)).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 4)
                    }.contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("打开\(kind.words)「\(FileKinds.title(name))」")
                Menu {
                    Button("打开", systemImage: "doc.text.magnifyingglass", action: open)
                    Button("分享或存储…", systemImage: "square.and.arrow.up") { Task { await share() } }
                    if kind.isText || kind == .web {
                        Button("复制文字", systemImage: "doc.on.doc") { Task { await copyText() } }
                    }
                } label: {
                    Image(systemName: "ellipsis").font(.system(size: 17, weight: .semibold)).foregroundStyle(.secondary)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("更多：打开、分享或复制")
            }
            .padding(.leading, 14).padding(.trailing, 4).padding(.vertical, 8)
        }
        .modifier(FileCardFill())
        .task(id: item.url) { await load() }
        .sheet(item: $sharing, onDismiss: { sharing = nil }) { ShareSheet(items: [$0.url]) }
        .alert("文件", isPresented: Binding(get: { problem != nil }, set: { if !$0 { problem = nil } })) {
            Button("好", role: .cancel) { problem = nil }
        } message: { Text(problem ?? "") }
    }

    private func load() async {
        let kind = self.kind, name = self.name, scale = displayScale
        guard kind.isText || kind.isPage, let remote else { loaded = true; return }
        if look == nil { loaded = false }  // back on screen after a cut-short look: the page's place again
        if kind.isPage, look == nil {
            if let kept = FilePreviewMemory.pictures.object(forKey: item.url as NSString) { look = .picture(kept) }
            else if let kept = await LocalStore.picture(pictureFile, scale: scale) {
                FilePreviewMemory.keep(kept, key: item.url)
                look = .picture(kept)
            }
        }
        // Kept on this iPhone after the first time (opening and sharing use the same copy).
        guard let data = await LocalStore.download(remote, kind: "files"), !Task.isCancelled else { loaded = true; return }
        bytes = data.count
        let drawn = look != nil
        switch kind {
        case .markdown, .text, .code:
            let text = await Task.detached(priority: .userInitiated) { FileKinds.text(data, limit: 1_800) }.value
            look = .text(kind == .markdown ? FileKinds.markdownPreview(text) : text)
        case .table:
            let separator: Character = FileKinds.ext(name) == "tsv" ? "\t" : ","
            let rows = await Task.detached(priority: .userInitiated) { FileKinds.rows(FileKinds.text(data, limit: 4_000), separator: separator) }.value
            look = rows.isEmpty ? nil : .table(rows)
        case .pdf:
            let made = await Task.detached(priority: .userInitiated) { FilePages.pdf(data, scale: scale, draw: !drawn) }.value
            pages = made.pages > 0 ? made.pages : nil
            if let picture = made.picture { keep(picture) }
        case .web:
            if !drawn { look = .web(await Task.detached(priority: .userInitiated) { FileKinds.text(data, limit: 400_000) }.value) }
        case .word, .sheet, .slides:
            if !drawn, let picture = await FilePages.quickLook(data, name: name, scale: scale).picture { keep(picture) }
        default: break
        }
        loaded = true
    }

    private func keep(_ picture: UIImage) {
        FilePreviewMemory.keep(picture, key: item.url)
        LocalStore.keep(picture, to: pictureFile)
        look = .picture(picture)
    }

    private func share() async {
        guard let remote, let data = await LocalStore.download(remote, kind: "files") else { problem = "文件没能取到，请检查连接后再试。"; return }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("CompanionShare", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let file = folder.appendingPathComponent(FileKinds.safeName(name))
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try data.write(to: file, options: [.atomic, .completeFileProtection])
            sharing = SharedFile(url: file)
        } catch { problem = "暂时无法准备文件，请稍后重试。" }
    }

    private func copyText() async {
        guard let remote, let data = await LocalStore.download(remote, kind: "files") else { problem = "文件没能取到，请检查连接后再试。"; return }
        UIPasteboard.general.string = await Task.detached(priority: .userInitiated) { FileKinds.text(data, limit: 2_000_000) }.value
        Haptics.selection()
    }
}

/// A file card's look: a reply bubble's colours, rounder, no wider than a page needs.
struct FileCardFill: ViewModifier {
    func body(content: Content) -> some View {
        content.frame(maxWidth: 400, alignment: .leading)
            .modifier(BubbleFill(shape: RoundedRectangle(cornerRadius: 24, style: .continuous)))
    }
}

/// The page in a file card: white paper (a document looks like one in dark mode too), its top part.
@MainActor
struct FilePaper: View {
    let look: FileLook?
    let kind: FileKind
    var height: CGFloat = 220
    /// A web page's picture once it has drawn (nil: it stays live).
    var onPicture: ((UIImage) -> Void)?

    var body: some View {
        ZStack(alignment: .top) {
            Color.white
            if let look {
                switch look {
                case .text(let text): textPage(text)
                case .table(let rows): tablePage(rows)
                case .picture(let picture): picturePage(picture)
                case .web(let html): FileWebPage(html: html, onPicture: onPicture)
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity)
        .frame(height: height, alignment: .top)
        .overlay(alignment: .bottom) {
            if isWords {
                LinearGradient(colors: [.white.opacity(0), .white], startPoint: .top, endPoint: .bottom)
                    .frame(height: 44).allowsHitTesting(false)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color.black.opacity(0.07)))
        .environment(\.colorScheme, .light)
        .contentShape(Rectangle())
    }

    private var isWords: Bool {
        guard let look else { return false }
        switch look {
        case .text, .table: return true
        case .picture, .web: return false
        }
    }

    @ViewBuilder private func textPage(_ text: String) -> some View {
        Group {
            if kind == .markdown {
                Markdown(text).markdownTheme(.basic).markdownTextStyle { FontSize(12) }
            } else if kind == .code {
                Text(text).font(.system(size: 10.5, design: .monospaced))
            } else {
                Text(text).font(.system(size: 12)).lineSpacing(2)
            }
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .fixedSize(horizontal: false, vertical: true)
        .allowsHitTesting(false)
    }

    private func tablePage(_ rows: [[String]]) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 7) {
            ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                GridRow {
                    ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                        Text(cell).font(.system(size: 11.5, weight: index == 0 ? .semibold : .regular)).lineLimit(1)
                    }
                }
                if index == 0 { Divider().gridCellUnsizedAxes(.horizontal) }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .allowsHitTesting(false)
    }

    /// A page fills the width from its top; a wide one (a slide) shows whole.
    private func picturePage(_ picture: UIImage) -> some View {
        GeometryReader { geometry in
            let wide = picture.size.width / max(picture.size.height, 1) > geometry.size.width / max(geometry.size.height, 1)
            Image(uiImage: picture).resizable()
                .aspectRatio(contentMode: wide ? .fit : .fill)
                .frame(width: geometry.size.width, height: geometry.size.height, alignment: wide ? .center : .top)
                .clipped()
        }
    }
}

/// Its kind at a glance: a page in the kind's colour with its ending on it (PDF, MD, XLSX).
struct FileBadge: View {
    let name: String
    let kind: FileKind
    var size: CGFloat = 40

    var body: some View {
        ZStack(alignment: .bottom) {
            Image(systemName: "doc.fill").font(.system(size: size)).foregroundStyle(color)
            Text(FileKinds.badge(name)).font(.system(size: size * 0.22, weight: .heavy)).foregroundStyle(.white)
                .lineLimit(1).minimumScaleFactor(0.6).frame(maxWidth: size * 0.66).padding(.bottom, size * 0.14)
        }
        .frame(width: size * 1.05, height: size * 1.2)
        .accessibilityHidden(true)
    }

    private var color: Color {
        switch kind {
        case .pdf: return Color(red: 0.89, green: 0.24, blue: 0.22)
        case .word: return Color(red: 0.17, green: 0.42, blue: 0.84)
        case .sheet, .table: return Color(red: 0.13, green: 0.56, blue: 0.33)
        case .slides: return Color(red: 0.92, green: 0.45, blue: 0.16)
        case .web: return Color(red: 0.52, green: 0.34, blue: 0.84)
        case .markdown, .text: return Color(red: 0.36, green: 0.43, blue: 0.53)
        case .code: return Color(red: 0.23, green: 0.28, blue: 0.37)
        case .audio: return Color(red: 0.86, green: 0.29, blue: 0.52)
        case .archive: return Color(red: 0.55, green: 0.42, blue: 0.30)
        case .other: return Color.gray
        }
    }
}

/// What it is writing, as the file will look, while it works (from the call that writes it): the
/// page shows before the file is sent, and gives way to the file's own card when it comes.
@MainActor
struct DraftCard: View, Equatable {
    let draft: FileDraft

    nonisolated static func == (a: DraftCard, b: DraftCard) -> Bool { a.draft == b.draft }

    var body: some View {
        let kind = FileKind.of(draft.name)
        VStack(alignment: .leading, spacing: 0) {
            FilePaper(look: look(kind), kind: kind).padding([.horizontal, .top], 8)
            HStack(spacing: 12) {
                FileBadge(name: draft.name, kind: kind)
                VStack(alignment: .leading, spacing: 2) {
                    Text(FileKinds.title(draft.name)).font(.system(size: 16, weight: .semibold)).lineLimit(2)
                    Text(draft.done ? "已写好" : "正在写…").font(.system(size: 13)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                if !draft.done { ProgressView().padding(.trailing, 8) }
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
        }
        .modifier(FileCardFill())
        .accessibilityElement(children: .combine)
    }

    private func look(_ kind: FileKind) -> FileLook {
        switch kind {
        case .table: return .table(FileKinds.rows(draft.content, separator: FileKinds.ext(draft.name) == "tsv" ? "\t" : ","))
        case .web: return .web(draft.content)
        case .markdown: return .text(FileKinds.markdownPreview(String(draft.content.prefix(1_800))))
        default: return .text(String(draft.content.prefix(1_800)))
        }
    }
}

/// A web page file drawn in a card: offline and sandboxed as the chat's cards are, not touchable (a
/// tap opens the file whole). In the chat its picture is taken once it has drawn and kept instead.
@MainActor
struct FileWebPage: UIViewRepresentable {
    let html: String
    var onPicture: ((UIImage) -> Void)?

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.backgroundColor = .white; webView.scrollView.backgroundColor = .white
        webView.scrollView.isScrollEnabled = false; webView.allowsLinkPreview = false
        webView.isUserInteractionEnabled = false
        webView.navigationDelegate = context.coordinator
        context.coordinator.webView = webView
        context.coordinator.prepare()
        return webView
    }
    func updateUIView(_ uiView: WKWebView, context: Context) { context.coordinator.parent = self; context.coordinator.load() }
    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        uiView.stopLoading(); uiView.navigationDelegate = nil
        coordinator.webView = nil
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate {
        var parent: FileWebPage
        weak var webView: WKWebView?
        private var ready = false
        private var loaded = ""
        private var pictureTask: Task<Void, Never>?
        init(_ parent: FileWebPage) { self.parent = parent }

        func prepare() {
            WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "CompanionCardPolicyV2", encodedContentRuleList: EmbeddedCardDocument.networkRules) { [weak self] list, _ in
                Task { @MainActor [weak self] in
                    guard let self, let webView = self.webView else { return }
                    // A second wall: the page's own policy already keeps it offline.
                    if let list { webView.configuration.userContentController.add(list) }
                    self.ready = true; self.load()
                }
            }
        }
        func load() {
            guard ready, loaded != parent.html else { return }
            loaded = parent.html
            webView?.loadHTMLString(FileKinds.webPage(loaded), baseURL: nil)
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard parent.onPicture != nil else { return }
            pictureTask?.cancel()
            pictureTask = Task { @MainActor [weak self] in
                try? await Task.sleep(for: .milliseconds(1_200))  // what it draws with its scripts
                guard let self, !Task.isCancelled, let webView = self.webView, webView.bounds.width > 0 else { return }
                webView.takeSnapshot(with: nil) { [weak self] image, _ in
                    guard let image else { return }
                    Task { @MainActor in self?.parent.onPicture?(image) }
                }
            }
        }
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let address = navigationAction.request.url?.absoluteString ?? ""
            decisionHandler(navigationAction.navigationType == .other && ["about:blank", "about:srcdoc"].contains(address) ? .allow : .cancel)
        }
    }
}

/// A clip in the chat: its first frame, its length and ▶ (a tap plays it full screen).
@MainActor
struct VideoCard: View {
    let item: MediaAttachment
    let baseURL: URL?
    var open: () -> Void
    @Environment(\.displayScale) private var displayScale
    @State private var poster: UIImage?
    @State private var seconds: Double?

    private var key: String { "poster|" + item.url }
    private var posterFile: URL? { LocalStore.url("previews", server: resolvedURL(item.url, baseURL: baseURL)?.absoluteString ?? "", name: key) }
    /// Width over height, kept between a tall phone clip and a wide one.
    private var ratio: CGFloat {
        guard let poster, poster.size.height > 0 else { return 16.0 / 9.0 }
        return min(16.0 / 9.0, max(3.0 / 4.0, poster.size.width / poster.size.height))
    }

    var body: some View {
        Button(action: open) {
            ZStack {
                Color.black
                if let poster { Image(uiImage: poster).resizable().scaledToFill() }
                Image(systemName: "play.fill").font(.system(size: 22, weight: .semibold)).foregroundStyle(.white)
                    .frame(width: 54, height: 54).background(.black.opacity(0.45), in: Circle())
            }
            .frame(width: 260, height: 260 / ratio)
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            .overlay(alignment: .bottomTrailing) {
                if let seconds {
                    Text(Elapsed.clock(seconds * 1_000)).font(.caption2.monospacedDigit().weight(.semibold)).foregroundStyle(.white)
                        .padding(.horizontal, 7).padding(.vertical, 3).background(.black.opacity(0.55), in: Capsule()).padding(8)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("播放视频" + (item.name.map { "「\(FileKinds.title($0))」" } ?? ""))
        .task(id: item.url) { await load() }
    }

    private func load() async {
        seconds = FilePreviewMemory.seconds[item.url]
        if let kept = FilePreviewMemory.pictures.object(forKey: key as NSString) { poster = kept }
        else if let kept = await LocalStore.picture(posterFile, scale: displayScale) {
            FilePreviewMemory.keep(kept, key: key)
            poster = kept
        }
        guard poster == nil || seconds == nil, let url = resolvedURL(item.url, baseURL: baseURL) else { return }
        let asset = AVURLAsset(url: url)
        if poster == nil {
            let generator = AVAssetImageGenerator(asset: asset)
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 720, height: 720)
            let frame = try? await generator.image(at: CMTime(seconds: 0.1, preferredTimescale: 600))
            if let frame {
                let picture = UIImage(cgImage: frame.image)
                FilePreviewMemory.keep(picture, key: key)
                LocalStore.keep(picture, to: posterFile)
                poster = picture
            }
        }
        let duration = try? await asset.load(.duration)
        if let duration, duration.seconds.isFinite, duration.seconds > 0 {
            FilePreviewMemory.seconds[item.url] = duration.seconds
            seconds = duration.seconds
        }
    }
}

/// Audio in the chat, played where it is: ▶, how far along, how long.
@MainActor
struct AudioCard: View {
    let item: MediaAttachment
    let baseURL: URL?
    var open: () -> Void
    @StateObject private var playback = AudioPlayback()

    /// What this iPhone plays itself (not Ogg, Opus or AMR: those open as files).
    nonisolated static func plays(_ name: String) -> Bool {
        ["mp3", "m4a", "aac", "wav", "aif", "aiff", "caf", "flac"].contains(FileKinds.ext(name))
    }

    var body: some View {
        HStack(spacing: 12) {
            Button { playback.toggle(resolvedURL(item.url, baseURL: baseURL)) } label: {
                Image(systemName: playback.playing ? "pause.fill" : "play.fill").font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(CompanionPalette.primaryText)
                    .frame(width: 44, height: 44).background(CompanionPalette.primary, in: Circle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(playback.playing ? "暂停" : "播放「\(FileKinds.title(item.name ?? "音频"))」")
            VStack(alignment: .leading, spacing: 6) {
                Text(FileKinds.title(item.name ?? "音频")).font(.system(size: 15, weight: .semibold)).lineLimit(1)
                ProgressView(value: playback.progress).tint(CompanionPalette.primary)
                Text(playback.time).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            Menu {
                Button("打开", systemImage: "doc.text.magnifyingglass", action: open)
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 17, weight: .semibold)).foregroundStyle(.secondary)
                    .frame(width: 36, height: 44).contentShape(Rectangle())
            }
            .accessibilityLabel("更多：打开或分享")
        }
        .padding(.leading, 12).padding(.trailing, 4).padding(.vertical, 10)
        .frame(width: 300)
        .modifier(BubbleFill(shape: RoundedRectangle(cornerRadius: 22, style: .continuous)))
        .onDisappear { playback.stop() }
    }
}

@MainActor
final class AudioPlayback: ObservableObject {
    @Published private(set) var playing = false
    @Published private(set) var progress = 0.0
    @Published private(set) var time = "0:00"
    private var player: AVPlayer?
    private var ticker: Any?
    private var ended: NSObjectProtocol?

    func toggle(_ url: URL?) {
        if player == nil {
            guard let url else { return }
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try? AVAudioSession.sharedInstance().setActive(true)
            let player = AVPlayer(url: url)
            ticker = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] now in
                MainActor.assumeIsolated { self?.tick(now) }
            }
            ended = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: player.currentItem, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.finished() }
            }
            self.player = player
        }
        if playing { player?.pause() } else { player?.play() }
        playing.toggle()
    }

    private func tick(_ now: CMTime) {
        let total = player?.currentItem?.duration.seconds ?? .nan
        let at = now.seconds.isFinite ? now.seconds : 0
        if total.isFinite, total > 0 {
            progress = min(1, at / total)
            time = Elapsed.clock(at * 1_000) + " / " + Elapsed.clock(total * 1_000)
        } else { time = Elapsed.clock(at * 1_000) }
    }

    private func finished() {
        playing = false
        progress = 0
        player?.seek(to: .zero)
    }

    /// Off screen: it stops, and lets go of the player.
    func stop() {
        player?.pause()
        if let ticker { player?.removeTimeObserver(ticker) }
        if let ended { NotificationCenter.default.removeObserver(ended) }
        ticker = nil; ended = nil; player = nil
        playing = false
    }
}
