import SwiftUI
import UIKit

@MainActor
enum Haptics {
    static func selection() { UISelectionFeedbackGenerator().selectionChanged() }
    static func send() { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
    static func hold() { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
}

@MainActor
struct ConnectionView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var server = ""
    @State private var code = ""
    @State private var help = false
    @FocusState private var focused: Bool

    private var pairing: Bool { store.needsPairing && !store.needsServer }

    var body: some View {
        ZStack {
            GlassBackdrop()
            ScrollView {
                VStack(spacing: 28) {
                    VStack(spacing: 10) {
                        Text(pairing ? "输入配对码" : "连接你的伙伴").font(.title.bold())
                        Text(pairing ? "在服务器上生成一个一次性配对码" : "填上你的服务器地址就能开始")
                            .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }.padding(.top, 150)

                    VStack(spacing: 14) {
                        if pairing {
                            TextField("ABCD-2345" as String, text: $code)
                                .font(.title3.monospaced()).multilineTextAlignment(.center)
                                .textInputAutocapitalization(.characters).autocorrectionDisabled()
                                .textContentType(.oneTimeCode).focused($focused).submitLabel(.go)
                                .onSubmit(pair).fieldStyle().accessibilityLabel("一次性配对码")
                            primaryButton("配对", enabled: !code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, action: pair)
                        } else {
                            TextField("http://192.168.1.20:8766" as String, text: $server)
                                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                                .focused($focused).submitLabel(.go).onSubmit(connect)
                                .fieldStyle().accessibilityLabel("服务器地址或连接链接")
                            primaryButton("连接", enabled: !server.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, action: connect)
                        }
                    }.padding(16).modifier(GlassCard(radius: 30))

                    if let error = store.errorMessage {
                        Label(error, systemImage: "exclamationmark.circle").font(.footnote).foregroundStyle(.red)
                            .multilineTextAlignment(.center).accessibilityAddTraits(.updatesFrequently)
                    }

                    VStack(spacing: 12) {
                        Button(pairing ? "怎么拿配对码？" : "填什么地址？") {
                            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { help.toggle() }
                        }.font(.footnote).foregroundStyle(.secondary).frame(minHeight: 44)
                        if help {
                            Group {
                                if pairing {
                                    VStack(spacing: 6) {
                                        Text("在服务器电脑上运行：")
                                        Text(verbatim: "python tools/companion-pair.py").font(.footnote.monospaced()).textSelection(.enabled)
                                    }
                                } else {
                                    Text("同一个 Wi-Fi 下填电脑的局域网地址；在外面就填服务器的 HTTPS 地址。也可以直接粘贴带配对码的连接链接。")
                                }
                            }.font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
                                .padding(14).modifier(GlassCard(radius: 20)).transition(.opacity)
                        }
                        if pairing {
                            Button("更换服务器地址") { code = ""; store.changeServer() }
                                .font(.footnote).foregroundStyle(.secondary).frame(minHeight: 44)
                        }
                    }
                }.frame(maxWidth: 420).padding(.horizontal, 24).frame(maxWidth: .infinity)
            }.scrollDismissesKeyboard(.interactively)
        }
        .onAppear { server = store.serverAddress }
        .onChange(of: pairing) { _, _ in help = false }
    }

    @ViewBuilder
    private func primaryButton(_ title: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        let label = HStack(spacing: 8) {
            if store.connecting { ProgressView().controlSize(.small) }
            Text(store.connecting ? "正在连接…" : title).font(.headline)
        }.frame(maxWidth: .infinity).frame(minHeight: 44)
        if #available(iOS 26.0, *) {
            Button(action: action) { label }
                .buttonStyle(.glassProminent).tint(CompanionPalette.primary)
                .disabled(!enabled || store.connecting).accessibilityLabel(title)
        } else {
            Button(action: action) { label }
                .buttonStyle(.borderedProminent).tint(CompanionPalette.primary).clipShape(Capsule())
                .disabled(!enabled || store.connecting).accessibilityLabel(title)
        }
    }

    private func connect() { focused = false; Task { await store.connect(server: server) } }
    private func pair() { focused = false; Task { await store.pair(code: code) } }
}

/// Soft colour behind the connecting screen, so the glass has something to show through.
private struct GlassBackdrop: View {
    var body: some View {
        ZStack {
            CompanionPalette.background
            Circle().fill(CompanionPalette.primary.opacity(0.45)).frame(width: 340).blur(radius: 90).offset(x: -120, y: -260)
            Circle().fill(CompanionPalette.userBubble.opacity(0.55)).frame(width: 300).blur(radius: 100).offset(x: 140, y: 120)
            Circle().fill(CompanionPalette.primary.opacity(0.25)).frame(width: 260).blur(radius: 90).offset(x: -80, y: 380)
        }.ignoresSafeArea().accessibilityHidden(true)
    }
}

private extension View {
    func fieldStyle() -> some View {
        padding(.horizontal, 18).frame(minHeight: 52)
            .background(Color.primary.opacity(0.06), in: Capsule())
            .overlay(Capsule().strokeBorder(Color.primary.opacity(0.08)))
    }
}
