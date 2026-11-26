import MarkdownUI
import SwiftUI
import UIKit
@preconcurrency import WebKit

/// Code-block artifact payload opened in `ArtifactPreviewSheet` (HTML or SVG).
struct ArtifactPreviewItem: Identifiable, Equatable, Sendable {
    let id = UUID()
    let title: String
    let language: String
    let code: String

    var isSVG: Bool {
        let lang = language.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if lang == "svg" { return true }
        if lang == "xml" && code.range(of: "<svg", options: .caseInsensitive) != nil { return true }
        return false
    }

    var fileExtension: String { isSVG ? "svg" : "html" }

    static func previewableLanguage(_ language: String?, content: String) -> String? {
        let normalized = (language ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized == "html" || normalized == "htm" { return "html" }
        if normalized == "svg" { return "svg" }
        if normalized == "xml" && content.range(of: "<svg", options: .caseInsensitive) != nil { return "svg" }
        return nil
    }
}

/// Sandboxed, non-persistent `WKWebView` with CSP + content rules + navigation lock for HTML/SVG artifacts.
@MainActor
struct SandboxedArtifactWebView: UIViewRepresentable {
    let code: String
    let isSVG: Bool

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.backgroundColor = .systemBackground
        webView.scrollView.backgroundColor = .systemBackground
        webView.allowsLinkPreview = false
        webView.navigationDelegate = context.coordinator
        context.coordinator.webView = webView
        context.coordinator.prepare()
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        context.coordinator.parent = self
        context.coordinator.loadIfNeeded()
    }

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        uiView.stopLoading()
        uiView.navigationDelegate = nil
        coordinator.webView = nil
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate {
        var parent: SandboxedArtifactWebView
        weak var webView: WKWebView?
        private var policyReady = false
        private var loadedKey = ""

        init(_ parent: SandboxedArtifactWebView) {
            self.parent = parent
        }

        func prepare() {
            WKContentRuleListStore.default().compileContentRuleList(
                forIdentifier: "CompanionCardPolicyV2",
                encodedContentRuleList: EmbeddedCardDocument.networkRules
            ) { [weak self] list, _ in
                Task { @MainActor [weak self] in
                    guard let self, let webView = self.webView else { return }
                    if let list { webView.configuration.userContentController.add(list) }
                    self.policyReady = true
                    self.loadIfNeeded()
                }
            }
        }

        func loadIfNeeded() {
            guard policyReady else { return }
            let nextKey = "\(parent.isSVG)|\(parent.code)"
            guard loadedKey != nextKey else { return }
            loadedKey = nextKey
            let wrapped = parent.isSVG ? FileKinds.svgPage(parent.code) : FileKinds.webPage(parent.code)
            webView?.loadHTMLString(wrapped, baseURL: nil)
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let address = navigationAction.request.url?.absoluteString ?? ""
            let allowed = navigationAction.navigationType == .other
                && (address == "about:blank" || address == "about:srcdoc")
            decisionHandler(allowed ? .allow : .cancel)
        }
    }
}

/// Interactive preview sheet for HTML/SVG code blocks: Preview/Source toggle, copy, and system share.
@MainActor
struct ArtifactPreviewSheet: View {
    enum Mode: Int, CaseIterable {
        case preview, source
        var label: String { self == .preview ? "预览" : "源码" }
    }

    let item: ArtifactPreviewItem
    @Environment(\.dismiss) private var dismiss
    @State private var mode: Mode = .preview
    @State private var copied = false
    @State private var sharedFile: SharedFile?
    @State private var shareCleanupFolder: URL?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("模式", selection: $mode) {
                    ForEach(Mode.allCases, id: \.self) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                Divider()
                if mode == .preview {
                    SandboxedArtifactWebView(code: item.code, isSVG: item.isSVG)
                        .ignoresSafeArea(edges: .bottom)
                } else {
                    CodeSourceScrollView(text: item.code, badge: item.language.uppercased())
                }
            }
            .background(CompanionPalette.background)
            .navigationTitle(item.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                }
                ToolbarItemGroup(placement: .primaryAction) {
                    Button {
                        UIPasteboard.general.string = item.code
                        Haptics.selection()
                        copied = true
                        Task {
                            try? await Task.sleep(for: .seconds(1.5))
                            copied = false
                        }
                    } label: {
                        Image(systemName: copied ? "checkmark" : "doc.on.doc")
                            .frame(minWidth: 32, minHeight: 40)
                    }
                    .accessibilityLabel(copied ? "已复制源码" : "复制源码")

                    Button {
                        prepareShare()
                    } label: {
                        Image(systemName: "square.and.arrow.up")
                            .frame(minWidth: 32, minHeight: 40)
                    }
                    .accessibilityLabel("分享代码文件")
                }
            }
        }
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
        .onDisappear { cleanupShare() }
        .sheet(item: $sharedFile, onDismiss: { sharedFile = nil; cleanupShare() }) { file in
            ShareSheet(items: [file.url])
        }
    }

    private func prepareShare() {
        cleanupShare()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("CompanionArtifact", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let base = FileKinds.safeName(item.title)
        let name = (base as NSString).pathExtension.isEmpty ? "\(base).\(item.fileExtension)" : base
        let file = folder.appendingPathComponent(name)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try Data(item.code.utf8).write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            shareCleanupFolder = folder
            sharedFile = SharedFile(url: file)
        } catch {
            try? FileManager.default.removeItem(at: folder)
        }
    }

    private func cleanupShare() {
        guard let folder = shareCleanupFolder else { return }
        shareCleanupFolder = nil
        try? FileManager.default.removeItem(at: folder)
    }
}

/// Clean monospaced scrollable viewer for code and plain-text archives.
@MainActor
struct CodeSourceScrollView: View {
    let text: String
    var badge: String = ""

    private var lineCount: Int {
        max(1, text.components(separatedBy: "\n").count)
    }

    var body: some View {
        VStack(spacing: 0) {
            if !badge.isEmpty {
                HStack {
                    Text(badge)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("\(lineCount) 行")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(CompanionPalette.surface)
                Divider()
            }
            ScrollView([.horizontal, .vertical]) {
                Text(text)
                    .font(.system(size: 13, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    .padding(16)
            }
        }
        .background(CompanionPalette.background)
    }
}

/// Native reader for code, Markdown, CSV/TSV tables, and plain-text files inside `MediaViewer`.
@MainActor
struct CodeTextReaderView: View {
    enum Tab: Int, CaseIterable {
        case rendered, source
        var label: String { self == .rendered ? "排版" : "源码" }
    }

    let text: String
    let filename: String
    @State private var tab: Tab = .rendered

    private var kind: FileKind { FileKind.of(filename) }

    var body: some View {
        VStack(spacing: 0) {
            if kind == .markdown {
                Picker("阅读模式", selection: $tab) {
                    ForEach(Tab.allCases, id: \.self) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                Divider()
                if tab == .rendered {
                    ScrollView {
                        Markdown(text)
                            .markdownTheme(.basic)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(20)
                    }
                } else {
                    CodeSourceScrollView(text: text, badge: FileKinds.badge(filename))
                }
            } else {
                CodeSourceScrollView(text: text, badge: FileKinds.badge(filename))
            }
        }
        .background(CompanionPalette.background)
    }
}
