import SwiftUI

@MainActor
struct PermissionsSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/permissions")
    private var kinds: [JSONValue] { SettingsDecoding.rows(resource.value, key: "kinds", id: "key") }
    var body: some View {
        Form {
            Section { Text("它和帮手做这些事之前，按这里的设置来。").font(.subheadline).foregroundStyle(.secondary) }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            Section {
                ForEach(kinds, id: \.selfID) { kind in
                    HStack(alignment: .center, spacing: 12) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(kind["label"].nonemptyString ?? kind["key"].stringValue)
                            Text(kind["detail"].stringValue).font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                        Menu {
                            ForEach(kind["levels"].array.compactMap(\.nonemptyString), id: \.self) { level in
                                Button(levelName(level), systemImage: kind["level"].stringValue == level ? "checkmark" : "circle") {
                                    Task { await resource.change(store, "companion.permissions.set", ["kind": kind["key"], "level": .string(level)]) }
                                }
                            }
                        } label: {
                            HStack(spacing: 6) { Text(levelName(kind["level"].stringValue)); Image(systemName: "chevron.down").font(.caption) }
                                .font(.subheadline).padding(.horizontal, 12).frame(minHeight: 44)
                                .background(CompanionPalette.elevated, in: Capsule())
                        }.disabled(resource.busy).accessibilityLabel("\(kind["label"].stringValue)：\(levelName(kind["level"].stringValue))")
                    }.padding(.vertical, 6)
                }
            }
        }.settingsStyle("权限").task { await resource.load(store) }.refreshable { await resource.load(store) }
    }
    private func levelName(_ key: String) -> String { ["deny": "禁止", "ask": "询问我", "allow": "允许"][key] ?? "询问我" }
}

extension JSONValue {
    var selfID: String {
        for key in ["id", "key", "name", "env"] { if let value = self[key].nonemptyString { return value } }
        return "unknown"
    }
}
