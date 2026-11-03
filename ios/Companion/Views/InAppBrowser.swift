import SwiftUI
import UIKit
import WebKit

/// A web page opened from the app (a link in 动态, a reply, a card): it slides up over the app with
/// its title and × at the top right, instead of leaving for Safari.
@MainActor
struct InAppBrowser: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss
    @StateObject private var page = WebPageState()

    var body: some View {
        NavigationStack {
            WebPageView(url: url, state: page)
                .ignoresSafeArea(edges: .bottom)
                .overlay(alignment: .top) {
                    if page.progress < 1 {
                        ProgressView(value: page.progress).tint(CompanionPalette.primary).accessibilityLabel("正在打开")
                    }
                }
                .navigationTitle(page.title.isEmpty ? (url.host ?? "网页") : page.title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        if page.canGoBack {
                            Button { page.webView?.goBack() } label: { Image(systemName: "chevron.left").frame(width: 44, height: 44) }
                                .accessibilityLabel("后退")
                        }
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark").font(.body.weight(.semibold)).frame(width: 44, height: 44)
                        }.accessibilityLabel("关闭")
                    }
                }
        }
        .tint(CompanionPalette.primary)
    }
}

@MainActor
final class WebPageState: ObservableObject {
    @Published var title = ""
    @Published var progress = 0.0
    @Published var canGoBack = false
    weak var webView: WKWebView?
}

@MainActor
private struct WebPageView: UIViewRepresentable {
    let url: URL
    @ObservedObject var state: WebPageState

    func makeCoordinator() -> Coordinator { Coordinator(state: state) }

    func makeUIView(context: Context) -> WKWebView {
        let view = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        view.allowsBackForwardNavigationGestures = true
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        context.coordinator.watch(view)
        state.webView = view
        view.load(URLRequest(url: url))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}

    static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) {
        view.stopLoading()
        coordinator.observations.removeAll()
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let state: WebPageState
        var observations: [NSKeyValueObservation] = []
        init(state: WebPageState) { self.state = state }

        func watch(_ view: WKWebView) {
            observations = [
                view.observe(\.title, options: [.initial, .new]) { [weak self] view, _ in
                    let title = view.title ?? ""
                    Task { @MainActor in self?.state.title = title }
                },
                view.observe(\.estimatedProgress, options: [.initial, .new]) { [weak self] view, _ in
                    let progress = view.estimatedProgress
                    Task { @MainActor in self?.state.progress = progress }
                },
                view.observe(\.canGoBack, options: [.initial, .new]) { [weak self] view, _ in
                    let back = view.canGoBack
                    Task { @MainActor in self?.state.canGoBack = back }
                },
            ]
        }

        /// Links that ask for a new window open in this page.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if navigationAction.targetFrame == nil { webView.load(navigationAction.request) }
            return nil
        }

        /// Web pages stay here; other kinds of link (mail, phone, app links) go to the system.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url, let scheme = url.scheme?.lowercased() else { decisionHandler(.cancel); return }
            if ["http", "https", "about", "data", "blob"].contains(scheme) { decisionHandler(.allow); return }
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
        }
    }
}

/// Web links in this part of the app open in `InAppBrowser`. Each sheet applies it again, since a
/// sheet can't present from the screen underneath it.
private struct InAppLinks: ViewModifier {
    @State private var page: SafariDestination?
    func body(content: Content) -> some View {
        content
            .environment(\.openURL, OpenURLAction { url in
                guard ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return .systemAction }
                page = SafariDestination(url: url)
                return .handled
            })
            .sheet(item: $page) { InAppBrowser(url: $0.url).presentationDragIndicator(.visible) }
    }
}

extension View {
    func inAppLinks() -> some View { modifier(InAppLinks()) }
}
