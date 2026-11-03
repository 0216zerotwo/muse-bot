import SwiftUI

@MainActor
struct CustomServiceSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var resource: SettingsResource
    @State private var kind = "url"
    @State private var name = ""
    @State private var address = ""
    @State private var command = ""
    @State private var credential = ""
    var body: some View {
        Form {
            Section {
                Picker("怎么连", selection: $kind) { Text("网址").tag("url"); Text("命令").tag("command") }.pickerStyle(.segmented)
                TextField("名称，小写英文、数字和横线", text: $name)
                if kind == "url" {
                    TextField("https://example.com/mcp", text: $address).keyboardType(.URL)
                    SecureField("密钥（可选）", text: $credential)
                } else {
                    TextField("服务器上的启动命令", text: $command)
                    SecureField("设置（可选，每行 名字=值）", text: $credential)
                }
                Text("服务会在你的服务器上运行。").font(.footnote).foregroundStyle(.secondary)
            }.textInputAutocapitalization(.never).autocorrectionDisabled()
            SettingsStatus(resource: resource) { resource.error = nil }
            Button("添加并连接") {
                let payload: [String: JSONValue] = kind == "url"
                    ? ["kind": .string(kind), "name": .string(name), "url": .string(address), "token": .string(credential)]
                    : ["kind": .string(kind), "name": .string(name), "command": .string(command), "env": .string(credential)]
                Task {
                    if await resource.change(store, "settings.mcp.custom", payload) {
                        credential = ""
                        if resource.value["hot_reload"]["ok"] == .bool(false) { resource.error = resource.value["hot_reload"]["message"].stringValue }
                        else { dismiss() }
                    }
                }
            }.disabled(resource.busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || (kind == "url" ? address.isEmpty : command.isEmpty))
        }.settingsStyle("自己添加一个服务")
    }
}
