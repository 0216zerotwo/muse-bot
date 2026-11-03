import SwiftUI

@MainActor
struct VaultSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/webui/companion/vault")
    @State private var handoff = false
    @State private var removing: JSONValue?
    var body: some View {
        Form {
            Section {
                Text("网站登录和服务密钥都加密存在你的服务器上。它需要时会在聊天里弹出卡片让你填，不用把密码发在聊天里。")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            Section {
                ForEach(SettingsDecoding.rows(resource.value, key: "logins", id: "id"), id: \.selfID) { login in
                    credential(login, kind: "login", id: login["id"].stringValue, detail: login["origin"].stringValue)
                }
                if resource.value != .null && resource.value["logins"].array.isEmpty { Text("还没有。").foregroundStyle(.secondary) }
            } header: { HStack { Text("网站登录"); Spacer(); Button("添加") { handoff = true } } }
            Section {
                ForEach(SettingsDecoding.rows(resource.value, key: "keys", id: "env"), id: \.selfID) { key in
                    credential(key, kind: "key", id: key["env"].stringValue, detail: key["set"].boolValue ? "已保存" : "尚未设置")
                }
                if resource.value != .null && resource.value["keys"].array.isEmpty { Text("还没有。模型的密钥在「模型」里管理。").foregroundStyle(.secondary) }
            } header: { HStack { Text("API 密钥"); Spacer(); Button("添加") { handoff = true } } }
        }.settingsStyle("安全凭证").task { await resource.load(store) }.refreshable { await resource.load(store) }
            .sheet(isPresented: $handoff) {
                SettingsBrowserHandoff(title: "在安全凭证页填写", explanation: "请在伙伴网页版的「设置 → 安全凭证」中填写登录信息或密钥，内容会直接保存到服务器。").padding(24)
                    .presentationDetents([.medium, .large]).presentationDragIndicator(.visible)
            }
            .confirmationDialog("移除这项凭证？", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
                Button("移除", role: .destructive) {
                    if let item = removing { Task { await resource.change(store, "companion.vault.change", ["action": .string("remove"), "kind": item["kind"], "id": item["id"]]) } }
                    removing = nil
                }
            }
    }
    private func credential(_ item: JSONValue, kind: String, id: String, detail: String) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(item["label"].nonemptyString ?? id)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("移除", role: .destructive) { removing = .object(["kind": .string(kind), "id": .string(id)]) }
                .disabled(resource.busy).frame(minHeight: 44)
        }
    }
}
