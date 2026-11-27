import MarkdownUI
import SwiftUI
import UIKit
@preconcurrency import WebKit

/// Code-block artifact payload opened in `ArtifactPreviewSheet` (HTML, SVG, or Markdown).
struct ArtifactPreviewItem: Identifiable, Equatable, Sendable {
    let id = UUID()
    let title: String
    let language: String
    let code: String

    private var normalizedLanguage: String {
        language.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    var isSVG: Bool {
        if normalizedLanguage == "svg" { return true }
        if normalizedLanguage == "xml" && code.range(of: "<svg", options: .caseInsensitive) != nil { return true }
        return false
    }

    var isMarkdown: Bool {
        ["markdown", "md", "mdown", "mdx"].contains(normalizedLanguage)
    }

    var fileExtension: String {
        if isSVG { return "svg" }
        if isMarkdown { return "md" }
        return "html"
    }

    var creationFormat: String { fileExtension }

    static func previewableLanguage(_ language: String?, content: String) -> String? {
        let normalized = (language ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized == "html" || normalized == "htm" { return "html" }
        if normalized == "svg" { return "svg" }
        if normalized == "xml" && content.range(of: "<svg", options: .caseInsensitive) != nil { return "svg" }
        if ["markdown", "md", "mdown", "mdx"].contains(normalized) { return "md" }
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

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction) async -> WKNavigationActionPolicy {
            let address = navigationAction.request.url?.absoluteString ?? ""
            let allowed = navigationAction.navigationType == .other
                && (address == "about:blank" || address == "about:srcdoc")
            return allowed ? .allow : .cancel
        }
    }
}

/// Interactive preview & live source-edit sheet for HTML, SVG, and Markdown code blocks.
@MainActor
struct ArtifactPreviewSheet: View {
    enum Mode: Int, CaseIterable {
        case preview, edit
        var label: String { self == .preview ? "预览" : "编辑" }
    }

    let item: ArtifactPreviewItem
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var mode: Mode = .preview
    @State private var editedCode: String
    @State private var renderedCode: String
    @State private var debounceTask: Task<Void, Never>?
    @State private var copied = false
    @State private var saving = false
    @State private var savedCreationID: String?
    @State private var saveToast: String?
    @State private var saveError: String?
    @State private var sharedFile: SharedFile?
    @State private var shareCleanupFolder: URL?

    init(item: ArtifactPreviewItem) {
        self.item = item
        _editedCode = State(initialValue: item.code)
        _renderedCode = State(initialValue: item.code)
    }

    private var isDirty: Bool { editedCode != item.code }
    private var lineCount: Int { max(1, editedCode.components(separatedBy: "\n").count) }
    private var canSave: Bool {
        store.connected && !saving && !editedCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                modePickerBar
                statusBanner
                Divider()
                if mode == .preview {
                    previewStage
                } else {
                    editorStage
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
                    toolbarActions
                }
            }
        }
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .tint(CompanionPalette.primary)
        .onChange(of: editedCode) { _, next in
            schedulePreviewRefresh(next)
        }
        .onChange(of: mode) { _, next in
            if next == .preview && renderedCode != editedCode {
                debounceTask?.cancel()
                renderedCode = editedCode
            }
        }
        .onDisappear {
            debounceTask?.cancel()
            cleanupShare()
        }
        .sheet(item: $sharedFile, onDismiss: { sharedFile = nil; cleanupShare() }) { file in
            ShareSheet(items: [file.url])
        }
    }

    @ViewBuilder
    private var modePickerBar: some View {
        HStack(spacing: 10) {
            Picker("画布模式", selection: $mode) {
                ForEach(Mode.allCases, id: \.self) { option in
                    Text(option.label).tag(option)
                }
            }
            .pickerStyle(.segmented)

            if isDirty {
                Button {
                    debounceTask?.cancel()
                    editedCode = item.code
                    renderedCode = item.code
                    Haptics.selection()
                } label: {
                    Label("还原", systemImage: "arrow.uturn.backward")
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 10)
                        .frame(minHeight: 32)
                        .background(CompanionPalette.surface, in: Capsule())
                        .overlay(Capsule().strokeBorder(CompanionPalette.border))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.primary)
                .accessibilityLabel("还原源码")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    @ViewBuilder
    private var statusBanner: some View {
        if let saveError {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                Text(saveError).font(.caption).foregroundStyle(.red).frame(maxWidth: .infinity, alignment: .leading)
                Button { self.saveError = nil } label: {
                    Image(systemName: "xmark").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                }.buttonStyle(.plain)
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 8)
        } else if let saveToast {
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(CompanionPalette.primary)
                Text(saveToast).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 8)
        }
    }

    @ViewBuilder
    private var previewStage: some View {
        if item.isMarkdown {
            ScrollView {
                Markdown(renderedCode)
                    .markdownTheme(.basic)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(20)
            }
        } else {
            SandboxedArtifactWebView(code: renderedCode, isSVG: item.isSVG)
                .ignoresSafeArea(edges: .bottom)
        }
    }

    @ViewBuilder
    private var editorStage: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text(item.fileExtension.uppercased())
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if isDirty {
                    Text("已修改")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(CompanionPalette.primary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(CompanionPalette.primary.opacity(0.14), in: Capsule())
                }
                Spacer()
                Text("\(lineCount) 行")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(CompanionPalette.surface)
            Divider()
            TextEditor(text: $editedCode)
                .font(.system(size: 13.5, design: .monospaced))
                .autocorrectionDisabled(true)
                .textInputAutocapitalization(.never)
                .scrollContentBackground(.hidden)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(CompanionPalette.background)
                .accessibilityLabel("画布源码编辑器")
        }
    }

    @ViewBuilder
    private var toolbarActions: some View {
        Button {
            Task { await saveToLibrary() }
        } label: {
            if saving {
                ProgressView().controlSize(.mini).frame(minWidth: 32, minHeight: 40)
            } else {
                Image(systemName: savedCreationID == nil ? "tray.and.arrow.down" : "tray.and.arrow.down.fill")
                    .frame(minWidth: 32, minHeight: 40)
            }
        }
        .disabled(!canSave)
        .accessibilityLabel(savedCreationID == nil ? "存入资源库" : "更新资源库作品")

        Button {
            UIPasteboard.general.string = editedCode
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

    private func schedulePreviewRefresh(_ nextCode: String) {
        debounceTask?.cancel()
        debounceTask = Task { @MainActor in
            do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
            renderedCode = nextCode
        }
    }

    private func saveToLibrary() async {
        guard canSave else { return }
        saving = true
        saveError = nil
        defer { saving = false }
        do {
            let outcome = try await store.saveCreationArtifact(
                id: savedCreationID,
                title: item.title,
                content: editedCode,
                format: item.creationFormat
            )
            if let nextID = outcome.savedID, !nextID.isEmpty {
                savedCreationID = nextID
            }
            Haptics.selection()
            let message = outcome.updated ? "已原地更新资源库作品" : "已存入资源库，再次点击可原地更新"
            saveToast = message
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(3.5))
                if saveToast == message { saveToast = nil }
            }
        } catch {
            saveError = error.localizedDescription
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
            try Data(editedCode.utf8).write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
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
