import SwiftUI
import Combine

/// A failed row never prevents the other rows from being displayed.
enum SettingsDecoding {
    static func rows(_ value: JSONValue, key: String, id: String) -> [JSONValue] {
        value[key].array.filter { !$0.object.isEmpty && $0[id].nonemptyString != nil }
    }
    static func title(_ row: JSONValue, _ key: String, fallback: String = "") -> String {
        row[key].nonemptyString ?? fallback
    }
    static func date(_ value: JSONValue) -> String {
        guard let stamp = value.doubleValue else { return value.nonemptyString ?? "" }
        let date = Date(timeIntervalSince1970: stamp > 100_000_000_000 ? stamp / 1000 : stamp)
        return date.formatted(.dateTime.month().day().hour().minute().locale(Locale(identifier: "zh_CN")))
    }
    static func tokens(_ value: Double) -> String {
        value >= 10_000 ? String(format: "%.1f 万", value / 10_000) : String(format: "%.0f", value)
    }
    static func cost(_ value: JSONValue) -> String {
        guard let cost = value.doubleValue else { return "未填价格" }
        return cost < 0.01 ? "不到 1 分钱" : String(format: "约 ¥%.2f", cost)
    }
}

@MainActor
final class SettingsResource: ObservableObject {
    let path: String
    @Published var value: JSONValue = .null
    @Published var loading = false
    @Published var busy = false
    @Published var error: String?
    init(_ path: String) { self.path = path }
    func load(_ store: CompanionStore) async {
        loading = true; error = nil
        defer { loading = false }
        do { value = try await store.getResource(path) }
        catch { self.error = error.localizedDescription }
    }
    @discardableResult
    func change(_ store: CompanionStore, _ action: String, _ payload: [String: JSONValue]) async -> Bool {
        busy = true; error = nil
        defer { busy = false }
        do {
            value = try await store.changeResource(action, payload: payload)
            if action.hasPrefix("settings.mcp.") {
                if value["last_action"]["ok"] == .bool(false) {
                    self.error = value["last_action"]["error"].nonemptyString ?? value["last_action"]["message"].nonemptyString ?? "连接没有完成，请重试。"
                    return false
                }
                if value["hot_reload"]["ok"] == .bool(false) {
                    self.error = value["hot_reload"]["message"].nonemptyString ?? "设置已保存，服务器还没有重新连接这个服务。"
                    return false
                }
            }
            Haptics.selection(); return true
        }
        catch { self.error = error.localizedDescription; return false }
    }
}

@MainActor
struct SettingsStatus: View {
    @ObservedObject var resource: SettingsResource
    var retry: () -> Void
    var body: some View {
        Group {
            if resource.loading { ProgressView("正在读取…").frame(maxWidth: .infinity) }
            if resource.busy { ProgressView("正在保存…").frame(maxWidth: .infinity) }
            if let error = resource.error { ResourceErrorView(message: error, retry: retry) }
        }
    }
}

private struct SettingsCloseKey: EnvironmentKey {
    static var defaultValue: @MainActor () -> Void { {} }
}

extension EnvironmentValues {
    var settingsClose: @MainActor () -> Void {
        get { self[SettingsCloseKey.self] }
        set { self[SettingsCloseKey.self] = newValue }
    }
}

@MainActor
private struct SettingsPageStyle: ViewModifier {
    let title: String
    @Environment(\.settingsClose) private var close
    func body(content: Content) -> some View {
        content.navigationTitle(title).navigationBarTitleDisplayMode(.inline)
            .scrollContentBackground(.hidden).background(CompanionPalette.surface)
            .labelStyle(SettingsIconLabel()).tint(CompanionPalette.primary)
            .scrollDismissesKeyboard(.interactively)
            .toolbar { ToolbarItem(placement: .topBarTrailing) {
                Button { close() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }
                    .accessibilityLabel("关闭设置")
            } }
    }
}

/// Row icons in plain text colour, as the web settings draw them (not tinted).
struct SettingsIconLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 12) {
            configuration.icon.foregroundStyle(.secondary).frame(width: 24)
            configuration.title
        }
    }
}

extension View {
    @MainActor func settingsStyle(_ title: String) -> some View { modifier(SettingsPageStyle(title: title)) }
}

@MainActor
struct SettingsBrowserHandoff: View {
    @EnvironmentObject private var store: CompanionStore
    var title: String
    var explanation: String
    @State private var safari: SafariDestination?
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(title, systemImage: "globe").font(.headline)
            Text(explanation).font(.subheadline).foregroundStyle(.secondary)
            Text("在服务器上的伙伴网页中完成后，回到这里刷新。网页可能需要单独配对。")
                .font(.footnote).foregroundStyle(.secondary)
            Button("打开 Muse Bot 网页版") {
                if let url = resolvedURL("/companion.html", baseURL: store.baseURL) { safari = SafariDestination(url: url) }
            }.buttonStyle(.borderedProminent).frame(minHeight: 44)
        }
        .sheet(item: $safari) { InAppBrowser(url: $0.url).presentationDragIndicator(.visible) }
    }
}
