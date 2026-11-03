import SwiftUI

/// 外观, as the web app's dock: a small panel at the bottom of the main screen, so the chat above it
/// is the preview — a choice shows at once. Saved on the server for every device.
@MainActor
struct AppearanceSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var theme = "system"
    @State private var size = "medium"
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("外观").font(.headline)
                Spacer()
                Button("完成") { dismiss() }
                    .font(.subheadline.weight(.semibold)).padding(.horizontal, 14).frame(minHeight: 34)
                    .background(CompanionPalette.primary, in: Capsule()).foregroundStyle(CompanionPalette.primaryText)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("模式").font(.caption).foregroundStyle(.secondary)
                Picker("模式", selection: $theme) {
                    Text("跟随系统").tag("system"); Text("浅色").tag("light"); Text("深色").tag("dark")
                }.pickerStyle(.segmented).disabled(busy)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("形象大小").font(.caption).foregroundStyle(.secondary)
                Picker("形象大小", selection: $size) {
                    Text("小").tag("small"); Text("中").tag("medium"); Text("大").tag("large")
                }.pickerStyle(.segmented).disabled(busy)
            }
            if let error {
                Label(error, systemImage: "exclamationmark.circle").font(.footnote).foregroundStyle(.red)
            } else {
                Text("上面的主聊天就是预览：选了马上能看到效果。").font(.footnote).foregroundStyle(.secondary)
            }
        }
        .padding(20)
        .onAppear { theme = store.companionState["theme_mode"].string ?? "system"; size = store.companionState["pet_size"].string ?? "medium" }
        .onChange(of: theme) { old, new in if old != new && new != store.companionState["theme_mode"].stringValue { save() } }
        .onChange(of: size) { old, new in if old != new && new != store.companionState["pet_size"].stringValue { save() } }
    }

    private func save() {
        guard !busy else { return }
        busy = true; error = nil
        Task {
            do {
                let state = try await store.changeResource("companion.update", payload: ["changes": .object(["theme_mode": .string(theme), "pet_size": .string(size)])])
                store.companionState = state; Haptics.selection()
            } catch { self.error = error.localizedDescription }
            busy = false
        }
    }
}

private struct OpenAppearanceKey: EnvironmentKey {
    static var defaultValue: @MainActor () -> Void { {} }
}

extension EnvironmentValues {
    /// Settings → 外观: closes settings and shows the appearance dock over the main screen.
    var openAppearance: @MainActor () -> Void {
        get { self[OpenAppearanceKey.self] }
        set { self[OpenAppearanceKey.self] = newValue }
    }
}
