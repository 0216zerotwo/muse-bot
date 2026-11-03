import SwiftUI

@MainActor
struct SettingsView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openAppearance) private var openAppearance
    @StateObject private var models = SettingsResource("/api/webui/companion/models")
    @StateObject private var usage = SettingsResource("/api/webui/companion/usage")
    @StateObject private var skills = SettingsResource("/api/webui/companion/skills")
    @StateObject private var services = SettingsResource("/api/settings/mcp-presets")
    /// How much this iPhone keeps (chats, pictures, card pictures, lists), for the clear row.
    @State private var keptBytes: Int64?
    @State private var confirmClear = false
    @AppStorage(ReadAloud.afterVoiceKey) private var readAfterVoice = true
    var body: some View {
        NavigationStack {
            Form {
                Section("通用") {
                    NavigationLink { NotificationsSettingsPage() } label: { Label("通知", systemImage: "bell") }
                    Button { openAppearance() } label: {
                        HStack {
                            row("外观", "paintpalette", "\(themeLabel) · 形象\(sizeLabel)")
                            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        }.foregroundStyle(.primary).contentShape(Rectangle())
                    }.buttonStyle(.plain)  // a row like the others, not an accent-coloured link
                    Toggle(isOn: $readAfterVoice) { Label("语音输入后朗读回复", systemImage: "speaker.wave.2") }
                }
                Section("模型") {
                    NavigationLink { ModelsSettingsPage() } label: {
                        row("模型", "cpu", summary(models, models.value["settings"]["enabled"].boolValue ? "自动" : models.value["settings"]["fixed"].stringValue))
                    }
                    NavigationLink { UsageSettingsPage() } label: {
                        row("用量", "chart.bar", summary(usage, "今天 \(SettingsDecoding.tokens(todayTokens)) token"))
                    }
                }
                Section("能力") {
                    NavigationLink { SkillsSettingsPage() } label: { row("技能", "graduationcap", summary(skills, "学会了 \(skills.value["learned"].array.count) 个")) }
                }
                Section("连接") {
                    NavigationLink { ConnectionsSettingsPage() } label: { row("已连接的服务", "powerplug", summary(services, "\(connectedCount) 个")) }
                }
                Section("安全") {
                    NavigationLink { PermissionsSettingsPage() } label: { Label("权限", systemImage: "checkmark.shield") }
                    NavigationLink { VaultSettingsPage() } label: { Label("安全凭证", systemImage: "key") }
                    NavigationLink { DevicesSettingsPage() } label: { Label("设备", systemImage: "iphone") }
                }
                if models.error != nil || usage.error != nil || skills.error != nil || services.error != nil {
                    Section { ResourceErrorView(message: "部分设置摘要没能读取，可打开对应页面重试。") { Task { await loadSummaries() } } }
                }
                Section("服务器") {
                    Text(store.serverAddress).font(.footnote).textSelection(.enabled)
                    Button("更换服务器地址") { dismiss(); store.changeServer() }
                    Button { confirmClear = true } label: {
                        HStack {
                            Text("清除这台 iPhone 上的缓存")
                            Spacer()
                            Text(keptBytes.map { ByteCountFormatter.string(fromByteCount: $0, countStyle: .file) } ?? "…")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }.contentShape(Rectangle())
                    }
                    .confirmationDialog("清除缓存", isPresented: $confirmClear, titleVisibility: .hidden) {
                        Button("清除缓存", role: .destructive) { Task { await clearKept() } }
                    } message: {
                        Text("聊天、图片和互动内容会在用到时重新从服务器读取。服务器上的内容不受影响。")
                    }
                    LabeledContent("版本", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0")
                }
            }
            .settingsStyle("设置")
        }
        .environment(\.settingsClose, { dismiss() })
        .inAppLinks()
        .task { await loadSummaries() }
        .task { await measureKept() }
    }
    private func measureKept() async { keptBytes = await Task.detached(priority: .utility) { LocalStore.size() }.value }
    private func clearKept() async {
        store.savedEvents = [:]
        await Task.detached(priority: .userInitiated) { LocalStore.clearAll() }.value
        Haptics.selection()
        await measureKept()
    }
    private var themeLabel: String { ["system": "跟随系统", "dark": "深色", "light": "浅色"][store.companionState["theme_mode"].stringValue] ?? "跟随系统" }
    private var sizeLabel: String { ["small": "小", "medium": "中", "large": "大"][store.companionState["pet_size"].stringValue] ?? "中" }
    private var todayTokens: Double { usage.value["today"]["by_source"].object.values.reduce(0) { $0 + ($1.doubleValue ?? 0) } }
    private var connectedCount: Int { services.value["presets"].array.filter { $0["installed"].boolValue || $0["configured"].boolValue }.count }
    private func summary(_ resource: SettingsResource, _ value: String) -> String { resource.value == .null ? (resource.error == nil ? "正在读取…" : "读取失败") : value }
    private func loadSummaries() async { await models.load(store); await usage.load(store); await skills.load(store); await services.load(store) }
    private func row(_ title: String, _ icon: String, _ detail: String) -> some View {
        HStack { Label(title, systemImage: icon); Spacer(); Text(detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1) }
    }
}

@MainActor
struct NotificationsSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @AppStorage(ReminderNotifications.enabledKey) private var reminderNotices = true
    var body: some View {
        Form {
            Section {
                // This app gets no push: the phone itself goes off when a reminder is due.
                Toggle(isOn: $reminderNotices) { Label("提醒到点时通知我", systemImage: "bell.badge") }
                    .onChange(of: reminderNotices) { _, on in
                        Task {
                            if on { await store.planReminders() } else { await ReminderNotifications.plan([], title: "") }
                        }
                    }
            } footer: {
                Text("喝水、吃药、每日早报这类定时提醒，到点时手机会直接响，不用开着 App。打开 App 或聊完一件事时，提醒时间会自动更新。")
            }
            Section {
                Label("回复和动态", systemImage: "bubble.left")
                Text("打开 App 时会实时收到回复和新动态；App 没打开时，其他消息要等下次打开才能看到。")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
        }.settingsStyle("通知")
    }
}
