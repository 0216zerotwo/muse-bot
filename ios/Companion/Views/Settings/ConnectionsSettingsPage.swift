import SwiftUI

@MainActor
struct ConnectionsSettingsPage: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var resource = SettingsResource("/api/settings/mcp-presets")
    @State private var query = ""
    private let groups: [(String, [String])] = [("日常", ["email", "icloud-calendar", "kuaidi100"]), ("出行", ["amap", "train-12306"]), ("替你动手", ["browser", "minimax"]), ("家里", ["home-assistant"])]
    private var presets: [JSONValue] { SettingsDecoding.rows(resource.value, key: "presets", id: "name") }
    private var connected: [JSONValue] { presets.filter(isConnected) }
    private var recommended: Set<String> { Set(groups.flatMap { $0.1 }) }
    var body: some View {
        Form {
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            Section("已连接") {
                ForEach(connected, id: \.selfID) { service in serviceLink(service) }
                if resource.value != .null && connected.isEmpty { Text("还没有已连接的服务。").foregroundStyle(.secondary) }
            }
            ForEach(groups.indices, id: \.self) { index in
                let group = groups[index]
                let rows = presets.filter { group.1.contains($0["name"].stringValue) && !isConnected($0) }
                if !rows.isEmpty {  // a category with nothing left to connect is left out
                    Section(group.0) {
                        ForEach(rows, id: \.selfID) { service in serviceLink(service) }
                    }
                }
            }
            Section("更多服务") {
                TextField("搜索服务", text: $query).autocorrectionDisabled()
                ForEach(presets.filter { !isConnected($0) && !recommended.contains($0["name"].stringValue) && $0["install_supported"].boolValue && matches($0) }, id: \.selfID) { service in serviceLink(service) }
                NavigationLink("自己添加一个服务") { CustomServiceSettingsPage(resource: resource) }
            }
        }.settingsStyle("连接").task { await resource.load(store) }.refreshable { await resource.load(store) }
    }
    private func isConnected(_ row: JSONValue) -> Bool { row["installed"].boolValue || row["configured"].boolValue }
    private func matches(_ row: JSONValue) -> Bool { query.isEmpty || (row["name"].stringValue + row["display_name"].stringValue + row["description"].stringValue).localizedCaseInsensitiveContains(query) }
    private func serviceLink(_ service: JSONValue) -> some View {
        NavigationLink { ConnectionDetailPage(name: service["name"].stringValue, resource: resource) } label: {
            HStack(spacing: 12) {
                // The service's own logo, as the web list shows it (its initial on its colour without one).
                VendorLogo(vendor: .object(["name": .string(service["display_name"].nonemptyString ?? service["name"].stringValue),
                                            "logo_url": service["logo_url"], "color": service["brand_color"]]), size: 28)
                VStack(alignment: .leading, spacing: 5) {
                    Text(service["display_name"].nonemptyString ?? service["name"].stringValue)
                    Text(service["description"].stringValue).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                Text(isConnected(service) ? (service["enabled"].boolValue ? "已连接" : "已暂停") : "连接").font(.caption).foregroundStyle(isConnected(service) ? Color.secondary : CompanionPalette.primary)
            }.padding(.vertical, 5)
        }
    }
}

@MainActor
struct ConnectionDetailPage: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    var name: String
    @ObservedObject var resource: SettingsResource
    @State private var values: [String: String] = [:]
    @State private var allTools: [String] = []
    @State private var confirmRemove = false
    @State private var handoff = false
    private var service: JSONValue { resource.value["presets"].array.first { $0["name"].stringValue == name } ?? .null }
    private var connected: Bool { service["installed"].boolValue || service["configured"].boolValue }
    private var tools: [String] { allTools.isEmpty ? service["tool_names"].array.compactMap(\.nonemptyString) : allTools }
    private var enabled: [String] { service["enabled_tools"].array.compactMap(\.nonemptyString) }
    var body: some View {
        Form {
            Section { Text(service["description"].stringValue).font(.subheadline).foregroundStyle(.secondary) }
            SettingsStatus(resource: resource) { Task { await resource.load(store) } }
            if connected {
                Section("它能做的事") {
                    if tools.isEmpty { Text("暂时读不到，连接正常后再看。").foregroundStyle(.secondary) }
                    ForEach(tools.filter { !$0.hasSuffix("_unsafe") || enabled.contains($0) }, id: \.self) { tool in
                        Toggle(rawName(tool), isOn: Binding(get: { enabled.contains("*") || enabled.contains(tool) || enabled.contains(rawName(tool)) }, set: { on in toggleTool(tool, on) }))
                            .disabled(resource.busy).font(.subheadline)
                    }
                    Button("刷新能力列表") { Task { await test() } }.disabled(resource.busy)
                    if !tools.filter({ isReadOnly($0) }).isEmpty {
                        Button("只允许查看") {
                            let reading = tools.filter(isReadOnly)
                            Task { await resource.change(store, "settings.mcp.tools", ["name": .string(name), "enabled_tools": .array(reading.map(JSONValue.string))]) }
                        }.disabled(resource.busy)
                    }
                }
                Section {
                    Button(service["enabled"].boolValue ? "暂停使用" : "恢复使用") {
                        run(service["enabled"].boolValue ? "disable" : "enable")
                    }.disabled(resource.busy)
                    Button("测试连接") { Task { await test() } }.disabled(resource.busy)
                    Button("断开连接", role: .destructive) { confirmRemove = true }.disabled(resource.busy)
                }
            }
            if !service["required_fields"].array.isEmpty || !connected {
                Section(connected ? "重新填写凭据" : "连接设置") {
                    ForEach(service["required_fields"].array.filter { $0["name"].nonemptyString != nil }, id: \.selfID) { field in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(field["label"].nonemptyString ?? field["name"].stringValue).font(.subheadline)
                            if field["secret"].boolValue {
                                SecureField(field["configured"].boolValue ? "已保存，留空保留" : field["placeholder"].stringValue, text: fieldBinding(field["name"].stringValue))
                            } else {
                                TextField(field["configured"].boolValue ? "已保存，留空保留" : field["placeholder"].stringValue, text: fieldBinding(field["name"].stringValue))
                            }
                        }.textInputAutocapitalization(.never).autocorrectionDisabled()
                    }
                    if let note = service["note"].nonemptyString { Text(note).font(.footnote).foregroundStyle(.secondary) }
                    Button(connected ? "保存并连接" : "连接") { run("enable", includeValues: true) }.disabled(resource.busy || missingFields)
                    if service["required_fields"].array.isEmpty { Button("需要登录？在服务器浏览器完成") { handoff = true } }
                }
            }
        }.settingsStyle(service["display_name"].nonemptyString ?? name)
            .task { if connected && !enabled.contains("*") { await test() } }
            .confirmationDialog("断开这个服务？", isPresented: $confirmRemove) { Button("断开", role: .destructive) { run("remove") } }
            .sheet(isPresented: $handoff) {
                SettingsBrowserHandoff(title: "在服务器上登录服务", explanation: "这个服务需要服务器浏览器上的登录状态。在伙伴网页版的连接页面完成登录后，回来刷新即可。").padding(24).presentationDetents([.medium, .large])
            }
    }
    private var missingFields: Bool {
        service["required_fields"].array.contains { $0["required"].boolValue && !$0["configured"].boolValue && (values[$0["name"].stringValue] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
    private func fieldBinding(_ key: String) -> Binding<String> { Binding(get: { values[key] ?? "" }, set: { values[key] = $0 }) }
    private func run(_ action: String, includeValues: Bool = false) {
        var payload: [String: JSONValue] = ["name": .string(name)]
        if includeValues { for (key, value) in values where !value.isEmpty { payload[key] = .string(value) } }
        Task {
            if await resource.change(store, "settings.mcp." + action, payload) {
                values = [:]
                if resource.value["last_action"]["ok"] == .bool(false) { resource.error = resource.value["last_action"]["error"].nonemptyString ?? resource.value["last_action"]["message"].stringValue }
                else if resource.value["hot_reload"]["ok"] == .bool(false) { resource.error = resource.value["hot_reload"]["message"].stringValue }
                else if action == "remove" { dismiss() }
            }
        }
    }
    private func test() async {
        if await resource.change(store, "settings.mcp.test", ["name": .string(name)]) {
            allTools = resource.value["last_action"]["tool_names"].array.compactMap(\.nonemptyString)
        }
    }
    private func rawName(_ name: String) -> String { let prefix = "mcp_\(self.name)_"; return name.hasPrefix(prefix) ? String(name.dropFirst(prefix.count)) : name }
    private func isReadOnly(_ name: String) -> Bool {
        let raw = rawName(name)
        return raw.range(of: "^(list|get|search|read|query|find|check|debug|fetch|view|describe|count|lookup)_", options: .regularExpression) != nil
    }
    private func toggleTool(_ tool: String, _ on: Bool) {
        var allowed = enabled.contains("*") ? tools : enabled
        allowed.removeAll { $0 == tool || $0 == rawName(tool) }
        if on { allowed.append(tool) }
        guard !allowed.isEmpty else { resource.error = "至少保留一个能力，或者暂停整个服务。"; return }
        Task { await resource.change(store, "settings.mcp.tools", ["name": .string(name), "enabled_tools": .array(allowed.map(JSONValue.string))]) }
    }
}
